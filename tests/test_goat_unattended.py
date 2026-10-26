"""Actual launcher/lifecycle proofs using synthetic novels, fake DPAPI and transport."""
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import translate
import run_production_engine as launcher
import command_code_provider_transport as transport
import command_code_provider_credentials as credentials
from novel_project import load_novel_project
from operator_preflight import build_operator_preflight
from production_provider_runtime import provider_identity, PROVIDER_ENV
from production_run_lock import production_run_lock, held_production_run_lock
from semantic_integrity_audit import protected_state_snapshot
from test_controlled_trial_plan import _empty_project, _write_json
from test_production_goat_runtime import response, fake_credentials

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")
GOAT = "command_code_provider"


def novel(tmp_path, language="zh", count=26):
    project = _empty_project(tmp_path)
    config = json.loads(project.config_file.read_text(encoding="utf-8"))
    config.update(final_chapter=count, source_language=language,
        profile="historical_military" if language == "zh" else "korean_fantasy")
    _write_json(project.config_file, config)
    source = "\u7532\u8d70\u8fdb\u5927\u5385\u3002" if language == "zh" else "\ucca0\uc218\ub294 \ud640\uc5d0 \ub4e4\uc5b4\uc654\ub2e4."
    gate = "\u957f\u95e8" if language == "zh" else "\uae34\ubb38"
    _write_json(project.chapters_file, [dict(chapter=n, text=source * 40 + (gate if n == 26 else ""))
        for n in range(1, count + 1)])
    term = "\u5927\u5385" if language == "zh" else "\ud640"
    _write_json(project.glossary_file, {"entries": {term: dict(english="Great Hall", category="location",
        status="provisional", first_chapter=1)}})
    return load_novel_project("fixture", repo_root=tmp_path)


def arguments(project, calls=100, credit=10):
    return ["--novel", "fixture", "--repo-root", str(project.repository_root),
        "--max-api-calls", str(calls), "--max-credit-equivalent-usd", str(credit)]


def plan(project, capsys, calls=100, credit=10):
    args = arguments(project, calls, credit)
    assert translate.main(args + ["--plan-goat-run"]) == 0
    return json.loads(capsys.readouterr().out)


def execute(project, approved, capsys):
    code = translate.main(arguments(project, approved["max_api_calls"], approved["max_credit_equivalent_usd"]) + [
        "--authorize-goat-run", "--state-fingerprint", approved["state_fingerprint"],
        "--approval-token", approved["approval_token"]])
    output = capsys.readouterr().out
    # Lifecycle console is followed by the executor's structured report.
    marker = output.rfind('\n{\n  "mode":')
    return code, json.loads(output[marker + 1:] if marker >= 0 else output)


def fake_runtime(project, monkeypatch, tmp_path, *, interrupt=None, load_hook=None):
    events, sends, operations, audits = [], [], [], []
    fake_credentials(monkeypatch, tmp_path, events)
    original_load = launcher.load_production_module
    def load(*args, **kwargs):
        module = original_load(*args, **kwargs)
        module.load_dotenv = lambda: pytest.fail("dotenv forbidden")
        module.OpenAI = lambda **k: pytest.fail("direct provider forbidden")
        original_audit = module.run_periodic_audit
        def audit(start, end):
            audits.append((start, end))
            return original_audit(start, end)
        module.run_periodic_audit = audit
        if load_hook is not None:
            load_hook(module)
        return module
    monkeypatch.setattr(launcher, "load_production_module", load)
    def create(**wire):
        held_production_run_lock(project.root)
        assert wire["model"] == "deepseek/deepseek-v4.1-flash"
        assert wire["reasoning_effort"] == "low"
        records = [json.loads(p.read_text(encoding="utf-8")) for p in
            (project.data_dir / "provider_request_journal").glob("*.json")]
        active = [r for r in records if r["status"] == "started"]
        assert len(active) == 1
        kind = active[0]["operation_kind"]
        sends.append(wire)
        operations.append((active[0]["chapter"], kind))
        if interrupt is not None:
            interrupt(active[0], module=None)
        if kind == "chapter_translation":
            assert wire["max_completion_tokens"] == 50000
            person = "Jia" if project.config.source_language == "zh" else "Chulsu"
            text = f"{person} entered the Great Hall. " * 40
            if active[0]["chapter"] == 26:
                text += "They reached the Long Gate."
        else:
            assert "max_completion_tokens" not in wire and "max_tokens" not in wire
            prompt = wire["messages"][-1]["content"]
            if "AUDIT TARGETS:\n" in prompt:
                candidates = json.loads(prompt.split("AUDIT TARGETS:\n")[1].split("\nSTRUCTURED FAMILY CONTEXT:")[0])
                text = json.dumps(dict(decisions=[dict(candidate_id=t["candidate_id"], action="keep",
                    english=t["english"], notes="fixture") for t in candidates], issues=[]))
            elif "NEW CANDIDATES:\n" in prompt:
                candidates = json.loads(prompt.split("NEW CANDIDATES:\n")[1].split("\nCHINESE SOURCE:")[0])
                text = json.dumps(dict(decisions=[dict(candidate_id=t["candidate_id"], action="accept",
                    english=t["english"], category=t["category"], notes="fixture") for t in candidates], issues=[]))
            else:
                person = "Jia" if project.config.source_language == "zh" else "Chulsu"
                key = "\u7532" if project.config.source_language == "zh" else "\ucca0\uc218"
                terms = [dict(chinese=key, english=person, category="character")]
                if active[0]["chapter"] == 26:
                    gate = "\u957f\u95e8" if project.config.source_language == "zh" else "\uae34\ubb38"
                    terms.append(dict(chinese=gate, english="Long Gate", category="location"))
                text = json.dumps(dict(new_terms=terms,
                    continuity_summary=f"{person} entered the Great Hall.", issues=[]))
        return response(content=text)
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: NS(
        chat=NS(completions=NS(create=create)), close=lambda: events.append("closed")))
    return events, sends, operations, audits


