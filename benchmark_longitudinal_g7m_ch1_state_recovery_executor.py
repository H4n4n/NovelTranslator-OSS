"""G7M Chapter-1 auxiliary state-recovery EXECUTOR (transport + atomic apply).

G7L owns the zero-provider recovery *plan* and its read-only verifier for the
committed GOAT Run-1 Chapter-1 degraded-state incident (an accepted
``state_extraction`` response that was EMPTY at the legacy 3,000-token ceiling,
followed by a JSON repair that was fed that EMPTY input and therefore fabricated
the committed continuity/issues state). G7L deliberately has no transport and no
apply. G7M is the bounded, separately-authorized EXECUTION layer that G7L lacks:

* a SEPARATE zero-provider preflight that verifies the reviewed code/policy
  identity, the materialized G7L plan (SHA, recovery generation, recovery
  binding), the whole historical evidence surface (source, committed Chapter-1
  translation, Chapter-1 QA, progress 1 -> 2, both original auxiliary journals,
  the original G7F Chapter-1 ledger, and the currently degraded
  glossary/continuity/issues state), the corrected 4,000 auxiliary ceiling, the
  2-new-send ceiling, the 0.05 credit-equivalent ceiling, and the
  non-reusability of the consumed historical authorization. It constructs no
  Provider client and touches no credential. It IS allowed to invoke the engine's
  exact state-extraction request shape, because it transports the RECOVERY
  OPERATION and carries no translation-operation request at all.
* a paid ``run_recovery_request`` that sends exactly the RECOVERY OPERATION
  (``state_extraction``, reasoning ``low``, 4,000 corrected ceiling) over the
  already-committed Chapter-1 source text and the already-committed Chapter-1
  English translation, under the repository's durable paid-request journal and
  its own recovery-budget ledger. Exactly one transport attempt per durable slot,
  no hidden SDK retry. The G7J auxiliary classifier gates the durable result:
  ``auxiliary_state_accepted`` may be applied, a nonempty malformed payload MAY
  use the one remaining authorized send for a bounded JSON repair, and an empty
  or otherwise fail-closed response STOPS with no repair, no fabrication, and no
  state mutation.
* an atomic, fail-closed ``apply_recovered_state`` that validates the complete
  candidate in memory, takes the single-writer production run lock, re-verifies
  the plan/generation/binding, preserves the previous bytes, writes
  ``data/glossary.json``, ``data/continuity.json`` and ``data/issues.json`` as one
  recoverable transaction, verifies the result, and restores the previous bytes on
  any failure. The engine's chapter progress file is never written and the
  committed Chapter-1 output and QA bytes never change.
* ``execute_ch1_state_recovery``, the paid orchestrator mirroring the G7G
  ``run_preflight``/``run_execute`` separation: internal re-preflight -> fresh
  authorization validation -> ONLY THEN credential read -> Provider client
  construction -> one journaled recovery request -> classifier gate -> atomic
  apply.

What G7M must never do: it never retranslates or retransmits Chapter 1, never
advances to Chapter 2, never fabricates local state, never repairs an empty
response, never hides an extra paid glossary send inside the recovery envelope
(a plan that demanded one fails closed with
``PLAN_REQUIRES_REMATERIALIZATION``), and never touches the consumed fixed
``g7c_*`` Chapter-1 journal slots, the historical G7F Chapter-1 ledger, or the
historical evidence bytes. This module contains no Chapter-1 translation request
path and no Chapter-2 path, and it asserts that structurally about its own source
at import time and at the start of preflight and execution.

Importing this module performs no filesystem write, reads no credential, loads no
``.env`` file, constructs no Provider client, and makes zero Provider calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

import benchmark_longitudinal_g7j_reasoning_budget as g7j
import benchmark_longitudinal_g7l_ch1_state_recovery_plan as g7l
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import (
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    PROVIDER_NAME,
)
from benchmark_longitudinal_g7c_stage1_integration import normalize_legacy_request
from benchmark_longitudinal_g7d_accounting_policy import (
    PRICING_VERSION,
    account_provider_usage,
    pricing_snapshot as g7d_pricing_snapshot,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)
from noveltranslator_core import atomic_write_bytes, atomic_write_json
from production_paid_request_journal import (
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from production_run_lock import production_run_lock


G7M_SCHEMA_VERSION = 1
G7M_REVISION = "goat-g7m-ch1-state-recovery-executor-v1"

TARGET_CHAPTER = 1
TARGET_MODEL_ID = "deepseek/deepseek-v4.1-flash"
TARGET_REASONING_EFFORT = "low"

# The ONLY logical operation a G7M recovery envelope may send. The bounded JSON
# repair is an internal sub-operation of the same envelope, never a second
# recovery operation.
RECOVERY_LOGICAL_OPERATION = "state_extraction"
ALLOWED_RECOVERY_OPERATIONS = frozenset({"state_extraction"})
ALLOWED_RECOVERY_CHAPTERS = (1,)

# G7M's OWN journal namespace. It is deliberately the G7L recovery operation id
# and never a frozen ``g7c_`` lifecycle slot.
RECOVERY_JOURNAL_KIND = "g7l_ch1_state_recovery_repair"

RECOVERY_LEDGER_DIR_RELATIVE = "data/g7m_recovery_ledgers"
RECOVERY_LEDGER_RELATIVE = "data/g7m_recovery_ledgers/0001.json"
RECOVERY_LEDGER_REVISION = "g7m-ch1-state-recovery-ledger-v1"
RECOVERY_LEDGER_SCHEMA_VERSION = 1

RECOVERY_MAX_COMPLETION_TOKENS = 4000
MAX_NEW_SENDS = 2
MAX_CE_USD = "0.05"

# Mirrors the frozen G7F conservative prompt-token safety overhead: the cost
# preflight is deliberately an upper bound, never an estimate of real usage.
PROMPT_TOKEN_SAFETY_OVERHEAD = 8192

PREFLIGHT_OK_STATUS = "PREFLIGHT_OK"
RECOVERY_ACCEPTED_STATUS = "CH1_STATE_RECOVERY_ACCEPTED"
RECOVERY_FAIL_CLOSED_STATUS = "CH1_STATE_RECOVERY_FAIL_CLOSED"
RECOVERY_APPLIED_STATUS = "CH1_STATE_RECOVERY_APPLIED"
REMATERIALIZATION_BLOCKER = "PLAN_REQUIRES_REMATERIALIZATION"

# The committed Chapter-1 source chapter text lives in the run root's production
# chapter library; the committed English translation is the committed Chapter-1
# output file. Neither is regenerated by G7M.
SOURCE_CHAPTER_LIBRARY_RELATIVE = "data/chapters.json"

APPLY_FIELDS = (
    g7l.GLOSSARY_RELATIVE,
    g7l.CONTINUITY_RELATIVE,
    g7l.ISSUES_RELATIVE,
)
STATE_KEYS = ("new_terms", "continuity_summary", "issues")
TERM_KEYS = ("chinese", "english", "category", "status", "notes")
PROVISIONAL_STATUS = "provisional"

# The plan's fresh authorization must bind exactly these values, plus the
# explicit operator flag.
RECOVERY_AUTHORIZATION_FIELDS = (
    "recovery_generation_id",
    "recovery_binding_sha256",
    "run_id",
    "novel_id",
    "chapter",
    "model_id",
    "corrected_auxiliary_budget",
    "maximum_new_provider_sends",
    "maximum_credit_equivalent_usd",
)
RECOVERY_AUTHORIZATION_OPERATOR_FIELD = "operator_authorized"

# A fresh authorization may keep a NON-active historical note under a
# ``historical_consumed*`` key; anywhere else the consumed executor authorizer's
# values are an attempt to reuse the consumed paid authorization.
_CONSUMED_AUTHORIZATION_VALUES = (
    g7l.ORIGINAL_LEDGER_PLAN_SHA256,
    g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN,
)
_CONSUMED_NOTE_KEY_PREFIX = "historical_consumed"

# The engine's frozen state-extraction prompt contract: exactly
# ``{"new_terms":[...],"continuity_summary":"...","issues":[]}`` with every
# new_term ``{"chinese","english","category","status":"provisional","notes"}``.
# The prompt is re-declared here rather than imported from the production engine
# so that the recovery boundary never loads the engine runtime (which owns the
# translation/Chapter-2 lifecycle this module must not be able to reach).
_STATE_SCHEMA_HINT = '{"new_terms":[],"continuity_summary":"...","issues":[]}'

_STATE_EXTRACTION_SYSTEM_PROMPT = (
    "\nYou maintain terminology and immediate continuity for a Chinese webnovel "
    "translation.\n"
    "Analyze the supplied Chinese chapter and final English translation.\n"
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "new_terms": [{"chinese":"...","english":"...","category":"...",'
    '"status":"provisional","notes":"..."}],\n'
    '  "continuity_summary":"...",\n'
    '  "issues":[]\n'
    "}\n"
    "Return exactly the three keys new_terms, continuity_summary, and issues.\n"
    "Only propose a term if it is genuinely useful for consistency in future "
    "chapters.\n"
    "Within new_terms, each exact \"chinese\" source key may appear at most once.\n"
    "Consolidate complementary evidence into that single proposal; never emit "
    "alternative English/category/status proposals for the same exact source "
    "key.\n"
    "The \"chinese\" field is the immutable source-side identity even for "
    "non-Chinese novels. Copy it as an EXACT literal span from CHINESE SOURCE. "
    "Never translate, transliterate, normalize, or substitute another script in "
    "that field.\n"
    "Do not propose ordinary vocabulary, one-time descriptions, ordinary "
    "objects, generic duties, generic tactical phrases, website/promotional "
    "language, or ordinary idioms.\n"
    "Do not propose a term already present in the supplied glossary list.\n"
    "All new terms must remain provisional.\n"
    "Keep the continuity summary concise and useful only for the NEXT chapter.\n"
    "\nISSUES RULES:\n"
    "- The local QA system already counts routine Hangul/encoding corruption.\n"
    "- Do NOT list individual corrupted glyphs or character substitutions.\n"
    "- Do NOT enumerate repeated encoding errors.\n"
    "- Do NOT report routine corruption if the intended meaning was resolved "
    "confidently.\n"
    "- Report an issue only when corruption or ambiguity materially affects "
    "translation confidence, terminology, continuity, or produces "
    "[TRANSLATION REVIEW REQUIRED].\n"
    "- Maximum 5 issues.\n"
    "- Each issue must be a short summary, preferably under 250 characters.\n"
    "\nReturn JSON only.\n"
)

_JSON_REPAIR_SYSTEM_PROMPT = (
    "\nYou repair malformed JSON.\n"
    "Return ONLY the corrected JSON object and nothing else.\n"
    "Never invent, extend, or fabricate content that is absent from the supplied "
    "malformed JSON; repair only its structure.\n"
    "Required structure:\n"
    + _STATE_SCHEMA_HINT
    + "\nEvery new term must stay provisional.\n"
    "Return JSON only.\n"
)

# Structural proof needles, assembled from fragments so that this module's own
# self-scan is never vacuous (and so that no needle can appear by accident). The
# recovery executor must contain no Chapter-1 translation request path and no
# Chapter-2/progress-advance path at all.
_FORBIDDEN_PATH_SYMBOLS = (
    "run_unattended" + "_production",
    "chapter_" + "trans" + "lation",
    '"' + "trans" + 'lation"',
    "progress" + "." + "json",
    "next_chapter" + " = ",
    "chapter" + " = 2",
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

Clock = Callable[[], datetime]


class G7MRecoveryError(RuntimeError):
    """Recovery execution, authorization, evidence, or apply is unsafe."""


def assert_no_translation_or_chapter2_path() -> None:
    """Fail closed unless this module has no translation/Chapter-2 path.

    The G7M executor is allowed to transport the RECOVERY OPERATION, so unlike
    the transport-free G7L planner it necessarily contains Provider transport.
    It must still be provably incapable of retranslating or retransmitting
    Chapter 1, of writing the engine's chapter progress file, and of advancing to
    Chapter 2. Its own source is scanned for every such symbol, and any
    occurrence fails closed.
    """
    try:
        source = Path(__file__).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        raise G7MRecoveryError(
            f"cannot read the G7M executor source: {exc}"
        ) from exc
    for symbol in _FORBIDDEN_PATH_SYMBOLS:
        if symbol in source:
            raise G7MRecoveryError(
                "the G7M recovery executor must contain no Chapter-1 "
                f"translation path and no Chapter-2 path; found {symbol!r}"
            )


# ---------------------------------------------------------------------------
# Small canonical helpers
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
        raise G7MRecoveryError(
            "value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise G7MRecoveryError(f"cannot hash file {path}: {exc}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7MRecoveryError(f"cannot read {label}: {target}") from exc
    if not isinstance(value, dict):
        raise G7MRecoveryError(f"{label} must contain a JSON object: {target}")
    return value


def _read_text(path: Path | str, label: str) -> str:
    target = Path(path)
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise G7MRecoveryError(f"cannot read {label}: {target}") from exc


def _read_text_exact_bytes(path: Path | str, label: str) -> str:
    """Read UTF-8 text whose re-encoding reproduces the file's exact bytes.

    ``Path.read_text`` applies universal-newline translation (CRLF -> LF), so a
    CRLF artifact would hash differently from its own raw bytes. Byte-identity
    bound artifacts are read this way instead.
    """
    target = Path(path)
    try:
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise G7MRecoveryError(f"cannot read {label}: {target}") from exc


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7MRecoveryError(f"{label} must be a SHA-256 hex digest")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise G7MRecoveryError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G7MRecoveryError(f"{label} must be a positive integer")
    return value


def _decimal_text(value: Any, label: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G7MRecoveryError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise G7MRecoveryError(f"{label} must be a nonnegative finite decimal")
    return format(parsed, "f")


def _matches_pinned_sha256(observed: str, pinned: str) -> bool:
    """Compare two SHA-256 digests case-insensitively."""
    return observed.lower() == pinned.lower()


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _use_clock(clock: Clock | None) -> Clock:
    return clock if clock is not None else _default_clock


def _now_utc(clock: Clock) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G7MRecoveryError(
            "the G7M recovery clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _iter_leaf_values(value: Any, prefix: tuple[str, ...] = ()) -> Any:
    """Yield ``(key-path, leaf-value)`` for every scalar leaf of a JSON value."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_leaf_values(item, prefix + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_leaf_values(item, prefix + (str(index),))
    else:
        yield prefix, value


