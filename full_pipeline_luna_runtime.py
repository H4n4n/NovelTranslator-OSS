"""Experiment-scoped GPT-5.6 Luna runtime for the real production engine.

This adapter exists only for the full-pipeline Batch experiment.  It deliberately
keeps GPT-5.6 Luna out of ordinary production model selection while allowing the
actual NovelTranslator downstream engine (QA, repairs, state extraction,
glossary review, audits, recovery, and commits) to run against Luna.

The adapter has three responsibilities:

1. Load the real legacy engine without the normal DeepSeek provider-identity
   launcher wrapper.
2. Initialize all real project/profile/glossary/checkpoint state while
   suppressing provider credential/client construction.
3. Install a Luna synchronous auxiliary transport for state/review/repair/audit
   calls.  Legacy DeepSeek-shaped request fields are normalized at the provider
   boundary only; engine prompts and checkpoint machinery are unchanged.

Importing this module performs no credential loading, provider calls, or file
writes.  Provider access occurs only through explicitly invoked helpers.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import uuid
from typing import Any, Mapping

import noveltranslator_core as core
from catastrophic_completeness_runtime import install_catastrophic_completeness_qa
from full_pipeline_batch_wave import (
    BATCH_MODEL,
    BATCH_PRICING_VERSION,
    BATCH_CACHE_HIT_RATE,
    BATCH_CACHE_MISS_RATE,
    BATCH_OUTPUT_RATE,
)
from semantic_shadow_runtime import install_semantic_shadow_qa


LUNA_MODEL = BATCH_MODEL
LUNA_MODEL_KEY = "luna"
LUNA_MODEL_VERSION = "gpt-5.6-luna"
LUNA_REASONING = "none"
LUNA_MAX_TOKENS = 12000
LUNA_STANDARD_PRICING_VERSION = "openai-gpt-5.6-luna-standard-0.20-0.02-1.20"
LUNA_STANDARD_CACHE_HIT_RATE = 0.02
LUNA_STANDARD_CACHE_MISS_RATE = 0.20
LUNA_STANDARD_OUTPUT_RATE = 1.20
LUNA_RUNTIME_MODE = "full_pipeline_luna_auxiliary_v1"
LUNA_QA_PROVENANCE_MODE = "full_pipeline_luna_batch_qa_provenance_v1"

_ACCOUNTING_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "rate_correct_cost_usd",
)
_KNOWN_PRICING = {
    LUNA_STANDARD_PRICING_VERSION: {
        "pricing_period": "standard",
        "cache_hit_rate_per_million": LUNA_STANDARD_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": LUNA_STANDARD_CACHE_MISS_RATE,
        "output_rate_per_million": LUNA_STANDARD_OUTPUT_RATE,
    },
    BATCH_PRICING_VERSION: {
        "pricing_period": "batch",
        "cache_hit_rate_per_million": BATCH_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": BATCH_CACHE_MISS_RATE,
        "output_rate_per_million": BATCH_OUTPUT_RATE,
    },
}


class FullPipelineLunaRuntimeError(RuntimeError):
    """Experiment runtime state or provider evidence is unsafe."""


class LunaProviderIdentityError(FullPipelineLunaRuntimeError):
    """A synchronous auxiliary response did not identify exact GPT-5.6 Luna."""


class _OSProxy:
    """Module-local os proxy that suppresses only the DeepSeek key lookup."""

    def __init__(self, original):
        self._original = original

    def getenv(self, name, default=None):
        if name == "DEEPSEEK_API_KEY":
            return "provider-suppressed-for-full-pipeline-experiment"
        return self._original.getenv(name, default)

    def __getattr__(self, name):
        return getattr(self._original, name)


class _SuppressedProviderClient:
    """Sentinel proving initializer provider construction never became usable."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FullPipelineLunaRuntimeError(
            "Luna request identity is not canonically JSON-serializable"
        ) from exc


