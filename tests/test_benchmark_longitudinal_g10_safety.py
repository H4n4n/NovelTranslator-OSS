from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

import benchmark_longitudinal_g10_safety as safety


AUTH_SHA = "a" * 64
REQ1 = "1" * 64
REQ2 = "2" * 64


def _clock():
    return datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)


def _ledger(tmp_path, maximum="10"):
    return safety.CreditReservationLedger.create(
        path=tmp_path / "budget.json",
        authorization_sha256=AUTH_SHA,
        maximum_credit_equivalent=maximum,
        arm_credit_equivalent_ceilings={
            "G10A": "1",
            "G10B": str(Decimal(maximum) - Decimal("1")),
        },
        clock=_clock,
    )


def _reserve(ledger, request_sha256=REQ1, reserved="2"):
    return ledger.reserve(
        request_sha256=request_sha256,
        arm_id="G10B",
        scope_id="qwen::ace_evolution",
        logical_operation="translation",
        model_id="qwen/qwen3.8-max-0902",
        novel_id="ace_evolution",
        chapter=1,
        reserved_credit_equivalent=reserved,
    )


def test_g10a1_is_zero_provider_and_does_not_authorize_paid_execution():
    safety.assert_paid_execution_still_blocked()
    identity = safety.g10a1_safety_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["journal"]["maximum_concurrent_paid_transports"] == 1
    assert identity["budget"]["single_writer_ledger_required"] is True
    assert identity["budget"]["one_global_ceiling_ledger_required"] is True
    assert (
        identity["budget"]["committed_ce_definition"]
        == "accepted_ce_plus_full_reservations_for_unresolved_or_ambiguous_requests"
    )


def test_fresh_root_rejects_equal_nested_and_parent_overlap(tmp_path):
    frozen = tmp_path / "GOAT"
    frozen.mkdir()

    with pytest.raises(safety.G10SafetyError, match="overlaps"):
        safety.assert_fresh_execution_root(
            frozen,
            forbidden_roots=(frozen,),
        )

    with pytest.raises(safety.G10SafetyError, match="overlaps"):
        safety.assert_fresh_execution_root(
            frozen / "nested",
            forbidden_roots=(frozen,),
        )

    with pytest.raises(safety.G10SafetyError, match="overlaps"):
        safety.assert_fresh_execution_root(
            tmp_path,
            forbidden_roots=(frozen,),
        )

    clean = safety.assert_fresh_execution_root(
        tmp_path / "G10_FRESH",
        forbidden_roots=(frozen,),
    )
    assert clean.name == "G10_FRESH"


def test_child_process_environment_forces_utf8_without_dropping_values():
    env = safety.utf8_subprocess_env({"EXISTING": "yes"})
    assert env["EXISTING"] == "yes"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"

    poisoned = safety.poisoned_transport_env({"EXISTING": "yes"})
    assert poisoned["G10_TRANSPORT_POISONED"] == "1"
    assert poisoned["PYTHONUTF8"] == "1"
    assert poisoned["PYTHONIOENCODING"] == "utf-8"


def test_reservation_counts_full_ce_before_transport_and_survives_restart(tmp_path):
    ledger = _ledger(tmp_path, maximum="10")
    row = _reserve(ledger, reserved="3")
    assert ledger.committed_credit_equivalent == Decimal("3")
    assert ledger.remaining_credit_equivalent == Decimal("7")
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "safe_pre_send_resume"
    )

    reopened = safety.CreditReservationLedger.open(
        path=tmp_path / "budget.json",
        authorization_sha256=AUTH_SHA,
        clock=_clock,
    )
    assert reopened.committed_credit_equivalent == Decimal("3")
    assert reopened.remaining_credit_equivalent == Decimal("7")


def test_pre_send_release_requires_no_send_proof_and_frees_reservation(tmp_path):
    ledger = _ledger(tmp_path)
    row = _reserve(ledger, reserved="4")

    with pytest.raises(safety.G10BudgetError, match="explicit no-send proof"):
        ledger.release_pre_send(
            row["reservation_id"],
            transport_never_started_proven=False,
        )

    ledger.release_pre_send(
        row["reservation_id"],
        transport_never_started_proven=True,
    )
    assert ledger.committed_credit_equivalent == Decimal("0")
    assert ledger.remaining_credit_equivalent == Decimal("10")
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "safe_no_send_released"
    )


def test_started_unresolved_keeps_full_reservation_and_cannot_release(tmp_path):
    ledger = _ledger(tmp_path)
    row = _reserve(ledger, reserved="4")
    ledger.mark_send_started(row["reservation_id"])

    assert ledger.committed_credit_equivalent == Decimal("4")
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "quarantine_no_resend"
    )

    with pytest.raises(safety.G10BudgetError, match="can never be released"):
        ledger.release_pre_send(
            row["reservation_id"],
            transport_never_started_proven=True,
        )


