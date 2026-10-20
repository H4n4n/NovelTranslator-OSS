"""Second GOAT G6 continuation after two terminal Tencent HTTP-524 outcomes.

The frozen R2 reconciliation proves that G6C035 and G6C036 are definitive,
durable Provider rejections. They MUST NOT be reissued. Only G6C037-G6C040
remain absent, and all four belong to MiMo V2.5 Pro at native/default effort.

This module is dry-run by default. Importing it performs no I/O, credential
access, network activity, model inference, or writes.
"""

from __future__ import annotations

import argparse
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
    G6RuntimeCase,
    _atomic_write_json,
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
from benchmark_longitudinal_g6_recovery import (
    EXPECTED_EXECUTION_PLAN_FILE_SHA256,
    EXPECTED_EXECUTION_PLAN_SHA256,
    G6D_ORIGINAL_EXECUTION_SUMMARY_SHA256,
    PRESERVED_REJECTION_REASONS,
    _aggregate_usage_from_results,
    _load_json_object,
    _require_file_sha,
    _write_result,
)
from production_paid_request_journal import (
    PaidRequestUncertainError,
    load_matching_journal,
    run_journaled_paid_request,
)


SECOND_RECOVERY_SCHEMA_VERSION = 1
SECOND_RECOVERY_REVISION = "goat-g6-preserve-two-rejects-continue-mimo-v1"

R2_RECONCILIATION_JSON_SHA256 = (
    "6e6cfeed307e8949088cf1547e763622cd87fd899b7dbe5d85cb099a0221c796"
)
R2_RECONCILIATION_TEXT_SHA256 = (
    "ce70959697d6197c27f66e03cfcf334574486cf2376c214d1be20490cf28bfe7"
)
R2_RECONCILIATION_MANIFEST_SHA256 = (
    "da2491d46666509e5c89979b50deaed42713f8fb9706fd9eb093ebc9d091cfd7"
)

R1_RECOVERY_PLAN_FILE_SHA256 = (
    "b236b06f74cb9eb58ff762ee592227c24e4df794adc1f27cc2f6c25b5790c43b"
)
R1_RECOVERY_SUMMARY_SHA256 = (
    "cf11296a674a5b588c36e62ea693cb043e969db28afa110cd5acc76d021a3773"
)

G6C035_JOURNAL_SHA256 = (
    "07d09af24f08ccc66e8513e13b67bb0624a240de3a7c457ee27168816632e3bb"
)
G6C035_RESULT_SHA256 = (
    "1a5c25d17d28506b80860ce6f554485a77c4811683f469d720218bb282dfa1ef"
)
G6C036_JOURNAL_SHA256 = (
    "bf62c3786f125ecd2a63e6fa459098afcdf06437d61424f071e155604145424d"
)
G6C036_RESULT_SHA256 = (
    "acbb37b41ca7b82edfab204cf4f838ec1a57bcc42d9c6048f573bb1cb1150138"
)

FROZEN_DURABLE_COUNT = 36
FROZEN_ACCEPTED_COUNT = 34
FROZEN_REJECTED_COUNT = 2

TERMINAL_REJECTIONS = (
    {
        "case_id": "G6C035",
        "index": 35,
        "model_id": "tencent/hy3-paid",
        "novel_id": "rogue_first_prince",
        "chapter": 25,
        "http_status": 524,
        "journal_sha256": G6C035_JOURNAL_SHA256,
        "result_sha256": G6C035_RESULT_SHA256,
    },
    {
        "case_id": "G6C036",
        "index": 36,
        "model_id": "tencent/hy3-paid",
        "novel_id": "rogue_first_prince",
        "chapter": 14,
        "http_status": 524,
        "journal_sha256": G6C036_JOURNAL_SHA256,
        "result_sha256": G6C036_RESULT_SHA256,
    },
)

PENDING_CASE_IDS = ("G6C037", "G6C038", "G6C039", "G6C040")
PENDING_CASE_INDICES = (37, 38, 39, 40)
PENDING_MODEL_ID = "xiaomi/mimo-v2.5-pro"
PENDING_CASE_SPECS = (
    ("G6C037", 37, "ace_evolution", 18),
    ("G6C038", 38, "ace_evolution", 2),
    ("G6C039", 39, "rogue_first_prince", 25),
    ("G6C040", 40, "rogue_first_prince", 14),
)
SECOND_RECOVERY_NEW_CALL_COUNT = 4
SECOND_RECOVERY_ZDR_ENABLED = False
SECOND_RECOVERY_AUTOMATIC_RETRIES = 0


