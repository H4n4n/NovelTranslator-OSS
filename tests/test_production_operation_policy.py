"""Zero-provider behavior and durable identity regressions for operation policy."""
import builtins
import copy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest

import production_operation_policy as policy
from production_state_extraction import DEFAULT_STATE_POLICY, StateExtractionPolicy
import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine

EXPECTED_CEILINGS = {
    "chapter_translation": 12000, "chapter_translation_recovery": 24000,
    "state_extraction": 4000, "state_extraction_recovery": 8000, "state_json_repair": 5000,
    "chapter_glossary_review": 4000, "chapter_glossary_review_json_repair": 5000,
    "chapter_glossary_review_schema_correction": 2000,
    "chapter_glossary_review_semantic_correction": 2000,
    "chapter_glossary_review_semantic_correction_retry": 2000,
    "periodic_audit": 4000, "periodic_audit_json_repair": 4000,
    "periodic_audit_semantic_correction": 4000,
    "residual_cjk_micro_repair": 800, "residual_cjk_micro_repair_correction": 800,
    "strict_terminology_micro_repair": 800, "strict_terminology_micro_repair_correction": 800,
    "glossary_candidate_conflict_adjudication": 800, "generic_json_repair": 5000,
}


def test_complete_policy_identity_is_deterministic_and_deeply_immutable():
    first = policy.policy_for_model("v41-flash")
    second = policy.policy_for_model("deepseek-flash")
    assert first == second
    assert first.identity() == second.identity()
    assert first.identity_sha256() == second.identity_sha256()
    assert first.state is DEFAULT_STATE_POLICY
    assert first.translation_recovery.trigger == "durable_length_response"
    assert {p.operation_id: p.max_tokens for p in first.operations()} == EXPECTED_CEILINGS
    with pytest.raises(FrozenInstanceError):
        first.model_id = "other"
    with pytest.raises(FrozenInstanceError):
        first.auxiliary[0].max_tokens = 1
    with pytest.raises(FrozenInstanceError):
        first.state.initial_tokens = 1
    identity = first.identity()
    identity["operations"][0]["max_tokens"] = 1
    controls = first.for_operation("chapter_translation").request_controls()
    controls["extra_body"]["thinking"]["type"] = "enabled"
    assert first == second
    assert first.identity_sha256() == second.identity_sha256()
    assert "prices" not in json.dumps(first.identity())
    assert first.identity()["model_binding"] == {
        "model": "deepseek-flash", "version": "DeepSeek-V4.1-Flash"
    }


@pytest.mark.parametrize("operation,ceiling", EXPECTED_CEILINGS.items())
def test_exact_current_request_controls(operation, ceiling):
    request = policy.policy_for_model("v41-flash").for_operation(operation)
    assert request.request_controls() == {
        "max_tokens": ceiling, "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert request.reasoning_effort is None
    assert request.max_attempts == 1


@pytest.mark.parametrize("overrides", [
    {"max_tokens": 0}, {"max_tokens": -1}, {"max_tokens": True}, {"max_tokens": 1.5},
    {"reasoning_effort": "low"}, {"reasoning_effort": "high"},
    {"reasoning_effort": "medium"}, {"thinking_type": "invalid"},
    {"max_attempts": 2}, {"mode": "recovery"},
    {"mode": "repair", "parent_operation": [], "trigger": "error"},
    {"mode": "repair", "parent_operation": "chapter_translation", "trigger": []},
])
def test_illegal_controls_fail_closed(overrides):
    values = {"operation_id": "chapter_translation", "max_tokens": 12000, **overrides}
    with pytest.raises(ValueError):
        policy.OperationRequestPolicy(**values)


def test_unknown_operations_models_and_invalid_rosters_fail_closed():
    production = policy.policy_for_model()
    with pytest.raises(ValueError, match="unknown production operation"):
        production.for_operation("benchmark_generation")
    for model in ("gpt-5.5", "qwen", "glm", "gemini"):
        with pytest.raises(ValueError, match="unsupported translation model"):
            policy.policy_for_model(model)
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(production, auxiliary=list(production.auxiliary))
    with pytest.raises(ValueError, match="roster"):
        replace(production, auxiliary=production.auxiliary[:-1])
    with pytest.raises(ValueError, match="cyclic"):
        replace(production, auxiliary=(replace(production.auxiliary[0], mode="repair",
                parent_operation="chapter_glossary_review", trigger="test"),
                *production.auxiliary[1:]))
    with pytest.raises(ValueError, match="immutable strings"):
        replace(production, model_id=[])


def test_pro_translation_recovery_remains_disabled_but_policy_can_describe_followup():
    production = policy.policy_for_model("pro")
    recovery = policy.OperationRequestPolicy("chapter_translation_recovery", 16000,
        mode="recovery", parent_operation="chapter_translation", trigger="reviewed_length_recovery")
    future = replace(production, translation_recovery=recovery)
    assert future.for_operation("chapter_translation_recovery") is recovery
    assert future.identity_sha256() != production.identity_sha256()
    assert production.translation_recovery is None
    with pytest.raises(ValueError, match="escalation"):
        replace(production, translation_recovery=replace(recovery, max_tokens=12000))


def test_state_is_the_single_authority_and_escalation_is_required():
    state = StateExtractionPolicy(recovery_tokens=9000)
    production = policy.policy_for_model("v41-flash", state=state)
    assert production.state is state
    assert production.identity()["state_extraction_policy"] == state.identity()
    assert production.for_operation("state_extraction_recovery").max_tokens == 9000
    with pytest.raises(ValueError, match="larger"):
        StateExtractionPolicy(recovery_tokens=4000)


def test_import_is_inert(monkeypatch):
    source = Path(policy.__file__).read_text(encoding="utf-8")
    module = ModuleType("_test_inert_operation_policy")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    def forbidden(*args, **kwargs):
        raise AssertionError("operation policy import must not perform IO or access credentials")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import os
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os, "environ", {})
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"openai", "dotenv"}:
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    exec(compile(source, policy.__file__, "exec"), module.__dict__)
    assert module.policy_for_model("v41-flash").identity() == policy.policy_for_model("v41-flash").identity()


