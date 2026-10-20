"""Zero-provider focused tests for the G11 blind protocol.

Every test uses only synthetic in-memory data.  No test touches a real G11
execution or final-freeze root, reads a credential, constructs a Provider client,
sends a request, scores a real translation, or reveals a real identity.
"""

import builtins
import copy
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

import benchmark_longitudinal_g11_blind as blind
from benchmark_longitudinal_g11_preregistration import (
    DEEPSEEK_MODEL_ID,
    GLM_MODEL_ID,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET = "1a" * 32
OTHER_SECRET = "2b" * 32
SYNTHETIC_FREEZE_ID = "cd" * 32


def _sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _literary_records():
    rows = []
    for row in blind.literary_roster():
        pair_id = row["pair_id"]
        rows.append(
            {
                "pair_id": pair_id,
                "novel_id": row["novel_id"],
                "chapter": row["chapter"],
                "source_text": f"source::{pair_id}",
                "translations": {
                    GLM_MODEL_ID: f"alpha::{pair_id}",
                    DEEPSEEK_MODEL_ID: f"beta::{pair_id}",
                },
            }
        )
    return rows


def _reference_chapters(novel_id, window):
    return [
        {
            "chapter": chapter,
            "source_text": f"src::{novel_id}::{chapter}",
            "source_text_sha256": _sha_text(f"src::{novel_id}::{chapter}"),
        }
        for chapter in range(window[0], window[1] + 1)
    ]


def _longitudinal_records():
    rows = []
    for novel_id in blind.NOVEL_ORDER:
        for checkpoint in blind.LONGITUDINAL_CHECKPOINTS:
            window = blind.REFERENCE_WINDOWS[checkpoint]
            rows.append(
                {
                    "novel_id": novel_id,
                    "checkpoint": checkpoint,
                    "reference_window": [window[0], window[1]],
                    "reference_chapters": _reference_chapters(novel_id, window),
                    "tracks": {
                        GLM_MODEL_ID: {
                            "glossary": {},
                            "continuity": {"latest_chapter": checkpoint},
                            "issues": [],
                            "audit_history": [],
                        },
                        DEEPSEEK_MODEL_ID: {
                            "glossary": {},
                            "continuity": {"latest_chapter": checkpoint},
                            "issues": [],
                            "audit_history": [],
                        },
                    },
                }
            )
    return rows


def _key(secret=SECRET, final_freeze_id=SYNTHETIC_FREEZE_ID):
    return blind.build_private_identity_key(
        secret, final_freeze_id=final_freeze_id
    )


def _bundle(secret=SECRET):
    key = _key(secret)
    bundle = blind.build_public_blind_bundle(
        private_key=key,
        literary_records=_literary_records(),
        longitudinal_records=_longitudinal_records(),
    )
    return key, bundle


def _literary_scores(bundle):
    rows = []
    for case in bundle["literary_cases"]:
        side = {
            "scores": {name: 8.0 for name in blind.LITERARY_DIMENSIONS},
            "defect_flags": {name: 0 for name in blind.LITERARY_DEFECT_FLAGS},
            "notes": "blind review",
        }
        rows.append(
            {
                "visible_pair_id": case["visible_pair_id"],
                "side_a": copy.deepcopy(side),
                "side_b": copy.deepcopy(side),
                "preference": "TIE",
            }
        )
    return rows


def _longitudinal_scores(bundle):
    rows = []
    for case in bundle["longitudinal_cases"]:
        side = {
            "scores": {name: 7.5 for name in blind.LONGITUDINAL_DIMENSIONS},
            "notes": "blind state review",
        }
        rows.append(
            {
                "visible_track_pair_id": case["visible_track_pair_id"],
                "side_a": copy.deepcopy(side),
                "side_b": copy.deepcopy(side),
                "preference": "TIE",
            }
        )
    return rows


def _freeze(bundle):
    return blind.freeze_anonymous_scores(
        public_bundle=bundle,
        literary_score_rows=_literary_scores(bundle),
        longitudinal_score_rows=_longitudinal_scores(bundle),
    )


def _forbid(*_args, **_kwargs):
    raise AssertionError("the G11 blind protocol must not perform this operation")


# --------------------------------------------------------------------------- #
# Import safety
# --------------------------------------------------------------------------- #


def test_module_import_is_zero_write_zero_network_zero_credential(monkeypatch):
    work = Path(tempfile.mkdtemp(prefix="g11-blind-import-"))
    try:
        source = REPO_ROOT / "benchmark_longitudinal_g11_blind.py"
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"openai", "dotenv"}:
                raise AssertionError("G11 blind must not import the provider SDK")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        monkeypatch.setattr(os, "getenv", _forbid)
        monkeypatch.setattr(socket, "socket", _forbid)
        monkeypatch.setattr(socket, "create_connection", _forbid)
        monkeypatch.setattr(socket, "getaddrinfo", _forbid)
        monkeypatch.chdir(work)

        unique = f"_g11_blind_import_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(unique, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(unique, None)

        assert [path.name for path in work.rglob("*")] == []
        assert module.LITERARY_PAIR_COUNT == 28
        assert module.LONGITUDINAL_PAIR_COUNT == 8
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def test_contract_is_zero_provider_and_reveal_free():
    blind.assert_paid_execution_blocked()
    identity = blind.g11l_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["credential_access"] is False
    assert identity["scoring_performed"] is False
    assert identity["identity_revealed"] is False
    assert identity["paid_execution_authorized"] is False
    assert identity["literary"]["case_count"] == 28
    assert identity["literary"]["per_novel_count"] == 14
    assert identity["longitudinal"]["case_count"] == 8
    assert identity["longitudinal"]["checkpoints"] == [25, 50, 75, 100]
    assert identity["literary"]["within_novel_orientation_balanced_7_7"] is True
    assert identity["longitudinal"]["one_novel_each_orientation"] is True


