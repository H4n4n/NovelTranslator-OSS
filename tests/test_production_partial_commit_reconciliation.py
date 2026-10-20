import json

import pytest

from committed_qa_provenance import committed_qa_model_record
from production_partial_commit_reconciliation import (
    NORMAL_RECOVERY_MODE,
    PARTIAL_COMMIT_RECOVERY_MODE,
    PartialCommitReconciliationError,
    inspect_reviewed_partial_commit_evidence,
)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fixture(tmp_path):
    pending = tmp_path / "output" / "pending" / "0001.md"
    meta_path = tmp_path / "output" / "pending" / "0001.json"
    final = tmp_path / "output" / "chapters" / "0001.md"
    qa = tmp_path / "output" / "qa" / "0001.json"
    for path in (pending, meta_path, final, qa):
        path.parent.mkdir(parents=True, exist_ok=True)

    pending.write_text("paid reviewed translation", encoding="utf-8")
    local_qa = {"commit_allowed": True, "issues": []}
    meta = {
        "chapter": 1,
        "stage": "reviewed",
        "translation_stats": {"prompt_tokens": 10},
        "local_qa": local_qa,
        "state": {"continuity_summary": "summary", "issues": []},
        "state_stats": {"prompt_tokens": 2},
        "state_checkpoint_identity": {
            "version": "fixture-state-v1",
            "identity_sha256": "a" * 64,
        },
        "eligible_candidates": [],
        "prefilter_rejected": [],
        "review_result": {"decisions": [], "issues": []},
        "review_stats": {"prompt_tokens": 0},
        "review_checkpoint_identity": {
            "version": "fixture-review-v1",
            "identity_sha256": "b" * 64,
        },
    }
    _write_json(meta_path, meta)
    _write_json(qa, local_qa)
    progress = {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
    }
    return pending, meta_path, final, qa, meta, progress


def _inspect(parts):
    pending, meta_path, final, qa, meta, progress = parts
    return inspect_reviewed_partial_commit_evidence(
        chapter_number=1,
        pending_file=pending,
        pending_meta_file=meta_path,
        final_file=final,
        qa_file=qa,
        progress=progress,
        meta=meta,
    )


def test_absent_final_remains_normal_pending_recovery(tmp_path):
    parts = _fixture(tmp_path)
    result = _inspect(parts)
    assert result == {
        "mode": NORMAL_RECOVERY_MODE,
        "version": "reviewed-partial-commit-v1",
        "final_exists": False,
        "final_sha256": "",
        "qa_sha256": "",
    }


def test_identical_final_with_reviewed_checkpoint_is_proven(tmp_path):
    parts = _fixture(tmp_path)
    pending, _meta_path, final, _qa, _meta, _progress = parts
    final.write_bytes(pending.read_bytes())

    result = _inspect(parts)

    assert result["mode"] == PARTIAL_COMMIT_RECOVERY_MODE
    assert result["final_exists"] is True
    assert len(result["final_sha256"]) == 64
    assert len(result["qa_sha256"]) == 64


def test_different_final_fails_closed(tmp_path):
    parts = _fixture(tmp_path)
    parts[2].write_text("different", encoding="utf-8")

    with pytest.raises(
        PartialCommitReconciliationError,
        match="does not exactly match",
    ):
        _inspect(parts)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda meta: meta.__setitem__("stage", "state_extracted"), "reviewed pending checkpoint"),
        (lambda meta: meta.__setitem__("local_qa", {"commit_allowed": False}), "passing local-QA"),
        (lambda meta: meta.pop("state"), "missing state"),
        (lambda meta: meta.pop("review_result"), "missing review_result"),
        (
            lambda meta: meta.pop("state_checkpoint_identity"),
            "missing state_checkpoint_identity",
        ),
        (
            lambda meta: meta.pop("review_checkpoint_identity"),
            "missing review_checkpoint_identity",
        ),
        (lambda meta: meta.pop("eligible_candidates"), "missing eligible_candidates"),
        (lambda meta: meta.pop("prefilter_rejected"), "missing prefilter_rejected"),
    ],
)
def test_incomplete_reviewed_evidence_fails_closed(tmp_path, mutation, message):
    parts = _fixture(tmp_path)
    pending, meta_path, final, _qa, meta, _progress = parts
    final.write_bytes(pending.read_bytes())
    mutation(meta)
    _write_json(meta_path, meta)

    with pytest.raises(PartialCommitReconciliationError, match=message):
        _inspect(parts)


def test_pinned_committed_qa_accepts_only_canonical_model_provenance(
    tmp_path,
):
    parts = _fixture(tmp_path)
    pending, meta_path, final, qa, meta, _progress = parts
    final.write_bytes(pending.read_bytes())
    meta["translation_model"] = "deepseek-v4-pro"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    qa_payload = dict(meta["local_qa"])
    qa_payload.update(committed_qa_model_record("deepseek-v4-pro"))
    _write_json(qa, qa_payload)

    result = _inspect(parts)
    assert result["mode"] == PARTIAL_COMMIT_RECOVERY_MODE

    qa_payload["translation_model_version"] = "tampered"
    _write_json(qa, qa_payload)
    with pytest.raises(
        PartialCommitReconciliationError,
        match="canonical model provenance",
    ):
        _inspect(parts)


def test_missing_or_mismatched_qa_evidence_fails_closed(tmp_path):
    parts = _fixture(tmp_path)
    pending, _meta_path, final, qa, _meta, _progress = parts
    final.write_bytes(pending.read_bytes())
    qa.unlink()

    with pytest.raises(
        PartialCommitReconciliationError,
        match="committed QA evidence does not exist",
    ):
        _inspect(parts)

    _write_json(qa, {"commit_allowed": True, "issues": ["different"]})
    with pytest.raises(
        PartialCommitReconciliationError,
        match="differs from",
    ):
        _inspect(parts)


def test_progress_binding_must_still_point_at_target(tmp_path):
    parts = _fixture(tmp_path)
    pending, _meta_path, final, _qa, _meta, progress = parts
    final.write_bytes(pending.read_bytes())
    progress["next_chapter"] = 2

    with pytest.raises(
        PartialCommitReconciliationError,
        match="progress.next_chapter",
    ):
        _inspect(parts)
