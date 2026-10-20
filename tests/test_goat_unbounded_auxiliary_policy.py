"""Zero-provider uncapped wire, authorization and identity regressions."""
import copy
import json
from pathlib import Path

import pytest

import production_goat_auxiliary_policy as policy
from production_goat_policy import policy_identity, policy_sha256, historical_policy_identity_v1
from production_goat_runtime import normalize_production_request, GoatBudget, GoatRuntimeError
from production_provider_runtime import provider_identity, historical_capped_goat_provider_identity
from production_paid_request_journal import _sha256_json, _updated_record
from benchmark_longitudinal_provider import build_provider_request_identity
from test_production_goat_runtime import engine, zero_network, kwargs, response, CLOCK, GOAT


@pytest.mark.parametrize("item", policy.domain_roster(), ids=lambda item: item["operation"])
def test_every_catalog_auxiliary_omits_both_caps(item):
    wire = normalize_production_request(item["logical"], kwargs(item["domain_tokens"]))
    assert wire["reasoning_effort"] == "low"
    assert wire["model"] == "deepseek/deepseek-v4.1-flash"
    assert "max_tokens" not in wire and "max_completion_tokens" not in wire
    assert build_provider_request_identity(request_body=wire).request_sha256 == build_provider_request_identity(
        request_body=normalize_production_request(item["logical"], kwargs(item["domain_tokens"]))).request_sha256
    assert build_provider_request_identity(request_body={**wire, "max_completion_tokens": 8000}).request_sha256 != build_provider_request_identity(request_body=wire).request_sha256
    assert item["max_attempts"] == 1


def test_production_policy_has_explicit_unbounded_identity_and_exact_history():
    new = provider_identity(GOAT, "v41-flash")
    old = historical_capped_goat_provider_identity()
    assert new["auxiliary_completion_policy"] == "provider_unbounded"
    assert new["auxiliary_completion_tokens"] is None
    assert new["revision"] == "production-provider-runtime-v3"
    assert old["auxiliary_completion_tokens"] == 8000
    assert "auxiliary_completion_policy" not in old
    assert old["translation_completion_tokens"] == new["translation_completion_tokens"] == 50000
    assert _sha256_json(old) != _sha256_json(new)
    assert policy_sha256() != _sha256_json(historical_policy_identity_v1())
    assert policy_identity()["max_retries"] == 0 and policy_identity()["fallback"] is None
    assert old["translation_budget_policy_sha256"] == _sha256_json(historical_policy_identity_v1())
    assert _sha256_json(old) == "441cf3c19093558fb3d9a9a8c221d9c3cc6ad9310231bcc656c0d7fb119d8f08"


@pytest.mark.parametrize("injected", [dict(max_completion_tokens=8000), dict(max_completion_tokens=None),
    dict(max_tokens=50000), dict(reasoning_effort="high"),
    dict(extra_body={"max_completion_tokens": 2000})])
def test_alternate_auxiliary_controls_cannot_be_smuggled(injected):
    with pytest.raises((ValueError, RuntimeError)):
        normalize_production_request("state_extraction", {**kwargs(4000), **injected})


def test_missing_reviewed_capability_fails_closed_without_hidden_ceiling(monkeypatch):
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", {})
    wire = normalize_production_request("state_extraction", kwargs(4000))
    budget = GoatBudget(2, 100)
    with pytest.raises(GoatRuntimeError, match="architecture review"):
        with budget.authorize(wire, CLOCK()):
            pytest.fail("authorization must not yield")
    assert budget.api_calls == 0 and budget.actual_cost_usd == 0
    assert wire == normalize_production_request("state_extraction", kwargs(4000))


def test_conservative_context_bound_never_subtracts_prompt_upper(synthetic_goat_hard_bound):
    wire = normalize_production_request("state_extraction", kwargs(4000))
    bound = policy.completion_exposure(wire, prompt_upper=20000)
    assert bound["completion_upper"] == 131072
    assert bound["prompt_upper"] == 131072
    assert bound["capability_source"]["primary_source"] == "synthetic-test-only"
    assert "max_tokens" not in wire and "max_completion_tokens" not in wire


