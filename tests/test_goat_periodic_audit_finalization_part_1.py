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


def test_direct_generic_aggregate_unchanged():
    run = dict(stats=dict(prompt_tokens=10, cost_usd=0.01), checkpoint=dict(attempt_records=[]))
    assert final.authoritative_audit_stats(run, lambda *a: pytest.fail("direct combiner altered"), provider="direct_deepseek") is run["stats"]


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
def test_fresh_audit_mixed_old_and_preferred_baseline_finalizes_once(incident):
    project, module, result, stats, chunking = incident
    preferred = (project.chapter_dir / "0002.md").read_bytes()
    assert execute(project)["status"] == "FINALIZED"
    assert (project.chapter_dir / "0002.md").read_bytes() == preferred
    assert b"Mount Yourong" in (project.chapter_dir / "0388.md").read_bytes()
    intent = final.load_intent(local_module(project), 376, 400)
    item = intent["transaction_plan"]["chapters"][1]
    assert item["before_sha256"] == item["after_sha256"] == final.tx.digest(preferred)
    before = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert before == snapshot(project)
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["plan", "state", "result", "backup", "reconcile"])
@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_missing_transaction_evidence_after_edit_cannot_be_reclassified_as_fresh(incident, damage, corrupt, published, monkeypatch):
    project, module, result, stats, chunking = incident
    if published:
        execute(project)
    else:
        original = final.tx.atomic_write_json
        def stop(path, data):
            original(path, data)
            if Path(path).name.endswith(".state.json") and data.get("state") == "FINALIZED":
                raise KeyboardInterrupt("before outer publication")
        with monkeypatch.context() as mp:
            mp.setattr(final.tx, "atomic_write_json", stop)
            with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    if damage == "backup":
        path = next(project.backup_dir.rglob("0388.md"))
    else:
        path = next((project.retroactive_dir / "transactions").glob(f"*.{damage}.*json" if damage == "reconcile" else f"*.{damage}.json"))
    if corrupt: path.write_bytes(b"{corrupt")
    else: path.unlink()
    before = snapshot(project)
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        plan(project)
    from production_uncertain_request_disposition import effective_paid_request_inventory
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        effective_paid_request_inventory(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed_multi"], indirect=True)
def test_out_of_order_chapter_after_prefix_blocks_local_reconciliation(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    original = final.tx.atomic_write_bytes
    def stop(path, value):
        original(path, value)
        if Path(path).parent == project.chapter_dir:
            raise KeyboardInterrupt("after first historical edit")
    with monkeypatch.context() as mp:
        mp.setattr(final.tx, "atomic_write_bytes", stop)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    intent = final.load_intent(module,376,400)
    items = [i for i in intent["transaction_plan"]["chapters"] if i["edits"]]
    assert [i["chapter"] for i in items] == [1,388]
    backup = project.backup_dir / f"retroactive_{intent['transaction_plan']['transaction_id']}" / "chapters_before"
    (project.chapter_dir / "0001.md").write_bytes((backup / "0001.md").read_bytes())
    raw = (project.chapter_dir / "0388.md").read_bytes()
    (backup / "0388.md").write_bytes(raw)
    (project.chapter_dir / "0388.md").write_bytes(final.tx.transform_bytes(raw,items[1]["edits"]))
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="prefix"):
        plan(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed", "mixed_competing", "mixed_contextual"], indirect=True)
def test_aut2_final_whole_history_and_publication_stale_are_independent_of_tail(incident):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    execute(project)
    current = local_module(project)
    # Writer-safe setup supplies a genuine 1-375 prefix. First prove it, then
    # remove its intent to construct the legacy missing-evidence negative case.
    before = snapshot(project)
    complete = history.final_consistency_report(current)
    assert complete["status"] == "CHECKED_WITH_DEFERRALS" and complete["examined_chapters"] == 400
    assert snapshot(project) == before
    prior_path = final.intent_path(current, 1, 375)
    prior_bytes = prior_path.read_bytes()
    prior_path.unlink()
    before = snapshot(project)
    with pytest.raises(Exception, match="MISSING_OR_COMPETING_AUDIT_HISTORY"):
        history.final_consistency_report(current)
    with pytest.raises(Exception, match="ORPHAN_FRESHNESS_MANIFEST"):
        history.publication_freshness(project)
    assert snapshot(project) == before
    # Restore only this fixture's exact bytes before the independent stale check.
    prior_path.write_bytes(prior_bytes)
    freshness = history.publication_freshness(project)
    assert freshness["status"] == "STALE"
    assert 388 in freshness["impacted_chapters"] or freshness["impacted_chapters"] == []
    assert freshness["rebuilt_epubs"] == 0
    with pytest.raises(Exception, match="PUBLICATION_STALE"):
        history.require_publication_freshness(project)
    intent = final.load_intent(current, 376, 400)
    report = intent["record"]["historical_reconciliation"]
    assert report["scope"] == "whole_committed_history"
    assert report["examined_chapters"] == 400
    assert "有榮山" not in json.dumps(report, ensure_ascii=False)
