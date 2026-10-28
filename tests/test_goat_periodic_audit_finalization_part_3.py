"""Synthetic 376–400 incident, exact paid replay and publication crash proofs."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS, FunctionType

import pytest

import noveltranslator_core as core
import production_periodic_audit_finalization as final
from production_accounting import migrate_direct_progress, GOAT_METADATA, combine_goat_stats
from production_goat_runtime import install_goat_runtime, GoatBudget
from production_provider_runtime import provider_identity
from production_paid_request_journal import _with_record_hash
from production_provider_errors import ProductionProviderError
from goat_periodic_audit_finalize_plan import build_goat_periodic_audit_finalize_plan, local_module
from goat_periodic_audit_finalize_execute import execute_goat_periodic_audit_finalize
from novel_project import load_novel_project
from test_controlled_trial_plan import _empty_project, _write_json
from run_production_engine import load_production_module
from chapter_library import index_chapter_library
from translation_profile import load_translation_profile, load_novel_terminology_policy, profile_audit_instructions
from pathlib import Path

CLOCK = lambda: datetime(2026, 10, 12, 11, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


from goat_audit_finalization_support import locked_finalize, snapshot, incident, plan, execute


def test_live_shaped_local_recovery_and_already_finalized(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    before = snapshot(project)
    approved = plan(project)
    assert snapshot(project) == before
    assert approved["status"] == "READY" and approved["provider_calls_required"] == 0
    assert all(approved[k] is False for k in ("translation_generation_allowed", "chapter_generation_allowed", "audit_provider_generation_allowed"))
    assert stats["provider"] == "command_code_provider" and stats["provider_invoice_claimed"] is False
    assert len(stats["accounting_components"]) == 3
    # Demonstrate original failure without weakening its guard.
    generic = {k: stats[k] for k in (*final.STAT_TOTAL_FIELDS, "api_request_count")}
    with pytest.raises(ProductionProviderError, match="cross-provider"):
        core.add_accounting_totals_once(copy.deepcopy(module.progress), "reproduce-original", generic)
    before_prose = (project.chapter_dir / "0388.md").read_bytes()
    before_tx = {k: v for k, v in before.items() if "transactions/" in k.replace("\\", "/")}
    assert execute(project, approved)["status"] == "FINALIZED"
    assert (project.chapter_dir / "0388.md").read_bytes() == before_prose
    progress = json.loads(project.progress_file.read_bytes())
    assert (progress["last_completed"], progress["next_chapter"], progress["last_audited_chapter"]) == (400, 401, 400)
    assert progress["rate_correct_cost_usd"] == stats["cost_usd"]
    assert len(progress["accounted_api_operation_ids"]) == 1
    glossary = json.loads(project.glossary_file.read_bytes())
    assert glossary["entries"]["有榮山"]["english"] == "Mount Yourong"
    assert glossary["entries"]["有熙氏"] == module.glossary["entries"]["有熙氏"]
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    for key, payload in before_tx.items():
        assert (project.root / key).read_bytes() == payload
    after = snapshot(project)
    assert plan(project)["status"] == "ALREADY_FINALIZED"
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert snapshot(project) == after


@pytest.mark.parametrize("boundary", ["glossary", "issues", "history", "audit", "backup", "progress"])
def test_every_local_publication_prefix_reconciles_without_duplicates(incident, monkeypatch, boundary):
    project, module, result, stats, chunking = incident
    original = core.atomic_write_json
    fired = []
    def crash(path, value):
        original(path, value)
        path = Path(path)
        match = ((boundary == "glossary" and path == project.glossary_file)
            or (boundary == "issues" and path == project.issues_file)
            or (boundary == "history" and path == project.audit_history_file)
            or (boundary == "audit" and path.name == "audit_0376_0400.json")
            or (boundary == "backup" and path.parent.name == "checkpoint_0400")
            or (boundary == "progress" and path == project.progress_file))
        if match and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic crash")
    monkeypatch.setattr(module, "save_json", crash)
    original_bytes = core.atomic_write_bytes
    def crash_backup(path, value):
        original_bytes(path, value)
        if boundary == "backup" and path.parent.name == "checkpoint_0400" and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic backup crash")
    monkeypatch.setattr(final, "atomic_write_bytes", crash_backup)
    with pytest.raises(KeyboardInterrupt):
        locked_finalize(module, 376, 400, result, stats, chunking)
    monkeypatch.setattr(module, "save_json", original)
    monkeypatch.setattr(final, "atomic_write_bytes", original_bytes)
    outcome = locked_finalize(module, 376, 400, result, stats, chunking)
    assert outcome["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("fault", ["missing_tx", "chapter", "backup", "result", "state", "extra_prose", "glossary", "checkpoint",
    "candidate", "journal", "started", "uncertain", "corrupt", "window", "progress", "continuity", "policy"])
def test_recovery_drift_fails_closed(incident, fault):
    project, module, result, stats, chunking = incident
    approved = plan(project)
    directory = project.retroactive_dir / "transactions"
    cp_path = module.periodic_audit_checkpoint_path(376, 400)
    journal_path = next((project.data_dir / "provider_request_journal").glob("*.json"))
    def change(path, key, value):
        data = json.loads(path.read_bytes()); data[key] = value; _write_json(path, data)
    if fault == "missing_tx":
        for p in directory.glob("*.json"): p.unlink()
    if fault == "chapter": (project.chapter_dir / "0388.md").write_bytes(b"wrong")
    if fault == "backup": next(project.backup_dir.rglob("0388.md")).write_bytes(b"wrong")
    if fault == "result": change(next(directory.glob("*.result.json")), "replacement_count", 99)
    if fault == "state": change(next(directory.glob("*.state.json")), "state", "BLOCKED")
    if fault == "extra_prose": (project.chapter_dir / "0387.md").write_bytes(b"extra")
    if fault == "glossary":
        data = json.loads(project.glossary_file.read_bytes()); data["entries"]["有榮山"]["english"] = "Third Value"; _write_json(project.glossary_file, data)
    if fault == "checkpoint": change(cp_path, "status", "in_progress")
    if fault == "candidate": change(cp_path, "candidate_set_hash", "a"*64)
    if fault in {"journal", "started", "uncertain", "policy"}:
        journal = json.loads(journal_path.read_bytes())
        if fault == "journal": journal["response"]["stats"]["prompt_tokens"] += 1
        if fault in {"started", "uncertain"}: journal.update(status=fault, response={}, response_sha256="")
        if fault == "policy": journal["provider_policy"]["revision"] = "wrong"
        _write_json(journal_path, _with_record_hash(journal))
    if fault == "corrupt": journal_path.write_bytes(b"{")
    if fault == "window": change(cp_path, "audit_window", dict(start_chapter=377, end_chapter=400))
    if fault == "progress": change(project.progress_file, "next_chapter", 402)
    if fault == "continuity": change(project.continuity_file, "latest_chapter", 399)
    before = snapshot(project)
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        execute(project, approved)
    # The existing empty rendezvous lock is the only executor side effect.
    after = snapshot(project)
    assert {k: v for k, v in after.items() if k != ".production.lock"} == {
        k: v for k, v in before.items() if k != ".production.lock"}


@pytest.mark.parametrize("missing_plan", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_legacy_intent_preserves_completion_but_cannot_regenerate_plan(incident, missing_plan, published, monkeypatch):
    project, module, result, stats, chunking = incident
    if published:
        execute(project)
    else:
        original = module.save_json
        def stop(path, data):
            original(path,data)
            if Path(path) == project.glossary_file: raise KeyboardInterrupt("legacy outer tail")
        with monkeypatch.context() as mp:
            mp.setattr(module,"save_json",stop)
            with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    path = next(project.audit_dir.glob("*.finalization.json"))
    value = json.loads(path.read_bytes())
    value.pop("historical_proof")
    # PR158 captured current AFTER hashes in its original chapters field.
    value["chapters"] = final.chapter_manifest(local_module(project))
    value["intent_sha256"] = final.canonical_sha256({k:v for k,v in value.items() if k != "intent_sha256"})
    _write_json(path,value)
    if missing_plan: next((project.retroactive_dir / "transactions").glob("*.plan.json")).unlink()
    before = snapshot(project)
    if missing_plan:
        with pytest.raises(RuntimeError): plan(project)
    else:
        assert plan(project)["status"] == ("ALREADY_FINALIZED" if published else "READY")
    from production_uncertain_request_disposition import effective_paid_request_inventory
    if missing_plan or not published:
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project)
    else:
        assert not effective_paid_request_inventory(project)["uncertain"]
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
def test_intent_before_plan_with_orphan_checkpoint_is_not_untouched_prefix(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    original = final.atomic_write_json
    def stop(path, data):
        original(path,data)
        if Path(path).name.endswith(".finalization.json"):
            raise KeyboardInterrupt("intent before plan")
    with monkeypatch.context() as mp:
        mp.setattr(final,"atomic_write_json",stop)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    intent = final.load_intent(module,376,400)
    checkpoint = project.backup_dir / "checkpoint_0400"
    checkpoint.mkdir(parents=True)
    _write_json(checkpoint / "progress.json",intent["post"]["progress"])
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="checkpoint"):
        plan(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["checkpoint", "paid_journal"])
def test_aut2_review_paid_proof_failure_precedes_first_receipt(incident, monkeypatch, damage):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    write = final.atomic_write_json
    def stop(path, value):
        write(path, value)
        if Path(path).name.endswith(".finalization.json"):
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", stop)
        with pytest.raises(KeyboardInterrupt):
            locked_finalize(module, 376, 400, result, stats, chunking)
    if damage == "checkpoint":
        module.periodic_audit_checkpoint_path(376, 400).write_bytes(b"stale checkpoint")
    else:
        path = next(module.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))
        value = json.loads(path.read_bytes())
        value["response"]["stats"]["prompt_tokens"] += 1
        _write_json(path, _with_record_hash(value))
    monkeypatch.setattr(module.client.chat.completions, "create", lambda **k: pytest.fail("provider send"))
    before = snapshot(project)
    directories = {p.relative_to(project.root).as_posix() for p in project.root.rglob("*") if p.is_dir()}
    with pytest.raises((RuntimeError, ValueError)):
        locked_finalize(module, 376, 400, result, stats, chunking)
    assert snapshot(project) == before
    assert {p.relative_to(project.root).as_posix() for p in project.root.rglob("*") if p.is_dir()} == directories
    intent = final.load_intent(module, 376, 400)
    assert not history.invalidation_path(module, intent).exists()
