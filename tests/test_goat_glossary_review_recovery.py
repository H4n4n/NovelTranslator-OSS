"""Zero-Provider, fake-DPAPI live-shaped chapter-review recovery proofs."""
import copy
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
import command_code_provider_transport as transport
import run_production_engine as launcher
from test_production_goat_runtime import engine, response, fake_credentials, zero_network, CLOCK
from production_goat_runtime import GoatRuntimeError
from production_goat_glossary_review_recovery import KIND, eligible_envelope, policy_sha256
from production_paid_request_journal import load_matching_journal, _updated_record
from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
from goat_glossary_review_recovery_plan import build_goat_glossary_review_recovery_plan as build_plan
from goat_glossary_review_recovery_execute import execute_goat_glossary_review_recovery as execute
from operator_preflight import build_operator_preflight

VALID = json.dumps({"decisions": [{"candidate_id": "c01", "action": "accept"}], "issues": []})
STATE = json.dumps({"new_terms": [{"chinese": "甲", "english": "Jia", "category": "character"}],
                   "continuity_summary": "Jia entered.", "issues": []}, ensure_ascii=False)


def empty_stop(**changes):
    raw = response(content="", finish="stop", completion=1286, reasoning=1286, **changes)
    raw.usage.prompt_tokens = 6864
    raw.usage.prompt_tokens_details.cached_tokens = 0
    raw.usage.total_tokens = 8150
    return raw


def configure(engine, answers):
    project, module, sends, client, budget, *_ = engine
    module.local_qa = lambda *a, **k: {"commit_allowed": True, "issues": []}
    def create(**body):
        sends.append(body)
        return answers.pop(0)
    client.chat.completions.create = create
    return project, module, sends, budget


def incident(engine):
    project, module, sends, budget = configure(engine, [
        response(content="A man entered the hall."), response(content=STATE), empty_stop()])
    budget.max_api_calls = 3
    assert module.process_chapter(1) is False
    assert len(sends) == 3
    meta = json.loads((project.pending_dir / "0001.json").read_text(encoding="utf-8"))
    assert meta[CHAPTER_REVIEW_RECOVERY_KEY]["status"] == "original_started"
    assert "review_result" not in meta and "state" in meta
    return project, module, sends, budget


def journal(project, kind=KIND):
    return project.data_dir / f"provider_request_journal/0001.{kind}.json"


def initial_path(project):
    paths = list((project.data_dir / "provider_request_journal").glob("0001.context_glossary_review_*.json"))
    assert len(paths) == 1
    return paths[0]


def plan(project, **kwargs):
    return build_plan("fixture", repo_root=project.repository_root, max_api_calls=kwargs.get("max_api_calls", 6),
                      max_credit_equivalent_usd=kwargs.get("max_credit_equivalent_usd", 1))


def review_again(engine, answer):
    project, module, sends, _, budget = engine[:5]
    configure(engine, [answer])
    budget.max_api_calls = 8
    budget.blocked = False
    return module.process_chapter(1)


