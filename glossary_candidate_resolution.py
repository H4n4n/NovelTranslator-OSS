"""Explicit, auditable resolution of persisted glossary candidate conflicts.

This module is import-safe and performs no API work.  A resolution selects one
already-present semantic candidate by deterministic fingerprint; it never
invents or ranks candidate meanings.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from glossary_integrity import (
    GlossaryCandidateIdentityError,
    SOURCE_PROVENANCE_FIELD,
    canonicalize_glossary_review_candidates,
    glossary_candidate_conflict_fingerprint,
    glossary_candidate_semantic_fingerprint,
    glossary_candidate_semantic_identity,
    normalized_glossary_source_form,
    source_backed_candidate_proposals,
    stable_glossary_candidate_id,
)
from noveltranslator_core import atomic_write_json
from translation_profile import filter_profile_candidates


RESOLUTION_SCHEMA_VERSION = "glossary-candidate-resolution-v1"
AUTOMATIC_RESOLUTION_SCHEMA_VERSION = "glossary-candidate-resolution-v2"
AUTOMATIC_RESOLUTION_POLICY_VERSION = "candidate-conflict-auto-v1"
RESOLUTION_DIRECTORY_NAME = "glossary_candidate_resolutions"
MAX_RESOLUTION_RATIONALE_CHARS = 2000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NOVEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_RECORD_FIELDS = frozenset({
    "schema_version",
    "novel_id",
    "chapter",
    "pending_content_identity",
    "state_extraction_identity",
    "created_utc",
    "updated_utc",
    "resolutions",
    "record_sha256",
})
_ENTRY_FIELDS = frozenset({
    "conflict_type",
    "conflict_fingerprint",
    "source_concept_id",
    "source_key",
    "conflicting_semantic_fingerprints",
    "selected_semantic_fingerprint",
    "approved_resolution",
    "conflicting_proposals",
    "rationale",
    "approved_at_utc",
})
_AUTOMATIC_ENTRY_FIELDS = _ENTRY_FIELDS | frozenset({
    "resolution_method",
    "resolution_policy",
    "deterministic_basis",
    "adjudication",
})
_DETERMINISTIC_BASIS_FIELDS = frozenset({"proof", "details"})
_ADJUDICATION_FIELDS = frozenset({
    "operation_id",
    "request_sha256",
    "validated_result_sha256",
    "conflict_id",
    "selected_proposal_id",
    "stats",
})
_PROPOSAL_FIELDS = frozenset({
    "semantic_identity_fingerprint",
    "english",
    "category",
    "status",
    "notes",
    "provenance_identity",
    "source_occurrence_evidence",
    "context_identity",
})


class GlossaryCandidateResolutionError(ValueError):
    """An explicit resolution is malformed, stale, or cannot be applied."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = dict(diagnostics or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def pending_content_identity(translation: str) -> str:
    return hashlib.sha256(str(translation or "").encode("utf-8")).hexdigest()


def state_extraction_identity(state: Mapping[str, Any]) -> str:
    """Hash persisted state while treating candidate presentation order as inert."""
    if not isinstance(state, Mapping):
        raise GlossaryCandidateResolutionError(
            "persisted state extraction must be an object",
            code="resolution_state_identity_invalid",
        )
    normalized = copy.deepcopy(dict(state))
    new_terms = normalized.get("new_terms", [])
    if not isinstance(new_terms, list):
        raise GlossaryCandidateResolutionError(
            "persisted state new_terms must be a list",
            code="resolution_state_identity_invalid",
        )
    normalized["new_terms"] = sorted(
        (copy.deepcopy(item) for item in new_terms),
        key=_canonical_json,
    )
    return _sha256_json(normalized)


def candidate_resolution_directory(data_dir: Path | str) -> Path:
    return Path(data_dir) / RESOLUTION_DIRECTORY_NAME


def candidate_resolution_path(data_dir: Path | str, chapter: int) -> Path:
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise ValueError("chapter must be a positive integer")
    return candidate_resolution_directory(data_dir) / f"{chapter:04d}.json"


def candidate_resolution_record_sha256(record: Mapping[str, Any]) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    return _sha256_json(body)


def _utc_timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _schema_error(message: str, **diagnostics: Any) -> None:
    raise GlossaryCandidateResolutionError(
        message,
        code="resolution_schema_malformed",
        diagnostics=diagnostics,
    )


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        _schema_error(f"resolution {field} must be a lowercase SHA-256", field=field)
    return value


