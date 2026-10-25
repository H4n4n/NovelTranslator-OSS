"""Crash-safe periodic-audit recovery for the full-pipeline Luna experiment.

This module is experiment-scoped.  It does not change ordinary production model
selection or the generic glossary-audit implementation.

It closes two restart windows around the real periodic glossary audit:

1. Audit provider requests are routed through the existing durable paid-request
   journal.  The journal slot is deterministically bound to the audit attempt's
   operation id.  A durable ``response_recorded`` envelope can therefore be
   copied back into an audit chunk checkpoint after a crash, while
   ``started``/``uncertain`` continues to fail closed.
2. Before ``run_periodic_audit`` starts, the exact post-chapter/pre-audit state
   is snapshotted.  If audit publication crashes before ``progress.json``
   advances ``last_audited_chapter``, the snapshot is restored byte-for-byte
   and the durable audit checkpoint is replayed locally. Legacy prose snapshots
   retain rollback recovery; owned-span transactions instead verify and preserve
   their BEFORE/AFTER chapter states and immutable evidence for local reconciliation.

The real production audit function remains the sole implementation of audit
semantics, state mutation, retroactive replacement, accounting, and final
publication.  This adapter only provides recovery/journaling around it.

Importing this module performs no filesystem writes, credential loading, or
provider calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any, Mapping

from production_run_lock import held_production_run_lock, ProductionRunLockedError

from production_paid_request_journal import (
    PaidRequestJournalError,
    load_matching_journal,
    paid_request_journal_path,
)


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_REVISION = "full-pipeline-periodic-audit-recovery-v1"
RECOVERY_DIRNAME = "full_pipeline_audit_recovery"
RECOVERY_MANIFEST = "recovery_manifest.json"
BASELINE_DIRNAME = "baseline"
_STATE_ATTR = "_full_pipeline_periodic_audit_recovery_state"
_AUDIT_KINDS = frozenset({"audit", "json_repair", "semantic_correction"})
_KIND_PREFIX = {
    "audit": "periodic_audit",
    "json_repair": "periodic_audit_json_repair",
    "semantic_correction": "periodic_audit_semantic_correction",
}


class FullPipelinePeriodicAuditRecoveryError(RuntimeError):
    """Periodic-audit experiment recovery evidence is missing or inconsistent."""


def _require_novel_lock(project_root):
    """Verify the caller's real lease; never acquire a nested rendezvous lock."""
    try:
        held_production_run_lock(project_root)
    except RuntimeError as exc:
        raise ProductionRunLockedError("Actual caller-held novel production lock required") from exc


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit recovery identity is not JSON-serializable"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _record_hash(value: Mapping[str, Any]) -> str:
    payload = {
        key: copy.deepcopy(item)
        for key, item in dict(value).items()
        if key != "record_sha256"
    }
    return _sha256_text(_canonical_json(payload))


def _with_record_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["record_sha256"] = _record_hash(result)
    return result


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"{label} must contain an object: {path}"
        )
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_bytes(payload)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    root = Path(root)
    if not root.exists():
        return {"file_count": 0, "byte_count": 0, "sha256": _sha256_bytes(b"")}
    if not root.is_dir():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"recovery tree root is not a directory: {root}"
        )
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        file_count += 1
        byte_count += len(payload)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return {
        "file_count": file_count,
        "byte_count": byte_count,
        "sha256": digest.hexdigest(),
    }


def _slot_name(start_chapter: int, end_chapter: int) -> str:
    return f"audit_{int(start_chapter):04d}_{int(end_chapter):04d}"