@pytest.mark.parametrize("metadata", [{}, {"source": "fixture", "source_sha256": "a"*64},
    {"source": "fixture", "source_sha256": "a"*64, "context_tokens": 10},
    {"source": "fixture", "source_sha256": "a"*64, "output_tokens": -1}])
def test_invalid_bound_metadata_refuses(monkeypatch, metadata):
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", {(GOAT, "deepseek/deepseek-v4.1-flash"): metadata})
    with pytest.raises(ValueError):
        policy.completion_exposure(normalize_production_request("state_extraction", kwargs(4000)), prompt_upper=100)


@pytest.mark.parametrize("gate", ["calls", "credits", "missing_bound", "budget_missing"])
def test_presend_refusal_stays_planned(engine, monkeypatch, gate):
    project, module, sends, client, budget, *_ = engine
    if gate == "calls": budget.api_calls = budget.max_api_calls
    if gate == "credits": budget.max_cost_usd = .000001
    if gate == "missing_bound": monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", {})
    if gate == "budget_missing":
        # Independent fixture module with explicitly missing budget.
        import run_production_engine as launcher
        from production_goat_runtime import install_goat_runtime
        module = launcher.load_production_module()
        module.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
        install_goat_runtime(module, provider_client=client, clock=CLOCK)
    with pytest.raises(GoatRuntimeError):
        module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    assert not sends
    path = project.data_dir / "provider_request_journal/0001.state_extraction.json"
    assert json.loads(path.read_text())["status"] == "planned"


def test_uncapped_replay_accounts_one_send_only(engine):
    _, module, sends, _, budget, *_ = engine
    a = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    b = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    assert a["response"] == b["response"] and b["replayed"]
    assert len(sends) == budget.api_calls == 1
    assert budget.actual_cost_usd == a["response"]["stats"]["cost_usd"]


@pytest.mark.parametrize("status", ["planned", "started", "uncertain", "response_recorded", "corrupt", "old_capped"])
def test_uncapped_journal_states_and_capped_mismatch(engine, status):
    project, module, sends, _, budget, *_ = engine
    module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    path = project.data_dir / "provider_request_journal/0001.state_extraction.json"
    record = json.loads(path.read_text())
    if status in {"planned", "started", "uncertain"}:
        module.atomic_write_json(path, _updated_record(record, status=status, response={}, response_sha256=""))
    if status == "corrupt": path.write_text("{}")
    if status == "old_capped":
        module.atomic_write_json(path, _updated_record(record, provider_policy=historical_capped_goat_provider_identity()))
    if status in {"planned", "response_recorded"}:
        result = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
        assert result["operation_id"] == record["operation_id"]
        assert len(sends) == (2 if status == "planned" else 1)
    else:
        with pytest.raises(RuntimeError): module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
        assert len(sends) == 1


@pytest.mark.parametrize("recovery_finish", ["stop", "length"])
def test_normal_state_controller_has_exactly_one_uncapped_recovery(engine, recovery_finish):
    project, module, sends, client, budget, *_ = engine
    state = json.dumps(dict(new_terms=[], continuity_summary="A man entered.", issues=[]))
    answers = [response(content="", finish="length", completion=100),
        response(content=state, finish=recovery_finish, completion=200)]
    def send(**body): sends.append(body); return answers.pop(0)
    client.chat.completions.create = send
    if recovery_finish == "stop":
        first = module.extract_state(1, "甲", "A man entered.")
        second = module.extract_state(1, "甲", "A man entered.")
        assert first == second and first[1]["completion_tokens"] == 300
    else:
        from production_state_extraction import StateExtractionError
        for _ in range(2):
            with pytest.raises(StateExtractionError): module.extract_state(1, "甲", "A man entered.")
    assert len(sends) == budget.api_calls == 2
    assert not any("max_tokens" in w or "max_completion_tokens" in w for w in sends)
    assert {p.name for p in (project.data_dir / "provider_request_journal").glob("*")} == {
        "0001.state_extraction.json", "0001.state_extraction_recovery.json"}
