"""Exact Batch-result provenance and recovery guards for full-pipeline commits.

The generic Luna runtime records that a committed chapter was translated by
GPT-5.6 Luna through OpenAI Batch.  This module adds the stronger experiment
identity needed for crash reconciliation: which frozen wave request and which
provider response became this exact committed chapter.

The installer also bridges two legacy recovery assumptions only for the active
experiment engine module:

* startup pending-accounting validation delegates recognized Luna Batch or Luna
  Standard records to the strict Luna validator instead of pretending they use
  DeepSeek peak/off-peak pricing; and
* the bounded production runner's ``SystemExit(1)`` is suppressed only when
  durable local evidence proves either that this exact Batch result was blocked
  by commit QA and remains pending, or that it was already committed and every
  audit due at this chapter has completed.

Every other production exit propagates unchanged.  Core validation is restored
immediately after the startup check.  Importing/installing this module performs
no provider access and installation itself performs no filesystem writes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

import noveltranslator_core as core
from full_pipeline_batch_wave import BATCH_MODEL, BATCH_PRICING_VERSION
from full_pipeline_luna_runtime import (
    LUNA_STANDARD_PRICING_VERSION,
    validate_saved_luna_stats,
)


ACCEPTANCE_SCHEMA_VERSION = 1
ACCEPTANCE_FIELD = "full_pipeline_batch_acceptance"
ACCEPTANCE_MODE = "full_pipeline_exact_batch_acceptance_v1"
_WRAPPER_STATE_ATTR = "_full_pipeline_exact_batch_acceptance_state"
_STARTUP_STATE_ATTR = "_full_pipeline_luna_pending_startup_validation_state"
_RUNNER_STATE_ATTR = "_full_pipeline_batch_runner_recovery_state"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LUNA_PRICING_VERSIONS = frozenset(
    (BATCH_PRICING_VERSION, LUNA_STANDARD_PRICING_VERSION)
)


class FullPipelineBatchAcceptanceError(RuntimeError):
    """Exact Batch acceptance evidence is malformed or conflicts with a commit."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FullPipelineBatchAcceptanceError(
            "Batch acceptance evidence is not canonically JSON-serializable"
        ) from exc


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FullPipelineBatchAcceptanceError(f"{label} must be non-empty")
    return value.strip()


