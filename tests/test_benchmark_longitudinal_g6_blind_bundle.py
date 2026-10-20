import json

import pytest

import benchmark_longitudinal_g6_blind_bundle as bundle
from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    G6_SOURCE_CASES,
)


def _records():
    records = {}
    for case in G6_CALIBRATION_CASES:
        terminal = case.case_id in bundle.EXPECTED_TERMINAL_CASE_IDS
        if terminal:
            response = {
                "http_status": 524,
                "content": None,
            }
            evaluation = {
                "provider_accepted": False,
                "provider_rejection_reasons": [
                    "http_status_524",
                    "provider_model_identity_mismatch",
                    "missing_usage",
                ],
            }
        else:
            text = (
                ""
                if case.case_id in {
                    "G6C003",
                    "G6C004",
                    "G6C005",
                    "G6C006",
                    "G6C007",
                    "G6C008",
                }
                else f"translation:{case.case_id}"
            )
            response = {
                "http_status": 200,
                "content": text,
            }
            evaluation = {
                "provider_accepted": True,
                "provider_rejection_reasons": [],
                "literary_diagnostics": {
                    "translation_sha256": bundle._sha256_text(text),
                },
            }
        records[case.case_id] = {
            "case": case,
            "response": response,
            "evaluation": evaluation,
            "journal_sha256": (case.case_id.lower() + "0" * 64)[:64],
            "result_sha256": (case.case_id.lower() + "1" * 64)[:64],
        }
    return records


def _source_texts():
    result = {}
    for source in G6_SOURCE_CASES:
        result[(source.novel_id, source.chapter)] = {
            "novel_id": source.novel_id,
            "source_language": source.source_language,
            "stress_axis": source.stress_axis,
            "chapter": source.chapter,
            "text_sha256": source.text_sha256,
            "text": f"source:{source.novel_id}:{source.chapter}",
        }
    return result


def _inventory():
    return [
        {
            "index": index,
            "case_id": case.case_id,
            "journal_file": f"{index:04d}.goat_g6_translation.json",
            "journal_sha256": f"{index:064x}",
            "result_file": f"{index:04d}.json",
            "result_sha256": f"{index + 100:064x}",
        }
        for index, case in enumerate(G6_CALIBRATION_CASES, start=1)
    ]


def _documents(nonce="01" * 32):
    inventory = _inventory()
    return bundle.build_bundle_documents(
        records=_records(),
        source_texts=_source_texts(),
        inventory=inventory,
        inventory_sha256=bundle._sha256_json(inventory),
        mapping_nonce=nonce,
    )


def test_g6i_freeze_constants_match_authoritative_handoff():
    assert bundle.G6I_REPOSITORY_ANCHOR == (
        "aaff4e6602b9d7849304b3971c7391416492eb18"
    )
    assert bundle.EXPECTED_CALIBRATION_FREEZE_SHA256 == (
        "fbd3127427cbc210dfdedcb80a6052e61f9851d4eb4551d9e26b25865d607c4c"
    )
    assert bundle.EXPECTED_BLIND_PROTOCOL_SHA256 == (
        "e1272d13d93bbbcbef28d85132f5af3500e73882c4983111c89ba5e7952bd45d"
    )
    assert bundle.R6_MANIFEST_SHA256 == (
        "8a91f643bf2f11fde42d9cd4dca9613ff2107af7864be234d300b18c607c49a1"
    )
    assert bundle.EXPECTED_LOGICAL_CASES == 40
    assert bundle.EXPECTED_ACCEPTED_RESULTS == 38
    assert bundle.EXPECTED_TERMINAL_CASE_IDS == ("G6C035", "G6C036")


def test_build_bundle_preserves_frozen_16_pairs_and_8_singletons():
    private_key, public_bundle, commitment = _documents()

    assert len(private_key["pairs"]) == 16
    assert len(private_key["singletons"]) == 8
    assert len(public_bundle["pairs"]) == 16
    assert len(public_bundle["singletons"]) == 8
    assert len(public_bundle["sources"]) == 4
    assert public_bundle["private_identity_key_commitment_sha256"] == commitment


