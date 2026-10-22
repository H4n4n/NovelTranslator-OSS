from public_test_support import supervised_callback_source
from pathlib import Path

import pytest

from production_failure_classification import (
    LocalCallbackError,
    LocalPersistenceError,
)
from production_state_extraction_checkpoint import (
    run_state_extraction_checkpoint,
)
from production_saved_checkpoint_identity import (
    SavedCheckpointIdentityError,
    STATE_CHECKPOINT_IDENTITY_KEY,
)


IDENTITY = {
    "version": "test-state-v1",
    "identity_sha256": "a" * 64,
}


def test_recovered_state_is_reused_repriced_and_not_persisted():
    state = {"new_terms": [], "continuity_summary": "saved", "issues": []}
    saved_stats = {"prompt_tokens": 12, "cost_usd": 0.01}
    repriced = {"prompt_tokens": 12, "cost_usd": 0.02}
    meta = {
        "chapter": 7,
        "stage": "state_extracted",
        "state": state,
        "state_stats": saved_stats,
        STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY,
    }
    calls = []
    persisted = []

    def zero():
        calls.append("zero")
        return {"cost_usd": 0.0}

    result = run_state_extraction_checkpoint(
        meta=meta,
        checkpoint_identity=IDENTITY,
        extract_state=lambda: pytest.fail("saved state must not be regenerated"),
        reprice_saved_stats=lambda value: (
            calls.append(("reprice", value)) or repriced
        ),
        zero_stats=zero,
        persist_meta=lambda value: persisted.append(value),
        recoverable_exceptions=(RuntimeError,),
    )

    assert result["status"] == "recovered"
    assert result["state"] is state
    assert result["state_stats"] is repriced
    assert result["meta"] is meta
    assert result["error"] is None
    # Preserve eager evaluation of the legacy meta.get default argument.
    assert calls == ["zero", ("reprice", saved_stats)]
    assert persisted == []


def test_reviewed_stage_with_saved_state_is_also_reused():
    state = {"new_terms": [{"chinese": "甲"}]}
    meta = {
        "stage": "reviewed",
        "state": state,
        "state_stats": {"cost_usd": 0.1},
        "review_result": {"decisions": []},
        STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY,
    }

    result = run_state_extraction_checkpoint(
        meta=meta,
        checkpoint_identity=IDENTITY,
        extract_state=lambda: pytest.fail("reviewed saved state must be reused"),
        reprice_saved_stats=lambda value: {"repriced": value},
        zero_stats=lambda: {},
        persist_meta=lambda _value: pytest.fail("recovery must not persist"),
    )

    assert result["status"] == "recovered"
    assert result["state"] is state
    assert result["state_stats"] == {"repriced": {"cost_usd": 0.1}}
    assert meta["stage"] == "reviewed"
    assert meta["review_result"] == {"decisions": []}


def test_missing_saved_stats_uses_zero_stats_before_repricing():
    state = {"new_terms": []}
    meta = {
        "stage": "state_extracted",
        "state": state,
        STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY,
    }
    calls = []

    def zero():
        calls.append("zero")
        return {"cost_usd": 0.0}

    def reprice(value):
        calls.append(("reprice", value))
        return {"cost_usd": 0.0, "repriced": True}

    result = run_state_extraction_checkpoint(
        meta=meta,
        checkpoint_identity=IDENTITY,
        extract_state=lambda: pytest.fail("saved state must not be regenerated"),
        reprice_saved_stats=reprice,
        zero_stats=zero,
        persist_meta=lambda _value: pytest.fail("recovery must not persist"),
    )

    assert calls == ["zero", ("reprice", {"cost_usd": 0.0})]
    assert result["state_stats"] == {"cost_usd": 0.0, "repriced": True}


def test_saved_state_without_identity_fails_closed_before_extraction():
    state = {"new_terms": []}
    meta = {
        "stage": "state_extracted",
        "state": state,
        "state_stats": {"cost_usd": 0.1},
    }

    with pytest.raises(
        SavedCheckpointIdentityError,
        match="lacks a durable checkpoint identity",
    ):
        run_state_extraction_checkpoint(
            meta=meta,
            checkpoint_identity=IDENTITY,
            extract_state=lambda: pytest.fail("must not extract"),
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            persist_meta=lambda _value: pytest.fail("must not persist"),
        )


