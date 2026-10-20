import json
from dataclasses import replace
from datetime import datetime, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import controlled_trial_execute as executor
import controlled_trial_plan as planner

from controlled_trial_execute import (
    ControlledTrialBudget,
    ControlledTrialBudgetError,
    ControlledTrialExecutionError,
    execute_controlled_trial,
    validate_execution_authorization,
)
from controlled_trial_plan import build_controlled_trial_plan
from novel_project import NovelConfig, NovelProject
from production_operation_policy import policy_for_model
from production_paid_request_journal import run_journaled_paid_request, request_identity, PaidRequestUncertainError
from translation_model_config import resolve_translation_model


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_project(tmp_path):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=2,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    _write_json(project.chapters_file, [
        {"chapter": 1, "text": "甲 entered."},
        {"chapter": 2, "text": "乙 followed."},
    ])
    _write_json(project.glossary_file, {"entries": {}})
    _write_json(project.progress_file, {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
    })
    _write_json(project.continuity_file, {"latest_chapter": 0, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    return project


class _FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response, error))


def _response(prompt=1000, completion=500, hits=0, misses=1000):
    return SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_cache_hit_tokens=hits,
        prompt_cache_miss_tokens=misses,
    ))


def test_exact_plan_validates_but_does_not_authorize_paid_work(tmp_path):
    _empty_project(tmp_path)
    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )
    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_trial=False,
    )
    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["paid_execution_authorized"] is False
    assert report["api_calls"] == 0
    assert report["actual_cost_usd"] == 0


def test_wrong_fingerprint_or_token_fails_before_execution(tmp_path):
    _empty_project(tmp_path)
    plan = build_controlled_trial_plan(
        "fixture", repo_root=tmp_path, model_selection="pro"
    )
    with pytest.raises(ControlledTrialExecutionError, match="state fingerprint"):
        validate_execution_authorization(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=6,
            max_cost_usd=0.10,
            expected_state_fingerprint="0" * 64,
            approval_token=plan["approval_token"],
        )
    with pytest.raises(ControlledTrialExecutionError, match="approval token"):
        validate_execution_authorization(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=6,
            max_cost_usd=0.10,
            expected_state_fingerprint=plan["state_fingerprint"],
            approval_token="1" * 64,
        )


def test_budget_counts_success_and_exact_usage_cost():
    budget = ControlledTrialBudget("pro", 2, 1.0)
    client = _FakeClient(_response())
    response = budget.create(
        client,
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "translate this"}],
        max_tokens=12000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    assert response is client.chat.completions.response
    assert budget.api_calls == 1
    assert client.chat.completions.calls == 1
    assert budget.actual_cost_usd > 0
    assert budget.records[0]["status"] == "success"
    assert budget.records[0]["actual_cost_usd"] == budget.actual_cost_usd


def test_budget_blocks_call_count_before_provider_request():
    budget = ControlledTrialBudget("pro", 1, 1.0)
    client = _FakeClient(_response(prompt=1, completion=1, misses=1))
    kwargs = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 12000,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    budget.create(client, **kwargs)
    with pytest.raises(ControlledTrialBudgetError, match="API-call ceiling"):
        budget.create(client, **kwargs)
    assert client.chat.completions.calls == 1
    assert budget.api_calls == 1


def test_budget_blocks_unsafe_cost_before_provider_request(monkeypatch):
    # Monday 02:12 UTC is explicitly peak; refusal must not use the real clock.
    pricing_moment = datetime(2026, 10, 5, 2, 12, tzinfo=timezone.utc)
    real_pricing_info = executor._pricing_info
    monkeypatch.setattr(
        executor, "_pricing_info",
        lambda config, _moment: real_pricing_info(config, pricing_moment),
    )
    budget = ControlledTrialBudget("pro", 6, 0.000001)
    client = _FakeClient(_response())
    with pytest.raises(ControlledTrialBudgetError, match="cost ceiling"):
        budget.create(
            client,
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "large enough"}],
            max_tokens=12000,
            extra_body={"thinking": {"type": "disabled"}},
        )
    assert client.chat.completions.calls == 0
    assert budget.api_calls == 0
    assert budget.records == []
    assert budget.actual_cost_usd == 0
    assert budget.pre_send_refusals[0]["kind"] == "deterministic_local_budget_refusal"


def test_provider_error_still_consumes_attempt_but_not_observed_cost():
    budget = ControlledTrialBudget("pro", 2, 1.0)
    client = _FakeClient(error=RuntimeError("network fail"))
    with pytest.raises(RuntimeError, match="network fail"):
        budget.create(
            client,
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=12000,
            extra_body={"thinking": {"type": "disabled"}},
        )
    assert budget.api_calls == 1
    assert budget.actual_cost_usd == 0
    assert budget.records[0]["status"] == "provider_error"

