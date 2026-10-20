import importlib.util
from pathlib import Path
import sys
import uuid

import openai
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCRIPT = REPO_ROOT / (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)


def _import_inert_production(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def forbidden_openai(*_args, **_kwargs):
        raise AssertionError("OpenAI client was constructed during module import")

    monkeypatch.setattr(openai, "OpenAI", forbidden_openai)
    module_name = f"_legacy_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PRODUCTION_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module_name, module


def test_production_module_import_is_zero_api_and_zero_project_state(monkeypatch, tmp_path):
    before = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
    )
    name, module = _import_inert_production(monkeypatch, tmp_path)
    try:
        after = sorted(
            str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
        )
        assert after == before
        assert module.MODEL is None
        assert module.ACTIVE_NOVEL_CONFIG is None
        assert module.ACTIVE_PROFILE is None
        assert module.ACTIVE_NOVEL_POLICY is None
        assert module.RUN_LOGGER is None
        assert module.api_key is None
        assert module.client is None
        assert module.chapters is None
        assert module.chapter_map is None
        assert module.glossary is None
        assert module.progress is None
        assert module.continuity is None
        assert module.issues_data is None
        assert module.audit_history is None
        assert module.system_prompt is None
        assert module._RUNTIME_INITIALIZED is False
        assert module._RUNTIME_INITIALIZING is False
        assert callable(module.initialize_production_runtime_state)
        assert callable(module.run_unattended_production)
        assert callable(module.main)
    finally:
        sys.modules.pop(name, None)


def test_production_main_dispatches_initializer_before_unattended_runner(
    monkeypatch, tmp_path
):
    name, module = _import_inert_production(monkeypatch, tmp_path)
    events = []
    sentinel = object()
    try:
        monkeypatch.setattr(
            module,
            "initialize_production_runtime_state",
            lambda: events.append("initialize"),
        )
        monkeypatch.setattr(
            module,
            "run_unattended_production",
            lambda: events.append("run") or sentinel,
        )
        assert module.main() is sentinel
        assert events == ["initialize", "run"]
    finally:
        sys.modules.pop(name, None)
def _configure_unattended_runner_fixture(
    monkeypatch,
    module,
    *,
    next_chapter=2,
    final_chapter=3,
):
    events = []

    module.progress = {
        "completed": list(range(1, next_chapter)),
        "last_completed": next_chapter - 1,
        "next_chapter": next_chapter,
        "last_audited_chapter": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "rate_correct_cost_start_chapter": 1,
        "rate_correct_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
    }
    module.FINAL_CHAPTER = final_chapter
    module.AUTO_OFF_PEAK_ONLY = True
    module.MANUAL_PAUSE_FILE = Path("pause.flag")
    module.V4_PRO_PRICES = {
        "off_peak": {
            "cache_hit": 0.0,
            "cache_miss": 0.0,
            "output": 0.0,
        },
        "peak": {
            "cache_hit": 0.0,
            "cache_miss": 0.0,
            "output": 0.0,
        },
    }
    module.glossary = {"entries": {}}

    class FakeRunLogger:
        path = None

    module.RUN_LOGGER = FakeRunLogger()

    monkeypatch.setattr(
        module,
        "print",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "log_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        module,
        "log_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        module,
        "describe_api_error",
        lambda exc: str(exc),
    )
    monkeypatch.setattr(
        module,
        "reconcile_stale_completed_pending",
        lambda: events.append("reconcile"),
    )
    monkeypatch.setattr(
        module,
        "perform_startup_self_check",
        lambda: events.append("self_check"),
    )
    monkeypatch.setattr(
        module,
        "run_due_audits",
        lambda: events.append("due_audits"),
    )
    monkeypatch.setattr(
        module,
        "run_final_audit_if_needed",
        lambda: events.append("final_audit"),
    )

    def process_chapter(chapter_number):
        events.append(f"process:{chapter_number}")
        module.progress["completed"].append(chapter_number)
        module.progress["last_completed"] = chapter_number
        module.progress["next_chapter"] = chapter_number + 1
        return True

    monkeypatch.setattr(module, "process_chapter", process_chapter)
    return events


def test_unattended_runner_without_target_preserves_full_range(
    monkeypatch,
    tmp_path,
):
    name, module = _import_inert_production(monkeypatch, tmp_path)
    try:
        events = _configure_unattended_runner_fixture(
            monkeypatch,
            module,
            next_chapter=2,
            final_chapter=3,
        )

        assert module.run_unattended_production() is None

        assert events == [
            "reconcile",
            "self_check",
            "due_audits",
            "process:2",
            "due_audits",
            "process:3",
            "due_audits",
            "final_audit",
        ]
        assert module.progress["last_completed"] == 3
        assert module.progress["next_chapter"] == 4
    finally:
        sys.modules.pop(name, None)


