"""Preregistered blind evaluation protocol for GOAT G6 literary calibration.

This module freezes HOW the future 40 G6 translations will be judged before any
literary output exists. It does not execute providers and does not contain model
outputs.

Adjustable models produce four paired low-vs-higher cases each (16 anonymous
A/B pairs total). Tencent Hy3 Paid and MiMo V2.5 Pro are default-only models and
produce eight anonymous singleton cases. Model/effort identity must stay private
until all anonymous scores are frozen.

Importing this module performs no network, credential, filesystem, or provider
activity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    G6_CALIBRATION_FREEZE_SHA256,
    G6_SOURCE_CASES,
)


G6_BLIND_SCHEMA_VERSION = 1
G6_BLIND_PROTOCOL_REVISION = "goat-g6-blind-evaluation-v1"

RUBRIC = {
    "scale": {"minimum": 0.0, "maximum": 10.0},
    "weights": {
        "fidelity": 0.30,
        "completeness": 0.15,
        "terminology": 0.15,
        "naturalness": 0.15,
        "dialogue_tone": 0.10,
        "relationships_pronouns": 0.075,
        "cultural_technical": 0.075,
    },
    "definitions": {
        "fidelity": (
            "Meaning accuracy, logic, nuance, and source-faithful interpretation."
        ),
        "completeness": (
            "No meaningful omissions, additions, duplicated content, or hallucinations."
        ),
        "terminology": (
            "Internally appropriate and consistent rendering of names, titles, "
            "skills, institutions, mechanics, and recurring concepts."
        ),
        "naturalness": (
            "Readable literary English without unnecessary stiffness or machine-like phrasing."
        ),
        "dialogue_tone": (
            "Voice, dialogue flow, register, emotional tone, and character differentiation."
        ),
        "relationships_pronouns": (
            "Correct pronouns, kinship/status relations, referents, and speaker relationships."
        ),
        "cultural_technical": (
            "Appropriate handling of culture-specific, martial, magical, institutional, "
            "game-system, and technical concepts."
        ),
        "blocker": (
            "Mark 1 only for a publication-blocking semantic, completeness, "
            "terminology, or readability defect; otherwise 0."
        ),
        "omission_or_truncation": (
            "Mark 1 when meaningful source content is missing or the translation "
            "terminates prematurely; otherwise 0."
        ),
        "invented_explanation": (
            "Mark 1 when the translation adds explanatory or factual content not "
            "supported by the source; otherwise 0."
        ),
        "terminology_inconsistency": (
            "Mark 1 for a material within-chapter terminology inconsistency; otherwise 0."
        ),
        "preference": (
            "For paired cases only: choose A, B, or TIE after scoring both candidates independently."
        ),
    },
}

DIMENSIONS = tuple(RUBRIC["weights"])
DEFECT_FLAGS = (
    "blocker",
    "omission_or_truncation",
    "invented_explanation",
    "terminology_inconsistency",
)

if abs(sum(RUBRIC["weights"].values()) - 1.0) > 1e-12:
    raise RuntimeError("G6 blind rubric weights must sum to one")

HIGHER_EFFORT_MIN_MEAN_GAIN = 0.10
HIGHER_EFFORT_MIN_PAIR_PREFERENCES = 3
PAIR_COUNT = 16
SINGLETON_COUNT = 8


class G6BlindProtocolError(ValueError):
    """Blind evaluation input or score data violates the frozen protocol."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6BlindProtocolError(
            "G6 blind protocol identity is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _adjustable_models() -> tuple[str, ...]:
    grouped: dict[str, set[str | None]] = {}
    for case in G6_CALIBRATION_CASES:
        grouped.setdefault(case.benchmark_model_id, set()).add(case.effort)
    return tuple(
        model
        for model, efforts in grouped.items()
        if None not in efforts and len(efforts) == 2
    )


def _default_models() -> tuple[str, ...]:
    grouped: dict[str, set[str | None]] = {}
    for case in G6_CALIBRATION_CASES:
        grouped.setdefault(case.benchmark_model_id, set()).add(case.effort)
    return tuple(
        model
        for model, efforts in grouped.items()
        if efforts == {None}
    )


