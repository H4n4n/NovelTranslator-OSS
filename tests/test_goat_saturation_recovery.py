"""Live-shaped zero-network saturation/replay/operator/lifecycle proofs."""
import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import command_code_provider_credentials as credentials
import command_code_provider_transport as transport
import run_production_engine as launcher
from test_production_goat_runtime import (
    engine, response, IDENTITY, GOAT, fake_credentials, CLOCK, zero_network,
)
from test_controlled_trial_plan import _empty_project
from production_goat_saturation_recovery import (
    KIND, eligible_envelope, prove_initial, recovery_binding, policy_sha256, REVISION,
)
from production_goat_runtime import GoatRuntimeError
from production_paid_request_journal import _updated_record, load_matching_journal
from goat_saturation_recovery_plan import build_goat_saturation_recovery_plan, inspect_target
from goat_saturation_recovery_execute import execute_goat_saturation_recovery
from controlled_trial_plan import build_controlled_trial_plan, ControlledTrialPlanError
from controlled_trial_execute import execute_controlled_trial
from operator_preflight import build_operator_preflight
from production_translation_request import build_translation_request
from production_accounting import validate_goat_stats
from production_translation_recovery_prefix import load_recorded_recovery_prefix


def saturation(**changes):
    raw = response(content="", finish="length", completion=24000, reasoning=24000, **changes)
    raw.usage.prompt_tokens = 4400
    raw.usage.total_tokens = 28400
    return raw


def journal(project, kind="chapter_translation"):
    return project.data_dir / f"provider_request_journal/0001.{kind}.json"


def payload(module):
    return build_translation_request(chapter=module.chapter_map[1], config=module.ACTIVE_NOVEL_CONFIG,
        profile=module.ACTIVE_PROFILE, system_prompt=module.system_prompt,
        glossary_entries=module.glossary["entries"], continuity=module.continuity, model=module.MODEL,
        controls=module.operation_request_controls("chapter_translation"))


def record_initial(engine):
    project, module, sends, client, budget, *_ = engine
    client.chat.completions.create = lambda **body: (sends.append(body), saturation())[1]
    result = module.journaled_chat_request("chapter_translation", 1, module.chapter_map[1]["text"], payload(module))
    assert budget.api_calls == 1 and not budget.blocked and not budget.unaccounted_provider_response
    return result


def configure(engine, answers):
    project, module, sends, client, budget, *_ = engine
    def create(**body):
        sends.append(body)
        return answers.pop(0)
    client.chat.completions.create = create
    return project, module, sends, budget


def test_exact_saturation_classifier_and_no_pending(engine):
    result = record_initial(engine)
    project, module, sends, *_ = engine
    assert eligible_envelope(result["response"], sends[0])
    record = load_matching_journal(journal(project), {})
    assert prove_initial(record, sends[0]) == record
    assert not list(project.pending_dir.iterdir())
    assert module.progress["next_chapter"] == 1


@pytest.mark.parametrize("change", ["stop", "unknown", "unexposed", "less", "wrong_model", "wrong_provider", "usage", "partial", "under_ceiling", "over_ceiling", "kind", "effort", "ceiling", "wire_model"])
def test_exact_predicate_fails_closed(engine, change):
    result = record_initial(engine)
    _, _, sends, *_ = engine
    envelope, wire = copy.deepcopy(result["response"]), copy.deepcopy(sends[0])
    kind = "chapter_translation"
    if change in ("stop", "unknown"): envelope["finish_reason"] = change
    if change == "unexposed": envelope["stats"]["reasoning_tokens_exposed"] = False
    if change == "less": envelope["stats"]["reasoning_tokens"] -= 1
    if change == "wrong_model": envelope["provider_response_model"] = "wrong"
    if change == "wrong_provider": envelope["provider_identity"]["provider"] = "deepseek"
    if change == "usage": envelope.pop("stats")
    if change == "partial": envelope["content"] = "A substantive partial translation ends here."
    if change == "under_ceiling": envelope["stats"]["completion_tokens"] = 23935
    if change == "over_ceiling": envelope["stats"]["completion_tokens"] = 24001
    if change == "kind": kind = KIND
    if change == "effort": wire["reasoning_effort"] = "medium"
    if change == "ceiling": wire["max_completion_tokens"] = 32000
    if change == "wire_model": wire["model"] = "wrong"
    assert not eligible_envelope(envelope, wire, kind=kind)


