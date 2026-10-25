"""Real durable journals and fake SDK responses only; no Provider access."""
import builtins
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone

import pytest

import production_translation_recovery as recovery
from production_operation_policy import policy_for_model
from production_paid_request_journal import PaidRequestJournalError, PaidRequestUncertainError, _with_record_hash
from provider_model_identity_runtime import install_provider_model_identity_runtime, ProviderModelIdentityError
from translation_model_config import pending_translation_model_record
from translation_model_runtime import install_pending_model_provenance, resolve_runtime_model
from noveltranslator_core import commit_chapter_accounting
import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine

PARTIAL = "PRIVATE_PARTIAL The knight crossed the valley and saw the old fortress."
FINAL = "PRIVATE_FINAL The complete fresh chapter ends here with all its events."
CHAPTER = {"chapter": 1, "text": "PRIVATE_SOURCE authoritative source chapter", "title": "Fixture"}


class DownstreamReached(Exception):
    pass


@pytest.fixture
def harness(monkeypatch, tmp_path):
    calls, events, initial_bytes = [], [], []
    answers = [(PARTIAL, "length", "deepseek-flash"), (FINAL, "stop", "deepseek-flash")]
    journal = tmp_path / "data" / "provider_request_journal"
    pending = tmp_path / "output" / "pending"
    pending.mkdir(parents=True)
    monkeypatch.setattr(engine, "MODEL", "deepseek-flash")
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_MAX_TOKENS", 12000)
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_THINKING", "disabled")
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", None)
    monkeypatch.setattr(engine, "ACTIVE_PROFILE", SimpleNamespace(id="historical_military", high_priority_categories=[]))
    monkeypatch.setattr(engine, "PAID_REQUEST_JOURNAL_DIR", journal)
    monkeypatch.setattr(engine, "PENDING_DIR", pending)
    monkeypatch.setattr(engine, "OUTPUT_DIR", tmp_path / "chapters")
    monkeypatch.setattr(engine, "chapter_map", {1: CHAPTER})
    monkeypatch.setattr(engine, "progress", {"completed": [], "last_completed": 0, "next_chapter": 1})
    monkeypatch.setattr(engine, "system_prompt", "PRIVATE_SYSTEM translation policy")
    monkeypatch.setattr(engine, "continuity", {"summary": "PRIVATE_CONTINUITY"})
    monkeypatch.setattr(engine, "glossary", {"entries": {}})
    monkeypatch.setattr(engine, "relevant_glossary", lambda source: {})
    monkeypatch.setattr(engine, "glossary_to_text", lambda entries, *, chapter_number: "PRIVATE_GLOSSARY")
    monkeypatch.setattr(engine, "wait_for_api_permission", lambda: None)
    monkeypatch.setattr(engine, "RUN_LOGGER", None)
    monkeypatch.setattr(engine, "log_event", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(engine, "log_error", lambda *args, **kw: None)
    monkeypatch.setattr(engine, "pricing_snapshot", lambda: {"pricing_period": "off_peak"})
    def stats(usage, pricing):
        # Different accepted rate windows prove aggregate never gets repriced.
        config = __import__("translation_model_config").resolve_translation_model(engine.MODEL)
        period = "off_peak" if usage.prompt_tokens == 10 else "peak"
        rates = config.prices[period]
        cost = (usage.prompt_tokens * rates["cache_miss"] + usage.completion_tokens * rates["output"]) / 1_000_000
        return {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "cache_hit_tokens": 0, "cache_miss_tokens": usage.prompt_tokens,
                "cost_usd": cost, "rate_correct_cost_usd": cost,
                "pricing_version": config.pricing_version, "pricing_period": period,
                "request_started_utc": "2026-10-02T12:00:00+00:00" if period == "off_peak" else "2026-10-02T01:00:00+00:00",
                "cache_hit_rate_per_million": rates["cache_hit"], "cache_miss_rate_per_million": rates["cache_miss"],
                "output_rate_per_million": rates["output"]}
    monkeypatch.setattr(engine, "usage_and_cost_for_pricing", stats)
    def send(**kwargs):
        if calls:
            initial_bytes.append((journal / "0001.chapter_translation.json").read_bytes())
        calls.append(copy.deepcopy(kwargs))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        text, finish, model = answer
        return SimpleNamespace(model=model, id=f"response-{len(calls)}", system_fingerprint="fixture",
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=10 * len(calls), completion_tokens=20 * len(calls),
                                  cost=.2 * len(calls)))
    monkeypatch.setattr(engine, "send_api_request_once", send)
    monkeypatch.setattr(engine, "run_journaled_paid_request", engine.run_journaled_paid_request)
    state_attr = "_noveltranslator_provider_model_identity_runtime_state"
    monkeypatch.delattr(engine, state_attr, raising=False)
    # monkeypatch records replacement so each fixture restores the guard state.
    monkeypatch.setattr(engine, state_attr, None, raising=False)
    install_provider_model_identity_runtime(engine, now_utc=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc))
    def downstream(*args, **kwargs):
        raise DownstreamReached
    monkeypatch.setattr(engine, "canonicalize_locked_high_priority_terms", downstream)
    monkeypatch.setattr(engine, "atomic_write_text", engine.atomic_write_text)
    monkeypatch.setattr(engine, "atomic_write_json", engine.atomic_write_json)
    import noveltranslator_core as core
    monkeypatch.setattr(core, "atomic_write_text", core.atomic_write_text)
    monkeypatch.setattr(core, "atomic_write_json", core.atomic_write_json)
    config = __import__("translation_model_config").resolve_translation_model("v41-flash")
    monkeypatch.setattr(core, "PRICING_VERSION", config.pricing_version)
    monkeypatch.setattr(core, "V4_PRO_PRICES", config.prices)
    install_pending_model_provenance(core, "v41-flash", pending)
    # The legacy module captures the core writers at import; install this bridge
    # just as the real launcher does before legacy import.
    monkeypatch.setattr(engine, "atomic_write_text", core.atomic_write_text)
    monkeypatch.setattr(engine, "atomic_write_json", core.atomic_write_json)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    return SimpleNamespace(calls=calls, answers=answers, events=events, journal=journal,
                           pending=pending, initial_bytes=initial_bytes)


