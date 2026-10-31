"""Chapter-level problems are flagged and the run keeps going."""
import json

import pytest

import novel_runner
import provider_retry_policy
import review_queue
import command_code_provider_transport as transport
from test_goat_unattended import fake_runtime, novel
from test_production_goat_runtime import response

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(provider_retry_policy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(novel_runner.time, "sleep", lambda seconds: None)


def override_responses(project, monkeypatch, change):
    """change(chapter, kind) returns a replacement response, or None to keep the fake one."""
    real_factory = transport.create_command_code_client

    def factory(**kwargs):
        client = real_factory(**kwargs)
        real_create = client.chat.completions.create

        def create(**wire):
            records = [json.loads(path.read_text(encoding="utf-8")) for path in
                       (project.data_dir / "provider_request_journal").glob("*.json")]
            active = next(record for record in records if record["status"] == "started")
            result = real_create(**wire)
            return change(active["chapter"], active["operation_kind"]) or result
        client.chat.completions.create = create
        return client
    monkeypatch.setattr(transport, "create_command_code_client", factory)


def finished(project, count):
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    return progress["last_completed"] == count and progress["next_chapter"] == count + 1


def flagged(project):
    return {(item["chapter"], item["kind"].split(":")[0]) for item in review_queue.read_items(project.data_dir)}


def run(project, tmp_path, monkeypatch, change, count=3):
    fake_runtime(project, monkeypatch, tmp_path)
    override_responses(project, monkeypatch, change)
    return novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)


def test_refused_state_extraction_commits_and_flags(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)

    def refuse(chapter, kind):
        if chapter == 2 and "state" in kind:
            return response(content="", finish="content_filter")
    result = run(project, tmp_path, monkeypatch, refuse)
    assert result["status"] == novel_runner.COMPLETE, result
    assert finished(project, 3)
    assert (2, "state_extraction") in flagged(project)
    assert (project.chapter_dir / "0002.md").read_text(encoding="utf-8").strip()


def test_garbage_state_commits_and_flags(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)

    def garbage(chapter, kind):
        if chapter == 2 and "state" in kind:
            return response(content="not json and nothing repairable")
    result = run(project, tmp_path, monkeypatch, garbage)
    assert result["status"] == novel_runner.COMPLETE, result
    assert (2, "state_extraction") in flagged(project)


def test_residual_cjk_commits_and_flags(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)

    def leftover(chapter, kind):
        if chapter == 2 and kind == "chapter_translation":
            return response(content="Jia entered the Great Hall. " * 40 + "大厅")
    result = run(project, tmp_path, monkeypatch, leftover)
    assert result["status"] == novel_runner.COMPLETE, result
    assert (2, "commit_qa") in flagged(project)
    qa = json.loads((project.qa_dir / "0002.json").read_text(encoding="utf-8"))
    assert qa["commit_allowed"] is True and qa["flagged_blockers"]


