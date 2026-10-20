"""Import-safe DeepSeek translation-model configuration.

This module contains only immutable model/request/pricing metadata and pure
validation helpers. It must never read credentials, call an API, or mutate
project state.

The production default remains DeepSeek V4 Pro until the operator explicitly
selects another model. Provider-routing cutoffs are represented here so the
runtime can fail closed before a paid request would be sent through an alias
that no longer guarantees the configured model identity.
"""

from dataclasses import dataclass
from datetime import datetime, timezone


DEFAULT_TRANSLATION_MODEL = "deepseek-v4-pro"
TRANSLATION_MODEL_ENV = "NOVELTRANSLATOR_TRANSLATION_MODEL"

# DeepSeek announced that requests to the V4 Pro API alias will route to
# V4.1 Flash beginning at this instant and until V4.1 Pro is released.
DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC = "2026-09-14T04:00:00+00:00"


class TranslationModelRoutingError(ValueError):
    """A configured API model cannot safely guarantee its recorded identity."""


@dataclass(frozen=True)
class TranslationModelConfig:
    key: str
    model: str
    version: str
    thinking: str
    max_tokens: int
    pricing_version: str
    prices: dict
    new_requests_allowed: bool = True
    request_cutoff_utc: str = ""


MODEL_CONFIGS = {
    "deepseek-v4-pro": TranslationModelConfig(
        key="pro",
        model="deepseek-v4-pro",
        version="DeepSeek-V4-Pro-0813",
        thinking="disabled",
        max_tokens=12000,
        pricing_version="deepseek-v4-pro-0813-peak-offpeak",
        prices={
            "off_peak": {
                "cache_hit": 0.022,
                "cache_miss": 0.66,
                "output": 1.98,
            },
            "peak": {
                "cache_hit": 0.044,
                "cache_miss": 1.32,
                "output": 3.96,
            },
        },
        request_cutoff_utc=DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC,
    ),
    # Preserve the historical V4 Flash identity for already-paid pending
    # recovery. DeepSeek now routes this legacy alias to V4.1 Flash, so it must
    # never be used for a new paid request.
    "deepseek-v4-flash": TranslationModelConfig(
        key="flash",
        model="deepseek-v4-flash",
        version="DeepSeek-V4-Flash-0731",
        thinking="disabled",
        max_tokens=12000,
        pricing_version="deepseek-v4-flash-0731-peak-offpeak",
        prices={
            "off_peak": {
                "cache_hit": 0.007,
                "cache_miss": 0.22,
                "output": 0.66,
            },
            "peak": {
                "cache_hit": 0.014,
                "cache_miss": 0.44,
                "output": 1.32,
            },
        },
        new_requests_allowed=False,
    ),
    "deepseek-v4.1-flash": TranslationModelConfig(
        key="v41-flash",
        model="deepseek-flash",
        version="DeepSeek-V4.1-Flash",
        thinking="disabled",
        max_tokens=12000,
        pricing_version="deepseek-v4.1-flash-2026-09-10-peak-offpeak",
        prices={
            "off_peak": {
                "cache_hit": 0.003,
                "cache_miss": 0.15,
                "output": 0.60,
            },
            "peak": {
                "cache_hit": 0.006,
                "cache_miss": 0.30,
                "output": 1.20,
            },
        },
    ),
}

MODEL_ALIASES = {
    "pro": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    # Historical aliases intentionally keep their old meaning for recovery.
    "flash": "deepseek-v4-flash",
    "v4-flash": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    # V4.1 Flash requires an explicit route-safe selection.
    "v41-flash": "deepseek-v4.1-flash",
    "v4.1-flash": "deepseek-v4.1-flash",
    "deepseek-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
}


