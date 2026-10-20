"""Committed Ch362 history followed by live-shaped future incidents; no network."""
import json
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from test_production_goat_runtime import engine, zero_network, response, CLOCK, GOAT
from test_uncertain_request_disposition import (
    incident, recovered_fixture, old_path, STATE, evidence, snapshot, activation,
)
from novel_project import load_novel_project
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    inspect_paid_request_journals, request_identity, paid_request_journal_path,
    run_journaled_paid_request, _new_planned_record, _updated_record, _with_record_hash,
)
from production_uncertain_request_disposition import (
    effective_paid_request_inventory, registry_path, text_sha,
)
from production_goat_runtime import install_goat_runtime, normalize_production_request, raw_response_record, GoatBudget
from production_provider_runtime import provider_identity, goat_provenance, historical_capped_goat_provider_identity
from production_goat_cap_recovery import accepted_envelope
from production_goat_policy import bind_auxiliary_checkpoint
from translation_model_config import pending_translation_model_record
from benchmark_longitudinal_provider import build_provider_request_identity
from uncertain_request_disposition_plan import build_uncertain_request_disposition_plan
from production_path_preflight import run_production_path_preflight
from goat_state_policy_migration_plan import definitions


@pytest.fixture
def history(incident):
    project, _, _ = recovered_fixture(incident, commit=True)
    project = load_novel_project("fixture", repo_root=project.repository_root)
    original = old_path(project).read_bytes()
    assert json.loads(original)["status"] == "uncertain"
    raw = inspect_paid_request_journals(project.data_dir / "provider_request_journal")
    assert [r["chapter"] for r in raw["uncertain"]] == [362]
    assert run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)["status"] == "PASS"
    before = snapshot(project)
    effective = effective_paid_request_inventory(project, inventory=raw)
    assert not effective["uncertain"] and effective["records"] == raw["records"]
    assert raw["uncertain"] and before == snapshot(project)
    yield (project, *incident[1:]), original
    assert old_path(project).read_bytes() == original
    assert json.loads(project.progress_file.read_text(encoding='utf-8'))["accounted_api_operation_ids"].count(
        json.loads(original)["operation_id"]) == 0


def future(history, *, target=500, completed=False):
    """Advance disposable intervening chapters, then hydrate a fresh runtime."""
    prior, _ = history
    project, previous = prior[:2]
    config = json.loads(project.config_file.read_text(encoding='utf-8')); config["final_chapter"] = max(501, target + 1)
    atomic_write_json(project.config_file, config)
    rows = json.loads(project.chapters_file.read_text(encoding='utf-8'))
    rows.extend(dict(chapter=n, text=rows[0]["text"]) for n in range(len(rows)+1, config["final_chapter"] + 1))
    atomic_write_json(project.chapters_file, rows)
    end = target if completed else target-1
    for n in range(363, end+1):
        (project.chapter_dir / f"{n:04d}.md").write_text("Existing English chapter.", encoding="utf-8")
    progress = json.loads(project.progress_file.read_text(encoding='utf-8'))
    progress.update(completed=list(range(1,end+1)), last_completed=end,
        next_chapter=end+1, last_audited_chapter=475)
    atomic_write_json(project.progress_file, progress)
    continuity = json.loads(project.continuity_file.read_text(encoding='utf-8')); continuity["latest_chapter"] = end
    atomic_write_json(project.continuity_file, continuity)
    project = load_novel_project("fixture", repo_root=project.repository_root)
    import run_production_engine as launcher
    module = launcher.load_production_module()
    module.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
    if completed:
        # Use the acquisition entry point under its real runtime audit context;
        # leave final publication to the separately authorized local planner.
        module.run_periodic_audit = module.call_periodic_audit
    sends = []
    client = NS(chat=NS(completions=NS(create=lambda **k:pytest.fail("unapproved synthetic send"))))
    budget = GoatBudget(8, 2)
    install_goat_runtime(module, provider_client=client, budget=budget, clock=CLOCK)
    return project, module, sends, client, budget