def run():
    return engine.translate_chapter(CHAPTER)


def document(h, kind):
    return json.loads((h.journal / f"0001.{kind}.json").read_text(encoding="utf-8"))


def test_normal_stop_is_request_identical_and_historical_journal_replays(harness):
    h = harness
    h.answers[:] = [(FINAL, "stop", "deepseek-flash")]
    result = run()
    assert not result.recovery_attempted and not result.failure
    assert len(h.calls) == 1
    assert h.calls[0] == {"model": "deepseek-flash", "messages": h.calls[0]["messages"],
                           "max_tokens": 12000, "extra_body": {"thinking": {"type": "disabled"}}}
    identity = engine._chat_request_identity("chapter_translation", 1, CHAPTER["text"], h.calls[0])
    assert document(h, "chapter_translation")["operation_id"] == identity["operation_id"]
    before = (h.journal / "0001.chapter_translation.json").read_bytes()
    replay = run()
    assert replay.initial["replayed"] and not replay.recovery_attempted
    assert replay.content == FINAL and replay.stats == result.stats
    assert len(h.calls) == 1
    assert (h.journal / "0001.chapter_translation.json").read_bytes() == before
    assert not (h.journal / "0001.chapter_translation_recovery.json").exists()


def test_full_recovery_separate_journal_binding_accounting_replay_and_no_content_logging(harness):
    h = harness
    result = run()
    assert result.recovery_used and result.recovery_attempted and result.content == FINAL
    assert PARTIAL not in result.content
    assert len(h.calls) == 2
    assert h.calls[1] == {**h.calls[0], "max_tokens": 24000}
    assert PARTIAL not in json.dumps(h.calls[1])
    assert "reasoning_effort" not in h.calls[1]
    initial, recovered = document(h, "chapter_translation"), document(h, "chapter_translation_recovery")
    assert initial["operation_id"] != recovered["operation_id"]
    assert recovered["translation_sha256"]
    assert initial["translation_sha256"] == ""
    assert (h.journal / "0001.chapter_translation.json").read_bytes() == h.initial_bytes[0]
    expected_binding = json.dumps({"initial_operation_id": initial["operation_id"],
        "initial_response_sha256": initial["response_sha256"],
        "recovery_policy_sha256": recovery.translation_recovery_policy_sha256(engine.current_production_operation_policy())},
        sort_keys=True, separators=(",", ":"))
    assert recovered["translation_sha256"] == hashlib.sha256(expected_binding.encode()).hexdigest()
    assert result.stats["prompt_tokens"] == 30 and result.stats["completion_tokens"] == 60
    assert result.stats["cost_usd"] == pytest.approx(.0000675)
    assert engine.reprice_saved_stats_if_needed(result.stats) == result.stats
    replay = run()
    assert replay.content == result.content and replay.stats == result.stats
    assert replay.initial["replayed"] and replay.recovery["replayed"]
    assert len(h.calls) == 2
    progress = {}
    assert commit_chapter_accounting(progress, 1, replay.stats)
    assert not commit_chapter_accounting(progress, 1, replay.stats)
    assert progress["estimated_cost_usd"] == pytest.approx(.0000675)
    serialized = json.dumps(h.events)
    assert all(marker not in serialized for marker in
               ("PRIVATE_SOURCE", "PRIVATE_PARTIAL", "PRIVATE_FINAL", "PRIVATE_SYSTEM", "PRIVATE_GLOSSARY", "PRIVATE_CONTINUITY"))
    assert "translation_recovery_replayed" in serialized


