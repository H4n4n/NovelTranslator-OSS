import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

import apply_normalization_plan as applier
import proven_stage_cleanup as stage_cleanup


ROGUE_BASELINE_SHA256 = "01c286974c5bfd21de5957e20d811d66a8887a71980814fc36e3aa0a97449cc3"
ROGUE_PLAN_SHA256 = "386c52d3c70d35c40f28a9f61e37682672dc17ff7955ef0a3a5ec646a3953564"


def _anchor(project_id, fingerprint, text, old="old term", replacement="new term"):
    paragraph, _, _ = applier.paragraph_spans(text)[1]
    start = paragraph.index(old)
    matches = applier.exact_occurrences(paragraph, old)
    anchor = {
        "unit": 1,
        "expected_final_unit": 1,
        "expected_baseline_file_sha256": fingerprint["files"][0]["sha256"],
        "target_paragraph": 2,
        "target_occurrence": 1,
        "target_start": start,
        "target_end": start + len(old),
        "target_occurrence_number_in_file": 1,
        "expected_file_occurrence_count": len(matches),
        "expected_paragraph_occurrence_count": len(matches),
        "expected_target_paragraph": paragraph,
        "expected_target_paragraph_sha256": hashlib.sha256(paragraph.encode()).hexdigest(),
        "exact_old_text": old,
        "exact_proposed_replacement": replacement,
        "confirmed_source_forms": ["synthetic-source-form"],
        "source_evidence": "exact_aligned_paragraph",
        "source_paragraph": 2,
    }
    anchor["occurrence_id"] = applier.physical_occurrence_id(project_id, anchor)
    return anchor


def _plan_fixture(tmp_path, text="# Chapter 1\n\nAn old term appears.\n"):
    baseline = tmp_path / "chapters"
    baseline.mkdir()
    (baseline / "0001.md").write_bytes(text.encode("utf-8"))
    fingerprint = applier.corpus_fingerprint(baseline)
    anchor = _anchor("synthetic_novel", fingerprint, text)
    decision = {
        "decision_id": "text-0001",
        "source_identity": "synthetic-source-identity",
        "source_form": "synthetic-source-form",
        "current_rendering": "old term",
        "proposed_rendering": "new term",
        "status": "APPROVED_CORRECTION",
        "exact_occurrence_count": 1,
        "occurrence_anchors": [anchor],
        "replacement_safe_automatically": True,
    }
    plan = {
        "schema_version": 3,
        "plan_revision": "2.1",
        "corpus": {
            "project_id": "synthetic_novel",
            "inventory_recorded_final_sha256": fingerprint["aggregate_sha256"],
            "verified_final_fingerprint": fingerprint,
        },
        "safety": {
            "source_identity_required": True,
            "global_string_replacement_allowed": False,
        },
        "application_policy": {
            "mode": "fail_closed_exact_occurrence_only",
            "paragraph_number_alone_is_sufficient": False,
            "global_replacement_authorized": False,
            "abort_on_any_precondition_mismatch": True,
            "review_required_is_executable": False,
        },
        "text_decisions": [decision],
        "pronoun_decisions": [],
        "summary_counts": {
            "APPROVED_CORRECTION": 1,
            "APPROVED_CORRECTION_OCCURRENCES": 1,
            "PRONOUN_APPROVED_CORRECTION_OCCURRENCES": 0,
            "TOTAL_APPROVED_PROSE_OCCURRENCES": 1,
            "REVIEW_REQUIRED": 0,
            "FALSE_POSITIVE_NO_CHANGE": 0,
        },
    }
    return baseline, plan


def _prepare(baseline, plan, allow_manual=False):
    return applier.prepare_application(
        baseline_dir=baseline,
        plan=plan,
        plan_sha256="a" * 64,
        project_id="synthetic_novel",
        allow_manual_approved=allow_manual,
    )


def _publish(tmp_path, preview):
    return applier.publish_normalized_corpus(
        preview,
        output_dir=tmp_path / "normalized_chapters",
        report_json_path=tmp_path / "reports" / "normalization_application.json",
        report_md_path=tmp_path / "reports" / "normalization_application.md",
    )


