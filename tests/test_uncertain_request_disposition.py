"""Disposable live-shaped incident and fake transports; no production execution."""
import copy
import json
from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from test_production_goat_runtime import engine, zero_network, response, CLOCK, GOAT, fake_credentials
from production_paid_request_journal import _sha256_json, _updated_record, request_identity, paid_request_journal_path, run_journaled_paid_request
from production_provider_runtime import provider_identity, goat_provenance
from production_goat_policy import bind_auxiliary_checkpoint
from production_goat_cap_recovery import accepted_envelope
from production_goat_runtime import raw_response_record, normalize_production_request
from benchmark_longitudinal_provider import build_provider_request_identity
from goat_state_policy_migration_plan import definitions
from uncertain_request_disposition_plan import build_uncertain_request_disposition_plan as build_plan, snapshot
from uncertain_request_disposition_execute import execute_uncertain_request_disposition as execute
from production_uncertain_request_disposition import (KIND, MARKER, text_sha, install_uncertain_recovery,
    save_uncertain_state_checkpoint, registry_path, claim_path, write_disposition, load_immutable)
from production_goat_state_terminal_recovery import RECEIPTS
from translation_model_config import pending_translation_model_record
from noveltranslator_core import atomic_write_json

STATE = dict(new_terms=[], continuity_summary="A man entered.", issues=[])


def plan(project, **kwargs):
    return build_plan("fixture", repo_root=project.repository_root, operator_evidence=kwargs.get("evidence", evidence(project)), max_api_calls=kwargs.get("max_api_calls", 6),
        max_credit_equivalent_usd=kwargs.get("max_credit_equivalent_usd", 2))


