import benchmark_longitudinal_g10_preregistration as g10


def test_preregistration_is_zero_provider_and_paid_execution_is_blocked():
    g10.assert_paid_execution_blocked()
    identity = g10.g10_preregistration_identity()
    assert identity["provider_calls"] == 0
    assert identity["provider_calls_authorized"] is False
    assert identity["identity_reveal_authorized"] is False
    assert identity["paid_execution_gate"]["status"] == "blocked"
    assert identity["budget"]["operator_global_credit_equivalent_ceiling"] is None


def test_model_and_reasoning_policy_are_frozen_to_goat_selected_efforts():
    assert g10.MODEL_ORDER == (
        "qwen/qwen3.8-max-0902",
        "z-ai/glm-5.3-flash",
    )
    assert g10.MODEL_POLICY[g10.QWEN_MODEL_ID] == {
        "primary_translation_effort": "medium",
        "structured_auxiliary_effort": "low",
        "semantic_auxiliary_effort": "medium",
    }
    assert g10.MODEL_POLICY[g10.GLM_MODEL_ID] == {
        "primary_translation_effort": "high",
        "structured_auxiliary_effort": "low",
        "semantic_auxiliary_effort": "high",
    }


def test_primary_literary_endpoint_is_fresh_balanced_and_separate_from_bridge():
    assert {k: len(v) for k, v in g10.PRIMARY_LITERARY_CHAPTERS.items()} == {
        "ace_evolution": 24,
        "rogue_first_prince": 24,
    }
    assert sum(len(v) for v in g10.PRIMARY_LITERARY_CHAPTERS.values()) == 48
    for chapters in g10.PRIMARY_LITERARY_CHAPTERS.values():
        assert 1 not in chapters
        assert 2 not in chapters
        assert len(chapters) == len(set(chapters))
    assert set(g10.BRIDGE_LITERARY_CASES) == {
        ("ace_evolution", 1),
        ("ace_evolution", 2),
        ("rogue_first_prince", 1),
    }
    assert g10.STATISTICS_POLICY["g10a_primary_pairs"] == 48
    assert g10.STATISTICS_POLICY["bridge_cases_descriptive_only"] is True


def test_g10a_is_state_free_and_has_no_audits_or_retroactive_mutation():
    policy = g10.G10A_POLICY
    assert policy["primary_pair_count"] == 48
    assert policy["bridge_pair_count"] == 3
    assert policy["paid_primary_translation_count_if_complete"] == 102
    assert policy["self_state_disabled"] is True
    assert policy["continuity_disabled"] is True
    assert policy["glossary_growth_disabled"] is True
    assert policy["periodic_audits_disabled"] is True
    assert policy["retroactive_corrections_disabled"] is True


def test_g10b_runs_four_real_engine_tracks_through_two_production_audits():
    assert len(g10.CHRONOLOGICAL_TRACKS) == 4
    assert {row["model_id"] for row in g10.CHRONOLOGICAL_TRACKS} == set(g10.MODEL_ORDER)
    assert {row["novel_id"] for row in g10.CHRONOLOGICAL_TRACKS} == set(g10.NOVELS)
    for row in g10.CHRONOLOGICAL_TRACKS:
        assert row["first_chapter"] == 1
        assert row["last_chapter"] == 50
        assert row["audit_interval"] == 25
        assert row["audit_checkpoints"] == (25, 50)
        assert row["independent_fresh_s0"] is True

    policy = g10.G10B_POLICY
    assert policy["canonical_chapter_executions_if_complete"] == 200
    assert policy["total_periodic_audits_if_complete"] == 8
    assert policy["synthetic_audit_cadence_forbidden"] is True
    assert policy["production_audit_interval_required"] == 25
    assert policy["real_production_lifecycle_required"] is True
    assert policy["real_production_audit_semantics_required"] is True
    assert policy["production_safe_retroactive_corrections_required"] is True


def test_audit_effects_freeze_before_after_and_retroactive_evidence():
    required = {
        "pre_audit_glossary_state_identity",
        "post_audit_glossary_state_identity",
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
    }
    assert required.issubset(set(g10.AUDIT_EFFECT_METRICS))
    assert g10.G10B_POLICY["pre_audit_snapshot_required"] is True
    assert g10.G10B_POLICY["post_audit_snapshot_required"] is True
    assert g10.G10B_POLICY["retroactive_fix_before_after_hashes_required"] is True
    assert g10.G10B_POLICY["retroactive_fix_backup_evidence_required"] is True


def test_safety_policy_has_no_silent_retry_and_quarantines_chronology():
    safety = g10.SAFETY_POLICY
    assert safety["application_auto_retry_count"] == 0
    assert safety["one_application_visible_attempt_per_request"] is True
    assert safety["maximum_concurrent_paid_transports"] == 1
    assert safety["single_writer_budget_ledger_required"] is True
    assert safety["ambiguous_remote_outcome_never_resends"] is True
    assert safety["response_recorded_local_replay_requires_physically_poisoned_transport"] is True
    assert safety["g10a_ambiguity_quarantine_scope"] == "model_case"
    assert safety["g10b_ambiguity_quarantine_scope"] == "model_novel_track"
    assert safety["old_goat_roots_read_only"] is True
    assert safety["old_goat_roots_must_never_be_execution_roots"] is True
    assert safety["pythonutf8_required_for_child_processes"] is True
    assert safety["pythonioencoding_utf8_required_for_child_processes"] is True


def test_blind_scores_freeze_before_reveal_and_hide_cost_identity():
    policy = g10.BLIND_EVALUATION_POLICY
    assert policy["public_bundle_model_identity_removed"] is True
    assert policy["public_bundle_effort_identity_removed"] is True
    assert policy["public_bundle_cost_transport_metadata_removed"] is True
    assert policy["literary_scores_frozen_before_identity_reveal"] is True
    assert policy["longitudinal_scores_frozen_before_identity_reveal"] is True
    assert policy["cost_and_identity_forbidden_from_evaluator_context_before_score_freeze"] is True


def test_rubric_weights_and_statistics_are_frozen():
    assert abs(sum(g10.LITERARY_RUBRIC["weights"].values()) - 1.0) < 1e-12
    assert abs(sum(g10.LONGITUDINAL_STATE_RUBRIC["weights"].values()) - 1.0) < 1e-12
    stats = g10.STATISTICS_POLICY
    assert stats["primary_strata"] == {
        "ace_evolution": 24,
        "rogue_first_prince": 24,
    }
    assert stats["two_sided_exact_sign_test_on_non_ties"] is True
    assert stats["stratified_bootstrap_replicates"] == 10000
    assert stats["stratified_bootstrap_confidence"] == 0.95
    assert stats["no_composite_overall_winner_score"] is True


def test_preregistration_identity_hash_is_deterministic():
    assert len(g10.G10_PREREGISTRATION_SHA256) == 64
    assert g10.G10_PREREGISTRATION_SHA256 == g10._sha256_json(
        g10.g10_preregistration_identity()
    )


def test_paid_phases_exist_but_are_not_authorized_by_this_module():
    paid = {phase for phase, _description, is_paid in g10.IMPLEMENTATION_PHASES if is_paid}
    assert paid == {"G10F", "G10G"}
    gate = g10.g10_preregistration_identity()["paid_execution_gate"]
    assert "separate_paid_authorization_contract_frozen" in gate["required_before_paid_execution"]
