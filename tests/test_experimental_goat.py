"""A-L zero-provider proofs, including the actual sequential engine lifecycle."""
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from create_novel import initial_project_state
from experimental_goat_policy import (ExperimentError, OPERATIONS, SEMANTIC, STRUCTURED,
    canonical, digest, external_path, qwen_catalog_policy)
from experimental_goat_budget import ExperimentBudget
from experimental_goat_runtime import (ExperimentalRuntime, ExperimentalRequestError,
    normalize_request, raw_record, accept_response)
from experimental_goat_experiment import (authorization_token, build_plan, create_s0,
    interleaved_slots, read_json, real_engine, require_live_verification, run_chronological,
    run_probes, runtime_for, validate_root)
from production_paid_request_journal import (_updated_record, load_matching_journal,
    inspect_paid_request_journals)
from noveltranslator_core import atomic_write_json

MODEL = qwen_catalog_policy()
LIMITS = {"global_ce":"3.00", "global_sends":150, "chapter_ce":"0.20", "chapter_sends":6}
REPO = Path(__file__).resolve().parents[1]
# Explicit physical contract, checked against the authoritative engine policy.
ENGINE_CEILINGS = {
    "chapter_translation": 50000,
    "state_extraction": 4000,
    "state_extraction_recovery": 8000,
    "state_json_repair": 5000,
    "chapter_glossary_review": 4000,
    "chapter_glossary_review_json_repair": 5000,
    "chapter_glossary_review_schema_correction": 2000,
    "chapter_glossary_review_semantic_correction": 2000,
    "chapter_glossary_review_semantic_correction_retry": 2000,
    "periodic_audit": 4000,
    "periodic_audit_json_repair": 4000,
    "periodic_audit_semantic_correction": 4000,
    "residual_cjk_micro_repair": 800,
    "residual_cjk_micro_repair_correction": 800,
    "strict_terminology_micro_repair": 800,
    "strict_terminology_micro_repair_correction": 800,
    "glossary_candidate_conflict_adjudication": 800,
    "generic_json_repair": 5000,
}
SOURCE = "第一章\n\n李明走进了大厅。他看见了一个人。那个人慢慢地站起来，向他点了点头。"
ENGLISH = "Chapter 1\n\nLi Ming walked into the hall. He saw a man there. The man slowly stood up and nodded to him."
STATE = {"new_terms":[], "continuity_summary":"Li Ming entered the hall and met a man.", "issues":[]}


def response(content="OK", finish="stop", model=None, usage=True, **changes):
    raw = NS(id="fake", model=model or MODEL.provider_model,
        choices=[NS(finish_reason=finish, message=NS(content=content))],
        usage=NS(prompt_tokens=100, completion_tokens=30, total_tokens=130,
                 prompt_tokens_details=NS(cached_tokens=20)) if usage else None)
    for key, value in changes.items():
        setattr(raw, key, value)
    return raw


class FakeClient:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda body:response())
        self.chat = NS(completions=NS(create=self.create))
    def create(self, **body):
        self.calls.append(copy.deepcopy(body))
        return self.handler(body)


def inputs(root, novel="ace", count=1, language="zh", profile="infinite_flow", residual=False):
    folder = root / "novels" / novel
    (folder / "data").mkdir(parents=True)
    (folder / "source").mkdir()
    (folder / "source/novel.txt").write_text(SOURCE, encoding="utf-8")
    config = {"id":novel, "title":"Fixture", "source_language":language, "target_language":"en",
              "profile":profile, "final_chapter":count, "audit_interval":25,
              "chapter_numbering":"global", "source_type":"txt",
              "residual_cjk_micro_repair":{"enabled":residual}}
    atomic_write_json(folder / "novel.json", config)
    (folder / "translation_addendum.txt").write_text("", encoding="utf-8")
    state = initial_project_state()
    state["chapters.json"] = [{"chapter":ch, "heading":f"Chapter {ch}", "text":SOURCE} for ch in range(1,count+1)]
    state["glossary.json"] = {"entries":{"future":{"english":"MatureLeak", "status":"locked"}}}
    state["progress.json"].update(last_completed=count, next_chapter=count+1, completed=list(range(1,count+1)),
                                 accounted_api_operation_ids=["mature-op"])
    for name, value in state.items():
        atomic_write_json(folder / "data" / name, value)
    return {"novel_id":novel, "profile":profile, "source_language":language, "last_chapter":count}


@pytest.fixture
def setup(tmp_path):
    production = tmp_path / "production"
    track = inputs(production)
    config = {"model":MODEL.to_dict(), "limits":LIMITS, "tracks":[track]}
    plan = build_plan(production_root=production, output_root=tmp_path / "experiment", config=config)
    root = create_s0(plan)
    return root, plan


def request(runtime, chapter=1, **extra):
    kwargs = {"model":MODEL.provider_model, "messages":[{"role":"user", "content":SOURCE}], "max_tokens":50000}
    kwargs.update(extra.pop("kwargs", {}))
    return runtime.request("chapter_translation", "ace", chapter, "infinite_flow", SOURCE, kwargs, **extra)


def test_A_import_has_no_io_or_activation(monkeypatch):
    import socket
    import builtins
    import command_code_provider_credentials as credentials
    def deny(*a, **k):
        pytest.fail("import IO/credential boundary crossed")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(credentials, "read_command_code_api_key", deny)
    monkeypatch.setattr(credentials, "load_platform_dpapi", deny)
    monkeypatch.setattr(Path, "mkdir", deny)
    monkeypatch.setattr(Path, "write_text", deny)
    monkeypatch.setattr(Path, "write_bytes", deny)
    # Execute source in independent module globals without replacing live dependency classes.
    for name in ("experimental_goat_policy", "experimental_goat_budget", "experimental_goat_runtime",
                 "experimental_goat_experiment", "experimental_goat_accounting", "experimental_goat"):
        import sys, types
        isolated = types.ModuleType("_import_safety_" + name)
        isolated.__file__ = str(REPO / (name + ".py"))
        sys.modules[isolated.__name__] = isolated
        try:
            exec(compile((REPO / (name + ".py")).read_text(encoding="utf-8-sig"), name, "exec"), isolated.__dict__)
        finally:
            sys.modules.pop(isolated.__name__, None)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_B_operation_identity_and_effort(operation):
    from production_operation_policy import policy_for_model
    assert set(ENGINE_CEILINGS) == OPERATIONS
    ceiling = ENGINE_CEILINGS[operation]
    if operation != "chapter_translation":
        assert ceiling == policy_for_model("pro").for_operation(operation).max_tokens
    wire = normalize_request(MODEL, operation, {"model":MODEL.provider_model,
        "messages":[{"role":"user", "content":"source"}], "max_tokens":ceiling})
    assert wire["model"] == "Qwen/Qwen3.8-27B"
    assert wire["reasoning_effort"] == ("low" if operation in STRUCTURED else "medium")
    assert wire["max_completion_tokens"] == ceiling
    assert "max_tokens" not in wire
    assert "deepseek" not in canonical(wire)


