"""Import-safe chunking and recovery helpers for periodic glossary audits."""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from model_candidate_ids import (
    MODEL_CANDIDATE_ID_TRANSPORT_VERSION,
    ModelCandidateIdError,
    ModelCandidateIdMap,
    build_model_candidate_id_map,
    model_facing_candidates,
    resolve_model_candidate_response,
)
from noveltranslator_core import atomic_write_json, clean_json_text


AUDIT_CHECKPOINT_SCHEMA_VERSION = 1
AUDIT_CHUNK_POLICY_VERSION = "source-backed-candidate-ids-v2"
AUDIT_FAMILY_POLICY_VERSION = "structured-alias-family-v1"
AUDIT_ATTEMPT_LIFECYCLE_VERSION = "audit-attempt-lifecycle-v1"
CONTEXTUAL_RENDERING_LOCAL_POLICY = "contextual-rendering-local-v1"
DEFAULT_MAX_CANDIDATES_PER_CHUNK = 20
DEFAULT_MAX_SERIALIZED_CHARS_PER_CHUNK = 24_000
DEFAULT_MAX_SPLIT_DEPTH = 6
DEFAULT_MAX_MIN_CHUNK_FAILURES = 2
VALID_AUDIT_ACTIONS = frozenset({"keep", "correct", "reject", "contextual", "merge"})
MAX_AUDIT_ISSUES_PER_CHUNK = 5
MAX_AUDIT_ISSUE_CHARS = 500
MAX_AUDIT_NOTES_CHARS = 500
STAT_TOTAL_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "rate_correct_cost_usd",
)


class GlossaryAuditError(RuntimeError):
    """Base error for a safely stopped chunked glossary audit."""


class AuditCheckpointMismatch(GlossaryAuditError):
    """A saved checkpoint does not belong to the current audit input."""


class AuditChunkFailure(GlossaryAuditError):
    """One audit chunk could not produce a complete validated result."""


class AuditResultValidationError(ValueError):
    """A parsed audit response failed bounded schema/semantic validation."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        candidate_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.candidate_id = candidate_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class AuditChunkConfig:
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_CHUNK
    max_serialized_chars: int = DEFAULT_MAX_SERIALIZED_CHARS_PER_CHUNK
    max_split_depth: int = DEFAULT_MAX_SPLIT_DEPTH
    max_min_chunk_failures: int = DEFAULT_MAX_MIN_CHUNK_FAILURES
    policy_version: str = AUDIT_CHUNK_POLICY_VERSION

    def validate(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if self.max_serialized_chars < 1:
            raise ValueError("max_serialized_chars must be at least 1")
        if self.max_split_depth < 0:
            raise ValueError("max_split_depth cannot be negative")
        if self.max_min_chunk_failures < 1:
            raise ValueError("max_min_chunk_failures must be at least 1")
        if not self.policy_version:
            raise ValueError("policy_version must be non-empty")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _candidate_source(candidate: Mapping[str, Any]) -> str:
    source = candidate.get("chinese")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("every audit candidate requires a non-empty 'chinese' key")
    return source.strip()


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    candidate_id = candidate.get("candidate_id")
    if candidate_id is None:
        return _candidate_source(candidate)
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("audit candidate_id must be a non-empty string")
    return candidate_id.strip()


def _serialized_candidate_chars(candidates: Sequence[Mapping[str, Any]]) -> int:
    return len(_canonical_json(list(candidates)))


def _family_id(root_source_key: str) -> str:
    digest = hashlib.sha256(root_source_key.encode("utf-8")).hexdigest()
    return f"family:{digest}"


def resolve_audit_candidate_families(
    candidates: Sequence[Mapping[str, Any]],
    *,
    all_glossary_keys: Iterable[str] | None = None,
    glossary_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve structured alias families without conflating exact identities."""
    by_source: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        source = _candidate_source(candidate)
        if source in by_source:
            raise ValueError(f"duplicate audit candidate source key: {source}")
        by_source[source] = candidate

    entries = dict(glossary_entries or {})
    known_sources = set(all_glossary_keys or ()) | set(entries) | set(by_source)
    alias_targets: dict[str, str | None] = {}

    def normalized_alias(source: str, value: Any, *, label: str) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"malformed alias target for {source} in {label}")
        target = value.strip()
        if target not in known_sources:
            raise ValueError(f"missing alias target for {source}: {target}")
        return target

    for source in known_sources:
        entry = entries.get(source)
        entry_alias = None
        if entry is not None:
            if not isinstance(entry, Mapping):
                raise ValueError(f"malformed glossary entry for alias family: {source}")
            entry_alias = normalized_alias(
                source, entry.get("alias_of"), label="glossary entry"
            )
        candidate_alias = None
        if source in by_source and "alias_of" in by_source[source]:
            candidate_alias = normalized_alias(
                source,
                by_source[source].get("alias_of"),
                label="audit candidate",
            )
        if entry_alias and candidate_alias and entry_alias != candidate_alias:
            raise ValueError(
                f"ambiguous alias family identity for {source}: "
                f"{entry_alias} != {candidate_alias}"
            )
        alias_targets[source] = candidate_alias or entry_alias

    roots: dict[str, str] = {}
    visiting: list[str] = []

    def resolve_root(source: str) -> str:
        if source in roots:
            return roots[source]
        if source in visiting:
            cycle = visiting[visiting.index(source):] + [source]
            raise ValueError("alias family cycle detected: " + " -> ".join(cycle))
        visiting.append(source)
        target = alias_targets.get(source)
        root = resolve_root(target) if target else source
        visiting.pop()
        roots[source] = root
        return root

    member_context: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for source, candidate in by_source.items():
        root = resolve_root(source)
        family_id = _family_id(root)
        context = {
            "candidate_id": _candidate_id(candidate),
            "source_key": source,
            "family_id": family_id,
            "family_root": root,
            "alias_of": alias_targets.get(source),
        }
        member_context[_candidate_id(candidate)] = context
        grouped.setdefault(family_id, []).append(context)

    family_units = []
    for family_id, members in grouped.items():
        members.sort(
            key=lambda item: (
                0 if item["source_key"] == item["family_root"] else 1,
                item["source_key"],
                item["candidate_id"],
            )
        )
        roots_in_family = {item["family_root"] for item in members}
        if len(roots_in_family) != 1:
            raise ValueError(f"ambiguous family root for {family_id}")
        family_units.append({
            "family_id": family_id,
            "family_root": members[0]["family_root"],
            "members": copy.deepcopy(members),
        })
    family_units.sort(key=lambda item: (item["family_root"], item["family_id"]))
    return {
        "policy": AUDIT_FAMILY_POLICY_VERSION,
        "members_by_candidate_id": member_context,
        "families": family_units,
    }


def _candidate_families(
    candidates: Sequence[Mapping[str, Any]],
    *,
    all_glossary_keys: Iterable[str] | None = None,
    glossary_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[list[Mapping[str, Any]]], dict[str, dict[str, Any]]]:
    family_plan = resolve_audit_candidate_families(
        candidates,
        all_glossary_keys=all_glossary_keys,
        glossary_entries=glossary_entries,
    )
    by_id = {_candidate_id(candidate): candidate for candidate in candidates}
    families = [
        [by_id[member["candidate_id"]] for member in unit["members"]]
        for unit in family_plan["families"]
    ]
    return families, family_plan["members_by_candidate_id"]


def _split_oversized_family(
    family: Sequence[Mapping[str, Any]], config: AuditChunkConfig
) -> list[list[Mapping[str, Any]]]:
    pieces: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for candidate in family:
        proposed = current + [candidate]
        if current and (
            len(proposed) > config.max_candidates
            or _serialized_candidate_chars(proposed) > config.max_serialized_chars
        ):
            pieces.append(current)
            current = [candidate]
        else:
            current = proposed
    if current:
        pieces.append(current)
    return pieces


