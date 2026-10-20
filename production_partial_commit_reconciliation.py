"""Read-only proof helpers for reviewed partial chapter commits.

A recoverable partial commit is deliberately narrow:
- progress still points at the pending target;
- pending Markdown and JSON both remain;
- a final chapter file already exists and is byte-identical to pending Markdown;
- pending metadata is already at the reviewed stage with all deterministic
  commit-tail inputs durably saved;
- committed QA evidence exists and exactly matches the saved local-QA record,
  optionally after a caller-supplied read-only normalizer validates and removes
  known additive provenance fields.

Normal production supplies no normalizer and therefore retains exact historical
QA equality. This module never calls providers and never writes project data.
"""

import hashlib
import json
from pathlib import Path

from production_provider_runtime import canonical_pending_provenance
from production_saved_checkpoint_identity import (
    REVIEW_CHECKPOINT_IDENTITY_KEY,
    STATE_CHECKPOINT_IDENTITY_KEY,
)


NORMAL_RECOVERY_MODE = "resume_pending_pipeline"
PARTIAL_COMMIT_RECOVERY_MODE = "reconcile_reviewed_partial_commit_tail"
PARTIAL_COMMIT_EVIDENCE_VERSION = "reviewed-partial-commit-v1"


class PartialCommitReconciliationError(RuntimeError):
    pass


def _sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_object(path, label):
    path = Path(path)
    if not path.is_file():
        raise PartialCommitReconciliationError(
            f"{label} does not exist: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PartialCommitReconciliationError(
            f"cannot read {label}: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PartialCommitReconciliationError(
            f"{label} must contain a JSON object"
        )
    return value


def _normalize_committed_qa(qa_disk, qa_normalizer):
    """Apply an explicitly supplied read-only QA normalizer, if any."""
    if qa_normalizer is None:
        return qa_disk
    if not callable(qa_normalizer):
        raise PartialCommitReconciliationError(
            "committed QA normalizer must be callable"
        )
    try:
        normalized = qa_normalizer(dict(qa_disk))
    except PartialCommitReconciliationError:
        raise
    except Exception as exc:
        raise PartialCommitReconciliationError(
            "committed QA provenance normalization failed"
        ) from exc
    if not isinstance(normalized, dict):
        raise PartialCommitReconciliationError(
            "committed QA normalizer must return a JSON object"
        )
    return normalized


def inspect_reviewed_partial_commit_evidence(
    *,
    chapter_number,
    pending_file,
    pending_meta_file,
    final_file,
    qa_file,
    progress,
    meta=None,
    qa_normalizer=None,
):
    """Return exact local evidence for normal or commit-tail recovery.

    ``qa_normalizer`` is intentionally opt-in. It may only normalize the
    in-memory committed-QA object used for equality proof; the on-disk bytes are
    never changed and their original SHA-256 remains in returned evidence.
    """

    pending_file = Path(pending_file)
    pending_meta_file = Path(pending_meta_file)
    final_file = Path(final_file)
    qa_file = Path(qa_file)

    if not final_file.is_file():
        return {
            "mode": NORMAL_RECOVERY_MODE,
            "version": PARTIAL_COMMIT_EVIDENCE_VERSION,
            "final_exists": False,
            "final_sha256": "",
            "qa_sha256": "",
        }

    if not pending_file.is_file():
        raise PartialCommitReconciliationError(
            "final output exists ahead of progress but pending Markdown is missing"
        )
    if not pending_meta_file.is_file():
        raise PartialCommitReconciliationError(
            "final output exists ahead of progress but pending metadata is missing"
        )

    if final_file.read_bytes() != pending_file.read_bytes():
        raise PartialCommitReconciliationError(
            "final output exists ahead of progress but does not exactly match "
            "the paid pending translation"
        )

    meta = (
        _read_json_object(pending_meta_file, "pending metadata")
        if meta is None
        else meta
    )
    if not isinstance(meta, dict):
        raise PartialCommitReconciliationError(
            "pending metadata must be a JSON object"
        )
    if meta.get("chapter") not in (None, chapter_number):
        raise PartialCommitReconciliationError(
            "reviewed partial commit metadata is bound to a different chapter"
        )
    if meta.get("stage") != "reviewed":
        raise PartialCommitReconciliationError(
            "final output ahead of progress is recoverable only from a "
            "durable reviewed pending checkpoint"
        )

    qa = meta.get("local_qa")
    if not isinstance(qa, dict) or qa.get("commit_allowed") is not True:
        raise PartialCommitReconciliationError(
            "reviewed partial commit has no durable passing local-QA checkpoint"
        )

    required_objects = (
        "state",
        "review_result",
        "translation_stats",
        "state_stats",
        "review_stats",
        STATE_CHECKPOINT_IDENTITY_KEY,
        REVIEW_CHECKPOINT_IDENTITY_KEY,
    )
    for key in required_objects:
        if not isinstance(meta.get(key), dict):
            raise PartialCommitReconciliationError(
                f"reviewed partial commit metadata is missing {key}"
            )

    for key in ("eligible_candidates", "prefilter_rejected"):
        if not isinstance(meta.get(key), list):
            raise PartialCommitReconciliationError(
                f"reviewed partial commit metadata is missing {key}"
            )

    qa_disk_raw = _read_json_object(qa_file, "committed QA evidence")
    qa_disk = _normalize_committed_qa(qa_disk_raw, qa_normalizer)
    expected_qa = dict(qa)
    if qa_disk != expected_qa:
        pending_model = meta.get("translation_model")
        if isinstance(pending_model, str) and pending_model.strip():
            try:
                expected_qa.update(
                    canonical_pending_provenance(meta)
                )
            except ValueError as exc:
                raise PartialCommitReconciliationError(
                    "reviewed partial commit has invalid pinned translation model"
                ) from exc
        if qa_disk != expected_qa:
            raise PartialCommitReconciliationError(
                "committed QA evidence differs from the reviewed pending QA "
                "checkpoint and its canonical model provenance"
            )

    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise PartialCommitReconciliationError(
            "progress.completed must be a list"
        )
    if chapter_number in completed:
        raise PartialCommitReconciliationError(
            "reviewed partial commit target is already marked completed"
        )
    if progress.get("next_chapter") != chapter_number:
        raise PartialCommitReconciliationError(
            "reviewed partial commit target does not match progress.next_chapter"
        )

    return {
        "mode": PARTIAL_COMMIT_RECOVERY_MODE,
        "version": PARTIAL_COMMIT_EVIDENCE_VERSION,
        "final_exists": True,
        "final_sha256": _sha256_file(final_file),
        "qa_sha256": _sha256_file(qa_file),
    }
