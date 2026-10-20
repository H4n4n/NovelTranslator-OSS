"""Zero-provider tests for the shared G10F/G10G paid-authorization layer."""

import builtins
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import uuid

import pytest

import benchmark_longitudinal_g10_paid_authorization as paid
import benchmark_longitudinal_g10_safety as safety
import benchmark_longitudinal_g10f_matched_executor as g10f
import benchmark_longitudinal_g10g_chronological_executor as g10g
from benchmark_longitudinal_g10_matched import G10A2_OPERATION_KIND
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import (
    CreditReservationLedger,
    G10BudgetError,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc)

# The frozen G10E genesis ledger was created at this exact UTC instant; a
# hermetic genesis ledger that reuses it reproduces the frozen lineage digest
# without touching any external root. Later legitimate mutations use a distinct
# clock so the descendant file provably differs from its genesis bytes.
GENESIS_CREATED_UTC = "2026-09-29T23:51:36.557042+00:00"
GENESIS_CLOCK = datetime(2026, 9, 29, 23, 51, 36, 557042, tzinfo=timezone.utc)
LATER_CLOCK = datetime(2026, 9, 30, 1, 15, tzinfo=timezone.utc)

# Frozen G10F/G10G executor identities; a repair must never move these.
G10F_EXECUTOR_SHA256 = (
    "2606e5cabbe859164f260e5d83b77bcda57ee36eef155c0db99e3ad7e46427f4"
)
G10G_EXECUTOR_SHA256 = (
    "2d24e90a5804336476a49a11171e98d13d709a15188bdf0bfde813471ddb89b5"
)


def _clock():
    return NOW


def _genesis_clock():
    return GENESIS_CLOCK


def _later_clock():
    return LATER_CLOCK


def _real_roots():
    """Return the four frozen external roots when they exist on this machine."""
    candidates = {
        "production_root": Path(r"D:\NovelTranslator"),
        "freeze_root": Path(r"D:\NovelTranslator_G10E_Freeze_20260929"),
        "matched_evidence_root": Path(
            r"D:\NovelTranslator_G10A_Matched_Evidence_20260929"
        ),
        "s0_root": Path(r"D:\NovelTranslator_G10B_S0_20260929"),
    }
    if not all(path.is_dir() for path in candidates.values()):
        return None
    return candidates


def _binding(tmp_path, **overrides):
    """Build a synthetic binding that still pins every frozen literal."""
    base = {
        "schema_version": paid.G10PAID_SCHEMA_VERSION,
        "revision": paid.G10PAID_REVISION,
        "benchmark_id": "g10-qwen-glm-headtohead-v1",
        "g10_paid_authorization_sha256": paid.G10PAID_AUTHORIZATION_SHA256,
        "freeze_id": paid.EXPECTED_FREEZE_ID,
        "freeze_file_sha256": paid.EXPECTED_FREEZE_FILE_SHA256,
        "freeze_file_path": str(tmp_path / "freeze" / "freeze.json"),
        "freeze_root": str(tmp_path / "freeze"),
        "budget_ledger_sha256": paid.EXPECTED_LEDGER_SHA256,
        "budget_ledger_path": str(tmp_path / "freeze" / "g10_budget.json"),
        "maximum_credit_equivalent": "30",
        "arm_credit_equivalent_ceilings": {"G10A": "5", "G10B": "25"},
        "matched_plan_sha256": paid.EXPECTED_MATCHED_PLAN_SHA256,
        "s0_manifest_sha256": paid.EXPECTED_S0_MANIFEST_SHA256,
        "g10a1_safety_sha256": paid.EXPECTED_G10A1_SAFETY_SHA256,
        "pricing_version": paid.EXPECTED_PRICING_VERSION,
        "model_ratecard": copy.deepcopy(paid.MODEL_RATECARD),
        "read_only_production_root": str(tmp_path / "production"),
        "matched_evidence_root": str(tmp_path / "evidence"),
        "s0_root": str(tmp_path / "s0"),
        "repo_root": str(tmp_path / "production"),
        "s0_runs": {
            run_id: {
                "relative_root": f"runs/{run_id}",
                "model_id": record["model_id"],
                "novel_id": record["novel_id"],
                "run_identity_sha256": "a" * 64,
            }
            for run_id, record in _frozen_s0_run_roster().items()
        },
    }
    base.update(overrides)
    return base


def _frozen_s0_run_roster():
    from benchmark_longitudinal_g10_chronological import TRACK_BY_ID

    return {
        run_id: {"model_id": track.model_id, "novel_id": track.novel_id}
        for run_id, track in TRACK_BY_ID.items()
    }


def _ledger(tmp_path, *, maximum="30", arms=None, authorization=None):
    return CreditReservationLedger.create(
        path=tmp_path / "budget.json",
        authorization_sha256=authorization or paid.EXPECTED_FREEZE_ID,
        maximum_credit_equivalent=maximum,
        arm_credit_equivalent_ceilings=arms or {"G10A": "5", "G10B": "25"},
        clock=_clock,
    )


def _genesis_ledger(tmp_path, *, maximum="30", arms=None, authorization=None):
    """Create a hermetic ledger that reproduces the frozen genesis identity."""
    ledger = CreditReservationLedger.create(
        path=tmp_path / "budget.json",
        authorization_sha256=authorization or paid.EXPECTED_FREEZE_ID,
        maximum_credit_equivalent=maximum,
        arm_credit_equivalent_ceilings=arms or {"G10A": "5", "G10B": "25"},
        clock=_genesis_clock,
    )
    # Later mutations re-stamp updated_utc with a distinct clock, so the file
    # provably becomes a descendant rather than the genesis bytes.
    ledger.clock = _later_clock
    return ledger


def _hermetic_binding(tmp_path, ledger_path=None):
    """Bind the frozen literals to a hermetic synthetic genesis ledger."""
    return _binding(
        tmp_path,
        budget_ledger_path=str(ledger_path or (tmp_path / "budget.json")),
    )