def test_C_transport_configuration():
    from benchmark_longitudinal_provider import create_provider_client
    captured = []
    create_provider_client(api_key="fake", client_factory=lambda **k:captured.append(k))
    assert captured == [{"api_key":"fake", "base_url":"https://api.commandcode.ai/provider/v1", "max_retries":0}]
    assert MODEL.endpoint == "/chat/completions"


@pytest.mark.parametrize("bad", ["wrong_model", "usage_missing", "usage_bool", "usage_negative", "cache", "content", "total", "over_cap"])
def test_D_response_rejection(setup, bad):
    root, plan = setup
    raw = response(ENGLISH)
    if bad == "wrong_model": raw.model = "deepseek/deepseek-v4.1-flash"
    if bad == "usage_missing": raw.usage = None
    if bad == "usage_bool": raw.usage.prompt_tokens = True
    if bad == "usage_negative": raw.usage.completion_tokens = -1
    if bad == "cache": raw.usage.prompt_tokens_details.cached_tokens = 101
    if bad == "content": raw.choices[0].message.content = ["bad"]
    if bad == "total": raw.usage.total_tokens = 129
    if bad == "over_cap": raw.usage.completion_tokens = 50001; raw.usage.total_tokens = 50101
    client = FakeClient(lambda body:raw)
    runtime = runtime_for(plan, client)
    with pytest.raises(ExperimentalRequestError): request(runtime)
    assert len(client.calls) == 1
    with pytest.raises(ExperimentalRequestError): request(runtime)
    assert len(client.calls) == 1
    journal = inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")
    assert journal["records"][0]["status"] == "response_recorded"
    with runtime.budget.locked():
        assert list(runtime.budget.document["records"].values())[0]["status"] == "reserved"


def test_D_cache_accounting_authoritative_totals():
    wire = normalize_request(MODEL, "chapter_translation", {"model":MODEL.provider_model,
        "messages":[{"role":"user", "content":"source"}], "max_tokens":50000})
    raw = response(ENGLISH)
    raw.usage.completion_tokens_details = NS(reasoning_tokens=10000)
    stats = accept_response(MODEL, raw_record(raw), wire, prompt_upper=1000)["stats"]
    assert stats["cost_credit_equivalent"] == str(MODEL.charge(100,30,20))
    del raw.usage.prompt_tokens_details
    stats = accept_response(MODEL, raw_record(raw), wire, prompt_upper=1000)["stats"]
    assert stats["cached_tokens_exposed"] is False
    assert stats["cost_credit_equivalent"] == str(MODEL.charge(100,30))


def test_E_poisoned_response_replay_and_changed_identity(setup):
    root, plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = runtime_for(plan, client)
    first = request(runtime)
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body:pytest.fail("poisoned transport called")
    second = request(runtime)
    assert second["replayed"] is True and second["api_called"] is False
    assert first["operation_id"] == second["operation_id"]
    assert (root / "budget.json").read_bytes() == before
    with pytest.raises(RuntimeError, match="identity mismatch"):
        request(runtime, kwargs={"messages":[{"role":"user", "content":"changed"}]})
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_E_started_uncertain_never_resend(setup, status):
    root, plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = runtime_for(plan, client)
    result = request(runtime)
    path = result["journal_path"]
    record = load_matching_journal(path, {})
    atomic_write_json(path, _updated_record(record, status=status, response={}, response_sha256=""))
    client.handler = lambda body:pytest.fail("resend forbidden")
    with pytest.raises(RuntimeError, match="uncertain remote outcome"):
        request(runtime)
    assert len(client.calls) == 1


def test_E_corrupt_journal(setup):
    root, plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = runtime_for(plan, client)
    result = request(runtime)
    result["journal_path"].write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError): request(runtime)
    assert len(client.calls) == 1


@pytest.mark.parametrize("limit,value,chapter", [("global_ce","0.01",1), ("chapter_ce","0.01",1),
    ("global_sends",1,2), ("chapter_sends",1,1)])
def test_F_budget_refusal_before_transport(setup, limit, value, chapter):
    root, plan = setup
    limits = {**LIMITS, limit:value}
    budget = ExperimentBudget(root, plan_sha256=plan["plan_sha256"], limits=limits, model=MODEL)
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = ExperimentalRuntime(root=root, model=MODEL, budget=budget, client=client, plan_sha256=plan["plan_sha256"])
    if "sends" in limit:
        request(runtime)
        kwargs = {"model":MODEL.provider_model, "messages":[{"role":"user", "content":"source"}], "max_tokens":800}
        with pytest.raises(ExperimentError, match="ceiling exceeded"):
            runtime.request("residual_cjk_micro_repair", "ace", chapter, "infinite_flow", SOURCE, kwargs)
        assert len(client.calls) == 1
    else:
        with pytest.raises(ExperimentError, match="ceiling exceeded"): request(runtime)
        assert len(client.calls) == 0
        assert inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"][0]["status"] == "planned"


def test_F_reservation_visible_before_send_ambiguity_retained(setup):
    root, plan = setup
    def fail(body):
        ledger = read_json(root / "budget.json")
        assert list(ledger["records"].values())[0]["status"] == "reserved"
        journal = inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")
        assert journal["records"][0]["status"] == "started"
        raise TimeoutError("secret-like-provider-error-must-not-be-saved")
    client = FakeClient(fail)
    runtime = runtime_for(plan, client)
    with pytest.raises(ExperimentalRequestError): request(runtime)
    before = (root / "budget.json").read_bytes()
    with pytest.raises(RuntimeError): request(runtime)
    assert (root / "budget.json").read_bytes() == before
    assert len(client.calls) == 1
    assert "secret-like" not in next((root / "tracks/ace/data/provider_request_journal").glob("*.json")).read_text()


def test_F_single_writer_rejected(setup):
    root, plan = setup
    runtime = runtime_for(plan, FakeClient())
    with runtime.budget.locked():
        with pytest.raises(RuntimeError):
            with runtime.budget.locked(): pass


def test_H_fresh_S0_no_leak_and_create_once(setup):
    root, plan = setup
    state = root / "tracks/ace/data"
    assert read_json(state / "glossary.json") == {"entries":{}}
    assert read_json(state / "continuity.json") == {"latest_chapter":0, "summary":""}
    assert read_json(state / "progress.json")["completed"] == []
    assert "accounted_api_operation_ids" not in read_json(state / "progress.json")
    assert not list((state / "provider_request_journal").iterdir())
    assert not list((root / "tracks/ace/output/pending").iterdir())
    assert validate_root(root) == plan
    with pytest.raises(ExperimentError): create_s0(plan)


