import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g6_execution as g6
from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    source_case,
)
from production_paid_request_journal import PaidRequestUncertainError


def _prompt_bundles():
    result = {}
    for case in G6_CALIBRATION_CASES:
        key = (case.novel_id, case.chapter)
        if key in result:
            continue
        messages = (
            ("system", f"system:{case.novel_id}"),
            ("user", f"user:{case.novel_id}:{case.chapter}"),
        )
        message_dicts = [
            {"role": role, "content": content}
            for role, content in messages
        ]
        frozen_source = source_case(case.novel_id, case.chapter)
        result[key] = g6.PromptBundle(
            novel_id=case.novel_id,
            source_language=case.source_language,
            chapter=case.chapter,
            profile_id=(
                "infinite_flow"
                if case.source_language == "zh"
                else "korean_fantasy"
            ),
            source_sha256=frozen_source.source_sha256,
            chapter_library_sha256=frozen_source.chapter_library_sha256,
            text_sha256=case.text_sha256,
            messages=messages,
            system_prompt_sha256=g6._sha256_text(messages[0][1]),
            user_prompt_sha256=g6._sha256_text(messages[1][1]),
            messages_sha256=g6._sha256_json(message_dicts),
        )
    return result


def _fake_ok_sender(**kwargs):
    body = kwargs["request_body"]
    return {
        "http_status": 200,
        "body_json": {
            "id": "response-fixture",
            "model": body["model"],
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Translated fixture output."},
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        },
        "body_sha256": "3" * 64,
        "request_id": "request-fixture",
    }


def test_runtime_plan_has_exactly_forty_cases_and_live_reasoning_controls(tmp_path):
    prompts = _prompt_bundles()
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=tmp_path / "external",
        prompt_bundles=prompts,
    )

    assert len(runtime) == 40

    for item in runtime:
        effort = item.calibration_case.effort
        if effort is None:
            assert "reasoning_effort" not in item.request_body
        else:
            assert item.request_body["reasoning_effort"] == effort


def test_all_arms_use_identical_messages_for_same_source(tmp_path):
    prompts = _prompt_bundles()
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=tmp_path / "external",
        prompt_bundles=prompts,
    )

    hashes = {}
    for item in runtime:
        key = (
            item.calibration_case.novel_id,
            item.calibration_case.chapter,
        )
        hashes.setdefault(key, set()).add(item.prompt.messages_sha256)

    assert all(len(values) == 1 for values in hashes.values())
    assert len(hashes) == 4


def test_paired_arms_differ_only_by_reasoning_effort(tmp_path):
    prompts = _prompt_bundles()
    runtime = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=tmp_path / "external",
        prompt_bundles=prompts,
    )

    grouped = {}
    for item in runtime:
        key = (
            item.calibration_case.benchmark_model_id,
            item.calibration_case.novel_id,
            item.calibration_case.chapter,
        )
        grouped.setdefault(key, []).append(item)

    for rows in grouped.values():
        stripped = []
        for row in rows:
            body = dict(row.request_body)
            body.pop("reasoning_effort", None)
            stripped.append(g6._canonical_json(body))
        assert len(set(stripped)) == 1


def test_external_execution_root_refuses_production_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(g6.G6ExecutionPlanError, match="outside"):
        g6.assert_external_evidence_root(repo / "evidence", repo)


def test_normalizer_accepts_length_finish_as_literary_diagnostic_not_retry(tmp_path):
    prompts = _prompt_bundles()
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=tmp_path / "external",
        prompt_bundles=prompts,
    )[0]

    response = g6.normalize_translation_http_result(
        {
            "http_status": 200,
            "body_json": {
                "id": "r1",
                "model": item.calibration_case.provider_model_id,
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "Partial but durable output"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 12,
                    "total_tokens": 22,
                },
            },
            "body_sha256": "4" * 64,
            "request_id": "q1",
        },
        expected_model=item.calibration_case.provider_model_id,
    )
    evaluation = g6.evaluate_translation_response(
        response,
        runtime_case=item,
    )

    assert evaluation["provider_accepted"] is True
    assert evaluation["literary_diagnostics"]["finish_reason_length"] is True
    assert evaluation["literary_diagnostics"]["preserve_without_automatic_retry"] is True


def test_missing_usage_is_definitive_provider_contract_rejection(tmp_path):
    prompts = _prompt_bundles()
    item = g6.build_runtime_cases(
        repo_root=tmp_path,
        evidence_root=tmp_path / "external",
        prompt_bundles=prompts,
    )[0]
    response = {
        "http_status": 200,
        "response_model": item.calibration_case.provider_model_id,
        "normalization_errors": [],
        "content": "text",
        "usage": None,
        "finish_reason": "stop",
    }
    evaluation = g6.evaluate_translation_response(response, runtime_case=item)
    assert evaluation["provider_accepted"] is False
    assert "missing_usage" in evaluation["provider_rejection_reasons"]


def test_inspect_empty_state_requires_exactly_forty_new_calls(tmp_path):
    prompts = _prompt_bundles()
    state = g6.inspect_execution_state(
        repo_root=tmp_path,
        evidence_root=tmp_path / "evidence",
        prompt_bundles=prompts,
    )
    assert state["case_count"] == 40
    assert state["new_calls_required"] == 40


