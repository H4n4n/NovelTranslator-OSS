import json
from datetime import datetime, timezone

import pytest

from noveltranslator_core import calculate_cost_from_token_counts, pricing_snapshot
from noveltranslator_observability import (
    SafeRunLogger,
    build_status_summary,
    error_event_fields,
    recent_chapter_metrics,
    startup_integrity_errors,
    stats_event_fields,
)


FIXED_NOW = datetime(2026, 8, 21, 5, 30, tzinfo=timezone.utc)


def saved_stats():
    pricing = pricing_snapshot(FIXED_NOW)
    cost, hits, misses = calculate_cost_from_token_counts(1000, 500, 400, 600, pricing)
    return {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cache_hit_tokens": hits,
        "cache_miss_tokens": misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        **pricing,
    }


def write_pending(directory, chapter=2, text="paid"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chapter:04d}.md").write_text(text, encoding="utf-8")
    (directory / f"{chapter:04d}.json").write_text(json.dumps({
        "chapter": chapter,
        "stage": "translated",
        "translation_stats": saved_stats(),
    }), encoding="utf-8")


def test_safe_run_logger_writes_jsonl_event(tmp_path):
    logger = SafeRunLogger(tmp_path / "logs", now=lambda: FIXED_NOW)
    assert logger.emit("chapter_commit", chapter=200, cost_usd=0.25)
    assert logger.path.name == "run_20260821_053000.jsonl"
    event = json.loads(logger.path.read_text(encoding="utf-8"))
    assert event == {
        "timestamp_utc": FIXED_NOW.isoformat(),
        "event": "chapter_commit",
        "chapter": 200,
        "cost_usd": 0.25,
    }


def test_logging_failure_never_raises(tmp_path):
    blocked = tmp_path / "not_a_directory"
    blocked.write_text("file", encoding="utf-8")
    logger = SafeRunLogger(blocked, now=lambda: FIXED_NOW)
    assert logger.path is None
    assert logger.emit("program_start") is False


def test_event_field_helpers_include_stats_and_error_details():
    fields = stats_event_fields(saved_stats())
    assert fields["prompt_tokens"] == 1000
    assert fields["cache_hit_tokens"] == 400
    assert fields["pricing_period"] == "off_peak"
    error = ValueError("bad state")
    assert error_event_fields(error) == {
        "error_type": "ValueError", "error_message": "bad state"
    }


def test_recent_metrics_use_recent_commit_events_and_skip_malformed_lines(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "run_20260821_053000.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"event": "chapter_commit", "cost_usd": 0.1, "elapsed_seconds": 10}) + "\n"
        + json.dumps({"event": "retry", "retry_attempt": 1}) + "\n"
        + json.dumps({"event": "chapter_commit", "cost_usd": 0.3, "elapsed_seconds": 30}) + "\n",
        encoding="utf-8",
    )
    metrics = recent_chapter_metrics(logs)
    assert metrics == {
        "sample_size": 2,
        "average_chapter_cost": pytest.approx(0.2),
        "average_chapter_seconds": pytest.approx(20.0),
    }


def test_status_summary_calculates_operator_fields():
    summary = build_status_summary(
        progress={
            "last_completed": 199, "next_chapter": 200,
            "last_audited_chapter": 180, "total_prompt_tokens": 1000,
            "total_completion_tokens": 500, "rate_correct_cost_usd": 0.25,
        },
        glossary_count=462,
        pending_items=[{"chapter": 200, "stage": "translated"}],
        final_chapter=1406,
        audit_interval=25,
        manual_pause_active=True,
        now=FIXED_NOW,
        recent_metrics={
            "sample_size": 2, "average_chapter_cost": 0.2,
            "average_chapter_seconds": 20.0,
        },
    )
    assert summary["percentage_complete"] == pytest.approx(199 / 1406 * 100)
    assert summary["next_audit_chapter"] == 205
    assert summary["remaining_chapters"] == 1207
    assert summary["manual_pause_active"] is True
    assert summary["pricing_period"] == "off_peak"


def test_status_summary_labels_weekend_peak_clock_hours_as_off_peak():
    summary = build_status_summary(
        progress={"last_completed": 0, "next_chapter": 1},
        glossary_count=0,
        pending_items=[],
        final_chapter=10,
        audit_interval=25,
        manual_pause_active=False,
        now=datetime(2026, 8, 22, 7, tzinfo=timezone.utc),
        recent_metrics={},
    )
    assert summary["pricing_period"] == "off_peak"


def test_startup_self_check_accepts_consistent_pending_recovery(tmp_path):
    output = tmp_path / "chapters"
    pending = tmp_path / "pending"
    output.mkdir()
    (output / "0001.md").write_text("committed", encoding="utf-8")
    write_pending(pending)
    progress = {"completed": [1], "last_completed": 1, "next_chapter": 2}
    assert startup_integrity_errors(progress, output, pending) == []


def test_startup_self_check_stops_unexpected_output_without_pending(tmp_path):
    output = tmp_path / "chapters"
    pending = tmp_path / "pending"
    output.mkdir()
    pending.mkdir()
    (output / "0001.md").write_text("committed", encoding="utf-8")
    (output / "0002.md").write_text("unexpected", encoding="utf-8")
    progress = {"completed": [1], "last_completed": 1, "next_chapter": 2}
    errors = startup_integrity_errors(progress, output, pending)
    assert any("duplicate billing" in error for error in errors)


def test_startup_self_check_allows_byte_identical_interrupted_commit(tmp_path):
    output = tmp_path / "chapters"
    pending = tmp_path / "pending"
    output.mkdir()
    (output / "0001.md").write_text("committed", encoding="utf-8")
    (output / "0002.md").write_text("paid", encoding="utf-8")
    write_pending(pending, text="paid")
    progress = {"completed": [1], "last_completed": 1, "next_chapter": 2}
    assert startup_integrity_errors(progress, output, pending) == []
