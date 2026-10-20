import pytest

import benchmark_longitudinal_g6_execution as g6
import benchmark_longitudinal_g6_mimo_recovery as recovery2
from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    source_case,
)
from benchmark_longitudinal_g6_recovery import (
    G6_BLIND_PROTOCOL_SHA256,
    G6_CALIBRATION_FREEZE_SHA256,
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
                    "message": {"content": f"fixture translation {item.index}"},
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
        "body_json": {"error": {"message": "fixture gateway timeout"}},
        "body_sha256": "b" * 64,
        "request_id": "fixture-524",
    }


def _write_case(execution, item, raw):
    journal_result = run_journaled_paid_request(
        journal_path=item.journal_path,
        identity=item.identity,
        save_json=g6._atomic_write_json,
        prepare_attempt=lambda: {"fixture": item.index},
        send_once=lambda: raw,
        normalize_response=lambda value, _attempt: (
            g6.normalize_translation_http_result(
                value,
                expected_model=item.calibration_case.provider_model_id,
            )
        ),
    )
    response = journal_result["response"]
    evaluation = g6.evaluate_translation_response(
        response,
        runtime_case=item,
    )
    (execution / "results").mkdir(parents=True, exist_ok=True)
    g6._atomic_write_json(
        execution / "results" / f"{item.index:04d}.json",
        {
            "schema_version": 1,
            "execution_revision": g6.EXECUTION_REVISION,
            "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
            "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
            "case": item.plan_identity(),
            "response": response,
            "evaluation": evaluation,
        },
    )


def _prepare_initial_36(tmp_path):
    prompts = _prompt_bundles()
    execution = tmp_path / "execution"
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    for item in runtime[:36]:
        _write_case(
            execution,
            item,
            _raw_524() if item.index in {35, 36} else _raw_ok(item),
        )
    return execution, prompts


def test_second_recovery_freeze_is_exactly_four_mimo_cases():
    assert recovery2.PENDING_CASE_IDS == (
        "G6C037",
        "G6C038",
        "G6C039",
        "G6C040",
    )
    assert recovery2.PENDING_CASE_INDICES == (37, 38, 39, 40)
    assert recovery2.SECOND_RECOVERY_NEW_CALL_COUNT == 4
    assert recovery2.PENDING_MODEL_ID == "xiaomi/mimo-v2.5-pro"
    assert recovery2.PENDING_CASE_SPECS == (
        ("G6C037", 37, "ace_evolution", 18),
        ("G6C038", 38, "ace_evolution", 2),
        ("G6C039", 39, "rogue_first_prince", 25),
        ("G6C040", 40, "rogue_first_prince", 14),
    )
    assert [item["case_id"] for item in recovery2.TERMINAL_REJECTIONS] == [
        "G6C035",
        "G6C036",
    ]


def test_inspection_preserves_two_rejections_and_finds_four_absent(tmp_path):
    execution, prompts = _prepare_initial_36(tmp_path)
    state = recovery2.inspect_second_recovery_state(
        repo_root=tmp_path,
        execution_root=execution,
        prompt_bundles=prompts,
    )
    assert state["case_count"] == 40
    assert state["accepted_count"] == 34
    assert state["terminal_rejected_count"] == 2
    assert state["terminal_rejected_case_ids"] == ["G6C035", "G6C036"]
    assert state["pending_count"] == 4
    assert state["pending_case_ids"] == list(recovery2.PENDING_CASE_IDS)


def test_terminal_rejections_never_become_pending(tmp_path):
    execution, prompts = _prepare_initial_36(tmp_path)
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
    assert pending == list(recovery2.PENDING_CASE_IDS)
    assert "G6C035" not in pending
    assert "G6C036" not in pending


def test_ambiguous_mimo_case_blocks_second_recovery(tmp_path):
    execution, prompts = _prepare_initial_36(tmp_path)
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )[36]

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
        recovery2.inspect_second_recovery_state(
            repo_root=tmp_path,
            execution_root=execution,
            prompt_bundles=prompts,
        )


def test_new_definitive_mimo_rejection_blocks_future_state(tmp_path):
    execution, prompts = _prepare_initial_36(tmp_path)
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=execution,
        prompt_bundles=prompts,
    )[36]
    _write_case(execution, item, _raw_524())

    with pytest.raises(recovery2.G6SecondRecoveryNewRejectionError):
        recovery2.inspect_second_recovery_state(
            repo_root=tmp_path,
            execution_root=execution,
            prompt_bundles=prompts,
        )


