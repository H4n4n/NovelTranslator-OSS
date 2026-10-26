import copy
import hashlib
import json
from pathlib import Path
import sys
import types
import uuid

import pytest
import chapter_glossary_review
import glossary_integrity
from glossary_candidate_resolution import (
    candidate_resolution_path,
    create_candidate_resolution,
    pending_content_identity,
    prepare_glossary_review_candidates,
    state_extraction_identity,
)

from chapter_glossary_review import (
    CHAPTER_REVIEW_RECOVERY_KEY,
    ChapterGlossaryCandidateIdentityError,
    ChapterGlossaryReviewFailure,
    ChapterGlossaryReviewPersistenceError,
    build_chapter_review_schema_correction_prompt,
    build_chapter_review_semantic_correction_prompt,
    chapter_review_input_hash,
    chapter_review_merge_targets,
    chapter_review_operation_ids,
    chapter_review_schema_correction_operation_id,
    chapter_review_semantic_retry_operation_id,
    run_chapter_glossary_review,
)
from glossary_integrity import (
    GlossaryCandidateIdentityError,
    canonicalize_glossary_review_candidates,
    normalized_glossary_source_form,
    stable_glossary_candidate_id,
)
from translation_profile import (
    filter_profile_candidates,
    load_translation_profile,
    project_low_value_terms,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

ISSUES_SHAPE_ERRORS = [{
    "code": "invalid_issues_shape",
    "message": "glossary review issues must be an array of strings",
    "retryable": True,
    "field": "issues",
    "allowed_fields": ["issues"],
}]


def candidate(source="term-a", english="Canonical A"):
    return {
        "chinese": source,
        "english": english,
        "category": "title",
        "status": "provisional",
        "candidate_id": stable_glossary_candidate_id(source),
        "source_provenance": {
            "origin": "chapter_state_extraction",
            "chapter": 7,
            "verification": "exact_literal",
        },
    }


def decision(model_id="c01", **overrides):
    item = {
        "candidate_id": model_id,
        "action": "accept",
        "english": "Canonical A",
        "category": "title",
        "notes": "",
    }
    item.update(overrides)
    return item


def response(decisions, *, issues=None, cost=0.1):
    if issues is None:
        issues = []
    return {
        "result": {"decisions": decisions, "issues": issues},
        "stats": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cost_usd": cost,
            "rate_correct_cost_usd": cost,
        },
    }


class Harness:
    def __init__(self, original, corrected=None):
        self.original = original
        self.corrected = corrected
        self.original_calls = []
        self.correction_calls = []
        self.persisted = []

    def request_original(self, candidates, metadata):
        self.original_calls.append((copy.deepcopy(candidates), copy.deepcopy(metadata)))
        return copy.deepcopy(self.original)

    def request_correction(self, parsed, errors, metadata):
        self.correction_calls.append(
            (copy.deepcopy(parsed), copy.deepcopy(errors), copy.deepcopy(metadata))
        )
        if isinstance(self.corrected, Exception):
            raise self.corrected
        if isinstance(self.corrected, tuple):
            index = len(self.correction_calls) - 1
            value = self.corrected[index]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        return copy.deepcopy(self.corrected)

    def persist(self, record):
        self.persisted.append(copy.deepcopy(record))


def run(harness, *, candidates=None, existing=("existing-key",), recovery=None):
    candidates = candidates or [candidate()]
    source = "\n".join(item["chinese"] for item in candidates)
    return run_chapter_glossary_review(
        novel_id="synthetic_novel",
        chapter=7,
        source_text=source,
        translation="Synthetic target text.",
        candidates=candidates,
        existing_source_keys=existing,
        request_original=harness.request_original,
        request_correction=harness.request_correction,
        recovery_record=recovery,
        persist_recovery=harness.persist,
    )


def test_valid_review_uses_short_id_and_needs_no_correction():
    harness = Harness(response([decision()]))
    result = run(harness)
    assert harness.original_calls[0][0][0]["candidate_id"] == "c01"
    assert harness.correction_calls == []
    assert result["review_result"]["decisions"][0]["candidate_id"].startswith(
        "source:"
    )
    assert result["stats"]["request_count"] == 1


@pytest.mark.parametrize(
    "decisions, message",
    [
        ([decision("c99")], "outside this request"),
        ([decision("c1")], "outside this request"),
        (
            [decision(stable_glossary_candidate_id("term-a"))],
            "outside this request",
        ),
        ([], "omitted request-local"),
        ([decision(), decision()], "duplicate model decision"),
        ([decision(), decision("c99")], "outside this request"),
        ([decision(chinese="changed-source")], "rewrote source key"),
    ],
)
def test_identity_failures_are_hard_stops_without_correction(decisions, message):
    harness = Harness(response(decisions))
    with pytest.raises(ChapterGlossaryReviewFailure, match=message):
        run(harness)
    assert harness.correction_calls == []


def test_candidate_source_identity_mismatch_is_a_hard_stop_before_request():
    item = candidate()
    item["candidate_id"] = stable_glossary_candidate_id("different-source")
    harness = Harness(response([decision()]))
    with pytest.raises(ChapterGlossaryReviewFailure, match="does not match"):
        run(harness, candidates=[item])
    assert harness.original_calls == []


def test_exact_duplicate_candidate_is_one_semantic_review_identity():
    item = candidate("刀騎將", "Blade Cavalry Commander")
    harness = Harness(
        response(
            [decision("c01", english="Blade Cavalry Commander", category="class")]
        )
    )
    outcome = run(harness, candidates=[item, copy.deepcopy(item)])

    assert len(harness.original_calls) == 1
    assert len(harness.original_calls[0][0]) == 1
    assert len(outcome["canonical_candidates"]) == 1
    assert outcome["candidate_preparation"]["exact_duplicates_removed"] == 1
    assert outcome["candidate_preparation"]["semantic_duplicates_removed"] == 0
    assert len(outcome["review_result"]["decisions"]) == 1


def test_same_semantic_candidate_with_distinct_notes_collapses_and_keeps_evidence():
    first = candidate("刀騎將", "Blade Cavalry Commander")
    first.update({"category": "class", "notes": "System enemy class."})
    second = copy.deepcopy(first)
    second["notes"] = "Enemy class shown in the interface."
    second["source_provenance"]["span_start"] = 20
    second["source_provenance"]["span_end"] = 23
    harness = Harness(
        response(
            [decision("c01", english="Blade Cavalry Commander", category="class")]
        )
    )

    outcome = run(harness, candidates=[first, second])
    canonical = outcome["canonical_candidates"][0]
    assert outcome["candidate_preparation"]["semantic_duplicates_removed"] == 1
    assert [item["notes"] for item in canonical["candidate_evidence"]] == [
        "System enemy class.",
        "Enemy class shown in the interface.",
    ]


@pytest.mark.parametrize(
    "field, conflicting_value",
    [
        ("english", "Cavalry Blade Commander"),
        ("category", "military rank"),
        ("status", "locked"),
        ("context_identity", "different-concept"),
    ],
)
def test_same_source_semantic_conflict_stops_before_review_request(
    field, conflicting_value
):
    first = candidate("同詞", "Same Proposal")
    second = copy.deepcopy(first)
    second[field] = conflicting_value
    harness = Harness(response([decision()]))

    with pytest.raises(
        ChapterGlossaryCandidateIdentityError,
        match="conflicting candidate proposals",
    ) as caught:
        run(harness, candidates=[first, second])

    assert caught.value.code == "same_source_semantic_conflict"
    assert caught.value.diagnostics["conflict_count"] == 1
    conflict = caught.value.diagnostics["conflicts"][0]
    assert conflict["raw_positions"] == [0, 1]
    assert conflict["differing_semantic_fields"] == [field]
    assert conflict["proposals"][0]["provenance_identity"] == {
        "origin": "chapter_state_extraction",
        "chapter": 7,
        "verification": "exact_literal",
    }
    assert harness.original_calls == []
    assert harness.correction_calls == []


def test_generic_item_and_consumable_category_conflict_remains_fail_closed():
    first = candidate("named-item", "Named Item")
    first.update({
        "category": "consumable",
        "notes": "Structured source metadata identifies a consumable.",
    })
    second = copy.deepcopy(first)
    second.update({
        "category": "item",
        "notes": "A broader item proposal for the same source key.",
    })

    with pytest.raises(GlossaryCandidateIdentityError) as caught:
        canonicalize_glossary_review_candidates([first, second])

    conflict = caught.value.diagnostics["conflicts"][0]
    assert conflict["differing_semantic_fields"] == ["category"]
    assert [item["category"] for item in conflict["proposals"]] == [
        "consumable", "item"
    ]
    assert [item["notes"] for item in conflict["proposals"]] == [
        "Structured source metadata identifies a consumable.",
        "A broader item proposal for the same source key.",
    ]


def test_repeated_korean_term_uses_one_shared_semantic_identity():
    first = candidate("마나 심장", "Mana Heart")
    first["notes"] = "Recurring progression term."
    second = copy.deepcopy(first)
    second["notes"] = "Repeated term from the same chapter."
    harness = Harness(response([decision("c01", english="Mana Heart")]))

    outcome = run(harness, candidates=[first, second])

    assert len(outcome["canonical_candidates"]) == 1
    assert outcome["canonical_candidates"][0]["chinese"] == "마나 심장"
    assert len(harness.original_calls[0][0]) == 1


