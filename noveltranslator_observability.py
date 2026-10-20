"""Import-safe logging, status, and startup-safety helpers."""

import json
from datetime import datetime, timezone
from pathlib import Path

from noveltranslator_core import (
    inspect_pending_state,
    is_deepseek_peak_time,
    pending_matches_committed,
    validate_progress_consistency,
)


STAT_EVENT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "pricing_period",
    "reasoning_tokens",
    "reasoning_tokens_exposed",
    "visible_completion_tokens",
    "provider",
    "accounting",
    "pricing_unit",
    "pricing_version",
    "provider_invoice_claimed",
)


class SafeRunLogger:
    """Append JSONL events while making every logging failure non-fatal."""

    def __init__(self, log_dir, now=None):
        self.path = None
        self._now = now or (lambda: datetime.now(timezone.utc))
        try:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = self._now().astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self.path = log_dir / f"run_{stamp}.jsonl"
        except Exception:
            self.path = None

    def emit(self, event, **fields):
        try:
            if self.path is None:
                return False
            payload = {
                "timestamp_utc": self._now().astimezone(timezone.utc).isoformat(),
                "event": event,
            }
            payload.update({key: value for key, value in fields.items() if value is not None})
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            return True
        except Exception:
            return False


def stats_event_fields(stats):
    stats = stats or {}
    return {key: stats.get(key) for key in STAT_EVENT_FIELDS if key in stats}


def error_event_fields(error):
    return {
        "error_type": type(error).__name__,
        "error_message": str(error),
    }


def recent_chapter_metrics(log_dir, limit=20, *, accounting_metadata=None, start_chapter=None):
    """Read recent chapter_commit events, skipping malformed/unreadable lines."""
    events = []
    try:
        paths = sorted(Path(log_dir).glob("run_*.jsonl"), reverse=True)
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if item.get("event") == "chapter_commit":
                    if accounting_metadata is not None and any(item.get(k) != v for k, v in accounting_metadata.items()):
                        continue
                    if start_chapter is not None and (type(item.get("chapter")) is not int or item["chapter"] < start_chapter):
                        continue
                    events.append(item)
                    if len(events) >= limit:
                        break
            if len(events) >= limit:
                break
    except OSError:
        pass
    costs = [item["cost_usd"] for item in events if isinstance(item.get("cost_usd"), (int, float))]
    times = [item["elapsed_seconds"] for item in events if isinstance(item.get("elapsed_seconds"), (int, float))]
    return {
        "sample_size": len(events),
        "average_chapter_cost": sum(costs) / len(costs) if costs else None,
        "average_chapter_seconds": sum(times) / len(times) if times else None,
    }


def build_status_summary(
    progress, glossary_count, pending_items, final_chapter, audit_interval,
    manual_pause_active, now, recent_metrics,
):
    completed = progress.get("last_completed", 0)
    last_audited = progress.get("last_audited_chapter", 0)
    next_audit = min(final_chapter, last_audited + audit_interval)
    return {
        "completed_chapter": completed,
        "next_chapter": progress.get("next_chapter", completed + 1),
        "percentage_complete": completed / final_chapter * 100 if final_chapter else 0.0,
        "pending": pending_items,
        "last_audited_chapter": last_audited,
        "next_audit_chapter": next_audit,
        "glossary_entry_count": glossary_count,
        "total_prompt_tokens": progress.get("total_prompt_tokens", 0),
        "total_completion_tokens": progress.get("total_completion_tokens", 0),
        "rate_correct_cost_usd": progress.get("rate_correct_cost_usd", 0.0),
        "average_recent_chapter_cost": recent_metrics.get("average_chapter_cost"),
        "average_recent_chapter_seconds": recent_metrics.get("average_chapter_seconds"),
        "recent_sample_size": recent_metrics.get("sample_size", 0),
        "remaining_chapters": max(0, final_chapter - completed),
        "manual_pause_active": manual_pause_active,
        "pricing_period": "peak" if is_deepseek_peak_time(now) else "off_peak",
        "current_utc": now.astimezone(timezone.utc).isoformat(),
    }


def progress_commit_integrity_errors(progress, output_dir, pending_dir):
    """Return progress/final-output contradictions, preserving one proven exception."""
    output_dir = Path(output_dir)
    committed_files = {
        int(path.stem)
        for path in output_dir.glob("*.md")
        if path.stem.isdigit()
    } if output_dir.exists() else set()
    report = validate_progress_consistency(progress, committed_files)
    errors = [
        error for error in report["errors"]
        if error != "committed chapter files contain chapters absent from progress"
    ]
    next_chapter = progress.get("next_chapter")
    for chapter in report["unexpected_committed_files"]:
        recoverable = (
            chapter == next_chapter
            and pending_matches_committed(chapter, pending_dir, output_dir)
        )
        if not recoverable:
            errors.append(
                f"unexpected committed Chapter {chapter} does not exactly match "
                "the pending recovery candidate and could risk overwrite or duplicate billing"
            )
    return list(dict.fromkeys(errors))


def startup_integrity_errors(progress, output_dir, pending_dir):
    """Return local inconsistencies that make starting API work unsafe."""
    errors = progress_commit_integrity_errors(
        progress,
        output_dir,
        pending_dir,
    )
    completed = progress.get("completed", [])
    next_chapter = progress.get("next_chapter")
    pending_report = inspect_pending_state(pending_dir, completed, next_chapter)
    errors.extend(pending_report["errors"])
    return list(dict.fromkeys(errors))
