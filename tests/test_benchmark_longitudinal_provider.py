from types import SimpleNamespace

import dataclasses

import pytest

from benchmark_longitudinal_config import PROVIDER_BASE_URL
from benchmark_longitudinal_provider import (
    CommandCodeProviderError,
    MissingProviderUsageError,
    ModelReasoningCapability,
    ProviderIdentityError,
    ReasoningLevelBinding,
    RequestExtensionError,
    UnsupportedProviderCapabilityError,
    accept_provider_response,
    assert_benchmark_base_url,
    assert_provider_identity,
    build_provider_request,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    perform_single_provider_request,
    require_provider_usage,
    send_chat_completion_once,
    validated_request_extras,
)


MESSAGES = (
    {"role": "system", "content": "system prompt"},
    {"role": "user", "content": "chapter text"},
)


class FakeCompletions:
    def __init__(self, response):
        self.calls = []
        self._response = response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def make_fake_client(response):
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def simple_response(*, model="z-ai/glm-5.3-flash", usage=None, finish_reason="stop"):
    return SimpleNamespace(
        id="resp-1",
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content="translated prose"),
            )
        ],
        usage=usage,
    )


def complete_usage():
    return SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)


def reasoning_capability(*, model_id="z-ai/glm-5.3-flash", verified=True):
    # This is a synthetic exact wire shape for tests only.  The real Command
    # Code Provider reasoning wire format is deliberately not invented yet.
    return ModelReasoningCapability(
        model_id=model_id,
        verified=verified,
        bindings=(
            ReasoningLevelBinding("low", (("reasoning_effort", "low"),)),
            ReasoningLevelBinding("medium", (("reasoning_effort", "medium"),)),
        ),
    )


def test_client_factory_uses_max_retries_zero_and_exact_base_url():
    captured = {}

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return "fake-client"

    client = create_provider_client(api_key="injected-key", client_factory=fake_factory)
    assert client == "fake-client"
    assert captured == {
        "api_key": "injected-key",
        "base_url": "https://api.commandcode.ai/provider/v1",
        "max_retries": 0,
    }


def test_client_factory_requires_explicit_key():
    with pytest.raises(CommandCodeProviderError, match="injected explicitly"):
        create_provider_client(api_key="", client_factory=lambda **kwargs: kwargs)


@pytest.mark.parametrize(
    "bad_url",
    (
        "http://insecure.example",
        "https://evil.example/provider/v1",
        "https://api.commandcode.ai/provider/v2",
    ),
)
def test_client_factory_rejects_arbitrary_base_url(bad_url):
    with pytest.raises(CommandCodeProviderError, match="exactly the Command Code Provider"):
        create_provider_client(
            api_key="key",
            base_url=bad_url,
            client_factory=lambda **kwargs: kwargs,
        )
    with pytest.raises(CommandCodeProviderError, match="exactly the Command Code Provider"):
        assert_benchmark_base_url(bad_url)


def test_default_client_factory_never_called_on_import():
    import benchmark_longitudinal_provider as provider

    assert "_default_client_factory" in dir(provider)
    # The factory imports openai lazily; nothing constructs a client at import.
    assert provider.PROVIDER_NAME == "command_code_provider"


def test_request_builder_produces_openai_compatible_body():
    body = build_provider_request(
        model="google/gemini-3.8-flash",
        messages=MESSAGES,
        max_output_tokens=12000,
    )
    assert body == {
        "model": "google/gemini-3.8-flash",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "chapter text"},
        ],
        "max_completion_tokens": 12000,
    }


def test_request_builder_rejects_bad_inputs():
    with pytest.raises(CommandCodeProviderError):
        build_provider_request(model="", messages=MESSAGES, max_output_tokens=10)
    with pytest.raises(CommandCodeProviderError):
        build_provider_request(model="m", messages=(), max_output_tokens=10)
    with pytest.raises(CommandCodeProviderError):
        build_provider_request(model="m", messages=MESSAGES, max_output_tokens=0)


def test_reserved_extra_fields_are_rejected():
    with pytest.raises(RequestExtensionError, match="adapter-owned field"):
        build_provider_request(
            model="m",
            messages=MESSAGES,
            max_output_tokens=10,
            request_extras={"messages": "hijack"},
        )
    with pytest.raises(RequestExtensionError, match="JSON-serializable"):
        validated_request_extras({"metadata": object()})


