import copy
from pathlib import Path

import pytest

from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
from production_glossary_review_checkpoint import (
    run_glossary_review_checkpoint,
)
from production_saved_checkpoint_identity import (
    REVIEW_CHECKPOINT_IDENTITY_KEY,
    SavedCheckpointIdentityError,
)


IDENTITY = {
    "version": "saved-review-checkpoint-v1",
    "identity_sha256": "a" * 64,
}


def _candidate():
    return {
        "candidate_id": "source:alpha",
        "chinese": "甲",
        "english": "Alpha",
        "category": "character",
        "notes": "",
    }


def _stats():
    return {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "cost_usd": 0.1,
    }


def test_saved_review_reuses_exact_identity_without_new_review_or_persistence():
    candidate = _candidate()
    meta = {
        "stage": "reviewed",
        "review_result": {
            "decisions": [
                {
                    "chinese": "甲",
                    "action": "accept",
                    "english": "Alpha",
                    "category": "character",
                    "notes": "",
                }
            ],
            "issues": [],
        },
        "review_stats": _stats(),
        REVIEW_CHECKPOINT_IDENTITY_KEY: copy.deepcopy(IDENTITY),
    }
    validation_seen = []
    repriced_seen = []

    outcome = run_glossary_review_checkpoint(
        meta=meta,
        eligible_candidates=[candidate],
        candidate_preparation={"raw_candidate_count": 1},
        prefilter_rejected=[],
        checkpoint_identity=IDENTITY,
        validate_saved_review=lambda value: (
            validation_seen.append(copy.deepcopy(value)) or value
        ),
        reprice_saved_stats=lambda value: (
            repriced_seen.append(copy.deepcopy(value)) or value
        ),
        zero_stats=lambda: {},
        run_review=lambda **_kwargs: pytest.fail("review must not run"),
        persist_checkpoint_meta=lambda *_args, **_kwargs: pytest.fail(
            "saved reuse must not persist"
        ),
        persist_recovery_meta=lambda _value: pytest.fail(
            "saved reuse must not persist recovery"
        ),
    )

    assert outcome["status"] == "recovered"
    assert outcome["meta"] is meta
    assert outcome["review_stats"] == _stats()
    assert validation_seen[0]["decisions"][0]["candidate_id"] == "source:alpha"
    assert repriced_seen == [_stats()]


def test_saved_review_missing_or_stale_identity_fails_before_reuse():
    candidate = _candidate()
    base = {
        "stage": "reviewed",
        "review_result": {"decisions": [], "issues": []},
        "review_stats": _stats(),
    }

    for identity in (
        None,
        {"version": "saved-review-checkpoint-v1", "identity_sha256": "b" * 64},
    ):
        meta = copy.deepcopy(base)
        if identity is not None:
            meta[REVIEW_CHECKPOINT_IDENTITY_KEY] = identity

        with pytest.raises(SavedCheckpointIdentityError):
            run_glossary_review_checkpoint(
                meta=meta,
                eligible_candidates=[candidate],
                candidate_preparation={},
                prefilter_rejected=[],
                checkpoint_identity=IDENTITY,
                validate_saved_review=lambda _value: pytest.fail(
                    "identity must fail first"
                ),
                reprice_saved_stats=lambda _value: pytest.fail(
                    "identity must fail first"
                ),
                zero_stats=dict,
                run_review=lambda **_kwargs: pytest.fail(
                    "identity must fail before provider/review work"
                ),
                persist_checkpoint_meta=lambda *_args, **_kwargs: pytest.fail(
                    "identity must fail before persistence"
                ),
                persist_recovery_meta=lambda _value: pytest.fail(
                    "identity must fail before recovery persistence"
                ),
            )


def test_new_review_persists_preparation_before_review_and_final_checkpoint_after():
    candidate = _candidate()
    meta = {
        "stage": "state_extracted",
        "state": {"new_terms": []},
    }
    events = []

    def persist_checkpoint(value, *, stage):
        events.append(
            (
                "checkpoint",
                stage,
                copy.deepcopy(value),
            )
        )

    def persist_recovery(value):
        events.append(("recovery", copy.deepcopy(value)))

    def run_review(*, recovery_record, persist_recovery):
        events.append(("review_entered", copy.deepcopy(recovery_record)))
        assert events[0][0:2] == (
            "checkpoint",
            "glossary_review_preparation",
        )
        persist_recovery(
            {
                "status": "original_started",
                "operation_ids": {"original": "op-1"},
            }
        )
        return (
            {
                "decisions": [
                    {
                        "candidate_id": "source:alpha",
                        "action": "accept",
                        "english": "Alpha",
                        "category": "character",
                        "notes": "",
                    }
                ],
                "issues": [],
            },
            _stats(),
        )

    outcome = run_glossary_review_checkpoint(
        meta=meta,
        eligible_candidates=[candidate],
        candidate_preparation={
            "raw_candidate_count": 1,
            "canonical_candidate_count": 1,
        },
        prefilter_rejected=[{"reason": "low value"}],
        checkpoint_identity=IDENTITY,
        validate_saved_review=lambda value: value,
        reprice_saved_stats=lambda value: value,
        zero_stats=dict,
        run_review=run_review,
        persist_checkpoint_meta=persist_checkpoint,
        persist_recovery_meta=persist_recovery,
    )

    assert outcome["status"] == "reviewed"
    assert outcome["meta"] is meta
    assert meta["stage"] == "reviewed"
    assert meta["eligible_candidates"] == [candidate]
    assert meta["prefilter_rejected"] == [{"reason": "low value"}]
    assert meta["review_result"] == outcome["review_result"]
    assert meta["review_stats"] == _stats()
    assert meta[REVIEW_CHECKPOINT_IDENTITY_KEY] == IDENTITY
    assert meta[CHAPTER_REVIEW_RECOVERY_KEY]["status"] == "original_started"

    assert events[0][0:2] == (
        "checkpoint",
        "glossary_review_preparation",
    )
    assert events[1] == ("review_entered", None)
    assert events[2][0] == "recovery"
    assert events[-1][0:2] == (
        "checkpoint",
        "glossary_review_result",
    )
    assert events[-1][2]["stage"] == "reviewed"