def test_I_exact_25_slots():
    tracks = [{"novel_id":"ace_evolution", "last_chapter":13}, {"novel_id":"rogue_first_prince", "last_chapter":12}]
    slots = interleaved_slots(tracks)
    assert slots == [{"track":name,"chapter":ch} for ch in range(1,13) for name in ("ace_evolution", "rogue_first_prince")] + [{"track":"ace_evolution", "chapter":13}]
    assert len(slots) == 25


@pytest.mark.parametrize("relative", ["", "output", "../", "frozen/output"])
def test_J_protected_output_refused(tmp_path, relative):
    protected = tmp_path / "production"
    protected.mkdir()
    with pytest.raises(ExperimentError): external_path(protected / relative, [protected])


def test_K_catalog_flag_cannot_grant_verification(setup):
    root, plan = setup
    atomic_write_json(root / "capability.json", {"status":"live_verified"})
    with pytest.raises((ExperimentError, FileNotFoundError)):
        require_live_verification(plan, runtime_for(plan, None))


def test_K_activation_authorization_precedes_credentials(setup, monkeypatch):
    root, plan = setup
    import command_code_provider_credentials as credentials
    import experimental_goat
    monkeypatch.setattr(credentials, "command_code_api_key_scope", lambda:pytest.fail("credential read"))
    with pytest.raises(ExperimentError):
        with experimental_goat.activate_client(plan, "probe", "bad", True): pass
    with pytest.raises(FileNotFoundError):
        with experimental_goat.activate_client(plan, "run", authorization_token(plan,"run"), True): pass


def probe(root, plan):
    return run_probes(root, client=FakeClient(), token=authorization_token(plan,"probe"), authorize_paid=True)


def test_F_K_probes_share_budget_and_provenance(setup):
    root, plan = setup
    client = FakeClient()
    result = run_probes(root, client=client, token=authorization_token(plan,"probe"), authorize_paid=True)
    assert result["status"] == "live_verified"
    assert [c["reasoning_effort"] for c in client.calls] == ["medium", "low"]
    assert all(c["max_completion_tokens"] == 64 for c in client.calls)
    assert require_live_verification(plan, runtime_for(plan,None)) == result
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body:pytest.fail("probe replay called transport")
    run_probes(root, client=client, token=authorization_token(plan,"probe"), authorize_paid=True)
    assert (root / "budget.json").read_bytes() == before


