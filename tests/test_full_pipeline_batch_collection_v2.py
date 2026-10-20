import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import full_pipeline_batch_wave as base
from full_pipeline_batch_collection_v2 import (
    COLLECTION_PROTOCOL_VERSION,
    STATUS_RAW_RECORDED,
    STATUS_STARTED,
    collect_wave,
)


class SyntheticCrash(RuntimeError):
    pass


class FakeFiles:
    def __init__(self):
        self.contents = {}
        self.create_calls = []
        self.content_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            id="file-input-v2",
            purpose="batch",
            filename="batch_input.jsonl",
            bytes=100,
        )

    def content(self, file_id):
        self.content_calls.append(file_id)
        return SimpleNamespace(text=self.contents[file_id])


class FakeBatches:
    def __init__(self):
        self.create_calls = []
        self.retrieve_calls = []
        self.list_calls = []
        self.batch = SimpleNamespace(
            id="batch-v2",
            status="validating",
            endpoint=base.BATCH_ENDPOINT,
            input_file_id="file-input-v2",
            output_file_id=None,
            error_file_id=None,
            completion_window=base.BATCH_COMPLETION_WINDOW,
            created_at=1,
            completed_at=None,
            expires_at=None,
            request_counts=SimpleNamespace(total=0, completed=0, failed=0),
            metadata={},
        )

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.batch.input_file_id = kwargs["input_file_id"]
        self.batch.metadata = kwargs["metadata"]
        return self.batch

    def retrieve(self, batch_id):
        self.retrieve_calls.append(batch_id)
        return self.batch

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return SimpleNamespace(data=[self.batch])


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


def _row(custom_id, *, content="Chapter 1\n\nA complete translation."):
    return {
        "id": f"req-{custom_id}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": f"request-{custom_id}",
            "body": {
                "id": f"response-{custom_id}",
                "model": base.BATCH_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 300,
                    "total_tokens": 1300,
                    "prompt_tokens_details": {"cached_tokens": 100},
                },
            },
        },
        "error": None,
    }


def _prepare_completed_wave(tmp_path):
    guard = {"production": "unchanged"}
    capture = lambda: guard
    request = {
        "novel_id": "great_king_zhuxia",
        "chapter": 1,
        "body": {
            "model": base.BATCH_MODEL,
            "messages": [
                {"role": "system", "content": "Translate."},
                {"role": "user", "content": "第1章\n\n正文"},
            ],
            "max_completion_tokens": 4096,
            "reasoning_effort": "none",
        },
        "lineage_before": {
            "novel_id": "great_king_zhuxia",
            "last_completed": 0,
        },
    }
    _manifest, target = base.prepare_wave(
        tmp_path / "waves",
        1,
        [request],
        capture_protected_state=capture,
        experiment_id="phase60g-collection-test",
    )
    client = FakeClient()
    submitted = base.submit_wave(
        target,
        client,
        base.SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    custom_id = submitted["requests"][0]["custom_id"]
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-v2"
    client.batches.batch.request_counts = SimpleNamespace(
        total=1,
        completed=1,
        failed=0,
    )
    client.files.contents["file-output-v2"] = (
        json.dumps(_row(custom_id), ensure_ascii=False) + "\n"
    )
    return target, client, capture


def _manifest(target):
    return json.loads(
        (Path(target) / base.MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def test_restart_after_raw_checkpoint_never_redownloads_provider_output(tmp_path):
    target, client, capture = _prepare_completed_wave(tmp_path)

    def crash(stage, _context):
        if stage == "after_raw_provider_files":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match="after_raw_provider_files"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
            crash_hook=crash,
        )

    state = _manifest(target)
    assert state["status"] == STATUS_RAW_RECORDED
    assert state["collection_protocol"] == COLLECTION_PROTOCOL_VERSION
    assert client.batches.retrieve_calls == ["batch-v2"]
    assert client.files.content_calls == ["file-output-v2"]

    document, finished = collect_wave(
        target,
        client,
        capture_protected_state=capture,
    )
    assert document["status"] == "complete"
    assert finished["status"] == "collected"
    assert client.batches.retrieve_calls == ["batch-v2"]
    assert client.files.content_calls == ["file-output-v2"]


def test_uncheckpointed_raw_file_is_redownloaded_only_for_exact_compare(tmp_path):
    target, client, capture = _prepare_completed_wave(tmp_path)

    def crash(stage, _context):
        if stage == "after_output_file_write":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match="after_output_file_write"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
            crash_hook=crash,
        )

    state = _manifest(target)
    assert state["status"] == STATUS_STARTED
    output_path = Path(target) / base.OUTPUT_FILENAME
    before_bytes = output_path.read_bytes()
    before_mtime = output_path.stat().st_mtime_ns
    assert client.files.content_calls == ["file-output-v2"]

    document, finished = collect_wave(
        target,
        client,
        capture_protected_state=capture,
    )
    assert document["status"] == "complete"
    assert finished["status"] == "collected"
    assert output_path.read_bytes() == before_bytes
    assert output_path.stat().st_mtime_ns == before_mtime
    assert client.batches.retrieve_calls == ["batch-v2"]
    assert client.files.content_calls == ["file-output-v2", "file-output-v2"]


def test_restart_after_results_file_before_manifest_adopts_local_results(tmp_path):
    target, client, capture = _prepare_completed_wave(tmp_path)

    def crash(stage, _context):
        if stage == "after_results_file_write":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match="after_results_file_write"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
            crash_hook=crash,
        )

    state = _manifest(target)
    assert state["status"] == STATUS_RAW_RECORDED
    results_path = Path(target) / base.RESULTS_FILENAME
    before = results_path.read_bytes()
    assert client.files.content_calls == ["file-output-v2"]

    document, finished = collect_wave(
        target,
        client,
        capture_protected_state=capture,
    )
    assert finished["status"] == "collected"
    assert results_path.read_bytes() == before
    assert document["collection_protocol"] == COLLECTION_PROTOCOL_VERSION
    assert client.batches.retrieve_calls == ["batch-v2"]
    assert client.files.content_calls == ["file-output-v2"]


def test_raw_checkpoint_tampering_fails_closed_without_provider_refetch(tmp_path):
    target, client, capture = _prepare_completed_wave(tmp_path)

    def crash(stage, _context):
        if stage == "after_raw_provider_files":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
            crash_hook=crash,
        )

    output_path = Path(target) / base.OUTPUT_FILENAME
    output_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(base.FullPipelineBatchSafetyError, match="hash"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
        )
    assert client.batches.retrieve_calls == ["batch-v2"]
    assert client.files.content_calls == ["file-output-v2"]


def test_provider_error_rows_are_durable_and_not_redownloaded_on_retry(tmp_path):
    target, client, capture = _prepare_completed_wave(tmp_path)
    client.batches.batch.error_file_id = "file-error-v2"
    client.files.contents["file-error-v2"] = json.dumps(
        {"custom_id": "failure", "error": {"message": "provider failure"}}
    ) + "\n"

    with pytest.raises(base.FullPipelineBatchError, match="provider error rows"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
        )
    calls_after_first = list(client.files.content_calls)
    assert calls_after_first == ["file-output-v2", "file-error-v2"]

    with pytest.raises(base.FullPipelineBatchError, match="already durable"):
        collect_wave(
            target,
            client,
            capture_protected_state=capture,
        )
    assert client.files.content_calls == calls_after_first
    assert client.batches.retrieve_calls == ["batch-v2"]
