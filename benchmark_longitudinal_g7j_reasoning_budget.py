"""G7J reasoning-aware GOAT completion-budget and response-diagnostic policy.

This module is import-safe and zero-provider. It exists because the first paid
GOAT Stage-1 primary-translation attempt demonstrated that an explicitly
reasoning-enabled model can consume the legacy 12k completion ceiling before
producing any visible translation content: the durable evidence is an empty
assistant message with ``finish_reason == "length"`` at 11,994/12,000 completion
tokens. That response is classified, mechanically and exactly, as
``completion_budget_exhausted_before_visible_content``.

G7J preserves the preregistered per-operation reasoning effort. It does not
silently disable reasoning and it does not choose a new effort. It only:

* freezes the incoming GOAT primary-translation baseline at exactly 12,000;
* rewrites the GOAT primary-translation total-completion ceiling to exactly
  24,000 for every one of the six GOAT models, including Provider-native /
  default-only models that carry no reasoning-effort field;
* explicitly rejects the unsupported 32,000 value;
* leaves every auxiliary operation budget and the production 12,000 ceiling
  unchanged;
* records reasoning-token diagnostics only when the Provider actually reports
  them (never reasoning text);
* classifies a deterministically inconsistent reasoning diagnostic as an exact
  bounded classification instead of raising, so a definitive paid response stays
  durable and exactly-once accounted rather than becoming uncertain;
* classifies empty / truncated visible translation responses mechanically.

The 24,000 value is evidence-derived, not guessed: it is the demonstrated
12,000-token pre-visible/reasoning reserve plus the original 12,000-token visible
translation allowance. 32,000 is not derived from any observed evidence and is
unsupported.

G7L extends the same reasoning-aware policy to the GOAT auxiliary operations. The
second observed paid incident is the auxiliary mirror image of Run 1: the legacy
3,000-token ``state_extraction`` request returned an accepted but EMPTY assistant
message with ``finish_reason == "length"`` (3,000 completion tokens, all 3,000 of
them reasoning, zero visible). No auxiliary budget correction and no finish-reason
classification applied, so the empty content reached ``json.loads("")``, the local
salvage helper returned ``None``, and a generic JSON repair ran on EMPTY input and
produced bogus state that was committed. G7L therefore adds:

* a GOAT-only uniform auxiliary completion budget of 4,000 tokens;
* an exact fail-closed auxiliary response classifier so an empty
  length-terminated auxiliary response is NEVER sent to JSON repair;
* a single source of truth for the state-extraction checkpoint identity ceiling,
  so the GOAT identity may use 4,000 while the production engine's own identity
  bytes stay exactly 3,000.

Both corrections are strictly opt-in and GOAT-only. Production behavior for every
auxiliary operation, and the production state-checkpoint identity bytes, are
unchanged.

Nothing in this module authorizes a Provider call, credential access, replay,
chapter execution, retry, or paid work. The scope is benchmark-only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from benchmark_longitudinal_config import (
    BENCHMARK_MODEL_IDS,
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
)
from benchmark_longitudinal_g7a_readiness import TRANSLATION_EFFORT_BY_MODEL


G7J_SCHEMA_VERSION = 1
G7J_REVISION = "goat-g7j-reasoning-aware-completion-budget-v3"

PRIMARY_TRANSLATION_OPERATION = LIFECYCLE_OPERATION_TRANSLATION
BENCHMARK_ONLY_SCOPE = True

# Frozen incoming GOAT primary-translation baseline. The production engine
# historically requested exactly this many visible completion tokens.
GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS = 12_000

# Corrected GOAT primary-translation total-completion ceiling.
# Derivation: 12,000 demonstrated pre-visible/reasoning reserve
#           + 12,000 original visible translation allowance.
GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS = 24_000

# Considered and explicitly rejected: not derived from any observed evidence.
UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS = 32_000

# Providers may report a few fewer tokens than the exact hard ceiling.
# Reused verbatim by the G7L auxiliary classifier so both classifiers agree.
NEAR_COMPLETION_CEILING_TOLERANCE = 64

FINISH_REASON_LENGTH = "length"
FINISH_REASON_STOP = "stop"

# ---------------------------------------------------------------------------
# G7L: GOAT-only auxiliary completion ceiling
# ---------------------------------------------------------------------------

# Corrected GOAT auxiliary completion ceiling. It is uniform for every
# auxiliary operation and for all six GOAT models and never varies by model or
# by reasoning effort.
#
# Evidence derivation (not a guess): the legacy 3,000-token ``state_extraction``
# request was ACCEPTED with an EMPTY visible message and
# ``finish_reason == "length"`` at exactly 3,000/3,000 completion tokens -- all
# 3,000 of them reasoning, zero visible. The same class of pre-visible
# reasoning exhaustion applies to every other frozen auxiliary ceiling the real
# lifecycle requests today (800 / 4,000 / 4,000 / 5,000, see the frozen incoming
# baseline table below). 4,000 is the smallest round ceiling that leaves visible
# headroom for the compact auxiliary JSON schemas while staying far below the
# corrected 24,000 primary-translation total.
GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS = 4_000

# The frozen legacy auxiliary baseline recorded by that incident: the production
# engine's own ``state_extraction`` request ceiling. 3,000 stays exactly the
# production auxiliary ceiling and the production state-checkpoint identity
# ceiling.
GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS = 3_000

# The GOAT auxiliary operations: every frozen provider-assisted lifecycle
# operation except primary translation.
GOAT_AUXILIARY_OPERATIONS = tuple(
    operation
    for operation in PURE_RUN_LIFECYCLE_OPERATIONS
    if operation != PRIMARY_TRANSLATION_OPERATION
)

# Frozen incoming GOAT auxiliary ceilings: exactly the ceilings the real
# production lifecycle requests today, per operation, in the frozen
# ``PURE_RUN_LIFECYCLE_OPERATIONS`` order. The only accepted incoming auxiliary
# ceilings are one of these frozen per-operation baselines or the frozen legacy
# 3,000 baseline above; every other incoming value is drift and fails closed. No
# ``max()`` selection is used anywhere.
GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS = (
    (LIFECYCLE_OPERATION_STATE_EXTRACTION, 3_000),
    (LIFECYCLE_OPERATION_GLOSSARY_REVIEW, 800),
    (LIFECYCLE_OPERATION_PERIODIC_AUDIT, 4_000),
    (LIFECYCLE_OPERATION_STATE_JSON_REPAIR, 5_000),
    (LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR, 4_000),
)

# Single source of truth for the state-extraction checkpoint identity ceiling.
# The GOAT identity may use the corrected 4,000 while the production engine's own
# identity bytes must stay exactly 3,000.
PRODUCTION_AUXILIARY_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
)

STATUS_OK = "ok"
STATUS_COMPLETION_BUDGET_EXHAUSTED_BEFORE_VISIBLE_CONTENT = (
    "completion_budget_exhausted_before_visible_content"
)
STATUS_EMPTY_VISIBLE_CONTENT_LENGTH_TERMINATED = (
    "empty_visible_content_length_terminated"
)
STATUS_EMPTY_VISIBLE_CONTENT = "empty_visible_content"
STATUS_VISIBLE_CONTENT_TRUNCATED_BY_COMPLETION_BUDGET = (
    "visible_content_truncated_by_completion_budget"
)
STATUS_UNSUPPORTED_TRANSLATION_FINISH_REASON = (
    "unsupported_translation_finish_reason"
)

# Exact bounded classifications for one post-response reasoning diagnostic.
# ``REASONING_DIAGNOSTIC_OK`` means the reported diagnostic is exactly
# acceptable; every other value names a deterministic inconsistency that must
# never be guessed around, must never be written into accounting stats, and must
# never be silently retried.
REASONING_DIAGNOSTIC_OK = "ok"
REASONING_DIAGNOSTIC_COMPLETION_TOTAL_MALFORMED = "completion_total_malformed"
REASONING_DIAGNOSTIC_EXPOSURE_METADATA_MALFORMED = (
    "reasoning_exposure_metadata_malformed"
)
REASONING_DIAGNOSTIC_UNEXPOSED_VALUE_PRESENT = "unexposed_reasoning_tokens_present"
REASONING_DIAGNOSTIC_VALUE_MALFORMED = "reasoning_tokens_malformed"
REASONING_DIAGNOSTIC_EXCEEDS_COMPLETION_TOTAL = (
    "reasoning_tokens_exceed_completion_tokens"
)

REASONING_DIAGNOSTIC_CLASSIFICATIONS = frozenset(
    {
        REASONING_DIAGNOSTIC_OK,
        REASONING_DIAGNOSTIC_COMPLETION_TOTAL_MALFORMED,
        REASONING_DIAGNOSTIC_EXPOSURE_METADATA_MALFORMED,
        REASONING_DIAGNOSTIC_UNEXPOSED_VALUE_PRESENT,
        REASONING_DIAGNOSTIC_VALUE_MALFORMED,
        REASONING_DIAGNOSTIC_EXCEEDS_COMPLETION_TOTAL,
    }
)

# The exact fail-closed message for each bounded classification.
REASONING_DIAGNOSTIC_ERROR_MESSAGES = {
    REASONING_DIAGNOSTIC_COMPLETION_TOTAL_MALFORMED: (
        "completion_tokens must be a nonnegative integer"
    ),
    REASONING_DIAGNOSTIC_EXPOSURE_METADATA_MALFORMED: (
        "reasoning_tokens_exposed must be boolean when present"
    ),
    REASONING_DIAGNOSTIC_UNEXPOSED_VALUE_PRESENT: (
        "unexposed reasoning_tokens must be absent rather than guessed"
    ),
    REASONING_DIAGNOSTIC_VALUE_MALFORMED: (
        "reasoning_tokens must be a nonnegative integer when exposed"
    ),
    REASONING_DIAGNOSTIC_EXCEEDS_COMPLETION_TOTAL: (
        "reasoning_tokens cannot exceed completion_tokens"
    ),
}

TRANSLATION_RESPONSE_STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_COMPLETION_BUDGET_EXHAUSTED_BEFORE_VISIBLE_CONTENT,
        STATUS_EMPTY_VISIBLE_CONTENT_LENGTH_TERMINATED,
        STATUS_EMPTY_VISIBLE_CONTENT,
        STATUS_VISIBLE_CONTENT_TRUNCATED_BY_COMPLETION_BUDGET,
        STATUS_UNSUPPORTED_TRANSLATION_FINISH_REASON,
    }
)

# ---------------------------------------------------------------------------
# G7L auxiliary response classifications
# ---------------------------------------------------------------------------

# The first three statuses are fail-closed: an empty auxiliary response must
# NEVER be handed to the engine's generic JSON repair branch. Only a nonempty
# malformed JSON payload legitimately allows one repair.
AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT = (
    "auxiliary_budget_exhausted_empty_content"
)
AUXILIARY_EMPTY_CONTENT_LENGTH = "auxiliary_empty_content_length"
AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH = (
    "auxiliary_empty_content_anomalous_finish"
)
AUXILIARY_MALFORMED_JSON_REPAIRED = "auxiliary_malformed_json_repaired"
AUXILIARY_STATE_ACCEPTED = "auxiliary_state_accepted"

AUXILIARY_FAIL_CLOSED_STATUSES = frozenset(
    {
        AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
        AUXILIARY_EMPTY_CONTENT_LENGTH,
        AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH,
    }
)

AUXILIARY_RESPONSE_STATUSES = frozenset(
    {
        AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
        AUXILIARY_EMPTY_CONTENT_LENGTH,
        AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH,
        AUXILIARY_MALFORMED_JSON_REPAIRED,
        AUXILIARY_STATE_ACCEPTED,
    }
)

# The exact frozen immutable facts of the historical Run-1 incident: the first
# paid GOAT Stage-1 primary-translation attempt. They are recorded here as frozen
# constants so the G7J policy identity is deterministic and zero-I/O. This module
# never reads, rewrites, or otherwise touches the historical run state, its paid
# request journal, its invocation ledger, its progress file, or its chapter
# artifacts; the evidence below is the frozen record of that incident.
HISTORICAL_RUN_1_MODEL_ID = "deepseek/deepseek-v4.1-flash"
HISTORICAL_RUN_1_REASONING_EFFORT = "low"
HISTORICAL_RUN_1_PROMPT_TOKENS = 6_058
HISTORICAL_RUN_1_COMPLETION_TOKENS = 11_994
HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS = (
    GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
)
HISTORICAL_RUN_1_VISIBLE_CONTENT = ""
HISTORICAL_RUN_1_FINISH_REASON = FINISH_REASON_LENGTH
HISTORICAL_RUN_1_ACCEPTED_CREDIT_EQUIVALENT_USD = "0.007597068"
HISTORICAL_RUN_1_LEDGER_STATUS = "accepted"
HISTORICAL_RUN_1_LEDGER_NEW_PROVIDER_SENDS_STARTED = 1
HISTORICAL_RUN_1_JOURNAL_STATUS = "response_recorded"
HISTORICAL_RUN_1_PROGRESS_LAST_COMPLETED = 0
HISTORICAL_RUN_1_PROGRESS_NEXT_CHAPTER = 1
HISTORICAL_RUN_1_PENDING_ARTIFACT_PRESENT = False
HISTORICAL_RUN_1_FINAL_ARTIFACT_PRESENT = False
HISTORICAL_RUN_1_CONSUMED_PAID_AUTHORIZATION_REUSABLE = False
HISTORICAL_RUN_1_CLASSIFICATION = (
    STATUS_COMPLETION_BUDGET_EXHAUSTED_BEFORE_VISIBLE_CONTENT
)

# The frozen immutable facts of the second observed paid incident: the Run-1
# Chapter-1 auxiliary mirror image of the translation incident above. The
# accepted ``state_extraction`` response was EMPTY with
# ``finish_reason == "length"`` at exactly the frozen 3,000 auxiliary completion
# ceiling, all 3,000 of them reasoning, and no auxiliary budget correction and
# no auxiliary finish-reason classification existed. The empty content therefore
# reached ``json.loads("")``, the local salvage helper returned ``None``, and the
# generic JSON repair then ran on EMPTY input and produced fabricated state that
# was committed. These constants are the frozen record of that incident; this
# module never reads, rewrites, or otherwise touches the historical run state.
HISTORICAL_AUXILIARY_MODEL_ID = HISTORICAL_RUN_1_MODEL_ID
HISTORICAL_AUXILIARY_REASONING_EFFORT = HISTORICAL_RUN_1_REASONING_EFFORT
HISTORICAL_AUXILIARY_RUN_INDEX = 1
HISTORICAL_AUXILIARY_CHAPTER = 1
HISTORICAL_AUXILIARY_OPERATION = LIFECYCLE_OPERATION_STATE_EXTRACTION
HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
)
HISTORICAL_AUXILIARY_COMPLETION_TOKENS = 3_000
HISTORICAL_AUXILIARY_REASONING_TOKENS = 3_000
HISTORICAL_AUXILIARY_VISIBLE_CONTENT = ""
HISTORICAL_AUXILIARY_FINISH_REASON = FINISH_REASON_LENGTH
HISTORICAL_AUXILIARY_LOCAL_SALVAGE_RETURNED_NONE = True
HISTORICAL_AUXILIARY_REPAIR_FED_EMPTY_INPUT = True
HISTORICAL_AUXILIARY_REPAIR_PRODUCED_FABRICATED_STATE = True
HISTORICAL_AUXILIARY_LEDGER_NEW_PROVIDER_SENDS_STARTED = 3
HISTORICAL_AUXILIARY_CLASSIFICATION = (
    AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
)
HISTORICAL_AUXILIARY_CORRECTED_CEILING = (
    GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
)

# The exact frozen GOAT primary-translation reasoning bindings G7J must never
# change: DeepSeek low, GLM high, Qwen medium, Gemini low, and no reasoning field
# for the two Provider-native/default-only models.
EXPECTED_TRANSLATION_REASONING_EFFORT_BY_MODEL = (
    ("deepseek/deepseek-v4.1-flash", "low"),
    ("z-ai/glm-5.3-flash", "high"),
    ("qwen/qwen3.8-max-0902", "medium"),
    ("google/gemini-3.8-flash", "low"),
    ("tencent/hy3-paid", None),
    ("xiaomi/mimo-v2.5-pro", None),
)


class G7JPolicyError(ValueError):
    """G7J policy input is malformed, drifted, or unsupported."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7JPolicyError(
            "G7J value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise G7JPolicyError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G7JPolicyError(f"{label} must be a positive integer")
    return value


