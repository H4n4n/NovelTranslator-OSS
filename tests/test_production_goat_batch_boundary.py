"""Partial B2B2: disposable identity/lock/refusal proofs, never paid authority.

Test observers may allow the existing fake journal transport. The exported
BatchNoSendContext never does, including when all local receipts are valid.
"""
import copy
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
from concurrent.futures import ThreadPoolExecutor

import pytest
import noveltranslator_core as core
import production_goat_batch_boundary as boundary
import production_goat_batch_ledger as ledger
import production_paid_request_journal as journal
import production_run_lock as locks
from production_goat_runtime import install_goat_runtime, GoatBudget, raw_response_record
from controlled_trial_execute import _capture_core_runtime, _restore_core_runtime
from test_goat_batch_plan import fixture_novel, plan
from test_production_goat_runtime import kwargs, response, zero_network, engine
from test_production_paid_request_journal import _identity
from test_production_goat_batch_journal import batch as recorded_batch, operation, publish, rewrite


pytestmark = pytest.mark.usefixtures("zero_network", "synthetic_goat_hard_bound")
START = "2026-10-16T02:00:00+00:00"


def journal_call(path, *, observer=None, send=None, authorize=None, save=None):
    return journal.run_journaled_paid_request(journal_path=path, identity=_identity(),
        save_json=save or core.atomic_write_json, prepare_attempt=lambda: {"request_started_utc": START},
        send_once=send or (lambda: {"content": "SYNTHETIC"}),
        authorize_send=authorize, observe_request=observer,
        normalize_response=lambda raw, context: dict(raw_response=raw, **context))


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != locks.LOCK_NAME}


@pytest.fixture
def batch_engine(tmp_path, monkeypatch, request):
    from run_production_engine import prepare_production_runtime, load_production_module
    language, profile = getattr(request, "param", ("zh", "historical_military"))
    project = fixture_novel(tmp_path, language=language, profile=profile)
    state = _capture_core_runtime()
    monkeypatch.chdir(project.root)
    sends, clocks = [], []
    def send(**wire):
        sends.append(wire)
        return response()
    def clock():
        clocks.append(1)
        return datetime.fromisoformat(START)
    client = NS(chat=NS(completions=NS(create=send)))
    try:
        with locks.production_run_lock(project.root):
            prepare_production_runtime("v41-flash", project.pending_dir,
                provider_selection="command_code_provider")
            module = load_production_module()
            monkeypatch.setattr(module, "load_dotenv", lambda: pytest.fail("dotenv forbidden"))
            monkeypatch.setattr(module, "OpenAI", lambda *a, **k: pytest.fail("client construction forbidden"))
            module.initialize_production_runtime_state(activate_provider=False, provider_backend="command_code_provider")
            binding = plan(project)["authorization_binding"]
            obj = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b2", binding)
            context = boundary.BatchNoSendContext.from_ledger_locked(obj)
            budget = GoatBudget(5, 3)
            install_goat_runtime(module, provider_client=client, budget=budget,
                clock=clock, batch_context=context)
            yield project, module, obj, context, sends, clocks, budget
    finally:
        _restore_core_runtime(state)


def test_observer_exact_frozen_snapshots_legacy_scope_and_single_send(tmp_path):
    path = tmp_path / "0001.chapter_translation.json"
    observed, sends, scopes = [], [], []
    def observe(event):
        observed.append(event)
        assert event.journal_path == str(path.absolute())
        assert event.identity == _identity()
        assert event.attempt_context == {"request_started_utc": START}
        event.identity["chapter"] = 99
        event.attempt_context["request_started_utc"] = "tampered"
        event.record["status"] = "tampered"
        with pytest.raises(FrozenInstanceError):
            event.phase = "tampered"
        assert event.identity["chapter"] == 1
        assert event.record["status"] in {"planned", "response_recorded"}
    def send():
        assert journal.load_matching_journal(path, _identity())["status"] == "started"
        sends.append(1)
        return {"content": "SYNTHETIC"}
    @contextmanager
    def authorize():
        assert observed[-1].phase == "before_started"
        assert journal.load_matching_journal(path, _identity())["status"] == "planned"
        scopes.append("enter")
        yield send
        scopes.append("exit")
    result = journal_call(path, observer=observe, send=send, authorize=authorize)
    assert result["api_called"] and sends == [1] and scopes == ["enter", "exit"]
    assert [e.phase for e in observed] == ["before_started", "response_recorded"]
    before = path.read_bytes()
    replay = journal_call(path, observer=observe, send=lambda: pytest.fail("duplicate send"),
        authorize=lambda: pytest.fail("authorization on replay"))
    assert replay["replayed"] and not replay["api_called"]
    assert observed[-1].phase == "replay" and path.read_bytes() == before