def _reservation_plan(arm_id="G10A", *, model_id=None, **overrides):
    """Minimal reservation-eligible plan row for the shared budget layer."""
    plan = {
        "arm_id": arm_id,
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": model_id
        or (QWEN_MODEL_ID if arm_id == "G10A" else GLM_MODEL_ID),
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    plan.update(overrides)
    return plan


def _read_ledger_document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tamper_ledger_document(path, **fields):
    """Rewrite a ledger with the internal ledger_sha256 recomputed."""
    document = _read_ledger_document(path)
    document.update(fields)
    tampered = safety._with_ledger_hash(document)
    Path(path).write_text(
        json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
    )
    return tampered


def _g10a_plan(tmp_path, **overrides):
    binding = overrides.pop("binding", None) or _binding(tmp_path)
    arguments = {
        "binding": binding,
        "arm_id": "G10A",
        "model_id": QWEN_MODEL_ID,
        "provider_model_id": "Qwen/Qwen3.8-Max-0902",
        "logical_operation": G10A2_OPERATION_KIND,
        "novel_id": "ace_evolution",
        "chapter": 3,
        "case_kind": "primary",
        "execution_index": 1,
        "request_sha256": "b" * 64,
        "target_state_fingerprint_sha256": "c" * 64,
        "maximum_credit_equivalent": "0.5",
    }
    arguments.update(overrides)
    return paid.build_paid_authorization(**arguments)


def _g10b_plan(tmp_path, **overrides):
    binding = overrides.pop("binding", None) or _binding(tmp_path)
    arguments = {
        "binding": binding,
        "arm_id": "G10B",
        "model_id": GLM_MODEL_ID,
        "provider_model_id": "z-ai/glm-5.3-flash",
        "logical_operation": G10A2_OPERATION_KIND,
        "novel_id": "ace_evolution",
        "chapter": 1,
        "run_id": "g10b-run02-glm-ace_evolution",
        "track_effort": "high",
        "target_state_fingerprint_sha256": "d" * 64,
        "maximum_credit_equivalent": "0.25",
    }
    arguments.update(overrides)
    return paid.build_paid_authorization(**arguments)


def _retokenize(plan):
    """Recompute the approval token so deeper checks can be exercised."""
    value = copy.deepcopy(dict(plan))
    value.pop("approval_token", None)
    plan.clear()
    plan.update(value)
    plan["approval_token"] = paid._sha256_json(value)
    return plan


# ---------------------------------------------------------------------------
# Import inertness
# ---------------------------------------------------------------------------


def _install_import_guards(monkeypatch):
    def _forbid(*_args, **_kwargs):
        raise AssertionError("import must not perform this operation")

    monkeypatch.setattr(os, "getenv", _forbid)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"openai", "dotenv"}:
            raise AssertionError(
                "G10F/G10G modules must not import the provider SDK at import time"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


@pytest.mark.parametrize(
    "module_file",
    (
        "benchmark_longitudinal_g10_paid_authorization.py",
        "benchmark_longitudinal_g10f_matched_executor.py",
        "benchmark_longitudinal_g10g_chronological_executor.py",
    ),
)
def test_import_is_zero_write_zero_network_zero_credentials(
    monkeypatch, tmp_path, module_file
):
    source = REPO_ROOT / module_file
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    openai_preloaded = "openai" in sys.modules
    unique = f"_g10fg_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, source)
    module = importlib.util.module_from_spec(spec)
    _install_import_guards(monkeypatch)
    monkeypatch.chdir(tmp_path)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
        after = sorted(
            str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
        )
        assert after == before == []
        # A full-suite run may already have imported the provider SDK elsewhere;
        # this import must not be what adds it.
        assert ("openai" in sys.modules) == openai_preloaded
    finally:
        sys.modules.pop(unique, None)


def test_authorization_identity_is_zero_provider_and_deterministic():
    paid.assert_paid_execution_still_blocked()
    identity = paid.g10_paid_authorization_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["separate_operator_authorization_required"] is True
    assert identity["budget"]["global_credit_equivalent_ceiling"] == "30"
    assert identity["budget"]["arm_credit_equivalent_ceilings"] == {
        "G10A": "5",
        "G10B": "25",
    }
    assert identity["frozen_identity"]["freeze_id"] == paid.EXPECTED_FREEZE_ID
    assert identity["frozen_identity"]["freeze_file_sha256"] == (
        paid.EXPECTED_FREEZE_FILE_SHA256
    )
    assert identity["frozen_identity"]["budget_ledger_sha256"] == (
        paid.EXPECTED_LEDGER_SHA256
    )
    assert identity["frozen_identity"]["matched_plan_sha256"] == (
        paid.EXPECTED_MATCHED_PLAN_SHA256
    )
    assert identity["frozen_identity"]["s0_manifest_sha256"] == (
        paid.EXPECTED_S0_MANIFEST_SHA256
    )
    assert identity["frozen_identity"]["pricing_version"] == (
        paid.EXPECTED_PRICING_VERSION
    )
    assert paid.G10PAID_AUTHORIZATION_SHA256 == paid._sha256_json(identity)
    assert identity["quarantine"]["allowed_outcomes"] == sorted(
        paid.ALLOWED_QUARANTINE_OUTCOMES
    )


def test_ledger_authorization_is_the_frozen_freeze_id_never_a_new_module_sha():
    assert paid.LEDGER_AUTHORIZATION_SHA256 == paid.EXPECTED_FREEZE_ID
    assert paid.LEDGER_AUTHORIZATION_SHA256 != paid.G10PAID_AUTHORIZATION_SHA256

    roots = _real_roots()
    if roots is None:
        pytest.skip("frozen external roots are not present on this machine")
    ledger_file = roots["freeze_root"] / "g10_budget.json"
    with pytest.raises(G10BudgetError, match="another authorization"):
        CreditReservationLedger.open(
            path=ledger_file,
            authorization_sha256=paid.G10PAID_AUTHORIZATION_SHA256,
            clock=_clock,
        )
    opened = CreditReservationLedger.open(
        path=ledger_file,
        authorization_sha256=paid.EXPECTED_FREEZE_ID,
        clock=_clock,
    )
    assert opened.maximum_credit_equivalent == Decimal("30")


# ---------------------------------------------------------------------------
# Frozen execution binding
# ---------------------------------------------------------------------------


def test_frozen_execution_binding_pins_every_frozen_identity():
    roots = _real_roots()
    if roots is None:
        pytest.skip("frozen external roots are not present on this machine")

    binding = paid.frozen_execution_binding(**roots)
    ledger_file = roots["freeze_root"] / "g10_budget.json"
    assert binding["freeze_id"] == paid.EXPECTED_FREEZE_ID
    assert binding["freeze_file_sha256"] == paid.EXPECTED_FREEZE_FILE_SHA256
    assert binding["budget_ledger_sha256"] == paid.EXPECTED_LEDGER_SHA256
    assert binding["budget_ledger_current_sha256"] == hashlib.sha256(
        ledger_file.read_bytes()
    ).hexdigest()
    assert binding["budget_ledger_genesis_lineage_sha256"] == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    assert paid.ledger_genesis_lineage_sha256(
        json.loads(ledger_file.read_bytes().decode("utf-8"))
    ) == paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    assert binding["matched_plan_sha256"] == paid.EXPECTED_MATCHED_PLAN_SHA256
    assert binding["s0_manifest_sha256"] == paid.EXPECTED_S0_MANIFEST_SHA256
    assert binding["g10a1_safety_sha256"] == paid.EXPECTED_G10A1_SAFETY_SHA256
    assert binding["pricing_version"] == paid.EXPECTED_PRICING_VERSION
    assert binding["model_ratecard"] == paid.MODEL_RATECARD
    assert binding["maximum_credit_equivalent"] == "30"
    assert binding["arm_credit_equivalent_ceilings"] == {
        "G10A": "5",
        "G10B": "25",
    }
    assert binding["all_frozen_identities_verified"] is True
    assert binding["provider_calls"] == 0
    assert binding["paid_execution_authorized"] is False
    assert set(binding["s0_runs"]) == set(_frozen_s0_run_roster())
    # Read-only binding: it must not require an untouched zero-commit ledger and
    # it must not create anything in the freeze root.
    assert (roots["freeze_root"] / "g10_budget.json").is_file()
    assert sorted(path.name for path in roots["freeze_root"].iterdir()) == [
        "g10_budget.json",
        "g10e_execution_freeze.json",
    ]
    assert paid.validate_execution_binding(binding)["freeze_id"] == (
        paid.EXPECTED_FREEZE_ID
    )
    # Reopening the actual descendant proves its internal hash and invariants
    # as well as immutable lineage, without changing any external evidence.
    before = ledger_file.read_bytes()
    opened = paid.open_frozen_budget_ledger(binding)
    assert paid.assert_ledger_genesis_lineage(opened.document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    assert opened.committed_credit_equivalent == Decimal(
        binding["committed_credit_equivalent"]
    )
    assert ledger_file.read_bytes() == before


def test_frozen_execution_binding_accepts_a_mutated_descendant_ledger(tmp_path):
    """A legitimately mutated ledger keeps its frozen genesis lineage."""
    # The full external frozen_execution_binding check lives in
    # test_frozen_execution_binding_pins_every_frozen_identity. Exercise the
    # descendant transition independently of that live ledger's paid history.
    ledger = _genesis_ledger(tmp_path)
    ledger_file = ledger.path
    genesis_bytes = ledger_file.read_bytes()
    genesis_sha256 = hashlib.sha256(genesis_bytes).hexdigest()
    genesis_lineage = paid.ledger_genesis_lineage_identity(ledger.document)
    assert ledger.document["records"] == []
    assert paid.assert_ledger_genesis_lineage(ledger.document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    binding = _hermetic_binding(tmp_path, ledger_file)
    ledger = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    accepted = ledger.reserve(
        request_sha256="a" * 64,
        arm_id="G10A",
        scope_id=paid.G10_BENCHMARK_ID,
        logical_operation=G10A2_OPERATION_KIND,
        model_id=QWEN_MODEL_ID,
        novel_id="ace_evolution",
        chapter=3,
        reserved_credit_equivalent="0.5",
    )
    ledger.mark_send_started(accepted["reservation_id"])
    ledger.record_accepted(
        accepted["reservation_id"], accepted_credit_equivalent="0.4"
    )
    ambiguous = ledger.reserve(
        request_sha256="b" * 64,
        arm_id="G10A",
        scope_id=paid.G10_BENCHMARK_ID,
        logical_operation=G10A2_OPERATION_KIND,
        model_id=QWEN_MODEL_ID,
        novel_id="ace_evolution",
        chapter=3,
        reserved_credit_equivalent="0.25",
    )
    ledger.mark_send_started(ambiguous["reservation_id"])
    ledger.mark_ambiguous(
        ambiguous["reservation_id"], reason="transport outcome unknown"
    )

    mutated_bytes = ledger_file.read_bytes()
    mutated_sha256 = hashlib.sha256(mutated_bytes).hexdigest()
    assert mutated_bytes != genesis_bytes
    assert mutated_sha256 != genesis_sha256
    assert mutated_sha256 != paid.EXPECTED_LEDGER_SHA256
    document = _read_ledger_document(ledger_file)
    assert paid.ledger_genesis_lineage_identity(document) == genesis_lineage
    assert paid.assert_ledger_genesis_lineage(document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    assert document["created_utc"] == GENESIS_CREATED_UTC
    assert document["updated_utc"] == LATER_CLOCK.isoformat()
    assert [record["status"] for record in document["records"]] == [
        "accepted", "ambiguous"
    ]
    assert all(record["send_started"] for record in document["records"])
    assert ledger.accepted_credit_equivalent == Decimal("0.4")
    assert ledger.committed_credit_equivalent == Decimal("0.65")
    assert binding["budget_ledger_sha256"] == paid.EXPECTED_LEDGER_SHA256

    # The live proof still opens, and a third distinct request can reserve.
    live = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    assert live.accepted_credit_equivalent == Decimal("0.4")
    assert live.committed_credit_equivalent == Decimal("0.65")
    assert live.resume_disposition(accepted["reservation_id"])["disposition"] == (
        "accepted_complete"
    )
    assert live.resume_disposition(ambiguous["reservation_id"])["disposition"] == (
        "quarantine_no_resend"
    )
    third = paid.preflight_and_reserve(
        ledger=live,
        plan=_reservation_plan(),
        request_sha256="c" * 64,
        reserved_credit_equivalent="0.5",
    )
    assert third["status"] == "reserved"
    assert len(live.document["records"]) == 3
    assert paid.ledger_genesis_lineage_sha256(live.document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )


def test_second_invocation_reserves_after_the_first_settlement(tmp_path):
    """The second paid invocation must work after a legitimate settlement."""
    ledger = _genesis_ledger(tmp_path)
    binding = _hermetic_binding(tmp_path)
    assert ledger.document["created_utc"] == GENESIS_CREATED_UTC
    assert paid.ledger_genesis_lineage_sha256(ledger.document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )

    first_pass = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    first = paid.preflight_and_reserve(
        ledger=first_pass,
        plan=_reservation_plan(),
        request_sha256="1" * 64,
        reserved_credit_equivalent="1",
    )
    paid.mark_send_started(
        ledger=first_pass, reservation_id=first["reservation_id"]
    )
    settled = paid.settle_accepted(
        ledger=first_pass,
        reservation_id=first["reservation_id"],
        accepted_credit_equivalent="0.25",
    )
    assert settled["status"] == "accepted"
    assert first_pass.committed_credit_equivalent == Decimal("0.25")

    # The ledger file has legitimately changed (records, accepted CE and
    # updated_utc), so a byte-pinned ledger SHA would be terminal here.
    second_pass = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    second = paid.preflight_and_reserve(
        ledger=second_pass,
        plan=_reservation_plan("G10B"),
        request_sha256="2" * 64,
        reserved_credit_equivalent="0.5",
    )
    assert second["reservation_id"] != first["reservation_id"]
    assert second["status"] == "reserved"

    document = _read_ledger_document(ledger.path)
    assert len(document["records"]) == 2
    assert document["created_utc"] == GENESIS_CREATED_UTC
    assert document["updated_utc"] == LATER_CLOCK.isoformat()
    assert document["accepted_credit_equivalent"] == "0.25"
    assert second_pass.committed_credit_equivalent == Decimal("0.75")
    assert paid.ledger_genesis_lineage_sha256(document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )


@pytest.mark.parametrize(
    "field, tampered, error, message",
    (
        (
            "created_utc",
            "2026-09-29T23:51:36.557043+00:00",
            paid.G10PaidAuthorizationError,
            "genesis lineage",
        ),
        (
            "maximum_credit_equivalent",
            "31",
            paid.G10PaidAuthorizationError,
            "genesis lineage",
        ),
        ("scope_id", "other-scope", G10BudgetError, "another scope"),
        (
            "benchmark_id",
            "other-benchmark",
            G10BudgetError,
            "benchmark identity changed",
        ),
        (
            "g10_preregistration_sha256",
            "1" * 64,
            G10BudgetError,
            "preregistration binding changed",
        ),
        (
            "g10a1_safety_sha256",
            "2" * 64,
            G10BudgetError,
            "safety binding changed",
        ),
        (
            "revision",
            "g10-credit-reservation-ledger-v2",
            G10BudgetError,
            "revision",
        ),
        ("schema_version", 2, G10BudgetError, "schema"),
        (
            "arm_credit_equivalent_ceilings",
            {"G10A": "6", "G10B": "25"},
            G10BudgetError,
            "exceed global ceiling",
        ),
        (
            "authorization_sha256",
            "3" * 64,
            G10BudgetError,
            "another authorization",
        ),
    ),
)
def test_open_frozen_budget_ledger_rejects_immutable_field_tampering(
    tmp_path, field, tampered, error, message
):
    """Every immutable genesis field stays proven, even with a fresh SHA."""
    ledger = _genesis_ledger(tmp_path)
    tampered_document = _tamper_ledger_document(
        ledger.path, **{field: tampered}
    )
    # The internal ledger SHA was recomputed for the tampered document, so the
    # rejection can only come from the ledger's own semantics or the lineage.
    assert tampered_document["ledger_sha256"] != ledger.document["ledger_sha256"]
    assert paid.ledger_genesis_lineage_sha256(tampered_document) != (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    with pytest.raises(error, match=message):
        paid.open_frozen_budget_ledger(_hermetic_binding(tmp_path))


def test_corrupted_current_ledger_sha_is_rejected(tmp_path):
    """A corrupted live ledger hash is rejected, exactly as before."""
    ledger = _genesis_ledger(tmp_path)
    document = _read_ledger_document(ledger.path)
    document["ledger_sha256"] = "f" * 64
    ledger.path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(G10BudgetError, match="does not recompute"):
        paid.open_frozen_budget_ledger(_hermetic_binding(tmp_path))


def test_ceiling_enforcement_survives_ledger_mutation(tmp_path):
    """Global and arm ceilings stay enforced on a mutated descendant ledger."""
    ledger = _genesis_ledger(
        tmp_path, maximum="2", arms={"G10A": "1.5", "G10B": "0.5"}
    )
    filled = paid.preflight_and_reserve(
        ledger=ledger,
        plan=_reservation_plan(),
        request_sha256="1" * 64,
        reserved_credit_equivalent="1.5",
    )
    assert filled["status"] == "reserved"
    assert ledger.arm_committed_credit_equivalent("G10A") == Decimal("1.5")

    # The ledger has now legitimately mutated: one record, a later updated_utc.
    document = _read_ledger_document(ledger.path)
    assert document["created_utc"] == GENESIS_CREATED_UTC
    assert document["updated_utc"] == LATER_CLOCK.isoformat()
    before = ledger.path.read_bytes()

    with pytest.raises(paid.G10PaidBudgetError, match="arm credit-equivalent"):
        paid.preflight_and_reserve(
            ledger=ledger,
            plan=_reservation_plan(),
            request_sha256="2" * 64,
            reserved_credit_equivalent="0.25",
        )
    assert ledger.path.read_bytes() == before

    with pytest.raises(paid.G10PaidBudgetError, match="global credit-equivalent"):
        paid.preflight_and_reserve(
            ledger=ledger,
            plan=_reservation_plan("G10B"),
            request_sha256="3" * 64,
            reserved_credit_equivalent="0.75",
        )
    assert ledger.path.read_bytes() == before
    assert len(ledger.document["records"]) == 1


def test_live_ledger_sha_is_never_pinned_and_genesis_sha_stays_frozen(tmp_path):
    """The live file SHA is evidence only; the genesis lineage is frozen."""
    binding = _hermetic_binding(tmp_path)
    assert paid.validate_execution_binding(binding)["freeze_id"] == (
        paid.EXPECTED_FREEZE_ID
    )

    evidence = paid.validate_execution_binding(
        {
            **binding,
            "budget_ledger_current_sha256": "f" * 64,
            "budget_ledger_genesis_lineage_sha256": (
                paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
            ),
        }
    )
    assert evidence["budget_ledger_sha256"] == paid.EXPECTED_LEDGER_SHA256
    assert evidence["budget_ledger_current_sha256"] == "f" * 64

    with pytest.raises(paid.G10PaidAuthorizationError, match="malformed"):
        paid.validate_execution_binding(
            {**binding, "budget_ledger_current_sha256": "not-a-sha"}
        )
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="genesis lineage changed"
    ):
        paid.validate_execution_binding(
            {**binding, "budget_ledger_genesis_lineage_sha256": "f" * 64}
        )
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="budget_ledger_sha256"
    ):
        paid.validate_execution_binding(
            {**binding, "budget_ledger_sha256": "9" * 64}
        )

    assert paid.G10PAID_AUTHORIZATION_SHA256 == (
        "1810a5461d9dc910fbb29b9115ac12aefc09c044004fdfc1514ba38fcaa98bfb"
    )
    assert paid.EXPECTED_LEDGER_SHA256 == (
        "b255ddbd5d2a47d03201315438f073d05e8716e569c77e90502d4ee395f7d9ce"
    )
    assert g10f.G10F_EXECUTOR_SHA256 == G10F_EXECUTOR_SHA256
    assert g10g.G10G_EXECUTOR_SHA256 == G10G_EXECUTOR_SHA256
    f_identity = g10f.g10f_executor_identity()
    g_identity = g10g.g10g_executor_identity()
    assert paid._sha256_json(f_identity) == G10F_EXECUTOR_SHA256
    assert paid._sha256_json(g_identity) == G10G_EXECUTOR_SHA256
    assert f_identity["predecessor"]["budget_ledger_sha256"] == (
        paid.EXPECTED_LEDGER_SHA256
    )
    assert g_identity["predecessor"]["budget_ledger_sha256"] == (
        paid.EXPECTED_LEDGER_SHA256
    )


def test_lineage_and_reservation_paths_make_no_network_calls(
    tmp_path, monkeypatch
):
    """The whole lineage/reservation surface stays zero-provider."""

    def _forbid(*_args, **_kwargs):
        raise AssertionError(
            "G10 lineage/reservation must not touch the network"
        )

    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    openai_preloaded = "openai" in sys.modules

    _genesis_ledger(tmp_path)
    binding = _hermetic_binding(tmp_path)
    first = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    reservation = paid.preflight_and_reserve(
        ledger=first,
        plan=_reservation_plan(),
        request_sha256="1" * 64,
        reserved_credit_equivalent="0.5",
    )
    paid.mark_send_started(
        ledger=first, reservation_id=reservation["reservation_id"]
    )
    paid.settle_accepted(
        ledger=first,
        reservation_id=reservation["reservation_id"],
        accepted_credit_equivalent="0.1",
    )
    second = paid.open_frozen_budget_ledger(binding, clock=_later_clock)
    assert paid.ledger_genesis_lineage_sha256(second.document) == (
        paid.EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    )
    assert second.committed_credit_equivalent == Decimal("0.1")
    assert ("openai" in sys.modules) == openai_preloaded


def test_tampered_freeze_document_is_rejected(tmp_path):
    roots = _real_roots()
    if roots is None:
        pytest.skip("frozen external roots are not present on this machine")
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    document = json.loads(
        (roots["freeze_root"] / paid.G10E_FREEZE_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    document["paid_execution_authorized"] = True
    (freeze / paid.G10E_FREEZE_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    (freeze / paid.G10E_BUDGET_LEDGER_FILENAME).write_bytes(
        (roots["freeze_root"] / paid.G10E_BUDGET_LEDGER_FILENAME).read_bytes()
    )
    with pytest.raises(paid.G10PaidAuthorizationError, match="freeze ID"):
        paid.frozen_execution_binding(
            production_root=roots["production_root"],
            freeze_root=freeze,
            matched_evidence_root=roots["matched_evidence_root"],
            s0_root=roots["s0_root"],
        )


def test_wrong_matched_plan_sha_is_rejected(tmp_path, monkeypatch):
    roots = _real_roots()
    if roots is None:
        pytest.skip("frozen external roots are not present on this machine")
    monkeypatch.setattr(
        paid,
        "matched_plan_identity",
        lambda **_kwargs: {"substituted": True},
    )
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="matched plan SHA-256"
    ):
        paid.frozen_execution_binding(
            production_root=roots["production_root"],
            freeze_root=roots["freeze_root"],
            matched_evidence_root=roots["matched_evidence_root"],
            s0_root=roots["s0_root"],
        )


def test_wrong_s0_manifest_sha_is_rejected(tmp_path):
    manifest = {
        "schema_version": 1,
        "revision": "g10-four-track-fresh-s0-v1",
        "benchmark_id": paid.G10_BENCHMARK_ID,
        "g10_preregistration_sha256": "1" * 64,
        "g10a1_safety_sha256": paid.EXPECTED_G10A1_SAFETY_SHA256,
        "g10b1_contract_sha256": "2" * 64,
        "run_count": 4,
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "runs": [
            {
                "run_id": run_id,
                "model_id": record["model_id"],
                "novel_id": record["novel_id"],
                "relative_root": f"runs/{run_id}",
                "run_identity_sha256": "a" * 64,
            }
            for run_id, record in _frozen_s0_run_roster().items()
        ],
    }
    manifest["manifest_sha256"] = paid._sha256_json(manifest)
    s0 = tmp_path / "s0"
    s0.mkdir()
    (s0 / "g10_s0_roots_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="not the frozen G10B artifact"
    ):
        paid._s0_run_records(s0)


def test_binding_rejects_wrong_ledger_sha_and_arm_ceilings(tmp_path):
    binding = _binding(tmp_path)
    paid.validate_execution_binding(binding)

    with pytest.raises(paid.G10PaidAuthorizationError, match="budget_ledger_sha256"):
        paid.validate_execution_binding(
            {**binding, "budget_ledger_sha256": "9" * 64}
        )
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="arm ceilings"
    ):
        paid.validate_execution_binding(
            {**binding, "arm_credit_equivalent_ceilings": {"G10A": "1", "G10B": "1"}}
        )
    with pytest.raises(paid.G10PaidAuthorizationError, match="ratecard"):
        paid.validate_execution_binding({**binding, "model_ratecard": {}})


# ---------------------------------------------------------------------------
# Authorization token semantics
# ---------------------------------------------------------------------------


def test_plans_bind_exact_identity_and_ratecard(tmp_path):
    g10a = _g10a_plan(tmp_path)
    assert g10a["arm_id"] == "G10A"
    assert g10a["case_kind"] == "primary"
    assert g10a["execution_index"] == 1
    assert g10a["run_id"] is None
    assert g10a["provider_model_id"] == "Qwen/Qwen3.8-Max-0902"
    assert g10a["ratecard_row"] == paid.MODEL_RATECARD[QWEN_MODEL_ID]
    assert g10a["ratecard_row"]["input"] == "2.00"
    assert g10a["ratecard_row"]["output"] == "6.00"
    assert g10a["ratecard_row"]["cache_read"] == "0.25"
    assert g10a["maximum_new_provider_sends"] == 1
    assert g10a["operator_authorization_required"] is True
    assert g10a["provider_calls"] == 0

    g10b = _g10b_plan(tmp_path)
    assert g10b["arm_id"] == "G10B"
    assert g10b["run_id"] == "g10b-run02-glm-ace_evolution"
    assert g10b["case_kind"] is None
    assert g10b["execution_index"] is None
    assert g10b["track_effort"] == "high"
    assert g10b["ratecard_row"] == paid.MODEL_RATECARD[GLM_MODEL_ID]
    assert g10b["ratecard_row"]["input"] == "0.15"
    assert g10b["ratecard_row"]["output"] == "0.50"
    assert g10b["ratecard_row"]["cache_read"] == "0.03"


def test_provider_model_spelling_is_exact(tmp_path):
    with pytest.raises(paid.G10PaidAuthorizationError, match="provider model"):
        _g10a_plan(tmp_path, provider_model_id="qwen/qwen3.8-max-0902")
    with pytest.raises(paid.G10PaidAuthorizationError, match="provider model"):
        _g10a_plan(tmp_path, provider_model_id="Qwen/Qwen3.8-max-0902")
    with pytest.raises(paid.G10PaidAuthorizationError, match="provider model"):
        _g10b_plan(tmp_path, provider_model_id="Z-AI/GLM-5.3-FLASH")


def test_identity_tuple_must_be_the_frozen_planned_tuple(tmp_path):
    with pytest.raises(paid.G10PaidAuthorizationError, match="frozen matched"):
        _g10a_plan(tmp_path, chapter=4)
    with pytest.raises(paid.G10PaidAuthorizationError, match="frozen matched"):
        _g10a_plan(tmp_path, novel_id="rogue_first_prince")
    with pytest.raises(paid.G10PaidAuthorizationError, match="frozen matched"):
        _g10a_plan(tmp_path, case_kind="bridge")
    with pytest.raises(paid.G10PaidAuthorizationError, match="frozen matched"):
        _g10a_plan(tmp_path, model_id=GLM_MODEL_ID, provider_model_id=(
            "z-ai/glm-5.3-flash"
        ))
    with pytest.raises(paid.G10PaidAuthorizationError, match="execution_index"):
        _g10a_plan(tmp_path, execution_index=0)
    with pytest.raises(paid.G10PaidAuthorizationError, match="execution_index"):
        _g10a_plan(tmp_path, execution_index=103)
    with pytest.raises(paid.G10PaidAuthorizationError, match="run id"):
        _g10b_plan(tmp_path, run_id="g10b-run99-nope")
    with pytest.raises(paid.G10PaidAuthorizationError, match="track identity"):
        _g10b_plan(
            tmp_path,
            run_id="g10b-run02-glm-ace_evolution",
            novel_id="rogue_first_prince",
        )
    with pytest.raises(paid.G10PaidAuthorizationError, match="track identity"):
        _g10b_plan(
            tmp_path,
            run_id="g10b-run02-glm-ace_evolution",
            novel_id="ace_evolution",
            model_id=QWEN_MODEL_ID,
            provider_model_id="Qwen/Qwen3.8-Max-0902",
        )


@pytest.mark.parametrize(
    "field, value",
    (
        ("arm_id", "G10B"),
        ("model_id", GLM_MODEL_ID),
        ("provider_model_id", "z-ai/glm-5.3-flash"),
        ("novel_id", "rogue_first_prince"),
        ("chapter", 4),
        ("case_kind", "bridge"),
        ("execution_index", 3),
        ("run_id", "g10b-run01-qwen-ace_evolution"),
        ("request_sha256", "e" * 64),
        ("target_state_fingerprint_sha256", "f" * 64),
        ("logical_operation", "other_operation"),
        ("maximum_credit_equivalent", "9.5"),
    ),
)
def test_approval_token_drift_on_any_field_is_rejected(tmp_path, field, value):
    plan = _g10a_plan(tmp_path)
    drifted = copy.deepcopy(plan)
    drifted[field] = value
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="does not recompute"
    ):
        paid.verify_paid_authorization(
            plan=drifted,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256="c" * 64,
            operator_authorized=True,
            binding=_binding(tmp_path),
        )


def test_retokenized_identity_and_state_drift_still_fails_closed(tmp_path):
    binding = _binding(tmp_path)
    plan = _g10a_plan(tmp_path)

    for field, value, message in (
        ("arm_id", "G10B", "run id"),
        ("model_id", GLM_MODEL_ID, "ratecard row changed"),
        ("novel_id", "rogue_first_prince", "frozen matched"),
        ("chapter", 4, "frozen matched"),
        ("case_kind", "bridge", "frozen matched"),
        ("execution_index", 3, "frozen matched"),
        (
            "target_state_fingerprint_sha256",
            "e" * 64,
            "target state changed",
        ),
    ):
        drifted = copy.deepcopy(plan)
        drifted[field] = value
        _retokenize(drifted)
        with pytest.raises(paid.G10PaidAuthorizationError, match=message):
            paid.verify_paid_authorization(
                plan=drifted,
                expected_approval_token=drifted["approval_token"],
                current_state_fingerprint_sha256="c" * 64,
                operator_authorized=True,
                binding=binding,
            )

    changed_state = copy.deepcopy(plan)
    _retokenize(changed_state)
    with pytest.raises(paid.G10PaidAuthorizationError, match="target state changed"):
        paid.verify_paid_authorization(
            plan=changed_state,
            expected_approval_token=changed_state["approval_token"],
            current_state_fingerprint_sha256="e" * 64,
            operator_authorized=True,
            binding=binding,
        )

    wrong_binding = _binding(tmp_path, freeze_id="f" * 64)
    with pytest.raises(paid.G10PaidAuthorizationError, match="freeze_id"):
        paid.verify_paid_authorization(
            plan=plan,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256="c" * 64,
            operator_authorized=True,
            binding=wrong_binding,
        )


def test_expected_token_and_operator_authorization_gate(tmp_path):
    binding = _binding(tmp_path)
    plan = _g10a_plan(tmp_path)

    with pytest.raises(paid.G10PaidAuthorizationError, match="token mismatch"):
        paid.verify_paid_authorization(
            plan=plan,
            expected_approval_token="a" * 64,
            current_state_fingerprint_sha256="c" * 64,
            operator_authorized=True,
            binding=binding,
        )
    with pytest.raises(paid.G10PaidAuthorizationError, match="AUTHORIZATION_REQUIRED"):
        paid.verify_paid_authorization(
            plan=plan,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256="c" * 64,
            operator_authorized=False,
            binding=binding,
        )

    consumption = paid.authorization_consumption_path(
        tmp_path / "evidence", plan["approval_token"]
    )
    assert not consumption.exists()
    with pytest.raises(paid.G10PaidAuthorizationError, match="AUTHORIZATION_REQUIRED"):
        paid.assert_operator_authorization_present(False)
    assert not consumption.exists()
    assert not (tmp_path / "evidence").exists()


def test_single_use_consumption_marker_refuses_a_second_send(tmp_path):
    binding = _binding(tmp_path)
    plan = _g10a_plan(tmp_path)
    evidence = tmp_path / "evidence"
    consumption = paid.authorization_consumption_path(
        evidence, plan["approval_token"]
    )
    verified = paid.verify_paid_authorization(
        plan=plan,
        expected_approval_token=plan["approval_token"],
        current_state_fingerprint_sha256="c" * 64,
        operator_authorized=True,
        binding=binding,
        consumption_path=consumption,
    )
    assert verified["authorized"] is True
    assert not consumption.exists()

    marker = paid.record_authorization_consumption(
        path=consumption,
        approval_token=plan["approval_token"],
        request_sha256=plan["request_sha256"],
        reservation_id="1" * 64,
        clock=_clock,
    )
    assert marker["send_started"] is True
    with pytest.raises(paid.G10PaidAuthorizationError, match="already consumed"):
        paid.verify_paid_authorization(
            plan=plan,
            expected_approval_token=plan["approval_token"],
            current_state_fingerprint_sha256="c" * 64,
            operator_authorized=True,
            binding=binding,
            consumption_path=consumption,
        )
    with pytest.raises(paid.G10PaidAuthorizationError, match="already"):
        paid.record_authorization_consumption(
            path=consumption,
            approval_token=plan["approval_token"],
            request_sha256=plan["request_sha256"],
            reservation_id="1" * 64,
            clock=_clock,
        )


# ---------------------------------------------------------------------------
# Budget admission and settlement
# ---------------------------------------------------------------------------


def _preflight(ledger, plan, request_sha256="7" * 64, reserved="1"):
    return paid.preflight_and_reserve(
        ledger=ledger,
        plan=plan,
        request_sha256=request_sha256,
        reserved_credit_equivalent=reserved,
    )


def test_reservation_and_send_started_precede_any_transport(tmp_path):
    ledger = _ledger(tmp_path)
    plan_ = _g10a_plan(tmp_path)
    authorization = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, authorization, request_sha256=plan_["request_sha256"], reserved="1"
    )
    assert reservation["status"] == "reserved"
    assert reservation["send_started"] is False
    assert ledger.committed_credit_equivalent == Decimal("1")

    started = paid.mark_send_started(
        ledger=ledger, reservation_id=reservation["reservation_id"]
    )
    assert started["send_started"] is True
    assert ledger.committed_credit_equivalent == Decimal("1")
    assert (
        ledger.resume_disposition(reservation["reservation_id"])["disposition"]
        == "quarantine_no_resend"
    )


def test_global_ceiling_rejection_leaves_the_ledger_untouched(tmp_path):
    ledger = _ledger(tmp_path, maximum="2", arms={"G10A": "1.5", "G10B": "0.5"})
    plan_a = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    plan_b = {**plan_a, "arm_id": "G10B", "model_id": GLM_MODEL_ID}
    _preflight(ledger, plan_a, request_sha256="1" * 64, reserved="1.5")
    _preflight(ledger, plan_b, request_sha256="2" * 64, reserved="0.5")
    before = ledger.path.read_bytes()

    with pytest.raises(paid.G10PaidBudgetError, match="global credit-equivalent"):
        _preflight(ledger, plan_a, request_sha256="3" * 64, reserved="0.1")
    assert ledger.path.read_bytes() == before
    assert len(ledger.document["records"]) == 2


def test_arm_ceiling_rejection_leaves_the_ledger_untouched(tmp_path):
    ledger = _ledger(tmp_path, maximum="10", arms={"G10A": "1", "G10B": "9"})
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    _preflight(ledger, plan, request_sha256="1" * 64, reserved="1")
    before = ledger.path.read_bytes()
    with pytest.raises(paid.G10PaidBudgetError, match="arm credit-equivalent"):
        _preflight(ledger, plan, request_sha256="2" * 64, reserved="0.5")
    assert ledger.path.read_bytes() == before


def test_exact_accepted_ce_settlement_replaces_the_reservation(tmp_path):
    ledger = _ledger(tmp_path)
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, plan, request_sha256="4" * 64, reserved="1"
    )
    paid.mark_send_started(
        ledger=ledger, reservation_id=reservation["reservation_id"]
    )
    usage = __import__("types").SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=25,
        cached_tokens=0,
        cached_tokens_exposed=True,
    )
    exact = paid.accepted_credit_equivalent(QWEN_MODEL_ID, usage)
    assert exact == Decimal("0.00035")

    settled = paid.settle_accepted(
        ledger=ledger,
        reservation_id=reservation["reservation_id"],
        accepted_credit_equivalent=exact,
    )
    assert settled["status"] == "accepted"
    assert ledger.accepted_credit_equivalent == exact
    assert ledger.committed_credit_equivalent == exact
    assert (
        ledger.resume_disposition(reservation["reservation_id"])["disposition"]
        == "accepted_complete"
    )


