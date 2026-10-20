"""G6K identity reveal and preregistered effort selection.

G6K is allowed only after the anonymous G6J score freeze is complete and
byte-verified. It performs no Provider/model calls and does not rank or
eliminate models.

Order of operations is fail-closed:
1. Verify the exact frozen G6J score evidence without touching the private key.
2. Verify the exact public G6I artifacts.
3. Only then read the committed private G6I identity key.
4. Verify the private-key file SHA, commitment, nonce-derived mapping, and every
   visible pair/singleton identity.
5. Reconcile anonymous scores to the revealed identities.
6. Apply the preregistered per-model effort-selection rule to the four
   adjustable models.
7. Freeze deterministic G6K reveal evidence.

Default-only models receive no effort decision. Terminal Provider-unavailable
singleton identities remain unavailable and are never assigned fabricated
literary scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g6_blind import (
    ADJUSTABLE_MODELS,
    DEFAULT_ONLY_MODELS,
    DEFECT_FLAGS,
    DIMENSIONS,
    G6_BLIND_PROTOCOL_SHA256,
    derive_private_blind_mapping,
    effort_decision,
    private_mapping_commitment,
)
from benchmark_longitudinal_g6_scoring import load_frozen_public_bundle


G6K_SCHEMA_VERSION = 1
G6K_REVISION = "goat-g6-identity-reveal-v1"

# Successful frozen G6I evidence.
G6I_PUBLIC_BUNDLE_SHA256 = (
    "d6172b0fe53d204c035888c8391b59d09f3080f94c81a8cc0bcd186df0bb9d42"
)
G6I_MANIFEST_SHA256 = (
    "442a2ff2240c91eae585854de3d375371c5658cb5358e73e5cfe7bac330cb155"
)
G6I_PRIVATE_KEY_FILE_SHA256 = (
    "11076404ce7825fcdbd55f6f0a309719e14f3618c9b95f96dd805e63d2271e66"
)
G6I_PRIVATE_KEY_COMMITMENT_SHA256 = (
    "13a93e86ba358f19ace969150affa4524eddec29657f436c9bc7986f7b38a19d"
)
G6I_DURABLE_INVENTORY_SHA256 = (
    "db3e1d9c7090928819f51b6857f1e9bb5b492e9af54eb62501e9b8fd247ee145"
)
G6I_R6_MANIFEST_SHA256 = (
    "8a91f643bf2f11fde42d9cd4dca9613ff2107af7864be234d300b18c607c49a1"
)

# Successful authoritative G6J anonymous score freeze.
G6J_PAIR_SCORE_SHA256 = (
    "8a95e3bce2f082b2a7651f5dbaca93633077262a55860ba1fa3f05fe24af7ff5"
)
G6J_SINGLETON_SCORE_SHA256 = (
    "6c9e6af2275aa5e3334bef79798d7028d89b8d4b5d1ce03a2867084a2f879af5"
)
G6J_SCORE_FREEZE_ID = (
    "a97aa9f854de6472daaa23719523b43a88d8275182fcc553b04d12e730d6e7b6"
)
G6J_FROZEN_JSON_SHA256 = (
    "dda073a832c497db33782bedd6c145cfc96cc2230cd59cca6edd834803fd1d87"
)
G6J_EVIDENCE_TREE_SHA256 = (
    "c1726137d2328c7b08eb1c864724383e4010ea6d2aaef32a93052e84f906e524"
)
G6J_EVIDENCE_FILE_COUNT = 3

PRIVATE_KEY_RELATIVE = Path("private") / "g6i_private_identity_key.json"
SCORE_JSON_FILENAME = "g6j_scores_frozen.json"
PAIR_SCORE_FILENAME = "pair_scores_scored.csv"
SINGLETON_SCORE_FILENAME = "singleton_scores_scored.csv"

REVEALED_JSON_FILENAME = "g6k_revealed_results.json"
MANIFEST_FILENAME = "g6k_reveal_manifest.json"
SUMMARY_FILENAME = "g6k_reveal_summary.txt"


class G6KRevealError(RuntimeError):
    """G6K reveal input or evidence violates the frozen protocol."""


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    try:
        return _sha_bytes(path.read_bytes())
    except OSError as exc:
        raise G6KRevealError(f"cannot read {path}: {exc}") from exc


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6KRevealError("value is not canonically JSON-serializable") from exc


def _sha_json(value: Any) -> str:
    return _sha_bytes(_canonical_json(value).encode("utf-8"))


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6KRevealError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise G6KRevealError(f"{label} must be a JSON object")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise G6KRevealError(f"artifact already exists: {path}") from exc
    if path.read_bytes() != payload:
        raise G6KRevealError(f"artifact byte verification failed: {path}")


def _historical_windows_tree_identity(root: Path | str) -> dict[str, Any]:
    """Reproduce the Windows PowerShell evidence-tree fingerprint."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise G6KRevealError(f"evidence root is not a directory: {root_path}")
    rows: list[str] = []
    for path in sorted(
        (item for item in root_path.rglob("*") if item.is_file()),
        key=lambda item: str(item.resolve()),
    ):
        relative = str(path.resolve().relative_to(root_path)).replace("/", "\\")
        rows.append(f"{relative}\t{_sha_file(path)}\t{path.stat().st_size}")
    return {
        "root": str(root_path),
        "file_count": len(rows),
        "tree_sha256": _sha_bytes("\n".join(rows).encode("utf-8")),
    }


