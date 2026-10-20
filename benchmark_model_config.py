"""Benchmark-only provider/model metadata for controlled translation comparisons.

This module is deliberately separate from translation_model_config.py.  Adding a
benchmark candidate here does not make it selectable by production, pending
recovery, controlled production trials, or operator preflight.

All helpers are import-safe: no credentials are read, no network calls are made,
and no project files are touched.
"""

from dataclasses import dataclass

from noveltranslator_core import deepseek_pricing_period


DEFAULT_BENCHMARK_MODEL = "deepseek-v4-pro"


@dataclass(frozen=True)
class BenchmarkModelConfig:
    key: str
    provider: str
    model: str
    version: str
    reasoning: str
    max_tokens: int
    pricing_version: str
    prices: dict
    economy_pricing_version: str
    economy_prices: dict
    economy_mode: str


BENCHMARK_MODEL_CONFIGS = {
    "deepseek-v4-pro": BenchmarkModelConfig(
        key="deepseek-pro",
        provider="deepseek",
        model="deepseek-v4-pro",
        version="DeepSeek-V4-Pro-0813",
        reasoning="disabled",
        max_tokens=12000,
        pricing_version="deepseek-v4-pro-0813-peak-offpeak",
        prices={
            "off_peak": {
                "cache_hit": 0.022,
                "cache_miss": 0.66,
                "output": 1.98,
            },
            "peak": {
                "cache_hit": 0.044,
                "cache_miss": 1.32,
                "output": 3.96,
            },
        },
        economy_pricing_version="deepseek-v4-pro-0813-off-peak",
        economy_prices={
            "cache_hit": 0.022,
            "cache_miss": 0.66,
            "output": 1.98,
        },
        economy_mode="off_peak_projection",
    ),
    "deepseek-v4.1-flash": BenchmarkModelConfig(
        key="deepseek-v41-flash",
        provider="deepseek",
        model="deepseek-flash",
        version="DeepSeek-V4.1-Flash",
        reasoning="disabled",
        max_tokens=12000,
        pricing_version="deepseek-v4.1-flash-2026-09-10-peak-offpeak",
        prices={
            "off_peak": {
                "cache_hit": 0.003,
                "cache_miss": 0.15,
                "output": 0.60,
            },
            "peak": {
                "cache_hit": 0.006,
                "cache_miss": 0.30,
                "output": 1.20,
            },
        },
        economy_pricing_version="deepseek-v4.1-flash-2026-09-10-off-peak",
        economy_prices={
            "cache_hit": 0.003,
            "cache_miss": 0.15,
            "output": 0.60,
        },
        economy_mode="off_peak_projection",
    ),
    "gpt-5.6-luna": BenchmarkModelConfig(
        key="luna",
        provider="openai",
        model="gpt-5.6-luna",
        version="gpt-5.6-luna",
        reasoning="none",
        max_tokens=12000,
        pricing_version="openai-gpt-5.6-luna-standard-0.20-0.02-1.20",
        prices={
            "standard": {
                "cache_hit": 0.02,
                "cache_miss": 0.20,
                "output": 1.20,
            },
        },
        economy_pricing_version="openai-gpt-5.6-luna-batch-0.10-0.01-0.60",
        economy_prices={
            "cache_hit": 0.01,
            "cache_miss": 0.10,
            "output": 0.60,
        },
        economy_mode="batch_50_percent_projection",
    ),
}


BENCHMARK_MODEL_ALIASES = {
    "pro": "deepseek-v4-pro",
    "deepseek-pro": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "flash": "deepseek-v4.1-flash",
    "v4.1-flash": "deepseek-v4.1-flash",
    "v41-flash": "deepseek-v4.1-flash",
    "deepseek-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "luna": "gpt-5.6-luna",
    "gpt-5.6-luna": "gpt-5.6-luna",
}


def resolve_benchmark_model(selection=None):
    """Resolve one benchmark candidate without widening production selection."""
    raw = DEFAULT_BENCHMARK_MODEL if selection is None else selection
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("benchmark model selection must be a non-empty string")
    normalized = raw.strip().lower()
    model = BENCHMARK_MODEL_ALIASES.get(normalized)
    if model is None:
        allowed = ", ".join(sorted(BENCHMARK_MODEL_ALIASES))
        raise ValueError(
            f"unsupported benchmark model {raw!r}; expected one of: {allowed}"
        )
    return BENCHMARK_MODEL_CONFIGS[model]


def benchmark_pricing_snapshot(config, moment):
    """Return rates for the synchronous request that is about to be sent."""
    config = resolve_benchmark_model(
        config.model if hasattr(config, "model") else config
    )
    if config.provider == "deepseek":
        period = deepseek_pricing_period(moment)
    else:
        period = "standard"
    rates = config.prices[period]
    return {
        "pricing_version": config.pricing_version,
        "pricing_period": period,
        "pricing_mode": "synchronous",
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": rates["cache_hit"],
        "cache_miss_rate_per_million": rates["cache_miss"],
        "output_rate_per_million": rates["output"],
    }


def economy_pricing_snapshot(config, moment):
    """Project the cheapest intended bulk mode using the same observed tokens.

    DeepSeek benchmark candidates are projected at their canonical off-peak
    rates. GPT-5.6 Luna is projected at Batch rates (50% of Standard). This is a
    pricing projection only; benchmark transport remains synchronous so
    translation quality and request payloads can be inspected immediately.
    """
    config = resolve_benchmark_model(
        config.model if hasattr(config, "model") else config
    )
    rates = config.economy_prices
    return {
        "pricing_version": config.economy_pricing_version,
        "pricing_period": (
            "off_peak" if config.provider == "deepseek" else "batch"
        ),
        "pricing_mode": config.economy_mode,
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": rates["cache_hit"],
        "cache_miss_rate_per_million": rates["cache_miss"],
        "output_rate_per_million": rates["output"],
    }
