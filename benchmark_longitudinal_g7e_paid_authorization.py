"""G7E frozen paid Stage-1 execution authorization contract.

G7E resolves the final generic Stage-1 blocker without itself making a Provider
call. It freezes how later paid invocations must be authorized and resumed.

One invocation may target exactly one model×novel run and one chapter. Every
invocation binds the exact G7C run identity, current state fingerprint, a finite
new-send ceiling, a finite credit-equivalent cost ceiling, and a deterministic
approval token.

The production paid-request journal remains authoritative:
- absent/planned may make the exact journaled send when otherwise allowed;
- response_recorded is replay-only and consumes no new Provider send;
- started/uncertain/corrupt or identity-drift state blocks automatic reissue.

G7E freezes authorization semantics only. A later executor must still create an
exact invocation plan, obtain explicit operator authorization, install the real
Command Code transport/accounting runtime, enforce the plan ceilings, and call
only run_unattended_production(target_chapter=<approved chapter>).

Importing this module performs no filesystem writes, credential reads, or
Provider calls.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import copy
import hashlib
import json
import re
from typing import Any, Mapping

from benchmark_longitudinal_g7a_readiness import STAGE1_LAST_CHAPTER
from benchmark_longitudinal_g7c_stage1_integration import (
    G7C_INTEGRATION_SHA256,
    stage1_run_specs,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    G7D_ACCOUNTING_SHA256,
    g7d_accounting_identity,
)


G7E_SCHEMA_VERSION = 1
G7E_REVISION = "goat-g7e-paid-stage1-authorization-contract-v1"
G7E_REPOSITORY_ANCHOR = "8bf6306246dfe110b8b02896f632e9a72fa796a2"

EXPECTED_G7C_INTEGRATION_SHA256 = (
    "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
)
EXPECTED_G7D_ACCOUNTING_SHA256 = (
    "1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0"
)

RESOLVED_G7D_BLOCKER = "freeze_stage1_paid_execution_authorization_contract"
AUTHORIZATION_MODE = "single_run_single_target_chapter"
INVOCATION_SCHEMA_VERSION = 1
INVOCATION_REVISION = "goat-g7e-stage1-invocation-authorization-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_JOURNAL_STATUSES = frozenset(
    {"planned", "started", "response_recorded", "uncertain"}
)


class G7EAuthorizationError(RuntimeError):
    """Paid Stage-1 authorization state is invalid or unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7EAuthorizationError(
            "G7E value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_predecessors() -> dict[str, Any]:
    if G7C_INTEGRATION_SHA256 != EXPECTED_G7C_INTEGRATION_SHA256:
        raise G7EAuthorizationError("G7C integration identity changed")
    if G7D_ACCOUNTING_SHA256 != EXPECTED_G7D_ACCOUNTING_SHA256:
        raise G7EAuthorizationError("G7D accounting identity changed")

    accounting = g7d_accounting_identity()
    if accounting["stage1"]["provider_calls_authorized"] is not False:
        raise G7EAuthorizationError(
            "G7D unexpectedly authorizes Provider calls"
        )
    if tuple(accounting["execution_gate"]["required_before_paid_stage1"]) != (
        RESOLVED_G7D_BLOCKER,
    ):
        raise G7EAuthorizationError(
            "G7D paid-execution blocker set changed"
        )
    return accounting


def g7e_authorization_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider Stage-1 authorization contract."""
    accounting = _validate_predecessors()
    specs = stage1_run_specs()
    if len(specs) != 12:
        raise G7EAuthorizationError("Stage-1 run roster changed")

    return {
        "schema_version": G7E_SCHEMA_VERSION,
        "revision": G7E_REVISION,
        "repository_anchor": G7E_REPOSITORY_ANCHOR,
        "predecessor": {
            "g7c_integration_sha256": EXPECTED_G7C_INTEGRATION_SHA256,
            "g7d_accounting_sha256": EXPECTED_G7D_ACCOUNTING_SHA256,
            "pricing_version": accounting["pricing_version"],
        },
        "authorization_scope": {
            "mode": AUTHORIZATION_MODE,
            "one_run_per_invocation": True,
            "one_target_chapter_per_invocation": True,
            "maximum_concurrent_paid_invocations": 1,
            "target_chapter_must_equal_progress_next_chapter": True,
            "target_chapter_range": [1, STAGE1_LAST_CHAPTER],
            "targeted_real_lifecycle_call": (
                "run_unattended_production(target_chapter=<approved chapter>)"
            ),
            "automatic_advance_to_next_chapter": False,
            "new_authorization_required_for_next_chapter": True,
            "all_twelve_runs_share_same_contract": True,
        },
        "invocation_plan_requirements": {
            "exact_g7e_contract_sha256": True,
            "exact_run_index_and_run_sha256": True,
            "exact_model_id_and_novel_id": True,
            "exact_target_chapter": True,
            "exact_pre_invocation_state_fingerprint_sha256": True,
            "hard_maximum_new_provider_sends_required": True,
            "hard_maximum_credit_equivalent_usd_required": True,
            "explicit_operator_authorization_flag_required": True,
            "deterministic_approval_token_required": True,
            "approval_token_cannot_authorize_another_run_or_chapter": True,
            "provider_credentials_must_not_be_embedded": True,
        },
        "journal_resume_policy": {
            "journal_module": "production_paid_request_journal",
            "absent": "eligible_for_exact_new_journaled_send",
            "planned": "safe_to_resume_exact_request_before_transport",
            "response_recorded": "local_replay_only_no_new_provider_send",
            "started": "ambiguous_remote_outcome_block_automatic_reissue",
            "uncertain": "ambiguous_remote_outcome_block_automatic_reissue",
            "corrupt_or_unknown": "block_paid_execution",
            "request_identity_drift": "block_paid_execution",
            "automatic_retry_after_provider_or_transport_exception": False,
            "hidden_provider_retries": 0,
            "provider_side_exactly_once_claimed": False,
        },
        "terminal_failure_policy": {
            "exception_after_started_becomes_uncertain": True,
            "definitive_or_ambiguous_provider_failure_auto_retry": False,
            "journal_evidence_preserved": True,
            "separate_operator_recovery_authorization_required": True,
            "failed_operation_may_not_be_silently_replaced_by_new_slot": True,
        },
        "resume_definition": {
            "same_external_run_root_required": True,
            "same_target_chapter_required": True,
            "progress_must_still_point_to_target_or_target_already_complete": True,
            "no_corrupt_journal_entries": True,
            "no_started_or_uncertain_journal_entries": True,
            "response_recorded_may_replay_locally": True,
            "planned_may_continue_to_one_exact_send": True,
            "persisted_invocation_budget_ledger_required_for_real_executor": True,
            "budget_must_not_reset_on_process_restart": True,
        },
        "accounting_and_budget": {
            "g7d_ratecard_required": True,
            "accepted_response_usage_required": True,
            "unknown_cache_uses_g7d_conservative_upper_bound": True,
            "new_send_count_persisted": True,
            "credit_equivalent_cost_persisted": True,
            "ceiling_checked_before_every_new_send": True,
            "ceiling_rechecked_after_response_usage": True,
            "missing_usage_blocks_further_paid_work": True,
        },
        "stage1": {
            "run_count": 12,
            "first_chapter": 1,
            "last_chapter": STAGE1_LAST_CHAPTER,
            "generic_paid_execution_contract_frozen": True,
            "provider_calls_authorized_only_by_valid_invocation_plan": True,
            "provider_calls_authorized_without_invocation_plan": False,
            "model_ranking_or_elimination_performed": False,
            "finalist_continuation_authorized": False,
        },
        "resolved_by_g7e": [RESOLVED_G7D_BLOCKER],
        "generic_stage1_blockers_remaining": [],
        "execution_gate": {
            "status": "contract_frozen_invocation_authorization_required",
            "paid_stage1_contract_frozen": True,
            "first_paid_request_may_only_be_made_by_separate_reviewed_executor": True,
            "explicit_invocation_plan_required": True,
            "explicit_operator_authorization_required": True,
        },
        "provider_calls": 0,
    }


G7E_AUTHORIZATION_SHA256 = _sha256_json(g7e_authorization_identity())


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G7EAuthorizationError(f"{label} must be a positive integer")
    return value


def _positive_decimal_string(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise G7EAuthorizationError(f"{label} must be a positive finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise G7EAuthorizationError(
            f"{label} must be a positive finite number"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G7EAuthorizationError(
            f"{label} must be a positive finite number"
        )
    return format(parsed.normalize(), "f")


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7EAuthorizationError(f"{label} must be a lowercase SHA-256")
    return value


def build_invocation_authorization(
    *,
    run_index: int,
    target_chapter: int,
    pre_invocation_state_fingerprint_sha256: str,
    maximum_new_provider_sends: int,
    maximum_credit_equivalent_usd: Any,
) -> dict[str, Any]:
    """Build one deterministic single-run/single-chapter authorization plan."""
    _validate_predecessors()
    run_index = _positive_int(run_index, "run_index")
    target_chapter = _positive_int(target_chapter, "target_chapter")
    if target_chapter > STAGE1_LAST_CHAPTER:
        raise G7EAuthorizationError(
            f"target_chapter must be within Stage-1 1..{STAGE1_LAST_CHAPTER}"
        )
    state_sha = _require_sha256(
        pre_invocation_state_fingerprint_sha256,
        "pre_invocation_state_fingerprint_sha256",
    )
    maximum_new_provider_sends = _positive_int(
        maximum_new_provider_sends,
        "maximum_new_provider_sends",
    )
    max_cost = _positive_decimal_string(
        maximum_credit_equivalent_usd,
        "maximum_credit_equivalent_usd",
    )

    specs = stage1_run_specs()
    if run_index > len(specs):
        raise G7EAuthorizationError(
            f"run_index must be within 1..{len(specs)}"
        )
    spec = specs[run_index - 1]

    plan = {
        "schema_version": INVOCATION_SCHEMA_VERSION,
        "revision": INVOCATION_REVISION,
        "g7e_authorization_sha256": G7E_AUTHORIZATION_SHA256,
        "run_index": run_index,
        "relative_root": spec["relative_root"],
        "run_sha256": spec["run_sha256"],
        "s0_identity_sha256": spec["s0_identity_sha256"],
        "model_id": spec["model_id"],
        "novel_id": spec["novel_id"],
        "target_chapter": target_chapter,
        "pre_invocation_state_fingerprint_sha256": state_sha,
        "maximum_new_provider_sends": maximum_new_provider_sends,
        "maximum_credit_equivalent_usd": max_cost,
        "pricing_policy_sha256": EXPECTED_G7D_ACCOUNTING_SHA256,
        "real_lifecycle_entrypoint": "run_unattended_production",
        "automatic_retry_forbidden": True,
        "automatic_chapter_advance_forbidden": True,
        "operator_authorization_required": True,
    }
    return {
        **plan,
        "approval_token": _sha256_json(plan),
    }


def verify_invocation_authorization(
    plan: Mapping[str, Any],
    *,
    expected_approval_token: str,
    current_state_fingerprint_sha256: str,
    operator_authorized: bool,
) -> dict[str, Any]:
    """Validate a frozen invocation plan before a real executor may send."""
    if not isinstance(plan, Mapping):
        raise G7EAuthorizationError("invocation plan must be a mapping")
    value = copy.deepcopy(dict(plan))
    token = value.pop("approval_token", None)
    if not isinstance(token, str) or not _SHA256_RE.fullmatch(token):
        raise G7EAuthorizationError("invocation approval token is invalid")
    if _sha256_json(value) != token:
        raise G7EAuthorizationError("invocation approval token does not recompute")
    if token != expected_approval_token:
        raise G7EAuthorizationError("invocation approval token mismatch")
    if value.get("g7e_authorization_sha256") != G7E_AUTHORIZATION_SHA256:
        raise G7EAuthorizationError("invocation binds another G7E contract")
    if value.get("pricing_policy_sha256") != EXPECTED_G7D_ACCOUNTING_SHA256:
        raise G7EAuthorizationError("invocation binds another accounting policy")
    current_state = _require_sha256(
        current_state_fingerprint_sha256,
        "current_state_fingerprint_sha256",
    )
    if value.get("pre_invocation_state_fingerprint_sha256") != current_state:
        raise G7EAuthorizationError(
            "invocation state fingerprint changed after authorization"
        )
    if operator_authorized is not True:
        raise G7EAuthorizationError(
            "explicit operator authorization is required for paid execution"
        )

    specs = stage1_run_specs()
    index = _positive_int(value.get("run_index"), "run_index")
    if index > len(specs):
        raise G7EAuthorizationError("invocation run index is out of range")
    spec = specs[index - 1]
    for field in (
        "relative_root",
        "run_sha256",
        "s0_identity_sha256",
        "model_id",
        "novel_id",
    ):
        if value.get(field) != spec[field]:
            raise G7EAuthorizationError(
                f"invocation run identity changed: {field}"
            )

    target = _positive_int(value.get("target_chapter"), "target_chapter")
    if target > STAGE1_LAST_CHAPTER:
        raise G7EAuthorizationError("invocation target exceeds Stage-1")
    _positive_int(
        value.get("maximum_new_provider_sends"),
        "maximum_new_provider_sends",
    )
    _positive_decimal_string(
        value.get("maximum_credit_equivalent_usd"),
        "maximum_credit_equivalent_usd",
    )
    if value.get("automatic_retry_forbidden") is not True:
        raise G7EAuthorizationError("automatic retry prohibition changed")
    if value.get("automatic_chapter_advance_forbidden") is not True:
        raise G7EAuthorizationError("automatic chapter-advance prohibition changed")
    if value.get("operator_authorization_required") is not True:
        raise G7EAuthorizationError("operator authorization requirement changed")

    return {
        "authorized": True,
        "approval_token": token,
        "run_index": index,
        "relative_root": spec["relative_root"],
        "model_id": spec["model_id"],
        "novel_id": spec["novel_id"],
        "target_chapter": target,
        "maximum_new_provider_sends": value["maximum_new_provider_sends"],
        "maximum_credit_equivalent_usd": value[
            "maximum_credit_equivalent_usd"
        ],
    }


def classify_journal_resume_state(
    inspection: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify production journal state for automatic Stage-1 continuation."""
    if not isinstance(inspection, Mapping):
        raise G7EAuthorizationError("journal inspection must be a mapping")
    errors = inspection.get("errors")
    uncertain = inspection.get("uncertain")
    records = inspection.get("records")
    if not isinstance(errors, list):
        raise G7EAuthorizationError("journal inspection errors must be a list")
    if not isinstance(uncertain, list):
        raise G7EAuthorizationError("journal inspection uncertain must be a list")
    if not isinstance(records, list):
        raise G7EAuthorizationError("journal inspection records must be a list")

    if errors:
        return {
            "status": "blocked_corrupt_or_unknown_journal",
            "automatic_paid_continuation_allowed": False,
            "new_provider_send_allowed": False,
            "local_replay_only_count": 0,
            "planned_send_count": 0,
            "blocking_records": copy.deepcopy(errors),
        }

    statuses = []
    for record in records:
        if not isinstance(record, Mapping):
            raise G7EAuthorizationError("journal record summary must be a mapping")
        status = record.get("status")
        if status not in _ALLOWED_JOURNAL_STATUSES:
            return {
                "status": "blocked_corrupt_or_unknown_journal",
                "automatic_paid_continuation_allowed": False,
                "new_provider_send_allowed": False,
                "local_replay_only_count": 0,
                "planned_send_count": 0,
                "blocking_records": [copy.deepcopy(dict(record))],
            }
        statuses.append(status)

    blocking = [
        copy.deepcopy(dict(record))
        for record in records
        if record.get("status") in {"started", "uncertain"}
    ]
    if uncertain:
        blocking.extend(copy.deepcopy(uncertain))
    if blocking:
        return {
            "status": "blocked_ambiguous_remote_outcome",
            "automatic_paid_continuation_allowed": False,
            "new_provider_send_allowed": False,
            "local_replay_only_count": sum(
                status == "response_recorded" for status in statuses
            ),
            "planned_send_count": sum(status == "planned" for status in statuses),
            "blocking_records": blocking,
        }

    return {
        "status": "resumable",
        "automatic_paid_continuation_allowed": True,
        "new_provider_send_allowed": True,
        "local_replay_only_count": sum(
            status == "response_recorded" for status in statuses
        ),
        "planned_send_count": sum(status == "planned" for status in statuses),
        "blocking_records": [],
    }


def classify_chapter_resume_state(
    *,
    progress: Mapping[str, Any],
    target_chapter: int,
    journal_inspection: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine chronological progress and journal safety for one target chapter."""
    if not isinstance(progress, Mapping):
        raise G7EAuthorizationError("progress must be a mapping")
    target = _positive_int(target_chapter, "target_chapter")
    if target > STAGE1_LAST_CHAPTER:
        raise G7EAuthorizationError("target chapter exceeds Stage-1")

    last_completed = progress.get("last_completed")
    next_chapter = progress.get("next_chapter")
    if (
        isinstance(last_completed, bool)
        or not isinstance(last_completed, int)
        or last_completed < 0
    ):
        raise G7EAuthorizationError("progress last_completed is invalid")
    if (
        isinstance(next_chapter, bool)
        or not isinstance(next_chapter, int)
        or next_chapter < 1
    ):
        raise G7EAuthorizationError("progress next_chapter is invalid")

    journals = classify_journal_resume_state(journal_inspection)

    if last_completed >= target:
        return {
            "status": "target_already_complete",
            "paid_send_needed": False,
            "automatic_paid_continuation_allowed": False,
            "target_chapter": target,
            "journal_state": journals,
        }

    if last_completed != target - 1 or next_chapter != target:
        return {
            "status": "blocked_chronological_state_mismatch",
            "paid_send_needed": False,
            "automatic_paid_continuation_allowed": False,
            "target_chapter": target,
            "journal_state": journals,
        }

    if not journals["automatic_paid_continuation_allowed"]:
        return {
            "status": journals["status"],
            "paid_send_needed": False,
            "automatic_paid_continuation_allowed": False,
            "target_chapter": target,
            "journal_state": journals,
        }

    return {
        "status": "ready_or_resumable",
        "paid_send_needed": True,
        "automatic_paid_continuation_allowed": True,
        "target_chapter": target,
        "journal_state": journals,
    }


def assert_contract_ready_for_invocation_planning() -> None:
    """Assert all generic pre-execution blockers are resolved."""
    identity = g7e_authorization_identity()
    if identity["generic_stage1_blockers_remaining"]:
        raise G7EAuthorizationError("generic Stage-1 blockers remain")
    if identity["provider_calls"] != 0:
        raise G7EAuthorizationError("G7E contract freeze must make zero calls")
    if not identity["stage1"]["generic_paid_execution_contract_frozen"]:
        raise G7EAuthorizationError("G7E contract is not frozen")
    if identity["stage1"]["provider_calls_authorized_without_invocation_plan"]:
        raise G7EAuthorizationError(
            "G7E must never authorize paid calls without an invocation plan"
        )


__all__ = [
    "AUTHORIZATION_MODE",
    "G7E_AUTHORIZATION_SHA256",
    "G7E_REVISION",
    "assert_contract_ready_for_invocation_planning",
    "build_invocation_authorization",
    "classify_chapter_resume_state",
    "classify_journal_resume_state",
    "g7e_authorization_identity",
    "verify_invocation_authorization",
]
