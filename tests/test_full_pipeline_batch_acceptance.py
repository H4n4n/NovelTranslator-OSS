import hashlib
import json
from types import SimpleNamespace

import pytest

from full_pipeline_batch_acceptance import (
    ACCEPTANCE_FIELD,
    FullPipelineBatchAcceptanceError,
    build_batch_acceptance_evidence,
    install_batch_acceptance_provenance,
    validate_batch_acceptance_evidence,
)
from full_pipeline_batch_wave import (
    BATCH_CACHE_HIT_RATE,
    BATCH_CACHE_MISS_RATE,
    BATCH_MODEL,
    BATCH_OUTPUT_RATE,
    BATCH_PRICING_VERSION,
)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _batch_stats():
    prompt_tokens = 100
    completion_tokens = 50
    cache_hit_tokens = 10
    cache_miss_tokens = prompt_tokens - cache_hit_tokens
    cost = (
        cache_hit_tokens / 1_000_000 * BATCH_CACHE_HIT_RATE
        + cache_miss_tokens / 1_000_000 * BATCH_CACHE_MISS_RATE
        + completion_tokens / 1_000_000 * BATCH_OUTPUT_RATE
    )
    return {
        "pricing_version": BATCH_PRICING_VERSION,
        "pricing_period": "batch",
        "pricing_mode": "batch_actual",
        "cache_hit_rate_per_million": BATCH_CACHE_HIT_RATE,
        "cache_miss_rate_per_million": BATCH_CACHE_MISS_RATE,
        "output_rate_per_million": BATCH_OUTPUT_RATE,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
    }


def _result():
    return {
        "novel_id": "great_king_zhuxia",
        "chapter": 7,
        "custom_id": "fpw0007-great_king_zhuxia-c0007-deadbeefdeadbeef",
        "request_sha256": "a" * 64,
        "body_sha256": "b" * 64,
        "lineage_before_sha256": "c" * 64,
        "content": "Chapter 7\n\nA complete Batch translation.",
        "finish_reason": "stop",
        "stats": _batch_stats(),
        "provider_request_id": "http-request-7",
        "provider_response_id": "chat-response-7",
        "provider_response_model": BATCH_MODEL,
        "transport": "openai_batch",
    }


def _evidence():
    return build_batch_acceptance_evidence(
        experiment_id="phase60-test",
        wave_number=7,
        batch_id="batch-test-7",
        result=_result(),
    )


def _module(tmp_path, write):
    return SimpleNamespace(
        atomic_write_json=write,
        startup_integrity_errors=lambda *_args, **_kwargs: [],
        run_unattended_production=lambda *_args, **_kwargs: None,
        PENDING_DIR=tmp_path / "pending",
        progress={"completed": [], "last_audited_chapter": 0},
        AUDIT_INTERVAL=25,
    )


def test_acceptance_evidence_binds_exact_collected_result():
    evidence = _evidence()
    assert evidence["wave_number"] == 7
    assert evidence["batch_id"] == "batch-test-7"
    assert evidence["provider_response_model"] == BATCH_MODEL
    assert evidence["translation_content_sha256"] == _sha(_result()["content"])
    assert validate_batch_acceptance_evidence(evidence) == evidence


def test_acceptance_hash_tampering_is_rejected():
    evidence = _evidence()
    evidence["batch_id"] = "different-batch"
    with pytest.raises(FullPipelineBatchAcceptanceError, match="hash mismatch"):
        validate_batch_acceptance_evidence(evidence)


def test_acceptance_rejects_wrong_provider_model_or_pricing():
    wrong_model = _result()
    wrong_model["provider_response_model"] = "wrong-model"
    with pytest.raises(FullPipelineBatchAcceptanceError, match="model"):
        build_batch_acceptance_evidence(
            experiment_id="phase60-test",
            wave_number=7,
            batch_id="batch-test-7",
            result=wrong_model,
        )

    wrong_price = _result()
    wrong_price["stats"] = dict(wrong_price["stats"])
    wrong_price["stats"]["pricing_version"] = "wrong-pricing"
    with pytest.raises(FullPipelineBatchAcceptanceError, match="pricing"):
        build_batch_acceptance_evidence(
            experiment_id="phase60-test",
            wave_number=7,
            batch_id="batch-test-7",
            result=wrong_price,
        )


def test_acceptance_rejects_tampered_batch_cost():
    wrong = _result()
    wrong["stats"] = dict(wrong["stats"])
    wrong["stats"]["cost_usd"] += 0.01
    with pytest.raises(FullPipelineBatchAcceptanceError, match="accounting"):
        build_batch_acceptance_evidence(
            experiment_id="phase60-test",
            wave_number=7,
            batch_id="batch-test-7",
            result=wrong,
        )


def test_qa_writer_adds_exact_evidence_only_to_expected_numeric_chapter(tmp_path):
    writes = []

    def write(path, payload):
        writes.append((path, json.loads(json.dumps(payload))))

    module = _module(tmp_path, write)
    evidence = _evidence()
    qa_dir = tmp_path / "output" / "qa"
    qa_dir.mkdir(parents=True)
    state = install_batch_acceptance_provenance(module, qa_dir, evidence)

    module.atomic_write_json(qa_dir / "0007.json", {"commit_allowed": True})
    module.atomic_write_json(qa_dir / "0008.json", {"commit_allowed": True})
    module.atomic_write_json(tmp_path / "other.json", {"value": 1})

    assert writes[0][1][ACCEPTANCE_FIELD] == evidence
    assert ACCEPTANCE_FIELD not in writes[1][1]
    assert ACCEPTANCE_FIELD not in writes[2][1]
    assert state["acceptance_sha256"] == evidence["acceptance_sha256"]
    assert state["startup_validation"]["mode"].startswith("full_pipeline_luna")
    assert state["runner_recovery"]["mode"].startswith("full_pipeline_exact")


def test_writer_refuses_conflicting_existing_acceptance(tmp_path):
    writes = []
    module = _module(
        tmp_path,
        lambda path, payload: writes.append(payload),
    )
    evidence = _evidence()
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    install_batch_acceptance_provenance(module, qa_dir, evidence)
    other = dict(evidence)
    other["batch_id"] = "other"
    other.pop("acceptance_sha256")
    other["acceptance_sha256"] = hashlib.sha256(
        json.dumps(other, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with pytest.raises(FullPipelineBatchAcceptanceError, match="different"):
        module.atomic_write_json(
            qa_dir / "0007.json",
            {ACCEPTANCE_FIELD: other},
        )
