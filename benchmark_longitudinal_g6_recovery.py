"""Recovery/continuation harness for GOAT G6 after one definitive rejection.

G6D produced 35 durable journaled Provider outcomes before stopping on a
definitive HTTP 524 at G6C035 (Tencent Hy3 Paid, Rogue Ch25). That failed
logical case is permanently preserved and MUST NOT be reissued.

G6E permits continuation of only the five originally absent logical cases
G6C036-G6C040. It never retries G6C035, never rewrites historical journals or
results, and stops again on any new ambiguous outcome or definitive rejection.

The module is dry-run by default. Importing it performs no I/O, credential
access, network activity, or writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_FREEZE_SHA256,
    G6_CALIBRATION_REVISION,
)
from benchmark_longitudinal_g6_blind import G6_BLIND_PROTOCOL_SHA256
from benchmark_longitudinal_g6_execution import (
    EXECUTION_API_KEY_ENV,
    EXECUTION_REVISION,
    EXECUTION_TIMEOUT_SECONDS,
    PROVIDER_CHAT_URL,
    G6ExecutionAuthorizationError,
    G6ExecutionPlanError,
    G6ExecutionRejectedError,
    G6RuntimeCase,
    _atomic_write_json,
    _canonical_json,
    _sha256_file,
    _sha256_json,
    assert_external_evidence_root,
    build_runtime_cases,
    evaluate_translation_response,
    execution_plan_identity,
    normalize_translation_http_result,
    read_execution_api_key,
    send_translation_http_once,
)
from production_paid_request_journal import (
    PaidRequestUncertainError,
    load_matching_journal,
    run_journaled_paid_request,
)


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_REVISION = "goat-g6-preserve-reject-continue-absent-v1"

EXPECTED_EXECUTION_PLAN_SHA256 = (
    "c47533cfc2acd1f211a23ee54ebf13594a4c52ab9947d52c6c8d2bc95aff92ca"
)
EXPECTED_EXECUTION_PLAN_FILE_SHA256 = (
    "01ec1435f2be0430839f14bf6005c22ed34544d34377f3b6e10842136c3bfd6c"
)

G6D_RECONCILIATION_JSON_SHA256 = (
    "b2b5f0c2951a494f0ee112beb65f0620847f4175cf56e3c4c2c13be6d18dffb7"
)
G6D_RECONCILIATION_TEXT_SHA256 = (
    "0d30a09fa459a8ba67ecca4852d18313f36891f2df2dd200c7987ba9d401b7a8"
)
G6D_RECONCILIATION_MANIFEST_SHA256 = (
    "8e713652f7bdccd06c0ab87d32185cbd2f8ff58be5f126c79f876d93211e53a4"
)

G6D_ORIGINAL_EXECUTION_SUMMARY_SHA256 = (
    "a01c3c1de133cf75c1db97b30b7d6f0fa4bdcf00912d82547aadc9823f2355b2"
)
G6D_FAILED_JOURNAL_SHA256 = (
    "07d09af24f08ccc66e8513e13b67bb0624a240de3a7c457ee27168816632e3bb"
)
G6D_FAILED_RESULT_SHA256 = (
    "1a5c25d17d28506b80860ce6f554485a77c4811683f469d720218bb282dfa1ef"
)

ORIGINAL_DURABLE_JOURNAL_COUNT = 35
ORIGINAL_DURABLE_RESULT_COUNT = 35
ORIGINAL_ACCEPTED_RESULT_COUNT = 34
ORIGINAL_REJECTED_RESULT_COUNT = 1

PRESERVED_REJECTED_CASE_ID = "G6C035"
PRESERVED_REJECTED_INDEX = 35
PRESERVED_REJECTED_MODEL_ID = "tencent/hy3-paid"
PRESERVED_REJECTED_NOVEL_ID = "rogue_first_prince"
PRESERVED_REJECTED_CHAPTER = 25
PRESERVED_REJECTED_HTTP_STATUS = 524
PRESERVED_REJECTION_REASONS = (
    "http_status_524",
    "provider_model_identity_mismatch",
    "missing_usage",
)

PENDING_CASE_IDS = (
    "G6C036",
    "G6C037",
    "G6C038",
    "G6C039",
    "G6C040",
)
PENDING_CASE_INDICES = (36, 37, 38, 39, 40)
RECOVERY_NEW_CALL_COUNT = 5
RECOVERY_ZDR_ENABLED = False
RECOVERY_AUTOMATIC_RETRIES = 0


class G6RecoveryError(RuntimeError):
    """Base class for G6 definitive-rejection recovery."""


class G6RecoveryEvidenceError(G6RecoveryError):
    """Frozen G6D reconciliation/evidence does not match the recovery anchor."""


class G6RecoveryStateError(G6RecoveryError):
    """Current execution state is not the exact allowed recovery state."""


class G6RecoveryNewRejectionError(G6RecoveryError):
    """A newly attempted recovery case returned a definitive rejection."""


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6RecoveryEvidenceError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise G6RecoveryEvidenceError(f"{label} must contain a JSON object")
    return value


def _require_file_sha(path: Path, expected: str, label: str) -> str:
    if not path.is_file():
        raise G6RecoveryEvidenceError(f"missing {label}: {path}")
    actual = _sha256_file(path)
    if actual != expected:
        raise G6RecoveryEvidenceError(
            f"{label} SHA256 mismatch: {actual} != {expected}"
        )
    return actual


def verify_reconciliation_evidence(
    reconciliation_root: Path | str,
    execution_root: Path | str,
) -> dict[str, Any]:
    """Bind the immutable R1 forensic evidence and its original 35 files.

    Extra execution journals/results are allowed only after recovery has begun,
    but every one of the original 35 files must still match the exact hashes
    recorded in the frozen R1 forensic JSON.
    """
    root = Path(reconciliation_root).resolve()
    execution = Path(execution_root).resolve()

    json_path = root / "g6d_reconciliation_r1.json"
    text_path = root / "g6d_reconciliation_r1.txt"
    manifest_path = root / "g6d_reconciliation_r1_manifest.json"

    observed = {
        "json": _require_file_sha(
            json_path,
            G6D_RECONCILIATION_JSON_SHA256,
            "G6D reconciliation JSON",
        ),
        "text": _require_file_sha(
            text_path,
            G6D_RECONCILIATION_TEXT_SHA256,
            "G6D reconciliation text",
        ),
        "manifest": _require_file_sha(
            manifest_path,
            G6D_RECONCILIATION_MANIFEST_SHA256,
            "G6D reconciliation manifest",
        ),
    }

    forensic = _load_json_object(json_path, "G6D reconciliation JSON")
    manifest = _load_json_object(manifest_path, "G6D reconciliation manifest")

    if manifest.get("classification") != "DEFINITIVE_PROVIDER_REJECTION_DO_NOT_RETRY":
        raise G6RecoveryEvidenceError("R1 classification is not the frozen rejection state")
    if manifest.get("automatic_reissue_authorized") is not False:
        raise G6RecoveryEvidenceError("R1 unexpectedly authorizes automatic reissue")

    durable = manifest.get("durable_state")
    if not isinstance(durable, Mapping):
        raise G6RecoveryEvidenceError("R1 durable_state is missing")
    expected_counts = {
        "journal_count": ORIGINAL_DURABLE_JOURNAL_COUNT,
        "response_recorded": ORIGINAL_DURABLE_JOURNAL_COUNT,
        "started": 0,
        "uncertain": 0,
        "planned": 0,
        "result_count": ORIGINAL_DURABLE_RESULT_COUNT,
        "accepted_results": ORIGINAL_ACCEPTED_RESULT_COUNT,
        "rejected_results": ORIGINAL_REJECTED_RESULT_COUNT,
        "journal_validation_errors": 0,
    }
    for key, expected in expected_counts.items():
        if durable.get(key) != expected:
            raise G6RecoveryEvidenceError(
                f"R1 durable_state[{key!r}] changed: {durable.get(key)!r}"
            )

    if forensic.get("journal_count") != ORIGINAL_DURABLE_JOURNAL_COUNT:
        raise G6RecoveryEvidenceError("R1 forensic journal count changed")
    if forensic.get("result_count") != ORIGINAL_DURABLE_RESULT_COUNT:
        raise G6RecoveryEvidenceError("R1 forensic result count changed")

    journals = forensic.get("journals")
    results = forensic.get("results")
    if not isinstance(journals, list) or len(journals) != ORIGINAL_DURABLE_JOURNAL_COUNT:
        raise G6RecoveryEvidenceError("R1 journal hash inventory is incomplete")
    if not isinstance(results, list) or len(results) != ORIGINAL_DURABLE_RESULT_COUNT:
        raise G6RecoveryEvidenceError("R1 result hash inventory is incomplete")

    for item in journals:
        if not isinstance(item, Mapping):
            raise G6RecoveryEvidenceError("R1 journal inventory contains a non-object")
        name = item.get("file")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise G6RecoveryEvidenceError("R1 journal inventory entry is malformed")
        path = execution / "journal" / name
        _require_file_sha(path, digest, f"original journal {name}")

    for item in results:
        if not isinstance(item, Mapping):
            raise G6RecoveryEvidenceError("R1 result inventory contains a non-object")
        name = item.get("file")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise G6RecoveryEvidenceError("R1 result inventory entry is malformed")
        path = execution / "results" / name
        _require_file_sha(path, digest, f"original result {name}")

    _require_file_sha(
        execution / "execution_plan.json",
        EXPECTED_EXECUTION_PLAN_FILE_SHA256,
        "G6 execution plan",
    )
    _require_file_sha(
        execution / "execution_summary.json",
        G6D_ORIGINAL_EXECUTION_SUMMARY_SHA256,
        "original stopped execution summary",
    )
    _require_file_sha(
        execution / "journal" / "0035.goat_g6_translation.json",
        G6D_FAILED_JOURNAL_SHA256,
        "preserved G6C035 journal",
    )
    _require_file_sha(
        execution / "results" / "0035.json",
        G6D_FAILED_RESULT_SHA256,
        "preserved G6C035 result",
    )

    return {
        "root": str(root),
        "files": observed,
        "classification": manifest["classification"],
        "original_execution_tree_sha256": (
            manifest.get("frozen_evidence", {}).get("execution_tree_sha256")
        ),
    }


def _result_path(execution_root: Path, index: int) -> Path:
    return execution_root / "results" / f"{index:04d}.json"


def _validate_preserved_rejection(
    runtime_case: G6RuntimeCase,
    journal_record: Mapping[str, Any],
) -> None:
    case = runtime_case.calibration_case
    if runtime_case.index != PRESERVED_REJECTED_INDEX:
        raise G6RecoveryStateError("unexpected durable rejected case index")
    if case.case_id != PRESERVED_REJECTED_CASE_ID:
        raise G6RecoveryStateError("unexpected preserved rejected case id")
    if case.benchmark_model_id != PRESERVED_REJECTED_MODEL_ID:
        raise G6RecoveryStateError("unexpected preserved rejected model")
    if case.novel_id != PRESERVED_REJECTED_NOVEL_ID:
        raise G6RecoveryStateError("unexpected preserved rejected novel")
    if case.chapter != PRESERVED_REJECTED_CHAPTER:
        raise G6RecoveryStateError("unexpected preserved rejected chapter")

    evaluation = evaluate_translation_response(
        journal_record["response"],
        runtime_case=runtime_case,
    )
    if evaluation["provider_accepted"]:
        raise G6RecoveryStateError("G6C035 is no longer a rejected Provider result")
    if journal_record["response"].get("http_status") != PRESERVED_REJECTED_HTTP_STATUS:
        raise G6RecoveryStateError("preserved G6C035 HTTP status changed")
    if tuple(evaluation["provider_rejection_reasons"]) != PRESERVED_REJECTION_REASONS:
        raise G6RecoveryStateError("preserved G6C035 rejection reasons changed")


def inspect_recovery_state(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Inspect all 40 frozen cases without sending or mutating anything."""
    execution = Path(execution_root).resolve()
    runtime_cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=execution,
        prompt_bundles=prompt_bundles,
    )

    records: list[dict[str, Any]] = []
    pending: list[G6RuntimeCase] = []
    accepted = 0
    preserved_rejected = 0

    for item in runtime_cases:
        record = load_matching_journal(item.journal_path, item.identity)
        if record is None:
            pending.append(item)
            records.append(
                {
                    "index": item.index,
                    "case_id": item.calibration_case.case_id,
                    "status": "absent",
                    "provider_accepted": None,
                }
            )
            continue

        status = record["status"]
        if status in {"started", "uncertain"}:
            raise PaidRequestUncertainError(
                "G6 recovery is blocked by ambiguous durable state: "
                f"{item.journal_path}"
            )
        if status != "response_recorded":
            raise G6RecoveryStateError(
                f"unexpected G6 recovery journal status {status!r}"
            )

        evaluation = evaluate_translation_response(
            record["response"],
            runtime_case=item,
        )
        if evaluation["provider_accepted"]:
            accepted += 1
            records.append(
                {
                    "index": item.index,
                    "case_id": item.calibration_case.case_id,
                    "status": "response_recorded",
                    "provider_accepted": True,
                }
            )
            continue

        if item.index == PRESERVED_REJECTED_INDEX:
            _validate_preserved_rejection(item, record)
            preserved_rejected += 1
            records.append(
                {
                    "index": item.index,
                    "case_id": item.calibration_case.case_id,
                    "status": "response_recorded",
                    "provider_accepted": False,
                    "terminal_preserved_rejection": True,
                }
            )
            continue

        raise G6RecoveryNewRejectionError(
            "G6 recovery found an additional definitive rejected response at "
            f"{item.calibration_case.case_id}; no further continuation is authorized"
        )

    pending_ids = tuple(item.calibration_case.case_id for item in pending)
    pending_indices = tuple(item.index for item in pending)

    allowed_states = {
        PENDING_CASE_IDS,
        (),
    }
    if pending_ids not in allowed_states:
        raise G6RecoveryStateError(
            f"pending recovery cases are not the frozen suffix: {pending_ids!r}"
        )
    if pending_indices not in {PENDING_CASE_INDICES, ()}:
        raise G6RecoveryStateError(
            f"pending recovery indices are not the frozen suffix: {pending_indices!r}"
        )
    if preserved_rejected != 1:
        raise G6RecoveryStateError("exactly one preserved rejected case is required")

    expected_accepted = 34 if pending else 39
    if accepted != expected_accepted:
        raise G6RecoveryStateError(
            f"unexpected accepted durable count: {accepted} != {expected_accepted}"
        )

    return {
        "case_count": len(runtime_cases),
        "accepted_count": accepted,
        "preserved_rejected_count": preserved_rejected,
        "pending_count": len(pending),
        "pending_case_ids": list(pending_ids),
        "pending_indices": list(pending_indices),
        "records": records,
    }


