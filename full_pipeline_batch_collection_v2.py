"""Restart-safe collection protocol for completed full-pipeline Batch waves.

Submission and paid-boundary semantics remain owned by ``full_pipeline_batch_wave``.
This module strengthens only the *completed output collection* side with durable
sub-states so a process crash never requires blind refetch/rewrite of provider
evidence that was already safely recorded.

Lifecycle:

    ... -> collection_started -> collection_raw_recorded
        -> collection_results_recorded -> collected

A crash after a raw/results checkpoint is recovered entirely from local bytes.
The only intentionally re-downloaded case is the narrow ambiguity where an
atomic local provider file exists but its hash checkpoint was not durably
written.  In that case the remote file is fetched only to compare exact text;
the existing local file is never overwritten.

Importing this module performs no filesystem writes, credential loading, or
provider calls.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Mapping

import full_pipeline_batch_wave as base


COLLECTION_PROTOCOL_VERSION = "full-pipeline-batch-collection-v2"
STATUS_STARTED = "collection_started"
STATUS_RAW_RECORDED = "collection_raw_recorded"
STATUS_RESULTS_RECORDED = "collection_results_recorded"
STATUS_PROVIDER_ERRORS = "collection_provider_errors"


class FullPipelineBatchCollectionV2Error(base.FullPipelineBatchError):
    """Collection-v2 evidence is inconsistent or cannot be safely resumed."""


def _hook(
    callback: Callable[[str, Mapping[str, Any]], None] | None,
    stage: str,
    manifest: Mapping[str, Any],
) -> None:
    if callback is not None:
        callback(stage, copy.deepcopy(dict(manifest)))


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise base.FullPipelineBatchSafetyError(f"{label} must be non-empty")
    return value


def _require_collection_protocol(manifest: Mapping[str, Any]) -> None:
    if manifest.get("collection_protocol") != COLLECTION_PROTOCOL_VERSION:
        raise base.FullPipelineBatchSafetyError(
            "Batch wave collection state does not use collection protocol v2"
        )


def _verify_provider_snapshot(manifest: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = manifest.get("provider_batch")
    if not isinstance(snapshot, Mapping):
        raise base.FullPipelineBatchSafetyError(
            "collection state lacks durable provider Batch snapshot"
        )
    snapshot = copy.deepcopy(dict(snapshot))
    batch_id = _require_text(manifest.get("batch_id"), "batch_id")
    if snapshot.get("id") not in {None, batch_id}:
        raise base.FullPipelineBatchSafetyError(
            "durable collection snapshot has a different Batch identity"
        )
    if snapshot.get("status") != "completed":
        raise base.FullPipelineBatchSafetyError(
            "durable collection snapshot is not completed"
        )
    counts = snapshot.get("request_counts")
    if not isinstance(counts, Mapping):
        raise base.FullPipelineBatchSafetyError(
            "durable collection snapshot lacks request counts"
        )
    expected_count = int(manifest.get("request_count", 0) or 0)
    if (
        int(counts.get("total", 0) or 0) != expected_count
        or int(counts.get("completed", 0) or 0) != expected_count
        or int(counts.get("failed", 0) or 0) != 0
    ):
        raise base.FullPipelineBatchSafetyError(
            "durable completed Batch counts do not match frozen request set"
        )
    output_file_id = snapshot.get("output_file_id")
    if not isinstance(output_file_id, str) or not output_file_id:
        raise base.FullPipelineBatchSafetyError(
            "durable completed Batch snapshot lacks output file ID"
        )
    if manifest.get("output_file_id") not in {None, output_file_id}:
        raise base.FullPipelineBatchSafetyError(
            "collection output file ID conflicts with durable provider snapshot"
        )
    error_file_id = snapshot.get("error_file_id")
    if error_file_id is not None and not isinstance(error_file_id, str):
        raise base.FullPipelineBatchSafetyError(
            "durable provider error file ID has invalid type"
        )
    return snapshot


def _begin_collection(
    target: Path,
    manifest: dict[str, Any],
    client: Any,
    now: Callable[[], Any],
) -> dict[str, Any]:
    batch_id = _require_text(manifest.get("batch_id"), "batch_id")
    batch = client.batches.retrieve(batch_id)
    snapshot = base._batch_snapshot(batch)
    if snapshot.get("id") not in {None, batch_id}:
        raise base.FullPipelineBatchSafetyError(
            "provider returned a different Batch identity during collection"
        )
    if snapshot.get("status") != "completed":
        raise base.FullPipelineBatchError(
            f"Batch wave is not completed: {snapshot.get('status')!r}"
        )
    counts = snapshot["request_counts"]
    expected_count = int(manifest["request_count"])
    if (
        counts["total"] != expected_count
        or counts["completed"] != expected_count
        or counts["failed"] != 0
    ):
        raise base.FullPipelineBatchSafetyError(
            "completed Batch wave request counts do not match frozen request set"
        )
    output_file_id = snapshot.get("output_file_id")
    if not isinstance(output_file_id, str) or not output_file_id:
        raise base.FullPipelineBatchSafetyError(
            "completed Batch wave has no output file ID"
        )

    manifest["collection_protocol"] = COLLECTION_PROTOCOL_VERSION
    manifest["collection_started_at_utc"] = base._iso(now())
    manifest["provider_batch"] = snapshot
    manifest["output_file_id"] = output_file_id
    manifest["error_file_id"] = snapshot.get("error_file_id")
    manifest["status"] = STATUS_STARTED
    base._write_manifest(target / base.MANIFEST_FILENAME, manifest)
    return manifest


def _compare_or_write_provider_file(
    path: Path,
    file_id: str,
    client: Any,
    *,
    label: str,
) -> str:
    """Persist once, or resolve an uncheckpointed local file by exact compare."""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise base.FullPipelineBatchSafetyError(
                f"cannot read existing {label} provider evidence: {exc}"
            ) from exc
        remote = base._content_text(client.files.content(file_id))
        if existing != remote:
            raise base.FullPipelineBatchSafetyError(
                f"existing uncheckpointed {label} provider evidence differs from "
                "the remote file; refusing to overwrite either identity"
            )
        return base._sha256_file(path)

    remote = base._content_text(client.files.content(file_id))
    base.atomic_write_text(path, remote)
    return base._sha256_file(path)


def _record_raw_provider_files(
    target: Path,
    manifest: dict[str, Any],
    client: Any,
    *,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    snapshot = _verify_provider_snapshot(manifest)
    output_file_id = snapshot["output_file_id"]
    output_path = target / base.OUTPUT_FILENAME
    output_sha = _compare_or_write_provider_file(
        output_path,
        output_file_id,
        client,
        label="Batch output",
    )
    _hook(crash_hook, "after_output_file_write", manifest)

    error_file_id = snapshot.get("error_file_id")
    error_sha = None
    error_path = target / base.ERROR_FILENAME
    if isinstance(error_file_id, str) and error_file_id:
        error_sha = _compare_or_write_provider_file(
            error_path,
            error_file_id,
            client,
            label="Batch error output",
        )
    elif error_path.exists():
        raise base.FullPipelineBatchSafetyError(
            "unexpected local Batch error output exists without provider error_file_id"
        )

    manifest["output_sha256"] = output_sha
    manifest["error_sha256"] = error_sha
    manifest["status"] = STATUS_RAW_RECORDED
    base._write_manifest(target / base.MANIFEST_FILENAME, manifest)
    _hook(crash_hook, "after_raw_provider_files", manifest)
    return manifest


def _load_verified_raw(target: Path, manifest: Mapping[str, Any]) -> tuple[str, str | None]:
    _require_collection_protocol(manifest)
    _verify_provider_snapshot(manifest)
    output_path = target / base.OUTPUT_FILENAME
    if not output_path.is_file():
        raise base.FullPipelineBatchSafetyError(
            "collection checkpoint says raw output is durable but file is missing"
        )
    if base._sha256_file(output_path) != manifest.get("output_sha256"):
        raise base.FullPipelineBatchSafetyError(
            "durable raw Batch output hash no longer matches manifest"
        )
    try:
        output_text = output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise base.FullPipelineBatchSafetyError(
            f"cannot read durable raw Batch output: {exc}"
        ) from exc

    error_file_id = manifest.get("error_file_id")
    error_text = None
    error_path = target / base.ERROR_FILENAME
    if isinstance(error_file_id, str) and error_file_id:
        if not error_path.is_file():
            raise base.FullPipelineBatchSafetyError(
                "collection checkpoint says provider error output exists but file is missing"
            )
        if base._sha256_file(error_path) != manifest.get("error_sha256"):
            raise base.FullPipelineBatchSafetyError(
                "durable Batch error-output hash no longer matches manifest"
            )
        try:
            error_text = error_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise base.FullPipelineBatchSafetyError(
                f"cannot read durable Batch error output: {exc}"
            ) from exc
    elif error_path.exists():
        raise base.FullPipelineBatchSafetyError(
            "unexpected Batch error-output file exists without provider identity"
        )
    return output_text, error_text


def _build_results_document(
    manifest: Mapping[str, Any],
    output_text: str,
) -> dict[str, Any]:
    rows = base._parse_jsonl(output_text, "Batch output")
    expected_count = int(manifest["request_count"])
    expected_records = {
        item["custom_id"]: item
        for item in manifest.get("requests", [])
        if isinstance(item, Mapping) and isinstance(item.get("custom_id"), str)
    }
    if len(expected_records) != expected_count:
        raise base.FullPipelineBatchSafetyError(
            "wave manifest request identities are malformed or duplicated"
        )
    actual: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        custom_id = row.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id:
            raise base.FullPipelineBatchSafetyError("Batch output row lacks custom_id")
        if custom_id in actual:
            raise base.FullPipelineBatchSafetyError(
                f"duplicate Batch output custom_id: {custom_id}"
            )
        if custom_id not in expected_records:
            raise base.FullPipelineBatchSafetyError(
                f"unknown Batch output custom_id: {custom_id}"
            )
        actual[custom_id] = row
    missing = sorted(set(expected_records) - set(actual))
    if missing:
        raise base.FullPipelineBatchSafetyError(
            "Batch output is missing frozen requests: " + ", ".join(missing)
        )

    results = [
        base._result_from_row(actual[record["custom_id"]], record)
        for record in manifest["requests"]
    ]
    totals = {
        key: sum(item["stats"][key] for item in results)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "cost_usd",
            "rate_correct_cost_usd",
        )
    }
    return {
        "schema_version": base.SCHEMA_VERSION,
        "transport_revision": base.TRANSPORT_REVISION,
        "collection_protocol": COLLECTION_PROTOCOL_VERSION,
        "experiment_id": manifest["experiment_id"],
        "run_id": manifest["run_id"],
        "wave_number": manifest["wave_number"],
        "status": "complete",
        "model": base.BATCH_MODEL,
        "transport": "openai_batch",
        "batch_id": manifest["batch_id"],
        "input_file_id": manifest["input_file_id"],
        "output_file_id": manifest["output_file_id"],
        "error_file_id": manifest.get("error_file_id"),
        "collected_at_utc": manifest["collection_started_at_utc"],
        "requests": results,
        "totals": totals,
        "protected_state_verified_unchanged": True,
    }


def _record_results(
    target: Path,
    manifest: dict[str, Any],
    document: Mapping[str, Any],
    *,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    results_path = target / base.RESULTS_FILENAME
    if results_path.exists():
        existing = base._read_json_object(results_path, "collected wave results")
        if existing != dict(document):
            raise base.FullPipelineBatchSafetyError(
                "existing uncheckpointed collected results differ from deterministic "
                "reconstruction; refusing to overwrite"
            )
    else:
        base.atomic_write_json(results_path, copy.deepcopy(dict(document)))
    _hook(crash_hook, "after_results_file_write", manifest)

    manifest["results_sha256"] = base._sha256_file(results_path)
    manifest["collected_at_utc"] = document["collected_at_utc"]
    manifest["status"] = STATUS_RESULTS_RECORDED
    base._write_manifest(target / base.MANIFEST_FILENAME, manifest)
    _hook(crash_hook, "after_results_recorded", manifest)
    return manifest


def _load_verified_results(target: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    results_path = target / base.RESULTS_FILENAME
    if not results_path.is_file():
        raise base.FullPipelineBatchSafetyError(
            "collection checkpoint says results are durable but file is missing"
        )
    if base._sha256_file(results_path) != manifest.get("results_sha256"):
        raise base.FullPipelineBatchSafetyError(
            "durable collected-results hash no longer matches manifest"
        )
    document = base._read_json_object(results_path, "collected wave results")
    if document.get("collection_protocol") != COLLECTION_PROTOCOL_VERSION:
        raise base.FullPipelineBatchSafetyError(
            "durable collected results use a different collection protocol"
        )
    if document.get("run_id") != manifest.get("run_id"):
        raise base.FullPipelineBatchSafetyError(
            "durable collected results run identity differs from manifest"
        )
    return document


def collect_wave(
    target: Path | str,
    client: Any,
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
    now: Callable[[], Any] = base.utc_now,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Collect one completed Batch with restart-safe local evidence checkpoints."""
    target, manifest = base._load_context(target, capture_protected_state)
    expected_state = manifest["protected_state_before"]
    status = manifest.get("status")

    if status == "collected":
        document = base._load_collected_local(target, manifest)
        return document, manifest
    if status == STATUS_PROVIDER_ERRORS:
        _load_verified_raw(target, manifest)
        raise base.FullPipelineBatchError(
            "completed Batch wave contains provider error rows; raw evidence is "
            "already durable and will not be downloaded again"
        )

    if status not in {STATUS_STARTED, STATUS_RAW_RECORDED, STATUS_RESULTS_RECORDED}:
        manifest = _begin_collection(target, manifest, client, now)
        _hook(crash_hook, "after_collection_started", manifest)
        status = STATUS_STARTED
    else:
        _require_collection_protocol(manifest)
        _verify_provider_snapshot(manifest)

    if status == STATUS_STARTED:
        manifest = _record_raw_provider_files(
            target,
            manifest,
            client,
            crash_hook=crash_hook,
        )
        status = STATUS_RAW_RECORDED

    if status == STATUS_RAW_RECORDED:
        output_text, error_text = _load_verified_raw(target, manifest)
        if error_text is not None and base._parse_jsonl(
            error_text,
            "Batch error output",
        ):
            manifest["status"] = STATUS_PROVIDER_ERRORS
            manifest["last_error"] = "completed Batch wave contains provider error rows"
            base._write_manifest(target / base.MANIFEST_FILENAME, manifest)
            raise base.FullPipelineBatchError(manifest["last_error"])
        document = _build_results_document(manifest, output_text)
        manifest = _record_results(
            target,
            manifest,
            document,
            crash_hook=crash_hook,
        )
        status = STATUS_RESULTS_RECORDED

    if status != STATUS_RESULTS_RECORDED:
        raise FullPipelineBatchCollectionV2Error(
            f"unexpected collection state before finalization: {status!r}"
        )

    document = _load_verified_results(target, manifest)
    base._ensure_protected_state(
        capture_protected_state,
        expected_state,
        "after collection",
    )
    manifest["status"] = "collected"
    manifest["protected_state_verified_unchanged"] = True
    manifest.pop("last_error", None)
    base._write_manifest(target / base.MANIFEST_FILENAME, manifest)
    return document, manifest