@pytest.mark.parametrize("language", ["zh", "ko"])
def test_full_novel_26_sequential_chapters_real_qa_and_audits(tmp_path, monkeypatch, capsys, language):
    project = novel(tmp_path, language)
    events, sends, operations, audits = fake_runtime(project, monkeypatch, tmp_path)
    approved = plan(project, capsys)
    assert approved["provider_identity"] == provider_identity(None, None)
    code, report = execute(project, approved, capsys)
    assert code == 0 and report["status"] == "COMMITTED", report
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["completed"] == list(range(1, 27))
    assert progress["last_completed"] == progress["last_audited_chapter"] == 26
    assert progress["next_chapter"] == 27
    assert progress["provider"] == GOAT
    assert audits == [(1, 25), (26, 26)]
    assert [n for n, kind in operations if kind == "chapter_translation"] == list(range(1, 27))
    for n in range(1, 27):
        assert (project.chapter_dir / f"{n:04d}.md").read_text(encoding="utf-8").strip()
        qa = json.loads((project.qa_dir / f"{n:04d}.json").read_text(encoding="utf-8"))
        assert qa["commit_allowed"] and qa["translation_provider"] == GOAT
    assert not list(project.pending_dir.glob("*.md"))
    assert report["actual_credit_equivalent_usd"] > 0
    assert report["provider_invoice_claimed"] is False
    assert events.count("fake_dpapi") == 1
    journals = [json.loads(path.read_text(encoding="utf-8")) for path in
        (project.data_dir / "provider_request_journal").glob("*.json")]
    assert all(record["profile_id"] == project.config.profile for record in journals)
    for path in project.root.rglob("*"):
        if path.is_file():
            assert b"sk-cc-test-only" not in path.read_bytes()
    assert len(json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]) == 3
    assert any("periodic_audit" in kind for _, kind in operations)
    assert any("review" in kind for _, kind in operations)
    with pytest.raises(ValueError, match="consumed|drift"):
        execute(project, approved, capsys)


def test_default_unapproved_no_credentials_mutations_or_transport(tmp_path, monkeypatch):
    project = novel(tmp_path)
    before = protected_state_snapshot(project)
    monkeypatch.setattr(credentials, "read_command_code_api_key", lambda **k: pytest.fail("credentials"))
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: pytest.fail("transport"))
    result = build_operator_preflight("fixture", repo_root=tmp_path)
    assert result["status"] == "BOUNDED_ALLOWANCE_REQUIRED"
    assert result["provider_identity"]["wire_model"] == "deepseek/deepseek-v4.1-flash"
    with pytest.raises(ValueError, match="BOUNDED_ALLOWANCE_REQUIRED"):
        translate.main(["--novel", "fixture", "--repo-root", str(tmp_path)])
    assert protected_state_snapshot(project) == before
    assert not (project.root / ".production.lock").exists()


