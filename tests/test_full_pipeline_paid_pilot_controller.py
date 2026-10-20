import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_paid_pilot_preflight as preflight
from full_pipeline_batch_wave import BATCH_MODEL
from full_pipeline_paid_pilot_controller import (
    BudgetEnforcingOpenAIClient,
    FullPipelinePaidPilotControllerError,
    PaidPilotBudgetExceeded,
    collect_pilot_wave,
    initialize_paid_pilot_execution,
    load_paid_pilot_execution,
    prepare_pilot_wave,
    reconcile_local_cost_evidence,
    submit_pilot_wave,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")
ANCHOR = "synthetic-phase60j-anchor"


def _helpers():
    name = "_phase60j_controller_harness_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _make(
    tmp_path,
    *,
    max_cost="1.50",
    pilot_wave_count=1,
    max_sync_model_calls=4,
    max_batch_status_reads=4,
    max_batch_reconciliation_reads=2,
):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path)
    experiment = tmp_path / "paid_pilot_experiment"
    v3.create_experiment(
        experiment,
        repo,
        repo_anchor=ANCHOR,
        target_chapters=25,
        experiment_id="phase60j-controller-test",
    )
    frozen = preflight.freeze_paid_pilot_preflight(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        pilot_wave_count=pilot_wave_count,
        max_sync_model_calls=max_sync_model_calls,
        max_batch_status_reads=max_batch_status_reads,
        max_batch_reconciliation_reads=max_batch_reconciliation_reads,
        max_total_paid_cost_usd=max_cost,
    )
    token = frozen["authorization_token"]
    ledger = initialize_paid_pilot_execution(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        authorization_token=token,
    )
    return h, repo, experiment, frozen, token, ledger


def _complete_batch(h, submitted, client, wave_number=1):
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = f"file-output-wave-{wave_number}"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )
    rows = [
        h._batch_row(item["custom_id"], h._translation(item["novel_id"], wave_number))
        for item in reversed(submitted["requests"])
    ]
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in rows
    ) + "\n"


class SyncCompletions:
    def __init__(self, *, prompt_tokens=1000, completion_tokens=200, model=BATCH_MODEL, error=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id=f"sync-{len(self.calls)}",
            model=self.model,
            system_fingerprint="phase60j-test",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"ok":true}'),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                prompt_tokens_details=SimpleNamespace(cached_tokens=100),
            ),
        )


class SyncClient:
    def __init__(self, **kwargs):
        self.completions = SyncCompletions(**kwargs)
        self.chat = SimpleNamespace(completions=self.completions)


def _sync_request(max_tokens=5000):
    return {
        "model": BATCH_MODEL,
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Test."},
        ],
        "max_completion_tokens": max_tokens,
        "reasoning_effort": "none",
    }


def test_initialize_binds_preflight_and_is_idempotent(tmp_path):
    _h, repo, experiment, frozen, token, ledger = _make(tmp_path)

    assert ledger["preflight_sha256"] == frozen["preflight_sha256"]
    assert ledger["repo_anchor"] == ANCHOR
    assert ledger["provider_api_calls_started"] == 0
    assert ledger["paid_model_requests_started"] == 0
    assert ledger["budget_summary"] == {
        "actual_cost_usd": "0",
        "locked_reserve_usd": "0",
        "authorized_exposure_usd": "0",
    }

    again = initialize_paid_pilot_execution(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        authorization_token=token,
    )
    assert again == ledger

    with pytest.raises(FullPipelinePaidPilotControllerError, match="authorization token"):
        load_paid_pilot_execution(
            experiment,
            repo,
            authorization_token="wrong-token",
        )


def test_batch_reserve_physically_blocks_sync_until_collection_releases_actual_cost(tmp_path):
    h, repo, experiment, _frozen, token, _ledger = _make(
        tmp_path,
        max_cost="1.00",
        max_sync_model_calls=3,
    )
    prepare_pilot_wave(experiment, repo, 1, authorization_token=token)
    batch_client = h.BatchClient("phase60j-wave1")
    submitted = submit_pilot_wave(
        experiment,
        repo,
        1,
        batch_client,
        authorization_token=token,
    )

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger["counts"]["batch_input_upload"] == 1
    assert ledger["counts"]["batch_create"] == 1
    assert ledger["batch_model_requests_started"] == 2
    assert ledger["paid_model_requests_started"] == 2
    assert ledger["batch_reservations"]["1"]["status"] == "active"
    assert float(ledger["budget_summary"]["locked_reserve_usd"]) > 0.88

    sync_raw = SyncClient()
    metered = BudgetEnforcingOpenAIClient(
        sync_raw,
        experiment_root=experiment,
        repository_root=repo,
        authorization_token=token,
        wave_number=1,
        novel_id="great_king_zhuxia",
    )
    with pytest.raises(PaidPilotBudgetExceeded, match="paid-cost ceiling"):
        metered.chat.completions.create(**_sync_request())
    assert sync_raw.completions.calls == []

    _complete_batch(h, submitted, batch_client)
    document, _manifest = collect_pilot_wave(
        experiment,
        repo,
        1,
        batch_client,
        authorization_token=token,
    )
    assert document["status"] == "complete"

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger["batch_reservations"]["1"]["status"] == "finalized"
    assert float(ledger["batch_reservations"]["1"]["actual_cost_usd"]) > 0
    assert ledger["budget_summary"]["locked_reserve_usd"] == "0"

    response = metered.chat.completions.create(**_sync_request())
    assert response.model == BATCH_MODEL
    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger["counts"]["sync_model_call"] == 1
    assert ledger["paid_model_requests_started"] == 3
    assert ledger["operations"][-1]["status"] == "finalized"
    assert ledger["budget_summary"]["locked_reserve_usd"] == "0"


