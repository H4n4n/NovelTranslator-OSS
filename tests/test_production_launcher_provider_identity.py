import sys

import run_production_engine as launcher
from provider_model_identity_runtime import PROVIDER_MODEL_IDENTITY_MODE


def test_real_production_loader_installs_provider_model_identity_guard(monkeypatch):
    monkeypatch.delitem(
        sys.modules,
        launcher.PRODUCTION_MODULE_NAME,
        raising=False,
    )

    module = launcher.load_production_module(
        launcher.PRODUCTION_SCRIPT
    )

    state = getattr(
        module,
        "_noveltranslator_provider_model_identity_runtime_state",
    )
    assert state["mode"] == PROVIDER_MODEL_IDENTITY_MODE
    assert module.send_api_request_once is state["guarded_send_api_request_once"]
    assert (
        module.run_journaled_paid_request
        is state["guarded_run_journaled_paid_request"]
    )


def test_custom_loader_fixture_is_not_forced_through_production_guard(
    tmp_path,
    monkeypatch,
):
    script = tmp_path / "custom_production_fixture.py"
    script.write_text(
        "def initialize_production_runtime_state():\n"
        "    return None\n"
        "def run_unattended_production():\n"
        "    return None\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(
        sys.modules,
        launcher.PRODUCTION_MODULE_NAME,
        raising=False,
    )

    module = launcher.load_production_module(script)

    assert not hasattr(
        module,
        "_noveltranslator_provider_model_identity_runtime_state",
    )
