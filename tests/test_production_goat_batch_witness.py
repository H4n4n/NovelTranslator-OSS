"""B2B2B-1 structural diagnostics: synthetic evidence, no service or transport."""
import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import production_goat_batch_witness as w


def sha(label):
    return hashlib.sha256(("SYNTHETIC:" + label).encode()).hexdigest()


def version(**values):
    return dict(schema_version=1, revision=w.REVISION, **values)


def authority():
    return version(authority_id="synthetic-authority", authority_epoch=1)


def checkpoint(count=1, **changes):
    value = version(ledger_schema=1, ledger_event_count=count, ledger_last_sequence=count - 1,
        ledger_head_sha256=sha("ledger-" + str(count)),
        ledger_previous_sha256=None if count == 1 else sha("ledger-" + str(count - 1)),
        evidence_state="COMPLETE", physical_slots=0, settled_credit_nano_usd=0,
        outstanding_exposure_nano_usd=0, prompt_tokens=0, completion_tokens=0,
        domain_revision=0, domain_sha256=sha("domain"), journal_inventory_sha256=sha("inventory"),
        last_completed=0, next_chapter=1, audited_through=0,
        publication_proof_sha256=None, stop_reasons=[])
    value.update(changes)
    return value


def enrollment(**changes):
    value = version(authority=authority(), enrollment_id="synthetic-enrollment", grant_sha256=sha("grant"),
        original_b1_sha256=sha("original-b1"), novel_id="synthetic-novel", storage_id="synthetic-storage",
        batch_id="synthetic-batch", ledger_identity_sha256=sha("ledger-identity"),
        initial_runtime_sha256=sha("runtime"), policy_sha256=sha("policy"), initial_checkpoint=checkpoint(),
        initial_chapter=1, end_chapter=5, max_api_calls=4, max_credit_nano_usd=1_000_000_000,
        execution_mode="NO_SEND")
    value.update(changes)
    return value


def local(en=None, cp=None):
    en = enrollment() if en is None else en
    return version(enrollment_sha256=w.Enrollment.from_value(en).sha256,
        original_b1_sha256=en["original_b1_sha256"], checkpoint=checkpoint() if cp is None else cp)


def head(en=None, cp=None, **changes):
    en = enrollment() if en is None else en
    value = version(authority=authority(), enrollment_sha256=w.Enrollment.from_value(en).sha256,
        original_b1_sha256=en["original_b1_sha256"], witness_sequence=0, previous_head_sha256=None,
        transition_id="synthetic-genesis", evidence_kind="SUPPLIED_HEAD",
        checkpoint=checkpoint() if cp is None else cp)
    value.update(changes)
    return value


def compare(**changes):
    args = dict(enrollment=enrollment(), pinned_authority=authority(), original_b1_sha256=sha("original-b1"),
                local=local(), witness=head())
    args.update(changes)
    return w.compare_checkpoints(**args)


def successor_args():
    before = head()
    after = head(cp=checkpoint(2, physical_slots=1, outstanding_exposure_nano_usd=200_000_000),
        witness_sequence=1, previous_head_sha256=w.WitnessHead.from_value(before).sha256,
        transition_id="synthetic-reservation")
    return dict(enrollment=enrollment(), pinned_authority=authority(), original_b1_sha256=sha("original-b1"),
                current=before, proposed=after)


def no_authority(result, status):
    assert result.status == status
    assert result.send_authorized is False and result.paid_execution_authorized is False
    assert result.authority_verified is False and result.freshness_verified is False
    assert result.transition_committed is False
    with pytest.raises(TypeError, match="not authorization"):
        bool(result)
    with pytest.raises(FrozenInstanceError):
        result.status = "AUTHORIZED"


@pytest.mark.parametrize("cls,factory", [(w.Authority, authority), (w.Checkpoint, checkpoint),
    (w.Enrollment, enrollment), (w.LocalHistory, local), (w.WitnessHead, head)])
