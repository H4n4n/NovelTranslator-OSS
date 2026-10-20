"""G10D zero-provider physical-shadow recovery rehearsal.

This module turns the recovery lessons from the completed experiments into a
reusable G10 rehearsal. It never constructs a Provider client and it makes
transport physically impossible during replay.

The rehearsal proves two independent properties:
1. a durable response_recorded paid-request journal replays locally without
   a second transport attempt;
2. periodic-audit publication recovery can restore a partially mutated physical
   shadow, including retroactive prose/accounting state, while the original tree
   remains byte-identical.

Importing this module performs no filesystem writes, credential reads, network
activity, or Provider calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
)
from benchmark_longitudinal_g10_safety import (
    CreditReservationLedger,
    G10A1_SAFETY_SHA256,
    assert_shadow_replay_preconditions,
    poisoned_transport_env,
)
from full_pipeline_periodic_audit_recovery import (
    RECOVERY_REVISION as AUDIT_RECOVERY_REVISION,
    reconcile_project_periodic_audit_recovery,
)
from production_paid_request_journal import (
    PaidRequestUncertainError,
    load_matching_journal,
    run_journaled_paid_request,
)


G10D_SCHEMA_VERSION = 1
G10D_REVISION = "g10-physical-shadow-poisoned-transport-rehearsal-v1"


class G10DRehearsalError(RuntimeError):
    """G10D rehearsal evidence is unsafe, incomplete, or inconsistent."""


class G10DTransportPoisonedError(G10DRehearsalError):
    """Raised if any code path attempts transport during a poisoned rehearsal."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_fingerprint(root: Path | str) -> dict[str, Any]:
    """Fingerprint a physical tree by relative path, size, and SHA-256."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise G10DRehearsalError(f"tree root is not a directory: {root_path}")
    rows: list[str] = []
    byte_count = 0
    for path in sorted(
        (item for item in root_path.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root_path).as_posix(),
    ):
        if path.is_symlink():
            raise G10DRehearsalError(
                f"rehearsal tree contains a symlink: {path}"
            )
        relative = path.relative_to(root_path).as_posix()
        size = path.stat().st_size
        byte_count += size
        rows.append(f"{relative}\t{size}\t{_sha256_file(path)}")
    return {
        "file_count": len(rows),
        "byte_count": byte_count,
        "sha256": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(),
    }


def _assert_distinct_roots(original_root: Path, shadow_root: Path) -> None:
    if original_root == shadow_root:
        raise G10DRehearsalError("shadow root must differ from original root")
    try:
        shadow_root.relative_to(original_root)
    except ValueError:
        pass
    else:
        raise G10DRehearsalError("shadow root may not be inside original root")
    try:
        original_root.relative_to(shadow_root)
    except ValueError:
        pass
    else:
        raise G10DRehearsalError("original root may not be inside shadow root")


def create_physical_shadow(
    original_root: Path | str,
    shadow_root: Path | str,
) -> dict[str, Any]:
    """Create one exact, create-once physical directory shadow."""
    original = Path(original_root).expanduser().resolve()
    shadow = Path(shadow_root).expanduser().resolve()
    if not original.is_dir():
        raise G10DRehearsalError(f"original root is missing: {original}")
    _assert_distinct_roots(original, shadow)
    if shadow.exists():
        raise G10DRehearsalError(
            f"shadow root already exists and will not be overwritten: {shadow}"
        )
    if original.is_symlink():
        raise G10DRehearsalError("original rehearsal root may not be a symlink")
    for item in original.rglob("*"):
        if item.is_symlink():
            raise G10DRehearsalError(
                f"original rehearsal tree contains a symlink: {item}"
            )

    before = tree_fingerprint(original)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(original, shadow)
    after_original = tree_fingerprint(original)
    shadow_identity = tree_fingerprint(shadow)
    if after_original != before:
        shutil.rmtree(shadow, ignore_errors=True)
        raise G10DRehearsalError(
            "original tree changed while creating physical shadow"
        )
    if shadow_identity != before:
        shutil.rmtree(shadow, ignore_errors=True)
        raise G10DRehearsalError(
            "physical shadow does not match original tree fingerprint"
        )
    return {
        "original_root": str(original),
        "shadow_root": str(shadow),
        "original_fingerprint": before,
        "shadow_fingerprint": shadow_identity,
        "physical_shadow_verified": True,
        "provider_calls": 0,
    }


def poisoned_send_once(*_args: Any, **_kwargs: Any) -> Any:
    """Hard transport poison: any invocation means the rehearsal failed."""
    raise G10DTransportPoisonedError(
        "G10D transport is deliberately poisoned; Provider send attempted"
    )


def _unexpected_persistence(*_args: Any, **_kwargs: Any) -> None:
    raise G10DRehearsalError(
        "response_recorded replay unexpectedly attempted journal persistence"
    )


def replay_recorded_response_on_shadow(
    *,
    original_root: Path | str,
    shadow_root: Path | str,
    journal_relative_path: Path | str,
    ledger_relative_path: Path | str,
    authorization_sha256: str,
    reservation_id: str,
    expected_identity: Mapping[str, Any],
    normalize_response: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay one exact durable response with transport physically impossible."""
    original = Path(original_root).expanduser().resolve()
    shadow = Path(shadow_root).expanduser().resolve()
    _assert_distinct_roots(original, shadow)
    if not original.is_dir() or not shadow.is_dir():
        raise G10DRehearsalError("original/shadow roots must both exist")

    original_before = tree_fingerprint(original)
    journal_path = shadow / Path(journal_relative_path)
    record = load_matching_journal(journal_path, expected_identity)
    if record is None:
        raise G10DRehearsalError("shadow response_recorded journal is missing")
    if record.get("status") != "response_recorded":
        raise G10DRehearsalError(
            "G10D local replay requires response_recorded journal state"
        )

    ledger = CreditReservationLedger.open(
        path=shadow / Path(ledger_relative_path),
        authorization_sha256=authorization_sha256,
    )
    matching = [
        item
        for item in ledger.document["records"]
        if item.get("reservation_id") == reservation_id
    ]
    if len(matching) != 1:
        raise G10DRehearsalError(
            "G10D replay reservation does not resolve uniquely"
        )
    reservation = matching[0]
    exact_request_match = (
        reservation.get("request_sha256") == expected_identity.get("request_sha256")
        == record.get("request_sha256")
    )
    if reservation.get("status") != "accepted":
        raise G10DRehearsalError(
            "G10D replay requires accepted durable CE reservation"
        )
    if reservation.get("send_started") is not True:
        raise G10DRehearsalError(
            "G10D accepted reservation lacks send-start evidence"
        )

    preconditions = assert_shadow_replay_preconditions(
        original_root=original,
        shadow_root=shadow,
        journal_status="response_recorded",
        ledger_status=reservation["status"],
        exact_request_match=exact_request_match,
        transport_poisoned=True,
    )

    replay = run_journaled_paid_request(
        journal_path=journal_path,
        identity=expected_identity,
        save_json=_unexpected_persistence,
        prepare_attempt=lambda: (_ for _ in ()).throw(
            G10DRehearsalError(
                "response_recorded replay unexpectedly prepared a new attempt"
            )
        ),
        send_once=poisoned_send_once,
        normalize_response=normalize_response,
    )
    if replay.get("api_called") is not False or replay.get("replayed") is not True:
        raise G10DRehearsalError(
            "durable response did not replay locally without Provider access"
        )
    if tree_fingerprint(original) != original_before:
        raise G10DRehearsalError(
            "original tree changed during shadow response replay"
        )

    return {
        **preconditions,
        "journal_relative_path": Path(journal_relative_path).as_posix(),
        "ledger_relative_path": Path(ledger_relative_path).as_posix(),
        "reservation_id": reservation_id,
        "ledger_status": reservation["status"],
        "operation_id": replay["operation_id"],
        "request_sha256": replay["request_sha256"],
        "response": replay["response"],
        "api_called": False,
        "replayed": True,
        "original_unchanged": True,
        "provider_calls": 0,
    }