def recovery_plan_identity(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    reconciliation_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    verification = verify_reconciliation_evidence(
        reconciliation_root,
        execution_root,
    )
    state = inspect_recovery_state(
        repo_root=repo_root,
        execution_root=execution_root,
        prompt_bundles=prompt_bundles,
    )
    plan = execution_plan_identity(
        repo_root=repo_root,
        evidence_root=execution_root,
        prompt_bundles=prompt_bundles,
    )
    plan_sha = _sha256_json(plan)
    if plan_sha != EXPECTED_EXECUTION_PLAN_SHA256:
        raise G6RecoveryStateError(
            f"underlying G6 execution plan drifted: {plan_sha}"
        )

    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "recovery_revision": RECOVERY_REVISION,
        "execution_revision": EXECUTION_REVISION,
        "g6_calibration_revision": G6_CALIBRATION_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "reconciliation_manifest_sha256": G6D_RECONCILIATION_MANIFEST_SHA256,
        "reconciliation_classification": verification["classification"],
        "preserved_terminal_rejection": {
            "case_id": PRESERVED_REJECTED_CASE_ID,
            "index": PRESERVED_REJECTED_INDEX,
            "model_id": PRESERVED_REJECTED_MODEL_ID,
            "novel_id": PRESERVED_REJECTED_NOVEL_ID,
            "chapter": PRESERVED_REJECTED_CHAPTER,
            "http_status": PRESERVED_REJECTED_HTTP_STATUS,
            "reasons": list(PRESERVED_REJECTION_REASONS),
            "retry_authorized": False,
        },
        "pending_case_ids": state["pending_case_ids"],
        "pending_indices": state["pending_indices"],
        "new_calls_required": state["pending_count"],
        "automatic_retries": 0,
        "zdr_enabled": False,
    }


