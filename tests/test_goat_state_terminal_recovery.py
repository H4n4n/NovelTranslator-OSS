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
from goat_state_terminal_recovery_plan import build_goat_state_terminal_recovery_plan as build_plan, snapshot
from goat_state_terminal_recovery_execute import execute_goat_state_terminal_recovery as execute
from production_goat_state_terminal_recovery import KIND, MARKER, RECEIPTS, text_sha, install_terminal_state_recovery, save_terminal_state_checkpoint
from translation_model_config import pending_translation_model_record
from noveltranslator_core import atomic_write_json

STATE = dict(new_terms=[], continuity_summary="A man entered.", issues=[])


def plan(project, **kwargs):
    return build_plan("fixture", repo_root=project.repository_root, max_api_calls=kwargs.get("max_api_calls", 6),
        max_credit_equivalent_usd=kwargs.get("max_credit_equivalent_usd", 2))


@pytest.fixture
def incident(engine):
    project, module, sends, *_ = engine
    policy = provider_identity(GOAT, "v41-flash")
    config = json.loads(project.config_file.read_text())
    config["final_chapter"] = 403
    atomic_write_json(project.config_file, config)
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, final_chapter=403)
    source = module.chapter_map[1]["text"]
    module.chapter_map = {n:dict(chapter=n, text=source) for n in range(1, 404)}
    atomic_write_json(project.chapters_file, list(module.chapter_map.values()))
    module.progress.update(completed=list(range(1,402)), last_completed=401, next_chapter=402, last_audited_chapter=400)
    module.continuity.update(latest_chapter=401)
    atomic_write_json(project.progress_file, module.progress)
    atomic_write_json(project.continuity_file, module.continuity)
    for n in range(1,402):
        (project.chapter_dir / f"{n:04d}.md").write_text("Existing English chapter.", encoding="utf-8")
    text = "A man entered the hall."
    (project.pending_dir / "0402.md").write_text(text, encoding="utf-8")
    def record(kind, wire, raw, translation=""):
        expected = request_identity(operation_kind=kind, novel_id="fixture", chapter=402,
            provider=GOAT, model=policy["wire_model"], profile_id=project.config.profile,
            source_sha256=text_sha(source), translation_sha256=text_sha(translation) if translation else "",
            request_payload={"wire_request":wire, "provider_policy":policy})
        expected.update(provider_policy=policy, wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
        return run_journaled_paid_request(journal_path=paid_request_journal_path(project.data_dir / "provider_request_journal", kind, 402),
            identity=expected, save_json=atomic_write_json, prepare_attempt=lambda:dict(request_started_utc=CLOCK().replace(hour=12).isoformat()),
            send_once=lambda:raw, normalize_response=lambda raw, context:accepted_envelope(dict(raw_response=raw_response_record(raw), **context), wire))
    translation_wire = dict(model=policy["wire_model"], reasoning_effort="low", max_completion_tokens=50000,
        messages=[{"role":"user", "content":source}])
    paid = record("chapter_translation", translation_wire, response(content=text))
    defs = definitions(project)
    meta = dict(chapter=402, stage="translated", translation_stats=paid["response"]["stats"], finish_reason="stop",
        local_qa=defs.local_qa(402, source, text, "stop"), **pending_translation_model_record("v41-flash"),
        **goat_provenance(policy, paid["response"]["request_sha256"]))
    meta.update(translation_max_tokens=50000, translation_thinking="low", translation_pricing_version=policy["pricing_version"])
    meta["state_checkpoint_identity"] = bind_auxiliary_checkpoint(defs.current_state_checkpoint_identity(
        402, source, text, prior_continuity=module.continuity), policy)
    atomic_write_json(project.pending_dir / "0402.json", meta)
    wire = normalize_production_request("state_extraction", dict(model="deepseek-flash", messages=[
        {"role":"system", "content":defs.STATE_PROMPT},
        {"role":"user", "content":defs.state_extraction_user_prompt(chapter=402, existing_terms=[],
            prior=module.continuity, source=source, translation=text)}], **defs.operation_request_controls("state_extraction")))
    raw = response(content="", finish="content_filter", completion=7535)
    raw.usage.prompt_tokens = 9238
    raw.usage.total_tokens = 16773
    raw.usage.prompt_tokens_details.cached_tokens = 0
    record("state_extraction", wire, raw, text)
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
    fake_credentials(monkeypatch, tmp_path, events)
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
        authorize_terminal_state_recovery=authorize)


