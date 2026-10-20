"""Restart-safe OpenAI Batch transport for chronological full-pipeline waves.

This module is experiment infrastructure, not a production model selector and
not a replacement for NovelTranslator's state/glossary engine.  It owns only
one immutable Batch wave lifecycle:

    prepare -> input_uploaded -> submitted -> batch_* -> collected

Ambiguous paid boundaries fail closed.  A Batch create whose response is lost
may be reconciled only by adopting exactly one remote Batch matching the frozen
run identity and uploaded input file.  Upload ambiguity is never automatically
reissued.  Collected provider output is persisted verbatim before parsing, and
a collected wave is replayed locally rather than downloaded again.

Importing this module performs no credential loading, network activity, or file
writes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from noveltranslator_core import atomic_write_json, atomic_write_text


SCHEMA_VERSION = 1
TRANSPORT_REVISION = "full-pipeline-luna-batch-wave-v1"
BATCH_ENDPOINT = "/v1/chat/completions"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_MODEL = "gpt-5.6-luna"
BATCH_PRICING_VERSION = "openai-gpt-5.6-luna-batch-0.10-0.01-0.60"
BATCH_CACHE_HIT_RATE = 0.01
BATCH_CACHE_MISS_RATE = 0.10
BATCH_OUTPUT_RATE = 0.60
SUBMIT_AUTHORIZATION = "RUN-FULL-PIPELINE-LUNA-BATCH-WAVE-V1"

MANIFEST_FILENAME = "wave_manifest.json"
INPUT_FILENAME = "batch_input.jsonl"
OUTPUT_FILENAME = "batch_output.jsonl"
ERROR_FILENAME = "batch_error.jsonl"
RESULTS_FILENAME = "collected_results.json"

TERMINAL_PROVIDER_STATUSES = frozenset(
    {"completed", "failed", "expired", "cancelled"}
)
RECONCILABLE_SUBMISSION_STATUSES = frozenset(
    {"submission_started", "submission_uncertain"}
)


class FullPipelineBatchError(RuntimeError):
    """Base fail-closed error for one full-pipeline Batch wave."""


class FullPipelineBatchSafetyError(FullPipelineBatchError):
    """Local identity/state evidence is inconsistent or unsafe."""


class FullPipelineBatchSubmissionUncertain(FullPipelineBatchError):
    """A paid remote boundary may have succeeded but lacks durable identity."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Any) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment.astimezone(timezone.utc).isoformat()
    return str(moment)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FullPipelineBatchSafetyError(
            "Batch wave identity is not canonically JSON-serializable"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise FullPipelineBatchSafetyError(
            f"cannot hash Batch wave file {path}: {exc}"
        ) from exc


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _provider_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    result: dict[str, Any] = {}
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
        "purpose",
        "filename",
        "bytes",
    ):
        item = getattr(value, name, None)
        if item is not None:
            result[name] = (
                _provider_dict(item)
                if name in {"request_counts", "metadata"}
                else item
            )
    return result


def _request_counts(batch: Any) -> dict[str, int]:
    counts = _field(batch, "request_counts")
    return {
        "total": int(_field(counts, "total", 0) or 0),
        "completed": int(_field(counts, "completed", 0) or 0),
        "failed": int(_field(counts, "failed", 0) or 0),
    }


def _batch_snapshot(batch: Any) -> dict[str, Any]:
    return {
        "id": _field(batch, "id"),
        "status": _field(batch, "status"),
        "endpoint": _field(batch, "endpoint"),
        "input_file_id": _field(batch, "input_file_id"),
        "output_file_id": _field(batch, "output_file_id"),
        "error_file_id": _field(batch, "error_file_id"),
        "completion_window": _field(batch, "completion_window"),
        "created_at": _field(batch, "created_at"),
        "completed_at": _field(batch, "completed_at"),
        "expires_at": _field(batch, "expires_at"),
        "request_counts": _request_counts(batch),
        "metadata": _provider_dict(_field(batch, "metadata")),
    }


def _validate_wave_number(wave_number: int) -> int:
    if (
        not isinstance(wave_number, int)
        or isinstance(wave_number, bool)
        or wave_number < 1
    ):
        raise FullPipelineBatchSafetyError(
            "wave_number must be a positive integer"
        )
    return wave_number


