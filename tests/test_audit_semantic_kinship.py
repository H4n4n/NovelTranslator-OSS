import json

from audit_semantic_kinship import audit_all, audit_project
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, novel_id, *, language="zh", source_text, translation):
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
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.progress_file, {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    })
    _write_json(project.continuity_file, {"latest_chapter": 1, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / "0001.md").write_text(translation, encoding="utf-8")
    return project


def test_project_audit_flags_ace_style_generic_siblings_and_is_read_only(tmp_path):
    project = _project(
        tmp_path,
        "ace_shape",
        source_text="两人是亲生的姐弟。",
        translation="Siblings. Biological siblings.",
    )
    before = protected_state_snapshot(project)
    report = audit_project("ace_shape", repo_root=tmp_path)
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["ordered_kinship_occurrence_count"] == 1
    assert report["diagnostic_count"] == 1
    assert report["classification_counts"] == {"generic_siblings": 1}
    assert report["findings"][0]["source_term"] == "姐弟"
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_project_audit_accepts_visible_age_and_gender(tmp_path):
    _project(
        tmp_path,
        "preserved",
        source_text="他们是一对兄妹。",
        translation="They were an older brother and a younger sister.",
    )
    report = audit_project("preserved", repo_root=tmp_path)
    assert report["ordered_kinship_occurrence_count"] == 1
    assert report["diagnostic_count"] == 0
    assert report["classification_counts"] == {"age_and_gender_visible": 1}


def test_aggregate_matches_zh_family_and_ignores_korean(tmp_path):
    _project(
        tmp_path,
        "traditional",
        language="zh-Hant",
        source_text="他們是姐弟。",
        translation="They were a sister and brother.",
    )
    _project(
        tmp_path,
        "simplified",
        language="zh-Hans",
        source_text="他们是兄妹。",
        translation="They were siblings.",
    )
    _project(
        tmp_path,
        "korean",
        language="ko",
        source_text="그들은 남매였다.",
        translation="They were siblings.",
    )
    report = audit_all(tmp_path, source_language="zh")
    assert report["status"] == "PASS"
    assert report["novels_discovered"] == 2
    assert report["novels_scanned"] == 2
    assert report["chapters_scanned"] == 2
    assert report["ordered_kinship_occurrence_count"] == 2
    assert report["diagnostic_count"] == 2
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["protected_state_unchanged"] is True


def test_aggregate_zero_occurrences_has_zero_rate(tmp_path):
    _project(
        tmp_path,
        "plain",
        source_text="他独自走进厨房。",
        translation="He entered the kitchen alone.",
    )
    report = audit_all(tmp_path, source_language="zh")
    assert report["ordered_kinship_occurrence_count"] == 0
    assert report["diagnostic_count"] == 0
    assert report["diagnostic_rate"] == 0.0
