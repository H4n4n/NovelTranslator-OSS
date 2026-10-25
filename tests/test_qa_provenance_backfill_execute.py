import hashlib
import json

import pytest

from novel_project import NovelConfig, NovelProject
from qa_provenance_backfill_execute import (
    QAProvenanceBackfillExecutionError,
    execute_qa_provenance_backfill,
)
from qa_provenance_backfill_plan import build_qa_provenance_backfill_plan
from translation_model_config import pending_translation_model_record


pytestmark = pytest.mark.usefixtures("direct_pro_route")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        "translation_characters": 21,
    }
    qa.update(qa_extra or {})
    _write_json(project.qa_dir / "0001.json", qa)
    return project


def _plan(tmp_path):
    return build_qa_provenance_backfill_plan(
        "fixture", 1, repo_root=tmp_path, model_selection="pro"
    )


def test_executor_without_authorization_is_zero_write(tmp_path):
    project = _project(tmp_path)
    plan = _plan(tmp_path)
    qa_path = project.qa_dir / "0001.json"
    before = qa_path.read_bytes()
    report = execute_qa_provenance_backfill(
        "fixture",
        1,
        repo_root=tmp_path,
        model_selection="pro",
        state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_backfill=False,
    )
    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["changed_paths"] == []
    assert qa_path.read_bytes() == before


def test_executor_rejects_wrong_fingerprint_or_token_before_write(tmp_path):
    project = _project(tmp_path)
    plan = _plan(tmp_path)
    qa_path = project.qa_dir / "0001.json"
    before = qa_path.read_bytes()
    with pytest.raises(QAProvenanceBackfillExecutionError, match="state fingerprint"):
        execute_qa_provenance_backfill(
            "fixture",
            1,
            repo_root=tmp_path,
            model_selection="pro",
            state_fingerprint="0" * 64,
            approval_token=plan["approval_token"],
            authorize_backfill=True,
        )
    assert qa_path.read_bytes() == before

    with pytest.raises(QAProvenanceBackfillExecutionError, match="approval token"):
        execute_qa_provenance_backfill(
            "fixture",
            1,
            repo_root=tmp_path,
            model_selection="pro",
            state_fingerprint=plan["state_fingerprint"],
            approval_token="f" * 64,
            authorize_backfill=True,
        )
    assert qa_path.read_bytes() == before


def test_authorized_executor_changes_only_qa_and_preserves_existing_fields(tmp_path):
    project = _project(tmp_path)
    plan = _plan(tmp_path)
    qa_path = project.qa_dir / "0001.json"
    progress_before = _sha(project.progress_file)
    output_before = _sha(project.chapter_dir / "0001.md")
    qa_before = json.loads(qa_path.read_text(encoding="utf-8"))
    writes = []

    def write_json(path, payload):
        writes.append(path)
        _write_json(path, payload)

    report = execute_qa_provenance_backfill(
        "fixture",
        1,
        repo_root=tmp_path,
        model_selection="pro",
        state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_backfill=True,
        write_json=write_json,
    )
    assert report["status"] == "BACKFILLED"
    assert report["api_calls"] == 0
    assert report["project_writes"] == 1
    assert writes == [qa_path]
    assert report["changed_paths"] == [str(qa_path)]
    assert report["qa_sha256_before"] != report["qa_sha256_after"]

    saved = json.loads(qa_path.read_text(encoding="utf-8"))
    for key, value in qa_before.items():
        assert saved[key] == value
    for key, value in pending_translation_model_record("pro").items():
        assert saved[key] == value
    assert _sha(project.progress_file) == progress_before
    assert _sha(project.chapter_dir / "0001.md") == output_before
    assert not (project.pending_dir / "0001.md").exists()
    assert not (project.pending_dir / "0001.json").exists()


def test_executor_rejects_stale_bound_state(tmp_path):
    project = _project(tmp_path)
    plan = _plan(tmp_path)
    qa_path = project.qa_dir / "0001.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["translation_characters"] = 999
    _write_json(qa_path, qa)
    changed = qa_path.read_bytes()
    with pytest.raises(QAProvenanceBackfillExecutionError, match="state fingerprint"):
        execute_qa_provenance_backfill(
            "fixture",
            1,
            repo_root=tmp_path,
            model_selection="pro",
            state_fingerprint=plan["state_fingerprint"],
            approval_token=plan["approval_token"],
            authorize_backfill=True,
        )
    assert qa_path.read_bytes() == changed


def test_already_pinned_is_idempotent_zero_write(tmp_path):
    project = _project(tmp_path, qa_extra=pending_translation_model_record("pro"))
    plan = _plan(tmp_path)
    qa_path = project.qa_dir / "0001.json"
    before = qa_path.read_bytes()
    report = execute_qa_provenance_backfill(
        "fixture",
        1,
        repo_root=tmp_path,
        model_selection="pro",
        state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_backfill=True,
    )
    assert report["status"] == "ALREADY_PINNED"
    assert report["project_writes"] == 0
    assert qa_path.read_bytes() == before