def record(project, chapter, kind, wire, raw, translation="", *, policy=None):
    policy = policy or provider_identity(GOAT, "v41-flash")
    source = json.loads(project.chapters_file.read_text(encoding='utf-8'))[chapter-1]["text"]
    identity = request_identity(operation_kind=kind, novel_id="fixture", chapter=chapter,
        provider=GOAT, model=policy["wire_model"], profile_id=project.config.profile,
        source_sha256=text_sha(source), translation_sha256=text_sha(translation) if translation else "",
        request_payload={"wire_request":wire, "provider_policy":policy})
    identity.update(provider_policy=policy,
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
    path = paid_request_journal_path(project.data_dir / "provider_request_journal",kind,chapter)
    if raw in ("uncertain", "started"):
        atomic_write_json(path, _updated_record(_new_planned_record(identity), status=raw,
            attempt_context=dict(request_started_utc="2026-10-14T19:33:22.108216Z"),
            error=dict(type="SyntheticTransportError", message="outcome unknown")))
        return identity
    return run_journaled_paid_request(journal_path=path, identity=identity, save_json=atomic_write_json,
        prepare_attempt=lambda:dict(request_started_utc="2026-10-14T19:33:21Z"),
        send_once=lambda:raw, normalize_response=lambda raw, context:accepted_envelope(
            dict(raw_response=raw_response_record(raw), **context), wire, provider_policy=policy))


def pending(context, status="uncertain", *, policy=None):
    project,module,*_ = context
    policy = policy or provider_identity(GOAT,"v41-flash")
    chapter = module.progress["next_chapter"]; source = module.chapter_map[chapter]["text"]
    text = "A man entered the hall."
    (project.pending_dir/f"{chapter:04d}.md").write_text(text,encoding="utf-8")
    raw = response(content=text,completion=20120)
    raw.usage.prompt_tokens=8675; raw.usage.total_tokens=28795; raw.usage.prompt_tokens_details.cached_tokens=4096
    paid = record(project,chapter,"chapter_translation",dict(model=policy["wire_model"],
        reasoning_effort="low",max_completion_tokens=50000,messages=[{"role":"user","content":source}]),raw,policy=policy)
    defs = definitions(project)
    meta = dict(chapter=chapter,stage="translated",translation_stats=paid["response"]["stats"],finish_reason="stop",
        local_qa=defs.local_qa(chapter,source,text,"stop"),**pending_translation_model_record("v41-flash"),
        **goat_provenance(policy,paid["response"]["request_sha256"]))
    meta.update(translation_max_tokens=50000,translation_thinking="low",translation_pricing_version=policy["pricing_version"])
    meta["state_checkpoint_identity"] = bind_auxiliary_checkpoint(defs.current_state_checkpoint_identity(
        chapter,source,text,prior_continuity=module.continuity),policy)
    atomic_write_json(project.pending_dir/f"{chapter:04d}.json",meta)
    wire = normalize_production_request("state_extraction",dict(model="deepseek-flash",messages=[
        {"role":"system","content":defs.STATE_PROMPT},{"role":"user","content":defs.state_extraction_user_prompt(
            chapter=chapter,existing_terms=[],prior=module.continuity,source=source,translation=text)}],
        **defs.operation_request_controls("state_extraction")))
    if status == "migration":
        wire["max_completion_tokens"] = 8000
        for kind in ("state_extraction","state_extraction_recovery"):
            record(project,chapter,kind,wire,response(content="",finish="length",completion=8000,reasoning=8000),text,policy=policy)
    else:
        record(project,chapter,"state_extraction",wire,status if status in {"uncertain","started"}
            else response(content="",finish="content_filter"),text,policy=policy)
    return wire


def future_evidence(project):
    chapter = json.loads(project.progress_file.read_text(encoding='utf-8'))["next_chapter"]
    e = evidence(project)
    from pending_recovery_plan import _sha256_file
    path = project.data_dir/f"provider_request_journal/{chapter:04d}.state_extraction.json"
    journal = json.loads(path.read_text(encoding='utf-8'))
    paid = json.loads((project.data_dir/f"provider_request_journal/{chapter:04d}.chapter_translation.json").read_text(encoding='utf-8'))
    e.update({k:journal[k] for k in ("operation_id","request_sha256","wire_request_sha256","provider","model")})
    e["journal_sha256"] = _sha256_file(path)
    e["reviewed_utc"] = "2026-10-14T20:00:00Z"
    e["usage_window"] = {k:v.replace("2026-10-13","2026-10-14") if isinstance(v,str) else v
        for k,v in e["usage_window"].items()}
    e["preceding_api"].update(timestamp_local="2026-10-14T14:33:21-05:00",
        translation_request_sha256=paid["wire_request_sha256"],displayed_cost=f'{paid["response"]["stats"]["cost_usd"]:.4f}')
    return e


def disposition_plan(project):
    return build_uncertain_request_disposition_plan("fixture",repo_root=project.repository_root,
        operator_evidence=future_evidence(project),max_api_calls=6,max_credit_equivalent_usd=2)


def ambiguity(project, status="uncertain"):
    wire = dict(model=provider_identity(GOAT,"v41-flash")["wire_model"],reasoning_effort="low",
        messages=[{"role":"user","content":"New unresolved unrelated request"}])
    record(project,499,"state_extraction",wire,status,"Existing English chapter.")


def test_second_incident_ready_recovery_restart_pending_stays_active(history,monkeypatch,tmp_path):
    context = future(history); project,module,sends,client,_ = context
    wire = pending(context)
    before = snapshot(project)
    p = disposition_plan(project)
    assert p["status"] == "READY" and p["target_chapter"] == 500
    assert p["recovery_wire"] == wire and before == snapshot(project)
    assert [r["chapter"] for r in inspect_paid_request_journals(project.data_dir/"provider_request_journal")["uncertain"]] == [362,500]
    from uncertain_request_disposition_execute import execute_uncertain_request_disposition
    def execute(p, authorized=True):
        return execute_uncertain_request_disposition("fixture",repo_root=project.repository_root,
            max_api_calls=6,max_credit_equivalent_usd=2,expected_state_fingerprint=p["state_fingerprint"],
            approval_token=p["approval_token"],operator_evidence=future_evidence(project),
            authorize_uncertain_no_provider_record_disposition=authorized)
    assert execute(p,False)["status"] == "AUTHORIZATION_REQUIRED" and snapshot(project) == before
    sent,_ = activation(monkeypatch,tmp_path,context,[response(content=json.dumps(STATE))])
    report = execute(p)
    assert report["status"] == "STATE_CHECKPOINT_SAVED" and report["api_calls"] == 1
    assert len(sent) == 1 and sent[0] == wire
    before = snapshot(project)
    q = disposition_plan(project)
    assert q["status"] == "READY" and q["saved_state"] and q["recovery_generation_sends_remaining"] == 0
    assert [r["chapter"] for r in effective_paid_request_inventory(project)["uncertain"]] == [500]
    assert execute(q)["api_calls"] == 0
    assert len(sent) == 1 and not sends and snapshot(project) == before
    assert json.loads(project.progress_file.read_text(encoding='utf-8'))["last_completed"] == 499


@pytest.mark.parametrize("damage",["unresolved","missing_disposition","missing_receipt","forged_state","forged_stats","corrupt_journal"])
def test_invalid_historical_disposition_blocks_second_incident(history,damage):
    context = future(history); project = context[0]; pending(context)
    path = registry_path(project,362)
    if damage in {"unresolved","missing_disposition"}: path.unlink()
    elif damage == "missing_receipt": path.with_suffix(".checkpoint.json").unlink()
    elif damage == "corrupt_journal":
        # Corrupt a separate current journal, retaining byte-exact Ch362 history.
        (project.data_dir/"provider_request_journal/0499.broken.json").write_text("{broken")
    else:
        path = path.with_suffix(".checkpoint.json"); value = json.loads(path.read_text(encoding='utf-8'))
        if damage == "forged_state": value["checkpoint"]["state"]["continuity_summary"] = "Other valid state."
        else: value["checkpoint"]["state_stats"]["completion_tokens"] += 1
        atomic_write_json(path,_with_record_hash(value))
    before = snapshot(project)
    with pytest.raises((RuntimeError,ValueError,KeyError)): disposition_plan(project)
    assert before == snapshot(project) and not context[2]


@pytest.mark.parametrize("status",["uncertain","started"])
def test_two_unresolved_ambiguities_block(history,status):
    context = future(history); project = context[0]; pending(context); ambiguity(project,status)
    before = snapshot(project)
    assert len(effective_paid_request_inventory(project)["uncertain"]) == 2
    with pytest.raises(RuntimeError,match="inventory"): disposition_plan(project)
    assert before == snapshot(project) and not context[2]


@pytest.mark.parametrize("blocked",[False,True])
def test_future_saturation_planner_and_normal_bounded_runtime(history,blocked):
    from test_goat_saturation_recovery import saturation
    from production_translation_request import build_translation_request
    from goat_saturation_recovery_plan import build_goat_saturation_recovery_plan
    context = future(history); project,module,sends,client,_ = context
    chapter = module.chapter_map[500]
    body = build_translation_request(chapter=chapter,config=project.config,profile=module.ACTIVE_PROFILE,
        system_prompt=module.system_prompt,glossary_entries=module.glossary["entries"],continuity=module.continuity,
        model=module.MODEL,controls=module.operation_request_controls("chapter_translation"))
    wire = normalize_production_request("translation",body)
    raw = saturation()
    raw.usage.completion_tokens = raw.usage.completion_tokens_details.reasoning_tokens = 50000
    raw.usage.total_tokens = raw.usage.prompt_tokens + 50000
    record(project,500,"chapter_translation",wire,raw)
    if blocked: ambiguity(project)
    before = snapshot(project)
    planner = lambda:build_goat_saturation_recovery_plan("fixture",repo_root=project.repository_root,
        max_api_calls=6,max_credit_equivalent_usd=2)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content="A man entered the hall."))
    if blocked:
        with pytest.raises(RuntimeError): planner()
        with pytest.raises(RuntimeError): module.translate_chapter(chapter)
        assert not sends
    else:
        assert planner()["status"] == "READY"
        stage = module.translate_chapter(chapter)
        assert stage.initial["replayed"] and stage.recovery_used and len(sends) == 1
        module.translate_chapter(chapter); assert len(sends) == 1
    # The read-only planning call and replay never rewrite historical journals.
    assert old_path(project).read_bytes() == history[1]


