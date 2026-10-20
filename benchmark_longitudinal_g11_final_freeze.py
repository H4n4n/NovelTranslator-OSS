"""G11 zero-provider FINAL FREEZE.

This module builds a create-once, read-only, zero-provider evidence boundary for
the completed G11 practical V4-Pro-replacement benchmark.  It is the immutable
line between the finished 400-chapter paid experiment and every later phase
(anonymity / A-B mapping, blind literary scoring, longitudinal scoring, score
freeze, identity reveal, final synthesis).

It deliberately does **not**:

* call a Provider or construct a client;
* read a credential, ``.env`` file, or DPAPI/provider secret;
* score a translation or name a literary winner;
* build an anonymous A/B bundle;
* reveal any model identity;
* create a private identity key;
* mutate the repository, execution, pristine, or production roots.

What it does:

* re-verifies the frozen G11 preregistration binding and the runner revision
  (read-only, ``verify_g11``);
* proves every track finished exactly Chapters 1-100 with the exact 14 literary
  checkpoints, the 4 state/audit boundaries, no active quarantine, and the
  expected DeepSeek adaptive-low Chapter-4 behaviour;
* reads each physical model-level ledger exactly ONCE (two tracks share one
  physical ledger and are never summed);
* takes a byte-identical physical snapshot of the whole execution root and
  proves ``source == snapshot == source`` for a deterministic physical-tree
  fingerprint (sorted POSIX path + byte size + SHA-256 per file, whole-tree
  SHA-256, symlinks rejected);
* writes a deterministic ``freeze_id`` over the canonical freeze payload.

Importing this module performs no filesystem write, credential read, network
activity, or Provider call.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import benchmark_longitudinal_g11_runner as g11_runner
from benchmark_longitudinal_g11_preregistration import (
    AUDIT_CHECKPOINTS,
    DEEPSEEK_MODEL_ID,
    G11_BENCHMARK_ID,
    G11_PREREGISTRATION_SHA256,
    G11_REPOSITORY_ANCHOR,
    G11_REVISION as G11_PREREGISTRATION_REVISION,
    MODEL_BUDGETS,
    MODEL_ORDER,
    MODEL_SLUGS,
    NOVELS,
    QUALITY_CHECKPOINTS,
    SOURCE_BINDINGS,
    STATE_EVIDENCE_CHECKPOINTS,
    TRACKS,
    file_sha256,
    g11_preregistration_identity,
)
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import inspect_paid_request_journals


G11F_SCHEMA_VERSION = 1
G11F_REVISION = "g11-final-freeze-v1"

EXECUTION_MANIFEST_FILENAME = "execution_tree_manifest.json"
FREEZE_FILENAME = "g11_final_freeze.json"
SUMMARY_FILENAME = "G11_FINAL_FREEZE_SUMMARY.txt"
EXECUTION_SNAPSHOT_DIRECTORY = "execution_snapshot"

LITERARY_CHECKPOINTS = tuple(QUALITY_CHECKPOINTS)
STATE_CHECKPOINTS = tuple(STATE_EVIDENCE_CHECKPOINTS)
AUDIT_BOUNDARIES = tuple(AUDIT_CHECKPOINTS)
AUDIT_WINDOWS = ((1, 25), (26, 50), (51, 75), (76, 100))
FINAL_AUDIT_WINDOW = (76, 100)
NOVEL_ORDER = tuple(NOVELS)
EXPECTED_TRACK_RUN_IDS = tuple(track["run_id"] for track in TRACKS)

EXPECTED_CANONICAL_CHAPTERS = 400
EXPECTED_SCHEDULED_AUDITS = 16
EXPECTED_LITERARY_MATCHED = 28
EXPECTED_STATE_MATCHED = 8
LAST_CHAPTER = 100
NEXT_CHAPTER_AFTER_COMPLETION = 101
DEEPSEEK_ADAPTIVE_LOW_ACTIVATION_CHAPTER = 4


class G11FinalFreezeError(RuntimeError):
    """The G11 final freeze is incomplete, unsafe, or inconsistent."""


class G11FinalFreezeSafetyError(G11FinalFreezeError):
    """A safety invariant (isolation, flags, create-once) was violated."""


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G11FinalFreezeError(
            "G11 final-freeze value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G11FinalFreezeError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _read_json(path: Path | str, label: str) -> Any:
    target = Path(path)
    if not target.is_file():
        raise G11FinalFreezeError(f"{label} is missing: {target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G11FinalFreezeError(f"{label} cannot be read: {target}") from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G11FinalFreezeError(f"{label} must be a JSON object")
    return dict(value)


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise G11FinalFreezeError(f"{label} must be a lowercase SHA-256")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise G11FinalFreezeError(f"{label} must be an integer")
    return value


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


# --------------------------------------------------------------------------- #
# Physical tree identity (freeze-specific deterministic fingerprint)
# --------------------------------------------------------------------------- #


def _tree_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_json(list(records))


def physical_tree_manifest(root: Path | str) -> dict[str, Any]:
    """Deterministic physical-tree fingerprint of every regular file.

    The identity is machine-independent: only the sorted relative POSIX file
    path, the exact byte size, and the SHA-256 of the file bytes participate.
    Symlinks (root or descendant) are rejected rather than followed.
    """
    base = Path(root)
    if base.is_symlink():
        raise G11FinalFreezeError(f"physical tree root must not be a symlink: {base}")
    if not base.is_dir():
        raise G11FinalFreezeError(f"physical tree root is missing: {base}")

    records: list[dict[str, Any]] = []
    for path in sorted(
        base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()
    ):
        if path.is_symlink():
            raise G11FinalFreezeError(f"physical tree contains a symlink: {path}")
        if not path.is_file():
            continue
        payload = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(base).as_posix(),
                "bytes": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )
    return {
        "files": records,
        "file_count": len(records),
        "byte_count": sum(record["bytes"] for record in records),
        "sha256": _tree_sha256(records),
    }


def _manifest_subset(
    manifest: Mapping[str, Any], prefix: str
) -> dict[str, Any]:
    records = [
        dict(record)
        for record in manifest.get("files", [])
        if str(record.get("path", "")).startswith(prefix)
    ]
    return {
        "file_count": len(records),
        "byte_count": sum(int(record["bytes"]) for record in records),
        "sha256": _tree_sha256(records),
    }


def copy_physical_tree(source: Path | str, destination: Path | str) -> Path:
    """Copy a whole tree byte-for-byte, rejecting symlinks and never overwriting."""
    src = Path(source)
    dst = Path(destination)
    if src.is_symlink():
        raise G11FinalFreezeError(f"physical tree root must not be a symlink: {src}")
    if not src.is_dir():
        raise G11FinalFreezeError(f"physical tree source is missing: {src}")
    if dst.exists():
        raise G11FinalFreezeError(f"physical tree destination already exists: {dst}")
    dst.mkdir(parents=True)
    for path in sorted(
        src.rglob("*"), key=lambda item: item.relative_to(src).as_posix()
    ):
        if path.is_symlink():
            raise G11FinalFreezeError(f"physical tree contains a symlink: {path}")
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    return dst


# --------------------------------------------------------------------------- #
# Output-root isolation / create-once safety
# --------------------------------------------------------------------------- #


def _assert_output_root_disjoint(
    output_root: Path, forbidden: Mapping[str, Path | str | None]
) -> Path:
    raw = Path(output_root).expanduser()
    if raw.is_symlink():
        raise G11FinalFreezeSafetyError("G11 final-freeze output root is a symlink")
    resolved = raw.resolve()
    for label, value in forbidden.items():
        if value is None:
            continue
        frozen = Path(value).expanduser().resolve()
        if (
            resolved == frozen
            or _is_within(resolved, frozen)
            or _is_within(frozen, resolved)
        ):
            raise G11FinalFreezeSafetyError(
                f"G11 final-freeze output root overlaps the {label} root"
            )
    return resolved


def _assert_output_root_available(output_root: Path) -> None:
    if output_root.exists():
        raise G11FinalFreezeSafetyError(
            f"G11 final-freeze output root already exists: {output_root}"
        )


# --------------------------------------------------------------------------- #
# Execution evidence extraction (read-only)
# --------------------------------------------------------------------------- #


def _read_execution_manifest(execution_root: Path | str) -> dict[str, Any]:
    path = Path(execution_root) / g11_runner.G11_EXECUTION_MANIFEST_FILENAME
    document = _require_mapping(
        _read_json(path, "G11 execution manifest"), "G11 execution manifest"
    )
    check = copy.deepcopy(document)
    stored = check.pop("manifest_sha256", None)
    _require_sha256(stored, "G11 execution manifest SHA")
    if _sha256_json(check) != stored:
        raise G11FinalFreezeError("G11 execution manifest SHA does not recompute")
    if document.get("benchmark_id") != G11_BENCHMARK_ID:
        raise G11FinalFreezeError("G11 execution manifest binds another benchmark")
    if document.get("g11_preregistration_sha256") != G11_PREREGISTRATION_SHA256:
        raise G11FinalFreezeError(
            "G11 execution manifest binds another preregistration"
        )
    return document


def _ledger_derived(document: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic metrics read directly from the durable ledger document."""
    reservations = [
        record for record in document.get("reservations", []) if isinstance(record, Mapping)
    ]
    attempts = [
        record for record in document.get("attempts", []) if isinstance(record, Mapping)
    ]
    status_counts = Counter(str(record.get("status")) for record in reservations)
    outcome_counts = Counter(str(record.get("outcome")) for record in attempts)
    operation_counts = Counter(str(record.get("operation")) for record in reservations)
    elapsed = Decimal(0)
    for record in attempts:
        try:
            elapsed += Decimal(str(record.get("elapsed_seconds", 0)))
        except (InvalidOperation, TypeError, ValueError) as exc:  # pragma: no cover
            raise G11FinalFreezeError("ledger attempt elapsed value is invalid") from exc
    return {
        "reservation_status_counts": dict(sorted(status_counts.items())),
        "attempt_outcome_counts": dict(sorted(outcome_counts.items())),
        "operation_kind_counts": dict(sorted(operation_counts.items())),
        "total_attempt_elapsed_seconds": _decimal_text(elapsed),
    }


