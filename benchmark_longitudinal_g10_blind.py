"""G10C zero-provider blind bundle, score-freeze, and reveal protocol.

G10C generalizes the proven G6 blind architecture for the Qwen-vs-GLM G10
experiment. It performs no Provider calls and contains no paid execution path.

Blindness rules:
- G10A literary candidates are anonymously labelled A/B.
- Primary literary orientation is cryptographically determined but exactly
  balanced: Qwen is A in 24 of the 48 primary cases and GLM is A in the other 24.
- The three bridge cases use cryptographic orientation but are descriptive only.
- G10B longitudinal orientation is cryptographically determined once per novel
  and stays stable across checkpoints 25 and 50, allowing blinded assessment of
  state evolution and audit effectiveness over time.
- Model, effort, cost, transport, request, journal, and accounting metadata are
  forbidden from the public evaluation bundle.
- Literary and longitudinal scores must freeze before private identity reveal.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g10_chronological import (
    G10B1_CONTRACT_SHA256,
)
from benchmark_longitudinal_g10_matched import (
    G10A2_CONTRACT_SHA256,
    PAIR_SPECS,
)
from benchmark_longitudinal_g10_preregistration import (
    BLIND_EVALUATION_POLICY,
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    LITERARY_RUBRIC,
    LONGITUDINAL_STATE_RUBRIC,
    MODEL_ORDER,
    NOVELS,
    QWEN_MODEL_ID,
    STATISTICS_POLICY,
)


G10C_SCHEMA_VERSION = 1
G10C_REVISION = "g10-blind-bundle-score-reveal-v1"

LITERARY_PRIMARY_PAIR_COUNT = 48
LITERARY_BRIDGE_PAIR_COUNT = 3
LITERARY_PAIR_COUNT = 51
LONGITUDINAL_CHECKPOINTS = (25, 50)
LONGITUDINAL_PAIR_COUNT = len(NOVELS) * len(LONGITUDINAL_CHECKPOINTS)

LITERARY_AVAILABLE = "paired_translation_available"
LITERARY_UNAVAILABLE = "unavailable_model_case"
LONGITUDINAL_AVAILABLE = "comparable_checkpoint_available"
LONGITUDINAL_UNAVAILABLE = "unavailable_due_track_reliability"

PRIVATE_KEY_SCHEMA_VERSION = 1
PRIVATE_KEY_REVISION = "g10-private-identity-key-v1"
PUBLIC_BUNDLE_SCHEMA_VERSION = 1
PUBLIC_BUNDLE_REVISION = "g10-public-blind-bundle-v1"
SCORE_FREEZE_SCHEMA_VERSION = 1
SCORE_FREEZE_REVISION = "g10-anonymous-score-freeze-v1"
REVEAL_SCHEMA_VERSION = 1
REVEAL_REVISION = "g10-identity-reveal-v1"

_SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

LITERARY_DIMENSIONS = tuple(LITERARY_RUBRIC["weights"])
LITERARY_DEFECT_FLAGS = tuple(LITERARY_RUBRIC["defect_flags"])
LONGITUDINAL_DIMENSIONS = tuple(LONGITUDINAL_STATE_RUBRIC["weights"])

PUBLIC_STATISTICS_POLICY = {
    "g10a_primary_pairs": STATISTICS_POLICY["g10a_primary_pairs"],
    "primary_strata": copy.deepcopy(STATISTICS_POLICY["primary_strata"]),
    "report_wtl_separately_by_language": STATISTICS_POLICY[
        "report_wtl_separately_by_language"
    ],
    "pooled_result_after_stratified_reporting_only": STATISTICS_POLICY[
        "pooled_result_after_stratified_reporting_only"
    ],
    "two_sided_exact_sign_test_on_non_ties": STATISTICS_POLICY[
        "two_sided_exact_sign_test_on_non_ties"
    ],
    "stratified_bootstrap_replicates": STATISTICS_POLICY[
        "stratified_bootstrap_replicates"
    ],
    "stratified_bootstrap_confidence": STATISTICS_POLICY[
        "stratified_bootstrap_confidence"
    ],
    "per_dimension_paired_differences_secondary": STATISTICS_POLICY[
        "per_dimension_paired_differences_secondary"
    ],
    "bridge_cases_descriptive_only": STATISTICS_POLICY[
        "bridge_cases_descriptive_only"
    ],
    "g10b_inference_mode": STATISTICS_POLICY["g10b_inference_mode"],
    "no_composite_overall_winner_score": STATISTICS_POLICY[
        "no_composite_overall_winner_score"
    ],
    "anonymous_preference_values": ("A", "B", "TIE"),
}

_FORBIDDEN_PUBLIC_METADATA_TOKENS = (
    "model_id",
    "model_name",
    "benchmark_model",
    "provider_model",
    "reasoning_effort",
    "effort",
    "credit_equivalent",
    "cost_usd",
    "provider",
    "transport",
    "request_sha",
    "journal",
    "reservation_id",
    "accounting",
)


class G10BlindProtocolError(RuntimeError):
    """A G10 blind-evaluation artifact violates the frozen protocol."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10BlindProtocolError(
            "G10 blind artifact is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_secret_hex(secret_hex: str) -> bytes:
    if not isinstance(secret_hex, str) or not _SECRET_RE.fullmatch(secret_hex):
        raise G10BlindProtocolError(
            "blind secret must be exactly 32 bytes as lowercase hex"
        )
    return bytes.fromhex(secret_hex)