@pytest.fixture
def transport(monkeypatch, tmp_path):
    calls = []
    events = []
    stats = engine.zero_stats()
    stats.update(pricing_period="off_peak", cache_hit_rate_per_million=0.003,
                 cache_miss_rate_per_million=0.15, output_rate_per_million=0.6)
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"decisions": [], "issues": []}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    def send(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return response
    monkeypatch.setattr(engine, "MODEL", "deepseek-flash")
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_MAX_TOKENS", 12000)
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_THINKING", "disabled")
    monkeypatch.setattr(engine, "STATE_EXTRACTION_POLICY", DEFAULT_STATE_POLICY)
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", None)
    monkeypatch.setattr(engine, "ACTIVE_PROFILE", SimpleNamespace(id="historical_military"))
    monkeypatch.setattr(engine, "RUN_LOGGER", SimpleNamespace(emit=lambda *args, **kw: None))
    monkeypatch.setattr(engine, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(engine, "call_api_with_retries", send)
    monkeypatch.setattr(engine, "send_api_request_once", send)
    monkeypatch.setattr(engine, "wait_for_api_permission", lambda: None)
    monkeypatch.setattr(engine, "pricing_snapshot", lambda: {})
    monkeypatch.setattr(engine, "usage_and_cost", lambda _: dict(stats))
    monkeypatch.setattr(engine, "usage_and_cost_for_pricing", lambda *args: dict(stats))
    def journal(operation, chapter, source, kwargs, **extra):
        send(**kwargs)
        return {"response": {"content": "Synthetic translation.", "finish_reason": "stop",
                             "stats": dict(stats)}, "replayed": False, "api_called": True, "request_sha256": "fake", "operation_id": "fake"}
    monkeypatch.setattr(engine, "journaled_chat_request", journal)
    return SimpleNamespace(calls=calls, events=events, response=response, stats=stats)


@pytest.mark.parametrize("function,operation,kind", [
    ("request_residual_cjk_micro_repair", "residual_cjk_micro_repair", "initial"),
    ("request_residual_cjk_micro_repair", "residual_cjk_micro_repair_correction", "corrective_retry"),
    ("request_strict_terminology_micro_repair", "strict_terminology_micro_repair", "initial"),
    ("request_strict_terminology_micro_repair", "strict_terminology_micro_repair_correction", "bounded_response_correction"),
    ("request_glossary_candidate_conflict_adjudication", "glossary_candidate_conflict_adjudication", "initial"),
])
def test_actual_micro_and_adjudication_payloads(transport, function, operation, kind):
    getattr(engine, function)({"system_prompt": "Synthetic system.", "user_prompt": "Synthetic user.",
                              "request_kind": kind}, chapter_number=1, source_text="synthetic source",
                              translation_text="Synthetic English.", operation_id=operation)
    assert transport.calls == [{"model": "deepseek-flash", "messages": [
        {"role": "system", "content": "Synthetic system."},
        {"role": "user", "content": "Synthetic user."}],
        "max_tokens": 800, "extra_body": {"thinking": {"type": "disabled"}}}]
    assert transport.events[0][1]["operation_kind"] == operation
    assert "Synthetic" not in json.dumps(transport.events)


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_actual_translation_payload_and_historical_journal_identity(transport, monkeypatch, model):
    monkeypatch.setattr(engine, "MODEL", model)
    monkeypatch.setattr(engine, "system_prompt", "Synthetic translation policy.")
    monkeypatch.setattr(engine, "continuity", {"summary": "Synthetic continuity."})
    monkeypatch.setattr(engine, "relevant_glossary", lambda source: {})
    monkeypatch.setattr(engine, "glossary_to_text", lambda entries, *, chapter_number: "Synthetic glossary.")
    engine.translate_chapter({"chapter": 1, "text": "Synthetic source.", "title": "Test"})
    actual = transport.calls[0]
    historical = {"model": model, "messages": copy.deepcopy(actual["messages"]),
                  "max_tokens": 12000, "extra_body": {"thinking": {"type": "disabled"}}}
    assert actual == historical
    assert "reasoning_effort" not in actual
    assert engine._chat_request_identity("chapter_translation", 1, "Synthetic source.", actual) == (
        engine._chat_request_identity("chapter_translation", 1, "Synthetic source.", historical))
    assert all("Synthetic" not in json.dumps(fields) for _, fields in transport.events)


@pytest.mark.parametrize("operation", ["generic_json_repair", "chapter_glossary_review_json_repair"])
def test_actual_generic_repair_payload(transport, operation):
    engine.repair_json_content("Test", "{broken}", "{schema}", operation=operation)
    assert transport.calls == [{"model": "deepseek-flash", "messages": [
        {"role": "system", "content": engine.GENERIC_JSON_REPAIR_PROMPT + "\n\nRequired structure:\n{schema}"},
        {"role": "user", "content": "Repair this malformed JSON:\n\n{broken}"}],
        "max_tokens": 5000, "extra_body": {"thinking": {"type": "disabled"}}}]


@pytest.mark.parametrize("function,operation", [
    ("request_periodic_audit_chunk", "periodic_audit"),
    ("repair_periodic_audit_chunk", "periodic_audit_json_repair"),
    ("correct_periodic_audit_chunk_semantics", "periodic_audit_semantic_correction"),
])
def test_actual_audit_payload_controls(transport, monkeypatch, function, operation):
    metadata = {"chunk_id": "fake", "chunk_index": 1, "total_chunks": 1,
                "model_candidate_ids": ["c01"], "family_context": {},
                "audit_window": "1-1", "candidate_sources": {}, "authorized_fields": {},
                "allowed_merge_targets": [], "operation_id": "fake"}
    if function == "request_periodic_audit_chunk":
        monkeypatch.setattr(engine, "AUDIT_MODEL_PROMPT", "Synthetic audit policy.")
        engine.request_periodic_audit_chunk(1, 1, "Synthetic glossary.", [], metadata)
    elif function == "repair_periodic_audit_chunk":
        engine.repair_periodic_audit_chunk("{broken}", metadata)
    else:
        engine.correct_periodic_audit_chunk_semantics({}, [], metadata)
    actual = transport.calls[0]
    assert actual["model"] == "deepseek-flash"
    assert actual["max_tokens"] == 4000
    assert actual["extra_body"] == {"thinking": {"type": "disabled"}}
    assert set(actual) == {"model", "messages", "max_tokens", "extra_body"}
    assert any(fields.get("operation_kind") == operation for _, fields in transport.events)


def test_review_checkpoint_identity_preserves_historical_controls(transport, monkeypatch):
    captured = []
    monkeypatch.setattr(engine, "glossary", {"entries": {}})
    monkeypatch.setattr(engine, "relevant_glossary_for_entries", lambda *args: {})
    monkeypatch.setattr(engine, "glossary_to_text", lambda entries, *, chapter_number: "")
    monkeypatch.setattr(engine, "TERM_REVIEW_MODEL_PROMPT", "Synthetic review policy.")
    original = engine.build_review_checkpoint_identity
    def capture(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(engine, "build_review_checkpoint_identity", capture)
    actual = engine.current_review_checkpoint_identity(1, "Synthetic source.", "Synthetic translation.", [])
    baseline = {**captured[0], "max_original_tokens": 4000,
                "max_correction_tokens": 2000, "thinking_type": "disabled"}
    assert actual == original(**baseline)


@pytest.mark.parametrize("malformed_original", [False, True])
def test_actual_review_original_repair_and_all_correction_controls(
    transport, monkeypatch, malformed_original
):
    monkeypatch.setattr(engine, "glossary", {"entries": {}})
    monkeypatch.setattr(engine, "relevant_glossary", lambda source: {})
    monkeypatch.setattr(engine, "glossary_to_text", lambda entries, *, chapter_number: "Synthetic glossary.")
    monkeypatch.setattr(engine, "TERM_REVIEW_MODEL_PROMPT", "Synthetic review policy.")
    monkeypatch.setattr(engine, "build_chapter_review_schema_correction_prompt",
                        lambda **kwargs: ("Synthetic schema system.", "Synthetic schema user."))
    monkeypatch.setattr(engine, "build_chapter_review_semantic_correction_prompt",
                        lambda **kwargs: ("Synthetic semantic system.", "Synthetic semantic user."))
    if malformed_original:
        count = 0
        def send(**kwargs):
            nonlocal count
            count += 1
            transport.calls.append(copy.deepcopy(kwargs))
            response = copy.deepcopy(transport.response)
            response.choices[0].message.content = "{broken}" if count == 1 else '{"decisions": [], "issues": []}'
            return response
        monkeypatch.setattr(engine, "call_api_with_retries", send)
    def run(**kwargs):
        metadata = {"model_candidate_ids": ["c01"], "operation_id": "synthetic",
                    "model_candidates": [], "candidate_sources": {}}
        kwargs["request_original"]([], metadata)
        for correction_class, attempt in (("model_schema", 1), ("semantic", 1), ("semantic", 2)):
            kwargs["request_correction"]({}, [], {**metadata, "correction_class": correction_class,
                                                  "correction_attempt": attempt})
        return {"review_result": {}, "stats": transport.stats,
                "reused_original": False, "reused_correction": False}
    monkeypatch.setattr(engine, "run_chapter_glossary_review", run)
    engine.review_new_terms(1, "Synthetic source.", "Synthetic translation.", [{"candidate_id": "c01"}])
    assert [call["max_tokens"] for call in transport.calls] == (
        [4000, 5000, 2000, 2000, 2000] if malformed_original else [4000, 2000, 2000, 2000])
    assert all(call["model"] == "deepseek-flash" for call in transport.calls)
    assert all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in transport.calls)
    assert all(set(call) == {"model", "messages", "max_tokens", "extra_body"} for call in transport.calls)
    operations = [fields["operation_kind"] for event, fields in transport.events
                  if event == "production_operation_policy"]
    expected = ["chapter_glossary_review"]
    if malformed_original:
        expected.append("chapter_glossary_review_json_repair")
    expected += ["chapter_glossary_review_schema_correction",
                 "chapter_glossary_review_semantic_correction",
                 "chapter_glossary_review_semantic_correction_retry"]
    assert operations == expected


def test_runtime_unknown_operation_and_ceiling_mismatch_never_reach_transport(transport):
    with pytest.raises(ValueError, match="unknown production operation"):
        engine.operation_request_controls("unknown")
    with pytest.raises(ValueError, match="disagrees"):
        engine.operation_request_controls("state_extraction", ceiling=3000)
    assert transport.calls == []
    assert transport.events == []


def test_current_auxiliary_catalog_preserves_frozen_adapter_ceiling_compatibility():
    # The old adapter's source scanner now sees only the explicit historical
    # 3000 replay ceiling. Prove its unchanged whitelist covers the new catalog
    # too, without modifying the frozen adapter or its contract literals.
    import benchmark_longitudinal_g7j_reasoning_budget as g7j
    recognized = set()
    for operation in g7j.GOAT_AUXILIARY_OPERATIONS:
        recognized.update(g7j._goat_auxiliary_stage1_runtime_accepted_incoming_tokens(operation))
    current = {p.max_tokens for p in policy.policy_for_model().operations()
               if p.operation_id != "chapter_translation"}
    assert current <= recognized
