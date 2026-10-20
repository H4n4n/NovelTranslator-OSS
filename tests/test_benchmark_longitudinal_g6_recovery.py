import json

import pytest

import benchmark_longitudinal_g6_execution as g6
import benchmark_longitudinal_g6_recovery as recovery
from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    source_case,
)
from production_paid_request_journal import run_journaled_paid_request


def _prompt_bundles():
    result = {}
    for case in G6_CALIBRATION_CASES:
        key = (case.novel_id, case.chapter)
        if key in result:
            continue
        source = source_case(case.novel_id, case.chapter)
        messages = (
            ("system", f"system:{case.novel_id}"),
            ("user", f"user:{case.novel_id}:{case.chapter}"),
        )
        message_dicts = [
            {"role": role, "content": content}
            for role, content in messages
        ]
        result[key] = g6.PromptBundle(
            novel_id=case.novel_id,
            source_language=case.source_language,
            chapter=case.chapter,
            profile_id=(
                "infinite_flow"
                if case.source_language == "zh"
                else "korean_fantasy"
            ),
            source_sha256=source.source_sha256,
            chapter_library_sha256=source.chapter_library_sha256,
            text_sha256=case.text_sha256,
            messages=messages,
            system_prompt_sha256=g6._sha256_text(messages[0][1]),
            user_prompt_sha256=g6._sha256_text(messages[1][1]),
            messages_sha256=g6._sha256_json(message_dicts),
        )
    return result


def _raw_ok(item):
    return {
        "http_status": 200,
        "body_json": {
            "id": f"fixture-{item.index}",
            "model": item.calibration_case.provider_model_id,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": f"fixture translation {item.index}"
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        },
        "body_sha256": "a" * 64,
        "request_id": f"fixture-{item.index}",
    }


def _raw_524():
    return {
        "http_status": 524,
        "body_json": {
            "error": {
                "message": "fixture gateway timeout",
            }
        },
        "body_sha256": "b" * 64,
        "request_id": "fixture-524",
    }


def _prepare_initial_35(tmp_path):
    prompts = _prompt_bundles()
    execution = tmp_path / "execution"
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    (execution / "results").mkdir(parents=True, exist_ok=True)

    for item in runtime[:35]:
        raw = _raw_524() if item.index == 35 else _raw_ok(item)

        def prepare(item=item):
            return {"fixture": item.index}

        def send(raw=raw):
            return raw

        def normalize(value, _attempt, item=item):
            return g6.normalize_translation_http_result(
                value,
                expected_model=item.calibration_case.provider_model_id,
            )

        journal_result = run_journaled_paid_request(
            journal_path=item.journal_path,
            identity=item.identity,
            save_json=g6._atomic_write_json,
            prepare_attempt=prepare,
            send_once=send,
            normalize_response=normalize,
        )
        response = journal_result["response"]
        evaluation = g6.evaluate_translation_response(
            response,
            runtime_case=item,
        )
        record = {
            "schema_version": 1,
            "execution_revision": g6.EXECUTION_REVISION,
            "g6_calibration_freeze_sha256": (
                recovery.G6_CALIBRATION_FREEZE_SHA256
            ),
            "g6_blind_protocol_sha256": recovery.G6_BLIND_PROTOCOL_SHA256,
            "case": item.plan_identity(),
            "response": response,
            "evaluation": evaluation,
        }
        g6._atomic_write_json(
            execution / "results" / f"{item.index:04d}.json",
            record,
        )

    return execution, prompts


def test_frozen_recovery_suffix_is_exactly_five_cases():
    assert recovery.PENDING_CASE_IDS == (
        "G6C036",
        "G6C037",
        "G6C038",
        "G6C039",
        "G6C040",
    )
    assert recovery.PENDING_CASE_INDICES == (36, 37, 38, 39, 40)
    assert recovery.RECOVERY_NEW_CALL_COUNT == 5
    assert recovery.PRESERVED_REJECTED_CASE_ID == "G6C035"
    assert recovery.PRESERVED_REJECTED_HTTP_STATUS == 524


def test_inspection_preserves_case35_and_finds_only_five_absent(tmp_path):
    execution, prompts = _prepare_initial_35(tmp_path)
    state = recovery.inspect_recovery_state(
        repo_root=tmp_path,
        execution_root=execution,
        prompt_bundles=prompts,
    )
    assert state["case_count"] == 40
    assert state["accepted_count"] == 34
    assert state["preserved_rejected_count"] == 1
    assert state["pending_count"] == 5
    assert state["pending_case_ids"] == list(recovery.PENDING_CASE_IDS)


def test_case35_is_never_a_pending_recovery_send(tmp_path):
    execution, prompts = _prepare_initial_35(tmp_path)
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    pending = [
        item.calibration_case.case_id
        for item in runtime
        if not item.journal_path.exists()
    ]
    assert pending == list(recovery.PENDING_CASE_IDS)
    assert recovery.PRESERVED_REJECTED_CASE_ID not in pending


