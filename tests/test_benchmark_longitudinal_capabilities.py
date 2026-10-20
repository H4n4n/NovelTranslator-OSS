import json

import pytest

import benchmark_longitudinal_capabilities as caps
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS


def test_capability_roster_matches_frozen_benchmark_roster_exactly():
    assert tuple(
        profile.benchmark_model_id for profile in caps.CAPABILITY_PROFILES
    ) == BENCHMARK_MODEL_IDS
    assert len(caps.CAPABILITY_PROFILES) == 6


def test_command_code_static_evidence_anchor_is_frozen():
    assert caps.CAPABILITY_EVIDENCE_PHASE == "G5B0"
    assert caps.COMMAND_CODE_VERSION == "1.54.2"
    assert (
        caps.COMMAND_CODE_CLI_SHA256
        == "551a17d143fd6687e4dc9085bf5f2e601a01f9f95faa6528a1c1011ff8a54828"
    )
    assert caps.REASONING_WIRE_FIELD == "reasoning_effort"
    assert (
        caps.REASONING_WIRE_STATIC_STATUS
        == "cli_discovered_direct_provider_unverified"
    )


def test_qwen_has_distinct_logical_and_provider_catalog_identity():
    qwen = caps.capability_profile("qwen/qwen3.8-max-0902")
    assert qwen.benchmark_model_id == "qwen/qwen3.8-max-0902"
    assert qwen.provider_catalog_model_id == "Qwen/Qwen3.8-Max-0902"
    assert caps.provider_model_id(qwen.benchmark_model_id) == qwen.provider_catalog_model_id

    for profile in caps.CAPABILITY_PROFILES:
        if profile.benchmark_model_id != qwen.benchmark_model_id:
            assert profile.provider_catalog_model_id == profile.benchmark_model_id


def test_exact_cli_effort_map_and_translation_calibration_pairs():
    expected = {
        "deepseek/deepseek-v4.1-flash": (
            ("low", "high", "max"),
            ("low", "high"),
        ),
        "z-ai/glm-5.3-flash": (
            ("low", "high", "max"),
            ("low", "high"),
        ),
        "qwen/qwen3.8-max-0902": (
            ("low", "medium", "xhigh"),
            ("low", "medium"),
        ),
        "google/gemini-3.8-flash": (
            ("low", "medium", "high"),
            ("low", "medium"),
        ),
        "tencent/hy3-paid": ((), ()),
        "xiaomi/mimo-v2.5-pro": ((), ()),
    }

    observed = {
        profile.benchmark_model_id: (
            profile.cli_supported_efforts,
            profile.translation_calibration_efforts,
        )
        for profile in caps.CAPABILITY_PROFILES
    }

    assert observed == expected


def test_tencent_and_mimo_are_default_effort_only_for_g6():
    for model_id in ("tencent/hy3-paid", "xiaomi/mimo-v2.5-pro"):
        profile = caps.capability_profile(model_id)
        assert profile.cli_supported_efforts == ()
        assert profile.translation_calibration_efforts == ()
        assert profile.uses_default_effort_for_translation_calibration is True
        assert profile.has_selectable_cli_reasoning_effort is False


def test_adjustable_models_have_exactly_two_calibration_efforts():
    adjustable = [
        profile
        for profile in caps.CAPABILITY_PROFILES
        if profile.has_selectable_cli_reasoning_effort
    ]
    assert len(adjustable) == 4
    for profile in adjustable:
        assert len(profile.translation_calibration_efforts) == 2
        assert set(profile.translation_calibration_efforts).issubset(
            profile.cli_supported_efforts
        )


def test_static_zdr_classification_and_benchmark_policy_are_distinct():
    qwen = caps.capability_profile("qwen/qwen3.8-max-0902")
    assert qwen.cli_known_model_supports_zdr is False

    for profile in caps.CAPABILITY_PROFILES:
        if profile.benchmark_model_id != qwen.benchmark_model_id:
            assert profile.cli_known_model_supports_zdr is True

    assert caps.BENCHMARK_ZDR_ENABLED is False
    assert caps.BENCHMARK_ZDR_POLICY == "disabled_for_cross_model_fairness"
    assert all(case.zdr_enabled is False for case in caps.G5C_CAPABILITY_PROBE_CASES)


