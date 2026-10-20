"""Restart-safe, non-committing GPT-5.6 Luna Batch benchmark.

This module is benchmark-only. Importing it performs no credential loading, file
writes, network activity, or provider calls.

Lifecycle:
  prepare   - zero API; freeze exact benchmark requests into JSONL + manifest
  submit    - explicit authorization; upload JSONL and create exactly one Batch
  reconcile - zero inference; adopt an exactly matching batch after uncertain submit
  status    - zero inference; retrieve and persist provider batch status
  collect   - zero inference; retrieve completed output and materialize results

Production progress, glossary, continuity, QA, audit history, committed chapters,
and pending translation state are protected and must remain byte-identical.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from benchmark_model_config import (
    economy_pricing_snapshot,
    resolve_benchmark_model,
)
from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    RESULTS_FILENAME,
    _chapter_result,
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
    resolve_output_dir,
    result_totals,
    results_document,
    validate_chapter_selection,
)
from noveltranslator_core import atomic_write_json, atomic_write_text
from production_run_lock import production_run_lock


BATCH_SCHEMA_VERSION = 1
BATCH_MANIFEST_FILENAME = "batch_manifest.json"
BATCH_INPUT_FILENAME = "batch_input.jsonl"
BATCH_OUTPUT_FILENAME = "batch_output.jsonl"
BATCH_ERROR_FILENAME = "batch_error.jsonl"
BATCH_ENDPOINT = "/v1/chat/completions"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_MODEL = "gpt-5.6-luna"
SUBMIT_AUTHORIZATION = "RUN-LUNA-BATCH-V1"
TERMINAL_BATCH_STATUSES = {
    "completed",
    "failed",
    "expired",
    "cancelled",
}


class BatchBenchmarkError(BenchmarkError):
    pass


class BatchSubmissionUncertain(BatchBenchmarkError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment.astimezone(timezone.utc).isoformat()
    return str(moment)


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text):
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file_bytes(path):
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise BatchBenchmarkError(f"cannot hash Batch file {path}: {exc}") from exc


def _write_manifest(path, manifest):
    atomic_write_json(path, manifest)


def _read_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchBenchmarkError(f"batch manifest could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchBenchmarkError("batch manifest must be a JSON object")
    if value.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise BatchBenchmarkError("unsupported batch manifest schema version")
    return value


def _manifest_dir(project, output_dir):
    return resolve_output_dir(project, output_dir)


def _provider_attr(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _provider_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return copy.deepcopy(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    result = {}
    for name in (
        "id",
        "status",
        "input_file_id",
        "output_file_id",
        "error_file_id",
        "created_at",
        "completed_at",
        "expires_at",
        "request_counts",
        "metadata",
        "endpoint",
        "completion_window",
        "model",
    ):
        attr = getattr(value, name, None)
        if attr is not None:
            result[name] = _provider_dict(attr) if name in {"request_counts", "metadata"} else attr
    return result


def _request_counts(batch):
    counts = _provider_attr(batch, "request_counts")
    if counts is None:
        return {"total": 0, "completed": 0, "failed": 0}
    return {
        "total": int(_provider_attr(counts, "total", 0) or 0),
        "completed": int(_provider_attr(counts, "completed", 0) or 0),
        "failed": int(_provider_attr(counts, "failed", 0) or 0),
    }


def _batch_snapshot(batch):
    return {
        "id": _provider_attr(batch, "id"),
        "status": _provider_attr(batch, "status"),
        "endpoint": _provider_attr(batch, "endpoint"),
        "input_file_id": _provider_attr(batch, "input_file_id"),
        "output_file_id": _provider_attr(batch, "output_file_id"),
        "error_file_id": _provider_attr(batch, "error_file_id"),
        "completion_window": _provider_attr(batch, "completion_window"),
        "created_at": _provider_attr(batch, "created_at"),
        "completed_at": _provider_attr(batch, "completed_at"),
        "expires_at": _provider_attr(batch, "expires_at"),
        "request_counts": _request_counts(batch),
        "metadata": _provider_dict(_provider_attr(batch, "metadata")),
    }


def _ensure_protected_state(project, expected, stage):
    current = capture_protected_state(project)
    if current != expected:
        raise BenchmarkSafetyError(
            f"protected production state changed during Batch benchmark: {stage}"
        )
    return current


def _custom_id(novel_id, chapter_number, body):
    request_hash = _sha256_bytes(_canonical_json_bytes(body))
    return f"{novel_id}-ch{chapter_number:04d}-{request_hash[:16]}"


def prepare_batch(inputs, chapter_numbers, output_dir=None, now=utc_now):
    """Freeze exact Luna requests into a new immutable benchmark target."""
    config = resolve_benchmark_model(BATCH_MODEL)
    if config.provider != "openai":
        raise BatchBenchmarkError("Batch benchmark is restricted to OpenAI Luna")

    chapter_numbers = validate_chapter_selection(inputs, chapter_numbers)
    output_dir = _manifest_dir(inputs.project, output_dir)
    if output_dir.exists():
        raise BenchmarkSafetyError(
            f"batch benchmark target already exists and was not overwritten: {output_dir}"
        )

    before = capture_protected_state(inputs.project)
    output_dir.mkdir(parents=True, exist_ok=False)

    lines = []
    request_records = []
    seen_custom_ids = set()
    for chapter_number in chapter_numbers:
        body = request_kwargs(inputs, chapter_number, config)
        custom_id = _custom_id(inputs.project.config.id, chapter_number, body)
        if custom_id in seen_custom_ids:
            raise BatchBenchmarkError(f"duplicate deterministic custom_id: {custom_id}")
        seen_custom_ids.add(custom_id)
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": body,
        }
        lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        chapter = inputs.chapters[chapter_number]
        request_records.append(
            {
                "chapter_number": chapter_number,
                "custom_id": custom_id,
                "request_sha256": _sha256_bytes(_canonical_json_bytes(line)),
                "source_sha256": _sha256_text(chapter["text"]),
            }
        )

    input_text = "\n".join(lines) + "\n"
    input_path = output_dir / BATCH_INPUT_FILENAME
    atomic_write_text(input_path, input_text)
    input_sha = _sha256_file_bytes(input_path)
    prepared_at = now()
    run_identity = {
        "novel_id": inputs.project.config.id,
        "model": config.model,
        "chapters": list(chapter_numbers),
        "input_sha256": input_sha,
        "protected_state": before,
    }
    run_id = _sha256_bytes(_canonical_json_bytes(run_identity))
    manifest = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "benchmark": "luna-batch-parity-v1",
        "status": "prepared",
        "novel_id": inputs.project.config.id,
        "profile_id": inputs.profile.id,
        "source_language": inputs.project.config.source_language,
        "model": config.model,
        "model_version": config.version,
        "reasoning": config.reasoning,
        "endpoint": BATCH_ENDPOINT,
        "completion_window": BATCH_COMPLETION_WINDOW,
        "chapters": list(chapter_numbers),
        "requests": request_records,
        "request_count": len(request_records),
        "run_id": run_id,
        "prepared_at_utc": _iso(prepared_at),
        "input_file": BATCH_INPUT_FILENAME,
        "input_sha256": input_sha,
        "input_file_id": None,
        "batch_id": None,
        "provider_batch": None,
        "protected_state_before": before,
        "protected_state_verified_unchanged": False,
        "paid_batch_authorized": False,
    }
    _write_manifest(output_dir / BATCH_MANIFEST_FILENAME, manifest)
    _ensure_protected_state(inputs.project, before, "prepare")
    manifest["protected_state_verified_unchanged"] = True
    _write_manifest(output_dir / BATCH_MANIFEST_FILENAME, manifest)
    return manifest, output_dir


def create_openai_client():
    """Load the OpenAI credential only for an explicitly invoked remote command."""
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise BatchBenchmarkError("OPENAI_API_KEY was not found in .env")
    return OpenAI(api_key=api_key, max_retries=0)


def _load_context(novel_id, output_dir, repo_root=None):
    inputs = load_benchmark_inputs(novel_id, repo_root)
    target = _manifest_dir(inputs.project, output_dir)
    manifest = _read_manifest(target / BATCH_MANIFEST_FILENAME)
    if manifest.get("novel_id") != inputs.project.config.id:
        raise BatchBenchmarkError("batch manifest novel identity mismatch")
    expected = manifest.get("protected_state_before")
    if not isinstance(expected, dict):
        raise BatchBenchmarkError("batch manifest has no protected-state binding")
    _ensure_protected_state(inputs.project, expected, "context load")
    input_path = target / BATCH_INPUT_FILENAME
    if not input_path.is_file():
        raise BatchBenchmarkError(f"batch input file is missing: {input_path}")
    if _sha256_file_bytes(input_path) != manifest.get("input_sha256"):
        raise BatchBenchmarkError("batch input JSONL byte hash no longer matches manifest")
    return inputs, target, manifest


def submit_batch(inputs, output_dir, client, authorization):
    """Upload and create exactly one remote Batch, failing closed on ambiguity."""
    target = _manifest_dir(inputs.project, output_dir)
    manifest = _read_manifest(target / BATCH_MANIFEST_FILENAME)
    expected = manifest["protected_state_before"]
    _ensure_protected_state(inputs.project, expected, "before submit")

    if authorization != SUBMIT_AUTHORIZATION:
        raise BatchBenchmarkError("paid Batch submission was not explicitly authorized")
    if manifest.get("status") not in {"prepared", "input_uploaded"}:
        raise BatchBenchmarkError(
            "Batch submission is not eligible from manifest status "
            f"{manifest.get('status')!r}; automatic reissue is blocked"
        )
    manifest["paid_batch_authorized"] = True

    input_path = target / BATCH_INPUT_FILENAME
    if manifest["status"] == "prepared":
        manifest["status"] = "upload_started"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        try:
            with input_path.open("rb") as handle:
                uploaded = client.files.create(file=handle, purpose="batch")
        except Exception as exc:
            manifest["status"] = "upload_uncertain"
            manifest["last_error"] = f"{type(exc).__name__}: {exc}"
            _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
            raise BatchSubmissionUncertain(
                "Batch input upload outcome is uncertain; do not rerun submit automatically"
            ) from exc
        file_id = _provider_attr(uploaded, "id")
        if not isinstance(file_id, str) or not file_id:
            manifest["status"] = "upload_uncertain"
            _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
            raise BatchSubmissionUncertain("Batch input upload returned no file ID")
        manifest["input_file_id"] = file_id
        manifest["status"] = "input_uploaded"
        manifest["uploaded_file"] = _provider_dict(uploaded)
        manifest.pop("last_error", None)
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)

    metadata = {
        "benchmark": "luna-batch-parity-v1",
        "run_id": manifest["run_id"],
        "novel_id": manifest["novel_id"],
    }
    manifest["status"] = "submission_started"
    manifest["submission_metadata"] = metadata
    _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
    try:
        batch = client.batches.create(
            input_file_id=manifest["input_file_id"],
            endpoint=BATCH_ENDPOINT,
            completion_window=BATCH_COMPLETION_WINDOW,
            metadata=metadata,
        )
    except Exception as exc:
        manifest["status"] = "submission_uncertain"
        manifest["last_error"] = f"{type(exc).__name__}: {exc}"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        raise BatchSubmissionUncertain(
            "remote Batch creation outcome is uncertain; do not reissue automatically"
        ) from exc

    batch_id = _provider_attr(batch, "id")
    if not isinstance(batch_id, str) or not batch_id:
        manifest["status"] = "submission_uncertain"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        raise BatchSubmissionUncertain("remote Batch creation returned no batch ID")

    manifest["batch_id"] = batch_id
    manifest["status"] = "submitted"
    manifest["provider_batch"] = _batch_snapshot(batch)
    manifest.pop("last_error", None)
    _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
    _ensure_protected_state(inputs.project, expected, "after submit")
    return manifest


def _iter_page_data(page):
    data = _provider_attr(page, "data", [])
    return list(data or [])


def _listed_batches(client, *, max_pages=20):
    """Yield a bounded provider Batch listing without assuming output order."""
    page = client.batches.list(limit=100)
    for _ in range(max_pages):
        for batch in _iter_page_data(page):
            yield batch
        has_next = getattr(page, "has_next_page", None)
        get_next = getattr(page, "get_next_page", None)
        if not callable(has_next) or not has_next():
            return
        if not callable(get_next):
            raise BatchSubmissionUncertain(
                "provider Batch listing reports another page but exposes no paginator"
            )
        page = get_next()
    raise BatchSubmissionUncertain(
        "provider Batch reconciliation exceeded the bounded listing horizon"
    )


def reconcile_submission(inputs, output_dir, client):
    """Adopt one exact provider batch after an ambiguous create boundary."""
    target = _manifest_dir(inputs.project, output_dir)
    manifest = _read_manifest(target / BATCH_MANIFEST_FILENAME)
    if manifest.get("status") not in {"submission_started", "submission_uncertain"}:
        raise BatchBenchmarkError(
            "reconcile requires submission_started or submission_uncertain status"
        )
    input_file_id = manifest.get("input_file_id")
    if not input_file_id:
        raise BatchBenchmarkError("cannot reconcile without a durable input_file_id")

    matches = []
    for batch in _listed_batches(client):
        metadata = _provider_dict(_provider_attr(batch, "metadata"))
        if (
            _provider_attr(batch, "input_file_id") == input_file_id
            and _provider_attr(batch, "endpoint") == BATCH_ENDPOINT
            and _provider_attr(batch, "completion_window") == BATCH_COMPLETION_WINDOW
            and metadata.get("run_id") == manifest.get("run_id")
            and metadata.get("benchmark") == "luna-batch-parity-v1"
            and metadata.get("novel_id") == manifest.get("novel_id")
        ):
            matches.append(batch)
    if len(matches) != 1:
        raise BatchSubmissionUncertain(
            "submission reconciliation did not prove exactly one matching Batch; "
            f"matches={len(matches)}"
        )
    batch = matches[0]
    manifest["batch_id"] = _provider_attr(batch, "id")
    manifest["status"] = "submitted"
    manifest["provider_batch"] = _batch_snapshot(batch)
    manifest.pop("last_error", None)
    _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
    _ensure_protected_state(
        inputs.project, manifest["protected_state_before"], "after reconcile"
    )
    return manifest


def refresh_status(inputs, output_dir, client):
    target = _manifest_dir(inputs.project, output_dir)
    manifest = _read_manifest(target / BATCH_MANIFEST_FILENAME)
    batch_id = manifest.get("batch_id")
    if not batch_id:
        raise BatchBenchmarkError("batch manifest has no durable batch_id")
    batch = client.batches.retrieve(batch_id)
    snapshot = _batch_snapshot(batch)
    manifest["provider_batch"] = snapshot
    provider_status = snapshot.get("status")
    manifest["status"] = (
        "batch_completed"
        if provider_status == "completed"
        else f"batch_{provider_status or 'unknown'}"
    )
    _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
    _ensure_protected_state(
        inputs.project, manifest["protected_state_before"], "status refresh"
    )
    return manifest


def _file_content_text(response):
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    read = getattr(response, "read", None)
    if callable(read):
        payload = read()
        if isinstance(payload, bytes):
            return payload.decode("utf-8")
        if isinstance(payload, str):
            return payload
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8")
    if isinstance(content, str):
        return content
    raise BatchBenchmarkError("provider file-content response could not be decoded")


def _parse_jsonl(text, label):
    rows = []
    for index, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BatchBenchmarkError(
                f"{label} line {index} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise BatchBenchmarkError(f"{label} line {index} is not a JSON object")
        rows.append(row)
    return rows


def _write_or_verify_text(path, text, label):
    """Materialize immutable provider-derived text, or prove an exact prior write."""
    path = Path(path)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchmarkSafetyError(f"{label} could not be read: {path}: {exc}") from exc
        if existing != text:
            raise BenchmarkSafetyError(
                f"{label} already exists with different bytes: {path}"
            )
        return "verified"
    atomic_write_text(path, text)
    try:
        recorded = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkSafetyError(f"{label} could not be verified: {path}: {exc}") from exc
    if recorded != text:
        raise BenchmarkSafetyError(f"{label} verification failed: {path}")
    return "written"


def _verify_existing_results(path, document):
    """Accept a crash-resume results file only when immutable identities agree."""
    try:
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkSafetyError(f"existing Batch results could not be read: {exc}") from exc
    for key in (
        "schema_version",
        "status",
        "novel_id",
        "profile_id",
        "provider",
        "model",
        "model_version",
        "chapters_requested",
    ):
        if existing.get(key) != document.get(key):
            raise BenchmarkSafetyError(
                f"existing Batch results identity mismatch for {key}"
            )
    if existing.get("transport", {}).get("batch_id") != document.get("transport", {}).get("batch_id"):
        raise BenchmarkSafetyError("existing Batch results batch_id mismatch")
    if existing.get("transport", {}).get("input_sha256") != document.get("transport", {}).get("input_sha256"):
        raise BenchmarkSafetyError("existing Batch results input hash mismatch")
    existing_chapters = existing.get("chapters")
    expected_chapters = document.get("chapters")
    if not isinstance(existing_chapters, list) or len(existing_chapters) != len(expected_chapters):
        raise BenchmarkSafetyError("existing Batch results chapter count mismatch")
    for prior, expected in zip(existing_chapters, expected_chapters):
        for key in (
            "chapter_number",
            "prompt_tokens",
            "completion_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "finish_reason",
            "batch_custom_id",
            "batch_request_id",
            "transport",
        ):
            if prior.get(key) != expected.get(key):
                raise BenchmarkSafetyError(
                    f"existing Batch results Chapter {expected.get('chapter_number')} "
                    f"identity mismatch for {key}"
                )
    if existing.get("safety", {}).get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError("existing Batch results do not prove protected-state safety")
    return existing


def _response_namespace(body):
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise BatchBenchmarkError("Batch response body contains no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise BatchBenchmarkError("Batch response choice is malformed")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise BatchBenchmarkError("Batch response message is malformed")
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=message.get("content") or ""),
                finish_reason=choice.get("finish_reason"),
            )
        ],
        usage=body.get("usage") or {},
    )


def _batch_pricing(config, moment):
    pricing = copy.deepcopy(economy_pricing_snapshot(config, moment))
    pricing["pricing_mode"] = "batch_actual"
    return pricing


def collect_batch(inputs, output_dir, client, now=utc_now):
    target = _manifest_dir(inputs.project, output_dir)
    manifest = _read_manifest(target / BATCH_MANIFEST_FILENAME)
    expected_state = manifest["protected_state_before"]
    _ensure_protected_state(inputs.project, expected_state, "before collect")

    batch_id = manifest.get("batch_id")
    if not batch_id:
        raise BatchBenchmarkError("batch manifest has no durable batch_id")
    batch = client.batches.retrieve(batch_id)
    snapshot = _batch_snapshot(batch)
    manifest["provider_batch"] = snapshot
    provider_status = snapshot.get("status")
    counts = snapshot.get("request_counts", {})
    if provider_status != "completed":
        manifest["status"] = f"batch_{provider_status or 'unknown'}"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        raise BatchBenchmarkError(
            f"Batch is not complete; provider status={provider_status!r}"
        )
    if counts.get("failed", 0):
        manifest["status"] = "batch_completed_with_failures"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        raise BatchBenchmarkError(
            "Batch completed with failed requests; automatic reissue is blocked"
        )
    expected_count = int(manifest.get("request_count", 0) or 0)
    if (
        counts.get("total") != expected_count
        or counts.get("completed") != expected_count
    ):
        manifest["status"] = "batch_completed_count_mismatch"
        _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
        raise BatchBenchmarkError(
            "completed Batch request counts do not exactly match the frozen manifest"
        )

    output_file_id = snapshot.get("output_file_id")
    if not output_file_id:
        raise BatchBenchmarkError("completed Batch has no output_file_id")
    output_text = _file_content_text(client.files.content(output_file_id))
    _write_or_verify_text(
        target / BATCH_OUTPUT_FILENAME,
        output_text,
        "Batch raw output",
    )
    output_rows = _parse_jsonl(output_text, "Batch output")

    error_file_id = snapshot.get("error_file_id")
    if error_file_id:
        error_text = _file_content_text(client.files.content(error_file_id))
        _write_or_verify_text(
            target / BATCH_ERROR_FILENAME,
            error_text,
            "Batch raw error output",
        )
        if _parse_jsonl(error_text, "Batch error"):
            manifest["status"] = "batch_completed_with_errors"
            _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
            raise BatchBenchmarkError(
                "Batch error file contains request errors; automatic reissue is blocked"
            )

    expected_by_id = {
        item["custom_id"]: item for item in manifest.get("requests", [])
    }
    actual_by_id = {}
    for row in output_rows:
        custom_id = row.get("custom_id")
        if custom_id not in expected_by_id:
            raise BatchBenchmarkError(f"unexpected Batch custom_id: {custom_id!r}")
        if custom_id in actual_by_id:
            raise BatchBenchmarkError(f"duplicate Batch custom_id: {custom_id!r}")
        actual_by_id[custom_id] = row
    missing = sorted(set(expected_by_id) - set(actual_by_id))
    if missing:
        raise BatchBenchmarkError(
            "completed Batch output is missing expected custom_ids: " + ", ".join(missing)
        )

    config = resolve_benchmark_model(BATCH_MODEL)
    created_at = snapshot.get("created_at")
    if isinstance(created_at, (int, float)):
        request_moment = datetime.fromtimestamp(created_at, tz=timezone.utc)
    else:
        request_moment = now()
    pricing = _batch_pricing(config, request_moment)

    results_path = target / RESULTS_FILENAME

    # Validate every provider row and build every chapter result in memory
    # before materializing any chapter file. This makes semantic/shape failure
    # fail closed without leaving a partial translated chapter set.
    prepared_outputs = []
    results = []
    for request_record in manifest["requests"]:
        chapter_number = request_record["chapter_number"]
        row = actual_by_id[request_record["custom_id"]]
        error = row.get("error")
        if error:
            raise BatchBenchmarkError(
                f"Batch request {request_record['custom_id']} returned error: {error}"
            )
        response = row.get("response")
        if not isinstance(response, dict) or response.get("status_code") != 200:
            raise BatchBenchmarkError(
                f"Batch request {request_record['custom_id']} did not return HTTP 200"
            )
        body = response.get("body")
        if not isinstance(body, dict):
            raise BatchBenchmarkError("Batch response body is missing or malformed")
        output_path = target / f"chapter_{chapter_number:04d}.txt"
        translation, result = _chapter_result(
            inputs.chapters[chapter_number],
            _response_namespace(body),
            pricing,
            pricing,
            0.0,
            output_path,
            inputs.project,
        )
        if result["finish_reason"] != "stop":
            raise BatchBenchmarkError(
                f"Batch Chapter {chapter_number} finish_reason is "
                f"{result['finish_reason']!r}, not 'stop'"
            )
        result["batch_custom_id"] = request_record["custom_id"]
        result["batch_request_id"] = row.get("id")
        result["transport"] = "openai_batch"
        prepared_outputs.append((output_path, translation))
        results.append(result)

    after = capture_protected_state(inputs.project)
    unchanged = after == expected_state
    document = results_document(
        inputs,
        manifest["chapters"],
        results,
        expected_state,
        config,
        verified=unchanged,
    )
    document["transport"] = {
        "type": "openai_batch",
        "batch_id": batch_id,
        "input_file_id": manifest.get("input_file_id"),
        "output_file_id": output_file_id,
        "completion_window": BATCH_COMPLETION_WINDOW,
        "provider_batch": snapshot,
        "input_sha256": manifest["input_sha256"],
        "run_id": manifest["run_id"],
    }
    document["totals"] = result_totals(results)
    if not unchanged:
        document["status"] = "failed"
        document["error"] = {
            "type": "BenchmarkSafetyError",
            "message": "protected production state changed during Batch collection",
        }
        raise BenchmarkSafetyError(
            "protected production state changed during Batch collection"
        )

    # All provider rows are now proven valid. Materialization is idempotent:
    # an interrupted prior collection may leave exact files, which are verified
    # rather than overwritten or treated as a reason to rebill/re-fetch inference.
    for output_path, translation in prepared_outputs:
        _write_or_verify_text(
            output_path,
            translation,
            f"Batch Chapter {output_path.stem}",
        )

    if results_path.exists():
        document = _verify_existing_results(results_path, document)
    else:
        atomic_write_json(results_path, document)
        _verify_existing_results(results_path, document)

    manifest["status"] = "collected"
    manifest["protected_state_verified_unchanged"] = True
    manifest["results_file"] = RESULTS_FILENAME
    manifest["output_sha256"] = _sha256_text(output_text)
    _write_manifest(target / BATCH_MANIFEST_FILENAME, manifest)
    return document, manifest


def build_parser():
    parser = argparse.ArgumentParser(
        description="Restart-safe, non-committing GPT-5.6 Luna Batch benchmark"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--novel", required=True)
    prepare.add_argument("--chapters", required=True, nargs="+", type=int)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--repo-root", type=Path, default=None)

    for name in ("submit", "reconcile", "status", "collect"):
        command = sub.add_parser(name)
        command.add_argument("--novel", required=True)
        command.add_argument("--output-dir", required=True)
        command.add_argument("--repo-root", type=Path, default=None)
        if name == "submit":
            command.add_argument("--authorize-paid-batch", default="")

    return parser


def _print_manifest_summary(manifest, target):
    print("=" * 72)
    print("GPT-5.6 LUNA BATCH BENCHMARK")
    print("=" * 72)
    print(f"Status       : {manifest.get('status')}")
    print(f"Novel        : {manifest.get('novel_id')}")
    print(f"Model        : {manifest.get('model')}")
    print(f"Chapters     : {', '.join(str(x) for x in manifest.get('chapters', []))}")
    print(f"Request count: {manifest.get('request_count')}")
    print(f"Run ID       : {manifest.get('run_id')}")
    print(f"Input file ID: {manifest.get('input_file_id')}")
    print(f"Batch ID     : {manifest.get('batch_id')}")
    provider = manifest.get("provider_batch") or {}
    print(f"Provider stat: {provider.get('status')}")
    print(f"Target       : {target}")
    print("Production progress/accounting updated: NO")
    print("=" * 72)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        inputs = load_benchmark_inputs(args.novel, args.repo_root)
        target = _manifest_dir(inputs.project, args.output_dir)
        with production_run_lock(inputs.project.root):
            if args.command == "prepare":
                manifest, target = prepare_batch(
                    inputs,
                    args.chapters,
                    output_dir=args.output_dir,
                )
            else:
                inputs, target, manifest = _load_context(
                    args.novel, args.output_dir, args.repo_root
                )
                client = create_openai_client()
                if args.command == "submit":
                    manifest = submit_batch(
                        inputs,
                        args.output_dir,
                        client,
                        args.authorize_paid_batch,
                    )
                elif args.command == "reconcile":
                    manifest = reconcile_submission(
                        inputs, args.output_dir, client
                    )
                elif args.command == "status":
                    manifest = refresh_status(inputs, args.output_dir, client)
                elif args.command == "collect":
                    document, manifest = collect_batch(
                        inputs, args.output_dir, client
                    )
                    print(
                        "Collected input/output tokens: "
                        f"{document['totals']['input_tokens']:,} / "
                        f"{document['totals']['output_tokens']:,}"
                    )
                else:
                    raise BatchBenchmarkError("unsupported command")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _print_manifest_summary(manifest, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
