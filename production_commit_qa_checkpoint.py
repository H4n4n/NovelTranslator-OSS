"""Deterministic commit-QA checkpoint for one repaired pending chapter.

This module owns only the checkpoint protocol after all local translation
repairs have finished and before any state extraction or glossary work may run.

The caller retains linguistic QA policy through ``qa_checker``, including all
source-script, locked-terminology, profile, and committed-QA field semantics.
The checkpoint additionally owns provider-completion invariants that must remain
true for both newly generated and recovered pending translations: a candidate
must contain non-empty text and its durable finish reason must be exactly
``stop`` before downstream state/glossary work can begin.

The helper preserves the established checkpoint order:

1. run the existing commit-QA callback;
2. attach that raw QA result to the current metadata object in memory;
3. require the caller's established commit decision;
4. apply provider-completion hard blockers without imposing any fixed length;
5. replace the in-memory QA record only when the provider gate adds evidence;
6. if commit is blocked, invalidate downstream pending work through the
   existing reset callback;
7. persist exactly one metadata checkpoint for either pass or block;
8. return the QA result, resulting metadata, and commit decision.

If the caller returns malformed QA without ``commit_allowed``, step 3 raises as
it historically did: the current metadata object has the raw QA attached in
memory, but nothing is persisted.

It performs no provider calls and owns no filesystem implementation; persistence
is supplied by the caller.
"""


def _provider_completion_gate(translation, finish_reason):
    blockers = []
    if finish_reason != "stop":
        blockers.append({
            "type": "unexpected_finish_reason",
            "expected": "stop",
            "observed": finish_reason,
        })
    if not isinstance(translation, str) or not translation.strip():
        blockers.append({
            "type": "empty_translation",
        })
    return {
        "commit_allowed": not blockers,
        "blockers": blockers,
    }


def _with_provider_completion_gate(qa, translation, finish_reason):
    """Overlay only provider-completion blockers on an established QA result."""
    # Read first inside this helper so malformed QA remains fail-closed. The
    # caller attaches raw QA to current metadata before entering this helper,
    # preserving the legacy in-memory mutation order.
    qa_commit_allowed = qa["commit_allowed"]
    gate = _provider_completion_gate(translation, finish_reason)
    if gate["commit_allowed"]:
        return qa, qa_commit_allowed

    effective = dict(qa)
    blockers = [
        dict(item)
        for item in qa.get("commit_blockers", [])
        if isinstance(item, dict)
    ]
    blockers.extend(dict(item) for item in gate["blockers"])
    issues = list(qa.get("issues", []))
    for blocker in gate["blockers"]:
        if blocker["type"] == "unexpected_finish_reason":
            message = (
                "Provider completion blocks commit: expected finish_reason "
                f"'stop', found {blocker.get('observed')!r}"
            )
        else:
            message = "Provider completion blocks commit: translation is empty"
        if message not in issues:
            issues.append(message)

    effective["provider_completion_gate"] = gate
    effective["commit_blockers"] = blockers
    effective["issues"] = issues
    effective["commit_allowed"] = False
    return effective, False


def run_commit_qa_checkpoint(
    *,
    chapter_number,
    source_text,
    translation,
    finish_reason,
    meta,
    qa_checker,
    reset_failed_meta,
    persist_meta,
):
    """Run and persist the post-repair hard commit-QA checkpoint."""

    raw_qa = qa_checker(
        chapter_number,
        source_text,
        translation,
        finish_reason,
    )

    # Preserve the historical mutation order exactly. A malformed QA object can
    # still raise on commit_allowed below, but current in-memory metadata has the
    # raw QA attached and no persistence has occurred yet.
    meta["local_qa"] = raw_qa
    qa, commit_allowed = _with_provider_completion_gate(
        raw_qa,
        translation,
        finish_reason,
    )
    if qa is not raw_qa:
        meta["local_qa"] = qa

    if not commit_allowed:
        meta = reset_failed_meta(meta, qa)

    persist_meta(meta)

    return {
        "qa": qa,
        "meta": meta,
        "commit_allowed": commit_allowed,
    }