def _recount_summary(plan):
    statuses = [item["status"] for item in plan["text_decisions"]]
    approved = [item for item in plan["text_decisions"] if item["status"] == "APPROVED_CORRECTION"]
    manual = [
        item for item in plan["pronoun_decisions"]
        if (item.get("decision") or item.get("status")) == "APPROVED_CORRECTION"
    ]
    automatic_count = sum(item["exact_occurrence_count"] for item in approved)
    manual_count = sum(item["exact_occurrence_count"] for item in manual)
    plan["summary_counts"] = {
        "APPROVED_CORRECTION": len(approved),
        "APPROVED_CORRECTION_OCCURRENCES": automatic_count,
        "PRONOUN_APPROVED_CORRECTION_OCCURRENCES": manual_count,
        "TOTAL_APPROVED_PROSE_OCCURRENCES": automatic_count + manual_count,
        "REVIEW_REQUIRED": statuses.count("REVIEW_REQUIRED"),
        "FALSE_POSITIVE_NO_CHANGE": statuses.count("FALSE_POSITIVE_NO_CHANGE"),
    }


def _manual_only_plan(plan):
    plan = copy.deepcopy(plan)
    anchor = copy.deepcopy(plan["text_decisions"][0]["occurrence_anchors"][0])
    anchor["pronoun_token_change_count"] = 1
    anchor.pop("occurrence_id")
    plan["text_decisions"] = []
    plan["pronoun_decisions"] = [{
        "pronoun_id": "manual-0001",
        "decision": "APPROVED_CORRECTION",
        "automatic_safe": False,
        "source_identity": "synthetic-source-identity",
        "source_evidence": "human-reviewed exact synthetic evidence",
        "exact_occurrence_count": 1,
        "pronoun_token_change_count": 1,
        "occurrence_anchors": [anchor],
    }]
    _recount_summary(plan)
    return plan


