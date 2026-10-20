"""Phase 60J hardened paid-pilot accounting facade.

This module keeps the already-tested controller/transport/recovery machinery from
``full_pipeline_paid_pilot_controller`` and replaces only the two Batch
accounting boundaries that must be exact before the first paid pilot:

* reservation is derived from the immutable ``batch_input.jsonl`` request
  bodies, not a shared assumed output cap;
* collected Batch cost is released from reserve only when every request remains
  inside the currently-supported <=272k prompt-token pricing tier.

The frozen JSONL, wave manifest request hashes, model, reasoning mode, endpoint,
and per-request completion cap are all cross-checked before a reservation is
created.  Unsupported/tampered accounting never releases reserve.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import full_pipeline_batch_wave as batch
import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_luna_runtime as luna
import full_pipeline_paid_pilot_controller as base
import full_pipeline_paid_pilot_preflight as preflight


ACCOUNTING_REVISION = "full-pipeline-paid-pilot-batch-accounting-v2"

FullPipelinePaidPilotControllerError = base.FullPipelinePaidPilotControllerError
PaidPilotBudgetExceeded = base.PaidPilotBudgetExceeded
PaidPilotOperationUncertain = base.PaidPilotOperationUncertain
BudgetEnforcingOpenAIClient = base.BudgetEnforcingOpenAIClient

initialize_paid_pilot_execution = base.initialize_paid_pilot_execution
load_paid_pilot_execution = base.load_paid_pilot_execution
prepare_pilot_wave = base.prepare_pilot_wave
reconcile_pilot_wave_submission = base.reconcile_pilot_wave_submission
refresh_pilot_wave_status = base.refresh_pilot_wave_status
process_pilot_wave = base.process_pilot_wave


def _canonical_sha(value: Any) -> str:
    return base._sha256_text(base._canonical_json(value))


def _frozen_batch_request_contracts(
    target: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Read and prove the exact immutable Batch request bodies for one wave."""
    input_path = target / batch.INPUT_FILENAME
    if not input_path.is_file():
        raise FullPipelinePaidPilotControllerError(
            f"frozen Batch input is missing: {input_path}"
        )
    if batch._sha256_file(input_path) != manifest.get("input_sha256"):
        raise FullPipelinePaidPilotControllerError(
            "frozen Batch input hash differs from the wave manifest"
        )
    try:
        raw_text = input_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FullPipelinePaidPilotControllerError(
            f"cannot read frozen Batch input {input_path}: {exc}"
        ) from exc
    try:
        rows = batch._parse_jsonl(raw_text, "paid-pilot frozen Batch input")
    except Exception as exc:
        raise FullPipelinePaidPilotControllerError(
            f"cannot parse frozen paid-pilot Batch input: {exc}"
        ) from exc

    request_records = manifest.get("requests")
    if not isinstance(request_records, list) or not request_records:
        raise FullPipelinePaidPilotControllerError(
            "prepared Batch manifest contains no request identities"
        )
    if len(rows) != len(request_records) or len(rows) != int(
        manifest.get("request_count", 0) or 0
    ):
        raise FullPipelinePaidPilotControllerError(
            "frozen Batch JSONL request count differs from the wave manifest"
        )

    by_custom_id: dict[str, Mapping[str, Any]] = {}
    for record in request_records:
        if not isinstance(record, Mapping):
            raise FullPipelinePaidPilotControllerError(
                "prepared Batch request identity is malformed"
            )
        custom_id = record.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id or custom_id in by_custom_id:
            raise FullPipelinePaidPilotControllerError(
                "prepared Batch request identities require unique custom_id values"
            )
        by_custom_id[custom_id] = record

    contracts = []
    seen: set[str] = set()
    total_reserve = Decimal("0")
    for row in rows:
        custom_id = row.get("custom_id")
        if not isinstance(custom_id, str) or custom_id not in by_custom_id:
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch JSONL contains an unknown custom_id"
            )
        if custom_id in seen:
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch JSONL contains duplicate custom_id values"
            )
        seen.add(custom_id)
        record = by_custom_id[custom_id]
        if row.get("method") != "POST" or row.get("url") != batch.BATCH_ENDPOINT:
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch request does not use the exact chat-completions endpoint"
            )
        body = row.get("body")
        if not isinstance(body, Mapping):
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch request body is missing"
            )
        if body.get("model") != luna.LUNA_MODEL:
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch request does not use exact GPT-5.6 Luna"
            )
        if body.get("reasoning_effort") != "none":
            raise FullPipelinePaidPilotControllerError(
                "frozen Batch request reasoning_effort is not exact 'none'"
            )
        max_completion_tokens = body.get("max_completion_tokens")
        reserve = base._request_reserve(max_completion_tokens)
        body_sha = _canonical_sha(dict(body))
        request_sha = _canonical_sha(dict(row))
        if body_sha != record.get("body_sha256"):
            raise FullPipelinePaidPilotControllerError(
                f"frozen Batch body hash mismatch for {custom_id}"
            )
        if request_sha != record.get("request_sha256"):
            raise FullPipelinePaidPilotControllerError(
                f"frozen Batch request hash mismatch for {custom_id}"
            )
        contracts.append(
            {
                "custom_id": custom_id,
                "novel_id": record.get("novel_id"),
                "chapter": record.get("chapter"),
                "request_sha256": request_sha,
                "body_sha256": body_sha,
                "max_completion_tokens": max_completion_tokens,
                "reserve_usd": base._money(reserve),
            }
        )
        total_reserve += reserve

    if seen != set(by_custom_id):
        raise FullPipelinePaidPilotControllerError(
            "frozen Batch JSONL request identities are incomplete"
        )
    return {
        "accounting_revision": ACCOUNTING_REVISION,
        "contracts": contracts,
        "contract_sha256": _canonical_sha(contracts),
        "reserve_usd": base._money(total_reserve),
    }