@pytest.fixture
def incident(engine, request):
    project, module, sends, *_ = engine
    policy = provider_identity(GOAT, "v41-flash")
    config = json.loads(project.config_file.read_text())
    config["final_chapter"] = 363
    atomic_write_json(project.config_file, config)
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, final_chapter=363)
    source = module.chapter_map[1]["text"]
    module.chapter_map = {n:dict(chapter=n, text=source) for n in range(1, 364)}
    atomic_write_json(project.chapters_file, list(module.chapter_map.values()))
    module.progress.update(completed=list(range(1,362)), last_completed=361, next_chapter=362, last_audited_chapter=350)
    module.continuity.update(latest_chapter=361)
    atomic_write_json(project.progress_file, module.progress)
    atomic_write_json(project.continuity_file, module.continuity)
    for n in range(1,362):
        (project.chapter_dir / f"{n:04d}.md").write_text("Existing English chapter.", encoding="utf-8")
    text = "A man entered the hall."
    if getattr(request, "param", "") in {"crlf_unicode", "bom"}:
        text = "# Chapter 362\n\nCafé — A man entered the hall.\n"
        if request.param == "bom":
            text = "\ufeff" + text
    (project.pending_dir / "0362.md").write_text(text, encoding="utf-8")
    def record(kind, wire, raw, translation=""):
        expected = request_identity(operation_kind=kind, novel_id="fixture", chapter=362,
            provider=GOAT, model=policy["wire_model"], profile_id=project.config.profile,
            source_sha256=text_sha(source), translation_sha256=text_sha(translation) if translation else "",
            request_payload={"wire_request":wire, "provider_policy":policy})
        expected.update(provider_policy=policy, wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
        return run_journaled_paid_request(journal_path=paid_request_journal_path(project.data_dir / "provider_request_journal", kind, 362),
            identity=expected, save_json=atomic_write_json, prepare_attempt=lambda:dict(request_started_utc=CLOCK().replace(hour=12).isoformat()),
            send_once=lambda:raw, normalize_response=lambda raw, context:accepted_envelope(dict(raw_response=raw_response_record(raw), **context), wire))
    translation_wire = dict(model=policy["wire_model"], reasoning_effort="low", max_completion_tokens=50000,
        messages=[{"role":"user", "content":source}])
    raw_translation = response(content=text, completion=20120)
    raw_translation.usage.prompt_tokens = 8675
    raw_translation.usage.total_tokens = 28795
    raw_translation.usage.prompt_tokens_details.cached_tokens = 4096
    paid = record("chapter_translation", translation_wire, raw_translation)
    defs = definitions(project)
    meta = dict(chapter=362, stage="translated", translation_stats=paid["response"]["stats"], finish_reason="stop",
        local_qa=defs.local_qa(362, source, text, "stop"), **pending_translation_model_record("v41-flash"),
        **goat_provenance(policy, paid["response"]["request_sha256"]))
    meta.update(translation_max_tokens=50000, translation_thinking="low", translation_pricing_version=policy["pricing_version"])
    meta["state_checkpoint_identity"] = bind_auxiliary_checkpoint(defs.current_state_checkpoint_identity(
        362, source, text, prior_continuity=module.continuity), policy)
    atomic_write_json(project.pending_dir / "0362.json", meta)
    wire = normalize_production_request("state_extraction", dict(model="deepseek-flash", messages=[
        {"role":"system", "content":defs.STATE_PROMPT},
        {"role":"user", "content":defs.state_extraction_user_prompt(chapter=362, existing_terms=[],
            prior=module.continuity, source=source, translation=text)}], **defs.operation_request_controls("state_extraction")))
    from production_paid_request_journal import _new_planned_record
    expected = request_identity(operation_kind="state_extraction", novel_id="fixture", chapter=362,
        provider=GOAT, model=policy["wire_model"], profile_id=project.config.profile,
        source_sha256=text_sha(source), translation_sha256=text_sha(text),
        request_payload={"wire_request":wire, "provider_policy":policy})
    expected.update(provider_policy=policy, wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
    uncertain = _updated_record(_new_planned_record(expected), status="uncertain",
        attempt_context=dict(request_started_utc="2026-10-13T19:33:22.108216Z"),
        error=dict(type="GoatRuntimeError", message="transport outcome uncertain"))
    uncertain["updated_utc"] = "2026-10-13T19:33:22.689115Z"
    from production_paid_request_journal import _with_record_hash
    atomic_write_json(project.data_dir / "provider_request_journal/0362.state_extraction.json", _with_record_hash(uncertain))
    strict_wire = dict(model=policy["wire_model"], reasoning_effort="low", messages=[{"role":"user","content":"Exact rejected old repair"}])
    strict = record("strict_terminology_micro_repair", strict_wire, response(content='{"patches":[]}'), text)
    from noveltranslator_core import add_accounting_totals_once
    add_accounting_totals_once(module.progress, strict["operation_id"], strict["response"]["stats"])
    atomic_write_json(project.progress_file, module.progress)
    assert not sends
    return engine


def activation(monkeypatch, tmp_path, incident, responses):
    import command_code_provider_transport as transport
    import run_production_engine as launcher
    events, sent = [], []
    import command_code_provider_credentials as credentials
    from contextlib import contextmanager
    @contextmanager
    def synthetic_scope():
        yield "synthetic-test-only-no-credential-read"
    monkeypatch.setattr(credentials, "command_code_api_key_scope", synthetic_scope)
    monkeypatch.setattr(credentials, "read_command_code_api_key", lambda *a, **k:pytest.fail("credential read forbidden"))
    def send(**body):
        sent.append(body)
        assert body["reasoning_effort"] == "low"
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        return responses.pop(0)
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k:NS(chat=NS(completions=NS(create=send)), close=lambda:events.append("closed")))
    original = launcher.load_production_module
    def load(*a, **k):
        module = original(*a, **k)
        module.run_periodic_audit = lambda *a, **k:pytest.fail("startup audit forbidden")
        return module
    monkeypatch.setattr(launcher, "load_production_module", load)
    return sent, events


def run(project, p, authorize=True):
    return execute("fixture", repo_root=project.repository_root, max_api_calls=6, max_credit_equivalent_usd=2,
        expected_state_fingerprint=p["state_fingerprint"], approval_token=p["approval_token"],
        operator_evidence=evidence(project), authorize_uncertain_no_provider_record_disposition=authorize)



def evidence(project):
    from pending_recovery_plan import _sha256_file
    path = project.data_dir / "provider_request_journal/0362.state_extraction.json"
    record = json.loads(path.read_text())
    paid = json.loads((project.data_dir / "provider_request_journal/0362.chapter_translation.json").read_text())
    return dict(type="operator_reviewed_external_provider_usage", operator="synthetic operator",
        attestation="I reviewed authoritative provider Usage and attest that this exact request was not recorded or metered.",
        provider_proof_claimed=False, reviewed_utc="2026-10-13T20:00:00Z",
        **{k:record[k] for k in ("operation_id","request_sha256","wire_request_sha256","provider","model")},
        journal_sha256=_sha256_file(path), usage_window=dict(timezone="America/Lima",
            start_local="2026-10-13T14:33:00-05:00", end_local="2026-10-13T14:34:00-05:00",
            uncertain_started_local="2026-10-13T14:33:22.108216-05:00"),
        preceding_api=dict(type="api", status="COMPLETED", timestamp_local="2026-10-13T14:33:21-05:00",
            model=record["model"], input_tokens=8675, output_tokens=20120,
            displayed_cost=f'{paid["response"]["stats"]["cost_usd"]:.4f}',
            matches_durable_translation=True, translation_request_sha256=paid["wire_request_sha256"]),
        no_subsequent_api_row_attributable_before_agent_traffic=True, later_traffic_type="agent",
        incident_reference=dict(operation_id="9135247c738938bac1970365a79571a2815ec32f57dd5787680378a0c095071a",
            request_sha256="205c8ce23120cbe919cbd09171f53a376b60c0c484359c0ea55ade50131d93cb",
            wire_request_sha256="e855754dafcbe436a300809fb7a2c618bed1d410562bf3692cee22f8ff9905ff",
            exact_ce=0.012771138, displayed_cost="$0.0128"))


def old_path(project):
    return project.data_dir / "provider_request_journal/0362.state_extraction.json"


def state_path(project):
    return project.pending_dir / "0362.json"


def test_readonly_and_ordinary_uncertain_blocks(incident):
    project = incident[0]
    before = snapshot(project)
    p = plan(project)
    assert p == plan(project) and snapshot(project) == before
    assert p["status"] == "READY" and p["target_chapter"] == 362
    assert p["maximum_new_recovery_sends"] == 1
    assert p["bindings"]["target"]["translation_journal"]["response"]["stats"]["cost_usd"] == 0.012771138
    assert p["api_calls"] == p["credential_reads"] == p["project_writes"] == 0
    assert p["recovery_identity"]["operation_id"] != p["disposition_proof"]["original_operation_id"]
    from production_path_preflight import run_production_path_preflight
    q = run_production_path_preflight("fixture",repo_root=project.repository_root,
        model_selection="v41-flash",provider_selection=GOAT)
    assert q["status"] != "PASS" and len(q["uncertain_paid_requests"]) == 1
    from pending_recovery_plan import build_pending_recovery_plan
    with pytest.raises(RuntimeError):
        build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
            provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)
    module = incident[1]
    from production_paid_request_journal import PaidRequestUncertainError, load_matching_journal
    record = load_matching_journal(old_path(project), {})
    with pytest.raises(PaidRequestUncertainError):
        run_journaled_paid_request(journal_path=old_path(project),identity=p["disposition_proof"]["original_identity"],
            save_json=atomic_write_json, prepare_attempt=lambda:{}, send_once=lambda:pytest.fail("resend"),
            normalize_response=lambda *a:{})