def build_audit_chunks(
    candidates: Sequence[Mapping[str, Any]],
    config: AuditChunkConfig | None = None,
    *,
    all_glossary_keys: Iterable[str] | None = None,
    glossary_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build stable candidate chunks, keeping known alias families together."""
    config = config or AuditChunkConfig()
    config.validate()
    families, family_context = _candidate_families(
        candidates,
        all_glossary_keys=all_glossary_keys,
        glossary_entries=glossary_entries,
    )
    bounded_families: list[list[Mapping[str, Any]]] = []
    for family in families:
        pieces = _split_oversized_family(family, config)
        if len(pieces) != 1:
            root = family_context[_candidate_id(family[0])]["family_root"]
            raise ValueError(
                f"audit alias family exceeds one bounded chunk: {root}"
            )
        bounded_families.extend(pieces)

    packed: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for family in bounded_families:
        proposed = current + family
        if current and (
            len(proposed) > config.max_candidates
            or _serialized_candidate_chars(proposed) > config.max_serialized_chars
        ):
            packed.append(current)
            current = list(family)
        else:
            current = proposed
    if current:
        packed.append(current)

    chunks = []
    for index, chunk in enumerate(packed, start=1):
        candidate_ids = [_candidate_id(candidate) for candidate in chunk]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("audit candidates require unique candidate IDs")
        chunks.append(
            {
                "chunk_id": f"c{index:04d}",
                "candidate_ids": candidate_ids,
                "family_keys": [
                    family_context[_candidate_id(candidate)]["family_id"]
                    for candidate in chunk
                ],
                "family_ids": [
                    family_context[_candidate_id(candidate)]["family_id"]
                    for candidate in chunk
                ],
                "family_roots": [
                    family_context[_candidate_id(candidate)]["family_root"]
                    for candidate in chunk
                ],
                "depth": 0,
                "status": "pending",
                "attempts": 0,
            }
        )
    return chunks


def audit_candidate_set_hash(
    candidates: Sequence[Mapping[str, Any]],
    config: AuditChunkConfig,
    context_fingerprint: str = "",
) -> str:
    config.validate()
    ordered = sorted(candidates, key=_candidate_source)
    payload = {
        "policy": config.policy_version,
        "config": asdict(config),
        "context_fingerprint": context_fingerprint,
        "candidates": ordered,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _new_checkpoint(
    start_chapter: int,
    end_chapter: int,
    candidate_hash: str,
    chunks: list[dict[str, Any]],
    config: AuditChunkConfig,
) -> dict[str, Any]:
    return {
        "schema_version": AUDIT_CHECKPOINT_SCHEMA_VERSION,
        "policy_version": config.policy_version,
        "audit_window": {"start_chapter": start_chapter, "end_chapter": end_chapter},
        "candidate_set_hash": candidate_hash,
        "config": asdict(config),
        "status": "in_progress",
        "chunks": chunks,
        "attempt_records": [],
    }


def _load_or_create_checkpoint(
    checkpoint_path: Path,
    start_chapter: int,
    end_chapter: int,
    candidate_hash: str,
    chunks: list[dict[str, Any]],
    config: AuditChunkConfig,
    supersede_incompatible_policy: bool = False,
) -> tuple[dict[str, Any], bool]:
    if not checkpoint_path.exists():
        checkpoint = _new_checkpoint(
            start_chapter, end_chapter, candidate_hash, chunks, config
        )
        atomic_write_json(checkpoint_path, checkpoint)
        return checkpoint, False
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditCheckpointMismatch(
            f"periodic audit checkpoint is unreadable: {checkpoint_path}"
        ) from exc
    expected_window = {"start_chapter": start_chapter, "end_chapter": end_chapter}
    mismatches = []
    if checkpoint.get("schema_version") != AUDIT_CHECKPOINT_SCHEMA_VERSION:
        mismatches.append("schema version")
    if checkpoint.get("policy_version") != config.policy_version:
        mismatches.append("policy version")
    if checkpoint.get("audit_window") != expected_window:
        mismatches.append("audit window")
    if checkpoint.get("candidate_set_hash") != candidate_hash:
        mismatches.append("candidate set/configuration hash")
    if checkpoint.get("config") != asdict(config):
        mismatches.append("chunk configuration")
    if mismatches:
        if (
            supersede_incompatible_policy
            and "policy version" in mismatches
            and checkpoint.get("status") != "completed"
        ):
            old_policy = str(checkpoint.get("policy_version") or "unknown")
            safe_policy = "".join(
                character if character.isalnum() or character in "-_" else "-"
                for character in old_policy
            ).strip("-") or "unknown"
            old_hash = str(checkpoint.get("candidate_set_hash") or "nohash")[:12]
            archive_path = checkpoint_path.with_name(
                f"{checkpoint_path.stem}.superseded-{safe_policy}-{old_hash}"
                f"{checkpoint_path.suffix}"
            )
            if archive_path.exists():
                try:
                    archived = json.loads(archive_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise AuditCheckpointMismatch(
                        f"superseded audit checkpoint archive is unreadable: {archive_path}"
                    ) from exc
                if archived != checkpoint:
                    raise AuditCheckpointMismatch(
                        f"superseded audit checkpoint archive conflicts: {archive_path}"
                    )
            else:
                atomic_write_json(archive_path, checkpoint)
            replacement = _new_checkpoint(
                start_chapter, end_chapter, candidate_hash, chunks, config
            )
            replacement["superseded_checkpoint"] = str(archive_path)
            replacement["superseded_reason"] = ", ".join(mismatches)
            replacement["attempt_records"] = [
                {
                    **dict(record),
                    "carried_from_superseded_checkpoint": str(archive_path),
                }
                for record in checkpoint.get("attempt_records", [])
                if isinstance(record, Mapping)
            ]
            atomic_write_json(checkpoint_path, replacement)
            return replacement, False
        raise AuditCheckpointMismatch(
            "stale periodic audit checkpoint rejected for "
            f"Chapters {start_chapter}-{end_chapter}: {', '.join(mismatches)}"
        )
    if not isinstance(checkpoint.get("chunks"), list):
        raise AuditCheckpointMismatch("periodic audit checkpoint has no valid chunk list")
    checkpoint.setdefault("attempt_records", [])
    return checkpoint, True


def response_is_truncated(finish_reason: Any) -> bool:
    return str(finish_reason or "").strip().lower() in {
        "length",
        "max_tokens",
        "max_output_tokens",
    }


def _resolve_request_local_candidate_ids(
    result: Mapping[str, Any],
    id_map: ModelCandidateIdMap | None,
) -> dict[str, Any]:
    if id_map is None:
        return dict(result)
    try:
        return resolve_model_candidate_response(result, id_map)
    except ModelCandidateIdError as exc:
        raise AuditResultValidationError(
            str(exc),
            code=exc.code,
            candidate_id=exc.model_id,
        ) from exc


def validate_audit_chunk_result(
    result: Any,
    candidate_ids: Sequence[str],
    all_glossary_keys: Iterable[str],
    candidate_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AuditResultValidationError(
            "audit response root must be an object", code="invalid_root"
        )
    decisions = result.get("decisions")
    issues = result.get("issues", [])
    if not isinstance(decisions, list):
        raise AuditResultValidationError(
            "audit response 'decisions' must be an array",
            code="invalid_decisions_structure",
        )
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise AuditResultValidationError(
            "audit response 'issues' must be an array of strings",
            code="invalid_issues_structure",
        )
    if len(issues) > MAX_AUDIT_ISSUES_PER_CHUNK or any(
        len(item) > MAX_AUDIT_ISSUE_CHARS for item in issues
    ):
        raise AuditResultValidationError(
            "audit response issues exceed the per-chunk size limit",
            code="issues_size_limit",
        )

    expected = set(candidate_ids)
    source_by_id = dict(candidate_sources or {})
    require_candidate_ids = candidate_sources is not None
    glossary_keys = set(all_glossary_keys)
    seen: set[str] = set()
    identified: list[tuple[str, str, dict[str, Any]]] = []

    # Identity is validated for the entire response before any field error can
    # become eligible for correction. A later injected/rewritten ID therefore
    # cannot hide behind an earlier empty action.
    for raw in decisions:
        if not isinstance(raw, dict):
            raise AuditResultValidationError(
                "every audit decision must be an object",
                code="invalid_decision_structure",
            )
        if require_candidate_ids:
            candidate_id = raw.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise AuditResultValidationError(
                    "audit decision omitted immutable candidate_id",
                    code="missing_candidate_id",
                )
            candidate_id = candidate_id.strip()
        else:
            candidate_id = raw.get("candidate_id", raw.get("chinese"))
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise AuditResultValidationError(
                    "every audit decision requires a candidate identity",
                    code="missing_candidate_id",
                )
            candidate_id = candidate_id.strip()
        if candidate_id not in expected:
            raise AuditResultValidationError(
                f"audit decision is outside this chunk: {candidate_id}",
                code="unknown_candidate_id",
                candidate_id=candidate_id,
            )
        if candidate_id in seen:
            raise AuditResultValidationError(
                f"duplicate audit decision for candidate ID: {candidate_id}",
                code="duplicate_candidate_id",
                candidate_id=candidate_id,
            )
        seen.add(candidate_id)
        source = source_by_id.get(candidate_id, candidate_id)
        echoed_source = raw.get("chinese")
        if (
            require_candidate_ids
            and echoed_source is not None
            and (not isinstance(echoed_source, str) or echoed_source.strip() != source)
        ):
            raise AuditResultValidationError(
                f"audit decision rewrote source identity for {candidate_id}",
                code="rewritten_source_key",
                candidate_id=candidate_id,
            )
        identified.append((candidate_id, source, raw))

    missing = expected - seen
    if missing:
        raise AuditResultValidationError(
            "audit response omitted candidate decisions: " + ", ".join(sorted(missing)),
            code="omitted_candidate_ids",
        )

    normalized: list[dict[str, Any]] = []
    for candidate_id, source, raw in identified:
        action = raw.get("action", "")
        if not isinstance(action, str) or action.strip().lower() not in VALID_AUDIT_ACTIONS:
            raise AuditResultValidationError(
                f"invalid audit action for {source}: {action!r}",
                code="invalid_action",
                retryable=True,
                candidate_id=candidate_id,
            )
        action = action.strip().lower()
        item = dict(raw)
        item["candidate_id"] = candidate_id
        item["chinese"] = source
        item["action"] = action
        notes = item.get("notes", "")
        if not isinstance(notes, str) or len(notes) > MAX_AUDIT_NOTES_CHARS:
            raise AuditResultValidationError(
                f"audit notes exceed the size limit for {source}",
                code="invalid_notes",
                retryable=True,
                candidate_id=candidate_id,
            )
        if action == "correct":
            english = item.get("english")
            if not isinstance(english, str) or not english.strip():
                raise AuditResultValidationError(
                    f"correct decision for {source} requires non-empty english",
                    code="missing_correct_english",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            item["english"] = english.strip()
        if action == "contextual":
            approved = item.get("approved_renderings")
            if not isinstance(approved, list) or not approved or any(
                not isinstance(value, str) or not value.strip() for value in approved
            ):
                raise AuditResultValidationError(
                    f"contextual decision for {source} requires approved_renderings",
                    code="missing_contextual_renderings",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            item["approved_renderings"] = [value.strip() for value in approved]
        if action == "merge":
            merge_with = item.get("merge_with")
            if not isinstance(merge_with, str) or not merge_with.strip():
                raise AuditResultValidationError(
                    f"merge decision for {source} requires merge_with",
                    code="missing_merge_target",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            merge_with = merge_with.strip()
            if merge_with == source or merge_with not in glossary_keys:
                raise AuditResultValidationError(
                    f"invalid merge target for {source}: {merge_with}",
                    code="invalid_merge_target",
                    retryable=True,
                    candidate_id=candidate_id,
                )
            item["merge_with"] = merge_with
        normalized.append(item)
    normalized.sort(key=lambda item: candidate_ids.index(item["candidate_id"]))
    return {"decisions": normalized, "issues": list(issues)}


_SEMANTIC_CORRECTION_FIELDS = {
    "invalid_action": {"action", "english", "approved_renderings", "merge_with"},
    "invalid_notes": {"notes"},
    "missing_correct_english": {"english"},
    "missing_contextual_renderings": {"approved_renderings"},
    "missing_merge_target": {"merge_with"},
    "invalid_merge_target": {"action", "merge_with"},
}


def audit_semantic_correction_authorization(
    errors: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    allowed_by_id: dict[str, set[str]] = {}
    for error in errors:
        candidate_id = str(error.get("candidate_id", "") or "").strip()
        code = str(error.get("code", "") or "").strip()
        allowed = _SEMANTIC_CORRECTION_FIELDS.get(code)
        if not candidate_id or not allowed or error.get("retryable") is not True:
            raise AuditResultValidationError(
                "semantic correction was requested for an unauthorized audit error",
                code="semantic_correction_scope_violation",
                candidate_id=candidate_id,
            )
        allowed_by_id.setdefault(candidate_id, set()).update(allowed)
    if not allowed_by_id:
        raise AuditResultValidationError(
            "semantic correction has no authorized audit decisions",
            code="semantic_correction_scope_violation",
        )
    return allowed_by_id


def _normalized_rendering(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def try_deterministic_glossary_semantic_normalization(
    original: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Promote one proven current rendering into a missing contextual list.

    This deliberately handles only the narrow, identity-complete shape where
    the primary audit chose ``contextual`` and repeated the exact current input
    rendering in ``english``. It never consults a corrective model response.
    """
    if len(errors) != 1:
        return None
    error = errors[0]
    candidate_id = str(error.get("candidate_id", "") or "").strip()
    if (
        error.get("code") != "missing_contextual_renderings"
        or error.get("retryable") is not True
        or not candidate_id
        or not isinstance(original, Mapping)
    ):
        return None

    decisions = original.get("decisions")
    if not isinstance(decisions, list) or not isinstance(candidates, Sequence):
        return None

    candidate_by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return None
        current_id = candidate.get("candidate_id")
        if not isinstance(current_id, str) or not current_id.strip():
            return None
        current_id = current_id.strip()
        if current_id in candidate_by_id:
            return None
        candidate_by_id[current_id] = candidate
    if candidate_id not in candidate_by_id:
        return None

    decision_by_id: dict[str, Mapping[str, Any]] = {}
    decision_order: list[str] = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            return None
        current_id = decision.get("candidate_id")
        if not isinstance(current_id, str) or not current_id.strip():
            return None
        current_id = current_id.strip()
        if current_id in decision_by_id:
            return None
        decision_by_id[current_id] = decision
        decision_order.append(current_id)
    if set(decision_by_id) != set(candidate_by_id):
        return None

    decision = decision_by_id[candidate_id]
    candidate = candidate_by_id[candidate_id]
    if decision.get("action") != "contextual":
        return None
    if "approved_renderings" in decision:
        approved = decision.get("approved_renderings")
        if not isinstance(approved, list) or approved != []:
            return None

    english = decision.get("english")
    current_english = candidate.get("english")
    if not isinstance(english, str) or not english.strip():
        return None
    if not isinstance(current_english, str) or not current_english.strip():
        return None
    if _normalized_rendering(english) != _normalized_rendering(current_english):
        return None

    candidate_source = candidate.get("chinese", candidate.get("source_key"))
    for source_field in ("chinese", "source_key"):
        if source_field not in decision:
            continue
        echoed_source = decision.get(source_field)
        if (
            not isinstance(candidate_source, str)
            or not isinstance(echoed_source, str)
            or echoed_source.strip() != candidate_source.strip()
        ):
            return None

    normalized = copy.deepcopy(dict(original))
    normalized_decisions = normalized.get("decisions")
    target_index = decision_order.index(candidate_id)
    normalized_decisions[target_index]["approved_renderings"] = [english]
    validate_semantic_correction_scope(original, normalized, errors)
    if [item.get("candidate_id") for item in normalized_decisions] != decision_order:
        return None
    return normalized, {
        "method": "deterministic_local",
        "policy": CONTEXTUAL_RENDERING_LOCAL_POLICY,
        "candidate_id": candidate_id,
        "validation_code": "missing_contextual_renderings",
        "applied_fields": ["approved_renderings"],
        "source_rendering": english,
        "api_calls": 0,
        "cost_usd": 0,
    }


def merge_audit_semantic_correction(
    original: Mapping[str, Any],
    patch: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge a sparse, field-authorized audit correction into paid output."""
    authorization = audit_semantic_correction_authorization(errors)
    if not isinstance(patch, Mapping) or set(patch) - {"decisions", "issues"}:
        raise AuditResultValidationError(
            "semantic correction must be an issues-neutral decision patch",
            code="semantic_correction_scope_violation",
        )
    if "issues" in patch and patch.get("issues") != original.get("issues", []):
        raise AuditResultValidationError(
            "semantic correction changed audit issues",
            code="semantic_correction_scope_violation",
        )
    patch_decisions = patch.get("decisions")
    original_decisions = original.get("decisions")
    if not isinstance(patch_decisions, list) or not isinstance(
        original_decisions, list
    ):
        raise AuditResultValidationError(
            "semantic correction changed the decision structure",
            code="semantic_correction_scope_violation",
        )
    patch_by_id: dict[str, Mapping[str, Any]] = {}
    for item in patch_decisions:
        if not isinstance(item, Mapping):
            raise AuditResultValidationError(
                "semantic correction decision patch must be an object",
                code="semantic_correction_scope_violation",
            )
        candidate_id = str(item.get("candidate_id", "") or "").strip()
        if candidate_id not in authorization or candidate_id in patch_by_id:
            raise AuditResultValidationError(
                "semantic correction changed candidate identity coverage",
                code="semantic_correction_scope_violation",
                candidate_id=candidate_id,
            )
        allowed_fields = authorization[candidate_id]
        supplied_fields = set(item) - {"candidate_id"}
        if not supplied_fields or not supplied_fields <= allowed_fields:
            raise AuditResultValidationError(
                "semantic correction modified fields outside its authorized scope "
                f"for {candidate_id}",
                code="semantic_correction_scope_violation",
                candidate_id=candidate_id,
            )
        patch_by_id[candidate_id] = item
    if set(patch_by_id) != set(authorization):
        raise AuditResultValidationError(
            "semantic correction omitted an invalid candidate decision",
            code="semantic_correction_scope_violation",
        )

    merged = copy.deepcopy(dict(original))
    merged_decisions = []
    applied_fields = []
    for decision in original_decisions:
        if not isinstance(decision, Mapping):
            raise AuditResultValidationError(
                "original audit decision is not an object",
                code="semantic_correction_scope_violation",
            )
        candidate_id = str(decision.get("candidate_id", "") or "").strip()
        updated = copy.deepcopy(dict(decision))
        patch_item = patch_by_id.get(candidate_id)
        if patch_item is not None:
            for field in sorted(set(patch_item) - {"candidate_id"}):
                patched_value = copy.deepcopy(patch_item[field])
                if field not in updated or updated[field] != patched_value:
                    updated[field] = patched_value
                    applied_fields.append({
                        "candidate_id": candidate_id,
                        "field": field,
                    })
        merged_decisions.append(updated)
    merged["decisions"] = merged_decisions
    if not applied_fields:
        raise AuditResultValidationError(
            "semantic correction supplied no effective authorized field change",
            code="semantic_correction_scope_violation",
        )
    validate_semantic_correction_scope(original, merged, errors)
    return merged, {
        "authorization": {
            candidate_id: sorted(fields)
            for candidate_id, fields in sorted(authorization.items())
        },
        "applied_fields": applied_fields,
    }


def validate_semantic_correction_scope(
    original: Mapping[str, Any],
    corrected: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
) -> None:
    """Ensure a bounded correction did not revise already-valid decisions."""
    allowed_by_id: dict[str, set[str]] = {}
    for error in errors:
        candidate_id = str(error.get("candidate_id", "") or "").strip()
        code = str(error.get("code", "") or "").strip()
        allowed = _SEMANTIC_CORRECTION_FIELDS.get(code)
        if candidate_id and allowed:
            allowed_by_id.setdefault(candidate_id, set()).update(allowed)

    original_decisions = original.get("decisions")
    corrected_decisions = corrected.get("decisions")
    if not isinstance(original_decisions, list) or not isinstance(
        corrected_decisions, list
    ):
        raise AuditResultValidationError(
            "semantic correction changed the decision structure",
            code="semantic_correction_scope_violation",
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
        raise AuditResultValidationError(
            "semantic correction changed candidate identity coverage",
            code="semantic_correction_scope_violation",
        )
    if original.get("issues", []) != corrected.get("issues", []):
        raise AuditResultValidationError(
            "semantic correction changed audit issues",
            code="semantic_correction_scope_violation",
        )

    for candidate_id, before in original_by_id.items():
        after = corrected_by_id[candidate_id]
        allowed_fields = allowed_by_id.get(candidate_id, set())
        comparable_before = {
            key: value for key, value in before.items() if key not in allowed_fields
        }
        comparable_after = {
            key: value for key, value in after.items() if key not in allowed_fields
        }
        if comparable_before != comparable_after:
            raise AuditResultValidationError(
                "semantic correction modified fields outside its authorized scope "
                f"for {candidate_id}",
                code="semantic_correction_scope_violation",
                candidate_id=candidate_id,
            )


def combine_audit_chunk_results(
    chunks: Sequence[Mapping[str, Any]],
    ordered_candidate_ids: Sequence[str],
    all_glossary_keys: Iterable[str],
) -> dict[str, Any]:
    decisions_by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for chunk in chunks:
        if chunk.get("status") != "completed":
            continue
        result = chunk.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"completed chunk {chunk.get('chunk_id')} has no result")
        issues.extend(result.get("issues", []))
        for decision in result.get("decisions", []):
            candidate_id = decision.get("candidate_id", decision["chinese"])
            if candidate_id in decisions_by_id:
                raise ValueError(
                    f"conflicting duplicate audit decisions for {candidate_id}"
                )
            decisions_by_id[candidate_id] = decision

    missing = set(ordered_candidate_ids) - set(decisions_by_id)
    extras = set(decisions_by_id) - set(ordered_candidate_ids)
    if missing or extras:
        details = []
        if missing:
            details.append("missing=" + ", ".join(sorted(missing)))
        if extras:
            details.append("unexpected=" + ", ".join(sorted(extras)))
        raise ValueError("combined audit decision coverage mismatch: " + "; ".join(details))

    glossary_keys = set(all_glossary_keys)
    merges = {
        decision["chinese"]: decision["merge_with"]
        for decision in decisions_by_id.values()
        if decision.get("action") == "merge"
    }
    for source, target in merges.items():
        if target not in glossary_keys:
            raise ValueError(f"invalid merge target for {source}: {target}")
        target_decision = next(
            (
                decision
                for decision in decisions_by_id.values()
                if decision["chinese"] == target
            ),
            None,
        )
        if target_decision and target_decision.get("action") == "reject":
            raise ValueError(f"merge target {target} is rejected by another chunk")
        visited = {source}
        cursor = target
        while cursor in merges:
            if cursor in visited:
                raise ValueError(f"contradictory merge cycle involving {source}")
            visited.add(cursor)
            cursor = merges[cursor]

    return {
        "decisions": [decisions_by_id[candidate_id] for candidate_id in ordered_candidate_ids],
        "issues": issues,
    }


def _combine_attempt_stats(attempt_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {field: 0 for field in STAT_TOTAL_FIELDS}
    for record in attempt_records:
        stats = record.get("stats") or {}
        for field in STAT_TOTAL_FIELDS:
            totals[field] += stats.get(field, 0) or 0
    totals["api_request_count"] = len(attempt_records)
    return totals


def _attempt_result_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _audit_attempt_operation_id(
    candidate_hash: str, chunk_id: str, attempt: int, kind: str
) -> str:
    return (
        f"periodic_glossary_audit_chunk:{candidate_hash}:{chunk_id}:"
        f"{kind}:{int(attempt)}"
    )


def _chunk_attempt_records(
    checkpoint: Mapping[str, Any], chunk_id: str
) -> list[Mapping[str, Any]]:
    return [
        record for record in checkpoint.get("attempt_records", [])
        if (
            isinstance(record, Mapping)
            and record.get("chunk_id") == chunk_id
            and "carried_from_superseded_checkpoint" not in record
        )
    ]


def classify_audit_chunk_recovery(
    checkpoint: Mapping[str, Any], chunk_id: str
) -> dict[str, Any]:
    """Classify durable restart evidence without mutating the checkpoint."""
    chunk = next(
        (
            item for item in checkpoint.get("chunks", [])
            if isinstance(item, Mapping) and item.get("chunk_id") == chunk_id
        ),
        None,
    )
    if chunk is None:
        raise ValueError(f"unknown audit chunk: {chunk_id}")
    if chunk.get("status") == "completed" and isinstance(chunk.get("result"), Mapping):
        return {"status": "completed", "fresh_request_required": False}
    records = _chunk_attempt_records(checkpoint, chunk_id)
    for record in reversed(records):
        if "parsed_result" in record:
            return {
                "status": "parsed_response_recorded",
                "kind": record.get("kind", "audit"),
                "lifecycle": record.get("lifecycle", "response_recorded"),
                "fresh_request_required": False,
            }
        if isinstance(record.get("raw_content"), str):
            return {
                "status": "raw_response_recorded",
                "kind": record.get("kind", "audit"),
                "lifecycle": record.get("lifecycle", "response_received"),
                "fresh_request_required": False,
            }
    if chunk.get("status") == "in_flight":
        return {
            "status": "remote_outcome_uncertain",
            "fresh_request_required": False,
        }
    if chunk.get("status") in {"failed", "validation_failed"} and records:
        return {
            "status": "historical_parsed_response_unrecoverable",
            "fresh_request_required": True,
            "reason": (
                "attempt stats/validation failure survived but the historical parsed "
                "response did not"
            ),
        }
    return {
        "status": "pending",
        "fresh_request_required": True,
    }


def _split_chunk(chunk: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_ids = list(chunk["candidate_ids"])
    family_keys = list(chunk.get("family_keys") or candidate_ids)
    midpoint = len(candidate_ids) // 2
    boundaries = [
        index
        for index in range(1, len(candidate_ids))
        if family_keys[index - 1] != family_keys[index]
    ]
    if boundaries:
        midpoint = min(boundaries, key=lambda value: (abs(value - midpoint), value))

    def child(suffix: str, start: int, end: int) -> dict[str, Any]:
        return {
            "chunk_id": f"{chunk['chunk_id']}.{suffix}",
            "candidate_ids": candidate_ids[start:end],
            "family_keys": family_keys[start:end],
            "depth": int(chunk.get("depth", 0)) + 1,
            "status": "pending",
            "attempts": 0,
            "parent_chunk_id": chunk["chunk_id"],
        }

    return child("1", 0, midpoint), child("2", midpoint, len(candidate_ids))


def _leaf_chunks(chunks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [chunk for chunk in chunks if chunk.get("status") != "split"]


def _chunk_failure_message(
    checkpoint: Mapping[str, Any], chunk: Mapping[str, Any], reason: str
) -> str:
    leaves = _leaf_chunks(checkpoint["chunks"])
    completed = [item["chunk_id"] for item in leaves if item.get("status") == "completed"]
    next_id = chunk.get("chunk_id")
    candidates = ", ".join(chunk.get("candidate_ids", []))
    window = checkpoint["audit_window"]
    return (
        f"Periodic glossary audit stopped safely for Chapters "
        f"{window['start_chapter']}-{window['end_chapter']}. "
        f"Failed chunk {next_id} of {len(leaves)}; candidates: {candidates}. "
        f"Completed chunks: {completed or 'none'}. Reason: {reason}"
    )


def run_chunked_audit_skipping_failures(
    *,
    candidates: Sequence[Mapping[str, Any]],
    checkpoint_path: Path,
    run_audit: Callable[..., dict[str, Any]] | None = None,
    on_skip: Callable[[str, int], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an audit, keeping the terms of any chunk that can't produce a valid result.

    A failed chunk changes nothing in the glossary, so the audit can finish.
    Provider outages and budget stops still propagate and resume later.
    """
    from provider_retry_policy import is_run_level_stop

    by_id = {_candidate_id(candidate): candidate for candidate in candidates}
    while True:
        try:
            return (run_audit or run_chunked_audit)(candidates=candidates, checkpoint_path=checkpoint_path, **kwargs)
        except AuditChunkFailure as exc:
            if is_run_level_stop(exc):
                raise
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
            failed = next((chunk for chunk in checkpoint["chunks"]
                           if chunk.get("status") not in {"completed", "split", "pending"}), None)
            if failed is None:
                raise
            failed["result"] = {"issues": [], "decisions": [
                {**({"candidate_id": candidate_id} if "candidate_id" in by_id[candidate_id] else {}),
                 "chinese": _candidate_source(by_id[candidate_id]), "action": "keep",
                 "english": by_id[candidate_id].get("english", ""), "notes": "Kept: audit chunk failed."}
                for candidate_id in failed["candidate_ids"]]}
            failed["status"] = "completed"
            failed["skipped_after_failure"] = True
            for record in checkpoint["attempt_records"]:
                if record.get("chunk_id") == failed["chunk_id"] and record.get("lifecycle") in {
                        "prepared", "in_flight", "remote_outcome_uncertain"}:
                    record["lifecycle"] = "abandoned"
            atomic_write_json(checkpoint_path, checkpoint)
            if on_skip is not None:
                on_skip(failed["chunk_id"], len(failed["candidate_ids"]))


def run_chunked_audit(
    *,
    candidates: Sequence[Mapping[str, Any]],
    all_glossary_keys: Iterable[str],
    start_chapter: int,
    end_chapter: int,
    checkpoint_path: Path,
    request_chunk: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]],
    repair_chunk: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    correct_semantic_chunk: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]], Mapping[str, Any]],
        Mapping[str, Any],
    ] | None = None,
    config: AuditChunkConfig | None = None,
    context_fingerprint: str = "",
    supersede_incompatible_policy: bool = False,
    glossary_entries: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run or resume an audit, persisting each validated candidate chunk."""
    config = config or AuditChunkConfig()
    config.validate()
    all_glossary_keys = tuple(all_glossary_keys)
    ordered_candidates = sorted(candidates, key=_candidate_source)
    candidate_by_id = {_candidate_id(item): item for item in ordered_candidates}
    if len(candidate_by_id) != len(ordered_candidates):
        raise ValueError("audit candidates must have unique candidate IDs")
    ordered_ids = list(candidate_by_id)
    candidate_sources = {
        candidate_id: _candidate_source(candidate)
        for candidate_id, candidate in candidate_by_id.items()
    }
    family_plan = resolve_audit_candidate_families(
        ordered_candidates,
        all_glossary_keys=all_glossary_keys,
        glossary_entries=glossary_entries,
    )
    family_by_candidate_id = family_plan["members_by_candidate_id"]
    explicit_candidate_identity = any(
        "candidate_id" in candidate for candidate in ordered_candidates
    )
    validation_sources = candidate_sources if explicit_candidate_identity else None
    candidate_hash = audit_candidate_set_hash(
        ordered_candidates, config, context_fingerprint
    )
    initial_chunks = build_audit_chunks(
        ordered_candidates,
        config,
        all_glossary_keys=all_glossary_keys,
        glossary_entries=glossary_entries,
    )
    checkpoint_path = Path(checkpoint_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint, resumed = _load_or_create_checkpoint(
        checkpoint_path,
        start_chapter,
        end_chapter,
        candidate_hash,
        initial_chunks,
        config,
        supersede_incompatible_policy,
    )

    unresolved = [record for record in checkpoint["attempt_records"]
        if "carried_from_superseded_checkpoint" not in record
        and "parsed_result" not in record
        and not isinstance(record.get("raw_content"), str)
        and record.get("lifecycle") in {"prepared", "in_flight", "remote_outcome_uncertain"}]
    if len(unresolved) > 1:
        raise AuditCheckpointMismatch("multiple unresolved periodic-audit paid attempts cannot be reconciled")
    for record in unresolved:
        matches = [chunk for chunk in checkpoint["chunks"] if chunk.get("chunk_id") == record.get("chunk_id")]
        same_kind = [item for item in checkpoint["attempt_records"]
            if "carried_from_superseded_checkpoint" not in item
            and item.get("chunk_id") == record.get("chunk_id") and item.get("kind") == record.get("kind")]
        if (record.get("kind") not in {"audit", "json_repair", "semantic_correction"}
            or len(matches) != 1 or matches[0].get("status") in {"completed", "split"}
            or same_kind[-1] is not record or checkpoint.get("status") == "completed"):
            raise AuditCheckpointMismatch("unfinished audit attempt has impossible kind/chunk/lifecycle ownership")

    if checkpoint.get("status") == "completed":
        combined = combine_audit_chunk_results(
            checkpoint["chunks"], ordered_ids, all_glossary_keys
        )
        return {
            "audit_result": combined,
            "stats": _combine_attempt_stats(checkpoint["attempt_records"]),
            "candidate_set_hash": candidate_hash,
            "checkpoint": checkpoint,
            "resumed": True,
        }

    def persist_checkpoint() -> None:
        atomic_write_json(checkpoint_path, checkpoint)

    def latest_record(chunk_id: str, kind: str) -> dict[str, Any] | None:
        for record in reversed(checkpoint["attempt_records"]):
            if (
                record.get("chunk_id") == chunk_id
                and record.get("kind") == kind
                and "carried_from_superseded_checkpoint" not in record
            ):
                return record
        return None

    from auxiliary_response_reconciliation import AuxiliaryReconciliation, audit_response_fields

    def request_args(record, args):
        metadata = copy.deepcopy(record.get("request_metadata", args[-1]))
        metadata["outer_attempt"] = record
        return (*args[:-1], metadata)

    def verify_carried_response(record, callback, args):
        # An experiment may require additional provenance for an unparsed outer
        # response. Verification is read-only; the outer bytes stay authoritative.
        verify = getattr(callback, "verify_recorded", None)
        if "parsed_result" not in record and isinstance(record.get("raw_content"), str) and callable(verify):
            verify(record, request_args(record, args))

    def recover_attempt(pending, record, callback, args, expected_operation_id, expected_attempt):
        if "parsed_result" in record or isinstance(record.get("raw_content"), str):
            return
        if (record.get("operation_id") != expected_operation_id
            or record.get("attempt") != expected_attempt
            or record.get("chunk_id") != pending["chunk_id"]
            or record.get("model_id_transport_version") != args[-1]["model_id_transport_version"]):
            raise AuditCheckpointMismatch("unfinished audit attempt identity mismatch")
        recover = getattr(callback, "recover", None)
        if not callable(recover):
            raise AuditChunkFailure("audit remote outcome is uncertain; automatic reissue is blocked (no journal reconciliation capability)")
        bound_args = request_args(record, args)
        outcome = recover(record, bound_args)
        if not isinstance(outcome, AuxiliaryReconciliation) or outcome.binding != record.get("transport_binding"):
            raise AuditCheckpointMismatch("audit reconciliation did not prove the exact saved transport binding")
        if outcome.status == "response_recorded":
            record["paid_journal_recovered"] = True
            record_response(pending, record, outcome.response)
        elif outcome.status == "pre_transport" and outcome.response is None:
            # Continue the existing durable attempt. Never append or increment.
            record["lifecycle"] = "in_flight"
            pending["status"] = "in_flight"
            persist_checkpoint()
            record_response(pending, record, callback(*bound_args))
        else:
            raise AuditCheckpointMismatch("unsupported audit reconciliation outcome")


    def begin_attempt(
        pending: dict[str, Any], metadata: Mapping[str, Any], kind: str,
        attempt: int, operation_id: str, callback, args,
    ) -> dict[str, Any]:
        record = {
            "chunk_id": pending["chunk_id"],
            "kind": kind,
            "attempt": int(attempt),
            "operation_id": operation_id,
            "lifecycle_version": AUDIT_ATTEMPT_LIFECYCLE_VERSION,
            "lifecycle": "prepared",
            "model_id_transport_version": metadata["model_id_transport_version"],
        }
        record["request_metadata"] = copy.deepcopy(dict(metadata))
        record["request_metadata"]["candidate_set_hash"] = candidate_hash
        prepare = getattr(callback, "prepare", None)
        if callable(prepare):
            record["transport_binding"] = prepare(record, request_args(record, args))
        checkpoint["attempt_records"].append(record)
        persist_checkpoint()
        record["lifecycle"] = "in_flight"
        pending["status"] = "in_flight"
        persist_checkpoint()
        return record

    def record_response(
        pending: dict[str, Any], record: dict[str, Any], response: Mapping[str, Any]
    ) -> None:
        record.update(audit_response_fields(response))
        pending["status"] = "response_received"
        persist_checkpoint()

    def parse_recorded_response(
        pending: dict[str, Any], record: dict[str, Any], label: str
    ) -> tuple[Any | None, str | None, json.JSONDecodeError | None]:
        if "parsed_result" in record:
            parsed_value = copy.deepcopy(record["parsed_result"])
            expected_hash = str(record.get("result_hash", "") or "")
            if not expected_hash or expected_hash != _attempt_result_hash(parsed_value):
                raise AuditCheckpointMismatch(
                    f"saved {label} parsed-result identity mismatch"
                )
            return parsed_value, None, None
        recorded_content = record.get("raw_content")
        if not isinstance(recorded_content, str):
            return None, None, None
        if hashlib.sha256(recorded_content.encode("utf-8")).hexdigest() != record.get("raw_content_hash"):
            raise AuditCheckpointMismatch(f"saved {label} raw-response identity mismatch")
        finish_reason = record.get("finish_reason")
        if response_is_truncated(finish_reason):
            return None, (
                f"truncated {label} response (finish_reason={finish_reason})"
            ), None
        raw_content = clean_json_text(recorded_content)
        try:
            parsed_value = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            record["lifecycle"] = "validation_failed"
            record["validation_errors"] = [{
                "code": "malformed_json",
                "message": str(exc),
                "retryable": False,
            }]
            pending["status"] = "validation_failed"
            persist_checkpoint()
            return None, None, exc
        record["parsed_result"] = copy.deepcopy(parsed_value)
        record["result_hash"] = _attempt_result_hash(parsed_value)
        record["lifecycle"] = "response_recorded"
        pending["status"] = "response_recorded"
        persist_checkpoint()
        return parsed_value, None, None

    while True:
        pending = next(
            (
                item for item in checkpoint["chunks"]
                if item.get("status") in {
                    "pending", "failed", "validation_failed",
                    "response_received", "response_recorded", "in_flight", "remote_outcome_uncertain",
                }
            ),
            None,
        )
        if pending is None:
            terminal = next(
                (
                    item for item in checkpoint["chunks"]
                    if item.get("status") in {
                        "terminal_failed", "in_flight", "remote_outcome_uncertain"
                    }
                ),
                None,
            )
            if terminal:
                if terminal.get("status") == "in_flight":
                    terminal["status"] = "remote_outcome_uncertain"
                    terminal["last_error"] = (
                        "audit remote outcome is uncertain; no durable response exists "
                        "and automatic reissue is blocked"
                    )
                    persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(
                        checkpoint,
                        terminal,
                        terminal.get("last_error", "terminal failure"),
                    )
                )
            combined = combine_audit_chunk_results(
                checkpoint["chunks"], ordered_ids, all_glossary_keys
            )
            checkpoint["status"] = "completed"
            checkpoint["combined_result"] = combined
            persist_checkpoint()
            return {
                "audit_result": combined,
                "stats": _combine_attempt_stats(checkpoint["attempt_records"]),
                "candidate_set_hash": candidate_hash,
                "checkpoint": checkpoint,
                "resumed": resumed,
            }

        chunk_candidates = [
            candidate_by_id[candidate_id] for candidate_id in pending["candidate_ids"]
        ]
        model_id_map = (
            build_model_candidate_id_map(pending["candidate_ids"])
            if explicit_candidate_identity else None
        )
        request_candidates = (
            model_facing_candidates(chunk_candidates, model_id_map)
            if model_id_map is not None
            else [copy.deepcopy(dict(item)) for item in chunk_candidates]
        )
        model_family_context = []
        for internal_id, request_candidate in zip(
            pending["candidate_ids"], request_candidates
        ):
            context = copy.deepcopy(family_by_candidate_id[internal_id])
            if model_id_map is not None:
                context["candidate_id"] = model_id_map.internal_to_alias[internal_id]
            request_candidate.update({
                "source_key": context["source_key"],
                "family_id": context["family_id"],
                "family_root": context["family_root"],
                "alias_of": context["alias_of"],
            })
            model_family_context.append(context)
        metadata = {
            "chunk_id": pending["chunk_id"],
            "chunk_index": _leaf_chunks(checkpoint["chunks"]).index(pending) + 1,
            "total_chunks": len(_leaf_chunks(checkpoint["chunks"])),
            "audit_window": checkpoint["audit_window"],
            "candidate_ids": list(pending["candidate_ids"]),
            "model_candidate_ids": (
                list(model_id_map.alias_to_internal) if model_id_map else []
            ),
            "model_id_transport_version": (
                MODEL_CANDIDATE_ID_TRANSPORT_VERSION if model_id_map else "legacy"
            ),
            "family_policy": AUDIT_FAMILY_POLICY_VERSION,
            "family_context": model_family_context,
        }

        audit_record = latest_record(pending["chunk_id"], "audit")
        if (
            audit_record is not None
            and response_is_truncated(audit_record.get("finish_reason"))
            and pending.get("status") == "failed"
            and len(pending["candidate_ids"]) == 1
            and pending.get("attempts", 0) < config.max_min_chunk_failures
        ):
            audit_record = None
        if audit_record is not None and not (
            "parsed_result" in audit_record
            or isinstance(audit_record.get("raw_content"), str)
        ):
            lifecycle = str(audit_record.get("lifecycle", "") or "")
            if lifecycle in {"prepared", "in_flight", "remote_outcome_uncertain"}:
                recover_attempt(pending, audit_record, request_chunk,
                    (request_candidates, metadata),
                    _audit_attempt_operation_id(candidate_hash, pending["chunk_id"], pending["attempts"], "audit"),
                    pending["attempts"])
            classification = classify_audit_chunk_recovery(
                checkpoint, pending["chunk_id"]
            )
            if classification["status"] == "historical_parsed_response_unrecoverable":
                pending["recovery_classification"] = classification
                persist_checkpoint()
                audit_record = None

        if audit_record is None:
            if (
                len(pending["candidate_ids"]) == 1
                and pending.get("attempts", 0) >= config.max_min_chunk_failures
            ):
                pending["status"] = "terminal_failed"
                pending.setdefault("last_error", "minimum chunk retry limit reached")
                persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(checkpoint, pending, pending["last_error"])
                )
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            operation_id = _audit_attempt_operation_id(
                candidate_hash, pending["chunk_id"], pending["attempts"], "audit"
            )
            audit_record = begin_attempt(
                pending, metadata, "audit", pending["attempts"], operation_id,
                request_chunk, (request_candidates, metadata),
            )
            try:
                response = dict(request_chunk(*request_args(audit_record, (request_candidates, metadata))))
            except Exception as exc:
                audit_record["lifecycle"] = "remote_outcome_uncertain"
                audit_record["request_error"] = f"{type(exc).__name__}: {exc}"
                pending["status"] = "remote_outcome_uncertain"
                pending["last_error"] = (
                    "audit API/network failure left the remote outcome uncertain: "
                    f"{type(exc).__name__}: {exc}"
                )
                persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(checkpoint, pending, pending["last_error"])
                ) from exc
            record_response(pending, audit_record, response)

        verify_carried_response(audit_record, request_chunk, (request_candidates, metadata))
        parsed, split_reason, parse_error = parse_recorded_response(
            pending, audit_record, "audit"
        )
        base_record = audit_record
        if parse_error is not None:
            repair_record = latest_record(pending["chunk_id"], "json_repair")
            if repair_record is None and repair_chunk is not None:
                repair_operation_id = _audit_attempt_operation_id(
                    candidate_hash,
                    pending["chunk_id"],
                    pending["attempts"],
                    "json_repair",
                )
                repair_record = begin_attempt(
                    pending,
                    metadata,
                    "json_repair",
                    pending["attempts"],
                    repair_operation_id, repair_chunk,
                    (str(audit_record.get("raw_content", "")), metadata),
                )
                try:
                    repair_response = dict(
                        repair_chunk(*request_args(repair_record, (str(audit_record.get("raw_content", "")), metadata)))
                    )
                except Exception as exc:
                    repair_record["lifecycle"] = "remote_outcome_uncertain"
                    repair_record["request_error"] = f"{type(exc).__name__}: {exc}"
                    pending["status"] = "remote_outcome_uncertain"
                    pending["last_error"] = (
                        "per-chunk JSON repair remote outcome is uncertain: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    persist_checkpoint()
                    raise AuditChunkFailure(
                        _chunk_failure_message(checkpoint, pending, pending["last_error"])
                    ) from exc
                record_response(pending, repair_record, repair_response)
            if repair_record is None:
                pending["status"] = "failed"
                pending["last_error"] = f"malformed audit JSON: {parse_error}"
                persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(checkpoint, pending, pending["last_error"])
                ) from parse_error
            recover_attempt(pending, repair_record, repair_chunk,
                (str(audit_record.get("raw_content", "")), metadata),
                _audit_attempt_operation_id(candidate_hash, pending["chunk_id"], pending["attempts"], "json_repair"),
                pending["attempts"])
            verify_carried_response(repair_record, repair_chunk,
                (str(audit_record.get("raw_content", "")), metadata))
            parsed, split_reason, repair_error = parse_recorded_response(
                pending, repair_record, "per-chunk JSON repair"
            )
            base_record = repair_record
            if repair_error is not None:
                pending["status"] = "failed"
                pending["last_error"] = (
                    "per-chunk JSON repair failed validation: " + str(repair_error)
                )
                persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(checkpoint, pending, pending["last_error"])
                ) from repair_error

        if split_reason:
            pending["last_error"] = split_reason
            if (
                len(pending["candidate_ids"]) > 1
                and int(pending.get("depth", 0)) < config.max_split_depth
            ):
                first, second = _split_chunk(pending)
                pending["status"] = "split"
                position = checkpoint["chunks"].index(pending) + 1
                checkpoint["chunks"][position:position] = [first, second]
                persist_checkpoint()
                continue
            pending["status"] = (
                "terminal_failed"
                if pending.get("attempts", 0) >= config.max_min_chunk_failures
                else "failed"
            )
            persist_checkpoint()
            raise AuditChunkFailure(
                _chunk_failure_message(checkpoint, pending, split_reason)
            )

        if parsed is None:
            raise AuditCheckpointMismatch(
                f"audit chunk {pending['chunk_id']} has no durable response content"
            )

        try:
            resolved_parsed = _resolve_request_local_candidate_ids(parsed, model_id_map)
            result = validate_audit_chunk_result(
                resolved_parsed,
                pending["candidate_ids"],
                all_glossary_keys,
                validation_sources,
            )
        except AuditResultValidationError as validation_error:
            base_record["lifecycle"] = "validation_failed"
            base_record["validation_errors"] = [validation_error.as_dict()]
            pending["status"] = "validation_failed"
            pending["last_error"] = (
                "audit schema/semantic validation failed: " + str(validation_error)
            )
            persist_checkpoint()
            model_errors = [validation_error.as_dict()]
            if model_id_map is not None:
                internal_id = model_errors[0].get("candidate_id", "")
                model_errors[0]["candidate_id"] = model_id_map.internal_to_alias.get(
                    internal_id, internal_id
                )
            local_normalization = try_deterministic_glossary_semantic_normalization(
                parsed, model_errors, request_candidates
            )
            if local_normalization is not None:
                normalized_model_result, normalization_audit = local_normalization
                try:
                    resolved_normalized = _resolve_request_local_candidate_ids(
                        normalized_model_result, model_id_map
                    )
                    result = validate_audit_chunk_result(
                        resolved_normalized,
                        pending["candidate_ids"],
                        all_glossary_keys,
                        validation_sources,
                    )
                except AuditResultValidationError:
                    local_normalization = None
                else:
                    normalization_audit.update({
                        "candidate_id": validation_error.candidate_id,
                        "model_candidate_id": model_errors[0]["candidate_id"],
                        "effective_result_hash": _attempt_result_hash(result),
                    })
                    pending["deterministic_normalization"] = normalization_audit

            if local_normalization is None:
                can_correct = (
                    validation_error.retryable and correct_semantic_chunk is not None
                )
                if not can_correct:
                    pending["status"] = "failed"
                    persist_checkpoint()
                    raise AuditChunkFailure(
                        _chunk_failure_message(
                            checkpoint, pending, pending["last_error"]
                        )
                    ) from validation_error

            if local_normalization is not None:
                pending["result"] = result
                pending["status"] = "completed"
                pending.pop("last_error", None)
                persist_checkpoint()
                continue

            authorization = audit_semantic_correction_authorization(model_errors)
            parsed_for_correction = copy.deepcopy(parsed)
            if model_id_map is None:
                for decision in parsed_for_correction.get("decisions", []):
                    if isinstance(decision, dict) and "candidate_id" not in decision:
                        decision["candidate_id"] = decision.get("chinese")
            invalid_ids = set(authorization)
            correction_input = {
                "decisions": [
                    copy.deepcopy(decision)
                    for decision in parsed_for_correction.get("decisions", [])
                    if isinstance(decision, Mapping)
                    and str(decision.get("candidate_id", "") or "").strip()
                    in invalid_ids
                ],
                "issues": copy.deepcopy(parsed_for_correction.get("issues", [])),
            }
            correction_operation_id = (
                "periodic_glossary_audit_semantic_correction:"
                f"{candidate_hash}:{pending['chunk_id']}:semantic-fields-v1"
            )
            correction_metadata = dict(metadata)
            correction_metadata.update({
                "operation_id": correction_operation_id,
                "candidate_sources": {
                    candidate_id: next(
                        context["source_key"] for context in model_family_context
                        if context["candidate_id"] == candidate_id
                    )
                    for candidate_id in sorted(invalid_ids)
                },
                "family_context": [
                    context for context in model_family_context
                    if context["candidate_id"] in invalid_ids
                ],
                "authorized_fields": {
                    candidate_id: sorted(fields)
                    for candidate_id, fields in sorted(authorization.items())
                },
                "allowed_merge_targets": {
                    candidate_id: sorted(
                        key for key in all_glossary_keys
                        if key != next(
                            context["source_key"] for context in model_family_context
                            if context["candidate_id"] == candidate_id
                        )
                    )
                    for candidate_id in sorted(invalid_ids)
                },
            })
            correction_record = latest_record(
                pending["chunk_id"], "semantic_correction"
            )
            if correction_record is not None and not (
                "parsed_result" in correction_record
                or isinstance(correction_record.get("raw_content"), str)
            ):
                recover_attempt(pending, correction_record, correct_semantic_chunk,
                    (correction_input, model_errors, correction_metadata), correction_operation_id, 1)
            if correction_record is None:
                pending["semantic_correction_attempted"] = True
                pending["semantic_correction_operation_id"] = correction_operation_id
                correction_record = begin_attempt(
                    pending,
                    correction_metadata,
                    "semantic_correction",
                    1,
                    correction_operation_id, correct_semantic_chunk,
                    (correction_input, model_errors, correction_metadata),
                )
                correction_record["base_result_hash"] = _attempt_result_hash(
                    parsed_for_correction
                )
                try:
                    correction_response = dict(
                        correct_semantic_chunk(
                            *request_args(correction_record, (correction_input, model_errors, correction_metadata))
                        )
                    )
                except Exception as exc:
                    correction_record["lifecycle"] = "remote_outcome_uncertain"
                    correction_record["request_error"] = f"{type(exc).__name__}: {exc}"
                    pending["status"] = "remote_outcome_uncertain"
                    pending["last_error"] = (
                        "per-chunk semantic correction remote outcome is uncertain: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    persist_checkpoint()
                    raise AuditChunkFailure(
                        _chunk_failure_message(checkpoint, pending, pending["last_error"])
                    ) from exc
                record_response(pending, correction_record, correction_response)
            verify_carried_response(correction_record, correct_semantic_chunk,
                (correction_input, model_errors, correction_metadata))
            corrected_patch, correction_split, correction_parse_error = (
                parse_recorded_response(
                    pending, correction_record, "per-chunk semantic correction"
                )
            )
            try:
                if correction_split:
                    raise AuditResultValidationError(
                        correction_split, code="semantic_correction_truncated"
                    )
                if correction_parse_error is not None:
                    raise AuditResultValidationError(
                        str(correction_parse_error),
                        code="semantic_correction_malformed_json",
                    )
                effective_model_result, merge_audit = merge_audit_semantic_correction(
                    parsed_for_correction, corrected_patch, model_errors
                )
                resolved_corrected = _resolve_request_local_candidate_ids(
                    effective_model_result, model_id_map
                )
                result = validate_audit_chunk_result(
                    resolved_corrected,
                    pending["candidate_ids"],
                    all_glossary_keys,
                    validation_sources,
                )
            except AuditResultValidationError as correction_error:
                correction_record["lifecycle"] = "validation_failed"
                correction_record["validation_errors"] = [
                    correction_error.as_dict()
                ]
                pending["status"] = "failed"
                pending["last_error"] = (
                    "per-chunk semantic correction failed validation: "
                    + str(correction_error)
                )
                persist_checkpoint()
                raise AuditChunkFailure(
                    _chunk_failure_message(checkpoint, pending, pending["last_error"])
                ) from correction_error
            correction_record["lifecycle"] = "validated"
            correction_record["effective_result_hash"] = _attempt_result_hash(
                result
            )
            correction_record["merge_audit"] = merge_audit
            base_record["lifecycle"] = "validation_failed_corrected"
        else:
            base_record["lifecycle"] = "validated"

        pending["result"] = result
        pending["status"] = "completed"
        pending.pop("last_error", None)
        persist_checkpoint()
