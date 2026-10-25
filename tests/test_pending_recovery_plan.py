import json

import pytest

import pending_recovery_plan as recovery_plan
from novel_project import NovelConfig, NovelProject
from pending_recovery_plan import (
    PendingRecoveryPlanError,
    build_pending_recovery_plan,
)
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _pending_project(tmp_path, *, stage="translated", pinned_model=None):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=2,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    _write_json(project.chapters_file, [
        {"chapter": 1, "text": "甲 entered the hall."},
        {"chapter": 2, "text": "乙 followed."},
    ])
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.progress_file, {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
        "last_audited_chapter": 0,
    })
    _write_json(project.continuity_file, {"latest_chapter": 0, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    (project.pending_dir / "0001.md").write_text(
        "A complete already-paid English translation. " * 40,
        encoding="utf-8",
    )
    meta = {
        "chapter": 1,
        "stage": stage,
        "finish_reason": "stop",
        "translation_stats": {"prompt_tokens": 10, "completion_tokens": 5},
        "local_qa": {"commit_allowed": True, "issues": []},
    }
    if pinned_model:
        meta["translation_model"] = pinned_model
    if stage in {"state_extracted", "reviewed"}:
        meta["state"] = {"new_terms": [], "continuity_summary": "ok", "issues": []}
        meta["state_stats"] = {"prompt_tokens": 3, "completion_tokens": 2}
        meta["state_checkpoint_identity"] = {
            "version": "fixture-state-v1",
            "identity_sha256": "a" * 64,
        }
    if stage == "reviewed":
        meta["review_result"] = {"decisions": [], "issues": []}
        meta["review_stats"] = {"prompt_tokens": 0, "completion_tokens": 0}
        meta["review_checkpoint_identity"] = {
            "version": "fixture-review-v1",
            "identity_sha256": "b" * 64,
        }
    _write_json(project.pending_dir / "0001.json", meta)
    return project


def _synthetic_runtime_root(tmp_path):
    root = tmp_path / "synthetic_engine"
    root.mkdir()
    runtime_file = root / "runtime_helper.py"
    runtime_file.write_text("VALUE = 1\n", encoding="utf-8")
    profile_root = root / "profiles" / "historical_military"
    profile_root.mkdir(parents=True)
    policy_file = profile_root / "profile.json"
    policy_file.write_text('{"policy": 1}\n', encoding="utf-8")
    dependency_manifest = root / "pyproject.toml"
    dependency_manifest.write_text(
        '[project]\nname = "fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    return root, runtime_file, policy_file, dependency_manifest


def test_legacy_pending_project_produces_read_only_ready_plan(tmp_path):
    project = _pending_project(tmp_path)
    before = protected_state_snapshot(project)
    plan = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
    )
    assert protected_state_snapshot(project) == before
    assert plan["status"] == "READY"
    assert plan["pending_status"] == "legacy_unpinned"
    assert plan["target_chapter"] == 1
    assert plan["pending_stage"] == "translated"
    assert plan["has_saved_state"] is False
    assert plan["minimum_new_logical_api_calls"] == 1
    assert plan["translation_generation_allowed"] is False
    assert plan["startup_periodic_audits_allowed"] is False
    assert plan["auxiliary_model"] == "deepseek-v4-pro"
    assert plan["api_calls"] == 0
    assert plan["project_writes"] == 0
    assert plan["protected_state_unchanged"] is True
    assert len(plan["recovery_planner_sha256"]) == 64
    assert len(plan["recovery_executor_sha256"]) == 64
    assert len(plan["recovery_production_script_sha256"]) == 64
    assert len(plan["recovery_chapter_commit_sha256"]) == 64
    assert len(plan["recovery_reviewed_state_sha256"]) == 64
    assert len(plan["recovery_commit_qa_checkpoint_sha256"]) == 64
    assert len(plan["recovery_state_extraction_checkpoint_sha256"]) == 64
    assert len(plan["recovery_partial_commit_reconciliation_sha256"]) == 64
    runtime_identity = plan["recovery_runtime_identity"]
    assert runtime_identity["revision"] == "pending-recovery-runtime-manifest-v1"
    assert runtime_identity["profile"] == "historical_military"
    assert runtime_identity["root_python_file_count"] > 0
    assert runtime_identity["profile_file_count"] > 0
    assert runtime_identity["dependency_manifest"] == "pyproject.toml"
    assert runtime_identity["file_count"] > 0
    assert len(runtime_identity["sha256"]) == 64
    assert len(plan["state_fingerprint"]) == 64
    assert len(plan["approval_token"]) == 64


def _make_reviewed_partial_commit(project):
    meta_path = project.pending_dir / "0001.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["stage"] = "reviewed"
    meta["state"] = {
        "new_terms": [],
        "continuity_summary": "ok",
        "issues": [],
    }
    meta["state_stats"] = {"prompt_tokens": 3, "completion_tokens": 2}
    meta["eligible_candidates"] = []
    meta["prefilter_rejected"] = []
    meta["review_result"] = {"decisions": [], "issues": []}
    meta["review_stats"] = {"prompt_tokens": 0, "completion_tokens": 0}
    _write_json(meta_path, meta)

    pending = project.pending_dir / "0001.md"
    final = project.chapter_dir / "0001.md"
    final.write_bytes(pending.read_bytes())
    project.qa_dir.mkdir(parents=True, exist_ok=True)
    _write_json(project.qa_dir / "0001.json", meta["local_qa"])
    return meta


def test_identical_reviewed_final_is_ready_for_commit_tail_reconciliation(
    tmp_path,
):
    project = _pending_project(tmp_path, stage="reviewed")
    _make_reviewed_partial_commit(project)

    plan = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert plan["status"] == "READY"
    assert plan["recovery_mode"] == "reconcile_reviewed_partial_commit_tail"
    evidence = plan["partial_commit_evidence"]
    assert evidence["final_exists"] is True
    assert len(evidence["final_sha256"]) == 64
    assert len(evidence["qa_sha256"]) == 64
    assert plan["minimum_new_logical_api_calls"] == 0


def test_committed_looking_target_with_different_bytes_still_fails_closed(
    tmp_path,
):
    project = _pending_project(tmp_path, stage="reviewed")
    _make_reviewed_partial_commit(project)
    (project.chapter_dir / "0001.md").write_text(
        "different committed-looking text",
        encoding="utf-8",
    )

    with pytest.raises(
        PendingRecoveryPlanError,
        match="does not exactly match",
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )


def test_identical_final_without_reviewed_checkpoint_still_fails_closed(
    tmp_path,
):
    project = _pending_project(tmp_path, stage="state_extracted")
    pending = project.pending_dir / "0001.md"
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / "0001.md").write_bytes(pending.read_bytes())

    with pytest.raises(
        PendingRecoveryPlanError,
        match="durable reviewed pending checkpoint",
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )


def test_identical_reviewed_final_requires_exact_committed_qa(tmp_path):
    project = _pending_project(tmp_path, stage="reviewed")
    _make_reviewed_partial_commit(project)
    (project.qa_dir / "0001.json").unlink()

    with pytest.raises(
        PendingRecoveryPlanError,
        match="committed QA evidence does not exist",
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )


def test_saved_state_reduces_known_minimum_new_calls_to_zero(tmp_path):
    _pending_project(tmp_path, stage="state_extracted")
    plan = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    assert plan["has_saved_state"] is True
    assert plan["minimum_new_logical_api_calls"] == 0


def test_pinned_pending_must_resolve_through_runtime_model_policy(tmp_path):
    _pending_project(tmp_path, pinned_model="deepseek-v4-flash")
    plan = build_pending_recovery_plan(
        "fixture", repo_root=tmp_path, model_selection="flash"
    )
    assert plan["pending_status"] == "pending_model_match"
    assert plan["auxiliary_model"] == "deepseek-v4-flash"


def test_pinned_pending_override_binds_effective_model_for_recovery(tmp_path):
    _pending_project(tmp_path, pinned_model="deepseek-v4-flash")
    plan = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert plan["pending_status"] == "pending_model_override"
    assert plan["requested_auxiliary_model"] == "deepseek-v4-pro"
    assert plan["auxiliary_model"] == "deepseek-v4-flash"
    assert plan["model_version"] == "DeepSeek-V4-Flash-0731"
    assert (
        plan["pricing_version"]
        == "deepseek-v4-flash-0731-peak-offpeak"
    )


def test_extra_pending_chapter_fails_closed(tmp_path):
    project = _pending_project(tmp_path)
    (project.pending_dir / "0002.md").write_text("extra", encoding="utf-8")
    _write_json(project.pending_dir / "0002.json", {"chapter": 2, "stage": "translated"})
    with pytest.raises(Exception):
        build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")


def test_pending_content_change_invalidates_fingerprint_and_token(tmp_path):
    project = _pending_project(tmp_path)
    first = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    (project.pending_dir / "0001.md").write_text(
        "changed pending bytes " * 80, encoding="utf-8"
    )
    second = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]


