"""G7A zero-provider Stage-1 readiness freeze for the GOAT benchmark.

G6K revealed and froze the translation-effort choice for each adjustable model.
G7A does NOT begin the longitudinal experiment. It converts that completed
calibration into a deterministic Stage-1 readiness contract while keeping every
paid/model call blocked.

The freeze binds:
- the exact G6K reveal evidence and selected translation efforts;
- all six benchmark models and both benchmark novels;
- twelve independent model×novel S0 runs;
- chronological Stage-1 Chapters 1..30;
- the Chapter-25 audit checkpoint;
- exact source and chapter-library identities already frozen by G6 evidence;
- same-model ownership for every provider-assisted lifecycle operation.

Critically, G6 calibrated PRIMARY TRANSLATION only. Reasoning for state
extraction, state-JSON repair, glossary review, periodic audit, and terminology
repair remains unresolved here. G7A therefore explicitly blocks Stage-1 paid
execution. A later zero-provider policy phase must freeze those operation
bindings before any Chapter-1 Provider request is authorized.

Importing this module performs no I/O, credential access, network activity, or
filesystem writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

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
    DEFAULT_BENCHMARK_CONFIGURATION,
)
from benchmark_longitudinal_g6_calibration import (
    ACE_CHAPTER_LIBRARY_SHA256,
    ACE_SOURCE_SHA256,
    ROGUE_CHAPTER_LIBRARY_SHA256,
    ROGUE_SOURCE_SHA256,
)
from benchmark_longitudinal_plan import (
    SourceIdentityDescriptor,
    build_benchmark_plan,
)


G7A_SCHEMA_VERSION = 1
G7A_REVISION = "goat-g7a-stage1-readiness-v1"
G7A_REPOSITORY_ANCHOR = "b9ae49f038087deb26e31f39623cf46a3d99c438"

# Authoritative G6K reveal freeze reported after G6J-bound reconciliation.
G6K_REVEAL_FREEZE_ID = (
    "a535595d373cfa0e92956c84142ef9877f09084425e644f651d1133b5ae1a7de"
)
G6K_REVEALED_JSON_SHA256 = (
    "c04ae3834048f6d7f1946d806e167c77cab238116e2e06df113512602a9e51f3"
)
G6K_MANIFEST_SHA256 = (
    "d6c27d8a0e851c9c832daaa7a6f0d459456603d8bc9af6f6fe19d745a17c37ae"
)
G6K_SUMMARY_SHA256 = (
    "4486d789795f11bcafa60ddcb6ff6b51d55559a83340460855840d47c229619d"
)
G6K_TREE_SHA256 = (
    "298c0034c7c40e6425dbc62e6e37ffff80a9f8391504fcc7108a0e806b1da1b9"
)
G6K_TREE_FILE_COUNT = 3

# Frozen predecessor evidence retained for lineage.
G6J_SCORE_FREEZE_ID = (
    "a97aa9f854de6472daaa23719523b43a88d8275182fcc553b04d12e730d6e7b6"
)
G6J_FROZEN_JSON_SHA256 = (
    "dda073a832c497db33782bedd6c145cfc96cc2230cd59cca6edd834803fd1d87"
)
G6I_PRIVATE_KEY_COMMITMENT_SHA256 = (
    "13a93e86ba358f19ace969150affa4524eddec29657f436c9bc7986f7b38a19d"
)

STAGE1_FIRST_CHAPTER = 1
STAGE1_LAST_CHAPTER = 30
STAGE1_AUDIT_CHECKPOINTS = (25,)
STAGE1_RUN_COUNT = 12

# Production source identities already established during G6 source binding.
ACE_FINAL_CHAPTER = 1214
ROGUE_FINAL_CHAPTER = 350

SOURCE_BINDINGS = MappingProxyType(
    {
        "ace_evolution": {
            "source_language": "zh",
            "target_language": "en",
            "chapter_count": ACE_FINAL_CHAPTER,
            "source_sha256": ACE_SOURCE_SHA256,
            "chapter_library_sha256": ACE_CHAPTER_LIBRARY_SHA256,
            "profile": "infinite_flow",
            "audit_interval": 25,
            "chapter_numbering": "spine_sequential",
        },
        "rogue_first_prince": {
            "source_language": "ko",
            "target_language": "en",
            "chapter_count": ROGUE_FINAL_CHAPTER,
            "source_sha256": ROGUE_SOURCE_SHA256,
            "chapter_library_sha256": ROGUE_CHAPTER_LIBRARY_SHA256,
            "profile": "korean_fantasy",
            "audit_interval": 25,
            "chapter_numbering": "spine_sequential",
        },
    }
)

# Exact G6K effort selections. None means a Provider-native/default-only model.
TRANSLATION_EFFORT_BY_MODEL = MappingProxyType(
    {
        "deepseek/deepseek-v4.1-flash": "low",
        "z-ai/glm-5.3-flash": "high",
        "qwen/qwen3.8-max-0902": "medium",
        "google/gemini-3.8-flash": "low",
        "tencent/hy3-paid": None,
        "xiaomi/mimo-v2.5-pro": None,
    }
)

EXPECTED_AUXILIARY_OPERATION_SET = frozenset(
    {
        LIFECYCLE_OPERATION_STATE_EXTRACTION,
        LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
        LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
        LIFECYCLE_OPERATION_PERIODIC_AUDIT,
        LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    }
)

AUXILIARY_OPERATIONS = tuple(
    operation
    for operation in PURE_RUN_LIFECYCLE_OPERATIONS
    if operation != LIFECYCLE_OPERATION_TRANSLATION
)

if frozenset(AUXILIARY_OPERATIONS) != EXPECTED_AUXILIARY_OPERATION_SET:
    raise RuntimeError("G7A auxiliary-operation set drifted from production lifecycle")


class G7AReadinessError(RuntimeError):
    """The revealed calibration cannot safely seed Stage-1 planning."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7AReadinessError(
            "G7A identity is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Stage1OperationBinding:
    operation: str
    model_id: str
    effort: str | None
    status: str

    def identity(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "model_id": self.model_id,
            "effort": self.effort,
            "status": self.status,
        }


