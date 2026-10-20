"""Zero-API execution manifest for the mature-glossary paid benchmark.

This module does not submit Batch jobs, create provider clients, or translate
anything. It freezes the already-reviewed schema-v2 sample plans into one exact
paid-execution manifest and proves every paid target is unused.

The resulting manifest is intentionally external to production-owned novel
state. Later operator gates use it to prepare exactly two Luna Batch jobs and
sixty one-chapter DeepSeek targets without widening authorization.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

from benchmark_novel import (
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    resolve_output_dir,
)
from benchmark_sample_plan import (
    PLAN_SCHEMA_VERSION,
    chapter_metrics,
)
from chapter_library import UNIT_KIND_CHAPTER, production_unit_kind


EXECUTION_SCHEMA_VERSION = 1
EXECUTION_BENCHMARK_ID = "mature-glossary-paid-execution-v2"


class ExecutionPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class NovelExecutionSpec:
    novel_id: str
    plan_path: Path
    expected_plan_sha256: str
    luna_batch_output_dir: str
    deepseek_output_root: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_json(path: Path, label: str):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionPlanError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionPlanError(f"{label} must be a JSON object")
    return value


def _verify_plan(inputs, spec: NovelExecutionSpec):
    plan_path = Path(spec.plan_path)
    try:
        payload = plan_path.read_bytes()
    except OSError as exc:
        raise ExecutionPlanError(
            f"{spec.novel_id} sample plan could not be read: {exc}"
        ) from exc
    actual_sha = _sha256_bytes(payload)
    if actual_sha != spec.expected_plan_sha256:
        raise BenchmarkSafetyError(
            f"{spec.novel_id} sample-plan SHA256 mismatch: "
            f"{actual_sha} != {spec.expected_plan_sha256}"
        )
    plan = _load_json(plan_path, f"{spec.novel_id} sample plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ExecutionPlanError(
            f"{spec.novel_id} plan schema is {plan.get('schema_version')!r}; "
            f"expected {PLAN_SCHEMA_VERSION}"
        )
    if plan.get("benchmark") != "mature-glossary-engine-benchmark-v2":
        raise ExecutionPlanError(f"{spec.novel_id} plan belongs to another benchmark")
    if plan.get("novel_id") != inputs.project.config.id:
        raise ExecutionPlanError(f"{spec.novel_id} plan novel identity mismatch")
    if plan.get("protected_state_binding") != capture_protected_state(inputs.project):
        raise BenchmarkSafetyError(
            f"{spec.novel_id} production-owned source/glossary state drifted "
            "after sample freezing"
        )

    selected = plan.get("selected_chapters")
    rows = plan.get("selections")
    if (
        not isinstance(selected, list)
        or len(selected) != 30
        or len(set(selected)) != 30
    ):
        raise ExecutionPlanError(
            f"{spec.novel_id} plan must contain exactly 30 unique chapters"
        )
    if not isinstance(rows, list) or len(rows) != 30:
        raise ExecutionPlanError(f"{spec.novel_id} plan selection rows are incomplete")

    by_chapter = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ExecutionPlanError(f"{spec.novel_id} plan has malformed selection row")
        number = row.get("chapter_number")
        if number in by_chapter:
            raise ExecutionPlanError(f"{spec.novel_id} plan duplicates Chapter {number}")
        by_chapter[number] = row

    if set(by_chapter) != set(selected):
        raise ExecutionPlanError(
            f"{spec.novel_id} plan row chapter set differs from selected_chapters"
        )

    frozen_rows = []
    for number in selected:
        if number not in inputs.chapters:
            raise ExecutionPlanError(
                f"{spec.novel_id} Chapter {number} is missing from current source library"
            )
        if production_unit_kind(inputs.chapters[number]) != UNIT_KIND_CHAPTER:
            raise ExecutionPlanError(
                f"{spec.novel_id} Chapter {number} is no longer an ordinary chapter"
            )
        row = by_chapter[number]
        actual = chapter_metrics(
            inputs,
            number,
            int(row.get("ordinal_position", 0)),
        )
        checks = {
            "source_sha256": actual.source_sha256,
            "relevant_glossary_sha256": actual.relevant_glossary_sha256,
            "luna_request_sha256": actual.luna_request_sha256,
            "deepseek_request_sha256": actual.deepseek_request_sha256,
        }
        for key, expected in checks.items():
            if row.get(key) != expected:
                raise BenchmarkSafetyError(
                    f"{spec.novel_id} Chapter {number} frozen {key} drifted"
                )
        frozen_rows.append(
            {
                "chapter_number": number,
                "stratum": int(row["stratum"]),
                "role": row["role"],
                **checks,
            }
        )

    return plan, actual_sha, frozen_rows


def build_execution_manifest(repo_root: Path, specs):
    repo_root = Path(repo_root)
    if len(specs) != 2:
        raise ExecutionPlanError("v2 paid benchmark requires exactly two novel specs")
    if len({spec.novel_id for spec in specs}) != len(specs):
        raise ExecutionPlanError("duplicate novel execution spec")

    before = {}
    novel_documents = []
    total_luna = 0
    total_deepseek = 0

    for spec in specs:
        inputs = load_benchmark_inputs(spec.novel_id, repo_root)
        project = inputs.project
        before[spec.novel_id] = capture_protected_state(project)
        plan, plan_sha, frozen_rows = _verify_plan(inputs, spec)

        luna_target = resolve_output_dir(project, spec.luna_batch_output_dir)
        if luna_target.exists():
            raise BenchmarkSafetyError(
                f"{spec.novel_id} Luna Batch target already exists: {luna_target}"
            )

        deepseek_root = resolve_output_dir(project, spec.deepseek_output_root)
        if deepseek_root.exists():
            raise BenchmarkSafetyError(
                f"{spec.novel_id} DeepSeek benchmark root already exists: {deepseek_root}"
            )

        deepseek_targets = []
        for row in frozen_rows:
            number = row["chapter_number"]
            relative = (
                f"{spec.deepseek_output_root.rstrip('/')}/unit-{number:04d}"
            )
            target = resolve_output_dir(project, relative)
            if target.exists():
                raise BenchmarkSafetyError(
                    f"{spec.novel_id} DeepSeek Chapter {number} target already exists: "
                    f"{target}"
                )
            deepseek_targets.append(
                {
                    "chapter_number": number,
                    "output_dir": relative,
                    "request_sha256": row["deepseek_request_sha256"],
                    "source_sha256": row["source_sha256"],
                    "stratum": row["stratum"],
                    "role": row["role"],
                }
            )

        luna_requests = [
            {
                "chapter_number": row["chapter_number"],
                "request_sha256": row["luna_request_sha256"],
                "source_sha256": row["source_sha256"],
                "stratum": row["stratum"],
                "role": row["role"],
            }
            for row in frozen_rows
        ]

        total_luna += len(luna_requests)
        total_deepseek += len(deepseek_targets)

        novel_documents.append(
            {
                "novel_id": spec.novel_id,
                "profile_id": plan["profile_id"],
                "source_language": plan["source_language"],
                "sample_plan": str(Path(spec.plan_path)),
                "sample_plan_sha256": plan_sha,
                "chapters": list(plan["selected_chapters"]),
                "luna_batch": {
                    "model": "gpt-5.6-luna",
                    "reasoning": "none",
                    "output_dir": spec.luna_batch_output_dir,
                    "request_count": len(luna_requests),
                    "requests": luna_requests,
                },
                "deepseek": {
                    "model": "deepseek-v4-pro",
                    "thinking": "disabled",
                    "output_root": spec.deepseek_output_root,
                    "request_count": len(deepseek_targets),
                    "targets": deepseek_targets,
                },
                "protected_state_binding": before[spec.novel_id],
            }
        )

    if total_luna != 60 or total_deepseek != 60:
        raise ExecutionPlanError(
            f"expected 60 Luna + 60 DeepSeek requests; got "
            f"{total_luna} + {total_deepseek}"
        )

    for spec in specs:
        current = capture_protected_state(
            load_benchmark_inputs(spec.novel_id, repo_root).project
        )
        if current != before[spec.novel_id]:
            raise BenchmarkSafetyError(
                f"{spec.novel_id} production-owned state changed during execution planning"
            )

    identity = {
        "benchmark": EXECUTION_BENCHMARK_ID,
        "novels": [
            {
                "novel_id": item["novel_id"],
                "sample_plan_sha256": item["sample_plan_sha256"],
                "chapters": item["chapters"],
                "luna_batch_output_dir": item["luna_batch"]["output_dir"],
                "deepseek_output_root": item["deepseek"]["output_root"],
                "protected_state_binding": item["protected_state_binding"],
            }
            for item in novel_documents
        ],
    }
    execution_id = _sha256_bytes(_canonical_json_bytes(identity))
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "benchmark": EXECUTION_BENCHMARK_ID,
        "status": "frozen_zero_api",
        "execution_id": execution_id,
        "counts": {
            "novels": 2,
            "source_chapters": 60,
            "luna_batch_jobs": 2,
            "luna_requests": 60,
            "deepseek_requests": 60,
            "raw_translation_requests": 120,
        },
        "authorization": {
            "provider_calls_performed": 0,
            "paid_requests_performed": 0,
            "paid_execution_authorized": False,
            "automatic_retry_forbidden": True,
            "production_commit_allowed": False,
        },
        "novels": novel_documents,
    }


def write_execution_manifest(path: Path, manifest):
    path = Path(path)
    if path.exists():
        raise BenchmarkSafetyError(
            f"execution manifest already exists and was not overwritten: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    path.write_text(payload, encoding="utf-8")
    reread = path.read_text(encoding="utf-8")
    if reread != payload:
        raise BenchmarkSafetyError("execution manifest write verification failed")
    return _sha256_bytes(path.read_bytes())


def _parse_spec(raw: str) -> NovelExecutionSpec:
    parts = raw.split("|")
    if len(parts) != 5:
        raise argparse.ArgumentTypeError(
            "--spec must be NOVEL|PLAN_PATH|PLAN_SHA256|LUNA_OUTPUT|DEEPSEEK_ROOT"
        )
    novel_id, plan_path, plan_sha, luna_output, deepseek_root = parts
    if not all(parts):
        raise argparse.ArgumentTypeError("--spec fields must be non-empty")
    if len(plan_sha) != 64:
        raise argparse.ArgumentTypeError("--spec PLAN_SHA256 must be 64 hex characters")
    try:
        int(plan_sha, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--spec PLAN_SHA256 must be hexadecimal"
        ) from exc
    return NovelExecutionSpec(
        novel_id=novel_id,
        plan_path=Path(plan_path),
        expected_plan_sha256=plan_sha.lower(),
        luna_batch_output_dir=luna_output,
        deepseek_output_root=deepseek_root,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Freeze a zero-API paid execution manifest for the 60-source study"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--spec", action="append", type=_parse_spec, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        manifest = build_execution_manifest(args.repo_root, args.spec)
        sha = write_execution_manifest(args.output, manifest)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=" * 88)
    print("MATURE-GLOSSARY PAID EXECUTION MANIFEST — ZERO API")
    print("=" * 88)
    print(f"Execution ID : {manifest['execution_id']}")
    print(f"Sources      : {manifest['counts']['source_chapters']}")
    print(f"Luna jobs    : {manifest['counts']['luna_batch_jobs']}")
    print(f"Luna requests: {manifest['counts']['luna_requests']}")
    print(f"DeepSeek req : {manifest['counts']['deepseek_requests']}")
    print(f"Output       : {args.output}")
    print(f"SHA256       : {sha}")
    print("Provider calls made: 0")
    print("Paid execution authorized: NO")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
