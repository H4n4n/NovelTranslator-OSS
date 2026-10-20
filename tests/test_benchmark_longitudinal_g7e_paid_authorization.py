import copy

import pytest

import benchmark_longitudinal_g7e_paid_authorization as g7e


STATE_SHA = "1" * 64


def _clean_journal(*records):
    return {
        "errors": [],
        "uncertain": [],
        "records": list(records),
    }


def test_g7e_binds_exact_g7c_and_g7d_and_makes_zero_calls():
    identity = g7e.g7e_authorization_identity()

    assert identity["predecessor"]["g7c_integration_sha256"] == (
        "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
    )
    assert identity["predecessor"]["g7d_accounting_sha256"] == (
        "1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0"
    )
    assert identity["provider_calls"] == 0
    assert identity["generic_stage1_blockers_remaining"] == []
    assert identity["resolved_by_g7e"] == [
        "freeze_stage1_paid_execution_authorization_contract"
    ]


def test_contract_is_one_run_one_chapter_and_does_not_authorize_without_plan():
    identity = g7e.g7e_authorization_identity()
    scope = identity["authorization_scope"]
    stage1 = identity["stage1"]

    assert scope["mode"] == "single_run_single_target_chapter"
    assert scope["one_run_per_invocation"] is True
    assert scope["one_target_chapter_per_invocation"] is True
    assert scope["maximum_concurrent_paid_invocations"] == 1
    assert scope["automatic_advance_to_next_chapter"] is False
    assert scope["new_authorization_required_for_next_chapter"] is True

    assert stage1["generic_paid_execution_contract_frozen"] is True
    assert stage1["provider_calls_authorized_only_by_valid_invocation_plan"] is True
    assert stage1["provider_calls_authorized_without_invocation_plan"] is False
    assert stage1["model_ranking_or_elimination_performed"] is False
    assert stage1["finalist_continuation_authorized"] is False

    g7e.assert_contract_ready_for_invocation_planning()


def test_journal_policy_matches_production_retry_ownership():
    policy = g7e.g7e_authorization_identity()["journal_resume_policy"]

    assert policy["planned"] == "safe_to_resume_exact_request_before_transport"
    assert policy["response_recorded"] == (
        "local_replay_only_no_new_provider_send"
    )
    assert policy["started"] == (
        "ambiguous_remote_outcome_block_automatic_reissue"
    )
    assert policy["uncertain"] == (
        "ambiguous_remote_outcome_block_automatic_reissue"
    )
    assert policy["automatic_retry_after_provider_or_transport_exception"] is False
    assert policy["hidden_provider_retries"] == 0
    assert policy["provider_side_exactly_once_claimed"] is False


def test_build_and_verify_invocation_authorization():
    plan = g7e.build_invocation_authorization(
        run_index=1,
        target_chapter=1,
        pre_invocation_state_fingerprint_sha256=STATE_SHA,
        maximum_new_provider_sends=8,
        maximum_credit_equivalent_usd="0.50",
    )

    assert plan["target_chapter"] == 1
    assert plan["maximum_new_provider_sends"] == 8
    assert plan["maximum_credit_equivalent_usd"] == "0.5"
    assert plan["automatic_retry_forbidden"] is True
    assert plan["automatic_chapter_advance_forbidden"] is True

    verified = g7e.verify_invocation_authorization(
        plan,
        expected_approval_token=plan["approval_token"],
        current_state_fingerprint_sha256=STATE_SHA,
        operator_authorized=True,
    )

    assert verified["authorized"] is True
    assert verified["run_index"] == 1
    assert verified["target_chapter"] == 1
    assert verified["maximum_new_provider_sends"] == 8
    assert verified["maximum_credit_equivalent_usd"] == "0.5"


def test_approval_token_is_bound_to_run_and_chapter():
    plan = g7e.build_invocation_authorization(
        run_index=1,
        target_chapter=1,
        pre_invocation_state_fingerprint_sha256=STATE_SHA,
        maximum_new_provider_sends=8,
        maximum_credit_equivalent_usd="0.50",
    )

    tampered = copy.deepcopy(plan)
    tampered["target_chapter"] = 2

    with pytest.raises(g7e.G7EAuthorizationError, match="does not recompute"):
        g7e.verify_invocation_authorization(
            tampered,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256=STATE_SHA,
            operator_authorized=True,
        )


def test_state_drift_blocks_authorized_plan():
    plan = g7e.build_invocation_authorization(
        run_index=1,
        target_chapter=1,
        pre_invocation_state_fingerprint_sha256=STATE_SHA,
        maximum_new_provider_sends=8,
        maximum_credit_equivalent_usd="0.50",
    )

    with pytest.raises(g7e.G7EAuthorizationError, match="state fingerprint changed"):
        g7e.verify_invocation_authorization(
            plan,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256="2" * 64,
            operator_authorized=True,
        )


