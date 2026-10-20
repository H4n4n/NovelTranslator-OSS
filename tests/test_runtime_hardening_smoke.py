import noveltranslator_core as core

from runtime_hardening_smoke import main, run_smoke


def _model_state():
    return (
        core.PRODUCTION_TRANSLATION_MODEL,
        core.PRODUCTION_TRANSLATION_MAX_TOKENS,
        core.PRODUCTION_TRANSLATION_THINKING,
        core.PRICING_VERSION,
        {period: dict(rates) for period, rates in core.V4_PRO_PRICES.items()},
    )


def test_smoke_passes_without_leaving_core_model_mutated():
    before = _model_state()
    result = run_smoke()
    after = _model_state()
    assert result == {
        "status": "PASS",
        "api_calls": 0,
        "project_writes": 0,
        "models_checked": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-flash",
        ],
        "pricing_periods_checked": ["off_peak", "peak"],
        "semantic_shadow_commit_decision_changed": False,
    }
    assert after == before


def test_smoke_cli_reports_zero_cost(capsys):
    assert main() == 0
    output = capsys.readouterr().out
    assert "Status: PASS" in output
    assert "deepseek-v4-pro, deepseek-v4-flash, deepseek-flash" in output
    assert "Semantic shadow changed commit decision: NO" in output
    assert "API calls: 0" in output
    assert "Project writes: 0" in output
