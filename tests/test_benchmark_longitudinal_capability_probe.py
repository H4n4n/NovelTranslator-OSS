import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

import benchmark_longitudinal_capability_probe as probe
from benchmark_longitudinal_capabilities import G5C_CAPABILITY_PROBE_CASES
from production_paid_request_journal import PaidRequestUncertainError


def _success_raw(model, *, prompt_tokens=7, completion_tokens=1):
    body = {
        "id": "resp-test",
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "OK"},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    raw_bytes = json.dumps(body).encode("utf-8")
    return {
        "http_status": 200,
        "body_text": raw_bytes.decode("utf-8"),
        "body_json": body,
        "body_sha256": probe.hashlib.sha256(raw_bytes).hexdigest(),
        "request_id": "req-test",
    }


def _sender_success(**kwargs):
    return _success_raw(kwargs["request_body"]["model"])


def test_probe_requests_match_frozen_ten_case_matrix():
    bodies = [probe.build_probe_request(case) for case in G5C_CAPABILITY_PROBE_CASES]
    assert len(bodies) == 10

    for case, body in zip(G5C_CAPABILITY_PROBE_CASES, bodies, strict=True):
        assert body["model"] == case.provider_model_id
        assert body["max_completion_tokens"] == probe.PROBE_MAX_COMPLETION_TOKENS
        assert body["messages"] == [{"role": "user", "content": probe.PROBE_PROMPT}]
        assert "x-cmd-zdr" not in json.dumps(body).lower()
        if case.effort is None:
            assert probe.REASONING_WIRE_FIELD not in body
        else:
            assert body[probe.REASONING_WIRE_FIELD] == case.effort


def test_qwen_request_uses_catalog_case_not_logical_case(tmp_path):
    cases = probe.build_probe_runtime_cases(tmp_path)
    qwen = [
        item for item in cases
        if item.case.benchmark_model_id == "qwen/qwen3.8-max-0902"
    ]
    assert len(qwen) == 2
    assert {item.request_body["model"] for item in qwen} == {
        "Qwen/Qwen3.8-Max-0902"
    }


def test_plan_identity_is_root_path_independent(tmp_path):
    first = probe.probe_plan_identity(tmp_path / "one")
    second = probe.probe_plan_identity(tmp_path / "two")
    assert first == second
    assert probe._sha256_json(first) == probe._sha256_json(second)
    assert first["case_count"] == 10
    assert first["zdr_enabled"] is False


def test_normalizer_records_definitive_http_error_without_raising():
    raw = {
        "http_status": 400,
        "body_text": '{"error":{"type":"invalid_request_error","code":"bad","message":"nope"}}',
        "body_json": {
            "error": {
                "type": "invalid_request_error",
                "code": "bad",
                "message": "nope",
            }
        },
        "body_sha256": "a" * 64,
        "request_id": "req-400",
    }
    result = probe.normalize_probe_http_result(raw, expected_model="model-a")
    assert result["http_status"] == 400
    assert result["error"]["code"] == "bad"
    assert result["normalization_errors"] == []


def test_acceptance_requires_200_exact_model_and_complete_usage(tmp_path):
    runtime = probe.build_probe_runtime_cases(tmp_path)[0]
    good = probe.normalize_probe_http_result(
        _success_raw(runtime.case.provider_model_id),
        expected_model=runtime.case.provider_model_id,
    )
    assert probe.evaluate_probe_response(good, runtime_case=runtime)["accepted"] is True

    mismatch = dict(good, response_model="other-model")
    evaluation = probe.evaluate_probe_response(mismatch, runtime_case=runtime)
    assert evaluation["accepted"] is False
    assert "provider_model_identity_mismatch" in evaluation["reasons"]

    missing_usage = dict(good, usage=None)
    evaluation = probe.evaluate_probe_response(missing_usage, runtime_case=runtime)
    assert evaluation["accepted"] is False
    assert "missing_usage" in evaluation["reasons"]


def test_full_fake_suite_makes_exactly_ten_calls_and_persists_no_secret(tmp_path):
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["request_body"]["model"])
        return _success_raw(kwargs["request_body"]["model"])

    secret = "super-secret-command-code-key"
    summary = probe.run_probe_suite(
        evidence_root=tmp_path,
        api_key=secret,
        authorize_new_calls=10,
        sender=sender,
    )

    assert summary["status"] == "accepted_complete"
    assert summary["api_calls_this_run"] == 10
    assert summary["accepted_count"] == 10
    assert len(calls) == 10

    journals = sorted((tmp_path / "journal").glob("*.json"))
    results = sorted((tmp_path / "results").glob("*.json"))
    assert len(journals) == 10
    assert len(results) == 10
    assert json.loads((tmp_path / "probe_manifest.json").read_text())[
        "credentials_persisted"
    ] is False

    all_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert secret not in all_text