def _validate_frozen_policy() -> None:
    """Prove the frozen roster, bindings, and budget derivation still hold."""
    if tuple(BENCHMARK_MODEL_IDS) != tuple(
        model_id
        for model_id, _effort in EXPECTED_TRANSLATION_REASONING_EFFORT_BY_MODEL
    ):
        raise G7JPolicyError(
            "GOAT roster/order changed; the six-model roster must not drift"
        )
    observed = tuple(
        (model_id, TRANSLATION_EFFORT_BY_MODEL[model_id])
        for model_id in BENCHMARK_MODEL_IDS
    )
    if observed != EXPECTED_TRANSLATION_REASONING_EFFORT_BY_MODEL:
        raise G7JPolicyError(
            "GOAT primary-translation reasoning bindings changed; G7J must "
            "preserve the frozen per-model efforts exactly"
        )
    if GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS == (
        UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the corrected GOAT total must never be the unsupported 32k value"
        )
    if GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS != (
        GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS * 2
    ):
        raise G7JPolicyError(
            "the corrected GOAT total must equal the baseline reserve plus "
            "the original visible allowance"
        )
    try:
        translation_completion_budget(
            logical_operation=PRIMARY_TRANSLATION_OPERATION,
            requested_tokens=UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS,
            reasoning_effort=None,
        )
    except G7JPolicyError:
        pass
    else:  # pragma: no cover - guarded by the explicit rejection below
        raise G7JPolicyError(
            "unsupported 32k GOAT completion budget must be rejected"
        )


def _goat_auxiliary_baseline_by_operation() -> dict[str, int]:
    """Return the frozen incoming GOAT auxiliary ceiling for every operation."""
    baselines = dict(GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS)
    if tuple(baselines) != tuple(GOAT_AUXILIARY_OPERATIONS):
        raise G7JPolicyError(
            "the frozen GOAT auxiliary baseline table must name exactly every "
            "GOAT auxiliary operation in the frozen lifecycle order"
        )
    return baselines


def _validate_frozen_auxiliary_policy() -> None:
    """Prove the GOAT auxiliary budget policy and classifier are consistent."""
    baselines = _goat_auxiliary_baseline_by_operation()
    if PRIMARY_TRANSLATION_OPERATION in GOAT_AUXILIARY_OPERATIONS:
        raise G7JPolicyError(
            "primary translation must never be a GOAT auxiliary operation"
        )
    if len(GOAT_AUXILIARY_OPERATIONS) != 5:
        raise G7JPolicyError(
            "the GOAT auxiliary operation set must be exactly the five frozen "
            "auxiliary lifecycle operations"
        )
    if GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS != 4_000:
        raise G7JPolicyError("the corrected GOAT auxiliary ceiling must be 4000")
    if GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS == (
        UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the corrected GOAT auxiliary ceiling must never be the unsupported "
            "32k value"
        )
    if GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS <= (
        GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the corrected GOAT auxiliary ceiling must leave visible headroom "
            "above the frozen legacy 3000 auxiliary baseline"
        )
    if PRODUCTION_AUXILIARY_COMPLETION_TOKENS != (
        GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the production auxiliary ceiling and the production state-checkpoint "
            "identity ceiling must stay exactly the frozen legacy 3000"
        )
    if baselines[LIFECYCLE_OPERATION_STATE_EXTRACTION] != (
        GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the frozen state-extraction auxiliary ceiling must stay the legacy "
            "3000 baseline recorded by the incident"
        )
    if set(AUXILIARY_FAIL_CLOSED_STATUSES) - set(AUXILIARY_RESPONSE_STATUSES):
        raise G7JPolicyError(
            "every fail-closed auxiliary status must be a known auxiliary status"
        )
    if AUXILIARY_MALFORMED_JSON_REPAIRED in AUXILIARY_FAIL_CLOSED_STATUSES:
        raise G7JPolicyError(
            "a legitimately repairable nonempty malformed payload must never be "
            "fail-closed"
        )

    for operation in GOAT_AUXILIARY_OPERATIONS:
        for accepted in (
            baselines[operation],
            GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
        ):
            if goat_auxiliary_completion_budget(
                logical_operation=operation,
                requested_tokens=accepted,
            ) != GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS:
                raise G7JPolicyError(
                    f"GOAT auxiliary operation {operation!r} did not receive the "
                    "corrected ceiling"
                )
    for drifted in (
        UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS,
        1,
        2_999,
        3_001,
        24_000,
    ):
        if drifted in (
            GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
            GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS,
        ):
            continue
        try:
            goat_auxiliary_completion_budget(
                logical_operation=LIFECYCLE_OPERATION_STATE_EXTRACTION,
                requested_tokens=drifted,
            )
        except G7JPolicyError:
            continue
        raise G7JPolicyError(
            f"GOAT auxiliary baseline drift must fail closed: {drifted}"
        )
    try:
        goat_auxiliary_completion_budget(
            logical_operation=PRIMARY_TRANSLATION_OPERATION,
            requested_tokens=GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
        )
    except G7JPolicyError:
        pass
    else:  # pragma: no cover - guarded by the explicit rejection above
        raise G7JPolicyError(
            "primary translation must never be routed through the GOAT "
            "auxiliary budget"
        )