def test_strict_records_round_trip_detached_and_immutable(cls, factory):
    original = factory()
    record = cls.from_value(original)
    assert record.to_dict() == original and cls(record.raw) == record
    assert len(record.raw) <= w.MAX_RECORD_BYTES
    assert record.sha256 == cls.from_value(dict(reversed(list(original.items())))).sha256
    original.clear()
    decoded = record.to_dict()
    decoded.clear()
    assert record.to_dict() == factory()
    with pytest.raises(FrozenInstanceError):
        record.raw = b"{}"
    assert not hasattr(record, "__dict__")


def test_nested_mutation_and_enrollment_commitment():
    value = enrollment()
    record = w.Enrollment.from_value(value)
    original = record.raw
    value["initial_checkpoint"]["stop_reasons"].append("AMBIGUOUS")
    record.to_dict()["authority"]["authority_epoch"] = 9
    record.to_dict()["initial_checkpoint"]["stop_reasons"].append("AMBIGUOUS")
    assert record.raw == original and record.to_dict() == enrollment()
    assert w.Checkpoint.from_value(checkpoint()).sha256 != hashlib.sha256(w.canonical_bytes(checkpoint())).hexdigest()


@pytest.mark.parametrize("cls,factory", [(w.Authority, authority), (w.Checkpoint, checkpoint),
    (w.Enrollment, enrollment), (w.LocalHistory, local), (w.WitnessHead, head)])
@pytest.mark.parametrize("change", ["extra", "missing", "schema", "revision", "bool_schema", "float_schema"])
def test_exact_fields_and_versions(cls, factory, change):
    value = factory()
    if change == "extra": value["api_key"] = "SYNTHETIC-NOT-A-SECRET"
    if change == "missing": value.pop("revision")
    if change == "schema": value["schema_version"] = 2
    if change == "revision": value["revision"] = "future"
    if change == "bool_schema": value["schema_version"] = True
    if change == "float_schema": value["schema_version"] = 1.0
    with pytest.raises(w.WitnessContractError): cls.from_value(value)


@pytest.mark.parametrize("raw", [b'{"schema_version":1,"schema_version":1}', b'{"a":NaN}',
    b'{"a":Infinity}', b'{"a":1e999}', b'{"a":0.1}', b'{', b'[]', b'null', b'', b'\xff',
    b'\xef\xbb\xbf{}', b'{' + b' ' * w.MAX_RECORD_BYTES + b'}'], ids=[f'bad-json-{n}' for n in range(12)])
def test_bad_json_and_bounded_input(raw):
    with pytest.raises(w.WitnessContractError): w.Checkpoint(raw)


@pytest.mark.parametrize("transform", [lambda raw: raw + b"\n", lambda raw: b" " + raw,
    lambda raw: raw.replace(b'":', b'": '),
    lambda raw: json.dumps(json.loads(raw), sort_keys=False, indent=2).encode(),
    lambda raw: raw.replace(b'"NO_SEND"', b'"\\u004eO_SEND"')])
def test_noncanonical_encoding_rejected(transform):
    with pytest.raises(w.WitnessContractError, match="NONCANONICAL"):
        w.Enrollment(transform(w.Enrollment.from_value(enrollment()).raw))


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, Decimal("1"), "1", None,
    float("nan"), float("inf"), w.MAX_INTEGER + 1])
@pytest.mark.parametrize("field", ["ledger_event_count", "ledger_last_sequence", "physical_slots",
    "settled_credit_nano_usd", "outstanding_exposure_nano_usd", "prompt_tokens", "completion_tokens"])
def test_exact_integer_quantities(field, bad):
    value = checkpoint(2)
    value[field] = bad
    with pytest.raises(w.WitnessContractError): w.Checkpoint.from_value(value)