@pytest.mark.parametrize("blocked",[False,True])
def test_future_glossary_planner_and_normal_bounded_runtime(history,blocked):
    from test_goat_glossary_review_recovery import STATE as TERM_STATE, empty_stop, VALID
    from goat_glossary_review_recovery_plan import build_goat_glossary_review_recovery_plan
    context = future(history); project,module,sends,client,budget = context
    replies = [response(content="A man entered the hall."),response(content=TERM_STATE),empty_stop()]
    client.chat.completions.create = lambda **body:(sends.append(body) or replies.pop(0))
    budget.max_api_calls = 3
    assert module.process_chapter(500) is False and len(sends) == 3
    budget.max_api_calls = 8
    if blocked: ambiguity(project)
    before = snapshot(project)
    planner = lambda:build_goat_glossary_review_recovery_plan("fixture",repo_root=project.repository_root,
        max_api_calls=6,max_credit_equivalent_usd=2)
    if blocked:
        with pytest.raises(RuntimeError): planner()
    else: assert planner()["status"] == "READY"
    assert before == snapshot(project)
    client.chat.completions.create = lambda **body:(sends.append(body) or response(content=VALID))
    if blocked:
        assert module.process_chapter(500) is False and len(sends) == 3
    else:
        assert module.process_chapter(500) is True and len(sends) == 4
        assert json.loads(project.progress_file.read_text(encoding='utf-8'))["last_completed"] == 500


