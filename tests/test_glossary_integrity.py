import copy

import pytest

from glossary_integrity import (
    LEGACY_NORMALIZED_OR_MANUAL,
    SOURCE_BACKED,
    UNBACKED_PROVISIONAL,
    UNRESOLVED,
    GlossaryReviewSchemaError,
    GlossaryReviewValidationError,
    GlossarySourceIdentityError,
    MAX_REVIEW_NOTES_CHARS,
    classify_glossary_source_entries,
    collect_glossary_review_decision_validation_errors,
    glossary_review_semantic_correction_authorization,
    merge_glossary_review_semantic_correction,
    normalize_glossary_review_issues_shape,
    partition_audit_candidates_by_source_integrity,
    source_backed_candidate_proposals,
    stable_glossary_candidate_id,
    validate_glossary_review_result,
    validate_glossary_review_schema,
    validate_glossary_review_schema_correction_scope,
    validate_glossary_review_semantic_correction_scope,
)


ISSUES_SHAPE_ERRORS = [{
    "code": "invalid_issues_shape",
    "message": "glossary review issues must be an array of strings",
    "retryable": True,
    "field": "issues",
    "allowed_fields": ["issues"],
}]


def proposal(source="마나 심장"):
    return {
        "chinese": source,
        "english": "Mana Heart",
        "category": "system term",
        "status": "provisional",
    }


def review(candidate, **overrides):
    decision = {
        "candidate_id": candidate["candidate_id"],
        "action": "accept",
        "english": candidate["english"],
        "category": candidate["category"],
        "notes": "",
    }
    decision.update(overrides)
    return {"decisions": [decision], "issues": []}


def test_literal_korean_candidate_keeps_immutable_identity_through_review():
    accepted, rejected = source_backed_candidate_proposals(
        [proposal()], "그는 마나 심장을 만들었다.", 7
    )
    assert rejected == []
    candidate = accepted[0]
    result = validate_glossary_review_result(review(candidate), accepted, ())

    assert candidate["candidate_id"] == stable_glossary_candidate_id("마나 심장")
    assert candidate["source_provenance"]["verification"] == "exact_literal"
    assert result["decisions"][0]["chinese"] == "마나 심장"


def test_literal_mixed_hangul_hanja_candidate_is_source_backed():
    source_key = "군신(軍神)"
    accepted, rejected = source_backed_candidate_proposals(
        [proposal(source_key)], f"그는 {source_key}이라 불렸다.", 9
    )
    assert rejected == []
    assert accepted[0]["chinese"] == source_key


@pytest.mark.parametrize("rewritten", ["魔力心脏", "Mana Simjang"])
def test_review_cannot_rewrite_source_key_in_another_script(rewritten):
    accepted, _ = source_backed_candidate_proposals(
        [proposal()], "마나 심장", 1
    )
    with pytest.raises(GlossarySourceIdentityError, match="rewrote source key"):
        validate_glossary_review_result(
            review(accepted[0], chinese=rewritten), accepted, ()
        )


def test_review_cannot_inject_unknown_candidate_id():
    accepted, _ = source_backed_candidate_proposals(
        [proposal()], "마나 심장", 1
    )
    with pytest.raises(GlossarySourceIdentityError, match="unknown candidate_id"):
        validate_glossary_review_result(
            review(accepted[0], candidate_id="source:not-supplied"), accepted, ()
        )


def test_model_proposal_without_literal_source_evidence_is_rejected():
    accepted, rejected = source_backed_candidate_proposals(
        [proposal("魔力心脏")], "마나 심장", 3
    )
    assert accepted == []
    assert rejected[0]["chinese"] == "魔力心脏"
    assert "no exact literal evidence" in rejected[0]["reason"]


def test_unbacked_provisional_is_excluded_from_audit_candidates():
    entries = {"魔力心脏": {"status": "provisional"}}
    report = classify_glossary_source_entries(entries, "마나 심장")
    candidates, excluded = partition_audit_candidates_by_source_integrity(
        [{"chinese": "魔力心脏"}], report
    )
    assert report["魔力心脏"]["classification"] == UNBACKED_PROVISIONAL
    assert candidates == []
    assert excluded[0]["classification"] == UNBACKED_PROVISIONAL