def test_budgeted_openai_factory_routes_create_and_delegates_access():
    real_client = _FakeClient(
        _response(
            prompt=1,
            completion=1,
            misses=1,
        )
    )
    real_client.marker = "client"
    real_client.chat.marker = "chat"
    real_client.chat.completions.marker = "completions"

    budget = ControlledTrialBudget("pro", 2, 1.0)

    factory = executor._budgeted_openai_factory(
        lambda *_args, **_kwargs: real_client,
        budget,
    )
    wrapped = factory(
        api_key="synthetic",
        base_url="https://api.deepseek.com",
    )

    assert wrapped.marker == "client"
    assert wrapped.chat.marker == "chat"
    assert (
        wrapped.chat.completions.marker
        == "completions"
    )

    response = wrapped.chat.completions.create(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "user",
                "content": "x",
            }
        ],
        max_tokens=12000,
        extra_body={"thinking": {"type": "disabled"}},
    )

    assert response is real_client.chat.completions.response
    assert real_client.chat.completions.calls == 1
    assert budget.api_calls == 1


def _callable_production_module(
    project,
    events,
    *,
    provider_during_initialization=False,
):
    raw_client = _FakeClient(
        _response(
            prompt=1,
            completion=1,
            misses=1,
        )
    )

    module = SimpleNamespace()
    module.client = None
    module.progress = None
    module.AUTO_OFF_PEAK_ONLY = True
    module.OpenAI = (
        lambda *_args, **_kwargs: raw_client
    )

    def initialize():
        events.append("initialize")

        module.client = module.OpenAI(
            api_key="synthetic-test-key",
            base_url="https://api.deepseek.com",
        )
        module.progress = {
            "next_chapter": 1,
        }

        if provider_during_initialization:
            module.client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=[
                    {
                        "role": "user",
                        "content": "initialization probe",
                    }
                ],
                max_tokens=12000,
                extra_body={"thinking": {"type": "disabled"}},
            )

    def run_unattended_production(*, target_chapter=None):
        events.append(
            (
                "run",
                target_chapter,
                module.AUTO_OFF_PEAK_ONLY,
            )
        )

        assert isinstance(
            module.client,
            executor._BudgetedClientProxy,
        )
        assert target_chapter == 1

        (project.chapter_dir / "0001.md").write_text(
            "translated",
            encoding="utf-8",
        )
        _write_json(
            project.progress_file,
            {
                "completed": [1],
                "last_completed": 1,
                "next_chapter": 2,
            },
        )

    def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError(
            "production main() must not be dispatched"
        )

    module.initialize_production_runtime_state = initialize
    module.run_unattended_production = (
        run_unattended_production
    )
    module.main = forbidden_dispatch

    return module, raw_client


def _prepare_callable_execution(
    monkeypatch,
    project,
    module,
):
    monkeypatch.setattr(
        executor,
        "prepare_production_runtime",
        lambda *_args, **_kwargs: {
            "runtime": {
                "effective_model": "deepseek-v4-pro",
            }
        },
    )
    monkeypatch.setattr(
        executor,
        "load_production_module",
        lambda _path: module,
    )


def test_executor_uses_callable_production_boundary_for_exact_target(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )

    events = []
    module, raw_client = _callable_production_module(
        project,
        events,
    )
    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "COMMITTED"
    assert report["api_calls"] == 0
    assert raw_client.chat.completions.calls == 0
    assert events == [
        "initialize",
        ("run", 1, True),
    ]


def test_provider_call_during_initialization_is_budgeted(
    tmp_path,
    monkeypatch,
):
    # Monday 12:00 UTC is explicitly off-peak, so the unchanged $0.05 fixture
    # tests initialization budgeting rather than the current pricing window.
    pricing_moment = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    real_pricing_info = executor._pricing_info
    monkeypatch.setattr(
        executor, "_pricing_info",
        lambda config, _moment: real_pricing_info(config, pricing_moment),
    )
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )

    events = []
    module, raw_client = _callable_production_module(
        project,
        events,
        provider_during_initialization=True,
    )
    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "COMMITTED"
    assert report["api_calls"] == 1
    assert raw_client.chat.completions.calls == 1
    assert (
        report["provider_calls"][0]["status"]
        == "success"
    )
    assert report["paid_execution_authorized"] is True
    call = report["provider_calls"][0]
    assert call["pricing_period"] == "off_peak"
    assert 0 < call["conservative_cost_bound_usd"] <= 0.05
    assert 0 < call["actual_cost_usd"] <= call["conservative_cost_bound_usd"]
    assert report["actual_cost_usd"] == call["actual_cost_usd"]


