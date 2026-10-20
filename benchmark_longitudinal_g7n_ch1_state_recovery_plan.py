"""G7N zero-provider Chapter-1 auxiliary state-recovery planner and verifier.

The real GOAT Run-1 Chapter-1 defect this module plans around is the auxiliary
mirror image of the G7J translation incident: the accepted ``state_extraction``
response was EMPTY with ``finish_reason == "length"`` at exactly the legacy
3,000-token ceiling (all 3,000 completion tokens reasoning, zero visible). No
auxiliary budget correction and no finish-reason classification existed, so the
empty content reached ``json.loads("")``, the local salvage helper returned
``None``, and the generic JSON repair ran on EMPTY input and produced fabricated
state that was committed. G7J now owns the mechanical fix (the corrected 4,000
GOAT auxiliary ceiling plus the fail-closed auxiliary classifier), and G7N owns
the recovery *plan* for the already-committed Chapter 1 under the versioned
8,000-token GOAT auxiliary ceiling.

The real G7M Chapter-1 recovery is now TERMINAL and is historical evidence: its
durable ledger and its ``response_recorded`` recovery journal are pinned by
canonical SHA-256 and byte-frozen forever. G7N therefore binds BOTH historical
layers: the original G7C/G7F Chapter-1 incident evidence and the terminal G7M
recovery evidence. It never replays, reinterprets, or re-materializes either.

What G7N does:

* reads the historical incident evidence strictly read-only;
* verifies every pinned historical fact and fails closed on any missing or
  modified byte (record SHA, response SHA, ledger hash, journal record hash);
* verifies the terminal G7M recovery evidence: canonical ledger/journal digests,
  ``response_recorded`` status, exactly one Provider send, 4,000 completion and
  4,000 reasoning tokens, empty visible content, ``finish_reason == "length"``,
  the fail-closed ``auxiliary_budget_exhausted_empty_content`` classification,
  no repair, and no apply;
* derives the corrected auxiliary ceiling from the additive 8K GOAT policy
  (4,000 observed pre-visible reasoning reserve + 4,000 visible structured-output
  allowance) instead of the frozen historical 4,000-token value, which it records
  as ``legacy_corrected_auxiliary_budget``;
* mints a DISTINCT recovery generation id that can never equal the previous
  ``G7J24K_RECOVERY_20260922_230426`` generation;
* publishes one recovery binding SHA-256 over the complete original incident,
  the terminal G7M recovery evidence, the pinned Chapter-1 output/QA bytes, the
  degraded committed state, and the corrected 8K auxiliary ceiling;
* uses its own operation identity and journal namespace so the consumed fixed
  ``g7c_state_extraction`` Chapter-1 slot is never reused;
* forbids a Chapter-1 retranslation/retransmit and any Chapter-2 advance;
* forbids the consumed paid authorization and its executor token as active
  authorization, and requires a fresh explicit operator authorization;
* describes an atomic, fail-closed apply contract without executing it;
* renders a summary.

What G7N deliberately does NOT do: it never sends a Provider request, never
constructs a Provider client, never reads a credential, never fabricates state,
never mutates the historical evidence, and never writes anywhere inside the
evidence root. The planner and verifier are strictly read-only with respect to
the evidence root; a plan may only be materialized by the operator outside it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import InvalidOperation, Decimal
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import benchmark_longitudinal_g7j_reasoning_budget as g7j
from benchmark_longitudinal_config import (
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TRANSLATION,
)
from noveltranslator_core import atomic_write_json


G7N_SCHEMA_VERSION = 1
G7N_REVISION = "goat-g7n-ch1-state-recovery-plan-v1"

PLAN_FILENAME = "G7N_CH1_STATE_RECOVERY_8K_PLAN.json"
PLAN_SUMMARY_LABEL = "G7N_CH1_STATE_RECOVERY"

TARGET_CHAPTER = 1
TARGET_MODEL_ID = "deepseek/deepseek-v4.1-flash"
TARGET_REASONING_EFFORT = "low"

# The frozen record of the incident, exactly as recorded by the operator logs.
COMMITTED_TRANSLATION_SHA256 = (
    "6FCE1FC5992B41A029BEC1AE358ACA03CE7037EE0FCE3881D7AFAC490417DBD2"
)
QA_SHA256 = (
    "4D7F65907258AFFD289A0AF544C1A34C16FC447CA03B4CA580A934C472573A32"
)
ORIGINAL_LEDGER_PLAN_SHA256 = (
    "f74b93f30b48e955be2f500e8ab6b604ccf0a183e2c848334c96dfc2b039bd90"
)
ORIGINAL_LEDGER_APPROVAL_TOKEN = (
    "0e392c30e0f5a9a1db99c30ec9f53adce01bdcf91db33a57e6dd6bfa7cf07a97"
)
ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED = 3
ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD = "0.012237714"

# The consumed recovery generation this plan must never reproduce.
PREVIOUS_RECOVERY_GENERATION_ID = "G7J24K_RECOVERY_20260922_230426"
RECOVERY_GENERATION_PREFIX = "G7N_CH1_STATE_RECOVERY_8K_"

# ---------------------------------------------------------------------------
# Corrected auxiliary ceiling: the additive, versioned GOAT-only 8K policy
# ---------------------------------------------------------------------------
#
# The 4,000-token G7L/G7M ceiling stays byte-frozen and historical. The terminal
# G7M recovery demonstrated a FULL 4,000-token pre-visible reasoning reserve at
# that ceiling, so the next evidence-derived auxiliary total is the observed
# pre-visible reserve plus the intended compact visible structured-output
# allowance:
#
#     4,000 observed pre-visible reasoning reserve
#   + 4,000 visible structured-output allowance
#   = 8,000 total completion tokens
#
# The derivation goes through the versioned policy helper so a drifting policy
# fails closed here instead of being silently accepted. The value is never a
# hardcoded literal in this module: it is whatever the 8K policy returns, and it
# is checked against that same policy below.
POLICY_REVISION = g7j.GOAT_AUXILIARY_8K_REVISION
LEGACY_CORRECTED_AUXILIARY_BUDGET = g7j.GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
CORRECTED_AUXILIARY_BUDGET = g7j.goat_auxiliary_8k_completion_budget(
    logical_operation="state_extraction",
    requested_tokens=(
        g7j.GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS
    ),
)

# G7N's OWN recovery operation identity and journal namespace. It is deliberately
# NOT a frozen PURE_RUN_LIFECYCLE_OPERATIONS member and it never reuses the
# consumed fixed ``g7c_state_extraction`` Chapter-1 slot.
RECOVERY_OPERATION_ID = "g7n_ch1_state_recovery_8k_repair"
FIXED_STATE_EXTRACTION_JOURNAL_KIND = "g7c_state_extraction"
FIXED_STATE_JSON_REPAIR_JOURNAL_KIND = "g7c_state_json_repair"
G7C_JOURNAL_KIND_PREFIX = "g7c_"
RECOVERED_LOGICAL_OPERATION = LIFECYCLE_OPERATION_STATE_EXTRACTION

# The G7F executor names an auxiliary journal FILE after its multi-request SLOT
# (``g7c_state_extraction`` plus an optional request-sha prefix) while the durable
# record INSIDE carries the LOGICAL operation name (``state_extraction``). The
# recovery boundary therefore reconciles the slot kind with the recorded
# ``operation_kind`` instead of comparing the two identical strings.
_FIXED_JOURNAL_OPERATION_KINDS = {
    FIXED_STATE_EXTRACTION_JOURNAL_KIND: RECOVERED_LOGICAL_OPERATION,
    FIXED_STATE_JSON_REPAIR_JOURNAL_KIND: LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
}

DEFAULT_MAX_SENDS = 2
DEFAULT_MAX_CE_USD = "0.05"

SHA256_EMPTY = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

# Evidence layout of one G7C Stage-1 run root (the real shapes the production
# lifecycle writes).
RUN_CONFIG_RELATIVE = "novel.json"
RUN_IDENTITY_RELATIVE = "g7c_run_identity.json"
PROGRESS_RELATIVE = "data/progress.json"
GLOSSARY_RELATIVE = "data/glossary.json"
CONTINUITY_RELATIVE = "data/continuity.json"
ISSUES_RELATIVE = "data/issues.json"
JOURNAL_DIR_RELATIVE = "data/provider_request_journal"
LEDGER_DIR_RELATIVE = "data/g7f_invocation_ledgers"
LEDGER_RELATIVE = f"{LEDGER_DIR_RELATIVE}/0001.json"
COMMITTED_CHAPTER_RELATIVE = "output/chapters/0001.md"
QA_RELATIVE = "output/qa/0001.json"

# ---------------------------------------------------------------------------
# Terminal G7M recovery evidence (SECOND historical layer, byte-frozen)
# ---------------------------------------------------------------------------
#
# The real G7M Chapter-1 recovery is TERMINAL. Its durable recovery ledger and its
# ``response_recorded`` recovery journal are historical evidence: they must never
# be replayed, deleted, overwritten, or reinterpreted. Both digests below are the
# RAW FILE-BYTE SHA-256 values captured from the real terminal artifacts at the
# moment G7M completed. The durable documents also carry their own independent
# canonical self-hashes in ``ledger_sha256`` / ``record_sha256``; G7N
# recomputes and verifies those separately. Keeping both identities prevents
# formatting/newline changes from being confused with semantic document identity.
G7M_TERMINAL_JOURNAL_KIND = "g7l_ch1_state_recovery_repair"
G7M_TERMINAL_LEDGER_RELATIVE = "data/g7m_recovery_ledgers/0001.json"
G7M_TERMINAL_JOURNAL_RELATIVE = (
    f"{JOURNAL_DIR_RELATIVE}/{TARGET_CHAPTER:04d}."
    f"{G7M_TERMINAL_JOURNAL_KIND}.json"
)
G7M_TERMINAL_LEDGER_SHA256 = (
    "7a451e6c55eb118a2356f20d141eac221b8cd77fb7862f435484d91dddccc3ed"
)
G7M_TERMINAL_JOURNAL_SHA256 = (
    "4346d822dcc1fce8c933aa1762b608dc46125b7e382e70b6fd1b3779390c7194"
)
G7M_TERMINAL_JOURNAL_STATUS = "response_recorded"
# The frozen durable paid-request-journal schema/revision the terminal G7M record
# was written under (``production_paid_request_journal``).
G7M_TERMINAL_JOURNAL_SCHEMA_VERSION = 1
G7M_TERMINAL_JOURNAL_REVISION = "paid-request-journal-v1"
G7M_TERMINAL_PROVIDER_SEND_COUNT = 1
G7M_TERMINAL_COMPLETION_TOKENS = 4_000
G7M_TERMINAL_REASONING_TOKENS = 4_000
G7M_TERMINAL_VISIBLE_CONTENT = ""
G7M_TERMINAL_FINISH_REASON = "length"
G7M_TERMINAL_CLASSIFICATION = "auxiliary_budget_exhausted_empty_content"
G7M_TERMINAL_REPAIR_PERFORMED = False
G7M_TERMINAL_APPLY_PERFORMED = False
# The durable self-hash field of each terminal document.
_LEDGER_SELF_HASH_KEY = "ledger_sha256"
_JOURNAL_SELF_HASH_KEY = "record_sha256"
_JOURNAL_RESPONSE_HASH_KEY = "response_sha256"

# Transport capability the recovery boundary must never contain. Every entry is
# assembled from fragments on purpose: this module asserts that its own source
# contains none of these symbols, and a literal would make that assertion vacuous.
_FORBIDDEN_TRANSPORT_SYMBOLS = (
    "send_chat_completion" + "_once",
    "create_provider" + "_client",
    "build_provider" + "_request",
    "benchmark_longitudinal_" + "provider",
    "open" + "ai",
    "req" + "uests",
    "htt" + "px",
    "api" + "_key",
    "Authorization" + ": Bearer",
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_JOURNAL_SLOT_SUFFIX_RE = re.compile(r"^[0-9a-f]{20}$")

# The only plan locations allowed to record the consumed executor authorizer: the
# authorization boundary's historical note, the historical observed evidence of
# the incident ledger, and the binding payload's historical ledger record. These
# are EXACT field paths; everywhere else the consumed authorizer's values must not
# appear at all.
_ALLOWED_CONSUMED_AUTHORIZATION_PATHS = (
    ("authorization", "historical_consumed_plan_sha256"),
    ("authorization", "historical_consumed_approval_token"),
    ("observed_evidence", "ledger_plan_sha256"),
    ("observed_evidence", "ledger_approval_token"),
    (
        "recovery_binding_payload",
        "original_ch1_ledger_record",
        "plan_sha256",
    ),
    (
        "recovery_binding_payload",
        "original_ch1_ledger_record",
        "approval_token",
    ),
)
# Recognition is by VALUE: the plan's own structural ``plan_sha256`` envelope key
# shares a name with the ledger's ``plan_sha256``, so a key-name scan would reject
# a legitimate plan. Scanning the consumed authorizer's literals is both stricter
# and unambiguous.
_CONSUMED_AUTHORIZATION_VALUES = (
    ORIGINAL_LEDGER_PLAN_SHA256,
    ORIGINAL_LEDGER_APPROVAL_TOKEN,
)


class G7NRecoveryError(RuntimeError):
    """Recovery evidence is missing, inconsistent, modified, or unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7NRecoveryError(
            "value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G7NRecoveryError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7NRecoveryError(f"cannot read {label}: {target}") from exc
    if not isinstance(value, dict):
        raise G7NRecoveryError(f"{label} must contain a JSON object: {target}")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7NRecoveryError(f"{label} must be a SHA-256 hex digest")
    return value


def _matches_pinned_sha256(observed: str, pinned: str) -> bool:
    """Compare two SHA-256 digests case-insensitively.

    The frozen incident record is written in uppercase hex while a computed
    digest is lowercase, so pinned literals are compared case-insensitively and
    the lowercase computed digest is what is re-derived everywhere else.
    """
    return observed.lower() == pinned.lower()


def _validate_generation_id(value: Any) -> str:
    if not isinstance(value, str) or not _GENERATION_RE.fullmatch(value):
        raise G7NRecoveryError(
            "recovery_generation_id must be 8..128 safe characters "
            "(letters, digits, dot, underscore, hyphen)"
        )
    if value == PREVIOUS_RECOVERY_GENERATION_ID:
        raise G7NRecoveryError(
            "recovery_generation_id must never reproduce the consumed previous "
            f"generation {PREVIOUS_RECOVERY_GENERATION_ID}"
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G7NRecoveryError(f"{label} must be a positive integer")
    return value


def _decimal_text(value: Any, label: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G7NRecoveryError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise G7NRecoveryError(f"{label} must be a nonnegative finite decimal")
    return format(parsed, "f")


def _path_is_within(path: Path, root: Path) -> bool:
    resolved = Path(path).resolve()
    root = Path(root).resolve()
    return resolved == root or root in resolved.parents


def assert_no_transport_symbols() -> None:
    """Fail closed unless this module contains no transport capability.

    Recovery planning and verification must be provably incapable of a Provider
    send: the module source is scanned for every transport symbol, SDK import,
    and bearer-credential marker. Any occurrence fails closed.
    """
    try:
        source = Path(__file__).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        raise G7NRecoveryError(
            f"cannot read the G7N module source: {exc}"
        ) from exc
    for symbol in _FORBIDDEN_TRANSPORT_SYMBOLS:
        if symbol in source:
            raise G7NRecoveryError(
                "the G7N recovery boundary must contain no transport symbol; "
                f"found {symbol!r}"
            )


# ---------------------------------------------------------------------------
# Frozen policy identity
# ---------------------------------------------------------------------------


def g7n_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider G7N recovery policy identity.

    The corrected ceiling is derived through the additive, versioned 8K GOAT
    auxiliary policy (4,000 observed pre-visible reasoning reserve + 4,000 visible
    structured-output allowance). The frozen historical 4,000-token ceiling is
    recorded alongside it as ``legacy_corrected_auxiliary_budget`` and the 8K
    policy revision is recorded as ``policy_revision``: nothing here reinterprets
    the historical G7L/G7M 4K calls.
    """
    corrected = g7j.goat_auxiliary_8k_completion_budget(
        logical_operation=RECOVERED_LOGICAL_OPERATION,
        requested_tokens=(
            g7j.GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS
        ),
    )
    production = g7j.auxiliary_completion_budget_for_identity(goat=False)
    if corrected != CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the corrected 8K auxiliary ceiling derivation changed"
        )
    if corrected != g7j.GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS:
        raise G7NRecoveryError(
            "the corrected 8K GOAT auxiliary ceiling changed"
        )
    if corrected == LEGACY_CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the corrected 8K ceiling must differ from the frozen historical "
            "4K ceiling"
        )
    if LEGACY_CORRECTED_AUXILIARY_BUDGET != (
        g7j.GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    ):
        raise G7NRecoveryError(
            "the frozen historical 4K auxiliary ceiling changed"
        )
    if POLICY_REVISION != g7j.GOAT_AUXILIARY_8K_REVISION:
        raise G7NRecoveryError("the 8K auxiliary policy revision changed")
    if g7j.HISTORICAL_AUXILIARY_CLASSIFICATION != (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    ):
        raise G7NRecoveryError(
            "the frozen auxiliary incident classification changed"
        )
    if G7M_TERMINAL_CLASSIFICATION != (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    ):
        raise G7NRecoveryError(
            "the terminal G7M recovery classification changed"
        )
    if G7M_TERMINAL_CLASSIFICATION not in g7j.AUXILIARY_FAIL_CLOSED_STATUSES:
        raise G7NRecoveryError(
            "the terminal G7M recovery classification must stay fail-closed"
        )
    if g7j.HISTORICAL_AUXILIARY_REPAIR_FED_EMPTY_INPUT is not True:
        raise G7NRecoveryError(
            "the frozen auxiliary incident no longer records empty repair input"
        )
    if g7j.HISTORICAL_AUXILIARY_MODEL_ID != TARGET_MODEL_ID:
        raise G7NRecoveryError(
            "the frozen auxiliary incident model changed"
        )
    if TARGET_CHAPTER != g7j.HISTORICAL_AUXILIARY_CHAPTER:
        raise G7NRecoveryError("the G7N target chapter changed")

    return {
        "schema_version": G7N_SCHEMA_VERSION,
        "revision": G7N_REVISION,
        "predecessor": {
            "g7j_policy_sha256": g7j.G7J_POLICY_SHA256,
            "g7j_revision": g7j.G7J_REVISION,
            "g7j_auxiliary_incident_classification": (
                g7j.HISTORICAL_AUXILIARY_CLASSIFICATION
            ),
        },
        "target": {
            "chapter": TARGET_CHAPTER,
            "model_id": TARGET_MODEL_ID,
            "reasoning_effort": TARGET_REASONING_EFFORT,
            "committed_translation_sha256": (
                COMMITTED_TRANSLATION_SHA256
            ),
            "qa_sha256": QA_SHA256,
        },
        "pinned_consumed_executor_authorization": {
            "plan_sha256": ORIGINAL_LEDGER_PLAN_SHA256,
            "approval_token": ORIGINAL_LEDGER_APPROVAL_TOKEN,
            "new_provider_sends_started": (
                ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
            ),
            "accepted_credit_equivalent_usd": (
                ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
            ),
            "reusable_as_active_authorization": False,
        },
        "auxiliary_budget_policy": {
            "corrected_goat_auxiliary_total": corrected,
            "legacy_corrected_auxiliary_budget": (
                LEGACY_CORRECTED_AUXILIARY_BUDGET
            ),
            "policy_revision": POLICY_REVISION,
            "derivation": {
                "observed_pre_visible_reasoning_reserve_tokens": (
                    g7j.GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS
                ),
                "visible_structured_output_allowance_tokens": (
                    g7j.GOAT_AUXILIARY_8K_VISIBLE_STRUCTURED_OUTPUT_ALLOWANCE_TOKENS
                ),
                "total_completion_tokens": corrected,
            },
            "goat_only": g7j.GOAT_AUXILIARY_8K_GOAT_ONLY,
            "production_auxiliary_identity_ceiling": production,
            "uniform_for_all_six_goat_models": True,
            "model_dependent": False,
            "effort_dependent": False,
            "empty_auxiliary_response_never_repaired": True,
            "fail_closed_statuses": sorted(
                g7j.AUXILIARY_FAIL_CLOSED_STATUSES
            ),
        },
        "terminal_g7m_recovery": {
            "ledger_relative": G7M_TERMINAL_LEDGER_RELATIVE,
            "ledger_sha256": G7M_TERMINAL_LEDGER_SHA256,
            "journal_relative": G7M_TERMINAL_JOURNAL_RELATIVE,
            "journal_kind": G7M_TERMINAL_JOURNAL_KIND,
            "journal_sha256": G7M_TERMINAL_JOURNAL_SHA256,
            "journal_status": G7M_TERMINAL_JOURNAL_STATUS,
            "provider_send_count": G7M_TERMINAL_PROVIDER_SEND_COUNT,
            "completion_tokens": G7M_TERMINAL_COMPLETION_TOKENS,
            "reasoning_tokens": G7M_TERMINAL_REASONING_TOKENS,
            "visible_content": G7M_TERMINAL_VISIBLE_CONTENT,
            "finish_reason": G7M_TERMINAL_FINISH_REASON,
            "classification": G7M_TERMINAL_CLASSIFICATION,
            "repair_performed": G7M_TERMINAL_REPAIR_PERFORMED,
            "apply_performed": G7M_TERMINAL_APPLY_PERFORMED,
            "historical_and_frozen": True,
            "replayable": False,
            "reusable": False,
        },
        "operation_namespace": {
            "operation_id": RECOVERY_OPERATION_ID,
            "journal_kind": RECOVERY_OPERATION_ID,
            "recovered_logical_operation": RECOVERED_LOGICAL_OPERATION,
            "fixed_chapter_1_state_extraction_slot": (
                FIXED_STATE_EXTRACTION_JOURNAL_KIND
            ),
            "fixed_slot_reused": False,
            "fixed_chapter_1_state_json_repair_slot": (
                FIXED_STATE_JSON_REPAIR_JOURNAL_KIND
            ),
            "fixed_slot_reused_by_g7n": False,
            "lifecycle_operation_slot_used": False,
            "lifecycle_glossary_review_slot_used": False,
        },
        "previous_recovery_generation_id": (
            PREVIOUS_RECOVERY_GENERATION_ID
        ),
        "generation_must_differ_from_previous": True,
        "scope": {
            "chapter_1_retranslation_allowed": False,
            "chapter_1_retransmit_allowed": False,
            "chapter_2_advance_allowed": False,
            "local_state_fabrication_allowed": False,
            "historical_evidence_mutation_allowed": False,
            "evidence_root_write_allowed": False,
            "apply_executed_by_g7n": False,
        },
        "authorization_boundary": {
            "planner_emits_plan_and_summary_only": True,
            "planner_emits_authorization": False,
            "fresh_explicit_operator_authorization_required": True,
            "consumed_authorization_reusable": False,
            "consumed_executor_token_reuse_forbidden": True,
        },
        "provider_boundary": {
            "provider_calls": 0,
            "provider_client_constructed": False,
            "credential_access": False,
            "chapter_execution": False,
            "retry_authorized": False,
            "transport_symbols_in_module": 0,
        },
    }


G7N_POLICY_SHA256 = _sha256_json(g7n_policy_identity())


# ---------------------------------------------------------------------------
# Read-only historical incident verification
# ---------------------------------------------------------------------------


def _run_root(evidence_root: Path | str, run_id: str) -> Path:
    """Resolve one run root below the evidence root, refusing any escape.

    ``run_id`` is the G7C run-root relative identifier (for example
    ``runs/01_<model>__<novel>__<sha12>``) or a plain directory name. Absolute
    paths, drive qualifiers, empty components, and ``..`` are rejected, and the
    resolved run root must stay inside the evidence root.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise G7NRecoveryError("run_id must be a non-empty string")
    normalized = run_id.strip().replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        raise G7NRecoveryError("run_id must be a relative run-root identifier")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise G7NRecoveryError(
            "run_id must not contain empty or parent path components"
        )
    root = Path(evidence_root).resolve()
    if not root.is_dir():
        raise G7NRecoveryError(f"evidence root missing: {root}")
    run_root = (root / Path(*parts)).resolve()
    if not _path_is_within(run_root, root):
        raise G7NRecoveryError("the run root escapes the evidence root")
    if not run_root.is_dir():
        raise G7NRecoveryError(f"run root missing: {run_root}")
    return run_root


def _verify_record_hash(record: Mapping[str, Any], label: str) -> None:
    value = dict(record)
    stored = value.pop("record_sha256", None)
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7NRecoveryError(f"{label} record_sha256 is invalid")
    if _sha256_json(value) != stored:
        raise G7NRecoveryError(f"{label} record_sha256 does not recompute")


def _verify_ledger_hash(document: Mapping[str, Any]) -> str:
    value = dict(document)
    stored = value.pop("ledger_sha256", None)
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7NRecoveryError("historical ledger_sha256 is invalid")
    if _sha256_json(value) != stored:
        raise G7NRecoveryError(
            "historical ledger_sha256 does not recompute"
        )
    return stored


def _canonical_document_sha256(
    document: Mapping[str, Any],
    self_hash_key: str,
) -> str:
    """Return the CANONICAL SHA-256 of one durable evidence document.

    The canonical digest is the canonical-JSON digest of the document with its own
    durable self-hash field excluded, which is exactly the value the durable writer
    stores in that field (``ledger_sha256`` for a recovery ledger, ``record_sha256``
    for a paid request journal record). Being canonical, it is independent of file
    formatting, so the historical evidence can be pinned without depending on
    whitespace or key order.
    """
    if not isinstance(document, Mapping):
        raise G7NRecoveryError(
            "a durable evidence document must be a JSON object"
        )
    without_self_hash = {
        key: item for key, item in document.items() if key != self_hash_key
    }
    return _sha256_json(without_self_hash)


def _discover_auxiliary_journal(
    run_root: Path,
    fixed_kind: str,
) -> Path:
    """Return the one journal file of an auxiliary chapter operation.

    The G7C executor writes auxiliary operations into a multi-request slot named
    ``NNNN.<fixed_kind>_<request_sha256_prefix>``; the fixed
    ``NNNN.<fixed_kind>.json`` name is also accepted. Recognition is by exact
    fixed-kind prefix plus an exactly-20-hex-digit slot suffix. Zero or more than
    one match fails closed rather than guessing.
    """
    directory = run_root / JOURNAL_DIR_RELATIVE
    if not directory.is_dir():
        raise G7NRecoveryError(
            f"historical paid request journal directory missing: {directory}"
        )
    prefix = f"{TARGET_CHAPTER:04d}.{fixed_kind}"
    accepted: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise G7NRecoveryError(
            f"cannot read historical journal directory {directory}: {exc}"
        ) from exc
    for path in entries:
        if not path.is_file() or path.suffix != ".json":
            continue
        stem = path.name[: -len(".json")]
        if not stem.startswith(prefix):
            continue
        suffix = stem[len(prefix) :]
        if suffix == "":
            accepted.append(path)
            continue
        if suffix.startswith("_") and _JOURNAL_SLOT_SUFFIX_RE.fullmatch(
            suffix[1:]
        ):
            accepted.append(path)

    if len(accepted) != 1:
        raise G7NRecoveryError(
            f"historical Chapter-{TARGET_CHAPTER} {fixed_kind} journal must be "
            f"exactly one file; found {len(accepted)}"
        )
    return accepted[0]


def _verify_journal(
    path: Path,
    fixed_kind: str,
    label: str,
) -> dict[str, Any]:
    """Verify one fixed auxiliary journal record against its durable facts.

    ``fixed_kind`` is the journal-file SLOT kind (``g7c_state_extraction``); the
    recorded ``operation_kind`` is the LOGICAL operation name
    (``state_extraction``), exactly as the G7F executor writes it. An unknown slot
    kind fails closed rather than being compared as-is.
    """
    expected_operation_kind = _FIXED_JOURNAL_OPERATION_KINDS.get(fixed_kind)
    if expected_operation_kind is None:
        raise G7NRecoveryError(
            f"unknown historical fixed journal slot kind: {fixed_kind}"
        )
    record = _read_object(path, label)
    _verify_record_hash(record, label)
    if record.get("operation_kind") != expected_operation_kind:
        raise G7NRecoveryError(f"{label} operation kind changed")
    if record.get("chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError(f"{label} chapter changed")
    if record.get("model") != TARGET_MODEL_ID:
        raise G7NRecoveryError(f"{label} model changed")
    if record.get("status") != "response_recorded":
        raise G7NRecoveryError(f"{label} is no longer response_recorded")
    if record.get("updated_utc") in (None, ""):
        raise G7NRecoveryError(f"{label} lost its durable timestamps")
    response = record.get("response")
    if not isinstance(response, Mapping):
        raise G7NRecoveryError(f"{label} durable response is missing")
    response_hash = record.get("response_sha256")
    if not isinstance(response_hash, str) or _sha256_json(
        dict(response)
    ) != response_hash:
        raise G7NRecoveryError(
            f"{label} response SHA-256 does not recompute"
        )
    return record


def verify_historical_incident(
    evidence_root: Path | str,
    run_id: str,
) -> dict[str, Any]:
    """Verify the exact historical incident without mutating anything.

    Every pinned fact is re-derived from bytes and every record/response hash is
    recomputed. Any missing, ambiguous, or modified byte fails closed. This
    function performs no write anywhere.
    """
    root = Path(evidence_root).resolve()
    run_root = _run_root(root, run_id)

    config = _read_object(
        run_root / RUN_CONFIG_RELATIVE,
        "historical novel config",
    )
    novel_id = config.get("id")
    if not isinstance(novel_id, str) or not novel_id.strip():
        raise G7NRecoveryError("historical novel config lacks a novel id")
    source_type = config.get("source_type", "epub")
    if not isinstance(source_type, str) or not source_type.strip():
        raise G7NRecoveryError("historical novel config source type is invalid")
    if "/" in source_type or "\\" in source_type or ".." in source_type:
        raise G7NRecoveryError("historical novel config source type is unsafe")
    source_path = run_root / "source" / f"novel.{source_type}"
    if not source_path.is_file():
        raise G7NRecoveryError(
            f"historical staged source missing: {source_path}"
        )
    source_sha256 = _sha256_file(source_path)

    # The G7C Stage-1 run identity is a durable production artifact of every run
    # root, so a missing identity fails closed instead of being skipped.
    identity_path = run_root / RUN_IDENTITY_RELATIVE
    if not identity_path.is_file():
        raise G7NRecoveryError(
            f"historical run identity missing: {identity_path}"
        )
    run_identity = _read_object(identity_path, "historical run identity")
    observed = dict(run_identity)
    stored = observed.pop("identity_sha256", None)
    if _sha256_json(observed) != stored:
        raise G7NRecoveryError(
            "historical run identity SHA does not recompute"
        )
    if run_identity.get("novel_id") != novel_id:
        raise G7NRecoveryError(
            "historical run identity novel id differs from the config"
        )
    if run_identity.get("source_sha256") != source_sha256:
        raise G7NRecoveryError(
            "historical staged source bytes differ from the run identity"
        )
    if run_identity.get("model_id") != TARGET_MODEL_ID:
        raise G7NRecoveryError(
            "historical run identity model changed"
        )

    progress_path = run_root / PROGRESS_RELATIVE
    progress = _read_object(progress_path, "historical progress")
    if progress.get("completed") != [TARGET_CHAPTER]:
        raise G7NRecoveryError(
            "historical progress completed chapters changed"
        )
    if progress.get("last_completed") != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "historical progress last_completed changed"
        )
    if progress.get("next_chapter") != TARGET_CHAPTER + 1:
        raise G7NRecoveryError(
            "historical progress next_chapter changed"
        )
    progress_identity = {
        "completed": [TARGET_CHAPTER],
        "last_completed": TARGET_CHAPTER,
        "next_chapter": TARGET_CHAPTER + 1,
    }

    glossary_path = run_root / GLOSSARY_RELATIVE
    glossary = _read_object(glossary_path, "historical glossary")
    glossary_entries = glossary.get("entries", {})
    if not isinstance(glossary_entries, Mapping):
        raise G7NRecoveryError("historical glossary entries are malformed")
    if glossary_entries:
        raise G7NRecoveryError(
            "the degraded incident state must commit no glossary entries"
        )
    if set(glossary) - {"entries"}:
        raise G7NRecoveryError(
            "historical glossary has an unexpected degraded shape"
        )

    continuity_path = run_root / CONTINUITY_RELATIVE
    continuity = _read_object(continuity_path, "historical continuity")
    continuity_summary = continuity.get("summary")
    if not isinstance(continuity_summary, str):
        raise G7NRecoveryError(
            "historical degraded continuity summary must be a string"
        )
    latest_chapter = continuity.get("latest_chapter", TARGET_CHAPTER)
    if latest_chapter != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "historical degraded continuity chapter changed"
        )
    if set(continuity) - {"latest_chapter", "summary"}:
        raise G7NRecoveryError(
            "historical continuity has an unexpected degraded shape"
        )

    issues_path = run_root / ISSUES_RELATIVE
    issues_document = _read_object(issues_path, "historical issues")
    issues = issues_document.get("issues")
    if not isinstance(issues, list):
        raise G7NRecoveryError(
            "historical degraded issues must be a list"
        )
    if set(issues_document) - {"issues"}:
        raise G7NRecoveryError(
            "historical issues document has an unexpected degraded shape"
        )

    committed_path = run_root / COMMITTED_CHAPTER_RELATIVE
    if not committed_path.is_file():
        raise G7NRecoveryError(
            f"historical committed Chapter-1 file missing: {committed_path}"
        )
    committed_sha256 = _sha256_file(committed_path)
    if not _matches_pinned_sha256(
        committed_sha256,
        COMMITTED_TRANSLATION_SHA256,
    ):
        raise G7NRecoveryError(
            "historical committed Chapter-1 translation bytes changed"
        )

    qa_path = run_root / QA_RELATIVE
    if not qa_path.is_file():
        raise G7NRecoveryError(f"historical Chapter-1 QA file missing: {qa_path}")
    qa_sha256 = _sha256_file(qa_path)
    if not _matches_pinned_sha256(qa_sha256, QA_SHA256):
        raise G7NRecoveryError("historical Chapter-1 QA bytes changed")
    qa_document = _read_object(qa_path, "historical Chapter-1 QA")
    if qa_document.get("chapter", TARGET_CHAPTER) != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "historical Chapter-1 QA names another chapter"
        )

    extraction_path = _discover_auxiliary_journal(
        run_root,
        FIXED_STATE_EXTRACTION_JOURNAL_KIND,
    )
    extraction = _verify_journal(
        extraction_path,
        FIXED_STATE_EXTRACTION_JOURNAL_KIND,
        "historical state_extraction journal",
    )
    extraction_response = dict(extraction["response"])
    if extraction_response.get("content") != "":
        raise G7NRecoveryError(
            "historical state_extraction visible content is no longer empty"
        )
    if extraction_response.get("finish_reason") != g7j.FINISH_REASON_LENGTH:
        raise G7NRecoveryError(
            "historical state_extraction finish_reason changed"
        )
    extraction_stats = extraction_response.get("stats")
    if not isinstance(extraction_stats, Mapping):
        raise G7NRecoveryError(
            "historical state_extraction response lacks accounting stats"
        )
    extraction_completion_tokens = extraction_stats.get("completion_tokens")
    extraction_classification = g7j.classify_auxiliary_response(
        logical_operation=RECOVERED_LOGICAL_OPERATION,
        content=extraction_response["content"],
        finish_reason=extraction_response["finish_reason"],
        completion_tokens=extraction_completion_tokens,
        max_completion_tokens=(
            g7j.HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
        ),
    )
    if extraction_classification["status"] != (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    ):
        raise G7NRecoveryError(
            "historical state_extraction no longer classifies as the frozen "
            "auxiliary budget-exhausted incident"
        )
    if extraction_classification["fail_closed"] is not True:
        raise G7NRecoveryError(
            "historical state_extraction incident must be fail-closed"
        )

    repair_path = _discover_auxiliary_journal(
        run_root,
        FIXED_STATE_JSON_REPAIR_JOURNAL_KIND,
    )
    repair = _verify_journal(
        repair_path,
        FIXED_STATE_JSON_REPAIR_JOURNAL_KIND,
        "historical state_json_repair journal",
    )
    repair_translation_sha256 = repair.get("translation_sha256")
    if repair_translation_sha256 is None:
        # Tolerate a nested identity mapping as well as the flattened production
        # record shape; either way the repair input must have been empty.
        nested = repair.get("identity")
        if isinstance(nested, Mapping):
            repair_translation_sha256 = nested.get("translation_sha256")
    if repair_translation_sha256 not in ("", SHA256_EMPTY):
        raise G7NRecoveryError(
            "the historical JSON repair was not fed empty input; the frozen "
            "incident record requires an empty repair input"
        )
    repair_response = dict(repair["response"])
    repair_content = repair_response.get("content")
    if not isinstance(repair_content, str) or not repair_content.strip():
        raise G7NRecoveryError(
            "historical JSON repair returned no fabricated state"
        )
    try:
        fabricated_state = json.loads(repair_content)
    except json.JSONDecodeError as exc:
        raise G7NRecoveryError(
            "historical JSON repair output is not the committed fabricated state"
        ) from exc
    if not isinstance(fabricated_state, dict):
        raise G7NRecoveryError(
            "historical JSON repair output is not an object"
        )

    ledger_path = run_root / LEDGER_RELATIVE
    ledger = _read_object(ledger_path, "historical G7F ledger")
    ledger_sha256 = _verify_ledger_hash(ledger)
    if ledger.get("plan_sha256") != ORIGINAL_LEDGER_PLAN_SHA256:
        raise G7NRecoveryError("historical ledger plan SHA changed")
    if ledger.get("approval_token") != ORIGINAL_LEDGER_APPROVAL_TOKEN:
        raise G7NRecoveryError("historical consumed approval token changed")
    if ledger.get("new_provider_sends_started") != (
        ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
    ):
        raise G7NRecoveryError("historical ledger send count changed")
    if ledger.get("accepted_credit_equivalent_usd") != (
        ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
    ):
        raise G7NRecoveryError("historical accepted credit cost changed")
    if ledger.get("target_chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError("historical ledger target chapter changed")
    if ledger.get("model_id") != TARGET_MODEL_ID:
        raise G7NRecoveryError("historical ledger model changed")
    if ledger.get("novel_id") != novel_id:
        raise G7NRecoveryError("historical ledger novel changed")
    records = ledger.get("records")
    if not isinstance(records, list) or len(records) != (
        ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
    ):
        raise G7NRecoveryError(
            "historical ledger must hold exactly the three consumed sends"
        )
    operations: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise G7NRecoveryError("historical ledger send record is malformed")
        if record.get("status") != "accepted":
            raise G7NRecoveryError(
                "historical ledger send is no longer accepted"
            )
        if record.get("chapter") != TARGET_CHAPTER:
            raise G7NRecoveryError(
                "historical ledger send chapter changed"
            )
        operations.append(str(record.get("logical_operation")))
    if sorted(operations) != [
        FIXED_STATE_EXTRACTION_JOURNAL_KIND.removeprefix(G7C_JOURNAL_KIND_PREFIX),
        FIXED_STATE_JSON_REPAIR_JOURNAL_KIND.removeprefix(
            G7C_JOURNAL_KIND_PREFIX
        ),
        LIFECYCLE_OPERATION_TRANSLATION,
    ]:
        raise G7NRecoveryError(
            "historical Chapter-1 ledger operations changed"
        )

    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "novel_id": novel_id,
        "model_id": TARGET_MODEL_ID,
        "chapter": TARGET_CHAPTER,
        "source_sha256": source_sha256,
        "source_relative": source_path.relative_to(run_root).as_posix(),
        "committed_translation_sha256": committed_sha256,
        "qa_sha256": qa_sha256,
        "progress": progress_identity,
        "progress_file_sha256": _sha256_file(progress_path),
        "state_extraction_journal_relative": (
            extraction_path.relative_to(run_root).as_posix()
        ),
        "state_extraction_journal_file_sha256": _sha256_file(extraction_path),
        "state_extraction_journal_record_sha256": extraction["record_sha256"],
        "state_extraction_response_sha256": extraction.get("response_sha256"),
        "state_extraction_request_sha256": extraction.get("request_sha256"),
        "state_extraction_completion_tokens": (
            extraction_completion_tokens
        ),
        "state_extraction_finish_reason": (
            extraction_response["finish_reason"]
        ),
        "state_extraction_classification": (
            extraction_classification["status"]
        ),
        "state_json_repair_journal_relative": (
            repair_path.relative_to(run_root).as_posix()
        ),
        "state_json_repair_journal_file_sha256": _sha256_file(repair_path),
        "state_json_repair_journal_record_sha256": repair["record_sha256"],
        "state_json_repair_response_sha256": repair.get("response_sha256"),
        "state_json_repair_request_sha256": repair.get("request_sha256"),
        "state_json_repair_received_empty_input": True,
        "state_json_repair_fabricated_state_sha256": _sha256_json(
            fabricated_state
        ),
        "ledger_relative": LEDGER_RELATIVE,
        "ledger_file_sha256": _sha256_file(ledger_path),
        "ledger_sha256": ledger_sha256,
        "ledger_plan_sha256": ORIGINAL_LEDGER_PLAN_SHA256,
        "ledger_approval_token": ORIGINAL_LEDGER_APPROVAL_TOKEN,
        "new_provider_sends_started": (
            ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
        ),
        "accepted_credit_equivalent_usd": (
            ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
        ),
        "degraded_state": {
            "glossary": {},
            "glossary_entries": 0,
            "glossary_file_sha256": _sha256_file(glossary_path),
            "continuity_summary": continuity_summary,
            "continuity_file_sha256": _sha256_file(continuity_path),
            "issues": list(issues),
            "issues_file_sha256": _sha256_file(issues_path),
        },
        "evidence_root": str(root),
        "evidence_root_modified": False,
    }


def verify_terminal_recovery_incident(
    evidence_root: Path | str,
    run_id: str,
    *,
    expected_ledger_sha256: str = G7M_TERMINAL_LEDGER_SHA256,
    expected_journal_sha256: str = G7M_TERMINAL_JOURNAL_SHA256,
) -> dict[str, Any]:
    """Verify the TERMINAL G7M Chapter-1 recovery evidence read-only.

    The G7M recovery is historical and byte-frozen. This boundary first pins the
    RAW FILE-BYTE SHA-256 of the durable G7M recovery ledger
    (``data/g7m_recovery_ledgers/0001.json`` below the run root) and of the durable
    terminal recovery journal (the ``recorded`` paid-request-journal record whose
    ``operation_kind`` is ``g7l_ch1_state_recovery_repair``) against the captured
    historical digests. It independently recomputes each document's canonical
    self-hash (``ledger_sha256`` / ``record_sha256``) and then proves the
    terminal facts: exactly one Provider send, 4,000 completion and 4,000 reasoning
    tokens, zero visible content, ``finish_reason == "length"``, the fail-closed
    ``auxiliary_budget_exhausted_empty_content`` classification, no repair, and no
    apply.

    Every mismatch fails closed. Nothing here is written: the evidence root is
    opened strictly read-only, no Provider client is constructed, and no credential
    is read. The no-apply fact is enforced structurally (a fail-closed, never
    repaired response can never satisfy the executor's apply preconditions) and is
    independently corroborated by :func:`verify_historical_incident`, which proves
    the committed Chapter-1 state is still the degraded pre-recovery state.
    """
    expected_ledger = _require_sha256(
        expected_ledger_sha256, "expected terminal G7M ledger SHA-256"
    )
    expected_journal = _require_sha256(
        expected_journal_sha256, "expected terminal G7M journal SHA-256"
    )
    root = Path(evidence_root).resolve()
    run_root = _run_root(root, run_id)

    ledger_path = (run_root / G7M_TERMINAL_LEDGER_RELATIVE).resolve()
    if not _path_is_within(ledger_path, run_root):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger escapes the run root"
        )
    if not ledger_path.is_file():
        raise G7NRecoveryError(
            f"terminal G7M recovery ledger missing: {ledger_path}"
        )
    ledger = _read_object(ledger_path, "terminal G7M recovery ledger")

    declared_ledger = ledger.get(_LEDGER_SELF_HASH_KEY)
    if not isinstance(declared_ledger, str) or not _SHA256_RE.fullmatch(
        declared_ledger
    ):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger self-hash is invalid"
        )
    observed_ledger_file = _sha256_file(ledger_path)
    if not _matches_pinned_sha256(observed_ledger_file, expected_ledger):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger file SHA-256 does not match "
            "the pinned historical digest"
        )
    observed_ledger = _canonical_document_sha256(
        ledger, _LEDGER_SELF_HASH_KEY
    )
    if observed_ledger != declared_ledger.lower():
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger self-hash does not recompute"
        )
    if ledger.get("logical_operation") != RECOVERED_LOGICAL_OPERATION:
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger binds another logical operation"
        )
    if ledger.get("model_id") != TARGET_MODEL_ID:
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger binds another model"
        )
    if ledger.get("target_chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger binds another chapter"
        )

    used_sends = ledger.get("new_provider_sends_started")
    if isinstance(used_sends, bool) or not isinstance(used_sends, int):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger send count is invalid"
        )
    if used_sends != G7M_TERMINAL_PROVIDER_SEND_COUNT:
        raise G7NRecoveryError(
            "the terminal G7M recovery must have started exactly "
            f"{G7M_TERMINAL_PROVIDER_SEND_COUNT} Provider send(s)"
        )
    ledger_maximum = ledger.get("maximum_new_provider_sends")
    if (
        isinstance(ledger_maximum, bool)
        or not isinstance(ledger_maximum, int)
        or ledger_maximum < 1
        or ledger_maximum > DEFAULT_MAX_SENDS
    ):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger send ceiling is invalid"
        )
    accepted_cost = Decimal(
        _decimal_text(
            ledger.get("accepted_credit_equivalent_usd"),
            "terminal G7M ledger accepted cost",
        )
    )
    if accepted_cost > Decimal(DEFAULT_MAX_CE_USD):
        raise G7NRecoveryError(
            "the terminal G7M recovery credit cost exceeds "
            f"{DEFAULT_MAX_CE_USD}"
        )

    records = ledger.get("records")
    if not isinstance(records, list):
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger records must be a list"
        )
    if len(records) != used_sends:
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger records do not match its send count"
        )
    repair_sends = 0
    for record in records:
        if not isinstance(record, Mapping):
            raise G7NRecoveryError(
                "a terminal G7M recovery send record is malformed"
            )
        _require_sha256(
            record.get("request_sha256"),
            "terminal G7M recovery send request_sha256",
        )
        operation = record.get("logical_operation")
        if operation == RECOVERED_LOGICAL_OPERATION:
            continue
        if operation == LIFECYCLE_OPERATION_STATE_JSON_REPAIR:
            repair_sends += 1
            continue
        raise G7NRecoveryError(
            "the terminal G7M recovery ledger holds a send outside the "
            "recovery envelope"
        )
    if repair_sends != 0:
        raise G7NRecoveryError(
            "the terminal G7M recovery must never have used a JSON repair send"
        )

    journal_path = (run_root / G7M_TERMINAL_JOURNAL_RELATIVE).resolve()
    if not _path_is_within(journal_path, run_root):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal escapes the run root"
        )
    if not journal_path.is_file():
        raise G7NRecoveryError(
            f"terminal G7M recovery journal missing: {journal_path}"
        )
    journal = _read_object(journal_path, "terminal G7M recovery journal")

    declared_journal = journal.get(_JOURNAL_SELF_HASH_KEY)
    if not isinstance(declared_journal, str) or not _SHA256_RE.fullmatch(
        declared_journal
    ):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal self-hash is invalid"
        )
    observed_journal_file = _sha256_file(journal_path)
    if not _matches_pinned_sha256(observed_journal_file, expected_journal):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal file SHA-256 does not match "
            "the pinned historical digest"
        )
    observed_journal = _canonical_document_sha256(
        journal, _JOURNAL_SELF_HASH_KEY
    )
    if observed_journal != declared_journal.lower():
        raise G7NRecoveryError(
            "the terminal G7M recovery journal self-hash does not recompute"
        )
    if journal.get("schema_version") != G7M_TERMINAL_JOURNAL_SCHEMA_VERSION:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal schema changed"
        )
    if journal.get("revision") != G7M_TERMINAL_JOURNAL_REVISION:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal revision changed"
        )
    if journal.get("operation_kind") != G7M_TERMINAL_JOURNAL_KIND:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal kind changed"
        )
    if journal.get("chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal binds another chapter"
        )
    if journal.get("model") != TARGET_MODEL_ID:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal binds another model"
        )
    if journal.get("status") != G7M_TERMINAL_JOURNAL_STATUS:
        raise G7NRecoveryError(
            "the terminal G7M recovery journal is no longer "
            f"{G7M_TERMINAL_JOURNAL_STATUS}"
        )
    if journal.get("error") not in ({}, None):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal carries an error record"
        )

    response = journal.get("response")
    if not isinstance(response, Mapping):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal has no durable response"
        )
    declared_response = journal.get(_JOURNAL_RESPONSE_HASH_KEY)
    if (
        not isinstance(declared_response, str)
        or not _SHA256_RE.fullmatch(declared_response)
        or _sha256_json(dict(response)) != declared_response.lower()
    ):
        raise G7NRecoveryError(
            "the terminal G7M recovery journal response hash does not recompute"
        )
    if response.get("logical_operation") != RECOVERED_LOGICAL_OPERATION:
        raise G7NRecoveryError(
            "the terminal G7M recovery response binds another logical operation"
        )
    if response.get("journal_kind") != G7M_TERMINAL_JOURNAL_KIND:
        raise G7NRecoveryError(
            "the terminal G7M recovery response binds another journal kind"
        )
    visible_content = response.get("content")
    if visible_content != G7M_TERMINAL_VISIBLE_CONTENT:
        raise G7NRecoveryError(
            "the terminal G7M recovery response must have empty visible content"
        )
    if response.get("finish_reason") != G7M_TERMINAL_FINISH_REASON:
        raise G7NRecoveryError(
            "the terminal G7M recovery response finish reason changed"
        )
    stats = response.get("stats")
    if not isinstance(stats, Mapping):
        raise G7NRecoveryError(
            "the terminal G7M recovery response lacks accounting stats"
        )
    if stats.get("completion_tokens") != G7M_TERMINAL_COMPLETION_TOKENS:
        raise G7NRecoveryError(
            "the terminal G7M recovery completion token count changed"
        )
    if stats.get("reasoning_tokens") != G7M_TERMINAL_REASONING_TOKENS:
        raise G7NRecoveryError(
            "the terminal G7M recovery reasoning token count changed"
        )
    try:
        classification = g7j.classify_auxiliary_response(
            logical_operation=RECOVERED_LOGICAL_OPERATION,
            content=visible_content,
            finish_reason=response.get("finish_reason"),
            completion_tokens=stats.get("completion_tokens"),
            max_completion_tokens=G7M_TERMINAL_COMPLETION_TOKENS,
            reasoning_tokens=stats.get("reasoning_tokens"),
        )
    except g7j.G7JPolicyError as exc:
        raise G7NRecoveryError(
            f"the terminal G7M recovery response is unclassifiable: {exc}"
        ) from exc
    classification_status = classification["status"]
    if classification_status != G7M_TERMINAL_CLASSIFICATION:
        raise G7NRecoveryError(
            "the terminal G7M recovery classification changed"
        )
    if classification_status not in g7j.AUXILIARY_FAIL_CLOSED_STATUSES:
        raise G7NRecoveryError(
            "the terminal G7M recovery classification must stay fail-closed"
        )
    if classification_status == g7j.AUXILIARY_STATE_ACCEPTED:
        raise G7NRecoveryError(
            "the terminal G7M recovery must never have produced accepted state"
        )

    journal_directory = run_root / JOURNAL_DIR_RELATIVE
    repair_journal_prefix = f"{TARGET_CHAPTER:04d}.{G7M_TERMINAL_JOURNAL_KIND}_"
    try:
        repair_candidates = [
            entry.name
            for entry in sorted(
                journal_directory.iterdir(), key=lambda item: item.name
            )
            if entry.is_file()
            and entry.name.endswith(".json")
            and entry.name.startswith(repair_journal_prefix)
        ]
    except OSError as exc:
        raise G7NRecoveryError(
            f"cannot read the terminal recovery journal directory: {exc}"
        ) from exc
    if repair_candidates:
        raise G7NRecoveryError(
            "the terminal G7M recovery must never have written a repair journal: "
            f"{repair_candidates}"
        )

    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "ledger_relative": G7M_TERMINAL_LEDGER_RELATIVE,
        "ledger_file_sha256": observed_ledger_file,
        "ledger_sha256": observed_ledger,
        "ledger_declared_sha256": declared_ledger.lower(),
        "ledger_relative_checked": str(
            ledger_path.relative_to(run_root).as_posix()
        ),
        "journal_relative": G7M_TERMINAL_JOURNAL_RELATIVE,
        "journal_file_sha256": observed_journal_file,
        "journal_sha256": observed_journal,
        "journal_declared_sha256": declared_journal.lower(),
        "journal_kind": G7M_TERMINAL_JOURNAL_KIND,
        "journal_status": G7M_TERMINAL_JOURNAL_STATUS,
        "journal_response_sha256": declared_response.lower(),
        "provider_send_count": used_sends,
        "repair_send_count": repair_sends,
        "completion_tokens": stats.get("completion_tokens"),
        "reasoning_tokens": stats.get("reasoning_tokens"),
        "visible_content": visible_content,
        "finish_reason": response.get("finish_reason"),
        "classification": classification_status,
        "accepted_credit_equivalent_usd": format(accepted_cost, "f"),
        "repair_performed": G7M_TERMINAL_REPAIR_PERFORMED,
        "apply_performed": G7M_TERMINAL_APPLY_PERFORMED,
        "historical_and_frozen": True,
        "evidence_root": str(root),
        "evidence_root_modified": False,
        "provider_calls": 0,
        "provider_client_constructed": False,
        "credential_access": False,
    }


