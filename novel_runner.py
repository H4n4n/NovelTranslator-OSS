"""Run one novel from a single command, within a standing budget.

Follows the existing preflight routes (cleanup, paid-pending recovery, normal
run) and restarts after a stop as long as the novel keeps advancing. The result
is also saved to data/last_run.json for the interface.
"""
import json
import time
from datetime import datetime, timezone

from build_checkpoint_epub import rebuild_reader_epub
from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
from noveltranslator_core import atomic_write_json
from novel_project import load_novel_project
from production_paid_request_journal import allow_another_resend, release_ambiguous_requests
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_run_lock import production_run_lock
from production_translation_request import filter_refused_marker
from provider_retry_policy import MAX_AMBIGUOUS_RESENDS
from review_queue import add_item as add_review_item

MODEL = "v41-flash"
REQUEST_SET_ASIDE_USD = 1.50  # A request with no output limit, priced for its whole context at peak rates.
PHYSICAL_CALL_LIMIT = 1_000_000  # The budget is the real limit. This only satisfies the planner.
MAX_NO_PROGRESS_RUNS = 2
MAX_STEPS = 10_000

COMPLETE = "COMPLETE"
BUDGET_REACHED = "BUDGET_REACHED"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
TRIAL_FINISHED = "TRIAL_FINISHED"

STOP_MESSAGES = {
    "provider_key_or_credit": ("The provider rejected the API key or the account is out of credit.",
                               "Check the key and your balance at commandcode.ai/usage, then start again."),
    "provider_refused": ("The provider refused a request.", "Check the review list for this chapter, then start again."),
    "provider_rejected": ("The provider rejected a request as invalid.", "This is probably a bug. Report the log file."),
    "provider_unavailable": ("The provider stayed unavailable through all retries.", "Start again later."),
    "provider_usage_limit": ("The plan's usage limit didn't reset in time.", "Check commandcode.ai/usage, then start again."),
    "network_unavailable": ("The internet connection stayed down for 12 hours.", "Reconnect, then start again."),
    "ambiguous_request_blocked": ("A request failed twice with no answer, so it won't be resent automatically.",
                                  "Check commandcode.ai/usage, then allow the resend."),
    "no_budget": ("This novel has no budget yet.", "Start it with a budget."),
    "writer_busy": ("Another process is already working on this novel.", "Wait for it to finish."),
}


def budget_path(project):
    return project.data_dir / "run_budget.json"


def _progress(project):
    return json.loads(project.progress_file.read_text(encoding="utf-8"))


def _cost(progress):
    return float(progress.get("rate_correct_cost_usd", 0.0))


def set_budget(project, limit_usd):
    """Allow limit_usd of spending from now on, replacing any earlier budget."""
    if type(limit_usd) not in (int, float) or limit_usd <= 0:
        raise ValueError("budget must be a positive number of USD")
    value = {"limit_usd": float(limit_usd), "baseline_cost_usd": _cost(_progress(project)),
             "set_utc": datetime.now(timezone.utc).isoformat()}
    atomic_write_json(budget_path(project), value)
    return value