def test_executor_applies_peak_override_to_loaded_module(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        allow_peak=True,
    )

    events = []
    module, _raw_client = _callable_production_module(
        project,
        events,
    )
    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )
    monkeypatch.setattr(
        executor,
        "_CONTROLLED_TRIAL_ALLOW_PEAK",
        True,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "COMMITTED"
    assert events == [
        "initialize",
        ("run", 1, False),
    ]


def test_executor_restores_previous_production_module_registration(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )

    events = []
    module, _raw_client = _callable_production_module(
        project,
        events,
    )

    monkeypatch.setattr(
        executor,
        "prepare_production_runtime",
        lambda *_args, **_kwargs: {
            "runtime": {
                "effective_model": "deepseek-v4-pro",
            }
        },
    )

    sentinel = object()
    missing = object()
    previous = sys.modules.get(
        executor.PRODUCTION_MODULE_NAME,
        missing,
    )
    sys.modules[
        executor.PRODUCTION_MODULE_NAME
    ] = sentinel

    def load_module(_path):
        sys.modules[
            executor.PRODUCTION_MODULE_NAME
        ] = module
        return module

    monkeypatch.setattr(
        executor,
        "load_production_module",
        load_module,
    )

    try:
        report = execute_controlled_trial(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=3,
            max_cost_usd=0.05,
            expected_state_fingerprint=plan[
                "state_fingerprint"
            ],
            approval_token=plan["approval_token"],
            authorize_paid_trial=True,
        )

        assert report["status"] == "COMMITTED"
        assert (
            sys.modules[
                executor.PRODUCTION_MODULE_NAME
            ]
            is sentinel
        )
    finally:
        if previous is missing:
            sys.modules.pop(
                executor.PRODUCTION_MODULE_NAME,
                None,
            )
        else:
            sys.modules[
                executor.PRODUCTION_MODULE_NAME
            ] = previous


def test_system_exit_during_initialization_is_contained(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )

    events = []
    module, raw_client = _callable_production_module(
        project,
        events,
    )

    def exiting_initializer():
        events.append("initialize")
        raise SystemExit(7)

    module.initialize_production_runtime_state = (
        exiting_initializer
    )

    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert report["api_calls"] == 0
    assert raw_client.chat.completions.calls == 0
    assert events == ["initialize"]
    assert "status 7" in report["error"]


def test_keyboard_interrupt_during_initialization_is_contained(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
    )

    events = []
    module, raw_client = _callable_production_module(
        project,
        events,
    )

    def interrupted_initializer():
        events.append("initialize")
        raise KeyboardInterrupt

    module.initialize_production_runtime_state = (
        interrupted_initializer
    )

    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert report["api_calls"] == 0
    assert raw_client.chat.completions.calls == 0
    assert events == ["initialize"]
    assert (
        report["error"]
        == "operator interrupted the controlled trial"
    )


def test_controlled_trial_executor_contains_no_ast_execution_path():
    source = Path(executor.__file__).read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "import ast",
        "_ControlledProductionTransformer",
        "compile_controlled_production_script",
        "ast.NodeTransformer",
        "ast.parse(",
        "exec(code",
    ):
        assert forbidden not in source

def test_normal_approval_cannot_be_reused_for_peak_execution(
    tmp_path,
    monkeypatch,
):
    _empty_project(tmp_path)

    normal = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        allow_peak=False,
    )

    monkeypatch.setattr(
        executor,
        "_CONTROLLED_TRIAL_ALLOW_PEAK",
        True,
    )

    with pytest.raises(
        ControlledTrialExecutionError,
        match="state fingerprint",
    ):
        execute_controlled_trial(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=3,
            max_cost_usd=0.05,
            expected_state_fingerprint=normal[
                "state_fingerprint"
            ],
            approval_token=normal["approval_token"],
            authorize_paid_trial=True,
        )


def test_peak_approval_cannot_be_reused_for_normal_execution(
    tmp_path,
):
    _empty_project(tmp_path)

    peak = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        allow_peak=True,
    )

    with pytest.raises(
        ControlledTrialExecutionError,
        match="state fingerprint",
    ):
        execute_controlled_trial(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=3,
            max_cost_usd=0.05,
            expected_state_fingerprint=peak[
                "state_fingerprint"
            ],
            approval_token=peak["approval_token"],
            authorize_paid_trial=True,
        )


def test_nonpeak_trial_fails_closed_if_scheduler_default_is_disabled(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        allow_peak=False,
    )

    events = []

    module, raw_client = _callable_production_module(
        project,
        events,
    )

    module.AUTO_OFF_PEAK_ONLY = False

    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert report["api_calls"] == 0
    assert raw_client.chat.completions.calls == 0
    assert events == []

    assert (
        "AUTO_OFF_PEAK_ONLY = True"
        in report["error"]
    )


def test_peak_trial_reports_explicit_scheduler_mode(
    tmp_path,
    monkeypatch,
):
    project = _empty_project(tmp_path)

    plan = build_controlled_trial_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        allow_peak=True,
    )

    events = []
    module, _raw_client = _callable_production_module(
        project,
        events,
    )

    _prepare_callable_execution(
        monkeypatch,
        project,
        module,
    )

    monkeypatch.setattr(
        executor,
        "_CONTROLLED_TRIAL_ALLOW_PEAK",
        True,
    )

    report = execute_controlled_trial(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=3,
        max_cost_usd=0.05,
        expected_state_fingerprint=plan[
            "state_fingerprint"
        ],
        approval_token=plan["approval_token"],
        authorize_paid_trial=True,
    )

    assert report["status"] == "COMMITTED"
    assert report["scheduler_mode"] == "peak_allowed"
    assert report["allow_peak"] is True


# Request controls come from the authoritative catalog, never a duplicated roster.
def _request(selection="v41-flash", operation="chapter_translation"):
    policy = policy_for_model(selection)
    return {"model": policy.model_id, "messages": [{"role": "user", "content": "fixture"}],
            **policy.for_operation(operation).request_controls()}


