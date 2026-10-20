import json
from pathlib import Path

from audit_semantic_integrity import main as audit_main
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import (
    audit_committed_project,
    protected_state_snapshot,
)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path, *, chapters, glossary, completed, outputs):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=max(item["chapter"] for item in chapters),
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    _write_json(project.chapters_file, chapters)
    _write_json(project.glossary_file, {"entries": glossary})
    _write_json(
        project.progress_file,
        {
            "completed": completed,
            "last_completed": max(completed) if completed else 0,
            "next_chapter": (max(completed) + 1) if completed else 1,
        },
    )
    _write_json(project.continuity_file, {"summary": "fixture"})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    for chapter, text in outputs.items():
        (project.chapter_dir / f"{chapter:04d}.md").write_text(text, encoding="utf-8")
    return project


def test_audit_reports_known_alias_and_cross_chapter_drift_without_writing(tmp_path):
    project = _project(
        tmp_path,
        chapters=[
            {"chapter": 1, "text": "甲 entered the hall."},
            {"chapter": 2, "text": "甲 left the hall."},
        ],
        glossary={
            "甲": {
                "english": "Alpha",
                "category": "character",
                "status": "locked",
                "repair_aliases": ["Beta"],
            },
        },
        completed=[1, 2],
        outputs={1: "Alpha entered the hall.", 2: "Beta left the hall."},
    )
    before = protected_state_snapshot(project)
    report = audit_committed_project(project)
    after = protected_state_snapshot(project)

    assert before == after
    assert report["api_calls"] == 0
    assert report["writes_performed"] == 0
    assert report["audited_chapter_count"] == 2
    assert report["advisory_count"] == 1
    assert report["advisory_types"] == {"known_noncanonical_entity_alias": 1}
    assert report["advisories"][0]["chapter"] == 2
    assert report["strict_blocker_count"] == 0
    assert report["drift_term_count"] == 1
    drift = report["drift"][0]
    assert drift["source_key"] == "甲"
    assert {item["rendering"] for item in drift["variants"]} == {"Alpha", "Beta"}


def test_audit_preserves_existing_strict_qa_as_blocker_but_remains_read_only(tmp_path):
    project = _project(
        tmp_path,
        chapters=[{"chapter": 1, "text": "乙 appeared."}],
        glossary={
            "乙": {
                "english": "Gamma",
                "category": "character",
                "status": "locked",
                "strict_qa": True,
            },
        },
        completed=[1],
        outputs={1: "Someone appeared."},
    )
    before = protected_state_snapshot(project)
    report = audit_committed_project(project)
    assert protected_state_snapshot(project) == before
    assert report["advisory_count"] == 0
    assert report["strict_blocker_count"] == 1
    assert report["strict_blockers"][0]["chapter"] == 1
    assert report["strict_blockers"][0]["type"] == "source_entity_missing_canonical_family"


def test_audit_range_selects_only_completed_chapters_in_range(tmp_path):
    project = _project(
        tmp_path,
        chapters=[
            {"chapter": 1, "text": "甲"},
            {"chapter": 2, "text": "甲"},
            {"chapter": 3, "text": "甲"},
        ],
        glossary={
            "甲": {"english": "Alpha", "category": "character", "status": "locked"},
        },
        completed=[1, 2, 3],
        outputs={1: "Alpha", 2: "Alpha", 3: "Alpha"},
    )
    report = audit_committed_project(project, start=2, end=2)
    assert report["selected_chapters"] == [2]
    assert report["selected_chapter_count"] == 1
    assert report["audited_chapter_count"] == 1


def test_audit_surfaces_missing_committed_translation_as_structural_error(tmp_path):
    project = _project(
        tmp_path,
        chapters=[{"chapter": 1, "text": "甲"}],
        glossary={
            "甲": {"english": "Alpha", "category": "character", "status": "locked"},
        },
        completed=[1],
        outputs={},
    )
    report = audit_committed_project(project)
    assert report["audited_chapter_count"] == 0
    assert report["errors"] == [
        {
            "chapter": 1,
            "type": "missing_committed_translation",
            "path": str(project.chapter_dir / "0001.md"),
        }
    ]


def test_audit_rejects_invalid_ranges_before_scanning(tmp_path):
    project = _project(
        tmp_path,
        chapters=[{"chapter": 1, "text": "甲"}],
        glossary={},
        completed=[1],
        outputs={1: "Alpha"},
    )
    try:
        audit_committed_project(project, start=2, end=1)
    except ValueError as exc:
        assert "greater than or equal" in str(exc)
    else:
        raise AssertionError("invalid range should fail")


def test_cli_human_mode_reports_zero_cost_and_unchanged_state(tmp_path, monkeypatch, capsys):
    project = _project(
        tmp_path,
        chapters=[{"chapter": 1, "text": "甲"}],
        glossary={
            "甲": {"english": "Alpha", "category": "character", "status": "locked"},
        },
        completed=[1],
        outputs={1: "Alpha"},
    )
    monkeypatch.setattr("audit_semantic_integrity.load_novel_project", lambda novel: project)
    assert audit_main(["--novel", "fixture"]) == 0
    output = capsys.readouterr().out
    assert "API calls: 0" in output
    assert "Writes performed: 0" in output
    assert "Protected production state unchanged: YES" in output


def test_cli_json_mode_is_machine_readable(tmp_path, monkeypatch, capsys):
    project = _project(
        tmp_path,
        chapters=[{"chapter": 1, "text": "甲"}],
        glossary={},
        completed=[1],
        outputs={1: "Alpha"},
    )
    monkeypatch.setattr("audit_semantic_integrity.load_novel_project", lambda novel: project)
    assert audit_main(["--novel", "fixture", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["mode"] == "read_only_semantic_integrity_audit"
    assert parsed["protected_state_unchanged"] is True
    assert parsed["api_calls"] == 0
    assert parsed["writes_performed"] == 0