def load_experiment_engine_module(
    production_script: Path | str,
    *,
    reporter=None,
    module_name: str | None = None,
):
    """Load the real production module without normal production provider wiring.

    The authoritative catastrophic-completeness hard gate and semantic shadow
    are installed on the import-safe core *before* the legacy module captures
    ``translation_commit_qa``.  Provider-model identity is intentionally not
    installed here because its normal production policy knows only supported
    DeepSeek production models.
    """
    path = Path(production_script).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    completeness = install_catastrophic_completeness_qa(core)
    semantic_shadow = install_semantic_shadow_qa(
        core,
        reporter=reporter,
        include_experimental=False,
    )
    name = module_name or f"_noveltranslator_full_pipeline_{uuid.uuid4().hex}"
    if name in sys.modules:
        raise FullPipelineLunaRuntimeError(
            f"experiment engine module name is already loaded: {name}"
        )
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create production module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        for required in (
            "initialize_production_runtime_state",
            "process_chapter",
            "run_unattended_production",
            "run_due_audits",
        ):
            if not callable(getattr(module, required, None)):
                raise FullPipelineLunaRuntimeError(
                    f"production module lacks callable {required}()"
                )
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return {
        "module": module,
        "module_name": name,
        "completeness": completeness,
        "semantic_shadow": semantic_shadow,
    }


def initialize_engine_without_provider(production_module):
    """Initialize the real engine locally without reading/constructing a provider.

    Use the engine's provider-neutral state-loading boundary. The suppression
    probes remain in place to detect an unexpected credential/client bootstrap;
    neither the GOAT nor direct provider is activated by this experiment helper.
    """
    initializer = getattr(
        production_module,
        "initialize_production_runtime_state",
        None,
    )
    if not callable(initializer):
        raise FullPipelineLunaRuntimeError(
            "production module has no runtime initializer"
        )
    if getattr(production_module, "_RUNTIME_INITIALIZED", False):
        raise FullPipelineLunaRuntimeError(
            "production module is already initialized"
        )

    original_load_dotenv = production_module.load_dotenv
    original_os = production_module.os
    original_openai = production_module.OpenAI
    sentinel = _SuppressedProviderClient()
    provider_constructor_calls = []

    def suppressed_openai(*args, **kwargs):
        provider_constructor_calls.append(
            {"args": len(args), "kwargs": sorted(kwargs)}
        )
        return sentinel

    production_module.load_dotenv = lambda *args, **kwargs: False
    production_module.os = _OSProxy(original_os)
    production_module.OpenAI = suppressed_openai
    try:
        initializer(activate_provider=False)
    finally:
        production_module.load_dotenv = original_load_dotenv
        production_module.os = original_os
        production_module.OpenAI = original_openai

    if production_module.client is not None:
        raise FullPipelineLunaRuntimeError(
            "provider-neutral initializer retained a usable client"
        )
    if provider_constructor_calls:
        raise FullPipelineLunaRuntimeError(
            "provider-neutral initializer attempted provider construction"
        )
    production_module.api_key = None
    production_module.client = None
    return {
        "status": "initialized_without_provider",
        "provider_constructor_calls": len(provider_constructor_calls),
        "runtime_initialized": bool(
            getattr(production_module, "_RUNTIME_INITIALIZED", False)
        ),
        "client": None,
        "api_key_loaded": False,
    }


def create_openai_client():
    """Create the experiment's synchronous Luna client only when explicitly called."""
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise FullPipelineLunaRuntimeError(
            "OPENAI_API_KEY was not found in .env"
        )
    return OpenAI(api_key=api_key, max_retries=0)