def test_live_shaped_readonly_plan(incident):
    project = incident[0]
    before = snapshot(project)
    p = plan(project)
    assert p == plan(project) and snapshot(project) == before
    assert p["status"] == "READY" and p["target_chapter"] == 402
    assert p["bindings"]["target"]["progress"]["last_completed"] == 401
    assert p["bindings"]["target"]["continuity"]["latest_chapter"] == 401
    assert p["terminal_recovery_proof"]["original_stats"]["prompt_tokens"] == 9238
    assert p["terminal_recovery_proof"]["original_stats"]["completion_tokens"] == 7535
    assert p["terminal_recovery_proof"]["original_stats"]["cost_usd"] == pytest.approx(0.0059067)
    assert p["api_calls"] == p["credential_reads"] == p["project_writes"] == 0
    assert p["translation_generation_allowed"] is p["ordinary_state_extraction_resend_allowed"] is False
    assert p["terminal_state_recovery_new_send_limit"] == 1 and p["startup_periodic_audits_allowed"] is False
    assert p["bindings"]["target"]["inventory"]["uncertain"] == []
    assert p["recovery_identity"]["operation_kind"] == KIND


def test_explicit_flag_and_stale_approval(incident):
    project = incident[0]
    p = plan(project)
    before = snapshot(project)
    assert run(project, p, False)["status"] == "AUTHORIZATION_REQUIRED"
    assert snapshot(project) == before
    atomic_write_json(project.issues_file, {"issues":["drift"]})
    with pytest.raises(RuntimeError, match="authorization drift"):
        run(project,p)


