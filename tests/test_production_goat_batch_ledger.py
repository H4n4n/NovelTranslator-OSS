"""B2A SYNTHETIC journal proofs only; never production journal verification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import production_goat_batch_ledger as ledger
from production_run_lock import production_run_lock, ProductionRunLockedError
from test_goat_batch_plan import fixture_novel, plan
from test_production_goat_runtime import zero_network


pytestmark = pytest.mark.usefixtures("zero_network", "synthetic_goat_hard_bound")


def money(n):
    return dict(unit=ledger.UNIT, nano_usd=n)


def request(binding, label="translation", n=200_000_000):
    op = ledger.digest(label)
    wire = ledger.digest({"synthetic_wire": label})
    policy = binding["provider_identity"]
    identity = dict(operation_id=op, chapter=binding["initial_chapter"], novel_id=binding["novel_id"],
        provider=policy["provider"], model=policy["wire_model"], provider_policy=policy,
        wire_request_sha256=wire, synthetic_operation=label)
    return dict(batch_id="synthetic-batch", operation_id=op, chapter=binding["initial_chapter"],
        journal_path=str(Path(binding["project_root"]) / "data" / "provider_request_journal" / (label + ".json")),
        journal_identity=identity, wire_request_sha256=wire, provider_policy_sha256=ledger.digest(policy),
        exposure={**money(n), "evidence_sha256": ledger.digest({"SYNTHETIC_exposure": n})})


def fake_journal(req, status="RESPONSE_RECORDED", actual=100_000_000):
    """Not a production journal. Independently reread by synthetic validators."""
    path = Path(req["journal_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(SYNTHETIC_ONLY=True, reservation_sha256=ledger.digest(req), status=status,
        response={"text": "SYNTHETIC paid response", "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        actual=money(actual))
    value["sha256"] = ledger.digest(value)
    path.write_bytes(ledger.canonical(value))
    return value


def synthetic_read(path, req):
    value = ledger.strict_load(path.read_bytes())
    assert value["SYNTHETIC_ONLY"] is True
    ledger.require(value["sha256"] == ledger.digest({k: v for k, v in value.items() if k != "sha256"}),
                   "synthetic record hash mismatch")
    ledger.require(value["reservation_sha256"] == ledger.digest(req), "synthetic identity mismatch")
    return value


def synthetic_receipt(path, req):
    value = synthetic_read(path, req)
    ledger.require(value["status"] == "RESPONSE_RECORDED", "synthetic response not recorded")
    usage = value["response"].get("usage")
    ledger.fields(usage, {"prompt_tokens", "completion_tokens"})
    for v in usage.values():
        ledger.integer(v)
    return dict(status=value["status"], reservation_sha256=value["reservation_sha256"],
        journal_record_sha256=value["sha256"], response_sha256=ledger.digest(value["response"]),
        usage_sha256=ledger.digest(usage), actual=value["actual"])


def synthetic_observation(path, req):
    value = synthetic_read(path, req) if path.exists() else {"status": "ABSENT"}
    return dict(status=value["status"], reservation_sha256=ledger.digest(req), evidence_sha256=ledger.digest(value))


@pytest.fixture
def bound(tmp_path):
    project = fixture_novel(tmp_path)
    return plan(project, max_api_calls=4, max_credit_equivalent_usd=1)["authorization_binding"]


def create(binding):
    return ledger.BatchLedger.create_locked(binding["project_root"], "synthetic-batch", binding)


def reopen(binding):
    return ledger.BatchLedger.open_locked(binding["project_root"], "synthetic-batch", binding)


def snapshot(root):
    # Windows byte-range locking intentionally prevents reading the rendezvous
    # byte while held. Its persistent existence is tested by the lock suite.
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != ".production.lock"}


@pytest.mark.parametrize("language,profile", [("zh", "historical_military"), ("ko", "korean_fantasy")])
def test_b1_original_binding_and_manifest(tmp_path, language, profile):
    project = fixture_novel(tmp_path, language=language, profile=profile)
    binding = plan(project, max_api_calls=4, max_credit_equivalent_usd=1.0000000001)["authorization_binding"]
    with production_run_lock(project.root):
        before = snapshot(project.root)
        obj = create(binding)
        initial = obj.snapshot_locked()
        assert initial == reopen(binding).snapshot_locked()
        assert initial["remaining_nano_usd"] == 1_000_000_000
        assert initial["paid_execution_authorized"] is False
        assert obj.identity["original_fingerprint"] == ledger.digest(binding)
        assert obj.binding == binding
        req = request(binding)
        obj.reserve_locked(req)
        after = snapshot(project.root)
        assert all(after[k] == v for k, v in before.items())
        assert all(k.startswith("data/goat_batch_ledgers/") for k in after.keys() - before.keys())
        assert [p.suffix for p in sorted(obj.path.iterdir())] == [".intent", ".json"] * 2
        with pytest.raises(ledger.LedgerError):
            create(binding)
        with pytest.raises(ProductionRunLockedError):
            with production_run_lock(project.root):
                pass


def test_shared_lifetime_capacity_fresh_process(bound, tmp_path):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        for i, label in enumerate(("translation", "glossary", "state_extraction", "audit_chunk"), 1):
            req = request(bound, label)
            result = reopen(bound).reserve_locked(req)
            assert result["ordinal"] == i and not result["send_authorized"]
            again = reopen(bound).reserve_locked(req)
            assert not again["new_reservation"] and again["ordinal"] == i
        with pytest.raises(ledger.LedgerError, match="physical-send"):
            reopen(bound).reserve_locked(request(bound, "extra", 0))
    config = tmp_path / "binding.json"
    config.write_bytes(ledger.canonical(bound))
    code = """import json, sys
