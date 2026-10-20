"""Production Command Code Provider transport adapter (GOAT inference).

This module supplies **transport and provider-specific request/response
normalization only**. It is NOT the durable paid journal: every real paid
request must still execute underneath the production journal
(``production_paid_request_journal.run_journaled_paid_request``).

It reuses the reviewed GOAT benchmark components rather than re-implementing
provider behavior:

* ``benchmark_longitudinal_provider`` — OpenAI-compatible client, request
  builder, response normalizer, exact model-identity and usage acceptance;
* ``benchmark_longitudinal_capability_results`` — the live-verified reasoning
  capability for ``deepseek/deepseek-v4.1-flash``;
* ``production_goat_policy`` — the current 50K primary translation policy;
* ``benchmark_longitudinal_g7j_reasoning_budget`` — unchanged auxiliary budgets;
* ``benchmark_longitudinal_g7d_accounting_policy`` — credit-equivalent
  accounting.

Import-safe: no IO, no credential reads, no network at import time.
"""
from benchmark_longitudinal_capability_results import verified_reasoning_capability
from benchmark_longitudinal_config import (
    CHAT_COMPLETIONS_PATH,
    PROVIDER_BASE_URL,
    PROVIDER_NAME,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    PRICING_UNIT as GOAT_PRICING_UNIT,
    account_provider_usage,
)
from benchmark_longitudinal_g7j_reasoning_budget import (
    goat_auxiliary_stage1_runtime_completion_budget,
)
from production_goat_policy import translation_completion_budget
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)

from production_provider_contract import (
    COMMAND_CODE_DEEPSEEK_V41_FLASH,
    COMMAND_CODE_TRANSLATION_REASONING_EFFORT,
    PROVIDER_COMMAND_CODE,
    resolve_provider_backend,
)


class CommandCodeTransportError(RuntimeError):
    """The GOAT transport could not build a safe, verified request."""


def create_command_code_client(*, api_key, client_factory=None):
    """Construct the GOAT client with an explicitly injected key.

    Reuses the reviewed builder: exact base URL, ``max_retries=0``, no env read.
    """
    return create_provider_client(
        api_key=api_key, base_url=PROVIDER_BASE_URL, client_factory=client_factory
    )


def _translation_reasoning(model_id):
    capability = verified_reasoning_capability(model_id)
    if capability is None:
        raise CommandCodeTransportError(
            f"no live-verified reasoning capability for GOAT model {model_id!r}"
        )
    level = COMMAND_CODE_TRANSLATION_REASONING_EFFORT
    if level not in capability.levels:
        raise CommandCodeTransportError(
            f"frozen GOAT translation reasoning effort {level!r} is not a "
            f"verified level for {model_id!r} ({capability.levels})"
        )
    return capability, level


def build_command_code_translation_request(*, messages, request_extras=None):
    """Build the exact GOAT primary-translation request body.

    Uses the corrected GOAT production completion budget (50k) and the frozen verified
    translation reasoning effort. The wire model is fixed to the GOAT DeepSeek
    identity via the provider contract; there is no fallback.
    """
    backend = resolve_provider_backend(
        PROVIDER_COMMAND_CODE, wire_model=COMMAND_CODE_DEEPSEEK_V41_FLASH
    )
    capability, level = _translation_reasoning(capability_model_id(backend))
    budget = translation_completion_budget(
        logical_operation="translation",
        requested_tokens=backend.translation_completion_tokens,
        reasoning_effort=level,
        goat=True,
    )
    return build_provider_request(
        model=backend.wire_model,
        messages=messages,
        max_output_tokens=budget,
        request_extras=request_extras,
        reasoning_capability=capability,
        reasoning_level=level,
    )


def capability_model_id(backend):
    """The exact model id used for the live-verified reasoning capability."""
    return backend.wire_model


def auxiliary_completion_budget(*, logical_operation, requested_tokens):
    """Corrected GOAT Stage-1 runtime auxiliary completion ceiling (8k)."""
    return goat_auxiliary_stage1_runtime_completion_budget(
        logical_operation=logical_operation, requested_tokens=requested_tokens
    )


def command_code_chat_completions_url():
    return PROVIDER_BASE_URL.rstrip("/") + CHAT_COMPLETIONS_PATH


def send_command_code_request_once(client, request_body):
    """Exactly one raw transport send, normalized; no retries, no fallback."""
    identity = build_provider_request_identity(request_body=request_body)
    raw = send_chat_completion_once(client, request_body)
    normalized = normalize_provider_response(raw, request_identity=identity)
    return identity, normalized


def accept_command_code_response(identity, normalized):
    """Fail-closed paid acceptance: exact model identity plus complete usage."""
    return accept_provider_response(identity, normalized)


def account_command_code_usage(model_id, usage, request_started_utc):
    """Credit-equivalent accounting for one accepted GOAT response."""
    return account_provider_usage(model_id, usage, request_started_utc)


def transport_identity():
    """Immutable GOAT transport identity for authorization binding."""
    backend = resolve_provider_backend(
        PROVIDER_COMMAND_CODE, wire_model=COMMAND_CODE_DEEPSEEK_V41_FLASH
    )
    return {
        "provider": PROVIDER_NAME,
        "base_url": PROVIDER_BASE_URL,
        "chat_completions_path": CHAT_COMPLETIONS_PATH,
        "wire_model": backend.wire_model,
        "reasoning_effort": backend.reasoning_effort,
        "translation_completion_tokens": backend.translation_completion_tokens,
        "auxiliary_completion_tokens": backend.auxiliary_completion_tokens,
        "accounting": backend.accounting,
        "pricing_unit": GOAT_PRICING_UNIT,
    }
