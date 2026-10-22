"""Shared provider-client-agnostic unattended production lifecycle sequencing.

This module owns only lifecycle order and presentation around already-supplied
callbacks. It does not import a provider client, load credentials, translate
chapters, inspect pending files directly, or write project state itself.

The legacy production module keeps its public callable boundary and supplies
the live runtime state/callbacks at call time. That preserves controlled-trial
identity checks while moving detailed startup/audit/chapter-loop sequencing
behind a reusable engine boundary.
"""

from production_failure_classification import ProductionControlError, ProductionIntegrityError


def run_due_audits(*, progress, audit_interval, run_periodic_audit):
    """Run every complete audit interval currently due."""

    if type(audit_interval) is not int or audit_interval < 1:
        raise ProductionControlError("audit interval must be a positive integer")

    while (
        progress["last_completed"] - progress["last_audited_chapter"]
        >= audit_interval
    ):
        start_chapter = progress["last_audited_chapter"] + 1
        end_chapter = progress["last_audited_chapter"] + audit_interval
        run_periodic_audit(start_chapter, end_chapter)
        if progress["last_audited_chapter"] != end_chapter:
            raise ProductionIntegrityError("due audit did not commit its exact boundary")


def run_final_audit_if_needed(
    *,
    progress,
    final_chapter,
    run_periodic_audit,
):
    """Run one final partial audit only after the configured novel is complete."""

    if progress["last_completed"] < final_chapter:
        return
    if progress["last_audited_chapter"] >= final_chapter:
        return
    run_periodic_audit(
        progress["last_audited_chapter"] + 1,
        final_chapter,
    )
    if progress["last_audited_chapter"] != final_chapter:
        raise ProductionIntegrityError("final audit did not commit its exact boundary")


