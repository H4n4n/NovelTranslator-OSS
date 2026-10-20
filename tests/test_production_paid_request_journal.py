import json
from pathlib import Path

import pytest
import noveltranslator_core as core
import production_paid_request_journal as journal

from production_failure_classification import LocalPersistenceError
from production_paid_request_journal import (
    PaidRequestJournalError,
    PaidRequestUncertainError,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
    load_matching_journal,
)


def _save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _identity(payload=None, *, model="deepseek-v4-pro"):
    return request_identity(
        operation_kind="chapter_translation",
        novel_id="fixture",
        chapter=1,
        provider="deepseek",
        model=model,
        profile_id="historical_military",
        source_sha256="1" * 64,
        request_payload=payload or {
            "model": model,
            "messages": [{"role": "user", "content": "source"}],
            "max_tokens": 100,
        },
    )


def _normalize(raw, attempt):
    return {
        "content": raw["content"],
        "finish_reason": raw.get("finish_reason", "stop"),
        "stats": raw.get("stats", {}),
        "pricing": dict(attempt),
    }


def _interrupt_response_persistence(monkeypatch, path, error, boundary):
    """Interrupt the real atomic writer, retaining the exact published bytes."""
    observed = {}
    replace = core.os.replace
    persist = journal._persist

    def capture():
        observed["bytes"] = path.read_bytes()

    if boundary == "after_persist":
        def interrupted_persist(target, record, *args, **kwargs):
            persist(target, record, *args, **kwargs)
            if Path(target).absolute() == path.absolute() and record["status"] == "response_recorded" and not observed:
                capture()
                raise error
        monkeypatch.setattr(journal, "_persist", interrupted_persist)
    else:
        def interrupted_replace(source, target):
            is_response = (Path(target).absolute() == path.absolute() and not observed
                and json.loads(Path(source).read_text(encoding="utf-8")).get("status") == "response_recorded")
            if is_response and boundary == "before_replace":
                capture()
                raise error
            result = replace(source, target)
            if is_response:
                capture()
                raise error
            return result
        monkeypatch.setattr(core.os, "replace", interrupted_replace)
    return observed


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("boundary", ["before_replace", "after_replace", "after_persist"])
@pytest.mark.parametrize("finish", ["stop", "content_filter", "length"])
def test_interrupt_after_response_replace_preserves_recorded_response(tmp_path, monkeypatch, error_type, boundary, finish):
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    error = error_type("paid response boundary")
    observed = _interrupt_response_persistence(monkeypatch, path, error, boundary)
    raw = {"content": "paid once" if finish == "stop" else "", "finish_reason": finish,
           "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
    sends = []
    def send():
        sends.append(1)
        return raw
    def run():
        return run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=core.atomic_write_json,
            prepare_attempt=lambda: {}, send_once=send,
            normalize_response=lambda raw, context: {"raw_response": raw, **context})
    with pytest.raises(error_type) as caught:
        run()
    assert caught.value is error
    saved = load_matching_journal(path, _identity())  # Proves identity, record and response hashes.
    if boundary == "before_replace":
        assert json.loads(observed["bytes"])["status"] == "started"
        assert saved["status"] == "uncertain" and saved["response"] == {} and saved["response_sha256"] == ""
        with pytest.raises(PaidRequestUncertainError):
            run()
    else:
        assert path.read_bytes() == observed["bytes"]
        assert saved["status"] == "response_recorded"
        assert saved["response"]["raw_response"] == raw
        replay = run_journaled_paid_request(journal_path=path, identity=_identity(),
            save_json=lambda *a: pytest.fail("write on replay"),
            prepare_attempt=lambda: pytest.fail("prepare on replay"),
            authorize_send=lambda: pytest.fail("authorization on replay"),
            send_once=lambda: pytest.fail("second physical send"),
            normalize_response=lambda *a: pytest.fail("normalization on replay"))
        assert replay["response"] == saved["response"] and replay["replayed"] and not replay["api_called"]
        assert path.read_bytes() == observed["bytes"]
    assert sends == [1]


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, ValueError])
@pytest.mark.parametrize("boundary", ["transport", "normalization"])
def test_interrupted_transport_or_normalization_remains_uncertain(tmp_path, error_type, boundary):
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    error = error_type("ambiguous paid attempt")
    sends = []
    def send():
        sends.append(1)
        if boundary == "transport":
            raise error
        return {"content": "paid response"}
    def normalize(*args):
        raise error
    with pytest.raises(error_type) as caught:
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=core.atomic_write_json,
            prepare_attempt=lambda: {}, send_once=send, normalize_response=normalize)
    assert caught.value is error
    record = load_matching_journal(path, _identity())
    assert record["status"] == "uncertain" and record["response"] == {} and record["response_sha256"] == ""
    assert record["error"] == {"type": error_type.__name__, "message": str(error)}
    with pytest.raises(PaidRequestUncertainError):
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=core.atomic_write_json,
            prepare_attempt=lambda: pytest.fail("prepare on uncertainty"), send_once=send, normalize_response=normalize)
    assert sends == [1]


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_uncertainty_write_failure_keeps_started_and_original_interruption(tmp_path, error_type):
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    original = error_type("transport interrupted")
    started = []
    def save(target, record):
        if record["status"] == "uncertain":
            raise OSError("uncertainty persistence unavailable")
        core.atomic_write_json(target, record)
        if record["status"] == "started":
            started.append(target.read_bytes())
    def send():
        raise original
    with pytest.raises(error_type) as caught:
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=save,
            prepare_attempt=lambda: {}, send_once=send, normalize_response=_normalize)
    assert caught.value is original and path.read_bytes() == started[0]
    assert load_matching_journal(path, _identity())["status"] == "started"
    with pytest.raises(PaidRequestUncertainError):
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=save,
            prepare_attempt=lambda: pytest.fail("prepare after STARTED"),
            send_once=lambda: pytest.fail("second send"), normalize_response=_normalize)


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("evidence", ["malformed", "record_hash", "response_hash", "identity", "unreadable", "directory"])
def test_ambiguous_persistence_never_replaces_unverified_evidence(tmp_path, monkeypatch, error_type, evidence):
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    error = error_type("original response persistence failure")
    observed, sends = {}, []
    read_text = Path.read_text
    def save(target, record):
        if record["status"] != "response_recorded":
            return core.atomic_write_json(target, record)
        broken = dict(record)
        if evidence == "malformed":
            target.write_bytes(b'{"partial":')
        elif evidence == "directory":
            target.unlink()
            target.mkdir()
        else:
            if evidence == "record_hash":
                broken["record_sha256"] = "0" * 64
            elif evidence == "response_hash":
                broken = journal._with_record_hash({**broken, "response_sha256": "0" * 64})
            elif evidence == "identity":
                broken = journal._updated_record(broken, request_sha256="0" * 64)
            core.atomic_write_json(target, broken)
        if evidence != "directory":
            observed["bytes"] = target.read_bytes()
        if evidence == "unreadable":
            def unreadable(self, *a, **k):
                if self == path:
                    raise PermissionError("synthetic unreadable journal")
                return read_text(self, *a, **k)
            monkeypatch.setattr(Path, "read_text", unreadable)
        raise error
    with pytest.raises(error_type) as caught:
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=save,
            prepare_attempt=lambda: {}, send_once=lambda: (sends.append(1) or {"content": "paid"}), normalize_response=_normalize)
    assert caught.value is error
    assert any("evidence retained" in note for note in error.__notes__)
    if evidence == "directory":
        assert path.is_dir() and not list(path.iterdir())
    else:
        assert path.read_bytes() == observed["bytes"]
    with pytest.raises(PaidRequestJournalError):
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=save,
            prepare_attempt=lambda: pytest.fail("prepare with invalid evidence"),
            send_once=lambda: pytest.fail("second physical send"), normalize_response=_normalize)
    assert sends == [1]