@pytest.mark.parametrize("point", ["translation_saturation_detected", "translation_recovery_end"])
def test_restart_after_recorded_initial_or_recorded_recovery(harness, monkeypatch, point):
    h = harness
    event = engine.log_event
    def crash(name, **fields):
        if name == point:
            raise KeyboardInterrupt("simulated crash")
        event(name, **fields)
    monkeypatch.setattr(engine, "log_event", crash)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert len(h.calls) == (1 if point == "translation_saturation_detected" else 2)
    monkeypatch.setattr(engine, "log_event", event)
    result = run()
    assert result.initial["replayed"] and result.recovery_used
    assert result.recovery["replayed"] == (point == "translation_recovery_end")
    assert len(h.calls) == 2


@pytest.mark.parametrize("content,finish", [(PARTIAL, "length"), ("", "stop"),
    ("   ", "stop"), ("", "content_filter"), (FINAL, "cancelled"), (FINAL, "tool_calls")])
def test_recovery_failure_terminal_no_third_request_or_pending(harness, content, finish):
    h = harness
    h.answers[1] = (content, finish, "deepseek-flash")
    assert engine.process_chapter(1) is False
    assert engine.process_chapter(1) is False
    assert len(h.calls) == 2
    assert not list(h.pending.iterdir())
    assert document(h, "chapter_translation")["status"] == "response_recorded"
    assert document(h, "chapter_translation_recovery")["status"] == "response_recorded"
    assert not engine.progress["completed"]
    replay = run()
    assert replay.failure and replay.recovery_attempted and not replay.recovery_used
    assert replay.stats["cost_usd"] == pytest.approx(.0000675)
    assert len(h.calls) == 2


@pytest.mark.parametrize("content,finish", [("", "stop"), (" ", "stop"), ("", "content_filter"),
    (PARTIAL, "content_filter"), (PARTIAL, "cancelled"), (PARTIAL, "tool_calls"),
    (PARTIAL, "unknown"), (PARTIAL, None), ("", "length"), ("...", "length"), ("Tiny word", "length")])
def test_ineligible_initial_never_recovers(harness, content, finish):
    h = harness
    h.answers[:] = [(content, finish, "deepseek-flash")]
    assert engine.process_chapter(1) is False
    assert engine.process_chapter(1) is False
    assert len(h.calls) == 1
    assert not list(h.pending.iterdir())
    assert not (h.journal / "0001.chapter_translation_recovery.json").exists()


@pytest.mark.parametrize("operation", ["chapter_translation", "chapter_translation_recovery"])
def test_uncertain_request_blocks_resend_and_further_recovery(harness, operation):
    h = harness
    if operation == "chapter_translation":
        h.answers[:] = [KeyboardInterrupt("remote outcome unknown")]
    else:
        h.answers[1] = KeyboardInterrupt("remote outcome unknown")
    with pytest.raises(KeyboardInterrupt):
        run()
    count = len(h.calls)
    assert document(h, operation)["status"] == "uncertain"
    with pytest.raises(PaidRequestUncertainError):
        run()
    assert len(h.calls) == count
    # A hard process death can leave STARTED instead of UNCERTAIN.
    record = document(h, operation)
    record["status"] = "started"
    path = h.journal / f"0001.{operation}.json"
    path.write_text(json.dumps(_with_record_hash(record)), encoding="utf-8")
    with pytest.raises(PaidRequestUncertainError):
        run()
    assert len(h.calls) == count


@pytest.mark.parametrize("kind", ["initial_corrupt", "initial_identity", "recovery_identity", "changed_response", "changed_usage", "changed_provider_id"])
def test_mismatched_or_corrupt_journal_never_bypassed(harness, kind):
    h = harness
    run()
    slot = "chapter_translation_recovery" if kind == "recovery_identity" else "chapter_translation"
    record = document(h, slot)
    if kind == "initial_corrupt":
        record["record_sha256"] = "0" * 64
    elif kind in {"changed_response", "changed_usage", "changed_provider_id"}:
        if kind == "changed_response":
            record["response"]["content"] += " Different saturated response."
        elif kind == "changed_usage":
            record["response"]["stats"]["completion_tokens"] += 1
        else:
            record["response"]["provider_response_id"] = "different-paid-response"
        record["response_sha256"] = recovery.response_sha256(record["response"])
        record = _with_record_hash(record)
    else:
        record["request_sha256"] = "0" * 64
        record = _with_record_hash(record)
    (h.journal / f"0001.{slot}.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PaidRequestJournalError):
        run()
    assert len(h.calls) == 2


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_other_models_have_no_v41_recovery(harness, monkeypatch, model):
    h = harness
    monkeypatch.setattr(engine, "MODEL", model)
    h.answers[:] = [(PARTIAL, "length", model)]
    if model == "deepseek-v4-flash":
        # New retired sends are guarded; replay its historical record only.
        from production_paid_request_journal import run_journaled_paid_request
        monkeypatch.setattr(engine, "run_journaled_paid_request", run_journaled_paid_request)
        monkeypatch.setattr(engine, "send_api_request_once", lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=PARTIAL), finish_reason="length")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, cost=.2), id="old"))
    result = run()
    assert result.failure == "translation_recovery_unavailable"
    assert not result.recovery_attempted
    assert engine.current_production_operation_policy().translation_recovery is None
    assert not (h.journal / "0001.chapter_translation_recovery.json").exists()