def _recovery_root(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / "data" / RECOVERY_DIRNAME


def _slot_path(project_root: Path | str, start_chapter: int, end_chapter: int) -> Path:
    return _recovery_root(project_root) / _slot_name(start_chapter, end_chapter)


def _manifest_path(slot: Path) -> Path:
    return Path(slot) / RECOVERY_MANIFEST


def _baseline_path(slot: Path) -> Path:
    return Path(slot) / BASELINE_DIRNAME


def _data_names() -> tuple[str, ...]:
    return (
        "glossary.json",
        "progress.json",
        "continuity.json",
        "issues.json",
        "audit_history.json",
    )


def _audit_record_path(project_root: Path, start_chapter: int, end_chapter: int) -> Path:
    return (
        project_root
        / "output"
        / "audits"
        / f"audit_{start_chapter:04d}_{end_chapter:04d}.json"
    )


def _audit_checkpoint_path(project_root: Path, start_chapter: int, end_chapter: int) -> Path:
    return (
        project_root
        / "output"
        / "audits"
        / f"audit_{start_chapter:04d}_{end_chapter:04d}.checkpoint.json"
    )


def _backup_checkpoint_path(project_root: Path, end_chapter: int) -> Path:
    return project_root / "backup" / f"checkpoint_{end_chapter:04d}"


def _copy_tree_exact(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    if source.exists():
        if not source.is_dir():
            raise FullPipelinePeriodicAuditRecoveryError(
                f"expected directory while copying recovery tree: {source}"
            )
        shutil.copytree(source, destination)
    else:
        destination.mkdir(parents=True, exist_ok=True)


def _snapshot_optional_file(source: Path, destination: Path) -> dict[str, Any]:
    if source.is_file():
        payload = source.read_bytes()
        _atomic_write_bytes(destination, payload)
        return {
            "exists": True,
            "sha256": _sha256_bytes(payload),
            "bytes": len(payload),
        }
    if source.exists():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"expected recovery snapshot file path: {source}"
        )
    return {"exists": False}


def _validate_window(start_chapter: int, end_chapter: int) -> None:
    if (
        not isinstance(start_chapter, int)
        or isinstance(start_chapter, bool)
        or not isinstance(end_chapter, int)
        or isinstance(end_chapter, bool)
        or start_chapter < 1
        or end_chapter < start_chapter
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"invalid periodic-audit recovery window {start_chapter!r}-{end_chapter!r}"
        )


def _load_manifest(slot: Path) -> dict[str, Any]:
    value = _read_object(_manifest_path(slot), "periodic-audit recovery manifest")
    if value.get("schema_version") != RECOVERY_SCHEMA_VERSION:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"unsupported periodic-audit recovery schema at {slot}"
        )
    if value.get("recovery_revision") != RECOVERY_REVISION:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"unsupported periodic-audit recovery revision at {slot}"
        )
    expected = value.get("record_sha256")
    if not isinstance(expected, str) or expected != _record_hash(value):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit recovery manifest hash mismatch at {slot}"
        )
    window = value.get("audit_window")
    if not isinstance(window, dict):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit recovery manifest lacks audit window at {slot}"
        )
    _validate_window(window.get("start_chapter"), window.get("end_chapter"))
    if value.get("status") not in {"active", "committed"}:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"invalid periodic-audit recovery status at {slot}"
        )
    return value