def normalize_luna_request(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the engine's legacy DeepSeek transport fields to Luna fields.

    Prompt/messages are copied unchanged.  Only provider transport controls are
    normalized.
    """
    if not isinstance(request_kwargs, Mapping):
        raise FullPipelineLunaRuntimeError("provider request must be an object")
    request = copy.deepcopy(dict(request_kwargs))
    requested_model = request.pop("model", None)
    if requested_model != LUNA_MODEL:
        raise FullPipelineLunaRuntimeError(
            f"auxiliary request model must be exact {LUNA_MODEL!r}; "
            f"found {requested_model!r}"
        )

    legacy_max = request.pop("max_tokens", None)
    luna_max = request.pop("max_completion_tokens", None)
    if legacy_max is not None and luna_max is not None and legacy_max != luna_max:
        raise FullPipelineLunaRuntimeError(
            "request contains conflicting max_tokens/max_completion_tokens"
        )
    max_tokens = luna_max if luna_max is not None else legacy_max
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise FullPipelineLunaRuntimeError(
            "Luna auxiliary request requires positive output-token limit"
        )

    extra_body = request.pop("extra_body", None)
    if extra_body is not None:
        expected = {"thinking": {"type": "disabled"}}
        if extra_body != expected:
            raise FullPipelineLunaRuntimeError(
                "unexpected DeepSeek extra_body in Luna auxiliary request"
            )
    reasoning = request.pop("reasoning_effort", None)
    if reasoning not in (None, LUNA_REASONING):
        raise FullPipelineLunaRuntimeError(
            f"Luna auxiliary reasoning must be {LUNA_REASONING!r}"
        )
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise FullPipelineLunaRuntimeError(
            "Luna auxiliary request requires messages"
        )
    return {
        "model": LUNA_MODEL,
        **request,
        "max_completion_tokens": max_tokens,
        "reasoning_effort": LUNA_REASONING,
    }


def luna_standard_pricing_snapshot(moment=None) -> dict[str, Any]:
    moment = utc_now() if moment is None else moment
    if not isinstance(moment, datetime):
        raise FullPipelineLunaRuntimeError(
            "Luna pricing snapshot moment must be a datetime"
        )
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise FullPipelineLunaRuntimeError(
            "Luna pricing snapshot moment must be timezone-aware"
        )
    return {
        "pricing_version": LUNA_STANDARD_PRICING_VERSION,
        "pricing_period": "standard",
        "pricing_mode": "synchronous",
        "request_started_utc": moment.astimezone(timezone.utc).isoformat(),
        "cache_hit_rate_per_million": LUNA_STANDARD_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": LUNA_STANDARD_CACHE_MISS_RATE,
        "output_rate_per_million": LUNA_STANDARD_OUTPUT_RATE,
    }


def _usage_value(usage: Any, name: str) -> int:
    value = _field(usage, name, 0) or 0
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FullPipelineLunaRuntimeError(
            f"provider usage {name} is not an integer"
        ) from exc
    if result < 0:
        raise FullPipelineLunaRuntimeError(
            f"provider usage {name} cannot be negative"
        )
    return result


def _usage_stats(usage: Any, pricing: Mapping[str, Any]) -> dict[str, Any]:
    if usage is None:
        raise FullPipelineLunaRuntimeError(
            "Luna response omitted usage; paid accounting is unknown"
        )
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    details = _field(usage, "prompt_tokens_details", {}) or {}
    cache_hits = _usage_value(details, "cached_tokens")
    if cache_hits > prompt_tokens:
        raise FullPipelineLunaRuntimeError(
            "cached prompt tokens exceed total prompt tokens"
        )
    cache_misses = prompt_tokens - cache_hits
    cost = (
        cache_hits / 1_000_000 * pricing["cache_hit_rate_per_million"]
        + cache_misses / 1_000_000 * pricing["cache_miss_rate_per_million"]
        + completion_tokens / 1_000_000 * pricing["output_rate_per_million"]
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        **copy.deepcopy(dict(pricing)),
    }


def _is_unpriced_zero(stats: Mapping[str, Any]) -> bool:
    return all((stats.get(field, 0) or 0) == 0 for field in _ACCOUNTING_FIELDS)


def validate_saved_luna_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Replay exact saved Batch/Standard Luna accounting without repricing."""
    if not isinstance(stats, Mapping):
        raise FullPipelineLunaRuntimeError("saved Luna stats must be an object")
    value = copy.deepcopy(dict(stats))
    for field in _ACCOUNTING_FIELDS:
        number = value.get(field, 0)
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or number < 0
        ):
            raise FullPipelineLunaRuntimeError(
                f"saved Luna stats {field} must be non-negative numeric"
            )
    if _is_unpriced_zero(value) and not value.get("pricing_version"):
        return value

    prompt_tokens = value["prompt_tokens"]
    if value["cache_hit_tokens"] + value["cache_miss_tokens"] != prompt_tokens:
        raise FullPipelineLunaRuntimeError(
            "saved Luna cache-hit + cache-miss tokens do not equal prompt tokens"
        )
    pricing_version = value.get("pricing_version")
    expected = _KNOWN_PRICING.get(pricing_version)
    if expected is None:
        raise FullPipelineLunaRuntimeError(
            f"saved stats use unrecognized Luna pricing version {pricing_version!r}"
        )
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise FullPipelineLunaRuntimeError(
                f"saved Luna stats {field} does not match {pricing_version}"
            )
    expected_cost = (
        value["cache_hit_tokens"] / 1_000_000
        * expected["cache_hit_rate_per_million"]
        + value["cache_miss_tokens"] / 1_000_000
        * expected["cache_miss_rate_per_million"]
        + value["completion_tokens"] / 1_000_000
        * expected["output_rate_per_million"]
    )
    if abs(value["cost_usd"] - expected_cost) > 1e-12:
        raise FullPipelineLunaRuntimeError(
            "saved Luna cost does not match saved tokens/rates"
        )
    if abs(value["rate_correct_cost_usd"] - expected_cost) > 1e-12:
        raise FullPipelineLunaRuntimeError(
            "saved Luna rate-correct cost does not match saved tokens/rates"
        )
    return value


