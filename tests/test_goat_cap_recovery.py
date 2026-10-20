"""Live-shaped zero-network saturation/replay/operator/lifecycle proofs."""
import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")
import command_code_provider_credentials as credentials
import command_code_provider_transport as transport
import run_production_engine as launcher
from test_production_goat_runtime import (
    engine, response, IDENTITY, GOAT, fake_credentials, CLOCK, zero_network,
)
from test_controlled_trial_plan import _empty_project
from production_goat_cap_recovery import (
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
    raw = response(content="", finish="length", completion=50000, reasoning=50000, **changes)
    raw.usage.prompt_tokens = 4400
    raw.usage.total_tokens = 54400
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
    if change == "under_ceiling": envelope["stats"]["completion_tokens"] = 49935
    if change == "over_ceiling": envelope["stats"]["completion_tokens"] = 50001
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
    assert stats["completion_tokens"] == 50100
    assert stats["reasoning_tokens"] == 50020 and stats["visible_completion_tokens"] == 80
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


@pytest.mark.parametrize("bad", ["empty_stop", "less", "model", "usage", "unknown"])
def test_rejected_initial_never_recovers(engine, bad):
    raw = response(content="A substantive partial chapter." if bad == "partial" else "",
        finish="stop" if bad == "empty_stop" else "unknown" if bad == "unknown" else "length",
        model="wrong" if bad == "model" else None, usage=bad != "usage", completion=50000,
        reasoning=None if bad == "unexposed" else 49999 if bad == "less" else 50000)
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
        if body.get("max_completion_tokens") == 50000: return previous(**body)
        sends.append(body)
        return response(content='{"repairs": []}')
    client.chat.completions.create = create
    assert module.process_chapter(1) is False
    assert sum(body.get("max_completion_tokens") == 50000 for body in sends) == (2 if recovery_used else 1)
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
    assert first["recovery_controls"] == dict(model=IDENTITY["wire_model"], reasoning_effort="low", max_completion_tokens=50000)
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
    assert "recorded_cap_failure" in human and "credit-equivalent USD" in human


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
    assert len(sends) == 3 and "max_completion_tokens" not in sends[-1]
    assert journal(project).read_bytes() == initial_bytes and journal(project, KIND).read_bytes() == recovery_bytes
    assert module.progress["total_completion_tokens"] == 50200
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
    assert [item.get("max_completion_tokens") for item in sends] == [50000, 50000, None]
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["completed"] == [1] and progress["total_completion_tokens"] == 50200
    assert progress["estimated_cost_usd"] == pytest.approx(budget.actual_cost_usd)
    qa_meta = json.loads((project.qa_dir / "0001.json").read_text(encoding="utf-8"))
    assert qa_meta["goat_reasoning_saturation_recovery_used"] is True
    assert qa_meta["initial_completion_tokens"] == 50000
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
                if body.get("max_completion_tokens") == 50000 else state_response())
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
    assert sum(body.get("max_completion_tokens") == 50000 for body in physical) == (0 if recovered_already else 1)
    assert journal(project).read_bytes() == before
    assert events == ["fake_dpapi"]
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["total_completion_tokens"] == 50200
    assert len(report["accepted_provider_responses"]) == 3
    assert progress["estimated_cost_usd"] == pytest.approx(sum(item["stats"]["cost_usd"]
        for item in report["accepted_provider_responses"]))


