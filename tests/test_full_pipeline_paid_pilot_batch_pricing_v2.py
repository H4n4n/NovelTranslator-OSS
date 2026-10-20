import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_paid_pilot_preflight as preflight
from full_pipeline_paid_pilot_controller_v2 import (
    FullPipelinePaidPilotControllerError,
    collect_pilot_wave,
    initialize_paid_pilot_execution,
    load_paid_pilot_execution,
    prepare_pilot_wave,
    reconcile_local_cost_evidence,
    submit_pilot_wave,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")
ANCHOR = "synthetic-phase60j-batch-pricing-anchor"


def _helpers():
    name = "_phase60j_batch_pricing_v2_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_long_context_batch_collection_keeps_reserve_locked_and_reconciles_locally(tmp_path):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path)
    experiment = tmp_path / "batch_pricing_v2_experiment"
    v3.create_experiment(
        experiment,
        repo,
        repo_anchor=ANCHOR,
        target_chapters=25,
        experiment_id="phase60j-batch-pricing-v2",
    )
    frozen = preflight.freeze_paid_pilot_preflight(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        pilot_wave_count=1,
        max_sync_model_calls=2,
        max_batch_status_reads=3,
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
    prepare_pilot_wave(experiment, repo, 1, authorization_token=token)

    client = h.BatchClient("phase60j-long-context")
    submitted = submit_pilot_wave(
        experiment,
        repo,
        1,
        client,
        authorization_token=token,
    )
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-phase60j-long-context"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )

    rows = []
    for index, item in enumerate(reversed(submitted["requests"])):
        row = h._batch_row(
            item["custom_id"], h._translation(item["novel_id"], 1)
        )
        if index == 0:
            usage = row["response"]["body"]["usage"]
            usage["prompt_tokens"] = 300001
            usage["total_tokens"] = 300351
            usage["prompt_tokens_details"]["cached_tokens"] = 100
        rows.append(row)
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in rows
    ) + "\n"

    with pytest.raises(FullPipelinePaidPilotControllerError, match="long-context"):
        collect_pilot_wave(
            experiment,
            repo,
            1,
            client,
            authorization_token=token,
        )

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    reservation = ledger["batch_reservations"]["1"]
    assert reservation["status"] == "cost_unverified"
    assert reservation["actual_cost_usd"] == "0"
    assert reservation["cost_evidence"]["reason"] == (
        "long_context_batch_pricing_not_supported_by_runtime_contract"
    )
    assert reservation["cost_evidence"]["requests"][0]["prompt_tokens"] == 300001
    assert float(ledger["budget_summary"]["locked_reserve_usd"]) > 0

    content_calls = list(client.files.content_calls)
    retrieve_calls = list(client.batches.retrieve_calls)
    with pytest.raises(FullPipelinePaidPilotControllerError, match="long-context"):
        reconcile_local_cost_evidence(
            experiment,
            repo,
            authorization_token=token,
        )
    assert client.files.content_calls == content_calls
    assert client.batches.retrieve_calls == retrieve_calls
