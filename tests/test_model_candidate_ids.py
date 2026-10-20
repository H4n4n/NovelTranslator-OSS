import pytest

from model_candidate_ids import (
    ModelCandidateIdError,
    build_model_candidate_id_map,
    model_facing_candidates,
    resolve_model_candidate_response,
)


def internal_id(number):
    return f"source:{number:064x}"


def response(*model_ids):
    return {
        "decisions": [
            {"candidate_id": model_id, "action": "keep"}
            for model_id in model_ids
        ],
        "issues": [],
    }


def test_twenty_internal_ids_map_to_c01_through_c20():
    mapping = build_model_candidate_id_map([internal_id(i) for i in range(1, 21)])
    assert list(mapping.alias_to_internal) == [f"c{i:02d}" for i in range(1, 21)]
    assert mapping.alias_to_internal["c09"] == internal_id(9)


def test_identical_order_produces_identical_mapping():
    values = [internal_id(3), internal_id(1), internal_id(2)]
    assert build_model_candidate_id_map(values) == build_model_candidate_id_map(values)


def test_mapping_is_deterministic_for_each_different_order():
    first = build_model_candidate_id_map([internal_id(1), internal_id(2)])
    second = build_model_candidate_id_map([internal_id(2), internal_id(1)])
    assert first.alias_to_internal["c01"] == internal_id(1)
    assert second.alias_to_internal["c01"] == internal_id(2)


def test_more_than_ninety_nine_candidates_extends_without_reformatting_prefix():
    mapping = build_model_candidate_id_map([internal_id(i) for i in range(1, 101)])
    assert mapping.alias_to_internal["c01"] == internal_id(1)
    assert mapping.alias_to_internal["c99"] == internal_id(99)
    assert mapping.alias_to_internal["c100"] == internal_id(100)


def test_exact_alias_coverage_resolves_to_durable_internal_ids():
    values = [internal_id(1), internal_id(2), internal_id(3)]
    mapping = build_model_candidate_id_map(values)
    resolved = resolve_model_candidate_response(response("c01", "c02", "c03"), mapping)
    assert [item["candidate_id"] for item in resolved["decisions"]] == values


@pytest.mark.parametrize(
    "bad_id",
    [
        "c99",
        "c1",
        "c001",
        "c021",
        " c01",
        "c01 ",
        internal_id(1),
        internal_id(999),
    ],
)
def test_unknown_typo_or_internal_id_is_never_interpreted(bad_id):
    mapping = build_model_candidate_id_map([internal_id(1), internal_id(2)])
    with pytest.raises(ModelCandidateIdError, match="outside this request"):
        resolve_model_candidate_response(response(bad_id, "c02"), mapping)


def test_duplicate_short_alias_is_rejected():
    mapping = build_model_candidate_id_map([internal_id(1), internal_id(2)])
    with pytest.raises(ModelCandidateIdError, match="duplicate model decision"):
        resolve_model_candidate_response(response("c01", "c01"), mapping)


def test_missing_short_alias_is_rejected():
    mapping = build_model_candidate_id_map([internal_id(1), internal_id(2)])
    with pytest.raises(ModelCandidateIdError, match="omitted request-local"):
        resolve_model_candidate_response(response("c01"), mapping)


def test_same_english_rendering_never_collapses_distinct_internal_identities():
    values = [internal_id(1), internal_id(2)]
    mapping = build_model_candidate_id_map(values)
    candidates = model_facing_candidates(
        [
            {"candidate_id": values[0], "chinese": "term-a", "english": "Same"},
            {"candidate_id": values[1], "chinese": "term-b", "english": "Same"},
        ],
        mapping,
    )
    assert [item["candidate_id"] for item in candidates] == ["c01", "c02"]
    resolved = resolve_model_candidate_response(response("c01", "c02"), mapping)
    assert [item["candidate_id"] for item in resolved["decisions"]] == values


def test_alias_outside_the_current_request_cannot_select_an_internal_candidate():
    first = build_model_candidate_id_map(
        [internal_id(1), internal_id(2), internal_id(3)]
    )
    second = build_model_candidate_id_map([internal_id(4), internal_id(5)])
    assert first.alias_to_internal["c03"] == internal_id(3)
    with pytest.raises(ModelCandidateIdError, match="outside this request"):
        resolve_model_candidate_response(response("c01", "c03"), second)


def test_model_facing_candidates_require_exact_internal_map_coverage():
    mapping = build_model_candidate_id_map([internal_id(1), internal_id(2)])
    with pytest.raises(ValueError, match="do not cover"):
        model_facing_candidates(
            [{"candidate_id": internal_id(1), "chinese": "term-a"}], mapping
        )
