"""G7D frozen Command Code Provider accounting policy.

G7D resolves the accounting blocker exposed by G7C without authorizing a paid
Stage-1 request.  The policy freezes a 2026-09-19 snapshot of Command Code's
public USD-denominated per-million-token rate card for the six benchmark
models.

This is benchmark accounting, not a claim to reproduce a subscription invoice.
When the Provider exposes cached-token usage, the calculated value is exact
under the frozen rate card.  When cached-token usage is not exposed, G7D fails
open neither to zero-cost nor to invented cache hits: it conservatively prices
the whole prompt at the fresh-input rate and marks the result as an upper bound.

DeepSeek V4.1 Flash uses Command Code's published UTC peak/off-peak schedule.
All other Stage-1 models use the flat public rates frozen below.

G7D performs zero Provider calls and keeps paid Stage-1 blocked until a separate
paid execution/authorization contract is frozen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS
from benchmark_longitudinal_g7c_stage1_integration import (
    G7C_INTEGRATION_SHA256,
    PAID_STAGE1_REMAINING_BLOCKERS,
    g7c_integration_identity,
)


G7D_SCHEMA_VERSION = 1
G7D_REVISION = "goat-g7d-command-code-accounting-policy-v1"
G7D_REPOSITORY_ANCHOR = "22c7ff38b3d3ae818907a765b6f91269a6c33634"

EXPECTED_G7C_INTEGRATION_SHA256 = (
    "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
)

PRICING_CAPTURE_DATE = "2026-09-19"
PRICING_VERSION = "command-code-provider-public-ratecard-2026-09-19-v1"
PRICING_UNIT = "usd_credit_equivalent_per_1m_tokens"

PRICING_SOURCE_URLS = (
    "https://commandcode.ai/docs/resources/pricing-limits",
    "https://commandcode.ai/docs/plans/pro",
    "https://commandcode.ai/models/deepseek-v4-1-flash",
    "https://commandcode.ai/models/glm-5-3-flash",
    "https://commandcode.ai/models/qwen3-8-max-0902",
)

RESOLVED_G7C_ACCOUNTING_BLOCKER = "freeze_command_code_provider_accounting_policy"
PAID_STAGE1_REMAINING_BLOCKERS = (
    "freeze_stage1_paid_execution_authorization_contract",
)

DEEPSEEK_MODEL_ID = "deepseek/deepseek-v4.1-flash"
DEEPSEEK_PEAK_UTC_WEEKDAYS = (0, 1, 2, 3, 4)
DEEPSEEK_PEAK_UTC_WINDOWS = ((1, 4), (6, 10))

# Decimal strings are identity data.  Do not replace them with binary floats.
# Rates are USD-denominated Command Code credit-equivalent values per 1M tokens.
_MODEL_RATECARD = {
    "deepseek/deepseek-v4.1-flash": {
        "pricing_mode": "utc_peak_offpeak",
        "periods": {
            "off_peak": {
                "input": "0.15",
                "output": "0.60",
                "cache_read": "0.003",
            },
            "peak": {
                "input": "0.30",
                "output": "1.20",
                "cache_read": "0.006",
            },
        },
        "peak_weekdays_utc": list(DEEPSEEK_PEAK_UTC_WEEKDAYS),
        "peak_windows_utc": [list(item) for item in DEEPSEEK_PEAK_UTC_WINDOWS],
    },
    "z-ai/glm-5.3-flash": {
        "pricing_mode": "flat",
        "periods": {
            "flat": {
                "input": "0.15",
                "output": "0.50",
                "cache_read": "0.03",
            },
        },
    },
    "qwen/qwen3.8-max-0902": {
        "pricing_mode": "flat",
        "periods": {
            "flat": {
                "input": "2.00",
                "output": "6.00",
                "cache_read": "0.25",
            },
        },
    },
    "google/gemini-3.8-flash": {
        "pricing_mode": "flat",
        "periods": {
            "flat": {
                "input": "1.50",
                "output": "7.50",
                "cache_read": "0.15",
            },
        },
    },
    "tencent/hy3-paid": {
        "pricing_mode": "flat",
        "periods": {
            "flat": {
                "input": "0.14",
                "output": "0.58",
                "cache_read": "0.035",
            },
        },
    },
    "xiaomi/mimo-v2.5-pro": {
        "pricing_mode": "flat",
        "periods": {
            "flat": {
                "input": "0.435",
                "output": "0.87",
                "cache_read": "0.0036",
            },
        },
    },
}

MODEL_RATECARD = MappingProxyType(_MODEL_RATECARD)


class G7DAccountingError(RuntimeError):
    """Frozen Stage-1 accounting input or policy is invalid."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7DAccountingError(
            "G7D value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_predecessor() -> dict[str, Any]:
    if G7C_INTEGRATION_SHA256 != EXPECTED_G7C_INTEGRATION_SHA256:
        raise G7DAccountingError("G7C integration identity changed")
    identity = g7c_integration_identity()
    if identity["stage1"]["provider_calls_authorized"] is not False:
        raise G7DAccountingError("G7C unexpectedly authorizes Provider calls")
    if tuple(identity["execution_gate"]["required_before_paid_stage1"]) != (
        "freeze_command_code_provider_accounting_policy",
        "freeze_stage1_paid_execution_authorization_contract",
    ):
        raise G7DAccountingError("G7C paid-execution blocker set changed")
    return identity


