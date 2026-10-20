import copy
import json
from types import SimpleNamespace

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from production_paid_request_journal import PaidRequestJournalError, PaidRequestUncertainError
from production_state_extraction import StateExtractionError, StateExtractionPolicy, validate_state

STATE = {"new_terms": [], "continuity_summary": "The prince returns home.", "issues": []}
SOURCE = "王子回家。"
TRANSLATION = "The prince returns home."


def response(content=None, finish="stop", tokens=10):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(STATE) if content is None else content),
                                 finish_reason=finish)],
        usage={"prompt_tokens": 20, "completion_tokens": tokens}, id="test-response",
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    calls, events, queue = [], [], []
    monkeypatch.setattr(production, "MODEL", "deepseek-flash")
    monkeypatch.setattr(production, "ACTIVE_NOVEL_CONFIG", None)
    monkeypatch.setattr(production, "ACTIVE_PROFILE", SimpleNamespace(id="historical_military"))
    monkeypatch.setattr(production, "glossary", {"entries": {}})
    monkeypatch.setattr(production, "continuity", {"latest_chapter": 0, "summary": "Prior working memory."})
    monkeypatch.setattr(production, "STATE_PROMPT", "Extract state.")
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy())
    monkeypatch.setattr(production, "PAID_REQUEST_JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(production, "wait_for_api_permission", lambda: None)
    monkeypatch.setattr(production, "pricing_snapshot", lambda: {})
    monkeypatch.setattr(production, "usage_and_cost_for_pricing", lambda usage, _: dict(usage))
    monkeypatch.setattr(production, "log_event", lambda event, **fields: events.append((event, fields)))

    def send(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(production, "send_api_request_once", send)
    return SimpleNamespace(calls=calls, events=events, queue=queue,
                           journal=tmp_path / "journal",
                           run=lambda: production.extract_state(1, SOURCE, TRANSLATION))


def test_normal_extraction_policy(harness):
    harness.queue.append(response())
    state, stats = harness.run()
    assert state == STATE
    assert stats["completion_tokens"] == 10
    assert len(harness.calls) == 1
    assert harness.calls[0]["max_tokens"] == 4000
    assert harness.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in harness.calls[0]
    assert harness.events[-2][0] == "state_extraction_accepted"


@pytest.mark.parametrize("content,finish", [("", "length"), ("  \n ", "stop"),
                                           ("```json\n\n```", "stop"),
                                           (json.dumps(STATE), "length")])
def test_saturation_or_blank_recovers_extraction_never_repairs(harness, content, finish):
    harness.queue.extend([response(content, finish, 4000), response(tokens=33)])
    state, stats = harness.run()
    assert state == STATE
    assert [call["max_tokens"] for call in harness.calls] == [4000, 8000]
    assert stats["completion_tokens"] == 4033
    assert sorted(p.name for p in harness.journal.iterdir()) == [
        "0001.state_extraction.json", "0001.state_extraction_recovery.json"]
    records = [json.loads(p.read_text()) for p in harness.journal.iterdir()]
    assert len({r["operation_id"] for r in records}) == 2
    assert any(e == "state_extraction_recovery_triggered" for e, _ in harness.events)


def test_recovery_also_saturates_blocks_and_restart_replays(harness):
    harness.queue.extend([response("", "length", 4000), response("", "length", 8000)])
    for _ in range(2):
        with pytest.raises(StateExtractionError, match="recovery_completion_budget_exhausted"):
            harness.run()
    assert len(harness.calls) == 2
    assert harness.events[-1][1]["health_status"] == "blocked"


def test_recorded_recovery_replays_and_accounting_is_not_duplicated(harness):
    harness.queue.extend([response("", "length", 4000), response(tokens=35)])
    first = harness.run()
    second = harness.run()
    assert first == second
    assert first[1]["completion_tokens"] == 4035
    assert len(harness.calls) == 2
    attempts = [fields for event, fields in harness.events if event == "state_extraction_attempt"]
    assert [fields["replayed_paid_response"] for fields in attempts] == [False, False, True, True]


def test_uncertain_recovery_never_reissues(harness):
    harness.queue.extend([response("", "length"), TimeoutError("ambiguous")])
    with pytest.raises(TimeoutError):
        harness.run()
    with pytest.raises(PaidRequestUncertainError):
        harness.run()
    assert len(harness.calls) == 2
    assert json.loads((harness.journal / "0001.state_extraction_recovery.json").read_text())["status"] == "uncertain"


def test_substantive_malformed_state_uses_one_repair(harness):
    malformed = '{"new_terms": [] "continuity_summary": "The prince returns home.", "issues": []}'
    harness.queue.extend([response(malformed), response(tokens=25)])
    state, stats = harness.run()
    assert state == STATE
    assert [c["max_tokens"] for c in harness.calls] == [4000, 5000]
    assert (harness.journal / "0001.state_json_repair.json").is_file()
    assert not (harness.journal / "0001.state_extraction_recovery.json").exists()
    assert stats["completion_tokens"] == 35
    harness.run()
    assert len(harness.calls) == 2


def test_truncated_issues_local_salvage_survives(harness):
    harness.queue.append(response('{"new_terms": [], "continuity_summary": "The prince returns home.", "issues": ["broken'))
    state, _ = harness.run()
    assert state == STATE
    assert len(harness.calls) == 1
    accepted = next(fields for event, fields in harness.events if event == "state_extraction_accepted")
    assert accepted["local_salvage_used"] is True


@pytest.mark.parametrize("content", ["Sorry, I cannot help.", "{", "[]", "{}",
                                     '{"new_terms": [], "continuity_summary": "", "issues": []}'])
def test_no_semantic_state_cannot_be_fabricated_by_repair(harness, content):
    harness.queue.append(response(content))
    with pytest.raises(StateExtractionError):
        harness.run()
    assert len(harness.calls) == 1
    assert not (harness.journal / "0001.state_json_repair.json").exists()


@pytest.mark.parametrize("repair_response", [response("", "length"), response(" "),
                                             response("{bad"), response(json.dumps({**STATE, "continuity_summary": ""}))])
def test_bad_repair_blocks_without_more_paid_calls(harness, repair_response):
    harness.queue.extend([response('{"continuity_summary": "The prince returns home." "new_terms": []}'), repair_response])
    with pytest.raises(StateExtractionError):
        harness.run()
    assert len(harness.calls) == 2


def test_recovery_identity_mismatch_fails_closed(harness, monkeypatch):
    harness.queue.extend([response("", "length"), response()])
    harness.run()
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(recovery_tokens=9000))
    with pytest.raises(PaidRequestJournalError):
        harness.run()
    assert len(harness.calls) == 2


def test_paid_legacy_initial_response_is_reused_before_recovery(harness, monkeypatch):
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(initial_tokens=3000))
    harness.queue.extend([response("", "length", 3000), response(tokens=15)])
    harness.run()
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy())
    state, stats = harness.run()
    assert state == STATE
    assert len(harness.calls) == 2
    assert stats["completion_tokens"] == 3015


