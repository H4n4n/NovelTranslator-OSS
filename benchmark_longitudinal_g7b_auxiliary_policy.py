"""G7B zero-provider auxiliary reasoning-policy freeze.

G7A froze the Stage-1 roster, source identities, independent S0 run identities,
and the G6K-selected PRIMARY TRANSLATION efforts, but deliberately blocked paid
Stage-1 because five provider-assisted lifecycle operations still lacked an
explicit reasoning policy.

G7B resolves only that policy gap. It does NOT authorize Chapter 1, construct a
Provider client, read credentials, touch source files, or create run roots.

Policy rationale:
- structured bookkeeping operations (state extraction and state-JSON repair)
  use the lowest exact live-verified effort for adjustable models;
- semantic terminology operations (glossary review, periodic audit, terminology
  repair) reuse that model's already frozen G6K-selected translation effort;
- Tencent Hy3 Paid and MiMo V2.5 Pro remain Provider-native/default-only for
  every operation because G5C verified default requests and no reasoning effort.

This is an explicit preregistered benchmark policy, not an empirical claim that
auxiliary-operation effort is independently optimal.

Importing this module performs no I/O, credential access, network activity, or
filesystem writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from benchmark_longitudinal_capability_results import live_provider_capability
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
from benchmark_longitudinal_g7a_readiness import (
    G6K_REVEAL_FREEZE_ID,
    G7A_READINESS_SHA256,
    TRANSLATION_EFFORT_BY_MODEL,
    g7a_readiness_identity,
)


G7B_SCHEMA_VERSION = 1
G7B_REVISION = "goat-g7b-auxiliary-reasoning-policy-v1"
G7B_REPOSITORY_ANCHOR = "5c0a504cb23fbbfb65a799a7884af54ab0abde25"

# Exact successful local G7A verification reported by the operator.
G7A_EXPECTED_READINESS_SHA256 = (
    "f43a7f1130b0e20aeca7ba068d278b1c41c2ff064b351eaa816748b99f6cd1e0"
)
G7A_EXPECTED_CONFIGURATION_SHA256 = (
    "e011cb5d65e633ce99dad6388890e5b2bb6e35cf23fbefc28e4d4686b20486c8"
)
G7A_EXPECTED_FULL_PLAN_SHA256 = (
    "b791e655ff80707d7b5cb8b3d8aa8346360a589fac0559ede67046f342dde4d1"
)

STRUCTURED_OPERATIONS = (
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
)

SEMANTIC_OPERATIONS = (
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
)

if set(STRUCTURED_OPERATIONS + SEMANTIC_OPERATIONS) != (
    set(PURE_RUN_LIFECYCLE_OPERATIONS) - {LIFECYCLE_OPERATION_TRANSLATION}
):
    raise RuntimeError("G7B auxiliary operation partition drifted")


class G7BPolicyError(RuntimeError):
    """The G7B auxiliary reasoning policy is malformed or unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7BPolicyError("G7B policy is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class G7BOperationPolicy:
    operation: str
    model_id: str
    effort: str | None
    status: str
    basis: str

    def identity(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "model_id": self.model_id,
            "effort": self.effort,
            "status": self.status,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class G7BModelPolicy:
    model_id: str
    operations: tuple[G7BOperationPolicy, ...]

    def binding_for(self, operation: str) -> G7BOperationPolicy:
        for binding in self.operations:
            if binding.operation == operation:
                return binding
        raise G7BPolicyError(
            f"model {self.model_id!r} lacks lifecycle operation {operation!r}"
        )

    def identity(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "operations": [item.identity() for item in self.operations],
        }


def _validate_predecessor() -> dict[str, Any]:
    if G7A_READINESS_SHA256 != G7A_EXPECTED_READINESS_SHA256:
        raise G7BPolicyError(
            "G7B is bound to a different G7A readiness identity"
        )
    g7a = g7a_readiness_identity()
    if g7a["configuration_sha256"] != G7A_EXPECTED_CONFIGURATION_SHA256:
        raise G7BPolicyError("G7A configuration SHA changed")
    if g7a["full_plan_sha256"] != G7A_EXPECTED_FULL_PLAN_SHA256:
        raise G7BPolicyError("G7A full-plan SHA changed")
    if g7a["execution_gate"]["status"] != "blocked":
        raise G7BPolicyError("G7A paid Stage-1 gate must still be blocked")
    if g7a["stage1"]["provider_calls_authorized"] is not False:
        raise G7BPolicyError("G7A unexpectedly authorized Provider calls")
    return g7a


def _default_only(model_id: str) -> bool:
    live = live_provider_capability(model_id)
    return not bool(live.verified_reasoning_efforts)


def _structured_effort(model_id: str) -> str | None:
    """Use lowest live-verified effort, or verified Provider default."""
    live = live_provider_capability(model_id)
    if live.verified_reasoning_efforts:
        if live.verified_reasoning_efforts[0] != "low":
            raise G7BPolicyError(
                f"{model_id!r} lowest frozen verified effort is not 'low'"
            )
        return "low"
    if not live.default_effort_request_verified:
        raise G7BPolicyError(
            f"default-only model {model_id!r} lacks verified default request"
        )
    return None


def _semantic_effort(model_id: str) -> str | None:
    """Reuse the G6K-selected translation effort for semantic auxiliary work."""
    live = live_provider_capability(model_id)
    effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
    if effort is None:
        if live.verified_reasoning_efforts:
            raise G7BPolicyError(
                f"adjustable model {model_id!r} cannot use implicit semantic default"
            )
        if not live.default_effort_request_verified:
            raise G7BPolicyError(
                f"default-only model {model_id!r} lacks verified default request"
            )
        return None
    if effort not in live.verified_reasoning_efforts:
        raise G7BPolicyError(
            f"semantic effort {effort!r} is not live-verified for {model_id!r}"
        )
    return effort


def build_g7b_model_policy(model_id: str) -> G7BModelPolicy:
    if model_id not in BENCHMARK_MODEL_IDS:
        raise G7BPolicyError(f"unknown benchmark model {model_id!r}")

    default_only = _default_only(model_id)
    translation_effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
    semantic_effort = _semantic_effort(model_id)
    structured_effort = _structured_effort(model_id)

    live = live_provider_capability(model_id)
    if translation_effort is None:
        if not default_only or not live.default_effort_request_verified:
            raise G7BPolicyError(
                f"translation default is not authorized for {model_id!r}"
            )
    elif translation_effort not in live.verified_reasoning_efforts:
        raise G7BPolicyError(
            f"translation effort {translation_effort!r} is not live-verified"
        )

    operations: list[G7BOperationPolicy] = []
    for operation in PURE_RUN_LIFECYCLE_OPERATIONS:
        if operation == LIFECYCLE_OPERATION_TRANSLATION:
            effort = translation_effort
            status = "frozen_from_g6k"
            basis = "g6k_primary_translation_selection"
        elif operation in STRUCTURED_OPERATIONS:
            effort = structured_effort
            status = "frozen_g7b"
            basis = (
                "g5c_verified_provider_default_for_structured_operation"
                if default_only
                else "g5c_lowest_verified_effort_for_structured_operation"
            )
        elif operation in SEMANTIC_OPERATIONS:
            effort = semantic_effort
            status = "frozen_g7b"
            basis = (
                "g5c_verified_provider_default_for_semantic_operation"
                if default_only
                else "reuse_g6k_selected_translation_effort_for_semantic_operation"
            )
        else:  # pragma: no cover - guarded by partition assertion above
            raise G7BPolicyError(f"unclassified lifecycle operation {operation!r}")

        operations.append(
            G7BOperationPolicy(
                operation=operation,
                model_id=model_id,
                effort=effort,
                status=status,
                basis=basis,
            )
        )

    policy = G7BModelPolicy(model_id=model_id, operations=tuple(operations))
    if tuple(item.operation for item in policy.operations) != PURE_RUN_LIFECYCLE_OPERATIONS:
        raise G7BPolicyError("G7B must preserve production lifecycle ordering")
    return policy


def build_g7b_model_policies() -> tuple[G7BModelPolicy, ...]:
    policies = tuple(build_g7b_model_policy(model_id) for model_id in BENCHMARK_MODEL_IDS)
    if tuple(policy.model_id for policy in policies) != BENCHMARK_MODEL_IDS:
        raise G7BPolicyError("G7B model roster/order changed")
    return policies


def reasoning_by_operation(model_id: str) -> MappingProxyType:
    policy = build_g7b_model_policy(model_id)
    return MappingProxyType(
        {binding.operation: binding.effort for binding in policy.operations}
    )


def g7b_policy_identity() -> dict[str, Any]:
    g7a = _validate_predecessor()
    policies = build_g7b_model_policies()

    unresolved = [
        binding.identity()
        for policy in policies
        for binding in policy.operations
        if binding.status.startswith("unresolved")
    ]
    if unresolved:
        raise G7BPolicyError("G7B unexpectedly left reasoning operations unresolved")

    return {
        "schema_version": G7B_SCHEMA_VERSION,
        "revision": G7B_REVISION,
        "repository_anchor": G7B_REPOSITORY_ANCHOR,
        "predecessor": {
            "g6k_reveal_freeze_id": G6K_REVEAL_FREEZE_ID,
            "g7a_readiness_sha256": G7A_EXPECTED_READINESS_SHA256,
            "g7a_configuration_sha256": G7A_EXPECTED_CONFIGURATION_SHA256,
            "g7a_full_plan_sha256": G7A_EXPECTED_FULL_PLAN_SHA256,
        },
        "benchmark_id": g7a["benchmark_id"],
        "policy_principles": {
            "same_model_all_provider_operations": True,
            "explicit_per_operation_reasoning": True,
            "no_silent_fallback": True,
            "auxiliary_effort_is_policy_not_independent_quality_optimum": True,
            "structured_operations_use_lowest_verified_effort": True,
            "semantic_operations_reuse_g6k_translation_selection": True,
            "default_only_models_remain_provider_native_default": True,
        },
        "structured_operations": list(STRUCTURED_OPERATIONS),
        "semantic_operations": list(SEMANTIC_OPERATIONS),
        "model_policies": [policy.identity() for policy in policies],
        "reasoning_policy_complete": True,
        "stage1": {
            "run_count": g7a["stage1"]["run_count"],
            "chapters": [
                g7a["stage1"]["first_chapter"],
                g7a["stage1"]["last_chapter"],
            ],
            "audit_checkpoints": g7a["stage1"]["audit_checkpoints"],
            "all_six_models_start_stage1": True,
            "provider_calls_authorized": False,
            "model_ranking_or_elimination_performed": False,
            "finalist_continuation_authorized": False,
        },
        "execution_gate": {
            "status": "blocked",
            "reason": "stage1_execution_integration_not_proven",
            "resolved_by_g7b": [
                "freeze_state_extraction_reasoning",
                "freeze_state_json_repair_reasoning",
                "freeze_glossary_review_reasoning",
                "freeze_periodic_audit_reasoning",
                "freeze_terminology_repair_reasoning",
            ],
            "required_before_paid_stage1": [
                "bind_stage1_execution_to_real_production_lifecycle",
                "prove_external_isolated_s0_roots_and_production_guard",
                "prove_durable_paid_request_journal_for_every_provider_assisted_operation",
            ],
        },
        "provider_calls": 0,
    }


G7B_POLICY_SHA256 = _sha256_json(g7b_policy_identity())


def assert_paid_stage1_blocked() -> None:
    identity = g7b_policy_identity()
    if identity["execution_gate"]["status"] != "blocked":
        raise G7BPolicyError("G7B must keep paid Stage-1 execution blocked")
    if identity["stage1"]["provider_calls_authorized"] is not False:
        raise G7BPolicyError("G7B must authorize zero Provider calls")
    if identity["provider_calls"] != 0:
        raise G7BPolicyError("G7B must report zero Provider calls")


__all__ = [
    "G7A_EXPECTED_CONFIGURATION_SHA256",
    "G7A_EXPECTED_FULL_PLAN_SHA256",
    "G7A_EXPECTED_READINESS_SHA256",
    "G7B_POLICY_SHA256",
    "G7B_REVISION",
    "SEMANTIC_OPERATIONS",
    "STRUCTURED_OPERATIONS",
    "assert_paid_stage1_blocked",
    "build_g7b_model_policies",
    "g7b_policy_identity",
    "reasoning_by_operation",
]
