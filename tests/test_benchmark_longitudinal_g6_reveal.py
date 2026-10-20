import hashlib
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g6_reveal as reveal
from benchmark_longitudinal_g6_blind import (
    ADJUSTABLE_MODELS,
    DEFAULT_ONLY_MODELS,
    DEFECT_FLAGS,
    DIMENSIONS,
    derive_private_blind_mapping,
    private_mapping_commitment,
)


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_private_key(tmp_path):
    nonce = "1" * 64
    derived = derive_private_blind_mapping(nonce)

    private_pairs = []
    for index, row in enumerate(derived["pairs"], start=1):
        private_pairs.append(
            {
                **row,
                "source_id": f"SRC{((index - 1) % 4) + 1}",
                "side_a_case_id": f"CASEA{index:02d}",
                "side_b_case_id": f"CASEB{index:02d}",
                "side_a_result_sha256": f"{index:064x}"[-64:],
                "side_b_result_sha256": f"{index + 100:064x}"[-64:],
            }
        )

    private_singletons = []
    for index, row in enumerate(derived["singletons"], start=1):
        available = index <= 6
        entry = {
            **row,
            "source_id": f"SRC{((index - 1) % 4) + 1}",
            "case_id": f"SCASE{index:02d}",
            "journal_sha256": f"{index + 200:064x}"[-64:],
            "result_sha256": f"{index + 300:064x}"[-64:],
            "availability": (
                "translation_available"
                if available
                else "terminal_provider_unavailable"
            ),
        }
        if not available:
            entry["http_status"] = 524
            entry["provider_rejection_reasons"] = ["terminal_http_524"]
        private_singletons.append(entry)

    private_key = {
        "schema_version": 1,
        "revision": "fixture",
        "repository_anchor": "fixture",
        "g6_calibration_freeze_sha256": "a" * 64,
        "g6_blind_protocol_sha256": reveal.G6_BLIND_PROTOCOL_SHA256,
        "execution_plan_sha256": "b" * 64,
        "r6_manifest_sha256": "c" * 64,
        "mapping_nonce": nonce,
        "pairs": private_pairs,
        "singletons": private_singletons,
        "durable_evidence_inventory": [],
        "durable_evidence_inventory_sha256": "d" * 64,
    }

    return private_key


def _pair_score(identity, index):
    row = {
        "visible_pair_id": identity["visible_pair_id"],
        "source_id": identity["source_id"],
        "preference": ("A", "B", "TIE")[(index - 1) % 3],
        "notes": f"pair {index}",
        "a_weighted_score": 7.0 + (index % 3) * 0.1,
        "b_weighted_score": 7.2 + (index % 2) * 0.1,
    }
    for side in ("a", "b"):
        for dim in DIMENSIONS:
            row[f"{side}_{dim}"] = 7.0
        for flag in DEFECT_FLAGS:
            row[f"{side}_{flag}"] = 0
    return row


def _singleton_score(identity, index):
    row = {
        "visible_singleton_id": identity["visible_singleton_id"],
        "source_id": identity["source_id"],
        "notes": f"singleton {index}",
        "weighted_score": 8.0,
    }
    for dim in DIMENSIONS:
        row[dim] = 8.0
    for flag in DEFECT_FLAGS:
        row[flag] = 0
    return row


