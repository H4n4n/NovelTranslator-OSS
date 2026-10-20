"""Recovery-safe state-extraction checkpoint for one pending chapter.

This module owns only orchestration around the already-existing state extraction
policy callback. It does not build prompts, call providers directly, interpret
state content, choose glossary policy, or own human-facing error reporting.

The established behavior is preserved:

1. reuse saved state for pending stages state_extracted and reviewed when a
   saved state value is present, verifying it through the optional caller validator;
2. reprice only recovered saved state accounting through the caller callback;
3. otherwise invoke the caller's existing extract_state callback;
4. classify only caller-supplied recoverable extraction exceptions as a
   controlled extraction failure;
5. persist exact request identity before extraction, then save the successful
   state_extracted result; refuse incompatible or incomplete saved checkpoints;
6. persistence failures occur outside the controlled extraction exception
   boundary and therefore propagate unchanged.

Callers provide pricing, zero-accounting, persistence, identity, and exception
policy so this helper remains provider- and filesystem-implementation agnostic.
"""

import copy

from production_failure_classification import (
    LocalCallbackError,
    LocalPersistenceError,
)
from production_saved_checkpoint_identity import (
    STATE_CHECKPOINT_IDENTITY_KEY,
    SavedCheckpointIdentityError,
    require_saved_checkpoint_identity,
)


def run_state_extraction_checkpoint(
    *,
    meta,
    extract_state,
    reprice_saved_stats,
    zero_stats,
    persist_meta,
    checkpoint_identity,
    recoverable_exceptions=(),
    validate_saved_state=None,
):
    """Reuse or produce the state checkpoint without changing policy ownership."""

    if meta.get("stage") in {"state_extracted", "reviewed"} and "state" in meta:
        require_saved_checkpoint_identity(
            saved=meta.get(STATE_CHECKPOINT_IDENTITY_KEY),
            expected=checkpoint_identity,
            label="saved state extraction",
        )
        state = meta["state"]
        if validate_saved_state is not None:
            validate_saved_state(state)
        try:
            state_stats = reprice_saved_stats(
                meta.get(
                    "state_stats",
                    zero_stats(),
                )
            )
        except Exception as exc:
            raise LocalCallbackError(
                "saved state accounting repricing callback failed"
            ) from exc
        return {
            "status": "recovered",
            "state": state,
            "state_stats": state_stats,
            "meta": meta,
            "error": None,
        }

    if meta.get("stage") in {"state_extracted", "reviewed"}:
        raise SavedCheckpointIdentityError("saved state checkpoint is incomplete; automatic extraction is blocked")

    # Bind recoverable request inputs before transport. A crash after the paid
    # journal records its response can reconstruct the same request locally.
    if STATE_CHECKPOINT_IDENTITY_KEY in meta:
        require_saved_checkpoint_identity(
            saved=meta[STATE_CHECKPOINT_IDENTITY_KEY],
            expected=checkpoint_identity,
            label="planned state extraction",
        )
    else:
        bound_meta = copy.deepcopy(meta)
        bound_meta[STATE_CHECKPOINT_IDENTITY_KEY] = copy.deepcopy(dict(checkpoint_identity))
        try:
            persist_meta(bound_meta)
        except Exception as exc:
            raise LocalPersistenceError(
                "state-extraction request checkpoint persistence failed"
            ) from exc
        # Publish only after persistence succeeds. A caller retrying the same
        # in-memory metadata after a write failure must not skip the snapshot.
        meta[STATE_CHECKPOINT_IDENTITY_KEY] = copy.deepcopy(dict(checkpoint_identity))

    try:
        state, state_stats = extract_state()
    except tuple(recoverable_exceptions or ()) as exc:
        return {
            "status": "failed",
            "state": None,
            "state_stats": None,
            "meta": meta,
            "error": exc,
        }

    meta["stage"] = "state_extracted"
    meta["state"] = state
    meta["state_stats"] = state_stats
    meta[STATE_CHECKPOINT_IDENTITY_KEY] = copy.deepcopy(
        dict(checkpoint_identity)
    )
    try:
        persist_meta(meta)
    except Exception as exc:
        raise LocalPersistenceError(
            "state-extraction pending checkpoint persistence failed"
        ) from exc

    return {
        "status": "extracted",
        "state": state,
        "state_stats": state_stats,
        "meta": meta,
        "error": None,
    }