# ---------------------------------------------------------------------------
# Recovery namespace helpers
# ---------------------------------------------------------------------------


def recovery_journal_path(journal_dir: Path | str) -> Path:
    """Return the recovery operation's own durable journal slot."""
    return paid_request_journal_path(
        journal_dir,
        RECOVERY_JOURNAL_KIND,
        TARGET_CHAPTER,
    )


def recovery_ledger_path(run_root: Path | str) -> Path:
    """Return the G7M recovery-budget ledger path for this run root."""
    return Path(run_root) / RECOVERY_LEDGER_RELATIVE


def recovery_summary(preflight: Mapping[str, Any]) -> str:
    """Render one zero-provider preflight result as a non-authorizing summary."""
    return "\n".join(
        [
            f"G7M_CH1_STATE_RECOVERY={preflight.get('status')}",
            "BENCHMARK_PROVIDER_CALLS=0",
            "PAID_INFERENCE=NO",
            "CREDENTIAL_ACCESS=NO",
            "API_KEY_ACCESS=NO",
            "PROVIDER_CLIENT_CONSTRUCTED=NO",
            "PAID_EXECUTION_AUTHORIZED=NO",
            "CHAPTER_1_TRANSLATION_RETRY=NO",
            "CHAPTER_2_EXECUTION=NO",
            "HISTORICAL_EVIDENCE_MODIFIED=NO",
            "REQUIRES_FRESH_OPERATOR_AUTHORIZATION=YES",
            "PLAN_REQUIRES_REMATERIALIZATION="
            + (
                "YES"
                if preflight.get("plan_requires_rematerialization")
                else "NO"
            ),
            f"G7M_REVISION={G7M_REVISION}",
            f"PLAN_SHA256={preflight.get('plan_sha256')}",
            f"RECOVERY_GENERATION_ID={preflight.get('recovery_generation_id')}",
            f"RECOVERY_BINDING_SHA256={preflight.get('recovery_binding_sha256')}",
            f"RUN_ID={preflight.get('run_id')}",
            f"NOVEL_ID={preflight.get('novel_id')}",
            f"MODEL_ID={preflight.get('model_id')}",
            f"CHAPTER={preflight.get('chapter')}",
            "LOGICAL_OPERATION="
            f"{preflight.get('logical_operation', RECOVERY_LOGICAL_OPERATION)}",
            "CORRECTED_AUXILIARY_BUDGET="
            f"{preflight.get('corrected_auxiliary_budget')}",
            "MAX_NEW_PROVIDER_SENDS="
            f"{preflight.get('maximum_new_provider_sends')}",
            "MAX_CREDIT_EQUIVALENT_USD="
            f"{preflight.get('maximum_credit_equivalent_usd')}",
            "JOURNAL_KIND="
            f"{preflight.get('journal_kind', RECOVERY_JOURNAL_KIND)}",
            f"RECOVERY_LEDGER={preflight.get('recovery_ledger_path')}",
            "CONSUMED_AUTHORIZATION_REUSABLE=NO",
        ]
    )


# ---------------------------------------------------------------------------
# Recovery budget ledger (own path, schema and recovery identity)
# ---------------------------------------------------------------------------


def _ledger_recovery_binding(preflight: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recovery_generation_id": preflight["recovery_generation_id"],
        "recovery_binding_sha256": preflight["recovery_binding_sha256"],
        "plan_sha256": preflight["plan_sha256"],
        "run_id": preflight["run_id"],
        "novel_id": preflight["novel_id"],
        "model_id": TARGET_MODEL_ID,
        "target_chapter": TARGET_CHAPTER,
    }