def _snapshot_baseline(
    project_root: Path,
    start_chapter: int,
    end_chapter: int,
) -> tuple[dict[str, Any], bool]:
    """Create one immutable pre-audit baseline, or load the existing one."""
    _validate_window(start_chapter, end_chapter)
    _require_novel_lock(project_root)
    project_root = Path(project_root).resolve()
    slot = _slot_path(project_root, start_chapter, end_chapter)
    if slot.exists():
        manifest = _load_manifest(slot)
        expected_window = {
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
        }
        if manifest["audit_window"] != expected_window:
            raise FullPipelinePeriodicAuditRecoveryError(
                f"recovery slot window mismatch at {slot}"
            )
        return manifest, False

    progress = _read_object(project_root / "data" / "progress.json", "progress")
    last_completed = progress.get("last_completed")
    last_audited = progress.get("last_audited_chapter")
    if not isinstance(last_completed, int) or last_completed < end_chapter:
        raise FullPipelinePeriodicAuditRecoveryError(
            "cannot snapshot periodic audit before its ending chapter is committed"
        )
    if not isinstance(last_audited, int) or last_audited >= end_chapter:
        raise FullPipelinePeriodicAuditRecoveryError(
            "cannot snapshot an already committed periodic audit"
        )
    if last_audited + 1 != start_chapter:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit recovery window is not the next scheduled audit"
        )

    # An acquisition backup is not edit authority. Prove independent inventory
    # and prior ownership before creating even this pre-audit baseline.
    from historical_reconciliation import writer_module, writer_inventory, writer_authorization
    from retroactive_transactions import build_plan, ownership_policy
    from production_failure_classification import ProductionIntegrityError
    try:
        novel = _read_object(project_root / "novel.json", "novel config")["id"]
        context = writer_module(project_root / "output/chapters", novel)
        writer_inventory(context, progress.get("completed"), end_chapter)
        request = dict(novel_id=novel, audit_label=f"{start_chapter:04d}_{end_chapter:04d}",
            through_chapter=end_chapter, rules=[], ownership_policy=ownership_policy(context.glossary),
            safe_categories=sorted(context.ACTIVE_PROFILE.retroactive_safe_categories),
            committed_chapters=list(range(1, end_chapter + 1)))
        baseline_authorization = writer_authorization(context, build_plan(request, {n: (context.OUTPUT_DIR / f"{n:04d}.md").read_bytes()
            for n in request["committed_chapters"]}, context.chapter_map))
    except (ProductionIntegrityError, OSError, ValueError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError("local retroactive baseline blocked: " + str(exc)) from exc
    root = _recovery_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{slot.name}.creating-{uuid.uuid4().hex}"
    baseline = _baseline_path(staging)
    try:
        baseline.mkdir(parents=True)
        data_records = {}
        for name in _data_names():
            source = project_root / "data" / name
            if not source.is_file():
                raise FullPipelinePeriodicAuditRecoveryError(
                    f"periodic-audit baseline is missing {source}"
                )
            payload = source.read_bytes()
            _atomic_write_bytes(baseline / "data" / name, payload)
            data_records[name] = {
                "sha256": _sha256_bytes(payload),
                "bytes": len(payload),
            }

        _copy_tree_exact(
            project_root / "output" / "chapters",
            baseline / "chapters",
        )
        _copy_tree_exact(
            project_root / "output" / "retroactive_fixes",
            baseline / "retroactive_fixes",
        )
        audit_record = _snapshot_optional_file(
            _audit_record_path(project_root, start_chapter, end_chapter),
            baseline / "audit_record.json",
        )
        backup_path = _backup_checkpoint_path(project_root, end_chapter)
        backup_existed = backup_path.exists()
        if backup_existed:
            if not backup_path.is_dir():
                raise FullPipelinePeriodicAuditRecoveryError(
                    f"audit backup checkpoint is not a directory: {backup_path}"
                )
            _copy_tree_exact(backup_path, baseline / "backup_checkpoint")
        else:
            (baseline / "backup_checkpoint").mkdir(parents=True)

        manifest = _with_record_hash({
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "recovery_revision": RECOVERY_REVISION,
            "status": "active",
            "project_id": str(
                _read_object(project_root / "novel.json", "novel config").get("id", "")
            ),
            "audit_window": {
                "start_chapter": start_chapter,
                "end_chapter": end_chapter,
            },
            "baseline_progress": {
                "last_completed": last_completed,
                "next_chapter": progress.get("next_chapter"),
                "last_audited_chapter": last_audited,
            },
            "baseline_data": data_records,
            "writer_baseline_authorization": baseline_authorization,
            "chapters_tree": _tree_fingerprint(baseline / "chapters"),
            "retroactive_fixes_tree": _tree_fingerprint(
                baseline / "retroactive_fixes"
            ),
            "audit_record": audit_record,
            "backup_checkpoint_existed": bool(backup_existed),
            "backup_checkpoint_tree": _tree_fingerprint(
                baseline / "backup_checkpoint"
            ),
        })
        _atomic_write_json(_manifest_path(staging), manifest)
        if slot.exists():
            raise FullPipelinePeriodicAuditRecoveryError(
                f"periodic-audit recovery slot appeared during creation: {slot}"
            )
        os.rename(staging, slot)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return _load_manifest(slot), True


def _validate_baseline(slot: Path, manifest: Mapping[str, Any]) -> None:
    baseline = _baseline_path(slot)
    for name, record in manifest["baseline_data"].items():
        path = baseline / "data" / name
        if not path.is_file():
            raise FullPipelinePeriodicAuditRecoveryError(
                f"periodic-audit baseline file disappeared: {path}"
            )
        payload = path.read_bytes()
        if (
            record.get("sha256") != _sha256_bytes(payload)
            or record.get("bytes") != len(payload)
        ):
            raise FullPipelinePeriodicAuditRecoveryError(
                f"periodic-audit baseline file hash mismatch: {path}"
            )
    if _tree_fingerprint(baseline / "chapters") != manifest["chapters_tree"]:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit baseline chapter tree mismatch at {slot}"
        )
    if (
        _tree_fingerprint(baseline / "retroactive_fixes")
        != manifest["retroactive_fixes_tree"]
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit baseline retroactive tree mismatch at {slot}"
        )
    if (
        _tree_fingerprint(baseline / "backup_checkpoint")
        != manifest["backup_checkpoint_tree"]
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit baseline backup tree mismatch at {slot}"
        )
    audit_record = manifest["audit_record"]
    baseline_audit = baseline / "audit_record.json"
    if audit_record.get("exists"):
        if not baseline_audit.is_file():
            raise FullPipelinePeriodicAuditRecoveryError(
                f"periodic-audit baseline record disappeared at {slot}"
            )
        payload = baseline_audit.read_bytes()
        if (
            audit_record.get("sha256") != _sha256_bytes(payload)
            or audit_record.get("bytes") != len(payload)
        ):
            raise FullPipelinePeriodicAuditRecoveryError(
                f"periodic-audit baseline record hash mismatch at {slot}"
            )
    elif baseline_audit.exists():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"unexpected periodic-audit baseline record at {slot}"
        )