@pytest.mark.parametrize("key,value",[("type",None),("operator",""),("attestation",None),
    ("provider_proof_claimed",True),("journal_sha256","0"*64),("operation_id","0"*64),
    ("request_sha256","0"*64),("wire_request_sha256","0"*64),("provider","wrong"),("model","wrong"),
    ("no_subsequent_api_row_attributable_before_agent_traffic",False)])
def test_missing_or_wrong_operator_evidence(incident,key,value):
    project = incident[0]
    e = evidence(project); e[key] = value
    with pytest.raises((RuntimeError,ValueError,KeyError,TypeError)):
        plan(project,evidence=e)
    with pytest.raises(RuntimeError):
        plan(project,evidence=None)


@pytest.mark.parametrize("mutation",["started","planned","response","usage","accounted","request","wire","provider","model","journal_hash"])
def test_original_evidence_blocks(incident,mutation):
    project = incident[0]; e = evidence(project)
    path = old_path(project); record = json.loads(path.read_text())
    changes = {}
    if mutation in {"started","planned"}: changes["status"] = mutation
    if mutation == "response": changes["response"] = {"content":"present"}
    if mutation == "usage": changes["usage"] = {"prompt_tokens":0}
    if mutation in {"request","wire"}: changes[mutation+"_request_sha256" if mutation == "wire" else "request_sha256"] = "0"*64
    if mutation in {"provider","model"}: changes[mutation] = "wrong"
    if mutation == "accounted":
        progress = json.loads(project.progress_file.read_text())
        progress["accounted_api_operation_ids"].append(record["operation_id"])
        atomic_write_json(project.progress_file,progress)
    elif mutation == "journal_hash":
        record["record_sha256"] = "0"*64; atomic_write_json(path,record)
    else: atomic_write_json(path,_updated_record(record,**changes))
    with pytest.raises((RuntimeError,ValueError,KeyError)):
        plan(project,evidence=e)


@pytest.mark.parametrize("mutation",["translation","source","progress","continuity","glossary","state","later","multiple"])
def test_context_drift_blocks(incident,mutation):
    project = incident[0]; p = plan(project)
    if mutation == "translation": (project.pending_dir/"0362.md").write_text("Drift")
    if mutation == "source":
        rows = json.loads(project.chapters_file.read_text()); rows[361]["text"] += "changed"
        atomic_write_json(project.chapters_file,rows)
    if mutation == "progress":
        value = json.loads(project.progress_file.read_text()); value["next_chapter"] = 363
        atomic_write_json(project.progress_file,value)
    if mutation == "continuity":
        value = json.loads(project.continuity_file.read_text()); value["latest_chapter"] = 360
        atomic_write_json(project.continuity_file,value)
    if mutation == "glossary":
        value = json.loads(project.glossary_file.read_text()); value["entries"]["甲"] = {"english":"Changed", "status":"locked"}
        atomic_write_json(project.glossary_file,value)
    if mutation == "state":
        value = json.loads(state_path(project).read_text()); value.update(stage="state_extracted",state=STATE)
        atomic_write_json(state_path(project),value)
    if mutation == "later": (project.chapter_dir/"0363.md").write_text("Later work")
    if mutation == "multiple":
        atomic_write_json(project.data_dir/"provider_request_journal/0361.state_extraction.json",json.loads(old_path(project).read_text()))
    with pytest.raises((RuntimeError,ValueError,KeyError)):
        plan(project)
    assert not incident[2]