def _validate_ratecard() -> None:
    if tuple(_MODEL_RATECARD) != tuple(BENCHMARK_MODEL_IDS):
        raise G7DAccountingError("G7D rate-card roster/order changed")

    required_rate_fields = {"input", "output", "cache_read"}
    for model_id, model in _MODEL_RATECARD.items():
        if model["pricing_mode"] not in {"flat", "utc_peak_offpeak"}:
            raise G7DAccountingError(f"unknown pricing mode for {model_id}")
        periods = model.get("periods")
        if not isinstance(periods, Mapping) or not periods:
            raise G7DAccountingError(f"pricing periods missing for {model_id}")
        for period, rates in periods.items():
            if set(rates) != required_rate_fields:
                raise G7DAccountingError(
                    f"rate fields changed for {model_id}/{period}"
                )
            try:
                parsed = {
                    name: Decimal(value)
                    for name, value in rates.items()
                }
            except (InvalidOperation, TypeError) as exc:
                raise G7DAccountingError(
                    f"invalid decimal rate for {model_id}/{period}"
                ) from exc
            if any(value < 0 for value in parsed.values()):
                raise G7DAccountingError(
                    f"negative rate for {model_id}/{period}"
                )
            if parsed["cache_read"] > parsed["input"]:
                raise G7DAccountingError(
                    f"cache-read rate exceeds fresh-input rate for {model_id}/{period}"
                )

    deepseek = _MODEL_RATECARD[DEEPSEEK_MODEL_ID]
    if tuple(deepseek["peak_weekdays_utc"]) != DEEPSEEK_PEAK_UTC_WEEKDAYS:
        raise G7DAccountingError("DeepSeek peak weekday policy changed")
    if tuple(tuple(row) for row in deepseek["peak_windows_utc"]) != (
        DEEPSEEK_PEAK_UTC_WINDOWS
    ):
        raise G7DAccountingError("DeepSeek peak windows changed")


_validate_ratecard()


def _model_identity(model_id: str) -> dict[str, Any]:
    try:
        model = _MODEL_RATECARD[model_id]
    except KeyError as exc:
        raise G7DAccountingError(f"unknown benchmark model id: {model_id!r}") from exc
    value = {
        "benchmark_model_id": model_id,
        "pricing_mode": model["pricing_mode"],
        "periods": {
            name: dict(rates)
            for name, rates in model["periods"].items()
        },
        "cache_write_rate": None,
        "cache_write_usage_accounted": False,
    }
    if model["pricing_mode"] == "utc_peak_offpeak":
        value["peak_weekdays_utc"] = list(model["peak_weekdays_utc"])
        value["peak_windows_utc"] = [
            list(item) for item in model["peak_windows_utc"]
        ]
    return value


