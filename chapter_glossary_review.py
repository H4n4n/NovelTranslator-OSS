"""Restart-safe bounded recovery for chapter-level glossary review."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from glossary_integrity import (
    GlossaryCandidateIdentityError,
    GlossaryReviewSchemaError,
    GlossaryReviewValidationError,
    GlossarySourceIdentityError,
    canonicalize_glossary_review_candidates,
    collect_glossary_review_decision_validation_errors,
    glossary_review_semantic_correction_authorization,
    merge_glossary_review_semantic_correction,
    normalize_glossary_review_issues_shape,
    stable_glossary_candidate_id,
    validate_glossary_review_result,
    validate_glossary_review_schema,
    validate_glossary_review_schema_correction_scope,
)
from production_failure_classification import (
    LocalPersistenceError,
    NonProviderProductionError,
)
from model_candidate_ids import (
    MODEL_CANDIDATE_ID_TRANSPORT_VERSION,
    ModelCandidateIdError,
    build_model_candidate_id_map,
    model_facing_candidates,
    resolve_model_candidate_response,
)


CHAPTER_REVIEW_RECOVERY_KEY = "chapter_glossary_review_recovery"
CHAPTER_REVIEW_POLICY_VERSION = "semantic-fields-v1"
CHAPTER_REVIEW_OPERATION_VERSION = "review-v1"
CHAPTER_REVIEW_CORRECTION_VERSION = "semantic-fields-v1"
CHAPTER_REVIEW_SCHEMA_CORRECTION_VERSION = "issues-shape-v1"
CHAPTER_REVIEW_LOCAL_ISSUES_POLICY = "issues-shape-local-v1"

STAT_TOTAL_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "rate_correct_cost_usd",
)


class ChapterGlossaryReviewFailure(NonProviderProductionError):
    """A chapter review could not be validated or safely corrected."""

    failure_kind = "local_safety"


class ChapterGlossaryReviewPersistenceError(LocalPersistenceError):
    """Durable chapter-review recovery metadata could not be persisted."""


class ChapterGlossaryCandidateIdentityError(ChapterGlossaryReviewFailure):
    """Candidate preparation found a known fail-closed identity conflict."""

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


def chapter_review_merge_targets(
    relevant_entries: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Index the canonical ordered relevant-entry records by source key."""
    targets: dict[str, dict[str, str]] = {}
    for entry in relevant_entries:
        if not isinstance(entry, Mapping):
            raise ChapterGlossaryReviewFailure(
                "relevant glossary entries must be ordered entry objects"
            )
        source_key = str(entry.get("chinese", "") or "").strip()
        if not source_key:
            raise ChapterGlossaryReviewFailure(
                "relevant glossary entry lacks an authoritative source key"
            )
        if source_key in targets:
            raise ChapterGlossaryReviewFailure(
                f"duplicate relevant glossary source key: {source_key}"
            )
        targets[source_key] = {
            "english": str(entry.get("english", "") or ""),
            "status": str(entry.get("status", "") or ""),
        }
    return targets


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _recorded_result_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _recorded_validation_errors_match(saved, current) -> bool:
    """Match historical errors while allowing new local diagnostic enrichment."""
    if not isinstance(saved, list) or len(saved) != len(current):
        return False
    return all(
        isinstance(before, Mapping)
        and all(after.get(key) == value for key, value in before.items())
        for before, after in zip(saved, current)
    )


def _remote_schema_correction_budget_consumed(value: Any) -> bool:
    """Use durable response evidence, never the legacy attempted flag alone."""
    return (
        isinstance(value, Mapping)
        and value.get("method") != "deterministic_local"
        and isinstance(value.get("result"), Mapping)
    )


def _candidate_internal_ids(
    candidates: Sequence[Mapping[str, Any]],
) -> list[str]:
    ids: list[str] = []
    for candidate in candidates:
        source = str(candidate.get("chinese", "") or "").strip()
        if not source:
            raise ChapterGlossaryReviewFailure(
                "chapter glossary-review candidate lacks an authoritative source key"
            )
        expected_id = stable_glossary_candidate_id(source)
        candidate_id = str(candidate.get("candidate_id", "") or "").strip()
        if candidate_id and candidate_id != expected_id:
            raise ChapterGlossaryReviewFailure(
                "chapter glossary-review candidate ID does not match its source key"
            )
        ids.append(candidate_id or expected_id)
    if len(ids) != len(set(ids)):
        raise ChapterGlossaryCandidateIdentityError(
            "chapter glossary-review candidate IDs must be unique",
            code="duplicate_candidate_ids_after_canonicalization",
            diagnostics={
                "raw_candidate_count": len(candidates),
                "canonical_candidate_count": len(candidates),
                "exact_duplicates_removed": 0,
                "semantic_duplicates_removed": 0,
                "conflict_count": 1,
                "candidate_ids": ids,
            },
        )
    return ids