@pytest.mark.parametrize("domain",["terminal","migration"])
@pytest.mark.parametrize("blocked",[False,True])
def test_future_state_recovery_planners(history,domain,blocked):
    context = future(history); project = context[0]
    if domain == "terminal":
        from goat_state_terminal_recovery_plan import build_goat_state_terminal_recovery_plan as planner
        pending(context,"terminal")
    else:
        from goat_state_policy_migration_plan import build_goat_state_policy_migration_plan as planner
        pending(context,"migration",policy=historical_capped_goat_provider_identity())
    if blocked: ambiguity(project)
    before = snapshot(project)
    call = lambda:planner("fixture",repo_root=project.repository_root,max_api_calls=6,max_credit_equivalent_usd=2)
    if blocked:
        with pytest.raises(RuntimeError): call()
    else: assert call()["status"] == "READY"
    assert snapshot(project) == before and not context[2]


@pytest.mark.parametrize("blocked",[False,True])
def test_future_periodic_audit_finalization(history,blocked):
    from goat_periodic_audit_finalize_plan import build_goat_periodic_audit_finalize_plan
    context = future(history,completed=True); project,module,sends,client,_ = context
    glossary = dict(entries={"甲":dict(english="Jia",category="character",status="provisional",first_chapter=500)})
    atomic_write_json(project.glossary_file,glossary); module.glossary = glossary
    client.chat.completions.create = lambda **body:(sends.append(body) or response(
        content=json.dumps(dict(decisions=[dict(candidate_id="c01",action="keep")],issues=[]))))
    module.run_periodic_audit(476,500)
    assert len(sends) == 1
    if blocked: ambiguity(project)
    before = snapshot(project)
    call = lambda:build_goat_periodic_audit_finalize_plan("fixture",repo_root=project.repository_root,
        start_chapter=476,end_chapter=500)
    client.chat.completions.create = lambda **k:pytest.fail("finalization is local only")
    if blocked:
        with pytest.raises(RuntimeError): call()
    else: assert call()["status"] == "READY"
    assert snapshot(project) == before and len(sends) == 1


def test_accounting_migration_intentionally_requires_no_paid_journals(history):
    from goat_accounting_migration_plan import build_goat_accounting_migration_plan
    project = history[0][0]
    assert not effective_paid_request_inventory(project)["uncertain"]
    before = snapshot(project)
    assert build_goat_accounting_migration_plan("fixture",repo_root=project.repository_root)["status"] == "BLOCKED_AMBIGUOUS_PAID_STATE"
    assert snapshot(project) == before