def test_exact_anchored_replacement_succeeds(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    report = _publish(tmp_path, preview)
    assert (tmp_path / "normalized_chapters" / "0001.md").read_text() == (
        "# Chapter 1\n\nAn new term appears.\n"
    )
    assert report["application_counts"]["total_applied_prose_occurrences"] == 1
    assert report["independent_verification"]["all_differences_authorized"] is True


def test_incorrect_file_sha_aborts(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    anchor = plan["text_decisions"][0]["occurrence_anchors"][0]
    anchor["expected_baseline_file_sha256"] = "b" * 64
    anchor["occurrence_id"] = applier.physical_occurrence_id("synthetic_novel", anchor)
    with pytest.raises(applier.NormalizationError, match="baseline file SHA-256 mismatch"):
        _prepare(baseline, plan)


def test_incorrect_paragraph_sha_aborts(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    anchor = plan["text_decisions"][0]["occurrence_anchors"][0]
    anchor["expected_target_paragraph_sha256"] = "b" * 64
    anchor["occurrence_id"] = applier.physical_occurrence_id("synthetic_novel", anchor)
    with pytest.raises(applier.NormalizationError, match="paragraph SHA-256 mismatch"):
        _prepare(baseline, plan)


def test_missing_old_text_aborts(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    anchor = plan["text_decisions"][0]["occurrence_anchors"][0]
    anchor["exact_old_text"] = "missing text"
    anchor["target_start"] = 0
    anchor["target_end"] = len("missing text")
    anchor["occurrence_id"] = applier.physical_occurrence_id("synthetic_novel", anchor)
    with pytest.raises(applier.NormalizationError, match="old text"):
        _prepare(baseline, plan)


def test_extra_occurrence_aborts_where_count_matters(tmp_path):
    baseline, plan = _plan_fixture(tmp_path, "# Chapter 1\n\nold term and old term.\n")
    anchor = plan["text_decisions"][0]["occurrence_anchors"][0]
    anchor["expected_file_occurrence_count"] = 1
    anchor["expected_paragraph_occurrence_count"] = 1
    with pytest.raises(applier.NormalizationError, match="file occurrence count mismatch"):
        _prepare(baseline, plan)


def test_duplicate_physical_occurrence_ownership_aborts(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    duplicate = copy.deepcopy(plan["text_decisions"][0])
    duplicate["decision_id"] = "text-0002"
    plan["text_decisions"].append(duplicate)
    _recount_summary(plan)
    with pytest.raises(applier.NormalizationError, match="duplicate physical occurrence ownership"):
        _prepare(baseline, plan)


def test_overlapping_spans_abort(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    text = (baseline / "0001.md").read_text()
    second_anchor = _anchor(
        "synthetic_novel",
        plan["corpus"]["verified_final_fingerprint"],
        text,
        old="term",
        replacement="word",
    )
    second = {
        "decision_id": "text-0002",
        "source_identity": "second-source-identity",
        "source_form": "second-source-form",
        "current_rendering": "term",
        "proposed_rendering": "word",
        "status": "APPROVED_CORRECTION",
        "exact_occurrence_count": 1,
        "occurrence_anchors": [second_anchor],
        "replacement_safe_automatically": True,
    }
    second_anchor["confirmed_source_forms"] = ["second-source-form"]
    plan["text_decisions"].append(second)
    _recount_summary(plan)
    with pytest.raises(applier.NormalizationError, match="overlapping physical target spans"):
        _prepare(baseline, plan)


def test_review_required_cannot_execute_even_with_unexpected_extra_fields(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    decision = plan["text_decisions"][0]
    decision["status"] = "REVIEW_REQUIRED"
    decision["replacement_safe_automatically"] = False
    decision["proposed_rendering"] = "candidate only"
    decision["unexpected_candidate"] = "full canonical identity"
    decision["occurrence_anchors"][0]["unexpected_replacement_candidate"] = "candidate only"
    _recount_summary(plan)
    preview = _prepare(baseline, plan)
    assert preview.edits == []
    assert preview.review_record_count == 1
    assert preview.total_occurrence_count == 0


def test_false_positive_no_change_cannot_execute(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    decision = plan["text_decisions"][0]
    decision["status"] = "FALSE_POSITIVE_NO_CHANGE"
    decision["replacement_safe_automatically"] = False
    decision["proposed_rendering"] = "tempting candidate"
    _recount_summary(plan)
    preview = _prepare(baseline, plan)
    assert preview.edits == []
    assert preview.no_change_record_count == 1


def test_nonapproved_explicit_execution_attempt_is_rejected(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    decision = plan["text_decisions"][0]
    decision["status"] = "FALSE_POSITIVE_NO_CHANGE"
    decision["executable_edit"] = True
    _recount_summary(plan)
    with pytest.raises(applier.NormalizationError, match="attempts to execute non-approved"):
        _prepare(baseline, plan)


def test_manual_approved_does_not_execute_without_explicit_authorization(tmp_path):
    baseline, base_plan = _plan_fixture(tmp_path)
    plan = _manual_only_plan(base_plan)
    preview = _prepare(baseline, plan, allow_manual=False)
    assert preview.manual_record_count == 1
    assert preview.manual_occurrence_count == 0
    assert preview.edits == []
    with pytest.raises(applier.NormalizationError, match="remain unresolved"):
        _publish(tmp_path, preview)
    assert not (tmp_path / "normalized_chapters").exists()


def test_manual_approved_executes_only_with_explicit_authorization(tmp_path):
    baseline, base_plan = _plan_fixture(tmp_path)
    plan = _manual_only_plan(base_plan)
    preview = _prepare(baseline, plan, allow_manual=True)
    assert preview.manual_occurrence_count == 1
    assert preview.edits[0].kind == "manual_approved"
    _publish(tmp_path, preview)
    assert "new term" in (tmp_path / "normalized_chapters" / "0001.md").read_text()


def test_short_source_form_is_not_expanded_from_editorial_family_metadata(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    decision = plan["text_decisions"][0]
    decision["status"] = "REVIEW_REQUIRED"
    decision["replacement_safe_automatically"] = False
    decision["source_form_role"] = "short_name"
    decision["editorial_family"] = {
        "canonical_full_identity": "Long Full Name",
        "candidate_renderings": ["Short", "Long Full Name"],
    }
    _recount_summary(plan)
    preview = _prepare(baseline, plan)
    assert preview.edits == []


def test_dry_run_preparation_writes_no_normalized_chapters(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    assert applier.dry_run_summary(preview)["writes_performed"] == 0
    assert not (tmp_path / "normalized_chapters").exists()


def test_transactional_failure_leaves_no_partial_normalized_corpus(tmp_path, monkeypatch):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)

    def fail_verification(*args, **kwargs):
        raise applier.NormalizationError("injected independent verification failure")

    monkeypatch.setattr(applier, "verify_normalized_corpus", fail_verification)
    with pytest.raises(applier.NormalizationError, match="injected"):
        _publish(tmp_path, preview)
    assert not (tmp_path / "normalized_chapters").exists()
    assert list(tmp_path.glob(".normalized_chapters.staging-*")) == []


def test_terminal_normalization_commit_then_error_is_classified_as_success(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    output = tmp_path / "normalized_chapters"
    report_json = tmp_path / "reports" / "normalization_application.json"
    report_md = tmp_path / "reports" / "normalization_application.md"
    real_rename = Path.rename

    def commit_then_error(path, target):
        target = Path(target)
        if target == output:
            real_rename(path, target)
            raise OSError("injected error after terminal normalization rename")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", commit_then_error)

    report = _publish(tmp_path, preview)

    assert report["application_kind"] == "fail_closed_exact_occurrence_normalization"
    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".normalized_chapters.staging-*"))
    assert not report_json.with_name(
        f".{report_json.name}.staging-{os.getpid()}"
    ).exists()
    assert not report_md.with_name(
        f".{report_md.name}.staging-{os.getpid()}"
    ).exists()


def test_ambiguous_terminal_normalization_commit_then_error_is_preserved(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    output = tmp_path / "normalized_chapters"
    report_json = tmp_path / "reports" / "normalization_application.json"
    report_md = tmp_path / "reports" / "normalization_application.md"
    real_rename = Path.rename

    def commit_tamper_then_error(path, target):
        target = Path(target)
        if target == output:
            real_rename(path, target)
            first = sorted(output.iterdir())[0]
            first.write_bytes(first.read_bytes() + b"\nTAMPERED\n")
            raise OSError("injected ambiguous post-commit normalization error")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", commit_tamper_then_error)

    with pytest.raises(
        applier.NormalizationError,
        match="ambiguous terminal normalization commit outcome",
    ):
        _publish(tmp_path, preview)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert b"TAMPERED" in sorted(output.iterdir())[0].read_bytes()
    assert not list(tmp_path.glob(".normalized_chapters.staging-*"))


def _leave_exact_normalization_prefix(tmp_path, preview, monkeypatch):
    output = tmp_path / "normalized_chapters"
    report_json = tmp_path / "reports" / "normalization_application.json"
    report_md = tmp_path / "reports" / "normalization_application.md"
    real_rename = Path.rename

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(path, target):
        if Path(target) == output:
            raise SimulatedHardStop("simulated stop before normalized corpus commit")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop, match="before normalized corpus commit"):
        _publish(tmp_path, preview)
    monkeypatch.setattr(Path, "rename", real_rename)

    stages = list(tmp_path.glob(".normalized_chapters.staging-*"))
    assert len(stages) == 1
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()
    return stages[0], report_json, report_md


def test_exact_interrupted_normalization_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )

    report = _publish(tmp_path, preview)

    assert report["application_kind"] == "fail_closed_exact_occurrence_normalization"
    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()
    assert not list(tmp_path.glob(".normalized_chapters.staging-*"))


def test_valid_different_utc_timestamp_is_non_authoritative_for_recovery(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    _, report_json, _ = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["generated_at_utc"] = "2000-01-01T00:00:00+00:00"
    report_json.write_bytes(applier._canonical_normalization_json_bytes(report))

    _publish(tmp_path, preview)

    assert (tmp_path / "normalized_chapters").is_dir()


def test_invalid_normalization_timestamp_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["generated_at_utc"] = "not-a-utc-timestamp"
    report_json.write_bytes(applier._canonical_normalization_json_bytes(report))

    with pytest.raises(
        applier.NormalizationError,
        match="invalid generated_at_utc",
    ):
        _publish(tmp_path, preview)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not (tmp_path / "normalized_chapters").exists()


def test_divergent_normalization_report_semantics_block_without_cleanup(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["project_id"] = "different-project"
    report_json.write_bytes(applier._canonical_normalization_json_bytes(report))

    with pytest.raises(
        applier.NormalizationError,
        match="semantics do not match reconstruction",
    ):
        _publish(tmp_path, preview)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not (tmp_path / "normalized_chapters").exists()


def test_divergent_normalization_stage_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    (stale_stage / "0001.md").write_bytes(b"divergent")

    with pytest.raises(applier.NormalizationError):
        _publish(tmp_path, preview)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not (tmp_path / "normalized_chapters").exists()


def test_exact_normalization_report_stage_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    report_json = tmp_path / "reports" / "normalization_application.json"
    real_replace = Path.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_json_publish(path, target):
        if Path(target) == report_json:
            raise SimulatedHardStop("simulated stop before normalization JSON publish")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", stop_before_json_publish)
    with pytest.raises(SimulatedHardStop, match="before normalization JSON publish"):
        _publish(tmp_path, preview)
    monkeypatch.setattr(Path, "replace", real_replace)

    assert not report_json.exists()
    assert len(list((tmp_path / "reports").glob(".normalization_application.json.staging-*"))) == 1
    assert len(list((tmp_path / "reports").glob(".normalization_application.md.staging-*"))) == 1
    assert len(list(tmp_path.glob(".normalized_chapters.staging-*"))) == 1

    _publish(tmp_path, preview)

    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert not list((tmp_path / "reports").glob(".normalization_application.json.staging-*"))
    assert not list((tmp_path / "reports").glob(".normalization_application.md.staging-*"))
    assert not list(tmp_path.glob(".normalized_chapters.staging-*"))


def test_markdown_only_legacy_normalization_prefix_is_recoverable(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    _, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    report_json.unlink()

    _publish(tmp_path, preview)

    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()


def test_interrupted_normalization_cleanup_remains_recoverable(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    real_unlink = Path.unlink

    def fail_json_cleanup(path, *args, **kwargs):
        if Path(path) == report_json:
            raise OSError("injected normalization-prefix cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_cleanup)
    with pytest.raises(OSError, match="normalization-prefix cleanup"):
        _publish(tmp_path, preview)

    assert not report_md.exists()
    assert report_json.is_file()
    assert stale_stage.is_dir()
    assert not (tmp_path / "normalized_chapters").exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    _publish(tmp_path, preview)

    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()


def test_partial_normalization_stage_delete_resumes_from_cleanup_journal(
    tmp_path, monkeypatch
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path, preview, monkeypatch
    )
    real_rmtree = stage_cleanup.shutil.rmtree
    failed = {"done": False}

    def partial_then_fail(path):
        path = Path(path)
        if (
            path.name.startswith(".normalization.cleanup-stage-")
            and not failed["done"]
        ):
            failed["done"] = True
            first = sorted(path.iterdir())[0]
            first.unlink()
            raise OSError("injected partial normalization stage cleanup")
        return real_rmtree(path)

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", partial_then_fail)
    with pytest.raises(OSError, match="partial normalization stage cleanup"):
        _publish(tmp_path, preview)

    assert not stale_stage.exists()
    assert not report_json.exists()
    assert not report_md.exists()
    assert not (tmp_path / "normalized_chapters").exists()
    assert len(list(tmp_path.glob(".normalization.cleanup-journal-*.json"))) == 1
    assert len(list(tmp_path.glob(".normalization.cleanup-stage-*"))) == 1

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", real_rmtree)
    _publish(tmp_path, preview)

    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".normalization.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".normalization.cleanup-stage-*"))


def test_partial_prejournal_scratch_recovers_with_exact_normalization_stage(
    tmp_path,
    monkeypatch,
):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    stale_stage, report_json, report_md = _leave_exact_normalization_prefix(
        tmp_path,
        preview,
        monkeypatch,
    )
    prep = tmp_path / (
        ".normalization.cleanup-journal-prep-" + ("e" * 32) + ".tmp"
    )
    prep.write_bytes(b"{partial")

    report = _publish(tmp_path, preview)

    assert report["application_kind"] == "fail_closed_exact_occurrence_normalization"
    assert (tmp_path / "normalized_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()
    assert not prep.exists()
    assert not list(
        tmp_path.glob(".normalization.cleanup-journal-prep-*")
    )
    assert not list(tmp_path.glob(".normalization.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".normalization.cleanup-stage-*"))


def test_dangling_symlink_normalization_artifacts_fail_closed(tmp_path, monkeypatch):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    output = tmp_path / "normalized_chapters"
    report_json = tmp_path / "reports" / "normalization_application.json"
    real_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == report_json else real_is_symlink(path),
    )
    with pytest.raises(
        applier.NormalizationError,
        match="ambiguous interrupted normalization JSON artifact",
    ):
        _publish(tmp_path, preview)
    assert not output.exists()

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == output else real_is_symlink(path),
    )
    with pytest.raises(
        applier.NormalizationError,
        match="normalized output already exists",
    ):
        _publish(tmp_path, preview)


def test_baseline_files_remain_untouched(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    before = (baseline / "0001.md").read_bytes()
    _publish(tmp_path, _prepare(baseline, plan))
    assert (baseline / "0001.md").read_bytes() == before


def test_output_is_copied_from_baseline_and_existing_output_is_not_reused(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    _publish(tmp_path, preview)
    normalized = tmp_path / "normalized_chapters" / "0001.md"
    normalized.write_text("tampered prior normalized output", encoding="utf-8")
    with pytest.raises(applier.NormalizationError, match="already exists"):
        applier.publish_normalized_corpus(
            preview,
            output_dir=tmp_path / "normalized_chapters",
            report_json_path=tmp_path / "different_report.json",
            report_md_path=tmp_path / "different_report.md",
        )
    assert "old term" in (baseline / "0001.md").read_text()


def test_independent_diff_verifier_rejects_unauthorized_change(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    preview = _prepare(baseline, plan)
    _publish(tmp_path, preview)
    normalized = tmp_path / "normalized_chapters" / "0001.md"
    normalized.write_bytes(normalized.read_bytes() + b"unauthorized\n")
    with pytest.raises(applier.NormalizationError, match="unauthorized trailing difference"):
        applier.verify_normalized_corpus(preview, tmp_path / "normalized_chapters")


def test_plan_source_hash_mismatch_aborts(tmp_path):
    _, plan = _plan_fixture(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(applier.NormalizationError, match="source plan SHA-256 mismatch"):
        applier.load_plan_file(plan_path, "b" * 64)


def test_second_dry_run_against_immutable_baseline_resolves_same_edits(tmp_path):
    baseline, plan = _plan_fixture(tmp_path)
    first = _prepare(baseline, plan)
    second = _prepare(baseline, plan)
    assert [edit.occurrence_id for edit in first.edits] == [
        edit.occurrence_id for edit in second.edits
    ]


def test_rogue_plan_resolves_exact_required_prose_set_without_writes():
    root = Path(__file__).resolve().parents[1]
    plan_path = root / "novels" / "rogue_first_prince" / "final" / "reports" / "normalization_plan.json"
    baseline = root / "novels" / "rogue_first_prince" / "final" / "chapters"
    inventory = plan_path.with_name("normalization_inventory.json")
    if not plan_path.exists() or not baseline.exists():
        pytest.skip("Rogue finalization inputs are not available")
    plan, plan_sha256 = applier.load_plan_file(plan_path, ROGUE_PLAN_SHA256)
    preview = applier.prepare_application(
        baseline_dir=baseline,
        plan=plan,
        plan_sha256=plan_sha256,
        project_id="rogue_first_prince",
        allow_manual_approved=True,
        expectations=applier.Expectations(
            baseline_files=351,
            baseline_bytes=5_224_006,
            baseline_sha256=ROGUE_BASELINE_SHA256,
            approved_records=33,
            automatic_occurrences=235,
            manual_occurrences=4,
            total_occurrences=239,
        ),
        plan_path=plan_path,
        inventory_path=inventory,
    )
    assert preview.baseline_fingerprint["aggregate_sha256"] == ROGUE_BASELINE_SHA256
    assert preview.approved_record_count == 33
    assert preview.automatic_occurrence_count == 235
    assert preview.manual_occurrence_count == 4
    assert preview.total_occurrence_count == 239
    assert preview.edit_span_count == 237
    assert preview.review_record_count == 4
    assert preview.no_change_record_count == 26
