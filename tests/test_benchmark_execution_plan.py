import json
from pathlib import Path
import shutil

import pytest

from benchmark_execution_plan import (
    EXECUTION_BENCHMARK_ID,
    EXECUTION_SCHEMA_VERSION,
    ExecutionPlanError,
    NovelExecutionSpec,
    build_execution_manifest,
    write_execution_manifest,
)
from benchmark_novel import BenchmarkSafetyError, capture_protected_state, load_benchmark_inputs
from benchmark_sample_plan import plan_sample, write_plan
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number):
    text = (
        f"第{number}章 測試\n\n"
        + ("姚雲施展神通，眾人議論紛紛。" * (6 + number % 5))
        + "\n\n“你來了。”\n\n“是，我來了。”"
    )
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": 4,
        "character_count": len(text),
        "text": text,
    }


def _make_project(tmp_path, novel_id):
    config = NovelConfig(
        id=novel_id,
        title=novel_id,
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=80,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(novel_id, tmp_path)
    project.chapters_file.write_text(
        json.dumps(
            [_chapter(number) for number in range(1, 81)],
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
                    },
                    "神通": {
                        "english": "divine ability",
                        "category": "specialized recurring term",
                        "status": "provisional",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project


def _freeze_spec(tmp_path, novel_id):
    inputs = load_benchmark_inputs(novel_id, tmp_path)
    plan = plan_sample(inputs, sample_size=30, strata=10)
    path = write_plan(inputs, plan, "execution-test/sample-plan")
    import hashlib
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return NovelExecutionSpec(
        novel_id=novel_id,
        plan_path=path,
        expected_plan_sha256=sha,
        luna_batch_output_dir="execution-test/paid/luna-batch",
        deepseek_output_root="execution-test/paid/deepseek-pro",
    )


@pytest.fixture
def execution_fixture(tmp_path):
    _prepare_repo(tmp_path)
    first = _make_project(tmp_path, "exec_one")
    second = _make_project(tmp_path, "exec_two")
    spec_one = _freeze_spec(tmp_path, first.config.id)
    spec_two = _freeze_spec(tmp_path, second.config.id)
    return tmp_path, first, second, spec_one, spec_two


def test_build_execution_manifest_freezes_exact_120_request_topology(execution_fixture):
    root, first, second, spec_one, spec_two = execution_fixture
    before = {
        first.config.id: capture_protected_state(first),
        second.config.id: capture_protected_state(second),
    }

    manifest = build_execution_manifest(root, [spec_one, spec_two])

    assert manifest["schema_version"] == EXECUTION_SCHEMA_VERSION
    assert manifest["benchmark"] == EXECUTION_BENCHMARK_ID
    assert manifest["status"] == "frozen_zero_api"
    assert manifest["counts"] == {
        "novels": 2,
        "source_chapters": 60,
        "luna_batch_jobs": 2,
        "luna_requests": 60,
        "deepseek_requests": 60,
        "raw_translation_requests": 120,
    }
    assert manifest["authorization"]["provider_calls_performed"] == 0
    assert manifest["authorization"]["paid_execution_authorized"] is False
    assert manifest["authorization"]["automatic_retry_forbidden"] is True
    assert len(manifest["execution_id"]) == 64

    for novel in manifest["novels"]:
        assert len(novel["chapters"]) == 30
        assert len(set(novel["chapters"])) == 30
        assert novel["luna_batch"]["request_count"] == 30
        assert novel["deepseek"]["request_count"] == 30
        assert len(novel["luna_batch"]["requests"]) == 30
        assert len(novel["deepseek"]["targets"]) == 30
        assert all(
            target["output_dir"].endswith(
                f"unit-{target['chapter_number']:04d}"
            )
            for target in novel["deepseek"]["targets"]
        )

    assert capture_protected_state(first) == before[first.config.id]
    assert capture_protected_state(second) == before[second.config.id]


def test_execution_identity_is_deterministic_for_same_frozen_inputs(execution_fixture):
    root, _first, _second, spec_one, spec_two = execution_fixture

    first = build_execution_manifest(root, [spec_one, spec_two])
    second = build_execution_manifest(root, [spec_one, spec_two])

    assert first == second


def test_execution_manifest_rejects_plan_hash_drift(execution_fixture):
    root, _first, _second, spec_one, spec_two = execution_fixture

    bad = NovelExecutionSpec(
        novel_id=spec_one.novel_id,
        plan_path=spec_one.plan_path,
        expected_plan_sha256="0" * 64,
        luna_batch_output_dir=spec_one.luna_batch_output_dir,
        deepseek_output_root=spec_one.deepseek_output_root,
    )

    with pytest.raises(BenchmarkSafetyError, match="sample-plan SHA256 mismatch"):
        build_execution_manifest(root, [bad, spec_two])


def test_execution_manifest_rejects_production_state_drift(execution_fixture):
    root, first, _second, spec_one, spec_two = execution_fixture

    glossary = json.loads(first.glossary_file.read_text(encoding="utf-8"))
    glossary["entries"]["姚雲"]["english"] = "Different Rendering"
    first.glossary_file.write_text(
        json.dumps(glossary, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkSafetyError, match="state drifted"):
        build_execution_manifest(root, [spec_one, spec_two])


@pytest.mark.parametrize("which", ["luna", "deepseek"])
def test_execution_manifest_rejects_any_preused_paid_target(
    execution_fixture,
    which,
):
    root, first, _second, spec_one, spec_two = execution_fixture

    if which == "luna":
        target = (
            first.benchmark_dir
            / spec_one.luna_batch_output_dir
        )
    else:
        target = (
            first.benchmark_dir
            / spec_one.deepseek_output_root
        )
    target.mkdir(parents=True, exist_ok=False)

    with pytest.raises(BenchmarkSafetyError, match="target|root"):
        build_execution_manifest(root, [spec_one, spec_two])


def test_execution_manifest_requires_exactly_two_novels(execution_fixture):
    root, _first, _second, spec_one, _spec_two = execution_fixture

    with pytest.raises(ExecutionPlanError, match="exactly two"):
        build_execution_manifest(root, [spec_one])


def test_write_execution_manifest_is_immutable(execution_fixture):
    root, _first, _second, spec_one, spec_two = execution_fixture
    manifest = build_execution_manifest(root, [spec_one, spec_two])
    output = root / "external" / "execution_manifest.json"

    sha = write_execution_manifest(output, manifest)

    assert output.is_file()
    assert len(sha) == 64
    assert json.loads(output.read_text(encoding="utf-8")) == manifest

    with pytest.raises(BenchmarkSafetyError, match="already exists"):
        write_execution_manifest(output, manifest)
