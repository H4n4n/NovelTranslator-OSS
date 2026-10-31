"""Synthetic paid-pending incident; no real credentials/provider/evidence."""
import copy
import json
from pathlib import Path

import pytest

from test_production_goat_runtime import engine, zero_network, response, CLOCK, GOAT, fake_credentials
from production_paid_request_journal import (
    _sha256_json, _updated_record, request_identity, paid_request_journal_path, run_journaled_paid_request)
from production_provider_runtime import historical_capped_goat_provider_identity, goat_provenance
from production_goat_policy import bind_auxiliary_checkpoint
from production_goat_cap_recovery import accepted_envelope
from production_goat_runtime import raw_response_record
from benchmark_longitudinal_provider import build_provider_request_identity
from goat_state_policy_migration_plan import definitions, build_goat_state_policy_migration_plan as build_plan, project_snapshot
from goat_state_policy_migration_execute import execute_goat_state_policy_migration as execute
from production_goat_state_policy_migration import KIND, MARKER, text_sha, install_state_policy_migration
from translation_model_config import pending_translation_model_record
from noveltranslator_core import atomic_write_json

STATE = dict(new_terms=[], continuity_summary="A man entered.", issues=[])


def plan(project, **kwargs):
    return build_plan("fixture", repo_root=project.repository_root,
        max_api_calls=kwargs.get("max_api_calls", 6), max_credit_equivalent_usd=kwargs.get("max_credit_equivalent_usd", 1))


@pytest.fixture
def incident(engine):
    project, module, sends, *_ = engine
    old = historical_capped_goat_provider_identity()
    source = module.chapter_map[1]["text"]
    text = "A man entered the hall."
    # Recreate an exact old production-era accepted response and both terminal
    # state LENGTHs; synthetic construction is confined to tmp_path.
    md = project.pending_dir / "0001.md"
    md.write_text(text, encoding="utf-8")
    def record(kind, wire, raw, translation=""):
        expected = request_identity(operation_kind=kind, novel_id="fixture", chapter=1,
            provider=GOAT, model=old["wire_model"], profile_id=project.config.profile,
            source_sha256=text_sha(source), translation_sha256=text_sha(translation) if translation else "",
            request_payload={"wire_request": wire, "provider_policy": old})
        expected.update(provider_policy=old,
            wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
        return run_journaled_paid_request(journal_path=paid_request_journal_path(
            project.data_dir / "provider_request_journal", kind, 1), identity=expected,
            save_json=atomic_write_json, prepare_attempt=lambda: dict(request_started_utc=CLOCK().isoformat()),
            send_once=lambda: raw, normalize_response=lambda raw, context: accepted_envelope(
                dict(raw_response=raw_response_record(raw), **context), wire, provider_policy=old))
    translation_wire = dict(model=old["wire_model"], reasoning_effort="low", max_completion_tokens=50000,
        messages=[{"role": "user", "content": source}])
    paid = record("chapter_translation", translation_wire, response(content=text))
    meta = dict(chapter=1, stage="translated", translation_stats=paid["response"]["stats"],
        finish_reason="stop", local_qa={"commit_allowed": True, "issues": []},
        **pending_translation_model_record("v41-flash"), **goat_provenance(old, paid["response"]["request_sha256"]))
    meta.update(translation_max_tokens=50000, translation_thinking="low", translation_pricing_version=old["pricing_version"])
    defs = definitions(project)
    meta["state_checkpoint_identity"] = bind_auxiliary_checkpoint(defs.current_state_checkpoint_identity(
        1, source, text, prior_continuity=module.continuity), old)
    atomic_write_json(project.pending_dir / "0001.json", meta)
    wire = dict(model=old["wire_model"], reasoning_effort="low", max_completion_tokens=8000, messages=[
        {"role": "system", "content": defs.STATE_PROMPT},
        {"role": "user", "content": defs.state_extraction_user_prompt(chapter=1,
            existing_terms=[], prior=module.continuity, source=source, translation=text)}])
    for kind in ("state_extraction", "state_extraction_recovery"):
        record(kind, wire, response(content="", finish="length", completion=8000, reasoning=8000), text)
    from production_provider_runtime import install_goat_provenance
    from translation_model_config import resolve_translation_model
    import noveltranslator_core as core
    core.atomic_write_json = atomic_write_json
    install_goat_provenance(core, resolve_translation_model("v41-flash"), project.pending_dir, old)
    module.atomic_write_json = core.atomic_write_json
    assert not sends
    return engine


def test_readonly_ready_plan_binds_exact_old_evidence(incident):
    project = incident[0]
    before = project_snapshot(project)
    a = plan(project)
    b = plan(project)
    assert a == b and before == project_snapshot(project)
    assert a["status"] == a["send_authorization_status"] == "READY"
    assert a["api_calls"] == a["credential_reads"] == a["project_writes"] == 0
    assert a["translation_generation_allowed"] is a["startup_periodic_audits_allowed"] is False
    assert a["recovery_generation_send_limit"] == a["recovery_generation_sends_remaining"] == 1
    proof = a["migration_proof"]
    assert len(proof["old_state_journals"]) == 2
    for item in proof["old_state_journals"]:
        journal = paid_request_journal_path(project.data_dir / "provider_request_journal", item["identity"]["operation_kind"], 1)
        record = json.loads(journal.read_text())
        assert item["record_sha256"] == record["record_sha256"]
        assert item["response_sha256"] == record["response_sha256"]
    assert a["recovery_identity"]["operation_kind"] == KIND
    assert "max_tokens" not in a["recovery_wire"] and "max_completion_tokens" not in a["recovery_wire"]


def test_missing_hard_bound_refuses_before_activation(incident, monkeypatch):
    import production_goat_auxiliary_policy as policy
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", {})
    project = incident[0]
    before = project_snapshot(project)
    p = plan(project)
    assert p["status"] == "READY" and p["send_authorization_status"] == "HOLD_FOR_ARCHITECTURE_REVIEW"
    with pytest.raises(RuntimeError, match="HOLD_FOR_ARCHITECTURE_REVIEW"):
        execute("fixture", repo_root=project.repository_root, max_api_calls=6, max_credit_equivalent_usd=1,
            expected_state_fingerprint=p["state_fingerprint"], approval_token=p["approval_token"], authorize_paid_recovery=True)
    assert not incident[2]
    assert {k:v for k,v in project_snapshot(project).items() if k != ".production.lock"} == before


def test_locked_executor_real_lifecycle_with_only_synthetic_activation(incident, monkeypatch, tmp_path):
    import command_code_provider_transport as transport
    import run_production_engine as launcher
    from types import SimpleNamespace as NS
    project = incident[0]
    p = plan(project)
    events = []
    fake_credentials(monkeypatch, tmp_path, events)
    sent = []
    def send(**body):
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        sent.append(body)
        return response(content=json.dumps(STATE))
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: NS(
        chat=NS(completions=NS(create=send)), close=lambda: events.append("closed")))
    original = launcher.load_production_module
    def load(*a, **k):
        module = original(*a, **k)
        module.local_qa = lambda *a, **k: dict(commit_allowed=True, issues=[])
        module.run_periodic_audit = lambda *a, **k: pytest.fail("startup audit forbidden")
        return module
    monkeypatch.setattr(launcher, "load_production_module", load)
    report = execute("fixture", repo_root=project.repository_root, max_api_calls=6,
        max_credit_equivalent_usd=1, expected_state_fingerprint=p["state_fingerprint"],
        approval_token=p["approval_token"], authorize_paid_recovery=True)
    assert report["status"] == "COMMITTED", report
    assert report["api_calls"] == len(sent) == 1
    assert report["translation_generation_attempted"] is False
    assert events == ["fake_dpapi", "closed"]