def test_flag_stale_token_and_budget(incident):
    project = incident[0]; p = plan(project); before = snapshot(project)
    assert run(project,p,False)["status"] == "AUTHORIZATION_REQUIRED"
    assert snapshot(project) == before
    q = copy.deepcopy(p); q["approval_token"] = "0"*64
    with pytest.raises(RuntimeError,match="authorization drift"): run(project,q)
    with pytest.raises((RuntimeError,ValueError)): plan(project,max_api_calls=0)
    with pytest.raises(RuntimeError): plan(project,max_credit_equivalent_usd=0.000001)
    assert snapshot(project) == before
    value = json.loads(project.progress_file.read_text()); value["last_audited_chapter"] = 349
    atomic_write_json(project.progress_file,value)
    with pytest.raises(RuntimeError,match="authorization drift"): run(project,p)


@pytest.mark.parametrize("with_repair", [False, True])
def test_success_then_pending_commit(incident,monkeypatch,tmp_path,with_repair):
    project = incident[0]; p = plan(project)
    before = json.loads(project.progress_file.read_text()); original = old_path(project).read_bytes()
    paid = (project.pending_dir/"0362.md").read_bytes()
    replies = [response(content=json.dumps(STATE))]
    if with_repair:
        replies.insert(0,response(content='{"new_terms": [] "continuity_summary":"A man entered.", "issues": []}'))
    expected_sends = 1 + int(with_repair)
    sent,_ = activation(monkeypatch,tmp_path,incident,replies)
    report = run(project,p)
    assert report["status"] == "STATE_CHECKPOINT_SAVED",report
    assert len(sent) == report["api_calls"] == expected_sends and sent[0] == p["recovery_wire"]
    assert json.loads(project.progress_file.read_text()) == before
    assert old_path(project).read_bytes() == original and (project.pending_dir/"0362.md").read_bytes() == paid
    d = load_immutable(registry_path(project,362)); assert d["provider_usage"] == "zero" and d["provider_usage_disposition"] == "not_recorded"
    meta = json.loads(state_path(project).read_text()); assert meta["stage"] == "state_extracted" and meta["state"] == STATE
    assert run(project,plan(project))["api_calls"] == 0 and len(sent) == expected_sends
    from pending_recovery_plan import build_pending_recovery_plan
    from pending_recovery_execute import execute_pending_recovery
    q = build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)
    assert q["status"] == "READY"
    result = execute_pending_recovery("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=6,max_cost_usd=None,max_credit_equivalent_usd=2,
        expected_state_fingerprint=q["state_fingerprint"],approval_token=q["approval_token"],authorize_paid_recovery=True)
    assert result["status"] == "COMMITTED",result
    after = json.loads(project.progress_file.read_text())
    assert after["last_completed"] == 362 and after["next_chapter"] == 363 and after["last_audited_chapter"] == 350
    assert p["disposition_proof"]["original_operation_id"] not in after["accounted_api_operation_ids"]
    assert after["estimated_cost_usd"] - before["estimated_cost_usd"] == pytest.approx(meta["translation_stats"]["cost_usd"] + meta["state_stats"]["cost_usd"])
    assert len(sent) == expected_sends and not (project.chapter_dir/"0363.md").exists()
    assert old_path(project).read_bytes() == original
    from production_path_preflight import run_production_path_preflight
    post = run_production_path_preflight("fixture",repo_root=project.repository_root,model_selection="v41-flash",provider_selection=GOAT)
    assert post["status"] == "PASS",post["errors"]
    # Normal chapter accounting may register operation IDs without making
    # detached terminal receipts. Its checkpoint subtotal must stay unchanged.
    for kind in ([KIND,"state_json_repair"] if with_repair else [KIND]):
        record = json.loads((project.data_dir/f"provider_request_journal/0362.{kind}.json").read_text())
        operation = record["operation_id"]
        assert operation not in after.get(RECEIPTS,{})
        if operation not in after.setdefault("accounted_api_operation_ids",[]):
            after["accounted_api_operation_ids"].append(operation)
    atomic_write_json(project.progress_file,after)
    post = run_production_path_preflight("fixture",repo_root=project.repository_root,model_selection="v41-flash",provider_selection=GOAT)
    assert post["status"] == "PASS",post["errors"]


@pytest.mark.parametrize("finish,content",[("content_filter",""),("length","partial"),("stop"," "),("tool_calls",""),(None,"")])
def test_terminal_no_third_generation(incident,monkeypatch,tmp_path,finish,content):
    project = incident[0]; original = old_path(project).read_bytes()
    sent,_ = activation(monkeypatch,tmp_path,incident,[response(content=content,finish=finish)])
    report = run(project,plan(project)); assert report["status"] == "FAILED_AFTER_AUTHORIZATION" and len(sent) == 1
    resumed = plan(project); assert resumed["status"] == "TERMINAL" and resumed["recovery_generation_sends_remaining"] == 0
    run(project,resumed); assert len(sent) == 1 and old_path(project).read_bytes() == original


