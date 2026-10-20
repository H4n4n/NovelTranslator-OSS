import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from create_novel import initial_project_state
from full_pipeline_batch_wave import (
    BATCH_PRICING_VERSION,
    BATCH_CACHE_HIT_RATE,
    BATCH_CACHE_MISS_RATE,
    BATCH_OUTPUT_RATE,
)
from full_pipeline_luna_runtime import (
    LUNA_MAX_TOKENS,
    LUNA_MODEL,
    LUNA_REASONING,
    LUNA_STANDARD_CACHE_HIT_RATE,
    LUNA_STANDARD_CACHE_MISS_RATE,
    LUNA_STANDARD_OUTPUT_RATE,
    LUNA_STANDARD_PRICING_VERSION,
    FullPipelineLunaRuntimeError,
    LunaProviderIdentityError,
    initialize_engine_without_provider,
    install_luna_auxiliary_runtime,
    install_luna_batch_qa_provenance,
    load_experiment_engine_module,
    luna_batch_pending_provenance,
    luna_standard_pricing_snapshot,
    normalize_luna_request,
    validate_saved_luna_stats,
)
from novel_project import NovelConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCRIPT = (
    REPO_ROOT
    / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
FIXED_TIME = datetime(2026, 9, 15, 20, 30, tzinfo=timezone.utc)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _synthetic_project(root):
    root.mkdir(parents=True, exist_ok=True)
    config = NovelConfig.from_dict(
        {
            "id": "full_pipeline_runtime_fixture",
            "title": "Full Pipeline Runtime Fixture",
            "source_language": "zh",
            "target_language": "en",
            "profile": "historical_military",
            "final_chapter": 1,
            "audit_interval": 25,
            "chapter_numbering": "global",
            "source_type": "epub",
        }
    )
    _write_json(root / "novel.json", config.to_dict())
    (root / "translation_addendum.txt").write_text("", encoding="utf-8")
    state = initial_project_state()
    state["chapters.json"] = [
        {
            "chapter": 1,
            "heading": "第一章",
            "text": "第一章\n\n这是一段足够用于运行时测试的中文正文。",
        }
    ]
    for name, value in state.items():
        _write_json(root / "data" / name, value)
    return config


def _load_initialized_engine(tmp_path, monkeypatch):
    project_root = tmp_path / "experiment_project"
    _synthetic_project(project_root)
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    loaded = load_experiment_engine_module(
        PRODUCTION_SCRIPT,
        reporter=None,
    )
    module = loaded["module"]
    initialized = initialize_engine_without_provider(module)
    return project_root, module, initialized


class FakeCompletions:
    def __init__(self, model=LUNA_MODEL):
        self.model = model
        self.calls = []
        self.raise_error = None
        self.content = '{"new_terms":[],"continuity_summary":"ok","issues":[]}'

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        return SimpleNamespace(
            id="chatcmpl-fixture",
            model=self.model,
            system_fingerprint="fp-fixture",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=250,
                prompt_tokens_details=SimpleNamespace(cached_tokens=200),
            ),
        )


class FakeClient:
    def __init__(self, model=LUNA_MODEL):
        self.completions = FakeCompletions(model=model)
        self.chat = SimpleNamespace(completions=self.completions)