def translation_completion_budget(
    *,
    logical_operation: str,
    requested_tokens: int,
    reasoning_effort: str | None,
    goat: bool = False,
) -> int:
    """Return the GOAT total-completion ceiling for one normalized request.

    Auxiliary operations preserve their original bounded token ceiling. Primary
    translation must arrive with the frozen 12,000 baseline and is rewritten to
    exactly 24,000 total completion tokens for every GOAT model, including
    default-only models that carry no reasoning-effort field. Any other incoming
    baseline fails closed; the 32,000 value is explicitly unsupported. No
    ``max()`` selection is used.

    ``goat`` is the explicit opt-in that routes an auxiliary operation through
    the G7L auxiliary policy (:func:`goat_auxiliary_completion_budget`), which is
    the single-call equivalent form of the auxiliary budget the G7F executor
    applies. With the default ``goat=False`` every auxiliary operation returns
    ``requested`` unchanged, so the production auxiliary path is byte-identical
    and non-GOAT callers are unaffected.
    """
    requested = _positive_int(requested_tokens, "requested_tokens")
    if goat is not True and goat is not False:
        raise G7JPolicyError("goat must be a boolean")

    if goat and logical_operation != PRIMARY_TRANSLATION_OPERATION:
        return goat_auxiliary_completion_budget(
            logical_operation=logical_operation,
            requested_tokens=requested,
        )

    if logical_operation != PRIMARY_TRANSLATION_OPERATION:
        return requested
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
    ):
        raise G7JPolicyError(
            "reasoning_effort must be a non-empty string when present"
        )
    if requested == UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "the 32000 GOAT primary-translation completion budget is explicitly "
            "unsupported; the evidence-derived corrected total is "
            f"{GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS}"
        )
    if requested != GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "GOAT primary-translation completion baseline drifted from "
            f"{GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS}: {requested}"
        )
    return GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS


def goat_auxiliary_completion_budget(
    *,
    logical_operation: str,
    requested_tokens: int,
) -> int:
    """Return the corrected GOAT-only auxiliary completion ceiling.

    This is the explicit GOAT auxiliary entrypoint. The G7F Stage-1 paid
    executor applies exactly this function at its auxiliary budget point (after
    G7C normalization and before provider request identity, journal identity,
    ledger preflight, and transport), and only for an opt-in GOAT runtime;
    :func:`translation_completion_budget` with ``goat=True`` is the equivalent
    single-call form.

    Every GOAT auxiliary operation receives the same corrected
    :data:`GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS`; the value is uniform for all
    six GOAT models and never varies by model or reasoning effort. An incoming
    ceiling is accepted only when it is that operation's frozen incoming
    baseline or the frozen legacy 3,000 auxiliary baseline that produced the
    incident. Any other incoming value -- including the explicitly unsupported
    32,000 total -- is drift and fails closed. Primary translation is never
    routed here. No ``max()`` selection is used.
    """
    requested = _positive_int(requested_tokens, "requested_tokens")
    if (
        not isinstance(logical_operation, str)
        or logical_operation not in GOAT_AUXILIARY_OPERATIONS
    ):
        raise G7JPolicyError(
            "logical_operation must be a GOAT auxiliary operation; "
            f"found {logical_operation!r}"
        )
    if requested == UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "the 32000 completion budget is explicitly unsupported for the GOAT "
            "auxiliary policy; the evidence-derived corrected auxiliary total is "
            f"{GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS}"
        )

    accepted = {
        _goat_auxiliary_baseline_by_operation()[logical_operation],
        GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
    }
    if requested not in accepted:
        raise G7JPolicyError(
            "GOAT auxiliary completion baseline drifted for "
            f"{logical_operation!r}: {requested}; accepted frozen incoming "
            f"ceilings are {sorted(accepted)}"
        )
    return GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS


def auxiliary_completion_budget_for_identity(*, goat: bool) -> int:
    """Return the state-extraction checkpoint identity ceiling.

    Single source of truth for the identity bytes of a state-extraction
    checkpoint: the GOAT identity uses the corrected 4,000 while the production
    engine's own identity stays exactly the frozen legacy 3,000.
    """
    if not isinstance(goat, bool):
        raise G7JPolicyError("goat must be a boolean")
    if goat:
        return GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    return PRODUCTION_AUXILIARY_COMPLETION_TOKENS


# ---------------------------------------------------------------------------
# G7N: additive, versioned GOAT-only 8K auxiliary completion ceiling
# ---------------------------------------------------------------------------
#
# The historical 4K policy above is byte-frozen and keeps its exact semantics.
# The G7M terminal Chapter-1 recovery (journal status ``response_recorded``,
# ``finish_reason == "length"``, 4,000/4,000 completion tokens, zero visible
# content, fail-closed classification, no repair, no apply) demonstrated a FULL
# 4,000-token pre-visible reasoning reserve at the corrected auxiliary ceiling.
# The corrected auxiliary design intends roughly 4,000 visible tokens for the
# compact structured state JSON, so the next evidence-derived auxiliary total is:
#
#     4,000 observed pre-visible reasoning reserve
#   + 4,000 visible structured-output allowance
#   = 8,000 total completion tokens
#
# This deliberately mirrors the existing primary-translation derivation
# (demonstrated pre-visible reserve + visible-output allowance). 12,000 / 16,000
# / 24,000 are NOT derived from the auxiliary evidence and are never accepted.
# The 8K policy is GOAT-only, uniform across all six GOAT models, does not change
# the production auxiliary ceiling (exactly 3,000), does not change the primary
# translation ceiling (exactly 24,000), and authorizes nothing paid by itself.

GOAT_AUXILIARY_8K_REVISION = "goat-g7j-auxiliary-8k-completion-budget-v1"
GOAT_AUXILIARY_8K_SCHEMA_VERSION = 1
GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS = 8_000
GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS = 4_000
GOAT_AUXILIARY_8K_VISIBLE_STRUCTURED_OUTPUT_ALLOWANCE_TOKENS = 4_000

# The historical corrected auxiliary total (the 4K policy above) and the frozen
# legacy auxiliary baseline. Both stay exactly what they were: the 8K policy is
# purely additive and never reinterprets the historical 4K request.
GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
)
GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
)

# The SAME frozen per-operation incoming auxiliary baselines the 4K policy
# accepts, exposed under the 8K revision so a caller can bind them explicitly.
GOAT_AUXILIARY_8K_INCOMING_BASELINE_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS
)

# The 8K policy is GOAT-only; the production auxiliary ceiling is unchanged.
GOAT_AUXILIARY_8K_GOAT_ONLY = True
GOAT_AUXILIARY_8K_PRODUCTION_AUXILIARY_COMPLETION_TOKENS = (
    PRODUCTION_AUXILIARY_COMPLETION_TOKENS
)


def _goat_auxiliary_8k_accepted_incoming_tokens(
    logical_operation: str,
) -> set[int]:
    """Return the accepted incoming ceilings for one GOAT auxiliary operation.

    An operation with a frozen incoming baseline accepts that baseline, the
    historical corrected 4,000 total, and the 8,000 total (the generation
    transition/recovery-planning values). Nothing else is accepted.
    """
    baselines = _goat_auxiliary_baseline_by_operation()
    accepted = {
        GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS,
        GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS,
    }
    baseline = baselines.get(logical_operation)
    if baseline is not None:
        accepted.add(baseline)
    return accepted


def goat_auxiliary_8k_completion_budget(
    *,
    logical_operation: str,
    requested_tokens: int,
) -> int:
    """Return the additive, versioned GOAT-only 8K auxiliary ceiling.

    The 8K policy is the versioned successor of
    :func:`goat_auxiliary_completion_budget`: it accepts exactly the frozen
    incoming per-operation auxiliary baseline, the historical corrected 4,000
    auxiliary total, or the corrected 8,000 total, and always returns the one
    corrected :data:`GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS`. The value is
    uniform for all six GOAT models and never varies by model or reasoning
    effort. Every other incoming ceiling -- including 0, 1, 2,999, 3,001, 5,000,
    12,000, 24,000, and the explicitly unsupported 32,000 -- is drift and fails
    closed. Primary translation is never routed here. No ``max()`` selection is
    used, and nothing here authorizes a Provider call or credential access.
    """
    requested = _positive_int(requested_tokens, "requested_tokens")
    if (
        not isinstance(logical_operation, str)
        or logical_operation not in GOAT_AUXILIARY_OPERATIONS
    ):
        raise G7JPolicyError(
            "logical_operation must be a GOAT auxiliary operation; "
            f"found {logical_operation!r}"
        )
    if requested == UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "the 32000 completion budget is explicitly unsupported for the GOAT "
            "auxiliary 8K policy; the evidence-derived corrected auxiliary total "
            f"is {GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS}"
        )

    accepted = _goat_auxiliary_8k_accepted_incoming_tokens(logical_operation)
    if requested not in accepted:
        raise G7JPolicyError(
            "GOAT auxiliary 8K completion baseline drifted for "
            f"{logical_operation!r}: {requested}; accepted frozen incoming "
            f"ceilings are {sorted(accepted)}"
        )
    return GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS


def goat_auxiliary_8k_completion_budget_for_identity(*, goat: bool) -> int:
    """Return the versioned state-extraction checkpoint identity ceiling.

    The GOAT identity uses the corrected 8K auxiliary ceiling; the production
    engine's own identity stays exactly the frozen legacy 3,000.
    """
    if not isinstance(goat, bool):
        raise G7JPolicyError("goat must be a boolean")
    if goat:
        return GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
    return PRODUCTION_AUXILIARY_COMPLETION_TOKENS


