import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from full_pipeline_batch_wave import (
    BATCH_MODEL,
    INPUT_FILENAME,
    MANIFEST_FILENAME,
    OUTPUT_FILENAME,
    RESULTS_FILENAME,
    SUBMIT_AUTHORIZATION,
    FullPipelineBatchError,
    FullPipelineBatchSafetyError,
    FullPipelineBatchSubmissionUncertain,
    collect_wave,
    prepare_wave,
    reconcile_submission,
    refresh_status,
    submit_wave,
)
from production_translation_request import build_production_translation_request


FIXED_TIME = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)


class FakeFiles:
    def __init__(self):
        self.create_calls = []
        self.content_calls = []
        self.contents = {}
        self.raise_on_create = None

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return SimpleNamespace(
            id="file-wave-input-123",
            purpose="batch",
            filename=INPUT_FILENAME,
            bytes=1234,
        )

    def content(self, file_id):
        self.content_calls.append(file_id)
        return SimpleNamespace(text=self.contents[file_id])


class FakeBatches:
    def __init__(self):
        self.create_calls = []
        self.retrieve_calls = []
        self.list_calls = []
        self.raise_on_create = None
        self.batch = SimpleNamespace(
            id="batch-wave-123",
            status="validating",
            endpoint="/v1/chat/completions",
            input_file_id="file-wave-input-123",
            output_file_id=None,
            error_file_id=None,
            completion_window="24h",
            created_at=FIXED_TIME.timestamp(),
            completed_at=None,
            expires_at=None,
            request_counts=SimpleNamespace(total=2, completed=0, failed=0),
            metadata={},
        )
        self.list_data = [self.batch]

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.batch.input_file_id = kwargs["input_file_id"]
        self.batch.metadata = kwargs["metadata"]
        return self.batch

    def retrieve(self, batch_id):
        self.retrieve_calls.append(batch_id)
        return self.batch

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return SimpleNamespace(data=list(self.list_data))


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


def _luna_body(chapter, source_text, continuity=""):
    return build_production_translation_request(
        {"chapter": chapter, "text": source_text},
        provider="openai",
        model=BATCH_MODEL,
        max_tokens=12000,
        reasoning="none",
        system_prompt="SYSTEM",
        chapter_numbering="global",
        source_language="zh",
        target_language="en",
        glossary_entries={},
        profile=SimpleNamespace(high_priority_categories=()),
        continuity_summary=continuity,
    )


def _requests():
    return [
        {
            "novel_id": "great_king_zhuxia",
            "chapter": 1,
            "body": _luna_body(1, "第一章。"),
            "lineage_before": {
                "glossary_sha256": "a" * 64,
                "continuity_sha256": "b" * 64,
                "issues_sha256": "c" * 64,
                "progress_sha256": "d" * 64,
            },
        },
        {
            "novel_id": "rogue_first_prince",
            "chapter": 1,
            "body": _luna_body(1, "제1장."),
            "lineage_before": {
                "glossary_sha256": "1" * 64,
                "continuity_sha256": "2" * 64,
                "issues_sha256": "3" * 64,
                "progress_sha256": "4" * 64,
            },
        },
    ]


def _state_capture(initial=None):
    state = {"production": "unchanged"} if initial is None else initial
    calls = []

    def capture():
        calls.append(1)
        return json.loads(json.dumps(state))

    return state, calls, capture


def _prepare(tmp_path):
    state, calls, capture = _state_capture()
    manifest, target = prepare_wave(
        tmp_path,
        1,
        _requests(),
        capture_protected_state=capture,
        experiment_id="phase60-full-pipeline-v1",
        now=lambda: FIXED_TIME,
    )
    return state, calls, capture, manifest, target


def _load_manifest(target):
    return json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))


def _output_row(custom_id, content, *, cached_tokens=100, model=BATCH_MODEL):
    return {
        "id": f"req-{custom_id}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": f"http-{custom_id}",
            "body": {
                "id": f"chat-{custom_id}",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "prompt_tokens_details": {"cached_tokens": cached_tokens},
                },
            },
        },
        "error": None,
    }


def _complete_provider_batch(client, manifest):
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-wave-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=2,
        failed=0,
    )
    ids = [item["custom_id"] for item in manifest["requests"]]
    rows = [
        _output_row(ids[1], "Chapter 1\n\nKorean lineage translation."),
        _output_row(ids[0], "Chapter 1\n\nChinese lineage translation."),
    ]
    client.files.contents["file-wave-output-123"] = "\n".join(
        json.dumps(row) for row in rows
    ) + "\n"


