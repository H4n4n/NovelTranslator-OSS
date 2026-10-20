"""Import-safe adapter between domain attempts and the generic paid journal.

Domain IDs and validation stay owned by repair/adjudication. The adapter only
binds exact requests and exposes durable transport evidence to those domains.
"""
from copy import deepcopy
from hashlib import sha256

from production_failure_classification import NonProviderProductionError
from production_paid_request_journal import load_matching_journal, paid_request_journal_path


class AuxiliaryPaidRequestError(NonProviderProductionError):
    failure_kind = "local_safety"

    def __init__(self, cause, evidence, stats=None):
        super().__init__(str(cause))
        self.transport_evidence = deepcopy(evidence)
        self.paid_stats = deepcopy(stats or {})


class JournaledAuxiliaryRequest:
    journaled = True

    def __init__(self, *, operation, chapter, source_text, translation_text,
                 journal_dir, request_kwargs, identity_factory, run_request):
        self.operation = operation
        self.chapter = chapter
        self.source_text = source_text
        self.translation_text = translation_text
        self.journal_dir = journal_dir
        self.request_kwargs = request_kwargs
        self.identity_factory = identity_factory
        self.run_request = run_request

    def __call__(self, request, *, operation_id):
        kind = request.get("request_kind", request.get("payload", {}).get("request_kind", "initial"))
        operation = self.operation + ("_correction" if kind != "initial" else "")
        base = paid_request_journal_path(self.journal_dir, operation, self.chapter)
        # Domain operation identity fixes the slot; changing a reconstructed
        # payload cannot create a replacement slot and evade mismatch blocking.
        path = base.with_name(base.stem + "." + sha256(operation_id.encode()).hexdigest() + ".json")
        try:
            kwargs = self.request_kwargs(operation, request)
            identity = self.identity_factory(operation, self.chapter, self.source_text,
                                             kwargs, translation_text=self.translation_text)
        except Exception as exc:
            # Request construction has no transport capability. An invalid
            # request has no immutable journal identity yet; retain the durable
            # domain PLANNED marker instead of inventing paid evidence.
            raise AuxiliaryPaidRequestError(exc, {"status": "planned", "journal_path": str(path),
                "domain_operation_id": operation_id, "api_called": False,
                "remote_attempt_possible": False, "replayed": False}) from exc
        previous_status = None
        runner_entered = False
        try:
            previous = load_matching_journal(path, identity)
            previous_status = previous["status"] if previous else None
            runner_entered = True
            result = self.run_request(operation, self.chapter, self.source_text, kwargs,
                translation_text=self.translation_text, journal_path=path)
        except Exception as exc:
            try:
                record = load_matching_journal(path, identity)
                status = record["status"] if record else "planned"
                stats = record.get("response", {}).get("stats", {}) if record else {}
            except Exception:
                status, stats = "blocked", {}
            raise AuxiliaryPaidRequestError(exc, {"status": status, "journal_path": str(path),
                "operation_id": identity["operation_id"],
                "request_sha256": identity["request_sha256"],
                "api_called": (runner_entered and previous_status not in {"started", "uncertain", "response_recorded"}
                               and status in {"started", "uncertain", "response_recorded", "blocked"}),
                "remote_attempt_possible": status in {"started", "uncertain", "response_recorded", "blocked"},
                "replayed": previous_status == "response_recorded"}, stats) from exc
        evidence = {"status": "response_recorded", "journal_path": str(path),
                    "operation_id": result["operation_id"], "request_sha256": result["request_sha256"],
                    "api_called": result["api_called"], "replayed": result["replayed"]}
        stats = deepcopy(result["response"]["stats"])
        stats["transport_evidence"] = evidence
        return result["response"]["content"], stats


def invoke_request(request_api, request, operation_id):
    if getattr(request_api, "journaled", False):
        return request_api(request, operation_id=operation_id)
    return request_api(request)


def planned_attempt(request_api):
    if getattr(request_api, "journaled", False):
        return {"status": "planned", "api_called": False, "transport_journaled": True}
    return {"status": "started", "api_called": True}


def resumable_attempt(attempt, request_api):
    return bool(getattr(request_api, "journaled", False) and attempt
        and attempt.get("transport_journaled") is True
        and (attempt.get("status") in {"planned", "uncertain", "response_recorded", "blocked"}
             or attempt.get("corrective_retry", {}).get("transport_journaled") is True))


def failure_values(exc, existing=None):
    """Preserve historical paid origin; transport evidence is invocation-local.

    Only known non-empty usage replaces prior usage. Blocking/uncertain replay
    supplies no new paid stats and must neither erase nor fabricate them.
    """
    existing = existing or {}
    if isinstance(exc, AuxiliaryPaidRequestError):
        evidence = deepcopy(exc.transport_evidence)
        values = {"status": evidence["status"],
                  "api_called": bool(existing.get("api_called") or evidence["api_called"]
                                     or evidence["status"] == "response_recorded"),
                  "transport_evidence": evidence}
        stats = exc.paid_stats or existing.get("stats")
        if stats:
            values["stats"] = deepcopy(stats)
        return values
    return {"status": "api_failed", "api_called": True}


def success_values(stats):
    evidence = stats.get("transport_evidence")
    return {"api_called": True, "transport_evidence": deepcopy(evidence)} if evidence else {}
