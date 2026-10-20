from decimal import Decimal
import json

import pytest

import benchmark_longitudinal_g10e_freeze as g10e


def test_g10e_contract_freezes_current_pricing_and_scaled_budget():
    identity = g10e.g10e_contract_identity()
    assert identity["pricing"]["capture_date"] == "2026-09-29"
    assert identity["pricing"]["models"]["z-ai/glm-5.3-flash"] == {
        "provider_model_id": "z-ai/glm-5.3-flash",
        "pricing_mode": "flat",
        "input": "0.15",
        "output": "0.50",
        "cache_read": "0.03",
    }
    assert identity["pricing"]["models"]["qwen/qwen3.8-max-0902"] == {
        "provider_model_id": "Qwen/Qwen3.8-Max-0902",
        "pricing_mode": "flat",
        "input": "2.00",
        "output": "6.00",
        "cache_read": "0.25",
    }
    assert identity["budget"]["global_credit_equivalent_ceiling"] == "30"
    assert identity["budget"]["arm_credit_equivalent_ceilings"] == {
        "G10A": "5",
        "G10B": "25",
    }


def test_g10e_binds_every_zero_provider_predecessor():
    assert set(g10e.PREDECESSOR_CONTRACTS) == {
        "g10_preregistration_sha256",
        "g10a1_safety_sha256",
        "g10a2_matched_sha256",
        "g10b1_chronological_sha256",
        "g10c_blind_sha256",
        "g10d_recovery_sha256",
        "g10s0_contract_sha256",
    }
    assert all(
        isinstance(value, str) and len(value) == 64
        for value in g10e.PREDECESSOR_CONTRACTS.values()
    )


def test_g10e_itself_remains_zero_provider_and_blocks_paid_execution():
    g10e.assert_paid_execution_blocked()
    gate = g10e.g10e_contract_identity()["execution_gate"]
    assert gate["provider_calls"] == 0
    assert gate["paid_execution_authorized"] is False
    assert gate["separate_g10f_g10g_paid_executor_required"] is True
    assert gate["explicit_operator_approval_required_later"] is True


def test_ratecard_and_budget_invariants():
    assert g10e.GLOBAL_CEILING == Decimal("30")
    assert g10e.ARM_CEILINGS == {
        "G10A": Decimal("5"),
        "G10B": Decimal("25"),
    }
    assert sum(g10e.ARM_CEILINGS.values()) == g10e.GLOBAL_CEILING
    for row in g10e.MODEL_RATECARD.values():
        assert Decimal(row["input"]) >= 0
        assert Decimal(row["output"]) >= 0
        assert Decimal(row["cache_read"]) >= 0
        assert Decimal(row["cache_read"]) <= Decimal(row["input"])


def test_root_overlap_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    production = repo
    matched = tmp_path / "matched"
    s0 = tmp_path / "s0"
    freeze = matched / "freeze"
    with pytest.raises(g10e.G10EFreezeError, match="overlap"):
        g10e._assert_distinct_external_roots(
            repo_root=repo.resolve(),
            production_root=production.resolve(),
            matched_evidence_root=matched.resolve(),
            s0_root=s0.resolve(),
            freeze_root=freeze.resolve(),
            forbidden_roots=(),
        )


def test_freeze_payload_is_deterministic():
    matched = {
        "request_count": 102,
        "pair_count": 51,
        "cases": [{"request_sha256": "a" * 64}],
    }
    s0 = {
        "manifest": {"manifest_sha256": "b" * 64},
        "manifest_sha256": "b" * 64,
        "run_count": 4,
        "production_guard_verified": True,
        "sources_verified": True,
        "all_roots_pristine_s0": True,
    }
    first = g10e._freeze_payload(
        matched_plan=matched,
        s0_verification=s0,
        matched_evidence_root=g10e.Path("/outside/matched"),
        s0_root=g10e.Path("/outside/s0"),
    )
    second = g10e._freeze_payload(
        matched_plan=matched,
        s0_verification=s0,
        matched_evidence_root=g10e.Path("/outside/matched"),
        s0_root=g10e.Path("/outside/s0"),
    )
    assert first == second
    assert g10e._sha256_json(first) == g10e._sha256_json(second)


def test_verify_rejects_tampered_freeze_id(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    matched = tmp_path / "matched"
    matched.mkdir()
    s0 = tmp_path / "s0"
    s0.mkdir()
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    production = repo

    document = {
        "g10e_contract_sha256": g10e.G10E_CONTRACT_SHA256,
        "predecessor_contracts": g10e.PREDECESSOR_CONTRACTS,
        "pricing": g10e.g10e_contract_identity()["pricing"],
        "budget": g10e.g10e_contract_identity()["budget"],
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "freeze_id": "0" * 64,
    }
    (freeze / g10e.G10E_FREEZE_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    (freeze / g10e.G10E_BUDGET_LEDGER_FILENAME).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        g10e,
        "_assert_distinct_external_roots",
        lambda **_kwargs: None,
    )
    with pytest.raises(g10e.G10EFreezeError, match="freeze ID"):
        g10e.verify_g10e(
            repo_root=repo,
            production_root=production,
            matched_evidence_root=matched,
            s0_root=s0,
            freeze_root=freeze,
        )


def test_contract_sha_is_deterministic():
    assert len(g10e.G10E_CONTRACT_SHA256) == 64
    assert g10e.G10E_CONTRACT_SHA256 == g10e._sha256_json(
        g10e.g10e_contract_identity()
    )
