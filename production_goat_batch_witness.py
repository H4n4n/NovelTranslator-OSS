"""B2B2B-1 structural witness contracts only: no IO, enrollment or send authority.

Every value, including an Authority, is supplied evidence. Parsing and matching
cannot establish authenticity, freshness, registry uniqueness or financial truth.
B1 fingerprints are opaque existing digests, never reserialized by this module.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import ClassVar, Protocol

SCHEMA_VERSION = 1
REVISION = "goat-batch-witness-contract-b2b2b1-v1"
MAX_RECORD_BYTES = 32_768
MAX_INTEGER = 2**63 - 1
MAX_CREDIT_NANO_USD = 1_000_000_000_000_000
_HASH_DOMAIN = b"NovelTranslator/B2B2B1/structural-record/v1\x00"
_STOP_REASONS = frozenset({"OPERATOR_PAUSE", "AMBIGUOUS", "UNVALIDATED_EVIDENCE",
    "JOURNAL_AHEAD", "VERIFIED_OVERRUN", "TOKEN_LIMIT_EXCEEDED", "EVIDENCE_DIVERGENCE"})


class WitnessContractError(ValueError):
    """Fixed diagnostic code; never echo potentially sensitive supplied data."""
    def __init__(self, status="MALFORMED_EVIDENCE"):
        self.status = status
        super().__init__(status)


def _require(condition, status="MALFORMED_EVIDENCE"):
    if not condition:
        raise WitnessContractError(status)


def canonical_bytes(value):
    """Bounded strict new-contract JSON. Deliberately NOT B1 serialization."""
    nodes = 0
    def check(item, depth=0):
        nonlocal nodes
        nodes += 1
        _require(depth <= 12 and nodes <= 2048)
        if type(item) is dict:
            _require(len(item) <= 64 and all(type(k) is str for k in item))
            for key, child in item.items():
                _require(len(key) <= 128)
                check(child, depth + 1)
        elif type(item) is list:
            _require(len(item) <= 64)
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            _require(len(item) <= 256)
        elif type(item) is int:
            _require(0 <= item <= MAX_INTEGER)
        else:
            _require(item is None or type(item) is bool)
    check(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise WitnessContractError() from exc
    _require(len(raw) <= MAX_RECORD_BYTES)
    return raw


def _decode(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_RECORD_BYTES)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    def invalid(_):
        raise WitnessContractError()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_float=invalid, parse_constant=invalid)
        _require(canonical_bytes(value) == raw, "NONCANONICAL_EVIDENCE")
        return value
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, WitnessContractError):
            raise
        raise WitnessContractError() from exc


def _fields(value, names):
    _require(type(value) is dict and set(value) == set(names.split()))


def _version(value, names):
    _require(type(value) is dict)
    if "schema_version" not in value or "revision" not in value:
        raise WitnessContractError("INCOMPLETE_EVIDENCE")
    _require(type(value["schema_version"]) is int)
    _require(value["schema_version"] == SCHEMA_VERSION and value["revision"] == REVISION,
             "UNSUPPORTED_SCHEMA")
    _fields(value, "schema_version revision " + names)


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    _require(type(value) is int and minimum <= value <= maximum)


def _sha(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _identifier(value):
    _require(type(value) is str and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is not None)


def _authority(value):
    _version(value, "authority_id authority_epoch")
    _identifier(value["authority_id"])
    _integer(value["authority_epoch"], 1)


def _checkpoint(value):
    _version(value, "ledger_schema ledger_event_count ledger_last_sequence ledger_head_sha256 "
        "ledger_previous_sha256 evidence_state physical_slots settled_credit_nano_usd "
        "outstanding_exposure_nano_usd prompt_tokens completion_tokens domain_revision "
        "domain_sha256 journal_inventory_sha256 last_completed next_chapter audited_through "
        "publication_proof_sha256 stop_reasons")
    _require(type(value["ledger_schema"]) is int)
    _require(value["ledger_schema"] == 1, "UNSUPPORTED_SCHEMA")
    _integer(value["ledger_event_count"], 1)
    _integer(value["ledger_last_sequence"])
    _require(value["ledger_last_sequence"] == value["ledger_event_count"] - 1)
    _sha(value["ledger_head_sha256"])
    if value["ledger_event_count"] == 1:
        _require(value["ledger_previous_sha256"] is None)
    else:
        _sha(value["ledger_previous_sha256"])
    _require(value["evidence_state"] in ("COMPLETE", "INCOMPLETE"))
    for key in ("physical_slots", "settled_credit_nano_usd", "outstanding_exposure_nano_usd",
                "prompt_tokens", "completion_tokens", "domain_revision", "last_completed", "audited_through"):
        _integer(value[key])
    _integer(value["next_chapter"], 1)
    _require(value["next_chapter"] == value["last_completed"] + 1
             and value["audited_through"] <= value["last_completed"])
    for key in ("domain_sha256", "journal_inventory_sha256"):
        _sha(value[key])
    if value["publication_proof_sha256"] is not None:
        _sha(value["publication_proof_sha256"])
    reasons = value["stop_reasons"]
    _require(type(reasons) is list and all(type(r) is str and r in _STOP_REASONS for r in reasons))
    _require(reasons == sorted(set(reasons)))


def _enrollment(value):
    _version(value, "authority enrollment_id grant_sha256 original_b1_sha256 novel_id storage_id "
        "batch_id ledger_identity_sha256 initial_runtime_sha256 policy_sha256 initial_checkpoint "
        "initial_chapter end_chapter max_api_calls max_credit_nano_usd execution_mode")
    _authority(value["authority"])
    for key in ("enrollment_id", "novel_id", "storage_id", "batch_id"):
        _identifier(value[key])
    for key in ("grant_sha256", "original_b1_sha256", "ledger_identity_sha256",
                "initial_runtime_sha256", "policy_sha256"):
        _sha(value[key])
    _integer(value["initial_chapter"], 1, 2**31 - 1)
    _integer(value["end_chapter"], value["initial_chapter"], 2**31 - 1)
    _require(value["end_chapter"] - value["initial_chapter"] < 1000)
    _integer(value["max_api_calls"], 1, 1_000_000)
    _integer(value["max_credit_nano_usd"], 1, MAX_CREDIT_NANO_USD)
    _require(value["execution_mode"] == "NO_SEND", "UNSUPPORTED_SCHEMA")
    initial = value["initial_checkpoint"]
    _checkpoint(initial)
    _require(initial["ledger_event_count"] == 1 and initial["evidence_state"] == "COMPLETE"
        and initial["next_chapter"] == value["initial_chapter"]
        and all(initial[key] == 0 for key in ("physical_slots", "settled_credit_nano_usd",
            "outstanding_exposure_nano_usd", "prompt_tokens", "completion_tokens", "domain_revision"))
        and initial["stop_reasons"] == [])


def _local(value):
    _version(value, "enrollment_sha256 original_b1_sha256 checkpoint")
    _sha(value["enrollment_sha256"])
    _sha(value["original_b1_sha256"])
    _checkpoint(value["checkpoint"])


def _head(value):
    _version(value, "authority enrollment_sha256 original_b1_sha256 witness_sequence "
        "previous_head_sha256 transition_id evidence_kind checkpoint")
    _authority(value["authority"])
    _sha(value["enrollment_sha256"])
    _sha(value["original_b1_sha256"])
    _integer(value["witness_sequence"])
    if value["witness_sequence"] == 0:
        _require(value["previous_head_sha256"] is None)
    else:
        _sha(value["previous_head_sha256"])
    _identifier(value["transition_id"])
    _require(value["evidence_kind"] in ("SUPPLIED_HEAD", "HISTORICAL_RECEIPT"))
    _checkpoint(value["checkpoint"])


@dataclass(frozen=True, slots=True)
class _Record:
    """Only canonical bytes are retained; decoded nested values are detached."""
    raw: bytes
    _kind: ClassVar[str]

    def __post_init__(self):
        value = _decode(self.raw)
        {"authority": _authority, "checkpoint": _checkpoint, "enrollment": _enrollment,
         "local": _local, "head": _head}[self._kind](value)

    @classmethod
    def from_value(cls, value):
        return cls(canonical_bytes(value))

    def to_dict(self):
        return json.loads(self.raw)

    @property
    def sha256(self):
        return hashlib.sha256(_HASH_DOMAIN + self._kind.encode("ascii") + b"\x00" + self.raw).hexdigest()


class Authority(_Record):
    __slots__ = ()
    _kind = "authority"


class Checkpoint(_Record):
    __slots__ = ()
    _kind = "checkpoint"


class Enrollment(_Record):
    """Description of supplied existing enrollment; construction does not enroll."""
    __slots__ = ()
    _kind = "enrollment"


class LocalHistory(_Record):
    __slots__ = ()
    _kind = "local"


class WitnessHead(_Record):
    __slots__ = ()
    _kind = "head"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A diagnostic is never truthy authority, including on structural equality."""
    status: str

    @property
    def send_authorized(self):
        return False

    @property
    def paid_execution_authorized(self):
        return False

    @property
    def authority_verified(self):
        return False

    @property
    def freshness_verified(self):
        return False

    @property
    def transition_committed(self):
        return False

    def __bool__(self):
        raise TypeError("Witness diagnostics are not authorization predicates")