ADJUSTABLE_MODELS = _adjustable_models()
DEFAULT_ONLY_MODELS = _default_models()

if len(ADJUSTABLE_MODELS) != 4 or len(DEFAULT_ONLY_MODELS) != 2:
    raise RuntimeError("unexpected G6 adjustable/default model partition")


@dataclass(frozen=True)
class LogicalPairIdentity:
    benchmark_model_id: str
    novel_id: str
    chapter: int
    lower_effort: str
    higher_effort: str

    def private_identity(self) -> dict[str, Any]:
        return {
            "benchmark_model_id": self.benchmark_model_id,
            "novel_id": self.novel_id,
            "chapter": self.chapter,
            "lower_effort": self.lower_effort,
            "higher_effort": self.higher_effort,
        }


def planned_pair_identities() -> tuple[LogicalPairIdentity, ...]:
    rows: list[LogicalPairIdentity] = []

    for model_id in ADJUSTABLE_MODELS:
        model_cases = [
            case for case in G6_CALIBRATION_CASES
            if case.benchmark_model_id == model_id
        ]
        efforts = []
        for case in model_cases:
            if case.effort not in efforts:
                efforts.append(case.effort)
        if len(efforts) != 2 or any(effort is None for effort in efforts):
            raise G6BlindProtocolError(
                f"adjustable model {model_id!r} lacks exactly two efforts"
            )
        lower, higher = efforts

        for source in G6_SOURCE_CASES:
            matching = [
                case
                for case in model_cases
                if case.novel_id == source.novel_id
                and case.chapter == source.chapter
            ]
            if len(matching) != 2:
                raise G6BlindProtocolError("paired source case is incomplete")
            rows.append(
                LogicalPairIdentity(
                    benchmark_model_id=model_id,
                    novel_id=source.novel_id,
                    chapter=source.chapter,
                    lower_effort=str(lower),
                    higher_effort=str(higher),
                )
            )

    return tuple(rows)


PLANNED_PAIRS = planned_pair_identities()

if len(PLANNED_PAIRS) != PAIR_COUNT:
    raise RuntimeError("G6 must freeze exactly 16 adjustable-model pairs")


def planned_singletons() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for model_id in DEFAULT_ONLY_MODELS:
        for source in G6_SOURCE_CASES:
            rows.append(
                {
                    "benchmark_model_id": model_id,
                    "novel_id": source.novel_id,
                    "chapter": source.chapter,
                    "effort": None,
                }
            )
    return tuple(rows)


PLANNED_SINGLETONS = planned_singletons()

if len(PLANNED_SINGLETONS) != SINGLETON_COUNT:
    raise RuntimeError("G6 must freeze exactly 8 default-only singleton cases")


def _require_mapping_nonce(mapping_nonce: str) -> str:
    if (
        not isinstance(mapping_nonce, str)
        or len(mapping_nonce) != 64
        or any(ch not in "0123456789abcdef" for ch in mapping_nonce)
    ):
        raise G6BlindProtocolError(
            "blind mapping nonce must be 32 random bytes encoded as lowercase hex"
        )
    return mapping_nonce


def _mapping_hash(mapping_nonce: str, purpose: str, *parts: Any) -> str:
    nonce = _require_mapping_nonce(mapping_nonce)
    material = "|".join([G6_BLIND_PROTOCOL_REVISION, purpose, *map(str, parts)])
    return hashlib.sha256(f"{nonce}|{material}".encode("utf-8")).hexdigest()


