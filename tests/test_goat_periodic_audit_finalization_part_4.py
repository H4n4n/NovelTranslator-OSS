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


def test_local_executor_requires_explicit_flag_and_installs_tripwire(incident):
    project, *_ = incident
    approved = plan(project)
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="authorize-local-finalization"):
        execute_goat_periodic_audit_finalize("fixture", repo_root=project.repository_root, start_chapter=376, end_chapter=400,
            expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"])
    assert snapshot(project) == before
    from goat_periodic_audit_finalize_execute import no_transport
    import socket
    import command_code_provider_credentials as credentials
    import dotenv
    with no_transport():
        for call in (lambda: socket.create_connection(("example.invalid", 443)), credentials.load_platform_dpapi, dotenv.load_dotenv):
            with pytest.raises(RuntimeError, match="tripwire"): call()


@pytest.mark.parametrize("stage", ["after_plan", "after_backup", "after_chapter", "after_applied", "after_result"])
def test_shared_finalizer_reconciles_interrupted_new_pr155_transaction(incident, monkeypatch, stage):
    project, module, result, stats, chunking = incident
    original_bytes = next(project.backup_dir.rglob("0388.md")).read_bytes()
    (project.chapter_dir / "0388.md").write_bytes(original_bytes)
    from retroactive_span_planning import canonical_hash
    slot = canonical_hash(dict(novel_id=project.config.id, audit_label="0376_0400", through_chapter=400))
    for path in (project.retroactive_dir / "transactions").glob(f"{slot}.*.json"): path.unlink()
    # This starts an independent synthetic new transaction from exact BEFORE.
    original_json, original_write = final.tx.atomic_write_json, final.tx.atomic_write_bytes
    def json_crash(path, data):
        original_json(path, data)
        if ((stage == "after_plan" and path.name.endswith(".plan.json")) or
            (stage == "after_applied" and data.get("state") == "APPLIED") or
            (stage == "after_result" and path.name.endswith(".result.json"))):
            raise KeyboardInterrupt("synthetic transaction crash")
    def byte_crash(path, data):
        original_write(path, data)
        if ((stage == "after_backup" and path.parent != project.chapter_dir) or
            (stage == "after_chapter" and path.parent == project.chapter_dir)):
            raise KeyboardInterrupt("synthetic transaction crash")
    # Remove the old test backup so AFTER_BACKUP exercises a real new backup.
    for path in project.backup_dir.rglob("0388.md"): path.unlink()
    monkeypatch.setattr(final.tx, "atomic_write_json", json_crash)
    monkeypatch.setattr(final.tx, "atomic_write_bytes", byte_crash)
    with pytest.raises(KeyboardInterrupt):
        locked_finalize(module, 376, 400, result, stats, chunking)
    monkeypatch.setattr(final.tx, "atomic_write_json", original_json)
    monkeypatch.setattr(final.tx, "atomic_write_bytes", original_write)
    assert plan(project)["status"] == "READY"
    execute(project)
    assert plan(project)["status"] == "ALREADY_FINALIZED"
    assert (project.chapter_dir / "0388.md").read_bytes().count(b"Mount Yourong") == 1
    assert json.loads(project.progress_file.read_bytes())["rate_correct_cost_usd"] == stats["cost_usd"]


@pytest.mark.parametrize("incident", ["json_repair", "semantic_correction", "split"], indirect=True)
def test_recovery_reuses_generic_repair_and_split_semantics(incident):
    project, module, result, stats, chunking = incident
    approved = plan(project)
    assert approved["provider_calls_required"] == 0
    assert len(approved["state_binding"]["proof"]["journals"]) == chunking["api_request_count"]
    assert chunking["api_request_count"] in {4, 5}
    assert execute(project, approved)["status"] == "FINALIZED"
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("incident", ["relative"], indirect=True)
def test_relative_production_evidence_paths_remain_exact_and_replayable(incident):
    project, module, result, stats, chunking = incident
    before = snapshot(project)
    approved = plan(project)
    assert snapshot(project) == before
    assert execute(project, approved)["provider_calls"] == 0
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("incident,changed", [("mixed_multi", 2), ("mixed_case", 1),
    ("mixed_nested", 1), ("mixed_competing", 0), ("mixed_contextual", 0), ("mixed_noop", 0)], indirect=["incident"])
def test_fresh_baseline_ownership_markdown_case_and_noops(incident, changed):
    project, *_ = incident
    before = {p.name: p.read_bytes() for p in project.chapter_dir.glob("*.md")}
    execute(project)
    after = {p.name: p.read_bytes() for p in project.chapter_dir.glob("*.md")}
    assert sum(before[n] != after[n] for n in before) == changed
    assert before["0002.md"] == after["0002.md"]
    if changed == 2:
        assert after["0001.md"] == before["0001.md"].replace(b"Mount Rong", b"Mount Yourong")
    before_replay = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert snapshot(project) == before_replay


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["malformed", "missing", "tampered", "foreign"])
def test_aut2_invalid_freshness_never_certifies_editorial_copy(incident, damage):
    import historical_reconciliation as history
    import build_publication_epub as publication
    project, module, result, stats, chunking = incident
    execute(project)
    path = next((project.root / "output/publication_freshness").glob("*.json"))
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_bytes(b"{bad")
    else:
        value = json.loads(path.read_bytes())
        value["status" if damage == "tampered" else "novel_id"] = "CURRENT" if damage == "tampered" else "foreign"
        path.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(project)
    with pytest.raises(Exception, match="AUT2"):
        publication.build_publication_epub(project)
    assert snapshot(project) == before