@pytest.mark.parametrize("failure", [None, "length", "wrong_model", "missing_usage", "ambiguous"])
def test_G_L_real_lifecycle_commit_and_terminal_failures(setup, failure, monkeypatch):
    root, plan = setup
    probe(root, plan)
    import production_goat_cap_recovery as cap
    monkeypatch.setattr(cap, "run_goat_translation_stage", lambda **k:pytest.fail("DeepSeek recovery called"))
    monkeypatch.setattr(cap, "eligible_envelope", lambda *a, **k:pytest.fail("DeepSeek recovery predicate entered"))
    def handler(body):
        if failure == "ambiguous": raise TimeoutError()
        if body["max_completion_tokens"] == 50000:
            return response(ENGLISH, finish="length" if failure == "length" else "stop",
                model="wrong" if failure == "wrong_model" else None, usage=failure != "missing_usage")
        return response(json.dumps(STATE))
    client = FakeClient(handler)
    result = run_chronological(root, client=client, token=authorization_token(plan,"run"), authorize_paid=True)
    assert result["slots"][0]["status"] == ("committed" if failure is None else "stopped")
    assert (root / "tracks/ace/output/chapters/0001.md").exists() == (failure is None)
    assert len(client.calls) == (2 if failure is None else 1)
    assert all(c["model"] == MODEL.provider_model for c in client.calls)
    assert all(c["reasoning_effort"] in {"low", "medium"} for c in client.calls)
    assert len(inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"]) == len(client.calls)


def test_L_forced_state_and_glossary_JSON_repairs(setup):
    root, plan = setup
    probe(root, plan)
    malformed_state = '{"new_terms":[],"continuity_summary":"ok","issues": [!!!]}'
    sequence = [response(ENGLISH), response(malformed_state), response(json.dumps(STATE))]
    client = FakeClient(lambda body:sequence.pop(0))
    runtime = runtime_for(plan,client)
    with real_engine(plan, root / "tracks/ace", runtime) as engine:
        # Disable only local salvage to force the real bounded JSON repair branch.
        engine.salvage_state_json_without_issues = lambda content:None
        assert engine.process_chapter(1)
    assert [r["operation"] for r in runtime.calls] == ["chapter_translation", "state_extraction", "state_json_repair"]
    assert [c["reasoning_effort"] for c in client.calls] == ["medium", "low", "low"]
    assert [c["max_completion_tokens"] for c in client.calls] == [50000, 4000, 5000]


def test_L_real_glossary_review_and_structured_review_repair(setup):
    root, plan = setup
    probe(root,plan)
    candidate = {"chinese":"李明", "english":"Li Ming", "category":"character", "status":"provisional"}
    state = {**STATE, "new_terms":[candidate]}
    decisions = {"decisions":[{"candidate_id":"c01", "action":"accept", "english":"Li Ming", "category":"character", "notes":""}], "issues":[]}
    sequence = [response(ENGLISH), response(json.dumps(state)), response('{"decisions":broken}'), response(json.dumps(decisions))]
    client = FakeClient(lambda body:sequence.pop(0))
    runtime = runtime_for(plan,client)
    with real_engine(plan, root / "tracks/ace", runtime) as engine:
        assert engine.process_chapter(1)
    assert [r["operation"] for r in runtime.calls] == ["chapter_translation", "state_extraction", "chapter_glossary_review", "chapter_glossary_review_json_repair"]
    assert [c["reasoning_effort"] for c in client.calls] == ["medium", "low", "medium", "low"]
    assert [c["max_completion_tokens"] for c in client.calls] == [50000, 4000, 4000, 5000]
    assert read_json(root / "tracks/ace/data/glossary.json")["entries"]["李明"]["english"] == "Li Ming"
    assert len(inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"]) == 4


@pytest.mark.parametrize("language,fragment,profile", [("zh","李明","infinite_flow"), ("ko","리밍","korean_fantasy")])
def test_L_real_bilingual_residual_repair(tmp_path, language, fragment, profile):
    production = tmp_path / "production"
    track = inputs(production, language=language, profile=profile, residual=True)
    if language == "ko":
        chapters = [{"chapter":1,"heading":"제1장","text":"제1장\n\n리밍은 홀로 들어갔다. 그는 한 남자를 보았다. 남자가 천천히 일어서서 고개를 끄덕였다."}]
        atomic_write_json(production / "novels/ace/data/chapters.json",chapters)
    plan = build_plan(production_root=production, output_root=tmp_path / "experiment",
                      config={"model":MODEL.to_dict(),"limits":LIMITS,"tracks":[track]})
    root = create_s0(plan)
    probe(root,plan)
    translation = ENGLISH.replace("Li Ming", fragment)
    sequence = [response(translation),response(json.dumps({"repairs":[{"old_text":fragment,"new_text":"Li Ming"}]})),response(json.dumps(STATE))]
    client = FakeClient(lambda body:sequence.pop(0))
    runtime = runtime_for(plan,client)
    with real_engine(plan,root / "tracks/ace",runtime) as engine:
        assert engine.process_chapter(1)
    assert [r["operation"] for r in runtime.calls] == ["chapter_translation","residual_cjk_micro_repair","state_extraction"]
    assert (root / "tracks/ace/output/chapters/0001.md").read_text(encoding="utf-8") == ENGLISH
    assert [c["reasoning_effort"] for c in client.calls] == ["medium","medium","low"]
    assert [c["max_completion_tokens"] for c in client.calls] == [50000, 800, 4000]


def test_I_bilingual_track_quarantine_preserves_every_slot(tmp_path):
    production = tmp_path / "production"
    tracks = [inputs(production,"ace_evolution",13), inputs(production,"rogue_first_prince",12,"ko","korean_fantasy")]
    plan = build_plan(production_root=production, output_root=tmp_path / "experiment",
        config={"model":MODEL.to_dict(),"limits":LIMITS,"tracks":tracks})
    root = create_s0(plan)
    probe(root,plan)
    def handler(body):
        if body["max_completion_tokens"] == 50000:
            if "CHINESE SOURCE" in body["messages"][-1]["content"]:
                raise TimeoutError("unsafe Ace")
            return response(ENGLISH)
        return response(json.dumps(STATE))
    client = FakeClient(handler)
    report = run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
    assert len(report["slots"]) == 25
    assert report["slots"][0]["status"] == "stopped"
    assert [s["status"] for s in report["slots"] if s["track"] == "rogue_first_prince"] == ["committed"]*12
    assert [s["status"] for s in report["slots"] if s["track"] == "ace_evolution"] == ["stopped"]+["quarantined_unattempted"]*12
    assert len(client.calls) == 25  # One ambiguous ACE send; twelve normal two-operation chapters.
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body:pytest.fail("completed/quarantined slot resent")
    assert run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True) == report
    assert (root / "budget.json").read_bytes() == before


@pytest.mark.parametrize("phase", ["started_write", "response_write", "settlement_write"])
def test_E_F_fault_injection_at_durable_boundaries(setup, monkeypatch, phase):
    root, plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = runtime_for(plan,client)
    import experimental_goat_runtime as transport
    import experimental_goat_budget as budgets
    original = transport.atomic_write_json
    def write(path, value):
        if (phase == "started_write" and value.get("status") == "started") or (phase == "response_write" and value.get("status") == "response_recorded"):
            raise OSError("injected persistence failure")
        return original(path,value)
    monkeypatch.setattr(transport,"atomic_write_json",write)
    original_budget = budgets.atomic_write_json
    def budget_write(path,value):
        if phase == "settlement_write" and any(r["status"] == "accepted" for r in value["records"].values()):
            raise OSError("injected settlement failure")
        return original_budget(path,value)
    monkeypatch.setattr(budgets,"atomic_write_json",budget_write)
    with pytest.raises((OSError,RuntimeError)):
        request(runtime)
    assert len(client.calls) == (0 if phase == "started_write" else 1)
    records = read_json(root / "budget.json")["records"]
    assert list(records.values())[0]["status"] == ("released" if phase == "started_write" else "reserved")
    monkeypatch.setattr(transport,"atomic_write_json",original)
    monkeypatch.setattr(budgets,"atomic_write_json",original_budget)
    client.handler = lambda body:pytest.fail("poisoned fault replay transport")
    if phase == "settlement_write":
        assert request(runtime)["replayed"] is True
        assert list(read_json(root / "budget.json")["records"].values())[0]["status"] == "accepted"
    else:
        with pytest.raises(RuntimeError): request(runtime)


def test_J_runtime_root_cannot_be_production_or_code(tmp_path):
    for root in (Path("D:/NovelTranslator"), REPO, tmp_path):
        if root == tmp_path:
            continue
        with pytest.raises(ExperimentError):
            ExperimentBudget(root,plan_sha256="a"*64,limits=LIMITS,model=MODEL)


def test_H_J_production_frozen_state_unchanged(setup):
    root,plan = setup
    production = Path(plan["production_root"])
    before = {str(p):p.read_bytes() for p in production.rglob("*") if p.is_file()}
    probe(root,plan)
    client = FakeClient(lambda body:response(ENGLISH if body["max_completion_tokens"] == 50000 else json.dumps(STATE)))
    run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
    assert {str(p):p.read_bytes() for p in production.rglob("*") if p.is_file()} == before
    frozen = production.parent / "frozen"
    frozen.mkdir()
    with pytest.raises(ExperimentError):
        build_plan(production_root=production,output_root=frozen / "experiment",
                   frozen_roots=[frozen],config={"model":MODEL.to_dict(),"limits":LIMITS,"tracks":plan["tracks"]})


@pytest.mark.parametrize("key,value", [("capability_status","live_verified"),("retries",1),("fallback","deepseek"),
    ("provider","direct_deepseek"),("base_url","https://api.deepseek.com"),("endpoint","/other"),
    ("provider_invoice_claimed",True),("reasoning_field","thinking"),("auxiliary_ceiling",8001),
    ("input_rate",0.4),("output_rate","NaN"),("cache_rate","1.0")])
def test_B_K_policy_drift_rejected(key,value):
    from experimental_goat_policy import ModelPolicy
    policy = MODEL.to_dict()
    policy[key] = value
    with pytest.raises((ExperimentError,TypeError)):
        ModelPolicy.from_dict(policy)


@pytest.mark.parametrize("patch", [{"model":"deepseek-flash"},{"max_tokens":True},{"max_tokens":0},
    {"max_tokens":50001},{"max_completion_tokens":50000},{"reasoning_effort":"xhigh"},
    {"extra_body":{"thinking":{"type":"enabled"}}},{"stream":True},{"n":2}])
def test_B_request_smuggling_fails_before_send(setup,patch):
    root,plan = setup
    client = FakeClient()
    with pytest.raises(ExperimentError):
        request(runtime_for(plan,client),kwargs=patch)
    assert not client.calls
    assert not (root / "budget.json").exists()


@pytest.mark.parametrize("field,value", [("profile","korean_fantasy"),("source","changed"),("translation","changed")])
def test_E_source_profile_dependent_text_identity_bound(setup,field,value):
    root,plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    runtime = runtime_for(plan,client)
    request(runtime)
    kwargs = {"model":MODEL.provider_model,"messages":[{"role":"user","content":SOURCE}],"max_tokens":50000}
    values = {"profile":"infinite_flow","source":SOURCE,"translation":""}
    values[field] = value
    with pytest.raises(RuntimeError,match="identity mismatch"):
        runtime.request("chapter_translation","ace",1,values["profile"],values["source"],kwargs,translation=values["translation"])
    assert len(client.calls) == 1


@pytest.mark.parametrize("which", ["budget","probe","capability","pending"])
def test_E_K_corruption_blocks_without_transport(setup,which):
    root,plan = setup
    probe(root,plan)
    client = FakeClient(lambda body:pytest.fail("corrupt evidence reached transport"))
    if which == "budget":
        (root / "budget.json").write_text("{}",encoding="utf-8")
    elif which == "probe":
        (root / "probes/low.json").write_text("{}",encoding="utf-8")
    elif which == "capability":
        saved = read_json(root / "capability.json")
        saved["model_policy_sha256"] = "b"*64
        atomic_write_json(root / "capability.json",saved)
    else:
        (root / "tracks/ace/output/pending/0001.md").write_text(ENGLISH,encoding="utf-8")
        atomic_write_json(root / "tracks/ace/output/pending/0001.json",{"chapter":1,"stage":"translated"})
        report = run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
        assert report["slots"][0]["status"] == "stopped"
        assert not client.calls
        return
    with pytest.raises((ExperimentError,RuntimeError)):
        run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
    assert not client.calls


def test_H_changed_immutable_input_prevents_S0_before_write(tmp_path):
    production = tmp_path / "production"
    track = inputs(production)
    plan = build_plan(production_root=production,output_root=tmp_path / "experiment",
        config={"model":MODEL.to_dict(),"limits":LIMITS,"tracks":[track]})
    (production / "novels/ace/translation_addendum.txt").write_text("drift",encoding="utf-8")
    with pytest.raises(ExperimentError): create_s0(plan)
    assert not (tmp_path / "experiment").exists()
    assert not (tmp_path / "experiment.staging").exists()


def test_L_pending_recovery_poisoned_translation_transport(setup):
    root,plan = setup
    probe(root,plan)
    state = {**STATE,"new_terms":[{"chinese":"李明","english":"Li Ming","category":"character","status":"provisional"}]}
    decisions = {"decisions":[{"candidate_id":"c01","action":"accept","english":"Li Ming","category":"character","notes":""}],"issues":[]}
    sequence = [response(ENGLISH),response(json.dumps(state)),response(json.dumps(decisions))]
    client = FakeClient(lambda body:sequence.pop(0))
    runtime = runtime_for(plan,client)
    with real_engine(plan,root / "tracks/ace",runtime) as engine:
        original = engine.save_json
        def interrupt(path,value):
            if str(path).replace("\\","/") == "output/pending/0001.json" and value.get("stage") == "reviewed":
                raise OSError("interrupted after paid glossary response")
            return original(path,value)
        engine.save_json = interrupt
        assert engine.process_chapter(1) is False
    assert len(client.calls) == 3
    assert (root / "tracks/ace/output/pending/0001.md").read_text(encoding="utf-8") == ENGLISH
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body:pytest.fail("paid pending/review resent")
    with real_engine(plan,root / "tracks/ace",runtime) as engine:
        assert engine.process_chapter(1)
    assert (root / "budget.json").read_bytes() == before
    assert len(client.calls) == 3


def test_L_natural_due_audit_and_JSON_repair_are_journaled(tmp_path):
    production = tmp_path / "production"
    track = inputs(production)
    config_path = production / "novels/ace/novel.json"
    config = read_json(config_path)
    config["audit_interval"] = 1  # Disposable policy; never change real novel intervals.
    atomic_write_json(config_path,config)
    plan = build_plan(production_root=production,output_root=tmp_path / "experiment",
        config={"model":MODEL.to_dict(),"limits":LIMITS,"tracks":[track]})
    root = create_s0(plan)
    probe(root,plan)
    state = {**STATE,"new_terms":[{"chinese":"李明","english":"Li Ming","category":"character","status":"provisional"}]}
    review = {"decisions":[{"candidate_id":"c01","action":"accept","english":"Li Ming","category":"character","notes":""}],"issues":[]}
    audit = {"decisions":[{"candidate_id":"c01","action":"keep","english":"Li Ming","category":"character","notes":""}],"issues":[]}
    sequence = [response(ENGLISH),response(json.dumps(state)),response(json.dumps(review)),
                response('{"decisions":broken}'),response(json.dumps(audit))]
    client = FakeClient(lambda body:sequence.pop(0))
    report = run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
    assert report["slots"][0]["status"] == "committed"
    assert len(client.calls) == 5
    assert [x["reasoning_effort"] for x in client.calls] == ["medium","low","medium","medium","low"]
    assert [x["max_completion_tokens"] for x in client.calls] == [50000, 4000, 4000, 4000, 4000]
    assert read_json(root / "tracks/ace/data/progress.json")["last_audited_chapter"] == 1
    records = inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"]
    assert {r["operation_kind"] for r in records} == {"chapter_translation","state_extraction","chapter_glossary_review","periodic_audit","periodic_audit_json_repair"}
    client.handler = lambda body:pytest.fail("audited commit replay sent")
    assert run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True) == report


