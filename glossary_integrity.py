"""Import-safe source-identity safeguards for persistent glossary entries."""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


SOURCE_BACKED = "SOURCE_BACKED"
LEGACY_NORMALIZED_OR_MANUAL = "LEGACY_NORMALIZED_OR_MANUAL"
UNBACKED_PROVISIONAL = "UNBACKED_PROVISIONAL"
UNRESOLVED = "UNRESOLVED"

SOURCE_PROVENANCE_FIELD = "source_provenance"
SOURCE_CANDIDATE_ID_PREFIX = "source:"
GLOSSARY_CANDIDATE_PREPARATION_VERSION = "source-concept-v1"
GLOSSARY_CANDIDATE_CONFLICT_VERSION = "source-concept-conflict-v1"

VALID_REVIEW_ACTIONS = frozenset(
    {"accept", "correct", "reject", "contextual", "merge"}
)
MAX_REVIEW_NOTES_CHARS = 500


class GlossarySourceIdentityError(ValueError):
    """A model decision attempted to change immutable source identity."""


class GlossaryCandidateIdentityError(ValueError):
    """A glossary candidate batch cannot be mapped to unique source identities."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        diagnostics: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = dict(diagnostics)


class GlossaryReviewValidationError(ValueError):
    """A parsed chapter review failed bounded semantic validation."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        candidate_id: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.candidate_id = candidate_id
        self.diagnostics = dict(diagnostics or {})

    def as_dict(self) -> dict[str, Any]:
        result = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "candidate_id": self.candidate_id,
        }
        result.update(copy.deepcopy(self.diagnostics))
        return result


class GlossaryReviewSchemaError(ValueError):
    """A parsed chapter review has an explicitly classified shape failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        field: str = "",
        allowed_fields: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.field = field
        self.allowed_fields = tuple(allowed_fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "field": self.field,
            "allowed_fields": list(self.allowed_fields),
        }


def stable_glossary_candidate_id(source_key: str) -> str:
    """Return an opaque stable ID without exposing source text as mutable identity."""
    source_key = str(source_key or "").strip()
    if not source_key:
        raise ValueError("source key must be non-empty")
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return SOURCE_CANDIDATE_ID_PREFIX + digest


def normalized_glossary_source_form(source_key: str) -> str:
    """Return a diagnostic Unicode form without changing exact source identity."""
    return unicodedata.normalize("NFC", str(source_key or "").strip())


_PROVENANCE_OCCURRENCE_FIELDS = frozenset({"span_start", "span_end"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _candidate_semantic_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields that must agree before same-source candidates collapse."""
    fields: dict[str, Any] = {}
    for key, value in candidate.items():
        if key in {"chinese", "candidate_id", "notes", "candidate_evidence"}:
            continue
        if key in {"aliases", "source_variants"}:
            if isinstance(value, (list, tuple)) and all(
                isinstance(item, str) for item in value
            ):
                continue
            fields[key] = value
            continue
        if key == SOURCE_PROVENANCE_FIELD and isinstance(value, Mapping):
            fields[key] = {
                provenance_key: provenance_value
                for provenance_key, provenance_value in value.items()
                if provenance_key not in _PROVENANCE_OCCURRENCE_FIELDS
            }
        elif key == "english":
            fields[key] = unicodedata.normalize("NFC", str(value or "").strip())
        elif key == "category":
            fields[key] = (
                str(value or "").strip().lower().replace("_", " ").replace("-", " ")
            )
        elif key == "status":
            fields[key] = str(value or "").strip().lower()
        else:
            fields[key] = value
    return fields