def test_frozen_rubrics_and_statistics_policy():
    assert abs(sum(blind.LITERARY_RUBRIC["weights"].values()) - 1.0) < 1e-12
    assert abs(sum(blind.LONGITUDINAL_RUBRIC["weights"].values()) - 1.0) < 1e-12
    assert blind.LITERARY_DIMENSIONS == (
        "fidelity",
        "completeness",
        "terminology",
        "naturalness",
        "dialogue_tone",
        "relationships_pronouns",
        "cultural_technical",
    )
    assert blind.LITERARY_DEFECT_FLAGS == (
        "blocker",
        "omission_or_truncation",
        "invented_explanation",
        "terminology_inconsistency",
    )
    assert blind.LONGITUDINAL_DIMENSIONS == (
        "entity_name_continuity",
        "terminology_mechanics_rank_continuity",
        "factual_plot_continuity",
        "relationships_status_pronouns",
        "state_precision_hygiene",
        "audit_correction_quality_no_regression",
    )
    policy = blind.STATISTICS_POLICY
    assert policy["literary_pair_count"] == 28
    assert policy["primary_strata"] == {
        "ace_evolution": 14,
        "rogue_first_prince": 14,
    }
    assert policy["stratified_bootstrap_replicates"] == 10000
    assert policy["stratified_bootstrap_confidence"] == 0.95
    assert policy["all_literary_cases_are_primary"] is True
    assert policy["no_composite_overall_winner_score"] is True
    assert policy["longitudinal_inference"] == (
        "descriptive_two_novel_tracks_per_model_repeated_checkpoints"
    )
    assert policy["anonymous_preference_values"] == ["A", "B", "TIE"]


def test_exact_28_literary_and_8_state_roster():
    literary = blind.literary_roster()
    assert len(literary) == 28
    assert {row["novel_id"] for row in literary} == {"ace_evolution", "rogue_first_prince"}
    for novel_id in blind.NOVEL_ORDER:
        chapters = sorted(
            row["chapter"] for row in literary if row["novel_id"] == novel_id
        )
        assert chapters == list(blind.LITERARY_CHECKPOINTS)

    longitudinal = blind.longitudinal_roster()
    assert len(longitudinal) == 8
    for novel_id in blind.NOVEL_ORDER:
        checkpoints = sorted(
            row["checkpoint"]
            for row in longitudinal
            if row["novel_id"] == novel_id
        )
        assert checkpoints == [25, 50, 75, 100]