def test_prepare_wave_freezes_two_lineages_without_provider_or_production_mutation(
    tmp_path,
):
    state, calls, _capture, manifest, target = _prepare(tmp_path)

    assert state == {"production": "unchanged"}
    assert len(calls) >= 2
    assert manifest["status"] == "prepared"
    assert manifest["wave_number"] == 1
    assert manifest["request_count"] == 2
    assert manifest["model"] == BATCH_MODEL
    assert manifest["paid_batch_authorized"] is False
    assert manifest["protected_state_verified_unchanged"] is True

    lines = [
        json.loads(line)
        for line in (target / INPUT_FILENAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert len({line["custom_id"] for line in lines}) == 2
    assert all(line["method"] == "POST" for line in lines)
    assert all(line["url"] == "/v1/chat/completions" for line in lines)
    assert all(line["body"]["model"] == BATCH_MODEL for line in lines)
    assert all(line["body"]["reasoning_effort"] == "none" for line in lines)
    assert all("lineage_before_sha256" in item for item in manifest["requests"])

    with pytest.raises(FullPipelineBatchSafetyError, match="already exists"):
        prepare_wave(
            tmp_path,
            1,
            _requests(),
            capture_protected_state=lambda: state,
            experiment_id="phase60-full-pipeline-v1",
        )


def test_prepare_rejects_deepseek_shaped_batch_request(tmp_path):
    state, _calls, capture = _state_capture()
    bad = _requests()[0]
    bad = json.loads(json.dumps(bad))
    bad["body"].pop("max_completion_tokens")
    bad["body"].pop("reasoning_effort")
    bad["body"]["max_tokens"] = 12000
    bad["body"]["extra_body"] = {"thinking": {"type": "disabled"}}

    with pytest.raises(FullPipelineBatchSafetyError):
        prepare_wave(
            tmp_path,
            1,
            [bad],
            capture_protected_state=capture,
            experiment_id="phase60-full-pipeline-v1",
        )
    assert state == {"production": "unchanged"}


def test_submit_requires_exact_authorization_before_any_remote_call(tmp_path):
    state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()

    with pytest.raises(FullPipelineBatchError, match="explicitly authorized"):
        submit_wave(
            target,
            client,
            "NO",
            capture_protected_state=capture,
        )

    assert client.files.create_calls == []
    assert client.batches.create_calls == []
    assert state == {"production": "unchanged"}
    assert _load_manifest(target)["status"] == "prepared"


def test_submit_persists_remote_ids_and_never_mutates_production(tmp_path):
    state, _calls, capture, manifest, target = _prepare(tmp_path)
    client = FakeClient()

    submitted = submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )

    assert submitted["status"] == "submitted"
    assert submitted["input_file_id"] == "file-wave-input-123"
    assert submitted["batch_id"] == "batch-wave-123"
    assert submitted["submission_metadata"]["run_id"] == manifest["run_id"]
    assert submitted["submission_metadata"]["wave"] == "1"
    assert len(client.files.create_calls) == 1
    assert len(client.batches.create_calls) == 1
    assert state == {"production": "unchanged"}


def test_ambiguous_upload_is_fail_closed_and_never_reissued(tmp_path):
    _state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    client.files.raise_on_create = RuntimeError("lost upload response")

    with pytest.raises(
        FullPipelineBatchSubmissionUncertain,
        match="automatic upload reissue is forbidden",
    ):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )

    assert _load_manifest(target)["status"] == "upload_uncertain"
    client.files.raise_on_create = None
    with pytest.raises(FullPipelineBatchError, match="automatic reissue is blocked"):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    assert len(client.files.create_calls) == 1
    assert client.batches.create_calls == []


def test_ambiguous_batch_creation_blocks_resubmit_and_can_reconcile_exact_match(
    tmp_path,
):
    state, _calls, capture, manifest, target = _prepare(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = RuntimeError("connection lost after POST")

    with pytest.raises(FullPipelineBatchSubmissionUncertain):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    persisted = _load_manifest(target)
    assert persisted["status"] == "submission_uncertain"
    assert persisted["input_file_id"] == "file-wave-input-123"
    assert persisted["batch_id"] is None

    client.batches.raise_on_create = None
    with pytest.raises(FullPipelineBatchError, match="automatic reissue is blocked"):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    assert len(client.batches.create_calls) == 1

    client.batches.batch.input_file_id = "file-wave-input-123"
    client.batches.batch.metadata = persisted["submission_metadata"]
    reconciled = reconcile_submission(
        target,
        client,
        capture_protected_state=capture,
    )
    assert reconciled["status"] == "submitted"
    assert reconciled["batch_id"] == "batch-wave-123"
    assert reconciled["reconciled_submission"] is True
    assert state == {"production": "unchanged"}
    assert manifest["run_id"] == reconciled["run_id"]


def test_hard_crash_after_batch_create_is_reconciled_without_second_create(tmp_path):
    _state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    persisted = _load_manifest(target)
    assert persisted["status"] == "submission_started"
    assert len(client.batches.create_calls) == 1

    client.batches.raise_on_create = None
    client.batches.batch.input_file_id = persisted["input_file_id"]
    client.batches.batch.metadata = persisted["submission_metadata"]
    reconciled = reconcile_submission(
        target,
        client,
        capture_protected_state=capture,
    )
    assert reconciled["batch_id"] == "batch-wave-123"
    assert len(client.batches.create_calls) == 1


def test_reconcile_requires_exactly_one_remote_identity_match(tmp_path):
    _state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = RuntimeError("lost")
    with pytest.raises(FullPipelineBatchSubmissionUncertain):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    client.batches.list_data = []
    with pytest.raises(
        FullPipelineBatchSubmissionUncertain,
        match="exactly one matching remote Batch; found 0",
    ):
        reconcile_submission(target, client, capture_protected_state=capture)


def test_status_refresh_records_provider_state_without_inference(tmp_path):
    state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    client.batches.batch.status = "in_progress"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=1,
        failed=0,
    )

    refreshed = refresh_status(
        target,
        client,
        capture_protected_state=capture,
    )
    assert refreshed["status"] == "batch_in_progress"
    assert refreshed["provider_batch"]["request_counts"] == {
        "total": 2,
        "completed": 1,
        "failed": 0,
    }
    assert client.batches.retrieve_calls == ["batch-wave-123"]
    assert state == {"production": "unchanged"}
    assert not (target / RESULTS_FILENAME).exists()