def test_saved_state_identity_mismatch_fails_closed_before_extraction():
    state = {"new_terms": []}
    meta = {
        "stage": "state_extracted",
        "state": state,
        "state_stats": {"cost_usd": 0.1},
        STATE_CHECKPOINT_IDENTITY_KEY: {
            "version": "test-state-v1",
            "identity_sha256": "b" * 64,
        },
    }

    with pytest.raises(
        SavedCheckpointIdentityError,
        match="does not match current source/text/policy input",
    ):
        run_state_extraction_checkpoint(
            meta=meta,
            checkpoint_identity=IDENTITY,
            extract_state=lambda: pytest.fail("must not extract"),
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            persist_meta=lambda _value: pytest.fail("must not persist"),
        )


def test_new_state_binds_request_before_extraction_and_checkpoints_result():
    state = {
        "new_terms": [{"chinese": "甲", "english": "A"}],
        "continuity_summary": "new",
        "issues": [],
    }
    stats = {"prompt_tokens": 20, "cost_usd": 0.03}
    meta = {
        "chapter": 7,
        "stage": "translated",
        "translation_stats": {"cost_usd": 1.0},
    }
    events = []
    persisted = []

    def extract():
        events.append("extract")
        return state, stats

    def persist(value):
        events.append("persist")
        persisted.append(value)

    result = run_state_extraction_checkpoint(
        meta=meta,
        checkpoint_identity=IDENTITY,
        extract_state=extract,
        reprice_saved_stats=lambda _value: pytest.fail(
            "new state must not be repriced"
        ),
        zero_stats=lambda: pytest.fail("new state does not need zero stats"),
        persist_meta=persist,
        recoverable_exceptions=(RuntimeError,),
    )

    assert events == ["persist", "extract", "persist"]
    assert result["status"] == "extracted"
    assert result["state"] is state
    assert result["state_stats"] is stats
    assert result["meta"] is meta
    assert result["error"] is None
    assert meta["stage"] == "state_extracted"
    assert meta["state"] is state
    assert meta["state_stats"] is stats
    assert meta[STATE_CHECKPOINT_IDENTITY_KEY] == IDENTITY
    assert len(persisted) == 2
    assert persisted[0]["stage"] == "translated"
    assert persisted[0][STATE_CHECKPOINT_IDENTITY_KEY] == IDENTITY
    assert "state" not in persisted[0]
    assert persisted[1] is meta


def test_recoverable_failure_preserves_bound_request_for_replay():
    meta = {"chapter": 7, "stage": "translated"}
    before = dict(meta)
    persisted = []
    error = RuntimeError("synthetic state extraction failure")

    def extract():
        raise error

    result = run_state_extraction_checkpoint(
        meta=meta,
        checkpoint_identity=IDENTITY,
        extract_state=extract,
        reprice_saved_stats=lambda value: value,
        zero_stats=dict,
        persist_meta=lambda value: persisted.append(value),
        recoverable_exceptions=(RuntimeError,),
    )

    assert result["status"] == "failed"
    assert result["error"] is error
    assert result["state"] is None
    assert result["state_stats"] is None
    assert result["meta"] is meta
    assert meta == {**before, STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY}
    assert persisted == [meta]


def test_unlisted_extraction_exception_propagates_unchanged():
    meta = {"stage": "translated"}

    def extract():
        raise ValueError("unexpected extraction bug")

    with pytest.raises(ValueError, match="unexpected extraction bug"):
        run_state_extraction_checkpoint(
            meta=meta,
            checkpoint_identity=IDENTITY,
            extract_state=extract,
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            persist_meta=lambda _value: None,
            recoverable_exceptions=(RuntimeError,),
        )

    assert meta == {"stage": "translated", STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY}


def test_persistence_runtime_error_is_not_classified_as_extraction_failure():
    state = {"new_terms": []}
    stats = {"cost_usd": 0.01}
    meta = {"stage": "translated"}

    def fail_persist(_value):
        if _value.get("stage") == "state_extracted":
            raise RuntimeError("synthetic state checkpoint write failure")

    with pytest.raises(
        LocalPersistenceError,
        match="state-extraction pending checkpoint persistence failed",
    ) as caught:
        run_state_extraction_checkpoint(
            meta=meta,
            checkpoint_identity=IDENTITY,
            extract_state=lambda: (state, stats),
            reprice_saved_stats=lambda value: value,
            zero_stats=dict,
            persist_meta=fail_persist,
            recoverable_exceptions=(RuntimeError,),
        )

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "synthetic state checkpoint write failure" in str(caught.value.__cause__)
    assert meta["stage"] == "state_extracted"
    assert meta["state"] is state
    assert meta["state_stats"] is stats


