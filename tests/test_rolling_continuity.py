"""Synthetic transitions through real request/journal/checkpoint wiring.

The fake provider is an explicit semantic oracle. These tests prove that exact
rolling inputs and compliant revisions survive the engine, not LLM comprehension.
"""
import copy
import json

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from production_failure_classification import LocalPersistenceError
from production_paid_request_journal import PaidRequestJournalError
from production_rolling_continuity import saved_continuity_baseline
from production_saved_checkpoint_identity import SavedCheckpointIdentityError
from production_state_extraction_checkpoint import run_state_extraction_checkpoint
from production_reviewed_state import finalize_reviewed_chapter_state
from test_production_state_recovery import harness, response
from test_post_guhuo_hardening import state_prompt
from test_production_goat_runtime import engine, zero_network


CASES = [
    ("survival", "Lin's left arm is injured.", "Another traveler buys rice.",
     ("另一位旅客买米。", "另一位旅客買米。", "다른 여행자가 쌀을 산다."),
     "Lin's left arm is injured. Another traveler has bought rice.", "left arm is injured"),
    ("superseded", "Lin lives in the eastern inn.", "Lin moves to the western inn.",
     ("林搬到了西客栈。", "林搬到了西客棧。", "린은 서쪽 여관으로 이사했다."),
     "Lin now lives in the western inn.", "eastern inn"),
    ("resolved", "Lin owes Wei ten coins; repayment is unresolved.", "Lin repays all ten coins.",
     ("林还清了十枚钱。", "林還清了十枚錢。", "린은 열 닢을 모두 갚았다."),
     "Lin's debt to Wei is settled; their restored trust endures.", "repayment is unresolved"),
    ("offer", "Wei offered Lin lodging; acceptance is not shown.", "Wei talks about lodging.",
     ("魏又提起留宿的提议。", "魏又提起留宿的提議。", "웨이는 숙박 제안을 다시 언급했다."),
     "Wei offered Lin lodging; acceptance is not shown.", "acceptance is not shown"),
    ("plan", "Lin intends to visit Wei; completion is unresolved.", "Lin is still preparing.",
     ("林仍在准备去见魏。", "林仍在準備去見魏。", "린은 웨이를 만나러 갈 준비를 계속한다."),
     "Lin intends to visit Wei; completion is unresolved.", "intends"),
    ("belief", "Wei believes an attack is likely; this is unconfirmed.", "Wei repeats his suspicion.",
     ("魏仍猜测可能有袭击。", "魏仍猜測可能有襲擊。", "웨이는 공격이 있을지도 모른다고 추측한다."),
     "Wei believes an attack is likely; this is unconfirmed.", "unconfirmed"),
    ("ambiguous", "Lin was reported alive.", "Conflicting rumors say Lin died or escaped.",
     ("有人说林死了，另有人说他逃了，尚未证实。", "有人說林死了，另有人說他逃了，尚未證實。", "린이 죽었다거나 달아났다는 상반된 소문은 확인되지 않았다."),
     "Lin's fate is uncertain: conflicting unconfirmed rumors report death or escape.", "uncertain"),
]


def _input(body):
    text = body["messages"][1]["content"]
    baseline = json.loads(text.split("PRIOR COMMITTED CONTINUITY (JSON):\n", 1)[1]
                          .split("\nCHINESE SOURCE:", 1)[0])
    return baseline, text


def _finalize(chapter, baseline, state):
    finalize_reviewed_chapter_state(chapter_number=chapter, eligible_candidates=[],
        review_result={"decisions": [], "issues": []}, prefilter_rejected=[],
        state=state, qa={"issues": []}, continuity=baseline,
        issues_data={"issues": []}, apply_term_review=lambda *a: {"rejected": []},
        replace_chapter_issues=lambda existing, chapter, items: items)


@pytest.mark.parametrize("language,index", [("zh", 0), ("zh-Hant", 1), ("ko", 2)])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_two_chapter_revision_preserves_modalities_and_source_evidence(
        harness, monkeypatch, language, index, case):
    kind, old, accepted, sources, revised, marker = case
    monkeypatch.setattr(production, "STATE_PROMPT", state_prompt())
    baseline = {"latest_chapter": 0, "summary": "Earlier memory."}
    sends = []

    def semantic_oracle(**body):
        prior, text = _input(body)
        sends.append(body)
        if len(sends) == 1:
            assert prior == baseline
            return response(json.dumps({"new_terms": [], "continuity_summary": old, "issues": []}))
        # The second request must carry the fact established by N, not blank
        # memory or N+1's output. The source and accepted text are independent.
        assert prior == {"latest_chapter": 1, "summary": old}
        assert f"CHINESE SOURCE:\n{sources[index]}\nFINAL ENGLISH:\n" in text
        assert text.endswith(accepted)
        assert body["messages"][0]["content"] == state_prompt()
        return response(json.dumps({"new_terms": [], "continuity_summary": revised, "issues": []}))

    monkeypatch.setattr(production, "send_api_request_once", semantic_oracle)
    first, _ = production.extract_state(1, "Previous source.", "Previous English.", prior_continuity=baseline)
    _finalize(1, baseline, first)
    # Deliberate English overstatement: current source still wins in the oracle.
    if kind == "offer":
        accepted = "Lin accepted and now resides with Wei."
    second, _ = production.extract_state(2, sources[index], accepted, prior_continuity=baseline)
    _finalize(2, baseline, second)
    assert baseline == {"latest_chapter": 2, "summary": revised}
    if kind in {"superseded", "resolved"}:
        assert marker not in baseline["summary"]
    else:
        assert marker in baseline["summary"]
    assert len(sends) == 2  # One existing extraction per chapter, no new review.