def wave_directory(root: Path | str, wave_number: int) -> Path:
    return Path(root).resolve() / f"wave_{_validate_wave_number(wave_number):04d}"


def _safe_id_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    return cleaned[:24] or "novel"


def _validate_request_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise FullPipelineBatchSafetyError("wave request must be an object")
    novel_id = spec.get("novel_id")
    chapter = spec.get("chapter")
    body = spec.get("body")
    lineage_before = spec.get("lineage_before")
    if not isinstance(novel_id, str) or not novel_id.strip():
        raise FullPipelineBatchSafetyError("wave request novel_id is missing")
    if (
        not isinstance(chapter, int)
        or isinstance(chapter, bool)
        or chapter < 1
    ):
        raise FullPipelineBatchSafetyError(
            "wave request chapter must be a positive integer"
        )
    if not isinstance(body, Mapping):
        raise FullPipelineBatchSafetyError("wave request body must be an object")
    body = copy.deepcopy(dict(body))
    if body.get("model") != BATCH_MODEL:
        raise FullPipelineBatchSafetyError(
            f"Batch wave request must use exact model {BATCH_MODEL!r}"
        )
    if body.get("reasoning_effort") != "none":
        raise FullPipelineBatchSafetyError(
            "Batch wave Luna request must use reasoning_effort='none'"
        )
    max_tokens = body.get("max_completion_tokens")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise FullPipelineBatchSafetyError(
            "Batch wave request requires positive max_completion_tokens"
        )
    if "max_tokens" in body or "extra_body" in body:
        raise FullPipelineBatchSafetyError(
            "Batch wave request contains DeepSeek-only request fields"
        )
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        raise FullPipelineBatchSafetyError(
            "Batch wave request requires system/user messages"
        )
    if not isinstance(lineage_before, Mapping):
        raise FullPipelineBatchSafetyError(
            "wave request requires a lineage_before identity object"
        )
    lineage = copy.deepcopy(dict(lineage_before))
    lineage_sha = _sha256_bytes(_canonical_json_bytes(lineage))
    return {
        "novel_id": novel_id.strip(),
        "chapter": chapter,
        "body": body,
        "lineage_before": lineage,
        "lineage_before_sha256": lineage_sha,
    }


def _custom_id(
    wave_number: int,
    novel_id: str,
    chapter: int,
    body: Mapping[str, Any],
    lineage_sha256: str,
) -> str:
    identity = {
        "revision": TRANSPORT_REVISION,
        "wave": wave_number,
        "novel_id": novel_id,
        "chapter": chapter,
        "body": body,
        "lineage_before_sha256": lineage_sha256,
    }
    digest = _sha256_bytes(_canonical_json_bytes(identity))
    return (
        f"fpw{wave_number:04d}-{_safe_id_component(novel_id)}-"
        f"c{chapter:04d}-{digest[:16]}"
    )


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    atomic_write_json(path, copy.deepcopy(dict(manifest)))


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullPipelineBatchSafetyError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FullPipelineBatchSafetyError(f"{label} must contain an object")
    return value