@dataclass(frozen=True)
class Stage1ModelPolicy:
    model_id: str
    operations: tuple[Stage1OperationBinding, ...]

    def identity(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "operations": [item.identity() for item in self.operations],
        }

    def binding_for(self, operation: str) -> Stage1OperationBinding:
        for binding in self.operations:
            if binding.operation == operation:
                return binding
        raise G7AReadinessError(
            f"model {self.model_id!r} lacks operation {operation!r}"
        )


def _validate_translation_effort(model_id: str, effort: str | None) -> None:
    live = live_provider_capability(model_id)
    if effort is None:
        if live.verified_reasoning_efforts:
            raise G7AReadinessError(
                f"adjustable model {model_id!r} cannot use default translation effort"
            )
        if not live.default_effort_request_verified:
            raise G7AReadinessError(
                f"default translation request was not live-verified for {model_id!r}"
            )
        return

    if effort not in live.verified_reasoning_efforts:
        raise G7AReadinessError(
            f"G6K selected unverified translation effort {effort!r} for {model_id!r}"
        )


def build_stage1_model_policy(model_id: str) -> Stage1ModelPolicy:
    try:
        translation_effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
    except KeyError as exc:
        raise G7AReadinessError(f"unknown benchmark model {model_id!r}") from exc

    _validate_translation_effort(model_id, translation_effort)

    operations: list[Stage1OperationBinding] = []
    for operation in PURE_RUN_LIFECYCLE_OPERATIONS:
        if operation == LIFECYCLE_OPERATION_TRANSLATION:
            operations.append(
                Stage1OperationBinding(
                    operation=operation,
                    model_id=model_id,
                    effort=translation_effort,
                    status="frozen_from_g6k",
                )
            )
        else:
            # Deliberately no guessed reasoning level.
            operations.append(
                Stage1OperationBinding(
                    operation=operation,
                    model_id=model_id,
                    effort=None,
                    status="unresolved_blocks_paid_stage1",
                )
            )

    return Stage1ModelPolicy(model_id=model_id, operations=tuple(operations))


def build_stage1_model_policies() -> tuple[Stage1ModelPolicy, ...]:
    policies = tuple(build_stage1_model_policy(model_id) for model_id in BENCHMARK_MODEL_IDS)
    if tuple(item.model_id for item in policies) != BENCHMARK_MODEL_IDS:
        raise G7AReadinessError("Stage-1 model-policy roster changed")
    return policies