def test_locked_source_absent_entry_is_unresolved_and_never_deleted():
    entries = {"Legacy Name": {"status": "locked", "english": "Legacy"}}
    before = copy.deepcopy(entries)
    report = classify_glossary_source_entries(entries, "authoritative text")
    candidates, excluded = partition_audit_candidates_by_source_integrity(
        [{"chinese": "Legacy Name"}], report
    )
    assert report["Legacy Name"]["classification"] == UNRESOLVED
    assert candidates == []
    assert excluded[0]["status"] == "locked"
    assert entries == before


def test_explicit_legacy_normalization_requires_verified_authoritative_key():
    entries = {
        "Normalized Form": {
            "status": "locked",
            "source_provenance": {
                "origin": "legacy_normalized",
                "verification": "normalized_from_exact_source",
                "authoritative_source_key": "원문 형식",
            },
        }
    }
    report = classify_glossary_source_entries(entries, "여기 원문 형식이 있다")
    assert report["Normalized Form"]["classification"] == (
        LEGACY_NORMALIZED_OR_MANUAL
    )


def test_exact_source_identity_remains_stable_after_metadata_correction():
    accepted, _ = source_backed_candidate_proposals(
        [proposal()], "마나 심장", 12
    )
    candidate = accepted[0]
    result = validate_glossary_review_result(
        review(candidate, action="correct", english="Mana Core"),
        accepted,
        (),
    )
    decision = result["decisions"][0]
    assert decision["chinese"] == "마나 심장"
    assert decision["candidate_id"] == candidate["candidate_id"]
    assert decision["english"] == "Mana Core"


def test_source_backed_classification_accepts_any_literal_script():
    entries = {
        "마나 심장": {"status": "provisional"},
        "군신(軍神)": {"status": "provisional"},
        "Dämmerung": {"status": "provisional"},
    }
    report = classify_glossary_source_entries(
        entries, "마나 심장 군신(軍神) Dämmerung"
    )
    assert {item["classification"] for item in report.values()} == {SOURCE_BACKED}


@pytest.mark.parametrize(
    "result, code, retryable, field",
    [
        ([], "invalid_response_root_shape", False, ""),
        ({"decisions": {}, "issues": []}, "invalid_decisions_shape", False, "decisions"),
        ({"decisions": [], "issues": "text"}, "invalid_issues_shape", True, "issues"),
        ({"decisions": [], "issues": [{"issue": "text"}]}, "invalid_issues_shape", True, "issues"),
        ({"decisions": [], "issues": [["text"]]}, "invalid_issues_shape", True, "issues"),
    ],
)
def test_review_schema_errors_are_explicitly_classified(
    result, code, retryable, field
):
    with pytest.raises(GlossaryReviewSchemaError) as caught:
        validate_glossary_review_result(result, (), ())
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.field == field
    assert caught.value.allowed_fields == (("issues",) if retryable else ())


def test_valid_string_issues_need_no_local_normalization():
    original = {"decisions": [], "issues": ["one", "two"]}
    assert normalize_glossary_review_issues_shape(
        original, ISSUES_SHAPE_ERRORS, {"c01"}
    ) is None
    assert original["issues"] == ["one", "two"]


@pytest.mark.parametrize(
    "issues",
    [
        [{"issue": "one"}, {"issue": "two"}],
        [
            {"candidate_id": "c01", "issue": "one"},
            {"candidate_id": "c02", "issue": "two"},
        ],
    ],
)
def test_safe_issue_objects_normalize_deterministically(issues):
    original = {
        "decisions": [{"candidate_id": "c01", "action": "accept"}],
        "issues": copy.deepcopy(issues),
        "review_version": "v1",
    }
    before_decisions = copy.deepcopy(original["decisions"])

    normalized = normalize_glossary_review_issues_shape(
        original, ISSUES_SHAPE_ERRORS, {"c01", "c02"}
    )

    assert normalized["issues"] == ["one", "two"]
    assert normalized["decisions"] == before_decisions
    assert original["issues"] == issues
    validate_glossary_review_schema_correction_scope(
        original, normalized, ISSUES_SHAPE_ERRORS
    )
    validate_glossary_review_schema(normalized)


@pytest.mark.parametrize(
    "unsafe_issues",
    [
        [{"candidate_id": "unknown", "issue": "x"}],
        [{"issue": "x", "english": "New Rendering"}],
        [{"issue": "x", "category": "creature"}],
        [{"candidate_id": "c01", "issue": 123}],
        [{"message": "x"}],
        [None],
        [42],
        ["already a string", {"issue": "x"}],
    ],
)
def test_unsafe_issue_objects_are_not_normalized(unsafe_issues):
    assert normalize_glossary_review_issues_shape(
        {"decisions": [], "issues": unsafe_issues},
        ISSUES_SHAPE_ERRORS,
        {"c01"},
    ) is None


