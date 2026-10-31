"""The interface's API and HTTP layer on synthetic novels. No sockets connect here: the zero-provider
guard forbids them, so HTTP tests feed raw requests straight into the real handler."""
import io
import json
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import novel_runner
import provider_retry_policy
import requested_corrections
import review_queue
import translate
from production_run_lock import production_run_lock
from test_goat_unattended import fake_runtime, novel
from ui import demo
from ui import server as ui_server
from ui.api import App, Request, safe_upload_name, ApiError
from ui.worker import Workers, process_start_time, translate_command

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")
REPO = Path(__file__).resolve().parents[1]
TEST_KEY = "cc-fake-key-for-tests"
HALL = "大厅"


class FakeDpapi:
    def protect(self, plaintext):
        return b"protected:" + bytes(byte ^ 0x5a for byte in plaintext)


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(provider_retry_policy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(novel_runner.time, "sleep", lambda seconds: None)


def make_app(tmp_path, **kwargs):
    if not (tmp_path / "profiles").exists():
        shutil.copytree(REPO / "profiles", tmp_path / "profiles")
    kwargs.setdefault("secret_path", tmp_path / "secrets" / "key.dpapi")
    kwargs.setdefault("dpapi", FakeDpapi())
    kwargs.setdefault("require_key", False)
    return App(tmp_path, **kwargs)


def call(app, method, path, value=None, raw=None):
    body = raw if raw is not None else json.dumps(value or {}).encode("utf-8") if method == "POST" else b""
    response = app.handle(Request(method, "/api/" + path, io.BytesIO(body), len(body)))
    return response.status, response.value if response.file_path is None else response


def ok(app, method, path, value=None, raw=None):
    status, result = call(app, method, path, value, raw)
    assert status == 200, result
    return result


def commit_chapters(project, through, audited=0):
    """Pretend chapters were translated, without running the engine."""
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, through + 1):
        (project.chapter_dir / f"{number:04d}.md").write_text(f"# Chapter {number}\n\nJia walked.\n", encoding="utf-8", newline="\n")
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress.update(completed=list(range(1, through + 1)), last_completed=through, next_chapter=through + 1,
                    last_audited_chapter=audited, rate_correct_cost_usd=0.5)
    project.progress_file.write_text(json.dumps(progress), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    return novel(tmp_path, count=6)


@pytest.fixture
def app(tmp_path, project):
    return make_app(tmp_path, command=lambda *args: pytest.fail("no worker may start in this test"))


# A real separate process stands in for translate.py: it holds the novel's lock until told to stop.
HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import time
from production_run_lock import production_run_lock
with production_run_lock(sys.argv[2]):
    Path(sys.argv[3]).write_text("held")
    while not Path(sys.argv[4]).exists():
        time.sleep(0.05)
"""


@pytest.fixture
def holder(tmp_path, project):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    ready, stop = tmp_path / "held", tmp_path / "stop"
    started = []

    def command(repo_root, novel_id, budget_usd=None, trial=None):
        started.append(dict(novel_id=novel_id, budget=budget_usd, trial=trial))
        return [sys.executable, str(script), str(REPO), str(project.root), str(ready), str(stop)]

    def wait_for_ready():
        for _ in range(400):
            if ready.exists():
                return
            pause_briefly()
        pytest.fail("the worker process never took the lock")

    yield NS(command=command, started=started, ready=wait_for_ready, stop=stop)
    stop.write_text("stop")


def pause_briefly():
    threading.Event().wait(0.05)  # no_waits replaces time.sleep.


def wait_until_stopped(app, project):
    for _ in range(400):
        if not app.workers.running(project):
            return
        pause_briefly()
    pytest.fail("the worker process didn't stop")


# Library and status ------------------------------------------------------

def test_library_shows_progress_cost_flags_and_a_broken_novel(tmp_path, app, project):
    commit_chapters(project, 2)
    review_queue.add_item(project.data_dir, chapter=2, kind="commit_qa", detail="leftover source text")
    novel_runner.set_budget(project, 3)
    broken = tmp_path / "novels" / "broken"
    broken.mkdir()
    result = ok(app, "GET", "novels")
    fixture, bad = {row["id"]: row for row in result["novels"]}["fixture"], {row["id"]: row for row in result["novels"]}["broken"]
    assert (fixture["completed"], fixture["final"], fixture["cost_usd"], fixture["flagged"]) == (2, 6, 0.5, 1)
    assert fixture["budget_left_usd"] == 3 and fixture["state"] == "ready"
    assert bad["valid"] is False and "problem" in bad["error"]


def test_empty_library(tmp_path):
    assert ok(make_app(tmp_path), "GET", "novels") == dict(novels=[], queue=[])


def test_novel_detail_shows_stop_message_and_log(app, project):
    message, action = novel_runner.STOP_MESSAGES["provider_refused"]
    (project.data_dir / "last_run.json").write_text(json.dumps(dict(
        status=novel_runner.STOPPED, message=message, action=action, finished_utc="2026-01-01T00:00:00+00:00")), encoding="utf-8")
    project.logs_dir.mkdir(exist_ok=True)
    (project.logs_dir / "run_1.jsonl").write_text('{"event": "chapter_commit", "chapter": 3, "stage": "commit"}\nnot json\n', encoding="utf-8")
    detail = ok(app, "GET", "novels/fixture")
    assert (detail["state"], detail["message"], detail["action"]) == ("stopped", message, action)
    assert detail["log"]["events"] == ["chapter 3 chapter_commit (commit)"]


def test_unknown_routes_and_methods(app):
    assert call(app, "GET", "nothing")[0] == 404
    assert call(app, "POST", "novels/fixture")[0] == 405


# Running -----------------------------------------------------------------

def test_worker_command_matches_the_agreed_form():
    command = translate_command(Path("repo"), "book", 5.0, 3)
    assert command[1:] == [str(Path("repo") / "translate.py"), "--novel", "book", "--start", "--repo-root", "repo",
                           "--budget-usd", "5.0", "--trial", "3"]


def test_start_needs_a_budget_and_rejects_bad_numbers(tmp_path, project):
    app = make_app(tmp_path, command=lambda *args: pytest.fail("must not start"))
    assert call(app, "POST", "novels/fixture/start")[0] == 400
    for bad in (dict(budget_usd=-1), dict(budget_usd="5"), dict(budget_usd=0), dict(budget_usd=True),
                dict(budget_usd=5, trial=0), dict(budget_usd=5, trial=1.5)):
        assert call(app, "POST", "novels/fixture/start", bad)[0] == 400, bad


def test_start_without_a_stored_key_says_so_and_starts_nothing(tmp_path, project):
    app = make_app(tmp_path, require_key=True, command=lambda *args: pytest.fail("must not start"))
    status, result = call(app, "POST", "novels/fixture/start", dict(budget_usd=5))
    assert status == 400 and "API key" in result["error"]


def test_real_process_runs_on_and_is_seen_after_the_server_restarts(tmp_path, project, holder, monkeypatch):
    app = make_app(tmp_path, command=holder.command)
    assert ok(app, "POST", "novels/fixture/start", dict(budget_usd=5, trial=2))["started"] is True
    holder.ready()
    assert holder.started == [dict(novel_id="fixture", budget=5.0, trial=2)]
    assert ok(app, "GET", "novels/fixture")["state"] == "running"
    assert call(app, "POST", "novels/fixture/start", dict(budget_usd=5))[0] == 409

    reopened = make_app(tmp_path, command=holder.command)
    assert ok(reopened, "GET", "novels/fixture")["state"] == "running"

    holder.stop.write_text("stop")
    wait_until_stopped(reopened, project)
    # It left no result behind, so the page says so rather than claiming it's ready.
    detail = ok(reopened, "GET", "novels/fixture")
    assert detail["state"] == "stopped" and "without reporting" in detail["message"]


def test_a_run_started_elsewhere_is_seen_through_the_lock(app, project):
    with production_run_lock(project.root):
        assert ok(app, "GET", "novels/fixture")["state"] == "running"
        assert call(app, "POST", "novels/fixture/start", dict(budget_usd=5))[0] == 409


def test_gaps_between_lock_holds_still_count_as_running(tmp_path, project, monkeypatch):
    """novel_runner lets go of the lock between steps. The process record covers those gaps."""
    app = make_app(tmp_path)
    app.workers._record_path("fixture").parent.mkdir(parents=True)
    import os
    app.workers._record_path("fixture").write_text(json.dumps(dict(
        pid=os.getpid(), process_start=process_start_time(os.getpid()), started_utc="2026-01-01T00:00:00+00:00")), encoding="utf-8")
    assert ok(app, "GET", "novels/fixture")["state"] == "running"
    assert call(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="X"))[0] == 409


def test_a_reused_process_id_is_not_mistaken_for_the_worker(tmp_path, project):
    app = make_app(tmp_path)
    app.workers._record_path("fixture").parent.mkdir(parents=True)
    import os
    app.workers._record_path("fixture").write_text(json.dumps(dict(pid=os.getpid(), process_start=1)), encoding="utf-8")
    assert app.workers.running(project) is False


def test_pause_and_resume_use_the_flag_and_leave_a_waiting_run_alone(tmp_path, project, holder):
    app = make_app(tmp_path, command=holder.command)
    ok(app, "POST", "novels/fixture/start", dict(budget_usd=5))
    holder.ready()
    ok(app, "POST", "novels/fixture/pause")
    assert project.pause_file.exists()
    assert ok(app, "GET", "novels/fixture")["state"] == "pausing"
    assert ok(app, "POST", "novels/fixture/resume")["started"] is False
    assert not project.pause_file.exists()
    assert len(holder.started) == 1


def test_paused_novel_wont_start_and_resume_starts_it_if_budget_remains(tmp_path, project, holder):
    app = make_app(tmp_path, command=holder.command)
    ok(app, "POST", "novels/fixture/pause")
    assert ok(app, "GET", "novels/fixture")["state"] == "paused"
    assert call(app, "POST", "novels/fixture/start", dict(budget_usd=5))[0] == 409
    assert "more budget" in ok(app, "POST", "novels/fixture/resume")["message"]
    assert holder.started == []
    novel_runner.set_budget(project, 2)
    ok(app, "POST", "novels/fixture/pause")
    assert ok(app, "POST", "novels/fixture/resume")["started"] is True
    holder.ready()


def test_budget_endpoint(app, project):
    assert ok(app, "POST", "novels/fixture/budget", dict(budget_usd=7.5))["budget_left_usd"] == 7.5
    assert call(app, "POST", "novels/fixture/budget", dict(budget_usd=-2))[0] == 400


def test_default_budget_is_used_when_the_novel_has_none(tmp_path, project):
    seen = []
    app = make_app(tmp_path, command=lambda *args: seen.append(args) or [sys.executable, "-c", ""])
    ok(app, "POST", "settings", dict(default_budget_usd=4))
    assert ok(app, "POST", "novels/fixture/start")["started"] is True
    assert seen[0][2] == 4.0


def two_novels(tmp_path, project):
    second = tmp_path / "novels" / "second"
    second.mkdir(parents=True)
    for name in ("data",):
        (second / name).mkdir()
    config = json.loads(project.config_file.read_text(encoding="utf-8"))
    (second / "novel.json").write_text(json.dumps({**config, "id": "second"}), encoding="utf-8")
    for path in project.data_dir.iterdir():
        if path.is_file():
            (second / "data" / path.name).write_bytes(path.read_bytes())
    from novel_project import load_novel_project
    return load_novel_project("second", tmp_path)


def test_one_novel_at_a_time_with_a_queue_that_moves_on(tmp_path, project, holder):
    second = two_novels(tmp_path, project)
    app = make_app(tmp_path, command=holder.command)
    ok(app, "POST", "novels/fixture/start", dict(budget_usd=5))
    holder.ready()
    queued = ok(app, "POST", "novels/second/start", dict(budget_usd=6, trial=1))
    assert queued["queued"] is True and queued["started"] is False
    assert ok(app, "GET", "novels/second")["state"] == "queued"
    assert app.tick() is None
    assert ok(app, "GET", "queue")["queue"] == [dict(novel_id="second", budget_usd=6.0, trial=1)]
    assert (tmp_path / "novels" / ".ui" / "queue.json").is_file()

    holder.stop.write_text("stop")
    wait_until_stopped(app, project)
    assert app.tick() == "second"
    assert holder.started[-1] == dict(novel_id="second", budget=6.0, trial=1)
    assert ok(app, "GET", "queue")["queue"] == []
    assert second.config.id == "second"


def test_queue_reorder_and_remove(tmp_path, project):
    two_novels(tmp_path, project)
    app = make_app(tmp_path)
    app.workers.enqueue("fixture", 1.0)
    app.workers.enqueue("second", 2.0)
    assert [e["novel_id"] for e in ok(app, "POST", "queue", dict(order=["second", "fixture"]))["queue"]] == ["second", "fixture"]
    assert call(app, "POST", "queue", dict(order=["second"]))[0] == 400
    assert call(app, "POST", "queue", dict(order="second"))[0] == 400
    assert [e["novel_id"] for e in ok(app, "POST", "queue/remove", dict(novel_id="second"))["queue"]] == ["fixture"]


# Writes refused while a run is active ------------------------------------

def test_every_write_to_glossary_chapters_and_flags_is_refused_while_running(app, project):
    commit_chapters(project, 2)
    review_queue.add_item(project.data_dir, chapter=2, kind="commit_qa")
    sha = ok(app, "GET", "novels/fixture/chapters/2")["sha256"]
    glossary_before = project.glossary_file.read_bytes()
    chapter_before = (project.chapter_dir / "0002.md").read_bytes()
    with production_run_lock(project.root):
        assert call(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="X"))[0] == 409
        assert call(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="X", correct_past=True))[0] == 409
        assert call(app, "POST", "novels/fixture/chapters/2", dict(text="new", expected_sha256=sha))[0] == 409
        assert call(app, "POST", "novels/fixture/review/resolve", dict(chapter=2, kind="commit_qa"))[0] == 409
        assert call(app, "POST", "novels/fixture/epub/rebuild")[0] == 409
        assert ok(app, "GET", "novels/fixture/chapters/2")["editable"] is False
    assert project.glossary_file.read_bytes() == glossary_before
    assert (project.chapter_dir / "0002.md").read_bytes() == chapter_before
    assert requested_corrections.waiting(project.data_dir) == []


# Review ------------------------------------------------------------------

def test_review_lists_and_closes_flags(app, project):
    commit_chapters(project, 2)
    review_queue.add_item(project.data_dir, chapter=2, kind="commit_qa", detail="a problem")
    review_queue.add_item(project.data_dir, chapter=2, kind="other", detail="another problem")
    items = ok(app, "GET", "novels/fixture/review")["items"]
    assert [(i["chapter"], i["kind"], i["detail"]) for i in items] == [(2, "commit_qa", "a problem"), (2, "other", "another problem")]
    assert ok(app, "POST", "novels/fixture/review/resolve", dict(chapter=2, kind="commit_qa"))["flagged"] == 1
    assert ok(app, "POST", "novels/fixture/review/resolve", dict(chapter=2, kind="other"))["flagged"] == 0
    assert call(app, "POST", "novels/fixture/review/resolve", dict(chapter=2, kind="other"))[0] == 404


# Glossary ----------------------------------------------------------------

def test_glossary_search_filter_edit_lock(app, project):
    glossary = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    glossary["entries"]["甲"] = dict(english="Jia", category="character", status="locked", first_chapter=1, aliases=["keep me"])
    project.glossary_file.write_text(json.dumps(glossary), encoding="utf-8")
    assert ok(app, "GET", "novels/fixture/glossary")["total"] == 2
    assert [e["english"] for e in ok(app, "GET", "novels/fixture/glossary?q=hall")["entries"]] == ["Great Hall"]
    assert [e["english"] for e in ok(app, "GET", "novels/fixture/glossary?q=%E7%94%B2")["entries"]] == ["Jia"]
    assert [e["english"] for e in ok(app, "GET", "novels/fixture/glossary?status=locked")["entries"]] == ["Jia"]
    assert [e["english"] for e in ok(app, "GET", "novels/fixture/glossary?category=location")["entries"]] == ["Great Hall"]

    assert "Past chapters keep" in ok(app, "POST", "novels/fixture/glossary", dict(chinese="甲", english="Jiaa", notes="n"))["message"]
    ok(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, status="locked"))
    saved = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert saved["甲"]["english"] == "Jiaa" and saved["甲"]["aliases"] == ["keep me"] and saved["甲"]["notes"] == "n"
    assert saved[HALL]["status"] == "locked"

    assert call(app, "POST", "novels/fixture/glossary", dict(chinese="missing", english="X"))[0] == 404
    assert call(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="  "))[0] == 400
    assert call(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, status="silly"))[0] == 400
    assert call(app, "POST", "novels/fixture/glossary", dict(chinese=["list"], english="X"))[0] == 400


def test_correcting_past_chapters_queues_and_leaves_the_entry_for_the_audit(app, project):
    result = ok(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="Grand Hall", correct_past=True))
    assert "next audit" in result["message"]
    assert result["corrections_waiting"] == [dict(chinese=HALL, new_english="Grand Hall")]
    assert json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"][HALL]["english"] == "Great Hall"


def test_after_the_last_audit_a_correction_says_it_will_wait(app, project):
    commit_chapters(project, 6, audited=6)
    result = ok(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="Grand Hall", correct_past=True))
    assert "nothing has changed yet" in result["message"] and "no audit left" in result["message"]
    assert "Queued" not in result["message"]
    assert requested_corrections.waiting(project.data_dir) == [dict(chinese=HALL, new_english="Grand Hall")]
    assert ok(app, "GET", "novels/fixture")["has_next_audit"] is False


# Chapters ----------------------------------------------------------------

def test_chapter_list_and_side_by_side(app, project):
    commit_chapters(project, 3, audited=1)
    review_queue.add_item(project.data_dir, chapter=2, kind="commit_qa")
    rows = ok(app, "GET", "novels/fixture/chapters")["chapters"]
    assert [(r["number"], r["translated"], r["audited"], r["flagged"]) for r in rows[:4]] == [
        (1, True, True, False), (2, True, False, True), (3, True, False, False), (4, False, False, False)]
    chapter = ok(app, "GET", "novels/fixture/chapters/3")
    assert HALL in chapter["source"] and "Jia walked" in chapter["english"] and chapter["editable"] is True
    untranslated = ok(app, "GET", "novels/fixture/chapters/4")
    assert untranslated["english"] is None and untranslated["editable"] is False


def test_manual_edit_keeps_a_backup_and_leaves_progress_alone(app, project):
    commit_chapters(project, 3)
    progress_before = project.progress_file.read_bytes()
    original = (project.chapter_dir / "0003.md").read_bytes()
    sha = ok(app, "GET", "novels/fixture/chapters/3")["sha256"]
    result = ok(app, "POST", "novels/fixture/chapters/3", dict(text="# Chapter 3\r\n\r\nEdited.\r\n", expected_sha256=sha))
    assert (project.chapter_dir / "0003.md").read_bytes() == b"# Chapter 3\n\nEdited.\n"
    assert (project.backup_dir / "manual_edits" / result["backup"]).read_bytes() == original
    assert project.progress_file.read_bytes() == progress_before
    assert json.loads((project.backup_dir / "manual_edits" / "edits.jsonl").read_text(encoding="utf-8"))["chapter"] == 3
    # The chapter moved on, so a tab still holding the old version is told to reopen.
    assert call(app, "POST", "novels/fixture/chapters/3", dict(text="again", expected_sha256=sha))[0] == 409


def test_manual_edit_input_checks(app, project):
    commit_chapters(project, 2)
    sha = ok(app, "GET", "novels/fixture/chapters/2")["sha256"]
    assert call(app, "POST", "novels/fixture/chapters/2", dict(text="  ", expected_sha256=sha))[0] == 400
    assert call(app, "POST", "novels/fixture/chapters/3", dict(text="x", expected_sha256=sha))[0] == 404
    assert ok(app, "POST", "novels/fixture/chapters/2", dict(
        text=(project.chapter_dir / "0002.md").read_text(encoding="utf-8"), expected_sha256=sha))["backup"] is None


def test_audited_chapters_are_refused_in_plain_words(app, project):
    commit_chapters(project, 3, audited=2)
    chapter = ok(app, "GET", "novels/fixture/chapters/2")
    assert chapter["editable"] is False and "audit" in chapter["edit_refusal"]
    before = (project.chapter_dir / "0002.md").read_bytes()
    status, result = call(app, "POST", "novels/fixture/chapters/2", dict(text="edited", expected_sha256=chapter["sha256"]))
    assert status == 409 and "stops the whole novel" in result["error"]
    assert (project.chapter_dir / "0002.md").read_bytes() == before


def test_a_chapter_inside_an_audit_that_began_but_did_not_finish_is_refused(app, project):
    commit_chapters(project, 3, audited=0)
    project.audit_dir.mkdir(parents=True, exist_ok=True)
    (project.audit_dir / "audit_0001_0002.finalization.json").write_text("{}", encoding="utf-8")
    sha = ok(app, "GET", "novels/fixture/chapters/2")["sha256"]
    assert call(app, "POST", "novels/fixture/chapters/2", dict(text="x", expected_sha256=sha))[0] == 409
    sha = ok(app, "GET", "novels/fixture/chapters/3")["sha256"]
    assert call(app, "POST", "novels/fixture/chapters/3", dict(text="x", expected_sha256=sha))[0] == 200


# What the engine does after a manual edit. This is why audited chapters are refused.

def run_to_trial(tmp_path, monkeypatch, count=27):
    project = novel(tmp_path, count=count)
    fake_runtime(project, monkeypatch, tmp_path)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10, trial_chapters=26)
    assert result["status"] == novel_runner.TRIAL_FINISHED, result
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert (progress["last_completed"], progress["last_audited_chapter"]) == (26, 25)
    return project


def test_restart_after_editing_an_audited_chapter_blocks_the_novel(tmp_path, monkeypatch):
    """Written directly to the file, as the API refuses it. Restoring the file clears the block."""
    project = run_to_trial(tmp_path, monkeypatch)
    path = project.chapter_dir / "0005.md"
    original = path.read_bytes()
    path.write_bytes(original + b"\nA manual line.\n")
    result = novel_runner.run_novel("fixture", repo_root=tmp_path)
    assert result["status"] == novel_runner.STOPPED and result["reason"] == "preflight_blocked"
    assert result["blockers"] == ["operator disposition/completed recovery proof failed"]
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["last_completed"] == 26
    path.write_bytes(original)
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["status"] == novel_runner.COMPLETE


def test_restart_after_an_api_edit_of_an_unaudited_chapter_continues(tmp_path, monkeypatch):
    project = run_to_trial(tmp_path, monkeypatch)
    app = make_app(tmp_path)
    assert ok(app, "GET", "novels/fixture/chapters/5")["editable"] is False
    chapter = ok(app, "GET", "novels/fixture/chapters/26")
    ok(app, "POST", "novels/fixture/chapters/26", dict(text=chapter["english"] + "\nA manual line.\n", expected_sha256=chapter["sha256"]))
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["status"] == novel_runner.COMPLETE
    assert "A manual line." in (project.chapter_dir / "0026.md").read_text(encoding="utf-8")


def test_restart_after_an_api_glossary_edit_on_an_audited_novel_continues(tmp_path, monkeypatch):
    project = run_to_trial(tmp_path, monkeypatch)
    app = make_app(tmp_path)
    ok(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="Grand Hall", status="locked"))
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["status"] == novel_runner.COMPLETE


def test_requested_correction_from_the_api_reaches_the_chapters_at_the_next_audit(tmp_path, monkeypatch):
    project = run_to_trial(tmp_path, monkeypatch)
    app = make_app(tmp_path)
    ok(app, "POST", "novels/fixture/glossary", dict(chinese=HALL, english="Grand Hall", correct_past=True))
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["status"] == novel_runner.COMPLETE
    assert requested_corrections.waiting(project.data_dir) == []
    assert json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"][HALL]["english"] == "Grand Hall"


# Start to EPUB with the real engine and a fake provider ------------------

def test_add_novel_run_it_and_download_the_epub(tmp_path, monkeypatch):
    def fake_worker(repo_root, novel_id, budget_usd=None, trial=None):
        return ["fake"]

    app = make_app(tmp_path, command=fake_worker)
    source = "样书\n\n" + "\n\n".join(f"第{n}章 雪\n\n" + "甲走进大厅。" * 40 for n in range(1, 4))
    upload = ok(app, "POST", "uploads?name=" + "%E6%A0%B7%E4%B9%A6.txt", raw=source.encode("utf-8"))
    assert upload["analysis"]["chapter_count"] == 3 and "source_path" not in upload["analysis"]
    assert ok(app, "POST", f"uploads/{upload['token']}/create",
              dict(title="Sample", novel_id="fixture", profile="historical_military"))["id"] == "fixture"
    assert not (tmp_path / "novels" / ".ui" / "uploads" / upload["token"]).exists()
    from novel_project import load_novel_project
    project = load_novel_project("fixture", tmp_path)
    fake_runtime(project, monkeypatch, tmp_path)

    # In production Popen launches this as its own process; here it runs in place for the fake provider.
    def run_in_place(command, **kwargs):
        assert translate.main(["--novel", "fixture", "--repo-root", str(tmp_path), "--start", "--budget-usd", "10"]) == 0
        return NS(pid=999999)
    monkeypatch.setattr("ui.worker.subprocess.Popen", run_in_place)
    monkeypatch.setattr("ui.worker.process_start_time", lambda pid: 12345 if pid == 999999 else None)
    ok(app, "POST", "novels/fixture/start", dict(budget_usd=10))
    monkeypatch.setattr("ui.worker.process_start_time", lambda pid: None)

    detail = ok(app, "GET", "novels/fixture")
    assert detail["state"] == "complete" and detail["completed"] == 3 and detail["cost_usd"] > 0
    assert detail["message"] == "The novel is fully translated."
    assert detail["epub"] == "fixture_latest.epub" and detail["epub"] in detail["epubs"]
    status, response = call(app, "GET", "novels/fixture/epub/fixture_latest.epub")
    assert status == 200 and response.file_path.read_bytes().startswith(b"PK")
    assert ok(app, "POST", "novels/fixture/epub/rebuild")["status"] == "SKIPPED"


# Adding a novel ----------------------------------------------------------

@pytest.mark.parametrize("name", ["../evil.txt", "..\\evil.txt", "a/b.txt", "a\\b.txt", "C:evil.txt", ".hidden.txt", "con.txt",
                                  "evil.exe", "novel", "a..b.txt", "bad\x00.txt", " space.txt", "dot.txt.", ""])
def test_unsafe_upload_names_are_refused(name):
    with pytest.raises(ApiError):
        safe_upload_name(name)


def test_upload_refusals_write_nothing(tmp_path, app):
    assert call(app, "POST", "uploads?name=..%2Fevil.txt", raw=b"x")[0] == 400
    assert call(app, "POST", "uploads?name=ok.txt", raw=b"")[0] == 400
    assert call(app, "POST", "uploads", raw=b"x")[0] == 400
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "novels" / "evil.txt").exists()


def test_cut_off_upload_is_cleaned_up(tmp_path, app):
    request = Request("POST", "/api/uploads?name=ok.txt", io.BytesIO(b"short"), 100)
    assert app.handle(request).status == 400
    uploads = tmp_path / "novels" / ".ui" / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir())


def test_a_large_upload_streams_in_chunks(tmp_path, app):
    body = ("第1章 雪\n\n" + "甲走进大厅。" * 200000).encode("utf-8")
    assert len(body) > 3 * 1024 * 1024
    assert ok(app, "POST", "uploads?name=big.txt", raw=body)["analysis"]["chapter_count"] == 1


def test_unusable_file_reports_the_problem_and_cant_be_created(app):
    upload = ok(app, "POST", "uploads?name=broken.epub", raw=b"this isn't an epub")
    assert upload["analysis"]["error"]
    assert call(app, "POST", f"uploads/{upload['token']}/create", dict(title="X", profile="historical_military"))[0] == 400


def test_create_refuses_bad_ids_and_tokens(tmp_path, app):
    upload = ok(app, "POST", "uploads?name=a.txt", raw=("第1章 雪\n\n" + "甲" * 50).encode("utf-8"))
    token = upload["token"]
    for novel_id in ("../evil", "Upper", ".ui", "fixture"):
        status, _ = call(app, "POST", f"uploads/{token}/create", dict(title="X", novel_id=novel_id, profile="historical_military"))
        assert status == 400, novel_id
    assert call(app, "POST", f"uploads/{token}/create", dict(title="  ", profile="historical_military"))[0] == 400
    assert call(app, "POST", f"uploads/{token}/create", dict(title="X", novel_id="new-one", profile="../evil"))[0] == 400
    assert call(app, "POST", "uploads/..%2F..%2Fsecrets/create", dict(title="X"))[0] == 400
    assert call(app, "POST", f"uploads/{'0' * 32}/create", dict(title="X"))[0] == 404
    assert not (tmp_path / "evil").exists()


def test_profiles_listed(app):
    assert "historical_military" in [item["id"] for item in ok(app, "GET", "profiles")["profiles"]]


def advice_client(reply):
    calls = []

    def create(**request):
        calls.append(request)
        return NS(choices=[NS(message=NS(content=json.dumps(reply)))], usage=NS(prompt_tokens=10, completion_tokens=5))
    return NS(chat=NS(completions=NS(create=create)), calls=calls)


def test_paid_advice_only_runs_when_explicitly_confirmed(tmp_path, project):
    client = advice_client(dict(profile_id="historical_military", confidence="high", reason="fits", addendum="Keep it plain."))
    app = make_app(tmp_path, advice_client=client)
    upload = ok(app, "POST", "uploads?name=a.txt", raw=("第1章 雪\n\n" + "甲" * 50).encode("utf-8"))
    assert client.calls == [], "reading a file must never spend anything"
    assert call(app, "POST", f"uploads/{upload['token']}/advice")[0] == 400
    assert call(app, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid="yes"))[0] == 400
    assert client.calls == []
    result = ok(app, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid=True))
    assert len(client.calls) == 1 and result["advice"]["profile_id"] == "historical_military"
    assert result["usage"]["prompt_tokens"] == 10


def test_paid_advice_needs_a_key_and_reports_failure_without_detail(tmp_path, project, monkeypatch):
    app = make_app(tmp_path)
    upload = ok(app, "POST", "uploads?name=a.txt", raw=("第1章 雪\n\n" + "甲" * 50).encode("utf-8"))
    assert "API key" in call(app, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid=True))[1]["error"]

    def explode(**request):
        raise RuntimeError("provider said something containing a secret")
    failing = make_app(tmp_path, advice_client=NS(chat=NS(completions=NS(create=explode))))
    status, result = call(failing, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid=True))
    assert status == 502 and "secret" not in result["error"]

    demo_mode = make_app(tmp_path, allow_paid_advice=False)
    assert call(demo_mode, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid=True))[0] == 409


def test_suggested_profile_can_be_saved(tmp_path, project):
    client = advice_client(dict(profile_id=None, reason="none fits", new_profile=dict(
        id="my-new-profile", display_name="Mine", description="d", instructions="Be plain.")))
    app = make_app(tmp_path, advice_client=client)
    upload = ok(app, "POST", "uploads?name=a.txt", raw=("第1章 雪\n\n" + "甲" * 50).encode("utf-8"))
    assert call(app, "POST", "profiles", dict(token=upload["token"], id="x", base_profile="historical_military"))[0] == 400
    ok(app, "POST", f"uploads/{upload['token']}/advice", dict(confirm_paid=True))
    saved = ok(app, "POST", "profiles", dict(token=upload["token"], id="my-new-profile", base_profile="historical_military"))
    assert "my-new-profile" in [item["id"] for item in saved["profiles"]]
    assert call(app, "POST", "profiles", dict(token=upload["token"], id="my-new-profile", base_profile="historical_military"))[0] == 400
    assert call(app, "POST", "profiles", dict(token=upload["token"], id="../evil", base_profile="historical_military"))[0] == 400


# Settings and the key ----------------------------------------------------

def test_key_is_stored_encrypted_and_never_comes_back(tmp_path, app, capsys):
    bodies = []
    def post(path, value):
        status, result = call(app, "POST", path, value)
        bodies.append(json.dumps(result))
        return status, result
    assert ok(app, "GET", "settings")["api_key_present"] is False
    assert post("settings/api-key", dict(key=TEST_KEY)) == (200, dict(api_key_present=True))
    for path in ("settings", "novels", "novels/fixture"):
        bodies.append(json.dumps(ok(app, "GET", path)))
    assert post("settings/api-key", dict(key="short"))[0] == 400
    assert post("settings/api-key", dict(key="has a space in it"))[0] == 400
    assert post("settings/api-key", dict(key=12345678))[0] == 400
    stored = (tmp_path / "secrets" / "key.dpapi").read_bytes()
    assert stored.startswith(b"protected:") and TEST_KEY.encode() not in stored
    assert ok(app, "GET", "settings")["api_key_present"] is True
    assert not any(TEST_KEY in body for body in bodies)
    captured = capsys.readouterr()
    assert TEST_KEY not in captured.out + captured.err
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TEST_KEY.encode() not in path.read_bytes(), path


def test_key_storage_failure_says_nothing_about_the_key(tmp_path, project):
    class Broken:
        def protect(self, plaintext):
            raise RuntimeError(plaintext.decode())
    status, result = call(make_app(tmp_path, dpapi=Broken()), "POST", "settings/api-key", dict(key=TEST_KEY))
    assert status == 500 and TEST_KEY not in result["error"]


def test_settings_default_budget(app):
    assert ok(app, "POST", "settings", dict(default_budget_usd=12.5))["default_budget_usd"] == 12.5
    assert call(app, "POST", "settings", dict(default_budget_usd=-1))[0] == 400
    assert ok(app, "POST", "settings", dict(default_budget_usd=None))["default_budget_usd"] is None


# Path safety -------------------------------------------------------------

@pytest.mark.parametrize("novel_id", ["..", "..%2F..%2Fsecrets", "%2E%2E", "Fixture", ".ui", "a%5Cb", "a%00b", "C%3A"])
def test_bad_novel_ids_are_refused(app, novel_id):
    for path in ("", "/review", "/glossary", "/chapters"):
        assert call(app, "GET", f"novels/{novel_id}{path}")[0] in (400, 404), (novel_id, path)


def test_missing_novel_is_404(app):
    assert call(app, "GET", "novels/nobody")[0] == 404


@pytest.mark.parametrize("number", ["0", "7", "-1", "1.5", "..", "..%2F..%2F0001", "0x1", "1234567", "%31%2F"])
def test_bad_chapter_numbers_are_refused(app, project, number):
    commit_chapters(project, 2)
    assert call(app, "GET", f"novels/fixture/chapters/{number}")[0] == 400
    assert call(app, "POST", f"novels/fixture/chapters/{number}", dict(text="x", expected_sha256=""))[0] == 400


@pytest.mark.parametrize("name", ["..%2F..%2Fnovel.json", "..%5C..%5Cnovel.json", "secret.txt", "..epub", "a..b.epub", ".epub", "x%00.epub"])
def test_epub_download_stays_inside_the_epub_folder(app, project, name):
    project.epub_dir.mkdir(parents=True, exist_ok=True)
    assert call(app, "GET", f"novels/fixture/epub/{name}")[0] == 400


def test_missing_epub_is_404(app, project):
    assert call(app, "GET", "novels/fixture/epub/none.epub")[0] == 404


# HTTP layer, through the real handler ------------------------------------

class FakeSocket:
    def __init__(self, raw):
        self.reader, self.written = io.BytesIO(raw), io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self.reader if "r" in mode else _NoClose(self.written)

    def sendall(self, data):
        self.written.write(data)

    def settimeout(self, value):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class _NoClose(io.BufferedIOBase):
    def __init__(self, target):
        self.target = target

    def write(self, data):
        return self.target.write(data)

    def flush(self):
        pass

    def writable(self):
        return True


PORT = 8765
GOOD_HOST = f"127.0.0.1:{PORT}"


def http(app, method, target, *, host=GOOD_HOST, origin=None, body=b""):
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}"] if host else [f"{method} {target} HTTP/1.1"]
    if origin:
        lines.append(f"Origin: {origin}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    connection = FakeSocket(raw)
    server = NS(app=app, server_address=("127.0.0.1", PORT))
    ui_server.Handler(connection, ("127.0.0.1", 1), server)
    head, _, payload = connection.written.getvalue().partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode("latin-1"), payload


def test_serves_the_app_and_its_files(app):
    status, head, page = http(app, "GET", "/")
    assert status == 200 and b"NovelTranslator" in page and "text/html" in head
    assert "Content-Security-Policy" in head and "X-Frame-Options: DENY" in head
    for name in ("app.js", "app.css", "index.html"):
        assert http(app, "GET", f"/static/{name}")[0] == 200


@pytest.mark.parametrize("target", ["/static/../api.py", "/static/..%2Fapi.py", "/static/%2e%2e/server.py", "/static/..\\api.py",
                                    "/static/../../AGENTS.md", "/static/", "/AGENTS.md", "/api.py", "/ui/server.py", "/novels/fixture/novel.json",
                                    "/static/app.js/../../api.py", "//etc/passwd", "/.ui/settings.json"])
def test_nothing_outside_static_is_served(app, target):
    status, _, payload = http(app, "GET", target)
    assert status == 404 and b"import" not in payload


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1", f"localhost.evil.example:{PORT}",
                                  f"127.0.0.1:{PORT + 1}", f"[::1]:{PORT}", "0.0.0.0:8765", None])
def test_other_hosts_are_refused(app, host):
    assert http(app, "GET", "/", host=host)[0] == 403
    assert http(app, "GET", "/api/novels", host=host)[0] == 403
    assert http(app, "POST", "/api/novels/fixture/pause", host=host, origin=f"http://{host}")[0] == 403


def test_both_local_names_are_accepted(app):
    assert http(app, "GET", "/api/novels")[0] == 200
    assert http(app, "GET", "/api/novels", host=f"localhost:{PORT}")[0] == 200
    assert http(app, "GET", "/api/novels", host=f"LOCALHOST:{PORT}")[0] == 200


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", f"https://{GOOD_HOST}", f"http://{GOOD_HOST}.evil.example", f"http://localhost:{PORT}"])
def test_post_needs_the_same_origin(app, project, origin):
    assert http(app, "POST", "/api/novels/fixture/pause", origin=origin)[0] == 403
    assert not project.pause_file.exists()


def test_same_origin_post_works(app, project):
    assert http(app, "POST", "/api/novels/fixture/pause", origin=f"http://{GOOD_HOST}")[0] == 200
    assert project.pause_file.exists()
    assert http(app, "POST", "/api/novels/fixture/resume", host=f"localhost:{PORT}", origin=f"http://localhost:{PORT}", body=b"{}")[0] == 200
    assert not project.pause_file.exists()


def test_bad_lengths_and_bad_json_are_refused(app):
    origin = f"http://{GOOD_HOST}"
    assert http(app, "POST", "/api/novels/fixture/budget", origin=origin, body=b"not json")[0] == 400
    assert http(app, "POST", "/api/novels/fixture/budget", origin=origin, body=b"[1]")[0] == 400


def test_epub_download_through_http(app, project):
    project.epub_dir.mkdir(parents=True, exist_ok=True)
    (project.epub_dir / "fixture_latest.epub").write_bytes(b"PKfake")
    status, head, payload = http(app, "GET", "/api/novels/fixture/epub/fixture_latest.epub")
    assert status == 200 and payload == b"PKfake"
    assert "attachment" in head and "application/epub+zip" in head


def test_an_unexpected_error_shows_no_detail_and_nothing_is_logged(app, monkeypatch, capsys):
    monkeypatch.setattr(app, "_routes", [("GET", ("boom",), lambda *args: (_ for _ in ()).throw(RuntimeError("private detail")))])
    status, _, payload = http(app, "GET", "/api/boom")
    assert status == 500 and b"private detail" not in payload


def test_key_in_a_request_never_reaches_the_response_or_console(app, capsys):
    body = json.dumps(dict(key=TEST_KEY)).encode()
    status, head, payload = http(app, "POST", "/api/settings/api-key", origin=f"http://{GOOD_HOST}", body=body)
    captured = capsys.readouterr()
    assert status == 200
    assert TEST_KEY not in head and TEST_KEY.encode() not in payload and TEST_KEY not in captured.out + captured.err


def test_binds_to_this_computer_only():
    assert ui_server.HOST == "127.0.0.1"


# Demo folder -------------------------------------------------------------

def test_demo_folder_and_fake_worker(tmp_path, monkeypatch):
    folder = tmp_path / "demo"
    monkeypatch.setenv("NOVELTRANSLATOR_NOVELS_ROOT", str(folder))
    made, sample = demo.make_demo_folder(folder)
    assert sample.is_file()
    app = demo.demo_app(REPO)
    assert app.allow_paid_advice is False and app.secret_path == folder / ".ui" / "demo_key.dpapi"
    novels = {row["id"]: row for row in ok(app, "GET", "novels")["novels"]}
    assert all(row["valid"] for row in novels.values()), novels
    assert novels["demo-midway"]["flagged"] == 2 and novels["demo-finished"]["epub"]
    assert ok(app, "GET", "novels/demo-midway/chapters/11")["editable"] is True
    assert ok(app, "GET", "novels/demo-midway/chapters/5")["editable"] is False
    monkeypatch.setattr(demo, "SECONDS_PER_CHAPTER", 0)
    assert demo.main(["--worker", "demo-midway", "--trial", "2"]) == 0
    detail = ok(app, "GET", "novels/demo-midway")
    assert detail["completed"] == 14 and detail["state"] == "trial"


def test_demo_mode_refuses_a_folder_it_did_not_make(tmp_path, project):
    with pytest.raises(ValueError):
        demo.demo_app(tmp_path)
