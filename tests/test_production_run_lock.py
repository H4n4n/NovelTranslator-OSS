import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from production_run_lock import LOCK_NAME, ProductionRunLockedError, production_run_lock


@pytest.mark.parametrize("error", [None, RuntimeError("stop"), KeyboardInterrupt(), SystemExit(3)])
def test_exclusive_lock_releases_on_every_exit_and_keeps_rendezvous(tmp_path, error):
    def run():
        with production_run_lock(tmp_path):
            with pytest.raises(ProductionRunLockedError):
                with production_run_lock(tmp_path):
                    pytest.fail("second writer acquired lock")
            if error is not None:
                raise error
    if error is None:
        run()
    else:
        with pytest.raises(type(error)) as caught:
            run()
        assert caught.value is error
    assert (tmp_path / LOCK_NAME).is_file()
    with production_run_lock(tmp_path):
        pass


def test_different_novels_do_not_share_lock(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    with production_run_lock(first), production_run_lock(second):
        assert (first / LOCK_NAME).exists() and (second / LOCK_NAME).exists()


def test_separate_process_is_rejected_and_hard_kill_releases_lock(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    script = (
        "import sys; from pathlib import Path; "
        "from production_run_lock import production_run_lock; "
        "root=Path(sys.argv[1]); "
        "lock=production_run_lock(root); lock.__enter__(); "
        "(root/'ready').write_text('ready'); sys.stdin.read()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            if child.poll() is not None:
                pytest.fail(child.communicate()[1].decode())
            if time.monotonic() >= deadline:
                pytest.fail("child lock acquisition timed out")
            time.sleep(0.01)
        with pytest.raises(ProductionRunLockedError):
            with production_run_lock(tmp_path):
                pytest.fail("simultaneous writer acquired lock")
    finally:
        child.kill()
        child.communicate(timeout=10)
    with production_run_lock(tmp_path):
        pass  # No PID inspection or stale-file deletion needed after hard kill.


def test_filesystem_open_error_is_not_misclassified_as_contention(tmp_path):
    (tmp_path / LOCK_NAME).mkdir()
    with pytest.raises(OSError) as caught:
        with production_run_lock(tmp_path):
            pytest.fail("invalid lock file accepted")
    assert not isinstance(caught.value, ProductionRunLockedError)


def test_normal_launcher_rejects_second_run_before_runtime_preparation(tmp_path, monkeypatch):
    import run_production_engine as launcher
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "prepare_production_runtime",
                        lambda *a, **k: pytest.fail("runtime was reached"))
    with production_run_lock(tmp_path):
        with pytest.raises(ProductionRunLockedError):
            launcher.main()


def test_direct_legacy_main_rejects_second_run_before_initialization(tmp_path, monkeypatch):
    import run_production_engine as launcher
    monkeypatch.chdir(tmp_path)
    module = launcher.load_production_module()
    monkeypatch.setattr(module, "initialize_production_runtime_state",
                        lambda: pytest.fail("initializer was reached"))
    with production_run_lock(tmp_path):
        with pytest.raises(ProductionRunLockedError):
            module.main()


@pytest.mark.parametrize("kind", ["recovery", "trial"])
def test_paid_executor_locks_before_authorization_and_credentials(tmp_path, monkeypatch, kind):
    import pending_recovery_execute as recovery
    import controlled_trial_execute as trial
    module, function, flag, validator = (
        (recovery, recovery.execute_pending_recovery, "authorize_paid_recovery", "validate_recovery_authorization")
        if kind == "recovery" else
        (trial, trial.execute_controlled_trial, "authorize_paid_trial", "validate_execution_authorization")
    )
    monkeypatch.setattr(module, "load_novel_project", lambda *a, **k: SimpleNamespace(root=tmp_path))
    calls = []
    class ValidatedUnderLock(Exception):
        pass
    def validate(*args, **kwargs):
        calls.append("validate")
        with pytest.raises(ProductionRunLockedError):
            with production_run_lock(tmp_path):
                pytest.fail("authorization ran outside lock")
        raise ValidatedUnderLock()
    monkeypatch.setattr(module, validator, validate)
    kwargs = dict(repo_root=tmp_path, model_selection="pro", max_api_calls=1,
                  max_cost_usd=0.01, expected_state_fingerprint="x", approval_token="y",
                  **{flag: True})
    with production_run_lock(tmp_path):
        with pytest.raises(ProductionRunLockedError):
            function("fixture", **kwargs)
    assert calls == []
    with pytest.raises(ValidatedUnderLock):
        function("fixture", **kwargs)
    assert calls == ["validate"]
    with production_run_lock(tmp_path):
        pass


@pytest.mark.parametrize("kind", ["recovery", "trial"])
def test_no_paid_opt_in_does_not_create_coordination_file(tmp_path, monkeypatch, kind):
    import pending_recovery_execute as recovery
    import controlled_trial_execute as trial
    module, function, private = (
        (recovery, recovery.execute_pending_recovery, "_execute_pending_recovery_under_lock")
        if kind == "recovery" else
        (trial, trial.execute_controlled_trial, "_execute_controlled_trial_under_lock")
    )
    monkeypatch.setattr(module, private, lambda *a, **k: "read-only")
    monkeypatch.setattr(module, "production_run_lock", lambda *a: pytest.fail("lock requested"))
    assert function("fixture", repo_root=tmp_path, model_selection="pro", max_api_calls=1,
                    max_cost_usd=0.01, expected_state_fingerprint="x", approval_token="y") == "read-only"
    assert list(tmp_path.iterdir()) == []

def test_authorized_qa_backfill_locks_before_plan_rebuild(
    tmp_path,
    monkeypatch,
):
    import qa_provenance_backfill_execute as module

    monkeypatch.setattr(
        module,
        "load_novel_project",
        lambda *args, **kwargs: SimpleNamespace(root=tmp_path),
    )
    calls = []

    class RebuiltUnderLock(Exception):
        pass

    def under_lock(*args, **kwargs):
        calls.append("rebuilt")
        with pytest.raises(ProductionRunLockedError):
            with production_run_lock(tmp_path):
                pytest.fail("backfill plan rebuild ran outside writer lock")
        raise RebuiltUnderLock()

    monkeypatch.setattr(
        module,
        "_execute_qa_provenance_backfill_under_lock",
        under_lock,
    )

    kwargs = {
        "repo_root": tmp_path,
        "model_selection": "pro",
        "state_fingerprint": "state",
        "approval_token": "approval",
        "authorize_backfill": True,
    }

    with production_run_lock(tmp_path):
        with pytest.raises(ProductionRunLockedError):
            module.execute_qa_provenance_backfill(
                "fixture",
                1,
                **kwargs,
            )

    assert calls == []

    with pytest.raises(RebuiltUnderLock):
        module.execute_qa_provenance_backfill(
            "fixture",
            1,
            **kwargs,
        )

    assert calls == ["rebuilt"]


def test_unauthorized_qa_backfill_remains_lock_free(
    tmp_path,
    monkeypatch,
):
    import qa_provenance_backfill_execute as module

    monkeypatch.setattr(
        module,
        "_execute_qa_provenance_backfill_under_lock",
        lambda *args, **kwargs: "read-only",
    )
    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail(
            "unauthorized QA backfill requested writer lock"
        ),
    )
    monkeypatch.setattr(
        module,
        "load_novel_project",
        lambda *args, **kwargs: pytest.fail(
            "unauthorized QA backfill loaded project only for locking"
        ),
    )

    result = module.execute_qa_provenance_backfill(
        "fixture",
        1,
        repo_root=tmp_path,
        model_selection="pro",
        state_fingerprint="state",
        approval_token="approval",
        authorize_backfill=False,
    )

    assert result == "read-only"
    assert not (tmp_path / LOCK_NAME).exists()


WRITER_CLI_CASES = (
    (
        "benchmark_novel",
        ["--novel", "fixture", "--chapters", "1"],
        "load_novel_project",
        "load_benchmark_inputs",
        1,
    ),
    (
        "qa_provenance_backfill_execute",
        [
            "--novel", "fixture",
            "--chapter", "1",
            "--model", "pro",
            "--state-fingerprint", "state",
            "--approval-token", "approval",
            "--authorize-backfill",
        ],
        "load_novel_project",
        "_execute_qa_provenance_backfill_under_lock",
        2,
    ),
    (
        "completed_pending_cleanup",
        ["--novel", "fixture"],
        "load_novel_project",
        "inspect_completed_pending_cleanup",
        1,
    ),
    (
        "resolve_glossary_candidate_conflict",
        [
            "--novel", "fixture",
            "--chapter", "1",
            "--source", "源",
            "--choose", "fingerprint",
            "--rationale", "reviewed source evidence",
        ],
        "load_novel_project",
        "resolve_from_args",
        2,
    ),
    (
        "extract_epub",
        ["--novel", "fixture"],
        "select_project",
        "extract_epub",
        1,
    ),
    (
        "extract_txt",
        ["--novel", "fixture"],
        "select_project",
        "extract_source_txt",
        1,
    ),
    (
        "build_checkpoint_epub",
        ["--novel", "fixture"],
        "select_project",
        "build_epub",
        1,
    ),
    (
        "build_finalization_corpus",
        ["--novel", "fixture"],
        "select_project",
        "build_finalization_corpus",
        1,
    ),
    (
        "build_normalization_inventory",
        ["--novel", "fixture"],
        "select_project",
        "build_inventory",
        1,
    ),
    (
        "build_normalization_plan",
        ["--novel", "fixture"],
        "select_project",
        "build_plan",
        1,
    ),
    (
        "apply_normalization_plan",
        ["--novel", "fixture", "--apply"],
        "select_project",
        "load_plan_file",
        1,
    ),
    (
        "build_title_resolution_plan",
        ["--novel", "fixture"],
        "select_project",
        "build_plan",
        1,
    ),
    (
        "apply_title_resolution_plan",
        ["--novel", "fixture", "--apply"],
        "select_project",
        "load_plan_file",
        1,
    ),
    (
        "build_publication_chapters",
        ["--apply"],
        None,
        "build_plan",
        1,
    ),
    (
        "plan_final_editorial_chapters",
        ["--novel", "fixture"],
        "select_project",
        "build_plan_report",
        1,
    ),
    (
        "apply_final_editorial_chapters",
        ["--novel", "fixture"],
        "select_project",
        "apply_ready_plan",
        1,
    ),
    (
        "build_publication_epub",
        ["--novel", "fixture"],
        "select_project",
        "build_publication_epub",
        1,
    ),
)


@pytest.mark.parametrize(
    "module_name,argv,project_loader,writer_name,expected_exit",
    WRITER_CLI_CASES,
)
def test_supported_mutating_writer_clis_reject_contention_before_writer_logic(
    tmp_path,
    monkeypatch,
    module_name,
    argv,
    project_loader,
    writer_name,
    expected_exit,
):
    module = importlib.import_module(module_name)
    if project_loader is None:
        monkeypatch.setattr(module, "DEFAULT_FINAL_DIR", tmp_path / "final")
    else:
        monkeypatch.setattr(
            module,
            project_loader,
            lambda *args, **kwargs: SimpleNamespace(root=tmp_path),
        )
    monkeypatch.setattr(
        module,
        writer_name,
        lambda *args, **kwargs: pytest.fail(
            f"{module_name} writer logic ran while another writer owned the lock"
        ),
    )

    with production_run_lock(tmp_path):
        assert module.main(argv) == expected_exit


def test_txt_dry_run_does_not_request_writer_lock(tmp_path, monkeypatch):
    import extract_txt as module

    project = SimpleNamespace(root=tmp_path)
    result = object()
    monkeypatch.setattr(module, "select_project", lambda *args, **kwargs: project)
    monkeypatch.setattr(module, "extract_source_txt", lambda value: result)
    monkeypatch.setattr(module, "_print_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail("TXT dry-run requested writer lock"),
    )

    assert module.main(["--novel", "fixture", "--dry-run"]) == 0
    assert not (tmp_path / LOCK_NAME).exists()



@pytest.mark.parametrize("mode", ["--dry-run", "--verify-only"])
def test_normalization_application_read_only_modes_do_not_request_writer_lock(
    tmp_path, monkeypatch, mode
):
    import apply_normalization_plan as module

    project = SimpleNamespace(root=tmp_path)
    preview = object()
    monkeypatch.setattr(module, "select_project", lambda *args, **kwargs: project)
    monkeypatch.setattr(module, "load_plan_file", lambda *args, **kwargs: ({}, "plan-sha"))
    monkeypatch.setattr(
        module,
        "prepare_application",
        lambda *args, **kwargs: preview,
    )
    monkeypatch.setattr(module, "dry_run_summary", lambda value: {"status": "ready"})
    monkeypatch.setattr(
        module,
        "verify_existing_application",
        lambda *args, **kwargs: {
            "normalized_fingerprint": {"aggregate_sha256": "verified"}
        },
    )
    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail(
            f"normalization application {mode} requested writer lock"
        ),
    )

    assert module.main(["--novel", "fixture", mode]) == 0
    assert not (tmp_path / LOCK_NAME).exists()