@pytest.mark.parametrize("chapter,mandatory", [(396, False), (397, True)])
def test_recovery_planner_renders_exact_artifact_chapter_policy(engine, chapter, mandatory):
    from goat_glossary_review_recovery_plan import inspect_target, project_snapshot

    project, module, sends, client, budget, *_ = engine
    config = replace(project.config, final_chapter=397)
    project = replace(project, config=config)
    module.ACTIVE_NOVEL_CONFIG = config
    module.atomic_write_json(project.config_file, config.to_dict())
    rows = [{"chapter": n, "text": "甲 and 有熙氏 entered."} for n in range(1, 398)]
    module.atomic_write_json(project.chapters_file, rows)
    module.chapter_map[chapter] = rows[chapter - 1]
    module.progress.update(completed=list(range(1, chapter)), last_completed=chapter - 1)
    module.atomic_write_json(project.progress_file, module.progress)
    module.continuity.update(latest_chapter=chapter - 1)
    module.atomic_write_json(project.continuity_file, module.continuity)
    for number in module.progress["completed"]:
        (project.chapter_dir / f"{number:04d}.md").write_text(
            "Jia and the Youxi Clan entered.", encoding="utf-8")
    module.glossary = {"entries": {"有熙氏": {
        "english": "Youxi Clan", "category": "clan", "status": "locked",
        "strict_qa": True, "strict_qa_from_chapter": 397}}}
    module.atomic_write_json(project.glossary_file, module.glossary)
    # Runtime request construction owns the artifact argument, despite this
    # deliberately conflicting mutable in-memory progress.
    assert module.progress["next_chapter"] == 1
    configure(engine, [response(content="Jia and the Youxi Clan entered."),
                       response(content=STATE), empty_stop()])
    budget.max_api_calls = 3
    assert module.process_chapter(chapter) is False
    assert len(sends) == 3
    meta = json.loads((project.pending_dir / f"{chapter:04d}.json").read_text(encoding="utf-8"))
    assert meta["chapter"] == chapter
    assert meta[CHAPTER_REVIEW_RECOVERY_KEY]["status"] == "original_started"
    assert module.progress["next_chapter"] == 1
    # The planner requires the selected progress target to match its sole
    # pending pair; preserve that existing recovery gate, without consulting
    # the runtime module's conflicting mutable progress for policy rendering.
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress["next_chapter"] = chapter
    module.atomic_write_json(project.progress_file, progress)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only recovery planning must not send or resend")
    client.chat.completions.create = forbidden
    before = project_snapshot(project)
    first, second = inspect_target(project), inspect_target(project)
    assert first == second
    assert first["chapter"] == chapter
    assert first["wire"] == sends[2]  # Exact recorded runtime request identity.
    assert ("MANDATORY CANONICAL" in json.dumps(first["wire"])) is mandatory
    assert first["proof"]["initial_wire_sha256"] == first["proof"]["recovery_wire_sha256"]
    assert first["recovery_identity"]["chapter"] == chapter
    approval = plan(project)
    assert approval["target_chapter"] == chapter
    assert approval["api_calls"] == approval["credential_reads"] == approval["project_writes"] == 0
    assert approval["initial_wire_sha256"] == first["proof"]["initial_wire_sha256"]
    assert len(sends) == 3 and budget.api_calls == 3  # Fake fixture setup only.
    assert project_snapshot(project) == before  # Includes pending/accounting/journals.

    # Persisted progress disagreement is refused by the existing target gate; it
    # must never cause the artifact to be rendered under another chapter.
    progress["next_chapter"] = 1
    module.atomic_write_json(project.progress_file, progress)
    before = project_snapshot(project)
    with pytest.raises(RuntimeError, match="target is paused or completed"):
        inspect_target(project)
    assert project_snapshot(project) == before and len(sends) == 3


def test_live_shaped_recorded_replay_one_send_same_wire_accounting_commit(engine):
    project, module, sends, budget = incident(engine)
    initial = initial_path(project)
    before = initial.read_bytes()
    record = load_matching_journal(initial, {})
    wire = sends[2]
    assert eligible_envelope(record["response"], wire, original=True)
    assert record["response"]["stats"]["cost_usd"] == pytest.approx(0.0036024)
    assert review_again(engine, response(content=VALID, completion=100, reasoning=20)) is True
    assert len(sends) == 4 and sends[2] == sends[3]
    assert initial.read_bytes() == before
    recovery = load_matching_journal(journal(project), {})
    assert recovery["operation_id"] != record["operation_id"]
    assert recovery["wire_request_sha256"] == record["wire_request_sha256"]
    qa = json.loads((project.qa_dir / "0001.json").read_text(encoding="utf-8"))
    assert (project.chapter_dir / "0001.md").exists()
    assert module.progress["total_completion_tokens"] == 1406  # 10 translation + 10 state + both review attempts
    assert module.progress["rate_correct_cost_usd"] == pytest.approx(budget.actual_cost_usd)
    assert not (project.chapter_dir / "0002.md").exists()


@pytest.mark.parametrize("answer", [empty_stop(), response(content="partial", finish="length"),
    response(content=VALID, usage=False), response(content=VALID, model="wrong"),
    response(content=VALID, reasoning=11)])
