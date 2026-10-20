import json

from audit_semantic_completeness_candidates import (
    broader_parenthetical_candidates,
    inventory_all,
    inventory_project,
)
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, novel_id, *, language="zh", source_text="", completed=(1,)):
    root = tmp_path / "novels" / novel_id
    config = NovelConfig(
        id=novel_id,
        title=novel_id,
        source_language=language,
        target_language="en",
        profile="historical_military",
        final_chapter=1,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    _write_json(project.chapters_file, [{"chapter": 1, "text": source_text}])
    _write_json(project.progress_file, {
        "completed": list(completed),
        "last_completed": 1 if completed else 0,
        "next_chapter": 2 if completed else 1,
    })
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.continuity_file, {"latest_chapter": 0, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    return project


def test_broader_inventory_preserves_v1_and_finds_alias_family():
    source = (
        "他做墩子（即饭店中负责切菜和配菜的人）。"
        "另一职位叫刀手（又称切配工）。"
    )
    candidates = broader_parenthetical_candidates(source)
    assert len(candidates) == 2
    assert candidates[0]["cue_family"] == "equivalence"
    assert candidates[0]["covered_by_v1"] is True
    assert candidates[1]["cue_family"] == "alias"
    assert candidates[1]["covered_by_v1"] is False


def test_inventory_finds_reference_form_not_supported_by_v1(tmp_path):
    project = _project(
        tmp_path,
        "fixture",
        source_text="这里的军户（是指世代承担军役的户籍）已经登记。",
    )
    before = protected_state_snapshot(project)
    report = inventory_project("fixture", repo_root=tmp_path)
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["candidate_count"] == 1
    assert report["covered_by_v1_count"] == 0
    assert report["additional_candidate_count"] == 1
    assert report["cue_family_counts"] == {"reference": 1}
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_inventory_accepts_tagged_chinese_project(tmp_path):
    _project(
        tmp_path,
        "traditional",
        language="zh-Hant",
        source_text="軍戶（是指世代承擔軍役的戶籍）已經登記。",
    )
    report = inventory_project("traditional", repo_root=tmp_path)
    assert report["status"] == "PASS"
    assert report["source_language"] == "zh-Hant"
    assert report["candidate_count"] == 1


def test_inventory_ignores_nondefinitional_parenthetical(tmp_path):
    _project(
        tmp_path,
        "fixture",
        source_text="他走进厨房（今天格外安静），拿起菜刀。",
    )
    report = inventory_project("fixture", repo_root=tmp_path)
    assert report["candidate_count"] == 0
    assert report["additional_candidate_count"] == 0


def test_inventory_all_filters_language_family_and_aggregates(tmp_path):
    _project(
        tmp_path,
        "zh_one",
        source_text="军户（是指世代承担军役的户籍）登记在册。",
    )
    _project(
        tmp_path,
        "zh_two",
        source_text="刀手（又称切配工）站在案板旁。",
    )
    _project(
        tmp_path,
        "zh_hant",
        language="zh-Hant",
        source_text="軍戶（是指世代承擔軍役的戶籍）登記在冊。",
    )
    _project(
        tmp_path,
        "ko_one",
        language="ko",
        source_text="한국어 문장",
    )
    report = inventory_all(tmp_path, source_language="zh")
    assert report["status"] == "PASS"
    assert report["novels_discovered"] == 3
    assert report["novels_scanned"] == 3
    assert report["chapters_scanned"] == 3
    assert report["candidate_count"] == 3
    assert report["covered_by_v1_count"] == 0
    assert report["additional_candidate_count"] == 3
    assert report["cue_family_counts"] == {"alias": 1, "reference": 2}
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["protected_state_unchanged"] is True