def _proposal_snapshot(proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "semantic_identity_fingerprint": str(
            proposal.get("semantic_identity_fingerprint", "") or ""
        ).strip(),
        "english": str(proposal.get("english", "") or "").strip(),
        "category": str(proposal.get("category", "") or "").strip(),
        "status": str(proposal.get("status", "") or "").strip(),
        "notes": str(proposal.get("notes", "") or ""),
        "provenance_identity": copy.deepcopy(
            proposal.get("provenance_identity", {})
        ),
        "source_occurrence_evidence": copy.deepcopy(
            proposal.get("source_occurrence_evidence", {})
        ),
        "context_identity": copy.deepcopy(proposal.get("context_identity", {})),
    }


def _proposal_semantic_candidate(proposal: Mapping[str, Any]) -> dict[str, Any]:
    candidate = {
        "english": proposal["english"],
        "category": proposal["category"],
        "status": proposal["status"],
        SOURCE_PROVENANCE_FIELD: copy.deepcopy(proposal["provenance_identity"]),
    }
    candidate.update(copy.deepcopy(proposal["context_identity"]))
    return candidate


def _approved_identity_from_candidate(candidate: Mapping[str, Any]) -> dict[str, str]:
    semantic = glossary_candidate_semantic_identity(candidate)
    return {
        "english": str(semantic.get("english", "") or "").strip(),
        "category": str(semantic.get("category", "") or "").strip(),
        "status": str(semantic.get("status", "") or "").strip(),
    }


def _normalized_approved_identity(value: Mapping[str, Any]) -> dict[str, str]:
    return _approved_identity_from_candidate(value)


def build_candidate_resolution_entry(
    conflict: Mapping[str, Any],
    selected_semantic_fingerprint: str,
    rationale: str,
    *,
    approved_at_utc: str | None = None,
) -> dict[str, Any]:
    if not isinstance(rationale, str) or not rationale.strip():
        raise GlossaryCandidateResolutionError(
            "resolution rationale must be non-empty",
            code="resolution_rationale_invalid",
        )
    rationale = rationale.strip()
    if len(rationale) > MAX_RESOLUTION_RATIONALE_CHARS:
        raise GlossaryCandidateResolutionError(
            "resolution rationale exceeds the bounded length",
            code="resolution_rationale_invalid",
        )
    selected_semantic_fingerprint = str(
        selected_semantic_fingerprint or ""
    ).strip()
    _require_hash(selected_semantic_fingerprint, "selected_semantic_fingerprint")
    proposals = [_proposal_snapshot(item) for item in conflict.get("proposals", [])]
    proposals.sort(
        key=lambda item: (
            item["semantic_identity_fingerprint"],
            _canonical_json(item),
        )
    )
    selected = [
        item for item in proposals
        if item["semantic_identity_fingerprint"] == selected_semantic_fingerprint
    ]
    if not selected:
        raise GlossaryCandidateResolutionError(
            "selected semantic fingerprint is not present in the conflict",
            code="resolution_selected_fingerprint_absent",
        )
    selected_proposal = selected[0]
    return {
        "conflict_type": str(conflict.get("code", "") or "").strip(),
        "conflict_fingerprint": glossary_candidate_conflict_fingerprint(conflict),
        "source_concept_id": str(conflict.get("candidate_id", "") or "").strip(),
        "source_key": str(conflict.get("source_term", "") or "").strip(),
        "conflicting_semantic_fingerprints": sorted({
            item["semantic_identity_fingerprint"] for item in proposals
        }),
        "selected_semantic_fingerprint": selected_semantic_fingerprint,
        "approved_resolution": {
            "english": selected_proposal["english"],
            "category": selected_proposal["category"],
            "status": selected_proposal["status"],
        },
        "conflicting_proposals": proposals,
        "rationale": rationale,
        "approved_at_utc": _utc_timestamp(approved_at_utc),
    }


