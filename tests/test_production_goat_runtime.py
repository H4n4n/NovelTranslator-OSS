"""Zero-network production GOAT proofs; all state is disposable fixture state."""
import copy
import importlib
import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import noveltranslator_core as core
import command_code_provider_credentials as credentials
import command_code_provider_transport as transport
import run_production_engine as launcher
from production_provider_runtime import (
    provider_identity, selected_provider, pending_provider_compatibility,
    goat_provenance, canonical_pending_provenance, PROVIDER_ENV,
)
from production_provider_contract import PROVIDER_COMMAND_CODE as GOAT
from production_goat_runtime import (
    install_goat_runtime, normalize_production_request, GoatBudget, GoatRuntimeError, DOMAIN_OPERATIONS,
)
from production_paid_request_journal import _updated_record
from controlled_trial_plan import build_controlled_trial_plan
from controlled_trial_execute import execute_controlled_trial, _capture_core_runtime, _restore_core_runtime
from pending_recovery_plan import build_pending_recovery_plan
from pending_recovery_execute import execute_pending_recovery
from production_path_preflight import run_production_path_preflight
from operator_preflight import build_operator_preflight
from test_controlled_trial_plan import _empty_project
from test_command_code_provider_credentials import _FakeDpapi

IDENTITY = provider_identity(GOAT, "v41-flash")
pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")
CLOCK = lambda: datetime(2026, 10, 5, 2, tzinfo=timezone.utc)


def response(*, model=None, usage=True, content="English translation.", finish="stop", completion=10, reasoning=None):
    return NS(id="fake-response", model=model or IDENTITY["wire_model"],
        choices=[NS(finish_reason=finish, message=NS(content=content))],
        usage=NS(prompt_tokens=20, completion_tokens=completion, total_tokens=20+completion,
            completion_tokens_details=NS(reasoning_tokens=reasoning) if reasoning is not None else None,
            prompt_tokens_details=NS(cached_tokens=3)) if usage else None)


def kwargs(ceiling=12000):
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "source"}],
            "max_tokens": ceiling, "extra_body": {"thinking": {"type": "disabled"}}}