def test_two_sends_same_body_distinct_immutable_slots_accounting(engine):
    project, module, sends, budget = configure(engine, [saturation(), response(content="A man entered the hall.", completion=100, reasoning=20)])
    initial_bytes = []
    original = module.journaled_chat_request
    def request(kind, *args, **kwargs):
        if kind == KIND: initial_bytes.append(journal(project).read_bytes())
        return original(kind, *args, **kwargs)
    module.journaled_chat_request = request
    stage = module.translate_chapter(module.chapter_map[1])
    assert stage.recovery_used and not stage.failure
    assert len(sends) == 2 and sends[0] == sends[1]
    assert initial_bytes == [journal(project).read_bytes()]
    initial = load_matching_journal(journal(project), {})
    recovered = load_matching_journal(journal(project, KIND), {})
    assert initial["operation_id"] != recovered["operation_id"]
    assert initial["wire_request_sha256"] == recovered["wire_request_sha256"]
    stats = stage.stats
    assert stats["completion_tokens"] == 24100
    assert stats["reasoning_tokens"] == 24020 and stats["visible_completion_tokens"] == 80
    assert stats["cost_usd"] == pytest.approx(sum(item["stats"]["cost_usd"] for item in stats["translation_accepted_operations"]))
    assert stats["cost_usd"] == budget.actual_cost_usd
    validate_goat_stats(stats)
    meta = stage.pending_provenance()
    assert meta["goat_reasoning_saturation_recovery_used"] and meta["translation_recovery_policy_revision"] == REVISION


@pytest.mark.parametrize("mutation", ["omit_initial", "duplicate_initial", "request_sha", "diagnostics"])
def test_saved_accounting_chain_rejects_omission_duplication_or_drift(engine, mutation):
    _, module, _, _ = configure(engine, [saturation(), response()])
    stage = module.translate_chapter(module.chapter_map[1])
    from translation_model_config import pending_translation_model_record
    from production_translation_recovery import validate_recovery_provenance
    meta = dict(translation_stats=copy.deepcopy(stage.stats), **pending_translation_model_record(module.MODEL))
    meta.update(stage.pending_provenance())
    if mutation in ("omit_initial", "duplicate_initial"):
        from production_accounting import combine_goat_stats
        components = meta["translation_stats"]["translation_accepted_operations"]
        selected = [components[1]["stats"]] if mutation == "omit_initial" else [components[0]["stats"], components[0]["stats"], components[1]["stats"]]
        altered = module.combine_stats(*selected)
        altered["translation_accepted_operations"] = components
        meta["translation_stats"] = altered
    if mutation == "request_sha": meta["recovery_request_sha256"] = "0" * 64
    if mutation == "diagnostics": meta["initial_reasoning_tokens"] -= 1
    with pytest.raises(RuntimeError): validate_recovery_provenance(meta)


@pytest.mark.parametrize("bad", ["saturation", "length", "empty", "model", "usage", "unknown", "reasoning_invalid"])
def test_second_response_terminal_no_third_send(engine, bad):
    second = saturation() if bad == "saturation" else response(
        content="" if bad == "empty" else "A man entered.",
        finish="length" if bad == "length" else "unknown" if bad == "unknown" else "stop",
        model="wrong" if bad == "model" else None, usage=bad != "usage",
        reasoning=11 if bad == "reasoning_invalid" else None)
    project, module, sends, _ = configure(engine, [saturation(), second])
    for _ in range(2):
        with pytest.raises(GoatRuntimeError): module.translate_chapter(module.chapter_map[1])
    assert len(sends) == 2
    assert load_matching_journal(journal(project, KIND), {})["status"] == "response_recorded"
    assert not list(project.pending_dir.iterdir()) and not list(project.chapter_dir.iterdir())
    assert module.progress["completed"] == []


def test_restart_initial_replay_one_recovery_send(engine):
    record_initial(engine)
    project, module, sends, client, budget, *_ = engine
    before = journal(project).read_bytes()
    configure(engine, [response(content="A man entered the hall.")])
    stage = module.translate_chapter(module.chapter_map[1])
    assert stage.initial["replayed"] and not stage.recovery["replayed"]
    assert len(sends) == 2 and journal(project).read_bytes() == before
    stage2 = module.translate_chapter(module.chapter_map[1])
    assert stage2.initial["replayed"] and stage2.recovery["replayed"] and len(sends) == 2
    assert stage.stats == stage2.stats


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_recovery_ambiguous_no_resend(engine, status):
    project, module, sends, _ = configure(engine, [saturation(), response()])
    module.translate_chapter(module.chapter_map[1])
    path = journal(project, KIND)
    record = load_matching_journal(path, {})
    module.atomic_write_json(path, _updated_record(record, status=status, response={}, response_sha256=""))
    with pytest.raises(RuntimeError): module.translate_chapter(module.chapter_map[1])
    with pytest.raises(RuntimeError): plan(project)
    assert len(sends) == 2


