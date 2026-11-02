import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import operator_preflight as operator
from novel_project import NovelConfig, NovelProject
from semantic_integrity_audit import protected_state_snapshot


def _project(tmp_path, *, paused=False):
    root = tmp_path / "novels" / "fixture"
    root.mkdir(parents=True)
    pause_file = root / "pause.flag"
    if paused:
        pause_file.write_text("", encoding="utf-8")
    return SimpleNamespace(
        root=root,
        pause_file=pause_file,
        config=SimpleNamespace(
            id="fixture",
            title="Fixture Novel",
            profile="historical_military",
            final_chapter=3,
        ),
    )


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _real_project(
    tmp_path,
    *,
    completed=(1,),
    outputs=None,
    pending_model=None,
    paused=False,
):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=2,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)

    _write_json(project.config_file, config.to_dict())
    _write_json(
        project.chapters_file,
        [
            {"chapter": 1, "text": "甲 entered."},
            {"chapter": 2, "text": "乙 followed."},
        ],
    )
    _write_json(
        project.glossary_file,
        {
            "entries": {
                "甲": {
                    "english": "Jia",
                    "category": "character",
                    "status": "locked",
                }
            }
        },
    )

    completed = list(completed)
    last_completed = max(completed) if completed else 0
    _write_json(
        project.progress_file,
        {
            "completed": completed,
            "last_completed": last_completed,
            "next_chapter": last_completed + 1,
        },
    )
    _write_json(
        project.continuity_file,
        {"latest_chapter": last_completed, "summary": ""},
    )
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")

    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)

    if outputs is None:
        outputs = {
            chapter: f"Committed Chapter {chapter}"
            for chapter in completed
        }
    for chapter, text in outputs.items():
        (project.chapter_dir / f"{chapter:04d}.md").write_text(
            text,
            encoding="utf-8",
        )

    if pending_model is not None:
        next_chapter = last_completed + 1
        (project.pending_dir / f"{next_chapter:04d}.md").write_text(
            "An already-paid pending translation.",
            encoding="utf-8",
        )
        _write_json(
            project.pending_dir / f"{next_chapter:04d}.json",
            {
                "chapter": next_chapter,
                "stage": "translated",
                "translation_model": pending_model,
            },
        )

    if paused:
        project.pause_file.write_text("", encoding="utf-8")

    return project


def _preflight(**overrides):
    report = {
        "status": "PASS",
        "errors": [],
        "requested_model": "deepseek-v4-pro",
        "effective_model": "deepseek-v4-pro",
        "pending_status": "requested",
        "pending_chapter": None,
        "completed_pending_cleanup_required": False,
        "completed_pending_cleanup_chapters": [],
        "completed_chapter_count": 1,
        "committed_output_count": 1,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": True,
    }
    report.update(overrides)
    return report


def _install(
    monkeypatch,
    project,
    *,
    preflight=None,
    recovery_plan=None,
):
    monkeypatch.setattr(
        operator,
        "load_novel_project",
        lambda *a, **k: project,
    )
    monkeypatch.setattr(
        operator,
        "run_production_path_preflight",
        lambda *a, **k: (
            _preflight()
            if preflight is None
            else preflight
        ),
    )
    if recovery_plan is not None:
        monkeypatch.setattr(
            operator,
            "build_pending_recovery_plan",
            lambda *a, **k: recovery_plan,
        )


