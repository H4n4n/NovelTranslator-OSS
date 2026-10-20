import copy
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g10_recovery_rehearsal as g10d
from benchmark_longitudinal_g10_safety import CreditReservationLedger
from full_pipeline_periodic_audit_recovery import _snapshot_baseline
from production_paid_request_journal import (
    _with_record_hash,
    request_identity,
)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _minimal_project(root):
    root = Path(root)
    (root / "data").mkdir(parents=True)
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "output" / "retroactive_fixes").mkdir(parents=True)
    (root / "output" / "audits").mkdir(parents=True)
    (root / "backup").mkdir(parents=True)
    _write_json(root / "novel.json", {"id": "fixture"})
    _write_json(
        root / "data" / "progress.json",
        {
            "completed": list(range(1, 26)),
            "last_completed": 25,
            "next_chapter": 26,
            "last_audited_chapter": 0,
            "total_prompt_tokens": 10,
        },
    )
    _write_json(root / "data" / "glossary.json", {"entries": {}})
    _write_json(root / "data" / "continuity.json", {"summary": "before"})
    _write_json(root / "data" / "issues.json", {"open": []})
    _write_json(root / "data" / "audit_history.json", {"audits": []})
    (root / "output" / "chapters" / "0001.md").write_text(
        "before chapter\n", encoding="utf-8"
    )
    (root / "output" / "retroactive_fixes" / "before.json").write_text(
        "before retro\n", encoding="utf-8"
    )
    return root