# ---------------------------------------------------------------------------
# Binding payload and plan semantics
# ---------------------------------------------------------------------------


def _binding_payload(
    *,
    recovery_generation_id: str,
    run_id: str,
    incident: Mapping[str, Any],
    terminal: Mapping[str, Any],
    corrected_aux_budget: int,
    max_sends: int,
    max_ce_usd: str,
) -> dict[str, Any]:
    """Return the exact canonical recovery binding payload.

    The payload is the sha256 source of the recovery binding: the run/novel/
    chapter identity, the pinned Chapter-1 output and QA bytes, the frozen
    historical incident record hashes, the terminal G7M recovery evidence, the
    degraded committed state, and the corrected 8K auxiliary budget with both send
    and cost ceilings. Nothing here is derived from a live Provider response: every
    value is either a pinned frozen literal or a hash re-derived from historical
    evidence bytes.
    """
    return {
        "revision": G7N_REVISION,
        "recovery_generation_id": recovery_generation_id,
        "run_id": run_id,
        "novel_id": incident["novel_id"],
        "chapter": TARGET_CHAPTER,
        "model_id": incident["model_id"],
        "source_sha256": incident["source_sha256"],
        "committed_translation_sha256": COMMITTED_TRANSLATION_SHA256,
        "qa_sha256": QA_SHA256,
        "progress": {
            "completed": [TARGET_CHAPTER],
            "last_completed": TARGET_CHAPTER,
            "next_chapter": TARGET_CHAPTER + 1,
        },
        "state_extraction_journal_record_sha256": (
            incident["state_extraction_journal_record_sha256"]
        ),
        "state_json_repair_journal_record_sha256": (
            incident["state_json_repair_journal_record_sha256"]
        ),
        "original_ch1_ledger_record": {
            "plan_sha256": ORIGINAL_LEDGER_PLAN_SHA256,
            "approval_token": ORIGINAL_LEDGER_APPROVAL_TOKEN,
            "new_provider_sends_started": (
                ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
            ),
            "accepted_credit_equivalent_usd": (
                ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
            ),
            "ledger_sha256": incident["ledger_sha256"],
            "ledger_file_sha256": incident["ledger_file_sha256"],
        },
        "degraded_state": {
            "glossary": {},
            "continuity_summary": incident["degraded_state"][
                "continuity_summary"
            ],
            "issues": list(incident["degraded_state"]["issues"]),
        },
        "terminal_g7m_recovery": {
            "ledger_sha256": terminal["ledger_sha256"],
            "ledger_file_sha256": terminal["ledger_file_sha256"],
            "journal_sha256": terminal["journal_sha256"],
            "journal_file_sha256": terminal["journal_file_sha256"],
            "journal_kind": terminal["journal_kind"],
            "journal_status": terminal["journal_status"],
            "provider_send_count": terminal["provider_send_count"],
            "completion_tokens": terminal["completion_tokens"],
            "reasoning_tokens": terminal["reasoning_tokens"],
            "visible_content": terminal["visible_content"],
            "finish_reason": terminal["finish_reason"],
            "classification": terminal["classification"],
            "repair_performed": terminal["repair_performed"],
            "apply_performed": terminal["apply_performed"],
        },
        "legacy_corrected_auxiliary_budget": LEGACY_CORRECTED_AUXILIARY_BUDGET,
        "policy_revision": POLICY_REVISION,
        "corrected_auxiliary_budget": corrected_aux_budget,
        "max_sends": max_sends,
        "max_ce_usd": max_ce_usd,
    }


