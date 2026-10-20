"""Deterministic identities for durable saved production checkpoints.

The helpers in this module are deliberately policy-agnostic. Callers supply the
exact semantic inputs used by their existing state-extraction and chapter-review
policies. The resulting records are persisted with pending metadata and compared
verbatim before a saved checkpoint may be reused.

This is a restart/recovery safeguard. It does not call providers or touch the
filesystem.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


STATE_CHECKPOINT_IDENTITY_VERSION = "saved-state-checkpoint-v2-rolling"
REVIEW_CHECKPOINT_IDENTITY_VERSION = "saved-review-checkpoint-v1"

STATE_CHECKPOINT_IDENTITY_KEY = "state_checkpoint_identity"
REVIEW_CHECKPOINT_IDENTITY_KEY = "review_checkpoint_identity"


class SavedCheckpointIdentityError(RuntimeError):
    """A durable saved checkpoint cannot be proven to match current inputs."""


def continuity_baseline(value):
    """Detach the complete committed object without trimming its summary."""
    if (not isinstance(value, Mapping)
            or type(value.get("latest_chapter")) is not int
            or value["latest_chapter"] < 0
            or not isinstance(value.get("summary"), str)):
        raise SavedCheckpointIdentityError("prior continuity baseline is missing or invalid")
    return copy.deepcopy(dict(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finalize_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(payload)
    record["identity_sha256"] = _sha256_json(record)
    return record


def build_state_checkpoint_identity(
    *,
    novel_id: str,
    chapter: int,
    source_text: str,
    translation: str,
    profile_id: str,
    model_id: str,
    state_prompt: str,
    existing_glossary_keys: Iterable[str],
    prior_continuity: Mapping[str, Any],
    max_tokens: int = 3000,
    thinking_type: str = "disabled",
    extraction_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a saved state extraction to the exact current semantic request."""

    keys = [str(key) for key in existing_glossary_keys]
    prior = continuity_baseline(prior_continuity)
    payload = {
        "version": STATE_CHECKPOINT_IDENTITY_VERSION,
        "novel_id": str(novel_id),
        "chapter": int(chapter),
        "profile_id": str(profile_id),
        "model_id": str(model_id),
        "source_sha256": _sha256_text(source_text),
        "translation_sha256": _sha256_text(translation),
        "state_prompt_sha256": _sha256_text(state_prompt),
        "existing_glossary_keys_sha256": _sha256_json(keys),
        "existing_glossary_key_count": len(keys),
        # Content, not just a hash: disk may already hold the new summary on restart.
        "prior_continuity": prior,
        "prior_continuity_sha256": _sha256_json(prior),
        "max_tokens": int(max_tokens),
        "thinking_type": str(thinking_type),
    }
    if extraction_policy is not None:
        payload["extraction_policy"] = dict(extraction_policy)
    return _finalize_identity(payload)


def build_review_checkpoint_identity(
    *,
    novel_id: str,
    chapter: int,
    source_text: str,
    translation: str,
    profile_id: str,
    model_id: str,
    review_prompt: str,
    review_schema: str,
    review_input_hash: str,
    relevant_glossary_text: str,
    max_original_tokens: int = 4000,
    max_correction_tokens: int = 2000,
    thinking_type: str = "disabled",
) -> dict[str, Any]:
    """Bind a saved chapter review to source, target, policy and glossary context."""

    payload = {
        "version": REVIEW_CHECKPOINT_IDENTITY_VERSION,
        "novel_id": str(novel_id),
        "chapter": int(chapter),
        "profile_id": str(profile_id),
        "model_id": str(model_id),
        "source_sha256": _sha256_text(source_text),
        "translation_sha256": _sha256_text(translation),
        "review_prompt_sha256": _sha256_text(review_prompt),
        "review_schema_sha256": _sha256_text(review_schema),
        "review_input_hash": str(review_input_hash),
        "relevant_glossary_text_sha256": _sha256_text(relevant_glossary_text),
        "max_original_tokens": int(max_original_tokens),
        "max_correction_tokens": int(max_correction_tokens),
        "thinking_type": str(thinking_type),
    }
    return _finalize_identity(payload)


def require_saved_checkpoint_identity(
    *,
    saved: Any,
    expected: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """Return the saved identity only when it exactly matches current inputs."""

    if not isinstance(saved, Mapping):
        raise SavedCheckpointIdentityError(
            f"{label} lacks a durable checkpoint identity; automatic reuse is blocked"
        )
    saved_record = dict(saved)
    expected_record = dict(expected)
    if saved_record != expected_record:
        raise SavedCheckpointIdentityError(
            f"{label} checkpoint identity does not match current source/text/policy input"
        )
    return saved_record