def test_heavy_leftovers_translate_once_more_through_the_runner(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    seen = []

    def heavy_once(chapter, kind):
        if chapter == 2 and kind == "chapter_translation":
            seen.append(chapter)
            if len(seen) == 1:
                return response(content="Jia entered the Great Hall. " * 40 + "大厅" * 10)
    result = run(project, tmp_path, monkeypatch, heavy_once)
    assert result["status"] == novel_runner.COMPLETE, result
    assert len(seen) == 2 and finished(project, 3)
    assert (2, "fresh_translation") in flagged(project)
    assert (2, "commit_qa") not in flagged(project)
    assert "大厅" not in (project.chapter_dir / "0002.md").read_text(encoding="utf-8")
    assert list((project.backup_dir / "superseded_translations").rglob("0002.md"))


def test_truncated_translation_is_still_a_hard_stop(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)

    def empty(chapter, kind):
        if chapter == 2 and kind == "chapter_translation":
            return response(content="")
    result = run(project, tmp_path, monkeypatch, empty)
    assert result["status"] == novel_runner.STOPPED, result
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["last_completed"] == 1


def test_bad_glossary_review_commits_and_flags(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)

    def bad(chapter, kind):
        if chapter == 1 and "glossary_review" in kind:
            return response(content="this is not a review")
    result = run(project, tmp_path, monkeypatch, bad)
    assert result["status"] == novel_runner.COMPLETE, result
    assert (1, "glossary_review") in flagged(project)
    assert finished(project, 3)


def test_failed_audit_chunk_keeps_terms_and_finishes(tmp_path, monkeypatch):
    project = novel(tmp_path, count=26)

    def bad(chapter, kind):
        if "periodic_audit" in kind:
            return response(content="this is not an audit")
    glossary_before = None
    result = run(project, tmp_path, monkeypatch, bad, count=26)
    assert result["status"] == novel_runner.COMPLETE, result
    assert finished(project, 26)
    assert any(kind == "audit_chunk" for _, kind in flagged(project))
    glossary_after = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert glossary_before is None and glossary_after


def test_provider_stop_during_review_is_not_treated_as_bad_review(tmp_path, monkeypatch):
    import httpx
    import openai
    project = novel(tmp_path, count=3)

    def rejected(chapter, kind):
        if chapter == 1 and "glossary_review" in kind:
            request = httpx.Request("POST", "https://example.test")
            raise openai.APIStatusError("no", response=httpx.Response(401, request=request), body={})
    result = run(project, tmp_path, monkeypatch, rejected)
    assert result["status"] == novel_runner.STOPPED, result
    assert result["reason"] == "provider_key_or_credit", result
    assert (1, "glossary_review") not in flagged(project)


def test_flagged_leftover_is_repaired_at_the_next_audit(tmp_path, monkeypatch):
    project = novel(tmp_path, count=52)  # A second audit must accept the edit made after the first.
    repairs = []

    def leftover_then_fixed(chapter, kind):
        if chapter == 2 and kind == "chapter_translation":
            return response(content="Jia entered the Great Hall. " * 40 + "\n\nThen the \u5927\u5385 stood quiet and very still.")
        if chapter == 2 and kind == "residual_cjk_micro_repair":
            repairs.append(kind)
            if len(repairs) == 1:
                return response(content='{"repairs": []}')
            return response(content=json.dumps({"repairs": [{"old_text": "the \u5927\u5385 stood", "new_text": "the hall stood"}]}))
    result = run(project, tmp_path, monkeypatch, leftover_then_fixed)
    assert result["status"] == novel_runner.COMPLETE, result
    assert len(repairs) == 2
    text = (project.chapter_dir / "0002.md").read_text(encoding="utf-8")
    assert "\u5927\u5385" not in text and "the hall stood" in text
    assert "\u5927\u5385" in (project.backup_dir / "flag_retry" / "0002_before.md").read_text(encoding="utf-8")
    assert review_queue.open_items(project.data_dir) == []


def test_flag_stays_open_when_the_retry_also_fails_and_is_tried_only_once(tmp_path, monkeypatch):
    project = novel(tmp_path, count=26)
    repairs = []

    def never_fixed(chapter, kind):
        if chapter == 2 and kind == "chapter_translation":
            return response(content="Jia entered the Great Hall. " * 40 + "\n\nThen the \u5927\u5385 stood quiet and very still.")
        if chapter == 2 and kind == "residual_cjk_micro_repair":
            repairs.append(kind)
            return response(content='{"repairs": []}')
    result = run(project, tmp_path, monkeypatch, never_fixed)
    assert result["status"] == novel_runner.COMPLETE, result
    assert len(repairs) == 2  # At the commit, then once at the first audit. The final audit doesn't retry.
    assert [item["kind"] for item in review_queue.open_items(project.data_dir)] == ["commit_qa"]


def test_requested_correction_changes_the_term_and_past_chapters_at_the_next_audit(tmp_path, monkeypatch):
    import requested_corrections
    project = novel(tmp_path, count=26)
    queued = []

    def ask_midway(chapter, kind):
        if chapter == 12 and not queued:
            queued.append(True)
            requested_corrections.add_request(project.data_dir, chinese="\u5927\u5385", new_english="Grand Hall")
    result = run(project, tmp_path, monkeypatch, ask_midway)
    assert result["status"] == novel_runner.COMPLETE, result
    glossary = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert glossary["\u5927\u5385"]["english"] == "Grand Hall"
    text = (project.chapter_dir / "0005.md").read_text(encoding="utf-8")
    assert "Grand Hall" in text and "Great Hall" not in text
    assert requested_corrections.waiting(project.data_dir) == []
    assert list(project.backup_dir.glob("retroactive_*/chapters_before/0005.md"))


def test_requested_correction_for_an_unknown_term_is_refused(tmp_path, capsys):
    import translate
    project = novel(tmp_path, count=3)
    code = translate.main(["--novel", "fixture", "--repo-root", str(tmp_path), "--correct-term", "\u4e0d\u5728", "X"])
    assert code == 2 and "isn't in this novel's glossary" in capsys.readouterr().out


def test_term_locks_at_the_next_audit_and_the_last_audit_looks_again(tmp_path, monkeypatch):
    project = novel(tmp_path, count=51)
    result = run(project, tmp_path, monkeypatch, lambda chapter, kind: None)
    assert result["status"] == novel_runner.COMPLETE, result
    entry = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]["\u5927\u5385"]
    assert entry["status"] == "locked" and entry["promoted_by_audit"] is True
    audits = json.loads(project.audit_history_file.read_text(encoding="utf-8"))["audits"]
    assert [(audit["start_chapter"], "\u5927\u5385" in audit["changes"]["kept"]) for audit in audits] == [
        (1, True), (26, False), (51, True)]


