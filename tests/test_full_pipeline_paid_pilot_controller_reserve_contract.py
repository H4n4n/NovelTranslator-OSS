import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_luna_runtime as luna
import full_pipeline_paid_pilot_preflight as preflight
from full_pipeline_paid_pilot_controller_v2 import (
    ACCOUNTING_REVISION,
    _frozen_batch_request_contracts,
    initialize_paid_pilot_execution,
    load_paid_pilot_execution,
    prepare_pilot_wave,
    submit_pilot_wave,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")
ANCHOR = "synthetic-phase60j-reserve-anchor"


def _helpers():
    name = "_phase60j_reserve_contract_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_primary_batch_reserve_is_derived_from_exact_frozen_jsonl_contract(
    tmp_path, monkeypatch
):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path)
    experiment = tmp_path / "reserve_contract_experiment"
    v3.create_experiment(
        experiment,
        repo,
        repo_anchor=ANCHOR,
        target_chapters=25,
        experiment_id="phase60j-reserve-contract",
    )
    frozen = preflight.freeze_paid_pilot_preflight(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        pilot_wave_count=1,
        max_sync_model_calls=2,
        max_batch_status_reads=2,
        max_batch_reconciliation_reads=1,
        max_total_paid_cost_usd="2.00",
    )
    token = frozen["authorization_token"]
    initialize_paid_pilot_execution(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        authorization_token=token,
    )
    manifest, target = prepare_pilot_wave(
        experiment,
        repo,
        1,
        authorization_token=token,
    )

    lines = [
        json.loads(line)
        for line in (target / "batch_input.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(lines) == manifest["request_count"] == 2
    frozen_output_cap = luna.LUNA_MAX_TOKENS
    assert {
        line["body"]["max_completion_tokens"] for line in lines
    } == {frozen_output_cap}
    assert all(line["body"]["model"] == luna.LUNA_MODEL for line in lines)
    assert all(line["body"]["reasoning_effort"] == "none" for line in lines)

    contract = _frozen_batch_request_contracts(target, manifest)
    assert contract["accounting_revision"] == ACCOUNTING_REVISION
    assert len(contract["contracts"]) == 2
    assert {
        item["max_completion_tokens"] for item in contract["contracts"]
    } == {frozen_output_cap}
    assert all(len(item["body_sha256"]) == 64 for item in contract["contracts"])
    assert all(len(item["request_sha256"]) == 64 for item in contract["contracts"])

    # The paid reservation must be a property of the already-frozen JSONL, not
    # whatever a later runtime import happens to expose as the shared cap.
    monkeypatch.setattr(luna, "LUNA_MAX_TOKENS", 1)

    raw = h.BatchClient("phase60j-reserve-contract")
    submit_pilot_wave(
        experiment,
        repo,
        1,
        raw,
        authorization_token=token,
    )
    ledger = load_paid_pilot_execution(
        experiment,
        repo,
        authorization_token=token,
    )
    reservation = ledger["batch_reservations"]["1"]
    assert reservation["accounting_revision"] == ACCOUNTING_REVISION
    assert reservation["request_contract_sha256"] == contract["contract_sha256"]
    assert reservation["request_contracts"] == contract["contracts"]
    assert {
        item["max_completion_tokens"] for item in reservation["request_contracts"]
    } == {frozen_output_cap}
    assert Decimal(reservation["reserve_usd"]) == Decimal(contract["reserve_usd"])