def _build_score_fixture(tmp_path, monkeypatch, private_key):
    root = tmp_path / "G6J_SCORE_FREEZE"
    root.mkdir()

    pair_rows = [
        _pair_score(identity, index)
        for index, identity in enumerate(private_key["pairs"], start=1)
    ]

    available = [
        row
        for row in private_key["singletons"]
        if row["availability"] == "translation_available"
    ]
    unavailable = [
        row
        for row in private_key["singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]
    singleton_rows = [
        _singleton_score(identity, index)
        for index, identity in enumerate(available, start=1)
    ]

    score_doc = {
        "schema_version": 1,
        "revision": "fixture",
        "status": "anonymous_scores_frozen",
        "g6i_public_bundle_sha256": "e" * 64,
        "g6i_manifest_sha256": "f" * 64,
        "g6i_private_identity_key_commitment_sha256": private_mapping_commitment(
            private_key
        ),
        "g6_blind_protocol_sha256": reveal.G6_BLIND_PROTOCOL_SHA256,
        "pair_score_sheet_sha256": "1" * 64,
        "singleton_score_sheet_sha256": "2" * 64,
        "rubric": {},
        "pair_rows": pair_rows,
        "singleton_rows": singleton_rows,
        "terminal_provider_unavailable_singletons": [
            {
                "visible_singleton_id": row["visible_singleton_id"],
                "source_id": row["source_id"],
                "availability": "terminal_provider_unavailable",
                "score_record_required": False,
            }
            for row in unavailable
        ],
        "pair_count": 16,
        "scored_singleton_count": 6,
        "unavailable_singleton_count": 2,
        "provider_calls": 0,
        "identity_revealed": False,
        "effort_selection_performed": False,
        "model_ranking_or_elimination_performed": False,
    }
    score_doc["score_freeze_id"] = reveal._sha_json(score_doc)

    pair_path = root / reveal.PAIR_SCORE_FILENAME
    singleton_path = root / reveal.SINGLETON_SCORE_FILENAME
    score_path = root / reveal.SCORE_JSON_FILENAME

    pair_path.write_bytes(b"pair scores\n")
    singleton_path.write_bytes(b"singleton scores\n")
    _write_json(score_path, score_doc)

    pair_sha = _sha_bytes(pair_path.read_bytes())
    singleton_sha = _sha_bytes(singleton_path.read_bytes())
    score_sha = _sha_bytes(score_path.read_bytes())
    tree = reveal._historical_windows_tree_identity(root)

    monkeypatch.setattr(reveal, "G6J_PAIR_SCORE_SHA256", pair_sha)
    monkeypatch.setattr(reveal, "G6J_SINGLETON_SCORE_SHA256", singleton_sha)
    monkeypatch.setattr(reveal, "G6J_FROZEN_JSON_SHA256", score_sha)
    monkeypatch.setattr(reveal, "G6J_SCORE_FREEZE_ID", score_doc["score_freeze_id"])
    monkeypatch.setattr(reveal, "G6J_EVIDENCE_TREE_SHA256", tree["tree_sha256"])

    # Rebind score document sheet hashes after the fixture files exist.
    score_doc["pair_score_sheet_sha256"] = pair_sha
    score_doc["singleton_score_sheet_sha256"] = singleton_sha
    score_doc.pop("score_freeze_id")
    score_doc["score_freeze_id"] = reveal._sha_json(score_doc)
    _write_json(score_path, score_doc)
    score_sha = _sha_bytes(score_path.read_bytes())
    tree = reveal._historical_windows_tree_identity(root)

    monkeypatch.setattr(reveal, "G6J_FROZEN_JSON_SHA256", score_sha)
    monkeypatch.setattr(reveal, "G6J_SCORE_FREEZE_ID", score_doc["score_freeze_id"])
    monkeypatch.setattr(reveal, "G6J_EVIDENCE_TREE_SHA256", tree["tree_sha256"])

    return root, score_doc


def _build_g6i_fixture(tmp_path, monkeypatch, private_key):
    root = tmp_path / "G6I"
    private_path = root / reveal.PRIVATE_KEY_RELATIVE
    _write_json(private_path, private_key)

    private_sha = _sha_bytes(private_path.read_bytes())
    commitment = private_mapping_commitment(private_key)

    monkeypatch.setattr(reveal, "G6I_PRIVATE_KEY_FILE_SHA256", private_sha)
    monkeypatch.setattr(reveal, "G6I_PRIVATE_KEY_COMMITMENT_SHA256", commitment)
    monkeypatch.setattr(reveal, "G6I_R6_MANIFEST_SHA256", "c" * 64)
    monkeypatch.setattr(reveal, "G6I_DURABLE_INVENTORY_SHA256", "d" * 64)

    bundle = {
        "pairs": [
            {"visible_pair_id": row["visible_pair_id"]}
            for row in private_key["pairs"]
        ],
        "singletons": [
            {"visible_singleton_id": row["visible_singleton_id"]}
            for row in private_key["singletons"]
        ],
    }
    manifest = {
        "private_identity_key_file_sha256": private_sha,
        "r6_manifest_sha256": "c" * 64,
        "durable_evidence_inventory_sha256": "d" * 64,
    }

    monkeypatch.setattr(
        reveal,
        "load_frozen_public_bundle",
        lambda _root: (bundle, manifest, {}),
    )
    return root


def test_frozen_successful_g6j_bindings_are_exact():
    assert reveal.G6J_SCORE_FREEZE_ID == (
        "a97aa9f854de6472daaa23719523b43a88d8275182fcc553b04d12e730d6e7b6"
    )
    assert reveal.G6J_FROZEN_JSON_SHA256 == (
        "dda073a832c497db33782bedd6c145cfc96cc2230cd59cca6edd834803fd1d87"
    )
    assert reveal.G6J_EVIDENCE_TREE_SHA256 == (
        "c1726137d2328c7b08eb1c864724383e4010ea6d2aaef32a93052e84f906e524"
    )
    assert reveal.G6I_PRIVATE_KEY_FILE_SHA256 == (
        "11076404ce7825fcdbd55f6f0a309719e14f3618c9b95f96dd805e63d2271e66"
    )


def test_score_freeze_must_verify_before_private_key_access(tmp_path, monkeypatch):
    called = {"private": False}

    def trap(*args, **kwargs):
        called["private"] = True
        raise AssertionError("private key must not be touched")

    monkeypatch.setattr(reveal, "open_and_verify_private_key", trap)

    score_root = tmp_path / "bad_scores"
    score_root.mkdir()
    g6i_root = tmp_path / "g6i"
    g6i_root.mkdir()

    with pytest.raises(reveal.G6KRevealError):
        reveal.reveal_and_freeze(
            g6i_root=g6i_root,
            score_root=score_root,
            output_root=tmp_path / "out",
        )

    assert called["private"] is False


def test_private_mapping_rejects_tampered_nonce_mapping(tmp_path, monkeypatch):
    private_key = _build_private_key(tmp_path)
    g6i_root = _build_g6i_fixture(tmp_path, monkeypatch, private_key)

    expected_pairs = [row["visible_pair_id"] for row in private_key["pairs"]]
    expected_singletons = [
        row["visible_singleton_id"] for row in private_key["singletons"]
    ]

    loaded = reveal.open_and_verify_private_key(
        g6i_root,
        expected_visible_pairs=expected_pairs,
        expected_visible_singletons=expected_singletons,
    )
    assert loaded["mapping_nonce"] == "1" * 64

    tampered = dict(private_key)
    tampered["pairs"] = [dict(row) for row in private_key["pairs"]]
    tampered["pairs"][0]["side_a_effort"] = "tampered"
    _write_json(g6i_root / reveal.PRIVATE_KEY_RELATIVE, tampered)

    tampered_sha = _sha_bytes(
        (g6i_root / reveal.PRIVATE_KEY_RELATIVE).read_bytes()
    )
    tampered_commitment = private_mapping_commitment(tampered)
    monkeypatch.setattr(reveal, "G6I_PRIVATE_KEY_FILE_SHA256", tampered_sha)
    monkeypatch.setattr(
        reveal,
        "G6I_PRIVATE_KEY_COMMITMENT_SHA256",
        tampered_commitment,
    )

    with pytest.raises(reveal.G6KRevealError, match="nonce derivation"):
        reveal.open_and_verify_private_key(
            g6i_root,
            expected_visible_pairs=expected_pairs,
            expected_visible_singletons=expected_singletons,
        )


def test_reconcile_preserves_unavailable_singletons_without_scores(tmp_path):
    private_key = _build_private_key(tmp_path)
    pair_rows = [
        _pair_score(identity, index)
        for index, identity in enumerate(private_key["pairs"], start=1)
    ]
    available = [
        row
        for row in private_key["singletons"]
        if row["availability"] == "translation_available"
    ]
    unavailable = [
        row
        for row in private_key["singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]
    scores = {
        "pair_rows": pair_rows,
        "singleton_rows": [
            _singleton_score(identity, index)
            for index, identity in enumerate(available, start=1)
        ],
        "terminal_provider_unavailable_singletons": [
            {
                "visible_singleton_id": row["visible_singleton_id"],
                "source_id": row["source_id"],
            }
            for row in unavailable
        ],
    }

    result = reveal.reconcile_revealed_scores(
        scores=scores,
        private_key=private_key,
    )

    assert len(result["revealed_pairs"]) == 16
    assert len(result["effort_decisions"]) == 4
    assert len(result["revealed_singletons"]) == 8
    assert len(result["default_only_summaries"]) == 2

    terminal = [
        row
        for row in result["revealed_singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]
    assert len(terminal) == 2
    assert all(row["literary_score"] is None for row in terminal)
    assert all(row["score_record_required"] is False for row in terminal)


def test_effort_decisions_exist_only_for_adjustable_models(tmp_path):
    private_key = _build_private_key(tmp_path)
    scores = {
        "pair_rows": [
            _pair_score(identity, index)
            for index, identity in enumerate(private_key["pairs"], start=1)
        ],
        "singleton_rows": [
            _singleton_score(identity, index)
            for index, identity in enumerate(
                [
                    row
                    for row in private_key["singletons"]
                    if row["availability"] == "translation_available"
                ],
                start=1,
            )
        ],
        "terminal_provider_unavailable_singletons": [
            {
                "visible_singleton_id": row["visible_singleton_id"],
                "source_id": row["source_id"],
            }
            for row in private_key["singletons"]
            if row["availability"] == "terminal_provider_unavailable"
        ],
    }

    result = reveal.reconcile_revealed_scores(
        scores=scores,
        private_key=private_key,
    )

    assert {
        row["benchmark_model_id"]
        for row in result["effort_decisions"]
    } == set(ADJUSTABLE_MODELS)
    assert {
        row["benchmark_model_id"]
        for row in result["default_only_summaries"]
    } == set(DEFAULT_ONLY_MODELS)
    assert all(
        row["effort_decision_applicable"] is False
        for row in result["default_only_summaries"]
    )


def test_end_to_end_reveal_freezes_without_model_ranking(tmp_path, monkeypatch):
    private_key = _build_private_key(tmp_path)
    score_root, score_doc = _build_score_fixture(
        tmp_path,
        monkeypatch,
        private_key,
    )
    # Score fixture commitment must track the exact private key.
    commitment = private_mapping_commitment(private_key)
    score_doc["g6i_private_identity_key_commitment_sha256"] = commitment
    score_doc.pop("score_freeze_id")
    score_doc["score_freeze_id"] = reveal._sha_json(score_doc)
    score_path = score_root / reveal.SCORE_JSON_FILENAME
    _write_json(score_path, score_doc)

    score_sha = _sha_bytes(score_path.read_bytes())
    tree = reveal._historical_windows_tree_identity(score_root)
    monkeypatch.setattr(reveal, "G6J_FROZEN_JSON_SHA256", score_sha)
    monkeypatch.setattr(reveal, "G6J_SCORE_FREEZE_ID", score_doc["score_freeze_id"])
    monkeypatch.setattr(reveal, "G6J_EVIDENCE_TREE_SHA256", tree["tree_sha256"])
    monkeypatch.setattr(
        reveal,
        "G6I_PRIVATE_KEY_COMMITMENT_SHA256",
        commitment,
    )

    g6i_root = _build_g6i_fixture(tmp_path, monkeypatch, private_key)
    output = tmp_path / "G6K"

    result = reveal.reveal_and_freeze(
        g6i_root=g6i_root,
        score_root=score_root,
        output_root=output,
    )

    assert result["revealed"]["identity_revealed"] is True
    assert result["revealed"]["effort_selection_performed"] is True
    assert (
        result["revealed"]["model_ranking_or_elimination_performed"]
        is False
    )
    assert result["revealed"]["provider_calls"] == 0
    assert len(result["revealed"]["effort_decisions"]) == 4
    assert len(list(output.iterdir())) == 3


def test_output_root_is_never_overwritten(tmp_path, monkeypatch):
    private_key = _build_private_key(tmp_path)
    score_root, score_doc = _build_score_fixture(
        tmp_path,
        monkeypatch,
        private_key,
    )
    commitment = private_mapping_commitment(private_key)
    score_doc["g6i_private_identity_key_commitment_sha256"] = commitment
    score_doc.pop("score_freeze_id")
    score_doc["score_freeze_id"] = reveal._sha_json(score_doc)
    score_path = score_root / reveal.SCORE_JSON_FILENAME
    _write_json(score_path, score_doc)
    monkeypatch.setattr(
        reveal,
        "G6J_FROZEN_JSON_SHA256",
        _sha_bytes(score_path.read_bytes()),
    )
    monkeypatch.setattr(reveal, "G6J_SCORE_FREEZE_ID", score_doc["score_freeze_id"])
    monkeypatch.setattr(
        reveal,
        "G6J_EVIDENCE_TREE_SHA256",
        reveal._historical_windows_tree_identity(score_root)["tree_sha256"],
    )
    monkeypatch.setattr(
        reveal,
        "G6I_PRIVATE_KEY_COMMITMENT_SHA256",
        commitment,
    )

    g6i_root = _build_g6i_fixture(tmp_path, monkeypatch, private_key)
    output = tmp_path / "G6K"
    output.mkdir()

    with pytest.raises(reveal.G6KRevealError, match="already exists"):
        reveal.reveal_and_freeze(
            g6i_root=g6i_root,
            score_root=score_root,
            output_root=output,
        )
