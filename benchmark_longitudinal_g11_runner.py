"""G11 practical DeepSeek-V4-Pro replacement benchmark runner.

This module implements the four-track, 100-chapter, real-production-engine G11
experiment frozen by ``benchmark_longitudinal_g11_preregistration``.

Design intent (lean, production-shaped, not a new framework):

* The REAL NovelTranslator engine is loaded and initialized exactly as the
  production experiment loader does it (provider construction suppressed), and
  every provider-shaped lifecycle call is then routed through this runner's
  single journal + retry + accounting boundary.
* The OpenAI-compatible client keeps ``max_retries=0``.  The G11 wrapper owns
  the visible retries: at most three physical transport attempts per logical
  operation, with a short backoff, for connection/timeout/rate-limit/408/429/5xx
  failures only.  Deterministic invalid-request/authentication failures are not
  retried.
* Every physical attempt is recorded durably.  A model's hard credit-equivalent
  ceiling (GLM 40, DeepSeek V4.1 Flash 60) is enforced from the same ledger.
* A translation whose response is unusable (``finish_reason == "length"`` or
  effectively empty content) is regenerated ONCE, and that regeneration is a
  separately journaled, fully accounted paid call.
* The production 25-chapter audit cadence stays entirely engine-owned: this
  runner never synthesizes an audit and never preregisters an audit send count.
* Importing this module performs no credential read, no client construction, no
  network activity, and no filesystem write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from benchmark_longitudinal_config import (
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
    PROVIDER_NAME,
)
from benchmark_longitudinal_capability_results import (
    live_provider_capability,
    verified_reasoning_capability,
)
from benchmark_longitudinal_g10_safety import assert_fresh_execution_root
from benchmark_longitudinal_g11_preregistration import (
    AUDIT_CHECKPOINTS,
    AUDIT_INTERVAL,
    AUXILIARY_COMPLETION_CEILING,
    CHAPTER_COUNT_PER_TRACK,
    DEEPSEEK_MODEL_ID,
    EFFORT_BY_OPERATION,
    FIRST_CHAPTER,
    G11_BENCHMARK_ID,
    G11_MODEL_RATECARD,
    G11_PREREGISTRATION_SHA256,
    G11_PRICING_VERSION,
    G11_REVISION,
    LAST_CHAPTER,
    MAX_TRANSPORT_ATTEMPTS,
    MODEL_BUDGETS,
    MODEL_ORDER,
    MODEL_SLUGS,
    NON_RETRYABLE_HTTP_STATUSES,
    NOVELS,
    PROVIDER_MODEL_IDS,
    QUALITY_CHECKPOINTS,
    RESIDUAL_CJK_MICRO_REPAIR,
    RETRYABLE_HTTP_STATUS_RANGES,
    RETRYABLE_HTTP_STATUSES,
    RETRY_BACKOFF_SECONDS,
    STATE_EVIDENCE_CHECKPOINTS,
    TRACKS,
    TRACK_BY_ID,
    TRANSLATION_COMPLETION_CEILING,
    assert_source_bindings,
    g11_preregistration_identity,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    DEEPSEEK_PEAK_UTC_WEEKDAYS,
    DEEPSEEK_PEAK_UTC_WINDOWS,
)
from benchmark_longitudinal_g7c_stage1_integration import (
    CONTEXT_ENTRYPOINTS,
    DIRECT_LEGACY_OPERATION_MAP,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)
from chapter_library import index_chapter_library, validate_chapter_library
from create_novel import PROJECT_DIRECTORIES, initial_project_state
from novel_project import load_novel_config
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    inspect_paid_request_journals,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from production_run_lock import production_run_lock


G11_RUNNER_SCHEMA_VERSION = 1
G11_RUNNER_REVISION = "g11-practical-benchmark-runner-v1"
G11_EXECUTION_MANIFEST_FILENAME = "g11_execution_manifest.json"
G11_RUN_IDENTITY_FILENAME = "g11_run_identity.json"
G11_QUARANTINE_FILENAME = "g11_quarantine.json"
G11_LEDGER_DIRECTORY = "g11_ledgers"
G11_EVIDENCE_DIRECTORY = "g11_evidence"
G11_INVOCATION_DIRECTORY = "data/g11_invocations"
G11_TRACK_FINGERPRINT_EXCLUDED_PREFIXES = (
    "data/provider_request_journal/",
    f"{G11_INVOCATION_DIRECTORY}/",
    "logs/",
)
G11_TRACK_FINGERPRINT_EXCLUDED_FILES = (".production.lock",)
G11_QUARANTINE_REVISION = "g11-model-novel-track-quarantine-v1"
G11_PROVIDER_TRANSPORT = "command_code_provider_direct"
G11_PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
G11_LIFECYCLE_ENTRYPOINT = "run_unattended_production"
G11_DEFAULT_PRISTINE_ROOT = r"D:\NovelTranslator_G10B_S0_20260929"
G11_S0_PRICING_PLACEHOLDER = "g11-preregistered-pricing-v1"

# Deterministic content/QA stops are recoverable, not permanent kills.  The
# rejected pending/journal evidence is archived (moved, never destroyed) and the
# same chapter is retried through the real lifecycle at most twice.
G11_CONTENT_RECOVERY_DIRECTORY = "g11_content_recovery"
G11_CONTENT_RECOVERY_MAX_CYCLES = 2
G11_CONTENT_STOP_REASON_MARKER = (
    "real lifecycle did not finish exactly the expected chapter"
)
G11_ENGINE_SAFE_STOP_REASON_PREFIX = "SystemExit"
G11_RECOVERY_TRANSLATION_INSTRUCTION = (
    "Recovery pass: return only the complete English translation. Preserve all "
    "source content, but leave no untranslated Chinese/Korean/Hanja source-script "
    "text and do not add explanations."
)
G11_DEEPSEEK_LOW_MODE_FILENAME = "g11_deepseek_low_translation_mode.json"
G11_DEEPSEEK_LOW_MODE_REVISION = "g11-deepseek-adaptive-low-translation-v1"
G11_DEEPSEEK_LOW_TRANSLATION_EFFORT = "low"
G11_VERIFIED_REASONING_EFFORTS = frozenset({"low", "high"})

# The audit-content recovery path covers exactly one situation: the final
# periodic glossary audit (Chapters 76-100 of a 100-chapter track) stops on a
# deterministic audit *content* failure after the last chapter is already
# committed.  The already-validated audit checkpoint is reused as-is except for
# its failed leaf chunk(s); completed chunks and their attempt evidence are
# preserved, and only the failed attempt is archived/reset so the real engine
# re-issues a genuinely new provider operation for the pending audit.
G11_AUDIT_CONTENT_RECOVERY_DIRECTORY = "g11_audit_content_recovery"
G11_AUDIT_CONTENT_RECOVERY_MAX_CYCLES = 2
G11_AUDIT_CONTENT_RECOVERY_REVISION = "g11-audit-content-recovery-v1"
G11_AUDIT_CHUNK_FAILURE_MARKER = "AuditChunkFailure"
G11_AUDIT_STOP_REASON_MARKER = "Periodic glossary audit stopped safely for Chapters"
G11_AUDIT_JOURNAL_KIND_MARKER = "audit"
G11_AUDIT_FAILED_CHUNK_STATUSES = frozenset({"failed", "validation_failed", "terminal_failed"})
G11_AUDIT_UNRESOLVED_CHUNK_STATUSES = frozenset(
    {"in_flight", "remote_outcome_uncertain"}
)

_OPERATION_PREFIX = "g11"
_HOOK_SYMBOLS = (
    "journaled_chat_request",
    "call_api_with_retries",
    "send_api_request_once",
    "usage_and_cost",
    "usage_and_cost_for_pricing",
    "reprice_saved_stats_if_needed",
    "pricing_snapshot",
)
_STATS_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "rate_correct_cost_usd",
)
_ALLOWED_OPERATIONS = frozenset(
    DIRECT_LEGACY_OPERATION_MAP.values()
) | frozenset(CONTEXT_ENTRYPOINTS.values())


class G11RunnerError(RuntimeError):
    """G11 execution state, accounting, or isolation is unsafe."""


class G11BudgetError(G11RunnerError):
    """A model's hard credit-equivalent ceiling cannot admit another attempt."""


class G11TransportExhausted(G11RunnerError):
    """Every allowed physical transport attempt failed transiently."""


class G11DeterministicTransportFailure(G11RunnerError):
    """A non-retryable transport/provider failure ended the logical operation."""


class G11AmbiguousOutcome(G11RunnerError):
    """A paid subrequest may have completed remotely with no durable response."""

    def __init__(self, message: str, *, request_sha256: str, chapter: int):
        super().__init__(message)
        self.request_sha256 = request_sha256
        self.chapter = chapter