def test_collect_maps_out_of_order_rows_preserves_raw_and_prices_batch(tmp_path):
    state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    submitted = submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    _complete_provider_batch(client, submitted)

    document, collected = collect_wave(
        target,
        client,
        capture_protected_state=capture,
        now=lambda: FIXED_TIME,
    )

    assert collected["status"] == "collected"
    assert document["status"] == "complete"
    assert document["transport"] == "openai_batch"
    assert document["batch_id"] == "batch-wave-123"
    assert [item["novel_id"] for item in document["requests"]] == [
        "great_king_zhuxia",
        "rogue_first_prince",
    ]
    assert [item["chapter"] for item in document["requests"]] == [1, 1]
    assert all(item["provider_response_model"] == BATCH_MODEL for item in document["requests"])
    assert all(item["stats"]["pricing_mode"] == "batch_actual" for item in document["requests"])
    assert all(item["stats"]["cache_hit_tokens"] == 100 for item in document["requests"])
    assert all(item["stats"]["cache_miss_tokens"] == 900 for item in document["requests"])
    assert document["totals"]["prompt_tokens"] == 2000
    assert document["totals"]["completion_tokens"] == 1000
    expected_each = 100 / 1_000_000 * 0.01 + 900 / 1_000_000 * 0.10 + 500 / 1_000_000 * 0.60
    assert document["totals"]["cost_usd"] == pytest.approx(expected_each * 2)
    assert (target / OUTPUT_FILENAME).is_file()
    assert (target / RESULTS_FILENAME).is_file()
    assert collected["output_sha256"]
    assert collected["results_sha256"]
    assert state == {"production": "unchanged"}


def test_collected_wave_replays_local_evidence_without_provider_access(tmp_path):
    _state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    submitted = submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    _complete_provider_batch(client, submitted)
    first, _ = collect_wave(
        target,
        client,
        capture_protected_state=capture,
        now=lambda: FIXED_TIME,
    )
    retrieve_count = len(client.batches.retrieve_calls)
    content_count = len(client.files.content_calls)

    replay, _ = collect_wave(
        target,
        client,
        capture_protected_state=capture,
        now=lambda: pytest.fail("replay must not call the clock"),
    )
    assert replay == first
    assert len(client.batches.retrieve_calls) == retrieve_count
    assert len(client.files.content_calls) == content_count


def test_collection_rejects_missing_or_wrong_provider_identity(tmp_path):
    _state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    submitted = submit_wave(
        target,
        client,
        SUBMIT_AUTHORIZATION,
        capture_protected_state=capture,
    )
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-wave-output-123"
    client.batches.batch.request_counts = SimpleNamespace(total=2, completed=2, failed=0)
    ids = [item["custom_id"] for item in submitted["requests"]]
    client.files.contents["file-wave-output-123"] = json.dumps(
        _output_row(ids[0], "Only one row.", model="wrong-model")
    ) + "\n"

    with pytest.raises(FullPipelineBatchSafetyError):
        collect_wave(target, client, capture_protected_state=capture)
    assert (target / OUTPUT_FILENAME).is_file()
    assert not (target / RESULTS_FILENAME).exists()


def test_protected_production_state_change_blocks_remote_work(tmp_path):
    state, _calls, capture, _manifest, target = _prepare(tmp_path)
    client = FakeClient()
    state["production"] = "changed"

    with pytest.raises(
        FullPipelineBatchSafetyError,
        match="protected production state changed",
    ):
        submit_wave(
            target,
            client,
            SUBMIT_AUTHORIZATION,
            capture_protected_state=capture,
        )
    assert client.files.create_calls == []
    assert client.batches.create_calls == []