def test_operator_flag_is_mandatory_even_with_valid_token():
    plan = g7e.build_invocation_authorization(
        run_index=1,
        target_chapter=1,
        pre_invocation_state_fingerprint_sha256=STATE_SHA,
        maximum_new_provider_sends=8,
        maximum_credit_equivalent_usd="0.50",
    )

    with pytest.raises(g7e.G7EAuthorizationError, match="operator authorization"):
        g7e.verify_invocation_authorization(
            plan,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256=STATE_SHA,
            operator_authorized=False,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"run_index": 0}, "run_index"),
        ({"target_chapter": 31}, "Stage-1"),
        ({"maximum_new_provider_sends": 0}, "maximum_new_provider_sends"),
        ({"maximum_credit_equivalent_usd": 0}, "positive finite"),
        (
            {"pre_invocation_state_fingerprint_sha256": "bad"},
            "lowercase SHA-256",
        ),
    ],
)
def test_invalid_invocation_limits_fail_closed(kwargs, match):
    values = {
        "run_index": 1,
        "target_chapter": 1,
        "pre_invocation_state_fingerprint_sha256": STATE_SHA,
        "maximum_new_provider_sends": 8,
        "maximum_credit_equivalent_usd": "0.50",
    }
    values.update(kwargs)

    with pytest.raises(g7e.G7EAuthorizationError, match=match):
        g7e.build_invocation_authorization(**values)


def test_empty_journal_is_resumable_for_new_exact_send():
    result = g7e.classify_journal_resume_state(_clean_journal())

    assert result["status"] == "resumable"
    assert result["automatic_paid_continuation_allowed"] is True
    assert result["new_provider_send_allowed"] is True
    assert result["local_replay_only_count"] == 0


def test_planned_journal_is_resumable():
    result = g7e.classify_journal_resume_state(
        _clean_journal({"status": "planned", "operation_id": "x"})
    )

    assert result["status"] == "resumable"
    assert result["planned_send_count"] == 1
    assert result["new_provider_send_allowed"] is True


def test_response_recorded_is_local_replay_and_not_a_retry():
    result = g7e.classify_journal_resume_state(
        _clean_journal(
            {"status": "response_recorded", "operation_id": "x"},
        )
    )

    assert result["status"] == "resumable"
    assert result["local_replay_only_count"] == 1


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_ambiguous_journal_state_blocks_automatic_reissue(status):
    record = {"status": status, "operation_id": "x"}
    result = g7e.classify_journal_resume_state(
        {
            "errors": [],
            "uncertain": [record] if status == "uncertain" else [],
            "records": [record],
        }
    )

    assert result["status"] == "blocked_ambiguous_remote_outcome"
    assert result["automatic_paid_continuation_allowed"] is False
    assert result["new_provider_send_allowed"] is False


def test_corrupt_journal_blocks_paid_execution():
    result = g7e.classify_journal_resume_state(
        {
            "errors": ["corrupt journal"],
            "uncertain": [],
            "records": [],
        }
    )

    assert result["status"] == "blocked_corrupt_or_unknown_journal"
    assert result["automatic_paid_continuation_allowed"] is False


def test_chapter_one_pristine_s0_is_ready():
    result = g7e.classify_chapter_resume_state(
        progress={"last_completed": 0, "next_chapter": 1},
        target_chapter=1,
        journal_inspection=_clean_journal(),
    )

    assert result["status"] == "ready_or_resumable"
    assert result["paid_send_needed"] is True


def test_chronological_mismatch_blocks_skip_ahead():
    result = g7e.classify_chapter_resume_state(
        progress={"last_completed": 0, "next_chapter": 1},
        target_chapter=2,
        journal_inspection=_clean_journal(),
    )

    assert result["status"] == "blocked_chronological_state_mismatch"
    assert result["automatic_paid_continuation_allowed"] is False


def test_already_completed_target_never_authorizes_another_paid_send():
    result = g7e.classify_chapter_resume_state(
        progress={"last_completed": 1, "next_chapter": 2},
        target_chapter=1,
        journal_inspection=_clean_journal(
            {"status": "response_recorded", "operation_id": "x"}
        ),
    )

    assert result["status"] == "target_already_complete"
    assert result["paid_send_needed"] is False
    assert result["automatic_paid_continuation_allowed"] is False


def test_ambiguous_journal_blocks_chapter_resume_even_when_progress_matches():
    result = g7e.classify_chapter_resume_state(
        progress={"last_completed": 0, "next_chapter": 1},
        target_chapter=1,
        journal_inspection={
            "errors": [],
            "uncertain": [{"status": "uncertain", "operation_id": "x"}],
            "records": [{"status": "uncertain", "operation_id": "x"}],
        },
    )

    assert result["status"] == "blocked_ambiguous_remote_outcome"
    assert result["paid_send_needed"] is False