def verify_g6j_score_freeze(score_root: Path | str) -> dict[str, Any]:
    """Verify complete anonymous score evidence before any private-key read."""
    root = Path(score_root).resolve()
    tree = _historical_windows_tree_identity(root)
    if tree["file_count"] != G6J_EVIDENCE_FILE_COUNT:
        raise G6KRevealError(
            f"G6J evidence file count changed: {tree['file_count']} "
            f"!= {G6J_EVIDENCE_FILE_COUNT}"
        )
    if tree["tree_sha256"] != G6J_EVIDENCE_TREE_SHA256:
        raise G6KRevealError("G6J evidence tree SHA256 mismatch")

    pair_path = root / PAIR_SCORE_FILENAME
    singleton_path = root / SINGLETON_SCORE_FILENAME
    score_path = root / SCORE_JSON_FILENAME

    if _sha_file(pair_path) != G6J_PAIR_SCORE_SHA256:
        raise G6KRevealError("G6J pair-score file SHA256 mismatch")
    if _sha_file(singleton_path) != G6J_SINGLETON_SCORE_SHA256:
        raise G6KRevealError("G6J singleton-score file SHA256 mismatch")
    if _sha_file(score_path) != G6J_FROZEN_JSON_SHA256:
        raise G6KRevealError("G6J frozen score JSON SHA256 mismatch")

    scores = _load_json(score_path, "G6J frozen score JSON")
    if scores.get("status") != "anonymous_scores_frozen":
        raise G6KRevealError("G6J score status is not frozen")
    if scores.get("score_freeze_id") != G6J_SCORE_FREEZE_ID:
        raise G6KRevealError("G6J score-freeze ID mismatch")
    if scores.get("pair_score_sheet_sha256") != G6J_PAIR_SCORE_SHA256:
        raise G6KRevealError("G6J pair-score binding mismatch")
    if scores.get("singleton_score_sheet_sha256") != G6J_SINGLETON_SCORE_SHA256:
        raise G6KRevealError("G6J singleton-score binding mismatch")
    if scores.get("g6_blind_protocol_sha256") != G6_BLIND_PROTOCOL_SHA256:
        raise G6KRevealError("G6J blind-protocol binding mismatch")
    if (
        scores.get("g6i_private_identity_key_commitment_sha256")
        != G6I_PRIVATE_KEY_COMMITMENT_SHA256
    ):
        raise G6KRevealError("G6J private-key commitment binding mismatch")
    if scores.get("provider_calls") != 0:
        raise G6KRevealError("G6J scores unexpectedly report Provider calls")
    if scores.get("identity_revealed") is not False:
        raise G6KRevealError("G6J scores were not frozen while blind")
    if scores.get("effort_selection_performed") is not False:
        raise G6KRevealError("G6J scores already contain effort selection")
    if scores.get("model_ranking_or_elimination_performed") is not False:
        raise G6KRevealError("G6J scores already contain model ranking/elimination")

    freeze_copy = dict(scores)
    freeze_id = freeze_copy.pop("score_freeze_id", None)
    if _sha_json(freeze_copy) != freeze_id:
        raise G6KRevealError("G6J score-freeze ID does not recompute")

    pair_rows = scores.get("pair_rows")
    singleton_rows = scores.get("singleton_rows")
    unavailable = scores.get("terminal_provider_unavailable_singletons")
    if not isinstance(pair_rows, list) or len(pair_rows) != 16:
        raise G6KRevealError("G6J must contain exactly 16 frozen pair rows")
    if not isinstance(singleton_rows, list) or len(singleton_rows) != 6:
        raise G6KRevealError("G6J must contain exactly six scored singleton rows")
    if not isinstance(unavailable, list) or len(unavailable) != 2:
        raise G6KRevealError("G6J must preserve exactly two unavailable singletons")

    return {"root": root, "tree": tree, "scores": scores}


