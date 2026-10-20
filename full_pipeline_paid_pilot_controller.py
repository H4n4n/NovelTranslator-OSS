"""Budget-enforcing execution controller for the chronological Luna paid pilot.

Phase 60I freezes an immutable authorization envelope.  This module consumes
that exact envelope and enforces its operation/cost ceilings *before* each
provider boundary.  It does not create credentials and performs no provider
work on import.

The controller is deliberately conservative:

* every remote API call consumes its operation slot before transport;
* every model request reserves a worst-case Luna cost before transport;
* an exception or unverifiable response never gives a slot or reservation back;
* only exact, supported usage evidence converts a reservation to actual cost;
* Batch reservations survive upload/create ambiguity and are finalized only
  from exact collected local evidence;
* all ledger mutations and provider calls are serialized by an OS-backed
  experiment lock so two processes cannot authorize against the same balance.

This is a spending safety envelope, not an exactly-once billing claim.  Existing
Batch ambiguity protocols and production paid-request/domain recovery protocols
remain authoritative for whether a logical request may be reissued.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import errno
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import full_pipeline_batch_wave as batch
import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_luna_runtime as luna
import full_pipeline_paid_pilot_preflight as preflight
from noveltranslator_core import atomic_write_json


CONTROLLER_SCHEMA_VERSION = 1
CONTROLLER_REVISION = "full-pipeline-paid-pilot-budget-controller-v1"
LEDGER_FILENAME = "paid_pilot_execution_ledger.json"
LOCK_FILENAME = ".paid_pilot_controller.lock"

# Current GPT-5.6 Luna API limits used only for a conservative *authorization
# reserve*.  Actual supported responses are still accounted by NovelTranslator's
# exact saved pricing evidence.  A response crossing the long-context threshold
# is deliberately left reserved/fail-closed until the runtime pricing contract
# is explicitly upgraded for that tier.
LUNA_CONTEXT_WINDOW_TOKENS = 1_050_000
LUNA_MAX_OUTPUT_TOKENS = 128_000
LUNA_LONG_CONTEXT_THRESHOLD_TOKENS = 272_000
WORST_STANDARD_INPUT_RATE_PER_MILLION = Decimal("0.40")
WORST_STANDARD_OUTPUT_RATE_PER_MILLION = Decimal("1.80")

_REMOTE_KINDS = (
    "batch_input_upload",
    "batch_create",
    "batch_status_read",
    "batch_reconciliation_list_read",
    "batch_output_download",
    "sync_model_call",
)
_CAP_FIELDS = {
    "batch_input_upload": "batch_input_upload_attempts_cap",
    "batch_create": "batch_create_attempts_cap",
    "batch_status_read": "batch_status_reads_cap",
    "batch_reconciliation_list_read": "batch_reconciliation_list_reads_cap",
    "batch_output_download": "batch_output_downloads_cap",
    "sync_model_call": "sync_model_calls_cap",
}
_LOCKED_OPERATION_STATUSES = frozenset(
    {"started", "uncertain", "identity_unverified", "accounting_unverified", "pricing_unsupported"}
)
_LOCKED_BATCH_STATUSES = frozenset(
    {"reserved", "active", "uncertain", "transport_blocked", "cost_unverified"}
)


class FullPipelinePaidPilotControllerError(RuntimeError):
    """Controller identity, budget, or provider evidence is unsafe."""


class PaidPilotBudgetExceeded(FullPipelinePaidPilotControllerError):
    """The next remote/model operation would exceed a frozen ceiling."""


class PaidPilotOperationUncertain(FullPipelinePaidPilotControllerError):
    """A remote operation consumed authorization but its outcome is uncertain."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FullPipelinePaidPilotControllerError(
            "controller identity is not canonically JSON-serializable"
        ) from exc


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return _sha256_text(token)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FullPipelinePaidPilotControllerError(
            f"{label} is not a valid decimal"
        ) from exc
    if not result.is_finite() or result < 0:
        raise FullPipelinePaidPilotControllerError(
            f"{label} must be finite and non-negative"
        )
    return result


def _money(value: Any) -> str:
    amount = _decimal(value, "money")
    text = format(amount.quantize(Decimal("0.000000000001")), "f")
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _ledger_path(experiment_root: Path | str) -> Path:
    return Path(experiment_root).resolve() / LEDGER_FILENAME


def _record_hash(value: Mapping[str, Any]) -> str:
    stable = {
        key: copy.deepcopy(item)
        for key, item in dict(value).items()
        if key != "ledger_sha256"
    }
    return _sha256_text(_canonical_json(stable))


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["ledger_sha256"] = _record_hash(result)
    return result


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullPipelinePaidPilotControllerError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FullPipelinePaidPilotControllerError(f"{label} must contain an object")
    return value


def _validate_ledger_hash(value: Mapping[str, Any], path: Path) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    if result.get("schema_version") != CONTROLLER_SCHEMA_VERSION:
        raise FullPipelinePaidPilotControllerError(
            f"unsupported paid-pilot controller schema at {path}"
        )
    if result.get("controller_revision") != CONTROLLER_REVISION:
        raise FullPipelinePaidPilotControllerError(
            f"unsupported paid-pilot controller revision at {path}"
        )
    observed = result.get("ledger_sha256")
    if not isinstance(observed, str) or observed != _record_hash(result):
        raise FullPipelinePaidPilotControllerError(
            f"paid-pilot execution ledger hash mismatch at {path}"
        )
    return result


