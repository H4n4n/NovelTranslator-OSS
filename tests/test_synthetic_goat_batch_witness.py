"""B2B2B-2 synthetic, offline witness probes; never live enrollment or authority."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest
import production_goat_batch_witness as w
import synthetic_goat_batch_witness as s
from test_production_goat_batch_witness import authority, enrollment, no_authority, sha

CHALLENGE = b"SYNTHETIC-challenge-0001"


def evidence(action="RESERVE", **changes):
    value = dict(synthetic_only=True, action=action)
    if action in {"RESERVE", "SETTLE"}:
        value.update(operation_id="synthetic-op", amount_nano_usd=200_000_000)
    if action == "SETTLE":
        value.update(amount_nano_usd=100_000_000, prompt_tokens=10, completion_tokens=20)
    if action == "STOP":
        value["reason"] = "AMBIGUOUS"
    value.update(changes)
    return w.canonical_bytes(value)


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def history(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT * FROM transitions ORDER BY enrollment, sequence").fetchall()


def current(store, en, retained=None):
    raw = store.read_current(enrollment=en, challenge=CHALLENGE, retained=retained)
    head = w.WitnessHead.from_value(json.loads(raw)["head"])
    no_authority(s.qualify_ack(raw, operation="READ", enrollment=en, head=head,
        evidence=b"", challenge=CHALLENGE), "SYNTHETIC_MATCH_UNVERIFIED_NO_SEND")
    return head


def append(store, en, before, raw=None, identifier="synthetic-transition"):
    raw = evidence() if raw is None else raw
    after = store.propose(enrollment=en, expected=before, transition_id=identifier, canonical_evidence=raw)
    result = s.probe_append(store, enrollment=en, expected=before, proposed=after,
        canonical_evidence=raw, challenge=CHALLENGE)
    no_authority(result, "SYNTHETIC_MATCH_UNVERIFIED_NO_SEND")
    return after


@pytest.fixture
def enrolled(tmp_path):
    root = tmp_path / "disposable-witness"
    root.mkdir()
    pin = w.Authority.from_value(authority())
    en = w.Enrollment.from_value(enrollment())
    store = s.SyntheticWitness.create(root, pin)
    ack = store.enroll(en, challenge=CHALLENGE)
    no_authority(s.qualify_ack(ack, operation="ENROLL", enrollment=en, head=s.genesis(en),
        evidence=b"", challenge=CHALLENGE), "SYNTHETIC_MATCH_UNVERIFIED_NO_SEND")
    return store, en


def reopened(store):
    return s.SyntheticWitness(store.path.parent, store.authority)


def test_monotonic_reserve_settle_stop_restart_and_exact_prefix(enrolled):
    store, en = enrolled
    initial = current(store, en)
    prefix = history(store)
    reserved = append(store, en, initial)
    settled = append(reopened(store), en, reserved, evidence("SETTLE"), "synthetic-settlement")
    stopped = append(store, en, settled, evidence("STOP"), "synthetic-stop")
    cp = stopped.to_dict()["checkpoint"]
    assert (cp["physical_slots"], cp["settled_credit_nano_usd"], cp["outstanding_exposure_nano_usd"]) == (1, 100_000_000, 0)
    assert (cp["prompt_tokens"], cp["completion_tokens"], cp["stop_reasons"]) == (10, 20, ["AMBIGUOUS"])
    assert history(store)[:len(prefix)] == prefix
    assert current(reopened(store), en, reserved) == stopped
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="STOP_LATCHED"):
        append(store, en, stopped, evidence(operation_id="new-op"))
    assert files(store.path.parent) == before
    assert en.raw == w.Enrollment.from_value(enrollment()).raw
    assert en.to_dict()["original_b1_sha256"] == initial.to_dict()["original_b1_sha256"] == stopped.to_dict()["original_b1_sha256"]


@pytest.mark.parametrize("field", ["enrollment_id", "batch_id", "storage_id", "novel_id", "grant_sha256",
    "original_b1_sha256", "ledger_identity_sha256", "max_api_calls", "max_credit_nano_usd", "initial_runtime_sha256", "policy_sha256"])
def test_reenrollment_cannot_renew_allowance_or_alias_identity(enrolled, field):
    store, en = enrolled
    append(store, en, current(store, en))
    changed = en.to_dict()
    changed[field] = changed[field] + 1 if type(changed[field]) is int else sha("changed") if field.endswith("sha256") else "changed"
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="ENROLLMENT_ALREADY_EXISTS"):
        reopened(store).enroll(w.Enrollment.from_value(changed), challenge=CHALLENGE)
    with pytest.raises(s.SyntheticRefusal, match="ENROLLMENT_ALREADY_EXISTS"):
        store.enroll(en, challenge=CHALLENGE)
    assert files(store.path.parent) == before


@pytest.mark.parametrize("collision", [0, 1, 2, 3, 4])
def test_each_registry_identity_key_is_independently_unique(enrolled, collision):
    store, en = enrolled
    changed = en.to_dict()
    for field in ("enrollment_id", "grant_sha256", "original_b1_sha256", "ledger_identity_sha256", "novel_id", "storage_id", "batch_id"):
        changed[field] = sha("other-" + field) if field.endswith("sha256") else "other-" + field.replace("_", "-")
    fields = [("enrollment_id",), ("grant_sha256",), ("original_b1_sha256",), ("ledger_identity_sha256",),
        ("novel_id", "storage_id", "batch_id")][collision]
    for field in fields:
        changed[field] = en.to_dict()[field]
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="ENROLLMENT_ALREADY_EXISTS"):
        store.enroll(w.Enrollment.from_value(changed), challenge=CHALLENGE)
    assert files(store.path.parent) == before


def test_different_synthetic_enrollments_have_disjoint_histories(enrolled):
    store, en = enrolled
    changes = {k: sha("different-" + k) for k in ("grant_sha256", "original_b1_sha256", "ledger_identity_sha256")}
    changes.update(enrollment_id="other-enrollment", batch_id="other-batch")
    other = w.Enrollment.from_value(enrollment(**changes))
    store.enroll(other, challenge=CHALLENGE)
    before = current(store, other)
    append(store, en, current(store, en))
    assert current(store, other) == before


@pytest.mark.parametrize("operation", ["enroll", "append"])
def test_atomic_competing_clients_accept_exactly_one(enrolled, operation):
    store, en = enrolled
    initial = current(store, en)
    proposals = [store.propose(enrollment=en, expected=initial, transition_id="race-" + str(i),
        canonical_evidence=evidence(operation_id="race-op-" + str(i))) for i in range(2)]
    barrier = Barrier(2)
    prefix = history(store)
    clients = [reopened(store), reopened(store)]
    def worker(i):
        client = clients[i]
        barrier.wait(timeout=10)
        try:
            if operation == "append":
                return client.compare_and_append(enrollment=en, expected=initial, proposed=proposals[i],
                    canonical_evidence=evidence(operation_id="race-op-" + str(i)), challenge=CHALLENGE)
            return client.enroll(w.Enrollment.from_value(enrollment(enrollment_id="racer-" + str(i),
                grant_sha256=sha("new-grant"), original_b1_sha256=sha("new-b1"),
                ledger_identity_sha256=sha("new-ledger"), batch_id="new-batch")), challenge=CHALLENGE)
        except s.SyntheticRefusal:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sum(type(result) is bytes for result in results) == 1
    after = history(store)
    assert all(row in after for row in prefix) and len(after) == len(prefix) + 1


def test_idempotent_id_retains_exact_evidence_and_old_ack_cannot_be_current(enrolled):
    store, en = enrolled
    initial = current(store, en)
    after = append(store, en, initial)
    stop = append(store, en, after, evidence("STOP"), "stop-after-reserve")
    before = files(store.path.parent)
    args = dict(enrollment=en, expected=initial, proposed=after, canonical_evidence=evidence(), challenge=CHALLENGE)
    raw = reopened(store).compare_and_append(**args)
    assert json.loads(raw)["kind"] == "HISTORICAL_RECEIPT"
    no_authority(s.qualify_ack(raw, operation="APPEND", enrollment=en, head=after, evidence=evidence(),
        challenge=CHALLENGE), "SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")
    assert current(store, en) == stop and files(store.path.parent) == before
    for changes in ({"canonical_evidence": evidence(amount_nano_usd=1)},
            {"proposed": w.WitnessHead.from_value(dict(after.to_dict(), checkpoint=initial.to_dict()["checkpoint"]))}):
        with pytest.raises(s.SyntheticRefusal, match="TRANSITION_ID_CONFLICT"):
            store.compare_and_append(**dict(args, **changes))
        assert files(store.path.parent) == before


@pytest.mark.parametrize("field", ["witness_sequence", "previous_head_sha256", "ledger_head_sha256", "ledger_previous_sha256",
    "physical_slots", "settled_credit_nano_usd", "outstanding_exposure_nano_usd", "prompt_tokens", "domain_sha256",
    "domain_revision", "journal_inventory_sha256", "publication_proof_sha256", "progress", "stop_reasons", "evidence_kind"])
def test_forged_fork_regression_or_domain_claim_rejected_exact_bytes(enrolled, field):
    store, en = enrolled
    initial = current(store, en)
    head = append(store, en, initial)
    proposed = store.propose(enrollment=en, expected=head, transition_id="next", canonical_evidence=evidence("STOP"))
    changed = proposed.to_dict()
    if field == "witness_sequence": changed[field] += 1
    elif field == "previous_head_sha256": changed[field] = sha("fork")
    elif field == "evidence_kind": changed[field] = "HISTORICAL_RECEIPT"
    elif field == "progress": changed["checkpoint"].update(last_completed=1, next_chapter=2)
    elif field == "stop_reasons": changed["checkpoint"][field] = []
    elif field == "physical_slots": changed["checkpoint"][field] = 0
    elif field.endswith("sha256"): changed["checkpoint"][field] = sha("forged")
    else: changed["checkpoint"][field] += 1
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="PROPOSED_HEAD_DIVERGENCE"):
        store.compare_and_append(enrollment=en, expected=head, proposed=w.WitnessHead.from_value(changed),
            canonical_evidence=evidence("STOP"), challenge=CHALLENGE)
    assert files(store.path.parent) == before


def test_stale_expected_and_deleted_local_suffix_rejected_by_retained_head(enrolled):
    store, en = enrolled
    initial = current(store, en)
    rival = store.propose(enrollment=en, expected=initial, transition_id="rival", canonical_evidence=evidence())
    after = append(store, en, initial)
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="STALE_EXPECTED_HEAD"):
        store.compare_and_append(enrollment=en, expected=initial, proposed=rival, canonical_evidence=evidence(), challenge=CHALLENGE)
    assert files(store.path.parent) == before
    old_local = w.LocalHistory.from_value(dict(schema_version=1, revision=w.REVISION,
        enrollment_sha256=en.sha256, original_b1_sha256=en.to_dict()["original_b1_sha256"], checkpoint=initial.to_dict()["checkpoint"]))
    no_authority(w.compare_checkpoints(enrollment=en, pinned_authority=store.authority,
        original_b1_sha256=en.to_dict()["original_b1_sha256"], local=old_local, witness=after), "WITNESS_AHEAD")
    assert files(store.path.parent) == before


@pytest.mark.parametrize("tamper", ["gap", "head_bytes", "evidence_bytes", "genesis", "registry", "missing_genesis", "orphan"])
def test_corrupt_retained_history_stops_without_repair(enrolled, tamper):
    store, en = enrolled
    one = append(store, en, current(store, en))
    append(store, en, one, evidence("STOP"), "stop")
    with sqlite3.connect(store.path) as db:
        if tamper == "gap": db.execute("DELETE FROM transitions WHERE sequence=1")
        if tamper == "missing_genesis": db.execute("DELETE FROM transitions WHERE sequence=0")
        if tamper == "head_bytes": db.execute("UPDATE transitions SET raw=? WHERE sequence=1", (b"{}",))
        if tamper == "evidence_bytes": db.execute("UPDATE transitions SET evidence=? WHERE sequence=1", (evidence(amount_nano_usd=1),))
        if tamper == "genesis": db.execute("UPDATE transitions SET evidence=? WHERE sequence=0", (evidence(),))
        if tamper == "registry": db.execute("UPDATE enrollments SET b1_digest=?", (sha("reset"),))
        if tamper == "orphan": db.execute("DELETE FROM enrollments")
    before = files(store.path.parent)
    with pytest.raises((s.SyntheticRefusal, w.WitnessContractError)):
        current(reopened(store), en)
    assert files(store.path.parent) == before


def test_whole_store_rollback_requires_separately_retained_anchor(enrolled):
    store, en = enrolled
    initial = current(store, en)
    old_bytes = store.path.read_bytes()
    after = append(store, en, initial)
    store.path.write_bytes(old_bytes)  # Deliberate adversary, disposable fixture only.
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="RETAINED_HEAD_DIVERGENCE"):
        current(reopened(store), en, after)
    assert files(store.path.parent) == before
    # Honest limitation: rollback of BOTH the entire store and caller anchor is undetectable.
    assert current(reopened(store), en, initial) == initial
    assert files(store.path.parent) == before


@pytest.mark.parametrize("where", ["pin", "enrollment", "store"])
def test_authority_epoch_drift_is_not_migrated(enrolled, where):
    store, en = enrolled
    pin = w.Authority.from_value(dict(authority(), authority_epoch=2))
    if where == "store":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE authority SET raw=?", (pin.raw,))
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="AUTHORITY_MISMATCH"):
        if where == "pin": s.SyntheticWitness(store.path.parent, pin)
        elif where == "enrollment": store.enroll(w.Enrollment.from_value(dict(en.to_dict(), authority=pin.to_dict())), challenge=CHALLENGE)
        else: current(store, en)
    assert files(store.path.parent) == before


@pytest.mark.parametrize("case", ["credit", "slots", "unreserved_settlement", "duplicate_reservation", "duplicate_settlement", "overrun"])
def test_synthetic_cap_and_accounting_fail_closed(enrolled, case):
    store, en = enrolled
    head = current(store, en)
    if case == "unreserved_settlement": raw = evidence("SETTLE")
    elif case == "credit": raw = evidence(amount_nano_usd=en.to_dict()["max_credit_nano_usd"] + 1)
    else:
        head = append(store, en, head)
        raw = evidence()
        if case == "duplicate_settlement":
            head = append(store, en, head, evidence("SETTLE"), "settle")
            raw = evidence("SETTLE")
        if case == "slots":
            for i in range(3):
                head = append(store, en, head, evidence(operation_id="op-" + str(i), amount_nano_usd=0), "slot-" + str(i))
            raw = evidence(operation_id="extra-op", amount_nano_usd=0)
        if case == "overrun":
            head = append(store, en, head, evidence("SETTLE", amount_nano_usd=1_000_000_001), "overrun")
            assert head.to_dict()["checkpoint"]["stop_reasons"] == ["VERIFIED_OVERRUN"]
            raw = evidence(operation_id="extra-op", amount_nano_usd=0)
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal):
        append(store, en, head, raw, "refused")
    assert files(store.path.parent) == before


@pytest.mark.parametrize("stage", ["after_insert", "before_commit", "after_commit"])
@pytest.mark.parametrize("operation", ["enroll", "append"])
def test_crash_boundary_recovers_only_committed_history(enrolled, monkeypatch, stage, operation):
    store, en = enrolled
    head = current(store, en)
    next_head = store.propose(enrollment=en, expected=head, transition_id="crashing", canonical_evidence=evidence())
    other = w.Enrollment.from_value(enrollment(enrollment_id="other", grant_sha256=sha("other-grant"),
        original_b1_sha256=sha("other-b1"), ledger_identity_sha256=sha("other-ledger"), batch_id="other"))
    prefix = history(store)
    before = files(store.path.parent)
    calls = []
    def crash(observed):
        if observed == stage:
            calls.append(observed)
            raise OSError("SYNTHETIC interrupted ACK")
    with monkeypatch.context() as scoped:
        scoped.setattr(s, "_fault", crash)
        if operation == "append":
            no_authority(s.probe_append(store, enrollment=en, expected=head, proposed=next_head,
                canonical_evidence=evidence(), challenge=CHALLENGE), "SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")
        else:
            with pytest.raises(OSError): store.enroll(other, challenge=CHALLENGE)
    assert calls == [stage]
    rows = history(reopened(store))
    assert all(row in rows for row in prefix)
    assert len(rows) == len(prefix) + int(stage == "after_commit")
    if stage != "after_commit": assert files(store.path.parent) == before
    if operation == "append": assert current(store, en) == (next_head if stage == "after_commit" else head)
    elif stage == "after_commit":
        retained = files(store.path.parent)
        with pytest.raises(s.SyntheticRefusal, match="ENROLLMENT_ALREADY_EXISTS"):
            store.enroll(other, challenge=CHALLENGE)
        assert files(store.path.parent) == retained


@pytest.mark.parametrize("stage", ["after_insert", "before_commit", "after_commit"])
def test_hard_process_exit_and_fresh_process_read(enrolled, tmp_path, stage):
    store, en = enrolled
    initial = current(store, en)
    after = store.propose(enrollment=en, expected=initial, transition_id="hard-exit", canonical_evidence=evidence())
    config = tmp_path / "synthetic-config.json"
    config.write_text(json.dumps(dict(root=str(store.path.parent), authority=store.authority.to_dict(),
        enrollment=en.to_dict(), expected=initial.to_dict(), proposed=after.to_dict())), encoding="utf-8")
    code = '''
import json, os, sys
from pathlib import Path
import production_goat_batch_witness as w
import synthetic_goat_batch_witness as s
c = json.loads(Path(sys.argv[1]).read_text())
client = s.SyntheticWitness(c['root'], w.Authority.from_value(c['authority']))
def crash(stage):
    if stage == sys.argv[2]: os._exit(71)
s._fault = crash
raw = w.canonical_bytes(dict(synthetic_only=True, action='RESERVE', operation_id='synthetic-op', amount_nano_usd=200000000))
client.compare_and_append(enrollment=w.Enrollment.from_value(c['enrollment']), expected=w.WitnessHead.from_value(c['expected']),
    proposed=w.WitnessHead.from_value(c['proposed']), canonical_evidence=raw, challenge=b'SYNTHETIC-challenge-0001')
'''
    env = dict(os.environ)
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join((str(repo), str(repo / "tests")))
    prefix = history(store)
    result = subprocess.run([sys.executable, "-B", "-c", code, str(config), stage],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 71, result.stderr
    # Reopen SQLite to recover an uncommitted hot journal, with no append retry.
    assert current(reopened(store), en) == (after if stage == "after_commit" else initial)
    assert history(store)[:len(prefix)] == prefix
    read_code = '''
import json, sys
from pathlib import Path
import production_goat_batch_witness as w
import synthetic_goat_batch_witness as s
c = json.loads(Path(sys.argv[1]).read_text())
a = s.SyntheticWitness(c['root'], w.Authority.from_value(c['authority']))
r = a.read_current(enrollment=w.Enrollment.from_value(c['enrollment']), challenge=b'SYNTHETIC-challenge-0001')
print(json.loads(r)['head_sha256'])
'''
    result = subprocess.run([sys.executable, "-B", "-c", read_code, str(config)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (after if stage == "after_commit" else initial).sha256


@pytest.mark.parametrize("mutation", ["lost", "truncated", "wrong_challenge", "wrong_head", "wrong_enrollment", "historical", "noncanonical"])
def test_ambiguous_or_replayed_ack_never_retries_or_authorizes(enrolled, mutation):
    store, en = enrolled
    head = current(store, en)
    proposed = store.propose(enrollment=en, expected=head, transition_id="ack-test", canonical_evidence=evidence())
    calls = []
    def call(**kwargs):
        calls.append(1)
        raw = store.compare_and_append(**kwargs)
        if mutation == "lost": raise OSError("SYNTHETIC ACK lost")
        if mutation == "truncated": return raw[:len(raw) // 2]
        if mutation == "noncanonical": return raw + b"\n"
        ack = json.loads(raw)
        if mutation == "wrong_challenge": ack["challenge_sha256"] = sha("old-challenge")
        if mutation == "wrong_head": ack["head_sha256"] = sha("old-head")
        if mutation == "wrong_enrollment": ack["enrollment_sha256"] = sha("foreign")
        if mutation == "historical": ack["kind"] = "HISTORICAL_RECEIPT"
        return w.canonical_bytes(ack)
    no_authority(s.probe_append(SimpleNamespace(compare_and_append=call), enrollment=en, expected=head,
        proposed=proposed, canonical_evidence=evidence(), challenge=CHALLENGE), "SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")
    assert calls == [1] and current(reopened(store), en) == proposed
    cp = proposed.to_dict()["checkpoint"]
    assert cp["physical_slots"] == 1 and cp["outstanding_exposure_nano_usd"] == 200_000_000
    assert len(history(store)) == 2


def test_missing_store_and_failed_genesis_never_recreated(enrolled, tmp_path):
    store, en = enrolled
    store.path.unlink()  # Disposable adversary only.
    with pytest.raises(s.SyntheticRefusal, match="STORE_UNAVAILABLE"): current(store, en)
    assert not store.path.exists()
    store.path.write_bytes(b"")
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal): reopened(store)
    with pytest.raises(s.SyntheticRefusal, match="EMPTY_TEST_ROOT_REQUIRED"):
        s.SyntheticWitness.create(store.path.parent, store.authority)
    assert files(store.path.parent) == before
    with pytest.raises(s.SyntheticRefusal, match="TEST_TEMP_ROOT_REQUIRED"):
        s.SyntheticWitness.create(Path(__file__).resolve().parents[1], store.authority)


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"synthetic_only":true,"synthetic_only":true}',
    b'{"synthetic_only":false}', b'{', b'\xff', b'[]', evidence() + b'\n',
    evidence("STOP", reason="RESET"), evidence("RESERVE", amount_nano_usd=True),
    evidence("SETTLE", prompt_tokens="10"), evidence("DOMAIN"), evidence(credential="forbidden")],
    ids=["bad-evidence-" + str(i) for i in range(13)])
def test_malformed_unbounded_or_unsupported_evidence_preserved(enrolled, raw):
    store, en = enrolled
    before = files(store.path.parent)
    with pytest.raises((s.SyntheticRefusal, w.WitnessContractError)):
        store.propose(enrollment=en, expected=s.genesis(en), transition_id="bad", canonical_evidence=raw)
    assert files(store.path.parent) == before


def test_synthetic_success_rejected_by_exact_production_context_type(enrolled):
    from production_goat_runtime import install_goat_runtime, GoatRuntimeError
    store, en = enrolled
    after = append(store, en, current(store, en))
    raw = store.read_current(enrollment=en, challenge=CHALLENGE)
    diagnostic = s.qualify_ack(raw, operation="READ", enrollment=en, head=after, evidence=b"", challenge=CHALLENGE)
    before = files(store.path.parent)
    for fake in (store, diagnostic, raw, after):
        module = SimpleNamespace(_RUNTIME_INITIALIZED=True)
        with pytest.raises(GoatRuntimeError, match="exact B2B2"):
            install_goat_runtime(module, provider_client=None, batch_context=fake)
        assert vars(module) == {"_RUNTIME_INITIALIZED": True}
    assert files(store.path.parent) == before


def test_simulator_not_imported_by_any_production_module():
    repo = Path(__file__).resolve().parents[1]
    for path in repo.glob("*.py"):
        assert b"synthetic_goat_batch_witness" not in path.read_bytes()


@pytest.mark.usefixtures("synthetic_goat_hard_bound")
def test_real_b1_opaque_binding_and_synthetic_source_history_untouched(enrolled, tmp_path):
    from test_goat_batch_plan import fixture_novel, plan
    import production_goat_batch_ledger as ledger
    from production_run_lock import production_run_lock
    from controlled_trial_plan import _canonical_hash
    store, _ = enrolled
    project = fixture_novel(tmp_path / "synthetic-project")
    binding = plan(project, max_api_calls=4, max_credit_equivalent_usd=1.0000000001)["authorization_binding"]
    opaque = _canonical_hash(binding)
    assert type(binding["max_credit_equivalent_usd"]) is float
    original_b1 = ledger.canonical(binding)
    with production_run_lock(project.root):
        batch = ledger.BatchLedger.create_locked(project.root, "synthetic-b2b2b2", binding)
        # Lock rendezvous byte is intentionally unreadable on Windows while held.
        before = {p.relative_to(project.root).as_posix(): p.read_bytes() for p in project.root.rglob("*")
            if p.is_file() and p.name != ".production.lock"}
        en = w.Enrollment.from_value(enrollment(enrollment_id="bound-original-b1", batch_id="synthetic-b2b2b2",
            grant_sha256=sha("bound-grant"), original_b1_sha256=opaque, ledger_identity_sha256=ledger.digest(batch.identity)))
        store.enroll(en, challenge=CHALLENGE)
        head = append(store, en, current(store, en))
        append(store, en, head, evidence("SETTLE"), "bound-settle")
        after = {p.relative_to(project.root).as_posix(): p.read_bytes() for p in project.root.rglob("*")
            if p.is_file() and p.name != ".production.lock"}
        assert after == before
        assert {k: hashlib.sha256(v).hexdigest() for k, v in after.items()} == {k: hashlib.sha256(v).hexdigest() for k, v in before.items()}
        assert ledger.canonical(binding) == original_b1 and _canonical_hash(binding) == opaque == ledger.digest(binding)
        assert batch.snapshot_locked()["physical_slots"] == 0
        assert not (project.data_dir / "goat_batch_enrollment").exists()


@pytest.mark.parametrize("challenge", [b"", b"short", b"x" * 65, "SYNTHETIC-challenge-0001", None])
def test_invalid_challenge_never_changes_history(enrolled, challenge):
    store, en = enrolled
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="INVALID_CHALLENGE"):
        store.read_current(enrollment=en, challenge=challenge)
    with pytest.raises(s.SyntheticRefusal, match="INVALID_CHALLENGE"):
        store.enroll(en, challenge=challenge)
    assert files(store.path.parent) == before


def test_fresh_import_inert_with_execution_and_filesystem_guard(tmp_path):
    code = '''
import os, sys
active = True
def guard(event, args):
    if not active: return
    if event.startswith('socket.') or event == 'sqlite3.connect': raise AssertionError('network or store access')
    if event in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir'): raise AssertionError('mutation')
    if event == 'ctypes.dlsym' and 'Crypt' in str(args): raise AssertionError('DPAPI')
    if event == 'import' and args[0].split('.')[0] in ('openai', 'dotenv', 'command_code_provider_credentials',
        'production_goat_runtime', 'production_goat_batch_ledger'): raise AssertionError('execution dependency')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.fsdecode(args[0]).lower().endswith(('.env', '.dpapi')): raise AssertionError('credential access')
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC): raise AssertionError('write')
sys.addaudithook(guard)
import synthetic_goat_batch_witness as s
assert not hasattr(s, 'main') and not hasattr(s, 'send')
print('SYNTHETIC_INERT_NO_SEND')
active = False
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join((str(repo), str(repo / "tests")))
    before = files(tmp_path)
    result = subprocess.run([sys.executable, "-B", "-c", code], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SYNTHETIC_INERT_NO_SEND" and files(tmp_path) == before


def test_settled_plus_outstanding_reuses_original_ceiling_only(enrolled):
    store, en = enrolled
    head = append(store, en, current(store, en), evidence(amount_nano_usd=600_000_000), "first-reserve")
    head = append(store, en, head, evidence("SETTLE", amount_nano_usd=400_000_000), "first-settle")
    head = append(store, en, head, evidence(operation_id="second-op", amount_nano_usd=600_000_000), "second-reserve")
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="CREDIT_CAP_EXCEEDED"):
        append(store, en, head, evidence(operation_id="excess-op", amount_nano_usd=1), "excess-reserve")
    assert files(store.path.parent) == before
    stopped = append(store, en, head, evidence("STOP"), "ambiguous-stop")
    settled = append(store, en, stopped, evidence("SETTLE", operation_id="second-op", amount_nano_usd=500_000_000), "second-settle")
    cp = settled.to_dict()["checkpoint"]
    assert (cp["physical_slots"], cp["settled_credit_nano_usd"], cp["outstanding_exposure_nano_usd"]) == (2, 900_000_000, 0)
    assert cp["stop_reasons"] == ["AMBIGUOUS"]
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="STOP_LATCHED"):
        append(store, en, settled, evidence(operation_id="new-op", amount_nano_usd=1), "after-stop")
    assert files(store.path.parent) == before


def test_unhashable_stop_reason_refuses_with_fixed_diagnostic(enrolled):
    store, en = enrolled
    before = files(store.path.parent)
    with pytest.raises(s.SyntheticRefusal, match="INVALID_STOP"):
        store.propose(enrollment=en, expected=s.genesis(en), transition_id="invalid-stop",
            canonical_evidence=evidence("STOP", reason=[]))
    assert files(store.path.parent) == before