@pytest.mark.parametrize("field,bad", [("ledger_last_sequence", 2), ("ledger_schema", 2),
    ("ledger_schema", True), ("next_chapter", 3), ("audited_through", 1),
    ("ledger_head_sha256", "A" * 64), ("ledger_head_sha256", "f" * 63),
    ("ledger_previous_sha256", None), ("publication_proof_sha256", ""),
    ("stop_reasons", ["UNVALIDATED_EVIDENCE", "AMBIGUOUS"]),
    ("stop_reasons", ["AMBIGUOUS", "AMBIGUOUS"]), ("stop_reasons", ["guess"]),
    ("evidence_state", "READY")])
def test_checkpoint_relationships(field, bad):
    value = checkpoint(2)
    value[field] = bad
    with pytest.raises(w.WitnessContractError): w.Checkpoint.from_value(value)


@pytest.mark.parametrize("field,bad", [("batch_id", "../alias"), ("enrollment_id", "x" * 65),
    ("storage_id", "some prose with spaces"), ("grant_sha256", None),
    ("max_api_calls", True), ("max_api_calls", 1_000_001),
    ("max_credit_nano_usd", Decimal("1")), ("max_credit_nano_usd", 1.0),
    ("max_credit_nano_usd", w.MAX_CREDIT_NANO_USD + 1), ("execution_mode", "PAID"),
    ("initial_chapter", 0), ("end_chapter", 1001)])
def test_enrollment_identity_limits_and_no_send_mode(field, bad):
    value = enrollment()
    value[field] = bad
    with pytest.raises(w.WitnessContractError): w.Enrollment.from_value(value)


@pytest.mark.parametrize("change", ["slots", "cost", "initial_count", "initial_domain", "progress", "stop"])
def test_enrollment_cannot_describe_reset_initial_state(change):
    value = enrollment()
    cp = value["initial_checkpoint"]
    if change == "slots": cp["physical_slots"] = 1
    if change == "cost": cp["settled_credit_nano_usd"] = 1
    if change == "initial_count": value["initial_checkpoint"] = checkpoint(2)
    if change == "initial_domain": cp["domain_revision"] = 1
    if change == "progress": cp.update(last_completed=1, next_chapter=2)
    if change == "stop": cp["stop_reasons"] = ["AMBIGUOUS"]
    with pytest.raises(w.WitnessContractError): w.Enrollment.from_value(value)


def test_json_resource_and_native_type_bounds():
    cycle = []; cycle.append(cycle)
    for value in ([0] * 65, {str(n): 0 for n in range(65)}, {1: "bad"}, "x" * 257,
                  "\ud800", cycle, {"bad": object()}, {"bytes": b"raw"}, {"float": -0.0}):
        with pytest.raises(w.WitnessContractError): w.canonical_bytes(value)
    assert w.canonical_bytes({"null": None}) == b'{"null":null}'


@pytest.mark.parametrize("changes,status", [({"enrollment": None}, "ENROLLMENT_MISSING"),
    ({"local": None}, "LOCAL_HISTORY_MISSING"), ({"witness": None}, "WITNESS_HISTORY_MISSING"),
    ({"local": {}}, "INCOMPLETE_EVIDENCE"), ({"local": b'{'}, "MALFORMED_EVIDENCE"),
    ({"original_b1_sha256": sha("different")}, "ORIGINAL_BINDING_MISMATCH"),
    ({"original_b1_sha256": "bad"}, "MALFORMED_EVIDENCE")])
def test_absent_and_invalid_evidence(changes, status):
    no_authority(compare(**changes), status)


@pytest.mark.parametrize("left,right,status", [(3, 2, "LOCAL_LEDGER_AHEAD"),
    (2, 3, "WITNESS_AHEAD"), (1, 3, "WITNESS_AHEAD")])
def test_local_ahead_and_consistent_suffix_rollback(left, right, status):
    no_authority(compare(local=local(cp=checkpoint(left)), witness=head(cp=checkpoint(right))), status)


