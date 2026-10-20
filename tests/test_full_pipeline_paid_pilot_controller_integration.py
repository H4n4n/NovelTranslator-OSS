import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_paid_pilot_preflight as preflight
from full_pipeline_paid_pilot_controller_v2 import (
    ACCOUNTING_REVISION,
    collect_pilot_wave,
    initialize_paid_pilot_execution,
    load_paid_pilot_execution,
    prepare_pilot_wave,
    process_pilot_wave,
    submit_pilot_wave,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")
ANCHOR = "synthetic-phase60j-integration-anchor"


def _helpers():
    name = "_phase60j_controller_integration_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _complete_batch(h, submitted, client, wave_number):
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = f"file-output-controller-{wave_number}"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )
    rows = [
        h._batch_row(
            item["custom_id"],
            h._translation(item["novel_id"], wave_number),
        )
        for item in reversed(submitted["requests"])
    ]
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in rows
    ) + "\n"


def test_controller_preserves_real_wave1_to_wave2_state_growth(tmp_path):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path)
    experiment = tmp_path / "controller_chronological_experiment"
    manifest = v3.create_experiment(
        experiment,
        repo,
        repo_anchor=ANCHOR,
        target_chapters=25,
        experiment_id="phase60j-controller-integration",
    )
    frozen = preflight.freeze_paid_pilot_preflight(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        pilot_wave_count=2,
        max_sync_model_calls=20,
        max_batch_status_reads=6,
        max_batch_reconciliation_reads=2,
        max_total_paid_cost_usd="5.00",
    )
    token = frozen["authorization_token"]
    initialize_paid_pilot_execution(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        authorization_token=token,
    )

    wave1_manifest, wave1_dir = prepare_pilot_wave(
        experiment,
        repo,
        1,
        authorization_token=token,
    )
    batch_client = h.BatchClient("phase60j-integration-wave1")
    submitted = submit_pilot_wave(
        experiment,
        repo,
        1,
        batch_client,
        authorization_token=token,
    )
    _complete_batch(h, submitted, batch_client, 1)
    collected, _collection_manifest = collect_pilot_wave(
        experiment,
        repo,
        1,
        batch_client,
        authorization_token=token,
    )
    assert collected["status"] == "complete"
    assert wave1_manifest["wave_number"] == 1
    assert wave1_dir.is_dir()

    created, raw_factory = h._client_factory_registry()
    processing = process_pilot_wave(
        experiment,
        repo,
        1,
        authorization_token=token,
        client_factory=raw_factory,
    )
    assert processing["status"] == "complete"
    assert len(created) == 2

    chinese = experiment / "projects" / "great_king_zhuxia"
    korean = experiment / "projects" / "rogue_first_prince"
    assert json.loads(
        (chinese / "data" / "glossary.json").read_text(encoding="utf-8")
    )["entries"]["姚雲"]["english"] == "Yao Yun"
    assert json.loads(
        (korean / "data" / "glossary.json").read_text(encoding="utf-8")
    )["entries"]["아렌"]["english"] == "Aren"

    ledger = load_paid_pilot_execution(
        experiment,
        repo,
        authorization_token=token,
    )
    reservation = ledger["batch_reservations"]["1"]
    assert reservation["status"] == "finalized"
    assert reservation["accounting_revision"] == ACCOUNTING_REVISION
    assert reservation["cost_evidence"]["request_contract_sha256"] == reservation[
        "request_contract_sha256"
    ]
    assert ledger["counts"]["sync_model_call"] >= 2
    assert ledger["budget_summary"]["locked_reserve_usd"] == "0"
    assert float(ledger["budget_summary"]["actual_cost_usd"]) > float(
        reservation["actual_cost_usd"]
    )

    wave2_manifest, wave2_dir = prepare_pilot_wave(
        experiment,
        repo,
        2,
        authorization_token=token,
    )
    lines = [
        json.loads(line)
        for line in (wave2_dir / "batch_input.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    prompts = [line["body"]["messages"][1]["content"] for line in lines]
    assert len(lines) == 2
    assert any("姚雲" in prompt and "Yao Yun" in prompt for prompt in prompts)
    assert any("아렌" in prompt and "Aren" in prompt for prompt in prompts)
    assert any(
        "Accepted continuity after Chapter 1 for great_king_zhuxia." in prompt
        for prompt in prompts
    )
    assert any(
        "Accepted continuity after Chapter 1 for rogue_first_prince." in prompt
        for prompt in prompts
    )
    assert wave2_manifest["wave_number"] == 2
    assert manifest["target_chapters"] == 25
