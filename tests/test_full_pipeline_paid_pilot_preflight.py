import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_experiment_harness as harness
from full_pipeline_paid_pilot_preflight import (
    AUTHORIZATION_PREFIX,
    PREFLIGHT_FILENAME,
    FullPipelinePaidPilotPreflightError,
    freeze_paid_pilot_preflight,
    load_paid_pilot_preflight,
    validate_paid_pilot_preflight_for_first_provider_operation,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")
ANCHOR = "synthetic-phase60i-anchor"
FIXED_NOW = datetime(2026, 9, 15, 23, 45, tzinfo=timezone.utc)


def _helpers():
    name = "_phase60i_preflight_harness_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _make(tmp_path):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path)
    experiment = tmp_path / "paid_pilot_experiment"
    manifest = v3.create_experiment(
        experiment,
        repo,
        repo_anchor=ANCHOR,
        target_chapters=25,
        experiment_id="phase60i-preflight-test",
        now=lambda: FIXED_NOW,
    )
    return repo, experiment, manifest


def _freeze(experiment, repo, **overrides):
    kwargs = {
        "expected_repo_anchor": ANCHOR,
        "pilot_wave_count": 2,
        "max_sync_model_calls": 10,
        "max_batch_status_reads": 6,
        "max_batch_reconciliation_reads": 2,
        "max_total_paid_cost_usd": "1.50",
        "now": lambda: FIXED_NOW,
    }
    kwargs.update(overrides)
    return freeze_paid_pilot_preflight(experiment, repo, **kwargs)


def test_freeze_binds_fresh_s0_wave1_causal_plan_audits_and_budgets(tmp_path):
    repo, experiment, manifest = _make(tmp_path)
    production_before = harness.capture_production_guard(repo, manifest["novel_ids"])

    frozen = _freeze(experiment, repo)

    assert frozen["experiment_manifest_sha256"] == manifest["experiment_manifest_sha256"]
    assert frozen["repo_anchor"] == ANCHOR
    assert frozen["pilot_scope"] == {
        "start_wave": 1,
        "end_wave": 2,
        "wave_count": 2,
        "novel_ids": ["great_king_zhuxia", "rogue_first_prince"],
        "full_experiment_target_chapters": 25,
    }
    assert frozen["execution_gate"]["provider_execution_authorized_by_this_module"] is False
    assert frozen["execution_gate"]["status"] == "frozen_preflight_only"

    wave1, wave2 = frozen["wave_plan"]
    assert wave1["request_identity_policy"] == "exact_s0_preview_bound"
    assert wave1["depends_on_prior_wave_commit"] is False
    assert wave1["batch_model_request_count"] == 2
    assert len(wave1["request_previews"]) == 2
    assert {item["chapter"] for item in wave1["request_previews"]} == {1}
    assert all(len(item["body_sha256"]) == 64 for item in wave1["request_previews"])
    assert all(
        len(item["lineage_before_sha256"]) == 64
        for item in wave1["request_previews"]
    )
    wave1_preview_by_novel = {
        item["novel_id"]: item for item in wave1["request_previews"]
    }
    assert wave2["request_identity_policy"] == "deferred_until_prior_wave_committed"
    assert wave2["depends_on_prior_wave_commit"] is True
    assert "request_previews" not in wave2

    for novel_id in manifest["novel_ids"]:
        audit = frozen["audit_schedule"]["by_novel"][novel_id]
        assert audit["audit_interval"] == 25
        assert audit["full_experiment_due_chapters"] == [25]
        assert audit["pilot_due_chapters"] == []

        project = frozen["projects"][novel_id]
        causal = project["initial_causal_lineage"]
        scheduler = causal["scheduler"]
        state_binding = project["initial_state_binding"]
        assert causal == manifest["initial_lineages"][novel_id]
        assert state_binding["causal"] == causal
        assert wave1_preview_by_novel[novel_id]["lineage_before_sha256"] == (
            harness._sha256_text(harness._canonical_json(causal))
        )
        assert scheduler["completed"] == []
        assert scheduler["failed"] == []
        assert scheduler["last_completed"] == 0
        assert scheduler["next_chapter"] == 1
        assert scheduler["last_audited_chapter"] == 0
        assert state_binding["committed_evidence"]["committed_chapters_tree"]["file_count"] == 0
        assert state_binding["committed_evidence"]["committed_qa_tree"]["file_count"] == 0
        assert state_binding["committed_evidence"]["audits_tree"]["file_count"] == 0
        assert project["pending_tree"]["file_count"] == 0
        assert project["provider_journal_tree"]["file_count"] == 0
        assert project["audit_recovery_tree"]["file_count"] == 0

    budget = frozen["operation_budget"]
    assert budget["batch_input_upload_attempts_cap"] == 2
    assert budget["batch_create_attempts_cap"] == 2
    assert budget["batch_model_requests_exact"] == 4
    assert budget["batch_status_reads_cap"] == 6
    assert budget["batch_reconciliation_list_reads_cap"] == 2
    assert budget["batch_output_downloads_cap"] == 2
    assert budget["sync_model_calls_cap"] == 10
    assert budget["paid_model_request_cap"] == 14
    assert budget["provider_api_call_cap"] == 24
    assert frozen["max_total_paid_cost_usd"] == "1.5"

    assert frozen["authorization_token"] == (
        f"{AUTHORIZATION_PREFIX}:{frozen['preflight_sha256']}"
    )
    assert (experiment / PREFLIGHT_FILENAME).is_file()
    assert list((experiment / "waves").iterdir()) == []
    assert harness.capture_production_guard(repo, manifest["novel_ids"]) == production_before

    validated = validate_paid_pilot_preflight_for_first_provider_operation(
        experiment,
        repo,
        expected_repo_anchor=ANCHOR,
        authorization_token=frozen["authorization_token"],
    )
    assert validated == frozen


def test_freeze_is_single_publish_and_tamper_evident(tmp_path):
    repo, experiment, _manifest = _make(tmp_path)
    frozen = _freeze(experiment, repo)

    with pytest.raises(FullPipelinePaidPilotPreflightError, match="already exists"):
        _freeze(experiment, repo)

    path = experiment / PREFLIGHT_FILENAME
    damaged = _read(path)
    damaged["max_total_paid_cost_usd"] = "999"
    _write(path, damaged)
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="hash mismatch"):
        load_paid_pilot_preflight(experiment, repo, expected_repo_anchor=ANCHOR)

    assert frozen["preflight_sha256"] != damaged.get("max_total_paid_cost_usd")