def test_harmless_generic_extras_are_allowed():
    extras = validated_request_extras(
        {"temperature": 0, "metadata": {"trace_id": "abc-123"}}
    )
    assert extras.as_dict() == {"temperature": 0, "metadata": {"trace_id": "abc-123"}}
    body = build_provider_request(
        model="m",
        messages=MESSAGES,
        max_output_tokens=10,
        request_extras={"temperature": 0},
    )
    assert body["temperature"] == 0


def test_nested_extra_body_reasoning_bypass_is_rejected():
    with pytest.raises(RequestExtensionError, match="at any nesting depth"):
        validated_request_extras({"extra_body": {"thinking": {"type": "enabled"}}})
    with pytest.raises(RequestExtensionError, match="at any nesting depth"):
        build_provider_request(
            model="m",
            messages=MESSAGES,
            max_output_tokens=10,
            request_extras={"extra_body": {"thinking": {"type": "enabled"}}},
        )
    with pytest.raises(RequestExtensionError, match="at any nesting depth"):
        validated_request_extras({"a": {"b": [{"reasoning_effort": "low"}]}})
    with pytest.raises(RequestExtensionError, match="at any nesting depth"):
        validated_request_extras({"reasoning_effort": "low"})


def test_reasoning_control_requires_verified_model_specific_capability():
    # Unverified capability fails closed.
    with pytest.raises(UnsupportedProviderCapabilityError):
        build_provider_request(
            model="z-ai/glm-5.3-flash",
            messages=MESSAGES,
            max_output_tokens=10,
            reasoning_capability=reasoning_capability(verified=False),
            reasoning_level="low",
        )
    # A capability verified for one model must not authorize another model.
    capability = reasoning_capability(model_id="z-ai/glm-5.3-flash")
    with pytest.raises(UnsupportedProviderCapabilityError, match="must not authorize"):
        build_provider_request(
            model="tencent/hy3-paid",
            messages=MESSAGES,
            max_output_tokens=10,
            reasoning_capability=capability,
            reasoning_level="low",
        )
    # A verified "low" must not authorize an unknown effort.
    with pytest.raises(UnsupportedProviderCapabilityError, match="invalid or unknown effort"):
        build_provider_request(
            model="z-ai/glm-5.3-flash",
            messages=MESSAGES,
            max_output_tokens=10,
            reasoning_capability=capability,
            reasoning_level="garbage",
        )
    # Capability and level are inseparable.
    with pytest.raises(RequestExtensionError, match="requires both"):
        build_provider_request(
            model="z-ai/glm-5.3-flash",
            messages=MESSAGES,
            max_output_tokens=10,
            reasoning_capability=capability,
        )
    with pytest.raises(RequestExtensionError, match="requires both"):
        build_provider_request(
            model="z-ai/glm-5.3-flash",
            messages=MESSAGES,
            max_output_tokens=10,
            reasoning_level="low",
        )


@pytest.mark.parametrize("level", ("low", "medium"))
def test_verified_reasoning_serializes_the_exact_payload(level):
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=10,
        reasoning_capability=reasoning_capability(),
        reasoning_level=level,
    )
    assert body["reasoning_effort"] == level
    assert body["model"] == "z-ai/glm-5.3-flash"


def test_request_identity_preserves_exact_requested_model():
    body = build_provider_request(
        model="tencent/hy3-paid",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)
    assert identity.requested_model == "tencent/hy3-paid"
    assert identity.base_url == PROVIDER_BASE_URL
    assert identity.endpoint_path == "/chat/completions"
    assert len(identity.request_sha256) == 64