def test_equal_sequence_conflicting_hash_and_checkpoint_divergence():
    no_authority(compare(local=local(cp=checkpoint(2)),
        witness=head(cp=checkpoint(2, ledger_head_sha256=sha("fork")))), "EQUAL_SEQUENCE_DIFFERENT_HASH")
    no_authority(compare(local=local(cp=checkpoint(2)),
        witness=head(cp=checkpoint(2, domain_sha256=sha("changed")))), "CHECKPOINT_DIVERGENCE")
    no_authority(compare(local=local(cp=checkpoint(2, evidence_state="INCOMPLETE"))), "INCOMPLETE_EVIDENCE")


@pytest.mark.parametrize("where", ["pin", "enrollment", "head"])
@pytest.mark.parametrize("field,bad", [("authority_id", "other-authority"), ("authority_epoch", 2)])
def test_authority_and_epoch_mismatch(where, field, bad):
    args = {}
    if where == "pin":
        args["pinned_authority"] = authority(); args["pinned_authority"][field] = bad
    elif where == "enrollment":
        args["enrollment"] = enrollment(); args["enrollment"]["authority"][field] = bad
    else:
        args["witness"] = head(); args["witness"]["authority"][field] = bad
    no_authority(compare(**args), "AUTHORITY_MISMATCH")


@pytest.mark.parametrize("where", ["local", "witness"])
def test_b1_fingerprint_mismatch(where):
    value = local() if where == "local" else head()
    value["original_b1_sha256"] = sha("wrong-b1")
    no_authority(compare(**{where: value}), "ORIGINAL_BINDING_MISMATCH")


def test_historical_signed_receipt_is_never_current_authority():
    # Signature cryptography is deliberately outside this schema. The caller may
    # have accepted a historical signed envelope; only its detached body is here.
    old = head(evidence_kind="HISTORICAL_RECEIPT")
    no_authority(compare(witness=old), "HISTORICAL_RECEIPT_ONLY")
    old["signature"] = "SYNTHETIC-ASSERTION-IS-NOT-VERIFICATION"
    no_authority(compare(witness=old), "MALFORMED_EVIDENCE")


@pytest.mark.parametrize("typed", [False, True])
def test_matching_supplied_values_never_authorize(typed):
    args = {}
    if typed:
        args = dict(enrollment=w.Enrollment.from_value(enrollment()), pinned_authority=w.Authority.from_value(authority()),
            local=w.LocalHistory.from_value(local()), witness=w.WitnessHead.from_value(head()))
    no_authority(compare(**args), "MATCHING_SUPPLIED_VALUES_UNVERIFIED")
    assert not hasattr(w, "send") and not hasattr(w, "enroll") and not hasattr(w, "main")


@pytest.mark.parametrize("field", ["batch_id", "storage_id", "enrollment_id", "grant_sha256",
    "max_api_calls", "max_credit_nano_usd", "initial_runtime_sha256", "policy_sha256"])
def test_context_recreation_cannot_renew_or_alias_supplied_enrollment(field):
    value = enrollment()
    if field in {"max_api_calls", "max_credit_nano_usd"}: value[field] += 1
    elif field.endswith("sha256"): value[field] = sha("replacement")
    else: value[field] = "replacement"
    no_authority(compare(enrollment=value), "ENROLLMENT_IDENTITY_MISMATCH")
    args = successor_args(); args["enrollment"] = value
    no_authority(w.compare_successor(**args), "ENROLLMENT_IDENTITY_MISMATCH")


def test_matching_asserted_reset_is_only_unverified_not_registry_proof():
    # A caller can fabricate an entirely new consistent context. Pure predicates
    # cannot establish global grant uniqueness and must not claim to do so.
    en = enrollment(max_api_calls=5)
    no_authority(compare(enrollment=en, local=local(en), witness=head(en)), "MATCHING_SUPPLIED_VALUES_UNVERIFIED")


