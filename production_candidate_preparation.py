"""Candidate-preparation protocol; source identity and selection remain policy callbacks.

Run preparation once, attempt the existing resolver only for a same-source
semantic conflict, then re-run authoritative preparation to verify resolution.
No direct provider calls, state writes, logging, or glossary mutation live here.
Only known domain failures become controlled outcomes; persistence/programming
errors and process-control exceptions propagate unchanged.
"""

from glossary_integrity import GlossaryCandidateIdentityError
from glossary_candidate_resolution import GlossaryCandidateResolutionError
from glossary_candidate_auto_resolution import GlossaryCandidateAutoResolutionError


def run_candidate_preparation_checkpoint(*, prepare_candidates, resolve_conflict):
    """Prepare candidates with at most one resolution and one verification pass."""
    try:
        prepared = prepare_candidates()
    except GlossaryCandidateResolutionError as exc:
        return {"status": "resolution_failed", "error": exc}
    except GlossaryCandidateIdentityError as exc:
        if exc.code != "same_source_semantic_conflict":
            return {"status": "identity_failed", "error": exc}
        try:
            automatic = resolve_conflict(exc)
        except (
            GlossaryCandidateAutoResolutionError,
            GlossaryCandidateResolutionError,
        ) as automatic_error:
            return {"status": "resolution_failed", "error": automatic_error}
        if automatic.get("status") == "disabled":
            return {"status": "identity_failed", "error": exc}
        if automatic.get("status") != "resolved":
            return {"status": "unresolved", "error": exc, "automatic": automatic}
        try:
            prepared = prepare_candidates()
        except GlossaryCandidateResolutionError as verification_error:
            return {"status": "resolution_failed", "error": verification_error}
        except GlossaryCandidateIdentityError as verification_error:
            return {"status": "identity_failed", "error": verification_error}

    eligible_candidates, prefilter_rejected, preparation = prepared
    return {
        "status": "prepared",
        "eligible_candidates": eligible_candidates,
        "prefilter_rejected": prefilter_rejected,
        "candidate_preparation": preparation,
    }