class G6SecondRecoveryError(RuntimeError):
    """Base class for the four-case MiMo-only continuation."""


class G6SecondRecoveryEvidenceError(G6SecondRecoveryError):
    """Frozen R2 evidence does not match the required recovery anchor."""


class G6SecondRecoveryStateError(G6SecondRecoveryError):
    """Current durable state is not the exact allowed second-recovery state."""


class G6SecondRecoveryNewRejectionError(G6SecondRecoveryError):
    """A newly attempted MiMo case returned a definitive rejection."""


def verify_r2_evidence(
    reconciliation_root: Path | str,
    execution_root: Path | str,
    first_recovery_control_root: Path | str,
) -> dict[str, Any]:
    """Bind R2 plus every one of the original 36 durable journals/results."""
    root = Path(reconciliation_root).resolve()
    execution = Path(execution_root).resolve()
    recovery_control = Path(first_recovery_control_root).resolve()

    json_path = root / "g6f_reconciliation_r2.json"
    text_path = root / "g6f_reconciliation_r2.txt"
    manifest_path = root / "g6f_reconciliation_r2_manifest.json"

    observed = {
        "json": _require_file_sha(
            json_path,
            R2_RECONCILIATION_JSON_SHA256,
            "G6F R2 reconciliation JSON",
        ),
        "text": _require_file_sha(
            text_path,
            R2_RECONCILIATION_TEXT_SHA256,
            "G6F R2 reconciliation text",
        ),
        "manifest": _require_file_sha(
            manifest_path,
            R2_RECONCILIATION_MANIFEST_SHA256,
            "G6F R2 reconciliation manifest",
        ),
    }

    forensic = _load_json_object(json_path, "G6F R2 reconciliation JSON")
    manifest = _load_json_object(manifest_path, "G6F R2 reconciliation manifest")

    if manifest.get("classification") != "TWO_DEFINITIVE_PROVIDER_REJECTIONS_FOUR_ABSENT":
        raise G6SecondRecoveryEvidenceError("R2 classification changed")

    for field in (
        "retry_of_G6C035_authorized",
        "retry_of_G6C036_authorized",
        "automatic_reissue_authorized",
    ):
        if manifest.get(field) is not False:
            raise G6SecondRecoveryEvidenceError(
                f"R2 unexpectedly authorizes {field}"
            )

    durable = manifest.get("durable_state")
    if not isinstance(durable, Mapping):
        raise G6SecondRecoveryEvidenceError("R2 durable_state is missing")

    expected = {
        "logical_case_count": 36,
        "journal_count": 36,
        "response_recorded": 36,
        "started": 0,
        "uncertain": 0,
        "result_count": 36,
        "accepted_results": 34,
        "rejected_results": 2,
        "pending_count": 4,
    }
    for key, value in expected.items():
        if durable.get(key) != value:
            raise G6SecondRecoveryEvidenceError(
                f"R2 durable_state[{key!r}] changed: {durable.get(key)!r}"
            )

    if durable.get("rejected_case_ids") != ["G6C035", "G6C036"]:
        raise G6SecondRecoveryEvidenceError("R2 rejected case set changed")
    if durable.get("pending_case_ids") != list(PENDING_CASE_IDS):
        raise G6SecondRecoveryEvidenceError("R2 pending case set changed")

    journals = forensic.get("journals")
    results = forensic.get("results")
    if not isinstance(journals, list) or len(journals) != FROZEN_DURABLE_COUNT:
        raise G6SecondRecoveryEvidenceError("R2 journal inventory is incomplete")
    if not isinstance(results, list) or len(results) != FROZEN_DURABLE_COUNT:
        raise G6SecondRecoveryEvidenceError("R2 result inventory is incomplete")

    for item in journals:
        if not isinstance(item, Mapping):
            raise G6SecondRecoveryEvidenceError("malformed R2 journal inventory")
        name = item.get("file")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise G6SecondRecoveryEvidenceError("malformed R2 journal hash entry")
        _require_file_sha(
            execution / "journal" / name,
            digest,
            f"R2 original journal {name}",
        )

    for item in results:
        if not isinstance(item, Mapping):
            raise G6SecondRecoveryEvidenceError("malformed R2 result inventory")
        name = item.get("file")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise G6SecondRecoveryEvidenceError("malformed R2 result hash entry")
        _require_file_sha(
            execution / "results" / name,
            digest,
            f"R2 original result {name}",
        )

    _require_file_sha(
        execution / "execution_plan.json",
        EXPECTED_EXECUTION_PLAN_FILE_SHA256,
        "original G6 execution plan",
    )
    _require_file_sha(
        execution / "execution_summary.json",
        G6D_ORIGINAL_EXECUTION_SUMMARY_SHA256,
        "original stopped G6 execution summary",
    )
    _require_file_sha(
        execution / "journal" / "0035.goat_g6_translation.json",
        G6C035_JOURNAL_SHA256,
        "terminal G6C035 journal",
    )
    _require_file_sha(
        execution / "results" / "0035.json",
        G6C035_RESULT_SHA256,
        "terminal G6C035 result",
    )
    _require_file_sha(
        execution / "journal" / "0036.goat_g6_translation.json",
        G6C036_JOURNAL_SHA256,
        "terminal G6C036 journal",
    )
    _require_file_sha(
        execution / "results" / "0036.json",
        G6C036_RESULT_SHA256,
        "terminal G6C036 result",
    )
    _require_file_sha(
        recovery_control / "recovery_plan.json",
        R1_RECOVERY_PLAN_FILE_SHA256,
        "first recovery plan",
    )
    _require_file_sha(
        recovery_control / "recovery_summary.json",
        R1_RECOVERY_SUMMARY_SHA256,
        "first recovery stopped summary",
    )
    if (recovery_control / "recovery_manifest.json").exists():
        raise G6SecondRecoveryEvidenceError(
            "first recovery manifest unexpectedly exists"
        )

    return {
        "classification": manifest["classification"],
        "files": observed,
        "pending_case_ids": list(PENDING_CASE_IDS),
    }