@pytest.mark.parametrize("mode", ["--dry-run", "--verify-only"])
def test_title_application_read_only_modes_do_not_request_writer_lock(
    tmp_path, monkeypatch, mode
):
    import apply_title_resolution_plan as module

    project = SimpleNamespace(root=tmp_path)
    preview = SimpleNamespace(recovery_review_targets=[], edits=[])
    monkeypatch.setattr(module, "select_project", lambda *args, **kwargs: project)
    monkeypatch.setattr(module, "load_plan_file", lambda *args, **kwargs: ({}, "plan-sha"))
    monkeypatch.setattr(
        module,
        "prepare_application",
        lambda *args, **kwargs: preview,
    )
    monkeypatch.setattr(module, "dry_run_summary", lambda value: {"status": "ready"})
    monkeypatch.setattr(
        module,
        "verify_existing_application",
        lambda *args, **kwargs: {"output_fingerprint": {"aggregate_sha256": "verified"}},
    )
    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail(
            f"title application {mode} requested writer lock"
        ),
    )

    assert module.main(["--novel", "fixture", mode]) == 0
    assert not (tmp_path / LOCK_NAME).exists()


def test_publication_cleanup_dry_run_does_not_request_writer_lock(
    tmp_path, monkeypatch
):
    import build_publication_chapters as module

    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail(
            "publication cleanup dry-run requested writer lock"
        ),
    )
    monkeypatch.setattr(
        module,
        "_refuse_existing_targets",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(module, "build_plan", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        module,
        "build_report",
        lambda plan: {
            "total_files": 1,
            "changed_file_count": 0,
            "unchanged_file_count": 1,
            "transformation_counts": {},
            "source_corpus_fingerprint": {"aggregate_sha256": "source"},
            "output_corpus_fingerprint": {"aggregate_sha256": "output"},
        },
    )

    assert module.main(["--dry-run"]) == 0
    assert not (tmp_path / LOCK_NAME).exists()


def test_publication_epub_verify_only_does_not_request_writer_lock(
    tmp_path, monkeypatch
):
    import build_publication_epub as module

    project = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(module, "select_project", lambda *args, **kwargs: project)
    monkeypatch.setattr(
        module,
        "verify_existing_publication_epub",
        lambda *args, **kwargs: {
            "content_document_count": 1,
            "spine_count": 2,
            "toc_count": 1,
            "metadata_title": "Fixture",
            "metadata_language": "en",
        },
    )
    monkeypatch.setattr(
        module,
        "production_run_lock",
        lambda *args, **kwargs: pytest.fail(
            "publication EPUB verify-only requested writer lock"
        ),
    )

    assert module.main(["--novel", "fixture", "--verify-only"]) == 0
    assert not (tmp_path / LOCK_NAME).exists()