@pytest.mark.parametrize("operation", ["chapter_translation", "chapter_translation_recovery"])
@pytest.mark.parametrize("observed", ["deepseek-v4-pro", "", "unexpected-model"])
def test_provider_identity_guard_applies_to_both_operations_and_replays(harness, operation, observed):
    h = harness
    index = 0 if operation == "chapter_translation" else 1
    h.answers[index] = (PARTIAL if index == 0 else FINAL, "length" if index == 0 else "stop", observed)
    with pytest.raises(ProviderModelIdentityError):
        run()
    assert document(h, operation)["status"] == "response_recorded"
    with pytest.raises(ProviderModelIdentityError):
        run()
    assert len(h.calls) == index + 1
    assert not list(h.pending.iterdir())


@pytest.mark.parametrize("phase", ["metadata_before", "metadata_after", "markdown_before", "markdown_after"])
def test_recovered_pending_crash_windows_preserve_accounting_pin_and_reuse(harness, monkeypatch, phase):
    h = harness
    original_text, original_json = engine.atomic_write_text, engine.save_json
    def text(path, value, *args, **kwargs):
        if Path(path) == h.pending / "0001.md":
            if phase == "markdown_before":
                raise KeyboardInterrupt
            original_text(path, value, *args, **kwargs)
            if phase == "markdown_after":
                raise KeyboardInterrupt
        else:
            original_text(path, value, *args, **kwargs)
    def meta(path, value):
        if Path(path) == h.pending / "0001.json":
            if phase == "metadata_before":
                raise KeyboardInterrupt
            original_json(path, value)
            if phase == "metadata_after":
                raise KeyboardInterrupt
        else:
            original_json(path, value)
    monkeypatch.setattr(engine, "atomic_write_text", text)
    monkeypatch.setattr(engine, "save_json", meta)
    with pytest.raises(KeyboardInterrupt):
        engine.process_chapter(1)
    assert len(h.calls) == 2
    from noveltranslator_core import inspect_pending_state
    assert inspect_pending_state(h.pending, [], 1)["errors"] == []
    engine.perform_startup_self_check()
    monkeypatch.setattr(engine, "atomic_write_text", original_text)
    monkeypatch.setattr(engine, "save_json", original_json)
    if (h.pending / "0001.md").exists():
        monkeypatch.setattr(engine, "translate_chapter", lambda *a: pytest.fail("paid pending must win"))
    with pytest.raises(DownstreamReached):
        engine.process_chapter(1)
    assert len(h.calls) == 2
    assert (h.pending / "0001.md").read_text(encoding="utf-8") == FINAL
    meta = json.loads((h.pending / "0001.json").read_text(encoding="utf-8"))
    recovery.validate_recovery_provenance(meta)
    assert meta["translation_max_tokens"] == 24000
    assert meta["translation_stats"]["cost_usd"] == pytest.approx(.0000675)
    assert meta["finish_reason"] == "stop"
    assert resolve_runtime_model("pro", h.pending)["effective_model"] == "deepseek-flash"
    assert PARTIAL not in json.dumps(meta)


@pytest.mark.parametrize("pinned", [False, True])
def test_old_paid_pending_never_generates_or_changes_model_provenance(harness, monkeypatch, pinned):
    h = harness
    (h.pending / "0001.md").write_text(FINAL, encoding="utf-8")
    meta = {"finish_reason": "stop", "translation_stats": engine.zero_stats()}
    if pinned:
        meta.update(pending_translation_model_record("v41-flash"))
    (h.pending / "0001.json").write_text(json.dumps(meta), encoding="utf-8")
    before = (h.pending / "0001.json").read_bytes()
    monkeypatch.setattr(engine, "translate_chapter", lambda *a: pytest.fail("pending regeneration"))
    with pytest.raises(DownstreamReached):
        engine.process_chapter(1)
    assert not h.calls and not h.journal.exists()
    assert (h.pending / "0001.json").read_bytes() == before


@pytest.mark.parametrize("model,version", [("deepseek-flash", "DeepSeek-V4.1-Flash"),
                                         ("deepseek-v4-pro", "DeepSeek-V4-Pro-0813")])
def test_runtime_version_binds_registry_without_changing_payload(harness, monkeypatch, model, version):
    monkeypatch.setattr(engine, "MODEL", model)
    policy = engine.current_production_operation_policy()
    assert policy.model_version == version
    assert policy.identity()["model_binding"] == {"model": model, "version": version}
    assert policy.translation.request_controls() == {"max_tokens": 12000,
                                                   "extra_body": {"thinking": {"type": "disabled"}}}
    assert policy_for_model("pro").translation_recovery is None