def _record(value, cls):
    if type(value) is cls:
        return value.to_dict()
    if type(value) is dict:
        return cls.from_value(value).to_dict()
    return cls(value).to_dict()  # strict canonical bytes; no duck typing


def _bound(enrollment, authority, original, *records):
    _sha(original)
    _require(enrollment["authority"] == authority, "AUTHORITY_MISMATCH")
    _require(enrollment["original_b1_sha256"] == original, "ORIGINAL_BINDING_MISMATCH")
    identity = Enrollment.from_value(enrollment).sha256
    for record in records:
        if "authority" in record:
            _require(record["authority"] == authority, "AUTHORITY_MISMATCH")
        _require(record["original_b1_sha256"] == original, "ORIGINAL_BINDING_MISMATCH")
        _require(record["enrollment_sha256"] == identity, "ENROLLMENT_IDENTITY_MISMATCH")
        checkpoint = record["checkpoint"]
        _require(checkpoint["evidence_state"] == "COMPLETE", "INCOMPLETE_EVIDENCE")
        _require(enrollment["initial_chapter"] - 1 <= checkpoint["last_completed"] <= enrollment["end_chapter"],
                 "CHECKPOINT_DIVERGENCE")
        used = checkpoint["settled_credit_nano_usd"] + checkpoint["outstanding_exposure_nano_usd"]
        _require(checkpoint["physical_slots"] <= enrollment["max_api_calls"]
                 and (used <= enrollment["max_credit_nano_usd"] or checkpoint["stop_reasons"]),
                 "LIMIT_EXCEEDED")
        if checkpoint["ledger_event_count"] == 1:
            _require(checkpoint == enrollment["initial_checkpoint"], "CHECKPOINT_DIVERGENCE")