def verify_public_g6i(g6i_root: Path | str) -> dict[str, Any]:
    """Verify public G6I evidence. This function never opens the private key."""
    bundle, manifest, _sources = load_frozen_public_bundle(g6i_root)
    if manifest.get("private_identity_key_file_sha256") != G6I_PRIVATE_KEY_FILE_SHA256:
        raise G6KRevealError("G6I manifest private-key file SHA binding mismatch")
    if manifest.get("r6_manifest_sha256") != G6I_R6_MANIFEST_SHA256:
        raise G6KRevealError("G6I manifest R6 binding mismatch")
    if (
        manifest.get("durable_evidence_inventory_sha256")
        != G6I_DURABLE_INVENTORY_SHA256
    ):
        raise G6KRevealError("G6I durable inventory binding mismatch")
    return {"bundle": bundle, "manifest": manifest}


def open_and_verify_private_key(
    g6i_root: Path | str,
    *,
    expected_visible_pairs: Sequence[str],
    expected_visible_singletons: Sequence[str],
) -> dict[str, Any]:
    """First permitted private-key read, after score freeze verification."""
    path = Path(g6i_root).resolve() / PRIVATE_KEY_RELATIVE
    if _sha_file(path) != G6I_PRIVATE_KEY_FILE_SHA256:
        raise G6KRevealError("G6I private identity key file SHA256 mismatch")

    private_key = _load_json(path, "G6I private identity key")
    if private_mapping_commitment(private_key) != G6I_PRIVATE_KEY_COMMITMENT_SHA256:
        raise G6KRevealError("G6I private identity key commitment mismatch")
    if private_key.get("g6_blind_protocol_sha256") != G6_BLIND_PROTOCOL_SHA256:
        raise G6KRevealError("private key blind-protocol binding mismatch")
    if private_key.get("r6_manifest_sha256") != G6I_R6_MANIFEST_SHA256:
        raise G6KRevealError("private key R6 binding mismatch")
    if (
        private_key.get("durable_evidence_inventory_sha256")
        != G6I_DURABLE_INVENTORY_SHA256
    ):
        raise G6KRevealError("private key durable inventory binding mismatch")

    nonce = private_key.get("mapping_nonce")
    derived = derive_private_blind_mapping(nonce)

    private_pairs = private_key.get("pairs")
    private_singletons = private_key.get("singletons")
    if not isinstance(private_pairs, list) or len(private_pairs) != 16:
        raise G6KRevealError("private key must contain 16 pair identities")
    if not isinstance(private_singletons, list) or len(private_singletons) != 8:
        raise G6KRevealError("private key must contain eight singleton identities")

    pair_fields = (
        "visible_pair_id",
        "benchmark_model_id",
        "novel_id",
        "chapter",
        "side_a_effort",
        "side_b_effort",
        "lower_effort",
        "higher_effort",
    )
    singleton_fields = (
        "visible_singleton_id",
        "benchmark_model_id",
        "novel_id",
        "chapter",
        "effort",
    )
    projected_pairs = [
        {name: row.get(name) for name in pair_fields}
        for row in private_pairs
    ]
    projected_singletons = [
        {name: row.get(name) for name in singleton_fields}
        for row in private_singletons
    ]
    if projected_pairs != derived["pairs"]:
        raise G6KRevealError("private pair mapping does not match nonce derivation")
    if projected_singletons != derived["singletons"]:
        raise G6KRevealError("private singleton mapping does not match nonce derivation")

    pair_ids = [row["visible_pair_id"] for row in private_pairs]
    singleton_ids = [row["visible_singleton_id"] for row in private_singletons]
    if sorted(pair_ids) != sorted(expected_visible_pairs):
        raise G6KRevealError("private/public pair visible-ID set mismatch")
    if sorted(singleton_ids) != sorted(expected_visible_singletons):
        raise G6KRevealError("private/public singleton visible-ID set mismatch")

    return private_key