def _exclusive_write_ledger(path: Path, value: Mapping[str, Any]) -> None:
    document = _with_hash(value)
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FullPipelinePaidPilotControllerError(
            f"paid-pilot execution ledger already exists: {path}"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Final-named partial evidence remains blocking; never overwrite it.
        raise
    if path.read_bytes() != payload:
        raise FullPipelinePaidPilotControllerError(
            "paid-pilot execution ledger readback differs from published bytes"
        )


def _persist_ledger(path: Path, ledger: Mapping[str, Any]) -> dict[str, Any]:
    document = _with_hash(ledger)
    atomic_write_json(path, document)
    observed = _validate_ledger_hash(
        _read_object(path, "paid-pilot execution ledger"), path
    )
    if observed != document:
        raise FullPipelinePaidPilotControllerError(
            "paid-pilot execution ledger readback differs after atomic write"
        )
    return observed


@contextmanager
def _controller_lock(experiment_root: Path | str):
    """Serialize budget authorization across cooperating controller processes."""
    root = Path(experiment_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_FILENAME
    if path.is_symlink():
        raise FullPipelinePaidPilotControllerError(
            f"paid-pilot controller lock must not be a symlink: {path}"
        )
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise FullPipelinePaidPilotControllerError(
                    "another paid-pilot controller process owns the experiment lock"
                ) from exc
            raise
        yield path


def _budget_snapshot(ledger: Mapping[str, Any]) -> dict[str, str]:
    actual = Decimal("0")
    locked = Decimal("0")
    for operation in ledger.get("operations", []):
        if not isinstance(operation, Mapping):
            continue
        if operation.get("kind") != "sync_model_call":
            continue
        status = str(operation.get("status") or "")
        if status == "finalized":
            actual += _decimal(operation.get("actual_cost_usd", "0"), "actual sync cost")
        elif status in _LOCKED_OPERATION_STATUSES:
            locked += _decimal(operation.get("reserve_usd", "0"), "sync reserve")
    for reservation in ledger.get("batch_reservations", {}).values():
        if not isinstance(reservation, Mapping):
            continue
        status = str(reservation.get("status") or "")
        if status == "finalized":
            actual += _decimal(reservation.get("actual_cost_usd", "0"), "actual Batch cost")
        elif status in _LOCKED_BATCH_STATUSES:
            locked += _decimal(reservation.get("reserve_usd", "0"), "Batch reserve")
    return {
        "actual_cost_usd": _money(actual),
        "locked_reserve_usd": _money(locked),
        "authorized_exposure_usd": _money(actual + locked),
    }


def _refresh_budget_summary(ledger: dict[str, Any]) -> None:
    ledger["budget_summary"] = _budget_snapshot(ledger)


def _load_preflight_bound(
    experiment_root: Path,
    repository_root: Path,
    expected_repo_anchor: str,
) -> dict[str, Any]:
    return preflight.load_paid_pilot_preflight(
        experiment_root,
        repository_root,
        expected_repo_anchor=expected_repo_anchor,
    )


def _load_ledger_unlocked(
    experiment_root: Path,
    repository_root: Path,
    authorization_token: str,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    path = _ledger_path(experiment_root)
    if not path.is_file():
        raise FullPipelinePaidPilotControllerError(
            "paid-pilot execution ledger has not been initialized"
        )
    ledger = _validate_ledger_hash(
        _read_object(path, "paid-pilot execution ledger"), path
    )
    token_hash = _token_hash(str(authorization_token))
    if ledger.get("authorization_token_sha256") != token_hash:
        raise FullPipelinePaidPilotControllerError(
            "paid-pilot controller authorization token does not match the ledger"
        )
    frozen = _load_preflight_bound(
        experiment_root,
        repository_root,
        str(ledger.get("repo_anchor") or ""),
    )
    if ledger.get("preflight_sha256") != frozen.get("preflight_sha256"):
        raise FullPipelinePaidPilotControllerError(
            "execution ledger belongs to a different paid-pilot preflight"
        )
    if ledger.get("experiment_id") != frozen.get("experiment_id"):
        raise FullPipelinePaidPilotControllerError(
            "execution ledger belongs to a different experiment"
        )
    if ledger.get("operation_budget") != frozen.get("operation_budget"):
        raise FullPipelinePaidPilotControllerError(
            "execution ledger operation ceilings differ from the frozen preflight"
        )
    if ledger.get("max_total_paid_cost_usd") != frozen.get(
        "max_total_paid_cost_usd"
    ):
        raise FullPipelinePaidPilotControllerError(
            "execution ledger cost ceiling differs from the frozen preflight"
        )
    return ledger, frozen, path


def initialize_paid_pilot_execution(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    expected_repo_anchor: str,
    authorization_token: str,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Create the execution ledger once, before the first remote operation.

    Re-calling after creation is an idempotent validation/load path and does not
    require the experiment to remain at S0.
    """
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    with _controller_lock(root):
        path = _ledger_path(root)
        if path.exists():
            ledger, _frozen, _path = _load_ledger_unlocked(
                root, repo, authorization_token
            )
            return ledger

        frozen = preflight.validate_paid_pilot_preflight_for_first_provider_operation(
            root,
            repo,
            expected_repo_anchor=expected_repo_anchor,
            authorization_token=authorization_token,
        )
        if luna.LUNA_MODEL != batch.BATCH_MODEL:
            raise FullPipelinePaidPilotControllerError(
                "Batch and synchronous Luna model identities differ"
            )
        if (
            Decimal(str(luna.LUNA_STANDARD_CACHE_MISS_RATE)) != Decimal("0.20")
            or Decimal(str(luna.LUNA_STANDARD_CACHE_HIT_RATE)) != Decimal("0.02")
            or Decimal(str(luna.LUNA_STANDARD_OUTPUT_RATE)) != Decimal("1.20")
        ):
            raise FullPipelinePaidPilotControllerError(
                "repository Luna standard pricing no longer matches the controller reserve contract"
            )
        created = now().astimezone(timezone.utc).isoformat()
        ledger = {
            "schema_version": CONTROLLER_SCHEMA_VERSION,
            "controller_revision": CONTROLLER_REVISION,
            "experiment_id": frozen["experiment_id"],
            "repo_anchor": frozen["repo_anchor"],
            "preflight_sha256": frozen["preflight_sha256"],
            "authorization_token_sha256": _token_hash(authorization_token),
            "created_at_utc": created,
            "updated_at_utc": created,
            "pilot_scope": copy.deepcopy(frozen["pilot_scope"]),
            "operation_budget": copy.deepcopy(frozen["operation_budget"]),
            "max_total_paid_cost_usd": frozen["max_total_paid_cost_usd"],
            "reserve_contract": {
                "model": luna.LUNA_MODEL,
                "context_window_tokens": LUNA_CONTEXT_WINDOW_TOKENS,
                "max_output_tokens": LUNA_MAX_OUTPUT_TOKENS,
                "long_context_threshold_tokens": LUNA_LONG_CONTEXT_THRESHOLD_TOKENS,
                "worst_input_rate_per_million": _money(
                    WORST_STANDARD_INPUT_RATE_PER_MILLION
                ),
                "worst_output_rate_per_million": _money(
                    WORST_STANDARD_OUTPUT_RATE_PER_MILLION
                ),
                "policy": "full-context-standard-long-context-worst-case-v1",
            },
            "counts": {kind: 0 for kind in _REMOTE_KINDS},
            "provider_api_calls_started": 0,
            "paid_model_requests_started": 0,
            "batch_model_requests_started": 0,
            "operations": [],
            "batch_reservations": {},
            "budget_summary": {
                "actual_cost_usd": "0",
                "locked_reserve_usd": "0",
                "authorized_exposure_usd": "0",
            },
        }
        _exclusive_write_ledger(path, ledger)
        return _validate_ledger_hash(
            _read_object(path, "paid-pilot execution ledger"), path
        )


def load_paid_pilot_execution(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    authorization_token: str,
) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    with _controller_lock(root):
        ledger, _frozen, _path = _load_ledger_unlocked(
            root, repo, authorization_token
        )
        expected = _budget_snapshot(ledger)
        if ledger.get("budget_summary") != expected:
            raise FullPipelinePaidPilotControllerError(
                "paid-pilot budget summary does not match durable operation evidence"
            )
        return ledger


def _max_cost(ledger: Mapping[str, Any]) -> Decimal:
    return _decimal(ledger["max_total_paid_cost_usd"], "frozen max paid cost")


def _ensure_budget_for_reserve(
    ledger: Mapping[str, Any], reserve: Decimal, label: str
) -> None:
    snapshot = _budget_snapshot(ledger)
    exposure = _decimal(snapshot["authorized_exposure_usd"], "authorized exposure")
    if exposure + reserve > _max_cost(ledger):
        raise PaidPilotBudgetExceeded(
            f"{label} would exceed frozen paid-cost ceiling: "
            f"current exposure ${_money(exposure)}, new reserve ${_money(reserve)}, "
            f"cap ${ledger['max_total_paid_cost_usd']}"
        )


def _request_reserve(max_completion_tokens: Any) -> Decimal:
    if (
        not isinstance(max_completion_tokens, int)
        or isinstance(max_completion_tokens, bool)
        or max_completion_tokens < 1
        or max_completion_tokens > LUNA_MAX_OUTPUT_TOKENS
    ):
        raise FullPipelinePaidPilotControllerError(
            "Luna request max_completion_tokens is outside the supported controller bound"
        )
    return (
        Decimal(LUNA_CONTEXT_WINDOW_TOKENS)
        / Decimal(1_000_000)
        * WORST_STANDARD_INPUT_RATE_PER_MILLION
        + Decimal(max_completion_tokens)
        / Decimal(1_000_000)
        * WORST_STANDARD_OUTPUT_RATE_PER_MILLION
    )


def _wave_target(root: Path, wave_number: int) -> Path:
    return root / "waves" / f"wave_{int(wave_number):04d}"


def _read_wave_manifest(root: Path, wave_number: int) -> dict[str, Any]:
    return batch._read_manifest(_wave_target(root, wave_number))


def _pilot_wave_allowed(frozen: Mapping[str, Any], wave_number: int) -> None:
    scope = frozen.get("pilot_scope")
    if not isinstance(scope, Mapping):
        raise FullPipelinePaidPilotControllerError("preflight lacks pilot scope")
    start = int(scope.get("start_wave", 0) or 0)
    end = int(scope.get("end_wave", 0) or 0)
    if wave_number < start or wave_number > end:
        raise FullPipelinePaidPilotControllerError(
            f"Wave {wave_number} is outside frozen paid-pilot scope {start}..{end}"
        )


def _batch_reserve_for_manifest(manifest: Mapping[str, Any]) -> Decimal:
    requests = manifest.get("requests")
    if not isinstance(requests, list) or not requests:
        raise FullPipelinePaidPilotControllerError(
            "prepared Batch wave contains no request identities"
        )
    total = Decimal("0")
    for item in requests:
        if not isinstance(item, Mapping):
            raise FullPipelinePaidPilotControllerError(
                "prepared Batch request identity is malformed"
            )
        # The actual request body is frozen in batch_input.jsonl, while the
        # manifest intentionally retains only body SHA.  Primary experiment
        # requests have the shared exact Luna output cap.
        total += _request_reserve(luna.LUNA_MAX_TOKENS)
    return total


def _ensure_batch_reservation(
    root: Path,
    repo: Path,
    token: str,
    wave_number: int,
) -> dict[str, Any]:
    with _controller_lock(root):
        ledger, frozen, path = _load_ledger_unlocked(root, repo, token)
        _pilot_wave_allowed(frozen, wave_number)
        manifest = _read_wave_manifest(root, wave_number)
        if manifest.get("model") != luna.LUNA_MODEL:
            raise FullPipelinePaidPilotControllerError(
                "prepared Batch wave does not use exact Luna model"
            )
        request_count = int(manifest.get("request_count", 0) or 0)
        plan = next(
            (
                item
                for item in frozen.get("wave_plan", [])
                if isinstance(item, Mapping)
                and item.get("wave_number") == wave_number
            ),
            None,
        )
        if not isinstance(plan, Mapping) or request_count != int(
            plan.get("batch_model_request_count", 0) or 0
        ):
            raise FullPipelinePaidPilotControllerError(
                "prepared Batch request count differs from the frozen pilot plan"
            )
        key = str(wave_number)
        existing = ledger["batch_reservations"].get(key)
        reserve = _batch_reserve_for_manifest(manifest)
        expected = {
            "wave_number": wave_number,
            "run_id": manifest["run_id"],
            "input_sha256": manifest["input_sha256"],
            "request_count": request_count,
            "reserve_usd": _money(reserve),
        }
        if isinstance(existing, Mapping):
            for field, value in expected.items():
                if existing.get(field) != value:
                    raise FullPipelinePaidPilotControllerError(
                        f"Wave {wave_number} Batch reservation identity changed for {field}"
                    )
            return copy.deepcopy(dict(existing))
        _ensure_budget_for_reserve(ledger, reserve, f"Wave {wave_number} Batch")
        ledger["batch_reservations"][key] = {
            **expected,
            "status": "reserved",
            "actual_cost_usd": "0",
            "reconciled_submission": False,
        }
        _refresh_budget_summary(ledger)
        ledger["updated_at_utc"] = utc_now().isoformat()
        _persist_ledger(path, ledger)
        return copy.deepcopy(ledger["batch_reservations"][key])


def _start_operation(
    ledger: dict[str, Any],
    *,
    kind: str,
    context: Mapping[str, Any],
    reserve: Decimal = Decimal("0"),
    paid_model_increment: int = 0,
    batch_model_increment: int = 0,
) -> dict[str, Any]:
    if kind not in _CAP_FIELDS:
        raise FullPipelinePaidPilotControllerError(f"unsupported remote operation {kind!r}")
    budget = ledger["operation_budget"]
    cap = int(budget[_CAP_FIELDS[kind]])
    used = int(ledger["counts"].get(kind, 0) or 0)
    if used >= cap:
        raise PaidPilotBudgetExceeded(
            f"{kind} cap exhausted ({used}/{cap}); provider call blocked"
        )
    provider_cap = int(budget["provider_api_call_cap"])
    provider_used = int(ledger.get("provider_api_calls_started", 0) or 0)
    if provider_used >= provider_cap:
        raise PaidPilotBudgetExceeded(
            f"provider API call cap exhausted ({provider_used}/{provider_cap})"
        )
    paid_cap = int(budget["paid_model_request_cap"])
    paid_used = int(ledger.get("paid_model_requests_started", 0) or 0)
    if paid_used + paid_model_increment > paid_cap:
        raise PaidPilotBudgetExceeded(
            "paid model request cap would be exceeded"
        )
    batch_cap = int(budget["batch_model_requests_exact"])
    batch_used = int(ledger.get("batch_model_requests_started", 0) or 0)
    if batch_used + batch_model_increment > batch_cap:
        raise PaidPilotBudgetExceeded(
            "Batch model request count would exceed the frozen pilot total"
        )
    if reserve:
        _ensure_budget_for_reserve(ledger, reserve, kind)

    sequence = len(ledger["operations"]) + 1
    operation = {
        "operation_id": f"paid-pilot-op-{sequence:06d}",
        "kind": kind,
        "status": "started",
        "context": copy.deepcopy(dict(context)),
        "reserve_usd": _money(reserve),
        "actual_cost_usd": "0",
        "started_at_utc": utc_now().isoformat(),
    }
    ledger["operations"].append(operation)
    ledger["counts"][kind] = used + 1
    ledger["provider_api_calls_started"] = provider_used + 1
    ledger["paid_model_requests_started"] = paid_used + paid_model_increment
    ledger["batch_model_requests_started"] = batch_used + batch_model_increment
    _refresh_budget_summary(ledger)
    return operation


def _operation_by_id(ledger: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for operation in ledger["operations"]:
        if operation.get("operation_id") == operation_id:
            return operation
    raise FullPipelinePaidPilotControllerError(
        f"controller operation disappeared from ledger: {operation_id}"
    )


def _complete_non_model_operation(
    operation: dict[str, Any], *, status: str = "completed", evidence: Mapping[str, Any] | None = None
) -> None:
    operation["status"] = status
    operation["completed_at_utc"] = utc_now().isoformat()
    if evidence:
        operation["evidence"] = copy.deepcopy(dict(evidence))


def _mark_uncertain(operation: dict[str, Any], exc: BaseException) -> None:
    operation["status"] = "uncertain"
    operation["error"] = {
        "type": type(exc).__name__,
        "message": str(exc)[:1000],
    }
    operation["completed_at_utc"] = utc_now().isoformat()


def _standard_actual_cost(response: Any) -> tuple[Decimal | None, dict[str, Any]]:
    model = _field(response, "model", "")
    usage = _field(response, "usage", None)
    evidence: dict[str, Any] = {"provider_response_model": model or ""}
    if model != luna.LUNA_MODEL:
        evidence["reason"] = "provider_model_identity_mismatch"
        return None, evidence
    if usage is None:
        evidence["reason"] = "provider_usage_missing"
        return None, evidence
    try:
        prompt = int(_field(usage, "prompt_tokens", 0) or 0)
        completion = int(_field(usage, "completion_tokens", 0) or 0)
        details = _field(usage, "prompt_tokens_details", {}) or {}
        cached = int(_field(details, "cached_tokens", 0) or 0)
    except (TypeError, ValueError) as exc:
        evidence["reason"] = "provider_usage_invalid"
        evidence["error"] = str(exc)
        return None, evidence
    if prompt < 0 or completion < 0 or cached < 0 or cached > prompt:
        evidence["reason"] = "provider_usage_invalid"
        return None, evidence
    evidence.update(
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": cached,
            "cache_miss_tokens": prompt - cached,
        }
    )
    if prompt > LUNA_LONG_CONTEXT_THRESHOLD_TOKENS:
        evidence["reason"] = "long_context_pricing_not_supported_by_runtime_contract"
        return None, evidence
    cost = (
        Decimal(cached) / Decimal(1_000_000) * Decimal(str(luna.LUNA_STANDARD_CACHE_HIT_RATE))
        + Decimal(prompt - cached)
        / Decimal(1_000_000)
        * Decimal(str(luna.LUNA_STANDARD_CACHE_MISS_RATE))
        + Decimal(completion)
        / Decimal(1_000_000)
        * Decimal(str(luna.LUNA_STANDARD_OUTPUT_RATE))
    )
    evidence["pricing_version"] = luna.LUNA_STANDARD_PRICING_VERSION
    evidence["actual_cost_usd"] = _money(cost)
    return cost, evidence


def _finalize_sync_operation(
    ledger: dict[str, Any], operation: dict[str, Any], response: Any
) -> str:
    actual, evidence = _standard_actual_cost(response)
    operation["evidence"] = evidence
    operation["completed_at_utc"] = utc_now().isoformat()
    if actual is None:
        reason = evidence.get("reason")
        operation["status"] = (
            "identity_unverified"
            if reason == "provider_model_identity_mismatch"
            else "pricing_unsupported"
            if reason == "long_context_pricing_not_supported_by_runtime_contract"
            else "accounting_unverified"
        )
        _refresh_budget_summary(ledger)
        return operation["status"]
    reserve = _decimal(operation["reserve_usd"], "sync reserve")
    if actual > reserve:
        operation["status"] = "accounting_unverified"
        operation["evidence"]["reason"] = "actual_cost_exceeded_worst_case_reserve"
        _refresh_budget_summary(ledger)
        return operation["status"]
    operation["actual_cost_usd"] = _money(actual)
    operation["status"] = "finalized"
    _refresh_budget_summary(ledger)
    return "finalized"


class _PageProxy:
    def __init__(self, controller: "BudgetEnforcingOpenAIClient", page: Any):
        self._controller = controller
        self._page = page

    def __getattr__(self, name: str) -> Any:
        if name == "get_next_page":
            return self.get_next_page
        return getattr(self._page, name)

    def get_next_page(self):
        getter = getattr(self._page, "get_next_page", None)
        if not callable(getter):
            raise FullPipelinePaidPilotControllerError(
                "provider Batch page exposes no get_next_page()"
            )
        page = self._controller._remote_non_model(
            "batch_reconciliation_list_read",
            getter,
            context={"pagination": "next_page"},
        )
        return _PageProxy(self._controller, page)


class _FilesProxy:
    def __init__(self, controller: "BudgetEnforcingOpenAIClient"):
        self._controller = controller

    def create(self, **kwargs):
        return self._controller._remote_non_model(
            "batch_input_upload",
            lambda: self._controller._raw.files.create(**kwargs),
        )

    def content(self, file_id):
        return self._controller._remote_non_model(
            "batch_output_download",
            lambda: self._controller._raw.files.content(file_id),
            context={"file_id": str(file_id)},
        )


class _BatchesProxy:
    def __init__(self, controller: "BudgetEnforcingOpenAIClient"):
        self._controller = controller

    def create(self, **kwargs):
        return self._controller._remote_batch_create(kwargs)

    def retrieve(self, batch_id):
        return self._controller._remote_non_model(
            "batch_status_read",
            lambda: self._controller._raw.batches.retrieve(batch_id),
            context={"batch_id": str(batch_id)},
        )

    def list(self, **kwargs):
        page = self._controller._remote_non_model(
            "batch_reconciliation_list_read",
            lambda: self._controller._raw.batches.list(**kwargs),
            context={"pagination": "first_page"},
        )
        return _PageProxy(self._controller, page)


class _ChatCompletionsProxy:
    def __init__(self, controller: "BudgetEnforcingOpenAIClient"):
        self._controller = controller

    def create(self, **kwargs):
        return self._controller._remote_sync_model(kwargs)


class _ChatProxy:
    def __init__(self, controller: "BudgetEnforcingOpenAIClient"):
        self.completions = _ChatCompletionsProxy(controller)


class BudgetEnforcingOpenAIClient:
    """Expose only the OpenAI surfaces used by the frozen paid-pilot contract."""

    def __init__(
        self,
        raw_client: Any,
        *,
        experiment_root: Path | str,
        repository_root: Path | str,
        authorization_token: str,
        wave_number: int,
        novel_id: str | None = None,
    ):
        self._raw = raw_client
        self._root = Path(experiment_root).resolve()
        self._repo = Path(repository_root).resolve()
        self._token = authorization_token
        self._wave = int(wave_number)
        self._novel_id = novel_id
        self.files = _FilesProxy(self)
        self.batches = _BatchesProxy(self)
        self.chat = _ChatProxy(self)

    def _base_context(self) -> dict[str, Any]:
        value = {"wave_number": self._wave}
        if self._novel_id:
            value["novel_id"] = self._novel_id
        return value

    def _remote_non_model(
        self,
        kind: str,
        callback: Callable[[], Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any:
        with _controller_lock(self._root):
            ledger, _frozen, path = _load_ledger_unlocked(
                self._root, self._repo, self._token
            )
            operation = _start_operation(
                ledger,
                kind=kind,
                context={**self._base_context(), **dict(context or {})},
            )
            _persist_ledger(path, ledger)
            try:
                result = callback()
            except BaseException as exc:
                current = _operation_by_id(ledger, operation["operation_id"])
                _mark_uncertain(current, exc)
                _refresh_budget_summary(ledger)
                ledger["updated_at_utc"] = utc_now().isoformat()
                _persist_ledger(path, ledger)
                raise
            current = _operation_by_id(ledger, operation["operation_id"])
            _complete_non_model_operation(current)
            _refresh_budget_summary(ledger)
            ledger["updated_at_utc"] = utc_now().isoformat()
            _persist_ledger(path, ledger)
            return result

    def _remote_batch_create(self, kwargs: Mapping[str, Any]) -> Any:
        with _controller_lock(self._root):
            ledger, frozen, path = _load_ledger_unlocked(
                self._root, self._repo, self._token
            )
            _pilot_wave_allowed(frozen, self._wave)
            reservation = ledger["batch_reservations"].get(str(self._wave))
            if not isinstance(reservation, dict):
                raise FullPipelinePaidPilotControllerError(
                    "Batch create has no durable controller reservation"
                )
            if reservation.get("status") != "reserved":
                raise PaidPilotOperationUncertain(
                    f"Wave {self._wave} Batch reservation is {reservation.get('status')!r}; "
                    "automatic create reissue is blocked"
                )
            request_count = int(reservation["request_count"])
            operation = _start_operation(
                ledger,
                kind="batch_create",
                context={
                    **self._base_context(),
                    "run_id": reservation["run_id"],
                    "request_count": request_count,
                },
                paid_model_increment=request_count,
                batch_model_increment=request_count,
            )
            reservation["status"] = "active"
            reservation["create_operation_id"] = operation["operation_id"]
            _refresh_budget_summary(ledger)
            _persist_ledger(path, ledger)
            try:
                result = self._raw.batches.create(**dict(kwargs))
            except BaseException as exc:
                current = _operation_by_id(ledger, operation["operation_id"])
                _mark_uncertain(current, exc)
                reservation["status"] = "uncertain"
                _refresh_budget_summary(ledger)
                ledger["updated_at_utc"] = utc_now().isoformat()
                _persist_ledger(path, ledger)
                raise
            current = _operation_by_id(ledger, operation["operation_id"])
            _complete_non_model_operation(
                current,
                evidence={"batch_id": str(_field(result, "id", "") or "")},
            )
            reservation["status"] = "active"
            reservation["batch_id"] = str(_field(result, "id", "") or "")
            _refresh_budget_summary(ledger)
            ledger["updated_at_utc"] = utc_now().isoformat()
            _persist_ledger(path, ledger)
            return result

    def _remote_sync_model(self, kwargs: Mapping[str, Any]) -> Any:
        request = copy.deepcopy(dict(kwargs))
        if request.get("model") != luna.LUNA_MODEL:
            raise FullPipelinePaidPilotControllerError(
                "controller permits only exact GPT-5.6 Luna synchronous requests"
            )
        max_tokens = request.get("max_completion_tokens")
        reserve = _request_reserve(max_tokens)
        request_sha = _sha256_text(_canonical_json(request))
        with _controller_lock(self._root):
            ledger, frozen, path = _load_ledger_unlocked(
                self._root, self._repo, self._token
            )
            _pilot_wave_allowed(frozen, self._wave)
            operation = _start_operation(
                ledger,
                kind="sync_model_call",
                context={
                    **self._base_context(),
                    "request_sha256": request_sha,
                    "max_completion_tokens": max_tokens,
                },
                reserve=reserve,
                paid_model_increment=1,
            )
            _persist_ledger(path, ledger)
            try:
                response = self._raw.chat.completions.create(**request)
            except BaseException as exc:
                current = _operation_by_id(ledger, operation["operation_id"])
                _mark_uncertain(current, exc)
                _refresh_budget_summary(ledger)
                ledger["updated_at_utc"] = utc_now().isoformat()
                _persist_ledger(path, ledger)
                raise
            current = _operation_by_id(ledger, operation["operation_id"])
            status = _finalize_sync_operation(ledger, current, response)
            ledger["updated_at_utc"] = utc_now().isoformat()
            _persist_ledger(path, ledger)
            if status == "pricing_unsupported":
                raise FullPipelinePaidPilotControllerError(
                    "Luna response crossed the supported long-context pricing threshold; "
                    "worst-case reserve remains locked and downstream accounting is blocked"
                )
            return response


def _mark_batch_transport_blocked(
    root: Path, repo: Path, token: str, wave_number: int
) -> None:
    with _controller_lock(root):
        ledger, _frozen, path = _load_ledger_unlocked(root, repo, token)
        reservation = ledger["batch_reservations"].get(str(wave_number))
        if isinstance(reservation, dict) and reservation.get("status") == "reserved":
            reservation["status"] = "transport_blocked"
            _refresh_budget_summary(ledger)
            ledger["updated_at_utc"] = utc_now().isoformat()
            _persist_ledger(path, ledger)


def prepare_pilot_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    authorization_token: str,
    now: Callable[[], Any] = batch.utc_now,
):
    """Freeze one in-scope chronological wave without provider access."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    ledger = load_paid_pilot_execution(root, repo, authorization_token=authorization_token)
    frozen = preflight.load_paid_pilot_preflight(
        root, repo, expected_repo_anchor=ledger["repo_anchor"]
    )
    _pilot_wave_allowed(frozen, wave_number)
    if wave_number == 1:
        requests = v3.build_wave_requests(root, repo, 1)
        current = preflight._request_preview(requests)
        expected = frozen["wave_plan"][0].get("request_previews")
        if current != expected:
            raise FullPipelinePaidPilotControllerError(
                "Wave 1 request identity differs from the frozen paid-pilot preflight"
            )
    return v3.prepare_experiment_wave(root, repo, wave_number, now=now)


def submit_pilot_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    raw_client: Any,
    *,
    authorization_token: str,
):
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    _ensure_batch_reservation(root, repo, authorization_token, wave_number)
    client = BudgetEnforcingOpenAIClient(
        raw_client,
        experiment_root=root,
        repository_root=repo,
        authorization_token=authorization_token,
        wave_number=wave_number,
    )
    try:
        return v3.submit_experiment_wave(
            root,
            repo,
            wave_number,
            client,
            batch.SUBMIT_AUTHORIZATION,
        )
    except BaseException:
        # Upload ambiguity or any failure before Batch create leaves a conservative
        # reservation locked.  If create was attempted its proxy already changed
        # the reservation to active/uncertain and this helper is a no-op.
        _mark_batch_transport_blocked(root, repo, authorization_token, wave_number)
        raise


def reconcile_pilot_wave_submission(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    raw_client: Any,
    *,
    authorization_token: str,
):
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    client = BudgetEnforcingOpenAIClient(
        raw_client,
        experiment_root=root,
        repository_root=repo,
        authorization_token=authorization_token,
        wave_number=wave_number,
    )
    result = v3.reconcile_experiment_wave_submission(
        root, repo, wave_number, client
    )
    with _controller_lock(root):
        ledger, _frozen, path = _load_ledger_unlocked(
            root, repo, authorization_token
        )
        reservation = ledger["batch_reservations"].get(str(wave_number))
        if not isinstance(reservation, dict):
            raise FullPipelinePaidPilotControllerError(
                "reconciled Batch has no controller reservation"
            )
        if reservation.get("status") not in {"uncertain", "active"}:
            raise FullPipelinePaidPilotControllerError(
                "reconciled Batch reservation has incompatible status"
            )
        reservation["status"] = "active"
        reservation["reconciled_submission"] = True
        reservation["batch_id"] = result.get("batch_id")
        _refresh_budget_summary(ledger)
        ledger["updated_at_utc"] = utc_now().isoformat()
        _persist_ledger(path, ledger)
    return result


def refresh_pilot_wave_status(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    raw_client: Any,
    *,
    authorization_token: str,
):
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    client = BudgetEnforcingOpenAIClient(
        raw_client,
        experiment_root=root,
        repository_root=repo,
        authorization_token=authorization_token,
        wave_number=wave_number,
    )
    return v3.refresh_experiment_wave_status(root, repo, wave_number, client)


def _validated_batch_actual(document: Mapping[str, Any]) -> Decimal:
    if document.get("model") != luna.LUNA_MODEL or document.get("transport") != "openai_batch":
        raise FullPipelinePaidPilotControllerError(
            "collected Batch evidence has wrong model/transport"
        )
    requests = document.get("requests")
    if not isinstance(requests, list) or not requests:
        raise FullPipelinePaidPilotControllerError(
            "collected Batch evidence contains no requests"
        )
    total = Decimal("0")
    for item in requests:
        if not isinstance(item, Mapping):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch request evidence is malformed"
            )
        stats = luna.validate_saved_luna_stats(item.get("stats", {}))
        if stats.get("pricing_version") != batch.BATCH_PRICING_VERSION:
            raise FullPipelinePaidPilotControllerError(
                "collected request is not exact Batch-priced Luna evidence"
            )
        total += _decimal(stats["cost_usd"], "Batch request actual cost")
    totals = document.get("totals")
    if isinstance(totals, Mapping):
        observed = _decimal(totals.get("cost_usd", "0"), "Batch totals cost")
        if abs(observed - total) > Decimal("0.000000000001"):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch total cost differs from per-request exact accounting"
            )
    return total


def _finalize_batch_reservation(
    root: Path,
    repo: Path,
    token: str,
    wave_number: int,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    actual = _validated_batch_actual(document)
    with _controller_lock(root):
        ledger, _frozen, path = _load_ledger_unlocked(root, repo, token)
        reservation = ledger["batch_reservations"].get(str(wave_number))
        if not isinstance(reservation, dict):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch has no controller reservation"
            )
        if reservation.get("run_id") != document.get("run_id"):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch run ID differs from controller reservation"
            )
        if reservation.get("status") == "finalized":
            if _decimal(reservation.get("actual_cost_usd", "0"), "saved Batch cost") != actual:
                raise FullPipelinePaidPilotControllerError(
                    "already-finalized Batch cost differs from current exact evidence"
                )
            return ledger
        reserve = _decimal(reservation["reserve_usd"], "Batch reserve")
        if actual > reserve:
            reservation["status"] = "cost_unverified"
            _refresh_budget_summary(ledger)
            _persist_ledger(path, ledger)
            raise FullPipelinePaidPilotControllerError(
                "actual Batch cost exceeded its worst-case controller reserve"
            )
        reservation["actual_cost_usd"] = _money(actual)
        reservation["status"] = "finalized"
        reservation["batch_id"] = document.get("batch_id")
        reservation["finalized_at_utc"] = utc_now().isoformat()
        _refresh_budget_summary(ledger)
        ledger["updated_at_utc"] = utc_now().isoformat()
        return _persist_ledger(path, ledger)


