import json

import pytest

from audit_semantic_completeness import (
    SemanticCompletenessAuditError,
    audit_semantic_completeness,
)
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, *, first_translation=None):
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
        {
            "chapter": 1,
            "text": "他已经做墩子（即饭店中负责切菜、切肉和配菜的人）半年了。",
        },
        {"chapter": 2, "text": "他走进厨房（今天格外安静），拿起菜刀。"},
    ])
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.progress_file, {
        "completed": [1, 2],
        "last_completed": 2,
        "next_chapter": 3,
    })
    _write_json(project.continuity_file, {"latest_chapter": 2, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / "0001.md").write_text(
        first_translation or "You've been a chopper for half a year.",
        encoding="utf-8",
    )
    (project.chapter_dir / "0002.md").write_text(
        "He entered the unusually quiet kitchen and picked up the knife.",
        encoding="utf-8",
    )
    return project


def test_corpus_audit_flags_omitted_explicit_gloss_and_is_read_only(tmp_path):
    project = _project(tmp_path)
    before = protected_state_snapshot(project)
    report = audit_semantic_completeness("fixture", repo_root=tmp_path)
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["chapters_scanned"] == 2
    assert report["explicit_source_gloss_count"] == 1
    assert report["completeness_diagnostic_count"] == 1
    assert report["chapters_with_diagnostics"] == [1]
    assert report["findings"][0]["source_label"] == "墩子"
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["protected_state_unchanged"] is True


def test_corpus_audit_accepts_preserved_explanation(tmp_path):
    _project(
        tmp_path,
        first_translation=(
            "He had worked for half a year as a prep cook "
            "(the person responsible for cutting meat and preparing ingredients)."
        ),
    )
    report = audit_semantic_completeness("fixture", repo_root=tmp_path)
    assert report["explicit_source_gloss_count"] == 1
    assert report["completeness_diagnostic_count"] == 0
    assert report["chapters_with_diagnostics"] == []


def test_corpus_audit_can_limit_to_one_committed_chapter(tmp_path):
    _project(tmp_path)
    report = audit_semantic_completeness(
        "fixture", repo_root=tmp_path, chapter=2
    )
    assert report["chapters_scanned"] == 1
    assert report["explicit_source_gloss_count"] == 0
    assert report["completeness_diagnostic_count"] == 0


def test_corpus_audit_refuses_uncommitted_chapter_filter(tmp_path):
    _project(tmp_path)
    with pytest.raises(SemanticCompletenessAuditError, match="not committed"):
        audit_semantic_completeness(
            "fixture", repo_root=tmp_path, chapter=3
        )