def _hard_failure(row: Mapping[str, Any], prefix: str = "") -> int:
    blocker = int(row[f"{prefix}blocker"])
    omission = int(row[f"{prefix}omission_or_truncation"])
    return int(bool(blocker or omission))


def reconcile_revealed_scores(
    *,
    scores: Mapping[str, Any],
    private_key: Mapping[str, Any],
) -> dict[str, Any]:
    """Map frozen anonymous scores to identities and apply effort decisions."""
    score_pairs = {
        row["visible_pair_id"]: row
        for row in scores["pair_rows"]
    }
    score_singletons = {
        row["visible_singleton_id"]: row
        for row in scores["singleton_rows"]
    }
    unavailable_scores = {
        row["visible_singleton_id"]: row
        for row in scores["terminal_provider_unavailable_singletons"]
    }

    revealed_pairs: list[dict[str, Any]] = []
    by_model: dict[str, list[dict[str, Any]]] = {
        model_id: [] for model_id in ADJUSTABLE_MODELS
    }

    for identity in private_key["pairs"]:
        visible_id = identity["visible_pair_id"]
        try:
            score = score_pairs[visible_id]
        except KeyError as exc:
            raise G6KRevealError(f"missing frozen score for {visible_id}") from exc
        if score.get("source_id") != identity.get("source_id"):
            raise G6KRevealError(f"{visible_id} source identity mismatch")

        lower_is_a = identity["side_a_effort"] == identity["lower_effort"]
        higher_is_a = identity["side_a_effort"] == identity["higher_effort"]
        if lower_is_a == higher_is_a:
            raise G6KRevealError(f"{visible_id} effort-side mapping is invalid")

        lower_prefix = "a_" if lower_is_a else "b_"
        higher_prefix = "b_" if lower_is_a else "a_"
        lower_side = "A" if lower_is_a else "B"
        higher_side = "B" if lower_is_a else "A"

        preference = score["preference"]
        if preference == "TIE":
            preferred_effort = "tie"
        elif preference == higher_side:
            preferred_effort = "higher"
        elif preference == lower_side:
            preferred_effort = "lower"
        else:
            raise G6KRevealError(f"{visible_id} preference cannot be reconciled")

        row = {
            "visible_pair_id": visible_id,
            "source_id": identity["source_id"],
            "benchmark_model_id": identity["benchmark_model_id"],
            "novel_id": identity["novel_id"],
            "chapter": identity["chapter"],
            "lower_effort": identity["lower_effort"],
            "higher_effort": identity["higher_effort"],
            "lower_side": lower_side,
            "higher_side": higher_side,
            "lower_case_id": (
                identity["side_a_case_id"] if lower_is_a else identity["side_b_case_id"]
            ),
            "higher_case_id": (
                identity["side_b_case_id"] if lower_is_a else identity["side_a_case_id"]
            ),
            "lower_result_sha256": (
                identity["side_a_result_sha256"]
                if lower_is_a
                else identity["side_b_result_sha256"]
            ),
            "higher_result_sha256": (
                identity["side_b_result_sha256"]
                if lower_is_a
                else identity["side_a_result_sha256"]
            ),
            "lower_weighted_score": score[f"{lower_prefix[0]}_weighted_score"],
            "higher_weighted_score": score[f"{higher_prefix[0]}_weighted_score"],
            "lower_hard_failure": _hard_failure(score, lower_prefix),
            "higher_hard_failure": _hard_failure(score, higher_prefix),
            "anonymous_preference": preference,
            "preferred_effort": preferred_effort,
            "notes": score.get("notes", ""),
        }
        for dimension in DIMENSIONS:
            row[f"lower_{dimension}"] = score[f"{lower_prefix}{dimension}"]
            row[f"higher_{dimension}"] = score[f"{higher_prefix}{dimension}"]
        for flag in DEFECT_FLAGS:
            row[f"lower_{flag}"] = score[f"{lower_prefix}{flag}"]
            row[f"higher_{flag}"] = score[f"{higher_prefix}{flag}"]

        revealed_pairs.append(row)
        by_model[identity["benchmark_model_id"]].append(row)

    effort_decisions: list[dict[str, Any]] = []
    for model_id in ADJUSTABLE_MODELS:
        rows = sorted(
            by_model[model_id],
            key=lambda row: (row["novel_id"], row["chapter"]),
        )
        if len(rows) != 4:
            raise G6KRevealError(
                f"{model_id} must reconcile exactly four paired source cases"
            )
        lower_scores = [float(row["lower_weighted_score"]) for row in rows]
        higher_scores = [float(row["higher_weighted_score"]) for row in rows]
        lower_hard = [int(row["lower_hard_failure"]) for row in rows]
        higher_hard = [int(row["higher_hard_failure"]) for row in rows]
        higher_preferences = sum(
            1 for row in rows if row["preferred_effort"] == "higher"
        )
        lower_preferences = sum(
            1 for row in rows if row["preferred_effort"] == "lower"
        )
        ties = sum(1 for row in rows if row["preferred_effort"] == "tie")

        decision = effort_decision(
            lower_scores=lower_scores,
            higher_scores=higher_scores,
            lower_hard_failure_flags=lower_hard,
            higher_hard_failure_flags=higher_hard,
            higher_preferences=higher_preferences,
            lower_preferences=lower_preferences,
        )
        effort_decisions.append(
            {
                "benchmark_model_id": model_id,
                "lower_effort": rows[0]["lower_effort"],
                "higher_effort": rows[0]["higher_effort"],
                "lower_mean_weighted_score": round(sum(lower_scores) / 4.0, 6),
                "higher_mean_weighted_score": round(sum(higher_scores) / 4.0, 6),
                "mean_gain_higher_minus_lower": round(
                    sum(higher_scores) / 4.0 - sum(lower_scores) / 4.0,
                    6,
                ),
                "lower_hard_failure_count": sum(lower_hard),
                "higher_hard_failure_count": sum(higher_hard),
                "higher_preferences": higher_preferences,
                "lower_preferences": lower_preferences,
                "ties": ties,
                "selected_effort": decision,
                "selected_effort_value": (
                    rows[0]["higher_effort"]
                    if decision == "higher"
                    else rows[0]["lower_effort"]
                ),
            }
        )

    revealed_singletons: list[dict[str, Any]] = []
    default_summaries: list[dict[str, Any]] = []
    singleton_by_model: dict[str, list[dict[str, Any]]] = {
        model_id: [] for model_id in DEFAULT_ONLY_MODELS
    }

    for identity in private_key["singletons"]:
        visible_id = identity["visible_singleton_id"]
        availability = identity.get("availability")
        base = {
            "visible_singleton_id": visible_id,
            "source_id": identity["source_id"],
            "benchmark_model_id": identity["benchmark_model_id"],
            "novel_id": identity["novel_id"],
            "chapter": identity["chapter"],
            "case_id": identity["case_id"],
            "availability": availability,
            "journal_sha256": identity["journal_sha256"],
            "result_sha256": identity["result_sha256"],
        }
        if availability == "translation_available":
            try:
                score = score_singletons[visible_id]
            except KeyError as exc:
                raise G6KRevealError(
                    f"missing score for available singleton {visible_id}"
                ) from exc
            if score.get("source_id") != identity.get("source_id"):
                raise G6KRevealError(f"{visible_id} singleton source mismatch")
            row = {
                **base,
                "weighted_score": score["weighted_score"],
                "hard_failure": _hard_failure(score),
                "notes": score.get("notes", ""),
            }
            for dimension in DIMENSIONS:
                row[dimension] = score[dimension]
            for flag in DEFECT_FLAGS:
                row[flag] = score[flag]
        elif availability == "terminal_provider_unavailable":
            if visible_id not in unavailable_scores:
                raise G6KRevealError(
                    f"unavailable singleton {visible_id} missing from G6J freeze"
                )
            row = {
                **base,
                "http_status": identity.get("http_status"),
                "provider_rejection_reasons": identity.get(
                    "provider_rejection_reasons"
                ),
                "literary_score": None,
                "score_record_required": False,
            }
        else:
            raise G6KRevealError(
                f"{visible_id} has unexpected availability {availability!r}"
            )

        revealed_singletons.append(row)
        singleton_by_model[identity["benchmark_model_id"]].append(row)

    for model_id in DEFAULT_ONLY_MODELS:
        rows = singleton_by_model[model_id]
        if len(rows) != 4:
            raise G6KRevealError(
                f"{model_id} must reconcile exactly four singleton identities"
            )
        available = [
            row for row in rows if row["availability"] == "translation_available"
        ]
        unavailable = [
            row
            for row in rows
            if row["availability"] == "terminal_provider_unavailable"
        ]
        default_summaries.append(
            {
                "benchmark_model_id": model_id,
                "effort_decision_applicable": False,
                "available_translation_count": len(available),
                "terminal_provider_unavailable_count": len(unavailable),
                "mean_available_weighted_score": (
                    round(
                        sum(float(row["weighted_score"]) for row in available)
                        / len(available),
                        6,
                    )
                    if available
                    else None
                ),
                "hard_failure_count_among_available": sum(
                    int(row["hard_failure"]) for row in available
                ),
            }
        )

    if len(revealed_pairs) != 16:
        raise G6KRevealError("revealed pair count changed")
    if len(revealed_singletons) != 8:
        raise G6KRevealError("revealed singleton identity count changed")

    return {
        "revealed_pairs": revealed_pairs,
        "effort_decisions": effort_decisions,
        "revealed_singletons": revealed_singletons,
        "default_only_summaries": default_summaries,
    }