def _require_sha(value: Any, label: str) -> str:
    value = _require_text(value, label)
    if not _SHA256_RE.fullmatch(value):
        raise FullPipelineBatchAcceptanceError(
            f"{label} must be a lowercase SHA-256 hex digest"
        )
    return value


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_batch_acceptance_evidence(
    *,
    experiment_id: str,
    wave_number: int,
    batch_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one collected request and return immutable commit provenance."""
    if not isinstance(result, Mapping):
        raise FullPipelineBatchAcceptanceError("collected Batch result must be an object")
    if (
        not isinstance(wave_number, int)
        or isinstance(wave_number, bool)
        or wave_number < 1
    ):
        raise FullPipelineBatchAcceptanceError("wave_number must be positive")
    chapter = result.get("chapter")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise FullPipelineBatchAcceptanceError("collected chapter must be positive")
    novel_id = _require_text(result.get("novel_id"), "novel_id")
    custom_id = _require_text(result.get("custom_id"), "custom_id")
    request_sha = _require_sha(result.get("request_sha256"), "request_sha256")
    body_sha = _require_sha(result.get("body_sha256"), "body_sha256")
    lineage_sha = _require_sha(
        result.get("lineage_before_sha256"),
        "lineage_before_sha256",
    )
    content = result.get("content")
    if not isinstance(content, str):
        raise FullPipelineBatchAcceptanceError("collected content must be a string")
    if result.get("transport") != "openai_batch":
        raise FullPipelineBatchAcceptanceError("collected result is not OpenAI Batch")
    if result.get("provider_response_model") != BATCH_MODEL:
        raise FullPipelineBatchAcceptanceError(
            f"collected result model must be exact {BATCH_MODEL!r}"
        )
    stats = result.get("stats")
    if not isinstance(stats, Mapping):
        raise FullPipelineBatchAcceptanceError("collected result stats must be an object")
    if stats.get("pricing_version") != BATCH_PRICING_VERSION:
        raise FullPipelineBatchAcceptanceError(
            "collected result does not use the frozen Luna Batch pricing version"
        )
    # Full accounting validation happens before pending installation too, but
    # acceptance evidence must itself refuse a malformed paid-cost record.
    try:
        validate_saved_luna_stats(stats)
    except Exception as exc:
        raise FullPipelineBatchAcceptanceError(
            f"collected Batch accounting is invalid: {exc}"
        ) from exc
    finish_reason = _require_text(result.get("finish_reason"), "finish_reason")

    evidence = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "mode": ACCEPTANCE_MODE,
        "experiment_id": _require_text(experiment_id, "experiment_id"),
        "wave_number": wave_number,
        "batch_id": _require_text(batch_id, "batch_id"),
        "novel_id": novel_id,
        "chapter": chapter,
        "custom_id": custom_id,
        "request_sha256": request_sha,
        "body_sha256": body_sha,
        "lineage_before_sha256": lineage_sha,
        "provider_request_id": str(result.get("provider_request_id") or ""),
        "provider_response_id": _require_text(
            result.get("provider_response_id"),
            "provider_response_id",
        ),
        "provider_response_model": BATCH_MODEL,
        "finish_reason": finish_reason,
        "translation_content_sha256": _sha256_text(content),
        "translation_pricing_version": BATCH_PRICING_VERSION,
    }
    evidence["acceptance_sha256"] = _sha256_text(_canonical_json(evidence))
    return evidence


def validate_batch_acceptance_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a previously constructed evidence object without provider access."""
    if not isinstance(value, Mapping):
        raise FullPipelineBatchAcceptanceError("acceptance evidence must be an object")
    evidence = copy.deepcopy(dict(value))
    if evidence.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        raise FullPipelineBatchAcceptanceError("unsupported acceptance schema version")
    if evidence.get("mode") != ACCEPTANCE_MODE:
        raise FullPipelineBatchAcceptanceError("unsupported acceptance mode")
    expected_hash = evidence.pop("acceptance_sha256", None)
    expected_hash = _require_sha(expected_hash, "acceptance_sha256")
    actual_hash = _sha256_text(_canonical_json(evidence))
    if actual_hash != expected_hash:
        raise FullPipelineBatchAcceptanceError("acceptance evidence hash mismatch")
    evidence["acceptance_sha256"] = expected_hash
    if evidence.get("provider_response_model") != BATCH_MODEL:
        raise FullPipelineBatchAcceptanceError("acceptance evidence model mismatch")
    if evidence.get("translation_pricing_version") != BATCH_PRICING_VERSION:
        raise FullPipelineBatchAcceptanceError("acceptance evidence pricing mismatch")
    _require_sha(evidence.get("request_sha256"), "request_sha256")
    _require_sha(evidence.get("body_sha256"), "body_sha256")
    _require_sha(evidence.get("lineage_before_sha256"), "lineage_before_sha256")
    _require_sha(
        evidence.get("translation_content_sha256"),
        "translation_content_sha256",
    )
    chapter = evidence.get("chapter")
    wave_number = evidence.get("wave_number")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise FullPipelineBatchAcceptanceError("acceptance chapter is invalid")
    if (
        not isinstance(wave_number, int)
        or isinstance(wave_number, bool)
        or wave_number < 1
    ):
        raise FullPipelineBatchAcceptanceError("acceptance wave number is invalid")
    for field in (
        "experiment_id",
        "batch_id",
        "novel_id",
        "custom_id",
        "provider_response_id",
        "finish_reason",
    ):
        _require_text(evidence.get(field), field)
    return evidence


def _install_luna_pending_startup_validation(production_module):
    """Accept only strictly validated Luna accounting during experiment startup.

    Normal production's core validator remains DeepSeek-specific.  The wrapper
    temporarily replaces only ``core.validate_stats_record`` while the already
    existing startup-integrity function inspects pending metadata.  Every
    non-Luna stats record is delegated byte-for-behavior to the original core
    validator, and the core symbol is restored in ``finally``.
    """
    current = getattr(production_module, "startup_integrity_errors", None)
    if not callable(current):
        raise FullPipelineBatchAcceptanceError(
            "production module has no callable startup_integrity_errors"
        )
    existing = getattr(current, _STARTUP_STATE_ATTR, None)
    if isinstance(existing, dict):
        return existing

    original_startup = current

    def wrapped_startup_integrity_errors(progress, output_dir, pending_dir):
        original_validator = core.validate_stats_record

        def validate_stats_record(stats, label, allow_unpriced_zero=False):
            if (
                isinstance(stats, Mapping)
                and stats.get("pricing_version") in _LUNA_PRICING_VERSIONS
            ):
                try:
                    validate_saved_luna_stats(stats)
                except Exception as exc:
                    return [f"{label} has invalid Luna accounting: {exc}"]
                return []
            return original_validator(
                stats,
                label,
                allow_unpriced_zero=allow_unpriced_zero,
            )

        core.validate_stats_record = validate_stats_record
        try:
            return original_startup(progress, output_dir, pending_dir)
        finally:
            core.validate_stats_record = original_validator

    state = {
        "mode": "full_pipeline_luna_pending_startup_validation_v1",
        "original_startup_integrity_errors": original_startup,
        "wrapped_startup_integrity_errors": wrapped_startup_integrity_errors,
    }
    setattr(wrapped_startup_integrity_errors, _STARTUP_STATE_ATTR, state)
    production_module.startup_integrity_errors = wrapped_startup_integrity_errors
    return state


def _exact_evidence_matches(value: Any, proven: Mapping[str, Any]) -> bool:
    try:
        return validate_batch_acceptance_evidence(value) == dict(proven)
    except Exception:
        return False


def _pending_is_exact_qa_block(
    production_module,
    proven: Mapping[str, Any],
) -> bool:
    chapter = proven["chapter"]
    meta_path = Path(production_module.PENDING_DIR) / f"{chapter:04d}.json"
    meta = _read_json_object(meta_path)
    if not isinstance(meta, dict):
        return False
    if not _exact_evidence_matches(meta.get(ACCEPTANCE_FIELD), proven):
        return False
    qa = meta.get("local_qa")
    return isinstance(qa, dict) and qa.get("commit_allowed") is False


def _committed_is_exact_and_audit_safe(
    production_module,
    qa_root: Path,
    proven: Mapping[str, Any],
) -> bool:
    chapter = proven["chapter"]
    progress = getattr(production_module, "progress", None)
    if not isinstance(progress, dict) or chapter not in progress.get("completed", []):
        return False
    qa = _read_json_object(qa_root / f"{chapter:04d}.json")
    if not isinstance(qa, dict) or not _exact_evidence_matches(
        qa.get(ACCEPTANCE_FIELD), proven
    ):
        return False
    audit_interval = getattr(production_module, "AUDIT_INTERVAL", None)
    if (
        isinstance(audit_interval, int)
        and not isinstance(audit_interval, bool)
        and audit_interval > 0
        and chapter % audit_interval == 0
        and progress.get("last_audited_chapter", 0) < chapter
    ):
        return False
    return True


def _install_exact_runner_recovery(
    production_module,
    qa_root: Path,
    proven: Mapping[str, Any],
):
    """Classify bounded-run exits only from durable exact local evidence."""
    current = getattr(production_module, "run_unattended_production", None)
    if not callable(current):
        raise FullPipelineBatchAcceptanceError(
            "production module has no callable run_unattended_production"
        )
    existing = getattr(current, _RUNNER_STATE_ATTR, None)
    if isinstance(existing, dict):
        if existing.get("acceptance_sha256") != proven["acceptance_sha256"]:
            raise FullPipelineBatchAcceptanceError(
                "bounded runner recovery is already installed for different evidence"
            )
        return existing

    original_runner = current

    def wrapped_run_unattended_production(*args, **kwargs):
        try:
            return original_runner(*args, **kwargs)
        except SystemExit as exc:
            if exc.code != 1:
                raise
            if _pending_is_exact_qa_block(production_module, proven):
                # The harness inspects the pending checkpoint immediately after
                # this return and raises its explicit NotCommitted outcome.
                return None
            if _committed_is_exact_and_audit_safe(
                production_module,
                qa_root,
                proven,
            ):
                # Restart after a committed Chapter-25 may run the due audit at
                # startup and then trip the bounded-target start check because
                # next_chapter is already 26.  Exact QA + completed due audit is
                # sufficient to reconcile without reprocessing the chapter.
                return None
            raise

    state = {
        "mode": "full_pipeline_exact_batch_runner_recovery_v1",
        "acceptance_sha256": proven["acceptance_sha256"],
        "original_run_unattended_production": original_runner,
        "wrapped_run_unattended_production": wrapped_run_unattended_production,
    }
    setattr(wrapped_run_unattended_production, _RUNNER_STATE_ATTR, state)
    production_module.run_unattended_production = wrapped_run_unattended_production
    return state


def install_batch_acceptance_provenance(
    production_module,
    qa_dir: Path | str,
    evidence: Mapping[str, Any],
):
    """Install exact commit provenance plus experiment-only recovery guards."""
    proven = validate_batch_acceptance_evidence(evidence)
    qa_root = Path(qa_dir).resolve()
    current = getattr(production_module, "atomic_write_json", None)
    if not callable(current):
        raise FullPipelineBatchAcceptanceError(
            "production module has no callable atomic_write_json"
        )
    existing = getattr(current, _WRAPPER_STATE_ATTR, None)
    if isinstance(existing, dict):
        if (
            existing.get("qa_root") != qa_root
            or existing.get("acceptance_sha256") != proven["acceptance_sha256"]
        ):
            raise FullPipelineBatchAcceptanceError(
                "exact Batch acceptance writer is already installed for different evidence"
            )
        return existing

    expected_chapter = proven["chapter"]
    original = current

    def wrapped_atomic_write_json(path, data):
        candidate = Path(path)
        payload = data
        try:
            is_target = (
                candidate.resolve().parent == qa_root
                and candidate.suffix == ".json"
                and candidate.stem.isdigit()
                and int(candidate.stem) == expected_chapter
            )
        except OSError:
            is_target = False
        if is_target:
            if not isinstance(data, dict):
                raise FullPipelineBatchAcceptanceError(
                    "committed QA payload must be an object"
                )
            already = data.get(ACCEPTANCE_FIELD)
            if already is not None:
                existing_evidence = validate_batch_acceptance_evidence(already)
                if existing_evidence != proven:
                    raise FullPipelineBatchAcceptanceError(
                        "committed QA already carries different Batch acceptance evidence"
                    )
                payload = data
            else:
                payload = dict(data)
                payload[ACCEPTANCE_FIELD] = copy.deepcopy(proven)
        return original(path, payload)

    startup_validation = _install_luna_pending_startup_validation(production_module)
    runner_recovery = _install_exact_runner_recovery(
        production_module,
        qa_root,
        proven,
    )
    state = {
        "mode": ACCEPTANCE_MODE,
        "qa_root": qa_root,
        "chapter": expected_chapter,
        "acceptance_sha256": proven["acceptance_sha256"],
        "evidence": copy.deepcopy(proven),
        "original_atomic_write_json": original,
        "wrapped_atomic_write_json": wrapped_atomic_write_json,
        "startup_validation": startup_validation,
        "runner_recovery": runner_recovery,
    }
    setattr(wrapped_atomic_write_json, _WRAPPER_STATE_ATTR, state)
    production_module.atomic_write_json = wrapped_atomic_write_json
    return state
