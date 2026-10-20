"""Zero-provider tests for the production provider-backend contract."""
import builtins
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest

import production_provider_contract as contract


def test_command_code_backend_exact_identity():
    backend = contract.resolve_provider_backend(
        contract.PROVIDER_COMMAND_CODE,
        wire_model=contract.COMMAND_CODE_DEEPSEEK_V41_FLASH,
    )
    assert backend.provider == "command_code_provider"
    assert backend.base_url == "https://api.commandcode.ai/provider/v1"
    assert backend.chat_path == "/chat/completions"
    assert backend.chat_completions_url() == (
        "https://api.commandcode.ai/provider/v1/chat/completions"
    )
    assert backend.wire_model == "deepseek/deepseek-v4.1-flash"
    assert backend.credential_source == "command_code_dpapi"
    assert backend.reasoning_effort == "low"
    assert backend.translation_completion_tokens == 50000
    assert backend.auxiliary_completion_tokens is None
    assert backend.identity()["auxiliary_completion_policy"] == "provider_unbounded"
    assert backend.accounting == "command_code_credit_equivalent"
    assert backend.pricing_unit == "usd_credit_equivalent_per_1m_tokens"


def test_direct_deepseek_backend_uses_direct_endpoint_and_env_credential():
    backend = contract.resolve_provider_backend(
        contract.PROVIDER_DIRECT_DEEPSEEK, wire_model="deepseek-flash"
    )
    assert backend.base_url == "https://api.deepseek.com"
    assert backend.wire_model == "deepseek-flash"
    assert backend.credential_source == "deepseek_env"
    assert backend.reasoning_effort is None
    assert backend.translation_completion_tokens == 12000
    assert backend.auxiliary_completion_tokens == 3000
    assert backend.accounting == "deepseek_usd_peak_offpeak"
    assert backend.pricing_unit == "usd_per_1m_tokens"


def test_unknown_provider_fails_closed():
    with pytest.raises(contract.ProviderBackendError):
        contract.resolve_provider_backend("anthropic", wire_model="claude")


@pytest.mark.parametrize("wire_model", ["deepseek-flash", "deepseek-v4-pro", ""])
def test_command_code_requires_exact_goat_wire_model(wire_model):
    with pytest.raises(contract.ProviderBackendError):
        contract.resolve_provider_backend(
            contract.PROVIDER_COMMAND_CODE, wire_model=wire_model
        )


def test_no_silent_fallback_to_direct_deepseek():
    # Requesting the GOAT provider with a direct-DeepSeek wire model must refuse
    # rather than quietly switching to api.deepseek.com.
    with pytest.raises(contract.ProviderBackendError):
        contract.resolve_provider_backend(
            contract.PROVIDER_COMMAND_CODE, wire_model="deepseek-flash"
        )
    # And the two backend identities are never equal.
    goat = contract.resolve_provider_backend(
        contract.PROVIDER_COMMAND_CODE,
        wire_model=contract.COMMAND_CODE_DEEPSEEK_V41_FLASH,
    )
    direct = contract.resolve_provider_backend(
        contract.PROVIDER_DIRECT_DEEPSEEK, wire_model="deepseek-flash"
    )
    assert goat.identity() != direct.identity()
    with pytest.raises(contract.ProviderBackendError):
        contract.assert_single_provider_identity(goat, direct)


def test_identity_is_deterministic_and_binds_provider_model_transport():
    backend = contract.resolve_provider_backend(
        contract.PROVIDER_COMMAND_CODE,
        wire_model=contract.COMMAND_CODE_DEEPSEEK_V41_FLASH,
    )
    first = backend.identity()
    second = contract.resolve_provider_backend(
        contract.PROVIDER_COMMAND_CODE,
        wire_model=contract.COMMAND_CODE_DEEPSEEK_V41_FLASH,
    ).identity()
    assert first == second
    for key in (
        "provider", "base_url", "chat_path", "wire_model",
        "reasoning_effort", "translation_completion_tokens",
        "accounting", "pricing_unit",
    ):
        assert key in first


def test_direct_deepseek_requires_explicit_wire_model():
    with pytest.raises(contract.ProviderBackendError):
        contract.resolve_provider_backend(contract.PROVIDER_DIRECT_DEEPSEEK, wire_model="")


def test_module_import_is_inert(monkeypatch):
    source = Path(contract.__file__).read_text(encoding="utf-8")
    module = ModuleType("_test_inert_provider_contract")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def forbidden(*args, **kwargs):
        raise AssertionError("provider contract import must not perform IO or network")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import os
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os, "environ", {})
    exec(compile(source, contract.__file__, "exec"), module.__dict__)
    assert module.PROVIDER_COMMAND_CODE == "command_code_provider"