def g7j_auxiliary_8k_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider 8K auxiliary policy identity.

    The identity records the 8K derivation, the GOAT-only scope, the frozen
    incoming per-operation baselines, the unchanged production 3,000 auxiliary
    ceiling, the unchanged 24,000 primary-translation total, and the absence of
    any Provider authorization. It is pure computation: no file, credential, or
    Provider access. It is deliberately NOT part of :func:`g7j_policy_identity`,
    so G7J_POLICY_SHA256 and the frozen 4K payload are unchanged.
    """
    baselines = _goat_auxiliary_baseline_by_operation()
    return {
        "schema_version": GOAT_AUXILIARY_8K_SCHEMA_VERSION,
        "revision": GOAT_AUXILIARY_8K_REVISION,
        "goat_only": GOAT_AUXILIARY_8K_GOAT_ONLY,
        "derivation": {
            "observed_pre_visible_reasoning_reserve_tokens": (
                GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS
            ),
            "visible_structured_output_allowance_tokens": (
                GOAT_AUXILIARY_8K_VISIBLE_STRUCTURED_OUTPUT_ALLOWANCE_TOKENS
            ),
            "corrected_total_completion_tokens": (
                GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
            ),
            "rule": (
                "4000 observed pre-visible reasoning reserve "
                "+ 4000 visible structured-output allowance"
            ),
            "previous_corrected_completion_tokens": (
                GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS
            ),
            "legacy_corrected_completion_tokens": (
                GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS
            ),
        },
        "accepted_incoming_ceilings": [
            [
                operation,
                sorted(_goat_auxiliary_8k_accepted_incoming_tokens(operation)),
            ]
            for operation in GOAT_AUXILIARY_OPERATIONS
        ],
        "frozen_incoming_auxiliary_baselines": [
            [operation, requested]
            for operation, requested in (
                GOAT_AUXILIARY_8K_INCOMING_BASELINE_COMPLETION_TOKENS
            )
        ],
        "auxiliary_operations": list(GOAT_AUXILIARY_OPERATIONS),
        "uniform_for_all_six_goat_models": True,
        "model_dependent": False,
        "effort_dependent": False,
        "explicitly_unsupported_completion_tokens": [
            UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
        ],
        "max_selection_used": False,
        "incoming_baseline_drift_fails_closed": True,
        "historical_4k_policy_unchanged": {
            "corrected_goat_auxiliary_total": (
                GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
            ),
            "legacy_baseline": GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
        },
        "production_auxiliary_completion_tokens": (
            GOAT_AUXILIARY_8K_PRODUCTION_AUXILIARY_COMPLETION_TOKENS
        ),
        "production_state_checkpoint_identity_ceiling": (
            goat_auxiliary_8k_completion_budget_for_identity(goat=False)
        ),
        "goat_state_checkpoint_identity_ceiling": (
            goat_auxiliary_8k_completion_budget_for_identity(goat=True)
        ),
        "translation_total_completion_tokens": (
            GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
        ),
        "provider_authorization": {
            "provider_calls_authorized": False,
            "provider_calls_observed": 0,
            "credential_access": False,
            "retry_authorized": False,
        },
        "scope": {
            "benchmark_only": BENCHMARK_ONLY_SCOPE,
            "production_policy_changed": False,
            "historical_4k_policy_changed": False,
            "applied_before_request_identity": True,
        },
    }


def _validate_goat_auxiliary_8k_policy() -> None:
    """Prove the additive 8K policy and the frozen 4K policy are consistent."""
    if GOAT_AUXILIARY_8K_GOAT_ONLY is not True:
        raise G7JPolicyError("the GOAT auxiliary 8K policy must be GOAT-only")
    if GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS != (
        GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS
        + GOAT_AUXILIARY_8K_VISIBLE_STRUCTURED_OUTPUT_ALLOWANCE_TOKENS
    ):
        raise G7JPolicyError(
            "the GOAT auxiliary 8K total must equal the observed pre-visible "
            "reasoning reserve plus the visible structured-output allowance"
        )
    if GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS != (
        GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the 8K pre-visible reasoning reserve must be the corrected 4000 "
            "auxiliary total demonstrated by the terminal recovery"
        )
    if GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS != (
        GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the frozen historical corrected auxiliary total must stay 4000"
        )
    if GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS == (
        GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the 8K policy must be additive and must differ from the frozen 4K "
            "auxiliary total"
        )
    if GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS == (
        UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the 8K auxiliary total must never be the unsupported 32k value"
        )
    if GOAT_AUXILIARY_8K_INCOMING_BASELINE_COMPLETION_TOKENS is not (
        GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the 8K policy must reuse the frozen incoming auxiliary baselines"
        )
    if GOAT_AUXILIARY_8K_PRODUCTION_AUXILIARY_COMPLETION_TOKENS != (
        PRODUCTION_AUXILIARY_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the production auxiliary ceiling must stay the frozen 3000"
        )
    if PRODUCTION_AUXILIARY_COMPLETION_TOKENS != 3_000:
        raise G7JPolicyError(
            "the production auxiliary ceiling must stay exactly 3000"
        )
    if auxiliary_completion_budget_for_identity(goat=False) != 3_000:
        raise G7JPolicyError(
            "the production state-checkpoint identity ceiling must stay 3000"
        )
    if auxiliary_completion_budget_for_identity(goat=True) != (
        GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the frozen 4K GOAT state-checkpoint identity ceiling must not drift"
        )
    if goat_auxiliary_8k_completion_budget_for_identity(goat=False) != 3_000:
        raise G7JPolicyError(
            "the production 8K-helper identity ceiling must stay 3000"
        )
    if goat_auxiliary_8k_completion_budget_for_identity(goat=True) != (
        GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the GOAT 8K-helper identity ceiling must be the 8K total"
        )
    if GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS != 24_000:
        raise G7JPolicyError(
            "the GOAT primary-translation total must stay exactly 24000"
        )

    baselines = _goat_auxiliary_baseline_by_operation()
    for operation in GOAT_AUXILIARY_OPERATIONS:
        accepted = _goat_auxiliary_8k_accepted_incoming_tokens(operation)
        if baselines[operation] not in accepted:
            raise G7JPolicyError(
                "every frozen incoming auxiliary baseline must stay accepted by "
                f"the 8K policy: {operation!r}"
            )
        for incoming in sorted(accepted):
            if goat_auxiliary_8k_completion_budget(
                logical_operation=operation,
                requested_tokens=incoming,
            ) != GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS:
                raise G7JPolicyError(
                    f"GOAT auxiliary operation {operation!r} did not receive the "
                    "8K ceiling"
                )
        # The historical 4K helper stays byte-frozen for every auxiliary
        # operation, and it never accepts the new 8K value as an incoming
        # ceiling.
        for historical_incoming in (
            baselines[operation],
            GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
        ):
            if goat_auxiliary_completion_budget(
                logical_operation=operation,
                requested_tokens=historical_incoming,
            ) != GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS:
                raise G7JPolicyError(
                    f"the frozen 4K GOAT auxiliary policy drifted for "
                    f"{operation!r}"
                )
        try:
            goat_auxiliary_completion_budget(
                logical_operation=operation,
                requested_tokens=GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS,
            )
        except G7JPolicyError:
            pass
        else:  # pragma: no cover - guarded by the frozen accepted sets
            raise G7JPolicyError(
                "the frozen 4K GOAT auxiliary policy must never accept the 8K "
                "incoming ceiling"
            )

    for drifted in (0, 1, 2_999, 3_001, 5_000, 12_000, 24_000, 32_000):
        for operation in GOAT_AUXILIARY_OPERATIONS:
            if drifted in _goat_auxiliary_8k_accepted_incoming_tokens(operation):
                continue
            try:
                goat_auxiliary_8k_completion_budget(
                    logical_operation=operation,
                    requested_tokens=drifted,
                )
            except G7JPolicyError:
                continue
            raise G7JPolicyError(
                f"GOAT auxiliary 8K incoming ceiling drift must fail closed: "
                f"{drifted} for {operation!r}"
            )
    for malformed in (True, False, 8_000.0, "8000", None):
        try:
            goat_auxiliary_8k_completion_budget(
                logical_operation=LIFECYCLE_OPERATION_STATE_EXTRACTION,
                requested_tokens=malformed,  # type: ignore[arg-type]
            )
        except G7JPolicyError:
            continue
        raise G7JPolicyError(
            "a non-integer GOAT auxiliary 8K incoming ceiling must fail closed"
        )
    for operation in (PRIMARY_TRANSLATION_OPERATION, "not_an_operation", None):
        try:
            goat_auxiliary_8k_completion_budget(
                logical_operation=operation,  # type: ignore[arg-type]
                requested_tokens=GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS,
            )
        except G7JPolicyError:
            continue
        raise G7JPolicyError(
            "primary translation and unknown operations must never be routed "
            "through the GOAT auxiliary 8K budget"
        )


# ---------------------------------------------------------------------------
# G7P: additive versioned GOAT Stage-1 runtime auxiliary subrequest compatibility
# ---------------------------------------------------------------------------
#
# The frozen G7N 8K policy above recognizes exactly the per-operation incoming
# auxiliary baselines plus the historical 4K/8K transition totals. The real
# production lifecycle, however, nests LEGITIMATE smaller provider subrequests
# under the SAME logical context operations: the G7C operation-context stack
# attributes every nested ``call_api_with_retries`` / ``send_api_request_once``
# call to the enclosing context entrypoint.
#
# The confirmed production subrequest ceilings, each anchored to a real call
# site in ``translate_production_unattended_scheduled_retroactive_dynamic_pricing``
# (``PRODUCTION_SCRIPT_NAME``), are:
#
#   glossary_review    4000 original review, 2000 schema/semantic correction,
#                      5000 generic JSON repair, 800 conflict adjudication
#   periodic_audit     4000 bounded audit chunk (plus its repairable variants)
#   terminology_repair 800 strict terminology micro-repair and 800 residual
#                      source-script/CJK micro-repair
#   state_extraction   3000 direct state extraction
#   state_json_repair  5000 direct state JSON repair
#
# Every one of those ceilings is a request the production engine generates
# today, so refusing one is a benchmark-adapter defect, never a model failure.
# This additive, separately versioned runtime policy therefore accepts EXACTLY
# the frozen G7N accepted set UNION the closed production subrequest-ceiling
# table below, and still rewrites every accepted request to exactly 8,000. It is
# deliberately NOT a ``max()`` selection and it never accepts arbitrary values
# below 8,000. The historical G7N 8K policy identity and function semantics are
# untouched, so the G7N/G7O historical/recovery bindings stay valid.

GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION = (
    "goat-g7j-auxiliary-8k-stage1-runtime-subrequest-compat-v1"
)
GOAT_AUXILIARY_STAGE1_RUNTIME_SCHEMA_VERSION = 1
GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS = (
    GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
)

# The G7N 8K policy stays the frozen historical/recovery binding. The G7P
# runtime policy is its additive successor and never reinterprets it.
GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_REVISION = (
    GOAT_AUXILIARY_8K_REVISION
)
GOAT_AUXILIARY_STAGE1_RUNTIME_GOAT_ONLY = True
GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_AUXILIARY_COMPLETION_TOKENS = (
    PRODUCTION_AUXILIARY_COMPLETION_TOKENS
)

# The closed, explicit set of legitimate production auxiliary subrequest
# ceilings, per logical operation. This is the complete set the real lifecycle
# requests today; nothing else is accepted. No frozen baselines or historical
# transition totals are repeated here -- they stay accepted through the frozen
# G7N accepted set, which this table extends by union.
GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_SUBREQUEST_CEILING_COMPLETION_TOKENS = (
    (LIFECYCLE_OPERATION_STATE_EXTRACTION, 3_000),
    (LIFECYCLE_OPERATION_GLOSSARY_REVIEW, 800),
    (LIFECYCLE_OPERATION_GLOSSARY_REVIEW, 2_000),
    (LIFECYCLE_OPERATION_GLOSSARY_REVIEW, 4_000),
    (LIFECYCLE_OPERATION_GLOSSARY_REVIEW, 5_000),
    (LIFECYCLE_OPERATION_PERIODIC_AUDIT, 4_000),
    (LIFECYCLE_OPERATION_STATE_JSON_REPAIR, 5_000),
    (LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR, 800),
)


def _goat_auxiliary_stage1_runtime_production_subrequest_ceilings_by_operation(
) -> dict[str, tuple[int, ...]]:
    """Return the closed production subrequest-ceiling table, per operation."""
    table: dict[str, list[int]] = {
        operation: [] for operation in GOAT_AUXILIARY_OPERATIONS
    }
    for operation, requested in (
        GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_SUBREQUEST_CEILING_COMPLETION_TOKENS
    ):
        if operation not in table:
            raise G7JPolicyError(
                "the Stage-1 runtime production subrequest table may only name "
                f"GOAT auxiliary operations; found {operation!r}"
            )
        table[operation].append(requested)
    return {
        operation: tuple(ceilings)
        for operation, ceilings in table.items()
    }


def _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
    logical_operation: str,
) -> set[int]:
    """Return the closed accepted incoming ceilings for one auxiliary operation.

    The accepted set is exactly the frozen G7N 8K accepted set UNION the closed
    Stage-1 runtime production subrequest-ceiling table. Nothing else is
    accepted: this is never ``max(requested, 8000)`` and never "every positive
    integer below 8000".
    """
    accepted = set(
        _goat_auxiliary_8k_accepted_incoming_tokens(logical_operation)
    )
    accepted.update(
        _goat_auxiliary_stage1_runtime_production_subrequest_ceilings_by_operation().get(
            logical_operation,
            (),
        )
    )
    return accepted


def goat_auxiliary_stage1_runtime_completion_budget(
    *,
    logical_operation: str,
    requested_tokens: int,
) -> int:
    """Return the additive, versioned GOAT Stage-1 runtime auxiliary ceiling.

    This is the Stage-1 runtime successor of
    :func:`goat_auxiliary_8k_completion_budget`. It accepts exactly the frozen
    G7N accepted set -- the per-operation incoming auxiliary baseline, the
    historical corrected 4,000 auxiliary total, and the corrected 8,000 total --
    UNION the closed set of legitimate production auxiliary subrequest ceilings
    the real lifecycle can nest under the same logical operation (for example the
    glossary schema/semantic correction at 2,000, the generic JSON repair at
    5,000, and the strict terminology / residual CJK micro-repairs at 800).

    It always returns the one corrected
    :data:`GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS` (8,000), which
    is uniform for all six GOAT models and never varies by model or reasoning
    effort. Every other incoming ceiling -- including 0, 1, 2,999, 3,001, 6,000,
    12,000, 24,000, and the explicitly unsupported 32,000 -- is drift and fails
    closed. Primary translation is never routed here. No ``max()`` selection is
    used, and nothing here authorizes a Provider call or credential access.
    """
    requested = _positive_int(requested_tokens, "requested_tokens")
    if (
        not isinstance(logical_operation, str)
        or logical_operation not in GOAT_AUXILIARY_OPERATIONS
    ):
        raise G7JPolicyError(
            "logical_operation must be a GOAT auxiliary operation; "
            f"found {logical_operation!r}"
        )
    if requested == UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "the 32000 completion budget is explicitly unsupported for the GOAT "
            "Stage-1 runtime auxiliary policy; the evidence-derived corrected "
            "auxiliary total is "
            f"{GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS}"
        )

    accepted = _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
        logical_operation
    )
    if requested not in accepted:
        raise G7JPolicyError(
            "GOAT Stage-1 runtime auxiliary completion baseline drifted for "
            f"{logical_operation!r}: {requested}; accepted frozen incoming "
            f"ceilings are {sorted(accepted)}"
        )
    return GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS


def goat_auxiliary_stage1_runtime_completion_budget_for_identity(
    *,
    goat: bool,
) -> int:
    """Return the versioned state-extraction checkpoint identity ceiling.

    The GOAT identity uses the corrected Stage-1 runtime auxiliary total; the
    production engine's own identity stays exactly the frozen legacy 3,000.
    """
    if not isinstance(goat, bool):
        raise G7JPolicyError("goat must be a boolean")
    if goat:
        return GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS
    return PRODUCTION_AUXILIARY_COMPLETION_TOKENS


def g7p_stage1_runtime_auxiliary_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider Stage-1 runtime policy identity.

    The identity records the additive derivation from the frozen G7N 8K policy
    (by revision and SHA), the closed production subrequest-ceiling table, the
    full accepted incoming-ceiling set per operation, the unchanged production
    3,000 auxiliary ceiling, the unchanged 24,000 primary-translation total, and
    the absence of any Provider authorization. It is pure computation: no file,
    credential, or Provider access. It is deliberately NOT part of
    :func:`g7j_policy_identity` or :func:`g7j_auxiliary_8k_policy_identity`, so
    G7J_POLICY_SHA256, G7J_AUXILIARY_8K_POLICY_SHA256, and the frozen 4K payload
    are unchanged.
    """
    return {
        "schema_version": GOAT_AUXILIARY_STAGE1_RUNTIME_SCHEMA_VERSION,
        "revision": GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION,
        "goat_only": GOAT_AUXILIARY_STAGE1_RUNTIME_GOAT_ONLY,
        "base_policy": {
            "revision": GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_REVISION,
            "sha256": GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_SHA256,
            "additive": True,
            "reinterpreted": False,
        },
        "corrected_total_completion_tokens": (
            GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS
        ),
        "production_subrequest_ceiling_completion_tokens": [
            [operation, requested]
            for operation, requested in (
                GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_SUBREQUEST_CEILING_COMPLETION_TOKENS
            )
        ],
        "accepted_incoming_ceilings": [
            [
                operation,
                sorted(
                    _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
                        operation
                    )
                ),
            ]
            for operation in GOAT_AUXILIARY_OPERATIONS
        ],
        "auxiliary_operations": list(GOAT_AUXILIARY_OPERATIONS),
        "uniform_for_all_six_goat_models": True,
        "model_dependent": False,
        "effort_dependent": False,
        "explicitly_unsupported_completion_tokens": [
            UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
        ],
        "max_selection_used": False,
        "arbitrary_below_total_accepted": False,
        "incoming_baseline_drift_fails_closed": True,
        "production_auxiliary_completion_tokens": (
            GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_AUXILIARY_COMPLETION_TOKENS
        ),
        "production_state_checkpoint_identity_ceiling": (
            goat_auxiliary_stage1_runtime_completion_budget_for_identity(
                goat=False
            )
        ),
        "goat_state_checkpoint_identity_ceiling": (
            goat_auxiliary_stage1_runtime_completion_budget_for_identity(
                goat=True
            )
        ),
        "translation_total_completion_tokens": (
            GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
        ),
        "provider_authorization": {
            "provider_calls_authorized": False,
            "provider_calls_observed": 0,
            "credential_access": False,
            "retry_authorized": False,
        },
        "scope": {
            "benchmark_only": BENCHMARK_ONLY_SCOPE,
            "production_policy_changed": False,
            "historical_8k_policy_changed": False,
            "historical_4k_policy_changed": False,
            "applied_before_request_identity": True,
        },
    }