def _ensure_batch_reservation(
    root: Path,
    repo: Path,
    token: str,
    wave_number: int,
) -> dict[str, Any]:
    with base._controller_lock(root):
        ledger, frozen, path = base._load_ledger_unlocked(root, repo, token)
        base._pilot_wave_allowed(frozen, wave_number)
        target = base._wave_target(root, wave_number)
        manifest = batch._read_manifest(target)
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

        request_contract = _frozen_batch_request_contracts(target, manifest)
        reserve = base._decimal(request_contract["reserve_usd"], "Batch reserve")
        key = str(wave_number)
        existing = ledger["batch_reservations"].get(key)
        expected = {
            "wave_number": wave_number,
            "run_id": manifest["run_id"],
            "input_sha256": manifest["input_sha256"],
            "request_count": request_count,
            "reserve_usd": base._money(reserve),
            "request_contract_sha256": request_contract["contract_sha256"],
            "accounting_revision": ACCOUNTING_REVISION,
        }
        if isinstance(existing, Mapping):
            for field, value in expected.items():
                if existing.get(field) != value:
                    raise FullPipelinePaidPilotControllerError(
                        f"Wave {wave_number} Batch reservation identity changed for {field}"
                    )
            return copy.deepcopy(dict(existing))

        base._ensure_budget_for_reserve(ledger, reserve, f"Wave {wave_number} Batch")
        ledger["batch_reservations"][key] = {
            **expected,
            "request_contracts": copy.deepcopy(request_contract["contracts"]),
            "status": "reserved",
            "actual_cost_usd": "0",
            "reconciled_submission": False,
        }
        base._refresh_budget_summary(ledger)
        ledger["updated_at_utc"] = base.utc_now().isoformat()
        base._persist_ledger(path, ledger)
        return copy.deepcopy(ledger["batch_reservations"][key])


def _validated_batch_actual(
    target: Path,
    manifest: Mapping[str, Any],
    document: Mapping[str, Any],
) -> tuple[Decimal | None, dict[str, Any]]:
    if document.get("model") != luna.LUNA_MODEL or document.get("transport") != "openai_batch":
        raise FullPipelinePaidPilotControllerError(
            "collected Batch evidence has wrong model/transport"
        )
    request_contract = _frozen_batch_request_contracts(target, manifest)
    contracts = request_contract["contracts"]
    by_custom_id = {item["custom_id"]: item for item in contracts}
    requests = document.get("requests")
    if not isinstance(requests, list) or len(requests) != len(contracts):
        raise FullPipelinePaidPilotControllerError(
            "collected Batch request count differs from frozen request contracts"
        )

    total = Decimal("0")
    long_context = []
    seen: set[str] = set()
    for item in requests:
        if not isinstance(item, Mapping):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch request evidence is malformed"
            )
        custom_id = item.get("custom_id")
        contract = by_custom_id.get(custom_id)
        if contract is None or custom_id in seen:
            raise FullPipelinePaidPilotControllerError(
                "collected Batch custom_id set differs from frozen request contracts"
            )
        seen.add(custom_id)
        for field in ("request_sha256", "body_sha256", "novel_id", "chapter"):
            if item.get(field) != contract.get(field):
                raise FullPipelinePaidPilotControllerError(
                    f"collected Batch identity mismatch for {custom_id}: {field}"
                )
        stats = luna.validate_saved_luna_stats(item.get("stats", {}))
        if stats.get("pricing_version") != batch.BATCH_PRICING_VERSION:
            raise FullPipelinePaidPilotControllerError(
                "collected request is not exact Batch-priced Luna evidence"
            )
        prompt = int(stats.get("prompt_tokens", 0) or 0)
        completion = int(stats.get("completion_tokens", 0) or 0)
        cached = int(stats.get("cache_hit_tokens", 0) or 0)
        misses = int(stats.get("cache_miss_tokens", 0) or 0)
        if prompt < 0 or completion < 0 or cached < 0 or misses < 0 or cached + misses != prompt:
            raise FullPipelinePaidPilotControllerError(
                "collected Batch token accounting is internally inconsistent"
            )
        if completion > int(contract["max_completion_tokens"]):
            raise FullPipelinePaidPilotControllerError(
                f"collected Batch completion exceeded frozen request cap for {custom_id}"
            )
        if prompt > base.LUNA_LONG_CONTEXT_THRESHOLD_TOKENS:
            long_context.append(
                {
                    "custom_id": custom_id,
                    "prompt_tokens": prompt,
                    "threshold_tokens": base.LUNA_LONG_CONTEXT_THRESHOLD_TOKENS,
                }
            )
            continue
        recomputed = (
            Decimal(cached) / Decimal(1_000_000) * Decimal(str(batch.BATCH_CACHE_HIT_RATE))
            + Decimal(misses) / Decimal(1_000_000) * Decimal(str(batch.BATCH_CACHE_MISS_RATE))
            + Decimal(completion) / Decimal(1_000_000) * Decimal(str(batch.BATCH_OUTPUT_RATE))
        )
        saved = base._decimal(stats.get("cost_usd", "0"), "Batch request actual cost")
        if abs(saved - recomputed) > Decimal("0.000000000001"):
            raise FullPipelinePaidPilotControllerError(
                f"collected Batch cost does not match token/rate evidence for {custom_id}"
            )
        total += recomputed

    if seen != set(by_custom_id):
        raise FullPipelinePaidPilotControllerError(
            "collected Batch request identities are incomplete"
        )
    if long_context:
        return None, {
            "reason": "long_context_batch_pricing_not_supported_by_runtime_contract",
            "requests": long_context,
            "request_contract_sha256": request_contract["contract_sha256"],
        }

    totals = document.get("totals")
    if isinstance(totals, Mapping):
        observed = base._decimal(totals.get("cost_usd", "0"), "Batch totals cost")
        if abs(observed - total) > Decimal("0.000000000001"):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch total cost differs from per-request exact accounting"
            )
    return total, {
        "request_contract_sha256": request_contract["contract_sha256"],
        "request_count": len(contracts),
        "pricing_version": batch.BATCH_PRICING_VERSION,
        "actual_cost_usd": base._money(total),
    }