def g7d_accounting_identity() -> dict[str, Any]:
    """Return the deterministic, zero-provider Stage-1 accounting contract."""
    _validate_predecessor()
    return {
        "schema_version": G7D_SCHEMA_VERSION,
        "revision": G7D_REVISION,
        "repository_anchor": G7D_REPOSITORY_ANCHOR,
        "predecessor": {
            "g7c_integration_sha256": EXPECTED_G7C_INTEGRATION_SHA256,
        },
        "pricing_capture_date": PRICING_CAPTURE_DATE,
        "pricing_version": PRICING_VERSION,
        "pricing_unit": PRICING_UNIT,
        "pricing_sources": list(PRICING_SOURCE_URLS),
        "billing_semantics": {
            "subscription_fee_allocated_per_request": False,
            "provider_invoice_claimed": False,
            "purpose": "frozen_stage1_benchmark_credit_equivalent_accounting",
            "cache_write_accounting": "not_reported_by_frozen_provider_contract",
            "reported_cache_tokens": "exact_under_frozen_ratecard",
            "unreported_cache_tokens": (
                "conservative_upper_bound_all_prompt_as_fresh_input"
            ),
            "cost_field_policy": (
                "cost_usd_and_rate_correct_cost_usd_store_the_same_frozen_"
                "ratecard_value; cost_exact_under_frozen_ratecard marks exactness"
            ),
        },
        "models": [_model_identity(model_id) for model_id in BENCHMARK_MODEL_IDS],
        "resolved_by_g7d": [RESOLVED_G7C_ACCOUNTING_BLOCKER],
        "execution_gate": {
            "status": "blocked",
            "reason": "stage1_paid_execution_authorization_not_frozen",
            "required_before_paid_stage1": list(PAID_STAGE1_REMAINING_BLOCKERS),
        },
        "stage1": {
            "provider_calls_authorized": False,
            "model_ranking_or_elimination_performed": False,
            "finalist_continuation_authorized": False,
        },
        "provider_calls": 0,
    }


G7D_ACCOUNTING_SHA256 = _sha256_json(g7d_accounting_identity())


def assert_paid_stage1_blocked() -> None:
    identity = g7d_accounting_identity()
    if identity["execution_gate"]["status"] != "blocked":
        raise G7DAccountingError("G7D must keep paid Stage-1 blocked")
    if identity["stage1"]["provider_calls_authorized"] is not False:
        raise G7DAccountingError("G7D cannot authorize Provider calls")
    if identity["provider_calls"] != 0:
        raise G7DAccountingError("G7D must report zero Provider calls")


