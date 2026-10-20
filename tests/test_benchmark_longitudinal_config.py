import json

import pytest

import benchmark_longitudinal_config as config_module
from benchmark_longitudinal_config import (
    CALIBRATION_REASONING_CANDIDATES,
    DEFAULT_BENCHMARK_CONFIGURATION,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_TRANSLATION,
    PROVIDER_BASE_URL,
    REASONING_CAPABILITY_UNVERIFIED,
    REASONING_CAPABILITY_VERIFIED,
    BenchmarkConfiguration,
    BenchmarkConfigurationError,
    BenchmarkModel,
    BenchmarkNovelSource,
    ReasoningCandidate,
    UnsupportedReasoningConfiguration,
    build_benchmark_roster,
    build_pure_run_lifecycle_policy,
    resolve_reasoning_candidate,
    validate_benchmark_configuration,
)


def _configuration_kwargs(**overrides):
    kwargs = dict(
        benchmark_id="unit-test",
        models=(BenchmarkModel("model-a"),),
        novels=(BenchmarkNovelSource("novel-a", "zh"),),
        stage1_target_chapter=30,
        finalist_target_chapter=60,
        audit_interval=25,
        provider_base_url=PROVIDER_BASE_URL,
        chat_completions_path="/chat/completions",
        reasoning_candidates=CALIBRATION_REASONING_CANDIDATES,
    )
    kwargs.update(overrides)
    return kwargs


def _verified_configuration():
    return BenchmarkConfiguration(
        **_configuration_kwargs(
            reasoning_candidates=(
                ReasoningCandidate("low", REASONING_CAPABILITY_VERIFIED),
                ReasoningCandidate("medium", REASONING_CAPABILITY_VERIFIED),
            ),
        )
    )


def test_exact_frozen_experiment_id():
    assert config_module.BENCHMARK_ID == "goat-longitudinal-cjk-v1"
    assert DEFAULT_BENCHMARK_CONFIGURATION.benchmark_id == "goat-longitudinal-cjk-v1"
    assert config_module.BENCHMARK_ID != "goat-longitudinal-v1"


def test_exact_six_model_roster():
    assert DEFAULT_BENCHMARK_CONFIGURATION.model_ids == (
        "deepseek/deepseek-v4.1-flash",
        "z-ai/glm-5.3-flash",
        "qwen/qwen3.8-max-0902",
        "google/gemini-3.8-flash",
        "tencent/hy3-paid",
        "xiaomi/mimo-v2.5-pro",
    )
    assert len(DEFAULT_BENCHMARK_CONFIGURATION.models) == 6


def test_two_planned_novels():
    assert DEFAULT_BENCHMARK_CONFIGURATION.novel_ids == (
        "ace_evolution",
        "rogue_first_prince",
    )
    novels = {novel.novel_id: novel for novel in DEFAULT_BENCHMARK_CONFIGURATION.novels}
    assert novels["ace_evolution"].source_language == "zh"
    assert novels["rogue_first_prince"].source_language == "ko"
    assert novels["ace_evolution"].target_language == "en"
    assert novels["rogue_first_prince"].target_language == "en"


def test_targets_urls_and_interval():
    configuration = DEFAULT_BENCHMARK_CONFIGURATION
    assert configuration.stage1_target_chapter == 30
    assert configuration.finalist_target_chapter == 60
    assert configuration.audit_interval == 25
    assert configuration.provider_base_url == "https://api.commandcode.ai/provider/v1"
    assert configuration.chat_completions_path == "/chat/completions"
    assert (
        configuration.chat_completions_url
        == "https://api.commandcode.ai/provider/v1/chat/completions"
    )


def test_duplicate_model_ids_rejected():
    with pytest.raises(BenchmarkConfigurationError, match="duplicate benchmark model id"):
        build_benchmark_roster(["model-a", "model-a"])
    with pytest.raises(BenchmarkConfigurationError, match="duplicate benchmark model"):
        BenchmarkConfiguration(
            **_configuration_kwargs(
                models=(BenchmarkModel("model-a"), BenchmarkModel("model-a")),
            )
        )


def test_duplicate_novel_ids_rejected():
    with pytest.raises(BenchmarkConfigurationError, match="duplicate benchmark novel"):
        BenchmarkConfiguration(
            **_configuration_kwargs(
                novels=(
                    BenchmarkNovelSource("novel-a", "zh"),
                    BenchmarkNovelSource("novel-a", "ko"),
                ),
            )
        )


def test_reasoning_candidates_are_unverified_not_assumed():
    levels = {candidate.level: candidate.capability for candidate in CALIBRATION_REASONING_CANDIDATES}
    assert levels == {"low": REASONING_CAPABILITY_UNVERIFIED, "medium": REASONING_CAPABILITY_UNVERIFIED}
    assert DEFAULT_BENCHMARK_CONFIGURATION.reasoning_candidates == CALIBRATION_REASONING_CANDIDATES