@pytest.mark.parametrize("limit", ["calls", "credits"])
def test_budget_refusal_planned_not_uncertain(engine, limit):
    project, module, sends, budget = configure(engine, [saturation()])
    if limit == "calls": budget.max_api_calls = 1
    else:
        original = module.journaled_chat_request
        def limited(kind, *args, **kwargs):
            result = original(kind, *args, **kwargs)
            if kind == "chapter_translation": budget.max_cost_usd = budget.actual_cost_usd + 0.000001
            return result
        module.journaled_chat_request = limited
    with pytest.raises(GoatRuntimeError): module.translate_chapter(module.chapter_map[1])
    assert len(sends) == 1 and budget.api_calls == 1
    assert load_matching_journal(journal(project, KIND), {})["status"] == "planned"
    assert not budget.unaccounted_provider_response
    assert budget.pre_send_refusals and not list(project.pending_dir.iterdir())


@pytest.mark.parametrize("bad", ["empty_stop", "unexposed", "less", "model", "usage", "partial", "unknown"])
def test_rejected_initial_never_recovers(engine, bad):
    raw = response(content="A substantive partial chapter." if bad == "partial" else "",
        finish="stop" if bad == "empty_stop" else "unknown" if bad == "unknown" else "length",
        model="wrong" if bad == "model" else None, usage=bad != "usage", completion=24000,
        reasoning=None if bad == "unexposed" else 23999 if bad == "less" else 24000)
    project, module, sends, _ = configure(engine, [raw])
    with pytest.raises(GoatRuntimeError): module.translate_chapter(module.chapter_map[1])
    assert len(sends) == 1 and not journal(project, KIND).exists()


def test_success_one_send(engine):
    _, module, sends, _ = configure(engine, [response()])
    stage = module.translate_chapter(module.chapter_map[1])
    assert not stage.recovery_used and len(sends) == 1


@pytest.mark.parametrize("recovery_used", [False, True])
def test_qa_failure_preserves_pending_no_extra_translation(engine, recovery_used):
    answers = ([saturation()] if recovery_used else []) + [response(content="A man entered the hall. 甲")]
    project, module, sends, _ = configure(engine, answers)
    client = engine[3]
    previous = client.chat.completions.create
    def create(**body):
        if body["max_completion_tokens"] == 24000: return previous(**body)
        sends.append(body)
        return response(content='{"repairs": []}')
    client.chat.completions.create = create
    assert module.process_chapter(1) is False
    assert sum(body["max_completion_tokens"] == 24000 for body in sends) == (2 if recovery_used else 1)
    assert (project.pending_dir / "0001.md").exists()
    assert module.progress["completed"] == []
    assert journal(project, KIND).exists() == recovery_used


def plan(project, calls=6, credits=1):
    return build_goat_saturation_recovery_plan("fixture", repo_root=project.repository_root,
        max_api_calls=calls, max_credit_equivalent_usd=credits)


def test_planner_readonly_stable_and_fresh_trial_routes(engine, capsys):
    record_initial(engine)
    project, _, sends, *_ = engine
    before = {p.relative_to(project.root).as_posix(): p.read_bytes() for p in project.root.rglob("*") if p.is_file()}
    first, second = plan(project), plan(project)
    assert first == second and first["api_calls"] == first["credential_reads"] == first["project_writes"] == 0
    assert len(sends) == 1
    assert before == {p.relative_to(project.root).as_posix(): p.read_bytes() for p in project.root.rglob("*") if p.is_file()}
    assert first["recovery_controls"] == dict(model=IDENTITY["wire_model"], reasoning_effort="low", max_completion_tokens=24000)
    with pytest.raises(ControlledTrialPlanError, match="goat_saturation_recovery_plan"):
        build_controlled_trial_plan("fixture", repo_root=project.repository_root, model_selection="v41-flash",
            provider_selection=GOAT, max_credit_equivalent_usd=1)
    operator = build_operator_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT, max_credit_equivalent_usd=1)
    assert operator["status"] == "GOAT_SATURATION_RECOVERY_READY"
    assert operator["recommended_entrypoint"] == "goat_saturation_recovery_plan.py"
    from operator_preflight import _print_human
    _print_human(operator)
    human = capsys.readouterr().out
    assert "recorded_reasoning_saturation" in human and "credit-equivalent USD" in human