def test_I_committed_prose_corruption_does_not_skip_silently(setup):
    root,plan = setup
    probe(root,plan)
    client = FakeClient(lambda body:response(ENGLISH if body["max_completion_tokens"] == 50000 else json.dumps(STATE)))
    run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)
    (root / "tracks/ace/output/chapters/0001.md").write_text("changed",encoding="utf-8")
    client.handler = lambda body:pytest.fail("corrupted commit resent")
    with pytest.raises(ExperimentError,match="committed slot proof"):
        run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)


def test_I_corrupted_results_refuse_execution(setup):
    root,plan = setup
    probe(root,plan)
    value = read_json(root / "results.json")
    value["slots"][0]["status"] = "committed"
    atomic_write_json(root / "results.json",value)
    client = FakeClient(lambda body:pytest.fail("corrupt result reached transport"))
    with pytest.raises(ExperimentError,match="result slot identity"):
        run_chronological(root,client=client,token=authorization_token(plan,"run"),authorize_paid=True)


def test_C_actual_SDK_endpoint_and_zero_hidden_retries():
    import httpx2 as httpx
    from openai import OpenAI, InternalServerError
    from benchmark_longitudinal_provider import create_provider_client
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(500,json={"error":{"message":"fake provider failure"}})
    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = create_provider_client(api_key="synthetic-key",client_factory=lambda **kwargs:OpenAI(http_client=http_client,**kwargs))
    body = normalize_request(MODEL,"chapter_translation",{"model":MODEL.provider_model,"max_tokens":50000,
        "messages":[{"role":"user","content":"source"}]})
    try:
        with pytest.raises(InternalServerError): client.chat.completions.create(**body)
        assert len(calls) == 1
        assert str(calls[0].url) == "https://api.commandcode.ai/provider/v1/chat/completions"
        wire = json.loads(calls[0].content)
        assert wire == body and "max_tokens" not in wire
    finally:
        client.close()