def test_operator_module_does_not_import_paid_executors():
    tree = ast.parse(
        Path(operator.__file__).read_text(encoding="utf-8")
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "pending_recovery_execute" not in imported
    assert "controlled_trial_execute" not in imported
    assert "controlled_trial_peak_allowed" not in imported


def test_real_clean_project_routes_to_production_without_mutation(
    tmp_path,
):
    project = _real_project(tmp_path)
    before = protected_state_snapshot(project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == operator.STATUS_PRODUCTION_READY
    assert report["preflight_status"] == "PASS"
    assert report["requested_model"] == "deepseek-v4-pro"
    assert report["effective_model"] == "deepseek-v4-pro"
    assert report["pending_status"] == "requested"
    assert report["recommended_entrypoint"] == "translate.py"
    assert report["paid_execution_authorized"] is False
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_cleanup_ready_routes_to_local_zero_provider_cleanup(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_pending_cleanup_required=True,
            completed_pending_cleanup_chapters=[1],
        ),
    )
    monkeypatch.setattr(
        operator,
        "build_pending_recovery_plan",
        lambda *args, **kwargs: pytest.fail(
            "paid recovery planner ran before completed cleanup"
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_CLEANUP_READY
    assert report["recommended_action"] == "run_completed_pending_cleanup"
    assert report["recommended_entrypoint"] == "completed_pending_cleanup.py"
    assert report["recommended_args"] == ["--novel", "fixture"]
    assert report["completed_pending_cleanup_chapters"] == [1]
    assert report["paid_execution_authorized"] is False
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_complete_novel_with_cleanup_residue_is_not_declared_complete(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_pending_cleanup_required=True,
            completed_pending_cleanup_chapters=[3],
            completed_chapter_count=3,
            committed_output_count=3,
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_CLEANUP_READY
    assert report["recommended_entrypoint"] == "completed_pending_cleanup.py"


def test_pause_preserves_cleanup_ready_resume_route(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path, paused=True)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_pending_cleanup_required=True,
            completed_pending_cleanup_chapters=[1],
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_PAUSED
    assert report["resume_route"] == operator.STATUS_CLEANUP_READY
    assert report["recommended_action"] == "remove_manual_pause"



def test_real_flash_pinned_pending_routes_to_bounded_recovery(
    tmp_path,
):
    project = _real_project(
        tmp_path,
        pending_model="deepseek-v4-flash",
    )
    before = protected_state_snapshot(project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == operator.STATUS_RECOVERY_READY
    assert report["preflight_status"] == "PASS"
    assert report["pending_status"] == "pending_model_override"
    assert report["pending_chapter"] == 2
    assert report["requested_model"] == "deepseek-v4-pro"
    assert report["effective_model"] == "deepseek-v4-flash"
    assert report["recovery"]["requested_auxiliary_model"] == (
        "deepseek-v4-pro"
    )
    assert report["recovery"]["effective_auxiliary_model"] == (
        "deepseek-v4-flash"
    )
    assert report["recovery"]["translation_generation_allowed"] is False
    assert report["recommended_entrypoint"] == "pending_recovery_plan.py"
    assert report["paid_execution_authorized"] is False
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_real_progress_output_inconsistency_routes_to_blocked(
    tmp_path,
):
    project = _real_project(
        tmp_path,
        outputs={},
    )
    before = protected_state_snapshot(project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == operator.STATUS_BLOCKED
    assert report["preflight_status"] == "FAIL"
    assert report["errors"]
    assert report["recommended_entrypoint"] == ""


def test_real_pause_flag_routes_to_paused_without_mutation(
    tmp_path,
):
    project = _real_project(tmp_path, paused=True)
    before = protected_state_snapshot(project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == operator.STATUS_PAUSED
    assert report["resume_route"] == operator.STATUS_PRODUCTION_READY
    assert report["recommended_action"] == "remove_manual_pause"


def test_real_complete_project_routes_to_complete(
    tmp_path,
):
    project = _real_project(
        tmp_path,
        completed=(1, 2),
    )
    before = protected_state_snapshot(project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == operator.STATUS_COMPLETE
    assert report["completed_chapter_count"] == 2
    assert report["committed_output_count"] == 2
    assert report["recommended_action"] == "none"


def test_finished_chapters_with_unfinished_last_audit_are_not_complete(tmp_path):
    project = _real_project(tmp_path, completed=(1, 2))
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress["last_audited_chapter"] = 1
    project.progress_file.write_text(json.dumps(progress), encoding="utf-8")

    report = operator.build_operator_preflight("fixture", repo_root=tmp_path, model_selection="pro")

    assert report["status"] != operator.STATUS_COMPLETE


def test_clean_unfinished_project_routes_to_production(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(monkeypatch, project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_PRODUCTION_READY
    assert report["recommended_action"] == "run_production"
    assert report["recommended_entrypoint"] == "translate.py"
    assert report["effective_model"] == "deepseek-v4-pro"
    assert report["optional_controlled_trial_plan"]["entrypoint"] == (
        "controlled_trial_plan.py"
    )
    assert report["paid_execution_authorized"] is False
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_recoverable_pending_routes_to_recovery_and_preserves_override(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    preflight = _preflight(
        effective_model="deepseek-v4-flash",
        pending_status="pending_model_override",
        pending_chapter=2,
    )
    recovery_plan = {
        "status": "READY",
        "target_chapter": 2,
        "pending_stage": "state_extracted",
        "requested_auxiliary_model": "deepseek-v4-pro",
        "auxiliary_model": "deepseek-v4-flash",
        "minimum_new_logical_api_calls": 0,
        "max_api_calls": 6,
        "max_cost_usd": 0.10,
        "translation_generation_allowed": False,
        "startup_periodic_audits_allowed": False,
        "state_fingerprint": "f" * 64,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": True,
    }
    _install(
        monkeypatch,
        project,
        preflight=preflight,
        recovery_plan=recovery_plan,
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
        max_api_calls=6,
        max_cost_usd=0.10,
    )

    assert report["status"] == operator.STATUS_RECOVERY_READY
    assert report["requested_model"] == "deepseek-v4-pro"
    assert report["effective_model"] == "deepseek-v4-flash"
    assert report["recovery"]["effective_auxiliary_model"] == (
        "deepseek-v4-flash"
    )
    assert report["recovery"]["translation_generation_allowed"] is False
    assert report["recommended_entrypoint"] == "pending_recovery_plan.py"
    assert report["paid_execution_authorized"] is False


def test_recovery_plan_failure_blocks_instead_of_guessing(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    preflight = _preflight(
        pending_status="legacy_unpinned",
        pending_chapter=2,
    )
    _install(monkeypatch, project, preflight=preflight)

    def fail(*_args, **_kwargs):
        raise operator.PendingRecoveryPlanError(
            "pending metadata is ambiguous"
        )

    monkeypatch.setattr(
        operator,
        "build_pending_recovery_plan",
        fail,
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "ambiguous" in report["errors"][0]
    assert report["recommended_entrypoint"] == ""


def test_failed_production_preflight_is_blocked(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            status="FAIL",
            errors=["progress and committed outputs disagree"],
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert report["preflight_errors"] == [
        "progress and committed outputs disagree"
    ]


def test_unexpected_preflight_exception_is_contained_as_blocked(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    monkeypatch.setattr(
        operator,
        "load_novel_project",
        lambda *a, **k: project,
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(
        operator,
        "run_production_path_preflight",
        explode,
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "RuntimeError" in report["errors"][0]


def test_manual_pause_blocks_unfinished_normal_production_route(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path, paused=True)
    _install(monkeypatch, project)

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_PAUSED
    assert report["resume_route"] == operator.STATUS_PRODUCTION_READY
    assert report["recommended_action"] == "remove_manual_pause"


def test_manual_pause_blocks_recovery_but_preserves_resume_route(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path, paused=True)
    preflight = _preflight(
        pending_status="pending_model_match",
        pending_chapter=2,
    )
    recovery_plan = {
        "status": "READY",
        "target_chapter": 2,
        "pending_stage": "reviewed",
        "requested_auxiliary_model": "deepseek-v4-pro",
        "auxiliary_model": "deepseek-v4-pro",
        "minimum_new_logical_api_calls": 0,
        "max_api_calls": 6,
        "max_cost_usd": 0.10,
        "translation_generation_allowed": False,
        "startup_periodic_audits_allowed": False,
        "state_fingerprint": "a" * 64,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": True,
    }
    _install(
        monkeypatch,
        project,
        preflight=preflight,
        recovery_plan=recovery_plan,
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_PAUSED
    assert report["resume_route"] == operator.STATUS_RECOVERY_READY
    assert report["recovery"]["status"] == "READY"


def test_complete_project_is_complete_even_if_pause_flag_remains(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path, paused=True)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_chapter_count=3,
            committed_output_count=3,
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_COMPLETE
    assert report["manual_pause_active"] is True
    assert report["recommended_action"] == "none"


def test_no_pending_model_override_fails_closed(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            effective_model="deepseek-v4-flash",
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "without a pending translation" in report["errors"][0]


def test_preflight_side_effect_evidence_must_remain_zero(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(api_calls=1),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "provider calls" in report["errors"][0]


def test_completed_and_committed_counts_must_match(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_chapter_count=2,
            committed_output_count=1,
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "counts disagree" in report["errors"][0]


def test_unknown_pending_status_fails_closed(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            pending_status="mystery",
            pending_chapter=2,
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "unknown pending status" in report["errors"][0]


def test_completed_count_beyond_final_chapter_fails_closed(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _install(
        monkeypatch,
        project,
        preflight=_preflight(
            completed_chapter_count=4,
            committed_output_count=4,
        ),
    )

    report = operator.build_operator_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert report["status"] == operator.STATUS_BLOCKED
    assert "exceeds configured final chapter" in report["errors"][0]



@pytest.mark.parametrize("allowance", [None, 0.25])
def test_clean_goat_operator_routes_only_to_complete_bounded_plan(tmp_path, capsys, allowance):
    from test_controlled_trial_plan import _empty_project
    from translate import build_parser as trial_parser
    project = _empty_project(tmp_path)
    report = operator.build_operator_preflight("fixture", repo_root=tmp_path, model_selection="v41-flash",
        provider_selection="command_code_provider", max_api_calls=3, max_credit_equivalent_usd=allowance)
    assert report["structurally_ready"] is True
    assert report["provider_invoice_claimed"] is False
    assert report["budget_unit"] == "usd_credit_equivalent_per_1m_tokens"
    if allowance is None:
        assert report["status"] == "BOUNDED_ALLOWANCE_REQUIRED"
        assert report["recommended_entrypoint"] == ""
        assert report["recommended_args"] == []
    else:
        assert report["status"] == "BOUNDED_GOAT_READY"
        assert report["recommended_entrypoint"] == "translate.py"
        assert report["recommended_args"] == ["--provider", "command_code_provider", "--novel", "fixture",
            "--model", "v41-flash", "--max-api-calls", "3", "--max-credit-equivalent-usd", "0.25", "--plan-goat-run"]
        parsed = trial_parser().parse_args(report["recommended_args"])
        assert parsed.max_api_calls == 3 and parsed.max_credit_equivalent_usd == allowance
        assert parsed.plan_goat_run is True
    operator._print_human(report)
    text = capsys.readouterr().out
    assert "credit-equivalent USD" in text
    assert "provider_invoice_claimed=false" in text
    assert ("translate.py" in text) == (allowance is not None)


@pytest.mark.parametrize("allowance", [0, -1, True, float("nan"), float("inf")])
def test_goat_operator_rejects_invalid_credit_allowance(tmp_path, allowance):
    from test_controlled_trial_plan import _empty_project
    _empty_project(tmp_path)
    with pytest.raises(ValueError):
        operator.build_operator_preflight("fixture", repo_root=tmp_path, model_selection="v41-flash",
            provider_selection="command_code_provider", max_api_calls=3, max_credit_equivalent_usd=allowance)



@pytest.mark.parametrize("allowance,status", [(None, "BOUNDED_ALLOWANCE_REQUIRED"), (0.25, "BOUNDED_GOAT_READY")])
def test_paused_clean_goat_never_resumes_to_unattended_route(tmp_path, allowance, status):
    from test_controlled_trial_plan import _empty_project
    project = _empty_project(tmp_path)
    project.pause_file.write_text("pause", encoding="utf-8")
    report = operator.build_operator_preflight("fixture", repo_root=tmp_path, model_selection="v41-flash",
        provider_selection="command_code_provider", max_api_calls=3, max_credit_equivalent_usd=allowance)
    assert report["status"] == "PAUSED"
    assert report["resume_route"] == status
    assert report["recommended_entrypoint"] == "" and report["recommended_args"] == []



@pytest.mark.parametrize("allowance,expected", [(None, 2), (0.25, 0)])
def test_goat_operator_cli_status_matches_bounded_readiness(tmp_path, capsys, allowance, expected):
    from test_controlled_trial_plan import _empty_project
    _empty_project(tmp_path)
    args = ["--repo-root", str(tmp_path), "--novel", "fixture", "--model", "v41-flash",
        "--provider", "command_code_provider", "--max-api-calls", "3", "--json"]
    if allowance is not None:
        args += ["--max-credit-equivalent-usd", str(allowance)]
    assert operator.main(args) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["api_calls"] == report["project_writes"] == 0
    assert report["paid_execution_authorized"] is False


@pytest.fixture(autouse=True)
def explicit_direct_route(monkeypatch):
    """These compatibility proofs exercise the explicitly selected direct route."""
    monkeypatch.setenv("NOVELTRANSLATOR_PROVIDER_BACKEND", "direct_deepseek")
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", "pro")