def assert_ambiguous_shadow_never_resends(
    *,
    shadow_root: Path | str,
    journal_relative_path: Path | str,
    expected_identity: Mapping[str, Any],
    normalize_response: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    """Prove started/uncertain evidence cannot reach the poisoned transport."""
    shadow = Path(shadow_root).expanduser().resolve()
    journal_path = shadow / Path(journal_relative_path)
    try:
        run_journaled_paid_request(
            journal_path=journal_path,
            identity=expected_identity,
            save_json=_unexpected_persistence,
            prepare_attempt=lambda: (_ for _ in ()).throw(
                G10DRehearsalError(
                    "ambiguous replay unexpectedly prepared a new attempt"
                )
            ),
            send_once=poisoned_send_once,
            normalize_response=normalize_response,
        )
    except PaidRequestUncertainError:
        return
    except G10DTransportPoisonedError as exc:
        raise G10DRehearsalError(
            "ambiguous durable state reached transport instead of failing closed"
        ) from exc
    raise G10DRehearsalError(
        "ambiguous durable state did not fail closed"
    )


def reconcile_audit_on_physical_shadow(
    *,
    original_root: Path | str,
    shadow_root: Path | str,
) -> dict[str, Any]:
    """Run real audit-publication recovery only on a verified physical shadow."""
    original = Path(original_root).expanduser().resolve()
    shadow = Path(shadow_root).expanduser().resolve()
    _assert_distinct_roots(original, shadow)
    if not original.is_dir() or not shadow.is_dir():
        raise G10DRehearsalError("original/shadow roots must both exist")

    original_before = tree_fingerprint(original)
    env = poisoned_transport_env({})
    if env.get("G10_TRANSPORT_POISONED") != "1":
        raise G10DRehearsalError("G10D transport poison marker is missing")
    actions = reconcile_project_periodic_audit_recovery(shadow)
    original_after = tree_fingerprint(original)
    if original_after != original_before:
        raise G10DRehearsalError(
            "original tree changed during audit recovery rehearsal"
        )
    if not actions:
        raise G10DRehearsalError(
            "audit recovery rehearsal performed no recovery action"
        )
    return {
        "actions": actions,
        "original_fingerprint_before": original_before,
        "original_fingerprint_after": original_after,
        "original_unchanged": True,
        "transport_poisoned": True,
        "provider_calls": 0,
    }


def g10d_contract_identity() -> dict[str, Any]:
    return {
        "schema_version": G10D_SCHEMA_VERSION,
        "revision": G10D_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessors": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
            "audit_recovery_revision": AUDIT_RECOVERY_REVISION,
        },
        "rehearsals": {
            "physical_shadow_copy_byte_identity_required": True,
            "original_tree_immutable_during_rehearsal": True,
            "response_recorded_local_replay_required": True,
            "accepted_durable_ce_reservation_required": True,
            "journal_reservation_request_sha_match_required": True,
            "response_recorded_provider_send_forbidden": True,
            "started_uncertain_provider_resend_forbidden": True,
            "poisoned_transport_required": True,
            "real_periodic_audit_reconciliation_required": True,
            "partial_retroactive_prose_rollback_covered": True,
            "partial_audit_accounting_rollback_covered": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


G10D_CONTRACT_SHA256 = _sha256_json(g10d_contract_identity())


def assert_paid_execution_blocked() -> None:
    identity = g10d_contract_identity()
    if identity["provider_calls"] != 0:
        raise G10DRehearsalError("G10D unexpectedly reports Provider calls")
    if identity["paid_execution_authorized"] is not False:
        raise G10DRehearsalError("G10D unexpectedly authorizes paid execution")


__all__ = [
    "G10D_CONTRACT_SHA256",
    "G10D_REVISION",
    "G10DRehearsalError",
    "G10DTransportPoisonedError",
    "assert_ambiguous_shadow_never_resends",
    "assert_paid_execution_blocked",
    "create_physical_shadow",
    "g10d_contract_identity",
    "poisoned_send_once",
    "reconcile_audit_on_physical_shadow",
    "replay_recorded_response_on_shadow",
    "tree_fingerprint",
]
