import builtins

import pytest

from production_failure_classification import NonProviderProductionError
from production_lifecycle import (
    run_due_audits,
    run_final_audit_if_needed,
    run_unattended_lifecycle,
)


class SyntheticProviderError(RuntimeError):
    pass


class SyntheticLocalError(NonProviderProductionError):
    pass


def test_due_audits_runs_only_complete_intervals():
    progress = {
        "last_completed": 5,
        "last_audited_chapter": 1,
    }
    calls = []

    def audit(start, end):
        calls.append((start, end))
        progress["last_audited_chapter"] = end

    run_due_audits(
        progress=progress,
        audit_interval=2,
        run_periodic_audit=audit,
    )

    assert calls == [(2, 3), (4, 5)]
    assert progress["last_audited_chapter"] == 5


def test_final_audit_runs_only_for_completed_tail():
    progress = {
        "last_completed": 5,
        "last_audited_chapter": 3,
    }
    calls = []

    run_final_audit_if_needed(
        progress=progress,
        final_chapter=5,
        run_periodic_audit=lambda start, end: calls.append(
            (start, end)
        ),
    )

    assert calls == [(4, 5)]

    calls.clear()
    progress["last_completed"] = 4

    run_final_audit_if_needed(
        progress=progress,
        final_chapter=5,
        run_periodic_audit=lambda start, end: calls.append(
            (start, end)
        ),
    )

    assert calls == []


def _fixture(*, next_chapter=2, final_chapter=3):
    events = []
    output = []
    errors = []

    progress = {
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
    glossary = {"entries": {}}
    prices = {
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

    def process(chapter):
        events.append(f"process:{chapter}")
        progress["completed"].append(chapter)
        progress["last_completed"] = chapter
        progress["next_chapter"] = chapter + 1
        return True

    kwargs = {
        "progress": progress,
        "glossary": glossary,
        "final_chapter": final_chapter,
        "auto_off_peak_only": True,
        "manual_pause_file": "pause.flag",
        "prices": prices,
        "run_log_path": None,
        "reconcile_stale_completed_pending": (
            lambda: events.append("reconcile")
        ),
        "perform_startup_self_check": (
            lambda: events.append("self_check")
        ),
        "reconcile_partial_commit_before_audits": (
            lambda: False
        ),
        "run_due_audits_callback": (
            lambda: events.append("due_audits")
        ),
        "run_final_audit_if_needed_callback": (
            lambda: events.append("final_audit")
        ),
        "process_chapter": process,
        "log_event": lambda event, **fields: events.append(
            ("log", event, fields)
        ),
        "log_error": lambda error, **fields: errors.append(
            (error, fields)
        ),
        "describe_api_error": lambda exc: f"provider-described:{exc}",
        "handled_error_types": (SyntheticProviderError,),
        "describe_local_error": lambda exc: f"local-described:{exc}",
        "local_error_types": (NonProviderProductionError,),
        "print_fn": output.append,
    }
    return progress, events, output, errors, kwargs


def _sequence_events(events):
    return [
        item
        for item in events
        if isinstance(item, str)
    ]


def test_unattended_lifecycle_preserves_full_range_sequence():
    progress, events, _output, errors, kwargs = _fixture()

    assert run_unattended_lifecycle(**kwargs) is None

    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
        "process:2",
        "due_audits",
        "process:3",
        "due_audits",
        "final_audit",
    ]
    assert progress["last_completed"] == 3
    assert progress["next_chapter"] == 4
    assert errors == []
    assert events[-1][0:2] == ("log", "shutdown")
    assert events[-1][2]["reason"] == "translation_complete"


def test_startup_partial_reconciliation_precedes_due_audits_and_chapter_loop():
    progress, events, _output, errors, kwargs = _fixture()

    def reconcile_partial():
        events.append("partial_reconcile")
        progress["completed"].append(2)
        progress["last_completed"] = 2
        progress["next_chapter"] = 3
        return True

    kwargs["reconcile_partial_commit_before_audits"] = reconcile_partial

    assert run_unattended_lifecycle(**kwargs) is None

    sequence = _sequence_events(events)
    assert sequence[:4] == [
        "reconcile",
        "self_check",
        "partial_reconcile",
        "due_audits",
    ]
    assert "process:2" not in sequence
    assert "process:3" in sequence
    assert progress["last_completed"] == 3
    assert progress["next_chapter"] == 4
    assert errors == []


def test_unattended_lifecycle_bounds_exact_target():
    progress, events, _output, _errors, kwargs = _fixture()

    assert (
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=2,
        )
        is None
    )

    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
        "process:2",
        "due_audits",
        "final_audit",
    ]
    assert progress["last_completed"] == 2
    assert progress["next_chapter"] == 3
    assert "process:3" not in events


def test_target_start_mismatch_fails_closed_before_processing():
    progress, events, _output, errors, kwargs = _fixture()

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=3,
        )

    assert exc.value.code == 1
    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
    ]
    assert progress["next_chapter"] == 2
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_local"


def test_invalid_target_fails_closed_before_processing():
    _progress, events, _output, errors, kwargs = _fixture()

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=True,
        )

    assert exc.value.code == 1
    assert not any(
        isinstance(item, str) and item.startswith("process:")
        for item in events
    )
    assert len(errors) == 1


def test_false_process_result_stops_at_exact_chapter():
    progress, events, _output, errors, kwargs = _fixture()

    def fail_process(chapter):
        events.append(f"process:{chapter}")
        return False

    kwargs["process_chapter"] = fail_process

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert progress["last_completed"] == 1
    assert "process:3" not in events
    assert errors == []
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "chapter_failed"


def test_keyboard_interrupt_is_contained_as_clean_operator_stop():
    _progress, events, _output, errors, kwargs = _fixture()

    def interrupt():
        raise KeyboardInterrupt

    kwargs["perform_startup_self_check"] = interrupt

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 0
    assert errors == []
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "keyboard_interrupt"


def test_provider_error_uses_provider_description_and_safe_stop():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise SyntheticProviderError("synthetic provider failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_provider"
    assert "provider-described:synthetic provider failure" in output
    assert not any("local-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "provider_error"


def test_local_controlled_error_uses_local_description_and_safe_stop():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise SyntheticLocalError("synthetic local failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_local"
    assert "local-described:synthetic local failure" in output
    assert not any("provider-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "local_error"


def test_raw_runtime_error_is_not_classified_as_provider_failure():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise RuntimeError("unclassified runtime failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(RuntimeError, match="unclassified runtime failure"):
        run_unattended_lifecycle(**kwargs)

    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_unexpected"
    assert not any("provider-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "unexpected_error"


def test_unexpected_error_is_logged_and_reraised():
    _progress, events, _output, errors, kwargs = _fixture()

    def fail():
        raise ValueError("unexpected")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(ValueError, match="unexpected"):
        run_unattended_lifecycle(**kwargs)

    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_unexpected"
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "unexpected_error"


def test_all_lifecycle_output_uses_injected_print(monkeypatch):
    _progress, _events, output, _errors, kwargs = _fixture()

    def forbidden_print(*_args, **_kwargs):
        raise AssertionError("shared lifecycle used builtins.print directly")

    monkeypatch.setattr(builtins, "print", forbidden_print)

    run_unattended_lifecycle(**kwargs)

    assert output