def test_B_physical_SHA_is_exact_wire_hash(setup):
    root,plan = setup
    client = FakeClient(lambda body:response(ENGLISH))
    result = request(runtime_for(plan,client))
    assert result["request_sha256"] == digest(client.calls[0])
    saved = load_matching_journal(result["journal_path"],{})
    assert saved["experiment_plan_sha256"] == plan["plan_sha256"]
    assert saved["model_policy_sha256"] == MODEL.sha256


def test_K_engine_catalog_only_refuses_before_bootstrap(setup):
    root,plan = setup
    client = FakeClient(lambda body:pytest.fail("catalog-only chapter provider"))
    with pytest.raises(FileNotFoundError):
        with real_engine(plan,root / "tracks/ace",runtime_for(plan,client)): pass
    assert not client.calls
    assert not (root / "budget.json").exists()


def test_F_L_concurrent_engine_context_refused(setup):
    root,plan = setup
    probe(root,plan)
    runtime = runtime_for(plan,FakeClient())
    with real_engine(plan,root / "tracks/ace",runtime):
        with pytest.raises(ExperimentError,match="shared core/cwd"):
            with real_engine(plan,root / "tracks/ace",runtime): pass


CEILING_SEAMS = [("chapter_translation", 50000),
                 ("residual_cjk_micro_repair", 8000), ("capability_probe", 64)]


def ceiling_request(operation, ceiling):
    return {"model": MODEL.provider_model,
            "messages": [{"role": "user", "content": "Reply with OK." if operation == "capability_probe" else SOURCE}],
            "max_tokens": ceiling}


@pytest.mark.parametrize("operation,maximum", CEILING_SEAMS)
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.0, "800", None, "over"])
def test_B_invalid_ceiling_refuses_before_journal_or_reservation(setup, operation, maximum, invalid):
    root, plan = setup
    client = FakeClient(lambda body: pytest.fail("invalid ceiling reached transport"))
    kwargs = ceiling_request(operation, maximum + 1 if invalid == "over" else invalid)
    with pytest.raises(ExperimentError):
        runtime_for(plan, client).request(operation, "ace", 1, "infinite_flow", SOURCE, kwargs,
                                         probe_effort="medium" if operation == "capability_probe" else None)
    assert not client.calls
    assert not (root / "budget.json").exists()
    assert not list((root / "tracks/ace/data/provider_request_journal").glob("*.json"))


@pytest.mark.parametrize("operation,maximum", CEILING_SEAMS)
@pytest.mark.parametrize("seam", ["missing", "wire_only", "both"])
def test_B_completion_ceiling_uses_only_legacy_input_seam(setup, operation, maximum, seam):
    root, plan = setup
    kwargs = ceiling_request(operation, maximum)
    if seam != "both":
        kwargs.pop("max_tokens")
    if seam != "missing":
        kwargs["max_completion_tokens"] = maximum
    client = FakeClient(lambda body: pytest.fail("smuggled ceiling reached transport"))
    with pytest.raises(ExperimentError):
        runtime_for(plan, client).request(operation, "ace", 1, "infinite_flow", SOURCE, kwargs,
                                         probe_effort="medium" if operation == "capability_probe" else None)
    assert not client.calls
    assert not (root / "budget.json").exists()
    assert not list((root / "tracks/ace/data/provider_request_journal").glob("*.json"))


@pytest.mark.parametrize("operation,maximum", CEILING_SEAMS)
@pytest.mark.parametrize("boundary", ["minimum", "maximum"])
def test_B_valid_ceiling_boundaries_are_not_widened(operation, maximum, boundary):
    ceiling = 1 if boundary == "minimum" else maximum
    wire = normalize_request(MODEL, operation, ceiling_request(operation, ceiling),
                             probe_effort="medium" if operation == "capability_probe" else None)
    assert wire["max_completion_tokens"] == ceiling
    assert "max_tokens" not in wire


@pytest.mark.parametrize("operation", sorted(ENGINE_CEILINGS))
def test_F_exact_operation_ceiling_binds_reservation_and_physical_SHA(setup, operation):
    root, plan = setup
    ceiling = ENGINE_CEILINGS[operation]
    expected_body = {"model": MODEL.provider_model, "messages": [{"role": "user", "content": SOURCE}],
                     "reasoning_effort": MODEL.effort(operation), "max_completion_tokens": ceiling}
    # The input envelope retains the existing conservative 8192-token margin.
    prompt_upper = len(canonical(expected_body).encode("utf-8")) + 8192
    expected_reserve = str(MODEL.charge(prompt_upper, ceiling))
    def respond(body):
        assert body == expected_body
        record = next(iter(read_json(root / "budget.json")["records"].values()))
        assert record["status"] == "reserved"
        assert record["prompt_upper"] == prompt_upper
        assert record["reserved_ce"] == expected_reserve
        assert record["request_sha256"] == digest(body)
        journal = inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"]
        assert journal[0]["status"] == "started"
        return response()
    client = FakeClient(respond)
    runtime = runtime_for(plan, client)
    result = runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, ceiling_request(operation, ceiling))
    assert result["request_sha256"] == digest(client.calls[0])
    record = read_json(root / "budget.json")["records"][result["operation_id"]]
    assert record["status"] == "accepted"
    assert record["reserved_ce"] == expected_reserve
    assert record["actual_ce"] == str(MODEL.charge(100, 30, 20))
    if ceiling < 8000:
        widened = {**expected_body, "max_completion_tokens": 8000}
        assert record["reserved_ce"] != str(MODEL.charge(len(canonical(widened).encode("utf-8")) + 8192, 8000))
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body: pytest.fail("exact-ceiling replay reached transport")
    replay = runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, ceiling_request(operation, ceiling))
    assert replay["replayed"] is True and replay["api_called"] is False
    assert (root / "budget.json").read_bytes() == before
    assert len(client.calls) == 1