@pytest.mark.parametrize("selection", ["v41-flash", "pro"])
def test_budget_accepts_all_and_only_catalog_controls(selection):
    policy = policy_for_model(selection)
    budget = ControlledTrialBudget(selection, len(policy.operations()), 1.0, operation_policy=policy)
    client = _FakeClient(_response())
    for operation in policy.operations():
        budget.create(client, **_request(selection, operation.operation_id))
    assert budget.api_calls == client.chat.completions.calls == len(policy.operations())
    assert budget.operation_policy == policy
    assert policy.translation.max_tokens == resolve_translation_model(selection).max_tokens == 12000
    assert (policy.translation_recovery is None) == (selection == "pro")


@pytest.mark.parametrize("changes", [
    {"max_tokens": 24001}, {"max_tokens": 16000}, {"max_tokens": 23999},
    {"max_tokens": "24000"}, {"max_tokens": 24000.0}, {"max_tokens": 24000.9},
    {"max_tokens": True}, {"max_tokens": None}, {"max_tokens": 0},
    {"extra_body": {"thinking": {"type": "enabled"}}},
    {"extra_body": None}, {"extra_body": {}},
    {"reasoning_effort": None}, {"reasoning_effort": "low"},
    {"reasoning_effort": "high"},
    {"extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}},
    {"extra_body": {"thinking": {"type": "disabled"}, "max_tokens": 24001}},
    {"max_completion_tokens": 24001}, {"thinking": {"type": "enabled"}},
    {"model": "deepseek-v4-pro"},
])
def test_unreviewed_controls_fail_before_send(changes):
    budget = ControlledTrialBudget("v41-flash", 2, 1.0)
    client = _FakeClient(_response())
    kwargs = {**_request(operation="chapter_translation_recovery"), **changes}
    with pytest.raises(ControlledTrialBudgetError):
        budget.create(client, **kwargs)
    assert client.chat.completions.calls == budget.api_calls == 0
    assert budget.actual_cost_usd == 0 and not budget.records


def test_pro_cannot_use_flash_recovery_controls():
    budget = ControlledTrialBudget("pro", 2, 1.0)
    client = _FakeClient(_response())
    kwargs = {**_request(operation="chapter_translation_recovery"), "model": "deepseek-v4-pro"}
    with pytest.raises(ControlledTrialBudgetError, match="operation policy"):
        budget.create(client, **kwargs)
    assert client.chat.completions.calls == budget.api_calls == 0


@pytest.mark.parametrize("selection", ["unknown", "glm-5", "qwen"])
def test_new_trial_budget_rejects_unknown_models(selection):
    with pytest.raises(ValueError):
        ControlledTrialBudget(selection, 2, 1.0)


@pytest.mark.parametrize("selection", ["flash", "deepseek-v4-flash"])
def test_retired_model_cannot_send_but_local_only_recovery_can_hold_empty_budget(selection):
    budget = ControlledTrialBudget(selection, 2, 1.0)
    client = _FakeClient(_response())
    with pytest.raises(ControlledTrialBudgetError, match="retired model"):
        budget.create(client, **_request(selection))
    assert budget.api_calls == client.chat.completions.calls == 0


@pytest.mark.parametrize("selection", ["v41-flash", "pro"])
def test_budget_cannot_be_supplied_a_forged_operation_policy(selection):
    policy = policy_for_model(selection)
    forged = replace(policy, translation=replace(policy.translation, max_tokens=16000))
    with pytest.raises(ControlledTrialBudgetError, match="selected production model policy"):
        ControlledTrialBudget(selection, 2, 1.0, operation_policy=forged)
    with pytest.raises(ControlledTrialBudgetError, match="selected production model policy"):
        ControlledTrialBudget(selection, 2, 1.0, operation_policy=policy_for_model("flash"))


def test_recovery_conservative_bound_prices_full_24000_and_remaining_budget(monkeypatch):
    monkeypatch.setattr(executor.core, "is_deepseek_peak_time", lambda moment: False)
    config = resolve_translation_model("v41-flash")
    pricing = executor._pricing_info(config, datetime(2026, 10, 3, tzinfo=timezone.utc))
    kwargs = _request(operation="chapter_translation_recovery")
    expected = ((executor._serialized_request_bytes(kwargs) + executor.PROMPT_TOKEN_SAFETY_OVERHEAD)
                * config.prices["off_peak"]["cache_miss"] + 24000 * config.prices["off_peak"]["output"]) / 1_000_000
    budget = ControlledTrialBudget(config, 2, 1.0)
    assert budget._worst_case_cost(kwargs, pricing) == pytest.approx(expected)
    initial_usage = _response(prompt=100, completion=12000, misses=100)
    initial_cost = (100 * .15 + 12000 * .60) / 1_000_000
    # Enough for initial request, but only half the recovery bound remains.
    budget = ControlledTrialBudget(config, 2, initial_cost + expected / 2)
    client = _FakeClient(initial_usage)
    budget.create(client, **_request())
    with pytest.raises(ControlledTrialBudgetError, match="cost ceiling"):
        budget.create(client, **kwargs)
    assert budget.api_calls == client.chat.completions.calls == 1
    assert budget.actual_cost_usd == pytest.approx(initial_cost)


def test_recovery_cannot_escape_api_call_limit():
    budget = ControlledTrialBudget("v41-flash", 1, 1.0)
    client = _FakeClient(_response())
    budget.create(client, **_request())
    with pytest.raises(ControlledTrialBudgetError, match="API-call ceiling"):
        budget.create(client, **_request(operation="chapter_translation_recovery"))
    assert budget.api_calls == client.chat.completions.calls == 1


def test_actual_usage_exceeding_budget_stops_after_response(monkeypatch):
    monkeypatch.setattr(executor.core, "is_deepseek_peak_time", lambda moment: False)
    budget = ControlledTrialBudget("v41-flash", 2, .1)
    client = _FakeClient(_response(prompt=1_000_000, completion=24000, misses=1_000_000))
    with pytest.raises(ControlledTrialBudgetError, match="usage exceeded"):
        budget.create(client, **_request(operation="chapter_translation_recovery"))
    assert budget.actual_cost_usd == pytest.approx(.1644)
    assert budget.records[0]["status"] == "cost_ceiling_exceeded_after_response"
    with pytest.raises(ControlledTrialBudgetError, match="cost ceiling"):
        budget.create(client, **_request())
    assert budget.api_calls == client.chat.completions.calls == 1


@pytest.mark.parametrize("max_calls,max_cost,expected_calls", [(2, 1.0, 2), (1, 1.0, 1), (2, .02, 1)])
def test_approved_trial_real_translation_recovery_obeys_envelopes(tmp_path, monkeypatch, max_calls, max_cost, expected_calls):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine
    from provider_model_identity_runtime import install_provider_model_identity_runtime
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("network forbidden"))
    project = _empty_project(tmp_path)
    monkeypatch.setattr(executor.core, "is_deepseek_peak_time", lambda moment: False)
    config = resolve_translation_model("v41-flash")
    pricing = executor._pricing_info(config, datetime(2026, 10, 3, tzinfo=timezone.utc))
    chapter = {"chapter": 1, "title": "Fixture", "text": "Authoritative fixture source"}
    partial = "PARTIAL The knight crossed the valley and saw the fortress."
    final = "FINAL The whole fresh translation ends with every event resolved."
    calls, results, initial_durable_bytes = [], [], []
    def observe(event, **fields):
        if event == "translation_saturation_detected":
            initial_durable_bytes.append((project.data_dir / "provider_request_journal" / "0001.chapter_translation.json").read_bytes())
    def send(**kwargs):
        calls.append(kwargs)
        index = len(calls)
        assert index <= 2
        response = _response(prompt=100, completion=12000 if index == 1 else 19045, misses=100)
        response.model = "deepseek-flash"
        response.id = f"fake-{index}"
        response.choices = [SimpleNamespace(message=SimpleNamespace(content=partial if index == 1 else final),
                                           finish_reason="length" if index == 1 else "stop")]
        return response
    raw_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))
    for name, value in {
        "MODEL": config.model, "PRODUCTION_TRANSLATION_MAX_TOKENS": config.max_tokens,
        "PRODUCTION_TRANSLATION_THINKING": config.thinking,
        "ACTIVE_NOVEL_CONFIG": project.config, "ACTIVE_PROFILE": SimpleNamespace(id=project.config.profile),
        "PAID_REQUEST_JOURNAL_DIR": project.data_dir / "provider_request_journal",
        "system_prompt": "Fixture system", "continuity": {"summary": "Fixture continuity"},
        "glossary": {"entries": {}}, "RUN_LOGGER": None, "AUTO_OFF_PEAK_ONLY": True,
        "relevant_glossary": lambda text: {},
        "glossary_to_text": lambda entries, *, chapter_number: (
            "Fixture glossary" if chapter_number == chapter["chapter"] else
            pytest.fail("translation prompt must use the operation chapter")),
        "wait_for_api_permission": lambda: None, "log_event": observe,
        "pricing_snapshot": lambda: pricing, "OpenAI": lambda *a, **kw: raw_client,
        "client": None, "progress": None,
    }.items():
        monkeypatch.setattr(engine, name, value)
    monkeypatch.setattr(engine, "send_api_request_once", engine.send_api_request_once)
    monkeypatch.setattr(engine, "run_journaled_paid_request", engine.run_journaled_paid_request)
    monkeypatch.setattr(engine, "_noveltranslator_provider_model_identity_runtime_state", None, raising=False)
    install_provider_model_identity_runtime(engine)
    def initialize_without_credentials():
        engine.client = engine.OpenAI()
        engine.progress = {"next_chapter": 1}
    def bounded_runner(*, target_chapter):
        assert target_chapter == 1
        result = engine.translate_chapter(chapter)
        results.append(result)
        assert result.content == final and result.recovery_used
        # Isolate the translation stage; no downstream state/review requests.
        (project.chapter_dir / "0001.md").write_text(result.content, encoding="utf-8")
        _write_json(project.progress_file, {"completed": [1], "last_completed": 1, "next_chapter": 2})
    monkeypatch.setattr(engine, "initialize_production_runtime_state", initialize_without_credentials)
    monkeypatch.setattr(engine, "run_unattended_production", bounded_runner)
    monkeypatch.setattr(executor, "load_production_module", lambda path: engine)
    monkeypatch.setattr(executor, "prepare_production_runtime", lambda *a, **kw: {"runtime": {"effective_model": config.model}})
    plan = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash",
                                     max_api_calls=max_calls, max_cost_usd=max_cost)
    report = execute_controlled_trial("fixture", repo_root=tmp_path, model_selection="v41-flash",
        max_api_calls=max_calls, max_cost_usd=max_cost, expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"], authorize_paid_trial=True)
    assert report["api_calls"] == len(calls) == expected_calls
    assert calls[0] == {**_request(), "messages": calls[0]["messages"]}
    initial = json.loads((engine.PAID_REQUEST_JOURNAL_DIR / "0001.chapter_translation.json").read_text())
    assert initial["status"] == "response_recorded"
    assert initial["response"]["finish_reason"] == "length"
    assert (engine.PAID_REQUEST_JOURNAL_DIR / "0001.chapter_translation.json").read_bytes() == initial_durable_bytes[0]
    expected_actual = (200 * .15 + (12000 + 19045) * .60) / 1_000_000 if expected_calls == 2 else (100 * .15 + 12000 * .60) / 1_000_000
    assert report["actual_cost_usd"] == pytest.approx(expected_actual)
    assert sum(r["actual_cost_usd"] for r in report["provider_calls"]) == pytest.approx(expected_actual)
    if expected_calls == 2:
        assert report["status"] == "COMMITTED", report["error"]
        assert calls[1] == {**calls[0], "max_tokens": 24000}
        assert partial not in json.dumps(calls[1])
        assert results[0].stats["completion_tokens"] == 31045
        assert results[0].stats["cost_usd"] == pytest.approx(expected_actual)
        assert results[0].stats["prompt_tokens"] == 200
        assert all(r["conservative_cost_bound_usd"] >= r["actual_cost_usd"] for r in report["provider_calls"])
        before = [p.read_bytes() for p in sorted(engine.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))]
        replay = engine.translate_chapter(chapter)
        assert replay.initial["replayed"] and replay.recovery["replayed"]
        assert replay.stats == results[0].stats and len(calls) == 2
        assert before == [p.read_bytes() for p in sorted(engine.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))]
    else:
        assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
        assert "ceiling" in report["error"]
        assert not results and not (project.chapter_dir / "0001.md").exists()
        recovery_record = json.loads((engine.PAID_REQUEST_JOURNAL_DIR / "0001.chapter_translation_recovery.json").read_text())
        assert recovery_record["status"] == "planned"
        assert recovery_record["response"] == {} and recovery_record["attempt_context"] == {}
        assert report["pre_send_refusals"][0]["kind"] == "deterministic_local_budget_refusal"
        initial_path = engine.PAID_REQUEST_JOURNAL_DIR / "0001.chapter_translation.json"
        initial_bytes = initial_path.read_bytes()
        # A fresh trial authorization/transport instance admits the exact planned
        # recovery. The already accepted initial spend belongs to Trial 1.
        monkeypatch.setattr(engine, "OpenAI", lambda *a, **kw: raw_client)
        new_plan = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash",
                                               max_api_calls=2, max_cost_usd=1.0)
        assert new_plan["approval_token"] != plan["approval_token"]
        retried = execute_controlled_trial("fixture", repo_root=tmp_path, model_selection="v41-flash",
            max_api_calls=2, max_cost_usd=1.0, expected_state_fingerprint=new_plan["state_fingerprint"],
            approval_token=new_plan["approval_token"], authorize_paid_trial=True)
        assert retried["status"] == "COMMITTED", retried["error"]
        assert retried["api_calls"] == 1 and len(calls) == 2
        assert calls[1] == {**calls[0], "max_tokens": 24000}
        assert results[0].initial["replayed"] and results[0].recovery_used
        assert not results[0].recovery["replayed"]
        assert initial_path.read_bytes() == initial_bytes
        recovered = json.loads((engine.PAID_REQUEST_JOURNAL_DIR / "0001.chapter_translation_recovery.json").read_text())
        assert recovered["status"] == "response_recorded"
        assert recovered["operation_id"] == recovery_record["operation_id"]
        assert results[0].stats["completion_tokens"] == 31045