@pytest.fixture(autouse=True)
def zero_network(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(credentials, "load_platform_dpapi", lambda: pytest.fail("real DPAPI forbidden"))


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Keep the historical harness's direct __wrapped__ invocation compatible.
    from conftest import install_synthetic_goat_hard_bound
    install_synthetic_goat_hard_bound(monkeypatch, tmp_path)
    project = _empty_project(tmp_path)
    before = _capture_core_runtime()
    monkeypatch.chdir(project.root)
    prepared = launcher.prepare_production_runtime("v41-flash", project.pending_dir, provider_selection=GOAT)
    module = launcher.load_production_module()
    monkeypatch.setattr(module, "load_dotenv", lambda: pytest.fail("dotenv forbidden"))
    monkeypatch.setattr(module, "OpenAI", lambda *a, **k: pytest.fail("direct client forbidden"))
    module.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
    sends = []
    def create(**body):
        sends.append(body)
        return response()
    client = NS(chat=NS(completions=NS(create=create)))
    budget = GoatBudget(8, 1.0)
    runtime = install_goat_runtime(module, provider_client=client, budget=budget, clock=CLOCK)
    yield project, module, sends, client, budget, prepared, runtime
    _restore_core_runtime(before)


def test_explicit_provider_dimension_and_unknown_fail_closed():
    assert selected_provider() == "direct_deepseek"
    assert provider_identity(None, "v41-flash")["provider"] == "direct_deepseek"
    assert IDENTITY["base_url"] == "https://api.commandcode.ai/provider/v1"
    assert IDENTITY["wire_model"] == "deepseek/deepseek-v4.1-flash"
    with pytest.raises(ValueError): selected_provider("invalid")
    with pytest.raises(ValueError): provider_identity(GOAT, "pro")


@pytest.mark.parametrize("logical,ceiling", [("translation",12000), ("state_extraction",4000),
    ("state_extraction",8000), ("state_json_repair",5000), ("glossary_review",800),
    ("glossary_review",2000), ("glossary_review",4000), ("glossary_review",5000),
    ("periodic_audit",4000), ("terminology_repair",800)])
def test_exact_normalized_closed_policy(logical, ceiling):
    wire = normalize_production_request(logical, kwargs(ceiling))
    assert wire["model"] == IDENTITY["wire_model"]
    assert wire["reasoning_effort"] == "low"
    if logical == "translation":
        assert wire["max_completion_tokens"] == 50000
    else:
        assert "max_completion_tokens" not in wire
    assert "extra_body" not in wire and "max_tokens" not in wire


@pytest.mark.parametrize("logical,ceiling", [("translation",24000),("translation",32000),
    ("translation",True),("glossary_review",7999),("state_extraction",1),
    ("terminology_repair",799),("unknown",4000)])
def test_unsupported_policy_refused(logical, ceiling):
    with pytest.raises((ValueError, RuntimeError)):
        normalize_production_request(logical, kwargs(ceiling))


def test_journal_order_exact_replay_and_accounting(engine):
    project, module, sends, client, budget, prepared, runtime = engine
    statuses = []
    original = module.atomic_write_json
    def save(path, data):
        if "status" in data: statuses.append(data["status"])
        original(path, data)
    module.atomic_write_json = save
    def create(**body):
        path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert disk["status"] == "started"
        assert disk["provider"] == GOAT and disk["model"] == IDENTITY["wire_model"]
        assert disk["provider_policy"] == IDENTITY
        assert disk["wire_request_sha256"]
        sends.append(body)
        return response()
    client.chat.completions.create = create
    first = module.journaled_chat_request("chapter_translation", 1, "甲", kwargs())
    second = module.journaled_chat_request("chapter_translation", 1, "甲", kwargs())
    assert statuses[:3] == ["planned", "started", "response_recorded"]
    assert first["response"] == second["response"]
    assert first["api_called"] and second["replayed"] and len(sends) == 1
    assert budget.api_calls == 1
    stats = first["response"]["stats"]
    assert stats["pricing_unit"] == IDENTITY["pricing_unit"]
    assert stats["provider_invoice_claimed"] is False
    assert stats["request_started_utc"] == CLOCK().isoformat()
    assert stats["cache_hit_tokens"] == 3 and stats["cache_miss_tokens"] == 17
    assert budget.actual_cost_usd == stats["cost_usd"]


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("boundary", ["before_replace", "after_replace", "after_persist", "transport", "normalization"])
@pytest.mark.parametrize("finish", ["stop", "content_filter", "length"])
def test_interrupt_after_response_replace_preserves_recorded_response(engine, monkeypatch, error_type, boundary, finish):
    from test_production_paid_request_journal import _interrupt_response_persistence
    import production_goat_runtime as goat
    from production_paid_request_journal import load_matching_journal, PaidRequestUncertainError
    from production_goat_cap_recovery import accepted_envelope
    from production_goat_state_terminal_recovery import settle, verify_settlement, RECEIPTS

    project, module, sends, client, budget, *_ = engine
    from production_accounting import GOAT_METADATA
    # Model an already activated GOAT accounting epoch, as required by the
    # reviewed detached terminal settlement path.
    module.progress.update(GOAT_METADATA)
    module.atomic_write_json(module.PROGRESS_FILE, module.progress)
    kind = "state_extraction"
    source = module.chapter_map[1]["text"]
    request = kwargs(4000)
    binding = module._chat_request_identity(kind, 1, source, request)
    path = project.data_dir / "provider_request_journal/0001.state_extraction.json"
    raw = response(content="A man entered." if finish == "stop" else "", finish=finish)
    raw_evidence = goat.raw_response_record(raw)
    error = error_type("GOAT paid response boundary")
    with monkeypatch.context() as injection:
        observed = (_interrupt_response_persistence(injection, path, error, boundary)
                    if boundary not in {"transport", "normalization"} else {})
        def create(**wire):
            sends.append(wire)
            if boundary == "transport":
                raise error
            return raw
        injection.setattr(client.chat.completions, "create", create)
        if boundary == "normalization":
            def interrupted_normalization(raw):
                raise error
            injection.setattr(goat, "raw_response_record", interrupted_normalization)
        # The GOAT transport adapter retains its existing wrapper for transport
        # failures. Persistence/normalization interruptions propagate unchanged.
        with pytest.raises(GoatRuntimeError if boundary == "transport" else error_type) as caught:
            module.journaled_chat_request(kind, 1, source, request)
        if boundary != "transport":
            assert caught.value is error

    durable = load_matching_journal(path, binding)
    recorded = boundary in {"after_replace", "after_persist"}
    assert durable["status"] == ("response_recorded" if recorded else "uncertain")
    if recorded:
        assert path.read_bytes() == observed["bytes"]
        assert durable["response"]["raw_response"] == raw_evidence
        assert durable["response"]["raw_response"]["usage"]["completion_tokens"] == 10
    else:
        assert durable["response"] == {} and durable["response_sha256"] == ""
    assert len(sends) == budget.api_calls == 1
    assert budget.actual_cost_usd == 0  # Acceptance did not run before interruption.

    replay_sends = []
    def forbidden_send(**wire):
        replay_sends.append(wire)
        pytest.fail("fresh runtime must not physically resend")
    def fresh_runtime():
        fresh = launcher.load_production_module()
        fresh.load_dotenv = lambda: pytest.fail("credentials on replay")
        fresh.OpenAI = lambda **k: pytest.fail("direct client on replay")
        fresh.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
        fresh_budget = GoatBudget(1, 1)
        install_goat_runtime(fresh, provider_client=NS(chat=NS(completions=NS(create=forbidden_send))),
            budget=fresh_budget, clock=CLOCK)
        fresh.wait_for_api_permission = lambda: pytest.fail("prepare/authorization on replay")
        return fresh, fresh_budget

    fresh, replay_budget = fresh_runtime()
    if not recorded:
        before = path.read_bytes()
        with pytest.raises(PaidRequestUncertainError):
            fresh.journaled_chat_request(kind, 1, source, request)
        assert path.read_bytes() == before
        assert not replay_sends and not replay_budget.api_calls and not replay_budget.response_records
        return

    if finish == "stop":
        replay = fresh.journaled_chat_request(kind, 1, source, request)
        assert replay["replayed"] and not replay["api_called"]
        envelope = replay["response"]
    else:
        with pytest.raises(GoatRuntimeError, match="no reissue"):
            fresh.journaled_chat_request(kind, 1, source, request)
        # The reviewed terminal mechanism accepts known usage even when the
        # ordinary domain result is rejected. It has no transport callback.
        envelope = accepted_envelope(durable["response"], normalize_production_request("state_extraction", request))
    enriched = load_matching_journal(path, binding)
    assert enriched["response"]["raw_response"] == raw_evidence
    stats = envelope["stats"]
    assert stats["prompt_tokens"] == 20 and stats["completion_tokens"] == 10
    assert replay_budget.response_records == [{"replayed": True, "stats": stats}]
    assert replay_budget.api_calls == 0 and replay_budget.actual_cost_usd == 0
    assert not replay_sends and len(sends) == 1
    if finish == "stop":
        assert core.add_accounting_totals_once(fresh.progress, enriched["operation_id"], stats)
        fresh.atomic_write_json(fresh.PROGRESS_FILE, fresh.progress)
    else:
        assert settle(fresh, enriched, envelope)
        assert verify_settlement(fresh.progress, enriched, envelope)
        assert len(fresh.progress[RECEIPTS]) == 1
    progress_bytes = project.progress_file.read_bytes()
    # A second newly initialized runtime proves durable once-only accounting.
    restarted, second_budget = fresh_runtime()
    if finish == "stop":
        again = restarted.journaled_chat_request(kind, 1, source, request)
        assert again["replayed"] and again["response"] == envelope
        assert not core.add_accounting_totals_once(restarted.progress, enriched["operation_id"], stats)
    else:
        with pytest.raises(GoatRuntimeError, match="no reissue"):
            restarted.journaled_chat_request(kind, 1, source, request)
        assert not settle(restarted, enriched, envelope)
    assert project.progress_file.read_bytes() == progress_bytes
    assert restarted.progress["total_prompt_tokens"] == 20
    assert restarted.progress["total_completion_tokens"] == 10
    assert restarted.progress["estimated_cost_usd"] == stats["cost_usd"]
    assert restarted.progress["accounted_api_operation_ids"].count(enriched["operation_id"]) == 1
    assert second_budget.api_calls == 0 and not replay_sends and len(sends) == 1


@pytest.mark.parametrize("bad", ["model", "usage", "content", "length", "empty_auxiliary"])
def test_raw_record_precedes_rejection_and_zero_resend(engine, bad):
    project, module, sends, client, budget, *_ = engine
    raw = response(model="wrong" if bad == "model" else None, usage=bad != "usage",
        content=None if bad == "content" else ("" if bad == "empty_auxiliary" else "text"),
        finish="length" if bad in {"length", "empty_auxiliary"} else "stop")
    def create(**body): sends.append(body); return raw
    client.chat.completions.create = create
    operation = "state_extraction" if bad == "empty_auxiliary" else "chapter_translation"
    payload = kwargs(4000 if bad == "empty_auxiliary" else 12000)
    for _ in range(2):
        with pytest.raises(GoatRuntimeError): module.journaled_chat_request(operation,1,"甲",payload)
    assert len(sends) == 1
    record = json.loads((project.data_dir / f"provider_request_journal/0001.{operation}.json").read_text(encoding="utf-8"))
    assert record["status"] == "response_recorded"
    assert record["response"]["raw_response"]["model"] == raw.model


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_surviving_attempt_never_reissued(engine, status):
    project, module, sends, *_ = engine
    module.journaled_chat_request("chapter_translation",1,"甲",kwargs())
    path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    module.atomic_write_json(path, _updated_record(record,status=status,response={},response_sha256=""))
    with pytest.raises(RuntimeError): module.journaled_chat_request("chapter_translation",1,"甲",kwargs())
    assert len(sends) == 1


def test_presend_budget_refusal_leaves_planned(engine):
    project, module, sends, client, budget, *_ = engine
    budget.max_cost_usd = 0.000001
    with pytest.raises(GoatRuntimeError): module.journaled_chat_request("chapter_translation",1,"甲",kwargs())
    assert not sends and not budget.api_calls
    record = json.loads((project.data_dir / "provider_request_journal/0001.chapter_translation.json").read_text(encoding="utf-8"))
    assert record["status"] == "planned"


def test_unknown_operation_and_context_fail_before_send(engine):
    _, module, sends, *_ = engine
    with pytest.raises(GoatRuntimeError): module.journaled_chat_request("invented",1,"甲",kwargs())
    with pytest.raises(GoatRuntimeError): module.send_api_request_once(**kwargs(800))
    assert not sends


@pytest.mark.parametrize("operation", list(DOMAIN_OPERATIONS))
def test_current_domain_operations_are_journaled(engine, operation):
    _, module, sends, *_ = engine
    # Catalog chooses exact existing operation ceilings.
    policy = module.current_production_operation_policy().for_operation(operation)
    module.journaled_chat_request(operation,1,"甲",kwargs(policy.max_tokens))
    assert "max_completion_tokens" not in sends[0] and "max_tokens" not in sends[0]


def test_provenance_and_checkpoint_bind_provider(engine):
    project, module, _, _, _, prepared, _ = engine
    result = module.journaled_chat_request("chapter_translation",1,"甲",kwargs())
    module.atomic_write_text(project.pending_dir / "0001.md", "English")
    module.atomic_write_json(project.pending_dir / "0001.json", {"chapter":1,"translation_stats":result["response"]["stats"]})
    meta = json.loads((project.pending_dir / "0001.json").read_text(encoding="utf-8"))
    assert meta["translation_provider"] == GOAT
    assert meta["translation_max_tokens"] == 50000
    assert meta["translation_thinking"] == "low"
    module.atomic_write_json(project.qa_dir / "0001.json", {"commit_allowed": True})
    qa = json.loads((project.qa_dir / "0001.json").read_text(encoding="utf-8"))
    assert qa["translation_provider_identity"] == IDENTITY
    assert qa["translation_request_sha256"] == meta["translation_request_sha256"]
    checkpoint = module.current_state_checkpoint_identity(1,"甲","English")
    assert checkpoint["provider_identity"] == IDENTITY
    assert checkpoint["wire_completion_policy"]["auxiliary_completion_policy"] == "provider_unbounded"
    assert "wire_max_completion_tokens" not in checkpoint
    with pytest.raises(ValueError): pending_provider_compatibility(meta, provider_identity(None,"v41-flash"))
    with pytest.raises(ValueError): pending_provider_compatibility({"translation_model":"deepseek-flash"}, IDENTITY)


def test_provider_aware_planning_and_readonly_preflight(tmp_path, monkeypatch):
    project = _empty_project(tmp_path)
    monkeypatch.setattr(os, "getenv", lambda key,*a: pytest.fail("direct key access") if key == "DEEPSEEK_API_KEY" else os.environ.get(key,*a))
    direct = build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash")
    goat = build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash",
        provider_selection=GOAT,max_credit_equivalent_usd=1)
    assert direct["state_fingerprint"] != goat["state_fingerprint"]
    assert direct["approval_token"] != goat["approval_token"]
    assert goat["scheduler_mode"] == "provider_unrestricted"
    preflight = run_production_path_preflight("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT)
    assert preflight["status"] == "PASS" and preflight["api_calls"] == preflight["project_writes"] == 0
    assert preflight["provider_identity"] == IDENTITY
    operator = build_operator_preflight("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT)
    assert operator["status"] == "BOUNDED_ALLOWANCE_REQUIRED"
    assert operator["structurally_ready"] is True
    assert operator["recommended_entrypoint"] == ""
    assert operator["recommended_args"] == []
    import controlled_trial_plan as planner
    original_hash = planner._sha256_file
    original_identity = planner._engine_runtime_identity("historical_military")
    for name in ("production_goat_runtime.py","production_goat_execution.py","production_provider_runtime.py",
                 "command_code_provider_transport.py","command_code_provider_credentials.py"):
        monkeypatch.setattr(planner, "_sha256_file", lambda path: "a"*64 if Path(path).name == name else original_hash(path))
        assert planner._engine_runtime_identity("historical_military")["sha256"] != original_identity["sha256"]


@pytest.mark.parametrize("change", ["token", "state", "provider"])
def test_invalid_trial_authorization_precedes_dpapi(tmp_path, change):
    _empty_project(tmp_path)
    plan = build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT,max_credit_equivalent_usd=1)
    with pytest.raises(RuntimeError):
        execute_controlled_trial("fixture",repo_root=tmp_path,model_selection="v41-flash",
            provider_selection="direct_deepseek" if change == "provider" else GOAT,
            max_api_calls=plan["max_api_calls"],max_cost_usd=1 if change == "provider" else None,
            max_credit_equivalent_usd=None if change == "provider" else 1,
            expected_state_fingerprint="0"*64 if change == "state" else plan["state_fingerprint"],
            approval_token="0"*64 if change == "token" else plan["approval_token"],authorize_paid_trial=True)


def fake_credentials(monkeypatch,tmp_path,events):
    secret=tmp_path/"fake.dpapi";secret.write_bytes(b"fake-ciphertext")
    monkeypatch.setattr(credentials,"command_code_secret_path",lambda **k: secret)
    class DPAPI(_FakeDpapi):
        def unprotect(self,cipher): events.append("fake_dpapi");return super().unprotect(cipher)
    monkeypatch.setattr(credentials,"load_platform_dpapi",lambda: DPAPI(b"sk-cc-test-only"))


def test_valid_authorization_real_lifecycle_fake_transport(tmp_path, monkeypatch, capsys):
    project = _empty_project(tmp_path)
    plan = build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT,max_credit_equivalent_usd=1)
    events=[]
    fake_credentials(monkeypatch,tmp_path,events)
    sent=[]
    def create(**body):
        assert events[0] == "fake_dpapi"
        sent.append(body)
        if len(sent)==1: return response(content="A man entered the hall.", completion=3617, reasoning=3501)
        return response(content=json.dumps({"new_terms":[],"continuity_summary":"A man entered.","issues":[]}), completion=1843, reasoning=1737)
    def factory(**opts):
        assert opts == {"api_key":"sk-cc-test-only","base_url":IDENTITY["base_url"],"max_retries":0}
        assert "DEEPSEEK_API_KEY" not in os.environ and "COMMAND_CODE_API_KEY" not in os.environ
        events.append("fake_client")
        return NS(chat=NS(completions=NS(create=create)),close=lambda: events.append("closed"))
    monkeypatch.setattr(transport,"create_command_code_client",lambda **k: transport.create_provider_client(
        api_key=k["api_key"],base_url=IDENTITY["base_url"],client_factory=factory))
    original_load=launcher.load_production_module
    def load(*a,**k):
        module=original_load(*a,**k)
        module.load_dotenv=lambda: pytest.fail("dotenv forbidden")
        module.OpenAI=lambda **k: pytest.fail("direct client forbidden")
        # Isolate provider wiring from short synthetic-source completeness policy.
        module.local_qa=lambda *a,**k: {"commit_allowed":True,"issues":[]}
        return module
    monkeypatch.setattr(launcher,"load_production_module",load)
    report=execute_controlled_trial("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT,
        max_api_calls=plan["max_api_calls"],max_cost_usd=None,max_credit_equivalent_usd=1,
        expected_state_fingerprint=plan["state_fingerprint"],approval_token=plan["approval_token"],authorize_paid_trial=True)
    assert report["status"] == "COMMITTED", report
    assert events == ["fake_dpapi","fake_client","closed"]
    assert report["api_calls"] == 2
    from production_accounting import GOAT_METADATA
    persisted = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert all(persisted[key] == value for key, value in GOAT_METADATA.items())
    assert persisted["total_completion_tokens"] == 5460
    stats = [record["stats"] for record in report["accepted_provider_responses"]]
    assert [(item["reasoning_tokens"], item["visible_completion_tokens"]) for item in stats] == [(3501,116),(1737,106)]
    assert all(item["reasoning_tokens_exposed"] for item in stats)
    assert report["actual_credit_equivalent_usd"] == sum(item["cost_usd"] for item in stats)
    assert run_production_path_preflight("fixture", repo_root=tmp_path, model_selection="v41-flash", provider_selection=GOAT)["status"] == "PASS"
    from validate_project import validate_project
    import shutil
    shutil.copytree(Path(core.__file__).parent / "profiles", tmp_path / "profiles")
    assert validate_project(project) == 0
    assert sent[0]["max_completion_tokens"] == 50000 and "max_completion_tokens" not in sent[1]
    assert report["provider_invoice_claimed"] is False
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["completed"] == [1]
    qa=json.loads((project.qa_dir/"0001.json").read_text(encoding="utf-8"))
    assert qa["translation_provider"]==GOAT
    assert not (project.chapter_dir/"0002.md").exists()
    from controlled_trial_execute import _print_human
    _print_human(report)
    human = capsys.readouterr().out
    assert "credit-equivalent USD; provider_invoice_claimed=false" in human
    assert "Actual accounted cost:" not in human
    assert "current DeepSeek rates" not in human
    assert "Calculated API cost" not in human


