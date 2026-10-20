import json
from pathlib import Path
import shutil

import pytest

from benchmark_novel import capture_protected_state, load_benchmark_inputs
from benchmark_sample_plan import (
    PLAN_FILENAME,
    PLAN_SCHEMA_VERSION,
    SamplePlanError,
    chapter_metrics,
    plan_sample,
    write_plan,
)
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number, text, *, unit_kind=None):
    item = {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": len([part for part in text.split("\n\n") if part]),
        "character_count": len(text),
        "text": text,
    }
    if unit_kind is not None:
        item["unit_kind"] = unit_kind
    return item


def _make_project(tmp_path, count=80):
    _prepare_repo(tmp_path)
    config = NovelConfig(
        id="planner_fixture",
        title="Planner Fixture",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=count,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)

    chapters = []
    for number in range(1, count + 1):
        if number % 4 == 0:
            text = (
                f"第{number}章 術法\n\n"
                + "姚雲施展神通。" * 18
                + "\n\n眾人震驚。"
            )
        elif number % 4 == 1:
            text = (
                f"第{number}章 對話\n\n"
                + "“姚雲，你來了。”\n\n“是，我來了。”\n\n" * 12
                + "眾人散去。"
            )
        elif number % 4 == 2:
            text = (
                f"第{number}章 平常\n\n"
                + "姚雲沿著道路前行，觀察四周的山川與城池。" * 8
            )
        else:
            text = (
                f"第{number}章 長章\n\n"
                + "遠方群山起伏，城中百姓議論紛紛。" * 22
            )
        unit_kind = "side_story" if number == 40 else None
        chapters.append(_chapter(number, text, unit_kind=unit_kind))

    project.chapters_file.write_text(
        json.dumps(chapters, ensure_ascii=False),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps(
            {
                "entries": {
                    "姚雲": {
                        "english": "Yao Yun",
                        "category": "character",
                        "status": "locked",
                        "strict_qa": True,
                    },
                    "神通": {
                        "english": "divine ability",
                        "category": "specialized recurring term",
                        "status": "locked",
                    },
                    "城池": {
                        "english": "fortified city",
                        "category": "location",
                        "status": "provisional",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project


def test_planner_selects_three_unique_roles_per_chronological_stratum(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)

    plan = plan_sample(inputs, sample_size=30, strata=10, excluded=(1, 5, 40))

    assert len(plan["selected_chapters"]) == 30
    assert len(set(plan["selected_chapters"])) == 30
    assert not ({1, 5, 40} & set(plan["selected_chapters"]))
    assert plan["eligible_ordinary_chapter_count"] == 77

    for stratum in range(1, 11):
        rows = [
            row for row in plan["selections"]
            if row["stratum"] == stratum
        ]
        assert len(rows) == 3
        assert {row["role"] for row in rows} == {
            "terminology_stress",
            "dialogue_stress",
            "representative",
        }
        assert len({row["chapter_number"] for row in rows}) == 3

    assert plan["schema_version"] == PLAN_SCHEMA_VERSION
    assert all(
        row["source_sha256"]
        and row["relevant_glossary_sha256"]
        and row["luna_request_sha256"]
        and row["deepseek_request_sha256"]
        for row in plan["selections"]
    )
    assert all(
        isinstance(row["effective_strict_relevant_terms"], int)
        and row["effective_strict_relevant_terms"] >= 0
        for row in plan["selections"]
    )
    assert any(
        row["effective_strict_relevant_terms"] > 0
        for row in plan["selections"]
    )
    assert all(
        (
            row["relevant_locked_terms"]
            + row["relevant_provisional_terms"]
            + row["relevant_contextual_terms"]
        )
        == row["distinct_relevant_glossary_terms"]
        for row in plan["selections"]
    )
    assert all(
        row["relevant_glossary_character_count"] >= 0
        and row["prompt_message_character_count"]
        > row["source_character_count"]
        for row in plan["selections"]
    )
    assert all(
        row["luna_request_sha256"] != row["deepseek_request_sha256"]
        for row in plan["selections"]
    )


def test_planner_is_deterministic_for_same_state_seed_and_exclusions(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)

    first = plan_sample(
        inputs,
        sample_size=30,
        strata=10,
        excluded=(1, 5, 40),
        seed="fixed-seed",
    )
    second = plan_sample(
        inputs,
        sample_size=30,
        strata=10,
        excluded=(1, 5, 40),
        seed="fixed-seed",
    )

    assert first == second


def test_planner_hash_binding_changes_when_source_or_glossary_changes(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    selected = 4
    original = chapter_metrics(inputs, selected, ordinal_position=4)

    chapters = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    chapters[selected - 1]["text"] += "新增一句。"
    project.chapters_file.write_text(
        json.dumps(chapters, ensure_ascii=False),
        encoding="utf-8",
    )
    changed_inputs = load_benchmark_inputs(project.config.id, tmp_path)
    changed_source = chapter_metrics(
        changed_inputs, selected, ordinal_position=4
    )

    assert original.source_sha256 != changed_source.source_sha256
    assert original.luna_request_sha256 != changed_source.luna_request_sha256

    glossary = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    glossary["entries"]["姚雲"]["english"] = "Yao Yun Prime"
    project.glossary_file.write_text(
        json.dumps(glossary, ensure_ascii=False),
        encoding="utf-8",
    )
    changed_glossary_inputs = load_benchmark_inputs(project.config.id, tmp_path)
    changed_glossary = chapter_metrics(
        changed_glossary_inputs, selected, ordinal_position=4
    )

    assert (
        changed_source.relevant_glossary_sha256
        != changed_glossary.relevant_glossary_sha256
    )
    assert (
        changed_source.luna_request_sha256
        != changed_glossary.luna_request_sha256
    )


def test_write_plan_changes_only_benchmark_tree_and_preserves_production_state(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    before = capture_protected_state(project)

    plan = plan_sample(inputs, sample_size=30, strata=10, excluded=(40,))
    path = write_plan(inputs, plan, "mature-v2/sample")

    assert path == project.benchmark_dir / "mature-v2/sample" / PLAN_FILENAME
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == plan
    assert capture_protected_state(project) == before

    with pytest.raises(FileExistsError):
        write_plan(inputs, plan, "mature-v2/sample")


def test_planner_requires_exactly_three_chapters_per_stratum(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)

    with pytest.raises(SamplePlanError, match="exactly three"):
        plan_sample(inputs, sample_size=20, strata=10)

    with pytest.raises(SamplePlanError, match="only"):
        plan_sample(inputs, sample_size=300, strata=100)
