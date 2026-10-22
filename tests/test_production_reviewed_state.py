from public_test_support import supervised_callback_source
from pathlib import Path

import pytest

from noveltranslator_core import replace_chapter_issues
from production_reviewed_state import finalize_reviewed_chapter_state


def _changes():
    return {
        "added": ["甲 → A"],
        "rejected": [{"chinese": "旧", "reason": "reviewer rejected"}],
        "corrected": [],
        "contextualized": [],
        "merged": [],
    }


def test_finalizer_preserves_reviewed_state_order_and_issue_shapes():
    continuity = {"latest_chapter": 4, "summary": "old"}
    issues_data = {
        "issues": [
            {"chapter": 4, "type": "model_flag", "details": "keep"},
            {"chapter": 5, "type": "model_flag", "details": "stale"},
        ]
    }
    eligible = [{"candidate_id": "source:a", "chinese": "甲"}]
    review = {
        "decisions": [{"candidate_id": "source:a", "action": "accept"}],
        "issues": ["review issue"],
    }
    state = {
        "continuity_summary": "new summary",
        "issues": ["model issue 1", "model issue 2"],
    }
    qa = {"issues": ["qa issue 1", "qa issue 2"]}
    prefilter = [{"chinese": "低", "reason": "low value"}]
    observed = {}

    def apply_term_review(chapter_number, candidates, review_result):
        observed["chapter"] = chapter_number
        observed["candidates"] = candidates
        observed["review_result"] = review_result
        observed["continuity_before"] = dict(continuity)
        observed["issues_before"] = list(issues_data["issues"])
        return _changes()

    result = finalize_reviewed_chapter_state(
        chapter_number=5,
        eligible_candidates=eligible,
        review_result=review,
        prefilter_rejected=prefilter,
        state=state,
        qa=qa,
        continuity=continuity,
        issues_data=issues_data,
        apply_term_review=apply_term_review,
        replace_chapter_issues=replace_chapter_issues,
    )

    assert observed == {
        "chapter": 5,
        "candidates": eligible,
        "review_result": review,
        "continuity_before": {"latest_chapter": 4, "summary": "old"},
        "issues_before": [
            {"chapter": 4, "type": "model_flag", "details": "keep"},
            {"chapter": 5, "type": "model_flag", "details": "stale"},
        ],
    }
    assert result["rejected"] == [
        {"chinese": "旧", "reason": "reviewer rejected"},
        {"chinese": "低", "reason": "low value"},
    ]
    assert continuity == {
        "latest_chapter": 5,
        "summary": "new summary",
    }
    assert issues_data["issues"] == [
        {"chapter": 4, "type": "model_flag", "details": "keep"},
        {"chapter": 5, "type": "model_flag", "details": "model issue 1"},
        {"chapter": 5, "type": "model_flag", "details": "model issue 2"},
        {
            "chapter": 5,
            "type": "glossary_reviewer",
            "details": "review issue",
        },
        {
            "chapter": 5,
            "type": "local_qa",
            "details": ["qa issue 1", "qa issue 2"],
        },
    ]


def test_reviewed_state_replay_is_idempotent_for_continuity_and_issues():
    continuity = {"latest_chapter": 6, "summary": "before"}
    issues_data = {
        "issues": [
            {"chapter": 6, "type": "model_flag", "details": "keep"},
            {"chapter": 7, "type": "model_flag", "details": "old"},
        ]
    }

    def apply_term_review(*_args):
        return _changes()

    kwargs = dict(
        chapter_number=7,
        eligible_candidates=[],
        review_result={"decisions": [], "issues": ["review"]},
        prefilter_rejected=[],
        state={"continuity_summary": "same", "issues": ["model"]},
        qa={"issues": ["qa"]},
        continuity=continuity,
        issues_data=issues_data,
        apply_term_review=apply_term_review,
        replace_chapter_issues=replace_chapter_issues,
    )

    first = finalize_reviewed_chapter_state(**kwargs)
    first_continuity = dict(continuity)
    first_issues = list(issues_data["issues"])

    second = finalize_reviewed_chapter_state(**kwargs)

    assert first == second
    assert continuity == first_continuity
    assert issues_data["issues"] == first_issues
    assert [item["chapter"] for item in issues_data["issues"]] == [6, 7, 7, 7]