def test_ambiguous_pending_case_blocks_recovery_state(tmp_path):
    execution, prompts = _prepare_initial_35(tmp_path)
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )[35]

    with pytest.raises(TimeoutError):
        run_journaled_paid_request(
            journal_path=item.journal_path,
            identity=item.identity,
            save_json=g6._atomic_write_json,
            prepare_attempt=lambda: {"fixture": True},
            send_once=lambda: (_ for _ in ()).throw(TimeoutError("fixture")),
            normalize_response=lambda raw, attempt: raw,
        )

    with pytest.raises(Exception):
        recovery.inspect_recovery_state(
            repo_root=tmp_path,
            execution_root=execution,
            prompt_bundles=prompts,
        )


def test_additional_definitive_rejection_blocks_recovery_state(tmp_path):
    execution, prompts = _prepare_initial_35(tmp_path)
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )[35]

    journal_result = run_journaled_paid_request(
        journal_path=item.journal_path,
        identity=item.identity,
        save_json=g6._atomic_write_json,
        prepare_attempt=lambda: {"fixture": True},
        send_once=_raw_524,
        normalize_response=lambda raw, _attempt: g6.normalize_translation_http_result(
            raw,
            expected_model=item.calibration_case.provider_model_id,
        ),
    )
    response = journal_result["response"]
    evaluation = g6.evaluate_translation_response(
        response,
        runtime_case=item,
    )
    g6._atomic_write_json(
        execution / "results" / "0036.json",
        {
            "schema_version": 1,
            "execution_revision": g6.EXECUTION_REVISION,
            "g6_calibration_freeze_sha256": recovery.G6_CALIBRATION_FREEZE_SHA256,
            "g6_blind_protocol_sha256": recovery.G6_BLIND_PROTOCOL_SHA256,
            "case": item.plan_identity(),
            "response": response,
            "evaluation": evaluation,
        },
    )

    with pytest.raises(recovery.G6RecoveryNewRejectionError):
        recovery.inspect_recovery_state(
            repo_root=tmp_path,
            execution_root=execution,
            prompt_bundles=prompts,
        )


def test_recovery_suite_sends_only_five_absent_and_never_case35(
    tmp_path,
    monkeypatch,
):
    execution, prompts = _prepare_initial_35(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    reconciliation = tmp_path / "reconciliation"
    reconciliation.mkdir()

    monkeypatch.setattr(
        recovery,
        "verify_reconciliation_evidence",
        lambda _root, _execution: {
            "classification": "DEFINITIVE_PROVIDER_REJECTION_DO_NOT_RETRY",
            "files": {},
        },
    )

    synthetic_plan = g6.execution_plan_identity(
        repo_root=repo,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    monkeypatch.setattr(
        recovery,
        "EXPECTED_EXECUTION_PLAN_SHA256",
        g6._sha256_json(synthetic_plan),
    )

    calls = []

    def sender(**kwargs):
        model = kwargs["request_body"]["model"]
        calls.append(model)
        return {
            "http_status": 200,
            "body_json": {
                "id": f"fixture-recovery-{len(calls)}",
                "model": model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "recovered fixture translation"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
            "body_sha256": "c" * 64,
            "request_id": f"recovery-{len(calls)}",
        }

    control = tmp_path / "control"
    summary = recovery.run_recovery_suite(
        repo_root=repo,
        execution_root=execution,
        reconciliation_root=reconciliation,
        recovery_control_root=control,
        api_key="fixture-key",
        authorize_new_calls=5,
        sender=sender,
        prompt_bundles=prompts,
    )

    assert summary["status"] == "recovery_complete_with_preserved_terminal_rejection"
    assert summary["new_calls_this_run"] == 5
    assert summary["accepted_count"] == 39
    assert summary["preserved_terminal_rejection_count"] == 1
    assert summary["pending_count"] == 0
    assert len(calls) == 5
    assert len(list((execution / "journal").glob("*.json"))) == 40
    assert len(list((execution / "results").glob("*.json"))) == 40

    original35 = json.loads(
        (execution / "journal" / "0035.goat_g6_translation.json").read_text(
            encoding="utf-8"
        )
    )
    assert original35["status"] == "response_recorded"
    assert original35["response"]["http_status"] == 524


def test_wrong_recovery_authorization_count_fails_before_send(
    tmp_path,
    monkeypatch,
):
    execution, prompts = _prepare_initial_35(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    reconciliation = tmp_path / "reconciliation"
    reconciliation.mkdir()

    monkeypatch.setattr(
        recovery,
        "verify_reconciliation_evidence",
        lambda _root, _execution: {
            "classification": "DEFINITIVE_PROVIDER_REJECTION_DO_NOT_RETRY",
            "files": {},
        },
    )

    with pytest.raises(g6.G6ExecutionAuthorizationError):
        recovery.run_recovery_suite(
            repo_root=repo,
            execution_root=execution,
            reconciliation_root=reconciliation,
            recovery_control_root=tmp_path / "control",
            api_key="fixture-key",
            authorize_new_calls=4,
            sender=lambda **kwargs: _raw_ok(
                g6.build_runtime_cases(
                    repo_root=tmp_path,
                    evidence_root=execution,
                    prompt_bundles=prompts,
                )[35]
            ),
            prompt_bundles=prompts,
        )


def test_recovery_control_root_must_be_external_to_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(g6.G6ExecutionPlanError):
        g6.assert_external_evidence_root(repo / "recovery", repo)
