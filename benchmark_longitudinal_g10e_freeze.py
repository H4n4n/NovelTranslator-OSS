"""G10E zero-provider tooling/source/budget/root freeze.

G10E binds the already-reviewed G10 A0-D contracts to a fresh execution freeze,
a current Command Code pricing snapshot, explicit arm/global credit-equivalent
ceilings, the matched-literary request plan, and four verified pristine G10B S0
roots.

Nothing here constructs a Provider client, reads credentials, or sends a paid
request.  G10E freezes what a later paid executor MUST bind; it does not itself
authorize G10F/G10G transport.
"""

from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence
import uuid

from benchmark_longitudinal_g10_blind import G10C_CONTRACT_SHA256
from benchmark_longitudinal_g10_chronological import G10B1_CONTRACT_SHA256
from benchmark_longitudinal_g10_matched import (
    G10A2_CONTRACT_SHA256,
    matched_plan_identity,
)
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_recovery_rehearsal import G10D_CONTRACT_SHA256
from benchmark_longitudinal_g10_s0 import (
    G10S0_CONTRACT_SHA256,
    prepare_s0_roots,
    verify_s0_roots,
)
from benchmark_longitudinal_g10_safety import (
    CreditReservationLedger,
    G10A1_SAFETY_SHA256,
    assert_fresh_execution_root,
)
from noveltranslator_core import atomic_write_json


G10E_SCHEMA_VERSION = 1
G10E_REVISION = "g10-tooling-source-budget-root-freeze-v1"
G10E_REPOSITORY_BASELINE = "e8a029d448ffe417318c0ce0a4948eabce48e800"
G10E_FREEZE_FILENAME = "g10e_execution_freeze.json"
G10E_BUDGET_LEDGER_FILENAME = "g10_budget.json"
G10E_PRICING_CAPTURE_DATE = "2026-09-29"
G10E_PRICING_VERSION = "command-code-provider-public-ratecard-2026-09-29-g10-v1"

GLOBAL_CEILING = Decimal("30")
ARM_CEILINGS = {
    "G10A": Decimal("5"),
    "G10B": Decimal("25"),
}

PRICING_SOURCES = (
    "https://commandcode.ai/docs/resources/pricing-limits",
    "https://commandcode.ai/models/glm-5-3-flash",
    "https://commandcode.ai/blog/qwen-3-8-max-0902-is-live-in-command-code",
)

# Decimal strings are identity data.  All rates are public Command Code
# credit-equivalent values per 1M tokens captured on 2026-09-29.
MODEL_RATECARD = {
    GLM_MODEL_ID: {
        "provider_model_id": "z-ai/glm-5.3-flash",
        "pricing_mode": "flat",
        "input": "0.15",
        "output": "0.50",
        "cache_read": "0.03",
    },
    QWEN_MODEL_ID: {
        "provider_model_id": "Qwen/Qwen3.8-Max-0902",
        "pricing_mode": "flat",
        "input": "2.00",
        "output": "6.00",
        "cache_read": "0.25",
    },
}

PREDECESSOR_CONTRACTS = {
    "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
    "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
    "g10a2_matched_sha256": G10A2_CONTRACT_SHA256,
    "g10b1_chronological_sha256": G10B1_CONTRACT_SHA256,
    "g10c_blind_sha256": G10C_CONTRACT_SHA256,
    "g10d_recovery_sha256": G10D_CONTRACT_SHA256,
    "g10s0_contract_sha256": G10S0_CONTRACT_SHA256,
}