def test_local_issue_normalization_preserves_order_and_duplicates():
    original = {
        "decisions": [],
        "issues": [
            {"issue": "second"},
            {"issue": "same"},
            {"issue": "first"},
            {"issue": "same"},
        ],
    }
    normalized = normalize_glossary_review_issues_shape(
        original, ISSUES_SHAPE_ERRORS, set()
    )
    assert normalized["issues"] == ["second", "same", "first", "same"]


def test_local_issue_normalization_is_not_used_for_unrelated_schema_error():
    original = {"decisions": [], "issues": [{"issue": "x"}]}
    assert normalize_glossary_review_issues_shape(
        original,
        [{
            "code": "invalid_decisions_shape",
            "retryable": False,
            "field": "decisions",
            "allowed_fields": [],
        }],
        {"c01"},
    ) is None


def test_semantic_validation_failure_is_not_an_issues_shape_normalization():
    item = proposal()
    item["candidate_id"] = stable_glossary_candidate_id(item["chinese"])
    original = review(item, action="")
    assert normalize_glossary_review_issues_shape(
        original,
        [{
            "code": "invalid_action",
            "retryable": True,
            "candidate_id": item["candidate_id"],
        }],
        {item["candidate_id"]},
    ) is None
    with pytest.raises(GlossaryReviewValidationError, match="action"):
        validate_glossary_review_result(original, [item], ())


def test_issues_schema_correction_scope_allows_only_issues_to_change():
    original = {
        "decisions": [{"candidate_id": "source:one", "action": "accept"}],
        "issues": [{"issue": "text"}],
        "review_version": "v1",
    }
    corrected = copy.deepcopy(original)
    corrected["issues"] = ["text"]
    validate_glossary_review_schema_correction_scope(
        original,
        corrected,
        [{"code": "invalid_issues_shape", "retryable": True}],
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["decisions"][0].update(action="reject"),
        lambda value: value["decisions"].reverse(),
        lambda value: value.update(review_version="v2"),
        lambda value: value.update(extra="injected"),
    ],
)
def test_issues_schema_correction_scope_rejects_other_changes(mutate):
    original = {
        "decisions": [
            {"candidate_id": "source:one", "action": "accept"},
            {"candidate_id": "source:two", "action": "reject"},
        ],
        "issues": [{"issue": "text"}],
        "review_version": "v1",
    }
    corrected = copy.deepcopy(original)
    corrected["issues"] = ["text"]
    mutate(corrected)
    with pytest.raises(GlossarySourceIdentityError, match="outside its authorized"):
        validate_glossary_review_schema_correction_scope(
            original,
            corrected,
            [{"code": "invalid_issues_shape", "retryable": True}],
        )


def test_schema_correction_scope_rejects_non_allowlisted_error():
    with pytest.raises(GlossarySourceIdentityError, match="non-allowlisted"):
        validate_glossary_review_schema_correction_scope(
            {"decisions": [], "issues": {}},
            {"decisions": [], "issues": []},
            [{"code": "invalid_decisions_shape", "retryable": False}],
        )


def semantic_merge_decisions():
    return {
        "decisions": [
            {
                "candidate_id": "source:predator-status",
                "action": "merge",
                "english": "",
                "category": "system term",
                "approved_renderings": [],
                "merge_with": "獵食者",
                "notes": "Invalid merge proposal.",
            },
            {
                "candidate_id": "source:other",
                "action": "accept",
                "english": "Other Term",
                "category": "system term",
                "approved_renderings": [],
                "merge_with": "",
                "notes": "Already valid.",
            },
        ],
        "issues": ["Preserve this review issue."],
    }


def invalid_merge_target_error():
    return [
        {
            "candidate_id": "source:predator-status",
            "code": "invalid_merge_target",
            "retryable": True,
        }
    ]


def test_invalid_merge_target_scope_allows_contextual_reclassification():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0].update(
        action="contextual",
        english="Predator Status",
        approved_renderings=["Predator Status", "Predator State"],
        merge_with="",
        notes="Distinct from the Predator role.",
    )

    validate_glossary_review_semantic_correction_scope(
        original, corrected, invalid_merge_target_error()
    )