def reveal_and_freeze(
    *,
    g6i_root: Path | str,
    score_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Perform the first permitted identity reveal and freeze deterministic output."""
    output = Path(output_root).resolve()
    if output.exists():
        raise G6KRevealError(
            f"G6K output root already exists and will not be overwritten: {output}"
        )

    # Critical ordering: prove the anonymous score freeze BEFORE private-key access.
    frozen = verify_g6j_score_freeze(score_root)
    public = verify_public_g6i(g6i_root)

    public_pair_ids = [
        row["visible_pair_id"] for row in public["bundle"]["pairs"]
    ]
    public_singleton_ids = [
        row["visible_singleton_id"] for row in public["bundle"]["singletons"]
    ]

    private_key = open_and_verify_private_key(
        g6i_root,
        expected_visible_pairs=public_pair_ids,
        expected_visible_singletons=public_singleton_ids,
    )

    reconciled = reconcile_revealed_scores(
        scores=frozen["scores"],
        private_key=private_key,
    )

    revealed = {
        "schema_version": G6K_SCHEMA_VERSION,
        "revision": G6K_REVISION,
        "status": "identity_revealed_after_score_freeze",
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "g6i_public_bundle_sha256": G6I_PUBLIC_BUNDLE_SHA256,
        "g6i_manifest_sha256": G6I_MANIFEST_SHA256,
        "g6i_private_identity_key_file_sha256": G6I_PRIVATE_KEY_FILE_SHA256,
        "g6i_private_identity_key_commitment_sha256": (
            G6I_PRIVATE_KEY_COMMITMENT_SHA256
        ),
        "g6j_score_freeze_id": G6J_SCORE_FREEZE_ID,
        "g6j_frozen_json_sha256": G6J_FROZEN_JSON_SHA256,
        "g6j_evidence_tree_sha256": G6J_EVIDENCE_TREE_SHA256,
        "provider_calls": 0,
        "identity_revealed": True,
        "effort_selection_performed": True,
        "model_ranking_or_elimination_performed": False,
        **reconciled,
    }
    revealed["reveal_freeze_id"] = _sha_json(revealed)

    output.mkdir(parents=True, exist_ok=False)
    revealed_path = output / REVEALED_JSON_FILENAME
    manifest_path = output / MANIFEST_FILENAME
    summary_path = output / SUMMARY_FILENAME

    revealed_bytes = _json_bytes(revealed)
    _write_exclusive(revealed_path, revealed_bytes)

    manifest = {
        "schema_version": G6K_SCHEMA_VERSION,
        "revision": G6K_REVISION,
        "g6j_score_freeze_id": G6J_SCORE_FREEZE_ID,
        "g6j_frozen_json_sha256": G6J_FROZEN_JSON_SHA256,
        "g6i_private_identity_key_file_sha256": G6I_PRIVATE_KEY_FILE_SHA256,
        "g6i_private_identity_key_commitment_sha256": (
            G6I_PRIVATE_KEY_COMMITMENT_SHA256
        ),
        "revealed_results_file_sha256": _sha_bytes(revealed_bytes),
        "reveal_freeze_id": revealed["reveal_freeze_id"],
        "adjustable_model_count": len(ADJUSTABLE_MODELS),
        "default_only_model_count": len(DEFAULT_ONLY_MODELS),
        "provider_calls": 0,
        "identity_revealed": True,
        "effort_selection_performed": True,
        "model_ranking_or_elimination_performed": False,
    }
    manifest_bytes = _json_bytes(manifest)
    _write_exclusive(manifest_path, manifest_bytes)

    decision_lines = []
    for row in reconciled["effort_decisions"]:
        decision_lines.append(
            f"{row['benchmark_model_id']}: "
            f"selected={row['selected_effort_value']} "
            f"(lower={row['lower_effort']}, higher={row['higher_effort']}, "
            f"lower_mean={row['lower_mean_weighted_score']}, "
            f"higher_mean={row['higher_mean_weighted_score']}, "
            f"lower_hard={row['lower_hard_failure_count']}, "
            f"higher_hard={row['higher_hard_failure_count']}, "
            f"higher_pref={row['higher_preferences']}, "
            f"lower_pref={row['lower_preferences']}, ties={row['ties']})"
        )
    summary = (
        "G6K IDENTITY REVEAL + EFFORT SELECTION\n"
        "========================================\n"
        f"revision={G6K_REVISION}\n"
        f"g6j_score_freeze_id={G6J_SCORE_FREEZE_ID}\n"
        f"g6i_private_identity_key_commitment_sha256="
        f"{G6I_PRIVATE_KEY_COMMITMENT_SHA256}\n"
        f"reveal_freeze_id={revealed['reveal_freeze_id']}\n"
        "provider_calls=0\n"
        "identity_revealed=True\n"
        "effort_selection_performed=True\n"
        "model_ranking_or_elimination_performed=False\n"
        "\nPREREGISTERED EFFORT DECISIONS\n"
        + "\n".join(decision_lines)
        + "\n"
    )
    _write_exclusive(summary_path, summary.encode("utf-8"))

    return {
        "output_root": str(output),
        "revealed": revealed,
        "manifest": manifest,
        "revealed_results_file_sha256": _sha_file(revealed_path),
        "manifest_file_sha256": _sha_file(manifest_path),
        "summary_file_sha256": _sha_file(summary_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reveal frozen G6 identities only after G6J score freeze"
    )
    parser.add_argument("--g6i-root", type=Path, required=True)
    parser.add_argument("--score-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = reveal_and_freeze(
            g6i_root=args.g6i_root,
            score_root=args.score_root,
            output_root=args.output_root,
        )
    except Exception as exc:
        import sys

        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    revealed = result["revealed"]
    print("=" * 88)
    print("G6K IDENTITY REVEAL + EFFORT SELECTION FROZEN")
    print("=" * 88)
    print("Score freeze ID          :", revealed["g6j_score_freeze_id"])
    print("Reveal freeze ID         :", revealed["reveal_freeze_id"])
    print("Provider calls           : 0")
    print("Identity revealed        : YES")
    print("Effort selection         : YES")
    print("Model ranking/elimination: NO")
    print("")
    print("Preregistered effort decisions:")
    for row in revealed["effort_decisions"]:
        print(
            " -",
            row["benchmark_model_id"],
            "=>",
            row["selected_effort_value"],
            f"(lower={row['lower_effort']}, higher={row['higher_effort']})",
        )
    print("")
    print("Default-only models:")
    for row in revealed["default_only_summaries"]:
        print(
            " -",
            row["benchmark_model_id"],
            f"available={row['available_translation_count']}",
            f"terminal_unavailable={row['terminal_provider_unavailable_count']}",
            "effort_decision=N/A",
        )
    print("")
    print("Output:", result["output_root"])
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