def read_model_ledgers(execution_root: Path | str) -> dict[str, Any]:
    """Read each physical model-level ledger exactly ONCE.

    Two tracks share one physical ledger per model.  This function opens the
    single physical ledger file for each model and never adds a duplicate
    track-level copy of the same ledger summary.
    """
    root = Path(execution_root).resolve()
    result: dict[str, Any] = {}
    for model_id in MODEL_ORDER:
        path = g11_runner.ledger_path(root, model_id)
        if not path.is_file():
            raise G11FinalFreezeError(
                f"G11 model ledger is missing for {model_id!r}: {path}"
            )
        ledger = g11_runner.G11Ledger.open(path=path, model_id=model_id)
        result[model_id] = {
            "model_id": model_id,
            "ledger_path": f"{g11_runner.G11_LEDGER_DIRECTORY}/"
            f"{MODEL_SLUGS[model_id]}.json",
            "file_sha256": _sha256_file(path),
            "summary": ledger.summary(),
            "derived_from_durable_ledger": _ledger_derived(ledger.document),
        }
    return result


def _journal_summary(track_root_path: Path) -> dict[str, Any]:
    """Use the production journal validator; malformed evidence must fail closed."""
    directory = Path(track_root_path) / "data" / "provider_request_journal"
    inspection = inspect_paid_request_journals(directory)
    records = [
        record
        for record in inspection.get("records", [])
        if isinstance(record, Mapping)
    ]
    errors = [str(item) for item in inspection.get("errors", [])]
    return {
        "records": len(records),
        "started": sum(1 for record in records if record.get("status") == "started"),
        "uncertain": sum(
            1 for record in records if record.get("status") == "uncertain"
        ),
        "errors": errors,
    }


def _audit_recovery_evidence(
    track_root_path: Path, start_chapter: int, end_chapter: int
) -> list[dict[str, Any]]:
    directory = g11_runner.audit_content_recovery_directory(
        track_root_path, start_chapter, end_chapter
    )
    if not directory.is_dir():
        return []
    evidence: list[dict[str, Any]] = []
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not item.is_dir() or not item.name.startswith("cycle_"):
            continue
        manifest_path = item / "manifest.json"
        if not manifest_path.is_file():
            raise G11FinalFreezeError(
                f"G11 audit-recovery cycle is missing its manifest: {item}"
            )
        evidence.append(
            {
                "cycle": item.name,
                "manifest_sha256": _sha256_file(manifest_path),
            }
        )
    return evidence