def test_missing_durable_proof_does_not_recover(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(engine, "load_matching_journal", lambda *a: None)
    with pytest.raises(recovery.TranslationRecoveryError, match="durable proof"):
        run()
    assert len(h.calls) == 1


def test_unreviewed_runtime_controls_do_not_enable_recovery(harness, monkeypatch):
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_MAX_TOKENS", 100)
    assert run().failure == "translation_recovery_unavailable"
    assert len(harness.calls) == 1


def test_benchmark_model_does_not_enable_recovery(harness, monkeypatch):
    monkeypatch.setattr(engine, "MODEL", "benchmark-only-model")
    assert engine.current_production_operation_policy().translation_recovery is None
    assert engine.current_production_operation_policy().model_version == ""


def test_recovery_import_has_no_io_credentials_sdk_or_network(monkeypatch):
    source = Path(recovery.__file__).read_text(encoding="utf-8-sig")
    module = ModuleType("_inert_translation_recovery")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    def forbidden(*a, **k):
        raise AssertionError("import performed forbidden IO")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    import os
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os, "environ", {})
    original_import = builtins.__import__
    def guarded(name, *a, **k):
        if name.split(".")[0] in {"openai", "dotenv"}:
            forbidden()
        return original_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", guarded)
    exec(compile(source, recovery.__file__, "exec"), module.__dict__)
    assert module.classify_initial_response({"content": PARTIAL, "finish_reason": "length"}) == "durable_length_response"