@pytest.mark.parametrize("kwargs", [{"initial_tokens": True}, {"recovery_tokens": 4000},
                                    {"recovery_tokens": 20000}, {"reasoning_effort": "high"},
                                    {"reasoning_effort": "low"}])
def test_invalid_or_unbounded_policy_fails_locally(kwargs):
    with pytest.raises(ValueError):
        StateExtractionPolicy(**kwargs)


def test_reasoning_enabled_policy_is_explicit_low_and_keeps_ceiling():
    policy = StateExtractionPolicy(thinking_type="enabled", reasoning_effort="low")
    assert policy.request_controls(policy.initial_tokens)["reasoning_effort"] == "low"
    assert policy.request_controls(policy.recovery_tokens)["max_tokens"] == 8000


@pytest.mark.parametrize("state", [None, [], {**STATE, "new_terms": {}},
                                   {**STATE, "issues": "bad"}, {**STATE, "continuity_summary": 3},
                                   {**STATE, "new_terms": [None]}])
def test_schema_validation(state):
    with pytest.raises(StateExtractionError):
        validate_state(state)


def test_empty_candidates_are_valid_with_useful_continuity():
    assert validate_state(STATE) == STATE


def test_uncertain_legacy_initial_never_creates_recovery(harness, monkeypatch):
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(initial_tokens=3000))
    harness.queue.append(TimeoutError("ambiguous"))
    with pytest.raises(TimeoutError):
        harness.run()
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy())
    with pytest.raises(PaidRequestUncertainError):
        harness.run()
    assert len(harness.calls) == 1
    assert not (harness.journal / "0001.state_extraction_recovery.json").exists()


