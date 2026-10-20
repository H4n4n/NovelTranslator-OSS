"""Retry-safe cleanup for already-proven stale staging directories.

This helper does not decide whether a stage is safe to delete. Callers must first
perform their workflow-specific exact proof. Once that proof succeeds, the helper
persists a small proof-bound cleanup journal, renames the proven stage to a unique
cleanup tombstone, and recursively removes the tombstone.

If recursive deletion is interrupted, the journal authorizes completion on a
later retry without pretending that the now-partial tombstone can still satisfy
the original corpus proof. Unknown tombstones, malformed journals, proof drift,
and conflicting filesystem states fail closed.

The journal file is file-fsynced before the stage rename. No parent-directory
fsync or power-loss metadata guarantee is claimed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
REVISION = "proven-stage-cleanup-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _artifact_exists(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def _raise(error_cls: type[Exception], message: str) -> None:
    raise error_cls(message)


def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(f"invalid cleanup namespace: {namespace!r}")


def _validate_stage_prefix(stage_prefix: str) -> None:
    if (
        not isinstance(stage_prefix, str)
        or not stage_prefix
        or "/" in stage_prefix
        or "\\" in stage_prefix
        or stage_prefix in {".", ".."}
    ):
        raise ValueError(f"invalid cleanup stage prefix: {stage_prefix!r}")


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def cleanup_proof_id(namespace: str, identity: Any) -> str:
    """Hash one caller-defined reconstructed identity for cleanup authorization."""
    _validate_namespace(namespace)
    payload = {"namespace": namespace, "identity": identity}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class StageCleanupRecord:
    journal_path: Path
    original_stage: Path
    tombstone: Path
    token: str
    state: str


@dataclass(frozen=True)
class StageCleanupInspection:
    parent: Path
    namespace: str
    stage_prefix: str
    proof_id: str
    current_stage: Path | None
    stage_candidates: tuple[Path, ...]
    records: tuple[StageCleanupRecord, ...]
    prejournal_temps: tuple[Path, ...]

    @property
    def residue_present(self) -> bool:
        return bool(
            self.stage_candidates
            or self.records
            or self.prejournal_temps
        )


def _journal_name(namespace: str, token: str) -> str:
    return f".{namespace}.cleanup-journal-{token}.json"


def _tombstone_name(namespace: str, token: str) -> str:
    return f".{namespace}.cleanup-stage-{token}"


def _prejournal_temp_name(namespace: str, token: str) -> str:
    return f".{namespace}.cleanup-journal-prep-{token}.tmp"


def _record_payload(
    *,
    namespace: str,
    stage_prefix: str,
    proof_id: str,
    token: str,
    original_stage_name: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": REVISION,
        "namespace": namespace,
        "stage_prefix": stage_prefix,
        "proof_id": proof_id,
        "token": token,
        "original_stage_name": original_stage_name,
        "tombstone_name": _tombstone_name(namespace, token),
    }


def _load_record(
    journal_path: Path,
    *,
    parent: Path,
    namespace: str,
    stage_prefix: str,
    proof_id: str,
    error_cls: type[Exception],
) -> StageCleanupRecord:
    if journal_path.is_symlink() or not journal_path.is_file():
        _raise(error_cls, f"ambiguous stage-cleanup journal artifact: {journal_path}")

    raw = journal_path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_cls(f"malformed stage-cleanup journal: {journal_path}") from exc
    if not isinstance(value, dict):
        _raise(error_cls, f"stage-cleanup journal is not an object: {journal_path}")

    expected_keys = {
        "schema_version",
        "revision",
        "namespace",
        "stage_prefix",
        "proof_id",
        "token",
        "original_stage_name",
        "tombstone_name",
    }
    if set(value) != expected_keys:
        _raise(error_cls, f"stage-cleanup journal field set mismatch: {journal_path}")
    if _canonical_json_bytes(value) != raw:
        _raise(error_cls, f"stage-cleanup journal is not canonical: {journal_path}")
    if value["schema_version"] != SCHEMA_VERSION or value["revision"] != REVISION:
        _raise(error_cls, f"unsupported stage-cleanup journal revision: {journal_path}")
    if value["namespace"] != namespace or value["stage_prefix"] != stage_prefix:
        _raise(error_cls, f"stage-cleanup journal workflow mismatch: {journal_path}")
    if value["proof_id"] != proof_id:
        _raise(error_cls, f"stage-cleanup journal proof identity mismatch: {journal_path}")

    token = value["token"]
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        _raise(error_cls, f"invalid stage-cleanup journal token: {journal_path}")
    if journal_path.name != _journal_name(namespace, token):
        _raise(error_cls, f"stage-cleanup journal filename/token mismatch: {journal_path}")

    original_name = value["original_stage_name"]
    if (
        not isinstance(original_name, str)
        or not original_name.startswith(stage_prefix)
        or Path(original_name).name != original_name
    ):
        _raise(error_cls, f"invalid original stage in cleanup journal: {journal_path}")

    expected_tombstone_name = _tombstone_name(namespace, token)
    if value["tombstone_name"] != expected_tombstone_name:
        _raise(error_cls, f"stage-cleanup journal tombstone mismatch: {journal_path}")

    original_stage = parent / original_name
    tombstone = parent / expected_tombstone_name
    original_exists = _artifact_exists(original_stage)
    tombstone_exists = _artifact_exists(tombstone)

    if original_exists and tombstone_exists:
        _raise(
            error_cls,
            "ambiguous stage-cleanup state: original stage and tombstone coexist "
            f"for {journal_path}",
        )
    if tombstone_exists and (tombstone.is_symlink() or not tombstone.is_dir()):
        _raise(error_cls, f"ambiguous stage-cleanup tombstone artifact: {tombstone}")

    if original_exists:
        state = "original"
    elif tombstone_exists:
        state = "tombstone"
    else:
        state = "journal_only"

    return StageCleanupRecord(
        journal_path=journal_path,
        original_stage=original_stage,
        tombstone=tombstone,
        token=token,
        state=state,
    )


def inspect_stage_cleanup(
    parent: Path,
    *,
    namespace: str,
    stage_prefix: str,
    proof_id: str,
    current_stage: Path | None,
    error_cls: type[Exception],
) -> StageCleanupInspection:
    """Inspect cleanup residue without mutating it."""
    _validate_namespace(namespace)
    _validate_stage_prefix(stage_prefix)
    if not isinstance(proof_id, str) or not SHA256_RE.fullmatch(proof_id):
        raise ValueError("cleanup proof_id must be a lowercase SHA-256 hex digest")

    parent = Path(parent)
    current_stage = Path(current_stage) if current_stage is not None else None
    if current_stage is not None and current_stage.parent != parent:
        raise ValueError("current stage must be a direct child of cleanup parent")

    journal_paths = sorted(
        parent.glob(f".{namespace}.cleanup-journal-*.json"),
        key=lambda item: item.name.casefold(),
    )
    prejournal_candidates = sorted(
        parent.glob(f".{namespace}.cleanup-journal-prep-*"),
        key=lambda item: item.name.casefold(),
    )
    prejournal_temps: list[Path] = []
    prep_prefix = f".{namespace}.cleanup-journal-prep-"
    for path in prejournal_candidates:
        name = path.name
        if not name.startswith(prep_prefix) or not name.endswith(".tmp"):
            _raise(
                error_cls,
                f"ambiguous stage-cleanup pre-journal artifact: {path}",
            )
        token = name[len(prep_prefix) : -len(".tmp")]
        if not TOKEN_RE.fullmatch(token):
            _raise(
                error_cls,
                f"invalid stage-cleanup pre-journal token: {path}",
            )
        if path.is_symlink() or not path.is_file():
            _raise(
                error_cls,
                f"ambiguous stage-cleanup pre-journal artifact: {path}",
            )
        prejournal_temps.append(path)

    records = tuple(
        _load_record(
            path,
            parent=parent,
            namespace=namespace,
            stage_prefix=stage_prefix,
            proof_id=proof_id,
            error_cls=error_cls,
        )
        for path in journal_paths
    )

    original_names: set[str] = set()
    tombstone_names: set[str] = set()
    for record in records:
        if record.original_stage.name in original_names:
            _raise(
                error_cls,
                f"duplicate original stage across cleanup journals: {record.original_stage}",
            )
        if record.tombstone.name in tombstone_names:
            _raise(
                error_cls,
                f"duplicate tombstone across cleanup journals: {record.tombstone}",
            )
        original_names.add(record.original_stage.name)
        tombstone_names.add(record.tombstone.name)
        if current_stage is not None and record.original_stage == current_stage:
            _raise(
                error_cls,
                f"current stage is unexpectedly referenced by cleanup journal: {current_stage}",
            )

    actual_tombstones = {
        path.name: path
        for path in sorted(
            parent.glob(f".{namespace}.cleanup-stage-*"),
            key=lambda item: item.name.casefold(),
        )
    }
    unknown_tombstones = sorted(set(actual_tombstones) - tombstone_names)
    if unknown_tombstones:
        _raise(
            error_cls,
            "unproven stage-cleanup tombstone(s): "
            + ", ".join(str(actual_tombstones[name]) for name in unknown_tombstones),
        )

    stage_candidates = tuple(
        path
        for path in sorted(
            parent.glob(f"{stage_prefix}*"),
            key=lambda item: item.name.casefold(),
        )
        if current_stage is None or path != current_stage
    )

    candidate_set = set(stage_candidates)
    for record in records:
        if record.state == "original" and record.original_stage not in candidate_set:
            _raise(
                error_cls,
                f"cleanup journal original stage is outside candidate inventory: {record.original_stage}",
            )

    return StageCleanupInspection(
        parent=parent,
        namespace=namespace,
        stage_prefix=stage_prefix,
        proof_id=proof_id,
        current_stage=current_stage,
        stage_candidates=stage_candidates,
        records=records,
        prejournal_temps=tuple(prejournal_temps),
    )


def _state(record: StageCleanupRecord, error_cls: type[Exception]) -> str:
    original_exists = _artifact_exists(record.original_stage)
    tombstone_exists = _artifact_exists(record.tombstone)
    if original_exists and tombstone_exists:
        _raise(
            error_cls,
            "ambiguous stage-cleanup state: original stage and tombstone coexist "
            f"for {record.journal_path}",
        )
    if tombstone_exists and (
        record.tombstone.is_symlink() or not record.tombstone.is_dir()
    ):
        _raise(
            error_cls,
            f"ambiguous stage-cleanup tombstone artifact: {record.tombstone}",
        )
    if original_exists:
        return "original"
    if tombstone_exists:
        return "tombstone"
    return "journal_only"


def _new_record(stage: Path, inspection: StageCleanupInspection) -> StageCleanupRecord:
    for _attempt in range(128):
        token = uuid.uuid4().hex
        journal = inspection.parent / _journal_name(
            inspection.namespace,
            token,
        )
        tombstone = inspection.parent / _tombstone_name(
            inspection.namespace,
            token,
        )
        prep = inspection.parent / _prejournal_temp_name(
            inspection.namespace,
            token,
        )
        if (
            _artifact_exists(journal)
            or _artifact_exists(tombstone)
            or _artifact_exists(prep)
        ):
            continue

        payload = _record_payload(
            namespace=inspection.namespace,
            stage_prefix=inspection.stage_prefix,
            proof_id=inspection.proof_id,
            token=token,
            original_stage_name=stage.name,
        )
        raw = _canonical_json_bytes(payload)
        published = False
        prep_owned = False
        retry_collision = False
        try:
            handle = prep.open("xb")
            prep_owned = True
            with handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if prep.read_bytes() != raw:
                raise OSError(
                    f"stage-cleanup pre-journal readback mismatch: {prep}"
                )
            if _artifact_exists(journal) or _artifact_exists(tombstone):
                retry_collision = True
            else:
                os.replace(prep, journal)
                published = True
        except FileExistsError:
            retry_collision = True
        finally:
            if (
                prep_owned
                and not published
                and _artifact_exists(prep)
            ):
                prep.unlink()

        if retry_collision:
            continue
        if not published:
            raise OSError(
                f"stage-cleanup journal was not published: {journal}"
            )
        if journal.read_bytes() != raw:
            raise OSError(
                f"stage-cleanup journal readback mismatch: {journal}"
            )
        return StageCleanupRecord(
            journal_path=journal,
            original_stage=stage,
            tombstone=tombstone,
            token=token,
            state="original",
        )
    raise FileExistsError("could not allocate unique stage-cleanup journal")


def _finish_record(
    record: StageCleanupRecord,
    *,
    proven_stages: set[Path],
    expected_state: str,
    error_cls: type[Exception],
) -> None:
    current_state = _state(record, error_cls)
    if current_state != expected_state:
        _raise(
            error_cls,
            "stage-cleanup state changed after inspection: "
            f"{record.journal_path} ({expected_state} -> {current_state})",
        )

    if current_state == "journal_only":
        record.journal_path.unlink()
        return

    if current_state == "original":
        if record.original_stage not in proven_stages:
            _raise(
                error_cls,
                f"refusing cleanup of unproven original stage: {record.original_stage}",
            )
        if record.original_stage.is_symlink() or not record.original_stage.is_dir():
            _raise(
                error_cls,
                f"ambiguous proven-stage cleanup artifact: {record.original_stage}",
            )
        if _artifact_exists(record.tombstone):
            _raise(
                error_cls,
                f"stage-cleanup tombstone appeared before rename: {record.tombstone}",
            )
        os.rename(record.original_stage, record.tombstone)

    if record.tombstone.is_symlink() or not record.tombstone.is_dir():
        _raise(
            error_cls,
            f"stage-cleanup tombstone missing after authorization: {record.tombstone}",
        )
    shutil.rmtree(record.tombstone)
    record.journal_path.unlink()


def cleanup_proven_stages(
    inspection: StageCleanupInspection,
    proven_stages: Iterable[Path],
    *,
    error_cls: type[Exception],
) -> None:
    """Finish prior authorized cleanup and delete every currently proven stage."""
    proven = {Path(path) for path in proven_stages}
    candidates = set(inspection.stage_candidates)
    if proven != candidates:
        missing = sorted(str(path) for path in candidates - proven)
        extra = sorted(str(path) for path in proven - candidates)
        _raise(
            error_cls,
            "stage-cleanup proof inventory mismatch"
            + (f"; unproven={missing}" if missing else "")
            + (f"; unexpected={extra}" if extra else ""),
        )

    if inspection.prejournal_temps and not candidates:
        _raise(
            error_cls,
            "stage-cleanup pre-journal residue has no stale stage candidate",
        )

    for prep in inspection.prejournal_temps:
        if prep.is_symlink() or not prep.is_file():
            _raise(
                error_cls,
                f"ambiguous stage-cleanup pre-journal artifact: {prep}",
            )
        prep.unlink()

    journaled_originals: set[Path] = set()
    for record in inspection.records:
        _finish_record(
            record,
            proven_stages=proven,
            expected_state=record.state,
            error_cls=error_cls,
        )
        if record.state == "original":
            journaled_originals.add(record.original_stage)

    for stage in sorted(
        proven - journaled_originals,
        key=lambda item: item.name.casefold(),
    ):
        if stage.is_symlink() or not stage.is_dir():
            _raise(error_cls, f"ambiguous proven stage before cleanup: {stage}")
        record = _new_record(stage, inspection)
        _finish_record(
            record,
            proven_stages=proven,
            expected_state="original",
            error_cls=error_cls,
        )