def save_paid_pending(engine):
    project,module,*_=engine
    result=module.journaled_chat_request("chapter_translation",1,module.chapter_map[1]["text"],kwargs())
    module.atomic_write_text(project.pending_dir/"0001.md","A man entered the hall.")
    module.atomic_write_json(project.pending_dir/"0001.json",{"chapter":1,"stage":"translated",
        "finish_reason":"stop","translation_stats":result["response"]["stats"],
        "local_qa":{"commit_allowed":True,"issues":[]}})
    return project


def test_goat_recovery_binding_and_cross_provider_refusal(engine):
    project=save_paid_pending(engine)
    plan=build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_credit_equivalent_usd=1)
    assert plan["provider_identity"]==IDENTITY
    assert plan["pending_provider"]==plan["requested_provider"]==plan["effective_provider"]==GOAT
    assert plan["translation_generation_allowed"] is False
    assert plan["startup_periodic_audits_allowed"] is False
    with pytest.raises(RuntimeError):
        build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",provider_selection="direct_deepseek")
    meta=json.loads((project.pending_dir/"0001.json").read_text(encoding="utf-8"))
    meta.pop("translation_provider")
    (project.pending_dir/"0001.json").write_text(json.dumps(meta),encoding="utf-8")
    with pytest.raises(RuntimeError):
        build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
            provider_selection=GOAT,max_credit_equivalent_usd=1)