class G11ContentRecoveryExhausted(G11RunnerError):
    """The bounded content-recovery cycles were spent without a commit."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G11RunnerError("G11 value is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _as_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G11RunnerError(f"{label} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G11RunnerError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise G11RunnerError(f"{label} must be a finite decimal")
    return parsed


def _read_json(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G11RunnerError(f"cannot read {path}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise G11RunnerError(f"{label} must contain a JSON object: {path}")
    return value


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G11RunnerError(f"G11 root must not be a symlink: {root}")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G11RunnerError(f"G11 tree contains a symlink: {item}")


def _tree_fingerprint(
    root: Path,
    *,
    excluded_prefixes: tuple[str, ...] = (),
    excluded_files: tuple[str, ...] = (),
) -> str:
    """Fingerprint a whole tree read-only (paths + bytes).

    Runner-owned volatile files (the run lock, the durable request journal, and
    per-invocation bookkeeping) are excluded because the run lock is held open
    for the whole track and is unreadable while held.
    """
    root = Path(root)
    digest = hashlib.sha256()
    if not root.exists():
        return "absent"
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.name in excluded_files:
            continue
        if any(relative.startswith(prefix) for prefix in excluded_prefixes):
            continue
        kind = "d" if path.is_dir() else ("l" if path.is_symlink() else "f")
        digest.update(f"{kind}\0{relative}\0".encode("utf-8"))
        if kind == "f":
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _track_fingerprint(root: Path) -> str:
    return _tree_fingerprint(
        root,
        excluded_prefixes=G11_TRACK_FINGERPRINT_EXCLUDED_PREFIXES,
        excluded_files=G11_TRACK_FINGERPRINT_EXCLUDED_FILES,
    )


def _aware_utc_now(clock: Any) -> datetime:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G11RunnerError("G11 clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


# ===========================================================================
# Pricing / accounting (frozen G11 snapshot, zero provider access)
# ===========================================================================


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


def g11_pricing_snapshot(model_id: str, moment: datetime) -> dict[str, Any]:
    """Resolve the frozen G11 rate in force at request start."""
    if model_id not in G11_MODEL_RATECARD:
        raise G11RunnerError(f"unknown G11 model id: {model_id!r}")
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise G11RunnerError("pricing moment must be a timezone-aware datetime")
    moment = moment.astimezone(timezone.utc)
    model = G11_MODEL_RATECARD[model_id]
    period = (
        _deepseek_period(moment)
        if model["pricing_mode"] == "utc_peak_offpeak"
        else "flat"
    )
    rates = model["periods"][period]
    return {
        "pricing_version": G11_PRICING_VERSION,
        "pricing_period": period,
        "pricing_mode": model["pricing_mode"],
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": float(Decimal(rates["cache_read"])),
        "cache_miss_rate_per_million": float(Decimal(rates["input"])),
        "output_rate_per_million": float(Decimal(rates["output"])),
    }


def g11_account_usage(
    model_id: str,
    usage: Any,
    request_started_utc: datetime | str,
) -> dict[str, Any]:
    """Convert accepted provider usage into frozen G11 accounting stats."""
    if usage is None:
        raise G11RunnerError("accepted provider usage is required")
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if (
        isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 0
    ):
        raise G11RunnerError("provider usage lacks prompt_tokens")
    if (
        isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens < 0
    ):
        raise G11RunnerError("provider usage lacks completion_tokens")

    exposed = bool(getattr(usage, "cached_tokens_exposed", False))
    cached = getattr(usage, "cached_tokens", None)
    if exposed and cached is not None:
        if isinstance(cached, bool) or not isinstance(cached, int) or cached < 0:
            raise G11RunnerError("provider cached_tokens is invalid")
        if cached > prompt_tokens:
            raise G11RunnerError("cached_tokens cannot exceed prompt_tokens")
        cache_hits = cached
    else:
        cache_hits = 0
    cache_misses = prompt_tokens - cache_hits

    moment = request_started_utc
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    snapshot = g11_pricing_snapshot(model_id, moment)
    hit_rate = Decimal(str(snapshot["cache_hit_rate_per_million"]))
    miss_rate = Decimal(str(snapshot["cache_miss_rate_per_million"]))
    output_rate = Decimal(str(snapshot["output_rate_per_million"]))
    cost = (
        Decimal(cache_hits) * hit_rate
        + Decimal(cache_misses) * miss_rate
        + Decimal(completion_tokens) * output_rate
    ) / Decimal(1_000_000)
    cost_float = float(cost)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost_float,
        "rate_correct_cost_usd": cost_float,
        "credit_equivalent": _decimal_text(cost),
        "pricing_version": G11_PRICING_VERSION,
        "pricing_period": snapshot["pricing_period"],
        "request_started_utc": snapshot["request_started_utc"],
        "cache_hit_rate_per_million": snapshot["cache_hit_rate_per_million"],
        "cache_miss_rate_per_million": snapshot["cache_miss_rate_per_million"],
        "output_rate_per_million": snapshot["output_rate_per_million"],
        "cached_tokens_exposed": exposed,
        "provider_transport": G11_PROVIDER_TRANSPORT,
    }


# ===========================================================================
# Durable per-model benchmark ledger
# ===========================================================================


class G11Ledger:
    """Small atomic JSON ledger: physical attempts, reservations, CE budget."""

    schema_version = 1
    revision = "g11-benchmark-ledger-v1"

    def __init__(self, path: Path, document: dict[str, Any]) -> None:
        self.path = Path(path)
        self.document = document

    # -- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        path: Path | str,
        model_id: str,
        ceiling: Any,
        clock: Any = None,
    ) -> "G11Ledger":
        target = Path(path)
        if target.exists():
            raise G11RunnerError(f"G11 ledger already exists: {target}")
        if model_id not in MODEL_BUDGETS:
            raise G11RunnerError(f"unknown G11 ledger model: {model_id!r}")
        ceiling_decimal = _as_decimal(ceiling, "ledger ceiling")
        if ceiling_decimal <= 0:
            raise G11RunnerError("G11 ledger ceiling must be positive")
        frozen_ceiling = MODEL_BUDGETS.get(model_id)
        stored_ceiling = (
            frozen_ceiling
            if frozen_ceiling is not None
            and _as_decimal(frozen_ceiling, "frozen ceiling") == ceiling_decimal
            else _decimal_text(ceiling_decimal)
        )
        now = _aware_utc_now(clock).isoformat()
        ledger = cls(
            target,
            {
                "schema_version": cls.schema_version,
                "revision": cls.revision,
                "benchmark_id": G11_BENCHMARK_ID,
                "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
                "model_id": model_id,
                "ceiling_credit_equivalent": stored_ceiling,
                "created_utc": now,
                "updated_utc": now,
                "reservations": [],
                "attempts": [],
            },
        )
        ledger._persist()
        return ledger

    @classmethod
    def open(cls, *, path: Path | str, model_id: str) -> "G11Ledger":
        target = Path(path)
        document = _read_object(target, "G11 ledger")
        if document.get("schema_version") != cls.schema_version:
            raise G11RunnerError("unsupported G11 ledger schema")
        if document.get("revision") != cls.revision:
            raise G11RunnerError("unsupported G11 ledger revision")
        if document.get("benchmark_id") != G11_BENCHMARK_ID:
            raise G11RunnerError("G11 ledger binds another benchmark")
        if document.get("model_id") != model_id:
            raise G11RunnerError("G11 ledger binds another model")
        if _as_decimal(
            document.get("ceiling_credit_equivalent", "0"), "ledger ceiling"
        ) != _as_decimal(MODEL_BUDGETS[model_id], "frozen ceiling"):
            raise G11RunnerError("G11 ledger ceiling drifted from the preregistration")
        return cls(target, document)

    @classmethod
    def open_or_create(
        cls,
        *,
        path: Path | str,
        model_id: str,
        ceiling: Any,
        clock: Any = None,
    ) -> "G11Ledger":
        if Path(path).is_file():
            return cls.open(path=path, model_id=model_id)
        return cls.create(path=path, model_id=model_id, ceiling=ceiling, clock=clock)

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        self.document["updated_utc"] = _aware_utc_now(None).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.document)

    # -- accounting --------------------------------------------------------
    @property
    def ceiling(self) -> Decimal:
        return _as_decimal(self.document["ceiling_credit_equivalent"], "ceiling")

    def committed_credit_equivalent(self) -> Decimal:
        total = Decimal(0)
        for record in self.document.get("reservations", []):
            if record.get("status") != "settled":
                continue
            total += _as_decimal(record.get("credit_equivalent", "0"), "settled CE")
        return total

    def remaining_credit_equivalent(self) -> Decimal:
        return self.ceiling - self.committed_credit_equivalent()

    def unresolved_reservations(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.document.get("reservations", [])
            if record.get("status") == "unresolved"
        ]

    def ensure_budget_available(self) -> None:
        """Fail closed before opening another paid operation."""
        unresolved = self.unresolved_reservations()
        if unresolved:
            raise G11BudgetError(
                "G11 ledger holds an unresolved paid reservation; stop this track "
                "for investigation instead of spending further"
            )
        if self.committed_credit_equivalent() >= self.ceiling:
            raise G11BudgetError(
                f"G11 credit-equivalent ceiling reached for "
                f"{self.document['model_id']}: "
                f"{_decimal_text(self.committed_credit_equivalent())} of "
                f"{_decimal_text(self.ceiling)}"
            )

    def begin_operation(
        self,
        *,
        operation: str,
        request_sha256: str,
        run_id: str,
        novel_id: str,
        chapter: int,
        regeneration: bool,
        clock: Any = None,
    ) -> str:
        now = _aware_utc_now(clock).isoformat()
        reservation_id = _sha256_json(
            {
                "model_id": self.document["model_id"],
                "operation": operation,
                "request_sha256": request_sha256,
                "chapter": chapter,
                "regeneration": bool(regeneration),
                "started_utc": now,
            }
        )
        self.document["reservations"].append(
            {
                "reservation_id": reservation_id,
                "operation": operation,
                "request_sha256": request_sha256,
                "run_id": run_id,
                "novel_id": novel_id,
                "chapter": chapter,
                "regeneration": bool(regeneration),
                "status": "open",
                "started_utc": now,
                "transport_attempts": 0,
                "first_attempt_success": None,
                "eventual_success": None,
                "credit_equivalent": "0",
                "tokens": {},
                "reason": "",
            }
        )
        self._persist()
        return reservation_id

    def _reservation(self, reservation_id: str) -> dict[str, Any]:
        for record in self.document["reservations"]:
            if record["reservation_id"] == reservation_id:
                return record
        raise G11RunnerError("unknown G11 reservation id")

    def record_attempt(
        self,
        *,
        reservation_id: str,
        attempt_index: int,
        outcome: str,
        error_type: str = "",
        error_message: str = "",
        classification: str = "",
        elapsed_seconds: float = 0.0,
        credit_equivalent: Any = "0",
        clock: Any = None,
    ) -> None:
        if outcome not in {"success", "transient_failure", "deterministic_failure"}:
            raise G11RunnerError(f"invalid G11 attempt outcome: {outcome!r}")
        now = _aware_utc_now(clock).isoformat()
        reservation = self._reservation(reservation_id)
        if attempt_index != reservation["transport_attempts"] + 1:
            raise G11RunnerError("G11 ledger attempt index is not sequential")
        reservation["transport_attempts"] = attempt_index
        if attempt_index == 1:
            reservation["first_attempt_success"] = outcome == "success"
        if outcome == "success":
            reservation["eventual_success"] = True
        self.document["attempts"].append(
            {
                "reservation_id": reservation_id,
                "attempt_index": attempt_index,
                "outcome": outcome,
                "error_type": error_type,
                "error_message": error_message[:500],
                "classification": classification,
                "elapsed_seconds": elapsed_seconds,
                "credit_equivalent": _decimal_text(
                    _as_decimal(credit_equivalent, "attempt CE")
                ),
                "recorded_utc": now,
            }
        )
        self._persist()

    def settle(
        self,
        *,
        reservation_id: str,
        stats: Mapping[str, Any],
        clock: Any = None,
    ) -> None:
        reservation = self._reservation(reservation_id)
        if reservation["status"] != "open":
            raise G11RunnerError("only an open G11 reservation can settle")
        credit = _as_decimal(stats.get("credit_equivalent", "0"), "settled CE")
        reservation["status"] = "settled"
        reservation["eventual_success"] = True
        reservation["credit_equivalent"] = _decimal_text(credit)
        reservation["tokens"] = {
            key: stats.get(key, 0) for key in _STATS_FIELDS
        } | {
            "pricing_period": stats.get("pricing_period", ""),
            "pricing_version": stats.get("pricing_version", ""),
        }
        reservation["settled_utc"] = _aware_utc_now(clock).isoformat()
        self._persist()
        if self.committed_credit_equivalent() > self.ceiling:
            self.document["budget_violation"] = True
            self._persist()
            raise G11BudgetError(
                "G11 accepted usage exceeded the hard model ceiling; stopping this "
                f"track ({_decimal_text(self.committed_credit_equivalent())} > "
                f"{_decimal_text(self.ceiling)})"
            )

    def mark_unresolved(self, *, reservation_id: str, reason: str) -> None:
        reservation = self._reservation(reservation_id)
        if reservation["status"] != "open":
            return
        reservation["status"] = "unresolved"
        reservation["reason"] = reason[:500]
        self._persist()

    def abandon_no_usage(self, *, reservation_id: str, reason: str) -> None:
        reservation = self._reservation(reservation_id)
        if reservation["status"] != "open":
            return
        reservation["status"] = "abandoned_no_usage"
        reservation["reason"] = reason[:500]
        self._persist()

    def summary(self) -> dict[str, Any]:
        reservations = self.document.get("reservations", [])
        attempts = self.document.get("attempts", [])
        settled = [r for r in reservations if r.get("status") == "settled"]
        first_attempt_successes = sum(
            1 for r in reservations if r.get("first_attempt_success") is True
        )
        eventual_successes = sum(
            1 for r in reservations if r.get("status") == "settled"
        )
        completed_operations = [
            r for r in reservations if r.get("status") != "open"
        ]
        return {
            "model_id": self.document["model_id"],
            "ceiling_credit_equivalent": self.document[
                "ceiling_credit_equivalent"
            ],
            "committed_credit_equivalent": _decimal_text(
                self.committed_credit_equivalent()
            ),
            "remaining_credit_equivalent": _decimal_text(
                self.remaining_credit_equivalent()
            ),
            "budget_violation": bool(self.document.get("budget_violation", False)),
            "operations": len(reservations),
            "settled_operations": len(settled),
            "unresolved_operations": len(self.unresolved_reservations()),
            "transport_attempts": len(attempts),
            "retries": sum(
                max(0, int(r.get("transport_attempts", 0)) - 1)
                for r in reservations
            ),
            "first_attempt_successes": first_attempt_successes,
            "eventual_successes": eventual_successes,
            "first_attempt_success_rate": (
                first_attempt_successes / len(completed_operations)
                if completed_operations
                else None
            ),
            "eventual_success_rate": (
                eventual_successes / len(completed_operations)
                if completed_operations
                else None
            ),
            "regenerations": sum(1 for r in reservations if r.get("regeneration")),
            "total_prompt_tokens": sum(
                int(r.get("tokens", {}).get("prompt_tokens", 0) or 0)
                for r in settled
            ),
            "total_completion_tokens": sum(
                int(r.get("tokens", {}).get("completion_tokens", 0) or 0)
                for r in settled
            ),
            "total_cache_hit_tokens": sum(
                int(r.get("tokens", {}).get("cache_hit_tokens", 0) or 0)
                for r in settled
            ),
        }


# ===========================================================================
# Transport retry policy
# ===========================================================================


def _transport_status_code(exc: BaseException) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def classify_transport_failure(exc: BaseException) -> str:
    """Classify one transport failure as ``transient`` or ``deterministic``.

    Transient: connection errors, timeouts, rate limiting, HTTP 408/429/5xx.
    Deterministic: invalid-request/authentication/not-found style 4xx answers
    plus any unclassified non-transport exception (fail closed, do not resend).
    """
    status = _transport_status_code(exc)
    if status is not None:
        if status in RETRYABLE_HTTP_STATUSES:
            return "transient"
        for low, high in RETRYABLE_HTTP_STATUS_RANGES:
            if low <= status <= high:
                return "transient"
        if status in NON_RETRYABLE_HTTP_STATUSES:
            return "deterministic"
        if 400 <= status < 500:
            return "deterministic"
        return "deterministic"

    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return "transient"
    if "ratelimit" in name or "rate_limit" in name:
        return "transient"
    if "internalserver" in name or "serviceunavailable" in name:
        return "transient"
    if isinstance(exc, (TimeoutError, ConnectionError, ConnectionResetError)):
        return "transient"
    return "deterministic"


class _Sleeper:
    """Indirection so tests can observe backoff without real sleeping."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(float(seconds))
        self._sleep(seconds)


# ===========================================================================
# Track progress + quarantine
# ===========================================================================


def track_root(execution_root: Path | str, run_id: str) -> Path:
    if run_id not in TRACK_BY_ID:
        raise G11RunnerError(f"unknown G11 run id: {run_id!r}")
    return Path(execution_root).resolve() / "runs" / run_id


def quarantine_path(track_root_path: Path | str) -> Path:
    return Path(track_root_path) / "data" / G11_QUARANTINE_FILENAME


def read_track_progress(track_root_path: Path | str) -> dict[str, Any]:
    return _read_object(
        Path(track_root_path) / "data" / "progress.json",
        "G11 track progress",
    )


def load_quarantine(track_root_path: Path | str) -> dict[str, Any] | None:
    path = quarantine_path(track_root_path)
    if not path.is_file():
        return None
    marker = _read_object(path, "G11 quarantine")
    if (
        marker.get("scope_kind") != "g11_model_novel_track"
        or marker.get("benchmark_id") != G11_BENCHMARK_ID
    ):
        raise G11RunnerError("G11 quarantine marker is invalid")
    return marker


def write_quarantine(
    *,
    track_root_path: Path | str,
    run_id: str,
    model_id: str,
    novel_id: str,
    first_blocked_chapter: int,
    outcome: str,
    reason: str,
    clock: Any = None,
) -> dict[str, Any]:
    path = quarantine_path(track_root_path)
    if path.exists():
        raise G11RunnerError("G11 quarantine marker already exists")
    if outcome not in {"ambiguous_remote_outcome", "terminal_failure"}:
        raise G11RunnerError(f"invalid G11 quarantine outcome: {outcome!r}")
    marker = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_QUARANTINE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "scope_kind": "g11_model_novel_track",
        "scope_id": run_id,
        "run_id": run_id,
        "model_id": model_id,
        "novel_id": novel_id,
        "first_blocked_chapter": first_blocked_chapter,
        "outcome": outcome,
        "reason": reason[:1000],
        "created_utc": _aware_utc_now(clock).isoformat(),
    }
    atomic_write_json(path, marker)
    return marker


# ===========================================================================
# Deterministic content-stop recovery + safe quarantine reopen
# ===========================================================================


def content_recovery_directory(
    track_root_path: Path | str,
    chapter: int | None = None,
) -> Path:
    base = Path(track_root_path) / "data" / G11_CONTENT_RECOVERY_DIRECTORY
    if chapter is None:
        return base
    return base / f"{int(chapter):04d}"


def chapter_pending_paths(track_root_path: Path | str, chapter: int) -> list[Path]:
    pending = Path(track_root_path) / "output" / "pending"
    return [pending / f"{int(chapter):04d}.md", pending / f"{int(chapter):04d}.json"]


def chapter_journal_paths(track_root_path: Path | str, chapter: int) -> list[Path]:
    directory = Path(track_root_path) / "data" / "provider_request_journal"
    if not directory.is_dir():
        return []
    prefix = f"{int(chapter):04d}."
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json" and path.name.startswith(prefix)
    )


def is_content_stop_reason(reason: Any) -> bool:
    """True when a quarantine reason is the engine's deterministic content stop."""
    if not isinstance(reason, str):
        return False
    if G11_CONTENT_STOP_REASON_MARKER in reason:
        return True
    return reason.startswith(G11_ENGINE_SAFE_STOP_REASON_PREFIX)


def _chapter_has_uncommitted_translation_evidence(
    track_root_path: Path | str, chapter: int
) -> bool:
    """A pending translation or a recorded translation journal is recoverable."""
    if any(path.is_file() for path in chapter_pending_paths(track_root_path, chapter)):
        return True
    for path in chapter_journal_paths(track_root_path, chapter):
        if "translation" not in path.name:
            continue
        record = _read_object(path, "G11 translation journal")
        if record.get("status") == "response_recorded":
            return True
    return False


def _chapter_journals_resolved(track_root_path: Path | str, chapter: int) -> bool:
    """False when a chapter journal is still ``started``/``uncertain``.

    Recovery must never archive (and therefore never resend) a paid request whose
    remote outcome is unresolved.
    """
    for path in chapter_journal_paths(track_root_path, chapter):
        record = _read_object(path, "G11 chapter journal")
        if record.get("status") in {"started", "uncertain"}:
            return False
    return True