def test_recovery_uncertain_stops_forever(incident,monkeypatch,tmp_path):
    project = incident[0]; p = plan(project); original = old_path(project).read_bytes()
    import command_code_provider_transport as transport
    sent,_ = activation(monkeypatch,tmp_path,incident,[])
    def fail(**body): sent.append(body); raise TimeoutError("synthetic timeout")
    monkeypatch.setattr(transport,"create_command_code_client",lambda **k:NS(chat=NS(completions=NS(create=fail)),close=lambda:None))
    report = run(project,p); assert report["status"] == "FAILED_AFTER_AUTHORIZATION" and len(sent) == 1
    with pytest.raises(RuntimeError): plan(project)
    assert old_path(project).read_bytes() == original


@pytest.mark.parametrize("finish",["stop","content_filter"])
def test_optional_standard_json_repair(incident,monkeypatch,tmp_path,finish):
    project = incident[0]
    malformed = '{"new_terms": [] "continuity_summary":"A man entered.", "issues": []}'
    sent,_ = activation(monkeypatch,tmp_path,incident,[response(content=malformed),response(content=json.dumps(STATE) if finish == "stop" else "",finish=finish)])
    report = run(project,plan(project)); assert len(sent) == 2,report
    assert report["status"] == ("STATE_CHECKPOINT_SAVED" if finish == "stop" else "FAILED_AFTER_AUTHORIZATION")
    run(project,plan(project)); assert len(sent) == 2


def test_crash_after_disposition_and_recorded_before_checkpoint(incident):
    project,module,sends,client,*_ = incident
    p = plan(project); write_disposition(p)
    q = plan(project); install_uncertain_recovery(module,q)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    module.extract_state(362,module.chapter_map[362]["text"],(project.pending_dir/"0362.md").read_text(),prior_continuity=module.continuity)
    assert len(sends) == 1
    q = plan(project); install_uncertain_recovery(module,q); save_uncertain_state_checkpoint(module,q)
    assert len(sends) == 1 and plan(project)["saved_state"]
    q = plan(project); install_uncertain_recovery(module,q); save_uncertain_state_checkpoint(module,q)
    assert len(sends) == 1


@pytest.mark.parametrize("mutation",["delete","planned","started","uncertain"])
def test_consumed_slot_cannot_reopen(incident,mutation):
    project,module,sends,client,*_ = incident; p = plan(project)
    install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    module.extract_state(362,module.chapter_map[362]["text"],(project.pending_dir/"0362.md").read_text(),prior_continuity=module.continuity)
    path = project.data_dir/f"provider_request_journal/0362.{KIND}.json"
    if mutation == "delete": path.unlink()
    else: atomic_write_json(path,_updated_record(json.loads(path.read_text()),status=mutation,response={},response_sha256=""))
    with pytest.raises(RuntimeError): plan(project)
    assert len(sends) == 1


def test_crash_claim_before_started_is_fail_closed(incident):
    from production_uncertain_request_disposition import claim_send
    project,module,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    claim_send(module,KIND,p["recovery_identity"])
    with pytest.raises(RuntimeError,match="consumed"): plan(project)


def test_immutable_partial_sidecar_and_proof_drift(incident):
    project = incident[0]; p = plan(project); write_disposition(p)
    with pytest.raises(FileExistsError):
        from production_uncertain_request_disposition import publish_immutable
        publish_immutable(registry_path(project,362),{})
    value = json.loads(project.issues_file.read_text()); value["issues"] = ["drift"]
    atomic_write_json(project.issues_file,value)
    with pytest.raises(RuntimeError,match="disposition drift"): plan(project)


def test_executor_budget_refusal_pre_send(incident):
    from production_goat_runtime import GoatBudget
    project,module,sends,*_ = incident; p = plan(project)
    install_uncertain_recovery(module,p)
    # The installed runtime budget is bound; consume its allowance before send.
    budget = incident[4]; budget.api_calls = budget.max_api_calls
    with pytest.raises(RuntimeError): save_uncertain_state_checkpoint(module,p)
    assert not sends and not claim_path(registry_path(project,362),KIND).exists()


def test_checkpoint_failure_replay_once_accounted(incident):
    from pathlib import Path
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    original = module.atomic_write_json
    def fail(path,value):
        if Path(path).name == "0362.json" and value.get("stage") == "state_extracted": raise OSError("synthetic disk failure")
        return original(path,value)
    module.atomic_write_json = fail
    with pytest.raises(RuntimeError): save_uncertain_state_checkpoint(module,p)
    module.atomic_write_json = original
    q = plan(project); install_uncertain_recovery(module,q); save_uncertain_state_checkpoint(module,q)
    assert len(sends) == 1 and plan(project)["saved_state"]
    assert json.loads(state_path(project).read_text())["state_stats"]["cost_usd"] == 0


@pytest.mark.parametrize("field,value",[("input_tokens",1),("output_tokens",1),("model","wrong"),
    ("displayed_cost","0.0000"),("matches_durable_translation",False),("type","agent")])
def test_provider_usage_chronology_facts_required(incident,field,value):
    project = incident[0]; e = evidence(project); e["preceding_api"][field] = value
    with pytest.raises(RuntimeError): plan(project,evidence=e)


