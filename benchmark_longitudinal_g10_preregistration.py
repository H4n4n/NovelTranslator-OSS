"""G10 Qwen-vs-GLM head-to-head preregistration.

This module freezes the zero-provider experimental design for the G10 comparison
before any G10 paid request exists.  It intentionally performs no filesystem,
credential, network, or provider activity on import.

G10 has two independent arms:
- G10A: matched, state-free literary translation quality.
- G10B: the real sequential NovelTranslator lifecycle through Chapter 50,
  including the production 25-chapter periodic audits and every production-safe
  retroactive correction those audits apply.

Paid execution remains blocked until a later, separately frozen authorization
contract proves the executors, budget ceiling, fresh roots, recovery behavior,
blind tooling, and exact accounting.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


G10_SCHEMA_VERSION = 1
G10_REVISION = "g10-qwen-glm-headtohead-preregistration-v1"
G10_BENCHMARK_ID = "g10-qwen-glm-headtohead-v1"
G10_REPOSITORY_ANCHOR = "e6a332924a5866be0fb96543ca4a3338d52ebd1c"

QWEN_MODEL_ID = "qwen/qwen3.8-max-0902"
GLM_MODEL_ID = "z-ai/glm-5.3-flash"
MODEL_ORDER = (QWEN_MODEL_ID, GLM_MODEL_ID)

MODEL_POLICY = {
    QWEN_MODEL_ID: {
        "primary_translation_effort": "medium",
        "structured_auxiliary_effort": "low",
        "semantic_auxiliary_effort": "medium",
    },
    GLM_MODEL_ID: {
        "primary_translation_effort": "high",
        "structured_auxiliary_effort": "low",
        "semantic_auxiliary_effort": "high",
    },
}

SOURCE_BINDINGS = {
    "production_engine": {
        "path": "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py",
        "blob_sha": "b4657d6f2ccf212cb7041c960e7e940c84b50544",
    },
    "production_lifecycle": {
        "path": "production_lifecycle.py",
        "blob_sha": "253c2f4c829aad97656a15b0433e12ad76be6ae8",
    },
    "g7b_reasoning_policy": {
        "path": "benchmark_longitudinal_g7b_auxiliary_policy.py",
        "blob_sha": "7c21581d70d297f0823e24e81c332e899b0ecb1a",
    },
    "g7d_accounting_policy": {
        "path": "benchmark_longitudinal_g7d_accounting_policy.py",
        "blob_sha": "3a329a2152af3eccdf4d2f6314081c2412f4c0af",
    },
    "audit_recovery_reference": {
        "path": "full_pipeline_periodic_audit_recovery.py",
        "blob_sha": "3b72ec90e893700df086f71466fc9e5c0503324b",
    },
    "g6_blind_protocol": {
        "path": "benchmark_longitudinal_g6_blind.py",
        "blob_sha": "c8cc9f783b1c066e6194debb4b70515e999ed004",
    },
    "g6_blind_bundle": {
        "path": "benchmark_longitudinal_g6_blind_bundle.py",
        "blob_sha": "984eaea35954bc6ce39b7c40ca6b24880101ea06",
    },
    "g6_blind_scoring": {
        "path": "benchmark_longitudinal_g6_scoring.py",
        "blob_sha": "a74b724f4a56bae8759fdd1219805fb3e86eb850",
    },
    "g6_blind_reveal": {
        "path": "benchmark_longitudinal_g6_reveal.py",
        "blob_sha": "afa7ed48f508f7917b490149c24070f262d2bc1f",
    },
}

NOVELS = {
    "ace_evolution": {
        "title": "Ace Evolution",
        "source_language": "zh",
        "target_language": "en",
        "profile": "infinite_flow",
        "final_chapter": 1214,
        "audit_interval": 25,
        "chapter_numbering": "spine_sequential",
        # GitHub object provenance at the reviewed repository anchor.
        "config_blob_sha": "51346b04b87482de3ee7e27a656839eec0968313",
        # Line-ending-invariant identity of the complete parsed novel.json object.
        "config_canonical_sha256": "82e7631ce2dc6d8c99419bfc047d0788cc504815a3a5112828f32abe665b4cd5",
    },
    "rogue_first_prince": {
        "title": "I Became the Rogue First Prince",
        "source_language": "ko",
        "target_language": "en",
        "profile": "korean_fantasy",
        "final_chapter": 350,
        "audit_interval": 25,
        "chapter_numbering": "spine_sequential",
        # GitHub object provenance at the reviewed repository anchor.
        "config_blob_sha": "eda25e6d2fa822cd98946010fed1740d72b408df",
        # Includes source_type and residual_cjk_micro_repair from full novel.json.
        "config_canonical_sha256": "9bc6a69b32ca5ae4984e6e0cdd80bee6ed476a6f0376b3306c8e7b3d79b6b2a1",
    },
}

PRIMARY_LITERARY_CHAPTERS = {
    "ace_evolution": (
        3, 56, 108, 161, 214, 266, 319, 372, 424, 477, 530, 582,
        635, 687, 740, 793, 845, 898, 951, 1003, 1056, 1109, 1161, 1214,
    ),
    "rogue_first_prince": (
        3, 18, 33, 48, 63, 78, 94, 109, 124, 139, 154, 169,
        184, 199, 214, 229, 244, 259, 275, 290, 305, 320, 335, 350,
    ),
}

BRIDGE_LITERARY_CASES = (
    ("ace_evolution", 1),
    ("ace_evolution", 2),
    ("rogue_first_prince", 1),
)

LITERARY_RUBRIC = {
    "scale": {"minimum": 0.0, "maximum": 10.0},
    "weights": {
        "fidelity": 0.30,
        "completeness": 0.15,
        "terminology": 0.15,
        "naturalness": 0.15,
        "dialogue_tone": 0.10,
        "relationships_pronouns": 0.075,
        "cultural_technical": 0.075,
    },
    "defect_flags": (
        "blocker",
        "omission_or_truncation",
        "invented_explanation",
        "terminology_inconsistency",
    ),
}

LONGITUDINAL_STATE_RUBRIC = {
    "scale": {"minimum": 0.0, "maximum": 10.0},
    "weights": {
        "entity_name_continuity": 0.20,
        "terminology_mechanics_rank_continuity": 0.20,
        "factual_plot_continuity": 0.20,
        "relationships_status_pronouns": 0.15,
        "state_precision_hygiene": 0.15,
        "audit_correction_quality_no_regression": 0.10,
    },
}

CHRONOLOGICAL_TRACKS = tuple(
    {
        "track_id": f"{model_id}::{novel_id}",
        "model_id": model_id,
        "novel_id": novel_id,
        "first_chapter": 1,
        "last_chapter": 50,
        "audit_interval": 25,
        "audit_checkpoints": (25, 50),
        "independent_fresh_s0": True,
    }
    for model_id in MODEL_ORDER
    for novel_id in NOVELS
)

G10A_POLICY = {
    "name": "matched_state_free_literary",
    "primary_pair_count": 48,
    "bridge_pair_count": 3,
    "paid_primary_translation_count_if_complete": 102,
    "same_source_text_per_pair": True,
    "same_model_independent_context": True,
    "self_state_disabled": True,
    "continuity_disabled": True,
    "glossary_growth_disabled": True,
    "periodic_audits_disabled": True,
    "retroactive_corrections_disabled": True,
    "one_application_visible_translation_attempt": True,
    "primary_endpoint_excludes_bridge_cases": True,
}

G10B_POLICY = {
    "name": "real_engine_chronological_self_state",
    "track_count": 4,
    "chapters_per_track": 50,
    "canonical_chapter_executions_if_complete": 200,
    "audit_checkpoints_per_track": (25, 50),
    "total_periodic_audits_if_complete": 8,
    "synthetic_audit_cadence_forbidden": True,
    "production_audit_interval_required": 25,
    "real_production_lifecycle_required": True,
    "real_production_audit_semantics_required": True,
    "production_safe_retroactive_corrections_required": True,
    "broad_contextual_find_replace_forbidden": True,
    "pre_audit_snapshot_required": True,
    "post_audit_snapshot_required": True,
    "retroactive_fix_before_after_hashes_required": True,
    "retroactive_fix_backup_evidence_required": True,
    "audit_request_response_evidence_required": True,
    "audit_accounting_required": True,
    "audit_effect_on_future_state_measured": True,
}

SAFETY_POLICY = {
    "fresh_experiment_roots_required": True,
    "old_goat_roots_read_only": True,
    "old_goat_roots_must_never_be_execution_roots": True,
    "request_sha256_binding_required": True,
    "durable_planned_request_before_send": True,
    "raw_response_persist_before_parse": True,
    "application_auto_retry_count": 0,
    "one_application_visible_attempt_per_request": True,
    "maximum_concurrent_paid_transports": 1,
    "single_writer_budget_ledger_required": True,
    "deterministic_rejection_is_terminal": True,
    "ambiguous_remote_outcome_never_resends": True,
    "response_recorded_local_replay_requires_physically_poisoned_transport": True,
    "g10a_ambiguity_quarantine_scope": "model_case",
    "g10b_ambiguity_quarantine_scope": "model_novel_track",
    "independent_non_quarantined_work_may_continue": True,
    "pythonutf8_required_for_child_processes": True,
    "pythonioencoding_utf8_required_for_child_processes": True,
}

BLIND_EVALUATION_POLICY = {
    "cryptographic_private_orientation_nonce_required": True,
    "public_bundle_model_identity_removed": True,
    "public_bundle_effort_identity_removed": True,
    "public_bundle_cost_transport_metadata_removed": True,
    "private_identity_key_commitment_required": True,
    "literary_scores_frozen_before_identity_reveal": True,
    "longitudinal_scores_frozen_before_identity_reveal": True,
    "cost_and_identity_forbidden_from_evaluator_context_before_score_freeze": True,
    "paired_candidates_scored_independently_before_preference": True,
    "preference_values": ("A", "B", "TIE"),
    "missing_later_state_not_imputed_as_quality_failure": True,
}

STATISTICS_POLICY = {
    "g10a_primary_pairs": 48,
    "primary_strata": {
        "ace_evolution": 24,
        "rogue_first_prince": 24,
    },
    "pair_point_rule": {
        "qwen_win": 1.0,
        "tie": 0.5,
        "glm_win": 0.0,
    },
    "report_wtl_separately_by_language": True,
    "pooled_result_after_stratified_reporting_only": True,
    "two_sided_exact_sign_test_on_non_ties": True,
    "stratified_bootstrap_replicates": 10000,
    "stratified_bootstrap_confidence": 0.95,
    "per_dimension_paired_differences_secondary": True,
    "bridge_cases_descriptive_only": True,
    "g10b_inference_mode": "descriptive_two_novel_tracks_per_model",
    "no_composite_overall_winner_score": True,
}

AUDIT_EFFECT_METRICS = (
    "pre_audit_glossary_state_identity",
    "post_audit_glossary_state_identity",
    "audit_decisions_kept_corrected_rejected_contextualized_merged",
    "retroactive_replacements_proposed",
    "retroactive_replacements_eligible",
    "retroactive_replacements_applied",
    "retroactive_replacements_rejected",
    "historical_chapters_modified",
    "historical_chapter_before_after_sha256",
    "audit_regressions_introduced",
    "post_audit_future_terminology_consistency",
    "post_audit_future_entity_continuity",
    "audit_provider_sends",
    "audit_accepted_credit_equivalent",
)

RELIABILITY_COST_METRICS = (
    "completed_depth_by_track",
    "provider_sends_by_operation",
    "accepted_credit_equivalent_by_operation",
    "unresolved_reserved_credit_equivalent",
    "credit_equivalent_per_canonical_chapter",
    "state_glossary_audit_cost_share",
    "ambiguous_remote_outcomes",
    "terminal_provider_policy_failures",
    "local_post_response_failures",
    "replay_resends",
)

BUDGET_POLICY = {
    "pricing_must_be_recaptured_and_frozen_before_paid_execution": True,
    "operator_global_credit_equivalent_ceiling": None,
    "g10a_credit_equivalent_ceiling": None,
    "g10b_credit_equivalent_ceiling": None,
    "budget_committed_definition": (
        "accepted_credit_equivalent_plus_full_reserved_credit_equivalent_"
        "for_every_unresolved_or_ambiguous_request"
    ),
    "finite_operation_aware_maximum_reservation_required": True,
    "next_wave_must_stop_before_credentials_if_max_reservation_exceeds_remaining_budget": True,
    "budget_does_not_reset_on_restart": True,
    "budget_change_requires_new_versioned_freeze": True,
}

IMPLEMENTATION_PHASES = (
    ("G10A0", "preregistration_and_source_binding_freeze", False),
    ("G10A1", "generic_safety_accounting_recovery_tooling", False),
    ("G10A2", "matched_literary_executor_and_ledger", False),
    ("G10B1", "four_track_ch1_50_real_engine_controller", False),
    ("G10C", "generic_blind_bundle_scoring_reveal_tooling", False),
    ("G10D", "physical_shadow_and_poisoned_transport_rehearsal", False),
    ("G10E", "tooling_source_budget_and_root_freeze", False),
    ("G10F", "paid_matched_literary_execution", True),
    ("G10G", "paid_chronological_execution", True),
    ("G10H", "blind_corpus_freeze", False),
    ("G10I", "blind_literary_and_longitudinal_score_freeze", False),
    ("G10J", "identity_reveal", False),
    ("G10K", "exact_reliability_and_cost_join", False),
    ("G10L", "final_immutable_freeze", False),
)


class G10PreregistrationError(RuntimeError):
    """The frozen G10 preregistration is internally inconsistent."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate() -> None:
    if tuple(MODEL_POLICY) != MODEL_ORDER:
        raise G10PreregistrationError("model policy order drifted")
    if set(NOVELS) != {"ace_evolution", "rogue_first_prince"}:
        raise G10PreregistrationError("novel roster drifted")
    if any(spec["audit_interval"] != 25 for spec in NOVELS.values()):
        raise G10PreregistrationError("G10B must preserve production audit interval 25")
    for novel_id, spec in NOVELS.items():
        canonical_sha = spec.get("config_canonical_sha256")
        if (
            not isinstance(canonical_sha, str)
            or len(canonical_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in canonical_sha)
        ):
            raise G10PreregistrationError(
                f"{novel_id} canonical novel config binding is invalid"
            )

    primary_count = sum(len(chapters) for chapters in PRIMARY_LITERARY_CHAPTERS.values())
    if primary_count != 48:
        raise G10PreregistrationError("G10A must contain exactly 48 primary pairs")
    for novel_id, chapters in PRIMARY_LITERARY_CHAPTERS.items():
        if len(chapters) != 24 or len(set(chapters)) != 24:
            raise G10PreregistrationError(f"{novel_id} primary sample must have 24 unique chapters")
        if any(chapter in {1, 2} for chapter in chapters):
            raise G10PreregistrationError("old GOAT early chapters leaked into primary endpoint")
        final_chapter = NOVELS[novel_id]["final_chapter"]
        if min(chapters) < 1 or max(chapters) > final_chapter:
            raise G10PreregistrationError(f"{novel_id} primary sample is out of bounds")

    if len(BRIDGE_LITERARY_CASES) != 3:
        raise G10PreregistrationError("G10A must preserve exactly three GOAT bridge cases")
    if set(BRIDGE_LITERARY_CASES) != {
        ("ace_evolution", 1),
        ("ace_evolution", 2),
        ("rogue_first_prince", 1),
    }:
        raise G10PreregistrationError("G10A bridge-case identity drifted")

    if len(CHRONOLOGICAL_TRACKS) != 4:
        raise G10PreregistrationError("G10B must contain exactly four tracks")
    if len({row["track_id"] for row in CHRONOLOGICAL_TRACKS}) != 4:
        raise G10PreregistrationError("G10B track IDs must be unique")
    for row in CHRONOLOGICAL_TRACKS:
        if row["first_chapter"] != 1 or row["last_chapter"] != 50:
            raise G10PreregistrationError("G10B must run Chapters 1-50")
        if tuple(row["audit_checkpoints"]) != (25, 50):
            raise G10PreregistrationError("G10B audit checkpoints must be 25 and 50")
        if row["audit_interval"] != NOVELS[row["novel_id"]]["audit_interval"]:
            raise G10PreregistrationError("G10B audit interval differs from novel production config")

    if abs(sum(LITERARY_RUBRIC["weights"].values()) - 1.0) > 1e-12:
        raise G10PreregistrationError("literary rubric weights do not sum to one")
    if abs(sum(LONGITUDINAL_STATE_RUBRIC["weights"].values()) - 1.0) > 1e-12:
        raise G10PreregistrationError("longitudinal rubric weights do not sum to one")

    if not G10B_POLICY["production_safe_retroactive_corrections_required"]:
        raise G10PreregistrationError("G10B may not suppress production retroactive fixes")
    if not G10B_POLICY["synthetic_audit_cadence_forbidden"]:
        raise G10PreregistrationError("G10B may not use synthetic audit cadence")
    if SAFETY_POLICY["application_auto_retry_count"] != 0:
        raise G10PreregistrationError("G10 must disable application auto retry")
    if SAFETY_POLICY["maximum_concurrent_paid_transports"] != 1:
        raise G10PreregistrationError("G10 must remain single-transport")
    if SAFETY_POLICY["single_writer_budget_ledger_required"] is not True:
        raise G10PreregistrationError("G10 budget ledger must remain single-writer")
    if BUDGET_POLICY["operator_global_credit_equivalent_ceiling"] is not None:
        raise G10PreregistrationError("initial preregistration must not silently authorize a budget")