def authoritative_source_descriptors() -> tuple[SourceIdentityDescriptor, ...]:
    descriptors: list[SourceIdentityDescriptor] = []
    for novel in DEFAULT_BENCHMARK_CONFIGURATION.novels:
        try:
            binding = SOURCE_BINDINGS[novel.novel_id]
        except KeyError as exc:
            raise G7AReadinessError(
                f"missing authoritative source binding for {novel.novel_id!r}"
            ) from exc
        if binding["source_language"] != novel.source_language:
            raise G7AReadinessError(
                f"source-language binding changed for {novel.novel_id!r}"
            )
        if binding["target_language"] != novel.target_language:
            raise G7AReadinessError(
                f"target-language binding changed for {novel.novel_id!r}"
            )
        descriptors.append(
            SourceIdentityDescriptor(
                novel_id=novel.novel_id,
                source_language=novel.source_language,
                target_language=novel.target_language,
                chapter_count=int(binding["chapter_count"]),
                source_sha256=str(binding["source_sha256"]),
                label="authoritative_g6_bound_source",
            )
        )
    return tuple(descriptors)


def _stage1_run_identity(run) -> dict[str, Any]:
    if run.stage1_chapters != tuple(range(STAGE1_FIRST_CHAPTER, STAGE1_LAST_CHAPTER + 1)):
        raise G7AReadinessError(
            f"Stage-1 chapter range changed for {(run.model_id, run.novel_id)!r}"
        )
    if run.stage1_audit_checkpoints != STAGE1_AUDIT_CHECKPOINTS:
        raise G7AReadinessError(
            f"Stage-1 audit checkpoints changed for {(run.model_id, run.novel_id)!r}"
        )
    if not (
        run.requires_s0_state
        and run.causal_sequential
        and run.same_model_pure_run_lifecycle
        and run.independent_model_state
    ):
        raise G7AReadinessError(
            f"Stage-1 run invariants changed for {(run.model_id, run.novel_id)!r}"
        )

    source = SOURCE_BINDINGS[run.novel_id]
    if run.source_identity.source_sha256 != source["source_sha256"]:
        raise G7AReadinessError(
            f"source SHA changed for {(run.model_id, run.novel_id)!r}"
        )
    if run.s0_state.glossary_entries != 0:
        raise G7AReadinessError("Stage-1 S0 glossary must start empty")
    if run.s0_state.continuity != "":
        raise G7AReadinessError("Stage-1 S0 continuity must start empty")
    if run.s0_state.issue_count != 0:
        raise G7AReadinessError("Stage-1 S0 issues must start empty")
    if run.s0_state.last_completed_chapter != 0:
        raise G7AReadinessError("Stage-1 S0 cannot contain completed chapters")
    if run.s0_state.last_audited_chapter != 0:
        raise G7AReadinessError("Stage-1 S0 cannot contain an audit")

    return {
        "model_id": run.model_id,
        "novel_id": run.novel_id,
        "run_sha256": run.run_identity.run_sha256,
        "run_plan_sha256": run.run_plan_sha256,
        "s0_identity_sha256": run.s0_state.identity_sha256,
        "source_sha256": run.source_identity.source_sha256,
        "chapter_library_sha256": source["chapter_library_sha256"],
        "profile": source["profile"],
        "audit_interval": source["audit_interval"],
        "chapter_numbering": source["chapter_numbering"],
        "stage1_chapters": list(run.stage1_chapters),
        "stage1_audit_checkpoints": list(run.stage1_audit_checkpoints),
        "causal_sequential": run.causal_sequential,
        "same_model_pure_run_lifecycle": run.same_model_pure_run_lifecycle,
        "independent_model_state": run.independent_model_state,
    }


