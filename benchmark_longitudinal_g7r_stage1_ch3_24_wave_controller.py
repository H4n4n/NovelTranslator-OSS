"""G7R dynamic Stage-1 Chapters 3-24 wave controller.

G7R is an additive, reusable orchestration layer on top of the frozen G7C/G7D/
G7E/G7F/G7G/G7J/G7P Stage-1 machinery.  It answers three operator questions with
one deterministic, machine-readable JSON document per invocation:

``status``
    Zero-Provider, read-only.  Derive the current cohort disposition for every
    one of the twelve real Stage-1 runs from durable evidence (progress,
    production paid-request journals, G7J/G7L response classifications) and
    report the dynamically derived active set for a target chapter.

``plan``
    Zero-Provider.  For one valid target chapter (3..24) materialize exactly one
    frozen G7F/G7E invocation plan per eligible active run, re-inspect each plan
    from disk, recompute its raw and semantic SHA plus approval token, and emit
    one wave bundle outside ``Stage1Root`` whose status is
    ``READY_FOR_OPERATOR_AUTHORIZATION``.

``execute``
    Paid-capable.  Requires the explicit ``--authorize-paid-wave`` flag, the
    exact bundle SHA, and a bundle whose stored repository/policy bindings still
    hold.  Every active-run zero-Provider G7G preflight runs before the first
    credential access; only then does the controller read the DPAPI secret and
    drive the real G7F executor for exactly one target chapter.

Hard invariants enforced here:

* one wave executes exactly ONE target chapter; the next chapter is never
  executed automatically;
* ambiguous remote outcomes (``started`` / ``uncertain`` journal, corrupt
  journal, unknown durable state) quarantine the run and stop the entire wave
  immediately, with no retry and no later run started;
* a durably quarantined run (G7S marker) is never reconstructed as
  ``ready_for_target``: the marker is read-only evidence here, and this
  controller never creates, rewrites, or deletes one;
* a definitive durable model/policy failure (for example a G7J translation
  content-filter classification or a G7L/G7P empty length-terminated auxiliary
  response) is terminal for that run only and the wave continues with the next
  independent run;
* the G7F executor and the production paid-request journal own transport, retry,
  and exactly-once bookkeeping; this controller never reissues a started or
  uncertain journal and never infers success from stdout;
* importing this module performs no filesystem writes, reads no credential,
  loads no ``.env`` file, constructs no Provider client, and makes zero Provider
  calls.

The controller never authorizes paid work by itself.  Nothing here is part of
benchmark inference; the Command Code coding agent is not in the inference path.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmark_longitudinal_g7a_readiness import SOURCE_BINDINGS
from benchmark_longitudinal_g7b_auxiliary_policy import reasoning_by_operation
from benchmark_longitudinal_g7c_stage1_integration import (
    G7C_INTEGRATION_SHA256,
    RUN_IDENTITY_FILENAME,
    stage1_run_specs,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    G7D_ACCOUNTING_SHA256,
    MODEL_RATECARD,
    PRICING_VERSION,
)
from benchmark_longitudinal_g7e_paid_authorization import (
    G7E_AUTHORIZATION_SHA256,
)
from benchmark_longitudinal_g7f_stage1_paid_executor import (
    EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
    G7F_EXECUTOR_SHA256,
    G7FExecutionError,
    InvocationBudgetLedger,
    build_stage1_invocation_plan,
    execute_stage1_invocation,
    inspect_stage1_invocation_plan,
    invocation_ledger_path,
    state_fingerprint_sha256,
)
from benchmark_longitudinal_g7g_stage1_secure_launcher import (
    G7GSecureLauncherError,
    assert_safe_secret_target,
    load_platform_dpapi,
    run_preflight,
)
from benchmark_longitudinal_g7j_reasoning_budget import (
    G7J_AUXILIARY_8K_POLICY_SHA256,
    G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256,
    GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS,
    GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS,
    PRIMARY_TRANSLATION_OPERATION,
    STATUS_OK,
    classify_translation_response,
    translation_reasoning_effort_by_model,
)
from benchmark_longitudinal_g7s_stage1_durable_quarantine import (
    G7S_POLICY_SHA256,
    MARKER_STATE_INVALID,
    MARKER_STATE_VALID,
    inspect_durable_quarantine_marker,
)
from novel_project import load_novel_config
from production_lifecycle import run_due_audits
from production_paid_request_journal import inspect_paid_request_journals


G7R_SCHEMA_VERSION = 1
G7R_REVISION = "goat-g7r-stage1-dynamic-wave-controller-v3"
G7R_REPOSITORY_ANCHOR = "9deaf11107f9ef43efd56b95ba2b30f7be61f57a"

CONTROLLER_MODULE_NAME = (
    "benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller"
)
PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
STAGE1_ROOTS_MANIFEST_FILENAME = "g7c_stage1_roots_manifest.json"

TARGET_FIRST_CHAPTER = 3
BUNDLE_STATUS_READY = "READY_FOR_OPERATOR_AUTHORIZATION"

# Explicitly documented volatile bundle fields, excluded from the semantic SHA.
VOLATILE_BUNDLE_FIELDS = ("created_utc", "bundle_sha256")

_HEX_RE_LENGTH = 64
_SHA256_CHARSET = set("0123456789abcdef")

# ---------------------------------------------------------------------------
# Outcome / disposition vocabulary
# ---------------------------------------------------------------------------

DISPOSITION_HEALTHY_COMPLETED = "healthy_completed"
DISPOSITION_READY_FOR_TARGET = "ready_for_target"
DISPOSITION_TERMINAL = "terminal_durable_model_or_policy_failure"
DISPOSITION_QUARANTINED = "quarantined_ambiguous_transport"
DISPOSITION_CORRUPT = "blocked_corrupt_evidence"
DISPOSITION_NOT_READY = "not_ready_for_target"

WAVE_COMPLETED = "WAVE_COMPLETED"
WAVE_COMPLETED_WITH_TERMINAL_RUNS = "WAVE_COMPLETED_WITH_TERMINAL_RUNS"
WAVE_STOPPED_AMBIGUOUS = "WAVE_STOPPED_AMBIGUOUS_REMOTE_OUTCOME"
WAVE_STOPPED_LOCAL_FAILURE = "WAVE_STOPPED_LOCAL_FAILURE"

RUN_OUTCOME_COMPLETED = "completed"
RUN_OUTCOME_TERMINAL = "terminal_durable_model_or_policy_failure"
RUN_OUTCOME_QUARANTINED = "quarantined_ambiguous_transport"
RUN_OUTCOME_LOCAL_FAILURE = "local_or_pre_send_failure"
# Internal subtype for a paid-boundary crossing whose durable post-send state is
# corrupt or unknown.  Externally the run is quarantined: no retry, later runs
# untouched, and the wave stops fail-closed.
RUN_OUTCOME_CORRUPT_STATE = "corrupt_or_unknown_durable_state"

# External dispositions that are quarantined for accounting and remaining-set
# purposes.  ``RUN_OUTCOME_CORRUPT_STATE`` is a quarantined extra subtype.
QUARANTINED_RUN_OUTCOMES = frozenset(
    {RUN_OUTCOME_QUARANTINED, RUN_OUTCOME_CORRUPT_STATE}
)

# Per-line accounting status vocabulary.  ``exact`` and ``zero_proven_pre_send``
# lines contribute exact known totals; ``unavailable_corrupt_or_missing`` lines
# make the whole wave accounting incomplete and are never fabricated as zero.
ACCOUNTING_EXACT = "exact"
ACCOUNTING_ZERO_PROVEN_PRE_SEND = "zero_proven_pre_send"
ACCOUNTING_UNAVAILABLE = "unavailable_corrupt_or_missing"
ACCOUNTING_KNOWN_STATUSES = frozenset(
    {ACCOUNTING_EXACT, ACCOUNTING_ZERO_PROVEN_PRE_SEND}
)

# Machine-readable execute-phase labels for truthful CLI error reporting.  They
# distinguish a provably credential-free failure from one that may have reached
# the protected secret or a live Provider send.
EXECUTION_PHASE_PRE_CREDENTIAL = "pre_credential"
EXECUTION_PHASE_SECRET_ACCESS = "secret_access"
EXECUTION_PHASE_POST_SECRET_PRE_EXECUTION = "post_secret_pre_execution"
EXECUTION_PHASE_POST_SECRET_EXECUTION = "post_secret_execution"

# ---------------------------------------------------------------------------
# Envelope derivation constants (all derived, none copied from Chapter 2)
# ---------------------------------------------------------------------------

# The frozen G7F executor bounds a prompt by
# ``serialized_request_utf8_bytes + PROMPT_TOKEN_SAFETY_OVERHEAD`` (bytes treated
# as an upper bound on tokens).  G7R mirrors that shape with a pre-computed,
# conservative request-size upper bound because a wave plan must be static.
PROMPT_TOKEN_SAFETY_OVERHEAD = 8192
UTF8_BYTES_PER_CHARACTER_UPPER_BOUND = 4
PROMPT_SCHEMA_OVERHEAD_BYTES = 32768

# Frozen output ceilings: primary GOAT translation is 24,000 and every
# legitimate GOAT auxiliary subrequest is 8,000.
TRANSLATION_OUTPUT_TOKENS = GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
AUXILIARY_OUTPUT_TOKENS = (
    GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS
)

# Derived per-chapter maximum number of *new* provider sends per lifecycle
# operation for Chapters 3..24, from the closed enumeration of real production
# call sites and their bounded attempt allowances:
#   translation 1; state_extraction 1; state_json_repair 1;
#   glossary_review: the bounded reachable maximum, derived below (4), +1 conflict
#     adjudication 800 when the per-novel glossary_candidate_auto_resolution
#     opt-in is enabled;
#   periodic_audit 0 while the audit interval is not due (Chapters 3..24);
#   terminology_repair 2 (strict micro-repair: one request plus one narrowly
#     safe correction) + 2 when the per-novel residual_cjk_micro_repair opt-in
#     is enabled (initial plus one corrective).
OPERATION_SENDS_TRANSLATION = 1
OPERATION_SENDS_STATE_EXTRACTION = 1
OPERATION_SENDS_STATE_JSON_REPAIR = 1
OPERATION_SENDS_GLOSSARY_CONFLICT_ADJUDICATION = 1
OPERATION_SENDS_TERMINOLOGY_STRICT = 2
OPERATION_SENDS_RESIDUAL_CJK = 2

# Real bounded recovery semantics of one chapter glossary review.  The primary
# review and a nonempty-malformed generic JSON repair both happen before any
# bounded correction.  Two correction branches are reachable but MUTUALLY
# EXCLUSIVE, because a durable remote schema correction consumes the single
# bounded remote correction budget, leaving no semantic correction:
#   schema    1 (primary) + 1 (json repair) + 1 (remote schema correction) = 3
#   semantic  1 (primary) + 1 (json repair) + 1 (semantic correction)
#                                             + 1 (semantic retry)          = 4
# The reachable maximum is therefore 4, not 3.
GLOSSARY_REVIEW_PRIMARY_SENDS = 1
GLOSSARY_REVIEW_JSON_REPAIR_SENDS = 1
GLOSSARY_REVIEW_REMOTE_SCHEMA_CORRECTION_SENDS = 1
GLOSSARY_REVIEW_SEMANTIC_CORRECTION_SENDS = 1
GLOSSARY_REVIEW_SEMANTIC_CORRECTION_RETRY_SENDS = 1

ZERO_CEILING = "0"

BUNDLE_SCHEMA_VERSION = 1
PREFLIGHT_OK_STATUS = "PREFLIGHT_OK"


class G7RControllerError(RuntimeError):
    """The Stage-1 wave controller refused an unsafe or invalid operation."""


class G7RChapterBoundsError(G7RControllerError):
    """The requested target chapter is outside the supported wave range."""


class G7RBundleError(G7RControllerError):
    """The wave bundle is missing, tampered, stale, or inconsistent."""


class G7RPreflightError(G7RControllerError):
    """One or more zero-Provider preflights failed before any credential use."""


class G7RExecutionPhaseError(G7RControllerError):
    """A paid-phase failure carrying truthful credential/Provider metadata.

    Unlike a pre-boundary controller error, a failure raised here may occur after
    the protected secret was unprotected and after a Provider send may have
    started, so the CLI must never guess ``provider_calls == 0`` or
    ``credential_access == false``.

    * ``phase`` is a stable machine-readable stage label;
    * ``credential_access_attempted`` is True once a secret read/unprotect was
      attempted;
    * ``credential_access`` is True only once the secret was successfully
      unprotected;
    * ``provider_calls`` is an exact int only when the count is provable,
      otherwise ``None`` (unknown, never a fabricated zero).
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        credential_access: bool,
        credential_access_attempted: bool,
        provider_calls: int | None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.credential_access = credential_access
        self.credential_access_attempted = credential_access_attempted
        self.provider_calls = provider_calls


