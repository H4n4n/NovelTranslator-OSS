"""G11 zero-provider blind bundle, anonymous score-freeze, and reveal protocol.

G11 generalizes the proven G10C blind architecture for the GLM-5.3-Flash vs
DeepSeek-V4.1-Flash practical V4-Pro-replacement benchmark.  It performs no
Provider call, reads no credential, and contains no paid execution path.

Blindness rules:

* the 28 literary checkpoint cases are anonymously labelled ``side_a`` /
  ``side_b``;
* literary orientation is cryptographically determined but exactly balanced
  *within each novel*: GLM is side A in 7 of the 14 cases and DeepSeek is side A
  in the other 7 (secret-key ranking chooses which seven);
* longitudinal orientation is cryptographically determined once per novel and
  stays stable across checkpoints 25/50/75/100, with exactly one novel anchored
  to GLM-as-A and one to DeepSeek-as-A (which novel is secret-keyed);
* model, effort, cost, transport, request, journal, and accounting metadata are
  forbidden from every evaluator-visible artifact;
* literary and longitudinal scores must freeze before any private identity
  reveal.

Importing this module performs no filesystem write, credential read, network
activity, or Provider call, and no function here scores or reveals anything at
import time or during bundle construction.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g11_preregistration import (
    DEEPSEEK_MODEL_ID,
    G11_BENCHMARK_ID,
    G11_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    MODEL_ORDER,
    NOVELS,
    QUALITY_CHECKPOINTS,
    STATE_EVIDENCE_CHECKPOINTS,
)


G11L_SCHEMA_VERSION = 1
G11L_REVISION = "g11-blind-bundle-score-reveal-v1"

# The immutable G11 final-freeze identity this blind protocol binds to.
G11_FINAL_FREEZE_ID = (
    "6176a7d76111f8eccc0a897a5ee3cfee7224a6455a49e778a0702ebd6ef4931b"
)

NOVEL_ORDER = tuple(NOVELS)
NOVEL_SOURCE_LANGUAGE = {
    novel_id: str(NOVELS[novel_id]["source_language"]) for novel_id in NOVEL_ORDER
}

LITERARY_CHECKPOINTS = tuple(QUALITY_CHECKPOINTS)
LONGITUDINAL_CHECKPOINTS = tuple(STATE_EVIDENCE_CHECKPOINTS)
REFERENCE_WINDOWS = {
    25: (1, 25),
    50: (26, 50),
    75: (51, 75),
    100: (76, 100),
}

LITERARY_PER_NOVEL = len(LITERARY_CHECKPOINTS)
LITERARY_PAIR_COUNT = len(NOVEL_ORDER) * LITERARY_PER_NOVEL
LITERARY_A_PER_MODEL_PER_NOVEL = LITERARY_PER_NOVEL // 2
LONGITUDINAL_PAIR_COUNT = len(NOVEL_ORDER) * len(LONGITUDINAL_CHECKPOINTS)

LITERARY_AVAILABLE = "paired_translation_available"
LONGITUDINAL_AVAILABLE = "comparable_checkpoint_available"

PRIVATE_KEY_SCHEMA_VERSION = 1
PRIVATE_KEY_REVISION = "g11-private-identity-key-v1"
PUBLIC_BUNDLE_SCHEMA_VERSION = 1
PUBLIC_BUNDLE_REVISION = "g11-public-blind-bundle-v1"
SCORE_FREEZE_SCHEMA_VERSION = 1
SCORE_FREEZE_REVISION = "g11-anonymous-score-freeze-v1"
REVEAL_SCHEMA_VERSION = 1
REVEAL_REVISION = "g11-identity-reveal-v1"

_SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

LITERARY_RUBRIC = {
    "score_range": [0, 10],
    "weights": {
        "fidelity": 0.30,
        "completeness": 0.15,
        "terminology": 0.15,
        "naturalness": 0.15,
        "dialogue_tone": 0.10,
        "relationships_pronouns": 0.075,
        "cultural_technical": 0.075,
    },
    "defect_flags": [
        "blocker",
        "omission_or_truncation",
        "invented_explanation",
        "terminology_inconsistency",
    ],
}

LONGITUDINAL_RUBRIC = {
    "score_range": [0, 10],
    "weights": {
        "entity_name_continuity": 0.20,
        "terminology_mechanics_rank_continuity": 0.20,
        "factual_plot_continuity": 0.20,
        "relationships_status_pronouns": 0.15,
        "state_precision_hygiene": 0.15,
        "audit_correction_quality_no_regression": 0.10,
    },
}

LITERARY_DIMENSIONS = tuple(LITERARY_RUBRIC["weights"])
LITERARY_DEFECT_FLAGS = tuple(LITERARY_RUBRIC["defect_flags"])
LONGITUDINAL_DIMENSIONS = tuple(LONGITUDINAL_RUBRIC["weights"])

PREFERENCE_VALUES = ("A", "B", "TIE")

STATISTICS_POLICY = {
    "literary_pair_count": LITERARY_PAIR_COUNT,
    "primary_strata": {
        novel_id: LITERARY_PER_NOVEL for novel_id in NOVEL_ORDER
    },
    "all_literary_cases_are_primary": True,
    "report_wtl_separately_by_novel_language_first": True,
    "pooled_literary_result_only_after_stratified_reporting": True,
    "two_sided_exact_sign_test_on_non_ties": True,
    "stratified_bootstrap_replicates": 10000,
    "stratified_bootstrap_confidence": 0.95,
    "per_dimension_paired_differences_secondary": True,
    "longitudinal_inference": (
        "descriptive_two_novel_tracks_per_model_repeated_checkpoints"
    ),
    "no_composite_overall_winner_score": True,
    "anonymous_preference_values": list(PREFERENCE_VALUES),
}

# Identity/accounting structure that must never appear in evaluator artifacts.
FORBIDDEN_PUBLIC_METADATA_TOKENS = (
    "model_id",
    "model_name",
    "provider_model",
    "provider",
    "reasoning_effort",
    "effort",
    "adaptive-low",
    "adaptive_low",
    "saturation",
    "credit_equivalent",
    "cost",
    "ledger",
    "transport",
    "retries",
    "regeneration",
    "recovery",
    "request_sha",
    "reservation_id",
    "journal",
    "accounting",
    "run_id",
)

# Exact identity literals that must never be visible anywhere in public material.
FORBIDDEN_PUBLIC_EXACT_LITERALS = (
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4.1-flash",
    "glm-5.3-flash",
    "deepseek-v4.1-flash",
)

_BARE_MODEL_TOKENS = ("glm", "deepseek")
_BARE_TOKEN_SKIP_KEYS = frozenset({"source_text", "translation"})


class G11BlindProtocolError(RuntimeError):
    """A G11 blind-evaluation artifact violates the frozen protocol."""


# --------------------------------------------------------------------------- #
# Canonical helpers
# --------------------------------------------------------------------------- #


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G11BlindProtocolError(
            "G11 blind artifact is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G11BlindProtocolError(f"{label} must be a lowercase SHA-256")
    return value


def _require_secret_hex(secret_hex: str) -> bytes:
    if not isinstance(secret_hex, str) or not _SECRET_RE.fullmatch(secret_hex):
        raise G11BlindProtocolError(
            "blind secret must be exactly 32 bytes as lowercase hex"
        )
    return bytes.fromhex(secret_hex)


def _hmac_rank(secret: bytes, namespace: str, logical_id: str) -> str:
    return hmac.new(
        secret,
        f"{namespace}|{logical_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _other_model(model_id: str) -> str:
    if model_id == GLM_MODEL_ID:
        return DEEPSEEK_MODEL_ID
    if model_id == DEEPSEEK_MODEL_ID:
        return GLM_MODEL_ID
    raise G11BlindProtocolError(f"unexpected model identity: {model_id!r}")


def _literary_pair_id(novel_id: str, chapter: int) -> str:
    return f"G11L-{novel_id}-CH{int(chapter):03d}"


def _longitudinal_pair_id(novel_id: str, checkpoint: int) -> str:
    return f"G11S-{novel_id}-CH{int(checkpoint):03d}"


def literary_roster() -> tuple[dict[str, Any], ...]:
    """The frozen 28-case literary roster (14 per novel)."""
    rows: list[dict[str, Any]] = []
    for novel_id in NOVEL_ORDER:
        for chapter in LITERARY_CHECKPOINTS:
            rows.append(
                {
                    "pair_id": _literary_pair_id(novel_id, chapter),
                    "novel_id": novel_id,
                    "chapter": int(chapter),
                }
            )
    if len(rows) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError("G11 literary roster drifted")
    return tuple(rows)


def longitudinal_roster() -> tuple[dict[str, Any], ...]:
    """The frozen 8-case longitudinal roster (4 checkpoints per novel)."""
    rows: list[dict[str, Any]] = []
    for novel_id in NOVEL_ORDER:
        for checkpoint in LONGITUDINAL_CHECKPOINTS:
            rows.append(
                {
                    "pair_id": _longitudinal_pair_id(novel_id, checkpoint),
                    "novel_id": novel_id,
                    "checkpoint": int(checkpoint),
                }
            )
    if len(rows) != LONGITUDINAL_PAIR_COUNT:
        raise G11BlindProtocolError("G11 longitudinal roster drifted")
    return tuple(rows)


# --------------------------------------------------------------------------- #
# Private A/B identity construction and validation
# --------------------------------------------------------------------------- #


def build_private_identity_key(
    secret_hex: str,
    *,
    final_freeze_id: str | None = None,
) -> dict[str, Any]:
    """Derive the private A/B identity mapping from one 256-bit secret."""
    secret = _require_secret_hex(secret_hex)
    freeze_id = (
        G11_FINAL_FREEZE_ID if final_freeze_id is None else final_freeze_id
    )
    _require_sha256(freeze_id, "G11 final-freeze id")

    roster = literary_roster()
    literary_mapping: list[dict[str, Any]] = []
    for novel_id in NOVEL_ORDER:
        novel_rows = [row for row in roster if row["novel_id"] == novel_id]
        ranked = sorted(
            novel_rows,
            key=lambda row: (
                _hmac_rank(secret, f"literary-{novel_id}", row["pair_id"]),
                row["pair_id"],
            ),
        )
        glm_a = {
            row["pair_id"]
            for row in ranked[:LITERARY_A_PER_MODEL_PER_NOVEL]
        }
        for row in novel_rows:
            side_a_model = GLM_MODEL_ID if row["pair_id"] in glm_a else DEEPSEEK_MODEL_ID
            literary_mapping.append(
                {
                    "pair_id": row["pair_id"],
                    "novel_id": novel_id,
                    "chapter": row["chapter"],
                    "side_a_model_id": side_a_model,
                    "side_b_model_id": _other_model(side_a_model),
                }
            )

    ranked_novels = sorted(
        NOVEL_ORDER,
        key=lambda novel_id: (
            _hmac_rank(secret, "longitudinal-novel", novel_id),
            novel_id,
        ),
    )
    glm_a_novel = ranked_novels[0]
    longitudinal_mapping: list[dict[str, Any]] = []
    for novel_id in NOVEL_ORDER:
        side_a_model = (
            GLM_MODEL_ID if novel_id == glm_a_novel else DEEPSEEK_MODEL_ID
        )
        longitudinal_mapping.append(
            {
                "novel_id": novel_id,
                "side_a_model_id": side_a_model,
                "side_b_model_id": _other_model(side_a_model),
                "checkpoints": list(LONGITUDINAL_CHECKPOINTS),
            }
        )

    key = {
        "schema_version": PRIVATE_KEY_SCHEMA_VERSION,
        "revision": PRIVATE_KEY_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
        "g11_final_freeze_id": freeze_id,
        "secret_sha256": hashlib.sha256(secret).hexdigest(),
        "literary_mapping": literary_mapping,
        "longitudinal_mapping": longitudinal_mapping,
        "provider_calls": 0,
    }
    validate_private_identity_key(key)
    return key


def _private_literary_map(
    private_key: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    rows = private_key.get("literary_mapping")
    if not isinstance(rows, list) or len(rows) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError("private literary identity mapping is invalid")
    roster = {row["pair_id"]: row for row in literary_roster()}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("private literary mapping row is invalid")
        pair_id = row.get("pair_id")
        if pair_id not in roster or pair_id in result:
            raise G11BlindProtocolError("private literary pair IDs are invalid")
        frozen = roster[pair_id]
        if row.get("novel_id") != frozen["novel_id"] or row.get("chapter") != frozen["chapter"]:
            raise G11BlindProtocolError("private literary row changed its source")
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G11BlindProtocolError("private literary side mapping is invalid")
        result[pair_id] = copy.deepcopy(dict(row))
    if set(result) != set(roster):
        raise G11BlindProtocolError("private literary mapping is incomplete")
    return result


def _private_longitudinal_map(
    private_key: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    rows = private_key.get("longitudinal_mapping")
    if not isinstance(rows, list) or len(rows) != len(NOVEL_ORDER):
        raise G11BlindProtocolError("private longitudinal identity mapping is invalid")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("private longitudinal mapping row is invalid")
        novel_id = row.get("novel_id")
        if novel_id not in NOVEL_ORDER or novel_id in result:
            raise G11BlindProtocolError("private longitudinal novel mapping is invalid")
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G11BlindProtocolError("private longitudinal side mapping is invalid")
        if tuple(row.get("checkpoints", ())) != LONGITUDINAL_CHECKPOINTS:
            raise G11BlindProtocolError("private longitudinal checkpoints drifted")
        result[novel_id] = copy.deepcopy(dict(row))
    if set(result) != set(NOVEL_ORDER):
        raise G11BlindProtocolError("private longitudinal mapping is incomplete")
    return result


def validate_private_identity_key(private_key: Mapping[str, Any]) -> None:
    """Reject any private key that is unbalanced, drifted, or incomplete."""
    if not isinstance(private_key, Mapping):
        raise G11BlindProtocolError("private identity key must be a mapping")
    checks = {
        "schema_version": PRIVATE_KEY_SCHEMA_VERSION,
        "revision": PRIVATE_KEY_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
        "provider_calls": 0,
    }
    for key, expected in checks.items():
        if private_key.get(key) != expected:
            raise G11BlindProtocolError(f"private identity key changed {key}")
    _require_sha256(private_key.get("g11_final_freeze_id"), "private key final-freeze id")
    _require_sha256(private_key.get("secret_sha256"), "private key secret commitment")

    literary = _private_literary_map(private_key)
    longitudinal = _private_longitudinal_map(private_key)

    for novel_id in NOVEL_ORDER:
        novel_a = [
            row["side_a_model_id"]
            for row in literary.values()
            if row["novel_id"] == novel_id
        ]
        if len(novel_a) != LITERARY_PER_NOVEL:
            raise G11BlindProtocolError("private literary novel roster drifted")
        if (
            novel_a.count(GLM_MODEL_ID) != LITERARY_A_PER_MODEL_PER_NOVEL
            or novel_a.count(DEEPSEEK_MODEL_ID) != LITERARY_A_PER_MODEL_PER_NOVEL
        ):
            raise G11BlindProtocolError(
                "private literary orientation is not 7/7 balanced within the novel"
            )

    long_a = sorted(row["side_a_model_id"] for row in longitudinal.values())
    if long_a != sorted(MODEL_ORDER):
        raise G11BlindProtocolError(
            "private longitudinal orientation is not one novel per side-A model"
        )


def private_identity_key_commitment(private_key: Mapping[str, Any]) -> str:
    """The public commitment: canonical-JSON SHA256 of the complete private key."""
    validate_private_identity_key(private_key)
    return _sha256_json(copy.deepcopy(dict(private_key)))


# --------------------------------------------------------------------------- #
# Public blindness scans
# --------------------------------------------------------------------------- #


def assert_no_forbidden_metadata(value: Any, path: str = "public") -> None:
    """Reject identity/accounting structural keys anywhere in evaluator material."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key).casefold()
            if any(token in text for token in FORBIDDEN_PUBLIC_METADATA_TOKENS):
                raise G11BlindProtocolError(
                    f"forbidden identity/accounting metadata key in {path}: {key}"
                )
            assert_no_forbidden_metadata(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_forbidden_metadata(item, f"{path}[{index}]")


def _non_prose_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _BARE_TOKEN_SKIP_KEYS:
                continue
            found.append(str(key))
            found.extend(_non_prose_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_non_prose_strings(item))
    elif isinstance(value, str):
        found.append(value)
    return found


def assert_no_public_identity_leak(artifact: Mapping[str, Any]) -> None:
    """Reject exact identity literals and bare model tokens outside prose."""
    canonical = _canonical_json(artifact).casefold()
    for literal in FORBIDDEN_PUBLIC_EXACT_LITERALS:
        if literal.casefold() in canonical:
            raise G11BlindProtocolError(
                f"public blind artifact leaks the literal identity {literal!r}"
            )
    for text in _non_prose_strings(artifact):
        folded = text.casefold()
        for token in _BARE_MODEL_TOKENS:
            if token in folded:
                raise G11BlindProtocolError(
                    f"public blind artifact leaks the model token {token!r}"
                )


# --------------------------------------------------------------------------- #
# Public blind bundle construction and validation
# --------------------------------------------------------------------------- #


def _literary_input_map(
    literary_records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    roster = {row["pair_id"]: row for row in literary_roster()}
    if len(literary_records) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError(
            f"literary input must contain exactly {LITERARY_PAIR_COUNT} cases"
        )
    result: dict[str, dict[str, Any]] = {}
    for item in literary_records:
        if not isinstance(item, Mapping):
            raise G11BlindProtocolError("literary input row must be an object")
        pair_id = item.get("pair_id")
        if pair_id not in roster or pair_id in result:
            raise G11BlindProtocolError("literary input pair_id is invalid or duplicated")
        frozen = roster[pair_id]
        for key in ("novel_id", "chapter"):
            if item.get(key) != frozen[key]:
                raise G11BlindProtocolError(
                    f"literary input {pair_id} changed frozen {key}"
                )
        source_text = item.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            raise G11BlindProtocolError(f"{pair_id} source text is missing")
        language = item.get(
            "source_language", NOVEL_SOURCE_LANGUAGE[frozen["novel_id"]]
        )
        if language != NOVEL_SOURCE_LANGUAGE[frozen["novel_id"]]:
            raise G11BlindProtocolError(f"{pair_id} source language changed")
        translations = item.get("translations")
        if not isinstance(translations, Mapping) or set(translations) != set(MODEL_ORDER):
            raise G11BlindProtocolError(
                f"{pair_id} must contain exactly both model translations"
            )
        clean: dict[str, str] = {}
        for model_id in MODEL_ORDER:
            translation = translations[model_id]
            if not isinstance(translation, str) or not translation.strip():
                raise G11BlindProtocolError(f"{pair_id} {model_id} translation is empty")
            clean[model_id] = translation
        result[str(pair_id)] = {
            "pair_id": pair_id,
            "novel_id": frozen["novel_id"],
            "chapter": frozen["chapter"],
            "source_language": language,
            "source_text": source_text,
            "source_text_sha256": _sha256_text(source_text),
            "translations": clean,
        }
    if set(result) != set(roster):
        raise G11BlindProtocolError("literary input roster is incomplete")
    return result


def _longitudinal_input_map(
    longitudinal_records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    roster = {
        (row["novel_id"], row["checkpoint"]): row
        for row in longitudinal_roster()
    }
    if len(longitudinal_records) != LONGITUDINAL_PAIR_COUNT:
        raise G11BlindProtocolError(
            f"longitudinal input must contain exactly {LONGITUDINAL_PAIR_COUNT} cases"
        )
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for item in longitudinal_records:
        if not isinstance(item, Mapping):
            raise G11BlindProtocolError("longitudinal input row must be an object")
        key = (item.get("novel_id"), item.get("checkpoint"))
        if key not in roster or key in result:
            raise G11BlindProtocolError(
                "longitudinal novel/checkpoint is invalid or duplicated"
            )
        novel_id, checkpoint = key
        window = REFERENCE_WINDOWS[checkpoint]
        if list(item.get("reference_window", ())) != [window[0], window[1]]:
            raise G11BlindProtocolError(
                f"{novel_id} Ch{checkpoint} reference window changed"
            )
        language = item.get("source_language", NOVEL_SOURCE_LANGUAGE[novel_id])
        if language != NOVEL_SOURCE_LANGUAGE[novel_id]:
            raise G11BlindProtocolError(f"{novel_id} source language changed")
        chapters = item.get("reference_chapters")
        if not isinstance(chapters, list) or len(chapters) != window[1] - window[0] + 1:
            raise G11BlindProtocolError(
                f"{novel_id} Ch{checkpoint} reference chapters are incomplete"
            )
        cleaned_chapters: list[dict[str, Any]] = []
        for offset, chapter in enumerate(range(window[0], window[1] + 1)):
            row = chapters[offset]
            if not isinstance(row, Mapping) or row.get("chapter") != chapter:
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} reference chapter order drifted"
                )
            text = row.get("source_text")
            if not isinstance(text, str) or not text.strip():
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} Ch{chapter} source text is missing"
                )
            if row.get("source_text_sha256") != _sha256_text(text):
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} Ch{chapter} source hash changed"
                )
            cleaned_chapters.append(
                {
                    "chapter": chapter,
                    "source_text": text,
                    "source_text_sha256": _sha256_text(text),
                }
            )
        tracks = item.get("tracks")
        if not isinstance(tracks, Mapping) or set(tracks) != set(MODEL_ORDER):
            raise G11BlindProtocolError(
                f"{novel_id} Ch{checkpoint} requires both model projections"
            )
        clean_tracks: dict[str, dict[str, Any]] = {}
        for model_id in MODEL_ORDER:
            projection = tracks[model_id]
            if not isinstance(projection, Mapping):
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} {model_id} projection is invalid"
                )
            clean_tracks[model_id] = copy.deepcopy(dict(projection))
        result[(novel_id, checkpoint)] = {
            "novel_id": novel_id,
            "checkpoint": checkpoint,
            "source_language": language,
            "reference_window": [window[0], window[1]],
            "reference_chapters": cleaned_chapters,
            "tracks": clean_tracks,
        }
    if set(result) != set(roster):
        raise G11BlindProtocolError("longitudinal input roster is incomplete")
    return result