class RecoveryBudgetLedger:
    """Persist recovery new-send count and accepted credit-equivalent cost.

    The state machine mirrors the frozen G7F ``InvocationBudgetLedger`` but with
    its own path (``data/g7m_recovery_ledgers/0001.json``), its own schema and
    revision, and its own recovery identity binding. It never reads or writes the
    historical ``data/g7f_invocation_ledgers/0001.json``.
    """

    def __init__(
        self,
        *,
        path: Path,
        document: Mapping[str, Any],
        clock: Clock,
    ):
        self.path = Path(path)
        self.document = copy.deepcopy(dict(document))
        self.clock = clock
        self._validate()

    @classmethod
    def open_or_create(
        cls,
        *,
        path: Path | str,
        preflight: Mapping[str, Any],
        clock: Clock | None = None,
        allow_create: bool = True,
    ) -> "RecoveryBudgetLedger":
        use_clock = _use_clock(clock)
        target = Path(path)
        binding = _ledger_recovery_binding(preflight)

        if target.is_file():
            ledger = cls(
                path=target,
                document=_read_object(target, "G7M recovery ledger"),
                clock=use_clock,
            )
            ledger._assert_recovery_binding(binding)
            return ledger

        if not allow_create:
            raise G7MRecoveryError(
                "the G7M recovery ledger is missing for an existing "
                "recovery response"
            )

        now = _now_utc(use_clock)
        document = {
            "schema_version": RECOVERY_LEDGER_SCHEMA_VERSION,
            "revision": RECOVERY_LEDGER_REVISION,
            "logical_operation": RECOVERY_LOGICAL_OPERATION,
            **binding,
            "maximum_new_provider_sends": preflight[
                "maximum_new_provider_sends"
            ],
            "maximum_credit_equivalent_usd": preflight[
                "maximum_credit_equivalent_usd"
            ],
            "new_provider_sends_started": 0,
            "accepted_credit_equivalent_usd": "0",
            "records": [],
            "created_utc": now.isoformat(),
            "updated_utc": now.isoformat(),
        }
        document["ledger_sha256"] = _sha256_json(document)
        ledger = cls(path=target, document=document, clock=use_clock)
        ledger._persist()
        return ledger

    def _validate(self) -> None:
        value = self.document
        if value.get("schema_version") != RECOVERY_LEDGER_SCHEMA_VERSION:
            raise G7MRecoveryError("unsupported G7M recovery ledger schema")
        if value.get("revision") != RECOVERY_LEDGER_REVISION:
            raise G7MRecoveryError("unsupported G7M recovery ledger revision")
        if value.get("logical_operation") != RECOVERY_LOGICAL_OPERATION:
            raise G7MRecoveryError(
                "the recovery ledger binds another logical operation"
            )
        stored = value.get("ledger_sha256")
        if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
            raise G7MRecoveryError("G7M recovery ledger SHA is invalid")
        without_hash = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key != "ledger_sha256"
        }
        if _sha256_json(without_hash) != stored:
            raise G7MRecoveryError(
                "G7M recovery ledger SHA does not recompute"
            )

        _require_sha256(
            value.get("recovery_binding_sha256"),
            "ledger recovery_binding_sha256",
        )
        _require_sha256(value.get("plan_sha256"), "ledger plan_sha256")
        _require_text(
            value.get("recovery_generation_id"),
            "ledger recovery_generation_id",
        )
        _require_text(value.get("run_id"), "ledger run_id")
        _require_text(value.get("novel_id"), "ledger novel_id")
        if value.get("model_id") != TARGET_MODEL_ID:
            raise G7MRecoveryError("the recovery ledger binds another model")
        if value.get("target_chapter") != TARGET_CHAPTER:
            raise G7MRecoveryError("the recovery ledger binds another chapter")

        maximum = _positive_int(
            value.get("maximum_new_provider_sends"),
            "ledger maximum_new_provider_sends",
        )
        if maximum > MAX_NEW_SENDS:
            raise G7MRecoveryError(
                "the recovery send ceiling must never exceed "
                f"{MAX_NEW_SENDS}"
            )
        used = value.get("new_provider_sends_started")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise G7MRecoveryError(
                "ledger new_provider_sends_started is invalid"
            )
        if used > maximum:
            raise G7MRecoveryError("the ledger send count exceeds its ceiling")

        max_cost = Decimal(
            _decimal_text(
                value.get("maximum_credit_equivalent_usd"),
                "ledger maximum_credit_equivalent_usd",
            )
        )
        if max_cost > Decimal(MAX_CE_USD):
            raise G7MRecoveryError(
                f"the recovery credit ceiling must never exceed {MAX_CE_USD}"
            )
        try:
            accepted = Decimal(
                str(value.get("accepted_credit_equivalent_usd"))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise G7MRecoveryError("ledger accepted cost is invalid") from exc
        if not accepted.is_finite() or accepted < 0:
            raise G7MRecoveryError("ledger accepted cost is invalid")

        records = value.get("records")
        if not isinstance(records, list):
            raise G7MRecoveryError("ledger records must be a list")
        if accepted > max_cost:
            terminal = records[-1] if records else None
            if not isinstance(terminal, Mapping) or terminal.get(
                "status"
            ) != "accepted_cost_ceiling_exceeded":
                raise G7MRecoveryError(
                    "ledger accepted cost exceeds its ceiling without "
                    "terminal over-ceiling evidence"
                )
        if used != len(records):
            raise G7MRecoveryError(
                "the ledger send count does not match its persisted records"
            )
        allowed_statuses = {
            "started",
            "transport_exception",
            "accepted",
            "accepted_cost_ceiling_exceeded",
        }
        for expected_index, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                raise G7MRecoveryError("ledger send record must be an object")
            if record.get("send_index") != expected_index:
                raise G7MRecoveryError("ledger send indexes are not contiguous")
            if record.get("status") not in allowed_statuses:
                raise G7MRecoveryError(
                    "the ledger contains an unknown send status"
                )
            if record.get("logical_operation") not in (
                ALLOWED_RECOVERY_OPERATIONS
                | {LIFECYCLE_OPERATION_STATE_JSON_REPAIR}
            ):
                raise G7MRecoveryError(
                    "the ledger contains a send outside the recovery envelope"
                )
            _require_sha256(
                record.get("request_sha256"),
                "ledger record request_sha256",
            )

    def _assert_recovery_binding(self, binding: Mapping[str, Any]) -> None:
        for field, wanted in dict(binding).items():
            if self.document.get(field) != wanted:
                raise G7MRecoveryError(
                    "the persisted recovery ledger binds another recovery "
                    f"identity: {field}"
                )

    @property
    def new_provider_sends_started(self) -> int:
        return int(self.document["new_provider_sends_started"])

    @property
    def accepted_cost(self) -> Decimal:
        return Decimal(
            str(self.document["accepted_credit_equivalent_usd"])
        )

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.document["updated_utc"] = _now_utc(self.clock).isoformat()
        self.document["ledger_sha256"] = _sha256_json(
            {
                key: copy.deepcopy(item)
                for key, item in self.document.items()
                if key != "ledger_sha256"
            }
        )
        atomic_write_json(self.path, self.document)

    def _cost_bound(
        self,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> tuple[Decimal, dict[str, Any]]:
        if not isinstance(request_body, Mapping):
            raise G7MRecoveryError("the provider request body must be a mapping")
        max_output = _positive_int(
            request_body.get("max_completion_tokens"),
            "provider max_completion_tokens",
        )
        serialized_bytes = len(
            _canonical_json(dict(request_body)).encode("utf-8")
        )
        prompt_upper = serialized_bytes + PROMPT_TOKEN_SAFETY_OVERHEAD
        try:
            snapshot = g7d_pricing_snapshot(
                self.document["model_id"],
                request_started_utc,
            )
        except Exception as exc:  # noqa: BLE001 - frozen pricing policy
            raise G7MRecoveryError(
                "the frozen G7D pricing snapshot could not be resolved"
            ) from exc
        miss_rate = Decimal(str(snapshot["cache_miss_rate_per_million"]))
        output_rate = Decimal(str(snapshot["output_rate_per_million"]))
        bound = (
            Decimal(prompt_upper) * miss_rate
            + Decimal(max_output) * output_rate
        ) / Decimal(1_000_000)
        return bound, {
            **snapshot,
            "serialized_request_utf8_bytes": serialized_bytes,
            "prompt_token_upper_bound": prompt_upper,
            "max_completion_tokens": max_output,
        }

    def preflight_next_send(
        self,
        *,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        if self.new_provider_sends_started >= self.document[
            "maximum_new_provider_sends"
        ]:
            raise G7MRecoveryError(
                "the G7M recovery new Provider-send ceiling is reached"
            )
        bound, pricing = self._cost_bound(request_body, request_started_utc)
        maximum = Decimal(str(self.document["maximum_credit_equivalent_usd"]))
        remaining = maximum - self.accepted_cost
        if bound > remaining:
            raise G7MRecoveryError(
                "the G7M credit-equivalent ceiling would be unsafe for the "
                "next recovery request: "
                f"remaining={_decimal_text(remaining, 'remaining')}, "
                f"conservative_bound={_decimal_text(bound, 'bound')}"
            )
        return {
            "conservative_cost_bound_usd": _decimal_text(bound, "bound"),
            "remaining_credit_equivalent_usd": _decimal_text(
                remaining, "remaining"
            ),
            "pricing": pricing,
        }

    def begin_send(
        self,
        *,
        logical_operation: str,
        chapter: int,
        request_sha256: str,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        request_sha256 = _require_sha256(request_sha256, "request_sha256")
        if logical_operation not in (
            ALLOWED_RECOVERY_OPERATIONS
            | {LIFECYCLE_OPERATION_STATE_JSON_REPAIR}
        ):
            raise G7MRecoveryError(
                "the recovery envelope may send only the recovery operation "
                "and one bounded JSON repair"
            )
        if chapter not in ALLOWED_RECOVERY_CHAPTERS:
            raise G7MRecoveryError(
                "the recovery envelope may send only Chapter 1"
            )
        preview = self.preflight_next_send(
            request_body=request_body,
            request_started_utc=request_started_utc,
        )
        send_index = self.new_provider_sends_started + 1
        if isinstance(request_started_utc, str):
            started = request_started_utc
        else:
            started = request_started_utc.astimezone(timezone.utc).isoformat()
        record = {
            "send_index": send_index,
            "logical_operation": logical_operation,
            "chapter": chapter,
            "request_sha256": request_sha256,
            "request_started_utc": started,
            "status": "started",
            "conservative_cost_bound_usd": preview[
                "conservative_cost_bound_usd"
            ],
            "accepted_cost_usd": "0",
            "stats": {},
            "error": {},
        }
        self.document["new_provider_sends_started"] = send_index
        self.document["records"].append(record)
        self._persist()
        return copy.deepcopy(record)

    def _record_for(
        self, send_index: int, request_sha256: str
    ) -> dict[str, Any]:
        if (
            isinstance(send_index, bool)
            or not isinstance(send_index, int)
            or send_index < 1
            or send_index > len(self.document["records"])
        ):
            raise G7MRecoveryError("invalid G7M recovery send index")
        record = self.document["records"][send_index - 1]
        if record.get("request_sha256") != request_sha256:
            raise G7MRecoveryError(
                "the G7M recovery send record request identity changed"
            )
        return record

    def record_transport_exception(
        self,
        *,
        send_index: int,
        request_sha256: str,
        error: BaseException,
    ) -> None:
        record = self._record_for(send_index, request_sha256)
        if record.get("status") != "started":
            raise G7MRecoveryError(
                "the transport exception record is not in started state"
            )
        record["status"] = "transport_exception"
        record["error"] = {
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }
        self._persist()

    def record_accepted_usage(
        self,
        *,
        send_index: int,
        request_sha256: str,
        usage: Any,
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        record = self._record_for(send_index, request_sha256)
        if record.get("status") != "started":
            raise G7MRecoveryError(
                "the accepted usage record is not in started state"
            )
        try:
            stats = account_provider_usage(
                self.document["model_id"],
                usage,
                request_started_utc,
            )
        except Exception as exc:  # noqa: BLE001 - frozen G7D accounting
            raise G7MRecoveryError(
                "the frozen G7D accounting could not price the accepted "
                "recovery usage"
            ) from exc

        # G7D accounting stays authoritative and is priced from the total
        # completion tokens only. A Provider-reported reasoning subtotal is a
        # diagnostic: an untrustworthy subtotal is never persisted.
        reasoning_outcome = g7j.reasoning_diagnostic_outcome(
            completion_tokens=stats["completion_tokens"],
            reasoning_tokens=getattr(usage, "reasoning_tokens", None),
            reasoning_tokens_exposed=getattr(
                usage, "reasoning_tokens_exposed", False
            ),
        )
        if reasoning_outcome["classification"] == g7j.REASONING_DIAGNOSTIC_OK:
            stats = g7j.augment_accounting_stats_with_reasoning(
                stats,
                reasoning_tokens=getattr(usage, "reasoning_tokens", None),
                reasoning_tokens_exposed=getattr(
                    usage, "reasoning_tokens_exposed", False
                ),
            )

        actual = Decimal(str(stats["rate_correct_cost_usd"]))
        projected = self.accepted_cost + actual
        maximum = Decimal(str(self.document["maximum_credit_equivalent_usd"]))
        record["accepted_cost_usd"] = _decimal_text(actual, "accepted cost")
        record["stats"] = copy.deepcopy(stats)
        if projected > maximum:
            record["status"] = "accepted_cost_ceiling_exceeded"
            self.document["accepted_credit_equivalent_usd"] = _decimal_text(
                projected, "projected cost"
            )
            self._persist()
            raise G7MRecoveryError(
                "the accepted recovery usage exceeded the credit-equivalent "
                "ceiling; no further recovery work is allowed"
            )
        record["status"] = "accepted"
        self.document["accepted_credit_equivalent_usd"] = _decimal_text(
            projected, "projected cost"
        )
        self._persist()
        return copy.deepcopy(stats)


# ---------------------------------------------------------------------------
# Preflight (zero provider, zero credential, zero client)
# ---------------------------------------------------------------------------


def _crosscheck_plan_identity(
    *,
    plan: Mapping[str, Any],
    report: Mapping[str, Any],
    evidence_root: Path | str,
    run_id: str,
    expected_plan_sha256: str,
    expected_recovery_generation_id: str,
    expected_recovery_binding_sha256: str,
) -> dict[str, Any]:
    if report.get("plan_sha256") != expected_plan_sha256:
        raise G7MRecoveryError(
            "the materialized G7L recovery plan SHA does not match the "
            "operator-supplied expected plan SHA"
        )
    if plan.get("plan_sha256") != expected_plan_sha256:
        raise G7MRecoveryError("the recovery plan file changed after loading")
    if report.get("recovery_generation_id") != expected_recovery_generation_id:
        raise G7MRecoveryError(
            "the materialized recovery generation id does not match the "
            "operator-supplied expected generation id"
        )
    if plan.get("recovery_generation_id") != expected_recovery_generation_id:
        raise G7MRecoveryError("the recovery generation id changed after loading")
    if report.get("recovery_binding_sha256") != (
        expected_recovery_binding_sha256
    ):
        raise G7MRecoveryError(
            "the materialized recovery binding does not match the "
            "operator-supplied expected recovery binding"
        )
    if plan.get("recovery_binding_sha256") != expected_recovery_binding_sha256:
        raise G7MRecoveryError("the recovery binding changed after loading")
    if report.get("chapter") != TARGET_CHAPTER:
        raise G7MRecoveryError("the recovery plan must target Chapter 1")
    if report.get("model_id") != TARGET_MODEL_ID:
        raise G7MRecoveryError("the recovery plan must target the frozen model")
    if report.get("corrected_auxiliary_budget") != RECOVERY_MAX_COMPLETION_TOKENS:
        raise G7MRecoveryError(
            "the recovery plan must carry the corrected auxiliary ceiling "
            f"{RECOVERY_MAX_COMPLETION_TOKENS}"
        )
    sends = report.get("maximum_new_provider_sends")
    if (
        isinstance(sends, bool)
        or not isinstance(sends, int)
        or sends < 1
        or sends > MAX_NEW_SENDS
    ):
        raise G7MRecoveryError(
            f"the recovery plan send ceiling must be 1..{MAX_NEW_SENDS}"
        )
    if Decimal(
        _decimal_text(
            report.get("maximum_credit_equivalent_usd"),
            "plan maximum_credit_equivalent_usd",
        )
    ) != Decimal(MAX_CE_USD):
        raise G7MRecoveryError(
            f"the recovery plan credit ceiling must be {MAX_CE_USD}"
        )
    if plan.get("run_id") != run_id:
        raise G7MRecoveryError("the recovery plan binds another run id")
    if RECOVERY_JOURNAL_KIND != g7l.RECOVERY_OPERATION_ID:
        raise G7MRecoveryError(
            "the G7M recovery journal namespace must stay the reviewed G7L "
            "recovery operation id"
        )

    authorization = plan.get("authorization")
    if not isinstance(authorization, Mapping):
        raise G7MRecoveryError("the recovery plan lacks its authorization")
    if authorization.get("historical_consumed_authorization_reusable") is not (
        False
    ):
        raise G7MRecoveryError(
            "the consumed historical authorization must be recorded as "
            "non-reusable"
        )
    fields = authorization.get("required_future_authorization_fields")
    if not isinstance(fields, list):
        raise G7MRecoveryError(
            "the recovery plan must list the required fresh authorization "
            "fields"
        )
    if set(fields) - {RECOVERY_AUTHORIZATION_OPERATOR_FIELD} != set(
        RECOVERY_AUTHORIZATION_FIELDS
    ):
        raise G7MRecoveryError(
            "the recovery plan's required fresh authorization fields changed"
        )

    try:
        incident = g7l.verify_historical_incident(evidence_root, run_id)
    except g7l.G7LRecoveryError as exc:
        raise G7MRecoveryError(
            f"the historical Chapter-1 incident evidence changed: {exc}"
        ) from exc
    except g7j.G7JPolicyError as exc:
        raise G7MRecoveryError(
            f"the historical auxiliary classification changed: {exc}"
        ) from exc

    observed = plan.get("observed_evidence")
    if not isinstance(observed, Mapping) or dict(observed) != incident:
        raise G7MRecoveryError(
            "the recovery plan's observed evidence no longer matches the "
            "re-derived historical incident byte-for-byte"
        )
    payload = plan.get("recovery_binding_payload")
    if not isinstance(payload, Mapping):
        raise G7MRecoveryError("the recovery plan lacks its binding payload")

    pinned = {
        "run_id": run_id,
        "novel_id": incident["novel_id"],
        "chapter": TARGET_CHAPTER,
        "model_id": TARGET_MODEL_ID,
        "source_sha256": incident["source_sha256"],
        "progress": incident["progress"],
        "state_extraction_journal_record_sha256": incident[
            "state_extraction_journal_record_sha256"
        ],
        "state_json_repair_journal_record_sha256": incident[
            "state_json_repair_journal_record_sha256"
        ],
        "corrected_auxiliary_budget": RECOVERY_MAX_COMPLETION_TOKENS,
    }
    for field, wanted in pinned.items():
        if payload.get(field) != wanted:
            raise G7MRecoveryError(
                "the recovery binding payload changed for " + field
            )
    if payload.get("max_sends") != sends:
        raise G7MRecoveryError(
            "the recovery binding payload send ceiling changed"
        )
    if payload.get("max_ce_usd") != report.get("maximum_credit_equivalent_usd"):
        raise G7MRecoveryError(
            "the recovery binding payload credit ceiling changed"
        )
    for field in ("committed_translation_sha256", "qa_sha256"):
        value = payload.get(field)
        if not isinstance(value, str) or not _matches_pinned_sha256(
            incident[field], value
        ):
            raise G7MRecoveryError(
                "the recovery binding payload changed for " + field
            )
    incident_degraded = incident["degraded_state"]
    expected_degraded = {
        "glossary": {},
        "continuity_summary": incident_degraded["continuity_summary"],
        "issues": list(incident_degraded["issues"]),
    }
    if payload.get("degraded_state") != expected_degraded:
        raise G7MRecoveryError(
            "the recovery binding payload degraded state changed"
        )
    ledger_record = payload.get("original_ch1_ledger_record")
    if not isinstance(ledger_record, Mapping):
        raise G7MRecoveryError(
            "the recovery binding payload lacks the original ledger record"
        )
    expected_ledger_record = {
        "plan_sha256": g7l.ORIGINAL_LEDGER_PLAN_SHA256,
        "approval_token": g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN,
        "new_provider_sends_started": incident["new_provider_sends_started"],
        "accepted_credit_equivalent_usd": incident[
            "accepted_credit_equivalent_usd"
        ],
        "ledger_sha256": incident["ledger_sha256"],
        "ledger_file_sha256": incident["ledger_file_sha256"],
    }
    if dict(ledger_record) != expected_ledger_record:
        raise G7MRecoveryError(
            "the recovery binding payload original Chapter-1 ledger record "
            "changed"
        )
    if incident["ledger_plan_sha256"] != g7l.ORIGINAL_LEDGER_PLAN_SHA256:
        raise G7MRecoveryError("the historical consumed plan SHA changed")
    if incident["ledger_approval_token"] != g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN:
        raise G7MRecoveryError("the historical consumed approval token changed")

    return incident


def _validate_recovery_plan_envelope(
    plan: Mapping[str, Any],
    *,
    plan_path: Path | str,
    evidence_root: Path | str,
    run_id: str,
    expected_plan_sha256: str,
    expected_recovery_generation_id: str,
    expected_recovery_binding_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(verified report, re-derived historical incident)``.

    The materialized plan is re-verified with the frozen G7L verifier (reuse,
    never reimplementation) and every pinned field is then cross-checked against
    the re-derived historical incident byte-for-byte.
    """
    try:
        report = g7l.verify_recovery_plan(plan_path)
    except g7l.G7LRecoveryError as exc:
        raise G7MRecoveryError(
            f"the G7L recovery plan failed verification: {exc}"
        ) from exc
    except (g7j.G7JPolicyError, KeyError, TypeError, ValueError) as exc:
        raise G7MRecoveryError(
            f"the G7L recovery plan is structurally unusable: {exc}"
        ) from exc
    incident = _crosscheck_plan_identity(
        plan=plan,
        report=report,
        evidence_root=evidence_root,
        run_id=run_id,
        expected_plan_sha256=expected_plan_sha256,
        expected_recovery_generation_id=expected_recovery_generation_id,
        expected_recovery_binding_sha256=expected_recovery_binding_sha256,
    )
    return report, incident


def _plan_requires_rematerialization(plan: Mapping[str, Any]) -> bool:
    """Return True when the plan hides a paid step inside the recovery envelope.

    The frozen G7L plan expresses the normal Chapter-1 glossary review as a
    SEPARATE, unblocked, separately-authorized post step, so the recovery's own
    envelope is exactly state extraction plus at most one bounded JSON repair.
    Any plan that folds another paid operation into this envelope -- a glossary
    review marked ``blocked_by_this_plan``, a missing glossary-review step, or a
    non-``state_extraction`` operation -- must fail closed instead of hiding a
    send.
    """
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        return True
    for operation in operations:
        if not isinstance(operation, Mapping):
            return True
        if operation.get("logical_operation") not in (
            ALLOWED_RECOVERY_OPERATIONS
        ):
            return True
        if operation.get("journal_kind") != RECOVERY_JOURNAL_KIND:
            return True
        if operation.get("chapter") not in ALLOWED_RECOVERY_CHAPTERS:
            return True

    pipeline = plan.get("post_recovery_pipeline")
    if not isinstance(pipeline, Mapping):
        return True
    steps = pipeline.get("steps")
    if not isinstance(steps, list):
        return True
    review_steps = [
        step
        for step in steps
        if isinstance(step, Mapping)
        and (
            step.get("step") == "normal_chapter_1_glossary_review"
            or step.get("logical_operation") == "glossary_review"
        )
    ]
    if len(review_steps) != 1:
        return True
    review = review_steps[0]
    if review.get("blocked_by_this_plan") is not False:
        return True
    if review.get("requires_separate_authorization") is not True:
        return True
    for step in steps:
        if not isinstance(step, Mapping):
            return True
        if step.get("step") == "recovered_state_checkpoint_apply":
            continue
        if step.get("blocked_by_this_plan") is True:
            return True
    return False


def _resolve_source_chapter_text(
    run_root: Path,
    *,
    chapters_file_sha256: str | None = None,
) -> tuple[str, str]:
    """Return ``(chapter text, chapters-file sha256)`` from the run-root library."""
    path = run_root / SOURCE_CHAPTER_LIBRARY_RELATIVE
    if not path.is_file():
        raise G7MRecoveryError(
            "the run root lacks its production chapter library: " + str(path)
        )
    file_sha256 = _sha256_file(path)
    if chapters_file_sha256 is not None and file_sha256 != chapters_file_sha256:
        raise G7MRecoveryError(
            "the production chapter library changed after the preflight"
        )
    try:
        library = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7MRecoveryError(
            "the production chapter library is unreadable"
        ) from exc
    if not isinstance(library, list):
        raise G7MRecoveryError(
            "the production chapter library must be a JSON array"
        )
    matches = [
        entry
        for entry in library
        if isinstance(entry, Mapping)
        and entry.get("chapter") == TARGET_CHAPTER
        and entry.get("production_index", TARGET_CHAPTER) == TARGET_CHAPTER
    ]
    if len(matches) != 1:
        raise G7MRecoveryError(
            "the production chapter library must hold exactly one committed "
            f"Chapter-{TARGET_CHAPTER} entry"
        )
    text = matches[0].get("text")
    if not isinstance(text, str) or not text.strip():
        raise G7MRecoveryError(
            "the committed Chapter-1 source text is missing from the chapter "
            "library"
        )
    return text, file_sha256


def preflight_ch1_state_recovery(
    *,
    evidence_root: Path | str,
    run_id: str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_recovery_generation_id: str,
    expected_recovery_binding_sha256: str,
) -> dict[str, Any]:
    """Verify every recovery precondition with ZERO Provider or credential access.

    This function constructs no Provider client, reads no credential, and makes
    no Provider call. It returns a machine-readable, non-authorizing summary that
    a fresh operator authorization must bind. Any mismatch raises
    :class:`G7MRecoveryError` before any credential could be touched.
    """
    assert_no_translation_or_chapter2_path()
    _require_sha256(expected_plan_sha256, "expected_plan_sha256")
    _require_text(expected_recovery_generation_id, "expected generation id")
    _require_sha256(
        expected_recovery_binding_sha256, "expected recovery binding"
    )
    _require_text(run_id, "run_id")

    path = Path(plan_path)
    if not path.is_file():
        raise G7MRecoveryError(f"the G7L recovery plan is missing: {path}")
    plan = _read_object(path, "G7L recovery plan")
    report, incident = _validate_recovery_plan_envelope(
        plan,
        plan_path=path,
        evidence_root=evidence_root,
        run_id=run_id,
        expected_plan_sha256=expected_plan_sha256,
        expected_recovery_generation_id=expected_recovery_generation_id,
        expected_recovery_binding_sha256=expected_recovery_binding_sha256,
    )

    if _plan_requires_rematerialization(plan):
        raise G7MRecoveryError(
            f"{REMATERIALIZATION_BLOCKER}: the recovery plan requires a paid "
            "operation inside the recovery envelope that G7M must never hide; "
            "rematerialize the plan so the normal Chapter-1 glossary review "
            "stays a separate, unblocked, separately authorized step"
        )

    run_root = Path(incident["run_root"])
    if not run_root.is_dir():
        raise G7MRecoveryError(
            f"the historical run root is missing: {run_root}"
        )
    config = _read_object(run_root / g7l.RUN_CONFIG_RELATIVE, "run config")
    novel_id = config.get("id")
    if not isinstance(novel_id, str) or novel_id != incident["novel_id"]:
        raise G7MRecoveryError(
            "the run config novel id differs from the verified incident"
        )
    profile_id = config.get("profile")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise G7MRecoveryError("the run config lacks a novel profile id")

    try:
        provider_model_id = live_provider_capability(
            TARGET_MODEL_ID
        ).provider_model_id
    except Exception as exc:  # noqa: BLE001 - frozen capability table
        raise G7MRecoveryError(
            "the live Provider capability for the frozen recovery model could "
            "not be resolved"
        ) from exc
    if not isinstance(provider_model_id, str) or not provider_model_id.strip():
        raise G7MRecoveryError(
            "the frozen recovery model has no Provider model identity"
        )

    source_text, chapters_file_sha256 = _resolve_source_chapter_text(run_root)
    committed_path = run_root / g7l.COMMITTED_CHAPTER_RELATIVE
    # Byte-exact read: the recovered text must re-encode to the raw-file SHA that
    # the verified incident bound, so no newline translation may be applied.
    committed_translation_text = _read_text_exact_bytes(
        committed_path, "committed Chapter-1 translation"
    )
    committed_translation_sha256 = _sha256_file(committed_path)
    if committed_translation_sha256 != incident["committed_translation_sha256"]:
        raise G7MRecoveryError(
            "the committed Chapter-1 translation bytes changed after the "
            "incident verification"
        )
    if _sha256_text(committed_translation_text) != committed_translation_sha256:
        raise G7MRecoveryError(
            "the committed Chapter-1 text does not re-encode to its bound bytes"
        )

    degraded = incident["degraded_state"]
    if degraded.get("glossary") != {}:
        raise G7MRecoveryError(
            "the recovery may only start from the empty degraded glossary"
        )

    sends = report["maximum_new_provider_sends"]
    cost_text = _decimal_text(
        report["maximum_credit_equivalent_usd"],
        "plan maximum_credit_equivalent_usd",
    )
    return {
        "status": PREFLIGHT_OK_STATUS,
        "schema_version": G7M_SCHEMA_VERSION,
        "revision": G7M_REVISION,
        "plan_sha256": report["plan_sha256"],
        "recovery_generation_id": report["recovery_generation_id"],
        "recovery_binding_sha256": report["recovery_binding_sha256"],
        "run_id": run_id,
        "novel_id": incident["novel_id"],
        "profile_id": profile_id,
        "chapter": TARGET_CHAPTER,
        "model_id": TARGET_MODEL_ID,
        "provider_model_id": provider_model_id,
        "logical_operation": RECOVERY_LOGICAL_OPERATION,
        "journal_kind": RECOVERY_JOURNAL_KIND,
        "corrected_auxiliary_budget": RECOVERY_MAX_COMPLETION_TOKENS,
        "maximum_new_provider_sends": sends,
        "maximum_credit_equivalent_usd": cost_text,
        "committed_translation_sha256": incident[
            "committed_translation_sha256"
        ],
        "committed_translation_text": committed_translation_text,
        "qa_sha256": incident["qa_sha256"],
        "source_sha256": incident["source_sha256"],
        "source_chapter_sha256": _sha256_text(source_text),
        "source_chapter_text": source_text,
        "source_chapter_library_relative": SOURCE_CHAPTER_LIBRARY_RELATIVE,
        "source_chapter_library_file_sha256": chapters_file_sha256,
        "progress": {
            "completed": [TARGET_CHAPTER],
            "last_completed": TARGET_CHAPTER,
            "next_chapter": TARGET_CHAPTER + 1,
        },
        "progress_file_sha256": incident["progress_file_sha256"],
        "degraded_state": {
            "glossary": {},
            "glossary_file_sha256": degraded["glossary_file_sha256"],
            "continuity_summary": degraded["continuity_summary"],
            "continuity_file_sha256": degraded["continuity_file_sha256"],
            "issues": list(degraded["issues"]),
            "issues_file_sha256": degraded["issues_file_sha256"],
        },
        "original_journals": {
            "state_extraction": {
                "relative": incident["state_extraction_journal_relative"],
                "file_sha256": incident[
                    "state_extraction_journal_file_sha256"
                ],
                "record_sha256": incident[
                    "state_extraction_journal_record_sha256"
                ],
                "classification": incident[
                    "state_extraction_classification"
                ],
            },
            "state_json_repair": {
                "relative": incident["state_json_repair_journal_relative"],
                "file_sha256": incident[
                    "state_json_repair_journal_file_sha256"
                ],
                "record_sha256": incident[
                    "state_json_repair_journal_record_sha256"
                ],
                "received_empty_input": incident[
                    "state_json_repair_received_empty_input"
                ],
            },
        },
        "original_ledger": {
            "relative": incident["ledger_relative"],
            "file_sha256": incident["ledger_file_sha256"],
            "ledger_sha256": incident["ledger_sha256"],
            "plan_sha256": incident["ledger_plan_sha256"],
            "approval_token": incident["ledger_approval_token"],
            "new_provider_sends_started": incident[
                "new_provider_sends_started"
            ],
            "accepted_credit_equivalent_usd": incident[
                "accepted_credit_equivalent_usd"
            ],
        },
        "consumed_authorization_reusable": False,
        "required_fresh_authorization_fields": list(
            plan["authorization"]["required_future_authorization_fields"]
        ),
        "plan_path": str(path),
        "plan_requires_rematerialization": False,
        "run_root": str(run_root),
        "evidence_root": str(incident["evidence_root"]),
        "journal_dir": str(run_root / g7l.JOURNAL_DIR_RELATIVE),
        "recovery_journal_path": str(
            recovery_journal_path(run_root / g7l.JOURNAL_DIR_RELATIVE)
        ),
        "recovery_ledger_path": str(recovery_ledger_path(run_root)),
        "provider_calls": 0,
        "provider_client_constructed": False,
        "credential_access": False,
        "chapter_1_translation_retry": False,
        "chapter_1_retranslated": False,
        "chapter_2_execution": False,
        "historical_evidence_modified": False,
        "state_fabricated": False,
        "apply_performed": False,
    }


def _validated_preflight(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated copy of one G7M preflight result."""
    if not isinstance(preflight, Mapping):
        raise G7MRecoveryError("a G7M preflight mapping is required")
    value = copy.deepcopy(dict(preflight))
    if value.get("status") != PREFLIGHT_OK_STATUS:
        raise G7MRecoveryError("the G7M preflight did not report PREFLIGHT_OK")
    if value.get("revision") != G7M_REVISION:
        raise G7MRecoveryError("the G7M preflight binds another revision")
    if value.get("provider_calls") != 0:
        raise G7MRecoveryError("the G7M preflight must report zero Provider calls")
    if value.get("provider_client_constructed") is not False:
        raise G7MRecoveryError(
            "the G7M preflight must never construct a Provider client"
        )
    if value.get("credential_access") is not False:
        raise G7MRecoveryError(
            "the G7M preflight must never access a credential"
        )
    if value.get("plan_requires_rematerialization") is not False:
        raise G7MRecoveryError(
            f"{REMATERIALIZATION_BLOCKER}: the recovery plan is not usable "
            "as-is"
        )
    if value.get("chapter") not in ALLOWED_RECOVERY_CHAPTERS:
        raise G7MRecoveryError(
            "the recovery envelope is limited to Chapter 1"
        )
    if value.get("logical_operation") not in ALLOWED_RECOVERY_OPERATIONS:
        raise G7MRecoveryError(
            "the recovery envelope is limited to the state-extraction "
            "operation"
        )
    if value.get("model_id") != TARGET_MODEL_ID:
        raise G7MRecoveryError("the recovery must target the frozen model")
    if value.get("corrected_auxiliary_budget") != RECOVERY_MAX_COMPLETION_TOKENS:
        raise G7MRecoveryError(
            "the recovery must carry the corrected auxiliary ceiling "
            f"{RECOVERY_MAX_COMPLETION_TOKENS}"
        )
    sends = value.get("maximum_new_provider_sends")
    if (
        isinstance(sends, bool)
        or not isinstance(sends, int)
        or sends < 1
        or sends > MAX_NEW_SENDS
    ):
        raise G7MRecoveryError(
            f"the recovery send ceiling must be 1..{MAX_NEW_SENDS}"
        )
    if Decimal(
        _decimal_text(
            value.get("maximum_credit_equivalent_usd"),
            "maximum_credit_equivalent_usd",
        )
    ) != Decimal(MAX_CE_USD):
        raise G7MRecoveryError(
            f"the recovery credit ceiling must be {MAX_CE_USD}"
        )
    for field in (
        "plan_sha256",
        "recovery_binding_sha256",
        "committed_translation_sha256",
        "qa_sha256",
        "source_sha256",
        "source_chapter_sha256",
        "source_chapter_library_file_sha256",
        "progress_file_sha256",
    ):
        _require_sha256(value.get(field), f"preflight {field}")
    for field in (
        "recovery_generation_id",
        "run_id",
        "novel_id",
        "profile_id",
        "provider_model_id",
        "plan_path",
        "run_root",
        "evidence_root",
        "journal_dir",
        "source_chapter_text",
        "committed_translation_text",
    ):
        _require_text(value.get(field), f"preflight {field}")
    if _sha256_text(value["source_chapter_text"]) != value[
        "source_chapter_sha256"
    ]:
        raise G7MRecoveryError(
            "the preflight Chapter-1 source text no longer matches its bound "
            "identity"
        )
    if _sha256_text(value["committed_translation_text"]) != value[
        "committed_translation_sha256"
    ]:
        raise G7MRecoveryError(
            "the preflight committed Chapter-1 translation no longer matches "
            "its bound identity"
        )
    degraded = value.get("degraded_state")
    if not isinstance(degraded, Mapping) or degraded.get("glossary") != {}:
        raise G7MRecoveryError(
            "the recovery may only start from the empty degraded glossary"
        )
    progress = value.get("progress")
    if progress != {
        "completed": [TARGET_CHAPTER],
        "last_completed": TARGET_CHAPTER,
        "next_chapter": TARGET_CHAPTER + 1,
    }:
        raise G7MRecoveryError(
            "the recovery requires Chapter 1 committed and Chapter 2 next"
        )
    return value


# ---------------------------------------------------------------------------
# Fresh operator authorization
# ---------------------------------------------------------------------------


def validate_fresh_recovery_authorization(
    authorization: Mapping[str, Any],
    *,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the normalized fresh authorization, or raise.

    The consumed historical Chapter-1 executor authorization is never reusable:
    the historical plan SHA and its executor approval token are rejected as an
    active grant anywhere in the fresh authorization.
    """
    assert_no_translation_or_chapter2_path()
    value = _validated_preflight(preflight)
    if not isinstance(authorization, Mapping):
        raise G7MRecoveryError(
            "the fresh recovery authorization must be a mapping"
        )
    if authorization.get(RECOVERY_AUTHORIZATION_OPERATOR_FIELD) is not True:
        raise G7MRecoveryError(
            "the fresh recovery authorization requires an explicit operator "
            "authorization"
        )
    normalized = {
        field: value[field] for field in RECOVERY_AUTHORIZATION_FIELDS
    }
    for field, wanted in normalized.items():
        if authorization.get(field) != wanted:
            raise G7MRecoveryError(
                f"the fresh recovery authorization must bind exactly {field}"
            )
    for field in ("logical_operations", "operations"):
        declared = authorization.get(field)
        if declared in (None, (), []):
            continue
        if list(declared) != [RECOVERY_LOGICAL_OPERATION]:
            raise G7MRecoveryError(
                "the fresh recovery authorization must never authorize a "
                "paid operation other than the Chapter-1 state extraction"
            )
    for path, item in _iter_leaf_values(authorization):
        if not isinstance(item, str) or item not in (
            _CONSUMED_AUTHORIZATION_VALUES
        ):
            continue
        if path and path[-1].startswith(_CONSUMED_NOTE_KEY_PREFIX):
            continue
        raise G7MRecoveryError(
            "the consumed Chapter-1 executor authorization must never be "
            "reused as an active grant: "
            + (".".join(path) or "<root>")
        )
    normalized[RECOVERY_AUTHORIZATION_OPERATOR_FIELD] = True
    normalized["authorization_validated"] = True
    normalized["historical_consumed_authorization_reusable"] = False
    normalized["credential_access_before_authorization"] = False
    normalized["provider_calls"] = 0
    return normalized


# ---------------------------------------------------------------------------
# Recovery request construction
# ---------------------------------------------------------------------------


def _corrected_auxiliary_completion_budget(
    *,
    logical_operation: str,
    requested_tokens: int,
) -> int:
    """Return the corrected GOAT auxiliary ceiling for one recovery request.

    The corrected 4,000 ceiling is DERIVED from the reused G7J GOAT auxiliary
    policy applied to the frozen legacy 3,000 auxiliary baseline recorded by the
    incident. An incoming ceiling is accepted only when it is exactly that frozen
    baseline or exactly the corrected ceiling; any other drift fails closed.
    """
    legacy = g7j.GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
    try:
        corrected = g7j.goat_auxiliary_completion_budget(
            logical_operation=logical_operation,
            requested_tokens=legacy,
        )
    except g7j.G7JPolicyError as exc:
        raise G7MRecoveryError(
            "the frozen G7J GOAT auxiliary policy could not derive the "
            "corrected recovery ceiling"
        ) from exc
    if corrected != RECOVERY_MAX_COMPLETION_TOKENS:
        raise G7MRecoveryError(
            "the corrected GOAT auxiliary ceiling drifted from "
            f"{RECOVERY_MAX_COMPLETION_TOKENS}"
        )
    if requested_tokens not in (legacy, corrected):
        raise G7MRecoveryError(
            "the recovery request completion ceiling drifted: "
            f"{requested_tokens}"
        )
    return corrected


def _recovery_request_body(
    *,
    preflight: Mapping[str, Any],
    logical_operation: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Build one frozen Command Code request body for the recovery envelope."""
    if logical_operation not in (
        ALLOWED_RECOVERY_OPERATIONS | {LIFECYCLE_OPERATION_STATE_JSON_REPAIR}
    ):
        raise G7MRecoveryError(
            "the recovery envelope permits only the Chapter-1 "
            "state-extraction operation plus one bounded JSON repair"
        )
    if preflight.get("chapter") not in ALLOWED_RECOVERY_CHAPTERS:
        raise G7MRecoveryError(
            "the recovery envelope is limited to Chapter 1"
        )
    live = live_provider_capability(TARGET_MODEL_ID)
    if live.provider_model_id != preflight.get("provider_model_id"):
        raise G7MRecoveryError(
            "the recovered Provider model identity changed"
        )
    request_kwargs = {
        "model": live.provider_model_id,
        "messages": copy.deepcopy(messages),
        "max_tokens": RECOVERY_MAX_COMPLETION_TOKENS,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    actual_request = normalize_legacy_request(
        model_id=TARGET_MODEL_ID,
        logical_operation=logical_operation,
        request_kwargs=request_kwargs,
    )
    requested = actual_request.get("max_completion_tokens")
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise G7MRecoveryError(
            "the normalized recovery request carries no completion ceiling"
        )
    corrected = _corrected_auxiliary_completion_budget(
        logical_operation=logical_operation,
        requested_tokens=requested,
    )
    if actual_request.get("max_completion_tokens") != corrected:
        raise G7MRecoveryError(
            "the corrected recovery completion ceiling did not reach the "
            "request body"
        )
    if actual_request.get("reasoning_effort") != TARGET_REASONING_EFFORT:
        raise G7MRecoveryError(
            "the G7B policy reasoning effort did not reach the request body"
        )
    if actual_request.get("model") != live.provider_model_id:
        raise G7MRecoveryError(
            "the recovered request body binds another Provider model"
        )
    return actual_request


def _state_extraction_messages(
    preflight: Mapping[str, Any],
) -> list[dict[str, str]]:
    glossary_terms: list[str] = []
    user_prompt = "\n".join(
        [
            f"CHAPTER: {TARGET_CHAPTER}",
            "EXISTING GLOSSARY TERMS: "
            + json.dumps(glossary_terms, ensure_ascii=False),
            "CHINESE SOURCE:",
            preflight["source_chapter_text"],
            "FINAL ENGLISH:",
            preflight["committed_translation_text"],
        ]
    )
    return [
        {"role": "system", "content": _STATE_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _state_json_repair_messages(malformed_content: str) -> list[dict[str, str]]:
    """Build the bounded repair prompt, which is never fed empty input."""
    if not isinstance(malformed_content, str) or not malformed_content.strip():
        raise G7MRecoveryError(
            "an empty recovery response must never be sent to JSON repair"
        )
    return [
        {"role": "system", "content": _JSON_REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Repair this malformed JSON:\n\n" + malformed_content
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Durable journaled recovery sends
# ---------------------------------------------------------------------------


def _journaled_recovery_send(
    *,
    preflight: Mapping[str, Any],
    ledger: RecoveryBudgetLedger,
    journal_dir: Path | str,
    provider_client: Any,
    clock: Clock,
    repair_of: str | None,
) -> dict[str, Any]:
    """Make exactly one durable, journaled recovery transport attempt."""
    if repair_of is None:
        logical_operation = RECOVERY_LOGICAL_OPERATION
        messages = _state_extraction_messages(preflight)
        journal_translation_sha256 = preflight[
            "committed_translation_sha256"
        ]
        journal_kind = RECOVERY_JOURNAL_KIND
    else:
        logical_operation = LIFECYCLE_OPERATION_STATE_JSON_REPAIR
        messages = _state_json_repair_messages(repair_of)
        # Mirrors the engine's repair slot: the repair request is bound to the
        # malformed input it was fed. An empty input is rejected above, so this
        # digest can never be the empty-input digest.
        journal_translation_sha256 = _sha256_text(repair_of)
        if journal_translation_sha256 == hashlib.sha256(b"").hexdigest():
            raise G7MRecoveryError(
                "an empty recovery response must never be sent to JSON repair"
            )

    actual_request = _recovery_request_body(
        preflight=preflight,
        logical_operation=logical_operation,
        messages=messages,
    )
    provider_identity = build_provider_request_identity(
        request_body=actual_request
    )
    if repair_of is not None:
        journal_kind = (
            f"{RECOVERY_JOURNAL_KIND}_"
            f"{provider_identity.request_sha256[:20]}"
        )
    journal_identity = request_identity(
        operation_kind=journal_kind,
        novel_id=preflight["novel_id"],
        chapter=TARGET_CHAPTER,
        provider=PROVIDER_NAME,
        model=provider_identity.requested_model,
        profile_id=preflight["profile_id"],
        source_sha256=preflight["source_chapter_sha256"],
        translation_sha256=journal_translation_sha256,
        request_payload=actual_request,
    )
    journal_path = paid_request_journal_path(
        journal_dir,
        journal_kind,
        TARGET_CHAPTER,
    )

    existing = load_matching_journal(journal_path, journal_identity)
    preflight_started = _now_utc(clock)
    if existing is None or existing["status"] == "planned":
        ledger.preflight_next_send(
            request_body=actual_request,
            request_started_utc=preflight_started,
        )

    attempt_box: dict[str, Any] = {}
    attempts = {"transport": 0}

    def prepare_attempt() -> dict[str, Any]:
        started = _now_utc(clock)
        snapshot = g7d_pricing_snapshot(TARGET_MODEL_ID, started)
        context = {
            "mode": G7M_REVISION,
            "pricing_version": PRICING_VERSION,
            "pricing_period": snapshot["pricing_period"],
            "request_started_utc": started.isoformat(),
            "request_sha256": provider_identity.request_sha256,
            "recovery_generation_id": preflight["recovery_generation_id"],
            "recovery_binding_sha256": preflight["recovery_binding_sha256"],
            "journal_kind": journal_kind,
            "logical_operation": logical_operation,
        }
        attempt_box.clear()
        attempt_box.update(context)
        return context

    def send_once() -> Any:
        if not attempt_box:
            raise G7MRecoveryError(
                "the recovery attempt context was not prepared"
            )
        record = ledger.begin_send(
            logical_operation=logical_operation,
            chapter=TARGET_CHAPTER,
            request_sha256=provider_identity.request_sha256,
            request_body=actual_request,
            request_started_utc=attempt_box["request_started_utc"],
        )
        attempts["transport"] += 1
        try:
            return send_chat_completion_once(provider_client, actual_request)
        except BaseException as exc:
            ledger.record_transport_exception(
                send_index=record["send_index"],
                request_sha256=provider_identity.request_sha256,
                error=exc,
            )
            raise

    def normalize_response(
        raw_response: Any,
        attempt_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = normalize_provider_response(
            raw_response,
            request_identity=provider_identity,
        )
        accepted = accept_provider_response(provider_identity, normalized)
        records = ledger.document["records"]
        if not records:
            raise G7MRecoveryError(
                "an accepted recovery response has no persisted send record"
            )
        stats = ledger.record_accepted_usage(
            send_index=records[-1]["send_index"],
            request_sha256=provider_identity.request_sha256,
            usage=accepted.usage,
            request_started_utc=attempt_metadata["request_started_utc"],
        )
        return {
            "content": accepted.content,
            "finish_reason": accepted.finish_reason,
            "stats": stats,
            "provider_response_id": accepted.provider_response_id,
            "provider_response_model": accepted.response_model,
            "request_sha256": provider_identity.request_sha256,
            "logical_operation": logical_operation,
            "journal_kind": journal_kind,
        }

    result = run_journaled_paid_request(
        journal_path=journal_path,
        identity=journal_identity,
        save_json=atomic_write_json,
        prepare_attempt=prepare_attempt,
        send_once=send_once,
        normalize_response=normalize_response,
    )
    return {
        "logical_operation": logical_operation,
        "transport_operation": logical_operation,
        "journal_kind": journal_kind,
        "journal_path": str(journal_path),
        "request_sha256": result["request_sha256"],
        "api_called": bool(result["api_called"]),
        "replayed": bool(result["replayed"]),
        "transport_attempts": attempts["transport"],
        "envelope": dict(result["response"]),
    }


def _classify_recovery_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the corrected G7J auxiliary classifier to a durable envelope."""
    stats = envelope.get("stats")
    if not isinstance(stats, Mapping):
        raise G7MRecoveryError(
            "the durable recovery response lacks accounting stats"
        )
    content = envelope.get("content")
    if not isinstance(content, str):
        raise G7MRecoveryError(
            "the durable recovery response content must be a string"
        )
    try:
        return g7j.classify_auxiliary_response(
            logical_operation=RECOVERY_LOGICAL_OPERATION,
            content=content,
            finish_reason=envelope.get("finish_reason"),
            completion_tokens=stats.get("completion_tokens"),
            max_completion_tokens=RECOVERY_MAX_COMPLETION_TOKENS,
            reasoning_tokens=stats.get("reasoning_tokens"),
        )
    except g7j.G7JPolicyError as exc:
        raise G7MRecoveryError(
            f"the recovered response could not be classified: {exc}"
        ) from exc


def _validated_recovered_state(recovered_state: Any) -> dict[str, Any]:
    """Validate the COMPLETE candidate state in memory before any write."""
    if not isinstance(recovered_state, Mapping):
        raise G7MRecoveryError("the recovered state must be a mapping")
    new_terms = recovered_state.get("new_terms")
    if not isinstance(new_terms, list):
        raise G7MRecoveryError(
            "the recovered state new_terms must be a list"
        )
    continuity_summary = recovered_state.get("continuity_summary")
    if not isinstance(continuity_summary, str):
        raise G7MRecoveryError(
            "the recovered state continuity_summary must be a string"
        )
    issues = recovered_state.get("issues")
    if not isinstance(issues, list):
        raise G7MRecoveryError("the recovered state issues must be a list")

    normalized_terms: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, term in enumerate(new_terms):
        if not isinstance(term, Mapping):
            raise G7MRecoveryError(
                f"recovered new_terms[{index}] must be an object"
            )
        chinese = term.get("chinese")
        english = term.get("english")
        category = term.get("category")
        notes = term.get("notes", "")
        status = term.get("status", PROVISIONAL_STATUS)
        if not isinstance(chinese, str) or not chinese.strip():
            raise G7MRecoveryError(
                f"recovered new_terms[{index}] lacks an exact source key"
            )
        if not isinstance(english, str) or not english.strip():
            raise G7MRecoveryError(
                f"recovered new_terms[{index}] lacks an English rendering"
            )
        if not isinstance(category, str) or not category.strip():
            raise G7MRecoveryError(
                f"recovered new_terms[{index}] lacks a category"
            )
        if status != PROVISIONAL_STATUS:
            raise G7MRecoveryError(
                "every recovered new term must stay provisional"
            )
        if not isinstance(notes, str):
            raise G7MRecoveryError(
                f"recovered new_terms[{index}] notes must be a string"
            )
        if chinese in seen:
            raise G7MRecoveryError(
                "each recovered new term source key may appear at most once"
            )
        seen.add(chinese)
        normalized_terms.append(
            {
                "chinese": chinese,
                "english": english,
                "category": category,
                "status": PROVISIONAL_STATUS,
                "notes": notes,
            }
        )
    candidate = {
        "new_terms": normalized_terms,
        "continuity_summary": continuity_summary,
        "issues": copy.deepcopy(issues),
    }
    # The complete candidate must be canonically serializable before any write.
    documents = _state_documents(candidate)
    for relative, document in documents.items():
        _canonical_json(document)
    return candidate


def _state_documents(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact on-disk engine shapes for the recovered state."""
    entries: dict[str, Any] = {}
    for term in candidate["new_terms"]:
        entries[term["chinese"]] = {
            "english": term["english"],
            "category": term["category"],
            "status": PROVISIONAL_STATUS,
            "first_chapter": TARGET_CHAPTER,
            "notes": term["notes"],
        }
    return {
        g7l.GLOSSARY_RELATIVE: {"entries": entries},
        g7l.CONTINUITY_RELATIVE: {
            "latest_chapter": TARGET_CHAPTER,
            "summary": candidate["continuity_summary"],
        },
        g7l.ISSUES_RELATIVE: {"issues": copy.deepcopy(candidate["issues"])},
    }


def _recovered_state_from_envelope(
    envelope: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(validated state, candidate problem)`` for one envelope."""
    if classification.get("status") != g7j.AUXILIARY_STATE_ACCEPTED:
        return None, None
    content = envelope.get("content")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, "the recovered payload is not a JSON object"
    try:
        return _validated_recovered_state(parsed), None
    except G7MRecoveryError as exc:
        return None, str(exc)


def run_recovery_request(
    *,
    preflight: Mapping[str, Any],
    recovery_ledger_path: Path | str,
    journal_dir: Path | str,
    provider_client: Any,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Send the durable Chapter-1 state-extraction recovery request (and, only
    for a nonempty malformed payload, at most ONE bounded JSON repair).

    The client is injected, so this function performs no credential handling of
    its own. The classifier gate is mandatory: an empty or otherwise fail-closed
    response stops with no repair, no fabrication, and no state mutation. This
    function never applies state; :func:`apply_recovered_state` owns the apply.
    """
    assert_no_translation_or_chapter2_path()
    value = _validated_preflight(preflight)
    use_clock = _use_clock(clock)
    ledger = RecoveryBudgetLedger.open_or_create(
        path=recovery_ledger_path,
        preflight=value,
        clock=use_clock,
        allow_create=True,
    )

    transport_attempts = 0
    extraction = _journaled_recovery_send(
        preflight=value,
        ledger=ledger,
        journal_dir=journal_dir,
        provider_client=provider_client,
        clock=use_clock,
        repair_of=None,
    )
    transport_attempts += extraction["transport_attempts"]
    classification = _classify_recovery_envelope(extraction["envelope"])
    recovered_state, candidate_problem = _recovered_state_from_envelope(
        extraction["envelope"], classification
    )

    report: dict[str, Any] = {
        "status": RECOVERY_FAIL_CLOSED_STATUS,
        "schema_version": G7M_SCHEMA_VERSION,
        "revision": G7M_REVISION,
        "recovery_generation_id": value["recovery_generation_id"],
        "recovery_binding_sha256": value["recovery_binding_sha256"],
        "plan_sha256": value["plan_sha256"],
        "run_id": value["run_id"],
        "novel_id": value["novel_id"],
        "chapter": TARGET_CHAPTER,
        "model_id": TARGET_MODEL_ID,
        "logical_operation": RECOVERY_LOGICAL_OPERATION,
        "journal_kind": RECOVERY_JOURNAL_KIND,
        "journal_path": extraction["journal_path"],
        "journal_kinds": [extraction["journal_kind"]],
        "journal_paths": [extraction["journal_path"]],
        "repair_send_used": False,
        "classification": classification["status"],
        "classifier": dict(classification),
        "fail_closed": bool(classification["fail_closed"]),
        "recovered_state": None,
        "state_source": None,
        "apply_performed": False,
        "credential_access": False,
        "max_completion_tokens": RECOVERY_MAX_COMPLETION_TOKENS,
        "replayed": bool(extraction["replayed"]),
        "api_called": bool(extraction["api_called"]),
    }

    if recovered_state is not None:
        # A complete, schema-valid recovery state: proceed (no repair needed).
        report.update(
            {
                "status": RECOVERY_ACCEPTED_STATUS,
                "fail_closed": False,
                "recovered_state": recovered_state,
                "state_source": "provider_response",
            }
        )
    elif _repair_permitted(classification, candidate_problem):
        # A nonempty malformed payload may use exactly ONE bounded repair send.
        if ledger.new_provider_sends_started >= ledger.document[
            "maximum_new_provider_sends"
        ]:
            raise G7MRecoveryError(
                "the recovery new Provider-send ceiling is reached; a bounded "
                "JSON repair is not authorized"
            )
        repair = _journaled_recovery_send(
            preflight=value,
            ledger=ledger,
            journal_dir=journal_dir,
            provider_client=provider_client,
            clock=use_clock,
            repair_of=extraction["envelope"].get("content"),
        )
        transport_attempts += repair["transport_attempts"]
        repair_classification = _classify_recovery_envelope(repair["envelope"])
        repaired_state, repaired_problem = _recovered_state_from_envelope(
            repair["envelope"], repair_classification
        )
        report.update(
            {
                "repair_send_used": True,
                "journal_path": repair["journal_path"],
                "journal_kinds": [
                    extraction["journal_kind"],
                    repair["journal_kind"],
                ],
                "journal_paths": [
                    extraction["journal_path"],
                    repair["journal_path"],
                ],
                "classification": repair_classification["status"],
                "classifier": dict(repair_classification),
                "fail_closed": bool(repair_classification["fail_closed"]),
                "replayed": bool(repair["replayed"]),
                "api_called": bool(repair["api_called"]),
                "initial_classification": classification["status"],
                "initial_candidate_problem": candidate_problem,
            }
        )
        if repaired_state is not None:
            report.update(
                {
                    "status": RECOVERY_ACCEPTED_STATUS,
                    "fail_closed": False,
                    "recovered_state": repaired_state,
                    "state_source": "provider_response_bounded_repair",
                }
            )
        else:
            report["blocker"] = (
                "the bounded JSON repair did not yield an accepted recovery "
                "state; no fabrication and no state mutation"
            )
            report["repair_candidate_problem"] = repaired_problem
    else:
        report["blocker"] = (
            "the recovery response was empty or otherwise fail-closed; no JSON "
            "repair, no fabrication, and no state mutation"
        )

    report["provider_calls"] = transport_attempts
    report["new_provider_sends_started"] = ledger.new_provider_sends_started
    report["accepted_credit_equivalent_usd"] = _decimal_text(
        ledger.accepted_cost, "accepted cost"
    )
    report["recovery_ledger_path"] = str(ledger.path)
    return report


def _repair_permitted(
    classification: Mapping[str, Any],
    candidate_problem: str | None,
) -> bool:
    """Return True only for a nonempty payload that may use one bounded repair.

    An empty or otherwise fail-closed classification is NEVER repairable, and a
    payload that is genuinely absent can never be repaired. A nonempty payload
    that is not a JSON object, or that is a JSON object which does not describe
    the required Chapter-1 state, may use the one bounded repair send.
    """
    if classification.get("fail_closed") is True:
        return False
    if classification.get("status") == g7j.AUXILIARY_MALFORMED_JSON_REPAIRED:
        return True
    if classification.get("status") == g7j.AUXILIARY_STATE_ACCEPTED:
        return candidate_problem is not None
    return False


# ---------------------------------------------------------------------------
# Atomic fail-closed apply
# ---------------------------------------------------------------------------


def _assert_apply_preconditions(
    preflight: Mapping[str, Any],
    re_verified: Mapping[str, Any],
) -> None:
    """Fail closed unless the run root still holds the preflighted bytes."""
    for field in (
        "plan_sha256",
        "recovery_generation_id",
        "recovery_binding_sha256",
        "run_id",
        "novel_id",
        "model_id",
        "chapter",
        "progress",
        "progress_file_sha256",
        "committed_translation_sha256",
        "qa_sha256",
        "source_sha256",
        "source_chapter_sha256",
        "source_chapter_library_file_sha256",
    ):
        if re_verified.get(field) != preflight.get(field):
            raise G7MRecoveryError(
                "the recovery apply refused an inconsistent re-verification: "
                + field
            )
    for field in (
        "glossary_file_sha256",
        "continuity_file_sha256",
        "issues_file_sha256",
    ):
        if re_verified["degraded_state"].get(field) != preflight[
            "degraded_state"
        ].get(field):
            raise G7MRecoveryError(
                "the degraded state files changed before the atomic apply: "
                + field
            )


def _verify_applied_state(
    run_root: Path,
    preflight: Mapping[str, Any],
    documents: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the written state, progress and the committed Chapter-1 bytes."""
    observed: dict[str, Any] = {}
    for relative, document in documents.items():
        path = run_root / relative
        observed[relative] = _read_object(path, f"applied {relative}")
        if observed[relative] != document:
            raise G7MRecoveryError(
                f"the applied state does not match the validated candidate: "
                f"{relative}"
            )
    progress_path = run_root / g7l.PROGRESS_RELATIVE
    progress_sha256 = _sha256_file(progress_path)
    if progress_sha256 != preflight["progress_file_sha256"]:
        raise G7MRecoveryError(
            "the recovery apply must never change the engine progress file"
        )
    progress = _read_object(progress_path, "run progress")
    if (
        progress.get("completed") != [TARGET_CHAPTER]
        or progress.get("last_completed") != TARGET_CHAPTER
        or progress.get("next_chapter") != TARGET_CHAPTER + 1
    ):
        raise G7MRecoveryError(
            "the recovery apply must leave Chapter 1 committed and Chapter 2 "
            "next"
        )
    committed_sha256 = _sha256_file(run_root / g7l.COMMITTED_CHAPTER_RELATIVE)
    if committed_sha256 != preflight["committed_translation_sha256"]:
        raise G7MRecoveryError(
            "the recovery apply must never change the committed Chapter-1 "
            "English bytes"
        )
    qa_sha256 = _sha256_file(run_root / g7l.QA_RELATIVE)
    if qa_sha256 != preflight["qa_sha256"]:
        raise G7MRecoveryError(
            "the recovery apply must never change the committed Chapter-1 QA"
        )
    return {
        "applied": [str(run_root / relative) for relative in documents],
        "progress_unchanged": True,
        "progress_sha256": progress_sha256,
        "committed_translation_sha256": committed_sha256,
        "qa_sha256": qa_sha256,
    }


def _restore_previous_bytes(
    run_root: Path, previous: Mapping[str, bytes]
) -> None:
    for relative, payload in previous.items():
        atomic_write_bytes(run_root / relative, payload)


def apply_recovered_state(
    *,
    run_root: Path | str,
    preflight: Mapping[str, Any],
    recovered_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically apply the recovered Chapter-1 state, or leave it unchanged.

    The complete candidate is validated in memory first, then the single-writer
    production run lock is taken and every precondition is re-verified. The three
    degraded state files are written as one recoverable transaction: on ANY
    failure the previous bytes are restored. The engine progress file is never
    written and the committed Chapter-1 output and QA bytes never change.
    """
    assert_no_translation_or_chapter2_path()
    value = _validated_preflight(preflight)
    candidate = _validated_recovered_state(recovered_state)
    documents = _state_documents(candidate)
    root = Path(run_root)
    if str(root) != value["run_root"]:
        raise G7MRecoveryError(
            "the recovery apply must target the preflighted run root"
        )

    with production_run_lock(root):
        re_verified = preflight_ch1_state_recovery(
            evidence_root=value["evidence_root"],
            run_id=value["run_id"],
            plan_path=value["plan_path"],
            expected_plan_sha256=value["plan_sha256"],
            expected_recovery_generation_id=value["recovery_generation_id"],
            expected_recovery_binding_sha256=value[
                "recovery_binding_sha256"
            ],
        )
        _assert_apply_preconditions(value, re_verified)
        previous = {
            relative: (root / relative).read_bytes()
            for relative in APPLY_FIELDS
        }
        try:
            for relative, document in documents.items():
                atomic_write_json(root / relative, document)
            verified = _verify_applied_state(root, value, documents)
        except BaseException as exc:
            try:
                _restore_previous_bytes(root, previous)
                for relative, payload in previous.items():
                    if (root / relative).read_bytes() != payload:
                        raise G7MRecoveryError(
                            "the recovery state rollback did not restore the "
                            f"previous bytes: {relative}"
                        )
            except BaseException as restore_exc:
                raise G7MRecoveryError(
                    "the recovery apply failed AND its rollback failed; the run "
                    "root requires manual inspection"
                ) from restore_exc
            if isinstance(exc, G7MRecoveryError):
                raise
            raise G7MRecoveryError(
                f"the recovery apply failed and was rolled back: {exc}"
            ) from exc

    return {
        "status": RECOVERY_APPLIED_STATUS,
        "schema_version": G7M_SCHEMA_VERSION,
        "revision": G7M_REVISION,
        "recovery_generation_id": value["recovery_generation_id"],
        "recovery_binding_sha256": value["recovery_binding_sha256"],
        "plan_sha256": value["plan_sha256"],
        "run_id": value["run_id"],
        "novel_id": value["novel_id"],
        "chapter": TARGET_CHAPTER,
        "model_id": TARGET_MODEL_ID,
        "new_terms_committed": len(candidate["new_terms"]),
        "glossary_entries": sorted(
            documents[g7l.GLOSSARY_RELATIVE]["entries"]
        ),
        "progress_unchanged": verified["progress_unchanged"],
        "chapter_1_retranslated": False,
        "chapter_2_advanced": False,
        "glossary_review_performed": False,
        "state_fabricated": False,
        "historical_evidence_modified": False,
        "apply": verified,
    }


# ---------------------------------------------------------------------------
# Paid orchestrator (credential access strictly after preflight + fresh auth)
# ---------------------------------------------------------------------------


def _read_recovery_api_key(
    *,
    secret_path: Path | str | None,
    secret_reader: Callable[[], str] | None,
) -> str:
    """Read the injected recovery credential. Never reads any ``.env`` file.

    This helper is only ever reached from :func:`execute_ch1_state_recovery`,
    strictly after a successful zero-Provider preflight AND a validated fresh
    operator authorization.
    """
    if secret_reader is not None:
        if not callable(secret_reader):
            raise G7MRecoveryError(
                "the recovery secret reader must be callable"
            )
        key = secret_reader()
    elif secret_path is not None:
        key = _read_text(secret_path, "recovery credential")
    else:
        raise G7MRecoveryError(
            "paid recovery execution requires an explicitly injected "
            "credential; no environment or .env credential is ever read"
        )
    if not isinstance(key, str) or not key.strip():
        raise G7MRecoveryError("the injected recovery credential is empty")
    return key.strip()


def execute_ch1_state_recovery(
    *,
    evidence_root: Path | str,
    run_id: str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_recovery_generation_id: str,
    expected_recovery_binding_sha256: str,
    authorization: Mapping[str, Any],
    authorize_paid_run: bool,
    secret_path: Path | str | None = None,
    secret_reader: Callable[[], str] | None = None,
    client_factory: Callable[..., Any] | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run one authorized Chapter-1 state recovery, then atomically apply it.

    Order is the G7G secure-launcher order: an internal zero-Provider preflight,
    then the fresh authorization check, and ONLY THEN any credential access and
    Provider client construction. Without the explicit ``authorize_paid_run``
    flag nothing paid happens at all.
    """
    assert_no_translation_or_chapter2_path()
    if authorize_paid_run is not True:
        raise G7MRecoveryError(
            "paid Chapter-1 state recovery requires the explicit "
            "authorize_paid_run flag"
        )
    preflight = preflight_ch1_state_recovery(
        evidence_root=evidence_root,
        run_id=run_id,
        plan_path=plan_path,
        expected_plan_sha256=expected_plan_sha256,
        expected_recovery_generation_id=expected_recovery_generation_id,
        expected_recovery_binding_sha256=expected_recovery_binding_sha256,
    )
    authorization_record = validate_fresh_recovery_authorization(
        authorization,
        preflight=preflight,
    )

    run_root = Path(preflight["run_root"])
    api_key = _read_recovery_api_key(
        secret_path=secret_path,
        secret_reader=secret_reader,
    )
    try:
        provider_client = create_provider_client(
            api_key=api_key,
            client_factory=client_factory,
        )
        outcome = run_recovery_request(
            preflight=preflight,
            recovery_ledger_path=recovery_ledger_path(run_root),
            journal_dir=run_root / g7l.JOURNAL_DIR_RELATIVE,
            provider_client=provider_client,
            clock=clock,
        )
    finally:
        api_key = None

    result = {
        "status": outcome["status"],
        "schema_version": G7M_SCHEMA_VERSION,
        "revision": G7M_REVISION,
        "preflight": PREFLIGHT_OK_STATUS,
        "recovery_generation_id": outcome["recovery_generation_id"],
        "recovery_binding_sha256": outcome["recovery_binding_sha256"],
        "plan_sha256": outcome["plan_sha256"],
        "run_id": outcome["run_id"],
        "novel_id": outcome["novel_id"],
        "chapter": TARGET_CHAPTER,
        "model_id": TARGET_MODEL_ID,
        "logical_operation": RECOVERY_LOGICAL_OPERATION,
        "authorization": authorization_record,
        "provider_client_constructed": True,
        "credential_access": True,
        "provider_calls": outcome.get("provider_calls", 0),
        "recovery": outcome,
        "plan_requires_rematerialization": False,
        "chapter_1_retranslated": False,
        "chapter_1_translation_retry": False,
        "chapter_2_execution": False,
        "glossary_review_performed": False,
        "historical_evidence_modified": False,
        "state_fabricated": False,
        "apply_performed": False,
    }
    if outcome["status"] != RECOVERY_ACCEPTED_STATUS:
        return result

    applied = apply_recovered_state(
        run_root=run_root,
        preflight=preflight,
        recovered_state=outcome["recovered_state"],
    )
    result["status"] = RECOVERY_APPLIED_STATUS
    result["apply_performed"] = True
    result["applied_state"] = applied
    return result


assert_no_translation_or_chapter2_path()


__all__ = [
    "ALLOWED_RECOVERY_CHAPTERS",
    "ALLOWED_RECOVERY_OPERATIONS",
    "APPLY_FIELDS",
    "G7MRecoveryError",
    "G7M_REVISION",
    "G7M_SCHEMA_VERSION",
    "MAX_CE_USD",
    "MAX_NEW_SENDS",
    "PREFLIGHT_OK_STATUS",
    "RECOVERY_ACCEPTED_STATUS",
    "RECOVERY_APPLIED_STATUS",
    "RECOVERY_FAIL_CLOSED_STATUS",
    "RECOVERY_JOURNAL_KIND",
    "RECOVERY_LEDGER_DIR_RELATIVE",
    "RECOVERY_LEDGER_RELATIVE",
    "RECOVERY_LEDGER_REVISION",
    "RECOVERY_LOGICAL_OPERATION",
    "RECOVERY_MAX_COMPLETION_TOKENS",
    "RecoveryBudgetLedger",
    "TARGET_CHAPTER",
    "TARGET_MODEL_ID",
    "TARGET_REASONING_EFFORT",
    "apply_recovered_state",
    "assert_no_translation_or_chapter2_path",
    "execute_ch1_state_recovery",
    "preflight_ch1_state_recovery",
    "recovery_journal_path",
    "recovery_ledger_path",
    "recovery_summary",
    "run_recovery_request",
    "validate_fresh_recovery_authorization",
]