def test_policy_file_change_invalidates_trial_approval_before_loading(tmp_path, monkeypatch):
    _empty_project(tmp_path)
    root = tmp_path / "engine_snapshot"
    root.mkdir()
    profile = root / "profiles" / "historical_military"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text("{}", encoding="utf-8")
    original_path = Path(executor.__file__).with_name("production_operation_policy.py")
    policy_path = root / original_path.name
    policy_path.write_bytes(original_path.read_bytes())
    monkeypatch.setattr(planner, "ENGINE_RUNTIME_ROOT", root)
    old = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash")
    text = policy_path.read_text(encoding="utf-8")
    assert '"chapter_translation_recovery", 24000' in text
    policy_path.write_text(text.replace('"chapter_translation_recovery", 24000', '"chapter_translation_recovery", 25000'), encoding="utf-8")
    new = build_controlled_trial_plan("fixture", repo_root=tmp_path, model_selection="v41-flash")
    assert old["engine_runtime_identity"] != new["engine_runtime_identity"]
    assert old["state_fingerprint"] != new["state_fingerprint"]
    assert old["approval_token"] != new["approval_token"]
    monkeypatch.setattr(executor, "load_production_module", lambda *a: pytest.fail("must reject before engine load"))
    with pytest.raises(ControlledTrialExecutionError, match="state fingerprint"):
        execute_controlled_trial("fixture", repo_root=tmp_path, model_selection="v41-flash", max_api_calls=6,
            max_cost_usd=.10, expected_state_fingerprint=old["state_fingerprint"],
            approval_token=old["approval_token"], authorize_paid_trial=True)


