"""Experiment-only QA provenance adapter for reviewed partial-commit recovery.

Normal production requires committed QA to equal the durable pending local-QA
checkpoint (plus its established production-model provenance).  The Luna Batch
experiment intentionally appends two additional provenance layers only when the
committed QA file is written:

* generic exact Luna Batch transport/model provenance; and
* ``full_pipeline_batch_acceptance`` identifying the exact collected result.

This adapter validates those additions byte-for-value in memory, removes only
those known fields from the comparison object, and delegates every other proof
to ``production_partial_commit_reconciliation``.  The QA file itself is never
rewritten and its original SHA-256 remains in the returned proof evidence.

Importing/installing this module performs no provider calls or filesystem
writes.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from full_pipeline_batch_acceptance import (
    ACCEPTANCE_FIELD,
    validate_batch_acceptance_evidence,
)
from full_pipeline_luna_runtime import luna_batch_pending_provenance
from production_partial_commit_reconciliation import (
    PARTIAL_COMMIT_RECOVERY_MODE,
    PartialCommitReconciliationError,
    inspect_reviewed_partial_commit_evidence,
)


ADAPTER_MODE = "full_pipeline_luna_partial_commit_qa_v2"
_STATE_ATTR = "_full_pipeline_luna_partial_commit_qa_state"


class FullPipelinePartialCommitV2Error(PartialCommitReconciliationError):
    """Experiment-specific partial-commit QA provenance is inconsistent."""


def build_experiment_qa_normalizer(
    evidence: Mapping[str, Any],
):
    """Return a strict in-memory normalizer for one exact Batch acceptance."""
    proven = validate_batch_acceptance_evidence(evidence)
    generic = luna_batch_pending_provenance()

    def normalize(qa_disk):
        if not isinstance(qa_disk, dict):
            raise FullPipelinePartialCommitV2Error(
                "committed QA must be an object before provenance normalization"
            )
        value = copy.deepcopy(qa_disk)
        observed_acceptance = value.pop(ACCEPTANCE_FIELD, None)
        try:
            observed_acceptance = validate_batch_acceptance_evidence(
                observed_acceptance
            )
        except Exception as exc:
            raise FullPipelinePartialCommitV2Error(
                "committed QA lacks valid exact Batch acceptance evidence"
            ) from exc
        if observed_acceptance != proven:
            raise FullPipelinePartialCommitV2Error(
                "committed QA carries different Batch acceptance evidence"
            )

        for field, expected in generic.items():
            if field not in value:
                raise FullPipelinePartialCommitV2Error(
                    f"committed QA lacks Luna Batch provenance field {field}"
                )
            if value[field] != expected:
                raise FullPipelinePartialCommitV2Error(
                    f"committed QA Luna Batch provenance mismatch for {field}"
                )
            value.pop(field)
        return value

    return normalize


def inspect_experiment_partial_commit_evidence(
    *,
    evidence: Mapping[str, Any],
    **kwargs,
):
    """Run production's read-only proof with exact experiment QA normalization."""
    if "qa_normalizer" in kwargs:
        raise FullPipelinePartialCommitV2Error(
            "experiment partial proof owns qa_normalizer"
        )
    return inspect_reviewed_partial_commit_evidence(
        **kwargs,
        qa_normalizer=build_experiment_qa_normalizer(evidence),
    )


def install_experiment_partial_commit_qa_adapter(
    production_module,
    evidence: Mapping[str, Any],
):
    """Patch only the loaded experiment module's imported inspector callback."""
    proven = validate_batch_acceptance_evidence(evidence)
    current = getattr(
        production_module,
        "inspect_reviewed_partial_commit_evidence",
        None,
    )
    if not callable(current):
        raise FullPipelinePartialCommitV2Error(
            "production module has no partial-commit evidence inspector"
        )
    existing = getattr(current, _STATE_ATTR, None)
    if isinstance(existing, dict):
        if existing.get("acceptance_sha256") != proven["acceptance_sha256"]:
            raise FullPipelinePartialCommitV2Error(
                "partial-commit QA adapter is installed for different Batch evidence"
            )
        return existing

    original = current
    normalizer = build_experiment_qa_normalizer(proven)

    def wrapped_inspector(*args, **kwargs):
        if "qa_normalizer" in kwargs:
            raise FullPipelinePartialCommitV2Error(
                "loaded experiment engine may not replace the QA normalizer"
            )
        return original(*args, **kwargs, qa_normalizer=normalizer)

    state = {
        "mode": ADAPTER_MODE,
        "acceptance_sha256": proven["acceptance_sha256"],
        "original_inspector": original,
        "wrapped_inspector": wrapped_inspector,
    }
    setattr(wrapped_inspector, _STATE_ATTR, state)
    production_module.inspect_reviewed_partial_commit_evidence = wrapped_inspector
    return state


def prove_experiment_reviewed_partial_commit(
    project_root,
    chapter: int,
    evidence: Mapping[str, Any],
):
    """Use the same exact proof directly from an external experiment root."""
    from pathlib import Path
    import json

    root = Path(project_root).resolve()
    pending_file = root / "output" / "pending" / f"{chapter:04d}.md"
    pending_meta_file = root / "output" / "pending" / f"{chapter:04d}.json"
    final_file = root / "output" / "chapters" / f"{chapter:04d}.md"
    qa_file = root / "output" / "qa" / f"{chapter:04d}.json"
    try:
        progress = json.loads(
            (root / "data" / "progress.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise FullPipelinePartialCommitV2Error(
            "cannot read experiment progress for partial-commit proof"
        ) from exc
    try:
        result = inspect_experiment_partial_commit_evidence(
            evidence=evidence,
            chapter_number=chapter,
            pending_file=pending_file,
            pending_meta_file=pending_meta_file,
            final_file=final_file,
            qa_file=qa_file,
            progress=progress,
        )
    except FullPipelinePartialCommitV2Error:
        raise
    except PartialCommitReconciliationError as exc:
        raise FullPipelinePartialCommitV2Error(
            f"experiment reviewed partial-commit proof failed: {exc}"
        ) from exc
    if result.get("mode") != PARTIAL_COMMIT_RECOVERY_MODE:
        raise FullPipelinePartialCommitV2Error(
            "experiment state is not a proven reviewed partial commit"
        )
    return result
