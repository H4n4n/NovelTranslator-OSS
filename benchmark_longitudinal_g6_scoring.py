"""Anonymous G6J literary scoring for the frozen G6I public bundle.

G6J is deliberately blind-only. It never reads the G6I private identity key,
never reveals model/effort identities, never calls a Provider, and never makes
an effort-selection decision. It has two actions:

1. prepare
   Verify the exact frozen G6I public artifacts and create reviewer-friendly
   anonymous case files plus blank pair/singleton score sheets.
2. freeze-scores
   Validate a complete anonymous score sheet and freeze it into an immutable
   score document bound to the exact G6I public bundle and private-key
   commitment.

Identity reveal belongs to a later phase implemented separately after this
score freeze exists.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g6_blind import (
    DEFECT_FLAGS,
    DIMENSIONS,
    G6_BLIND_PROTOCOL_SHA256,
    RUBRIC,
    validate_pair_score_record,
    validate_singleton_score_record,
    weighted_score,
)


G6J_SCHEMA_VERSION = 1
G6J_REVISION = "goat-g6-anonymous-scoring-v1"

# Exact successful G6I freeze reported after R6-bound zero-provider generation.
G6I_PUBLIC_BUNDLE_SHA256 = (
    "d6172b0fe53d204c035888c8391b59d09f3080f94c81a8cc0bcd186df0bb9d42"
)
G6I_MANIFEST_SHA256 = (
    "442a2ff2240c91eae585854de3d375371c5658cb5358e73e5cfe7bac330cb155"
)
G6I_FREEZE_SUMMARY_SHA256 = (
    "191f1ed72a3cfcf5172cdccfa2e13ba09ce6bd8a1e6f5e913f626bd1f1446f26"
)
G6I_PRIVATE_KEY_COMMITMENT_SHA256 = (
    "13a93e86ba358f19ace969150affa4524eddec29657f436c9bc7986f7b38a19d"
)
G6I_TREE_SHA256 = (
    "7d3c96931ddce4bf4f4672eb872faff9b2ff32715f1e8697931dd9872c0edc9d"
)
G6I_REPOSITORY_HEAD = "851a0aa6f54069a3e2406b68f60016c4e69e96d0"

PAIR_COUNT = 16
SINGLETON_IDENTITY_COUNT = 8
SCOREABLE_SINGLETON_COUNT = 6
UNAVAILABLE_SINGLETON_COUNT = 2

PUBLIC_BUNDLE_RELATIVE = Path("public") / "g6i_public_blind_bundle.json"
PUBLIC_MANIFEST_RELATIVE = Path("public") / "g6i_bundle_manifest.json"
PUBLIC_SUMMARY_RELATIVE = Path("public") / "g6i_freeze_summary.txt"

PAIR_SCORE_COLUMNS = [
    "visible_pair_id",
    "source_id",
    *[
        f"{side}_{dimension}"
        for side in ("a", "b")
        for dimension in DIMENSIONS
    ],
    *[
        f"{side}_{flag}"
        for side in ("a", "b")
        for flag in DEFECT_FLAGS
    ],
    "preference",
    "notes",
]

SINGLETON_SCORE_COLUMNS = [
    "visible_singleton_id",
    "source_id",
    *DIMENSIONS,
    *DEFECT_FLAGS,
    "notes",
]


class G6JScoringError(RuntimeError):
    """Anonymous G6J scoring input or artifact violates the frozen protocol."""


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    try:
        return _sha_bytes(path.read_bytes())
    except OSError as exc:
        raise G6JScoringError(f"cannot read {path}: {exc}") from exc


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6JScoringError("value is not canonically JSON-serializable") from exc


def _sha_json(value: Any) -> str:
    return _sha_bytes(_canonical_json(value).encode("utf-8"))


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6JScoringError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise G6JScoringError(f"{label} must be a JSON object")
    return value


def _write_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError as exc:
        raise G6JScoringError(f"artifact already exists: {path}") from exc
    if path.read_bytes() != payload:
        raise G6JScoringError(f"artifact byte verification failed: {path}")


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_new_bytes(
        path,
        (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _write_new_text(path: Path, text: str) -> None:
    _write_new_bytes(path, text.encode("utf-8"))


def _require_hex_sha(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise G6JScoringError(f"{label} must be lowercase SHA256 hex")
    return value


def load_frozen_public_bundle(g6i_root: Path | str) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]
]:
    """Load only public G6I artifacts and bind them to the successful freeze."""
    root = Path(g6i_root).resolve()
    bundle_path = root / PUBLIC_BUNDLE_RELATIVE
    manifest_path = root / PUBLIC_MANIFEST_RELATIVE
    summary_path = root / PUBLIC_SUMMARY_RELATIVE

    if _sha_file(bundle_path) != G6I_PUBLIC_BUNDLE_SHA256:
        raise G6JScoringError("G6I public blind bundle SHA256 mismatch")
    if _sha_file(manifest_path) != G6I_MANIFEST_SHA256:
        raise G6JScoringError("G6I public manifest SHA256 mismatch")
    if _sha_file(summary_path) != G6I_FREEZE_SUMMARY_SHA256:
        raise G6JScoringError("G6I freeze summary SHA256 mismatch")

    bundle = _load_json(bundle_path, "G6I public blind bundle")
    manifest = _load_json(manifest_path, "G6I public manifest")

    if bundle.get("g6_blind_protocol_sha256") != G6_BLIND_PROTOCOL_SHA256:
        raise G6JScoringError("G6I bundle blind-protocol binding mismatch")
    if manifest.get("g6_blind_protocol_sha256") != G6_BLIND_PROTOCOL_SHA256:
        raise G6JScoringError("G6I manifest blind-protocol binding mismatch")
    if (
        bundle.get("private_identity_key_commitment_sha256")
        != G6I_PRIVATE_KEY_COMMITMENT_SHA256
    ):
        raise G6JScoringError("G6I bundle private-key commitment mismatch")
    if (
        manifest.get("private_identity_key_commitment_sha256")
        != G6I_PRIVATE_KEY_COMMITMENT_SHA256
    ):
        raise G6JScoringError("G6I manifest private-key commitment mismatch")
    if manifest.get("public_blind_bundle_file_sha256") != G6I_PUBLIC_BUNDLE_SHA256:
        raise G6JScoringError("G6I manifest public-bundle file binding mismatch")
    if manifest.get("repository_head") != G6I_REPOSITORY_HEAD:
        raise G6JScoringError("G6I manifest repository HEAD mismatch")
    if manifest.get("provider_calls") != 0 or bundle.get("provider_calls") != 0:
        raise G6JScoringError("G6I public artifacts unexpectedly report Provider calls")
    if manifest.get("scores_frozen") is not False:
        raise G6JScoringError("G6I manifest unexpectedly reports scores frozen")
    if bundle.get("identity_reveal_authorized") is not False:
        raise G6JScoringError("G6I bundle unexpectedly authorizes identity reveal")
    if bundle.get("effort_selection_authorized") is not False:
        raise G6JScoringError("G6I bundle unexpectedly authorizes effort selection")

    if bundle.get("dimensions") != list(DIMENSIONS):
        raise G6JScoringError("G6I dimensions differ from frozen blind protocol")
    if bundle.get("defect_flags") != list(DEFECT_FLAGS):
        raise G6JScoringError("G6I defect flags differ from frozen blind protocol")
    if bundle.get("rubric") != RUBRIC:
        raise G6JScoringError("G6I rubric differs from frozen blind protocol")

    sources = bundle.get("sources")
    if not isinstance(sources, list) or len(sources) != 4:
        raise G6JScoringError("G6I public bundle must contain four sources")
    source_map: dict[str, dict[str, Any]] = {}
    for row in sources:
        if not isinstance(row, dict):
            raise G6JScoringError("G6I source entry is not an object")
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise G6JScoringError("G6I source entry lacks source_id")
        if source_id in source_map:
            raise G6JScoringError(f"duplicate G6I source_id: {source_id}")
        text = row.get("source_text")
        if not isinstance(text, str):
            raise G6JScoringError(f"{source_id} source text is not a string")
        if _sha_bytes(text.encode("utf-8")) != row.get("text_sha256"):
            raise G6JScoringError(f"{source_id} source-text SHA mismatch")
        source_map[source_id] = row

    pairs = bundle.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != PAIR_COUNT:
        raise G6JScoringError("G6I public bundle must contain 16 pairs")
    pair_ids: set[str] = set()
    for row in pairs:
        if not isinstance(row, dict):
            raise G6JScoringError("G6I pair entry is not an object")
        visible_id = row.get("visible_pair_id")
        if not isinstance(visible_id, str) or visible_id in pair_ids:
            raise G6JScoringError("G6I pair visible IDs are invalid or duplicated")
        pair_ids.add(visible_id)
        if row.get("source_id") not in source_map:
            raise G6JScoringError(f"{visible_id} references unknown source")
        if row.get("score_record_required") is not True:
            raise G6JScoringError(f"{visible_id} unexpectedly not scoreable")
        for side in ("side_a", "side_b"):
            output = row.get(side)
            if not isinstance(output, dict):
                raise G6JScoringError(f"{visible_id} {side} is missing")
            translation = output.get("translation")
            if not isinstance(translation, str):
                raise G6JScoringError(f"{visible_id} {side} translation is not text")
            if _sha_bytes(translation.encode("utf-8")) != output.get(
                "translation_sha256"
            ):
                raise G6JScoringError(f"{visible_id} {side} translation SHA mismatch")

    singletons = bundle.get("singletons")
    if not isinstance(singletons, list) or len(singletons) != SINGLETON_IDENTITY_COUNT:
        raise G6JScoringError("G6I public bundle must contain eight singleton identities")
    singleton_ids: set[str] = set()
    available = 0
    unavailable = 0
    for row in singletons:
        if not isinstance(row, dict):
            raise G6JScoringError("G6I singleton entry is not an object")
        visible_id = row.get("visible_singleton_id")
        if not isinstance(visible_id, str) or visible_id in singleton_ids:
            raise G6JScoringError(
                "G6I singleton visible IDs are invalid or duplicated"
            )
        singleton_ids.add(visible_id)
        if row.get("source_id") not in source_map:
            raise G6JScoringError(f"{visible_id} references unknown source")

        availability = row.get("availability")
        if availability == "translation_available":
            available += 1
            if row.get("score_record_required") is not True:
                raise G6JScoringError(f"{visible_id} available output is not scoreable")
            translation = row.get("translation")
            if not isinstance(translation, str):
                raise G6JScoringError(f"{visible_id} translation is not text")
            if _sha_bytes(translation.encode("utf-8")) != row.get(
                "translation_sha256"
            ):
                raise G6JScoringError(f"{visible_id} translation SHA mismatch")
        elif availability == "terminal_provider_unavailable":
            unavailable += 1
            if row.get("score_record_required") is not False:
                raise G6JScoringError(
                    f"{visible_id} terminal-unavailable output is scoreable"
                )
            if "translation" in row or "translation_sha256" in row:
                raise G6JScoringError(
                    f"{visible_id} terminal-unavailable slot fabricates output"
                )
        else:
            raise G6JScoringError(
                f"{visible_id} has unexpected availability {availability!r}"
            )

    if available != SCOREABLE_SINGLETON_COUNT or unavailable != UNAVAILABLE_SINGLETON_COUNT:
        raise G6JScoringError("G6I singleton availability cardinality changed")

    return bundle, manifest, source_map


def _pair_review_text(
    pair: Mapping[str, Any],
    source: Mapping[str, Any],
) -> str:
    return (
        f"# {pair['visible_pair_id']}\n\n"
        f"Source ID: {pair['source_id']}\n"
        f"Language: {source['source_language']}\n"
        f"Stress axis: {source['stress_axis']}\n"
        f"Chapter: {source['chapter']}\n\n"
        "## Source\n\n"
        f"{source['source_text']}\n\n"
        "## Candidate A\n\n"
        f"{pair['side_a']['translation']}\n\n"
        "## Candidate B\n\n"
        f"{pair['side_b']['translation']}\n"
    )


def _singleton_review_text(
    singleton: Mapping[str, Any],
    source: Mapping[str, Any],
) -> str:
    return (
        f"# {singleton['visible_singleton_id']}\n\n"
        f"Source ID: {singleton['source_id']}\n"
        f"Language: {source['source_language']}\n"
        f"Stress axis: {source['stress_axis']}\n"
        f"Chapter: {source['chapter']}\n\n"
        "## Source\n\n"
        f"{source['source_text']}\n\n"
        "## Translation\n\n"
        f"{singleton['translation']}\n"
    )


def _csv_bytes(fieldnames: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    import io

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def prepare_review(
    *,
    g6i_root: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Create anonymous reviewer material without touching private G6I identity."""
    bundle, _manifest, sources = load_frozen_public_bundle(g6i_root)
    output = Path(output_dir).resolve()
    if output.exists():
        raise G6JScoringError(f"G6J review target already exists: {output}")

    output.mkdir(parents=True, exist_ok=False)

    pair_rows = []
    review_files = []
    for pair in bundle["pairs"]:
        visible_id = pair["visible_pair_id"]
        review_path = output / "pairs" / f"{visible_id}.md"
        payload = _pair_review_text(pair, sources[pair["source_id"]]).encode("utf-8")
        _write_new_bytes(review_path, payload)
        review_files.append(
            {
                "kind": "pair",
                "visible_id": visible_id,
                "relative_path": review_path.relative_to(output).as_posix(),
                "sha256": _sha_bytes(payload),
            }
        )
        row = {column: "" for column in PAIR_SCORE_COLUMNS}
        row["visible_pair_id"] = visible_id
        row["source_id"] = pair["source_id"]
        pair_rows.append(row)

    singleton_rows = []
    unavailable_rows = []
    for singleton in bundle["singletons"]:
        visible_id = singleton["visible_singleton_id"]
        if singleton["availability"] == "translation_available":
            review_path = output / "singletons" / f"{visible_id}.md"
            payload = _singleton_review_text(
                singleton,
                sources[singleton["source_id"]],
            ).encode("utf-8")
            _write_new_bytes(review_path, payload)
            review_files.append(
                {
                    "kind": "singleton",
                    "visible_id": visible_id,
                    "relative_path": review_path.relative_to(output).as_posix(),
                    "sha256": _sha_bytes(payload),
                }
            )
            row = {column: "" for column in SINGLETON_SCORE_COLUMNS}
            row["visible_singleton_id"] = visible_id
            row["source_id"] = singleton["source_id"]
            singleton_rows.append(row)
        else:
            unavailable_rows.append(
                {
                    "visible_singleton_id": visible_id,
                    "source_id": singleton["source_id"],
                    "availability": singleton["availability"],
                    "score_record_required": False,
                }
            )

    pair_csv = _csv_bytes(PAIR_SCORE_COLUMNS, pair_rows)
    singleton_csv = _csv_bytes(SINGLETON_SCORE_COLUMNS, singleton_rows)
    _write_new_bytes(output / "pair_scores.csv", pair_csv)
    _write_new_bytes(output / "singleton_scores.csv", singleton_csv)
    _write_new_json(
        output / "unavailable_singletons.json",
        {
            "schema_version": G6J_SCHEMA_VERSION,
            "revision": G6J_REVISION,
            "rows": unavailable_rows,
        },
    )
    _write_new_json(
        output / "rubric.json",
        {
            "schema_version": G6J_SCHEMA_VERSION,
            "revision": G6J_REVISION,
            "rubric": RUBRIC,
            "dimensions": list(DIMENSIONS),
            "defect_flags": list(DEFECT_FLAGS),
            "review_policy": {
                "score_candidates_independently_before_preference": True,
                "do_not_open_private_identity_key_before_score_freeze": True,
                "terminal_provider_unavailable_singletons_are_not_scored": True,
                "provider_calls_authorized": False,
                "identity_reveal_authorized": False,
                "effort_selection_authorized": False,
            },
        },
    )

    review_manifest = {
        "schema_version": G6J_SCHEMA_VERSION,
        "revision": G6J_REVISION,
        "status": "anonymous_review_material_prepared",
        "g6i_public_bundle_sha256": G6I_PUBLIC_BUNDLE_SHA256,
        "g6i_manifest_sha256": G6I_MANIFEST_SHA256,
        "g6i_private_identity_key_commitment_sha256": (
            G6I_PRIVATE_KEY_COMMITMENT_SHA256
        ),
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "pair_count": len(pair_rows),
        "scoreable_singleton_count": len(singleton_rows),
        "unavailable_singleton_count": len(unavailable_rows),
        "pair_score_template_sha256": _sha_bytes(pair_csv),
        "singleton_score_template_sha256": _sha_bytes(singleton_csv),
        "review_files": review_files,
        "provider_calls": 0,
        "identity_reveal_authorized": False,
        "effort_selection_authorized": False,
    }
    _write_new_json(output / "g6j_review_manifest.json", review_manifest)

    readme = """G6J ANONYMOUS LITERARY SCORING

1. Review only this directory and the G6I PUBLIC artifacts.
2. Do NOT open G6I/private/g6i_private_identity_key.json.
3. Score each A and B candidate independently before recording pair preference.
4. Fill every dimension and defect flag in pair_scores.csv for all 16 pairs.
5. Fill every dimension and defect flag in singleton_scores.csv for all six
   available singleton translations.
6. The two terminal-provider-unavailable singleton slots are intentionally not
   scored and must never receive fabricated literary output or a score.
7. Run G6J freeze-scores only after every required anonymous score is complete.
8. Identity reveal and effort selection are separate later phases.
"""
    _write_new_text(output / "README.txt", readme)

    return review_manifest


