from datetime import datetime, timezone

import pytest

from translation_model_config import (
    DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC,
    DEFAULT_TRANSLATION_MODEL,
    MODEL_CONFIGS,
    TranslationModelRoutingError,
    assert_translation_request_allowed,
    pending_model_compatibility,
    pending_translation_model_record,
    pricing_rates_for_model,
    resolve_translation_model,
    validate_provider_response_model,
)


BEFORE_PRO_CUTOFF = datetime(2026, 9, 14, 3, 59, 59, tzinfo=timezone.utc)
AT_PRO_CUTOFF = datetime(2026, 9, 14, 4, 0, 0, tzinfo=timezone.utc)
AFTER_PRO_CUTOFF = datetime(2026, 9, 14, 5, 0, 0, tzinfo=timezone.utc)


def test_pro_remains_the_default_translation_model():
    config = resolve_translation_model()
    assert DEFAULT_TRANSLATION_MODEL == "deepseek-v4-pro"
    assert config.model == "deepseek-v4-pro"
    assert config.key == "pro"
    assert config.version == "DeepSeek-V4-Pro-0813"
    assert config.thinking == "disabled"
    assert config.max_tokens == 12000
    assert config.request_cutoff_utc == DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC


def test_legacy_flash_identity_remains_resolvable_for_recovery():
    config = resolve_translation_model("flash")
    assert config.model == "deepseek-v4-flash"
    assert config.key == "flash"
    assert config.version == "DeepSeek-V4-Flash-0731"
    assert config.new_requests_allowed is False
    assert resolve_translation_model().model == "deepseek-v4-pro"


@pytest.mark.parametrize(
    "selection",
    ("v41-flash", "v4.1-flash", "deepseek-flash", "deepseek-v4.1-flash"),
)
def test_v41_flash_is_explicitly_selectable_without_changing_default(selection):
    config = resolve_translation_model(selection)
    assert config.model == "deepseek-flash"
    assert config.key == "v41-flash"
    assert config.version == "DeepSeek-V4.1-Flash"
    assert config.thinking == "disabled"
    assert config.max_tokens == 12000
    assert config.new_requests_allowed is True
    assert config.request_cutoff_utc == ""
    assert resolve_translation_model().model == "deepseek-v4-pro"


@pytest.mark.parametrize(
    "selection, expected",
    [
        ("pro", "deepseek-v4-pro"),
        ("v4-pro", "deepseek-v4-pro"),
        ("DEEPSEEK-V4-PRO", "deepseek-v4-pro"),
        ("flash", "deepseek-v4-flash"),
        ("v4-flash", "deepseek-v4-flash"),
        ("DEEPSEEK-V4-FLASH", "deepseek-v4-flash"),
        ("v41-flash", "deepseek-flash"),
        ("v4.1-flash", "deepseek-flash"),
        ("DEEPSEEK-FLASH", "deepseek-flash"),
        ("DEEPSEEK-V4.1-FLASH", "deepseek-flash"),
    ],
)
def test_model_aliases_resolve_deterministically(selection, expected):
    assert resolve_translation_model(selection).model == expected


def test_unknown_or_empty_model_selection_fails_closed():
    with pytest.raises(ValueError, match="unsupported translation model"):
        resolve_translation_model("some-other-model")
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_translation_model("")


@pytest.mark.parametrize(
    "selection, period, expected",
    [
        ("pro", "off_peak", {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98}),
        ("pro", "peak", {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96}),
        ("flash", "off_peak", {"cache_hit": 0.007, "cache_miss": 0.22, "output": 0.66}),
        ("flash", "peak", {"cache_hit": 0.014, "cache_miss": 0.44, "output": 1.32}),
        ("v41-flash", "off_peak", {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60}),
        ("v41-flash", "peak", {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20}),
    ],
)
def test_official_model_specific_peak_and_off_peak_rates(selection, period, expected):
    assert pricing_rates_for_model(selection, period) == expected