def _hmac_rank(secret: bytes, namespace: str, logical_id: str) -> str:
    return hmac.new(
        secret,
        f"{namespace}|{logical_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _model_pair(side_a_model: str) -> tuple[str, str]:
    if side_a_model == QWEN_MODEL_ID:
        return QWEN_MODEL_ID, GLM_MODEL_ID
    if side_a_model == GLM_MODEL_ID:
        return GLM_MODEL_ID, QWEN_MODEL_ID
    raise G10BlindProtocolError(f"unexpected model identity: {side_a_model!r}")


def _pair_roster() -> tuple[dict[str, Any], ...]:
    rows = tuple(pair.identity() for pair in PAIR_SPECS)
    if len(rows) != LITERARY_PAIR_COUNT:
        raise G10BlindProtocolError("G10C literary pair roster changed")
    if sum(row["case_kind"] == "primary" for row in rows) != 48:
        raise G10BlindProtocolError("G10C primary pair roster changed")
    if sum(row["case_kind"] == "bridge" for row in rows) != 3:
        raise G10BlindProtocolError("G10C bridge pair roster changed")
    return rows


def build_private_identity_key(secret_hex: str) -> dict[str, Any]:
    """Derive the private A/B identity mapping from one 256-bit secret."""
    secret = _require_secret_hex(secret_hex)
    pairs = _pair_roster()
    primary = [row for row in pairs if row["case_kind"] == "primary"]
    bridges = [row for row in pairs if row["case_kind"] == "bridge"]

    # Exact 24/24 primary orientation, while selection remains secret-keyed.
    ranked_primary = sorted(
        primary,
        key=lambda row: (
            _hmac_rank(secret, "literary-primary", row["pair_id"]),
            row["pair_id"],
        ),
    )
    qwen_a_primary = {
        row["pair_id"] for row in ranked_primary[: LITERARY_PRIMARY_PAIR_COUNT // 2]
    }

    literary_mapping: list[dict[str, Any]] = []
    for row in pairs:
        if row["case_kind"] == "primary":
            side_a_model = (
                QWEN_MODEL_ID
                if row["pair_id"] in qwen_a_primary
                else GLM_MODEL_ID
            )
        else:
            rank = _hmac_rank(secret, "literary-bridge", row["pair_id"])
            side_a_model = (
                QWEN_MODEL_ID if int(rank[-1], 16) % 2 == 0 else GLM_MODEL_ID
            )
        side_a_model, side_b_model = _model_pair(side_a_model)
        literary_mapping.append(
            {
                "pair_id": row["pair_id"],
                "case_kind": row["case_kind"],
                "novel_id": row["novel_id"],
                "chapter": row["chapter"],
                "side_a_model_id": side_a_model,
                "side_b_model_id": side_b_model,
            }
        )

    # Two novels: force one novel to each A-side model while keeping the choice
    # secret-keyed. The chosen orientation stays stable at checkpoints 25/50.
    novel_ids = sorted(NOVELS)
    ranked_novels = sorted(
        novel_ids,
        key=lambda novel_id: (
            _hmac_rank(secret, "longitudinal-novel", novel_id),
            novel_id,
        ),
    )
    qwen_a_novel = ranked_novels[0]
    longitudinal_mapping = []
    for novel_id in novel_ids:
        side_a_model = QWEN_MODEL_ID if novel_id == qwen_a_novel else GLM_MODEL_ID
        side_a_model, side_b_model = _model_pair(side_a_model)
        longitudinal_mapping.append(
            {
                "novel_id": novel_id,
                "side_a_model_id": side_a_model,
                "side_b_model_id": side_b_model,
                "checkpoints": list(LONGITUDINAL_CHECKPOINTS),
            }
        )

    key = {
        "schema_version": PRIVATE_KEY_SCHEMA_VERSION,
        "revision": PRIVATE_KEY_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a2_contract_sha256": G10A2_CONTRACT_SHA256,
        "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        "secret_sha256": hashlib.sha256(secret).hexdigest(),
        "literary_mapping": literary_mapping,
        "longitudinal_mapping": longitudinal_mapping,
        "provider_calls": 0,
    }

    primary_a = [
        row["side_a_model_id"]
        for row in literary_mapping
        if row["case_kind"] == "primary"
    ]
    if primary_a.count(QWEN_MODEL_ID) != 24 or primary_a.count(GLM_MODEL_ID) != 24:
        raise G10BlindProtocolError(
            "G10C primary orientation is not exactly 24/24 balanced"
        )
    long_a = [row["side_a_model_id"] for row in longitudinal_mapping]
    if sorted(long_a) != sorted(MODEL_ORDER):
        raise G10BlindProtocolError(
            "G10C longitudinal novel orientation is not one-per-model"
        )
    return key


def private_identity_key_commitment(private_key: Mapping[str, Any]) -> str:
    return _sha256_json(copy.deepcopy(dict(private_key)))


def _private_literary_map(private_key: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = private_key.get("literary_mapping")
    if not isinstance(rows, list) or len(rows) != LITERARY_PAIR_COUNT:
        raise G10BlindProtocolError("private literary identity mapping is invalid")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("private literary mapping row is invalid")
        pair_id = row.get("pair_id")
        if not isinstance(pair_id, str) or pair_id in result:
            raise G10BlindProtocolError("private literary pair IDs are invalid")
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G10BlindProtocolError("private literary side mapping is invalid")
        result[pair_id] = copy.deepcopy(dict(row))
    return result


def _private_longitudinal_map(
    private_key: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    rows = private_key.get("longitudinal_mapping")
    if not isinstance(rows, list) or len(rows) != len(NOVELS):
        raise G10BlindProtocolError("private longitudinal identity mapping is invalid")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("private longitudinal mapping row is invalid")
        novel_id = row.get("novel_id")
        if novel_id not in NOVELS or novel_id in result:
            raise G10BlindProtocolError("private longitudinal novel mapping is invalid")
        if {row.get("side_a_model_id"), row.get("side_b_model_id")} != set(MODEL_ORDER):
            raise G10BlindProtocolError("private longitudinal side mapping is invalid")
        if tuple(row.get("checkpoints", ())) != LONGITUDINAL_CHECKPOINTS:
            raise G10BlindProtocolError("private longitudinal checkpoints drifted")
        result[novel_id] = copy.deepcopy(dict(row))
    return result


def _forbid_metadata(value: Any, path: str = "public") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key).casefold()
            if any(token in text for token in _FORBIDDEN_PUBLIC_METADATA_TOKENS):
                raise G10BlindProtocolError(
                    f"forbidden identity/accounting metadata key in {path}: {key}"
                )
            _forbid_metadata(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _forbid_metadata(item, f"{path}[{index}]")


def _validate_private_key(private_key: Mapping[str, Any]) -> None:
    if not isinstance(private_key, Mapping):
        raise G10BlindProtocolError("private identity key must be a mapping")
    if private_key.get("schema_version") != PRIVATE_KEY_SCHEMA_VERSION:
        raise G10BlindProtocolError("private identity key schema changed")
    if private_key.get("revision") != PRIVATE_KEY_REVISION:
        raise G10BlindProtocolError("private identity key revision changed")
    if private_key.get("benchmark_id") != G10_BENCHMARK_ID:
        raise G10BlindProtocolError("private identity key benchmark changed")
    if private_key.get("g10_preregistration_sha256") != G10_PREREGISTRATION_SHA256:
        raise G10BlindProtocolError("private key preregistration binding changed")
    if private_key.get("g10a2_contract_sha256") != G10A2_CONTRACT_SHA256:
        raise G10BlindProtocolError("private key G10A2 binding changed")
    if private_key.get("g10b1_contract_sha256") != G10B1_CONTRACT_SHA256:
        raise G10BlindProtocolError("private key G10B1 binding changed")
    digest = private_key.get("secret_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise G10BlindProtocolError("private key secret commitment is invalid")
    if private_key.get("provider_calls") != 0:
        raise G10BlindProtocolError("private key unexpectedly reports provider calls")
    _private_literary_map(private_key)
    _private_longitudinal_map(private_key)


def _literary_input_map(
    literary_records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    roster = {row["pair_id"]: row for row in _pair_roster()}
    if len(literary_records) != LITERARY_PAIR_COUNT:
        raise G10BlindProtocolError("literary input must contain exactly 51 planned pairs")
    result: dict[str, dict[str, Any]] = {}
    for item in literary_records:
        if not isinstance(item, Mapping):
            raise G10BlindProtocolError("literary input row must be an object")
        pair_id = item.get("pair_id")
        if pair_id not in roster or pair_id in result:
            raise G10BlindProtocolError("literary input pair_id is invalid or duplicated")
        frozen = roster[pair_id]
        for key in ("case_kind", "novel_id", "chapter"):
            if item.get(key) != frozen[key]:
                raise G10BlindProtocolError(
                    f"literary input {pair_id} changed frozen {key}"
                )
        source_text = item.get("source_text")
        if not isinstance(source_text, str) or not source_text:
            raise G10BlindProtocolError(f"{pair_id} source text is missing")

        availability = item.get("availability", LITERARY_AVAILABLE)
        if availability not in {LITERARY_AVAILABLE, LITERARY_UNAVAILABLE}:
            raise G10BlindProtocolError(
                f"{pair_id} literary availability is invalid"
            )
        translations = item.get("translations", {})
        if not isinstance(translations, Mapping):
            raise G10BlindProtocolError(
                f"{pair_id} translations must be a mapping"
            )
        if not set(translations).issubset(set(MODEL_ORDER)):
            raise G10BlindProtocolError(
                f"{pair_id} translations contain an unknown model"
            )
        clean_translations: dict[str, str] = {}
        for model_id, translation in translations.items():
            if not isinstance(translation, str) or not translation.strip():
                raise G10BlindProtocolError(
                    f"{pair_id} available translation is empty"
                )
            clean_translations[str(model_id)] = translation

        if availability == LITERARY_AVAILABLE:
            if set(clean_translations) != set(MODEL_ORDER):
                raise G10BlindProtocolError(
                    f"{pair_id} scoreable literary pair requires both translations"
                )
        elif len(clean_translations) == len(MODEL_ORDER):
            raise G10BlindProtocolError(
                f"{pair_id} unavailable literary pair unexpectedly has both translations"
            )

        result[str(pair_id)] = {
            "pair_id": pair_id,
            "case_kind": frozen["case_kind"],
            "novel_id": frozen["novel_id"],
            "chapter": frozen["chapter"],
            "source_text": source_text,
            "source_text_sha256": _sha256_text(source_text),
            "availability": availability,
            "translations": clean_translations,
        }
    if set(result) != set(roster):
        raise G10BlindProtocolError("literary input pair roster is incomplete")
    return result
def _longitudinal_input_map(
    longitudinal_records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    expected = {
        (novel_id, checkpoint)
        for novel_id in NOVELS
        for checkpoint in LONGITUDINAL_CHECKPOINTS
    }
    if len(longitudinal_records) != LONGITUDINAL_PAIR_COUNT:
        raise G10BlindProtocolError(
            "longitudinal input must contain exactly four planned checkpoint pairs"
        )
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for item in longitudinal_records:
        if not isinstance(item, Mapping):
            raise G10BlindProtocolError("longitudinal input row must be an object")
        novel_id = item.get("novel_id")
        checkpoint = item.get("checkpoint")
        key = (novel_id, checkpoint)
        if key not in expected or key in result:
            raise G10BlindProtocolError(
                "longitudinal novel/checkpoint is invalid or duplicated"
            )
        expected_window = [1, 25] if checkpoint == 25 else [26, 50]
        if list(item.get("audit_window", ())) != expected_window:
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} audit window changed"
            )
        reference_evidence = item.get("reference_evidence")
        if not isinstance(reference_evidence, Mapping):
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} reference evidence is missing"
            )
        _forbid_metadata(reference_evidence, "longitudinal-reference")

        availability = item.get("availability", LONGITUDINAL_AVAILABLE)
        if availability not in {
            LONGITUDINAL_AVAILABLE,
            LONGITUDINAL_UNAVAILABLE,
        }:
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} availability is invalid"
            )

        tracks = item.get("tracks", {})
        if not isinstance(tracks, Mapping):
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} tracks must be a mapping"
            )
        if not set(tracks).issubset(set(MODEL_ORDER)):
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} tracks contain an unknown model"
            )
        clean_tracks: dict[str, dict[str, Any]] = {}
        for model_id, evidence in tracks.items():
            if not isinstance(evidence, Mapping):
                raise G10BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} track evidence must be an object"
                )
            _forbid_metadata(evidence, "private-longitudinal-evidence")
            clean_tracks[str(model_id)] = copy.deepcopy(dict(evidence))

        if availability == LONGITUDINAL_AVAILABLE:
            if set(clean_tracks) != set(MODEL_ORDER):
                raise G10BlindProtocolError(
                    f"{novel_id} Ch{checkpoint} scoreable checkpoint requires both tracks"
                )
        elif len(clean_tracks) == len(MODEL_ORDER):
            raise G10BlindProtocolError(
                f"{novel_id} Ch{checkpoint} unavailable checkpoint unexpectedly has both tracks"
            )

        result[(str(novel_id), int(checkpoint))] = {
            "novel_id": novel_id,
            "checkpoint": checkpoint,
            "audit_window": expected_window,
            "reference_evidence": copy.deepcopy(dict(reference_evidence)),
            "availability": availability,
            "tracks": clean_tracks,
        }
    if set(result) != expected:
        raise G10BlindProtocolError("longitudinal input roster is incomplete")
    return result