def _legacy_aux_request(max_tokens=3000):
    return {
        "model": LUNA_MODEL,
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "USER"},
        ],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _batch_stats():
    prompt = 1000
    hits = 200
    misses = 800
    completion = 500
    cost = (
        hits / 1_000_000 * BATCH_CACHE_HIT_RATE
        + misses / 1_000_000 * BATCH_CACHE_MISS_RATE
        + completion / 1_000_000 * BATCH_OUTPUT_RATE
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": hits,
        "cache_miss_tokens": misses,
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


def test_request_normalization_changes_only_provider_transport_controls():
    legacy = _legacy_aux_request(max_tokens=4321)
    normalized = normalize_luna_request(legacy)

    assert normalized["model"] == LUNA_MODEL
    assert normalized["messages"] == legacy["messages"]
    assert normalized["max_completion_tokens"] == 4321
    assert normalized["reasoning_effort"] == LUNA_REASONING
    assert "max_tokens" not in normalized
    assert "extra_body" not in normalized
    assert legacy["max_tokens"] == 4321
    assert "extra_body" in legacy


def test_request_normalization_rejects_wrong_model_or_unknown_deepseek_extension():
    wrong = _legacy_aux_request()
    wrong["model"] = "deepseek-v4-pro"
    with pytest.raises(FullPipelineLunaRuntimeError, match="model must be exact"):
        normalize_luna_request(wrong)

    bad_extra = _legacy_aux_request()
    bad_extra["extra_body"] = {"thinking": {"type": "enabled"}}
    with pytest.raises(FullPipelineLunaRuntimeError, match="unexpected DeepSeek"):
        normalize_luna_request(bad_extra)


def test_standard_pricing_snapshot_is_fixed_luna_standard_rate():
    pricing = luna_standard_pricing_snapshot(FIXED_TIME)
    assert pricing["pricing_version"] == LUNA_STANDARD_PRICING_VERSION
    assert pricing["pricing_period"] == "standard"
    assert pricing["pricing_mode"] == "synchronous"
    assert pricing["cache_hit_rate_per_million"] == LUNA_STANDARD_CACHE_HIT_RATE
    assert pricing["cache_miss_rate_per_million"] == LUNA_STANDARD_CACHE_MISS_RATE
    assert pricing["output_rate_per_million"] == LUNA_STANDARD_OUTPUT_RATE


def test_saved_batch_accounting_is_replayed_exactly_without_deepseek_repricing():
    stats = _batch_stats()
    recovered = validate_saved_luna_stats(stats)
    assert recovered == stats
    assert recovered is not stats

    zero = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }
    assert validate_saved_luna_stats(zero) == zero

    deepseek = dict(stats)
    deepseek["pricing_version"] = "deepseek-v4-pro-0813-peak-offpeak"
    with pytest.raises(FullPipelineLunaRuntimeError, match="unrecognized Luna pricing"):
        validate_saved_luna_stats(deepseek)


def test_real_engine_initializes_without_env_or_usable_provider_client(
    tmp_path,
    monkeypatch,
):
    project_root, module, initialized = _load_initialized_engine(tmp_path, monkeypatch)

    assert initialized == {
        "status": "initialized_without_provider",
        "provider_constructor_calls": 1,
        "runtime_initialized": True,
        "client": None,
        "api_key_loaded": False,
    }
    assert module._RUNTIME_INITIALIZED is True
    assert module.client is None
    assert module.api_key is None
    assert module.ACTIVE_NOVEL_CONFIG.id == "full_pipeline_runtime_fixture"
    assert module.progress["next_chapter"] == 1
    assert module.glossary == {"entries": {}}
    assert module.continuity == {"latest_chapter": 0, "summary": ""}
    assert module.issues_data == {"issues": []}
    assert module.audit_history == {"audits": []}
    assert (project_root / "output" / "pending").is_dir()
    assert callable(module.translation_commit_qa)


def test_luna_runtime_makes_exactly_one_normalized_provider_attempt_and_prices_usage(
    tmp_path,
    monkeypatch,
):
    _root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    client = FakeClient()
    runtime = install_luna_auxiliary_runtime(
        module,
        client,
        now_utc=lambda: FIXED_TIME,
    )

    response = module.call_api_with_retries(**_legacy_aux_request(max_tokens=800))
    assert response.model == LUNA_MODEL
    assert len(client.completions.calls) == 1
    sent = client.completions.calls[0]
    assert sent["model"] == LUNA_MODEL
    assert sent["max_completion_tokens"] == 800
    assert sent["reasoning_effort"] == "none"
    assert "max_tokens" not in sent
    assert "extra_body" not in sent
    assert runtime["model"] == LUNA_MODEL
    assert module.MODEL == LUNA_MODEL
    assert module.AUTO_OFF_PEAK_ONLY is False

    stats = module.usage_and_cost(response.usage)
    expected_cost = (
        200 / 1_000_000 * LUNA_STANDARD_CACHE_HIT_RATE
        + 800 / 1_000_000 * LUNA_STANDARD_CACHE_MISS_RATE
        + 250 / 1_000_000 * LUNA_STANDARD_OUTPUT_RATE
    )
    assert stats["prompt_tokens"] == 1000
    assert stats["cache_hit_tokens"] == 200
    assert stats["cache_miss_tokens"] == 800
    assert stats["cost_usd"] == pytest.approx(expected_cost)
    assert stats["pricing_version"] == LUNA_STANDARD_PRICING_VERSION


def test_luna_direct_provider_failure_is_not_retried(tmp_path, monkeypatch):
    _root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    client = FakeClient()
    client.completions.raise_error = RuntimeError("provider failed")
    install_luna_auxiliary_runtime(module, client, now_utc=lambda: FIXED_TIME)

    with pytest.raises(RuntimeError, match="provider failed"):
        module.call_api_with_retries(**_legacy_aux_request())
    assert len(client.completions.calls) == 1