def test_duplicate_alias_and_source_variant_evidence_is_deterministically_unioned():
    first = candidate("別名", "Alias")
    first["aliases"] = ["別稱"]
    first["source_variants"] = ["別名"]
    second = copy.deepcopy(first)
    second["aliases"] = ["別稱", "異名"]
    second["source_variants"] = ["異體別名"]

    prepared = canonicalize_glossary_review_candidates([first, second])
    canonical = prepared["candidates"][0]

    assert canonical["aliases"] == ["別稱", "異名"]
    assert canonical["source_variants"] == ["別名", "異體別名"]
    assert prepared["diagnostics"]["semantic_duplicates_removed"] == 1


def test_candidate_order_and_ids_are_stable_across_reconstruction():
    raw = [
        candidate("甲", "A"),
        candidate("乙", "B"),
        copy.deepcopy(candidate("甲", "A")),
        candidate("병사", "Soldier"),
    ]

    first = canonicalize_glossary_review_candidates(copy.deepcopy(raw))
    second = canonicalize_glossary_review_candidates(copy.deepcopy(raw))

    assert first == second
    assert [item["chinese"] for item in first["candidates"]] == [
        "甲",
        "乙",
        "병사",
    ]
    assert len(
        {item["candidate_id"] for item in first["candidates"]}
    ) == 3


def test_traditional_and_simplified_exact_source_keys_remain_distinct():
    traditional = candidate("後臺", "Backstage")
    simplified = candidate("后台", "Backstage")
    prepared = canonicalize_glossary_review_candidates(
        [traditional, simplified]
    )

    assert [item["chinese"] for item in prepared["candidates"]] == [
        "後臺",
        "后台",
    ]
    assert len({item["candidate_id"] for item in prepared["candidates"]}) == 2


def test_unicode_normalization_is_diagnostic_not_an_exact_source_key_collapse():
    decomposed = candidate("e\u0301", "Acute E")
    composed = candidate("é", "Acute E")
    prepared = canonicalize_glossary_review_candidates([decomposed, composed])

    assert normalized_glossary_source_form("e\u0301") == "é"
    assert len(prepared["candidates"]) == 2
    assert len({item["candidate_id"] for item in prepared["candidates"]}) == 2


def test_equivalent_proposal_formatting_normalizes_without_changing_source_identity():
    first = candidate("軍階", "Cafe\u0301 Rank")
    first.update({"category": "military_rank", "status": "PROVISIONAL"})
    second = copy.deepcopy(first)
    second.update({"english": "Café Rank", "category": "military-rank"})

    prepared = canonicalize_glossary_review_candidates([first, second])

    assert len(prepared["candidates"]) == 1
    assert prepared["candidates"][0]["chinese"] == "軍階"
    assert prepared["diagnostics"]["semantic_duplicates_removed"] == 1


def test_surrounding_punctuation_and_korean_spacing_are_not_over_normalized():
    prepared = canonicalize_glossary_review_candidates(
        [
            candidate("마나심장", "Mana Heart"),
            candidate("마나 심장", "Mana Heart"),
            candidate("《마나심장》", "Mana Heart"),
        ]
    )

    assert len(prepared["candidates"]) == 3
    assert len({item["candidate_id"] for item in prepared["candidates"]}) == 3


def test_only_surrounding_source_whitespace_is_safely_normalized():
    prepared = canonicalize_glossary_review_candidates(
        [candidate("  術語  ", "Term"), candidate("術語", "Term")]
    )

    assert len(prepared["candidates"]) == 1
    assert prepared["candidates"][0]["chinese"] == "術語"


def test_distinct_source_keys_with_forced_digest_collision_fail_closed(monkeypatch):
    raw = [
        {key: value for key, value in candidate("甲", "A").items() if key != "candidate_id"},
        {key: value for key, value in candidate("乙", "B").items() if key != "candidate_id"},
    ]
    monkeypatch.setattr(
        glossary_integrity,
        "stable_glossary_candidate_id",
        lambda source_key: "source:forced-collision",
    )

    with pytest.raises(
        GlossaryCandidateIdentityError,
        match="distinct glossary source keys",
    ) as caught:
        canonicalize_glossary_review_candidates(raw)

    assert caught.value.code == "candidate_id_collision"
    assert caught.value.diagnostics["conflict_count"] == 1


def test_candidate_without_exact_source_evidence_is_a_hard_stop():
    item = candidate()
    harness = Harness(response([decision()]))
    with pytest.raises(ChapterGlossaryReviewFailure, match="source evidence"):
        run_chapter_glossary_review(
            novel_id="synthetic_novel",
            chapter=7,
            source_text="different source",
            translation="target",
            candidates=[item],
            existing_source_keys=(),
            request_original=harness.request_original,
            request_correction=harness.request_correction,
        )
    assert harness.original_calls == []


def test_later_identity_injection_cannot_hide_behind_retryable_action():
    items = [candidate("term-a", "A"), candidate("term-b", "B")]
    harness = Harness(
        response([decision("c01", action=""), decision("c99", english="B")])
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="outside this request"):
        run(harness, candidates=items)
    assert harness.correction_calls == []


@pytest.mark.parametrize(
    "result, message",
    [
        ({"decisions": "not-an-array", "issues": []}, "decisions must be an array"),
    ],
)
def test_structural_schema_failures_stop_without_semantic_retry(result, message):
    harness = Harness({"result": result, "stats": {}})
    with pytest.raises(ChapterGlossaryReviewFailure, match=message):
        run(harness)
    assert harness.correction_calls == []


def test_non_object_response_root_is_a_hard_failure_without_correction():
    harness = Harness({"result": [], "stats": {}})
    with pytest.raises(ChapterGlossaryReviewFailure, match="parsed review object"):
        run(harness)
    assert harness.correction_calls == []


@pytest.mark.parametrize(
    "malformed_issues",
    [
        "one textual issue",
        [["one textual issue"]],
    ],
)
def test_unsafe_issues_shapes_receive_one_schema_correction(malformed_issues):
    harness = Harness(
        response([decision()], issues=malformed_issues),
        response([decision()], issues=["one textual issue"], cost=0.02),
    )
    result = run(harness)
    assert len(harness.original_calls) == 1
    assert len(harness.correction_calls) == 1
    parsed, errors, metadata = harness.correction_calls[0]
    assert parsed["issues"] == malformed_issues
    assert errors == [
        {
            "code": "invalid_issues_shape",
            "message": "glossary review issues must be an array of strings",
            "retryable": True,
            "field": "issues",
            "allowed_fields": ["issues"],
        }
    ]
    assert metadata["correction_class"] == "model_schema"
    assert metadata["allowed_fields"] == ["issues"]
    assert result["review_result"]["issues"] == ["one textual issue"]
    assert result["review_result"]["decisions"][0]["action"] == "accept"
    assert result["stats"]["request_count"] == 2
    assert result["recovery_record"]["schema_correction"]["attempted"] is True
    assert result["recovery_record"]["schema_correction"]["method"] == "model_remote"
    assert result["recovery_record"]["schema_correction"]["lifecycle"] == "validated"


def test_safe_issue_objects_use_deterministic_local_normalization():
    malformed_issues = [
        {"candidate_id": "c01", "issue": "one textual issue"},
        {"issue": "one textual issue"},
    ]
    original_decisions = [decision()]
    harness = Harness(
        response(original_decisions, issues=malformed_issues),
        RuntimeError("local normalization must not call correction"),
    )

    result = run(harness)

    assert harness.correction_calls == []
    assert result["review_result"]["issues"] == [
        "one textual issue", "one textual issue"
    ]
    assert result["review_result"]["decisions"][0]["action"] == "accept"
    assert result["stats"]["request_count"] == 1
    assert result["stats"]["operation_ids"] == [
        result["operation_ids"]["original"]
    ]
    correction = result["recovery_record"]["schema_correction"]
    assert correction["attempted"] is False
    assert correction["method"] == "deterministic_local"
    assert correction["policy"] == "issues-shape-local-v1"
    assert correction["lifecycle"] == "validated"
    assert correction["stats"]["api_calls"] == 0
    assert correction["stats"]["cost_usd"] == 0
    assert correction["result"]["decisions"] == original_decisions


def test_local_issues_normalization_does_not_consume_semantic_budget():
    harness = Harness(
        response(
            [decision(action="")],
            issues=[{"candidate_id": "c01", "issue": "verbatim issue"}],
        ),
        response(
            [{"candidate_id": "c01", "action": "accept"}],
            issues=["verbatim issue"],
            cost=0.02,
        ),
    )

    result = run(harness)

    assert len(harness.correction_calls) == 1
    parsed, errors, metadata = harness.correction_calls[0]
    assert parsed["issues"] == ["verbatim issue"]
    assert errors[0]["code"] == "invalid_action"
    assert metadata["correction_class"] == "semantic"
    assert result["review_result"]["issues"] == ["verbatim issue"]
    assert result["review_result"]["decisions"][0]["action"] == "accept"
    assert result["recovery_record"]["schema_correction"]["attempted"] is False
    assert result["recovery_record"]["semantic_correction"]["attempted"] is True
    assert result["stats"]["request_count"] == 2


