import copy

import pytest

import benchmark_longitudinal_g10_blind as blind
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    LITERARY_RUBRIC,
    LONGITUDINAL_STATE_RUBRIC,
    QWEN_MODEL_ID,
)


SECRET = "1a" * 32


def _literary_records():
    rows = []
    for pair in blind.PAIR_SPECS:
        source = f"source::{pair.novel_id}::{pair.chapter}"
        rows.append(
            {
                "pair_id": pair.pair_id,
                "case_kind": pair.case_kind,
                "novel_id": pair.novel_id,
                "chapter": pair.chapter,
                "source_text": source,
                "translations": {
                    QWEN_MODEL_ID: f"Q::{pair.pair_id}",
                    GLM_MODEL_ID: f"G::{pair.pair_id}",
                },
            }
        )
    return rows


def _longitudinal_records():
    rows = []
    for novel_id in sorted(blind.NOVELS):
        for checkpoint in blind.LONGITUDINAL_CHECKPOINTS:
            rows.append(
                {
                    "novel_id": novel_id,
                    "checkpoint": checkpoint,
                    "audit_window": [1, 25] if checkpoint == 25 else [26, 50],
                    "reference_evidence": {
                        "source_window": f"{novel_id}:{checkpoint}",
                        "audit_boundary": checkpoint,
                    },
                    "tracks": {
                        QWEN_MODEL_ID: {
                            "pre_audit_state": f"Q-pre-{novel_id}-{checkpoint}",
                            "audit_decisions": f"Q-audit-{novel_id}-{checkpoint}",
                            "post_audit_state": f"Q-post-{novel_id}-{checkpoint}",
                            "retroactive_fixes": [],
                        },
                        GLM_MODEL_ID: {
                            "pre_audit_state": f"G-pre-{novel_id}-{checkpoint}",
                            "audit_decisions": f"G-audit-{novel_id}-{checkpoint}",
                            "post_audit_state": f"G-post-{novel_id}-{checkpoint}",
                            "retroactive_fixes": [],
                        },
                    },
                }
            )
    return rows


def _bundle():
    key = blind.build_private_identity_key(SECRET)
    bundle = blind.build_public_blind_bundle(
        private_key=key,
        literary_records=_literary_records(),
        longitudinal_records=_longitudinal_records(),
    )
    return key, bundle


def _literary_scores(bundle):
    rows = []
    for pair in bundle["literary_pairs"]:
        side = {
            "scores": {name: 8.0 for name in blind.LITERARY_DIMENSIONS},
            "defect_flags": {name: 0 for name in blind.LITERARY_DEFECT_FLAGS},
            "notes": "blind review",
        }
        rows.append(
            {
                "visible_pair_id": pair["visible_pair_id"],
                "side_a": copy.deepcopy(side),
                "side_b": copy.deepcopy(side),
                "preference": "TIE",
            }
        )
    return rows


def _longitudinal_scores(bundle):
    rows = []
    for pair in bundle["longitudinal_pairs"]:
        side = {
            "scores": {name: 7.5 for name in blind.LONGITUDINAL_DIMENSIONS},
            "notes": "blind state review",
        }
        rows.append(
            {
                "visible_track_pair_id": pair["visible_track_pair_id"],
                "side_a": copy.deepcopy(side),
                "side_b": copy.deepcopy(side),
                "preference": "TIE",
            }
        )
    return rows


def test_contract_is_zero_provider_and_paid_execution_blocked():
    blind.assert_paid_execution_blocked()
    identity = blind.g10c_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["literary"]["pair_count"] == 51
    assert identity["literary"]["primary_pair_count"] == 48
    assert identity["longitudinal"]["pair_count"] == 4
    assert identity["longitudinal"]["checkpoints"] == (25, 50)
    assert identity["availability_policy"]["all_planned_slots_preserved"] is True
    assert identity["availability_policy"]["only_fully_comparable_pairs_are_scoreable"] is True
    assert identity["availability_policy"]["unavailable_slots_receive_no_quality_score"] is True
    assert identity["availability_policy"]["missingness_is_reliability_not_quality"] is True


def test_private_orientation_is_deterministic_and_balanced():
    first = blind.build_private_identity_key(SECRET)
    second = blind.build_private_identity_key(SECRET)
    assert first == second

    primary = [
        row
        for row in first["literary_mapping"]
        if row["case_kind"] == "primary"
    ]
    a_models = [row["side_a_model_id"] for row in primary]
    assert a_models.count(QWEN_MODEL_ID) == 24
    assert a_models.count(GLM_MODEL_ID) == 24

    long_a = {
        row["novel_id"]: row["side_a_model_id"]
        for row in first["longitudinal_mapping"]
    }
    assert sorted(long_a.values()) == sorted([QWEN_MODEL_ID, GLM_MODEL_ID])
    assert all(row["checkpoints"] == [25, 50] for row in first["longitudinal_mapping"])