def _validate_proposal_snapshot(proposal: Any) -> dict[str, Any]:
    if not isinstance(proposal, Mapping):
        _schema_error("resolution conflicting proposal must be an object")
    unknown = set(proposal) - _PROPOSAL_FIELDS
    missing = _PROPOSAL_FIELDS - set(proposal)
    if unknown or missing:
        _schema_error(
            "resolution conflicting proposal has invalid fields",
            unknown_fields=sorted(unknown),
            missing_fields=sorted(missing),
        )
    result = dict(proposal)
    fingerprint = _require_hash(
        result["semantic_identity_fingerprint"],
        "semantic_identity_fingerprint",
    )
    for field in ("english", "category", "status"):
        if not isinstance(result[field], str) or not result[field].strip():
            _schema_error(f"resolution proposal {field} must be non-empty")
        result[field] = result[field].strip()
    if not isinstance(result["notes"], str):
        _schema_error("resolution proposal notes must be a string")
    for field in (
        "provenance_identity",
        "source_occurrence_evidence",
        "context_identity",
    ):
        if not isinstance(result[field], Mapping):
            _schema_error(f"resolution proposal {field} must be an object")
        result[field] = dict(result[field])
    occurrence_evidence = result["source_occurrence_evidence"]
    if set(occurrence_evidence) - {"span_start", "span_end"}:
        _schema_error("resolution source occurrence evidence has invalid fields")
    for field, value in occurrence_evidence.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _schema_error(f"resolution {field} must be a non-negative integer")
    if (
        "span_start" in occurrence_evidence
        and "span_end" in occurrence_evidence
        and occurrence_evidence["span_end"] < occurrence_evidence["span_start"]
    ):
        _schema_error("resolution source occurrence span is inverted")
    recomputed = glossary_candidate_semantic_fingerprint(
        _proposal_semantic_candidate(result)
    )
    if recomputed != fingerprint:
        _schema_error(
            "resolution proposal semantic fingerprint does not match its identity",
            expected=fingerprint,
            actual=recomputed,
        )
    return result