@pytest.mark.parametrize(
    "changed_decision",
    [
        decision(action="reject"),
        decision(english="Rewritten"),
        decision(notes="rewritten"),
        decision(merge_with="injected-target"),
    ],
)
def test_schema_correction_cannot_change_any_decision_field(changed_decision):
    harness = Harness(
        response([decision()], issues={"issue": "text"}),
        response([changed_decision], issues=["text"]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="outside its authorized"):
        run(harness)
    assert len(harness.correction_calls) == 1


def test_schema_correction_cannot_change_candidate_identity():
    harness = Harness(
        response([decision()], issues={"issue": "text"}),
        response([decision("c99")], issues=["text"]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="outside this request"):
        run(harness)
    assert len(harness.correction_calls) == 1


@pytest.mark.parametrize("mode", ["order", "coverage"])
def test_schema_correction_cannot_change_candidate_order_or_coverage(mode):
    items = [candidate("term-a", "A"), candidate("term-b", "B")]
    original_decisions = [decision("c01", english="A"), decision("c02", english="B")]
    if mode == "order":
        corrected_decisions = list(reversed(original_decisions))
        message = "outside its authorized"
    else:
        corrected_decisions = original_decisions[:1]
        message = "omitted request-local"
    harness = Harness(
        response(original_decisions, issues={"issue": "text"}),
        response(corrected_decisions, issues=["text"]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match=message):
        run(harness, candidates=items)
    assert len(harness.correction_calls) == 1


@pytest.mark.parametrize("still_invalid", ["text", [{"issue": "text"}]])
def test_schema_correction_with_invalid_issues_fails_without_second_call(
    still_invalid,
):
    harness = Harness(
        response([decision()], issues={"issue": "text"}),
        response([decision()], issues=still_invalid),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="failed validation"):
        run(harness)
    assert len(harness.correction_calls) == 1

    saved = harness.persisted[-1]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("schema correction must not repeat"),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="failed validation"):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


def test_schema_correction_consumes_the_only_correction_budget():
    harness = Harness(
        response([decision(action="")], issues={"issue": "text"}),
        response([decision(action="")], issues=["text"]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="failed validation"):
        run(harness)
    assert len(harness.correction_calls) == 1


def test_interrupted_schema_correction_is_not_reissued_on_restart():
    first = Harness(
        response([decision()], issues={"issue": "text"}),
        RuntimeError("simulated interruption after schema attempt checkpoint"),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        run(first)
    saved = first.persisted[-1]
    assert saved["schema_correction"]["attempted"] is True
    assert "result" not in saved["schema_correction"]

    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("schema correction must not repeat"),
    )
    with pytest.raises(
        ChapterGlossaryReviewFailure,
        match="schema_correction_remote_outcome_uncertain",
    ):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert restarted.persisted[-1]["schema_correction"]["lifecycle"] == (
        "outcome_uncertain"
    )


def test_legacy_in_flight_safe_issue_objects_recover_locally_without_calls():
    item = candidate()
    source = item["chinese"]
    translation = "Synthetic target text."
    input_hash = chapter_review_input_hash(
        [item], ("existing-key",), source, translation
    )
    operation_ids = chapter_review_operation_ids(
        "synthetic_novel", 7, input_hash
    )
    schema_operation_id = chapter_review_schema_correction_operation_id(
        "synthetic_novel", 7, input_hash
    )
    original_decisions = [decision()]
    original_result = {
        "decisions": copy.deepcopy(original_decisions),
        "issues": [{"candidate_id": "c01", "issue": "verbatim issue"}],
    }
    recovery = {
        "policy_version": "semantic-fields-v1",
        "model_id_transport_version": "short-request-ids-v1",
        "input_hash": input_hash,
        "candidate_ids": [item["candidate_id"]],
        "operation_ids": operation_ids,
        "status": "schema_correction_in_flight",
        "original": {
            "operation_id": operation_ids["original"],
            "result": original_result,
            "stats": {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.1},
        },
        "schema_correction": {
            "attempted": True,
            "operation_id": schema_operation_id,
            "errors": copy.deepcopy(ISSUES_SHAPE_ERRORS),
        },
    }
    harness = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("uncertain schema request must not repeat"),
    )

    result = run(harness, recovery=recovery)

    assert harness.original_calls == []
    assert harness.correction_calls == []
    assert result["review_result"]["issues"] == ["verbatim issue"]
    assert result["review_result"]["decisions"] == [
        {
            **original_decisions[0],
            "candidate_id": item["candidate_id"],
            "chinese": item["chinese"],
        }
    ]
    correction = result["recovery_record"]["schema_correction"]
    assert correction["attempted"] is False
    assert correction["method"] == "deterministic_local"
    assert correction["orphaned_remote_operation"] == {
        "operation_id": schema_operation_id,
        "prior_status": "schema_correction_in_flight",
        "legacy_attempted": True,
        "prior_lifecycle": "legacy_in_flight",
        "durable_result_recorded": False,
        "outcome": "uncertain_not_reissued",
    }
    assert correction["stats"]["api_calls"] == 0
    assert result["stats"]["operation_ids"] == [operation_ids["original"]]


def test_recorded_paid_schema_correction_is_reused_without_reissue():
    first = Harness(
        response([decision()], issues="unsafe scalar"),
        response([decision()], issues=["corrected issue"], cost=0.02),
    )
    initial = run(first)
    statuses = [record["status"] for record in first.persisted]
    assert "schema_correction_prepared" in statuses
    assert "schema_correction_in_flight" in statuses
    assert "schema_correction_recorded" in statuses
    assert "schema_correction_validated" in statuses

    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("recorded schema correction must not repeat"),
    )
    recovered = run(restarted, recovery=initial["recovery_record"])

    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert recovered["reused_original"] is True
    assert recovered["reused_correction"] is True
    assert recovered["review_result"] == initial["review_result"]
    assert recovered["stats"]["request_count"] == 2
    assert recovered["stats"]["cost_usd"] == pytest.approx(0.12)


def test_prepared_schema_operation_can_start_once_after_restart():
    first = Harness(
        response([decision()], issues="unsafe scalar"),
        RuntimeError("stop after in-flight checkpoint"),
    )
    with pytest.raises(RuntimeError, match="stop after in-flight"):
        run(first)
    prepared = next(
        record for record in first.persisted
        if record["status"] == "schema_correction_prepared"
    )
    assert prepared["schema_correction"]["attempted"] is False
    assert prepared["schema_correction"]["lifecycle"] == "prepared"

    restarted = Harness(
        RuntimeError("original must not repeat"),
        response([decision()], issues=["corrected issue"], cost=0.02),
    )
    recovered = run(restarted, recovery=prepared)

    assert restarted.original_calls == []
    assert len(restarted.correction_calls) == 1
    assert recovered["review_result"]["issues"] == ["corrected issue"]
    assert recovered["recovery_record"]["schema_correction"]["lifecycle"] == (
        "validated"
    )


def test_real_chapter_213_legacy_recovery_is_read_only_and_zero_api():
    pending_md = REPO_ROOT / "novels/guhuo_bird/output/pending/0213.md"
    pending_json = REPO_ROOT / "novels/guhuo_bird/output/pending/0213.json"
    if not pending_md.exists() or not pending_json.exists():
        pytest.skip("Chapter 213 has since been committed; synthetic recovery coverage remains")
    progress_json = REPO_ROOT / "novels/guhuo_bird/data/progress.json"
    chapters_json = REPO_ROOT / "novels/guhuo_bird/data/chapters.json"
    glossary_json = REPO_ROOT / "novels/guhuo_bird/data/glossary.json"
    protected = {
        pending_md: "dfefffcccab9f8f5ac834673092cad2341ef4a87b1122e242ec298e5b31d8529",
        pending_json: "2a9a49b0ee9beadccba342bc38d136ca2a7251770926551371594f1f766073a1",
        progress_json: "deb763fdefeecb982994f61fda61e667b29d97a53a3e95df26d6eaa12833e15a",
    }

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {path: digest(path) for path in protected}
    assert before == protected
    meta = json.loads(pending_json.read_text(encoding="utf-8"))
    recovery = copy.deepcopy(meta[CHAPTER_REVIEW_RECOVERY_KEY])
    chapters = json.loads(chapters_json.read_text(encoding="utf-8"))
    source_text = next(
        item["text"] for item in chapters if item.get("production_index") == 213
    )
    translation = pending_md.read_text(encoding="utf-8")
    glossary = json.loads(glossary_json.read_text(encoding="utf-8"))
    novel_config = json.loads(
        (REPO_ROOT / "novels/guhuo_bird/novel.json").read_text(encoding="utf-8")
    )
    profile = load_translation_profile(novel_config["profile"], REPO_ROOT)
    prepared = prepare_glossary_review_candidates(
        novel_id="guhuo_bird",
        chapter=213,
        state=meta["state"],
        source_text=source_text,
        translation=translation,
        profile=profile,
        existing_terms=glossary["entries"],
        resolution_path=candidate_resolution_path(
            REPO_ROOT / "novels/guhuo_bird/data", 213
        ),
        low_value_terms=project_low_value_terms("guhuo_bird"),
    )
    candidates = prepared["candidates"]
    original_result = copy.deepcopy(recovery["original"]["result"])
    persisted = []
    api_calls = []
    accounting_calls = []

    def forbidden_api(*args, **kwargs):
        api_calls.append((args, kwargs))
        raise AssertionError("real Chapter 213 recovery attempted an API call")

    outcome = run_chapter_glossary_review(
        novel_id="guhuo_bird",
        chapter=213,
        source_text=source_text,
        translation=translation,
        candidates=candidates,
        existing_source_keys=glossary["entries"].keys(),
        request_original=forbidden_api,
        request_correction=forbidden_api,
        recovery_record=recovery,
        persist_recovery=lambda record: persisted.append(copy.deepcopy(record)),
    )

    expected_issues = [
        "媽閣具 rendering choice between Mage Utensils and Mage Tools; both acceptable. Retained as proposed for stability with Final English.",
        "京蜥 may not mean 'Capital Lizard' in Min dialect context; flagged for future review if additional source evidence emerges.",
    ]
    assert recovery["input_hash"] == (
        "03108d5ccc209219a3f7faedd7a96b47539a0258868e642903e75b1f59ca226e"
    )
    assert len(candidates) == 23
    assert meta["candidate_preparation"]["conflict_count"] == 0
    assert len(original_result["decisions"]) == 23
    assert original_result["issues"] == [
        {"candidate_id": "c09", "issue": expected_issues[0]},
        {"candidate_id": "c23", "issue": expected_issues[1]},
    ]
    assert api_calls == []
    assert accounting_calls == []
    assert outcome["input_hash"] == recovery["input_hash"]
    assert outcome["review_result"]["issues"] == expected_issues
    assert len(outcome["review_result"]["decisions"]) == 23
    local = outcome["recovery_record"]["schema_correction"]
    assert local["method"] == "deterministic_local"
    assert local["stats"]["api_calls"] == 0
    assert local["stats"]["cost_usd"] == 0
    assert local["result"]["issues"] == expected_issues
    assert local["result"]["decisions"] == original_result["decisions"]
    c09 = next(
        item for item in local["result"]["decisions"]
        if item["candidate_id"] == "c09"
    )
    assert c09["action"] == "correct"
    assert c09["english"] == "Mage Tools"
    assert c09["category"] == "artifact"
    assert outcome["recovery_record"]["original"]["result"] == original_result
    assert outcome["stats"]["operation_ids"] == [
        recovery["operation_ids"]["original"]
    ]
    assert persisted
    assert {path: digest(path) for path in protected} == before


def test_chapter_32_style_saved_original_is_reused_and_schema_result_is_exactly_once():
    items = [candidate(f"term-{index}", f"Name {index}") for index in range(1, 7)]
    source = "\n".join(item["chinese"] for item in items)
    translation = "Synthetic target text."
    existing = ("existing-key",)
    input_hash = chapter_review_input_hash(items, existing, source, translation)
    operation_ids = chapter_review_operation_ids("synthetic_novel", 7, input_hash)
    candidate_ids = [item["candidate_id"] for item in items]
    original_decisions = [
        decision(f"c{index:02d}", english=f"Name {index}")
        for index in range(1, 7)
    ]
    malformed_original = {
        "decisions": original_decisions,
        "issues": [{"candidate_id": "c02", "issue": "keep identities distinct"}],
    }
    saved_recovery = {
        "policy_version": "semantic-fields-v1",
        "model_id_transport_version": "short-request-ids-v1",
        "input_hash": input_hash,
        "candidate_ids": candidate_ids,
        "operation_ids": operation_ids,
        "status": "original_recorded",
        "original": {
            "operation_id": operation_ids["original"],
            "result": malformed_original,
            "stats": {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.1},
        },
    }
    first = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("local normalization must not call correction"),
    )
    outcome = run(first, candidates=items, existing=existing, recovery=saved_recovery)

    assert first.original_calls == []
    assert first.correction_calls == []
    assert outcome["reused_original"] is True
    assert outcome["review_result"]["issues"] == ["keep identities distinct"]
    assert [item["english"] for item in outcome["review_result"]["decisions"]] == [
        f"Name {index}" for index in range(1, 7)
    ]
    persisted = outcome["recovery_record"]
    assert persisted["policy_version"] == "semantic-fields-v1"
    assert persisted["input_hash"] == input_hash
    assert persisted["candidate_ids"] == candidate_ids
    assert persisted["operation_ids"] == operation_ids
    assert persisted["schema_correction"]["errors"][0]["code"] == "invalid_issues_shape"
    assert persisted["schema_correction"]["method"] == "deterministic_local"
    assert persisted["schema_correction"]["attempted"] is False
    assert outcome["stats"]["request_count"] == 1

    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("schema correction must not repeat"),
    )
    recovered = run(restarted, candidates=items, existing=existing, recovery=persisted)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert recovered["reused_original"] is True
    assert recovered["reused_correction"] is True
    assert recovered["review_result"] == outcome["review_result"]


