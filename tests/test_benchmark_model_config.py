from datetime import datetime, timezone

import pytest

from benchmark_model_config import (
    BENCHMARK_MODEL_CONFIGS,
    benchmark_pricing_snapshot,
    economy_pricing_snapshot,
    resolve_benchmark_model,
)
from translation_model_config import MODEL_CONFIGS, resolve_translation_model


PEAK = datetime(2026, 8, 21, 2, tzinfo=timezone.utc)


def test_benchmark_registry_remains_separate_from_production_registry():
    assert set(MODEL_CONFIGS) == {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
    }
    assert "gpt-5.6-luna" not in MODEL_CONFIGS
    with pytest.raises(ValueError, match="unsupported translation model"):
        resolve_translation_model("luna")

    production_flash = resolve_translation_model("deepseek-v4.1-flash")
    benchmark_flash = resolve_benchmark_model("deepseek-v4.1-flash")
    assert production_flash.model == benchmark_flash.model == "deepseek-flash"
    assert production_flash.version == benchmark_flash.version == "DeepSeek-V4.1-Flash"
    assert production_flash.max_tokens == benchmark_flash.max_tokens == 12000


def test_benchmark_default_remains_current_production_model():
    config = resolve_benchmark_model()
    assert config.model == "deepseek-v4-pro"
    assert config.provider == "deepseek"
    assert config.reasoning == "disabled"


@pytest.mark.parametrize("selection", ("luna", "gpt-5.6-luna"))
def test_luna_is_explicitly_selectable_for_benchmark_only(selection):
    config = resolve_benchmark_model(selection)
    assert config.model == "gpt-5.6-luna"
    assert config.provider == "openai"
    assert config.reasoning == "none"
    assert config.max_tokens == 12000


@pytest.mark.parametrize(
    "selection",
    (
        "flash",
        "v4.1-flash",
        "v41-flash",
        "deepseek-flash",
        "deepseek-v4.1-flash",
    ),
)
def test_deepseek_v41_flash_is_explicitly_selectable_for_benchmark(selection):
    config = resolve_benchmark_model(selection)
    assert config.key == "deepseek-v41-flash"
    assert config.model == "deepseek-flash"
    assert config.version == "DeepSeek-V4.1-Flash"
    assert config.provider == "deepseek"
    assert config.reasoning == "disabled"
    assert config.max_tokens == 12000


def test_unknown_benchmark_model_fails_closed():
    with pytest.raises(ValueError, match="unsupported benchmark model"):
        resolve_benchmark_model("future-model")


def test_luna_standard_and_batch_prices_are_exact():
    config = resolve_benchmark_model("luna")
    synchronous = benchmark_pricing_snapshot(config, PEAK)
    economy = economy_pricing_snapshot(config, PEAK)

    assert synchronous == {
        "pricing_version": "openai-gpt-5.6-luna-standard-0.20-0.02-1.20",
        "pricing_period": "standard",
        "pricing_mode": "synchronous",
        "request_started_utc": PEAK.isoformat(),
        "cache_hit_rate_per_million": 0.02,
        "cache_miss_rate_per_million": 0.20,
        "output_rate_per_million": 1.20,
    }
    assert economy == {
        "pricing_version": "openai-gpt-5.6-luna-batch-0.10-0.01-0.60",
        "pricing_period": "batch",
        "pricing_mode": "batch_50_percent_projection",
        "request_started_utc": PEAK.isoformat(),
        "cache_hit_rate_per_million": 0.01,
        "cache_miss_rate_per_million": 0.10,
        "output_rate_per_million": 0.60,
    }


def test_deepseek_economy_projection_is_off_peak_even_during_peak_request():
    config = resolve_benchmark_model("deepseek-pro")
    synchronous = benchmark_pricing_snapshot(config, PEAK)
    economy = economy_pricing_snapshot(config, PEAK)

    assert synchronous["pricing_period"] == "peak"
    assert synchronous["cache_miss_rate_per_million"] == 1.32
    assert synchronous["output_rate_per_million"] == 3.96
    assert economy["pricing_period"] == "off_peak"
    assert economy["pricing_mode"] == "off_peak_projection"
    assert economy["cache_miss_rate_per_million"] == 0.66
    assert economy["output_rate_per_million"] == 1.98


def test_deepseek_v41_flash_peak_and_off_peak_prices_are_exact():
    config = resolve_benchmark_model("deepseek-v4.1-flash")
    synchronous = benchmark_pricing_snapshot(config, PEAK)
    economy = economy_pricing_snapshot(config, PEAK)

    assert synchronous == {
        "pricing_version": "deepseek-v4.1-flash-2026-09-10-peak-offpeak",
        "pricing_period": "peak",
        "pricing_mode": "synchronous",
        "request_started_utc": PEAK.isoformat(),
        "cache_hit_rate_per_million": 0.006,
        "cache_miss_rate_per_million": 0.30,
        "output_rate_per_million": 1.20,
    }
    assert economy == {
        "pricing_version": "deepseek-v4.1-flash-2026-09-10-off-peak",
        "pricing_period": "off_peak",
        "pricing_mode": "off_peak_projection",
        "request_started_utc": PEAK.isoformat(),
        "cache_hit_rate_per_million": 0.003,
        "cache_miss_rate_per_million": 0.15,
        "output_rate_per_million": 0.60,
    }


def test_registry_contains_only_the_three_controlled_benchmark_candidates():
    assert set(BENCHMARK_MODEL_CONFIGS) == {
        "deepseek-v4-pro",
        "deepseek-v4.1-flash",
        "gpt-5.6-luna",
    }