def test_execution_aware_singletons_keep_two_terminal_slots_unscored():
    private_key, public_bundle, _ = _documents()

    available = [
        row for row in public_bundle["singletons"]
        if row["availability"] == "translation_available"
    ]
    unavailable = [
        row for row in public_bundle["singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]

    assert len(available) == 6
    assert len(unavailable) == 2
    assert all(row["score_record_required"] is True for row in available)
    assert all(row["score_record_required"] is False for row in unavailable)
    assert all("translation" not in row for row in unavailable)
    assert all("translation_sha256" not in row for row in unavailable)

    private_unavailable = [
        row for row in private_key["singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]
    assert {row["case_id"] for row in private_unavailable} == {
        "G6C035",
        "G6C036",
    }


def test_deepseek_zero_outputs_remain_real_empty_translations():
    private_key, public_bundle, _ = _documents()

    public_by_id = {
        row["visible_pair_id"]: row for row in public_bundle["pairs"]
    }
    observed_zero_cases = set()

    for private_row in private_key["pairs"]:
        public_row = public_by_id[private_row["visible_pair_id"]]
        for side in ("a", "b"):
            case_id = private_row[f"side_{side}_case_id"]
            if case_id in {
                "G6C003",
                "G6C004",
                "G6C005",
                "G6C006",
                "G6C007",
                "G6C008",
            }:
                output = public_row[f"side_{side}"]
                assert output["translation"] == ""
                assert output["translation_sha256"] == bundle._sha256_text("")
                observed_zero_cases.add(case_id)

    assert observed_zero_cases == {
        "G6C003",
        "G6C004",
        "G6C005",
        "G6C006",
        "G6C007",
        "G6C008",
    }


def test_public_bundle_has_no_private_model_effort_case_or_nonce_fields():
    nonce = "02" * 32
    private_key, public_bundle, _ = _documents(nonce)
    rendered = json.dumps(public_bundle, ensure_ascii=False, sort_keys=True)

    assert nonce not in rendered
    for forbidden in bundle._FORBIDDEN_PUBLIC_KEYS:
        assert f'"{forbidden}"' not in rendered

    # The private key intentionally retains the identities needed after score freeze.
    assert private_key["mapping_nonce"] == nonce
    assert any("benchmark_model_id" in row for row in private_key["pairs"])


def test_pair_requires_both_real_provider_accepted_outputs():
    records = _records()
    records["G6C001"]["evaluation"]["provider_accepted"] = False
    inventory = _inventory()

    with pytest.raises(bundle.G6IEvidenceError, match="accepted literary output"):
        bundle.build_bundle_documents(
            records=records,
            source_texts=_source_texts(),
            inventory=inventory,
            inventory_sha256=bundle._sha256_json(inventory),
            mapping_nonce="03" * 32,
        )


def test_unexpected_singleton_rejection_fails_closed():
    records = _records()
    records["G6C037"]["evaluation"]["provider_accepted"] = False
    records["G6C037"]["response"]["http_status"] = 500
    inventory = _inventory()

    with pytest.raises(bundle.G6IEvidenceError, match="unexpected unavailable singleton"):
        bundle.build_bundle_documents(
            records=records,
            source_texts=_source_texts(),
            inventory=inventory,
            inventory_sha256=bundle._sha256_json(inventory),
            mapping_nonce="04" * 32,
        )


def test_private_commitment_changes_with_nonce_but_public_remains_blind():
    first_private, first_public, first_commitment = _documents("05" * 32)
    second_private, second_public, second_commitment = _documents("06" * 32)

    assert first_commitment != second_commitment
    assert first_private["mapping_nonce"] != second_private["mapping_nonce"]
    assert first_public["provider_calls"] == 0
    assert second_public["provider_calls"] == 0
    assert first_public["identity_reveal_authorized"] is False
    assert second_public["identity_reveal_authorized"] is False


def test_exclusive_writer_never_overwrites_existing_artifact(tmp_path):
    path = tmp_path / "artifact.json"
    bundle._write_exclusive(path, b"first")
    assert path.read_bytes() == b"first"

    with pytest.raises(bundle.G6IBundleError, match="already exists"):
        bundle._write_exclusive(path, b"second")

    assert path.read_bytes() == b"first"


def test_tree_fingerprint_is_deterministic_and_byte_sensitive(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "b.txt").write_text("beta", encoding="utf-8")

    first = bundle._external_tree_identity(root)
    second = bundle._external_tree_identity(root)
    assert first == second
    assert first["file_count"] == 2

    (root / "b.txt").write_text("changed", encoding="utf-8")
    third = bundle._external_tree_identity(root)
    assert third["tree_sha256"] != first["tree_sha256"]


def test_source_text_is_stored_once_per_frozen_source():
    _private_key, public_bundle, _ = _documents()
    assert len(public_bundle["sources"]) == 4

    source_ids = {row["source_id"] for row in public_bundle["sources"]}
    assert len(source_ids) == 4
    assert all(row["source_id"] in source_ids for row in public_bundle["pairs"])
    assert all(row["source_id"] in source_ids for row in public_bundle["singletons"])

def test_tree_fingerprint_uses_historical_windows_relative_paths(tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "a.txt"
    second = nested / "b.txt"
    first.write_text("alpha", encoding="utf-8")
    second.write_text("beta", encoding="utf-8")

    identity = bundle._external_tree_identity(root)

    rows = [
        f"a.txt\t{bundle._sha256_file(first)}\t{first.stat().st_size}",
        f"nested\\b.txt\t{bundle._sha256_file(second)}\t{second.stat().st_size}",
    ]
    expected = bundle._sha256_text("\n".join(rows))

    assert identity["file_count"] == 2
    assert identity["tree_sha256"] == expected
    assert bundle._historical_windows_relative_path(second, root) == "nested\\b.txt"