def test_recovery_executor_change_invalidates_fingerprint_and_token(monkeypatch, tmp_path):
    _pending_project(tmp_path)
    planner = tmp_path / "pending_recovery_plan.py"
    executor = tmp_path / "pending_recovery_execute.py"
    planner.write_text("planner-v1", encoding="utf-8")
    executor.write_text("executor-v1", encoding="utf-8")
    monkeypatch.setattr(recovery_plan, "PLANNER_FILE", planner)
    monkeypatch.setattr(recovery_plan, "RECOVERY_EXECUTOR_FILE", executor)

    first = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    executor.write_text("executor-v2", encoding="utf-8")
    second = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")

    assert first["recovery_planner_sha256"] == second["recovery_planner_sha256"]
    assert first["recovery_executor_sha256"] != second["recovery_executor_sha256"]
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]



def test_recovery_production_code_change_invalidates_authorization(
    monkeypatch,
    tmp_path,
):
    _pending_project(tmp_path)

    planner = tmp_path / "pending_recovery_plan.py"
    executor = tmp_path / "pending_recovery_execute.py"
    production = tmp_path / (
        "translate_production_unattended_scheduled_"
        "retroactive_dynamic_pricing.py"
    )
    chapter_commit = tmp_path / "production_chapter_commit.py"
    reviewed_state = tmp_path / "production_reviewed_state.py"
    commit_qa_checkpoint = tmp_path / "production_commit_qa_checkpoint.py"
    state_extraction_checkpoint = (
        tmp_path / "production_state_extraction_checkpoint.py"
    )

    planner.write_text("planner-v1", encoding="utf-8")
    executor.write_text("executor-v1", encoding="utf-8")
    production.write_text("production-v1", encoding="utf-8")
    chapter_commit.write_text("chapter-commit-v1", encoding="utf-8")
    reviewed_state.write_text("reviewed-state-v1", encoding="utf-8")
    commit_qa_checkpoint.write_text(
        "commit-qa-checkpoint-v1",
        encoding="utf-8",
    )
    state_extraction_checkpoint.write_text(
        "state-extraction-checkpoint-v1",
        encoding="utf-8",
    )

    monkeypatch.setattr(recovery_plan, "PLANNER_FILE", planner)
    monkeypatch.setattr(
        recovery_plan,
        "RECOVERY_EXECUTOR_FILE",
        executor,
    )
    monkeypatch.setattr(
        recovery_plan,
        "PRODUCTION_SCRIPT_FILE",
        production,
    )
    monkeypatch.setattr(
        recovery_plan,
        "CHAPTER_COMMIT_FILE",
        chapter_commit,
    )
    monkeypatch.setattr(
        recovery_plan,
        "REVIEWED_STATE_FILE",
        reviewed_state,
    )
    monkeypatch.setattr(
        recovery_plan,
        "COMMIT_QA_CHECKPOINT_FILE",
        commit_qa_checkpoint,
    )
    monkeypatch.setattr(
        recovery_plan,
        "STATE_EXTRACTION_CHECKPOINT_FILE",
        state_extraction_checkpoint,
    )

    original = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    production.write_text("production-v2", encoding="utf-8")

    changed_production = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        original["recovery_production_script_sha256"]
        != changed_production["recovery_production_script_sha256"]
    )
    assert (
        original["state_fingerprint"]
        != changed_production["state_fingerprint"]
    )
    assert (
        original["approval_token"]
        != changed_production["approval_token"]
    )

    chapter_commit.write_text("chapter-commit-v2", encoding="utf-8")

    changed_commit = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        changed_production["recovery_chapter_commit_sha256"]
        != changed_commit["recovery_chapter_commit_sha256"]
    )
    assert (
        changed_production["state_fingerprint"]
        != changed_commit["state_fingerprint"]
    )
    assert (
        changed_production["approval_token"]
        != changed_commit["approval_token"]
    )

    reviewed_state.write_text("reviewed-state-v2", encoding="utf-8")

    changed_reviewed_state = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        changed_commit["recovery_reviewed_state_sha256"]
        != changed_reviewed_state["recovery_reviewed_state_sha256"]
    )
    assert (
        changed_commit["state_fingerprint"]
        != changed_reviewed_state["state_fingerprint"]
    )
    assert (
        changed_commit["approval_token"]
        != changed_reviewed_state["approval_token"]
    )

    commit_qa_checkpoint.write_text(
        "commit-qa-checkpoint-v2",
        encoding="utf-8",
    )

    changed_commit_qa_checkpoint = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        changed_reviewed_state["recovery_commit_qa_checkpoint_sha256"]
        != changed_commit_qa_checkpoint[
            "recovery_commit_qa_checkpoint_sha256"
        ]
    )
    assert (
        changed_reviewed_state["state_fingerprint"]
        != changed_commit_qa_checkpoint["state_fingerprint"]
    )
    assert (
        changed_reviewed_state["approval_token"]
        != changed_commit_qa_checkpoint["approval_token"]
    )

    state_extraction_checkpoint.write_text(
        "state-extraction-checkpoint-v2",
        encoding="utf-8",
    )

    changed_state_extraction_checkpoint = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        changed_commit_qa_checkpoint[
            "recovery_state_extraction_checkpoint_sha256"
        ]
        != changed_state_extraction_checkpoint[
            "recovery_state_extraction_checkpoint_sha256"
        ]
    )
    assert (
        changed_commit_qa_checkpoint["state_fingerprint"]
        != changed_state_extraction_checkpoint["state_fingerprint"]
    )
    assert (
        changed_commit_qa_checkpoint["approval_token"]
        != changed_state_extraction_checkpoint["approval_token"]
    )