def remaining_budget(project):
    try:
        value = json.loads(budget_path(project).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    spent = _cost(_progress(project)) - value["baseline_cost_usd"]
    return value["limit_usd"] - spent


def last_logged_error(project):
    """The newest error in the novel's run logs, for a plain-language stop message."""
    logs = sorted(project.logs_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in logs[:2]:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "error":
                return event
    return None


def describe_stop(project, reason=None):
    error = None if reason else last_logged_error(project)
    if error and error.get("error_type") == "GoatProviderStop":
        reason = error.get("error_message")
    if reason in STOP_MESSAGES:
        message, action = STOP_MESSAGES[reason]
        return dict(reason=reason, message=message, action=action)
    detail = f"{error.get('error_type')} during {error.get('stage')}" if error else "no error was logged"
    return dict(reason=reason or "unexpected_stop", message=f"The run stopped: {detail}.",
                action="See the latest file in this novel's logs folder.")


def allow_resend(project):
    """Let a request that failed twice with no answer be sent once more."""
    with production_run_lock(project.root):
        return allow_another_resend(project.data_dir / "provider_request_journal",
                                    next_chapter=_progress(project)["next_chapter"], max_resends=MAX_AMBIGUOUS_RESENDS)


def _release_ambiguous(project, next_chapter):
    with production_run_lock(project.root):
        _set_aside_orphan_pending(project)
        released = release_ambiguous_requests(project.data_dir / "provider_request_journal", next_chapter=next_chapter,
            save_json=atomic_write_json, max_resends=MAX_AMBIGUOUS_RESENDS)
        _clear_unanswered_review_attempt(project, next_chapter)
        released["refused"] = _retry_refused_translation(project, next_chapter)
        return released


def _retry_refused_translation(project, chapter):
    """The provider's filter refused this chapter, at no cost. Returns True if it refused twice.

    The filter reads the user message, so the next try moves the glossary and continuity
    to the system message. The chapter goes on the review list.
    """
    record_path = project.data_dir / "provider_request_journal" / f"{chapter:04d}.chapter_translation.json"
    if not record_path.is_file():
        return False
    record = json.loads(record_path.read_text(encoding="utf-8"))
    response = record.get("response") or {}
    choices = (response.get("raw_response") or {}).get("choices") or [{}]
    if record.get("status") != "response_recorded" or "content_filter" not in (response.get("finish_reason"), choices[0].get("finish_reason")):
        return False
    marker = filter_refused_marker(project.data_dir, chapter)
    set_aside = marker.with_suffix(".refused_request.json")
    if set_aside.exists():
        return True
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(marker, dict(chapter=chapter, operation_id=record["operation_id"], utc=datetime.now(timezone.utc).isoformat()))
    record_path.replace(set_aside)
    add_review_item(project.data_dir, chapter=chapter, kind="filter_retry",
                    detail="The provider's filter refused the normal request, so this chapter was sent again with its glossary and context in a different place.")
    return False


def _set_aside_orphan_pending(project):
    """A crash can save pending text before its metadata. The journal still holds
    the paid response, so the text moves to backup and the next run replays it."""
    journal_dir = project.data_dir / "provider_request_journal"
    for text in sorted(project.pending_dir.glob("*.md")):
        if not text.stem.isdigit() or text.with_suffix(".json").exists():
            continue
        record_path = journal_dir / f"{text.stem}.chapter_translation.json"
        if not record_path.is_file():
            continue
        if json.loads(record_path.read_text(encoding="utf-8")).get("status") != "response_recorded":
            continue
        destination = project.backup_dir / "orphan_pending" / f"{text.stem}_{int(time.time())}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        text.replace(destination)


def _clear_unanswered_review_attempt(project, next_chapter):
    """A review that got no answer may simply run again from its reusable slot."""
    meta_path = project.pending_dir / f"{next_chapter:04d}.json"
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if (meta.get(CHAPTER_REVIEW_RECOVERY_KEY) or {}).get("status") != "original_started":
        return
    journal_dir = project.data_dir / "provider_request_journal"
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in journal_dir.glob(f"{next_chapter:04d}.*glossary_review*.json")]
    if any(record.get("status") == "response_recorded" for record in records):
        return
    meta.pop(CHAPTER_REVIEW_RECOVERY_KEY)
    atomic_write_json(meta_path, meta)


def _run_cleanup(novel_id, repo_root):
    from completed_pending_cleanup import run_cleanup
    run_cleanup(novel_id, repo_root=repo_root)


def _run_pending_recovery(novel_id, repo_root, allowance):
    from pending_recovery_execute import execute_pending_recovery
    from pending_recovery_plan import build_pending_recovery_plan
    settings = dict(repo_root=repo_root, model_selection=MODEL, max_api_calls=PHYSICAL_CALL_LIMIT,
                    provider_selection=PROVIDER_COMMAND_CODE, max_credit_equivalent_usd=allowance)
    plan = build_pending_recovery_plan(novel_id, **settings)
    return execute_pending_recovery(novel_id, max_cost_usd=None, expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"], authorize_paid_recovery=True, **settings)


def _run_saturation_recovery(novel_id, repo_root, allowance):
    from goat_saturation_recovery_execute import execute_goat_saturation_recovery
    from goat_saturation_recovery_plan import build_goat_saturation_recovery_plan
    settings = dict(repo_root=repo_root, max_api_calls=PHYSICAL_CALL_LIMIT, max_credit_equivalent_usd=allowance)
    plan = build_goat_saturation_recovery_plan(novel_id, **settings)
    return execute_goat_saturation_recovery(novel_id, expected_state_fingerprint=plan["state_fingerprint"],
        approval_token=plan["approval_token"], authorize_paid_recovery=True, **settings)


def _run_production(project, novel_id, repo_root, allowance):
    from controlled_trial_plan import build_controlled_trial_plan
    from production_goat_execution import execute_authorized_goat
    with production_run_lock(project.root):
        plan = build_controlled_trial_plan(novel_id, repo_root=repo_root, model_selection=MODEL,
            provider_selection=PROVIDER_COMMAND_CODE, max_api_calls=PHYSICAL_CALL_LIMIT,
            max_credit_equivalent_usd=allowance, unattended=True)
        return execute_authorized_goat(plan, repo_root=project.repository_root, recovery=False)


def run_novel(novel_id, *, repo_root=None, budget_usd=None, trial_chapters=None):
    """With trial_chapters, translate only that many more, then stop so you can read them."""
    project = load_novel_project(novel_id, repo_root=repo_root)
    trial_path = project.data_dir / "trial_stop.json"
    if trial_chapters is not None:
        atomic_write_json(trial_path, {"after_chapter": _progress(project)["last_completed"] + trial_chapters})
    try:
        result = _run_until_stopped(project, novel_id, repo_root, budget_usd)
    finally:
        trial_path.unlink(missing_ok=True)
    progress = _progress(project)
    result["reader_epub"] = rebuild_reader_epub(project, take_lock=True).get("status")
    result.update(novel_id=novel_id, last_completed=progress["last_completed"], next_chapter=progress["next_chapter"],
                  final_chapter=project.config.final_chapter, finished_utc=datetime.now(timezone.utc).isoformat())
    atomic_write_json(project.data_dir / "last_run.json", result)
    return result


def _trial_is_finished(project):
    path = project.data_dir / "trial_stop.json"
    return path.is_file() and _progress(project)["last_completed"] >= json.loads(path.read_text(encoding="utf-8"))["after_chapter"]


def _run_until_stopped(project, novel_id, repo_root, budget_usd):
    from operator_preflight import build_operator_preflight
    from production_run_lock import ProductionRunLockedError
    if budget_usd is not None:
        set_budget(project, budget_usd)
    runs_without_progress = 0
    last_state = None
    for _ in range(MAX_STEPS):
        remaining = remaining_budget(project)
        if remaining is None:
            return dict(status=STOPPED, **describe_stop(project, "no_budget"))
        if remaining <= 0:
            return dict(status=BUDGET_REACHED, message="The budget for this novel is used up.",
                        action="Add budget to continue.")
        progress = _progress(project)
        try:
            released = _release_ambiguous(project, progress["next_chapter"])
        except ProductionRunLockedError:
            return dict(status=STOPPED, **describe_stop(project, "writer_busy"))
        if released["blocked"]:
            return dict(status=STOPPED, blocked_requests=released["blocked"],
                        **describe_stop(project, "ambiguous_request_blocked"))
        if released["refused"]:
            return dict(status=STOPPED, **describe_stop(project, "provider_refused"))
        report = build_operator_preflight(novel_id, repo_root=repo_root, model_selection=MODEL,
            provider_selection=PROVIDER_COMMAND_CODE, max_api_calls=PHYSICAL_CALL_LIMIT,
            max_credit_equivalent_usd=remaining)
        status = report["status"]
        if status == "COMPLETE":
            return dict(status=COMPLETE, message="The novel is fully translated.", action="")
        if status == "PAUSED":
            return dict(status=PAUSED, message="The novel is paused.", action="Remove the pause to continue.")
        try:
            if status == "CLEANUP_READY":
                _run_cleanup(novel_id, repo_root)
                continue
            if status == "RECOVERY_READY":
                outcome = _run_pending_recovery(novel_id, repo_root, remaining)
            elif status == "GOAT_SATURATION_RECOVERY_READY":
                outcome = _run_saturation_recovery(novel_id, repo_root, remaining)
            elif status == "BOUNDED_GOAT_READY":
                outcome = _run_production(project, novel_id, repo_root, remaining)
            else:
                blockers = report.get("errors") or report.get("blockers") or []
                return dict(status=STOPPED, reason="preflight_blocked", blockers=blockers[:10],
                            message="The novel can't safely continue yet.",
                            action="See the listed blockers.")
        except ProductionRunLockedError:
            return dict(status=STOPPED, **describe_stop(project, "writer_busy"))
        if outcome.get("status") == "ALLOWANCE_EXHAUSTED":
            return dict(status=BUDGET_REACHED, message="The rest of the budget is too small for the next request.",
                        action=f"Add budget to continue. A run keeps up to ${REQUEST_SET_ASIDE_USD:.2f} aside for each request. "
                               "Most of it isn't spent.")
        if _trial_is_finished(project):
            return dict(status=TRIAL_FINISHED, message="The trial chapters are ready to read.",
                        action="Start again to continue the novel.")
        progress = _progress(project)
        state = (progress["last_completed"], progress.get("last_audited_chapter", 0))
        runs_without_progress = runs_without_progress + 1 if state == last_state else 0
        last_state = state
        if runs_without_progress >= MAX_NO_PROGRESS_RUNS:
            return dict(status=STOPPED, run_status=outcome.get("status"), stop_exception=outcome.get("stop_exception"),
                        **describe_stop(project))
        time.sleep(1)
    return dict(status=STOPPED, reason="too_many_restarts", message="The run restarted too many times.",
                action="See the latest file in this novel's logs folder.")