@pytest.mark.parametrize("status", ["started", "uncertain", "planned"])
def test_persist_refuses_recorded_response_downgrade(tmp_path, status):
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=core.atomic_write_json,
        prepare_attempt=lambda: {}, send_once=lambda: {"content": "paid"}, normalize_response=_normalize)
    before = path.read_bytes()
    recorded = load_matching_journal(path, _identity())
    weaker = journal._updated_record(recorded, status=status, response={}, response_sha256="")
    with pytest.raises(PaidRequestJournalError, match="transition blocked"):
        journal._persist(path, weaker, core.atomic_write_json, expected_identity=_identity())
    assert path.read_bytes() == before


def test_planned_is_durable_before_provider_attempt(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    provider_calls = []

    def prepare():
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == "planned"
        return {"request_started_utc": "2026-09-05T12:00:00+00:00"}

    def send():
        started = json.loads(path.read_text(encoding="utf-8"))
        assert started["status"] == "started"
        provider_calls.append(1)
        return {"content": "translated"}

    result = run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=prepare,
        send_once=send,
        normalize_response=_normalize,
    )

    assert provider_calls == [1]
    assert result["api_called"] is True
    assert result["replayed"] is False
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "response_recorded"
    assert saved["response"]["content"] == "translated"


def test_response_recorded_replays_without_provider(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=lambda: {"pricing": "off_peak"},
        send_once=lambda: {"content": "paid once"},
        normalize_response=_normalize,
    )

    result = run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=lambda: (_ for _ in ()).throw(
            AssertionError("prepare must not run on replay")
        ),
        send_once=lambda: (_ for _ in ()).throw(
            AssertionError("provider must not run on replay")
        ),
        normalize_response=_normalize,
    )

    assert result["response"]["content"] == "paid once"
    assert result["api_called"] is False
    assert result["replayed"] is True