def _journal_record(identity, status="response_recorded"):
    response = {
        "content": "durable response",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    record = {
        "schema_version": 1,
        **copy.deepcopy(identity),
        "status": status,
        "attempt_context": {"send_started": True},
        "response": response if status == "response_recorded" else {},
        "response_sha256": (
            g10d._sha256_json(response)
            if status == "response_recorded"
            else ""
        ),
        "error": (
            {}
            if status == "response_recorded"
            else {"type": "FixtureAmbiguity", "message": "unknown"}
        ),
        "created_utc": "2026-09-29T00:00:00+00:00",
        "updated_utc": "2026-09-29T00:00:01+00:00",
    }
    return _with_record_hash(record)


def _accepted_ledger(path, request_sha256):
    authorization_sha256 = "a" * 64
    ledger = CreditReservationLedger.create(
        path=path,
        authorization_sha256=authorization_sha256,
        maximum_credit_equivalent="10",
        arm_credit_equivalent_ceilings={
            "G10A": "5",
            "G10B": "5",
        },
    )
    reservation = ledger.reserve(
        request_sha256=request_sha256,
        arm_id="G10A",
        scope_id="fixture-scope",
        logical_operation="translation",
        model_id="fixture-model",
        novel_id="fixture",
        chapter=1,
        reserved_credit_equivalent="1",
    )
    ledger.mark_send_started(reservation["reservation_id"])
    ledger.record_accepted(
        reservation["reservation_id"],
        accepted_credit_equivalent="0.5",
    )
    return authorization_sha256, reservation["reservation_id"]


def test_contract_is_zero_provider_and_paid_execution_blocked():
    g10d.assert_paid_execution_blocked()
    identity = g10d.g10d_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["rehearsals"]["poisoned_transport_required"] is True
    assert (
        identity["rehearsals"]["real_periodic_audit_reconciliation_required"]
        is True
    )


def test_physical_shadow_is_exact_and_create_once(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "a.txt").write_text("alpha\n", encoding="utf-8")
    (original / "nested").mkdir()
    (original / "nested" / "b.bin").write_bytes(b"\x00\x01\x02")
    shadow = tmp_path / "shadow"

    result = g10d.create_physical_shadow(original, shadow)
    assert result["physical_shadow_verified"] is True
    assert result["provider_calls"] == 0
    assert result["original_fingerprint"] == result["shadow_fingerprint"]
    assert g10d.tree_fingerprint(original) == g10d.tree_fingerprint(shadow)

    with pytest.raises(g10d.G10DRehearsalError, match="already exists"):
        g10d.create_physical_shadow(original, shadow)


def test_response_recorded_replays_on_shadow_with_poisoned_transport(tmp_path):
    original = tmp_path / "original"
    journal_rel = Path("data") / "journal" / "0001.translation.json"
    identity = request_identity(
        operation_kind="translation",
        novel_id="fixture",
        chapter=1,
        provider="openai",
        model="fixture-model",
        profile_id="fixture-profile",
        source_sha256="1" * 64,
        request_payload={"model": "fixture-model", "messages": []},
    )
    _write_json(original / journal_rel, _journal_record(identity))
    ledger_rel = Path("data") / "g10_budget.json"
    authorization_sha256, reservation_id = _accepted_ledger(
        original / ledger_rel,
        identity["request_sha256"],
    )
    shadow = tmp_path / "shadow"
    g10d.create_physical_shadow(original, shadow)
    original_before = g10d.tree_fingerprint(original)

    replay = g10d.replay_recorded_response_on_shadow(
        original_root=original,
        shadow_root=shadow,
        journal_relative_path=journal_rel,
        ledger_relative_path=ledger_rel,
        authorization_sha256=authorization_sha256,
        reservation_id=reservation_id,
        expected_identity=identity,
        normalize_response=lambda response, _context: response,
    )
    assert replay["api_called"] is False
    assert replay["replayed"] is True
    assert replay["provider_calls"] == 0
    assert replay["ledger_status"] == "accepted"
    assert replay["reservation_id"] == reservation_id
    assert replay["response"]["content"] == "durable response"
    assert g10d.tree_fingerprint(original) == original_before




def test_response_recorded_replay_rejects_mismatched_durable_reservation(tmp_path):
    original = tmp_path / "original"
    journal_rel = Path("data") / "journal" / "0001.translation.json"
    identity = request_identity(
        operation_kind="translation",
        novel_id="fixture",
        chapter=1,
        provider="openai",
        model="fixture-model",
        profile_id="fixture-profile",
        source_sha256="3" * 64,
        request_payload={"model": "fixture-model", "messages": []},
    )
    _write_json(original / journal_rel, _journal_record(identity))
    ledger_rel = Path("data") / "g10_budget.json"
    authorization_sha256, reservation_id = _accepted_ledger(
        original / ledger_rel,
        "4" * 64,
    )
    shadow = tmp_path / "shadow"
    g10d.create_physical_shadow(original, shadow)

    with pytest.raises(Exception, match="request identity"):
        g10d.replay_recorded_response_on_shadow(
            original_root=original,
            shadow_root=shadow,
            journal_relative_path=journal_rel,
            ledger_relative_path=ledger_rel,
            authorization_sha256=authorization_sha256,
            reservation_id=reservation_id,
            expected_identity=identity,
            normalize_response=lambda response, _context: response,
        )

@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_ambiguous_shadow_never_reaches_transport(tmp_path, status):
    shadow = tmp_path / "shadow"
    journal_rel = Path("data") / "journal" / "0001.translation.json"
    identity = request_identity(
        operation_kind="translation",
        novel_id="fixture",
        chapter=1,
        provider="openai",
        model="fixture-model",
        profile_id="fixture-profile",
        source_sha256="2" * 64,
        request_payload={"model": "fixture-model", "messages": []},
    )
    _write_json(shadow / journal_rel, _journal_record(identity, status=status))
    g10d.assert_ambiguous_shadow_never_resends(
        shadow_root=shadow,
        journal_relative_path=journal_rel,
        expected_identity=identity,
        normalize_response=lambda response, _context: response,
    )


def test_real_audit_recovery_restores_shadow_and_never_mutates_original(tmp_path):
    original = _minimal_project(tmp_path / "original")
    _snapshot_baseline(original, 1, 25)
    shadow = tmp_path / "shadow"
    g10d.create_physical_shadow(original, shadow)
    original_before = g10d.tree_fingerprint(original)

    glossary = json.loads(
        (shadow / "data" / "glossary.json").read_text(encoding="utf-8")
    )
    glossary["entries"]["甲"] = {"english": "Changed"}
    _write_json(shadow / "data" / "glossary.json", glossary)
    progress = json.loads(
        (shadow / "data" / "progress.json").read_text(encoding="utf-8")
    )
    progress["total_prompt_tokens"] = 99999
    progress["last_audited_chapter"] = 0
    _write_json(shadow / "data" / "progress.json", progress)
    _write_json(
        shadow / "data" / "audit_history.json",
        {"audits": [{"start_chapter": 1, "end_chapter": 25}]},
    )
    (shadow / "output" / "chapters" / "0001.md").write_text(
        "partially rewritten chapter\n", encoding="utf-8"
    )
    (shadow / "output" / "retroactive_fixes" / "before.json").write_text(
        "partially rewritten retro\n", encoding="utf-8"
    )
    (shadow / "output" / "retroactive_fixes" / "new.json").write_text(
        "new partial evidence\n", encoding="utf-8"
    )
    _write_json(
        shadow / "output" / "audits" / "audit_0001_0025.json",
        {"start_chapter": 1, "end_chapter": 25},
    )
    backup = shadow / "backup" / "checkpoint_0025"
    backup.mkdir(parents=True)
    (backup / "partial.txt").write_text("partial\n", encoding="utf-8")

    result = g10d.reconcile_audit_on_physical_shadow(
        original_root=original,
        shadow_root=shadow,
    )
    assert result["provider_calls"] == 0
    assert result["transport_poisoned"] is True
    assert result["original_unchanged"] is True
    assert any(
        action["action"] == "restored_baseline"
        for action in result["actions"]
    )
    assert (shadow / "output" / "chapters" / "0001.md").read_text(
        encoding="utf-8"
    ) == "before chapter\n"
    assert (
        shadow / "output" / "retroactive_fixes" / "before.json"
    ).read_text(encoding="utf-8") == "before retro\n"
    assert not (shadow / "output" / "retroactive_fixes" / "new.json").exists()
    assert not (
        shadow / "output" / "audits" / "audit_0001_0025.json"
    ).exists()
    assert not backup.exists()
    assert g10d.tree_fingerprint(original) == original_before


def test_shadow_and_original_may_not_contain_each_other(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    with pytest.raises(g10d.G10DRehearsalError, match="inside original"):
        g10d.create_physical_shadow(original, original / "shadow")