def test_observer_return_value_cannot_bypass_legacy_authorization(tmp_path):
    path = tmp_path / "request.json"
    observed = []
    def observe(event):
        observed.append(event.phase)
        return {"send_authorized": True, "paid_execution_authorized": True}
    @contextmanager
    def refuse():
        raise RuntimeError("existing budget/recovery refusal")
        yield  # pragma: no cover
    with pytest.raises(RuntimeError, match="existing budget"):
        journal_call(path, observer=observe, authorize=refuse,
            send=lambda: pytest.fail("observer bypassed authorization"))
    assert observed == ["before_started"]
    assert journal.load_matching_journal(path, _identity())["status"] == "planned"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["before_started", "response_recorded", "replay"])
def test_observer_exception_retains_evidence_and_never_reissues(tmp_path, error_type, phase):
    path = tmp_path / "request.json"
    sends, authorizations = [], []
    error = error_type("SYNTHETIC observer crash")
    def send():
        sends.append(1)
        return {"content": "paid once SYNTHETIC"}
    @contextmanager
    def authorize():
        authorizations.append(1)
        yield send
    if phase == "replay":
        journal_call(path, send=send, authorize=authorize)
    def observe(event):
        if event.phase == phase:
            raise error
    with pytest.raises(error_type) as caught:
        journal_call(path, observer=observe, send=send, authorize=authorize)
    assert caught.value is error
    disk = journal.load_matching_journal(path, _identity())
    if phase == "before_started":
        assert disk["status"] == "planned" and not sends and not authorizations
    else:
        assert disk["status"] == "response_recorded" and sends == authorizations == [1]
        retained = path.read_bytes()
        assert journal_call(path, send=lambda: pytest.fail("second send"))["replayed"]
        assert path.read_bytes() == retained


