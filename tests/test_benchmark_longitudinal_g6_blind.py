import pytest

import benchmark_longitudinal_g6_blind as blind


def test_blind_protocol_has_expected_pair_and_singleton_counts():
    assert blind.PAIR_COUNT == 16
    assert blind.SINGLETON_COUNT == 8
    assert len(blind.PLANNED_PAIRS) == 16
    assert len(blind.PLANNED_SINGLETONS) == 8


def test_rubric_weights_sum_to_one_and_match_historical_dimensions():
    assert abs(sum(blind.RUBRIC["weights"].values()) - 1.0) < 1e-12
    assert blind.DIMENSIONS == (
        "fidelity",
        "completeness",
        "terminology",
        "naturalness",
        "dialogue_tone",
        "relationships_pronouns",
        "cultural_technical",
    )


def test_weighted_score_rejects_missing_or_out_of_range_values():
    good = {dimension: 8.0 for dimension in blind.DIMENSIONS}
    assert blind.weighted_score(good) == 8.0

    bad = dict(good)
    bad["fidelity"] = 11
    with pytest.raises(blind.G6BlindProtocolError):
        blind.weighted_score(bad)

    missing = dict(good)
    del missing["terminology"]
    with pytest.raises(blind.G6BlindProtocolError):
        blind.weighted_score(missing)


def test_pair_score_validation_requires_independent_scores_and_preference():
    record = {}
    for side in ("a", "b"):
        for dimension in blind.DIMENSIONS:
            record[f"{side}_{dimension}"] = 8.0
        for flag in blind.DEFECT_FLAGS:
            record[f"{side}_{flag}"] = 0
    record["preference"] = "TIE"

    blind.validate_pair_score_record(record)

    record["preference"] = "LOW"
    with pytest.raises(blind.G6BlindProtocolError):
        blind.validate_pair_score_record(record)


def test_singleton_score_validation_works():
    record = {dimension: 7.5 for dimension in blind.DIMENSIONS}
    record.update({flag: 0 for flag in blind.DEFECT_FLAGS})
    blind.validate_singleton_score_record(record)


def test_effort_decision_hard_defects_override_score_advantage():
    result = blind.effort_decision(
        lower_scores=[8.0, 8.0, 8.0, 8.0],
        higher_scores=[9.0, 9.0, 9.0, 9.0],
        lower_hard_failure_flags=[0, 0, 0, 0],
        higher_hard_failure_flags=[0, 0, 1, 0],
        higher_preferences=4,
        lower_preferences=0,
    )
    assert result == "lower"


def test_effort_decision_selects_higher_only_on_preregistered_threshold():
    assert blind.effort_decision(
        lower_scores=[8.0, 8.0, 8.0, 8.0],
        higher_scores=[8.3, 8.2, 8.2, 8.1],
        lower_hard_failure_flags=[0, 0, 0, 0],
        higher_hard_failure_flags=[0, 0, 0, 0],
        higher_preferences=3,
        lower_preferences=1,
    ) == "higher"

    assert blind.effort_decision(
        lower_scores=[8.0, 8.0, 8.0, 8.0],
        higher_scores=[8.05, 8.05, 8.05, 8.05],
        lower_hard_failure_flags=[0, 0, 0, 0],
        higher_hard_failure_flags=[0, 0, 0, 0],
        higher_preferences=4,
        lower_preferences=0,
    ) == "lower"

    assert blind.effort_decision(
        lower_scores=[8.0, 8.0, 8.0, 8.0],
        higher_scores=[8.3, 8.3, 8.3, 8.3],
        lower_hard_failure_flags=[0, 0, 0, 0],
        higher_hard_failure_flags=[0, 0, 0, 0],
        higher_preferences=2,
        lower_preferences=2,
    ) == "lower"


def test_effort_decision_can_select_higher_when_lower_has_more_hard_defects():
    assert blind.effort_decision(
        lower_scores=[7.0, 7.0, 7.0, 7.0],
        higher_scores=[6.0, 6.0, 6.0, 6.0],
        lower_hard_failure_flags=[1, 0, 1, 0],
        higher_hard_failure_flags=[0, 0, 0, 0],
        higher_preferences=0,
        lower_preferences=4,
    ) == "higher"


def test_protocol_freeze_is_deterministic_and_does_not_authorize_provider_calls():
    identity = blind.blind_protocol_identity()
    assert blind.G6_BLIND_PROTOCOL_SHA256 == blind._sha256_json(identity)
    assert len(blind.G6_BLIND_PROTOCOL_SHA256) == 64
    assert identity["provider_calls_authorized"] is False
    assert identity["model_ranking_or_elimination_authorized"] is False


def test_private_mapping_is_nonce_bound_balanced_and_opaque():
    nonce = "01" * 32
    mapping = blind.derive_private_blind_mapping(nonce)

    assert mapping["mapping_nonce"] == nonce
    assert len(mapping["pairs"]) == 16
    assert len(mapping["singletons"]) == 8

    visible_pair_ids = [row["visible_pair_id"] for row in mapping["pairs"]]
    visible_singleton_ids = [
        row["visible_singleton_id"] for row in mapping["singletons"]
    ]
    assert visible_pair_ids == [f"G6P{index:03d}" for index in range(1, 17)]
    assert visible_singleton_ids == [f"G6S{index:03d}" for index in range(1, 9)]

    for model_id in blind.ADJUSTABLE_MODELS:
        rows = [
            row for row in mapping["pairs"]
            if row["benchmark_model_id"] == model_id
        ]
        assert len(rows) == 4
        lower_on_a = sum(
            1 for row in rows
            if row["side_a_effort"] == row["lower_effort"]
        )
        assert lower_on_a == 2

    commitment = blind.private_mapping_commitment(mapping)
    assert len(commitment) == 64


def test_private_mapping_changes_with_nonce():
    first = blind.derive_private_blind_mapping("01" * 32)
    second = blind.derive_private_blind_mapping("02" * 32)

    assert blind.private_mapping_commitment(first) != blind.private_mapping_commitment(second)


def test_private_mapping_rejects_invalid_nonce():
    with pytest.raises(blind.G6BlindProtocolError, match="nonce"):
        blind.derive_private_blind_mapping("not-secret")
