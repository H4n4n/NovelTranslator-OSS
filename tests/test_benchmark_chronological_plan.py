import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_chronological_plan as plan
from benchmark_novel import BenchmarkSafetyError


CODE_HEAD = "c" * 40
DATA_HEAD = "d" * 40


def _fake_inputs(data_repo_root: Path, novel_id: str, language: str, profile_id: str):
    project_root = data_repo_root / "novels" / novel_id
    data_root = project_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    entries = {
        f"{novel_id}-early": {
            "english": "Early Term",
            "category": "character",
            "status": "locked",
            "first_chapter": 2,
            "notes": "mature reference",
        },
        f"{novel_id}-future": {
            "english": "Future Term",
            "category": "character",
            "status": "locked",
            "first_chapter": 30,
            "notes": "must not enter early reference",
        },
        f"{novel_id}-unanchored": {
            "english": "Unanchored",
            "category": "character",
            "status": "locked",
        },
    }
    glossary_file = data_root / "glossary.json"
    glossary_file.write_text(
        json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    addendum_file = project_root / "translation_addendum.txt"
    addendum_file.write_text(
        f"MATURE-ADDENDUM-LEAK-{novel_id}",
        encoding="utf-8",
    )

    config = SimpleNamespace(
        id=novel_id,
        title=f"Title {novel_id}",
        source_language=language,
        target_language="en",
        chapter_numbering="global",
    )
    project = SimpleNamespace(
        root=project_root,
        config=config,
        glossary_file=glossary_file,
        translation_addendum_file=addendum_file,
    )
    profile = SimpleNamespace(
        id=profile_id,
        translation_instructions=f"PROFILE-ONLY-{profile_id}",
    )
    chapters = {
        chapter: {
            "chapter": chapter,
            "text": f"{novel_id} source {chapter}\n\nparagraph two {chapter}",
            "paragraph_count": 2,
        }
        for chapter in range(1, 26)
    }
    return SimpleNamespace(
        project=project,
        profile=profile,
        system_prompt=(
            profile.translation_instructions
            + "\n\n"
            + addendum_file.read_text(encoding="utf-8")
        ),
        chapters=chapters,
        glossary_entries=entries,
    )


def _install_fixture(monkeypatch, tmp_path):
    code_repo_root = tmp_path / "code-repo"
    data_repo_root = tmp_path / "data-repo"
    code_repo_root.mkdir()
    data_repo_root.mkdir()
    fixtures = {
        "great_king_zhuxia": _fake_inputs(
            data_repo_root, "great_king_zhuxia", "zh", "xianxia_xuanhuan"
        ),
        "rogue_first_prince": _fake_inputs(
            data_repo_root, "rogue_first_prince", "ko", "korean_fantasy"
        ),
    }

    monkeypatch.setattr(plan, "CODE_REPO_ROOT", code_repo_root)

    def fake_repo_head(root):
        root = Path(root).resolve()
        if root == code_repo_root.resolve():
            return CODE_HEAD
        if root == data_repo_root.resolve():
            return DATA_HEAD
        raise AssertionError(f"unexpected repository root: {root}")

    monkeypatch.setattr(plan, "_repo_head", fake_repo_head)
    monkeypatch.setattr(
        plan,
        "load_benchmark_inputs",
        lambda novel_id, _root=None: fixtures[novel_id],
    )
    monkeypatch.setattr(
        plan,
        "capture_protected_state",
        lambda project: {
            "novel_id": project.config.id,
            "protected": "stable",
        },
    )
    monkeypatch.setattr(
        plan,
        "validate_chapter_selection",
        lambda inputs, chapters: tuple(
            chapter
            for chapter in chapters
            if chapter in inputs.chapters
        ),
    )
    monkeypatch.setattr(
        plan,
        "source_prompt_heading",
        lambda chapter, numbering, **kwargs: f"FROZEN HEADING {chapter['chapter']}",
    )
    monkeypatch.setattr(
        plan,
        "source_language_prompt_label",
        lambda language: f"SOURCE-{language}",
    )
    return code_repo_root, data_repo_root, fixtures


def _freeze(plan_module, data_repo_root, output_root, **kwargs):
    return plan_module.freeze_chronological_plan(
        data_repo_root=data_repo_root,
        output_root=output_root,
        expected_code_repo_head=kwargs.pop("expected_code_repo_head", CODE_HEAD),
        expected_data_repo_head=kwargs.pop("expected_data_repo_head", DATA_HEAD),
        **kwargs,
    )


def test_freeze_chronological_plan_is_no_leak_and_deterministic(monkeypatch, tmp_path):
    _, data_repo_root, fixtures = _install_fixture(monkeypatch, tmp_path)
    first_root = tmp_path / "freeze-one"

    frozen, path = _freeze(plan, data_repo_root, first_root)

    assert path == first_root / plan.PLAN_FILENAME
    assert frozen["status"] == "frozen"
    assert frozen["identity"]["code_repo_head"] == CODE_HEAD
    assert frozen["identity"]["data_repo_head"] == DATA_HEAD
    assert frozen["identity"]["chapter_count_per_novel"] == 25
    assert frozen["paid_provider_calls"] == 0
    assert frozen["production_writes"] == 0
    assert frozen["state_policy"] == {
        "initial_dynamic_glossary": "empty",
        "initial_continuity": "empty",
        "novel_specific_addendum_provider_visible": False,
        "mature_glossary_provider_visible": False,
        "provider_visible_system_prompt": "profile_only",
        "per_arm_state_lineage": "independent",
        "translation_uses_state_before_chapter": True,
        "state_update_occurs_after_translation": True,
        "human_corrections_feed_back_into_model_state": False,
        "reference_material_role": "scoring_and_audit_only",
    }

    for record in frozen["novels"]:
        novel_id = record["novel_id"]
        fixture = fixtures[novel_id]
        assert record["chapters"] == list(range(1, 26))
        assert len(record["chapter_records"]) == 25

        provider_prompt = first_root / record["provider_visible_system_prompt"]["path"]
        provider_text = provider_prompt.read_text(encoding="utf-8")
        assert provider_text == fixture.profile.translation_instructions
        assert "MATURE-ADDENDUM-LEAK" not in provider_text
        assert record["provider_visible_system_prompt"][
            "novel_specific_addendum_included"
        ] is False

        reference_addendum = record["reference_only_translation_addendum"]
        assert reference_addendum["provider_visible"] is False
        assert "MATURE-ADDENDUM-LEAK" in (
            first_root / reference_addendum["snapshot_path"]
        ).read_text(encoding="utf-8")

        state_path = first_root / record["initial_state"]["path"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["through_chapter"] == 0
        assert state["glossary_entries"] == {}
        assert state["continuity_summary"] == ""
        assert state["issues"] == []
        assert state["state_events"] == []
        assert record["initial_state"]["glossary_entry_count"] == 0
        assert record["initial_state"]["continuity_is_empty"] is True

        reference = record["reference_only_mature_glossary"]
        assert reference["provider_visible"] is False
        assert reference["eligible_reference_entries"] == 1
        assert reference["anchored_future_entries_excluded"] == 1
        assert reference["entries_without_valid_first_chapter_excluded"] == 1
        reference_doc = json.loads(
            (first_root / reference["snapshot_path"]).read_text(encoding="utf-8")
        )
        assert list(reference_doc["entries"]) == [f"{novel_id}-early"]

        for chapter_record in record["chapter_records"]:
            chapter = chapter_record["chapter_number"]
            source_path = first_root / chapter_record["source_snapshot_path"]
            assert source_path.read_text(encoding="utf-8") == fixture.chapters[chapter]["text"]
            assert chapter_record["source_heading"] == f"FROZEN HEADING {chapter}"
            assert chapter_record["source_label"] == f"SOURCE-{fixture.project.config.source_language}"

    second_root = tmp_path / "freeze-two"
    second, _ = _freeze(plan, data_repo_root, second_root)
    assert second["identity_sha256"] == frozen["identity_sha256"]
    assert second["identity"] == frozen["identity"]


def test_freeze_rejects_output_inside_data_repository(monkeypatch, tmp_path):
    _, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    with pytest.raises(BenchmarkSafetyError, match="outside code and data repositories"):
        _freeze(plan, data_repo_root, data_repo_root / "benchmark-output")


def test_freeze_rejects_output_inside_code_repository(monkeypatch, tmp_path):
    code_repo_root, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    with pytest.raises(BenchmarkSafetyError, match="outside code and data repositories"):
        _freeze(plan, data_repo_root, code_repo_root / "benchmark-output")


def test_freeze_rejects_code_repository_head_mismatch(monkeypatch, tmp_path):
    _, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    with pytest.raises(BenchmarkSafetyError, match="code repository HEAD mismatch"):
        _freeze(
            plan,
            data_repo_root,
            tmp_path / "outside-code-head",
            expected_code_repo_head="1" * 40,
        )


def test_freeze_rejects_data_repository_head_mismatch(monkeypatch, tmp_path):
    _, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    with pytest.raises(BenchmarkSafetyError, match="data repository HEAD mismatch"):
        _freeze(
            plan,
            data_repo_root,
            tmp_path / "outside-data-head",
            expected_data_repo_head="2" * 40,
        )


def test_freeze_never_overwrites_existing_target(monkeypatch, tmp_path):
    _, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    output_root = tmp_path / "already-there"
    output_root.mkdir()
    marker = output_root / "keep.txt"
    marker.write_text("do not touch", encoding="utf-8")

    with pytest.raises(BenchmarkSafetyError, match="already exists"):
        _freeze(plan, data_repo_root, output_root)

    assert marker.read_text(encoding="utf-8") == "do not touch"


def test_freeze_fails_if_protected_state_changes(monkeypatch, tmp_path):
    _, data_repo_root, _ = _install_fixture(monkeypatch, tmp_path)
    calls = {"count": 0}

    def changing_state(project):
        calls["count"] += 1
        return {
            "novel_id": project.config.id,
            "version": calls["count"],
        }

    monkeypatch.setattr(plan, "capture_protected_state", changing_state)

    with pytest.raises(BenchmarkSafetyError, match="production-owned state changed"):
        _freeze(plan, data_repo_root, tmp_path / "unsafe-freeze")


def test_freeze_requires_complete_consecutive_corpus(monkeypatch, tmp_path):
    _, data_repo_root, fixtures = _install_fixture(monkeypatch, tmp_path)
    fixtures["rogue_first_prince"].chapters.pop(25)

    with pytest.raises(plan.ChronologicalPlanError, match="complete consecutive corpus 1-25"):
        _freeze(plan, data_repo_root, tmp_path / "incomplete-freeze")


def test_reference_glossary_excludes_future_and_unanchored_entries():
    entries = {
        "early": {"english": "Early", "first_chapter": 1},
        "edge": {"english": "Edge", "first_chapter": 25},
        "future": {"english": "Future", "first_chapter": 26},
        "unanchored": {"english": "Unknown"},
        "bad_bool": {"english": "Bool", "first_chapter": True},
    }
    selected, stats = plan._reference_glossary(entries, 25)

    assert list(selected) == ["early", "edge"]
    assert stats == {
        "eligible_reference_entries": 2,
        "anchored_future_entries_excluded": 1,
        "entries_without_valid_first_chapter_excluded": 2,
    }


def test_initial_state_contains_no_reference_knowledge():
    state = plan.initial_state("novel", "zh", "profile")
    assert state == {
        "schema_version": 1,
        "policy_version": "chronological-state-v1",
        "benchmark": plan.BENCHMARK_ID,
        "novel_id": "novel",
        "source_language": "zh",
        "profile_id": "profile",
        "through_chapter": 0,
        "glossary_entries": {},
        "continuity_summary": "",
        "issues": [],
        "parent_state_sha256": None,
        "state_events": [],
    }