def _read_manifest(target: Path) -> dict[str, Any]:
    manifest = _read_json_object(target / MANIFEST_FILENAME, "wave manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise FullPipelineBatchSafetyError("unsupported Batch wave schema version")
    if manifest.get("transport_revision") != TRANSPORT_REVISION:
        raise FullPipelineBatchSafetyError("unsupported Batch wave transport revision")
    return manifest


def _capture_state(capture: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    value = capture()
    if not isinstance(value, Mapping):
        raise FullPipelineBatchSafetyError(
            "protected-state capture must return an object"
        )
    return copy.deepcopy(dict(value))


def _ensure_protected_state(
    capture: Callable[[], Mapping[str, Any]],
    expected: Mapping[str, Any],
    stage: str,
) -> dict[str, Any]:
    current = _capture_state(capture)
    if current != dict(expected):
        raise FullPipelineBatchSafetyError(
            f"protected production state changed during Batch wave: {stage}"
        )
    return current


def _validate_frozen_input(target: Path, manifest: Mapping[str, Any]) -> None:
    input_path = target / INPUT_FILENAME
    if not input_path.is_file():
        raise FullPipelineBatchSafetyError(
            f"frozen Batch input is missing: {input_path}"
        )
    if _sha256_file(input_path) != manifest.get("input_sha256"):
        raise FullPipelineBatchSafetyError(
            "frozen Batch input hash no longer matches wave manifest"
        )


def prepare_wave(
    root: Path | str,
    wave_number: int,
    requests: Sequence[Mapping[str, Any]],
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
    experiment_id: str,
    now: Callable[[], datetime] = utc_now,
) -> tuple[dict[str, Any], Path]:
    """Freeze one chronological wave without provider access."""
    wave_number = _validate_wave_number(wave_number)
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise FullPipelineBatchSafetyError("experiment_id must be non-empty")
    if not requests:
        raise FullPipelineBatchSafetyError("Batch wave requires at least one request")

    target = wave_directory(root, wave_number)
    if target.exists():
        raise FullPipelineBatchSafetyError(
            f"Batch wave target already exists and was not overwritten: {target}"
        )
    protected_before = _capture_state(capture_protected_state)

    normalized: list[dict[str, Any]] = []
    seen_novel_chapter: set[tuple[str, int]] = set()
    for raw in requests:
        item = _validate_request_spec(raw)
        key = (item["novel_id"], item["chapter"])
        if key in seen_novel_chapter:
            raise FullPipelineBatchSafetyError(
                f"duplicate novel/chapter request in wave: {key[0]} Chapter {key[1]}"
            )
        seen_novel_chapter.add(key)
        normalized.append(item)

    lines: list[str] = []
    records: list[dict[str, Any]] = []
    seen_custom_ids: set[str] = set()
    for item in normalized:
        custom_id = _custom_id(
            wave_number,
            item["novel_id"],
            item["chapter"],
            item["body"],
            item["lineage_before_sha256"],
        )
        if custom_id in seen_custom_ids:
            raise FullPipelineBatchSafetyError(
                f"duplicate deterministic custom_id: {custom_id}"
            )
        seen_custom_ids.add(custom_id)
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": item["body"],
        }
        lines.append(
            json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        )
        records.append(
            {
                "novel_id": item["novel_id"],
                "chapter": item["chapter"],
                "custom_id": custom_id,
                "request_sha256": _sha256_bytes(_canonical_json_bytes(line)),
                "body_sha256": _sha256_bytes(_canonical_json_bytes(item["body"])),
                "lineage_before": item["lineage_before"],
                "lineage_before_sha256": item["lineage_before_sha256"],
            }
        )

    target.mkdir(parents=True, exist_ok=False)
    input_text = "\n".join(lines) + "\n"
    input_path = target / INPUT_FILENAME
    atomic_write_text(input_path, input_text)
    input_sha = _sha256_file(input_path)
    prepared_at = now()
    stable_identity = {
        "transport_revision": TRANSPORT_REVISION,
        "experiment_id": experiment_id.strip(),
        "wave_number": wave_number,
        "model": BATCH_MODEL,
        "pricing_version": BATCH_PRICING_VERSION,
        "input_sha256": input_sha,
        "requests": records,
        "protected_state_before": protected_before,
    }
    run_id = _sha256_bytes(_canonical_json_bytes(stable_identity))
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        **stable_identity,
        "run_id": run_id,
        "status": "prepared",
        "prepared_at_utc": _iso(prepared_at),
        "endpoint": BATCH_ENDPOINT,
        "completion_window": BATCH_COMPLETION_WINDOW,
        "request_count": len(records),
        "input_file": INPUT_FILENAME,
        "input_file_id": None,
        "batch_id": None,
        "provider_batch": None,
        "paid_batch_authorized": False,
        "protected_state_verified_unchanged": False,
        "pricing": {
            "cache_hit_rate_per_million": BATCH_CACHE_HIT_RATE,
            "cache_miss_rate_per_million": BATCH_CACHE_MISS_RATE,
            "output_rate_per_million": BATCH_OUTPUT_RATE,
        },
    }
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    _ensure_protected_state(
        capture_protected_state,
        protected_before,
        "prepare",
    )
    manifest["protected_state_verified_unchanged"] = True
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    return manifest, target