def test_corrupt_initial_does_not_open_new_slot(harness):
    harness.queue.append(response())
    harness.run()
    path = harness.journal / "0001.state_extraction.json"
    path.write_text("{}")
    with pytest.raises(PaidRequestJournalError):
        harness.run()
    assert len(harness.calls) == 1


def test_new_checkpoint_identity_binds_recovery_policy(harness, monkeypatch):
    first = production.current_state_checkpoint_identity(1, SOURCE, TRANSLATION)
    assert first["max_tokens"] == 4000
    assert first["extraction_policy"]["recovery_tokens"] == 8000
    monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(recovery_tokens=9000))
    second = production.current_state_checkpoint_identity(1, SOURCE, TRANSLATION)
    assert first["identity_sha256"] != second["identity_sha256"]
    legacy = production.current_state_checkpoint_identity(1, SOURCE, TRANSLATION, legacy_policy=True)
    assert legacy["max_tokens"] == 3000
    assert "extraction_policy" not in legacy


def test_recovery_model_mismatch_is_recorded_and_replay_blocks(harness, monkeypatch):
    from provider_model_identity_runtime import (
        ProviderModelIdentityError, install_provider_model_identity_runtime,
    )
    guarded = SimpleNamespace(
        send_api_request_once=production.send_api_request_once,
        run_journaled_paid_request=production.run_journaled_paid_request,
    )
    install_provider_model_identity_runtime(guarded)
    monkeypatch.setattr(production, "send_api_request_once", guarded.send_api_request_once)
    monkeypatch.setattr(production, "run_journaled_paid_request", guarded.run_journaled_paid_request)
    initial = response("", "length")
    initial.model = "deepseek-flash"
    recovery = response()
    recovery.model = "wrong-model"
    harness.queue.extend([initial, recovery])
    for _ in range(2):
        with pytest.raises(ProviderModelIdentityError, match="mismatch"):
            harness.run()
    assert len(harness.calls) == 2
    saved = json.loads((harness.journal / "0001.state_extraction_recovery.json").read_text())
    assert saved["status"] == "response_recorded"
    assert saved["response"]["provider_response_model"] == "wrong-model"


def test_saved_empty_state_blocks_without_provider_or_pending_mutation(harness):
    from production_state_extraction_checkpoint import run_state_extraction_checkpoint
    identity = production.current_state_checkpoint_identity(1, SOURCE, TRANSLATION)
    meta = {"stage": "state_extracted", "state_checkpoint_identity": identity,
            "state": {**STATE, "continuity_summary": ""}}
    before = copy.deepcopy(meta)
    with pytest.raises(StateExtractionError, match="empty_continuity"):
        run_state_extraction_checkpoint(
            meta=meta, checkpoint_identity=identity, extract_state=harness.run,
            validate_saved_state=validate_state, reprice_saved_stats=lambda stats: stats,
            zero_stats=lambda: {}, persist_meta=lambda _: pytest.fail("must not persist"),
        )
    assert not harness.calls
    assert meta == before


def test_observability_excludes_source_and_visible_state(harness):
    harness.queue.extend([response("", "length"), response()])
    harness.run()
    serialized = json.dumps(harness.events, ensure_ascii=False)
    assert SOURCE not in serialized
    assert TRANSLATION not in serialized
    assert "api_key" not in serialized.lower()
    assert "paid_operation_id" in serialized
    assert "completion_ceiling" in serialized


@pytest.mark.parametrize("finish", ["content_filter", "tool_calls", None])
@pytest.mark.parametrize("content", ["", json.dumps(STATE)])
def test_unexpected_finish_blocks_without_recovery_or_repair(harness, finish, content):
    harness.queue.append(response(content, finish))
    with pytest.raises(StateExtractionError) as caught:
        harness.run()
    assert caught.value.category == "unexpected_finish_reason"
    assert len(harness.calls) == 1
    assert (harness.journal / "0001.state_extraction.json").is_file()
    assert not (harness.journal / "0001.state_extraction_recovery.json").exists()
    assert not (harness.journal / "0001.state_json_repair.json").exists()
    assert not any(event == "state_extraction_recovery_triggered" for event, _ in harness.events)
    # Restart must replay the rejected initial response without another send.
    with pytest.raises(StateExtractionError) as replayed:
        harness.run()
    assert replayed.value.category == "unexpected_finish_reason"
    assert len(harness.calls) == 1