def test_schema_correction_prompt_is_issues_only_and_does_not_expose_sources():
    system_prompt, user_prompt = build_chapter_review_schema_correction_prompt(
        chapter=32,
        schema='{"decisions":[],"issues":[]}',
        errors=[{"code": "invalid_issues_shape", "field": "issues"}],
        rejected_result={
            "decisions": [decision()],
            "issues": [{"issue": "text"}],
        },
    )
    compact_system = " ".join(system_prompt.split())
    assert "Preserve decisions EXACTLY" in compact_system
    assert "Change ONLY issues" in compact_system
    assert "return []" in compact_system
    assert "term-a" not in user_prompt
    assert "source:" not in user_prompt


@pytest.mark.parametrize(
    "bad, corrected",
    [
        (decision(action="unsupported"), decision(action="accept")),
        (decision(action=""), decision(action="reject")),
        (
            decision(action="correct", english=""),
            decision(action="correct", english="Corrected A"),
        ),
        (
            decision(action="contextual", approved_renderings=[]),
            decision(
                action="contextual", approved_renderings=["Form A", "Form B"]
            ),
        ),
        (
            decision(action="merge", merge_with=""),
            decision(action="merge", merge_with="existing-key"),
        ),
        (
            decision(notes="x" * 501),
            decision(notes="concise"),
        ),
    ],
)
def test_allowlisted_semantic_errors_receive_one_correction(bad, corrected):
    harness = Harness(response([bad]), response([corrected], cost=0.02))
    result = run(harness)
    assert len(harness.correction_calls) == 1
    assert result["stats"]["request_count"] == 2
    assert result["stats"]["cost_usd"] == pytest.approx(0.12)


def test_english_merge_rendering_is_not_silently_mapped_to_source_key():
    tower = "\ud0d1\uc8fc"
    mage_tower = "\ub9c8\ud0d1\uc8fc"
    item = candidate(mage_tower, "Tower Master")
    harness = Harness(
        response([decision(action="merge", merge_with="Tower Master")]),
        response([decision(action="merge", merge_with=tower)], cost=0.02),
    )
    result = run(harness, candidates=[item], existing=(tower,))
    assert harness.correction_calls[0][1][0]["code"] == "invalid_merge_target"
    assert result["review_result"]["decisions"][0]["merge_with"] == tower


def test_chapter_36_shaped_invalid_merge_correction_and_recovery_are_reused():
    items = [candidate(f"term-{index}", f"Name {index}") for index in range(1, 6)]
    predator_status = candidate("獵食者身份", "Predator Status")
    predator_status["category"] = "system term"
    items.append(predator_status)
    original_decisions = [
        decision(f"c{index:02d}", english=f"Name {index}")
        for index in range(1, 6)
    ]
    original_decisions.append(
        decision(
            "c06",
            action="merge",
            english="",
            category="system term",
            approved_renderings=[],
            merge_with="獵食者",
            notes="Merge the acquired status into the Predator role.",
        )
    )
    corrected_decisions = copy.deepcopy(original_decisions)
    corrected_decisions[5].update(
        action="accept",
        english="Predator Status",
        approved_renderings=[],
        merge_with="",
        notes=(
            "Formal status that can be acquired through certain special items. "
            "Distinct from the Predator role itself, so kept as a separate entry."
        ),
    )
    first = Harness(
        response(original_decisions),
        response(corrected_decisions, cost=0.02),
    )

    result = run(first, candidates=items, existing=("existing-key",))

    assert first.correction_calls[0][1][0]["code"] == "invalid_merge_target"
    corrected = result["review_result"]["decisions"][5]
    assert corrected["action"] == "accept"
    assert corrected["english"] == "Predator Status"
    assert corrected["category"] == "system term"
    assert corrected["approved_renderings"] == []
    assert corrected["merge_with"] == ""

    saved = copy.deepcopy(result["recovery_record"])
    saved["status"] = "failed"
    saved["correction_error"] = (
        "glossary semantic correction modified fields outside its authorized scope"
    )
    restarted = Harness(
        RuntimeError("original review must not repeat"),
        RuntimeError("semantic correction must not repeat"),
    )
    recovered = run(
        restarted,
        candidates=items,
        existing=("existing-key",),
        recovery=saved,
    )

    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert recovered["reused_original"] is True
    assert recovered["reused_correction"] is True
    assert recovered["review_result"] == result["review_result"]