def _restore_baseline(project_root: Path, manifest: Mapping[str, Any]) -> None:
    """Restore pre-audit state while preserving provider/checkpoint evidence."""
    _require_novel_lock(project_root)
    project_root = Path(project_root).resolve()
    window = manifest["audit_window"]
    start_chapter = int(window["start_chapter"])
    end_chapter = int(window["end_chapter"])
    slot = _slot_path(project_root, start_chapter, end_chapter)
    _validate_baseline(slot, manifest)

    current_progress = _read_object(project_root / "data" / "progress.json", "progress")
    baseline_progress = manifest["baseline_progress"]
    if (
        isinstance(current_progress.get("last_completed"), int)
        and current_progress["last_completed"] > baseline_progress["last_completed"]
        and current_progress.get("last_audited_chapter", 0) < end_chapter
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            "refusing to restore an audit baseline across later committed chapters"
        )

    baseline = _baseline_path(slot)
    # The local span engine owns restart reconciliation. Never erase its durable
    # intent or restore its AFTER prose to BEFORE through a whole-tree rollback.
    from retroactive_transactions import RetroactiveBlocked, verify_audit_recovery_baseline
    from production_failure_classification import ProductionIntegrityError
    try:
        preserve_span_transaction = verify_audit_recovery_baseline(
            project_root, baseline, manifest["project_id"],
            f"{start_chapter:04d}_{end_chapter:04d}", end_chapter,
            baseline_authorization=manifest.get("writer_baseline_authorization"),
        )
    except (RetroactiveBlocked, ProductionIntegrityError, OSError, ValueError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"local retroactive transaction recovery blocked: {exc}"
        ) from exc
    for name in _data_names():
        _atomic_write_bytes(
            project_root / "data" / name,
            (baseline / "data" / name).read_bytes(),
        )
    if not preserve_span_transaction:
        _copy_tree_exact(baseline / "chapters", project_root / "output" / "chapters")
        _copy_tree_exact(
            baseline / "retroactive_fixes",
            project_root / "output" / "retroactive_fixes",
        )

    audit_path = _audit_record_path(project_root, start_chapter, end_chapter)
    if manifest["audit_record"].get("exists"):
        _atomic_write_bytes(audit_path, (baseline / "audit_record.json").read_bytes())
    elif audit_path.exists():
        audit_path.unlink()

    backup_path = _backup_checkpoint_path(project_root, end_chapter)
    if manifest["backup_checkpoint_existed"]:
        _copy_tree_exact(baseline / "backup_checkpoint", backup_path)
    elif backup_path.exists():
        shutil.rmtree(backup_path)

    for name, record in manifest["baseline_data"].items():
        payload = (project_root / "data" / name).read_bytes()
        if _sha256_bytes(payload) != record["sha256"]:
            raise FullPipelinePeriodicAuditRecoveryError(
                f"restored periodic-audit baseline differs for {name}"
            )
    if not preserve_span_transaction and _tree_fingerprint(project_root / "output" / "chapters") != manifest[
        "chapters_tree"
    ]:
        raise FullPipelinePeriodicAuditRecoveryError(
            "restored periodic-audit chapter tree differs from baseline"
        )
    if (
        not preserve_span_transaction and _tree_fingerprint(project_root / "output" / "retroactive_fixes")
        != manifest["retroactive_fixes_tree"]
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            "restored periodic-audit retroactive tree differs from baseline"
        )