@pytest.mark.parametrize("content,reasoning,shape", [
    ("A substantive initial partial chapter ends abruptly.", 49000, "visible_truncated"),
    ("", None, "blank_all_reasoning"),
])
def test_both_shapes_same_body_no_partial_in_messages(engine, content, reasoning, shape):
    project, module, sends, budget = configure(engine, [
        response(content=content, finish="length", completion=50000, reasoning=reasoning),
        response(content="The complete replacement chapter ends here.")])
    stage = module.translate_chapter(module.chapter_map[1])
    assert len(sends) == 2 and sends[0] == sends[1]
    assert content == "" or content not in json.dumps(sends[1]["messages"])
    assert stage.content == "The complete replacement chapter ends here."
    assert stage.pending_provenance()["cap_failure_shape"] == shape
    assert stage.pending_provenance()["translation_max_tokens"] == 50000
    assert stage.pending_provenance()["translation_recovery_max_tokens"] == 50000
    assert stage.pending_provenance()["translation_recovery_policy_revision"] == REVISION
    again = module.translate_chapter(module.chapter_map[1])
    assert again.stats == stage.stats and len(sends) == 2


@pytest.mark.parametrize("completion,recovers", [(49935, False), (49936, True), (50000, True), (50001, False)])
def test_visible_length_exact_near_ceiling_boundaries(engine, completion, recovers):
    _, module, sends, _ = configure(engine, [response(content="A fluent partial translation is still incomplete.",
        finish="length", completion=completion, reasoning=completion-100), response()])
    if recovers:
        assert module.translate_chapter(module.chapter_map[1]).recovery_used
    else:
        with pytest.raises(GoatRuntimeError): module.translate_chapter(module.chapter_map[1])
    assert len(sends) == (2 if recovers else 1)


@pytest.mark.parametrize("cap_recovered", [False, True])
@pytest.mark.parametrize("repair", ["clean", "success", "residual", "malformed", "qa"])
def test_residual_composition_single_send_full_qa_and_accounting(engine, monkeypatch, cap_recovered, repair):
    from dataclasses import replace
    from novel_project import ResidualCJKMicroRepairConfig
    project, module, sends, client, budget, *_ = engine
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG,
        residual_cjk_micro_repair=ResidualCJKMicroRepairConfig(enabled=True, max_total_cjk_chars=12,
            max_fragments=3, max_affected_lines=3, max_attempts_per_pending_version=1))
    text = "A man entered the hall." if repair == "clean" else "A man entered the hall carrying a sealed letter and wearing a dark coat with 甲 on his badge."
    patch = {"repairs": [{"old_text": "with 甲 on his badge", "new_text":
        "with Jia on his badge" if repair in ("success", "qa") else "with 乙 on his badge"}]}
    answers = ([saturation()] if cap_recovered else []) + [response(content=text)]
    if repair != "clean":
        answers.append(response(content="invalid JSON" if repair == "malformed" else json.dumps(patch)))
    if repair in ("clean", "success"):
        answers.append(state_response())
    configure(engine, answers)
    qa_inputs = []
    original = module.local_qa
    def qa(chapter, source, candidate, finish):
        qa_inputs.append(candidate)
        result = original(chapter, source, candidate, finish)
        if repair == "qa" and "Jia" in candidate:
            result["commit_allowed"] = False
        return result
    monkeypatch.setattr(module, "local_qa", qa)
    committed = module.process_chapter(1)
    assert committed == (repair in ("clean", "success"))
    translation_sends = [body for body in sends if body.get("max_completion_tokens") == 50000]
    assert len(translation_sends) == (2 if cap_recovered else 1)
    assert len(sends) == (len(translation_sends) + (1 if repair != "clean" else 0) + (1 if committed else 0))
    assert qa_inputs
    if repair == "success":
        assert sum("Jia" in candidate for candidate in qa_inputs) >= 2
        initial = load_matching_journal(journal(project), {})
        if cap_recovered:
            recovered = load_matching_journal(journal(project, KIND), {})
            assert initial["operation_id"] != recovered["operation_id"]
            assert initial["wire_request_sha256"] == recovered["wire_request_sha256"]
        residuals = list((project.data_dir / "provider_request_journal").glob("*residual*"))
        assert len(residuals) == 1
        residual_record = load_matching_journal(residuals[0], {})
        assert residual_record["operation_kind"] == "residual_cjk_micro_repair"
        progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
        assert progress["estimated_cost_usd"] == pytest.approx(budget.actual_cost_usd)
        assert len(progress["accounted_api_operation_ids"]) == 1
    if not committed:
        before = len(sends)
        assert module.process_chapter(1) is False
        assert len(sends) == before
        assert (project.pending_dir / "0001.md").read_text(encoding="utf-8") == text
        assert module.progress["completed"] == []