def derive_private_blind_mapping(mapping_nonce: str) -> dict[str, Any]:
    """Derive opaque visible IDs and balanced A/B sides from one private nonce.

    The nonce belongs only in the private identity key.  Without it, public case
    IDs do not reveal model identity, effort identity, source-order grouping, or
    the A/B side assignment.
    """
    nonce = _require_mapping_nonce(mapping_nonce)

    pair_rows = []
    for pair in PLANNED_PAIRS:
        private = pair.private_identity()
        private["order_key"] = _mapping_hash(
            nonce,
            "pair-order",
            pair.benchmark_model_id,
            pair.novel_id,
            pair.chapter,
        )
        pair_rows.append(private)

    pair_rows.sort(key=lambda item: item["order_key"])

    # Balance lower-vs-higher side assignment independently inside each model:
    # exactly two of four source cases have lower effort on side A.
    lower_a_by_model: dict[str, set[tuple[str, int]]] = {}
    for model_id in ADJUSTABLE_MODELS:
        members = [
            pair for pair in PLANNED_PAIRS
            if pair.benchmark_model_id == model_id
        ]
        ranked = sorted(
            members,
            key=lambda pair: _mapping_hash(
                nonce,
                "pair-side",
                pair.benchmark_model_id,
                pair.novel_id,
                pair.chapter,
            ),
        )
        lower_a_by_model[model_id] = {
            (pair.novel_id, pair.chapter) for pair in ranked[:2]
        }

    private_pairs = []
    for index, item in enumerate(pair_rows, start=1):
        model_id = item["benchmark_model_id"]
        key = (item["novel_id"], item["chapter"])
        lower_is_a = key in lower_a_by_model[model_id]
        private_pairs.append(
            {
                "visible_pair_id": f"G6P{index:03d}",
                "benchmark_model_id": model_id,
                "novel_id": item["novel_id"],
                "chapter": item["chapter"],
                "side_a_effort": (
                    item["lower_effort"] if lower_is_a else item["higher_effort"]
                ),
                "side_b_effort": (
                    item["higher_effort"] if lower_is_a else item["lower_effort"]
                ),
                "lower_effort": item["lower_effort"],
                "higher_effort": item["higher_effort"],
            }
        )

    singleton_rows = []
    for item in PLANNED_SINGLETONS:
        row = dict(item)
        row["order_key"] = _mapping_hash(
            nonce,
            "singleton-order",
            row["benchmark_model_id"],
            row["novel_id"],
            row["chapter"],
        )
        singleton_rows.append(row)
    singleton_rows.sort(key=lambda item: item["order_key"])

    private_singletons = [
        {
            "visible_singleton_id": f"G6S{index:03d}",
            "benchmark_model_id": item["benchmark_model_id"],
            "novel_id": item["novel_id"],
            "chapter": item["chapter"],
            "effort": None,
        }
        for index, item in enumerate(singleton_rows, start=1)
    ]

    return {
        "mapping_nonce": nonce,
        "pairs": private_pairs,
        "singletons": private_singletons,
    }


def private_mapping_commitment(private_mapping: Mapping[str, Any]) -> str:
    return _sha256_json(dict(private_mapping))


def weighted_score(scores: Mapping[str, Any]) -> float:
    total = 0.0
    for dimension, weight in RUBRIC["weights"].items():
        value = scores.get(dimension)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise G6BlindProtocolError(
                f"score for {dimension!r} must be numeric"
            )
        numeric = float(value)
        if not 0.0 <= numeric <= 10.0:
            raise G6BlindProtocolError(
                f"score for {dimension!r} must be between 0 and 10"
            )
        total += numeric * weight
    return round(total, 6)


def validate_defect_flags(record: Mapping[str, Any]) -> None:
    for name in DEFECT_FLAGS:
        value = record.get(name)
        if value not in (0, 1):
            raise G6BlindProtocolError(f"{name} must be 0 or 1")


def validate_pair_score_record(record: Mapping[str, Any]) -> None:
    for side in ("a", "b"):
        weighted_score(
            {
                dimension: record.get(f"{side}_{dimension}")
                for dimension in DIMENSIONS
            }
        )
        validate_defect_flags(
            {name: record.get(f"{side}_{name}") for name in DEFECT_FLAGS}
        )

    preference = record.get("preference")
    if preference not in {"A", "B", "TIE"}:
        raise G6BlindProtocolError("paired preference must be A, B, or TIE")


