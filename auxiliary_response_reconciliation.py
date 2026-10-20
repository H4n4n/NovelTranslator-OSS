"""Exact, local reconciliation between domain attempts and paid transport.

This capability is opt-in. It never calls a provider, and absence is meaningful
only for an adapter that owns every send through this exact durable slot.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from production_paid_request_journal import (
    PaidRequestJournalError, PaidRequestUncertainError, load_matching_journal,
)

# Outer-only protocol proof; never part of the request or paid slot identity.
BOUND_BEFORE_TRANSPORT_VERSION = "bound-before-transport-v1"


def original_review_attempt_marker(item):
    """Retain the exact legacy marker check with validated ordering fields."""
    marker = deepcopy(dict(item))
    marker.pop("transport_binding", None)
    if "transport_ownership_version" in marker:
        if (marker.pop("transport_ownership_version") != BOUND_BEFORE_TRANSPORT_VERSION
                or marker.pop("lifecycle", None) != "in_flight"):
            raise PaidRequestJournalError("invalid original review transport ownership proof")
    return marker


@dataclass(frozen=True)
class AuxiliaryReconciliation:
    status: Literal["pre_transport", "response_recorded"]
    binding: dict
    response: dict | None = None


def reconcile_owned_request(*, binding, load_response):
    """The caller guarantees exclusive journal ownership, including STARTED.

    load_response must be a local acceptance/accounting validator, with its own
    replay-only guard. The generic layer knows no provider envelope internals.
    """
    record = load_matching_journal(binding["journal_path"], binding["identity"])
    if record is None or record["status"] == "planned":
        return AuxiliaryReconciliation("pre_transport", deepcopy(binding))
    if record["status"] in {"started", "uncertain"}:
        raise PaidRequestUncertainError(
            f"paid auxiliary request is {record['status']}; automatic reissue is blocked: "
            + binding["journal_path"]
        )
    if record["status"] != "response_recorded":
        raise PaidRequestJournalError("unsupported paid auxiliary journal state")
    response = load_response()
    return AuxiliaryReconciliation("response_recorded", deepcopy(binding), deepcopy(response))


def audit_response_fields(response):
    """One representation for normal responses and locally hydrated responses."""
    from hashlib import sha256
    content = response.get("content", "")
    if not isinstance(content, str):
        raise PaidRequestJournalError("audit response content must be a string")
    return {
        "finish_reason": response.get("finish_reason"),
        "stats": deepcopy(dict(response.get("stats") or {})),
        "raw_content": content,
        "raw_content_hash": sha256(content.encode("utf-8")).hexdigest(),
        "lifecycle": "response_received",
    }


class JournalOwnedAuxiliaryCallback:
    """Bind before the outer PREPARED write; recover only the same request.

    bind_request and send_request belong to the journal-owning runtime. The
    callback cannot substitute a new slot on restart when inputs drift.
    """
    def __init__(self, *, build_request, bind_request, send_request):
        self.build_request = build_request
        self.bind_request = bind_request
        self.send_request = send_request

    def prepare(self, record, args):
        return self.bind_request(self.build_request(*args), record, args[-1])

    def invoke(self, args, binding, *, replay_only):
        return self.send_request(self.build_request(*args), binding, replay_only=replay_only)

    def __call__(self, *args):
        record = args[-1]["outer_attempt"]
        binding = self.prepare(record, args)
        if binding != record.get("transport_binding"):
            raise PaidRequestJournalError("audit transport binding changed before request")
        return self.invoke(args, binding, replay_only=False)

    def recover(self, record, args):
        binding = self.prepare(record, args)
        if binding != record.get("transport_binding"):
            raise PaidRequestJournalError("saved audit transport/request binding mismatch")
        return reconcile_owned_request(binding=binding, load_response=lambda:
            self.invoke(args, binding, replay_only=True))


class JournalOwnedAuditCallback(JournalOwnedAuxiliaryCallback):
    """Raw periodic-audit responses use the generic binding capability."""
