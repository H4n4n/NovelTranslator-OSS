import benchmark_longitudinal_g10_chronological as g10b
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    QWEN_MODEL_ID,
)


def _progress(last_completed, last_audited):
    return {
        "last_completed": last_completed,
        "next_chapter": last_completed + 1,
        "last_audited_chapter": last_audited,
    }


def test_g10b1_is_zero_provider_and_paid_execution_is_blocked():
    g10b.assert_paid_execution_blocked()
    identity = g10b.g10b1_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["paid_execution_entrypoint_present"] is False
    assert identity["track_count"] == 4
    assert identity["chapters_per_track"] == 50
    assert identity["canonical_chapter_executions_if_complete"] == 200
    assert identity["audits_if_complete"] == 8


def test_track_roster_is_exact_qwen_glm_x_two_novels():
    assert len(g10b.TRACK_SPECS) == 4
    assert {(row.model_id, row.novel_id) for row in g10b.TRACK_SPECS} == {
        (QWEN_MODEL_ID, "ace_evolution"),
        (GLM_MODEL_ID, "ace_evolution"),
        (QWEN_MODEL_ID, "rogue_first_prince"),
        (GLM_MODEL_ID, "rogue_first_prince"),
    }
    assert len({row.run_id for row in g10b.TRACK_SPECS}) == 4
    for row in g10b.TRACK_SPECS:
        assert row.audit_interval == 25
        assert row.translation_effort in {"medium", "high"}
        assert row.structured_effort == "low"
        assert row.semantic_effort == row.translation_effort


def test_real_audit_windows_are_only_chapter_25_and_50():
    assert g10b.audit_window_for_target(1) is None
    assert g10b.audit_window_for_target(24) is None
    assert g10b.audit_window_for_target(25) == (1, 25)
    assert g10b.audit_window_for_target(26) is None
    assert g10b.audit_window_for_target(49) is None
    assert g10b.audit_window_for_target(50) == (26, 50)


def test_expected_audit_marker_follows_real_production_boundaries():
    assert g10b.expected_last_audited_chapter(0) == 0
    assert g10b.expected_last_audited_chapter(24) == 0
    assert g10b.expected_last_audited_chapter(25) == 25
    assert g10b.expected_last_audited_chapter(49) == 25
    assert g10b.expected_last_audited_chapter(50) == 50


def test_track_cannot_advance_past_ch25_without_committed_real_audit():
    classification = g10b.classify_track_progress(
        _progress(25, 0)
    )
    assert classification["status"] == "audit_recovery_required"
    assert classification["audit_window"] == (1, 25)
    assert classification["provider_send_authorized"] is False

    ready = g10b.classify_track_progress(
        _progress(25, 25)
    )
    assert ready["status"] == "ready_for_target"
    assert ready["target_chapter"] == 26


def test_track_cannot_complete_ch50_without_second_committed_audit():
    classification = g10b.classify_track_progress(
        _progress(50, 25)
    )
    assert classification["status"] == "audit_recovery_required"
    assert classification["audit_window"] == (26, 50)

    complete = g10b.classify_track_progress(
        _progress(50, 50)
    )
    assert complete["status"] == "complete"
    assert complete["provider_send_authorized"] is False


def test_corrupt_progress_fails_closed():
    wrong_next = {
        "last_completed": 10,
        "next_chapter": 12,
        "last_audited_chapter": 0,
    }
    assert g10b.classify_track_progress(wrong_next)["status"] == "corrupt_block"

    audit_ahead = _progress(10, 25)
    assert g10b.classify_track_progress(audit_ahead)["status"] == "corrupt_block"

    impossible_audit = _progress(30, 0)
    result = g10b.classify_track_progress(impossible_audit)
    assert result["status"] == "corrupt_block"
    assert result["reason"] == (
        "later_chapters_crossed_uncommitted_audit_boundary"
    )
    assert result["audit_window"] == (1, 25)


def test_durable_quarantine_blocks_only_that_track():
    run_id = g10b.TRACK_SPECS[0].run_id
    result = g10b.classify_track_progress(
        _progress(16, 0),
        quarantine={
            "scope_kind": "chronological_model_novel_track",
            "scope_id": run_id,
            "first_blocked_chapter": 17,
            "outcome": "ambiguous_remote_outcome",
        },
        expected_run_id=run_id,
    )
    assert result["status"] == "quarantined"
    assert result["first_blocked_chapter"] == 17


