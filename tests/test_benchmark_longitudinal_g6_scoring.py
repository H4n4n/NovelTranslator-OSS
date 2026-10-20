import csv
import hashlib
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g6_scoring as scoring
from benchmark_longitudinal_g6_blind import DEFECT_FLAGS, DIMENSIONS, RUBRIC


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_g6i_fixture(tmp_path, monkeypatch):
    root = tmp_path / "G6I_BLIND_BUNDLE"
    public = root / "public"
    public.mkdir(parents=True)

    sources = []
    for index in range(1, 5):
        text = f"source text {index}"
        sources.append(
            {
                "source_id": f"G6SRC{index:03d}",
                "novel_id": "ace_evolution" if index <= 2 else "rogue_first_prince",
                "source_language": "zh" if index <= 2 else "ko",
                "stress_axis": (
                    "dialogue_stress" if index % 2 else "information_density_stress"
                ),
                "chapter": index,
                "text_sha256": _sha_bytes(text.encode("utf-8")),
                "source_text": text,
            }
        )

    pairs = []
    for index in range(1, 17):
        source = sources[(index - 1) % 4]
        a = f"pair {index} candidate A"
        b = "" if index == 3 else f"pair {index} candidate B"
        pairs.append(
            {
                "visible_pair_id": f"G6P{index:03d}",
                "source_id": source["source_id"],
                "side_a": {
                    "translation": a,
                    "translation_sha256": _sha_bytes(a.encode("utf-8")),
                },
                "side_b": {
                    "translation": b,
                    "translation_sha256": _sha_bytes(b.encode("utf-8")),
                },
                "score_record_required": True,
            }
        )

    singletons = []
    for index in range(1, 9):
        source = sources[(index - 1) % 4]
        row = {
            "visible_singleton_id": f"G6S{index:03d}",
            "source_id": source["source_id"],
        }
        if index <= 6:
            text = f"singleton {index}"
            row.update(
                {
                    "availability": "translation_available",
                    "score_record_required": True,
                    "translation": text,
                    "translation_sha256": _sha_bytes(text.encode("utf-8")),
                }
            )
        else:
            row.update(
                {
                    "availability": "terminal_provider_unavailable",
                    "score_record_required": False,
                }
            )
        singletons.append(row)

    bundle = {
        "schema_version": 1,
        "revision": "goat-g6-blind-bundle-v1",
        "g6_calibration_freeze_sha256": "a" * 64,
        "g6_blind_protocol_sha256": scoring.G6_BLIND_PROTOCOL_SHA256,
        "private_identity_key_commitment_sha256": "c" * 64,
        "provider_calls": 0,
        "identity_reveal_authorized": False,
        "effort_selection_authorized": False,
        "model_ranking_or_elimination_authorized": False,
        "rubric": RUBRIC,
        "dimensions": list(DIMENSIONS),
        "defect_flags": list(DEFECT_FLAGS),
        "sources": sources,
        "pairs": pairs,
        "singletons": singletons,
        "execution_aware_singleton_policy": {
            "frozen_singleton_identity_count": 8,
            "translation_available_count": 6,
            "terminal_provider_unavailable_count": 2,
            "terminal_provider_unavailable_scored": False,
            "missing_literary_output_fabricated": False,
            "provider_retry_authorized": False,
        },
    }

    bundle_path = public / "g6i_public_blind_bundle.json"
    _write_json(bundle_path, bundle)
    bundle_sha = _sha_bytes(bundle_path.read_bytes())

    manifest = {
        "schema_version": 1,
        "revision": "goat-g6-blind-bundle-v1",
        "repository_anchor": "aaff4e6602b9d7849304b3971c7391416492eb18",
        "repository_head": "fixture-head",
        "g6_blind_protocol_sha256": scoring.G6_BLIND_PROTOCOL_SHA256,
        "private_identity_key_commitment_sha256": "c" * 64,
        "public_blind_bundle_file_sha256": bundle_sha,
        "provider_calls": 0,
        "scores_frozen": False,
        "identity_reveal_authorized": False,
        "effort_selection_authorized": False,
        "pair_count": 16,
        "singleton_identity_count": 8,
        "literary_singleton_count": 6,
        "terminal_provider_unavailable_singleton_count": 2,
    }
    manifest_path = public / "g6i_bundle_manifest.json"
    _write_json(manifest_path, manifest)

    summary_path = public / "g6i_freeze_summary.txt"
    summary_path.write_text("fixture summary\n", encoding="utf-8")

    monkeypatch.setattr(scoring, "G6I_PUBLIC_BUNDLE_SHA256", bundle_sha)
    monkeypatch.setattr(
        scoring,
        "G6I_MANIFEST_SHA256",
        _sha_bytes(manifest_path.read_bytes()),
    )
    monkeypatch.setattr(
        scoring,
        "G6I_FREEZE_SUMMARY_SHA256",
        _sha_bytes(summary_path.read_bytes()),
    )
    monkeypatch.setattr(scoring, "G6I_PRIVATE_KEY_COMMITMENT_SHA256", "c" * 64)
    monkeypatch.setattr(scoring, "G6I_REPOSITORY_HEAD", "fixture-head")

    return root, bundle


