"""Generic zero-provider safety/accounting/recovery primitives for G10.

G10A1 separates reusable paid-boundary safety from the old GOAT Stage-1 run
layout. Nothing in this module constructs a Provider client, reads credentials,
sends a request, or authorizes paid execution.

Later G10 executors must layer these primitives around the existing production
paid-request journal:
- reserve conservative credit-equivalent maximum before transport;
- persist send_started before transport;
- retain the full reservation for a post-send ambiguous outcome;
- convert a reservation to accepted exact usage only after a durable response;
- release a reservation only when durable evidence proves transport never began;
- quarantine one literary model/case or one chronological model/novel track;
- permit recovery replay only from a distinct physical shadow with deliberately
  poisoned transport and exact request/accounting identity.

The global budget rule is stricter than the historical accepted-cost-only
ledger: committed CE equals accepted CE plus the FULL reservation of every
unresolved or ambiguous request.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from noveltranslator_core import atomic_write_json
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    SAFETY_POLICY,
)


G10A1_SCHEMA_VERSION = 1
G10A1_REVISION = "g10-generic-safety-accounting-recovery-v1"
LEDGER_SCHEMA_VERSION = 1
LEDGER_REVISION = "g10-credit-reservation-ledger-v1"
QUARANTINE_SCHEMA_VERSION = 1
QUARANTINE_REVISION = "g10-durable-quarantine-v1"

ALLOWED_RESERVATION_STATUSES = frozenset(
    {
        "reserved",
        "accepted",
        "accepted_reservation_exceeded",
        "ambiguous",
        "released_pre_send",
    }
)
COMMITTED_RESERVATION_STATUSES = frozenset({"reserved", "ambiguous"})
ALLOWED_QUARANTINE_SCOPE_KINDS = frozenset(
    {"literary_model_case", "chronological_model_novel_track"}
)
ALLOWED_QUARANTINE_OUTCOMES = frozenset(
    {
        "ambiguous_remote_outcome",
        "corrupt_or_unknown_durable_state",
        "accepted_budget_boundary_corrupt",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class G10SafetyError(RuntimeError):
    """A G10 safety/accounting/recovery invariant is unsafe or inconsistent."""


class G10BudgetError(G10SafetyError):
    """The G10 durable credit-equivalent budget cannot safely continue."""


class G10QuarantineError(G10SafetyError):
    """A G10 durable quarantine marker is invalid or conflicts."""


class G10ReplayError(G10SafetyError):
    """A G10 response replay is not proven local-only and transport-poisoned."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10SafetyError("value is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G10SafetyError(f"{label} must be lowercase SHA-256 hex")
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10BudgetError(f"{label} must be a positive finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G10BudgetError(f"{label} must be a positive finite decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G10BudgetError(f"{label} must be a positive finite decimal")
    return parsed


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10BudgetError(f"{label} must be a nonnegative finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G10BudgetError(f"{label} must be a nonnegative finite decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise G10BudgetError(f"{label} must be a nonnegative finite decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc_iso(clock: Callable[[], datetime]) -> str:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G10SafetyError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def assert_fresh_execution_root(
    execution_root: Path | str,
    *,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> Path:
    """Reject an execution root equal to, inside, or containing frozen roots."""

    raw_candidate = Path(execution_root).expanduser()
    if raw_candidate.is_symlink():
        raise G10SafetyError("G10 execution root must not be a symlink")
    candidate = raw_candidate.resolve()
    if candidate.exists():
        if not candidate.is_dir():
            raise G10SafetyError("G10 execution root must be a directory")
        for item in candidate.rglob("*"):
            if item.is_symlink():
                raise G10SafetyError(
                    "G10 execution root tree must not contain symlinks"
                )

    for frozen_value in forbidden_roots:
        frozen = Path(frozen_value).expanduser().resolve()
        if candidate == frozen or _is_within(candidate, frozen) or _is_within(
            frozen, candidate
        ):
            raise G10SafetyError(
                "G10 execution root overlaps a forbidden/frozen experiment root"
            )

    return candidate


def utf8_subprocess_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an explicit UTF-8 child-process environment without reading secrets."""

    result = dict(base_env or {})
    result["PYTHONUTF8"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    return result


def g10a1_safety_identity() -> dict[str, Any]:
    if SAFETY_POLICY["application_auto_retry_count"] != 0:
        raise G10SafetyError("G10 preregistration retry policy drifted")
    if SAFETY_POLICY["ambiguous_remote_outcome_never_resends"] is not True:
        raise G10SafetyError("G10 ambiguity policy drifted")
    if SAFETY_POLICY["maximum_concurrent_paid_transports"] != 1:
        raise G10SafetyError("G10 paid transport concurrency drifted")
    if SAFETY_POLICY["single_writer_budget_ledger_required"] is not True:
        raise G10SafetyError("G10 budget ledger writer policy drifted")

    return {
        "schema_version": G10A1_SCHEMA_VERSION,
        "revision": G10A1_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessor": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        },
        "journal": {
            "authoritative_module": "production_paid_request_journal",
            "request_sha256_required": True,
            "planned_before_transport_required": True,
            "send_started_persisted_before_transport_required": True,
            "application_auto_retry_count": 0,
            "maximum_concurrent_paid_transports": 1,
            "ambiguous_remote_outcome_never_resends": True,
        },
        "budget": {
            "ledger_revision": LEDGER_REVISION,
            "one_global_ceiling_ledger_required": True,
            "single_writer_ledger_required": True,
            "arm_subceilings_enforced_inside_global_ledger": True,
            "arm_subceilings_partition_global_ceiling": True,
            "reserve_before_transport": True,
            "committed_ce_definition": (
                "accepted_ce_plus_full_reservations_for_unresolved_or_ambiguous_requests"
            ),
            "pre_send_release_requires_proof_transport_never_started": True,
            "post_send_ambiguity_keeps_full_reservation": True,
            "accepted_usage_replaces_its_reservation": True,
            "accepted_usage_above_reservation_fails_closed": True,
            "restart_must_reuse_same_ledger": True,
        },
        "quarantine": {
            "revision": QUARANTINE_REVISION,
            "literary_scope": "model_case",
            "chronological_scope": "model_novel_track",
            "chronological_first_blocked_chapter_required": True,
            "create_once_never_overwrite": True,
            "invalid_marker_fails_closed": True,
        },
        "replay": {
            "response_recorded_only": True,
            "accepted_ledger_record_required": True,
            "exact_request_match_required": True,
            "canonical_root_local_replay_allowed": True,
            "all_replay_requires_poisoned_transport": True,
            "shadow_rehearsal_requires_distinct_physical_copy": True,
            "provider_send_during_replay_forbidden": True,
        },
        "subprocess": {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        "fresh_roots": {
            "frozen_root_overlap_forbidden": True,
            "symlink_execution_root_forbidden": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


G10A1_SAFETY_SHA256 = _sha256_json(g10a1_safety_identity())


def _ledger_hash(document: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            key: copy.deepcopy(value)
            for key, value in dict(document).items()
            if key != "ledger_sha256"
        }
    )


def _with_ledger_hash(document: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(document))
    value["ledger_sha256"] = _ledger_hash(value)
    return value


class CreditReservationLedger:
    """Durably enforce accepted CE plus unresolved full reservations."""

    def __init__(
        self,
        *,
        path: Path | str,
        document: Mapping[str, Any],
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.document = copy.deepcopy(dict(document))
        self.clock = clock or _utc_now
        self._validate()

    @classmethod
    def create(
        cls,
        *,
        path: Path | str,
        authorization_sha256: str,
        maximum_credit_equivalent: Any,
        arm_credit_equivalent_ceilings: Mapping[str, Any],
        scope_id: str = G10_BENCHMARK_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> "CreditReservationLedger":
        target = Path(path)
        if target.exists():
            raise G10BudgetError("G10 budget ledger already exists")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise G10BudgetError("G10 budget scope_id must be non-empty")
        authorization_sha256 = _require_sha256(
            authorization_sha256, "authorization_sha256"
        )
        maximum = _positive_decimal(
            maximum_credit_equivalent, "maximum_credit_equivalent"
        )
        if not isinstance(arm_credit_equivalent_ceilings, Mapping):
            raise G10BudgetError(
                "arm_credit_equivalent_ceilings must be a mapping"
            )
        if set(arm_credit_equivalent_ceilings) != {"G10A", "G10B"}:
            raise G10BudgetError(
                "G10 budget ledger requires exactly G10A and G10B arm ceilings"
            )
        arm_ceilings = {
            arm_id: _positive_decimal(value, f"{arm_id} arm ceiling")
            for arm_id, value in arm_credit_equivalent_ceilings.items()
        }
        if sum(arm_ceilings.values(), Decimal("0")) > maximum:
            raise G10BudgetError(
                "G10A plus G10B arm ceilings may not exceed the global ceiling"
            )
        use_clock = clock or _utc_now
        now = _aware_utc_iso(use_clock)
        document = _with_ledger_hash(
            {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "revision": LEDGER_REVISION,
                "benchmark_id": G10_BENCHMARK_ID,
                "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
                "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
                "authorization_sha256": authorization_sha256,
                "scope_id": scope_id,
                "maximum_credit_equivalent": _decimal_text(maximum),
                "arm_credit_equivalent_ceilings": {
                    arm_id: _decimal_text(value)
                    for arm_id, value in arm_ceilings.items()
                },
                "accepted_credit_equivalent": "0",
                "records": [],
                "created_utc": now,
                "updated_utc": now,
            }
        )
        ledger = cls(path=target, document=document, clock=use_clock)
        ledger._persist()
        return ledger

    @classmethod
    def open(
        cls,
        *,
        path: Path | str,
        authorization_sha256: str,
        scope_id: str = G10_BENCHMARK_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> "CreditReservationLedger":
        target = Path(path)
        if not target.is_file():
            raise G10BudgetError("G10 budget ledger is missing")
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise G10BudgetError("G10 budget ledger cannot be read") from exc
        if not isinstance(document, dict):
            raise G10BudgetError("G10 budget ledger must contain an object")
        ledger = cls(path=target, document=document, clock=clock)
        if ledger.document.get("authorization_sha256") != _require_sha256(
            authorization_sha256, "authorization_sha256"
        ):
            raise G10BudgetError("G10 budget ledger binds another authorization")
        if ledger.document.get("scope_id") != scope_id:
            raise G10BudgetError("G10 budget ledger binds another scope")
        return ledger

    def _validate(self) -> None:
        value = self.document
        if value.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise G10BudgetError("unsupported G10 budget ledger schema")
        if value.get("revision") != LEDGER_REVISION:
            raise G10BudgetError("unsupported G10 budget ledger revision")
        if value.get("benchmark_id") != G10_BENCHMARK_ID:
            raise G10BudgetError("G10 budget ledger benchmark identity changed")
        if value.get("g10_preregistration_sha256") != G10_PREREGISTRATION_SHA256:
            raise G10BudgetError("G10 budget ledger preregistration binding changed")
        if value.get("g10a1_safety_sha256") != G10A1_SAFETY_SHA256:
            raise G10BudgetError("G10 budget ledger safety binding changed")
        _require_sha256(value.get("authorization_sha256"), "ledger authorization_sha256")

        stored = value.get("ledger_sha256")
        if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
            raise G10BudgetError("G10 budget ledger SHA is invalid")
        if _ledger_hash(value) != stored:
            raise G10BudgetError("G10 budget ledger SHA does not recompute")

        maximum = _positive_decimal(
            value.get("maximum_credit_equivalent"),
            "ledger maximum_credit_equivalent",
        )
        accepted = _nonnegative_decimal(
            value.get("accepted_credit_equivalent"),
            "ledger accepted_credit_equivalent",
        )
        arm_ceiling_values = value.get("arm_credit_equivalent_ceilings")
        if not isinstance(arm_ceiling_values, Mapping) or set(
            arm_ceiling_values
        ) != {"G10A", "G10B"}:
            raise G10BudgetError(
                "G10 budget ledger arm ceilings are invalid"
            )
        arm_ceilings = {
            arm_id: _positive_decimal(ceiling, f"{arm_id} arm ceiling")
            for arm_id, ceiling in arm_ceiling_values.items()
        }
        if sum(arm_ceilings.values(), Decimal("0")) > maximum:
            raise G10BudgetError(
                "G10 budget ledger arm ceilings exceed global ceiling"
            )
        records = value.get("records")
        if not isinstance(records, list) or not all(
            isinstance(record, Mapping) for record in records
        ):
            raise G10BudgetError("G10 budget ledger records must be objects")

        seen_reservations: set[str] = set()
        seen_requests: set[str] = set()
        accepted_sum = Decimal("0")
        for index, record in enumerate(records, start=1):
            if record.get("record_index") != index:
                raise G10BudgetError("G10 budget ledger record indexes are not contiguous")
            reservation_id = _require_sha256(
                record.get("reservation_id"), "ledger reservation_id"
            )
            request_sha256 = _require_sha256(
                record.get("request_sha256"), "ledger request_sha256"
            )
            if reservation_id in seen_reservations:
                raise G10BudgetError("duplicate G10 reservation_id")
            if request_sha256 in seen_requests:
                raise G10BudgetError("duplicate G10 request_sha256 reservation")
            seen_reservations.add(reservation_id)
            seen_requests.add(request_sha256)

            arm_id = record.get("arm_id")
            if arm_id not in arm_ceilings:
                raise G10BudgetError(
                    "G10 reservation binds an unknown arm"
                )
            status = record.get("status")
            if status not in ALLOWED_RESERVATION_STATUSES:
                raise G10BudgetError("unknown G10 reservation status")
            reserved = _positive_decimal(
                record.get("reserved_credit_equivalent"),
                "record reserved_credit_equivalent",
            )
            actual = _nonnegative_decimal(
                record.get("accepted_credit_equivalent", "0"),
                "record accepted_credit_equivalent",
            )
            send_started = record.get("send_started")
            if not isinstance(send_started, bool):
                raise G10BudgetError("record send_started must be boolean")

            if status == "released_pre_send":
                if send_started or actual != 0:
                    raise G10BudgetError("released reservation contains paid evidence")
            elif status in {"accepted", "accepted_reservation_exceeded"}:
                if not send_started:
                    raise G10BudgetError("accepted reservation lacks send_started")
                accepted_sum += actual
                if status == "accepted" and actual > reserved:
                    raise G10BudgetError(
                        "accepted record exceeds reservation without terminal status"
                    )
                if status == "accepted_reservation_exceeded" and actual <= reserved:
                    raise G10BudgetError(
                        "terminal reservation-overrun status is inconsistent"
                    )
            elif status == "ambiguous":
                if not send_started or actual != 0:
                    raise G10BudgetError(
                        "ambiguous reservation evidence is inconsistent"
                    )
            elif status == "reserved" and actual != 0:
                raise G10BudgetError(
                    "unresolved reservation contains accepted cost"
                )

        if accepted != accepted_sum:
            raise G10BudgetError("ledger accepted CE differs from record sum")

        has_terminal_overrun = any(
            record.get("status") == "accepted_reservation_exceeded"
            for record in records
        )
        if accepted > maximum and not has_terminal_overrun:
            raise G10BudgetError(
                "ledger accepted CE exceeds ceiling without terminal evidence"
            )

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.document["updated_utc"] = _aware_utc_iso(self.clock)
        self.document = _with_ledger_hash(self.document)
        atomic_write_json(self.path, self.document)

    @property
    def maximum_credit_equivalent(self) -> Decimal:
        return Decimal(str(self.document["maximum_credit_equivalent"]))

    @property
    def accepted_credit_equivalent(self) -> Decimal:
        return Decimal(str(self.document["accepted_credit_equivalent"]))

    @property
    def committed_credit_equivalent(self) -> Decimal:
        unresolved = Decimal("0")
        for record in self.document["records"]:
            if record["status"] in COMMITTED_RESERVATION_STATUSES:
                unresolved += Decimal(str(record["reserved_credit_equivalent"]))
        return self.accepted_credit_equivalent + unresolved

    @property
    def remaining_credit_equivalent(self) -> Decimal:
        return self.maximum_credit_equivalent - self.committed_credit_equivalent

    def arm_credit_equivalent_ceiling(self, arm_id: str) -> Decimal:
        ceilings = self.document["arm_credit_equivalent_ceilings"]
        if arm_id not in ceilings:
            raise G10BudgetError("unknown G10 budget arm")
        return Decimal(str(ceilings[arm_id]))

    def arm_committed_credit_equivalent(self, arm_id: str) -> Decimal:
        self.arm_credit_equivalent_ceiling(arm_id)
        total = Decimal("0")
        for record in self.document["records"]:
            if record["arm_id"] != arm_id:
                continue
            if record["status"] in COMMITTED_RESERVATION_STATUSES:
                total += Decimal(str(record["reserved_credit_equivalent"]))
            elif record["status"] in {
                "accepted",
                "accepted_reservation_exceeded",
            }:
                total += Decimal(str(record["accepted_credit_equivalent"]))
        return total

    def arm_remaining_credit_equivalent(self, arm_id: str) -> Decimal:
        return (
            self.arm_credit_equivalent_ceiling(arm_id)
            - self.arm_committed_credit_equivalent(arm_id)
        )

    @property
    def has_terminal_budget_overrun(self) -> bool:
        return any(
            record.get("status") == "accepted_reservation_exceeded"
            for record in self.document["records"]
        )

    def _record_for(self, reservation_id: str) -> dict[str, Any]:
        reservation_id = _require_sha256(reservation_id, "reservation_id")
        matches = [
            record
            for record in self.document["records"]
            if record.get("reservation_id") == reservation_id
        ]
        if len(matches) != 1:
            raise G10BudgetError("G10 reservation does not resolve uniquely")
        return matches[0]

    def reserve(
        self,
        *,
        request_sha256: str,
        arm_id: str,
        scope_id: str,
        logical_operation: str,
        model_id: str,
        novel_id: str,
        chapter: int,
        reserved_credit_equivalent: Any,
    ) -> dict[str, Any]:
        if self.has_terminal_budget_overrun:
            raise G10BudgetError(
                "G10 budget ledger is terminal after reservation overrun"
            )
        request_sha256 = _require_sha256(request_sha256, "request_sha256")
        if any(
            record.get("request_sha256") == request_sha256
            for record in self.document["records"]
        ):
            raise G10BudgetError(
                "request_sha256 already has a G10 reservation"
            )
        for label, value in {
            "arm_id": arm_id,
            "scope_id": scope_id,
            "logical_operation": logical_operation,
            "model_id": model_id,
            "novel_id": novel_id,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise G10BudgetError(f"{label} must be non-empty")
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise G10BudgetError("chapter must be a positive integer")

        reserved = _positive_decimal(
            reserved_credit_equivalent, "reserved_credit_equivalent"
        )
        if arm_id not in self.document["arm_credit_equivalent_ceilings"]:
            raise G10BudgetError("unknown G10 budget arm")
        projected = self.committed_credit_equivalent + reserved
        if projected > self.maximum_credit_equivalent:
            raise G10BudgetError(
                "G10 global credit-equivalent ceiling would be exceeded by reservation"
            )
        arm_projected = self.arm_committed_credit_equivalent(arm_id) + reserved
        if arm_projected > self.arm_credit_equivalent_ceiling(arm_id):
            raise G10BudgetError(
                "G10 arm credit-equivalent ceiling would be exceeded by reservation"
            )

        stable = {
            "benchmark_id": G10_BENCHMARK_ID,
            "request_sha256": request_sha256,
            "arm_id": arm_id,
            "scope_id": scope_id,
            "logical_operation": logical_operation,
            "model_id": model_id,
            "novel_id": novel_id,
            "chapter": chapter,
            "reserved_credit_equivalent": _decimal_text(reserved),
        }
        reservation_id = _sha256_json(stable)
        record = {
            "record_index": len(self.document["records"]) + 1,
            "reservation_id": reservation_id,
            **stable,
            "status": "reserved",
            "send_started": False,
            "send_started_utc": None,
            "accepted_credit_equivalent": "0",
            "terminal_reason": None,
        }
        self.document["records"].append(record)
        self._persist()
        return copy.deepcopy(record)

    def mark_send_started(self, reservation_id: str) -> dict[str, Any]:
        record = self._record_for(reservation_id)
        if record["status"] != "reserved" or record["send_started"]:
            raise G10BudgetError(
                "G10 reservation is not eligible to start transport"
            )
        record["send_started"] = True
        record["send_started_utc"] = _aware_utc_iso(self.clock)
        self._persist()
        return copy.deepcopy(record)

    def release_pre_send(
        self,
        reservation_id: str,
        *,
        transport_never_started_proven: bool,
    ) -> dict[str, Any]:
        record = self._record_for(reservation_id)
        if transport_never_started_proven is not True:
            raise G10BudgetError(
                "pre-send release requires explicit no-send proof"
            )
        if record["status"] != "reserved" or record["send_started"]:
            raise G10BudgetError(
                "post-send reservation can never be released as pre-send"
            )
        record["status"] = "released_pre_send"
        record["terminal_reason"] = "transport_never_started_proven"
        self._persist()
        return copy.deepcopy(record)

    def mark_ambiguous(
        self,
        reservation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        record = self._record_for(reservation_id)
        if record["status"] != "reserved" or not record["send_started"]:
            raise G10BudgetError(
                "only a started unresolved reservation may become ambiguous"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise G10BudgetError(
                "ambiguous outcome reason must be non-empty"
            )
        record["status"] = "ambiguous"
        record["terminal_reason"] = reason[:1000]
        self._persist()
        return copy.deepcopy(record)

    def record_accepted(
        self,
        reservation_id: str,
        *,
        accepted_credit_equivalent: Any,
    ) -> dict[str, Any]:
        record = self._record_for(reservation_id)
        if record["status"] != "reserved" or not record["send_started"]:
            raise G10BudgetError(
                "accepted response must bind one started reservation"
            )
        actual = _nonnegative_decimal(
            accepted_credit_equivalent, "accepted_credit_equivalent"
        )
        reserved = Decimal(str(record["reserved_credit_equivalent"]))
        projected_accepted = self.accepted_credit_equivalent + actual

        record["accepted_credit_equivalent"] = _decimal_text(actual)
        record["status"] = (
            "accepted"
            if actual <= reserved
            else "accepted_reservation_exceeded"
        )
        if actual > reserved:
            record["terminal_reason"] = (
                "accepted_usage_exceeded_conservative_reservation"
            )
        self.document["accepted_credit_equivalent"] = _decimal_text(
            projected_accepted
        )
        self._persist()

        if (
            actual > reserved
            or self.committed_credit_equivalent
            > self.maximum_credit_equivalent
        ):
            raise G10BudgetError(
                "accepted Provider usage exceeded its G10 reservation "
                "or global CE ceiling"
            )
        return copy.deepcopy(record)

    def resume_disposition(self, reservation_id: str) -> dict[str, Any]:
        record = copy.deepcopy(self._record_for(reservation_id))
        status = record["status"]
        if status == "accepted":
            disposition = "accepted_complete"
        elif status == "released_pre_send":
            disposition = "safe_no_send_released"
        elif status == "accepted_reservation_exceeded":
            disposition = "terminal_budget_overrun"
        elif status == "ambiguous" or (
            status == "reserved" and record["send_started"]
        ):
            disposition = "quarantine_no_resend"
        else:
            disposition = "safe_pre_send_resume"
        return {
            "reservation_id": record["reservation_id"],
            "status": status,
            "disposition": disposition,
            "committed_credit_equivalent": _decimal_text(
                self.committed_credit_equivalent
            ),
            "remaining_credit_equivalent": _decimal_text(
                self.remaining_credit_equivalent
            ),
        }


def _quarantine_hash(marker: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            key: copy.deepcopy(value)
            for key, value in dict(marker).items()
            if key != "marker_sha256"
        }
    )


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise G10QuarantineError(
            "G10 quarantine marker already exists"
        ) from exc
    if path.read_bytes() != payload:
        raise G10QuarantineError(
            "G10 quarantine marker byte verification failed"
        )


def create_quarantine_marker(
    *,
    path: Path | str,
    scope_kind: str,
    scope_id: str,
    outcome: str,
    reason: str,
    request_sha256: str,
    reservation_id: str,
    first_blocked_chapter: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if scope_kind not in ALLOWED_QUARANTINE_SCOPE_KINDS:
        raise G10QuarantineError(
            "unsupported G10 quarantine scope"
        )
    if outcome not in ALLOWED_QUARANTINE_OUTCOMES:
        raise G10QuarantineError(
            "unsupported G10 quarantine outcome"
        )
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise G10QuarantineError(
            "quarantine scope_id must be non-empty"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise G10QuarantineError(
            "quarantine reason must be non-empty"
        )
    request_sha256 = _require_sha256(
        request_sha256, "request_sha256"
    )
    reservation_id = _require_sha256(
        reservation_id, "reservation_id"
    )

    if scope_kind == "chronological_model_novel_track":
        if (
            isinstance(first_blocked_chapter, bool)
            or not isinstance(first_blocked_chapter, int)
            or first_blocked_chapter < 1
        ):
            raise G10QuarantineError(
                "chronological quarantine requires first_blocked_chapter"
            )
    elif first_blocked_chapter is not None:
        raise G10QuarantineError(
            "literary model/case quarantine must not carry "
            "chronological chapter blocking"
        )

    marker = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "revision": QUARANTINE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "first_blocked_chapter": first_blocked_chapter,
        "outcome": outcome,
        "reason": reason[:1000],
        "request_sha256": request_sha256,
        "reservation_id": reservation_id,
        "created_utc": _aware_utc_iso(clock or _utc_now),
    }
    marker["marker_sha256"] = _quarantine_hash(marker)
    _write_new_json(Path(path), marker)
    return copy.deepcopy(marker)


def load_quarantine_marker(
    path: Path | str,
) -> dict[str, Any]:
    marker_path = Path(path)
    try:
        value = json.loads(
            marker_path.read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise G10QuarantineError(
            "G10 quarantine marker cannot be read"
        ) from exc
    if not isinstance(value, dict):
        raise G10QuarantineError(
            "G10 quarantine marker must contain an object"
        )
    if value.get("schema_version") != QUARANTINE_SCHEMA_VERSION:
        raise G10QuarantineError(
            "unsupported G10 quarantine schema"
        )
    if value.get("revision") != QUARANTINE_REVISION:
        raise G10QuarantineError(
            "unsupported G10 quarantine revision"
        )
    if value.get("benchmark_id") != G10_BENCHMARK_ID:
        raise G10QuarantineError(
            "G10 quarantine benchmark identity changed"
        )
    if (
        value.get("g10_preregistration_sha256")
        != G10_PREREGISTRATION_SHA256
    ):
        raise G10QuarantineError(
            "G10 quarantine preregistration binding changed"
        )
    if value.get("g10a1_safety_sha256") != G10A1_SAFETY_SHA256:
        raise G10QuarantineError(
            "G10 quarantine safety binding changed"
        )
    if value.get("scope_kind") not in ALLOWED_QUARANTINE_SCOPE_KINDS:
        raise G10QuarantineError(
            "G10 quarantine scope is invalid"
        )
    if value.get("outcome") not in ALLOWED_QUARANTINE_OUTCOMES:
        raise G10QuarantineError(
            "G10 quarantine outcome is invalid"
        )
    _require_sha256(
        value.get("request_sha256"),
        "quarantine request_sha256",
    )
    _require_sha256(
        value.get("reservation_id"),
        "quarantine reservation_id",
    )
    stored = value.get("marker_sha256")
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G10QuarantineError(
            "G10 quarantine marker SHA is invalid"
        )
    if _quarantine_hash(value) != stored:
        raise G10QuarantineError(
            "G10 quarantine marker SHA does not recompute"
        )

    if value["scope_kind"] == "chronological_model_novel_track":
        chapter = value.get("first_blocked_chapter")
        if (
            isinstance(chapter, bool)
            or not isinstance(chapter, int)
            or chapter < 1
        ):
            raise G10QuarantineError(
                "chronological quarantine chapter is invalid"
            )
    elif value.get("first_blocked_chapter") is not None:
        raise G10QuarantineError(
            "literary quarantine carries a chapter block"
        )

    return copy.deepcopy(value)


def classify_journal_reservation_state(
    *,
    journal_status: str | None,
    reservation_status: str,
    send_started: bool,
) -> str:
    """Reconcile journal and CE-reservation evidence without contacting transport."""

    if journal_status not in {
        None,
        "planned",
        "started",
        "uncertain",
        "response_recorded",
    }:
        return "corrupt_block"
    if reservation_status not in ALLOWED_RESERVATION_STATUSES:
        return "corrupt_block"
    if not isinstance(send_started, bool):
        return "corrupt_block"

    if reservation_status == "accepted_reservation_exceeded":
        return "terminal_budget_overrun"

    if journal_status == "response_recorded":
        if reservation_status == "accepted" and send_started:
            return "local_replay_candidate"
        return "corrupt_block"

    if journal_status in {"started", "uncertain"}:
        if reservation_status == "released_pre_send":
            return "corrupt_block"
        return "quarantine_no_resend"

    # Absent/planned journal evidence proves no durable provider response.
    if reservation_status == "released_pre_send" and not send_started:
        return "safe_no_send_released"
    if reservation_status == "reserved" and not send_started:
        return "safe_pre_send"
    if send_started or reservation_status in {"accepted", "ambiguous"}:
        return "quarantine_no_resend"
    return "corrupt_block"


def assert_local_replay_preconditions(
    *,
    journal_status: str,
    ledger_status: str,
    exact_request_match: bool,
    transport_poisoned: bool,
) -> dict[str, Any]:
    """Authorize canonical-root local replay only when transport is impossible."""

    if journal_status != "response_recorded":
        raise G10ReplayError(
            "G10 replay requires durable response_recorded journal state"
        )
    if ledger_status != "accepted":
        raise G10ReplayError(
            "G10 replay requires an accepted durable accounting record"
        )
    if exact_request_match is not True:
        raise G10ReplayError(
            "G10 replay request identity is not exact"
        )
    if transport_poisoned is not True:
        raise G10ReplayError(
            "G10 replay requires deliberately poisoned transport"
        )
    return {
        "replay_authorized": True,
        "provider_send_authorized": False,
        "journal_status": journal_status,
        "ledger_status": ledger_status,
        "exact_request_match": True,
        "transport_poisoned": True,
    }


def assert_shadow_replay_preconditions(
    *,
    original_root: Path | str,
    shadow_root: Path | str,
    journal_status: str,
    ledger_status: str,
    exact_request_match: bool,
    transport_poisoned: bool,
) -> dict[str, Any]:
    """Authorize a G10D physical-shadow replay rehearsal."""

    original = Path(original_root).expanduser().resolve()
    shadow = Path(shadow_root).expanduser().resolve()
    if original == shadow:
        raise G10ReplayError(
            "shadow replay root must differ from original root"
        )
    if _is_within(shadow, original) or _is_within(
        original, shadow
    ):
        raise G10ReplayError(
            "shadow/original roots must not contain one another"
        )
    if original.exists() and shadow.exists():
        try:
            if os.path.samefile(original, shadow):
                raise G10ReplayError(
                    "shadow replay root is the same physical directory"
                )
        except OSError as exc:
            raise G10ReplayError(
                "cannot prove shadow/original directory distinction"
            ) from exc

    local = assert_local_replay_preconditions(
        journal_status=journal_status,
        ledger_status=ledger_status,
        exact_request_match=exact_request_match,
        transport_poisoned=transport_poisoned,
    )
    return {
        **local,
        "original_root": str(original),
        "shadow_root": str(shadow),
        "physical_shadow_rehearsal": True,
    }


def poisoned_transport_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment contract used by G10D shadow replay rehearsal."""

    result = utf8_subprocess_env(base_env)
    result["G10_TRANSPORT_POISONED"] = "1"
    return result


def assert_paid_execution_still_blocked() -> None:
    identity = g10a1_safety_identity()
    if identity["provider_calls"] != 0:
        raise G10SafetyError(
            "G10A1 unexpectedly reports Provider calls"
        )
    if identity["paid_execution_authorized"] is not False:
        raise G10SafetyError(
            "G10A1 unexpectedly authorizes paid execution"
        )


__all__ = [
    "ALLOWED_QUARANTINE_OUTCOMES",
    "ALLOWED_QUARANTINE_SCOPE_KINDS",
    "CreditReservationLedger",
    "G10A1_REVISION",
    "G10A1_SAFETY_SHA256",
    "G10BudgetError",
    "G10QuarantineError",
    "G10ReplayError",
    "G10SafetyError",
    "LEDGER_REVISION",
    "QUARANTINE_REVISION",
    "assert_fresh_execution_root",
    "assert_local_replay_preconditions",
    "assert_paid_execution_still_blocked",
    "assert_shadow_replay_preconditions",
    "classify_journal_reservation_state",
    "create_quarantine_marker",
    "g10a1_safety_identity",
    "load_quarantine_marker",
    "poisoned_transport_env",
    "utf8_subprocess_env",
]
