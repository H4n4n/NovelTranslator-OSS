from pathlib import Path

import pytest

from noveltranslator_core import pending_meta_after_commit_qa_failure
from production_commit_qa_checkpoint import run_commit_qa_checkpoint


def _qa(*, allowed=True):
    return {
        "chapter": 7,
        "commit_allowed": allowed,
        "remaining_chinese_characters": 0 if allowed else 2,
        "residual_cjk_fragments": [] if allowed else ["心虚"],
        "locked_high_priority_term_violations": [],
        "issues": [] if allowed else ["residual source script"],
    }


def test_passing_checkpoint_preserves_meta_identity_and_persists_once():
    meta = {
        "chapter": 7,
        "stage": "reviewed",
        "translation_stats": {"prompt_tokens": 10},
        "state": {"new_terms": []},
    }
    qa = _qa(allowed=True)
    calls = []
    persisted = []

    def checker(chapter_number, source_text, translation, finish_reason):
        calls.append(
            (chapter_number, source_text, translation, finish_reason)
        )
        return qa

    result = run_commit_qa_checkpoint(
        chapter_number=7,
        source_text="源文",
        translation="English translation",
        finish_reason="stop",
        meta=meta,
        qa_checker=checker,
        reset_failed_meta=pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: persisted.append(value),
    )

    assert calls == [(7, "源文", "English translation", "stop")]
    assert result["commit_allowed"] is True
    assert result["qa"] is qa
    assert result["meta"] is meta
    assert meta["local_qa"] is qa
    assert meta["stage"] == "reviewed"
    assert meta["state"] == {"new_terms": []}
    assert persisted == [meta]
    assert persisted[0] is meta


def test_blocked_checkpoint_resets_only_downstream_pending_work():
    original = {
        "chapter": 7,
        "stage": "reviewed",
        "translation_stats": {"prompt_tokens": 10, "cost_usd": 0.01},
        "elapsed_seconds": 1.25,
        "finish_reason": "stop",
        "state": {"new_terms": []},
        "state_stats": {"prompt_tokens": 2},
        "eligible_candidates": [{"candidate_id": "source:a"}],
        "prefilter_rejected": [{"chinese": "低"}],
        "candidate_preparation": {"candidate_count": 1},
        "review_result": {"decisions": []},
        "review_stats": {"prompt_tokens": 1},
        "chapter_glossary_review_recovery": {"status": "completed"},
    }
    qa = _qa(allowed=False)
    persisted = []

    result = run_commit_qa_checkpoint(
        chapter_number=7,
        source_text="源文",
        translation="English 心虚",
        finish_reason="stop",
        meta=original,
        qa_checker=lambda *_args: qa,
        reset_failed_meta=pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: persisted.append(value),
    )

    assert result["commit_allowed"] is False
    assert result["qa"] is qa
    assert result["meta"] is not original

    # Legacy order mutates the current object with QA before creating the reset
    # replacement, while all other original downstream fields remain untouched.
    assert original["local_qa"] is qa
    assert original["stage"] == "reviewed"
    assert "state" in original

    reset = result["meta"]
    assert reset["chapter"] == 7
    assert reset["translation_stats"] == original["translation_stats"]
    assert reset["elapsed_seconds"] == 1.25
    assert reset["finish_reason"] == "stop"
    assert reset["local_qa"] is qa
    assert reset["stage"] == "translated"

    for key in (
        "state",
        "state_stats",
        "eligible_candidates",
        "prefilter_rejected",
        "candidate_preparation",
        "review_result",
        "review_stats",
        "chapter_glossary_review_recovery",
    ):
        assert key not in reset

    assert persisted == [reset]
    assert persisted[0] is reset


def test_qa_failure_does_not_mutate_or_persist_metadata():
    meta = {"chapter": 7, "stage": "translated"}
    before = dict(meta)
    persisted = []

    def checker(*_args):
        raise RuntimeError("synthetic QA failure")

    with pytest.raises(RuntimeError, match="synthetic QA failure"):
        run_commit_qa_checkpoint(
            chapter_number=7,
            source_text="源文",
            translation="English",
            finish_reason="stop",
            meta=meta,
            qa_checker=checker,
            reset_failed_meta=pending_meta_after_commit_qa_failure,
            persist_meta=lambda value: persisted.append(value),
        )

    assert meta == before
    assert persisted == []


def test_missing_commit_allowed_preserves_legacy_fail_closed_order():
    meta = {"chapter": 7, "stage": "translated"}
    qa = {"issues": ["malformed QA record"]}
    persisted = []

    with pytest.raises(KeyError, match="commit_allowed"):
        run_commit_qa_checkpoint(
            chapter_number=7,
            source_text="源文",
            translation="English",
            finish_reason="stop",
            meta=meta,
            qa_checker=lambda *_args: qa,
            reset_failed_meta=pending_meta_after_commit_qa_failure,
            persist_meta=lambda value: persisted.append(value),
        )

    # Existing process_chapter behavior attaches QA before reading the gate key,
    # but does not persist if that key lookup itself fails.
    assert meta["local_qa"] is qa
    assert persisted == []


def test_persistence_failure_propagates_after_checkpoint_mutation():
    meta = {"chapter": 7, "stage": "translated"}
    qa = _qa(allowed=True)

    def fail_persist(_value):
        raise OSError("synthetic checkpoint write failure")

    with pytest.raises(OSError, match="synthetic checkpoint write failure"):
        run_commit_qa_checkpoint(
            chapter_number=7,
            source_text="源文",
            translation="English",
            finish_reason="stop",
            meta=meta,
            qa_checker=lambda *_args: qa,
            reset_failed_meta=pending_meta_after_commit_qa_failure,
            persist_meta=fail_persist,
        )

    assert meta["local_qa"] is qa


def test_legacy_process_orders_repairs_checkpoint_state_and_commit():
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        repo_root
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    process = source[source.index("def process_chapter("):]

    strict_repair = process.index(
        "strict_micro_repair = maybe_repair_strict_terminology("
    )
    residual_repair = process.index(
        "micro_repair = maybe_repair_residual_cjk(",
        strict_repair,
    )
    checkpoint = process.index(
        "qa_checkpoint = run_commit_qa_checkpoint(",
        residual_repair,
    )
    state = process.index(
        "state_checkpoint = run_state_extraction_checkpoint(",
        checkpoint,
    )
    reviewed = process.index(
        "glossary_changes = finalize_reviewed_chapter_state(",
        state,
    )
    commit = process.index(
        "chapter_stats = commit_reviewed_chapter(",
        reviewed,
    )

    assert (
        strict_repair
        < residual_repair
        < checkpoint
        < state
        < reviewed
        < commit
    )

    checkpoint_block = process[checkpoint:state]
    assert "qa_checker=local_qa" in checkpoint_block
    assert (
        "reset_failed_meta=pending_meta_after_commit_qa_failure"
        in checkpoint_block
    )
    assert "persist_meta=lambda value: save_json(" in checkpoint_block
    assert 'if not qa_checkpoint["commit_allowed"]:' in checkpoint_block