def test_root_engine_runtime_change_invalidates_recovery_approval(
    tmp_path,
    monkeypatch,
):
    _pending_project(tmp_path)
    engine_root, runtime_file, _policy_file, _dependency_manifest = (
        _synthetic_runtime_root(tmp_path)
    )
    monkeypatch.setattr(recovery_plan, "ENGINE_RUNTIME_ROOT", engine_root)

    first = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    runtime_file.write_text("VALUE = 2\n", encoding="utf-8")
    second = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        first["recovery_runtime_identity"]["sha256"]
        != second["recovery_runtime_identity"]["sha256"]
    )
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]

    (engine_root / "new_runtime_helper.py").write_text(
        "VALUE = 3\n",
        encoding="utf-8",
    )
    third = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        third["recovery_runtime_identity"]["root_python_file_count"]
        == second["recovery_runtime_identity"]["root_python_file_count"] + 1
    )
    assert (
        second["recovery_runtime_identity"]["sha256"]
        != third["recovery_runtime_identity"]["sha256"]
    )
    assert second["state_fingerprint"] != third["state_fingerprint"]
    assert second["approval_token"] != third["approval_token"]


def test_selected_profile_policy_change_invalidates_recovery_approval(
    tmp_path,
    monkeypatch,
):
    _pending_project(tmp_path)
    engine_root, _runtime_file, policy_file, _dependency_manifest = (
        _synthetic_runtime_root(tmp_path)
    )
    monkeypatch.setattr(recovery_plan, "ENGINE_RUNTIME_ROOT", engine_root)

    first = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    policy_file.write_text('{"policy": 2}\n', encoding="utf-8")
    second = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        first["recovery_runtime_identity"]["sha256"]
        != second["recovery_runtime_identity"]["sha256"]
    )
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]


