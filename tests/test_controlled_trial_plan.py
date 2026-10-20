import json

import pytest

import controlled_trial_plan as planner

from controlled_trial_plan import (
    ControlledTrialPlanError,
    build_controlled_trial_plan,
)
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_project(tmp_path):
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
        {"chapter": 1, "text": "甲 entered."},
        {"chapter": 2, "text": "乙 followed."},
    ])
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.progress_file, {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
    })
    _write_json(project.continuity_file, {"latest_chapter": 0, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    return project


def test_clean_empty_project_produces_read_only_ready_plan(tmp_path):
    project = _empty_project(tmp_path)
    before = protected_state_snapshot(project)
    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=4,
        max_cost_usd=0.05,
    )
    assert protected_state_snapshot(project) == before
    assert plan["status"] == "READY"
    assert plan["paid_execution_authorized"] is False
    assert plan["target_chapter"] == 1
    assert plan["requested_model"] == "deepseek-v4-pro"
    assert plan["model_version"] == "DeepSeek-V4-Pro-0813"
    assert plan["pending_numeric_count"] == 0
    assert plan["max_api_calls"] == 4
    assert plan["max_cost_usd"] == 0.05
    assert len(plan["state_fingerprint"]) == 64
    assert len(plan["approval_token"]) == 64
    assert plan["api_calls"] == 0
    assert plan["project_writes"] == 0
    assert plan["protected_state_unchanged"] is True

    assert plan["scheduler_mode"] == "off_peak_only"
    assert plan["allow_peak"] is False

    assert set(plan["trial_tools"]) == {
        "planner",
        "executor",
        "production_script",
        "production_launcher",
        "peak_wrapper",
        "credential_bridge",
    }

    for identity in plan["trial_tools"].values():
        assert isinstance(identity["path"], str)
        assert len(identity["sha256"]) == 64


    assert plan["engine_runtime_identity"]["profile"] == "historical_military"
    assert plan["engine_runtime_identity"]["file_count"] > 0
    assert len(plan["engine_runtime_identity"]["sha256"]) == 64


def test_model_selection_is_bound_into_approval_token(tmp_path):
    _empty_project(tmp_path)
    pro = build_controlled_trial_plan(
        "fixture", repo_root=tmp_path, model_selection="pro"
    )
    flash = build_controlled_trial_plan(
        "fixture", repo_root=tmp_path, model_selection="flash"
    )
    assert pro["state_fingerprint"] != flash["state_fingerprint"]
    assert pro["approval_token"] != flash["approval_token"]
    assert flash["requested_model"] == "deepseek-v4-flash"


def test_existing_pending_translation_rejects_first_trial(tmp_path):
    project = _empty_project(tmp_path)
    (project.pending_dir / "0001.md").write_text("paid output", encoding="utf-8")
    _write_json(project.pending_dir / "0001.json", {
        "chapter": 1,
        "stage": "translated",
        "translation_model": "deepseek-v4-pro",
    })
    before = protected_state_snapshot(project)
    with pytest.raises(ControlledTrialPlanError, match="no existing pending"):
        build_controlled_trial_plan(
            "fixture", repo_root=tmp_path, model_selection="pro"
        )
    assert protected_state_snapshot(project) == before


def test_budget_must_be_positive(tmp_path):
    _empty_project(tmp_path)
    with pytest.raises(ControlledTrialPlanError, match="max_api_calls"):
        build_controlled_trial_plan(
            "fixture", repo_root=tmp_path, max_api_calls=0
        )
    with pytest.raises(ControlledTrialPlanError, match="max_cost_usd"):
        build_controlled_trial_plan(
            "fixture", repo_root=tmp_path, max_cost_usd=0
        )


def test_project_state_change_invalidates_fingerprint_and_token(tmp_path):
    project = _empty_project(tmp_path)
    first = build_controlled_trial_plan(
        "fixture", repo_root=tmp_path, model_selection="pro"
    )
    _write_json(project.continuity_file, {
        "latest_chapter": 0,
        "summary": "changed after plan",
    })
    second = build_controlled_trial_plan(
        "fixture", repo_root=tmp_path, model_selection="pro"
    )
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]

