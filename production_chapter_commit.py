"""Durable persistence protocol for one fully reviewed chapter.

This module owns only the final commit ordering after translation, commit QA,
state extraction, and glossary review have already succeeded. It does not call
providers, generate text, inspect source material, or decide glossary policy.

Normal commit ordering is intentionally conservative:
1. persist committed QA evidence;
2. combine already-recorded stage accounting;
3. replace the final chapter text;
4. stage chapter accounting on a detached progress copy;
5. persist associated glossary/continuity/issues/audit state;
6. persist progress.json as the final durable commit marker;
7. publish in-memory progress and remove pending only after that marker succeeds.

For a proven reviewed partial commit where QA and final text are already durable
and byte-identical to pending evidence, reconciliation starts at step 4 and
never rewrites those already-proven artifacts.

Reject an already-accounted chapter before any commit-tail write.

Callers supply the established atomic writers and accounting helpers so model
provenance wrappers and existing runtime behavior remain unchanged.
"""


from copy import deepcopy


def _commit_associated_state_and_progress(
    *,
    chapter_number,
    chapter_stats,
    progress,
    glossary,
    continuity,
    issues_data,
    audit_history,
    glossary_file,
    continuity_file,
    issues_file,
    audit_history_file,
    progress_file,
    pending_file,
    pending_meta_file,
    save_json,
    commit_chapter_accounting,
):
    """Persist the replay-safe commit tail with progress as final marker."""

    if chapter_number in progress.get("completed", []):
        raise RuntimeError(
            f"Chapter {chapter_number} was already accounted before commit"
        )

    staged_progress = deepcopy(progress)
    if not commit_chapter_accounting(
        staged_progress,
        chapter_number,
        chapter_stats,
    ):
        raise RuntimeError(
            f"Chapter {chapter_number} was already accounted before commit"
        )

    save_json(glossary_file, glossary)
    save_json(continuity_file, continuity)
    save_json(issues_file, issues_data)
    save_json(audit_history_file, audit_history)

    # progress.json remains the final durable commit marker. It advances only
    # after every associated persistent state file has been replaced.
    save_json(progress_file, staged_progress)
    progress.clear()
    progress.update(staged_progress)

    if pending_file.exists():
        pending_file.unlink()
    if pending_meta_file.exists():
        pending_meta_file.unlink()


def commit_reviewed_chapter(
    *,
    chapter_number,
    translation,
    qa,
    translation_stats,
    state_stats,
    review_stats,
    progress,
    glossary,
    continuity,
    issues_data,
    audit_history,
    final_file,
    qa_file,
    glossary_file,
    continuity_file,
    issues_file,
    audit_history_file,
    progress_file,
    pending_file,
    pending_meta_file,
    combine_stats,
    atomic_write_text,
    save_json,
    commit_chapter_accounting,
):
    """Persist one reviewed chapter using the established durable commit order."""

    if chapter_number in progress.get("completed", []):
        raise RuntimeError(
            f"Chapter {chapter_number} was already accounted before commit"
        )

    save_json(qa_file, qa)

    chapter_stats = combine_stats(
        translation_stats,
        state_stats,
        review_stats,
    )

    atomic_write_text(final_file, translation)

    _commit_associated_state_and_progress(
        chapter_number=chapter_number,
        chapter_stats=chapter_stats,
        progress=progress,
        glossary=glossary,
        continuity=continuity,
        issues_data=issues_data,
        audit_history=audit_history,
        glossary_file=glossary_file,
        continuity_file=continuity_file,
        issues_file=issues_file,
        audit_history_file=audit_history_file,
        progress_file=progress_file,
        pending_file=pending_file,
        pending_meta_file=pending_meta_file,
        save_json=save_json,
        commit_chapter_accounting=commit_chapter_accounting,
    )

    return chapter_stats


def commit_reconciled_reviewed_chapter_tail(
    *,
    chapter_number,
    translation_stats,
    state_stats,
    review_stats,
    progress,
    glossary,
    continuity,
    issues_data,
    audit_history,
    final_file,
    pending_file,
    pending_meta_file,
    glossary_file,
    continuity_file,
    issues_file,
    audit_history_file,
    progress_file,
    combine_stats,
    save_json,
    commit_chapter_accounting,
):
    """Finish a proven partial commit without rewriting final text or QA."""

    if chapter_number in progress.get("completed", []):
        raise RuntimeError(
            f"Chapter {chapter_number} was already accounted before commit"
        )
    if not final_file.is_file() or not pending_file.is_file():
        raise RuntimeError(
            "reviewed partial commit requires both final and pending chapter files"
        )
    if final_file.read_bytes() != pending_file.read_bytes():
        raise RuntimeError(
            "reviewed partial commit final bytes no longer match pending bytes"
        )

    chapter_stats = combine_stats(
        translation_stats,
        state_stats,
        review_stats,
    )

    _commit_associated_state_and_progress(
        chapter_number=chapter_number,
        chapter_stats=chapter_stats,
        progress=progress,
        glossary=glossary,
        continuity=continuity,
        issues_data=issues_data,
        audit_history=audit_history,
        glossary_file=glossary_file,
        continuity_file=continuity_file,
        issues_file=issues_file,
        audit_history_file=audit_history_file,
        progress_file=progress_file,
        pending_file=pending_file,
        pending_meta_file=pending_meta_file,
        save_json=save_json,
        commit_chapter_accounting=commit_chapter_accounting,
    )

    return chapter_stats
