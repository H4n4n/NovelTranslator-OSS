"""In-memory finalization of one fully reviewed chapter.

This module owns only the deterministic mutation that occurs after glossary
review has already succeeded and before the durable chapter commit begins.
It does not call providers, perform glossary policy itself, or write files.

The existing glossary policy remains supplied by the caller through
`apply_term_review`. This helper preserves the established ordering:
1. apply reviewed glossary decisions;
2. append locally prefiltered rejections to reporting;
3. update continuity to the reviewed chapter;
4. assemble model/reviewer/local-QA issue records;
5. replace that chapter's issue records replay-safely.
"""


def finalize_reviewed_chapter_state(
    *,
    chapter_number,
    eligible_candidates,
    review_result,
    prefilter_rejected,
    state,
    qa,
    continuity,
    issues_data,
    apply_term_review,
    replace_chapter_issues,
):
    """Apply reviewed in-memory state immediately before durable commit."""

    glossary_changes = apply_term_review(
        chapter_number,
        eligible_candidates,
        review_result,
    )
    glossary_changes["rejected"].extend(prefilter_rejected)

    continuity["latest_chapter"] = chapter_number
    continuity["summary"] = state.get("continuity_summary", "")

    chapter_issues = []
    for issue in state.get("issues", []):
        chapter_issues.append(
            {
                "chapter": chapter_number,
                "type": "model_flag",
                "details": issue,
            }
        )
    for issue in review_result.get("issues", []):
        chapter_issues.append(
            {
                "chapter": chapter_number,
                "type": "glossary_reviewer",
                "details": issue,
            }
        )
    if qa["issues"]:
        chapter_issues.append(
            {
                "chapter": chapter_number,
                "type": "local_qa",
                "details": qa["issues"],
            }
        )

    issues_data["issues"] = replace_chapter_issues(
        issues_data["issues"],
        chapter_number,
        chapter_issues,
    )

    return glossary_changes