def test_real_style_source_merge_key_validates_but_rendering_does_not():
    tower = "\ud0d1\uc8fc"
    mage_tower = "\ub9c8\ud0d1\uc8fc"
    item = candidate(mage_tower, "Tower Master")
    valid = Harness(response([decision(action="merge", merge_with=tower)]))
    assert run(valid, candidates=[item], existing=(tower,))["review_result"]

    invalid = Harness(
        response([decision(action="merge", merge_with="Tower Master")]),
        response([decision(action="merge", merge_with="Tower Master")]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="failed validation"):
        run(invalid, candidates=[item], existing=(tower,))


@pytest.mark.parametrize(
    "corrected, message",
    [
        (response([decision("c99")]), "outside this request"),
        (response([decision(chinese="changed")]), "rewrote source key"),
        (response([]), "omitted request-local"),
        (response([decision(), decision()]), "duplicate model decision"),
        (response([decision(), decision("c99")]), "outside this request"),
    ],
)
def test_corrective_response_cannot_mutate_candidate_identity(corrected, message):
    harness = Harness(response([decision(action="")]), corrected)
    with pytest.raises(ChapterGlossaryReviewFailure, match=message):
        run(harness)
    assert len(harness.correction_calls) == 1


def test_correction_discards_change_to_already_valid_other_decision_with_audit():
    items = [candidate("term-a", "A"), candidate("term-b", "B")]
    initial = response(
        [decision("c01", action=""), decision("c02", english="B")]
    )
    corrected = response(
        [
            decision("c01", action="accept"),
            decision("c02", english="Changed B"),
        ]
    )
    harness = Harness(initial, corrected)
    result = run(harness, candidates=items)

    decisions = result["review_result"]["decisions"]
    assert decisions[0]["action"] == "accept"
    assert decisions[1]["english"] == "B"
    discarded = result["recovery_record"]["local_revalidation"][
        "semantic_correction_merge"
    ]["discarded_unauthorized_changes"]
    assert discarded == [{
        "candidate_id": items[1]["candidate_id"],
        "field": "english",
        "original_value": "B",
        "corrective_value": "Changed B",
    }]


def test_one_correction_maximum_and_failed_result_is_not_retried_on_restart():
    harness = Harness(
        response([decision(action="merge", merge_with="English Name")]),
        response([decision(action="merge", merge_with="English Name")]),
    )
    with pytest.raises(ChapterGlossaryReviewFailure):
        run(harness)
    saved = harness.persisted[-1]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )
    with pytest.raises(ChapterGlossaryReviewFailure):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


def test_interrupted_in_flight_correction_is_not_reissued_on_restart():
    first = Harness(
        response([decision(action="")]),
        RuntimeError("simulated interruption after attempt checkpoint"),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        run(first)
    saved = first.persisted[-1]
    assert saved["semantic_correction"]["attempted"] is True
    assert "result" not in saved["semantic_correction"]

    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="already attempted"):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


def test_successful_correction_and_stats_are_reused_without_duplicate_calls():
    first = Harness(
        response([decision(action="")], cost=0.1),
        response([decision(action="accept")], cost=0.02),
    )
    result = run(first)
    saved = result["recovery_record"]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )
    recovered = run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert recovered["stats"]["cost_usd"] == pytest.approx(0.12)
    assert recovered["reused_original"] is True
    assert recovered["reused_correction"] is True


def test_recorded_action_only_correction_merges_and_discards_other_candidates():
    items = [
        candidate("term-a", "A"),
        candidate("term-b", "B"),
        candidate("term-c", "C"),
    ]
    original_decisions = [
        decision(
            "c01", action="approve_character_name", english="A",
            category="character", notes="Original A notes.",
        ),
        decision(
            "c02", action="correct", english="B", category="skill",
            notes="Original B notes.",
        ),
        decision(
            "c03", action="contextual", english="C", category="skill",
            approved_renderings=["C", "Form C"], notes="Original C notes.",
        ),
    ]
    correction_decisions = [
        {"candidate_id": "c01", "action": "accept"},
        {"candidate_id": "c02", "action": "accept"},
        {"candidate_id": "c03", "action": "accept"},
    ]
    source = "\n".join(item["chinese"] for item in items)
    translation = "Synthetic target text."
    input_hash = chapter_review_input_hash(items, ("existing-key",), source, translation)
    operation_ids = chapter_review_operation_ids("synthetic_novel", 7, input_hash)
    recovery = {
        "policy_version": "semantic-fields-v1",
        "model_id_transport_version": "short-request-ids-v1",
        "input_hash": input_hash,
        "candidate_ids": [item["candidate_id"] for item in items],
        "operation_ids": operation_ids,
        "status": "failed",
        "original": {
            "operation_id": operation_ids["original"],
            "result": {"decisions": original_decisions, "issues": []},
            "stats": {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.1},
        },
        "semantic_correction": {
            "attempted": True,
            "operation_id": operation_ids["corrective"],
            "validation_errors": [{
                "code": "invalid_action",
                "message": (
                    "invalid glossary review action for "
                    f"{items[0]['candidate_id']}: 'approve_character_name'"
                ),
                "retryable": True,
                "candidate_id": "c01",
            }],
            "result": {"decisions": correction_decisions, "issues": []},
            "stats": {"prompt_tokens": 3, "completion_tokens": 1, "cost_usd": 0.02},
        },
        "correction_error": "historical scope rejection",
    }
    harness = Harness(
        RuntimeError("original review must not repeat"),
        RuntimeError("semantic correction must not repeat"),
    )

    result = run(harness, candidates=items, recovery=recovery)

    assert harness.original_calls == []
    assert harness.correction_calls == []
    decisions = result["review_result"]["decisions"]
    assert [item["action"] for item in decisions] == [
        "accept", "correct", "contextual"
    ]
    assert decisions[0]["english"] == "A"
    assert decisions[0]["category"] == "character"
    assert decisions[0]["notes"] == "Original A notes."
    audit = result["recovery_record"]["local_revalidation"]
    assert audit["prior_failure"] == {
        "status": "failed",
        "correction_error": "historical scope rejection",
    }
    assert audit["semantic_correction_merge"]["authorization"] == {
        items[0]["candidate_id"]: ["action"]
    }
    assert [item["candidate_id"] for item in audit[
        "semantic_correction_merge"
    ]["discarded_unauthorized_changes"]] == [
        items[1]["candidate_id"], items[2]["candidate_id"]
    ]
    assert result["stats"]["operation_ids"] == [
        operation_ids["original"], operation_ids["corrective"]
    ]
    assert recovery["status"] == "failed"
    assert recovery["semantic_correction"]["result"] == {
        "decisions": correction_decisions,
        "issues": [],
    }


def test_recorded_action_only_recovery_rediscovers_and_repairs_later_invalid_action():
    items = [candidate("term-a", "A"), candidate("term-b", "B")]
    source = "\n".join(item["chinese"] for item in items)
    translation = "Synthetic target text."
    input_hash = chapter_review_input_hash(items, ("existing-key",), source, translation)
    operation_ids = chapter_review_operation_ids("synthetic_novel", 7, input_hash)
    original_decisions = [
        decision("c01", action="approve_character_name", english="A"),
        decision("c02", action="approve_technique", english="B"),
    ]
    correction_decisions = [
        {"candidate_id": "c01", "action": "accept"},
        {"candidate_id": "c02", "action": "accept"},
    ]
    recovery = {
        "policy_version": "semantic-fields-v1",
        "model_id_transport_version": "short-request-ids-v1",
        "input_hash": input_hash,
        "candidate_ids": [item["candidate_id"] for item in items],
        "operation_ids": operation_ids,
        "status": "failed",
        "original": {
            "operation_id": operation_ids["original"],
            "result": {"decisions": original_decisions, "issues": []},
            "stats": {},
        },
        "semantic_correction": {
            "attempted": True,
            "operation_id": operation_ids["corrective"],
            "validation_errors": [{
                "code": "invalid_action",
                "message": (
                    "invalid glossary review action for "
                    f"{items[0]['candidate_id']}: 'approve_character_name'"
                ),
                "retryable": True,
                "candidate_id": "c01",
            }],
            "result": {"decisions": correction_decisions, "issues": []},
            "stats": {},
        },
        "correction_error": "historical scope rejection",
    }
    harness = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    result = run(harness, candidates=items, recovery=recovery)

    assert harness.original_calls == []
    assert harness.correction_calls == []
    assert [item["action"] for item in result["review_result"]["decisions"]] == [
        "accept", "accept"
    ]
    saved = result["recovery_record"]
    assert saved["correction_error"] == "historical scope rejection"
    local = saved["local_revalidation"]
    assert local["status"] == "completed"
    assert local["prior_failure"] == {
        "status": "failed",
        "correction_error": "historical scope rejection",
    }
    assert [item["candidate_id"] for item in local[
        "local_revalidation_errors"
    ]] == [item["candidate_id"] for item in items]
    assert local["semantic_correction_merge"][
        "discarded_unauthorized_changes"
    ] == []


def test_three_fail_fast_invalid_actions_recover_from_one_recorded_error_without_calls():
    items = [
        candidate("term-a", "A"),
        candidate("term-b", "B"),
        candidate("term-c", "C"),
    ]
    first = Harness(
        response([
            decision("c01", action="approve_character_name", english="A"),
            decision("c02", action="approve_technique", english="B"),
            decision("c03", action="approve_skill", english="C"),
        ]),
        response([
            {"candidate_id": "c01", "action": "accept"},
            {"candidate_id": "c02", "action": "accept"},
            {"candidate_id": "c03", "action": "accept"},
        ], cost=0.02),
    )
    initial = run(first, candidates=items)
    saved = initial["recovery_record"]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    recovered = run(restarted, candidates=items, recovery=saved)

    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert [item["action"] for item in recovered[
        "review_result"
    ]["decisions"]] == ["accept", "accept", "accept"]
    local = recovered["recovery_record"]["local_revalidation"]
    assert [item["code"] for item in local["local_revalidation_errors"]] == [
        "invalid_action", "invalid_action", "invalid_action"
    ]
    assert local["semantic_correction_merge"]["authorization"] == {
        item["candidate_id"]: ["action"] for item in items
    }
    assert saved["semantic_correction"]["validation_errors"] == [{
        "code": "invalid_action",
        "message": (
            "invalid glossary review action for "
            f"{items[0]['candidate_id']}: 'approve_character_name'"
        ),
        "retryable": True,
        "candidate_id": "c01",
    }]


