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


@pytest.mark.parametrize("fault", ["mixed", "metadata", "numeric", "usage", "duplicate"])
def test_authoritative_accounting_rejects_component_or_numeric_drift(incident, fault):
    project, module, result, stats, chunking = incident
    cp = json.loads(module.periodic_audit_checkpoint_path(376, 400).read_bytes())
    generic = {k: stats[k] for k in (*final.STAT_TOTAL_FIELDS, "api_request_count")}
    if fault == "mixed": cp["attempt_records"][0]["stats"]["provider"] = "direct_deepseek"
    if fault == "metadata": cp["attempt_records"][0]["stats"].pop("pricing_unit")
    if fault == "numeric": generic["prompt_tokens"] += 1
    if fault == "usage": cp["attempt_records"][0]["stats"].pop("completion_tokens")
    if fault == "duplicate": cp["attempt_records"][1]["operation_id"] = cp["attempt_records"][0]["operation_id"]
    with pytest.raises((RuntimeError, KeyError, ProductionProviderError)):
        final.authoritative_audit_stats(dict(checkpoint=cp, stats=generic), module.combine_stats, provider="command_code_provider")


def test_accounting_failure_before_any_prose_transaction(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    # Restore a separate disposable original fixture; never touch live evidence.
    isolated = project.root / "no_transaction"
    isolated.mkdir()
    monkeypatch.setattr(module, "RETROACTIVE_DIR", isolated)
    bad = copy.deepcopy(stats)
    bad["provider"] = "direct_deepseek"
    before = snapshot(project)
    monkeypatch.setattr(final.tx, "execute_retroactive_transaction", lambda **k: pytest.fail("transaction before accounting validation"))
    with pytest.raises((RuntimeError, ProductionProviderError)):
        locked_finalize(module, 376, 400, result, bad, chunking)
    assert snapshot(project) == before


def test_zero_candidate_goat_audit_no_fabricated_paid_accounting(incident):
    project, module, result, stats, chunking = incident
    locked_finalize(module, 376, 400, result, stats, chunking)
    module.progress.update(last_completed=425, next_chapter=426, completed=list(range(1, 426)))
    for n in range(401, 426): (project.chapter_dir / f"{n:04d}.md").write_bytes(b"English.")
    _write_json(project.progress_file, module.progress)
    _write_json(project.continuity_file, dict(latest_chapter=425, summary="fixture"))
    zero_result, zero_stats, zero_chunking = module.call_periodic_audit(401, 425)
    before = copy.deepcopy(module.progress)
    locked_finalize(module, 401, 425, zero_result, zero_stats, zero_chunking)
    assert module.progress == {**before, "last_audited_chapter": 425}
    assert "provider" not in zero_stats and "accounting_components" not in zero_stats
    assert locked_finalize(module, 401, 425, zero_result, zero_stats, zero_chunking)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("incident", ["mixed_multi"], indirect=True)
@pytest.mark.parametrize("boundary", ["intent", "plan", "backup", "chapter", "APPLIED", "result", "FINALIZED", "audit", "progress"])
def test_fresh_baseline_crash_prefixes_preserve_manifest_and_account_once(incident, monkeypatch, boundary):
    project, module, result, stats, chunking = incident
    original_json, original_bytes = final.atomic_write_json, final.atomic_write_bytes
    preferred = (project.chapter_dir / "0002.md").read_bytes()
    fired = []
    def crash_json(path, value):
        original_json(path, value)
        path = Path(path)
        match = ((boundary == "intent" and path.name.endswith(".finalization.json")) or
            (boundary == "plan" and path.name.endswith(".plan.json")) or
            (boundary in {"APPLIED", "FINALIZED"} and path.name.endswith(".state.json") and value.get("state") == boundary) or
            (boundary == "result" and path.name.endswith(".result.json")) or
            (boundary == "audit" and path.name == "audit_0376_0400.json") or
            (boundary == "progress" and path == project.progress_file))
        if match and not fired:
            fired.append(True); raise KeyboardInterrupt("durable fresh audit boundary")
    def crash_bytes(path, value):
        original_bytes(path, value)
        path = Path(path)
        if not fired and ((boundary == "backup" and path.parent.name == "chapters_before") or
                          (boundary == "chapter" and path.parent == project.chapter_dir)):
            fired.append(True); raise KeyboardInterrupt("durable fresh audit bytes")
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_bytes", crash_bytes)
        mp.setattr(module, "save_json", crash_json)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    assert fired
    from production_uncertain_request_disposition import effective_paid_request_inventory
    if boundary != "progress":
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project)
    approved = plan(project)
    assert execute(project, approved)["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
    assert (project.chapter_dir / "0002.md").read_bytes() == preferred
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    before = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert before == snapshot(project)


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("stage", ["invalidation", "first_chapter", "glossary", "progress"])
def test_aut2_crash_invalidation_precedes_edits_and_progress_last(incident, monkeypatch, stage):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    original_json, original_prose, original_save = history.atomic_write_json, final.tx.atomic_write_bytes, module.save_json
    fired = []
    def crash(path, value):
        original_json(path, value)
        if stage == "invalidation" and "publication_freshness" in str(path) and not fired:
            fired.append(True)
            raise KeyboardInterrupt()
    def prose(path, raw):
        original_prose(path, raw)
        if stage == "first_chapter" and Path(path).parent == module.OUTPUT_DIR and not fired:
            fired.append(True)
            assert list((project.root / "output/publication_freshness").glob("*.json"))
            raise KeyboardInterrupt()
    def save(path, value):
        original_save(path, value)
        if ((stage == "glossary" and path == module.GLOSSARY_FILE)
                or (stage == "progress" and path == module.PROGRESS_FILE)) and not fired:
            fired.append(True)
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(history, "atomic_write_json", crash)
        mp.setattr(final.tx, "atomic_write_bytes", prose)
        mp.setattr(module, "save_json", save)
        with pytest.raises(KeyboardInterrupt):
            locked_finalize(module, 376, 400, result, stats, chunking)
    journals = {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    reloaded = local_module(project)
    locked_finalize(reloaded, 376, 400, result, stats, chunking)
    assert journals == {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    assert history.publication_freshness(project)["status"] == "STALE"
    before = snapshot(project)
    locked_finalize(local_module(project), 376, 400, result, stats, chunking)
    assert snapshot(project) == before