def glossary_candidate_semantic_identity(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical semantic identity used for duplicate decisions."""
    if not isinstance(candidate, Mapping):
        raise ValueError("glossary candidate must be an object")
    return _candidate_semantic_fields(candidate)


def glossary_candidate_semantic_fingerprint(
    candidate: Mapping[str, Any],
) -> str:
    """Return one deterministic semantic fingerprint, independent of position."""
    return hashlib.sha256(
        _canonical_json(glossary_candidate_semantic_identity(candidate)).encode(
            "utf-8"
        )
    ).hexdigest()


def glossary_candidate_conflict_fingerprint(
    conflict: Mapping[str, Any],
) -> str:
    """Return the order-independent identity of one same-source conflict."""
    if not isinstance(conflict, Mapping):
        raise ValueError("glossary candidate conflict must be an object")
    proposals = conflict.get("proposals")
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        raise ValueError("glossary candidate conflict proposals must be a list")
    semantic_fingerprints = []
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise ValueError("glossary candidate conflict proposal must be an object")
        fingerprint = str(
            proposal.get("semantic_identity_fingerprint", "") or ""
        ).strip()
        if not fingerprint:
            raise ValueError(
                "glossary candidate conflict proposal lacks a semantic fingerprint"
            )
        semantic_fingerprints.append(fingerprint)
    identity = {
        "version": GLOSSARY_CANDIDATE_CONFLICT_VERSION,
        "conflict_type": str(conflict.get("code", "") or "").strip(),
        "source_concept_id": str(
            conflict.get("candidate_id", "") or ""
        ).strip(),
        "source_key": str(conflict.get("source_term", "") or "").strip(),
        "normalized_source_form": str(
            conflict.get("normalized_source_form", "") or ""
        ).strip(),
        "semantic_fingerprints": sorted(set(semantic_fingerprints)),
    }
    if (
        not identity["conflict_type"]
        or not identity["source_concept_id"]
        or not identity["source_key"]
        or len(identity["semantic_fingerprints"]) < 2
    ):
        raise ValueError("glossary candidate conflict identity is incomplete")
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _string_evidence(candidate: Mapping[str, Any], field: str) -> list[str]:
    raw = candidate.get(field)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(value, str) for value in raw
    ):
        return []
    return [value.strip() for value in raw if value.strip()]


def canonicalize_glossary_review_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collapse provable same-source duplicates and fail on semantic conflicts.

    Source identity remains the exact stripped source key. NFC is recorded only
    for diagnostics: canonically equivalent but byte-distinct source keys are
    not silently merged because glossary keys must retain exact literal source
    evidence.
    """
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    source_order: list[str] = []
    preliminary: list[dict[str, Any]] = []

    for position, raw_candidate in enumerate(candidates):
        if not isinstance(raw_candidate, Mapping):
            diagnostics = {
                "preparation_version": GLOSSARY_CANDIDATE_PREPARATION_VERSION,
                "raw_candidate_count": len(candidates),
                "canonical_candidate_count": 0,
                "exact_duplicates_removed": 0,
                "semantic_duplicates_removed": 0,
                "conflict_count": 1,
                "conflicts": [
                    {
                        "code": "invalid_candidate_shape",
                        "raw_positions": [position],
                    }
                ],
            }
            raise GlossaryCandidateIdentityError(
                "chapter glossary-review candidate must be an object",
                code="invalid_candidate_shape",
                diagnostics=diagnostics,
            )
        candidate = dict(raw_candidate)
        source_key = str(candidate.get("chinese", "") or "").strip()
        if not source_key:
            diagnostics = {
                "preparation_version": GLOSSARY_CANDIDATE_PREPARATION_VERSION,
                "raw_candidate_count": len(candidates),
                "canonical_candidate_count": 0,
                "exact_duplicates_removed": 0,
                "semantic_duplicates_removed": 0,
                "conflict_count": 1,
                "conflicts": [
                    {
                        "code": "missing_source_key",
                        "raw_positions": [position],
                    }
                ],
            }
            raise GlossaryCandidateIdentityError(
                "chapter glossary-review candidate lacks an authoritative source key",
                code="missing_source_key",
                diagnostics=diagnostics,
            )
        expected_id = stable_glossary_candidate_id(source_key)
        supplied_id = str(candidate.get("candidate_id", "") or "").strip()
        if supplied_id and supplied_id != expected_id:
            diagnostics = {
                "preparation_version": GLOSSARY_CANDIDATE_PREPARATION_VERSION,
                "raw_candidate_count": len(candidates),
                "canonical_candidate_count": 0,
                "exact_duplicates_removed": 0,
                "semantic_duplicates_removed": 0,
                "conflict_count": 1,
                "conflicts": [
                    {
                        "code": "candidate_id_source_mismatch",
                        "raw_positions": [position],
                        "source_term": source_key,
                        "candidate_id": supplied_id,
                        "expected_candidate_id": expected_id,
                    }
                ],
            }
            raise GlossaryCandidateIdentityError(
                "chapter glossary-review candidate ID does not match its source key",
                code="candidate_id_source_mismatch",
                diagnostics=diagnostics,
            )
        candidate["chinese"] = source_key
        candidate["candidate_id"] = expected_id
        if source_key not in grouped:
            grouped[source_key] = []
            source_order.append(source_key)
        grouped[source_key].append((position, candidate))
        preliminary.append(candidate)

    ids_to_sources: dict[str, set[str]] = {}
    for candidate in preliminary:
        ids_to_sources.setdefault(candidate["candidate_id"], set()).add(
            candidate["chinese"]
        )
    id_collisions = [
        {
            "code": "candidate_id_collision",
            "candidate_id": candidate_id,
            "source_terms": sorted(source_terms),
            "normalized_source_forms": sorted(
                {normalized_glossary_source_form(value) for value in source_terms}
            ),
        }
        for candidate_id, source_terms in ids_to_sources.items()
        if len(source_terms) > 1
    ]
    if id_collisions:
        diagnostics = {
            "preparation_version": GLOSSARY_CANDIDATE_PREPARATION_VERSION,
            "raw_candidate_count": len(candidates),
            "canonical_candidate_count": 0,
            "exact_duplicates_removed": 0,
            "semantic_duplicates_removed": 0,
            "conflict_count": len(id_collisions),
            "conflicts": id_collisions,
        }
        raise GlossaryCandidateIdentityError(
            "distinct glossary source keys produced the same candidate ID",
            code="candidate_id_collision",
            diagnostics=diagnostics,
        )

    canonical: list[dict[str, Any]] = []
    collapsed_groups: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    exact_removed = 0
    semantic_removed = 0

    for source_key in source_order:
        records = grouped[source_key]
        signatures: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        semantic_records: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for position, candidate in records:
            semantic_fields = _candidate_semantic_fields(candidate)
            semantic_records.append((position, candidate, semantic_fields))
            signature = hashlib.sha256(
                _canonical_json(semantic_fields).encode("utf-8")
            ).hexdigest()
            signatures.setdefault(signature, []).append((position, candidate))

        if len(signatures) > 1:
            semantic_keys = sorted({
                key
                for _, _, semantic_fields in semantic_records
                for key in semantic_fields
            })
            differing_fields = [
                key for key in semantic_keys
                if len({
                    (
                        key in semantic_fields,
                        _canonical_json(semantic_fields.get(key)),
                    )
                    for _, _, semantic_fields in semantic_records
                }) > 1
            ]
            conflict = {
                    "code": "same_source_semantic_conflict",
                    "candidate_id": stable_glossary_candidate_id(source_key),
                    "source_term": source_key,
                    "normalized_source_form": normalized_glossary_source_form(
                        source_key
                    ),
                    "raw_positions": [position for position, _ in records],
                    "differing_semantic_fields": differing_fields,
                    "proposals": [
                        {
                            "raw_position": position,
                            "semantic_identity_fingerprint": (
                                glossary_candidate_semantic_fingerprint(candidate)
                            ),
                            "english": candidate.get("english", ""),
                            "category": candidate.get("category", ""),
                            "status": candidate.get("status", ""),
                            "notes": str(candidate.get("notes", "") or "")[
                                :MAX_REVIEW_NOTES_CHARS
                            ],
                            "provenance_identity": dict(
                                semantic_fields.get(SOURCE_PROVENANCE_FIELD, {})
                            ) if isinstance(
                                semantic_fields.get(SOURCE_PROVENANCE_FIELD),
                                Mapping,
                            ) else {},
                            "source_occurrence_evidence": {
                                key: candidate.get(
                                    SOURCE_PROVENANCE_FIELD, {}
                                ).get(key)
                                for key in ("span_start", "span_end")
                                if isinstance(
                                    candidate.get(SOURCE_PROVENANCE_FIELD),
                                    Mapping,
                                )
                                and key in candidate[SOURCE_PROVENANCE_FIELD]
                            },
                            "context_identity": {
                                key: value
                                for key, value in semantic_fields.items()
                                if key
                                not in {
                                    "english",
                                    "category",
                                    "status",
                                    SOURCE_PROVENANCE_FIELD,
                                }
                            },
                        }
                        for position, candidate, semantic_fields in semantic_records
                    ],
                }
            conflict["conflicting_semantic_fingerprints"] = sorted({
                proposal["semantic_identity_fingerprint"]
                for proposal in conflict["proposals"]
            })
            conflict["conflict_fingerprint"] = (
                glossary_candidate_conflict_fingerprint(conflict)
            )
            conflicts.append(conflict)
            continue

        representative = dict(records[0][1])
        if len(records) > 1:
            seen_raw: set[str] = set()
            for _, candidate in records:
                raw_fingerprint = _canonical_json(candidate)
                if raw_fingerprint in seen_raw:
                    exact_removed += 1
                else:
                    if seen_raw:
                        semantic_removed += 1
                    seen_raw.add(raw_fingerprint)

            for evidence_field in ("aliases", "source_variants"):
                values: list[str] = []
                for _, candidate in records:
                    for value in _string_evidence(candidate, evidence_field):
                        if value not in values:
                            values.append(value)
                if values:
                    representative[evidence_field] = values

            evidence: list[dict[str, Any]] = []
            for position, candidate in records:
                item: dict[str, Any] = {"raw_position": position}
                notes = candidate.get("notes")
                if isinstance(notes, str) and notes.strip():
                    item["notes"] = notes.strip()
                provenance = candidate.get(SOURCE_PROVENANCE_FIELD)
                if isinstance(provenance, Mapping):
                    item[SOURCE_PROVENANCE_FIELD] = dict(provenance)
                if len(item) > 1:
                    evidence.append(item)
            if evidence:
                representative["candidate_evidence"] = evidence

            collapsed_groups.append(
                {
                    "candidate_id": representative["candidate_id"],
                    "source_term": source_key,
                    "normalized_source_form": normalized_glossary_source_form(
                        source_key
                    ),
                    "raw_positions": [position for position, _ in records],
                    "raw_count": len(records),
                    "removed_count": len(records) - 1,
                    "semantic_identity_fingerprint": next(iter(signatures)),
                }
            )
        canonical.append(representative)

    diagnostics = {
        "preparation_version": GLOSSARY_CANDIDATE_PREPARATION_VERSION,
        "raw_candidate_count": len(candidates),
        "canonical_candidate_count": len(canonical),
        "exact_duplicates_removed": exact_removed,
        "semantic_duplicates_removed": semantic_removed,
        "conflict_count": len(conflicts),
        "candidate_ids": [candidate["candidate_id"] for candidate in canonical],
        "candidates": [
            {
                "candidate_id": candidate["candidate_id"],
                "source_term": candidate["chinese"],
                "normalized_source_form": normalized_glossary_source_form(
                    candidate["chinese"]
                ),
                "identity_fingerprint": hashlib.sha256(
                    _canonical_json(
                        {
                            "source_term": candidate["chinese"],
                            "semantic_fields": _candidate_semantic_fields(candidate),
                        }
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for candidate in canonical
        ],
        "collapsed_groups": collapsed_groups,
        "conflicts": conflicts,
    }
    if conflicts:
        raise GlossaryCandidateIdentityError(
            "same source glossary key has conflicting candidate proposals",
            code="same_source_semantic_conflict",
            diagnostics=diagnostics,
        )
    if len(diagnostics["candidate_ids"]) != len(
        set(diagnostics["candidate_ids"])
    ):
        raise GlossaryCandidateIdentityError(
            "chapter glossary-review candidate IDs must be unique",
            code="duplicate_candidate_ids_after_canonicalization",
            diagnostics=diagnostics,
        )
    return {"candidates": canonical, "diagnostics": diagnostics}


def source_backed_candidate_proposals(
    proposals: Sequence[Mapping[str, Any]],
    authoritative_source: str,
    chapter_number: int,
    *,
    origin: str = "chapter_state_extraction",
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Attach immutable IDs/provenance only to literal source-backed proposals."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    source_text = authoritative_source or ""
    for raw in proposals:
        proposal = dict(raw)
        source_key = str(proposal.get("chinese", "") or "").strip()
        if not source_key:
            rejected.append(
                {"chinese": "", "reason": "missing authoritative source key"}
            )
            continue
        span_start = source_text.find(source_key)
        if span_start < 0:
            rejected.append(
                {
                    "chinese": source_key,
                    "reason": (
                        "source key has no exact literal evidence in the "
                        f"authoritative source for Chapter {chapter_number}"
                    ),
                }
            )
            continue
        candidate_id = stable_glossary_candidate_id(source_key)
        proposal["chinese"] = source_key
        proposal["candidate_id"] = candidate_id
        proposal[SOURCE_PROVENANCE_FIELD] = {
            "origin": origin,
            "chapter": int(chapter_number),
            "verification": "exact_literal",
            "candidate_id": candidate_id,
            "span_start": span_start,
            "span_end": span_start + len(source_key),
        }
        accepted.append(proposal)
    return accepted, rejected


def _explicit_legacy_or_manual_identity(
    entry: Mapping[str, Any], authoritative_source: str
) -> tuple[bool, str]:
    provenance = entry.get(SOURCE_PROVENANCE_FIELD)
    if not isinstance(provenance, Mapping):
        return False, ""
    origin = str(provenance.get("origin", "") or "").strip().lower()
    verification = str(provenance.get("verification", "") or "").strip().lower()
    if origin == "legacy_normalized":
        authoritative_key = str(
            provenance.get("authoritative_source_key", "") or ""
        ).strip()
        if (
            verification == "normalized_from_exact_source"
            and authoritative_key
            and authoritative_key in authoritative_source
        ):
            return True, authoritative_key
    if (
        origin == "manual"
        and verification == "explicit_manual"
        and provenance.get("source_absent_approved") is True
    ):
        return True, ""
    return False, ""


def classify_glossary_source_entries(
    entries: Mapping[str, Mapping[str, Any]],
    authoritative_source: str | Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Classify source-key integrity without mutating glossary state."""
    if isinstance(authoritative_source, str):
        corpus = authoritative_source
    else:
        corpus = "\n".join(str(value or "") for value in authoritative_source)
    result: dict[str, dict[str, Any]] = {}
    for source_key, raw_entry in entries.items():
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        status = str(entry.get("status", "") or "").strip().lower()
        if source_key and source_key in corpus:
            classification = SOURCE_BACKED
            reason = "exact glossary key occurs in authoritative source"
            authoritative_key = source_key
        else:
            explicit, authoritative_key = _explicit_legacy_or_manual_identity(
                entry, corpus
            )
            if explicit:
                classification = LEGACY_NORMALIZED_OR_MANUAL
                reason = "explicit reviewed legacy/manual provenance"
            elif status in {"provisional", "contextual"}:
                classification = UNBACKED_PROVISIONAL
                reason = "non-locked key lacks exact authoritative source evidence"
            else:
                classification = UNRESOLVED
                reason = "locked or historical key lacks verified source provenance"
                authoritative_key = ""
        result[source_key] = {
            "source_key": source_key,
            "classification": classification,
            "reason": reason,
            "status": status,
            "locked": status == "locked",
            "authoritative_source_key": authoritative_key,
        }
    return result


def partition_audit_candidates_by_source_integrity(
    candidates: Sequence[Mapping[str, Any]],
    classifications: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only exact source-backed entries in model-facing periodic audits."""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = dict(raw)
        source_key = str(candidate.get("chinese", "") or "").strip()
        diagnostic = classifications.get(source_key, {})
        classification = diagnostic.get("classification", UNRESOLVED)
        if classification == SOURCE_BACKED:
            candidate["candidate_id"] = stable_glossary_candidate_id(source_key)
            candidate["source_integrity"] = SOURCE_BACKED
            included.append(candidate)
        else:
            excluded.append(
                {
                    "chinese": source_key,
                    "classification": classification,
                    "status": diagnostic.get("status", ""),
                    "reason": diagnostic.get("reason", "unresolved source identity"),
                }
            )
    return included, excluded


def build_source_integrity_audit_plan(
    entries: Mapping[str, Mapping[str, Any]],
    chapters: Sequence[Mapping[str, Any]],
    start_chapter: int,
    end_chapter: int,
    *,
    max_contexts: int = 2,
    context_radius: int = 180,
) -> dict[str, Any]:
    """Build one read-only, source-verified periodic-audit candidate plan."""
    chapter_texts = [
        str(chapter.get("text", "") or "")
        for chapter in chapters
        if isinstance(chapter, Mapping)
    ]
    classifications = classify_glossary_source_entries(entries, chapter_texts)
    chapter_by_index = {
        int(chapter["production_index"]): chapter
        for chapter in chapters
        if isinstance(chapter, Mapping)
        and isinstance(chapter.get("production_index"), int)
    }

    raw_candidates: list[dict[str, Any]] = []
    for source_key, entry in sorted(entries.items()):
        first_chapter = entry.get("first_chapter", 0)
        if not isinstance(first_chapter, int) or not (
            start_chapter <= first_chapter <= end_chapter
        ):
            continue
        if str(entry.get("status", "") or "").strip().lower() == "locked":
            continue
        contexts: list[dict[str, Any]] = []
        for chapter_number in range(start_chapter, end_chapter + 1):
            text = str(
                chapter_by_index.get(chapter_number, {}).get("text", "") or ""
            )
            search_start = 0
            while len(contexts) < max_contexts:
                index = text.find(source_key, search_start)
                if index < 0:
                    break
                left = max(0, index - context_radius)
                right = min(
                    len(text), index + len(source_key) + context_radius
                )
                contexts.append(
                    {"chapter": chapter_number, "context": text[left:right]}
                )
                search_start = index + len(source_key)
            if len(contexts) >= max_contexts:
                break
        raw_candidates.append(
            {
                "chinese": source_key,
                "english": entry.get("english", ""),
                "approved_renderings": entry.get("approved_renderings", []),
                "category": entry.get("category", ""),
                "status": entry.get("status", "provisional"),
                "alias_of": entry.get("alias_of", ""),
                "notes": entry.get("notes", ""),
                "contexts": contexts,
            }
        )

    candidates, excluded = partition_audit_candidates_by_source_integrity(
        raw_candidates, classifications
    )
    classes = (
        SOURCE_BACKED,
        LEGACY_NORMALIZED_OR_MANUAL,
        UNBACKED_PROVISIONAL,
        UNRESOLVED,
    )
    counts = {
        classification: sum(
            item["classification"] == classification
            for item in classifications.values()
        )
        for classification in classes
    }
    return {
        "candidates": candidates,
        "excluded_window_candidates": excluded,
        "classifications": classifications,
        "counts": counts,
        "safe_glossary_keys": sorted(
            source_key
            for source_key, item in classifications.items()
            if item["classification"]
            in {SOURCE_BACKED, LEGACY_NORMALIZED_OR_MANUAL}
        ),
    }


def validate_glossary_review_schema(result: Any) -> Mapping[str, Any]:
    """Validate only the generic top-level chapter-review container shape."""
    if not isinstance(result, Mapping):
        raise GlossaryReviewSchemaError(
            "glossary review response root must be an object",
            code="invalid_response_root_shape",
        )
    decisions = result.get("decisions")
    issues = result.get("issues", [])
    if not isinstance(decisions, list):
        raise GlossaryReviewSchemaError(
            "glossary review decisions must be an array",
            code="invalid_decisions_shape",
            field="decisions",
        )
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise GlossaryReviewSchemaError(
            "glossary review issues must be an array of strings",
            code="invalid_issues_shape",
            retryable=True,
            field="issues",
            allowed_fields=("issues",),
        )
    return result


def normalize_glossary_review_issues_shape(
    result: Any,
    errors: Sequence[Mapping[str, Any]],
    valid_candidate_ids: Iterable[str],
) -> dict[str, Any] | None:
    """Return an issues-only deterministic correction, or fail closed with None.

    This normalization is intentionally narrower than the model schema repair:
    it accepts only a list made entirely of tiny issue objects and copies each
    issue string verbatim.  A valid string list needs no correction and also
    returns None.
    """
    if not isinstance(result, Mapping) or len(errors) != 1:
        return None
    error = errors[0]
    if not isinstance(error, Mapping):
        return None
    allowed_fields = error.get("allowed_fields")
    if (
        error.get("code") != "invalid_issues_shape"
        or error.get("retryable") is not True
        or error.get("field") != "issues"
        or allowed_fields not in (["issues"], ("issues",))
    ):
        return None

    issues = result.get("issues")
    if not isinstance(issues, list):
        return None
    if all(isinstance(item, str) for item in issues):
        return None

    known_ids = set(valid_candidate_ids)
    normalized_issues: list[str] = []
    allowed_keys = {"issue", "candidate_id"}
    for item in issues:
        if not isinstance(item, Mapping):
            return None
        if not set(item).issubset(allowed_keys) or "issue" not in item:
            return None
        issue = item.get("issue")
        if not isinstance(issue, str):
            return None
        if "candidate_id" in item:
            candidate_id = item.get("candidate_id")
            if not isinstance(candidate_id, str) or candidate_id not in known_ids:
                return None
        normalized_issues.append(issue)

    normalized = copy.deepcopy(dict(result))
    normalized["issues"] = normalized_issues
    validate_glossary_review_schema_correction_scope(result, normalized, errors)
    return normalized


def validate_glossary_review_result(
    result: Any,
    candidates: Sequence[Mapping[str, Any]],
    existing_source_keys: Iterable[str],
) -> dict[str, Any]:
    """Validate review metadata while reattaching immutable source keys locally."""
    validate_glossary_review_schema(result)
    decisions = result.get("decisions")
    issues = result.get("issues", [])

    candidate_by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        source_key = str(candidate.get("chinese", "") or "").strip()
        candidate_id = str(candidate.get("candidate_id", "") or "").strip()
        if not candidate_id:
            candidate_id = stable_glossary_candidate_id(source_key)
        if candidate_id in candidate_by_id:
            raise ValueError(f"duplicate glossary candidate ID: {candidate_id}")
        candidate_by_id[candidate_id] = candidate

    existing = set(existing_source_keys)
    seen: set[str] = set()
    identified: list[tuple[str, str, Mapping[str, Any]]] = []

    # Validate the complete identity set before inspecting semantic fields. A
    # later injected or rewritten candidate can therefore never hide behind an
    # earlier retryable action/field error.
    for raw in decisions:
        if not isinstance(raw, Mapping):
            raise GlossarySourceIdentityError(
                "every glossary review decision must be an identity-bearing object"
            )
        candidate_id = str(raw.get("candidate_id", "") or "").strip()
        if not candidate_id:
            raise GlossarySourceIdentityError(
                "glossary review decision omitted immutable candidate_id"
            )
        if candidate_id not in candidate_by_id:
            raise GlossarySourceIdentityError(
                f"glossary review injected unknown candidate_id: {candidate_id}"
            )
        if candidate_id in seen:
            raise GlossarySourceIdentityError(
                f"duplicate glossary review decision for candidate_id: {candidate_id}"
            )
        seen.add(candidate_id)
        candidate = candidate_by_id[candidate_id]
        source_key = str(candidate.get("chinese", "") or "").strip()
        echoed_source = raw.get("chinese")
        if echoed_source is not None and str(echoed_source).strip() != source_key:
            raise GlossarySourceIdentityError(
                f"glossary review rewrote source key for {candidate_id}"
            )
        identified.append((candidate_id, source_key, raw))

    missing = set(candidate_by_id) - seen
    if missing:
        raise GlossarySourceIdentityError(
            "glossary review omitted candidate IDs: " + ", ".join(sorted(missing))
        )

    normalized: list[dict[str, Any]] = []
    for candidate_id, source_key, raw in identified:
        action = str(raw.get("action", "") or "").strip().lower()
        if action not in VALID_REVIEW_ACTIONS:
            raise GlossaryReviewValidationError(
                f"invalid glossary review action for {candidate_id}: {action!r}",
                code="invalid_action",
                retryable=True,
                candidate_id=candidate_id,
            )
        item = dict(raw)
        item["candidate_id"] = candidate_id
        item["chinese"] = source_key
        item["action"] = action

        notes = item.get("notes", "")
        if not isinstance(notes, str):
            raise GlossaryReviewValidationError(
                f"invalid glossary review notes for {candidate_id}",
                code="invalid_notes",
                retryable=True,
                candidate_id=candidate_id,
                diagnostics={
                    "field": "notes",
                    "reason": "wrong_type",
                    "expected_type": "string",
                    "actual_type": type(notes).__name__,
                },
            )
        if len(notes) > MAX_REVIEW_NOTES_CHARS:
            raise GlossaryReviewValidationError(
                f"invalid glossary review notes for {candidate_id}",
                code="invalid_notes",
                retryable=True,
                candidate_id=candidate_id,
                diagnostics={
                    "field": "notes",
                    "reason": "max_length_exceeded",
                    "actual_length": len(notes),
                    "max_length": MAX_REVIEW_NOTES_CHARS,
                },
            )
        item["notes"] = notes

        if action == "correct":
            english = item.get("english")
            if not isinstance(english, str) or not english.strip():
                raise GlossaryReviewValidationError(
                    f"correct glossary review decision requires English for {candidate_id}",
                    code="missing_correct_english",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            item["english"] = english.strip()

        if action == "contextual":
            approved = item.get("approved_renderings")
            if (
                not isinstance(approved, list)
                or not approved
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in approved
                )
            ):
                raise GlossaryReviewValidationError(
                    "contextual glossary review decision requires approved "
                    f"renderings for {candidate_id}",
                    code="missing_contextual_renderings",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            item["approved_renderings"] = [value.strip() for value in approved]

        if action == "merge":
            item = normalize_glossary_review_merge_decision(
                item,
                source_key=source_key,
                candidate_source_keys={
                    str(candidate.get("chinese", "") or "").strip()
                    for candidate in candidates
                    if str(candidate.get("chinese", "") or "").strip()
                },
                existing_source_keys=existing,
                candidate_id=candidate_id,
            )
        normalized.append(item)
    order = {candidate_id: index for index, candidate_id in enumerate(candidate_by_id)}
    normalized.sort(key=lambda item: order[item["candidate_id"]])
    return {"decisions": normalized, "issues": list(issues)}


def collect_glossary_review_decision_validation_errors(
    result: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    existing_source_keys: Iterable[str],
) -> list[dict[str, Any]]:
    """Discover each original decision's first canonical semantic error.

    The canonical batch validator is intentionally reused. Each decision is
    moved to the front of an otherwise identical complete batch, so identity,
    same-batch merge context, and the authoritative action/field rules remain
    exactly the same while a later candidate cannot be hidden by an earlier
    fail-fast semantic error.
    """
    if not isinstance(result, Mapping) or not isinstance(
        result.get("decisions"), list
    ):
        # Reuse canonical schema/identity exceptions for malformed input.
        validate_glossary_review_result(result, candidates, existing_source_keys)
        return []
    decisions = result["decisions"]
    errors = []
    for index, raw in enumerate(decisions):
        if not isinstance(raw, Mapping):
            validate_glossary_review_result(
                result, candidates, existing_source_keys
            )
            return []
        candidate_id = str(raw.get("candidate_id", "") or "").strip()
        reordered = {
            **dict(result),
            "decisions": [
                raw,
                *decisions[:index],
                *decisions[index + 1:],
            ],
        }
        try:
            validate_glossary_review_result(
                reordered, candidates, existing_source_keys
            )
        except GlossaryReviewValidationError as exc:
            if exc.candidate_id == candidate_id:
                errors.append(exc.as_dict())
        # Schema and source-identity failures deliberately propagate. They are
        # not candidate-local semantic evidence and cannot authorize fields.
    return errors



def normalize_glossary_review_merge_decision(
    item: Mapping[str, Any],
    *,
    source_key: str,
    candidate_source_keys: Iterable[str],
    existing_source_keys: Iterable[str],
    candidate_id: str,
) -> dict[str, Any]:
    # Existing glossary targets stay true merges.
    # Same-batch new targets safely degrade to separate accepted entries.
    normalized = dict(item)
    merge_with = str(normalized.get("merge_with", "") or "").strip()
    if not merge_with:
        raise GlossaryReviewValidationError(
            f"glossary review merge target is missing for {candidate_id}",
            code="missing_merge_target",
            retryable=True,
            candidate_id=candidate_id,
        )

    source_key = str(source_key or "").strip()
    existing = {
        str(value or "").strip()
        for value in existing_source_keys
        if str(value or "").strip()
    }
    same_batch = {
        str(value or "").strip()
        for value in candidate_source_keys
        if str(value or "").strip()
    }

    if merge_with == source_key:
        raise GlossaryReviewValidationError(
            f"invalid glossary review merge target for {candidate_id}: {merge_with}",
            code="invalid_merge_target",
            retryable=True,
            candidate_id=candidate_id,
        )

    if merge_with in existing:
        normalized["merge_with"] = merge_with
        return normalized

    if merge_with in same_batch:
        normalized["action"] = "accept"
        normalized["merge_with"] = ""
        return normalized

    raise GlossaryReviewValidationError(
        f"invalid glossary review merge target for {candidate_id}: {merge_with}",
        code="invalid_merge_target",
        retryable=True,
        candidate_id=candidate_id,
    )


_REVIEW_SEMANTIC_CORRECTION_FIELDS = {
    "invalid_action": frozenset({"action"}),
    "invalid_notes": frozenset({"notes"}),
    "missing_correct_english": frozenset({"english"}),
    "missing_contextual_renderings": frozenset({"approved_renderings"}),
    "missing_merge_target": frozenset({"merge_with"}),
    "invalid_merge_target": frozenset({
        "action",
        "english",
        "approved_renderings",
        "merge_with",
        "notes",
    }),
}

_REVIEW_SCHEMA_CORRECTION_FIELDS = {
    "invalid_issues_shape": frozenset({"issues"}),
}


def validate_glossary_review_schema_correction_scope(
    original: Mapping[str, Any],
    corrected: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
) -> None:
    """Allow only explicitly allowlisted model-shape fields to change."""
    allowed_fields: set[str] = set()
    for error in errors:
        code = str(error.get("code", "") or "").strip()
        allowed = _REVIEW_SCHEMA_CORRECTION_FIELDS.get(code)
        if not allowed or error.get("retryable") is not True:
            raise GlossarySourceIdentityError(
                "glossary schema correction was requested for a non-allowlisted error"
            )
        allowed_fields.update(allowed)

    if not allowed_fields:
        raise GlossarySourceIdentityError(
            "glossary schema correction has no authorized fields"
        )

    original_fixed = {
        key: value for key, value in original.items() if key not in allowed_fields
    }
    corrected_fixed = {
        key: value for key, value in corrected.items() if key not in allowed_fields
    }
    if original_fixed != corrected_fixed:
        raise GlossarySourceIdentityError(
            "glossary schema correction modified content outside its authorized "
            "schema fields"
        )


def validate_glossary_review_semantic_correction_scope(
    original: Mapping[str, Any],
    corrected: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
) -> None:
    """Reject a correction that changes identity or already-valid fields."""
    allowed_by_id = {
        candidate_id: set(fields)
        for candidate_id, fields in (
            glossary_review_semantic_correction_authorization(errors).items()
        )
    }

    original_decisions = original.get("decisions")
    corrected_decisions = corrected.get("decisions")
    if not isinstance(original_decisions, list) or not isinstance(
        corrected_decisions, list
    ):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed decision structure"
        )
    original_by_id = {
        str(item.get("candidate_id", "") or "").strip(): item
        for item in original_decisions
        if isinstance(item, Mapping)
    }
    corrected_by_id = {
        str(item.get("candidate_id", "") or "").strip(): item
        for item in corrected_decisions
        if isinstance(item, Mapping)
    }
    if set(original_by_id) != set(corrected_by_id):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed candidate identity coverage"
        )
    if original.get("issues", []) != corrected.get("issues", []):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed review issues"
        )

    for candidate_id, before in original_by_id.items():
        after = corrected_by_id[candidate_id]
        allowed_fields = allowed_by_id.get(candidate_id, set())
        before_fixed = {
            key: value for key, value in before.items() if key not in allowed_fields
        }
        after_fixed = {
            key: value for key, value in after.items() if key not in allowed_fields
        }
        if before_fixed != after_fixed:
            raise GlossarySourceIdentityError(
                "glossary semantic correction modified fields outside its "
                f"authorized scope for {candidate_id}"
            )


def glossary_review_semantic_correction_authorization(
    errors: Sequence[Mapping[str, Any]],
) -> dict[str, frozenset[str]]:
    """Derive candidate-local corrective fields from trusted validator errors."""
    allowed_by_id: dict[str, set[str]] = {}
    if not errors:
        raise GlossarySourceIdentityError(
            "glossary semantic correction has no recorded validation errors"
        )
    for error in errors:
        if not isinstance(error, Mapping) or error.get("retryable") is not True:
            raise GlossarySourceIdentityError(
                "glossary semantic correction requires retryable validation errors"
            )
        code = str(error.get("code", "") or "").strip()
        allowed = _REVIEW_SEMANTIC_CORRECTION_FIELDS.get(code)
        if not allowed:
            raise GlossarySourceIdentityError(
                "glossary semantic correction was requested for an unknown or "
                f"non-allowlisted validation code: {code!r}"
            )
        candidate_id = str(error.get("candidate_id", "") or "").strip()
        if not candidate_id:
            raise GlossarySourceIdentityError(
                "glossary semantic correction error lacks candidate identity"
            )
        allowed_by_id.setdefault(candidate_id, set()).update(allowed)
    return {
        candidate_id: frozenset(fields)
        for candidate_id, fields in allowed_by_id.items()
    }


def merge_glossary_review_semantic_correction(
    original: Mapping[str, Any],
    corrected: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply only error-authorized fields as a patch over original decisions.

    A corrective response must retain the complete candidate identity set, but
    it may omit unchanged decision fields. Supplied changes outside a
    candidate's locally derived field scope are discarded and reported in the
    returned audit metadata; they can never influence the effective result.
    """
    authorization = glossary_review_semantic_correction_authorization(errors)
    original_decisions = original.get("decisions")
    corrected_decisions = corrected.get("decisions")
    if not isinstance(original_decisions, list) or not isinstance(
        corrected_decisions, list
    ):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed decision structure"
        )
    if original.get("issues", []) != corrected.get("issues", []):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed review issues"
        )

    def decisions_by_id(items, label):
        result = {}
        for item in items:
            if not isinstance(item, Mapping):
                raise GlossarySourceIdentityError(
                    f"glossary semantic correction {label} decision is not an object"
                )
            candidate_id = str(item.get("candidate_id", "") or "").strip()
            if not candidate_id or candidate_id in result:
                raise GlossarySourceIdentityError(
                    "glossary semantic correction has missing or duplicate "
                    f"candidate identity in {label} decisions"
                )
            result[candidate_id] = item
        return result

    original_by_id = decisions_by_id(original_decisions, "original")
    corrected_by_id = decisions_by_id(corrected_decisions, "corrective")
    if set(original_by_id) != set(corrected_by_id):
        raise GlossarySourceIdentityError(
            "glossary semantic correction changed candidate identity coverage"
        )
    if not set(authorization) <= set(original_by_id):
        raise GlossarySourceIdentityError(
            "glossary semantic correction error identity is outside the review batch"
        )

    effective_decisions = []
    applied_fields = []
    discarded_changes = []
    for before_item in original_decisions:
        candidate_id = str(before_item.get("candidate_id", "") or "").strip()
        before = copy.deepcopy(dict(before_item))
        after = corrected_by_id[candidate_id]
        allowed_fields = authorization.get(candidate_id, frozenset())
        for field, value in after.items():
            if field == "candidate_id":
                continue
            if field == "chinese":
                if value != before.get(field):
                    raise GlossarySourceIdentityError(
                        "glossary semantic correction rewrote source key for "
                        f"{candidate_id}"
                    )
                continue
            if field in allowed_fields:
                if before.get(field) != value:
                    before[field] = copy.deepcopy(value)
                    applied_fields.append({
                        "candidate_id": candidate_id,
                        "field": field,
                    })
            elif before.get(field) != value:
                discarded_changes.append({
                    "candidate_id": candidate_id,
                    "field": field,
                    "original_value": copy.deepcopy(before.get(field)),
                    "corrective_value": copy.deepcopy(value),
                })
        effective_decisions.append(before)

    return (
        {
            "decisions": effective_decisions,
            "issues": copy.deepcopy(list(original.get("issues", []))),
        },
        {
            "authorization": {
                candidate_id: sorted(fields)
                for candidate_id, fields in authorization.items()
            },
            "applied_fields": applied_fields,
            "discarded_unauthorized_changes": discarded_changes,
        },
    )
