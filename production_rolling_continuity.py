"""Exact rolling-memory inputs and read-only transition proofs; no provider IO."""
from copy import deepcopy
import json
from collections.abc import Mapping

from production_saved_checkpoint_identity import (
    STATE_CHECKPOINT_IDENTITY_VERSION,
    SavedCheckpointIdentityError,
    continuity_baseline,
)


def saved_continuity_baseline(identity):
    """Old checkpoints lack reconstructable rolling inputs and must block."""
    if not isinstance(identity, Mapping) or identity.get("version") != STATE_CHECKPOINT_IDENTITY_VERSION:
        raise SavedCheckpointIdentityError("saved state lacks a rolling continuity identity")
    return continuity_baseline(identity.get("prior_continuity"))


def require_continuity_transition(*, current, prior, chapter, summary):
    """Accept only the exact pre-write or post-write continuity object."""
    prior = continuity_baseline(prior)
    expected = deepcopy(prior)
    expected.update(latest_chapter=chapter, summary=summary)
    if current != prior and current != expected:
        raise SavedCheckpointIdentityError(
            "current continuity does not match the reviewed pending state or its exact prior baseline"
        )
    return expected


def state_extraction_user_prompt(*, chapter, existing_terms, prior, source, translation):
    """The immutable baseline is an explicit input to the existing operation."""
    prior = continuity_baseline(prior)
    return f"""
CHAPTER: {chapter}
EXISTING GLOSSARY TERMS:
{json.dumps(list(existing_terms), ensure_ascii=False)}
PRIOR COMMITTED CONTINUITY (JSON):
{json.dumps(prior, ensure_ascii=False, sort_keys=True)}
CHINESE SOURCE:
{source}
FINAL ENGLISH:
{translation}
""".strip()