def test_recovery_terminal_no_second_no_third_original(engine, answer):
    project, module, sends, budget = incident(engine)
    before = initial_path(project).read_bytes()
    assert review_again(engine, answer) is False
    assert review_again(engine, response(content=VALID)) is False
    assert len(sends) == 4
    assert initial_path(project).read_bytes() == before
    assert load_matching_journal(journal(project), {})["status"] == "response_recorded"
    assert not list(project.chapter_dir.glob("*.md"))
    assert project.pending_dir.joinpath("0001.md").exists()


@pytest.mark.parametrize("mutation", ["unexposed", "less", "length", "content", "effort", "ceiling", "model", "provider", "completion"])
def test_exact_predicate(engine, mutation):
    project, module, sends, _ = incident(engine)
    record = load_matching_journal(initial_path(project), {})
    envelope, wire = copy.deepcopy(record["response"]), copy.deepcopy(sends[2])
    if mutation == "unexposed": envelope["stats"]["reasoning_tokens_exposed"] = False
    if mutation == "less": envelope["stats"]["reasoning_tokens"] -= 1
    if mutation == "length": envelope["finish_reason"] = "length"
    if mutation == "content": envelope["content"] = VALID
    if mutation == "effort": wire["reasoning_effort"] = "medium"
    if mutation == "ceiling": wire["max_completion_tokens"] = 24000
    if mutation == "model": envelope["provider_response_model"] = "wrong"
    if mutation == "provider": envelope["provider_identity"]["provider"] = "direct_deepseek"
    if mutation == "completion": envelope["stats"]["completion_tokens"] = 0
    assert not eligible_envelope(envelope, wire, original=True)
    assert not eligible_envelope(record["response"], sends[2])


def test_nonempty_malformed_recovery_uses_existing_json_repair(engine):
    project, module, sends, budget = incident(engine)
    configure(engine, [response(content="nonempty malformed"), response(content=VALID)])
    budget.max_api_calls = 8; budget.blocked = False
    inputs = []
    original = module.repair_json_content
    def repair(label, content, *args, **kwargs):
        inputs.append(content)
        assert content.strip()
        return original(label, content, *args, **kwargs)
    module.repair_json_content = repair
    assert module.process_chapter(1) is True
    assert inputs == ["nonempty malformed"] and len(sends) == 5


def test_recovery_recorded_restart_local_domain_replay(engine):
    project, module, sends, budget = incident(engine)
    configure(engine, [response(content=VALID)])
    budget.max_api_calls = 8; budget.blocked = False
    saved = []
    original = module.atomic_write_json
    def save(path, value):
        if Path(path).resolve() == project.pending_dir / "0001.json" and value.get(CHAPTER_REVIEW_RECOVERY_KEY, {}).get("status") == "original_recorded":
            saved.append(copy.deepcopy(value))
            raise RuntimeError("crash before domain original result persists")
        original(path, value)
    module.atomic_write_json = save
    assert module.process_chapter(1) is False
    assert len(sends) == 4
    module.atomic_write_json = original
    initial_bytes, recovery_bytes = initial_path(project).read_bytes(), journal(project).read_bytes()
    assert module.process_chapter(1) is True
    assert len(sends) == 4
    assert initial_path(project).read_bytes() == initial_bytes and journal(project).read_bytes() == recovery_bytes
    domain = saved[0][CHAPTER_REVIEW_RECOVERY_KEY]
    assert domain["original"]["attempted"] is True
    proof = domain["reasoning_empty_transport_recovery"]
    assert proof["initial_accepted_empty_accounted"] and proof["recovery_authorized"]
    assert proof["effective_original"] == "recovered_nonempty_response"


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_ambiguous_recovery_no_send(engine, status):
    project, module, sends, budget = incident(engine)
    path = journal(project)
    module.atomic_write_json(path, _updated_record(load_matching_journal(path, {}), status=status))
    assert review_again(engine, response(content=VALID)) is False
    assert len(sends) == 3
    with pytest.raises(RuntimeError): plan(project)


