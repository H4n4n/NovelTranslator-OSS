import json
from pathlib import Path
import shutil

import pytest

from benchmark_engine_finalize import (
    FINALIZATION_FILENAME,
    EngineFinalizationError,
    finalize_benchmark,
)
from benchmark_novel import (
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION, _canonical_json_sha256
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number, text):
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": len([part for part in text.split("\n\n") if part]),
        "character_count": len(text),
        "text": text,
    }


def _make_project(tmp_path):
    _prepare_repo(tmp_path)
    config = NovelConfig(
        id="finalize_fixture",
        title="Finalize Fixture",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=3,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)
    project.chapters_file.write_text(
        json.dumps(
            [
                _chapter(1, "姚雲施展鳴鶴拳。"),
                _chapter(2, "姚雲看見道。"),
                _chapter(3, "姚雲回到城中。"),
            ],
            ensure_ascii=False,
        ),
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
                        "auto_repair": True,
                        "repair_aliases": ["Yao-Yun"],
                    },
                    "鳴鶴拳": {
                        "english": "Minghequan (Calling Crane Boxing)",
                        "category": "technique",
                        "status": "locked",
                        "strict_qa": True,
                        "auto_repair": True,
                        "repair_aliases": [
                            "Minghequan",
                            "Calling Crane Boxing",
                        ],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project


def _plan(inputs, path):
    selected = [1, 2, 3]
    rows = []
    for stratum, chapter in enumerate(selected, start=1):
        rows.append(
            {
                "chapter_number": chapter,
                "stratum": stratum,
                "role": "representative",
                "luna_request_sha256": _canonical_json_sha256(
                    request_kwargs(inputs, chapter, "luna")
                ),
                "deepseek_request_sha256": _canonical_json_sha256(
                    request_kwargs(inputs, chapter, "deepseek-pro")
                ),
            }
        )
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "benchmark": "mature-glossary-engine-benchmark-v2",
        "novel_id": inputs.project.config.id,
        "selected_chapters": selected,
        "selections": rows,
        "protected_state_binding": capture_protected_state(inputs.project),
    }
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return plan


def _raw_results(inputs, raw_dir, provider="openai"):
    raw_dir.mkdir(parents=True, exist_ok=False)
    translations = {
        1: "Chapter 1\n\nYao-Yun used Minghequan.",
        2: "Chapter 2\n\nYao Yun saw 道.",
        3: "Chapter 3\nChapter 3\n\nYao Yun returned to the city.",
    }
    for chapter, text in translations.items():
        (raw_dir / f"chapter_{chapter:04d}.txt").write_text(
            text,
            encoding="utf-8",
        )
    model = "gpt-5.6-luna" if provider == "openai" else "deepseek-v4-pro"
    document = {
        "schema_version": 3,
        "status": "complete",
        "novel_id": inputs.project.config.id,
        "provider": provider,
        "model": model,
        "model_version": model,
        "chapters_requested": [1, 2, 3],
        "chapters": [
            {
                "chapter_number": chapter,
                "finish_reason": "stop",
                "remaining_cjk_character_count": (
                    1 if chapter == 2 else 0
                ),
            }
            for chapter in (1, 2, 3)
        ],
        "safety": {
            "protected_state_before": capture_protected_state(inputs.project),
            "protected_state_verified_unchanged": True,
        },
    }
    (raw_dir / "results.json").write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    return translations