def test_four_track_wave_order_rotates_every_chapter():
    orders = [g10b.wave_track_order(chapter) for chapter in range(1, 5)]
    base = orders[0]
    assert orders[1] == base[1:] + base[:1]
    assert orders[2] == base[2:] + base[:2]
    assert orders[3] == base[3:] + base[:3]

    for run_id in base:
        positions = [
            g10b.wave_track_order(chapter).index(run_id) + 1
            for chapter in range(1, 5)
        ]
        assert sorted(positions) == [1, 2, 3, 4]


def test_target_25_contract_keeps_audit_inside_real_engine_call():
    track = g10b.TRACK_SPECS[0]
    contract = g10b.target_contract(track, 25)
    assert contract["production_call"] == (
        "run_unattended_production(target_chapter=25)"
    )
    assert contract["audit"]["due_after_target_commit"] is True
    assert contract["audit"]["window"] == (1, 25)
    assert contract["audit"]["real_production_trigger_only"] is True
    assert contract["audit"]["synthetic_audit_forbidden"] is True
    assert (
        contract["audit"]["dynamic_request_level_budget_admission_required"]
        is True
    )
    assert contract["audit"]["static_audit_send_ceiling_preregistered"] is False
    assert contract["provider_calls_authorized"] is False


def test_wave_planner_runs_ready_tracks_and_leaves_quarantined_track_out():
    chapter = 17
    progress = {
        run_id: _progress(16, 0)
        for run_id in g10b.TRACK_BY_ID
    }
    blocked_run = g10b.wave_track_order(chapter)[0]
    wave = g10b.plan_chapter_wave(
        chapter,
        progress,
        quarantines={
            blocked_run: {
                "scope_kind": "chronological_model_novel_track",
                "scope_id": blocked_run,
                "first_blocked_chapter": 17,
                "outcome": "ambiguous_remote_outcome",
            }
        },
    )
    assert wave["provider_calls"] == 0
    assert wave["paid_execution_authorized"] is False
    assert len(wave["ready_invocations"]) == 3
    assert all(
        row["run_id"] != blocked_run
        for row in wave["ready_invocations"]
    )
    assert any(
        row["run_id"] == blocked_run
        and row["status"] == "quarantined"
        for row in wave["blocked_or_not_at_target"]
    )


def test_contract_freezes_dynamic_audit_budget_policy_not_fake_static_ceiling():
    policy = g10b.g10b1_contract_identity()["audit_policy"]
    assert policy["real_production_audit_trigger_only"] is True
    assert policy["synthetic_audits_forbidden"] is True
    assert policy["audit_subrequests_use_request_level_g10a1_reservations"] is True
    assert policy["state_dependent_audit_send_count_not_invented"] is True
    assert policy["g10e_budget_authorization_still_required"] is True


def test_quarantine_for_wrong_run_or_wrong_chapter_fails_closed():
    run_id = g10b.TRACK_SPECS[0].run_id
    other = g10b.TRACK_SPECS[1].run_id
    wrong_run = g10b.classify_track_progress(
        _progress(16, 0),
        quarantine={
            "scope_kind": "chronological_model_novel_track",
            "scope_id": other,
            "first_blocked_chapter": 17,
            "outcome": "ambiguous_remote_outcome",
        },
        expected_run_id=run_id,
    )
    assert wrong_run["status"] == "corrupt_block"
    assert wrong_run["reason"] == "quarantine_scope_id_mismatch"

    wrong_chapter = g10b.classify_track_progress(
        _progress(16, 0),
        quarantine={
            "scope_kind": "chronological_model_novel_track",
            "scope_id": run_id,
            "first_blocked_chapter": 18,
            "outcome": "ambiguous_remote_outcome",
        },
        expected_run_id=run_id,
    )
    assert wrong_chapter["status"] == "corrupt_block"
    assert wrong_chapter["reason"] == "quarantine_blocked_chapter_mismatch"
