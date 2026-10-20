import benchmark_longitudinal_g7b_auxiliary_policy as g7b
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import (
    BENCHMARK_MODEL_IDS,
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
)
from benchmark_longitudinal_g7a_readiness import TRANSLATION_EFFORT_BY_MODEL


def test_g7b_binds_exact_successful_g7a_identity():
    assert g7b.G7A_EXPECTED_READINESS_SHA256 == (
        "f43a7f1130b0e20aeca7ba068d278b1c41c2ff064b351eaa816748b99f6cd1e0"
    )
    assert g7b.G7A_EXPECTED_CONFIGURATION_SHA256 == (
        "e011cb5d65e633ce99dad6388890e5b2bb6e35cf23fbefc28e4d4686b20486c8"
    )
    assert g7b.G7A_EXPECTED_FULL_PLAN_SHA256 == (
        "b791e655ff80707d7b5cb8b3d8aa8346360a589fac0559ede67046f342dde4d1"
    )


def test_g7b_preserves_all_models_and_lifecycle_order():
    policies = g7b.build_g7b_model_policies()

    assert tuple(policy.model_id for policy in policies) == BENCHMARK_MODEL_IDS
    for policy in policies:
        assert tuple(item.operation for item in policy.operations) == (
            PURE_RUN_LIFECYCLE_OPERATIONS
        )
        assert all(item.model_id == policy.model_id for item in policy.operations)


def test_structured_operations_use_lowest_verified_or_default():
    for policy in g7b.build_g7b_model_policies():
        live = live_provider_capability(policy.model_id)

        expected = "low" if live.verified_reasoning_efforts else None

        for operation in (
            LIFECYCLE_OPERATION_STATE_EXTRACTION,
            LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
        ):
            binding = policy.binding_for(operation)
            assert binding.effort == expected
            assert binding.status == "frozen_g7b"
            if expected is None:
                assert (
                    binding.basis
                    == "g5c_verified_provider_default_for_structured_operation"
                )
            else:
                assert (
                    binding.basis
                    == "g5c_lowest_verified_effort_for_structured_operation"
                )


def test_semantic_operations_reuse_g6k_translation_selection():
    for policy in g7b.build_g7b_model_policies():
        expected = TRANSLATION_EFFORT_BY_MODEL[policy.model_id]

        for operation in (
            LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
            LIFECYCLE_OPERATION_PERIODIC_AUDIT,
            LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
        ):
            binding = policy.binding_for(operation)
            assert binding.effort == expected
            assert binding.status == "frozen_g7b"


def test_translation_binding_remains_exact_g6k_selection():
    for policy in g7b.build_g7b_model_policies():
        binding = policy.binding_for(LIFECYCLE_OPERATION_TRANSLATION)
        assert binding.effort == TRANSLATION_EFFORT_BY_MODEL[policy.model_id]
        assert binding.status == "frozen_from_g6k"
        assert binding.basis == "g6k_primary_translation_selection"


def test_every_explicit_effort_is_live_verified():
    for policy in g7b.build_g7b_model_policies():
        live = live_provider_capability(policy.model_id)

        for binding in policy.operations:
            if binding.effort is None:
                assert not live.verified_reasoning_efforts
                assert live.default_effort_request_verified is True
            else:
                assert binding.effort in live.verified_reasoning_efforts


def test_default_only_models_never_invent_reasoning_effort():
    for model_id in ("tencent/hy3-paid", "xiaomi/mimo-v2.5-pro"):
        policy = g7b.build_g7b_model_policy(model_id)
        assert all(binding.effort is None for binding in policy.operations)


def test_g7b_resolves_all_five_reasoning_blockers_but_keeps_paid_stage1_blocked():
    identity = g7b.g7b_policy_identity()

    assert identity["reasoning_policy_complete"] is True
    assert identity["provider_calls"] == 0
    assert identity["stage1"]["provider_calls_authorized"] is False
    assert identity["stage1"]["model_ranking_or_elimination_performed"] is False
    assert identity["stage1"]["finalist_continuation_authorized"] is False

    gate = identity["execution_gate"]
    assert gate["status"] == "blocked"
    assert gate["reason"] == "stage1_execution_integration_not_proven"

    assert set(gate["resolved_by_g7b"]) == {
        "freeze_state_extraction_reasoning",
        "freeze_state_json_repair_reasoning",
        "freeze_glossary_review_reasoning",
        "freeze_periodic_audit_reasoning",
        "freeze_terminology_repair_reasoning",
    }

    assert gate["required_before_paid_stage1"] == [
        "bind_stage1_execution_to_real_production_lifecycle",
        "prove_external_isolated_s0_roots_and_production_guard",
        "prove_durable_paid_request_journal_for_every_provider_assisted_operation",
    ]

    g7b.assert_paid_stage1_blocked()


def test_g7b_policy_principles_are_explicit():
    principles = g7b.g7b_policy_identity()["policy_principles"]

    assert principles == {
        "same_model_all_provider_operations": True,
        "explicit_per_operation_reasoning": True,
        "no_silent_fallback": True,
        "auxiliary_effort_is_policy_not_independent_quality_optimum": True,
        "structured_operations_use_lowest_verified_effort": True,
        "semantic_operations_reuse_g6k_translation_selection": True,
        "default_only_models_remain_provider_native_default": True,
    }


def test_g7b_policy_identity_is_deterministic():
    first = g7b.g7b_policy_identity()
    second = g7b.g7b_policy_identity()

    assert first == second
    assert len(g7b.G7B_POLICY_SHA256) == 64