def test_private_orientation_is_deterministic_and_7_7_balanced_per_novel():
    first = _key()
    second = _key()
    assert first == second

    for novel_id in blind.NOVEL_ORDER:
        novel_a = [
            row["side_a_model_id"]
            for row in first["literary_mapping"]
            if row["novel_id"] == novel_id
        ]
        assert len(novel_a) == 14
        assert novel_a.count(GLM_MODEL_ID) == 7
        assert novel_a.count(DEEPSEEK_MODEL_ID) == 7


def test_longitudinal_orientation_is_stable_and_one_novel_each():
    key = _key()
    long_map = {
        row["novel_id"]: row for row in key["longitudinal_mapping"]
    }
    assert set(long_map) == {"ace_evolution", "rogue_first_prince"}
    assert all(
        tuple(row["checkpoints"]) == (25, 50, 75, 100)
        for row in key["longitudinal_mapping"]
    )
    assert sorted(row["side_a_model_id"] for row in key["longitudinal_mapping"]) == sorted(
        [GLM_MODEL_ID, DEEPSEEK_MODEL_ID]
    )


def test_different_secret_changes_orientation():
    first = _key(SECRET)
    second = _key(OTHER_SECRET)
    assert (
        first["literary_mapping"] != second["literary_mapping"]
        or first["longitudinal_mapping"] != second["longitudinal_mapping"]
    )


def test_private_key_commitment_and_validation():
    key = _key()
    blind.validate_private_identity_key(key)
    commitment = blind.private_identity_key_commitment(key)
    assert len(commitment) == 64
    assert "secret" not in json.dumps(key) or SECRET not in json.dumps(key)

    tampered = copy.deepcopy(key)
    tampered["literary_mapping"][0]["side_a_model_id"] = "not-a-model"
    with pytest.raises(blind.G11BlindProtocolError):
        blind.validate_private_identity_key(tampered)

    unbalanced = copy.deepcopy(key)
    for row in unbalanced["literary_mapping"]:
        row["side_a_model_id"] = GLM_MODEL_ID
        row["side_b_model_id"] = DEEPSEEK_MODEL_ID
    with pytest.raises(blind.G11BlindProtocolError, match="7/7"):
        blind.validate_private_identity_key(unbalanced)


def test_private_key_rejects_raw_secret_and_bad_freeze_id():
    with pytest.raises(blind.G11BlindProtocolError):
        blind.build_private_identity_key("not-hex")
    with pytest.raises(blind.G11BlindProtocolError):
        blind.build_private_identity_key(SECRET, final_freeze_id="zz" * 32)


def test_public_bundle_hides_models_effort_cost_transport_and_accounting():
    key, bundle = _bundle()
    blind.validate_public_blind_bundle(bundle)
    text = blind._canonical_json(bundle).casefold()
    assert GLM_MODEL_ID.casefold() not in text
    assert DEEPSEEK_MODEL_ID.casefold() not in text
    for literal in blind.FORBIDDEN_PUBLIC_EXACT_LITERALS:
        assert literal.casefold() not in text
    for forbidden in (
        "reasoning_effort",
        "credit_equivalent",
        "provider_model",
        "reservation_id",
        "request_sha",
    ):
        assert forbidden not in text
    assert bundle["private_identity_key_commitment_sha256"] == (
        blind.private_identity_key_commitment(key)
    )
    assert bundle["identity_revealed"] is False
    assert bundle["scoring_performed"] is False
    assert bundle["provider_calls"] == 0


def test_public_orientation_matches_private_mapping():
    key, bundle = _bundle()
    literary_map = {row["pair_id"]: row for row in key["literary_mapping"]}
    source = {
        row["pair_id"]: row for row in _literary_records()
    }
    for case in bundle["literary_cases"]:
        mapping = literary_map[case["visible_pair_id"]]
        expected_a = source[case["visible_pair_id"]]["translations"][
            mapping["side_a_model_id"]
        ]
        assert case["side_a"]["translation"] == expected_a


def test_longitudinal_orientation_stable_across_checkpoints():
    key, bundle = _bundle()
    long_map = {row["novel_id"]: row for row in key["longitudinal_mapping"]}
    source = {
        (row["novel_id"], row["checkpoint"]): row
        for row in _longitudinal_records()
    }
    for case in bundle["longitudinal_cases"]:
        mapping = long_map[case["novel_id"]]
        expected = source[(case["novel_id"], case["checkpoint"])]["tracks"][
            mapping["side_a_model_id"]
        ]
        assert case["side_a"] == expected
        assert case["side_a"] == {
            "glossary": {},
            "continuity": {"latest_chapter": case["checkpoint"]},
            "issues": [],
            "audit_history": [],
        }