@pytest.mark.parametrize("tamper", ["missing_journal", "uncertain", "hash", "binding", "model", "usage", "text"])
def test_pending_prefix_requires_both_completed_exact_journals(harness, monkeypatch, tamper):
    h = harness
    original = engine.atomic_write_text
    def crash(path, value, *args, **kwargs):
        if Path(path) == h.pending / "0001.md":
            raise KeyboardInterrupt
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(engine, "atomic_write_text", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.process_chapter(1)
    path = h.journal / "0001.chapter_translation_recovery.json"
    record = document(h, "chapter_translation_recovery")
    if tamper == "missing_journal":
        path.unlink()
    elif tamper == "text":
        (h.pending / "0001.md").write_text("Tampered final English", encoding="utf-8")
    else:
        if tamper == "uncertain":
            record.update(status="uncertain", response={}, response_sha256="")
        elif tamper == "hash":
            record["response_sha256"] = "0" * 64
        elif tamper == "binding":
            record["translation_sha256"] = "0" * 64
        elif tamper == "model":
            record["response"]["provider_response_model"] = "deepseek-v4-pro"
            record["response_sha256"] = recovery.response_sha256(record["response"])
        elif tamper == "usage":
            record["response"]["stats"]["cost_usd"] += 1
            record["response_sha256"] = recovery.response_sha256(record["response"])
        path.write_text(json.dumps(_with_record_hash(record)), encoding="utf-8")
    from noveltranslator_core import inspect_pending_state
    assert inspect_pending_state(h.pending, [], 1)["errors"]
    assert len(h.calls) == 2


@pytest.mark.parametrize("tamper", ["total", "individual_cost", "duplicate", "nested", "incomplete", "total_nan", "individual_nan"])
def test_aggregate_stats_validation_preserves_original_price_guards(harness, tamper):
    stats = copy.deepcopy(run().stats)
    from noveltranslator_core import validate_stats_record
    assert validate_stats_record(stats, "translation") == []
    operations = stats["translation_accepted_operations"]
    if tamper == "total_nan":
        stats["cost_usd"] = float("nan")
    elif tamper == "individual_nan":
        operations[0]["stats"]["cost_usd"] = float("nan")
    elif tamper == "total":
        stats["cost_usd"] += 1
    elif tamper == "individual_cost":
        operations[0]["stats"]["cost_usd"] += 1
    elif tamper == "duplicate":
        operations[1]["operation_id"] = operations[0]["operation_id"]
    elif tamper == "nested":
        operations[0]["stats"]["translation_accepted_operations"] = copy.deepcopy(operations)
    else:
        operations.pop()
    assert validate_stats_record(stats, "translation")


@pytest.mark.parametrize("field,value", [("translation_model_version", "invented"),
    ("translation_thinking", "enabled"), ("translation_recovery_max_tokens", 48000),
    ("initial_finish_reason", "stop"), ("recovery_finish_reason", "length"),
    ("initial_translation_operation_id", "invalid"), ("translation_recovery_policy_revision", "")])
def test_pending_writer_does_not_normalize_unsafe_recovery_provenance(harness, field, value):
    h = harness
    with pytest.raises(DownstreamReached):
        engine.process_chapter(1)
    path = h.pending / "0001.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    before = path.read_bytes()
    meta[field] = value
    with pytest.raises(recovery.TranslationRecoveryError):
        engine.save_json(path, meta)
    assert path.read_bytes() == before


def test_unrelated_state_policy_preserves_recorded_recovery_replay(harness, monkeypatch):
    h = harness
    run()
    initial = (h.journal / "0001.chapter_translation.json").read_bytes()
    from production_state_extraction import StateExtractionPolicy
    monkeypatch.setattr(engine, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(recovery_tokens=9000))
    replay = run()
    assert replay.initial["replayed"] and replay.recovery["replayed"]
    assert replay.recovery_used and replay.content == FINAL
    assert replay.recovery_policy_sha256 == recovery.translation_recovery_policy_sha256(policy_for_model("v41-flash"))
    assert len(h.calls) == 2
    assert (h.journal / "0001.chapter_translation.json").read_bytes() == initial


def test_changed_full_context_blocks_initial_replay_and_recovery(harness, monkeypatch):
    h = harness
    run()
    monkeypatch.setattr(engine, "continuity", {"summary": "A different authoritative context"})
    with pytest.raises(PaidRequestJournalError):
        run()
    assert len(h.calls) == 2


def test_recovered_stop_still_passes_normal_commit_qa_and_preserves_failed_pending(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(engine, "canonicalize_locked_high_priority_terms", lambda *a, chapter_number: {"repairs_applied": []})
    monkeypatch.setattr(engine, "perform_deterministic_strict_terminology_repair", lambda **kw: {"repairs_applied": []})
    monkeypatch.setattr(engine, "maybe_revalidate_recorded_strict_terminology", lambda *a: {"status": "absent"})
    def clean(chapter, source, text, meta, *args):
        return {"status": "clean", "translation": text, "meta": meta}
    monkeypatch.setattr(engine, "maybe_repair_strict_terminology", clean)
    monkeypatch.setattr(engine, "maybe_repair_residual_cjk", clean)
    seen = []
    def qa(chapter, source, text, finish):
        seen.append((text, finish))
        return {"commit_allowed": False, "issues": ["Completeness blocker"],
                "remaining_chinese_characters": 0, "residual_cjk_fragments": [], "locked_high_priority_term_violations": []}
    monkeypatch.setattr(engine, "local_qa", qa)
    monkeypatch.setattr(engine, "extract_state", lambda *a: pytest.fail("blocked QA must precede state"))
    monkeypatch.setattr(engine, "review_new_terms", lambda *a: pytest.fail("blocked QA must precede review"))
    assert engine.process_chapter(1) is False
    assert engine.process_chapter(1) is False
    assert seen == [(FINAL, "stop"), (FINAL, "stop")]
    assert len(h.calls) == 2 and not engine.progress["completed"]
    meta = json.loads((h.pending / "0001.json").read_text(encoding="utf-8"))
    assert meta["translation_stats"]["cost_usd"] == pytest.approx(.0000675)
    assert meta["translation_recovery_used"] is True
    assert (h.pending / "0001.md").read_text(encoding="utf-8") == FINAL


@pytest.mark.parametrize("operation", ["chapter_glossary_review", "periodic_audit",
                                      "residual_cjk_micro_repair", "strict_terminology_micro_repair"])
def test_unrelated_auxiliary_policy_preserves_recorded_recovery_replay(harness, monkeypatch, operation):
    h = harness
    recorded = run()
    original = engine.current_production_operation_policy()
    changed = replace(original, auxiliary=tuple(replace(node, max_tokens=node.max_tokens + 1)
        if node.operation_id == operation else node for node in original.auxiliary))
    assert changed.identity_sha256() != original.identity_sha256()
    assert recovery.translation_recovery_policy_sha256(changed) == recorded.recovery_policy_sha256
    monkeypatch.setattr(engine, "current_production_operation_policy", lambda: changed)
    replay = run()
    assert replay.initial["replayed"] and replay.recovery["replayed"] and replay.recovery_used
    assert replay.content == FINAL and replay.stats == recorded.stats
    assert replay.pending_provenance() == recorded.pending_provenance()
    assert len(h.calls) == 2


def test_translation_recovery_identity_is_deterministic_and_excludes_global_policy():
    policy = policy_for_model("v41-flash")
    identity = recovery.translation_recovery_policy_identity(policy)
    assert identity == recovery.translation_recovery_policy_identity(policy_for_model("deepseek-flash"))
    assert identity["schema_version"] == 1
    assert identity["revision"] == "translation-recovery-policy-v1"
    assert set(identity) == {"schema_version", "revision", "model_binding", "initial_translation",
        "recovery_translation", "strategy", "saturation_minimum_characters", "saturation_minimum_words"}
    assert identity["model_binding"] == {"model": "deepseek-flash", "version": "DeepSeek-V4.1-Flash"}
    assert identity["initial_translation"] == policy.translation.identity()
    assert identity["recovery_translation"] == policy.translation_recovery.identity()
    unrelated = replace(policy)
    object.__setattr__(unrelated, "revision", "future-global-policy")
    assert recovery.translation_recovery_policy_sha256(unrelated) == recovery.translation_recovery_policy_sha256(policy)
    identity["recovery_translation"]["max_tokens"] = 48000
    assert recovery.translation_recovery_policy_identity(policy)["recovery_translation"]["max_tokens"] == 24000


@pytest.mark.parametrize("change", ["recovery_ceiling", "recovery_thinking", "recovery_reasoning",
    "initial_ceiling", "initial_thinking", "model", "version", "strategy", "revision", "schema", "eligibility"])
def test_translation_recovery_semantic_changes_block_paid_recovery(harness, monkeypatch, change):
    h = harness
    recorded = run()
    original = engine.current_production_operation_policy()
    changed = original
    if change == "recovery_ceiling":
        changed = replace(original, translation_recovery=replace(original.translation_recovery, max_tokens=24001))
    elif change in {"recovery_thinking", "recovery_reasoning"}:
        changed = replace(original, translation_recovery=replace(original.translation_recovery,
            thinking_type="enabled", reasoning_effort="low" if change == "recovery_reasoning" else None))
    elif change == "initial_ceiling":
        changed = replace(original, translation=replace(original.translation, max_tokens=12001))
    elif change == "initial_thinking":
        changed = replace(original, translation=replace(original.translation, thinking_type="enabled"))
    elif change == "model":
        changed = replace(original, model_id="deepseek-v4-pro")
    elif change == "version":
        changed = replace(original, model_version="Different-declared-version")
    else:
        field = {"strategy": "TRANSLATION_RECOVERY_STRATEGY", "revision": "TRANSLATION_RECOVERY_POLICY_REVISION",
                 "schema": "TRANSLATION_RECOVERY_SCHEMA_VERSION", "eligibility": "MINIMUM_SATURATION_WORDS"}[change]
        monkeypatch.setattr(recovery, field, 4 if change == "eligibility" else 2 if change == "schema" else "changed-protocol")
    assert recovery.translation_recovery_policy_sha256(changed) != recorded.recovery_policy_sha256
    monkeypatch.setattr(engine, "current_production_operation_policy", lambda: changed)
    assert engine.process_chapter(1) is False
    assert len(h.calls) == 2 and not list(h.pending.iterdir())


@pytest.mark.parametrize("tamper", ["missing_initial", "missing_recovery", "uncertain_initial", "uncertain_recovery",
    "response_hash", "provider_model", "binding", "model", "version", "chapter", "stage", "accounting",
    "invalid_json", "arbitrary_json", "stable_identity"])
def test_metadata_only_prefix_cannot_select_model_without_exact_proof(harness, monkeypatch, tamper):
    h = harness
    original = engine.atomic_write_text
    def crash(path, value, *args, **kwargs):
        if Path(path) == h.pending / "0001.md":
            raise KeyboardInterrupt
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(engine, "atomic_write_text", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.process_chapter(1)
    assert not (h.pending / "0001.md").exists()
    meta_path = h.pending / "0001.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    slot = "chapter_translation" if tamper.endswith("initial") else "chapter_translation_recovery"
    path = h.journal / f"0001.{slot}.json"
    if tamper.startswith("missing"):
        path.unlink()
    elif tamper.startswith("uncertain") or tamper in {"response_hash", "provider_model", "binding"}:
        record = document(h, slot)
        if tamper.startswith("uncertain"):
            record.update(status="uncertain", response={}, response_sha256="")
        elif tamper == "response_hash":
            record["response_sha256"] = "0" * 64
        elif tamper == "provider_model":
            record["response"]["provider_response_model"] = "deepseek-v4-pro"
            record["response_sha256"] = recovery.response_sha256(record["response"])
            meta["recovery_response_sha256"] = record["response_sha256"]
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
        else:
            record["translation_sha256"] = "0" * 64
        path.write_text(json.dumps(_with_record_hash(record)), encoding="utf-8")
    elif tamper == "stable_identity":
        for kind in ("chapter_translation", "chapter_translation_recovery"):
            record = document(h, kind)
            record["source_sha256"] = "0" * 64
            (h.journal / f"0001.{kind}.json").write_text(json.dumps(_with_record_hash(record)), encoding="utf-8")
    elif tamper == "invalid_json":
        meta_path.write_text("{broken", encoding="utf-8")
    else:
        if tamper == "model":
            meta["translation_model"] = "deepseek-v4-pro"
        elif tamper == "version":
            meta["translation_model_version"] = "forged-version"
        elif tamper == "chapter":
            meta["chapter"] = 2
        elif tamper == "stage":
            meta["stage"] = "translated"
        elif tamper == "accounting":
            meta["translation_stats"]["cost_usd"] += 1
        else:
            meta = {"translation_model": "deepseek-flash"}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    from translation_model_runtime import TranslationModelRuntimeError
    with pytest.raises(TranslationModelRuntimeError):
        resolve_runtime_model(None, h.pending)
    assert len(h.calls) == 2


@pytest.mark.parametrize("requested", ["pro", "deepseek-v4-pro"])
def test_true_fresh_launcher_recovers_metadata_only_prefix_with_zero_sends(harness, monkeypatch, requested):
    import subprocess
    h = harness
    original = engine.atomic_write_text
    def crash(path, value, *args, **kwargs):
        if Path(path) == h.pending / "0001.md":
            raise KeyboardInterrupt
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(engine, "atomic_write_text", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.process_chapter(1)
    assert len(h.calls) == 2 and not (h.pending / "0001.md").exists()
    before = {p.name: p.read_bytes() for p in h.journal.glob("*.json")}
    original_meta = json.loads((h.pending / "0001.json").read_text(encoding="utf-8"))
    script = r"""
import json, sys
from pathlib import Path
from types import SimpleNamespace
import run_production_engine as launcher
import noveltranslator_core as core
inputs = json.loads(sys.stdin.read())
pending = Path(inputs["pending"])
prepared = launcher.prepare_production_runtime(
    inputs["requested"], pending, provider_selection="direct_deepseek")
assert prepared["runtime"]["requested_model"] == "deepseek-v4-pro"
assert prepared["runtime"]["effective_model"] == "deepseek-flash"
assert prepared["config"].version == "DeepSeek-V4.1-Flash"
m = launcher.load_production_module()
m.MODEL = core.PRODUCTION_TRANSLATION_MODEL
m.ACTIVE_NOVEL_CONFIG = None
m.ACTIVE_PROFILE = SimpleNamespace(id="historical_military", high_priority_categories=[])
m.PAID_REQUEST_JOURNAL_DIR = pending.parent.parent / "data" / "provider_request_journal"
m.PENDING_DIR = pending
m.OUTPUT_DIR = pending.parent / "chapters"
m.chapter_map = {1: inputs["chapter"]}
m.progress = {"completed": [], "last_completed": 0, "next_chapter": 1}
m.system_prompt = "PRIVATE_SYSTEM translation policy"
m.continuity = {"summary": "PRIVATE_CONTINUITY"}
m.glossary = {"entries": {}}
m.relevant_glossary = lambda source: {}
m.glossary_to_text = lambda entries, *, chapter_number: "PRIVATE_GLOSSARY"
m.RUN_LOGGER = None
m.log_event = lambda *args, **kwargs: None
sends = []
def forbidden(**kwargs):
    sends.append(1)
    raise AssertionError("fresh restart must make zero sends")
m.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=forbidden)))
m.wait_for_api_permission = lambda: forbidden()
class DownstreamReached(Exception): pass
def stop_after_pending(*args, **kwargs): raise DownstreamReached
m.canonicalize_locked_high_priority_terms = stop_after_pending
seen = []
original = m.translate_chapter
def capture(chapter):
    result = original(chapter)
    assert result.initial["replayed"] and result.recovery["replayed"] and result.recovery_used
    seen.append(result)
    return result
m.translate_chapter = capture
m.perform_startup_self_check()
try:
    m.process_chapter(1)
except DownstreamReached:
    pass
else:
    raise AssertionError("normal downstream QA boundary was not reached")
assert len(seen) == 1 and not sends
meta = json.loads((pending / "0001.json").read_text(encoding="utf-8"))
assert meta["stage"] == "translated" and meta["finish_reason"] == "stop"
assert meta["translation_stats"] == inputs["stats"]
assert (pending / "0001.md").read_text(encoding="utf-8") == seen[0].content
print(json.dumps({"effective_model": prepared["config"].model,
                  "version": prepared["config"].version, "sends": len(sends)}))
"""
    child = subprocess.run([sys.executable, "-c", script], input=json.dumps({"pending": str(h.pending),
        "requested": requested, "chapter": CHAPTER, "stats": original_meta["translation_stats"]}),
        text=True, capture_output=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
    assert json.loads(child.stdout.splitlines()[-1]) == {"effective_model": "deepseek-flash",
        "version": "DeepSeek-V4.1-Flash", "sends": 0}
    assert (h.pending / "0001.md").read_text(encoding="utf-8") == FINAL
    assert PARTIAL not in (h.pending / "0001.md").read_text(encoding="utf-8")
    assert {p.name: p.read_bytes() for p in h.journal.glob("*.json")} == before
    assert len(h.calls) == 2


def test_prefix_loader_import_has_no_filesystem_or_credentials_io(monkeypatch):
    import production_translation_recovery_prefix as prefix
    source = Path(prefix.__file__).read_text(encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("prefix helper import must be inert")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import os
    monkeypatch.setattr(os, "getenv", forbidden)
    exec(compile(source, prefix.__file__, "exec"), {})