def chapter_review_input_hash(
    candidates: Sequence[Mapping[str, Any]],
    existing_source_keys: Iterable[str],
    source_text: str,
    translation: str,
) -> str:
    """Bind recovery to immutable candidates, source, target, and merge keys."""
    payload = {
        "policy_version": CHAPTER_REVIEW_POLICY_VERSION,
        "candidates": [dict(candidate) for candidate in candidates],
        "existing_source_keys": sorted(set(existing_source_keys)),
        "source_sha256": hashlib.sha256((source_text or "").encode("utf-8")).hexdigest(),
        "translation_sha256": hashlib.sha256(
            (translation or "").encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def chapter_review_operation_ids(
    novel_id: str,
    chapter: int,
    input_hash: str,
) -> dict[str, str]:
    base = f"{novel_id}:{int(chapter)}:{input_hash}"
    return {
        "original": (
            f"chapter_glossary_review:{base}:{CHAPTER_REVIEW_OPERATION_VERSION}"
        ),
        "corrective": (
            "chapter_glossary_review_semantic_correction:"
            f"{base}:{CHAPTER_REVIEW_CORRECTION_VERSION}"
        ),
    }


def chapter_review_schema_correction_operation_id(
    novel_id: str,
    chapter: int,
    input_hash: str,
) -> str:
    """Return a schema-correction ID without changing persisted legacy IDs."""
    base = f"{novel_id}:{int(chapter)}:{input_hash}"
    return (
        "chapter_glossary_review_schema_correction:"
        f"{base}:{CHAPTER_REVIEW_SCHEMA_CORRECTION_VERSION}"
    )


def chapter_review_semantic_retry_operation_id(
    novel_id: str, chapter: int, input_hash: str
) -> str:
    """Return the distinct identity for the single bounded semantic retry."""
    base = f"{novel_id}:{int(chapter)}:{input_hash}"
    return (
        "chapter_glossary_review_semantic_correction:"
        f"{base}:{CHAPTER_REVIEW_CORRECTION_VERSION}:2"
    )


def build_chapter_review_schema_correction_prompt(
    *,
    chapter: int,
    schema: str,
    errors: Sequence[Mapping[str, Any]],
    rejected_result: Mapping[str, Any],
) -> tuple[str, str]:
    """Build the issues-only model-shape correction prompt."""
    system_prompt = f"""
You correct only the top-level issues field in one parsed chapter-level
glossary-review response. Preserve decisions EXACTLY, including decision order,
candidate aliases/IDs, actions, English renderings, notes, merge targets, and
every other decision field. Do not add, omit, summarize, or rewrite decisions.
Change ONLY issues. Return issues as an array of strings. If there are no valid
textual issues, return []. Do not change or add any other top-level content.
Return the complete response as JSON matching:
{schema}
""".strip()
    user_prompt = f"""
CHAPTER: {int(chapter)}
VALIDATOR ERRORS:
{json.dumps(list(errors), ensure_ascii=False, indent=2)}
REJECTED PARSED REVIEW:
{json.dumps(dict(rejected_result), ensure_ascii=False, indent=2)}
""".strip()
    return system_prompt, user_prompt


def build_chapter_review_semantic_correction_prompt(
    *,
    chapter: int,
    schema: str,
    model_candidates: Sequence[Mapping[str, Any]],
    candidate_sources: Mapping[str, str],
    existing_merge_targets: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    rejected_result: Mapping[str, Any],
) -> tuple[str, str]:
    """Build a bounded correction prompt without chapter source/target prose."""
    model_ids = [str(item.get("candidate_id", "") or "") for item in model_candidates]
    authorization = glossary_review_semantic_correction_authorization(errors)
    decision_fields = {
        "action", "english", "category", "approved_renderings",
        "merge_with", "notes",
    }
    constraints = []
    for error in errors:
        candidate_id = str(error.get("candidate_id", "") or "")
        field = str(error.get("field", "") or "")
        reason = str(error.get("reason", "") or "")
        if reason == "max_length_exceeded" and field:
            constraints.append(
                f"{candidate_id}.{field}: rejected length "
                f"{error.get('actual_length')}; must be a string of at most "
                f"{error.get('max_length')} characters."
            )
        elif reason == "wrong_type" and field:
            constraints.append(
                f"{candidate_id}.{field}: rejected type "
                f"{error.get('actual_type')}; must be "
                f"{error.get('expected_type')}."
            )
    scope_lines = []
    for candidate_id in model_ids:
        allowed = sorted(authorization.get(candidate_id, ()))
        forbidden = sorted(decision_fields - set(allowed))
        scope_lines.append(
            f"{candidate_id}: AUTHORIZED FIELDS: "
            f"{', '.join(allowed) if allowed else '(none)'}; "
            f"DO NOT CHANGE: {', '.join(forbidden)}."
        )
    system_prompt = f"""
You correct only one parsed chapter-level glossary-review response.
Candidate IDs and source keys are immutable. Use every supplied request-local
candidate_id exactly once. Do not add, omit, duplicate, replace, or rewrite a
candidate ID or source key. Correct ONLY the validator-identified semantic
fields and preserve every already-valid decision and issue.
Treat the response as a sparse field correction: for each candidate, change
only its explicitly AUTHORIZED FIELDS. Omit or preserve every unauthorized
field. Never change action unless action is independently authorized. Supply a
valid replacement for every authorized field identified by the validator.

Allowed actions: accept, correct, reject, contextual, merge.
For merge, merge_with MUST be an exact existing SOURCE glossary key. An English
rendering is never a source identity and is not a valid merge_with value.
Return the complete decision set as JSON matching:
{schema}
""".strip()
    user_prompt = f"""
CHAPTER: {int(chapter)}
VALID REQUEST-LOCAL CANDIDATE IDS:
{', '.join(model_ids)}
CANDIDATES:
{json.dumps(list(model_candidates), ensure_ascii=False, indent=2)}
REQUEST-LOCAL IDS AND IMMUTABLE SOURCE KEYS:
{json.dumps(dict(candidate_sources), ensure_ascii=False, indent=2)}
VALID EXISTING SOURCE MERGE TARGETS:
{json.dumps(dict(existing_merge_targets), ensure_ascii=False, indent=2)}
VALIDATOR ERRORS:
{json.dumps(list(errors), ensure_ascii=False, indent=2)}
AUTHORIZED CORRECTION SCOPE:
{chr(10).join(scope_lines)}
DETERMINISTIC FIELD CONSTRAINTS:
{chr(10).join(constraints) if constraints else '(No additional field constraints.)'}
REJECTED PARSED REVIEW:
{json.dumps(dict(rejected_result), ensure_ascii=False, indent=2)}
""".strip()
    return system_prompt, user_prompt


def _numeric_combine_stats(*values: Mapping[str, Any] | None) -> dict[str, Any]:
    usable = [copy.deepcopy(dict(value)) for value in values if isinstance(value, Mapping)]
    if not usable:
        return {}
    combined = copy.deepcopy(usable[-1])
    for field in STAT_TOTAL_FIELDS:
        combined[field] = sum(value.get(field, 0) or 0 for value in usable)
    periods = {
        str(value.get("pricing_period"))
        for value in usable
        if value.get("pricing_period")
    }
    if len(periods) > 1:
        combined["pricing_period"] = "mixed"
    combined["request_count"] = len(usable)
    return combined


def _combine_stats(*values: Mapping[str, Any] | None) -> dict[str, Any]:
    if any(value and value.get("provider") == "command_code_provider" for value in values):
        from production_accounting import combine_goat_stats
        combined = combine_goat_stats(_numeric_combine_stats, *values)
        for value in values:
            if value and "glossary_review_accepted_operations" in value:
                combined["glossary_review_accepted_operations"] = copy.deepcopy(value["glossary_review_accepted_operations"])
        return combined
    return _numeric_combine_stats(*values)


def _response_parts(response: Mapping[str, Any], label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(response, Mapping):
        raise ChapterGlossaryReviewFailure(f"{label} callback returned no response")
    result = response.get("result")
    stats = response.get("stats", {})
    if not isinstance(result, Mapping):
        raise ChapterGlossaryReviewFailure(
            f"{label} response did not contain a parsed review object"
        )
    if not isinstance(stats, Mapping):
        raise ChapterGlossaryReviewFailure(f"{label} response stats must be an object")
    return copy.deepcopy(dict(result)), copy.deepcopy(dict(stats))


def _persist(
    record: Mapping[str, Any],
    persist_recovery: Callable[[Mapping[str, Any]], None],
) -> None:
    try:
        persist_recovery(copy.deepcopy(dict(record)))
    except Exception as exc:
        raise ChapterGlossaryReviewPersistenceError(
            "chapter glossary-review recovery persistence failed"
        ) from exc


def _resolve_model_result(result, id_map):
    try:
        return resolve_model_candidate_response(result, id_map)
    except ModelCandidateIdError as exc:
        raise ChapterGlossaryReviewFailure(
            f"chapter glossary review identity validation failed: {exc}"
        ) from exc


def _validate_internal_result(result, candidates, existing_source_keys):
    try:
        return validate_glossary_review_result(
            result, candidates, existing_source_keys
        )
    except (GlossaryReviewSchemaError, GlossaryReviewValidationError):
        raise
    except GlossarySourceIdentityError as exc:
        raise ChapterGlossaryReviewFailure(
            f"chapter glossary review identity validation failed: {exc}"
        ) from exc
    except ValueError as exc:
        raise ChapterGlossaryReviewFailure(
            f"chapter glossary review schema validation failed: {exc}"
        ) from exc


def run_chapter_glossary_review(
    *,
    novel_id: str,
    chapter: int,
    source_text: str,
    translation: str,
    candidates: Sequence[Mapping[str, Any]],
    existing_source_keys: Iterable[str],
    request_original: Callable[
        [Sequence[Mapping[str, Any]], Mapping[str, Any]], Mapping[str, Any]
    ],
    request_correction: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]], Mapping[str, Any]],
        Mapping[str, Any],
    ],
    reconcile_original: Callable | None = None,
    combine_stats: Callable | None = None,
    recovery_record: Mapping[str, Any] | None = None,
    persist_recovery: Callable[[Mapping[str, Any]], None] = lambda record: None,
) -> dict[str, Any]:
    """Run/recover one review; optional accounting callback preserves isolated rates."""
    correction_merge_audit = None
    authorization_errors = []
    correction_retry_stats = {}
    correction_retry_operation_id = ""
    correction_retry_merge_audit = None
    correction_retry_model_result = None
    correction_retry_errors = []
    reused_correction_retry = False
    correction_ineffective = False
    correction_stats = {}
    correction_performed = False
    correction_operation_id = ""
    reused_correction = False
    try:
        prepared = canonicalize_glossary_review_candidates(candidates)
    except GlossaryCandidateIdentityError as exc:
        raise ChapterGlossaryCandidateIdentityError(
            str(exc), code=exc.code, diagnostics=exc.diagnostics
        ) from exc
    candidates = prepared["candidates"]
    candidate_preparation = prepared["diagnostics"]
    existing_keys = tuple(sorted(set(existing_source_keys)))
    for candidate in candidates:
        source_key = str(candidate.get("chinese", "") or "").strip()
        if not source_key or source_key not in (source_text or ""):
            raise ChapterGlossaryReviewFailure(
                "chapter glossary-review candidate lacks exact source evidence"
            )
    candidate_ids = _candidate_internal_ids(candidates)
    id_map = build_model_candidate_id_map(candidate_ids)
    model_candidates = model_facing_candidates(candidates, id_map)
    input_hash = chapter_review_input_hash(
        candidates, existing_keys, source_text, translation
    )
    operation_ids = chapter_review_operation_ids(novel_id, chapter, input_hash)
    schema_correction_operation_id = chapter_review_schema_correction_operation_id(
        novel_id, chapter, input_hash
    )
    semantic_retry_operation_id = chapter_review_semantic_retry_operation_id(
        novel_id, chapter, input_hash
    )

    if recovery_record:
        record = copy.deepcopy(dict(recovery_record))
        if (
            record.get("policy_version") != CHAPTER_REVIEW_POLICY_VERSION
            or record.get("model_id_transport_version")
            != MODEL_CANDIDATE_ID_TRANSPORT_VERSION
            or record.get("input_hash") != input_hash
            or record.get("candidate_ids") != candidate_ids
            or record.get("operation_ids") != operation_ids
        ):
            raise ChapterGlossaryReviewFailure(
                "saved chapter glossary-review recovery does not match current input"
            )
    else:
        record = {
            "policy_version": CHAPTER_REVIEW_POLICY_VERSION,
            "model_id_transport_version": MODEL_CANDIDATE_ID_TRANSPORT_VERSION,
            "input_hash": input_hash,
            "candidate_ids": list(candidate_ids),
            "operation_ids": operation_ids,
            "status": "pending",
        }

    metadata = {
        "chapter": int(chapter),
        "input_hash": input_hash,
        "candidate_ids": list(candidate_ids),
        "model_candidate_ids": list(id_map.alias_to_internal),
        "candidate_sources": {
            id_map.internal_to_alias[candidate_id]: str(
                candidate.get("chinese", "") or ""
            ).strip()
            for candidate_id, candidate in zip(candidate_ids, candidates)
        },
        "model_candidates": copy.deepcopy(model_candidates),
        "model_id_transport_version": MODEL_CANDIDATE_ID_TRANSPORT_VERSION,
    }

    unresolved = [item for key in ("original", "schema_correction", "semantic_correction", "semantic_correction_retry")
        if isinstance(item := record.get(key), Mapping) and item.get("attempted") is True
        and not isinstance(item.get("result"), Mapping)]
    if len(unresolved) > 1:
        raise ChapterGlossaryReviewFailure("multiple unresolved chapter-review paid attempts cannot be reconciled")

    from auxiliary_response_reconciliation import (
        AuxiliaryReconciliation, BOUND_BEFORE_TRANSPORT_VERSION,
    )

    def bound_args(item, args):
        return (*args[:-1], {**args[-1], "outer_attempt": item})

    def prepare_request(item, callback, args):
        prepare = getattr(callback, "prepare", None)
        if callable(prepare):
            item["transport_binding"] = prepare(item, bound_args(item, args))
            item["transport_ownership_version"] = BOUND_BEFORE_TRANSPORT_VERSION

    def resume_request(key, callback, args):
        item = record.get(key)
        if not isinstance(item, dict) or isinstance(item.get("result"), Mapping):
            return False
        recover = getattr(callback, "recover", None)
        if "transport_binding" not in item or not callable(recover):
            return False
        if item.get("operation_id") != args[-1]["operation_id"]:
            raise ChapterGlossaryReviewFailure("unfinished chapter review operation identity mismatch")
        outcome = recover(item, bound_args(item, args))
        if not isinstance(outcome, AuxiliaryReconciliation) or outcome.binding != item["transport_binding"]:
            raise ChapterGlossaryReviewFailure("chapter review reconciliation binding mismatch")
        if outcome.status == "response_recorded":
            response = outcome.response
        elif outcome.status == "pre_transport" and outcome.response is None:
            response = callback(*bound_args(item, args))
        else:
            raise ChapterGlossaryReviewFailure("unsupported chapter review reconciliation outcome")
        result, stats = _response_parts(response, "recovered chapter review " + key)
        item.update(result=result, stats=stats, result_hash=_recorded_result_hash(result))
        record["status"] = key + "_recorded"
        _persist(record, persist_recovery)
        return True

    def invoke_prepared(key, callback, args):
        item = record[key]
        prepare_request(item, callback, args)
        if callable(getattr(callback, "prepare", None)):
            # No attempted/in-flight state escapes before exact ownership is
            # durable. A crash at either write resumes this same bound attempt.
            item["lifecycle"] = "prepared"
            record["status"] = key + "_prepared"
            _persist(record, persist_recovery)
            item["lifecycle"] = "in_flight"
            record["status"] = "original_started" if key == "original" else key + "_in_flight"
            _persist(record, persist_recovery)
            return callback(*bound_args(item, args))
        if key == "schema_correction":
            # Preserve the legacy direct callback's provably unattempted
            # prepared checkpoint; it has no journal ownership capability.
            item["attempted"] = False
            item["lifecycle"] = "prepared"
            record["status"] = key + "_prepared"
            _persist(record, persist_recovery)
            item["attempted"] = True
            item["lifecycle"] = "in_flight"
            record["status"] = key + "_in_flight"
        _persist(record, persist_recovery)
        return callback(*args)

    original = record.get("original")
    if isinstance(original, Mapping) and isinstance(original.get("result"), Mapping):
        if original.get("operation_id") != operation_ids["original"]:
            raise ChapterGlossaryReviewFailure(
                "saved original chapter glossary-review operation identity mismatch"
            )
        original_model_result = copy.deepcopy(dict(original["result"]))
        original_stats = copy.deepcopy(dict(original.get("stats", {})))
        reused_original = True
    else:
        # A missing durable response cannot prove that the provider did no work.
        # Preserve the attempt across interruption, malformed output, and failed
        # response persistence instead of silently paying for the operation again.
        interrupted = "original" in record or record.get("status") != "pending"
        journal_recovery = (interrupted and isinstance(original, Mapping)
            and "transport_binding" in original and callable(getattr(request_original, "recover", None)))
        if interrupted and not journal_recovery and (reconcile_original is None or record.get("status") != "original_started"
                or not isinstance(original, Mapping) or original.get("attempted") is not True
                or original.get("operation_id") != operation_ids["original"]
                or "result" in original):
            raise ChapterGlossaryReviewFailure(
                "original chapter glossary review has no valid durable response; "
                "automatic retry is forbidden because paid work may have occurred. "
                f"Inspect saved recovery evidence for {operation_ids['original']}"
            )
        if not interrupted:
            record["original"] = {"operation_id": operation_ids["original"], "attempted": True}
            record["status"] = "original_started"
        def persist_transport_recovery(proof):
            prior = record.get("reasoning_empty_transport_recovery")
            if prior is not None and prior != proof:
                raise ChapterGlossaryReviewFailure("transport recovery evidence drift")
            record["reasoning_empty_transport_recovery"] = copy.deepcopy(proof)
            _persist(record, persist_recovery)
        original_metadata = {**metadata, "operation_id": operation_ids["original"],
            "require_recorded_empty": interrupted and not journal_recovery,
            "persist_transport_recovery": persist_transport_recovery}
        if journal_recovery:
            resume_request("original", request_original, (model_candidates, original_metadata))
            response = {"result": record["original"]["result"], "stats": record["original"]["stats"]}
        elif interrupted:
            response = reconcile_original(model_candidates, original_metadata)
        else:
            response = invoke_prepared("original", request_original, (model_candidates, original_metadata))
        if interrupted and not journal_recovery and "reasoning_empty_transport_recovery" not in record:
            raise ChapterGlossaryReviewFailure("original reconciliation requires durable GOAT recovery evidence")
        original_model_result, original_stats = _response_parts(
            response, "original chapter glossary review"
        )
        record["original"] = {
            **record["original"],
            "operation_id": operation_ids["original"],
            "result": original_model_result,
            "stats": original_stats,
        }
        record["status"] = "original_recorded"
        _persist(record, persist_recovery)
        reused_original = False

    if "reasoning_empty_transport_recovery" in record:
        from production_goat_glossary_review_recovery import validate_recovery_accounting
        validate_recovery_accounting(original_stats, record["reasoning_empty_transport_recovery"])

    saved_local_revalidation = record.get("local_revalidation")
    if isinstance(saved_local_revalidation, Mapping):
        expected_original_hash = saved_local_revalidation.get(
            "original_result_hash"
        )
        if (
            expected_original_hash
            and expected_original_hash != _recorded_result_hash(original_model_result)
        ):
            raise ChapterGlossaryReviewFailure(
                "saved original chapter glossary-review result identity mismatch"
            )

    try:
        resolved_original = _resolve_model_result(original_model_result, id_map)
    except ChapterGlossaryReviewFailure as exc:
        record["status"] = "failed"
        record["identity_error"] = str(exc)
        _persist(record, persist_recovery)
        raise
    effective_model_result = original_model_result
    try:
        validate_glossary_review_schema(resolved_original)
    except GlossaryReviewSchemaError as schema_error:
        schema_errors = [schema_error.as_dict()]
        if not schema_error.retryable or schema_error.code != "invalid_issues_shape":
            record["status"] = "failed"
            record["schema_errors"] = schema_errors
            _persist(record, persist_recovery)
            raise ChapterGlossaryReviewFailure(
                "chapter glossary review schema validation failed: "
                + str(schema_error)
            ) from schema_error

        model_errors = copy.deepcopy(schema_errors)
        correction = record.get("schema_correction")
        if isinstance(correction, Mapping):
            if correction.get("operation_id") != schema_correction_operation_id:
                raise ChapterGlossaryReviewFailure(
                    "saved chapter glossary schema-correction operation identity "
                    "mismatch"
                )
            if not _recorded_validation_errors_match(
                correction.get("errors"), model_errors
            ):
                raise ChapterGlossaryReviewFailure(
                    "saved chapter glossary schema-correction error identity mismatch"
                )

        resume_request("schema_correction", request_correction, (original_model_result, model_errors,
            {**metadata, "operation_id": schema_correction_operation_id,
             "correction_class": "model_schema", "allowed_fields": list(schema_error.allowed_fields)}))
        if isinstance(correction, Mapping) and isinstance(
            correction.get("result"), Mapping
        ):
            corrected_model_result = copy.deepcopy(dict(correction["result"]))
            correction_stats = copy.deepcopy(dict(correction.get("stats", {})))
            reused_correction = True
            recorded_hash = str(correction.get("result_hash", "") or "")
            if recorded_hash and recorded_hash != _recorded_result_hash(
                corrected_model_result
            ):
                raise ChapterGlossaryReviewFailure(
                    "saved chapter glossary schema-correction result identity mismatch"
                )
            if isinstance(saved_local_revalidation, Mapping):
                expected_correction_hash = saved_local_revalidation.get(
                    "corrective_result_hash"
                )
                if (
                    expected_correction_hash
                    and expected_correction_hash
                    != _recorded_result_hash(corrected_model_result)
                ):
                    raise ChapterGlossaryReviewFailure(
                        "saved chapter glossary semantic-correction result identity "
                        "mismatch"
                    )
        else:
            corrected_model_result = normalize_glossary_review_issues_shape(
                original_model_result,
                model_errors,
                id_map.alias_to_internal.keys(),
            )
            if corrected_model_result is not None:
                prior_marker = None
                if isinstance(correction, Mapping):
                    prior_marker = {
                        "operation_id": schema_correction_operation_id,
                        "prior_status": record.get("status"),
                        "legacy_attempted": correction.get("attempted") is True,
                        "prior_lifecycle": str(
                            correction.get("lifecycle", "legacy_in_flight") or ""
                        ),
                        "durable_result_recorded": False,
                        "outcome": "uncertain_not_reissued",
                    }
                local_record = {
                    "attempted": False,
                    "operation_id": schema_correction_operation_id,
                    "lifecycle": "validated",
                    "method": "deterministic_local",
                    "policy": CHAPTER_REVIEW_LOCAL_ISSUES_POLICY,
                    "errors": model_errors,
                    "result": copy.deepcopy(corrected_model_result),
                    "result_hash": _recorded_result_hash(corrected_model_result),
                    "stats": {
                        "api_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cache_hit_tokens": 0,
                        "cache_miss_tokens": 0,
                        "cost_usd": 0.0,
                        "rate_correct_cost_usd": 0.0,
                    },
                }
                if prior_marker is not None:
                    local_record["orphaned_remote_operation"] = prior_marker
                record["schema_correction"] = local_record
                record["status"] = "schema_correction_locally_validated"
                _persist(record, persist_recovery)
                correction_stats = {}
                correction_performed = False
                correction_operation_id = ""
                reused_correction = isinstance(correction, Mapping)
            else:
                lifecycle = ""
                if isinstance(correction, Mapping):
                    lifecycle = str(correction.get("lifecycle", "") or "")
                    if not lifecycle:
                        lifecycle = (
                            "in_flight"
                            if correction.get("attempted") is True
                            else "prepared"
                        )
                if isinstance(correction, Mapping) and lifecycle != "prepared":
                    uncertain = copy.deepcopy(dict(correction))
                    uncertain["lifecycle"] = "outcome_uncertain"
                    uncertain["method"] = str(
                        uncertain.get("method", "model_remote") or "model_remote"
                    )
                    uncertain["durable_result_recorded"] = False
                    record["schema_correction"] = uncertain
                    record["status"] = (
                        "schema_correction_remote_outcome_uncertain"
                    )
                    record["schema_correction_error"] = (
                        "schema_correction_remote_outcome_uncertain: operation was "
                        "prepared/in-flight but has no durable result; deterministic "
                        "local recovery was unavailable; automatic reissue is blocked "
                        "to avoid a possible duplicate charge"
                    )
                    _persist(record, persist_recovery)
                    raise ChapterGlossaryReviewFailure(
                        record["schema_correction_error"]
                    )

                semantic_correction = record.get("semantic_correction")
                if (
                    isinstance(semantic_correction, Mapping)
                    and semantic_correction.get("attempted")
                ):
                    raise ChapterGlossaryReviewFailure(
                        "chapter glossary correction budget was already consumed; "
                        "no schema correction remains"
                    )

                record["schema_correction"] = {
                    "attempted": True,
                    "operation_id": schema_correction_operation_id,
                    "lifecycle": "in_flight",
                    "method": "model_remote",
                    "errors": model_errors,
                    "durable_result_recorded": False,
                }
                record["status"] = "schema_correction_in_flight"
                correction_metadata = {
                    **metadata,
                    "operation_id": schema_correction_operation_id,
                    "correction_class": "model_schema",
                    "allowed_fields": list(schema_error.allowed_fields),
                }
                response = invoke_prepared("schema_correction", request_correction,
                    (original_model_result, model_errors, correction_metadata))
                corrected_model_result, correction_stats = _response_parts(
                    response, "chapter glossary schema correction"
                )
                record["schema_correction"] = {
                    "attempted": True,
                    "operation_id": schema_correction_operation_id,
                    "lifecycle": "recorded",
                    "method": "model_remote",
                    "errors": model_errors,
                    "result": corrected_model_result,
                    "result_hash": _recorded_result_hash(
                        corrected_model_result
                    ),
                    "stats": correction_stats,
                    "durable_result_recorded": True,
                }
                record["status"] = "schema_correction_recorded"
                _persist(record, persist_recovery)
                reused_correction = False

        local_schema_correction = (
            record["schema_correction"].get("method") == "deterministic_local"
        )
        correction_performed = not local_schema_correction
        correction_operation_id = (
            schema_correction_operation_id if correction_performed else ""
        )
        try:
            resolved_corrected = _resolve_model_result(corrected_model_result, id_map)
            validate_glossary_review_schema_correction_scope(
                resolved_original, resolved_corrected, schema_errors
            )
            validate_glossary_review_schema(resolved_corrected)
        except (
            ChapterGlossaryReviewFailure,
            GlossaryReviewSchemaError,
            GlossarySourceIdentityError,
        ) as exc:
            record["status"] = "failed"
            record["schema_correction_error"] = str(exc)
            _persist(record, persist_recovery)
            raise ChapterGlossaryReviewFailure(
                "chapter glossary schema correction failed validation: " + str(exc)
            ) from exc
        resolved_original = resolved_corrected
        effective_model_result = corrected_model_result
        if correction_performed:
            record["schema_correction"]["lifecycle"] = "validated"
            record["status"] = "schema_correction_validated"
            _persist(record, persist_recovery)

    try:
        validated = _validate_internal_result(
            resolved_original, candidates, existing_keys
        )
    except GlossaryReviewValidationError as semantic_error:
        if not semantic_error.retryable:
            record["status"] = "failed"
            record["validation_errors"] = [semantic_error.as_dict()]
            _persist(record, persist_recovery)
            raise ChapterGlossaryReviewFailure(
                "chapter glossary review semantic validation failed: "
                + str(semantic_error)
            ) from semantic_error

        internal_errors = [semantic_error.as_dict()]
        rediscovered_errors = collect_glossary_review_decision_validation_errors(
            resolved_original, candidates, existing_keys
        )
        rediscovered_identities = {
            (
                str(error.get("candidate_id", "") or ""),
                str(error.get("code", "") or ""),
                error.get("retryable") is True,
            )
            for error in rediscovered_errors
        }
        historical_identity = (
            semantic_error.candidate_id,
            semantic_error.code,
            semantic_error.retryable is True,
        )
        if historical_identity not in rediscovered_identities:
            raise ChapterGlossaryReviewFailure(
                "current local validation cannot reproduce the recorded chapter "
                "glossary semantic error"
            )
        # Rediscovery includes the historical first failure, so using the
        # deduplicated current inventory is exactly the historical/current
        # union while retaining one authoritative error per identity.
        authorization_errors = []
        seen_error_identities = set()
        for error in rediscovered_errors:
            identity = (
                str(error.get("candidate_id", "") or ""),
                str(error.get("code", "") or ""),
            )
            if identity in seen_error_identities:
                continue
            seen_error_identities.add(identity)
            authorization_errors.append(error)
        model_errors = copy.deepcopy(internal_errors)
        for error in model_errors:
            internal_id = str(error.get("candidate_id", "") or "")
            error["candidate_id"] = id_map.internal_to_alias.get(
                internal_id, internal_id
            )

        schema_correction = record.get("schema_correction")
        if _remote_schema_correction_budget_consumed(schema_correction):
            record["status"] = "failed"
            record["schema_correction_error"] = (
                "paid schema correction failed semantic validation; the bounded "
                "correction budget was already consumed"
            )
            _persist(record, persist_recovery)
            raise ChapterGlossaryReviewFailure(
                "chapter glossary schema correction failed validation: correction "
                "budget was already consumed; no semantic correction remains"
            )

        resume_request("semantic_correction", request_correction, (effective_model_result, model_errors,
            {**metadata, "operation_id": operation_ids["corrective"], "correction_class": "semantic", "correction_attempt": 1}))
        correction = record.get("semantic_correction")
        if isinstance(correction, Mapping) and correction.get("attempted"):
            if correction.get("operation_id") != operation_ids["corrective"]:
                raise ChapterGlossaryReviewFailure(
                    "saved chapter glossary semantic-correction operation identity "
                    "mismatch"
                )
            if not _recorded_validation_errors_match(
                correction.get("validation_errors"), model_errors
            ):
                raise ChapterGlossaryReviewFailure(
                    "saved chapter glossary semantic-correction validation-error "
                    "identity mismatch"
                )
            if not isinstance(correction.get("result"), Mapping):
                raise ChapterGlossaryReviewFailure(
                    "chapter glossary semantic correction was already attempted; "
                    "no automatic retry remains"
                )
            corrected_model_result = copy.deepcopy(dict(correction["result"]))
            correction_stats = copy.deepcopy(dict(correction.get("stats", {})))
            reused_correction = True
            if isinstance(saved_local_revalidation, Mapping):
                expected_correction_hash = saved_local_revalidation.get(
                    "corrective_result_hash"
                )
                if (
                    expected_correction_hash
                    and expected_correction_hash
                    != _recorded_result_hash(corrected_model_result)
                ):
                    raise ChapterGlossaryReviewFailure(
                        "saved chapter glossary semantic-correction result identity "
                        "mismatch"
                    )
        else:
            record["semantic_correction"] = {
                "attempted": True,
                "operation_id": operation_ids["corrective"],
                "validation_errors": model_errors,
            }
            record["status"] = "semantic_correction_in_flight"
            correction_metadata = {
                **metadata,
                "operation_id": operation_ids["corrective"],
                "correction_class": "semantic",
                "correction_attempt": 1,
            }
            response = invoke_prepared("semantic_correction", request_correction,
                (effective_model_result, model_errors, correction_metadata))
            corrected_model_result, correction_stats = _response_parts(
                response, "chapter glossary semantic correction"
            )
            record["semantic_correction"] = {
                "attempted": True,
                "operation_id": operation_ids["corrective"],
                "validation_errors": model_errors,
                "result": corrected_model_result,
                "stats": correction_stats,
            }
            record["status"] = "semantic_correction_recorded"
            _persist(record, persist_recovery)
            reused_correction = False

        correction_performed = True
        correction_operation_id = operation_ids["corrective"]
        try:
            resolved_corrected = _resolve_model_result(corrected_model_result, id_map)
            effective_corrected, correction_merge_audit = (
                merge_glossary_review_semantic_correction(
                    resolved_original, resolved_corrected, authorization_errors
                )
            )
            record["local_revalidation"] = {
                "status": "candidate_merged",
                "original_operation_id": operation_ids["original"],
                "corrective_operation_id": operation_ids["corrective"],
                "reused_original": reused_original,
                "reused_correction": reused_correction,
                "prior_failure": {
                    "status": record.get("status"),
                    "correction_error": record.get("correction_error"),
                },
                "original_result_hash": _recorded_result_hash(
                    original_model_result
                ),
                "corrective_result_hash": _recorded_result_hash(
                    corrected_model_result
                ),
                "semantic_correction_merge": copy.deepcopy(
                    correction_merge_audit
                ),
                "local_revalidation_errors": copy.deepcopy(
                    authorization_errors
                ),
            }
        except (
            ChapterGlossaryReviewFailure,
            GlossarySourceIdentityError,
        ) as exc:
            record["status"] = "failed"
            if "correction_error" not in record:
                record["correction_error"] = str(exc)
            if isinstance(record.get("local_revalidation"), dict):
                record["local_revalidation"]["status"] = "validation_failed"
                record["local_revalidation"]["validation_error"] = str(exc)
            _persist(record, persist_recovery)
            raise ChapterGlossaryReviewFailure(
                "chapter glossary semantic correction failed validation: " + str(exc)
            ) from exc

        try:
            validated = _validate_internal_result(
                effective_corrected, candidates, existing_keys
            )
        except GlossaryReviewValidationError as first_correction_error:
            correction_retry_errors = (
                collect_glossary_review_decision_validation_errors(
                    effective_corrected, candidates, existing_keys
                )
            )
            original_error_identities = {
                (
                    str(error.get("candidate_id", "") or ""),
                    str(error.get("code", "") or ""),
                )
                for error in authorization_errors
            }
            unresolved_identities = {
                (
                    str(error.get("candidate_id", "") or ""),
                    str(error.get("code", "") or ""),
                )
                for error in correction_retry_errors
            }
            applied_fields = list(
                correction_merge_audit.get("applied_fields", [])
            )
            # This fails closed on unknown rediscovered codes and identifies
            # the exact still-unresolved fields before retry eligibility is
            # decided. A field fixed by attempt 1 is reusable progress and
            # therefore cannot be silently discarded by an attempt-2 merge.
            retry_authorization = (
                glossary_review_semantic_correction_authorization(
                    correction_retry_errors
                )
            )
            retry_scope_is_exact = bool(
                correction_retry_errors
                and unresolved_identities <= original_error_identities
                and all(
                    error.get("retryable") is True
                    for error in correction_retry_errors
                )
                and all(
                    item.get("field") in retry_authorization.get(
                        item.get("candidate_id"), frozenset()
                    )
                    for item in applied_fields
                )
            )
            if not retry_scope_is_exact:
                record["status"] = "failed"
                if "correction_error" not in record:
                    record["correction_error"] = str(first_correction_error)
                record["local_revalidation"]["status"] = "validation_failed"
                record["local_revalidation"]["validation_error"] = str(
                    first_correction_error
                )
                _persist(record, persist_recovery)
                raise ChapterGlossaryReviewFailure(
                    "chapter glossary semantic correction failed validation: "
                    + str(first_correction_error)
                ) from first_correction_error

            correction_ineffective = True
            record["local_revalidation"].update({
                "status": "semantic_correction_ineffective",
                "validation_error": str(first_correction_error),
                "unresolved_errors": copy.deepcopy(correction_retry_errors),
                "authorized_fields_supplied_count": len(applied_fields),
                "resolved_authorized_field_count": 0,
            })
            _persist(record, persist_recovery)

            retry_model_errors = copy.deepcopy(correction_retry_errors)
            for error in retry_model_errors:
                internal_id = str(error.get("candidate_id", "") or "")
                error["candidate_id"] = id_map.internal_to_alias.get(
                    internal_id, internal_id
                )
            retry_rejected_model_result = {
                **copy.deepcopy(dict(effective_corrected)),
                "decisions": model_facing_candidates(
                    effective_corrected["decisions"], id_map
                ),
            }
            resume_request("semantic_correction_retry", request_correction,
                (retry_rejected_model_result, retry_model_errors,
                 {**metadata, "operation_id": semantic_retry_operation_id, "correction_class": "semantic",
                  "correction_attempt": 2, "previous_correction_operation_id": operation_ids["corrective"],
                  "previous_correction_status": "semantic_correction_ineffective"}))
            retry_record = record.get("semantic_correction_retry")
            if isinstance(retry_record, Mapping) and retry_record.get("attempted"):
                if retry_record.get("operation_id") != semantic_retry_operation_id:
                    raise ChapterGlossaryReviewFailure(
                        "saved chapter glossary semantic-correction retry operation "
                        "identity mismatch"
                    )
                if not _recorded_validation_errors_match(
                    retry_record.get("validation_errors"), retry_model_errors
                ):
                    raise ChapterGlossaryReviewFailure(
                        "saved chapter glossary semantic-correction retry error "
                        "identity mismatch"
                    )
                if not isinstance(retry_record.get("result"), Mapping):
                    raise ChapterGlossaryReviewFailure(
                        "chapter glossary semantic-correction retry was already "
                        "attempted; no automatic retry remains"
                    )
                correction_retry_model_result = copy.deepcopy(
                    dict(retry_record["result"])
                )
                expected_retry_hash = str(
                    retry_record.get("result_hash", "") or ""
                )
                if (
                    not expected_retry_hash
                    or expected_retry_hash
                    != _recorded_result_hash(correction_retry_model_result)
                ):
                    raise ChapterGlossaryReviewFailure(
                        "saved chapter glossary semantic-correction retry result "
                        "identity mismatch"
                    )
                correction_retry_stats = copy.deepcopy(
                    dict(retry_record.get("stats", {}))
                )
                reused_correction_retry = True
            else:
                record["semantic_correction_retry"] = {
                    "attempted": True,
                    "attempt": 2,
                    "operation_id": semantic_retry_operation_id,
                    "validation_errors": copy.deepcopy(retry_model_errors),
                }
                record["status"] = "semantic_correction_retry_in_flight"
                retry_metadata = {
                    **metadata,
                    "operation_id": semantic_retry_operation_id,
                    "correction_class": "semantic",
                    "correction_attempt": 2,
                    "previous_correction_operation_id": operation_ids[
                        "corrective"
                    ],
                    "previous_correction_status": (
                        "semantic_correction_ineffective"
                    ),
                }
                response = invoke_prepared("semantic_correction_retry", request_correction,
                    (retry_rejected_model_result, retry_model_errors, retry_metadata))
                correction_retry_model_result, correction_retry_stats = (
                    _response_parts(
                        response, "chapter glossary semantic-correction retry"
                    )
                )
                record["semantic_correction_retry"] = {
                    "attempted": True,
                    "attempt": 2,
                    "operation_id": semantic_retry_operation_id,
                    "validation_errors": copy.deepcopy(retry_model_errors),
                    "result": copy.deepcopy(correction_retry_model_result),
                    "result_hash": _recorded_result_hash(
                        correction_retry_model_result
                    ),
                    "stats": copy.deepcopy(correction_retry_stats),
                }
                record["status"] = "semantic_correction_retry_recorded"
                _persist(record, persist_recovery)
                reused_correction_retry = False

            correction_retry_operation_id = semantic_retry_operation_id
            try:
                resolved_retry = _resolve_model_result(
                    correction_retry_model_result, id_map
                )
                retry_effective, correction_retry_merge_audit = (
                    merge_glossary_review_semantic_correction(
                        resolved_original,
                        resolved_retry,
                        correction_retry_errors,
                    )
                )
                validated = _validate_internal_result(
                    retry_effective, candidates, existing_keys
                )
            except (
                ChapterGlossaryReviewFailure,
                GlossaryReviewValidationError,
                GlossarySourceIdentityError,
            ) as retry_exc:
                record["status"] = "failed"
                record["semantic_correction_retry_error"] = str(retry_exc)
                record["local_revalidation"].update({
                    "status": "semantic_correction_retry_failed",
                    "semantic_correction_retry_operation_id": (
                        semantic_retry_operation_id
                    ),
                    "semantic_correction_retry_merge": copy.deepcopy(
                        correction_retry_merge_audit
                    ),
                    "semantic_correction_retry_error": str(retry_exc),
                })
                _persist(record, persist_recovery)
                raise ChapterGlossaryReviewFailure(
                    "chapter glossary semantic-correction retry failed validation: "
                    + str(retry_exc)
                ) from retry_exc
    combined_stats = (combine_stats or _combine_stats)(
        original_stats,
        correction_stats if correction_performed else None,
        correction_retry_stats if correction_retry_operation_id else None,
    )
    combined_stats["operation_ids"] = [
        operation_ids["original"],
        *([correction_operation_id] if correction_performed else []),
        *([correction_retry_operation_id] if correction_retry_operation_id else []),
    ]
    prior_failure = None
    if record.get("status") == "failed" or record.get("correction_error"):
        prior_failure = {
            "status": record.get("status"),
            "correction_error": record.get("correction_error"),
        }
    record["local_revalidation"] = {
        "status": "completed",
        "original_operation_id": operation_ids["original"],
        "corrective_operation_id": (
            correction_operation_id if correction_performed else ""
        ),
        "reused_original": reused_original,
        "reused_correction": reused_correction,
        "prior_failure": prior_failure,
        "original_result_hash": _recorded_result_hash(original_model_result),
        "corrective_result_hash": (
            _recorded_result_hash(corrected_model_result)
            if correction_performed else ""
        ),
        "semantic_correction_merge": copy.deepcopy(correction_merge_audit),
        "semantic_correction_ineffective": correction_ineffective,
        "semantic_correction_retry_operation_id": correction_retry_operation_id,
        "reused_correction_retry": reused_correction_retry,
        "semantic_correction_retry_result_hash": (
            _recorded_result_hash(correction_retry_model_result)
            if correction_retry_operation_id
            and isinstance(correction_retry_model_result, Mapping)
            else ""
        ),
        "semantic_correction_retry_errors": copy.deepcopy(
            correction_retry_errors
        ),
        "semantic_correction_retry_merge": copy.deepcopy(
            correction_retry_merge_audit
        ),
        "local_revalidation_errors": copy.deepcopy(authorization_errors),
    }
    record["status"] = "completed"
    record["validated_result"] = validated
    record["combined_stats"] = combined_stats
    _persist(record, persist_recovery)
    return {
        "review_result": validated,
        "stats": combined_stats,
        "canonical_candidates": copy.deepcopy(candidates),
        "candidate_preparation": copy.deepcopy(candidate_preparation),
        "recovery_record": copy.deepcopy(record),
        "input_hash": input_hash,
        "operation_ids": operation_ids,
        "reused_original": reused_original,
        "reused_correction": reused_correction,
        "reused_correction_retry": reused_correction_retry,
    }