def _sync_module_state_from_disk(production_module, project_root: Path) -> None:
    mapping = {
        "glossary": "glossary.json",
        "progress": "progress.json",
        "continuity": "continuity.json",
        "issues_data": "issues.json",
        "audit_history": "audit_history.json",
    }
    for attr, name in mapping.items():
        disk = _read_object(project_root / "data" / name, name)
        current = getattr(production_module, attr, None)
        if not isinstance(current, dict):
            setattr(production_module, attr, disk)
        else:
            current.clear()
            current.update(copy.deepcopy(disk))


def _validate_committed_audit(project_root: Path, manifest: Mapping[str, Any]) -> None:
    window = manifest["audit_window"]
    start_chapter = int(window["start_chapter"])
    end_chapter = int(window["end_chapter"])
    progress = _read_object(project_root / "data" / "progress.json", "progress")
    if progress.get("last_audited_chapter", 0) < end_chapter:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic audit progress marker has not committed"
        )
    audit_path = _audit_record_path(project_root, start_chapter, end_chapter)
    if not audit_path.is_file():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"committed periodic audit lacks record {audit_path}"
        )
    audit_record = _read_object(audit_path, "periodic audit record")
    if (
        audit_record.get("start_chapter") != start_chapter
        or audit_record.get("end_chapter") != end_chapter
    ):
        raise FullPipelinePeriodicAuditRecoveryError(
            "committed periodic audit record has wrong window"
        )
    history = _read_object(project_root / "data" / "audit_history.json", "audit history")
    audits = history.get("audits")
    if not isinstance(audits, list) or audit_record not in audits:
        raise FullPipelinePeriodicAuditRecoveryError(
            "committed periodic audit record is absent from audit history"
        )


def _ensure_backup_checkpoint(project_root: Path, manifest: Mapping[str, Any]) -> None:
    end_chapter = int(manifest["audit_window"]["end_chapter"])
    target = _backup_checkpoint_path(project_root, end_chapter)
    expected_names = _data_names()
    if target.exists() and not target.is_dir():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit post-marker backup is not a directory: {target}"
        )
    if not target.exists():
        target.mkdir(parents=True)
    for name in expected_names:
        source = project_root / "data" / name
        destination = target / name
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise FullPipelinePeriodicAuditRecoveryError(
                    f"periodic-audit backup differs from committed {name}"
                )
        else:
            _atomic_write_bytes(destination, source.read_bytes())