def test_unknown_locally_rediscovered_error_fails_closed_without_replay_calls(
    monkeypatch,
):
    first = Harness(
        response([decision(action="")]),
        response([decision(action="accept")], cost=0.02),
    )
    saved = run(first)["recovery_record"]
    internal_id = saved["candidate_ids"][0]
    monkeypatch.setattr(
        chapter_glossary_review,
        "collect_glossary_review_decision_validation_errors",
        lambda *args, **kwargs: [
            {
                "code": "invalid_action",
                "message": "reproduced",
                "retryable": True,
                "candidate_id": internal_id,
            },
            {
                "code": "unknown_local_error",
                "message": "synthetic unknown",
                "retryable": True,
                "candidate_id": internal_id,
            },
        ],
    )
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    with pytest.raises(ChapterGlossaryReviewFailure, match="unknown or non-allowlisted"):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


@pytest.mark.parametrize(
    "second_correction",
    [
        {"candidate_id": "c02"},
        {"candidate_id": "c02", "action": "approve_skill"},
    ],
    ids=("missing-required-action", "still-invalid-action"),
)
def test_rediscovered_invalid_action_without_valid_stored_patch_fails_closed(
    second_correction,
):
    items = [candidate("term-a", "A"), candidate("term-b", "B")]
    original = response([
        decision("c01", action="approve_character_name", english="A"),
        decision("c02", action="approve_technique", english="B"),
    ])
    correction = response([
        {"candidate_id": "c01", "action": "accept"},
        second_correction,
    ], cost=0.02)
    first = Harness(original, correction)

    with pytest.raises(ChapterGlossaryReviewFailure, match="invalid.*action"):
        run(first, candidates=items)
    saved = first.persisted[-1]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    with pytest.raises(ChapterGlossaryReviewFailure, match="invalid.*action"):
        run(restarted, candidates=items, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert saved["local_revalidation"]["status"] == "validation_failed"
    assert [item["candidate_id"] for item in saved["local_revalidation"][
        "local_revalidation_errors"
    ]] == [item["candidate_id"] for item in items]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(input_hash="0" * 64),
        lambda record: record.update(policy_version="semantic-fields-v0"),
        lambda record: record.update(model_id_transport_version="wrong-transport"),
        lambda record: record["candidate_ids"].append("source:extra"),
        lambda record: record["original"].update(operation_id="wrong-original"),
        lambda record: record["semantic_correction"].update(
            operation_id="wrong-corrective"
        ),
        lambda record: record["semantic_correction"].update(
            validation_errors=[{
                "code": "unknown",
                "message": "changed",
                "retryable": True,
                "candidate_id": "c01",
            }]
        ),
        lambda record: record["original"]["result"]["decisions"][0].update(
            notes="changed after local revalidation"
        ),
        lambda record: record["semantic_correction"]["result"]["decisions"][0].update(
            action="reject"
        ),
    ],
    ids=(
        "input-hash",
        "policy",
        "transport",
        "candidate-identity",
        "original-operation",
        "corrective-operation",
        "validation-error-identity",
        "original-result-identity",
        "corrective-result-identity",
    ),
)
def test_recorded_semantic_recovery_identity_mismatches_fail_without_api(mutation):
    first = Harness(
        response([decision(action="")]),
        response([decision(action="accept")], cost=0.02),
    )
    result = run(first)
    recovery = copy.deepcopy(result["recovery_record"])
    mutation(recovery)
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    with pytest.raises(ChapterGlossaryReviewFailure):
        run(restarted, recovery=recovery)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


def test_original_and_corrective_operation_ids_are_distinct_and_stable():
    harness = Harness(
        response([decision(action="")]),
        response([decision(action="accept")]),
    )
    first = run(harness)
    second_ids = chapter_review_operation_ids(
        "synthetic_novel", 7, first["input_hash"]
    )
    assert first["operation_ids"] == second_ids
    assert second_ids["original"] != second_ids["corrective"]
    assert harness.original_calls[0][1]["operation_id"] == second_ids["original"]
    assert harness.correction_calls[0][2]["operation_id"] == second_ids["corrective"]


def test_short_id_mapping_is_reused_by_semantic_correction():
    harness = Harness(
        response([decision(action="")]),
        response([decision(action="accept")]),
    )
    run(harness)
    parsed, errors, metadata = harness.correction_calls[0]
    assert parsed["decisions"][0]["candidate_id"] == "c01"
    assert errors[0]["candidate_id"] == "c01"
    assert metadata["candidate_sources"] == {"c01": "term-a"}


def test_corrective_prompt_is_bounded_and_requires_source_key_merge_identity():
    system_prompt, user_prompt = build_chapter_review_semantic_correction_prompt(
        chapter=7,
        schema='{"decisions":[]}',
        model_candidates=[{"candidate_id": "c01", "chinese": "term-a"}],
        candidate_sources={"c01": "term-a"},
        existing_merge_targets={"existing-key": {"english": "English Name"}},
        errors=[{
            "candidate_id": "c01",
            "code": "invalid_merge_target",
            "retryable": True,
        }],
        rejected_result={
            "decisions": [
                {
                    "candidate_id": "c01",
                    "action": "merge",
                    "merge_with": "English Name",
                }
            ],
            "issues": [],
        },
    )
    compact_system = " ".join(system_prompt.split())
    assert "exact existing SOURCE glossary key" in compact_system
    assert "English rendering is never a source identity" in compact_system
    assert "existing-key" in user_prompt
    assert "English Name" in user_prompt
    assert "chapter source prose" not in user_prompt
    assert "full target prose" not in user_prompt


def test_invalid_notes_prompt_exposes_exact_scope_and_length_constraint():
    system_prompt, user_prompt = build_chapter_review_semantic_correction_prompt(
        chapter=146,
        schema='{"decisions":[]}',
        model_candidates=[{"candidate_id": "c01", "chinese": "item"}],
        candidate_sources={"c01": "item"},
        existing_merge_targets={},
        errors=[{
            "candidate_id": "c01",
            "code": "invalid_notes",
            "retryable": True,
            "field": "notes",
            "reason": "max_length_exceeded",
            "actual_length": 559,
            "max_length": 500,
        }],
        rejected_result={
            "decisions": [{
                "candidate_id": "c01",
                "action": "correct",
                "notes": "x" * 559,
            }],
            "issues": [],
        },
    )

    assert "Never change action unless action is independently authorized" in system_prompt
    assert "c01: AUTHORIZED FIELDS: notes" in user_prompt
    assert "DO NOT CHANGE: action" in user_prompt
    assert "rejected length 559" in user_prompt
    assert "at most 500 characters" in user_prompt


def test_invalid_notes_type_prompt_requires_a_string():
    _, user_prompt = build_chapter_review_semantic_correction_prompt(
        chapter=7,
        schema='{"decisions":[]}',
        model_candidates=[{"candidate_id": "c01", "chinese": "item"}],
        candidate_sources={"c01": "item"},
        existing_merge_targets={},
        errors=[{
            "candidate_id": "c01",
            "code": "invalid_notes",
            "retryable": True,
            "field": "notes",
            "reason": "wrong_type",
            "expected_type": "string",
            "actual_type": "list",
        }],
        rejected_result={
            "decisions": [{"candidate_id": "c01", "notes": []}],
            "issues": [],
        },
    )

    assert "c01.notes: rejected type list; must be string" in user_prompt


def test_ineffective_notes_correction_gets_one_distinct_bounded_retry():
    long_notes = "x" * 559
    first_correction = response(
        [{"candidate_id": "c01", "action": "accept"}], cost=0.02
    )
    second_correction = response(
        [{"candidate_id": "c01", "notes": "Short valid rationale."}],
        cost=0.03,
    )
    harness = Harness(
        response([decision(action="correct", notes=long_notes)], cost=0.1),
        (first_correction, second_correction),
    )

    result = run(harness)

    assert len(harness.correction_calls) == 2
    first_metadata = harness.correction_calls[0][2]
    retry_errors = harness.correction_calls[1][1]
    retry_metadata = harness.correction_calls[1][2]
    expected_retry_id = chapter_review_semantic_retry_operation_id(
        "synthetic_novel", 7, result["input_hash"]
    )
    assert first_metadata["correction_attempt"] == 1
    assert retry_metadata["correction_attempt"] == 2
    assert retry_metadata["operation_id"] == expected_retry_id
    assert retry_metadata["operation_id"] != first_metadata["operation_id"]
    assert retry_errors[0]["reason"] == "max_length_exceeded"
    assert retry_errors[0]["actual_length"] == 559
    assert retry_errors[0]["max_length"] == 500
    final = result["review_result"]["decisions"][0]
    assert final["action"] == "correct"
    assert final["notes"] == "Short valid rationale."
    local = result["recovery_record"]["local_revalidation"]
    assert local["semantic_correction_ineffective"] is True
    assert local["semantic_correction_merge"]["applied_fields"] == []
    assert [item["field"] for item in local[
        "semantic_correction_merge"
    ]["discarded_unauthorized_changes"]] == ["action"]
    assert local["semantic_correction_retry_merge"]["applied_fields"] == [{
        "candidate_id": candidate()["candidate_id"],
        "field": "notes",
    }]
    assert result["stats"]["request_count"] == 3
    assert result["stats"]["cost_usd"] == pytest.approx(0.15)
    assert result["stats"]["operation_ids"] == [
        result["operation_ids"]["original"],
        result["operation_ids"]["corrective"],
        expected_retry_id,
    ]


def test_valid_first_notes_correction_needs_no_second_request():
    harness = Harness(
        response([decision(action="correct", notes="x" * 501)]),
        response([{"candidate_id": "c01", "notes": "Valid notes."}], cost=0.02),
    )

    result = run(harness)

    assert len(harness.correction_calls) == 1
    assert result["review_result"]["decisions"][0]["notes"] == "Valid notes."
    assert result["stats"]["request_count"] == 2
    assert "semantic_correction_retry" not in result["recovery_record"]