def _fill_pair_scores(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)

    assert fieldnames == scoring.PAIR_SCORE_COLUMNS
    for index, row in enumerate(rows, start=1):
        for side in ("a", "b"):
            for dimension in DIMENSIONS:
                row[f"{side}_{dimension}"] = "8.0"
            for flag in DEFECT_FLAGS:
                row[f"{side}_{flag}"] = "0"
        row["preference"] = ("A", "B", "TIE")[(index - 1) % 3]
        row["notes"] = f"pair review {index}"

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=scoring.PAIR_SCORE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _fill_singleton_scores(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)

    assert fieldnames == scoring.SINGLETON_SCORE_COLUMNS
    for index, row in enumerate(rows, start=1):
        for dimension in DIMENSIONS:
            row[dimension] = "7.5"
        for flag in DEFECT_FLAGS:
            row[flag] = "0"
        row["notes"] = f"singleton review {index}"

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=scoring.SINGLETON_SCORE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def test_frozen_g6i_bindings_match_successful_g6i_freeze():
    assert scoring.G6I_PUBLIC_BUNDLE_SHA256 == (
        "d6172b0fe53d204c035888c8391b59d09f3080f94c81a8cc0bcd186df0bb9d42"
    )
    assert scoring.G6I_MANIFEST_SHA256 == (
        "442a2ff2240c91eae585854de3d375371c5658cb5358e73e5cfe7bac330cb155"
    )
    assert scoring.G6I_PRIVATE_KEY_COMMITMENT_SHA256 == (
        "13a93e86ba358f19ace969150affa4524eddec29657f436c9bc7986f7b38a19d"
    )
    assert scoring.G6I_TREE_SHA256 == (
        "7d3c96931ddce4bf4f4672eb872faff9b2ff32715f1e8697931dd9872c0edc9d"
    )


def test_prepare_review_creates_16_pairs_6_singletons_and_2_unavailable(
    tmp_path, monkeypatch
):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    output = tmp_path / "review"

    manifest = scoring.prepare_review(
        g6i_root=g6i_root,
        output_dir=output,
    )

    assert manifest["pair_count"] == 16
    assert manifest["scoreable_singleton_count"] == 6
    assert manifest["unavailable_singleton_count"] == 2
    assert len(list((output / "pairs").glob("*.md"))) == 16
    assert len(list((output / "singletons").glob("*.md"))) == 6

    unavailable = json.loads(
        (output / "unavailable_singletons.json").read_text(encoding="utf-8")
    )
    assert len(unavailable["rows"]) == 2
    assert all(row["score_record_required"] is False for row in unavailable["rows"])


def test_prepare_review_preserves_real_empty_translation(tmp_path, monkeypatch):
    g6i_root, bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    output = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=output)

    target_pair = next(
        row for row in bundle["pairs"] if row["visible_pair_id"] == "G6P003"
    )
    assert target_pair["side_b"]["translation"] == ""

    review = (output / "pairs" / "G6P003.md").read_text(encoding="utf-8")
    assert "## Candidate B\n\n\n" in review


