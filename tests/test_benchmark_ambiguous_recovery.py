import hashlib
import json
from pathlib import Path
import shutil

import pytest

from benchmark_ambiguous_recovery import (
    RECOVERY_BENCHMARK_ID,
    RECOVERY_SCHEMA_VERSION,
    AmbiguousRecoveryError,
    build_recovery_manifest,
    write_recovery_manifest,
)
from benchmark_batch import prepare_batch
from benchmark_execution_plan import (
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
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": len([part for part in text.split("\n\n") if part]),
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
        final_chapter=100,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(novel_id, tmp_path)
    project.chapters_file.write_text(
        json.dumps(
            [_chapter(number) for number in range(1, 101)],
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


def _plan_and_spec(tmp_path, novel_id):
    inputs = load_benchmark_inputs(novel_id, tmp_path)
    plan = plan_sample(inputs, sample_size=30, strata=10)
    plan_path = write_plan(inputs, plan, "recovery-test/sample-plan")
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    return plan, NovelExecutionSpec(
        novel_id=novel_id,
        plan_path=plan_path,
        expected_plan_sha256=plan_sha,
        luna_batch_output_dir="recovery-test/paid/luna-batch",
        deepseek_output_root="recovery-test/paid/deepseek-pro",
    )


def _write_complete_target(project, target):
    chapter = int(target["chapter_number"])
    path = project.benchmark_dir / target["output_dir"]
    path.mkdir(parents=True, exist_ok=False)
    text = f"Chapter {chapter}\n\nYao Yun completed the benchmark."
    (path / f"chapter_{chapter:04d}.txt").write_text(text, encoding="utf-8")
    result = {
        "schema_version": 3,
        "status": "complete",
        "novel_id": project.config.id,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "model_version": "DeepSeek-V4-Pro-0813",
        "thinking": "disabled",
        "chapters_requested": [chapter],
        "chapters": [
            {
                "chapter_number": chapter,
                "finish_reason": "stop",
                "pricing_period": "off_peak",
            }
        ],
        "totals": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_hit_tokens": 128,
            "total_cost_usd": 0.001,
        },
        "safety": {
            "protected_state_before": capture_protected_state(project),
            "protected_state_verified_unchanged": True,
        },
    }
    (path / "results.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_ambiguous_target(project, target):
    chapter = int(target["chapter_number"])
    path = project.benchmark_dir / target["output_dir"]
    path.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 3,
        "status": "failed",
        "novel_id": project.config.id,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "model_version": "DeepSeek-V4-Pro-0813",
        "thinking": "disabled",
        "chapters_requested": [chapter],
        "chapters": [],
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit_tokens": 0,
            "total_cost_usd": 0.0,
        },
        "safety": {
            "protected_state_before": capture_protected_state(project),
            "protected_state_verified_unchanged": True,
        },
        "error": {
            "type": "APIConnectionError",
            "message": "Connection error.",
        },
    }
    (path / "results.json").write_text(
        json.dumps(result, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def recovery_fixture(tmp_path):
    _prepare_repo(tmp_path)
    first = _make_project(tmp_path, "recovery_one")
    second = _make_project(tmp_path, "recovery_two")
    first_plan, first_spec = _plan_and_spec(tmp_path, first.config.id)
    _second_plan, second_spec = _plan_and_spec(tmp_path, second.config.id)

    execution = build_execution_manifest(tmp_path, [first_spec, second_spec])
    execution_path = tmp_path / "external" / "execution_manifest.json"
    execution_sha = write_execution_manifest(execution_path, execution)

    authorization = {
        "schema_version": 1,
        "benchmark": "mature-glossary-paid-execution-v2",
        "execution_id": execution["execution_id"],
        "execution_manifest_sha256": execution_sha,
        "authorization_phrase": "RUN-MATURE-V2-PAID",
    }
    authorization_path = tmp_path / "external" / "paid_authorization.json"
    authorization_path.write_text(
        json.dumps(authorization, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    authorization_sha = hashlib.sha256(
        authorization_path.read_bytes()
    ).hexdigest()

    first_inputs = load_benchmark_inputs(first.config.id, tmp_path)
    second_inputs = load_benchmark_inputs(second.config.id, tmp_path)
    prepare_batch(
        first_inputs,
        execution["novels"][0]["chapters"],
        first_spec.luna_batch_output_dir,
    )
    prepare_batch(
        second_inputs,
        execution["novels"][1]["chapters"],
        second_spec.luna_batch_output_dir,
    )

    first_exec = execution["novels"][0]
    dialogue = next(
        row
        for row in first_plan["selections"]
        if row["stratum"] == 1 and row["role"] == "dialogue_stress"
    )
    ambiguous_chapter = dialogue["chapter_number"]
    targets = first_exec["deepseek"]["targets"]
    ambiguous_index = next(
        index
        for index, target in enumerate(targets)
        if target["chapter_number"] == ambiguous_chapter
    )

    for target in targets[:ambiguous_index]:
        _write_complete_target(first, target)

    ambiguous_target = targets[ambiguous_index]
    _write_ambiguous_target(first, ambiguous_target)

    return {
        "root": tmp_path,
        "first": first,
        "second": second,
        "plan": first_plan,
        "execution": execution,
        "execution_path": execution_path,
        "execution_sha": execution_sha,
        "authorization_path": authorization_path,
        "authorization_sha": authorization_sha,
        "ambiguous_chapter": ambiguous_chapter,
        "ambiguous_target": ambiguous_target,
        "completed_count": ambiguous_index,
    }


def _build(fixture):
    return build_recovery_manifest(
        fixture["root"],
        execution_manifest_path=fixture["execution_path"],
        execution_manifest_sha256=fixture["execution_sha"],
        expected_execution_id=fixture["execution"]["execution_id"],
        authorization_path=fixture["authorization_path"],
        authorization_sha256=fixture["authorization_sha"],
        ambiguous_novel=fixture["first"].config.id,
        ambiguous_chapter=fixture["ambiguous_chapter"],
        replacement_luna_output_dir="recovery-test/recovery1/luna-batch",
        replacement_deepseek_output_root="recovery-test/recovery1/deepseek-pro",
    )


def test_recovery_retires_ambiguous_target_and_selects_same_role_stratum_replacement(
    recovery_fixture,
):
    manifest = _build(recovery_fixture)

    assert manifest["schema_version"] == RECOVERY_SCHEMA_VERSION
    assert manifest["benchmark"] == RECOVERY_BENCHMARK_ID
    assert manifest["status"] == "frozen_zero_api"
    assert len(manifest["recovery_id"]) == 64

    ambiguous = manifest["ambiguous_deepseek"]
    assert ambiguous["chapter_number"] == recovery_fixture["ambiguous_chapter"]
    assert ambiguous["retry_allowed"] is False
    assert ambiguous["remote_outcome"] == "unprovable"
    assert ambiguous["translated_output_present"] is False
    assert ambiguous["error"]["type"] == "APIConnectionError"

    replacement = manifest["replacement"]
    original_row = next(
        row
        for row in recovery_fixture["plan"]["selections"]
        if row["chapter_number"] == recovery_fixture["ambiguous_chapter"]
    )
    assert replacement["chapter_number"] != recovery_fixture["ambiguous_chapter"]
    assert replacement["chapter_number"] not in recovery_fixture["plan"]["selected_chapters"]
    assert replacement["stratum"] == original_row["stratum"]
    assert replacement["role"] == original_row["role"] == "dialogue_stress"

    final = manifest["final_matched_sample"]["chapters_by_novel"][
        recovery_fixture["first"].config.id
    ]
    assert len(final) == 30
    assert len(set(final)) == 30
    assert recovery_fixture["ambiguous_chapter"] not in final
    assert replacement["chapter_number"] in final

    assert (
        manifest["completed_deepseek_prefix"]["count"]
        == recovery_fixture["completed_count"]
    )
    assert (
        manifest["remaining_deepseek"]["new_paid_request_ceiling"]
        == 60 - recovery_fixture["completed_count"]
    )
    assert manifest["authorization"]["provider_calls_performed"] == 0
    assert manifest["authorization"]["recovery_paid_execution_authorized"] is False


def test_recovery_is_deterministic_for_same_frozen_evidence(recovery_fixture):
    first = _build(recovery_fixture)
    second = _build(recovery_fixture)
    assert first == second


def test_recovery_preserves_original_prepared_batch_and_requires_new_replacement_batch(
    recovery_fixture,
):
    manifest = _build(recovery_fixture)
    batches = manifest["luna_batches"]

    superseded = batches["superseded_prepared_batch"]
    assert superseded["status"] == "prepared"
    assert superseded["remote_created"] is False
    assert superseded["submit_allowed"] is False
    assert superseded["delete_allowed"] is False
    assert superseded["request_count"] == 30

    replacement = batches["replacement_batch"]
    assert replacement["status"] == "not_prepared"
    assert replacement["request_count"] == 30

    unchanged = batches["unchanged_batches"]
    assert len(unchanged) == 1
    assert unchanged[0]["status"] == "prepared"
    assert unchanged[0]["submit_allowed_after_recovery_gate"] is True


def test_recovery_rejects_missing_ambiguous_tombstone(recovery_fixture):
    path = (
        recovery_fixture["first"].benchmark_dir
        / recovery_fixture["ambiguous_target"]["output_dir"]
        / "results.json"
    )
    path.unlink()

    with pytest.raises(AmbiguousRecoveryError, match="tombstone is missing"):
        _build(recovery_fixture)


def test_recovery_rejects_translated_output_at_ambiguous_boundary(recovery_fixture):
    chapter = recovery_fixture["ambiguous_chapter"]
    path = (
        recovery_fixture["first"].benchmark_dir
        / recovery_fixture["ambiguous_target"]["output_dir"]
        / f"chapter_{chapter:04d}.txt"
    )
    path.write_text("Unexpected output", encoding="utf-8")

    with pytest.raises(AmbiguousRecoveryError, match="unexpectedly has translated output"):
        _build(recovery_fixture)


def test_recovery_rejects_any_post_ambiguity_target_that_already_exists(
    recovery_fixture,
):
    first_exec = recovery_fixture["execution"]["novels"][0]
    targets = first_exec["deepseek"]["targets"]
    index = next(
        i
        for i, target in enumerate(targets)
        if target["chapter_number"] == recovery_fixture["ambiguous_chapter"]
    )
    next_target = targets[index + 1]
    path = recovery_fixture["first"].benchmark_dir / next_target["output_dir"]
    path.mkdir(parents=True)

    with pytest.raises(BenchmarkSafetyError, match="post-ambiguity"):
        _build(recovery_fixture)


def test_recovery_rejects_original_execution_or_authorization_hash_drift(
    recovery_fixture,
):
    with pytest.raises(BenchmarkSafetyError, match="execution manifest.*SHA256"):
        build_recovery_manifest(
            recovery_fixture["root"],
            execution_manifest_path=recovery_fixture["execution_path"],
            execution_manifest_sha256="0" * 64,
            expected_execution_id=recovery_fixture["execution"]["execution_id"],
            authorization_path=recovery_fixture["authorization_path"],
            authorization_sha256=recovery_fixture["authorization_sha"],
            ambiguous_novel=recovery_fixture["first"].config.id,
            ambiguous_chapter=recovery_fixture["ambiguous_chapter"],
            replacement_luna_output_dir="recovery-test/recovery1/luna-batch",
            replacement_deepseek_output_root="recovery-test/recovery1/deepseek-pro",
        )

    with pytest.raises(BenchmarkSafetyError, match="paid authorization record SHA256"):
        build_recovery_manifest(
            recovery_fixture["root"],
            execution_manifest_path=recovery_fixture["execution_path"],
            execution_manifest_sha256=recovery_fixture["execution_sha"],
            expected_execution_id=recovery_fixture["execution"]["execution_id"],
            authorization_path=recovery_fixture["authorization_path"],
            authorization_sha256="0" * 64,
            ambiguous_novel=recovery_fixture["first"].config.id,
            ambiguous_chapter=recovery_fixture["ambiguous_chapter"],
            replacement_luna_output_dir="recovery-test/recovery1/luna-batch",
            replacement_deepseek_output_root="recovery-test/recovery1/deepseek-pro",
        )


def test_write_recovery_manifest_is_immutable(recovery_fixture):
    manifest = _build(recovery_fixture)
    path = recovery_fixture["root"] / "external" / "recovery_manifest.json"

    sha = write_recovery_manifest(path, manifest)

    assert path.is_file()
    assert len(sha) == 64
    assert json.loads(path.read_text(encoding="utf-8")) == manifest

    with pytest.raises(BenchmarkSafetyError, match="already exists"):
        write_recovery_manifest(path, manifest)