# ---------------------------------------------------------------------------
# Canonical JSON / hashing helpers
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7RControllerError(
            "G7R value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path | str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7RControllerError(f"cannot read {label}: {path}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise G7RControllerError(f"{label} must contain a JSON object: {path}")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_RE_LENGTH
        and set(value) <= _SHA256_CHARSET
    )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Target chapter bounds (derived from the frozen audit cadence)
# ---------------------------------------------------------------------------


def _stage1_audit_interval() -> int:
    intervals = {int(spec["audit_interval"]) for spec in stage1_run_specs()}
    if len(intervals) != 1:
        raise G7RControllerError(
            "Stage-1 runs disagree on the audit interval; the wave bound "
            "cannot be derived"
        )
    return intervals.pop()


def _first_audit_chapter() -> int:
    """Derive the first chapter whose processing triggers a periodic audit.

    Proven by running the real ``production_lifecycle.run_due_audits`` cadence
    with a zero ``last_audited_chapter``, so the bound is never asserted.
    """
    interval = _stage1_audit_interval()
    for chapter in range(1, interval + 2):
        if periodic_audit_due(
            chapter, last_audited_chapter=0, audit_interval=interval
        ):
            return chapter
    raise G7RControllerError(
        "could not derive the first audit-reachable Stage-1 chapter"
    )


def target_chapter_bounds() -> tuple[int, int]:
    """Return the inclusive valid wave-target chapter range.

    The upper bound is derived, not hardcoded: the first chapter whose
    processing runs a periodic audit is excluded from wave targets, so the last
    valid target is that chapter minus one.  For the frozen cadence of 25 the
    audit first becomes reachable at Chapter 25 and Chapter 24 is the last
    audit-free wave target.
    """
    last = _first_audit_chapter() - 1
    if last < TARGET_FIRST_CHAPTER:
        raise G7RControllerError(
            "the derived audit-free chapter range is empty"
        )
    return TARGET_FIRST_CHAPTER, last


def assert_valid_target_chapter(chapter: Any) -> int:
    """Fail closed unless ``chapter`` is a supported wave target."""
    if isinstance(chapter, bool) or not isinstance(chapter, int):
        raise G7RChapterBoundsError("target chapter must be an integer")
    first, last = target_chapter_bounds()
    if chapter < first:
        raise G7RChapterBoundsError(
            f"target chapter {chapter} is below the supported wave range "
            f"{first}..{last}"
        )
    if chapter > last:
        if chapter >= _first_audit_chapter():
            raise G7RChapterBoundsError(
                f"target chapter {chapter} is not supported: a periodic audit "
                f"becomes reachable at Chapter {_first_audit_chapter()}, so the "
                f"wave controller refuses every chapter above {last}"
            )
        raise G7RChapterBoundsError(
            f"target chapter {chapter} is above the supported wave range "
            f"{first}..{last}"
        )
    return chapter


def audits_due_for_chapter(
    chapter: int,
    *,
    last_audited_chapter: int,
    audit_interval: int,
) -> list[list[int]]:
    """Return the audit windows ``production_lifecycle`` would run.

    ``run_due_audits`` is invoked with a counting callback so the audit
    reachability of a target chapter is derived from the real lifecycle code
    instead of being asserted.
    """
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
        raise G7RControllerError("chapter must be a positive integer")
    if (
        isinstance(last_audited_chapter, bool)
        or not isinstance(last_audited_chapter, int)
        or last_audited_chapter < 0
    ):
        raise G7RControllerError("invalid last-audited chapter")
    if isinstance(audit_interval, bool) or not isinstance(audit_interval, int) or (
        audit_interval < 1
    ):
        raise G7RControllerError("invalid audit interval")

    progress = {
        "last_completed": chapter,
        "last_audited_chapter": last_audited_chapter,
    }
    windows: list[list[int]] = []

    def record(start_chapter: int, end_chapter: int) -> None:
        windows.append([int(start_chapter), int(end_chapter)])
        progress["last_audited_chapter"] = int(end_chapter)

    run_due_audits(
        progress=progress,
        audit_interval=audit_interval,
        run_periodic_audit=record,
    )
    return windows


def periodic_audit_due(
    chapter: int,
    *,
    last_audited_chapter: int,
    audit_interval: int,
) -> bool:
    """Return whether processing ``chapter`` would trigger a periodic audit."""
    return bool(
        audits_due_for_chapter(
            chapter,
            last_audited_chapter=last_audited_chapter,
            audit_interval=audit_interval,
        )
    )


def _audit_interval_for_spec(spec: Mapping[str, Any]) -> int:
    interval = spec.get("audit_interval")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise G7RControllerError("Stage-1 spec carries an invalid audit interval")
    binding = SOURCE_BINDINGS.get(str(spec["novel_id"]))
    if isinstance(binding, Mapping) and binding.get("audit_interval") != interval:
        raise G7RControllerError(
            "Stage-1 spec audit interval disagrees with the frozen source binding"
        )
    return interval


# ---------------------------------------------------------------------------
# Durable-evidence scanning
# ---------------------------------------------------------------------------


def _journal_files(journal_dir: Path) -> list[Path]:
    if not journal_dir.is_dir():
        return []
    return sorted(
        (item for item in journal_dir.iterdir() if item.is_file()),
        key=lambda item: item.name,
    )


