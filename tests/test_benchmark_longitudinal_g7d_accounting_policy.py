from datetime import datetime, timezone

import pytest

import benchmark_longitudinal_g7d_accounting_policy as g7d
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS


EXPECTED_RATES = {
    "deepseek/deepseek-v4.1-flash": {
        "off_peak": (0.15, 0.60, 0.003),
        "peak": (0.30, 1.20, 0.006),
    },
    "z-ai/glm-5.3-flash": {
        "flat": (0.15, 0.50, 0.03),
    },
    "qwen/qwen3.8-max-0902": {
        "flat": (2.00, 6.00, 0.25),
    },
    "google/gemini-3.8-flash": {
        "flat": (1.50, 7.50, 0.15),
    },
    "tencent/hy3-paid": {
        "flat": (0.14, 0.58, 0.035),
    },
    "xiaomi/mimo-v2.5-pro": {
        "flat": (0.435, 0.87, 0.0036),
    },
}


def _usage(
    *,
    prompt=1000,
    completion=100,
    cached=None,
    exposed=False,
):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cached_tokens_exposed": exposed,
    }


def test_g7d_is_bound_to_successful_g7c_and_stays_zero_provider():
    assert g7d.EXPECTED_G7C_INTEGRATION_SHA256 == (
        "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
    )

    identity = g7d.g7d_accounting_identity()

    assert identity["predecessor"]["g7c_integration_sha256"] == (
        g7d.EXPECTED_G7C_INTEGRATION_SHA256
    )
    assert identity["provider_calls"] == 0
    assert identity["stage1"]["provider_calls_authorized"] is False
    assert (
        identity["stage1"]["model_ranking_or_elimination_performed"]
        is False
    )
    assert identity["stage1"]["finalist_continuation_authorized"] is False


def test_g7d_resolves_only_accounting_and_leaves_paid_authorization_blocked():
    identity = g7d.g7d_accounting_identity()

    assert identity["resolved_by_g7d"] == [
        "freeze_command_code_provider_accounting_policy"
    ]
    assert identity["execution_gate"] == {
        "status": "blocked",
        "reason": "stage1_paid_execution_authorization_not_frozen",
        "required_before_paid_stage1": [
            "freeze_stage1_paid_execution_authorization_contract"
        ],
    }

    g7d.assert_paid_stage1_blocked()


def test_ratecard_roster_and_public_rates_are_frozen_exactly():
    identity = g7d.g7d_accounting_identity()

    assert [row["benchmark_model_id"] for row in identity["models"]] == list(
        BENCHMARK_MODEL_IDS
    )

    for row in identity["models"]:
        model_id = row["benchmark_model_id"]
        assert model_id in EXPECTED_RATES
        observed = {
            period: (
                float(rates["input"]),
                float(rates["output"]),
                float(rates["cache_read"]),
            )
            for period, rates in row["periods"].items()
        }
        assert observed == EXPECTED_RATES[model_id]
        assert row["cache_write_rate"] is None
        assert row["cache_write_usage_accounted"] is False


def test_pricing_sources_are_command_code_public_pages():
    identity = g7d.g7d_accounting_identity()

    assert identity["pricing_capture_date"] == "2026-09-19"
    assert identity["pricing_version"] == (
        "command-code-provider-public-ratecard-2026-09-19-v1"
    )
    assert identity["pricing_unit"] == "usd_credit_equivalent_per_1m_tokens"
    assert identity["pricing_sources"]
    assert all(
        source.startswith("https://commandcode.ai/")
        for source in identity["pricing_sources"]
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 21, 0, 59, tzinfo=timezone.utc), "off_peak"),
        (datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc), "peak"),
        (datetime(2026, 9, 21, 3, 59, tzinfo=timezone.utc), "peak"),
        (datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc), "off_peak"),
        (datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc), "peak"),
        (datetime(2026, 9, 21, 9, 59, tzinfo=timezone.utc), "peak"),
        (datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc), "off_peak"),
        (datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc), "off_peak"),
    ],
)
def test_deepseek_peak_offpeak_schedule(moment, expected):
    snapshot = g7d.pricing_snapshot(
        "deepseek/deepseek-v4.1-flash",
        moment,
    )
    assert snapshot["pricing_period"] == expected

    rates = EXPECTED_RATES["deepseek/deepseek-v4.1-flash"][expected]
    assert snapshot["cache_miss_rate_per_million"] == rates[0]
    assert snapshot["output_rate_per_million"] == rates[1]
    assert snapshot["cache_hit_rate_per_million"] == rates[2]