def run_unattended_lifecycle(
    *,
    progress,
    glossary,
    final_chapter,
    auto_off_peak_only,
    manual_pause_file,
    prices,
    run_log_path,
    reconcile_stale_completed_pending,
    perform_startup_self_check,
    reconcile_partial_commit_before_audits,
    run_due_audits_callback,
    run_final_audit_if_needed_callback,
    process_chapter,
    log_event,
    log_error,
    describe_api_error,
    handled_error_types,
    describe_local_error,
    local_error_types,
    print_fn,
    target_chapter=None,
):
    """Run production sequencing, optionally bounded to one exact target.

    Provider calls and project mutations may occur only through the supplied
    callbacks. This function itself owns no provider or filesystem primitive.
    """

    print_fn("\n" + "=" * 70)
    print_fn("DEEPSEEK NOVEL TRANSLATION — UNATTENDED PRODUCTION")
    print_fn("=" * 70)
    print_fn(f"Final chapter: {final_chapter}")
    print_fn(
        "Automatic off-peak-only mode: "
        + ("ON" if auto_off_peak_only else "OFF")
    )
    print_fn(f"Manual pause file: {manual_pause_file}")
    print_fn(
        "V4 Pro OFF-PEAK / 1M: "
        f"${prices['off_peak']['cache_hit']:g} cache hit | "
        f"${prices['off_peak']['cache_miss']:g} cache miss | "
        f"${prices['off_peak']['output']:g} output"
    )
    print_fn(
        "V4 Pro PEAK / 1M: "
        f"${prices['peak']['cache_hit']:g} cache hit | "
        f"${prices['peak']['cache_miss']:g} cache miss | "
        f"${prices['peak']['output']:g} output"
    )
    print_fn(f"Completed through: {progress['last_completed']}")
    print_fn(f"Next chapter: {progress['next_chapter']}")
    print_fn(f"Last audited chapter: {progress['last_audited_chapter']}")
    print_fn(f"Current glossary entries: {len(glossary['entries'])}")
    print_fn("=" * 70)
    log_event(
        "program_start",
        stage="startup",
        last_completed=progress["last_completed"],
        next_chapter=progress["next_chapter"],
        auto_off_peak_only=auto_off_peak_only,
        log_file=run_log_path,
    )

    try:
        reconcile_stale_completed_pending()
        perform_startup_self_check()
        reconcile_partial_commit_before_audits()
        run_due_audits_callback()

        start_chapter = progress["next_chapter"]
        if target_chapter is None:
            end_chapter = final_chapter
        else:
            if (
                not isinstance(target_chapter, int)
                or isinstance(target_chapter, bool)
                or target_chapter < 1
                or target_chapter > final_chapter
            ):
                raise ProductionControlError(
                    "bounded production target chapter is invalid"
                )
            if start_chapter != target_chapter:
                raise ProductionControlError(
                    "controlled trial start chapter changed after approval"
                )
            end_chapter = target_chapter

        if start_chapter <= final_chapter:
            for chapter_number in range(
                start_chapter,
                end_chapter + 1,
            ):
                if (progress["next_chapter"] != chapter_number
                    or progress["last_completed"] != chapter_number - 1):
                    raise ProductionIntegrityError("chapter chronology changed before dispatch")
                if not process_chapter(chapter_number):
                    print_fn("\n" + "=" * 70)
                    print_fn(
                        f"STOPPED SAFELY AT CHAPTER {chapter_number}"
                    )
                    print_fn(
                        f"Last committed: {progress['last_completed']}"
                    )
                    print_fn(f"Next chapter: {progress['next_chapter']}")
                    print_fn("=" * 70)
                    log_event(
                        "shutdown",
                        stage="chapter_processing",
                        reason="chapter_failed",
                        chapter=chapter_number,
                    )
                    raise SystemExit(1)
                if (progress["last_completed"] != chapter_number
                    or progress["next_chapter"] != chapter_number + 1):
                    raise ProductionIntegrityError("chapter callback returned before durable commit")
                run_due_audits_callback()

        run_final_audit_if_needed_callback()

    except KeyboardInterrupt:
        log_event(
            "shutdown",
            stage="operator",
            reason="keyboard_interrupt",
        )
        print_fn("\n" + "=" * 70)
        print_fn("MANUAL STOP REQUESTED")
        print_fn("=" * 70)
        print_fn(
            f"Last committed chapter: {progress['last_completed']}"
        )
        print_fn(f"Next chapter: {progress['next_chapter']}")
        print_fn(
            "Any already-saved pending stage will be reused "
            "when the script runs again."
        )
        print_fn(
            "Safest planned stop: create pause.flag, wait for "
            "'MANUAL PAUSE ACTIVE', then press Ctrl+C."
        )
        print_fn("=" * 70)
        raise SystemExit(0)

    except handled_error_types as exc:
        log_error(exc, stage="production_provider")
        log_event(
            "shutdown",
            stage="production",
            reason="provider_error",
        )
        print_fn("\n" + "=" * 70)
        print_fn("UNATTENDED PRODUCTION STOPPED SAFELY")
        print_fn("=" * 70)
        print_fn(f"Provider/request error: {exc}\n")
        print_fn(describe_api_error(exc))
        print_fn(
            f"\nLast committed chapter: "
            f"{progress['last_completed']}"
        )
        print_fn(f"Next chapter: {progress['next_chapter']}")
        print_fn(
            "Run this same script again after resolving the problem."
        )
        print_fn("=" * 70)
        raise SystemExit(1)

    except local_error_types as exc:
        log_error(exc, stage="production_local")
        log_event(
            "shutdown",
            stage="production",
            reason="local_error",
        )
        print_fn("\n" + "=" * 70)
        print_fn("UNATTENDED PRODUCTION STOPPED SAFELY")
        print_fn("=" * 70)
        print_fn(f"Local/recovery error: {exc}\n")
        print_fn(describe_local_error(exc))
        print_fn(
            f"\nLast committed chapter: "
            f"{progress['last_completed']}"
        )
        print_fn(f"Next chapter: {progress['next_chapter']}")
        print_fn(
            "Run the same guarded workflow again only after resolving "
            "the reported local problem."
        )
        print_fn("=" * 70)
        raise SystemExit(1)

    except Exception as exc:
        log_error(
            exc,
            stage="production_unexpected",
        )
        log_event(
            "shutdown",
            stage="production",
            reason="unexpected_error",
        )
        raise

    print_fn("\n" + "=" * 70)
    print_fn("NOVEL TRANSLATION COMPLETE")
    print_fn("=" * 70)
    print_fn(
        f"Completed through Chapter {progress['last_completed']}"
    )
    print_fn(
        f"Last audited chapter: {progress['last_audited_chapter']}"
    )
    print_fn(
        f"Total input tokens: {progress['total_prompt_tokens']:,}"
    )
    print_fn(
        f"Total output tokens: "
        f"{progress['total_completion_tokens']:,}"
    )
    from production_accounting import accounting_report_lines
    accounting_lines = accounting_report_lines(progress)
    if accounting_lines:
        for line in accounting_lines:
            print_fn(line)
    else:
        print_fn(
            "Rate-correct API cost tracked since "
            f"Chapter {progress['rate_correct_cost_start_chapter']}: "
            f"${progress['rate_correct_cost_usd']:.6f}"
        )
        print_fn(
            "Legacy cumulative project counter: "
            f"${progress['estimated_cost_usd']:.6f}"
        )
    print_fn(
        f"Final glossary entries: {len(glossary['entries'])}"
    )
    print_fn("=" * 70)
    log_event(
        "shutdown",
        stage="complete",
        reason="translation_complete",
    )