def _validate_resolution_entry(
    entry: Any,
    *,
    schema_version: str = RESOLUTION_SCHEMA_VERSION,
) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        _schema_error("resolution entry must be an object")
    entry_fields = (
        _AUTOMATIC_ENTRY_FIELDS
        if schema_version == AUTOMATIC_RESOLUTION_SCHEMA_VERSION
        else _ENTRY_FIELDS
    )
    unknown = set(entry) - entry_fields
    missing = entry_fields - set(entry)
    if unknown or missing:
        _schema_error(
            "resolution entry has invalid fields",
            unknown_fields=sorted(unknown),
            missing_fields=sorted(missing),
        )
    result = copy.deepcopy(dict(entry))
    for field in (
        "conflict_type",
        "source_concept_id",
        "source_key",
        "rationale",
        "approved_at_utc",
    ):
        if not isinstance(result[field], str) or not result[field].strip():
            _schema_error(f"resolution entry {field} must be non-empty")
        result[field] = result[field].strip()
    if result["conflict_type"] != "same_source_semantic_conflict":
        _schema_error("resolution conflict type is unsupported")
    if len(result["rationale"]) > MAX_RESOLUTION_RATIONALE_CHARS:
        _schema_error("resolution rationale exceeds the bounded length")
    try:
        _utc_timestamp(result["approved_at_utc"])
    except ValueError as exc:
        _schema_error(str(exc), field="approved_at_utc")
    conflict_fingerprint = _require_hash(
        result["conflict_fingerprint"], "conflict_fingerprint"
    )
    selected_fingerprint = _require_hash(
        result["selected_semantic_fingerprint"],
        "selected_semantic_fingerprint",
    )
    fingerprints = result["conflicting_semantic_fingerprints"]
    if (
        not isinstance(fingerprints, list)
        or len(fingerprints) < 2
        or fingerprints != sorted(set(fingerprints))
    ):
        _schema_error(
            "resolution conflicting semantic fingerprints must be a sorted unique list"
        )
    for fingerprint in fingerprints:
        _require_hash(fingerprint, "conflicting_semantic_fingerprints")
    if selected_fingerprint not in fingerprints:
        _schema_error("selected semantic fingerprint is outside the conflict")
    if result["source_concept_id"] != stable_glossary_candidate_id(
        result["source_key"]
    ):
        _schema_error("resolution source concept ID does not match its source key")
    approved = result["approved_resolution"]
    if not isinstance(approved, Mapping) or set(approved) != {
        "english", "category", "status"
    }:
        _schema_error("resolution approved_resolution has invalid fields")
    approved = _normalized_approved_identity(approved)
    if not all(approved.values()):
        _schema_error("resolution approved identity fields must be non-empty")
    result["approved_resolution"] = approved
    proposals = result["conflicting_proposals"]
    if not isinstance(proposals, list) or len(proposals) < 2:
        _schema_error("resolution must retain at least two conflicting proposals")
    proposals = [_validate_proposal_snapshot(item) for item in proposals]
    proposal_fingerprints = sorted({
        item["semantic_identity_fingerprint"] for item in proposals
    })
    if proposal_fingerprints != fingerprints:
        _schema_error("resolution proposal set does not match conflict fingerprints")
    selected = [
        item for item in proposals
        if item["semantic_identity_fingerprint"] == selected_fingerprint
    ]
    if not selected:
        _schema_error("selected semantic fingerprint has no retained proposal")
    if not any(
        _normalized_approved_identity(item) == approved for item in selected
    ):
        _schema_error("approved resolution does not match the selected proposal")
    result["conflicting_proposals"] = proposals
    synthetic_conflict = {
        "code": result["conflict_type"],
        "candidate_id": result["source_concept_id"],
        "source_term": result["source_key"],
        "normalized_source_form": normalized_glossary_source_form(
            result["source_key"]
        ),
        "proposals": proposals,
    }
    recomputed_conflict = glossary_candidate_conflict_fingerprint(
        synthetic_conflict
    )
    if recomputed_conflict != conflict_fingerprint:
        _schema_error(
            "resolution conflict fingerprint does not match its proposal set",
            expected=conflict_fingerprint,
            actual=recomputed_conflict,
        )
    if schema_version == AUTOMATIC_RESOLUTION_SCHEMA_VERSION:
        method = result["resolution_method"]
        if method not in {"deterministic", "model_adjudicated"}:
            _schema_error("automatic resolution method is unsupported")
        if result["resolution_policy"] != AUTOMATIC_RESOLUTION_POLICY_VERSION:
            _schema_error("automatic resolution policy is unsupported")
        basis = result["deterministic_basis"]
        if not isinstance(basis, list):
            _schema_error("automatic deterministic basis must be a list")
        normalized_basis = []
        for item in basis:
            if not isinstance(item, Mapping) or set(item) != _DETERMINISTIC_BASIS_FIELDS:
                _schema_error("automatic deterministic basis has invalid fields")
            proof = item.get("proof")
            details = item.get("details")
            if not isinstance(proof, str) or not proof.strip():
                _schema_error("automatic deterministic proof must be non-empty")
            if not isinstance(details, Mapping):
                _schema_error("automatic deterministic proof details must be an object")
            normalized_basis.append({"proof": proof.strip(), "details": dict(details)})
        result["deterministic_basis"] = normalized_basis
        adjudication = result["adjudication"]
        if not isinstance(adjudication, Mapping):
            _schema_error("automatic adjudication provenance must be an object")
        adjudication = dict(adjudication)
        if method == "deterministic":
            if not normalized_basis or adjudication:
                _schema_error(
                    "deterministic resolution requires proof basis and no adjudication"
                )
        else:
            if normalized_basis or set(adjudication) != _ADJUDICATION_FIELDS:
                _schema_error(
                    "model-adjudicated resolution has invalid provenance fields"
                )
            for field in ("operation_id", "conflict_id", "selected_proposal_id"):
                value = adjudication[field]
                if not isinstance(value, str) or not value.strip():
                    _schema_error(f"automatic adjudication {field} must be non-empty")
                adjudication[field] = value.strip()
            _require_hash(adjudication["request_sha256"], "request_sha256")
            _require_hash(
                adjudication["validated_result_sha256"],
                "validated_result_sha256",
            )
            if not isinstance(adjudication["stats"], Mapping):
                _schema_error("automatic adjudication stats must be an object")
            adjudication["stats"] = copy.deepcopy(dict(adjudication["stats"]))
        result["adjudication"] = adjudication
    return result


