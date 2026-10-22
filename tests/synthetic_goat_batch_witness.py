"""UNTRUSTED B2B2B-2 simulator. Test-owned temporary stores only; never authority.

No production imports this module. SQLite serializes synthetic enrollment/CAS;
its entire history can be forged or rolled back by the local owner. See
docs/GOAT_BATCH_SYNTHETIC_WITNESS.md for the independent-service gaps.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

import production_goat_batch_witness as w

REVISION = "synthetic-witness-b2b2b2-v1"
_DOMAIN = b"NovelTranslator/B2B2B2/SYNTHETIC-ledger/v1\x00"
MAX_HISTORY = 1024
MAX_ENROLLMENTS = 64


class SyntheticRefusal(ValueError):
    """Fixed codes only; supplied data is never included in diagnostics."""


def require(condition, code):
    if not condition:
        raise SyntheticRefusal(code)


def _fault(stage):
    """Test fault injection seam. No retry or repair on either side of COMMIT."""


def _record(value, cls):
    require(type(value) is cls, "EXACT_CONTRACT_REQUIRED")
    return cls(value.raw)


def _challenge(value):
    require(type(value) is bytes and 16 <= len(value) <= 64, "INVALID_CHALLENGE")
    return hashlib.sha256(value).hexdigest()


def _path(root):
    root = Path(root).resolve()
    # The zero-provider wrapper relocates TEMP for children; its explicit
    # disposable basetemp is a sibling of that new temp directory.
    roots = [Path(tempfile.gettempdir()).resolve()]
    roots += [Path(p).resolve() for p in os.environ.get("NOVELTRANSLATOR_GUARD_ALLOWED_ROOTS", "").split(os.pathsep) if p]
    require(any(candidate in root.parents for candidate in roots), "TEST_TEMP_ROOT_REQUIRED")
    repo = Path(__file__).resolve().parents[1]
    require(root != repo and repo not in root.parents, "TEST_TEMP_ROOT_REQUIRED")
    require(root.is_dir(), "TEST_TEMP_ROOT_REQUIRED")
    path = root / "synthetic-witness.sqlite3"
    require(not path.is_symlink(), "STORE_UNAVAILABLE")
    return path


def _keys(en):
    return tuple(en[k] for k in ("enrollment_id", "grant_sha256", "original_b1_sha256",
        "ledger_identity_sha256")) + ("/".join(en[k] for k in ("novel_id", "storage_id", "batch_id")),)


def genesis(enrollment):
    en = _record(enrollment, w.Enrollment).to_dict()
    return w.WitnessHead.from_value(dict(schema_version=1, revision=w.REVISION,
        authority=en["authority"], enrollment_sha256=enrollment.sha256,
        original_b1_sha256=en["original_b1_sha256"], witness_sequence=0,
        previous_head_sha256=None, transition_id="synthetic-genesis", evidence_kind="SUPPLIED_HEAD",
        checkpoint=en["initial_checkpoint"]))


def _evidence(raw):
    # Reuse the bounded strict canonical decoder; never change B1 serialization.
    value = w._decode(raw)
    require(type(value) is dict and value.get("synthetic_only") is True, "SYNTHETIC_EVIDENCE_REQUIRED")
    action = value.get("action")
    fields = {"synthetic_only", "action"}
    if action == "RESERVE":
        fields |= {"operation_id", "amount_nano_usd"}
    elif action == "SETTLE":
        fields |= {"operation_id", "amount_nano_usd", "prompt_tokens", "completion_tokens"}
    elif action == "STOP":
        fields |= {"reason"}
        require(type(value.get("reason")) is str and value["reason"] in w._STOP_REASONS, "INVALID_STOP")
    else:
        raise SyntheticRefusal("UNSUPPORTED_SYNTHETIC_ACTION")
    require(set(value) == fields, "INVALID_SYNTHETIC_EVIDENCE")
    if action != "STOP":
        w._identifier(value["operation_id"])
        w._integer(value["amount_nano_usd"], maximum=w.MAX_CREDIT_NANO_USD)
    if action == "SETTLE":
        w._integer(value["prompt_tokens"])
        w._integer(value["completion_tokens"])
    return value


def _successor(enrollment, before, transition_id, raw, operations):
    """Only toy reserve/settle/stop arithmetic. No domain or journal truth."""
    en, old = enrollment.to_dict(), before.to_dict()
    cp = old["checkpoint"]
    event = _evidence(raw)
    action = event["action"]
    if action == "RESERVE":
        require(not cp["stop_reasons"], "STOP_LATCHED")
        require(event["operation_id"] not in operations, "OPERATION_ALREADY_RESERVED")
        require(cp["physical_slots"] < en["max_api_calls"], "SLOT_CAP_EXCEEDED")
        amount = event["amount_nano_usd"]
        require(cp["settled_credit_nano_usd"] + cp["outstanding_exposure_nano_usd"] + amount
            <= en["max_credit_nano_usd"], "CREDIT_CAP_EXCEEDED")
        operations[event["operation_id"]] = (amount, False)
        cp["physical_slots"] += 1
        cp["outstanding_exposure_nano_usd"] += amount
    elif action == "SETTLE":
        op = operations.get(event["operation_id"])
        require(op is not None and not op[1], "SETTLEMENT_WITHOUT_OPEN_RESERVATION")
        operations[event["operation_id"]] = (op[0], True)
        cp["outstanding_exposure_nano_usd"] -= op[0]
        cp["settled_credit_nano_usd"] += event["amount_nano_usd"]
        cp["prompt_tokens"] += event["prompt_tokens"]
        cp["completion_tokens"] += event["completion_tokens"]
        if event["amount_nano_usd"] > op[0]:
            cp["stop_reasons"] = sorted(set(cp["stop_reasons"]) | {"VERIFIED_OVERRUN"})
    else:
        cp["stop_reasons"] = sorted(set(cp["stop_reasons"]) | {event["reason"]})
    cp["ledger_event_count"] += 1
    cp["ledger_last_sequence"] += 1
    cp["ledger_previous_sha256"] = cp["ledger_head_sha256"]
    cp["ledger_head_sha256"] = hashlib.sha256(_DOMAIN + bytes.fromhex(cp["ledger_previous_sha256"]) + raw).hexdigest()
    proposed = w.WitnessHead.from_value(dict(old, checkpoint=cp,
        witness_sequence=old["witness_sequence"] + 1, previous_head_sha256=before.sha256,
        transition_id=transition_id))
    diagnostic = w.compare_successor(enrollment=enrollment, pinned_authority=w.Authority.from_value(en["authority"]),
        original_b1_sha256=en["original_b1_sha256"], current=before, proposed=proposed)
    require(diagnostic.status == "SUCCESSOR_VALUES_COMPATIBLE_UNVERIFIED", diagnostic.status)
    return proposed


def _ack(operation, enrollment, head, evidence, challenge, *, historical=False):
    _record(enrollment, w.Enrollment)
    _record(head, w.WitnessHead)
    require(type(evidence) is bytes, "INVALID_SYNTHETIC_EVIDENCE")
    return w.canonical_bytes(dict(revision=REVISION, synthetic_only=True, operation=operation,
        enrollment_sha256=enrollment.sha256, head_sha256=head.sha256,
        evidence_sha256=hashlib.sha256(evidence).hexdigest(), challenge_sha256=_challenge(challenge),
        kind="HISTORICAL_RECEIPT" if historical else "SIMULATED_CURRENT", head=head.to_dict()))


def qualify_ack(raw, *, operation, enrollment, head, evidence, challenge):
    """Exact synthetic envelope comparison only. Never proves freshness/trust."""
    try:
        require(type(raw) is bytes and raw == _ack(operation, enrollment, head, evidence, challenge), "SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")
        return w.Diagnostic("SYNTHETIC_MATCH_UNVERIFIED_NO_SEND")
    except (SyntheticRefusal, w.WitnessContractError):
        return w.Diagnostic("SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")


def probe_append(client, *, enrollment, expected, proposed, canonical_evidence, challenge):
    """One simulated attempt. Lost/invalid ACK never causes retry or authority."""
    try:
        raw = client.compare_and_append(enrollment=enrollment, expected=expected, proposed=proposed,
            canonical_evidence=canonical_evidence, challenge=challenge)
    except (SyntheticRefusal, w.WitnessContractError, OSError):
        return w.Diagnostic("SYNTHETIC_ACK_AMBIGUOUS_NO_SEND")
    return qualify_ack(raw, operation="APPEND", enrollment=enrollment, head=proposed,
        evidence=canonical_evidence, challenge=challenge)


class SyntheticWitness:
    """Disposable local simulator; deliberately no real WitnessClient adapter."""
    def __init__(self, root, pinned_authority):
        self.path = _path(root)
        self.authority = _record(pinned_authority, w.Authority)
        with self._transaction() as db:
            require(db.execute("SELECT raw FROM authority").fetchall() == [(self.authority.raw,)],
                "AUTHORITY_MISMATCH")
            self._registry(db)

    @classmethod
    def create(cls, root, pinned_authority):
        path = _path(root)
        authority = _record(pinned_authority, w.Authority)
        require(not any(path.parent.iterdir()), "EMPTY_TEST_ROOT_REQUIRED")
        with path.open("xb"):
            pass  # Explicit one-time creation. Empty/corrupt survivors are never reset.
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.executescript("""
                PRAGMA synchronous=FULL;
                BEGIN IMMEDIATE;
                CREATE TABLE authority(raw BLOB NOT NULL);
                CREATE TABLE enrollments(digest TEXT PRIMARY KEY, raw BLOB NOT NULL,
                    enrollment_id TEXT UNIQUE, grant_digest TEXT UNIQUE, b1_digest TEXT UNIQUE,
                    ledger_digest TEXT UNIQUE, scope TEXT UNIQUE);
                CREATE TABLE transitions(enrollment TEXT NOT NULL, sequence INTEGER NOT NULL,
                    transition_id TEXT NOT NULL, raw BLOB NOT NULL, evidence BLOB NOT NULL,
                    PRIMARY KEY(enrollment, sequence), UNIQUE(enrollment, transition_id));
            """)
            db.execute("INSERT INTO authority VALUES (?)", (authority.raw,))
            db.commit()
        finally:
            db.close()
        return cls(root, authority)

    @contextmanager
    def _transaction(self):
        db = None
        try:
            # mode=rw is essential: missing history must NEVER recreate a store.
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0,
                isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            require(db.execute("SELECT raw FROM authority").fetchall() == [(self.authority.raw,)],
                "AUTHORITY_MISMATCH")
            yield db
            _fault("before_commit")
            db.commit()
            _fault("after_commit")
        except sqlite3.Error as exc:
            raise SyntheticRefusal("STORE_UNAVAILABLE") from exc
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    def _registry(self, db):
        rows = db.execute("SELECT * FROM enrollments LIMIT ?", (MAX_ENROLLMENTS + 1,)).fetchall()
        require(len(rows) <= MAX_ENROLLMENTS, "SYNTHETIC_STORE_LIMIT")
        for digest, raw, *keys in rows:
            enrollment = w.Enrollment(raw)
            require(digest == enrollment.sha256 and tuple(keys) == _keys(enrollment.to_dict()), "REGISTRY_DIVERGENCE")
            require(enrollment.to_dict()["authority"] == self.authority.to_dict(), "AUTHORITY_MISMATCH")
        require(not db.execute("SELECT 1 FROM transitions WHERE enrollment NOT IN (SELECT digest FROM enrollments)").fetchone(),
            "ORPHAN_HISTORY")
        return rows

    def _history(self, db, enrollment):
        _record(enrollment, w.Enrollment)
        self._registry(db)
        require(db.execute("SELECT raw FROM enrollments WHERE digest=?", (enrollment.sha256,)).fetchone()
            == (enrollment.raw,), "ENROLLMENT_MISSING")
        rows = db.execute("SELECT sequence, transition_id, raw, evidence FROM transitions WHERE enrollment=? ORDER BY sequence LIMIT ?",
            (enrollment.sha256, MAX_HISTORY + 1)).fetchall()
        require(0 < len(rows) <= MAX_HISTORY, "INCOMPLETE_HISTORY")
        head, operations = genesis(enrollment), {}
        for sequence, (ordinal, identifier, raw, evidence) in enumerate(rows):
            require(ordinal == sequence, "HISTORY_GAP")
            if sequence == 0:
                require(evidence == b"" and identifier == head.to_dict()["transition_id"], "GENESIS_DIVERGENCE")
            else:
                head = _successor(enrollment, head, identifier, evidence, operations)
            require(head.raw == raw, "HISTORY_DIVERGENCE")
        return head, operations, rows

    def enroll(self, enrollment, *, challenge):
        _challenge(challenge)
        head = genesis(enrollment)
        require(enrollment.to_dict()["authority"] == self.authority.to_dict(), "AUTHORITY_MISMATCH")
        with self._transaction() as db:
            rows = self._registry(db)
            require(len(rows) < MAX_ENROLLMENTS, "SYNTHETIC_STORE_LIMIT")
            keys = _keys(enrollment.to_dict())
            require(all(not any(a == b for a, b in zip(keys, row[2:])) for row in rows), "ENROLLMENT_ALREADY_EXISTS")
            db.execute("INSERT INTO enrollments VALUES (?, ?, ?, ?, ?, ?, ?)", (enrollment.sha256, enrollment.raw, *keys))
            db.execute("INSERT INTO transitions VALUES (?, ?, ?, ?, ?)", (enrollment.sha256, 0,
                head.to_dict()["transition_id"], head.raw, b""))
            _fault("after_insert")
        return _ack("ENROLL", enrollment, head, b"", challenge)

    def read_current(self, *, enrollment, challenge, retained=None):
        _challenge(challenge)
        with self._transaction() as db:
            head, _, rows = self._history(db, enrollment)
            if retained is not None:
                _record(retained, w.WitnessHead)
                index = retained.to_dict()["witness_sequence"]
                require(index < len(rows) and rows[index][2] == retained.raw, "RETAINED_HEAD_DIVERGENCE")
        return _ack("READ", enrollment, head, b"", challenge)

    def propose(self, *, enrollment, expected, transition_id, canonical_evidence):
        _record(expected, w.WitnessHead)
        with self._transaction() as db:
            current, operations, _ = self._history(db, enrollment)
            require(current.raw == expected.raw, "STALE_EXPECTED_HEAD")
            return _successor(enrollment, expected, transition_id, canonical_evidence, operations)

    def compare_and_append(self, *, enrollment, expected, proposed, canonical_evidence, challenge):
        _challenge(challenge)
        _record(expected, w.WitnessHead)
        _record(proposed, w.WitnessHead)
        _evidence(canonical_evidence)
        with self._transaction() as db:
            current, operations, rows = self._history(db, enrollment)
            identifier = proposed.to_dict()["transition_id"]
            retained = next((row for row in rows if row[1] == identifier), None)
            if retained is not None:
                require(retained[2:] == (proposed.raw, canonical_evidence), "TRANSITION_ID_CONFLICT")
                require(proposed.to_dict()["previous_head_sha256"] == expected.sha256, "STALE_EXPECTED_HEAD")
                # Idempotent lookup is explicitly historical, never a fresh head ACK.
                return _ack("APPEND", enrollment, proposed, canonical_evidence, challenge, historical=True)
            require(len(rows) < MAX_HISTORY, "SYNTHETIC_STORE_LIMIT")
            require(current.raw == expected.raw, "STALE_EXPECTED_HEAD")
            computed = _successor(enrollment, current, identifier, canonical_evidence, operations)
            require(computed.raw == proposed.raw, "PROPOSED_HEAD_DIVERGENCE")
            db.execute("INSERT INTO transitions VALUES (?, ?, ?, ?, ?)", (enrollment.sha256,
                proposed.to_dict()["witness_sequence"], identifier, proposed.raw, canonical_evidence))
            _fault("after_insert")
        return _ack("APPEND", enrollment, proposed, canonical_evidence, challenge)