def test_unexposed_cache_tokens_are_charged_at_the_full_input_rate():
    unexposed = __import__("types").SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=None,
        cached_tokens_exposed=False,
    )
    assert paid.accepted_credit_equivalent(QWEN_MODEL_ID, unexposed) == Decimal(
        "2.00"
    )
    exposed = __import__("types").SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=1_000_000,
        cached_tokens_exposed=True,
    )
    assert paid.accepted_credit_equivalent(QWEN_MODEL_ID, exposed) == Decimal(
        "0.25"
    )


def test_missing_usage_fails_closed():
    with pytest.raises(paid.G10PaidAuthorizationError, match="usage must be present"):
        paid.accepted_credit_equivalent(QWEN_MODEL_ID, None)
    with pytest.raises(paid.G10PaidAuthorizationError, match="prompt_tokens"):
        paid.accepted_credit_equivalent(
            QWEN_MODEL_ID,
            __import__("types").SimpleNamespace(
                prompt_tokens=None, completion_tokens=5
            ),
        )
    with pytest.raises(paid.G10PaidAuthorizationError, match="unknown G10 ratecard"):
        paid.accepted_credit_equivalent(
            "google/gemini-3.8-flash",
            __import__("types").SimpleNamespace(
                prompt_tokens=1, completion_tokens=1
            ),
        )