def test_ambiguous_outcome_retains_full_reservation_and_blocks_duplicate(tmp_path):
    ledger = _ledger(tmp_path)
    row = _reserve(ledger, reserved="4")
    ledger.mark_send_started(row["reservation_id"])
    ledger.mark_ambiguous(
        row["reservation_id"],
        reason="transport outcome unknown",
    )

    assert ledger.committed_credit_equivalent == Decimal("4")
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "quarantine_no_resend"
    )

    with pytest.raises(safety.G10BudgetError, match="already has"):
        _reserve(ledger, request_sha256=REQ1, reserved="1")


def test_accepted_usage_replaces_reservation_with_exact_ce(tmp_path):
    ledger = _ledger(tmp_path)
    row = _reserve(ledger, reserved="4")
    ledger.mark_send_started(row["reservation_id"])
    ledger.record_accepted(
        row["reservation_id"],
        accepted_credit_equivalent="1.25",
    )

    assert ledger.accepted_credit_equivalent == Decimal("1.25")
    assert ledger.committed_credit_equivalent == Decimal("1.25")
    assert ledger.remaining_credit_equivalent == Decimal("8.75")
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "accepted_complete"
    )


def test_reservation_admission_respects_arm_partition_before_global_ceiling(tmp_path):
    ledger = _ledger(tmp_path, maximum="5")
    assert ledger.arm_credit_equivalent_ceiling("G10B") == Decimal("4")
    _reserve(ledger, request_sha256=REQ1, reserved="3")

    with pytest.raises(safety.G10BudgetError, match="arm credit-equivalent"):
        _reserve(ledger, request_sha256=REQ2, reserved="1.1")

    _reserve(ledger, request_sha256=REQ2, reserved="1")
    assert ledger.arm_committed_credit_equivalent("G10B") == Decimal("4")
    assert ledger.committed_credit_equivalent == Decimal("4")
    assert ledger.remaining_credit_equivalent == Decimal("1")


def test_accepted_above_conservative_reservation_is_terminal(tmp_path):
    ledger = _ledger(tmp_path, maximum="10")
    row = _reserve(ledger, reserved="2")
    ledger.mark_send_started(row["reservation_id"])

    with pytest.raises(safety.G10BudgetError, match="exceeded"):
        ledger.record_accepted(
            row["reservation_id"],
            accepted_credit_equivalent="2.01",
        )

    assert ledger.has_terminal_budget_overrun is True
    assert (
        ledger.resume_disposition(row["reservation_id"])["disposition"]
        == "terminal_budget_overrun"
    )
    with pytest.raises(safety.G10BudgetError, match="terminal"):
        _reserve(ledger, request_sha256=REQ2, reserved="1")


def test_tampered_budget_ledger_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    _reserve(ledger, reserved="2")
    path = tmp_path / "budget.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["maximum_credit_equivalent"] = "999"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(safety.G10BudgetError, match="SHA does not recompute"):
        safety.CreditReservationLedger.open(
            path=path,
            authorization_sha256=AUTH_SHA,
            clock=_clock,
        )


def test_quarantine_is_create_once_hash_bound_and_scope_aware(tmp_path):
    marker_path = (
        tmp_path / "track" / "data" / "g10_quarantine.json"
    )
    marker = safety.create_quarantine_marker(
        path=marker_path,
        scope_kind="chronological_model_novel_track",
        scope_id="qwen::ace_evolution",
        outcome="ambiguous_remote_outcome",
        reason="request may have completed remotely",
        request_sha256=REQ1,
        reservation_id=REQ2,
        first_blocked_chapter=17,
        clock=_clock,
    )
    loaded = safety.load_quarantine_marker(marker_path)
    assert loaded == marker
    assert loaded["first_blocked_chapter"] == 17

    with pytest.raises(safety.G10QuarantineError, match="already exists"):
        safety.create_quarantine_marker(
            path=marker_path,
            scope_kind="chronological_model_novel_track",
            scope_id="qwen::ace_evolution",
            outcome="ambiguous_remote_outcome",
            reason="duplicate",
            request_sha256=REQ1,
            reservation_id=REQ2,
            first_blocked_chapter=17,
            clock=_clock,
        )

    with pytest.raises(safety.G10QuarantineError, match="must not carry"):
        safety.create_quarantine_marker(
            path=tmp_path / "literary.json",
            scope_kind="literary_model_case",
            scope_id="qwen::ace::3",
            outcome="corrupt_or_unknown_durable_state",
            reason="corrupt response evidence",
            request_sha256=REQ1,
            reservation_id=REQ2,
            first_blocked_chapter=3,
            clock=_clock,
        )