def test_second_recovery_sends_only_four_mimo_cases(
    tmp_path,
    monkeypatch,
):
    execution, prompts = _prepare_initial_36(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    reconciliation = tmp_path / "r2"
    reconciliation.mkdir()
    first_control = tmp_path / "first-control"
    first_control.mkdir()

    monkeypatch.setattr(
        recovery2,
        "verify_r2_evidence",
        lambda *_args, **_kwargs: {
            "classification": "TWO_DEFINITIVE_PROVIDER_REJECTIONS_FOUR_ABSENT",
            "files": {},
        },
    )
    synthetic_plan = g6.execution_plan_identity(
        repo_root=repo,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    monkeypatch.setattr(
        recovery2,
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
                "id": f"fixture-mimo-{len(calls)}",
                "model": model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "fixture MiMo translation"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
            "body_sha256": "c" * 64,
            "request_id": f"mimo-{len(calls)}",
        }

    control = tmp_path / "second-control"
    summary = recovery2.run_second_recovery_suite(
        repo_root=repo,
        execution_root=execution,
        reconciliation_root=reconciliation,
        first_recovery_control_root=first_control,
        second_recovery_control_root=control,
        api_key="fixture-key",
        authorize_new_calls=4,
        sender=sender,
        prompt_bundles=prompts,
    )

    assert summary["status"] == (
        "second_recovery_complete_with_two_terminal_rejections"
    )
    assert summary["new_calls_this_run"] == 4
    assert summary["accepted_count"] == 38
    assert summary["terminal_rejection_count"] == 2
    assert summary["terminal_rejection_case_ids"] == ["G6C035", "G6C036"]
    assert summary["pending_count"] == 0
    assert summary["completed_new_case_ids"] == list(recovery2.PENDING_CASE_IDS)
    assert len(calls) == 4
    assert len(list((execution / "journal").glob("*.json"))) == 40
    assert len(list((execution / "results").glob("*.json"))) == 40


def test_wrong_second_recovery_authorization_fails_before_send(
    tmp_path,
    monkeypatch,
):
    execution, prompts = _prepare_initial_36(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        recovery2,
        "verify_r2_evidence",
        lambda *_args, **_kwargs: {
            "classification": "TWO_DEFINITIVE_PROVIDER_REJECTIONS_FOUR_ABSENT",
            "files": {},
        },
    )

    with pytest.raises(g6.G6ExecutionAuthorizationError):
        recovery2.run_second_recovery_suite(
            repo_root=repo,
            execution_root=execution,
            reconciliation_root=tmp_path / "r2",
            first_recovery_control_root=tmp_path / "first-control",
            second_recovery_control_root=tmp_path / "second-control",
            api_key="fixture-key",
            authorize_new_calls=3,
            sender=lambda **kwargs: {},
            prompt_bundles=prompts,
        )


def test_new_rejection_stops_without_attempting_later_mimo_cases(
    tmp_path,
    monkeypatch,
):
    execution, prompts = _prepare_initial_36(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        recovery2,
        "verify_r2_evidence",
        lambda *_args, **_kwargs: {
            "classification": "TWO_DEFINITIVE_PROVIDER_REJECTIONS_FOUR_ABSENT",
            "files": {},
        },
    )
    synthetic_plan = g6.execution_plan_identity(
        repo_root=repo,
        evidence_root=execution,
        prompt_bundles=prompts,
    )
    monkeypatch.setattr(
        recovery2,
        "EXPECTED_EXECUTION_PLAN_SHA256",
        g6._sha256_json(synthetic_plan),
    )

    calls = []

    def sender(**kwargs):
        calls.append(kwargs["request_body"]["model"])
        return _raw_524()

    control = tmp_path / "second-control"
    with pytest.raises(recovery2.G6SecondRecoveryNewRejectionError):
        recovery2.run_second_recovery_suite(
            repo_root=repo,
            execution_root=execution,
            reconciliation_root=tmp_path / "r2",
            first_recovery_control_root=tmp_path / "first-control",
            second_recovery_control_root=control,
            api_key="fixture-key",
            authorize_new_calls=4,
            sender=sender,
            prompt_bundles=prompts,
        )

    assert len(calls) == 1
    summary = recovery2._load_json_object(
        control / "second_recovery_summary.json",
        "fixture second recovery summary",
    )
    assert summary["failed_case_id"] == "G6C037"
    assert summary["new_calls_this_run"] == 1
    assert summary["completed_new_case_ids"] == []


def test_second_recovery_control_root_must_be_external_to_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(g6.G6ExecutionPlanError):
        g6.assert_external_evidence_root(repo / "second-recovery", repo)
