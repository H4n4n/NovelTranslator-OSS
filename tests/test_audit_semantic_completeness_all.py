import json

from audit_semantic_completeness_all import (
    audit_all_semantic_completeness,
    discover_novel_ids,
)
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, novel_id, *, source_language="zh", diagnostic=False):
    root = tmp_path / "novels" / novel_id
    config = NovelConfig(
        id=novel_id,
        title=f"Fixture {novel_id}",
        source_language=source_language,
        target_language="en",
        profile="historical_military",
        final_chapter=1,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    source_text = (
        "他已经做墩子（即饭店中负责切菜、切肉和配菜的人）半年了。"
        if source_language.casefold().startswith("zh")
        else "그는 부엌에서 일했다."
    )
    _write_json(project.chapters_file, [{"chapter": 1, "text": source_text}])
    _write_json(project.glossary_file, {"entries": {}})
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
    translation = (
        "You've been a chopper for half a year."
        if diagnostic
        else "He worked as a prep cook (the person responsible for cutting and preparing ingredients)."
    )
    (project.chapter_dir / "0001.md").write_text(translation, encoding="utf-8")
    return project


def test_discovery_selects_requested_language_family_including_tagged_variants(tmp_path):
    _project(tmp_path, "zh_one", source_language="zh")
    _project(tmp_path, "zh_hant", source_language="zh-Hant")
    _project(tmp_path, "ko_one", source_language="ko")
    selected, errors = discover_novel_ids(tmp_path, source_language="zh")
    assert selected == ["zh_hant", "zh_one"]
    assert errors == []


def test_explicit_tag_filter_remains_exact(tmp_path):
    _project(tmp_path, "zh_one", source_language="zh")
    _project(tmp_path, "zh_hant", source_language="zh-Hant")
    selected, errors = discover_novel_ids(tmp_path, source_language="zh-Hant")
    assert selected == ["zh_hant"]
    assert errors == []


def test_aggregate_calibration_sums_chinese_projects_and_is_read_only(tmp_path):
    first = _project(tmp_path, "zh_clean", diagnostic=False)
    second = _project(tmp_path, "zh_flagged", diagnostic=True)
    third = _project(tmp_path, "zh_hant_clean", source_language="zh-Hant", diagnostic=False)
    _project(tmp_path, "ko_ignored", source_language="ko")
    before = {
        first.config.id: protected_state_snapshot(first),
        second.config.id: protected_state_snapshot(second),
        third.config.id: protected_state_snapshot(third),
    }

    report = audit_all_semantic_completeness(tmp_path, source_language="zh")

    assert protected_state_snapshot(first) == before[first.config.id]
    assert protected_state_snapshot(second) == before[second.config.id]
    assert protected_state_snapshot(third) == before[third.config.id]
    assert report["status"] == "PASS"
    assert report["novel_ids"] == ["zh_clean", "zh_flagged", "zh_hant_clean"]
    assert report["novels_discovered"] == 3
    assert report["novels_scanned"] == 3
    assert report["chapters_scanned"] == 3
    assert report["explicit_source_gloss_count"] == 3
    assert report["completeness_diagnostic_count"] == 1
    assert report["chapters_with_diagnostics"] == 1
    assert report["diagnostic_rate_per_explicit_gloss"] == 1 / 3
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_aggregate_reports_zero_rate_when_no_explicit_glosses(tmp_path):
    project = _project(tmp_path, "zh_plain", diagnostic=False)
    _write_json(project.chapters_file, [{"chapter": 1, "text": "他走进厨房。"}])
    (project.chapter_dir / "0001.md").write_text("He entered the kitchen.", encoding="utf-8")
    report = audit_all_semantic_completeness(tmp_path, source_language="zh")
    assert report["status"] == "PASS"
    assert report["explicit_source_gloss_count"] == 0
    assert report["completeness_diagnostic_count"] == 0
    assert report["diagnostic_rate_per_explicit_gloss"] == 0.0


def test_discovery_error_fails_aggregate_without_running_invalid_project(tmp_path):
    _project(tmp_path, "zh_valid", diagnostic=False)
    broken = tmp_path / "novels" / "broken"
    broken.mkdir(parents=True)
    (broken / "novel.json").write_text("{not json", encoding="utf-8")

    report = audit_all_semantic_completeness(tmp_path, source_language="zh")

    assert report["status"] == "FAIL"
    assert report["novels_discovered"] == 1
    assert report["novels_scanned"] == 1
    assert report["errors"]
    assert "novel.json" in report["errors"][0]