def test_checkpoint_limits_and_stopped_actual_overrun():
    cp = checkpoint(2, physical_slots=5)
    no_authority(compare(local=local(cp=cp), witness=head(cp=cp)), "LIMIT_EXCEEDED")
    cp = checkpoint(2, settled_credit_nano_usd=1_000_000_001)
    no_authority(compare(local=local(cp=cp), witness=head(cp=cp)), "LIMIT_EXCEEDED")
    cp["stop_reasons"] = ["VERIFIED_OVERRUN"]
    no_authority(compare(local=local(cp=cp), witness=head(cp=cp)), "MATCHING_SUPPLIED_VALUES_UNVERIFIED")


def test_expected_successor_is_only_structurally_compatible():
    no_authority(w.compare_successor(**successor_args()), "SUCCESSOR_VALUES_COMPATIBLE_UNVERIFIED")


@pytest.mark.parametrize("change,status", [("stale", "STALE_EXPECTED_HEAD"), ("gap", "SEQUENCE_GAP"),
    ("ledger_gap", "SEQUENCE_GAP"), ("ledger_parent", "LEDGER_PREDECESSOR_MISMATCH"),
    ("conflicting_id", "TRANSITION_ID_CONFLICT"), ("historical", "HISTORICAL_RECEIPT_ONLY"),
    ("missing", "WITNESS_HISTORY_MISSING"), ("incomplete", "INCOMPLETE_EVIDENCE"),
    ("unsupported", "UNSUPPORTED_SCHEMA")])
def test_expected_head_failure_conditions(change, status):
    args = successor_args()
    after = args["proposed"]
    if change == "stale": after["previous_head_sha256"] = sha("stale")
    if change == "gap": after["witness_sequence"] = 2
    if change == "ledger_gap": after["checkpoint"] = checkpoint(3)
    if change == "ledger_parent": after["checkpoint"]["ledger_previous_sha256"] = sha("wrong")
    if change == "conflicting_id": after["transition_id"] = args["current"]["transition_id"]
    if change == "historical": args["current"]["evidence_kind"] = "HISTORICAL_RECEIPT"
    if change == "missing": args["current"] = None
    if change == "incomplete": after["checkpoint"]["evidence_state"] = "INCOMPLETE"
    if change == "unsupported": after["revision"] = "future"
    no_authority(w.compare_successor(**args), status)


def test_unique_successor_requires_real_atomic_service():
    args = successor_args()
    other = copy.deepcopy(args)
    other["proposed"]["transition_id"] = "competing-successor"
    other["proposed"]["checkpoint"]["ledger_head_sha256"] = sha("competing-event")
    # Both candidates satisfy necessary predicates; neither has been accepted.
    for candidate in (args, other):
        no_authority(w.compare_successor(**candidate), "SUCCESSOR_VALUES_COMPATIBLE_UNVERIFIED")
    other["current"] = args["proposed"]
    no_authority(w.compare_successor(**other), "STALE_EXPECTED_HEAD")


def test_duplicate_transition_lookup_is_unverified_and_conflicts_rejected():
    args = successor_args()
    args["retained_transition"] = copy.deepcopy(args["proposed"])
    no_authority(w.compare_successor(**args), "DUPLICATE_SUPPLIED_TRANSITION_UNVERIFIED")
    args["retained_transition"]["checkpoint"]["domain_sha256"] = sha("altered")
    no_authority(w.compare_successor(**args), "TRANSITION_ID_CONFLICT")


@pytest.mark.parametrize("field", ["physical_slots", "settled_credit_nano_usd", "prompt_tokens",
    "completion_tokens", "domain_revision", "last_completed", "audited_through", "stop_reasons"])
