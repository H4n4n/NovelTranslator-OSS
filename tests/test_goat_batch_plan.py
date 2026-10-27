"""B1 contract proofs: disposable novels only; no real transport or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import controlled_trial_plan as trial
import goat_batch_plan as planner
from novel_project import load_novel_project
from production_provider_runtime import goat_provenance, provider_identity
from test_controlled_trial_plan import _empty_project, _write_json
from test_production_goat_runtime import zero_network, engine
from test_goat_periodic_audit_finalization import incident as audit_incident
from test_uncertain_request_disposition import incident as disposition_incident

pytestmark = pytest.mark.usefixtures("zero_network", "synthetic_goat_hard_bound")


def fixture_novel(tmp_path, *, next_chapter=1, final=60, audited=0,
                  language="zh", profile="historical_military", interval=25):
    project = _empty_project(tmp_path)
    config = project.config.to_dict()
    config.update(final_chapter=final, source_language=language, profile=profile, audit_interval=interval)
    _write_json(project.config_file, config)
    _write_json(project.chapters_file, [dict(chapter=n, production_index=n,
        text="甲 entered." if language == "zh" else "왕자가 들어왔다.") for n in range(1, final + 1)])
    _write_json(project.progress_file, dict(completed=list(range(1, next_chapter)),
        last_completed=next_chapter - 1, next_chapter=next_chapter, last_audited_chapter=audited))
    _write_json(project.continuity_file, dict(latest_chapter=next_chapter - 1, summary=""))
    identity = provider_identity("command_code_provider", "v41-flash")
    for n in range(1, next_chapter):
        (project.chapter_dir / f"{n:04d}.md").write_text("A man entered.", encoding="utf-8")
        _write_json(project.qa_dir / f"{n:04d}.json", dict(chapter=n, commit_allowed=True,
            translation_model="deepseek-flash", **goat_provenance(identity, "a" * 64)))
    return load_novel_project("fixture", repo_root=tmp_path)


def plan(project, **changes):
    args = dict(initial_chapter=json.loads(project.progress_file.read_text())["next_chapter"],
                chapter_count=5, max_api_calls=50, max_credit_equivalent_usd=25)
    args.update(changes)
    return planner.build_goat_batch_plan(project.config.id, repo_root=project.repository_root, **args)


def snapshot(project):
    return {p.relative_to(project.root).as_posix(): p.read_bytes()
            for p in project.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("language,profile", [("zh", "historical_military"), ("ko", "korean_fantasy")])
def test_deterministic_exact_range_and_profile(tmp_path, language, profile):
    project = fixture_novel(tmp_path, language=language, profile=profile)
    before = snapshot(project)
    first = plan(project)
    assert first == plan(project)
    assert snapshot(project) == before
    bound = first["authorization_binding"]
    assert (bound["initial_chapter"], bound["end_chapter"], bound["chapter_count"]) == (1, 5, 5)
    assert bound["authorized_next_chapter"] == 1
    assert (bound["source_language"], bound["profile"]) == (language, profile)
    assert bound["provider_identity"]["model_version"] == "DeepSeek-V4.1-Flash"
    assert bound["provider_identity"]["provider"] == "command_code_provider"
    assert first["provider_invoice_claimed"] is first["paid_execution_authorized"] is False
    assert first["api_calls"] == first["project_writes"] == 0
    assert first["state_fingerprint"] == planner._canonical_hash(bound)
    assert first["execution_blockers"]


@pytest.mark.parametrize("field", ["initial_chapter", "chapter_count", "max_api_calls"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "5", None, float("inf"), 10**1000])
def test_invalid_integer_parameters(tmp_path, field, value):
    project = fixture_novel(tmp_path)
    with pytest.raises(ValueError):
        plan(project, **{field: value})


@pytest.mark.parametrize("value", [0, -1, True, False, "5", None, float("inf"),
    float("-inf"), float("nan"), 10**1000, 1_000_001])
def test_invalid_credit_parameters(tmp_path, value):
    with pytest.raises(ValueError):
        plan(fixture_novel(tmp_path), max_credit_equivalent_usd=value)


@pytest.mark.parametrize("field,value", [("chapter_count", 1001), ("max_api_calls", 1_000_001),
    ("initial_chapter", 2), ("provider_selection", "direct_deepseek"), ("provider_selection", None),
    ("model_selection", "pro"), ("model_selection", None)])
def test_bounds_range_and_provider_rejection(tmp_path, field, value):
    with pytest.raises(ValueError):
        plan(fixture_novel(tmp_path), **{field: value})


def test_budget_values_not_rounded_or_renewed(tmp_path):
    project = fixture_novel(tmp_path)
    first = plan(project, max_credit_equivalent_usd=0.123456781)
    second = plan(project, max_credit_equivalent_usd=0.123456782)
    assert first["approval_token"] != second["approval_token"]
    assert plan(project, max_api_calls=51)["approval_token"] != plan(project)["approval_token"]


@pytest.mark.parametrize("next_chapter", [59, 61])
def test_insufficient_or_exhausted(tmp_path, next_chapter):
    with pytest.raises(ValueError, match="remaining|exhausted"):
        plan(fixture_novel(tmp_path, next_chapter=next_chapter))


@pytest.mark.parametrize("kind", ["duplicate", "missing", "out_of_order", "empty", "wrong_index"])
def test_bad_source_inventory(tmp_path, kind):
    project = fixture_novel(tmp_path)
    chapters = json.loads(project.chapters_file.read_text())
    if kind == "duplicate": chapters[1] = chapters[0]
    if kind == "missing": chapters.pop()
    if kind == "out_of_order": chapters.reverse()
    if kind == "empty": chapters[3]["text"] = ""
    if kind == "wrong_index": chapters[1]["production_index"] = 8
    _write_json(project.chapters_file, chapters)
    with pytest.raises(ValueError): plan(project)


@pytest.mark.parametrize("kind", ["completed", "last_completed", "next_chapter", "audit", "output", "duplicate_output"])
def test_bad_progress_or_output(tmp_path, kind):
    project = fixture_novel(tmp_path, next_chapter=3)
    progress = json.loads(project.progress_file.read_text())
    if kind == "completed": progress["completed"] = [1, 1]
    if kind == "last_completed": progress["last_completed"] = 1
    if kind == "next_chapter": progress["next_chapter"] = True
    if kind == "audit": progress["last_audited_chapter"] = 3
    if kind == "output": (project.chapter_dir / "0001.md").unlink()
    if kind == "duplicate_output": (project.chapter_dir / "1.md").write_text("duplicate")
    _write_json(project.progress_file, progress)
    with pytest.raises(ValueError): plan(project)


@pytest.mark.parametrize("which", ["source", "raw_source", "config", "continuity", "glossary", "qa", "prompt", "override"])
def test_input_drift_invalidates(tmp_path, which):
    project = fixture_novel(tmp_path, next_chapter=2)
    first = plan(project)
    if which == "source":
        chapters = json.loads(project.chapters_file.read_text()); chapters[4]["text"] += " changed"
        _write_json(project.chapters_file, chapters)
    elif which == "raw_source":
        project.source_dir.mkdir(exist_ok=True); project.source_epub.write_bytes(b"synthetic source")
    elif which == "config":
        config = project.config.to_dict(); config["title"] = "Changed"; _write_json(project.config_file, config)
    elif which == "continuity": _write_json(project.continuity_file, dict(latest_chapter=1, summary="Changed"))
    elif which == "glossary": _write_json(project.glossary_file, {"entries": {}, "revision": 1})
    elif which == "qa":
        path = project.qa_dir / "0001.json"; qa = json.loads(path.read_text()); qa["fixture_note"] = "Changed"; _write_json(path, qa)
    elif which == "prompt": project.translation_prompt_file.write_text("Changed", encoding="utf-8")
    elif which == "override": _write_json(project.source_overrides_file, {})
    assert first["state_fingerprint"] != plan(project)["state_fingerprint"]


@pytest.mark.parametrize("which", ["runtime", "profile"])
def test_runtime_and_profile_policy_drift(tmp_path, monkeypatch, which):
    project = fixture_novel(tmp_path)
    runtime = tmp_path / "engine"
    policy = runtime / "profiles" / project.config.profile
    policy.mkdir(parents=True)
    (runtime / "engine.py").write_text("# engine")
    (policy / "policy.txt").write_text("policy")
    monkeypatch.setattr(trial, "ENGINE_RUNTIME_ROOT", runtime)
    first = plan(project)
    target = runtime / "engine.py" if which == "runtime" else policy / "policy.txt"
    target.write_text("changed")
    assert first["approval_token"] != plan(project)["approval_token"]


def test_price_and_provider_policy_drift(tmp_path, monkeypatch):
    import benchmark_longitudinal_g7d_accounting_policy as pricing
    project = fixture_novel(tmp_path)
    first = plan(project)
    original = pricing.pricing_snapshot
    monkeypatch.setattr(pricing, "pricing_snapshot", lambda *a, **k: {
        **original(*a, **k), "synthetic_price_revision": "changed"})
    assert first["state_fingerprint"] != plan(project)["state_fingerprint"]
    original_identity = planner.provider_identity
    monkeypatch.setattr(planner, "provider_identity", lambda *a, **k: {
        **original_identity(*a, **k), "pricing_version": "changed"})
    with pytest.raises(ValueError, match="preflight"): plan(project)


@pytest.mark.parametrize("contents", ['{"entries":{},"entries":{}}', '{"entries":{},"x":NaN}',
    '{"entries":{},"x":1e999}', '{broken'])
def test_malformed_evidence(tmp_path, contents):
    project = fixture_novel(tmp_path)
    project.glossary_file.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError): plan(project)


def test_pending_and_accounting_and_provenance_reject(tmp_path):
    project = fixture_novel(tmp_path, next_chapter=2)
    (project.pending_dir / "0002.md").write_text("paid text")
    with pytest.raises(ValueError, match="pending"): plan(project)
    (project.pending_dir / "0002.md").unlink()
    progress = json.loads(project.progress_file.read_text()); progress["estimated_cost_usd"] = 1
    _write_json(project.progress_file, progress)
    with pytest.raises((ValueError, RuntimeError)): plan(project)
    progress.pop("estimated_cost_usd"); _write_json(project.progress_file, progress)
    qa = json.loads((project.qa_dir / "0001.json").read_text()); qa["translation_wire_model"] = "wrong"
    _write_json(project.qa_dir / "0001.json", qa)
    with pytest.raises((ValueError, RuntimeError)): plan(project)


def test_guhuo_shaped_preview_and_audit_crossing(tmp_path):
    project = fixture_novel(tmp_path, next_chapter=369, final=500, audited=350)
    assert plan(project)["authorization_binding"]["end_chapter"] == 373
    assert plan(project)["authorization_binding"]["audit_obligations"]["windows"] == []
    crossing = plan(project, chapter_count=10)["authorization_binding"]["audit_obligations"]
    assert crossing["windows"] == [dict(start_chapter=351, end_chapter=375,
        phase="after_commit", before_next_chapter=376)]


def test_configurable_startup_and_final_audit(tmp_path):
    project = fixture_novel(tmp_path, next_chapter=5, final=11, interval=3)
    audit = plan(project, chapter_count=7)["authorization_binding"]["audit_obligations"]
    assert audit["interval"] == 3
    assert [(w["start_chapter"], w["end_chapter"], w["phase"]) for w in audit["windows"]] == [
        (1, 3, "startup"), (4, 6, "after_commit"), (7, 9, "after_commit"), (10, 11, "final_partial")]
    assert audit["final_novel_audit_required"] and audit["publication_finalization_required"]
    assert audit["windows"][0]["before_next_chapter"] == 5


@pytest.mark.parametrize("status", ["planned", "started", "uncertain", "unknown", "response_recorded", "corrupt"])
def test_unresolved_invalid_or_future_journal_rejected(tmp_path, status):
    from production_paid_request_journal import request_identity, _new_planned_record, _updated_record, _sha256_json
    project = fixture_novel(tmp_path)
    identity = request_identity(operation_kind="chapter_translation", novel_id="fixture", chapter=1,
        provider="command_code_provider", model="deepseek/deepseek-v4.1-flash",
        profile_id="historical_military", source_sha256="a" * 64, request_payload={"synthetic": True})
    record = _new_planned_record(identity)
    fields = dict(status=status, attempt_context={"request_started_utc": "2026-10-15T00:00:00+00:00"})
    if status == "response_recorded": fields.update(response={"synthetic": True}, response_sha256=_sha256_json({"synthetic": True}))
    record = _updated_record(record, **fields)
    if status == "corrupt": record["record_sha256"] = "0" * 64
    _write_json(project.data_dir / "provider_request_journal/0001.chapter_translation.json", record)
    before = snapshot(project)
    with pytest.raises((ValueError, RuntimeError)): plan(project)
    assert snapshot(project) == before


def test_historical_disposition_uses_verified_inventory(disposition_incident, monkeypatch, tmp_path):
    from test_uncertain_request_disposition import activation, plan as recovery_plan, run, STATE, response
    from pending_recovery_plan import build_pending_recovery_plan
    from pending_recovery_execute import execute_pending_recovery
    project = disposition_incident[0]
    approved = recovery_plan(project)
    activation(monkeypatch, tmp_path, disposition_incident, [response(content=json.dumps(STATE))])
    assert run(project, approved)["status"] == "STATE_CHECKPOINT_SAVED"
    approved = build_pending_recovery_plan("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection="command_code_provider",
        max_api_calls=6, max_credit_equivalent_usd=2)
    assert execute_pending_recovery("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection="command_code_provider", max_api_calls=6,
        max_cost_usd=None, max_credit_equivalent_usd=2, expected_state_fingerprint=approved["state_fingerprint"],
        approval_token=approved["approval_token"], authorize_paid_recovery=True)["status"] == "COMMITTED"
    before = snapshot(project)
    assert plan(project, chapter_count=1)["authorization_binding"]["initial_chapter"] == 363
    assert snapshot(project) == before
    # Original uncertain bytes remain required; a missing original is not a whitelist match.
    original = project.data_dir / "provider_request_journal/0362.state_extraction.json"
    original.unlink()
    with pytest.raises((ValueError, RuntimeError)): plan(project, chapter_count=1)


@pytest.mark.parametrize("audit_incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("legacy", [False, True])
def test_great_king_historical_audit_compatibility(audit_incident, legacy):
    import production_periodic_audit_finalization as final
    from test_goat_periodic_audit_finalization import execute, local_module
    project = audit_incident[0]
    with pytest.raises((ValueError, RuntimeError)): plan(project)
    assert execute(project)["status"] == "FINALIZED"
    if legacy:
        path = next(project.audit_dir.glob("*.finalization.json"))
        intent = json.loads(path.read_bytes())
        intent.pop("historical_proof")
        intent["chapters"] = final.chapter_manifest(local_module(project))
        intent["intent_sha256"] = final.canonical_sha256({k: v for k, v in intent.items() if k != "intent_sha256"})
        _write_json(path, intent)
    before = snapshot(project)
    assert plan(project)["authorization_binding"]["initial_chapter"] == 401
    assert snapshot(project) == before
    # A self-consistent rehash cannot attach a foreign plan to a proven audit.
    path = next((project.retroactive_dir / "transactions").glob("*.authorization.json"))
    original = path.read_bytes()
    forged = json.loads(original)
    forged["plan_sha256"] = "f" * 64
    forged["authorization_sha256"] = planner._canonical_hash(
        {k: v for k, v in forged.items() if k != "authorization_sha256"})
    _write_json(path, forged)
    damaged = snapshot(project)
    with pytest.raises((ValueError, RuntimeError), match="authorization"):
        plan(project)
    assert snapshot(project) == damaged
    path.write_bytes(original)
    # Broken immutable BEFORE evidence must still block.
    next((project.backup_dir).glob("retroactive_*/chapters_before/0388.md")).write_bytes(b"changed")
    with pytest.raises((ValueError, RuntimeError)): plan(project)


def test_orphan_historical_transaction(tmp_path):
    project = fixture_novel(tmp_path)
    _write_json(project.retroactive_dir / "transactions/orphan.state.json",
                dict(transaction_id="unknown", state="APPLIED"))
    with pytest.raises(ValueError, match="orphan"): plan(project)


def test_concurrent_change_fails_closed(tmp_path, monkeypatch):
    project = fixture_novel(tmp_path)
    original = planner.run_production_path_preflight
    def changing(*args, **kwargs):
        report = original(*args, **kwargs)
        project.translation_addendum_file.write_text("changed during planning", encoding="utf-8")
        return report
    monkeypatch.setattr(planner, "run_production_path_preflight", changing)
    with pytest.raises(ValueError, match="changed during planning"): plan(project)


def test_config_changed_between_load_and_snapshot(tmp_path, monkeypatch):
    project = fixture_novel(tmp_path)
    original = planner.load_novel_project
    def changing(*args, **kwargs):
        loaded = original(*args, **kwargs)
        config = loaded.config.to_dict(); config["title"] += " changed"
        _write_json(loaded.config_file, config)
        return loaded
    monkeypatch.setattr(planner, "load_novel_project", changing)
    with pytest.raises(ValueError, match="configuration changed"): plan(project)


@pytest.mark.parametrize("action", ["import", "help", "json", "human", "bad"])
def test_fresh_process_import_help_and_planning_no_io(tmp_path, synthetic_goat_hard_bound, action):
    from dataclasses import asdict
    project = fixture_novel(tmp_path)
    before = snapshot(project)
    # The audit hook precedes imports: all writes, sockets and real secret access
    # are forbidden even if a future transitive import starts doing them.
    script = '''
import sys, os, json
def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        raise RuntimeError("B1 forbidden IO: " + event)
    if event == "ctypes.dlsym" and str(args[1]) in {"CryptProtectData", "CryptUnprotectData"}:
        raise RuntimeError("B1 DPAPI forbidden")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0]).lower()
        if path.endswith((".env", ".dpapi")):
            raise RuntimeError("B1 secret access forbidden")
        if isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise RuntimeError("B1 write forbidden")
sys.addaudithook(guard)
import goat_batch_plan as planner
import production_goat_auxiliary_policy as policy
from types import MappingProxyType
policy.REVIEWED_HARD_BOUNDS = MappingProxyType({("command_code_provider", "deepseek/deepseek-v4.1-flash"):
    policy.ReviewedCapability(**CAPABILITY)})
if ACTION == "help":
    planner.main(["--help"])
elif ACTION != "import":
    args = ["--novel", "fixture", "--repo-root", ROOT, "--initial-chapter", "1", "--chapter-count", "5",
            "--max-api-calls", "50", "--max-credit-equivalent-usd", "25"]
    if ACTION == "json": args += ["--json"]
    if ACTION == "bad": args[-1] = "nan"
    result = planner.main(args)
    assert result == (2 if ACTION == "bad" else 0)
assert "production_goat_execution" not in sys.modules
assert "controlled_trial_execute" not in sys.modules
'''
    script = ("CAPABILITY=" + repr(asdict(synthetic_goat_hard_bound)) + "\nACTION=" + repr(action)
              + "\nROOT=" + repr(str(project.repository_root)) + "\n" + script)
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(planner.__file__).parent,
                            capture_output=True, text=True, check=False,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr
    assert snapshot(project) == before
    if action == "json":
        assert json.loads(result.stdout)["project_writes"] == 0
    if action == "bad": assert json.loads(result.stdout)["status"] == "BLOCKED"
    if action == "human": assert "physical sends" in result.stdout and '"provider_invoice_claimed": false' in result.stdout