def test_request_identity_rejects_arbitrary_base_url():
    body = build_provider_request(
        model="tencent/hy3-paid",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    with pytest.raises(CommandCodeProviderError, match="exactly the Command Code Provider"):
        build_provider_request_identity(request_body=body, base_url="https://evil.example/v1")


def test_normalize_records_requested_and_returned_model():
    body = build_provider_request(
        model="xiaomi/mimo-v2.5-pro",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)
    response = normalize_provider_response(
        simple_response(model="xiaomi/mimo-v2.5-pro"),
        request_identity=identity,
    )
    assert response.requested_model == "xiaomi/mimo-v2.5-pro"
    assert response.response_model == "xiaomi/mimo-v2.5-pro"
    assert response.finish_reason == "stop"
    assert response.content == "translated prose"
    assert response.response_model_matches_requested is True


def test_identity_mismatch_fails_closed_and_preserves_both_models():
    body = build_provider_request(
        model="deepseek/deepseek-v4.1-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)
    response = normalize_provider_response(
        simple_response(model="z-ai/glm-5.3-flash"),
        request_identity=identity,
    )
    assert response.requested_model == "deepseek/deepseek-v4.1-flash"
    assert response.response_model == "z-ai/glm-5.3-flash"
    with pytest.raises(ProviderIdentityError, match="identity mismatch"):
        assert_provider_identity(identity, response)


def test_missing_usage_is_unknown_and_never_fabricated():
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)
    response = normalize_provider_response(
        simple_response(usage=None),
        request_identity=identity,
    )
    assert response.usage is None
    assert response.usage_known is False
    with pytest.raises(MissingProviderUsageError, match="must never be fabricated"):
        require_provider_usage(response)


def test_partial_usage_stays_unknown_and_fails_accounting():
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=None, total_tokens=100)
    response = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )
    assert response.usage.prompt_tokens == 100
    assert response.usage.completion_tokens is None
    assert response.usage_known is False
    with pytest.raises(MissingProviderUsageError):
        require_provider_usage(response)


def test_cached_tokens_captured_only_when_exposed():
    body = build_provider_request(
        model="qwen/qwen3.8-max-0902",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)

    exposed = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=640),
    )
    with_cache = normalize_provider_response(
        simple_response(usage=exposed),
        request_identity=identity,
    )
    assert with_cache.usage.cached_tokens == 640
    assert with_cache.usage.cached_tokens_exposed is True
    assert with_cache.usage_known is True
    assert require_provider_usage(with_cache).completion_tokens == 200

    not_exposed = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, total_tokens=1200)
    without_cache = normalize_provider_response(
        simple_response(usage=not_exposed),
        request_identity=identity,
    )
    assert without_cache.usage.cached_tokens is None
    assert without_cache.usage.cached_tokens_exposed is False


def test_single_attempt_transport_uses_fake_client_exactly_once():
    response = simple_response(
        model="z-ai/glm-5.3-flash",
        usage=complete_usage(),
    )
    client, completions = make_fake_client(response)
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    normalized = perform_single_provider_request(client, request_body=body)
    assert len(completions.calls) == 1
    assert completions.calls[0]["model"] == "z-ai/glm-5.3-flash"
    assert normalized.content == "translated prose"
    assert normalized.usage_known is True

    send_chat_completion_once(client, body)
    assert len(completions.calls) == 2


def test_transport_attempt_is_not_paid_acceptance():
    # A raw transport attempt normalizes even an unsafe response; acceptance is
    # the separate fail-closed step.
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    client, _ = make_fake_client(simple_response(model="tencent/hy3-paid", usage=None))
    normalized = perform_single_provider_request(client, request_body=body)
    assert normalized.response_model == "tencent/hy3-paid"
    identity = build_provider_request_identity(request_body=body)
    with pytest.raises(ProviderIdentityError):
        accept_provider_response(identity, normalized)


def test_accepted_response_requires_exact_model_and_complete_usage():
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    identity = build_provider_request_identity(request_body=body)

    accepted = accept_provider_response(
        identity,
        normalize_provider_response(
            simple_response(model="z-ai/glm-5.3-flash", usage=complete_usage()),
            request_identity=identity,
        ),
    )
    assert accepted.requested_model == "z-ai/glm-5.3-flash"
    assert accepted.response_model == "z-ai/glm-5.3-flash"
    assert accepted.usage.completion_tokens == 5
    assert accepted.content == "translated prose"

    # Model mismatch is rejected.
    with pytest.raises(ProviderIdentityError, match="identity mismatch"):
        accept_provider_response(
            identity,
            normalize_provider_response(
                simple_response(model="tencent/hy3-paid", usage=complete_usage()),
                request_identity=identity,
            ),
        )
    # Missing usage is rejected.
    with pytest.raises(MissingProviderUsageError):
        accept_provider_response(
            identity,
            normalize_provider_response(
                simple_response(model="z-ai/glm-5.3-flash", usage=None),
                request_identity=identity,
            ),
        )
    # Partial usage is rejected.
    with pytest.raises(MissingProviderUsageError):
        accept_provider_response(
            identity,
            normalize_provider_response(
                simple_response(
                    model="z-ai/glm-5.3-flash",
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=None),
                ),
                request_identity=identity,
            ),
        )