def _as_utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise G7DAccountingError(
                "request_started_utc must be valid ISO-8601"
            ) from exc
    if not isinstance(value, datetime):
        raise G7DAccountingError(
            "request_started_utc must be an aware UTC datetime or ISO-8601 string"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise G7DAccountingError("request_started_utc must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise G7DAccountingError("request_started_utc must use UTC")
    return value.astimezone(timezone.utc)


def _deepseek_period(moment: datetime) -> str:
    if moment.weekday() not in DEEPSEEK_PEAK_UTC_WEEKDAYS:
        return "off_peak"
    hour = (
        moment.hour
        + moment.minute / 60
        + moment.second / 3600
        + moment.microsecond / 3_600_000_000
    )
    if any(start <= hour < end for start, end in DEEPSEEK_PEAK_UTC_WINDOWS):
        return "peak"
    return "off_peak"


def pricing_snapshot(
    model_id: str,
    request_started_utc: datetime | str,
) -> dict[str, Any]:
    """Resolve the frozen model rate in force at request start."""
    moment = _as_utc_datetime(request_started_utc)
    try:
        model = _MODEL_RATECARD[model_id]
    except KeyError as exc:
        raise G7DAccountingError(f"unknown benchmark model id: {model_id!r}") from exc

    period = (
        _deepseek_period(moment)
        if model["pricing_mode"] == "utc_peak_offpeak"
        else "flat"
    )
    rates = model["periods"][period]
    return {
        "pricing_version": PRICING_VERSION,
        "pricing_capture_date": PRICING_CAPTURE_DATE,
        "pricing_unit": PRICING_UNIT,
        "pricing_period": period,
        "pricing_mode": model["pricing_mode"],
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": float(Decimal(rates["cache_read"])),
        "cache_miss_rate_per_million": float(Decimal(rates["input"])),
        "output_rate_per_million": float(Decimal(rates["output"])),
        "cache_write_rate_per_million": None,
    }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _required_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise G7DAccountingError(f"{name} must be a nonnegative integer")
    return value


def account_provider_usage(
    model_id: str,
    usage: Any,
    request_started_utc: datetime | str,
) -> dict[str, Any]:
    """Convert accepted Provider usage into frozen Stage-1 accounting stats."""
    if usage is None:
        raise G7DAccountingError("accepted Provider usage is required")

    prompt_tokens = _required_nonnegative_int(
        _field(usage, "prompt_tokens"),
        "prompt_tokens",
    )
    completion_tokens = _required_nonnegative_int(
        _field(usage, "completion_tokens"),
        "completion_tokens",
    )
    cached_exposed = _field(usage, "cached_tokens_exposed", False)
    if not isinstance(cached_exposed, bool):
        raise G7DAccountingError("cached_tokens_exposed must be boolean")

    raw_cached = _field(usage, "cached_tokens", None)
    if cached_exposed:
        cached_tokens = _required_nonnegative_int(raw_cached, "cached_tokens")
        if cached_tokens > prompt_tokens:
            raise G7DAccountingError("cached_tokens cannot exceed prompt_tokens")
        cache_hit_tokens = cached_tokens
        cache_miss_tokens = prompt_tokens - cached_tokens
        exact = True
        cache_mode = "reported_cache_exact"
        cost_semantics = "frozen_ratecard_exact"
    else:
        if raw_cached not in (None, 0):
            raise G7DAccountingError(
                "unexposed cached_tokens must be absent rather than guessed"
            )
        cache_hit_tokens = 0
        cache_miss_tokens = prompt_tokens
        exact = False
        cache_mode = "cache_unexposed_conservative_upper_bound"
        cost_semantics = "frozen_ratecard_upper_bound"

    snapshot = pricing_snapshot(model_id, request_started_utc)
    hit_rate = Decimal(str(snapshot["cache_hit_rate_per_million"]))
    miss_rate = Decimal(str(snapshot["cache_miss_rate_per_million"]))
    output_rate = Decimal(str(snapshot["output_rate_per_million"]))

    cost = (
        Decimal(cache_hit_tokens) * hit_rate
        + Decimal(cache_miss_tokens) * miss_rate
        + Decimal(completion_tokens) * output_rate
    ) / Decimal(1_000_000)

    cost_float = float(cost)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cost_usd": cost_float,
        "rate_correct_cost_usd": cost_float,
        "pricing_version": snapshot["pricing_version"],
        "pricing_period": snapshot["pricing_period"],
        "pricing_mode": "command_code_public_ratecard_snapshot",
        "request_started_utc": snapshot["request_started_utc"],
        "cache_hit_rate_per_million": snapshot["cache_hit_rate_per_million"],
        "cache_miss_rate_per_million": snapshot["cache_miss_rate_per_million"],
        "output_rate_per_million": snapshot["output_rate_per_million"],
        "cache_write_rate_per_million": None,
        "cached_tokens_exposed": cached_exposed,
        "cache_accounting_mode": cache_mode,
        "cost_semantics": cost_semantics,
        "cost_exact_under_frozen_ratecard": exact,
        "provider_invoice_claimed": False,
        "pricing_capture_date": PRICING_CAPTURE_DATE,
    }


__all__ = [
    "DEEPSEEK_PEAK_UTC_WEEKDAYS",
    "DEEPSEEK_PEAK_UTC_WINDOWS",
    "G7D_ACCOUNTING_SHA256",
    "G7D_REVISION",
    "MODEL_RATECARD",
    "PAID_STAGE1_REMAINING_BLOCKERS",
    "PRICING_CAPTURE_DATE",
    "PRICING_SOURCE_URLS",
    "PRICING_VERSION",
    "account_provider_usage",
    "assert_paid_stage1_blocked",
    "g7d_accounting_identity",
    "pricing_snapshot",
]
