"""Durable glossary-review checkpoint sequencing for one production chapter.

This module owns only restart/recovery mechanics around chapter glossary review:
- exact saved-review checkpoint identity verification;
- saved-review replay and accounting recovery;
- durable candidate-preparation metadata before a new review;
- durable review-recovery record updates supplied by the review protocol;
- final reviewed-stage pending metadata persistence.

Glossary candidate policy, prompts, provider calls, validation policy, pricing,
logging, and operator-facing error reporting remain caller-owned callbacks.
All callback/provider/domain exceptions propagate unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
from production_saved_checkpoint_identity import (
    REVIEW_CHECKPOINT_IDENTITY_KEY,
    require_saved_checkpoint_identity,
)


def _restore_saved_candidate_ids(
    review_result: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Backfill candidate IDs for legacy saved decisions using exact source keys."""

    restored = copy.deepcopy(dict(review_result))
    candidates_by_source = {
        str(candidate.get("chinese", "") or "").strip(): candidate
        for candidate in candidates
        if str(candidate.get("chinese", "") or "").strip()
    }
    for decision in restored.get("decisions", []):
        if not isinstance(decision, dict) or decision.get("candidate_id"):
            continue
        source_key = str(decision.get("chinese", "") or "").strip()
        candidate = candidates_by_source.get(source_key)
        if candidate is not None and candidate.get("candidate_id"):
            decision["candidate_id"] = candidate["candidate_id"]
    return restored


def run_glossary_review_checkpoint(
    *,
    meta: dict[str, Any],
    eligible_candidates: Sequence[Mapping[str, Any]],
    candidate_preparation: Mapping[str, Any],
    prefilter_rejected: Sequence[Mapping[str, Any]],
    checkpoint_identity: Mapping[str, Any],
    validate_saved_review: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    reprice_saved_stats: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    zero_stats: Callable[[], Mapping[str, Any]],
    run_review: Callable[..., tuple[Mapping[str, Any], Mapping[str, Any]]],
    persist_checkpoint_meta: Callable[..., None],
    persist_recovery_meta: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Recover or execute one glossary-review checkpoint without owning policy."""

    if meta.get("stage") == "reviewed" and "review_result" in meta:
        require_saved_checkpoint_identity(
            saved=meta.get(REVIEW_CHECKPOINT_IDENTITY_KEY),
            expected=checkpoint_identity,
            label="saved chapter glossary review",
        )
        saved_review_result = _restore_saved_candidate_ids(
            meta["review_result"],
            eligible_candidates,
        )
        review_result = validate_saved_review(saved_review_result)
        review_stats = reprice_saved_stats(
            meta.get(
                "review_stats",
                zero_stats(),
            )
        )
        return {
            "status": "recovered",
            "review_result": review_result,
            "review_stats": review_stats,
            "meta": meta,
        }

    meta["candidate_preparation"] = copy.deepcopy(
        dict(candidate_preparation)
    )
    persist_checkpoint_meta(
        meta,
        stage="glossary_review_preparation",
    )

    def persist_recovery(record: Mapping[str, Any]) -> None:
        meta[CHAPTER_REVIEW_RECOVERY_KEY] = copy.deepcopy(dict(record))
        persist_recovery_meta(meta)

    review_result, review_stats = run_review(
        recovery_record=meta.get(CHAPTER_REVIEW_RECOVERY_KEY),
        persist_recovery=persist_recovery,
    )

    meta["stage"] = "reviewed"
    meta["eligible_candidates"] = eligible_candidates
    meta["candidate_preparation"] = copy.deepcopy(
        dict(candidate_preparation)
    )
    meta["prefilter_rejected"] = prefilter_rejected
    meta["review_result"] = review_result
    meta["review_stats"] = review_stats
    meta[REVIEW_CHECKPOINT_IDENTITY_KEY] = copy.deepcopy(
        dict(checkpoint_identity)
    )
    persist_checkpoint_meta(
        meta,
        stage="glossary_review_result",
    )

    return {
        "status": "reviewed",
        "review_result": review_result,
        "review_stats": review_stats,
        "meta": meta,
    }