def test_disposition_alone_cannot_bypass_ordinary_routes(incident):
    project = incident[0]; p = plan(project); write_disposition(p)
    from controlled_trial_plan import build_controlled_trial_plan
    from pending_recovery_plan import build_pending_recovery_plan
    for builder in (build_controlled_trial_plan,build_pending_recovery_plan):
        with pytest.raises(RuntimeError):
            builder("fixture",repo_root=project.repository_root,model_selection="v41-flash",
                provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)


def test_raw_recorded_response_restart_zero_resend(incident):
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    module.extract_state(362,module.chapter_map[362]["text"],(project.pending_dir/"0362.md").read_text(),prior_continuity=module.continuity)
    path = project.data_dir/f"provider_request_journal/0362.{KIND}.json"
    record = json.loads(path.read_text())
    raw = {k:record["response"][k] for k in ("raw_response","request_started_utc")}
    atomic_write_json(path,_updated_record(record,response=raw,response_sha256=_sha256_json(raw)))
    q = plan(project); install_uncertain_recovery(module,q); save_uncertain_state_checkpoint(module,q)
    assert len(sends) == 1 and plan(project)["saved_state"]


def test_partial_disposition_is_never_overwritten(incident):
    project = incident[0]; path = registry_path(project,362); path.parent.mkdir()
    path.write_text('{"incomplete":')
    with pytest.raises((RuntimeError,ValueError)): plan(project)
    assert path.read_text() == '{"incomplete":'


def test_readonly_planner_does_not_read_artifact_env(incident,monkeypatch):
    from pathlib import Path
    project = incident[0]; (project.pending_dir/".env").write_text("synthetic-do-not-read")
    original = Path.open
    def guarded(path,*a,**k):
        if path.name == ".env": pytest.fail("environment file read forbidden")
        return original(path,*a,**k)
    monkeypatch.setattr(Path,"open",guarded)
    assert plan(project)["status"] == "READY"


def test_direct_recovery_needs_scoped_authorization(incident):
    from test_production_goat_runtime import kwargs
    project,module,sends,*_ = incident
    with pytest.raises(RuntimeError,match="scoped"):
        module.journaled_chat_request(KIND,362,module.chapter_map[362]["text"],kwargs(8000),
            translation_text=(project.pending_dir/"0362.md").read_text())
    assert not sends


def test_direct_journal_downgrade_cannot_reopen_claim(incident):
    from test_production_goat_runtime import kwargs
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    source = module.chapter_map[362]["text"]; text = (project.pending_dir/"0362.md").read_text()
    module.extract_state(362,source,text,prior_continuity=module.continuity)
    path = project.data_dir/f"provider_request_journal/0362.{KIND}.json"
    atomic_write_json(path,_updated_record(json.loads(path.read_text()),status="planned",response={},response_sha256=""))
    with pytest.raises(FileExistsError): module.extract_state(362,source,text,prior_continuity=module.continuity)
    assert len(sends) == 1


def test_zero_disposition_rejects_saved_progress_drift(incident):
    project = incident[0]; p = plan(project); write_disposition(p)
    progress = json.loads(project.progress_file.read_text()); progress["total_prompt_tokens"] += 1
    atomic_write_json(project.progress_file,progress)
    with pytest.raises(RuntimeError,match="progress drift"): plan(project)


def test_lock_conflict_refuses_before_send(incident):
    from production_run_lock import production_run_lock
    project = incident[0]; p = plan(project)
    with production_run_lock(project.root):
        with pytest.raises(RuntimeError): run(project,p)
    assert not incident[2] and not registry_path(project,362).exists()


def test_pending_translation_accounting_drift_blocks(incident):
    project = incident[0]; meta = json.loads(state_path(project).read_text())
    meta["translation_stats"]["cost_usd"] += 1
    atomic_write_json(state_path(project),meta)
    with pytest.raises(RuntimeError,match="translation accounting"): plan(project)


@pytest.mark.parametrize("kind",["chapter_translation","state_extraction","state_extraction_recovery","periodic_audit"])
def test_installed_scope_forbids_other_fresh_sends(incident,kind):
    from test_production_goat_runtime import kwargs
    project,module,sends,*_ = incident; install_uncertain_recovery(module,plan(project))
    with pytest.raises(RuntimeError):
        module.journaled_chat_request(kind,363,module.chapter_map[363]["text"],kwargs())
    assert not sends


def test_saved_checkpoint_receipt_drift_blocks_pending_handoff(incident):
    from pending_recovery_plan import build_pending_recovery_plan
    from production_paid_request_journal import _with_record_hash
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:response(content=json.dumps(STATE))
    save_uncertain_state_checkpoint(module,p)
    path = registry_path(project,362).with_suffix(".checkpoint.json")
    record = json.loads(path.read_text()); record["checkpoint"]["state"]["continuity_summary"] = "invented"
    atomic_write_json(path,_with_record_hash(record))
    with pytest.raises(RuntimeError):
        build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
            provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)


def test_checkpoint_receipt_consumes_slot_even_if_claim_and_journal_lost(incident):
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=json.dumps(STATE)))
    save_uncertain_state_checkpoint(module,p)
    (project.data_dir/f"provider_request_journal/0362.{KIND}.json").unlink()
    claim_path(registry_path(project,362),KIND).unlink()
    meta = json.loads(p["disposition_proof"]["original_pending_json"])
    meta[MARKER] = {"proof":p["disposition_proof"]}
    atomic_write_json(state_path(project),meta)
    with pytest.raises(RuntimeError,match="receipt proves consumed"): plan(project)
    assert len(sends) == 1