def _validate_goat_auxiliary_stage1_runtime_policy() -> None:
    """Prove the additive Stage-1 runtime policy and its frozen base agree."""
    if GOAT_AUXILIARY_STAGE1_RUNTIME_GOAT_ONLY is not True:
        raise G7JPolicyError(
            "the GOAT Stage-1 runtime auxiliary policy must be GOAT-only"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS != (
        GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the Stage-1 runtime auxiliary total must be the corrected 8000"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS == (
        GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the Stage-1 runtime policy must be additive and must differ from "
            "the frozen 4K auxiliary total"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS == (
        UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the Stage-1 runtime auxiliary total must never be the unsupported "
            "32k value"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_REVISION != (
        GOAT_AUXILIARY_8K_REVISION
    ):
        raise G7JPolicyError(
            "the Stage-1 runtime policy must bind the frozen G7N 8K policy"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_SHA256 != (
        G7J_AUXILIARY_8K_POLICY_SHA256
    ):
        raise G7JPolicyError(
            "the Stage-1 runtime policy must bind the frozen G7N 8K policy SHA"
        )
    if GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_AUXILIARY_COMPLETION_TOKENS != (
        PRODUCTION_AUXILIARY_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the production auxiliary ceiling must stay the frozen 3000"
        )
    if GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS != 24_000:
        raise G7JPolicyError(
            "the GOAT primary-translation total must stay exactly 24000"
        )

    production_table = (
        _goat_auxiliary_stage1_runtime_production_subrequest_ceilings_by_operation()
    )
    if set(production_table) != set(GOAT_AUXILIARY_OPERATIONS):
        raise G7JPolicyError(
            "the Stage-1 runtime production subrequest table must name exactly "
            "every GOAT auxiliary operation"
        )
    if PRIMARY_TRANSLATION_OPERATION in production_table:
        raise G7JPolicyError(
            "primary translation must never appear in the Stage-1 runtime "
            "production subrequest table"
        )

    frozen_8k_accepted = {
        operation: _goat_auxiliary_8k_accepted_incoming_tokens(operation)
        for operation in GOAT_AUXILIARY_OPERATIONS
    }
    for operation in GOAT_AUXILIARY_OPERATIONS:
        accepted = (
            _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(operation)
        )
        # The runtime accepted set is a strict superset of the frozen G7N set.
        if not frozen_8k_accepted[operation] <= accepted:
            raise G7JPolicyError(
                "the Stage-1 runtime policy must keep every frozen G7N accepted "
                f"incoming ceiling for {operation!r}"
            )
        for incoming in sorted(accepted):
            if goat_auxiliary_stage1_runtime_completion_budget(
                logical_operation=operation,
                requested_tokens=incoming,
            ) != GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS:
                raise G7JPolicyError(
                    f"GOAT auxiliary operation {operation!r} did not receive the "
                    "Stage-1 runtime ceiling"
                )
        # The frozen G7N 8K helper stays byte-frozen: it must still reject every
        # production subrequest ceiling that is not that operation's own frozen
        # baseline or a historical transition total.
        baseline = _goat_auxiliary_baseline_by_operation()[operation]
        historical_transitions = {
            GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS,
            GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS,
        }
        for subrequest in production_table[operation]:
            if (
                subrequest == baseline
                or subrequest in historical_transitions
            ):
                continue
            try:
                goat_auxiliary_8k_completion_budget(
                    logical_operation=operation,
                    requested_tokens=subrequest,
                )
            except G7JPolicyError:
                continue
            raise G7JPolicyError(
                "the frozen G7N 8K policy must never accept the Stage-1 runtime "
                f"production subrequest ceiling {subrequest} for {operation!r}"
            )

    # The two production ceilings that provoked G7P must be accepted.
    if 2_000 not in _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
        LIFECYCLE_OPERATION_GLOSSARY_REVIEW
    ):
        raise G7JPolicyError(
            "the glossary schema/semantic correction ceiling 2000 must be "
            "accepted by the Stage-1 runtime policy"
        )
    if 5_000 not in _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
        LIFECYCLE_OPERATION_GLOSSARY_REVIEW
    ):
        raise G7JPolicyError(
            "the glossary generic JSON repair ceiling 5000 must be accepted by "
            "the Stage-1 runtime policy"
        )
    if 800 not in _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
        LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR
    ):
        raise G7JPolicyError(
            "the terminology micro-repair ceiling 800 must be accepted by the "
            "Stage-1 runtime policy"
        )

    # Closure: unrecognized ceilings fail closed for every operation.
    for drifted in (0, 1, 2_999, 3_001, 6_000, 7_999, 12_000, 24_000, 32_000):
        for operation in GOAT_AUXILIARY_OPERATIONS:
            if drifted in (
                _goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
                    operation
                )
            ):
                continue
            try:
                goat_auxiliary_stage1_runtime_completion_budget(
                    logical_operation=operation,
                    requested_tokens=drifted,
                )
            except G7JPolicyError:
                continue
            raise G7JPolicyError(
                "GOAT Stage-1 runtime incoming ceiling drift must fail closed: "
                f"{drifted} for {operation!r}"
            )
    for malformed in (True, False, 8_000.0, "8000", None):
        try:
            goat_auxiliary_stage1_runtime_completion_budget(
                logical_operation=LIFECYCLE_OPERATION_STATE_EXTRACTION,
                requested_tokens=malformed,  # type: ignore[arg-type]
            )
        except G7JPolicyError:
            continue
        raise G7JPolicyError(
            "a non-integer Stage-1 runtime incoming ceiling must fail closed"
        )
    for operation in (PRIMARY_TRANSLATION_OPERATION, "not_an_operation", None):
        try:
            goat_auxiliary_stage1_runtime_completion_budget(
                logical_operation=operation,  # type: ignore[arg-type]
                requested_tokens=(
                    GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS
                ),
            )
        except G7JPolicyError:
            continue
        raise G7JPolicyError(
            "primary translation and unknown operations must never be routed "
            "through the GOAT Stage-1 runtime auxiliary budget"
        )
    if goat_auxiliary_stage1_runtime_completion_budget_for_identity(
        goat=False
    ) != 3_000:
        raise G7JPolicyError(
            "the production Stage-1 runtime identity ceiling must stay 3000"
        )
    if goat_auxiliary_stage1_runtime_completion_budget_for_identity(
        goat=True
    ) != GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS:
        raise G7JPolicyError(
            "the GOAT Stage-1 runtime identity ceiling must be the 8K total"
        )


