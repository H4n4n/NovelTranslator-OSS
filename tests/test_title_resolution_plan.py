import json
from pathlib import Path

import pytest

from build_normalization_inventory import parse_english_heading
from build_title_resolution_plan import (
    EXPECTED_BASELINE,
    EXPECTED_NORMALIZED,
    build_physical_title_targets,
    build_plan,
    corpus_fingerprint,
    detect_source_number_discontinuities,
    discover_physical_title_target,
    repeated_identity_supports_missing_recovery,
    require_fingerprint,
    resolve_title_decisions,
    review_is_non_executable,
)
from novel_project import select_project


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "novels" / "rogue_first_prince" / "final" / "reports" / "title_resolution_plan.json"


def load_plan():
    return json.loads(REPORT.read_text(encoding="utf-8"))


def synthetic_decision(title_id="synthetic-1", korean="동일 제목", number=7, part=1):
    return {
        "decision_id": title_id,
        "title_kind": "main_story",
        "source_title_number": number,
        "side_story_number": None,
        "korean_title_text": korean,
        "normalized_korean_base_title": f"{number}. {korean}",
        "canonical_english_title_text": "Same Title",
        "unit_parts": [{
            "final_unit": part,
            "part_number": part,
            "current_english_title_text": None,
            "raw_heading": f"Chapter {part}",
            "usable_english_title_present": False,
        }],
        "decision": "APPROVED_CANONICAL",
    }


def test_review_required_title_has_no_executable_canonical():
    reviews = [item for item in load_plan()["title_decisions"] if item["decision"] == "REVIEW_REQUIRED"]
    assert len(reviews) == 9
    assert all(review_is_non_executable(item) for item in reviews)
    assert all(item["canonical_english_title_text"] is None for item in reviews)


def test_exact_repeated_korean_identity_can_support_missing_title_recovery():
    decision = synthetic_decision()
    assert repeated_identity_supports_missing_recovery(decision)
    decision["canonical_english_title_text"] = None
    assert not repeated_identity_supports_missing_recovery(decision)


def discover(text, *, current="Same Title", canonical="Same Title", status="APPROVED_CANONICAL",
             korean="동일 제목", number=7, part_number=1, unit=1):
    decision = synthetic_decision(korean=korean, number=number, part=unit)
    decision.update({
        "decision": status,
        "canonical_english_title_text": canonical,
        "ranked_existing_candidates": [{"english_title_text": current, "occurrence_count": 1}],
        "units": [unit],
        "automatic_application_safe": True,
        "automatic_application_forbidden": status != "APPROVED_CANONICAL",
    })
    decision["unit_parts"][0].update({
        "final_unit": unit,
        "part_number": part_number,
        "current_english_title_text": current,
        "usable_english_title_present": current is not None,
    })
    return discover_physical_title_target(text, unit, decision, decision["unit_parts"][0], "a" * 64)


def test_full_file_discovery_finds_normal_first_line_title():
    target = discover("Chapter 1. 7. Same Title (1)\n\nBody")
    assert target["title_target_status"] == "EXISTING_TITLE"
    assert target["title_location_classification"] == "EXISTING_TITLE_PRIMARY_HEADING"
    assert target["line_number"] == 1


def test_full_file_discovery_finds_secondary_title_after_generic_heading():
    target = discover("Chapter 1\n\n< 7. Same Title (1) >\n\nBody", current=None)
    assert target["title_location_classification"] == "EXISTING_TITLE_SECONDARY_LINE"
    assert target["line_number"] == 3
    assert target["recovery_required"] is False


def test_unit_99_style_discovers_existing_second_line_not_recovery():
    target = discover("Chapter 99<br>\n< 32. Moonlight (2) >\n\nBody",
                      current=None, canonical="Moonlight", number=32,
                      part_number=2, unit=99, korean="달빛")
    assert target["first_nonblank_line_classification"] == "GENERIC_CHAPTER_HEADING"
    assert target["line"] == "< 32. Moonlight (2) >"
    assert target["title_text_span"]["exact_text"] == "Moonlight"
    assert target["title_truly_absent"] is False