def recovered_fixture(incident, with_repair=False, *, commit=False):
    """All provider data is injected; verification must never use the client."""
    project,module,sends,client,*_ = incident
    p = plan(project); install_uncertain_recovery(module,p)
    replies = [response(content=json.dumps(STATE))]
    if with_repair:
        replies.insert(0,response(content='{"new_terms": [] "continuity_summary":"A man entered.", "issues": []}'))
    client.chat.completions.create = lambda **body:(sends.append(body) or replies.pop(0))
    save_uncertain_state_checkpoint(module,p)
    client.chat.completions.create = lambda **k:pytest.fail("verification must not send")
    if commit:
        from pending_recovery_plan import build_pending_recovery_plan
        q = build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
            provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)
        assert q["status"] == "READY"
        assert module.process_chapter(362) is True
        assert not state_path(project).exists()
    return project,p,sends


def assert_handoff_blocked(project, *, completed):
    if completed:
        from production_path_preflight import run_production_path_preflight
        result = run_production_path_preflight("fixture",repo_root=project.repository_root,
            model_selection="v41-flash",provider_selection=GOAT)
        assert result["status"] != "PASS"
        assert result["uncertain_paid_requests"]
    else:
        from pending_recovery_plan import build_pending_recovery_plan
        with pytest.raises(RuntimeError):
            build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
                provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)


@pytest.mark.parametrize("with_repair",[False,True])
@pytest.mark.parametrize("completed",[False,True])
@pytest.mark.parametrize("field",["state","state_stats"])
def test_joint_checkpoint_receipt_mutation_cannot_forge_paid_response(incident,with_repair,completed,field):
    from production_paid_request_journal import _with_record_hash
    from production_accounting import combine_goat_stats, validate_goat_stats
    project,p,sends = recovered_fixture(incident,with_repair,commit=completed)
    original = old_path(project).read_bytes()
    journal = project.data_dir/f"provider_request_journal/0362.{KIND}.json"
    paid_bytes = journal.read_bytes()
    path = registry_path(project,362).with_suffix(".checkpoint.json")
    receipt = json.loads(path.read_text())
    if field == "state":
        forged = dict(STATE,continuity_summary="A different but structurally valid event.")
    else:
        raw = response(content=json.dumps(STATE),completion=99)
        start = json.loads(journal.read_text())["response"]["request_started_utc"]
        envelope = accepted_envelope(dict(raw_response=raw_response_record(raw),request_started_utc=start),p["recovery_wire"])
        # A different *valid* accounting subtotal, not an invalid schema/cost.
        items = [envelope["stats"]]
        if with_repair:
            items.append(json.loads((project.data_dir/"provider_request_journal/0362.state_json_repair.json").read_text())["response"]["stats"])
        forged = combine_goat_stats(definitions(project).combine_stats,*items)
        validate_goat_stats(forged)
    receipt["checkpoint"][field] = copy.deepcopy(forged)
    atomic_write_json(path,_with_record_hash(receipt))
    if not completed:
        meta = json.loads(state_path(project).read_text()); meta[field] = copy.deepcopy(forged)
        atomic_write_json(state_path(project),meta)
    assert_handoff_blocked(project,completed=completed)
    assert len(sends) == 1 + int(with_repair)
    assert journal.read_bytes() == paid_bytes and old_path(project).read_bytes() == original


@pytest.mark.parametrize("completed",[False,True])
@pytest.mark.parametrize("finish,content",[("content_filter",""),("length","partial"),("stop"," "),("unknown",""),(None,"")])
def test_terminal_record_cannot_back_a_completed_checkpoint(incident,completed,finish,content):
    from production_paid_request_journal import _with_record_hash
    project,p,sends = recovered_fixture(incident,commit=completed)
    path = project.data_dir/f"provider_request_journal/0362.{KIND}.json"
    record = json.loads(path.read_text())
    envelope = accepted_envelope(dict(raw_response=raw_response_record(response(content=content,finish=finish)),
        request_started_utc=record["response"]["request_started_utc"]),p["recovery_wire"])
    atomic_write_json(path,_updated_record(record,response=envelope,response_sha256=_sha256_json(envelope)))
    receipt_path = registry_path(project,362).with_suffix(".checkpoint.json")
    receipt = json.loads(receipt_path.read_text()); receipt["records"][0]["response_sha256"] = _sha256_json(envelope)
    atomic_write_json(receipt_path,_with_record_hash(receipt))
    assert_handoff_blocked(project,completed=completed)
    assert len(sends) == 1