def test_provider_exception_becomes_uncertain_and_blocks_restart(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    calls = []

    def fail():
        calls.append("provider")
        raise TimeoutError("ambiguous timeout")

    with pytest.raises(TimeoutError, match="ambiguous timeout"):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: {"pricing": "off_peak"},
            send_once=fail,
            normalize_response=_normalize,
        )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "uncertain"
    assert calls == ["provider"]

    with pytest.raises(
        PaidRequestUncertainError,
        match="automatic reissue is blocked",
    ):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: {},
            send_once=lambda: calls.append("second"),
            normalize_response=_normalize,
        )
    assert calls == ["provider"]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7)])
def test_base_exceptions_preserve_semantics_and_block_reissue(tmp_path, error):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )

    def raise_original():
        raise error

    with pytest.raises(type(error)):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: {},
            send_once=raise_original,
            normalize_response=_normalize,
        )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "uncertain"


def test_intent_persistence_failure_makes_zero_provider_calls(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    calls = []

    def fail_save(_path, _value):
        raise OSError("disk full")

    with pytest.raises(
        LocalPersistenceError,
        match="paid request journal persistence failed",
    ) as caught:
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=fail_save,
            prepare_attempt=lambda: {},
            send_once=lambda: calls.append("provider"),
            normalize_response=_normalize,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert "disk full" in str(caught.value.__cause__)
    assert calls == []


def test_response_persistence_failure_leaves_restart_fail_closed(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    calls = []

    def save(path_value, value):
        if value["status"] == "response_recorded":
            raise OSError("response journal unavailable")
        _save_json(path_value, value)

    def send():
        calls.append(1)
        return {"content": "remote success"}

    with pytest.raises(
        LocalPersistenceError,
        match="paid request journal persistence failed",
    ) as caught:
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=save,
            prepare_attempt=lambda: {},
            send_once=send,
            normalize_response=_normalize,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert "response journal unavailable" in str(caught.value.__cause__)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "uncertain"

    with pytest.raises(PaidRequestUncertainError):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: {},
            send_once=send,
            normalize_response=_normalize,
        )
    assert calls == [1]


def test_planned_record_can_resume_when_provider_never_started(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )

    with pytest.raises(RuntimeError, match="pause preparation failed"):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: (_ for _ in ()).throw(
                RuntimeError("pause preparation failed")
            ),
            send_once=lambda: (_ for _ in ()).throw(
                AssertionError("provider must not run")
            ),
            normalize_response=_normalize,
        )

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "planned"

    result = run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=lambda: {},
        send_once=lambda: {"content": "safe later"},
        normalize_response=_normalize,
    )
    assert result["response"]["content"] == "safe later"


