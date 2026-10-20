"""Fail-closed recovery planning after an ambiguous paid benchmark request.

This module performs ZERO provider calls. It preserves the ambiguous target as a
permanent tombstone, verifies the successful prefix, deterministically chooses a
same-stratum/same-role replacement using only frozen source/glossary metadata,
and freezes a recovery manifest for the remaining paid work.

It never deletes or overwrites an original benchmark artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

from benchmark_novel import (
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    resolve_output_dir,
)
from benchmark_sample_plan import (
    DEFAULT_SEED,
    PLAN_SCHEMA_VERSION,
    _ordinary_chapters,
    _partition,
    _representative_key,
    chapter_metrics,
)


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_BENCHMARK_ID = "mature-glossary-ambiguous-recovery-v2"


class AmbiguousRecoveryError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AmbiguousRecoveryError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise AmbiguousRecoveryError(f"{label} must be a JSON object")
    return value


def _require_file_sha(path: Path, expected: str, label: str):
    path = Path(path)
    try:
        actual = _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise AmbiguousRecoveryError(f"{label} could not be read: {exc}") from exc
    if actual != expected:
        raise BenchmarkSafetyError(
            f"{label} SHA256 mismatch: {actual} != {expected}"
        )
    return actual


def _selection_row(plan, chapter_number: int):
    matches = [
        row
        for row in plan.get("selections", [])
        if isinstance(row, dict) and row.get("chapter_number") == chapter_number
    ]
    if len(matches) != 1:
        raise AmbiguousRecoveryError(
            f"expected exactly one sample row for Chapter {chapter_number}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _replacement_metric(inputs, plan, ambiguous_chapter: int):
    """Choose one unused same-stratum/same-role alternate deterministically."""
    row = _selection_row(plan, ambiguous_chapter)
    stratum = int(row["stratum"])
    role = row["role"]
    if role not in {"terminology_stress", "dialogue_stress", "representative"}:
        raise AmbiguousRecoveryError(f"unsupported sample role {role!r}")

    planner = plan.get("planner", {})
    strata = int(planner.get("strata", 10))
    seed = planner.get("seed", DEFAULT_SEED)
    excluded = {int(value) for value in planner.get("excluded_chapters", [])}
    ordinary = _ordinary_chapters(inputs, excluded)
    buckets = _partition(ordinary, strata)
    if stratum < 1 or stratum > len(buckets):
        raise AmbiguousRecoveryError(f"invalid frozen stratum {stratum}")

    bucket = buckets[stratum - 1]
    ordinal = {chapter: index + 1 for index, chapter in enumerate(ordinary)}
    selected = {int(value) for value in plan.get("selected_chapters", [])}
    candidates = [
        chapter_metrics(inputs, chapter, ordinal[chapter])
        for chapter in bucket
        if chapter not in selected
    ]
    if not candidates:
        raise AmbiguousRecoveryError(
            f"no unused Chapter remains in stratum {stratum} for recovery"
        )

    if role == "terminology_stress":
        metric = max(
            candidates,
            key=lambda item: (
                item.glossary_density_per_1000_chars,
                item.high_priority_relevant_terms,
                item.effective_strict_relevant_terms,
                item.source_character_count,
                -item.chapter_number,
            ),
        )
    elif role == "dialogue_stress":
        metric = max(
            candidates,
            key=lambda item: (
                item.dialogue_density_per_1000_chars,
                item.dialogue_marker_count,
                item.source_character_count,
                -item.chapter_number,
            ),
        )
    else:
        all_metrics = [
            chapter_metrics(inputs, chapter, ordinal[chapter])
            for chapter in bucket
        ]
        med_chars = statistics.median(
            item.source_character_count for item in all_metrics
        )
        med_glossary = statistics.median(
            item.glossary_density_per_1000_chars for item in all_metrics
        )
        metric = min(
            candidates,
            key=lambda item: _representative_key(
                item,
                med_chars,
                med_glossary,
                seed,
                inputs.project.config.id,
            ),
        )
    return row, metric


def _benchmark_target(project, relative: str) -> Path:
    return resolve_output_dir(project, relative)


def _verify_complete_target(project, target, expected):
    path = _benchmark_target(project, target["output_dir"])
    result_path = path / "results.json"
    if not result_path.is_file():
        raise AmbiguousRecoveryError(
            f"expected completed DeepSeek target is missing: {result_path}"
        )
    result = _load_json(result_path, "completed DeepSeek result")
    chapter = int(expected["chapter_number"])
    if result.get("status") != "complete":
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} is not complete"
        )
    if result.get("provider") != "deepseek" or result.get("model") != "deepseek-v4-pro":
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} provider/model identity mismatch"
        )
    if result.get("thinking") != "disabled":
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} thinking configuration changed"
        )
    if result.get("chapters_requested") != [chapter]:
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} requested-chapter identity mismatch"
        )
    rows = result.get("chapters")
    if not isinstance(rows, list) or len(rows) != 1:
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} result metadata is incomplete"
        )
    if rows[0].get("chapter_number") != chapter or rows[0].get("finish_reason") != "stop":
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} did not finish normally"
        )
    if rows[0].get("pricing_period") != "off_peak":
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} was not recorded as off_peak"
        )
    if not result.get("safety", {}).get("protected_state_verified_unchanged"):
        raise BenchmarkSafetyError(
            f"DeepSeek Chapter {chapter} did not verify protected-state preservation"
        )
    output = path / f"chapter_{chapter:04d}.txt"
    if not output.is_file() or not output.read_text(encoding="utf-8").strip():
        raise AmbiguousRecoveryError(
            f"DeepSeek Chapter {chapter} translated text is missing"
        )
    return {
        "chapter_number": chapter,
        "output_dir": target["output_dir"],
        "results_sha256": _sha256_bytes(result_path.read_bytes()),
        "translation_sha256": _sha256_bytes(output.read_bytes()),
        "input_tokens": result["totals"]["input_tokens"],
        "output_tokens": result["totals"]["output_tokens"],
        "cache_hit_tokens": result["totals"]["cache_hit_tokens"],
        "cost_usd": result["totals"]["total_cost_usd"],
    }


def _verify_ambiguous_target(project, target):
    chapter = int(target["chapter_number"])
    path = _benchmark_target(project, target["output_dir"])
    result_path = path / "results.json"
    if not result_path.is_file():
        raise AmbiguousRecoveryError(
            f"ambiguous DeepSeek tombstone is missing: {result_path}"
        )
    result = _load_json(result_path, "ambiguous DeepSeek result")
    if result.get("status") != "failed":
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} must have failed local status"
        )
    if result.get("provider") != "deepseek" or result.get("model") != "deepseek-v4-pro":
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} provider/model identity mismatch"
        )
    if result.get("chapters_requested") != [chapter]:
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} requested-chapter identity mismatch"
        )
    if result.get("chapters") not in ([], None):
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} unexpectedly contains a completed response row"
        )
    if not result.get("safety", {}).get("protected_state_verified_unchanged"):
        raise BenchmarkSafetyError(
            f"ambiguous Chapter {chapter} did not verify protected-state preservation"
        )
    error = result.get("error")
    if not isinstance(error, dict) or not error.get("type") or not error.get("message"):
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} lacks durable error evidence"
        )
    output = path / f"chapter_{chapter:04d}.txt"
    if output.exists():
        raise AmbiguousRecoveryError(
            f"ambiguous Chapter {chapter} unexpectedly has translated output"
        )
    return {
        "chapter_number": chapter,
        "output_dir": target["output_dir"],
        "results_sha256": _sha256_bytes(result_path.read_bytes()),
        "error": {
            "type": error["type"],
            "message": error["message"],
        },
        "translated_output_present": False,
        "retry_allowed": False,
    }


def _verify_prepared_batch(project, relative_dir: str):
    target = _benchmark_target(project, relative_dir)
    manifest_path = target / "batch_manifest.json"
    input_path = target / "batch_input.jsonl"
    if not manifest_path.is_file() or not input_path.is_file():
        raise AmbiguousRecoveryError(
            f"prepared Luna Batch artifacts are incomplete: {target}"
        )
    manifest = _load_json(manifest_path, "prepared Luna Batch manifest")
    if manifest.get("status") != "prepared":
        raise AmbiguousRecoveryError(
            f"Luna Batch {target} is no longer prepared-only"
        )
    if manifest.get("input_file_id") or manifest.get("batch_id"):
        raise AmbiguousRecoveryError(
            f"Luna Batch {target} already has remote identity"
        )
    if manifest.get("paid_batch_authorized") is not False:
        raise AmbiguousRecoveryError(
            f"Luna Batch {target} unexpectedly has paid authorization"
        )
    if manifest.get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError(
            f"Luna Batch {target} did not verify protected-state preservation"
        )
    if manifest.get("request_count") != 30 or len(manifest.get("chapters", [])) != 30:
        raise AmbiguousRecoveryError(
            f"Luna Batch {target} no longer contains exactly 30 frozen requests"
        )
    actual_input = _sha256_bytes(input_path.read_bytes())
    if manifest.get("input_sha256") != actual_input:
        raise BenchmarkSafetyError(
            f"Luna Batch input bytes do not match its manifest: {target}"
        )
    return {
        "output_dir": relative_dir,
        "run_id": manifest["run_id"],
        "input_sha256": actual_input,
        "request_count": manifest["request_count"],
        "chapters": manifest["chapters"],
        "status": "prepared",
        "remote_created": False,
    }


def build_recovery_manifest(
    repo_root: Path,
    *,
    execution_manifest_path: Path,
    execution_manifest_sha256: str,
    expected_execution_id: str,
    authorization_path: Path,
    authorization_sha256: str,
    ambiguous_novel: str,
    ambiguous_chapter: int,
    replacement_luna_output_dir: str,
    replacement_deepseek_output_root: str,
):
    repo_root = Path(repo_root)
    _require_file_sha(
        execution_manifest_path,
        execution_manifest_sha256,
        "original execution manifest",
    )
    execution = _load_json(execution_manifest_path, "original execution manifest")
    if execution.get("execution_id") != expected_execution_id:
        raise BenchmarkSafetyError("original execution ID mismatch")
    if execution.get("status") != "frozen_zero_api":
        raise AmbiguousRecoveryError("original execution manifest status changed")

    counts = execution.get("counts", {})
    if (
        counts.get("novels") != 2
        or counts.get("source_chapters") != 60
        or counts.get("luna_requests") != 60
        or counts.get("deepseek_requests") != 60
        or counts.get("raw_translation_requests") != 120
    ):
        raise AmbiguousRecoveryError(
            "original execution manifest no longer describes the exact 60-source / "
            "120-translation topology"
        )

    _require_file_sha(
        authorization_path,
        authorization_sha256,
        "paid authorization record",
    )
    authorization = _load_json(authorization_path, "paid authorization record")
    if authorization.get("execution_id") != expected_execution_id:
        raise BenchmarkSafetyError("paid authorization execution ID mismatch")
    if authorization.get("execution_manifest_sha256") != execution_manifest_sha256:
        raise BenchmarkSafetyError("paid authorization manifest binding mismatch")

    novels = execution.get("novels", [])
    if not isinstance(novels, list) or len(novels) != 2:
        raise AmbiguousRecoveryError(
            "original execution must contain exactly two novels"
        )
    global_targets = []
    inputs_by_novel = {}
    novel_by_id = {}
    for novel in novels:
        novel_id = novel["novel_id"]
        inputs = load_benchmark_inputs(novel_id, repo_root)
        inputs_by_novel[novel_id] = inputs
        novel_by_id[novel_id] = novel
        if novel.get("protected_state_binding") != capture_protected_state(inputs.project):
            raise BenchmarkSafetyError(
                f"{novel_id} production-owned state drifted after paid authorization"
            )
        for target in novel["deepseek"]["targets"]:
            global_targets.append((novel_id, target))

    if len(global_targets) != 60:
        raise AmbiguousRecoveryError(
            f"original execution contains {len(global_targets)} DeepSeek targets; expected 60"
        )

    matches = [
        (index, novel_id, target)
        for index, (novel_id, target) in enumerate(global_targets)
        if novel_id == ambiguous_novel
        and int(target["chapter_number"]) == int(ambiguous_chapter)
    ]
    if len(matches) != 1:
        raise AmbiguousRecoveryError(
            "ambiguous target is not uniquely present in original execution"
        )
    ambiguous_index, _, ambiguous_target = matches[0]

    completed = []
    for novel_id, target in global_targets[:ambiguous_index]:
        completed.append(
            {
                "novel_id": novel_id,
                **_verify_complete_target(
                    inputs_by_novel[novel_id].project,
                    target,
                    target,
                ),
            }
        )

    ambiguous = _verify_ambiguous_target(
        inputs_by_novel[ambiguous_novel].project,
        ambiguous_target,
    )

    untouched_remaining = []
    for novel_id, target in global_targets[ambiguous_index + 1 :]:
        path = _benchmark_target(
            inputs_by_novel[novel_id].project,
            target["output_dir"],
        )
        if path.exists():
            raise BenchmarkSafetyError(
                f"post-ambiguity DeepSeek target unexpectedly exists: {path}"
            )
        untouched_remaining.append(
            {
                "novel_id": novel_id,
                **target,
            }
        )

    ambiguous_execution = novel_by_id[ambiguous_novel]
    sample_plan_path = Path(ambiguous_execution["sample_plan"])
    plan = _load_json(sample_plan_path, "ambiguous novel sample plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise AmbiguousRecoveryError("sample-plan schema changed")
    if plan.get("protected_state_binding") != capture_protected_state(
        inputs_by_novel[ambiguous_novel].project
    ):
        raise BenchmarkSafetyError("sample-plan production binding drifted")

    frozen_row, replacement_metric = _replacement_metric(
        inputs_by_novel[ambiguous_novel],
        plan,
        int(ambiguous_chapter),
    )
    replacement_chapter = replacement_metric.chapter_number
    replacement_row = {
        "chapter_number": replacement_chapter,
        "stratum": int(frozen_row["stratum"]),
        "role": frozen_row["role"],
        **asdict(replacement_metric),
    }

    replacement_target_relative = (
        replacement_deepseek_output_root.rstrip("/")
        + f"/unit-{replacement_chapter:04d}"
    )
    replacement_target = _benchmark_target(
        inputs_by_novel[ambiguous_novel].project,
        replacement_target_relative,
    )
    if replacement_target.exists():
        raise BenchmarkSafetyError(
            f"replacement DeepSeek target already exists: {replacement_target}"
        )

    final_chapters = {}
    for novel in novels:
        chapters = [int(value) for value in novel["chapters"]]
        if novel["novel_id"] == ambiguous_novel:
            chapters = [
                replacement_chapter if value == int(ambiguous_chapter) else value
                for value in chapters
            ]
        if len(chapters) != 30 or len(set(chapters)) != 30:
            raise AmbiguousRecoveryError(
                f"{novel['novel_id']} recovery chapter set is not 30 unique sources"
            )
        final_chapters[novel["novel_id"]] = chapters

    batch_states = {}
    for novel in novels:
        batch_states[novel["novel_id"]] = _verify_prepared_batch(
            inputs_by_novel[novel["novel_id"]].project,
            novel["luna_batch"]["output_dir"],
        )

    for novel in novels:
        novel_id = novel["novel_id"]
        if batch_states[novel_id]["chapters"] != novel["chapters"]:
            raise BenchmarkSafetyError(
                f"original {novel_id} Luna Batch does not match original execution"
            )

    replacement_luna_target = _benchmark_target(
        inputs_by_novel[ambiguous_novel].project,
        replacement_luna_output_dir,
    )
    if replacement_luna_target.exists():
        raise BenchmarkSafetyError(
            f"replacement Luna Batch target already exists: {replacement_luna_target}"
        )

    recovery_identity = {
        "original_execution_id": expected_execution_id,
        "original_manifest_sha256": execution_manifest_sha256,
        "authorization_sha256": authorization_sha256,
        "ambiguous_novel": ambiguous_novel,
        "ambiguous_chapter": int(ambiguous_chapter),
        "ambiguous_results_sha256": ambiguous["results_sha256"],
        "replacement_chapter": replacement_chapter,
        "replacement_source_sha256": replacement_metric.source_sha256,
        "replacement_luna_request_sha256": replacement_metric.luna_request_sha256,
        "replacement_deepseek_request_sha256": replacement_metric.deepseek_request_sha256,
        "replacement_luna_output_dir": replacement_luna_output_dir,
        "replacement_deepseek_output_dir": replacement_target_relative,
        "final_chapters": final_chapters,
    }
    recovery_id = _sha256_bytes(
        json.dumps(
            recovery_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "benchmark": RECOVERY_BENCHMARK_ID,
        "status": "frozen_zero_api",
        "recovery_id": recovery_id,
        "original": {
            "execution_id": expected_execution_id,
            "execution_manifest": str(Path(execution_manifest_path)),
            "execution_manifest_sha256": execution_manifest_sha256,
            "paid_authorization": str(Path(authorization_path)),
            "paid_authorization_sha256": authorization_sha256,
        },
        "completed_deepseek_prefix": {
            "count": len(completed),
            "targets": completed,
            "total_cost_usd": sum(item["cost_usd"] for item in completed),
            "input_tokens": sum(item["input_tokens"] for item in completed),
            "output_tokens": sum(item["output_tokens"] for item in completed),
        },
        "ambiguous_deepseek": {
            "novel_id": ambiguous_novel,
            **ambiguous,
            "remote_outcome": "unprovable",
            "retry_allowed": False,
            "replacement_required_for_matched_sample": True,
        },
        "replacement": {
            "novel_id": ambiguous_novel,
            "retired_chapter": int(ambiguous_chapter),
            "chapter_number": replacement_chapter,
            "stratum": int(frozen_row["stratum"]),
            "role": frozen_row["role"],
            "source_sha256": replacement_metric.source_sha256,
            "relevant_glossary_sha256": replacement_metric.relevant_glossary_sha256,
            "luna_request_sha256": replacement_metric.luna_request_sha256,
            "deepseek_request_sha256": replacement_metric.deepseek_request_sha256,
            "deepseek_output_dir": replacement_target_relative,
        },
        "remaining_deepseek": {
            "original_untouched_count": len(untouched_remaining),
            "original_untouched_targets": untouched_remaining,
            "replacement_count": 1,
            "new_paid_request_ceiling": len(untouched_remaining) + 1,
        },
        "luna_batches": {
            "superseded_prepared_batch": {
                "novel_id": ambiguous_novel,
                **batch_states[ambiguous_novel],
                "submit_allowed": False,
                "delete_allowed": False,
            },
            "replacement_batch": {
                "novel_id": ambiguous_novel,
                "output_dir": replacement_luna_output_dir,
                "chapters": final_chapters[ambiguous_novel],
                "request_count": 30,
                "status": "not_prepared",
            },
            "unchanged_batches": [
                {
                    "novel_id": novel["novel_id"],
                    **batch_states[novel["novel_id"]],
                    "submit_allowed_after_recovery_gate": True,
                }
                for novel in novels
                if novel["novel_id"] != ambiguous_novel
            ],
        },
        "final_matched_sample": {
            "source_count": 60,
            "chapters_by_novel": final_chapters,
            "retired_ambiguous_sources": [
                {
                    "novel_id": ambiguous_novel,
                    "chapter_number": int(ambiguous_chapter),
                }
            ],
            "replacement_sources": [
                {
                    "novel_id": ambiguous_novel,
                    "chapter_number": replacement_chapter,
                }
            ],
        },
        "authorization": {
            "provider_calls_performed": 0,
            "new_paid_requests_performed": 0,
            "recovery_paid_execution_authorized": False,
            "ambiguous_target_retry_forbidden": True,
            "production_commit_allowed": False,
        },
    }


def write_recovery_manifest(path: Path, manifest):
    path = Path(path)
    if path.exists():
        raise BenchmarkSafetyError(
            f"recovery manifest already exists and was not overwritten: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_text(encoding="utf-8") != payload:
        raise BenchmarkSafetyError("recovery manifest write verification failed")
    return _sha256_bytes(path.read_bytes())


def build_parser():
    parser = argparse.ArgumentParser(
        description="Freeze zero-API recovery after an ambiguous mature-glossary request"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--execution-manifest", type=Path, required=True)
    parser.add_argument("--execution-manifest-sha256", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--paid-authorization", type=Path, required=True)
    parser.add_argument("--paid-authorization-sha256", required=True)
    parser.add_argument("--ambiguous-novel", required=True)
    parser.add_argument("--ambiguous-chapter", type=int, required=True)
    parser.add_argument("--replacement-luna-output-dir", required=True)
    parser.add_argument("--replacement-deepseek-output-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        manifest = build_recovery_manifest(
            args.repo_root,
            execution_manifest_path=args.execution_manifest,
            execution_manifest_sha256=args.execution_manifest_sha256,
            expected_execution_id=args.execution_id,
            authorization_path=args.paid_authorization,
            authorization_sha256=args.paid_authorization_sha256,
            ambiguous_novel=args.ambiguous_novel,
            ambiguous_chapter=args.ambiguous_chapter,
            replacement_luna_output_dir=args.replacement_luna_output_dir,
            replacement_deepseek_output_root=args.replacement_deepseek_output_root,
        )
        sha = write_recovery_manifest(args.output, manifest)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=" * 92)
    print("MATURE-GLOSSARY AMBIGUOUS-REQUEST RECOVERY — ZERO API")
    print("=" * 92)
    print(f"Recovery ID       : {manifest['recovery_id']}")
    print(
        "Completed DeepSeek: "
        f"{manifest['completed_deepseek_prefix']['count']}"
    )
    print(
        "Ambiguous retired : "
        f"{manifest['ambiguous_deepseek']['novel_id']} / "
        f"{manifest['ambiguous_deepseek']['chapter_number']}"
    )
    print(
        "Replacement       : "
        f"{manifest['replacement']['novel_id']} / "
        f"{manifest['replacement']['chapter_number']} "
        f"({manifest['replacement']['role']}, "
        f"stratum {manifest['replacement']['stratum']})"
    )
    print(
        "New DeepSeek max  : "
        f"{manifest['remaining_deepseek']['new_paid_request_ceiling']}"
    )
    print(f"Output            : {args.output}")
    print(f"SHA256            : {sha}")
    print("Provider calls made: 0")
    print("Ambiguous target retry allowed: NO")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