@pytest.mark.parametrize("boundary_name", ["before_started_replace", "after_started_replace", "transport",
    "raw_before_replace", "raw_after_replace", "post_response"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_fake_journal_crashes_keep_single_physical_callback(tmp_path, boundary_name, error_type):
    path = tmp_path / "request.json"
    sends = []
    error = error_type("SYNTHETIC crash")
    def save(target, record):
        if (boundary_name == "before_started_replace" and record["status"] == "started"
                or boundary_name == "raw_before_replace" and record["status"] == "response_recorded"):
            raise error
        core.atomic_write_json(target, record)
        if (boundary_name == "after_started_replace" and record["status"] == "started"
                or boundary_name == "raw_after_replace" and record["status"] == "response_recorded"):
            raise error
    def send():
        sends.append(1)
        if boundary_name == "transport":
            raise error
        return {"content": "SYNTHETIC"}
    def observe(event):
        if boundary_name == "post_response" and event.phase == "response_recorded":
            raise error
    with pytest.raises(error_type):
        journal_call(path, observer=observe, save=save, send=send)
    disk = journal.load_matching_journal(path, _identity())
    assert len(sends) == (0 if "started_replace" in boundary_name else 1)
    assert disk["status"] == {
        "before_started_replace": "planned", "after_started_replace": "started",
        "transport": "uncertain", "raw_before_replace": "uncertain",
        "raw_after_replace": "response_recorded", "post_response": "response_recorded",
    }[boundary_name]
    retained = path.read_bytes()
    def refuse(event):
        raise boundary.BatchBoundaryBlocked("NO_SEND even for surviving PLANNED")
    with pytest.raises((boundary.BatchBoundaryBlocked, journal.PaidRequestUncertainError)):
        journal_call(path, observer=refuse, send=lambda: pytest.fail("restart send"))
    assert path.read_bytes() == retained


@pytest.mark.parametrize("batch_engine", [("zh", "historical_military"), ("ko", "korean_fantasy")], indirect=True)
def test_actual_runtime_parent_wire_utc_lock_and_unconditional_no_send(batch_engine, monkeypatch):
    project, module, obj, context, sends, clocks, budget = batch_engine
    events = []
    inspect = boundary.BatchNoSendContext.inspect_observation_locked
    def capture(self, event):
        report = inspect(self, event)
        events.append((event, report))
        return report
    monkeypatch.setattr(boundary.BatchNoSendContext, "inspect_observation_locked", capture)
    ledger_before = snapshot(obj.path)
    source = module.chapter_map[1]["text"]
    with pytest.raises(boundary.BatchBoundaryBlocked, match=boundary.STATUS):
        module.journaled_chat_request("chapter_translation", 1, source, kwargs())
    assert sends == [] and clocks == [1] and budget.api_calls == 0
    assert snapshot(obj.path) == ledger_before
    event, report = events[0]
    assert event.authorization_binding == obj.binding
    assert event.original_fingerprint == obj.identity["original_fingerprint"]
    assert event.batch_id == obj.identity["batch_id"]
    assert event.wire_request["max_completion_tokens"] == 50000
    assert journal._sha256_json(event.wire_request) == event.journal.identity["wire_request_sha256"]
    assert event.lock_lease is locks.held_production_run_lock(project.root)
    assert report["request_started_utc"] == START and not report["send_authorized"]
    assert not report["paid_execution_authorized"] and not report["replay_consumption_authorized"]
    assert journal.load_matching_journal(event.journal.journal_path, event.journal.identity)["status"] == "planned"
    assert not (project.data_dir / "goat_batch_evidence").exists()
    assert obj.snapshot_locked()["physical_slots"] == 0


@pytest.mark.parametrize("field", ["wire", "model", "policy", "operation", "chapter", "source", "domain",
    "path", "parent", "batch", "lease", "profile", "attempt_fields", "clock"])
def test_identity_drift_refuses_without_ledger_mutation(batch_engine, monkeypatch, field):
    project, module, obj, context, sends, _, _ = batch_engine
    events = []
    original = boundary.BatchNoSendContext.inspect_observation_locked
    def capture(self, event):
        events.append(event)
        return original(self, event)
    monkeypatch.setattr(boundary.BatchNoSendContext, "inspect_observation_locked", capture)
    with pytest.raises(boundary.BatchBoundaryBlocked):
        module.journaled_chat_request("chapter_translation", 1, module.chapter_map[1]["text"], kwargs())
    event = events[0]
    identity, attempt = event.journal.identity, event.journal.attempt_context
    if field == "wire":
        wire = event.wire_request
        wire["messages"][0]["content"] = "changed"
        event = replace(event, wire_json=json.dumps(wire))
    if field == "model": identity["model"] = "wrong"
    if field == "policy": identity["provider_policy"]["reasoning_effort"] = "high"
    if field == "operation": identity["operation_id"] = "f" * 64
    if field == "chapter": identity["chapter"] = 6
    if field == "source": identity["source_sha256"] = "f" * 64
    if field == "domain": identity["auxiliary_domain_binding"] = {"kind": "forged"}
    if field == "profile": identity["profile_id"] = "wrong"
    if field == "path": event = replace(event, journal=replace(event.journal, journal_path=str(project.root / "wrong.json")))
    if field == "parent": event = replace(event, original_fingerprint="0" * 64)
    if field == "batch": event = replace(event, batch_id="fresh-approval-does-not-renew")
    if field == "lease": event = replace(event, lock_lease=str(project.root / locks.LOCK_NAME))
    if field == "attempt_fields": attempt["other"] = 1
    if field == "clock": attempt["request_started_utc"] = "2026-10-16T02:00:00-05:00"
    event = replace(event, journal=replace(event.journal, identity_json=json.dumps(identity),
        attempt_context_json=json.dumps(attempt)))
    before = snapshot(project.root)
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        context.inspect_observation_locked(event)
    assert snapshot(project.root) == before and sends == []


def test_runtime_replay_without_reservation_does_not_escape_hook(batch_engine):
    _, module, obj, context, sends, clocks, budget = batch_engine
    source = module.chapter_map[1]["text"]
    binding = module._chat_request_identity("chapter_translation", 1, source, kwargs())
    path = journal.paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, "chapter_translation", 1)
    record = journal._updated_record(journal._new_planned_record(binding), status="response_recorded",
        attempt_context={"request_started_utc": START},
        response={"raw_response": raw_response_record(response()), "request_started_utc": START})
    record = journal._updated_record(record, response_sha256=journal._sha256_json(record["response"]))
    core.atomic_write_json(path, record)
    retained = path.read_bytes()
    with pytest.raises(boundary.BatchBoundaryBlocked, match="INDEPENDENT_MONOTONIC"):
        module.journaled_chat_request("chapter_translation", 1, source, kwargs())
    assert sends == clocks == [] and budget.api_calls == 0
    assert path.read_bytes() == retained and obj.snapshot_locked()["physical_slots"] == 0
    assert not hasattr(context, "authorize_send") and not hasattr(context, "settle_locked")


def test_legacy_runtime_preserves_budget_once_and_response_enrichment(engine):
    _, module, sends, _, budget, *_ = engine
    first = module.journaled_chat_request("chapter_translation", 1, "甲", kwargs())
    second = module.journaled_chat_request("chapter_translation", 1, "甲", kwargs())
    assert first["api_called"] and second["replayed"] and first["response"] == second["response"]
    assert len(sends) == budget.api_calls == 1


def test_lock_lease_is_actual_not_path_copy_expired_or_other_thread(tmp_path):
    with pytest.raises(RuntimeError):
        locks.held_production_run_lock(tmp_path)
    with locks.production_run_lock(tmp_path) as path:
        assert path == tmp_path / locks.LOCK_NAME
        lease = locks.held_production_run_lock(tmp_path)
        assert locks.verify_held_production_lock(lease, tmp_path) is lease
        for forged in (str(path), path, copy.copy(lease)):
            with pytest.raises(RuntimeError):
                locks.verify_held_production_lock(forged, tmp_path)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(locks.verify_held_production_lock, lease, tmp_path)
            with pytest.raises(RuntimeError):
                future.result()
    assert path.exists() and path.stat().st_size == 0
    with pytest.raises(RuntimeError):
        locks.verify_held_production_lock(lease, tmp_path)
    with locks.production_run_lock(tmp_path):
        assert locks.held_production_run_lock(tmp_path) is not lease


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_lock_lease_revoked_on_exception(tmp_path, error_type):
    lease = None
    with pytest.raises(error_type):
        with locks.production_run_lock(tmp_path):
            lease = locks.held_production_run_lock(tmp_path)
            raise error_type()
    with pytest.raises(RuntimeError):
        locks.verify_held_production_lock(lease, tmp_path)


def test_missing_old_ledger_never_bootstraps_or_creates_anchor(batch_engine):
    project, _, obj, context, sends, _, _ = batch_engine
    # Attacker simulation in disposable fixture: never repair or recreate it.
    obj.path.rename(obj.path.with_name("retained-original"))
    before = snapshot(project.root)
    with pytest.raises(ValueError, match="missing ledger"):
        context._open_locked()
    assert snapshot(project.root) == before and sends == []


@pytest.mark.parametrize("kind", ["state_extraction_uncertain_no_record_recovery", "state_extraction_terminal_recovery",
    "state_extraction_auxiliary_policy_migration_v1", "chapter_translation_cap_recovery_v2",
    "chapter_glossary_review_reasoning_empty_recovery", "state_extraction_recovery"])
def test_special_identities_cannot_enter_partial_boundary(recorded_batch, kind):
    project, obj, _ = recorded_batch
    context = boundary.BatchNoSendContext.from_ledger_locked(obj)
    req, wire, _ = operation(recorded_batch)
    path = publish(req, status="planned")
    identity = dict(req["journal_identity"], operation_kind=kind)
    event = journal.JournalObservation("before_started",
        str(journal.paid_request_journal_path(path.parent, kind, 1)), json.dumps(identity),
        json.dumps({"request_started_utc": START}), path.read_text(encoding="utf-8"))
    before = snapshot(project.root)
    with pytest.raises(ValueError, match="special operation"):
        context.observe_locked(event, wire)
    assert snapshot(project.root) == before


@pytest.mark.parametrize("enrich,rollback", [(False, False), (True, False), (True, True)])
def test_settled_response_and_consistent_suffix_rollback_still_no_send(recorded_batch, enrich, rollback):
    from production_goat_cap_recovery import accepted_envelope
    project, obj, adapter = recorded_batch
    context = boundary.BatchNoSendContext.from_ledger_locked(obj)
    req, wire, _ = operation(recorded_batch)
    path = publish(req)
    obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
    if enrich:
        rewrite(path, lambda r: r.update(response=accepted_envelope(r["response"], wire)))
    if rollback:
        # Disposable adversarial deletion demonstrates B2A's admitted limit:
        # a self-consistent chain is NOT an independent expected-head anchor.
        for item in obj.path.iterdir():
            if item.stem != "00000000": item.unlink()
        fresh = ledger.BatchLedger.open_locked(project.root, obj.identity["batch_id"], obj.binding)
        assert fresh.snapshot_locked()["physical_slots"] == 0
    record = json.loads(path.read_bytes())
    event = journal.JournalObservation("replay", str(path), json.dumps(req["journal_identity"]),
        json.dumps(record["attempt_context"]), json.dumps(record))
    before = snapshot(project.root)
    with pytest.raises(boundary.BatchBoundaryBlocked, match="INDEPENDENT_MONOTONIC"):
        context.observe_locked(event, wire)
    assert snapshot(project.root) == before


def test_lock_lease_rejects_junction_alias_and_traversal(tmp_path):
    root = tmp_path / "actual"
    root.mkdir()
    alias = tmp_path / "alias"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(root), str(alias))
    else:
        alias.symlink_to(root, target_is_directory=True)
    with locks.production_run_lock(root):
        lease = locks.held_production_run_lock(root)
        with pytest.raises(RuntimeError, match="symlinks/reparse"):
            locks.verify_held_production_lock(lease, alias)
        with pytest.raises(RuntimeError, match="non-traversing"):
            locks.verify_held_production_lock(lease, root / ".." / "actual")


