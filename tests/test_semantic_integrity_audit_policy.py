import json

from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import audit_committed_project


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _project(tmp_path, *, source_text, translation, glossary):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=1,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.chapters_file, [{"chapter": 1, "text": source_text}])
    _write_json(project.glossary_file, {"entries": glossary})
    _write_json(
        project.progress_file,
        {"completed": [1], "last_completed": 1, "next_chapter": 2},
    )
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / "0001.md").write_text(translation, encoding="utf-8")
    return project


def test_project_audit_demotes_missing_canonical_family_to_diagnostic(tmp_path):
    project = _project(
        tmp_path,
        source_text="甲 entered.",
        translation="Someone entered.",
        glossary={
            "甲": {
                "english": "Jia",
                "category": "character",
                "status": "locked",
            }
        },
    )
    report = audit_committed_project(project)
    assert report["diagnostic_count"] == 1
    assert report["diagnostic_types"] == {
        "source_entity_missing_canonical_family": 1
    }
    assert report["advisory_count"] == 0
    assert report["strict_blocker_count"] == 0


def test_project_audit_suppresses_cross_entity_signal_unless_explicitly_enabled(tmp_path):
    project = _project(
        tmp_path,
        source_text="甲 entered.",
        translation="Yi entered.",
        glossary={
            "甲": {
                "english": "Jia",
                "category": "character",
                "status": "locked",
            },
            "乙": {
                "english": "Yi",
                "category": "character",
                "status": "locked",
            },
        },
    )

    default_report = audit_committed_project(project)
    assert default_report["experimental_signal_count"] == 0
    assert default_report["suppressed_experimental_signal_count"] == 1

    research_report = audit_committed_project(project, include_experimental=True)
    assert research_report["suppressed_experimental_signal_count"] == 0
    assert research_report["experimental_signal_count"] == 1
    assert research_report["experimental_signal_types"] == {
        "possible_cross_entity_substitution": 1
    }
    assert research_report["strict_blocker_count"] == 0