@pytest.mark.parametrize("field",["request_sha256","wire_request_sha256","translation_sha256","operation_id","model"])
def test_recorded_repair_requires_exact_request_identity(incident,field):
    from production_paid_request_journal import _with_record_hash
    project,p,sends = recovered_fixture(incident,with_repair=True)
    path = project.data_dir/"provider_request_journal/0362.state_json_repair.json"
    record = json.loads(path.read_text()); record[field] = "wrong" if field == "model" else "0"*64
    atomic_write_json(path,_with_record_hash(record))
    claim = claim_path(registry_path(project,362),"state_json_repair")
    value = json.loads(claim.read_text()); value["recovery_identity"][field] = record[field]
    atomic_write_json(claim,_with_record_hash(value))
    receipt_path = registry_path(project,362).with_suffix(".checkpoint.json")
    receipt = json.loads(receipt_path.read_text()); receipt["records"][1]["operation_id"] = record["operation_id"]
    atomic_write_json(receipt_path,_with_record_hash(receipt))
    assert_handoff_blocked(project,completed=False)
    assert len(sends) == 2


def directory_link(link, target):
    """Use symlinks where available, otherwise an unprivileged Windows junction."""
    import os, subprocess
    try:
        link.symlink_to(target,target_is_directory=True)
    except OSError:
        if os.name != "nt": raise
        result = subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],capture_output=True)
        assert result.returncode == 0, "synthetic directory link creation failed"


def test_symlink_registry_blocks_planner_executor_and_all_publication(incident,monkeypatch,tmp_path):
    from production_uncertain_request_disposition import publish_immutable, write_checkpoint_receipt, claim_send
    from semantic_integrity_audit import protected_state_snapshot
    import production_goat_execution
    project,module,sends,*_ = incident; p = plan(project)
    protected = protected_state_snapshot(project); original = old_path(project).read_bytes()
    target = tmp_path/"outside_novel"; target.mkdir()
    link = project.data_dir/"uncertain_request_dispositions"
    directory_link(link,target)
    monkeypatch.setattr(production_goat_execution,"execute_authorized_goat",lambda *a,**k:pytest.fail("provider activation forbidden"))
    module._uncertain_disposition_plan = p
    for action in (lambda:plan(project), lambda:run(project,p), lambda:write_disposition(p),
        lambda:publish_immutable(link/"0362.json",{}),
        lambda:claim_send(module,KIND,p["recovery_identity"]), lambda:write_checkpoint_receipt(p)):
        with pytest.raises(RuntimeError,match="symlink/reparse"): action()
    assert list(target.iterdir()) == [] and not sends
    assert old_path(project).read_bytes() == original and protected_state_snapshot(project) == protected


def test_unsafe_registry_ancestor_and_traversal_block_without_writes(incident,tmp_path):
    from production_uncertain_request_disposition import publish_immutable, require_safe_registry_path
    from semantic_integrity_audit import protected_state_snapshot
    project = incident[0]; before = protected_state_snapshot(project)
    target = tmp_path/"ancestor_target"; target.mkdir()
    link = tmp_path/"aliased_novel"; directory_link(link,target)
    path = link/"data"/"uncertain_request_dispositions"/"0362.json"
    with pytest.raises(RuntimeError,match="symlink/reparse"): publish_immutable(path,{})
    with pytest.raises(RuntimeError,match="outside novel root"):
        require_safe_registry_path(project.data_dir/"uncertain_request_dispositions"/".."/"escape.json")
    assert list(target.iterdir()) == [] and protected_state_snapshot(project) == before


@pytest.mark.parametrize("with_repair",[False,True])
def test_detached_settlement_zero_subtotal_handoff_and_postcommit_pass(incident,with_repair):
    from pathlib import Path
    from pending_recovery_plan import build_pending_recovery_plan
    from production_path_preflight import run_production_path_preflight
    project,module,sends,client,*_ = incident; p = plan(project); install_uncertain_recovery(module,p)
    replies = [response(content=json.dumps(STATE))]
    if with_repair:
        replies.insert(0,response(content='{"new_terms": [] "continuity_summary":"A man entered.", "issues": []}'))
    client.chat.completions.create = lambda **body:(sends.append(body) or replies.pop(0))
    original = module.atomic_write_json
    def fail_checkpoint(path,value):
        if Path(path).name == "0362.json" and value.get("stage") == "state_extracted":
            raise OSError("synthetic checkpoint failure")
        return original(path,value)
    module.atomic_write_json = fail_checkpoint
    with pytest.raises(RuntimeError): save_uncertain_state_checkpoint(module,p)
    module.atomic_write_json = original
    q = plan(project); install_uncertain_recovery(module,q); save_uncertain_state_checkpoint(module,q)
    assert json.loads(state_path(project).read_text())["state_stats"]["cost_usd"] == 0
    before_commit = json.loads(project.progress_file.read_text())
    assert len(before_commit[RECEIPTS]) == 1 + int(with_repair)
    q = build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)
    assert q["status"] == "READY"
    client.chat.completions.create = lambda **k:pytest.fail("no resend or repair on commit/replay")
    assert module.process_chapter(362) is True
    post = run_production_path_preflight("fixture",repo_root=project.repository_root,
        model_selection="v41-flash",provider_selection=GOAT)
    assert post["status"] == "PASS",post["errors"]
    assert len(sends) == 1 + int(with_repair)