@pytest.mark.parametrize(
    "changed",
    [
        _identity(model="deepseek-v4-flash"),
        _identity(payload={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "different prompt"}],
            "max_tokens": 100,
        }),
    ],
)
def test_existing_slot_rejects_request_identity_change(tmp_path, changed):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=lambda: {},
        send_once=lambda: {"content": "first"},
        normalize_response=_normalize,
    )

    with pytest.raises(PaidRequestJournalError, match="identity mismatch"):
        run_journaled_paid_request(
            journal_path=path,
            identity=changed,
            save_json=_save_json,
            prepare_attempt=lambda: {},
            send_once=lambda: {"content": "must not run"},
            normalize_response=_normalize,
        )


def test_corrupt_record_hash_fails_closed(tmp_path):
    path = paid_request_journal_path(
        tmp_path,
        "chapter_translation",
        1,
    )
    run_journaled_paid_request(
        journal_path=path,
        identity=_identity(),
        save_json=_save_json,
        prepare_attempt=lambda: {},
        send_once=lambda: {"content": "first"},
        normalize_response=_normalize,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["response"]["content"] = "tampered"
    path.write_text(json.dumps(saved), encoding="utf-8")

    with pytest.raises(PaidRequestJournalError, match="hash mismatch"):
        run_journaled_paid_request(
            journal_path=path,
            identity=_identity(),
            save_json=_save_json,
            prepare_attempt=lambda: {},
            send_once=lambda: {"content": "must not run"},
            normalize_response=_normalize,
        )


@pytest.mark.parametrize("failure", [RuntimeError("local refusal"), KeyboardInterrupt("local crash")])
def test_authorization_refusal_leaves_exact_planned_record_retryable(tmp_path, failure):
    from contextlib import contextmanager
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    planned_bytes, calls = [], []
    @contextmanager
    def refused():
        assert json.loads(path.read_text())["status"] == "planned"
        planned_bytes.append(path.read_bytes())
        raise failure
        yield  # An authorization scope must never send.
    with pytest.raises(type(failure)):
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=_save_json,
            prepare_attempt=lambda: {}, authorize_send=refused, send_once=lambda: calls.append(1), normalize_response=_normalize)
    assert path.read_bytes() == planned_bytes[0]
    assert not calls
    @contextmanager
    def permitted():
        assert json.loads(path.read_text())["status"] == "planned"
        def send():
            assert json.loads(path.read_text())["status"] == "started"
            calls.append(1)
            return {"content": "paid once"}
        yield send
    result = run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=_save_json,
        prepare_attempt=lambda: {}, authorize_send=permitted, send_once=lambda: pytest.fail("unbound send"), normalize_response=_normalize)
    assert result["api_called"] and calls == [1]
    recorded_bytes = path.read_bytes()
    replay = run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=_save_json,
        prepare_attempt=lambda: pytest.fail("prepare on replay"), authorize_send=lambda: pytest.fail("authorize on replay"),
        send_once=lambda: pytest.fail("send on replay"), normalize_response=_normalize)
    assert replay["replayed"] and not replay["api_called"]
    assert path.read_bytes() == recorded_bytes and calls == [1]


def test_authorization_scope_releases_after_started_persistence_failure(tmp_path):
    from contextlib import contextmanager
    path = paid_request_journal_path(tmp_path, "chapter_translation", 1)
    events = []
    @contextmanager
    def authorized():
        events.append("authorize")
        try:
            yield lambda: pytest.fail("transport cannot follow failed STARTED persistence")
        finally:
            events.append("release")
    def save(path, record):
        if record["status"] == "started":
            raise OSError("cannot persist STARTED")
        _save_json(path, record)
    with pytest.raises(LocalPersistenceError):
        run_journaled_paid_request(journal_path=path, identity=_identity(), save_json=save,
            prepare_attempt=lambda: {}, authorize_send=authorized, send_once=lambda: None, normalize_response=_normalize)
    assert events == ["authorize", "release"]
    assert json.loads(path.read_text())["status"] == "planned"
