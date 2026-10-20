import json
import sys

import pytest
import pending_recovery_plan as recovery_plan_module
from contextlib import nullcontext
from types import SimpleNamespace

import pending_recovery_execute as recovery
from novel_project import NovelConfig, NovelProject


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(tmp_path):
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
        {"chapter": 1, "text": "source one"},
        {"chapter": 2, "text": "source two"},
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
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    (project.pending_dir / "0001.md").write_text("paid pending", encoding="utf-8")
    _write_json(project.pending_dir / "0001.json", {"chapter": 1, "stage": "translated"})
    return project


def _plan(project):
    return {
        "novel_id": "fixture",
        "target_chapter": 1,
        "auxiliary_model": "deepseek-v4-pro",
        "state_fingerprint": "f" * 64,
        "approval_token": "a" * 64,
    }


def _raw_client():
    def create(**_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                prompt_cache_hit_tokens=0,
                prompt_cache_miss_tokens=1,
            )
        )

    return SimpleNamespace(
        marker="raw-client",
        chat=SimpleNamespace(
            marker="raw-chat",
            completions=SimpleNamespace(
                marker="raw-completions",
                create=create,
            ),
        ),
    )


def _production_module(project, *, initialize_hook=None):
    module = SimpleNamespace()
    raw_client = _raw_client()
    factory_calls = []

    def openai_factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return raw_client

    def initialize():
        if initialize_hook is not None:
            initialize_hook(module)
        module.progress = json.loads(
            project.progress_file.read_text(encoding="utf-8")
        )
        module.client = module.OpenAI(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        )

    def forbidden_dispatch():
        raise AssertionError(
            "legacy main/unattended runner must never execute in recovery"
        )

    module.OpenAI = openai_factory
    module.initialize_production_runtime_state = initialize
    module.run_unattended_production = forbidden_dispatch
    module.main = forbidden_dispatch
    module.translate_chapter = lambda _chapter: "original generator"
    module.process_chapter = lambda _chapter: True
    module.progress = None
    module.client = None

    return module, raw_client, factory_calls


def _prepare_mocks(monkeypatch, project, production_module):
    plan = _plan(project)
    monkeypatch.setattr(
        recovery,
        "validate_recovery_authorization",
        lambda *a, **k: plan,
    )
    monkeypatch.setattr(
        recovery,
        "load_novel_project",
        lambda *a, **k: project,
    )
    monkeypatch.setattr(
        recovery,
        "load_production_module",
        lambda *a, **k: production_module,
    )
    monkeypatch.setattr(
        recovery,
        "production_api_key_environment",
        lambda _root: nullcontext({"source": "test"}),
    )
    monkeypatch.setattr(
        recovery,
        "prepare_production_runtime",
        lambda *a, **k: {
            "runtime": {
                "pending_chapter": 1,
                "effective_model": "deepseek-v4-pro",
            }
        },
    )
    return plan

def test_actual_production_module_loads_inertly_for_recovery():
    missing = object()
    previous = sys.modules.get(
        recovery.PRODUCTION_MODULE_NAME,
        missing,
    )
    try:
        module = recovery.load_production_module(
            recovery.PRODUCTION_SCRIPT
        )
        assert callable(module.initialize_production_runtime_state)
        assert callable(module.run_unattended_production)
        assert callable(module.process_chapter)
        assert callable(module.translate_chapter)
        assert module._RUNTIME_INITIALIZED is False
        assert module.client is None
        assert module.progress is None
    finally:
        if previous is missing:
            sys.modules.pop(recovery.PRODUCTION_MODULE_NAME, None)
        else:
            sys.modules[recovery.PRODUCTION_MODULE_NAME] = previous


def test_budgeted_openai_factory_routes_create_and_delegates_access():
    raw_client = _raw_client()
    factory_calls = []
    budget_calls = []

    def real_factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return raw_client

    class Budget:
        def create(self, client, **kwargs):
            budget_calls.append((client, kwargs))
            return "budgeted-response"

    proxy_factory = recovery._budgeted_openai_factory(
        real_factory,
        Budget(),
    )
    proxy = proxy_factory("positional", api_key="secret")

    assert isinstance(proxy, recovery._BudgetedClientProxy)
    assert factory_calls == [
        (("positional",), {"api_key": "secret"})
    ]
    assert proxy.marker == "raw-client"
    assert proxy.chat.marker == "raw-chat"
    assert proxy.chat.completions.marker == "raw-completions"
    assert (
        proxy.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[],
        )
        == "budgeted-response"
    )
    assert budget_calls == [
        (
            raw_client,
            {
                "model": "deepseek-v4-pro",
                "messages": [],
            },
        )
    ]