def _parse_score(value: Any, label: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise G6JScoringError(f"{label} must be numeric") from exc
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 10.0:
        raise G6JScoringError(f"{label} must be between 0 and 10")
    return numeric


def _parse_flag(value: Any, label: str) -> int:
    if str(value).strip() not in {"0", "1"}:
        raise G6JScoringError(f"{label} must be 0 or 1")
    return int(str(value).strip())


def _read_csv(path: Path, expected_columns: Sequence[str]) -> tuple[bytes, list[dict[str, str]]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise G6JScoringError(f"score sheet could not be read: {path}: {exc}") from exc
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(expected_columns):
                raise G6JScoringError(
                    f"{path.name} columns do not match frozen G6J template"
                )
            rows = list(reader)
    except OSError as exc:
        raise G6JScoringError(f"score sheet could not be parsed: {path}: {exc}") from exc
    return payload, rows


def _freeze_pair_rows(
    rows: Sequence[Mapping[str, str]],
    expected_pairs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected = [
        (pair["visible_pair_id"], pair["source_id"])
        for pair in expected_pairs
    ]
    observed = [
        (row.get("visible_pair_id", ""), row.get("source_id", ""))
        for row in rows
    ]
    if observed != expected:
        raise G6JScoringError("pair score rows do not exactly match frozen pair order")

    frozen = []
    for row in rows:
        visible_id = row["visible_pair_id"]
        record: dict[str, Any] = {
            "visible_pair_id": visible_id,
            "source_id": row["source_id"],
            "preference": row["preference"].strip().upper(),
            "notes": row.get("notes", ""),
        }
        for side in ("a", "b"):
            for dimension in DIMENSIONS:
                record[f"{side}_{dimension}"] = _parse_score(
                    row[f"{side}_{dimension}"],
                    f"{visible_id} {side}_{dimension}",
                )
            for flag in DEFECT_FLAGS:
                record[f"{side}_{flag}"] = _parse_flag(
                    row[f"{side}_{flag}"],
                    f"{visible_id} {side}_{flag}",
                )

        try:
            validate_pair_score_record(record)
        except Exception as exc:
            raise G6JScoringError(f"{visible_id} pair score invalid: {exc}") from exc

        record["a_weighted_score"] = weighted_score(
            {dimension: record[f"a_{dimension}"] for dimension in DIMENSIONS}
        )
        record["b_weighted_score"] = weighted_score(
            {dimension: record[f"b_{dimension}"] for dimension in DIMENSIONS}
        )
        frozen.append(record)
    return frozen


def _freeze_singleton_rows(
    rows: Sequence[Mapping[str, str]],
    expected_singletons: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    available = [
        row
        for row in expected_singletons
        if row["availability"] == "translation_available"
    ]
    expected = [
        (row["visible_singleton_id"], row["source_id"])
        for row in available
    ]
    observed = [
        (row.get("visible_singleton_id", ""), row.get("source_id", ""))
        for row in rows
    ]
    if observed != expected:
        raise G6JScoringError(
            "singleton score rows do not exactly match six available singleton identities"
        )

    frozen = []
    for row in rows:
        visible_id = row["visible_singleton_id"]
        record: dict[str, Any] = {
            "visible_singleton_id": visible_id,
            "source_id": row["source_id"],
            "notes": row.get("notes", ""),
        }
        for dimension in DIMENSIONS:
            record[dimension] = _parse_score(
                row[dimension],
                f"{visible_id} {dimension}",
            )
        for flag in DEFECT_FLAGS:
            record[flag] = _parse_flag(
                row[flag],
                f"{visible_id} {flag}",
            )

        try:
            validate_singleton_score_record(record)
        except Exception as exc:
            raise G6JScoringError(f"{visible_id} singleton score invalid: {exc}") from exc

        record["weighted_score"] = weighted_score(
            {dimension: record[dimension] for dimension in DIMENSIONS}
        )
        frozen.append(record)
    return frozen


def freeze_scores(
    *,
    g6i_root: Path | str,
    review_dir: Path | str,
    pair_scores_csv: Path | str | None = None,
    singleton_scores_csv: Path | str | None = None,
    output_path: Path | str | None = None,
) -> tuple[dict[str, Any], Path, str]:
    """Freeze complete anonymous scores without opening private identity."""
    bundle, _manifest, _sources = load_frozen_public_bundle(g6i_root)
    review = Path(review_dir).resolve()
    pair_path = (
        Path(pair_scores_csv).resolve()
        if pair_scores_csv is not None
        else review / "pair_scores.csv"
    )
    singleton_path = (
        Path(singleton_scores_csv).resolve()
        if singleton_scores_csv is not None
        else review / "singleton_scores.csv"
    )
    output = (
        Path(output_path).resolve()
        if output_path is not None
        else review / "g6j_scores_frozen.json"
    )

    pair_payload, pair_rows = _read_csv(pair_path, PAIR_SCORE_COLUMNS)
    singleton_payload, singleton_rows = _read_csv(
        singleton_path,
        SINGLETON_SCORE_COLUMNS,
    )

    if len(pair_rows) != PAIR_COUNT:
        raise G6JScoringError("pair score sheet must contain exactly 16 rows")
    if len(singleton_rows) != SCOREABLE_SINGLETON_COUNT:
        raise G6JScoringError("singleton score sheet must contain exactly six rows")

    frozen_pairs = _freeze_pair_rows(pair_rows, bundle["pairs"])
    frozen_singletons = _freeze_singleton_rows(
        singleton_rows,
        bundle["singletons"],
    )
    unavailable = [
        {
            "visible_singleton_id": row["visible_singleton_id"],
            "source_id": row["source_id"],
            "availability": row["availability"],
            "score_record_required": False,
        }
        for row in bundle["singletons"]
        if row["availability"] == "terminal_provider_unavailable"
    ]
    if len(unavailable) != UNAVAILABLE_SINGLETON_COUNT:
        raise G6JScoringError("terminal-unavailable singleton count changed")

    document: dict[str, Any] = {
        "schema_version": G6J_SCHEMA_VERSION,
        "revision": G6J_REVISION,
        "status": "anonymous_scores_frozen",
        "g6i_public_bundle_sha256": G6I_PUBLIC_BUNDLE_SHA256,
        "g6i_manifest_sha256": G6I_MANIFEST_SHA256,
        "g6i_private_identity_key_commitment_sha256": (
            G6I_PRIVATE_KEY_COMMITMENT_SHA256
        ),
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "pair_score_sheet_sha256": _sha_bytes(pair_payload),
        "singleton_score_sheet_sha256": _sha_bytes(singleton_payload),
        "rubric": RUBRIC,
        "pair_rows": frozen_pairs,
        "singleton_rows": frozen_singletons,
        "terminal_provider_unavailable_singletons": unavailable,
        "pair_count": len(frozen_pairs),
        "scored_singleton_count": len(frozen_singletons),
        "unavailable_singleton_count": len(unavailable),
        "provider_calls": 0,
        "identity_revealed": False,
        "effort_selection_performed": False,
        "model_ranking_or_elimination_performed": False,
    }
    document["score_freeze_id"] = _sha_json(document)

    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")

    if output.exists():
        if output.read_bytes() != payload:
            raise G6JScoringError(
                "existing frozen score artifact differs from recomputation"
            )
        action = "VERIFIED EXISTING"
    else:
        _write_new_bytes(output, payload)
        action = "CREATED"

    return document, output, action


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and freeze anonymous G6J literary scores"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--g6i-root", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)

    freeze = sub.add_parser("freeze-scores")
    freeze.add_argument("--g6i-root", type=Path, required=True)
    freeze.add_argument("--review-dir", type=Path, required=True)
    freeze.add_argument("--pair-scores-csv", type=Path, default=None)
    freeze.add_argument("--singleton-scores-csv", type=Path, default=None)
    freeze.add_argument("--output", type=Path, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "prepare":
            manifest = prepare_review(
                g6i_root=args.g6i_root,
                output_dir=args.output_dir,
            )
            print("=" * 88)
            print("G6J ANONYMOUS REVIEW MATERIAL PREPARED")
            print("=" * 88)
            print("Pairs                    :", manifest["pair_count"])
            print("Scoreable singletons     :", manifest["scoreable_singleton_count"])
            print("Unavailable singletons   :", manifest["unavailable_singleton_count"])
            print("Public bundle SHA        :", manifest["g6i_public_bundle_sha256"])
            print(
                "Private-key commitment   :",
                manifest["g6i_private_identity_key_commitment_sha256"],
            )
            print("Provider calls           : 0")
            print("Identity revealed        : NO")
            print("Output                   :", Path(args.output_dir).resolve())
            print("=" * 88)
            return 0

        document, path, action = freeze_scores(
            g6i_root=args.g6i_root,
            review_dir=args.review_dir,
            pair_scores_csv=args.pair_scores_csv,
            singleton_scores_csv=args.singleton_scores_csv,
            output_path=args.output,
        )
        print("=" * 88)
        print("G6J ANONYMOUS SCORES FROZEN")
        print("=" * 88)
        print("Action                   :", action)
        print("Pairs                    :", document["pair_count"])
        print("Scored singletons        :", document["scored_singleton_count"])
        print("Unavailable singletons   :", document["unavailable_singleton_count"])
        print("Score freeze ID          :", document["score_freeze_id"])
        print("Identity revealed        : NO")
        print("Effort selection         : NO")
        print("Provider calls           : 0")
        print("Output                   :", path)
        print("=" * 88)
        return 0
    except Exception as exc:
        import sys

        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