@pytest.mark.parametrize("missing", ["max_tokens", "extra_body"])
def test_missing_explicit_controls_are_not_provider_defaults(missing):
    budget = ControlledTrialBudget("v41-flash", 2, 1.0)
    client = _FakeClient(_response())
    kwargs = _request(operation="chapter_translation_recovery")
    del kwargs[missing]
    with pytest.raises(ControlledTrialBudgetError):
        budget.create(client, **kwargs)
    assert budget.api_calls == client.chat.completions.calls == 0


def test_trial_imports_have_no_credential_provider_or_write_side_effects(tmp_path):
    import subprocess
    code = r"""
import builtins, io, sys, socket
import dotenv, openai
sys.dontwritebytecode = True
sys.path.insert(0, REPOSITORY)
def forbidden(*a, **kw):
    raise AssertionError('import attempted credentials, provider, or state write')
def guarded(original):
    def check(file, mode='r', *a, **kw):
        name = str(file).lower()
        if '.env' in name or 'credential' in name and not name.endswith(('.py', '.pyc')):
            forbidden()
        if any(flag in mode for flag in 'wax+'):
            forbidden()
        return original(file, mode, *a, **kw)
    return check
builtins.open = guarded(builtins.open)
io.open = guarded(io.open)
socket.create_connection = forbidden
socket.socket.connect = forbidden
dotenv.load_dotenv = forbidden
dotenv.dotenv_values = forbidden
openai.OpenAI = forbidden
import controlled_trial_plan, controlled_trial_execute, controlled_trial_peak_allowed
assert controlled_trial_execute._CONTROLLED_TRIAL_ALLOW_PEAK is False
print('import safe')
"""
    code = code.replace("REPOSITORY", repr(str(Path(executor.__file__).parent)))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import safe"
    assert not list(tmp_path.iterdir())