@pytest.mark.parametrize("change",["token","state","provider"])
def test_invalid_recovery_authorization_precedes_dpapi(engine,change):
    project=save_paid_pending(engine)
    plan=build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_credit_equivalent_usd=1)
    with pytest.raises(RuntimeError):
        execute_pending_recovery("fixture",repo_root=project.repository_root,model_selection="v41-flash",
            provider_selection="direct_deepseek" if change=="provider" else GOAT,
            max_api_calls=plan["max_api_calls"],max_cost_usd=1 if change=="provider" else None,
            max_credit_equivalent_usd=None if change=="provider" else 1,
            expected_state_fingerprint="0"*64 if change=="state" else plan["state_fingerprint"],
            approval_token="0"*64 if change=="token" else plan["approval_token"],authorize_paid_recovery=True)


def test_goat_recovery_reuses_paid_text_real_lifecycle(engine,tmp_path,monkeypatch,capsys):
    project=save_paid_pending(engine)
    plan=build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_credit_equivalent_usd=1)
    events=[];fake_credentials(monkeypatch,tmp_path,events)
    sends=[]
    def create(**body):
        sends.append(body)
        assert "max_completion_tokens" not in body and "max_tokens" not in body
        return response(content=json.dumps({"new_terms":[],"continuity_summary":"Saved text reused.","issues":[]}))
    monkeypatch.setattr(transport,"create_command_code_client",lambda **k: NS(
        chat=NS(completions=NS(create=create)),close=lambda:events.append("closed")))
    original=launcher.load_production_module
    def load(*a,**k):
        module=original(*a,**k)
        module.local_qa=lambda *a,**k:{"commit_allowed":True,"issues":[]}
        return module
    monkeypatch.setattr(launcher,"load_production_module",load)
    report=execute_pending_recovery("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=plan["max_api_calls"],max_cost_usd=None,max_credit_equivalent_usd=1,
        expected_state_fingerprint=plan["state_fingerprint"],approval_token=plan["approval_token"],authorize_paid_recovery=True)
    assert report["status"]=="COMMITTED",report
    assert len(sends)==report["api_calls"]==1
    from production_accounting import GOAT_METADATA
    persisted = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert all(persisted[key] == value for key, value in GOAT_METADATA.items())
    assert len(report["accepted_provider_responses"]) == 1
    assert report["translation_generation_attempted"] is False
    assert (project.chapter_dir/"0001.md").read_text(encoding="utf-8")=="A man entered the hall."
    assert events==["fake_dpapi","closed"]
    from pending_recovery_execute import _print_human
    _print_human(report)
    human = capsys.readouterr().out
    assert "credit-equivalent USD; provider_invoice_claimed=false" in human
    assert "Actual accounted cost:" not in human
    assert "current DeepSeek rates" not in human
    assert "Calculated API cost" not in human