def test_term_is_not_locked_by_the_audit_that_first_keeps_it(tmp_path, monkeypatch):
    project = novel(tmp_path, count=25)
    result = run(project, tmp_path, monkeypatch, lambda chapter, kind: None)
    assert result["status"] == novel_runner.COMPLETE, result
    assert json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]["\u5927\u5385"]["status"] == "provisional"


def test_run_end_leaves_a_current_reader_epub(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    result = run(project, tmp_path, monkeypatch, lambda chapter, kind: None)
    assert result["status"] == novel_runner.COMPLETE, result
    # The final audit already rebuilt it, so the run end finds it current.
    assert result["reader_epub"] == "SKIPPED"
    assert (project.epub_dir / "fixture_latest.epub").is_file()


def test_failed_reader_epub_never_changes_the_run_result(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    monkeypatch.setattr(novel_runner, "rebuild_reader_epub", lambda *a, **k: {"status": "ERROR"})
    result = run(project, tmp_path, monkeypatch, lambda chapter, kind: None)
    assert result["status"] == novel_runner.COMPLETE, result
    assert result["reader_epub"] == "ERROR"


def test_soften_leaves_hard_blockers_alone():
    hard = {"commit_allowed": False, "commit_blockers": [{"type": "catastrophic_completeness"}]}
    assert review_queue.soften_commit_blockers(hard) == hard
    mixed = {"commit_allowed": False, "commit_blockers": [{"type": "residual_cjk"}, {"type": "x"}]}
    assert review_queue.soften_commit_blockers(mixed) == mixed
    soft = {"commit_allowed": False, "commit_blockers": [{"type": "locked_terminology"}]}
    assert review_queue.soften_commit_blockers(soft)["commit_allowed"] is True


def test_queue_skips_duplicates_and_survives_bad_lines(tmp_path):
    review_queue.add_item(tmp_path, chapter=1, kind="x", detail="a")
    review_queue.add_item(tmp_path, chapter=1, kind="x", detail="b")
    with (tmp_path / review_queue.QUEUE_FILE).open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    assert len(review_queue.read_items(tmp_path)) == 1