def classify_quarantine_reopen(
    track_root_path: Path | str,
    quarantine: Mapping[str, Any] | None,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Decide whether an existing terminal content stop may be reopened.

    Only the exact deterministic content-stop quarantine is reopenable, and only
    while progress is parked on a chapter that carries uncommitted translation
    evidence and no unresolved provider journal.  Ambiguous outcomes, unresolved
    journals, budget stops, corrupt state, and arbitrary terminal errors are
    never reopened.
    """
    if not isinstance(quarantine, Mapping):
        return {"reopenable": False, "reason": "no_quarantine"}
    if quarantine.get("scope_kind") != "g11_model_novel_track":
        return {"reopenable": False, "reason": "quarantine_scope_mismatch"}
    if expected_run_id is not None and quarantine.get("scope_id") != expected_run_id:
        return {"reopenable": False, "reason": "quarantine_run_mismatch"}
    if quarantine.get("outcome") != "terminal_failure":
        return {"reopenable": False, "reason": "outcome_not_terminal_failure"}
    if not is_content_stop_reason(quarantine.get("reason")):
        return {"reopenable": False, "reason": "reason_not_content_stop"}
    blocked = quarantine.get("first_blocked_chapter")
    if isinstance(blocked, bool) or not isinstance(blocked, int) or blocked < 1:
        return {"reopenable": False, "reason": "malformed_blocked_chapter"}

    progress = read_track_progress(track_root_path)
    if progress.get("last_completed") != blocked - 1:
        return {"reopenable": False, "reason": "progress_advanced_past_block"}
    if progress.get("next_chapter") != blocked:
        return {"reopenable": False, "reason": "progress_not_parked_on_block"}

    journals = inspect_paid_request_journals(
        Path(track_root_path) / "data" / "provider_request_journal"
    )
    if journals.get("uncertain") or journals.get("errors"):
        return {"reopenable": False, "reason": "unresolved_provider_journals"}
    if not _chapter_has_uncommitted_translation_evidence(track_root_path, blocked):
        return {"reopenable": False, "reason": "no_pending_translation"}

    return {
        "reopenable": True,
        "blocked_chapter": blocked,
        "reason": quarantine.get("reason"),
    }


def _archive_bytes(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    payload = destination.read_bytes()
    if payload != source.read_bytes():
        raise G11RunnerError(f"G11 recovery archive differs for {source.name}")
    source.unlink()
    return {
        "name": destination.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def archive_chapter_recovery_evidence(
    *,
    track_root_path: Path | str,
    run_id: str,
    model_id: str,
    chapter: int,
    cycle: int,
    reason: str,
    clock: Any = None,
) -> dict[str, Any]:
    """Move the rejected pending/journal evidence into a durable recovery archive."""
    if cycle < 1 or cycle > G11_CONTENT_RECOVERY_MAX_CYCLES:
        raise G11RunnerError("G11 content-recovery cycle is out of range")
    directory = content_recovery_directory(track_root_path, chapter) / (
        f"cycle_{cycle:02d}"
    )
    if directory.exists():
        raise G11RunnerError(
            f"G11 recovery archive already exists and is never overwritten: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=False)
    archived = []
    for source in (
        *chapter_pending_paths(track_root_path, chapter),
        *chapter_journal_paths(track_root_path, chapter),
    ):
        if source.is_file():
            archived.append(_archive_bytes(source, directory / source.name))
    manifest = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_RUNNER_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "run_id": run_id,
        "model_id": model_id,
        "chapter": int(chapter),
        "cycle": int(cycle),
        "reason": str(reason)[:1000],
        "archived": archived,
        "created_utc": _aware_utc_now(clock).isoformat(),
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def archive_quarantine_marker(
    *,
    track_root_path: Path | str,
    quarantine: Mapping[str, Any],
    clock: Any = None,
) -> Path:
    """Archive (never destroy) the active quarantine marker before reopening."""
    path = quarantine_path(track_root_path)
    if not path.is_file():
        raise G11RunnerError("G11 quarantine marker vanished before archiving")
    blocked = quarantine.get("first_blocked_chapter")
    if isinstance(blocked, bool) or not isinstance(blocked, int) or blocked < 1:
        raise G11RunnerError("G11 quarantine marker has no blocked chapter to archive")
    stamp = _aware_utc_now(clock).strftime("%Y%m%dT%H%M%S%f")
    directory = content_recovery_directory(track_root_path) / "quarantine_reopen"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{blocked:04d}_{stamp}.json"
    shutil.copyfile(path, destination)
    if destination.read_bytes() != path.read_bytes():
        raise G11RunnerError("G11 quarantine archive differs from the active marker")
    path.unlink()
    return destination


# ===========================================================================
# Audit-only content recovery (final periodic glossary audit)
# ===========================================================================


def g11_audit_checkpoint_path(
    track_root_path: Path | str,
    start_chapter: int,
    end_chapter: int,
) -> Path:
    return (
        Path(track_root_path)
        / "output"
        / "audits"
        / f"audit_{int(start_chapter):04d}_{int(end_chapter):04d}.checkpoint.json"
    )


def audit_content_recovery_directory(
    track_root_path: Path | str,
    start_chapter: int,
    end_chapter: int,
) -> Path:
    return (
        Path(track_root_path)
        / "data"
        / G11_AUDIT_CONTENT_RECOVERY_DIRECTORY
        / f"{int(start_chapter):04d}_{int(end_chapter):04d}"
    )


def count_audit_content_recovery_cycles(
    track_root_path: Path | str,
    start_chapter: int,
    end_chapter: int,
) -> int:
    """Count durable audit-recovery cycles without mutating anything."""
    root = audit_content_recovery_directory(
        track_root_path, start_chapter, end_chapter
    )
    if not root.is_dir():
        return 0
    return sum(
        1
        for item in root.iterdir()
        if item.is_dir() and item.name.startswith("cycle_")
    )


def g11_evidence_checkpoint_path(
    execution_root: Path | str,
    run_id: str,
    chapter: int,
) -> Path:
    return (
        Path(execution_root).resolve()
        / G11_EVIDENCE_DIRECTORY
        / run_id
        / f"{int(chapter):04d}"
    )


def _audit_chunk_is_leaf(chunk: Mapping[str, Any]) -> bool:
    return isinstance(chunk, Mapping) and chunk.get("status") != "split"


def classify_audit_content_recovery(
    track_root_path: Path | str,
    quarantine: Mapping[str, Any] | None,
    *,
    expected_run_id: str | None = None,
    evidence_checkpoint_exists: bool | None = None,
) -> dict[str, Any]:
    """Decide whether an exact final-audit quarantine may be recovered.

    Only the single deterministic situation described in the module policy is
    recoverable: a ``terminal_failure`` quarantine raised by ``AuditChunkFailure``
    for the final audit window, with the last chapter already committed, the
    audit checkpoint on disk, no Chapter-100 evidence frozen yet, and no
    unresolved provider journal.  ``ambiguous_remote_outcome`` is never
    recoverable, exactly like the chapter-content reopen path.
    """
    if not isinstance(quarantine, Mapping):
        return {"recoverable": False, "reason": "no_quarantine"}
    if quarantine.get("scope_kind") != "g11_model_novel_track":
        return {"recoverable": False, "reason": "quarantine_scope_mismatch"}
    run_id = quarantine.get("scope_id")
    if expected_run_id is not None and run_id != expected_run_id:
        return {"recoverable": False, "reason": "quarantine_run_mismatch"}
    if quarantine.get("outcome") != "terminal_failure":
        return {"recoverable": False, "reason": "outcome_not_terminal_failure"}
    reason = quarantine.get("reason")
    if not isinstance(reason, str) or G11_AUDIT_CHUNK_FAILURE_MARKER not in reason:
        return {"recoverable": False, "reason": "reason_not_audit_chunk_failure"}

    blocked = quarantine.get("first_blocked_chapter")
    if (
        isinstance(blocked, bool)
        or not isinstance(blocked, int)
        or blocked != LAST_CHAPTER + 1
    ):
        return {"recoverable": False, "reason": "blocked_chapter_not_final"}

    progress = read_track_progress(track_root_path)
    if progress.get("last_completed") != LAST_CHAPTER:
        return {"recoverable": False, "reason": "last_completed_not_final"}
    if progress.get("next_chapter") != LAST_CHAPTER + 1:
        return {"recoverable": False, "reason": "next_chapter_not_parked"}
    last_audited = progress.get("last_audited_chapter")
    if (
        isinstance(last_audited, bool)
        or not isinstance(last_audited, int)
        or last_audited not in AUDIT_CHECKPOINTS
        or last_audited >= LAST_CHAPTER
    ):
        return {"recoverable": False, "reason": "last_audited_not_prior_boundary"}

    start_chapter = last_audited + 1
    end_chapter = LAST_CHAPTER
    window = f"Chapters {start_chapter}-{end_chapter}"
    if window not in reason:
        return {"recoverable": False, "reason": "reason_window_mismatch"}

    checkpoint_path = g11_audit_checkpoint_path(
        track_root_path, start_chapter, end_chapter
    )
    if not checkpoint_path.is_file():
        return {"recoverable": False, "reason": "audit_checkpoint_missing"}

    chapter_file = (
        Path(track_root_path) / "output" / "chapters" / f"{LAST_CHAPTER:04d}.md"
    )
    if not chapter_file.is_file():
        return {"recoverable": False, "reason": "final_chapter_not_committed"}

    if evidence_checkpoint_exists is None:
        root = Path(track_root_path).resolve()
        evidence_checkpoint_exists = g11_evidence_checkpoint_path(
            root.parent.parent, str(run_id), LAST_CHAPTER
        ).exists()
    if evidence_checkpoint_exists:
        return {"recoverable": False, "reason": "chapter_evidence_already_frozen"}

    journals = inspect_paid_request_journals(
        Path(track_root_path) / "data" / "provider_request_journal"
    )
    if journals.get("uncertain") or journals.get("errors"):
        return {"recoverable": False, "reason": "unresolved_provider_journals"}

    checkpoint = _read_object(checkpoint_path, "G11 audit checkpoint")
    expected_window = {"start_chapter": start_chapter, "end_chapter": end_chapter}
    if checkpoint.get("audit_window") != expected_window:
        return {"recoverable": False, "reason": "audit_checkpoint_window_mismatch"}
    if checkpoint.get("status") == "completed":
        return {"recoverable": False, "reason": "audit_checkpoint_already_complete"}

    leaves = [
        chunk for chunk in checkpoint.get("chunks", []) if _audit_chunk_is_leaf(chunk)
    ]
    if any(
        chunk.get("status") in G11_AUDIT_UNRESOLVED_CHUNK_STATUSES for chunk in leaves
    ):
        return {"recoverable": False, "reason": "unresolved_audit_chunk"}
    failed_chunks = [
        str(chunk.get("chunk_id"))
        for chunk in leaves
        if chunk.get("status") in G11_AUDIT_FAILED_CHUNK_STATUSES
    ]
    remaining = [
        chunk for chunk in leaves if chunk.get("status") != "completed"
    ]
    if not remaining:
        return {"recoverable": False, "reason": "no_failed_audit_chunk"}

    return {
        "recoverable": True,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "last_audited_chapter": last_audited,
        "failed_chunks": failed_chunks,
        "already_complete": False,
        "reason": reason,
        "blocked_chapter": blocked,
    }


def reset_failed_audit_chunks(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Reset only the failed leaf chunk(s) of a reusable audit checkpoint.

    Completed chunks, their results, and their attempt records are preserved
    exactly.  For a failed/validation-failed leaf chunk the stale attempt record
    and failure/validation state are removed from the *active* checkpoint (they
    remain preserved in the byte-for-byte archive) and the chunk is reset to
    ``pending`` with its attempt counter kept, so the engine's next attempt gets
    a brand-new operation identity.  An ``in_flight``/uncertain chunk fails
    closed instead of being reset.
    """
    chunks = checkpoint.get("chunks")
    if not isinstance(chunks, list):
        raise G11RunnerError("G11 audit checkpoint has no chunk list")
    leaves = [chunk for chunk in chunks if _audit_chunk_is_leaf(chunk)]
    unresolved = [
        str(chunk.get("chunk_id"))
        for chunk in leaves
        if chunk.get("status") in G11_AUDIT_UNRESOLVED_CHUNK_STATUSES
    ]
    if unresolved:
        raise G11RunnerError(
            "G11 audit chunk remote outcome is uncertain; resend is forbidden: "
            f"{unresolved}"
        )
    failed = [
        chunk
        for chunk in leaves
        if chunk.get("status") in G11_AUDIT_FAILED_CHUNK_STATUSES
    ]
    failed_ids = {str(chunk.get("chunk_id")) for chunk in failed}
    removed_operations: list[str] = []
    records = checkpoint.get("attempt_records")
    if not isinstance(records, list):
        records = []
    for record in list(records):
        if not isinstance(record, Mapping):
            continue
        if "carried_from_superseded_checkpoint" in record:
            continue
        if str(record.get("chunk_id")) not in failed_ids:
            continue
        removed_operations.append(str(record.get("operation_id")))
        records.remove(record)
    reset_chunks: list[dict[str, Any]] = []
    for chunk in failed:
        reset_chunks.append(
            {
                "chunk_id": str(chunk.get("chunk_id")),
                "previous_status": chunk.get("status"),
                "attempts": int(chunk.get("attempts", 0) or 0),
            }
        )
        chunk["status"] = "pending"
        chunk.pop("last_error", None)
        chunk.pop("recovery_classification", None)
        chunk.pop("result", None)
    return {
        "reset_chunks": reset_chunks,
        "removed_attempt_operations": removed_operations,
        "preserved_completed_chunks": [
            str(chunk.get("chunk_id"))
            for chunk in leaves
            if chunk.get("status") == "completed"
        ],
    }


def _snapshot_bytes(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    payload = destination.read_bytes()
    if payload != source.read_bytes():
        raise G11RunnerError(f"G11 audit recovery snapshot differs for {source.name}")
    return {
        "name": source.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def archive_audit_content_evidence(
    *,
    track_root_path: Path | str,
    run_id: str,
    model_id: str,
    novel_id: str,
    start_chapter: int,
    end_chapter: int,
    cycle: int,
    reason: str,
    clock: Any = None,
) -> dict[str, Any]:
    """Archive (never destroy) the audit quarantine/checkpoint/failed journals.

    The active quarantine marker and the audit checkpoint are copied
    byte-for-byte and left in place for the restart-safe mutation that follows.
    Provider journals belonging to the failed audit are moved byte-for-byte so
    the engine cannot replay the rejected response.
    """
    if cycle < 1 or cycle > G11_AUDIT_CONTENT_RECOVERY_MAX_CYCLES:
        raise G11RunnerError("G11 audit-recovery cycle is out of range")
    directory = audit_content_recovery_directory(
        track_root_path, start_chapter, end_chapter
    ) / f"cycle_{cycle:02d}"
    if directory.exists():
        raise G11RunnerError(
            f"G11 audit-recovery archive already exists and is never overwritten: "
            f"{directory}"
        )
    directory.mkdir(parents=True, exist_ok=False)

    archived: dict[str, Any] = {}
    quarantine_file = quarantine_path(track_root_path)
    if quarantine_file.is_file():
        archived["quarantine"] = _snapshot_bytes(
            quarantine_file, directory / "quarantine.json"
        )
    checkpoint_file = g11_audit_checkpoint_path(
        track_root_path, start_chapter, end_chapter
    )
    if checkpoint_file.is_file():
        archived["checkpoint"] = _snapshot_bytes(
            checkpoint_file,
            directory / f"audit_{start_chapter:04d}_{end_chapter:04d}.checkpoint.json",
        )
    journal_records: list[dict[str, Any]] = []
    journal_dir = Path(track_root_path) / "data" / "provider_request_journal"
    if journal_dir.is_dir():
        for path in sorted(journal_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix != ".json":
                continue
            if not path.name.startswith(f"{int(end_chapter):04d}."):
                continue
            if G11_AUDIT_JOURNAL_KIND_MARKER not in path.name:
                continue
            journal_records.append(
                _archive_bytes(path, directory / "provider_journal" / path.name)
            )
    archived["provider_journals"] = journal_records

    manifest = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_AUDIT_CONTENT_RECOVERY_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "run_id": run_id,
        "model_id": model_id,
        "novel_id": novel_id,
        "audit_window": {
            "start_chapter": int(start_chapter),
            "end_chapter": int(end_chapter),
        },
        "cycle": int(cycle),
        "reason": str(reason)[:1000],
        "archived": archived,
        "created_utc": _aware_utc_now(clock).isoformat(),
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return {
        "directory": str(directory),
        "manifest_path": str(directory / "manifest.json"),
        "manifest": manifest,
    }


def deepseek_low_mode_path(track_root_path: Path | str) -> Path:
    return Path(track_root_path) / "data" / G11_DEEPSEEK_LOW_MODE_FILENAME


def load_deepseek_low_mode(path: Path | str) -> dict[str, Any] | None:
    target = Path(path)
    if not target.is_file():
        return None
    marker = _read_object(target, "G11 DeepSeek low translation mode marker")
    if marker.get("benchmark_id") != G11_BENCHMARK_ID:
        raise G11RunnerError("G11 DeepSeek low-mode marker binds another benchmark")
    if marker.get("revision") != G11_DEEPSEEK_LOW_MODE_REVISION:
        raise G11RunnerError("G11 DeepSeek low-mode marker revision is unsupported")
    if marker.get("activated") is not True:
        raise G11RunnerError("G11 DeepSeek low-mode marker is not an activation")
    return marker


def activate_deepseek_low_mode(
    *,
    path: Path | str,
    run_id: str,
    model_id: str,
    chapter: int,
    reason: str,
    clock: Any = None,
) -> dict[str, Any]:
    """Persist the track-local adaptive low-mode marker (idempotent)."""
    if model_id != DEEPSEEK_MODEL_ID:
        raise G11RunnerError("adaptive low translation mode is only defined for DeepSeek")
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
        raise G11RunnerError("G11 low-mode activation chapter must be positive")
    target = Path(path)
    now = _aware_utc_now(clock).isoformat()
    existing = load_deepseek_low_mode(target)
    events = (
        list(existing.get("high_effort_saturation_events", [])) if existing else []
    )
    event = {"chapter": chapter, "reason": str(reason)[:200], "recorded_utc": now}
    if event not in events:
        events.append(event)
    marker = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_DEEPSEEK_LOW_MODE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "run_id": run_id,
        "model_id": model_id,
        "activated": True,
        "activation_chapter": (
            int(existing["activation_chapter"]) if existing else chapter
        ),
        "activation_reason": (
            existing.get("activation_reason") if existing else str(reason)[:200]
        ),
        "high_effort_saturation_events": events,
        "created_utc": existing.get("created_utc") if existing else now,
        "updated_utc": now,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, marker)
    return marker


def deepseek_low_mode_summary(marker: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(marker, Mapping) or marker.get("activated") is not True:
        return {
            "activated": False,
            "activation_chapter": None,
            "high_effort_saturation_events": 0,
        }
    events = marker.get("high_effort_saturation_events", [])
    return {
        "activated": True,
        "activation_chapter": marker.get("activation_chapter"),
        "high_effort_saturation_events": len(events) if isinstance(events, list) else 0,
    }


def track_adaptive_low_summary(track_root_path: Path | str) -> dict[str, Any]:
    try:
        marker = load_deepseek_low_mode(deepseek_low_mode_path(track_root_path))
    except G11RunnerError:
        return {
            "activated": False,
            "activation_chapter": None,
            "high_effort_saturation_events": 0,
        }
    return deepseek_low_mode_summary(marker)


def count_content_recovery_cycles(track_root_path: Path | str) -> int:
    """Count durable recovery cycles without mutating anything."""
    root = content_recovery_directory(track_root_path)
    if not root.is_dir():
        return 0
    total = 0
    for chapter_dir in root.iterdir():
        if not chapter_dir.is_dir() or not chapter_dir.name.isdigit():
            continue
        total += sum(
            1
            for item in chapter_dir.iterdir()
            if item.is_dir() and item.name.startswith("cycle_")
        )
    return total


def _translation_saturation_reason(record: Mapping[str, Any]) -> str:
    response = record.get("response")
    if not isinstance(response, Mapping):
        return ""
    return regeneration_reason(response)


def _note_deepseek_recovery_saturation(
    *,
    track_root_path: Path | str,
    run_id: str,
    model_id: str,
    chapter: int,
    runtime_state: Mapping[str, Any] | None,
    clock: Any = None,
) -> None:
    """Activate low mode from pre-patch evidence before a fresh recovery send."""
    if model_id != DEEPSEEK_MODEL_ID:
        return
    low_state = (runtime_state or {}).get("deepseek_low_mode")
    if isinstance(low_state, dict) and low_state.get("active"):
        return
    reason = ""
    for path in chapter_journal_paths(track_root_path, chapter):
        if "translation" not in path.name:
            continue
        try:
            record = _read_object(path, "G11 translation journal")
        except G11RunnerError:
            continue
        detected = _translation_saturation_reason(record)
        if detected:
            reason = f"pre_existing_high_effort_saturation:{detected}"
            break
    if not reason:
        return
    marker = activate_deepseek_low_mode(
        path=deepseek_low_mode_path(track_root_path),
        run_id=run_id,
        model_id=model_id,
        chapter=chapter,
        reason=reason,
        clock=clock,
    )
    low_state = (runtime_state or {}).get("deepseek_low_mode")
    if isinstance(low_state, dict):
        low_state["active"] = True
        low_state["marker"] = marker


def required_last_audited_chapter(last_completed: int) -> int:
    """Largest production audit boundary already owed for ``last_completed``."""
    required = 0
    for checkpoint in AUDIT_CHECKPOINTS:
        if last_completed >= checkpoint:
            required = checkpoint
    return required


def classify_track_progress(
    progress: Mapping[str, Any],
    *,
    quarantine: Mapping[str, Any] | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Classify one durable track without mutating it or contacting a provider."""

    if not isinstance(progress, Mapping):
        raise G11RunnerError("G11 progress must be a mapping")
    last_completed = progress.get("last_completed")
    next_chapter = progress.get("next_chapter")
    last_audited = progress.get("last_audited_chapter")
    for label, value in (
        ("last_completed", last_completed),
        ("next_chapter", next_chapter),
        ("last_audited_chapter", last_audited),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            return {"status": "corrupt_block", "reason": f"{label}_not_an_integer"}
    if last_completed < 0 or last_completed > LAST_CHAPTER:
        return {"status": "corrupt_block", "reason": "last_completed_out_of_range"}
    if next_chapter != last_completed + 1:
        return {"status": "corrupt_block", "reason": "next_chapter_mismatch"}
    if last_audited not in (0, *AUDIT_CHECKPOINTS):
        return {"status": "corrupt_block", "reason": "unexpected_last_audited_chapter"}
    if last_audited > last_completed:
        return {
            "status": "corrupt_block",
            "reason": "audit_marker_ahead_of_completed_chapters",
        }

    if quarantine is not None:
        if not isinstance(quarantine, Mapping):
            return {"status": "corrupt_block", "reason": "malformed_quarantine"}
        if quarantine.get("scope_kind") != "g11_model_novel_track":
            return {"status": "corrupt_block", "reason": "quarantine_scope_mismatch"}
        if expected_run_id is not None and quarantine.get("scope_id") != expected_run_id:
            return {"status": "corrupt_block", "reason": "quarantine_run_mismatch"}
        blocked = quarantine.get("first_blocked_chapter")
        if isinstance(blocked, bool) or not isinstance(blocked, int) or blocked < 1:
            return {"status": "corrupt_block", "reason": "malformed_quarantine_chapter"}
        return {
            "status": "quarantined",
            "reason": quarantine.get("outcome", "durable_quarantine"),
            "first_blocked_chapter": blocked,
            "last_completed": last_completed,
            "last_audited_chapter": last_audited,
        }

    if last_completed >= LAST_CHAPTER:
        return {
            "status": "complete",
            "last_completed": last_completed,
            "last_audited_chapter": last_audited,
        }

    return {
        "status": "ready_for_target",
        "target_chapter": next_chapter,
        "audit_pending": last_audited < required_last_audited_chapter(last_completed),
        "last_completed": last_completed,
        "last_audited_chapter": last_audited,
    }


# ===========================================================================
# Frozen request building (real engine shape -> Command Code shape)
# ===========================================================================


def _output_token_ceiling(logical_operation: str, requested: Any) -> int:
    if logical_operation == LIFECYCLE_OPERATION_TRANSLATION:
        return TRANSLATION_COMPLETION_CEILING
    if requested is None:
        return AUXILIARY_COMPLETION_CEILING
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise G11RunnerError("legacy auxiliary max token control is invalid")
    return min(requested, AUXILIARY_COMPLETION_CEILING)


def normalize_g11_request(
    *,
    model_id: str,
    logical_operation: str,
    request_kwargs: Mapping[str, Any],
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Convert a legacy production request into the frozen Command Code shape."""
    if logical_operation not in PURE_RUN_LIFECYCLE_OPERATIONS:
        raise G11RunnerError(f"unsupported lifecycle operation {logical_operation!r}")
    if model_id not in MODEL_ORDER:
        raise G11RunnerError(f"unsupported G11 model {model_id!r}")
    if not isinstance(request_kwargs, Mapping):
        raise G11RunnerError("legacy request kwargs must be a mapping")

    effort = EFFORT_BY_OPERATION[logical_operation]
    if reasoning_effort is not None:
        if logical_operation != LIFECYCLE_OPERATION_TRANSLATION:
            raise G11RunnerError(
                "G11 reasoning effort may only be overridden for translation"
            )
        if reasoning_effort not in G11_VERIFIED_REASONING_EFFORTS:
            raise G11RunnerError(
                f"G11 reasoning effort override {reasoning_effort!r} is not a "
                "live-verified level"
            )
        effort = reasoning_effort
    live = live_provider_capability(model_id)

    request = copy.deepcopy(dict(request_kwargs))
    legacy_model = request.pop("model", None)
    if legacy_model not in (model_id, live.provider_model_id):
        raise G11RunnerError(
            f"legacy request model {legacy_model!r} does not match "
            f"{model_id!r}/{live.provider_model_id!r}"
        )
    messages = request.pop("messages", None)
    max_tokens = request.pop("max_tokens", None)
    max_completion_tokens = request.pop("max_completion_tokens", None)
    if (
        max_tokens is not None
        and max_completion_tokens is not None
        and max_tokens != max_completion_tokens
    ):
        raise G11RunnerError("legacy request has conflicting max token controls")
    requested_tokens = (
        max_completion_tokens if max_completion_tokens is not None else max_tokens
    )
    legacy_extra_body = request.pop("extra_body", None)
    if legacy_extra_body not in (None, {"thinking": {"type": "disabled"}}):
        raise G11RunnerError("unexpected legacy provider-specific extra_body")
    if "reasoning_effort" in request:
        raise G11RunnerError(
            "legacy request must not pre-populate reasoning_effort; G11 owns it"
        )

    capability = verified_reasoning_capability(model_id)
    if capability is None:
        raise G11RunnerError(
            f"model {model_id!r} has no live-verified reasoning capability; "
            "G11 must not invent a reasoning parameter shape"
        )
    return build_provider_request(
        model=live.provider_model_id,
        messages=messages,
        max_output_tokens=_output_token_ceiling(logical_operation, requested_tokens),
        request_extras=request,
        reasoning_capability=capability,
        reasoning_level=effort,
    )


def _validate_saved_stats(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        raise G11RunnerError("saved G11 stats must be a mapping")
    value = copy.deepcopy(dict(stats))
    for name in _STATS_FIELDS:
        item = value.get(name, 0)
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or item < 0
        ):
            raise G11RunnerError(f"saved G11 stats contain invalid {name}")
    if all((value.get(name, 0) or 0) == 0 for name in _STATS_FIELDS):
        return value
    if value.get("pricing_version") != G11_PRICING_VERSION:
        raise G11RunnerError("saved G11 stats bind another pricing version")
    return value


def _stats_from_accepted(
    *,
    model_id: str,
    usage: Any,
    request_started_utc: str,
) -> dict[str, Any]:
    return g11_account_usage(model_id, usage, request_started_utc)


def _response_from_envelope(envelope: Mapping[str, Any]) -> Any:
    stats = _validate_saved_stats(envelope.get("stats"))
    usage = SimpleNamespace(
        prompt_tokens=int(stats["prompt_tokens"]),
        completion_tokens=int(stats["completion_tokens"]),
        total_tokens=int(stats["prompt_tokens"]) + int(stats["completion_tokens"]),
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=int(stats["cache_hit_tokens"])
        ),
        _g11_stats=copy.deepcopy(stats),
    )
    return SimpleNamespace(
        id=envelope.get("provider_response_id", ""),
        model=envelope.get("provider_response_model", ""),
        choices=[
            SimpleNamespace(
                finish_reason=envelope.get("finish_reason"),
                message=SimpleNamespace(content=envelope.get("content", "")),
            )
        ],
        usage=usage,
    )


def regeneration_reason(envelope: Mapping[str, Any]) -> str:
    """Return why a translation envelope is unusable, or an empty string."""
    if not isinstance(envelope, Mapping):
        return "missing_envelope"
    if envelope.get("finish_reason") == "length":
        return "finish_reason_length"
    content = envelope.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return "empty_or_blank_content"
    return ""


def _poisoned_transport(*_args: Any, **_kwargs: Any) -> Any:
    raise G11RunnerError(
        "G11 replay transport is deliberately poisoned; no send is authorized"
    )


def _logical_operation_kind(
    logical_operation: str,
    request_sha256: str,
    *,
    multi_request_slot: bool,
    regeneration: bool,
) -> str:
    if logical_operation not in _ALLOWED_OPERATIONS:
        raise G11RunnerError(f"unknown G11 lifecycle operation: {logical_operation!r}")
    if regeneration:
        return f"{_OPERATION_PREFIX}_{logical_operation}_regen_{request_sha256[:20]}"
    if not multi_request_slot:
        return f"{_OPERATION_PREFIX}_{logical_operation}"
    return f"{_OPERATION_PREFIX}_{logical_operation}_{request_sha256[:20]}"


def install_g11_paid_runtime(
    production_module: Any,
    *,
    model_id: str,
    provider_client: Any,
    ledger: G11Ledger,
    run_id: str,
    novel_id: str,
    clock: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    deepseek_low_mode_path: Path | str | None = None,
) -> dict[str, Any]:
    """Install the retrying, journaled, budget-enforcing G11 provider runtime."""
    if not getattr(production_module, "_RUNTIME_INITIALIZED", False):
        raise G11RunnerError("real production engine must be initialized first")
    if getattr(production_module, "client", None) is not None:
        raise G11RunnerError("provider-suppressed engine unexpectedly has a client")
    if getattr(production_module, "api_key", None) is not None:
        raise G11RunnerError("provider-suppressed engine unexpectedly retained a key")
    if provider_client is None:
        raise G11RunnerError("G11 runtime requires an injected Provider client")
    if model_id not in MODEL_ORDER:
        raise G11RunnerError(f"unknown G11 model id: {model_id!r}")
    if run_id not in TRACK_BY_ID or TRACK_BY_ID[run_id]["model_id"] != model_id:
        raise G11RunnerError("G11 runtime run id does not bind this model")
    if ledger.document["model_id"] != model_id:
        raise G11RunnerError("G11 ledger binds another model")
    if getattr(production_module, "_g11_paid_runtime_state", None) is not None:
        raise G11RunnerError("G11 paid runtime is already installed")

    missing = [
        name
        for name in (*_HOOK_SYMBOLS, *CONTEXT_ENTRYPOINTS)
        if not callable(getattr(production_module, name, None))
    ]
    if missing:
        raise G11RunnerError(
            "the real production engine lacks G11 hook symbols: "
            f"{sorted(missing)}"
        )

    use_clock = clock
    sleeper = _Sleeper(sleep)
    originals = {
        name: getattr(production_module, name)
        for name in (*_HOOK_SYMBOLS, *CONTEXT_ENTRYPOINTS)
    }
    operation_stack: list[tuple[str, int]] = []
    regenerated_chapters: set[int] = set()
    counters: dict[str, Any] = {
        "transport_attempts": 0,
        "new_sends": 0,
        "replays": 0,
        "regenerations": 0,
        "retries": 0,
        "reservations": [],
        "operations": [],
    }
    low_mode_marker_path = (
        None if deepseek_low_mode_path is None else Path(deepseek_low_mode_path)
    )
    initial_low_mode = (
        load_deepseek_low_mode(low_mode_marker_path)
        if low_mode_marker_path is not None
        else None
    )
    low_mode_state: dict[str, Any] = {
        "active": initial_low_mode is not None,
        "marker": initial_low_mode,
    }
    recovery_context: dict[str, Any] = {
        "active": False,
        "chapter": None,
        "cycle": 0,
    }

    def translation_effort_override() -> str | None:
        if model_id == DEEPSEEK_MODEL_ID and low_mode_state["active"]:
            return G11_DEEPSEEK_LOW_TRANSLATION_EFFORT
        return None

    def note_deepseek_saturation(chapter_number: int, reason: str) -> None:
        if low_mode_marker_path is None:
            return
        marker = activate_deepseek_low_mode(
            path=low_mode_marker_path,
            run_id=run_id,
            model_id=model_id,
            chapter=int(chapter_number),
            reason=reason,
            clock=use_clock,
        )
        low_mode_state["active"] = True
        low_mode_state["marker"] = marker

    def _with_recovery_instruction(
        request_kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        adjusted = copy.deepcopy(dict(request_kwargs))
        messages = adjusted.get("messages")
        instruction = {
            "role": "system",
            "content": G11_RECOVERY_TRANSLATION_INSTRUCTION,
        }
        if isinstance(messages, list):
            adjusted["messages"] = [*messages, instruction]
        else:
            adjusted["messages"] = [instruction]
        return adjusted

    def current_context() -> tuple[str, int]:
        if not operation_stack:
            raise G11RunnerError(
                "provider-shaped request occurred outside an explicit G11 "
                "lifecycle context"
            )
        return operation_stack[-1]

    @contextmanager
    def operation_context(logical_operation: str, chapter: int):
        if logical_operation not in _ALLOWED_OPERATIONS:
            raise G11RunnerError("invalid G11 operation context")
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise G11RunnerError("G11 operation context chapter must be positive")
        operation_stack.append((logical_operation, chapter))
        try:
            yield
        finally:
            popped = operation_stack.pop()
            if popped != (logical_operation, chapter):
                raise G11RunnerError("G11 operation context stack corrupted")

    def physical_send(actual_request: Mapping[str, Any], reservation_id: str) -> Any:
        """Up to three physical transport attempts for one logical operation."""
        last_error: BaseException | None = None
        for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                raw = send_chat_completion_once(provider_client, actual_request)
            except BaseException as exc:  # noqa: BLE001 - classified below
                classification = classify_transport_failure(exc)
                elapsed = time.perf_counter() - started
                outcome = (
                    "transient_failure"
                    if classification == "transient"
                    else "deterministic_failure"
                )
                ledger.record_attempt(
                    reservation_id=reservation_id,
                    attempt_index=attempt,
                    outcome=outcome,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    classification=classification,
                    elapsed_seconds=elapsed,
                    clock=use_clock,
                )
                counters["transport_attempts"] += 1
                last_error = exc
                retryable = (
                    classification == "transient"
                    and attempt < MAX_TRANSPORT_ATTEMPTS
                )
                if not retryable:
                    if classification == "transient":
                        raise G11TransportExhausted(
                            "G11 exhausted "
                            f"{MAX_TRANSPORT_ATTEMPTS} transient transport attempts: "
                            f"{type(exc).__name__}: {str(exc)[:300]}"
                        ) from exc
                    raise G11DeterministicTransportFailure(
                        "G11 transport failure is not retryable: "
                        f"{type(exc).__name__}: {str(exc)[:300]}"
                    ) from exc
                counters["retries"] += 1
                sleeper(RETRY_BACKOFF_SECONDS[attempt - 1])
                continue

            elapsed = time.perf_counter() - started
            ledger.record_attempt(
                reservation_id=reservation_id,
                attempt_index=attempt,
                outcome="success",
                elapsed_seconds=elapsed,
                clock=use_clock,
            )
            counters["transport_attempts"] += 1
            return SimpleNamespace(
                raw=raw,
                started_utc=_aware_utc_now(use_clock).isoformat(),
            )
        raise G11TransportExhausted(  # pragma: no cover - loop always returns/raises
            f"G11 transport exhausted: {last_error!r}"
        )

    def journaled_subrequest(
        logical_operation: str,
        chapter_number: int,
        source_text: str,
        request_kwargs: Mapping[str, Any],
        *,
        translation_text: str = "",
        multi_request_slot: bool,
        regeneration: bool = False,
    ) -> dict[str, Any]:
        effective_kwargs: Mapping[str, Any] = request_kwargs
        if (
            logical_operation == LIFECYCLE_OPERATION_TRANSLATION
            and recovery_context.get("active")
        ):
            effective_kwargs = _with_recovery_instruction(request_kwargs)
        actual_request = normalize_g11_request(
            model_id=model_id,
            logical_operation=logical_operation,
            request_kwargs=effective_kwargs,
            reasoning_effort=(
                translation_effort_override()
                if logical_operation == LIFECYCLE_OPERATION_TRANSLATION
                else None
            ),
        )
        provider_identity = build_provider_request_identity(
            request_body=actual_request
        )
        active_novel_id = (
            production_module.ACTIVE_NOVEL_CONFIG.id
            if production_module.ACTIVE_NOVEL_CONFIG is not None
            else novel_id
        )
        profile_id = (
            getattr(production_module.ACTIVE_PROFILE, "id", "") or "unknown"
        )
        request_sha = provider_identity.request_sha256
        journal_identity = request_identity(
            operation_kind=logical_operation,
            novel_id=active_novel_id,
            chapter=chapter_number,
            provider=PROVIDER_NAME,
            model=provider_identity.requested_model,
            profile_id=profile_id,
            source_sha256=_sha256_text(source_text),
            translation_sha256=(
                _sha256_text(translation_text) if translation_text else ""
            ),
            request_payload=actual_request,
        )
        if journal_identity["request_sha256"] != request_sha:
            raise G11RunnerError("G11 journal and provider request identities differ")
        journal_kind = _logical_operation_kind(
            logical_operation,
            request_sha,
            multi_request_slot=multi_request_slot,
            regeneration=regeneration,
        )
        journal_path = paid_request_journal_path(
            production_module.PAID_REQUEST_JOURNAL_DIR,
            journal_kind,
            chapter_number,
        )
        existing = load_matching_journal(journal_path, journal_identity)
        journal_status = None if existing is None else existing["status"]

        if journal_status == "response_recorded":
            result = run_journaled_paid_request(
                journal_path=journal_path,
                identity=journal_identity,
                save_json=lambda path, value: atomic_write_json(path, value),
                prepare_attempt=lambda: _poisoned_transport(),
                send_once=lambda: _poisoned_transport(),
                normalize_response=(
                    lambda *_args, **_kwargs: _poisoned_transport()
                ),
            )
            if result.get("replayed") is not True or result.get("api_called"):
                raise G11RunnerError(
                    "G11 replay unexpectedly attempted provider transport"
                )
            counters["replays"] += 1
            counters["operations"].append(
                {
                    "logical_operation": logical_operation,
                    "chapter": chapter_number,
                    "journal_kind": journal_kind,
                    "replayed": True,
                    "regeneration": bool(regeneration),
                }
            )
            return result

        if journal_status in {"started", "uncertain"}:
            raise G11AmbiguousOutcome(
                "G11 paid subrequest has an unresolved "
                f"{journal_status} durable outcome; automatic reissue is forbidden",
                request_sha256=request_sha,
                chapter=chapter_number,
            )

        ledger.ensure_budget_available()
        reservation_id = ledger.begin_operation(
            operation=logical_operation,
            request_sha256=request_sha,
            run_id=run_id,
            novel_id=active_novel_id,
            chapter=chapter_number,
            regeneration=regeneration,
            clock=use_clock,
        )
        counters["new_sends"] += 1

        def prepare_attempt() -> dict[str, Any]:
            return {
                "mode": G11_RUNNER_REVISION,
                "run_id": run_id,
                "model_id": model_id,
                "target_chapter": chapter_number,
                "logical_operation": logical_operation,
                "request_sha256": request_sha,
                "reservation_id": reservation_id,
                "regeneration": bool(regeneration),
                "pricing_version": G11_PRICING_VERSION,
            }

        def normalize_response(
            attempt_result: Any,
            _attempt_metadata: Mapping[str, Any],
        ) -> dict[str, Any]:
            normalized = normalize_provider_response(
                attempt_result.raw,
                request_identity=provider_identity,
            )
            accepted = accept_provider_response(provider_identity, normalized)
            stats = _stats_from_accepted(
                model_id=model_id,
                usage=accepted.usage,
                request_started_utc=attempt_result.started_utc,
            )
            return {
                "content": accepted.content,
                "finish_reason": accepted.finish_reason,
                "stats": stats,
                "provider_response_id": accepted.provider_response_id,
                "provider_response_model": accepted.response_model,
                "request_sha256": request_sha,
                "logical_operation": logical_operation,
                "reservation_id": reservation_id,
                "regeneration": bool(regeneration),
                "accepted_credit_equivalent": stats["credit_equivalent"],
            }

        try:
            result = run_journaled_paid_request(
                journal_path=journal_path,
                identity=journal_identity,
                save_json=lambda path, value: atomic_write_json(path, value),
                prepare_attempt=prepare_attempt,
                send_once=lambda: physical_send(actual_request, reservation_id),
                normalize_response=normalize_response,
            )
        except G11DeterministicTransportFailure as exc:
            ledger.abandon_no_usage(
                reservation_id=reservation_id,
                reason=f"deterministic transport failure: {exc}",
            )
            raise
        except BaseException as exc:  # noqa: BLE001 - ambiguity is durable
            ledger.mark_unresolved(
                reservation_id=reservation_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            raise G11AmbiguousOutcome(
                "G11 paid subrequest did not reach a durable response: "
                f"{type(exc).__name__}: {str(exc)[:300]}",
                request_sha256=request_sha,
                chapter=chapter_number,
            ) from exc

        if not isinstance(result, Mapping) or result.get("api_called") is not True:
            raise G11RunnerError(
                "G11 paid journal did not report exactly one logical send"
            )
        envelope = result.get("response")
        if not isinstance(envelope, Mapping):
            raise G11RunnerError("G11 paid journal returned no accepted envelope")
        ledger.settle(
            reservation_id=reservation_id,
            stats=envelope["stats"],
            clock=use_clock,
        )
        counters["reservations"].append(
            {
                "logical_operation": logical_operation,
                "chapter": chapter_number,
                "journal_kind": journal_kind,
                "request_sha256": request_sha,
                "reservation_id": reservation_id,
                "regeneration": bool(regeneration),
                "accepted_credit_equivalent": envelope[
                    "accepted_credit_equivalent"
                ],
                "replayed": False,
            }
        )
        counters["operations"].append(
            {
                "logical_operation": logical_operation,
                "chapter": chapter_number,
                "journal_kind": journal_kind,
                "replayed": False,
                "regeneration": bool(regeneration),
            }
        )
        return result

    def journaled_chat_request(
        operation_kind: str,
        chapter_number: int,
        source_text: str,
        request_kwargs: Mapping[str, Any],
        *,
        translation_text: str = "",
    ) -> dict[str, Any]:
        try:
            logical = DIRECT_LEGACY_OPERATION_MAP[operation_kind]
        except KeyError as exc:
            raise G11RunnerError(
                f"unmapped direct legacy operation {operation_kind!r}"
            ) from exc
        result = journaled_subrequest(
            logical,
            chapter_number,
            source_text,
            request_kwargs,
            translation_text=translation_text,
            multi_request_slot=False,
        )
        if logical != LIFECYCLE_OPERATION_TRANSLATION:
            return result
        if int(chapter_number) in regenerated_chapters:
            return result
        reason = regeneration_reason(result.get("response", {}))
        if not reason:
            return result
        if model_id == DEEPSEEK_MODEL_ID and not low_mode_state["active"]:
            note_deepseek_saturation(int(chapter_number), reason)
        regenerated_chapters.add(int(chapter_number))
        counters["regenerations"] += 1
        return journaled_subrequest(
            logical,
            chapter_number,
            source_text,
            request_kwargs,
            translation_text=translation_text,
            multi_request_slot=False,
            regeneration=True,
        )

    def auxiliary_request(request_kwargs: Mapping[str, Any]) -> Any:
        logical, chapter = current_context()
        messages = request_kwargs.get("messages")
        if not isinstance(messages, list):
            raise G11RunnerError("auxiliary request lacks messages")
        source_text = "\n".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, Mapping)
        )
        result = journaled_subrequest(
            logical,
            chapter,
            source_text,
            request_kwargs,
            multi_request_slot=True,
        )
        return _response_from_envelope(result["response"])

    def call_api_with_retries(**kwargs: Any) -> Any:
        return auxiliary_request(kwargs)

    def send_api_request_once(**kwargs: Any) -> Any:
        return auxiliary_request(kwargs)

    def usage_and_cost(usage: Any) -> dict[str, Any]:
        stats = getattr(usage, "_g11_stats", None)
        if stats is None:
            raise G11RunnerError("G11 response usage lacks bound accounting stats")
        return _validate_saved_stats(stats)

    def usage_and_cost_for_pricing(usage: Any, _pricing: Any) -> dict[str, Any]:
        return usage_and_cost(usage)

    def reprice_saved_stats_if_needed(stats: Any) -> dict[str, Any]:
        return _validate_saved_stats(stats)

    def pricing_snapshot(moment: Any = None) -> dict[str, Any]:
        return g11_pricing_snapshot(
            model_id,
            moment if moment is not None else _aware_utc_now(use_clock),
        )

    production_module.MODEL = PROVIDER_MODEL_IDS[model_id]
    production_module.client = None
    production_module.api_key = None
    production_module.AUTO_OFF_PEAK_ONLY = False
    production_module.LAST_SUCCESSFUL_API_PRICING = None
    production_module.journaled_chat_request = journaled_chat_request
    production_module.call_api_with_retries = call_api_with_retries
    production_module.send_api_request_once = send_api_request_once
    production_module.usage_and_cost = usage_and_cost
    production_module.usage_and_cost_for_pricing = usage_and_cost_for_pricing
    production_module.reprice_saved_stats_if_needed = reprice_saved_stats_if_needed
    production_module.pricing_snapshot = pricing_snapshot

    wrapped_entrypoints: dict[str, Any] = {}
    for name, logical_operation in CONTEXT_ENTRYPOINTS.items():
        original = originals[name]

        def make_wrapper(original_callable: Any, operation: str):
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                if args:
                    chapter = args[0]
                else:
                    chapter = kwargs.get("chapter_number")
                    if chapter is None and operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT:
                        chapter = kwargs.get("end_chapter")
                if operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT and len(args) >= 2:
                    chapter = args[1]
                with operation_context(operation, int(chapter)):
                    return original_callable(*args, **kwargs)

            return wrapped

        wrapper = make_wrapper(original, logical_operation)
        setattr(production_module, name, wrapper)
        wrapped_entrypoints[name] = wrapper

    state = {
        "mode": G11_RUNNER_REVISION,
        "model_id": model_id,
        "provider_model_id": PROVIDER_MODEL_IDS[model_id],
        "run_id": run_id,
        "novel_id": novel_id,
        "journaled_operations": sorted(_ALLOWED_OPERATIONS),
        "context_entrypoints": dict(CONTEXT_ENTRYPOINTS),
        "ledger_path": str(ledger.path),
        "counters": counters,
        "originals": originals,
        "wrapped_entrypoints": wrapped_entrypoints,
        "sleeper": sleeper,
        "provider_transport": G11_PROVIDER_TRANSPORT,
        "provider_client_max_retries": 0,
        "wrapper_transport_attempt_ceiling": MAX_TRANSPORT_ATTEMPTS,
        "provider_calls_authorized": True,
        "deepseek_low_mode": low_mode_state,
        "recovery_context": recovery_context,
        "deepseek_low_mode_path": (
            None if deepseek_low_mode_path is None else str(deepseek_low_mode_path)
        ),
    }
    production_module._g11_paid_runtime_state = state
    return state


# ===========================================================================
# Checkpoint evidence (read-only extraction)
# ===========================================================================


def _copy_evidence_file(source: Path, destination: Path, label: str) -> dict[str, Any] | None:
    if not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    payload = destination.read_bytes()
    if payload != source.read_bytes():
        raise G11RunnerError(f"G11 checkpoint copy differs for {label}")
    return {
        "source": source.name,
        "destination": destination.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def freeze_checkpoints_for_chapter(
    *,
    execution_root: Path | str,
    run_id: str,
    chapter: int,
    clock: Any = None,
) -> dict[str, Any]:
    """Freeze the read-only quality/state evidence for one committed chapter."""
    if run_id not in TRACK_BY_ID:
        raise G11RunnerError(f"unknown G11 run id: {run_id!r}")
    if chapter not in QUALITY_CHECKPOINTS and chapter not in STATE_EVIDENCE_CHECKPOINTS:
        return {"frozen": False, "reason": "chapter_is_not_a_checkpoint"}
    root = track_root(execution_root, run_id)
    if not root.is_dir():
        raise G11RunnerError(f"G11 track root is missing: {root}")

    before = _track_fingerprint(root)
    evidence = (
        Path(execution_root).resolve()
        / G11_EVIDENCE_DIRECTORY
        / run_id
        / f"{chapter:04d}"
    )
    if evidence.exists():
        raise G11RunnerError(
            f"G11 checkpoint evidence already exists and is never overwritten: {evidence}"
        )
    evidence.mkdir(parents=True, exist_ok=False)

    artifacts: dict[str, Any] = {}
    quality = chapter in QUALITY_CHECKPOINTS
    state = chapter in STATE_EVIDENCE_CHECKPOINTS
    chapter_file = root / "output" / "chapters" / f"{chapter:04d}.md"
    qa_file = root / "output" / "qa" / f"{chapter:04d}.json"
    if quality:
        for label, source, name in (
            ("chapter", chapter_file, "chapter.md"),
            ("qa", qa_file, "qa.json"),
            ("progress", root / "data" / "progress.json", "progress.json"),
            ("glossary", root / "data" / "glossary.json", "glossary.json"),
            ("continuity", root / "data" / "continuity.json", "continuity.json"),
            ("issues", root / "data" / "issues.json", "issues.json"),
        ):
            record = _copy_evidence_file(source, evidence / name, label)
            if record is not None:
                artifacts[label] = record
    if state:
        for label, source, name in (
            ("audit_history", root / "data" / "audit_history.json", "audit_history.json"),
            ("glossary", root / "data" / "glossary.json", "glossary.json"),
            ("continuity", root / "data" / "continuity.json", "continuity.json"),
            ("progress", root / "data" / "progress.json", "progress.json"),
        ):
            record = _copy_evidence_file(source, evidence / name, label)
            if record is not None:
                artifacts[label] = record

    manifest = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_RUNNER_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "run_id": run_id,
        "model_id": TRACK_BY_ID[run_id]["model_id"],
        "novel_id": TRACK_BY_ID[run_id]["novel_id"],
        "chapter": chapter,
        "quality_checkpoint": quality,
        "state_evidence_checkpoint": state,
        "artifacts": artifacts,
        "frozen_utc": _aware_utc_now(clock).isoformat(),
        "track_unchanged_by_extraction": True,
    }
    atomic_write_json(evidence / "checkpoint.json", manifest)

    after = _track_fingerprint(root)
    if before != after:
        raise G11RunnerError(
            "G11 checkpoint extraction mutated the track root; refusing to continue"
        )
    return {"frozen": True, "path": str(evidence), "manifest": manifest}


# ===========================================================================
# Preparation: four fresh S0 experiment copies
# ===========================================================================


def _pristine_novel_projects(pristine_root: Path) -> dict[str, Path]:
    runs = pristine_root / "runs"
    if not runs.is_dir():
        raise G11RunnerError(
            f"pristine S0 template has no runs/ directory: {pristine_root}"
        )
    discovered: dict[str, Path] = {}
    for candidate in sorted(runs.iterdir(), key=lambda item: item.name):
        config_file = candidate / "novel.json"
        if not config_file.is_file():
            continue
        config = load_novel_config(config_file)
        if config.id in NOVELS and config.id not in discovered:
            discovered[config.id] = candidate
    missing = sorted(set(NOVELS) - set(discovered))
    if missing:
        raise G11RunnerError(
            f"pristine S0 template lacks novel projects: {missing}"
        )
    return discovered


def _validate_pristine_project(root: Path, novel_id: str) -> dict[str, Any]:
    config = load_novel_config(root / "novel.json")
    if config.id != novel_id:
        raise G11RunnerError("pristine project path/config id mismatch")
    frozen = NOVELS[novel_id]
    for field in (
        "source_language",
        "target_language",
        "profile",
        "final_chapter",
        "audit_interval",
        "chapter_numbering",
    ):
        if getattr(config, field) != frozen[field]:
            raise G11RunnerError(
                f"pristine {novel_id} config drifted for {field}"
            )
    suffix = "txt" if config.source_type == "txt" else "epub"
    source = root / "source" / f"novel.{suffix}"
    chapters_path = root / "data" / "chapters.json"
    addendum = root / "translation_addendum.txt"
    for label, path in (
        ("source", source),
        ("chapter library", chapters_path),
        ("translation addendum", addendum),
    ):
        if not path.is_file():
            raise G11RunnerError(f"pristine {novel_id} {label} is missing: {path}")
    chapters = _read_json(chapters_path)
    errors = validate_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    if errors:
        raise G11RunnerError(
            f"invalid pristine chapter library for {novel_id}: " + "; ".join(errors)
        )
    chapter_map = index_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    missing = [n for n in range(FIRST_CHAPTER, LAST_CHAPTER + 1) if n not in chapter_map]
    if missing:
        raise G11RunnerError(f"pristine {novel_id} lacks chapters: {missing}")
    return {
        "root": root,
        "config": config,
        "source": source,
        "chapters": chapters_path,
        "addendum": addendum,
    }


def _s0_state() -> dict[str, Any]:
    state = initial_project_state()
    state["progress.json"] = copy.deepcopy(state["progress.json"])
    state["progress.json"]["pricing_version"] = G11_S0_PRICING_PLACEHOLDER
    return state


def _write_track_root(
    destination: Path,
    *,
    track: Mapping[str, Any],
    pristine: Mapping[str, Any],
    clock: Any = None,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    for relative in PROJECT_DIRECTORIES:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for relative in (
        "data/glossary_candidate_resolutions",
        "data/provider_request_journal",
        G11_INVOCATION_DIRECTORY,
    ):
        (destination / relative).mkdir(parents=True, exist_ok=True)

    novel_id = track["novel_id"]
    config_path = destination / "novel.json"
    shutil.copyfile(Path(pristine["root"]) / "novel.json", config_path)
    shutil.copyfile(
        Path(pristine["addendum"]), destination / "translation_addendum.txt"
    )
    source_file = Path(pristine["source"])
    shutil.copyfile(source_file, destination / "source" / source_file.name)
    shutil.copyfile(
        Path(pristine["chapters"]), destination / "data" / "chapters.json"
    )

    # The ONLY intentional deviations from the pristine S0 copy: the G11 pricing
    # placeholder stamp in progress.json and the preregistered residual-source
    # micro-repair enablement in novel.json.  Both are experiment-copy settings.
    for name, value in _s0_state().items():
        if name == "chapters.json":
            continue
        atomic_write_json(destination / "data" / name, value)

    config = load_novel_config(config_path)
    config_dict = config.to_dict()
    config_dict["source_type"] = config.source_type
    config_dict["residual_cjk_micro_repair"] = dict(RESIDUAL_CJK_MICRO_REPAIR)
    atomic_write_json(config_path, config_dict)
    if not load_novel_config(config_path).residual_cjk_micro_repair.enabled:
        raise G11RunnerError("G11 experiment copy did not enable residual repair")

    run_identity = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_RUNNER_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
        "run_index": track["run_index"],
        "run_id": track["run_id"],
        "relative_root": f"runs/{track['run_id']}",
        "model_id": track["model_id"],
        "provider_model_id": track["provider_model_id"],
        "novel_id": novel_id,
        "profile_id": track["profile_id"],
        "first_chapter": FIRST_CHAPTER,
        "last_chapter": LAST_CHAPTER,
        "audit_interval": AUDIT_INTERVAL,
        "audit_checkpoints": list(AUDIT_CHECKPOINTS),
        "quality_checkpoints": list(QUALITY_CHECKPOINTS),
        "translation_effort": track["translation_effort"],
        "structured_effort": track["structured_effort"],
        "semantic_effort": track["semantic_effort"],
        "source_bindings": {
            "novel_config_sha256": _sha256_text(
                config_path.read_text(encoding="utf-8")
            ),
            "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
            "chapter_library_sha256": hashlib.sha256(
                (destination / "data" / "chapters.json").read_bytes()
            ).hexdigest(),
            "addendum_sha256": hashlib.sha256(
                (destination / "translation_addendum.txt").read_bytes()
            ).hexdigest(),
        },
        "residual_cjk_micro_repair": dict(RESIDUAL_CJK_MICRO_REPAIR),
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "created_utc": _aware_utc_now(clock).isoformat(),
    }
    run_identity["identity_sha256"] = _sha256_json(run_identity)
    atomic_write_json(destination / G11_RUN_IDENTITY_FILENAME, run_identity)
    return run_identity


def prepare_g11(
    *,
    execution_root: Path | str,
    pristine_root: Path | str = G11_DEFAULT_PRISTINE_ROOT,
    production_root: Path | str | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
    clock: Any = None,
) -> dict[str, Any]:
    """Create the four fresh G11 track copies from the pristine S0 template.

    Performs zero provider access, reads no credential, and never mutates the
    pristine S0 template or the live production novel state.
    """
    pristine = Path(pristine_root).expanduser().resolve()
    if not pristine.is_dir():
        raise G11RunnerError(f"pristine S0 template is missing: {pristine}")
    code_root = Path(__file__).resolve().parent
    source_binding_proof = assert_source_bindings(code_root)
    extra_forbidden: list[Any] = [pristine, code_root, *forbidden_roots]
    if production_root is not None:
        extra_forbidden.append(Path(production_root).expanduser().resolve())
    execution = assert_fresh_execution_root(
        execution_root,
        forbidden_roots=tuple(extra_forbidden),
    )
    if execution.exists():
        raise G11RunnerError(
            f"G11 execution root already exists and is never overwritten: {execution}"
        )

    pristine_fingerprint = _tree_fingerprint(pristine)
    discovery = _pristine_novel_projects(pristine)
    pristine_projects = {
        novel_id: _validate_pristine_project(root, novel_id)
        for novel_id, root in discovery.items()
    }

    execution.parent.mkdir(parents=True, exist_ok=True)
    staging = execution.parent / f".{execution.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        run_records = []
        for track in TRACKS:
            identity = _write_track_root(
                staging / "runs" / track["run_id"],
                track=track,
                pristine=pristine_projects[track["novel_id"]],
                clock=clock,
            )
            run_records.append(
                {
                    "run_id": track["run_id"],
                    "run_index": track["run_index"],
                    "model_id": track["model_id"],
                    "novel_id": track["novel_id"],
                    "relative_root": identity["relative_root"],
                    "run_identity_sha256": identity["identity_sha256"],
                }
            )
        (staging / G11_LEDGER_DIRECTORY).mkdir(parents=True, exist_ok=True)
        (staging / G11_EVIDENCE_DIRECTORY).mkdir(parents=True, exist_ok=True)

        manifest = {
            "schema_version": G11_RUNNER_SCHEMA_VERSION,
            "revision": G11_RUNNER_REVISION,
            "benchmark_id": G11_BENCHMARK_ID,
            "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
            "pristine_root": str(pristine),
            "pristine_tree_sha256": pristine_fingerprint,
            "production_root": (
                None
                if production_root is None
                else str(Path(production_root).expanduser().resolve())
            ),
            "track_count": len(run_records),
            "chapters_per_track": CHAPTER_COUNT_PER_TRACK,
            "first_chapter": FIRST_CHAPTER,
            "last_chapter": LAST_CHAPTER,
            "audit_checkpoints": list(AUDIT_CHECKPOINTS),
            "quality_checkpoints": list(QUALITY_CHECKPOINTS),
            "state_evidence_checkpoints": list(STATE_EVIDENCE_CHECKPOINTS),
            "model_budgets": dict(MODEL_BUDGETS),
            "runs": run_records,
            "source_bindings_verified": source_binding_proof,
            "provider_calls": 0,
            "credential_access": False,
            "created_utc": _aware_utc_now(clock).isoformat(),
        }
        manifest["manifest_sha256"] = _sha256_json(manifest)
        atomic_write_json(staging / G11_EXECUTION_MANIFEST_FILENAME, manifest)

        if _tree_fingerprint(pristine) != pristine_fingerprint:
            raise G11RunnerError(
                "pristine S0 template changed while preparing G11 roots"
            )
        if execution.exists():
            raise G11RunnerError(
                "G11 execution root appeared during create-once staging"
            )
        os.rename(staging, execution)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return verify_g11(
        execution_root=execution,
        pristine_root=pristine,
        production_root=production_root,
    ) | {"manifest": manifest, "manifest_sha256": manifest["manifest_sha256"]}


def _read_manifest(execution_root: Path | str) -> dict[str, Any]:
    path = Path(execution_root).resolve() / G11_EXECUTION_MANIFEST_FILENAME
    manifest = _read_object(path, "G11 execution manifest")
    check = copy.deepcopy(manifest)
    stored = check.pop("manifest_sha256", None)
    if not isinstance(stored, str) or _sha256_json(check) != stored:
        raise G11RunnerError("G11 execution manifest SHA does not recompute")
    if manifest.get("benchmark_id") != G11_BENCHMARK_ID:
        raise G11RunnerError("G11 execution manifest binds another benchmark")
    if manifest.get("g11_preregistration_sha256") != G11_PREREGISTRATION_SHA256:
        raise G11RunnerError("G11 execution manifest binds another preregistration")
    return manifest


def resolve_manifest_track(
    execution_root: Path | str,
    run_id: str,
) -> dict[str, Any]:
    manifest = _read_manifest(execution_root)
    records = [
        record
        for record in manifest.get("runs", [])
        if isinstance(record, Mapping) and record.get("run_id") == run_id
    ]
    if len(records) != 1:
        raise G11RunnerError(f"G11 run {run_id!r} is not uniquely bound in the manifest")
    record = records[0]
    track = TRACK_BY_ID.get(run_id)
    if track is None:
        raise G11RunnerError(f"unknown G11 run id: {run_id!r}")
    identity = _read_object(
        track_root(execution_root, run_id) / G11_RUN_IDENTITY_FILENAME,
        "G11 run identity",
    )
    check = copy.deepcopy(identity)
    stored = check.pop("identity_sha256", None)
    if not isinstance(stored, str) or _sha256_json(check) != stored:
        raise G11RunnerError("G11 run identity SHA does not recompute")
    for field, expected in {
        "run_id": run_id,
        "run_index": track["run_index"],
        "model_id": track["model_id"],
        "novel_id": track["novel_id"],
        "first_chapter": FIRST_CHAPTER,
        "last_chapter": LAST_CHAPTER,
        "relative_root": f"runs/{run_id}",
        "state": "s0_unexecuted",
        "provider_calls": 0,
    }.items():
        if identity.get(field) != expected:
            raise G11RunnerError(f"G11 run identity changed for {field}")
    if record.get("run_identity_sha256") != stored:
        raise G11RunnerError("G11 manifest/run identity mismatch")
    return record


def ledger_path(execution_root: Path | str, model_id: str) -> Path:
    return (
        Path(execution_root).resolve()
        / G11_LEDGER_DIRECTORY
        / f"{MODEL_SLUGS[model_id]}.json"
    )


def open_model_ledger(
    execution_root: Path | str,
    model_id: str,
    *,
    clock: Any = None,
) -> G11Ledger:
    return G11Ledger.open_or_create(
        path=ledger_path(execution_root, model_id),
        model_id=model_id,
        ceiling=MODEL_BUDGETS[model_id],
        clock=clock,
    )


# ===========================================================================
# Execution
# ===========================================================================


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def default_production_module_loader(*, code_root: Path | str, track_root_path: Path | str):
    """Load and initialize the real production engine with provider suppressed."""
    from full_pipeline_luna_runtime import (
        initialize_engine_without_provider,
        load_experiment_engine_module,
    )

    production_script = (
        Path(code_root).resolve() / G11_PRODUCTION_SCRIPT_NAME
    )
    if not production_script.is_file():
        raise G11RunnerError(f"real production script is missing: {production_script}")
    with _working_directory(Path(track_root_path).resolve()):
        loaded = load_experiment_engine_module(production_script)
        initialize_engine_without_provider(loaded["module"])
    return loaded


def _counter_snapshot(runtime_state: Mapping[str, Any] | None) -> dict[str, Any]:
    counters = (runtime_state or {}).get("counters", {}) or {}
    return {
        "transport_attempts": int(counters.get("transport_attempts", 0)),
        "new_sends": int(counters.get("new_sends", 0)),
        "replays": int(counters.get("replays", 0)),
        "regenerations": int(counters.get("regenerations", 0)),
        "retries": int(counters.get("retries", 0)),
        "reservations": len(counters.get("reservations", [])),
    }


def _write_invocation_ledger(
    *,
    track_root_path: Path,
    run_id: str,
    model_id: str,
    chapter: int,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    reservations: list[dict[str, Any]],
) -> Path:
    path = (
        Path(track_root_path)
        / G11_INVOCATION_DIRECTORY
        / f"{chapter:04d}.json"
    )
    record = {
        "schema_version": G11_RUNNER_SCHEMA_VERSION,
        "revision": G11_RUNNER_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "run_id": run_id,
        "model_id": model_id,
        "chapter": chapter,
        "lifecycle_invocations": 1,
        "provider_transport_attempts": (
            int(after["transport_attempts"]) - int(before["transport_attempts"])
        ),
        "new_provider_sends": int(after["new_sends"]) - int(before["new_sends"]),
        "replayed_subrequests": int(after["replays"]) - int(before["replays"]),
        "translation_regenerations": (
            int(after["regenerations"]) - int(before["regenerations"])
        ),
        "retries": int(after["retries"]) - int(before["retries"]),
        "subrequests": copy.deepcopy(reservations),
        "observed_subrequest_count": len(reservations),
        "automatic_chapter_advance": False,
        "automatic_retry": False,
        "synthetic_audit_inserted": False,
        "write_utc": _aware_utc_now(None).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    return path


def run_g11_track(
    *,
    execution_root: Path | str,
    run_id: str,
    api_key: str | None = None,
    code_root: Path | str | None = None,
    client_factory: Any = None,
    production_module_loader: Callable[..., Any] | None = None,
    max_chapters: int | None = None,
    clock: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Advance one G11 track through the real engine, stopping on any failure."""
    execution = Path(execution_root).resolve()
    if not execution.is_dir():
        raise G11RunnerError(f"G11 execution root is missing: {execution}")
    track = TRACK_BY_ID.get(run_id)
    if track is None:
        raise G11RunnerError(f"unknown G11 run id: {run_id!r}")
    code = Path(code_root).resolve() if code_root is not None else Path(__file__).resolve().parent
    resolve_manifest_track(execution, run_id)
    root = track_root(execution, run_id)
    _assert_no_symlinks(root)

    report: dict[str, Any] = {
        "run_id": run_id,
        "model_id": track["model_id"],
        "novel_id": track["novel_id"],
        "track_root": str(root),
        "provider_calls": 0,
    }

    existing_quarantine = load_quarantine(root)
    classification = classify_track_progress(
        read_track_progress(root),
        quarantine=existing_quarantine,
        expected_run_id=run_id,
    )
    reopen_decision: dict[str, Any] | None = None
    if classification["status"] == "quarantined":
        reopen_decision = classify_quarantine_reopen(
            root, existing_quarantine, expected_run_id=run_id
        )
        if not reopen_decision.get("reopenable"):
            return report | {
                "status": "QUARANTINED_STOPPED",
                "quarantine": existing_quarantine,
                "quarantine_reopen": reopen_decision,
                "last_completed_chapter": classification.get("last_completed"),
                "last_audited_chapter": classification.get("last_audited_chapter"),
            }
    if classification["status"] == "corrupt_block":
        return report | {"status": "CORRUPT_STOPPED", "reason": classification["reason"]}
    if classification["status"] == "complete":
        return report | {
            "status": "ALREADY_COMPLETE",
            "last_completed_chapter": classification["last_completed"],
            "last_audited_chapter": classification["last_audited_chapter"],
        }

    if not isinstance(api_key, str) or not api_key.strip():
        return report | {
            "status": "AUTHORIZATION_REQUIRED",
            "reason": "G11 execution requires an explicitly injected API key",
            "last_completed_chapter": classification["last_completed"],
            "last_audited_chapter": classification["last_audited_chapter"],
        }

    # Reopen only after authorization is confirmed: archive the old marker as
    # evidence, then remove the active marker so the track can resume.
    reopened_quarantine: dict[str, Any] | None = None
    if reopen_decision is not None and reopen_decision.get("reopenable"):
        archived_marker = archive_quarantine_marker(
            track_root_path=root,
            quarantine=existing_quarantine,
            clock=clock,
        )
        reopened_quarantine = {
            "blocked_chapter": reopen_decision.get("blocked_chapter"),
            "reason": reopen_decision.get("reason"),
            "archived_marker": str(archived_marker),
        }

    ledger = open_model_ledger(execution, track["model_id"], clock=clock)
    provider_client = create_provider_client(
        api_key=api_key,
        client_factory=client_factory,
    )
    loader = production_module_loader or default_production_module_loader
    runtime_state: Mapping[str, Any] | None = None
    loaded: Mapping[str, Any] | None = None
    invoked_chapters: list[int] = []
    checkpoint_records: list[dict[str, Any]] = []
    recovery_records: list[dict[str, Any]] = []
    total_recovery_cycles = 0

    try:
        with production_run_lock(root):
            with _working_directory(root):
                loaded = loader(code_root=code, track_root_path=root)
            if not isinstance(loaded, Mapping) or "module" not in loaded:
                raise G11RunnerError("G11 production module loader returned no module")
            module = loaded["module"]
            runtime_state = install_g11_paid_runtime(
                module,
                model_id=track["model_id"],
                provider_client=provider_client,
                ledger=ledger,
                run_id=run_id,
                novel_id=track["novel_id"],
                clock=clock,
                sleep=sleep,
                deepseek_low_mode_path=deepseek_low_mode_path(root),
            )
            runner = getattr(module, G11_LIFECYCLE_ENTRYPOINT, None)
            if not callable(runner):
                raise G11RunnerError(
                    f"real production module lacks {G11_LIFECYCLE_ENTRYPOINT}"
                )
            limit = (
                CHAPTER_COUNT_PER_TRACK if max_chapters is None else int(max_chapters)
            )
            while len(invoked_chapters) < limit:
                progress = read_track_progress(root)
                state_now = classify_track_progress(
                    progress,
                    quarantine=load_quarantine(root),
                    expected_run_id=run_id,
                )
                if state_now["status"] == "complete":
                    break
                if state_now["status"] != "ready_for_target":
                    report["stop_classification"] = state_now
                    break
                target = state_now["target_chapter"]
                before_chapter = progress.get("last_completed")
                counters_before = _counter_snapshot(runtime_state)
                recovery_cycles = 0

                def invoke_lifecycle_once(*, recovery: bool = False) -> BaseException | None:
                    context = (runtime_state or {}).get("recovery_context")
                    if recovery and isinstance(context, dict):
                        context.update(
                            active=True, chapter=target, cycle=recovery_cycles
                        )
                    try:
                        with _working_directory(root):
                            runner(target_chapter=target)
                    except SystemExit as exc:
                        return exc
                    finally:
                        if recovery and isinstance(context, dict):
                            context.update(active=False, chapter=None, cycle=0)
                    return None

                def archive_recovery_cycle(cycle_reason: str) -> None:
                    nonlocal recovery_cycles, total_recovery_cycles
                    recovery_cycles += 1
                    total_recovery_cycles += 1
                    if track["model_id"] == DEEPSEEK_MODEL_ID:
                        _note_deepseek_recovery_saturation(
                            track_root_path=root,
                            run_id=run_id,
                            model_id=track["model_id"],
                            chapter=target,
                            runtime_state=runtime_state,
                            clock=clock,
                        )
                    recovery_records.append(
                        archive_chapter_recovery_evidence(
                            track_root_path=root,
                            run_id=run_id,
                            model_id=track["model_id"],
                            chapter=target,
                            cycle=recovery_cycles,
                            reason=cycle_reason,
                            clock=clock,
                        )
                    )

                # A reopened (or crashed) track can arrive with uncommitted
                # evidence already on disk.  Archive it before the fresh attempt
                # so the engine never reuses a rejected pending stage or collides
                # with a stale paid-request journal slot.  Unresolved
                # (started/uncertain) journals are never archived or resent.
                pre_existing = _chapter_has_uncommitted_translation_evidence(
                    root, target
                ) and _chapter_journals_resolved(root, target)
                if pre_existing:
                    archive_recovery_cycle("pre_existing_content_stop")

                engine_error = invoke_lifecycle_once(recovery=recovery_cycles > 0)
                after = read_track_progress(root)
                while (
                    after.get("last_completed") != target
                    and recovery_cycles < G11_CONTENT_RECOVERY_MAX_CYCLES
                    and after.get("last_completed") == (before_chapter or 0)
                    and _chapter_has_uncommitted_translation_evidence(root, target)
                    and _chapter_journals_resolved(root, target)
                ):
                    recovery_reason = (
                        f"{type(engine_error).__name__}: {engine_error}"
                        if engine_error is not None
                        else "engine_returned_without_commit"
                    )
                    archive_recovery_cycle(recovery_reason)
                    engine_error = invoke_lifecycle_once(recovery=True)
                    after = read_track_progress(root)

                if after.get("last_completed") != target:
                    if recovery_cycles > 0:
                        raise G11ContentRecoveryExhausted(
                            f"content recovery exhausted for chapter {target} after "
                            f"{recovery_cycles} cycle(s)"
                        )
                    if engine_error is not None:
                        raise engine_error
                    raise G11RunnerError(
                        "the real lifecycle did not finish exactly the expected "
                        f"chapter: last_completed={after.get('last_completed')!r}, "
                        f"target={target!r}"
                    )
                if after.get("last_completed") != (before_chapter or 0) + 1:
                    raise G11RunnerError(
                        "the real lifecycle advanced by more than one chapter"
                    )
                invoked_chapters.append(target)
                counters_after = _counter_snapshot(runtime_state)
                runtime_reservations = list(
                    ((runtime_state or {}).get("counters", {}) or {}).get(
                        "reservations", []
                    )
                )
                _write_invocation_ledger(
                    track_root_path=root,
                    run_id=run_id,
                    model_id=track["model_id"],
                    chapter=target,
                    before=counters_before,
                    after=counters_after,
                    reservations=runtime_reservations[counters_before["reservations"]:],
                )
                if (
                    target in QUALITY_CHECKPOINTS
                    or target in STATE_EVIDENCE_CHECKPOINTS
                ):
                    checkpoint_records.append(
                        freeze_checkpoints_for_chapter(
                            execution_root=execution,
                            run_id=run_id,
                            chapter=target,
                            clock=clock,
                        )
                    )
    except KeyboardInterrupt:
        return report | {
            "status": "INTERRUPTED",
            "chapters_committed": invoked_chapters,
            "last_completed_chapter": read_track_progress(root).get("last_completed"),
            "last_audited_chapter": read_track_progress(root).get(
                "last_audited_chapter"
            ),
            "ledger": ledger.summary(),
            "quarantined": False,
            "resume_supported": True,
            "content_recovery_cycles": total_recovery_cycles,
            "content_recovery_records": recovery_records,
            "deepseek_adaptive_low_mode": track_adaptive_low_summary(root),
            "quarantine_reopened": reopened_quarantine,
        }
    except G11AmbiguousOutcome as exc:
        marker = write_quarantine(
            track_root_path=root,
            run_id=run_id,
            model_id=track["model_id"],
            novel_id=track["novel_id"],
            first_blocked_chapter=exc.chapter,
            outcome="ambiguous_remote_outcome",
            reason=str(exc),
            clock=clock,
        )
        return report | {
            "status": "AMBIGUOUS_STOPPED",
            "quarantine": marker,
            "first_blocked_chapter": exc.chapter,
            "chapters_committed": invoked_chapters,
            "ledger": ledger.summary(),
            "provider_calls": int(
                ((runtime_state or {}).get("counters", {}) or {}).get(
                    "transport_attempts", 0
                )
            ),
            "other_tracks_unaffected": True,
            "content_recovery_cycles": total_recovery_cycles,
            "content_recovery_records": recovery_records,
            "deepseek_adaptive_low_mode": track_adaptive_low_summary(root),
            "quarantine_reopened": reopened_quarantine,
        }
    except G11BudgetError as exc:
        return report | {
            "status": "BUDGET_STOPPED",
            "reason": str(exc),
            "chapters_committed": invoked_chapters,
            "ledger": ledger.summary(),
            "other_tracks_unaffected": True,
            "content_recovery_cycles": total_recovery_cycles,
            "content_recovery_records": recovery_records,
            "deepseek_adaptive_low_mode": track_adaptive_low_summary(root),
            "quarantine_reopened": reopened_quarantine,
        }
    except BaseException as exc:  # noqa: BLE001 - terminal by contract
        try:
            blocked = read_track_progress(root).get("next_chapter")
        except G11RunnerError:
            blocked = FIRST_CHAPTER
        marker = None
        if not quarantine_path(root).exists():
            marker = write_quarantine(
                track_root_path=root,
                run_id=run_id,
                model_id=track["model_id"],
                novel_id=track["novel_id"],
                first_blocked_chapter=int(blocked or FIRST_CHAPTER),
                outcome="terminal_failure",
                reason=f"{type(exc).__name__}: {exc}",
                clock=clock,
            )
        return report | {
            "status": "TERMINAL_FAILURE",
            "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
            "quarantine": marker,
            "chapters_committed": invoked_chapters,
            "ledger": ledger.summary(),
            "provider_calls": int(
                ((runtime_state or {}).get("counters", {}) or {}).get(
                    "transport_attempts", 0
                )
            ),
            "other_tracks_unaffected": True,
            "content_recovery_cycles": total_recovery_cycles,
            "content_recovery_records": recovery_records,
            "deepseek_adaptive_low_mode": track_adaptive_low_summary(root),
            "quarantine_reopened": reopened_quarantine,
        }
    finally:
        if loaded is not None and loaded.get("module_name"):
            sys.modules.pop(loaded["module_name"], None)

    final_progress = read_track_progress(root)
    counters = dict((runtime_state or {}).get("counters", {}))
    final_classification = classify_track_progress(final_progress)
    return report | {
        "status": (
            "COMPLETED"
            if final_classification["status"] == "complete"
            else "STOPPED_BEFORE_COMPLETION"
        ),
        "chapters_committed": invoked_chapters,
        "last_completed_chapter": final_progress.get("last_completed"),
        "last_audited_chapter": final_progress.get("last_audited_chapter"),
        "transport_attempts": int(counters.get("transport_attempts", 0)),
        "retries": int(counters.get("retries", 0)),
        "translation_regenerations": int(counters.get("regenerations", 0)),
        "replayed_subrequests": int(counters.get("replays", 0)),
        "provider_calls": int(counters.get("transport_attempts", 0)),
        "checkpoints_frozen": [
            record.get("path") for record in checkpoint_records if record.get("frozen")
        ],
        "ledger": ledger.summary(),
        "quarantined": False,
        "automatic_chapter_advance_within_track": True,
        "synthetic_audits_inserted": False,
        "content_recovery_cycles": total_recovery_cycles,
        "content_recovery_records": recovery_records,
        "deepseek_adaptive_low_mode": track_adaptive_low_summary(root),
        "quarantine_reopened": reopened_quarantine,
    }


def run_g11_unattended(
    *,
    execution_root: Path | str,
    api_key: str | None = None,
    code_root: Path | str | None = None,
    tracks: tuple[str, ...] | None = None,
    max_chapters: int | None = None,
    client_factory: Any = None,
    production_module_loader: Callable[..., Any] | None = None,
    clock: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the four independent G11 tracks, continuing past a failed track."""
    execution = Path(execution_root).resolve()
    manifest = _read_manifest(execution)
    if manifest.get("provider_calls") != 0:
        raise G11RunnerError("G11 manifest reports provider calls before execution")
    selected = (
        tuple(track["run_id"] for track in TRACKS)
        if tracks is None
        else tuple(tracks)
    )
    unknown = [run_id for run_id in selected if run_id not in TRACK_BY_ID]
    if unknown:
        raise G11RunnerError(f"unknown G11 run ids: {unknown}")

    reports: dict[str, Any] = {}
    for run_id in selected:
        reports[run_id] = run_g11_track(
            execution_root=execution,
            run_id=run_id,
            api_key=api_key,
            code_root=code_root,
            client_factory=client_factory,
            production_module_loader=production_module_loader,
            max_chapters=max_chapters,
            clock=clock,
            sleep=sleep,
        )
    return {
        "benchmark_id": G11_BENCHMARK_ID,
        "revision": G11_REVISION,
        "execution_root": str(execution),
        "tracks": reports,
        "provider_calls": sum(
            int(report.get("provider_calls", 0)) for report in reports.values()
        ),
    }


def _finalize_audit_recovery(
    *,
    execution: Path,
    run_id: str,
    root: Path,
    quarantine: Mapping[str, Any] | None,
    clock: Any,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze Chapter-100 evidence, then archive/remove the active quarantine."""
    evidence = g11_evidence_checkpoint_path(execution, run_id, LAST_CHAPTER)
    frozen: dict[str, Any] = {"frozen": False, "reason": "already_frozen"}
    if not evidence.exists():
        frozen = freeze_checkpoints_for_chapter(
            execution_root=execution,
            run_id=run_id,
            chapter=LAST_CHAPTER,
            clock=clock,
        )
    if not evidence.exists():
        raise G11RunnerError("G11 Chapter 100 evidence checkpoint was not frozen")
    archived_quarantine: str | None = None
    if quarantine_path(root).is_file():
        marker = (
            quarantine
            if isinstance(quarantine, Mapping)
            else _read_object(quarantine_path(root), "G11 quarantine")
        )
        archived_quarantine = str(
            archive_quarantine_marker(
                track_root_path=root, quarantine=marker, clock=clock
            )
        )
    progress = read_track_progress(root)
    return dict(report) | {
        "status": "AUDIT_RECOVERED",
        "last_completed_chapter": progress.get("last_completed"),
        "next_chapter": progress.get("next_chapter"),
        "last_audited_chapter": progress.get("last_audited_chapter"),
        "checkpoint_100_evidence": str(evidence),
        "checkpoint_frozen": frozen,
        "quarantine_removed": True,
        "archived_quarantine": archived_quarantine,
        "chapter_retranslation": False,
    }


def recover_g11_pending_audit(
    *,
    execution_root: Path | str,
    run_id: str,
    api_key: str | None = None,
    code_root: Path | str | None = None,
    client_factory: Any = None,
    production_module_loader: Callable[..., Any] | None = None,
    clock: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Recover *only* the unfinished final periodic glossary audit of one track.

    This never retranslates a chapter, resets a track, resets the GLM ledger, or
    regenerates Chapters 1-100.  It reuses the already-validated audit checkpoint
    (completed chunks and their evidence preserved; only the failed leaf chunk is
    reset after its rejected attempt/journal evidence is archived byte-for-byte)
    and then lets the real production engine re-run the pending Chapters 76-100
    audit.  It is restart-safe: an already-committed audit or an already-frozen
    Chapter-100 checkpoint performs no provider call and no duplicate freeze, an
    unresolved remote outcome fails closed, and a deterministic failed audit
    content is bounded to two fresh recovery cycles.
    """
    execution = Path(execution_root).resolve()
    if not execution.is_dir():
        raise G11RunnerError(f"G11 execution root is missing: {execution}")
    track = TRACK_BY_ID.get(run_id)
    if track is None:
        raise G11RunnerError(f"unknown G11 run id: {run_id!r}")
    code = (
        Path(code_root).resolve()
        if code_root is not None
        else Path(__file__).resolve().parent
    )
    resolve_manifest_track(execution, run_id)
    root = track_root(execution, run_id)
    _assert_no_symlinks(root)

    report: dict[str, Any] = {
        "run_id": run_id,
        "model_id": track["model_id"],
        "novel_id": track["novel_id"],
        "track_root": str(root),
        "provider_calls": 0,
        "chapter_retranslation": False,
    }

    quarantine = load_quarantine(root)
    if quarantine is None:
        return report | {"status": "NOT_QUARANTINED"}

    progress = read_track_progress(root)
    if progress.get("last_audited_chapter") == LAST_CHAPTER:
        return _finalize_audit_recovery(
            execution=execution,
            run_id=run_id,
            root=root,
            quarantine=quarantine,
            clock=clock,
            report=report,
        )

    evidence_exists = g11_evidence_checkpoint_path(
        execution, run_id, LAST_CHAPTER
    ).exists()
    decision = classify_audit_content_recovery(
        root,
        quarantine,
        expected_run_id=run_id,
        evidence_checkpoint_exists=evidence_exists,
    )
    if not decision.get("recoverable"):
        return report | {
            "status": "AUDIT_RECOVERY_NOT_APPLICABLE",
            "decision": decision,
            "quarantine": quarantine,
            "last_completed_chapter": progress.get("last_completed"),
            "last_audited_chapter": progress.get("last_audited_chapter"),
        }

    start_chapter = int(decision["start_chapter"])
    end_chapter = int(decision["end_chapter"])
    used = count_audit_content_recovery_cycles(root, start_chapter, end_chapter)
    if used >= G11_AUDIT_CONTENT_RECOVERY_MAX_CYCLES:
        return report | {
            "status": "AUDIT_RECOVERY_EXHAUSTED",
            "cycles_used": used,
            "max_cycles": G11_AUDIT_CONTENT_RECOVERY_MAX_CYCLES,
            "quarantine": quarantine,
        }

    if not isinstance(api_key, str) or not api_key.strip():
        return report | {
            "status": "AUTHORIZATION_REQUIRED",
            "reason": "G11 audit recovery requires an explicitly injected API key",
            "last_completed_chapter": progress.get("last_completed"),
            "last_audited_chapter": progress.get("last_audited_chapter"),
        }

    cycle = used + 1
    archive = archive_audit_content_evidence(
        track_root_path=root,
        run_id=run_id,
        model_id=track["model_id"],
        novel_id=track["novel_id"],
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        cycle=cycle,
        reason=str(quarantine.get("reason")),
        clock=clock,
    )
    checkpoint_path = g11_audit_checkpoint_path(root, start_chapter, end_chapter)
    checkpoint = _read_object(checkpoint_path, "G11 audit checkpoint")
    reset = reset_failed_audit_chunks(checkpoint)
    atomic_write_json(checkpoint_path, checkpoint)
    manifest = archive["manifest"]
    manifest["reset"] = reset
    atomic_write_json(Path(archive["manifest_path"]), manifest)

    ledger = open_model_ledger(execution, track["model_id"], clock=clock)
    provider_client = create_provider_client(
        api_key=api_key,
        client_factory=client_factory,
    )
    loader = production_module_loader or default_production_module_loader
    runtime_state: Mapping[str, Any] | None = None
    loaded: Mapping[str, Any] | None = None
    try:
        with production_run_lock(root):
            with _working_directory(root):
                loaded = loader(code_root=code, track_root_path=root)
            if not isinstance(loaded, Mapping) or "module" not in loaded:
                raise G11RunnerError("G11 production module loader returned no module")
            module = loaded["module"]
            runtime_state = install_g11_paid_runtime(
                module,
                model_id=track["model_id"],
                provider_client=provider_client,
                ledger=ledger,
                run_id=run_id,
                novel_id=track["novel_id"],
                clock=clock,
                sleep=sleep,
                deepseek_low_mode_path=deepseek_low_mode_path(root),
            )
            audit_runner = getattr(module, "run_periodic_audit", None)
            if not callable(audit_runner):
                raise G11RunnerError(
                    "real production module lacks run_periodic_audit"
                )
            with _working_directory(root):
                audit_runner(start_chapter, end_chapter)
    except G11AmbiguousOutcome as exc:
        return report | {
            "status": "AUDIT_RECOVERY_AMBIGUOUS",
            "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
            "quarantine": quarantine,
            "cycles_used": cycle,
            "archived_evidence": archive["directory"],
        }
    except BaseException as exc:  # noqa: BLE001 - fail closed, keep quarantine
        return report | {
            "status": "AUDIT_RECOVERY_FAILED",
            "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
            "quarantine": quarantine,
            "cycles_used": cycle,
            "archived_evidence": archive["directory"],
            "reset": reset,
        }
    finally:
        if loaded is not None and loaded.get("module_name"):
            sys.modules.pop(loaded["module_name"], None)

    after = read_track_progress(root)
    if (
        after.get("last_completed") != LAST_CHAPTER
        or after.get("next_chapter") != LAST_CHAPTER + 1
        or after.get("last_audited_chapter") != LAST_CHAPTER
    ):
        return report | {
            "status": "AUDIT_RECOVERY_INCOMPLETE",
            "last_completed_chapter": after.get("last_completed"),
            "next_chapter": after.get("next_chapter"),
            "last_audited_chapter": after.get("last_audited_chapter"),
            "cycles_used": cycle,
            "quarantine": quarantine,
        }

    provider_calls = int(
        ((runtime_state or {}).get("counters", {}) or {}).get("transport_attempts", 0)
    )
    finalized = _finalize_audit_recovery(
        execution=execution,
        run_id=run_id,
        root=root,
        quarantine=quarantine,
        clock=clock,
        report=report,
    )
    return finalized | {
        "audit_window": {
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
        },
        "cycles_used": cycle,
        "archived_evidence": archive["directory"],
        "reset": reset,
        "provider_calls": provider_calls,
        "ledger": ledger.summary(),
    }


# ===========================================================================
# Status + verification (read-only, zero provider)
# ===========================================================================


def status_g11(*, execution_root: Path | str) -> dict[str, Any]:
    """Report durable G11 state without mutating anything or contacting anyone."""
    execution = Path(execution_root).resolve()
    result: dict[str, Any] = {
        "benchmark_id": G11_BENCHMARK_ID,
        "revision": G11_REVISION,
        "execution_root": str(execution),
        "manifest": None,
        "tracks": [],
        "provider_calls": 0,
    }
    if not (execution / G11_EXECUTION_MANIFEST_FILENAME).is_file():
        return result | {"status": "NOT_PREPARED"}
    manifest = _read_manifest(execution)
    result["manifest"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "pristine_root": manifest.get("pristine_root"),
        "track_count": manifest.get("track_count"),
        "chapters_per_track": manifest.get("chapters_per_track"),
        "model_budgets": manifest.get("model_budgets"),
    }
    for track in TRACKS:
        root = track_root(execution, track["run_id"])
        if not root.is_dir():
            result["tracks"].append(
                {"run_id": track["run_id"], "status": "MISSING_ROOT"}
            )
            continue
        quarantine = load_quarantine(root)
        classification = classify_track_progress(
            read_track_progress(root),
            quarantine=quarantine,
            expected_run_id=track["run_id"],
        )
        journals = inspect_paid_request_journals(
            root / "data" / "provider_request_journal"
        )
        ledger_file = ledger_path(execution, track["model_id"])
        ledger_summary = None
        if ledger_file.is_file():
            ledger_summary = G11Ledger.open(
                path=ledger_file, model_id=track["model_id"]
            ).summary()
        evidence = execution / G11_EVIDENCE_DIRECTORY / track["run_id"]
        frozen_checkpoints = (
            sorted(
                int(item.name)
                for item in evidence.iterdir()
                if item.is_dir() and item.name.isdigit()
            )
            if evidence.is_dir()
            else []
        )
        reopen_decision = (
            classify_quarantine_reopen(
                root, quarantine, expected_run_id=track["run_id"]
            )
            if quarantine is not None
            else None
        )
        result["tracks"].append(
            {
                "run_id": track["run_id"],
                "model_id": track["model_id"],
                "novel_id": track["novel_id"],
                "status": classification["status"],
                "classification": classification,
                "quarantine": quarantine,
                "quarantine_reopen": reopen_decision,
                "last_completed_chapter": classification.get("last_completed"),
                "last_audited_chapter": classification.get("last_audited_chapter"),
                "journals": {
                    "records": len(journals.get("records", [])),
                    "uncertain": len(journals.get("uncertain", [])),
                    "errors": list(journals.get("errors", [])),
                },
                "ledger": ledger_summary,
                "frozen_checkpoints": frozen_checkpoints,
                "deepseek_low_translation_mode": track_adaptive_low_summary(root),
                "content_recovery_cycles": count_content_recovery_cycles(root),
            }
        )
    result["status"] = "PREPARED"
    return result


def verify_g11(
    *,
    execution_root: Path | str,
    pristine_root: Path | str | None = None,
    production_root: Path | str | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Prove the G11 execution layout is frozen, isolated, and source-bound."""
    execution = Path(execution_root).resolve()
    code_root = Path(__file__).resolve().parent
    assert_source_bindings(code_root)
    extra_forbidden: list[Any] = [*forbidden_roots]
    if pristine_root is not None:
        extra_forbidden.append(Path(pristine_root).expanduser().resolve())
    if production_root is not None:
        extra_forbidden.append(Path(production_root).expanduser().resolve())
    if _is_within(execution, code_root) or _is_within(code_root, execution):
        raise G11RunnerError("G11 execution root must be external to the code worktree")
    if extra_forbidden:
        assert_fresh_execution_root(
            execution, forbidden_roots=tuple(extra_forbidden)
        )
    if not execution.is_dir():
        raise G11RunnerError(f"G11 execution root is missing: {execution}")
    _assert_no_symlinks(execution)

    manifest = _read_manifest(execution)
    if manifest.get("track_count") != len(TRACKS):
        raise G11RunnerError("G11 manifest track count drifted")
    if manifest.get("chapters_per_track") != CHAPTER_COUNT_PER_TRACK:
        raise G11RunnerError("G11 manifest chapter scope drifted")
    if list(manifest.get("audit_checkpoints", [])) != list(AUDIT_CHECKPOINTS):
        raise G11RunnerError("G11 manifest audit checkpoints drifted")
    if list(manifest.get("quality_checkpoints", [])) != list(QUALITY_CHECKPOINTS):
        raise G11RunnerError("G11 manifest quality checkpoints drifted")
    if manifest.get("model_budgets") != dict(MODEL_BUDGETS):
        raise G11RunnerError("G11 manifest budgets drifted")

    for track in TRACKS:
        resolve_manifest_track(execution, track["run_id"])
        root = track_root(execution, track["run_id"])
        config = load_novel_config(root / "novel.json")
        if not config.residual_cjk_micro_repair.enabled:
            raise G11RunnerError(
                f"G11 experiment copy {track['run_id']} has residual repair disabled"
            )
        identity = _read_object(
            root / G11_RUN_IDENTITY_FILENAME, "G11 run identity"
        )
        for field, expected in (
            ("translation_effort", "high"),
            ("semantic_effort", "high"),
            ("structured_effort", "low"),
        ):
            if identity.get(field) != expected:
                raise G11RunnerError(f"G11 run identity effort drifted for {field}")

        ledger_file = ledger_path(execution, track["model_id"])
        if ledger_file.is_file():
            G11Ledger.open(path=ledger_file, model_id=track["model_id"])

    pristine_proof = None
    if pristine_root is not None:
        pristine = Path(pristine_root).expanduser().resolve()
        observed = _tree_fingerprint(pristine)
        if manifest.get("pristine_root") is not None and (
            Path(manifest["pristine_root"]).resolve() == pristine
        ):
            if observed != manifest.get("pristine_tree_sha256"):
                raise G11RunnerError(
                    "the pristine S0 template changed after G11 preparation"
                )
            pristine_proof = {
                "pristine_root": str(pristine),
                "unchanged": True,
                "tree_sha256": observed,
            }

    return {
        "benchmark_id": G11_BENCHMARK_ID,
        "revision": G11_REVISION,
        "execution_root": str(execution),
        "manifest_sha256": manifest["manifest_sha256"],
        "track_count": len(TRACKS),
        "chapters_per_track": CHAPTER_COUNT_PER_TRACK,
        "frozen_layout_verified": True,
        "residual_repair_enabled_in_all_copies": True,
        "pristine_proof": pristine_proof,
        "provider_calls": 0,
    }


__all__ = [
    "G11AmbiguousOutcome",
    "G11BudgetError",
    "G11ContentRecoveryExhausted",
    "G11DeterministicTransportFailure",
    "G11Ledger",
    "G11RunnerError",
    "G11TransportExhausted",
    "G11_AUDIT_CONTENT_RECOVERY_MAX_CYCLES",
    "G11_CONTENT_RECOVERY_MAX_CYCLES",
    "G11_DEFAULT_PRISTINE_ROOT",
    "G11_EVIDENCE_DIRECTORY",
    "G11_EXECUTION_MANIFEST_FILENAME",
    "G11_LEDGER_DIRECTORY",
    "G11_LIFECYCLE_ENTRYPOINT",
    "G11_PRODUCTION_SCRIPT_NAME",
    "G11_RECOVERY_TRANSLATION_INSTRUCTION",
    "G11_RUNNER_REVISION",
    "G11_RUN_IDENTITY_FILENAME",
    "G11_QUARANTINE_FILENAME",
    "activate_deepseek_low_mode",
    "archive_audit_content_evidence",
    "archive_chapter_recovery_evidence",
    "archive_quarantine_marker",
    "audit_content_recovery_directory",
    "chapter_journal_paths",
    "chapter_pending_paths",
    "classify_audit_content_recovery",
    "classify_quarantine_reopen",
    "classify_track_progress",
    "classify_transport_failure",
    "content_recovery_directory",
    "count_audit_content_recovery_cycles",
    "count_content_recovery_cycles",
    "deepseek_low_mode_path",
    "deepseek_low_mode_summary",
    "default_production_module_loader",
    "freeze_checkpoints_for_chapter",
    "g11_account_usage",
    "g11_audit_checkpoint_path",
    "g11_evidence_checkpoint_path",
    "g11_pricing_snapshot",
    "g11_preregistration_identity",
    "install_g11_paid_runtime",
    "is_content_stop_reason",
    "ledger_path",
    "load_deepseek_low_mode",
    "load_quarantine",
    "normalize_g11_request",
    "open_model_ledger",
    "prepare_g11",
    "quarantine_path",
    "read_track_progress",
    "recover_g11_pending_audit",
    "regeneration_reason",
    "required_last_audited_chapter",
    "reset_failed_audit_chunks",
    "resolve_manifest_track",
    "run_g11_track",
    "run_g11_unattended",
    "status_g11",
    "track_adaptive_low_summary",
    "track_root",
    "verify_g11",
    "write_quarantine",
]