def test_fabricated_goat_pending_without_journal_is_refused(tmp_path):
    project=_empty_project(tmp_path)
    meta={"chapter":1,"stage":"translated","translation_model":"deepseek-flash",**goat_provenance(IDENTITY,"a"*64)}
    (project.pending_dir/"0001.md").write_text("Historical paid text.",encoding="utf-8")
    (project.pending_dir/"0001.json").write_text(json.dumps(meta),encoding="utf-8")
    result=run_production_path_preflight("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT)
    assert result["status"]=="FAIL"
    assert any("original paid translation journal" in error for error in result["errors"])


def test_imports_never_activate_credentials_network_or_writers(monkeypatch):
    import types
    names=("production_provider_runtime","production_goat_runtime","production_goat_execution")
    sources={name:Path(importlib.import_module(name).__file__).read_text(encoding="utf-8") for name in names}
    def deny(*a,**k): pytest.fail("import-side IO/credentials/client forbidden")
    monkeypatch.setattr(Path,"write_text",deny)
    monkeypatch.setattr(Path,"write_bytes",deny)
    monkeypatch.setattr(Path,"mkdir",deny)
    monkeypatch.setattr(credentials,"read_command_code_api_key",deny)
    monkeypatch.setattr(transport,"create_command_code_client",deny)
    for name,source in sources.items():
        module=types.ModuleType("_inert_"+name)
        exec(compile(source,name,"exec"),module.__dict__)


@pytest.mark.parametrize("budget",[0,-1,float("nan"),float("inf"),True])
def test_goat_budget_rejects_invalid_allowance(budget):
    with pytest.raises(RuntimeError): GoatBudget(1,budget)


def test_credit_units_are_exclusive(tmp_path):
    _empty_project(tmp_path)
    with pytest.raises(ValueError):
        build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection=GOAT,max_cost_usd=1)
    with pytest.raises(ValueError):
        build_controlled_trial_plan("fixture",repo_root=tmp_path,model_selection="v41-flash",provider_selection="direct_deepseek",max_credit_equivalent_usd=1)