def test_generator_wrapped_title_targets_real_line_not_markers():
    text = "#!/GEN_METHOD_TITLE_START\n\nChapter 1: 7. Same Title (1)\n\n#!/GEN_METHOD_TITLE_END\nBody"
    target = discover(text)
    assert target["title_location_classification"] == "EXISTING_TITLE_GENERATOR_WRAPPED"
    assert target["line_number"] == 3
    assert "GEN_METHOD" not in target["line"]


def test_prose_first_and_no_title_is_true_absent():
    target = discover('"Dialogue first."\n\nBody prose.', current=None)
    assert target["title_target_status"] == "TRUE_TITLE_ABSENT"
    assert target["recovery_required"] is True
    assert target["recovery_authorized"] is False


def test_prose_first_then_structured_title_in_bounded_region_is_found():
    target = discover("Opening note.\n\n## 7. Same Title (1)\n\nBody", current=None)
    assert target["title_target_status"] == "EXISTING_TITLE"
    assert target["title_location_classification"] == "EXISTING_TITLE_NONSTANDARD_WRAPPER"


def test_title_like_body_phrase_is_not_mistaken_for_heading():
    text = "Opening prose.\n\nThis paragraph mentions Same Title in ordinary prose.\n" + "\n" * 12 + "## Same Title"
    target = discover(text)
    assert target["title_target_status"] == "TRUE_TITLE_ABSENT"


def test_quoted_nonstandard_title_wrapper_is_classified_separately():
    target = discover('"Same Title"\n\nBody')
    assert target["title_location_classification"] == "EXISTING_TITLE_NONSTANDARD_WRAPPER"


def test_multiple_structured_title_candidates_are_ambiguous():
    target = discover("Chapter 1. 7. Same Title (1)\n< 7. Same Title (1) >\nBody")
    assert target["title_target_status"] == "AMBIGUOUS_TITLE_LOCATION"
    assert target["automatic_application_forbidden"] is True


def test_exact_title_target_binds_normalized_file_sha():
    target = discover("Chapter 1. 7. Same Title (1)\nBody")
    assert target["baseline_normalized_chapter_file_sha256"] == "a" * 64


def test_recovery_authorization_is_target_local_and_never_inherited():
    missing = discover("Body only", current=None)
    existing = discover("Chapter 1. 7. Same Title (1)\nBody")
    assert missing["recovery_authorized"] is False
    assert existing["recovery_required"] is False


def test_one_missing_unit_does_not_authorize_another(tmp_path):
    decision = synthetic_decision()
    decision.update({
        "units": [1, 2], "automatic_application_safe": True,
        "automatic_application_forbidden": False, "ranked_existing_candidates": [],
    })
    decision["unit_parts"] = [
        {"final_unit": 1, "part_number": 1, "current_english_title_text": None, "usable_english_title_present": False},
        {"final_unit": 2, "part_number": 2, "current_english_title_text": None, "usable_english_title_present": False},
    ]
    (tmp_path / "0001.md").write_text("Body only", encoding="utf-8")
    (tmp_path / "0002.md").write_text("Chapter 2\n< 7. Same Title (2) >", encoding="utf-8")
    hashes = {n: corpus_fingerprint(tmp_path)["files"][n - 1]["sha256"] for n in (1, 2)}
    targets, anchors, recovery_anchors = build_physical_title_targets(decision, tmp_path, hashes)
    assert targets[0]["recovery_authorized"] is False
    assert targets[1]["title_target_status"] == "EXISTING_TITLE"
    assert [anchor["unit"] for anchor in anchors] == [2]
    assert recovery_anchors == []


def test_review_required_group_stays_nonexecutable_despite_physical_title(tmp_path):
    decision = synthetic_decision()
    decision.update({"decision": "REVIEW_REQUIRED", "canonical_english_title_text": None,
                     "ranked_existing_candidates": [{"english_title_text": "Same Title", "occurrence_count": 1}],
                     "units": [1]})
    decision["unit_parts"][0]["current_english_title_text"] = "Same Title"
    (tmp_path / "0001.md").write_text("Chapter 1. 7. Same Title (1)", encoding="utf-8")
    targets, anchors, recovery_anchors = build_physical_title_targets(decision, tmp_path, {1: corpus_fingerprint(tmp_path)["files"][0]["sha256"]})
    assert targets[0]["title_target_status"] == "EXISTING_TITLE"
    assert anchors == []
    assert recovery_anchors == []


