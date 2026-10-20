"""Import-safe Command Code Provider adapter foundation.

This module is the foundation of the longitudinal benchmark's provider
transport.  It talks to the Command Code Provider API directly over its
OpenAI-compatible chat-completions endpoint; it never routes through ``cmdc``
or the Command Code coding agent.

Guarantees for this slice:

* importing the module reads no credentials, performs no network activity,
  constructs no client, and writes no files;
* the API key is injected explicitly at call time -- nothing here calls
  ``getenv`` or ``load_dotenv``;
* the OpenAI-compatible client is constructed with ``max_retries=0`` and the
  exact benchmark base URL -- any other HTTPS base URL is rejected;
* request/response identity preserves the exact requested model and the exact
  returned response model;
* raw visible assistant content is preserved exactly and must be a string: an
  absent ``content`` field keeps the documented empty-content behavior, while a
  present non-string value (``None``, booleans, numbers, lists, objects) fails
  closed instead of being coerced into an empty translation;
* finish reason and token usage are normalized, cached-token usage is captured
  only when the provider exposes it, and missing usage is reported as unknown --
  it is never fabricated;
* provider-specific reasoning parameter shapes are deliberately NOT invented.
  Generic request extras may never smuggle a reasoning/thinking/effort/budget
  control at any nesting depth; a reasoning control is only ever attached as the
  EXACT serialized payload of an explicitly verified, model-specific
  :class:`ModelReasoningCapability`.

This adapter is transport only.  It is **not** the durable fail-closed paid
request journal.  A raw single transport attempt later MUST execute underneath
the repository's existing durable paid-request journal/controller boundary
(``production_paid_request_journal``) before any real paid request is sent;
``max_retries=0`` alone does not resolve ambiguous paid outcomes.  Response
acceptance is a separate, fail-closed step (:func:`accept_provider_response`)
that requires exact returned model identity and complete usage accounting.

Provider access only happens through explicitly invoked helpers, and every
helper is testable with a fake client.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from benchmark_longitudinal_config import (
    CHAT_COMPLETIONS_PATH,
    PROVIDER_BASE_URL,
    PROVIDER_NAME,
)


# Fields the adapter owns; an extra request field may never collide with them.
_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "n",
        "max_completion_tokens",
        "max_tokens",
    }
)

# A request field whose name contains one of these markers is treated as a
# reasoning control and may only be attached as the exact payload of a verified
# model-specific capability -- at ANY nesting depth.
_REASONING_CONTROL_MARKERS = (
    "reason",
    "thinking",
    "think",
    "effort",
    "budget",
    "verbosity",
)


class CommandCodeProviderError(RuntimeError):
    """The provider request, response, or accounting is unsafe to accept."""


class ProviderIdentityError(CommandCodeProviderError):
    """The provider-reported model identity does not match the request."""


class MissingProviderUsageError(CommandCodeProviderError):
    """The response omitted usage and paid accounting must not be guessed."""


class RequestExtensionError(CommandCodeProviderError):
    """An explicit request extension is malformed or unsupported."""


class UnsupportedProviderCapabilityError(RequestExtensionError):
    """A provider capability is not verified and must never be assumed."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CommandCodeProviderError(
            "provider request identity is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _assert_json_serializable(name: str, value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RequestExtensionError(
            f"request extension {name!r} is not JSON-serializable"
        ) from exc


