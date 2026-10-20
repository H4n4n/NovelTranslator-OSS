"""G11 zero-provider blind bundle freeze.

This is the zero-provider phase between the immutable G11 final freeze and the
later anonymous literary/longitudinal scoring, score freeze, and identity
reveal.  It:

* re-verifies the immutable G11 final freeze (read-only): its freeze id, the
  exact SHA-256 of ``g11_final_freeze.json``, ``execution_tree_manifest.json``,
  and ``G11_FINAL_FREEZE_SUMMARY.txt``, the physical execution-snapshot tree
  identity, and the frozen track/model/completion evidence through
  :func:`benchmark_longitudinal_g11_final_freeze.build_execution_evidence`;
* builds 28 literary cases from the **pre-extracted checkpoint evidence**
  (``execution_snapshot/g11_evidence/<run_id>/<chapter>/chapter.md``), never from
  the mutable-by-audit ``runs/<run_id>/output/chapters/<chapter>.md``;
* builds 8 longitudinal cases from the frozen Ch25/50/75/100 checkpoint evidence
  with a blind-safe, sanitized semantic state projection and the exact 25-chapter
  reference source window;
* derives one private A/B identity key from a fresh in-memory 256-bit secret
  (the raw secret is never persisted), persists it only inside ``private/``, and
  records only its cryptographic commitment publicly;
* writes a create-once, externally isolated public/private package using a
  sibling staging directory and one atomic rename;
* makes **zero** Provider calls, reads **zero** credentials, scores nothing,
  reveals nothing, and never consults a live G11 paid execution root.

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
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import benchmark_longitudinal_g11_blind as blind
import benchmark_longitudinal_g11_final_freeze as final_freeze
import benchmark_longitudinal_g11_runner as g11_runner
from benchmark_longitudinal_g11_preregistration import (
    G11_BENCHMARK_ID,
    MODEL_ORDER,
    TRACKS,
)
from chapter_library import chapter_production_index
from noveltranslator_core import atomic_write_json


G11BB_SCHEMA_VERSION = 1
G11BB_REVISION = "g11-blind-bundle-freeze-v1"
PRIVATE_INPUT_REVISION = "g11-private-input-manifest-v1"

FREEZE_FILENAME = "g11_blind_bundle_freeze.json"
SUMMARY_FILENAME = "G11_BLIND_BUNDLE_SUMMARY.txt"
PUBLIC_TREE_MANIFEST_FILENAME = "public_tree_manifest.json"

PRIVATE_DIRECTORY = "private"
PUBLIC_DIRECTORY = "public"
PRIVATE_KEY_FILENAME = "private_identity_key.json"
PRIVATE_INPUT_FILENAME = "private_input_manifest.json"
PUBLIC_MANIFEST_FILENAME = "public_blind_manifest.json"
INSTRUCTIONS_FILENAME = "G11_BLIND_EVALUATION_INSTRUCTIONS.md"
LITERARY_DIRECTORY = "literary"
LONGITUDINAL_DIRECTORY = "longitudinal"

FINAL_FREEZE_DOCUMENT_FILENAME = final_freeze.FREEZE_FILENAME
FINAL_FREEZE_MANIFEST_FILENAME = final_freeze.EXECUTION_MANIFEST_FILENAME
FINAL_FREEZE_SUMMARY_FILENAME = final_freeze.SUMMARY_FILENAME
FINAL_FREEZE_SNAPSHOT_DIRECTORY = final_freeze.EXECUTION_SNAPSHOT_DIRECTORY

EXPECTED_LITERARY_COUNT = blind.LITERARY_PAIR_COUNT
EXPECTED_LONGITUDINAL_COUNT = blind.LONGITUDINAL_PAIR_COUNT
EXPECTED_CANONICAL_CHAPTERS = 400
EXPECTED_SCHEDULED_AUDITS = 16
EXPECTED_LITERARY_MATCHED = 28
EXPECTED_STATE_MATCHED = 8

# The exact immutable G11 final-freeze identities this phase binds to.  These are
# module globals so a review harness may pin synthetic values when exercising the
# protocol with temporary data; production refuses anything else.
EXPECTED_FINAL_FREEZE_ID = (
    "6176a7d76111f8eccc0a897a5ee3cfee7224a6455a49e778a0702ebd6ef4931b"
)
EXPECTED_FINAL_FREEZE_FILE_SHA256 = (
    "0667c518e078eaecbf291099f767280b19624d7f9478d793c77df1785ec39d3a"
)
EXPECTED_EXECUTION_MANIFEST_FILE_SHA256 = (
    "f739fec39c718ddd3c035a4f764808fff32db7e20b09a1e9686a8d722df301f7"
)
EXPECTED_FINAL_FREEZE_SUMMARY_SHA256 = (
    "013a71426d45772bcbf471947c26353e7afeeff5acdbb127a2ca64fd73248846"
)
EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256 = (
    "9e6c35163b10e9ffcfc511ce354f055dacac27db970447a665167b4140ce18b9"
)
EXPECTED_TOOL_REPOSITORY_BINDING = (
    "750f9e12f05d178d593394c8a3205928793cd9d4"
)

_FORBIDDEN_SEMANTIC_TOKENS = blind.FORBIDDEN_PUBLIC_METADATA_TOKENS
_AUDIT_CHANGE_BUCKETS = ("kept", "corrected", "rejected", "contextualized", "merged")


class G11BlindBundleError(RuntimeError):
    """The G11 blind bundle is incomplete, unsafe, or inconsistent."""


class G11BlindBundleSafetyError(G11BlindBundleError):
    """A safety invariant (isolation, create-once, flags) was violated."""


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise G11BlindBundleError(
            "G11 blind-bundle value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G11BlindBundleError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _read_json(path: Path | str, label: str) -> Any:
    target = Path(path)
    if not target.is_file():
        raise G11BlindBundleError(f"{label} is missing: {target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G11BlindBundleError(f"{label} cannot be read: {target}") from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G11BlindBundleError(f"{label} must be a JSON object")
    return dict(value)


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise G11BlindBundleError(f"{label} must be a lowercase SHA-256")
    return value


def _repository_head(repo_root: Path | str) -> str:
    repo = Path(repo_root).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git unavailable
        raise G11BlindBundleError(f"cannot resolve repository HEAD: {exc}") from exc
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        raise G11BlindBundleError("repository HEAD cannot be resolved")
    return head


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _file_tree_manifest(directory: Path, *, path_prefix: str) -> dict[str, Any]:
    """Deterministic physical fingerprint of a directory (symlinks rejected)."""
    base = Path(directory)
    if base.is_symlink():
        raise G11BlindBundleError(f"tree root must not be a symlink: {base}")
    if not base.is_dir():
        raise G11BlindBundleError(f"tree root is missing: {base}")
    records: list[dict[str, Any]] = []
    for path in sorted(
        base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()
    ):
        if path.is_symlink():
            raise G11BlindBundleError(f"tree contains a symlink: {path}")
        if not path.is_file():
            continue
        payload = path.read_bytes()
        relative = path.relative_to(base).as_posix()
        records.append(
            {
                "path": f"{path_prefix}/{relative}" if path_prefix else relative,
                "bytes": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )
    return {
        "files": records,
        "file_count": len(records),
        "byte_count": sum(record["bytes"] for record in records),
        "sha256": _sha256_json(records),
    }


# --------------------------------------------------------------------------- #
# Frozen semantic sanitization
# --------------------------------------------------------------------------- #


def _has_forbidden_key(key: Any) -> bool:
    text = str(key).casefold()
    return any(token in text for token in _FORBIDDEN_SEMANTIC_TOKENS)


def _strip_forbidden(value: Any) -> Any:
    """Recursively remove operational identity/accounting keys."""
    if isinstance(value, Mapping):
        return {
            str(key): _strip_forbidden(item)
            for key, item in value.items()
            if not _has_forbidden_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_strip_forbidden(item) for item in value]
    return value


def _glossary_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    entries = raw.get("entries", {}) if isinstance(raw, Mapping) else {}
    if not isinstance(entries, Mapping):
        return {}
    result: dict[str, Any] = {}
    for source_key in sorted(entries, key=str):
        entry = entries[source_key]
        entry = entry if isinstance(entry, Mapping) else {}
        source_term = entry.get("source_term", entry.get("chinese", source_key))
        result[str(source_key)] = {
            "source_key": str(source_key),
            "source_term": str(source_term if source_term is not None else ""),
            "english": entry.get("english", ""),
            "category": entry.get("category", ""),
            "status": entry.get("status", ""),
            "approved_renderings": _strip_forbidden(
                entry.get("approved_renderings", [])
            ),
            "alias_of": entry.get("alias_of"),
            "notes": entry.get("notes", ""),
        }
    return result


def _continuity_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _strip_forbidden(dict(raw)) if isinstance(raw, Mapping) else {}


def _issues_projection(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = raw.get("issues", []) if isinstance(raw, Mapping) else []
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "chapter": item.get("chapter"),
                "type": item.get("type"),
                "details": _strip_forbidden(item.get("details")),
            }
        )
    return result


def _audit_history_projection(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    audits = raw.get("audits", []) if isinstance(raw, Mapping) else []
    if not isinstance(audits, list):
        return []
    result: list[dict[str, Any]] = []
    for record in audits:
        if not isinstance(record, Mapping):
            continue
        changes = record.get("changes", {})
        changes = changes if isinstance(changes, Mapping) else {}
        retroactive = record.get("retroactive", {})
        retroactive = retroactive if isinstance(retroactive, Mapping) else {}
        result.append(
            {
                "start_chapter": record.get("start_chapter"),
                "end_chapter": record.get("end_chapter"),
                "changes": {
                    bucket: _strip_forbidden(changes.get(bucket, []))
                    for bucket in _AUDIT_CHANGE_BUCKETS
                },
                "retroactive": {
                    "chapters_changed": _strip_forbidden(
                        retroactive.get("chapters_changed", [])
                    ),
                    "replacement_count": retroactive.get("replacement_count", 0),
                },
                "issues": _strip_forbidden(record.get("issues", [])),
            }
        )
    return result


def _assert_projection_clean(projection: Mapping[str, Any], label: str) -> None:
    try:
        blind.assert_no_forbidden_metadata(projection, label)
    except blind.G11BlindProtocolError as exc:
        raise G11BlindBundleError(
            f"public state projection leaked operational metadata: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Final-freeze predecessor validation (read-only)
# --------------------------------------------------------------------------- #


def _final_freeze_binding() -> dict[str, Any]:
    return {
        "final_freeze_id": EXPECTED_FINAL_FREEZE_ID,
        "g11_final_freeze_file_sha256": EXPECTED_FINAL_FREEZE_FILE_SHA256,
        "execution_tree_manifest_file_sha256": EXPECTED_EXECUTION_MANIFEST_FILE_SHA256,
        "g11_final_freeze_summary_file_sha256": EXPECTED_FINAL_FREEZE_SUMMARY_SHA256,
        "execution_snapshot_tree_sha256": EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256,
        "tool_repository_binding": EXPECTED_TOOL_REPOSITORY_BINDING,
    }


def verify_final_freeze_predecessor(final_freeze_root: Path | str) -> dict[str, Any]:
    """Re-prove every immutable G11 final-freeze identity, read-only."""
    raw = Path(final_freeze_root).expanduser()
    if raw.is_symlink():
        raise G11BlindBundleError("G11 final-freeze root is a symlink")
    root = raw.resolve()
    if not root.is_dir():
        raise G11BlindBundleError(f"G11 final-freeze root is missing: {root}")

    freeze_path = root / FINAL_FREEZE_DOCUMENT_FILENAME
    document = _require_mapping(_read_json(freeze_path, "G11 final-freeze document"), "G11 final-freeze document")
    freeze_id = document.get("freeze_id")
    payload = {key: value for key, value in document.items() if key != "freeze_id"}
    if freeze_id != EXPECTED_FINAL_FREEZE_ID:
        raise G11BlindBundleError("G11 final-freeze ID does not match the frozen binding")
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G11BlindBundleError("G11 final-freeze ID does not recompute")
    for key, expected in {
        "provider_calls": 0,
        "blind_bundle_created": False,
        "scoring_performed": False,
        "identity_revealed": False,
    }.items():
        if document.get(key) != expected:
            raise G11BlindBundleError(
                f"G11 final freeze is not a clean zero-provider boundary ({key})"
            )

    observed_freeze_sha = _sha256_file(freeze_path)
    if observed_freeze_sha != EXPECTED_FINAL_FREEZE_FILE_SHA256:
        raise G11BlindBundleError("G11 g11_final_freeze.json SHA drifted")

    manifest_path = root / FINAL_FREEZE_MANIFEST_FILENAME
    if _sha256_file(manifest_path) != EXPECTED_EXECUTION_MANIFEST_FILE_SHA256:
        raise G11BlindBundleError("G11 execution_tree_manifest.json SHA drifted")

    summary_path = root / FINAL_FREEZE_SUMMARY_FILENAME
    if _sha256_file(summary_path) != EXPECTED_FINAL_FREEZE_SUMMARY_SHA256:
        raise G11BlindBundleError("G11 G11_FINAL_FREEZE_SUMMARY.txt SHA drifted")

    repository = _require_mapping(document.get("repository"), "final freeze repository")
    if repository.get("worktree_head") != EXPECTED_TOOL_REPOSITORY_BINDING:
        raise G11BlindBundleError("G11 final-freeze tool repository binding drifted")

    snapshot = root / FINAL_FREEZE_SNAPSHOT_DIRECTORY
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise G11BlindBundleError("G11 execution snapshot directory is missing")

    stored_manifest = _require_mapping(
        _read_json(manifest_path, "G11 execution manifest"), "G11 execution manifest"
    )
    stored_records = stored_manifest.get("files")
    if not isinstance(stored_records, list):
        raise G11BlindBundleError("G11 execution manifest file records are invalid")
    if stored_manifest.get("sha256") != _sha256_json(stored_records):
        raise G11BlindBundleError("G11 execution manifest tree SHA is invalid")
    if stored_manifest.get("file_count") != len(stored_records):
        raise G11BlindBundleError("G11 execution manifest file count is invalid")
    if stored_manifest.get("byte_count") != sum(
        int(record["bytes"]) for record in stored_records
    ):
        raise G11BlindBundleError("G11 execution manifest byte count is invalid")

    try:
        recomputed = final_freeze.physical_tree_manifest(snapshot)
    except final_freeze.G11FinalFreezeError as exc:
        raise G11BlindBundleError(
            f"G11 execution snapshot is invalid: {exc}"
        ) from exc
    if recomputed["files"] != stored_records:
        raise G11BlindBundleError(
            "G11 execution snapshot differs from the frozen manifest "
            "(byte tampering or missing/extra files)"
        )
    if recomputed["sha256"] != stored_manifest["sha256"]:
        raise G11BlindBundleError("G11 execution snapshot tree identity drifted")
    if recomputed["sha256"] != EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256:
        raise G11BlindBundleError("G11 execution snapshot tree SHA drifted")
    execution = _require_mapping(document.get("execution"), "final freeze execution")
    if execution.get("snapshot_tree_sha256") != recomputed["sha256"]:
        raise G11BlindBundleError("G11 final-freeze execution binding drifted")

    try:
        evidence = final_freeze.build_execution_evidence(snapshot)
    except final_freeze.G11FinalFreezeError as exc:
        raise G11BlindBundleError(
            f"G11 execution snapshot evidence is invalid: {exc}"
        ) from exc
    for key in ("tracks", "model_ledgers", "completion"):
        if evidence[key] != document.get(key):
            raise G11BlindBundleError(
                f"G11 frozen {key} evidence does not match the snapshot evidence"
            )
    completion = _require_mapping(evidence["completion"], "G11 completion evidence")
    if completion.get("canonical_chapter_translations") != EXPECTED_CANONICAL_CHAPTERS:
        raise G11BlindBundleError("G11 canonical chapter shape is not 400")
    if completion.get("scheduled_audits") != EXPECTED_SCHEDULED_AUDITS:
        raise G11BlindBundleError("G11 scheduled audit shape is not 16")
    if completion.get("literary_matched_checkpoints") != EXPECTED_LITERARY_MATCHED:
        raise G11BlindBundleError("G11 literary matched shape is not 28")
    if completion.get("state_matched_checkpoints") != EXPECTED_STATE_MATCHED:
        raise G11BlindBundleError("G11 state matched shape is not 8")

    return {
        "root": root,
        "snapshot": snapshot,
        "final_freeze_id": freeze_id,
        "binding": _final_freeze_binding(),
        "final_freeze_file_sha256": observed_freeze_sha,
        "snapshot_tree_sha256": recomputed["sha256"],
        "completion": completion,
        "provider_calls": 0,
        "credential_access": False,
    }


# --------------------------------------------------------------------------- #
# Source material and checkpoint evidence
# --------------------------------------------------------------------------- #


def _track_by_novel_model() -> dict[str, dict[str, Mapping[str, Any]]]:
    result: dict[str, dict[str, Mapping[str, Any]]] = {}
    for novel_id in blind.NOVEL_ORDER:
        result[novel_id] = {
            track["model_id"]: track
            for track in TRACKS
            if track["novel_id"] == novel_id
        }
    return result


def load_source_libraries(snapshot: Path | str) -> dict[str, dict[str, Any]]:
    """Prove the two model tracks share one identical source chapter library."""
    snap = Path(snapshot)
    libraries: dict[str, dict[str, Any]] = {}
    tracks_by_novel = _track_by_novel_model()
    for novel_id in blind.NOVEL_ORDER:
        tracks = tracks_by_novel[novel_id]
        payloads: dict[str, bytes] = {}
        for model_id in MODEL_ORDER:
            run_id = tracks[model_id]["run_id"]
            path = snap / "runs" / run_id / "data" / "chapters.json"
            if not path.is_file():
                raise G11BlindBundleError(
                    f"source chapter library is missing for {novel_id} / {model_id}"
                )
            payloads[model_id] = path.read_bytes()
        if len(set(payloads.values())) != 1:
            raise G11BlindBundleError(
                f"{novel_id} model tracks bind different source chapter libraries"
            )
        library_sha = _sha256_bytes(next(iter(payloads.values())))
        for model_id in MODEL_ORDER:
            run_id = tracks[model_id]["run_id"]
            identity = _require_mapping(
                _read_json(
                    snap / "runs" / run_id / g11_runner.G11_RUN_IDENTITY_FILENAME,
                    "G11 run identity",
                ),
                "G11 run identity",
            )
            bindings = _require_mapping(
                identity.get("source_bindings"), "G11 run source bindings"
            )
            if bindings.get("chapter_library_sha256") != library_sha:
                raise G11BlindBundleError(
                    f"{run_id} run identity chapter library binding drifted"
                )
        try:
            raw = json.loads(next(iter(payloads.values())).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise G11BlindBundleError(
                f"{novel_id} chapter library is not valid JSON"
            ) from exc
        if not isinstance(raw, list):
            raise G11BlindBundleError(f"{novel_id} chapter library is not an array")
        index: dict[int, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise G11BlindBundleError(f"{novel_id} chapter entry is invalid")
            try:
                number = chapter_production_index(item)
            except ValueError as exc:
                raise G11BlindBundleError(
                    f"{novel_id} chapter entry is invalid: {exc}"
                ) from exc
            if number in index:
                raise G11BlindBundleError(f"{novel_id} duplicate Chapter {number}")
            index[number] = dict(item)
        missing = [n for n in range(1, 101) if n not in index]
        if missing:
            raise G11BlindBundleError(
                f"{novel_id} source library lacks Chapters 1-100: {missing[:5]}"
            )
        libraries[novel_id] = {"chapters": index, "library_sha256": library_sha}
    return libraries


def _source_text(libraries: Mapping[str, Any], novel_id: str, chapter: int) -> str:
    row = libraries[novel_id]["chapters"].get(chapter)
    text = row.get("text") if isinstance(row, Mapping) else None
    if not isinstance(text, str) or not text.strip():
        raise G11BlindBundleError(
            f"frozen source text is missing for {novel_id} Ch{chapter}"
        )
    return text


def _evidence_directory(snapshot: Path, run_id: str, chapter: int) -> Path:
    return (
        Path(snapshot)
        / g11_runner.G11_EVIDENCE_DIRECTORY
        / run_id
        / f"{int(chapter):04d}"
    )


def _read_checkpoint(snapshot: Path, run_id: str, chapter: int) -> tuple[Path, dict[str, Any]]:
    directory = _evidence_directory(snapshot, run_id, chapter)
    if not directory.is_dir():
        raise G11BlindBundleError(
            f"checkpoint evidence is missing for {run_id} Ch{chapter}"
        )
    checkpoint = _require_mapping(
        _read_json(directory / "checkpoint.json", "G11 checkpoint"),
        "G11 checkpoint",
    )
    if checkpoint.get("run_id") != run_id or checkpoint.get("chapter") != chapter:
        raise G11BlindBundleError(f"{run_id} Ch{chapter} checkpoint binds another case")
    if checkpoint.get("quality_checkpoint") is not True:
        raise G11BlindBundleError(f"{run_id} Ch{chapter} is not a quality checkpoint")
    return directory, checkpoint


def load_literary_evidence(
    snapshot: Path | str, run_id: str, chapter: int
) -> dict[str, Any]:
    """Validate one pre-extracted literary checkpoint chapter evidence file."""
    snap = Path(snapshot)
    directory, checkpoint = _read_checkpoint(snap, run_id, chapter)
    artifacts = _require_mapping(checkpoint.get("artifacts"), "checkpoint artifacts")
    record = artifacts.get("chapter")
    if not isinstance(record, Mapping) or record.get("destination") != "chapter.md":
        raise G11BlindBundleError(
            f"{run_id} Ch{chapter} checkpoint has no chapter.md artifact record"
        )
    chapter_md = directory / "chapter.md"
    if not chapter_md.is_file():
        raise G11BlindBundleError(
            f"literary checkpoint chapter.md is missing for {run_id} Ch{chapter}"
        )
    payload = chapter_md.read_bytes()
    observed = _sha256_bytes(payload)
    if observed != record.get("sha256"):
        raise G11BlindBundleError(
            f"{run_id} Ch{chapter} chapter.md SHA does not match the "
            "checkpoint artifact record"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise G11BlindBundleError(
            f"{run_id} Ch{chapter} chapter.md is not valid UTF-8"
        ) from exc
    if not text.strip():
        raise G11BlindBundleError(f"{run_id} Ch{chapter} chapter.md is empty")
    return {
        "text": text,
        "chapter_md_sha256": observed,
        "checkpoint_sha256": _sha256_file(directory / "checkpoint.json"),
        "checkpoint_path": f"{g11_runner.G11_EVIDENCE_DIRECTORY}/{run_id}/{int(chapter):04d}/checkpoint.json",
    }


def _state_projection(directory: Path) -> dict[str, Any]:
    required = {
        "glossary": "glossary.json",
        "continuity": "continuity.json",
        "issues": "issues.json",
        "audit_history": "audit_history.json",
    }
    payloads: dict[str, Any] = {}
    for label, name in required.items():
        path = directory / name
        if not path.is_file():
            raise G11BlindBundleError(
                f"state checkpoint evidence is missing {name}: {directory}"
            )
        payloads[label] = _require_mapping(_read_json(path, f"checkpoint {name}"), name)
    projection = {
        "glossary": _glossary_projection(payloads["glossary"]),
        "continuity": _continuity_projection(payloads["continuity"]),
        "issues": _issues_projection(payloads["issues"]),
        "audit_history": _audit_history_projection(payloads["audit_history"]),
    }
    _assert_projection_clean(projection, "state-projection")
    return projection


def load_state_evidence(
    snapshot: Path | str, run_id: str, chapter: int
) -> dict[str, Any]:
    """Validate one pre-extracted state checkpoint and project it blind-safe."""
    snap = Path(snapshot)
    directory, checkpoint = _read_checkpoint(snap, run_id, chapter)
    if checkpoint.get("state_evidence_checkpoint") is not True:
        raise G11BlindBundleError(
            f"{run_id} Ch{chapter} is not a state evidence checkpoint"
        )
    projection = _state_projection(directory)
    return {
        "projection": projection,
        "checkpoint_sha256": _sha256_file(directory / "checkpoint.json"),
        "checkpoint_path": f"{g11_runner.G11_EVIDENCE_DIRECTORY}/{run_id}/{int(chapter):04d}/checkpoint.json",
    }


# --------------------------------------------------------------------------- #
# Private candidate records
# --------------------------------------------------------------------------- #


def build_private_literary_records(
    snapshot: Path | str,
    sources: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the 28 private literary rows from frozen checkpoint evidence."""
    snap = Path(snapshot)
    tracks_by_novel = _track_by_novel_model()
    records: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    for novel_id in blind.NOVEL_ORDER:
        tracks = tracks_by_novel[novel_id]
        for chapter in blind.LITERARY_CHECKPOINTS:
            pair_id = f"G11L-{novel_id}-CH{int(chapter):03d}"
            translations: dict[str, str] = {}
            case_evidence: dict[str, Any] = {}
            for model_id in MODEL_ORDER:
                run_id = tracks[model_id]["run_id"]
                loaded = load_literary_evidence(snap, run_id, chapter)
                translations[model_id] = loaded["text"]
                case_evidence[model_id] = {
                    "checkpoint_path": loaded["checkpoint_path"],
                    "checkpoint_sha256": loaded["checkpoint_sha256"],
                    "content_sha256": _sha256_text(loaded["text"]),
                }
            source_text = _source_text(sources, novel_id, chapter)
            records.append(
                {
                    "pair_id": pair_id,
                    "novel_id": novel_id,
                    "chapter": int(chapter),
                    "source_language": blind.NOVEL_SOURCE_LANGUAGE[novel_id],
                    "source_text": source_text,
                    "translations": translations,
                }
            )
            evidence[pair_id] = {
                "source_text_sha256": _sha256_text(source_text),
                "candidates": case_evidence,
            }
    if len(records) != EXPECTED_LITERARY_COUNT:
        raise G11BlindBundleError("G11 literary roster is not 28 cases")
    return records, evidence