def test_perform_single_provider_request_rejects_arbitrary_base_url():
    client, completions = make_fake_client(simple_response())
    body = build_provider_request(
        model="z-ai/glm-5.3-flash",
        messages=MESSAGES,
        max_output_tokens=500,
    )
    with pytest.raises(CommandCodeProviderError, match="exactly the Command Code Provider"):
        perform_single_provider_request(
            client, request_body=body, base_url="https://evil.example/v1"
        )
    assert completions.calls == []


def test_unsupported_response_shapes_fail_closed():
    body = build_provider_request(
        model="m", messages=MESSAGES, max_output_tokens=5
    )
    identity = build_provider_request_identity(request_body=body)
    with pytest.raises(CommandCodeProviderError, match="exactly one choice"):
        normalize_provider_response(
            SimpleNamespace(choices=[]), request_identity=identity
        )


def _reasoning_identity(model="deepseek/deepseek-v4.1-flash"):
    body = build_provider_request(
        model=model,
        messages=MESSAGES,
        max_output_tokens=500,
    )
    return build_provider_request_identity(request_body=body)


def _reasoning_usage(**overrides):
    values = {
        "prompt_tokens": 1_000,
        "completion_tokens": 200,
        "total_tokens": 1_200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_reported_reasoning_tokens_are_captured_as_a_diagnostic_subset():
    identity = _reasoning_identity()
    usage = _reasoning_usage(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=150)
    )
    normalized = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )
    assert normalized.usage.reasoning_tokens == 150
    assert normalized.usage.reasoning_tokens_exposed is True
    # Completion tokens stay authoritative for paid accounting.
    assert normalized.usage.completion_tokens == 200


def test_explicitly_reported_zero_reasoning_tokens_is_exposed():
    identity = _reasoning_identity()
    usage = _reasoning_usage(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0)
    )
    normalized = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )
    assert normalized.usage.reasoning_tokens == 0
    assert normalized.usage.reasoning_tokens_exposed is True


@pytest.mark.parametrize(
    "usage",
    (
        _reasoning_usage(),
        _reasoning_usage(completion_tokens_details=None),
        _reasoning_usage(prompt_tokens_details=None),
    ),
)
def test_absent_reasoning_details_stay_unknown_and_unexposed(usage):
    identity = _reasoning_identity()
    normalized = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )
    assert normalized.usage.reasoning_tokens is None
    assert normalized.usage.reasoning_tokens_exposed is False
    assert normalized.usage.usage_known is True


@pytest.mark.parametrize(
    "bad_reasoning",
    (-1, "150", 150.0, True, [150]),
)
def test_malformed_reasoning_tokens_fail_closed(bad_reasoning):
    identity = _reasoning_identity()
    usage = _reasoning_usage(
        completion_tokens_details=SimpleNamespace(
            reasoning_tokens=bad_reasoning
        )
    )
    with pytest.raises(CommandCodeProviderError, match="reasoning_tokens"):
        normalize_provider_response(
            simple_response(usage=usage),
            request_identity=identity,
        )


def test_reasoning_diagnostics_do_not_change_usage_known():
    identity = _reasoning_identity()
    usage = _reasoning_usage(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=199)
    )
    normalized = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )
    assert normalized.usage_known is True
    assert require_provider_usage(normalized).completion_tokens == 200
    assert normalized.usage.total_tokens == 1_200
    assert normalized.usage.cached_tokens is None