def test_preparation_persistence_failure_prevents_review():
    meta = {"stage": "state_extracted"}
    failure = RuntimeError("synthetic preparation persistence failure")

    def fail_persist(_value, *, stage):
        assert stage == "glossary_review_preparation"
        raise failure

    with pytest.raises(RuntimeError) as caught:
        run_glossary_review_checkpoint(
            meta=meta,
            eligible_candidates=[],
            candidate_preparation={},
            prefilter_rejected=[],
            checkpoint_identity=IDENTITY,
            validate_saved_review=lambda value: value,
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            run_review=lambda **_kwargs: pytest.fail("review must not run"),
            persist_checkpoint_meta=fail_persist,
            persist_recovery_meta=lambda _value: pytest.fail(
                "recovery must not persist"
            ),
        )

    assert caught.value is failure
    assert meta["stage"] == "state_extracted"


def test_review_exception_propagates_without_final_reviewed_checkpoint():
    meta = {"stage": "state_extracted"}
    failure = RuntimeError("synthetic review failure")
    persisted = []

    def run_review(**_kwargs):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        run_glossary_review_checkpoint(
            meta=meta,
            eligible_candidates=[],
            candidate_preparation={"raw_candidate_count": 0},
            prefilter_rejected=[],
            checkpoint_identity=IDENTITY,
            validate_saved_review=lambda value: value,
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            run_review=run_review,
            persist_checkpoint_meta=lambda value, *, stage: persisted.append(
                (stage, copy.deepcopy(value))
            ),
            persist_recovery_meta=lambda _value: None,
        )

    assert caught.value is failure
    assert meta["stage"] == "state_extracted"
    assert [item[0] for item in persisted] == [
        "glossary_review_preparation"
    ]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(5)])
def test_review_process_control_exceptions_propagate_unchanged(failure):
    with pytest.raises(type(failure)) as caught:
        run_glossary_review_checkpoint(
            meta={"stage": "state_extracted"},
            eligible_candidates=[],
            candidate_preparation={},
            prefilter_rejected=[],
            checkpoint_identity=IDENTITY,
            validate_saved_review=lambda value: value,
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            run_review=lambda **_kwargs: (_ for _ in ()).throw(failure),
            persist_checkpoint_meta=lambda _value, *, stage: None,
            persist_recovery_meta=lambda _value: None,
        )

    assert caught.value is failure


def test_final_persistence_failure_occurs_after_review_result_is_in_memory():
    meta = {"stage": "state_extracted"}
    failure = OSError("synthetic final checkpoint failure")
    stages = []

    def persist(value, *, stage):
        stages.append(stage)
        if stage == "glossary_review_result":
            raise failure

    with pytest.raises(OSError) as caught:
        run_glossary_review_checkpoint(
            meta=meta,
            eligible_candidates=[],
            candidate_preparation={},
            prefilter_rejected=[],
            checkpoint_identity=IDENTITY,
            validate_saved_review=lambda value: value,
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            run_review=lambda **_kwargs: (
                {"decisions": [], "issues": []},
                _stats(),
            ),
            persist_checkpoint_meta=persist,
            persist_recovery_meta=lambda _value: None,
        )

    assert caught.value is failure
    assert stages == [
        "glossary_review_preparation",
        "glossary_review_result",
    ]
    assert meta["stage"] == "reviewed"
    assert meta[REVIEW_CHECKPOINT_IDENTITY_KEY] == IDENTITY


def test_review_checkpoint_helper_owns_no_provider_or_glossary_policy():
    source = (
        Path(__file__).resolve().parents[1]
        / "production_glossary_review_checkpoint.py"
    ).read_text(encoding="utf-8")

    assert "from openai" not in source
    assert "import openai" not in source
    assert "review_new_terms" not in source
    assert "validate_glossary_review_result" not in source
    assert "reprice_saved_stats_if_needed" not in source
    assert "describe_api_error" not in source
    assert "print(" not in source
    assert "log_event" not in source
    assert "log_error" not in source