def _reference_chapters(
    sources: Mapping[str, Any], novel_id: str, checkpoint: int
) -> list[dict[str, Any]]:
    start, end = blind.REFERENCE_WINDOWS[checkpoint]
    return [
        {
            "chapter": chapter,
            "source_text": _source_text(sources, novel_id, chapter),
            "source_text_sha256": _sha256_text(
                _source_text(sources, novel_id, chapter)
            ),
        }
        for chapter in range(start, end + 1)
    ]


def build_private_longitudinal_records(
    snapshot: Path | str,
    sources: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the 8 private longitudinal rows from frozen state evidence."""
    snap = Path(snapshot)
    tracks_by_novel = _track_by_novel_model()
    records: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    for novel_id in blind.NOVEL_ORDER:
        tracks = tracks_by_novel[novel_id]
        for checkpoint in blind.LONGITUDINAL_CHECKPOINTS:
            tracks_projection: dict[str, Any] = {}
            case_evidence: dict[str, Any] = {}
            for model_id in MODEL_ORDER:
                run_id = tracks[model_id]["run_id"]
                loaded = load_state_evidence(snap, run_id, checkpoint)
                tracks_projection[model_id] = loaded["projection"]
                case_evidence[model_id] = {
                    "checkpoint_path": loaded["checkpoint_path"],
                    "checkpoint_sha256": loaded["checkpoint_sha256"],
                    "state_sha256": _sha256_json(loaded["projection"]),
                }
            chapters = _reference_chapters(sources, novel_id, checkpoint)
            pair_id = f"G11S-{novel_id}-CH{int(checkpoint):03d}"
            records.append(
                {
                    "visible_track_pair_id": pair_id,
                    "novel_id": novel_id,
                    "checkpoint": int(checkpoint),
                    "source_language": blind.NOVEL_SOURCE_LANGUAGE[novel_id],
                    "reference_window": list(blind.REFERENCE_WINDOWS[checkpoint]),
                    "reference_chapters": chapters,
                    "tracks": tracks_projection,
                }
            )
            evidence[pair_id] = {
                "reference_window_sha256": _sha256_json(
                    [
                        {
                            "chapter": row["chapter"],
                            "source_text_sha256": row["source_text_sha256"],
                        }
                        for row in chapters
                    ]
                ),
                "candidates": case_evidence,
            }
    if len(records) != EXPECTED_LONGITUDINAL_COUNT:
        raise G11BlindBundleError("G11 longitudinal roster is not 8 cases")
    return records, evidence


def build_private_input_manifest(
    *,
    private_key: Mapping[str, Any],
    literary_records: Sequence[Mapping[str, Any]],
    literary_evidence: Mapping[str, Any],
    longitudinal_records: Sequence[Mapping[str, Any]],
    longitudinal_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind every private candidate to its checkpoint, SHA, and public case ID."""
    literary_rows = []
    for record in literary_records:
        pair_id = record["pair_id"]
        literary_rows.append(
            {
                "visible_pair_id": pair_id,
                "novel_id": record["novel_id"],
                "chapter": record["chapter"],
                "source_language": record["source_language"],
                "source_text_sha256": literary_evidence[pair_id]["source_text_sha256"],
                "candidates": copy.deepcopy(literary_evidence[pair_id]["candidates"]),
            }
        )
    longitudinal_rows = []
    for record in longitudinal_records:
        pair_id = record["visible_track_pair_id"]
        longitudinal_rows.append(
            {
                "visible_track_pair_id": pair_id,
                "novel_id": record["novel_id"],
                "checkpoint": record["checkpoint"],
                "reference_window": list(record["reference_window"]),
                "reference_window_sha256": longitudinal_evidence[pair_id][
                    "reference_window_sha256"
                ],
                "candidates": copy.deepcopy(
                    longitudinal_evidence[pair_id]["candidates"]
                ),
            }
        )
    return {
        "schema_version": G11BB_SCHEMA_VERSION,
        "revision": PRIVATE_INPUT_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_final_freeze_id": private_key["g11_final_freeze_id"],
        "literary_case_count": len(literary_rows),
        "longitudinal_case_count": len(longitudinal_rows),
        "literary": literary_rows,
        "longitudinal": longitudinal_rows,
        "provider_calls": 0,
        "credential_access": False,
    }


# --------------------------------------------------------------------------- #
# Public bundle construction
# --------------------------------------------------------------------------- #


def _build_public_bundle(
    private_key: Mapping[str, Any],
    literary_records: Sequence[Mapping[str, Any]],
    longitudinal_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        bundle = blind.build_public_blind_bundle(
            private_key=private_key,
            literary_records=literary_records,
            longitudinal_records=longitudinal_records,
        )
    except blind.G11BlindProtocolError as exc:
        raise G11BlindBundleError(f"public blind bundle is invalid: {exc}") from exc
    return bundle


def _case_filename(case_id: str) -> str:
    return f"{case_id}.json"


def _public_manifest(
    bundle: Mapping[str, Any],
    *,
    literary_files: Sequence[Mapping[str, Any]],
    longitudinal_files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": G11BB_SCHEMA_VERSION,
        "revision": blind.PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_final_freeze_id": bundle["g11_final_freeze_id"],
        "private_identity_key_commitment_sha256": bundle[
            "private_identity_key_commitment_sha256"
        ],
        "literary_rubric": copy.deepcopy(bundle["literary_rubric"]),
        "longitudinal_rubric": copy.deepcopy(bundle["longitudinal_rubric"]),
        "statistics_policy": copy.deepcopy(bundle["statistics_policy"]),
        "literary_case_count": len(literary_files),
        "longitudinal_case_count": len(longitudinal_files),
        "literary_cases": [copy.deepcopy(dict(row)) for row in literary_files],
        "longitudinal_cases": [
            copy.deepcopy(dict(row)) for row in longitudinal_files
        ],
        "identity_revealed": False,
        "scoring_performed": False,
        "provider_calls": 0,
        "credential_access": False,
    }


def _instructions_text() -> str:
    return (
        "# G11 blind evaluation instructions\n"
        "\n"
        "You are scoring an anonymized two-candidate translation/state comparison.\n"
        "\n"
        "* Candidate identity is blinded: each case exposes only `side_a` and\n"
        "  `side_b`.\n"
        "* **Do not try to infer model identity** from any content, ordering, or\n"
        "  metadata; identity is deliberately unknowable until the score freeze.\n"
        "* Score `side_a` and `side_b` **independently** before forming any\n"
        "  preference.\n"
        "* Use only the frozen literary and longitudinal rubrics in\n"
        "  `public_blind_manifest.json`.\n"
        "* Record a preference of exactly `A`, `B`, or `TIE` **only after** both\n"
        "  sides have been scored independently.\n"
        "* Do not use operational, billing, throughput, or infrastructural\n"
        "  considerations: none are provided and none may be guessed.\n"
        "* All 28 literary cases are primary; report win/tie/loss separately by\n"
        "  novel/language first and only pool afterwards.\n"
        "* The 8 longitudinal cases are descriptive repeated checkpoints across two\n"
        "  novels; there is no composite overall winner score.\n"
        "* Scores must be frozen completely (all 28 literary and all 8 longitudinal\n"
        "  rows) **before** any identity reveal.\n"
        "\n"
        "Each case is a separate JSON file referenced by the public manifest so the\n"
        "corpus can be evaluated in manageable batches.\n"
    )


# --------------------------------------------------------------------------- #
# Freeze document
# --------------------------------------------------------------------------- #


def _counts(private_key: Mapping[str, Any]) -> dict[str, Any]:
    per_novel = {
        novel_id: {
            "literary": sum(
                1
                for row in private_key["literary_mapping"]
                if row["novel_id"] == novel_id
            ),
            "state": sum(
                1
                for row in blind.longitudinal_roster()
                if row["novel_id"] == novel_id
            ),
        }
        for novel_id in blind.NOVEL_ORDER
    }
    return {
        "literary": EXPECTED_LITERARY_COUNT,
        "state": EXPECTED_LONGITUDINAL_COUNT,
        "per_novel": per_novel,
    }


def _literary_orientation_proof(private_key: Mapping[str, Any]) -> dict[str, Any]:
    global_counts = [
        sum(
            1
            for row in private_key["literary_mapping"]
            if row["side_a_model_id"] == model_id
        )
        for model_id in MODEL_ORDER
    ]
    per_novel = {
        novel_id: sorted(
            sum(
                1
                for row in private_key["literary_mapping"]
                if row["novel_id"] == novel_id and row["side_a_model_id"] == model_id
            )
            for model_id in MODEL_ORDER
        )
        for novel_id in blind.NOVEL_ORDER
    }
    balanced = all(
        counts == [blind.LITERARY_A_PER_MODEL_PER_NOVEL] * len(MODEL_ORDER)
        for counts in per_novel.values()
    ) and sorted(global_counts) == [blind.LITERARY_PER_NOVEL] * len(MODEL_ORDER)
    return {
        "total_cases": EXPECTED_LITERARY_COUNT,
        "per_novel_cases": blind.LITERARY_PER_NOVEL,
        "global_side_a_orientation_counts": sorted(global_counts),
        "per_novel_side_a_orientation_counts": per_novel,
        "balanced": balanced,
    }


def _longitudinal_orientation_proof(private_key: Mapping[str, Any]) -> dict[str, Any]:
    long_a = [row["side_a_model_id"] for row in private_key["longitudinal_mapping"]]
    return {
        "novel_count": len(blind.NOVEL_ORDER),
        "checkpoints": list(blind.LONGITUDINAL_CHECKPOINTS),
        "orientation_stable_across_checkpoints": all(
            tuple(row["checkpoints"]) == blind.LONGITUDINAL_CHECKPOINTS
            for row in private_key["longitudinal_mapping"]
        ),
        "one_novel_each_orientation": sorted(long_a) == sorted(MODEL_ORDER),
    }


def _generated_artifacts(staging: Path) -> dict[str, Any]:
    return {
        PUBLIC_TREE_MANIFEST_FILENAME: _sha256_file(
            staging / PUBLIC_TREE_MANIFEST_FILENAME
        ),
        f"{PUBLIC_DIRECTORY}/{INSTRUCTIONS_FILENAME}": _sha256_file(
            staging / PUBLIC_DIRECTORY / INSTRUCTIONS_FILENAME
        ),
    }


def _freeze_payload(
    *,
    staging: Path,
    repo_head: str,
    predecessor: Mapping[str, Any],
    private_key: Mapping[str, Any],
    public_tree: Mapping[str, Any],
) -> dict[str, Any]:
    private_key_rel = f"{PRIVATE_DIRECTORY}/{PRIVATE_KEY_FILENAME}"
    private_input_rel = f"{PRIVATE_DIRECTORY}/{PRIVATE_INPUT_FILENAME}"
    public_manifest_rel = f"{PUBLIC_DIRECTORY}/{PUBLIC_MANIFEST_FILENAME}"
    return {
        "schema_version": G11BB_SCHEMA_VERSION,
        "revision": G11BB_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "repository": {"head": repo_head},
        "final_freeze": copy.deepcopy(dict(predecessor["binding"])),
        "final_freeze_id": predecessor["final_freeze_id"],
        "private_identity_key_commitment_sha256": blind.private_identity_key_commitment(
            private_key
        ),
        "private_key": {
            "file": private_key_rel,
            "file_sha256": _sha256_file(staging / private_key_rel),
            "commitment_sha256": blind.private_identity_key_commitment(private_key),
        },
        "private_files": {
            private_key_rel: _sha256_file(staging / private_key_rel),
            private_input_rel: _sha256_file(staging / private_input_rel),
        },
        "public_manifest": {
            "file": public_manifest_rel,
            "file_sha256": _sha256_file(staging / public_manifest_rel),
        },
        "public_manifest_sha256": _sha256_file(staging / public_manifest_rel),
        "public_bundle_tree": {
            "file_count": public_tree["file_count"],
            "byte_count": public_tree["byte_count"],
            "sha256": public_tree["sha256"],
        },
        "generated_artifacts": _generated_artifacts(staging),
        "counts": _counts(private_key),
        "literary_orientation_proof": _literary_orientation_proof(private_key),
        "longitudinal_orientation_proof": _longitudinal_orientation_proof(
            private_key
        ),
        "provider_calls": 0,
        "credential_access": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }


def _freeze_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(dict(payload))
    return {**body, "bundle_freeze_id": _sha256_json(body)}


def _summary_text(document: Mapping[str, Any]) -> str:
    counts = document["counts"]
    lines = [
        "G11 BLIND BUNDLE FREEZE",
        f"bundle_freeze_id: {document['bundle_freeze_id']}",
        f"benchmark_id: {document['benchmark_id']}",
        f"final_freeze_id: {document['final_freeze_id']}",
        "",
        "COUNTS",
        f"  literary cases: {counts['literary']}",
        f"  state cases: {counts['state']}",
    ]
    for novel_id in sorted(counts["per_novel"]):
        lines.append(
            f"    {novel_id}: literary {counts['per_novel'][novel_id]['literary']} "
            f"state {counts['per_novel'][novel_id]['state']}"
        )
    orientation = document["literary_orientation_proof"]
    lines.extend(
        [
            "",
            "ORIENTATION",
            "  literary side-a orientation counts (globally): "
            f"{orientation['global_side_a_orientation_counts']}",
            f"  literary orientation balanced: {orientation['balanced']}",
            "  longitudinal orientation stable: "
            f"{document['longitudinal_orientation_proof']['orientation_stable_across_checkpoints']}",
            "  longitudinal one-novel-each-orientation: "
            f"{document['longitudinal_orientation_proof']['one_novel_each_orientation']}",
            "",
            "SAFETY",
            "  provider_calls: 0",
            "  credential_access: false",
            "  scoring_performed: false",
            "  identity_revealed: false",
            "  paid_execution_authorized: false",
        ]
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Output-root isolation and staging
# --------------------------------------------------------------------------- #


def _assert_output_root_disjoint(
    output_root: Path,
    *,
    repo_root: Path | str,
    final_freeze_root: Path | str,
) -> Path:
    raw = Path(output_root).expanduser()
    if raw.is_symlink():
        raise G11BlindBundleSafetyError("G11 blind-bundle output root is a symlink")
    resolved = raw.resolve()
    for label, value in {
        "repository": repo_root,
        "final-freeze": final_freeze_root,
    }.items():
        frozen = Path(value).expanduser().resolve()
        if resolved == frozen or _is_within(resolved, frozen) or _is_within(frozen, resolved):
            raise G11BlindBundleSafetyError(
                f"G11 blind-bundle output root overlaps the {label} root"
            )
    return resolved


def _assert_output_root_available(output_root: Path) -> None:
    if output_root.exists():
        raise G11BlindBundleSafetyError(
            f"G11 blind-bundle output root already exists: {output_root}"
        )


def _write_package(
    staging: Path,
    *,
    private_key: Mapping[str, Any],
    private_manifest: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    private_dir = staging / PRIVATE_DIRECTORY
    private_dir.mkdir(parents=True, exist_ok=False)
    atomic_write_json(private_dir / PRIVATE_KEY_FILENAME, dict(private_key))
    atomic_write_json(private_dir / PRIVATE_INPUT_FILENAME, dict(private_manifest))

    public_dir = staging / PUBLIC_DIRECTORY
    literary_dir = public_dir / LITERARY_DIRECTORY
    longitudinal_dir = public_dir / LONGITUDINAL_DIRECTORY
    literary_dir.mkdir(parents=True, exist_ok=False)
    longitudinal_dir.mkdir(parents=True, exist_ok=False)

    literary_files = []
    for case in bundle["literary_cases"]:
        case_id = case["visible_pair_id"]
        relative = f"{LITERARY_DIRECTORY}/{_case_filename(case_id)}"
        path = public_dir / relative
        atomic_write_json(path, dict(case))
        literary_files.append(
            {
                "case_id": case_id,
                "path": relative,
                "sha256": _sha256_file(path),
                "novel_id": case["novel_id"],
                "chapter": case["chapter"],
                "source_language": case["source_language"],
            }
        )

    longitudinal_files = []
    for case in bundle["longitudinal_cases"]:
        case_id = case["visible_track_pair_id"]
        relative = f"{LONGITUDINAL_DIRECTORY}/{_case_filename(case_id)}"
        path = public_dir / relative
        atomic_write_json(path, dict(case))
        longitudinal_files.append(
            {
                "case_id": case_id,
                "path": relative,
                "sha256": _sha256_file(path),
                "novel_id": case["novel_id"],
                "checkpoint": case["checkpoint"],
                "source_language": case["source_language"],
            }
        )

    (public_dir / INSTRUCTIONS_FILENAME).write_text(
        _instructions_text(), encoding="utf-8", newline="\n"
    )
    manifest = _public_manifest(
        bundle,
        literary_files=literary_files,
        longitudinal_files=longitudinal_files,
    )
    atomic_write_json(public_dir / PUBLIC_MANIFEST_FILENAME, manifest)

    public_tree = _file_tree_manifest(public_dir, path_prefix=PUBLIC_DIRECTORY)
    atomic_write_json(
        staging / PUBLIC_TREE_MANIFEST_FILENAME,
        {
            "schema_version": G11BB_SCHEMA_VERSION,
            "revision": G11BB_REVISION,
            "benchmark_id": G11_BENCHMARK_ID,
            "root": PUBLIC_DIRECTORY,
            "files": public_tree["files"],
            "file_count": public_tree["file_count"],
            "byte_count": public_tree["byte_count"],
            "sha256": public_tree["sha256"],
            "provider_calls": 0,
        },
    )
    return {"public_tree": public_tree, "manifest": manifest}


def _assert_output_layout(output: Path) -> None:
    expected_top = {
        PRIVATE_DIRECTORY,
        PUBLIC_DIRECTORY,
        PUBLIC_TREE_MANIFEST_FILENAME,
        FREEZE_FILENAME,
        SUMMARY_FILENAME,
    }
    if not output.is_dir():
        raise G11BlindBundleError("G11 blind-bundle output root is missing")
    observed = {item.name for item in output.iterdir()}
    if observed != expected_top:
        raise G11BlindBundleError(
            "G11 blind-bundle output layout has extra or missing entries"
        )


# --------------------------------------------------------------------------- #
# create / verify
# --------------------------------------------------------------------------- #


def create_blind_bundle(
    *,
    repo_root: Path | str,
    final_freeze_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Create the immutable G11 blind bundle once (zero provider; secret is ephemeral)."""
    output = _assert_output_root_disjoint(
        Path(output_root),
        repo_root=repo_root,
        final_freeze_root=final_freeze_root,
    )
    _assert_output_root_available(output)
    repo = Path(repo_root).expanduser().resolve()

    predecessor = verify_final_freeze_predecessor(final_freeze_root)
    snapshot = predecessor["snapshot"]
    sources = load_source_libraries(snapshot)
    literary_records, literary_evidence = build_private_literary_records(
        snapshot, sources
    )
    longitudinal_records, longitudinal_evidence = (
        build_private_longitudinal_records(snapshot, sources)
    )

    secret_hex = os.urandom(32).hex()
    try:
        private_key = blind.build_private_identity_key(
            secret_hex, final_freeze_id=predecessor["final_freeze_id"]
        )
    finally:
        del secret_hex
    bundle = _build_public_bundle(
        private_key, literary_records, longitudinal_records
    )
    commitment = blind.private_identity_key_commitment(private_key)
    if bundle["private_identity_key_commitment_sha256"] != commitment:
        raise G11BlindBundleError("public bundle private-key commitment changed")
    private_manifest = build_private_input_manifest(
        private_key=private_key,
        literary_records=literary_records,
        literary_evidence=literary_evidence,
        longitudinal_records=longitudinal_records,
        longitudinal_evidence=longitudinal_evidence,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        written = _write_package(
            staging,
            private_key=private_key,
            private_manifest=private_manifest,
            bundle=bundle,
        )
        document = _freeze_document(
            _freeze_payload(
                staging=staging,
                repo_head=_repository_head(repo),
                predecessor=predecessor,
                private_key=private_key,
                public_tree=written["public_tree"],
            )
        )
        atomic_write_json(staging / FREEZE_FILENAME, document)
        (staging / SUMMARY_FILENAME).write_text(
            _summary_text(document), encoding="utf-8", newline="\n"
        )
        if output.exists():
            raise G11BlindBundleSafetyError(
                "G11 blind-bundle output root appeared during create-once staging"
            )
        os.rename(staging, output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return _result(output, document, commitment)


def _case_path(public_dir: Path, row: Any, label: str) -> Path:
    if not isinstance(row, Mapping):
        raise G11BlindBundleError(f"public {label} case manifest row is invalid")
    relative = row.get("path")
    if not isinstance(relative, str) or not relative:
        raise G11BlindBundleError(f"public {label} case path is invalid")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise G11BlindBundleError(f"public {label} case path escapes the public root")
    path = public_dir / candidate
    resolved_root = public_dir.resolve()
    if not _is_within(path.resolve(), resolved_root):
        raise G11BlindBundleError(f"public {label} case path escapes the public root")
    return path


def _reconstruct_bundle(
    manifest: Mapping[str, Any],
    *,
    output: Path,
) -> dict[str, Any]:
    public_dir = output / PUBLIC_DIRECTORY
    literary_rows = manifest.get("literary_cases", [])
    longitudinal_rows = manifest.get("longitudinal_cases", [])
    if not isinstance(literary_rows, list) or not isinstance(longitudinal_rows, list):
        raise G11BlindBundleError("public manifest case lists are invalid")
    if len(literary_rows) != EXPECTED_LITERARY_COUNT:
        raise G11BlindBundleError("public manifest literary case count is not 28")
    if len(longitudinal_rows) != EXPECTED_LONGITUDINAL_COUNT:
        raise G11BlindBundleError("public manifest longitudinal case count is not 8")

    literary_cases = []
    for row in literary_rows:
        path = _case_path(public_dir, row, "literary")
        if not path.is_file():
            raise G11BlindBundleError(f"public literary case is missing: {row.get('path')}")
        if _sha256_file(path) != row.get("sha256"):
            raise G11BlindBundleError(
                f"public literary case SHA drifted: {row.get('path')}"
            )
        literary_cases.append(_read_json(path, "public literary case"))
    longitudinal_cases = []
    for row in longitudinal_rows:
        path = _case_path(public_dir, row, "longitudinal")
        if not path.is_file():
            raise G11BlindBundleError(
                f"public longitudinal case is missing: {row.get('path')}"
            )
        if _sha256_file(path) != row.get("sha256"):
            raise G11BlindBundleError(
                f"public longitudinal case SHA drifted: {row.get('path')}"
            )
        longitudinal_cases.append(_read_json(path, "public longitudinal case"))
    return {
        "schema_version": blind.PUBLIC_BUNDLE_SCHEMA_VERSION,
        "revision": blind.PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_final_freeze_id": manifest.get("g11_final_freeze_id"),
        "private_identity_key_commitment_sha256": manifest.get(
            "private_identity_key_commitment_sha256"
        ),
        "literary_rubric": copy.deepcopy(manifest.get("literary_rubric")),
        "longitudinal_rubric": copy.deepcopy(manifest.get("longitudinal_rubric")),
        "statistics_policy": copy.deepcopy(manifest.get("statistics_policy")),
        "literary_cases": literary_cases,
        "longitudinal_cases": longitudinal_cases,
        "identity_revealed": manifest.get("identity_revealed"),
        "scoring_performed": manifest.get("scoring_performed"),
        "provider_calls": manifest.get("provider_calls"),
        "credential_access": manifest.get("credential_access"),
    }


def verify_blind_bundle(
    *,
    repo_root: Path | str,
    final_freeze_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Independently re-check the G11 blind bundle, read-only, zero provider."""
    output = Path(output_root).expanduser().resolve()
    if not output.is_dir():
        raise G11BlindBundleError(f"G11 blind-bundle output root is missing: {output}")
    _assert_output_layout(output)

    document = _require_mapping(
        _read_json(output / FREEZE_FILENAME, "G11 blind-bundle freeze"),
        "G11 blind-bundle freeze",
    )
    body = {key: value for key, value in document.items() if key != "bundle_freeze_id"}
    stored_id = document.get("bundle_freeze_id")
    _require_sha256(stored_id, "bundle freeze id")
    if _sha256_json(body) != stored_id:
        raise G11BlindBundleError("bundle_freeze_id does not recompute")
    if document.get("schema_version") != G11BB_SCHEMA_VERSION:
        raise G11BlindBundleError("G11 blind-bundle freeze schema changed")
    if document.get("revision") != G11BB_REVISION:
        raise G11BlindBundleError("G11 blind-bundle freeze revision changed")
    if document.get("benchmark_id") != G11_BENCHMARK_ID:
        raise G11BlindBundleError("G11 blind-bundle freeze binds another benchmark")
    for key, expected in {
        "provider_calls": 0,
        "credential_access": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }.items():
        if document.get(key) != expected:
            raise G11BlindBundleError(
                f"G11 blind-bundle safety flag {key} is not {expected!r}"
            )

    repo = Path(repo_root).expanduser().resolve()
    if document.get("repository") != {"head": _repository_head(repo)}:
        raise G11BlindBundleError("G11 blind-bundle repository binding changed")

    predecessor = verify_final_freeze_predecessor(final_freeze_root)
    if document.get("final_freeze") != predecessor["binding"]:
        raise G11BlindBundleError("G11 blind-bundle final-freeze binding changed")
    if document.get("final_freeze_id") != predecessor["final_freeze_id"]:
        raise G11BlindBundleError("G11 blind-bundle final-freeze id changed")

    private_key = _require_mapping(
        _read_json(
            output / PRIVATE_DIRECTORY / PRIVATE_KEY_FILENAME, "G11 private key"
        ),
        "G11 private key",
    )
    try:
        blind.validate_private_identity_key(private_key)
        commitment = blind.private_identity_key_commitment(private_key)
    except blind.G11BlindProtocolError as exc:
        raise G11BlindBundleError(f"private identity key is invalid: {exc}") from exc
    if document["private_key"]["commitment_sha256"] != commitment:
        raise G11BlindBundleError("G11 private-key commitment changed")
    if document.get("private_identity_key_commitment_sha256") != commitment:
        raise G11BlindBundleError("G11 root private-key commitment changed")
    if private_key.get("g11_final_freeze_id") != predecessor["final_freeze_id"]:
        raise G11BlindBundleError("private key binds another final freeze")

    for relative, expected in document["private_files"].items():
        if _sha256_file(output / relative) != expected:
            raise G11BlindBundleError(f"private artifact SHA drifted: {relative}")

    public_tree = _file_tree_manifest(output / PUBLIC_DIRECTORY, path_prefix=PUBLIC_DIRECTORY)
    stored_tree = _require_mapping(
        _read_json(output / PUBLIC_TREE_MANIFEST_FILENAME, "public tree manifest"),
        "public tree manifest",
    )
    if stored_tree.get("files") != public_tree["files"]:
        raise G11BlindBundleError(
            "public tree has extra, missing, or tampered files"
        )
    if stored_tree.get("sha256") != public_tree["sha256"]:
        raise G11BlindBundleError("public tree identity changed")
    if document.get("public_bundle_tree") != {
        "file_count": public_tree["file_count"],
        "byte_count": public_tree["byte_count"],
        "sha256": public_tree["sha256"],
    }:
        raise G11BlindBundleError("public bundle tree binding changed")

    manifest_path = output / PUBLIC_DIRECTORY / PUBLIC_MANIFEST_FILENAME
    manifest_sha = _sha256_file(manifest_path)
    if manifest_sha != document.get("public_manifest_sha256"):
        raise G11BlindBundleError("public manifest SHA changed")
    if document["public_manifest"]["file_sha256"] != manifest_sha:
        raise G11BlindBundleError("public manifest file binding changed")
    manifest = _require_mapping(
        _read_json(manifest_path, "public blind manifest"), "public blind manifest"
    )
    if document["generated_artifacts"] != _generated_artifacts(output):
        raise G11BlindBundleError("generated artifact hashes changed")

    reconstructed = _reconstruct_bundle(manifest, output=output)
    try:
        blind.validate_public_blind_bundle(reconstructed)
    except blind.G11BlindProtocolError as exc:
        raise G11BlindBundleError(f"public blind bundle is invalid: {exc}") from exc
    if reconstructed["private_identity_key_commitment_sha256"] != commitment:
        raise G11BlindBundleError("public bundle private-key commitment changed")
    if reconstructed["g11_final_freeze_id"] != predecessor["final_freeze_id"]:
        raise G11BlindBundleError("public bundle final-freeze binding changed")
    if len(reconstructed["literary_cases"]) != EXPECTED_LITERARY_COUNT:
        raise G11BlindBundleError("public literary case count changed")
    if len(reconstructed["longitudinal_cases"]) != EXPECTED_LONGITUDINAL_COUNT:
        raise G11BlindBundleError("public longitudinal case count changed")

    snapshot = predecessor["snapshot"]
    sources = load_source_libraries(snapshot)
    literary_records, literary_evidence = build_private_literary_records(
        snapshot, sources
    )
    longitudinal_records, longitudinal_evidence = (
        build_private_longitudinal_records(snapshot, sources)
    )
    expected_manifest = build_private_input_manifest(
        private_key=private_key,
        literary_records=literary_records,
        literary_evidence=literary_evidence,
        longitudinal_records=longitudinal_records,
        longitudinal_evidence=longitudinal_evidence,
    )
    if _sha256_json(copy.deepcopy(dict(private_key))) != commitment:
        raise G11BlindBundleError("private identity key is not internally consistent")
    private_input_path = output / PRIVATE_DIRECTORY / PRIVATE_INPUT_FILENAME
    expected_sha = _sha256_json(expected_manifest)
    stored_private_input = _require_mapping(
        _read_json(private_input_path, "G11 private input manifest"),
        "G11 private input manifest",
    )
    if _sha256_json(copy.deepcopy(stored_private_input)) != expected_sha:
        raise G11BlindBundleError(
            "private input manifest does not derive from the final-freeze evidence"
        )

    expected_bundle = _build_public_bundle(
        private_key, literary_records, longitudinal_records
    )
    if _sha256_json(reconstructed) != _sha256_json(expected_bundle):
        raise G11BlindBundleError(
            "public bundle does not correspond exactly to the private candidates "
            "after the A/B mapping"
        )

    _validate_private_mapping(private_key)
    if document.get("counts") != _counts(private_key):
        raise G11BlindBundleError("G11 frozen counts changed")
    if document.get("literary_orientation_proof") != _literary_orientation_proof(
        private_key
    ):
        raise G11BlindBundleError("G11 literary orientation proof changed")
    if document.get(
        "longitudinal_orientation_proof"
    ) != _longitudinal_orientation_proof(private_key):
        raise G11BlindBundleError("G11 longitudinal orientation proof changed")
    if not document["literary_orientation_proof"].get("balanced"):
        raise G11BlindBundleError("G11 literary orientation is not balanced")

    return {
        "bundle_freeze_id": document["bundle_freeze_id"],
        "output_root": str(output),
        "verified": True,
        "final_freeze_id": predecessor["final_freeze_id"],
        "literary_case_count": EXPECTED_LITERARY_COUNT,
        "longitudinal_case_count": EXPECTED_LONGITUDINAL_COUNT,
        "provider_calls": 0,
        "credential_access": False,
        "scoring_performed": False,
        "identity_revealed": False,
    }


def _validate_private_mapping(private_key: Mapping[str, Any]) -> None:
    """Prove 7/7 literary per novel and one-novel-each longitudinal orientation."""
    literary = private_key["literary_mapping"]
    for novel_id in blind.NOVEL_ORDER:
        novel_counts = [
            sum(
                1
                for row in literary
                if row["novel_id"] == novel_id and row["side_a_model_id"] == model_id
            )
            for model_id in MODEL_ORDER
        ]
        if novel_counts != [blind.LITERARY_A_PER_MODEL_PER_NOVEL] * len(MODEL_ORDER):
            raise G11BlindBundleError(
                f"{novel_id} literary orientation is not 7/7 balanced"
            )
    long_a = sorted(
        row["side_a_model_id"] for row in private_key["longitudinal_mapping"]
    )
    if long_a != sorted(MODEL_ORDER):
        raise G11BlindBundleError(
            "longitudinal orientation is not one novel per side-A model"
        )


def _result(
    output: Path, document: Mapping[str, Any], commitment: str
) -> dict[str, Any]:
    return {
        "bundle_freeze_id": document["bundle_freeze_id"],
        "output_root": str(output),
        "final_freeze_id": document["final_freeze_id"],
        "literary_case_count": EXPECTED_LITERARY_COUNT,
        "longitudinal_case_count": EXPECTED_LONGITUDINAL_COUNT,
        "provider_calls": 0,
        "credential_access": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
        "private_identity_key_commitment_sha256": commitment,
        "public_tree_sha256": document["public_bundle_tree"]["sha256"],
    }


def assert_zero_provider_safety() -> None:
    if blind.G11_BENCHMARK_ID != G11_BENCHMARK_ID:
        raise G11BlindBundleSafetyError("G11 benchmark identity changed")
    if EXPECTED_LITERARY_COUNT != 28:
        raise G11BlindBundleSafetyError("G11 literary case total changed")
    if EXPECTED_LONGITUDINAL_COUNT != 8:
        raise G11BlindBundleSafetyError("G11 longitudinal case total changed")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/verify the zero-provider G11 blind bundle freeze."
    )
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--final-freeze", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {
        "repo_root": args.repo,
        "final_freeze_root": args.final_freeze,
        "output_root": args.output,
    }
    if args.action == "create":
        result = create_blind_bundle(**kwargs)
    else:
        result = verify_blind_bundle(**kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_EXECUTION_MANIFEST_FILE_SHA256",
    "EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256",
    "EXPECTED_FINAL_FREEZE_FILE_SHA256",
    "EXPECTED_FINAL_FREEZE_ID",
    "EXPECTED_FINAL_FREEZE_SUMMARY_SHA256",
    "EXPECTED_TOOL_REPOSITORY_BINDING",
    "FREEZE_FILENAME",
    "G11BlindBundleError",
    "G11BlindBundleSafetyError",
    "G11BB_REVISION",
    "G11BB_SCHEMA_VERSION",
    "INSTRUCTIONS_FILENAME",
    "PRIVATE_DIRECTORY",
    "PRIVATE_INPUT_FILENAME",
    "PRIVATE_KEY_FILENAME",
    "PUBLIC_DIRECTORY",
    "PUBLIC_MANIFEST_FILENAME",
    "PUBLIC_TREE_MANIFEST_FILENAME",
    "SUMMARY_FILENAME",
    "assert_zero_provider_safety",
    "build_private_input_manifest",
    "build_private_literary_records",
    "build_private_longitudinal_records",
    "create_blind_bundle",
    "load_literary_evidence",
    "load_source_libraries",
    "load_state_evidence",
    "main",
    "verify_blind_bundle",
    "verify_final_freeze_predecessor",
]