_validate()


def g10_preregistration_identity() -> dict[str, Any]:
    return {
        "schema_version": G10_SCHEMA_VERSION,
        "revision": G10_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "repository_anchor": G10_REPOSITORY_ANCHOR,
        "source_bindings": SOURCE_BINDINGS,
        "models": MODEL_POLICY,
        "novels": NOVELS,
        "g10a": {
            "policy": G10A_POLICY,
            "primary_literary_chapters": PRIMARY_LITERARY_CHAPTERS,
            "bridge_literary_cases": BRIDGE_LITERARY_CASES,
            "literary_rubric": LITERARY_RUBRIC,
        },
        "g10b": {
            "policy": G10B_POLICY,
            "tracks": CHRONOLOGICAL_TRACKS,
            "longitudinal_state_rubric": LONGITUDINAL_STATE_RUBRIC,
            "audit_effect_metrics": AUDIT_EFFECT_METRICS,
        },
        "blind_evaluation": BLIND_EVALUATION_POLICY,
        "statistics": STATISTICS_POLICY,
        "safety": SAFETY_POLICY,
        "reliability_cost_metrics": RELIABILITY_COST_METRICS,
        "budget": BUDGET_POLICY,
        "implementation_phases": IMPLEMENTATION_PHASES,
        "provider_calls": 0,
        "provider_calls_authorized": False,
        "identity_reveal_authorized": False,
        "paid_execution_gate": {
            "status": "blocked",
            "required_before_paid_execution": (
                "current_pricing_recaptured_and_frozen",
                "operator_budget_ceiling_explicitly_frozen",
                "fresh_external_g10_roots_verified",
                "generic_durable_execution_layer_green",
                "physical_shadow_poisoned_transport_rehearsal_green",
                "matched_literary_executor_green",
                "ch1_50_real_engine_controller_green",
                "blind_bundle_scoring_reveal_tooling_green",
                "exact_accounting_reconciliation_green",
                "separate_paid_authorization_contract_frozen",
            ),
        },
    }