from production_goat_batch_ledger import BatchLedger
from production_run_lock import production_run_lock
b=json.load(open(sys.argv[1],encoding='utf-8'))
with production_run_lock(b['project_root']):
 print(json.dumps(BatchLedger.open_locked(b['project_root'],'synthetic-batch',b).snapshot_locked()))
"""
    result = child(code, config, tmp_path)
    state = json.loads(result.stdout)
    assert state["physical_slots"] == 4 and state["remaining_nano_usd"] == 200_000_000
    assert state["operations"][ledger.digest("audit_chunk")]["ordinal"] == 4


def child(code, config, tmp_path, expected=0):
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = str(Path(ledger.__file__).parent)
    result = subprocess.run([sys.executable, "-B", "-c", code, str(config)], cwd=tmp_path,
                            env=env, text=True, capture_output=True)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None, float("nan"), float("inf"), -float("inf")])
def test_invalid_exposure_fails_without_writes(bound, value):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        before = snapshot(obj.path)
        req = request(bound)
        req["exposure"]["nano_usd"] = value
        with pytest.raises(ledger.LedgerError):
            obj.reserve_locked(req)
        assert snapshot(obj.path) == before


def test_credit_capacity_and_pause_never_reset(bound):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        obj.reserve_locked(request(bound, n=900_000_000))
        with pytest.raises(ledger.LedgerError, match="credit ceiling"):
            obj.reserve_locked(request(bound, "state"))
        assert reopen(bound).snapshot_locked()["remaining_nano_usd"] == 100_000_000
        obj.stop_locked("OPERATOR_PAUSE")
        obj.stop_locked("OPERATOR_PAUSE")
        with pytest.raises(ledger.LedgerError):
            reopen(bound).reserve_locked(request(bound, "state", 0))
        assert reopen(bound).snapshot_locked()["stop_reasons"] == ["OPERATOR_PAUSE"]


def test_settled_plus_outstanding_exposure_uses_one_ceiling(bound):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound, n=600_000_000)
        obj.reserve_locked(req)
        fake_journal(req, actual=400_000_000)
        obj.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        obj.reserve_locked(request(bound, "state", 400_000_000))
        with pytest.raises(ledger.LedgerError, match="credit ceiling"):
            reopen(bound).reserve_locked(request(bound, "audit", 200_000_001))
        assert reopen(bound).snapshot_locked()["credit_committed_nano_usd"] == 800_000_000
        reopened = reopen(bound)
        reopened.reserve_locked(request(bound, "audit", 200_000_000))
        assert reopened.snapshot_locked()["remaining_nano_usd"] == 0


@pytest.mark.parametrize("field", ["wire", "journal", "chapter", "model", "exposure", "policy", "slot", "unit", "unknown"])
def test_conflicting_replay(bound, field):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        changed = copy.deepcopy(req)
        if field == "wire": changed["wire_request_sha256"] = "0" * 64
        if field == "journal": changed["journal_path"] = str(Path(req["journal_path"]).with_name("other.json"))
        if field == "chapter": changed["chapter"] += 1
        if field == "model": changed["journal_identity"]["model"] = "other-model"
        if field == "exposure": changed["exposure"]["nano_usd"] += 1
        if field == "policy": changed["provider_policy_sha256"] = "0" * 64
        if field == "slot":
            changed["operation_id"] = "0" * 64
            changed["journal_identity"]["operation_id"] = "0" * 64
        if field == "unit": changed["exposure"]["unit"] = "USD"
        if field == "unknown": changed["unknown"] = 1
        before = snapshot(obj.path)
        with pytest.raises(ledger.LedgerError): obj.reserve_locked(changed)
        assert snapshot(obj.path) == before


@pytest.mark.parametrize("field", ["max_api_calls", "max_credit_equivalent_usd", "runtime_identity", "pricing", "initial_evidence"])
def test_parent_cannot_be_renewed(bound, field):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        changed = copy.deepcopy(bound)
        if field in {"max_api_calls", "max_credit_equivalent_usd"}: changed[field] += 1
        else: changed[field] = {"SYNTHETIC_DRIFT": True}
        with pytest.raises(ledger.LedgerError): reopen(changed)
        assert obj.snapshot_locked()["physical_slots"] == 0


@pytest.mark.parametrize("stage", ["intent", "before_replace", "after_replace"])
@pytest.mark.parametrize("transition", ["reserve", "settle"])
def test_interrupted_publication(bound, monkeypatch, stage, transition):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        if transition == "settle":
            obj.reserve_locked(req)
            fake_journal(req)
        before = snapshot(obj.path)
        original_replace, original_fsync = ledger.os.replace, ledger.os.fsync
        count = 0
        def sync(fd):
            nonlocal count
            original_fsync(fd)
            count += 1
            if stage == "intent" and count == 1: raise KeyboardInterrupt("SYNTHETIC intent interruption")
        def replace(source, target):
            if stage == "before_replace": raise KeyboardInterrupt("SYNTHETIC pre-replace")
            original_replace(source, target)
            if stage == "after_replace": raise KeyboardInterrupt("SYNTHETIC post-replace")
        with monkeypatch.context() as patch:
            patch.setattr(ledger.os, "fsync", sync)
            patch.setattr(ledger.os, "replace", replace)
            with pytest.raises(KeyboardInterrupt):
                if transition == "reserve": obj.reserve_locked(req)
                else: obj.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        after = snapshot(obj.path)
        assert all(after[k] == v for k, v in before.items())
        if stage != "after_replace":
            with pytest.raises(ledger.LedgerError): reopen(bound)
            with pytest.raises(ledger.LedgerError): create(bound)
        else:
            restarted = reopen(bound)
            state = restarted.snapshot_locked()
            assert state["physical_slots"] == 1
            assert state["credit_committed_nano_usd"] == (200_000_000 if transition == "reserve" else 100_000_000)
            assert not restarted.reserve_locked(req)["new_reservation"]


@pytest.mark.parametrize("status", ["ABSENT", "PLANNED", "STARTED", "UNCERTAIN", "RESPONSE_RECORDED"])
def test_reconciliation_at_fake_journal_boundaries(bound, status):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        if status != "ABSENT": fake_journal(req, status)
        restarted = reopen(bound)
        result = restarted.reconcile_locked(req["operation_id"], validate_observation=synthetic_observation,
                                             validate_recorded_response=synthetic_receipt)
        state = reopen(bound).snapshot_locked()
        assert state["physical_slots"] == 1
        if status == "RESPONSE_RECORDED":
            assert result == "SETTLED_NO_SEND" and state["credit_committed_nano_usd"] == 100_000_000
        else:
            assert state["credit_committed_nano_usd"] == 200_000_000
            assert result == ("BLOCKED_AMBIGUOUS" if status in {"STARTED", "UNCERTAIN"} else "LEDGER_AHEAD_NO_SEND")
        assert not reopen(bound).reserve_locked(req)["send_authorized"]


def test_attempted_and_journal_ahead_are_not_free(bound):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        obj.note_attempt_locked(req["operation_id"])
        obj.note_attempt_locked(req["operation_id"])
        assert obj.reconcile_locked(req["operation_id"], validate_observation=synthetic_observation,
                                    validate_recorded_response=synthetic_receipt) == "BLOCKED_AMBIGUOUS"
        assert obj.reconcile_locked("0" * 64, validate_observation=None,
                                    validate_recorded_response=None) == "BLOCKED_JOURNAL_AHEAD"
        assert reopen(bound).snapshot_locked()["physical_slots"] == 1


@pytest.mark.parametrize("actual", [0, 100_000_000, 200_000_001, 1_000_000_001])
def test_once_only_settlement_and_verified_overrun(bound, actual):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        fake_journal(req, actual=actual)
        obj.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        before = snapshot(obj.path)
        reopened = reopen(bound)
        reopened.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        reopened.note_attempt_locked(req["operation_id"])
        assert before == snapshot(obj.path)
        state = reopened.snapshot_locked()
        assert state["credit_committed_nano_usd"] == actual and state["physical_slots"] == 1
        assert (state["state"] == "STOP") == (actual > 200_000_000)
        if actual > 200_000_000:
            with pytest.raises(ledger.LedgerError): reopened.reserve_locked(request(bound, "other", 0))


@pytest.mark.parametrize("bad", ["missing_usage", "usage_bool", "wrong_identity", "wrong_hash", "receipt_identity", "extra_field", "missing_actual"])
def test_unvalidated_usage_keeps_reservation_and_stops(bound, bad):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        value = fake_journal(req)
        if bad == "missing_usage": value["response"].pop("usage")
        if bad == "usage_bool": value["response"]["usage"]["prompt_tokens"] = True
        if bad == "wrong_identity": value["reservation_sha256"] = "0" * 64
        if bad == "missing_actual": value.pop("actual")
        value["sha256"] = ledger.digest({k: v for k, v in value.items() if k != "sha256"})
        if bad == "wrong_hash": value["sha256"] = "0" * 64
        Path(req["journal_path"]).write_bytes(ledger.canonical(value))
        def validate(path, request):
            receipt = synthetic_receipt(path, request)
            if bad == "receipt_identity": receipt["reservation_sha256"] = "0" * 64
            if bad == "extra_field": receipt["unexpected"] = 1
            return receipt
        with pytest.raises((ledger.LedgerError, KeyError)):
            obj.settle_locked(req["operation_id"], validate_recorded_response=validate)
        state = reopen(bound).snapshot_locked()
        assert state["state"] == "STOP" and state["credit_committed_nano_usd"] == 200_000_000


@pytest.mark.parametrize("bad", ["missing", "truncated", "duplicate", "nan", "extra", "mismatch", "foreign", "missing_intent"])
def test_corrupt_evidence_preserved(bound, bad):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        target = obj.path / "00000000.json"
        if bad == "missing": target.unlink()
        if bad == "truncated": target.write_bytes(b"{")
        if bad == "duplicate": target.write_bytes(b'{"schema":1,"schema":1}')
        if bad == "nan": target.write_bytes(b'{"schema":NaN}')
        if bad == "extra":
            value = ledger.strict_load(target.read_bytes()); value["foreign"] = True
            raw = ledger.canonical(value)
            target.write_bytes(raw); target.with_suffix(".intent").write_bytes(raw)
        if bad == "mismatch":
            with pytest.raises(ledger.LedgerError):
                ledger.BatchLedger.open_locked(bound["project_root"], "other-batch", bound)
            target.write_bytes(b"[]")
        if bad == "foreign": (obj.path / "foreign.tmp").write_bytes(b"evidence")
        if bad == "missing_intent": target.with_suffix(".intent").unlink()
        before = snapshot(obj.path)
        with pytest.raises(ledger.LedgerError): reopen(bound)
        with pytest.raises(ledger.LedgerError): create(bound)
        assert before == snapshot(obj.path)


def test_live_instance_rejects_foreign_valid_append(bound):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        reopen(bound).reserve_locked(request(bound))
        with pytest.raises(ledger.LedgerError, match="foreign write"): obj.snapshot_locked()


@pytest.mark.parametrize("after_replace", [False, True])
def test_hard_process_death_retains_slot_or_blocks(bound, tmp_path, after_replace):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
    config = tmp_path / "crash.json"
    req = request(bound)
    config.write_bytes(ledger.canonical(dict(binding=bound, request=req, after=after_replace)))
    code = '''import json,sys,os
import production_goat_batch_ledger as l
from production_run_lock import production_run_lock
c=json.load(open(sys.argv[1],encoding='utf-8')); b=c['binding']
with production_run_lock(b['project_root']):
 o=l.BatchLedger.open_locked(b['project_root'],'synthetic-batch',b)
 original=l.os.replace
 def die(source,target):
  if c['after']: original(source,target)
  os._exit(71)
 l.os.replace=die
 o.reserve_locked(c['request'])
'''
    child(code, config, tmp_path, expected=71)
    with production_run_lock(bound["project_root"]):
        if after_replace:
            restarted = reopen(bound)
            assert restarted.snapshot_locked()["physical_slots"] == 1
            assert restarted.snapshot_locked()["remaining_nano_usd"] == 800_000_000
            assert not restarted.reserve_locked(req)["new_reservation"]
        else:
            with pytest.raises(ledger.LedgerError): reopen(bound)
            assert len(list(obj.path.glob("*.intent"))) == 2
            assert len(list(obj.path.glob("*.tmp"))) == 1


def test_raw_checkout_bytes_and_path_bound(bound, tmp_path, monkeypatch):
    runtime = tmp_path / "SYNTHETIC-runtime"
    profile = runtime / "profiles" / bound["profile"]
    profile.mkdir(parents=True)
    engine = runtime / "synthetic.py"
    engine.write_bytes(b"# SYNTHETIC\r\n")
    (profile / "policy.txt").write_bytes(b"SYNTHETIC policy")
    (runtime / "pyproject.toml").write_bytes(b"# SYNTHETIC dependencies")
    monkeypatch.setattr(ledger, "__file__", str(engine))
    bound["runtime_identity"] = dict(profile=bound["profile"], file_count=2,
        sha256=ledger.digest({p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (engine, profile / "policy.txt")}))
    bound["dependency_policy_sha256"] = hashlib.sha256((runtime / "pyproject.toml").read_bytes()).hexdigest()
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        before = snapshot(obj.path)
        engine.write_bytes(b"# SYNTHETIC\n")
        with pytest.raises(ledger.LedgerError, match="raw operating runtime drift"): reopen(bound)
        assert snapshot(obj.path) == before
        engine.write_bytes(b"# SYNTHETIC\r\n")
        assert reopen(bound).snapshot_locked()["physical_slots"] == 0
        other = tmp_path / "other-SYNTHETIC-runtime"
        import shutil
        shutil.copytree(runtime, other)
        monkeypatch.setattr(ledger, "__file__", str(other / "synthetic.py"))
        with pytest.raises(ledger.LedgerError, match="authorization/storage drift"): reopen(bound)


def test_reparse_ancestor_refused(bound, tmp_path):
    target = tmp_path / "SYNTHETIC-target"
    target.mkdir()
    alias = tmp_path / "SYNTHETIC-alias"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(alias))
    else:
        alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ledger.LedgerError, match="symlink/reparse"):
        ledger.safe_path(alias / "child")


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}', b'{'])
def test_strict_json_input(raw):
    with pytest.raises(ledger.LedgerError): ledger.strict_load(raw)


def test_missing_expected_ledger_never_created(bound):
    with production_run_lock(bound["project_root"]):
        path = Path(bound["project_root"]) / "data" / "goat_batch_ledgers"
        with pytest.raises(ledger.LedgerError): reopen(bound)
        assert not path.exists()


def test_settlement_replay_conflict_and_evidence_regression(bound):
    with production_run_lock(bound["project_root"]):
        obj = create(bound)
        req = request(bound)
        obj.reserve_locked(req)
        fake_journal(req)
        obj.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        fake_journal(req, actual=1)
        with pytest.raises(ledger.LedgerError, match="replay drift"):
            obj.settle_locked(req["operation_id"], validate_recorded_response=synthetic_receipt)
        assert obj.snapshot_locked()["credit_committed_nano_usd"] == 100_000_000
        Path(req["journal_path"]).unlink()
        with pytest.raises(ledger.LedgerError, match="regressed"):
            obj.reconcile_locked(req["operation_id"], validate_observation=synthetic_observation,
                                 validate_recorded_response=synthetic_receipt)


def test_module_import_and_operations_guarded_child(bound, tmp_path):
    config = tmp_path / "bound.json"
    config.write_bytes(ledger.canonical(bound))
    code = r'''
import sys, os, pathlib, json
allowed=pathlib.Path(sys.argv[1]).parent.resolve()
blocked=[]
def guard(event,args):
 if event in ('socket.connect','socket.getaddrinfo'): raise AssertionError('network')
 if event=='import' and any(s in args[0] for s in ('credential','production_goat_runtime','controlled_trial_execute','production_goat_execution','openai','dotenv')): raise AssertionError('paid import '+args[0])
 if event=='ctypes.dlsym' and 'Crypt' in str(args): raise AssertionError('DPAPI')
 if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
  p=pathlib.Path(os.fsdecode(args[0])).resolve()
  if p.name=='.env' or p.suffix=='.dpapi': raise AssertionError('credential')
  flags=args[2] if len(args)>2 and isinstance(args[2],int) else 0
  if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC) and allowed not in p.parents: raise AssertionError('escaped write')
 if event in ('os.mkdir','os.rename','os.remove','os.rmdir'):
  for item in args[:2] if event=='os.rename' else args[:1]:
   p=pathlib.Path(item).resolve()
   if p!=allowed and allowed not in p.parents: raise AssertionError('escaped mutation')
sys.addaudithook(guard)
import runpy
sys.argv=['production_goat_batch_ledger.py','--help',sys.argv[1]]
runpy.run_module('production_goat_batch_ledger',run_name='__main__')
import production_goat_batch_ledger as l
from production_run_lock import production_run_lock
b=json.load(open(sys.argv[2],encoding='utf-8'))
with production_run_lock(b['project_root']):
 o=l.BatchLedger.create_locked(b['project_root'],'synthetic-batch',b)
 assert o.snapshot_locked()['physical_slots']==0
 o.stop_locked('OPERATOR_PAUSE')
 assert l.BatchLedger.open_locked(b['project_root'],'synthetic-batch',b).snapshot_locked()['state']=='STOP'
print('ZERO_PROVIDER_GUARD_PASS network=0 credential=0 DPAPI=0 escaped_writes=0 paid_imports=0')
'''
    assert "ZERO_PROVIDER_GUARD_PASS" in child(code, config, tmp_path).stdout
