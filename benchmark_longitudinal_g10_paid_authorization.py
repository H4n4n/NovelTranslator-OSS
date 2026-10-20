"""G10F/G10G paid-execution authorization, budget, and quarantine boundary.

This module is the shared, import-safe foundation for the two paid G10 executors.
It binds the frozen G10E execution freeze (freeze ID, freeze-file SHA-256, durable
budget-ledger SHA-256, matched-literary plan SHA-256, G10B S0 manifest SHA-256,
the G10A1 safety identity, the frozen pricing version, and the frozen Decimal
ratecard) into one deterministic paid-authorization plan.

Guarantees:

* importing this module reads no credentials, opens no client, performs no
  network activity, makes no filesystem write, and constructs no Provider client;
* every authorization binds the exact arm/model/novel/chapter/case/run/request/
  state-fingerprint tuple, the exact freeze identity, and a deterministic
  ``approval_token`` that cannot authorize any other tuple;
* the frozen ledger is opened with the *G10E freeze ID* as its authorization
  value: the freeze created ``g10_budget.json`` with
  ``authorization_sha256 == freeze_id``, so no new module hash may ever be used
  there;
* ``frozen_execution_binding`` is a read-only binding check that deliberately
  does **not** require an untouched zero-commit ledger (that stricter check
  belongs to G10E's initial verification only);
* a reservation and its ``send_started`` marker always strictly precede the
  single transport attempt, and a rejected ceiling is a hard error with no
  journal mutation.

Nothing here authorizes paid work by itself. A real paid send still requires a
materialized plan, its exact plan SHA-256, its exact approval token, an unchanged
target-state fingerprint, and an explicit operator authorization flag.
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
from typing import Any, Mapping

from benchmark_longitudinal_g10_chronological import (
    G10B1_LAST_CHAPTER,
    TRACK_BY_ID,
)
from benchmark_longitudinal_g10_matched import (
    G10A2_REQUEST_COUNT,
    PAIR_SPECS,
    matched_plan_identity,
)
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    GLM_MODEL_ID,
    NOVELS,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import (
    ALLOWED_QUARANTINE_OUTCOMES,
    ALLOWED_QUARANTINE_SCOPE_KINDS,
    CreditReservationLedger,
    G10A1_SAFETY_SHA256,
    G10SafetyError,
    create_quarantine_marker,
    poisoned_transport_env,
    utf8_subprocess_env,
)
from benchmark_longitudinal_g10e_freeze import (
    ARM_CEILINGS as G10E_ARM_CEILINGS,
    G10E_BUDGET_LEDGER_FILENAME,
    G10E_CONTRACT_SHA256,
    G10E_FREEZE_FILENAME,
    G10E_PRICING_VERSION,
    GLOBAL_CEILING as G10E_GLOBAL_CEILING,
    MODEL_RATECARD as G10E_MODEL_RATECARD,
    PREDECESSOR_CONTRACTS,
    g10e_contract_identity,
)


G10PAID_SCHEMA_VERSION = 1
G10PAID_REVISION = "g10-paid-authorization-v1"
G10PAID_REPOSITORY_ANCHOR = "ae33848cf3832a780fbc7b7baa264fd13c7e0567"
G10PAID_STATE_FINGERPRINT_REVISION = "g10-paid-target-state-fingerprint-v1"
G10PAID_ARTIFACT_DIRNAME = "artifacts"
G10PAID_AUTHORIZATION_DIRNAME = "authorization"

# Frozen G10F/G10G identities. These literals are re-verified against the live
# predecessor modules at import time, so a drift fails closed immediately.
EXPECTED_FREEZE_ID = (
    "22fd63371c9e38fe7051f0d14194e3d6ded9f07805e48fc6f45b3f325eff25ca"
)
EXPECTED_FREEZE_FILE_SHA256 = (
    "70d3983a6d2adf0f63ecb8894f870e2b1b2ae16cb2f03b31d24a9770bbdde853"
)
EXPECTED_LEDGER_SHA256 = (
    "b255ddbd5d2a47d03201315438f073d05e8716e569c77e90502d4ee395f7d9ce"
)

# The frozen ``g10_budget.json`` is *legitimately mutable*: every reservation,
# send-started marker, accepted settlement, or ambiguity record rewrites it.
# Its immutable genesis identity is therefore proven by a lineage digest over
# the fields no ledger operation may ever change (plus a revision marker), not
# by the SHA-256 of the live file. ``CreditReservationLedger.create`` re-stamps
# ``updated_utc`` inside ``_persist`` and the file is written with CRLF line
# endings, so byte-level reconstruction can never reproduce the genesis bytes;
# this digest is the equivalent lineage proof over the genesis fields.
LEDGER_GENESIS_LINEAGE_REVISION = (
    "g10-credit-reservation-ledger-genesis-lineage-v1"
)
LEDGER_GENESIS_LINEAGE_FIELDS = (
    "schema_version",
    "revision",
    "benchmark_id",
    "g10_preregistration_sha256",
    "g10a1_safety_sha256",
    "authorization_sha256",
    "scope_id",
    "maximum_credit_equivalent",
    "arm_credit_equivalent_ceilings",
    "created_utc",
)
EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256 = (
    "08e8ad93d7879dfcfb9ede8c66a095bde15a62b51081acd95b5267ba217ee6ff"
)

EXPECTED_MATCHED_PLAN_SHA256 = (
    "b18a3db4e7ea0ab02103e1e902b0d3e5290546e000fa8e1b94b1a1a70963f006"
)
EXPECTED_S0_MANIFEST_SHA256 = (
    "07e5aebc744a1d82a03f5688add19d2e23d9f2ee567a3890ac7552e1ae75a30b"
)
EXPECTED_G10A1_SAFETY_SHA256 = (
    "b9b6032f6bbb34a3805e7038c74c6d7b6dbd568e9b0fbdfb38775710e3c8292a"
)
EXPECTED_PRICING_VERSION = (
    "command-code-provider-public-ratecard-2026-09-29-g10-v1"
)

ARM_IDS = ("G10A", "G10B")
GLOBAL_CREDIT_EQUIVALENT_CEILING = "30"
ARM_CREDIT_EQUIVALENT_CEILINGS = {"G10A": "5", "G10B": "25"}

# The frozen ledger was created by G10E with authorization_sha256 == freeze_id.
# A new module SHA must NEVER be substituted here.
LEDGER_AUTHORIZATION_SHA256 = EXPECTED_FREEZE_ID

DEFAULT_QUARANTINE_OUTCOME = "ambiguous_remote_outcome"
LITERARY_CASE_QUARANTINE_SCOPE = "literary_model_case"
CHRONOLOGICAL_TRACK_QUARANTINE_SCOPE = "chronological_model_novel_track"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_REQUIRED_MARKER = "AUTHORIZATION_REQUIRED"

MODEL_RATECARD = copy.deepcopy(G10E_MODEL_RATECARD)

EXPECTED_PROVIDER_MODEL_IDS = {
    QWEN_MODEL_ID: "Qwen/Qwen3.8-Max-0902",
    GLM_MODEL_ID: "z-ai/glm-5.3-flash",
}


class G10PaidAuthorizationError(RuntimeError):
    """A paid G10F/G10G authorization is missing, stale, or unsafe."""


class G10PaidBudgetError(G10PaidAuthorizationError):
    """A G10 credit-equivalent ceiling cannot safely admit another send."""


class G10PaidQuarantineError(G10PaidAuthorizationError):
    """A durable G10 quarantine marker is invalid or conflicts."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10PaidAuthorizationError(
            "G10 paid value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G10PaidAuthorizationError(
            f"cannot hash file: {path}"
        ) from exc
    return digest.hexdigest()


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10PaidAuthorizationError(
            f"cannot read {label}: {target}"
        ) from exc
    if not isinstance(value, dict):
        raise G10PaidAuthorizationError(
            f"{label} must contain a JSON object: {target}"
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G10PaidAuthorizationError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G10PaidAuthorizationError(
            f"{label} must be a positive integer"
        )
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10PaidBudgetError(
            f"{label} must be a positive finite decimal"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G10PaidBudgetError(
            f"{label} must be a positive finite decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G10PaidBudgetError(
            f"{label} must be a positive finite decimal"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10PaidAuthorizationError(
            f"{label} must be a nonnegative finite decimal"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G10PaidAuthorizationError(
            f"{label} must be a nonnegative finite decimal"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise G10PaidAuthorizationError(
            f"{label} must be a nonnegative finite decimal"
        )
    return parsed


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G10PaidAuthorizationError(
            f"G10 paid root must not be a symlink: {root}"
        )
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G10PaidAuthorizationError(
                f"G10 paid tree contains a symlink: {item}"
            )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc_iso(clock: Any) -> str:
    if clock is None:
        return _utc_now().isoformat()
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G10PaidAuthorizationError(
            "G10 paid clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat()


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create one JSON artifact exactly once, then byte-verify it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise G10PaidAuthorizationError(
            f"G10 paid artifact already exists: {path}"
        ) from exc
    if path.read_bytes() != payload:
        raise G10PaidAuthorizationError(
            f"G10 paid artifact byte verification failed: {path}"
        )


def _validate_frozen_identities() -> None:
    if G10A1_SAFETY_SHA256 != EXPECTED_G10A1_SAFETY_SHA256:
        raise G10PaidAuthorizationError(
            "live G10A1 safety identity differs from the frozen literal"
        )
    if G10E_PRICING_VERSION != EXPECTED_PRICING_VERSION:
        raise G10PaidAuthorizationError(
            "G10E pricing version differs from the frozen G10F/G10G literal"
        )
    if MODEL_RATECARD != G10E_MODEL_RATECARD:
        raise G10PaidAuthorizationError("G10 ratecard drifted from G10E")
    if _decimal_text(G10E_GLOBAL_CEILING) != GLOBAL_CREDIT_EQUIVALENT_CEILING:
        raise G10PaidAuthorizationError("G10 global ceiling drifted from G10E")
    if {
        arm: _decimal_text(value) for arm, value in G10E_ARM_CEILINGS.items()
    } != ARM_CREDIT_EQUIVALENT_CEILINGS:
        raise G10PaidAuthorizationError("G10 arm ceilings drifted from G10E")
    if set(ARM_CREDIT_EQUIVALENT_CEILINGS) != set(ARM_IDS):
        raise G10PaidAuthorizationError("G10 arm roster is inconsistent")
    for model_id, row in MODEL_RATECARD.items():
        expected = EXPECTED_PROVIDER_MODEL_IDS.get(model_id)
        if expected is None or row.get("provider_model_id") != expected:
            raise G10PaidAuthorizationError(
                f"G10 provider model spelling drifted for {model_id!r}"
            )
    if DEFAULT_QUARANTINE_OUTCOME not in ALLOWED_QUARANTINE_OUTCOMES:
        raise G10PaidAuthorizationError(
            "G10 default quarantine outcome is not an allowed outcome"
        )
    if (
        LITERARY_CASE_QUARANTINE_SCOPE
        not in ALLOWED_QUARANTINE_SCOPE_KINDS
        or CHRONOLOGICAL_TRACK_QUARANTINE_SCOPE
        not in ALLOWED_QUARANTINE_SCOPE_KINDS
    ):
        raise G10PaidAuthorizationError(
            "G10 quarantine scope kinds drifted from the safety layer"
        )


_validate_frozen_identities()


def g10_paid_authorization_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G10F/G10G authorization contract."""
    return {
        "schema_version": G10PAID_SCHEMA_VERSION,
        "revision": G10PAID_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "repository_anchor": G10PAID_REPOSITORY_ANCHOR,
        "predecessor": {
            "g10e_contract_sha256": G10E_CONTRACT_SHA256,
            "g10a1_safety_sha256": EXPECTED_G10A1_SAFETY_SHA256,
            "g10e_predecessor_contracts": copy.deepcopy(PREDECESSOR_CONTRACTS),
        },
        "frozen_identity": {
            "freeze_id": EXPECTED_FREEZE_ID,
            "freeze_file_sha256": EXPECTED_FREEZE_FILE_SHA256,
            "budget_ledger_sha256": EXPECTED_LEDGER_SHA256,
            "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
            "s0_manifest_sha256": EXPECTED_S0_MANIFEST_SHA256,
            "pricing_version": EXPECTED_PRICING_VERSION,
            "model_ratecard": copy.deepcopy(MODEL_RATECARD),
        },
        "budget": {
            "global_credit_equivalent_ceiling": (
                GLOBAL_CREDIT_EQUIVALENT_CEILING
            ),
            "arm_credit_equivalent_ceilings": copy.deepcopy(
                ARM_CREDIT_EQUIVALENT_CEILINGS
            ),
            "ledger_authorization_source": "g10e_freeze_id",
            "request_level_reservation_before_transport": True,
            "reserve_and_send_started_precede_transport": True,
            "committed_ce_definition": (
                "accepted_ce_plus_full_reservations_for_unresolved_or_ambiguous"
                "_requests"
            ),
            "post_send_ambiguity_keeps_full_reservation": True,
            "pre_send_release_requires_proof_transport_never_started": True,
            "accepted_ce_from_frozen_ratecard_and_accepted_usage_only": True,
        },
        "authorization": {
            "explicit_operator_authorization_required": True,
            "deterministic_approval_token_required": True,
            "approval_token_is_single_use_per_request": True,
            "consumption_marker_required_before_send": True,
            "unchanged_target_state_fingerprint_required": True,
            "credentials_never_embedded_or_persisted": True,
        },
        "execution_scope": {
            "one_arm_per_plan": True,
            "one_model_novel_chapter_tuple_per_plan": True,
            "automatic_retry_forbidden": True,
            "automatic_chapter_advance_forbidden": True,
            "automatic_loop_forbidden": True,
            "provider_client_max_retries": 0,
        },
        "quarantine": {
            "literary_scope_kind": LITERARY_CASE_QUARANTINE_SCOPE,
            "chronological_scope_kind": CHRONOLOGICAL_TRACK_QUARANTINE_SCOPE,
            "allowed_outcomes": sorted(ALLOWED_QUARANTINE_OUTCOMES),
            "create_once_never_overwrite": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "separate_operator_authorization_required": True,
    }


G10PAID_AUTHORIZATION_SHA256 = _sha256_json(g10_paid_authorization_identity())


def assert_paid_execution_still_blocked() -> None:
    identity = g10_paid_authorization_identity()
    if identity["provider_calls"] != 0:
        raise G10PaidAuthorizationError(
            "G10 paid authorization unexpectedly reports Provider calls"
        )
    if identity["paid_execution_authorized"] is not False:
        raise G10PaidAuthorizationError(
            "G10 paid authorization unexpectedly authorizes paid execution"
        )


def accepted_credit_equivalent(model_id: str, usage: Any) -> Decimal:
    """Return the exact accepted CE from frozen Decimal rates and real usage.

    Token counts returned by the Provider are authoritative. A response without
    complete ``prompt_tokens``/``completion_tokens`` fails closed: paid
    accounting is never estimated. When the Provider does not expose cached
    prompt tokens, every prompt token is conservatively charged at the full
    input rate (the upper bound), never at the cheaper cache-read rate.
    """
    if model_id not in MODEL_RATECARD:
        raise G10PaidAuthorizationError(
            f"unknown G10 ratecard model: {model_id!r}"
        )
    if usage is None:
        raise G10PaidAuthorizationError(
            "accepted usage must be present to compute exact CE"
        )
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    for label, value in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise G10PaidAuthorizationError(
                f"accepted usage {label} is missing or malformed"
            )
    cached = getattr(usage, "cached_tokens", None)
    exposed = bool(getattr(usage, "cached_tokens_exposed", False))
    if not exposed or cached is None:
        cached_tokens = 0
    else:
        if (
            isinstance(cached, bool)
            or not isinstance(cached, int)
            or cached < 0
        ):
            raise G10PaidAuthorizationError(
                "accepted usage cached_tokens is malformed"
            )
        cached_tokens = cached
    if cached_tokens > prompt_tokens:
        raise G10PaidAuthorizationError(
            "accepted usage cached tokens exceed prompt tokens"
        )

    row = MODEL_RATECARD[model_id]
    input_rate = Decimal(str(row["input"]))
    output_rate = Decimal(str(row["output"]))
    cache_read_rate = Decimal(str(row["cache_read"]))
    miss_tokens = prompt_tokens - cached_tokens
    total = (
        Decimal(cached_tokens) * cache_read_rate
        + Decimal(miss_tokens) * input_rate
        + Decimal(completion_tokens) * output_rate
    ) / Decimal(1_000_000)
    return total


def target_state_fingerprint(
    root: Path | str,
    *,
    excluded_prefixes: tuple[str, ...] = (),
    excluded_files: tuple[str, ...] = (),
) -> str:
    """Hash one target state deterministically, ignoring explicit exclusions.

    The digest is the single source of truth for "the target state has not
    changed since the authorization was materialized". Only explicitly named
    operational paths (journal/ledger/log/lock state) are excluded.
    """
    target = Path(root).expanduser().resolve()
    if not target.is_dir():
        raise G10PaidAuthorizationError(
            f"target state root is missing: {target}"
        )
    _assert_no_symlinks(target)

    prefixes = tuple(excluded_prefixes)
    files = frozenset(excluded_files)
    rows = []
    for path in sorted(
        (item for item in target.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(target).as_posix(),
    ):
        relative = path.relative_to(target).as_posix()
        if relative in files:
            continue
        if any(relative.startswith(prefix) for prefix in prefixes):
            continue
        rows.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return _sha256_json(
        {
            "revision": G10PAID_STATE_FINGERPRINT_REVISION,
            "files": rows,
        }
    )


def _s0_run_records(
    s0_root: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Read and structurally verify the frozen S0 manifest run records."""
    from benchmark_longitudinal_g10_s0 import G10S0_MANIFEST_FILENAME

    manifest_path = s0_root / G10S0_MANIFEST_FILENAME
    manifest = _read_object(manifest_path, "G10 S0 roots manifest")
    check = copy.deepcopy(manifest)
    stored = check.pop("manifest_sha256", None)
    if _sha256_json(check) != stored:
        raise G10PaidAuthorizationError(
            "G10 S0 roots manifest SHA does not recompute"
        )
    if stored != EXPECTED_S0_MANIFEST_SHA256:
        raise G10PaidAuthorizationError(
            "G10 S0 roots manifest is not the frozen G10B artifact"
        )
    for key, expected in {
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": PREDECESSOR_CONTRACTS[
            "g10_preregistration_sha256"
        ],
        "g10a1_safety_sha256": EXPECTED_G10A1_SAFETY_SHA256,
        "run_count": 4,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }.items():
        if manifest.get(key) != expected:
            raise G10PaidAuthorizationError(
                f"G10 S0 manifest binding changed for {key}"
            )

    records = manifest.get("runs")
    if not isinstance(records, list) or len(records) != 4:
        raise G10PaidAuthorizationError(
            "G10 S0 manifest must bind exactly four runs"
        )
    by_run: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise G10PaidAuthorizationError(
                "G10 S0 manifest run record must be an object"
            )
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or run_id not in TRACK_BY_ID:
            raise G10PaidAuthorizationError(
                "G10 S0 manifest run id is unknown"
            )
        if run_id in by_run:
            raise G10PaidAuthorizationError(
                "G10 S0 manifest repeats a run id"
            )
        track = TRACK_BY_ID[run_id]
        if (
            record.get("model_id") != track.model_id
            or record.get("novel_id") != track.novel_id
            or record.get("relative_root") != f"runs/{run_id}"
        ):
            raise G10PaidAuthorizationError(
                f"G10 S0 manifest track layout changed for {run_id}"
            )
        by_run[run_id] = copy.deepcopy(dict(record))
    if set(by_run) != set(TRACK_BY_ID):
        raise G10PaidAuthorizationError(
            "G10 S0 manifest track roster changed"
        )
    return by_run, stored


def ledger_genesis_lineage_identity(document: Any) -> dict[str, Any]:
    """Return the immutable genesis-lineage payload of one ledger document.

    Only ``LEDGER_GENESIS_LINEAGE_FIELDS`` participate, copied verbatim (no
    coercion), so a legitimately mutated descendant ledger keeps the exact
    genesis digest. The mutable ledger state (``accepted_credit_equivalent``,
    ``records``, ``updated_utc``, ``ledger_sha256``) is deliberately excluded.
    """
    if not isinstance(document, Mapping):
        raise G10PaidAuthorizationError(
            "G10 budget ledger document must be a mapping"
        )
    value = copy.deepcopy(dict(document))
    payload: dict[str, Any] = {
        "lineage_revision": LEDGER_GENESIS_LINEAGE_REVISION
    }
    for field in LEDGER_GENESIS_LINEAGE_FIELDS:
        if field not in value:
            raise G10PaidAuthorizationError(
                f"G10 budget ledger lacks the genesis field {field}"
            )
        payload[field] = value[field]
    return payload


def ledger_genesis_lineage_sha256(document: Any) -> str:
    """Return the canonical SHA-256 of one ledger's genesis lineage payload."""
    return _sha256_json(ledger_genesis_lineage_identity(document))


def assert_ledger_genesis_lineage(document: Any) -> str:
    """Prove one ledger still carries the frozen genesis identity exactly."""
    digest = ledger_genesis_lineage_sha256(document)
    if digest != EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256:
        raise G10PaidAuthorizationError(
            "G10 budget ledger does not preserve the frozen genesis lineage"
        )
    return digest


def open_frozen_budget_ledger(
    binding: Mapping[str, Any],
    *,
    clock: Any = None,
) -> CreditReservationLedger:
    """Open the frozen single-writer CE ledger with the G10E freeze ID.

    The ledger is opened with the G10E freeze ID (its ``authorization_sha256``)
    and its immutable genesis lineage is re-proven from the live document. The
    file itself is allowed to be a legitimately mutated descendant.
    """
    _validate_binding(binding)
    ledger = CreditReservationLedger.open(
        path=binding["budget_ledger_path"],
        authorization_sha256=LEDGER_AUTHORIZATION_SHA256,
        scope_id=G10_BENCHMARK_ID,
        clock=clock,
    )
    assert_ledger_genesis_lineage(ledger.document)
    if (
        _decimal_text(ledger.maximum_credit_equivalent)
        != GLOBAL_CREDIT_EQUIVALENT_CEILING
    ):
        raise G10PaidBudgetError(
            "frozen G10 global budget-ledger ceiling changed"
        )
    for arm_id, expected in ARM_CREDIT_EQUIVALENT_CEILINGS.items():
        if _decimal_text(ledger.arm_credit_equivalent_ceiling(arm_id)) != expected:
            raise G10PaidBudgetError(
                f"frozen G10 {arm_id} budget-ledger ceiling changed"
            )
    return ledger


def frozen_execution_binding(
    *,
    production_root: Path | str,
    freeze_root: Path | str,
    matched_evidence_root: Path | str,
    s0_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Bind the live frozen G10E artifacts read-only; fail closed on any drift.

    Unlike ``benchmark_longitudinal_g10e_freeze.verify_g10e`` this deliberately
    does **not** require an untouched zero-commit ledger: the paid executors run
    exactly when reservations already exist. Every frozen G10E binding (freeze ID,
    freeze-file SHA-256, budget ceilings, pricing, matched plan, S0 manifest,
    zero Provider calls, paid execution not authorized by the freeze itself) is
    re-proven here from the live artifacts.

    The budget ledger is proven in two deliberately separate parts:

    * **genesis lineage (frozen):** the ledger document must still carry the
      exact immutable genesis identity (``created_utc``, authorization, scope,
      ceilings, preregistration/safety identities, schema and revision) as the
      canonical ``EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256`` digest, and it is
      re-proven on every open by ``open_frozen_budget_ledger``;
    * **live ledger (mutable evidence):** the current file SHA-256 is reported as
      ``budget_ledger_current_sha256`` evidence only. It is never compared to a
      frozen constant, because every legitimate reservation, settlement, or
      ambiguity record rewrites the file.

    ``budget_ledger_sha256`` keeps the historical pristine-genesis file SHA-256
    (``EXPECTED_LEDGER_SHA256``) as contract evidence; it is the genesis
    artifact's hash, not a claim about the current file. The final live proof is
    ``open_frozen_budget_ledger(binding)``.

    ``repo_root`` is the repository whose ``novels/`` tree the matched plan was
    frozen from. It defaults to ``production_root``, which is the read-only
    authoritative novel source in the frozen deployment.
    """
    production = Path(production_root).expanduser().resolve()
    freeze = Path(freeze_root).expanduser().resolve()
    matched = Path(matched_evidence_root).expanduser().resolve()
    s0 = Path(s0_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve() if repo_root else production

    for label, path in {
        "production_root": production,
        "freeze_root": freeze,
        "matched_evidence_root": matched,
        "s0_root": s0,
    }.items():
        if not path.is_dir():
            raise G10PaidAuthorizationError(
                f"G10 {label} is missing or not a directory: {path}"
            )

    freeze_file = freeze / G10E_FREEZE_FILENAME
    ledger_file = freeze / G10E_BUDGET_LEDGER_FILENAME
    if not freeze_file.is_file() or not ledger_file.is_file():
        raise G10PaidAuthorizationError(
            "G10E freeze artifacts are incomplete"
        )

    document = _read_object(freeze_file, "G10E freeze document")
    freeze_id = document.get("freeze_id")
    payload = copy.deepcopy(document)
    payload.pop("freeze_id", None)
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G10PaidAuthorizationError(
            "G10E freeze ID does not recompute"
        )
    if freeze_id != EXPECTED_FREEZE_ID:
        raise G10PaidAuthorizationError(
            "G10E freeze ID differs from the frozen G10F/G10G literal"
        )
    if document.get("g10e_contract_sha256") != G10E_CONTRACT_SHA256:
        raise G10PaidAuthorizationError("G10E contract binding changed")
    if document.get("predecessor_contracts") != PREDECESSOR_CONTRACTS:
        raise G10PaidAuthorizationError(
            "G10E predecessor bindings changed"
        )
    if document.get("pricing") != g10e_contract_identity()["pricing"]:
        raise G10PaidAuthorizationError("G10E pricing snapshot changed")
    if document.get("budget") != g10e_contract_identity()["budget"]:
        raise G10PaidAuthorizationError("G10E budget policy changed")
    if document.get("provider_calls") != 0:
        raise G10PaidAuthorizationError(
            "G10E freeze unexpectedly reports Provider calls"
        )
    if document.get("paid_execution_authorized") is not False:
        raise G10PaidAuthorizationError(
            "G10E freeze unexpectedly authorizes paid execution"
        )

    freeze_file_sha256 = _sha256_file(freeze_file)
    if freeze_file_sha256 != EXPECTED_FREEZE_FILE_SHA256:
        raise G10PaidAuthorizationError(
            "G10E freeze file SHA-256 differs from the frozen literal"
        )
    budget_ledger_current_sha256 = _sha256_file(ledger_file)

    matched_plan = matched_plan_identity(
        repo_root=repo,
        evidence_root=matched,
        forbidden_roots=(production, s0, freeze, *forbidden_roots),
    )
    matched_sha = _sha256_json(matched_plan)
    if matched_sha != EXPECTED_MATCHED_PLAN_SHA256:
        raise G10PaidAuthorizationError(
            "G10 matched plan SHA-256 differs from the frozen literal"
        )
    if document["matched"]["matched_plan_sha256"] != matched_sha:
        raise G10PaidAuthorizationError(
            "G10E freeze binds another matched plan"
        )
    if document["matched"]["request_count"] != matched_plan.get(
        "request_count"
    ):
        raise G10PaidAuthorizationError(
            "G10 matched plan request count changed"
        )

    s0_records, observed_manifest_sha = _s0_run_records(s0)
    if (
        document["chronological"]["s0_manifest_sha256"]
        != observed_manifest_sha
    ):
        raise G10PaidAuthorizationError(
            "G10E freeze binds another S0 manifest"
        )

    if G10A1_SAFETY_SHA256 != EXPECTED_G10A1_SAFETY_SHA256:
        raise G10PaidAuthorizationError(
            "live G10A1 safety identity differs from the frozen literal"
        )

    binding = {
        "schema_version": G10PAID_SCHEMA_VERSION,
        "revision": G10PAID_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
        "freeze_id": freeze_id,
        "freeze_file_sha256": freeze_file_sha256,
        "freeze_file_path": str(freeze_file),
        "freeze_root": str(freeze),
        "budget_ledger_sha256": EXPECTED_LEDGER_SHA256,
        "budget_ledger_current_sha256": budget_ledger_current_sha256,
        "budget_ledger_genesis_lineage_sha256": (
            EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
        ),
        "budget_ledger_path": str(ledger_file),
        "maximum_credit_equivalent": GLOBAL_CREDIT_EQUIVALENT_CEILING,
        "arm_credit_equivalent_ceilings": copy.deepcopy(
            ARM_CREDIT_EQUIVALENT_CEILINGS
        ),
        "matched_plan_sha256": matched_sha,
        "s0_manifest_sha256": observed_manifest_sha,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "pricing_version": EXPECTED_PRICING_VERSION,
        "model_ratecard": copy.deepcopy(MODEL_RATECARD),
        "read_only_production_root": str(production),
        "matched_evidence_root": str(matched),
        "s0_root": str(s0),
        "repo_root": str(repo),
        "s0_runs": {
            run_id: {
                "relative_root": record["relative_root"],
                "model_id": record["model_id"],
                "novel_id": record["novel_id"],
                "run_identity_sha256": record.get("run_identity_sha256"),
            }
            for run_id, record in sorted(s0_records.items())
        },
    }
    ledger = open_frozen_budget_ledger(binding)

    return {
        **binding,
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "remaining_credit_equivalent": _decimal_text(
            ledger.remaining_credit_equivalent
        ),
        "reserved_credit_equivalent_at_binding": _decimal_text(
            ledger.committed_credit_equivalent - ledger.accepted_credit_equivalent
        ),
        "all_frozen_identities_verified": True,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


_BINDING_FROZEN_FIELDS = {
    "freeze_id": EXPECTED_FREEZE_ID,
    "freeze_file_sha256": EXPECTED_FREEZE_FILE_SHA256,
    "budget_ledger_sha256": EXPECTED_LEDGER_SHA256,
    "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
    "s0_manifest_sha256": EXPECTED_S0_MANIFEST_SHA256,
    "g10a1_safety_sha256": EXPECTED_G10A1_SAFETY_SHA256,
    "pricing_version": EXPECTED_PRICING_VERSION,
    "maximum_credit_equivalent": GLOBAL_CREDIT_EQUIVALENT_CEILING,
}


def _validate_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Prove a binding still pins every frozen identity exactly."""
    if not isinstance(binding, Mapping):
        raise G10PaidAuthorizationError(
            "G10 execution binding must be a mapping"
        )
    value = copy.deepcopy(dict(binding))
    for field, expected in _BINDING_FROZEN_FIELDS.items():
        if value.get(field) != expected:
            raise G10PaidAuthorizationError(
                f"G10 execution binding changed for {field}"
            )
    if (
        value.get("arm_credit_equivalent_ceilings")
        != ARM_CREDIT_EQUIVALENT_CEILINGS
    ):
        raise G10PaidAuthorizationError(
            "G10 execution binding arm ceilings changed"
        )
    if value.get("model_ratecard") != MODEL_RATECARD:
        raise G10PaidAuthorizationError(
            "G10 execution binding ratecard changed"
        )
    for field in (
        "budget_ledger_path",
        "read_only_production_root",
        "matched_evidence_root",
        "s0_root",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise G10PaidAuthorizationError(
                f"G10 execution binding lacks {field}"
            )
    if not isinstance(value.get("s0_runs"), Mapping) or set(
        value["s0_runs"]
    ) != set(TRACK_BY_ID):
        raise G10PaidAuthorizationError(
            "G10 execution binding track roster changed"
        )
    # Evidence-only optional fields. Neither is a frozen constant of the binding
    # contract, so an existing binding without them stays valid (synthetic
    # executor bindings must keep working); when present they are proven well
    # formed, and a wrong genesis lineage fails closed.
    live_ledger_sha256 = value.get("budget_ledger_current_sha256")
    if live_ledger_sha256 is not None and (
        not isinstance(live_ledger_sha256, str)
        or not _SHA256_RE.fullmatch(live_ledger_sha256)
    ):
        raise G10PaidAuthorizationError(
            "G10 execution binding current ledger SHA is malformed"
        )
    genesis_lineage_sha256 = value.get(
        "budget_ledger_genesis_lineage_sha256"
    )
    if (
        genesis_lineage_sha256 is not None
        and genesis_lineage_sha256 != EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    ):
        raise G10PaidAuthorizationError(
            "G10 execution binding genesis lineage changed"
        )
    return value


def validate_execution_binding(
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Public fail-closed validation of one frozen execution binding."""
    return _validate_binding(binding)


def _validate_identity_tuple(
    *,
    arm_id: str,
    model_id: str,
    novel_id: str,
    chapter: Any,
    case_kind: Any,
    execution_index: Any,
    run_id: Any,
    track_effort: Any,
) -> dict[str, Any]:
    """Prove one arm identity tuple is exactly the frozen planned tuple."""
    if arm_id not in ARM_IDS:
        raise G10PaidAuthorizationError(
            f"unknown G10 arm: {arm_id!r}"
        )
    if model_id not in MODEL_RATECARD:
        raise G10PaidAuthorizationError(
            f"unknown G10 model: {model_id!r}"
        )
    if novel_id not in NOVELS:
        raise G10PaidAuthorizationError(
            f"unknown G10 novel: {novel_id!r}"
        )
    if (
        isinstance(chapter, bool)
        or not isinstance(chapter, int)
        or chapter < 1
    ):
        raise G10PaidAuthorizationError(
            "G10 plan chapter must be a positive integer"
        )

    if arm_id == "G10A":
        if (
            isinstance(execution_index, bool)
            or not isinstance(execution_index, int)
            or not 1 <= execution_index <= G10A2_REQUEST_COUNT
        ):
            raise G10PaidAuthorizationError(
                "G10A requires exactly one execution_index within "
                f"1..{G10A2_REQUEST_COUNT}"
            )
        if case_kind not in {"primary", "bridge"}:
            raise G10PaidAuthorizationError(
                "G10A requires a frozen case_kind"
            )
        if run_id is not None:
            raise G10PaidAuthorizationError(
                "G10A must not bind a chronological run id"
            )
        if track_effort is not None:
            raise G10PaidAuthorizationError(
                "G10A must not bind a chronological track effort"
            )
        pair = PAIR_SPECS[(execution_index + 1) // 2 - 1]
        second = (
            GLM_MODEL_ID
            if pair.first_model_id == QWEN_MODEL_ID
            else QWEN_MODEL_ID
        )
        expected_model = (
            pair.first_model_id if execution_index % 2 else second
        )
        expected = {
            "model_id": expected_model,
            "novel_id": pair.novel_id,
            "chapter": pair.chapter,
            "case_kind": pair.case_kind,
        }
        observed = {
            "model_id": model_id,
            "novel_id": novel_id,
            "chapter": chapter,
            "case_kind": case_kind,
        }
        if observed != expected:
            raise G10PaidAuthorizationError(
                "G10A identity tuple is not the frozen matched-plan case"
            )
        return {
            "arm_id": "G10A",
            **expected,
            "execution_index": execution_index,
            "run_id": None,
            "track_effort": None,
            "pair_id": pair.pair_id,
            "pair_index": pair.pair_index,
        }

    if run_id not in TRACK_BY_ID:
        raise G10PaidAuthorizationError(
            f"unknown G10B run id: {run_id!r}"
        )
    if case_kind is not None or execution_index is not None:
        raise G10PaidAuthorizationError(
            "G10B must not bind a literary case_kind/execution_index"
        )
    track = TRACK_BY_ID[run_id]
    if model_id != track.model_id or novel_id != track.novel_id:
        raise G10PaidAuthorizationError(
            "G10B track identity does not match its model/novel"
        )
    if chapter > G10B1_LAST_CHAPTER:
        raise G10PaidAuthorizationError(
            f"G10B target chapter must be within 1..{G10B1_LAST_CHAPTER}"
        )
    if track_effort is not None and track_effort != track.translation_effort:
        raise G10PaidAuthorizationError(
            "G10B track translation effort drifted"
        )
    return {
        "arm_id": "G10B",
        "model_id": model_id,
        "novel_id": novel_id,
        "chapter": chapter,
        "case_kind": None,
        "execution_index": None,
        "run_id": run_id,
        "track_effort": track.translation_effort,
    }


def build_paid_authorization(
    *,
    binding: Mapping[str, Any],
    arm_id: str,
    model_id: str,
    provider_model_id: str,
    logical_operation: str,
    novel_id: str,
    chapter: int,
    case_kind: str | None = None,
    execution_index: int | None = None,
    run_id: str | None = None,
    track_effort: str | None = None,
    request_sha256: str | None = None,
    target_state_fingerprint_sha256: str,
    maximum_credit_equivalent: Any,
    maximum_new_provider_sends: int = 1,
    scope_id: str = G10_BENCHMARK_ID,
) -> dict[str, Any]:
    """Build one deterministic, single-use paid authorization plan."""
    value = _validate_binding(binding)
    identity = _validate_identity_tuple(
        arm_id=arm_id,
        model_id=model_id,
        novel_id=novel_id,
        chapter=chapter,
        case_kind=case_kind,
        execution_index=execution_index,
        run_id=run_id,
        track_effort=track_effort,
    )
    if not isinstance(logical_operation, str) or not logical_operation.strip():
        raise G10PaidAuthorizationError(
            "paid authorization logical_operation must be non-empty"
        )
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise G10PaidAuthorizationError(
            "paid authorization scope_id must be non-empty"
        )
    if scope_id != G10_BENCHMARK_ID:
        raise G10PaidAuthorizationError(
            "paid authorization must bind the frozen benchmark scope"
        )
    expected_provider_model = MODEL_RATECARD[model_id]["provider_model_id"]
    if provider_model_id != expected_provider_model:
        raise G10PaidAuthorizationError(
            "paid authorization provider model spelling is not the frozen "
            f"exact identity for {model_id!r}"
        )
    state_sha = _require_sha256(
        target_state_fingerprint_sha256,
        "target_state_fingerprint_sha256",
    )
    request_sha = (
        None
        if request_sha256 is None
        else _require_sha256(request_sha256, "request_sha256")
    )
    maximum = _positive_decimal(
        maximum_credit_equivalent, "maximum_credit_equivalent"
    )
    sends = _positive_int(
        maximum_new_provider_sends, "maximum_new_provider_sends"
    )

    plan = {
        "schema_version": G10PAID_SCHEMA_VERSION,
        "revision": G10PAID_REVISION,
        "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "scope_id": scope_id,
        "arm_id": identity["arm_id"],
        "model_id": model_id,
        "provider_model_id": provider_model_id,
        "logical_operation": logical_operation.strip(),
        "novel_id": novel_id,
        "chapter": chapter,
        "case_kind": identity["case_kind"],
        "execution_index": identity["execution_index"],
        "run_id": identity["run_id"],
        "track_effort": identity["track_effort"],
        "pair_id": identity.get("pair_id"),
        "request_sha256": request_sha,
        "target_state_fingerprint_sha256": state_sha,
        "maximum_credit_equivalent": _decimal_text(maximum),
        "maximum_new_provider_sends": sends,
        "freeze_id": value["freeze_id"],
        "freeze_file_sha256": value["freeze_file_sha256"],
        "budget_ledger_sha256": value["budget_ledger_sha256"],
        "matched_plan_sha256": value["matched_plan_sha256"],
        "s0_manifest_sha256": value["s0_manifest_sha256"],
        "g10a1_safety_sha256": value["g10a1_safety_sha256"],
        "pricing_version": value["pricing_version"],
        "ratecard_row": copy.deepcopy(MODEL_RATECARD[model_id]),
        "operator_authorization_required": True,
        "automatic_retry_forbidden": True,
        "automatic_chapter_advance_forbidden": True,
        "provider_calls": 0,
    }
    return {**plan, "approval_token": _sha256_json(plan)}


def _require_token(plan: Mapping[str, Any], expected_approval_token: Any) -> str:
    value = copy.deepcopy(dict(plan))
    token = value.pop("approval_token", None)
    if not isinstance(token, str) or not _SHA256_RE.fullmatch(token):
        raise G10PaidAuthorizationError(
            "paid authorization approval token is invalid"
        )
    if _sha256_json(value) != token:
        raise G10PaidAuthorizationError(
            "paid authorization approval token does not recompute"
        )
    return _require_sha256(token, "approval_token")


def assert_operator_authorization_present(operator_authorized: Any) -> None:
    """Fail closed unless an explicit operator authorization is present."""
    if operator_authorized is not True:
        raise G10PaidAuthorizationError(
            f"{_AUTHORIZATION_REQUIRED_MARKER}: explicit operator "
            "authorization is required before any paid send"
        )


def _read_consumption_marker(path: Path) -> dict[str, Any]:
    return _read_object(path, "paid authorization consumption marker")


def verify_paid_authorization(
    *,
    plan: Mapping[str, Any],
    expected_approval_token: str,
    current_state_fingerprint_sha256: str,
    operator_authorized: Any,
    binding: Mapping[str, Any],
    consumption_path: Path | str | None = None,
) -> dict[str, Any]:
    """Prove one paid authorization before any client/ledger/send exists."""
    if not isinstance(plan, Mapping):
        raise G10PaidAuthorizationError(
            "paid authorization plan must be a mapping"
        )
    value = copy.deepcopy(dict(plan))
    token = _require_token(value, expected_approval_token)
    if token != _require_sha256(
        expected_approval_token, "expected_approval_token"
    ):
        raise G10PaidAuthorizationError(
            "paid authorization approval token mismatch"
        )
    if (
        value.get("g10_paid_authorization_sha256")
        != G10PAID_AUTHORIZATION_SHA256
    ):
        raise G10PaidAuthorizationError(
            "paid authorization binds another G10F/G10G contract"
        )
    if value.get("schema_version") != G10PAID_SCHEMA_VERSION:
        raise G10PaidAuthorizationError(
            "unsupported paid authorization schema"
        )
    if value.get("revision") != G10PAID_REVISION:
        raise G10PaidAuthorizationError(
            "unsupported paid authorization revision"
        )
    if value.get("benchmark_id") != G10_BENCHMARK_ID:
        raise G10PaidAuthorizationError(
            "paid authorization benchmark identity changed"
        )
    if value.get("provider_calls") != 0:
        raise G10PaidAuthorizationError(
            "materialized paid authorization must report zero Provider calls"
        )

    bound = _validate_binding(binding)
    for field in (
        "freeze_id",
        "freeze_file_sha256",
        "budget_ledger_sha256",
        "matched_plan_sha256",
        "s0_manifest_sha256",
        "g10a1_safety_sha256",
        "pricing_version",
    ):
        if value.get(field) != bound[field]:
            raise G10PaidAuthorizationError(
                f"paid authorization binding changed for {field}"
            )
    if value.get("model_id") not in MODEL_RATECARD:
        raise G10PaidAuthorizationError(
            "paid authorization binds an unknown G10 model"
        )
    if value.get("ratecard_row") != MODEL_RATECARD[value["model_id"]]:
        raise G10PaidAuthorizationError(
            "paid authorization ratecard row changed"
        )

    identity = _validate_identity_tuple(
        arm_id=value.get("arm_id"),
        model_id=value.get("model_id"),
        novel_id=value.get("novel_id"),
        chapter=value.get("chapter"),
        case_kind=value.get("case_kind"),
        execution_index=value.get("execution_index"),
        run_id=value.get("run_id"),
        track_effort=value.get("track_effort"),
    )
    if value.get("pair_id") != identity.get("pair_id"):
        raise G10PaidAuthorizationError(
            "paid authorization pair binding changed"
        )
    if value.get("provider_model_id") != MODEL_RATECARD[
        value["model_id"]
    ]["provider_model_id"]:
        raise G10PaidAuthorizationError(
            "paid authorization provider model identity changed"
        )
    if value.get("scope_id") != G10_BENCHMARK_ID:
        raise G10PaidAuthorizationError(
            "paid authorization scope changed"
        )
    if not isinstance(value.get("logical_operation"), str) or not value[
        "logical_operation"
    ].strip():
        raise G10PaidAuthorizationError(
            "paid authorization logical operation changed"
        )
    request_sha = value.get("request_sha256")
    if request_sha is not None:
        _require_sha256(request_sha, "paid authorization request_sha256")
    _positive_decimal(
        value.get("maximum_credit_equivalent"),
        "paid authorization maximum_credit_equivalent",
    )
    _positive_int(
        value.get("maximum_new_provider_sends"),
        "paid authorization maximum_new_provider_sends",
    )
    if value.get("operator_authorization_required") is not True:
        raise G10PaidAuthorizationError(
            "paid authorization operator requirement changed"
        )
    if value.get("automatic_retry_forbidden") is not True:
        raise G10PaidAuthorizationError(
            "paid authorization retry prohibition changed"
        )
    if value.get("automatic_chapter_advance_forbidden") is not True:
        raise G10PaidAuthorizationError(
            "paid authorization chapter-advance prohibition changed"
        )

    current_state = _require_sha256(
        current_state_fingerprint_sha256,
        "current_state_fingerprint_sha256",
    )
    if value.get("target_state_fingerprint_sha256") != current_state:
        raise G10PaidAuthorizationError(
            "paid authorization target state changed after planning"
        )

    consumption = None
    if consumption_path is not None:
        consumption = Path(consumption_path)
        if consumption.exists() and not consumption.is_file():
            raise G10PaidAuthorizationError(
                "paid authorization consumption path is not a file"
            )
        if consumption.is_file():
            marker = _read_consumption_marker(consumption)
            if marker.get("approval_token") != token:
                raise G10PaidAuthorizationError(
                    "paid authorization consumption marker binds another token"
                )
            if marker.get("send_started") is True:
                raise G10PaidAuthorizationError(
                    "paid authorization approval token is already consumed by "
                    "a started transport; a second send under this token is "
                    "forbidden"
                )

    assert_operator_authorization_present(operator_authorized)

    return {
        "authorized": True,
        "approval_token": token,
        "arm_id": value["arm_id"],
        "model_id": value["model_id"],
        "provider_model_id": value["provider_model_id"],
        "novel_id": value["novel_id"],
        "chapter": value["chapter"],
        "case_kind": value["case_kind"],
        "execution_index": value["execution_index"],
        "run_id": value["run_id"],
        "logical_operation": value["logical_operation"],
        "request_sha256": request_sha,
        "target_state_fingerprint_sha256": current_state,
        "maximum_credit_equivalent": value["maximum_credit_equivalent"],
        "maximum_new_provider_sends": value["maximum_new_provider_sends"],
        "consumption_path": (
            None if consumption is None else str(consumption)
        ),
        "consumed_send_started": False,
        "plan": value,
        "provider_calls": 0,
    }


def authorization_consumption_path(
    root: Path | str,
    approval_token: str,
) -> Path:
    """Return the create-once consumption marker path for one token."""
    token = _require_sha256(approval_token, "approval_token")
    return (
        Path(root)
        / G10PAID_AUTHORIZATION_DIRNAME
        / f"{token}.json"
    )


def paid_artifact_dir(root: Path | str, *parts: str) -> Path:
    """Return one deterministic paid-artifact directory under a root."""
    base = Path(root) / G10PAID_ARTIFACT_DIRNAME
    result = base
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise G10PaidAuthorizationError(
                "paid artifact path component must be a non-empty string"
            )
        result = result / part
    return result


def record_authorization_consumption(
    *,
    path: Path | str,
    approval_token: str,
    request_sha256: str,
    reservation_id: str | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Durably consume one approval token before its single transport attempt."""
    target = Path(path)
    token = _require_sha256(approval_token, "approval_token")
    request_sha = _require_sha256(request_sha256, "request_sha256")
    if reservation_id is not None:
        reservation_id = _require_sha256(reservation_id, "reservation_id")
    if target.exists():
        marker = _read_consumption_marker(target)
        if marker.get("approval_token") != token:
            raise G10PaidAuthorizationError(
                "paid authorization consumption marker binds another token"
            )
        if (
            marker.get("request_sha256") != request_sha
            or marker.get("reservation_id") != reservation_id
        ):
            raise G10PaidAuthorizationError(
                "paid authorization consumption marker binds another request"
            )
        if marker.get("send_started") is True:
            raise G10PaidAuthorizationError(
                "paid authorization approval token is already consumed by a "
                "started transport"
            )
        return marker
    marker = {
        "schema_version": G10PAID_SCHEMA_VERSION,
        "revision": G10PAID_REVISION,
        "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "freeze_id": EXPECTED_FREEZE_ID,
        "approval_token": token,
        "request_sha256": request_sha,
        "reservation_id": reservation_id,
        "send_started": True,
        "created_utc": _aware_utc_iso(clock),
    }
    _write_new_json(target, marker)
    return marker


def preflight_and_reserve(
    *,
    ledger: CreditReservationLedger,
    plan: Mapping[str, Any],
    request_sha256: str,
    reserved_credit_equivalent: Any,
) -> dict[str, Any]:
    """Check global+arm ceilings, then reserve strictly before transport."""
    if not isinstance(plan, Mapping):
        raise G10PaidBudgetError(
            "paid reservation requires an authorization plan"
        )
    value = copy.deepcopy(dict(plan))
    arm_id = value.get("arm_id")
    if arm_id not in ARM_CREDIT_EQUIVALENT_CEILINGS:
        raise G10PaidBudgetError(f"unknown G10 arm: {arm_id!r}")
    scope_id = value.get("scope_id")
    if scope_id != G10_BENCHMARK_ID:
        raise G10PaidBudgetError("paid reservation scope changed")
    for label in ("logical_operation", "model_id", "novel_id"):
        if not isinstance(value.get(label), str) or not value[label].strip():
            raise G10PaidBudgetError(
                f"paid reservation {label} must be non-empty"
            )
    _positive_int(value.get("chapter"), "paid reservation chapter")
    request_sha = _require_sha256(request_sha256, "request_sha256")
    reserved = _positive_decimal(
        reserved_credit_equivalent, "reserved_credit_equivalent"
    )

    projected_global = ledger.committed_credit_equivalent + reserved
    if projected_global > ledger.maximum_credit_equivalent:
        raise G10PaidBudgetError(
            "G10 global credit-equivalent ceiling would be exceeded by "
            "reservation: "
            f"committed={_decimal_text(ledger.committed_credit_equivalent)}, "
            f"ceiling={_decimal_text(ledger.maximum_credit_equivalent)}, "
            f"requested={_decimal_text(reserved)}"
        )
    projected_arm = (
        ledger.arm_committed_credit_equivalent(arm_id) + reserved
    )
    if projected_arm > ledger.arm_credit_equivalent_ceiling(arm_id):
        raise G10PaidBudgetError(
            f"G10 {arm_id} arm credit-equivalent ceiling would be exceeded by "
            "reservation: "
            f"committed={_decimal_text(ledger.arm_committed_credit_equivalent(arm_id))}, "
            f"ceiling={_decimal_text(ledger.arm_credit_equivalent_ceiling(arm_id))}, "
            f"requested={_decimal_text(reserved)}"
        )

    try:
        record = ledger.reserve(
            request_sha256=request_sha,
            arm_id=arm_id,
            scope_id=scope_id,
            logical_operation=value["logical_operation"],
            model_id=value["model_id"],
            novel_id=value["novel_id"],
            chapter=value["chapter"],
            reserved_credit_equivalent=reserved_credit_equivalent,
        )
    except G10SafetyError as exc:
        raise G10PaidBudgetError(
            f"G10 reservation was rejected before transport: {exc}"
        ) from exc

    return {
        **copy.deepcopy(record),
        "requested_reserved_credit_equivalent": _decimal_text(reserved),
        "committed_credit_equivalent_after": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "remaining_credit_equivalent_after": _decimal_text(
            ledger.remaining_credit_equivalent
        ),
        "send_started": False,
    }


def reservation_record(
    ledger: CreditReservationLedger,
    reservation_id: str,
) -> dict[str, Any]:
    """Return the one durable reservation record for an id, or fail closed."""
    reservation = _require_sha256(reservation_id, "reservation_id")
    matches = [
        record
        for record in ledger.document["records"]
        if record.get("reservation_id") == reservation
    ]
    if len(matches) != 1:
        raise G10PaidBudgetError(
            "G10 reservation does not resolve uniquely"
        )
    return matches[0]


def mark_send_started(
    *,
    ledger: CreditReservationLedger,
    reservation_id: str,
) -> dict[str, Any]:
    """Persist the send-started marker strictly before the transport call."""
    try:
        record = ledger.mark_send_started(reservation_id)
    except G10SafetyError as exc:
        raise G10PaidBudgetError(
            f"G10 send-started marker was rejected: {exc}"
        ) from exc
    return copy.deepcopy(record)


def settle_accepted(
    *,
    ledger: CreditReservationLedger,
    reservation_id: str,
    accepted_credit_equivalent: Any,
) -> dict[str, Any]:
    """Replace one reservation with its exact accepted CE."""
    accepted = _nonnegative_decimal(
        accepted_credit_equivalent, "accepted_credit_equivalent"
    )
    try:
        record = ledger.record_accepted(
            reservation_id,
            accepted_credit_equivalent=accepted,
        )
    except G10SafetyError as exc:
        if ledger.has_terminal_budget_overrun:
            raise G10PaidBudgetError(
                "G10 accepted usage exceeded its conservative reservation; "
                "the ledger is terminal and no further paid work is allowed"
            ) from exc
        raise G10PaidBudgetError(
            f"G10 accepted settlement was rejected: {exc}"
        ) from exc
    return {
        **copy.deepcopy(record),
        "accepted_credit_equivalent_exact": _decimal_text(accepted),
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "remaining_credit_equivalent": _decimal_text(
            ledger.remaining_credit_equivalent
        ),
    }


def settle_ambiguous(
    *,
    ledger: CreditReservationLedger,
    reservation_id: str,
    reason: str,
) -> dict[str, Any]:
    """Keep the full reservation for one started but unresolved request."""
    if not isinstance(reason, str) or not reason.strip():
        raise G10PaidBudgetError(
            "ambiguous outcome reason must be non-empty"
        )
    try:
        record = ledger.mark_ambiguous(reservation_id, reason=reason)
    except G10SafetyError as exc:
        raise G10PaidBudgetError(
            f"G10 ambiguous settlement was rejected: {exc}"
        ) from exc
    return {
        **copy.deepcopy(record),
        "full_reservation_retained": True,
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "disposition": ledger.resume_disposition(reservation_id)["disposition"],
    }


def settle_pre_send_release(
    *,
    ledger: CreditReservationLedger,
    reservation_id: str,
    transport_never_started_proven: bool,
) -> dict[str, Any]:
    """Release one reservation only when no transport ever started."""
    try:
        record = ledger.release_pre_send(
            reservation_id,
            transport_never_started_proven=transport_never_started_proven,
        )
    except G10SafetyError as exc:
        raise G10PaidBudgetError(
            f"G10 pre-send release was rejected: {exc}"
        ) from exc
    return {
        **copy.deepcopy(record),
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "remaining_credit_equivalent": _decimal_text(
            ledger.remaining_credit_equivalent
        ),
    }


def _resolve_outcome(outcome: Any) -> str:
    if outcome in ALLOWED_QUARANTINE_OUTCOMES:
        return outcome
    raise G10PaidQuarantineError(
        "G10 quarantine outcome is not allowed by the safety layer"
    )


def _quarantine_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G10PaidQuarantineError(
            f"{label} must be a positive integer"
        )
    return value


def _create_marker(
    *,
    path: Path | str,
    scope_kind: str,
    scope_id: str,
    outcome: Any,
    reason: str,
    request_sha256: str,
    reservation_id: str,
    first_blocked_chapter: int | None,
    clock: Any = None,
) -> dict[str, Any]:
    try:
        return create_quarantine_marker(
            path=path,
            scope_kind=scope_kind,
            scope_id=scope_id,
            outcome=_resolve_outcome(outcome),
            reason=reason,
            request_sha256=request_sha256,
            reservation_id=reservation_id,
            first_blocked_chapter=first_blocked_chapter,
            clock=clock,
        )
    except G10SafetyError as exc:
        raise G10PaidQuarantineError(
            f"G10 quarantine marker was rejected: {exc}"
        ) from exc


def quarantine_literary_case(
    *,
    path: Path | str,
    model_id: str,
    novel_id: str,
    chapter: int,
    case_kind: str,
    request_sha256: str,
    reservation_id: str,
    reason: str,
    outcome: Any = DEFAULT_QUARANTINE_OUTCOME,
    clock: Any = None,
) -> dict[str, Any]:
    """Quarantine exactly one G10A model+novel+chapter+case_kind."""
    if model_id not in MODEL_RATECARD:
        raise G10PaidQuarantineError(
            f"unknown G10 model for quarantine: {model_id!r}"
        )
    if novel_id not in NOVELS:
        raise G10PaidQuarantineError(
            f"unknown G10 novel for quarantine: {novel_id!r}"
        )
    _quarantine_positive_int(chapter, "literary quarantine chapter")
    if case_kind not in {"primary", "bridge"}:
        raise G10PaidQuarantineError(
            "literary quarantine requires a frozen case kind"
        )
    scope_id = f"{model_id}::{novel_id}::{chapter:04d}::{case_kind}"
    return _create_marker(
        path=path,
        scope_kind=LITERARY_CASE_QUARANTINE_SCOPE,
        scope_id=scope_id,
        outcome=outcome,
        reason=reason,
        request_sha256=request_sha256,
        reservation_id=reservation_id,
        first_blocked_chapter=None,
        clock=clock,
    )


def quarantine_chronological_track(
    *,
    path: Path | str,
    model_id: str,
    novel_id: str,
    run_id: str,
    first_blocked_chapter: int,
    request_sha256: str,
    reservation_id: str,
    reason: str,
    outcome: Any = DEFAULT_QUARANTINE_OUTCOME,
    clock: Any = None,
) -> dict[str, Any]:
    """Quarantine exactly one G10B model+novel track at a blocked chapter."""
    if run_id not in TRACK_BY_ID:
        raise G10PaidQuarantineError(
            f"unknown G10B run id for quarantine: {run_id!r}"
        )
    track = TRACK_BY_ID[run_id]
    if model_id != track.model_id or novel_id != track.novel_id:
        raise G10PaidQuarantineError(
            "chronological quarantine track identity changed"
        )
    _quarantine_positive_int(first_blocked_chapter, "first_blocked_chapter")
    return _create_marker(
        path=path,
        scope_kind=CHRONOLOGICAL_TRACK_QUARANTINE_SCOPE,
        scope_id=run_id,
        outcome=outcome,
        reason=reason,
        request_sha256=request_sha256,
        reservation_id=reservation_id,
        first_blocked_chapter=first_blocked_chapter,
        clock=clock,
    )


def load_paid_quarantine(path: Path | str) -> dict[str, Any] | None:
    """Read one durable paid quarantine marker, or None when absent."""
    target = Path(path)
    if not target.exists():
        return None
    from benchmark_longitudinal_g10_safety import load_quarantine_marker

    try:
        return load_quarantine_marker(target)
    except G10SafetyError as exc:
        raise G10PaidQuarantineError(
            f"G10 paid quarantine marker is invalid: {exc}"
        ) from exc


# Backwards-compatible alias for the chronological track scope.
load_track_quarantine = load_paid_quarantine


__all__ = [
    "ARM_CREDIT_EQUIVALENT_CEILINGS",
    "ARM_IDS",
    "CHRONOLOGICAL_TRACK_QUARANTINE_SCOPE",
    "DEFAULT_QUARANTINE_OUTCOME",
    "EXPECTED_FREEZE_FILE_SHA256",
    "EXPECTED_FREEZE_ID",
    "EXPECTED_G10A1_SAFETY_SHA256",
    "EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256",
    "EXPECTED_LEDGER_SHA256",
    "EXPECTED_MATCHED_PLAN_SHA256",
    "EXPECTED_PRICING_VERSION",
    "EXPECTED_S0_MANIFEST_SHA256",
    "G10PAID_ARTIFACT_DIRNAME",
    "G10PAID_AUTHORIZATION_DIRNAME",
    "G10PAID_AUTHORIZATION_SHA256",
    "G10PAID_REPOSITORY_ANCHOR",
    "G10PAID_REVISION",
    "G10PAID_SCHEMA_VERSION",
    "GLOBAL_CREDIT_EQUIVALENT_CEILING",
    "LEDGER_AUTHORIZATION_SHA256",
    "LEDGER_GENESIS_LINEAGE_FIELDS",
    "LEDGER_GENESIS_LINEAGE_REVISION",
    "LITERARY_CASE_QUARANTINE_SCOPE",
    "MODEL_RATECARD",
    "G10PaidAuthorizationError",
    "G10PaidBudgetError",
    "G10PaidQuarantineError",
    "accepted_credit_equivalent",
    "assert_ledger_genesis_lineage",
    "assert_operator_authorization_present",
    "assert_paid_execution_still_blocked",
    "authorization_consumption_path",
    "build_paid_authorization",
    "frozen_execution_binding",
    "g10_paid_authorization_identity",
    "ledger_genesis_lineage_identity",
    "ledger_genesis_lineage_sha256",
    "load_paid_quarantine",
    "load_track_quarantine",
    "mark_send_started",
    "open_frozen_budget_ledger",
    "paid_artifact_dir",
    "poisoned_transport_env",
    "preflight_and_reserve",
    "quarantine_chronological_track",
    "quarantine_literary_case",
    "record_authorization_consumption",
    "reservation_record",
    "settle_accepted",
    "settle_ambiguous",
    "settle_pre_send_release",
    "target_state_fingerprint",
    "utf8_subprocess_env",
    "validate_execution_binding",
    "verify_paid_authorization",
]