@pytest.mark.parametrize("original_ceiling", [800, 8000])
@pytest.mark.parametrize("status", ["response_recorded", "started", "uncertain"])
def test_E_800_and_8000_ceiling_identities_cannot_cross_replay(setup, original_ceiling, status):
    root, plan = setup
    operation = "residual_cjk_micro_repair"
    client = FakeClient()
    runtime = runtime_for(plan, client)
    kwargs = ceiling_request(operation, original_ceiling)
    result = runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, kwargs)
    path = result["journal_path"]
    original = load_matching_journal(path, {})
    if status != "response_recorded":
        atomic_write_json(path, _updated_record(original, status=status, response={}, response_sha256=""))
    other = ceiling_request(operation, 8000 if original_ceiling == 800 else 800)
    original_identity, original_wire = runtime.identity(operation, "ace", 1, "infinite_flow", SOURCE, kwargs)
    other_identity, other_wire = runtime.identity(operation, "ace", 1, "infinite_flow", SOURCE, other)
    assert original_identity["request_sha256"] == digest(original_wire)
    assert other_identity["request_sha256"] == digest(other_wire)
    assert original_identity["request_sha256"] != other_identity["request_sha256"]
    assert original_identity["operation_id"] != other_identity["operation_id"]
    before_journal, before_budget = path.read_bytes(), (root / "budget.json").read_bytes()
    client.handler = lambda body: pytest.fail("ceiling drift reached transport")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, other)
    if status == "response_recorded":
        assert runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, kwargs)["replayed"] is True
    else:
        with pytest.raises(RuntimeError, match="uncertain remote outcome"):
            runtime.request(operation, "ace", 1, "infinite_flow", SOURCE, kwargs)
    assert path.read_bytes() == before_journal
    assert (root / "budget.json").read_bytes() == before_budget
    assert len(client.calls) == 1


@pytest.mark.parametrize("ceiling", [800, 2000, 4000, 5000, 8000])
def test_D_usage_cannot_exceed_exact_auxiliary_wire_ceiling(setup, ceiling):
    root, plan = setup
    raw = response()
    raw.usage.completion_tokens = ceiling + 1
    raw.usage.total_tokens = 100 + ceiling + 1
    client = FakeClient(lambda body: raw)
    runtime = runtime_for(plan, client)
    kwargs = ceiling_request("generic_json_repair", ceiling)
    with pytest.raises(ExperimentalRequestError, match="reserved token envelope"):
        runtime.request("generic_json_repair", "ace", 1, "infinite_flow", SOURCE, kwargs)
    before = (root / "budget.json").read_bytes()
    client.handler = lambda body: pytest.fail("rejected response resent")
    with pytest.raises(ExperimentalRequestError):
        runtime.request("generic_json_repair", "ace", 1, "infinite_flow", SOURCE, kwargs)
    assert len(client.calls) == 1
    assert (root / "budget.json").read_bytes() == before
    journal = inspect_paid_request_journals(root / "tracks/ace/data/provider_request_journal")["records"]
    assert journal[0]["status"] == "response_recorded"
    assert next(iter(read_json(root / "budget.json")["records"].values()))["status"] == "reserved"


@pytest.mark.parametrize("content,finish", [("OK","stop"), ("OK.","stop"),
    ("\n\nOK.","stop"), ("  OK. \n","stop"), ("Acknowledged.","stop"),
    ("","stop"), ("partial response","length"), ("","length")])
def test_K_capability_text_and_finish_are_diagnostics(setup, content, finish):
    from experimental_goat_policy import text_sha
    root, plan = setup
    client = FakeClient(lambda body:response(content, finish=finish))
    record = run_probes(root, client=client, token=authorization_token(plan,"probe"), authorize_paid=True)
    assert len(client.calls) == 2
    assert require_live_verification(plan, runtime_for(plan,None)) == record
    for diagnostic in record["probe_evidence"]:
        assert diagnostic["returned_model"] == MODEL.provider_model
        assert diagnostic["finish_reason"] == finish
        assert diagnostic["visible_content_sha256"] == text_sha(content)
        assert diagnostic["prompt_tokens"] == 100
        assert diagnostic["completion_tokens"] == 30
        assert diagnostic["cache_tokens"] == 20
        assert diagnostic["cached_tokens_exposed"] is True
        assert "content" not in diagnostic
    before = (root/"budget.json").read_bytes()
    client.handler = lambda body:pytest.fail("mechanical probe replay resent")
    assert run_probes(root,client=client,token=authorization_token(plan,"probe"),authorize_paid=True) == record
    assert (root/"budget.json").read_bytes() == before
    assert len(client.calls) == 2


@pytest.mark.parametrize("bad", ["wrong_model","missing_usage","malformed_usage","total",
    "over64","finish","content"])
def test_K_capability_invalid_provider_envelope_never_publishes(setup, bad):
    root, plan = setup
    raw = response("OK.")
    if bad == "wrong_model": raw.model = "wrong-model"
    elif bad == "missing_usage": raw.usage = None
    elif bad == "malformed_usage": raw.usage.prompt_tokens = True
    elif bad == "total": raw.usage.total_tokens = 999
    elif bad == "over64": raw.usage.completion_tokens = 65; raw.usage.total_tokens = 165
    elif bad == "finish": raw.choices[0].finish_reason = "tool_calls"
    elif bad == "content": raw.choices[0].message.content = ["OK"]
    client = FakeClient(lambda body:raw)
    with pytest.raises(ExperimentalRequestError):
        run_probes(root,client=client,token=authorization_token(plan,"probe"),authorize_paid=True)
    assert len(client.calls) == 1
    assert not (root/"capability.json").exists()
    assert not (root/"probes/low.json").exists()
    assert read_json(root/"probes/medium.json")["status"] == "response_recorded"
    assert next(iter(read_json(root/"budget.json")["records"].values()))["status"] == "reserved"
    client.handler = lambda body:pytest.fail("invalid recorded probe resent")
    with pytest.raises(ExperimentalRequestError):
        run_probes(root,client=client,token=authorization_token(plan,"probe"),authorize_paid=True)
    assert len(client.calls) == 1


@pytest.mark.parametrize("bad", ["wrong_model","missing_usage","malformed_usage","total",
    "over64","finish","content","corrupt","identity","unsettled","response_sha","actual_ce",
    "started","uncertain","diagnostics"])