def test_successor_cannot_reset_retained_counters_or_stop(field):
    args = successor_args()
    a = checkpoint(2, physical_slots=1, settled_credit_nano_usd=10, prompt_tokens=20,
        completion_tokens=10, domain_revision=1, last_completed=1, next_chapter=2,
        audited_through=1, stop_reasons=["AMBIGUOUS"])
    args["current"] = head(cp=a, witness_sequence=1, previous_head_sha256=sha("prior"))
    b = copy.deepcopy(a)
    b.update(ledger_event_count=3, ledger_last_sequence=2, ledger_head_sha256=sha("third"),
             ledger_previous_sha256=a["ledger_head_sha256"])
    b[field] = [] if field == "stop_reasons" else 0
    if field == "last_completed": b.update(next_chapter=1, audited_through=0)
    args["proposed"] = head(cp=b, witness_sequence=2,
        previous_head_sha256=w.WitnessHead.from_value(args["current"]).sha256, transition_id="next")
    no_authority(w.compare_successor(**args), "STOP_REGRESSION" if field == "stop_reasons" else "CHECKPOINT_REGRESSION")


@pytest.mark.usefixtures("synthetic_goat_hard_bound")
def test_actual_b1_float_fingerprint_and_schema_one_ledger_unchanged(tmp_path):
    from test_goat_batch_plan import fixture_novel, plan
    import production_goat_batch_ledger as ledger
    from production_run_lock import production_run_lock
    from controlled_trial_plan import _canonical_hash
    project = fixture_novel(tmp_path)
    binding = plan(project, max_credit_equivalent_usd=1.0000000001)["authorization_binding"]
    assert type(binding["max_credit_equivalent_usd"]) is float
    fingerprint = _canonical_hash(binding)
    assert fingerprint == ledger.digest(binding)
    old_bytes = ledger.canonical(binding)
    en = enrollment(original_b1_sha256=fingerprint, max_credit_nano_usd=ledger.parent_ceiling(binding))
    assert w.Enrollment.from_value(en).to_dict()["original_b1_sha256"] == fingerprint
    with pytest.raises(w.WitnessContractError): w.canonical_bytes(binding)
    assert ledger.canonical(binding) == old_bytes and _canonical_hash(binding) == fingerprint
    with production_run_lock(project.root):
        obj = ledger.BatchLedger.create_locked(project.root, "synthetic-unenrolled", binding)
        before = {p.name: p.read_bytes() for p in obj.path.iterdir()}
        assert json.loads(before["00000000.json"])["schema"] == 1
        no_authority(compare(enrollment=None), "ENROLLMENT_MISSING")
        assert {p.name: p.read_bytes() for p in obj.path.iterdir()} == before
        assert not (project.data_dir / "goat_batch_enrollment").exists()
        assert ledger.BatchLedger.open_locked(project.root, "synthetic-unenrolled", binding).identity == obj.identity


def test_fresh_import_and_operations_inert_under_audit_guard(tmp_path):
    script = r'''
import sys, os, json
active = True
def guard(event, args):
    if not active: return
    if event.startswith('socket.') or (event == 'ctypes.dlsym' and 'Crypt' in str(args)):
        raise AssertionError('network or DPAPI')
    if event in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir'):
        raise AssertionError('filesystem mutation')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0]).lower()
        if path.endswith(('.env', '.dpapi')): raise AssertionError('credential access')
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC): raise AssertionError('write')
    if event == 'import' and args[0].split('.')[0] in ('openai', 'dotenv', 'production_goat_runtime',
        'command_code_provider_credentials', 'production_goat_batch_ledger'):
        raise AssertionError('execution dependency')
sys.addaudithook(guard)
sys.path.insert(0, sys.argv[1])
import production_goat_batch_witness as w
r = w.compare_checkpoints(enrollment=None, pinned_authority=None, original_b1_sha256=None, local=None, witness=None)
assert r.status == 'ENROLLMENT_MISSING' and r.send_authorized is False
assert not any(name in sys.modules for name in ('openai', 'dotenv', 'command_code_provider_credentials'))
assert not hasattr(w, 'main')
print('INERT_NO_SEND network=0 credential=0 DPAPI=0 writes=0')
active = False  # Allow the independent external guard to retain its exit counters.
'''
    env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(w.__file__).parent)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'INERT_NO_SEND network=0 credential=0 DPAPI=0 writes=0'
    assert not list(tmp_path.iterdir())