def test_finalization_replays_zero_api_repairs_and_blocks_residual_script(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    plan_path = project.benchmark_dir / "plan.json"
    project.benchmark_dir.mkdir(parents=True, exist_ok=True)
    _plan(inputs, plan_path)
    raw_dir = project.benchmark_dir / "raw"
    raw = _raw_results(inputs, raw_dir)
    before = capture_protected_state(project)

    document, target = finalize_benchmark(
        inputs,
        plan_path=plan_path,
        raw_output_dir="raw",
        output_dir="finalized",
        expected_provider="openai",
    )

    assert document["provider"] == "openai"
    assert document["chapters_requested"] == [1, 2, 3]
    assert document["scope"]["model_assisted_residual_repair"] is False
    assert document["scope"]["glossary_candidate_review"] is False
    assert document["safety"]["protected_state_verified_unchanged"] is True
    assert capture_protected_state(project) == before

    by_chapter = {
        row["chapter_number"]: row for row in document["chapters"]
    }

    first = (target / "chapter_0001.txt").read_text(encoding="utf-8")
    assert "Minghequan (Calling Crane Boxing)" in first
    assert "Yao Yun" in first
    assert first != raw[1]
    assert by_chapter[1]["text_changed"] is True
    assert by_chapter[1]["zero_api_repairs_applied"] >= 1
    assert by_chapter[1]["deterministic_commit_allowed"] is True

    second = by_chapter[2]
    assert second["deterministic_commit_allowed"] is False
    assert second["would_require_model_assisted_residual_repair"] is True
    assert second["would_require_nonlocal_review"] is True
    assert "道" in (target / "chapter_0002.txt").read_text(encoding="utf-8")

    third = by_chapter[3]
    assert third["raw_duplicate_leading_heading_warning"] is True
    assert third["final_duplicate_leading_heading_warning"] is True
    assert (
        target / "chapter_0003.txt"
    ).read_text(encoding="utf-8") == raw[3]

    summary = document["summary"]
    assert summary["chapter_count"] == 3
    assert summary["chapters_requiring_model_assisted_residual_repair"] == 1
    assert summary["raw_duplicate_leading_heading_warnings"] == 1
    assert summary["final_duplicate_leading_heading_warnings"] == 1
    assert (target / FINALIZATION_FILENAME).is_file()


def test_finalization_rejects_provider_request_binding_drift(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    project.benchmark_dir.mkdir(parents=True, exist_ok=True)
    plan_path = project.benchmark_dir / "plan.json"
    _plan(inputs, plan_path)
    _raw_results(inputs, project.benchmark_dir / "raw")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["selections"][0]["luna_request_sha256"] = "0" * 64
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(BenchmarkSafetyError, match="request no longer matches"):
        finalize_benchmark(
            inputs,
            plan_path=plan_path,
            raw_output_dir="raw",
            output_dir="finalized",
            expected_provider="openai",
        )


def test_finalization_rejects_mature_glossary_state_drift(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    project.benchmark_dir.mkdir(parents=True, exist_ok=True)
    plan_path = project.benchmark_dir / "plan.json"
    _plan(inputs, plan_path)
    _raw_results(inputs, project.benchmark_dir / "raw")

    glossary = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    glossary["entries"]["姚雲"]["english"] = "Different Name"
    project.glossary_file.write_text(
        json.dumps(glossary, ensure_ascii=False),
        encoding="utf-8",
    )
    changed_inputs = load_benchmark_inputs(project.config.id, tmp_path)

    with pytest.raises(BenchmarkSafetyError, match="state drifted"):
        finalize_benchmark(
            changed_inputs,
            plan_path=plan_path,
            raw_output_dir="raw",
            output_dir="finalized",
            expected_provider="openai",
        )


def test_finalization_rejects_raw_provider_or_chapter_mismatch(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    project.benchmark_dir.mkdir(parents=True, exist_ok=True)
    plan_path = project.benchmark_dir / "plan.json"
    _plan(inputs, plan_path)
    raw_dir = project.benchmark_dir / "raw"
    _raw_results(inputs, raw_dir, provider="deepseek")

    with pytest.raises(EngineFinalizationError, match="does not match expected"):
        finalize_benchmark(
            inputs,
            plan_path=plan_path,
            raw_output_dir="raw",
            output_dir="finalized",
            expected_provider="openai",
        )

    result = json.loads((raw_dir / "results.json").read_text(encoding="utf-8"))
    result["provider"] = "openai"
    result["model"] = "gpt-5.6-luna"
    result["chapters_requested"] = [1, 3, 2]
    (raw_dir / "results.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(EngineFinalizationError, match="order/set"):
        finalize_benchmark(
            inputs,
            plan_path=plan_path,
            raw_output_dir="raw",
            output_dir="finalized",
            expected_provider="openai",
        )


def test_finalization_never_overwrites_existing_target(tmp_path):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    project.benchmark_dir.mkdir(parents=True, exist_ok=True)
    plan_path = project.benchmark_dir / "plan.json"
    _plan(inputs, plan_path)
    _raw_results(inputs, project.benchmark_dir / "raw")
    (project.benchmark_dir / "finalized").mkdir()

    with pytest.raises(BenchmarkSafetyError, match="already exists"):
        finalize_benchmark(
            inputs,
            plan_path=plan_path,
            raw_output_dir="raw",
            output_dir="finalized",
            expected_provider="openai",
        )