def resolve_translation_model(selection=None):
    """Return one validated immutable model configuration.

    ``None`` intentionally resolves to Pro so importing this module cannot
    silently change production behavior.
    """
    raw = DEFAULT_TRANSLATION_MODEL if selection is None else selection
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("translation model selection must be a non-empty string")
    normalized = raw.strip().lower()
    model = MODEL_ALIASES.get(normalized)
    if model is None:
        allowed = ", ".join(sorted(MODEL_ALIASES))
        raise ValueError(
            f"unsupported translation model {raw!r}; expected one of: {allowed}"
        )
    return MODEL_CONFIGS[model]


def pricing_rates_for_model(selection, period):
    """Return official per-million rates for ``peak`` or ``off_peak``."""
    config = resolve_translation_model(selection)
    if period not in config.prices:
        raise ValueError("pricing period must be 'peak' or 'off_peak'")
    return dict(config.prices[period])


def _aware_utc(moment=None):
    value = datetime.now(timezone.utc) if moment is None else moment
    if not isinstance(value, datetime):
        raise ValueError("request moment must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("request moment must be timezone-aware")
    return value.astimezone(timezone.utc)


def assert_translation_request_allowed(selection=None, *, moment=None):
    """Fail closed before a provider call whose alias cannot prove identity."""
    config = resolve_translation_model(selection)
    now = _aware_utc(moment)
    if not config.new_requests_allowed:
        raise TranslationModelRoutingError(
            f"new paid requests are disabled for legacy model {config.model!r}; "
            "select 'v41-flash' / 'deepseek-flash' for DeepSeek V4.1 Flash"
        )
    if config.request_cutoff_utc:
        cutoff = datetime.fromisoformat(config.request_cutoff_utc).astimezone(timezone.utc)
        if now >= cutoff:
            raise TranslationModelRoutingError(
                f"new paid requests for {config.model!r} are blocked at/after "
                f"{config.request_cutoff_utc} because the provider announced alias "
                "routing that no longer guarantees this configured model identity; "
                "select an explicit currently supported model instead"
            )
    return config


def validate_provider_response_model(selection, response_model):
    """Require the provider-reported API model to equal the requested API model."""
    config = resolve_translation_model(selection)
    if not isinstance(response_model, str) or not response_model.strip():
        raise TranslationModelRoutingError(
            f"provider response for requested model {config.model!r} did not report "
            "a model identity"
        )
    observed = response_model.strip()
    if observed != config.model:
        raise TranslationModelRoutingError(
            f"provider model identity mismatch: requested {config.model!r}, "
            f"provider reported {observed!r}"
        )
    return observed


def pending_translation_model_record(selection=None):
    """Return immutable provenance fields suitable for pending metadata."""
    config = resolve_translation_model(selection)
    return {
        "translation_model": config.model,
        "translation_model_key": config.key,
        "translation_model_version": config.version,
        "translation_thinking": config.thinking,
        "translation_max_tokens": config.max_tokens,
        "translation_pricing_version": config.pricing_version,
    }


def pending_model_compatibility(meta, selected_model=None):
    """Classify whether a pending translation is safe under a model selection.

    Legacy pending files are deliberately reported as ``legacy_unpinned``.
    They remain recoverable because the engine must never discard an already
    paid translation merely because it predates model provenance metadata.
    New pending files should be pinned and an exact mismatch should be surfaced
    before any new model-dependent continuation or repair decision.
    """
    selected = resolve_translation_model(selected_model)
    if not isinstance(meta, dict):
        return {
            "status": "legacy_unpinned",
            "selected_model": selected.model,
            "pending_model": "",
        }
    pending_model = meta.get("translation_model")
    if not isinstance(pending_model, str) or not pending_model.strip():
        return {
            "status": "legacy_unpinned",
            "selected_model": selected.model,
            "pending_model": "",
        }
    try:
        pending = resolve_translation_model(pending_model)
    except ValueError:
        return {
            "status": "unknown_pending_model",
            "selected_model": selected.model,
            "pending_model": pending_model,
        }
    return {
        "status": "match" if pending.model == selected.model else "mismatch",
        "selected_model": selected.model,
        "pending_model": pending.model,
    }
