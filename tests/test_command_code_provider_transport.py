"""Zero-provider tests for the GOAT production transport adapter."""
import builtins
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import command_code_provider_transport as transport

GOAT_MODEL = "deepseek/deepseek-v4.1-flash"
MSGS = [{"role": "user", "content": "hello"}]


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response))


def _response(model=GOAT_MODEL, finish="stop", content="translated", usage=True):
    usage_obj = (
        SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        if usage
        else None
    )
    return SimpleNamespace(
        model=model,
        id="fake-id",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=usage_obj,
    )


def test_client_uses_exact_base_url_and_zero_retries():
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return object()

    transport.create_command_code_client(api_key="sk-cc-fake", client_factory=factory)
    assert captured["base_url"] == "https://api.commandcode.ai/provider/v1"
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "sk-cc-fake"


def test_client_requires_injected_key():
    with pytest.raises(Exception):
        transport.create_command_code_client(api_key="")


def test_translation_request_body_is_goat_and_verified():
    body = transport.build_command_code_translation_request(
        messages=[{"role": "user", "content": "hello"}]
    )
    assert body["model"] == GOAT_MODEL
    assert body["max_completion_tokens"] == 50000
    assert body["reasoning_effort"] == "low"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    # never the direct-DeepSeek control shape
    assert "max_tokens" not in body
    assert "extra_body" not in body


def test_translation_request_never_routes_to_direct_deepseek():
    body = transport.build_command_code_translation_request(messages=MSGS)
    assert body["model"] != "deepseek-flash"
    assert transport.transport_identity()["base_url"] == (
        "https://api.commandcode.ai/provider/v1"
    )


def test_unverified_reasoning_effort_fails_closed(monkeypatch):
    monkeypatch.setattr(transport, "COMMAND_CODE_TRANSLATION_REASONING_EFFORT", "max")
    with pytest.raises(transport.CommandCodeTransportError):
        transport.build_command_code_translation_request(messages=MSGS)


def test_unknown_model_has_no_capability():
    from benchmark_longitudinal_capability_results import verified_reasoning_capability
    with pytest.raises(Exception):
        verified_reasoning_capability("deepseek/does-not-exist")


def test_send_and_accept_roundtrip():
    client = _FakeClient(_response())
    identity, normalized = transport.send_command_code_request_once(
        client, transport.build_command_code_translation_request(messages=MSGS)
    )
    accepted = transport.accept_command_code_response(identity, normalized)
    assert accepted.response_model == GOAT_MODEL
    assert accepted.content == "translated"
    assert accepted.finish_reason == "stop"
    assert client.chat.completions.calls[0]["model"] == GOAT_MODEL


def test_provider_model_mismatch_fails_closed():
    client = _FakeClient(_response(model="deepseek-flash"))
    identity, normalized = transport.send_command_code_request_once(
        client, transport.build_command_code_translation_request(messages=MSGS)
    )
    with pytest.raises(Exception):
        transport.accept_command_code_response(identity, normalized)


def test_missing_usage_fails_closed():
    client = _FakeClient(_response(usage=False))
    identity, normalized = transport.send_command_code_request_once(
        client, transport.build_command_code_translation_request(messages=MSGS)
    )
    with pytest.raises(Exception):
        transport.accept_command_code_response(identity, normalized)


def test_transport_identity_binds_provider_model_policy():
    ident = transport.transport_identity()
    assert ident["provider"] == "command_code_provider"
    assert ident["wire_model"] == GOAT_MODEL
    assert ident["reasoning_effort"] == "low"
    assert ident["translation_completion_tokens"] == 50000
    assert ident["auxiliary_completion_tokens"] is None
    assert ident["pricing_unit"] == "usd_credit_equivalent_per_1m_tokens"


def test_auxiliary_budget_is_corrected_8k():
    assert transport.auxiliary_completion_budget(
        logical_operation="state_extraction", requested_tokens=3000
    ) == 8000


def test_import_is_inert(monkeypatch):
    source = Path(transport.__file__).read_text(encoding="utf-8")
    module = ModuleType("_test_inert_transport")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def forbidden(*args, **kwargs):
        raise AssertionError("transport import must not touch the network or credentials")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    import os
    monkeypatch.setattr(os, "getenv", forbidden)
    exec(compile(source, transport.__file__, "exec"), module.__dict__)
    assert module.PROVIDER_BASE_URL == "https://api.commandcode.ai/provider/v1"
