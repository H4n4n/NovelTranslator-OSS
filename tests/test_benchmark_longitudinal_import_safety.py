import builtins
import importlib.util
import os
from pathlib import Path
import socket
import sys
import uuid

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

MODULES = {
    "benchmark_longitudinal_config": (
        REPO_ROOT / "benchmark_longitudinal_config.py",
        (
            "DEFAULT_BENCHMARK_CONFIGURATION",
            "BENCHMARK_MODEL_IDS",
            "PROVIDER_BASE_URL",
            "resolve_reasoning_candidate",
        ),
    ),
    "benchmark_longitudinal_provider": (
        REPO_ROOT / "benchmark_longitudinal_provider.py",
        (
            "create_provider_client",
            "build_provider_request",
            "normalize_provider_response",
        ),
    ),
    "benchmark_longitudinal_capabilities": (
        REPO_ROOT / "benchmark_longitudinal_capabilities.py",
        (
            "CAPABILITY_PROFILES",
            "G5C_CAPABILITY_PROBE_CASES",
            "CAPABILITY_FREEZE_SHA256",
        ),
    ),
    "benchmark_longitudinal_capability_probe": (
        REPO_ROOT / "benchmark_longitudinal_capability_probe.py",
        (
            "build_probe_request",
            "inspect_probe_state",
            "run_probe_suite",
        ),
    ),
    "benchmark_longitudinal_capability_results": (
        REPO_ROOT / "benchmark_longitudinal_capability_results.py",
        (
            "LIVE_PROVIDER_CAPABILITIES",
            "verified_reasoning_capability",
            "G5C_LIVE_RESULT_SHA256",
        ),
    ),
    "benchmark_longitudinal_g6_source_selection": (
        REPO_ROOT / "benchmark_longitudinal_g6_source_selection.py",
        (
            "profile_chapters",
            "select_stress_cases",
            "build_selection_bundle",
        ),
    ),
    "benchmark_longitudinal_g6_calibration": (
        REPO_ROOT / "benchmark_longitudinal_g6_calibration.py",
        (
            "G6_SOURCE_CASES",
            "G6_CALIBRATION_CASES",
            "G6_CALIBRATION_FREEZE_SHA256",
        ),
    ),
    "benchmark_longitudinal_g6_blind": (
        REPO_ROOT / "benchmark_longitudinal_g6_blind.py",
        (
            "RUBRIC",
            "derive_private_blind_mapping",
            "G6_BLIND_PROTOCOL_SHA256",
        ),
    ),
    "benchmark_longitudinal_g6_execution": (
        REPO_ROOT / "benchmark_longitudinal_g6_execution.py",
        (
            "build_runtime_cases",
            "inspect_execution_state",
            "run_execution_suite",
        ),
    ),
    "benchmark_longitudinal_g6_blind_bundle": (
        REPO_ROOT / "benchmark_longitudinal_g6_blind_bundle.py",
        (
            "discover_evidence_roots",
            "build_bundle_documents",
            "freeze_bundle",
        ),
    ),
    "benchmark_longitudinal_g6_scoring": (
        REPO_ROOT / "benchmark_longitudinal_g6_scoring.py",
        (
            "load_frozen_public_bundle",
            "prepare_review",
            "freeze_scores",
        ),
    ),
    "benchmark_longitudinal_g6_reveal": (
        REPO_ROOT / "benchmark_longitudinal_g6_reveal.py",
        (
            "verify_g6j_score_freeze",
            "open_and_verify_private_key",
            "reconcile_revealed_scores",
            "reveal_and_freeze",
        ),
    ),
    "benchmark_longitudinal_g7a_readiness": (
        REPO_ROOT / "benchmark_longitudinal_g7a_readiness.py",
        (
            "g7a_readiness_identity",
            "build_stage1_model_policies",
            "assert_paid_stage1_blocked",
            "G7A_READINESS_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7b_auxiliary_policy": (
        REPO_ROOT / "benchmark_longitudinal_g7b_auxiliary_policy.py",
        (
            "g7b_policy_identity",
            "build_g7b_model_policies",
            "assert_paid_stage1_blocked",
            "G7B_POLICY_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7c_stage1_integration": (
        REPO_ROOT / "benchmark_longitudinal_g7c_stage1_integration.py",
        (
            "g7c_integration_identity",
            "stage1_run_specs",
            "normalize_legacy_request",
            "prepare_external_s0_roots",
            "install_stage1_synthetic_proof_runtime",
            "G7C_INTEGRATION_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7d_accounting_policy": (
        REPO_ROOT / "benchmark_longitudinal_g7d_accounting_policy.py",
        (
            "g7d_accounting_identity",
            "pricing_snapshot",
            "account_provider_usage",
            "assert_paid_stage1_blocked",
            "G7D_ACCOUNTING_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7e_paid_authorization": (
        REPO_ROOT / "benchmark_longitudinal_g7e_paid_authorization.py",
        (
            "g7e_authorization_identity",
            "build_invocation_authorization",
            "verify_invocation_authorization",
            "classify_journal_resume_state",
            "classify_chapter_resume_state",
            "G7E_AUTHORIZATION_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7f_stage1_paid_executor": (
        REPO_ROOT / "benchmark_longitudinal_g7f_stage1_paid_executor.py",
        (
            "g7f_executor_identity",
            "build_stage1_invocation_plan",
            "verify_stage1_invocation_plan",
            "execute_stage1_invocation",
            "G7F_EXECUTOR_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7g_stage1_secure_launcher": (
        REPO_ROOT / "benchmark_longitudinal_g7g_stage1_secure_launcher.py",
        (
            "build_parser",
            "main",
            "init_secret",
            "run_preflight",
            "run_execute",
            "default_secret_path",
        ),
    ),
    "benchmark_longitudinal_g7j_reasoning_budget": (
        REPO_ROOT / "benchmark_longitudinal_g7j_reasoning_budget.py",
        (
            "g7j_policy_identity",
            "translation_completion_budget",
            "classify_translation_response",
            "G7J_POLICY_SHA256",
        ),
    ),
    "benchmark_longitudinal_g7k_fresh_s0_recovery": (
        REPO_ROOT / "benchmark_longitudinal_g7k_fresh_s0_recovery.py",
        (
            "g7k_policy_identity",
            "reconstruct_fresh_s0_from_historical",
            "verify_historical_incident",
            "verify_fresh_s0",
            "prepare_fresh_s0_recovery",
            "verify_recovery",
            "G7K_POLICY_SHA256",
        ),
    ),
    "benchmark_longitudinal_g6_recovery": (
        REPO_ROOT / "benchmark_longitudinal_g6_recovery.py",
        (
            "inspect_recovery_state",
            "recovery_plan_identity",
            "run_recovery_suite",
        ),
    ),
    "benchmark_longitudinal_g6_mimo_recovery": (
        REPO_ROOT / "benchmark_longitudinal_g6_mimo_recovery.py",
        (
            "inspect_second_recovery_state",
            "second_recovery_plan_identity",
            "run_second_recovery_suite",
        ),
    ),
    "benchmark_longitudinal_plan": (
        REPO_ROOT / "benchmark_longitudinal_plan.py",
        (
            "build_benchmark_plan",
            "build_default_plan",
            "synthetic_source_identities",
        ),
    ),
    "benchmark_longitudinal_g10_paid_authorization": (
        REPO_ROOT / "benchmark_longitudinal_g10_paid_authorization.py",
        (
            "g10_paid_authorization_identity",
            "frozen_execution_binding",
            "build_paid_authorization",
            "verify_paid_authorization",
            "preflight_and_reserve",
            "settle_accepted",
            "quarantine_chronological_track",
            "G10PAID_AUTHORIZATION_SHA256",
        ),
    ),
    "benchmark_longitudinal_g10f_matched_executor": (
        REPO_ROOT / "benchmark_longitudinal_g10f_matched_executor.py",
        (
            "g10f_executor_identity",
            "build_g10f_invocation",
            "verify_g10f_invocation",
            "execute_g10f_invocation",
            "G10F_EXECUTOR_SHA256",
        ),
    ),
    "benchmark_longitudinal_g10g_chronological_executor": (
        REPO_ROOT / "benchmark_longitudinal_g10g_chronological_executor.py",
        (
            "g10g_executor_identity",
            "build_g10g_invocation",
            "verify_g10g_invocation",
            "execute_g10g_invocation",
            "resolve_track_root",
            "G10G_EXECUTOR_SHA256",
        ),
    ),
}

# The benchmark modules must not import the provider SDK or dotenv at module
# import time; only client construction may lazily import openai.
FORBIDDEN_IMPORT_ROOTS = ("openai", "dotenv")


def _forbid(*_args, **_kwargs):
    raise AssertionError("module import must not perform this operation")


def _install_import_guards(monkeypatch):
    """Forbid environment reads, network, and provider-SDK imports at import time."""
    monkeypatch.setattr(os, "getenv", _forbid)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS:
            raise AssertionError(
                "benchmark modules must not import the provider SDK (or dotenv) at "
                "import time"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def _import_inert(monkeypatch, tmp_path, name):
    source, _symbols = MODULES[name]
    unique = f"_longitudinal_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, source)
    module = importlib.util.module_from_spec(spec)
    _install_import_guards(monkeypatch)
    monkeypatch.chdir(tmp_path)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(unique, None)
        raise
    return unique, module


@pytest.mark.parametrize("name", sorted(MODULES))
def test_import_is_zero_write_zero_network_zero_credentials(monkeypatch, tmp_path, name):
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    unique, module = _import_inert(monkeypatch, tmp_path, name)
    try:
        after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
        assert after == before
        assert after == []
        for symbol in MODULES[name][1]:
            assert hasattr(module, symbol), f"{name} must expose {symbol}"
    finally:
        sys.modules.pop(unique, None)


def test_longitudinal_modules_are_import_safe_together(monkeypatch, tmp_path):
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    for name in sorted(MODULES):
        unique, _module = _import_inert(monkeypatch, tmp_path, name)
        sys.modules.pop(unique, None)
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before


def test_import_safety_does_not_require_openai_preload(monkeypatch, tmp_path):
    # Self-contained: nothing pre-imports openai.  If any benchmark module tried
    # to import it during module import, the guard would fail the test.
    saved = sys.modules.pop("openai", None)
    try:
        unique, module = _import_inert(monkeypatch, tmp_path, "benchmark_longitudinal_provider")
        try:
            assert "openai" not in sys.modules
            assert not hasattr(module, "OpenAI")
            assert hasattr(module, "_default_client_factory")
        finally:
            sys.modules.pop(unique, None)
    finally:
        if saved is not None:
            sys.modules["openai"] = saved


def test_g7g_launcher_exposes_executable_entrypoint_without_import_effects(
    monkeypatch, tmp_path
):
    source, _symbols = MODULES["benchmark_longitudinal_g7g_stage1_secure_launcher"]
    text = source.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in text
    assert "raise SystemExit(main())" in text

    unique, module = _import_inert(
        monkeypatch, tmp_path, "benchmark_longitudinal_g7g_stage1_secure_launcher"
    )
    try:
        assert callable(module.main)
        assert callable(module.build_parser)
    finally:
        sys.modules.pop(unique, None)