def test_invalid_merge_target_scope_keeps_category_immutable():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0]["merge_with"] = "existing-source-key"
    corrected["decisions"][0]["category"] = "role"

    with pytest.raises(GlossarySourceIdentityError, match="outside its authorized"):
        validate_glossary_review_semantic_correction_scope(
            original, corrected, invalid_merge_target_error()
        )


def test_invalid_merge_target_scope_cannot_change_another_candidate():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0]["merge_with"] = "existing-source-key"
    corrected["decisions"][1]["english"] = "Changed Other Term"

    with pytest.raises(GlossarySourceIdentityError, match="outside its authorized"):
        validate_glossary_review_semantic_correction_scope(
            original, corrected, invalid_merge_target_error()
        )


@pytest.mark.parametrize("identity_change", ["omit", "replace"])
def test_invalid_merge_target_scope_cannot_change_identity_coverage(identity_change):
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    if identity_change == "omit":
        corrected["decisions"].pop()
    else:
        corrected["decisions"][1]["candidate_id"] = "source:injected"

    with pytest.raises(GlossarySourceIdentityError, match="identity coverage"):
        validate_glossary_review_semantic_correction_scope(
            original, corrected, invalid_merge_target_error()
        )


def test_invalid_merge_target_scope_cannot_change_review_issues():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0]["merge_with"] = "existing-source-key"
    corrected["issues"] = ["Changed review issue."]

    with pytest.raises(GlossarySourceIdentityError, match="changed review issues"):
        validate_glossary_review_semantic_correction_scope(
            original, corrected, invalid_merge_target_error()
        )


def test_invalid_merge_target_scope_still_allows_merge_target_only_repair():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0]["merge_with"] = "existing-source-key"

    validate_glossary_review_semantic_correction_scope(
        original, corrected, invalid_merge_target_error()
    )


def test_chapter_36_shaped_invalid_merge_transition_passes_scope_validation():
    original = semantic_merge_decisions()
    corrected = copy.deepcopy(original)
    corrected["decisions"][0].update(
        action="accept",
        english="Predator Status",
        approved_renderings=[],
        merge_with="",
        notes=(
            "Formal status that can be acquired through certain special items. "
            "Distinct from the Predator role itself, so kept as a separate entry."
        ),
    )

    validate_glossary_review_semantic_correction_scope(
        original, corrected, invalid_merge_target_error()
    )


def test_invalid_action_is_retryable_and_authorizes_action_only():
    item = proposal("角色名")
    item["candidate_id"] = stable_glossary_candidate_id(item["chinese"])
    with pytest.raises(GlossaryReviewValidationError) as caught:
        validate_glossary_review_result(
            {
                "decisions": [{
                    "candidate_id": item["candidate_id"],
                    "action": "approve_character_name",
                }],
                "issues": [],
            },
            [item],
            (),
        )
    error = caught.value.as_dict()

    assert error["code"] == "invalid_action"
    assert error["retryable"] is True
    assert glossary_review_semantic_correction_authorization([error]) == {
        item["candidate_id"]: frozenset({"action"})
    }


def test_invalid_notes_length_exposes_canonical_constraint_diagnostics():
    item = proposal("long-notes-term")
    item["candidate_id"] = stable_glossary_candidate_id(item["chinese"])
    with pytest.raises(GlossaryReviewValidationError) as caught:
        validate_glossary_review_result(
            review(item, notes="x" * (MAX_REVIEW_NOTES_CHARS + 59)),
            [item],
            (),
        )

    error = caught.value.as_dict()
    assert error == {
        "code": "invalid_notes",
        "message": f"invalid glossary review notes for {item['candidate_id']}",
        "retryable": True,
        "candidate_id": item["candidate_id"],
        "field": "notes",
        "reason": "max_length_exceeded",
        "actual_length": 559,
        "max_length": MAX_REVIEW_NOTES_CHARS,
    }


def test_invalid_notes_type_exposes_canonical_type_diagnostics():
    item = proposal("wrong-notes-type")
    item["candidate_id"] = stable_glossary_candidate_id(item["chinese"])
    with pytest.raises(GlossaryReviewValidationError) as caught:
        validate_glossary_review_result(
            review(item, notes=[]),
            [item],
            (),
        )

    error = caught.value.as_dict()
    assert error["code"] == "invalid_notes"
    assert error["field"] == "notes"
    assert error["reason"] == "wrong_type"
    assert error["expected_type"] == "string"
    assert error["actual_type"] == "list"