@pytest.mark.parametrize("fake_context", ["approved", True, {"send_authorized": True}, NS(batch_id="guessed")])
def test_runtime_rejects_duck_typed_batch_authorization(fake_context):
    from production_goat_runtime import GoatRuntimeError
    module = NS(_RUNTIME_INITIALIZED=True)
    with pytest.raises(GoatRuntimeError, match="exact B2B2"):
        install_goat_runtime(module, provider_client=None, batch_context=fake_context)
    assert vars(module) == {"_RUNTIME_INITIALIZED": True}


def test_fresh_process_original_context_and_settled_replay_stays_blocked(tmp_path):
    from production_goat_batch_journal import BatchJournalEvidence
    project = fixture_novel(tmp_path)
    binding = plan(project)["authorization_binding"]
    with locks.production_run_lock(project.root):
        obj = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b", binding)
        context = boundary.BatchNoSendContext.from_ledger_locked(obj)
        adapter = BatchJournalEvidence(obj)
        req, wire, _ = operation((project, obj, adapter))
        path = publish(req)
        obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
        proof = dict(batch_id=context.batch_id, original_binding_json=context.original_binding_json,
            original_identity_json=context.original_identity_json, request=req, wire=wire)
    proof_path = tmp_path / "original-parent-proof.json"
    proof_path.write_bytes(ledger.canonical(proof))
    child = r'''
import json, sys
from pathlib import Path
from production_goat_batch_boundary import BatchNoSendContext, BatchBoundaryBlocked, STATUS
from production_run_lock import production_run_lock
from production_paid_request_journal import JournalObservation
p = json.loads(Path(sys.argv[1]).read_bytes())
c = BatchNoSendContext(p['batch_id'], p['original_binding_json'], p['original_identity_json'])
with production_run_lock(c.novel_root):
    path = Path(p['request']['journal_path'])
    r = json.loads(path.read_bytes())
    event = JournalObservation('replay', str(path), json.dumps(p['request']['journal_identity']),
        json.dumps(r['attempt_context']), json.dumps(r))
    try: c.observe_locked(event, p['wire'])
    except BatchBoundaryBlocked: print(STATUS)
    else: raise AssertionError('fresh-process batch replay escaped')
'''
    before = snapshot(project.root)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, "-B", "-c", child, str(proof_path)],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == boundary.STATUS and snapshot(project.root) == before


