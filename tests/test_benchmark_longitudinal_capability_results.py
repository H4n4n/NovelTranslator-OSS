import json

import pytest

import benchmark_longitudinal_capability_results as live
from benchmark_longitudinal_capabilities import (
    CAPABILITY_FREEZE_SHA256,
    CAPABILITY_PROFILES,
)


def test_g5c_result_binds_exact_static_freeze_and_execution_evidence():
    assert (
        CAPABILITY_FREEZE_SHA256
        == "348d864cc7969cf65fb5be3fedda8a28d436cb0f00bcaf3e40d31f1e98dc7176"
    )
    assert live.G5C_EXPECTED_CAPABILITY_FREEZE_SHA256 == CAPABILITY_FREEZE_SHA256
    assert (
        live.G5C_PROBE_PLAN_SHA256
        == "7893943e94a896e60599089813f9e3e23c6a37eec8580066d2fa4fd26028d0ac"
    )
    assert (
        live.G5C_EXECUTION_REVIEW_SHA256
        == "5633728b0378c16fdc232dce5bc6daf462d32f44650f93576f0a303b5e9447f4"
    )
    assert (
        live.G5C_WRAPPER_MANIFEST_SHA256
        == "5731caff6bf9c1a4c3c5efd24d6f87e6af5f94180044d148b89b5570d7c1a019"
    )


def test_g5c_all_ten_requests_were_accepted_without_ambiguity_or_retry():
    assert live.G5C_BOUND_REQUEST_COUNT == 10
    assert live.G5C_ACCEPTED_COUNT == 10
    assert live.G5C_REJECTED_COUNT == 0
    assert live.G5C_AMBIGUOUS_COUNT == 0
    assert live.G5C_AUTOMATIC_RETRY_COUNT == 0


def test_g5c_usage_totals_are_frozen():
    assert live.G5C_PROMPT_TOKENS == 589
    assert live.G5C_COMPLETION_TOKENS == 136
    assert live.G5C_TOTAL_TOKENS == 725
    assert live.G5C_CACHED_TOKENS == 192


def test_live_roster_and_provider_model_ids_match_static_profiles():
    assert tuple(
        item.benchmark_model_id for item in live.LIVE_PROVIDER_CAPABILITIES
    ) == tuple(profile.benchmark_model_id for profile in CAPABILITY_PROFILES)

    expected = {
        "deepseek/deepseek-v4.1-flash": "deepseek/deepseek-v4.1-flash",
        "z-ai/glm-5.3-flash": "z-ai/glm-5.3-flash",
        "qwen/qwen3.8-max-0902": "Qwen/Qwen3.8-Max-0902",
        "google/gemini-3.8-flash": "google/gemini-3.8-flash",
        "tencent/hy3-paid": "tencent/hy3-paid",
        "xiaomi/mimo-v2.5-pro": "xiaomi/mimo-v2.5-pro",
    }

    assert {
        item.benchmark_model_id: item.provider_model_id
        for item in live.LIVE_PROVIDER_CAPABILITIES
    } == expected


def test_exact_live_verified_reasoning_pairs_and_default_only_models():
    expected_efforts = {
        "deepseek/deepseek-v4.1-flash": ("low", "high"),
        "z-ai/glm-5.3-flash": ("low", "high"),
        "qwen/qwen3.8-max-0902": ("low", "medium"),
        "google/gemini-3.8-flash": ("low", "medium"),
        "tencent/hy3-paid": (),
        "xiaomi/mimo-v2.5-pro": (),
    }

    observed = {
        item.benchmark_model_id: item.verified_reasoning_efforts
        for item in live.LIVE_PROVIDER_CAPABILITIES
    }

    assert observed == expected_efforts

    assert live.live_provider_capability(
        "tencent/hy3-paid"
    ).default_effort_request_verified is True
    assert live.live_provider_capability(
        "xiaomi/mimo-v2.5-pro"
    ).default_effort_request_verified is True


def test_verified_provider_reasoning_capabilities_generate_exact_wire():
    for model_id in (
        "deepseek/deepseek-v4.1-flash",
        "z-ai/glm-5.3-flash",
        "qwen/qwen3.8-max-0902",
        "google/gemini-3.8-flash",
    ):
        live_record = live.live_provider_capability(model_id)
        capability = live.verified_reasoning_capability(model_id)

        assert capability is not None
        assert capability.verified is True
        assert capability.model_id == live_record.provider_model_id
        assert capability.levels == live_record.verified_reasoning_efforts

        for level in live_record.verified_reasoning_efforts:
            control = capability.control_for(live_record.provider_model_id, level)
            assert control.as_dict() == {"reasoning_effort": level}


def test_default_only_models_have_no_reasoning_capability():
    assert live.verified_reasoning_capability("tencent/hy3-paid") is None
    assert live.verified_reasoning_capability("xiaomi/mimo-v2.5-pro") is None
    assert live.g6_translation_efforts("tencent/hy3-paid") == (None,)
    assert live.g6_translation_efforts("xiaomi/mimo-v2.5-pro") == (None,)


def test_g6_translation_efforts_are_exactly_live_verified():
    assert live.g6_translation_efforts(
        "deepseek/deepseek-v4.1-flash"
    ) == ("low", "high")
    assert live.g6_translation_efforts(
        "z-ai/glm-5.3-flash"
    ) == ("low", "high")
    assert live.g6_translation_efforts(
        "qwen/qwen3.8-max-0902"
    ) == ("low", "medium")
    assert live.g6_translation_efforts(
        "google/gemini-3.8-flash"
    ) == ("low", "medium")


def test_zdr_was_not_live_tested_and_g6_is_not_authorized_yet():
    assert live.G5C_ZDR_SENT is False
    assert live.G5C_ZDR_LIVE_STATUS == "not_tested"
    assert live.G6_PROVIDER_CALLS_AUTHORIZED is False

    for item in live.LIVE_PROVIDER_CAPABILITIES:
        assert item.zdr_live_status == "not_tested"


def test_unknown_or_alias_model_fails_closed():
    with pytest.raises(live.LiveCapabilityConfigurationError):
        live.live_provider_capability("Qwen/Qwen3.8-Max-0902")

    with pytest.raises(live.LiveCapabilityConfigurationError):
        live.verified_reasoning_capability("not-a-model")


def test_live_result_identity_is_deterministic_and_credential_free():
    identity = live.live_capability_result_identity()
    assert live.G5C_LIVE_RESULT_SHA256 == live._sha256_json(identity)
    assert len(live.G5C_LIVE_RESULT_SHA256) == 64

    rendered = json.dumps(identity).lower()
    for forbidden in (
        "api_key",
        "apikey",
        "secret",
        "password",
        "authorization",
        "bearer",
    ):
        assert forbidden not in rendered