def _finalize_batch_reservation(
    root: Path,
    repo: Path,
    token: str,
    wave_number: int,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    target = base._wave_target(root, wave_number)
    manifest = batch._read_manifest(target)
    actual, evidence = _validated_batch_actual(target, manifest, document)
    with base._controller_lock(root):
        ledger, _frozen, path = base._load_ledger_unlocked(root, repo, token)
        reservation = ledger["batch_reservations"].get(str(wave_number))
        if not isinstance(reservation, dict):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch has no controller reservation"
            )
        if reservation.get("run_id") != document.get("run_id"):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch run ID differs from controller reservation"
            )
        if reservation.get("request_contract_sha256") != evidence.get(
            "request_contract_sha256"
        ):
            raise FullPipelinePaidPilotControllerError(
                "collected Batch request contract differs from controller reservation"
            )
        if actual is None:
            reservation["status"] = "cost_unverified"
            reservation["cost_evidence"] = copy.deepcopy(evidence)
            base._refresh_budget_summary(ledger)
            ledger["updated_at_utc"] = base.utc_now().isoformat()
            base._persist_ledger(path, ledger)
            raise FullPipelinePaidPilotControllerError(
                "Batch response crossed the supported long-context pricing threshold; "
                "worst-case reserve remains locked and Batch accounting is blocked"
            )
        if reservation.get("status") == "finalized":
            if base._decimal(
                reservation.get("actual_cost_usd", "0"), "saved Batch cost"
            ) != actual:
                raise FullPipelinePaidPilotControllerError(
                    "already-finalized Batch cost differs from current exact evidence"
                )
            return ledger
        reserve = base._decimal(reservation["reserve_usd"], "Batch reserve")
        if actual > reserve:
            reservation["status"] = "cost_unverified"
            reservation["cost_evidence"] = {
                **copy.deepcopy(evidence),
                "reason": "actual_cost_exceeded_worst_case_reserve",
            }
            base._refresh_budget_summary(ledger)
            base._persist_ledger(path, ledger)
            raise FullPipelinePaidPilotControllerError(
                "actual Batch cost exceeded its worst-case controller reserve"
            )
        reservation["actual_cost_usd"] = base._money(actual)
        reservation["status"] = "finalized"
        reservation["batch_id"] = document.get("batch_id")
        reservation["cost_evidence"] = copy.deepcopy(evidence)
        reservation["finalized_at_utc"] = base.utc_now().isoformat()
        base._refresh_budget_summary(ledger)
        ledger["updated_at_utc"] = base.utc_now().isoformat()
        return base._persist_ledger(path, ledger)


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
        base._mark_batch_transport_blocked(
            root, repo, authorization_token, wave_number
        )
        raise


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
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    ledger = load_paid_pilot_execution(
        root, repo, authorization_token=authorization_token
    )
    for key, reservation in list(ledger.get("batch_reservations", {}).items()):
        if not isinstance(reservation, Mapping) or reservation.get("status") == "finalized":
            continue
        wave_number = int(key)
        target = base._wave_target(root, wave_number)
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
    return load_paid_pilot_execution(
        root, repo, authorization_token=authorization_token
    )