def test_frozen_v1_identity_and_24k_provenance_validate_unchanged(engine):
    import production_goat_saturation_recovery as v1
    from production_provider_runtime import historical_goat_provider_identity, pending_provider_compatibility
    from production_goat_runtime import raw_response_record
    from production_translation_recovery import TranslationStageResult, aggregate_translation_stats, validate_recovery_provenance
    from production_accounting import combine_goat_stats
    wire = transport.build_command_code_translation_request(messages=[{"role": "user", "content": "Original source"}])
    wire["max_completion_tokens"] = 24000
    def operation(kind, raw, binding=""):
        ident = v1.journal_identity(kind=kind, novel_id="fixture", chapter=1, profile_id="historical_military",
            source_sha256="a"*64, wire=wire, binding=binding)
        envelope = v1.accepted_envelope({"raw_response": raw_response_record(raw), "request_started_utc": CLOCK().isoformat()}, wire)
        return {**ident, "status": "response_recorded", "response": envelope,
            "response_sha256": v1.response_sha256(envelope)}
    initial = operation("chapter_translation", response(content="", finish="length", completion=24000, reasoning=24000))
    assert v1.eligible_envelope(initial["response"], wire)
    binding = v1.recovery_binding(initial, wire)
    recovered = operation(v1.KIND, response(), binding)
    stats = aggregate_translation_stats([{"operation_id": r["operation_id"], "stats": r["response"]["stats"]}
        for r in (initial, recovered)], engine[1].combine_stats)
    result = TranslationStageResult(content=recovered["response"]["content"], stats=stats, elapsed_seconds=0,
        finish_reason="stop", initial=initial, recovery=recovered, policy_revision=v1.REVISION)
    meta = dict(translation_stats=stats, translation_model="deepseek-flash", finish_reason="stop", **result.pending_provenance())
    validate_recovery_provenance(meta)
    v1.prove_pending_prefix(meta, initial, recovered, text=result.content)
    assert pending_provider_compatibility(meta, IDENTITY)["provider_compatible"]
    assert meta["translation_provider_identity"] == historical_goat_provider_identity()
    assert v1.policy_identity()["max_completion_tokens"] == 24000
    assert v1.REVISION == "goat-reasoning-saturation-recovery-v1"
    assert v1.policy_sha256() == "17a59c838993201495ca02c5758229abefdc48e327aa48c701265917092b3e63"
    assert not eligible_envelope(initial["response"], wire)


@pytest.mark.parametrize("count,eligible", [(12, True), (13, False)])
def test_exact_residual_total_character_boundary(count, eligible):
    from public_test_support import enabled_config
    from residual_cjk_micro_repair import assess_micro_repair_eligibility
    fragment = "甲" * count
    prose = "The lengthy English narration continues through the entire scene with careful detail and clear meaning. " * 3
    result = assess_micro_repair_eligibility(fragment, prose + fragment, enabled_config())
    assert result["eligible"] == eligible
    assert result["remaining_cjk_character_count"] == count


def test_legacy_recovery_does_not_authorize_direct_provider(engine):
    from production_provider_runtime import provider_identity, pending_provider_compatibility, historical_goat_provider_identity
    meta = {"translation_recovery_policy_revision": "goat-reasoning-saturation-recovery-v1",
            "translation_provider_identity": historical_goat_provider_identity(),
            "translation_provider": GOAT}
    with pytest.raises(ValueError):
        pending_provider_compatibility(meta, provider_identity("direct_deepseek", "v41-flash"))