def test_synthetic_non_rogue_chinese_structure_discovers_exact_title():
    target = discover("Chapter 1\n< 7. 黎明之剑 (1) >\nBody", current=None,
                      canonical="黎明之剑", korean="黎明之剑")
    assert target["title_target_status"] == "EXISTING_TITLE"
    assert target["title_text_span"]["exact_text"] == "黎明之剑"


def test_different_korean_title_text_with_same_source_number_is_not_collapsed():
    left = synthetic_decision("left", "첫 제목", 43, 1)
    right = synthetic_decision("right", "다른 제목", 43, 2)
    guards = detect_source_number_discontinuities([left, right])
    assert len(guards) == 1
    assert guards[0]["distinct_korean_title_count"] == 2
    assert {x["korean_title_text"] for x in guards[0]["title_identities"]} == {"첫 제목", "다른 제목"}
    assert guards[0]["automatic_cross_identity_recovery_forbidden"] is True


def test_explicit_part_numbers_are_never_inferred_from_unit_sequence():
    decision = synthetic_decision()
    decision["unit_parts"][0]["part_number"] = None
    assert decision["unit_parts"][0]["part_number"] is None
    assert "infer" not in repeated_identity_supports_missing_recovery.__doc__.casefold()


def test_unit_99_does_not_parse_br_as_title():
    parsed = parse_english_heading("Chapter 99<br>\n\nBody")
    assert parsed == {"raw": "Chapter 99<br>", "title": None, "kind": "chapter_heading"}
    guard = load_plan()["unit_99_guard"]
    assert guard["bogus_br_title_forbidden"] is True
    assert guard["generic_first_line_is_publication_artifact"] is True
    assert guard["title_target_status"] == "EXISTING_TITLE"
    assert guard["title_location_classification"] == "EXISTING_TITLE_SECONDARY_LINE"
    assert guard["recovery_required"] is False


def test_source_number_43_discontinuity_remains_protected():
    guard = next(x for x in load_plan()["source_number_discontinuity_guards"] if x["source_title_number"] == 43)
    assert guard["automatic_cross_identity_recovery_forbidden"] is True
    assert len(guard["title_identities"]) >= 2
    assert all(item["units"] for item in guard["title_identities"])


def test_source_number_reuse_fails_closed_when_title_identity_differs():
    same = synthetic_decision("same", "같은 제목", 12, 1)
    other = synthetic_decision("other", "바뀐 제목", 12, 2)
    guard = detect_source_number_discontinuities([same, other])[0]
    assert guard["automatic_cross_identity_recovery_forbidden"]


def test_majority_english_cannot_override_known_semantic_mismatch():
    title = next(x for x in load_plan()["title_decisions"] if x["decision_id"] == "title-0003")
    assert title["ranked_existing_candidates"][0]["english_title_text"] == "A Notorious Establishment"
    assert title["ranked_existing_candidates"][0]["occurrence_count"] == 2
    assert title["decision"] == "REVIEW_REQUIRED"
    assert title["canonical_english_title_text"] is None


def test_previously_downgraded_semantic_cases_remain_unresolved():
    plan = load_plan()
    by_id = {x["decision_id"]: x for x in plan["title_decisions"]}
    assert plan["human_downgraded_group_ids"] == ["title-0003", "title-0071", "title-0085", "title-0089"]
    assert all(by_id[item]["decision"] == "REVIEW_REQUIRED" for item in plan["human_downgraded_group_ids"])


def test_generic_synthetic_non_rogue_discontinuity_behaves_correctly():
    chinese_a = synthetic_decision("cn-a", "同名编号甲", 9, 1)
    chinese_b = synthetic_decision("cn-b", "同名编号乙", 9, 2)
    guard = detect_source_number_discontinuities([chinese_a, chinese_b])[0]
    assert guard["source_title_number"] == 9
    assert guard["distinct_korean_title_count"] == 2


def test_title_application_anchors_target_normalized_chapters():
    plan = load_plan()
    anchors = [anchor for item in plan["title_decisions"] for anchor in item["application_anchors"]]
    assert plan["approved_title_target_count"] == 264
    assert len(anchors) == plan["existing_title_executable_anchor_count"] == 261
    assert all(anchor["target_layer"] == "final/normalized_chapters" for anchor in anchors)
    assert all("baseline_normalized_chapter_file_sha256" in anchor for anchor in anchors)
    assert all("line_number" in anchor and "line_sha256" in anchor for anchor in anchors)
    assert all("title_text_span" in anchor for anchor in anchors)