def _validate_response_model(response: Any) -> str:
    observed = _field(response, "model", "")
    if not isinstance(observed, str) or observed.strip() != LUNA_MODEL:
        raise LunaProviderIdentityError(
            f"Luna provider model identity mismatch: expected {LUNA_MODEL!r}, "
            f"found {observed!r}"
        )
    return observed.strip()


def install_luna_auxiliary_runtime(
    production_module,
    client,
    *,
    now_utc=utc_now,
):
    """Install one single-attempt Luna transport under the real engine callbacks."""
    if not getattr(production_module, "_RUNTIME_INITIALIZED", False):
        raise FullPipelineLunaRuntimeError(
            "engine must be locally initialized before Luna runtime installation"
        )
    if client is None:
        raise FullPipelineLunaRuntimeError("Luna client is required")
    existing = getattr(production_module, "_full_pipeline_luna_runtime_state", None)
    if isinstance(existing, dict):
        if existing.get("client") is not client:
            raise FullPipelineLunaRuntimeError(
                "Luna runtime is already installed with a different client"
            )
        return existing

    originals = {
        name: getattr(production_module, name)
        for name in (
            "send_api_request_once",
            "call_api_with_retries",
            "pricing_snapshot",
            "pricing_period_for_utc",
            "usage_and_cost_for_pricing",
            "usage_and_cost",
            "reprice_saved_stats_if_needed",
            "journaled_chat_request",
            "wait_for_api_permission",
        )
    }

    def pricing_snapshot(moment=None):
        return luna_standard_pricing_snapshot(
            now_utc() if moment is None else moment
        )

    def pricing_period_for_utc(moment=None):
        return "standard"

    def raw_send_actual(actual_request):
        return client.chat.completions.create(**actual_request)

    def send_api_request_once(**kwargs):
        actual = normalize_luna_request(kwargs)
        response = raw_send_actual(actual)
        _validate_response_model(response)
        return response

    def usage_and_cost_for_pricing(usage, pricing_info):
        return _usage_stats(usage, pricing_info)

    def usage_and_cost(usage):
        pricing = (
            production_module.LAST_SUCCESSFUL_API_PRICING
            or pricing_snapshot()
        )
        return _usage_stats(usage, pricing)

    def reprice_saved_stats_if_needed(stats):
        return validate_saved_luna_stats(stats)

    def call_api_with_retries(**kwargs):
        # Despite the legacy name, the experiment performs exactly one provider
        # attempt.  The OpenAI client itself is required to use max_retries=0.
        pricing = pricing_snapshot()
        response = send_api_request_once(**kwargs)
        production_module.LAST_SUCCESSFUL_API_PRICING = pricing
        return response

    def journaled_chat_request(
        operation_kind,
        chapter_number,
        source_text,
        request_kwargs,
        *,
        translation_text="",
    ):
        actual_request = normalize_luna_request(request_kwargs)
        novel_id = (
            production_module.ACTIVE_NOVEL_CONFIG.id
            if production_module.ACTIVE_NOVEL_CONFIG is not None
            else "ming_novel"
        )
        profile_id = (
            getattr(production_module.ACTIVE_PROFILE, "id", "")
            or "historical_military"
        )
        source_sha256 = _sha256_text(source_text)
        translation_sha256 = (
            _sha256_text(translation_text) if translation_text else ""
        )
        identity = production_module.request_identity(
            operation_kind=operation_kind,
            novel_id=novel_id,
            chapter=chapter_number,
            provider="openai",
            model=LUNA_MODEL,
            profile_id=profile_id,
            source_sha256=source_sha256,
            translation_sha256=translation_sha256,
            request_payload=actual_request,
        )
        journal_path = production_module.paid_request_journal_path(
            production_module.PAID_REQUEST_JOURNAL_DIR,
            operation_kind,
            chapter_number,
        )

        def prepare_attempt():
            return pricing_snapshot()

        def normalize_response(response, attempt_pricing):
            choices = _field(response, "choices", None)
            if not isinstance(choices, (list, tuple)) or len(choices) != 1:
                raise FullPipelineLunaRuntimeError(
                    "journaled Luna response must contain exactly one choice"
                )
            choice = choices[0]
            message = _field(choice, "message", None)
            content = _field(message, "content", "") if message is not None else ""
            return {
                "content": content or "",
                "finish_reason": _field(choice, "finish_reason", None),
                "stats": _usage_stats(
                    _field(response, "usage", None),
                    attempt_pricing,
                ),
                "provider_response_id": _field(response, "id", "") or "",
                "provider_response_model": _field(response, "model", "") or "",
                "provider_system_fingerprint": (
                    _field(response, "system_fingerprint", "") or ""
                ),
            }

        result = production_module.run_journaled_paid_request(
            journal_path=journal_path,
            identity=identity,
            save_json=lambda path, value: production_module.atomic_write_json(
                path, value
            ),
            prepare_attempt=prepare_attempt,
            send_once=lambda: raw_send_actual(actual_request),
            normalize_response=normalize_response,
        )
        response = result.get("response") if isinstance(result, dict) else None
        if not isinstance(response, dict):
            raise FullPipelineLunaRuntimeError(
                "Luna paid-request journal returned no durable response envelope"
            )
        observed = response.get("provider_response_model")
        if observed != LUNA_MODEL:
            replay_state = "replayed" if result.get("replayed") else "newly recorded"
            raise LunaProviderIdentityError(
                f"Luna provider model identity mismatch: expected {LUNA_MODEL!r}, "
                f"found {observed!r}; paid response is {replay_state} in the "
                "durable request journal, so automatic provider reissue is forbidden"
            )
        return result

    production_module.MODEL = LUNA_MODEL
    production_module.client = client
    production_module.api_key = None
    production_module.AUTO_OFF_PEAK_ONLY = False
    production_module.V4_PRO_PRICES = {
        "off_peak": {
            "cache_hit": LUNA_STANDARD_CACHE_HIT_RATE,
            "cache_miss": LUNA_STANDARD_CACHE_MISS_RATE,
            "output": LUNA_STANDARD_OUTPUT_RATE,
        },
        "peak": {
            "cache_hit": LUNA_STANDARD_CACHE_HIT_RATE,
            "cache_miss": LUNA_STANDARD_CACHE_MISS_RATE,
            "output": LUNA_STANDARD_OUTPUT_RATE,
        },
    }
    production_module.PRICING_VERSION = LUNA_STANDARD_PRICING_VERSION
    production_module.wait_for_api_permission = lambda: None
    production_module.pricing_snapshot = pricing_snapshot
    production_module.pricing_period_for_utc = pricing_period_for_utc
    production_module.send_api_request_once = send_api_request_once
    production_module.call_api_with_retries = call_api_with_retries
    production_module.usage_and_cost_for_pricing = usage_and_cost_for_pricing
    production_module.usage_and_cost = usage_and_cost
    production_module.reprice_saved_stats_if_needed = reprice_saved_stats_if_needed
    production_module.journaled_chat_request = journaled_chat_request
    production_module.LAST_SUCCESSFUL_API_PRICING = None

    state = {
        "mode": LUNA_RUNTIME_MODE,
        "model": LUNA_MODEL,
        "reasoning": LUNA_REASONING,
        "client": client,
        "pricing_version": LUNA_STANDARD_PRICING_VERSION,
        "originals": originals,
        "normalize_request": normalize_luna_request,
    }
    production_module._full_pipeline_luna_runtime_state = state
    return state