def validate_singleton_score_record(record: Mapping[str, Any]) -> None:
    weighted_score({dimension: record.get(dimension) for dimension in DIMENSIONS})
    validate_defect_flags(record)


def effort_decision(
    *,
    lower_scores: Sequence[float],
    higher_scores: Sequence[float],
    lower_hard_failure_flags: Sequence[int],
    higher_hard_failure_flags: Sequence[int],
    higher_preferences: int,
    lower_preferences: int,
) -> str:
    """Apply the preregistered effort-selection rule for one adjustable model.

    A hard failure means blocker=1 OR omission_or_truncation=1 for that output.
    The softer diagnostic flags remain reported but do not independently trigger
    the first-stage override.
    """
    if len(lower_scores) != 4 or len(higher_scores) != 4:
        raise G6BlindProtocolError("effort decision requires four paired score cases")
    if len(lower_hard_failure_flags) != 4 or len(higher_hard_failure_flags) != 4:
        raise G6BlindProtocolError("effort decision requires four hard-failure flags")
    if any(value not in (0, 1) for value in lower_hard_failure_flags):
        raise G6BlindProtocolError("lower hard-failure flags must be 0 or 1")
    if any(value not in (0, 1) for value in higher_hard_failure_flags):
        raise G6BlindProtocolError("higher hard-failure flags must be 0 or 1")

    lower_hard = sum(lower_hard_failure_flags)
    higher_hard = sum(higher_hard_failure_flags)

    if higher_hard < lower_hard:
        return "higher"
    if lower_hard < higher_hard:
        return "lower"

    mean_lower = sum(float(value) for value in lower_scores) / 4.0
    mean_higher = sum(float(value) for value in higher_scores) / 4.0
    gain = mean_higher - mean_lower

    if (
        gain >= HIGHER_EFFORT_MIN_MEAN_GAIN
        and higher_preferences >= HIGHER_EFFORT_MIN_PAIR_PREFERENCES
        and lower_preferences <= 1
    ):
        return "higher"

    return "lower"


def blind_protocol_identity() -> dict[str, Any]:
    return {
        "schema_version": G6_BLIND_SCHEMA_VERSION,
        "revision": G6_BLIND_PROTOCOL_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "rubric": RUBRIC,
        "dimensions": list(DIMENSIONS),
        "defect_flags": list(DEFECT_FLAGS),
        "pair_count": PAIR_COUNT,
        "singleton_count": SINGLETON_COUNT,
        "adjustable_models": list(ADJUSTABLE_MODELS),
        "default_only_models": list(DEFAULT_ONLY_MODELS),
        "logical_pair_matrix": [pair.private_identity() for pair in PLANNED_PAIRS],
        "logical_singleton_matrix": list(PLANNED_SINGLETONS),
        "blinding": {
            "model_identity_hidden_during_scoring": True,
            "effort_identity_hidden_during_scoring": True,
            "visible_case_ids_derived_from_private_random_nonce": True,
            "private_nonce_not_present_in_visible_bundle": True,
            "paired_side_assignment_balanced_per_adjustable_model": True,
            "exactly_two_lower_effort_a_sides_per_adjustable_model": True,
            "private_identity_key_sha256_committed_before_scoring": True,
            "scores_frozen_before_identity_reveal": True,
        },
        "effort_selection_rule": {
            "hard_defect_count_first": True,
            "higher_effort_min_mean_gain": HIGHER_EFFORT_MIN_MEAN_GAIN,
            "higher_effort_min_pair_preferences": HIGHER_EFFORT_MIN_PAIR_PREFERENCES,
            "higher_effort_max_lower_preferences": 1,
            "tie_or_insufficient_gain_defaults_to": "lower",
        },
        "default_only_models_have_no_effort_decision": True,
        "model_ranking_or_elimination_authorized": False,
        "provider_calls_authorized": False,
    }


G6_BLIND_PROTOCOL_SHA256 = _sha256_json(blind_protocol_identity())