def test_direct_provider_capabilities_remain_unverified_after_static_freeze():
    for profile in caps.CAPABILITY_PROFILES:
        assert profile.direct_provider_model_identity_status == "unverified"
        assert profile.direct_provider_reasoning_status == "unverified"
        assert profile.direct_provider_zdr_status == "unverified"


def test_g5c_probe_plan_is_exactly_ten_cases():
    cases = caps.G5C_CAPABILITY_PROBE_CASES
    assert len(cases) == 10
    assert caps.G5C_CAPABILITY_PROBE_CALL_COUNT == 10

    by_model = {}
    for case in cases:
        by_model.setdefault(case.benchmark_model_id, []).append(case.effort)

    assert by_model == {
        "deepseek/deepseek-v4.1-flash": ["low", "high"],
        "z-ai/glm-5.3-flash": ["low", "high"],
        "qwen/qwen3.8-max-0902": ["low", "medium"],
        "google/gemini-3.8-flash": ["low", "medium"],
        "tencent/hy3-paid": [None],
        "xiaomi/mimo-v2.5-pro": [None],
    }


def test_g6_literary_calibration_call_count_is_forty():
    assert caps.G6_DIFFICULT_CHAPTERS_PER_MODEL == 4
    assert caps.G6_PRIMARY_CALIBRATION_CALL_COUNT == 40
    assert caps.G6_PRIMARY_CALIBRATION_CALL_COUNT == 32 + 8


def test_unknown_model_fails_closed_and_never_casefolds():
    with pytest.raises(caps.CapabilityConfigurationError, match="unknown benchmark model"):
        caps.capability_profile("Qwen/Qwen3.8-Max-0902")
    with pytest.raises(caps.CapabilityConfigurationError, match="unknown benchmark model"):
        caps.provider_model_id("not-a-model")


def test_profile_rejects_calibration_not_supported_by_cli_map():
    with pytest.raises(
        caps.CapabilityConfigurationError,
        match="subset of the exact CLI-supported efforts",
    ):
        caps.ModelCapabilityProfile(
            benchmark_model_id="model-a",
            provider_catalog_model_id="model-a",
            cli_supported_efforts=("low", "high"),
            translation_calibration_efforts=("low", "medium"),
            cli_known_model_supports_zdr=True,
        )


def test_profile_rejects_unfrozen_adjustable_or_multiway_calibration():
    with pytest.raises(
        caps.CapabilityConfigurationError,
        match="must freeze a two-level translation calibration pair",
    ):
        caps.ModelCapabilityProfile(
            benchmark_model_id="model-a",
            provider_catalog_model_id="model-a",
            cli_supported_efforts=("low", "high"),
            translation_calibration_efforts=(),
            cli_known_model_supports_zdr=True,
        )

    with pytest.raises(
        caps.CapabilityConfigurationError,
        match="exactly two translation calibration efforts",
    ):
        caps.ModelCapabilityProfile(
            benchmark_model_id="model-a",
            provider_catalog_model_id="model-a",
            cli_supported_efforts=("low", "high", "max"),
            translation_calibration_efforts=("low", "high", "max"),
            cli_known_model_supports_zdr=True,
        )


def test_capability_identity_is_deterministic_and_contains_no_credentials():
    first = caps.CAPABILITY_FREEZE_SHA256
    second = caps._sha256_json(caps.capability_freeze_identity())
    assert first == second
    assert len(first) == 64

    rendered = json.dumps(caps.capability_freeze_identity()).lower()
    for forbidden in ("api_key", "apikey", "secret", "password", "authorization"):
        assert forbidden not in rendered
