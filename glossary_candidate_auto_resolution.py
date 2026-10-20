"""Fail-closed automatic selection for same-source glossary conflicts.

The model-facing tier is selection-only: every accepted decision maps to one
semantic fingerprint already present in the exact current conflict.  This
module is import-safe and performs no API work unless an injected callback is
called by the production adapter.
"""

from __future__ import annotations

from production_auxiliary_paid_request import (invoke_request, planned_attempt, resumable_attempt, failure_values, success_values)

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any, Callable, Mapping, Sequence

from glossary_candidate_resolution import (
    AUTOMATIC_RESOLUTION_POLICY_VERSION,
    GlossaryCandidateResolutionError,
    create_automatic_candidate_resolutions,
    load_candidate_resolution_record,
)
from glossary_integrity import glossary_candidate_conflict_fingerprint


ADJUDICATION_SCHEMA_VERSION = "glossary-candidate-conflict-adjudication-v1"
ADJUDICATION_RECOVERY_KEY = "glossary_candidate_conflict_adjudication"
MAX_ADJUDICATION_RESPONSE_CHARS = 20000
MAX_ADJUDICATION_CONFLICTS = 20
MAX_PROPOSALS_PER_CONFLICT = 8
MAX_ADJUDICATION_USER_PROMPT_CHARS = 24000
SOURCE_CONTEXT_RADIUS = 260
TARGET_CONTEXT_RADIUS = 220
MAX_GLOSSARY_CONTEXT_ENTRIES = 12

_RECOVERY_FIELDS = frozenset({
    "schema_version",
    "operation_id",
    "novel_id",
    "chapter",
    "pending_content_identity",
    "state_extraction_identity",
    "conflict_fingerprints",
    "policy_version",
    "request_sha256",
    "request",
    "status",
    "response_text",
    "response_sha256",
    "stats",
    "validated_result",
    "validated_result_sha256",
    "errors",
    "created_utc",
    "updated_utc",
    "record_sha256",
})
_RECOVERY_STATUSES = frozenset({
    "initiated",
    "planned",
    "uncertain",
    "blocked",
    "api_failed",
    "response_recorded",
    "accepted",
    "abstained",
    "invalid",
})
_DECISION_FIELDS = frozenset({"conflict_id", "selected_proposal_id"})


