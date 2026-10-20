import json

import pytest

from full_pipeline_batch_acceptance import (
    ACCEPTANCE_FIELD,
    build_batch_acceptance_evidence,
)
from full_pipeline_batch_wave import (
    BATCH_CACHE_HIT_RATE,
    BATCH_CACHE_MISS_RATE,
    BATCH_MODEL,
    BATCH_OUTPUT_RATE,
    BATCH_PRICING_VERSION,
)
from full_pipeline_luna_runtime import luna_batch_pending_provenance
from full_pipeline_partial_commit_v2 import (
    FullPipelinePartialCommitV2Error,
    inspect_experiment_partial_commit_evidence,
)
from production_partial_commit_reconciliation import PARTIAL_COMMIT_RECOVERY_MODE


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stats():
    prompt = 1000
    cached = 100
    completion = 300
    cost = (
        cached / 1_000_000 * BATCH_CACHE_HIT_RATE
        + (prompt - cached) / 1_000_000 * BATCH_CACHE_MISS_RATE
        + completion / 1_000_000 * BATCH_OUTPUT_RATE
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": cached,
        "cache_miss_tokens": prompt - cached,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "pricing_version": BATCH_PRICING_VERSION,
        "pricing_period": "batch",
        "pricing_mode": "batch_actual",
        "request_started_utc": "openai-batch-provider-managed",
        "cache_hit_rate_per_million": BATCH_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": BATCH_CACHE_MISS_RATE,
        "output_rate_per_million": BATCH_OUTPUT_RATE,
    }


def _acceptance():
    result = {
        "novel_id": "great_king_zhuxia",
        "chapter": 1,
        "custom_id": "fpw0001-great_king_zhuxia-c0001-0123456789abcdef",
        "request_sha256": "a" * 64,
        "body_sha256": "b" * 64,
        "lineage_before_sha256": "c" * 64,
        "content": "Chapter 1\n\nPaid reviewed translation.",
        "finish_reason": "stop",
        "stats": _stats(),
        "provider_request_id": "http-request-1",
        "provider_response_id": "chat-response-1",
        "provider_response_model": BATCH_MODEL,
        "transport": "openai_batch",
    }
    return build_batch_acceptance_evidence(
        experiment_id="phase60g-partial-test",
        wave_number=1,
        batch_id="batch-phase60g-1",
        result=result,
    )


def _fixture(tmp_path):
    pending = tmp_path / "output" / "pending" / "0001.md"
    meta_path = tmp_path / "output" / "pending" / "0001.json"
    final = tmp_path / "output" / "chapters" / "0001.md"
    qa = tmp_path / "output" / "qa" / "0001.json"
    for path in (pending, meta_path, final, qa):
        path.parent.mkdir(parents=True, exist_ok=True)

    text = "Chapter 1\n\nPaid reviewed translation."
    pending.write_text(text, encoding="utf-8")
    final.write_text(text, encoding="utf-8")
    local_qa = {"commit_allowed": True, "issues": []}
    evidence = _acceptance()
    meta = {
        "chapter": 1,
        "stage": "reviewed",
        "translation_stats": _stats(),
        "local_qa": local_qa,
        "state": {"continuity_summary": "summary", "issues": []},
        "state_stats": {},
        "state_checkpoint_identity": {
            "version": "fixture-state-v1",
            "identity_sha256": "d" * 64,
        },
        "eligible_candidates": [],
        "prefilter_rejected": [],
        "review_result": {"decisions": [], "issues": []},
        "review_stats": {},
        "review_checkpoint_identity": {
            "version": "fixture-review-v1",
            "identity_sha256": "e" * 64,
        },
        ACCEPTANCE_FIELD: evidence,
    }
    meta.update(luna_batch_pending_provenance())
    _write_json(meta_path, meta)

    qa_payload = dict(local_qa)
    qa_payload.update(luna_batch_pending_provenance())
    qa_payload[ACCEPTANCE_FIELD] = evidence
    _write_json(qa, qa_payload)
    progress = {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
    }
    return pending, meta_path, final, qa, progress, evidence


def _inspect(parts):
    pending, meta_path, final, qa, progress, evidence = parts
    return inspect_experiment_partial_commit_evidence(
        evidence=evidence,
        chapter_number=1,
        pending_file=pending,
        pending_meta_file=meta_path,
        final_file=final,
        qa_file=qa,
        progress=progress,
    )


def test_exact_luna_and_batch_acceptance_additions_are_normalized_read_only(tmp_path):
    parts = _fixture(tmp_path)
    qa_before = parts[3].read_bytes()

    result = _inspect(parts)

    assert result["mode"] == PARTIAL_COMMIT_RECOVERY_MODE
    assert len(result["qa_sha256"]) == 64
    assert parts[3].read_bytes() == qa_before


def test_any_luna_provenance_mismatch_fails_before_partial_commit_tail(tmp_path):
    parts = _fixture(tmp_path)
    qa = parts[3]
    payload = json.loads(qa.read_text(encoding="utf-8"))
    payload["translation_model_version"] = "tampered"
    _write_json(qa, payload)

    with pytest.raises(
        FullPipelinePartialCommitV2Error,
        match="provenance mismatch",
    ):
        _inspect(parts)


def test_different_exact_batch_acceptance_fails_before_partial_commit_tail(tmp_path):
    parts = _fixture(tmp_path)
    qa = parts[3]
    payload = json.loads(qa.read_text(encoding="utf-8"))
    different = dict(payload[ACCEPTANCE_FIELD])
    different["batch_id"] = "different-batch"
    body = dict(different)
    body.pop("acceptance_sha256")
    import hashlib

    different["acceptance_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload[ACCEPTANCE_FIELD] = different
    _write_json(qa, payload)

    with pytest.raises(
        FullPipelinePartialCommitV2Error,
        match="different Batch acceptance",
    ):
        _inspect(parts)
