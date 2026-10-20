"""B2B1 real journal/ledger fixtures only; no B2B send hook is implemented."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager

import pytest
import noveltranslator_core as core
import production_goat_batch_journal as bridge
import production_goat_batch_ledger as ledger
import production_paid_request_journal as journal
from production_goat_runtime import normalize_production_request, raw_response_record
from production_goat_cap_recovery import accepted_envelope
from production_run_lock import production_run_lock
from test_goat_batch_plan import fixture_novel, plan
from test_production_goat_runtime import kwargs, response


START = "2026-10-16T02:00:00+00:00"
pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


@pytest.fixture
def batch(tmp_path):
    project = fixture_novel(tmp_path)
    binding = plan(project, max_api_calls=5, max_credit_equivalent_usd=3)["authorization_binding"]
    with production_run_lock(project.root):
        obj = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b", binding)
        yield project, obj, bridge.BatchJournalEvidence(obj)


def operation(batch, kind="chapter_translation", *, chapter=1, started=START, reserve=True):
    project, obj, adapter = batch
    logical = "translation" if kind == "chapter_translation" else (
        "periodic_audit" if "audit" in kind else "state_extraction")
    wire = normalize_production_request(logical, kwargs(12000 if logical == "translation" else 4000))
    if kind == "context_audit":
        kind = "context_periodic_audit_" + journal._sha256_json(wire)
    policy = obj.binding["provider_identity"]
    source = json.loads(project.chapters_file.read_text(encoding="utf-8"))[chapter - 1]["text"]
    identity = journal.request_identity(operation_kind=kind, novel_id=project.config.id, chapter=chapter,
        provider=policy["provider"], model=policy["wire_model"], profile_id=project.config.profile,
        source_sha256=__import__("hashlib").sha256(source.encode()).hexdigest(),
        request_payload={"wire_request": wire, "provider_policy": policy})
    identity.update(provider_policy=policy, wire_request_sha256=journal._sha256_json(wire))
    path = journal.paid_request_journal_path(project.data_dir / "provider_request_journal", kind, chapter)
    evidence = bridge.exposure_evidence(batch_id=obj.identity["batch_id"], journal_path=path,
        identity=identity, wire=wire, request_started_utc=started)
    request = dict(batch_id=obj.identity["batch_id"], operation_id=identity["operation_id"], chapter=chapter,
        journal_path=str(path), journal_identity=identity, wire_request_sha256=identity["wire_request_sha256"],
        provider_policy_sha256=ledger.digest(policy), exposure=dict(unit=ledger.UNIT,
            nano_usd=evidence["nano_usd"], evidence_sha256=ledger.digest(evidence)))
    adapter.context_dir.mkdir(parents=True, exist_ok=True)
    (adapter.context_dir / (identity["operation_id"] + ".json")).write_bytes(ledger.canonical(evidence))
    if reserve:
        obj.reserve_locked(request)
    return request, wire, evidence


def publish(request, *, status="response_recorded", raw=None, enrich=False):
    path = Path(request["journal_path"])
    identity = request["journal_identity"]
    record = journal._new_planned_record(identity)
    if status != "planned":
        record = journal._updated_record(record, status="started", attempt_context={"request_started_utc": START})
    if status == "response_recorded":
        envelope = dict(raw_response=raw if raw is not None else raw_response_record(response()),
                        request_started_utc=START)
        if enrich:
            wire = normalize_production_request("translation", kwargs())
            envelope = accepted_envelope(envelope, wire)
        record = journal._updated_record(record, status=status, response=envelope,
                                        response_sha256=journal._sha256_json(envelope))
    elif status == "uncertain":
        record = journal._updated_record(record, status=status, error={"type": "Crash", "message": "fixture"})
    path.parent.mkdir(parents=True, exist_ok=True)
    core.atomic_write_json(path, record)
    return path


def rewrite(path, mutate):
    value = json.loads(path.read_bytes())
    mutate(value)
    if value["status"] == "response_recorded":
        value["response_sha256"] = journal._sha256_json(value["response"])
    core.atomic_write_json(path, journal._with_record_hash(value))


def bytes_under(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != ".production.lock"}


def test_real_journal_raw_enriched_receipt_and_once_settlement(batch):
    project, obj, adapter = batch
    req, wire, _ = operation(batch)
    path = publish(req)
    raw = adapter.validate_recorded_response_locked(path, req)
    # 17 fresh * .30 + 3 cached * .006 + 10 output * 1.20, in nano-USD
    assert raw["actual"]["nano_usd"] == 17118
    obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
    rewrite(path, lambda r: r.update(response=accepted_envelope(r["response"], wire)))
    enriched = adapter.validate_recorded_response_locked(path, req)
    assert enriched == raw
    before = bytes_under(project.root)
    restarted = ledger.BatchLedger.open_locked(project.root, "synthetic-b2b", obj.binding)
    fresh = bridge.BatchJournalEvidence(restarted)
    restarted.settle_locked(req["operation_id"], validate_recorded_response=fresh.validate_recorded_response_locked)
    assert restarted.snapshot_locked()["credit_committed_nano_usd"] == 17118
    assert bytes_under(project.root) == before
    report = fresh.inspect_locked()
    assert report["receipts"][req["operation_id"]] == raw
    assert not report["send_authorized"] and not report["paid_execution_authorized"]


@pytest.mark.parametrize("kind", ["chapter_translation", "state_extraction", "state_json_repair", "context_audit"])
def test_real_journal_runner_order_and_replay_no_provider(batch, kind):
    _, obj, adapter = batch
    req, wire, _ = operation(batch, kind)
    path = Path(req["journal_path"])
    sends, statuses = [], []
    @contextmanager
    def authorize():
        # Test choreography ONLY: no exported production B2B authorization API.
        assert journal.load_matching_journal(path, req["journal_identity"])["status"] == "planned"
        assert obj.snapshot_locked()["physical_slots"] == 1
        obj.note_attempt_locked(req["operation_id"])
        yield send
    def send():
        assert journal.load_matching_journal(path, req["journal_identity"])["status"] == "started"
        sends.append(copy.deepcopy(wire))
        return response()
    def save(target, record):
        statuses.append(record["status"])
        core.atomic_write_json(target, record)
    def run():
        return journal.run_journaled_paid_request(journal_path=path, identity=req["journal_identity"],
            save_json=save, prepare_attempt=lambda: {"request_started_utc": START}, send_once=send,
            authorize_send=authorize, normalize_response=lambda raw, attempt: {"raw_response": raw_response_record(raw), **attempt})
    run()
    obj.reconcile_locked(req["operation_id"], validate_observation=adapter.validate_observation_locked,
                         validate_recorded_response=adapter.validate_recorded_response_locked)
    assert run()["replayed"] and len(sends) == 1
    assert statuses == ["planned", "started", "response_recorded"]


@pytest.mark.parametrize("status", ["ABSENT", "planned", "started", "uncertain", "response_recorded"])
def test_inventory_and_real_reconciliation_windows(batch, status):
    _, obj, adapter = batch
    req, _, _ = operation(batch)
    if status != "ABSENT":
        publish(req, status=status)
    report = adapter.inspect_locked()
    assert not report["send_authorized"]
    assert report["observations"][req["operation_id"]]["status"] == status.upper()
    result = obj.reconcile_locked(req["operation_id"], validate_observation=adapter.validate_observation_locked,
                                  validate_recorded_response=adapter.validate_recorded_response_locked)
    state = obj.snapshot_locked()
    assert state["physical_slots"] == 1
    if status == "response_recorded":
        assert result == "SETTLED_NO_SEND" and state["credit_committed_nano_usd"] == 17118
    else:
        assert state["credit_committed_nano_usd"] == req["exposure"]["nano_usd"]
        assert result == ("BLOCKED_AMBIGUOUS" if status in {"started", "uncertain"} else "LEDGER_AHEAD_NO_SEND")


@pytest.mark.parametrize("bad", ["missing_usage", "cached", "total", "reasoning", "model", "id", "noninteger",
    "boolean", "missing_prompt", "unknown_usage", "shape", "content", "cost_zero", "timestamp", "enriched_content"])
def test_rehashed_raw_and_envelope_attacks_rejected_preserving_proof(batch, bad):
    _, _, adapter = batch
    req, wire, _ = operation(batch)
    path = publish(req, enrich=bad in {"cost_zero", "enriched_content"})
    def corrupt(record):
        envelope = record["response"]
        raw = envelope["raw_response"]
        if bad == "missing_usage": raw["usage"] = None
        if bad == "cached": raw["usage"]["prompt_tokens_details"]["cached_tokens"] = 21
        if bad == "total": raw["usage"]["total_tokens"] = 29
        if bad == "reasoning": raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 11}
        if bad == "model": raw["model"] = "wrong"
        if bad == "id": raw.pop("id")
        if bad == "noninteger": raw["usage"]["prompt_tokens"] = 20.0
        if bad == "boolean": raw["usage"]["completion_tokens"] = True
        if bad == "missing_prompt": raw["usage"].pop("prompt_tokens")
        if bad == "unknown_usage": raw["usage"]["cost_usd"] = 0
        if bad == "shape": raw["choices"] = []
        if bad == "content": raw["choices"][0]["message"]["content"] = None
        if bad == "cost_zero": envelope["stats"]["cost_usd"] = 0
        if bad == "timestamp": envelope["request_started_utc"] = "2026-10-16T12:00:00+00:00"
        if bad == "enriched_content": envelope["content"] = "Changed"
    rewrite(path, corrupt)
    before = bytes_under(adapter.root)
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        adapter.inspect_locked()
    assert bytes_under(adapter.root) == before


@pytest.mark.parametrize("bad", ["duplicate", "nan", "overflow", "malformed", "record_hash", "response_hash",
    "extra_file", "journal_ahead", "missing_context", "changed_context", "orphan_context", "missing_history"])
def test_inventory_evidence_attacks(batch, bad):
    _, obj, adapter = batch
    req, _, _ = operation(batch)
    path = publish(req)
    context = adapter.context_dir / (req["operation_id"] + ".json")
    if bad == "duplicate": path.write_bytes(path.read_bytes().replace(b'"schema_version": 1,', b'"schema_version": 1, "schema_version": 1,'))
    if bad in {"nan", "overflow"}: path.write_bytes(path.read_bytes().replace(b'"schema_version": 1', b'"schema_version": ' + (b'NaN' if bad == "nan" else b'1e999')))
    if bad == "malformed": path.write_bytes(b'{"bad":')
    if bad in {"record_hash", "response_hash"}:
        record = json.loads(path.read_bytes())
        record[bad.replace("hash", "sha256")] = "0" * 64
        if bad == "response_hash": record = journal._with_record_hash(record)
        core.atomic_write_json(path, record)
    if bad == "extra_file": (path.parent / "foreign.tmp").write_text("retained")
    if bad == "journal_ahead":
        other, _, _ = operation(batch, "state_extraction", reserve=False)
        publish(other)
        (adapter.context_dir / (other["operation_id"] + ".json")).unlink()  # synthetic attacker, not repair
    if bad == "missing_context": context.unlink()
    if bad == "changed_context": context.write_text("{}")
    if bad == "orphan_context": (adapter.context_dir / "foreign.json").write_text("{}")
    if bad == "missing_history":
        # Original B1 binding contains an expected historical journal, but it is absent.
        obj.binding["initial_evidence"]["data/provider_request_journal/0000.absent.json"] = "0" * 64
        # This is in-memory identity drift, which original ledger replay must reject.
    before = bytes_under(adapter.root)
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        adapter.inspect_locked()
    assert bytes_under(adapter.root) == before


@pytest.mark.parametrize("started,period", [("2026-10-16T00:59:59.999999+00:00", "off_peak"),
    ("2026-10-16T01:00:00+00:00", "peak"), ("2026-10-16T04:00:00+00:00", "off_peak"),
    ("2026-10-17T02:00:00+00:00", "off_peak")])
def test_exact_utc_pricing_and_auxiliary_hard_bound(batch, started, period):
    req, _, evidence = operation(batch, "state_extraction", started=started)
    assert evidence["pricing"]["snapshot"]["pricing_period"] == period
    assert evidence["completion_exposure"]["completion_upper"] == 131072
    assert req["exposure"]["nano_usd"] == (196608000 if period == "peak" else 98304000)


@pytest.mark.parametrize("stamp", ["yesterday", "2026-10-16", "2026-10-16T02:00:00-05:00", "", None])
def test_malformed_or_non_utc_start_fails_before_reservation(batch, stamp):
    _, obj, _ = batch
    before = obj.snapshot_locked()
    with pytest.raises((ValueError, RuntimeError)):
        operation(batch, started=stamp)
    assert obj.snapshot_locked() == before


def test_decimal_rounding_independent_of_float_or_decimal_context():
    from decimal import localcontext
    rates = dict(input="0.0000001", cache_read="0.00000001", output="0.000000001")
    with localcontext() as ctx:
        ctx.prec = 2
        assert bridge._nano(rates, fresh=10000, cached=0, output=0) == 1
        assert bridge._nano(rates, fresh=10000, cached=0, output=1) == 2
    assert bridge._nano(rates, fresh=0, cached=0, output=0) == 0


def test_decimal_large_count_never_loses_the_last_nano_units():
    rates = dict(input="0.15", cache_read="0.003", output="0.60")
    count = 10 ** 150 + 1
    assert bridge._nano(rates, fresh=count, cached=1, output=1) == count * 150 + 603


@pytest.mark.parametrize("kind", ["state_extraction_uncertain_no_record_recovery", "state_extraction_terminal_recovery",
    "state_extraction_auxiliary_policy_migration_v1", "chapter_translation_cap_recovery_v2",
    "chapter_glossary_review_reasoning_empty_recovery", "state_extraction_recovery"])
def test_special_identity_routes_explicitly_fail_closed(batch, kind):
    _, obj, _ = batch
    before = bytes_under(obj.root)
    with pytest.raises(bridge.JournalEvidenceError, match="special operation"):
        operation(batch, kind)
    assert bytes_under(obj.root) == before


def test_fresh_process_receipt_with_no_writes_or_send(batch, tmp_path):
    _, obj, adapter = batch
    req, _, _ = operation(batch)
    path = publish(req)
    receipt = adapter.validate_recorded_response_locked(path, req)
    binding_file = tmp_path / "original-binding.json"
    binding_file.write_bytes(ledger.canonical(obj.binding))
    script = tmp_path / "reload.py"
    script.write_text('''import json, sys
from pathlib import Path
from production_run_lock import production_run_lock
from production_goat_batch_ledger import BatchLedger
from production_goat_batch_journal import BatchJournalEvidence
binding=json.loads(Path(sys.argv[1]).read_bytes())
obj=BatchLedger.open_locked(binding['project_root'], 'synthetic-b2b', binding)
adapter=BatchJournalEvidence(obj)
print(json.dumps(adapter.inspect_locked(), sort_keys=True))
''')
    # Parent retains the cooperative lock across this read-only child inspection.
    env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "COMMAND_CODE_API_KEY"}}
    env["PYTHONPATH"] = os.pathsep.join([os.environ.get("PYTHONPATH", ""), str(Path(bridge.__file__).parent)])
    completed = subprocess.run([sys.executable, "-B", str(script), str(binding_file)],
        capture_output=True, text=True, env=env, check=True)
    report = json.loads(completed.stdout)
    assert report["receipts"][req["operation_id"]] == receipt
    assert not report["send_authorized"]


@pytest.mark.parametrize("mutation", ["raw_usage", "missing_journal", "planned", "context_start", "source"])
def test_post_settlement_evidence_cannot_regress_or_change(batch, mutation):
    project, obj, adapter = batch
    req, _, _ = operation(batch)
    path = publish(req)
    obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
    if mutation == "raw_usage":
        def changed(record):
            record["response"]["raw_response"]["usage"].update(completion_tokens=11, total_tokens=31)
        rewrite(path, changed)
    if mutation == "missing_journal": path.unlink()
    if mutation == "planned": core.atomic_write_json(path, journal._new_planned_record(req["journal_identity"]))
    if mutation == "context_start":
        context = adapter.context_dir / (req["operation_id"] + ".json")
        value = json.loads(context.read_bytes())
        value["request_started_utc"] = "2026-10-16T12:00:00+00:00"
        context.write_bytes(ledger.canonical(value))
    if mutation == "source": project.chapters_file.write_text("[]")
    before = bytes_under(project.root)
    with pytest.raises((ValueError, RuntimeError)):
        adapter.inspect_locked()
    assert bytes_under(project.root) == before


@pytest.mark.parametrize("transition", ["reserve", "attempt", "settle"])
@pytest.mark.parametrize("boundary", ["intent", "before_replace", "after_replace"])
def test_real_journal_ledger_publication_crashes(batch, monkeypatch, transition, boundary):
    project, obj, adapter = batch
    req, _, _ = operation(batch, reserve=transition != "reserve")
    path = publish(req)
    original_sync, original_replace = ledger.os.fsync, ledger.os.replace
    syncs = 0
    class Crash(BaseException):
        pass
    def sync(fd):
        nonlocal syncs
        original_sync(fd)
        syncs += 1
        if boundary == "intent" and syncs == 1: raise Crash()
    def replace(source, target):
        if boundary == "before_replace": raise Crash()
        original_replace(source, target)
        if boundary == "after_replace": raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(ledger.os, "fsync", sync)
        patch.setattr(ledger.os, "replace", replace)
        with pytest.raises(Crash):
            if transition == "reserve": obj.reserve_locked(req)
            if transition == "attempt": obj.note_attempt_locked(req["operation_id"])
            if transition == "settle": obj.settle_locked(req["operation_id"],
                validate_recorded_response=adapter.validate_recorded_response_locked)
    proof = bytes_under(project.root)
    if boundary != "after_replace":
        with pytest.raises(ledger.LedgerError):
            ledger.BatchLedger.open_locked(project.root, "synthetic-b2b", obj.binding)
    else:
        fresh = ledger.BatchLedger.open_locked(project.root, "synthetic-b2b", obj.binding)
        reader = bridge.BatchJournalEvidence(fresh)
        state = fresh.snapshot_locked()
        assert state["physical_slots"] == 1
        assert not fresh.reserve_locked(req)["send_authorized"]
        # Complete durable response survives every ledger crash; a fresh validator
        # permits local-only settlement, including reservation-only crash recovery.
        fresh.settle_locked(req["operation_id"], validate_recorded_response=reader.validate_recorded_response_locked)
        assert fresh.snapshot_locked()["credit_committed_nano_usd"] == 17118
    assert path.read_bytes() == proof[path.relative_to(project.root).as_posix()]


@pytest.mark.parametrize("after", [False, True])
def test_started_replace_baseexception_never_calls_transport(batch, monkeypatch, after):
    _, obj, adapter = batch
    req, _, _ = operation(batch)
    path = Path(req["journal_path"])
    sends = []
    replace = core.os.replace
    class Crash(BaseException):
        pass
    def interrupted(source, target):
        is_started = json.loads(Path(source).read_bytes()).get("status") == "started"
        if is_started and not after: raise Crash()
        replace(source, target)
        if is_started: raise Crash()
    @contextmanager
    def authorize():
        obj.note_attempt_locked(req["operation_id"])
        yield lambda: sends.append(1)
    with monkeypatch.context() as patch:
        patch.setattr(core.os, "replace", interrupted)
        with pytest.raises(Crash):
            journal.run_journaled_paid_request(journal_path=path, identity=req["journal_identity"],
                save_json=core.atomic_write_json, prepare_attempt=lambda: {"request_started_utc": START},
                send_once=lambda: pytest.fail("bypassed authorization"), authorize_send=authorize,
                normalize_response=lambda *a: pytest.fail("no response"))
    report = adapter.inspect_locked()
    assert report["ambiguous_operations"] == [req["operation_id"]]
    assert not report["send_authorized"] and not sends
    assert obj.snapshot_locked()["credit_committed_nano_usd"] == req["exposure"]["nano_usd"]


@pytest.mark.parametrize("exposed", [False, True])
def test_cache_accounting_and_legacy_enrichment(batch, exposed):
    _, obj, adapter = batch
    req, wire, _ = operation(batch)
    raw = raw_response_record(response())
    if not exposed: raw["usage"].pop("prompt_tokens_details")
    path = publish(req, raw=raw)
    first = adapter.validate_recorded_response_locked(path, req)
    assert first["actual"]["nano_usd"] == (17118 if exposed else 18000)
    envelope = accepted_envelope(json.loads(path.read_bytes())["response"], wire)
    for key in ("provider", "reasoning_tokens", "reasoning_tokens_exposed", "visible_completion_tokens"):
        envelope["stats"].pop(key, None)
    rewrite(path, lambda r: r.update(response=envelope))
    assert adapter.validate_recorded_response_locked(path, req) == first


def test_ratecard_and_hard_bound_drift_refuse_before_reserve(batch, monkeypatch):
    _, obj, adapter = batch
    before = obj.snapshot_locked()
    with monkeypatch.context() as patch:
        patch.setitem(bridge.tariff._MODEL_RATECARD["deepseek/deepseek-v4.1-flash"]["periods"]["peak"], "input", "0.29")
        with pytest.raises(bridge.JournalEvidenceError, match="ratecard drift"):
            operation(batch)
    import production_goat_auxiliary_policy as auxiliary
    with monkeypatch.context() as patch:
        patch.setattr(auxiliary, "REVIEWED_HARD_BOUNDS", {})
        with pytest.raises(ValueError, match="hard bound"):
            operation(batch, "state_extraction")
    assert obj.snapshot_locked() == before


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_original_ceiling_exact_nano_boundary(tmp_path, difference):
    project = fixture_novel(tmp_path)
    original = plan(project)["authorization_binding"]
    # Construct once to determine the authoritative exact request exposure.
    with production_run_lock(project.root):
        temporary = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b", original)
        batch = project, temporary, bridge.BatchJournalEvidence(temporary)
        req, _, _ = operation(batch, reserve=False)
        cap = (req["exposure"]["nano_usd"] + difference) / 1_000_000_000
        binding = plan(project, max_credit_equivalent_usd=cap)["authorization_binding"]
        # A distinct SYNTHETIC parent starts before any send. No resume/renewal.
        obj = ledger.BatchLedger.create_locked(project.root, "separate-cap-fixture", binding)
        req["batch_id"] = "separate-cap-fixture"
        if difference < 0:
            with pytest.raises(ledger.LedgerError, match="credit ceiling"):
                obj.reserve_locked(req)
            assert obj.snapshot_locked()["physical_slots"] == 0
        else:
            obj.reserve_locked(req)
            assert obj.snapshot_locked()["remaining_nano_usd"] == difference


def test_multiple_real_operation_kinds_share_permanent_slots(batch):
    _, obj, adapter = batch
    for kind, chapter in [("chapter_translation", 1), ("state_extraction", 1),
                           ("context_audit", 1), ("state_json_repair", 1), ("chapter_translation", 2)]:
        req, _, _ = operation(batch, kind, chapter=chapter)
        path = publish(req)
        obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
    assert obj.snapshot_locked()["physical_slots"] == 5
    assert obj.snapshot_locked()["credit_committed_nano_usd"] == 5 * 17118
    assert len(adapter.inspect_locked()["receipts"]) == 5
    before = bytes_under(obj.path)
    with pytest.raises(ledger.LedgerError, match="send ceiling"):
        operation(batch, "chapter_translation", chapter=3)
    assert bytes_under(obj.path) == before


def test_verified_overrun_retains_actual_and_stops(batch):
    _, obj, adapter = batch
    req, _, _ = operation(batch)
    raw = raw_response_record(response(completion=10_000_000))
    path = publish(req, raw=raw)
    obj.settle_locked(req["operation_id"], validate_recorded_response=adapter.validate_recorded_response_locked)
    state = obj.snapshot_locked()
    assert state["state"] == "STOP" and state["stop_reasons"] == ["VERIFIED_OVERRUN"]
    assert state["credit_committed_nano_usd"] > obj.ceiling
    assert state["operations"][req["operation_id"]]["receipt"] == adapter.validate_recorded_response_locked(path, req)


@pytest.mark.parametrize("alter", ["none", "delete", "rehash_change"])
def test_original_historical_journal_is_never_batch_spend(tmp_path, alter):
    project = fixture_novel(tmp_path, next_chapter=2)
    wire = normalize_production_request("translation", kwargs())
    policy = bridge.provider_identity("command_code_provider", "v41-flash")
    identity = journal.request_identity(operation_kind="chapter_translation", novel_id=project.config.id, chapter=1,
        provider=policy["provider"], model=policy["wire_model"], profile_id=project.config.profile,
        source_sha256=__import__("hashlib").sha256("甲 entered.".encode()).hexdigest(),
        request_payload={"wire_request": wire, "provider_policy": policy})
    identity.update(provider_policy=policy, wire_request_sha256=journal._sha256_json(wire))
    path = journal.paid_request_journal_path(project.data_dir / "provider_request_journal", "chapter_translation", 1)
    publish(dict(journal_path=str(path), journal_identity=identity))
    binding = plan(project)["authorization_binding"]
    with production_run_lock(project.root):
        obj = ledger.BatchLedger.create_locked(project.root, "historical-fixture", binding)
        adapter = bridge.BatchJournalEvidence(obj)
        if alter == "delete": path.unlink()
        if alter == "rehash_change": rewrite(path, lambda r: r.update(created_utc="2026-10-15T02:00:00+00:00"))
        before = bytes_under(project.root)
        if alter == "none":
            result = adapter.inspect_locked()
            assert result["historical_files"] == [path.relative_to(project.root).as_posix()]
            assert result["receipts"] == {} and obj.snapshot_locked()["physical_slots"] == 0
        else:
            with pytest.raises(bridge.JournalEvidenceError): adapter.inspect_locked()
        assert bytes_under(project.root) == before


def test_fresh_import_is_inert_with_guessed_flags_and_token(tmp_path):
    env = dict(os.environ, NT_GOAT_BATCH_ENABLED="1", NT_B2B_ENABLED="1", NT_B1_APPROVAL_TOKEN="guessed")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    code = '''import sys
import production_goat_batch_journal as bridge
assert 'command_code_provider_credentials' not in sys.modules
assert 'production_goat_execution' not in sys.modules
assert 'openai' not in sys.modules
assert not hasattr(bridge.BatchJournalEvidence, 'authorize')
assert not hasattr(bridge.BatchJournalEvidence, 'send')
print('B2B1_INERT_NO_PAID_EXECUTOR')
'''
    before = bytes_under(tmp_path)
    result = subprocess.run([sys.executable, "-B", "-c", code], env=env, cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "B2B1_INERT_NO_PAID_EXECUTOR"
    assert bytes_under(tmp_path) == before


@pytest.mark.parametrize("language,profile", [("zh", "historical_military"), ("ko", "korean_fantasy")])
def test_real_evidence_chinese_and_korean_parent_bindings(tmp_path, language, profile):
    project = fixture_novel(tmp_path, language=language, profile=profile)
    binding = plan(project)["authorization_binding"]
    with production_run_lock(project.root):
        obj = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b", binding)
        adapter = bridge.BatchJournalEvidence(obj)
        req, _, _ = operation((project, obj, adapter))
        path = publish(req)
        assert adapter.validate_recorded_response_locked(path, req)["actual"]["nano_usd"] == 17118
        assert not adapter.inspect_locked()["send_authorized"]