def test_invalid_pricing_period_fails_closed():
    with pytest.raises(ValueError, match="peak"):
        pricing_rates_for_model("pro", "weekend")


def test_model_registry_contains_route_safe_v41_flash_and_legacy_identity():
    assert set(MODEL_CONFIGS) == {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
    }


def test_pending_model_record_preserves_legacy_flash_identity_exactly():
    record = pending_translation_model_record("flash")
    assert record == {
        "translation_model": "deepseek-v4-flash",
        "translation_model_key": "flash",
        "translation_model_version": "DeepSeek-V4-Flash-0731",
        "translation_thinking": "disabled",
        "translation_max_tokens": 12000,
        "translation_pricing_version": "deepseek-v4-flash-0731-peak-offpeak",
    }


def test_pending_model_record_pins_v41_flash_api_identity_exactly():
    record = pending_translation_model_record("v41-flash")
    assert record == {
        "translation_model": "deepseek-flash",
        "translation_model_key": "v41-flash",
        "translation_model_version": "DeepSeek-V4.1-Flash",
        "translation_thinking": "disabled",
        "translation_max_tokens": 12000,
        "translation_pricing_version": "deepseek-v4.1-flash-2026-09-10-peak-offpeak",
    }


def test_pending_model_compatibility_accepts_exact_model_and_reports_mismatch():
    flash_meta = pending_translation_model_record("flash")
    assert pending_model_compatibility(flash_meta, "flash")["status"] == "match"
    mismatch = pending_model_compatibility(flash_meta, "pro")
    assert mismatch == {
        "status": "mismatch",
        "selected_model": "deepseek-v4-pro",
        "pending_model": "deepseek-v4-flash",
    }


def test_legacy_pending_metadata_is_preserved_as_unpinned_instead_of_rejected():
    result = pending_model_compatibility({"chapter": 123, "stage": "translated"}, "pro")
    assert result == {
        "status": "legacy_unpinned",
        "selected_model": "deepseek-v4-pro",
        "pending_model": "",
    }


def test_unknown_model_in_pending_metadata_is_surfaced_without_guessing():
    result = pending_model_compatibility(
        {"translation_model": "deepseek-future-model"}, "pro"
    )
    assert result == {
        "status": "unknown_pending_model",
        "selected_model": "deepseek-v4-pro",
        "pending_model": "deepseek-future-model",
    }


def test_pro_request_is_allowed_before_announced_route_cutoff():
    config = assert_translation_request_allowed("pro", moment=BEFORE_PRO_CUTOFF)
    assert config.model == "deepseek-v4-pro"


def test_pro_request_is_blocked_at_and_after_announced_route_cutoff():
    for moment in (AT_PRO_CUTOFF, AFTER_PRO_CUTOFF):
        with pytest.raises(TranslationModelRoutingError, match="alias routing"):
            assert_translation_request_allowed("pro", moment=moment)


def test_legacy_flash_alias_is_never_allowed_for_new_paid_requests():
    with pytest.raises(TranslationModelRoutingError, match="legacy model"):
        assert_translation_request_allowed("deepseek-v4-flash", moment=BEFORE_PRO_CUTOFF)


def test_v41_flash_remains_route_safe_after_pro_cutoff():
    config = assert_translation_request_allowed("v41-flash", moment=AFTER_PRO_CUTOFF)
    assert config.model == "deepseek-flash"


def test_request_moment_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        assert_translation_request_allowed(
            "pro",
            moment=datetime(2026, 9, 13, 23, 0, 0),
        )


def test_provider_response_model_requires_exact_api_identity():
    assert validate_provider_response_model("pro", "deepseek-v4-pro") == "deepseek-v4-pro"
    assert validate_provider_response_model("v41-flash", "deepseek-flash") == "deepseek-flash"

    with pytest.raises(TranslationModelRoutingError, match="identity mismatch"):
        validate_provider_response_model("pro", "deepseek-flash")

    with pytest.raises(TranslationModelRoutingError, match="did not report"):
        validate_provider_response_model("pro", "")