def _budgeted_journal(path, budget, raw_client, kwargs, *, normalize=None, save=None):
    proxy = executor._BudgetedClientProxy(raw_client, budget)
    send = lambda: proxy.chat.completions.create(**kwargs)
    return run_journaled_paid_request(journal_path=path,
        identity=request_identity(operation_kind="chapter_translation_recovery", novel_id="fixture", chapter=1,
            provider="deepseek", model=kwargs["model"], profile_id="historical_military", source_sha256="1" * 64,
            request_payload=kwargs),
        save_json=save or _write_json, prepare_attempt=lambda: {}, send_once=send,
        authorize_send=lambda: proxy.chat.completions.authorize_journaled_send(send_once=send, **kwargs),
        normalize_response=normalize or (lambda response, context: {"content": "complete", "finish_reason": "stop"}))


@pytest.mark.parametrize("selection,changes", [
    ("v41-flash", {"max_tokens": 16000}), ("v41-flash", {"max_tokens": 24001}),
    ("v41-flash", {"reasoning_effort": None}),
    ("v41-flash", {"extra_body": {"thinking": {"type": "enabled"}}}),
    ("flash", {"max_tokens": 12000}),
    ("v41-flash", {"model": "deepseek-v4-pro"}),
])
def test_journaled_control_and_retired_route_refusals_are_definitely_unsent(tmp_path, selection, changes):
    kwargs = {**_request(selection), **changes}
    budget = ControlledTrialBudget(selection, 2, 1.0)
    client = _FakeClient(_response())
    path = tmp_path / "recovery.json"
    with pytest.raises(executor.ControlledTrialPreSendRefusal) as raised:
        _budgeted_journal(path, budget, client, kwargs)
    assert raised.value.deterministic_local_budget_refusal and raised.value.failure_kind == "control"
    record = json.loads(path.read_text())
    assert record["status"] == "planned" and record["attempt_context"] == {} and record["response"] == {}
    assert budget.api_calls == client.chat.completions.calls == 0
    assert budget.pre_send_refusals[0]["kind"] == "deterministic_local_budget_refusal"