def install_luna_batch_qa_provenance(
    production_module,
    qa_dir: Path | str,
):
    """Persist exact Batch translation provenance in committed QA JSON."""
    qa_root = Path(qa_dir).resolve()
    current = getattr(production_module, "atomic_write_json", None)
    if not callable(current):
        raise FullPipelineLunaRuntimeError(
            "production module has no atomic JSON writer"
        )
    existing = getattr(current, "_full_pipeline_luna_qa_provenance", None)
    if isinstance(existing, dict):
        if existing.get("qa_root") != qa_root:
            raise FullPipelineLunaRuntimeError(
                "Luna QA provenance is already installed for another QA root"
            )
        return existing

    provenance = {
        "translation_provider": "openai",
        "translation_transport": "openai_batch",
        "translation_model": LUNA_MODEL,
        "translation_model_key": LUNA_MODEL_KEY,
        "translation_model_version": LUNA_MODEL_VERSION,
        "translation_thinking": LUNA_REASONING,
        "translation_max_tokens": LUNA_MAX_TOKENS,
        "translation_pricing_version": BATCH_PRICING_VERSION,
    }
    original = current

    def wrapped_atomic_write_json(path, data):
        candidate = Path(path)
        try:
            is_qa = (
                candidate.resolve().parent == qa_root
                and candidate.suffix == ".json"
                and candidate.stem.isdigit()
            )
        except OSError:
            is_qa = False
        payload = data
        if is_qa:
            if not isinstance(data, dict):
                raise FullPipelineLunaRuntimeError(
                    "committed QA must be written from a dictionary"
                )
            payload = dict(data)
            payload.update(provenance)
        return original(path, payload)

    state = {
        "mode": LUNA_QA_PROVENANCE_MODE,
        "qa_root": qa_root,
        "provenance": dict(provenance),
        "original_atomic_write_json": original,
        "wrapped_atomic_write_json": wrapped_atomic_write_json,
    }
    wrapped_atomic_write_json._full_pipeline_luna_qa_provenance = state
    production_module.atomic_write_json = wrapped_atomic_write_json
    return state


def luna_batch_pending_provenance() -> dict[str, Any]:
    """Return immutable fields to bind experiment pending text to Batch Luna."""
    return {
        "translation_provider": "openai",
        "translation_transport": "openai_batch",
        "translation_model": LUNA_MODEL,
        "translation_model_key": LUNA_MODEL_KEY,
        "translation_model_version": LUNA_MODEL_VERSION,
        "translation_thinking": LUNA_REASONING,
        "translation_max_tokens": LUNA_MAX_TOKENS,
        "translation_pricing_version": BATCH_PRICING_VERSION,
    }
