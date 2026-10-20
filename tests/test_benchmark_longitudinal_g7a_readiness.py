import benchmark_longitudinal_g7a_readiness as g7a
from benchmark_longitudinal_config import (
    BENCHMARK_MODEL_IDS,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
)


def test_g7a_binds_authoritative_g6k_freeze():
    assert g7a.G6K_REVEAL_FREEZE_ID == (
        "a535595d373cfa0e92956c84142ef9877f09084425e644f651d1133b5ae1a7de"
    )
    assert g7a.G6K_REVEALED_JSON_SHA256 == (
        "c04ae3834048f6d7f1946d806e167c77cab238116e2e06df113512602a9e51f3"
    )
    assert g7a.G6K_MANIFEST_SHA256 == (
        "d6c27d8a0e851c9c832daaa7a6f0d459456603d8bc9af6f6fe19d745a17c37ae"
    )
    assert g7a.G6K_SUMMARY_SHA256 == (
        "4486d789795f11bcafa60ddcb6ff6b51d55559a83340460855840d47c229619d"
    )
    assert g7a.G6K_TREE_SHA256 == (
        "298c0034c7c40e6425dbc62e6e37ffff80a9f8391504fcc7108a0e806b1da1b9"
    )


def test_g7a_freezes_exact_revealed_translation_efforts():
    assert dict(g7a.TRANSLATION_EFFORT_BY_MODEL) == {
        "deepseek/deepseek-v4.1-flash": "low",
        "z-ai/glm-5.3-flash": "high",
        "qwen/qwen3.8-max-0902": "medium",
        "google/gemini-3.8-flash": "low",
        "tencent/hy3-paid": None,
        "xiaomi/mimo-v2.5-pro": None,
    }

    policies = g7a.build_stage1_model_policies()
    assert tuple(policy.model_id for policy in policies) == BENCHMARK_MODEL_IDS

    for policy in policies:
        translation = policy.binding_for(LIFECYCLE_OPERATION_TRANSLATION)
        assert translation.model_id == policy.model_id
        assert translation.effort == g7a.TRANSLATION_EFFORT_BY_MODEL[policy.model_id]
        assert translation.status == "frozen_from_g6k"


def test_g7a_does_not_guess_auxiliary_reasoning():
    policies = g7a.build_stage1_model_policies()

    for policy in policies:
        assert len(policy.operations) == len(PURE_RUN_LIFECYCLE_OPERATIONS)
        assert {item.operation for item in policy.operations} == set(
            PURE_RUN_LIFECYCLE_OPERATIONS
        )

        for operation in g7a.AUXILIARY_OPERATIONS:
            binding = policy.binding_for(operation)
            assert binding.model_id == policy.model_id
            assert binding.effort is None
            assert binding.status == "unresolved_blocks_paid_stage1"


def test_g7a_builds_twelve_independent_empty_s0_stage1_runs():
    identity = g7a.g7a_readiness_identity()

    assert identity["stage1"]["run_count"] == 12
    assert len(identity["runs"]) == 12

    pairs = {
        (row["model_id"], row["novel_id"])
        for row in identity["runs"]
    }
    assert len(pairs) == 12

    assert len({row["run_sha256"] for row in identity["runs"]}) == 12
    assert len({row["s0_identity_sha256"] for row in identity["runs"]}) == 12

    for row in identity["runs"]:
        assert row["stage1_chapters"] == list(range(1, 31))
        assert row["stage1_audit_checkpoints"] == [25]
        assert row["causal_sequential"] is True
        assert row["same_model_pure_run_lifecycle"] is True
        assert row["independent_model_state"] is True


def test_g7a_source_bindings_are_exact_and_cover_stage1():
    bindings = dict(g7a.SOURCE_BINDINGS)

    assert bindings["ace_evolution"]["source_sha256"] == (
        "b43ab411a4e33a616a0353a8f6c8b4682c75d36dc96090915c91c4fc0d9dbc1f"
    )
    assert bindings["ace_evolution"]["chapter_library_sha256"] == (
        "5eb324aaedebf81f60bf00facad7afe830d3201d39d837973ccf82caaee8e6a3"
    )
    assert bindings["ace_evolution"]["chapter_count"] == 1214
    assert bindings["ace_evolution"]["audit_interval"] == 25

    assert bindings["rogue_first_prince"]["source_sha256"] == (
        "ce939a2167c0eb11700bd1b5a0f463c891da232d1bbe84265e2c5e718714015e"
    )
    assert bindings["rogue_first_prince"]["chapter_library_sha256"] == (
        "39d2c0e63627d122eb2b3d5a46586dcba87e9ba0f69e9955b1ba735d571ff63d"
    )
    assert bindings["rogue_first_prince"]["chapter_count"] == 350
    assert bindings["rogue_first_prince"]["audit_interval"] == 25

    for descriptor in g7a.authoritative_source_descriptors():
        assert descriptor.chapter_count >= 60
        assert len(descriptor.source_sha256) == 64


def test_g7a_all_six_models_start_and_finalist_selection_is_not_performed():
    identity = g7a.g7a_readiness_identity()

    assert identity["stage1"]["all_six_models_start_stage1"] is True
    assert identity["stage1"]["model_ranking_or_elimination_performed"] is False
    assert identity["stage1"]["finalist_continuation_authorized"] is False

    model_ids = {row["model_id"] for row in identity["runs"]}
    assert model_ids == set(BENCHMARK_MODEL_IDS)


def test_g7a_paid_stage1_is_explicitly_blocked():
    identity = g7a.g7a_readiness_identity()

    assert identity["stage1"]["provider_calls_authorized"] is False
    assert identity["execution_gate"]["status"] == "blocked"
    assert (
        identity["execution_gate"]["reason"]
        == "auxiliary_lifecycle_reasoning_not_frozen"
    )
    assert len(identity["execution_gate"]["required_before_paid_stage1"]) >= 8

    g7a.assert_paid_stage1_blocked()


def test_g7a_readiness_hash_is_stable_for_rebuild():
    first = g7a.g7a_readiness_identity()
    second = g7a.g7a_readiness_identity()

    assert first == second
    assert len(g7a.G7A_READINESS_SHA256) == 64