def compare_checkpoints(*, enrollment, pinned_authority, original_b1_sha256, local, witness):
    """Compare supplied values only. None explicitly means absent, not empty."""
    try:
        if enrollment is None:
            return Diagnostic("ENROLLMENT_MISSING")
        en = _record(enrollment, Enrollment)
        authority = _record(pinned_authority, Authority)
        _bound(en, authority, original_b1_sha256)
        # Validate every present side before reporting an absent counterpart.
        loc = None if local is None else _record(local, LocalHistory)
        remote = None if witness is None else _record(witness, WitnessHead)
        _bound(en, authority, original_b1_sha256, *[r for r in (loc, remote) if r is not None])
        if loc is None:
            return Diagnostic("LOCAL_HISTORY_MISSING")
        if remote is None:
            return Diagnostic("WITNESS_HISTORY_MISSING")
        if remote["evidence_kind"] == "HISTORICAL_RECEIPT":
            return Diagnostic("HISTORICAL_RECEIPT_ONLY")
        a, b = loc["checkpoint"], remote["checkpoint"]
        if a["ledger_event_count"] > b["ledger_event_count"]:
            return Diagnostic("LOCAL_LEDGER_AHEAD")
        if a["ledger_event_count"] < b["ledger_event_count"]:
            return Diagnostic("WITNESS_AHEAD")
        if a["ledger_head_sha256"] != b["ledger_head_sha256"]:
            return Diagnostic("EQUAL_SEQUENCE_DIFFERENT_HASH")
        if a != b:
            return Diagnostic("CHECKPOINT_DIVERGENCE")
        return Diagnostic("MATCHING_SUPPLIED_VALUES_UNVERIFIED")
    except WitnessContractError as exc:
        return Diagnostic(exc.status)