def test_runtime_manifest_drift_invalidates_authorization_before_execution(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    engine_root = tmp_path / "synthetic_engine"
    engine_root.mkdir()
    runtime_file = engine_root / "runtime_helper.py"
    runtime_file.write_text("VALUE = 1\n", encoding="utf-8")
    profile_root = engine_root / "profiles" / "historical_military"
    profile_root.mkdir(parents=True)
    (profile_root / "profile.json").write_text(
        '{"policy": 1}\n',
        encoding="utf-8",
    )
    (engine_root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        recovery_plan_module,
        "ENGINE_RUNTIME_ROOT",
        engine_root,
    )

    plan = recovery_plan_module.build_pending_recovery_plan(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
    )
    progress_before = project.progress_file.read_bytes()
    pending_before = (project.pending_dir / "0001.md").read_bytes()

    runtime_file.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(
        recovery.PendingRecoveryExecutionError,
        match="state fingerprint does not match",
    ):
        recovery.validate_recovery_authorization(
            "fixture",
            repo_root=tmp_path,
            model_selection="pro",
            max_api_calls=6,
            max_cost_usd=0.10,
            expected_state_fingerprint=plan["state_fingerprint"],
            approval_token=plan["approval_token"],
        )

    assert project.progress_file.read_bytes() == progress_before
    assert (project.pending_dir / "0001.md").read_bytes() == pending_before


def test_without_authorization_makes_zero_calls_and_zero_writes(monkeypatch, tmp_path):
    project = _project(tmp_path)
    plan = _plan(project)
    monkeypatch.setattr(recovery, "validate_recovery_authorization", lambda *a, **k: plan)
    before = project.progress_file.read_bytes()
    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=False,
    )
    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert project.progress_file.read_bytes() == before
    assert (project.pending_dir / "0001.md").is_file()


def test_authorized_executor_calls_only_target_process_and_commits(monkeypatch, tmp_path):
    project = _project(tmp_path)
    module, _raw_client_value, factory_calls = _production_module(project)

    def process_chapter(chapter_number):
        assert chapter_number == 1
        pending_md = project.pending_dir / "0001.md"
        pending_json = project.pending_dir / "0001.json"
        out = project.chapter_dir / "0001.md"
        out.write_text(
            pending_md.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        module.progress["completed"] = [1]
        module.progress["last_completed"] = 1
        module.progress["next_chapter"] = 2
        project.progress_file.write_text(
            json.dumps(module.progress),
            encoding="utf-8",
        )
        pending_md.unlink()
        pending_json.unlink()
        return True

    module.process_chapter = process_chapter
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "COMMITTED"
    assert report["api_calls"] == 0
    assert report["target_committed"] is True
    assert report["translation_generation_attempted"] is False
    assert report["startup_periodic_audits_executed"] is False
    assert report["escaped_chapters"] == []
    assert len(factory_calls) == 1
    assert not (project.pending_dir / "0001.md").exists()
    assert not (project.chapter_dir / "0002.md").exists()

def test_partial_commit_mode_dispatches_only_local_reconciler(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)
    calls = []

    def forbidden_process(_chapter_number):
        raise AssertionError("normal process_chapter must not run")

    def reconcile(chapter_number):
        calls.append(chapter_number)
        pending_md = project.pending_dir / "0001.md"
        pending_json = project.pending_dir / "0001.json"
        final = project.chapter_dir / "0001.md"
        final.write_bytes(pending_md.read_bytes())
        module.progress["completed"] = [1]
        module.progress["last_completed"] = 1
        module.progress["next_chapter"] = 2
        project.progress_file.write_text(
            json.dumps(module.progress),
            encoding="utf-8",
        )
        pending_md.unlink()
        pending_json.unlink()
        return True

    module.process_chapter = forbidden_process
    module.reconcile_reviewed_partial_commit_tail = reconcile
    plan = _prepare_mocks(monkeypatch, project, module)
    plan["recovery_mode"] = "reconcile_reviewed_partial_commit_tail"

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )

    assert calls == [1]
    assert report["status"] == "COMMITTED"
    assert report["recovery_mode"] == "reconcile_reviewed_partial_commit_tail"
    assert report["api_calls"] == 0
    assert report["translation_generation_attempted"] is False
    assert not (project.pending_dir / "0001.md").exists()


def test_partial_commit_mode_requires_reconciler_before_target_invocation(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)
    module.reconcile_reviewed_partial_commit_tail = None
    plan = _prepare_mocks(monkeypatch, project, module)
    plan["recovery_mode"] = "reconcile_reviewed_partial_commit_tail"

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )

    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert "partial-commit reconciler was not defined" in report["error"]
    assert report["api_calls"] == 0
    assert (project.pending_dir / "0001.md").is_file()