def test_tampered_quarantine_marker_fails_closed(tmp_path):
    path = tmp_path / "marker.json"
    safety.create_quarantine_marker(
        path=path,
        scope_kind="literary_model_case",
        scope_id="qwen::ace::3",
        outcome="corrupt_or_unknown_durable_state",
        reason="corrupt response evidence",
        request_sha256=REQ1,
        reservation_id=REQ2,
        clock=_clock,
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["reason"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        safety.G10QuarantineError,
        match="SHA does not recompute",
    ):
        safety.load_quarantine_marker(path)


def test_shadow_replay_requires_distinct_root_recorded_accepted_and_poison(tmp_path):
    original = tmp_path / "original"
    shadow = tmp_path / "shadow"
    original.mkdir()
    shadow.mkdir()

    result = safety.assert_shadow_replay_preconditions(
        original_root=original,
        shadow_root=shadow,
        journal_status="response_recorded",
        ledger_status="accepted",
        exact_request_match=True,
        transport_poisoned=True,
    )
    assert result["replay_authorized"] is True
    assert result["provider_send_authorized"] is False

    with pytest.raises(safety.G10ReplayError, match="poisoned"):
        safety.assert_shadow_replay_preconditions(
            original_root=original,
            shadow_root=shadow,
            journal_status="response_recorded",
            ledger_status="accepted",
            exact_request_match=True,
            transport_poisoned=False,
        )

    with pytest.raises(safety.G10ReplayError, match="response_recorded"):
        safety.assert_shadow_replay_preconditions(
            original_root=original,
            shadow_root=shadow,
            journal_status="started",
            ledger_status="accepted",
            exact_request_match=True,
            transport_poisoned=True,
        )

    with pytest.raises(safety.G10ReplayError, match="differ"):
        safety.assert_shadow_replay_preconditions(
            original_root=original,
            shadow_root=original,
            journal_status="response_recorded",
            ledger_status="accepted",
            exact_request_match=True,
            transport_poisoned=True,
        )


def test_arm_subceiling_is_enforced_inside_single_global_ledger(tmp_path):
    ledger = safety.CreditReservationLedger.create(
        path=tmp_path / "budget.json",
        authorization_sha256=AUTH_SHA,
        maximum_credit_equivalent="10",
        arm_credit_equivalent_ceilings={
            "G10A": "3",
            "G10B": "7",
        },
        clock=_clock,
    )
    assert ledger.arm_credit_equivalent_ceiling("G10A") == Decimal("3")
    assert ledger.arm_credit_equivalent_ceiling("G10B") == Decimal("7")

    ledger.reserve(
        request_sha256=REQ1,
        arm_id="G10A",
        scope_id="qwen::ace::3",
        logical_operation="translation",
        model_id="qwen/qwen3.8-max-0902",
        novel_id="ace_evolution",
        chapter=3,
        reserved_credit_equivalent="3",
    )
    assert ledger.arm_remaining_credit_equivalent("G10A") == Decimal("0")
    assert ledger.remaining_credit_equivalent == Decimal("7")

    with pytest.raises(safety.G10BudgetError, match="arm credit-equivalent"):
        ledger.reserve(
            request_sha256=REQ2,
            arm_id="G10A",
            scope_id="glm::ace::3",
            logical_operation="translation",
            model_id="z-ai/glm-5.3-flash",
            novel_id="ace_evolution",
            chapter=3,
            reserved_credit_equivalent="0.01",
        )


def test_journal_reservation_reconciliation_is_fail_closed():
    assert safety.classify_journal_reservation_state(
        journal_status=None,
        reservation_status="reserved",
        send_started=False,
    ) == "safe_pre_send"
    assert safety.classify_journal_reservation_state(
        journal_status="response_recorded",
        reservation_status="accepted",
        send_started=True,
    ) == "local_replay_candidate"
    assert safety.classify_journal_reservation_state(
        journal_status="started",
        reservation_status="accepted",
        send_started=True,
    ) == "quarantine_no_resend"
    assert safety.classify_journal_reservation_state(
        journal_status="planned",
        reservation_status="accepted",
        send_started=True,
    ) == "quarantine_no_resend"
    assert safety.classify_journal_reservation_state(
        journal_status="response_recorded",
        reservation_status="reserved",
        send_started=True,
    ) == "corrupt_block"


def test_canonical_local_replay_requires_poisoned_transport():
    result = safety.assert_local_replay_preconditions(
        journal_status="response_recorded",
        ledger_status="accepted",
        exact_request_match=True,
        transport_poisoned=True,
    )
    assert result["replay_authorized"] is True
    assert result["provider_send_authorized"] is False

    with pytest.raises(safety.G10ReplayError, match="poisoned"):
        safety.assert_local_replay_preconditions(
            journal_status="response_recorded",
            ledger_status="accepted",
            exact_request_match=True,
            transport_poisoned=False,
        )