def test_completed_suite_replays_with_zero_new_calls(tmp_path):
    probe.run_probe_suite(
        evidence_root=tmp_path,
        api_key="secret-first-run",
        authorize_new_calls=10,
        sender=_sender_success,
    )

    def forbidden_sender(**_kwargs):
        raise AssertionError("replay must make zero provider calls")

    summary = probe.run_probe_suite(
        evidence_root=tmp_path,
        api_key="",
        authorize_new_calls=0,
        sender=forbidden_sender,
    )
    assert summary["api_calls_this_run"] == 0
    assert summary["replayed_count"] == 10


def test_wrong_authorized_call_count_fails_before_sender(tmp_path):
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return _sender_success(**kwargs)

    with pytest.raises(
        probe.CapabilityProbeAuthorizationError,
        match="exact remaining G5C send count",
    ):
        probe.run_probe_suite(
            evidence_root=tmp_path,
            api_key="secret",
            authorize_new_calls=9,
            sender=sender,
        )
    assert calls == []


def test_definitive_rejection_is_recorded_and_stops_later_calls(tmp_path):
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["request_body"]["model"])
        if len(calls) == 1:
            raw = json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                        "message": "reasoning_effort unsupported",
                    }
                }
            ).encode("utf-8")
            return {
                "http_status": 400,
                "body_text": raw.decode("utf-8"),
                "body_json": json.loads(raw),
                "body_sha256": probe.hashlib.sha256(raw).hexdigest(),
                "request_id": "req-reject",
            }
        return _success_raw(kwargs["request_body"]["model"])

    with pytest.raises(probe.CapabilityProbeRejectedError):
        probe.run_probe_suite(
            evidence_root=tmp_path,
            api_key="secret",
            authorize_new_calls=10,
            sender=sender,
        )

    assert len(calls) == 1
    record = json.loads(
        next((tmp_path / "journal").glob("*.json")).read_text(encoding="utf-8")
    )
    assert record["status"] == "response_recorded"
    summary = json.loads((tmp_path / "probe_summary.json").read_text())
    assert summary["status"] == "stopped_on_definitive_rejection"


def test_transport_exception_becomes_uncertain_and_blocks_automatic_reissue(tmp_path):
    def fail_sender(**_kwargs):
        raise TimeoutError("ambiguous transport timeout")

    with pytest.raises(TimeoutError, match="ambiguous transport timeout"):
        probe.run_probe_suite(
            evidence_root=tmp_path,
            api_key="secret",
            authorize_new_calls=10,
            sender=fail_sender,
        )

    first_journal = sorted((tmp_path / "journal").glob("*.json"))[0]
    assert json.loads(first_journal.read_text())["status"] == "uncertain"

    with pytest.raises(PaidRequestUncertainError):
        probe.inspect_probe_state(tmp_path)


def test_cli_without_execute_reads_no_credential(monkeypatch, tmp_path, capsys):
    def forbidden_credential_read():
        raise AssertionError("dry-run must not read credential environment")

    monkeypatch.setattr(probe, "read_probe_api_key", forbidden_credential_read)
    assert probe.main(["--evidence-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "execution=disabled" in output
    assert "new_calls_required=10" in output


def test_execute_path_reads_credential_through_single_runtime_helper(
    monkeypatch, tmp_path
):
    calls = []

    def fake_read():
        calls.append("credential")
        return "runtime-secret"

    def fake_run(**kwargs):
        assert kwargs["api_key"] == "runtime-secret"
        assert kwargs["authorize_new_calls"] == 10
        return {
            "status": "accepted_complete",
            "api_calls_this_run": 10,
            "accepted_count": 10,
        }

    monkeypatch.setattr(probe, "read_probe_api_key", fake_read)
    monkeypatch.setattr(probe, "run_probe_suite", fake_run)

    assert (
        probe.main(
            [
                "--evidence-root",
                str(tmp_path),
                "--execute",
                "--authorize-new-calls",
                "10",
                "--confirm-probe-revision",
                probe.G5C_PROBE_REVISION,
            ]
        )
        == 0
    )
    assert calls == ["credential"]


def test_http_sender_captures_http_error_as_definitive_response():
    body = b'{"error":{"type":"invalid_request_error","message":"bad"}}'

    def opener(_request, timeout):
        assert timeout == probe.PROBE_TIMEOUT_SECONDS
        raise urllib.error.HTTPError(
            probe.PROVIDER_CHAT_URL,
            400,
            "Bad Request",
            {},
            BytesIO(body),
        )

    result = probe.send_probe_http_once(
        api_key="secret",
        request_body={
            "model": "model-a",
            "messages": [{"role": "user", "content": "x"}],
        },
        opener=opener,
    )
    assert result["http_status"] == 400
    assert result["body_json"]["error"]["type"] == "invalid_request_error"
