"""Read-only operator status summary. Makes zero API calls."""

import argparse
import json
from datetime import datetime, timezone

from noveltranslator_core import inspect_pending_state
from noveltranslator_observability import build_status_summary, recent_chapter_metrics
from novel_project import select_project
from translation_profile import load_translation_profile


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def format_optional(value, template):
    return "not available" if value is None else template.format(value)


def show_status(project, now=None):
    from production_accounting import GOAT_METADATA, accounting_report_lines
    profile = load_translation_profile(
        project.config.profile, project.repository_root
    )
    progress = load_json(project.progress_file)
    goat_active = progress.get("provider") == GOAT_METADATA["provider"]
    accounting_lines = accounting_report_lines(progress)
    glossary = load_json(project.glossary_file)
    pending_report = inspect_pending_state(
        project.pending_dir,
        progress.get("completed", []),
        progress.get("next_chapter"),
    )
    summary = build_status_summary(
        progress=progress,
        glossary_count=len(glossary.get("entries", {})),
        pending_items=pending_report["pending"],
        final_chapter=project.config.final_chapter,
        audit_interval=project.config.audit_interval,
        manual_pause_active=project.pause_file.exists(),
        now=now or datetime.now(timezone.utc),
        recent_metrics=recent_chapter_metrics(project.logs_dir,
            **(dict(accounting_metadata=GOAT_METADATA,
                    start_chapter=progress.get("active_accounting_epoch", {}).get("start_chapter"))
               if goat_active else {})),
    )

    from historical_reconciliation import publication_freshness
    try:
        freshness = publication_freshness(project)
    except (OSError, ValueError, RuntimeError):
        freshness = {"status": "NOT_VERIFIED", "reason_code": "HISTORY_OR_FRESHNESS_PROOF_INVALID"}
    print(f"Publication freshness: {freshness['status']} ({freshness['reason_code']})")
    print("NovelTranslator status (zero API calls)")
    print("=" * 42)
    print(f"Project: {project.config.id} ({project.config.title})")
    print(f"Profile: {profile.id} ({profile.display_name})")
    print(f"Project root: {project.root}")
    print(f"Completed chapter: {summary['completed_chapter']}")
    print(f"Next chapter: {summary['next_chapter']}")
    print(f"Complete: {summary['percentage_complete']:.2f}%")
    if summary["pending"]:
        pending_text = ", ".join(
            f"Chapter {item['chapter']} ({item['stage']})" for item in summary["pending"]
        )
    else:
        pending_text = "none"
    print(f"Pending: {pending_text}")
    print(f"Last audited chapter: {summary['last_audited_chapter']}")
    print(f"Next audit chapter: {summary['next_audit_chapter']}")
    print(f"Glossary entries: {summary['glossary_entry_count']}")
    print(f"Total prompt tokens: {summary['total_prompt_tokens']:,}")
    print(f"Total output tokens: {summary['total_completion_tokens']:,}")
    if accounting_lines:
        for line in accounting_lines:
            print(line)
    else:
        print(f"Rate-correct cost: ${summary['rate_correct_cost_usd']:.6f}")
    print(("Average recent active GOAT chapter cost (USD credit-equivalent): "
           if goat_active else "Average recent chapter cost: ") + format_optional(
        summary["average_recent_chapter_cost"], "${:.6f}"
    ))
    print("Average recent chapter time: " + format_optional(
        summary["average_recent_chapter_seconds"], "{:.2f} seconds"
    ))
    print(f"Recent log sample: {summary['recent_sample_size']} chapters")
    print(f"Estimated remaining chapters: {summary['remaining_chapters']}")
    print(f"Manual pause active: {'yes' if summary['manual_pause_active'] else 'no'}")
    print(f"Current UTC: {summary['current_utc']}")
    print(f"Current pricing period: {summary['pricing_period']}")
    if pending_report["errors"]:
        print("Recovery warnings:")
        for error in pending_report["errors"]:
            print(f"  {error}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Show read-only status for one NovelTranslator project"
    )
    parser.add_argument(
        "--novel",
        help="novel identifier under novels/; omit for the legacy root project",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return show_status(select_project(args.novel))


if __name__ == "__main__":
    raise SystemExit(main())