def test_budget_refuses_before_send(engine):
    project, module, sends, budget = incident(engine)
    assert load_matching_journal(journal(project), {})["status"] == "planned"
    assert len(sends) == 3 and budget.pre_send_refusals
    with pytest.raises(RuntimeError): plan(project, max_credit_equivalent_usd=0.000001)


def test_planner_deterministic_read_only_and_operator_route(engine):
    project, *_ = incident(engine)
    from goat_glossary_review_recovery_plan import project_snapshot
    before = project_snapshot(project)
    first, second = plan(project), plan(project)
    assert first == second
    assert first["api_calls"] == first["credential_reads"] == first["project_writes"] == 0
    assert first["translation_generation_allowed"] is first["state_extraction_generation_allowed"] is False
    assert first["glossary_review_recovery_sends_remaining"] == 1
    assert first["initial_wire_sha256"] == first["recovery_wire_sha256"]
    assert project_snapshot(project) == before
    route = build_operator_preflight("fixture", repo_root=project.repository_root, model_selection="v41-flash",
        provider_selection="command_code_provider", max_api_calls=6, max_credit_equivalent_usd=1)
    assert route["status"] == "GOAT_GLOSSARY_REVIEW_RECOVERY_READY", route


@pytest.mark.parametrize("drift", ["source", "translation", "glossary", "checkpoint", "journal", "candidates", "policy", "runtime", "budget"])
def test_drift_before_dpapi(engine, monkeypatch, drift):
    project, module, *_ = incident(engine)
    approval = plan(project)
    if drift in ("policy", "runtime"):
        import goat_glossary_review_recovery_plan as planner
        if drift == "policy": monkeypatch.setattr(planner, "policy_sha256", lambda: "0" * 64)
        else: monkeypatch.setattr(planner, "_recovery_runtime_identity", lambda *a: {"sha256": "0" * 64})
    elif drift == "journal":
        value = load_matching_journal(initial_path(project), {})
        module.atomic_write_json(initial_path(project), _updated_record(value, request_started_utc="changed"))
    elif drift != "budget":
        path = project.chapters_file if drift == "source" else project.glossary_file if drift == "glossary" else project.pending_dir / ("0001.md" if drift == "translation" else "0001.json")
        if drift == "translation": path.write_text("changed", encoding="utf-8")
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            if drift == "source": value[0]["text"] += " changed"
            elif drift == "glossary": value["entries"]["乙"] = {"english": "Yi", "category": "character", "status": "locked"}
            elif drift == "candidates": value["state"]["new_terms"][0]["english"] = "Changed"
            else: value["state_checkpoint_identity"]["source_sha256"] = "0" * 64
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError):
        execute("fixture", repo_root=project.repository_root, max_api_calls=7 if drift == "budget" else 6,
            max_credit_equivalent_usd=1, expected_state_fingerprint=approval["state_fingerprint"],
            approval_token=approval["approval_token"], authorize_paid_recovery=True)


def test_dedicated_executor_fake_dpapi_one_send_commit(engine, monkeypatch, tmp_path):
    project, module, sends, _ = incident(engine)
    approval = plan(project)
    events = []; fake_credentials(monkeypatch, tmp_path, events)
    recovered_sends = []
    def create(**body):
        assert events[0] == "fake_dpapi"
        recovered_sends.append(body)
        assert body == sends[2]
        return response(content=VALID)
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: NS(
        chat=NS(completions=NS(create=create)), close=lambda: events.append("closed")))
    original_load = launcher.load_production_module
    def load(*a, **k):
        loaded = original_load(*a, **k)
        loaded.local_qa = lambda *a, **k: {"commit_allowed": True, "issues": []}
        return loaded
    monkeypatch.setattr(launcher, "load_production_module", load)
    report = execute("fixture", repo_root=project.repository_root, max_api_calls=6,
        max_credit_equivalent_usd=1, expected_state_fingerprint=approval["state_fingerprint"],
        approval_token=approval["approval_token"], authorize_paid_recovery=True)
    assert report["status"] == "COMMITTED", report
    assert len(recovered_sends) == 1 and report["api_calls"] == 1
    assert len(report["accepted_provider_responses"]) == 2
    assert report["accepted_provider_responses"][0]["replayed"] is True
    assert report["translation_generation_attempted"] is False