def collect_pilot_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    raw_client: Any,
    *,
    authorization_token: str,
    now: Callable[[], Any] = batch.utc_now,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    client = BudgetEnforcingOpenAIClient(
        raw_client,
        experiment_root=root,
        repository_root=repo,
        authorization_token=authorization_token,
        wave_number=wave_number,
    )
    document, manifest = v3.collect_experiment_wave(
        root,
        repo,
        wave_number,
        client,
        now=now,
        crash_hook=crash_hook,
    )
    _finalize_batch_reservation(
        root, repo, authorization_token, wave_number, document
    )
    return document, manifest


def reconcile_local_cost_evidence(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    authorization_token: str,
) -> dict[str, Any]:
    """Finalize any Batch reservation whose exact collected evidence is local.

    Synchronous calls intentionally remain locked if the controller itself did
    not durably finalize their provider response.  Domain journals/recovery
    protocols decide request reissue safety, while the locked reserve preserves
    the global spending ceiling without guessing a lost cost.
    """
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    ledger = load_paid_pilot_execution(root, repo, authorization_token=authorization_token)
    for key, reservation in list(ledger.get("batch_reservations", {}).items()):
        if not isinstance(reservation, Mapping) or reservation.get("status") == "finalized":
            continue
        wave_number = int(key)
        target = _wave_target(root, wave_number)
        manifest_path = target / batch.MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = batch._read_manifest(target)
        if manifest.get("status") not in {"collection_results_recorded", "collected"}:
            continue
        try:
            document = batch._load_collected_local(target, manifest)
        except Exception:
            continue
        _finalize_batch_reservation(
            root, repo, authorization_token, wave_number, document
        )
    return load_paid_pilot_execution(root, repo, authorization_token=authorization_token)


def process_pilot_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    authorization_token: str,
    client_factory: Callable[[str, int], Any],
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    """Advance the real lifecycle with metered synchronous Luna boundaries."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    ledger = load_paid_pilot_execution(root, repo, authorization_token=authorization_token)
    frozen = preflight.load_paid_pilot_preflight(
        root, repo, expected_repo_anchor=ledger["repo_anchor"]
    )
    _pilot_wave_allowed(frozen, wave_number)

    def metered_factory(novel_id: str, chapter: int):
        raw = client_factory(novel_id, chapter)
        return BudgetEnforcingOpenAIClient(
            raw,
            experiment_root=root,
            repository_root=repo,
            authorization_token=authorization_token,
            wave_number=wave_number,
            novel_id=novel_id,
        )

    return v3.process_collected_wave(
        root,
        repo,
        wave_number,
        client_factory=metered_factory,
        crash_hook=crash_hook,
    )