def test_empty_reviewed_issues_remove_stale_chapter_issue_records():
    continuity = {"latest_chapter": 8, "summary": "old"}
    issues_data = {
        "issues": [
            {"chapter": 8, "type": "model_flag", "details": "keep"},
            {"chapter": 9, "type": "local_qa", "details": ["stale"]},
        ]
    }

    finalize_reviewed_chapter_state(
        chapter_number=9,
        eligible_candidates=[],
        review_result={"decisions": [], "issues": []},
        prefilter_rejected=[],
        state={"continuity_summary": "", "issues": []},
        qa={"issues": []},
        continuity=continuity,
        issues_data=issues_data,
        apply_term_review=lambda *_args: _changes(),
        replace_chapter_issues=replace_chapter_issues,
    )

    assert continuity == {"latest_chapter": 9, "summary": ""}
    assert issues_data["issues"] == [
        {"chapter": 8, "type": "model_flag", "details": "keep"}
    ]


def test_glossary_policy_failure_occurs_before_continuity_or_issue_mutation():
    continuity = {"latest_chapter": 10, "summary": "before"}
    issues_data = {
        "issues": [
            {"chapter": 10, "type": "model_flag", "details": "keep"}
        ]
    }
    before_continuity = dict(continuity)
    before_issues = list(issues_data["issues"])

    def fail_policy(*_args):
        raise RuntimeError("synthetic glossary policy failure")

    with pytest.raises(RuntimeError, match="synthetic glossary policy failure"):
        finalize_reviewed_chapter_state(
            chapter_number=11,
            eligible_candidates=[],
            review_result={"decisions": [], "issues": ["review"]},
            prefilter_rejected=[],
            state={"continuity_summary": "new", "issues": ["model"]},
            qa={"issues": ["qa"]},
            continuity=continuity,
            issues_data=issues_data,
            apply_term_review=fail_policy,
            replace_chapter_issues=replace_chapter_issues,
        )

    assert continuity == before_continuity
    assert issues_data["issues"] == before_issues


def test_issue_replacement_runs_after_continuity_update():
    continuity = {"latest_chapter": 11, "summary": "before"}
    issues_data = {"issues": []}
    observed = {}

    def replace(existing, chapter_number, new_items):
        observed["continuity"] = dict(continuity)
        observed["existing"] = list(existing)
        observed["chapter"] = chapter_number
        observed["new_items"] = list(new_items)
        return list(new_items)

    finalize_reviewed_chapter_state(
        chapter_number=12,
        eligible_candidates=[],
        review_result={"decisions": [], "issues": []},
        prefilter_rejected=[],
        state={"continuity_summary": "after", "issues": ["model"]},
        qa={"issues": []},
        continuity=continuity,
        issues_data=issues_data,
        apply_term_review=lambda *_args: _changes(),
        replace_chapter_issues=replace,
    )

    assert observed["continuity"] == {
        "latest_chapter": 12,
        "summary": "after",
    }
    assert observed["existing"] == []
    assert observed["chapter"] == 12
    assert observed["new_items"] == [
        {"chapter": 12, "type": "model_flag", "details": "model"}
    ]


def test_legacy_process_orders_reviewed_state_before_durable_commit():
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        repo_root
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    source = supervised_callback_source(source, "process_chapter", {
        'state_checkpoint': ('state', 'run_state_extraction_checkpoint'),
        'glossary_changes': ('reviewed_state', 'finalize_reviewed_chapter_state'),
        'chapter_stats': ('chapter_commit', 'commit_reviewed_chapter'),
    })
    process = source[source.index("def process_chapter("):]

    state = process.index(
        "state_checkpoint = run_state_extraction_checkpoint("
    )
    reviewed = process.index(
        "glossary_changes = finalize_reviewed_chapter_state("
    )
    commit = process.index("chapter_stats = commit_reviewed_chapter(")

    assert state < reviewed < commit

    reviewed_block = process[reviewed:commit]
    assert "apply_term_review=apply_term_review" in reviewed_block
    assert "replace_chapter_issues=replace_chapter_issues" in reviewed_block