def test_unselected_profile_change_does_not_invalidate_recovery_approval(
    tmp_path,
    monkeypatch,
):
    _pending_project(tmp_path)
    engine_root, _runtime_file, _policy_file, _dependency_manifest = (
        _synthetic_runtime_root(tmp_path)
    )
    other_profile = engine_root / "profiles" / "xianxia_xuanhuan"
    other_profile.mkdir(parents=True)
    other_policy = other_profile / "profile.json"
    other_policy.write_text('{"policy": 1}\n', encoding="utf-8")
    monkeypatch.setattr(recovery_plan, "ENGINE_RUNTIME_ROOT", engine_root)

    first = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    other_policy.write_text('{"policy": 2}\n', encoding="utf-8")
    second = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        first["recovery_runtime_identity"]["sha256"]
        == second["recovery_runtime_identity"]["sha256"]
    )
    assert first["state_fingerprint"] == second["state_fingerprint"]
    assert first["approval_token"] == second["approval_token"]


def test_dependency_policy_change_invalidates_recovery_approval(
    tmp_path,
    monkeypatch,
):
    _pending_project(tmp_path)
    engine_root, _runtime_file, _policy_file, dependency_manifest = (
        _synthetic_runtime_root(tmp_path)
    )
    monkeypatch.setattr(recovery_plan, "ENGINE_RUNTIME_ROOT", engine_root)

    first = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    dependency_manifest.write_text(
        '[project]\nname = "fixture"\nversion = "2.0"\n',
        encoding="utf-8",
    )
    second = build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        first["recovery_runtime_identity"]["sha256"]
        != second["recovery_runtime_identity"]["sha256"]
    )
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]