@pytest.mark.parametrize("allowance", [0, -1, True, float("nan"), float("inf")])
def test_plan_invalid_credit_ceiling_fails_before_credentials(tmp_path, allowance):
    _empty_project(tmp_path)
    with pytest.raises(ValueError):
        build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash",
            provider_selection=GOAT, max_credit_equivalent_usd=allowance)


def test_double_runtime_install_refused(engine):
    _, module, _, client, *_ = engine
    with pytest.raises(GoatRuntimeError, match="already installed"):
        install_goat_runtime(module, provider_client=client)


@pytest.mark.parametrize("drift", ["novel_id", "profile_id", "source_sha256"])
def test_goat_pending_rejects_original_journal_domain_drift(engine, drift):
    project = save_paid_pending(engine)
    path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record = _updated_record(record, **{drift: "different-domain"})
    path.write_text(json.dumps(record), encoding="utf-8")
    result = run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert result["status"] == "FAIL"
    assert any("novel/profile/source" in value for value in result["errors"])


def test_goat_partial_commit_evidence_preserves_exact_provenance(tmp_path):
    from test_production_partial_commit_reconciliation import _fixture, _inspect
    from production_partial_commit_reconciliation import PartialCommitReconciliationError
    parts = _fixture(tmp_path)
    pending, meta_path, final, qa_path, meta, _ = parts
    meta.update(translation_model="deepseek-flash", **goat_provenance(IDENTITY, "a" * 64))
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    qa = {**meta["local_qa"], **canonical_pending_provenance(meta)}
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    final.write_bytes(pending.read_bytes())
    assert _inspect(parts)["mode"] == "reconcile_reviewed_partial_commit_tail"
    qa["translation_request_sha256"] = "b" * 64
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    with pytest.raises(PartialCommitReconciliationError):
        _inspect(parts)