def test_deepseek_exact_known_cache_cost_doubles_at_peak():
    usage = _usage(
        prompt=1000,
        completion=200,
        cached=400,
        exposed=True,
    )

    off_peak = g7d.account_provider_usage(
        "deepseek/deepseek-v4.1-flash",
        usage,
        datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
    )
    peak = g7d.account_provider_usage(
        "deepseek/deepseek-v4.1-flash",
        usage,
        datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
    )

    assert off_peak["cache_hit_tokens"] == 400
    assert off_peak["cache_miss_tokens"] == 600
    assert off_peak["cost_usd"] == pytest.approx(0.0002112)
    assert off_peak["cost_exact_under_frozen_ratecard"] is True
    assert off_peak["cache_accounting_mode"] == "reported_cache_exact"
    assert off_peak["cost_semantics"] == "frozen_ratecard_exact"

    assert peak["cost_usd"] == pytest.approx(0.0004224)
    assert peak["cost_usd"] == pytest.approx(off_peak["cost_usd"] * 2)


def test_flat_model_exact_known_cache_cost():
    result = g7d.account_provider_usage(
        "z-ai/glm-5.3-flash",
        _usage(prompt=1000, completion=100, cached=200, exposed=True),
        "2026-09-19T12:00:00+00:00",
    )

    assert result["pricing_period"] == "flat"
    assert result["cache_hit_tokens"] == 200
    assert result["cache_miss_tokens"] == 800
    assert result["cost_usd"] == pytest.approx(0.000176)
    assert result["rate_correct_cost_usd"] == result["cost_usd"]
    assert result["provider_invoice_claimed"] is False


def test_unexposed_cache_is_conservative_upper_bound_not_fake_exactness():
    result = g7d.account_provider_usage(
        "z-ai/glm-5.3-flash",
        _usage(prompt=1000, completion=100, cached=None, exposed=False),
        "2026-09-19T12:00:00Z",
    )

    assert result["cache_hit_tokens"] == 0
    assert result["cache_miss_tokens"] == 1000
    assert result["cost_usd"] == pytest.approx(0.0002)
    assert result["cost_exact_under_frozen_ratecard"] is False
    assert result["cache_accounting_mode"] == (
        "cache_unexposed_conservative_upper_bound"
    )
    assert result["cost_semantics"] == "frozen_ratecard_upper_bound"


def test_unknown_cache_upper_bound_is_not_less_than_equivalent_known_cache_cost():
    unknown = g7d.account_provider_usage(
        "xiaomi/mimo-v2.5-pro",
        _usage(prompt=10000, completion=500, cached=None, exposed=False),
        "2026-09-19T12:00:00Z",
    )
    known = g7d.account_provider_usage(
        "xiaomi/mimo-v2.5-pro",
        _usage(prompt=10000, completion=500, cached=8000, exposed=True),
        "2026-09-19T12:00:00Z",
    )

    assert unknown["cost_usd"] >= known["cost_usd"]
    assert unknown["cost_exact_under_frozen_ratecard"] is False
    assert known["cost_exact_under_frozen_ratecard"] is True


@pytest.mark.parametrize(
    "model_id",
    list(EXPECTED_RATES),
)
def test_each_model_can_produce_stage1_accounting(model_id):
    result = g7d.account_provider_usage(
        model_id,
        _usage(prompt=1234, completion=321, cached=234, exposed=True),
        "2026-09-19T12:34:56+00:00",
    )

    assert result["prompt_tokens"] == 1234
    assert result["completion_tokens"] == 321
    assert result["cache_hit_tokens"] == 234
    assert result["cache_miss_tokens"] == 1000
    assert result["cost_usd"] >= 0
    assert result["pricing_version"] == g7d.PRICING_VERSION
    assert result["provider_invoice_claimed"] is False


def test_cache_count_cannot_exceed_prompt():
    with pytest.raises(g7d.G7DAccountingError, match="cannot exceed"):
        g7d.account_provider_usage(
            "google/gemini-3.8-flash",
            _usage(prompt=10, completion=2, cached=11, exposed=True),
            "2026-09-19T12:00:00Z",
        )


def test_unexposed_cache_cannot_smuggle_nonzero_cache_count():
    with pytest.raises(g7d.G7DAccountingError, match="absent"):
        g7d.account_provider_usage(
            "tencent/hy3-paid",
            _usage(prompt=10, completion=2, cached=5, exposed=False),
            "2026-09-19T12:00:00Z",
        )


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": None, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": None},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": -1},
    ],
)
def test_missing_or_invalid_paid_usage_fails_closed(usage):
    with pytest.raises(g7d.G7DAccountingError):
        g7d.account_provider_usage(
            "qwen/qwen3.8-max-0902",
            usage,
            "2026-09-19T12:00:00Z",
        )


def test_non_utc_request_time_fails_closed():
    with pytest.raises(g7d.G7DAccountingError, match="must use UTC"):
        g7d.pricing_snapshot(
            "deepseek/deepseek-v4.1-flash",
            "2026-09-19T12:00:00-05:00",
        )


def test_unknown_model_fails_closed():
    with pytest.raises(g7d.G7DAccountingError, match="unknown benchmark"):
        g7d.pricing_snapshot(
            "not/a-model",
            "2026-09-19T12:00:00Z",
        )