def _terminal_spec(index: int) -> Mapping[str, Any] | None:
    for spec in TERMINAL_REJECTIONS:
        if spec["index"] == index:
            return spec
    return None


def _validate_terminal_rejection(
    runtime_case: G6RuntimeCase,
    journal_record: Mapping[str, Any],
) -> None:
    spec = _terminal_spec(runtime_case.index)
    if spec is None:
        raise G6SecondRecoveryStateError(
            f"case {runtime_case.index} is not a frozen terminal rejection"
        )

    case = runtime_case.calibration_case
    if case.case_id != spec["case_id"]:
        raise G6SecondRecoveryStateError("terminal case id changed")
    if case.benchmark_model_id != spec["model_id"]:
        raise G6SecondRecoveryStateError("terminal model id changed")
    if case.novel_id != spec["novel_id"]:
        raise G6SecondRecoveryStateError("terminal novel id changed")
    if case.chapter != spec["chapter"]:
        raise G6SecondRecoveryStateError("terminal chapter changed")

    response = journal_record["response"]
    evaluation = evaluate_translation_response(
        response,
        runtime_case=runtime_case,
    )
    if evaluation["provider_accepted"]:
        raise G6SecondRecoveryStateError(
            f"{case.case_id} is no longer rejected"
        )
    if response.get("http_status") != spec["http_status"]:
        raise G6SecondRecoveryStateError(
            f"{case.case_id} HTTP status changed"
        )
    if tuple(evaluation["provider_rejection_reasons"]) != PRESERVED_REJECTION_REASONS:
        raise G6SecondRecoveryStateError(
            f"{case.case_id} rejection reasons changed"
        )