def compare_successor(*, enrollment, pinned_authority, original_b1_sha256,
                      current, proposed, retained_transition=None):
    """Necessary append conditions only; does not append, authenticate or ACK.

    retained_transition is an optional supplied ID lookup, not a complete registry.
    Each proposed successor describes exactly one local ledger event. Financial
    and domain evidence truth, full ancestry and global uniqueness remain unproved.
    """
    try:
        if enrollment is None:
            return Diagnostic("ENROLLMENT_MISSING")
        en = _record(enrollment, Enrollment)
        authority = _record(pinned_authority, Authority)
        _bound(en, authority, original_b1_sha256)
        if current is None:
            return Diagnostic("WITNESS_HISTORY_MISSING")
        before, after = _record(current, WitnessHead), _record(proposed, WitnessHead)
        records = [before, after]
        retained = None if retained_transition is None else _record(retained_transition, WitnessHead)
        if retained is not None:
            records.append(retained)
        _bound(en, authority, original_b1_sha256, *records)
        _require(all(r["evidence_kind"] == "SUPPLIED_HEAD" for r in records), "HISTORICAL_RECEIPT_ONLY")
        for seen in (before, retained):
            if seen is not None and seen["transition_id"] == after["transition_id"]:
                _require(seen == after, "TRANSITION_ID_CONFLICT")
                return Diagnostic("DUPLICATE_SUPPLIED_TRANSITION_UNVERIFIED")
        if retained is not None:
            _require(retained["transition_id"] == after["transition_id"], "MALFORMED_EVIDENCE")
        _require(after["previous_head_sha256"] == WitnessHead.from_value(before).sha256,
                 "STALE_EXPECTED_HEAD")
        _require(after["witness_sequence"] == before["witness_sequence"] + 1, "SEQUENCE_GAP")
        a, b = before["checkpoint"], after["checkpoint"]
        _require(b["ledger_event_count"] == a["ledger_event_count"] + 1, "SEQUENCE_GAP")
        _require(b["ledger_previous_sha256"] == a["ledger_head_sha256"], "LEDGER_PREDECESSOR_MISMATCH")
        for key in ("physical_slots", "settled_credit_nano_usd", "prompt_tokens", "completion_tokens",
                    "domain_revision", "last_completed", "audited_through"):
            _require(b[key] >= a[key], "CHECKPOINT_REGRESSION")
        _require(set(a["stop_reasons"]) <= set(b["stop_reasons"]), "STOP_REGRESSION")
        return Diagnostic("SUCCESSOR_VALUES_COMPATIBLE_UNVERIFIED")
    except WitnessContractError as exc:
        return Diagnostic(exc.status)


class WitnessClient(Protocol):
    """Future backend contract, NOT an implemented or qualified trusted client.

    read_current must pin authority/epoch, verify complete retained enrollment
    and history, and authenticate a fresh challenge-bound head. A cached signed
    receipt is insufficient. Returned bytes need future backend-specific envelope
    verification; merely parsing WitnessHead cannot perform that verification.

    compare_and_append must atomically validate expected head, unique ID and
    canonical evidence, accept one successor without gaps/forks, retain it, and
    acknowledge its exact identity with replay-resistant challenge binding. It
    must independently validate the evidence required by its advertised financial
    and domain guarantees, preserve original caps, and reject stale/conflicting
    proposals. Incomplete/ambiguous acknowledgement stops without automatic retry,
    repair, new enrollment, rollback or renewed allowance. Neither method grants
    physical send authorization. No enrollment-creation method is supplied here.
    """
    def read_current(self, *, enrollment: Enrollment, challenge: bytes) -> bytes:
        ...

    def compare_and_append(self, *, enrollment: Enrollment, expected: WitnessHead,
                           proposed: WitnessHead, canonical_evidence: bytes,
                           challenge: bytes) -> bytes:
        ...