@pytest.mark.parametrize("operation", [
    "chapter_glossary_review_json_repair", "chapter_glossary_review_schema_correction",
    "chapter_glossary_review_semantic_correction", "chapter_glossary_review_semantic_correction_retry",
    "glossary_candidate_conflict_adjudication", "periodic_audit", "periodic_audit_json_repair",
    "strict_terminology_micro_repair", "state_extraction", "state_json_repair", "state_extraction_recovery",
])
def test_other_operations_empty_stop_never_recover(engine, operation):
    project, module, sends, budget = configure(engine, [empty_stop()])
    kwargs = dict(model=module.MODEL, messages=[{"role": "user", "content": "source"}],
                  **module.operation_request_controls(operation))
    for _ in range(2):
        with pytest.raises(GoatRuntimeError):
            module.journaled_chat_request(operation, 1, module.chapter_map[1]["text"], kwargs)
    assert len(sends) == 1 and not journal(project).exists()


def test_recovery_slot_without_original_authorization_refused(engine):
    project, module, sends, budget = configure(engine, [response(content=VALID)])
    with pytest.raises(GoatRuntimeError):
        module.journaled_chat_request(KIND, 1, module.chapter_map[1]["text"],
            dict(model=module.MODEL, messages=[{"role": "user", "content": "source"}], **module.operation_request_controls("chapter_glossary_review")))
    assert sends == [] and not journal(project).exists()


def test_fresh_inline_one_recovery_normal_domain(engine):
    project, module, sends, budget = configure(engine, [response(content="A man entered the hall."),
        response(content=STATE), empty_stop(), response(content=VALID)])
    assert module.process_chapter(1) is True
    assert len(sends) == 4 and sends[2] == sends[3]
    assert budget.actual_cost_usd == pytest.approx(module.progress["rate_correct_cost_usd"])


def test_nonempty_original_ordinary_no_recovery(engine):
    project, module, sends, budget = configure(engine, [response(content="A man entered the hall."),
        response(content=STATE), response(content=VALID)])
    assert module.process_chapter(1) is True
    assert len(sends) == 3 and not journal(project).exists()


@pytest.mark.parametrize("correction_class", ["schema", "semantic"])
def test_recovered_original_corrections_preserve_accounting(engine, correction_class):
    project, module, sends, budget = incident(engine)
    parsed = {"decisions": [{"candidate_id": "c01", "action": ""}], "issues": []}
    if correction_class == "schema": parsed = {"decisions": [{"candidate_id": "c01", "action": "accept"}], "issues": {"unresolved": ["A material issue"]}}
    answers = [response(content=json.dumps(parsed), completion=100, reasoning=20)]
    answers.append(response(content=VALID, completion=50, reasoning=10))
    configure(engine, answers); budget.max_api_calls = 8; budget.blocked = False
    assert module.process_chapter(1) is True
    assert module.progress["rate_correct_cost_usd"] == pytest.approx(budget.actual_cost_usd)
    assert module.progress["total_completion_tokens"] == 1456


def test_domain_started_without_provider_proof_remains_blocked(engine):
    project, module, sends, budget = incident(engine)
    # Retain the historical/blocked-head checkpoint shape. New attempts prove
    # bound-before-transport absence separately, without deleting paid journals.
    pending_path = project.pending_dir / "0001.json"
    meta = json.loads(pending_path.read_text(encoding="utf-8"))
    original = meta[CHAPTER_REVIEW_RECOVERY_KEY]["original"]
    original.pop("transport_ownership_version", None)
    original.pop("lifecycle", None)
    module.atomic_write_json(pending_path, meta)
    initial_path(project).unlink()  # disposable fixture only
    assert review_again(engine, response(content=VALID)) is False
    assert len(sends) == 3
    meta = json.loads((project.pending_dir / "0001.json").read_text(encoding="utf-8"))
    original = meta[CHAPTER_REVIEW_RECOVERY_KEY]["original"]
    assert {key: value for key, value in original.items() if key != "transport_binding"} == {
        "attempted": True, "operation_id": meta[CHAPTER_REVIEW_RECOVERY_KEY]["operation_ids"]["original"]}
    assert original["transport_binding"]["identity"]["auxiliary_domain_binding"]["operation_id"] == original["operation_id"]