def test_scheduler_mode_is_bound_into_fingerprint_and_token(
    tmp_path,
):
    _empty_project(tmp_path)

    normal = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        allow_peak=False,
    )

    peak = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        allow_peak=True,
    )

    assert normal["scheduler_mode"] == "off_peak_only"
    assert normal["allow_peak"] is False

    assert peak["scheduler_mode"] == "peak_allowed"
    assert peak["allow_peak"] is True

    assert (
        normal["state_fingerprint"]
        != peak["state_fingerprint"]
    )
    assert (
        normal["approval_token"]
        != peak["approval_token"]
    )


def test_executor_code_identity_is_bound_into_approval(
    tmp_path,
    monkeypatch,
):
    _empty_project(tmp_path)

    original = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    changed_executor = (
        tmp_path / "controlled_trial_execute.py"
    )
    changed_executor.write_text(
        "# synthetic changed executor\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        planner,
        "TRIAL_EXECUTOR_FILE",
        changed_executor,
    )

    changed = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        original["trial_tools"]["executor"]["sha256"]
        != changed["trial_tools"]["executor"]["sha256"]
    )

    assert (
        original["state_fingerprint"]
        != changed["state_fingerprint"]
    )
    assert (
        original["approval_token"]
        != changed["approval_token"]
    )


def test_engine_runtime_code_identity_is_bound_into_approval(
    tmp_path,
    monkeypatch,
):
    _empty_project(tmp_path)

    engine_root = tmp_path / "synthetic_engine"
    engine_root.mkdir()
    runtime_file = engine_root / "runtime_helper.py"
    runtime_file.write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    profile_root = (
        engine_root
        / "profiles"
        / "historical_military"
    )
    profile_root.mkdir(parents=True)
    (profile_root / "profile.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        planner,
        "ENGINE_RUNTIME_ROOT",
        engine_root,
    )

    original = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    runtime_file.write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )

    changed = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        original["engine_runtime_identity"]["sha256"]
        != changed["engine_runtime_identity"]["sha256"]
    )
    assert (
        original["state_fingerprint"]
        != changed["state_fingerprint"]
    )
    assert (
        original["approval_token"]
        != changed["approval_token"]
    )


def test_selected_profile_policy_identity_is_bound_into_approval(
    tmp_path,
    monkeypatch,
):
    _empty_project(tmp_path)

    engine_root = tmp_path / "synthetic_engine"
    engine_root.mkdir()
    (engine_root / "runtime_helper.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    profile_root = (
        engine_root
        / "profiles"
        / "historical_military"
    )
    profile_root.mkdir(parents=True)
    policy_file = profile_root / "profile.json"
    policy_file.write_text(
        "{\"policy\": 1}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        planner,
        "ENGINE_RUNTIME_ROOT",
        engine_root,
    )

    original = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    policy_file.write_text(
        "{\"policy\": 2}\n",
        encoding="utf-8",
    )

    changed = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert (
        original["engine_runtime_identity"]["sha256"]
        != changed["engine_runtime_identity"]["sha256"]
    )
    assert (
        original["state_fingerprint"]
        != changed["state_fingerprint"]
    )
    assert (
        original["approval_token"]
        != changed["approval_token"]
    )


def test_allow_peak_must_be_boolean(tmp_path):
    _empty_project(tmp_path)

    with pytest.raises(
        ControlledTrialPlanError,
        match="allow_peak must be a boolean",
    ):
        build_controlled_trial_plan(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            allow_peak="yes",
        )


def test_v41_plan_is_deterministic_read_only_and_requires_separate_authorization(tmp_path):
    project = _empty_project(tmp_path)
    before = protected_state_snapshot(project)
    first = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash")
    second = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="deepseek-flash")
    assert first == second
    assert first["requested_model"] == "deepseek-flash"
    assert first["model_version"] == "DeepSeek-V4.1-Flash"
    assert first["max_output_tokens"] == 12000  # Ordinary translation ceiling stays unchanged.
    assert first["thinking"] == "disabled"
    assert first["paid_execution_authorized"] is False
    assert first["api_calls"] == first["project_writes"] == 0
    assert protected_state_snapshot(project) == before


def test_real_engine_manifest_includes_operation_policy_and_dependencies(monkeypatch):
    observed = set()
    original_hash = planner._sha256_file
    def record(path):
        observed.add(path.name)
        return original_hash(path)
    monkeypatch.setattr(planner, "_sha256_file", record)
    planner._engine_runtime_identity("historical_military")
    assert {"production_operation_policy.py", "production_state_extraction.py",
            "translation_model_config.py", "controlled_trial_execute.py"} <= observed