@pytest.mark.parametrize("drift", ["source", "journal", "response", "policy", "runtime", "calls", "credits", "glossary", "continuity"])
def test_authorization_drift_before_credentials(engine, monkeypatch, drift):
    record_initial(engine)
    project, module, sends, *_ = engine
    approved = plan(project)
    calls, credits = 6, 1
    if drift == "source": project.chapters_file.write_text("[]", encoding="utf-8")
    if drift == "journal": journal(project).write_text("{}", encoding="utf-8")
    if drift == "response":
        record = load_matching_journal(journal(project), {})
        changed = copy.deepcopy(record["response"]); changed["raw_response"]["id"] = "drift"
        from production_translation_recovery import response_sha256
        module.atomic_write_json(journal(project), _updated_record(record, response=changed, response_sha256=response_sha256(changed)))
    if drift == "policy": monkeypatch.setattr("goat_saturation_recovery_plan.policy_sha256", lambda: "0"*64)
    if drift == "runtime": monkeypatch.setattr("goat_saturation_recovery_plan._engine_runtime_identity", lambda *a: {"sha256":"0"*64})
    if drift == "calls": calls = 7
    if drift == "credits": credits = 2
    if drift == "glossary": project.glossary_file.write_text('{"entries":{},"changed":true}', encoding="utf-8")
    if drift == "continuity": project.continuity_file.write_text('{"summary":"changed"}', encoding="utf-8")
    with pytest.raises(Exception):
        execute_goat_saturation_recovery("fixture", repo_root=project.repository_root,
            max_api_calls=calls, max_credit_equivalent_usd=credits,
            expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"],
            authorize_paid_recovery=True)
    assert len(sends) == 1


def test_pending_prefix_readonly_chain(engine):
    project, module, _, _ = configure(engine, [saturation(), response()])
    stage = module.translate_chapter(module.chapter_map[1])
    from translation_model_config import pending_translation_model_record
    meta = dict(chapter=1, stage="translation_recovery_recorded", finish_reason="stop",
        translation_stats=stage.stats, **pending_translation_model_record(module.MODEL))
    meta.update(stage.pending_provenance())
    path = project.pending_dir / "0001.json"
    module.atomic_write_json(path, meta)
    initial, recovered = load_recorded_recovery_prefix(path, meta)
    assert initial["operation_id"] != recovered["operation_id"]
    assert launcher.prepare_production_runtime("v41-flash", project.pending_dir,
        provider_selection=GOAT)["runtime"]["effective_model"] == module.MODEL


@pytest.mark.parametrize("phase", ["metadata_before", "metadata_after", "markdown_before", "markdown_after"])
def test_pending_crash_replay_keeps_first_cost_once(engine, monkeypatch, phase):
    project, module, sends, budget = configure(engine, [saturation(), response(content="A man entered the hall.",
        completion=100, reasoning=20), state_response()])
    text_writer, json_writer = module.atomic_write_text, module.save_json
    md_path, meta_path = project.pending_dir / "0001.md", project.pending_dir / "0001.json"
    def write_text(path, value, *args, **kwargs):
        if Path(path).resolve() == md_path.resolve() and phase == "markdown_before": raise KeyboardInterrupt
        text_writer(path, value, *args, **kwargs)
        if Path(path).resolve() == md_path.resolve() and phase == "markdown_after": raise KeyboardInterrupt
    def write_json(path, value):
        if Path(path).resolve() == meta_path.resolve() and phase == "metadata_before": raise KeyboardInterrupt
        json_writer(path, value)
        if Path(path).resolve() == meta_path.resolve() and phase == "metadata_after": raise KeyboardInterrupt
    monkeypatch.setattr(module, "atomic_write_text", write_text)
    monkeypatch.setattr(module, "save_json", write_json)
    with pytest.raises(KeyboardInterrupt): module.process_chapter(1)
    assert len(sends) == 2 and module.progress["completed"] == []
    initial_bytes = journal(project).read_bytes()
    recovery_bytes = journal(project, KIND).read_bytes()
    monkeypatch.setattr(module, "atomic_write_text", text_writer)
    monkeypatch.setattr(module, "save_json", json_writer)
    assert module.process_chapter(1) is True
    assert len(sends) == 3 and sends[-1]["max_completion_tokens"] == 8000
    assert journal(project).read_bytes() == initial_bytes and journal(project, KIND).read_bytes() == recovery_bytes
    assert module.progress["total_completion_tokens"] == 24200
    assert module.progress["estimated_cost_usd"] == pytest.approx(budget.actual_cost_usd)