def test_executor_uses_effective_auxiliary_model_from_validated_plan(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)
    plan = _plan(project)
    plan["auxiliary_model"] = "deepseek-v4-flash"

    monkeypatch.setattr(
        recovery,
        "validate_recovery_authorization",
        lambda *a, **k: plan,
    )
    monkeypatch.setattr(
        recovery,
        "load_novel_project",
        lambda *a, **k: project,
    )
    monkeypatch.setattr(
        recovery,
        "load_production_module",
        lambda *a, **k: module,
    )
    monkeypatch.setattr(
        recovery,
        "production_api_key_environment",
        lambda _root: nullcontext({"source": "test"}),
    )

    observed = {}

    def prepare(model_selection, *args, **kwargs):
        observed["model_selection"] = model_selection
        return {
            "runtime": {
                "pending_chapter": 1,
                "effective_model": "deepseek-v4-flash",
            }
        }

    monkeypatch.setattr(
        recovery,
        "prepare_production_runtime",
        prepare,
    )

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )

    assert observed["model_selection"] == "deepseek-v4-flash"
    assert report["auxiliary_model"] == "deepseek-v4-flash"
    assert report["status"] == "INCOMPLETE"


def test_translation_generation_is_hard_blocked(monkeypatch, tmp_path):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)

    def process_chapter(chapter_number):
        module.translate_chapter({"chapter": chapter_number})
        return True

    module.process_chapter = process_chapter
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "TRANSLATION_GENERATION_BLOCKED"
    assert report["translation_generation_attempted"] is True
    assert report["api_calls"] == 0
    assert (project.pending_dir / "0001.md").is_file()

def test_unexpected_system_exit_becomes_failure_report(monkeypatch, tmp_path):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)

    def process_chapter(_chapter_number):
        raise SystemExit(7)

    module.process_chapter = process_chapter
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert "unexpected SystemExit" in report["error"]
    assert report["api_calls"] == 0

def test_executor_fails_closed_if_initializer_escapes_budgeted_client(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, raw_client, _factory_calls = _production_module(project)

    def escaping_initializer():
        module.progress = json.loads(
            project.progress_file.read_text(encoding="utf-8")
        )
        module.client = raw_client

    module.initialize_production_runtime_state = escaping_initializer
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert "escaped bounded provider proxy" in report["error"]
    assert report["api_calls"] == 0
    assert (project.pending_dir / "0001.md").is_file()


def test_executor_fails_closed_if_initializer_replaces_generation_blocker(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)

    def replacing_initializer():
        module.progress = json.loads(
            project.progress_file.read_text(encoding="utf-8")
        )
        module.client = module.OpenAI(
            api_key="test-key",
            base_url="https://api.deepseek.com",
        )
        module.translate_chapter = (
            lambda _chapter: "replacement generator"
        )

    module.initialize_production_runtime_state = replacing_initializer
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "FAILED_AFTER_AUTHORIZATION"
    assert "translation-generation blocker was replaced" in report["error"]
    assert report["api_calls"] == 0
    assert (project.pending_dir / "0001.md").is_file()


def test_provider_call_during_initialization_is_budgeted(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, factory_calls = _production_module(project)
    original_initializer = module.initialize_production_runtime_state

    def initializing_with_provider_call():
        original_initializer()
        module.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[],
            max_tokens=12000,
            extra_body={"thinking": {"type": "disabled"}},
        )

    module.initialize_production_runtime_state = (
        initializing_with_provider_call
    )
    plan = _prepare_mocks(monkeypatch, project, module)

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "INCOMPLETE"
    assert report["api_calls"] == 1
    assert len(report["provider_calls"]) == 1
    assert report["provider_calls"][0]["status"] == "success"
    assert len(factory_calls) == 1
    assert report["translation_generation_attempted"] is False


def test_executor_restores_previous_production_module_registration(
    monkeypatch,
    tmp_path,
):
    project = _project(tmp_path)
    module, _raw_client_value, _factory_calls = _production_module(project)
    sentinel = object()

    monkeypatch.setitem(
        sys.modules,
        recovery.PRODUCTION_MODULE_NAME,
        sentinel,
    )
    plan = _prepare_mocks(monkeypatch, project, module)

    def load_module(*_args, **_kwargs):
        assert (
            sys.modules[recovery.PRODUCTION_MODULE_NAME]
            is sentinel
        )
        sys.modules[recovery.PRODUCTION_MODULE_NAME] = module
        return module

    monkeypatch.setattr(
        recovery,
        "load_production_module",
        load_module,
    )

    report = recovery.execute_pending_recovery(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
        expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"],
        authorize_paid_recovery=True,
    )
    assert report["status"] == "INCOMPLETE"
    assert report["api_calls"] == 0
    assert (
        sys.modules[recovery.PRODUCTION_MODULE_NAME]
        is sentinel
    )