def test_ambiguous_outcome_keeps_the_full_reservation(tmp_path):
    ledger = _ledger(tmp_path)
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, plan, request_sha256="5" * 64, reserved="0.75"
    )
    paid.mark_send_started(
        ledger=ledger, reservation_id=reservation["reservation_id"]
    )
    settled = paid.settle_ambiguous(
        ledger=ledger,
        reservation_id=reservation["reservation_id"],
        reason="transport outcome unknown",
    )
    assert settled["status"] == "ambiguous"
    assert settled["full_reservation_retained"] is True
    assert ledger.accepted_credit_equivalent == Decimal("0")
    assert ledger.committed_credit_equivalent == Decimal("0.75")
    assert settled["disposition"] == "quarantine_no_resend"


def test_pre_send_release_requires_proof(tmp_path):
    ledger = _ledger(tmp_path)
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, plan, request_sha256="6" * 64, reserved="0.5"
    )
    with pytest.raises(paid.G10PaidBudgetError, match="no-send proof"):
        paid.settle_pre_send_release(
            ledger=ledger,
            reservation_id=reservation["reservation_id"],
            transport_never_started_proven=False,
        )
    released = paid.settle_pre_send_release(
        ledger=ledger,
        reservation_id=reservation["reservation_id"],
        transport_never_started_proven=True,
    )
    assert released["status"] == "released_pre_send"
    assert ledger.committed_credit_equivalent == Decimal("0")