def test_different_secret_changes_private_mapping():
    first = blind.build_private_identity_key(SECRET)
    second = blind.build_private_identity_key("2b" * 32)
    assert (
        first["literary_mapping"] != second["literary_mapping"]
        or first["longitudinal_mapping"] != second["longitudinal_mapping"]
    )


def test_public_bundle_hides_models_effort_cost_transport_and_accounting():
    key, bundle = _bundle()
    blind.validate_public_blind_bundle(bundle)
    text = blind._canonical_json(bundle).casefold()
    assert QWEN_MODEL_ID.casefold() not in text
    assert GLM_MODEL_ID.casefold() not in text
    assert "qwen_win" not in text
    assert "glm_win" not in text
    assert '"qwen"' not in text
    assert '"glm"' not in text
    for forbidden in (
        "reasoning_effort",
        "credit_equivalent",
        "cost_usd",
        "provider_model",
        "request_sha",
        "reservation_id",
    ):
        assert forbidden not in text
    assert bundle["private_identity_key_commitment_sha256"] == (
        blind.private_identity_key_commitment(key)
    )


def test_longitudinal_public_orientation_is_stable_across_checkpoints():
    key, bundle = _bundle()
    private_map = {
        row["novel_id"]: row for row in key["longitudinal_mapping"]
    }
    source = {
        (row["novel_id"], row["checkpoint"]): row
        for row in _longitudinal_records()
    }
    for public in bundle["longitudinal_pairs"]:
        novel_id = public["novel_id"]
        checkpoint = public["checkpoint"]
        mapping = private_map[novel_id]
        expected_a = source[(novel_id, checkpoint)]["tracks"][
            mapping["side_a_model_id"]
        ]
        assert public["side_a"] == expected_a


def test_public_bundle_rejects_identity_metadata_inside_longitudinal_evidence():
    key = blind.build_private_identity_key(SECRET)
    records = _longitudinal_records()
    records[0]["tracks"][QWEN_MODEL_ID]["provider_model_id"] = "secret"
    with pytest.raises(blind.G10BlindProtocolError, match="forbidden"):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=_literary_records(),
            longitudinal_records=records,
        )


def test_score_freeze_validates_all_rows_and_stays_anonymous():
    key, bundle = _bundle()
    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )
    blind.validate_score_freeze(bundle, frozen)
    assert frozen["identity_revealed"] is False
    assert frozen["model_ranking_or_elimination_performed"] is False
    assert frozen["provider_calls"] == 0
    assert len(frozen["literary_rows"]) == 51
    assert len(frozen["longitudinal_rows"]) == 4
    assert len(frozen["score_freeze_id"]) == 64
    assert frozen["literary_rows"][0]["side_a"]["weighted_total"] == 8.0
    assert frozen["longitudinal_rows"][0]["side_a"]["weighted_total"] == 7.5
    assert blind.private_identity_key_commitment(key) == (
        frozen["private_identity_key_commitment_sha256"]
    )


def test_score_freeze_rejects_missing_rows_and_bad_scores():
    _key, bundle = _bundle()
    literary = _literary_scores(bundle)
    with pytest.raises(blind.G10BlindProtocolError, match="row count"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=literary[:-1],
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )

    bad = _literary_scores(bundle)
    first_dimension = blind.LITERARY_DIMENSIONS[0]
    bad[0]["side_a"]["scores"][first_dimension] = 10.1
    with pytest.raises(blind.G10BlindProtocolError, match="0..10"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=bad,
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )


def test_reveal_is_impossible_with_wrong_private_key():
    key, bundle = _bundle()
    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )
    wrong = blind.build_private_identity_key("2b" * 32)
    with pytest.raises(blind.G10BlindProtocolError, match="commitment"):
        blind.reveal_frozen_scores(
            public_bundle=bundle,
            score_freeze=frozen,
            private_key=wrong,
        )


def test_reveal_occurs_only_after_valid_score_freeze_and_does_not_rank():
    key, bundle = _bundle()
    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )
    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle,
        score_freeze=frozen,
        private_key=key,
    )
    assert revealed["identity_revealed"] is True
    assert revealed["model_ranking_or_elimination_performed"] is False
    assert revealed["provider_calls"] == 0
    assert len(revealed["literary_rows"]) == 51
    assert len(revealed["longitudinal_rows"]) == 4
    assert len(revealed["reveal_id"]) == 64
    assert {
        revealed["literary_rows"][0]["side_a_model_id"],
        revealed["literary_rows"][0]["side_b_model_id"],
    } == {QWEN_MODEL_ID, GLM_MODEL_ID}