def test_still_invalid_authorized_notes_are_ineffective_and_retryable_once():
    harness = Harness(
        response([decision(action="correct", notes="x" * 501)]),
        (
            response([{"candidate_id": "c01", "notes": "y" * 510}], cost=0.02),
            response([{"candidate_id": "c01", "notes": "Recovered."}], cost=0.03),
        ),
    )

    result = run(harness)

    assert len(harness.correction_calls) == 2
    assert len(harness.correction_calls[1][0]["decisions"][0]["notes"]) == 510
    assert result["review_result"]["decisions"][0]["notes"] == "Recovered."
    first_merge = result["recovery_record"]["local_revalidation"][
        "semantic_correction_merge"
    ]
    assert first_merge["applied_fields"] == [{
        "candidate_id": candidate()["candidate_id"],
        "field": "notes",
    }]


def test_retry_applies_valid_notes_and_discards_unauthorized_action():
    harness = Harness(
        response([decision(action="correct", notes="x" * 501)]),
        (
            response([{"candidate_id": "c01", "action": "accept"}], cost=0.02),
            response([{
                "candidate_id": "c01",
                "action": "accept",
                "notes": "Valid retry notes.",
            }], cost=0.03),
        ),
    )

    result = run(harness)

    final = result["review_result"]["decisions"][0]
    assert final["action"] == "correct"
    assert final["notes"] == "Valid retry notes."
    retry_audit = result["recovery_record"]["local_revalidation"][
        "semantic_correction_retry_merge"
    ]
    assert retry_audit["discarded_unauthorized_changes"] == [{
        "candidate_id": candidate()["candidate_id"],
        "field": "action",
        "original_value": "correct",
        "corrective_value": "accept",
    }]


def test_recorded_retry_is_reused_without_api_or_duplicate_accounting():
    first = Harness(
        response([decision(action="correct", notes="x" * 501)], cost=0.1),
        (
            response([{"candidate_id": "c01", "action": "accept"}], cost=0.02),
            response([{"candidate_id": "c01", "notes": "Valid."}], cost=0.03),
        ),
    )
    initial = run(first)
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("semantic correction must not repeat"),
    )

    recovered = run(restarted, recovery=initial["recovery_record"])

    assert restarted.original_calls == []
    assert restarted.correction_calls == []
    assert recovered["reused_original"] is True
    assert recovered["reused_correction"] is True
    assert recovered["reused_correction_retry"] is True
    assert recovered["stats"]["request_count"] == 3
    assert recovered["stats"]["cost_usd"] == pytest.approx(0.15)


def test_failed_second_notes_correction_stops_without_attempt_three():
    first = Harness(
        response([decision(action="correct", notes="x" * 501)]),
        (
            response([{"candidate_id": "c01", "action": "accept"}], cost=0.02),
            response([{"candidate_id": "c01", "action": "accept"}], cost=0.03),
        ),
    )
    with pytest.raises(ChapterGlossaryReviewFailure, match="retry failed validation"):
        run(first)
    assert len(first.correction_calls) == 2
    saved = first.persisted[-1]
    restarted = Harness(
        RuntimeError("original must not repeat"),
        RuntimeError("correction must not repeat"),
    )

    with pytest.raises(ChapterGlossaryReviewFailure, match="retry failed validation"):
        run(restarted, recovery=saved)
    assert restarted.original_calls == []
    assert restarted.correction_calls == []


def test_two_source_keys_with_same_english_remain_distinct_identities():
    items = [candidate("source-a", "Same"), candidate("source-b", "Same")]
    harness = Harness(
        response(
            [
                decision("c01", english="Same"),
                decision("c02", english="Same"),
            ]
        )
    )
    result = run(harness, candidates=items)
    decisions = result["review_result"]["decisions"]
    assert [item["chinese"] for item in decisions] == ["source-a", "source-b"]
    assert len({item["candidate_id"] for item in decisions}) == 2


def test_pending_state_and_paid_work_are_not_advanced_before_review_passes():
    meta = {
        "stage": "state_extracted",
        "translation_stats": {"cost_usd": 1.0},
        "state": {"new_terms": []},
        "state_stats": {"cost_usd": 0.2},
    }
    progress = {"last_completed": 6, "next_chapter": 7}
    harness = Harness(response([decision(action="")]), response([decision(action="")]))

    def persist(record):
        meta[CHAPTER_REVIEW_RECOVERY_KEY] = copy.deepcopy(record)
        harness.persist(record)

    with pytest.raises(ChapterGlossaryReviewFailure):
        run_chapter_glossary_review(
            novel_id="synthetic_novel",
            chapter=7,
            source_text="term-a",
            translation="Paid target text.",
            candidates=[candidate()],
            existing_source_keys=(),
            request_original=harness.request_original,
            request_correction=harness.request_correction,
            persist_recovery=persist,
        )
    assert meta["stage"] == "state_extracted"
    assert meta["translation_stats"] == {"cost_usd": 1.0}
    assert meta["state_stats"] == {"cost_usd": 0.2}
    assert progress == {"last_completed": 6, "next_chapter": 7}


def test_merge_target_context_preserves_order_and_distinct_shared_english():
    relevant = [
        {
            "chinese": "source-b",
            "english": "Shared Rendering",
            "status": "provisional",
        },
        {
            "chinese": "source-a",
            "english": "Shared Rendering",
            "status": "locked",
        },
    ]
    targets = chapter_review_merge_targets(relevant)
    assert list(targets) == ["source-b", "source-a"]
    assert targets == {
        "source-b": {
            "english": "Shared Rendering",
            "status": "provisional",
        },
        "source-a": {"english": "Shared Rendering", "status": "locked"},
    }