def classify_auxiliary_response(
    *,
    logical_operation: str,
    content: str,
    finish_reason: str | None,
    completion_tokens: int | None,
    max_completion_tokens: int,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """Mechanically classify one normalized auxiliary response.

    The classifier is deterministic, bounded, and never coerces evidence:
    ``content`` must already be a string, and a non-string, malformed
    ``finish_reason``, or malformed token count fails closed with a policy error
    instead of being coerced.

    Exactly one of the five statuses is returned:

    * ``AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT`` -- empty/whitespace content
      with ``finish_reason == "length"`` at or above
      ``max_completion_tokens - NEAR_COMPLETION_CEILING_TOLERANCE`` (the incident
      case);
    * ``AUXILIARY_EMPTY_CONTENT_LENGTH`` -- empty content, length-terminated,
      below the ceiling;
    * ``AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH`` -- empty content with any
      other finish reason;
    * ``AUXILIARY_MALFORMED_JSON_REPAIRED`` -- nonempty content that is not a
      JSON object, so one legitimate repair is allowed;
    * ``AUXILIARY_STATE_ACCEPTED`` -- nonempty content that is a JSON object.

    Every empty-content status is fail-closed:
    ``"repair_allowed": False`` and ``"fail_closed": True``. Nothing here reads
    a file, performs Provider I/O, or authorizes paid work.
    """
    if (
        not isinstance(logical_operation, str)
        or logical_operation not in GOAT_AUXILIARY_OPERATIONS
    ):
        raise G7JPolicyError(
            "logical_operation must be a GOAT auxiliary operation; "
            f"found {logical_operation!r}"
        )
    if not isinstance(content, str):
        raise G7JPolicyError("content must be a string")
    maximum = _positive_int(max_completion_tokens, "max_completion_tokens")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise G7JPolicyError("finish_reason must be a string when present")
    if completion_tokens is not None:
        _nonnegative_int(completion_tokens, "completion_tokens")
    if reasoning_tokens is not None:
        reported_reasoning = _nonnegative_int(
            reasoning_tokens, "reasoning_tokens"
        )
        if (
            completion_tokens is not None
            and reported_reasoning > completion_tokens
        ):
            raise G7JPolicyError(
                "reasoning_tokens cannot exceed completion_tokens"
            )

    visible_empty = not content.strip()
    near_ceiling = (
        completion_tokens is not None
        and completion_tokens
        >= maximum - NEAR_COMPLETION_CEILING_TOLERANCE
    )

    valid_json_object = False
    if not visible_empty:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        valid_json_object = isinstance(parsed, dict)

    if visible_empty:
        if finish_reason == FINISH_REASON_LENGTH:
            status = (
                AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
                if near_ceiling
                else AUXILIARY_EMPTY_CONTENT_LENGTH
            )
        else:
            status = AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH
    elif valid_json_object:
        status = AUXILIARY_STATE_ACCEPTED
    else:
        status = AUXILIARY_MALFORMED_JSON_REPAIRED

    return {
        "status": status,
        "logical_operation": logical_operation,
        "visible_content_empty": visible_empty,
        "valid_json_object": valid_json_object,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "max_completion_tokens": maximum,
        "near_completion_ceiling": near_ceiling,
        "repair_allowed": status == AUXILIARY_MALFORMED_JSON_REPAIRED,
        "fail_closed": status in AUXILIARY_FAIL_CLOSED_STATUSES,
    }


def _historical_auxiliary_incident_evidence() -> dict[str, Any]:
    """Return every frozen immutable fact of the Run-1 auxiliary incident.

    The classifier is re-run over the frozen content/finish-reason/token facts
    so the recorded classification cannot silently drift from the frozen
    evidence, and the recorded classification must be exactly the fail-closed
    budget-exhausted status. This is pure computation over constants: it reads no
    file, journal, ledger, progress file, or chapter artifact.
    """
    baselines = _goat_auxiliary_baseline_by_operation()
    if HISTORICAL_AUXILIARY_OPERATION not in baselines:
        raise G7JPolicyError(
            "the historical auxiliary incident operation is not a GOAT "
            "auxiliary operation"
        )
    if baselines[HISTORICAL_AUXILIARY_OPERATION] != (
        HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the historical auxiliary request ceiling drifted from the frozen "
            "state-extraction baseline"
        )
    if HISTORICAL_AUXILIARY_MODEL_ID not in BENCHMARK_MODEL_IDS:
        raise G7JPolicyError(
            "the historical auxiliary incident model is not part of the frozen "
            "GOAT roster"
        )
    if HISTORICAL_AUXILIARY_REASONING_TOKENS != (
        HISTORICAL_AUXILIARY_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "the historical auxiliary incident consumed every completion token "
            "as reasoning with zero visible content"
        )

    incident = classify_auxiliary_response(
        logical_operation=HISTORICAL_AUXILIARY_OPERATION,
        content=HISTORICAL_AUXILIARY_VISIBLE_CONTENT,
        finish_reason=HISTORICAL_AUXILIARY_FINISH_REASON,
        completion_tokens=HISTORICAL_AUXILIARY_COMPLETION_TOKENS,
        max_completion_tokens=(
            HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
        ),
        reasoning_tokens=HISTORICAL_AUXILIARY_REASONING_TOKENS,
    )
    if incident["status"] != HISTORICAL_AUXILIARY_CLASSIFICATION:
        raise G7JPolicyError(
            "the historical auxiliary incident evidence must classify as "
            "auxiliary_budget_exhausted_empty_content"
        )
    if incident["fail_closed"] is not True:
        raise G7JPolicyError(
            "the historical auxiliary incident classification must be fail-closed"
        )
    if incident["repair_allowed"] is not False:
        raise G7JPolicyError(
            "the historical auxiliary incident must never allow JSON repair"
        )

    return {
        "run_index": HISTORICAL_AUXILIARY_RUN_INDEX,
        "chapter": HISTORICAL_AUXILIARY_CHAPTER,
        "model_id": HISTORICAL_AUXILIARY_MODEL_ID,
        "reasoning_effort": HISTORICAL_AUXILIARY_REASONING_EFFORT,
        "logical_operation": HISTORICAL_AUXILIARY_OPERATION,
        "request_max_completion_tokens": (
            HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
        ),
        "completion_tokens": HISTORICAL_AUXILIARY_COMPLETION_TOKENS,
        "reasoning_tokens": HISTORICAL_AUXILIARY_REASONING_TOKENS,
        "visible_content": HISTORICAL_AUXILIARY_VISIBLE_CONTENT,
        "visible_content_empty": incident["visible_content_empty"],
        "finish_reason": HISTORICAL_AUXILIARY_FINISH_REASON,
        "near_completion_ceiling": incident["near_completion_ceiling"],
        "local_salvage_returned_none": (
            HISTORICAL_AUXILIARY_LOCAL_SALVAGE_RETURNED_NONE
        ),
        "repair_fed_empty_input": (
            HISTORICAL_AUXILIARY_REPAIR_FED_EMPTY_INPUT
        ),
        "repair_produced_fabricated_state": (
            HISTORICAL_AUXILIARY_REPAIR_PRODUCED_FABRICATED_STATE
        ),
        "ledger_new_provider_sends_started": (
            HISTORICAL_AUXILIARY_LEDGER_NEW_PROVIDER_SENDS_STARTED
        ),
        "classification": incident["status"],
        "fail_closed": incident["fail_closed"],
        "repair_allowed": incident["repair_allowed"],
        "corrected_auxiliary_ceiling": (
            HISTORICAL_AUXILIARY_CORRECTED_CEILING
        ),
        "reasoning_text_persisted": False,
    }


def translation_reasoning_effort_by_model() -> dict[str, str | None]:
    """Return the recorded frozen GOAT primary-translation effort bindings."""
    _validate_frozen_policy()
    return {
        model_id: TRANSLATION_EFFORT_BY_MODEL[model_id]
        for model_id in BENCHMARK_MODEL_IDS
    }


def reasoning_diagnostic_classification(
    *,
    completion_tokens: Any,
    reasoning_tokens: Any,
    reasoning_tokens_exposed: Any,
) -> str:
    """Return the exact bounded classification of one reasoning diagnostic.

    This is the single rule table shared by the raising normalizer
    (:func:`reasoning_usage_diagnostics`) and the non-raising bounded outcome
    (:func:`reasoning_diagnostic_outcome`), so a durable response can never be
    classified differently from the accounting that was persisted for it.

    ``REASONING_DIAGNOSTIC_OK`` means the reported diagnostic is exactly
    acceptable. Anything else is a deterministic inconsistency. Nothing here
    raises, reads files, or performs Provider I/O.
    """
    if (
        isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens < 0
    ):
        return REASONING_DIAGNOSTIC_COMPLETION_TOTAL_MALFORMED
    if not isinstance(reasoning_tokens_exposed, bool):
        return REASONING_DIAGNOSTIC_EXPOSURE_METADATA_MALFORMED
    if not reasoning_tokens_exposed:
        if reasoning_tokens is not None:
            return REASONING_DIAGNOSTIC_UNEXPOSED_VALUE_PRESENT
        return REASONING_DIAGNOSTIC_OK
    if (
        isinstance(reasoning_tokens, bool)
        or not isinstance(reasoning_tokens, int)
        or reasoning_tokens < 0
    ):
        return REASONING_DIAGNOSTIC_VALUE_MALFORMED
    if reasoning_tokens > completion_tokens:
        return REASONING_DIAGNOSTIC_EXCEEDS_COMPLETION_TOTAL
    return REASONING_DIAGNOSTIC_OK


def reasoning_diagnostic_outcome(
    *,
    completion_tokens: Any,
    reasoning_tokens: Any,
    reasoning_tokens_exposed: Any,
) -> dict[str, Any]:
    """Return a bounded, never-raising reasoning-diagnostic outcome.

    A deterministic post-response diagnostic inconsistency is reported as an
    exact bounded classification instead of raising, so a definitive paid
    response can still be recorded and accounted exactly once rather than
    becoming an uncertain paid outcome. ``diagnostics`` is the normalized
    non-billing diagnostic mapping for ``REASONING_DIAGNOSTIC_OK`` and ``None``
    for every other classification; an untrustworthy subtotal is therefore never
    returned for use.
    """
    classification = reasoning_diagnostic_classification(
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        reasoning_tokens_exposed=reasoning_tokens_exposed,
    )
    if classification != REASONING_DIAGNOSTIC_OK:
        return {
            "classification": classification,
            "diagnostics": None,
        }
    return {
        "classification": classification,
        "diagnostics": reasoning_usage_diagnostics(
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            reasoning_tokens_exposed=reasoning_tokens_exposed,
        ),
    }


def reasoning_usage_diagnostics(
    *,
    completion_tokens: int,
    reasoning_tokens: int | None,
    reasoning_tokens_exposed: bool,
) -> dict[str, Any]:
    """Normalize non-billing reasoning-token diagnostics.

    Completion-token accounting remains authoritative and unchanged. Reasoning
    tokens are a diagnostic subset only when the Provider explicitly reports
    them; an unreported subtotal is never guessed. Only the reported subtotal is
    ever read here -- no reasoning text or thinking trace is accepted.
    """
    classification = reasoning_diagnostic_classification(
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        reasoning_tokens_exposed=reasoning_tokens_exposed,
    )
    if classification != REASONING_DIAGNOSTIC_OK:
        raise G7JPolicyError(
            REASONING_DIAGNOSTIC_ERROR_MESSAGES[classification]
        )

    if not reasoning_tokens_exposed:
        return {
            "reasoning_tokens_exposed": False,
            "reasoning_tokens": None,
        }

    return {
        "reasoning_tokens_exposed": True,
        "reasoning_tokens": reasoning_tokens,
        "visible_completion_tokens": completion_tokens - reasoning_tokens,
    }


def augment_accounting_stats_with_reasoning(
    stats: Mapping[str, Any],
    *,
    reasoning_tokens: int | None = None,
    reasoning_tokens_exposed: bool = False,
) -> dict[str, Any]:
    """Return copied accounting stats plus non-billing reasoning diagnostics.

    When the Provider reports no reasoning subtotal, the returned mapping is
    byte-for-byte the pre-G7J stats schema: no diagnostic key is manufactured.
    """
    if not isinstance(stats, Mapping):
        raise G7JPolicyError("stats must be a mapping")
    completion = stats.get("completion_tokens")
    completion = _nonnegative_int(completion, "stats completion_tokens")

    diagnostics = reasoning_usage_diagnostics(
        completion_tokens=completion,
        reasoning_tokens=reasoning_tokens,
        reasoning_tokens_exposed=reasoning_tokens_exposed,
    )

    value = copy.deepcopy(dict(stats))
    if diagnostics["reasoning_tokens_exposed"]:
        value.update(diagnostics)
    return value


def classify_translation_response(
    *,
    content: str,
    finish_reason: str | None,
    completion_tokens: int | None,
    max_completion_tokens: int,
) -> dict[str, Any]:
    """Mechanically classify one normalized visible translation response.

    The classifier is deterministic and bounded. ``content`` is never coerced
    with ``str()``: a non-string value fails closed.
    """
    if not isinstance(content, str):
        raise G7JPolicyError("content must be a string")
    maximum = _positive_int(max_completion_tokens, "max_completion_tokens")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise G7JPolicyError("finish_reason must be a string when present")
    if completion_tokens is not None:
        _nonnegative_int(completion_tokens, "completion_tokens")

    visible_empty = not content.strip()
    near_ceiling = (
        completion_tokens is not None
        and completion_tokens
        >= maximum - NEAR_COMPLETION_CEILING_TOLERANCE
    )

    if visible_empty:
        if finish_reason == FINISH_REASON_LENGTH:
            status = (
                STATUS_COMPLETION_BUDGET_EXHAUSTED_BEFORE_VISIBLE_CONTENT
                if near_ceiling
                else STATUS_EMPTY_VISIBLE_CONTENT_LENGTH_TERMINATED
            )
        else:
            status = STATUS_EMPTY_VISIBLE_CONTENT
    elif finish_reason == FINISH_REASON_LENGTH:
        status = STATUS_VISIBLE_CONTENT_TRUNCATED_BY_COMPLETION_BUDGET
    elif finish_reason == FINISH_REASON_STOP:
        status = STATUS_OK
    else:
        status = STATUS_UNSUPPORTED_TRANSLATION_FINISH_REASON

    return {
        "status": status,
        "visible_content_empty": visible_empty,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "max_completion_tokens": maximum,
        "near_completion_ceiling": near_ceiling,
    }


def _historical_run_1_evidence() -> dict[str, Any]:
    """Return every frozen immutable fact of the historical Run-1 incident.

    The classifier is re-run over the frozen content/finish-reason/completion
    facts so the recorded classification cannot silently drift from the frozen
    evidence. This is pure computation over constants: it reads no file, journal,
    ledger, progress file, or chapter artifact.
    """
    if HISTORICAL_RUN_1_MODEL_ID not in BENCHMARK_MODEL_IDS:
        raise G7JPolicyError(
            "historical Run-1 model is not part of the frozen GOAT roster"
        )
    if TRANSLATION_EFFORT_BY_MODEL[HISTORICAL_RUN_1_MODEL_ID] != (
        HISTORICAL_RUN_1_REASONING_EFFORT
    ):
        raise G7JPolicyError(
            "historical Run-1 reasoning effort drifted from the frozen binding"
        )
    if HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS != (
        GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
    ):
        raise G7JPolicyError(
            "historical Run-1 request ceiling drifted from the frozen baseline"
        )

    run1 = classify_translation_response(
        content=HISTORICAL_RUN_1_VISIBLE_CONTENT,
        finish_reason=HISTORICAL_RUN_1_FINISH_REASON,
        completion_tokens=HISTORICAL_RUN_1_COMPLETION_TOKENS,
        max_completion_tokens=(
            HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS
        ),
    )
    if run1["status"] != HISTORICAL_RUN_1_CLASSIFICATION:
        raise G7JPolicyError(
            "historical Run-1 evidence must classify as "
            "completion_budget_exhausted_before_visible_content"
        )

    return {
        "model_id": HISTORICAL_RUN_1_MODEL_ID,
        "reasoning_effort": HISTORICAL_RUN_1_REASONING_EFFORT,
        "prompt_tokens": HISTORICAL_RUN_1_PROMPT_TOKENS,
        "completion_tokens": HISTORICAL_RUN_1_COMPLETION_TOKENS,
        "request_max_completion_tokens": (
            HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS
        ),
        "visible_content": HISTORICAL_RUN_1_VISIBLE_CONTENT,
        "visible_content_empty": run1["visible_content_empty"],
        "finish_reason": HISTORICAL_RUN_1_FINISH_REASON,
        "near_completion_ceiling": run1["near_completion_ceiling"],
        "accepted_credit_equivalent_usd": (
            HISTORICAL_RUN_1_ACCEPTED_CREDIT_EQUIVALENT_USD
        ),
        "ledger_status": HISTORICAL_RUN_1_LEDGER_STATUS,
        "ledger_new_provider_sends_started": (
            HISTORICAL_RUN_1_LEDGER_NEW_PROVIDER_SENDS_STARTED
        ),
        "journal_status": HISTORICAL_RUN_1_JOURNAL_STATUS,
        "progress_last_completed": (
            HISTORICAL_RUN_1_PROGRESS_LAST_COMPLETED
        ),
        "progress_next_chapter": (
            HISTORICAL_RUN_1_PROGRESS_NEXT_CHAPTER
        ),
        "pending_artifact_present": (
            HISTORICAL_RUN_1_PENDING_ARTIFACT_PRESENT
        ),
        "final_artifact_present": (
            HISTORICAL_RUN_1_FINAL_ARTIFACT_PRESENT
        ),
        "consumed_paid_authorization_reusable": (
            HISTORICAL_RUN_1_CONSUMED_PAID_AUTHORIZATION_REUSABLE
        ),
        "classification": run1["status"],
        "reasoning_text_persisted": False,
        "reasoning_tokens_optional_diagnostic_only": True,
    }


def g7j_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider G7J policy identity.

    The identity records every frozen historical Run-1 incident fact, the
    evidence-derived 24,000 correction, the explicit rejection of 32,000,
    roster-wide fairness across all six GOAT models, benchmark-only scope, and
    the absence of any Provider authorization. It performs no I/O.
    """
    _validate_frozen_policy()
    _validate_frozen_auxiliary_policy()

    historical_run_1_evidence = _historical_run_1_evidence()
    historical_auxiliary_incident_evidence = (
        _historical_auxiliary_incident_evidence()
    )

    roster: list[dict[str, Any]] = []
    for model_id in BENCHMARK_MODEL_IDS:
        effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
        corrected = translation_completion_budget(
            logical_operation=PRIMARY_TRANSLATION_OPERATION,
            requested_tokens=GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS,
            reasoning_effort=effort,
        )
        if corrected != GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS:
            raise G7JPolicyError(
                f"GOAT model {model_id!r} did not receive the corrected total"
            )
        roster.append(
            {
                "model_id": model_id,
                "reasoning_effort": effort,
                "reasoning_effort_field_present": effort is not None,
                "baseline_completion_tokens": (
                    GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
                ),
                "corrected_total_completion_tokens": corrected,
            }
        )

    return {
        "schema_version": G7J_SCHEMA_VERSION,
        "revision": G7J_REVISION,
        "historical_run_1_evidence": historical_run_1_evidence,
        "completion_budget_policy": {
            "incoming_goat_primary_translation_baseline": (
                GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
            ),
            "corrected_goat_primary_translation_total": (
                GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
            ),
            "derivation": (
                "12000 demonstrated pre-visible/reasoning reserve "
                "+ 12000 original visible translation allowance"
            ),
            "explicitly_unsupported_completion_tokens": [
                UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
            ],
            "max_selection_used": False,
            "incoming_baseline_drift_fails_closed": True,
            # The production auxiliary path is unchanged: the G7L auxiliary
            # correction is opt-in and is enumerated in full under
            # ``auxiliary_completion_budget_policy``.
            "auxiliary_operations_unchanged": True,
            "auxiliary_operations_unchanged_without_goat_opt_in": True,
            "production_completion_budget_unchanged": (
                GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
            ),
            "near_completion_ceiling_tolerance": (
                NEAR_COMPLETION_CEILING_TOLERANCE
            ),
        },
        "roster_fairness": {
            "all_six_goat_models_corrected_to_total": True,
            "default_only_models_included": True,
            "models": roster,
        },
        "historical_auxiliary_incident_evidence": (
            historical_auxiliary_incident_evidence
        ),
        "auxiliary_completion_budget_policy": {
            "incoming_goat_auxiliary_baseline": (
                GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS
            ),
            "corrected_goat_auxiliary_total": (
                GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
            ),
            "derivation": (
                "the accepted incident response was empty with "
                "finish_reason length at exactly the frozen 3000 auxiliary "
                "ceiling (3000 reasoning, 0 visible); 4000 is the smallest "
                "round ceiling that leaves visible headroom for the compact "
                "auxiliary JSON schemas"
            ),
            "frozen_incoming_auxiliary_baselines": [
                [operation, requested]
                for operation, requested in (
                    GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS
                )
            ],
            "auxiliary_operations": list(GOAT_AUXILIARY_OPERATIONS),
            "uniform_for_all_six_goat_models": True,
            "model_dependent": False,
            "effort_dependent": False,
            "translation_keeps_corrected_total": (
                GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
            ),
            "explicitly_unsupported_completion_tokens": [
                UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS
            ],
            "max_selection_used": False,
            "incoming_baseline_drift_fails_closed": True,
            "applied_only_when_caller_opts_in": True,
            "production_auxiliary_budgets_unchanged": [
                [operation, requested]
                for operation, requested in (
                    GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS
                )
            ],
            "production_state_checkpoint_identity_ceiling": (
                auxiliary_completion_budget_for_identity(goat=False)
            ),
            "goat_state_checkpoint_identity_ceiling": (
                auxiliary_completion_budget_for_identity(goat=True)
            ),
            "near_completion_ceiling_tolerance": (
                NEAR_COMPLETION_CEILING_TOLERANCE
            ),
            "empty_auxiliary_response_never_repaired": True,
            "response_statuses": sorted(AUXILIARY_RESPONSE_STATUSES),
            "fail_closed_statuses": sorted(AUXILIARY_FAIL_CLOSED_STATUSES),
        },
        "scope": {
            "benchmark_only": BENCHMARK_ONLY_SCOPE,
            "production_policy_changed": False,
            "applied_after_g7c_normalization": True,
            "applied_before_request_identity": True,
        },
        "provider_authorization": {
            "provider_calls_authorized": False,
            "provider_calls_observed": 0,
            "credential_access": False,
            "retry_authorized": False,
        },
    }


G7J_POLICY_SHA256 = _sha256_json(g7j_policy_identity())

# The additive 8K auxiliary policy (G7N) is computed and validated at import,
# immediately AFTER the existing frozen-policy validation above (which runs
# inside ``g7j_policy_identity()``). G7J_POLICY_SHA256, the 4K auxiliary payload,
# and every frozen assertion above are untouched: the 8K policy is purely
# additive and is deliberately not part of ``g7j_policy_identity()``.
G7J_AUXILIARY_8K_POLICY_SHA256 = _sha256_json(g7j_auxiliary_8k_policy_identity())
_validate_goat_auxiliary_8k_policy()

# The additive Stage-1 runtime auxiliary subrequest-compatibility policy (G7P)
# is computed and validated at import, immediately AFTER the frozen G7N 8K
# policy. It binds the frozen G7N policy SHA as its unchanged base, so
# G7J_POLICY_SHA256, G7J_AUXILIARY_8K_POLICY_SHA256, the frozen 4K payload, and
# every G7N/G7O historical/recovery binding are untouched.
GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_SHA256 = (
    G7J_AUXILIARY_8K_POLICY_SHA256
)
G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256 = _sha256_json(
    g7p_stage1_runtime_auxiliary_policy_identity()
)
_validate_goat_auxiliary_stage1_runtime_policy()


__all__ = [
    "AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT",
    "AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH",
    "AUXILIARY_EMPTY_CONTENT_LENGTH",
    "AUXILIARY_FAIL_CLOSED_STATUSES",
    "AUXILIARY_MALFORMED_JSON_REPAIRED",
    "AUXILIARY_RESPONSE_STATUSES",
    "AUXILIARY_STATE_ACCEPTED",
    "BENCHMARK_ONLY_SCOPE",
    "EXPECTED_TRANSLATION_REASONING_EFFORT_BY_MODEL",
    "FINISH_REASON_LENGTH",
    "FINISH_REASON_STOP",
    "G7J_AUXILIARY_8K_POLICY_SHA256",
    "G7J_POLICY_SHA256",
    "G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256",
    "G7JPolicyError",
    "G7J_REVISION",
    "G7J_SCHEMA_VERSION",
    "GOAT_AUXILIARY_8K_INCOMING_BASELINE_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_8K_LEGACY_TOTAL_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_8K_OBSERVED_PRE_VISIBLE_REASONING_RESERVE_TOKENS",
    "GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_8K_REVISION",
    "GOAT_AUXILIARY_8K_SCHEMA_VERSION",
    "GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_8K_VISIBLE_STRUCTURED_OUTPUT_ALLOWANCE_TOKENS",
    "GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_OPERATIONS",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_REVISION",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_SHA256",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_GOAT_ONLY",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_AUXILIARY_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_SUBREQUEST_CEILING_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_SCHEMA_VERSION",
    "GOAT_AUXILIARY_STAGE1_RUNTIME_TOTAL_COMPLETION_TOKENS",
    "GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS",
    "GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS",
    "GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS",
    "HISTORICAL_AUXILIARY_CHAPTER",
    "HISTORICAL_AUXILIARY_CLASSIFICATION",
    "HISTORICAL_AUXILIARY_COMPLETION_TOKENS",
    "HISTORICAL_AUXILIARY_CORRECTED_CEILING",
    "HISTORICAL_AUXILIARY_FINISH_REASON",
    "HISTORICAL_AUXILIARY_LEDGER_NEW_PROVIDER_SENDS_STARTED",
    "HISTORICAL_AUXILIARY_LOCAL_SALVAGE_RETURNED_NONE",
    "HISTORICAL_AUXILIARY_MODEL_ID",
    "HISTORICAL_AUXILIARY_OPERATION",
    "HISTORICAL_AUXILIARY_REASONING_EFFORT",
    "HISTORICAL_AUXILIARY_REASONING_TOKENS",
    "HISTORICAL_AUXILIARY_REPAIR_FED_EMPTY_INPUT",
    "HISTORICAL_AUXILIARY_REPAIR_PRODUCED_FABRICATED_STATE",
    "HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS",
    "HISTORICAL_AUXILIARY_RUN_INDEX",
    "HISTORICAL_AUXILIARY_VISIBLE_CONTENT",
    "HISTORICAL_RUN_1_ACCEPTED_CREDIT_EQUIVALENT_USD",
    "HISTORICAL_RUN_1_CLASSIFICATION",
    "HISTORICAL_RUN_1_COMPLETION_TOKENS",
    "HISTORICAL_RUN_1_CONSUMED_PAID_AUTHORIZATION_REUSABLE",
    "HISTORICAL_RUN_1_FINAL_ARTIFACT_PRESENT",
    "HISTORICAL_RUN_1_FINISH_REASON",
    "HISTORICAL_RUN_1_JOURNAL_STATUS",
    "HISTORICAL_RUN_1_LEDGER_NEW_PROVIDER_SENDS_STARTED",
    "HISTORICAL_RUN_1_LEDGER_STATUS",
    "HISTORICAL_RUN_1_MODEL_ID",
    "HISTORICAL_RUN_1_PENDING_ARTIFACT_PRESENT",
    "HISTORICAL_RUN_1_PROGRESS_LAST_COMPLETED",
    "HISTORICAL_RUN_1_PROGRESS_NEXT_CHAPTER",
    "HISTORICAL_RUN_1_PROMPT_TOKENS",
    "HISTORICAL_RUN_1_REASONING_EFFORT",
    "HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS",
    "HISTORICAL_RUN_1_VISIBLE_CONTENT",
    "NEAR_COMPLETION_CEILING_TOLERANCE",
    "PRIMARY_TRANSLATION_OPERATION",
    "PRODUCTION_AUXILIARY_COMPLETION_TOKENS",
    "REASONING_DIAGNOSTIC_CLASSIFICATIONS",
    "REASONING_DIAGNOSTIC_COMPLETION_TOTAL_MALFORMED",
    "REASONING_DIAGNOSTIC_ERROR_MESSAGES",
    "REASONING_DIAGNOSTIC_EXCEEDS_COMPLETION_TOTAL",
    "REASONING_DIAGNOSTIC_EXPOSURE_METADATA_MALFORMED",
    "REASONING_DIAGNOSTIC_OK",
    "REASONING_DIAGNOSTIC_UNEXPOSED_VALUE_PRESENT",
    "REASONING_DIAGNOSTIC_VALUE_MALFORMED",
    "STATUS_COMPLETION_BUDGET_EXHAUSTED_BEFORE_VISIBLE_CONTENT",
    "STATUS_EMPTY_VISIBLE_CONTENT",
    "STATUS_EMPTY_VISIBLE_CONTENT_LENGTH_TERMINATED",
    "STATUS_OK",
    "STATUS_UNSUPPORTED_TRANSLATION_FINISH_REASON",
    "STATUS_VISIBLE_CONTENT_TRUNCATED_BY_COMPLETION_BUDGET",
    "TRANSLATION_RESPONSE_STATUSES",
    "UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS",
    "augment_accounting_stats_with_reasoning",
    "auxiliary_completion_budget_for_identity",
    "classify_auxiliary_response",
    "classify_translation_response",
    "g7j_auxiliary_8k_policy_identity",
    "g7j_policy_identity",
    "g7p_stage1_runtime_auxiliary_policy_identity",
    "goat_auxiliary_8k_completion_budget",
    "goat_auxiliary_8k_completion_budget_for_identity",
    "goat_auxiliary_completion_budget",
    "goat_auxiliary_stage1_runtime_completion_budget",
    "goat_auxiliary_stage1_runtime_completion_budget_for_identity",
    "reasoning_diagnostic_classification",
    "reasoning_diagnostic_outcome",
    "reasoning_usage_diagnostics",
    "translation_completion_budget",
    "translation_reasoning_effort_by_model",
]
