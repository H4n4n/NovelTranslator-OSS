"""Zero-provider production auxiliary transport and domain restart coverage."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine
from controlled_trial_execute import ControlledTrialBudget, _BudgetedClientProxy
from production_auxiliary_paid_request import AuxiliaryPaidRequestError
from production_paid_request_journal import run_journaled_paid_request
from provider_model_identity_runtime import install_provider_model_identity_runtime
from novel_project import ResidualCJKMicroRepairConfig, load_novel_project
from residual_cjk_micro_repair import perform_residual_cjk_micro_repair, assess_micro_repair_eligibility
from strict_terminology_repair import perform_strict_terminology_micro_repair
from noveltranslator_core import add_accounting_totals_once

SOURCE = "這位嚴苛的主管檢查了新的機器。"
TEXT = "The new machine worked steadily while the嚴苛 supervisor carefully checked every detail of the entire system."
PATCH = {"repairs": [{"region_id": "line-1-region-1", "line_number": 1,
                      "old_text": "the嚴苛 supervisor", "new_text": "the demanding supervisor"}]}
CONFIG = ResidualCJKMicroRepairConfig(True, 12, 3, 3, 1)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    calls, statuses, responses = [], [], []
    budget = ControlledTrialBudget("v41-flash", 10, 1.0)
    journal_dir = tmp_path / "journals"
    def physical(**kwargs):
        started = [json.loads(p.read_text()) for p in journal_dir.glob("*.json")]
        assert any(r["status"] == "started" for r in started)
        assert budget.api_calls == len(calls) + 1
        calls.append(copy.deepcopy(kwargs))
        response = responses.pop(0) if responses else json.dumps(PATCH)
        if callable(response):
            response = response(kwargs)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            response = SimpleNamespace(id="paid-1", model="deepseek-flash",
                choices=[SimpleNamespace(message=SimpleNamespace(content=response), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                      prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=20))
        return response
    client = _BudgetedClientProxy(SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=physical))), budget)
    monkeypatch.setattr(engine, "client", client)
    monkeypatch.setattr(engine, "MODEL", "deepseek-flash")
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_THINKING", "disabled")
    monkeypatch.setattr(engine, "PRODUCTION_TRANSLATION_MAX_TOKENS", 12000)
    monkeypatch.setattr(engine, "PAID_REQUEST_JOURNAL_DIR", journal_dir)
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", SimpleNamespace(id="fixture"))
    monkeypatch.setattr(engine, "ACTIVE_PROFILE", SimpleNamespace(id="infinite_flow"))
    monkeypatch.setattr(engine, "RUN_LOGGER", None)
    monkeypatch.setattr(engine, "wait_for_api_permission", lambda: None)
    monkeypatch.setattr(engine, "pricing_snapshot", lambda: {
        "pricing_period": "off_peak", "pricing_version": "fixture", "request_started_utc": "2026-10-03T00:00:00+00:00",
        "cache_hit_rate_per_million": .003, "cache_miss_rate_per_million": .15, "output_rate_per_million": .6})
    monkeypatch.setattr(engine, "run_journaled_paid_request", run_journaled_paid_request)
    monkeypatch.setattr(engine, "send_api_request_once", lambda **kw: client.chat.completions.create(**kw))
    monkeypatch.setattr(engine, "_noveltranslator_provider_model_identity_runtime_state", None, raising=False)
    original_save = engine.atomic_write_json
    def save(path, record):
        statuses.append(record["status"])
        return original_save(path, record)
    monkeypatch.setattr(engine, "atomic_write_json", save)
    install_provider_model_identity_runtime(engine, now_utc=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc))
    def adapter(operation="residual_cjk_micro_repair", text=TEXT):
        return engine._auxiliary_request_api(operation, 3, SOURCE, text)
    return SimpleNamespace(calls=calls, statuses=statuses, responses=responses, budget=budget,
        adapter=adapter, journal_dir=journal_dir, records=lambda: [json.loads(p.read_text()) for p in sorted(journal_dir.glob("*.json"))])


def request(kind="initial"):
    return {"system_prompt": "Synthetic bounded patch only.", "user_prompt": "Synthetic region.",
            "request_kind": kind}


@pytest.mark.parametrize("operation,kind", [
    ("residual_cjk_micro_repair", "initial"), ("residual_cjk_micro_repair", "corrective_retry"),
    ("strict_terminology_micro_repair", "initial"), ("strict_terminology_micro_repair", "empty_patch_correction"),
    ("glossary_candidate_conflict_adjudication", "initial")])
def test_all_auxiliary_operations_record_then_replay_exact_controls(runtime, operation, kind):
    adapter = runtime.adapter(operation)
    content, stats = adapter(request(kind), operation_id="domain-operation")
    record = runtime.records()[0]
    assert record["operation_kind"] == operation + ("_correction" if kind != "initial" else "")
    assert record["status"] == "response_recorded"
    assert runtime.statuses == ["planned", "started", "response_recorded"]
    assert record["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert record["translation_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert record["novel_id"] == "fixture" and record["profile_id"] == "infinite_flow"
    assert record["response"]["provider_response_model"] == "deepseek-flash"
    assert runtime.calls == [{"model": "deepseek-flash", "messages": [
        {"role": "system", "content": request()["system_prompt"]},
        {"role": "user", "content": request()["user_prompt"]}],
        "max_tokens": 800, "extra_body": {"thinking": {"type": "disabled"}}}]
    before = [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    replay, replay_stats = runtime.adapter(operation)(request(kind), operation_id="domain-operation")
    assert replay == content and replay_stats["cost_usd"] == stats["cost_usd"]
    assert replay_stats["transport_evidence"]["replayed"]
    assert runtime.budget.api_calls == len(runtime.calls) == 1
    assert before == [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]


def test_initial_and_correction_use_distinct_slots_and_identities(runtime):
    adapter = runtime.adapter()
    adapter(request(), operation_id="initial-domain")
    adapter(request("corrective_retry"), operation_id="correction-domain")
    first, second = runtime.records()
    assert len(runtime.calls) == 2 and first["operation_kind"] != second["operation_kind"]
    assert first["operation_id"] != second["operation_id"]


@pytest.mark.parametrize("refusal", ["call", "cost", "controls", "pro", "retired"])
def test_deterministic_refusal_remains_planned(runtime, monkeypatch, refusal):
    if refusal == "call": runtime.budget.max_api_calls = 0
    if refusal == "cost": runtime.budget.max_cost_usd = 0
    if refusal == "controls": monkeypatch.setattr(engine, "operation_request_controls", lambda kind: {"max_tokens": 801, "extra_body": {"thinking": {"type": "disabled"}}})
    if refusal == "pro": monkeypatch.setattr(engine, "MODEL", "deepseek-v4-pro")
    if refusal == "retired": monkeypatch.setattr(engine, "MODEL", "deepseek-v4-flash")
    with pytest.raises(AuxiliaryPaidRequestError) as caught:
        runtime.adapter()(request(), operation_id="refused")
    assert caught.value.transport_evidence["status"] == "planned"
    assert not caught.value.transport_evidence["api_called"]
    assert runtime.records()[0]["status"] == "planned"
    assert runtime.statuses == ["planned"] and not runtime.calls and runtime.budget.api_calls == 0


def test_started_write_failure_releases_lease_without_send(runtime, monkeypatch):
    save = engine.atomic_write_json
    def fail(path, record):
        if record["status"] == "started": raise OSError("fixture persistence failure")
        return save(path, record)
    monkeypatch.setattr(engine, "atomic_write_json", fail)
    with pytest.raises(AuxiliaryPaidRequestError): runtime.adapter()(request(), operation_id="persist")
    assert runtime.records()[0]["status"] == "planned" and not runtime.calls
    assert runtime.budget._active_authorization is None
    monkeypatch.setattr(engine, "atomic_write_json", save)
    runtime.adapter()(request(), operation_id="persist")
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("failure", ["transport", "usage", "normalization"])
def test_ambiguous_transport_or_normalization_blocks_restart(runtime, failure):
    runtime.responses.append(RuntimeError("ambiguous send") if failure == "transport" else
        SimpleNamespace(model="deepseek-flash", choices=[] if failure == "normalization" else [
            SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")], usage=None))
    with pytest.raises(AuxiliaryPaidRequestError) as caught:
        runtime.adapter()(request(), operation_id="uncertain")
    assert caught.value.transport_evidence["status"] == "uncertain"
    before = [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    with pytest.raises(AuxiliaryPaidRequestError): runtime.adapter()(request(), operation_id="uncertain")
    assert len(runtime.calls) == runtime.budget.api_calls == 1
    assert before == [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]


def test_wrong_model_response_stays_paid_and_recorded_on_replay(runtime):
    runtime.responses.append(SimpleNamespace(model="deepseek-v4-pro", choices=[
        SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10)))
    for _ in range(2):
        with pytest.raises(AuxiliaryPaidRequestError) as caught:
            runtime.adapter()(request(), operation_id="wrong-model")
        assert caught.value.transport_evidence["status"] == "response_recorded"
        assert caught.value.paid_stats["cost_usd"] > 0
    assert runtime.records()[0]["response"]["provider_response_model"] == "deepseek-v4-pro"
    assert len(runtime.calls) == 1


class Domain:
    def __init__(self):
        self.meta = {"stage": "translated"}
        self.translation = TEXT
        self.progress = {}
    def persist(self, value): self.meta = copy.deepcopy(value)
    def account(self, operation_id, stats): return add_accounting_totals_once(self.progress, operation_id, stats)
    def run(self, runtime, *, qa=True):
        return perform_residual_cjk_micro_repair(novel_id="fixture", chapter=3, profile_id="infinite_flow",
            target_language="en", source_text=SOURCE, translation=self.translation, meta=self.meta, config=CONFIG,
            relevant_locked_glossary=[], auxiliary_terminology_policy="", request_api=runtime.adapter(),
            persist_meta=self.persist, persist_translation=lambda value: setattr(self, "translation", value),
            account_api_usage=self.account, qa_checker=lambda text: {"commit_allowed": qa})


@pytest.mark.parametrize("mode", ["success", "malformed", "qa", "call", "uncertain"])
def test_residual_domain_preserves_paid_or_definitely_unsent_evidence(runtime, mode):
    domain = Domain()
    if mode == "malformed": runtime.responses.append("not JSON")
    if mode == "call": runtime.budget.max_api_calls = 0
    if mode == "uncertain": runtime.responses.append(RuntimeError("ambiguous transport"))
    result = domain.run(runtime, qa=mode != "qa")
    attempts = next(iter(domain.meta["residual_cjk_micro_repair"]["attempts_by_pending_hash"].values()))
    record = runtime.records()[0]
    if mode == "call":
        assert record["status"] == attempts[0]["status"] == "planned"
        assert attempts[0]["api_called"] is False and not result["api_called"]
        runtime.budget.max_api_calls = 2
        result = domain.run(runtime)
        assert result["status"] == "succeeded" and len(runtime.calls) == 1
    elif mode == "uncertain":
        assert record["status"] == attempts[0]["status"] == "uncertain"
        domain.run(runtime)
        assert len(runtime.calls) == 1
    else:
        assert record["status"] == "response_recorded" and domain.progress["estimated_cost_usd"] > 0
        assert result["status"] == {"success": "succeeded", "malformed": "locally_rejected", "qa": "qa_rejected"}[mode]
        domain.run(runtime, qa=mode != "qa")
        assert len(runtime.calls) == 1


def test_residual_crash_after_paid_response_before_domain_write_replays(runtime):
    domain = Domain()
    persisted = domain.persist
    def crash(value):
        attempt = next(iter(value["residual_cjk_micro_repair"]["attempts_by_pending_hash"].values()))[0]
        if attempt.get("stats"): raise KeyboardInterrupt("crash after paid response")
        persisted(value)
    domain.persist = crash
    with pytest.raises(KeyboardInterrupt): domain.run(runtime)
    assert runtime.records()[0]["status"] == "response_recorded"
    domain.persist = persisted
    result = domain.run(runtime)
    assert result["status"] == "succeeded" and len(runtime.calls) == 1
    assert len(domain.progress["accounted_api_operation_ids"]) == 1


def test_ace_config_and_synthetic_modifier_shape_are_eligible():
    from pathlib import Path
    ace = load_novel_project("ace_evolution", Path(__file__).resolve().parents[1])
    assert ace.config.residual_cjk_micro_repair == CONFIG
    eligible = assess_micro_repair_eligibility(SOURCE, TEXT, CONFIG)
    assert eligible["eligible"] and not eligible["reasons"]
    assert eligible["repair_regions"][0]["anchor_mode"] == "exact_source"


def test_residual_correction_replays_after_crash_before_corrective_domain_record(runtime):
    domain = Domain()
    invalid = copy.deepcopy(PATCH)
    invalid["repairs"][0]["new_text"] = "the嚴苛 supervisor"
    runtime.responses.extend([json.dumps(invalid), json.dumps(PATCH)])
    persist = domain.persist
    def crash(value):
        attempt = next(iter(value["residual_cjk_micro_repair"]["attempts_by_pending_hash"].values()))[0]
        if attempt.get("corrective_retry", {}).get("stats"):
            raise KeyboardInterrupt("corrective domain write crash")
        persist(value)
    domain.persist = crash
    with pytest.raises(KeyboardInterrupt): domain.run(runtime)
    assert len(runtime.calls) == 2
    assert {r["operation_kind"] for r in runtime.records()} == {"residual_cjk_micro_repair", "residual_cjk_micro_repair_correction"}
    domain.persist = persist
    result = domain.run(runtime)
    assert result["status"] == "succeeded" and len(runtime.calls) == 2
    assert len(domain.progress["accounted_api_operation_ids"]) == 2


@pytest.mark.parametrize("mode", ["success", "correction", "malformed", "refusal", "uncertain", "crash_initial", "crash_correction"])
def test_strict_domain_transport_replay_preserves_validation(runtime, mode):
    from noveltranslator_core import translation_commit_qa
    source, text = "他使用鳴鶴拳。", "He used Minghequan at close range in this complete English sentence."
    entries = {"鳴鶴拳": {"english": "Calling Crane Boxing", "category": "technique", "status": "locked",
                      "strict_qa": True, "auto_repair": True, "repair_aliases": ["Minghequan"]}}
    patch = json.dumps({"patches": [{"region_id": hashlib.sha256("鳴鶴拳".encode()).hexdigest()[:10] + "-region-1", "source_term": "鳴鶴拳",
                                   "old_text": "Minghequan", "new_text": "Calling Crane Boxing"}]})
    state = {"meta": {}, "text": text, "progress": {}}
    if mode in {"correction", "crash_correction"}: runtime.responses.append('{"patches": []}')
    runtime.responses.append("not JSON" if mode == "malformed" else RuntimeError("uncertain") if mode == "uncertain" else patch)
    if mode == "refusal": runtime.budget.max_api_calls = 0
    crashed = False
    def persist(value):
        nonlocal crashed
        attempts = next(iter(value["strict_terminology_micro_repair"]["attempts_by_pending_hash"].values()))
        crash_at = 2 if mode == "crash_correction" else 1
        if mode.startswith("crash_") and not crashed and len(attempts) == crash_at and attempts[-1].get("stats"):
            crashed = True
            raise KeyboardInterrupt("strict domain write crash")
        state["meta"] = copy.deepcopy(value)
    def run():
        return perform_strict_terminology_micro_repair(novel_id="fixture", chapter=3, profile_id="infinite_flow",
            target_language="en", source_text=source, translation=state["text"], meta=state["meta"], glossary_entries=entries,
            high_priority_categories=(), auxiliary_terminology_policy="",
            request_api=engine._auxiliary_request_api("strict_terminology_micro_repair", 3, source, text),
            persist_meta=persist, persist_translation=lambda value: state.__setitem__("text", value),
            account_api_usage=lambda op, stats: add_accounting_totals_once(state["progress"], op, stats),
            qa_checker=lambda candidate: translation_commit_qa(source, candidate, entries, ()))
    if mode.startswith("crash_"):
        with pytest.raises(KeyboardInterrupt): run()
        count = len(runtime.calls)
        result = run()
        assert result["status"] == "succeeded" and len(runtime.calls) == count
    else:
        result = run()
        if mode == "refusal":
            attempts = next(iter(state["meta"]["strict_terminology_micro_repair"]["attempts_by_pending_hash"].values()))
            assert attempts[0]["status"] == "planned" and not attempts[0]["api_called"]
            assert not runtime.calls
            runtime.budget.max_api_calls = 2
            result = run()
        if mode == "uncertain":
            assert runtime.records()[0]["status"] == "uncertain"
            run()
            assert len(runtime.calls) == 1
        elif mode == "malformed":
            assert result["status"] == "locally_rejected" and runtime.records()[0]["status"] == "response_recorded"
            run()
            assert len(runtime.calls) == 1
        else:
            assert result["status"] == "succeeded"
            expected = 2 if mode == "correction" else 1
            assert len(runtime.calls) == expected
    if mode in {"correction", "crash_correction"}:
        assert {r["operation_kind"] for r in runtime.records()} == {"strict_terminology_micro_repair", "strict_terminology_micro_repair_correction"}
    if mode != "uncertain":
        assert state["progress"]["estimated_cost_usd"] > 0


@pytest.mark.parametrize("mode", ["success", "malformed", "refusal", "uncertain", "crash"])
def test_adjudication_domain_paid_transport_and_restart(runtime, tmp_path, mode):
    from test_glossary_candidate_auto_resolution import conflict_for, conflict_candidates, config, response_for
    from glossary_candidate_auto_resolution import resolve_candidate_conflicts_automatically
    conflicts = [conflict_for(conflict_candidates())]
    state = {"record": None, "progress": {}}
    def response(kwargs):
        prompt = json.JSONDecoder().raw_decode(kwargs["messages"][1]["content"])[0]
        return json.dumps({"decisions": [{"conflict_id": c["conflict_id"], "selected_proposal_id": c["proposals"][0]["proposal_id"]} for c in prompt["conflicts"]]})
    runtime.responses.append("not JSON" if mode == "malformed" else RuntimeError("uncertain") if mode == "uncertain" else response)
    if mode == "refusal": runtime.budget.max_api_calls = 0
    crashed = False
    def persist(value):
        nonlocal crashed
        if mode == "crash" and not crashed and value.get("stats"):
            crashed = True
            raise KeyboardInterrupt("adjudication domain persistence crash")
        state["record"] = copy.deepcopy(value)
    def run():
        return resolve_candidate_conflicts_automatically(novel_id="fixture", chapter=3,
            pending_identity="1"*64, state_identity="2"*64, conflicts=conflicts, source_text=SOURCE, translation=TEXT,
            existing_glossary={}, config=config(), resolution_path=tmp_path/"resolution.json", recovery_record=state["record"],
            persist_recovery=persist, request_api=runtime.adapter("glossary_candidate_conflict_adjudication"),
            is_operation_accounted=lambda op: op in state["progress"].get("accounted_api_operation_ids", []),
            account_api_usage=lambda op, stats: add_accounting_totals_once(state["progress"], op, stats))
    if mode == "crash":
        with pytest.raises(KeyboardInterrupt): run()
        result = run()
        assert result["status"] == "resolved" and len(runtime.calls) == 1
    else:
        result = run()
        if mode == "refusal":
            assert state["record"]["status"] == "planned" and not result["api_called"]
            assert not runtime.calls
            runtime.budget.max_api_calls = 2
            result = run()
            assert result["status"] == "resolved" and len(runtime.calls) == 1
        elif mode == "uncertain":
            assert state["record"]["status"] == "uncertain"
            run()
            assert len(runtime.calls) == 1
        elif mode == "malformed":
            assert result["status"] == "unresolved" and runtime.records()[0]["status"] == "response_recorded"
            run()
            assert len(runtime.calls) == 1
        else:
            assert result["status"] == "resolved" and len(runtime.calls) == 1
    if mode != "uncertain": assert state["progress"]["estimated_cost_usd"] > 0


def test_recorded_auxiliary_replay_skips_route_and_budget_authorization(runtime, monkeypatch):
    import provider_model_identity_runtime as guard
    adapter = runtime.adapter()
    adapter(request(), operation_id="replay")
    before = [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    monkeypatch.setattr(guard, "assert_translation_request_allowed", lambda *a, **kw: pytest.fail("route authorization on paid replay"))
    monkeypatch.setattr(runtime.budget, "authorize_request", lambda **kw: pytest.fail("budget authorization on paid replay"))
    _, stats = runtime.adapter()(request(), operation_id="replay")
    assert stats["transport_evidence"]["replayed"] and len(runtime.calls) == 1
    assert before == [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]


@pytest.mark.parametrize("change", ["request", "pending", "source", "model"])
def test_exact_domain_slot_mismatch_cannot_create_a_replacement_request(runtime, monkeypatch, change):
    runtime.adapter()(request(), operation_id="bound")
    before = [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    modified = request()
    if change == "request": modified["user_prompt"] += " changed"
    if change == "model": monkeypatch.setattr(engine, "MODEL", "deepseek-v4-pro")
    adapter = engine._auxiliary_request_api("residual_cjk_micro_repair", 3,
        SOURCE + (" changed" if change == "source" else ""), TEXT + (" changed" if change == "pending" else ""))
    with pytest.raises(AuxiliaryPaidRequestError) as caught: adapter(modified, operation_id="bound")
    assert caught.value.transport_evidence["status"] == "blocked"
    assert not caught.value.transport_evidence["api_called"]
    assert len(runtime.calls) == 1 and len(runtime.records()) == 1
    assert before == [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]


def test_auxiliary_adapter_import_has_no_provider_credential_or_write_side_effects(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    code = r'''
import sys, os
sys.dontwritebytecode = True
sys.path.insert(0, ROOT)
def audit(event, args):
    if event == 'open':
        name, mode, flags = args
        if '.env' in str(name).lower() or 'credential' in str(name).lower():
            raise AssertionError('credential read')
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError('state write')
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('provider/network call')
sys.addaudithook(audit)
import production_auxiliary_paid_request
print('import safe')
'''
    result = subprocess.run([sys.executable, "-c", code.replace("ROOT", repr(str(Path(__file__).resolve().parents[1])))],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import safe" and not list(tmp_path.iterdir())


def test_request_construction_refusal_cannot_claim_physical_execution(runtime, monkeypatch):
    monkeypatch.setattr(engine, "operation_request_controls", lambda kind: (_ for _ in ()).throw(ValueError("unsupported controls")))
    domain = Domain()
    result = domain.run(runtime)
    attempt = next(iter(domain.meta["residual_cjk_micro_repair"]["attempts_by_pending_hash"].values()))[0]
    assert attempt["status"] == "planned" and not attempt["api_called"] and not result["api_called"]
    assert not runtime.records() and not runtime.calls and runtime.budget.api_calls == 0


@pytest.fixture(params=["residual", "strict", "adjudication"])
def provenance_domain(request, runtime, tmp_path):
    kind = request.param
    state = {"meta": {}, "record": None, "progress": {}, "events": []}
    def account(op, stats): return add_accounting_totals_once(state["progress"], op, stats)
    def log(event, **fields): state["events"].append(fields)
    if kind == "residual":
        domain = Domain()
        def run():
            return perform_residual_cjk_micro_repair(novel_id="fixture", chapter=3, profile_id="infinite_flow",
                target_language="en", source_text=SOURCE, translation=TEXT, meta=domain.meta, config=CONFIG,
                relevant_locked_glossary=[], auxiliary_terminology_policy="", request_api=runtime.adapter(),
                persist_meta=domain.persist, persist_translation=lambda value: None,
                account_api_usage=account, qa_checker=lambda text: {"commit_allowed": True}, log_event=log)
        def records(): return next(iter(domain.meta["residual_cjk_micro_repair"]["attempts_by_pending_hash"].values()))
    elif kind == "strict":
        from noveltranslator_core import translation_commit_qa
        source = "他使用鳴鶴拳。"
        text = "He used Minghequan at close range in this complete English sentence."
        entries = {"鳴鶴拳": {"english": "Calling Crane Boxing", "category": "technique", "status": "locked",
                              "strict_qa": True, "auto_repair": True, "repair_aliases": ["Minghequan"]}}
        def run():
            return perform_strict_terminology_micro_repair(novel_id="fixture", chapter=3, profile_id="infinite_flow",
                target_language="en", source_text=source, translation=text, meta=state["meta"], glossary_entries=entries,
                high_priority_categories=(), auxiliary_terminology_policy="",
                request_api=engine._auxiliary_request_api("strict_terminology_micro_repair", 3, source, text),
                persist_meta=lambda value: state.__setitem__("meta", copy.deepcopy(value)),
                persist_translation=lambda value: None, account_api_usage=account,
                qa_checker=lambda candidate: translation_commit_qa(source, candidate, entries, ()), log_event=log)
        def records(): return next(iter(state["meta"]["strict_terminology_micro_repair"]["attempts_by_pending_hash"].values()))
    else:
        from test_glossary_candidate_auto_resolution import conflict_for, conflict_candidates, config
        from glossary_candidate_auto_resolution import resolve_candidate_conflicts_automatically
        def run():
            return resolve_candidate_conflicts_automatically(novel_id="fixture", chapter=3,
                pending_identity="1"*64, state_identity="2"*64, conflicts=[conflict_for(conflict_candidates())],
                source_text=SOURCE, translation=TEXT, existing_glossary={}, config=config(),
                resolution_path=tmp_path/"resolution.json", recovery_record=state["record"],
                persist_recovery=lambda value: state.__setitem__("record", copy.deepcopy(value)),
                request_api=runtime.adapter("glossary_candidate_conflict_adjudication"),
                is_operation_accounted=lambda op: op in state["progress"].get("accounted_api_operation_ids", []),
                account_api_usage=account)
        def records(): return [state["record"]]
    return SimpleNamespace(kind=kind, run=run, records=records, state=state)


def wrong_model_response():
    return SimpleNamespace(model="deepseek-v4-pro", choices=[
        SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))


def test_domain_wrong_model_paid_origin_and_stats_are_monotonic(runtime, provenance_domain):
    domain = provenance_domain
    runtime.responses.append(wrong_model_response())
    domain.run()
    first = copy.deepcopy(domain.records()[-1])
    assert first["status"] == "response_recorded" and first["api_called"] is True
    assert first["stats"]["cost_usd"] > 0 and first["transport_evidence"]["api_called"] is True
    progress = copy.deepcopy(domain.state["progress"])
    before = [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    domain.run()
    replay = domain.records()[-1]
    assert replay["status"] == "response_recorded" and replay["api_called"] is True
    assert replay["stats"] == first["stats"]
    assert replay["transport_evidence"]["api_called"] is False and replay["transport_evidence"]["replayed"] is True
    assert domain.state["progress"] == progress
    assert len(runtime.calls) == 1 and before == [p.read_bytes() for p in runtime.journal_dir.glob("*.json")]
    if domain.kind != "adjudication": assert domain.state["events"][-1]["api_cost"] == first["stats"]["cost_usd"]


def test_domain_uncertain_restart_preserves_historical_attempt(runtime, provenance_domain):
    domain = provenance_domain
    runtime.responses.append(RuntimeError("possible remote send"))
    domain.run()
    first = copy.deepcopy(domain.records()[-1])
    assert first["status"] == "uncertain" and first["api_called"] is True and not first.get("stats")
    domain.run()
    blocked = domain.records()[-1]
    assert blocked["status"] == "uncertain" and blocked["api_called"] is True
    assert blocked["transport_evidence"]["api_called"] is False and blocked["transport_evidence"]["replayed"] is False
    assert blocked["transport_evidence"]["remote_attempt_possible"] is True
    assert not blocked.get("stats") and not domain.state["progress"] and len(runtime.calls) == 1


def test_domain_identity_block_cannot_erase_paid_stats(runtime, provenance_domain, monkeypatch):
    domain = provenance_domain
    runtime.responses.append(wrong_model_response())
    domain.run()
    first = copy.deepcopy(domain.records()[-1])
    monkeypatch.setattr(engine, "operation_request_controls", lambda kind: {
        "max_tokens": 799, "extra_body": {"thinking": {"type": "disabled"}}})
    domain.run()
    blocked = domain.records()[-1]
    assert blocked["status"] == "blocked" and blocked["api_called"] is True
    assert blocked["stats"] == first["stats"] and blocked["transport_evidence"]["api_called"] is False
    assert not blocked["transport_evidence"]["replayed"] and len(runtime.calls) == 1


@pytest.mark.parametrize("provenance_domain", ["residual", "strict"], indirect=True)
def test_domain_paid_correction_failure_is_observable_and_stable(runtime, provenance_domain):
    domain = provenance_domain
    if domain.kind == "residual":
        invalid = copy.deepcopy(PATCH)
        invalid["repairs"][0]["new_text"] = "the嚴苛 supervisor"
        runtime.responses.append(json.dumps(invalid))
    else: runtime.responses.append('{"patches": []}')
    runtime.responses.append(wrong_model_response())
    first_result = domain.run()
    def correction(): return domain.records()[0]["corrective_retry"] if domain.kind == "residual" else domain.records()[-1]
    first = copy.deepcopy(correction())
    assert first["status"] == "response_recorded" and first["api_called"] is True
    assert len(runtime.records()) == len(runtime.calls) == 2
    assert len({r["operation_kind"] for r in runtime.records()}) == 2
    expected = sum(r["response"]["stats"]["cost_usd"] for r in runtime.records())
    def returned_cost(result):
        return result["repair_stats"]["cost_usd"] if domain.kind == "residual" else sum(
            stats["cost_usd"] for stats in result["repair_attempt_stats"])
    assert returned_cost(first_result) == pytest.approx(expected)
    assert domain.state["events"][-1]["api_cost"] == first["stats"]["cost_usd"]
    progress = copy.deepcopy(domain.state["progress"])
    replay_result = domain.run()
    replay = correction()
    assert replay["api_called"] is True and replay["stats"] == first["stats"]
    assert replay["transport_evidence"]["replayed"] is True and replay["transport_evidence"]["api_called"] is False
    assert returned_cost(replay_result) == pytest.approx(expected)
    assert domain.state["events"][-1]["api_cost"] == first["stats"]["cost_usd"]
    assert len(runtime.calls) == 2 and domain.state["progress"] == progress