def test_terminal_overrun_flag_is_persisted_and_blocks_further_work(tmp_path):
    ledger = _ledger(tmp_path)
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, plan, request_sha256="8" * 64, reserved="0.25"
    )
    paid.mark_send_started(
        ledger=ledger, reservation_id=reservation["reservation_id"]
    )
    with pytest.raises(paid.G10PaidBudgetError, match="terminal"):
        paid.settle_accepted(
            ledger=ledger,
            reservation_id=reservation["reservation_id"],
            accepted_credit_equivalent="0.30",
        )
    assert ledger.has_terminal_budget_overrun is True
    assert ledger.accepted_credit_equivalent == Decimal("0.30")
    assert ledger.committed_credit_equivalent == Decimal("0.30")
    with pytest.raises(paid.G10PaidBudgetError, match="terminal"):
        _preflight(ledger, plan, request_sha256="9" * 64, reserved="0.1")


def test_settle_accepted_requires_a_started_reservation(tmp_path):
    ledger = _ledger(tmp_path)
    plan = {
        "arm_id": "G10A",
        "scope_id": paid.G10_BENCHMARK_ID,
        "logical_operation": G10A2_OPERATION_KIND,
        "model_id": QWEN_MODEL_ID,
        "novel_id": "ace_evolution",
        "chapter": 3,
    }
    reservation = _preflight(
        ledger, plan, request_sha256="a" * 64, reserved="0.25"
    )
    with pytest.raises(paid.G10PaidBudgetError, match="rejected"):
        paid.settle_accepted(
            ledger=ledger,
            reservation_id=reservation["reservation_id"],
            accepted_credit_equivalent="0.1",
        )


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def test_literary_quarantine_never_carries_a_blocked_chapter(tmp_path):
    marker = paid.quarantine_literary_case(
        path=tmp_path / "literary.json",
        model_id=QWEN_MODEL_ID,
        novel_id="ace_evolution",
        chapter=3,
        case_kind="primary",
        request_sha256="1" * 64,
        reservation_id="2" * 64,
        reason="ambiguous remote outcome",
        clock=_clock,
    )
    assert marker["scope_kind"] == "literary_model_case"
    assert marker["first_blocked_chapter"] is None
    assert marker["scope_id"] == f"{QWEN_MODEL_ID}::ace_evolution::0003::primary"
    assert marker["outcome"] in paid.ALLOWED_QUARANTINE_OUTCOMES

    with pytest.raises(paid.G10PaidQuarantineError, match="chapter"):
        paid.quarantine_literary_case(
            path=tmp_path / "literary-2.json",
            model_id=QWEN_MODEL_ID,
            novel_id="ace_evolution",
            chapter=0,
            case_kind="primary",
            request_sha256="1" * 64,
            reservation_id="2" * 64,
            reason="bad chapter",
            clock=_clock,
        )
    with pytest.raises(paid.G10PaidQuarantineError, match="outcome"):
        paid.quarantine_literary_case(
            path=tmp_path / "literary-3.json",
            model_id=QWEN_MODEL_ID,
            novel_id="ace_evolution",
            chapter=3,
            case_kind="primary",
            request_sha256="1" * 64,
            reservation_id="2" * 64,
            reason="bad outcome",
            outcome="not_allowed",
            clock=_clock,
        )