def recovery_plan_sha256(**kwargs: Any) -> str:
    return _sha256_json(recovery_plan_identity(**kwargs))


def _write_result(
    execution_root: Path,
    item: G6RuntimeCase,
    response: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> Path:
    path = _result_path(execution_root, item.index)
    if path.exists():
        raise G6RecoveryStateError(
            f"recovery refuses to overwrite existing result: {path}"
        )
    record = {
        "schema_version": 1,
        "execution_revision": EXECUTION_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "case": item.plan_identity(),
        "response": dict(response),
        "evaluation": dict(evaluation),
    }
    _atomic_write_json(path, record)
    return path


def _aggregate_usage_from_results(execution_root: Path) -> dict[str, int]:
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    for path in sorted((execution_root / "results").glob("*.json")):
        value = _load_json_object(path, f"G6 result {path.name}")
        response = value.get("response")
        if not isinstance(response, Mapping):
            continue
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in totals:
            token_value = usage.get(key)
            if (
                isinstance(token_value, int)
                and not isinstance(token_value, bool)
                and token_value >= 0
            ):
                totals[key] += token_value
    return totals


def run_recovery_suite(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    reconciliation_root: Path | str,
    recovery_control_root: Path | str,
    api_key: str,
    authorize_new_calls: int,
    sender: Callable[..., Mapping[str, Any]] = send_translation_http_once,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Send only the five frozen absent cases, preserving G6C035 forever."""
    repo = Path(repo_root).resolve()
    execution = assert_external_evidence_root(execution_root, repo)
    control = assert_external_evidence_root(recovery_control_root, repo)

    if control.exists():
        raise G6RecoveryStateError(
            "recovery control root already exists; do not overwrite prior evidence"
        )

    verify_reconciliation_evidence(reconciliation_root, execution)
    state = inspect_recovery_state(
        repo_root=repo,
        execution_root=execution,
        prompt_bundles=prompt_bundles,
    )

    if state["pending_case_ids"] != list(PENDING_CASE_IDS):
        raise G6RecoveryStateError(
            "paid recovery may start only from the exact frozen five-case pending state"
        )
    if authorize_new_calls != RECOVERY_NEW_CALL_COUNT:
        raise G6ExecutionAuthorizationError(
            f"recovery requires exactly {RECOVERY_NEW_CALL_COUNT} newly authorized calls"
        )
    if authorize_new_calls != state["pending_count"]:
        raise G6ExecutionAuthorizationError(
            "authorized recovery calls do not equal exact pending calls"
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise G6ExecutionAuthorizationError(
            f"{EXECUTION_API_KEY_ENV} is required for paid recovery"
        )

    runtime_cases = build_runtime_cases(
        repo_root=repo,
        evidence_root=execution,
        prompt_bundles=prompt_bundles,
    )
    pending_runtime = [
        item
        for item in runtime_cases
        if item.calibration_case.case_id in PENDING_CASE_IDS
    ]
    if tuple(item.index for item in pending_runtime) != PENDING_CASE_INDICES:
        raise G6RecoveryStateError("runtime pending suffix differs from recovery freeze")

    control.mkdir(parents=True, exist_ok=False)
    control_plan = recovery_plan_identity(
        repo_root=repo,
        execution_root=execution,
        reconciliation_root=reconciliation_root,
        prompt_bundles=prompt_bundles,
    )
    _atomic_write_json(control / "recovery_plan.json", control_plan)

    calls_this_run = 0
    completed_new: list[str] = []

    for item in pending_runtime:
        if item.journal_path.exists():
            raise G6RecoveryStateError(
                f"pending recovery journal unexpectedly exists: {item.journal_path}"
            )

        def prepare_attempt(item=item) -> Mapping[str, Any]:
            return {
                "recovery_revision": RECOVERY_REVISION,
                "execution_revision": EXECUTION_REVISION,
                "case_id": item.calibration_case.case_id,
                "case_index": item.index,
                "benchmark_model_id": item.calibration_case.benchmark_model_id,
                "provider_model_id": item.calibration_case.provider_model_id,
                "novel_id": item.calibration_case.novel_id,
                "source_chapter": item.calibration_case.chapter,
                "stress_axis": item.calibration_case.stress_axis,
                "effort": item.calibration_case.effort,
                "messages_sha256": item.prompt.messages_sha256,
                "request_sha256": item.identity["request_sha256"],
                "preserved_rejected_case_id": PRESERVED_REJECTED_CASE_ID,
                "automatic_retries": 0,
                "zdr_enabled": False,
            }

        def send_once(item=item) -> Mapping[str, Any]:
            return sender(
                api_key=api_key,
                request_body=item.request_body,
                url=PROVIDER_CHAT_URL,
                timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            )

        def normalize(raw, _attempt, item=item) -> Mapping[str, Any]:
            if not isinstance(raw, Mapping):
                return {
                    "http_status": None,
                    "expected_model": item.calibration_case.provider_model_id,
                    "provider_response_id": None,
                    "response_model": None,
                    "finish_reason": None,
                    "content": None,
                    "usage": None,
                    "error": None,
                    "normalization_errors": ["raw_http_result_not_mapping"],
                    "body_sha256": None,
                    "request_id": None,
                }
            return normalize_translation_http_result(
                raw,
                expected_model=item.calibration_case.provider_model_id,
            )

        journal_result = run_journaled_paid_request(
            journal_path=item.journal_path,
            identity=item.identity,
            save_json=_atomic_write_json,
            prepare_attempt=prepare_attempt,
            send_once=send_once,
            normalize_response=normalize,
        )
        if journal_result["api_called"]:
            calls_this_run += 1

        response = journal_result["response"]
        evaluation = evaluate_translation_response(
            response,
            runtime_case=item,
        )
        _write_result(execution, item, response, evaluation)

        if not evaluation["provider_accepted"]:
            summary = {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "recovery_revision": RECOVERY_REVISION,
                "status": "stopped_on_new_definitive_provider_rejection",
                "new_calls_this_run": calls_this_run,
                "completed_new_case_ids": completed_new,
                "failed_case_id": item.calibration_case.case_id,
                "failed_case_index": item.index,
                "provider_rejection_reasons": evaluation[
                    "provider_rejection_reasons"
                ],
                "preserved_original_rejection_case_id": PRESERVED_REJECTED_CASE_ID,
                "automatic_reissue_authorized": False,
            }
            _atomic_write_json(control / "recovery_summary.json", summary)
            raise G6RecoveryNewRejectionError(
                "G6 recovery stopped on a new definitive Provider rejection at "
                f"{item.calibration_case.case_id}; do not rerun automatically"
            )

        completed_new.append(item.calibration_case.case_id)

    final_state = inspect_recovery_state(
        repo_root=repo,
        execution_root=execution,
        prompt_bundles=prompt_bundles,
    )
    if final_state["pending_count"] != 0:
        raise G6RecoveryStateError("G6 recovery completed sends but pending cases remain")
    if final_state["accepted_count"] != 39:
        raise G6RecoveryStateError("G6 recovery expected exactly 39 accepted cases")
    if final_state["preserved_rejected_count"] != 1:
        raise G6RecoveryStateError("G6 recovery lost preserved terminal rejection")

    summary = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "recovery_revision": RECOVERY_REVISION,
        "status": "recovery_complete_with_preserved_terminal_rejection",
        "total_logical_cases": 40,
        "accepted_count": 39,
        "preserved_terminal_rejection_count": 1,
        "preserved_terminal_rejection_case_id": PRESERVED_REJECTED_CASE_ID,
        "new_calls_this_run": calls_this_run,
        "completed_new_case_ids": completed_new,
        "pending_count": 0,
        "automatic_retries": 0,
        "zdr_enabled": False,
        "aggregate_usage": _aggregate_usage_from_results(execution),
        "blind_scoring_authorized": False,
        "identity_reveal_authorized": False,
    }
    _atomic_write_json(control / "recovery_summary.json", summary)

    result_hashes = {
        path.name: _sha256_file(path)
        for path in sorted((execution / "results").glob("*.json"))
    }
    journal_hashes = {
        path.name: _sha256_file(path)
        for path in sorted((execution / "journal").glob("*.json"))
    }
    manifest = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "recovery_revision": RECOVERY_REVISION,
        "recovery_plan_sha256": _sha256_json(control_plan),
        "recovery_plan_file_sha256": _sha256_file(control / "recovery_plan.json"),
        "recovery_summary_sha256": _sha256_file(control / "recovery_summary.json"),
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "preserved_terminal_rejection_case_id": PRESERVED_REJECTED_CASE_ID,
        "new_provider_calls_this_run": calls_this_run,
        "automatic_retries": 0,
        "credentials_persisted": False,
        "zdr_enabled": False,
        "journal_files": journal_hashes,
        "result_files": result_hashes,
    }
    _atomic_write_json(control / "recovery_manifest.json", manifest)
    return summary


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continue only absent G6 cases after preserved G6C035 rejection."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--execution-root", required=True)
    parser.add_argument("--reconciliation-root", required=True)
    parser.add_argument("--recovery-control-root", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorize-new-calls", type=int)
    parser.add_argument("--confirm-recovery-revision")
    parser.add_argument("--confirm-execution-plan-sha")
    parser.add_argument("--confirm-reconciliation-manifest-sha")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = recovery_plan_identity(
        repo_root=args.repo_root,
        execution_root=args.execution_root,
        reconciliation_root=args.reconciliation_root,
    )
    plan_sha = _sha256_json(plan)

    print(f"recovery_revision={RECOVERY_REVISION}")
    print(f"g6_calibration_freeze_sha256={G6_CALIBRATION_FREEZE_SHA256}")
    print(f"g6_blind_protocol_sha256={G6_BLIND_PROTOCOL_SHA256}")
    print(f"execution_plan_sha256={EXPECTED_EXECUTION_PLAN_SHA256}")
    print(f"recovery_plan_sha256={plan_sha}")
    print(f"preserved_rejected_case_id={PRESERVED_REJECTED_CASE_ID}")
    print(f"preserved_rejected_http_status={PRESERVED_REJECTED_HTTP_STATUS}")
    print("preserved_rejected_retry_authorized=False")
    print(f"pending_count={plan['new_calls_required']}")
    print("pending_case_ids=" + ",".join(plan["pending_case_ids"]))

    if not args.execute:
        if args.authorize_new_calls is not None:
            raise G6ExecutionAuthorizationError(
                "--authorize-new-calls is valid only together with --execute"
            )
        print("execution=disabled")
        return 0

    if args.confirm_recovery_revision != RECOVERY_REVISION:
        raise G6ExecutionAuthorizationError(
            "paid recovery requires the exact recovery revision"
        )
    if args.confirm_execution_plan_sha != EXPECTED_EXECUTION_PLAN_SHA256:
        raise G6ExecutionAuthorizationError(
            "paid recovery requires the exact original G6 execution-plan SHA"
        )
    if (
        args.confirm_reconciliation_manifest_sha
        != G6D_RECONCILIATION_MANIFEST_SHA256
    ):
        raise G6ExecutionAuthorizationError(
            "paid recovery requires the exact frozen reconciliation manifest SHA"
        )
    if args.authorize_new_calls is None:
        raise G6ExecutionAuthorizationError(
            "--authorize-new-calls is required with --execute"
        )

    summary = run_recovery_suite(
        repo_root=args.repo_root,
        execution_root=args.execution_root,
        reconciliation_root=args.reconciliation_root,
        recovery_control_root=args.recovery_control_root,
        api_key=read_execution_api_key(),
        authorize_new_calls=args.authorize_new_calls,
    )
    print(f"status={summary['status']}")
    print(f"new_calls_this_run={summary['new_calls_this_run']}")
    print(f"accepted_count={summary['accepted_count']}")
    print(
        "preserved_terminal_rejection_count="
        f"{summary['preserved_terminal_rejection_count']}"
    )
    print(f"pending_count={summary['pending_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