def test_freeze_rejects_wrong_anchor_nonfresh_state_and_wave_residue(tmp_path):
    repo, experiment, _manifest = _make(tmp_path)

    with pytest.raises(FullPipelinePaidPilotPreflightError, match="repo_anchor differs"):
        _freeze(experiment, repo, expected_repo_anchor="wrong-anchor")

    progress_path = (
        experiment / "projects" / "great_king_zhuxia" / "data" / "progress.json"
    )
    progress = _read(progress_path)
    progress["next_chapter"] = 2
    _write(progress_path, progress)
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="frozen S0 causal lineage"):
        _freeze(experiment, repo)

    repo2, experiment2, _manifest2 = _make(tmp_path / "wave-residue")
    (experiment2 / "waves" / "wave_0001").mkdir()
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="no wave artifacts"):
        _freeze(experiment2, repo2)


def test_freeze_rejects_source_guard_drift_and_invalid_operation_caps(tmp_path):
    repo, experiment, _manifest = _make(tmp_path)
    addendum = repo / "novels" / "great_king_zhuxia" / "translation_addendum.txt"
    addendum.write_text("source drift\n", encoding="utf-8")
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="production projects differ"):
        _freeze(experiment, repo)

    repo2, experiment2, _manifest2 = _make(tmp_path / "caps")
    with pytest.raises(
        FullPipelinePaidPilotPreflightError,
        match="at least one status observation per wave",
    ):
        _freeze(experiment2, repo2, max_batch_status_reads=1)
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="finite and positive"):
        _freeze(experiment2, repo2, max_total_paid_cost_usd="0")


def test_final_gate_rejects_wrong_token_or_postfreeze_engine_drift(tmp_path):
    repo, experiment, _manifest = _make(tmp_path)
    frozen = _freeze(experiment, repo)

    with pytest.raises(FullPipelinePaidPilotPreflightError, match="token was not supplied"):
        validate_paid_pilot_preflight_for_first_provider_operation(
            experiment,
            repo,
            expected_repo_anchor=ANCHOR,
            authorization_token="wrong-token",
        )

    marker = repo / "profiles" / "phase60i-drift-marker.txt"
    marker.write_text("drift\n", encoding="utf-8")
    with pytest.raises(FullPipelinePaidPilotPreflightError, match="engine/profile binding"):
        validate_paid_pilot_preflight_for_first_provider_operation(
            experiment,
            repo,
            expected_repo_anchor=ANCHOR,
            authorization_token=frozen["authorization_token"],
        )
