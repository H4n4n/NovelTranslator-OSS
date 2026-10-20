"""B2A internal caller-locked lifetime ledger. No transport or paid authority.

Every *_locked call requires production_run_lock(root), including construction.
Validators are trusted integration code, not caller-supplied receipt assertions.
There is deliberately no production journal validator or executable CLI here.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from decimal import Decimal, ROUND_FLOOR, localcontext


UNIT = "GOAT_CREDIT_EQUIVALENT_NANO_USD"
SCALE = 1_000_000_000
BINDING_FIELDS = set("mode novel_id project_root source_language target_language profile "
    "initial_chapter end_chapter chapter_count authorized_next_chapter final_chapter "
    "max_api_calls max_credit_equivalent_usd provider_identity capability_exposure "
    "runtime_identity dependency_policy_sha256 immutable_inputs pricing source_range_sha256 "
    "initial_evidence audit_obligations transition_policy recovery_policy publication_policy".split())


class LedgerError(ValueError):
    """Preserve all evidence and stop; never infer a free send."""


def require(condition, message):
    if not condition:
        raise LedgerError(message)


def canonical(value):
    """B1-compatible canonical UTF-8, with strict JSON-native types."""
    def check(item):
        if type(item) is dict:
            require(all(type(k) is str for k in item), "non-string JSON key")
            for v in item.values():
                check(v)
        elif type(item) is list:
            for v in item:
                check(v)
        else:
            require(type(item) in (str, int, float, bool, type(None)), "non-JSON type")
            if type(item) is float:
                require(math.isfinite(item), "nonfinite number")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_load(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result, "duplicate JSON field")
            result[k] = v
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: require(False, "nonfinite number"))
        require(canonical(value) == raw, "noncanonical or invalid JSON bytes")
        return value
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise LedgerError("invalid ledger JSON") from exc


def fields(value, expected):
    require(type(value) is dict and set(value) == set(expected), "unexpected/missing fields")


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, "invalid exact integer")
    return value


def sha(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "invalid SHA256")
    return value


def safe_path(value):
    require(isinstance(value, (str, Path)), "invalid path")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "absolute non-traversing path required")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            "symlink/reparse path forbidden")
    return path.resolve()


def amount(value, *, exposure=False):
    fields(value, {"unit", "nano_usd", "evidence_sha256"} if exposure else {"unit", "nano_usd"})
    require(value["unit"] == UNIT, "wrong credit unit")
    integer(value["nano_usd"])
    if exposure:
        sha(value["evidence_sha256"])
    return value["nano_usd"]


def parent_ceiling(binding):
    value = binding["max_credit_equivalent_usd"]
    require(type(value) in (int, float) and math.isfinite(value)
            and 0 < value <= 1_000_000, "invalid parent credit ceiling")
    with localcontext() as ctx:
        ctx.prec = 100
        return int((Decimal(str(value)) * SCALE).to_integral_value(rounding=ROUND_FLOOR))


class BatchLedger:
    """Internal API: caller owns the novel lock for the entire object's use.

    There is no lock token assertion or second acquisition. This is cooperative
    serialization, not a security boundary against arbitrary external writers.
    """
    def __init__(self, root, batch_id, binding):
        self.root = safe_path(root)
        require(type(batch_id) is str and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", batch_id),
                "invalid batch ID")
        fields(binding, BINDING_FIELDS)
        self.binding = strict_load(canonical(binding))
        require(binding["mode"] == "goat_bounded_parent_plan_b1", "not B1 binding")
        require(binding["project_root"] == str(self.root), "novel root drift")
        require(binding["source_language"] in {"zh", "ko"} and binding["target_language"] == "en",
                "language drift")
        for key in ("novel_id", "profile"):
            require(type(binding[key]) is str and binding[key].strip(), "missing novel/profile")
        start = integer(binding["initial_chapter"], 1)
        end = integer(binding["end_chapter"], start)
        count = integer(binding["chapter_count"], 1)
        require(count == end - start + 1 and count <= 1000
                and type(binding["authorized_next_chapter"]) is int
                and binding["authorized_next_chapter"] == start, "range drift")
        integer(binding["final_chapter"], end)
        require(integer(binding["max_api_calls"], 1) <= 1_000_000, "send ceiling too large")
        self.ceiling = parent_ceiling(binding)
        policy = binding["provider_identity"]
        require(type(policy) is dict and policy.get("provider") == "command_code_provider"
                and policy.get("wire_model") == "deepseek/deepseek-v4.1-flash", "provider/model drift")
        self.path = safe_path(self.root / "data" / "goat_batch_ledgers" / batch_id)
        self.identity = dict(schema=1, batch_id=batch_id, novel_root=str(self.root),
            ledger_path=str(self.path), authorization_binding=self.binding,
            operating_checkout=str(Path(__file__).resolve().parent),
            original_fingerprint=digest(self.binding), credit_ceiling_nano_usd=self.ceiling)
        self._head = None

    def _check_runtime(self):
        # Reproduce B1's raw-byte manifest without importing its planner or any
        # execution/credential dependencies. Never normalize checkout bytes.
        root = safe_path(Path(__file__).absolute().parent)
        profile = safe_path(root / "profiles" / self.binding["profile"])
        require(profile.is_relative_to(root / "profiles") and profile.is_dir(), "missing profile")
        paths = sorted(root.glob("*.py")) + sorted(p for p in profile.rglob("*") if p.is_file())
        manifest = {}
        for path in paths:
            safe_path(path)
            require(path.is_file(), "runtime file missing")
            manifest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        current = dict(profile=self.binding["profile"], file_count=len(manifest), sha256=digest(manifest))
        require(canonical(current) == canonical(self.binding["runtime_identity"]), "raw operating runtime drift")
        dependency = safe_path(root / "pyproject.toml")
        require(hashlib.sha256(dependency.read_bytes()).hexdigest() == self.binding["dependency_policy_sha256"],
                "dependency policy drift")

    @classmethod
    def create_locked(cls, root, batch_id, binding):
        obj = cls(root, batch_id, binding)
        obj._check_runtime()
        require(not obj.path.exists(), "existing batch must never be recreated")
        obj.path.parent.mkdir(parents=True, exist_ok=True)
        obj.path.mkdir()  # exclusive; interrupted empty directory blocks recreation
        obj._publish(0, "0" * 64, "INITIALIZE", obj.identity)
        obj._read()
        return obj

    @classmethod
    def open_locked(cls, root, batch_id, binding):
        obj = cls(root, batch_id, binding)
        obj._read()  # never mkdir/recreate
        return obj

    def _publish(self, seq, previous, kind, payload):
        value = dict(schema=1, sequence=seq, previous_sha256=previous,
                     kind=kind, payload=copy.deepcopy(payload))
        value["sha256"] = digest(value)
        raw = canonical(value)
        stem = self.path / f"{seq:08d}"
        intent, temp, target = (stem.with_suffix(s) for s in (".intent", ".tmp", ".json"))
        require(not any(p.exists() for p in (intent, temp, target)), "publication collision")
        # Retain even partial intent/temp after interruption: never erase evidence.
        for path in (intent, temp):
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temp, target)

    def _request(self, request):
        fields(request, {"batch_id", "operation_id", "chapter", "journal_path", "journal_identity",
                         "wire_request_sha256", "provider_policy_sha256", "exposure"})
        require(request["batch_id"] == self.identity["batch_id"], "batch drift")
        sha(request["operation_id"])
        sha(request["wire_request_sha256"])
        chapter = integer(request["chapter"], 1)
        require(self.binding["initial_chapter"] <= chapter <= self.binding["end_chapter"], "chapter outside range")
        path = safe_path(request["journal_path"])
        require(str(path) == request["journal_path"] and path.parent ==
                self.root / "data" / "provider_request_journal" and path.suffix == ".json", "journal path drift")
        policy = self.binding["provider_identity"]
        require(request["provider_policy_sha256"] == digest(policy), "policy drift")
        identity = request["journal_identity"]
        require(type(identity) is dict, "missing journal identity")
        expected = dict(operation_id=request["operation_id"], chapter=chapter,
            novel_id=self.binding["novel_id"], model=policy["wire_model"], provider=policy["provider"],
            wire_request_sha256=request["wire_request_sha256"], provider_policy=policy)
        require(all(k in identity and canonical(identity[k]) == canonical(v)
                    for k, v in expected.items()), "journal identity drift")
        amount(request["exposure"], exposure=True)

    def _receipt(self, receipt, request):
        fields(receipt, {"status", "reservation_sha256", "journal_record_sha256", "response_sha256",
                         "usage_sha256", "actual"})
        require(receipt["status"] == "RESPONSE_RECORDED" and
                receipt["reservation_sha256"] == digest(request), "receipt binding drift")
        for key in ("journal_record_sha256", "response_sha256", "usage_sha256"):
            sha(receipt[key])
        amount(receipt["actual"])

    def _apply(self, state, kind, payload):
        operations = state["operations"]
        if kind == "RESERVE":
            self._request(payload)
            op = payload["operation_id"]
            require(not state["stop_reasons"] and op not in operations, "reservation not monotonic")
            require(all(Path(r["request"]["journal_path"]) != Path(payload["journal_path"]) for r in operations.values()),
                    "journal slot collision")
            require(len(operations) < self.binding["max_api_calls"], "physical-send ceiling exhausted")
            require(self._used(state) + amount(payload["exposure"], exposure=True) <= self.ceiling,
                    "credit ceiling exhausted")
            operations[op] = dict(ordinal=len(operations) + 1, request=copy.deepcopy(payload),
                                  status="RESERVED", receipt=None)
        elif kind == "ATTEMPT":
            fields(payload, {"operation_id"})
            require(payload["operation_id"] in operations, "unknown operation")
            record = operations[payload["operation_id"]]
            require(not state["stop_reasons"] and record["status"] == "RESERVED", "attempt not monotonic")
            record["status"] = "ATTEMPTED"
        elif kind == "SETTLE":
            fields(payload, {"operation_id", "receipt"})
            require(payload["operation_id"] in operations, "unknown settlement")
            record = operations[payload["operation_id"]]
            require(record["status"] != "SETTLED", "duplicate settlement event")
            self._receipt(payload["receipt"], record["request"])
            record.update(status="SETTLED", receipt=copy.deepcopy(payload["receipt"]))
            if (amount(record["receipt"]["actual"]) > amount(record["request"]["exposure"], exposure=True)
                    or self._used(state) > self.ceiling):
                if "VERIFIED_OVERRUN" not in state["stop_reasons"]:
                    state["stop_reasons"].append("VERIFIED_OVERRUN")
        elif kind == "STOP":
            fields(payload, {"reason"})
            require(payload["reason"] in {"OPERATOR_PAUSE", "AMBIGUOUS", "UNVALIDATED_EVIDENCE", "JOURNAL_AHEAD"},
                    "invalid stop reason")
            require(payload["reason"] not in state["stop_reasons"], "duplicate stop event")
            state["stop_reasons"].append(payload["reason"])
        else:
            raise LedgerError("unknown transition")

    @staticmethod
    def _used(state):
        return sum(amount(r["receipt"]["actual"]) if r["status"] == "SETTLED" else
                   amount(r["request"]["exposure"], exposure=True) for r in state["operations"].values())

    def _read(self):
        self._check_runtime()
        safe_path(self.path)
        require(self.path.is_dir(), "missing ledger; never recreate an expected batch")
        paths = list(self.path.iterdir())
        require(paths and len(paths) % 2 == 0, "incomplete ledger publication")
        count = len(paths) // 2
        expected = {f"{n:08d}{suffix}" for n in range(count) for suffix in (".intent", ".json")}
        require({p.name for p in paths} == expected, "foreign/missing/temp ledger evidence")
        state = dict(operations={}, stop_reasons=[])
        previous = "0" * 64
        for seq in range(count):
            pair = [self.path / f"{seq:08d}{suffix}" for suffix in (".intent", ".json")]
            for path in pair:
                safe_path(path)
                require(path.is_file(), "non-file ledger evidence")
            raw = pair[0].read_bytes()
            require(raw == pair[1].read_bytes(), "intent/publication divergence")
            event = strict_load(raw)
            fields(event, {"schema", "sequence", "previous_sha256", "kind", "payload", "sha256"})
            require(type(event["schema"]) is int and event["schema"] == 1 and
                    type(event["sequence"]) is int and event["sequence"] == seq and
                    event["previous_sha256"] == previous, "ledger chain drift")
            require(sha(event["sha256"]) == digest({k: v for k, v in event.items() if k != "sha256"}),
                    "ledger hash mismatch")
            if seq == 0:
                require(event["kind"] == "INITIALIZE" and canonical(event["payload"]) == canonical(self.identity),
                        "original authorization/storage drift")
            else:
                self._apply(state, event["kind"], event["payload"])
            previous = event["sha256"]
        if self._head is not None:
            require(self._head == (count, previous), "unexpected foreign write; reopen under lock")
        self._head = (count, previous)
        return state

    def _append(self, kind, payload):
        state = self._read()
        self._apply(state, kind, payload)  # validate before any intent/write
        self._publish(*self._head, kind, payload)
        self._head = None  # exception before this line requires a fresh instance
        return self._read()

    def snapshot_locked(self):
        state = self._read()
        used = self._used(state)
        return {**copy.deepcopy(state), "state": "STOP" if state["stop_reasons"] else "OPEN",
            "physical_slots": len(state["operations"]), "credit_committed_nano_usd": used,
            "remaining_nano_usd": max(0, self.ceiling - used), "paid_execution_authorized": False,
            "head_sha256": self._head[1]}

    def reserve_locked(self, request):
        request = strict_load(canonical(request))
        self._request(request)
        state = self._read()
        previous = state["operations"].get(request["operation_id"])
        if previous:
            require(canonical(previous["request"]) == canonical(request), "operation replay identity drift")
            return {**copy.deepcopy(previous), "new_reservation": False, "send_authorized": False}
        state = self._append("RESERVE", request)
        return {**copy.deepcopy(state["operations"][request["operation_id"]]),
                "new_reservation": True, "send_authorized": False}

    def note_attempt_locked(self, operation_id):
        state = self._read()
        require(operation_id in state["operations"], "unknown attempt")
        if state["operations"][operation_id]["status"] == "RESERVED":
            self._append("ATTEMPT", dict(operation_id=operation_id))

    def stop_locked(self, reason):
        state = self._read()
        if reason not in state["stop_reasons"]:
            self._append("STOP", dict(reason=reason))

    def settle_locked(self, operation_id, *, validate_recorded_response):
        state = self._read()
        require(operation_id in state["operations"], "unknown settlement")
        record = state["operations"][operation_id]
        try:
            receipt = validate_recorded_response(Path(record["request"]["journal_path"]),
                                                 copy.deepcopy(record["request"]))
            receipt = strict_load(canonical(receipt))
            self._receipt(receipt, record["request"])
            if record["receipt"] is not None:
                require(canonical(record["receipt"]) == canonical(receipt), "settlement replay drift")
                return copy.deepcopy(record)
        except Exception:
            self.stop_locked("UNVALIDATED_EVIDENCE")
            raise
        state = self._append("SETTLE", dict(operation_id=operation_id, receipt=receipt))
        return copy.deepcopy(state["operations"][operation_id])

    def reconcile_locked(self, operation_id, *, validate_observation, validate_recorded_response):
        """Conservative local observation only; B2B must enumerate ALL journals."""
        state = self._read()
        if operation_id not in state["operations"]:
            self.stop_locked("JOURNAL_AHEAD")
            return "BLOCKED_JOURNAL_AHEAD"
        record = state["operations"][operation_id]
        try:
            proof = validate_observation(Path(record["request"]["journal_path"]), copy.deepcopy(record["request"]))
            fields(proof, {"status", "reservation_sha256", "evidence_sha256"})
            require(proof["reservation_sha256"] == digest(record["request"]), "observation drift")
            sha(proof["evidence_sha256"])
            require(proof["status"] in {"ABSENT", "PLANNED", "STARTED", "UNCERTAIN", "RESPONSE_RECORDED"},
                    "invalid journal observation")
            require(record["status"] != "SETTLED" or proof["status"] == "RESPONSE_RECORDED",
                    "recorded response evidence regressed")
        except Exception:
            self.stop_locked("UNVALIDATED_EVIDENCE")
            raise
        if proof["status"] == "RESPONSE_RECORDED":
            self.settle_locked(operation_id, validate_recorded_response=validate_recorded_response)
            return "SETTLED_NO_SEND"
        if proof["status"] in {"STARTED", "UNCERTAIN"} or record["status"] == "ATTEMPTED":
            self.stop_locked("AMBIGUOUS")
            return "BLOCKED_AMBIGUOUS"
        return "LEDGER_AHEAD_NO_SEND"