def test_rubrics_are_exactly_preregistered():
    identity = blind.g10c_contract_identity()
    assert identity["literary"]["rubric"] == LITERARY_RUBRIC
    assert identity["longitudinal"]["rubric"] == LONGITUDINAL_STATE_RUBRIC
    assert abs(sum(LITERARY_RUBRIC["weights"].values()) - 1.0) < 1e-12
    assert abs(sum(LONGITUDINAL_STATE_RUBRIC["weights"].values()) - 1.0) < 1e-12


def test_unavailable_literary_slot_is_preserved_but_never_scored():
    key = blind.build_private_identity_key(SECRET)
    literary = _literary_records()
    unavailable = literary[0]
    unavailable["availability"] = blind.LITERARY_UNAVAILABLE
    unavailable["translations"] = {
        QWEN_MODEL_ID: unavailable["translations"][QWEN_MODEL_ID],
    }
    bundle = blind.build_public_blind_bundle(
        private_key=key,
        literary_records=literary,
        longitudinal_records=_longitudinal_records(),
    )
    slot = bundle["literary_pairs"][0]
    assert slot["availability"] == blind.LITERARY_UNAVAILABLE
    assert slot["score_record_required"] is False
    assert "side_a" not in slot
    assert "side_b" not in slot

    scores = _literary_scores(bundle)[1:]
    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=scores,
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )
    assert frozen["literary_unavailable_pair_ids"] == [
        slot["visible_pair_id"]
    ]
    assert frozen["literary_primary_scoreable_pair_count"] == 47
    assert frozen["unavailable_slots_are_not_quality_penalties"] is True

    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle,
        score_freeze=frozen,
        private_key=key,
    )
    unavailable_revealed = revealed["literary_unavailable_rows"]
    assert len(unavailable_revealed) == 1
    assert unavailable_revealed[0]["quality_score"] is None
    assert {
        unavailable_revealed[0]["side_a_model_id"],
        unavailable_revealed[0]["side_b_model_id"],
    } == {QWEN_MODEL_ID, GLM_MODEL_ID}


def test_unavailable_longitudinal_checkpoint_is_preserved_but_never_scored():
    key = blind.build_private_identity_key(SECRET)
    longitudinal = _longitudinal_records()
    unavailable = longitudinal[-1]
    unavailable["availability"] = blind.LONGITUDINAL_UNAVAILABLE
    qwen_only = unavailable["tracks"][QWEN_MODEL_ID]
    unavailable["tracks"] = {QWEN_MODEL_ID: qwen_only}

    bundle = blind.build_public_blind_bundle(
        private_key=key,
        literary_records=_literary_records(),
        longitudinal_records=longitudinal,
    )
    slot = bundle["longitudinal_pairs"][-1]
    assert slot["availability"] == blind.LONGITUDINAL_UNAVAILABLE
    assert slot["score_record_required"] is False
    assert "side_a" not in slot
    assert "side_b" not in slot

    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle)[:-1],
    )
    assert frozen["longitudinal_unavailable_pair_ids"] == [
        slot["visible_track_pair_id"]
    ]
    assert frozen["longitudinal_scoreable_pair_count"] == 3

    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle,
        score_freeze=frozen,
        private_key=key,
    )
    unavailable_revealed = revealed["longitudinal_unavailable_rows"]
    assert len(unavailable_revealed) == 1
    assert unavailable_revealed[0]["quality_score"] is None


def test_unavailable_slot_cannot_hide_a_complete_pair():
    key = blind.build_private_identity_key(SECRET)
    literary = _literary_records()
    literary[0]["availability"] = blind.LITERARY_UNAVAILABLE
    with pytest.raises(blind.G10BlindProtocolError, match="unexpectedly has both"):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=literary,
            longitudinal_records=_longitudinal_records(),
        )

    longitudinal = _longitudinal_records()
    longitudinal[0]["availability"] = blind.LONGITUDINAL_UNAVAILABLE
    with pytest.raises(blind.G10BlindProtocolError, match="unexpectedly has both"):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=_literary_records(),
            longitudinal_records=longitudinal,
        )


def test_unavailable_slot_cannot_be_submitted_for_blind_scoring():
    key = blind.build_private_identity_key(SECRET)
    literary = _literary_records()
    literary[0]["availability"] = blind.LITERARY_UNAVAILABLE
    literary[0]["translations"] = {}
    bundle = blind.build_public_blind_bundle(
        private_key=key,
        literary_records=literary,
        longitudinal_records=_longitudinal_records(),
    )
    with pytest.raises(blind.G10BlindProtocolError, match="row count"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=_literary_scores(bundle),
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )


def test_score_freeze_tampering_is_detected():
    _key, bundle = _bundle()
    frozen = blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )
    frozen["literary_rows"][0]["preference"] = "A"
    with pytest.raises(blind.G10BlindProtocolError, match="does not recompute"):
        blind.validate_score_freeze(bundle, frozen)