@contextmanager
def damaged_original(project, original, damage):
    """Deliberate synthetic evidence loss; restore only after read-only assertions."""
    from production_paid_request_journal import _sha256_json
    path = old_path(project)
    try:
        value = json.loads(original)
        if damage == "delete":
            path.unlink()
        elif damage == "recorded":
            receipt = json.loads(registry_path(project,362).with_suffix(".checkpoint.json").read_text(encoding="utf-8"))
            envelope = accepted_envelope(dict(raw_response=raw_response_record(response(content=json.dumps(STATE))),
                request_started_utc="2026-10-14T19:33:21Z"),receipt["recovery_wire"])
            atomic_write_json(path,_updated_record(value,status="response_recorded",response=envelope,
                response_sha256=_sha256_json(envelope)))
        elif damage == "planned":
            atomic_write_json(path,_updated_record(value,status="planned"))
        elif damage == "started":
            atomic_write_json(path,_updated_record(value,status="started"))
        else:
            value["error"]["message"] = "Different syntactically valid historical error"
            atomic_write_json(path,_with_record_hash(value))
        yield
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("damage",["delete","planned","recorded","started","rewritten"])
def test_history_integrity_verified_without_raw_original_ambiguity(history,damage):
    from production_uncertain_request_disposition import verified_completed_dispositions
    project = history[0][0]
    with damaged_original(project,history[1],damage):
        raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
        assert not raw["errors"]
        if damage in {"delete","planned","recorded"}:
            assert raw["uncertain"] == []
        else:
            assert len(raw["uncertain"]) == 1
        before = snapshot(project)
        for check in (lambda:effective_paid_request_inventory(project,inventory=raw),
                      lambda:verified_completed_dispositions(project,raw)):
            with pytest.raises(RuntimeError): check()
        result = run_production_path_preflight("fixture",repo_root=project.repository_root,
            model_selection="v41-flash",provider_selection=GOAT)
        assert result["status"] != "PASS" and result["errors"]
        assert snapshot(project) == before


@pytest.mark.parametrize("damage",["delete","planned","recorded"])
def test_second_incident_blocked_by_original_historical_evidence_loss(history,damage):
    context = future(history); project = context[0]; pending(context)
    operator_evidence = future_evidence(project)
    with damaged_original(project,history[1],damage):
        raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
        assert not raw["errors"] and [r["chapter"] for r in raw["uncertain"]] == [500]
        before = snapshot(project)
        with pytest.raises(RuntimeError):
            build_uncertain_request_disposition_plan("fixture",repo_root=project.repository_root,
                operator_evidence=operator_evidence,max_api_calls=6,max_credit_equivalent_usd=2)
        assert not context[2] and snapshot(project) == before


@pytest.mark.parametrize("damage",["delete","planned","recorded"])
def test_runtime_recovery_verifies_registry_when_raw_uncertainty_empty(history,damage):
    from production_translation_request import build_translation_request
    from production_goat_cap_recovery import KIND as SATURATION_KIND, recovery_binding
    from test_goat_saturation_recovery import saturation
    context = future(history); project,module,sends,client,_ = context
    chapter = module.chapter_map[500]
    body = build_translation_request(chapter=chapter,config=project.config,profile=module.ACTIVE_PROFILE,
        system_prompt=module.system_prompt,glossary_entries=module.glossary["entries"],continuity=module.continuity,
        model=module.MODEL,controls=module.operation_request_controls("chapter_translation"))
    wire = normalize_production_request("translation",body)
    raw = saturation(); raw.usage.completion_tokens = raw.usage.completion_tokens_details.reasoning_tokens = 50000
    raw.usage.total_tokens = raw.usage.prompt_tokens + 50000
    record(project,500,"chapter_translation",wire,raw)
    initial = json.loads((project.data_dir/"provider_request_journal/0500.chapter_translation.json").read_text(encoding="utf-8"))
    client.chat.completions.create = lambda **k:pytest.fail("damaged history must block before send")
    with damaged_original(project,history[1],damage):
        inventory = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
        assert not inventory["errors"] and not inventory["uncertain"]
        before = snapshot(project)
        # Initial-response saturation gate and the distinct recovery-identity
        # gate both execute registry verification despite empty raw ambiguity.
        with pytest.raises(RuntimeError): module.translate_chapter(chapter)
        with pytest.raises(RuntimeError):
            module.journaled_chat_request(SATURATION_KIND,500,chapter["text"],body,
                translation_text=recovery_binding(initial,wire))
        assert not sends and snapshot(project) == before


@pytest.mark.parametrize("orphan",["checkpoint","claim","root_only","unknown","bad_name"])
def test_orphaned_or_inconsistent_registry_evidence_blocks_empty_raw_history(history,orphan):
    from production_uncertain_request_disposition import KIND as RECOVERY_KIND, claim_path
    project = history[0][0]
    path = registry_path(project,362)
    if orphan == "root_only":
        path.with_suffix(".checkpoint.json").unlink()
    elif orphan in {"checkpoint","claim"}:
        # Retain only the named sidecar and remove its root in the disposable fixture.
        keep = path.with_suffix(".checkpoint.json") if orphan == "checkpoint" else claim_path(path,RECOVERY_KIND)
        for child in path.parent.iterdir():
            if child != keep: child.unlink()
    elif orphan == "unknown":
        (path.parent/"0362.other.claim.json").write_text("{}",encoding="utf-8")
    else:
        (path.parent/"00362.json").write_bytes(path.read_bytes())
    with damaged_original(project,history[1],"delete"):
        raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
        assert not raw["errors"] and not raw["uncertain"]
        before = snapshot(project)
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project,inventory=raw)
        result = run_production_path_preflight("fixture",repo_root=project.repository_root,
            model_selection="v41-flash",provider_selection=GOAT)
        assert result["status"] != "PASS" and snapshot(project) == before