def test_real_plan_physical_target_counts_and_editorial_counts_are_stable():
    plan = load_plan()
    assert plan["final_title_counts"] == {
        "ALREADY_CONSISTENT": 10,
        "APPROVED_CANONICAL": 71,
        "REVIEW_REQUIRED": 9,
        "STRUCTURAL_ONLY": 2,
    }
    assert plan["true_title_absent_units"] == [59, 109, 145]
    assert plan["ambiguous_title_units"] == []
    assert plan["recovery_review_required_target_count"] == 0
    assert plan["authorized_recovery_insertion_anchor_count"] == 3
    assert plan["authorized_recovery_insertion_units"] == [59, 109, 145]
    assert len(plan["suspect_title_target_audit"]) == 19


def test_unit_148_anchors_real_generator_wrapped_title_line():
    plan = load_plan()
    decision = next(item for item in plan["title_decisions"] if 148 in item["units"])
    anchor = next(item for item in decision["application_anchors"] if item["unit"] == 148)
    assert anchor["title_location_classification"] == "EXISTING_TITLE_GENERATOR_WRAPPED"
    assert anchor["exact_title_bearing_line"] == "Chapter 148: 46. The Great Cataclysm (2)"


def test_wrong_normalized_corpus_fingerprint_aborts_anchor_generation():
    wrong = dict(EXPECTED_NORMALIZED)
    wrong["aggregate_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="normalized title base fingerprint mismatch"):
        require_fingerprint(wrong, EXPECTED_NORMALIZED, "normalized title base")


def test_no_title_plan_operation_changes_chapter_prose():
    project = select_project("rogue_first_prince")

    baseline_dir = project.root / "final" / "chapters"
    normalized_dir = project.root / "final" / "normalized_chapters"

    if not baseline_dir.is_dir() or not normalized_dir.is_dir():
        pytest.skip("real Rogue final corpora are not present")

    before_baseline = corpus_fingerprint(baseline_dir, 351)
    before_normalized = corpus_fingerprint(normalized_dir, 351)

    plan = build_plan(project)

    after_baseline = corpus_fingerprint(baseline_dir, 351)
    after_normalized = corpus_fingerprint(normalized_dir, 351)

    assert before_baseline == after_baseline
    assert before_normalized == after_normalized

    assert {
        key: before_baseline[key]
        for key in EXPECTED_BASELINE
    } == EXPECTED_BASELINE

    assert {
        key: before_normalized[key]
        for key in EXPECTED_NORMALIZED
    } == EXPECTED_NORMALIZED

    assert plan["safety"]["applies_titles"] is False


def test_all_48_original_reviews_are_accounted_for_once():
    plan = load_plan()
    rows = plan["review_of_original_48"]
    assert len(rows) == 48
    assert len({row["group_identifier"] for row in rows}) == 48
    assert sum(row["decision"] == "PROMOTE_TO_APPROVED_CANONICAL" for row in rows) == 39
    assert sum(row["decision"] == "KEEP_REVIEW_REQUIRED" for row in rows) == 9


def test_normalized_fingerprint_is_authoritative_for_real_plan():
    plan = load_plan()
    actual = plan["corpora"]["normalized_title_base"]
    assert {key: actual[key] for key in EXPECTED_NORMALIZED} == EXPECTED_NORMALIZED

def test_epilogue_bare_explicit_part_stays_outside_editable_span():
    plan = load_plan()

    decision = next(
        item
        for item in plan["title_decisions"]
        if item["decision_id"] == "title-0087"
    )

    part = next(
        item
        for item in decision["unit_parts"]
        if item["final_unit"] == 334
    )

    target = discover_physical_title_target(
        "Epilogue 2\n\nBody",
        334,
        decision,
        part,
        "a" * 64,
    )

    assert target["title_target_status"] == "EXISTING_TITLE"
    assert target["title_text_span"] == {
        "start": 0,
        "end": len("Epilogue"),
        "exact_text": "Epilogue",
    }
    assert target["current_english_title_text"] == "Epilogue"
    assert target["explicit_part_number"] == 2