def build_public_blind_bundle(
    *,
    private_key: Mapping[str, Any],
    literary_records: Sequence[Mapping[str, Any]],
    longitudinal_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the public anonymous bundle without exposing identity metadata."""
    validate_private_identity_key(private_key)
    literary_map = _private_literary_map(private_key)
    long_map = _private_longitudinal_map(private_key)
    literary_input = _literary_input_map(literary_records)
    long_input = _longitudinal_input_map(longitudinal_records)
    commitment = _sha256_json(copy.deepcopy(dict(private_key)))

    literary_cases: list[dict[str, Any]] = []
    for row in literary_roster():
        pair_id = row["pair_id"]
        mapping = literary_map[pair_id]
        source = literary_input[pair_id]
        a_model = mapping["side_a_model_id"]
        b_model = mapping["side_b_model_id"]
        literary_cases.append(
            {
                "visible_pair_id": pair_id,
                "novel_id": row["novel_id"],
                "source_language": source["source_language"],
                "chapter": row["chapter"],
                "source_text": source["source_text"],
                "source_text_sha256": source["source_text_sha256"],
                "side_a": {
                    "translation": source["translations"][a_model],
                    "translation_sha256": _sha256_text(
                        source["translations"][a_model]
                    ),
                },
                "side_b": {
                    "translation": source["translations"][b_model],
                    "translation_sha256": _sha256_text(
                        source["translations"][b_model]
                    ),
                },
                "score_record_required": True,
            }
        )

    longitudinal_cases: list[dict[str, Any]] = []
    for novel_id in NOVEL_ORDER:
        mapping = long_map[novel_id]
        for checkpoint in LONGITUDINAL_CHECKPOINTS:
            source = long_input[(novel_id, checkpoint)]
            a_model = mapping["side_a_model_id"]
            b_model = mapping["side_b_model_id"]
            longitudinal_cases.append(
                {
                    "visible_track_pair_id": _longitudinal_pair_id(
                        novel_id, checkpoint
                    ),
                    "novel_id": novel_id,
                    "source_language": source["source_language"],
                    "checkpoint": checkpoint,
                    "reference_window": source["reference_window"],
                    "orientation_stable_across_checkpoints": True,
                    "reference_chapters": copy.deepcopy(source["reference_chapters"]),
                    "side_a": copy.deepcopy(source["tracks"][a_model]),
                    "side_b": copy.deepcopy(source["tracks"][b_model]),
                    "score_record_required": True,
                }
            )

    bundle = {
        "schema_version": PUBLIC_BUNDLE_SCHEMA_VERSION,
        "revision": PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_final_freeze_id": private_key["g11_final_freeze_id"],
        "private_identity_key_commitment_sha256": commitment,
        "literary_rubric": copy.deepcopy(LITERARY_RUBRIC),
        "longitudinal_rubric": copy.deepcopy(LONGITUDINAL_RUBRIC),
        "statistics_policy": copy.deepcopy(STATISTICS_POLICY),
        "literary_cases": literary_cases,
        "longitudinal_cases": longitudinal_cases,
        "identity_revealed": False,
        "scoring_performed": False,
        "provider_calls": 0,
        "credential_access": False,
    }
    validate_public_blind_bundle(bundle)
    return bundle


def public_bundle_sha256(bundle: Mapping[str, Any]) -> str:
    validate_public_blind_bundle(bundle)
    return _sha256_json(copy.deepcopy(dict(bundle)))


def validate_public_blind_bundle(bundle: Mapping[str, Any]) -> None:
    if not isinstance(bundle, Mapping):
        raise G11BlindProtocolError("public bundle must be an object")
    checks = {
        "schema_version": PUBLIC_BUNDLE_SCHEMA_VERSION,
        "revision": PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "identity_revealed": False,
        "scoring_performed": False,
        "provider_calls": 0,
        "credential_access": False,
    }
    for key, expected in checks.items():
        if bundle.get(key) != expected:
            raise G11BlindProtocolError(f"public bundle changed for {key}")
    _require_sha256(bundle.get("g11_final_freeze_id"), "public bundle final-freeze id")
    _require_sha256(
        bundle.get("private_identity_key_commitment_sha256"),
        "public bundle private-key commitment",
    )
    if bundle.get("literary_rubric") != LITERARY_RUBRIC:
        raise G11BlindProtocolError("public literary rubric changed")
    if bundle.get("longitudinal_rubric") != LONGITUDINAL_RUBRIC:
        raise G11BlindProtocolError("public longitudinal rubric changed")
    if bundle.get("statistics_policy") != STATISTICS_POLICY:
        raise G11BlindProtocolError("public statistics policy changed")

    literary = bundle.get("literary_cases")
    if not isinstance(literary, list) or len(literary) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError("public literary case count changed")
    expected_literary = {row["pair_id"]: row for row in literary_roster()}
    seen: set[str] = set()
    for case in literary:
        if not isinstance(case, Mapping):
            raise G11BlindProtocolError("public literary case is invalid")
        pair_id = case.get("visible_pair_id")
        if pair_id not in expected_literary or pair_id in seen:
            raise G11BlindProtocolError("public literary case ID is invalid")
        seen.add(pair_id)
        frozen = expected_literary[pair_id]
        if case.get("novel_id") != frozen["novel_id"] or case.get("chapter") != frozen["chapter"]:
            raise G11BlindProtocolError(f"{pair_id} source identity changed")
        if case.get("source_language") != NOVEL_SOURCE_LANGUAGE[frozen["novel_id"]]:
            raise G11BlindProtocolError(f"{pair_id} source language changed")
        source_text = case.get("source_text")
        if not isinstance(source_text, str) or _sha256_text(source_text) != case.get(
            "source_text_sha256"
        ):
            raise G11BlindProtocolError(f"{pair_id} source text hash changed")
        if case.get("score_record_required") is not True:
            raise G11BlindProtocolError(f"{pair_id} scoreability flag changed")
        for side in ("side_a", "side_b"):
            candidate = case.get(side)
            if not isinstance(candidate, Mapping):
                raise G11BlindProtocolError(f"{pair_id} {side} is invalid")
            text = candidate.get("translation")
            if not isinstance(text, str) or not text.strip():
                raise G11BlindProtocolError(f"{pair_id} {side} translation is empty")
            if _sha256_text(text) != candidate.get("translation_sha256"):
                raise G11BlindProtocolError(f"{pair_id} {side} translation hash changed")
    if seen != set(expected_literary):
        raise G11BlindProtocolError("public literary roster is incomplete")

    longitudinal = bundle.get("longitudinal_cases")
    if not isinstance(longitudinal, list) or len(longitudinal) != LONGITUDINAL_PAIR_COUNT:
        raise G11BlindProtocolError("public longitudinal case count changed")
    expected_long = {
        (row["novel_id"], row["checkpoint"]): row for row in longitudinal_roster()
    }
    seen_long: set[tuple[str, int]] = set()
    for case in longitudinal:
        if not isinstance(case, Mapping):
            raise G11BlindProtocolError("public longitudinal case is invalid")
        key = (case.get("novel_id"), case.get("checkpoint"))
        if key not in expected_long or key in seen_long:
            raise G11BlindProtocolError("public longitudinal case ID is invalid")
        seen_long.add(key)
        novel_id, checkpoint = key
        window = REFERENCE_WINDOWS[checkpoint]
        if case.get("reference_window") != [window[0], window[1]]:
            raise G11BlindProtocolError(f"{novel_id} Ch{checkpoint} window changed")
        if case.get("source_language") != NOVEL_SOURCE_LANGUAGE[novel_id]:
            raise G11BlindProtocolError(f"{novel_id} Ch{checkpoint} language changed")
        if case.get("orientation_stable_across_checkpoints") is not True:
            raise G11BlindProtocolError("longitudinal orientation stability flag changed")
        if case.get("score_record_required") is not True:
            raise G11BlindProtocolError("longitudinal scoreability flag changed")
        chapters = case.get("reference_chapters")
        if not isinstance(chapters, list) or len(chapters) != window[1] - window[0] + 1:
            raise G11BlindProtocolError(
                f"{novel_id} Ch{checkpoint} reference chapters are incomplete"
            )
        for offset, chapter in enumerate(range(window[0], window[1] + 1)):
            row = chapters[offset]
            if not isinstance(row, Mapping) or row.get("chapter") != chapter:
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} reference chapter order changed"
                )
            if _sha256_text(str(row.get("source_text"))) != row.get(
                "source_text_sha256"
            ):
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} Ch{chapter} source hash changed"
                )
        for side in ("side_a", "side_b"):
            if not isinstance(case.get(side), Mapping):
                raise G11BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} {side} projection is invalid"
                )
    if seen_long != set(expected_long):
        raise G11BlindProtocolError("public longitudinal roster is incomplete")

    assert_no_forbidden_metadata(
        {"literary_cases": literary, "longitudinal_cases": longitudinal}
    )
    assert_no_public_identity_leak(bundle)


# --------------------------------------------------------------------------- #
# Anonymous score freeze
# --------------------------------------------------------------------------- #


def _score_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G11BlindProtocolError(f"{label} must be numeric")
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 10.0:
        raise G11BlindProtocolError(f"{label} must be within 0..10")
    return score


def _validate_dimension_scores(
    value: Any,
    dimensions: tuple[str, ...],
    label: str,
) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(dimensions):
        raise G11BlindProtocolError(f"{label} dimensions are incomplete or changed")
    return {
        dimension: _score_number(value[dimension], f"{label}.{dimension}")
        for dimension in dimensions
    }


def _validate_flags(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(LITERARY_DEFECT_FLAGS):
        raise G11BlindProtocolError(f"{label} defect flags are incomplete or changed")
    result = {}
    for name in LITERARY_DEFECT_FLAGS:
        raw = value[name]
        if raw not in (0, 1, False, True):
            raise G11BlindProtocolError(f"{label}.{name} must be 0/1")
        result[name] = int(bool(raw))
    return result


def _weighted_total(scores: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return round(sum(scores[name] * float(weights[name]) for name in weights), 6)


def _preference(value: Any, label: str) -> str:
    if value not in PREFERENCE_VALUES:
        raise G11BlindProtocolError(f"{label} preference must be A/B/TIE")
    return str(value)


def freeze_anonymous_scores(
    *,
    public_bundle: Mapping[str, Any],
    literary_score_rows: Sequence[Mapping[str, Any]],
    longitudinal_score_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze all 28 literary and all 8 longitudinal anonymous rows."""
    validate_public_blind_bundle(public_bundle)
    bundle_sha = _sha256_json(copy.deepcopy(dict(public_bundle)))
    commitment = public_bundle["private_identity_key_commitment_sha256"]

    required_literary = [
        case["visible_pair_id"] for case in public_bundle["literary_cases"]
    ]
    if len(literary_score_rows) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError(
            "literary score rows must cover all 28 literary cases"
        )
    literary_by_id: dict[str, dict[str, Any]] = {}
    for row in literary_score_rows:
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("literary score row must be an object")
        pair_id = row.get("visible_pair_id")
        if pair_id not in required_literary or pair_id in literary_by_id:
            raise G11BlindProtocolError(
                "literary score pair ID is invalid or duplicated"
            )
        sides: dict[str, Any] = {}
        for side in ("side_a", "side_b"):
            value = row.get(side)
            if not isinstance(value, Mapping):
                raise G11BlindProtocolError(f"{pair_id} {side} score is missing")
            scores = _validate_dimension_scores(
                value.get("scores"), LITERARY_DIMENSIONS, f"{pair_id}.{side}"
            )
            sides[side] = {
                "scores": scores,
                "weighted_total": _weighted_total(scores, LITERARY_RUBRIC["weights"]),
                "defect_flags": _validate_flags(
                    value.get("defect_flags"), f"{pair_id}.{side}"
                ),
                "notes": str(value.get("notes", "")),
            }
        literary_by_id[pair_id] = {
            "visible_pair_id": pair_id,
            **sides,
            "preference": _preference(row.get("preference"), pair_id),
        }
    if set(literary_by_id) != set(required_literary):
        raise G11BlindProtocolError("literary score roster is incomplete")

    required_long = [
        case["visible_track_pair_id"] for case in public_bundle["longitudinal_cases"]
    ]
    if len(longitudinal_score_rows) != LONGITUDINAL_PAIR_COUNT:
        raise G11BlindProtocolError(
            "longitudinal score rows must cover all 8 longitudinal cases"
        )
    longitudinal_by_id: dict[str, dict[str, Any]] = {}
    for row in longitudinal_score_rows:
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("longitudinal score row must be an object")
        visible_id = row.get("visible_track_pair_id")
        if visible_id not in required_long or visible_id in longitudinal_by_id:
            raise G11BlindProtocolError(
                "longitudinal score pair ID is invalid or duplicated"
            )
        sides = {}
        for side in ("side_a", "side_b"):
            value = row.get(side)
            if not isinstance(value, Mapping):
                raise G11BlindProtocolError(f"{visible_id} {side} score is missing")
            scores = _validate_dimension_scores(
                value.get("scores"),
                LONGITUDINAL_DIMENSIONS,
                f"{visible_id}.{side}",
            )
            sides[side] = {
                "scores": scores,
                "weighted_total": _weighted_total(
                    scores, LONGITUDINAL_RUBRIC["weights"]
                ),
                "notes": str(value.get("notes", "")),
            }
        longitudinal_by_id[visible_id] = {
            "visible_track_pair_id": visible_id,
            **sides,
            "preference": _preference(row.get("preference"), visible_id),
        }
    if set(longitudinal_by_id) != set(required_long):
        raise G11BlindProtocolError("longitudinal score roster is incomplete")

    frozen = {
        "schema_version": SCORE_FREEZE_SCHEMA_VERSION,
        "revision": SCORE_FREEZE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "public_bundle_sha256": bundle_sha,
        "private_identity_key_commitment_sha256": commitment,
        "literary_rows": [literary_by_id[key] for key in required_literary],
        "longitudinal_rows": [longitudinal_by_id[key] for key in required_long],
        "literary_case_count": LITERARY_PAIR_COUNT,
        "longitudinal_case_count": LONGITUDINAL_PAIR_COUNT,
        "identity_revealed": False,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
    }
    frozen["score_freeze_id"] = _sha256_json(frozen)
    return frozen


def _validate_frozen_literary_row(
    row: Any,
    expected_pair_id: str,
) -> None:
    if not isinstance(row, Mapping):
        raise G11BlindProtocolError("frozen literary score row must be an object")
    if set(row) != {"visible_pair_id", "side_a", "side_b", "preference"}:
        raise G11BlindProtocolError("frozen literary score row shape changed")
    if row.get("visible_pair_id") != expected_pair_id:
        raise G11BlindProtocolError("score freeze literary roster changed")
    for side in ("side_a", "side_b"):
        value = row.get(side)
        if not isinstance(value, Mapping):
            raise G11BlindProtocolError(
                f"{expected_pair_id} {side} frozen score is invalid"
            )
        if set(value) != {"scores", "weighted_total", "defect_flags", "notes"}:
            raise G11BlindProtocolError(
                f"{expected_pair_id} {side} frozen score shape changed"
            )
        scores = _validate_dimension_scores(
            value.get("scores"),
            LITERARY_DIMENSIONS,
            f"{expected_pair_id}.{side}",
        )
        expected_total = _weighted_total(scores, LITERARY_RUBRIC["weights"])
        stored_total = _score_number(
            value.get("weighted_total"),
            f"{expected_pair_id}.{side}.weighted_total",
        )
        if stored_total != expected_total:
            raise G11BlindProtocolError(
                f"{expected_pair_id} {side} weighted total does not recompute"
            )
        _validate_flags(value.get("defect_flags"), f"{expected_pair_id}.{side}")
        if not isinstance(value.get("notes"), str):
            raise G11BlindProtocolError(
                f"{expected_pair_id} {side} notes must be text"
            )
    _preference(row.get("preference"), expected_pair_id)


def _validate_frozen_longitudinal_row(
    row: Any,
    expected_visible_id: str,
) -> None:
    if not isinstance(row, Mapping):
        raise G11BlindProtocolError(
            "frozen longitudinal score row must be an object"
        )
    if set(row) != {"visible_track_pair_id", "side_a", "side_b", "preference"}:
        raise G11BlindProtocolError("frozen longitudinal score row shape changed")
    if row.get("visible_track_pair_id") != expected_visible_id:
        raise G11BlindProtocolError("score freeze longitudinal roster changed")
    for side in ("side_a", "side_b"):
        value = row.get(side)
        if not isinstance(value, Mapping):
            raise G11BlindProtocolError(
                f"{expected_visible_id} {side} frozen score is invalid"
            )
        if set(value) != {"scores", "weighted_total", "notes"}:
            raise G11BlindProtocolError(
                f"{expected_visible_id} {side} frozen score shape changed"
            )
        scores = _validate_dimension_scores(
            value.get("scores"),
            LONGITUDINAL_DIMENSIONS,
            f"{expected_visible_id}.{side}",
        )
        expected_total = _weighted_total(
            scores, LONGITUDINAL_RUBRIC["weights"]
        )
        stored_total = _score_number(
            value.get("weighted_total"),
            f"{expected_visible_id}.{side}.weighted_total",
        )
        if stored_total != expected_total:
            raise G11BlindProtocolError(
                f"{expected_visible_id} {side} weighted total does not recompute"
            )
        if not isinstance(value.get("notes"), str):
            raise G11BlindProtocolError(
                f"{expected_visible_id} {side} notes must be text"
            )
    _preference(row.get("preference"), expected_visible_id)


def validate_score_freeze(
    public_bundle: Mapping[str, Any],
    score_freeze: Mapping[str, Any],
) -> None:
    validate_public_blind_bundle(public_bundle)
    if not isinstance(score_freeze, Mapping):
        raise G11BlindProtocolError("score freeze must be an object")
    checks = {
        "schema_version": SCORE_FREEZE_SCHEMA_VERSION,
        "revision": SCORE_FREEZE_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "identity_revealed": False,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
        "literary_case_count": LITERARY_PAIR_COUNT,
        "longitudinal_case_count": LONGITUDINAL_PAIR_COUNT,
    }
    for key, expected in checks.items():
        if score_freeze.get(key) != expected:
            raise G11BlindProtocolError(f"score freeze changed {key}")
    if score_freeze.get("public_bundle_sha256") != _sha256_json(
        copy.deepcopy(dict(public_bundle))
    ):
        raise G11BlindProtocolError("score freeze binds another public bundle")
    if (
        score_freeze.get("private_identity_key_commitment_sha256")
        != public_bundle.get("private_identity_key_commitment_sha256")
    ):
        raise G11BlindProtocolError("score freeze private-key commitment changed")

    literary_rows = score_freeze.get("literary_rows")
    if not isinstance(literary_rows, list) or len(literary_rows) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError("score freeze literary row count changed")
    expected_literary = [
        case["visible_pair_id"] for case in public_bundle["literary_cases"]
    ]
    for row, expected_pair_id in zip(literary_rows, expected_literary):
        _validate_frozen_literary_row(row, expected_pair_id)

    longitudinal_rows = score_freeze.get("longitudinal_rows")
    if (
        not isinstance(longitudinal_rows, list)
        or len(longitudinal_rows) != LONGITUDINAL_PAIR_COUNT
    ):
        raise G11BlindProtocolError("score freeze longitudinal row count changed")
    expected_long = [
        case["visible_track_pair_id"] for case in public_bundle["longitudinal_cases"]
    ]
    for row, expected_visible_id in zip(longitudinal_rows, expected_long):
        _validate_frozen_longitudinal_row(row, expected_visible_id)

    stored = score_freeze.get("score_freeze_id")
    _require_sha256(stored, "score freeze ID")
    check = copy.deepcopy(dict(score_freeze))
    check.pop("score_freeze_id", None)
    if _sha256_json(check) != stored:
        raise G11BlindProtocolError("score freeze ID does not recompute")


# --------------------------------------------------------------------------- #
# Identity reveal
# --------------------------------------------------------------------------- #


def validate_identity_reveal(reveal: Mapping[str, Any]) -> None:
    if not isinstance(reveal, Mapping):
        raise G11BlindProtocolError("identity reveal must be an object")
    checks = {
        "schema_version": REVEAL_SCHEMA_VERSION,
        "revision": REVEAL_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "identity_revealed": True,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
        "literary_case_count": LITERARY_PAIR_COUNT,
        "longitudinal_case_count": LONGITUDINAL_PAIR_COUNT,
    }
    for key, expected in checks.items():
        if reveal.get(key) != expected:
            raise G11BlindProtocolError(f"identity reveal changed {key}")
    _require_sha256(reveal.get("public_bundle_sha256"), "revealed public bundle SHA")
    _require_sha256(reveal.get("score_freeze_id"), "revealed score freeze ID")
    _require_sha256(
        reveal.get("private_identity_key_commitment_sha256"),
        "revealed private-key commitment",
    )

    literary_rows = reveal.get("literary_rows")
    if not isinstance(literary_rows, list) or len(literary_rows) != LITERARY_PAIR_COUNT:
        raise G11BlindProtocolError("identity reveal literary row count changed")
    expected_literary = literary_roster()
    for row, frozen in zip(literary_rows, expected_literary):
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("revealed literary row is invalid")
        if row.get("visible_pair_id") != frozen["pair_id"]:
            raise G11BlindProtocolError("identity reveal literary roster changed")
        if (
            row.get("novel_id") != frozen["novel_id"]
            or row.get("chapter") != frozen["chapter"]
            or row.get("source_language")
            != NOVEL_SOURCE_LANGUAGE[frozen["novel_id"]]
        ):
            raise G11BlindProtocolError("revealed literary source identity changed")
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G11BlindProtocolError("revealed literary sides are invalid")
        _validate_frozen_literary_row(
            {
                "visible_pair_id": row.get("visible_pair_id"),
                "side_a": row.get("side_a"),
                "side_b": row.get("side_b"),
                "preference": row.get("preference"),
            },
            frozen["pair_id"],
        )

    longitudinal_rows = reveal.get("longitudinal_rows")
    if (
        not isinstance(longitudinal_rows, list)
        or len(longitudinal_rows) != LONGITUDINAL_PAIR_COUNT
    ):
        raise G11BlindProtocolError("identity reveal longitudinal row count changed")
    expected_long = longitudinal_roster()
    for row, frozen in zip(longitudinal_rows, expected_long):
        if not isinstance(row, Mapping):
            raise G11BlindProtocolError("revealed longitudinal row is invalid")
        if row.get("visible_track_pair_id") != frozen["pair_id"]:
            raise G11BlindProtocolError("identity reveal longitudinal roster changed")
        if (
            row.get("novel_id") != frozen["novel_id"]
            or row.get("checkpoint") != frozen["checkpoint"]
        ):
            raise G11BlindProtocolError(
                "revealed longitudinal source identity changed"
            )
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G11BlindProtocolError("revealed longitudinal sides are invalid")
        _validate_frozen_longitudinal_row(
            {
                "visible_track_pair_id": row.get("visible_track_pair_id"),
                "side_a": row.get("side_a"),
                "side_b": row.get("side_b"),
                "preference": row.get("preference"),
            },
            frozen["pair_id"],
        )

    _require_sha256(reveal.get("reveal_id"), "identity reveal ID")
    check = copy.deepcopy(dict(reveal))
    check.pop("reveal_id", None)
    if _sha256_json(check) != reveal["reveal_id"]:
        raise G11BlindProtocolError("identity reveal ID does not recompute")


def reveal_frozen_scores(
    *,
    public_bundle: Mapping[str, Any],
    score_freeze: Mapping[str, Any],
    private_key: Mapping[str, Any],
) -> dict[str, Any]:
    """Reveal identities only after the complete anonymous score freeze."""
    validate_public_blind_bundle(public_bundle)
    validate_score_freeze(public_bundle, score_freeze)
    validate_private_identity_key(private_key)
    commitment = _sha256_json(copy.deepcopy(dict(private_key)))
    if commitment != public_bundle["private_identity_key_commitment_sha256"]:
        raise G11BlindProtocolError("private identity key commitment mismatch")

    literary_map = _private_literary_map(private_key)
    long_map = _private_longitudinal_map(private_key)

    literary_public = {
        case["visible_pair_id"]: case for case in public_bundle["literary_cases"]
    }
    literary_rows = []
    for row in score_freeze["literary_rows"]:
        pair_id = row["visible_pair_id"]
        mapping = literary_map[pair_id]
        public_case = literary_public[pair_id]
        literary_rows.append(
            {
                "visible_pair_id": pair_id,
                "novel_id": mapping["novel_id"],
                "chapter": mapping["chapter"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "side_a": copy.deepcopy(row["side_a"]),
                "side_b": copy.deepcopy(row["side_b"]),
                "preference": row["preference"],
                "source_language": public_case["source_language"],
            }
        )

    longitudinal_public = {
        case["visible_track_pair_id"]: case
        for case in public_bundle["longitudinal_cases"]
    }
    longitudinal_rows = []
    for row in score_freeze["longitudinal_rows"]:
        visible_id = row["visible_track_pair_id"]
        public_case = longitudinal_public[visible_id]
        mapping = long_map[public_case["novel_id"]]
        longitudinal_rows.append(
            {
                "visible_track_pair_id": visible_id,
                "novel_id": public_case["novel_id"],
                "checkpoint": public_case["checkpoint"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "side_a": copy.deepcopy(row["side_a"]),
                "side_b": copy.deepcopy(row["side_b"]),
                "preference": row["preference"],
            }
        )

    revealed = {
        "schema_version": REVEAL_SCHEMA_VERSION,
        "revision": REVEAL_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "public_bundle_sha256": _sha256_json(copy.deepcopy(dict(public_bundle))),
        "score_freeze_id": score_freeze["score_freeze_id"],
        "private_identity_key_commitment_sha256": commitment,
        "literary_rows": literary_rows,
        "longitudinal_rows": longitudinal_rows,
        "literary_case_count": LITERARY_PAIR_COUNT,
        "longitudinal_case_count": LONGITUDINAL_PAIR_COUNT,
        "identity_revealed": True,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
    }
    revealed["reveal_id"] = _sha256_json(revealed)
    validate_identity_reveal(revealed)
    return revealed


# --------------------------------------------------------------------------- #
# Contract identity
# --------------------------------------------------------------------------- #


def g11l_contract_identity() -> dict[str, Any]:
    if abs(sum(LITERARY_RUBRIC["weights"].values()) - 1.0) > 1e-12:
        raise G11BlindProtocolError("G11 literary rubric weights drifted")
    if abs(sum(LONGITUDINAL_RUBRIC["weights"].values()) - 1.0) > 1e-12:
        raise G11BlindProtocolError("G11 longitudinal rubric weights drifted")
    return {
        "schema_version": G11L_SCHEMA_VERSION,
        "revision": G11L_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "g11_final_freeze_id": G11_FINAL_FREEZE_ID,
        "predecessors": {
            "g11_preregistration_sha256": G11_PREREGISTRATION_SHA256,
        },
        "literary": {
            "case_count": LITERARY_PAIR_COUNT,
            "per_novel_count": LITERARY_PER_NOVEL,
            "checkpoints": list(LITERARY_CHECKPOINTS),
            "within_novel_orientation_balanced_7_7": True,
            "rubric": copy.deepcopy(LITERARY_RUBRIC),
        },
        "longitudinal": {
            "case_count": LONGITUDINAL_PAIR_COUNT,
            "checkpoints": list(LONGITUDINAL_CHECKPOINTS),
            "orientation_stable_within_novel_across_checkpoints": True,
            "one_novel_each_orientation": True,
            "rubric": copy.deepcopy(LONGITUDINAL_RUBRIC),
        },
        "blindness": {
            "private_identity_key_commitment_required": True,
            "model_metadata_removed": True,
            "effort_metadata_removed": True,
            "cost_transport_request_journal_metadata_removed": True,
            "scores_freeze_before_reveal": True,
            "reveal_requires_exact_private_key_commitment": True,
            "exact_identity_literals_forbidden": list(FORBIDDEN_PUBLIC_EXACT_LITERALS),
        },
        "statistics_policy": copy.deepcopy(STATISTICS_POLICY),
        "provider_calls": 0,
        "credential_access": False,
        "scoring_performed": False,
        "identity_revealed": False,
        "paid_execution_authorized": False,
    }


G11L_CONTRACT_SHA256 = _sha256_json(g11l_contract_identity())


def assert_paid_execution_blocked() -> None:
    identity = g11l_contract_identity()
    if identity["provider_calls"] != 0:
        raise G11BlindProtocolError("G11L unexpectedly reports Provider calls")
    if identity["paid_execution_authorized"] is not False:
        raise G11BlindProtocolError("G11L unexpectedly authorizes paid execution")
    if identity["credential_access"] is not False:
        raise G11BlindProtocolError("G11L unexpectedly authorizes credential access")


__all__ = [
    "DEEPSEEK_MODEL_ID",
    "FORBIDDEN_PUBLIC_EXACT_LITERALS",
    "FORBIDDEN_PUBLIC_METADATA_TOKENS",
    "G11_BENCHMARK_ID",
    "G11_FINAL_FREEZE_ID",
    "G11BlindProtocolError",
    "G11L_CONTRACT_SHA256",
    "G11L_REVISION",
    "G11L_SCHEMA_VERSION",
    "GLM_MODEL_ID",
    "LITERARY_A_PER_MODEL_PER_NOVEL",
    "LITERARY_AVAILABLE",
    "LITERARY_CHECKPOINTS",
    "LITERARY_DEFECT_FLAGS",
    "LITERARY_DIMENSIONS",
    "LITERARY_PAIR_COUNT",
    "LITERARY_PER_NOVEL",
    "LITERARY_RUBRIC",
    "LONGITUDINAL_AVAILABLE",
    "LONGITUDINAL_CHECKPOINTS",
    "LONGITUDINAL_DIMENSIONS",
    "LONGITUDINAL_PAIR_COUNT",
    "LONGITUDINAL_RUBRIC",
    "MODEL_ORDER",
    "NOVEL_ORDER",
    "NOVEL_SOURCE_LANGUAGE",
    "PREFERENCE_VALUES",
    "REFERENCE_WINDOWS",
    "STATISTICS_POLICY",
    "assert_no_forbidden_metadata",
    "assert_no_public_identity_leak",
    "assert_paid_execution_blocked",
    "build_private_identity_key",
    "build_public_blind_bundle",
    "freeze_anonymous_scores",
    "g11l_contract_identity",
    "literary_roster",
    "longitudinal_roster",
    "private_identity_key_commitment",
    "public_bundle_sha256",
    "reveal_frozen_scores",
    "validate_identity_reveal",
    "validate_private_identity_key",
    "validate_public_blind_bundle",
    "validate_score_freeze",
]
