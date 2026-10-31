from public_test_support import supervised_callback_source
from pathlib import Path

import pytest

from production_candidate_preparation import (
    GlossaryCandidateAutoResolutionError,
    GlossaryCandidateIdentityError,
    GlossaryCandidateResolutionError,
    run_candidate_preparation_checkpoint,
)


def identity(code="same_source_semantic_conflict"):
    return GlossaryCandidateIdentityError("conflict", code=code, diagnostics={})


def raise_error(error):
    raise error


def test_prepared_values_keep_identity_without_running_resolver():
    values = ([{"candidate_id": "exact"}], [], {"conflict_count": 0})
    result = run_candidate_preparation_checkpoint(
        prepare_candidates=lambda: values,
        resolve_conflict=lambda _: pytest.fail("no conflict to resolve"),
    )
    assert result["status"] == "prepared"
    for key, value in zip(
        ("eligible_candidates", "prefilter_rejected", "candidate_preparation"), values
    ):
        assert result[key] is value


@pytest.mark.parametrize("error,status", [
    (identity("ambiguous_anchor"), "identity_failed"),
    (GlossaryCandidateResolutionError("stale", code="stale"), "resolution_failed"),
])
def test_initial_hard_stop_never_attempts_resolution(error, status):
    result = run_candidate_preparation_checkpoint(
        prepare_candidates=lambda: raise_error(error),
        resolve_conflict=lambda _: pytest.fail("must stop before resolution"),
    )
    assert result == {"status": status, "error": error}


@pytest.mark.parametrize("automatic_status,expected", [
    ("disabled", "identity_failed"), ("abstained", "unresolved"),
    ("unknown", "unresolved"), (None, "unresolved"),
])
def test_nonresolved_outcome_preserves_original_evidence_and_never_reprepares(
    automatic_status, expected
):
    conflict = identity()
    calls = []
    automatic = {"status": automatic_status}
    def prepare():
        calls.append("prepare")
        raise conflict
    def resolve(error):
        assert error is conflict
        calls.append("resolve")
        return automatic
    result = run_candidate_preparation_checkpoint(
        prepare_candidates=prepare, resolve_conflict=resolve
    )
    assert calls == ["prepare", "resolve"]
    assert result["status"] == expected
    assert result["error"] is conflict
    if expected == "unresolved":
        assert result["automatic"] is automatic


@pytest.mark.parametrize("error", [
    GlossaryCandidateAutoResolutionError("bad record", code="bad"),
    GlossaryCandidateResolutionError("stale sidecar", code="stale"),
])
def test_resolver_domain_errors_are_controlled_without_second_preparation(error):
    calls = []
    def prepare():
        calls.append("prepare")
        raise identity()
    result = run_candidate_preparation_checkpoint(
        prepare_candidates=prepare,
        resolve_conflict=lambda _: raise_error(error),
    )
    assert calls == ["prepare"]
    assert result == {"status": "resolution_failed", "error": error}


@pytest.mark.parametrize("verification_error,status", [
    (None, "prepared"), (identity(), "identity_failed"),
    (GlossaryCandidateResolutionError("stale", code="stale"), "resolution_failed"),
])
def test_resolved_result_requires_authoritative_verification_once(verification_error, status):
    calls = []
    def prepare():
        calls.append("prepare")
        if len(calls) == 1:
            raise identity()
        if verification_error:
            raise verification_error
        return [], [], {"verified": True}
    def resolve(_):
        calls.append("resolve")
        return {"status": "resolved"}
    result = run_candidate_preparation_checkpoint(
        prepare_candidates=prepare, resolve_conflict=resolve
    )
    assert calls == ["prepare", "resolve", "prepare"]
    assert result["status"] == status
    if verification_error:
        assert result["error"] is verification_error


@pytest.mark.parametrize("stage", ["prepare", "resolve", "verify"])
@pytest.mark.parametrize("error", [OSError("disk full"), RuntimeError("bug"),
                                  KeyboardInterrupt(), SystemExit(7)])
def test_unrelated_failures_escape_unchanged_without_retry(stage, error):
    calls = []
    def prepare():
        calls.append("prepare")
        if stage == "prepare" or len(calls) > 1:
            raise error
        raise identity()
    def resolve(_):
        calls.append("resolve")
        if stage == "resolve":
            raise error
        return {"status": "resolved"}
    with pytest.raises(type(error)) as caught:
        run_candidate_preparation_checkpoint(
            prepare_candidates=prepare, resolve_conflict=resolve
        )
    assert caught.value is error
    assert calls == {"prepare": ["prepare"], "resolve": ["prepare", "resolve"],
                     "verify": ["prepare", "resolve", "prepare"]}[stage]