def test_same_fixed_slot_replays_success_after_checkpoint_crash(incident):
    project, module, sends, client, budget, *_ = incident
    p = plan(project)
    # Keep the checkpoint translated to model a crash after durable response
    # acceptance but before the ordinary state checkpoint write.
    install_state_policy_migration(module, p)
    client.chat.completions.create = lambda **wire: (sends.append(wire) or response(content=json.dumps(STATE)))
    source = module.chapter_map[1]["text"]
    paid = (project.pending_dir / "0001.md").read_text()
    first = module.extract_state(1, source, paid, prior_continuity=module.continuity)
    resumed = plan(project)
    assert resumed["recovery_generation_sends_remaining"] == 0
    install_state_policy_migration(module, resumed)
    second = module.extract_state(1, source, paid, prior_continuity=module.continuity)
    assert second == first and len(sends) == budget.api_calls == 1


def test_scope_refuses_other_sends_before_started(incident):
    project, module, sends, *_ = incident
    p = plan(project)
    install_state_policy_migration(module, p)
    from test_production_goat_runtime import kwargs
    from production_goat_runtime import GoatRuntimeError
    with pytest.raises(GoatRuntimeError):
        module.journaled_chat_request("chapter_glossary_review", 1, "甲", kwargs(4000))
    record = json.loads(paid_request_journal_path(project.data_dir / "provider_request_journal", "chapter_glossary_review", 1).read_text())
    assert record["status"] == "planned" and not sends


def test_exact_metadata_and_runtime_drift_invalidates_authorization(incident):
    project = incident[0]
    p = plan(project)
    path = project.pending_dir / "0001.json"
    meta = json.loads(path.read_text())
    meta["operator_note"] = "changed after approval"
    atomic_write_json(path, meta)
    with pytest.raises(RuntimeError, match="authorization drift"):
        execute("fixture", repo_root=project.repository_root, max_api_calls=6,
            max_credit_equivalent_usd=1, expected_state_fingerprint=p["state_fingerprint"],
            approval_token=p["approval_token"], authorize_paid_recovery=True)