def validate_candidate_resolution_record(
    record: Any,
    *,
    novel_id: str | None = None,
    chapter: int | None = None,
    expected_pending_identity: str | None = None,
    expected_state_identity: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        _schema_error("candidate resolution record must be an object")
    unknown = set(record) - _RECORD_FIELDS
    missing = _RECORD_FIELDS - set(record)
    if unknown or missing:
        _schema_error(
            "candidate resolution record has invalid fields",
            unknown_fields=sorted(unknown),
            missing_fields=sorted(missing),
        )
    result = copy.deepcopy(dict(record))
    if result["schema_version"] not in {
        RESOLUTION_SCHEMA_VERSION,
        AUTOMATIC_RESOLUTION_SCHEMA_VERSION,
    }:
        _schema_error("candidate resolution schema version is unsupported")
    if not isinstance(result["novel_id"], str) or not NOVEL_ID_RE.fullmatch(
        result["novel_id"]
    ):
        _schema_error("candidate resolution novel_id is invalid")
    if (
        not isinstance(result["chapter"], int)
        or isinstance(result["chapter"], bool)
        or result["chapter"] < 1
    ):
        _schema_error("candidate resolution chapter is invalid")
    _require_hash(result["pending_content_identity"], "pending_content_identity")
    _require_hash(result["state_extraction_identity"], "state_extraction_identity")
    for field in ("created_utc", "updated_utc"):
        try:
            _utc_timestamp(result[field])
        except ValueError as exc:
            _schema_error(str(exc), field=field)
    resolutions = result["resolutions"]
    if not isinstance(resolutions, list) or not resolutions:
        _schema_error("candidate resolution record must contain resolutions")
    resolutions = [
        _validate_resolution_entry(item, schema_version=result["schema_version"])
        for item in resolutions
    ]
    conflict_fingerprints = [
        item["conflict_fingerprint"] for item in resolutions
    ]
    if len(conflict_fingerprints) != len(set(conflict_fingerprints)):
        raise GlossaryCandidateResolutionError(
            "candidate resolution record contains duplicate conflict resolutions",
            code="duplicate_conflict_resolutions",
            diagnostics={"conflict_fingerprints": conflict_fingerprints},
        )
    result["resolutions"] = resolutions
    record_hash = _require_hash(result["record_sha256"], "record_sha256")
    recomputed_hash = candidate_resolution_record_sha256(result)
    if recomputed_hash != record_hash:
        raise GlossaryCandidateResolutionError(
            "candidate resolution record hash does not match its contents",
            code="resolution_record_hash_mismatch",
            diagnostics={"expected": record_hash, "actual": recomputed_hash},
        )
    expected_checks = (
        (novel_id, result["novel_id"], "resolution_novel_mismatch", "novel"),
        (chapter, result["chapter"], "resolution_chapter_mismatch", "chapter"),
        (
            expected_pending_identity,
            result["pending_content_identity"],
            "resolution_pending_identity_mismatch",
            "pending content",
        ),
        (
            expected_state_identity,
            result["state_extraction_identity"],
            "resolution_state_identity_mismatch",
            "state extraction",
        ),
    )
    for expected, actual, code, label in expected_checks:
        if expected is not None and expected != actual:
            raise GlossaryCandidateResolutionError(
                f"candidate resolution {label} identity is stale or mismatched",
                code=code,
                diagnostics={"expected": expected, "actual": actual},
            )
    return result


def load_candidate_resolution_record(
    path: Path | str,
    **expected: Any,
) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GlossaryCandidateResolutionError(
            "candidate resolution record cannot be read as JSON",
            code="resolution_schema_malformed",
            diagnostics={"path": str(path), "error": str(exc)},
        ) from exc
    return validate_candidate_resolution_record(raw, **expected)


def apply_candidate_conflict_resolutions(
    candidates: Sequence[Mapping[str, Any]],
    resolution_record: Mapping[str, Any] | None,
    *,
    novel_id: str,
    chapter: int,
    expected_pending_identity: str,
    expected_state_identity: str,
) -> dict[str, Any]:
    try:
        initial = canonicalize_glossary_review_candidates(candidates)
    except GlossaryCandidateIdentityError as exc:
        identity_error = exc
        conflicts = list(exc.diagnostics.get("conflicts", []))
    else:
        if resolution_record is not None:
            raise GlossaryCandidateResolutionError(
                "candidate resolution exists but no matching conflict remains",
                code="resolution_without_matching_conflict",
            )
        return initial

    if resolution_record is None:
        raise identity_error
    record = validate_candidate_resolution_record(
        resolution_record,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=expected_pending_identity,
        expected_state_identity=expected_state_identity,
    )
    current_by_fingerprint = {
        conflict["conflict_fingerprint"]: conflict for conflict in conflicts
    }
    resolution_by_fingerprint = {
        entry["conflict_fingerprint"]: entry for entry in record["resolutions"]
    }
    stale_fingerprints = sorted(
        set(resolution_by_fingerprint) - set(current_by_fingerprint)
    )
    if stale_fingerprints:
        raise GlossaryCandidateResolutionError(
            "candidate resolution does not match the current conflict set",
            code="resolution_conflict_fingerprint_mismatch",
            diagnostics={"stale_conflict_fingerprints": stale_fingerprints},
        )
    unresolved_fingerprints = sorted(
        set(current_by_fingerprint) - set(resolution_by_fingerprint)
    )
    if unresolved_fingerprints:
        raise identity_error

    selected_by_source: dict[str, str] = {}
    applied = []
    for fingerprint, conflict in current_by_fingerprint.items():
        entry = resolution_by_fingerprint[fingerprint]
        if (
            entry["source_concept_id"] != conflict["candidate_id"]
            or entry["source_key"] != conflict["source_term"]
        ):
            raise GlossaryCandidateResolutionError(
                "candidate resolution source concept no longer matches",
                code="resolution_source_concept_mismatch",
                diagnostics={"conflict_fingerprint": fingerprint},
            )
        current_semantic_fingerprints = sorted(
            conflict["conflicting_semantic_fingerprints"]
        )
        if current_semantic_fingerprints != entry[
            "conflicting_semantic_fingerprints"
        ]:
            raise GlossaryCandidateResolutionError(
                "candidate resolution proposal set no longer matches",
                code="resolution_conflict_fingerprint_mismatch",
                diagnostics={"conflict_fingerprint": fingerprint},
            )
        selected = entry["selected_semantic_fingerprint"]
        if selected not in current_semantic_fingerprints:
            raise GlossaryCandidateResolutionError(
                "selected semantic fingerprint is absent from the current conflict",
                code="resolution_selected_fingerprint_absent",
            )
        selected_by_source[entry["source_key"]] = selected
        applied.append({
            "conflict_fingerprint": fingerprint,
            "source_concept_id": entry["source_concept_id"],
            "source_key": entry["source_key"],
            "selected_semantic_fingerprint": selected,
            "approved_resolution": copy.deepcopy(entry["approved_resolution"]),
            "rationale": entry["rationale"],
            "resolution_method": entry.get(
                "resolution_method", "human_approved"
            ),
            "resolution_policy": entry.get("resolution_policy", ""),
            "deterministic_basis": copy.deepcopy(
                entry.get("deterministic_basis", [])
            ),
            "adjudication": copy.deepcopy(entry.get("adjudication", {})),
        })

    filtered = []
    selected_found: dict[str, int] = {source: 0 for source in selected_by_source}
    for raw in candidates:
        candidate = dict(raw)
        source_key = str(candidate.get("chinese", "") or "").strip()
        selected = selected_by_source.get(source_key)
        if selected is None:
            filtered.append(candidate)
            continue
        if glossary_candidate_semantic_fingerprint(candidate) == selected:
            filtered.append(candidate)
            selected_found[source_key] += 1
    absent = sorted(source for source, count in selected_found.items() if not count)
    if absent:
        raise GlossaryCandidateResolutionError(
            "selected semantic fingerprint is absent from current candidates",
            code="resolution_selected_fingerprint_absent",
            diagnostics={"source_keys": absent},
        )
    try:
        final = canonicalize_glossary_review_candidates(filtered)
    except GlossaryCandidateIdentityError as exc:
        raise GlossaryCandidateResolutionError(
            "resolved candidates still fail source identity validation",
            code="resolved_candidate_still_conflicts",
            diagnostics=exc.diagnostics,
        ) from exc
    final_by_source = {
        item["chinese"]: item for item in final["candidates"]
    }
    for item in applied:
        approved = item["approved_resolution"]
        selected_candidate = final_by_source.get(item["source_key"])
        if selected_candidate is None:
            raise GlossaryCandidateResolutionError(
                "resolved source candidate is absent after canonicalization",
                code="resolution_source_not_present",
            )
        if _approved_identity_from_candidate(selected_candidate) != approved:
            raise GlossaryCandidateResolutionError(
                "resolved candidate does not match the approved semantic identity",
                code="resolution_approved_identity_mismatch",
            )
    diagnostics = final["diagnostics"]
    diagnostics["raw_candidate_count"] = len(candidates)
    diagnostics["original_conflict_count"] = len(conflicts)
    diagnostics["resolved_conflict_count"] = len(applied)
    diagnostics["original_conflicts"] = copy.deepcopy(conflicts)
    diagnostics["applied_resolutions"] = applied
    diagnostics["resolution_record_sha256"] = record["record_sha256"]
    return final


def create_candidate_resolution(
    path: Path | str,
    *,
    novel_id: str,
    chapter: int,
    pending_identity: str,
    state_identity: str,
    conflict: Mapping[str, Any],
    selected_semantic_fingerprint: str,
    rationale: str,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    path = Path(path)
    timestamp = _utc_timestamp(timestamp_utc)
    entry = build_candidate_resolution_entry(
        conflict,
        selected_semantic_fingerprint,
        rationale,
        approved_at_utc=timestamp,
    )
    existing = load_candidate_resolution_record(
        path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    if existing is None:
        record = {
            "schema_version": RESOLUTION_SCHEMA_VERSION,
            "novel_id": novel_id,
            "chapter": chapter,
            "pending_content_identity": pending_identity,
            "state_extraction_identity": state_identity,
            "created_utc": timestamp,
            "updated_utc": timestamp,
            "resolutions": [entry],
        }
        status = "created"
    else:
        matches = [
            item for item in existing["resolutions"]
            if item["conflict_fingerprint"] == entry["conflict_fingerprint"]
        ]
        if matches:
            comparable_fields = _ENTRY_FIELDS - {"approved_at_utc"}
            existing_identity = {
                key: matches[0][key] for key in comparable_fields
            }
            requested_identity = {
                key: entry[key] for key in comparable_fields
            }
            if existing_identity != requested_identity:
                raise GlossaryCandidateResolutionError(
                    "an existing resolution selects different conflict semantics",
                    code="resolution_overwrite_blocked",
                    diagnostics={
                        "existing_selected_semantic_fingerprint": matches[0][
                            "selected_semantic_fingerprint"
                        ],
                        "requested_selected_semantic_fingerprint": entry[
                            "selected_semantic_fingerprint"
                        ],
                    },
                )
            return {"status": "unchanged", "path": path, "record": existing}
        record = copy.deepcopy(existing)
        record["resolutions"].append(entry)
        record["resolutions"].sort(key=lambda item: item["conflict_fingerprint"])
        record["updated_utc"] = timestamp
        record.pop("record_sha256", None)
        status = "updated"
    record["record_sha256"] = candidate_resolution_record_sha256(record)
    validate_candidate_resolution_record(
        record,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    verified = load_candidate_resolution_record(
        path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    if verified != record:
        raise GlossaryCandidateResolutionError(
            "candidate resolution verification did not reproduce the written record",
            code="resolution_atomic_verification_failed",
        )
    return {"status": status, "path": path, "record": verified}


def create_automatic_candidate_resolutions(
    path: Path | str,
    *,
    novel_id: str,
    chapter: int,
    pending_identity: str,
    state_identity: str,
    selections: Sequence[Mapping[str, Any]],
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Atomically persist exact existing selections in the strict v2 schema."""
    path = Path(path)
    if not isinstance(selections, Sequence) or isinstance(selections, (str, bytes)):
        raise GlossaryCandidateResolutionError(
            "automatic resolutions must be a non-empty sequence",
            code="automatic_resolution_invalid",
        )
    if not selections:
        raise GlossaryCandidateResolutionError(
            "automatic resolutions must not be empty",
            code="automatic_resolution_invalid",
        )
    timestamp = _utc_timestamp(timestamp_utc)
    entries = []
    for selection in selections:
        if not isinstance(selection, Mapping):
            raise GlossaryCandidateResolutionError(
                "automatic resolution selection must be an object",
                code="automatic_resolution_invalid",
            )
        method = str(selection.get("resolution_method", "") or "").strip()
        if method not in {"deterministic", "model_adjudicated"}:
            raise GlossaryCandidateResolutionError(
                "automatic resolution method is unsupported",
                code="automatic_resolution_invalid",
            )
        entry = build_candidate_resolution_entry(
            selection.get("conflict", {}),
            str(selection.get("selected_semantic_fingerprint", "") or ""),
            str(selection.get("rationale", "") or ""),
            approved_at_utc=timestamp,
        )
        entry.update({
            "resolution_method": method,
            "resolution_policy": AUTOMATIC_RESOLUTION_POLICY_VERSION,
            "deterministic_basis": copy.deepcopy(
                list(selection.get("deterministic_basis", []))
            ),
            "adjudication": copy.deepcopy(dict(selection.get("adjudication", {}))),
        })
        entries.append(
            _validate_resolution_entry(
                entry,
                schema_version=AUTOMATIC_RESOLUTION_SCHEMA_VERSION,
            )
        )
    entries.sort(key=lambda item: item["conflict_fingerprint"])
    fingerprints = [item["conflict_fingerprint"] for item in entries]
    if len(fingerprints) != len(set(fingerprints)):
        raise GlossaryCandidateResolutionError(
            "automatic resolution contains duplicate conflicts",
            code="duplicate_conflict_resolutions",
        )

    existing = load_candidate_resolution_record(
        path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    if existing is not None:
        if existing["schema_version"] == RESOLUTION_SCHEMA_VERSION:
            raise GlossaryCandidateResolutionError(
                "human-approved v1 resolution has precedence and cannot be changed",
                code="automatic_resolution_human_record_precedence",
            )
        comparable_fields = _AUTOMATIC_ENTRY_FIELDS - {"approved_at_utc"}
        existing_by_fingerprint = {
            item["conflict_fingerprint"]: item for item in existing["resolutions"]
        }
        requested_by_fingerprint = {
            item["conflict_fingerprint"]: item for item in entries
        }
        if set(existing_by_fingerprint) != set(requested_by_fingerprint):
            raise GlossaryCandidateResolutionError(
                "existing automatic resolution conflict set cannot be extended or replaced",
                code="resolution_overwrite_blocked",
            )
        for fingerprint, requested in requested_by_fingerprint.items():
            current = existing_by_fingerprint[fingerprint]
            if any(current[field] != requested[field] for field in comparable_fields):
                raise GlossaryCandidateResolutionError(
                    "existing automatic resolution semantics cannot be overwritten",
                    code="resolution_overwrite_blocked",
                )
        return {"status": "unchanged", "path": path, "record": existing}

    record = {
        "schema_version": AUTOMATIC_RESOLUTION_SCHEMA_VERSION,
        "novel_id": novel_id,
        "chapter": chapter,
        "pending_content_identity": pending_identity,
        "state_extraction_identity": state_identity,
        "created_utc": timestamp,
        "updated_utc": timestamp,
        "resolutions": entries,
    }
    record["record_sha256"] = candidate_resolution_record_sha256(record)
    validate_candidate_resolution_record(
        record,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    verified = load_candidate_resolution_record(
        path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    if verified != record:
        raise GlossaryCandidateResolutionError(
            "automatic resolution verification did not reproduce the written record",
            code="resolution_atomic_verification_failed",
        )
    return {"status": "created", "path": path, "record": verified}


def prepare_glossary_review_candidates(
    *,
    novel_id: str,
    chapter: int,
    state: Mapping[str, Any],
    source_text: str,
    translation: str,
    profile: Any,
    existing_terms: Mapping[str, Any] | Sequence[str],
    resolution_path: Path | str,
    low_value_terms: Sequence[str] = (),
) -> dict[str, Any]:
    new_terms = state.get("new_terms", []) if isinstance(state, Mapping) else []
    source_backed, source_rejected = source_backed_candidate_proposals(
        new_terms,
        source_text,
        chapter,
    )
    eligible, profile_rejected = filter_profile_candidates(
        profile,
        source_backed,
        existing_terms=existing_terms,
        low_value_terms=low_value_terms,
    )
    pending_identity = pending_content_identity(translation)
    state_identity = state_extraction_identity(state)
    record = load_candidate_resolution_record(
        resolution_path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    prepared = apply_candidate_conflict_resolutions(
        eligible,
        record,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    return {
        "candidates": prepared["candidates"],
        "rejected": source_rejected + profile_rejected,
        "diagnostics": prepared["diagnostics"],
        "pending_content_identity": pending_identity,
        "state_extraction_identity": state_identity,
        "resolution_record": record,
    }