def test_registry_children_checked_before_open_when_raw_uncertainty_empty(history,monkeypatch,tmp_path):
    from pathlib import Path
    from test_uncertain_request_disposition import directory_link
    project = history[0][0]
    target = tmp_path/"outside_registry"; target.mkdir()
    directory_link(registry_path(project,362).parent/"linked_child",target)
    original_open = Path.open
    def guarded(path,*args,**kwargs):
        if path.parent == target: pytest.fail("registry discovery followed linked child")
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",guarded)
    with damaged_original(project,history[1],"delete"):
        raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
        assert not raw["uncertain"]
        with pytest.raises(RuntimeError,match="symlink/reparse"):
            effective_paid_request_inventory(project,inventory=raw)
        assert list(target.iterdir()) == []


def test_no_history_empty_inventory_fast_path_remains_readonly(engine,monkeypatch):
    from pathlib import Path
    from production_uncertain_request_disposition import verified_completed_dispositions
    project = engine[0]
    raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
    assert raw == {"errors":[],"records":[],"uncertain":[]}
    before = snapshot(project)
    original_open = Path.open
    def guarded(path,*args,**kwargs):
        if path == project.progress_file: pytest.fail("empty history needs no progress read")
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",guarded)
    assert verified_completed_dispositions(project,raw) is raw
    assert effective_paid_request_inventory(project,inventory=raw) is raw
    monkeypatch.setattr(Path,"open",original_open)
    assert before == snapshot(project)


def test_only_bound_raw_journal_can_inherit_completed_resolution(history):
    project = history[0][0]
    extra = project.data_dir/"provider_request_journal/0361.state_extraction.json"
    extra.write_bytes(history[1])
    raw = inspect_paid_request_journals(project.data_dir/"provider_request_journal")
    assert not raw["errors"] and len(raw["uncertain"]) == 2
    assert len({item["operation_id"] for item in raw["uncertain"]}) == 1
    before = snapshot(project)
    active = effective_paid_request_inventory(project,inventory=raw)
    assert [item["file"] for item in active["uncertain"]] == [extra.name]
    assert active["records"] == raw["records"] and len(raw["uncertain"]) == 2
    assert run_production_path_preflight("fixture",repo_root=project.repository_root,
        model_selection="v41-flash",provider_selection=GOAT)["status"] != "PASS"
    assert snapshot(project) == before


def correcting_audit(history, *, target=500, old="A man", new="Jia"):
    """Actual acquisition/checkpoint entry point with synthetic paid responses."""
    context = future(history, target=target, completed=True)
    project, module, sends, client, _ = context
    start = target - 24
    module.progress["last_audited_chapter"] = start - 1
    atomic_write_json(project.progress_file, module.progress)
    glossary = dict(entries={"甲": dict(english=old, category="character", status="provisional", first_chapter=target)})
    atomic_write_json(project.glossary_file, glossary)
    module.glossary = glossary
    client.chat.completions.create = lambda **body: (sends.append(body) or response(content=json.dumps(
        dict(decisions=[dict(candidate_id="c01", action="correct", english=new)], issues=[]))))
    result, stats, chunking = module.run_periodic_audit(start, target)
    assert len(sends) == 1
    client.chat.completions.create = lambda **body: pytest.fail("lineage proof/finalization must never send")
    return context, result, stats, chunking


def finalize_correction(context, result, stats, chunking):
    import production_periodic_audit_finalization as final
    from production_run_lock import production_run_lock
    project, module = context[:2]
    end = module.progress["last_completed"]
    with production_run_lock(project.root):
        return final.finalize_periodic_audit(module, end - 24, end, result, stats, chunking)


def strict_history(project):
    assert not effective_paid_request_inventory(project)["uncertain"]
    report = run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert report["status"] == "PASS", report


def test_completed_disposition_accepts_proven_retroactive_lineage_after_restart(history):
    context, result, stats, chunking = correcting_audit(history)
    project = context[0]
    original = {p.name: p.read_bytes() for p in registry_path(project, 362).parent.iterdir()}
    qa_before = {p.name: p.read_bytes() for p in project.qa_dir.glob("*") if p.is_file()}
    finalize_correction(context, result, stats, chunking)
    assert (project.chapter_dir / "0362.md").read_text(encoding="utf-8") == "Jia entered the hall."
    strict_history(load_novel_project("fixture", repo_root=project.repository_root))
    assert original == {p.name: p.read_bytes() for p in registry_path(project, 362).parent.iterdir()}
    assert qa_before == {p.name: p.read_bytes() for p in project.qa_dir.glob("*") if p.is_file()}
    assert old_path(project).read_bytes() == history[1] and len(context[2]) == 1


