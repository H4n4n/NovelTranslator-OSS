"""One-call DeepSeek V4.1 Flash benchmark smoke execution.

Importing this module performs no credential loading, network activity, provider
calls, or file writes. The CLI defaults to a zero-provider preflight. Passing
``--execute-paid-smoke`` additionally requires an exact confirmation token,
claims one external immutable output directory, and performs exactly one
synchronous DeepSeek Chat Completions request.

A transport exception after the durable ``started`` event is an ambiguous paid
outcome: evidence is preserved as ``uncertain`` and retry is forbidden. Once a
provider response is durably recorded, later parsing or safety failures are
recorded separately as completed-response failures, never as transport
ambiguity.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from benchmark_model_config import (
    benchmark_pricing_snapshot,
    economy_pricing_snapshot,
    resolve_benchmark_model,
)
from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    _chapter_result,
    capture_protected_state,
    create_benchmark_client,
    load_benchmark_inputs,
    request_kwargs,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION, chapter_metrics


SMOKE_SCHEMA_VERSION = 1
SMOKE_KIND = "deepseek-v4.1-flash-one-call-smoke"
FLASH_SELECTOR = "deepseek-v4.1-flash"
EXPECTED_FLASH_API_MODEL = "deepseek-flash"
EXPECTED_FLASH_VERSION = "DeepSeek-V4.1-Flash"
EXPECTED_FLASH_THINKING = "disabled"
EXPECTED_FLASH_MAX_TOKENS = 12000
EXECUTE_CONFIRMATION = "AUTHORIZE_ONE_DEEPSEEK_V41_FLASH_PAID_REQUEST"


class FlashSmokeError(BenchmarkError):
    pass


class FlashSmokeUncertainError(FlashSmokeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_sha256(value) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _git_head(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FlashSmokeError(f"repository HEAD could not be resolved: {exc}") from exc
    value = result.stdout.strip()
    if len(value) != 40:
        raise FlashSmokeError(f"repository HEAD is malformed: {value!r}")
    return value


def _ensure_expected_repo_head(repo_root: Path, expected: str) -> str:
    expected = (expected or "").strip().lower()
    if len(expected) != 40 or any(ch not in "0123456789abcdef" for ch in expected):
        raise FlashSmokeError("--expected-repo-head must be an exact 40-character SHA")
    actual = _git_head(repo_root).lower()
    if actual != expected:
        raise BenchmarkSafetyError(f"repository HEAD drifted: {actual} != {expected}")
    return actual


def _ensure_external_output(repo_root: Path, output_dir: Path) -> Path:
    repo_root = Path(repo_root).resolve()
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        raise FlashSmokeError("--output-dir must be an absolute external path")
    output_dir = output_dir.resolve()
    if output_dir == repo_root or repo_root in output_dir.parents:
        raise BenchmarkSafetyError("Flash smoke output must remain outside the repository")
    if output_dir.exists():
        raise BenchmarkSafetyError(
            f"Flash smoke output already exists and is immutable: {output_dir}"
        )
    return output_dir


def _load_bound_row(
    inputs,
    chapter_number: int,
    plan_path: Path,
    expected_plan_sha256: str,
):
    plan_path = Path(plan_path)
    if not plan_path.is_file():
        raise FlashSmokeError(f"sample plan does not exist: {plan_path}")
    actual_plan_sha = _sha256_file(plan_path)
    expected_plan_sha256 = (expected_plan_sha256 or "").lower()
    if actual_plan_sha != expected_plan_sha256:
        raise BenchmarkSafetyError(
            f"sample-plan SHA256 mismatch: {actual_plan_sha} != {expected_plan_sha256}"
        )

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FlashSmokeError(f"sample plan could not be read: {exc}") from exc
    if not isinstance(plan, dict):
        raise FlashSmokeError("sample plan must be a JSON object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise FlashSmokeError(
            f"sample plan schema is {plan.get('schema_version')!r}; "
            f"expected {PLAN_SCHEMA_VERSION}"
        )
    if plan.get("benchmark") != "mature-glossary-engine-benchmark-v2":
        raise FlashSmokeError("sample plan belongs to another benchmark")
    if plan.get("novel_id") != inputs.project.config.id:
        raise FlashSmokeError("sample plan novel identity mismatch")
    if plan.get("protected_state_binding") != capture_protected_state(inputs.project):
        raise BenchmarkSafetyError(
            "production-owned source/glossary state drifted after sample freezing"
        )

    selected = plan.get("selected_chapters")
    rows = plan.get("selections")
    if not isinstance(selected, list) or chapter_number not in selected:
        raise FlashSmokeError(
            f"Chapter {chapter_number} is not in the frozen mature-glossary sample"
        )
    if not isinstance(rows, list):
        raise FlashSmokeError("sample plan selections are malformed")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("chapter_number") == chapter_number
    ]
    if len(matches) != 1:
        raise FlashSmokeError(
            f"sample plan must contain exactly one row for Chapter {chapter_number}"
        )
    row = matches[0]
    ordinal = row.get("ordinal_position")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise FlashSmokeError("sample plan row has invalid ordinal_position")

    metrics = chapter_metrics(inputs, chapter_number, ordinal)
    checks = {
        "source_sha256": metrics.source_sha256,
        "relevant_glossary_sha256": metrics.relevant_glossary_sha256,
        "deepseek_request_sha256": metrics.deepseek_request_sha256,
    }
    for key, actual in checks.items():
        if row.get(key) != actual:
            raise BenchmarkSafetyError(f"Chapter {chapter_number} frozen {key} drifted")
    return row, actual_plan_sha


def _without_model(request: dict) -> dict:
    value = copy.deepcopy(request)
    value.pop("model", None)
    return value


def build_smoke_plan(
    *,
    repo_root: Path,
    novel_id: str,
    chapter_number: int,
    plan_path: Path,
    expected_plan_sha256: str,
    output_dir: Path,
    expected_repo_head: str,
):
    inputs = load_benchmark_inputs(novel_id, repo_root)
    actual_head = _ensure_expected_repo_head(repo_root, expected_repo_head)
    output_dir = _ensure_external_output(repo_root, output_dir)
    row, plan_sha = _load_bound_row(
        inputs,
        chapter_number,
        plan_path,
        expected_plan_sha256,
    )

    config = resolve_benchmark_model(FLASH_SELECTOR)
    expected_config = {
        "provider": "deepseek",
        "model": EXPECTED_FLASH_API_MODEL,
        "version": EXPECTED_FLASH_VERSION,
        "reasoning": EXPECTED_FLASH_THINKING,
        "max_tokens": EXPECTED_FLASH_MAX_TOKENS,
    }
    actual_config = {
        "provider": config.provider,
        "model": config.model,
        "version": config.version,
        "reasoning": config.reasoning,
        "max_tokens": config.max_tokens,
    }
    if actual_config != expected_config:
        raise BenchmarkSafetyError(
            f"Flash benchmark configuration drifted: {actual_config!r}"
        )

    pro_request = request_kwargs(inputs, chapter_number, "deepseek-pro")
    flash_request = request_kwargs(inputs, chapter_number, config)
    pro_sha = _canonical_json_sha256(pro_request)
    flash_sha = _canonical_json_sha256(flash_request)
    if pro_sha != row.get("deepseek_request_sha256"):
        raise BenchmarkSafetyError(
            f"Chapter {chapter_number} Pro request no longer matches frozen plan"
        )
    if _without_model(pro_request) != _without_model(flash_request):
        raise BenchmarkSafetyError(
            "Flash request differs from frozen Pro request by more than model"
        )
    if flash_request.get("model") != EXPECTED_FLASH_API_MODEL:
        raise BenchmarkSafetyError("Flash request model literal is incorrect")
    if flash_request.get("max_tokens") != EXPECTED_FLASH_MAX_TOKENS:
        raise BenchmarkSafetyError("Flash request max_tokens is incorrect")
    if flash_request.get("extra_body") != {
        "thinking": {"type": EXPECTED_FLASH_THINKING}
    }:
        raise BenchmarkSafetyError("Flash request thinking payload is incorrect")

    protected_before = capture_protected_state(inputs.project)
    protected_before_sha = _canonical_json_sha256(protected_before)
    identity = {
        "kind": SMOKE_KIND,
        "repo_head": actual_head,
        "novel_id": novel_id,
        "chapter_number": chapter_number,
        "sample_plan_sha256": plan_sha,
        "source_sha256": row["source_sha256"],
        "relevant_glossary_sha256": row["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": row["deepseek_request_sha256"],
        "flash_request_sha256": flash_sha,
        "provider": config.provider,
        "api_model": config.model,
        "model_version": config.version,
        "thinking": config.reasoning,
        "max_tokens": config.max_tokens,
        "output_dir": str(output_dir),
        "protected_state_sha256": protected_before_sha,
    }
    return {
        "inputs": inputs,
        "config": config,
        "request": flash_request,
        "request_sha256": flash_sha,
        "pro_request_sha256": pro_sha,
        "row": row,
        "sample_plan_sha256": plan_sha,
        "protected_before": protected_before,
        "protected_before_sha256": protected_before_sha,
        "output_dir": output_dir,
        "repo_head": actual_head,
        "identity": identity,
        "identity_sha256": _canonical_json_sha256(identity),
    }


def _write_exclusive(path: Path, payload: bytes):
    try:
        with Path(path).open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise BenchmarkSafetyError(f"artifact already exists: {path}") from exc


def _write_json_exclusive(path: Path, value):
    _write_exclusive(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _append_journal(path: Path, event: dict):
    with Path(path).open("ab") as handle:
        handle.write(_canonical_json_bytes(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _provider_identity(response):
    choices = _field(response, "choices", None) or []
    first = choices[0] if choices else None
    return {
        "response_id": _field(response, "id", None),
        "response_model": _field(response, "model", None),
        "system_fingerprint": _field(response, "system_fingerprint", None),
        "finish_reason": _field(first, "finish_reason", None),
    }


def _usage_snapshot(response):
    usage = _field(response, "usage", None)
    result = {}
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        result[name] = int((_field(usage, name, 0) if usage is not None else 0) or 0)
    return result


def _perform_single_transport(client, request):
    """The only paid transport call in this module. Never add retries here."""
    return client.chat.completions.create(**request)


def execute_paid_smoke(plan, *, now=utc_now, timer=time.perf_counter):
    output_dir = plan["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=False)
    request_path = output_dir / "request.json"
    manifest_path = output_dir / "manifest.json"
    journal_path = output_dir / "journal.jsonl"
    translation_path = output_dir / "translation.txt"

    _write_exclusive(request_path, _canonical_json_bytes(plan["request"]))
    if _sha256_file(request_path) != plan["request_sha256"]:
        raise BenchmarkSafetyError("written Flash request bytes failed SHA256 verification")

    planned = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "kind": SMOKE_KIND,
        "status": "planned",
        "identity_sha256": plan["identity_sha256"],
        "identity": plan["identity"],
        "authorization": {
            "paid_requests_authorized": 1,
            "automatic_retry_forbidden": True,
            "ambiguous_outcome_retry_forbidden": True,
        },
        "artifacts": {
            "request_json": request_path.name,
            "request_sha256": plan["request_sha256"],
            "journal": journal_path.name,
        },
    }
    _write_json_exclusive(manifest_path, planned)
    _write_exclusive(journal_path, b"")
    _append_journal(
        journal_path,
        {
            "event": "planned",
            "at_utc": now().isoformat(),
            "identity_sha256": plan["identity_sha256"],
            "request_sha256": plan["request_sha256"],
        },
    )

    client = create_benchmark_client(plan["config"])
    request_started = now()
    pricing = benchmark_pricing_snapshot(plan["config"], request_started)
    economy_pricing = economy_pricing_snapshot(plan["config"], request_started)
    _append_journal(
        journal_path,
        {
            "event": "started",
            "at_utc": request_started.isoformat(),
            "attempt": 1,
            "api_model": EXPECTED_FLASH_API_MODEL,
            "request_sha256": plan["request_sha256"],
            "automatic_retry_forbidden": True,
        },
    )

    started = timer()
    try:
        response = _perform_single_transport(client, plan["request"])
        elapsed = timer() - started
    except BaseException as exc:
        elapsed = timer() - started
        uncertain = {
            **planned,
            "status": "uncertain",
            "elapsed_seconds": elapsed,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "authorization": {
                **planned["authorization"],
                "paid_requests_attempted": 1,
                "retry_forbidden": True,
            },
        }
        _append_journal(
            journal_path,
            {
                "event": "uncertain",
                "at_utc": now().isoformat(),
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "retry_forbidden": True,
            },
        )
        _write_json_exclusive(output_dir / "uncertain.json", uncertain)
        raise FlashSmokeUncertainError(
            "DeepSeek Flash smoke outcome is ambiguous; evidence was preserved "
            "and retry is forbidden"
        ) from exc

    provider_identity = _provider_identity(response)
    usage = _usage_snapshot(response)
    _append_journal(
        journal_path,
        {
            "event": "response_recorded",
            "at_utc": now().isoformat(),
            "elapsed_seconds": elapsed,
            **provider_identity,
            "usage": usage,
        },
    )

    try:
        chapter_number = plan["identity"]["chapter_number"]
        chapter = plan["inputs"].chapters[chapter_number]
        translation, result = _chapter_result(
            chapter,
            response,
            pricing,
            economy_pricing,
            elapsed,
            translation_path,
            plan["inputs"].project,
        )
        if not translation:
            raise FlashSmokeError("provider returned an empty translation")
        if not provider_identity["response_model"]:
            raise FlashSmokeError("provider response did not report a model identity")

        _write_exclusive(translation_path, translation.encode("utf-8"))
        translation_sha = _sha256_file(translation_path)
        protected_after = capture_protected_state(plan["inputs"].project)
        protected_after_sha = _canonical_json_sha256(protected_after)
        if protected_after != plan["protected_before"]:
            raise BenchmarkSafetyError(
                "protected production state changed during Flash smoke execution"
            )
    except BaseException as exc:
        protected_after = capture_protected_state(plan["inputs"].project)
        response_failed = {
            **planned,
            "status": "response_failed",
            "elapsed_seconds": elapsed,
            "provider_response": {**provider_identity, "usage": usage},
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "safety": {
                "provider_calls_attempted": 1,
                "paid_requests_attempted": 1,
                "provider_response_recorded": True,
                "protected_state_verified_unchanged": (
                    protected_after == plan["protected_before"]
                ),
                "retry_forbidden": True,
            },
        }
        _append_journal(
            journal_path,
            {
                "event": "response_failed",
                "at_utc": now().isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "retry_forbidden": True,
            },
        )
        _write_json_exclusive(output_dir / "response_failed.json", response_failed)
        raise FlashSmokeError(
            "DeepSeek returned a response, but smoke validation failed; "
            "evidence was preserved and retry is forbidden"
        ) from exc

    final = {
        **planned,
        "status": "complete",
        "request_started_utc": request_started.isoformat(),
        "response_recorded_utc": now().isoformat(),
        "elapsed_seconds": elapsed,
        "provider_response": {**provider_identity, "usage": usage},
        "result": result,
        "artifacts": {
            **planned["artifacts"],
            "translation": translation_path.name,
            "translation_sha256": translation_sha,
        },
        "safety": {
            "protected_state_before_sha256": plan["protected_before_sha256"],
            "protected_state_after_sha256": protected_after_sha,
            "protected_state_verified_unchanged": True,
            "provider_calls_attempted": 1,
            "paid_requests_attempted": 1,
            "provider_response_recorded": True,
            "automatic_retry_forbidden": True,
        },
    }
    _write_json_exclusive(output_dir / "complete.json", final)
    _append_journal(
        journal_path,
        {
            "event": "complete",
            "at_utc": now().isoformat(),
            "translation_sha256": translation_sha,
            "protected_state_verified_unchanged": True,
            "retry_forbidden": True,
        },
    )
    return final


def build_parser():
    parser = argparse.ArgumentParser(
        description="Prepare or execute one immutable DeepSeek V4.1 Flash smoke request"
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapter", required=True, type=int)
    parser.add_argument("--sample-plan", required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--expected-repo-head", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--execute-paid-smoke",
        action="store_true",
        help="perform exactly one paid DeepSeek request after all bindings pass",
    )
    parser.add_argument(
        "--confirmation",
        help="exact one-call authorization string required for paid execution",
    )
    return parser


def _print_plan(plan):
    row = plan["row"]
    print("=" * 70)
    print("DEEPSEEK V4.1 FLASH ONE-CALL SMOKE — ZERO-PROVIDER PREFLIGHT")
    print("=" * 70)
    print(f"Repository HEAD       : {plan['repo_head']}")
    print(f"Novel                 : {plan['identity']['novel_id']}")
    print(f"Chapter               : {plan['identity']['chapter_number']}")
    print(f"Sample role           : {row.get('role')}")
    print(f"Sample stratum        : {row.get('stratum')}")
    print(f"Sample-plan SHA256    : {plan['sample_plan_sha256']}")
    print(f"Source SHA256         : {row['source_sha256']}")
    print(f"Glossary SHA256       : {row['relevant_glossary_sha256']}")
    print(f"Frozen Pro request SHA: {plan['pro_request_sha256']}")
    print(f"Flash request SHA256  : {plan['request_sha256']}")
    print(f"Smoke identity SHA256 : {plan['identity_sha256']}")
    print(f"API model             : {plan['identity']['api_model']}")
    print(f"Model version         : {plan['identity']['model_version']}")
    print(f"Thinking              : {plan['identity']['thinking']}")
    print(f"Max tokens            : {plan['identity']['max_tokens']}")
    print(f"Output directory      : {plan['output_dir']}")
    print("Flash vs Pro payload  : IDENTICAL EXCEPT MODEL")
    print("Provider calls        : 0")
    print("Paid requests         : 0")
    print("=" * 70)


def _print_result(document, output_dir):
    response = document["provider_response"]
    result = document["result"]
    safety = document["safety"]
    print("=" * 70)
    print("DEEPSEEK V4.1 FLASH ONE-CALL SMOKE: COMPLETE")
    print("=" * 70)
    print(f"Provider response model : {response.get('response_model')}")
    print(f"System fingerprint      : {response.get('system_fingerprint')}")
    print(f"Finish reason           : {response.get('finish_reason')}")
    print(f"Prompt tokens           : {result['prompt_tokens']}")
    print(f"Completion tokens       : {result['completion_tokens']}")
    print(f"Cache hit tokens        : {result['cache_hit_tokens']}")
    print(f"Cache miss tokens       : {result['cache_miss_tokens']}")
    print(f"Elapsed seconds         : {document['elapsed_seconds']:.3f}")
    print(f"Measured cost USD       : {result['total_cost_usd']:.8f}")
    print(f"Residual CJK chars      : {result['remaining_cjk_character_count']}")
    print(f"Translation SHA256      : {document['artifacts']['translation_sha256']}")
    print(
        "Protected state unchanged: "
        f"{safety['protected_state_verified_unchanged']}"
    )
    print(f"Provider calls attempted: {safety['provider_calls_attempted']}")
    print(f"Paid requests attempted : {safety['paid_requests_attempted']}")
    print(f"Output directory        : {output_dir}")
    print("Automatic retry         : FORBIDDEN")
    print("=" * 70)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        repo_root = Path(args.repo_root).resolve()
        plan = build_smoke_plan(
            repo_root=repo_root,
            novel_id=args.novel,
            chapter_number=args.chapter,
            plan_path=Path(args.sample_plan),
            expected_plan_sha256=args.expected_plan_sha256,
            output_dir=Path(args.output_dir),
            expected_repo_head=args.expected_repo_head,
        )
        _print_plan(plan)
        if not args.execute_paid_smoke:
            return 0
        if args.confirmation != EXECUTE_CONFIRMATION:
            raise FlashSmokeError(
                "--execute-paid-smoke requires the exact one-call confirmation string"
            )
        document = execute_paid_smoke(plan)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_result(document, plan["output_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