def test_chapter2_after_pr146_translation_recovery(engine):
    project, module, sends, budget = configure(engine, [response(content="A man entered the hall."),
        response(content=json.dumps({"new_terms": [], "continuity_summary": "A man entered.", "issues": []})),
        response(content="", finish="length", completion=50000, reasoning=50000),
        response(content="Another man followed.", completion=2879, reasoning=361),
        response(content=STATE.replace("甲", "乙").replace("Jia", "Yi")), empty_stop()])
    assert module.process_chapter(1) is True
    budget.max_api_calls = 6
    assert module.process_chapter(2) is False
    meta = json.loads((project.pending_dir / "0002.json").read_text(encoding="utf-8"))
    assert meta["goat_reasoning_saturation_recovery_used"] is True
    assert meta["local_qa"]["commit_allowed"] is True and "state" in meta and "review_result" not in meta
    assert module.progress["completed"] == [1] and module.progress["next_chapter"] == 2
    approval = plan(project)
    assert approval["target_chapter"] == 2
    configure(engine, [response(content=VALID)]); budget.max_api_calls = 8; budget.blocked = False
    assert module.process_chapter(2) is True
    assert len(sends) == 7 and sends[5] == sends[6]
    assert module.progress["completed"] == [1, 2]
    assert not list((project.data_dir / "provider_request_journal").glob("0003.*"))


@pytest.mark.parametrize("mutation", ["missing_initial", "duplicate_initial", "diagnostics", "request"])
def test_recovery_accounting_chain_tamper_rejected(engine, mutation):
    project, module, sends, budget = incident(engine)
    configure(engine, [response(content=VALID, completion=100, reasoning=20)])
    budget.max_api_calls = 8; budget.blocked = False
    saved = []
    original = module.atomic_write_json
    def save(path, value):
        if Path(path).resolve() == project.pending_dir / "0001.json" and value.get(CHAPTER_REVIEW_RECOVERY_KEY, {}).get("status") == "original_recorded":
            saved.append(copy.deepcopy(value))
            raise RuntimeError("interrupt original result persistence")
        original(path, value)
    module.atomic_write_json = save
    assert module.process_chapter(1) is False
    domain = saved[0][CHAPTER_REVIEW_RECOVERY_KEY]
    stats = domain["original"]["stats"]
    if mutation == "missing_initial": stats["glossary_review_accepted_operations"].pop(0)
    if mutation == "duplicate_initial": stats["glossary_review_accepted_operations"][1] = copy.deepcopy(stats["glossary_review_accepted_operations"][0])
    if mutation == "diagnostics": stats["glossary_review_accepted_operations"][0]["stats"]["reasoning_tokens"] -= 1
    if mutation == "request": stats["glossary_review_accepted_operations"][0]["stats"]["request_sha256"] = "0" * 64
    from production_goat_glossary_review_recovery import validate_recovery_accounting
    with pytest.raises(RuntimeError): validate_recovery_accounting(stats, domain["reasoning_empty_transport_recovery"])


def test_planner_recorded_recovery_remaining_zero(engine):
    project, module, sends, budget = incident(engine)
    configure(engine, [response(content=VALID)])
    budget.max_api_calls = 8; budget.blocked = False
    original = module.atomic_write_json
    def save(path, value):
        if Path(path).resolve() == project.pending_dir / "0001.json" and value.get(CHAPTER_REVIEW_RECOVERY_KEY, {}).get("status") == "original_recorded":
            raise RuntimeError("interrupt result persistence")
        original(path, value)
    module.atomic_write_json = save
    assert module.process_chapter(1) is False
    module.atomic_write_json = original
    approval = plan(project)
    assert approval["glossary_review_recovery_sends_remaining"] == 0
    assert len(sends) == 4