def test_prepare_and_freeze_complete_anonymous_scores(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)

    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")

    document, path, action = scoring.freeze_scores(
        g6i_root=g6i_root,
        review_dir=review,
    )

    assert action == "CREATED"
    assert path == review / "g6j_scores_frozen.json"
    assert document["status"] == "anonymous_scores_frozen"
    assert document["pair_count"] == 16
    assert document["scored_singleton_count"] == 6
    assert document["unavailable_singleton_count"] == 2
    assert document["provider_calls"] == 0
    assert document["identity_revealed"] is False
    assert document["effort_selection_performed"] is False
    assert document["model_ranking_or_elimination_performed"] is False
    assert len(document["score_freeze_id"]) == 64
    assert all(row["a_weighted_score"] == 8.0 for row in document["pair_rows"])
    assert all(row["b_weighted_score"] == 8.0 for row in document["pair_rows"])
    assert all(
        row["weighted_score"] == 7.5 for row in document["singleton_rows"]
    )


def test_freeze_scores_can_verify_identical_existing_freeze(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)
    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")

    first, path, action = scoring.freeze_scores(
        g6i_root=g6i_root,
        review_dir=review,
    )
    second, second_path, second_action = scoring.freeze_scores(
        g6i_root=g6i_root,
        review_dir=review,
    )

    assert action == "CREATED"
    assert second_action == "VERIFIED EXISTING"
    assert second_path == path
    assert second == first


def test_freeze_rejects_missing_pair_row(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)
    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")

    pair_path = review / "pair_scores.csv"
    with pair_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    with pair_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=scoring.PAIR_SCORE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows[:-1])

    with pytest.raises(scoring.G6JScoringError, match="exactly 16"):
        scoring.freeze_scores(g6i_root=g6i_root, review_dir=review)


def test_freeze_rejects_invalid_pair_preference(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)
    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")

    pair_path = review / "pair_scores.csv"
    with pair_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    rows[0]["preference"] = "C"

    with pair_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=scoring.PAIR_SCORE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(scoring.G6JScoringError, match="pair score invalid"):
        scoring.freeze_scores(g6i_root=g6i_root, review_dir=review)


def test_unavailable_singletons_cannot_be_added_to_score_sheet(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)
    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")

    singleton_path = review / "singleton_scores.csv"
    with singleton_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    extra = dict(rows[0])
    extra["visible_singleton_id"] = "G6S007"
    rows.append(extra)

    with singleton_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=scoring.SINGLETON_SCORE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(scoring.G6JScoringError, match="exactly six"):
        scoring.freeze_scores(g6i_root=g6i_root, review_dir=review)


def test_public_bundle_tamper_fails_closed(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    bundle_path = g6i_root / "public" / "g6i_public_blind_bundle.json"
    bundle_path.write_bytes(bundle_path.read_bytes() + b"\n")

    with pytest.raises(scoring.G6JScoringError, match="bundle SHA256 mismatch"):
        scoring.prepare_review(
            g6i_root=g6i_root,
            output_dir=tmp_path / "review",
        )


def test_private_identity_key_is_never_opened(tmp_path, monkeypatch):
    g6i_root, _bundle = _build_g6i_fixture(tmp_path, monkeypatch)
    private_path = g6i_root / "private" / "g6i_private_identity_key.json"
    private_path.parent.mkdir(parents=True)
    private_path.write_text("PRIVATE-TRAP", encoding="utf-8")

    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self.resolve() == private_path.resolve():
            raise AssertionError("G6J attempted to open the private identity key")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    review = tmp_path / "review"
    scoring.prepare_review(g6i_root=g6i_root, output_dir=review)
    _fill_pair_scores(review / "pair_scores.csv")
    _fill_singleton_scores(review / "singleton_scores.csv")
    document, _path, _action = scoring.freeze_scores(
        g6i_root=g6i_root,
        review_dir=review,
    )

    assert document["identity_revealed"] is False


def test_cli_exposes_no_reveal_command():
    parser = scoring.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["reveal"])