def g7a_readiness_identity() -> dict[str, Any]:
    if tuple(TRANSLATION_EFFORT_BY_MODEL) != BENCHMARK_MODEL_IDS:
        raise G7AReadinessError("G6K effort-selection roster/order changed")

    plan = build_benchmark_plan(
        DEFAULT_BENCHMARK_CONFIGURATION,
        authoritative_source_descriptors(),
    )
    if plan.run_count != STAGE1_RUN_COUNT:
        raise G7AReadinessError(
            f"expected {STAGE1_RUN_COUNT} model×novel runs, found {plan.run_count}"
        )

    policies = build_stage1_model_policies()
    unresolved = {
        policy.model_id: [
            binding.operation
            for binding in policy.operations
            if binding.status == "unresolved_blocks_paid_stage1"
        ]
        for policy in policies
    }
    if any(tuple(items) != AUXILIARY_OPERATIONS for items in unresolved.values()):
        raise G7AReadinessError("auxiliary reasoning blocker matrix changed")

    return {
        "schema_version": G7A_SCHEMA_VERSION,
        "revision": G7A_REVISION,
        "repository_anchor": G7A_REPOSITORY_ANCHOR,
        "predecessor_evidence": {
            "g6i_private_key_commitment_sha256": G6I_PRIVATE_KEY_COMMITMENT_SHA256,
            "g6j_score_freeze_id": G6J_SCORE_FREEZE_ID,
            "g6j_frozen_json_sha256": G6J_FROZEN_JSON_SHA256,
            "g6k_reveal_freeze_id": G6K_REVEAL_FREEZE_ID,
            "g6k_revealed_json_sha256": G6K_REVEALED_JSON_SHA256,
            "g6k_manifest_sha256": G6K_MANIFEST_SHA256,
            "g6k_summary_sha256": G6K_SUMMARY_SHA256,
            "g6k_tree_sha256": G6K_TREE_SHA256,
            "g6k_tree_file_count": G6K_TREE_FILE_COUNT,
        },
        "benchmark_id": DEFAULT_BENCHMARK_CONFIGURATION.benchmark_id,
        "configuration_sha256": DEFAULT_BENCHMARK_CONFIGURATION.identity_sha256,
        "full_plan_sha256": plan.plan_sha256,
        "stage1": {
            "first_chapter": STAGE1_FIRST_CHAPTER,
            "last_chapter": STAGE1_LAST_CHAPTER,
            "audit_checkpoints": list(STAGE1_AUDIT_CHECKPOINTS),
            "run_count": plan.run_count,
            "all_six_models_start_stage1": True,
            "model_ranking_or_elimination_performed": False,
            "finalist_continuation_authorized": False,
            "provider_calls_authorized": False,
        },
        "source_bindings": {
            novel_id: dict(binding)
            for novel_id, binding in SOURCE_BINDINGS.items()
        },
        "translation_effort_by_model": dict(TRANSLATION_EFFORT_BY_MODEL),
        "model_policies": [policy.identity() for policy in policies],
        "unresolved_auxiliary_reasoning": unresolved,
        "runs": [_stage1_run_identity(run) for run in plan.runs],
        "execution_gate": {
            "status": "blocked",
            "reason": "auxiliary_lifecycle_reasoning_not_frozen",
            "required_before_paid_stage1": [
                "freeze_state_extraction_reasoning",
                "freeze_state_json_repair_reasoning",
                "freeze_glossary_review_reasoning",
                "freeze_periodic_audit_reasoning",
                "freeze_terminology_repair_reasoning",
                "bind_stage1_execution_to_real_production_lifecycle",
                "prove_external_isolated_s0_roots_and_production_guard",
                "prove_durable_paid_request_journal_for_every_provider_assisted_operation",
            ],
        },
    }


G7A_READINESS_SHA256 = _sha256_json(g7a_readiness_identity())


def assert_paid_stage1_blocked() -> None:
    identity = g7a_readiness_identity()
    gate = identity["execution_gate"]
    if gate["status"] != "blocked":
        raise G7AReadinessError("G7A must keep paid Stage-1 execution blocked")
    if identity["stage1"]["provider_calls_authorized"] is not False:
        raise G7AReadinessError("G7A must authorize zero Provider calls")


__all__ = [
    "AUXILIARY_OPERATIONS",
    "G6K_MANIFEST_SHA256",
    "G6K_REVEAL_FREEZE_ID",
    "G6K_REVEALED_JSON_SHA256",
    "G6K_SUMMARY_SHA256",
    "G6K_TREE_SHA256",
    "G7A_READINESS_SHA256",
    "G7A_REVISION",
    "SOURCE_BINDINGS",
    "STAGE1_AUDIT_CHECKPOINTS",
    "STAGE1_LAST_CHAPTER",
    "STAGE1_RUN_COUNT",
    "TRANSLATION_EFFORT_BY_MODEL",
    "assert_paid_stage1_blocked",
    "authoritative_source_descriptors",
    "build_stage1_model_policies",
    "g7a_readiness_identity",
]