@pytest.mark.parametrize("calls,credit,completed", [(100, 0.00000001, 0), (1, 10, 0), (5, 10, 2)])
def test_allowance_stop_preserves_pending_and_separate_authorization(tmp_path, monkeypatch, capsys, calls, credit, completed):
    project = novel(tmp_path, count=4)
    events, sends, operations, audits = fake_runtime(project, monkeypatch, tmp_path)
    approved = plan(project, capsys, calls, credit)
    code, report = execute(project, approved, capsys)
    assert code == 0 and report["status"] == "ALLOWANCE_EXHAUSTED", report
    assert report["api_calls"] == len(sends) <= calls
    assert report["last_completed"] == completed
    records = [json.loads(p.read_text(encoding="utf-8")) for p in
        (project.data_dir / "provider_request_journal").glob("*.json")]
    assert all(r["status"] in {"planned", "response_recorded"} for r in records)
    with pytest.raises((ValueError, RuntimeError), match="consumed|drift|pending"):
        execute(project, approved, capsys)
    if calls == 1:
        assert report["pending_chapters"] == [1]
        assert (project.pending_dir / "0001.md").exists()
        with pytest.raises(RuntimeError, match="pending"):
            plan(project, capsys)
        return  # Existing dedicated pending recovery remains required.
    renewed = plan(project, capsys)
    assert renewed["approval_token"] != approved["approval_token"]
    code, resumed = execute(project, renewed, capsys)
    assert code == 0 and resumed["status"] == "COMMITTED", resumed
    assert [n for n, kind in operations if kind == "chapter_translation"] == [1, 2, 3, 4]


def test_lock_and_incompatible_routes_block_before_execution(tmp_path, monkeypatch, capsys):
    project = novel(tmp_path)
    fake_runtime(project, monkeypatch, tmp_path)
    approved = plan(project, capsys)
    with production_run_lock(project.root):
        with pytest.raises((RuntimeError, OSError)):
            execute(project, approved, capsys)
    for flags in (["--model", "pro"], ["--model", "flash"]):
        with pytest.raises(ValueError):
            translate.main(arguments(project) + flags + ["--plan-goat-run"])
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress.update(total_prompt_tokens=50, estimated_cost_usd=0.01)
    _write_json(project.progress_file, progress)
    with pytest.raises(RuntimeError, match="preflight"):
        plan(project, capsys)


def test_ambiguous_remote_outcome_cannot_be_resent(tmp_path, monkeypatch, capsys):
    project = novel(tmp_path)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("synthetic physical interruption")
    events, sends, *_ = fake_runtime(project, monkeypatch, tmp_path, interrupt=interrupt)
    approved = plan(project, capsys)
    code, report = execute(project, approved, capsys)
    assert code == 2 and len(sends) == 1
    with pytest.raises(RuntimeError, match="preflight"):
        plan(project, capsys)
    assert len(sends) == 1


def test_exact_response_replays_through_new_invocation_without_physical_send(tmp_path, monkeypatch, capsys):
    project = novel(tmp_path, count=2)
    def crash_after_recorded_response(module):
        original = module.atomic_write_text
        def write(path, text, *args, **kwargs):
            if Path(path).resolve().parent == project.pending_dir and Path(path).suffix == ".md":
                raise KeyboardInterrupt("synthetic crash before pending persistence")
            return original(path, text, *args, **kwargs)
        module.atomic_write_text = write
    events, sends, *_ = fake_runtime(project, monkeypatch, tmp_path, load_hook=crash_after_recorded_response)
    approved = plan(project, capsys)
    code, report = execute(project, approved, capsys)
    assert code == 2 and len(sends) == 1
    assert not list(project.pending_dir.glob("*.md"))
    # Remove the injected crash, keep all durable response evidence.
    # fake_runtime layers its wrapper on the original loader, so use a context
    # restore for just the injection instead of fabricating any paid state.
    monkeypatch.undo()
    from conftest import install_synthetic_goat_hard_bound
    install_synthetic_goat_hard_bound(monkeypatch, tmp_path)
    events, replay_sends, *_ = fake_runtime(project, monkeypatch, tmp_path)
    renewed = plan(project, capsys, credit=0.00000001)
    code, report = execute(project, renewed, capsys)
    assert code == 0 and report["status"] == "ALLOWANCE_EXHAUSTED"
    assert report["api_calls"] == 0 and replay_sends == []
    assert report["accepted_provider_responses"][0]["replayed"] is True
    assert (project.pending_dir / "0001.md").read_text(encoding="utf-8").startswith("Jia")


def test_manual_pause_after_translation_waits_locally_then_interrupts(tmp_path, monkeypatch, capsys):
    project = novel(tmp_path)
    def pause_after_send(*args, **kwargs):
        project.pause_file.write_text("pause", encoding="utf-8")
    def interrupt_sleep(module):
        def sleep(seconds):
            assert project.pause_file.is_file()
            raise KeyboardInterrupt("synthetic operator stop while paused")
        monkeypatch.setattr(module.time, "sleep", sleep)
    events, sends, *_ = fake_runtime(project, monkeypatch, tmp_path,
        interrupt=pause_after_send, load_hook=interrupt_sleep)
    approved = plan(project, capsys)
    code, report = execute(project, approved, capsys)
    assert code == 2 and len(sends) == report["api_calls"] == 1
    assert (project.pending_dir / "0001.md").exists()
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["last_completed"] == 0