def test_unknown_reasoning_level_never_falls_back():
    with pytest.raises(UnsupportedReasoningConfiguration, match="never silently fall back"):
        resolve_reasoning_candidate(DEFAULT_BENCHMARK_CONFIGURATION, "garbage")
    with pytest.raises(UnsupportedReasoningConfiguration):
        resolve_reasoning_candidate(DEFAULT_BENCHMARK_CONFIGURATION, "")


def test_resolve_reasoning_candidate_preserves_exact_level():
    candidate = resolve_reasoning_candidate(DEFAULT_BENCHMARK_CONFIGURATION, "low")
    assert candidate.level == "low"
    assert candidate.level != "medium"


@pytest.mark.parametrize("level", ("low", "medium"))
def test_unverified_reasoning_cannot_be_bound(level):
    reasoning_by_operation = {
        operation: level for operation in config_module.PURE_RUN_LIFECYCLE_OPERATIONS
    }
    with pytest.raises(UnsupportedReasoningConfiguration):
        build_pure_run_lifecycle_policy(
            DEFAULT_BENCHMARK_CONFIGURATION,
            "z-ai/glm-5.3-flash",
            reasoning_by_operation,
        )


def test_same_model_is_fixed_but_reasoning_differs_by_operation():
    configuration = _verified_configuration()
    policy = build_pure_run_lifecycle_policy(
        configuration,
        "model-a",
        {
            LIFECYCLE_OPERATION_TRANSLATION: "low",
            "state_extraction": "low",
            "glossary_review": "low",
            LIFECYCLE_OPERATION_PERIODIC_AUDIT: "medium",
            "state_json_repair": "low",
            "terminology_repair": "low",
        },
    )
    # One model owns every provider-assisted operation.
    for operation in config_module.PURE_RUN_LIFECYCLE_OPERATIONS:
        assert policy.model_for(operation) == "model-a"
    # Reasoning effort is independently frozen per operation.
    assert policy.reasoning_for(LIFECYCLE_OPERATION_TRANSLATION) == "low"
    assert policy.reasoning_for(LIFECYCLE_OPERATION_PERIODIC_AUDIT) == "medium"
    with pytest.raises(BenchmarkConfigurationError, match="unknown provider-assisted"):
        policy.reasoning_for("not-a-lifecycle-operation")


def test_missing_operation_fails_closed_without_fallback():
    configuration = _verified_configuration()
    with pytest.raises(BenchmarkConfigurationError, match="no silent fallback"):
        build_pure_run_lifecycle_policy(
            configuration,
            "model-a",
            {LIFECYCLE_OPERATION_TRANSLATION: "low"},
        )
    with pytest.raises(BenchmarkConfigurationError, match="unknown provider-assisted"):
        build_pure_run_lifecycle_policy(
            configuration,
            "model-a",
            {
                **{op: "low" for op in config_module.PURE_RUN_LIFECYCLE_OPERATIONS},
                "made_up_operation": "low",
            },
        )


def test_same_model_rule_is_bound_into_the_configuration_identity():
    identity = DEFAULT_BENCHMARK_CONFIGURATION.identity()
    assert identity["same_model_all_provider_operations"] is True
    assert tuple(identity["lifecycle_operations"]) == config_module.PURE_RUN_LIFECYCLE_OPERATIONS
    with pytest.raises(BenchmarkConfigurationError, match="same_model_all_provider_operations"):
        BenchmarkConfiguration(
            **_configuration_kwargs(same_model_all_provider_operations=False)
        )
    with pytest.raises(BenchmarkConfigurationError, match="missing required entries"):
        BenchmarkConfiguration(
            **_configuration_kwargs(lifecycle_operations=(LIFECYCLE_OPERATION_TRANSLATION,))
        )


def test_configuration_identity_is_deterministic_and_has_no_credentials():
    first = DEFAULT_BENCHMARK_CONFIGURATION.identity_sha256
    second = BenchmarkConfiguration(
        **_configuration_kwargs(
            benchmark_id=config_module.BENCHMARK_ID,
            models=build_benchmark_roster(config_module.BENCHMARK_MODEL_IDS),
            novels=DEFAULT_BENCHMARK_CONFIGURATION.novels,
        )
    ).identity_sha256
    assert first == second
    assert len(first) == 64

    rendered = json.dumps(DEFAULT_BENCHMARK_CONFIGURATION.identity()).lower()
    for forbidden in ("api_key", "apikey", "secret", "password", "token", "authorization"):
        assert forbidden not in rendered
    assert not hasattr(config_module, "API_KEY")
    assert not hasattr(config_module, "PROVIDER_API_KEY")


def test_validate_returns_the_configuration_and_rejects_arbitrary_urls():
    assert (
        validate_benchmark_configuration(DEFAULT_BENCHMARK_CONFIGURATION)
        is DEFAULT_BENCHMARK_CONFIGURATION
    )
    for bad_url in ("http://insecure.example", "https://evil.example/provider/v1"):
        with pytest.raises(BenchmarkConfigurationError, match="exactly the Command Code Provider"):
            BenchmarkConfiguration(**_configuration_kwargs(provider_base_url=bad_url))