def _load_context(
    target: Path | str,
    capture_protected_state: Callable[[], Mapping[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    target = Path(target).resolve()
    manifest = _read_manifest(target)
    expected = manifest.get("protected_state_before")
    if not isinstance(expected, Mapping):
        raise FullPipelineBatchSafetyError(
            "wave manifest lacks protected production-state identity"
        )
    _ensure_protected_state(capture_protected_state, expected, "context load")
    _validate_frozen_input(target, manifest)
    return target, manifest


def submit_wave(
    target: Path | str,
    client: Any,
    authorization: str,
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Upload and create exactly one Batch, failing closed on ambiguity."""
    target, manifest = _load_context(target, capture_protected_state)
    expected = manifest["protected_state_before"]
    if authorization != SUBMIT_AUTHORIZATION:
        raise FullPipelineBatchError(
            "paid Batch wave submission was not explicitly authorized"
        )
    if manifest.get("status") not in {"prepared", "input_uploaded"}:
        raise FullPipelineBatchError(
            "Batch wave submission is not eligible from manifest status "
            f"{manifest.get('status')!r}; automatic reissue is blocked"
        )
    manifest["paid_batch_authorized"] = True

    input_path = target / INPUT_FILENAME
    if manifest["status"] == "prepared":
        manifest["status"] = "upload_started"
        _write_manifest(target / MANIFEST_FILENAME, manifest)
        try:
            with input_path.open("rb") as handle:
                uploaded = client.files.create(file=handle, purpose="batch")
        except Exception as exc:
            manifest["status"] = "upload_uncertain"
            manifest["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
            _write_manifest(target / MANIFEST_FILENAME, manifest)
            raise FullPipelineBatchSubmissionUncertain(
                "Batch wave input upload outcome is uncertain; automatic upload "
                "reissue is forbidden"
            ) from exc
        file_id = _field(uploaded, "id")
        if not isinstance(file_id, str) or not file_id:
            manifest["status"] = "upload_uncertain"
            _write_manifest(target / MANIFEST_FILENAME, manifest)
            raise FullPipelineBatchSubmissionUncertain(
                "Batch wave input upload returned no file ID"
            )
        manifest["input_file_id"] = file_id
        manifest["uploaded_file"] = _provider_dict(uploaded)
        manifest["status"] = "input_uploaded"
        manifest.pop("last_error", None)
        _write_manifest(target / MANIFEST_FILENAME, manifest)

    metadata = {
        "experiment": "noveltranslator-full-pipeline",
        "run_id": manifest["run_id"],
        "wave": str(manifest["wave_number"]),
    }
    manifest["status"] = "submission_started"
    manifest["submission_metadata"] = metadata
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    try:
        batch = client.batches.create(
            input_file_id=manifest["input_file_id"],
            endpoint=BATCH_ENDPOINT,
            completion_window=BATCH_COMPLETION_WINDOW,
            metadata=metadata,
        )
    except Exception as exc:
        manifest["status"] = "submission_uncertain"
        manifest["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        _write_manifest(target / MANIFEST_FILENAME, manifest)
        raise FullPipelineBatchSubmissionUncertain(
            "remote Batch wave creation outcome is uncertain; automatic reissue "
            "is forbidden"
        ) from exc

    batch_id = _field(batch, "id")
    if not isinstance(batch_id, str) or not batch_id:
        manifest["status"] = "submission_uncertain"
        _write_manifest(target / MANIFEST_FILENAME, manifest)
        raise FullPipelineBatchSubmissionUncertain(
            "remote Batch wave creation returned no batch ID"
        )
    manifest["batch_id"] = batch_id
    manifest["provider_batch"] = _batch_snapshot(batch)
    manifest["status"] = "submitted"
    manifest.pop("last_error", None)
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    _ensure_protected_state(capture_protected_state, expected, "after submit")
    return manifest


def _iter_page_data(page: Any) -> list[Any]:
    return list(_field(page, "data", []) or [])


def _listed_batches(client: Any, *, max_pages: int = 20):
    page = client.batches.list(limit=100)
    for _ in range(max_pages):
        yield from _iter_page_data(page)
        has_next = getattr(page, "has_next_page", None)
        get_next = getattr(page, "get_next_page", None)
        if not callable(has_next) or not has_next():
            return
        if not callable(get_next):
            raise FullPipelineBatchSubmissionUncertain(
                "provider Batch listing reports another page but exposes no paginator"
            )
        page = get_next()
    raise FullPipelineBatchSubmissionUncertain(
        "Batch wave reconciliation exceeded bounded listing horizon"
    )


def reconcile_submission(
    target: Path | str,
    client: Any,
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Adopt exactly one matching remote Batch after an ambiguous create."""
    target, manifest = _load_context(target, capture_protected_state)
    expected = manifest["protected_state_before"]
    if manifest.get("status") not in RECONCILABLE_SUBMISSION_STATUSES:
        raise FullPipelineBatchError(
            "Batch wave reconciliation is allowed only after an ambiguous "
            "submission boundary"
        )
    input_file_id = manifest.get("input_file_id")
    metadata = manifest.get("submission_metadata")
    if not isinstance(input_file_id, str) or not input_file_id:
        raise FullPipelineBatchSubmissionUncertain(
            "ambiguous Batch wave has no durable input file ID; do not resubmit"
        )
    if not isinstance(metadata, Mapping):
        raise FullPipelineBatchSubmissionUncertain(
            "ambiguous Batch wave has no durable submission metadata"
        )

    matches = []
    for batch in _listed_batches(client):
        snapshot = _batch_snapshot(batch)
        if snapshot.get("input_file_id") != input_file_id:
            continue
        if snapshot.get("endpoint") != BATCH_ENDPOINT:
            continue
        if snapshot.get("completion_window") != BATCH_COMPLETION_WINDOW:
            continue
        if snapshot.get("metadata") != dict(metadata):
            continue
        matches.append(batch)
    if len(matches) != 1:
        raise FullPipelineBatchSubmissionUncertain(
            "Batch wave reconciliation requires exactly one matching remote Batch; "
            f"found {len(matches)}"
        )

    batch = matches[0]
    batch_id = _field(batch, "id")
    if not isinstance(batch_id, str) or not batch_id:
        raise FullPipelineBatchSubmissionUncertain(
            "matching remote Batch has no batch ID"
        )
    manifest["batch_id"] = batch_id
    manifest["provider_batch"] = _batch_snapshot(batch)
    manifest["status"] = "submitted"
    manifest["reconciled_submission"] = True
    manifest.pop("last_error", None)
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    _ensure_protected_state(capture_protected_state, expected, "after reconcile")
    return manifest


def refresh_status(
    target: Path | str,
    client: Any,
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Retrieve provider status only; this performs no inference."""
    target, manifest = _load_context(target, capture_protected_state)
    expected = manifest["protected_state_before"]
    batch_id = manifest.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise FullPipelineBatchError("Batch wave has no durable batch ID")
    if manifest.get("status") == "collected":
        return manifest

    batch = client.batches.retrieve(batch_id)
    snapshot = _batch_snapshot(batch)
    if snapshot.get("id") not in {None, batch_id}:
        raise FullPipelineBatchSafetyError(
            "provider returned a different Batch identity during status refresh"
        )
    provider_status = snapshot.get("status")
    manifest["provider_batch"] = snapshot
    manifest["status"] = f"batch_{provider_status}"
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    _ensure_protected_state(capture_protected_state, expected, "after status")
    return manifest


def _content_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8")
    if isinstance(content, str):
        return content
    raise FullPipelineBatchSafetyError(
        "provider file-content response contains no UTF-8 text"
    )


def _parse_jsonl(text: str, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FullPipelineBatchSafetyError(
                f"{label} line {line_number} is malformed JSON"
            ) from exc
        if not isinstance(value, dict):
            raise FullPipelineBatchSafetyError(
                f"{label} line {line_number} must contain an object"
            )
        rows.append(value)
    return rows


def _usage_value(usage: Any, name: str) -> int:
    value = _field(usage, name, 0) or 0
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise FullPipelineBatchSafetyError(
            f"provider usage {name} is not an integer"
        ) from exc
    if integer < 0:
        raise FullPipelineBatchSafetyError(
            f"provider usage {name} cannot be negative"
        )
    return integer


def _batch_stats(usage: Any) -> dict[str, Any]:
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    details = _field(usage, "prompt_tokens_details", {}) or {}
    cache_hits = _usage_value(details, "cached_tokens")
    if cache_hits > prompt_tokens:
        raise FullPipelineBatchSafetyError(
            "provider cached prompt tokens exceed total prompt tokens"
        )
    cache_misses = prompt_tokens - cache_hits
    cost = (
        cache_hits / 1_000_000 * BATCH_CACHE_HIT_RATE
        + cache_misses / 1_000_000 * BATCH_CACHE_MISS_RATE
        + completion_tokens / 1_000_000 * BATCH_OUTPUT_RATE
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "pricing_version": BATCH_PRICING_VERSION,
        "pricing_period": "batch",
        "pricing_mode": "batch_actual",
        "request_started_utc": "openai-batch-provider-managed",
        "cache_hit_rate_per_million": BATCH_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": BATCH_CACHE_MISS_RATE,
        "output_rate_per_million": BATCH_OUTPUT_RATE,
    }


def _result_from_row(
    row: Mapping[str, Any],
    request_record: Mapping[str, Any],
) -> dict[str, Any]:
    if row.get("custom_id") != request_record.get("custom_id"):
        raise FullPipelineBatchSafetyError("Batch row custom_id identity mismatch")
    if row.get("error") not in (None, {}):
        raise FullPipelineBatchError(
            f"Batch request {row.get('custom_id')} returned an error: {row.get('error')}"
        )
    response = row.get("response")
    if not isinstance(response, Mapping):
        raise FullPipelineBatchSafetyError("Batch row response is missing")
    if response.get("status_code") != 200:
        raise FullPipelineBatchError(
            f"Batch request {row.get('custom_id')} returned HTTP "
            f"{response.get('status_code')!r}"
        )
    body = response.get("body")
    if not isinstance(body, Mapping):
        raise FullPipelineBatchSafetyError("Batch response body is missing")
    if body.get("model") != BATCH_MODEL:
        raise FullPipelineBatchSafetyError(
            f"Batch provider model mismatch: expected {BATCH_MODEL!r}, "
            f"found {body.get('model')!r}"
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise FullPipelineBatchSafetyError(
            "Batch response must contain exactly one translation choice"
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise FullPipelineBatchSafetyError("Batch response choice is malformed")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise FullPipelineBatchSafetyError("Batch response message is missing")
    content = message.get("content")
    if not isinstance(content, str):
        raise FullPipelineBatchSafetyError("Batch response content is not text")
    stats = _batch_stats(body.get("usage") or {})
    return {
        "novel_id": request_record["novel_id"],
        "chapter": request_record["chapter"],
        "custom_id": request_record["custom_id"],
        "request_sha256": request_record["request_sha256"],
        "body_sha256": request_record["body_sha256"],
        "lineage_before": copy.deepcopy(request_record["lineage_before"]),
        "lineage_before_sha256": request_record["lineage_before_sha256"],
        "content": content,
        "finish_reason": choice.get("finish_reason"),
        "stats": stats,
        "provider_request_id": response.get("request_id") or "",
        "provider_response_id": body.get("id") or "",
        "provider_response_model": body.get("model") or "",
        "transport": "openai_batch",
    }


def _load_collected_local(target: Path, manifest: Mapping[str, Any]):
    results_path = target / RESULTS_FILENAME
    output_path = target / OUTPUT_FILENAME
    if not results_path.is_file() or not output_path.is_file():
        raise FullPipelineBatchSafetyError(
            "collected wave is missing local provider/result evidence"
        )
    if _sha256_file(results_path) != manifest.get("results_sha256"):
        raise FullPipelineBatchSafetyError(
            "collected results hash no longer matches wave manifest"
        )
    if _sha256_file(output_path) != manifest.get("output_sha256"):
        raise FullPipelineBatchSafetyError(
            "collected raw output hash no longer matches wave manifest"
        )
    return _read_json_object(results_path, "collected wave results")


def collect_wave(
    target: Path | str,
    client: Any,
    *,
    capture_protected_state: Callable[[], Mapping[str, Any]],
    now: Callable[[], datetime] = utc_now,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist raw completed provider output, validate identity, and materialize results."""
    target, manifest = _load_context(target, capture_protected_state)
    expected_state = manifest["protected_state_before"]
    if manifest.get("status") == "collected":
        document = _load_collected_local(target, manifest)
        return document, manifest

    batch_id = manifest.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise FullPipelineBatchError("Batch wave has no durable batch ID")
    batch = client.batches.retrieve(batch_id)
    snapshot = _batch_snapshot(batch)
    if snapshot.get("id") not in {None, batch_id}:
        raise FullPipelineBatchSafetyError(
            "provider returned a different Batch identity during collection"
        )
    if snapshot.get("status") != "completed":
        raise FullPipelineBatchError(
            f"Batch wave is not completed: {snapshot.get('status')!r}"
        )
    counts = snapshot["request_counts"]
    expected_count = int(manifest["request_count"])
    if (
        counts["total"] != expected_count
        or counts["completed"] != expected_count
        or counts["failed"] != 0
    ):
        raise FullPipelineBatchSafetyError(
            "completed Batch wave request counts do not match frozen request set"
        )
    output_file_id = snapshot.get("output_file_id")
    if not isinstance(output_file_id, str) or not output_file_id:
        raise FullPipelineBatchSafetyError(
            "completed Batch wave has no output file ID"
        )

    output_text = _content_text(client.files.content(output_file_id))
    atomic_write_text(target / OUTPUT_FILENAME, output_text)
    output_sha = _sha256_file(target / OUTPUT_FILENAME)

    error_file_id = snapshot.get("error_file_id")
    error_sha = None
    if isinstance(error_file_id, str) and error_file_id:
        error_text = _content_text(client.files.content(error_file_id))
        atomic_write_text(target / ERROR_FILENAME, error_text)
        error_sha = _sha256_file(target / ERROR_FILENAME)
        if _parse_jsonl(error_text, "Batch error output"):
            raise FullPipelineBatchError(
                "completed Batch wave contains provider error rows"
            )

    rows = _parse_jsonl(output_text, "Batch output")
    expected_records = {
        item["custom_id"]: item
        for item in manifest.get("requests", [])
        if isinstance(item, Mapping) and isinstance(item.get("custom_id"), str)
    }
    if len(expected_records) != expected_count:
        raise FullPipelineBatchSafetyError(
            "wave manifest request identities are malformed or duplicated"
        )
    actual: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        custom_id = row.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id:
            raise FullPipelineBatchSafetyError("Batch output row lacks custom_id")
        if custom_id in actual:
            raise FullPipelineBatchSafetyError(
                f"duplicate Batch output custom_id: {custom_id}"
            )
        if custom_id not in expected_records:
            raise FullPipelineBatchSafetyError(
                f"unknown Batch output custom_id: {custom_id}"
            )
        actual[custom_id] = row
    missing = sorted(set(expected_records) - set(actual))
    if missing:
        raise FullPipelineBatchSafetyError(
            "Batch output is missing frozen requests: " + ", ".join(missing)
        )

    results = [
        _result_from_row(actual[record["custom_id"]], record)
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
    document = {
        "schema_version": SCHEMA_VERSION,
        "transport_revision": TRANSPORT_REVISION,
        "experiment_id": manifest["experiment_id"],
        "run_id": manifest["run_id"],
        "wave_number": manifest["wave_number"],
        "status": "complete",
        "model": BATCH_MODEL,
        "transport": "openai_batch",
        "batch_id": batch_id,
        "input_file_id": manifest["input_file_id"],
        "output_file_id": output_file_id,
        "error_file_id": error_file_id,
        "collected_at_utc": _iso(now()),
        "requests": results,
        "totals": totals,
        "protected_state_verified_unchanged": True,
    }
    atomic_write_json(target / RESULTS_FILENAME, document)
    results_sha = _sha256_file(target / RESULTS_FILENAME)

    _ensure_protected_state(
        capture_protected_state,
        expected_state,
        "after collection",
    )
    manifest["provider_batch"] = snapshot
    manifest["status"] = "collected"
    manifest["output_file_id"] = output_file_id
    manifest["error_file_id"] = error_file_id
    manifest["output_sha256"] = output_sha
    manifest["error_sha256"] = error_sha
    manifest["results_sha256"] = results_sha
    manifest["collected_at_utc"] = document["collected_at_utc"]
    manifest["protected_state_verified_unchanged"] = True
    _write_manifest(target / MANIFEST_FILENAME, manifest)
    return document, manifest