class GlossaryCandidateAutoResolutionError(ValueError):
    """Automatic resolution state is malformed, stale, or unsafe."""

    def __init__(self, message: str, *, code: str, diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = dict(diagnostics or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str) or not value.strip():
        raise GlossaryCandidateAutoResolutionError(
            "automatic resolution timestamp must be non-empty",
            code="auto_resolution_recovery_malformed",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GlossaryCandidateAutoResolutionError(
            "automatic resolution timestamp must be ISO-8601",
            code="auto_resolution_recovery_malformed",
        ) from exc
    if parsed.tzinfo is None:
        raise GlossaryCandidateAutoResolutionError(
            "automatic resolution timestamp must include a timezone",
            code="auto_resolution_recovery_malformed",
        )
    return value


def _normalized_category(value: Any) -> str:
    return " ".join(
        str(value or "").strip().lower().replace("_", " ").replace("-", " ").split()
    )


def _normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _validated_conflicts(conflicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(conflicts, Sequence) or isinstance(conflicts, (str, bytes)):
        raise GlossaryCandidateAutoResolutionError(
            "candidate conflicts must be a list",
            code="auto_resolution_conflict_invalid",
        )
    result = []
    for conflict in conflicts:
        if not isinstance(conflict, Mapping):
            raise GlossaryCandidateAutoResolutionError(
                "candidate conflict must be an object",
                code="auto_resolution_conflict_invalid",
            )
        item = copy.deepcopy(dict(conflict))
        if item.get("code") != "same_source_semantic_conflict":
            raise GlossaryCandidateAutoResolutionError(
                "automatic resolver only accepts same-source semantic conflicts",
                code="auto_resolution_conflict_type_unsupported",
            )
        proposals = item.get("proposals")
        if not isinstance(proposals, list) or len(proposals) < 2:
            raise GlossaryCandidateAutoResolutionError(
                "candidate conflict must retain at least two proposals",
                code="auto_resolution_conflict_invalid",
            )
        fingerprint = glossary_candidate_conflict_fingerprint(item)
        supplied = str(item.get("conflict_fingerprint", "") or "").strip()
        if supplied and supplied != fingerprint:
            raise GlossaryCandidateAutoResolutionError(
                "candidate conflict fingerprint changed",
                code="auto_resolution_conflict_fingerprint_mismatch",
            )
        item["conflict_fingerprint"] = fingerprint
        semantic_fingerprints = sorted({
            str(proposal.get("semantic_identity_fingerprint", "") or "").strip()
            for proposal in proposals
            if isinstance(proposal, Mapping)
        })
        if len(semantic_fingerprints) < 2 or any(
            len(value) != 64 for value in semantic_fingerprints
        ):
            raise GlossaryCandidateAutoResolutionError(
                "candidate conflict proposal set is invalid",
                code="auto_resolution_proposal_set_invalid",
            )
        supplied_set = item.get("conflicting_semantic_fingerprints")
        if supplied_set is not None and sorted(supplied_set) != semantic_fingerprints:
            raise GlossaryCandidateAutoResolutionError(
                "candidate conflict proposal set changed",
                code="auto_resolution_proposal_set_mismatch",
            )
        item["conflicting_semantic_fingerprints"] = semantic_fingerprints
        result.append(item)
    fingerprints = [item["conflict_fingerprint"] for item in result]
    if len(fingerprints) != len(set(fingerprints)):
        raise GlossaryCandidateAutoResolutionError(
            "candidate conflict set contains duplicates",
            code="auto_resolution_conflict_invalid",
        )
    result.sort(key=lambda item: item["conflict_fingerprint"])
    return result


def _proposal_identity(proposal: Mapping[str, Any]) -> dict[str, Any]:
    context = proposal.get("context_identity", {})
    if not isinstance(context, Mapping):
        return {}
    return {
        "english": _normalized_text(proposal.get("english")),
        "category": _normalized_category(proposal.get("category")),
        "status": str(proposal.get("status", "") or "").strip().lower(),
        "context_identity": copy.deepcopy(dict(context)),
    }


def _matches_existing_glossary_identity(
    proposal: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> bool:
    identity = _proposal_identity(proposal)
    if not identity or any(not identity[key] for key in ("english", "category", "status")):
        return False
    if identity["english"] != _normalized_text(entry.get("english")):
        return False
    if identity["category"] != _normalized_category(entry.get("category")):
        return False
    if identity["status"] != str(entry.get("status", "") or "").strip().lower():
        return False
    for key, value in identity["context_identity"].items():
        if key not in entry or entry[key] != value:
            return False
    if identity["status"] == "contextual" and "approved_renderings" not in identity[
        "context_identity"
    ]:
        return False
    return True


def _unique_proposals(conflict: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_fingerprint = {}
    for proposal in conflict["proposals"]:
        if not isinstance(proposal, Mapping):
            continue
        fingerprint = str(
            proposal.get("semantic_identity_fingerprint", "") or ""
        ).strip()
        if fingerprint:
            by_fingerprint.setdefault(fingerprint, copy.deepcopy(dict(proposal)))
    return [by_fingerprint[key] for key in sorted(by_fingerprint)]


def deterministic_candidate_conflict_resolution(
    conflicts: Sequence[Mapping[str, Any]],
    *,
    existing_glossary: Mapping[str, Any],
    category_normalizations: Mapping[str, str] | Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Resolve only uniquely proven existing proposal identities."""
    current = _validated_conflicts(conflicts)
    glossary = dict(existing_glossary or {})
    normalizations = {
        _normalized_category(source): _normalized_category(target)
        for source, target in dict(category_normalizations or {}).items()
    }
    resolved = []
    unresolved = []
    for conflict in current:
        proposals = _unique_proposals(conflict)
        source_key = str(conflict.get("source_term", "") or "").strip()
        established = glossary.get(source_key)
        if isinstance(established, Mapping):
            matches = [
                proposal for proposal in proposals
                if _matches_existing_glossary_identity(proposal, established)
            ]
            if len(matches) == 1:
                resolved.append({
                    "status": "resolved",
                    "method": "deterministic",
                    "policy": AUTOMATIC_RESOLUTION_POLICY_VERSION,
                    "conflict": conflict,
                    "conflict_fingerprint": conflict["conflict_fingerprint"],
                    "selected_semantic_fingerprint": matches[0][
                        "semantic_identity_fingerprint"
                    ],
                    "basis": [{
                        "proof": "existing_exact_glossary_identity",
                        "details": {
                            "source_key": source_key,
                            "english": _normalized_text(established.get("english")),
                            "category": _normalized_category(
                                established.get("category")
                            ),
                            "status": str(
                                established.get("status", "") or ""
                            ).strip().lower(),
                        },
                    }],
                    "rationale": (
                        "Automatically selected the only proposal matching the "
                        "existing exact glossary semantic identity."
                    ),
                })
                continue

        identities_without_category = []
        canonical_categories = []
        for proposal in proposals:
            identity = _proposal_identity(proposal)
            category = identity.pop("category", "")
            identities_without_category.append(_canonical_json(identity))
            canonical_categories.append(normalizations.get(category, category))
        if (
            normalizations
            and len(set(identities_without_category)) == 1
            and len(set(canonical_categories)) == 1
        ):
            canonical_category = canonical_categories[0]
            matches = [
                proposal for proposal in proposals
                if _normalized_category(proposal.get("category")) == canonical_category
            ]
            if len(matches) == 1 and any(
                _normalized_category(proposal.get("category")) in normalizations
                for proposal in proposals
            ):
                resolved.append({
                    "status": "resolved",
                    "method": "deterministic",
                    "policy": AUTOMATIC_RESOLUTION_POLICY_VERSION,
                    "conflict": conflict,
                    "conflict_fingerprint": conflict["conflict_fingerprint"],
                    "selected_semantic_fingerprint": matches[0][
                        "semantic_identity_fingerprint"
                    ],
                    "basis": [{
                        "proof": "configured_category_normalization",
                        "details": {
                            "canonical_category": canonical_category,
                            "proposal_categories": sorted({
                                _normalized_category(item.get("category"))
                                for item in proposals
                            }),
                        },
                    }],
                    "rationale": (
                        "Automatically selected the only proposal matching an "
                        "explicit structured category normalization."
                    ),
                })
                continue
        unresolved.append(conflict)
    return {
        "status": "resolved" if not unresolved else "unresolved",
        "method": "deterministic",
        "policy": AUTOMATIC_RESOLUTION_POLICY_VERSION,
        "resolved": resolved,
        "unresolved": unresolved,
    }


def _bounded_context(text: str, needle: str, radius: int, fallback_start=None) -> str:
    text = str(text or "")
    position = text.find(needle) if needle else -1
    if position < 0 and isinstance(fallback_start, int):
        position = max(0, min(fallback_start, len(text)))
    if position < 0:
        return ""
    start = max(0, position - radius)
    end = min(len(text), position + max(len(needle), 1) + radius)
    return text[start:end]


def build_adjudication_request(
    *,
    chapter: int,
    conflicts: Sequence[Mapping[str, Any]],
    source_text: str,
    translation: str,
    existing_glossary: Mapping[str, Any],
) -> dict[str, Any]:
    current = _validated_conflicts(conflicts)
    if len(current) > MAX_ADJUDICATION_CONFLICTS:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication conflict batch exceeds the bounded limit",
            code="adjudication_request_too_large",
        )
    transport = {}
    model_conflicts = []
    glossary = dict(existing_glossary or {})
    next_proposal_index = 1
    for conflict_index, conflict in enumerate(current, 1):
        conflict_id = f"c{conflict_index:02d}"
        source_key = str(conflict.get("source_term", "") or "")
        proposals = _unique_proposals(conflict)
        if len(proposals) > MAX_PROPOSALS_PER_CONFLICT:
            raise GlossaryCandidateAutoResolutionError(
                "adjudication proposal set exceeds the bounded limit",
                code="adjudication_request_too_large",
            )
        span_starts = [
            proposal.get("source_occurrence_evidence", {}).get("span_start")
            for proposal in proposals
            if isinstance(proposal.get("source_occurrence_evidence"), Mapping)
        ]
        span_start = next((value for value in span_starts if isinstance(value, int)), None)
        source_context = _bounded_context(
            source_text, source_key, SOURCE_CONTEXT_RADIUS, span_start
        )
        english_needles = sorted({
            str(proposal.get("english", "") or "").strip()
            for proposal in proposals
            if str(proposal.get("english", "") or "").strip()
        }, key=lambda value: (-len(value), value))
        target_context = ""
        for needle in english_needles:
            target_context = _bounded_context(
                translation, needle, TARGET_CONTEXT_RADIUS
            )
            if target_context:
                break
        relevant_entries = []
        for glossary_source in sorted(glossary):
            entry = glossary[glossary_source]
            if not isinstance(entry, Mapping):
                continue
            if glossary_source != source_key and glossary_source not in source_context:
                continue
            relevant_entries.append({
                "source": glossary_source,
                "english": entry.get("english", ""),
                "category": entry.get("category", ""),
                "status": entry.get("status", ""),
            })
            if len(relevant_entries) >= MAX_GLOSSARY_CONTEXT_ENTRIES:
                break
        model_proposals = []
        option_map = {}
        for proposal in proposals:
            proposal_id = f"p{next_proposal_index:02d}"
            next_proposal_index += 1
            option_map[proposal_id] = proposal["semantic_identity_fingerprint"]
            model_proposals.append({
                "proposal_id": proposal_id,
                "english": proposal.get("english", ""),
                "category": proposal.get("category", ""),
                "status": proposal.get("status", ""),
                "notes": str(proposal.get("notes", "") or "")[:500],
            })
        transport[conflict_id] = {
            "conflict_fingerprint": conflict["conflict_fingerprint"],
            "options": option_map,
        }
        model_conflicts.append({
            "conflict_id": conflict_id,
            "source": source_key,
            "source_context": source_context,
            "target_context": target_context,
            "existing_glossary_context": relevant_entries,
            "proposals": model_proposals,
        })
    payload = {"chapter": chapter, "conflicts": model_conflicts}
    system_prompt = (
        "You adjudicate an existing glossary candidate conflict. This is a "
        "selection-only task. For every conflict, select exactly one supplied "
        "proposal_id or return null to abstain. Never invent, combine, correct, "
        "rewrite, or add a proposal. Return only one JSON object with exactly the "
        "field decisions; each decision must contain exactly conflict_id and "
        "selected_proposal_id."
    )
    user_prompt = (
        _canonical_json(payload)
        + "\nReturn only: {\"decisions\":[{\"conflict_id\":\"c01\","
        "\"selected_proposal_id\":\"p01\"}]} (use null to abstain)."
    )
    if len(user_prompt) > MAX_ADJUDICATION_USER_PROMPT_CHARS:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication prompt exceeds the bounded character limit",
            code="adjudication_request_too_large",
        )
    request = {
        "policy_version": AUTOMATIC_RESOLUTION_POLICY_VERSION,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "payload": payload,
        "transport": transport,
    }
    return {"request": request, "request_sha256": _sha256_json(request)}


def adjudication_operation_id(
    *,
    novel_id: str,
    chapter: int,
    pending_identity: str,
    state_identity: str,
    conflict_fingerprints: Sequence[str],
) -> str:
    conflict_set_hash = _sha256_json(sorted(conflict_fingerprints))
    return (
        "glossary_candidate_conflict_adjudication:"
        f"{novel_id}:{chapter}:{pending_identity}:{state_identity}:"
        f"{conflict_set_hash}:{AUTOMATIC_RESOLUTION_POLICY_VERSION}"
    )


def validate_adjudication_result(
    result: Any,
    transport: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(result, Mapping) or set(result) != {"decisions"}:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication result must contain only decisions",
            code="adjudication_schema_invalid",
        )
    decisions = result["decisions"]
    if not isinstance(decisions, list):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication decisions must be a list",
            code="adjudication_schema_invalid",
        )
    known_conflicts = set(transport)
    seen = set()
    selections = []
    abstained = []
    normalized_decisions = []
    for decision in decisions:
        if not isinstance(decision, Mapping) or set(decision) != _DECISION_FIELDS:
            raise GlossaryCandidateAutoResolutionError(
                "adjudication decision has unknown or missing fields",
                code="adjudication_schema_invalid",
            )
        conflict_id = decision.get("conflict_id")
        if not isinstance(conflict_id, str) or conflict_id not in known_conflicts:
            raise GlossaryCandidateAutoResolutionError(
                "adjudication returned an unknown conflict ID",
                code="adjudication_unknown_conflict",
            )
        if conflict_id in seen:
            raise GlossaryCandidateAutoResolutionError(
                "adjudication returned duplicate decisions for one conflict",
                code="adjudication_duplicate_conflict",
            )
        seen.add(conflict_id)
        proposal_id = decision.get("selected_proposal_id")
        if proposal_id is None:
            abstained.append(conflict_id)
        else:
            if not isinstance(proposal_id, str):
                raise GlossaryCandidateAutoResolutionError(
                    "adjudication proposal ID must be a string or null",
                    code="adjudication_unknown_proposal",
                )
            options = transport[conflict_id]["options"]
            if proposal_id not in options:
                raise GlossaryCandidateAutoResolutionError(
                    "adjudication selected a proposal outside its conflict",
                    code="adjudication_unknown_proposal",
                )
            selections.append({
                "conflict_id": conflict_id,
                "selected_proposal_id": proposal_id,
                "selected_semantic_fingerprint": options[proposal_id],
                "conflict_fingerprint": transport[conflict_id][
                    "conflict_fingerprint"
                ],
            })
        normalized_decisions.append({
            "conflict_id": conflict_id,
            "selected_proposal_id": proposal_id,
        })
    missing = sorted(known_conflicts - seen)
    if missing:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication omitted an unresolved conflict",
            code="adjudication_missing_conflict",
            diagnostics={"missing_conflict_ids": missing},
        )
    normalized_decisions.sort(key=lambda item: item["conflict_id"])
    selections.sort(key=lambda item: item["conflict_id"])
    return {
        "status": "abstained" if abstained else "resolved",
        "decisions": normalized_decisions,
        "selections": selections,
        "abstained_conflict_ids": sorted(abstained),
    }


def _recovery_record_sha256(record: Mapping[str, Any]) -> str:
    body = dict(record)
    body.pop("record_sha256", None)
    return _sha256_json(body)


def _with_recovery_hash(record: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(record))
    result["record_sha256"] = _recovery_record_sha256(result)
    return result


def validate_adjudication_recovery_record(
    record: Any,
    *,
    novel_id: str,
    chapter: int,
    pending_identity: str,
    state_identity: str,
    conflict_fingerprints: Sequence[str],
    request_sha256: str,
    operation_id: str,
) -> dict[str, Any]:
    optional = {"transport_journaled", "transport_evidence", "api_called"}
    if (not isinstance(record, Mapping) or not _RECOVERY_FIELDS <= set(record)
        or set(record) - _RECOVERY_FIELDS - optional
        or (set(record) & optional and record.get("transport_journaled") is not True)):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery record has invalid fields",
            code="auto_resolution_recovery_malformed",
        )
    result = copy.deepcopy(dict(record))
    if result["schema_version"] != ADJUDICATION_SCHEMA_VERSION:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery schema is unsupported",
            code="auto_resolution_recovery_malformed",
        )
    expected = {
        "operation_id": operation_id,
        "novel_id": novel_id,
        "chapter": chapter,
        "pending_content_identity": pending_identity,
        "state_extraction_identity": state_identity,
        "conflict_fingerprints": sorted(conflict_fingerprints),
        "policy_version": AUTOMATIC_RESOLUTION_POLICY_VERSION,
        "request_sha256": request_sha256,
    }
    for field, value in expected.items():
        if result.get(field) != value:
            raise GlossaryCandidateAutoResolutionError(
                f"adjudication recovery {field} is stale or mismatched",
                code=f"auto_resolution_recovery_{field}_mismatch",
            )
    if _sha256_json(result.get("request")) != request_sha256:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery request hash does not match",
            code="auto_resolution_recovery_request_mismatch",
        )
    if result["status"] not in _RECOVERY_STATUSES:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery status is invalid",
            code="auto_resolution_recovery_malformed",
        )
    if not isinstance(result["response_text"], str):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery response must be text",
            code="auto_resolution_recovery_malformed",
        )
    if _sha256_text(result["response_text"]) != result["response_sha256"]:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery response hash does not match",
            code="auto_resolution_recovery_response_mismatch",
        )
    if not isinstance(result["stats"], Mapping):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery stats must be an object",
            code="auto_resolution_recovery_malformed",
        )
    if not isinstance(result["validated_result"], Mapping):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery validated result must be an object",
            code="auto_resolution_recovery_malformed",
        )
    if _sha256_json(result["validated_result"]) != result["validated_result_sha256"]:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication validated result hash does not match",
            code="auto_resolution_recovery_result_mismatch",
        )
    if not isinstance(result["errors"], list) or any(
        not isinstance(item, str) for item in result["errors"]
    ):
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery errors must be strings",
            code="auto_resolution_recovery_malformed",
        )
    if result["status"] in {"planned", "uncertain", "blocked"} and result.get("transport_journaled") is not True:
        raise GlossaryCandidateAutoResolutionError(
            "transport status requires journal-aware domain provenance",
            code="auto_resolution_recovery_malformed")
    if "api_called" in result and not isinstance(result["api_called"], bool):
        raise GlossaryCandidateAutoResolutionError(
            "transport api_called must be boolean", code="auto_resolution_recovery_malformed")
    if "transport_evidence" in result and not isinstance(result["transport_evidence"], Mapping):
        raise GlossaryCandidateAutoResolutionError(
            "transport evidence must be an object", code="auto_resolution_recovery_malformed")
    _timestamp(result["created_utc"])
    _timestamp(result["updated_utc"])
    if _recovery_record_sha256(result) != result["record_sha256"]:
        raise GlossaryCandidateAutoResolutionError(
            "adjudication recovery record hash does not match",
            code="auto_resolution_recovery_hash_mismatch",
        )
    return result


def _config_value(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def _persist_updated_recovery(
    record: Mapping[str, Any],
    persist_recovery: Callable[[Mapping[str, Any]], None],
    **changes: Any,
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(record))
    updated.update(copy.deepcopy(changes))
    updated["updated_utc"] = _timestamp()
    updated = _with_recovery_hash(updated)
    persist_recovery(updated)
    return updated


def resolve_candidate_conflicts_automatically(
    *,
    novel_id: str,
    chapter: int,
    pending_identity: str,
    state_identity: str,
    conflicts: Sequence[Mapping[str, Any]],
    source_text: str,
    translation: str,
    existing_glossary: Mapping[str, Any],
    config: Any,
    resolution_path: Path | str,
    recovery_record: Mapping[str, Any] | None,
    persist_recovery: Callable[[Mapping[str, Any]], None],
    request_api: Callable[[Mapping[str, Any]], tuple[str, Mapping[str, Any]]],
    is_operation_accounted: Callable[[str], bool],
    account_api_usage: Callable[[str, Mapping[str, Any]], Any],
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Run Tier 1, then at most one recoverable Tier-2 request."""
    if not _config_value(config, "enabled", False):
        return {"status": "disabled", "api_called": False}
    current = _validated_conflicts(conflicts)
    existing = load_candidate_resolution_record(
        resolution_path,
        novel_id=novel_id,
        chapter=chapter,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    if existing is not None:
        return {
            "status": "existing_resolution_present",
            "api_called": False,
            "resolution_record": existing,
        }
    category_rules = _config_value(config, "category_normalizations", ())
    deterministic = deterministic_candidate_conflict_resolution(
        current,
        existing_glossary=existing_glossary,
        category_normalizations=dict(category_rules or ()),
    )
    deterministic_selections = [{
        "conflict": item["conflict"],
        "selected_semantic_fingerprint": item[
            "selected_semantic_fingerprint"
        ],
        "resolution_method": "deterministic",
        "deterministic_basis": item["basis"],
        "adjudication": {},
        "rationale": item["rationale"],
    } for item in deterministic["resolved"]]
    if not deterministic["unresolved"]:
        created = create_automatic_candidate_resolutions(
            resolution_path,
            novel_id=novel_id,
            chapter=chapter,
            pending_identity=pending_identity,
            state_identity=state_identity,
            selections=deterministic_selections,
            timestamp_utc=timestamp_utc,
        )
        return {
            "status": "resolved",
            "method": "deterministic",
            "api_called": False,
            "deterministic": deterministic,
            "created": created,
        }
    if not _config_value(config, "allow_model_adjudication", False):
        return {
            "status": "unresolved",
            "reason": "model_adjudication_disabled",
            "api_called": False,
            "deterministic": deterministic,
        }
    if _config_value(config, "max_model_requests_per_chapter", 1) != 1:
        raise GlossaryCandidateAutoResolutionError(
            "automatic adjudication request limit must be exactly one",
            code="auto_resolution_request_limit_invalid",
        )

    unresolved = deterministic["unresolved"]
    built = build_adjudication_request(
        chapter=chapter,
        conflicts=unresolved,
        source_text=source_text,
        translation=translation,
        existing_glossary=existing_glossary,
    )
    conflict_fingerprints = [
        item["conflict_fingerprint"] for item in unresolved
    ]
    operation_id = adjudication_operation_id(
        novel_id=novel_id,
        chapter=chapter,
        pending_identity=pending_identity,
        state_identity=state_identity,
        conflict_fingerprints=conflict_fingerprints,
    )
    api_called = False
    validated_recovery = None
    if recovery_record is not None:
        validated_recovery = validate_adjudication_recovery_record(
            recovery_record, novel_id=novel_id, chapter=chapter,
            pending_identity=pending_identity, state_identity=state_identity,
            conflict_fingerprints=conflict_fingerprints,
            request_sha256=built["request_sha256"], operation_id=operation_id)
    resume_transport = resumable_attempt(validated_recovery, request_api)
    if recovery_record is None or resume_transport:
        created_at = _timestamp(timestamp_utc)
        record = _with_recovery_hash({
            "schema_version": ADJUDICATION_SCHEMA_VERSION,
            "operation_id": operation_id,
            "novel_id": novel_id,
            "chapter": chapter,
            "pending_content_identity": pending_identity,
            "state_extraction_identity": state_identity,
            "conflict_fingerprints": sorted(conflict_fingerprints),
            "policy_version": AUTOMATIC_RESOLUTION_POLICY_VERSION,
            "request_sha256": built["request_sha256"],
            "request": built["request"],
            "status": "initiated",
            "response_text": "",
            "response_sha256": _sha256_text(""),
            "stats": {},
            "validated_result": {},
            "validated_result_sha256": _sha256_json({}),
            "errors": [],
            "created_utc": created_at,
            "updated_utc": created_at,
        })
        if resume_transport:
            record = validated_recovery
        elif getattr(request_api, "journaled", False):
            record = _with_recovery_hash({**record, **planned_attempt(request_api)})
        persist_recovery(record)
        try:
            response_text, stats = invoke_request(request_api, {
                **copy.deepcopy(built["request"]),
                "operation_id": operation_id,
                "request_sha256": built["request_sha256"],
            }, operation_id)
            api_called = not stats.get("transport_evidence", {}).get("replayed", False)
        except Exception as exc:
            transport_failure = failure_values(exc, record)
            if transport_failure.get("stats"):
                account_api_usage(operation_id, transport_failure["stats"])
            if not getattr(request_api, "journaled", False):
                transport_failure = {"status": "api_failed"}
            record = _persist_updated_recovery(
                record,
                persist_recovery,
                **transport_failure,
                errors=[f"{type(exc).__name__}: {str(exc)[:500]}"],
            )
            return {
                "status": "unresolved",
                "reason": "adjudication_api_failed",
                "api_called": failure_values(exc)["api_called"],
                "operation_id": operation_id,
                "recovery_record": record,
                "deterministic": deterministic,
            }
        if not isinstance(response_text, str) or not isinstance(stats, Mapping):
            raise GlossaryCandidateAutoResolutionError(
                "adjudication callback returned an invalid response envelope",
                code="adjudication_transport_invalid",
            )
        record = _persist_updated_recovery(
            record,
            persist_recovery,
            status="response_recorded",
            response_text=response_text,
            response_sha256=_sha256_text(response_text),
            stats=copy.deepcopy(dict(stats)),
            **success_values(stats),
        )
    else:
        record = validate_adjudication_recovery_record(
            recovery_record,
            novel_id=novel_id,
            chapter=chapter,
            pending_identity=pending_identity,
            state_identity=state_identity,
            conflict_fingerprints=conflict_fingerprints,
            request_sha256=built["request_sha256"],
            operation_id=operation_id,
        )
        if record["status"] in {"initiated", "api_failed"}:
            return {
                "status": "unresolved",
                "reason": "adjudication_attempt_already_consumed",
                "api_called": False,
                "operation_id": operation_id,
                "recovery_record": record,
                "deterministic": deterministic,
            }
        if record["status"] in {"invalid", "abstained"}:
            return {
                "status": "unresolved",
                "reason": f"adjudication_{record['status']}",
                "api_called": False,
                "operation_id": operation_id,
                "recovery_record": record,
                "deterministic": deterministic,
            }

    if not is_operation_accounted(operation_id):
        account_api_usage(operation_id, record["stats"])

    if len(record["response_text"]) > MAX_ADJUDICATION_RESPONSE_CHARS:
        validation_error = GlossaryCandidateAutoResolutionError(
            "adjudication response exceeds the bounded size",
            code="adjudication_response_too_large",
        )
    else:
        try:
            parsed = json.loads(record["response_text"])
            validated = validate_adjudication_result(
                parsed, built["request"]["transport"]
            )
        except (json.JSONDecodeError, GlossaryCandidateAutoResolutionError) as exc:
            validation_error = exc
        else:
            validation_error = None
    if validation_error is not None:
        record = _persist_updated_recovery(
            record,
            persist_recovery,
            status="invalid",
            validated_result={},
            validated_result_sha256=_sha256_json({}),
            errors=[
                f"{getattr(validation_error, 'code', type(validation_error).__name__)}: "
                f"{str(validation_error)[:500]}"
            ],
        )
        return {
            "status": "unresolved",
            "reason": "adjudication_invalid",
            "api_called": api_called,
            "operation_id": operation_id,
            "recovery_record": record,
            "deterministic": deterministic,
        }
    record = _persist_updated_recovery(
        record,
        persist_recovery,
        status=("abstained" if validated["status"] == "abstained" else "accepted"),
        validated_result=validated,
        validated_result_sha256=_sha256_json(validated),
        errors=[],
    )
    if validated["status"] == "abstained":
        return {
            "status": "unresolved",
            "reason": "adjudication_abstained",
            "api_called": api_called,
            "operation_id": operation_id,
            "recovery_record": record,
            "deterministic": deterministic,
        }

    conflicts_by_fingerprint = {
        item["conflict_fingerprint"]: item for item in unresolved
    }
    model_selections = []
    for selection in validated["selections"]:
        model_selections.append({
            "conflict": conflicts_by_fingerprint[
                selection["conflict_fingerprint"]
            ],
            "selected_semantic_fingerprint": selection[
                "selected_semantic_fingerprint"
            ],
            "resolution_method": "model_adjudicated",
            "deterministic_basis": [],
            "adjudication": {
                "operation_id": operation_id,
                "request_sha256": built["request_sha256"],
                "validated_result_sha256": record[
                    "validated_result_sha256"
                ],
                "conflict_id": selection["conflict_id"],
                "selected_proposal_id": selection["selected_proposal_id"],
                "stats": copy.deepcopy(record["stats"]),
            },
            "rationale": (
                "Automatically selected the exact existing proposal chosen by "
                "one bounded semantic adjudication."
            ),
        })
    created = create_automatic_candidate_resolutions(
        resolution_path,
        novel_id=novel_id,
        chapter=chapter,
        pending_identity=pending_identity,
        state_identity=state_identity,
        selections=deterministic_selections + model_selections,
        timestamp_utc=timestamp_utc,
    )
    return {
        "status": "resolved",
        "method": "model_adjudicated",
        "api_called": api_called,
        "operation_id": operation_id,
        "recovery_record": record,
        "deterministic": deterministic,
        "adjudication": validated,
        "created": created,
    }