@pytest.mark.parametrize("phase,expected_sends", [("before_started", 0), ("response_recorded", 1)])
def test_hard_exit_observer_and_fresh_process_refusal(tmp_path, phase, expected_sends):
    path = tmp_path / "request.json"
    child = r'''
import os, sys
from pathlib import Path
import production_paid_request_journal as j
import noveltranslator_core as core
from test_production_paid_request_journal import _identity
root = Path(sys.argv[1])
def observe(event):
    if event.phase == sys.argv[2]: os._exit(71)
def send():
    (root/'physical-send').write_text('one SYNTHETIC send')
    return {'content': 'SYNTHETIC'}
j.run_journaled_paid_request(journal_path=root/'request.json', identity=_identity(),
    save_json=core.atomic_write_json, prepare_attempt=lambda: {'request_started_utc': '2026-10-16T02:00:00+00:00'},
    send_once=send, normalize_response=lambda raw, context: dict(raw_response=raw, **context), observe_request=observe)
'''
    env = dict(os.environ)
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repo) + os.pathsep + str(repo / "tests")
    completed = subprocess.run([sys.executable, "-B", "-c", child, str(tmp_path), phase],
        env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 71, completed.stderr
    assert (tmp_path / "physical-send").exists() == bool(expected_sends)
    retained = path.read_bytes()
    def refuse(event):
        raise boundary.BatchBoundaryBlocked(boundary.STATUS)
    with pytest.raises(boundary.BatchBoundaryBlocked):
        journal_call(path, observer=refuse, send=lambda: pytest.fail("restart send"))
    assert path.read_bytes() == retained


def test_fresh_import_inert_even_with_guessed_flags_and_tokens(tmp_path):
    script = r'''
import sys
def guard(event, args):
    if event == 'socket.connect': raise AssertionError('network')
    if event == 'import' and args[0].split('.')[0] in {'openai','dotenv','command_code_provider_credentials'}:
        raise AssertionError('paid/credential import')
    if event == 'open' and isinstance(args[0], str) and (args[0].endswith('.dpapi') or args[0].endswith('.env')):
        raise AssertionError('secret read')
sys.addaudithook(guard)
import production_goat_batch_boundary as b
assert b.STATUS == 'B2B2_PARTIAL_NO_SEND'
assert not hasattr(b, 'main')
print('INERT_NO_SEND')
'''
    env = dict(os.environ, NOVELTRANSLATOR_BATCH_ENABLED="1", NOVELTRANSLATOR_BATCH_TOKEN="guessed",
        NOVELTRANSLATOR_BATCH_ID="new-parent", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", script, "--authorize-paid-batch"],
        capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "INERT_NO_SEND"