G10_PREREGISTRATION_SHA256 = _sha256_json(g10_preregistration_identity())


def assert_paid_execution_blocked() -> None:
    identity = g10_preregistration_identity()
    if identity["provider_calls"] != 0:
        raise G10PreregistrationError("preregistration unexpectedly reports Provider calls")
    if identity["provider_calls_authorized"] is not False:
        raise G10PreregistrationError("preregistration unexpectedly authorizes Provider calls")
    if identity["paid_execution_gate"]["status"] != "blocked":
        raise G10PreregistrationError("initial G10 paid gate must remain blocked")
    if identity["budget"]["operator_global_credit_equivalent_ceiling"] is not None:
        raise G10PreregistrationError("paid budget was silently authorized")


__all__ = [
    "AUDIT_EFFECT_METRICS",
    "BLIND_EVALUATION_POLICY",
    "BRIDGE_LITERARY_CASES",
    "BUDGET_POLICY",
    "CHRONOLOGICAL_TRACKS",
    "G10A_POLICY",
    "G10B_POLICY",
    "G10_BENCHMARK_ID",
    "G10_PREREGISTRATION_SHA256",
    "G10_REPOSITORY_ANCHOR",
    "G10_REVISION",
    "GLM_MODEL_ID",
    "IMPLEMENTATION_PHASES",
    "LITERARY_RUBRIC",
    "LONGITUDINAL_STATE_RUBRIC",
    "MODEL_ORDER",
    "MODEL_POLICY",
    "NOVELS",
    "PRIMARY_LITERARY_CHAPTERS",
    "QWEN_MODEL_ID",
    "RELIABILITY_COST_METRICS",
    "SAFETY_POLICY",
    "SOURCE_BINDINGS",
    "STATISTICS_POLICY",
    "assert_paid_execution_blocked",
    "g10_preregistration_identity",
]