@pytest.mark.parametrize("incident", ["crlf_unicode", "bom"], indirect=True)
def test_two_audits_verify_chronological_lineage_and_decoded_byte_bridge(history):
    from production_historical_lineage import decoded_text_sha
    context, result, stats, chunking = correcting_audit(history)
    project = context[0]
    before = (project.chapter_dir / "0362.md").read_bytes()
    proof = json.loads(registry_path(project,362).read_bytes())["proof"]
    assert decoded_text_sha(before) == proof["translation_sha256"]
    assert b"\r\n" in before and "Café".encode() in before
    finalize_correction(context, result, stats, chunking)
    strict_history(project)
    context, result, stats, chunking = correcting_audit(history, target=525, old="Jia", new="Master Yi")
    finalize_correction(context, result, stats, chunking)
    strict_history(load_novel_project("fixture", repo_root=project.repository_root))
    assert b"Master Yi" in (project.chapter_dir / "0362.md").read_bytes()
    assert b"\r\n" in (project.chapter_dir / "0362.md").read_bytes()
    assert len(context[2]) == 1


@pytest.mark.parametrize("damage", ["direct", "backup", "missing_backup", "plan", "state", "result", "reconcile",
    "novel", "source", "chapter", "span", "disconnected", "fork", "order", "unfinished", "outer", "accounting", "unrelated"])
def test_historical_lineage_adversarial_evidence_fails_closed(history, damage):
    from production_accounting import canonical_sha256
    context, result, stats, chunking = correcting_audit(history)
    project = context[0]
    finalize_correction(context, result, stats, chunking)
    directory = project.retroactive_dir / "transactions"
    def alter(path, change):
        value = json.loads(path.read_bytes()); change(value); atomic_write_json(path, value)
    if damage == "direct": (project.chapter_dir / "0362.md").write_bytes(b"Unproven edit.")
    elif damage in {"backup", "missing_backup"}:
        p = next(project.backup_dir.rglob("0362.md"))
        if damage == "backup": p.write_bytes(b"Wrong BEFORE")
        else: p.unlink()
    elif damage in {"plan", "state", "result", "reconcile"}:
        p = next(directory.glob("*.reconcile.*.json" if damage == "reconcile" else f"*.{damage}.json"))
        alter(p, lambda v: v.update(tampered=True))
    elif damage in {"novel", "source", "chapter", "span", "disconnected"}:
        p = next(directory.glob("*.plan.json"))
        def edit(v):
            item = next(i for i in v["chapters"] if i["edits"])
            if damage == "novel": v["request"]["novel_id"] = "other"
            elif damage == "source": item["source_sha256"] = "0"*64
            elif damage == "chapter": item["chapter"] = 361
            elif damage == "span": item["edits"][0]["start"] += 1
            else: item["before_sha256"] = "f"*64
            v["transaction_id"] = canonical_sha256({k:x for k,x in v.items() if k != "transaction_id"})
        alter(p, edit)
    elif damage == "fork":
        p = next(directory.glob("*.plan.json")); (directory / "competing.plan.json").write_bytes(p.read_bytes())
    elif damage == "order":
        p = next(project.audit_dir.glob("*.finalization.json"))
        def reorder(v):
            v["start"] = 450; v["intent_sha256"] = canonical_sha256({k:x for k,x in v.items() if k != "intent_sha256"})
        alter(p, reorder)
    elif damage == "unfinished": alter(next(directory.glob("*.state.json")), lambda v: v.update(state="APPLIED"))
    elif damage == "outer": (project.audit_dir / "audit_0476_0500.json").unlink()
    elif damage == "accounting":
        alter(project.progress_file, lambda v: v.update(accounted_api_operation_ids=[]))
    elif damage == "unrelated": ambiguity(project)
    before = snapshot(project)
    if damage == "unrelated":
        assert effective_paid_request_inventory(project)["uncertain"]
    else:
        with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
            effective_paid_request_inventory(project)
    assert run_production_path_preflight("fixture",repo_root=project.repository_root,
        model_selection="v41-flash",provider_selection=GOAT)["status"] != "PASS"
    assert snapshot(project) == before and len(context[2]) == 1


