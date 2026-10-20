"""Proof and cleanup for stale completed-chapter pending residue.

The durable progress marker is authoritative only after the reviewed chapter
commit tail has succeeded. Pending files are intentionally unlinked afterward.
A process stop can therefore leave either:

- the reviewed pending Markdown + metadata pair; or
- metadata only, because cleanup unlinks Markdown before JSON.

This module proves only those post-commit residue states. It never calls a
provider, changes progress, rewrites final prose/QA, or guesses around malformed
evidence. Cleanup preserves the Markdown-first, metadata-second order so an
interruption remains independently provable on the next retry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chapter_library import index_chapter_library
from production_saved_checkpoint_identity import (
    REVIEW_CHECKPOINT_IDENTITY_KEY,
    REVIEW_CHECKPOINT_IDENTITY_VERSION,
    STATE_CHECKPOINT_IDENTITY_KEY,
    STATE_CHECKPOINT_IDENTITY_VERSION,
)
from production_provider_runtime import canonical_pending_provenance


class CompletedPendingCleanupError(RuntimeError):
    """Completed pending residue cannot be proven safe to remove."""


@dataclass(frozen=True)
class CompletedPendingCleanupRecord:
    chapter: int
    state: str
    pending_md: Path
    pending_json: Path
    final_file: Path
    qa_file: Path
    pending_md_sha256: str
    pending_json_sha256: str
    final_sha256: str
    qa_sha256: str


@dataclass(frozen=True)
class CompletedPendingCleanupInspection:
    records: tuple[CompletedPendingCleanupRecord, ...]

    @property
    def chapters(self) -> tuple[int, ...]:
        return tuple(record.chapter for record in self.records)

    @property
    def required(self) -> bool:
        return bool(self.records)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(str(value or "").encode("utf-8"))


def _sha256_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return ""
    return _sha256_bytes(path.read_bytes())


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(raw)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CompletedPendingCleanupError(
            f"{label} is missing or not a regular file: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompletedPendingCleanupError(
            f"cannot read {label}: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CompletedPendingCleanupError(
            f"{label} must contain a JSON object"
        )
    return value


def _validate_checkpoint_identity(
    saved: Any,
    *,
    label: str,
    expected_version: str,
    chapter: int,
    novel_id: str,
    profile_id: str,
    source_sha256: str,
    translation_sha256: str,
) -> dict[str, Any]:
    if not isinstance(saved, dict):
        raise CompletedPendingCleanupError(f"{label} is missing")

    record = dict(saved)
    identity_sha256 = record.pop("identity_sha256", None)
    if not isinstance(identity_sha256, str) or len(identity_sha256) != 64:
        raise CompletedPendingCleanupError(
            f"{label} has no valid identity_sha256"
        )
    if _canonical_hash(record) != identity_sha256:
        raise CompletedPendingCleanupError(
            f"{label} identity_sha256 does not match its fields"
        )

    if record.get("version") != expected_version:
        raise CompletedPendingCleanupError(
            f"{label} uses an unsupported version"
        )
    if record.get("chapter") != chapter:
        raise CompletedPendingCleanupError(
            f"{label} is bound to a different chapter"
        )
    if record.get("novel_id") != novel_id:
        raise CompletedPendingCleanupError(
            f"{label} is bound to a different novel"
        )
    if record.get("profile_id") != profile_id:
        raise CompletedPendingCleanupError(
            f"{label} is bound to a different profile"
        )
    if record.get("source_sha256") != source_sha256:
        raise CompletedPendingCleanupError(
            f"{label} source hash does not match current source"
        )
    if record.get("translation_sha256") != translation_sha256:
        raise CompletedPendingCleanupError(
            f"{label} translation hash does not match committed final text"
        )
    model_id = record.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise CompletedPendingCleanupError(f"{label} has no valid model_id")

    record["identity_sha256"] = identity_sha256
    return record


def _validate_reviewed_metadata(
    *,
    chapter: int,
    meta_path: Path,
    qa_path: Path,
    source_text: str,
    final_text: str,
    novel_id: str,
    profile_id: str,
) -> None:
    meta = _read_json_object(meta_path, "completed pending metadata")
    if meta.get("chapter") != chapter:
        raise CompletedPendingCleanupError(
            f"completed pending Chapter {chapter} metadata chapter "
            "does not match filename"
        )
    if meta.get("stage") != "reviewed":
        raise CompletedPendingCleanupError(
            f"completed pending Chapter {chapter} is not a durable "
            "reviewed checkpoint"
        )

    required_objects = (
        "translation_stats",
        "state",
        "state_stats",
        "review_result",
        "review_stats",
        STATE_CHECKPOINT_IDENTITY_KEY,
        REVIEW_CHECKPOINT_IDENTITY_KEY,
    )
    for key in required_objects:
        if not isinstance(meta.get(key), dict):
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} metadata is missing {key}"
            )
    for key in ("eligible_candidates", "prefilter_rejected"):
        if not isinstance(meta.get(key), list):
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} metadata is missing {key}"
            )

    local_qa = meta.get("local_qa")
    if not isinstance(local_qa, dict) or local_qa.get("commit_allowed") is not True:
        raise CompletedPendingCleanupError(
            f"completed pending Chapter {chapter} has no passing saved local QA"
        )

    source_sha256 = _sha256_text(source_text)
    translation_sha256 = _sha256_text(final_text)
    state_identity = _validate_checkpoint_identity(
        meta.get(STATE_CHECKPOINT_IDENTITY_KEY),
        label=f"completed pending Chapter {chapter} state checkpoint identity",
        expected_version=STATE_CHECKPOINT_IDENTITY_VERSION,
        chapter=chapter,
        novel_id=novel_id,
        profile_id=profile_id,
        source_sha256=source_sha256,
        translation_sha256=translation_sha256,
    )
    review_identity = _validate_checkpoint_identity(
        meta.get(REVIEW_CHECKPOINT_IDENTITY_KEY),
        label=f"completed pending Chapter {chapter} review checkpoint identity",
        expected_version=REVIEW_CHECKPOINT_IDENTITY_VERSION,
        chapter=chapter,
        novel_id=novel_id,
        profile_id=profile_id,
        source_sha256=source_sha256,
        translation_sha256=translation_sha256,
    )
    if state_identity["model_id"] != review_identity["model_id"]:
        raise CompletedPendingCleanupError(
            f"completed pending Chapter {chapter} checkpoint model "
            "identities disagree"
        )

    pinned_model = meta.get("translation_model")
    if isinstance(pinned_model, str) and pinned_model.strip():
        try:
            model_record = canonical_pending_provenance(meta)
        except ValueError as exc:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has invalid "
                "model provenance"
            ) from exc
        if state_identity["model_id"] != model_record["translation_model"]:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} checkpoint model differs "
                "from pinned translation model"
            )
        for key, expected_value in model_record.items():
            if meta.get(key) != expected_value:
                raise CompletedPendingCleanupError(
                    f"completed pending Chapter {chapter} pinned model "
                    f"provenance field {key} is not canonical"
                )
    else:
        model_record = {}

    qa_disk = _read_json_object(qa_path, "committed QA evidence")
    expected_qa = dict(local_qa)
    expected_qa.update(model_record)
    if qa_disk != expected_qa:
        raise CompletedPendingCleanupError(
            f"committed QA differs from completed pending Chapter {chapter} "
            "saved QA and canonical model provenance"
        )


def inspect_completed_pending_cleanup(project) -> CompletedPendingCleanupInspection:
    """Return every exact completed-pending residue safe for local cleanup."""
    progress = _read_json_object(project.progress_file, "progress")
    completed_raw = progress.get("completed")
    if not isinstance(completed_raw, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in (completed_raw or [])
    ):
        raise CompletedPendingCleanupError(
            "progress.completed must be a list of positive chapter integers"
        )
    completed_list = list(completed_raw)
    if completed_list != sorted(set(completed_list)):
        raise CompletedPendingCleanupError(
            "progress.completed must be strictly increasing without duplicates"
        )
    last_completed = completed_list[-1] if completed_list else 0
    if completed_list != list(range(1, last_completed + 1)):
        raise CompletedPendingCleanupError(
            "progress.completed must be continuous from Chapter 1"
        )
    if progress.get("last_completed") != last_completed:
        raise CompletedPendingCleanupError(
            "progress.last_completed does not match progress.completed"
        )
    if progress.get("next_chapter") != last_completed + 1:
        raise CompletedPendingCleanupError(
            "progress.next_chapter does not match progress.completed"
        )
    completed = set(completed_list)

    try:
        chapters = json.loads(
            Path(project.chapters_file).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompletedPendingCleanupError(
            f"cannot read chapter library: {exc}"
        ) from exc
    if not isinstance(chapters, list):
        raise CompletedPendingCleanupError(
            "chapter library must be a JSON list"
        )
    try:
        chapter_map = index_chapter_library(
            chapters,
            project.config.final_chapter,
            project.config.chapter_numbering,
        )
    except Exception as exc:
        raise CompletedPendingCleanupError(
            f"cannot validate chapter library for completed-pending cleanup: {exc}"
        ) from exc

    pending_dir = Path(project.pending_dir)
    if not pending_dir.exists():
        return CompletedPendingCleanupInspection(records=())
    if pending_dir.is_symlink() or not pending_dir.is_dir():
        raise CompletedPendingCleanupError(
            f"pending path is not a regular directory: {pending_dir}"
        )

    files = sorted(
        (
            path
            for path in pending_dir.iterdir()
            if path.is_file() or path.is_symlink()
        ),
        key=lambda item: item.name.casefold(),
    )
    stems = sorted(
        {
            path.stem
            for path in files
            if path.stem.isdigit() and int(path.stem) in completed
        },
        key=int,
    )

    records: list[CompletedPendingCleanupRecord] = []
    for stem in stems:
        chapter = int(stem)
        related = [path for path in files if path.stem == stem]
        unsupported = [
            path
            for path in related
            if path.suffix not in {".md", ".json"}
        ]
        if unsupported:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has unexpected sibling "
                "files: "
                + ", ".join(path.name for path in unsupported)
            )

        pending_md = pending_dir / f"{stem}.md"
        pending_json = pending_dir / f"{stem}.json"
        md_exists = pending_md.exists() or pending_md.is_symlink()
        json_exists = pending_json.exists() or pending_json.is_symlink()

        if md_exists and (
            pending_md.is_symlink() or not pending_md.is_file()
        ):
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} Markdown is not "
                "a regular file"
            )
        if json_exists and (
            pending_json.is_symlink() or not pending_json.is_file()
        ):
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} metadata is not "
                "a regular file"
            )
        if md_exists and not json_exists:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has Markdown without "
                "reviewed metadata"
            )
        if not json_exists:
            continue

        final_file = Path(project.chapter_dir) / f"{stem}.md"
        qa_file = Path(project.qa_dir) / f"{stem}.json"
        if final_file.is_symlink() or not final_file.is_file():
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has no regular "
                "committed final file"
            )
        if qa_file.is_symlink() or not qa_file.is_file():
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has no regular "
                "committed QA file"
            )

        try:
            final_bytes = final_file.read_bytes()
            final_text = final_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CompletedPendingCleanupError(
                f"cannot read committed final Chapter {chapter}: {exc}"
            ) from exc

        if md_exists and pending_md.read_bytes() != final_bytes:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} Markdown differs "
                "from committed final"
            )

        source_entry = chapter_map.get(chapter)
        source_text = (
            source_entry.get("text")
            if isinstance(source_entry, dict)
            else None
        )
        if not isinstance(source_text, str):
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {chapter} has no authoritative "
                "source text"
            )

        _validate_reviewed_metadata(
            chapter=chapter,
            meta_path=pending_json,
            qa_path=qa_file,
            source_text=source_text,
            final_text=final_text,
            novel_id=project.config.id,
            profile_id=project.config.profile,
        )

        records.append(
            CompletedPendingCleanupRecord(
                chapter=chapter,
                state="pair" if md_exists else "metadata_only",
                pending_md=pending_md,
                pending_json=pending_json,
                final_file=final_file,
                qa_file=qa_file,
                pending_md_sha256=(
                    _sha256_file(pending_md) if md_exists else ""
                ),
                pending_json_sha256=_sha256_file(pending_json),
                final_sha256=_sha256_file(final_file),
                qa_sha256=_sha256_file(qa_file),
            )
        )

    return CompletedPendingCleanupInspection(records=tuple(records))


def _require_snapshot(
    path: Path,
    expected_sha256: str,
    label: str,
) -> None:
    actual = _sha256_file(path)
    if not actual or actual != expected_sha256:
        raise CompletedPendingCleanupError(
            f"{label} changed after cleanup proof; refusing deletion"
        )


def cleanup_completed_pending(
    inspection: CompletedPendingCleanupInspection,
) -> tuple[int, ...]:
    """Delete only records already proven by the read-only inspector."""
    records = tuple(inspection.records)

    # Prove every snapshot again before deleting any candidate.
    for record in records:
        _require_snapshot(
            record.final_file,
            record.final_sha256,
            "committed final",
        )
        _require_snapshot(
            record.qa_file,
            record.qa_sha256,
            "committed QA",
        )
        _require_snapshot(
            record.pending_json,
            record.pending_json_sha256,
            "completed pending metadata",
        )
        if record.state == "pair":
            _require_snapshot(
                record.pending_md,
                record.pending_md_sha256,
                "completed pending Markdown",
            )
        elif record.state == "metadata_only":
            if record.pending_md.exists() or record.pending_md.is_symlink():
                raise CompletedPendingCleanupError(
                    "pending Markdown appeared after metadata-only cleanup proof"
                )
        else:
            raise CompletedPendingCleanupError(
                f"unsupported completed-pending cleanup state: "
                f"{record.state!r}"
            )

    removed: list[int] = []
    for record in records:
        try:
            if record.state == "pair":
                _require_snapshot(
                    record.pending_md,
                    record.pending_md_sha256,
                    "completed pending Markdown",
                )
                record.pending_md.unlink()

            # MD-first ordering is intentional. Recheck the surviving proof
            # anchors before the final metadata unlink.
            _require_snapshot(
                record.final_file,
                record.final_sha256,
                "committed final",
            )
            _require_snapshot(
                record.qa_file,
                record.qa_sha256,
                "committed QA",
            )
            _require_snapshot(
                record.pending_json,
                record.pending_json_sha256,
                "completed pending metadata",
            )
            record.pending_json.unlink()
        except CompletedPendingCleanupError:
            raise
        except OSError as exc:
            raise CompletedPendingCleanupError(
                f"completed pending Chapter {record.chapter} cleanup failed: "
                f"{exc}"
            ) from exc
        removed.append(record.chapter)

    return tuple(removed)