class G10EFreezeError(RuntimeError):
    """The G10E execution freeze is incomplete, stale, or unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10EFreezeError("G10E value is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G10EFreezeError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_ratecard() -> None:
    if set(MODEL_RATECARD) != {GLM_MODEL_ID, QWEN_MODEL_ID}:
        raise G10EFreezeError("G10E pricing model roster drifted")
    for model_id, row in MODEL_RATECARD.items():
        if row.get("pricing_mode") != "flat":
            raise G10EFreezeError(f"G10E expects flat pricing for {model_id}")
        for field in ("input", "output", "cache_read"):
            try:
                value = Decimal(str(row[field]))
            except Exception as exc:
                raise G10EFreezeError(
                    f"invalid G10E {model_id} {field} rate"
                ) from exc
            if value < 0:
                raise G10EFreezeError(f"negative G10E {model_id} {field} rate")
        if Decimal(row["cache_read"]) > Decimal(row["input"]):
            raise G10EFreezeError(
                f"G10E {model_id} cache-read rate exceeds input rate"
            )


def _validate_budget() -> None:
    if set(ARM_CEILINGS) != {"G10A", "G10B"}:
        raise G10EFreezeError("G10E requires exactly G10A/G10B arm ceilings")
    if GLOBAL_CEILING <= 0 or any(value <= 0 for value in ARM_CEILINGS.values()):
        raise G10EFreezeError("G10E budget ceilings must be positive")
    if sum(ARM_CEILINGS.values(), Decimal("0")) > GLOBAL_CEILING:
        raise G10EFreezeError("G10E arm ceilings exceed global ceiling")


_validate_ratecard()
_validate_budget()


def g10e_contract_identity() -> dict[str, Any]:
    return {
        "schema_version": G10E_SCHEMA_VERSION,
        "revision": G10E_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "repository_baseline": G10E_REPOSITORY_BASELINE,
        "predecessor_contracts": copy.deepcopy(PREDECESSOR_CONTRACTS),
        "pricing": {
            "capture_date": G10E_PRICING_CAPTURE_DATE,
            "version": G10E_PRICING_VERSION,
            "sources": list(PRICING_SOURCES),
            "unit": "usd_credit_equivalent_per_1m_tokens",
            "models": copy.deepcopy(MODEL_RATECARD),
        },
        "budget": {
            "global_credit_equivalent_ceiling": _decimal_text(GLOBAL_CEILING),
            "arm_credit_equivalent_ceilings": {
                arm: _decimal_text(value)
                for arm, value in ARM_CEILINGS.items()
            },
            "committed_definition": (
                "accepted_credit_equivalent_plus_full_reserved_credit_equivalent_"
                "for_every_unresolved_or_ambiguous_request"
            ),
            "request_level_reservation_required_before_transport": True,
            "arm_and_global_ceiling_checked_before_transport": True,
            "budget_persists_across_restarts": True,
            "ceiling_change_requires_new_versioned_freeze": True,
            "ceiling_is_safety_limit_not_expected_spend": True,
        },
        "root_freeze": {
            "matched_evidence_root_external_required": True,
            "chronological_s0_root_external_required": True,
            "freeze_root_external_required": True,
            "all_roots_must_be_distinct": True,
            "symlinks_forbidden": True,
            "old_goat_overlap_forbidden": True,
            "matched_plan_sha_required": True,
            "s0_manifest_sha_required": True,
            "production_guard_verified_required": True,
        },
        "execution_gate": {
            "paid_execution_authorized": False,
            "provider_calls": 0,
            "separate_g10f_g10g_paid_executor_required": True,
            "explicit_operator_approval_required_later": True,
        },
    }


G10E_CONTRACT_SHA256 = _sha256_json(g10e_contract_identity())


def _assert_distinct_external_roots(
    *,
    repo_root: Path,
    production_root: Path,
    matched_evidence_root: Path,
    s0_root: Path,
    freeze_root: Path,
    forbidden_roots: tuple[Path | str, ...],
) -> None:
    roots = {
        "matched_evidence_root": matched_evidence_root,
        "s0_root": s0_root,
        "freeze_root": freeze_root,
    }
    normalized = {
        name: assert_fresh_execution_root(
            path,
            forbidden_roots=(repo_root, production_root, *forbidden_roots),
        )
        for name, path in roots.items()
    }
    values = list(normalized.items())
    for index, (name_a, path_a) in enumerate(values):
        for name_b, path_b in values[index + 1:]:
            if (
                path_a == path_b
                or _is_within(path_a, path_b)
                or _is_within(path_b, path_a)
            ):
                raise G10EFreezeError(
                    f"G10E roots overlap: {name_a} and {name_b}"
                )


def _freeze_payload(
    *,
    matched_plan: Mapping[str, Any],
    s0_verification: Mapping[str, Any],
    matched_evidence_root: Path,
    s0_root: Path,
) -> dict[str, Any]:
    s0_manifest = s0_verification.get("manifest")
    s0_manifest_sha = s0_verification.get("manifest_sha256")
    if not isinstance(s0_manifest, Mapping):
        raise G10EFreezeError("G10E S0 verification lacks manifest")
    if not isinstance(s0_manifest_sha, str) or len(s0_manifest_sha) != 64:
        raise G10EFreezeError("G10E S0 verification lacks manifest SHA")

    matched_sha = _sha256_json(matched_plan)
    return {
        "schema_version": G10E_SCHEMA_VERSION,
        "revision": G10E_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10e_contract_sha256": G10E_CONTRACT_SHA256,
        "repository_baseline": G10E_REPOSITORY_BASELINE,
        "predecessor_contracts": copy.deepcopy(PREDECESSOR_CONTRACTS),
        "pricing": g10e_contract_identity()["pricing"],
        "budget": g10e_contract_identity()["budget"],
        "matched": {
            "evidence_root": str(matched_evidence_root),
            "matched_plan_sha256": matched_sha,
            "request_count": matched_plan.get("request_count"),
            "pair_count": matched_plan.get("pair_count"),
        },
        "chronological": {
            "s0_root": str(s0_root),
            "s0_manifest_sha256": s0_manifest_sha,
            "run_count": s0_verification.get("run_count"),
            "production_guard_verified": s0_verification.get(
                "production_guard_verified"
            ),
            "sources_verified": s0_verification.get("sources_verified"),
            "all_roots_pristine_s0": s0_verification.get(
                "all_roots_pristine_s0"
            ),
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


def freeze_g10e(
    *,
    repo_root: Path | str,
    production_root: Path | str,
    matched_evidence_root: Path | str,
    s0_root: Path | str,
    freeze_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
    prepare_s0_if_missing: bool = False,
) -> dict[str, Any]:
    """Create the immutable G10E local freeze and empty durable budget ledger."""

    repo = Path(repo_root).expanduser().resolve()
    production = Path(production_root).expanduser().resolve()
    matched = Path(matched_evidence_root).expanduser().resolve()
    s0 = Path(s0_root).expanduser().resolve()
    freeze = Path(freeze_root).expanduser().resolve()

    if not repo.is_dir():
        raise G10EFreezeError(f"repository root missing: {repo}")
    if not production.is_dir():
        raise G10EFreezeError(f"production root missing: {production}")
    if freeze.exists():
        raise G10EFreezeError("G10E freeze root already exists")
    if matched.exists():
        if not matched.is_dir() or any(matched.iterdir()):
            raise G10EFreezeError(
                "G10A matched evidence root must be absent or empty before freeze"
            )

    _assert_distinct_external_roots(
        repo_root=repo,
        production_root=production,
        matched_evidence_root=matched,
        s0_root=s0,
        freeze_root=freeze,
        forbidden_roots=forbidden_roots,
    )

    if not s0.exists():
        if not prepare_s0_if_missing:
            raise G10EFreezeError(
                "G10B S0 root is missing; prepare it or pass prepare_s0_if_missing=True"
            )
        prepare_s0_roots(
            production_root=production,
            output_root=s0,
            forbidden_roots=(repo, matched, freeze, *forbidden_roots),
        )

    s0_verification = verify_s0_roots(
        production_root=production,
        output_root=s0,
        forbidden_roots=(repo, matched, freeze, *forbidden_roots),
    )

    matched.mkdir(parents=True, exist_ok=False)
    try:
        matched_plan = matched_plan_identity(
            repo_root=repo,
            evidence_root=matched,
            forbidden_roots=(production, s0, freeze, *forbidden_roots),
        )
        payload = _freeze_payload(
            matched_plan=matched_plan,
            s0_verification=s0_verification,
            matched_evidence_root=matched,
            s0_root=s0,
        )
        freeze_id = _sha256_json(payload)
        document = {
            **payload,
            "freeze_id": freeze_id,
        }

        staging = freeze.with_name(
            f".{freeze.name}.staging-{uuid.uuid4().hex}"
        )
        if staging.exists():
            raise G10EFreezeError("G10E staging root collision")
        staging.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write_json(staging / G10E_FREEZE_FILENAME, document)
            CreditReservationLedger.create(
                path=staging / G10E_BUDGET_LEDGER_FILENAME,
                authorization_sha256=freeze_id,
                maximum_credit_equivalent=_decimal_text(GLOBAL_CEILING),
                arm_credit_equivalent_ceilings={
                    arm: _decimal_text(value)
                    for arm, value in ARM_CEILINGS.items()
                },
            )
            if freeze.exists():
                raise G10EFreezeError(
                    "G10E freeze root appeared during create-once staging"
                )
            os.rename(staging, freeze)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
    except BaseException:
        if matched.exists() and not any(matched.iterdir()):
            matched.rmdir()
        raise

    return verify_g10e(
        repo_root=repo,
        production_root=production,
        matched_evidence_root=matched,
        s0_root=s0,
        freeze_root=freeze,
        forbidden_roots=forbidden_roots,
    )


def verify_g10e(
    *,
    repo_root: Path | str,
    production_root: Path | str,
    matched_evidence_root: Path | str,
    s0_root: Path | str,
    freeze_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    production = Path(production_root).expanduser().resolve()
    matched = Path(matched_evidence_root).expanduser().resolve()
    s0 = Path(s0_root).expanduser().resolve()
    freeze = Path(freeze_root).expanduser().resolve()

    _assert_distinct_external_roots(
        repo_root=repo,
        production_root=production,
        matched_evidence_root=matched,
        s0_root=s0,
        freeze_root=freeze,
        forbidden_roots=forbidden_roots,
    )

    freeze_file = freeze / G10E_FREEZE_FILENAME
    ledger_file = freeze / G10E_BUDGET_LEDGER_FILENAME
    if not freeze_file.is_file() or not ledger_file.is_file():
        raise G10EFreezeError("G10E freeze artifacts are incomplete")
    try:
        document = json.loads(freeze_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10EFreezeError("G10E freeze document cannot be read") from exc
    if not isinstance(document, dict):
        raise G10EFreezeError("G10E freeze document must be an object")

    freeze_id = document.get("freeze_id")
    payload = copy.deepcopy(document)
    payload.pop("freeze_id", None)
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G10EFreezeError("G10E freeze ID does not recompute")
    if document.get("g10e_contract_sha256") != G10E_CONTRACT_SHA256:
        raise G10EFreezeError("G10E contract binding changed")
    if document.get("predecessor_contracts") != PREDECESSOR_CONTRACTS:
        raise G10EFreezeError("G10E predecessor bindings changed")
    if document.get("pricing") != g10e_contract_identity()["pricing"]:
        raise G10EFreezeError("G10E pricing snapshot changed")
    if document.get("budget") != g10e_contract_identity()["budget"]:
        raise G10EFreezeError("G10E budget policy changed")
    if document.get("provider_calls") != 0:
        raise G10EFreezeError("G10E unexpectedly reports Provider calls")
    if document.get("paid_execution_authorized") is not False:
        raise G10EFreezeError("G10E unexpectedly authorizes paid execution")

    s0_verification = verify_s0_roots(
        production_root=production,
        output_root=s0,
        forbidden_roots=(repo, matched, freeze, *forbidden_roots),
    )
    if (
        document["chronological"]["s0_manifest_sha256"]
        != s0_verification["manifest_sha256"]
    ):
        raise G10EFreezeError("G10E S0 manifest binding changed")

    matched_plan = matched_plan_identity(
        repo_root=repo,
        evidence_root=matched,
        forbidden_roots=(production, s0, freeze, *forbidden_roots),
    )
    if (
        document["matched"]["matched_plan_sha256"]
        != _sha256_json(matched_plan)
    ):
        raise G10EFreezeError("G10E matched plan binding changed")

    ledger = CreditReservationLedger.open(
        path=ledger_file,
        authorization_sha256=freeze_id,
    )
    if ledger.maximum_credit_equivalent != GLOBAL_CEILING:
        raise G10EFreezeError("G10E global budget ledger ceiling changed")
    for arm_id, expected in ARM_CEILINGS.items():
        if ledger.arm_credit_equivalent_ceiling(arm_id) != expected:
            raise G10EFreezeError(f"G10E {arm_id} ledger ceiling changed")
    if ledger.committed_credit_equivalent != 0:
        raise G10EFreezeError(
            "G10E initial verification requires an untouched zero-commit ledger"
        )

    return {
        "freeze_id": freeze_id,
        "freeze_file_sha256": _sha256_file(freeze_file),
        "budget_ledger_sha256": _sha256_file(ledger_file),
        "matched_plan_sha256": document["matched"]["matched_plan_sha256"],
        "s0_manifest_sha256": document["chronological"]["s0_manifest_sha256"],
        "global_credit_equivalent_ceiling": _decimal_text(GLOBAL_CEILING),
        "arm_credit_equivalent_ceilings": {
            arm: _decimal_text(value)
            for arm, value in ARM_CEILINGS.items()
        },
        "all_roots_verified": True,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


def assert_paid_execution_blocked() -> None:
    identity = g10e_contract_identity()
    gate = identity["execution_gate"]
    if gate["provider_calls"] != 0:
        raise G10EFreezeError("G10E unexpectedly reports Provider calls")
    if gate["paid_execution_authorized"] is not False:
        raise G10EFreezeError("G10E must not itself authorize paid execution")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare/verify the zero-provider G10E freeze.")
    parser.add_argument("action", choices=("freeze", "verify"))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--production-root", required=True)
    parser.add_argument("--matched-evidence-root", required=True)
    parser.add_argument("--s0-root", required=True)
    parser.add_argument("--freeze-root", required=True)
    parser.add_argument("--forbidden-root", action="append", default=[])
    parser.add_argument("--prepare-s0-if-missing", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {
        "repo_root": args.repo_root,
        "production_root": args.production_root,
        "matched_evidence_root": args.matched_evidence_root,
        "s0_root": args.s0_root,
        "freeze_root": args.freeze_root,
        "forbidden_roots": tuple(args.forbidden_root),
    }
    if args.action == "freeze":
        result = freeze_g10e(
            **kwargs,
            prepare_s0_if_missing=args.prepare_s0_if_missing,
        )
    else:
        result = verify_g10e(**kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARM_CEILINGS",
    "GLOBAL_CEILING",
    "G10E_BUDGET_LEDGER_FILENAME",
    "G10E_CONTRACT_SHA256",
    "G10E_FREEZE_FILENAME",
    "G10E_PRICING_CAPTURE_DATE",
    "G10E_PRICING_VERSION",
    "G10E_REPOSITORY_BASELINE",
    "G10E_REVISION",
    "G10EFreezeError",
    "MODEL_RATECARD",
    "PREDECESSOR_CONTRACTS",
    "PRICING_SOURCES",
    "assert_paid_execution_blocked",
    "freeze_g10e",
    "g10e_contract_identity",
    "verify_g10e",
]