@pytest.mark.parametrize("change", ["prior", "source", "translation", "prompt", "policy"])
def test_changed_request_blocks_saved_response_without_sending(harness, monkeypatch, change):
    harness.queue.append(response())
    harness.run()
    identity = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    assert identity["prior_continuity"] == production.continuity
    assert len(identity["prior_continuity_sha256"]) == 64
    source, translation = "王子回家。", "The prince returns home."
    if change == "prior":
        production.continuity["summary"] += "\n Exact new content. "
    elif change == "source":
        source += "再见。"
    elif change == "translation":
        translation += " Farewell."
    elif change == "prompt":
        monkeypatch.setattr(production, "STATE_PROMPT", "Changed rolling policy.")
    else:
        from production_state_extraction import StateExtractionPolicy
        monkeypatch.setattr(production, "STATE_EXTRACTION_POLICY", StateExtractionPolicy(initial_tokens=5000))
    new = production.current_state_checkpoint_identity(1, source, translation)
    assert identity["identity_sha256"] != new["identity_sha256"]
    with pytest.raises(PaidRequestJournalError):
        production.extract_state(1, source, translation)
    assert len(harness.calls) == 1
    assert len(list(harness.journal.glob("*.json"))) == 1


def _checkpoint(harness, meta, identity, persist):
    return run_state_extraction_checkpoint(meta=meta, checkpoint_identity=identity,
        extract_state=harness.run, reprice_saved_stats=lambda value: value,
        zero_stats=dict, persist_meta=persist, validate_saved_state=production.validate_state)


@pytest.mark.parametrize("crash", ["response_recorded", "state_extracted"])
def test_crash_before_commit_replays_with_exact_persisted_baseline(harness, crash):
    identity = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    original = copy.deepcopy(production.continuity)
    durable = []

    def persist(meta):
        if crash == "response_recorded" and meta["stage"] == "state_extracted":
            raise OSError("crash before state checkpoint")
        durable.append(copy.deepcopy(meta))

    harness.queue.append(response())
    if crash == "response_recorded":
        with pytest.raises(LocalPersistenceError):
            _checkpoint(harness, {"stage": "translated"}, identity, persist)
    else:
        _checkpoint(harness, {"stage": "translated"}, identity, persist)
    saved = copy.deepcopy(durable[-1])
    assert saved["state_checkpoint_identity"]["prior_continuity"] == original
    assert production.continuity == original
    result = _checkpoint(harness, saved, identity, lambda m: durable.append(copy.deepcopy(m)))
    assert result["state"]["continuity_summary"] == "The prince returns home."
    assert len(harness.calls) == 1  # Replay is local in both crash windows.
    if crash == "response_recorded":
        assert any(fields.get("replayed_paid_response") for _, fields in harness.events)


def test_request_snapshot_write_failure_prevents_transport(harness):
    identity = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    meta = {"stage": "translated"}
    writes = []
    def fail(meta):
        writes.append(copy.deepcopy(meta))
        raise OSError("snapshot write failed")
    with pytest.raises(LocalPersistenceError, match="request checkpoint"):
        _checkpoint(harness, meta, identity, fail)
    assert harness.calls == []
    assert not harness.journal.exists()
    assert meta == {"stage": "translated"}
    # Same object retry must attempt persistence again before any send.
    with pytest.raises(LocalPersistenceError, match="request checkpoint"):
        _checkpoint(harness, meta, identity, fail)
    assert len(writes) == 2
    assert harness.calls == []