def test_repricing_failure_propagates_without_extraction_or_persistence():
    state = {"new_terms": []}
    meta = {
        "stage": "state_extracted",
        "state": state,
        "state_stats": {"cost_usd": 0.1},
        STATE_CHECKPOINT_IDENTITY_KEY: IDENTITY,
    }

    def fail_reprice(_value):
        raise RuntimeError("synthetic repricing failure")

    with pytest.raises(
        LocalCallbackError,
        match="saved state accounting repricing callback failed",
    ) as caught:
        run_state_extraction_checkpoint(
            meta=meta,
            checkpoint_identity=IDENTITY,
            extract_state=lambda: pytest.fail("must not extract"),
            reprice_saved_stats=fail_reprice,
            zero_stats=dict,
            persist_meta=lambda _value: pytest.fail("must not persist"),
            recoverable_exceptions=(RuntimeError,),
        )

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "synthetic repricing failure" in str(caught.value.__cause__)


def test_reviewed_stage_without_state_blocks_without_regeneration():
    meta = {"stage": "reviewed", "review_result": {"decisions": []}}
    with pytest.raises(SavedCheckpointIdentityError, match="incomplete"):
        run_state_extraction_checkpoint(
            meta=meta, checkpoint_identity=IDENTITY,
            extract_state=lambda: pytest.fail("must not regenerate"),
            reprice_saved_stats=lambda value: value, zero_stats=dict,
            persist_meta=lambda value: pytest.fail("must not persist"))


def test_legacy_process_orders_qa_state_glossary_and_commit():
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        repo_root
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    source = supervised_callback_source(source, "process_chapter", {
        'qa_checkpoint': ('commit_qa', 'run_commit_qa_checkpoint'),
        'state_checkpoint': ('state', 'run_state_extraction_checkpoint'),
        'candidate_checkpoint': ('candidates', 'run_candidate_preparation_checkpoint'),
        'review_checkpoint': ('glossary_review', 'run_glossary_review_checkpoint'),
        'glossary_changes': ('reviewed_state', 'finalize_reviewed_chapter_state'),
        'chapter_stats': ('chapter_commit', 'commit_reviewed_chapter'),
    })
    process = source[source.index("def process_chapter("):]

    qa_checkpoint = process.index(
        "qa_checkpoint = run_commit_qa_checkpoint("
    )
    state_checkpoint = process.index(
        "state_checkpoint = run_state_extraction_checkpoint(",
        qa_checkpoint,
    )
    candidate_preparation = process.index(
        "candidate_checkpoint = run_candidate_preparation_checkpoint(",
        state_checkpoint,
    )
    glossary_review = process.index(
        "review_checkpoint = run_glossary_review_checkpoint(",
        candidate_preparation,
    )
    reviewed_state = process.index(
        "glossary_changes = finalize_reviewed_chapter_state(",
        glossary_review,
    )
    durable_commit = process.index(
        "chapter_stats = commit_reviewed_chapter(",
        reviewed_state,
    )

    assert (
        qa_checkpoint
        < state_checkpoint
        < candidate_preparation
        < glossary_review
        < reviewed_state
        < durable_commit
    )

    review_block = process[glossary_review:reviewed_state]
    assert "checkpoint_identity=review_checkpoint_identity" in review_block
    assert "validate_saved_review=lambda saved:" in review_block
    assert "reprice_saved_stats=lambda saved:" in review_block
    assert "run_review=lambda recovery_record, persist_recovery:" in review_block
    assert "persist_checkpoint_meta=lambda value, *, stage:" in review_block
    assert "persist_recovery_meta=lambda value:" in review_block
    assert "APIError," in review_block
    assert "RateLimitError," in review_block
    assert "RuntimeError," not in review_block

    state_block = process[state_checkpoint:candidate_preparation]
    assert "extract_state=lambda: extract_state(" in state_block
    assert "reprice_saved_stats=reprice_saved_stats_if_needed" in state_block
    assert "zero_stats=zero_stats" in state_block
    assert "persist_meta=lambda value: save_json(" in state_block
    assert "checkpoint_identity=state_checkpoint_identity" in state_block
    assert "APIError," in state_block
    assert "ProviderResponseError," in state_block
    assert "RuntimeError," not in state_block
    assert 'if state_checkpoint["status"] == "failed":' in state_block
    assert 'if state_checkpoint["status"] == "recovered":' in state_block