@pytest.mark.parametrize("boundary", ["intent", "plan", "backup", "chapter", "APPLIED", "result", "FINALIZED", "audit", "progress"])
def test_disposition_lineage_interrupted_audit_local_tail_only(history, monkeypatch, boundary):
    import production_periodic_audit_finalization as final
    from goat_periodic_audit_finalize_plan import build_goat_periodic_audit_finalize_plan
    from goat_periodic_audit_finalize_execute import execute_goat_periodic_audit_finalize
    from pathlib import Path
    context, result, stats, chunking = correcting_audit(history)
    project, module = context[:2]
    original_json, original_bytes = final.atomic_write_json, final.atomic_write_bytes
    fired = []
    def crash_json(path, value):
        original_json(path, value)
        p = Path(path)
        match = ((boundary == "intent" and p.name.endswith(".finalization.json")) or
            (boundary == "plan" and p.name.endswith(".plan.json")) or
            (boundary in {"APPLIED", "FINALIZED"} and p.name.endswith(".state.json") and value.get("state") == boundary) or
            (boundary == "result" and p.name.endswith(".result.json")) or
            (boundary == "audit" and p.name == "audit_0476_0500.json") or
            (boundary == "progress" and p.resolve() == project.progress_file.resolve()))
        if match and not fired:
            fired.append(True); raise KeyboardInterrupt("synthetic durable boundary")
    def crash_bytes(path, value):
        original_bytes(path, value)
        p = Path(path)
        if not fired and ((boundary == "backup" and p.parent.name == "chapters_before") or
                          (boundary == "chapter" and p.parent.resolve() == project.chapter_dir.resolve())):
            fired.append(True); raise KeyboardInterrupt("synthetic durable boundary")
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_bytes", crash_bytes)
        mp.setattr(module, "save_json", crash_json)
        with pytest.raises(KeyboardInterrupt): finalize_correction(context, result, stats, chunking)
    assert fired
    edited = boundary != "progress"
    if edited:
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project)
        assert run_production_path_preflight("fixture",repo_root=project.repository_root,
            model_selection="v41-flash",provider_selection=GOAT)["status"] != "PASS"
    approved = build_goat_periodic_audit_finalize_plan("fixture",repo_root=project.repository_root,
        start_chapter=476,end_chapter=500)
    def execute():
        return execute_goat_periodic_audit_finalize("fixture",repo_root=project.repository_root,
            start_chapter=476,end_chapter=500, expected_state_fingerprint=approved["state_fingerprint"],
            approval_token=approved["approval_token"],authorize_local_finalization=True)
    assert execute()["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
    strict_history(project)
    approved = build_goat_periodic_audit_finalize_plan("fixture",repo_root=project.repository_root,
        start_chapter=476,end_chapter=500)
    before = snapshot(project)
    assert execute()["status"] == "ALREADY_FINALIZED"
    assert snapshot(project) == before and len(context[2]) == 1


def test_valid_but_disconnected_second_audit_is_not_disposition_lineage(history):
    context, result, stats, chunking = correcting_audit(history)
    project = context[0]
    before = (project.chapter_dir / "0362.md").read_bytes()
    finalize_correction(context, result, stats, chunking)
    context, result, stats, chunking = correcting_audit(history, target=525, old="A man", new="Master Yi")
    project = context[0]
    # An unauthorized intervening revert can produce a locally valid second
    # transaction, but cannot bridge the preceding finalized AFTER identity.
    (project.chapter_dir / "0362.md").write_bytes(before)
    finalize_correction(context, result, stats, chunking)
    snapshot_before = snapshot(project)
    with pytest.raises(RuntimeError, match="disconnected"):
        effective_paid_request_inventory(project)
    assert snapshot(project) == snapshot_before and len(context[2]) == 1


def test_named_retroactive_transaction_without_outer_audit_never_resolves_disposition(history):
    from retroactive_transactions import execute_retroactive_transaction
    from noveltranslator_core import RETROACTIVE_SAFE_CATEGORIES
    project, module = history[0][:2]
    glossary = dict(entries={"甲": dict(english="A man", category="character", status="provisional")})
    staged = dict(entries={"甲": dict(english="Jia", category="character", status="provisional")})
    execute_retroactive_transaction(novel_id="fixture",audit_label="0363_0375",through_chapter=375,
        replacements=[dict(chinese="甲",old_english="A man",new_english="Jia",category="character")],
        glossary=glossary,staged_glossary=staged,safe_categories=RETROACTIVE_SAFE_CATEGORIES,
        committed_chapters=[362],chapter_map=module.chapter_map,output_dir=project.chapter_dir,
        retroactive_dir=project.retroactive_dir,backup_dir=project.backup_dir)
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="outer audit intent"):
        effective_paid_request_inventory(project)
    assert before == snapshot(project)


def test_legacy_intent_is_not_automatically_migrated_to_disposition_lineage(history):
    from production_accounting import canonical_sha256
    context, result, stats, chunking = correcting_audit(history)
    project = context[0]
    finalize_correction(context,result,stats,chunking)
    path = next(project.audit_dir.glob("*.finalization.json"))
    value = json.loads(path.read_bytes()); value.pop("historical_proof")
    value["intent_sha256"] = canonical_sha256({k:v for k,v in value.items() if k != "intent_sha256"})
    atomic_write_json(path,value)
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="legacy audit"):
        effective_paid_request_inventory(project)
    assert before == snapshot(project)