def _reasoning_marker_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[str]:
    """Return every key path (at any depth) that looks like a reasoning control."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            path = (*prefix, key)
            lowered = key.lower()
            if any(marker in lowered for marker in _REASONING_CONTROL_MARKERS):
                found.append(".".join(path))
            found.extend(_reasoning_marker_paths(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_reasoning_marker_paths(item, (*prefix, str(index))))
    return found


@dataclass(frozen=True)
class ValidatedRequestExtras:
    """Explicit, validated, harmless extra provider request-body fields."""

    fields: tuple[tuple[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {name: copy.deepcopy(value) for name, value in self.fields}


def validated_request_extras(
    extras: Mapping[str, Any] | None = None,
) -> ValidatedRequestExtras:
    """Validate an explicit mapping of harmless extra request-body fields.

    Provider-specific reasoning controls are not invented here.  A field whose
    name looks like a reasoning control is rejected at any nesting depth, so a
    generic extra cannot smuggle ``{"extra_body": {"thinking": ...}}`` past a
    top-level-only check.  Reasoning controls must instead be supplied as the
    exact serialized payload of a verified capability.
    """
    if extras is None:
        return ValidatedRequestExtras()
    if not isinstance(extras, Mapping):
        raise RequestExtensionError("request extensions must be a mapping")

    markers = _reasoning_marker_paths(extras)
    if markers:
        raise RequestExtensionError(
            "request extensions must not carry a reasoning/thinking/effort/budget "
            f"control at any nesting depth: {sorted(markers)}; the benchmark must "
            "not invent provider-specific reasoning parameter shapes"
        )

    fields: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for name, value in extras.items():
        if not isinstance(name, str) or not name.strip():
            raise RequestExtensionError("request extension field name must be a non-empty string")
        if name in _RESERVED_REQUEST_FIELDS:
            raise RequestExtensionError(
                f"request extension {name!r} collides with an adapter-owned field"
            )
        if name in seen:
            raise RequestExtensionError(f"duplicate request extension field {name!r}")
        seen.add(name)
        _assert_json_serializable(name, value)
        fields.append((name, copy.deepcopy(value)))
    return ValidatedRequestExtras(tuple(fields))


@dataclass(frozen=True)
class ReasoningLevelBinding:
    """The exact verified request-wire payload for one reasoning level."""

    level: str
    wire_payload: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.level, str) or not self.level.strip():
            raise RequestExtensionError("reasoning level must be a non-empty string")
        if not isinstance(self.wire_payload, tuple) or not self.wire_payload:
            raise RequestExtensionError(
                "reasoning level wire payload must be a non-empty tuple of fields"
            )
        seen: set[str] = set()
        for name, value in self.wire_payload:
            if not isinstance(name, str) or not name.strip():
                raise RequestExtensionError(
                    "reasoning wire payload field name must be a non-empty string"
                )
            if name in _RESERVED_REQUEST_FIELDS:
                raise RequestExtensionError(
                    f"reasoning wire payload field {name!r} collides with an "
                    "adapter-owned field"
                )
            if name in seen:
                raise RequestExtensionError(
                    f"duplicate reasoning wire payload field {name!r}"
                )
            seen.add(name)
            _assert_json_serializable(name, value)

    def as_dict(self) -> dict[str, Any]:
        return {name: copy.deepcopy(value) for name, value in self.wire_payload}


@dataclass(frozen=True)
class VerifiedReasoningControl:
    """The exact serialized reasoning-control payload authorized for one request."""

    model_id: str
    level: str
    wire_payload: tuple[tuple[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {name: copy.deepcopy(value) for name, value in self.wire_payload}


@dataclass(frozen=True)
class ModelReasoningCapability:
    """A model-specific verified reasoning capability.

    A capability is exact and model-bound: it names a single ``model_id`` and
    the exact, serialized request-wire payload for each supported reasoning
    level.  A capability verified for one model never authorizes another model,
    a verified ``low`` never authorizes an unknown effort, and there is no way
    to request an arbitrary thinking budget or a different request field shape.
    """

    model_id: str
    verified: bool
    bindings: tuple[ReasoningLevelBinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise RequestExtensionError(
                "reasoning capability model id must be a non-empty string"
            )
        if not isinstance(self.verified, bool):
            raise RequestExtensionError(
                "reasoning capability verified flag must be a boolean"
            )
        if not isinstance(self.bindings, tuple) or not self.bindings:
            raise RequestExtensionError(
                "reasoning capability must declare at least one level binding"
            )
        seen: set[str] = set()
        for binding in self.bindings:
            if not isinstance(binding, ReasoningLevelBinding):
                raise RequestExtensionError(
                    "reasoning capability bindings must be ReasoningLevelBinding"
                )
            if binding.level in seen:
                raise RequestExtensionError(
                    f"duplicate reasoning capability level: {binding.level!r}"
                )
            seen.add(binding.level)

    @property
    def levels(self) -> tuple[str, ...]:
        return tuple(binding.level for binding in self.bindings)

    def assert_verified(self) -> ModelReasoningCapability:
        if not self.verified:
            raise UnsupportedProviderCapabilityError(
                f"reasoning capability for model {self.model_id!r} is not verified; "
                "the benchmark must not guess a provider-specific parameter shape and "
                "must never silently fall back to an unsupported control"
            )
        return self

    def control_for(self, model_id: str, level: str) -> VerifiedReasoningControl:
        """Return the exact verified payload for one model and one level."""
        self.assert_verified()
        if model_id != self.model_id:
            raise UnsupportedProviderCapabilityError(
                f"reasoning capability verified for model {self.model_id!r} must not "
                f"authorize model {model_id!r}"
            )
        if not isinstance(level, str) or level not in self.levels:
            available = ", ".join(self.levels)
            raise UnsupportedProviderCapabilityError(
                f"reasoning level {level!r} is not an exactly verified level for model "
                f"{self.model_id!r} ({available}); an invalid or unknown effort must "
                "never be authorized"
            )
        for binding in self.bindings:
            if binding.level == level:
                return VerifiedReasoningControl(
                    model_id=self.model_id,
                    level=level,
                    wire_payload=binding.wire_payload,
                )
        raise UnsupportedProviderCapabilityError(
            f"reasoning level {level!r} has no exact verified wire payload"
        )


def assert_benchmark_base_url(base_url: str) -> str:
    """Fail closed unless the benchmark uses the exact Command Code base URL."""
    if base_url != PROVIDER_BASE_URL:
        raise CommandCodeProviderError(
            "benchmark paid transport must use exactly the Command Code Provider "
            f"base URL {PROVIDER_BASE_URL!r}; refusing {base_url!r}"
        )
    return base_url


@dataclass(frozen=True)
class ProviderRequestIdentity:
    """The immutable identity binding for one provider request."""

    provider: str
    base_url: str
    endpoint_path: str
    requested_model: str
    request_sha256: str

    def identity(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "endpoint_path": self.endpoint_path,
            "requested_model": self.requested_model,
            "request_sha256": self.request_sha256,
        }


def build_provider_request(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_output_tokens: int,
    request_extras: Mapping[str, Any] | None = None,
    reasoning_capability: ModelReasoningCapability | None = None,
    reasoning_level: str | None = None,
) -> dict[str, Any]:
    """Build one canonical OpenAI-compatible chat-completions request body.

    Harmless generic extras are validated separately from a verified
    reasoning-control payload.  Supplying only one of
    ``reasoning_capability``/``reasoning_level`` is rejected: reasoning can never
    be attached without an exact verified model-specific capability.
    """
    if not isinstance(model, str) or not model.strip():
        raise CommandCodeProviderError("provider request model must be a non-empty string")
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise CommandCodeProviderError("provider request messages must be a non-empty sequence")
    if not messages:
        raise CommandCodeProviderError("provider request messages must be a non-empty sequence")
    normalized_messages = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise CommandCodeProviderError("each provider message must be a mapping")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role.strip():
            raise CommandCodeProviderError("each provider message requires a role")
        if not isinstance(content, str):
            raise CommandCodeProviderError("each provider message requires string content")
        normalized_messages.append({"role": role, "content": content})
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 1
    ):
        raise CommandCodeProviderError(
            "provider request max_output_tokens must be a positive integer"
        )

    extras = validated_request_extras(request_extras)
    body: dict[str, Any] = {
        "model": model.strip(),
        "messages": normalized_messages,
        "max_completion_tokens": max_output_tokens,
    }
    for name, value in extras.fields:
        body[name] = copy.deepcopy(value)

    if reasoning_capability is not None or reasoning_level is not None:
        if reasoning_capability is None or reasoning_level is None:
            raise RequestExtensionError(
                "attaching reasoning requires both an explicitly verified "
                "model-specific capability and its exact reasoning level"
            )
        control = reasoning_capability.control_for(model.strip(), reasoning_level)
        for name, value in control.wire_payload:
            if name in body:
                raise RequestExtensionError(
                    f"verified reasoning control field {name!r} collides with the "
                    "request body"
                )
            body[name] = copy.deepcopy(value)
    return body


def build_provider_request_identity(
    *,
    request_body: Mapping[str, Any],
    base_url: str = PROVIDER_BASE_URL,
) -> ProviderRequestIdentity:
    """Bind one request body to its exact provider/model/endpoint identity."""
    if not isinstance(request_body, Mapping):
        raise CommandCodeProviderError("provider request body must be a mapping")
    body = dict(request_body)
    requested_model = body.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise CommandCodeProviderError(
            "provider request body must carry a non-empty exact model identity"
        )
    assert_benchmark_base_url(base_url)
    return ProviderRequestIdentity(
        provider=PROVIDER_NAME,
        base_url=base_url,
        endpoint_path=CHAT_COMPLETIONS_PATH,
        requested_model=requested_model,
        request_sha256=_sha256_json(body),
    )


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized token usage.  Absent values stay ``None`` (unknown).

    Reasoning-token details are diagnostics only. They remain optional because
    not every Provider/model exposes them, and paid accounting must never
    fabricate an unreported reasoning subtotal.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cached_tokens_exposed: bool
    total_tokens: int | None
    reasoning_tokens: int | None = None
    reasoning_tokens_exposed: bool = False

    @property
    def usage_known(self) -> bool:
        return self.prompt_tokens is not None and self.completion_tokens is not None


def _optional_token(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandCodeProviderError(
            f"provider usage {name} must be an integer when present"
        )
    if value < 0:
        raise CommandCodeProviderError(f"provider usage {name} cannot be negative")
    return value


def _normalize_usage(usage: Any) -> ProviderUsage | None:
    if usage is None:
        return None

    prompt_details = _field(usage, "prompt_tokens_details", None)
    cached: int | None = None
    cached_exposed = False
    if prompt_details is not None:
        raw_cached = _field(prompt_details, "cached_tokens", None)
        if raw_cached is not None:
            cached = _optional_token(raw_cached, "cached_tokens")
            cached_exposed = True

    completion_details = _field(usage, "completion_tokens_details", None)
    reasoning: int | None = None
    reasoning_exposed = False
    if completion_details is not None:
        raw_reasoning = _field(completion_details, "reasoning_tokens", None)
        if raw_reasoning is not None:
            reasoning = _optional_token(raw_reasoning, "reasoning_tokens")
            reasoning_exposed = True

    return ProviderUsage(
        prompt_tokens=_optional_token(_field(usage, "prompt_tokens", None), "prompt_tokens"),
        completion_tokens=_optional_token(
            _field(usage, "completion_tokens", None), "completion_tokens"
        ),
        cached_tokens=cached,
        cached_tokens_exposed=cached_exposed,
        total_tokens=_optional_token(_field(usage, "total_tokens", None), "total_tokens"),
        reasoning_tokens=reasoning,
        reasoning_tokens_exposed=reasoning_exposed,
    )


@dataclass(frozen=True)
class NormalizedProviderResponse:
    """One normalized provider response, preserving exact model identities.

    This is a raw transport-attempt result, not an accepted paid response.  Use
    :func:`accept_provider_response` before treating it as safe accounting.
    """

    requested_model: str
    response_model: str
    provider_response_id: str
    finish_reason: str | None
    content: str
    usage: ProviderUsage | None

    @property
    def usage_known(self) -> bool:
        return self.usage is not None and self.usage.usage_known

    @property
    def response_model_matches_requested(self) -> bool:
        return self.response_model == self.requested_model


def normalize_provider_response(
    raw_response: Any,
    *,
    request_identity: ProviderRequestIdentity,
) -> NormalizedProviderResponse:
    """Normalize one raw provider response without inventing missing data."""
    if not isinstance(request_identity, ProviderRequestIdentity):
        raise CommandCodeProviderError(
            "a ProviderRequestIdentity is required to normalize a provider response"
        )
    choices = _field(raw_response, "choices", None)
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise CommandCodeProviderError(
            "provider response must contain exactly one choice"
        )
    choice = choices[0]
    message = _field(choice, "message", None)
    # The raw visible assistant content is preserved exactly as delivered.  An
    # absent ``content`` field keeps the adapter's documented empty-content
    # behavior, but every present non-string value (``None``, booleans, numbers,
    # lists, objects) fails closed: content is never truthiness-coerced with
    # ``or ""`` and never ``str()``-coerced, so a missing visible translation can
    # never be silently normalized into a usable empty string.
    content: Any = ""
    if message is not None:
        content = _field(message, "content", "")
    if not isinstance(content, str):
        raise CommandCodeProviderError(
            "provider response content must be a string when present"
        )

    finish_reason = _field(choice, "finish_reason", None)
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise CommandCodeProviderError(
            "provider finish_reason must be a string when present"
        )

    response_model = _field(raw_response, "model", "")
    if not isinstance(response_model, str):
        raise CommandCodeProviderError("provider response model must be a string")

    response_id = _field(raw_response, "id", "")
    if not isinstance(response_id, str):
        response_id = ""

    return NormalizedProviderResponse(
        requested_model=request_identity.requested_model,
        response_model=response_model,
        provider_response_id=response_id,
        finish_reason=finish_reason,
        content=content,
        usage=_normalize_usage(_field(raw_response, "usage", None)),
    )


def assert_provider_identity(
    request_identity: ProviderRequestIdentity,
    response: NormalizedProviderResponse,
) -> str:
    """Fail closed unless the returned model exactly matches the request."""
    if response.response_model != request_identity.requested_model:
        raise ProviderIdentityError(
            "provider model identity mismatch: requested "
            f"{request_identity.requested_model!r}, provider reported "
            f"{response.response_model!r}"
        )
    return response.response_model


def require_provider_usage(response: NormalizedProviderResponse) -> ProviderUsage:
    """Return known usage, or fail when paid accounting would otherwise guess."""
    if response.usage is None or not response.usage.usage_known:
        raise MissingProviderUsageError(
            "provider response omitted usage; paid accounting is unknown and must "
            "never be fabricated"
        )
    return response.usage


@dataclass(frozen=True)
class AcceptedProviderResponse:
    """A paid response that passed exact model-identity and usage requirements."""

    requested_model: str
    response_model: str
    usage: ProviderUsage
    response: NormalizedProviderResponse

    @property
    def content(self) -> str:
        return self.response.content

    @property
    def finish_reason(self) -> str | None:
        return self.response.finish_reason

    @property
    def provider_response_id(self) -> str:
        return self.response.provider_response_id


def accept_provider_response(
    request_identity: ProviderRequestIdentity,
    response: NormalizedProviderResponse,
) -> AcceptedProviderResponse:
    """Fail-closed paid acceptance: exact model identity plus complete usage.

    A raw single transport attempt is not acceptance.  This helper refuses a
    response whose returned model differs from the request or whose usage is
    missing or partial, so a paid benchmark caller can never proceed on guessed
    accounting.
    """
    if not isinstance(response, NormalizedProviderResponse):
        raise CommandCodeProviderError(
            "an accepted provider response requires a normalized response"
        )
    model = assert_provider_identity(request_identity, response)
    usage = require_provider_usage(response)
    return AcceptedProviderResponse(
        requested_model=request_identity.requested_model,
        response_model=model,
        usage=usage,
        response=response,
    )


def send_chat_completion_once(client: Any, request_body: Mapping[str, Any]) -> Any:
    """Perform exactly one raw provider call.  No retries, no fallback."""
    if client is None:
        raise CommandCodeProviderError("a provider client is required")
    if not isinstance(request_body, Mapping):
        raise CommandCodeProviderError("provider request body must be a mapping")
    return client.chat.completions.create(**dict(request_body))


def perform_single_provider_request(
    client: Any,
    *,
    request_body: Mapping[str, Any],
    base_url: str = PROVIDER_BASE_URL,
) -> NormalizedProviderResponse:
    """Make one application-visible transport attempt and normalize the result.

    This is transport only and is deliberately NOT paid acceptance: it neither
    verifies the returned model identity nor requires usage.  Before any real
    paid request, the transport attempt MUST execute underneath the repository's
    existing durable paid-request journal/controller boundary; ``max_retries=0``
    alone does not make an ambiguous remote outcome safe.  Callers must then use
    :func:`accept_provider_response` to obtain a fail-closed accepted response.
    """
    identity = build_provider_request_identity(
        request_body=request_body,
        base_url=base_url,
    )
    raw_response = send_chat_completion_once(client, request_body)
    return normalize_provider_response(raw_response, request_identity=identity)


def _default_client_factory(**kwargs: Any) -> Any:
    """Lazily import and construct the OpenAI-compatible client."""
    from openai import OpenAI

    return OpenAI(**kwargs)


def create_provider_client(
    *,
    api_key: str,
    base_url: str = PROVIDER_BASE_URL,
    client_factory=None,
) -> Any:
    """Construct the Command Code Provider client with an injected API key.

    The key is injected explicitly at runtime.  This function reads no
    environment variables and is never called at import.  Only the exact
    benchmark base URL is accepted.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise CommandCodeProviderError(
            "provider API key must be injected explicitly at runtime"
        )
    assert_benchmark_base_url(base_url)
    factory = client_factory if client_factory is not None else _default_client_factory
    return factory(api_key=api_key, base_url=base_url, max_retries=0)
