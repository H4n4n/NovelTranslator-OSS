"""Zero-cost smoke check for the hardened model/semantic runtime.

This command performs no provider calls and writes no project files. It uses
fixed UTC instants, synthetic translation text, and in-memory core mutation that
is restored before returning.
"""

from copy import deepcopy
from datetime import datetime, timezone

import noveltranslator_core as core
from production_output_runtime import model_display_name, rewrite_legacy_rate_label
from semantic_shadow_runtime import build_semantic_shadow_report
from translation_model_config import resolve_translation_model
from translation_model_runtime import apply_core_model_config


OFF_PEAK_UTC = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
PEAK_UTC = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)


def _core_model_snapshot():
    return {
        "PRODUCTION_TRANSLATION_MODEL": core.PRODUCTION_TRANSLATION_MODEL,
        "PRODUCTION_TRANSLATION_MAX_TOKENS": core.PRODUCTION_TRANSLATION_MAX_TOKENS,
        "PRODUCTION_TRANSLATION_THINKING": core.PRODUCTION_TRANSLATION_THINKING,
        "V4_PRO_PRICES": deepcopy(core.V4_PRO_PRICES),
        "PRICING_VERSION": core.PRICING_VERSION,
    }


def _restore_core_model_snapshot(snapshot):
    for key, value in snapshot.items():
        setattr(core, key, value)


def _assert_model_pricing(selection):
    config = resolve_translation_model(selection)
    apply_core_model_config(core, config)
    off_peak = core.pricing_snapshot(OFF_PEAK_UTC)
    peak = core.pricing_snapshot(PEAK_UTC)
    assert off_peak["pricing_period"] == "off_peak"
    assert peak["pricing_period"] == "peak"
    assert off_peak["pricing_version"] == config.pricing_version
    assert peak["pricing_version"] == config.pricing_version
    assert off_peak["cache_hit_rate_per_million"] == config.prices["off_peak"]["cache_hit"]
    assert off_peak["cache_miss_rate_per_million"] == config.prices["off_peak"]["cache_miss"]
    assert off_peak["output_rate_per_million"] == config.prices["off_peak"]["output"]
    assert peak["cache_hit_rate_per_million"] == config.prices["peak"]["cache_hit"]
    assert peak["cache_miss_rate_per_million"] == config.prices["peak"]["cache_miss"]
    assert peak["output_rate_per_million"] == config.prices["peak"]["output"]
    label = rewrite_legacy_rate_label("V4 Pro rates / 1M: smoke", config)
    assert label.startswith(model_display_name(config) + " rates / 1M:")
    return config


def _assert_semantic_shadow_non_blocking():
    glossary = {
        "甲": {
            "english": "Jia",
            "category": "character",
            "status": "locked",
        }
    }
    legacy = core.translation_commit_qa("甲 entered.", "Someone entered.", glossary, ())
    assert legacy["commit_allowed"] is True
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Someone entered.",
        glossary,
        legacy,
    )
    assert shadow["diagnostic_count"] == 1
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def _assert_existing_strict_block_remains_authoritative():
    glossary = {
        "甲": {
            "english": "Jia",
            "category": "character",
            "status": "locked",
            "strict_qa": True,
        }
    }
    legacy = core.translation_commit_qa("甲 entered.", "Someone entered.", glossary, ())
    assert legacy["commit_allowed"] is False
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Someone entered.",
        glossary,
        legacy,
    )
    assert shadow["legacy_commit_allowed"] is False
    assert shadow["semantic_strict_blocker_count"] == 1
    assert shadow["commit_decision_changed"] is False


def run_smoke():
    snapshot = _core_model_snapshot()
    try:
        pro = _assert_model_pricing("pro")
        legacy_flash = _assert_model_pricing("flash")
        v41_flash = _assert_model_pricing("v41-flash")
        _assert_semantic_shadow_non_blocking()
        _assert_existing_strict_block_remains_authoritative()
        return {
            "status": "PASS",
            "api_calls": 0,
            "project_writes": 0,
            "models_checked": [pro.model, legacy_flash.model, v41_flash.model],
            "pricing_periods_checked": ["off_peak", "peak"],
            "semantic_shadow_commit_decision_changed": False,
        }
    finally:
        _restore_core_model_snapshot(snapshot)


def main():
    result = run_smoke()
    print("=" * 70)
    print("NOVELTRANSLATOR HARDENED RUNTIME SMOKE")
    print("=" * 70)
    print(f"Status: {result['status']}")
    print("Models: " + ", ".join(result["models_checked"]))
    print("Pricing periods: OFF-PEAK, PEAK")
    print("Semantic shadow changed commit decision: NO")
    print(f"API calls: {result['api_calls']}")
    print(f"Project writes: {result['project_writes']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