def test_K_capability_reconstruction_rejects_bad_evidence(setup, bad):
    root, plan = setup
    probe(root,plan)
    path = root/"probes/medium.json"
    journal = load_matching_journal(path,{})
    runtime = runtime_for(plan,None)
    if bad in {"unsettled","response_sha","actual_ce"}:
        with runtime.budget.locked():
            reserved = runtime.budget.document["records"][journal["operation_id"]]
            if bad == "unsettled": reserved["status"] = "reserved"
            elif bad == "response_sha": reserved["response_sha256"] = "a"*64
            else: reserved["actual_ce"] = "0"
            runtime.budget.save()
    elif bad == "corrupt": path.write_text("{}",encoding="utf-8")
    elif bad == "identity":
        atomic_write_json(path,_updated_record(journal,request_sha256="a"*64))
    elif bad in {"started","uncertain"}:
        atomic_write_json(path,_updated_record(journal,status=bad,response={},response_sha256=""))
    elif bad == "diagnostics":
        record=read_json(root/"capability.json")
        record["probe_evidence"][0]["finish_reason"]="length"
        atomic_write_json(root/"capability.json",record)
    else:
        raw=copy.deepcopy(journal["response"])
        if bad == "wrong_model": raw["model"]="wrong-model"
        elif bad == "missing_usage": raw["usage"]=None
        elif bad == "malformed_usage": raw["usage"]["prompt_tokens"]=True
        elif bad == "total": raw["usage"]["total_tokens"]=999
        elif bad == "over64": raw["usage"]["completion_tokens"]=65; raw["usage"]["total_tokens"]=165
        elif bad == "finish": raw["choices"][0]["finish_reason"]="tool_calls"
        elif bad == "content": raw["choices"][0]["content"]=["OK"]
        atomic_write_json(path,_updated_record(journal,response=raw,response_sha256=digest(raw)))
    before = {p:p.read_bytes() for p in (path,root/"budget.json",root/"capability.json")}
    with pytest.raises((ExperimentError,RuntimeError)):
        require_live_verification(plan,runtime)
    assert {p:p.read_bytes() for p in before} == before


@pytest.mark.parametrize("bad", ["unsettled","response_sha","actual_ce"])
def test_K_probe_publication_uses_same_settlement_validator(setup, monkeypatch, bad):
    root,plan=setup
    original=ExperimentalRuntime.request
    def request_then_damage(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        with self.budget.locked():
            reserve=self.budget.document["records"][result["operation_id"]]
            if bad == "unsettled": reserve["status"]="reserved"
            elif bad == "response_sha": reserve["response_sha256"]="a"*64
            else: reserve["actual_ce"]="0"
            self.budget.save()
        return result
    monkeypatch.setattr(ExperimentalRuntime,"request",request_then_damage)
    client=FakeClient()
    with pytest.raises(ExperimentError,match="settlement mismatch"):
        run_probes(root,client=client,token=authorization_token(plan,"probe"),authorize_paid=True)
    assert len(client.calls)==1
    assert not (root/"capability.json").exists()
    assert not (root/"probes/low.json").exists()


@pytest.mark.parametrize("failure", ["local","local_chained","raw_sdk","unknown","close"])
def test_K_activation_preserves_only_safe_local_errors_after_scope_closes(setup, monkeypatch, capsys, failure):
    from contextlib import contextmanager
    import traceback
    import command_code_provider_credentials as credentials
    import benchmark_longitudinal_provider as provider
    import experimental_goat
    root,plan=setup
    secret="fake-secret-never-expose-Authorization-plaintext"
    events=[]
    original_scope=credentials.command_code_api_key_scope
    # Only synthetic functions are used; no actual credential file or DPAPI backend.
    monkeypatch.setattr(credentials,"read_command_code_api_key",lambda *a,**k:secret)
    monkeypatch.setattr(credentials,"load_platform_dpapi",lambda:pytest.fail("real DPAPI seam entered"))
    @contextmanager
    def instrumented_scope():
        events.append("scope_enter")
        try:
            with original_scope(secret_path=root/"synthetic-unused.dpapi",dpapi=object()) as key:
                yield key
        except credentials.CommandCodeCredentialError:
            events.append("scope_masked")
            raise
        else:
            events.append("scope_normal_exit")
        finally:
            events.append("scope_closed")
    monkeypatch.setattr(credentials,"command_code_api_key_scope",instrumented_scope)
    client=FakeClient(lambda body:pytest.fail("activation test provider request"))
    def close():
        events.append("client_closed")
        if failure == "close": raise RuntimeError(secret)
    client.close=close
    def create(**kwargs):
        assert kwargs=={"api_key":secret}
        events.append("client_created")
        return client
    monkeypatch.setattr(provider,"create_provider_client",create)
    safe=failure in {"local","local_chained"}
    expected=ExperimentError if safe else credentials.CommandCodeCredentialError
    with pytest.raises(expected) as error:
        with experimental_goat.activate_client(plan,"probe",authorization_token(plan,"probe"),True) as active:
            assert active is client
            if failure == "local": raise ExperimentError("synthetic safe local failure")
            if failure == "local_chained":
                try: raise RuntimeError(secret)
                except RuntimeError as exc: raise ExperimentError("synthetic safe local failure") from exc
            if failure == "raw_sdk": raise RuntimeError(secret)
            if failure == "unknown": raise ValueError(secret)
    assert events==["scope_enter","client_created","client_closed",
                    "scope_normal_exit" if safe else "scope_masked","scope_closed"]
    assert str(error.value)==("synthetic safe local failure" if safe else "Command Code Provider credential/execution scope failed")
    displayed="".join(traceback.format_exception(error.value))
    captured=capsys.readouterr()
    assert secret not in displayed+captured.out+captured.err
    assert error.value.__cause__ is None
    assert not client.calls
    assert all(secret not in p.read_text(encoding="utf-8") for p in root.rglob("*.json"))


def test_K_changed_engine_inventory_blocks_old_plan_without_migration(setup, monkeypatch):
    import experimental_goat_experiment as experiment
    root,plan=setup
    probe(root,plan)
    before={p:p.read_bytes() for p in root.rglob("*.json")}
    original=experiment.code_inventory
    monkeypatch.setattr(experiment,"code_inventory",lambda engine:{**original(engine),"experimental_goat_experiment.py":"a"*64})
    with pytest.raises(ExperimentError,match="engine/profile input drift"):
        validate_root(root)
    assert {p:p.read_bytes() for p in before}==before


@pytest.mark.parametrize("exposed", [True,False])
def test_K_probe_cache_diagnostics_and_exact_example_cost(setup, exposed):
    root,plan=setup
    raw=response("\n\nOK.")
    raw.usage.prompt_tokens=14
    raw.usage.completion_tokens=24
    raw.usage.total_tokens=38
    raw.usage.prompt_tokens_details.cached_tokens=0
    if not exposed: del raw.usage.prompt_tokens_details
    client=FakeClient(lambda body:raw)
    record=run_probes(root,client=client,token=authorization_token(plan,"probe"),authorize_paid=True)
    assert require_live_verification(plan,runtime_for(plan,None))==record
    for entry in record["probe_evidence"]:
        assert entry["prompt_tokens"]==14 and entry["completion_tokens"]==24
        assert entry["cached_tokens_exposed"] is exposed
        assert entry["cache_tokens"]==(0 if exposed else None)
    assert all(r["actual_ce"]=="0.0000776" for r in read_json(root/"budget.json")["records"].values())