def test_recovery_runtime_manifest_requires_profile_and_dependency_policy(
    tmp_path,
    monkeypatch,
):
    _pending_project(tmp_path)
    engine_root, _runtime_file, policy_file, dependency_manifest = (
        _synthetic_runtime_root(tmp_path)
    )
    monkeypatch.setattr(recovery_plan, "ENGINE_RUNTIME_ROOT", engine_root)

    policy_file.unlink()
    with pytest.raises(
        PendingRecoveryPlanError,
        match="selected translation profile contains no policy files",
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )

    policy_file.write_text('{"policy": 1}\n', encoding="utf-8")
    dependency_manifest.unlink()
    with pytest.raises(
        PendingRecoveryPlanError,
        match="dependency policy manifest does not exist",
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )


@pytest.mark.parametrize(
    ("stage", "missing_key"),
    [
        ("state_extracted", "state_checkpoint_identity"),
        ("reviewed", "state_checkpoint_identity"),
        ("reviewed", "review_checkpoint_identity"),
    ],
)
def test_unbound_saved_checkpoint_stage_is_blocked_read_only(
    tmp_path,
    stage,
    missing_key,
):
    project = _pending_project(tmp_path, stage=stage)
    meta_path = project.pending_dir / "0001.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop(missing_key)
    _write_json(meta_path, meta)

    with pytest.raises(
        PendingRecoveryPlanError,
        match=missing_key,
    ):
        build_pending_recovery_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
        )


def test_budget_must_be_positive(tmp_path):
    _pending_project(tmp_path)
    with pytest.raises(PendingRecoveryPlanError, match="max_api_calls"):
        build_pending_recovery_plan("fixture", repo_root=tmp_path, max_api_calls=0)
    with pytest.raises(PendingRecoveryPlanError, match="max_cost_usd"):
        build_pending_recovery_plan("fixture", repo_root=tmp_path, max_cost_usd=0)


def test_candidate_preparation_bytes_invalidate_recovery_approval(tmp_path, monkeypatch):
    _pending_project(tmp_path)
    helper = tmp_path / "production_candidate_preparation.py"
    helper.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(recovery_plan, "CANDIDATE_PREPARATION_FILE", helper)
    first = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    helper.write_text("v2", encoding="utf-8")
    second = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    for key in ("recovery_candidate_preparation_sha256", "state_fingerprint", "approval_token"):
        assert first[key] != second[key]
    helper.unlink()
    with pytest.raises(PendingRecoveryPlanError, match="candidate_preparation"):
        build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")


def test_chapter_review_bytes_invalidate_recovery_approval(tmp_path, monkeypatch):
    _pending_project(tmp_path)
    helper = tmp_path / "chapter_glossary_review.py"
    helper.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(recovery_plan, "CHAPTER_REVIEW_FILE", helper)
    first = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    helper.write_text("v2", encoding="utf-8")
    second = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    for key in ("recovery_chapter_review_sha256", "state_fingerprint", "approval_token"):
        assert first[key] != second[key]


def test_production_lock_bytes_invalidate_recovery_approval(tmp_path, monkeypatch):
    _pending_project(tmp_path)
    helper = tmp_path / "production_run_lock.py"
    helper.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(recovery_plan, "PRODUCTION_RUN_LOCK_FILE", helper)
    first = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    helper.write_text("v2", encoding="utf-8")
    second = build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    for key in ("recovery_production_run_lock_sha256", "state_fingerprint", "approval_token"):
        assert first[key] != second[key]


@pytest.mark.parametrize("original", [None, {}, {"attempted": True}, "malformed"])
def test_uncertain_original_review_blocks_read_only_plan_and_operator(tmp_path, original):
    from operator_preflight import build_operator_preflight
    project = _pending_project(tmp_path, stage="state_extracted")
    path = project.pending_dir / "0001.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["chapter_glossary_review_recovery"] = {
        "status": "original_started", "original": original,
    }
    _write_json(path, meta)
    before = protected_state_snapshot(project)
    with pytest.raises(PendingRecoveryPlanError, match="automatic retry is forbidden"):
        build_pending_recovery_plan("fixture", repo_root=tmp_path, model_selection="pro")
    report = build_operator_preflight("fixture", repo_root=tmp_path, model_selection="pro")
    assert report["status"] == "BLOCKED"
    assert report["api_calls"] == report["project_writes"] == 0
    assert protected_state_snapshot(project) == before


@pytest.fixture(autouse=True)
def explicit_direct_route(monkeypatch):
    """Recovery compatibility proofs explicitly select the historical direct route."""
    monkeypatch.setenv("NOVELTRANSLATOR_PROVIDER_BACKEND", "direct_deepseek")
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", "pro")