def test_direct_response_model_mismatch_fails_closed(tmp_path, monkeypatch):
    _root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    client = FakeClient(model="wrong-model")
    install_luna_auxiliary_runtime(module, client, now_utc=lambda: FIXED_TIME)

    with pytest.raises(LunaProviderIdentityError, match="model identity mismatch"):
        module.call_api_with_retries(**_legacy_aux_request())
    assert len(client.completions.calls) == 1


def test_journaled_wrong_model_is_recorded_then_replayed_without_reissue(
    tmp_path,
    monkeypatch,
):
    _root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    client = FakeClient(model="wrong-model")
    install_luna_auxiliary_runtime(module, client, now_utc=lambda: FIXED_TIME)

    request = _legacy_aux_request()
    with pytest.raises(LunaProviderIdentityError, match="newly recorded"):
        module.journaled_chat_request(
            "state_extraction",
            1,
            "第一章",
            request,
            translation_text="Chapter One",
        )
    assert len(client.completions.calls) == 1

    journal_path = module.PAID_REQUEST_JOURNAL_DIR / "0001.state_extraction.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "response_recorded"
    assert journal["provider"] == "openai"
    assert journal["model"] == LUNA_MODEL
    assert journal["response"]["provider_response_model"] == "wrong-model"

    client.completions.model = LUNA_MODEL
    with pytest.raises(LunaProviderIdentityError, match="replayed"):
        module.journaled_chat_request(
            "state_extraction",
            1,
            "第一章",
            request,
            translation_text="Chapter One",
        )
    assert len(client.completions.calls) == 1


def test_journal_identity_hashes_actual_openai_request_not_legacy_transport(
    tmp_path,
    monkeypatch,
):
    _root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    client = FakeClient()
    install_luna_auxiliary_runtime(module, client, now_utc=lambda: FIXED_TIME)
    request = _legacy_aux_request(max_tokens=1234)

    result = module.journaled_chat_request(
        "state_extraction",
        1,
        "第一章",
        request,
        translation_text="Chapter One",
    )
    assert result["replayed"] is False
    assert len(client.completions.calls) == 1
    actual = client.completions.calls[0]
    assert actual["max_completion_tokens"] == 1234
    assert actual["reasoning_effort"] == "none"

    journal = json.loads(
        (module.PAID_REQUEST_JOURNAL_DIR / "0001.state_extraction.json").read_text(
            encoding="utf-8"
        )
    )
    expected_identity = module.request_identity(
        operation_kind="state_extraction",
        novel_id="full_pipeline_runtime_fixture",
        chapter=1,
        provider="openai",
        model=LUNA_MODEL,
        profile_id=module.ACTIVE_PROFILE.id,
        source_sha256=__import__("hashlib").sha256("第一章".encode("utf-8")).hexdigest(),
        translation_sha256=__import__("hashlib").sha256(
            "Chapter One".encode("utf-8")
        ).hexdigest(),
        request_payload=actual,
    )
    assert journal["request_sha256"] == expected_identity["request_sha256"]
    assert journal["operation_id"] == expected_identity["operation_id"]


def test_committed_qa_provenance_records_batch_translation_identity(
    tmp_path,
    monkeypatch,
):
    root, module, _initialized = _load_initialized_engine(tmp_path, monkeypatch)
    state = install_luna_batch_qa_provenance(module, root / "output" / "qa")

    qa_path = root / "output" / "qa" / "0001.json"
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    module.atomic_write_json(qa_path, {"chapter": 1, "commit_allowed": True})
    payload = json.loads(qa_path.read_text(encoding="utf-8"))
    assert payload["translation_provider"] == "openai"
    assert payload["translation_transport"] == "openai_batch"
    assert payload["translation_model"] == LUNA_MODEL
    assert payload["translation_thinking"] == "none"
    assert payload["translation_max_tokens"] == LUNA_MAX_TOKENS
    assert payload["translation_pricing_version"] == BATCH_PRICING_VERSION
    assert state["mode"] == "full_pipeline_luna_batch_qa_provenance_v1"

    other = root / "data" / "other.json"
    module.atomic_write_json(other, {"chapter": 1})
    other_payload = json.loads(other.read_text(encoding="utf-8"))
    assert "translation_model" not in other_payload


def test_pending_provenance_is_exact_batch_luna_identity():
    provenance = luna_batch_pending_provenance()
    assert provenance == {
        "translation_provider": "openai",
        "translation_transport": "openai_batch",
        "translation_model": LUNA_MODEL,
        "translation_model_key": "luna",
        "translation_model_version": "gpt-5.6-luna",
        "translation_thinking": "none",
        "translation_max_tokens": 12000,
        "translation_pricing_version": BATCH_PRICING_VERSION,
    }