def _audit_record_path(
    track_root_path: Path, start_chapter: int, end_chapter: int
) -> Path:
    return (
        Path(track_root_path)
        / "output"
        / "audits"
        / f"audit_{start_chapter:04d}_{end_chapter:04d}.json"
    )


def _audit_window_evidence(
    *,
    track_root_path: Path,
    run_id: str,
    start_chapter: int,
    end_chapter: int,
    audit_history_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one successfully committed production audit window.

    Production has a legitimate zero-candidate fast path: call_periodic_audit
    returns before run_chunked_audit, so no chunk checkpoint is created.  The
    committed audit record, audit history, and progress marker remain the
    authoritative durable proof.  Candidate-bearing audits additionally require
    a completed chunk checkpoint bound to the same window.
    """
    expected_window = {
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
    }
    audit_path = _audit_record_path(
        track_root_path, start_chapter, end_chapter
    )
    audit_record = _require_mapping(
        _read_json(
            audit_path,
            f"{run_id} audit record {start_chapter}-{end_chapter}",
        ),
        f"{run_id} audit record {start_chapter}-{end_chapter}",
    )
    if (
        audit_record.get("start_chapter") != start_chapter
        or audit_record.get("end_chapter") != end_chapter
    ):
        raise G11FinalFreezeError(
            f"{run_id} audit record {start_chapter}-{end_chapter} "
            "binds another audit window"
        )

    history_matches = [
        dict(item)
        for item in audit_history_records
        if (
            item.get("start_chapter") == start_chapter
            and item.get("end_chapter") == end_chapter
        )
    ]
    if len(history_matches) != 1:
        raise G11FinalFreezeError(
            f"{run_id} audit history does not contain exactly one "
            f"{start_chapter}-{end_chapter} audit record"
        )
    if history_matches[0] != audit_record:
        raise G11FinalFreezeError(
            f"{run_id} audit file/history mismatch for "
            f"{start_chapter}-{end_chapter}"
        )

    chunking = _require_mapping(
        audit_record.get("chunking"),
        f"{run_id} audit chunking {start_chapter}-{end_chapter}",
    )
    candidate_count = _require_int(
        chunking.get("candidate_count"),
        f"{run_id} audit candidate_count {start_chapter}-{end_chapter}",
    )
    total_chunks = _require_int(
        chunking.get("total_chunks"),
        f"{run_id} audit total_chunks {start_chapter}-{end_chapter}",
    )
    completed_chunks = chunking.get("completed_chunks")
    if not isinstance(completed_chunks, list):
        raise G11FinalFreezeError(
            f"{run_id} audit completed_chunks {start_chapter}-{end_chapter} "
            "must be a list"
        )
    if candidate_count < 0 or total_chunks < 0:
        raise G11FinalFreezeError(
            f"{run_id} audit counts {start_chapter}-{end_chapter} "
            "must be non-negative"
        )

    checkpoint_path = g11_runner.g11_audit_checkpoint_path(
        track_root_path, start_chapter, end_chapter
    )
    checkpoint_present = checkpoint_path.is_file()

    if candidate_count == 0:
        if total_chunks != 0 or completed_chunks:
            raise G11FinalFreezeError(
                f"{run_id} zero-candidate audit {start_chapter}-{end_chapter} "
                "has contradictory chunk accounting"
            )
        return {
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
            "audit_record_sha256": _sha256_file(audit_path),
            "candidate_count": 0,
            "total_chunks": 0,
            "completed_chunks": [],
            "completion_basis": "committed_zero_candidate_audit_record",
            "checkpoint_present": checkpoint_present,
            "checkpoint_sha256": (
                _sha256_file(checkpoint_path) if checkpoint_present else None
            ),
        }

    if total_chunks < 1:
        raise G11FinalFreezeError(
            f"{run_id} candidate-bearing audit {start_chapter}-{end_chapter} "
            "has no chunks"
        )
    if len(completed_chunks) != total_chunks:
        raise G11FinalFreezeError(
            f"{run_id} audit {start_chapter}-{end_chapter} "
            "does not report all chunks completed"
        )
    if not checkpoint_present:
        raise G11FinalFreezeError(
            f"{run_id} candidate-bearing audit checkpoint "
            f"{start_chapter}-{end_chapter} is missing: {checkpoint_path}"
        )

    checkpoint = _require_mapping(
        _read_json(
            checkpoint_path,
            f"{run_id} audit checkpoint {start_chapter}-{end_chapter}",
        ),
        f"{run_id} audit checkpoint {start_chapter}-{end_chapter}",
    )
    if checkpoint.get("audit_window") != expected_window:
        raise G11FinalFreezeError(
            f"{run_id} audit checkpoint {start_chapter}-{end_chapter} "
            "binds another audit window"
        )
    if checkpoint.get("status") != "completed":
        raise G11FinalFreezeError(
            f"{run_id} audit checkpoint {start_chapter}-{end_chapter} "
            "is not completed"
        )

    leaf_chunks = [
        item
        for item in checkpoint.get("chunks", [])
        if isinstance(item, Mapping) and item.get("status") != "split"
    ]
    completed_leaf_ids = [
        str(item.get("chunk_id"))
        for item in leaf_chunks
        if item.get("status") == "completed"
    ]
    if len(leaf_chunks) != total_chunks:
        raise G11FinalFreezeError(
            f"{run_id} audit checkpoint {start_chapter}-{end_chapter} "
            "leaf-chunk count differs from committed audit record"
        )
    if sorted(completed_leaf_ids) != sorted(str(item) for item in completed_chunks):
        raise G11FinalFreezeError(
            f"{run_id} audit checkpoint {start_chapter}-{end_chapter} "
            "completed-chunk roster differs from committed audit record"
        )

    return {
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "audit_record_sha256": _sha256_file(audit_path),
        "candidate_count": candidate_count,
        "total_chunks": total_chunks,
        "completed_chunks": [str(item) for item in completed_chunks],
        "completion_basis": "committed_audit_record_and_completed_chunk_checkpoint",
        "checkpoint_present": True,
        "checkpoint_sha256": _sha256_file(checkpoint_path),
    }


def _track_record(
    execution_root: Path,
    track: Mapping[str, Any],
    tree: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = track["run_id"]
    root = g11_runner.track_root(execution_root, run_id)
    progress = g11_runner.read_track_progress(root)
    quarantine = g11_runner.load_quarantine(root)
    classification = g11_runner.classify_track_progress(
        progress, quarantine=quarantine, expected_run_id=run_id
    )

    low = g11_runner.track_adaptive_low_summary(root)
    marker = g11_runner.load_deepseek_low_mode(
        g11_runner.deepseek_low_mode_path(root)
    )
    saturation_events = (
        list(marker.get("high_effort_saturation_events", [])) if marker else []
    )

    evidence_root = (
        Path(execution_root) / g11_runner.G11_EVIDENCE_DIRECTORY / run_id
    )
    frozen_checkpoints = (
        sorted(
            int(item.name)
            for item in evidence_root.iterdir()
            if item.is_dir() and item.name.isdigit()
        )
        if evidence_root.is_dir()
        else []
    )

    checkpoint_flags: dict[str, Any] = {}
    for chapter in LITERARY_CHECKPOINTS:
        checkpoint_path = g11_runner.g11_evidence_checkpoint_path(
            execution_root, run_id, chapter
        ) / "checkpoint.json"
        document = _require_mapping(
            _read_json(checkpoint_path, f"{run_id} Ch{chapter} checkpoint"),
            f"{run_id} Ch{chapter} checkpoint",
        )
        if document.get("run_id") != run_id or document.get("chapter") != chapter:
            raise G11FinalFreezeError(
                f"{run_id} Ch{chapter} checkpoint binds another chapter"
            )
        if document.get("quality_checkpoint") is not True:
            raise G11FinalFreezeError(
                f"{run_id} Ch{chapter} checkpoint is not a quality checkpoint"
            )
        checkpoint_flags[str(chapter)] = {
            "quality_checkpoint": True,
            "state_evidence_checkpoint": bool(
                document.get("state_evidence_checkpoint")
            ),
        }

    audit_history_path = root / "data" / "audit_history.json"
    audit_history = _require_mapping(
        _read_json(audit_history_path, f"{run_id} audit history"),
        f"{run_id} audit history",
    )
    audit_history_records = audit_history.get("audits")
    if not isinstance(audit_history_records, list):
        raise G11FinalFreezeError(f"{run_id} audit history has no audits list")
    if len(audit_history_records) != len(AUDIT_WINDOWS):
        raise G11FinalFreezeError(
            f"{run_id} audit history does not contain exactly "
            f"{len(AUDIT_WINDOWS)} scheduled audits"
        )
    if not all(isinstance(item, Mapping) for item in audit_history_records):
        raise G11FinalFreezeError(
            f"{run_id} audit history contains a non-object audit record"
        )

    audit_windows = [
        _audit_window_evidence(
            track_root_path=root,
            run_id=run_id,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            audit_history_records=audit_history_records,
        )
        for start_chapter, end_chapter in AUDIT_WINDOWS
    ]

    audit_history_sha256 = _sha256_file(audit_history_path)

    audit_recovery: dict[str, int] = {}
    audit_recovery_evidence: dict[str, Any] = {}
    for start_chapter, end_chapter in AUDIT_WINDOWS:
        key = f"{start_chapter:04d}_{end_chapter:04d}"
        audit_recovery[key] = g11_runner.count_audit_content_recovery_cycles(
            root, start_chapter, end_chapter
        )
        audit_recovery_evidence[key] = _audit_recovery_evidence(
            root, start_chapter, end_chapter
        )
    final_recovery = audit_recovery[
        f"{FINAL_AUDIT_WINDOW[0]:04d}_{FINAL_AUDIT_WINDOW[1]:04d}"
    ]

    return {
        "run_id": run_id,
        "run_index": track["run_index"],
        "model_id": track["model_id"],
        "novel_id": track["novel_id"],
        "profile_id": track["profile_id"],
        "progress": {
            "last_completed": progress.get("last_completed"),
            "next_chapter": progress.get("next_chapter"),
            "last_audited_chapter": progress.get("last_audited_chapter"),
        },
        "classification": classification,
        "quarantine": quarantine,
        "literary_checkpoints": list(LITERARY_CHECKPOINTS),
        "state_checkpoints": list(STATE_CHECKPOINTS),
        "audit_boundaries": list(AUDIT_BOUNDARIES),
        "frozen_checkpoints": frozen_checkpoints,
        "checkpoint_flags": checkpoint_flags,
        "content_recovery_cycles": g11_runner.count_content_recovery_cycles(root),
        "final_audit_content_recovery_cycles": final_recovery,
        "audit_content_recovery_cycles": audit_recovery,
        "audit_content_recovery_evidence": audit_recovery_evidence,
        "adaptive_low_translation_mode": low,
        "high_effort_saturation_events": saturation_events,
        "audit_evidence": {
            "windows": audit_windows,
            "audit_history_sha256": audit_history_sha256,
        },
        "journals": _journal_summary(root),
        "track_tree": _manifest_subset(tree, f"runs/{run_id}/"),
    }


def _validate_track_completion(record: Mapping[str, Any], track: Mapping[str, Any]) -> None:
    run_id = track["run_id"]
    classification = record["classification"]
    if classification.get("status") != "complete":
        raise G11FinalFreezeError(
            f"{run_id} is not complete: {classification.get('status')!r}"
        )
    progress = record["progress"]
    if progress.get("last_completed") != LAST_CHAPTER:
        raise G11FinalFreezeError(
            f"{run_id} last_completed is not {LAST_CHAPTER}"
        )
    if progress.get("next_chapter") != NEXT_CHAPTER_AFTER_COMPLETION:
        raise G11FinalFreezeError(f"{run_id} next_chapter is not 101")
    if progress.get("last_audited_chapter") != LAST_CHAPTER:
        raise G11FinalFreezeError(
            f"{run_id} last_audited_chapter is not {LAST_CHAPTER}"
        )
    if record["quarantine"] is not None:
        raise G11FinalFreezeError(f"{run_id} carries an active quarantine")
    if record["frozen_checkpoints"] != list(LITERARY_CHECKPOINTS):
        raise G11FinalFreezeError(
            f"{run_id} literary checkpoint roster is not the exact 14 checkpoints"
        )
    for chapter in STATE_CHECKPOINTS:
        flags = record["checkpoint_flags"].get(str(chapter))
        if not flags or flags.get("state_evidence_checkpoint") is not True:
            raise G11FinalFreezeError(
                f"{run_id} Ch{chapter} state checkpoint evidence is missing"
            )
    if record["journals"].get("errors"):
        raise G11FinalFreezeError(
            f"{run_id} has corrupt or unexpected provider journal evidence: "
            f"{record['journals']['errors']}"
        )
    if record["journals"]["started"] or record["journals"]["uncertain"]:
        raise G11FinalFreezeError(f"{run_id} has unresolved provider journals")

    low = record["adaptive_low_translation_mode"]
    if track["model_id"] == DEEPSEEK_MODEL_ID:
        if low.get("activated") is not True:
            raise G11FinalFreezeError(
                f"{run_id} DeepSeek adaptive low mode is not activated"
            )
        if low.get("activation_chapter") != DEEPSEEK_ADAPTIVE_LOW_ACTIVATION_CHAPTER:
            raise G11FinalFreezeError(
                f"{run_id} DeepSeek adaptive low activation chapter is not 4"
            )
    else:
        if low.get("activated") is not False:
            raise G11FinalFreezeError(
                f"{run_id} GLM must not be marked adaptive-low"
            )


def _completion_summary(tracks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    canonical = sum(int(track["progress"]["last_completed"]) for track in tracks)
    scheduled_audits = sum(len(track["audit_evidence"]["windows"]) for track in tracks)

    literary_matched = 0
    state_matched = 0
    for novel_id in NOVEL_ORDER:
        novel_tracks = [track for track in tracks if track["novel_id"] == novel_id]
        if len(novel_tracks) != len(MODEL_ORDER):
            raise G11FinalFreezeError(f"novel {novel_id} does not have both tracks")
        for chapter in LITERARY_CHECKPOINTS:
            if all(chapter in track["frozen_checkpoints"] for track in novel_tracks):
                literary_matched += 1
        for chapter in STATE_CHECKPOINTS:
            if all(
                track["checkpoint_flags"].get(str(chapter), {}).get(
                    "state_evidence_checkpoint"
                )
                is True
                for track in novel_tracks
            ):
                state_matched += 1

    return {
        "canonical_chapter_translations": canonical,
        "canonical_chapter_translations_expected": EXPECTED_CANONICAL_CHAPTERS,
        "scheduled_audits": scheduled_audits,
        "scheduled_audits_expected": EXPECTED_SCHEDULED_AUDITS,
        "literary_matched_checkpoints": literary_matched,
        "literary_matched_checkpoints_expected": EXPECTED_LITERARY_MATCHED,
        "state_matched_checkpoints": state_matched,
        "state_matched_checkpoints_expected": EXPECTED_STATE_MATCHED,
    }


def _assert_completion_exact(completion: Mapping[str, Any]) -> None:
    exact = {
        "canonical_chapter_translations": EXPECTED_CANONICAL_CHAPTERS,
        "scheduled_audits": EXPECTED_SCHEDULED_AUDITS,
        "literary_matched_checkpoints": EXPECTED_LITERARY_MATCHED,
        "state_matched_checkpoints": EXPECTED_STATE_MATCHED,
    }
    for key, wanted in exact.items():
        if completion.get(key) != wanted:
            raise G11FinalFreezeError(
                f"G11 completion {key} is {completion.get(key)!r}, expected {wanted}"
            )


def build_execution_evidence(execution_root: Path | str) -> dict[str, Any]:
    """Extract every durable frozen fact from an execution tree (read-only)."""
    root = Path(execution_root).resolve()
    manifest = _read_execution_manifest(root)
    tree = physical_tree_manifest(root)
    model_ledgers = read_model_ledgers(root)

    tracks: list[dict[str, Any]] = []
    for track in TRACKS:
        tracks.append(_track_record(root, track, tree))
    for track, record in zip(TRACKS, tracks):
        _validate_track_completion(record, track)

    completion = _completion_summary(tracks)
    _assert_completion_exact(completion)

    return {
        "execution_root": str(root),
        "manifest": manifest,
        "tree": tree,
        "model_ledgers": model_ledgers,
        "tracks": tracks,
        "completion": completion,
    }


# --------------------------------------------------------------------------- #
# Predecessor / source bindings
# --------------------------------------------------------------------------- #


def _repository_head(repo_root: Path | str) -> str:
    import subprocess

    repo = Path(repo_root).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git unavailable
        raise G11FinalFreezeError(f"cannot resolve repository HEAD: {exc}") from exc
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        raise G11FinalFreezeError("repository HEAD cannot be resolved")
    return head


def _source_bindings(repo_root: Path) -> dict[str, Any]:
    repo_root = Path(repo_root).expanduser().resolve()
    module_root = Path(__file__).resolve().parent
    if repo_root != module_root:
        raise G11FinalFreezeError(
            "G11 final-freeze --repo must be the checkout that contains "
            "the executing final-freeze module"
        )
    prereg_path = repo_root / "benchmark_longitudinal_g11_preregistration.py"
    runner_path = repo_root / "benchmark_longitudinal_g11_runner.py"
    identity = g11_preregistration_identity()
    if identity["provider_calls"] != 0 or identity["credential_access"] is not False:
        raise G11FinalFreezeError("G11 preregistration is not a zero-provider design")
    if _sha256_json(identity) != G11_PREREGISTRATION_SHA256:
        raise G11FinalFreezeError("G11 preregistration identity does not recompute")
    return {
        "worktree_head": _repository_head(repo_root),
        "preregistration": {
            "revision": G11_PREREGISTRATION_REVISION,
            "sha256": G11_PREREGISTRATION_SHA256,
            "file_sha256": file_sha256(prereg_path),
        },
        "runner": {
            "revision": g11_runner.G11_RUNNER_REVISION,
            "file_sha256": _sha256_file(runner_path),
        },
    }


def _historical_execution_binding(
    execution_root: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the historical paid-execution repository anchor from durable evidence.

    The paid execution predates this final-freeze commit.  The durable run
    identities bind ``G11_PREREGISTRATION_SHA256``, and the frozen preregistration
    carries its own ``repository_anchor``.  That anchor is the historical
    paid-execution repository identity and is deliberately kept distinct from the
    final-freeze worktree HEAD.
    """
    for track in TRACKS:
        identity = _require_mapping(
            _read_json(
                g11_runner.track_root(execution_root, track["run_id"])
                / g11_runner.G11_RUN_IDENTITY_FILENAME,
                "G11 run identity",
            ),
            "G11 run identity",
        )
        if identity.get("g11_preregistration_sha256") != G11_PREREGISTRATION_SHA256:
            raise G11FinalFreezeError(
                f"{track['run_id']} run identity binds another preregistration"
            )

    source_binding_proof = manifest.get("source_bindings_verified")
    expected_binding_proof = {
        "verified": True,
        "binding_count": len(SOURCE_BINDINGS),
    }
    if source_binding_proof != expected_binding_proof:
        raise G11FinalFreezeError(
            "G11 historical execution manifest does not carry the exact "
            "verified source-binding proof"
        )

    return {
        "repository_anchor": G11_REPOSITORY_ANCHOR,
        "preregistration_sha256": G11_PREREGISTRATION_SHA256,
        "execution_manifest_sha256": manifest.get("manifest_sha256"),
        "source_bindings_verified": copy.deepcopy(expected_binding_proof),
        "distinct_from_final_freeze_head": True,
    }


# --------------------------------------------------------------------------- #
# Freeze document construction
# --------------------------------------------------------------------------- #


def _freeze_payload(
    *,
    sources: Mapping[str, Any],
    historical: Mapping[str, Any],
    execution_verification: Mapping[str, Any],
    manifest_file_sha256: str,
    snapshot_tree: Mapping[str, Any],
    source_tree: Mapping[str, Any],
    tracks: Sequence[Mapping[str, Any]],
    model_ledgers: Mapping[str, Any],
    completion: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": G11F_SCHEMA_VERSION,
        "revision": G11F_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "repository": {
            "worktree_head": sources["worktree_head"],
            "historical_paid_execution": dict(historical),
        },
        "preregistration": dict(sources["preregistration"]),
        "runner": dict(sources["runner"]),
        "execution": {
            "snapshot_directory": EXECUTION_SNAPSHOT_DIRECTORY,
            "manifest_filename": EXECUTION_MANIFEST_FILENAME,
            "source_tree_sha256": source_tree["sha256"],
            "source_file_count": source_tree["file_count"],
            "source_byte_count": source_tree["byte_count"],
            "snapshot_tree_sha256": snapshot_tree["sha256"],
            "snapshot_file_count": snapshot_tree["file_count"],
            "snapshot_byte_count": snapshot_tree["byte_count"],
            "execution_tree_manifest_file_sha256": manifest_file_sha256,
            "execution_tree_manifest_sha256": snapshot_tree["sha256"],
            "source_equals_snapshot": source_tree["sha256"] == snapshot_tree["sha256"],
        },
        "execution_verification": dict(execution_verification),
        "shared_ledger_deduplicated": True,
        "model_ledgers": {
            model_id: copy.deepcopy(model_ledgers[model_id])
            for model_id in MODEL_ORDER
        },
        "tracks": [copy.deepcopy(dict(track)) for track in tracks],
        "completion": dict(completion),
        "provider_calls": 0,
        "credential_access": False,
        "blind_bundle_created": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }


def _freeze_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(dict(payload))
    return {**body, "freeze_id": _sha256_json(body)}


def _rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


def _summary_text(document: Mapping[str, Any]) -> str:
    execution = document["execution"]
    completion = document["completion"]
    lines: list[str] = [
        "G11 FINAL FREEZE",
        f"freeze_id: {document['freeze_id']}",
        f"benchmark_id: {document['benchmark_id']}",
        f"worktree_head: {document['repository']['worktree_head']}",
        "historical_paid_execution_anchor: "
        f"{document['repository']['historical_paid_execution']['repository_anchor']}",
        f"execution_tree_sha256: {execution['snapshot_tree_sha256']}",
        f"execution_file_count: {execution['snapshot_file_count']}",
        f"execution_byte_count: {execution['snapshot_byte_count']}",
        "",
        "COMPLETION",
        "  canonical_chapter_translations: "
        f"{completion['canonical_chapter_translations']} / 400",
        f"  scheduled_audits: {completion['scheduled_audits']} / 16",
        "  literary_matched_checkpoints: "
        f"{completion['literary_matched_checkpoints']} / 28",
        "  state_matched_checkpoints: "
        f"{completion['state_matched_checkpoints']} / 8",
        "",
        "MODEL LEDGERS (one physical ledger per model; two tracks share it, never summed)",
    ]
    for model_id in MODEL_ORDER:
        ledger = document["model_ledgers"][model_id]
        summary = ledger["summary"]
        lines.extend(
            [
                f"  model: {model_id}",
                f"    ledger: {ledger['ledger_path']}",
                f"    ceiling_credit_equivalent: {summary['ceiling_credit_equivalent']}",
                "    committed_credit_equivalent: "
                f"{summary['committed_credit_equivalent']}",
                "    remaining_credit_equivalent: "
                f"{summary['remaining_credit_equivalent']}",
                f"    operations: {summary['operations']}",
                f"    settled_operations: {summary['settled_operations']}",
                f"    unresolved_operations: {summary['unresolved_operations']}",
                f"    transport_attempts: {summary['transport_attempts']}",
                f"    retries: {summary['retries']}",
                f"    regenerations: {summary['regenerations']}",
                "    first_attempt_success_rate: "
                f"{_rate(summary['first_attempt_success_rate'])}",
                f"    eventual_success_rate: {_rate(summary['eventual_success_rate'])}",
            ]
        )
    lines.extend(["", "TRACKS"])
    for track in document["tracks"]:
        low = track["adaptive_low_translation_mode"]
        lines.extend(
            [
                f"  run_id: {track['run_id']}",
                f"    model_id: {track['model_id']}",
                f"    novel_id: {track['novel_id']}",
                f"    last_completed: {track['progress']['last_completed']}",
                f"    last_audited_chapter: {track['progress']['last_audited_chapter']}",
                "    quarantine: "
                f"{'present' if track['quarantine'] is not None else 'none'}",
                f"    literary_checkpoints: {len(track['literary_checkpoints'])}",
                f"    state_checkpoints: {len(track['state_checkpoints'])}",
                f"    content_recovery_cycles: {track['content_recovery_cycles']}",
                "    final_audit_content_recovery_cycles: "
                f"{track['final_audit_content_recovery_cycles']}",
                "    adaptive_low: activated="
                f"{low['activated']} chapter={low['activation_chapter']}",
                "    track_tree_sha256: "
                f"{track['track_tree']['sha256']}",
            ]
        )
    lines.extend(
        [
            "",
            "SAFETY",
            "  provider_calls: 0",
            "  credential_access: false",
            "  blind_bundle_created: false",
            "  scoring_performed: false",
            "  identity_revealed: false",
            "  paid_execution_authorized: false",
        ]
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# create / verify
# --------------------------------------------------------------------------- #


def _execution_verification_subset(verification: Mapping[str, Any]) -> dict[str, Any]:
    proof = verification.get("pristine_proof")
    subset = {
        "frozen_layout_verified": verification.get("frozen_layout_verified"),
        "residual_repair_enabled_in_all_copies": verification.get(
            "residual_repair_enabled_in_all_copies"
        ),
        "track_count": verification.get("track_count"),
        "chapters_per_track": verification.get("chapters_per_track"),
        "pristine_unchanged": None,
        "pristine_tree_sha256": None,
        "provider_calls": verification.get("provider_calls"),
    }
    if isinstance(proof, Mapping):
        subset["pristine_unchanged"] = proof.get("unchanged")
        subset["pristine_tree_sha256"] = proof.get("tree_sha256")
    return subset


def create_final_freeze(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    pristine_root: Path | str,
    production_root: Path | str | None = None,
    output_root: Path | str,
) -> dict[str, Any]:
    """Create the immutable G11 final freeze exactly once."""
    output = _assert_output_root_disjoint(
        Path(output_root),
        {
            "repository": repo_root,
            "execution": execution_root,
            "pristine": pristine_root,
            "production": production_root,
        },
    )
    _assert_output_root_available(output)

    repo = Path(repo_root).expanduser().resolve()
    execution = Path(execution_root).expanduser().resolve()
    pristine = Path(pristine_root).expanduser().resolve()
    production = (
        None if production_root is None else Path(production_root).expanduser().resolve()
    )

    sources = _source_bindings(repo)
    verification = g11_runner.verify_g11(
        execution_root=execution,
        pristine_root=pristine,
        production_root=production,
        forbidden_roots=(repo,),
    )
    evidence = build_execution_evidence(execution)
    manifest = evidence["manifest"]
    historical = _historical_execution_binding(execution, manifest)

    source_tree = evidence["tree"]

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        snapshot = copy_physical_tree(
            execution, staging / EXECUTION_SNAPSHOT_DIRECTORY
        )
        snapshot_tree = physical_tree_manifest(snapshot)
        if snapshot_tree["sha256"] != source_tree["sha256"]:
            raise G11FinalFreezeError(
                "G11 execution snapshot is not byte-identical to the source"
            )

        manifest_path = staging / EXECUTION_MANIFEST_FILENAME
        atomic_write_json(
            manifest_path,
            {
                "schema_version": G11F_SCHEMA_VERSION,
                "revision": G11F_REVISION,
                "benchmark_id": G11_BENCHMARK_ID,
                "source_tree_sha256": source_tree["sha256"],
                "files": snapshot_tree["files"],
                "file_count": snapshot_tree["file_count"],
                "byte_count": snapshot_tree["byte_count"],
                "sha256": snapshot_tree["sha256"],
                "provider_calls": 0,
            },
        )
        manifest_file_sha256 = _sha256_file(manifest_path)

        # C: the source must be unchanged by the read-only snapshot copy.
        if physical_tree_manifest(execution)["sha256"] != source_tree["sha256"]:
            raise G11FinalFreezeError(
                "G11 execution root changed while snapshotting"
            )

        payload = _freeze_payload(
            sources=sources,
            historical=historical,
            execution_verification=_execution_verification_subset(verification),
            manifest_file_sha256=manifest_file_sha256,
            snapshot_tree=snapshot_tree,
            source_tree=source_tree,
            tracks=evidence["tracks"],
            model_ledgers=evidence["model_ledgers"],
            completion=evidence["completion"],
        )
        document = _freeze_document(payload)
        atomic_write_json(staging / FREEZE_FILENAME, document)
        (staging / SUMMARY_FILENAME).write_text(
            _summary_text(document), encoding="utf-8", newline="\n"
        )

        if output.exists():
            raise G11FinalFreezeSafetyError(
                "G11 final-freeze output root appeared during create-once staging"
            )
        os.rename(staging, output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        "freeze_id": document["freeze_id"],
        "output_root": str(output),
        "execution_tree_sha256": document["execution"]["snapshot_tree_sha256"],
        "model_ids": list(MODEL_ORDER),
        "canonical_chapter_translations": document["completion"][
            "canonical_chapter_translations"
        ],
        "scheduled_audits": document["completion"]["scheduled_audits"],
        "literary_matched_checkpoints": document["completion"][
            "literary_matched_checkpoints"
        ],
        "state_matched_checkpoints": document["completion"][
            "state_matched_checkpoints"
        ],
        "provider_calls": 0,
        "credential_access": False,
        "blind_bundle_created": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }


def _assert_safety_flags(document: Mapping[str, Any]) -> None:
    expected = {
        "provider_calls": 0,
        "credential_access": False,
        "blind_bundle_created": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }
    for key, wanted in expected.items():
        if document.get(key) != wanted:
            raise G11FinalFreezeSafetyError(
                f"G11 final-freeze safety flag {key} is not {wanted!r}"
            )


def verify_final_freeze(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    pristine_root: Path | str,
    production_root: Path | str | None = None,
    output_root: Path | str,
) -> dict[str, Any]:
    """Independently re-check the G11 final freeze, read-only, zero provider."""
    output = Path(output_root).expanduser().resolve()
    freeze_path = output / FREEZE_FILENAME
    document = _require_mapping(
        _read_json(freeze_path, "G11 final-freeze document"),
        "G11 final-freeze document",
    )
    freeze_id = document.get("freeze_id")
    payload = {key: value for key, value in document.items() if key != "freeze_id"}
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G11FinalFreezeError("G11 final-freeze ID does not recompute")
    if document.get("schema_version") != G11F_SCHEMA_VERSION:
        raise G11FinalFreezeError("G11 final-freeze schema changed")
    if document.get("revision") != G11F_REVISION:
        raise G11FinalFreezeError("G11 final-freeze revision changed")
    if document.get("benchmark_id") != G11_BENCHMARK_ID:
        raise G11FinalFreezeError("G11 final-freeze binds another benchmark")
    _assert_safety_flags(document)

    snapshot = output / EXECUTION_SNAPSHOT_DIRECTORY
    if not snapshot.is_dir():
        raise G11FinalFreezeError("G11 execution snapshot directory is missing")

    stored_manifest = _require_mapping(
        _read_json(output / EXECUTION_MANIFEST_FILENAME, "G11 execution manifest"),
        "G11 execution manifest",
    )
    stored_records = stored_manifest.get("files")
    if not isinstance(stored_records, list):
        raise G11FinalFreezeError("G11 execution manifest file records are invalid")
    if stored_manifest.get("sha256") != _tree_sha256(stored_records):
        raise G11FinalFreezeError("G11 execution manifest tree SHA is invalid")
    if stored_manifest.get("file_count") != len(stored_records):
        raise G11FinalFreezeError("G11 execution manifest file count is invalid")
    if stored_manifest.get("byte_count") != sum(
        int(record["bytes"]) for record in stored_records
    ):
        raise G11FinalFreezeError("G11 execution manifest byte count is invalid")

    recomputed_snapshot = physical_tree_manifest(snapshot)
    if recomputed_snapshot["files"] != stored_records:
        raise G11FinalFreezeError(
            "G11 execution snapshot differs from the frozen manifest "
            "(byte tampering or missing/extra files)"
        )

    execution = document["execution"]
    if stored_manifest["sha256"] != execution["snapshot_tree_sha256"]:
        raise G11FinalFreezeError("G11 execution manifest tree SHA drifted")
    if stored_manifest["file_count"] != execution["snapshot_file_count"]:
        raise G11FinalFreezeError("G11 execution manifest file count drifted")
    if stored_manifest["byte_count"] != execution["snapshot_byte_count"]:
        raise G11FinalFreezeError("G11 execution manifest byte count drifted")
    if execution["source_tree_sha256"] != execution["snapshot_tree_sha256"]:
        raise G11FinalFreezeError("G11 frozen source/snapshot identity drifted")
    if _sha256_file(output / EXECUTION_MANIFEST_FILENAME) != execution[
        "execution_tree_manifest_file_sha256"
    ]:
        raise G11FinalFreezeError("G11 execution manifest file changed")

    # Rebuild every frozen fact from the snapshot; any ledger/evidence/checkpoint
    # tampering fails these exact comparisons.
    snapshot_evidence = build_execution_evidence(snapshot)
    if snapshot_evidence["model_ledgers"] != document["model_ledgers"]:
        raise G11FinalFreezeError("G11 model-ledger evidence drifted or was tampered")
    if snapshot_evidence["tracks"] != document["tracks"]:
        raise G11FinalFreezeError("G11 per-track evidence drifted or was tampered")
    if snapshot_evidence["completion"] != document["completion"]:
        raise G11FinalFreezeError("G11 completion evidence drifted")

    # Live execution must still match the frozen source identity (declared-hash
    # drift) and must still satisfy the G11 layout/effort/isolation proof.
    repo = Path(repo_root).expanduser().resolve()
    live_execution = Path(execution_root).expanduser().resolve()
    pristine = Path(pristine_root).expanduser().resolve()
    production = (
        None if production_root is None else Path(production_root).expanduser().resolve()
    )
    sources = _source_bindings(repo)
    if sources["worktree_head"] != document["repository"]["worktree_head"]:
        raise G11FinalFreezeError("G11 worktree HEAD drifted since the freeze")
    if sources["preregistration"] != document["preregistration"]:
        raise G11FinalFreezeError("G11 preregistration binding drifted")
    if sources["runner"] != document["runner"]:
        raise G11FinalFreezeError("G11 runner binding drifted")

    live_tree = physical_tree_manifest(live_execution)
    if live_tree["sha256"] != execution["source_tree_sha256"]:
        raise G11FinalFreezeError(
            "G11 live execution root drifted from the frozen source identity"
        )
    verification = g11_runner.verify_g11(
        execution_root=live_execution,
        pristine_root=pristine,
        production_root=production,
        forbidden_roots=(repo,),
    )
    if _execution_verification_subset(verification) != document[
        "execution_verification"
    ]:
        raise G11FinalFreezeError("G11 execution verification binding drifted")

    if any(
        item.name.startswith(f".{output.name}.creating-")
        for item in output.parent.iterdir()
    ):
        raise G11FinalFreezeError("G11 final-freeze staging directory was left behind")

    return {
        "freeze_id": document["freeze_id"],
        "output_root": str(output),
        "verified": True,
        "execution_tree_sha256": execution["snapshot_tree_sha256"],
        "provider_calls": 0,
        "credential_access": False,
        "blind_bundle_created": False,
        "scoring_performed": False,
        "identity_revealed": False,
    }


def assert_zero_provider_safety() -> None:
    """Fail closed if the frozen final-freeze design is ever weakened."""
    if G11F_REVISION != "g11-final-freeze-v1":
        raise G11FinalFreezeSafetyError("G11 final-freeze revision changed")
    if EXPECTED_CANONICAL_CHAPTERS != 400:
        raise G11FinalFreezeSafetyError("G11 canonical chapter total changed")
    if EXPECTED_TRACK_RUN_IDS != (
        "g11-run01-glm-ace_evolution",
        "g11-run02-deepseek-ace_evolution",
        "g11-run03-glm-rogue_first_prince",
        "g11-run04-deepseek-rogue_first_prince",
    ):
        raise G11FinalFreezeSafetyError("G11 track roster changed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/verify the zero-provider G11 practical FINAL FREEZE."
    )
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--pristine", required=True)
    parser.add_argument("--production", default=None)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {
        "repo_root": args.repo,
        "execution_root": args.execution,
        "pristine_root": args.pristine,
        "production_root": args.production,
        "output_root": args.output,
    }
    if args.action == "create":
        result = create_final_freeze(**kwargs)
    else:
        result = verify_final_freeze(**kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUDIT_BOUNDARIES",
    "AUDIT_WINDOWS",
    "DEEPSEEK_ADAPTIVE_LOW_ACTIVATION_CHAPTER",
    "EXECUTION_MANIFEST_FILENAME",
    "EXECUTION_SNAPSHOT_DIRECTORY",
    "EXPECTED_CANONICAL_CHAPTERS",
    "EXPECTED_LITERARY_MATCHED",
    "EXPECTED_SCHEDULED_AUDITS",
    "EXPECTED_STATE_MATCHED",
    "EXPECTED_TRACK_RUN_IDS",
    "FREEZE_FILENAME",
    "G11FinalFreezeError",
    "G11FinalFreezeSafetyError",
    "G11F_REVISION",
    "G11F_SCHEMA_VERSION",
    "LITERARY_CHECKPOINTS",
    "NOVEL_ORDER",
    "STATE_CHECKPOINTS",
    "SUMMARY_FILENAME",
    "assert_zero_provider_safety",
    "build_execution_evidence",
    "copy_physical_tree",
    "create_final_freeze",
    "physical_tree_manifest",
    "read_model_ledgers",
    "verify_final_freeze",
]
