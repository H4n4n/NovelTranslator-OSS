"""Provider retry decisions and their effect on the paid-request journal."""
import json

import httpx
import openai
import pytest

import provider_retry_policy as policy
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    load_matching_journal, release_ambiguous_requests, request_identity, resend_log_path, run_journaled_paid_request,
)

REQUEST = httpx.Request("POST", "https://example.test/chat")


def status_error(status, message="error"):
    response = httpx.Response(status, request=REQUEST)
    return openai.APIStatusError(message, response=response, body={"message": message})


def connect_error():
    error = openai.APIConnectionError(request=REQUEST)
    error.__cause__ = httpx.ConnectError("no route")
    return error


def test_the_openai_clients_own_http_library_counts_as_never_connected():
    httpx2 = pytest.importorskip("httpx2")
    error = openai.APIConnectionError(request=REQUEST)
    error.__cause__ = httpx2.ConnectError("no route")
    assert policy.classify(error) == policy.WAIT_FOR_NETWORK


def read_timeout():
    return openai.APITimeoutError(request=REQUEST)


class Script:
    """Fails with each scripted error in turn, then succeeds."""
    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "response"


def run(script, **kwargs):
    return policy.send_with_retries(script, sleep=lambda seconds: None, **kwargs)


@pytest.mark.parametrize("error,expected", [
    (status_error(429), policy.RETRY),
    (status_error(500), policy.RETRY),
    (status_error(502), policy.RETRY),
    (status_error(524), policy.RETRY),
    (status_error(429, "You've reached your 5-hour usage limit for your plan. Your limit resets at 3:00 PM."), policy.WAIT_FOR_LIMIT),
    (status_error(403, "You've reached your weekly usage limit"), policy.WAIT_FOR_LIMIT),
    (status_error(401), policy.STOP),
    (status_error(402), policy.STOP),
    (status_error(429, "insufficient credits"), policy.STOP),
    (status_error(403, "content_policy_refused"), policy.STOP),
    (status_error(400), policy.STOP),
    (connect_error(), policy.WAIT_FOR_NETWORK),
    (read_timeout(), policy.RESEND_ONCE),
    (ValueError("local bug"), None),
])
def test_classification(error, expected):
    assert policy.classify(error) == expected


def test_transient_errors_retry_until_success():
    script = Script(status_error(429), status_error(502))
    assert run(script) == "response"
    assert script.calls == 3


def test_transient_errors_eventually_stop_as_definite():
    script = Script(*[status_error(503) for _ in range(30)])
    with pytest.raises(policy.DefiniteProviderFailure) as caught:
        run(script)
    assert caught.value.reason == "provider_unavailable"
    assert script.calls == len(policy.BACKOFF_SECONDS) + 1


def test_usage_limit_waits_without_charging_attempts():
    retracted = []
    waits = []
    script = Script(status_error(429, "usage limit reached"), status_error(429, "usage limit reached"))
    result = policy.send_with_retries(script, sleep=waits.append, on_definite_failure=lambda: retracted.append(1))
    assert result == "response"
    assert waits == [policy.LIMIT_POLL_SECONDS] * 2
    assert len(retracted) == 2


def test_network_outage_waits_then_recovers():
    script = Script(connect_error(), connect_error())
    assert run(script) == "response"


def test_ambiguous_gets_exactly_one_resend():
    assert run(Script(read_timeout())) == "response"
    script = Script(read_timeout(), read_timeout())
    with pytest.raises(openai.APITimeoutError):
        run(script)
    assert script.calls == 2


def test_ambiguous_attempts_stay_counted():
    retracted = []
    run(Script(read_timeout()), on_definite_failure=lambda: retracted.append(1))
    assert retracted == []


def test_key_problem_stops_immediately():
    script = Script(status_error(401), "never reached")
    with pytest.raises(policy.DefiniteProviderFailure) as caught:
        run(script)
    assert caught.value.reason == "provider_key_or_credit"
    assert script.calls == 1


def test_unknown_error_propagates_unchanged():
    with pytest.raises(ValueError):
        run(Script(ValueError("bug")))


def test_interrupt_during_wait_keeps_slot_reusable():
    def interrupted(_seconds):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt) as caught:
        policy.send_with_retries(Script(status_error(500)), sleep=interrupted)
    assert caught.value.definite_no_response is True


def test_retry_log_holds_no_message_text():
    entries = []
    run(Script(status_error(429, "secret sk-abc echoed")), log=entries.append)
    assert entries[0]["status"] == 429 and "secret" not in json.dumps(entries)


def identity(chapter=5, kind="state_extraction"):
    return request_identity(operation_kind=kind, novel_id="n", chapter=chapter, provider="p", model="m",
                            profile_id="x", source_sha256="a" * 64, translation_sha256="", request_payload={"q": 1})


def journal_send(path, send, chapter=5):
    return run_journaled_paid_request(
        journal_path=path, identity=identity(chapter), save_json=atomic_write_json,
        prepare_attempt=lambda: {"request_started_utc": "2026-01-01T00:00:00+00:00"},
        send_once=send, normalize_response=lambda raw, context: {"content": raw, **context})


def test_definite_failure_leaves_slot_planned_and_resendable(tmp_path):
    path = tmp_path / "0005.state_extraction.json"

    def fail():
        raise policy.DefiniteProviderFailure("provider_unavailable")

    with pytest.raises(policy.DefiniteProviderFailure):
        journal_send(path, fail)
    assert load_matching_journal(path, identity())["status"] == "planned"
    assert journal_send(path, lambda: "ok")["api_called"] is True
    assert load_matching_journal(path, identity())["status"] == "response_recorded"


def make_uncertain(path, chapter=5):
    def fail():
        raise RuntimeError("cut off")
    with pytest.raises(RuntimeError):
        journal_send(path, fail, chapter)


def test_ambiguous_slot_released_once_then_blocked(tmp_path):
    path = tmp_path / "0005.state_extraction.json"
    make_uncertain(path)
    first = release_ambiguous_requests(tmp_path, next_chapter=5, save_json=atomic_write_json, max_resends=1)
    assert first == {"released": [path.name], "blocked": []}
    assert load_matching_journal(path, identity())["status"] == "planned"
    assert resend_log_path(tmp_path).is_file()

    make_uncertain(path)
    second = release_ambiguous_requests(tmp_path, next_chapter=5, save_json=atomic_write_json, max_resends=1)
    assert second == {"released": [], "blocked": [path.name]}
    assert load_matching_journal(path, identity())["status"] == "uncertain"


def test_committed_chapters_are_left_alone(tmp_path):
    path = tmp_path / "0005.state_extraction.json"
    make_uncertain(path)
    result = release_ambiguous_requests(tmp_path, next_chapter=6, save_json=atomic_write_json, max_resends=1)
    assert result == {"released": [], "blocked": []}
    assert load_matching_journal(path, identity())["status"] == "uncertain"
