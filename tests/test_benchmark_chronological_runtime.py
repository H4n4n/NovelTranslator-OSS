import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_chronological_runtime as rt
from benchmark_novel import BenchmarkSafetyError


MAIN = "5" * 40
CANONICAL_ID = "a" * 64
CANONICAL_PLAN_SHA = "b" * 64
CANONICAL_TREE_SHA = "c" * 64


def _initial_state(novel_id, language):
    return {
        "schema_version": 1,
        "policy_version": "chronological-state-v1",
        "benchmark": "chronological-state-growth-v1",
        "novel_id": novel_id,
        "source_language": language,
        "profile_id": "test-profile",
        "through_chapter": 0,
        "glossary_entries": {},
        "continuity_summary": "",
        "issues": [],
        "parent_state_sha256": None,
        "state_events": [],
    }


def _canonical_fixture(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    novels = []
    for novel_id, language in (
        ("great_king_zhuxia", "zh"),
        ("rogue_first_prince", "ko"),
    ):
        novel_root = root / novel_id
        prompt = novel_root / "prompts" / "profile.txt"
        prompt.parent.mkdir(parents=True)
        prompt.write_text(f"PROFILE {novel_id}", encoding="utf-8")
        state = _initial_state(novel_id, language)
        state_path = novel_root / "states" / "state_0000.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        records = []
        for chapter in range(1, 26):
            source = novel_root / "corpus" / f"chapter_{chapter:04d}.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"SOURCE {novel_id} {chapter}", encoding="utf-8")
            records.append(
                {
                    "chapter_number": chapter,
                    "source_snapshot_path": source.relative_to(root).as_posix(),
                    "source_sha256": rt._sha256_file(source),
                    "source_label": f"SOURCE-{language}",
                    "source_heading": f"Chapter {chapter}",
                }
            )
        novels.append(
            {
                "novel_id": novel_id,
                "source_language": language,
                "chapters": list(range(1, 26)),
                "chapter_records": records,
                "provider_visible_system_prompt": {
                    "path": prompt.relative_to(root).as_posix(),
                    "sha256": rt._sha256_file(prompt),
                },
                "initial_state": {
                    "path": state_path.relative_to(root).as_posix(),
                    "identity_sha256": rt._canonical_json_sha256(state),
                },
            }
        )
    plan = {
        "schema_version": 1,
        "benchmark": "chronological-state-growth-v1",
        "status": "frozen",
        "identity_sha256": CANONICAL_ID,
        "novels": novels,
    }
    (root / "chronological_state_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    return root, plan


def _runtime_plan(tmp_path, canonical_root, canonical_plan):
    run_root = tmp_path / "run"
    run_root.mkdir()
    configs = {
        arm: {
            **rt.EXPECTED_MODEL_CONFIG[arm].copy(),
            "pricing_version": "test",
            "prices": {},
            "economy_pricing_version": "test-economy",
            "economy_prices": {},
            "economy_mode": "test",
        }
        for arm in rt.SYNC_ARMS
    }
    plan = {
        "schema_version": rt.RUNTIME_SCHEMA_VERSION,
        "kind": rt.RUNTIME_KIND,
        "run_id": "r" * 64,
        "canonical": {
            "root": str(canonical_root),
            "identity_sha256": CANONICAL_ID,
            "plan_sha256": CANONICAL_PLAN_SHA,
            "tree_sha256": CANONICAL_TREE_SHA,
        },
        "repo_root": str(tmp_path / "repo"),
        "repo_head": MAIN,
        "budgets": {
            f"{rt.FLASH_ARM}_usd": 0.75,
            f"{rt.LUNA_ARM}_usd": 0.75,
            f"{rt.BATCH_ARM}_usd": 0.75,
        },
        "identity": {
            "model_configs": configs,
        },
        "protected_state_before": {"stable": True},
    }
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
    return run_root, plan


def _fake_document(term="甲", english="Jia"):
    return {
        "translation": "Complete English translation.",
        "state_update": {
            "glossary_upserts": [
                {
                    "source": term,
                    "english": english,
                    "category": "character",
                    "confidence": "high",
                    "note": "name",
                }
            ],
            "continuity_summary": "A compact continuity note.",
            "issues": ["One unresolved identity."],
        },
    }


def _fake_response(model, document=None, finish_reason="stop"):
    if document is None:
        document = _fake_document()
    return {
        "id": "resp-test",
        "model": model,
        "system_fingerprint": "fp-test",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": json.dumps(document, ensure_ascii=False)
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }


class _FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeSyncClient:
    def __init__(self, response=None, error=None):
        self.completions = _FakeCompletions(response=response, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _patch_sync_environment(monkeypatch, canonical_plan):
    monkeypatch.setattr(rt, "_ensure_repo_head", lambda repo, expected: expected)
    monkeypatch.setattr(
        rt,
        "_validate_canonical",
        lambda root, **kwargs: (
            canonical_plan,
            kwargs["expected_plan_sha256"],
            kwargs["expected_tree_sha256"],
        ),
    )
    monkeypatch.setattr(
        rt,
        "_ensure_protected_state",
        lambda repo, expected, stage: expected,
    )
    monkeypatch.setattr(rt, "_protected_state", lambda repo: {"stable": True})


def test_parse_model_document_and_state_update_supports_correction():
    parsed = rt.parse_model_document(json.dumps(_fake_document()))
    before = _initial_state("great_king_zhuxia", "zh")
    after1, metrics1 = rt.apply_state_update(before, parsed, 1)
    assert after1["through_chapter"] == 1
    assert after1["glossary_entries"]["甲"]["english"] == "Jia"
    assert metrics1["glossary_added"] == 1

    corrected = rt.parse_model_document(
        json.dumps(_fake_document(term="甲", english="Corrected Jia"))
    )
    after2, metrics2 = rt.apply_state_update(after1, corrected, 2)
    assert after2["through_chapter"] == 2
    assert after2["glossary_entries"]["甲"]["english"] == "Corrected Jia"
    assert after2["glossary_entries"]["甲"]["first_chapter"] == 1
    assert metrics2["glossary_revised"] == 1
    assert after2["parent_state_sha256"] == rt._canonical_json_sha256(after1)


def test_parse_model_document_accepts_json_fence_and_rejects_duplicate_terms():
    text = "```json\n" + json.dumps(_fake_document(), ensure_ascii=False) + "\n```"
    assert rt.parse_model_document(text)["translation"]

    bad = _fake_document()
    bad["state_update"]["glossary_upserts"].append(
        dict(bad["state_update"]["glossary_upserts"][0])
    )
    with pytest.raises(rt.ChronologicalRuntimeError, match="duplicate source term"):
        rt.parse_model_document(json.dumps(bad, ensure_ascii=False))


def test_apply_state_update_requires_exact_parent_chapter():
    before = _initial_state("great_king_zhuxia", "zh")
    before["through_chapter"] = 7
    with pytest.raises(BenchmarkSafetyError, match="state chain mismatch"):
        rt.apply_state_update(
            before,
            rt.parse_model_document(json.dumps(_fake_document())),
            1,
        )


def test_prepare_run_is_zero_provider_and_binds_frozen_inputs(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root = tmp_path / "external-run"

    monkeypatch.setattr(rt, "_ensure_repo_head", lambda root, expected: expected)
    monkeypatch.setattr(
        rt,
        "_validate_canonical",
        lambda root, **kwargs: (
            canonical_plan,
            CANONICAL_PLAN_SHA,
            CANONICAL_TREE_SHA,
        ),
    )
    monkeypatch.setattr(rt, "_protected_state", lambda root: {"stable": True})
    monkeypatch.setattr(
        rt,
        "_ensure_protected_state",
        lambda root, expected, stage: expected,
    )

    document = rt.prepare_run(
        repo_root=repo,
        expected_repo_head=MAIN,
        canonical_root=canonical_root,
        expected_canonical_plan_sha256=CANONICAL_PLAN_SHA,
        expected_canonical_identity_sha256=CANONICAL_ID,
        expected_canonical_tree_sha256=CANONICAL_TREE_SHA,
        run_root=run_root,
    )

    assert document["status"] == "prepared"
    assert document["canonical"]["identity_sha256"] == CANONICAL_ID
    assert document["authorization"]["automatic_retry_forbidden"] is True
    assert document["identity"]["case_count"] == 50
    assert document["identity"]["batch_design"]["batch_does_not_mutate_state_lineage"] is True
    assert rt._journal_events(run_root / rt.RUN_JOURNAL_FILENAME) == ["prepared"]


def test_execute_sync_next_completes_one_case_and_advances_only_its_lineage(
    monkeypatch, tmp_path
):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)

    client = _FakeSyncClient(
        response=_fake_response(rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"])
    )
    complete = rt.execute_sync_next(
        run_root,
        arm=rt.FLASH_ARM,
        authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
        client_factory=lambda config: client,
    )

    assert client.completions.calls == 1
    assert complete["status"] == "complete"
    assert complete["identity"]["novel_id"] == "great_king_zhuxia"
    assert complete["identity"]["chapter_number"] == 1
    assert complete["safety"]["automatic_retry_forbidden"] is True

    state_after = rt._load_json(
        rt._case_dir(run_root, rt.FLASH_ARM, "great_king_zhuxia", 1)
        / "state_after.json",
        "state",
    )
    assert state_after["through_chapter"] == 1
    assert state_after["glossary_entries"]["甲"]["english"] == "Jia"

    status = rt.sync_arm_status(run_plan, run_root, rt.FLASH_ARM)
    assert status["completed_total"] == 1
    assert status["next_case"]["novel_id"] == "rogue_first_prince"
    assert status["next_case"]["chapter_number"] == 1


def test_transport_exception_is_uncertain_and_restart_blocks_reissue(
    monkeypatch, tmp_path
):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)

    first = _FakeSyncClient(error=TimeoutError("ambiguous timeout"))
    with pytest.raises(rt.ChronologicalUncertainError, match="retry is forbidden"):
        rt.execute_sync_next(
            run_root,
            arm=rt.FLASH_ARM,
            authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
            client_factory=lambda config: first,
        )
    assert first.completions.calls == 1

    second = _FakeSyncClient(
        response=_fake_response(rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"])
    )
    with pytest.raises(BenchmarkSafetyError, match="further paid calls are blocked"):
        rt.execute_sync_next(
            run_root,
            arm=rt.FLASH_ARM,
            authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
            client_factory=lambda config: second,
        )
    assert second.completions.calls == 0


@pytest.mark.parametrize(
    "response,pattern",
    [
        (_fake_response("wrong-model"), "validation failed"),
        (
            _fake_response(
                rt.EXPECTED_MODEL_CONFIG[rt.LUNA_ARM]["model"],
                finish_reason="length",
            ),
            "validation failed",
        ),
    ],
)
def test_response_identity_or_finish_failure_blocks_luna(
    monkeypatch, tmp_path, response, pattern
):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)

    client = _FakeSyncClient(response=response)
    with pytest.raises(rt.ChronologicalRuntimeError, match=pattern):
        rt.execute_sync_next(
            run_root,
            arm=rt.LUNA_ARM,
            authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.LUNA_ARM],
            client_factory=lambda config: client,
        )

    status = rt.sync_arm_status(run_plan, run_root, rt.LUNA_ARM)
    assert status["counts"]["response_failed"] == 1
    assert status["blocked"] is True


def test_fresh_status_marks_tampered_complete_invalid(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)

    client = _FakeSyncClient(
        response=_fake_response(rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"])
    )
    rt.execute_sync_next(
        run_root,
        arm=rt.FLASH_ARM,
        authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
        client_factory=lambda config: client,
    )
    case_dir = rt._case_dir(run_root, rt.FLASH_ARM, "great_king_zhuxia", 1)
    complete = json.loads((case_dir / "complete.json").read_text(encoding="utf-8"))
    complete["provider_response"]["response_model"] = "tampered"
    (case_dir / "complete.json").write_text(json.dumps(complete), encoding="utf-8")

    status = rt.sync_arm_status(run_plan, run_root, rt.FLASH_ARM)
    assert status["counts"]["invalid_complete"] == 1
    assert status["blocked"] is True


def test_chapter_two_uses_own_novel_chapter_one_state(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)

    responses = [
        _fake_response(
            rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"],
            _fake_document(term="中一", english="CN-One"),
        ),
        _fake_response(
            rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"],
            _fake_document(term="한일", english="KR-One"),
        ),
        _fake_response(
            rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"],
            _fake_document(term="中二", english="CN-Two"),
        ),
    ]

    class SequenceClient:
        def __init__(self):
            self.calls = 0
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        def create(self, **kwargs):
            value = responses[self.calls]
            self.calls += 1
            return value

    client = SequenceClient()
    for _ in range(3):
        rt.execute_sync_next(
            run_root,
            arm=rt.FLASH_ARM,
            authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
            client_factory=lambda config: client,
        )

    cn2_request = json.loads(
        (
            rt._case_dir(run_root, rt.FLASH_ARM, "great_king_zhuxia", 2)
            / "request.json"
        ).read_text(encoding="utf-8")
    )
    prompt = cn2_request["messages"][1]["content"]
    assert "中一" in prompt
    assert "한일" not in prompt


def test_prepare_batch_replay_requires_complete_luna(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    monkeypatch.setattr(
        rt,
        "sync_arm_status",
        lambda plan, root, arm: {"complete": False},
    )
    with pytest.raises(BenchmarkSafetyError, match="fully complete"):
        rt.prepare_batch_replay(run_root)


def _install_fake_luna_complete_requests(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)

    monkeypatch.setattr(
        rt,
        "sync_arm_status",
        lambda plan, root, arm: {"complete": True},
    )

    def fake_complete(root, canonical_root_arg, canonical_plan_arg, arm, novel_id, chapter, **kwargs):
        case = rt._case_dir(root, rt.LUNA_ARM, novel_id, chapter)
        case.mkdir(parents=True, exist_ok=True)
        request = {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "same"},
                {"role": "user", "content": f"{novel_id}-{chapter}"},
            ],
            "max_completion_tokens": 12000,
            "reasoning_effort": "none",
        }
        request_path = case / "request.json"
        if not request_path.exists():
            request_path.write_bytes(rt._canonical_json_bytes(request))
        request_sha = rt._sha256_file(request_path)
        return {
            "identity": {"state_before_identity_sha256": f"state-{novel_id}-{chapter}"},
            "artifacts": {"request_sha256": request_sha},
        }

    monkeypatch.setattr(rt, "_validate_sync_complete", fake_complete)
    return canonical_root, canonical_plan, run_root, run_plan


def test_batch_prepare_replays_exact_fifty_luna_requests(monkeypatch, tmp_path):
    _, _, run_root, _ = _install_fake_luna_complete_requests(monkeypatch, tmp_path)

    manifest = rt.prepare_batch_replay(run_root)
    assert manifest["status"] == "prepared"
    assert manifest["identity"]["request_count"] == 50
    input_lines = (
        rt._batch_dir(run_root) / rt.BATCH_INPUT_FILENAME
    ).read_text(encoding="utf-8").splitlines()
    assert len(input_lines) == 50
    rows = [json.loads(line) for line in input_lines]
    assert len({row["custom_id"] for row in rows}) == 50
    assert all(row["url"] == "/v1/chat/completions" for row in rows)
    assert all(row["body"]["model"] == "gpt-5.6-luna" for row in rows)


class _FakeFiles:
    def __init__(self):
        self.upload_calls = 0
        self.content_payload = b""

    def create(self, **kwargs):
        self.upload_calls += 1
        return {"id": "file-input"}

    def content(self, file_id):
        assert file_id == "file-output"
        return self.content_payload


class _FakeBatches:
    def __init__(self, create_error=None):
        self.create_error = create_error
        self.create_calls = 0
        self.retrieve_calls = 0

    def create(self, **kwargs):
        self.create_calls += 1
        if self.create_error:
            raise self.create_error
        return {
            "id": "batch-1",
            "status": "validating",
            "endpoint": "/v1/chat/completions",
            "input_file_id": "file-input",
            "output_file_id": None,
            "error_file_id": None,
            "completion_window": "24h",
            "request_counts": {"total": 50, "completed": 0, "failed": 0},
            "metadata": kwargs["metadata"],
        }

    def retrieve(self, batch_id):
        self.retrieve_calls += 1
        return {
            "id": batch_id,
            "status": "completed",
            "endpoint": "/v1/chat/completions",
            "input_file_id": "file-input",
            "output_file_id": "file-output",
            "error_file_id": None,
            "completion_window": "24h",
            "request_counts": {"total": 50, "completed": 50, "failed": 0},
            "metadata": {},
        }


class _FakeBatchClient:
    def __init__(self, create_error=None):
        self.files = _FakeFiles()
        self.batches = _FakeBatches(create_error=create_error)


def _patch_batch_environment(monkeypatch):
    monkeypatch.setattr(rt, "_ensure_repo_head", lambda repo, expected: expected)
    monkeypatch.setattr(
        rt,
        "_ensure_protected_state",
        lambda repo, expected, stage: expected,
    )


def test_batch_submit_is_one_upload_one_create_and_records_no_retry(
    monkeypatch, tmp_path
):
    _, _, run_root, _ = _install_fake_luna_complete_requests(monkeypatch, tmp_path)
    rt.prepare_batch_replay(run_root)
    _patch_batch_environment(monkeypatch)
    client = _FakeBatchClient()

    manifest = rt.submit_batch_replay(
        run_root,
        authorization=rt.BATCH_SUBMIT_AUTHORIZATION,
        client=client,
    )

    assert client.files.upload_calls == 1
    assert client.batches.create_calls == 1
    assert manifest["status"] == "submitted"
    assert manifest["batch_id"] == "batch-1"
    assert manifest["authorization"]["automatic_retry_forbidden"] is True


def test_batch_submission_ambiguity_is_terminal_without_reissue(
    monkeypatch, tmp_path
):
    _, _, run_root, _ = _install_fake_luna_complete_requests(monkeypatch, tmp_path)
    rt.prepare_batch_replay(run_root)
    _patch_batch_environment(monkeypatch)
    first = _FakeBatchClient(create_error=TimeoutError("ambiguous"))

    with pytest.raises(rt.ChronologicalUncertainError, match="reissue is forbidden"):
        rt.submit_batch_replay(
            run_root,
            authorization=rt.BATCH_SUBMIT_AUTHORIZATION,
            client=first,
        )
    assert first.batches.create_calls == 1

    second = _FakeBatchClient()
    with pytest.raises(BenchmarkSafetyError, match="cannot be submitted"):
        rt.submit_batch_replay(
            run_root,
            authorization=rt.BATCH_SUBMIT_AUTHORIZATION,
            client=second,
        )
    assert second.batches.create_calls == 0


def test_batch_collect_maps_shuffled_output_by_custom_id_and_never_applies_state(
    monkeypatch, tmp_path
):
    _, _, run_root, run_plan = _install_fake_luna_complete_requests(monkeypatch, tmp_path)
    manifest = rt.prepare_batch_replay(run_root)
    manifest["status"] = "provider_completed"
    manifest["batch_id"] = "batch-1"
    manifest["provider_batch"] = {
        "output_file_id": "file-output",
    }
    rt.atomic_write_json(rt._batch_manifest_path(run_root), manifest)
    _patch_batch_environment(monkeypatch)

    rows = []
    for mapping in reversed(manifest["identity"]["requests"]):
        body = _fake_response(
            "gpt-5.6-luna",
            _fake_document(
                term=f"T{mapping['ordinal']}",
                english=f"E{mapping['ordinal']}",
            ),
        )
        rows.append(
            {
                "id": f"req-{mapping['ordinal']}",
                "custom_id": mapping["custom_id"],
                "response": {
                    "status_code": 200,
                    "request_id": f"r-{mapping['ordinal']}",
                    "body": body,
                },
                "error": None,
            }
        )
    client = _FakeBatchClient()
    client.files.content_payload = (
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    ).encode("utf-8")

    final = rt.collect_batch_replay(run_root, client=client)
    assert final["status"] == "complete"
    assert final["request_count"] == 50
    assert final["safety"]["batch_state_applied"] is False
    assert final["safety"]["inference_retries"] == 0
    result_files = list((rt._batch_dir(run_root) / "results").glob("*.json"))
    assert len(result_files) == 50
    first = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert first["batch_state_applied"] is False


def test_wrong_authorization_never_constructs_paid_client(monkeypatch, tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)
    constructed = {"count": 0}

    def factory(config):
        constructed["count"] += 1
        return _FakeSyncClient()

    with pytest.raises(rt.ChronologicalRuntimeError, match="authorization"):
        rt.execute_sync_next(
            run_root,
            arm=rt.FLASH_ARM,
            authorization="NO",
            client_factory=factory,
        )
    assert constructed["count"] == 0


def test_flash_and_luna_use_identical_messages_for_same_state(tmp_path):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    state = _initial_state("great_king_zhuxia", "zh")
    flash, _, _, _ = rt._build_request(
        canonical_root,
        canonical_plan,
        rt.FLASH_ARM,
        "great_king_zhuxia",
        1,
        state,
    )
    luna, _, _, _ = rt._build_request(
        canonical_root,
        canonical_plan,
        rt.LUNA_ARM,
        "great_king_zhuxia",
        1,
        state,
    )
    assert flash["messages"] == luna["messages"]
    assert flash["model"] == "deepseek-flash"
    assert luna["model"] == "gpt-5.6-luna"
    assert "extra_body" in flash
    assert "reasoning_effort" in luna
    assert "MATURE" not in flash["messages"][0]["content"]


def test_conservative_sync_budget_blocks_before_client_construction(
    monkeypatch, tmp_path
):
    canonical_root, canonical_plan = _canonical_fixture(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_root, run_plan = _runtime_plan(tmp_path, canonical_root, canonical_plan)
    run_plan["repo_root"] = str(repo)
    run_plan["budgets"][f"{rt.FLASH_ARM}_usd"] = 0.000001
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    _patch_sync_environment(monkeypatch, canonical_plan)
    constructed = {"count": 0}

    def factory(config):
        constructed["count"] += 1
        return _FakeSyncClient(
            response=_fake_response(rt.EXPECTED_MODEL_CONFIG[rt.FLASH_ARM]["model"])
        )

    with pytest.raises(BenchmarkSafetyError, match="ceiling would exceed budget"):
        rt.execute_sync_next(
            run_root,
            arm=rt.FLASH_ARM,
            authorization=rt.SYNC_EXECUTE_AUTHORIZATION[rt.FLASH_ARM],
            client_factory=factory,
        )
    assert constructed["count"] == 0
    assert not rt._case_dir(
        run_root, rt.FLASH_ARM, "great_king_zhuxia", 1
    ).exists()


def test_conservative_batch_budget_blocks_before_upload(monkeypatch, tmp_path):
    _, _, run_root, run_plan = _install_fake_luna_complete_requests(
        monkeypatch, tmp_path
    )
    run_plan["budgets"][f"{rt.BATCH_ARM}_usd"] = 0.000001
    (run_root / rt.PLAN_FILENAME).write_text(json.dumps(run_plan), encoding="utf-8")
    rt.prepare_batch_replay(run_root)
    _patch_batch_environment(monkeypatch)
    client = _FakeBatchClient()

    with pytest.raises(BenchmarkSafetyError, match="Batch cost ceiling exceeds"):
        rt.submit_batch_replay(
            run_root,
            authorization=rt.BATCH_SUBMIT_AUTHORIZATION,
            client=client,
        )
    assert client.files.upload_calls == 0
    assert client.batches.create_calls == 0


def test_batch_output_missing_custom_id_fails_closed(monkeypatch, tmp_path):
    _, _, run_root, _ = _install_fake_luna_complete_requests(monkeypatch, tmp_path)
    manifest = rt.prepare_batch_replay(run_root)
    manifest["status"] = "provider_completed"
    manifest["batch_id"] = "batch-1"
    manifest["provider_batch"] = {"output_file_id": "file-output"}
    rt.atomic_write_json(rt._batch_manifest_path(run_root), manifest)
    _patch_batch_environment(monkeypatch)

    mapping = manifest["identity"]["requests"][0]
    row = {
        "id": "batch_req_1",
        "custom_id": mapping["custom_id"],
        "response": {
            "status_code": 200,
            "request_id": "req-1",
            "body": _fake_response("gpt-5.6-luna"),
        },
        "error": None,
    }
    client = _FakeBatchClient()
    client.files.content_payload = (json.dumps(row) + "\n").encode("utf-8")

    with pytest.raises(rt.ChronologicalRuntimeError, match="missing 49 requests"):
        rt.collect_batch_replay(run_root, client=client)