def test_chronological_quarantine_requires_a_chapter_and_track_identity(tmp_path):
    run_id = "g10b-run01-qwen-ace_evolution"
    marker = paid.quarantine_chronological_track(
        path=tmp_path / "track" / "data" / "g10_quarantine.json",
        model_id=QWEN_MODEL_ID,
        novel_id="ace_evolution",
        run_id=run_id,
        first_blocked_chapter=25,
        request_sha256="3" * 64,
        reservation_id="4" * 64,
        reason="ambiguous remote outcome",
        clock=_clock,
    )
    assert marker["scope_kind"] == "chronological_model_novel_track"
    assert marker["scope_id"] == run_id
    assert marker["first_blocked_chapter"] == 25
    loaded = paid.load_track_quarantine(
        tmp_path / "track" / "data" / "g10_quarantine.json"
    )
    assert loaded == marker

    with pytest.raises(paid.G10PaidQuarantineError, match="run id"):
        paid.quarantine_chronological_track(
            path=tmp_path / "other.json",
            model_id=QWEN_MODEL_ID,
            novel_id="ace_evolution",
            run_id="g10b-run99-nope",
            first_blocked_chapter=25,
            request_sha256="3" * 64,
            reservation_id="4" * 64,
            reason="bad run",
            clock=_clock,
        )
    with pytest.raises(paid.G10PaidQuarantineError, match="track identity"):
        paid.quarantine_chronological_track(
            path=tmp_path / "other-2.json",
            model_id=GLM_MODEL_ID,
            novel_id="ace_evolution",
            run_id=run_id,
            first_blocked_chapter=25,
            request_sha256="3" * 64,
            reservation_id="4" * 64,
            reason="wrong model",
            clock=_clock,
        )
    with pytest.raises(paid.G10PaidQuarantineError, match="first_blocked_chapter"):
        paid.quarantine_chronological_track(
            path=tmp_path / "other-3.json",
            model_id=QWEN_MODEL_ID,
            novel_id="ace_evolution",
            run_id=run_id,
            first_blocked_chapter=0,
            request_sha256="3" * 64,
            reservation_id="4" * 64,
            reason="bad chapter",
            clock=_clock,
        )