def test_action_only_correction_merges_original_fields_and_discards_unauthorized_changes():
    candidate_id = "source:character"
    original = {
        "decisions": [{
            "candidate_id": candidate_id,
            "action": "approve_character_name",
            "english": "Xiao Meng",
            "category": "character",
            "approved_renderings": [],
            "merge_with": "",
            "notes": "Original notes.",
        }],
        "issues": [],
    }
    corrected = {
        "decisions": [{
            "candidate_id": candidate_id,
            "action": "accept",
            "english": "Changed Name",
            "category": "title",
            "notes": "Changed notes.",
        }],
        "issues": [],
    }
    effective, audit = merge_glossary_review_semantic_correction(
        original,
        corrected,
        [{
            "candidate_id": candidate_id,
            "code": "invalid_action",
            "retryable": True,
        }],
    )
    decision = effective["decisions"][0]

    assert decision == {
        **original["decisions"][0],
        "action": "accept",
    }
    assert audit["authorization"] == {candidate_id: ["action"]}
    assert {item["field"] for item in audit[
        "discarded_unauthorized_changes"
    ]} == {"english", "category", "notes"}


def test_multiple_errors_union_fields_independently_by_candidate():
    authorization = glossary_review_semantic_correction_authorization([
        {
            "candidate_id": "source:first",
            "code": "invalid_action",
            "retryable": True,
        },
        {
            "candidate_id": "source:first",
            "code": "invalid_notes",
            "retryable": True,
        },
        {
            "candidate_id": "source:second",
            "code": "missing_contextual_renderings",
            "retryable": True,
        },
    ])

    assert authorization == {
        "source:first": frozenset({"action", "notes"}),
        "source:second": frozenset({"approved_renderings"}),
    }


@pytest.mark.parametrize(
    "error",
    [
        {"candidate_id": "source:first", "code": "unknown", "retryable": True},
        {
            "candidate_id": "source:first",
            "code": "invalid_action",
            "retryable": False,
        },
        {"candidate_id": "", "code": "invalid_action", "retryable": True},
    ],
)
def test_semantic_correction_authorization_unknown_or_untrusted_error_fails_closed(
    error,
):
    with pytest.raises(GlossarySourceIdentityError):
        glossary_review_semantic_correction_authorization([error])


def test_nonfailing_candidates_are_preserved_with_discard_audit():
    original = {
        "decisions": [
            {"candidate_id": "source:first", "action": "bad", "english": "A"},
            {"candidate_id": "source:second", "action": "correct", "english": "B"},
        ],
        "issues": [],
    }
    corrected = {
        "decisions": [
            {"candidate_id": "source:first", "action": "accept"},
            {"candidate_id": "source:second", "action": "accept"},
        ],
        "issues": [],
    }
    effective, audit = merge_glossary_review_semantic_correction(
        original,
        corrected,
        [{
            "candidate_id": "source:first",
            "code": "invalid_action",
            "retryable": True,
        }],
    )

    assert [item["action"] for item in effective["decisions"]] == [
        "accept", "correct"
    ]
    assert audit["discarded_unauthorized_changes"] == [{
        "candidate_id": "source:second",
        "field": "action",
        "original_value": "correct",
        "corrective_value": "accept",
    }]


def test_fail_fast_batch_records_first_but_local_rediscovery_finds_all_actions():
    candidates = []
    decisions = []
    bad_actions = [
        "approve_character_name",
        "approve_technique",
        "approve_skill",
    ]
    for index, action in enumerate(bad_actions, start=1):
        item = proposal(f"術語{index}")
        item["candidate_id"] = stable_glossary_candidate_id(item["chinese"])
        candidates.append(item)
        decisions.append({
            "candidate_id": item["candidate_id"],
            "action": action,
            "english": item["english"],
            "category": item["category"],
            "notes": "",
        })
    result = {"decisions": decisions, "issues": []}

    with pytest.raises(GlossaryReviewValidationError) as historical:
        validate_glossary_review_result(result, candidates, ())
    rediscovered = collect_glossary_review_decision_validation_errors(
        result, candidates, ()
    )

    assert historical.value.candidate_id == candidates[0]["candidate_id"]
    assert [item["candidate_id"] for item in rediscovered] == [
        item["candidate_id"] for item in candidates
    ]
    assert [item["code"] for item in rediscovered] == ["invalid_action"] * 3
    assert glossary_review_semantic_correction_authorization(rediscovered) == {
        item["candidate_id"]: frozenset({"action"})
        for item in candidates
    }
    glossary_review_semantic_correction_authorization,
    merge_glossary_review_semantic_correction,
