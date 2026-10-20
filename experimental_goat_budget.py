"""Experiment-local Decimal ledger. Lock spans reservation, journal and transport."""
import copy
from contextlib import contextmanager
from pathlib import Path
from experimental_goat_policy import ExperimentError, canonical, decimal, digest, external_path, local_path, positive_int
from noveltranslator_core import atomic_write_json
from production_run_lock import production_run_lock


class ExperimentBudget:
    def __init__(self, root, *, plan_sha256, limits, model):
        self.root = external_path(root, [Path(__file__).parent])
        self.path = self.root / "budget.json"
        self.identity = {"revision": "experimental-goat-budget-v1", "plan_sha256": plan_sha256,
                         "model_sha256": model.sha256, "limits": copy.deepcopy(limits)}
        if set(limits) != {"global_ce", "global_sends", "chapter_ce", "chapter_sends"}:
            raise ExperimentError("explicit four budget ceilings required")
        for key in ("global_ce", "chapter_ce"):
            if decimal(limits[key]) <= 0:
                raise ExperimentError("positive budget required")
        for key in ("global_sends", "chapter_sends"):
            positive_int(limits[key])
        self.limits = copy.deepcopy(limits)
        self.model = model

    @contextmanager
    def locked(self):
        with production_run_lock(self.root):
            self.load()
            yield self

    def load(self):
        import json
        if self.path.exists():
            local_path(self.path)
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if value["sha256"] != digest({k:v for k,v in value.items() if k != "sha256"}):
                    raise ExperimentError("budget hash mismatch")
                if value["identity"] != self.identity or not isinstance(value["records"], dict):
                    raise ExperimentError("budget identity mismatch")
                self.document = value
                for op, record in value["records"].items():
                    if (record["operation_id"] != op or record["status"] not in {"reserved", "accepted", "released"}
                        or type(record["chapter"]) is not int or record["chapter"] < 0
                        or not isinstance(record["track"], str) or not isinstance(record["journal_path"], str)):
                        raise ExperimentError("invalid budget record")
                    decimal(record["reserved_ce"])
                    if record["status"] == "accepted" and decimal(record["actual_ce"]) > decimal(record["reserved_ce"]):
                        raise ExperimentError("settlement exceeds reservation")
                self._check_caps()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ExperimentError("corrupt budget ledger") from exc
        else:
            self.document = {"identity": copy.deepcopy(self.identity), "records": {}}
        return copy.deepcopy(self.document)

    def save(self):
        value = {k:v for k,v in self.document.items() if k != "sha256"}
        value["sha256"] = digest(value)
        atomic_write_json(self.path, value)
        self.document = value

    def totals(self, track=None, chapter=None):
        records = [r for r in self.document["records"].values() if r["status"] != "released"
                   and (track is None or (r["track"], r["chapter"]) == (track, chapter))]
        return (sum((decimal(r["actual_ce"] if r["status"] == "accepted" else r["reserved_ce"])
                     for r in records), decimal("0")), len(records))

    def _check_caps(self):
        ce, sends = self.totals()
        if ce > decimal(self.limits["global_ce"]) or sends > self.limits["global_sends"]:
            raise ExperimentError("global budget ceiling exceeded")
        groups = {(r["track"], r["chapter"]) for r in self.document["records"].values() if r["chapter"]}
        for track, chapter in groups:
            ce, sends = self.totals(track, chapter)
            if ce > decimal(self.limits["chapter_ce"]) or sends > self.limits["chapter_sends"]:
                raise ExperimentError("chapter budget ceiling exceeded")

    def reserve(self, identity, path, track, chapter, wire):
        """Pessimistic send count begins before STARTED; unresolved retains it."""
        op = identity["operation_id"]
        if op in self.document["records"]:
            raise ExperimentError("existing reservation cannot authorize replacement send")
        prompt_upper = len(canonical(wire).encode("utf-8")) + 8192
        if prompt_upper + wire["max_completion_tokens"] > self.model.context_tokens:
            raise ExperimentError("request exceeds conservative context envelope")
        bound = self.model.charge(prompt_upper, wire["max_completion_tokens"])
        record = dict(operation_id=op, request_sha256=identity["request_sha256"],
            journal_path=str(local_path(path)), track=track, chapter=chapter,
            reserved_ce=str(bound), prompt_upper=prompt_upper, status="reserved")
        self.document["records"][op] = record
        try:
            self._check_caps()
        except BaseException:
            del self.document["records"][op]
            raise
        self.save()
        return record

    def require(self, identity, path):
        record = self.document["records"].get(identity["operation_id"])
        if (not record or record["request_sha256"] != identity["request_sha256"]
            or record["journal_path"] != str(local_path(path)) or record["status"] == "released"):
            raise ExperimentError("journal lacks matching durable reservation")
        return record

    def release_presend(self, identity):
        record = self.document["records"][identity["operation_id"]]
        if record["status"] != "reserved":
            raise ExperimentError("cannot release accepted reservation")
        # Caller must prove journal remains PLANNED; STARTED is never released.
        record["status"] = "released"
        self.save()

    def settle(self, identity, path, actual, response_sha):
        record = self.require(identity, path)
        actual = decimal(actual)
        if actual > decimal(record["reserved_ce"]):
            raise ExperimentError("usage exceeds conservative reservation; quarantine")
        if record["status"] == "accepted":
            if record["actual_ce"] != str(actual) or record["response_sha256"] != response_sha:
                raise ExperimentError("settlement replay mismatch")
            return
        record.update(status="accepted", actual_ce=str(actual), response_sha256=response_sha)
        self.save()