@pytest.mark.parametrize("stage", ["translated", "state_extracted", "reviewed"])
def test_old_checkpoint_contract_blocks_without_automatic_replacement(harness, stage):
    identity = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    old = copy.deepcopy(identity)
    old["version"] = "saved-state-checkpoint-v1"
    old.pop("prior_continuity")
    old.pop("prior_continuity_sha256")
    meta = {"stage": stage, "state_checkpoint_identity": old}
    if stage != "translated":
        meta["state"] = {"new_terms": [], "continuity_summary": "old output", "issues": []}
    with pytest.raises(SavedCheckpointIdentityError):
        _checkpoint(harness, meta, identity, lambda m: pytest.fail("must not persist"))
    with pytest.raises(SavedCheckpointIdentityError):
        saved_continuity_baseline(old)
    assert harness.calls == []


def test_snapshot_preserves_exact_whitespace_and_is_detached(harness):
    production.continuity.update(summary="  Injury persists.\nOffer unresolved.\n", annotation="fixture")
    identity = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    original = copy.deepcopy(production.continuity)
    production.continuity["summary"] = "Changed"
    assert saved_continuity_baseline(identity) == original
    assert identity["prior_continuity"]["summary"] == "  Injury persists.\nOffer unresolved.\n"


def test_stale_snapshot_even_before_result_blocks_extraction(harness):
    old = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    production.continuity["summary"] = "Different committed memory."
    new = production.current_state_checkpoint_identity(1, "王子回家。", "The prince returns home.")
    with pytest.raises(SavedCheckpointIdentityError):
        _checkpoint(harness, {"stage": "translated", "state_checkpoint_identity": old}, new,
                    lambda m: pytest.fail("must not persist"))
    assert harness.calls == []


@pytest.mark.parametrize("status", ["started", "uncertain", "response_recorded"])
def test_pre_rolling_paid_journal_never_gets_a_replacement_operation(harness, status):
    from production_paid_request_journal import _updated_record
    old_messages = [
        {"role": "system", "content": "Extract state."},
        {"role": "user", "content": "CHAPTER: 1\nEXISTING GLOSSARY TERMS:\n[]\nCHINESE SOURCE:\n王子回家。\nFINAL ENGLISH:\nThe prince returns home."},
    ]
    harness.queue.append(response())
    production.journaled_chat_request("state_extraction", 1, "王子回家。",
        {"model": production.MODEL, "messages": old_messages,
         **production.operation_request_controls("state_extraction")},
        translation_text="The prince returns home.")
    path = harness.journal / "0001.state_extraction.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if status != "response_recorded":
        production.atomic_write_json(path, _updated_record(record, status=status, response={}, response_sha256=""))
    before = path.read_bytes()
    with pytest.raises(PaidRequestJournalError):
        harness.run()
    assert path.read_bytes() == before
    assert len(harness.calls) == 1
    assert len(list(harness.journal.glob("*.json"))) == 1


def test_goat_rolling_state_response_replays_locally_and_binds_baseline(engine):
    _, module, sends, client, _, _, _ = engine
    from test_production_goat_runtime import response as goat_response
    prior = copy.deepcopy(module.continuity)
    summary = "Prior durable injury persists; Wei offered lodging, acceptance unconfirmed."
    def create(**body):
        sends.append(copy.deepcopy(body))
        assert _input(body)[0] == prior
        return goat_response(content=json.dumps({"new_terms": [], "continuity_summary": summary, "issues": []}))
    client.chat.completions.create = create
    source, translation = "魏提出留宿。", "Wei offered lodging."
    first = module.extract_state(1, source, translation, prior_continuity=prior)
    second = module.extract_state(1, source, translation, prior_continuity=prior)
    assert first == second
    assert len(sends) == 1

    assert sends[0]["reasoning_effort"] == "low"
    assert "max_completion_tokens" not in sends[0] and "max_tokens" not in sends[0]
    identity = module.current_state_checkpoint_identity(1, source, translation)
    assert identity["prior_continuity"] == prior
    assert identity["provider_identity"]["wire_model"] == "deepseek/deepseek-v4.1-flash"
    with pytest.raises(PaidRequestJournalError):
        module.extract_state(1, source, translation, prior_continuity={**prior, "summary": "changed"})
    assert len(sends) == 1


@pytest.mark.parametrize("change", ["summary", "chapter", "snapshot"])
def test_read_only_goat_recovery_planner_blocks_continuity_drift(engine, change):
    from test_goat_glossary_review_recovery import incident, plan
    project, module, sends, _ = incident(engine)
    assert plan(project)["status"] == "READY"
    path = project.pending_dir / "0001.json" if change == "snapshot" else project.continuity_file
    value = json.loads(path.read_text(encoding="utf-8"))
    if change == "summary":
        value["summary"] = "Different committed memory."
    elif change == "chapter":
        value["latest_chapter"] = 1
    else:
        value["state_checkpoint_identity"]["prior_continuity"]["summary"] = "Tampered original."
    module.atomic_write_json(path, value)
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        plan(project)
    assert path.read_bytes() == before
    assert len(sends) == 3  # Synthetic incident only; planning sends nothing.
