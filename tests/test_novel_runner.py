"""One start command, using the real lifecycle with fake credentials and transport."""
import json

import httpx
import openai
import pytest

import novel_runner
import provider_retry_policy
import translate
import command_code_provider_transport as transport
from test_goat_unattended import fake_runtime, novel

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(provider_retry_policy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(novel_runner.time, "sleep", lambda seconds: None)


def progress(project):
    return json.loads(project.progress_file.read_text(encoding="utf-8"))


def wrap_create(monkeypatch, behavior):
    """Run behavior(call_number) before each fake send; it may raise."""
    real_factory = transport.create_command_code_client
    calls = []

    def factory(**kwargs):
        client = real_factory(**kwargs)
        real_create = client.chat.completions.create

        def create(**wire):
            calls.append(wire)
            behavior(len(calls))
            return real_create(**wire)
        client.chat.completions.create = create
        return client
    monkeypatch.setattr(transport, "create_command_code_client", factory)
    return calls


def status_error(status, message="error"):
    request = httpx.Request("POST", "https://example.test")
    return openai.APIStatusError(message, response=httpx.Response(status, request=request), body={"message": message})


def test_start_runs_whole_novel_with_one_command(tmp_path, monkeypatch, capsys):
    project = novel(tmp_path)
    fake_runtime(project, monkeypatch, tmp_path)
    code = translate.main(["--novel", "fixture", "--repo-root", str(tmp_path), "--start", "--budget-usd", "10"])
    output = capsys.readouterr().out
    result = json.loads(output[output.rfind('\n{\n  "status"') + 1:])
    assert code == 0 and result["status"] == novel_runner.COMPLETE, result
    assert progress(project)["last_completed"] == 26
    assert json.loads((project.data_dir / "last_run.json").read_text(encoding="utf-8"))["status"] == novel_runner.COMPLETE


def test_no_budget_stops_with_plain_message(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path)
    assert result["status"] == novel_runner.STOPPED and result["reason"] == "no_budget"


def test_budget_too_small_stops_cleanly(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    _, sends, _, _ = fake_runtime(project, monkeypatch, tmp_path)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=0.001)
    assert result["status"] == novel_runner.BUDGET_REACHED
    assert progress(project)["last_completed"] == 0 and sends == []


def test_budget_persists_across_runs(tmp_path, monkeypatch):
    project = novel(tmp_path, count=2)
    fake_runtime(project, monkeypatch, tmp_path)
    first = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert first["status"] == novel_runner.COMPLETE, first
    second = novel_runner.run_novel("fixture", repo_root=tmp_path)
    assert second["status"] == novel_runner.COMPLETE, second


def test_transient_errors_do_not_stop_the_run(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)

    def flaky(call):
        if call in {1, 4}:
            raise status_error(503)
    wrap_create(monkeypatch, flaky)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert result["status"] == novel_runner.COMPLETE, result
    assert progress(project)["last_completed"] == 3


def test_interrupted_request_resends_once_and_finishes(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)

    def crash(call):
        if call == 2:
            raise KeyboardInterrupt
    wrap_create(monkeypatch, crash)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert result["status"] == novel_runner.COMPLETE, result
    assert (project.data_dir / "provider_resends.jsonl").is_file()
    translations = [path for path in (project.data_dir / "provider_request_journal").glob("*chapter_translation.json")]
    assert len(translations) == 3


def test_bad_key_stops_with_actionable_message(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)

    def rejected(call):
        raise status_error(401)
    wrap_create(monkeypatch, rejected)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert result["status"] == novel_runner.STOPPED, result
    assert result["reason"] == "provider_key_or_credit" and "API key" in result["message"], result
    assert progress(project)["last_completed"] == 0
    journal = json.loads(next((project.data_dir / "provider_request_journal").glob("*.json")).read_text(encoding="utf-8"))
    assert journal["status"] == "planned"


def test_pause_flag_returns_paused(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)
    project.pause_file.write_text("", encoding="utf-8")
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert result["status"] == novel_runner.PAUSED


def test_second_ambiguous_failure_blocks_with_message(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    fake_runtime(project, monkeypatch, tmp_path)

    network_is_bad = [True]

    def cut_off(call):
        if network_is_bad:
            raise KeyboardInterrupt
    wrap_create(monkeypatch, cut_off)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert result["status"] == novel_runner.STOPPED
    assert result["reason"] == "ambiguous_request_blocked"
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["reason"] == "ambiguous_request_blocked"

    assert novel_runner.allow_resend(project)
    assert novel_runner.allow_resend(project) == []
    network_is_bad.clear()
    assert novel_runner.run_novel("fixture", repo_root=tmp_path)["status"] == novel_runner.COMPLETE


def test_crash_after_paid_translation_replays_without_resending(tmp_path, monkeypatch):
    project = novel(tmp_path, count=3)
    failed = []

    def fail_once(module):
        real = module.save_json

        def save_json(path, value):
            if str(path).endswith("0002.json") and "pending" in str(path) and not failed:
                failed.append(path)
                raise OSError("disk hiccup")
            return real(path, value)
        module.save_json = save_json
    _, _, operations, _ = fake_runtime(project, monkeypatch, tmp_path, load_hook=fail_once)
    result = novel_runner.run_novel("fixture", repo_root=tmp_path, budget_usd=10)
    assert failed and result["status"] == novel_runner.COMPLETE, result
    assert [n for n, kind in operations if kind == "chapter_translation"] == [1, 2, 3]