def test_journaled_suite_makes_exactly_forty_fake_calls_and_replays_zero(tmp_path, monkeypatch):
    prompts = _prompt_bundles()
    g6a = tmp_path / "g6a"
    g6a.mkdir()
    monkeypatch.setattr(
        g6,
        "verify_g6a_evidence",
        lambda _root: {
            "selection_semantic_sha256": "5" * 64,
            "files": {},
        },
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "outside" / "run"
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["request_body"]["model"])
        return _fake_ok_sender(**kwargs)

    first = g6.run_execution_suite(
        repo_root=repo,
        g6a_evidence_root=g6a,
        evidence_root=evidence,
        api_key="fixture-key",
        authorize_new_calls=40,
        sender=sender,
        prompt_bundles=prompts,
    )
    assert first["status"] == "provider_accepted_complete"
    assert first["api_calls_this_run"] == 40
    assert len(calls) == 40
    assert len(list((evidence / "journal").glob("*.json"))) == 40
    assert len(list((evidence / "results").glob("*.json"))) == 40

    second = g6.run_execution_suite(
        repo_root=repo,
        g6a_evidence_root=g6a,
        evidence_root=evidence,
        api_key="",
        authorize_new_calls=0,
        sender=sender,
        prompt_bundles=prompts,
    )
    assert second["api_calls_this_run"] == 0
    assert second["replayed_count_this_run"] == 40
    assert len(calls) == 40


def test_ambiguous_send_becomes_uncertain_and_blocks_reissue(tmp_path, monkeypatch):
    prompts = _prompt_bundles()
    monkeypatch.setattr(
        g6,
        "verify_g6a_evidence",
        lambda _root: {
            "selection_semantic_sha256": "5" * 64,
            "files": {},
        },
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "outside" / "run"

    def ambiguous(**_kwargs):
        raise TimeoutError("fixture timeout after possible remote processing")

    with pytest.raises(TimeoutError):
        g6.run_execution_suite(
            repo_root=repo,
            g6a_evidence_root=tmp_path,
            evidence_root=evidence,
            api_key="fixture-key",
            authorize_new_calls=40,
            sender=ambiguous,
            prompt_bundles=prompts,
        )

    journals = list((evidence / "journal").glob("*.json"))
    assert len(journals) == 1
    record = json.loads(journals[0].read_text(encoding="utf-8"))
    assert record["status"] == "uncertain"

    with pytest.raises(PaidRequestUncertainError):
        g6.inspect_execution_state(
            repo_root=repo,
            evidence_root=evidence,
            prompt_bundles=prompts,
        )


def test_definitive_http_rejection_is_recorded_and_not_reissued(tmp_path, monkeypatch):
    prompts = _prompt_bundles()
    monkeypatch.setattr(
        g6,
        "verify_g6a_evidence",
        lambda _root: {
            "selection_semantic_sha256": "5" * 64,
            "files": {},
        },
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "outside" / "run"
    calls = []

    def rejected(**kwargs):
        calls.append(1)
        return {
            "http_status": 400,
            "body_json": {
                "error": {
                    "type": "invalid_request_error",
                    "message": "fixture rejection",
                }
            },
            "body_sha256": "6" * 64,
            "request_id": "fixture",
        }

    with pytest.raises(g6.G6ExecutionRejectedError):
        g6.run_execution_suite(
            repo_root=repo,
            g6a_evidence_root=tmp_path,
            evidence_root=evidence,
            api_key="fixture-key",
            authorize_new_calls=40,
            sender=rejected,
            prompt_bundles=prompts,
        )

    assert len(calls) == 1
    journal = json.loads(
        next((evidence / "journal").glob("*.json")).read_text(encoding="utf-8")
    )
    assert journal["status"] == "response_recorded"

    with pytest.raises(g6.G6ExecutionRejectedError):
        g6.inspect_execution_state(
            repo_root=repo,
            evidence_root=evidence,
            prompt_bundles=prompts,
        )
    assert len(calls) == 1


def test_plan_identity_contains_no_source_prose_or_credentials(tmp_path):
    prompts = _prompt_bundles()
    plan = g6.execution_plan_identity(
        repo_root=tmp_path,
        evidence_root=tmp_path / "outside",
        prompt_bundles=prompts,
    )
    rendered = json.dumps(plan, ensure_ascii=False).lower()

    assert "user:ace_evolution" not in rendered
    assert "system:rogue_first_prince" not in rendered
    for forbidden in ("api_key", "authorization", "bearer", "fixture-key"):
        assert forbidden not in rendered


def test_dry_run_main_does_not_read_api_key_or_create_execution_root(
    tmp_path,
    monkeypatch,
    capsys,
):
    prompts = _prompt_bundles()
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "outside" / "run"

    monkeypatch.setattr(
        g6,
        "verify_g6a_evidence",
        lambda _root: {
            "selection_semantic_sha256": "5" * 64,
            "files": {},
        },
    )
    monkeypatch.setattr(g6, "load_all_prompt_bundles", lambda _repo: prompts)
    monkeypatch.setattr(
        g6,
        "read_execution_api_key",
        lambda: (_ for _ in ()).throw(AssertionError("credential read")),
    )

    assert g6.main(
        [
            "--repo-root",
            str(repo),
            "--g6a-evidence-root",
            str(tmp_path),
            "--evidence-root",
            str(evidence),
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "new_calls_required=40" in output
    assert "execution=disabled" in output
    assert not evidence.exists()
