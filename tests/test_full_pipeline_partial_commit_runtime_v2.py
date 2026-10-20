import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
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
    install_experiment_partial_commit_qa_adapter,
)


HELPER_PATH = Path(__file__).with_name("test_production_partial_commit_runtime.py")


def _helpers():
    name = "_phase60g_partial_runtime_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _batch_stats():
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


def _acceptance(content):
    result = {
        "novel_id": "great_king_zhuxia",
        "chapter": 1,
        "custom_id": "fpw0001-great_king_zhuxia-c0001-0123456789abcdef",
        "request_sha256": "a" * 64,
        "body_sha256": "b" * 64,
        "lineage_before_sha256": "c" * 64,
        "content": content,
        "finish_reason": "stop",
        "stats": _batch_stats(),
        "provider_request_id": "http-request-1",
        "provider_response_id": "chat-response-1",
        "provider_response_model": BATCH_MODEL,
        "transport": "openai_batch",
    }
    return build_batch_acceptance_evidence(
        experiment_id="phase60g-real-partial-runtime",
        wave_number=1,
        batch_id="batch-phase60g-real-partial",
        result=result,
    )


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _add_experiment_provenance(paths):
    meta = json.loads(paths["pending_meta"].read_text(encoding="utf-8"))
    evidence = _acceptance(paths["pending"].read_text(encoding="utf-8"))
    meta.update(luna_batch_pending_provenance())
    meta[ACCEPTANCE_FIELD] = evidence
    _write(paths["pending_meta"], meta)

    qa = json.loads(paths["qa_file"].read_text(encoding="utf-8"))
    qa.update(luna_batch_pending_provenance())
    qa[ACCEPTANCE_FIELD] = evidence
    _write(paths["qa_file"], qa)
    return evidence


def test_real_commit_tail_reconciles_exact_luna_batch_qa_without_rewriting_paid_bytes(
    monkeypatch,
    tmp_path,
):
    paths = _helpers()._configure(monkeypatch, tmp_path)
    evidence = _add_experiment_provenance(paths)
    final_before = paths["final"].read_bytes()
    qa_before = paths["qa_file"].read_bytes()
    original_inspector = production.inspect_reviewed_partial_commit_evidence
    install_experiment_partial_commit_qa_adapter(production, evidence)
    try:
        assert production.reconcile_reviewed_partial_commit_tail(1) is True
    finally:
        production.inspect_reviewed_partial_commit_evidence = original_inspector

    assert production.progress["completed"] == [1]
    assert production.progress["next_chapter"] == 2
    assert paths["final"].read_bytes() == final_before
    assert paths["qa_file"].read_bytes() == qa_before
    assert not paths["pending"].exists()
    assert not paths["pending_meta"].exists()


def test_real_commit_tail_rejects_tampered_luna_provenance_before_progress_advances(
    monkeypatch,
    tmp_path,
):
    paths = _helpers()._configure(monkeypatch, tmp_path)
    evidence = _add_experiment_provenance(paths)
    qa = json.loads(paths["qa_file"].read_text(encoding="utf-8"))
    qa["translation_pricing_version"] = "tampered"
    _write(paths["qa_file"], qa)
    progress_before = json.loads(json.dumps(production.progress))
    pending_before = paths["pending"].read_bytes()
    original_inspector = production.inspect_reviewed_partial_commit_evidence
    install_experiment_partial_commit_qa_adapter(production, evidence)
    try:
        with pytest.raises(
            FullPipelinePartialCommitV2Error,
            match="provenance mismatch",
        ):
            production.reconcile_reviewed_partial_commit_tail(1)
    finally:
        production.inspect_reviewed_partial_commit_evidence = original_inspector

    assert production.progress == progress_before
    assert paths["pending"].read_bytes() == pending_before
    assert paths["pending_meta"].is_file()