def test_reasoning_text_is_ignored_and_never_copied_into_the_normalized_object():
    secret = "hidden chain of thought that must never be persisted"
    response = SimpleNamespace(
        id="resp-1",
        model="deepseek/deepseek-v4.1-flash",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="visible translation",
                    reasoning_content=secret,
                    thinking={"summary": secret},
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=4,
                reasoning_content=secret,
                thinking=secret,
            ),
        ),
    )
    identity = _reasoning_identity()
    normalized = normalize_provider_response(
        response, request_identity=identity
    )

    assert normalized.content == "visible translation"
    assert normalized.usage.reasoning_tokens == 4
    assert secret not in repr(normalized)
    assert secret not in repr(normalized.usage)
    assert {
        field.name for field in dataclasses.fields(normalized)
    } == {
        "requested_model",
        "response_model",
        "provider_response_id",
        "finish_reason",
        "content",
        "usage",
    }
    assert not hasattr(normalized.usage, "reasoning_content")
    assert not hasattr(normalized.usage, "thinking")


# --------------------------------------------------------------------------
# Raw visible content preservation
# --------------------------------------------------------------------------


def _content_response(content, *, usage=None, finish_reason="stop"):
    return SimpleNamespace(
        id="resp-1",
        model="deepseek/deepseek-v4.1-flash",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=usage if usage is not None else complete_usage(),
    )


def test_raw_visible_content_is_preserved_byte_for_byte():
    identity = _reasoning_identity()

    prose = "  Translated prose with leading spaces.\n\nSecond paragraph.  "
    normalized = normalize_provider_response(
        _content_response(prose),
        request_identity=identity,
    )
    assert normalized.content == prose

    empty = normalize_provider_response(
        _content_response(""),
        request_identity=identity,
    )
    assert empty.content == ""
    assert empty.content == ""


@pytest.mark.parametrize(
    "bad_content",
    (
        None,
        False,
        True,
        0,
        5,
        0.0,
        [],
        ["translated prose"],
        {"text": "translated prose"},
        object(),
    ),
)
def test_present_non_string_visible_content_fails_closed(bad_content):
    """A present non-string content is never truthiness- or str-coerced."""
    identity = _reasoning_identity()

    with pytest.raises(
        CommandCodeProviderError,
        match="content must be a string when present",
    ):
        normalize_provider_response(
            _content_response(bad_content),
            request_identity=identity,
        )


def test_empty_looking_non_string_content_never_becomes_an_empty_translation():
    """None/False/0/[] used to be coerced into ``""`` by ``or ""``."""
    identity = _reasoning_identity()

    for empty_looking in (None, False, 0, []):
        with pytest.raises(CommandCodeProviderError):
            normalize_provider_response(
                _content_response(empty_looking),
                request_identity=identity,
            )


def test_absent_content_field_keeps_the_documented_empty_content_behavior():
    """Justified by the adapter contract: only an absent field defaults to ''.

    The adapter has always treated a message without a ``content`` field (or a
    response without a message at all) as an empty visible translation, and that
    empty translation is what G7J then classifies mechanically. Every explicitly
    delivered non-string value fails closed instead, and no such value is ever
    coerced.
    """
    identity = _reasoning_identity()

    without_field = SimpleNamespace(
        id="resp-1",
        model="deepseek/deepseek-v4.1-flash",
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(),
            )
        ],
        usage=complete_usage(),
    )
    normalized = normalize_provider_response(
        without_field,
        request_identity=identity,
    )
    assert normalized.content == ""
    assert normalized.finish_reason == "length"
    assert normalized.usage.completion_tokens == 5

    without_message = SimpleNamespace(
        id="resp-1",
        model="deepseek/deepseek-v4.1-flash",
        choices=[SimpleNamespace(finish_reason="length")],
        usage=complete_usage(),
    )
    assert normalize_provider_response(
        without_message,
        request_identity=identity,
    ).content == ""


def test_explicitly_absent_reasoning_tokens_stays_unknown_and_unexposed():
    """``completion_tokens_details`` present with ``reasoning_tokens=None``."""
    identity = _reasoning_identity()
    usage = _reasoning_usage(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=None)
    )
    normalized = normalize_provider_response(
        simple_response(usage=usage),
        request_identity=identity,
    )

    assert normalized.usage.reasoning_tokens is None
    assert normalized.usage.reasoning_tokens_exposed is False
    assert normalized.usage_known is True
    assert normalized.usage.completion_tokens == 200
    assert require_provider_usage(normalized).completion_tokens == 200
    assert normalized.content == "translated prose"