def test_closed_credential_scope_drops_transport_and_cannot_send(engine):
    project, module, sends, _, _, _, runtime = engine
    runtime["release_transport"]()
    with pytest.raises(GoatRuntimeError, match="scope is closed"):
        module.journaled_chat_request("chapter_translation", 1, "甲", kwargs())
    assert sends == []
    path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "planned"



def test_goat_completed_pending_cleanup_requires_exact_provider_qa(tmp_path):
    from test_production_completed_pending_cleanup import _fixture
    from production_completed_pending_cleanup import inspect_completed_pending_cleanup, CompletedPendingCleanupError
    from production_saved_checkpoint_identity import _finalize_identity
    parts = _fixture(tmp_path)
    meta = parts["meta"]
    meta.update(translation_model="deepseek-flash", **goat_provenance(IDENTITY, "a" * 64))
    meta.update(canonical_pending_provenance(meta))
    for name in ("state_checkpoint_identity", "review_checkpoint_identity"):
        value = dict(meta[name]); value.pop("identity_sha256")
        value.update(model_id="deepseek-flash", provider_identity=IDENTITY, wire_max_completion_tokens=8000)
        meta[name] = _finalize_identity(value)
    parts["pending_json"].write_text(json.dumps(meta), encoding="utf-8")
    qa = {**meta["local_qa"], **canonical_pending_provenance(meta)}
    parts["qa_path"].write_text(json.dumps(qa), encoding="utf-8")
    assert inspect_completed_pending_cleanup(parts["project"]).chapters == (1,)
    qa["translation_provider"] = "direct_deepseek"
    parts["qa_path"].write_text(json.dumps(qa), encoding="utf-8")
    with pytest.raises(CompletedPendingCleanupError):
        inspect_completed_pending_cleanup(parts["project"])



@pytest.mark.parametrize("route", ["translate_cli", "launcher", "launcher_main"])
def test_public_unattended_goat_refuses_before_credentials_or_network(monkeypatch, route):
    import translate
    monkeypatch.setenv(PROVIDER_ENV, GOAT)
    def denied(*a, **k): pytest.fail("unbounded route reached activation/dispatch/state")
    monkeypatch.setattr(launcher, "activated_goat_transport", denied)
    monkeypatch.setattr(launcher, "reconcile_completed_pending_before_runtime", denied)
    monkeypatch.setattr(translate.subprocess, "run", denied)
    monkeypatch.setattr(translate, "load_novel_project", denied)
    with pytest.raises(ValueError, match="unattended production is disabled"):
        if route == "translate_cli":
            translate.main(["--provider", GOAT, "--model", "v41-flash", "--novel", "fixture"])
        elif route == "launcher":
            launcher._run_production()
        else:
            launcher.main()


def test_goat_activation_without_bounded_budget_refuses_before_dpapi():
    with pytest.raises(RuntimeError, match="bounded budget"):
        with launcher.activated_goat_transport(NS(MODEL="deepseek-flash"), IDENTITY):
            pytest.fail("unbounded activation permitted")