def _iter_leaf_values(
    value: Any,
    prefix: tuple[str, ...] = (),
) -> Any:
    """Yield ``(key-path, leaf-value)`` for every scalar leaf of a JSON value."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_leaf_values(item, prefix + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_leaf_values(item, prefix + (str(index),))
    else:
        yield prefix, value


def _validate_no_active_consumed_authorization(plan: Mapping[str, Any]) -> None:
    """Fail closed if the consumed authorizer is referenced as active.

    The consumed Chapter-1 ``plan_sha256``/``approval_token`` VALUES may appear
    ONLY in the historical record locations of this plan. Any other occurrence --
    and any populated ``active_authorization`` -- means the plan is trying to reuse
    the consumed paid authorization and must never be verified.
    """
    authorization = plan.get("authorization")
    if not isinstance(authorization, Mapping):
        raise G7NRecoveryError("the plan has no authorization boundary")
    if authorization.get("active_authorization") is not None:
        raise G7NRecoveryError(
            "the plan must never carry an active authorization"
        )
    if authorization.get("granted") is not False:
        raise G7NRecoveryError(
            "the plan must never grant operator authorization"
        )

    for path, value in _iter_leaf_values(plan):
        if not isinstance(value, str) or value not in (
            _CONSUMED_AUTHORIZATION_VALUES
        ):
            continue
        if any(
            path[: len(allowed)] == allowed
            for allowed in _ALLOWED_CONSUMED_AUTHORIZATION_PATHS
        ):
            continue
        raise G7NRecoveryError(
            "the consumed Chapter-1 executor authorization must never be "
            "referenced as an active authorization: "
            f"{'.'.join(path) or '<root>'}"
        )


def _validate_plan_semantics(plan: Mapping[str, Any]) -> None:
    """Fail closed unless a recovery plan satisfies every G7N safety rule."""
    if not isinstance(plan, Mapping):
        raise G7NRecoveryError("the recovery plan must be a mapping")
    if plan.get("schema_version") != G7N_SCHEMA_VERSION:
        raise G7NRecoveryError("unsupported G7N plan schema")
    if plan.get("revision") != G7N_REVISION:
        raise G7NRecoveryError("unsupported G7N plan revision")
    if plan.get("g7n_policy_sha256") != G7N_POLICY_SHA256:
        raise G7NRecoveryError("the plan binds another G7N policy")
    generation_id = _validate_generation_id(
        plan.get("recovery_generation_id")
    )
    if generation_id == PREVIOUS_RECOVERY_GENERATION_ID:
        raise G7NRecoveryError("the recovery generation id was reused")

    scope = plan.get("chapter_scope")
    if not isinstance(scope, Mapping):
        raise G7NRecoveryError("the plan has no chapter scope")
    if scope.get("chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError("the recovery scope must be Chapter 1")
    if scope.get("allowed_chapters") != [TARGET_CHAPTER]:
        raise G7NRecoveryError("the recovery scope must be Chapter 1 only")
    for flag in (
        "chapter_1_retranslation_allowed",
        "chapter_1_retransmit_allowed",
        "chapter_2_advance_allowed",
    ):
        if scope.get(flag) is not False:
            raise G7NRecoveryError(f"the recovery scope must forbid {flag}")
    if scope.get("next_chapter") != TARGET_CHAPTER + 1:
        raise G7NRecoveryError("the recovery scope next chapter changed")
    if scope.get("committed_translation_sha256") != (
        COMMITTED_TRANSLATION_SHA256
    ):
        raise G7NRecoveryError(
            "the recovery scope must pin the committed Chapter-1 translation"
        )

    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise G7NRecoveryError("the plan must declare its operations")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise G7NRecoveryError("a planned operation is malformed")
        if operation.get("chapter") != TARGET_CHAPTER:
            raise G7NRecoveryError(
                "every planned operation must stay inside Chapter 1; a "
                "Chapter-2-or-later operation is rejected"
            )
        logical = operation.get("logical_operation")
        if logical == LIFECYCLE_OPERATION_TRANSLATION:
            raise G7NRecoveryError(
                "a Chapter-1 retranslation operation is forbidden"
            )
        journal_kind = operation.get("journal_kind")
        if not isinstance(journal_kind, str) or not journal_kind:
            raise G7NRecoveryError(
                "a planned operation must declare its journal namespace"
            )
        if journal_kind.startswith(G7C_JOURNAL_KIND_PREFIX) or journal_kind in (
            FIXED_STATE_EXTRACTION_JOURNAL_KIND,
            FIXED_STATE_JSON_REPAIR_JOURNAL_KIND,
        ):
            raise G7NRecoveryError(
                "the recovery must never reuse a consumed fixed lifecycle "
                f"journal slot: {journal_kind}"
            )
        if operation.get("operation_id") != journal_kind:
            raise G7NRecoveryError(
                "the recovery operation identity and journal namespace must "
                "match exactly"
            )
        if operation.get("reuses_fixed_state_extraction_slot") is not False:
            raise G7NRecoveryError(
                "the consumed Chapter-1 state_extraction slot must stay unused"
            )
        if operation.get("corrected_auxiliary_budget") != (
            CORRECTED_AUXILIARY_BUDGET
        ):
            raise G7NRecoveryError(
                "every planned recovery operation must carry the corrected 8K "
                "auxiliary ceiling"
            )
        for forbidden in (
            "translation_sha256",
            "retransmit",
            "fabricated_state",
        ):
            if operation.get(forbidden) not in (None, False, ""):
                raise G7NRecoveryError(
                    f"a planned operation must never carry {forbidden}"
                )

    _validate_no_active_consumed_authorization(plan)

    authorization = plan["authorization"]
    if authorization.get("required") is not True:
        raise G7NRecoveryError(
            "a fresh explicit operator authorization must be required"
        )
    fields = authorization.get("required_future_authorization_fields")
    if not isinstance(fields, list) or not fields:
        raise G7NRecoveryError(
            "the plan must list the required fresh authorization fields"
        )
    for required in (
        "recovery_generation_id",
        "recovery_binding_sha256",
        "chapter",
        "model_id",
        "corrected_auxiliary_budget",
        "maximum_new_provider_sends",
        "maximum_credit_equivalent_usd",
    ):
        if required not in fields:
            raise G7NRecoveryError(
                f"the fresh authorization must bind {required}"
            )
    if authorization.get("historical_consumed_authorization_reusable") is not (
        False
    ):
        raise G7NRecoveryError(
            "the consumed authorization must be recorded as non-reusable"
        )
    if authorization.get("bound_recovery_generation_id") != generation_id:
        raise G7NRecoveryError(
            "the authorization boundary must bind the recovery generation id"
        )
    if authorization.get("bound_recovery_binding_sha256") != plan.get(
        "recovery_binding_sha256"
    ):
        raise G7NRecoveryError(
            "the authorization boundary must bind the recovery binding"
        )

    apply_contract = plan.get("apply_contract")
    if not isinstance(apply_contract, Mapping):
        raise G7NRecoveryError("the plan must describe its apply contract")
    if apply_contract.get("executed") is not False:
        raise G7NRecoveryError("the apply contract must not be executed")
    if apply_contract.get("atomic") is not True:
        raise G7NRecoveryError("the apply contract must be atomic")
    if apply_contract.get("fail_closed") is not True:
        raise G7NRecoveryError("the apply contract must fail closed")
    if apply_contract.get("partial_apply_forbidden") is not True:
        raise G7NRecoveryError(
            "a partial apply must be forbidden"
        )
    if apply_contract.get("no_local_fabrication_of_state") is not True:
        raise G7NRecoveryError(
            "the apply contract must forbid local state fabrication"
        )
    if apply_contract.get("progress_or_chapter_files_mutated") is not False:
        raise G7NRecoveryError(
            "the apply contract must never mutate progress or chapter files"
        )
    fields = apply_contract.get("fields")
    if not isinstance(fields, list) or sorted(fields) != sorted(
        [GLOSSARY_RELATIVE, CONTINUITY_RELATIVE, ISSUES_RELATIVE]
    ):
        raise G7NRecoveryError(
            "the apply contract must describe exactly the degraded state files"
        )

    pipeline = plan.get("post_recovery_pipeline")
    if not isinstance(pipeline, Mapping):
        raise G7NRecoveryError(
            "the plan must describe the later normal pipeline"
        )
    if pipeline.get("glossary_review_expressible") is not True:
        raise G7NRecoveryError(
            "the later normal Chapter-1 glossary review must stay expressible"
        )

    budget = plan.get("corrected_auxiliary_budget")
    if budget != CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the plan must carry the versioned corrected 8K GOAT auxiliary "
            "ceiling"
        )
    if budget != g7j.GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS:
        raise G7NRecoveryError(
            "the plan must carry the corrected 8K GOAT auxiliary ceiling"
        )
    if budget == LEGACY_CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the plan must not carry the frozen historical 4K auxiliary ceiling"
        )
    if plan.get("legacy_corrected_auxiliary_budget") != (
        LEGACY_CORRECTED_AUXILIARY_BUDGET
    ):
        raise G7NRecoveryError(
            "the plan must keep recording the frozen historical 4K auxiliary "
            "ceiling"
        )
    if plan.get("policy_revision") != g7j.GOAT_AUXILIARY_8K_REVISION:
        raise G7NRecoveryError(
            "the plan must record the versioned 8K auxiliary policy revision"
        )
    if plan.get("production_auxiliary_identity_ceiling") != (
        g7j.auxiliary_completion_budget_for_identity(goat=False)
    ):
        raise G7NRecoveryError(
            "the plan must record the unchanged production identity ceiling"
        )

    terminal = plan.get("terminal_evidence")
    if not isinstance(terminal, Mapping):
        raise G7NRecoveryError(
            "the plan must bind the terminal G7M recovery evidence"
        )
    if plan.get("terminal_evidence_modified") is not False:
        raise G7NRecoveryError(
            "the terminal G7M recovery evidence must be recorded as unmodified"
        )
    if not _matches_pinned_sha256(
        str(terminal.get("ledger_file_sha256")), G7M_TERMINAL_LEDGER_SHA256
    ):
        raise G7NRecoveryError(
            "the plan must rebind the pinned terminal G7M ledger file digest"
        )
    if not _matches_pinned_sha256(
        str(terminal.get("journal_file_sha256")), G7M_TERMINAL_JOURNAL_SHA256
    ):
        raise G7NRecoveryError(
            "the plan must rebind the pinned terminal G7M journal file digest"
        )
    for terminal_field, expected_value in (
        ("journal_kind", G7M_TERMINAL_JOURNAL_KIND),
        ("journal_status", G7M_TERMINAL_JOURNAL_STATUS),
        ("provider_send_count", G7M_TERMINAL_PROVIDER_SEND_COUNT),
        ("completion_tokens", G7M_TERMINAL_COMPLETION_TOKENS),
        ("reasoning_tokens", G7M_TERMINAL_REASONING_TOKENS),
        ("visible_content", G7M_TERMINAL_VISIBLE_CONTENT),
        ("finish_reason", G7M_TERMINAL_FINISH_REASON),
        ("classification", G7M_TERMINAL_CLASSIFICATION),
        ("repair_performed", G7M_TERMINAL_REPAIR_PERFORMED),
        ("apply_performed", G7M_TERMINAL_APPLY_PERFORMED),
    ):
        if terminal.get(terminal_field) != expected_value:
            raise G7NRecoveryError(
                "the terminal G7M recovery evidence must record "
                f"{terminal_field}={expected_value!r}"
            )
    if terminal.get("provider_calls") != 0:
        raise G7NRecoveryError(
            "the terminal G7M verification must show zero Provider calls"
        )
    if terminal.get("credential_access") is not False:
        raise G7NRecoveryError(
            "the terminal G7M verification must show zero credential access"
        )

    payload = plan.get("recovery_binding_payload")
    if not isinstance(payload, Mapping):
        raise G7NRecoveryError("the plan lacks its recovery binding payload")
    if payload.get("recovery_generation_id") != generation_id:
        raise G7NRecoveryError(
            "the recovery binding payload must bind the recovery generation id"
        )
    if payload.get("chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError(
            "the recovery binding payload must bind Chapter 1"
        )
    if payload.get("committed_translation_sha256") != (
        COMMITTED_TRANSLATION_SHA256
    ):
        raise G7NRecoveryError(
            "the recovery binding payload must pin the committed translation"
        )
    if payload.get("qa_sha256") != QA_SHA256:
        raise G7NRecoveryError(
            "the recovery binding payload must pin the Chapter-1 QA bytes"
        )
    if payload.get("corrected_auxiliary_budget") != budget:
        raise G7NRecoveryError(
            "the recovery binding payload must bind the corrected ceiling"
        )
    if payload.get("legacy_corrected_auxiliary_budget") != (
        LEGACY_CORRECTED_AUXILIARY_BUDGET
    ):
        raise G7NRecoveryError(
            "the recovery binding payload must record the frozen historical 4K "
            "ceiling"
        )
    if payload.get("policy_revision") != g7j.GOAT_AUXILIARY_8K_REVISION:
        raise G7NRecoveryError(
            "the recovery binding payload must bind the 8K policy revision"
        )
    payload_terminal = payload.get("terminal_g7m_recovery")
    if not isinstance(payload_terminal, Mapping):
        raise G7NRecoveryError(
            "the recovery binding payload must bind the terminal G7M recovery"
        )
    for payload_field in (
        "ledger_sha256",
        "ledger_file_sha256",
        "journal_sha256",
        "journal_file_sha256",
        "journal_kind",
        "journal_status",
        "provider_send_count",
        "completion_tokens",
        "reasoning_tokens",
        "visible_content",
        "finish_reason",
        "classification",
        "repair_performed",
        "apply_performed",
    ):
        if payload_terminal.get(payload_field) != terminal.get(payload_field):
            raise G7NRecoveryError(
                "the recovery binding payload terminal G7M binding must match "
                f"the verified terminal evidence: {payload_field}"
            )
    expected_binding_sha256 = _sha256_json(dict(payload))
    if plan.get("recovery_binding_sha256") != expected_binding_sha256:
        raise G7NRecoveryError(
            "the recovery binding SHA-256 does not recompute"
        )

    safety = plan.get("safety")
    if not isinstance(safety, Mapping):
        raise G7NRecoveryError("the plan lacks its safety record")
    if dict(safety) != _expected_safety():
        raise G7NRecoveryError("the plan safety record changed")


def _expected_safety() -> dict[str, Any]:
    return {
        "provider_calls": 0,
        "provider_calls_performed": 0,
        "provider_client_constructed": False,
        "credential_access": False,
        "paid_execution_authorized": False,
        "chapter_1_executed": False,
        "chapter_1_retranslated": False,
        "chapter_2_advanced": False,
        "state_fabricated": False,
        "historical_evidence_modified": False,
        "evidence_root_written": False,
        "transport_symbols_in_module": 0,
        "reasoning_effort": TARGET_REASONING_EFFORT,
        "maximum_new_provider_sends": DEFAULT_MAX_SENDS,
        "maximum_credit_equivalent_usd": DEFAULT_MAX_CE_USD,
        "ch1_primary_retry_forbidden": True,
        "ch2_forbidden": True,
        "normal_glossary_review_separate": True,
        "empty_fail_closed_repair_forbidden": True,
    }


def _validate_plan_envelope(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(plan)
    stored = value.pop("plan_sha256", None)
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7NRecoveryError("the G7N recovery plan SHA is invalid")
    if _sha256_json(value) != stored:
        raise G7NRecoveryError(
            "the G7N recovery plan SHA does not recompute"
        )
    value["plan_sha256"] = stored
    return value


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def plan_ch1_state_recovery(
    evidence_root: Path | str,
    run_id: str,
    *,
    corrected_aux_budget: int = CORRECTED_AUXILIARY_BUDGET,
    max_sends: int = DEFAULT_MAX_SENDS,
    max_ce_usd: Any = DEFAULT_MAX_CE_USD,
) -> dict[str, Any]:
    """Build one zero-provider Chapter-1 auxiliary state-recovery plan.

    The plan is returned only; nothing is written anywhere, and the evidence root
    is opened strictly read-only. Both historical layers are verified: the original
    G7C/G7F Chapter-1 incident evidence and the terminal G7M recovery evidence. The
    plan carries no authorization and performs no send: a later, separately
    authorized execution owns any Provider call.
    """
    assert_no_transport_symbols()

    budget = _positive_int(corrected_aux_budget, "corrected_aux_budget")
    if budget != CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the corrected auxiliary ceiling must be the versioned 8K GOAT "
            f"auxiliary total derived by policy revision {POLICY_REVISION}"
        )
    if budget != g7j.GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS:
        raise G7NRecoveryError(
            "the corrected GOAT auxiliary ceiling is frozen at "
            f"{g7j.GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS}"
        )
    if budget != g7j.goat_auxiliary_8k_completion_budget_for_identity(goat=True):
        raise G7NRecoveryError(
            "the corrected auxiliary budget must match the GOAT state "
            "checkpoint identity ceiling"
        )
    if budget == LEGACY_CORRECTED_AUXILIARY_BUDGET:
        raise G7NRecoveryError(
            "the corrected 8K ceiling must differ from the frozen historical 4K "
            "auxiliary ceiling"
        )
    sends = _positive_int(max_sends, "max_sends")
    cost = _decimal_text(max_ce_usd, "max_ce_usd")

    incident = verify_historical_incident(evidence_root, run_id)
    terminal = verify_terminal_recovery_incident(evidence_root, run_id)

    generation_id = _validate_generation_id(
        RECOVERY_GENERATION_PREFIX
        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    if generation_id == PREVIOUS_RECOVERY_GENERATION_ID:
        raise G7NRecoveryError(
            "the minted recovery generation must differ from the consumed one"
        )

    payload = _binding_payload(
        recovery_generation_id=generation_id,
        run_id=run_id,
        incident=incident,
        terminal=terminal,
        corrected_aux_budget=budget,
        max_sends=sends,
        max_ce_usd=cost,
    )
    binding_sha256 = _sha256_json(payload)

    plan = {
        "schema_version": G7N_SCHEMA_VERSION,
        "revision": G7N_REVISION,
        "g7n_policy_sha256": G7N_POLICY_SHA256,
        "recovery_generation_id": generation_id,
        "previous_recovery_generation_id": (
            PREVIOUS_RECOVERY_GENERATION_ID
        ),
        "recovery_generation_id_differs_from_previous": True,
        "recovery_binding_payload": payload,
        "recovery_binding_sha256": binding_sha256,
        "run_id": run_id,
        "novel_id": incident["novel_id"],
        "model_id": incident["model_id"],
        "chapter": TARGET_CHAPTER,
        "corrected_auxiliary_budget": budget,
        "legacy_corrected_auxiliary_budget": (
            LEGACY_CORRECTED_AUXILIARY_BUDGET
        ),
        "policy_revision": POLICY_REVISION,
        "production_auxiliary_identity_ceiling": (
            g7j.auxiliary_completion_budget_for_identity(goat=False)
        ),
        "maximum_new_provider_sends": sends,
        "maximum_credit_equivalent_usd": cost,
        "chapter_scope": {
            "chapter": TARGET_CHAPTER,
            "allowed_chapters": [TARGET_CHAPTER],
            "next_chapter": TARGET_CHAPTER + 1,
            "chapter_1_retranslation_allowed": False,
            "chapter_1_retransmit_allowed": False,
            "chapter_2_advance_allowed": False,
            "committed_translation_sha256": (
                COMMITTED_TRANSLATION_SHA256
            ),
            "qa_sha256": QA_SHA256,
            "translation_operation_present": False,
        },
        "operations": [
            {
                "operation_id": RECOVERY_OPERATION_ID,
                "journal_kind": RECOVERY_OPERATION_ID,
                "journal_slot_rule": (
                    "own operation namespace; never a frozen lifecycle slot"
                ),
                "chapter": TARGET_CHAPTER,
                "logical_operation": RECOVERED_LOGICAL_OPERATION,
                "recovered_logical_operation": (
                    RECOVERED_LOGICAL_OPERATION
                ),
                "reuses_fixed_state_extraction_slot": False,
                "reuses_fixed_state_json_repair_slot": False,
                "uses_lifecycle_operation_slot": False,
                "corrected_auxiliary_budget": budget,
                "reasoning_effort": TARGET_REASONING_EFFORT,
                "state_source_rule": (
                    "state may only come from a Provider response that the G7J "
                    "auxiliary classifier reports as auxiliary_state_accepted; "
                    "an empty or fail-closed response is never repaired, never "
                    "retried, and never fabricated locally"
                ),
                "provider_calls_executed": 0,
                "provider_calls_planned": 0,
                "maximum_new_provider_sends": sends,
                "maximum_credit_equivalent_usd": cost,
                "requires_fresh_operator_authorization": True,
            }
        ],
        "post_recovery_pipeline": {
            "glossary_review_expressible": True,
            "normal_chapter_1_terminology_pipeline_expressible": True,
            "normal_chapter_1_continuity_pipeline_expressible": True,
            "steps": [
                {
                    "step": "recovered_state_checkpoint_apply",
                    "operation_id": RECOVERY_OPERATION_ID,
                    "chapter": TARGET_CHAPTER,
                    "blocked_by_this_plan": True,
                    "requires_fresh_operator_authorization": True,
                },
                {
                    "step": "normal_chapter_1_glossary_review",
                    "logical_operation": "glossary_review",
                    "chapter": TARGET_CHAPTER,
                    "blocked_by_this_plan": False,
                    "requires_separate_authorization": True,
                },
                {
                    "step": "normal_chapter_1_continuity_and_issues_update",
                    "chapter": TARGET_CHAPTER,
                    "blocked_by_this_plan": False,
                    "requires_separate_authorization": True,
                },
            ],
            "next_chapter_requires_a_separate_future_plan": True,
        },
        "authorization": {
            "required": True,
            "granted": False,
            "active_authorization": None,
            "planner_emits_authorization": False,
            "historical_consumed_plan_sha256": (
                ORIGINAL_LEDGER_PLAN_SHA256
            ),
            "historical_consumed_approval_token": (
                ORIGINAL_LEDGER_APPROVAL_TOKEN
            ),
            "historical_consumed_authorization_reusable": False,
            "historical_consumed_executor_token_reuse_forbidden": True,
            "bound_recovery_generation_id": generation_id,
            "bound_recovery_binding_sha256": binding_sha256,
            "required_future_authorization_fields": [
                "recovery_generation_id",
                "recovery_binding_sha256",
                "run_id",
                "novel_id",
                "chapter",
                "model_id",
                "corrected_auxiliary_budget",
                "maximum_new_provider_sends",
                "maximum_credit_equivalent_usd",
                "operator_authorized",
            ],
        },
        "apply_contract": {
            "status": "described_not_executed",
            "executed": False,
            "atomic": True,
            "fail_closed": True,
            "atomic_swap_required": True,
            "single_writer_run_lock_required": True,
            "partial_apply_forbidden": True,
            "no_local_fabrication_of_state": True,
            "progress_or_chapter_files_mutated": False,
            "fields": [
                GLOSSARY_RELATIVE,
                CONTINUITY_RELATIVE,
                ISSUES_RELATIVE,
            ],
            "pre_apply_verification": [
                "recovery_binding_sha256",
                "recovery_generation_id",
                "fresh_operator_authorization",
                "corrected_auxiliary_budget",
                "committed_translation_sha256",
                "qa_sha256",
                "historical_evidence_unchanged",
            ],
            "post_apply_verification": [
                "glossary_entries_committed",
                "continuity_summary_committed",
                "issues_committed",
                "progress_still_at_chapter_1",
            ],
            "rollback": "restore_previous_bytes_or_abort",
            "on_any_failure": "abort_without_mutation",
        },
        "observed_evidence": incident,
        "terminal_evidence": terminal,
        "terminal_evidence_modified": False,
        "safety": _expected_safety(),
        "provider_calls": 0,
    }
    plan["plan_sha256"] = _sha256_json(plan)
    value = _validate_plan_envelope(plan)
    _validate_plan_semantics(value)
    return value


def render_summary(plan: Mapping[str, Any]) -> str:
    """Render one recovery plan as a non-authorizing text summary."""
    return "\n".join(
        [
            f"{PLAN_SUMMARY_LABEL}=PASS",
            "BENCHMARK_PROVIDER_CALLS=0",
            "CREDENTIAL_ACCESS=NO",
            "PAID_EXECUTION_AUTHORIZED=NO",
            "CHAPTER_1_EXECUTED=NO",
            "CHAPTER_1_RETRANSLATED=NO",
            "CHAPTER_2_ADVANCED=NO",
            "HISTORICAL_EVIDENCE_MODIFIED=NO",
            "EVIDENCE_ROOT_WRITTEN=NO",
            "FRESH_OPERATOR_AUTHORIZATION_REQUIRED=YES",
            f"STATUS={plan.get('status', 'READY_FOR_FRESH_OPERATOR_AUTHORIZATION')}",
            f"G7N_POLICY_SHA256={plan.get('g7n_policy_sha256')}",
            f"RECOVERY_GENERATION_ID={plan.get('recovery_generation_id')}",
            f"PREVIOUS_RECOVERY_GENERATION_ID={plan.get('previous_recovery_generation_id')}",
            f"RECOVERY_BINDING_SHA256={plan.get('recovery_binding_sha256')}",
            f"PLAN_SHA256={plan.get('plan_sha256')}",
            f"RUN_ID={plan.get('run_id')}",
            f"NOVEL_ID={plan.get('novel_id')}",
            f"MODEL_ID={plan.get('model_id')}",
            f"CHAPTER={plan.get('chapter')}",
            "CORRECTED_AUXILIARY_BUDGET="
            f"{plan.get('corrected_auxiliary_budget')}",
            "LEGACY_CORRECTED_AUXILIARY_BUDGET="
            f"{plan.get('legacy_corrected_auxiliary_budget')}",
            f"POLICY_REVISION={plan.get('policy_revision')}",
            "PRODUCTION_AUXILIARY_IDENTITY_CEILING="
            f"{plan.get('production_auxiliary_identity_ceiling')}",
            f"MAX_NEW_PROVIDER_SENDS={plan.get('maximum_new_provider_sends')}",
            "MAX_CREDIT_EQUIVALENT_USD="
            f"{plan.get('maximum_credit_equivalent_usd')}",
            "G7M_TERMINAL_LEDGER_SHA256="
            f"{G7M_TERMINAL_LEDGER_SHA256}",
            "G7M_TERMINAL_JOURNAL_SHA256="
            f"{G7M_TERMINAL_JOURNAL_SHA256}",
            "G7M_TERMINAL_JOURNAL_STATUS="
            f"{G7M_TERMINAL_JOURNAL_STATUS}",
            "G7M_TERMINAL_PROVIDER_SEND_COUNT="
            f"{G7M_TERMINAL_PROVIDER_SEND_COUNT}",
            "G7M_TERMINAL_COMPLETION_TOKENS="
            f"{G7M_TERMINAL_COMPLETION_TOKENS}",
            "G7M_TERMINAL_REASONING_TOKENS="
            f"{G7M_TERMINAL_REASONING_TOKENS}",
            f"G7M_TERMINAL_CLASSIFICATION={G7M_TERMINAL_CLASSIFICATION}",
            "G7M_TERMINAL_REPAIR_PERFORMED="
            f"{G7M_TERMINAL_REPAIR_PERFORMED}",
            "G7M_TERMINAL_APPLY_PERFORMED="
            f"{G7M_TERMINAL_APPLY_PERFORMED}",
            f"COMMITTED_TRANSLATION_SHA256={COMMITTED_TRANSLATION_SHA256}",
            f"QA_SHA256={QA_SHA256}",
            "CONSUMED_AUTHORIZATION_REUSABLE=NO",
        ]
    )


def verify_recovery_plan(plan_path: Path | str) -> dict[str, Any]:
    """Read-only re-verification of one materialized G7N recovery plan."""
    assert_no_transport_symbols()
    path = Path(plan_path)
    if not path.is_file():
        raise G7NRecoveryError(f"recovery plan file missing: {path}")
    value = _validate_plan_envelope(
        _read_object(path, "G7N recovery plan")
    )
    _validate_plan_semantics(value)
    if value.get("chapter") != TARGET_CHAPTER:
        raise G7NRecoveryError("the recovery plan chapter changed")
    if value.get("recovery_generation_id_differs_from_previous") is not True:
        raise G7NRecoveryError(
            "the recovery generation must be explicitly distinct"
        )
    terminal = value["terminal_evidence"]
    return {
        "status": "READY_FOR_FRESH_OPERATOR_AUTHORIZATION",
        "g7n_policy_sha256": G7N_POLICY_SHA256,
        "plan_path": str(path),
        "plan_sha256": value["plan_sha256"],
        "recovery_generation_id": value["recovery_generation_id"],
        "recovery_generation_id_differs_from_previous": True,
        "previous_recovery_generation_id": (
            PREVIOUS_RECOVERY_GENERATION_ID
        ),
        "recovery_binding_sha256": value["recovery_binding_sha256"],
        "run_id": value.get("run_id"),
        "novel_id": value.get("novel_id"),
        "chapter": value.get("chapter"),
        "model_id": value.get("model_id"),
        "corrected_auxiliary_budget": value.get(
            "corrected_auxiliary_budget"
        ),
        "legacy_corrected_auxiliary_budget": value.get(
            "legacy_corrected_auxiliary_budget"
        ),
        "policy_revision": value.get("policy_revision"),
        "production_auxiliary_identity_ceiling": value.get(
            "production_auxiliary_identity_ceiling"
        ),
        "maximum_new_provider_sends": value.get(
            "maximum_new_provider_sends"
        ),
        "maximum_credit_equivalent_usd": value.get(
            "maximum_credit_equivalent_usd"
        ),
        "g7m_terminal_ledger_sha256": terminal.get("ledger_sha256"),
        "g7m_terminal_journal_sha256": terminal.get("journal_sha256"),
        "g7m_terminal_journal_status": terminal.get("journal_status"),
        "g7m_terminal_provider_send_count": terminal.get(
            "provider_send_count"
        ),
        "g7m_terminal_classification": terminal.get("classification"),
        "committed_translation_sha256": COMMITTED_TRANSLATION_SHA256,
        "qa_sha256": QA_SHA256,
        "consumed_authorization_reusable": False,
        "provider_calls": 0,
        "provider_client_constructed": False,
        "credential_access": False,
        "paid_execution_authorized": False,
        "chapter_1_executed": False,
        "chapter_1_retranslated": False,
        "chapter_2_advanced": False,
        "state_fabricated": False,
        "historical_evidence_modified": False,
        "evidence_root_written": False,
        "new_operator_authorization_required": True,
    }


def write_recovery_plan(
    plan: Mapping[str, Any],
    plan_path: Path | str,
    *,
    evidence_root: Path | str,
) -> Path:
    """Materialize a verified plan OUTSIDE the evidence root.

    The recovery boundary must never write into the historical evidence root, so
    a target inside it is rejected before anything is created.
    """
    assert_no_transport_symbols()
    value = _validate_plan_envelope(plan)
    _validate_plan_semantics(value)
    root = Path(evidence_root).resolve()
    target = Path(plan_path)
    if _path_is_within(target, root):
        raise G7NRecoveryError(
            "a recovery plan must never be written inside the evidence root"
        )
    if atomic_write_json is None:  # pragma: no cover - defensive
        raise G7NRecoveryError("no atomic JSON writer is available")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, value)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7n-ch1-state-recovery-plan",
        description=(
            "Zero-provider Chapter-1 auxiliary state-recovery planning and "
            "verification for the GOAT Run-1 degraded state incident"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--evidence-root", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--plan-out", required=True)
    plan.add_argument(
        "--corrected-auxiliary-budget",
        type=int,
        default=CORRECTED_AUXILIARY_BUDGET,
    )
    plan.add_argument("--max-sends", type=int, default=DEFAULT_MAX_SENDS)
    plan.add_argument("--max-ce-usd", default=DEFAULT_MAX_CE_USD)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan = plan_ch1_state_recovery(
                args.evidence_root,
                args.run_id,
                corrected_aux_budget=args.corrected_auxiliary_budget,
                max_sends=args.max_sends,
                max_ce_usd=args.max_ce_usd,
            )
            write_recovery_plan(
                plan,
                args.plan_out,
                evidence_root=args.evidence_root,
            )
            print(render_summary(plan))
            return 0
        report = verify_recovery_plan(args.plan)
    except G7NRecoveryError as exc:
        print(f"{PLAN_SUMMARY_LABEL}=FAIL")
        print("BENCHMARK_PROVIDER_CALLS=0")
        print("CREDENTIAL_ACCESS=NO")
        print("PAID_EXECUTION_AUTHORIZED=NO")
        print(f"G7N_BLOCKER={exc}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        print(f"{PLAN_SUMMARY_LABEL}=FAIL")
        print("BENCHMARK_PROVIDER_CALLS=0")
        print("CREDENTIAL_ACCESS=NO")
        print("PAID_EXECUTION_AUTHORIZED=NO")
        print(f"G7N_BLOCKER={type(exc).__name__}: {str(exc)[:500]}")
        return 1

    print("\n".join(f"{key}={value}" for key, value in report.items()))
    return 0


__all__ = [
    "COMMITTED_TRANSLATION_SHA256",
    "CORRECTED_AUXILIARY_BUDGET",
    "DEFAULT_MAX_CE_USD",
    "DEFAULT_MAX_SENDS",
    "FIXED_STATE_EXTRACTION_JOURNAL_KIND",
    "FIXED_STATE_JSON_REPAIR_JOURNAL_KIND",
    "G7M_TERMINAL_APPLY_PERFORMED",
    "G7M_TERMINAL_CLASSIFICATION",
    "G7M_TERMINAL_COMPLETION_TOKENS",
    "G7M_TERMINAL_FINISH_REASON",
    "G7M_TERMINAL_JOURNAL_KIND",
    "G7M_TERMINAL_JOURNAL_RELATIVE",
    "G7M_TERMINAL_JOURNAL_SHA256",
    "G7M_TERMINAL_JOURNAL_STATUS",
    "G7M_TERMINAL_LEDGER_RELATIVE",
    "G7M_TERMINAL_LEDGER_SHA256",
    "G7M_TERMINAL_PROVIDER_SEND_COUNT",
    "G7M_TERMINAL_REASONING_TOKENS",
    "G7M_TERMINAL_REPAIR_PERFORMED",
    "G7M_TERMINAL_VISIBLE_CONTENT",
    "G7N_POLICY_SHA256",
    "G7N_REVISION",
    "G7N_SCHEMA_VERSION",
    "G7NRecoveryError",
    "LEGACY_CORRECTED_AUXILIARY_BUDGET",
    "ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD",
    "ORIGINAL_LEDGER_APPROVAL_TOKEN",
    "ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED",
    "ORIGINAL_LEDGER_PLAN_SHA256",
    "PLAN_FILENAME",
    "POLICY_REVISION",
    "PREVIOUS_RECOVERY_GENERATION_ID",
    "QA_SHA256",
    "RECOVERY_OPERATION_ID",
    "TARGET_CHAPTER",
    "TARGET_MODEL_ID",
    "assert_no_transport_symbols",
    "build_parser",
    "g7n_policy_identity",
    "main",
    "plan_ch1_state_recovery",
    "render_summary",
    "verify_historical_incident",
    "verify_recovery_plan",
    "verify_terminal_recovery_incident",
    "write_recovery_plan",
]


if __name__ == "__main__":
    raise SystemExit(main())