def _terminal_translation_finding(
    record: Mapping[str, Any],
    name: str,
) -> dict[str, Any] | None:
    response = record.get("response")
    if not isinstance(response, Mapping):
        return None
    stats = response.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    diagnostic = response.get("diagnostic")
    maximum = None
    if isinstance(diagnostic, Mapping):
        candidate = diagnostic.get("max_completion_tokens")
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 1
        ):
            maximum = candidate
    if maximum is None:
        maximum = TRANSLATION_OUTPUT_TOKENS
    try:
        classification = classify_translation_response(
            content=response.get("content", ""),
            finish_reason=response.get("finish_reason"),
            completion_tokens=stats.get("completion_tokens"),
            max_completion_tokens=maximum,
        )
    except Exception as exc:  # noqa: BLE001 - malformed durable evidence
        return {
            "file": name,
            "operation_kind": record.get("operation_kind"),
            "chapter": record.get("chapter"),
            "failure_kind": "unclassifiable_translation_response",
            "status": None,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    if classification["status"] == STATUS_OK:
        return None
    return {
        "file": name,
        "operation_kind": record.get("operation_kind"),
        "chapter": record.get("chapter"),
        "failure_kind": "translation_response_rejected",
        "status": classification["status"],
        "detail": (
            f"finish_reason={classification['finish_reason']!r} "
            f"completion_tokens={classification['completion_tokens']!r} "
            f"max_completion_tokens={classification['max_completion_tokens']}"
        ),
    }


def _terminal_auxiliary_finding(
    record: Mapping[str, Any],
    name: str,
) -> dict[str, Any] | None:
    response = record.get("response")
    if not isinstance(response, Mapping):
        return None

    reasoning_failure = response.get("reasoning_diagnostic_failure")
    if isinstance(reasoning_failure, Mapping) and reasoning_failure:
        return {
            "file": name,
            "operation_kind": record.get("operation_kind"),
            "chapter": record.get("chapter"),
            "failure_kind": "reasoning_diagnostic_rejected",
            "status": reasoning_failure.get("classification"),
            "detail": "durable reasoning diagnostic was rejected",
        }

    content = response.get("content")
    if not isinstance(content, str):
        return {
            "file": name,
            "operation_kind": record.get("operation_kind"),
            "chapter": record.get("chapter"),
            "failure_kind": "auxiliary_response_content_missing",
            "status": None,
            "detail": "durable auxiliary response has no string content",
        }
    if content.strip():
        return None
    return {
        "file": name,
        "operation_kind": record.get("operation_kind"),
        "chapter": record.get("chapter"),
        "failure_kind": "auxiliary_empty_visible_content",
        "status": None,
        "detail": (
            f"finish_reason={response.get('finish_reason')!r} "
            "empty visible content is fail-closed for every G7L/G7P auxiliary "
            "operation"
        ),
    }


def scan_run_evidence(
    run_root: Path | str,
    *,
    target_chapter: int,
) -> dict[str, Any]:
    """Read-only durable-evidence scan for one Stage-1 run root.

    Distinguishes exactly the four safety-relevant durable situations:

    * ``errors``           - corrupt / unknown journal state;
    * ``uncertain``        - a ``started`` or ``uncertain`` journal exists, which
      blocks automatic reissue and must never be read as a model failure;
    * ``terminal_findings``- durable deterministic model/policy rejections;
    * everything else      - resumable.
    """
    root = Path(run_root)
    journal_dir = root / "data" / "provider_request_journal"

    errors: list[str] = []
    uncertain: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    terminal_findings: list[dict[str, Any]] = []

    if journal_dir.exists() and not journal_dir.is_dir():
        errors.append(f"journal path is not a directory: {journal_dir}")

    inspection = inspect_paid_request_journals(journal_dir)
    errors.extend(str(item) for item in inspection.get("errors", []))
    uncertain.extend(
        copy.deepcopy(dict(item)) for item in inspection.get("uncertain", [])
    )

    for path in _journal_files(journal_dir):
        if path.suffix != ".json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # inspect_paid_request_journals already reported this as an error.
            continue
        if not isinstance(record, Mapping):
            continue
        status = record.get("status")
        if status == "planned":
            planned.append(
                {
                    "file": path.name,
                    "operation_kind": record.get("operation_kind"),
                    "chapter": record.get("chapter"),
                }
            )
            continue
        if status != "response_recorded":
            continue
        try:
            if str(record.get("operation_kind")) == PRIMARY_TRANSLATION_OPERATION:
                finding = _terminal_translation_finding(record, path.name)
            else:
                finding = _terminal_auxiliary_finding(record, path.name)
        except Exception as exc:  # noqa: BLE001 - malformed durable evidence
            errors.append(
                f"unclassifiable durable response in {path.name}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if finding is not None:
            # The frozen G7F runtime persists the canonical ``request_sha256``
            # (the SHA-256 of the identical normalized request body) in both the
            # paid-request journal and the invocation ledger record, so the
            # durable terminal response can be matched to the exact ledger
            # record that accounted it.
            finding["request_sha256"] = record.get("request_sha256")
            terminal_findings.append(finding)

    return {
        "target_chapter": int(target_chapter),
        "journal_dir": str(journal_dir),
        "errors": sorted(set(errors)),
        "uncertain": uncertain,
        "planned": planned,
        "terminal_findings": terminal_findings,
        "journal_records": copy.deepcopy(list(inspection.get("records", []))),
    }


# ---------------------------------------------------------------------------
# Per-run binding and disposition
# ---------------------------------------------------------------------------


def _validate_run_identity(
    run_root: Path,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    identity_path = run_root / RUN_IDENTITY_FILENAME
    identity = _read_object(identity_path, "G7C run identity")
    check = copy.deepcopy(identity)
    stored = check.pop("identity_sha256", None)
    if not _is_sha256(stored) or _sha256_json(check) != stored:
        raise G7RControllerError(
            f"G7C run identity SHA does not recompute: {identity_path}"
        )
    if identity.get("g7c_integration_sha256") != G7C_INTEGRATION_SHA256:
        raise G7RControllerError(
            f"run {spec['relative_root']} binds another G7C integration"
        )
    for field in (
        "run_index",
        "model_id",
        "novel_id",
        "run_sha256",
        "s0_identity_sha256",
        "relative_root",
    ):
        if identity.get(field) != spec.get(field):
            raise G7RControllerError(
                f"run {spec['relative_root']} identity changed: {field}"
            )
    return identity


def _read_progress(run_root: Path) -> dict[str, Any]:
    progress = _read_object(run_root / "data" / "progress.json", "Stage-1 progress")
    for field in ("last_completed", "next_chapter"):
        value = progress.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise G7RControllerError(
                f"Stage-1 progress {field} is invalid for {run_root}"
            )
    if progress["next_chapter"] != progress["last_completed"] + 1:
        raise G7RControllerError(
            f"Stage-1 progress is not chronological for {run_root}"
        )
    return progress


def derive_run_disposition(
    stage1_root: Path | str,
    spec: Mapping[str, Any],
    target_chapter: int,
) -> dict[str, Any]:
    """Classify one run for ``target_chapter`` from durable evidence only."""
    root = Path(stage1_root).resolve()
    run_root = (root / Path(str(spec["relative_root"]))).resolve()

    # G7S: the durable quarantine marker is read-only evidence.  A valid marker
    # blocks the blocked chapter and every later one; an invalid marker fails
    # closed as corrupt evidence and dominates every other cause; an absent
    # marker is a no-op.  No ``expected_g7r_controller_sha256`` is supplied: the
    # marker's stored controller digest is historical provenance, so a marker is
    # never invalidated merely because the current controller identity moved on.
    g7s = inspect_durable_quarantine_marker(run_root, spec=spec)
    base: dict[str, Any] = {
        "run_index": int(spec["run_index"]),
        "relative_root": str(spec["relative_root"]),
        "model_id": str(spec["model_id"]),
        "novel_id": str(spec["novel_id"]),
        "run_root": str(run_root),
        "durable_quarantine": g7s,
    }

    if g7s["state"] == MARKER_STATE_INVALID:
        return {
            **base,
            "disposition": DISPOSITION_CORRUPT,
            "detail": f"G7S durable marker {g7s['reason']}: {g7s['detail']}",
            "progress": None,
            "evidence": None,
        }

    if not run_root.is_dir():
        return {
            **base,
            "disposition": DISPOSITION_CORRUPT,
            "detail": "run root is missing",
            "progress": None,
            "evidence": None,
        }

    try:
        _validate_run_identity(run_root, spec)
        progress = _read_progress(run_root)
        evidence = scan_run_evidence(run_root, target_chapter=target_chapter)
    except G7RControllerError as exc:
        return {
            **base,
            "disposition": DISPOSITION_CORRUPT,
            "detail": str(exc),
            "progress": None,
            "evidence": None,
        }

    common = {
        **base,
        "progress": {
            "last_completed": progress["last_completed"],
            "next_chapter": progress["next_chapter"],
        },
        "evidence": evidence,
    }

    # A durable G7S quarantine outlives the process that recorded it: because
    # chronology is broken at ``first_blocked_chapter``, the run can never be
    # ready for that chapter or any later one.  Targets below it keep their
    # historical classification.
    if g7s["state"] == MARKER_STATE_VALID and target_chapter >= int(
        g7s["first_blocked_chapter"]
    ):
        return {
            **common,
            "disposition": DISPOSITION_QUARANTINED,
            "detail": (
                f"durable G7S quarantine marker: first_blocked_chapter="
                f"{g7s['first_blocked_chapter']} source_outcome="
                f"{g7s['source_outcome']} ({g7s['detail']})"
            ),
        }

    # Ambiguity dominates: a started/uncertain/corrupt journal must never be
    # re-read as a model failure, and must never be retried.
    if evidence["errors"]:
        return {
            **common,
            "disposition": DISPOSITION_CORRUPT,
            "detail": "; ".join(evidence["errors"]),
        }
    if evidence["uncertain"]:
        return {
            **common,
            "disposition": DISPOSITION_QUARANTINED,
            "detail": (
                "blocking journal(s): "
                + ", ".join(
                    str(item.get("file")) for item in evidence["uncertain"]
                )
            ),
        }
    if evidence["terminal_findings"]:
        return {
            **common,
            "disposition": DISPOSITION_TERMINAL,
            "detail": "; ".join(
                f"{item['file']}:{item['failure_kind']}"
                for item in evidence["terminal_findings"]
            ),
        }

    last_completed = progress["last_completed"]
    if last_completed >= target_chapter:
        return {
            **common,
            "disposition": DISPOSITION_HEALTHY_COMPLETED,
            "detail": f"already completed target chapter {target_chapter}",
        }
    if (
        last_completed == target_chapter - 1
        and progress["next_chapter"] == target_chapter
    ):
        return {
            **common,
            "disposition": DISPOSITION_READY_FOR_TARGET,
            "detail": f"chronologically positioned for chapter {target_chapter}",
        }
    return {
        **common,
        "disposition": DISPOSITION_NOT_READY,
        "detail": (
            f"last_completed={last_completed} next_chapter="
            f"{progress['next_chapter']} is not the frontier for chapter "
            f"{target_chapter}"
        ),
    }


def _durably_quarantined(
    run: Mapping[str, Any], target_chapter: int
) -> bool:
    """Return whether a valid durable G7S marker blocks ``target_chapter``.

    The rule mirrors ``derive_run_disposition`` exactly: a valid marker blocks
    its ``first_blocked_chapter`` and every later target.  Targets below it are
    not blocked.
    """
    snapshot = run.get("durable_quarantine")
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("state") != MARKER_STATE_VALID:
        return False
    first = snapshot.get("first_blocked_chapter")
    if isinstance(first, bool) or not isinstance(first, int):
        return False
    return target_chapter >= first


def derive_cohort(stage1_root: Path | str, target_chapter: int) -> dict[str, Any]:
    """Derive the full twelve-run cohort disposition and active set."""
    chapter = assert_valid_target_chapter(target_chapter)
    runs = [
        derive_run_disposition(stage1_root, spec, chapter)
        for spec in stage1_run_specs()
    ]
    active = [
        run["run_index"]
        for run in runs
        if run["disposition"] == DISPOSITION_READY_FOR_TARGET
    ]
    counts: dict[str, int] = {}
    for run in runs:
        counts[run["disposition"]] = counts.get(run["disposition"], 0) + 1
    return {
        "target_chapter": chapter,
        "active_run_indices": active,
        "active_count": len(active),
        "disposition_counts": dict(sorted(counts.items())),
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# Envelope derivation
# ---------------------------------------------------------------------------


def _max_source_characters(
    run_root: Path,
    first_chapter: int,
    last_chapter: int,
) -> int:
    chapters = _read_json(
        run_root / "data" / "chapters.json",
        "Stage-1 chapter library",
    )
    if not isinstance(chapters, list):
        raise G7RControllerError("Stage-1 chapter library must be a list")
    maximum = 0
    observed = 0
    for entry in chapters:
        if not isinstance(entry, Mapping):
            continue
        number = entry.get("chapter")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        if number < first_chapter or number > last_chapter:
            continue
        count = entry.get("character_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            text = entry.get("text")
            count = len(text) if isinstance(text, str) else 0
        maximum = max(maximum, count)
        observed += 1
    if observed == 0:
        raise G7RControllerError(
            f"chapter library has no chapter in {first_chapter}..{last_chapter} "
            f"for {run_root}"
        )
    return maximum


def derive_glossary_review_send_ceiling() -> int:
    """Derive the bounded reachable chapter glossary-review send maximum.

    The primary review and, for a nonempty-malformed payload, one generic JSON
    repair both precede any bounded correction.  Two correction branches are
    reachable:

    * remote schema correction (one send); and
    * semantic correction with its single bounded retry (two sends).

    They are MUTUALLY EXCLUSIVE: a durable remote schema correction consumes the
    one bounded remote correction budget, so no semantic correction remains, and
    vice versa.  The reachable maximum is the larger of the two branch totals.
    The derivation is closed and never assumes both corrections can run.
    """
    schema_branch = (
        GLOSSARY_REVIEW_PRIMARY_SENDS
        + GLOSSARY_REVIEW_JSON_REPAIR_SENDS
        + GLOSSARY_REVIEW_REMOTE_SCHEMA_CORRECTION_SENDS
    )
    semantic_branch = (
        GLOSSARY_REVIEW_PRIMARY_SENDS
        + GLOSSARY_REVIEW_JSON_REPAIR_SENDS
        + GLOSSARY_REVIEW_SEMANTIC_CORRECTION_SENDS
        + GLOSSARY_REVIEW_SEMANTIC_CORRECTION_RETRY_SENDS
    )
    return max(schema_branch, semantic_branch)


OPERATION_SENDS_GLOSSARY_REVIEW = derive_glossary_review_send_ceiling()


def _derive_state_growth_budget_bytes(
    *,
    chapter_horizon: int,
    max_source_chars: int,
) -> int:
    """Derive a finite, conservative accumulated glossary/state growth bound.

    A wave prompt carries accumulated chronological state: prior translation
    context, the compact glossary/continuity state, and any source-anchored term
    bytes.  Every component is bounded by a frozen output/input ceiling, so the
    total is finite, reproducible, and strictly derived rather than an arbitrary
    constant:

    * accumulated prior translation context: every chapter strictly before the
      horizon can contribute at most one full translation output ceiling;
    * accumulated glossary/continuity state: every chapter through the horizon
      can contribute at most one auxiliary completion ceiling of compact JSON;
    * source-anchored term bytes: at most the largest source chapter in the span.

    The bound is intentionally generous because a static wave plan must dominate
    the serialized request bound for any real chapter in the supported horizon;
    correctness dominates a tight dollar estimate.
    """
    if (
        isinstance(chapter_horizon, bool)
        or not isinstance(chapter_horizon, int)
        or chapter_horizon < 1
    ):
        raise G7RControllerError("chapter horizon must be a positive integer")
    if (
        isinstance(max_source_chars, bool)
        or not isinstance(max_source_chars, int)
        or max_source_chars < 0
    ):
        raise G7RControllerError("max source characters must be non-negative")

    per_chapter_translation_bytes = (
        TRANSLATION_OUTPUT_TOKENS * UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    )
    per_chapter_state_growth_bytes = (
        AUXILIARY_OUTPUT_TOKENS * UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    )
    return (
        max(chapter_horizon - 1, 0) * per_chapter_translation_bytes
        + chapter_horizon * per_chapter_state_growth_bytes
        + max_source_chars * UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    )


def _operation_send_ceiling(
    *,
    audit_due: bool,
    auto_resolution_enabled: bool,
    residual_cjk_enabled: bool,
) -> dict[str, int]:
    glossary = OPERATION_SENDS_GLOSSARY_REVIEW + (
        OPERATION_SENDS_GLOSSARY_CONFLICT_ADJUDICATION
        if auto_resolution_enabled
        else 0
    )
    terminology = OPERATION_SENDS_TERMINOLOGY_STRICT + (
        OPERATION_SENDS_RESIDUAL_CJK if residual_cjk_enabled else 0
    )
    return {
        PRIMARY_TRANSLATION_OPERATION: OPERATION_SENDS_TRANSLATION,
        "state_extraction": OPERATION_SENDS_STATE_EXTRACTION,
        "state_json_repair": OPERATION_SENDS_STATE_JSON_REPAIR,
        "glossary_review": glossary,
        "periodic_audit": 1 if audit_due else 0,
        "terminology_repair": terminology,
    }


def _worst_case_rates(model_id: str) -> dict[str, Any]:
    """Return the worst-case frozen G7D rates for one model.

    DeepSeek uses the published UTC peak/off-peak schedule, so a static ceiling
    must assume its peak period.  Every other Stage-1 model is flat, so its
    frozen rate is already its worst case.
    """
    model = MODEL_RATECARD[model_id]
    if model["pricing_mode"] == "utc_peak_offpeak":
        rates = model["periods"]["peak"]
        period = "peak"
    else:
        rates = model["periods"]["flat"]
        period = "flat"
    return {
        "pricing_period": period,
        "cache_hit_rate_per_million": float(Decimal(rates["cache_read"])),
        "cache_miss_rate_per_million": float(Decimal(rates["input"])),
        "output_rate_per_million": float(Decimal(rates["output"])),
    }


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def derive_run_envelope(
    stage1_root: Path | str,
    spec: Mapping[str, Any],
    target_chapter: int,
) -> dict[str, Any]:
    """Derive one finite, conservative send/CE envelope for a run and chapter.

    Everything here is derived from the current production lifecycle and the
    frozen G7P runtime semantics, never copied from an obsolete Chapter-2 plan:

    * periodic-audit reachability comes from the real audit cadence;
    * glossary conflict-adjudication reachability comes from the per-novel
      ``glossary_candidate_auto_resolution`` opt-in;
    * strict-terminology and residual-CJK reachability come from the production
      call graph plus the per-novel ``residual_cjk_micro_repair`` opt-in;
    * every legitimate GOAT auxiliary request uses the reviewed 8K G7P runtime
      ceiling and primary translation keeps its 24K corrected ceiling;
    * the prompt bound adds conservative input headroom for source, translation,
      state and glossary growth;
    * cost uses the real G7D rate card with worst-case (peak or flat) rates and
      no cache-hit assumption.
    """
    root = Path(stage1_root).resolve()
    run_root = (root / Path(str(spec["relative_root"]))).resolve()

    config = load_novel_config(run_root / "novel.json")
    audit_interval = _audit_interval_for_spec(spec)
    last_audited = 0
    audit_due = periodic_audit_due(
        target_chapter,
        last_audited_chapter=last_audited,
        audit_interval=audit_interval,
    )
    if audit_due:
        raise G7RControllerError(
            "a periodic audit is reachable for the requested target; the wave "
            "controller must not plan an audit-bearing chapter"
        )

    auto_resolution_enabled = bool(
        config.glossary_candidate_auto_resolution.enabled
    )
    residual_cjk_enabled = bool(config.residual_cjk_micro_repair.enabled)

    first, last = target_chapter_bounds()
    chapter_span_first = max(first, target_chapter)
    max_source_chars = _max_source_characters(
        run_root, chapter_span_first, last
    )

    source_bytes = max_source_chars * UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    translation_text_bytes = (
        TRANSLATION_OUTPUT_TOKENS * UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    )
    glossary_and_state_growth_budget = _derive_state_growth_budget_bytes(
        chapter_horizon=last,
        max_source_chars=max_source_chars,
    )
    prompt_token_upper_bound = (
        source_bytes
        + translation_text_bytes
        + glossary_and_state_growth_budget
        + PROMPT_SCHEMA_OVERHEAD_BYTES
        + PROMPT_TOKEN_SAFETY_OVERHEAD
    )
    if prompt_token_upper_bound < 1:
        raise G7RControllerError(
            "could not prove a finite positive prompt upper bound"
        )

    model_id = str(spec["model_id"])
    sends = _operation_send_ceiling(
        audit_due=audit_due,
        auto_resolution_enabled=auto_resolution_enabled,
        residual_cjk_enabled=residual_cjk_enabled,
    )
    total_sends = sum(sends.values())
    if total_sends < 1:
        raise G7RControllerError(
            "could not prove a finite positive per-chapter send ceiling"
        )

    rates = _worst_case_rates(model_id)
    miss_rate = Decimal(str(rates["cache_miss_rate_per_million"]))
    output_rate = Decimal(str(rates["output_rate_per_million"]))
    # EVERY reachable send carries its own input prompt, so the conservative
    # ceiling prices the prompt upper bound once per reachable send (never once
    # per invocation) plus the per-send completion bounds: the 24K primary
    # translation allowance and the 8K auxiliary allowance for every other send.
    prompt_cost = (
        Decimal(prompt_token_upper_bound) * total_sends * miss_rate
    )
    output_tokens = (
        TRANSLATION_OUTPUT_TOKENS * OPERATION_SENDS_TRANSLATION
        + AUXILIARY_OUTPUT_TOKENS * (total_sends - OPERATION_SENDS_TRANSLATION)
    )
    output_cost = Decimal(output_tokens) * output_rate
    cost_ceiling = (prompt_cost + output_cost) / Decimal(1_000_000)
    if not cost_ceiling.is_finite() or cost_ceiling <= 0:
        raise G7RControllerError(
            "could not prove a finite positive credit-equivalent ceiling"
        )

    return {
        "target_chapter": int(target_chapter),
        "audit_interval": audit_interval,
        "last_audited_chapter": last_audited,
        "periodic_audit_due": audit_due,
        "glossary_candidate_auto_resolution_enabled": auto_resolution_enabled,
        "residual_cjk_micro_repair_enabled": residual_cjk_enabled,
        "strict_terminology_repair_reachable": True,
        "chapter_span": [chapter_span_first, last],
        "max_source_characters_in_span": max_source_chars,
        "prompt_token_upper_bound_per_send": prompt_token_upper_bound,
        "prompt_headroom_bytes": {
            "translation_text_upper_bound": translation_text_bytes,
            "glossary_and_state_growth_budget": (
                glossary_and_state_growth_budget
            ),
            "glossary_and_state_growth_budget_horizon_chapters": last,
            "schema_overhead": PROMPT_SCHEMA_OVERHEAD_BYTES,
            "g7f_prompt_token_safety_overhead": PROMPT_TOKEN_SAFETY_OVERHEAD,
        },
        "operation_max_new_sends": sends,
        "total_max_new_sends": total_sends,
        "output_token_ceilings": {
            PRIMARY_TRANSLATION_OPERATION: TRANSLATION_OUTPUT_TOKENS,
            "auxiliary": AUXILIARY_OUTPUT_TOKENS,
        },
        "worst_case_pricing": {
            "pricing_version": PRICING_VERSION,
            "cache_hit_assumed": False,
            "prompt_input_priced_per_reachable_send": True,
            "reachable_sends_priced": total_sends,
            **rates,
        },
        "maximum_new_provider_sends": total_sends,
        "maximum_credit_equivalent_usd": _decimal_text(cost_ceiling),
    }


# ---------------------------------------------------------------------------
# Controller / policy identity
# ---------------------------------------------------------------------------


def _policy_bindings() -> dict[str, Any]:
    first, last = target_chapter_bounds()
    return {
        "g7f_executor_sha256": G7F_EXECUTOR_SHA256,
        "g7p_stage1_runtime_auxiliary_policy_sha256": (
            G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256
        ),
        "g7n_auxiliary_8k_policy_sha256": G7J_AUXILIARY_8K_POLICY_SHA256,
        "g7e_authorization_sha256": G7E_AUTHORIZATION_SHA256,
        "g7c_integration_sha256": G7C_INTEGRATION_SHA256,
        "g7d_accounting_sha256": G7D_ACCOUNTING_SHA256,
        "g7s_stage1_durable_quarantine_policy_sha256": G7S_POLICY_SHA256,
        "stage1_roots_manifest_sha256": EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
        "pricing_version": PRICING_VERSION,
        "target_chapter_range": [first, last],
        "audit_first_reachable_chapter": _first_audit_chapter(),
        "reasoning_policy": {
            model_id: effort
            for model_id, effort in translation_reasoning_effort_by_model().items()
        },
        "auxiliary_reasoning_policy": {
            spec["model_id"]: dict(reasoning_by_operation(spec["model_id"]))
            for spec in stage1_run_specs()
        },
    }


def g7r_controller_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G7R controller contract."""
    first, last = target_chapter_bounds()
    return {
        "schema_version": G7R_SCHEMA_VERSION,
        "revision": G7R_REVISION,
        "repository_anchor": G7R_REPOSITORY_ANCHOR,
        "module": CONTROLLER_MODULE_NAME,
        "scope": {
            "target_chapter_range": [first, last],
            "audit_first_reachable_chapter": _first_audit_chapter(),
            "one_target_chapter_per_execute": True,
            "automatic_next_chapter_execution": False,
            "automatic_retry": False,
            "ambiguous_outcome_stops_entire_wave": True,
            "terminal_failure_continues_independent_runs": True,
            "success_inferred_from_stdout": False,
            "preflight_before_first_credential_access": True,
            "coding_agent_used_for_inference": False,
        },
        "policies": _policy_bindings(),
        "provider_calls": 0,
    }


G7R_CONTROLLER_SHA256 = _sha256_json(g7r_controller_identity())


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------


def build_status_report(
    *,
    stage1_root: Path | str,
    target_chapter: int = TARGET_FIRST_CHAPTER,
) -> dict[str, Any]:
    """Build the deterministic, read-only cohort status document."""
    chapter = assert_valid_target_chapter(target_chapter)
    root = Path(stage1_root).resolve()
    if not root.is_dir():
        raise G7RControllerError(f"Stage-1 root is missing: {root}")

    cohort = derive_cohort(root, chapter)
    terminal = [
        run["run_index"]
        for run in cohort["runs"]
        if run["disposition"] == DISPOSITION_TERMINAL
    ]
    quarantined = [
        run["run_index"]
        for run in cohort["runs"]
        if run["disposition"] == DISPOSITION_QUARANTINED
    ]
    corrupt = [
        run["run_index"]
        for run in cohort["runs"]
        if run["disposition"] == DISPOSITION_CORRUPT
    ]
    completed = [
        run["run_index"]
        for run in cohort["runs"]
        if run["disposition"] == DISPOSITION_HEALTHY_COMPLETED
    ]
    durably_quarantined = [
        run["run_index"]
        for run in cohort["runs"]
        if _durably_quarantined(run, chapter)
    ]

    return {
        "status": "OK",
        "schema_version": G7R_SCHEMA_VERSION,
        "revision": G7R_REVISION,
        "controller_sha256": G7R_CONTROLLER_SHA256,
        "stage1_root": str(root),
        "target_chapter": chapter,
        "target_chapter_range": list(target_chapter_bounds()),
        "active_run_indices": list(cohort["active_run_indices"]),
        "active_count": cohort["active_count"],
        "disposition_counts": cohort["disposition_counts"],
        "terminal_run_indices": terminal,
        "quarantined_run_indices": quarantined,
        "durably_quarantined_run_indices": durably_quarantined,
        "corrupt_run_indices": corrupt,
        "healthy_completed_run_indices": completed,
        "runs": [
            {
                "run_index": run["run_index"],
                "relative_root": run["relative_root"],
                "model_id": run["model_id"],
                "novel_id": run["novel_id"],
                "disposition": run["disposition"],
                "detail": run["detail"],
                "progress": run["progress"],
                "durable_quarantine": run["durable_quarantine"],
                "terminal_evidence": (
                    run["evidence"]["terminal_findings"] if run["evidence"] else []
                ),
                "uncertain_journals": (
                    [item.get("file") for item in run["evidence"]["uncertain"]]
                    if run["evidence"]
                    else []
                ),
                "journal_errors": (
                    run["evidence"]["errors"] if run["evidence"] else []
                ),
            }
            for run in cohort["runs"]
        ],
        "policy_bindings": _policy_bindings(),
        "zero_provider_verification": {
            "provider_calls": 0,
            "credential_access": False,
            "provider_client_constructed": False,
            "transport_sends": 0,
            "engine_loaded": False,
        },
    }


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------


def _wave_directory(control_dir: Path, chapter: int) -> Path:
    return Path(control_dir) / f"wave_chapter_{chapter:02d}"


def wave_bundle_path(control_dir: Path | str, chapter: int) -> Path:
    """Return the deterministic bundle path for one chapter's wave."""
    return _wave_directory(Path(control_dir), chapter) / (
        f"g7r_wave_chapter_{chapter:02d}_bundle.json"
    )


def _plan_filename(run_index: int, relative_root: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in relative_root
    )
    return f"run_{run_index:02d}_{safe}.plan.json"


def semantic_bundle_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bundle payload used for the stable semantic bundle SHA."""
    return {
        key: copy.deepcopy(value)
        for key, value in dict(bundle).items()
        if key not in VOLATILE_BUNDLE_FIELDS
    }


def compute_bundle_sha256(bundle: Mapping[str, Any]) -> str:
    return _sha256_json(semantic_bundle_payload(bundle))


def _build_one_run_plan(
    *,
    stage1_root: Path,
    spec: Mapping[str, Any],
    target_chapter: int,
    bundle_dir: Path,
    plans_dir: Path,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    plan = build_stage1_invocation_plan(
        stage1_root=stage1_root,
        run_index=int(spec["run_index"]),
        target_chapter=target_chapter,
        maximum_new_provider_sends=int(
            envelope["maximum_new_provider_sends"]
        ),
        maximum_credit_equivalent_usd=envelope[
            "maximum_credit_equivalent_usd"
        ],
    )
    plan_path = plans_dir / _plan_filename(
        int(spec["run_index"]), str(spec["relative_root"])
    )
    plan_path.write_text(
        json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    # Re-inspect the materialized plan from disk: never trust the in-memory one.
    reloaded = _read_object(plan_path, "materialized G7F plan")
    inspection = inspect_stage1_invocation_plan(
        stage1_root=stage1_root,
        plan=reloaded,
    )
    inspected_plan = inspection["plan"]
    if inspected_plan["plan_sha256"] != plan["plan_sha256"]:
        raise G7RControllerError(
            f"materialized plan SHA drifted for run {spec['run_index']}"
        )
    if (
        inspected_plan["pre_invocation_state_fingerprint_sha256"]
        != plan["pre_invocation_state_fingerprint_sha256"]
    ):
        raise G7RControllerError(
            f"materialized plan fingerprint drifted for run {spec['run_index']}"
        )
    token = inspected_plan["approval_token"]
    if not _is_sha256(token) or token != plan["approval_token"]:
        raise G7RControllerError(
            f"materialized approval token drifted for run {spec['run_index']}"
        )

    return {
        "run_index": int(spec["run_index"]),
        "relative_root": str(spec["relative_root"]),
        "run_root": str((stage1_root / Path(str(spec["relative_root"]))).resolve()),
        "model_id": str(spec["model_id"]),
        "novel_id": str(spec["novel_id"]),
        "pre_invocation_state_fingerprint_sha256": (
            inspected_plan["pre_invocation_state_fingerprint_sha256"]
        ),
        "chapter_resume_status": inspected_plan["chapter_resume_status"],
        "journal_resume_status": inspected_plan["journal_resume_status"],
        "plan_relative_path": str(
            plan_path.relative_to(bundle_dir)
        ).replace("\\", "/"),
        "plan_file_sha256": _sha256_file(plan_path),
        "plan_semantic_sha256": inspected_plan["plan_sha256"],
        "approval_token": token,
        "maximum_new_provider_sends": int(
            inspected_plan["g7e_plan"]["maximum_new_provider_sends"]
        ),
        "maximum_credit_equivalent_usd": str(
            inspected_plan["g7e_plan"]["maximum_credit_equivalent_usd"]
        ),
        "envelope_derivation": copy.deepcopy(dict(envelope)),
    }


def plan_wave(
    *,
    stage1_root: Path | str,
    target_chapter: int,
    control_dir: Path | str,
    repository_head: str | None = None,
    code_root: Path | str | None = None,
    repository_head_resolver: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    """Materialize one wave bundle for one target chapter (zero Provider).

    ``repository_head`` is an optional EXPECTED repository HEAD assertion, never
    an override: the ACTUAL checkout HEAD is always resolved from disk, recorded
    in the bundle, and re-resolved before the bundle is finalized so a HEAD change
    during planning fails closed.  A supplied expectation that disagrees with the
    observed HEAD fails closed before any bundle is written.
    """
    chapter = assert_valid_target_chapter(target_chapter)
    root = Path(stage1_root).resolve()
    control = Path(control_dir).resolve()
    if not root.is_dir():
        raise G7RControllerError(f"Stage-1 root is missing: {root}")
    if control == root or root in control.parents or control in root.parents:
        raise G7RControllerError(
            "the control directory must be outside the Stage-1 evidence root"
        )

    resolver = repository_head_resolver or resolve_repository_head
    head_root = (
        Path(code_root).resolve()
        if code_root is not None
        else Path(__file__).resolve().parent
    )
    observed_head = resolver(head_root)
    if not isinstance(observed_head, str) or not observed_head.strip():
        raise G7RBundleError(
            "the controller checkout repository HEAD could not be resolved"
        )
    if repository_head is not None and repository_head != observed_head:
        raise G7RBundleError(
            "expected repository HEAD does not match the controller checkout: "
            f"expected={repository_head!r} observed={observed_head!r}"
        )

    cohort = derive_cohort(root, chapter)
    active = list(cohort["active_run_indices"])
    if not active:
        raise G7RControllerError(
            f"no active Stage-1 run is positioned for chapter {chapter}"
        )

    work_dir = _wave_directory(control, chapter)
    plans_dir = work_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    specs = {int(spec["run_index"]): spec for spec in stage1_run_specs()}
    run_records = []
    for run_index in active:
        spec = specs[run_index]
        envelope = derive_run_envelope(root, spec, chapter)
        run_records.append(
            _build_one_run_plan(
                stage1_root=root,
                spec=spec,
                target_chapter=chapter,
                bundle_dir=work_dir,
                plans_dir=plans_dir,
                envelope=envelope,
            )
        )

    final_head = resolver(head_root)
    if final_head != observed_head:
        raise G7RBundleError(
            "the repository HEAD changed during wave planning: "
            f"start={observed_head!r} end={final_head!r}"
        )

    bundle: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "revision": G7R_REVISION,
        "status": BUNDLE_STATUS_READY,
        "controller_sha256": G7R_CONTROLLER_SHA256,
        "repository_head": observed_head,
        "target_chapter": chapter,
        "target_chapter_range": list(target_chapter_bounds()),
        "stage1_root": str(root),
        "active_run_indices": active,
        "active_count": len(active),
        "cohort_disposition_counts": cohort["disposition_counts"],
        "durably_quarantined_run_indices": [
            run["run_index"]
            for run in cohort["runs"]
            if _durably_quarantined(run, chapter)
        ],
        "cohort_dispositions": [
            {
                "run_index": run["run_index"],
                "relative_root": run["relative_root"],
                "model_id": run["model_id"],
                "novel_id": run["novel_id"],
                "disposition": run["disposition"],
                "detail": run["detail"],
                "progress": run["progress"],
                "durable_quarantine": run["durable_quarantine"],
            }
            for run in cohort["runs"]
        ],
        "runs": run_records,
        "ceilings": {
            "per_run": [
                {
                    "run_index": record["run_index"],
                    "maximum_new_provider_sends": record[
                        "maximum_new_provider_sends"
                    ],
                    "maximum_credit_equivalent_usd": record[
                        "maximum_credit_equivalent_usd"
                    ],
                }
                for record in run_records
            ],
            "aggregate_maximum_new_provider_sends": sum(
                record["maximum_new_provider_sends"] for record in run_records
            ),
            "aggregate_maximum_credit_equivalent_usd": _decimal_text(
                sum(
                    (
                        Decimal(record["maximum_credit_equivalent_usd"])
                        for record in run_records
                    ),
                    Decimal(0),
                )
            ),
        },
        "policies": _policy_bindings(),
        "zero_provider_verification": {
            "provider_calls": 0,
            "credential_access": False,
            "provider_client_constructed": False,
            "transport_sends": 0,
            "engine_loaded": False,
            "bundle_scope": "plan_only",
        },
        "created_utc": _now_utc_iso(),
    }
    bundle_path = wave_bundle_path(control, chapter)
    bundle["bundle_sha256"] = compute_bundle_sha256(bundle)
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return bundle


def load_wave_bundle(bundle_path: Path | str) -> dict[str, Any]:
    bundle = _read_object(bundle_path, "G7R wave bundle")
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise G7RBundleError("unsupported wave bundle schema")
    if bundle.get("revision") != G7R_REVISION:
        raise G7RBundleError("unsupported wave bundle revision")
    return bundle


def verify_wave_bundle(
    bundle: Mapping[str, Any],
    *,
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    """Fail closed unless the bundle is intact and its policies still hold."""
    stored = bundle.get("bundle_sha256")
    if not _is_sha256(stored):
        raise G7RBundleError("wave bundle SHA is missing or malformed")
    recomputed = compute_bundle_sha256(bundle)
    if recomputed != stored:
        raise G7RBundleError("wave bundle SHA does not recompute")
    if not _is_sha256(expected_bundle_sha256):
        raise G7RBundleError("expected bundle SHA is malformed")
    if stored != expected_bundle_sha256:
        raise G7RBundleError("wave bundle SHA mismatch")
    if bundle.get("status") != BUNDLE_STATUS_READY:
        raise G7RBundleError(
            f"wave bundle is not ready for authorization: {bundle.get('status')!r}"
        )
    if bundle.get("controller_sha256") != G7R_CONTROLLER_SHA256:
        raise G7RBundleError("wave bundle binds another G7R controller identity")
    observed = _policy_bindings()
    recorded = bundle.get("policies")
    if not isinstance(recorded, Mapping):
        raise G7RBundleError("wave bundle has no policy bindings")
    for field in (
        "g7f_executor_sha256",
        "g7p_stage1_runtime_auxiliary_policy_sha256",
        "g7n_auxiliary_8k_policy_sha256",
        "g7e_authorization_sha256",
        "g7c_integration_sha256",
        "g7d_accounting_sha256",
        "g7s_stage1_durable_quarantine_policy_sha256",
        "stage1_roots_manifest_sha256",
        "pricing_version",
    ):
        if recorded.get(field) != observed[field]:
            raise G7RBundleError(
                f"wave bundle binds a stale repository policy: {field}"
            )
    return {"recomputed_bundle_sha256": recomputed}


def resolve_repository_head(code_root: Path | str) -> str:
    """Resolve the exact repository HEAD without any network access."""
    root = Path(code_root).resolve()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise G7RBundleError(
            f"cannot resolve the repository HEAD for {root}: {exc}"
        ) from exc
    head = (completed.stdout or "").strip()
    if completed.returncode != 0 or not head:
        raise G7RBundleError(
            "cannot resolve the repository HEAD for "
            f"{root}: {(completed.stderr or '').strip()[:200]}"
        )
    return head


# ---------------------------------------------------------------------------
# EXECUTE
# ---------------------------------------------------------------------------


class _TeeWriter:
    """Forward engine progress to stderr and, optionally, one progress log."""

    def __init__(self, primary: Any, log_handle: Any | None = None):
        self._primary = primary
        self._log = log_handle

    def write(self, text: str) -> int:
        written = self._primary.write(text)
        if self._log is not None:
            try:
                self._log.write(text)
                self._log.flush()
            except (OSError, ValueError):
                pass
        return written

    def flush(self) -> None:
        try:
            self._primary.flush()
        except (OSError, ValueError):
            pass
        if self._log is not None:
            try:
                self._log.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False


def read_dpapi_secret(secret_path: Path | str, dpapi: Any) -> str:
    """Read and unprotect one DPAPI secret; the plaintext never leaves memory."""
    target = assert_safe_secret_target(secret_path)
    try:
        ciphertext = target.read_bytes()
    except OSError as exc:
        raise G7RControllerError(
            f"cannot read protected secret: {target}"
        ) from exc
    if not ciphertext:
        raise G7RControllerError("protected secret is empty")
    try:
        plaintext = dpapi.unprotect(ciphertext)
    except G7GSecureLauncherError:
        raise
    except BaseException as exc:  # noqa: BLE001 - opaque DPAPI failure
        raise G7RControllerError(
            f"DPAPI could not unprotect the stored secret: {type(exc).__name__}"
        ) from None
    if isinstance(plaintext, (bytes, bytearray)):
        try:
            text = bytes(plaintext).decode("utf-8")
        except UnicodeDecodeError:
            raise G7RControllerError(
                "unprotected secret is not valid UTF-8"
            ) from None
    elif isinstance(plaintext, str):
        text = plaintext
    else:
        raise G7RControllerError("DPAPI returned an unsupported secret type")
    text = text.strip()
    if not text:
        raise G7RControllerError("unprotected secret is empty")
    return text


def _open_canonical_ledger(
    run_root: Path,
    target_chapter: int,
    plan: Mapping[str, Any] | None,
) -> InvocationBudgetLedger | None:
    """Canonically validate one target-chapter G7F ledger, or return ``None``.

    ``None`` means the ledger file is genuinely absent.  A present ledger is
    never trusted from a hand-parsed subset of fields: it must pass the REAL
    frozen G7F :class:`InvocationBudgetLedger` contract -- schema, revision,
    ``ledger_sha256`` recomputation, ``plan_sha256``, ``approval_token``,
    run/model/novel/chapter identity, send and accepted-cost ceilings, record
    list, contiguous send indexes, known record statuses, and valid
    ``request_sha256`` values -- and must bind the exact materialized plan
    through the frozen ``_assert_plan_binding`` path.  Any other outcome raises
    ``G7RControllerError`` so the caller fails closed instead of weakening the
    contract.

    ``allow_create=False`` keeps this strictly read-only: an existing paid
    response can never fabricate a missing ledger here.
    """
    path = invocation_ledger_path(run_root, target_chapter)
    if not path.is_file():
        return None
    if plan is None:
        raise G7RControllerError(
            "canonical G7F ledger validation requires the exact materialized "
            f"plan for {path}"
        )
    if plan.get("target_chapter") != target_chapter:
        raise G7RControllerError(
            "the exact materialized plan binds another target chapter: "
            f"plan={plan.get('target_chapter')!r} target={target_chapter!r}"
        )
    try:
        return InvocationBudgetLedger.open_or_create(
            run_root=run_root,
            plan=plan,
            allow_create=False,
        )
    except G7FExecutionError as exc:
        raise G7RControllerError(
            f"G7F ledger failed canonical validation: {path}: {exc}"
        ) from None


def _read_ledger_summary(
    run_root: Path,
    target_chapter: int,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one target-chapter ledger through the frozen canonical validator.

    Only a canonically validated, exact-plan-bound ledger is reported as
    present with exact send/cost totals.  A missing ledger stays absent; every
    other unreadable, non-canonical, or plan-mismatched ledger raises
    ``G7RControllerError`` and is never downgraded to a two-field parse.
    """
    path = invocation_ledger_path(run_root, target_chapter)
    ledger = _open_canonical_ledger(run_root, target_chapter, plan)
    if ledger is None:
        return {
            "present": False,
            "new_provider_sends_started": 0,
            "accepted_credit_equivalent_usd": ZERO_CEILING,
            "path": str(path),
        }
    return {
        "present": True,
        "new_provider_sends_started": ledger.new_provider_sends_started,
        "accepted_credit_equivalent_usd": _decimal_text(ledger.accepted_cost),
        "path": str(path),
    }


def _target_chapter_send_evidence(
    run_root: Path,
    target_chapter: int,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return durable evidence that a paid send may have started for a chapter.

    Positive proof is a canonically validated, exact-plan-bound target-chapter
    G7F ledger recording at least one started send, or any target-chapter journal
    record whose durable status is a ``started`` / ``uncertain`` /
    ``response_recorded`` send.  When the ledger cannot be canonically validated
    (missing plan, wrong plan binding, bad hash, inconsistent records) the
    boundary is treated as crossed, so a corrupt accounting artifact can never
    be silently retried.  ``planned`` journals do not by themselves prove a send
    started.
    """
    root = Path(run_root)
    try:
        ledger = _read_ledger_summary(root, target_chapter, plan=plan)
        ledger_sends = int(ledger["new_provider_sends_started"])
        ledger_readable = True
    except G7RControllerError:
        ledger_sends = 1
        ledger_readable = False

    send_like_journals: list[str] = []
    journal_dir = root / "data" / "provider_request_journal"
    prefix = f"{int(target_chapter):04d}."
    if journal_dir.is_dir():
        for path in _journal_files(journal_dir):
            if path.suffix != ".json" or not path.name.startswith(prefix):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, Mapping):
                continue
            if record.get("status") in ("started", "uncertain", "response_recorded"):
                send_like_journals.append(path.name)

    return {
        "paid_boundary_crossed": (
            ledger_sends >= 1 or bool(send_like_journals)
        ),
        "ledger_readable": ledger_readable,
        "ledger_new_provider_sends_started": ledger_sends,
        "send_like_journals": sorted(send_like_journals),
    }


def _accounting_record_for_line(
    run_root: Path | str,
    target_chapter: int,
    outcome: str,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one executed line's target ledger defensively for accounting.

    This never raises: an unreadable, non-canonical, plan-mismatched, or missing
    target ledger must never destroy the structured fail-closed wave result.  A
    present ledger only counts as ``exact`` when it passes the frozen canonical
    ``InvocationBudgetLedger`` contract and binds the exact materialized plan; a
    readable JSON ledger that fails either test becomes
    ``unavailable_corrupt_or_missing`` rather than a two-field parse.  A missing
    ledger is only exact zero when the durable evidence proves the paid boundary
    was not crossed; once the boundary may have been crossed, the line is
    ``unavailable_corrupt_or_missing`` so the wave never fabricates a zero it
    cannot prove.
    """
    root = Path(run_root)
    path = invocation_ledger_path(root, target_chapter)
    try:
        summary = _read_ledger_summary(root, target_chapter, plan=plan)
    except G7RControllerError as exc:
        return {
            "status": ACCOUNTING_UNAVAILABLE,
            "outcome": outcome,
            "ledger_present": None,
            "new_provider_sends_started": None,
            "accepted_credit_equivalent_usd": None,
            "ledger_path": str(path),
            "detail": f"target ledger is unreadable or invalid: {exc}",
        }
    if summary["present"]:
        return {
            "status": ACCOUNTING_EXACT,
            "outcome": outcome,
            "ledger_present": True,
            "new_provider_sends_started": int(
                summary["new_provider_sends_started"]
            ),
            "accepted_credit_equivalent_usd": summary[
                "accepted_credit_equivalent_usd"
            ],
            "ledger_path": summary["path"],
            "detail": "",
        }
    boundary = _target_chapter_send_evidence(root, target_chapter, plan=plan)
    if (
        outcome == RUN_OUTCOME_LOCAL_FAILURE
        and not boundary["paid_boundary_crossed"]
    ):
        return {
            "status": ACCOUNTING_ZERO_PROVEN_PRE_SEND,
            "outcome": outcome,
            "ledger_present": False,
            "new_provider_sends_started": 0,
            "accepted_credit_equivalent_usd": ZERO_CEILING,
            "ledger_path": summary["path"],
            "detail": "proven pre-send failure; no paid boundary was crossed",
        }
    crossing = ", ".join(boundary["send_like_journals"]) or (
        "the paid boundary was crossed"
    )
    return {
        "status": ACCOUNTING_UNAVAILABLE,
        "outcome": outcome,
        "ledger_present": False,
        "new_provider_sends_started": None,
        "accepted_credit_equivalent_usd": None,
        "ledger_path": summary["path"],
        "detail": (
            "target G7F ledger is missing after a possible paid send: "
            + crossing
        ),
    }


def _next_chapter_artifacts_absent(
    run_root: Path,
    target_chapter: int,
) -> dict[str, Any]:
    next_chapter = target_chapter + 1
    chapter_file = (
        Path(run_root) / "output" / "chapters" / f"{next_chapter:04d}.md"
    )
    ledger_file = invocation_ledger_path(run_root, next_chapter)
    journal_dir = Path(run_root) / "data" / "provider_request_journal"
    next_journals = []
    if journal_dir.is_dir():
        for path in _journal_files(journal_dir):
            if path.name.startswith(f"{next_chapter:04d}."):
                next_journals.append(path.name)
    return {
        "next_chapter": next_chapter,
        "next_chapter_file_present": chapter_file.is_file(),
        "next_chapter_ledger_present": ledger_file.is_file(),
        "next_chapter_journals": sorted(next_journals),
    }


def _verify_target_commit(
    run_root: Path,
    target_chapter: int,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    progress = _read_progress(run_root)
    if progress["last_completed"] != target_chapter:
        raise G7RControllerError(
            f"run did not commit exactly chapter {target_chapter}: "
            f"last_completed={progress['last_completed']}"
        )
    if progress["next_chapter"] != target_chapter + 1:
        raise G7RControllerError(
            f"run advanced to an unexpected chapter: "
            f"next_chapter={progress['next_chapter']}"
        )
    chapter_file = (
        Path(run_root) / "output" / "chapters" / f"{int(target_chapter):04d}.md"
    )
    if not chapter_file.is_file():
        raise G7RControllerError(
            f"target chapter file is missing: {chapter_file}"
        )
    ledger = _read_ledger_summary(run_root, target_chapter, plan=plan)
    if not ledger["present"]:
        raise G7RControllerError(
            f"target chapter G7F ledger is missing: {ledger['path']}"
        )
    next_artifacts = _next_chapter_artifacts_absent(run_root, target_chapter)
    if (
        next_artifacts["next_chapter_file_present"]
        or next_artifacts["next_chapter_ledger_present"]
        or next_artifacts["next_chapter_journals"]
    ):
        raise G7RControllerError(
            "an unexpected chapter "
            f"{target_chapter + 1} artifact was created"
        )
    return {"progress": progress, "ledger": ledger, "next_chapter_artifacts": next_artifacts}


def _prove_terminal_target_ledger(
    run_root: Path | str,
    target_chapter: int,
    *,
    plan: Mapping[str, Any] | None = None,
    terminal_findings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Prove the target-chapter G7F ledger is a canonical, plan-bound paid artifact.

    The canonical exactly-once/accounting contract requires the target-chapter
    G7F ledger for every durable paid response.  A deterministic terminal
    model/policy classification is therefore only admissible when that ledger
    exists, passes the REAL frozen ``InvocationBudgetLedger`` contract (schema,
    revision, ``ledger_sha256``, plan/approval binding, records, contiguous send
    indexes, known statuses, request identities, send and accepted-cost
    ceilings), binds the exact materialized plan, and records at least one
    started Provider send.  A ``response_recorded`` / send-like target journal
    without such a ledger is corrupt or unknown durable state and must quarantine
    without retry, never be read as terminal evidence.

    Every target-chapter durable terminal response is additionally matched to the
    exact ledger record that accounted its request.  The frozen G7F runtime
    persists the same canonical ``request_sha256`` (the SHA-256 of the identical
    normalized request body) in both the paid-request journal and the invocation
    ledger record, so a target-chapter terminal finding whose request identity is
    absent from the canonical ledger is corrupt or unknown durable state.  A
    terminal finding from another chapter is deliberately not cross-matched: the
    G7F ledger is per-target-chapter, while a durable terminal rejection remains
    a valid run-level disposition.
    """
    path = invocation_ledger_path(Path(run_root), target_chapter)
    try:
        ledger = _open_canonical_ledger(
            Path(run_root), target_chapter, plan
        )
    except G7RControllerError as exc:
        return {
            "proven": False,
            "reason": f"target G7F ledger is unreadable or invalid: {exc}",
            "ledger": None,
        }
    if ledger is None:
        return {
            "proven": False,
            "reason": f"target G7F ledger is missing: {path}",
            "ledger": None,
        }
    summary = {
        "present": True,
        "new_provider_sends_started": ledger.new_provider_sends_started,
        "accepted_credit_equivalent_usd": _decimal_text(ledger.accepted_cost),
        "path": str(path),
    }
    if ledger.new_provider_sends_started < 1:
        return {
            "proven": False,
            "reason": (
                "target G7F ledger records zero started Provider sends for a "
                "durable terminal response"
            ),
            "ledger": summary,
        }
    records = ledger.document.get("records")
    record_request_hashes = {
        record.get("request_sha256")
        for record in records
        if isinstance(record, Mapping)
    }
    for finding in terminal_findings:
        if finding.get("chapter") != target_chapter:
            continue
        request_sha256 = finding.get("request_sha256")
        if not _is_sha256(request_sha256):
            return {
                "proven": False,
                "reason": (
                    "target-chapter terminal response lacks a valid request "
                    "identity in the paid-request journal"
                ),
                "ledger": summary,
            }
        if request_sha256 not in record_request_hashes:
            return {
                "proven": False,
                "reason": (
                    "target-chapter terminal request is not accounted by the "
                    f"canonical G7F ledger: request_sha256={request_sha256}"
                ),
                "ledger": summary,
            }
    return {"proven": True, "reason": "", "ledger": summary}


def classify_after_execution(
    run_root: Path | str,
    target_chapter: int,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a failed paid invocation from durable evidence only.

    A corrupt or unknown durable state is only a local failure when the paid
    boundary provably was not crossed.  Once a send may have started, corrupt or
    unknown durable state must quarantine the run: the paid outcome is
    unrecoverable and reissue is forbidden.

    A deterministic terminal model/policy finding additionally requires a
    canonically validated, exact-plan-bound target-chapter G7F ledger proving the
    paid boundary was crossed exactly as the canonical accounting contract
    demands; without it the line is corrupt or unknown durable state and must
    quarantine.  ``plan`` is the exact materialized plan for this run and is
    required whenever a target ledger is present.
    """
    evidence = scan_run_evidence(run_root, target_chapter=target_chapter)
    boundary = _target_chapter_send_evidence(
        run_root, target_chapter, plan=plan
    )
    if evidence["errors"]:
        return {
            "outcome": RUN_OUTCOME_CORRUPT_STATE,
            "detail": "corrupt or unknown durable journal evidence after a "
            "possible paid send: " + "; ".join(evidence["errors"]),
            "paid_boundary": boundary,
            "evidence": evidence,
        }
    if evidence["uncertain"]:
        return {
            "outcome": RUN_OUTCOME_QUARANTINED,
            "detail": "ambiguous remote outcome: "
            + ", ".join(str(item.get("file")) for item in evidence["uncertain"]),
            "paid_boundary": boundary,
            "evidence": evidence,
        }
    if evidence["terminal_findings"]:
        proof = _prove_terminal_target_ledger(
            run_root,
            target_chapter,
            plan=plan,
            terminal_findings=evidence["terminal_findings"],
        )
        if not proof["proven"]:
            return {
                "outcome": RUN_OUTCOME_CORRUPT_STATE,
                "detail": (
                    "durable terminal response recorded without a valid target "
                    "G7F ledger: " + str(proof["reason"])
                ),
                "paid_boundary": boundary,
                "evidence": evidence,
            }
        return {
            "outcome": RUN_OUTCOME_TERMINAL,
            "detail": "; ".join(
                f"{item['file']}:{item['failure_kind']}"
                for item in evidence["terminal_findings"]
            ),
            "paid_boundary": boundary,
            "evidence": evidence,
            "terminal_ledger": proof["ledger"],
        }
    if boundary["paid_boundary_crossed"]:
        return {
            "outcome": RUN_OUTCOME_CORRUPT_STATE,
            "detail": "unknown durable post-send state: the paid boundary was "
            "crossed but no durable terminal or ambiguous evidence classifies it",
            "paid_boundary": boundary,
            "evidence": evidence,
        }
    return {
        "outcome": RUN_OUTCOME_LOCAL_FAILURE,
        "detail": "no durable terminal or ambiguous evidence was recorded and "
        "no paid send was started",
        "paid_boundary": boundary,
        "evidence": evidence,
    }


def execute_wave(
    *,
    stage1_root: Path | str,
    bundle_path: Path | str,
    expected_bundle_sha256: str,
    authorize_paid_wave: bool,
    code_root: Path | str | None = None,
    secret_path: Path | str | None = None,
    dpapi: Any | None = None,
    preflight_fn: Callable[..., Mapping[str, Any]] | None = None,
    execute_fn: Callable[..., Mapping[str, Any]] | None = None,
    repository_head_resolver: Callable[[Path], str] | None = None,
    progress_log_path: Path | str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Execute exactly one target chapter of one wave bundle.

    The zero-Provider preflight phase runs for every active run before the first
    credential access.  Any preflight failure, missing authorization flag, or
    bundle mismatch aborts before the secret is read and before any send.
    """
    if authorize_paid_wave is not True:
        raise G7RControllerError(
            "paid wave execution requires the explicit --authorize-paid-wave flag"
        )

    root = Path(stage1_root).resolve()
    bundle_file = Path(bundle_path).resolve()
    bundle = load_wave_bundle(bundle_file)
    verify_wave_bundle(bundle, expected_bundle_sha256=expected_bundle_sha256)

    if Path(str(bundle["stage1_root"])).resolve() != root:
        raise G7RBundleError("wave bundle binds another Stage-1 root")

    chapter = assert_valid_target_chapter(int(bundle["target_chapter"]))
    bundle_dir = bundle_file.parent

    resolver = repository_head_resolver or resolve_repository_head
    head_root = Path(code_root).resolve() if code_root is not None else Path(
        __file__
    ).resolve().parent
    observed_head = resolver(head_root)
    if observed_head != bundle.get("repository_head"):
        raise G7RBundleError(
            "wave bundle was planned against a stale repository HEAD: "
            f"bundle={bundle.get('repository_head')!r} observed={observed_head!r}"
        )

    starting_active = [int(index) for index in bundle["active_run_indices"]]
    if not starting_active:
        raise G7RBundleError("wave bundle contains no active runs")

    # Re-derive the cohort and require every active run to still be ready.
    cohort = derive_cohort(root, chapter)
    for run in cohort["runs"]:
        if run["run_index"] not in starting_active:
            continue
        if run["disposition"] != DISPOSITION_READY_FOR_TARGET:
            raise G7RBundleError(
                f"active run {run['run_index']} is no longer ready for chapter "
                f"{chapter}: {run['disposition']} ({run['detail']})"
            )
    if set(cohort["active_run_indices"]) != set(starting_active):
        raise G7RBundleError(
            "the derived active set changed after the bundle was planned"
        )

    run_records = {int(record["run_index"]): record for record in bundle["runs"]}
    for index in starting_active:
        if index not in run_records:
            raise G7RBundleError(f"wave bundle lacks a plan record for run {index}")

    preflight = preflight_fn or run_preflight
    executor = execute_fn or execute_stage1_invocation

    # ---- phase 1: zero-Provider preflight for every active run --------------
    preflights: dict[int, dict[str, Any]] = {}
    for index in starting_active:
        record = run_records[index]
        plan_path = bundle_dir / record["plan_relative_path"]
        if not plan_path.is_file():
            raise G7RBundleError(
                f"materialized plan is missing for run {index}: {plan_path}"
            )
        if _sha256_file(plan_path) != record["plan_file_sha256"]:
            raise G7RBundleError(
                f"materialized plan bytes changed for run {index}"
            )
        current_fingerprint = state_fingerprint_sha256(Path(record["run_root"]))
        if current_fingerprint != record["pre_invocation_state_fingerprint_sha256"]:
            raise G7RBundleError(
                f"pre-invocation state fingerprint changed for run {index}"
            )
        try:
            result = preflight(
                code_root=Path(__file__).resolve().parent,
                stage1_root=root,
                plan_path=plan_path,
                expected_plan_sha256=record["plan_semantic_sha256"],
                expected_approval_token=record["approval_token"],
            )
        except G7GSecureLauncherError as exc:
            raise G7RPreflightError(
                f"zero-Provider preflight failed for run {index}: {exc}"
            ) from exc
        except BaseException as exc:  # noqa: BLE001 - fail closed
            raise G7RPreflightError(
                f"zero-Provider preflight failed for run {index}: "
                f"{type(exc).__name__}: {exc}"
            ) from None
        if not isinstance(result, Mapping) or result.get(
            "status"
        ) != PREFLIGHT_OK_STATUS:
            raise G7RPreflightError(
                f"zero-Provider preflight did not pass for run {index}"
            )
        if result.get("provider_calls") != 0:
            raise G7RPreflightError(
                f"zero-Provider preflight for run {index} reported Provider calls"
            )
        if result.get("paid_execution_authorized") not in (False, None):
            raise G7RPreflightError(
                f"zero-Provider preflight for run {index} claimed paid "
                "authorization"
            )
        preflights[index] = dict(result)

    # ---- phase 2: credential access only after every preflight passed -------
    # The obvious --secret-path presence/path-safety check precedes any DPAPI
    # construction or use, so a missing or unsafe target never touches DPAPI.
    if secret_path is None:
        raise G7RControllerError(
            "explicitly authorized paid wave execution requires --secret-path"
        )
    try:
        assert_safe_secret_target(secret_path)
    except G7GSecureLauncherError as exc:
        raise G7RControllerError(f"unsafe secret target: {exc}") from exc

    backend = dpapi if dpapi is not None else load_platform_dpapi()
    try:
        api_key = read_dpapi_secret(secret_path, backend)
    except (G7RControllerError, G7GSecureLauncherError) as exc:
        # The secret read/unprotect was attempted but failed: the Provider call
        # count is provably zero, and credential access is reported as an
        # attempt that did not succeed rather than a fabricated false.
        raise G7RExecutionPhaseError(
            f"credential access failed: {exc}",
            phase=EXECUTION_PHASE_SECRET_ACCESS,
            credential_access=False,
            credential_access_attempted=True,
            provider_calls=0,
        ) from exc

    log_handle = None
    if progress_log_path is not None:
        log_target = Path(progress_log_path)
        try:
            log_target.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_target.open("a", encoding="utf-8")
        except OSError as exc:
            api_key = None
            raise G7RExecutionPhaseError(
                "cannot open the progress log after credential access: "
                f"{log_target}",
                phase=EXECUTION_PHASE_POST_SECRET_PRE_EXECUTION,
                credential_access=True,
                credential_access_attempted=True,
                provider_calls=0,
            ) from exc

    completed: list[int] = []
    terminal: list[int] = []
    quarantined: list[int] = []
    per_run: list[dict[str, Any]] = []
    executed: list[int] = []
    # The exact materialized plan for each executed run, so every durable ledger
    # is validated against the frozen G7F contract and its exact plan binding
    # rather than a hand-parsed field subset.
    plans_by_index: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None
    fail_closed = False
    wave_status = WAVE_COMPLETED
    # Flips to True immediately before the first executor invocation: from then
    # on an escaping failure may have reached a live Provider send, so the exact
    # Provider call count is no longer provably zero.
    provider_send_possible = False

    try:
        tee = _TeeWriter(sys.stderr, log_handle)
        for index in starting_active:
            record = run_records[index]
            plan_path = bundle_dir / record["plan_relative_path"]
            plan = _read_object(plan_path, "materialized G7F plan")
            plans_by_index[index] = plan
            executed.append(index)
            provider_send_possible = True
            try:
                with contextlib.redirect_stdout(tee):
                    result = executor(
                        code_root=Path(__file__).resolve().parent,
                        stage1_root=root,
                        plan=plan,
                        expected_plan_sha256=record["plan_semantic_sha256"],
                        expected_approval_token=record["approval_token"],
                        operator_authorized=True,
                        api_key=api_key,
                        clock=clock,
                    )
            except BaseException as exc:  # noqa: BLE001 - classify durably
                failure = classify_after_execution(
                    record["run_root"], chapter, plan=plan
                )
                outcome = failure["outcome"]
                per_run.append(
                    {
                        "run_index": index,
                        "relative_root": record["relative_root"],
                        "outcome": outcome,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "durable_detail": failure["detail"],
                    }
                )
                if outcome == RUN_OUTCOME_TERMINAL:
                    terminal.append(index)
                    continue
                if outcome in QUARANTINED_RUN_OUTCOMES:
                    quarantined.append(index)
                    stop_reason = (
                        f"ambiguous or corrupt durable outcome for run {index} "
                        f"({outcome}); the wave stops immediately and never "
                        "retries"
                    )
                    wave_status = WAVE_STOPPED_AMBIGUOUS
                else:
                    stop_reason = (
                        f"local/pre-send failure for run {index}; the wave stops "
                        "fail-closed"
                    )
                    wave_status = WAVE_STOPPED_LOCAL_FAILURE
                fail_closed = True
                break

            try:
                proof = _verify_target_commit(
                    Path(record["run_root"]), chapter, plan=plan
                )
            except G7RControllerError as exc:
                failure = classify_after_execution(
                    record["run_root"], chapter, plan=plan
                )
                outcome = failure["outcome"]
                per_run.append(
                    {
                        "run_index": index,
                        "relative_root": record["relative_root"],
                        "outcome": outcome,
                        "detail": f"post-run verification failed: {exc}",
                        "durable_detail": failure["detail"],
                    }
                )
                if outcome == RUN_OUTCOME_TERMINAL:
                    terminal.append(index)
                    continue
                if outcome in QUARANTINED_RUN_OUTCOMES:
                    quarantined.append(index)
                    wave_status = WAVE_STOPPED_AMBIGUOUS
                else:
                    wave_status = WAVE_STOPPED_LOCAL_FAILURE
                stop_reason = (
                    f"post-run verification failed for run {index}: {exc}"
                )
                fail_closed = True
                break

            completed.append(index)
            per_run.append(
                {
                    "run_index": index,
                    "relative_root": record["relative_root"],
                    "outcome": RUN_OUTCOME_COMPLETED,
                    "detail": (
                        f"chapter {chapter} committed; next_chapter="
                        f"{chapter + 1}"
                    ),
                    "durable_detail": "",
                    "new_provider_sends_started": proof["ledger"][
                        "new_provider_sends_started"
                    ],
                    "accepted_credit_equivalent_usd": proof["ledger"][
                        "accepted_credit_equivalent_usd"
                    ],
                    "next_chapter_artifacts": proof["next_chapter_artifacts"],
                }
            )
    except G7RExecutionPhaseError:
        raise
    except BaseException as exc:  # noqa: BLE001 - never fabricate zero
        # Any unexpected escape once the paid phase has begun must never be
        # reported as a provably credential-free / zero-Provider outcome.
        raise G7RExecutionPhaseError(
            "paid wave execution failed after credential access: "
            f"{type(exc).__name__}: {exc}",
            phase=EXECUTION_PHASE_POST_SECRET_EXECUTION,
            credential_access=True,
            credential_access_attempted=True,
            provider_calls=(0 if not provider_send_possible else None),
        ) from None
    finally:
        api_key = None
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass

    try:
        not_started = [
            index for index in starting_active if index not in executed
        ]

        # Accounting covers EVERY executed target-chapter ledger -- completed,
        # terminal, quarantined, and corrupt/unknown -- and is read only from the
        # durable G7F target ledgers, never inferred from stdout or transport
        # results.  A line whose target ledger is missing/corrupt after a possible
        # paid send is never fabricated as zero: it marks the wave accounting
        # incomplete and the exact aggregate totals unknown.
        outcome_by_index = {
            int(entry["run_index"]): str(entry["outcome"])
            for entry in per_run
        }
        actual_sends = 0
        accepted_total = Decimal(0)
        actual_sends_by_disposition: dict[str, int] = {}
        accepted_by_disposition: dict[str, Decimal] = {}
        accounting_lines: list[dict[str, Any]] = []
        accounting_unavailable: list[int] = []
        for index in executed:
            outcome = outcome_by_index.get(index, RUN_OUTCOME_LOCAL_FAILURE)
            line = _accounting_record_for_line(
                run_records[index]["run_root"],
                chapter,
                outcome,
                plan=plans_by_index.get(index),
            )
            line["run_index"] = index
            accounting_lines.append(line)
            if line["status"] == ACCOUNTING_UNAVAILABLE:
                accounting_unavailable.append(index)
                continue
            sends = int(line["new_provider_sends_started"])
            accepted = Decimal(line["accepted_credit_equivalent_usd"])
            actual_sends += sends
            accepted_total += accepted
            actual_sends_by_disposition[outcome] = (
                actual_sends_by_disposition.get(outcome, 0) + sends
            )
            accepted_by_disposition[outcome] = (
                accepted_by_disposition.get(outcome, Decimal(0)) + accepted
            )

        accounting_complete = not accounting_unavailable
        known_accounted_sends = actual_sends
        known_accounted_credit = _decimal_text(accepted_total)
        if accounting_complete:
            reported_sends: int | None = actual_sends
            reported_credit: str | None = known_accounted_credit
        else:
            reported_sends = None
            reported_credit = None

        remaining_for_target = [
            index
            for index in starting_active
            if index not in completed
            and index not in terminal
            and index not in quarantined
        ]

        after = derive_cohort(root, chapter)
        next_chapter = chapter + 1
        next_active: list[int] = []
        if next_chapter <= target_chapter_bounds()[1]:
            next_active = list(
                derive_cohort(root, next_chapter)["active_run_indices"]
            )

        all_completed = (
            stop_reason is None
            and not terminal
            and not quarantined
            and sorted(completed) == sorted(starting_active)
        )
        if stop_reason is not None:
            # wave_status already records the fail-closed stop condition.
            fail_closed = True
        elif terminal:
            # Every selected run was attempted and every one ended in a proven,
            # accounted deterministic terminal result (completed and/or terminal):
            # the wave completed its work.  Definitive durable model/policy
            # terminal outcomes never stop independent runs.
            wave_status = WAVE_COMPLETED_WITH_TERMINAL_RUNS
        elif all_completed:
            wave_status = WAVE_COMPLETED
        else:
            wave_status = WAVE_STOPPED_LOCAL_FAILURE
            fail_closed = True

        result_document: dict[str, Any] = {
            "status": wave_status,
            "schema_version": G7R_SCHEMA_VERSION,
            "revision": G7R_REVISION,
            "controller_sha256": G7R_CONTROLLER_SHA256,
            "bundle_sha256": expected_bundle_sha256,
            "fail_closed": fail_closed,
            "stop_reason": stop_reason,
            "target_chapter": chapter,
            "starting_active_set": starting_active,
            "per_run_results": per_run,
            "completed_set": completed,
            "newly_terminal_set": terminal,
            "newly_quarantined_set": quarantined,
            "not_started_after_stop_set": not_started,
            "remaining_active_set_for_target": remaining_for_target,
            "accounting_complete": accounting_complete,
            "accounting_unavailable_run_indices": accounting_unavailable,
            "actual_new_provider_sends": reported_sends,
            "accepted_credit_equivalent_usd": reported_credit,
            "known_accounted_new_provider_sends": known_accounted_sends,
            "known_accounted_credit_equivalent_usd": known_accounted_credit,
            "actual_new_provider_sends_by_disposition": {
                key: actual_sends_by_disposition[key]
                for key in sorted(actual_sends_by_disposition)
            },
            "accepted_credit_equivalent_usd_by_disposition": {
                key: _decimal_text(accepted_by_disposition[key])
                for key in sorted(accepted_by_disposition)
            },
            "actual_accounting_lines": accounting_lines,
            "original_ceilings": {
                "maximum_new_provider_sends_per_run": [
                    {
                        "run_index": record["run_index"],
                        "maximum_new_provider_sends": record[
                            "maximum_new_provider_sends"
                        ],
                        "maximum_credit_equivalent_usd": record[
                            "maximum_credit_equivalent_usd"
                        ],
                    }
                    for record in bundle["runs"]
                ],
                "aggregate_maximum_new_provider_sends": bundle["ceilings"][
                    "aggregate_maximum_new_provider_sends"
                ],
                "aggregate_maximum_credit_equivalent_usd": bundle["ceilings"][
                    "aggregate_maximum_credit_equivalent_usd"
                ],
            },
            "next_active_set": next_active,
            "next_chapter": next_chapter,
            "after_cohort_disposition_counts": after["disposition_counts"],
            "automatic_retry_performed": False,
            "automatic_next_chapter_execution": False,
            "authorization": {
                "authorize_paid_wave": True,
                "preflights_passed": sorted(preflights),
                "preflight_count": len(preflights),
                "secret_access_after_all_preflights": True,
            },
        }
    except G7RExecutionPhaseError:
        raise
    except BaseException as exc:  # noqa: BLE001 - never fabricate zero
        # A post-execution accounting/derivation failure may occur after a live
        # Provider send; the exact count is unknown, so it is never reported as
        # zero and credential access is reported truthfully.
        raise G7RExecutionPhaseError(
            "paid wave post-execution accounting failed: "
            f"{type(exc).__name__}: {exc}",
            phase=EXECUTION_PHASE_POST_SECRET_EXECUTION,
            credential_access=True,
            credential_access_attempted=True,
            provider_calls=(0 if not provider_send_possible else None),
        ) from None
    return result_document


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7r-stage1-wave-controller",
        description=(
            "Dynamic Stage-1 Chapters 3-24 wave controller over the frozen G7F "
            "paid executor"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status",
        help="zero-Provider cohort status for one target chapter",
    )
    status_parser.add_argument("--stage1-root", required=True)
    status_parser.add_argument(
        "--chapter", type=int, default=TARGET_FIRST_CHAPTER
    )

    plan_parser = subparsers.add_parser(
        "plan",
        help="zero-Provider wave bundle planning for one target chapter",
    )
    plan_parser.add_argument("--stage1-root", required=True)
    plan_parser.add_argument("--chapter", type=int, required=True)
    plan_parser.add_argument("--control-dir", required=True)
    plan_parser.add_argument(
        "--repository-head",
        default=None,
        help=(
            "expected repository HEAD assertion only; the actual checkout HEAD "
            "is always resolved and recorded, and a mismatch fails closed"
        ),
    )

    execute_parser = subparsers.add_parser(
        "execute",
        help="execute exactly one authorized target chapter",
    )
    execute_parser.add_argument("--stage1-root", required=True)
    execute_parser.add_argument("--bundle", required=True)
    execute_parser.add_argument("--expected-bundle-sha256", required=True)
    execute_parser.add_argument(
        "--authorize-paid-wave",
        action="store_true",
        help="explicitly authorize exactly one paid target chapter",
    )
    execute_parser.add_argument("--secret-path", default=None)
    execute_parser.add_argument("--code-root", default=None)
    execute_parser.add_argument("--progress-log", default=None)

    return parser


def _emit(document: Mapping[str, Any], *, exit_code: int) -> int:
    sys.stdout.write(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = build_status_report(
                stage1_root=args.stage1_root,
                target_chapter=args.chapter,
            )
            return _emit(result, exit_code=0)
        if args.command == "plan":
            bundle = plan_wave(
                stage1_root=args.stage1_root,
                target_chapter=args.chapter,
                control_dir=args.control_dir,
                repository_head=args.repository_head,
            )
            return _emit(
                {
                    **bundle,
                    "bundle_path": str(
                        wave_bundle_path(args.control_dir, args.chapter)
                    ),
                },
                exit_code=0,
            )
        if args.command == "execute":
            result = execute_wave(
                stage1_root=args.stage1_root,
                bundle_path=args.bundle,
                expected_bundle_sha256=args.expected_bundle_sha256,
                authorize_paid_wave=args.authorize_paid_wave,
                code_root=args.code_root,
                secret_path=args.secret_path,
                progress_log_path=args.progress_log,
            )
            return _emit(
                result, exit_code=1 if result.get("fail_closed") else 0
            )
        parser.error(f"unknown command: {args.command}")
        return 2
    except G7RExecutionPhaseError as exc:
        # A paid-phase failure carries its own truthful metadata: never a
        # fabricated zero Provider call and never a false credential-free claim.
        return _emit(
            {
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "phase": exc.phase,
                "provider_calls": exc.provider_calls,
                "credential_access": exc.credential_access,
                "credential_access_attempted": exc.credential_access_attempted,
            },
            exit_code=1,
        )
    except G7RControllerError as exc:
        # Every remaining controller error is raised before the paid boundary:
        # status/plan, unauthorized execute, and zero-Provider preflight failures
        # all provably made no Provider call and read no credential.
        return _emit(
            {
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "phase": EXECUTION_PHASE_PRE_CREDENTIAL,
                "provider_calls": 0,
                "credential_access": False,
                "credential_access_attempted": False,
            },
            exit_code=1,
        )


__all__ = [
    "ACCOUNTING_EXACT",
    "ACCOUNTING_KNOWN_STATUSES",
    "ACCOUNTING_UNAVAILABLE",
    "ACCOUNTING_ZERO_PROVEN_PRE_SEND",
    "BUNDLE_STATUS_READY",
    "DISPOSITION_CORRUPT",
    "DISPOSITION_HEALTHY_COMPLETED",
    "DISPOSITION_NOT_READY",
    "DISPOSITION_QUARANTINED",
    "DISPOSITION_READY_FOR_TARGET",
    "DISPOSITION_TERMINAL",
    "EXECUTION_PHASE_POST_SECRET_EXECUTION",
    "EXECUTION_PHASE_POST_SECRET_PRE_EXECUTION",
    "EXECUTION_PHASE_PRE_CREDENTIAL",
    "EXECUTION_PHASE_SECRET_ACCESS",
    "G7RBundleError",
    "G7RChapterBoundsError",
    "G7RExecutionPhaseError",
    "G7RPreflightError",
    "G7R_CONTROLLER_SHA256",
    "G7RControllerError",
    "G7R_REVISION",
    "G7R_SCHEMA_VERSION",
    "RUN_OUTCOME_COMPLETED",
    "RUN_OUTCOME_CORRUPT_STATE",
    "RUN_OUTCOME_LOCAL_FAILURE",
    "RUN_OUTCOME_QUARANTINED",
    "RUN_OUTCOME_TERMINAL",
    "QUARANTINED_RUN_OUTCOMES",
    "TARGET_FIRST_CHAPTER",
    "WAVE_COMPLETED",
    "WAVE_COMPLETED_WITH_TERMINAL_RUNS",
    "WAVE_STOPPED_AMBIGUOUS",
    "WAVE_STOPPED_LOCAL_FAILURE",
    "assert_valid_target_chapter",
    "audits_due_for_chapter",
    "build_parser",
    "build_status_report",
    "classify_after_execution",
    "compute_bundle_sha256",
    "derive_cohort",
    "derive_glossary_review_send_ceiling",
    "derive_run_disposition",
    "derive_run_envelope",
    "execute_wave",
    "g7r_controller_identity",
    "load_wave_bundle",
    "main",
    "periodic_audit_due",
    "plan_wave",
    "read_dpapi_secret",
    "resolve_repository_head",
    "scan_run_evidence",
    "semantic_bundle_payload",
    "target_chapter_bounds",
    "verify_wave_bundle",
    "wave_bundle_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
