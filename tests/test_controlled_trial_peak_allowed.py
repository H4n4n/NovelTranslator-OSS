from pathlib import Path

import controlled_trial_execute as executor
import controlled_trial_peak_allowed as peak


def test_peak_allowed_context_sets_and_restores_runtime_override():
    assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is False

    with peak.peak_allowed_execution():
        assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is True

    assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is False


def test_peak_allowed_context_restores_previous_true_value():
    executor._CONTROLLED_TRIAL_ALLOW_PEAK = True
    try:
        with peak.peak_allowed_execution():
            assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is True

        assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is True
    finally:
        executor._CONTROLLED_TRIAL_ALLOW_PEAK = False


def test_peak_allowed_main_scopes_override_around_repo_executor(
    monkeypatch,
):
    observed = []

    def delegated_main(argv=None):
        observed.append(
            (
                argv,
                executor._CONTROLLED_TRIAL_ALLOW_PEAK,
            )
        )
        return 17

    monkeypatch.setattr(
        peak.repo_env,
        "main",
        delegated_main,
    )

    result = peak.main(["--synthetic"])

    assert result == 17
    assert observed == [
        (["--synthetic"], True),
    ]
    assert executor._CONTROLLED_TRIAL_ALLOW_PEAK is False


def test_peak_allowed_module_contains_no_ast_transform():
    source = Path(peak.__file__).read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "import ast",
        "PeakAllowedTransformer",
        "_ControlledProductionTransformer",
        "compile_controlled_production_script",
        "ast.NodeTransformer",
    ):
        assert forbidden not in source