# ---------------------------------------------------------------------------
# Target state fingerprint, evidence paths, child-process environment
# ---------------------------------------------------------------------------


def test_target_state_fingerprint_is_deterministic_and_exclusion_aware(tmp_path):
    root = tmp_path / "state"
    (root / "data" / "provider_request_journal").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "data" / "progress.json").write_text(
        '{"last_completed":0}', encoding="utf-8"
    )
    first = paid.target_state_fingerprint(
        root,
        excluded_prefixes=("data/provider_request_journal/", "logs/"),
    )
    second = paid.target_state_fingerprint(
        root,
        excluded_prefixes=("data/provider_request_journal/", "logs/"),
    )
    assert first == second

    (root / "data" / "provider_request_journal" / "x.json").write_text(
        "{}", encoding="utf-8"
    )
    (root / "logs" / "run.log").write_text("x", encoding="utf-8")
    assert (
        paid.target_state_fingerprint(
            root,
            excluded_prefixes=("data/provider_request_journal/", "logs/"),
        )
        == first
    )
    (root / "data" / "progress.json").write_text(
        '{"last_completed":1}', encoding="utf-8"
    )
    assert (
        paid.target_state_fingerprint(
            root,
            excluded_prefixes=("data/provider_request_journal/", "logs/"),
        )
        != first
    )
    with pytest.raises(paid.G10PaidAuthorizationError, match="missing"):
        paid.target_state_fingerprint(tmp_path / "absent")