def test_exact_forbidden_literal_leak_refusal():
    _key_, bundle = _bundle()
    for literal in blind.FORBIDDEN_PUBLIC_EXACT_LITERALS:
        leaked = copy.deepcopy(bundle)
        leaked["statistics_policy"]["note"] = literal
        with pytest.raises(blind.G11BlindProtocolError, match="literal identity"):
            blind.assert_no_public_identity_leak(leaked)


def test_bare_model_token_leak_outside_prose_refusal():
    _key_, bundle = _bundle()
    leaked = copy.deepcopy(bundle)
    leaked["statistics_policy"]["extra"] = "this mentions glm directly"
    with pytest.raises(blind.G11BlindProtocolError, match="model token"):
        blind.assert_no_public_identity_leak(leaked)


def test_bare_model_token_in_state_notes_refusal():
    _key_, bundle = _bundle()
    leaked = copy.deepcopy(bundle)
    leaked["longitudinal_cases"][0]["side_a"]["glossary"] = {
        "term": {
            "source_key": "term",
            "source_term": "term",
            "english": "term",
            "category": "name",
            "status": "approved",
            "approved_renderings": [],
            "alias_of": None,
            "notes": "DeepSeek preferred this rendering",
        }
    }
    with pytest.raises(blind.G11BlindProtocolError, match="model token"):
        blind.assert_no_public_identity_leak(leaked)


def test_forbidden_metadata_key_refusal():
    _key_, bundle = _bundle()
    leaked = copy.deepcopy(bundle)
    leaked["literary_cases"][0]["model_id"] = "x"
    with pytest.raises(blind.G11BlindProtocolError, match="forbidden"):
        blind.validate_public_blind_bundle(leaked)


def test_literary_record_validation_refusals():
    key = _key()
    records = _literary_records()
    records[0]["novel_id"] = "rogue_first_prince"
    with pytest.raises(blind.G11BlindProtocolError):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=records,
            longitudinal_records=_longitudinal_records(),
        )

    missing = _literary_records()
    missing[0]["translations"].pop(GLM_MODEL_ID)
    with pytest.raises(blind.G11BlindProtocolError):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=missing,
            longitudinal_records=_longitudinal_records(),
        )


def test_longitudinal_reference_window_validation():
    key = _key()
    records = _longitudinal_records()
    records[0]["reference_chapters"] = records[0]["reference_chapters"][:-1]
    with pytest.raises(blind.G11BlindProtocolError):
        blind.build_public_blind_bundle(
            private_key=key,
            literary_records=_literary_records(),
            longitudinal_records=records,
        )


def test_score_freeze_requires_all_28_literary_and_8_longitudinal_rows():
    _key_, bundle = _bundle()
    frozen = _freeze(bundle)
    blind.validate_score_freeze(bundle, frozen)
    assert frozen["identity_revealed"] is False
    assert frozen["model_ranking_or_elimination_performed"] is False
    assert frozen["provider_calls"] == 0
    assert len(frozen["literary_rows"]) == 28
    assert len(frozen["longitudinal_rows"]) == 8
    assert frozen["literary_rows"][0]["side_a"]["weighted_total"] == 8.0
    assert frozen["longitudinal_rows"][0]["side_a"]["weighted_total"] == 7.5
    assert len(frozen["score_freeze_id"]) == 64

    with pytest.raises(blind.G11BlindProtocolError, match="28 literary"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=_literary_scores(bundle)[:-1],
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )
    with pytest.raises(blind.G11BlindProtocolError, match="8 longitudinal"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=_literary_scores(bundle),
            longitudinal_score_rows=_longitudinal_scores(bundle)[:-1],
        )