def build_public_blind_bundle(
    *,
    private_key: Mapping[str, Any],
    literary_records: Sequence[Mapping[str, Any]],
    longitudinal_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the public anonymous bundle without exposing identity/cost metadata."""
    _validate_private_key(private_key)
    literary_map = _private_literary_map(private_key)
    long_map = _private_longitudinal_map(private_key)
    literary_input = _literary_input_map(literary_records)
    long_input = _longitudinal_input_map(longitudinal_records)
    commitment = private_identity_key_commitment(private_key)

    public_literary = []
    for pair in _pair_roster():
        pair_id = pair["pair_id"]
        mapping = literary_map[pair_id]
        source = literary_input[pair_id]
        row = {
            "visible_pair_id": pair_id,
            "case_kind": pair["case_kind"],
            "primary_endpoint": pair["case_kind"] == "primary",
            "novel_id": pair["novel_id"],
            "chapter": pair["chapter"],
            "source_text": source["source_text"],
            "source_text_sha256": source["source_text_sha256"],
            "availability": source["availability"],
            "score_record_required": source["availability"] == LITERARY_AVAILABLE,
        }
        if source["availability"] == LITERARY_AVAILABLE:
            a_model = mapping["side_a_model_id"]
            b_model = mapping["side_b_model_id"]
            row["side_a"] = {
                "translation": source["translations"][a_model],
                "translation_sha256": _sha256_text(
                    source["translations"][a_model]
                ),
            }
            row["side_b"] = {
                "translation": source["translations"][b_model],
                "translation_sha256": _sha256_text(
                    source["translations"][b_model]
                ),
            }
        public_literary.append(row)

    public_longitudinal = []
    for novel_id in sorted(NOVELS):
        mapping = long_map[novel_id]
        for checkpoint in LONGITUDINAL_CHECKPOINTS:
            source = long_input[(novel_id, checkpoint)]
            row = {
                "visible_track_pair_id": f"G10B-{novel_id}-CH{checkpoint:02d}",
                "novel_id": novel_id,
                "checkpoint": checkpoint,
                "audit_window": source["audit_window"],
                "orientation_stable_across_checkpoints": True,
                "reference_evidence": source["reference_evidence"],
                "availability": source["availability"],
                "score_record_required": (
                    source["availability"] == LONGITUDINAL_AVAILABLE
                ),
            }
            if source["availability"] == LONGITUDINAL_AVAILABLE:
                a_model = mapping["side_a_model_id"]
                b_model = mapping["side_b_model_id"]
                row["side_a"] = copy.deepcopy(source["tracks"][a_model])
                row["side_b"] = copy.deepcopy(source["tracks"][b_model])
            public_longitudinal.append(row)

    bundle = {
        "schema_version": PUBLIC_BUNDLE_SCHEMA_VERSION,
        "revision": PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a2_contract_sha256": G10A2_CONTRACT_SHA256,
        "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        "private_identity_key_commitment_sha256": commitment,
        "literary_rubric": copy.deepcopy(LITERARY_RUBRIC),
        "longitudinal_state_rubric": copy.deepcopy(LONGITUDINAL_STATE_RUBRIC),
        "statistics_policy": copy.deepcopy(PUBLIC_STATISTICS_POLICY),
        "literary_pairs": public_literary,
        "longitudinal_pairs": public_longitudinal,
        "identity_revealed": False,
        "cost_metadata_present": False,
        "provider_calls": 0,
    }
    _forbid_metadata(
        {
            "literary_pairs": bundle["literary_pairs"],
            "longitudinal_pairs": bundle["longitudinal_pairs"],
        }
    )
    return bundle


def public_bundle_sha256(bundle: Mapping[str, Any]) -> str:
    validate_public_blind_bundle(bundle)
    return _sha256_json(copy.deepcopy(dict(bundle)))


def validate_public_blind_bundle(bundle: Mapping[str, Any]) -> None:
    if not isinstance(bundle, Mapping):
        raise G10BlindProtocolError("public bundle must be an object")
    checks = {
        "schema_version": PUBLIC_BUNDLE_SCHEMA_VERSION,
        "revision": PUBLIC_BUNDLE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a2_contract_sha256": G10A2_CONTRACT_SHA256,
        "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        "identity_revealed": False,
        "cost_metadata_present": False,
        "provider_calls": 0,
    }
    for key, expected in checks.items():
        if bundle.get(key) != expected:
            raise G10BlindProtocolError(f"public bundle changed for {key}")
    commitment = bundle.get("private_identity_key_commitment_sha256")
    if not isinstance(commitment, str) or not _SHA256_RE.fullmatch(commitment):
        raise G10BlindProtocolError("public bundle private-key commitment is invalid")
    if bundle.get("literary_rubric") != LITERARY_RUBRIC:
        raise G10BlindProtocolError("public literary rubric changed")
    if bundle.get("longitudinal_state_rubric") != LONGITUDINAL_STATE_RUBRIC:
        raise G10BlindProtocolError("public longitudinal rubric changed")
    if bundle.get("statistics_policy") != PUBLIC_STATISTICS_POLICY:
        raise G10BlindProtocolError("public statistics policy changed")

    literary = bundle.get("literary_pairs")
    if not isinstance(literary, list) or len(literary) != LITERARY_PAIR_COUNT:
        raise G10BlindProtocolError("public literary pair count changed")
    expected_pairs = {row["pair_id"]: row for row in _pair_roster()}
    seen = set()
    for row in literary:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("public literary pair row is invalid")
        pair_id = row.get("visible_pair_id")
        if pair_id not in expected_pairs or pair_id in seen:
            raise G10BlindProtocolError("public literary pair ID is invalid")
        seen.add(pair_id)
        frozen = expected_pairs[pair_id]
        if row.get("case_kind") != frozen["case_kind"]:
            raise G10BlindProtocolError(f"{pair_id} case kind changed")
        if row.get("primary_endpoint") is not (frozen["case_kind"] == "primary"):
            raise G10BlindProtocolError(f"{pair_id} endpoint flag changed")
        if row.get("novel_id") != frozen["novel_id"] or row.get("chapter") != frozen["chapter"]:
            raise G10BlindProtocolError(f"{pair_id} source identity changed")
        source_text = row.get("source_text")
        if not isinstance(source_text, str):
            raise G10BlindProtocolError(f"{pair_id} source text is invalid")
        if _sha256_text(source_text) != row.get("source_text_sha256"):
            raise G10BlindProtocolError(f"{pair_id} source text hash changed")
        availability = row.get("availability")
        if availability not in {LITERARY_AVAILABLE, LITERARY_UNAVAILABLE}:
            raise G10BlindProtocolError(f"{pair_id} availability is invalid")
        required = availability == LITERARY_AVAILABLE
        if row.get("score_record_required") is not required:
            raise G10BlindProtocolError(f"{pair_id} scoreability flag changed")
        if required:
            for side in ("side_a", "side_b"):
                candidate = row.get(side)
                if not isinstance(candidate, Mapping):
                    raise G10BlindProtocolError(f"{pair_id} {side} is invalid")
                text = candidate.get("translation")
                if not isinstance(text, str) or not text.strip():
                    raise G10BlindProtocolError(f"{pair_id} {side} translation is empty")
                if _sha256_text(text) != candidate.get("translation_sha256"):
                    raise G10BlindProtocolError(f"{pair_id} {side} translation hash changed")
        elif "side_a" in row or "side_b" in row:
            raise G10BlindProtocolError(
                f"{pair_id} unavailable slot exposes candidate content"
            )

    longitudinal = bundle.get("longitudinal_pairs")
    if not isinstance(longitudinal, list) or len(longitudinal) != LONGITUDINAL_PAIR_COUNT:
        raise G10BlindProtocolError("public longitudinal pair count changed")
    seen_long = set()
    for row in longitudinal:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("public longitudinal row is invalid")
        novel_id = row.get("novel_id")
        checkpoint = row.get("checkpoint")
        key = (novel_id, checkpoint)
        if novel_id not in NOVELS or checkpoint not in LONGITUDINAL_CHECKPOINTS:
            raise G10BlindProtocolError("public longitudinal identity is invalid")
        if key in seen_long:
            raise G10BlindProtocolError("duplicate public longitudinal pair")
        seen_long.add(key)
        expected_window = [1, 25] if checkpoint == 25 else [26, 50]
        if row.get("audit_window") != expected_window:
            raise G10BlindProtocolError("public longitudinal audit window changed")
        if row.get("orientation_stable_across_checkpoints") is not True:
            raise G10BlindProtocolError("longitudinal orientation stability flag changed")
        if not isinstance(row.get("reference_evidence"), Mapping):
            raise G10BlindProtocolError("longitudinal reference evidence is invalid")
        availability = row.get("availability")
        if availability not in {LONGITUDINAL_AVAILABLE, LONGITUDINAL_UNAVAILABLE}:
            raise G10BlindProtocolError("longitudinal availability is invalid")
        required = availability == LONGITUDINAL_AVAILABLE
        if row.get("score_record_required") is not required:
            raise G10BlindProtocolError("longitudinal scoreability flag changed")
        if required:
            if not isinstance(row.get("side_a"), Mapping) or not isinstance(row.get("side_b"), Mapping):
                raise G10BlindProtocolError("longitudinal candidate evidence is invalid")
        elif "side_a" in row or "side_b" in row:
            raise G10BlindProtocolError(
                "unavailable longitudinal slot exposes candidate evidence"
            )

    _forbid_metadata(
        {
            "literary_pairs": literary,
            "longitudinal_pairs": longitudinal,
        }
    )


def _score_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G10BlindProtocolError(f"{label} must be numeric")
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 10.0:
        raise G10BlindProtocolError(f"{label} must be within 0..10")
    return score


def _validate_dimension_scores(
    value: Any,
    dimensions: tuple[str, ...],
    label: str,
) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(dimensions):
        raise G10BlindProtocolError(f"{label} dimensions are incomplete or changed")
    return {
        dimension: _score_number(value[dimension], f"{label}.{dimension}")
        for dimension in dimensions
    }


def _weighted_total(scores: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return round(sum(scores[name] * float(weights[name]) for name in weights), 6)


def _validate_flags(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(LITERARY_DEFECT_FLAGS):
        raise G10BlindProtocolError(f"{label} defect flags are incomplete or changed")
    result = {}
    for name in LITERARY_DEFECT_FLAGS:
        raw = value[name]
        if raw not in (0, 1, False, True):
            raise G10BlindProtocolError(f"{label}.{name} must be 0/1")
        result[name] = int(bool(raw))
    return result


def _preference(value: Any, label: str) -> str:
    if value not in {"A", "B", "TIE"}:
        raise G10BlindProtocolError(f"{label} preference must be A/B/TIE")
    return str(value)


def freeze_anonymous_scores(
    *,
    public_bundle: Mapping[str, Any],
    literary_score_rows: Sequence[Mapping[str, Any]],
    longitudinal_score_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze all scoreable anonymous rows while preserving unavailable slots."""
    validate_public_blind_bundle(public_bundle)
    bundle_sha = _sha256_json(copy.deepcopy(dict(public_bundle)))
    commitment = public_bundle["private_identity_key_commitment_sha256"]

    literary_required = [
        row["visible_pair_id"]
        for row in public_bundle["literary_pairs"]
        if row["score_record_required"]
    ]
    literary_unavailable = [
        row["visible_pair_id"]
        for row in public_bundle["literary_pairs"]
        if not row["score_record_required"]
    ]
    if len(literary_score_rows) != len(literary_required):
        raise G10BlindProtocolError("literary score row count is incomplete")
    literary_by_id = {}
    for row in literary_score_rows:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("literary score row must be an object")
        pair_id = row.get("visible_pair_id")
        if pair_id not in literary_required or pair_id in literary_by_id:
            raise G10BlindProtocolError(
                "literary score pair ID is invalid, unavailable, or duplicated"
            )
        side_rows = {}
        for side in ("side_a", "side_b"):
            value = row.get(side)
            if not isinstance(value, Mapping):
                raise G10BlindProtocolError(f"{pair_id} {side} score is missing")
            scores = _validate_dimension_scores(
                value.get("scores"),
                LITERARY_DIMENSIONS,
                f"{pair_id}.{side}",
            )
            flags = _validate_flags(
                value.get("defect_flags"),
                f"{pair_id}.{side}",
            )
            side_rows[side] = {
                "scores": scores,
                "weighted_total": _weighted_total(
                    scores, LITERARY_RUBRIC["weights"]
                ),
                "defect_flags": flags,
                "notes": str(value.get("notes", "")),
            }
        literary_by_id[pair_id] = {
            "visible_pair_id": pair_id,
            **side_rows,
            "preference": _preference(row.get("preference"), pair_id),
        }

    longitudinal_required = [
        row["visible_track_pair_id"]
        for row in public_bundle["longitudinal_pairs"]
        if row["score_record_required"]
    ]
    longitudinal_unavailable = [
        row["visible_track_pair_id"]
        for row in public_bundle["longitudinal_pairs"]
        if not row["score_record_required"]
    ]
    if len(longitudinal_score_rows) != len(longitudinal_required):
        raise G10BlindProtocolError("longitudinal score row count is incomplete")
    longitudinal_by_id = {}
    for row in longitudinal_score_rows:
        if not isinstance(row, Mapping):
            raise G10BlindProtocolError("longitudinal score row must be an object")
        visible_id = row.get("visible_track_pair_id")
        if (
            visible_id not in longitudinal_required
            or visible_id in longitudinal_by_id
        ):
            raise G10BlindProtocolError(
                "longitudinal score pair ID is invalid, unavailable, or duplicated"
            )
        side_rows = {}
        for side in ("side_a", "side_b"):
            value = row.get(side)
            if not isinstance(value, Mapping):
                raise G10BlindProtocolError(
                    f"{visible_id} {side} score is missing"
                )
            scores = _validate_dimension_scores(
                value.get("scores"),
                LONGITUDINAL_DIMENSIONS,
                f"{visible_id}.{side}",
            )
            side_rows[side] = {
                "scores": scores,
                "weighted_total": _weighted_total(
                    scores, LONGITUDINAL_STATE_RUBRIC["weights"]
                ),
                "notes": str(value.get("notes", "")),
            }
        longitudinal_by_id[visible_id] = {
            "visible_track_pair_id": visible_id,
            **side_rows,
            "preference": _preference(row.get("preference"), visible_id),
        }

    literary_primary_scoreable = sum(
        row["case_kind"] == "primary" and row["score_record_required"]
        for row in public_bundle["literary_pairs"]
    )
    literary_bridge_scoreable = sum(
        row["case_kind"] == "bridge" and row["score_record_required"]
        for row in public_bundle["literary_pairs"]
    )

    frozen = {
        "schema_version": SCORE_FREEZE_SCHEMA_VERSION,
        "revision": SCORE_FREEZE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "public_bundle_sha256": bundle_sha,
        "private_identity_key_commitment_sha256": commitment,
        "literary_rows": [
            literary_by_id[pair_id] for pair_id in literary_required
        ],
        "literary_unavailable_pair_ids": literary_unavailable,
        "longitudinal_rows": [
            longitudinal_by_id[visible_id]
            for visible_id in longitudinal_required
        ],
        "longitudinal_unavailable_pair_ids": longitudinal_unavailable,
        "literary_primary_planned_pair_count": 48,
        "literary_primary_scoreable_pair_count": literary_primary_scoreable,
        "literary_bridge_planned_pair_count": 3,
        "literary_bridge_scoreable_pair_count": literary_bridge_scoreable,
        "longitudinal_planned_pair_count": LONGITUDINAL_PAIR_COUNT,
        "longitudinal_scoreable_pair_count": len(longitudinal_required),
        "unavailable_slots_are_not_quality_penalties": True,
        "identity_revealed": False,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
    }
    frozen["score_freeze_id"] = _sha256_json(frozen)
    return frozen

def validate_score_freeze(
    public_bundle: Mapping[str, Any],
    score_freeze: Mapping[str, Any],
) -> None:
    validate_public_blind_bundle(public_bundle)
    if not isinstance(score_freeze, Mapping):
        raise G10BlindProtocolError("score freeze must be an object")
    if score_freeze.get("schema_version") != SCORE_FREEZE_SCHEMA_VERSION:
        raise G10BlindProtocolError("score freeze schema changed")
    if score_freeze.get("revision") != SCORE_FREEZE_REVISION:
        raise G10BlindProtocolError("score freeze revision changed")
    if score_freeze.get("benchmark_id") != G10_BENCHMARK_ID:
        raise G10BlindProtocolError("score freeze benchmark changed")
    if score_freeze.get("public_bundle_sha256") != _sha256_json(
        copy.deepcopy(dict(public_bundle))
    ):
        raise G10BlindProtocolError("score freeze binds another public bundle")
    if (
        score_freeze.get("private_identity_key_commitment_sha256")
        != public_bundle.get("private_identity_key_commitment_sha256")
    ):
        raise G10BlindProtocolError("score freeze private-key commitment changed")
    if score_freeze.get("identity_revealed") is not False:
        raise G10BlindProtocolError("score freeze was not anonymous")
    if score_freeze.get("model_ranking_or_elimination_performed") is not False:
        raise G10BlindProtocolError("score freeze already ranks/eliminates models")
    if score_freeze.get("provider_calls") != 0:
        raise G10BlindProtocolError("score freeze unexpectedly reports provider calls")
    expected_literary_available = [
        row["visible_pair_id"]
        for row in public_bundle["literary_pairs"]
        if row["score_record_required"]
    ]
    expected_literary_unavailable = [
        row["visible_pair_id"]
        for row in public_bundle["literary_pairs"]
        if not row["score_record_required"]
    ]
    if [row.get("visible_pair_id") for row in score_freeze.get("literary_rows", ())] != expected_literary_available:
        raise G10BlindProtocolError("score freeze literary scoreable roster changed")
    if score_freeze.get("literary_unavailable_pair_ids") != expected_literary_unavailable:
        raise G10BlindProtocolError("score freeze literary unavailable roster changed")

    expected_long_available = [
        row["visible_track_pair_id"]
        for row in public_bundle["longitudinal_pairs"]
        if row["score_record_required"]
    ]
    expected_long_unavailable = [
        row["visible_track_pair_id"]
        for row in public_bundle["longitudinal_pairs"]
        if not row["score_record_required"]
    ]
    if [row.get("visible_track_pair_id") for row in score_freeze.get("longitudinal_rows", ())] != expected_long_available:
        raise G10BlindProtocolError("score freeze longitudinal scoreable roster changed")
    if score_freeze.get("longitudinal_unavailable_pair_ids") != expected_long_unavailable:
        raise G10BlindProtocolError("score freeze longitudinal unavailable roster changed")
    if score_freeze.get("unavailable_slots_are_not_quality_penalties") is not True:
        raise G10BlindProtocolError("score freeze unavailable-slot policy changed")
    stored = score_freeze.get("score_freeze_id")
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G10BlindProtocolError("score freeze ID is invalid")
    check = copy.deepcopy(dict(score_freeze))
    check.pop("score_freeze_id", None)
    if _sha256_json(check) != stored:
        raise G10BlindProtocolError("score freeze ID does not recompute")


def reveal_frozen_scores(
    *,
    public_bundle: Mapping[str, Any],
    score_freeze: Mapping[str, Any],
    private_key: Mapping[str, Any],
) -> dict[str, Any]:
    """Reveal identities only after complete anonymous score freeze verification."""
    validate_public_blind_bundle(public_bundle)
    validate_score_freeze(public_bundle, score_freeze)
    _validate_private_key(private_key)
    commitment = private_identity_key_commitment(private_key)
    if commitment != public_bundle["private_identity_key_commitment_sha256"]:
        raise G10BlindProtocolError("private identity key commitment mismatch")

    literary_map = _private_literary_map(private_key)
    long_map = _private_longitudinal_map(private_key)

    literary_revealed = []
    for row in score_freeze["literary_rows"]:
        pair_id = row["visible_pair_id"]
        mapping = literary_map[pair_id]
        literary_revealed.append(
            {
                "pair_id": pair_id,
                "case_kind": mapping["case_kind"],
                "novel_id": mapping["novel_id"],
                "chapter": mapping["chapter"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "side_a": copy.deepcopy(row["side_a"]),
                "side_b": copy.deepcopy(row["side_b"]),
                "preference": row["preference"],
            }
        )

    longitudinal_public = {
        row["visible_track_pair_id"]: row
        for row in public_bundle["longitudinal_pairs"]
    }
    longitudinal_revealed = []
    for row in score_freeze["longitudinal_rows"]:
        visible_id = row["visible_track_pair_id"]
        public_row = longitudinal_public[visible_id]
        mapping = long_map[public_row["novel_id"]]
        longitudinal_revealed.append(
            {
                "visible_track_pair_id": visible_id,
                "novel_id": public_row["novel_id"],
                "checkpoint": public_row["checkpoint"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "side_a": copy.deepcopy(row["side_a"]),
                "side_b": copy.deepcopy(row["side_b"]),
                "preference": row["preference"],
            }
        )

    literary_public = {
        row["visible_pair_id"]: row
        for row in public_bundle["literary_pairs"]
    }
    literary_unavailable_revealed = []
    for pair_id in score_freeze["literary_unavailable_pair_ids"]:
        mapping = literary_map[pair_id]
        public_row = literary_public[pair_id]
        literary_unavailable_revealed.append(
            {
                "pair_id": pair_id,
                "case_kind": mapping["case_kind"],
                "novel_id": mapping["novel_id"],
                "chapter": mapping["chapter"],
                "availability": public_row["availability"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "quality_score": None,
            }
        )

    longitudinal_unavailable_revealed = []
    for visible_id in score_freeze["longitudinal_unavailable_pair_ids"]:
        public_row = longitudinal_public[visible_id]
        mapping = long_map[public_row["novel_id"]]
        longitudinal_unavailable_revealed.append(
            {
                "visible_track_pair_id": visible_id,
                "novel_id": public_row["novel_id"],
                "checkpoint": public_row["checkpoint"],
                "availability": public_row["availability"],
                "side_a_model_id": mapping["side_a_model_id"],
                "side_b_model_id": mapping["side_b_model_id"],
                "quality_score": None,
            }
        )

    revealed = {
        "schema_version": REVEAL_SCHEMA_VERSION,
        "revision": REVEAL_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "public_bundle_sha256": _sha256_json(copy.deepcopy(dict(public_bundle))),
        "score_freeze_id": score_freeze["score_freeze_id"],
        "private_identity_key_commitment_sha256": commitment,
        "literary_rows": literary_revealed,
        "literary_unavailable_rows": literary_unavailable_revealed,
        "longitudinal_rows": longitudinal_revealed,
        "longitudinal_unavailable_rows": longitudinal_unavailable_revealed,
        "unavailable_slots_are_not_quality_penalties": True,
        "identity_revealed": True,
        "model_ranking_or_elimination_performed": False,
        "provider_calls": 0,
    }
    revealed["reveal_id"] = _sha256_json(revealed)
    return revealed


def g10c_contract_identity() -> dict[str, Any]:
    if BLIND_EVALUATION_POLICY["literary_scores_frozen_before_identity_reveal"] is not True:
        raise G10BlindProtocolError("G10 blind literary freeze policy drifted")
    if BLIND_EVALUATION_POLICY["longitudinal_scores_frozen_before_identity_reveal"] is not True:
        raise G10BlindProtocolError("G10 blind longitudinal freeze policy drifted")
    return {
        "schema_version": G10C_SCHEMA_VERSION,
        "revision": G10C_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessors": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a2_contract_sha256": G10A2_CONTRACT_SHA256,
            "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        },
        "literary": {
            "pair_count": LITERARY_PAIR_COUNT,
            "primary_pair_count": LITERARY_PRIMARY_PAIR_COUNT,
            "bridge_pair_count": LITERARY_BRIDGE_PAIR_COUNT,
            "primary_orientation_exactly_balanced_24_24": True,
            "bridge_cases_descriptive_only": True,
            "rubric": copy.deepcopy(LITERARY_RUBRIC),
        },
        "longitudinal": {
            "pair_count": LONGITUDINAL_PAIR_COUNT,
            "checkpoints": LONGITUDINAL_CHECKPOINTS,
            "orientation_stable_within_novel_across_checkpoints": True,
            "one_novel_qwen_a_one_novel_glm_a": True,
            "rubric": copy.deepcopy(LONGITUDINAL_STATE_RUBRIC),
        },
        "availability_policy": {
            "all_planned_slots_preserved": True,
            "only_fully_comparable_pairs_are_scoreable": True,
            "unavailable_slots_receive_no_quality_score": True,
            "missingness_is_reliability_not_quality": True,
            "failure_identity_revealed_only_after_score_freeze": True,
        },
        "blindness": {
            "private_identity_key_commitment_required": True,
            "model_metadata_removed": True,
            "effort_metadata_removed": True,
            "cost_transport_request_journal_metadata_removed": True,
            "scores_freeze_before_reveal": True,
            "reveal_requires_exact_private_key_commitment": True,
        },
        "statistics_policy": copy.deepcopy(STATISTICS_POLICY),
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


G10C_CONTRACT_SHA256 = _sha256_json(g10c_contract_identity())


def assert_paid_execution_blocked() -> None:
    identity = g10c_contract_identity()
    if identity["provider_calls"] != 0:
        raise G10BlindProtocolError("G10C unexpectedly reports Provider calls")
    if identity["paid_execution_authorized"] is not False:
        raise G10BlindProtocolError("G10C unexpectedly authorizes paid execution")


__all__ = [
    "G10C_CONTRACT_SHA256",
    "G10C_REVISION",
    "G10BlindProtocolError",
    "LITERARY_AVAILABLE",
    "LITERARY_UNAVAILABLE",
    "LITERARY_DIMENSIONS",
    "LITERARY_DEFECT_FLAGS",
    "LONGITUDINAL_AVAILABLE",
    "LONGITUDINAL_UNAVAILABLE",
    "LONGITUDINAL_CHECKPOINTS",
    "LONGITUDINAL_DIMENSIONS",
    "assert_paid_execution_blocked",
    "build_private_identity_key",
    "build_public_blind_bundle",
    "freeze_anonymous_scores",
    "g10c_contract_identity",
    "private_identity_key_commitment",
    "public_bundle_sha256",
    "reveal_frozen_scores",
    "validate_public_blind_bundle",
    "validate_score_freeze",
]
