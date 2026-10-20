"""Deterministic request-local aliases for immutable model candidate IDs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MODEL_CANDIDATE_ID_TRANSPORT_VERSION = "short-request-ids-v1"


class ModelCandidateIdError(ValueError):
    """A model response violated exact request-local identity coverage."""

    def __init__(self, message: str, *, code: str, model_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.model_id = model_id


@dataclass(frozen=True)
class ModelCandidateIdMap:
    """One request's exact transport aliases and durable internal identities."""

    alias_to_internal: Mapping[str, str]
    internal_to_alias: Mapping[str, str]


def build_model_candidate_id_map(
    internal_candidate_ids: Sequence[str],
) -> ModelCandidateIdMap:
    """Map an ordered immutable-ID list to c01, c02, ... request aliases."""
    internal_ids = tuple(str(value or "").strip() for value in internal_candidate_ids)
    if any(not value for value in internal_ids):
        raise ValueError("internal candidate IDs must be non-empty strings")
    if len(internal_ids) != len(set(internal_ids)):
        raise ValueError("internal candidate IDs must be unique")
    aliases = tuple(f"c{index:02d}" for index in range(1, len(internal_ids) + 1))
    return ModelCandidateIdMap(
        alias_to_internal=dict(zip(aliases, internal_ids)),
        internal_to_alias=dict(zip(internal_ids, aliases)),
    )


def model_facing_candidates(
    candidates: Sequence[Mapping[str, Any]],
    id_map: ModelCandidateIdMap,
) -> list[dict[str, Any]]:
    """Replace durable IDs with request aliases in model-facing candidate copies."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate = dict(raw)
        internal_id = str(candidate.get("candidate_id", "") or "").strip()
        alias = id_map.internal_to_alias.get(internal_id)
        if alias is None:
            raise ValueError(
                f"candidate is outside the request-local ID map: {internal_id}"
            )
        if internal_id in seen:
            raise ValueError(f"duplicate model-facing candidate: {internal_id}")
        seen.add(internal_id)
        candidate["candidate_id"] = alias
        result.append(candidate)
    if seen != set(id_map.internal_to_alias):
        raise ValueError("model-facing candidates do not cover the request ID map")
    return result


def resolve_model_candidate_response(
    result: Mapping[str, Any],
    id_map: ModelCandidateIdMap,
) -> dict[str, Any]:
    """Resolve exact model aliases back to durable IDs with complete coverage."""
    if not isinstance(result, Mapping):
        raise ModelCandidateIdError(
            "model response root must be an object",
            code="invalid_model_response_structure",
        )
    decisions = result.get("decisions")
    if not isinstance(decisions, list):
        raise ModelCandidateIdError(
            "model response decisions must be an array",
            code="invalid_model_response_structure",
        )

    expected = set(id_map.alias_to_internal)
    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for raw in decisions:
        if not isinstance(raw, Mapping):
            raise ModelCandidateIdError(
                "every model decision must be an object",
                code="invalid_model_response_structure",
            )
        model_id = raw.get("candidate_id")
        if not isinstance(model_id, str) or not model_id:
            raise ModelCandidateIdError(
                "model decision omitted request-local candidate_id",
                code="missing_model_candidate_id",
            )
        internal_id = id_map.alias_to_internal.get(model_id)
        if internal_id is None:
            raise ModelCandidateIdError(
                f"model decision is outside this request: {model_id}",
                code="unknown_model_candidate_id",
                model_id=model_id,
            )
        if model_id in seen:
            raise ModelCandidateIdError(
                f"duplicate model decision for request-local ID: {model_id}",
                code="duplicate_model_candidate_id",
                model_id=model_id,
            )
        seen.add(model_id)
        decision = dict(raw)
        decision["candidate_id"] = internal_id
        resolved.append(decision)

    missing = expected - seen
    if missing:
        raise ModelCandidateIdError(
            "model response omitted request-local candidate IDs: "
            + ", ".join(sorted(missing)),
            code="omitted_model_candidate_ids",
        )
    output = dict(result)
    output["decisions"] = resolved
    return output