@pytest.mark.parametrize("bad", ["usage", "model"])
def test_unaccounted_response_reporting_survives_fresh_execution_replay(tmp_path, monkeypatch, bad, capsys):
    project = _empty_project(tmp_path)
    events = []
    fake_credentials(monkeypatch, tmp_path, events)
    sends = []
    def create(**body):
        sends.append(body)
        return response(usage=bad != "usage", model="wrong-model" if bad == "model" else None)
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: NS(
        chat=NS(completions=NS(create=create)), close=lambda: events.append("closed")))
    reports = []
    for _ in range(2):
        # A new executor invocation loads a fresh production module and budget;
        # its only response evidence on the second invocation is the journal.
        plan = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash",
            provider_selection=GOAT, max_api_calls=4, max_credit_equivalent_usd=1)
        reports.append(execute_controlled_trial("fixture", repo_root=tmp_path, model_selection="v41-flash",
            provider_selection=GOAT, max_api_calls=4, max_cost_usd=None, max_credit_equivalent_usd=1,
            expected_state_fingerprint=plan["state_fingerprint"], approval_token=plan["approval_token"],
            authorize_paid_trial=True))
        record = json.loads((project.data_dir / "provider_request_journal/0001.chapter_translation.json").read_text(encoding="utf-8"))
        assert record["status"] == "response_recorded"
        assert "stats" not in record["response"]
        assert reports[-1]["unaccounted_provider_response"] is True
        assert reports[-1]["actual_credit_equivalent_usd"] == 0
    assert len(sends) == 1
    assert reports[0]["api_calls"] == 1
    assert reports[1]["api_calls"] == 0
    assert reports[1]["provider_calls"] == []
    from pending_recovery_execute import _print_human as print_recovery_report
    print_recovery_report(reports[1])
    assert "Unaccounted provider response: YES" in capsys.readouterr().out




def test_goat_pending_operator_command_and_human_units(engine, capsys):
    import operator_preflight as operator
    import pending_recovery_plan as recovery_planner
    project = save_paid_pending(engine)
    report = operator.build_operator_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT, max_api_calls=3, max_credit_equivalent_usd=0.25)
    assert report["status"] == "RECOVERY_READY"
    assert report["recommended_entrypoint"] == "pending_recovery_plan.py"
    assert report["recommended_args"] == ["--provider", GOAT, "--novel", "fixture", "--model", "v41-flash",
        "--max-api-calls", "3", "--max-credit-equivalent-usd", "0.25"]
    assert report["recovery"]["provider_invoice_claimed"] is False
    operator._print_human(report)
    plan = build_pending_recovery_plan("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT, max_api_calls=3, max_credit_equivalent_usd=0.25)
    assert plan["pricing_version"] == IDENTITY["pricing_version"]
    recovery_planner._print_human(plan)
    text = capsys.readouterr().out
    assert "0.2500 credit-equivalent USD" in text
    assert "provider_invoice_claimed=false" in text
    assert "Future cost ceiling" not in text and "USD 0.2500" not in text


def test_goat_trial_planner_human_units(tmp_path, capsys):
    import controlled_trial_plan as planner
    _empty_project(tmp_path)
    plan = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash",
        provider_selection=GOAT, max_credit_equivalent_usd=0.25)
    assert plan["pricing_version"] == IDENTITY["pricing_version"]
    assert plan["thinking"] == "low" and plan["max_output_tokens"] == 50000
    planner._print_human(plan)
    text = capsys.readouterr().out
    assert "0.2500 credit-equivalent USD" in text and "provider_invoice_claimed=false" in text
    assert "Future trial cost ceiling" not in text


def test_production_50k_wire_identity_and_worst_case_budget():
    from benchmark_longitudinal_provider import build_provider_request_identity
    from benchmark_longitudinal_g7d_accounting_policy import pricing_snapshot
    wire = normalize_production_request("translation", kwargs())
    historical = copy.deepcopy(wire); historical["max_completion_tokens"] = 24000
    assert build_provider_request_identity(request_body=wire).request_sha256 != build_provider_request_identity(request_body=historical).request_sha256
    rates = pricing_snapshot(IDENTITY["wire_model"], CLOCK())
    prompt = len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) + 8192
    old_bound = (prompt * rates["cache_miss_rate_per_million"] + 24000 * rates["output_rate_per_million"]) / 1e6
    budget = GoatBudget(2, old_bound + 0.001)
    with pytest.raises(GoatRuntimeError):
        with budget.authorize(wire, CLOCK()): pass
    assert not budget.api_calls and budget.actual_cost_usd == 0


@pytest.mark.parametrize("drift", ["duplicate", "new_cap", "missing_thinking", "high", "extra_control"])
def test_exact_translation_bridge_rejects_shape_drift(drift):
    body = kwargs()
    if drift == "duplicate": body["max_completion_tokens"] = 12000
    if drift == "new_cap": body["max_tokens"] = 50000
    if drift == "missing_thinking": body.pop("extra_body")
    if drift == "high": body["reasoning_effort"] = "high"
    if drift == "extra_control": body["temperature"] = 0.1
    with pytest.raises(GoatRuntimeError): normalize_production_request("translation", body)