def test_sync_exception_consumes_slot_and_keeps_reserve_locked(tmp_path):
    _h, repo, experiment, _frozen, token, _ledger = _make(
        tmp_path,
        max_cost="0.60",
        max_sync_model_calls=3,
    )
    raw = SyncClient(error=RuntimeError("network ambiguity"))
    client = BudgetEnforcingOpenAIClient(
        raw,
        experiment_root=experiment,
        repository_root=repo,
        authorization_token=token,
        wave_number=1,
        novel_id="great_king_zhuxia",
    )

    with pytest.raises(RuntimeError, match="network ambiguity"):
        client.chat.completions.create(**_sync_request())

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger["counts"]["sync_model_call"] == 1
    assert ledger["operations"][-1]["status"] == "uncertain"
    assert float(ledger["budget_summary"]["locked_reserve_usd"]) > 0.42

    raw.completions.error = None
    with pytest.raises(PaidPilotBudgetExceeded, match="paid-cost ceiling"):
        client.chat.completions.create(**_sync_request())
    assert len(raw.completions.calls) == 1
    ledger2 = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger2["counts"]["sync_model_call"] == 1


def test_long_context_response_stays_reserved_and_blocks_runtime_accounting(tmp_path):
    _h, repo, experiment, _frozen, token, _ledger = _make(tmp_path)
    raw = SyncClient(prompt_tokens=300001)
    client = BudgetEnforcingOpenAIClient(
        raw,
        experiment_root=experiment,
        repository_root=repo,
        authorization_token=token,
        wave_number=1,
        novel_id="great_king_zhuxia",
    )

    with pytest.raises(FullPipelinePaidPilotControllerError, match="long-context"):
        client.chat.completions.create(**_sync_request())

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    operation = ledger["operations"][-1]
    assert operation["status"] == "pricing_unsupported"
    assert operation["evidence"]["prompt_tokens"] == 300001
    assert float(ledger["budget_summary"]["locked_reserve_usd"]) > 0.42


def test_batch_create_ambiguity_consumes_model_count_and_cannot_auto_reissue(tmp_path):
    h, repo, experiment, _frozen, token, _ledger = _make(tmp_path)
    prepare_pilot_wave(experiment, repo, 1, authorization_token=token)
    raw = h.BatchClient("phase60j-ambiguous")

    def ambiguous_create(**kwargs):
        raw.batches.create_calls.append(kwargs)
        raise RuntimeError("lost Batch create response")

    raw.batches.create = ambiguous_create
    with pytest.raises(Exception):
        submit_pilot_wave(
            experiment,
            repo,
            1,
            raw,
            authorization_token=token,
        )

    ledger = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger["counts"]["batch_input_upload"] == 1
    assert ledger["counts"]["batch_create"] == 1
    assert ledger["batch_model_requests_started"] == 2
    assert ledger["batch_reservations"]["1"]["status"] == "uncertain"
    assert float(ledger["budget_summary"]["locked_reserve_usd"]) > 0.88

    before_calls = len(raw.batches.create_calls)
    with pytest.raises(Exception):
        submit_pilot_wave(
            experiment,
            repo,
            1,
            raw,
            authorization_token=token,
        )
    assert len(raw.batches.create_calls) == before_calls
    ledger2 = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert ledger2["counts"]["batch_create"] == 1


def test_local_collection_checkpoint_can_finalize_cost_without_provider_replay(tmp_path):
    h, repo, experiment, _frozen, token, _ledger = _make(tmp_path)
    prepare_pilot_wave(experiment, repo, 1, authorization_token=token)
    batch_client = h.BatchClient("phase60j-reconcile")
    submitted = submit_pilot_wave(
        experiment,
        repo,
        1,
        batch_client,
        authorization_token=token,
    )
    _complete_batch(h, submitted, batch_client)

    def crash(stage, _context):
        if stage == "after_results_recorded":
            raise RuntimeError("synthetic hard stop after durable results")

    with pytest.raises(RuntimeError, match="synthetic hard stop"):
        collect_pilot_wave(
            experiment,
            repo,
            1,
            batch_client,
            authorization_token=token,
            crash_hook=crash,
        )

    before = load_paid_pilot_execution(
        experiment, repo, authorization_token=token
    )
    assert before["batch_reservations"]["1"]["status"] == "active"
    content_calls = list(batch_client.files.content_calls)
    retrieve_calls = list(batch_client.batches.retrieve_calls)

    recovered = reconcile_local_cost_evidence(
        experiment,
        repo,
        authorization_token=token,
    )
    assert recovered["batch_reservations"]["1"]["status"] == "finalized"
    assert recovered["budget_summary"]["locked_reserve_usd"] == "0"
    assert batch_client.files.content_calls == content_calls
    assert batch_client.batches.retrieve_calls == retrieve_calls


def test_pilot_scope_and_status_caps_block_before_remote_calls(tmp_path):
    h, repo, experiment, _frozen, token, _ledger = _make(
        tmp_path,
        pilot_wave_count=1,
        max_batch_status_reads=1,
    )
    with pytest.raises(FullPipelinePaidPilotControllerError, match="outside frozen"):
        prepare_pilot_wave(experiment, repo, 2, authorization_token=token)
    assert not (experiment / "waves" / "wave_0002").exists()

    raw = h.BatchClient("phase60j-caps")
    client = BudgetEnforcingOpenAIClient(
        raw,
        experiment_root=experiment,
        repository_root=repo,
        authorization_token=token,
        wave_number=1,
    )
    client.batches.retrieve("batch-a")
    with pytest.raises(PaidPilotBudgetExceeded, match="batch_status_read cap exhausted"):
        client.batches.retrieve("batch-b")
    assert raw.batches.retrieve_calls == ["batch-a"]