@pytest.mark.parametrize("mutation", ["source", "translation", "provenance", "checkpoint", "progress", "continuity", "response_identity", "started", "uncertain", "old_request", "policy"])
def test_drift_or_ambiguous_old_evidence_blocks(incident, monkeypatch, mutation):
    project, module, sends, *_ = incident
    p = plan(project)
    meta_path = project.pending_dir / "0001.json"
    if mutation == "source": atomic_write_json(project.chapters_file, [{"chapter":1,"text":"changed"},{"chapter":2,"text":"乙 followed."}])
    if mutation == "translation": meta_path.with_suffix(".md").write_text("changed")
    if mutation in {"provenance", "checkpoint"}:
        meta = json.loads(meta_path.read_text())
        if mutation == "provenance": meta["translation_request_sha256"] = "0"*64
        else: meta["state_checkpoint_identity"]["translation_sha256"] = "0"*64
        atomic_write_json(meta_path, meta)
    if mutation == "progress": atomic_write_json(project.progress_file, dict(completed=[1],last_completed=1,next_chapter=2))
    if mutation == "continuity": atomic_write_json(project.continuity_file, dict(latest_chapter=1,summary="drift"))
    if mutation in {"response_identity", "started", "uncertain", "old_request"}:
        path = paid_request_journal_path(project.data_dir / "provider_request_journal", "state_extraction", 1)
        record = json.loads(path.read_text())
        if mutation in {"started", "uncertain"}: record = _updated_record(record,status=mutation,response={},response_sha256="")
        elif mutation == "old_request": record = _updated_record(record, request_sha256="0"*64)
        else:
            envelope = copy.deepcopy(record["response"])
            envelope["provider_identity"]["reasoning_effort"] = "high"
            record = _updated_record(record,response=envelope,response_sha256=_sha256_json(envelope))
        atomic_write_json(path, record)
    if mutation == "policy":
        import goat_state_policy_migration_plan as planner
        old_identity = planner.provider_identity
        monkeypatch.setattr(planner, "provider_identity", lambda *a: {**old_identity(*a),"auxiliary_completion_policy":"capped"})
    with pytest.raises((RuntimeError, ValueError)):
        plan(project)
    assert not sends


@pytest.mark.parametrize("finish", ["stop", "length", "malformed", "ambiguous"])
def test_single_generation_preserves_translation_and_resumes_local_pipeline(incident, monkeypatch, finish):
    project, module, sends, client, budget, *_ = incident
    p = plan(project)
    paid = (project.pending_dir / "0001.md").read_bytes()
    old = {f.name:f.read_bytes() for f in (project.data_dir / "provider_request_journal").glob("*")}
    # Fixture engine has current wire policy; use an explicitly scoped old
    # provenance writer exactly as the dedicated executor preparation does.
    from production_provider_runtime import install_goat_provenance
    import noveltranslator_core as core
    from translation_model_config import resolve_translation_model
    core.atomic_write_json = atomic_write_json
    install_goat_provenance(core, resolve_translation_model("v41-flash"), project.pending_dir, p["migration_proof"]["old_policy"])
    module.atomic_write_json = core.atomic_write_json
    module.local_qa = lambda *a,**k: dict(commit_allowed=True, issues=[])
    def send(**wire):
        sends.append(wire)
        if finish == "ambiguous": raise TimeoutError("synthetic uncertain transport")
        return response(content="{}" if finish == "malformed" else json.dumps(STATE),
            finish="length" if finish == "length" else "stop")
    client.chat.completions.create = send
    module.translate_chapter = lambda *a,**k: pytest.fail("translation regeneration forbidden")
    module.run_periodic_audit = lambda *a,**k: pytest.fail("startup audits forbidden")
    install_state_policy_migration(module, p)
    success = module.process_chapter(1)
    assert success is (finish != "ambiguous")
    assert len(sends) == budget.api_calls == 1
    assert "max_tokens" not in sends[0] and "max_completion_tokens" not in sends[0]
    for name, data in old.items(): assert (project.data_dir / "provider_request_journal" / name).read_bytes() == data
    if finish == "stop":
        assert (project.chapter_dir / "0001.md").read_bytes() == paid
        assert module.progress["completed"] == [1]
        assert module.progress["total_completion_tokens"] == 16020
        assert module.continuity["latest_chapter"] == 1
        qa = json.loads((project.qa_dir / "0001.json").read_text())
        assert qa["translation_provider_identity"] == p["migration_proof"]["old_policy"]
    elif finish in {"length", "malformed"}:
        from review_queue import read_items
        assert (project.chapter_dir / "0001.md").read_bytes() == paid
        assert module.progress["completed"] == [1]
        assert [item["kind"] for item in read_items(project.data_dir)] == ["state_extraction"]
    else:
        assert (project.pending_dir / "0001.md").read_bytes() == paid
        assert module.progress["completed"] == []
        with pytest.raises((RuntimeError, ValueError)):
            plan(project)
        assert module.process_chapter(1) is False
        assert len(sends) == 1
