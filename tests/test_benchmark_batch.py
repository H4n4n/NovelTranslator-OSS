import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark_batch import (
    BATCH_ERROR_FILENAME,
    BATCH_INPUT_FILENAME,
    BATCH_MANIFEST_FILENAME,
    BATCH_OUTPUT_FILENAME,
    SUBMIT_AUTHORIZATION,
    BatchBenchmarkError,
    BatchSubmissionUncertain,
    collect_batch,
    prepare_batch,
    reconcile_submission,
    refresh_status,
    submit_batch,
)
from benchmark_novel import capture_protected_state, load_benchmark_inputs
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number, text):
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": 1,
        "character_count": len(text),
        "text": text,
    }


def _make_project(tmp_path):
    _prepare_repo(tmp_path)
    config = NovelConfig(
        id="batch_fixture",
        title="Batch Fixture",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=2,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)
    project.chapters_file.write_text(
        json.dumps(
            [
                _chapter(1, "姚雲使用神通。"),
                _chapter(2, "姚雲再次使用神通。"),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps(
            {
                "entries": {
                    "姚雲": {
                        "english": "Yao Yun",
                        "category": "character",
                        "status": "locked",
                    },
                    "神通": {
                        "english": "divine ability",
                        "category": "specialized recurring term",
                        "status": "locked",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project


class FakeFiles:
    def __init__(self):
        self.create_calls = []
        self.content_calls = []
        self.contents = {}

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            id="file-input-123",
            purpose="batch",
            filename="batch_input.jsonl",
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
            id="batch-123",
            status="validating",
            endpoint="/v1/chat/completions",
            input_file_id="file-input-123",
            output_file_id=None,
            error_file_id=None,
            completion_window="24h",
            created_at=FIXED_TIME.timestamp(),
            completed_at=None,
            expires_at=None,
            request_counts=SimpleNamespace(total=2, completed=0, failed=0),
            metadata={
                "benchmark": "luna-batch-parity-v1",
                "run_id": "",
                "novel_id": "batch_fixture",
            },
        )

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
        return SimpleNamespace(data=[self.batch])


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


def _prepared(tmp_path, chapters=(1, 2), output_dir="batch-test"):
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    before = capture_protected_state(project)
    manifest, target = prepare_batch(
        inputs,
        chapters,
        output_dir=output_dir,
        now=lambda: FIXED_TIME,
    )
    return project, inputs, before, manifest, target


def _load_manifest(target):
    return json.loads(
        (target / BATCH_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def _batch_output_row(custom_id, translation, *, cached_tokens=100):
    return {
        "id": f"req-{custom_id}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": f"http-{custom_id}",
            "body": {
                "id": f"chat-{custom_id}",
                "model": "gpt-5.6-luna",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": translation,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "prompt_tokens_details": {
                        "cached_tokens": cached_tokens,
                    },
                },
            },
        },
        "error": None,
    }


def test_prepare_batch_is_zero_api_deterministic_and_protects_state(tmp_path):
    project, inputs, before, manifest, target = _prepared(tmp_path)

    assert manifest["status"] == "prepared"
    assert manifest["model"] == "gpt-5.6-luna"
    assert manifest["reasoning"] == "none"
    assert manifest["request_count"] == 2
    assert manifest["paid_batch_authorized"] is False
    assert manifest["protected_state_verified_unchanged"] is True
    assert capture_protected_state(project) == before
    assert manifest["input_sha256"] == hashlib.sha256(
        (target / BATCH_INPUT_FILENAME).read_bytes()
    ).hexdigest()

    lines = [
        json.loads(line)
        for line in (target / BATCH_INPUT_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert len({line["custom_id"] for line in lines}) == 2
    assert [line["body"]["model"] for line in lines] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
    ]
    assert all(line["method"] == "POST" for line in lines)
    assert all(line["url"] == "/v1/chat/completions" for line in lines)
    assert all(line["body"]["reasoning_effort"] == "none" for line in lines)
    assert all(line["body"]["max_completion_tokens"] == 12000 for line in lines)
    assert all("CHINESE SOURCE" in line["body"]["messages"][1]["content"] for line in lines)

    with pytest.raises(Exception, match="already exists"):
        prepare_batch(inputs, [1, 2], output_dir="batch-test")


def test_submit_requires_exact_authorization_before_any_remote_call(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()

    with pytest.raises(BatchBenchmarkError, match="explicitly authorized"):
        submit_batch(inputs, "batch-test", client, "NO")

    assert client.files.create_calls == []
    assert client.batches.create_calls == []
    assert capture_protected_state(project) == before
    assert _load_manifest(target)["status"] == "prepared"


def test_submit_persists_upload_and_batch_identity_without_production_mutation(tmp_path):
    project, inputs, before, manifest, target = _prepared(tmp_path)
    client = FakeClient()

    result = submit_batch(
        inputs,
        "batch-test",
        client,
        SUBMIT_AUTHORIZATION,
    )

    assert result["status"] == "submitted"
    assert result["input_file_id"] == "file-input-123"
    assert result["batch_id"] == "batch-123"
    assert result["paid_batch_authorized"] is True
    assert len(client.files.create_calls) == 1
    assert len(client.batches.create_calls) == 1
    create = client.batches.create_calls[0]
    assert create["endpoint"] == "/v1/chat/completions"
    assert create["completion_window"] == "24h"
    assert create["metadata"]["run_id"] == manifest["run_id"]
    assert create["metadata"]["benchmark"] == "luna-batch-parity-v1"
    assert capture_protected_state(project) == before
    persisted = _load_manifest(target)
    assert persisted["batch_id"] == "batch-123"
    assert persisted["status"] == "submitted"


def test_ambiguous_batch_creation_blocks_automatic_reissue(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = RuntimeError("connection lost after POST")

    with pytest.raises(BatchSubmissionUncertain, match="do not reissue"):
        submit_batch(
            inputs,
            "batch-test",
            client,
            SUBMIT_AUTHORIZATION,
        )

    persisted = _load_manifest(target)
    assert persisted["status"] == "submission_uncertain"
    assert persisted["input_file_id"] == "file-input-123"
    assert persisted["batch_id"] is None
    assert capture_protected_state(project) == before

    client.batches.raise_on_create = None
    with pytest.raises(BatchBenchmarkError, match="automatic reissue is blocked"):
        submit_batch(
            inputs,
            "batch-test",
            client,
            SUBMIT_AUTHORIZATION,
        )
    assert len(client.batches.create_calls) == 1




def test_hard_crash_after_batch_create_boundary_is_reconcilable_not_resubmittable(
    tmp_path,
):
    project, inputs, before, manifest, target = _prepared(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    persisted = _load_manifest(target)
    assert persisted["status"] == "submission_started"
    assert persisted["input_file_id"] == "file-input-123"
    assert persisted["batch_id"] is None

    # A normal submit must remain blocked from this ambiguous boundary.
    client.batches.raise_on_create = None
    with pytest.raises(BatchBenchmarkError, match="automatic reissue is blocked"):
        submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)
    assert len(client.batches.create_calls) == 1

    # Simulate the provider having created the Batch before the local crash.
    client.batches.batch.input_file_id = "file-input-123"
    client.batches.batch.metadata = {
        "benchmark": "luna-batch-parity-v1",
        "run_id": manifest["run_id"],
        "novel_id": "batch_fixture",
    }

    adopted = reconcile_submission(inputs, "batch-test", client)
    assert adopted["batch_id"] == "batch-123"
    assert adopted["status"] == "submitted"
    assert len(client.batches.create_calls) == 1
    assert capture_protected_state(project) == before


def test_uncertain_submission_can_adopt_exactly_one_matching_remote_batch(tmp_path):
    project, inputs, before, manifest, target = _prepared(tmp_path)
    client = FakeClient()
    client.batches.raise_on_create = RuntimeError("lost response")

    with pytest.raises(BatchSubmissionUncertain):
        submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.raise_on_create = None
    client.batches.batch.metadata = {
        "benchmark": "luna-batch-parity-v1",
        "run_id": manifest["run_id"],
        "novel_id": "batch_fixture",
    }
    client.batches.batch.input_file_id = "file-input-123"

    adopted = reconcile_submission(inputs, "batch-test", client)

    assert adopted["status"] == "submitted"
    assert adopted["batch_id"] == "batch-123"
    assert len(client.batches.create_calls) == 1
    assert len(client.batches.list_calls) == 1
    assert capture_protected_state(project) == before
    assert _load_manifest(target)["batch_id"] == "batch-123"


def test_status_refresh_records_provider_state_without_inference(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "in_progress"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=1,
        failed=0,
    )
    refreshed = refresh_status(inputs, "batch-test", client)

    assert refreshed["status"] == "batch_in_progress"
    assert refreshed["provider_batch"]["request_counts"] == {
        "total": 2,
        "completed": 1,
        "failed": 0,
    }
    assert len(client.batches.retrieve_calls) == 1
    assert capture_protected_state(project) == before
    assert not (target / "results.json").exists()


def test_collect_maps_out_of_order_custom_ids_and_records_actual_batch_cost(tmp_path):
    project, inputs, before, manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submitted = submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=2,
        failed=0,
    )
    ids = [item["custom_id"] for item in submitted["requests"]]
    rows = [
        _batch_output_row(ids[1], "Chapter 2\n\nSecond English translation."),
        _batch_output_row(ids[0], "Chapter 1\n\nFirst English translation."),
    ]
    client.files.contents["file-output-123"] = "\n".join(
        json.dumps(row) for row in rows
    ) + "\n"

    document, collected = collect_batch(
        inputs,
        "batch-test",
        client,
        now=lambda: FIXED_TIME,
    )

    assert collected["status"] == "collected"
    assert document["status"] == "complete"
    assert document["transport"]["type"] == "openai_batch"
    assert document["transport"]["batch_id"] == "batch-123"
    assert document["safety"]["protected_state_verified_unchanged"] is True
    assert [
        item["chapter_number"] for item in document["chapters"]
    ] == [1, 2]
    assert all(item["transport"] == "openai_batch" for item in document["chapters"])
    assert all(item["pricing_mode"] == "batch_actual" for item in document["chapters"])
    assert all(item["cache_hit_tokens"] == 100 for item in document["chapters"])
    assert all(item["cache_miss_tokens"] == 900 for item in document["chapters"])
    assert (target / "chapter_0001.txt").read_text(encoding="utf-8").startswith(
        "Chapter 1"
    )
    assert (target / "chapter_0002.txt").read_text(encoding="utf-8").startswith(
        "Chapter 2"
    )
    assert (target / BATCH_OUTPUT_FILENAME).is_file()
    assert capture_protected_state(project) == before

    # Luna Batch: $0.01/M cached + $0.10/M uncached + $0.60/M output.
    expected_one = 100 / 1_000_000 * 0.01 + 900 / 1_000_000 * 0.10
    expected_one += 500 / 1_000_000 * 0.60
    assert document["chapters"][0]["total_cost_usd"] == pytest.approx(expected_one)
    assert document["totals"]["total_cost_usd"] == pytest.approx(expected_one * 2)

    # Re-collection is idempotent: exact prior materialization is verified.
    second_document, second_manifest = collect_batch(
        inputs,
        "batch-test",
        client,
        now=lambda: FIXED_TIME,
    )
    assert second_manifest["status"] == "collected"
    assert second_document["chapters"] == document["chapters"]
    assert capture_protected_state(project) == before




def test_collect_requires_exact_completed_request_count(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=1,
        failed=0,
    )

    with pytest.raises(BatchBenchmarkError, match="exactly match"):
        collect_batch(inputs, "batch-test", client)

    assert _load_manifest(target)["status"] == "batch_completed_count_mismatch"
    assert capture_protected_state(project) == before
    assert not (target / "results.json").exists()




def test_collect_validates_all_rows_before_materializing_any_chapter(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submitted = submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=2,
        failed=0,
    )
    ids = [item["custom_id"] for item in submitted["requests"]]
    good = _batch_output_row(ids[0], "Chapter 1\n\nValid first translation.")
    bad = _batch_output_row(ids[1], "Chapter 2\n\nMalformed second translation.")
    bad["response"]["body"]["choices"][0]["finish_reason"] = "length"
    client.files.contents["file-output-123"] = "\n".join(
        json.dumps(row) for row in (good, bad)
    ) + "\n"

    with pytest.raises(BatchBenchmarkError, match="finish_reason"):
        collect_batch(inputs, "batch-test", client)

    # Raw paid provider output is durably retained for diagnosis/recovery,
    # but no translated chapter/result materialization occurred.
    assert (target / BATCH_OUTPUT_FILENAME).is_file()
    assert not (target / "chapter_0001.txt").exists()
    assert not (target / "chapter_0002.txt").exists()
    assert not (target / "results.json").exists()
    assert capture_protected_state(project) == before


def test_collect_resumes_after_exact_partial_materialization(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submitted = submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=2,
        failed=0,
    )
    ids = [item["custom_id"] for item in submitted["requests"]]
    rows = [
        _batch_output_row(ids[0], "Chapter 1\n\nFirst English translation."),
        _batch_output_row(ids[1], "Chapter 2\n\nSecond English translation."),
    ]
    client.files.contents["file-output-123"] = "\n".join(
        json.dumps(row) for row in rows
    ) + "\n"

    # Simulate a hard crash after the first chapter was written by a prior
    # collection attempt but before the rest/results/manifest were finalized.
    (target / "chapter_0001.txt").write_text(
        "Chapter 1\n\nFirst English translation.",
        encoding="utf-8",
    )

    document, manifest = collect_batch(
        inputs,
        "batch-test",
        client,
        now=lambda: FIXED_TIME,
    )

    assert manifest["status"] == "collected"
    assert document["status"] == "complete"
    assert (target / "chapter_0001.txt").read_text(encoding="utf-8") == (
        "Chapter 1\n\nFirst English translation."
    )
    assert (target / "chapter_0002.txt").read_text(encoding="utf-8") == (
        "Chapter 2\n\nSecond English translation."
    )
    assert (target / "results.json").is_file()
    assert capture_protected_state(project) == before


def test_collect_refuses_mismatched_partial_materialization(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submitted = submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=2,
        failed=0,
    )
    ids = [item["custom_id"] for item in submitted["requests"]]
    rows = [
        _batch_output_row(ids[0], "Chapter 1\n\nProvider translation."),
        _batch_output_row(ids[1], "Chapter 2\n\nSecond translation."),
    ]
    client.files.contents["file-output-123"] = "\n".join(
        json.dumps(row) for row in rows
    ) + "\n"

    (target / "chapter_0001.txt").write_text(
        "DIFFERENT LOCAL CONTENT",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="different bytes"):
        collect_batch(inputs, "batch-test", client)

    assert not (target / "chapter_0002.txt").exists()
    assert not (target / "results.json").exists()
    assert capture_protected_state(project) == before


def test_collect_fails_closed_if_completed_batch_reports_failed_requests(tmp_path):
    project, inputs, before, _manifest, target = _prepared(tmp_path)
    client = FakeClient()
    submit_batch(inputs, "batch-test", client, SUBMIT_AUTHORIZATION)

    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = "file-output-123"
    client.batches.batch.error_file_id = "file-error-123"
    client.batches.batch.request_counts = SimpleNamespace(
        total=2,
        completed=1,
        failed=1,
    )

    with pytest.raises(BatchBenchmarkError, match="failed requests"):
        collect_batch(inputs, "batch-test", client)

    assert _load_manifest(target)["status"] == "batch_completed_with_failures"
    assert capture_protected_state(project) == before
    assert not (target / "results.json").exists()
    assert not (target / BATCH_OUTPUT_FILENAME).exists()
    assert not (target / BATCH_ERROR_FILENAME).exists()


def test_batch_client_disables_sdk_retries_and_module_is_import_safe():
    source = (REPO_ROOT / "benchmark_batch.py").read_text(encoding="utf-8")
    client_block = source[
        source.index("def create_openai_client():"):
        source.index("def _load_context(")
    ]
    assert "max_retries=0" in client_block
    assert "OPENAI_API_KEY" in client_block

    prefix = source[: source.index("def create_openai_client():")]
    assert "load_dotenv()" not in prefix
    assert "OpenAI(" not in prefix
    assert ".batches.create(" not in prefix
    assert ".files.create(" not in prefix
