import json

import pytest

from novel_project import NovelConfig, NovelProject
from qa_provenance_backfill_plan import (
    QAProvenanceBackfillPlanError,
    build_qa_provenance_backfill_plan,
)
from semantic_integrity_audit import protected_state_snapshot
from translation_model_config import pending_translation_model_record


pytestmark = pytest.mark.usefixtures("direct_pro_route")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, *, qa_extra=None):
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
        {"chapter": 1, "text": "甲 entered the room."},
        {"chapter": 2, "text": "乙 followed."},
    ])
    _write_json(project.glossary_file, {"entries": {
        "甲": {
            "english": "Jia",
            "category": "character",
            "status": "locked",
        }
    }})
    _write_json(project.progress_file, {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    })
    _write_json(project.continuity_file, {"latest_chapter": 1, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    project.qa_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / "0001.md").write_text("Jia entered the room.", encoding="utf-8")
    qa = {
        "chapter": 1,
        "commit_allowed": True,
        "commit_blockers": [],
        "issues": [],
    }
    qa.update(qa_extra or {})
    _write_json(project.qa_dir / "0001.json", qa)
    return project


def test_backfill_plan_is_ready_and_read_only(tmp_path):
    project = _project(tmp_path)
    before = protected_state_snapshot(project)
    plan = build_qa_provenance_backfill_plan(
        "fixture", 1, repo_root=tmp_path, model_selection="pro"
    )
    assert protected_state_snapshot(project) == before
    assert plan["status"] == "READY"
    assert plan["model"] == "deepseek-v4-pro"
    assert plan["model_version"] == "DeepSeek-V4-Pro-0813"
    assert plan["pricing_version"] == "deepseek-v4-pro-0813-peak-offpeak"
    assert plan["existing_provenance"] == {}
    assert plan["proposed_provenance"] == pending_translation_model_record("pro")
    assert len(plan["state_fingerprint"]) == 64
    assert len(plan["approval_token"]) == 64
    assert plan["backfill_authorized"] is False
    assert plan["api_calls"] == 0
    assert plan["project_writes"] == 0
    assert plan["protected_state_unchanged"] is True


def test_backfill_plan_reports_already_pinned_when_exact(tmp_path):
    exact = pending_translation_model_record("pro")
    _project(tmp_path, qa_extra=exact)
    plan = build_qa_provenance_backfill_plan(
        "fixture", 1, repo_root=tmp_path, model_selection="pro"
    )
    assert plan["status"] == "ALREADY_PINNED"
    assert plan["existing_provenance"] == exact


def test_backfill_plan_fails_closed_on_conflicting_provenance(tmp_path):
    _project(tmp_path, qa_extra={"translation_model": "deepseek-v4-flash"})
    with pytest.raises(QAProvenanceBackfillPlanError, match="conflicting model provenance"):
        build_qa_provenance_backfill_plan(
            "fixture", 1, repo_root=tmp_path, model_selection="pro"
        )


def test_backfill_plan_refuses_pending_or_uncommitted_chapter(tmp_path):
    project = _project(tmp_path)
    (project.pending_dir / "0001.md").write_text("pending", encoding="utf-8")
    with pytest.raises(QAProvenanceBackfillPlanError):
        build_qa_provenance_backfill_plan(
            "fixture", 1, repo_root=tmp_path, model_selection="pro"
        )


def test_backfill_fingerprint_changes_when_bound_qa_changes(tmp_path):
    project = _project(tmp_path)
    first = build_qa_provenance_backfill_plan(
        "fixture", 1, repo_root=tmp_path, model_selection="pro"
    )
    qa_path = project.qa_dir / "0001.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["translation_characters"] = 1234
    _write_json(qa_path, qa)
    second = build_qa_provenance_backfill_plan(
        "fixture", 1, repo_root=tmp_path, model_selection="pro"
    )
    assert first["qa_sha256"] != second["qa_sha256"]
    assert first["state_fingerprint"] != second["state_fingerprint"]
    assert first["approval_token"] != second["approval_token"]