def test_unattended_runner_target_bounds_execution_to_exact_chapter(
    monkeypatch,
    tmp_path,
):
    name, module = _import_inert_production(monkeypatch, tmp_path)
    try:
        events = _configure_unattended_runner_fixture(
            monkeypatch,
            module,
            next_chapter=2,
            final_chapter=3,
        )

        assert (
            module.run_unattended_production(target_chapter=2)
            is None
        )

        assert events == [
            "reconcile",
            "self_check",
            "due_audits",
            "process:2",
            "due_audits",
            "final_audit",
        ]
        assert module.progress["last_completed"] == 2
        assert module.progress["next_chapter"] == 3
        assert "process:3" not in events
    finally:
        sys.modules.pop(name, None)


def test_unattended_runner_target_fails_closed_if_start_changed(
    monkeypatch,
    tmp_path,
):
    name, module = _import_inert_production(monkeypatch, tmp_path)
    try:
        events = _configure_unattended_runner_fixture(
            monkeypatch,
            module,
            next_chapter=2,
            final_chapter=3,
        )

        with pytest.raises(SystemExit) as exc:
            module.run_unattended_production(target_chapter=3)

        assert exc.value.code == 1
        assert events == [
            "reconcile",
            "self_check",
            "due_audits",
        ]
        assert not any(
            event.startswith("process:")
            for event in events
        )
        assert module.progress["next_chapter"] == 2
    finally:
        sys.modules.pop(name, None)



def test_unattended_runner_resolves_print_at_call_time_for_model_bridge(
    monkeypatch,
    tmp_path,
    capsys,
):
    from production_output_runtime import model_aware_legacy_print
    from translation_model_config import resolve_translation_model

    name, module = _import_inert_production(monkeypatch, tmp_path)

    class FakeRunLogger:
        path = None

    try:
        module.progress = {
            "last_completed": 0,
            "next_chapter": 1,
            "last_audited_chapter": 0,
        }
        module.glossary = {"entries": {}}
        module.FINAL_CHAPTER = 1
        module.AUTO_OFF_PEAK_ONLY = True
        module.MANUAL_PAUSE_FILE = Path("pause.flag")
        module.V4_PRO_PRICES = {
            "off_peak": {
                "cache_hit": 0.0,
                "cache_miss": 0.0,
                "output": 0.0,
            },
            "peak": {
                "cache_hit": 0.0,
                "cache_miss": 0.0,
                "output": 0.0,
            },
        }
        module.RUN_LOGGER = FakeRunLogger()

        def shared_runner(**kwargs):
            kwargs["print_fn"](
                "V4 Pro rates / 1M: delegated lifecycle smoke"
            )

        monkeypatch.setattr(
            module,
            "run_unattended_lifecycle",
            shared_runner,
        )

        flash = resolve_translation_model("flash")
        with model_aware_legacy_print(flash):
            module.run_unattended_production()

        assert (
            "DeepSeek V4 Flash rates / 1M: "
            "delegated lifecycle smoke"
            in capsys.readouterr().out
        )
    finally:
        sys.modules.pop(name, None)


def test_unattended_runner_remains_stable_compatibility_callable(
    monkeypatch,
    tmp_path,
):
    name, module = _import_inert_production(monkeypatch, tmp_path)
    captured = module.run_unattended_production
    sentinel = object()
    observed = {}

    class FakeRunLogger:
        path = None

    try:
        module.progress = {
            "last_completed": 1,
            "next_chapter": 2,
            "last_audited_chapter": 0,
        }
        module.glossary = {"entries": {}}
        module.FINAL_CHAPTER = 3
        module.AUTO_OFF_PEAK_ONLY = True
        module.MANUAL_PAUSE_FILE = Path("pause.flag")
        module.V4_PRO_PRICES = {
            "off_peak": {
                "cache_hit": 0.0,
                "cache_miss": 0.0,
                "output": 0.0,
            },
            "peak": {
                "cache_hit": 0.0,
                "cache_miss": 0.0,
                "output": 0.0,
            },
        }
        module.RUN_LOGGER = FakeRunLogger()

        def shared_runner(**kwargs):
            observed.update(kwargs)
            return sentinel

        monkeypatch.setattr(
            module,
            "run_unattended_lifecycle",
            shared_runner,
        )

        result = module.run_unattended_production(
            target_chapter=2,
        )

        assert result is sentinel
        assert module.run_unattended_production is captured
        assert observed["progress"] is module.progress
        assert observed["glossary"] is module.glossary
        assert observed["final_chapter"] == 3
        assert observed["target_chapter"] == 2
        assert observed["process_chapter"] is module.process_chapter
        assert (
            observed["reconcile_partial_commit_before_audits"]
            is module.reconcile_startup_partial_commit_if_present
        )
        assert observed["run_due_audits_callback"] is module.run_due_audits
        assert (
            observed["run_final_audit_if_needed_callback"]
            is module.run_final_audit_if_needed
        )
        assert RuntimeError not in observed["handled_error_types"]
        assert module.APIError in observed["handled_error_types"]
        assert (
            observed["local_error_types"]
            == (module.NonProviderProductionError,)
        )
        assert observed["describe_local_error"] is module.describe_non_provider_error
    finally:
        sys.modules.pop(name, None)