def test_score_freeze_rejects_out_of_range_and_flags():
    _key_, bundle = _bundle()
    bad = _literary_scores(bundle)
    bad[0]["side_a"]["scores"][blind.LITERARY_DIMENSIONS[0]] = 10.1
    with pytest.raises(blind.G11BlindProtocolError, match="0..10"):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=bad,
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )

    bad_flags = _literary_scores(bundle)
    bad_flags[0]["side_a"]["defect_flags"] = {}
    with pytest.raises(blind.G11BlindProtocolError):
        blind.freeze_anonymous_scores(
            public_bundle=bundle,
            literary_score_rows=bad_flags,
            longitudinal_score_rows=_longitudinal_scores(bundle),
        )


def test_score_freeze_tamper_detection():
    _key_, bundle = _bundle()
    frozen = _freeze(bundle)
    frozen["literary_rows"][0]["preference"] = "A"
    with pytest.raises(blind.G11BlindProtocolError, match="does not recompute"):
        blind.validate_score_freeze(bundle, frozen)


def test_score_freeze_recomputed_id_cannot_hide_invalid_score_payload():
    _key_, bundle = _bundle()
    frozen = _freeze(bundle)
    frozen["literary_rows"][0]["side_a"]["scores"][
        blind.LITERARY_DIMENSIONS[0]
    ] = 99.0
    body = {key: value for key, value in frozen.items() if key != "score_freeze_id"}
    frozen["score_freeze_id"] = _sha_json(body)
    with pytest.raises(blind.G11BlindProtocolError, match="0..10"):
        blind.validate_score_freeze(bundle, frozen)


def test_reveal_refuses_wrong_private_commitment():
    _key_, bundle = _bundle()
    frozen = _freeze(bundle)
    wrong = _key(OTHER_SECRET)
    with pytest.raises(blind.G11BlindProtocolError, match="commitment"):
        blind.reveal_frozen_scores(
            public_bundle=bundle, score_freeze=frozen, private_key=wrong
        )


def test_reveal_requires_valid_score_freeze_then_maps_identities():
    key, bundle = _bundle()
    frozen = _freeze(bundle)
    tampered = copy.deepcopy(frozen)
    tampered["literary_rows"][0]["preference"] = "B"
    with pytest.raises(blind.G11BlindProtocolError):
        blind.reveal_frozen_scores(
            public_bundle=bundle, score_freeze=tampered, private_key=key
        )

    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle, score_freeze=frozen, private_key=key
    )
    blind.validate_identity_reveal(revealed)
    assert revealed["identity_revealed"] is True
    assert revealed["model_ranking_or_elimination_performed"] is False
    assert revealed["provider_calls"] == 0
    assert len(revealed["literary_rows"]) == 28
    assert len(revealed["longitudinal_rows"]) == 8
    assert len(revealed["reveal_id"]) == 64
    literary_map = {row["pair_id"]: row for row in key["literary_mapping"]}
    for row in revealed["literary_rows"]:
        mapping = literary_map[row["visible_pair_id"]]
        assert row["side_a_model_id"] == mapping["side_a_model_id"]
        assert row["side_b_model_id"] == mapping["side_b_model_id"]


def test_reveal_tamper_detection():
    key, bundle = _bundle()
    frozen = _freeze(bundle)
    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle, score_freeze=frozen, private_key=key
    )
    revealed["reveal_id"] = "0" * 64
    with pytest.raises(blind.G11BlindProtocolError, match="does not recompute"):
        blind.validate_identity_reveal(revealed)


def test_reveal_recomputed_id_cannot_hide_missing_rows():
    key, bundle = _bundle()
    frozen = _freeze(bundle)
    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle, score_freeze=frozen, private_key=key
    )
    revealed["literary_rows"] = revealed["literary_rows"][:-1]
    body = {key: value for key, value in revealed.items() if key != "reveal_id"}
    revealed["reveal_id"] = _sha_json(body)
    with pytest.raises(blind.G11BlindProtocolError, match="row count"):
        blind.validate_identity_reveal(revealed)


def test_score_and_reveal_make_no_provider_calls(monkeypatch):
    import benchmark_longitudinal_provider as provider

    monkeypatch.setattr(provider, "create_provider_client", _forbid)
    monkeypatch.setattr(provider, "send_chat_completion_once", _forbid)

    key, bundle = _bundle()
    frozen = _freeze(bundle)
    revealed = blind.reveal_frozen_scores(
        public_bundle=bundle, score_freeze=frozen, private_key=key
    )
    assert frozen["provider_calls"] == 0
    assert revealed["provider_calls"] == 0