def test_success_checkpoint_then_existing_pipeline_commit(incident, monkeypatch, tmp_path):
    project = incident[0]
    p = plan(project)
    original_md = (project.pending_dir / "0402.md").read_bytes()
    original_journal = (project.data_dir / "provider_request_journal/0402.state_extraction.json").read_bytes()
    before = json.loads(project.progress_file.read_text())
    sent, events = activation(monkeypatch,tmp_path,incident,[response(content=json.dumps(STATE))])
    report = run(project,p)
    assert report["status"] == "STATE_CHECKPOINT_SAVED", report
    assert len(sent) == report["api_calls"] == 1
    assert sent[0] == p["recovery_wire"]
    assert (project.pending_dir / "0402.md").read_bytes() == original_md
    assert (project.data_dir / "provider_request_journal/0402.state_extraction.json").read_bytes() == original_journal
    progress = json.loads(project.progress_file.read_text())
    assert progress["last_completed"] == 401 and progress["next_chapter"] == 402 and progress["last_audited_chapter"] == 400
    old_id = p["terminal_recovery_proof"]["original_identity"]["operation_id"]
    assert progress["accounted_api_operation_ids"].count(old_id) == 1
    assert progress["estimated_cost_usd"] - before["estimated_cost_usd"] == pytest.approx(0.0059067)
    meta = json.loads((project.pending_dir / "0402.json").read_text())
    assert meta["stage"] == "state_extracted" and meta["state"] == STATE
    assert meta["state_stats"]["prompt_tokens"] == 20
    again = run(project,plan(project))
    assert again["api_calls"] == 0 and len(sent) == 1
    # Existing pending executor owns the next separately authorized review/commit.
    from pending_recovery_plan import build_pending_recovery_plan
    from pending_recovery_execute import execute_pending_recovery
    q = build_pending_recovery_plan("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=6,max_credit_equivalent_usd=2)
    result = execute_pending_recovery("fixture",repo_root=project.repository_root,model_selection="v41-flash",
        provider_selection=GOAT,max_api_calls=6,max_cost_usd=None,max_credit_equivalent_usd=2,
        expected_state_fingerprint=q["state_fingerprint"],approval_token=q["approval_token"],authorize_paid_recovery=True)
    assert result["status"] == "COMMITTED", result
    progress = json.loads(project.progress_file.read_text())
    assert progress["last_completed"] == 402 and progress["next_chapter"] == 403 and progress["last_audited_chapter"] == 400
    assert json.loads(project.continuity_file.read_text())["latest_chapter"] == 402
    assert progress["accounted_api_operation_ids"].count(old_id) == 1
    assert progress["estimated_cost_usd"] - before["estimated_cost_usd"] == pytest.approx(
        0.0059067 + meta["translation_stats"]["cost_usd"] + meta["state_stats"]["cost_usd"])
    assert len(sent) == 1 and not (project.pending_dir / "0403.md").exists()


@pytest.mark.parametrize("finish,content",[("content_filter",""),("length","partial"),("stop"," "),
    ("tool_calls",""),("future_finish",""),(None,"")])
def test_terminal_recovery_failure_settles_and_never_third_send(incident,monkeypatch,tmp_path,finish,content):
    project = incident[0]
    p = plan(project)
    sent,_ = activation(monkeypatch,tmp_path,incident,[response(content=content,finish=finish)])
    report = run(project,p)
    assert report["status"] == "FAILED_AFTER_AUTHORIZATION" and len(sent) == 1
    progress = json.loads(project.progress_file.read_text())
    assert len(progress[RECEIPTS]) == 2
    assert progress["last_completed"] == 401 and progress["next_chapter"] == 402
    meta = json.loads((project.pending_dir / "0402.json").read_text())
    assert "state" not in meta and meta["stage"] == "translated"
    resumed = plan(project)
    assert resumed["status"] == "TERMINAL" and resumed["recovery_generation_sends_remaining"] == 0
    run(project,resumed)
    assert len(sent) == 1 and json.loads(project.progress_file.read_text()) == progress


@pytest.mark.parametrize("repair_finish",["stop","content_filter","length"])
def test_standard_json_repair(incident,monkeypatch,tmp_path,repair_finish):
    project = incident[0]
    malformed = '{"new_terms": [] "continuity_summary":"A man entered.", "issues": []}'
    sent,_ = activation(monkeypatch,tmp_path,incident,[response(content=malformed),
        response(content=json.dumps(STATE) if repair_finish == "stop" else "",finish=repair_finish)])
    report = run(project,plan(project))
    assert len(sent) == 2, report
    assert report["status"] == ("STATE_CHECKPOINT_SAVED" if repair_finish == "stop" else "FAILED_AFTER_AUTHORIZATION")
    if repair_finish != "stop":
        progress = json.loads(project.progress_file.read_text())
        assert len(progress[RECEIPTS]) == 3
        run(project,plan(project))
        assert len(sent) == 2


@pytest.mark.parametrize("mutation",["started","uncertain","planned","wrong_model","usage","request","response","filter_visible","tool_calls","mixed"])
def test_original_bad_or_ambiguous_blocks(incident,mutation):
    project = incident[0]
    path = project.data_dir / "provider_request_journal/0402.state_extraction.json"
    record = json.loads(path.read_text())
    if mutation in {"started","uncertain","planned"}:
        record = _updated_record(record,status=mutation,response={},response_sha256="")
    else:
        envelope = copy.deepcopy(record["response"])
        if mutation == "wrong_model": envelope["raw_response"]["model"] = "wrong"
        if mutation == "usage": envelope["raw_response"]["usage"] = None
        if mutation == "request": record["wire_request_sha256"] = "0"*64
        if mutation == "response": envelope["stats"]["cost_usd"] = 0
        if mutation == "filter_visible": envelope["content"] = "refusal"
        if mutation == "tool_calls": envelope["finish_reason"] = "tool_calls"
        if mutation == "mixed": envelope["stats"]["provider"] = "direct_deepseek"
        record = _updated_record(record,response=envelope,response_sha256=_sha256_json(envelope))
    atomic_write_json(path,record)
    with pytest.raises((RuntimeError,ValueError,KeyError)):
        plan(project)
    assert not incident[2]


def test_crash_after_original_settlement_and_response_replay(incident):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    resumed = plan(project)
    assert resumed["original_operation_accounted"]
    install_terminal_state_recovery(module,resumed)
    client.chat.completions.create = lambda **wire:(sends.append(wire) or response(content=json.dumps(STATE)))
    source = module.chapter_map[402]["text"]
    text = (project.pending_dir / "0402.md").read_text()
    a = module.extract_state(402,source,text,prior_continuity=module.continuity)
    resumed = plan(project)
    install_terminal_state_recovery(module,resumed)
    b = module.extract_state(402,source,text,prior_continuity=module.continuity)
    assert a == b and len(sends) == 1
    save_terminal_state_checkpoint(module,resumed)
    assert plan(project)["saved_state"]


def test_scoped_translation_and_ordinary_state_forbidden(incident):
    project,module,sends,*_ = incident
    install_terminal_state_recovery(module,plan(project))
    from test_production_goat_runtime import kwargs
    for kind in ("chapter_translation","chapter_translation_recovery","state_extraction_recovery","strict_terminology_micro_repair"):
        with pytest.raises((RuntimeError,ValueError)):
            module.journaled_chat_request(kind,402,module.chapter_map[402]["text"],kwargs())
    assert not sends


def test_accounting_conflict_blocks(incident):
    project,module,*_ = incident
    p = plan(project)
    old_id = p["terminal_recovery_proof"]["original_identity"]["operation_id"]
    module.progress.setdefault("accounted_api_operation_ids",[]).append(old_id)
    atomic_write_json(project.progress_file,module.progress)
    with pytest.raises(RuntimeError,match="settlement conflict"):
        plan(project)


@pytest.mark.parametrize("status", ["planned", "started", "uncertain"])
def test_recovery_journal_status_and_exact_slot(incident,status):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    # Produce the reviewed slot once, then model its durable lifecycle boundary.
    client.chat.completions.create = lambda **wire:response(content=json.dumps(STATE))
    module.extract_state(402,module.chapter_map[402]["text"],
        (project.pending_dir/"0402.md").read_text(),prior_continuity=module.continuity)
    path = project.data_dir / f"provider_request_journal/0402.{KIND}.json"
    record = json.loads(path.read_text())
    atomic_write_json(path,_updated_record(record,status=status,response={},response_sha256=""))
    if status == "planned":
        resumed = plan(project)
        assert resumed["recovery_generation_sends_remaining"] == 1
        install_terminal_state_recovery(module,resumed)
        client.chat.completions.create = lambda **wire:(sends.append(wire) or response(content=json.dumps(STATE)))
        save_terminal_state_checkpoint(module,resumed)
        assert len(sends) == 1
    else:
        with pytest.raises(RuntimeError,match="ambiguous"):
            plan(project)
        assert not sends


def test_checkpoint_write_failure_settles_and_replay_excludes_usage(incident):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    client.chat.completions.create = lambda **wire:(sends.append(wire) or response(content=json.dumps(STATE)))
    original = module.atomic_write_json
    def fail_checkpoint(path,value):
        if Path(path).name == "0402.json" and value.get("stage") == "state_extracted":
            raise OSError("synthetic disk failure")
        return original(path,value)
    from pathlib import Path
    module.atomic_write_json = fail_checkpoint
    with pytest.raises(RuntimeError,match="checkpoint persistence"):
        save_terminal_state_checkpoint(module,p)
    assert len(json.loads(project.progress_file.read_text())[RECEIPTS]) == 2
    module.atomic_write_json = original
    resumed = plan(project)
    install_terminal_state_recovery(module,resumed)
    save_terminal_state_checkpoint(module,resumed)
    meta = json.loads((project.pending_dir/"0402.json").read_text())
    assert meta["state_stats"]["cost_usd"] == 0 and meta["state_stats"]["prompt_tokens"] == 0
    assert len(sends) == 1 and plan(project)["saved_state"]


def test_raw_response_crash_replays_then_settles_without_send(incident):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    client.chat.completions.create = lambda **wire:(sends.append(wire) or response(content="",finish="content_filter"))
    with pytest.raises(RuntimeError):
        module.extract_state(402,module.chapter_map[402]["text"],
            (project.pending_dir/"0402.md").read_text(),prior_continuity=module.continuity)
    path = project.data_dir/f"provider_request_journal/0402.{KIND}.json"
    record = json.loads(path.read_text())
    raw = {key:record["response"][key] for key in ("raw_response","request_started_utc")}
    atomic_write_json(path,_updated_record(record,response=raw,response_sha256=_sha256_json(raw)))
    # Model process death before any failure settlement was durable.
    progress = json.loads(project.progress_file.read_text())
    recovery_id = record["operation_id"]
    receipt = progress[RECEIPTS].pop(recovery_id)
    progress["accounted_api_operation_ids"].remove(recovery_id)
    for a,b in (("prompt_tokens","total_prompt_tokens"),("completion_tokens","total_completion_tokens"),
        ("cache_hit_tokens","total_cache_hit_tokens"),("cache_miss_tokens","total_cache_miss_tokens"),
        ("cost_usd","estimated_cost_usd"),("rate_correct_cost_usd","rate_correct_cost_usd")):
        progress[b] -= receipt["stats"][a]
    atomic_write_json(project.progress_file,progress)
    module.progress = progress
    resumed = plan(project)
    install_terminal_state_recovery(module,resumed)
    with pytest.raises(RuntimeError):
        save_terminal_state_checkpoint(module,resumed)
    assert len(sends) == 1 and len(json.loads(project.progress_file.read_text())[RECEIPTS]) == 2
    assert plan(project)["failure_usage_settled"]


@pytest.mark.parametrize("field", ["state","state_stats","state_checkpoint_identity"])
def test_saved_checkpoint_drift_blocks(incident,field):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    client.chat.completions.create = lambda **wire:response(content=json.dumps(STATE))
    save_terminal_state_checkpoint(module,p)
    path = project.pending_dir/"0402.json"
    meta = json.loads(path.read_text())
    if field == "state": meta[field]["continuity_summary"] = "invented"
    elif field == "state_stats": meta[field]["cost_usd"] += 1
    else: meta[field]["source_sha256"] = "0"*64
    atomic_write_json(path,meta)
    with pytest.raises(RuntimeError):
        plan(project)


def test_insufficient_credit_refuses_with_zero_mutation(incident):
    project = incident[0]
    before = snapshot(project)
    with pytest.raises(RuntimeError,match="ceiling"):
        plan(project,max_credit_equivalent_usd=0.000001)
    assert before == snapshot(project) and not incident[2]


def test_nested_owner_and_standalone_base_remain_strict():
    import noveltranslator_core as core
    entries = {
        "有熙氏":dict(english="Youxi Clan",status="locked",strict_qa=True,strict_qa_from_chapter=397,category="clan"),
        "有熙氏族人":dict(english="Youxi Clansmen",status="locked",owns_nested_strict_source_terms=True,category="clan")}
    source = "有熙氏，有熙氏族人。"
    violations = core.translation_commit_qa(source,"Youxi Clansmen",entries,("clan",),chapter_number=402)["locked_high_priority_term_violations"]
    assert len(violations) == 1
    assert violations[0]["source_anchor_count"] == 1
    assert core.translation_commit_qa(source,"Youxi Clan and Youxi Clansmen",entries,("clan",),chapter_number=402)["commit_allowed"]


@pytest.mark.parametrize("mutation", ["negative_usage", "boolean_usage", "missing_completion", "unrelated_started", "later_review"])
def test_additional_evidence_blocks(incident,mutation):
    project = incident[0]
    path = project.data_dir/"provider_request_journal/0402.state_extraction.json"
    record = json.loads(path.read_text())
    if mutation in {"negative_usage","boolean_usage","missing_completion"}:
        envelope = record["response"]
        envelope["raw_response"]["usage"]["completion_tokens"] = {
            "negative_usage":-1,"boolean_usage":True,"missing_completion":None}[mutation]
        atomic_write_json(path,_updated_record(record,response=envelope,response_sha256=_sha256_json(envelope)))
    else:
        kind = "chapter_glossary_review" if mutation == "later_review" else "state_extraction"
        target = project.data_dir/f"provider_request_journal/{400 if mutation == 'unrelated_started' else 402:04d}.{kind}.json"
        record = _updated_record(record,status="started",response={},response_sha256="")
        atomic_write_json(target,record)
    with pytest.raises((RuntimeError,ValueError)):
        plan(project)
    assert not incident[2]


def test_settlement_progress_failure_precedes_send_and_restarts(incident):
    project,module,sends,*_ = incident
    p = plan(project)
    original = module.atomic_write_json
    def fail_progress(path,value):
        if str(path).replace("\\","/").endswith("data/progress.json"):
            raise OSError("synthetic progress write failure")
        return original(path,value)
    module.atomic_write_json = fail_progress
    with pytest.raises(OSError):
        install_terminal_state_recovery(module,p)
    assert not sends and not plan(project)["original_operation_accounted"]
    module.atomic_write_json = original
    install_terminal_state_recovery(module,plan(project))
    assert plan(project)["original_operation_accounted"]


def test_settlement_receipt_total_conflict_blocks(incident):
    project,module,*_ = incident
    install_terminal_state_recovery(module,plan(project))
    module.progress["total_prompt_tokens"] = 0
    atomic_write_json(project.progress_file,module.progress)
    with pytest.raises(RuntimeError,match="totals contradict"):
        plan(project)


@pytest.mark.parametrize("status", ["missing", "planned"])
def test_settled_recovery_cannot_resend_after_journal_loss(incident,status):
    project,module,sends,client,*_ = incident
    p = plan(project)
    install_terminal_state_recovery(module,p)
    client.chat.completions.create = lambda **wire:(sends.append(wire) or response(content="",finish="content_filter"))
    with pytest.raises(RuntimeError):
        save_terminal_state_checkpoint(module,p)
    path = project.data_dir/f"provider_request_journal/0402.{KIND}.json"
    if status == "missing":
        path.unlink()  # Disposable fixture only; simulate local evidence loss.
    else:
        record = json.loads(path.read_text())
        atomic_write_json(path,_updated_record(record,status="planned",response={},response_sha256=""))
    with pytest.raises(RuntimeError,match="already consumed"):
        plan(project)
    assert len(sends) == 1


def test_planner_never_hashes_env_in_artifact_directory(incident,monkeypatch):
    from pathlib import Path
    project = incident[0]
    (project.pending_dir/".env").write_text("synthetic-secret-do-not-read")
    original = Path.read_bytes
    def guarded(path):
        if path.name == ".env":
            pytest.fail("planner must not read environment files")
        return original(path)
    monkeypatch.setattr(Path,"read_bytes",guarded)
    assert plan(project)["status"] == "READY"