def test_authorization_and_artifact_paths_are_deterministic(tmp_path):
    token = "1" * 64
    assert paid.authorization_consumption_path(tmp_path, token) == (
        tmp_path / "authorization" / f"{token}.json"
    )
    assert paid.paid_artifact_dir(tmp_path, "ace_evolution", "qwen") == (
        tmp_path / "artifacts" / "ace_evolution" / "qwen"
    )
    with pytest.raises(paid.G10PaidAuthorizationError, match="component"):
        paid.paid_artifact_dir(tmp_path, "")
    with pytest.raises(paid.G10PaidAuthorizationError, match="SHA-256"):
        paid.authorization_consumption_path(tmp_path, "nope")


def test_child_process_environment_contract_is_utf8():
    env = paid.utf8_subprocess_env({"EXISTING": "1"})
    assert env["EXISTING"] == "1"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    poisoned = paid.poisoned_transport_env({"EXISTING": "1"})
    assert poisoned["G10_TRANSPORT_POISONED"] == "1"
    assert poisoned["PYTHONUTF8"] == "1"


def test_exports_cover_the_frozen_surface():
    for name in (
        "EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256",
        "LEDGER_GENESIS_LINEAGE_FIELDS",
        "LEDGER_GENESIS_LINEAGE_REVISION",
        "G10PaidAuthorizationError",
        "G10PaidBudgetError",
        "G10PaidQuarantineError",
        "accepted_credit_equivalent",
        "assert_ledger_genesis_lineage",
        "assert_operator_authorization_present",
        "authorization_consumption_path",
        "build_paid_authorization",
        "frozen_execution_binding",
        "g10_paid_authorization_identity",
        "ledger_genesis_lineage_identity",
        "ledger_genesis_lineage_sha256",
        "paid_artifact_dir",
        "preflight_and_reserve",
        "quarantine_chronological_track",
        "quarantine_literary_case",
        "settle_accepted",
        "settle_ambiguous",
        "settle_pre_send_release",
        "target_state_fingerprint",
        "utf8_subprocess_env",
        "verify_paid_authorization",
    ):
        assert name in paid.__all__, name
        assert hasattr(paid, name), name