def inspect_second_recovery_state(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Inspect all 40 cases while treating 35 and 36 as terminal outcomes."""
    execution = Path(execution_root).resolve()
    runtime_cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=execution,
        prompt_bundles=prompt_bundles,
    )

    accepted = 0
    terminal_rejected = 0
    pending: list[G6RuntimeCase] = []
    records: list[dict[str, Any]] = []

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
                "G6 second recovery is blocked by ambiguous durable state: "
                f"{item.journal_path}"
            )
        if status != "response_recorded":
            raise G6SecondRecoveryStateError(
                f"unexpected journal status {status!r}"
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

        if item.index in (35, 36):
            _validate_terminal_rejection(item, record)
            terminal_rejected += 1
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

        raise G6SecondRecoveryNewRejectionError(
            "G6 second recovery found an additional definitive rejection at "
            f"{item.calibration_case.case_id}; no automatic continuation is authorized"
        )

    pending_ids = tuple(item.calibration_case.case_id for item in pending)
    pending_indices = tuple(item.index for item in pending)

    if pending_ids not in {PENDING_CASE_IDS, ()}:
        raise G6SecondRecoveryStateError(
            f"pending cases are not the frozen MiMo suffix: {pending_ids!r}"
        )
    if pending_indices not in {PENDING_CASE_INDICES, ()}:
        raise G6SecondRecoveryStateError(
            f"pending indices are not the frozen MiMo suffix: {pending_indices!r}"
        )

    if pending:
        observed_specs = tuple(
            (
                item.calibration_case.case_id,
                item.index,
                item.calibration_case.novel_id,
                item.calibration_case.chapter,
            )
            for item in pending
        )
        if observed_specs != PENDING_CASE_SPECS:
            raise G6SecondRecoveryStateError(
                f"pending MiMo case identities changed: {observed_specs!r}"
            )
        for item in pending:
            if item.calibration_case.benchmark_model_id != PENDING_MODEL_ID:
                raise G6SecondRecoveryStateError(
                    f"pending case {item.calibration_case.case_id} is not MiMo V2.5 Pro"
                )
            if item.calibration_case.effort is not None:
                raise G6SecondRecoveryStateError(
                    f"pending case {item.calibration_case.case_id} is not native/default effort"
                )

    if terminal_rejected != 2:
        raise G6SecondRecoveryStateError(
            f"expected two terminal rejections, found {terminal_rejected}"
        )

    expected_accepted = 34 if pending else 38
    if accepted != expected_accepted:
        raise G6SecondRecoveryStateError(
            f"unexpected accepted count: {accepted} != {expected_accepted}"
        )

    return {
        "case_count": len(runtime_cases),
        "accepted_count": accepted,
        "terminal_rejected_count": terminal_rejected,
        "terminal_rejected_case_ids": ["G6C035", "G6C036"],
        "pending_count": len(pending),
        "pending_case_ids": list(pending_ids),
        "pending_indices": list(pending_indices),
        "records": records,
    }


def second_recovery_plan_identity(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    reconciliation_root: Path | str,
    first_recovery_control_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    verification = verify_r2_evidence(
        reconciliation_root,
        execution_root,
        first_recovery_control_root,
    )
    state = inspect_second_recovery_state(
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
        raise G6SecondRecoveryStateError(
            f"underlying G6 execution plan drifted: {plan_sha}"
        )

    return {
        "schema_version": SECOND_RECOVERY_SCHEMA_VERSION,
        "second_recovery_revision": SECOND_RECOVERY_REVISION,
        "execution_revision": EXECUTION_REVISION,
        "g6_calibration_revision": G6_CALIBRATION_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "r2_reconciliation_manifest_sha256": R2_RECONCILIATION_MANIFEST_SHA256,
        "r2_classification": verification["classification"],
        "terminal_rejections": [
            {
                "case_id": spec["case_id"],
                "index": spec["index"],
                "model_id": spec["model_id"],
                "novel_id": spec["novel_id"],
                "chapter": spec["chapter"],
                "http_status": spec["http_status"],
                "journal_sha256": spec["journal_sha256"],
                "result_sha256": spec["result_sha256"],
                "retry_authorized": False,
            }
            for spec in TERMINAL_REJECTIONS
        ],
        "pending_case_ids": state["pending_case_ids"],
        "pending_indices": state["pending_indices"],
        "new_calls_required": state["pending_count"],
        "automatic_retries": 0,
        "zdr_enabled": False,
    }


def second_recovery_plan_sha256(**kwargs: Any) -> str:
    return _sha256_json(second_recovery_plan_identity(**kwargs))


def run_second_recovery_suite(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
    reconciliation_root: Path | str,
    first_recovery_control_root: Path | str,
    second_recovery_control_root: Path | str,
    api_key: str,
    authorize_new_calls: int,
    sender: Callable[..., Mapping[str, Any]] = send_translation_http_once,
    prompt_bundles: Mapping[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Send only G6C037-G6C040 while preserving 35/36 forever."""
    repo = Path(repo_root).resolve()
    execution = assert_external_evidence_root(execution_root, repo)
    control = assert_external_evidence_root(second_recovery_control_root, repo)

    if control.exists():
        raise G6SecondRecoveryStateError(
            "second recovery control root already exists; do not overwrite it"
        )

    verify_r2_evidence(
        reconciliation_root,
        execution,
        first_recovery_control_root,
    )
    state = inspect_second_recovery_state(
        repo_root=repo,
        execution_root=execution,
        prompt_bundles=prompt_bundles,
    )

    if state["pending_case_ids"] != list(PENDING_CASE_IDS):
        raise G6SecondRecoveryStateError(
            "paid second recovery may start only from exact G6C037-G6C040 pending state"
        )
    if authorize_new_calls != SECOND_RECOVERY_NEW_CALL_COUNT:
        raise G6ExecutionAuthorizationError(
            f"second recovery requires exactly {SECOND_RECOVERY_NEW_CALL_COUNT} calls"
        )
    if authorize_new_calls != state["pending_count"]:
        raise G6ExecutionAuthorizationError(
            "authorized calls do not equal exact pending count"
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise G6ExecutionAuthorizationError(
            f"{EXECUTION_API_KEY_ENV} is required for paid second recovery"
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
        raise G6SecondRecoveryStateError(
            "runtime pending cases differ from frozen MiMo suffix"
        )

    control.mkdir(parents=True, exist_ok=False)
    control_plan = second_recovery_plan_identity(
        repo_root=repo,
        execution_root=execution,
        reconciliation_root=reconciliation_root,
        first_recovery_control_root=first_recovery_control_root,
        prompt_bundles=prompt_bundles,
    )
    _atomic_write_json(control / "second_recovery_plan.json", control_plan)

    calls_this_run = 0
    completed_new: list[str] = []

    for item in pending_runtime:
        if item.journal_path.exists():
            raise G6SecondRecoveryStateError(
                f"pending journal unexpectedly exists: {item.journal_path}"
            )

        def prepare_attempt(item=item) -> Mapping[str, Any]:
            return {
                "second_recovery_revision": SECOND_RECOVERY_REVISION,
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
                "terminal_rejections": ["G6C035", "G6C036"],
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

        def normalize(raw: Any, _attempt: Any, item=item) -> Mapping[str, Any]:
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
                "schema_version": SECOND_RECOVERY_SCHEMA_VERSION,
                "second_recovery_revision": SECOND_RECOVERY_REVISION,
                "status": "stopped_on_new_definitive_provider_rejection",
                "new_calls_this_run": calls_this_run,
                "completed_new_case_ids": completed_new,
                "failed_case_id": item.calibration_case.case_id,
                "failed_case_index": item.index,
                "provider_rejection_reasons": evaluation[
                    "provider_rejection_reasons"
                ],
                "preserved_terminal_rejection_case_ids": [
                    "G6C035",
                    "G6C036",
                ],
                "automatic_reissue_authorized": False,
            }
            _atomic_write_json(
                control / "second_recovery_summary.json",
                summary,
            )
            raise G6SecondRecoveryNewRejectionError(
                "G6 MiMo-only recovery stopped on a new definitive rejection at "
                f"{item.calibration_case.case_id}; do not rerun automatically"
            )

        completed_new.append(item.calibration_case.case_id)

    final_state = inspect_second_recovery_state(
        repo_root=repo,
        execution_root=execution,
        prompt_bundles=prompt_bundles,
    )
    if final_state["pending_count"] != 0:
        raise G6SecondRecoveryStateError(
            "second recovery sent calls but pending cases remain"
        )
    if final_state["accepted_count"] != 38:
        raise G6SecondRecoveryStateError(
            "second recovery expected exactly 38 accepted cases"
        )
    if final_state["terminal_rejected_count"] != 2:
        raise G6SecondRecoveryStateError(
            "second recovery lost the two terminal rejections"
        )

    summary = {
        "schema_version": SECOND_RECOVERY_SCHEMA_VERSION,
        "second_recovery_revision": SECOND_RECOVERY_REVISION,
        "status": "second_recovery_complete_with_two_terminal_rejections",
        "total_logical_cases": 40,
        "accepted_count": 38,
        "terminal_rejection_count": 2,
        "terminal_rejection_case_ids": ["G6C035", "G6C036"],
        "new_calls_this_run": calls_this_run,
        "completed_new_case_ids": completed_new,
        "pending_count": 0,
        "automatic_retries": 0,
        "zdr_enabled": False,
        "aggregate_usage": _aggregate_usage_from_results(execution),
        "blind_scoring_authorized": False,
        "identity_reveal_authorized": False,
    }
    _atomic_write_json(control / "second_recovery_summary.json", summary)

    manifest = {
        "schema_version": SECOND_RECOVERY_SCHEMA_VERSION,
        "second_recovery_revision": SECOND_RECOVERY_REVISION,
        "second_recovery_plan_sha256": _sha256_json(control_plan),
        "second_recovery_plan_file_sha256": _sha256_file(
            control / "second_recovery_plan.json"
        ),
        "second_recovery_summary_sha256": _sha256_file(
            control / "second_recovery_summary.json"
        ),
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "r2_reconciliation_manifest_sha256": R2_RECONCILIATION_MANIFEST_SHA256,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "terminal_rejection_case_ids": ["G6C035", "G6C036"],
        "new_provider_calls_this_run": calls_this_run,
        "automatic_retries": 0,
        "credentials_persisted": False,
        "zdr_enabled": False,
        "journal_files": {
            path.name: _sha256_file(path)
            for path in sorted((execution / "journal").glob("*.json"))
        },
        "result_files": {
            path.name: _sha256_file(path)
            for path in sorted((execution / "results").glob("*.json"))
        },
    }
    _atomic_write_json(
        control / "second_recovery_manifest.json",
        manifest,
    )
    return summary


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continue only G6C037-G6C040 after preserved G6C035/G6C036 "
            "definitive rejections."
        )
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--execution-root", required=True)
    parser.add_argument("--reconciliation-root", required=True)
    parser.add_argument("--first-recovery-control-root", required=True)
    parser.add_argument("--second-recovery-control-root", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorize-new-calls", type=int)
    parser.add_argument("--confirm-second-recovery-revision")
    parser.add_argument("--confirm-execution-plan-sha")
    parser.add_argument("--confirm-r2-reconciliation-manifest-sha")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = second_recovery_plan_identity(
        repo_root=args.repo_root,
        execution_root=args.execution_root,
        reconciliation_root=args.reconciliation_root,
        first_recovery_control_root=args.first_recovery_control_root,
    )
    plan_sha = _sha256_json(plan)

    print(f"second_recovery_revision={SECOND_RECOVERY_REVISION}")
    print(f"g6_calibration_freeze_sha256={G6_CALIBRATION_FREEZE_SHA256}")
    print(f"g6_blind_protocol_sha256={G6_BLIND_PROTOCOL_SHA256}")
    print(f"execution_plan_sha256={EXPECTED_EXECUTION_PLAN_SHA256}")
    print(f"second_recovery_plan_sha256={plan_sha}")
    print("terminal_rejected_case_ids=G6C035,G6C036")
    print("terminal_rejected_retry_authorized=False")
    print(f"pending_count={plan['new_calls_required']}")
    print("pending_case_ids=" + ",".join(plan["pending_case_ids"]))

    if not args.execute:
        if args.authorize_new_calls is not None:
            raise G6ExecutionAuthorizationError(
                "--authorize-new-calls is valid only together with --execute"
            )
        print("execution=disabled")
        return 0

    if args.confirm_second_recovery_revision != SECOND_RECOVERY_REVISION:
        raise G6ExecutionAuthorizationError(
            "paid second recovery requires the exact revision"
        )
    if args.confirm_execution_plan_sha != EXPECTED_EXECUTION_PLAN_SHA256:
        raise G6ExecutionAuthorizationError(
            "paid second recovery requires the exact original execution-plan SHA"
        )
    if (
        args.confirm_r2_reconciliation_manifest_sha
        != R2_RECONCILIATION_MANIFEST_SHA256
    ):
        raise G6ExecutionAuthorizationError(
            "paid second recovery requires the exact R2 reconciliation manifest SHA"
        )
    if args.authorize_new_calls is None:
        raise G6ExecutionAuthorizationError(
            "--authorize-new-calls is required with --execute"
        )

    summary = run_second_recovery_suite(
        repo_root=args.repo_root,
        execution_root=args.execution_root,
        reconciliation_root=args.reconciliation_root,
        first_recovery_control_root=args.first_recovery_control_root,
        second_recovery_control_root=args.second_recovery_control_root,
        api_key=read_execution_api_key(),
        authorize_new_calls=args.authorize_new_calls,
    )
    print(f"status={summary['status']}")
    print(f"new_calls_this_run={summary['new_calls_this_run']}")
    print(f"accepted_count={summary['accepted_count']}")
    print(f"terminal_rejection_count={summary['terminal_rejection_count']}")
    print(f"pending_count={summary['pending_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