def test_goat_accounting_cannot_switch_to_direct_even_explicitly(tmp_path, monkeypatch):
    from production_accounting import GOAT_METADATA
    project = novel(tmp_path)
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress.update(GOAT_METADATA, total_prompt_tokens=100, estimated_cost_usd=0.01)
    _write_json(project.progress_file, progress)
    before = protected_state_snapshot(project)
    monkeypatch.setattr(translate.subprocess, "run", lambda **k: pytest.fail("direct spawn forbidden"))
    with pytest.raises(ValueError, match="provider"):
        translate.main(["--novel", "fixture", "--repo-root", str(tmp_path),
            "--provider", "direct_deepseek", "--model", "pro"])
    assert protected_state_snapshot(project) == before


@pytest.mark.parametrize("calls,last", [(51, 25), (55, 26)])
def test_budget_stop_at_periodic_or_final_audit_resumes_audit_before_new_chapter(tmp_path, monkeypatch, capsys, calls, last):
    project = novel(tmp_path)
    events, sends, operations, audits = fake_runtime(project, monkeypatch, tmp_path)
    approved = plan(project, capsys, calls=calls)
    code, report = execute(project, approved, capsys)
    assert code == 0 and report["status"] == "ALLOWANCE_EXHAUSTED", report
    assert report["last_completed"] == last
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["last_audited_chapter"] == (0 if last == 25 else 25)
    assert len(sends) == calls
    renewed = plan(project, capsys)
    before = len(operations)
    code, report = execute(project, renewed, capsys)
    assert code == 0 and report["status"] == "COMMITTED", report
    assert "periodic_audit" in operations[before][1]
    assert [n for n, kind in operations if kind == "chapter_translation"] == list(range(1, 27))
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["last_audited_chapter"] == 26


def test_unapproved_limits_and_trial_token_are_not_unattended_authority(tmp_path, monkeypatch, capsys):
    from controlled_trial_plan import build_controlled_trial_plan
    project = novel(tmp_path)
    monkeypatch.setattr(credentials, "read_command_code_api_key", lambda **k: pytest.fail("credential activation"))
    before = protected_state_snapshot(project)
    with pytest.raises(ValueError, match="AUTHORIZATION_REQUIRED"):
        translate.main(arguments(project))
    assert not (project.root / ".production.lock").exists()
    trial = build_controlled_trial_plan("fixture", repo_root=tmp_path, max_api_calls=100,
        max_credit_equivalent_usd=10)
    with pytest.raises(ValueError, match="drift"):
        execute(project, trial, capsys)
    assert protected_state_snapshot(project) == before
    assert not (project.data_dir / "goat_run_authorizations").exists()
    result = build_operator_preflight("fixture", repo_root=tmp_path, max_credit_equivalent_usd=10)
    assert result["status"] == "BOUNDED_ALLOWANCE_REQUIRED"


def test_environment_selection_and_explicit_direct_pro_command(tmp_path, monkeypatch):
    project = novel(tmp_path)
    monkeypatch.setenv(PROVIDER_ENV, "direct_deepseek")
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", "pro")
    env = translate.production_environment(base_environment={PROVIDER_ENV: "direct_deepseek",
        "NOVELTRANSLATOR_TRANSLATION_MODEL": "pro"})
    assert env[PROVIDER_ENV] == "direct_deepseek" and env["NOVELTRANSLATOR_TRANSLATION_MODEL"] == "deepseek-v4-pro"
    # Explicit CLI dimensions win over either inherited selection.
    env = translate.production_environment("v41-flash", env, provider_selection=GOAT)
    assert env[PROVIDER_ENV] == GOAT and env["NOVELTRANSLATOR_TRANSLATION_MODEL"] == "deepseek-flash"
    observed = []
    def child(command, **kwargs):
        observed.append(kwargs)
        return NS(returncode=0)
    # The synthetic repository uses the real shared profile policy.
    import shutil
    shutil.copytree(Path(translate.__file__).parent / "profiles", tmp_path / "profiles")
    monkeypatch.setattr(translate.subprocess, "run", child)
    assert translate.main(["--novel", "fixture", "--repo-root", str(tmp_path),
        "--provider", "direct_deepseek", "--model", "pro"]) == 0
    assert observed[0]["env"][PROVIDER_ENV] == "direct_deepseek"
    assert observed[0]["env"]["NOVELTRANSLATOR_TRANSLATION_MODEL"] == "deepseek-v4-pro"