def _recovery_inventory(project_root, *, through=None, novel_id=None):
    """Pure inventory precheck before any reconciliation mutation or cleanup."""
    from historical_reconciliation import writer_module, writer_inventory, read_object
    from production_failure_classification import ProductionIntegrityError
    try:
        novel_id = novel_id or read_object(project_root / "novel.json")["id"]
        context = writer_module(project_root / "output/chapters", novel_id)
        progress = read_object(context.PROGRESS_FILE)
        return writer_inventory(context, progress.get("completed"),
            progress.get("last_completed") if through is None else through)
    except (ProductionIntegrityError, OSError, ValueError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError("local recovery inventory blocked: " + str(exc)) from exc


def _verify_snapshot_inventory(project_root, manifest):
    """Bind post-marker backup repair to the original complete source inventory."""
    from historical_reconciliation import plain
    from retroactive_span_planning import canonical_hash
    from production_failure_classification import ProductionIntegrityError
    try:
        inventory = _recovery_inventory(project_root, novel_id=manifest["project_id"],
                                        through=manifest["audit_window"]["end_chapter"])
        slot = _slot_path(project_root, *(
            manifest["audit_window"][key] for key in ("start_chapter", "end_chapter")))
        before = {f"{n:04d}.md": _sha256_bytes(plain(_baseline_path(slot) / "chapters" /
                   f"{n:04d}.md").read_bytes()) for n in inventory["committed"]}
        proof = manifest.get("writer_baseline_authorization")
        if (not isinstance(proof, dict) or proof.get("inventory_sha256") !=
                canonical_hash({**inventory, "before_chapters": before})):
            raise ProductionIntegrityError("AUT2: SOURCE_INVENTORY_DRIFT: recovery baseline")
    except (ProductionIntegrityError, OSError, ValueError) as exc:
        raise FullPipelinePeriodicAuditRecoveryError("local recovery inventory blocked: " + str(exc)) from exc


def _mark_manifest_committed(project_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    _require_novel_lock(project_root)
    _verify_snapshot_inventory(project_root, manifest)
    value = copy.deepcopy(dict(manifest))
    _validate_committed_audit(project_root, value)
    _ensure_backup_checkpoint(project_root, value)
    value["status"] = "committed"
    value["committed_progress_sha256"] = _sha256_bytes(
        (project_root / "data" / "progress.json").read_bytes()
    )
    value["committed_audit_sha256"] = _sha256_bytes(
        _audit_record_path(
            project_root,
            int(value["audit_window"]["start_chapter"]),
            int(value["audit_window"]["end_chapter"]),
        ).read_bytes()
    )
    value = _with_record_hash(value)
    slot = _slot_path(
        project_root,
        int(value["audit_window"]["start_chapter"]),
        int(value["audit_window"]["end_chapter"]),
    )
    _atomic_write_json(_manifest_path(slot), value)
    return value


def reconcile_project_periodic_audit_recovery(
    project_root: Path | str,
) -> list[dict[str, Any]]:
    """Reconcile durable audit-publication recovery state without provider access."""
    _require_novel_lock(project_root)
    project_root = Path(project_root).resolve()
    root = _recovery_root(project_root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise FullPipelinePeriodicAuditRecoveryError(
            f"periodic-audit recovery root is not a directory: {root}"
        )
    _recovery_inventory(project_root)
    actions = []
    for slot in sorted(item for item in root.iterdir() if item.is_dir()):
        if slot.name.startswith("."):
            # Snapshot staging is created before the wrapped production audit is
            # entered, so an abandoned staging directory proves no provider call
            # was started by this recovery slot.  It may be discarded locally.
            shutil.rmtree(slot)
            actions.append({"slot": slot.name, "action": "discarded_staging"})
            continue
        manifest = _load_manifest(slot)
        _validate_baseline(slot, manifest)
        end_chapter = int(manifest["audit_window"]["end_chapter"])
        progress = _read_object(project_root / "data" / "progress.json", "progress")
        if progress.get("last_audited_chapter", 0) >= end_chapter:
            committed = _mark_manifest_committed(project_root, manifest)
            actions.append({
                "slot": slot.name,
                "action": "committed",
                "record_sha256": committed["record_sha256"],
            })
            continue
        if manifest.get("status") == "committed":
            raise FullPipelinePeriodicAuditRecoveryError(
                f"committed recovery manifest is ahead of progress at {slot}"
            )
        _restore_baseline(project_root, manifest)
        actions.append({"slot": slot.name, "action": "restored_baseline"})
    return actions


def _journal_operation_kind(kind: str, operation_id: str) -> str:
    if kind not in _AUDIT_KINDS:
        raise FullPipelinePeriodicAuditRecoveryError(
            f"unsupported periodic-audit paid operation kind {kind!r}"
        )
    if not isinstance(operation_id, str) or not operation_id:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit operation id must be non-empty"
        )
    suffix = _sha256_text(operation_id)[:16]
    return f"{_KIND_PREFIX[kind]}_{suffix}"


def _journal_expected_subset(
    production_module,
    kind: str,
    operation_id: str,
    end_chapter: int,
) -> dict[str, Any]:
    config = getattr(production_module, "ACTIVE_NOVEL_CONFIG", None)
    profile = getattr(production_module, "ACTIVE_PROFILE", None)
    novel_id = getattr(config, "id", "") or "legacy_single_novel"
    profile_id = getattr(profile, "id", "") or ""
    model = getattr(production_module, "MODEL", None)
    if not profile_id or not isinstance(model, str) or not model:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit paid journal lacks model/profile runtime identity"
        )
    return {
        "operation_kind": _journal_operation_kind(kind, operation_id),
        "novel_id": novel_id,
        "chapter": end_chapter,
        "provider": "openai",
        "model": model,
        "profile_id": profile_id,
        "source_sha256": _sha256_text(operation_id),
        "translation_sha256": "",
    }


def _journal_path_for_attempt(
    production_module,
    kind: str,
    operation_id: str,
    end_chapter: int,
) -> Path:
    operation_kind = _journal_operation_kind(kind, operation_id)
    return paid_request_journal_path(
        production_module.PAID_REQUEST_JOURNAL_DIR,
        operation_kind,
        end_chapter,
    )


def _validate_envelope(production_module, envelope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit paid journal response must be an object"
        )
    value = copy.deepcopy(dict(envelope))
    content = value.get("content")
    finish_reason = value.get("finish_reason")
    if not isinstance(content, str):
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit paid journal response lacks content"
        )
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit paid journal finish_reason is malformed"
        )
    observed_model = value.get("provider_response_model")
    if observed_model != production_module.MODEL:
        raise FullPipelinePeriodicAuditRecoveryError(
            "periodic-audit paid journal provider model identity mismatch"
        )
    stats = production_module.reprice_saved_stats_if_needed(value.get("stats") or {})
    value["stats"] = stats
    return value