@pytest.mark.parametrize("failure", ["transport", "missing_usage", "normalization", "post_send_budget_error", "cost_overrun"])
def test_after_authorization_remote_outcomes_remain_uncertain_and_block_replay(tmp_path, failure):
    budget = ControlledTrialBudget("v41-flash", 3, 1.0)
    path = tmp_path / "recovery.json"
    calls = []
    def physical_send(**kwargs):
        assert json.loads(path.read_text())["status"] == "started"
        assert budget.api_calls == 1
        calls.append(kwargs)
        if failure == "transport":
            raise RuntimeError("remote outcome ambiguous")
        if failure == "post_send_budget_error":
            # Exception class alone never proves that transport did not happen.
            raise executor.ControlledTrialPreSendRefusal("raised inside physical transport")
        if failure == "cost_overrun":
            return _response(prompt=100_000_000, completion=24000, misses=100_000_000)
        return SimpleNamespace(usage=None) if failure == "missing_usage" else _response()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=physical_send)))
    def normalize(response, context):
        if failure == "normalization":
            raise ValueError("normalizer failed after response")
        return {"content": "complete"}
    with pytest.raises((RuntimeError, ValueError)):
        _budgeted_journal(path, budget, client, _request(operation="chapter_translation_recovery"), normalize=normalize)
    assert json.loads(path.read_text())["status"] == "uncertain"
    assert budget.api_calls == len(calls) == 1
    assert not budget.pre_send_refusals
    if failure == "missing_usage":
        assert budget.records[0]["status"] == "missing_usage_unaccounted_response"
    if failure == "normalization":
        assert budget.records[0]["status"] == "success" and budget.actual_cost_usd > 0
    if failure == "cost_overrun":
        assert budget.records[0]["status"] == "cost_ceiling_exceeded_after_response"
        assert budget.actual_cost_usd > budget.max_cost_usd
    before = path.read_bytes()
    with pytest.raises(PaidRequestUncertainError):
        _budgeted_journal(path, budget, client, _request(operation="chapter_translation_recovery"), normalize=normalize)
    assert path.read_bytes() == before and len(calls) == 1


def test_authorization_freezes_payload_prices_and_capacity_and_is_single_use():
    budget = ControlledTrialBudget("v41-flash", 2, 1.0)
    observed = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: observed.append(kwargs) or _response())))
    kwargs = _request(operation="chapter_translation_recovery")
    expected = json.loads(json.dumps(kwargs))
    with budget.authorize_request(**kwargs) as authorization:
        kwargs["messages"][0]["content"] = "mutated after authorization"
        kwargs["max_tokens"] = 24001
        with pytest.raises(executor.ControlledTrialPreSendRefusal, match="authorization is active"):
            budget.create(client, **_request())
        budget.send_authorized(client, authorization)
        with pytest.raises(ControlledTrialBudgetError, match="already consumed"):
            budget.send_authorized(client, authorization)
    assert observed == [expected] and budget.api_calls == 1
    with pytest.raises(ControlledTrialBudgetError, match="inactive"):
        budget.send_authorized(client, authorization)
    budget.create(client, **_request())
    assert budget.api_calls == len(observed) == 2


def test_budget_lease_releases_when_journal_cannot_persist_started(tmp_path):
    budget = ControlledTrialBudget("v41-flash", 2, 1.0)
    client = _FakeClient(_response())
    path = tmp_path / "recovery.json"
    def save(path, record):
        if record["status"] == "started":
            raise OSError("STARTED persistence failed")
        _write_json(path, record)
    from production_failure_classification import LocalPersistenceError
    with pytest.raises(LocalPersistenceError):
        _budgeted_journal(path, budget, client, _request(operation="chapter_translation_recovery"), save=save)
    assert budget.api_calls == client.chat.completions.calls == 0
    assert json.loads(path.read_text())["status"] == "planned"
    result = _budgeted_journal(path, budget, client, _request(operation="chapter_translation_recovery"))
    assert result["api_called"] and budget.api_calls == client.chat.completions.calls == 1
    before = path.read_bytes()
    result = _budgeted_journal(path, budget, client, _request(operation="chapter_translation_recovery"))
    assert result["replayed"] and budget.api_calls == client.chat.completions.calls == 1
    assert path.read_bytes() == before


def test_proxy_pre_authorization_cannot_send_before_journal_started(tmp_path):
    budget = ControlledTrialBudget("v41-flash", 2, 1.0)
    client = _FakeClient(_response())
    proxy = executor._BudgetedClientProxy(client, budget)
    path = tmp_path / "recovery.json"
    kwargs = _request(operation="chapter_translation_recovery")
    send = lambda: proxy.chat.completions.create(**kwargs)
    def save(path, record):
        if record["status"] == "started":
            # The lease exists now, but transport is not armed until the journal
            # has persisted STARTED and invokes the yielded callback.
            with pytest.raises(executor.ControlledTrialPreSendRefusal, match="authorization is active"):
                proxy.chat.completions.create(**kwargs)
            assert budget.api_calls == client.chat.completions.calls == 0
        _write_json(path, record)
    run_journaled_paid_request(journal_path=path,
        identity=request_identity(operation_kind="chapter_translation_recovery", novel_id="fixture", chapter=1,
            provider="deepseek", model=kwargs["model"], profile_id="historical_military", source_sha256="1" * 64,
            request_payload=kwargs), save_json=save, prepare_attempt=lambda: {}, send_once=send,
        authorize_send=lambda: proxy.chat.completions.authorize_journaled_send(send_once=send, **kwargs),
        normalize_response=lambda response, context: {"content": "complete"})
    assert budget.api_calls == client.chat.completions.calls == 1
    assert json.loads(path.read_text())["status"] == "response_recorded"