def test_plan_budget_refuses_before_credentials(engine):
    record_initial(engine)
    project, _, sends, *_ = engine
    with pytest.raises(GoatRuntimeError): plan(project, credits=0.000001)
    assert len(sends) == 1 and not journal(project, KIND).exists()


def test_executor_without_optin_is_readonly(engine):
    record_initial(engine)
    project, _, sends, *_ = engine
    approved = plan(project)
    report = execute_goat_saturation_recovery("fixture", repo_root=project.repository_root,
        max_api_calls=approved["max_api_calls"], max_credit_equivalent_usd=approved["max_credit_equivalent_usd"],
        expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"])
    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["api_calls"] == report["credential_reads"] == report["project_writes"] == 0
    assert len(sends) == 1


def state_response():
    return response(content=json.dumps({"new_terms": [], "continuity_summary": "A man entered.", "issues": []}),
                    completion=100, reasoning=20)


def test_inline_recovery_normal_lifecycle_commit_both_costs(engine, monkeypatch):
    project, module, sends, budget = configure(engine, [saturation(), response(content="A man entered the hall.",
        completion=100, reasoning=20), state_response()])
    events = []
    original = module.local_qa
    def qa(*args, **kwargs):
        events.append("qa")
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "local_qa", qa)
    assert module.process_chapter(1) is True
    assert events and len(sends) == 3
    assert [item["max_completion_tokens"] for item in sends] == [24000, 24000, 8000]
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["completed"] == [1] and progress["total_completion_tokens"] == 24200
    assert progress["estimated_cost_usd"] == pytest.approx(budget.actual_cost_usd)
    qa_meta = json.loads((project.qa_dir / "0001.json").read_text(encoding="utf-8"))
    assert qa_meta["goat_reasoning_saturation_recovery_used"] is True
    assert qa_meta["initial_completion_tokens"] == 24000
    assert qa_meta["recovery_completion_tokens"] == 100
    assert project.continuity_file.exists() and project.glossary_file.exists()


@pytest.mark.parametrize("recovered_already", [False, True])
def test_dedicated_executor_fresh_process_replay_fake_dpapi(engine, monkeypatch, tmp_path, recovered_already):
    record_initial(engine)
    project, module, sends, *_ = engine
    if recovered_already:
        configure(engine, [response(content="A man entered the hall.", completion=100, reasoning=20)])
        module.translate_chapter(module.chapter_map[1])
    before = journal(project).read_bytes()
    approved = plan(project)
    events, physical = [], []
    fake_credentials(monkeypatch, tmp_path, events)
    def create(**body):
        physical.append(body)
        return (response(content="A man entered the hall.", completion=100, reasoning=20)
                if body["max_completion_tokens"] == 24000 else state_response())
    def factory(**options):
        assert options["max_retries"] == 0 and events == ["fake_dpapi"]
        return NS(chat=NS(completions=NS(create=create)), close=lambda: None)
    monkeypatch.setattr(transport, "create_command_code_client", lambda **k: transport.create_provider_client(
        api_key=k["api_key"], base_url=IDENTITY["base_url"], client_factory=factory))
    report = execute_goat_saturation_recovery("fixture", repo_root=project.repository_root,
        max_api_calls=approved["max_api_calls"], max_credit_equivalent_usd=approved["max_credit_equivalent_usd"],
        expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"],
        authorize_paid_recovery=True)
    assert report["status"] == "COMMITTED", report
    assert sum(body["max_completion_tokens"] == 24000 for body in physical) == (0 if recovered_already else 1)
    assert journal(project).read_bytes() == before
    assert events == ["fake_dpapi"]
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["total_completion_tokens"] == 24200
    assert len(report["accepted_provider_responses"]) == 3
    assert progress["estimated_cost_usd"] == pytest.approx(sum(item["stats"]["cost_usd"]
        for item in report["accepted_provider_responses"]))