def test_empty_relevant_glossary_context_is_legal():
    assert chapter_review_merge_targets([]) == {}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _load_synthetic_production_module(project_root, monkeypatch):
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    module_name = f"_chapter_review_recovery_{uuid.uuid4().hex}"
    script = REPO_ROOT / (
        "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    module = types.ModuleType(module_name)
    module.__file__ = str(script)
    module.__package__ = None
    sys.modules[module_name] = module
    try:
        source = script.read_text(encoding="utf-8")
        exec(compile(source, str(script), "exec"), module.__dict__)
        module.initialize_production_runtime_state()
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module_name, module


@pytest.mark.usefixtures("direct_pro_route")
def test_recovered_process_path_builds_list_shaped_review_context_without_regeneration(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "synthetic_recovery"
    (project / "data").mkdir(parents=True)
    (project / "output" / "pending").mkdir(parents=True)
    _write_json(
        project / "novel.json",
        {
            "id": "synthetic_recovery",
            "title": "Synthetic Recovery",
            "source_language": "ko",
            "target_language": "en",
            "profile": "korean_fantasy",
            "final_chapter": 1,
            "audit_interval": 25,
            "chapter_numbering": "global",
            "source_type": "txt",
        },
    )
    source_text = "마탑주가 탑주와 부탑주를 불렀다."
    _write_json(
        project / "data" / "chapters.json",
        [
            {
                "chapter": 1,
                "production_index": 1,
                "unit_kind": "chapter",
                "heading": "Chapter 1",
                "text": source_text,
            }
        ],
    )
    _write_json(
        project / "data" / "glossary.json",
        {
            "entries": {
                "탑주": {
                    "english": "Tower Master",
                    "category": "title",
                    "status": "provisional",
                },
                "부탑주": {
                    "english": "Tower Master",
                    "category": "title",
                    "status": "provisional",
                },
            }
        },
    )
    _write_json(
        project / "data" / "progress.json",
        {
            "completed": [],
            "failed": [],
            "last_completed": 0,
            "next_chapter": 1,
            "last_audited_chapter": 0,
        },
    )
    _write_json(
        project / "data" / "continuity.json",
        {"latest_chapter": 0, "summary": ""},
    )
    _write_json(project / "data" / "issues.json", {"issues": []})
    _write_json(project / "data" / "audit_history.json", {"audits": []})
    (project / "translation_addendum.txt").write_text("", encoding="utf-8")

    paid_translation = "# Chapter 1\n\n" + (
        "The recovered paid translation remains unchanged. " * 30
    )
    pending_md = project / "output" / "pending" / "0001.md"
    pending_json = project / "output" / "pending" / "0001.json"
    pending_md.write_text(paid_translation, encoding="utf-8")
    saved_state = {
        "new_terms": [
            {
                "chinese": "마탑주",
                "english": "Tower Master",
                "category": "title",
                "status": "provisional",
                "notes": "Synthetic candidate.",
            },
            {
                "chinese": "마탑주",
                "english": "Tower Master",
                "category": "title",
                "status": "provisional",
                "notes": "Repeated extraction of the same semantic candidate.",
            },
        ],
        "continuity_summary": "Synthetic continuity.",
        "issues": [],
    }
    zero_stats = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }
    original_meta = {
        "chapter": 1,
        "stage": "state_extracted",
        "translation_stats": zero_stats,
        "elapsed_seconds": 1.0,
        "finish_reason": "stop",
        "state": saved_state,
        "state_stats": zero_stats,
    }
    _write_json(pending_json, original_meta)

    module_name, production = _load_synthetic_production_module(project, monkeypatch)
    original_meta["state_checkpoint_identity"] = (
        production.current_state_checkpoint_identity(
            1,
            source_text,
            paid_translation,
        )
    )
    _write_json(pending_json, original_meta)
    assert 'each exact "chinese" source key may appear at most once' in (
        production.STATE_PROMPT
    )
    context_shapes = []
    real_context_builder = production.chapter_review_merge_targets

    def record_context_shape(entries):
        context_shapes.append(
            (type(entries), [entry["chinese"] for entry in entries])
        )
        return real_context_builder(entries)

    def forbidden_generation(*args, **kwargs):
        raise AssertionError("paid translation/state extraction was regenerated")

    class ReachedMockReviewRequest(production.NonProviderProductionError):
        pass

    request_calls = []

    def stop_at_mock_request(*args, **kwargs):
        request_calls.append(copy.deepcopy(kwargs))
        raise ReachedMockReviewRequest("synthetic API boundary reached")

    monkeypatch.setattr(production, "chapter_review_merge_targets", record_context_shape)
    monkeypatch.setattr(production, "translate_chapter", forbidden_generation)
    monkeypatch.setattr(production, "extract_state", forbidden_generation)
    monkeypatch.setattr(production, "call_api_with_retries", stop_at_mock_request)
    try:
        assert production.process_chapter(1) is False
    finally:
        sys.modules.pop(module_name, None)

    assert context_shapes == [(list, ["탑주", "부탑주"])]
    assert len(request_calls) == 1
    user_prompt = request_calls[0]["messages"][1]["content"]
    assert "탑주" in user_prompt
    assert "부탑주" in user_prompt
    assert '"candidate_id": "c01"' in user_prompt
    assert '"candidate_id": "c02"' not in user_prompt
    assert pending_md.read_text(encoding="utf-8") == paid_translation
    recovered_meta = json.loads(pending_json.read_text(encoding="utf-8"))
    assert recovered_meta["stage"] == "state_extracted"
    assert recovered_meta["state"] == saved_state
    assert recovered_meta["translation_stats"] == original_meta["translation_stats"]
    assert recovered_meta["state_stats"] == original_meta["state_stats"]
    assert recovered_meta["candidate_preparation"]["raw_candidate_count"] == 2
    assert recovered_meta["candidate_preparation"]["canonical_candidate_count"] == 1
    assert (
        recovered_meta["candidate_preparation"]["semantic_duplicates_removed"]
        == 1
    )

    # The original mocked request may have been paid. An unchanged restart
    # must stop at its durable intent without generating translation or review.
    assert production.process_chapter(1) is False
    assert len(request_calls) == 1
    assert "automatic retry is forbidden" in capsys.readouterr().out
    assert pending_md.read_text(encoding="utf-8") == paid_translation

    conflicting_state = copy.deepcopy(saved_state)
    conflicting_state["new_terms"][1]["english"] = "Conflicting Tower Title"
    conflict_meta = copy.deepcopy(recovered_meta)
    conflict_meta["state"] = conflicting_state
    _write_json(pending_json, conflict_meta)
    progress_before = (project / "data" / "progress.json").read_bytes()
    paid_before = pending_md.read_bytes()
    request_count_before = len(request_calls)

    assert production.process_chapter(1) is False

    controlled_output = capsys.readouterr().out
    assert "GLOSSARY CANDIDATE IDENTITY CONFLICT" in controlled_output
    assert "Differing semantic fields: english" in controlled_output
    assert "Proposal 0:" in controlled_output
    assert "Proposal 1:" in controlled_output
    assert "No new glossary-review API call was made." in controlled_output
    assert len(request_calls) == request_count_before
    assert pending_md.read_bytes() == paid_before
    assert (project / "data" / "progress.json").read_bytes() == progress_before

    source_backed, rejected = glossary_integrity.source_backed_candidate_proposals(
        conflicting_state["new_terms"], source_text, 1
    )
    assert rejected == []
    eligible, rejected = filter_profile_candidates(
        production.ACTIVE_PROFILE,
        source_backed,
        existing_terms=production.glossary["entries"],
        low_value_terms=production.LOW_VALUE_TERMS,
    )
    assert rejected == []
    with pytest.raises(GlossaryCandidateIdentityError) as conflict_error:
        canonicalize_glossary_review_candidates(eligible)
    conflict = conflict_error.value.diagnostics["conflicts"][0]
    selected = next(
        item["semantic_identity_fingerprint"]
        for item in conflict["proposals"]
        if item["english"] == "Tower Master"
    )
    create_candidate_resolution(
        candidate_resolution_path(project / "data", 1),
        novel_id="synthetic_recovery",
        chapter=1,
        pending_identity=pending_content_identity(paid_translation),
        state_identity=state_extraction_identity(conflicting_state),
        conflict=conflict,
        selected_semantic_fingerprint=selected,
        rationale="Synthetic explicit source review approved Tower Master.",
        timestamp_utc="2026-08-29T20:00:00+00:00",
    )

    assert production.process_chapter(1) is False
    resolved_output = capsys.readouterr().out
    assert "GLOSSARY CANDIDATE CONFLICT RESOLUTION APPLIED" in resolved_output
    assert "Canonical candidates after resolution: 1" in resolved_output
    assert len(request_calls) == request_count_before
    assert "saved chapter glossary-review recovery does not match current input" in resolved_output
    assert pending_md.read_bytes() == paid_before
    assert (project / "data" / "progress.json").read_bytes() == progress_before
    resolved_meta = json.loads(pending_json.read_text(encoding="utf-8"))
    assert resolved_meta["candidate_preparation"]["resolved_conflict_count"] == 1
    assert resolved_meta["candidate_preparation"]["canonical_candidate_count"] == 1

    resolution_file = candidate_resolution_path(project / "data", 1)
    resolution_file.write_text("{malformed", encoding="utf-8")
    requests_before_invalid = len(request_calls)
    assert production.process_chapter(1) is False
    invalid_output = capsys.readouterr().out
    assert "GLOSSARY CANDIDATE CONFLICT RESOLUTION INVALID" in invalid_output
    assert "No new glossary-review API call was made." in invalid_output
    assert len(request_calls) == requests_before_invalid
    assert pending_md.read_bytes() == paid_before
    assert (project / "data" / "progress.json").read_bytes() == progress_before


def test_original_review_intent_is_durable_before_request():
    harness = Harness(response([decision()]))
    request = harness.request_original
    def checked_request(candidates, metadata):
        assert harness.persisted[-1]["status"] == "original_started"
        assert harness.persisted[-1]["original"] == {
            "operation_id": metadata["operation_id"], "attempted": True,
        }
        return request(candidates, metadata)
    harness.request_original = checked_request
    result = run(harness)
    assert len(harness.original_calls) == 1
    recovered = Harness(None)
    replay = run(recovered, recovery=result["recovery_record"])
    assert recovered.original_calls == recovered.correction_calls == []
    assert replay["stats"] == result["stats"]


@pytest.mark.parametrize(
    "error",
    [
        OSError("intent disk full"),
        RuntimeError("writer bug"),
    ],
)
def test_original_intent_persistence_failure_is_typed_and_prevents_request(error):
    harness = Harness(response([decision()]))

    def fail(_):
        raise error

    harness.persist = fail

    with pytest.raises(
        ChapterGlossaryReviewPersistenceError,
        match="recovery persistence failed",
    ) as caught:
        run(harness)

    assert caught.value.__cause__ is error
    assert harness.original_calls == harness.correction_calls == []


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(5)])
def test_original_intent_base_exceptions_preserve_process_control(error):
    harness = Harness(response([decision()]))

    def fail(_):
        raise error

    harness.persist = fail

    with pytest.raises(type(error)) as caught:
        run(harness)

    assert caught.value is error
    assert harness.original_calls == harness.correction_calls == []


@pytest.mark.parametrize("failure_point", ["request", "response_shape", "response_write"])
def test_uncertain_original_review_never_reissues_on_repeated_restart(failure_point):
    harness = Harness(response([decision()]))
    if failure_point == "request":
        def request(candidates, metadata):
            harness.original_calls.append((candidates, metadata))
            raise TimeoutError("unknown provider outcome")
        harness.request_original = request
    elif failure_point == "response_shape":
        harness.original = {"result": "malformed", "stats": {}}
    else:
        persist = harness.persist
        def fail_response_write(record):
            if record["status"] == "original_recorded":
                raise OSError("response disk full")
            persist(record)
        harness.persist = fail_response_write
    with pytest.raises(
        (
            TimeoutError,
            ChapterGlossaryReviewFailure,
            ChapterGlossaryReviewPersistenceError,
        )
    ):
        run(harness)
    assert len(harness.original_calls) == 1
    assert harness.correction_calls == []
    saved = copy.deepcopy(harness.persisted[-1])
    assert saved["status"] == "original_started"
    assert "stats" not in saved["original"]  # Do not invent unknown usage.
    for _ in range(2):
        restarted = Harness(response([decision()]))
        with pytest.raises(ChapterGlossaryReviewFailure, match="automatic retry is forbidden"):
            run(restarted, recovery=saved)
        assert restarted.original_calls == restarted.correction_calls == []
        assert restarted.persisted == []
        assert saved == harness.persisted[-1]


@pytest.mark.parametrize("malformed", [None, {}, {"result": None}, "broken"])
def test_malformed_saved_original_record_fails_closed_without_request(malformed):
    harness = Harness(response([decision()]))
    saved = run(harness)["recovery_record"]
    saved["original"] = malformed
    restarted = Harness(response([decision()]))
    with pytest.raises(ChapterGlossaryReviewFailure, match="automatic retry is forbidden"):
        run(restarted, recovery=saved)
    assert restarted.original_calls == restarted.correction_calls == []