def install_periodic_audit_recovery(
    production_module,
    project_root: Path | str,
    *,
    checkpoint_validator=None,
):
    """Install experiment-only audit journaling and publication recovery."""
    project_root = Path(project_root).resolve()
    existing = getattr(production_module, _STATE_ATTR, None)
    if isinstance(existing, dict):
        if Path(existing.get("project_root", "")).resolve() != project_root:
            raise FullPipelinePeriodicAuditRecoveryError(
                "periodic-audit recovery already installed for another project"
            )
        return existing

    required = (
        "run_periodic_audit",
        "run_chunked_audit",
        "call_api_with_retries",
        "journaled_chat_request",
        "reprice_saved_stats_if_needed",
        "atomic_write_json",
        "paid_request_journal_path",
    )
    missing = [name for name in required if not callable(getattr(production_module, name, None))]
    if missing:
        raise FullPipelinePeriodicAuditRecoveryError(
            "production module lacks periodic-audit recovery callbacks: "
            + ", ".join(missing)
        )

    reconcile_project_periodic_audit_recovery(project_root)
    _sync_module_state_from_disk(production_module, project_root)

    original_run_periodic_audit = production_module.run_periodic_audit
    original_run_chunked_audit = production_module.run_chunked_audit
    original_call_api = production_module.call_api_with_retries
    active_context: list[dict[str, Any]] = []

    def routed_call_api(**kwargs):
        if not active_context:
            return original_call_api(**kwargs)
        context = active_context[-1]
        if context.get("capture"):
            raise CapturedRequest(copy.deepcopy(kwargs))
        raise FullPipelinePeriodicAuditRecoveryError("unsupported periodic-audit request context")

    from auxiliary_response_reconciliation import JournalOwnedAuditCallback
    from full_pipeline_luna_runtime import normalize_luna_request

    class CapturedRequest(BaseException):
        def __init__(self, request):
            self.request = request

    # Capture only request construction. The router raises before journal entry,
    # scheduling, client access or send. This also supports historical callbacks
    # whose request builders were not separately exposed.
    def wrap_callback(callback, kind, end_chapter):
        if callback is None:
            return None

        def build_request(*args):
            context = {"capture": True}
            active_context.append(context)
            try:
                callback(*args)
            except CapturedRequest as captured:
                return captured.request
            finally:
                active_context.pop()
            raise FullPipelinePeriodicAuditRecoveryError("audit callback constructed no request")

        def bind_request(kwargs, record, metadata):
            operation_id = record["operation_id"]
            expected = _journal_expected_subset(production_module, kind, operation_id, end_chapter)
            identity = production_module.request_identity(
                **{key: value for key, value in expected.items() if key != "request_sha256"},
                request_payload=normalize_luna_request(kwargs))
            return {"journal_path": str(_journal_path_for_attempt(production_module, kind, operation_id, end_chapter)),
                    "identity": identity}

        def send_request(kwargs, binding, *, replay_only):
            expected = binding["identity"]
            record = load_matching_journal(binding["journal_path"], expected)
            if replay_only:
                if record is None or record["status"] != "response_recorded":
                    raise FullPipelinePeriodicAuditRecoveryError("local audit recovery requires recorded response")
                envelope = _validate_envelope(production_module, record["response"])
            else:
                result = production_module.journaled_chat_request(expected["operation_kind"], end_chapter,
                    args_operation_id(binding), kwargs)
                envelope = _validate_envelope(production_module, result["response"])
            return {"content": envelope["content"], "finish_reason": envelope["finish_reason"],
                    "stats": envelope["stats"]}

        # Luna's historical source binding is the exact outer operation ID.
        operation_ids = {}
        original_bind = bind_request
        def bind_request(kwargs, record, metadata):
            binding = original_bind(kwargs, record, metadata)
            operation_ids[binding["identity"]["operation_id"]] = record["operation_id"]
            return binding
        def args_operation_id(binding):
            return operation_ids[binding["identity"]["operation_id"]]

        class HistoricalAuditCallback(JournalOwnedAuditCallback):
            def verify_recorded(self, record, args):
                if checkpoint_validator is None:
                    return
                # V3's extra provenance requirement is diagnostic only. Preserve
                # durable outer bytes and prove the complete current request SHA.
                binding = self.prepare(record, args)
                if "transport_binding" in record and record["transport_binding"] != binding:
                    raise FullPipelinePeriodicAuditRecoveryError("carried audit response request binding mismatch")
                paid = load_matching_journal(binding["journal_path"], binding["identity"])
                if paid is None or paid["status"] != "response_recorded":
                    raise FullPipelinePeriodicAuditRecoveryError("carried audit response lacks exact recorded paid journal")
                envelope = _validate_envelope(production_module, paid["response"])
                for field in ("raw_content", "finish_reason", "stats"):
                    expected = envelope["content"] if field == "raw_content" else envelope[field]
                    if record.get(field) != expected:
                        raise FullPipelinePeriodicAuditRecoveryError("carried audit response differs from exact paid journal")

            def recover(self, record, args):
                if "transport_binding" not in record:
                    # Read compatibility for pre-seam experiment checkpoints:
                    # manifest-before-hook ownership plus exact reconstructed
                    # request/operation identity, never a subset-only hydration.
                    manifest = _load_manifest(_slot_path(project_root, *(
                        args[-1]["audit_window"][key] for key in ("start_chapter", "end_chapter"))))
                    if manifest.get("status") != "active":
                        raise FullPipelinePeriodicAuditRecoveryError("historical audit lacks active ownership manifest")
                    binding = self.prepare(record, args)
                    load_matching_journal(binding["journal_path"], binding["identity"])
                    record["transport_binding"] = binding
                try:
                    return super().recover(record, args)
                except PaidRequestJournalError as exc:
                    raise FullPipelinePeriodicAuditRecoveryError(
                        "periodic-audit paid request has an uncertain remote outcome or invalid evidence; "
                        "automatic reissue is blocked") from exc

        return HistoricalAuditCallback(build_request=build_request, bind_request=bind_request,
            send_request=send_request)

    def wrapped_run_chunked_audit(*args, **kwargs):
        if kwargs.get("end_chapter") is None:
            return original_run_chunked_audit(*args, **kwargs)
        if checkpoint_validator is not None:
            checkpoint_validator(production_module, project_root,
                Path(kwargs["checkpoint_path"]), int(kwargs["start_chapter"]),
                int(kwargs["end_chapter"]))
        forwarded = dict(kwargs)
        for name, kind in (("request_chunk", "audit"), ("repair_chunk", "json_repair"),
                           ("correct_semantic_chunk", "semantic_correction")):
            forwarded[name] = wrap_callback(kwargs.get(name), kind, int(kwargs["end_chapter"]))
        return original_run_chunked_audit(*args, **forwarded)

    def wrapped_run_periodic_audit(start_chapter, end_chapter):
        manifest, created = _snapshot_baseline(
            project_root,
            int(start_chapter),
            int(end_chapter),
        )
        progress_disk = _read_object(
            project_root / "data" / "progress.json",
            "progress",
        )
        if progress_disk.get("last_audited_chapter", 0) >= int(end_chapter):
            committed = _mark_manifest_committed(project_root, manifest)
            _sync_module_state_from_disk(production_module, project_root)
            return {
                "recovery": "already_committed",
                "record_sha256": committed["record_sha256"],
            }
        if not created:
            _restore_baseline(project_root, manifest)
            _sync_module_state_from_disk(production_module, project_root)
        result = original_run_periodic_audit(start_chapter, end_chapter)
        committed = _mark_manifest_committed(project_root, manifest)
        _sync_module_state_from_disk(production_module, project_root)
        return result if result is not None else {
            "recovery": "committed",
            "record_sha256": committed["record_sha256"],
        }

    production_module.call_api_with_retries = routed_call_api
    production_module.run_chunked_audit = wrapped_run_chunked_audit
    production_module.run_periodic_audit = wrapped_run_periodic_audit
    state = {
        "mode": RECOVERY_REVISION,
        "project_root": str(project_root),
        "original_run_periodic_audit": original_run_periodic_audit,
        "original_run_chunked_audit": original_run_chunked_audit,
        "original_call_api_with_retries": original_call_api,
        "wrapped_run_periodic_audit": wrapped_run_periodic_audit,
        "wrapped_run_chunked_audit": wrapped_run_chunked_audit,
        "routed_call_api_with_retries": routed_call_api,
    }
    setattr(production_module, _STATE_ATTR, state)
    return state
