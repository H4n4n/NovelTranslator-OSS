"""Real source-owned startup witnesses in read-only batch planning; no provider."""
import copy
import json
import pytest

from goat_periodic_audit_finalize_plan import local_module
from noveltranslator_core import atomic_write_json
from production_run_lock import production_run_lock
from production_standalone_lineage import witnesses, verified_events, slot_for, bound_authorization
from production_accounting import canonical_sha256
from test_goat_batch_plan import fixture_novel, plan, snapshot

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


@pytest.fixture
def witnessed(tmp_path, monkeypatch):
    project = fixture_novel(tmp_path, next_chapter=2, final=8)
    source = "蓋州游擊"
    rows = json.loads(project.chapters_file.read_bytes())
    rows[0]["text"] = source + "來了。"
    atomic_write_json(project.chapters_file, rows)
    atomic_write_json(project.glossary_file, {"entries": {source: {
        "english": "Ganzhou Mobile Corps Commander", "category": "historical office",
        "status": "provisional", "first_chapter": 1}}})
    (project.chapter_dir / "0001.md").write_text("Ganzhou Mobile Corps Commander arrived.", encoding="utf-8")
    module = local_module(project)
    monkeypatch.setattr(module, "MING_COMPATIBILITY_MODE", True)
    with production_run_lock(project.root):
        module.apply_known_glossary_fixes()
    assert (project.chapter_dir / "0001.md").read_text(encoding="utf-8") == "Gaizhou Mobile Corps Commander arrived."
    assert len(verified_events(local_module(project))) == 1
    witness = witnesses(local_module(project))[0]
    receipt = project.retroactive_dir / "transactions" / (slot_for(witness["plan"]) + ".authorization.json")
    assert json.loads(receipt.read_bytes()) == bound_authorization(witness["authorization"], witness["witness_sha256"])
    return project, witness, receipt


def test_legitimate_standalone_receipt_is_readonly_and_unpaid(witnessed):
    project, witness, receipt = witnessed
    before = snapshot(project)
    result = plan(project)
    assert result["authorization_binding"]["initial_chapter"] == 2
    assert result["paid_execution_authorized"] is False
    assert result["api_calls"] == result["project_writes"] == 0
    assert result["execution_blockers"]
    assert before == snapshot(project)


@pytest.mark.parametrize("damage", ["missing_witness", "missing_seal", "forged_witness", "wrong_source",
    "foreign_novel", "orphan_receipt", "duplicate_slot", "mismatched_plan", "extra_key", "missing_binding"])
def test_standalone_receipt_requires_actual_original_ownership(witnessed, damage):
    project, witness, receipt = witnessed
    folder = project.retroactive_dir / "standalone_lineage"
    prepared = folder / (slot_for(witness["plan"]) + ".prepared.json")
    if damage == "missing_witness":
        prepared.unlink()
    elif damage == "missing_seal":
        prepared.with_name(prepared.name.replace(".prepared.json", ".complete.json")).unlink()
    elif damage == "forged_witness":
        value = copy.deepcopy(witness)
        value["before_chapters"]["0001.md"] = "f" * 64
        value["witness_sha256"] = canonical_sha256({k: v for k, v in value.items() if k != "witness_sha256"})
        atomic_write_json(prepared, value)
    elif damage == "wrong_source":
        rows = json.loads(project.chapters_file.read_bytes())
        rows[0]["text"] = "不同來源。"
        atomic_write_json(project.chapters_file, rows)
    elif damage == "foreign_novel":
        value = json.loads(project.config_file.read_bytes()); value["id"] = "other"
        atomic_write_json(project.config_file, value)
    elif damage == "orphan_receipt":
        receipt.with_name("f" * 64 + ".authorization.json").write_bytes(receipt.read_bytes())
    elif damage == "duplicate_slot":
        prepared.with_name("f" * 64 + ".prepared.json").write_bytes(prepared.read_bytes())
    else:
        value = json.loads(receipt.read_bytes())
        if damage == "mismatched_plan": value["plan_sha256"] = "f" * 64
        elif damage == "extra_key": value["unowned"] = "f" * 64
        else: value.pop("standalone_witness_sha256")
        value["authorization_sha256"] = canonical_sha256({k: v for k, v in value.items() if k != "authorization_sha256"})
        atomic_write_json(receipt, value)
    before = snapshot(project)
    with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
        plan(project)
    assert before == snapshot(project)
