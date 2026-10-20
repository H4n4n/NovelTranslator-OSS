import copy
import hashlib
import json
from pathlib import Path

import pytest

import apply_title_resolution_plan as app
import proven_stage_cleanup as stage_cleanup


ROGUE_V13_PLAN_SHA256 = "e31983280254647a4591ad75a6f0bf19c392f0125c6c3e3b134a8351c96b57af"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path, *, heading="Chapter 1. 7. Old Title (1)", body="Body prose.\n", **decision_overrides):
    source = tmp_path / "normalized_chapters"
    source.mkdir()
    data = f"{heading}\n\n{body}".encode()
    (source / "0001.md").write_bytes(data)
    fp = app.corpus_fingerprint(source, 1)
    current = decision_overrides.pop("current", "Old Title")
    status = decision_overrides.pop("decision", "APPROVED_CANONICAL")
    normalized_heading = heading.replace("’", "'")
    title_start = normalized_heading.find(current) if isinstance(current, str) else -1
    anchor = {
        "decision_id": "title-0001", "unit": 1,
        "target_layer": "final/normalized_chapters",
        "baseline_normalized_chapter_file_sha256": _sha(data),
        "exact_current_heading_line": heading,
        "exact_current_heading_line_occurrence_count": 1,
        "expected_heading_structure": {
            "title_kind": "main_story", "source_title_number": 7,
            "side_story_number": None, "canonical_english_title_text": "New Title",
            "part_number": 1,
        },
        "source_title_identity": {
            "normalized_korean_base_identity": "7. 가짜 제목",
            "korean_title_text": "가짜 제목", "source_title_number": 7,
            "side_story_number": None,
        },
        "approved_canonical_english_title_text": "New Title",
        "explicit_part_number": 1, "expected_occurrence_count": 1,
        "title_target_status": "EXISTING_TITLE", "recovery_required": False,
        "recovery_authorized": False, "line_number": 1,
        "line_sha256": _sha(heading.encode()), "exact_title_bearing_line": heading,
        "title_location_classification": "EXISTING_TITLE_PRIMARY_HEADING",
        "title_text_span": {
            "start": title_start, "end": title_start + len(current or ""),
            "exact_text": heading[title_start:title_start + len(current or "")] if title_start >= 0 else current,
        },
        "current_english_title_text": current,
    }
    existing = current is not None and title_start >= 0
    target = {
        "decision_id": "title-0001", "unit": 1,
        "title_target_status": "EXISTING_TITLE" if existing else "TRUE_TITLE_ABSENT",
        "recovery_required": not existing, "recovery_authorized": False,
        "recovery_review_required": not existing,
        "automatic_application_forbidden": not existing,
    }
    decision = {
        "decision_id": "title-0001", "decision": status,
        "normalized_korean_base_title": "7. 가짜 제목", "korean_title_text": "가짜 제목",
        "title_kind": "main_story", "source_title_number": 7, "side_story_number": None,
        "canonical_english_title_text": "New Title" if status != "REVIEW_REQUIRED" else None,
        "units": [1], "unit_parts": [{"final_unit": 1, "part_number": 1,
                                         "current_english_title_text": current,
                                         "raw_heading": heading}],
        "automatic_application_safe": True, "automatic_application_forbidden": False,
        "missing_title_recovery_supported": False,
        "physical_title_targets": [target],
        "application_anchors": [anchor] if status == "APPROVED_CANONICAL" and existing else [],
        "recovery_insertion_anchors": [],
    }
    decision.update(decision_overrides)
    counts = {name: int(name == status) for name in (
        "APPROVED_CANONICAL", "REVIEW_REQUIRED", "ALREADY_CONSISTENT", "STRUCTURAL_ONLY"
    )}
    plan = {
        "schema_version": 2, "plan_revision": "title-resolution-v1.2",
        "corpora": {"normalized_title_base": fp}, "title_decisions": [decision],
        "final_title_counts": counts,
        "approved_title_target_count": int(status == "APPROVED_CANONICAL"),
        "existing_title_executable_anchor_count": len(decision["application_anchors"]),
        "authorized_recovery_insertion_anchor_count": 0,
        "executable_title_target_count": len(decision["application_anchors"]),
    }
    return source, plan


def _prepare(source, plan, *, manual=True):
    return app.prepare_application(source, plan, "a" * 64, "synthetic", manual)


def _authorize_recovery(source, plan, inserted_title="Chapter 1. 7. New Title (1)"):
    decision = plan["title_decisions"][0]
    target = decision["physical_title_targets"][0]
    target.update({
        "title_target_status": "AUTHORIZED_RECOVERY_INSERTION",
        "recovery_authorized": True,
        "recovery_authorization": "HUMAN_AUTHORIZED",
        "recovery_review_required": False,
        "automatic_application_forbidden": False,
    })
    identity = {
        "normalized_korean_base_identity": decision["normalized_korean_base_title"],
        "korean_title_text": decision["korean_title_text"],
        "source_title_number": decision["source_title_number"],
        "side_story_number": decision["side_story_number"],
    }
    decision["recovery_insertion_anchors"] = [{
        "decision_id": decision["decision_id"], "unit": 1,
        "target_layer": "final/normalized_chapters",
        "anchor_type": "HUMAN_AUTHORIZED_RECOVERY_INSERTION",
        "baseline_normalized_chapter_file_sha256": app.sha256_bytes((source / "0001.md").read_bytes()),
        "expected_original_first_line": "Chapter 1", "expected_original_first_line_number": 1,
        "inserted_title_line": inserted_title, "insertion_point": "LOGICAL_TEXT_START",
        "blank_line_count_after_title": 1, "expected_input_bom": False,
        "expected_newline_convention": "LF", "preserve_bom": True,
        "preserve_original_newline_convention": True, "immutable_original_content_suffix": True,
        "source_title_identity": identity, "explicit_part_number": 1,
        "approved_canonical_english_title_text": "New Title",
        "title_target_status": "AUTHORIZED_RECOVERY_INSERTION", "recovery_required": True,
        "recovery_authorized": True, "recovery_authorization": "HUMAN_AUTHORIZED",
        "automatic_application_forbidden": False,
    }]
    plan["authorized_recovery_insertion_anchor_count"] = 1
    plan["executable_title_target_count"] = 1


def _publish(tmp_path, preview):
    return app.publish_titled_corpus(
        preview, tmp_path / "titled_chapters", tmp_path / "application.json", tmp_path / "application.md"
    )


def test_approved_automatic_title_applies(tmp_path):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    assert preview.automatic_anchor_count == 1
    _publish(tmp_path, preview)
    assert (tmp_path / "titled_chapters/0001.md").read_text().startswith("Chapter 1. 7. New Title (1)")


def test_manual_approved_blocked_without_authorization(tmp_path):
    source, plan = _fixture(tmp_path, automatic_application_safe=False)
    preview = _prepare(source, plan, manual=False)
    assert preview.blocked_manual_anchor_count == 1 and not preview.edits
    with pytest.raises(app.TitleApplicationError, match="manual anchors remain blocked"):
        _publish(tmp_path, preview)


def test_manual_approved_applies_with_authorization(tmp_path):
    source, plan = _fixture(tmp_path, automatic_application_safe=False)
    preview = _prepare(source, plan, manual=True)
    assert preview.manual_anchor_count == 1 and len(preview.edits) == 1


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "STRUCTURAL_ONLY", "ALREADY_CONSISTENT"])
def test_nonapproved_status_never_applies(tmp_path, status):
    source, plan = _fixture(tmp_path, decision=status)
    preview = _prepare(source, plan)
    assert not preview.edits and not preview.authorized_anchors


def test_review_required_with_malformed_anchor_is_rejected(tmp_path):
    source, plan = _fixture(tmp_path, decision="REVIEW_REQUIRED")
    plan["title_decisions"][0]["application_anchors"] = [{"candidate": "New Title"}]
    with pytest.raises(app.TitleApplicationError, match="non-approved decision"):
        _prepare(source, plan)


def test_wrong_normalized_file_sha_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["title_decisions"][0]["application_anchors"][0]["baseline_normalized_chapter_file_sha256"] = "0" * 64
    with pytest.raises(app.TitleApplicationError, match="input file SHA-256"):
        _prepare(source, plan)


def test_wrong_aggregate_corpus_sha_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["corpora"]["normalized_title_base"]["aggregate_sha256"] = "0" * 64
    with pytest.raises(app.TitleApplicationError, match="aggregate_sha256"):
        _prepare(source, plan)


def test_wrong_exact_current_heading_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["title_decisions"][0]["application_anchors"][0]["exact_current_heading_line"] = "Chapter 1 wrong"
    with pytest.raises(app.TitleApplicationError, match="title-bearing line mismatch"):
        _prepare(source, plan)


def test_duplicate_executable_anchor_ownership_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    duplicate = copy.deepcopy(plan["title_decisions"][0])
    duplicate["decision_id"] = "title-0002"
    duplicate["application_anchors"][0]["decision_id"] = "title-0002"
    plan["title_decisions"].append(duplicate)
    plan["final_title_counts"]["APPROVED_CANONICAL"] = 2
    plan["approved_title_target_count"] = 2
    plan["existing_title_executable_anchor_count"] = 2
    plan["executable_title_target_count"] = 2
    with pytest.raises(app.TitleApplicationError, match="duplicate executable anchor ownership"):
        _prepare(source, plan)


def test_conflicting_edit_line_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    preview.edits.append(copy.copy(preview.edits[0]))
    with pytest.raises(app.TitleApplicationError, match="conflicting authorized line edits"):
        app.verify_titled_corpus(preview, source)


def test_part_number_mismatch_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["title_decisions"][0]["application_anchors"][0]["explicit_part_number"] = 2
    with pytest.raises(app.TitleApplicationError, match="anchor part mismatch"):
        _prepare(source, plan)


def test_source_identity_mismatch_aborts(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["title_decisions"][0]["application_anchors"][0]["source_title_identity"]["korean_title_text"] = "다른 제목"
    with pytest.raises(app.TitleApplicationError, match="source title identity mismatch"):
        _prepare(source, plan)


def test_number_discontinuity_cannot_cross_identity(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["title_decisions"][0]["korean_title_text"] = "다른 제목"
    with pytest.raises(app.TitleApplicationError, match="source title identity mismatch"):
        _prepare(source, plan)


def test_duplicate_explicit_parts_are_preserved(tmp_path):
    source, plan = _fixture(
        tmp_path,
        heading="Chapter 1. 7. Old Title (3)",
    )
    decision = plan["title_decisions"][0]
    decision["units"] = [1]
    decision["unit_parts"] = [{"final_unit": 1, "part_number": 3, "current_english_title_text": "Old Title"}]
    anchor = decision["application_anchors"][0]
    anchor["explicit_part_number"] = 3
    anchor["expected_heading_structure"]["part_number"] = 3
    preview = _prepare(source, plan)
    assert preview.edits[0].explicit_part == 3


def test_missing_title_target_requires_separate_recovery_review(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1", current=None)
    preview = _prepare(source, plan)
    assert [item["unit"] for item in preview.recovery_review_targets] == [1]
    assert preview.edits == []
    with pytest.raises(app.TitleApplicationError, match="recovery-review-required"):
        _publish(tmp_path, preview)


def test_human_authorized_recovery_prepends_exact_region_and_preserves_suffix(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1", current=None)
    original = (source / "0001.md").read_bytes()
    _authorize_recovery(source, plan)
    preview = _prepare(source, plan)
    summary = app.dry_run_summary(preview)
    assert summary["missing_title_recovery_count"] == 1
    assert summary["blocker_count"] == 0
    report = _publish(tmp_path, preview)
    output = (tmp_path / "titled_chapters/0001.md").read_bytes()
    assert output == b"Chapter 1. 7. New Title (1)\n\n" + original
    assert output.endswith(original)
    assert report["independent_verification"]["recovery_suffix_verified_units"] == [1]


def test_authorized_recovery_fails_closed_on_first_line_mismatch(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1", current=None)
    _authorize_recovery(source, plan)
    plan["title_decisions"][0]["recovery_insertion_anchors"][0]["expected_original_first_line"] = "Wrong"
    with pytest.raises(app.TitleApplicationError, match="original first line"):
        _prepare(source, plan)


def test_decision_level_missing_recovery_flag_cannot_authorize_target(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1<br>", current=None,
                            missing_title_recovery_supported=True, automatic_application_safe=False)
    preview = _prepare(source, plan)
    assert preview.recovery_review_targets[0]["recovery_authorized"] is False
    assert preview.edits == []


def test_terminal_br_is_not_title_text_or_automatic_recovery(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1<br>", current=None,
                            missing_title_recovery_supported=True)
    preview = _prepare(source, plan)
    assert preview.edits == []
    assert (source / "0001.md").read_text().startswith("Chapter 1<br>")


def test_prose_line_never_becomes_executable_recovery_anchor(tmp_path):
    source, plan = _fixture(tmp_path, heading="This is prose.", current=None,
                            missing_title_recovery_supported=True)
    preview = _prepare(source, plan)
    assert preview.authorized_anchors == []
    assert preview.recovery_review_targets[0]["title_target_status"] == "TRUE_TITLE_ABSENT"


def test_ordinary_replacement_preserves_heading_structure(tmp_path):
    source, plan = _fixture(tmp_path, heading="# Chapter 1 — < 7. Old Title (1) >")
    edit = _prepare(source, plan).edits[0]
    assert edit.new_line == "# Chapter 1 — < 7. New Title (1) >\n"


def test_canonical_title_is_approved_noop(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1. 7. New Title (1)", current="New Title")
    preview = _prepare(source, plan)
    assert len(preview.noops) == 1 and not preview.edits


def test_curly_apostrophe_semantic_noop_preserves_bytes(tmp_path):
    source, plan = _fixture(tmp_path, heading="Chapter 1. 7. Fisherman’s Gain (1)", current="Fisherman's Gain")
    decision = plan["title_decisions"][0]
    decision["canonical_english_title_text"] = "Fisherman's Gain"
    decision["application_anchors"][0]["approved_canonical_english_title_text"] = "Fisherman's Gain"
    decision["application_anchors"][0]["expected_heading_structure"]["canonical_english_title_text"] = "Fisherman's Gain"
    assert not _prepare(source, plan).edits


def test_dry_run_writes_no_corpus(tmp_path):
    source, plan = _fixture(tmp_path)
    summary = app.dry_run_summary(_prepare(source, plan))
    assert summary["writes_performed"] == 0 and not (tmp_path / "titled_chapters").exists()


def test_failure_before_publication_leaves_no_partial_corpus(tmp_path, monkeypatch):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    monkeypatch.setattr(app, "verify_titled_corpus", lambda *a, **k: (_ for _ in ()).throw(app.TitleApplicationError("injected")))
    with pytest.raises(app.TitleApplicationError, match="injected"):
        _publish(tmp_path, preview)
    assert not (tmp_path / "titled_chapters").exists()
    assert not list(tmp_path.glob(".titled_chapters.staging-*"))


def test_terminal_title_commit_then_error_is_classified_as_success(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    output = tmp_path / "titled_chapters"
    report_json = tmp_path / "application.json"
    report_md = tmp_path / "application.md"
    real_replace = app.os.replace

    def commit_then_error(source_path, destination):
        destination = Path(destination)
        if destination == output:
            real_replace(source_path, destination)
            raise OSError("injected error after terminal title rename")
        return real_replace(source_path, destination)

    monkeypatch.setattr(app.os, "replace", commit_then_error)

    report = _publish(tmp_path, preview)

    assert report["application_revision"] == "title-application-v1"
    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".titled_chapters.staging-*"))
    assert not list(tmp_path.glob(".application.json.*"))
    assert not list(tmp_path.glob(".application.md.*"))


def test_ambiguous_terminal_title_commit_then_error_is_preserved(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    output = tmp_path / "titled_chapters"
    report_json = tmp_path / "application.json"
    report_md = tmp_path / "application.md"
    real_replace = app.os.replace

    def commit_tamper_then_error(source_path, destination):
        destination = Path(destination)
        if destination == output:
            real_replace(source_path, destination)
            first = sorted(output.iterdir())[0]
            first.write_bytes(first.read_bytes() + b"\nTAMPERED\n")
            raise OSError("injected ambiguous post-commit title error")
        return real_replace(source_path, destination)

    monkeypatch.setattr(app.os, "replace", commit_tamper_then_error)

    with pytest.raises(
        app.TitleApplicationError,
        match="ambiguous terminal title commit outcome",
    ):
        _publish(tmp_path, preview)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert b"TAMPERED" in sorted(output.iterdir())[0].read_bytes()
    assert not list(tmp_path.glob(".titled_chapters.staging-*"))


def _leave_exact_title_prefix(tmp_path, preview, monkeypatch):
    output = tmp_path / "titled_chapters"
    report_json = tmp_path / "application.json"
    report_md = tmp_path / "application.md"
    real_replace = app.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        if Path(destination) == output:
            raise SimulatedHardStop("simulated stop before titled corpus commit")
        return real_replace(source, destination)

    monkeypatch.setattr(app.os, "replace", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop, match="before titled corpus commit"):
        _publish(tmp_path, preview)
    monkeypatch.setattr(app.os, "replace", real_replace)

    stages = list(tmp_path.glob(".titled_chapters.staging-*"))
    assert len(stages) == 1
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()
    return stages[0], report_json, report_md


def test_exact_interrupted_title_prefix_recovers_on_retry(tmp_path, monkeypatch):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    stale_stage, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )

    report = _publish(tmp_path, preview)

    assert report["application_revision"] == "title-application-v1"
    assert (tmp_path / "titled_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()
    assert not list(tmp_path.glob(".titled_chapters.staging-*"))


def test_exact_title_report_temp_prefix_recovers_on_retry(tmp_path, monkeypatch):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    report_json = tmp_path / "application.json"
    real_replace = app.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_json_publish(source_path, destination):
        if Path(destination) == report_json:
            raise SimulatedHardStop("simulated stop before JSON report publish")
        return real_replace(source_path, destination)

    monkeypatch.setattr(app.os, "replace", stop_before_json_publish)
    with pytest.raises(SimulatedHardStop, match="before JSON report publish"):
        _publish(tmp_path, preview)
    monkeypatch.setattr(app.os, "replace", real_replace)

    assert not report_json.exists()
    assert len(list(tmp_path.glob(".application.json.*"))) == 1
    assert len(list(tmp_path.glob(".titled_chapters.staging-*"))) == 1

    _publish(tmp_path, preview)

    assert (tmp_path / "titled_chapters").is_dir()
    assert report_json.is_file()
    assert not list(tmp_path.glob(".application.json.*"))
    assert not list(tmp_path.glob(".titled_chapters.staging-*"))


def test_divergent_interrupted_title_report_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    stale_stage, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )
    original_md = report_md.read_bytes()
    report_json.write_bytes(report_json.read_bytes() + b" ")

    with pytest.raises(
        app.TitleApplicationError,
        match="does not match reconstructed bytes",
    ):
        _publish(tmp_path, preview)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.read_bytes() == original_md
    assert not (tmp_path / "titled_chapters").exists()


def test_divergent_interrupted_title_stage_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    stale_stage, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )
    (stale_stage / "0001.md").write_bytes(b"divergent")

    with pytest.raises(app.TitleApplicationError):
        _publish(tmp_path, preview)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not (tmp_path / "titled_chapters").exists()


def test_markdown_only_legacy_title_prefix_is_recoverable(tmp_path, monkeypatch):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    _, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )
    report_json.unlink()

    _publish(tmp_path, preview)

    assert (tmp_path / "titled_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()


def test_interrupted_title_prefix_cleanup_remains_recoverable(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    stale_stage, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )
    real_unlink = Path.unlink

    def fail_json_cleanup(path, *args, **kwargs):
        if Path(path) == report_json:
            raise OSError("injected title prefix cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_cleanup)
    with pytest.raises(OSError, match="title prefix cleanup"):
        _publish(tmp_path, preview)

    assert not report_md.exists()
    assert report_json.is_file()
    assert stale_stage.is_dir()
    assert not (tmp_path / "titled_chapters").exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    _publish(tmp_path, preview)

    assert (tmp_path / "titled_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()


def test_partial_title_stage_delete_resumes_from_cleanup_journal(
    tmp_path, monkeypatch
):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    stale_stage, report_json, report_md = _leave_exact_title_prefix(
        tmp_path, preview, monkeypatch
    )
    real_rmtree = stage_cleanup.shutil.rmtree
    failed = {"done": False}

    def partial_then_fail(path):
        path = Path(path)
        if (
            path.name.startswith(".title-application.cleanup-stage-")
            and not failed["done"]
        ):
            failed["done"] = True
            first = sorted(path.iterdir())[0]
            first.unlink()
            raise OSError("injected partial title stage cleanup")
        return real_rmtree(path)

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", partial_then_fail)
    with pytest.raises(OSError, match="partial title stage cleanup"):
        _publish(tmp_path, preview)

    assert not stale_stage.exists()
    assert not report_json.exists()
    assert not report_md.exists()
    assert not (tmp_path / "titled_chapters").exists()
    assert len(
        list(tmp_path.glob(".title-application.cleanup-journal-*.json"))
    ) == 1
    assert len(list(tmp_path.glob(".title-application.cleanup-stage-*"))) == 1

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", real_rmtree)
    _publish(tmp_path, preview)

    assert (tmp_path / "titled_chapters").is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".title-application.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".title-application.cleanup-stage-*"))


def test_dangling_symlink_title_artifacts_fail_closed(tmp_path, monkeypatch):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    output = tmp_path / "titled_chapters"
    report_json = tmp_path / "application.json"
    real_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == report_json else real_is_symlink(path),
    )
    with pytest.raises(
        app.TitleApplicationError,
        match="ambiguous interrupted title JSON artifact",
    ):
        _publish(tmp_path, preview)
    assert not output.exists()

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == output else real_is_symlink(path),
    )
    with pytest.raises(
        app.TitleApplicationError,
        match="refusing to overwrite existing output",
    ):
        _publish(tmp_path, preview)


def test_normalized_input_remains_untouched(tmp_path):
    source, plan = _fixture(tmp_path)
    before = (source / "0001.md").read_bytes()
    _publish(tmp_path, _prepare(source, plan))
    assert (source / "0001.md").read_bytes() == before


def test_existing_title_application_preserves_crlf_bytes(tmp_path):
    source, plan = _fixture(tmp_path)
    path = source / "0001.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    fp = app.corpus_fingerprint(source, 1)
    plan["corpora"]["normalized_title_base"] = fp
    plan["title_decisions"][0]["application_anchors"][0]["baseline_normalized_chapter_file_sha256"] = fp["files"][0]["sha256"]
    preview = _prepare(source, plan)
    _publish(tmp_path, preview)
    output = (tmp_path / "titled_chapters/0001.md").read_bytes()
    assert b"\r\n\r\n" in output
    assert b"\n" not in output.replace(b"\r\n", b"")


def test_output_is_copied_from_normalized_not_historical(tmp_path):
    source, plan = _fixture(tmp_path, body="Normalized body.\n")
    historical = tmp_path / "chapters"
    historical.mkdir()
    (historical / "0001.md").write_text("Historical body")
    _publish(tmp_path, _prepare(source, plan))
    assert "Normalized body." in (tmp_path / "titled_chapters/0001.md").read_text()


def test_independent_verifier_detects_prose_mutation(tmp_path):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    _publish(tmp_path, preview)
    target = tmp_path / "titled_chapters/0001.md"
    target.write_text(target.read_text().replace("Body prose.", "Changed prose."))
    with pytest.raises(app.TitleApplicationError, match="authorized title-line differences"):
        app.verify_titled_corpus(preview, tmp_path / "titled_chapters")


def test_independent_verifier_detects_whitespace_mutation(tmp_path):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    _publish(tmp_path, preview)
    target = tmp_path / "titled_chapters/0001.md"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(app.TitleApplicationError):
        app.verify_titled_corpus(preview, tmp_path / "titled_chapters")


def test_plan_hash_mismatch_detected(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{}")
    with pytest.raises(app.TitleApplicationError, match="title plan SHA-256 mismatch"):
        app.load_plan_file(path, "0" * 64)


def test_verify_only_detects_report_plan_hash_mismatch(tmp_path):
    source, plan = _fixture(tmp_path)
    preview = _prepare(source, plan)
    _publish(tmp_path, preview)
    report_path = tmp_path / "application.json"
    report = json.loads(report_path.read_text())
    report["title_plan_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report))
    with pytest.raises(app.TitleApplicationError, match="report title plan"):
        app.verify_existing_application(preview, tmp_path / "titled_chapters", report_path)


def test_non_rogue_korean_identity_is_generic(tmp_path):
    source, plan = _fixture(tmp_path)
    decision = plan["title_decisions"][0]
    decision["normalized_korean_base_title"] = "7. 새벽의 검"
    decision["korean_title_text"] = "새벽의 검"
    identity = decision["application_anchors"][0]["source_title_identity"]
    identity["normalized_korean_base_identity"] = "7. 새벽의 검"
    identity["korean_title_text"] = "새벽의 검"
    assert len(_prepare(source, plan).edits) == 1


def test_non_rogue_chinese_identity_is_generic(tmp_path):
    source, plan = _fixture(tmp_path)
    decision = plan["title_decisions"][0]
    decision["normalized_korean_base_title"] = "7. 黎明之剑"
    decision["korean_title_text"] = "黎明之剑"
    identity = decision["application_anchors"][0]["source_title_identity"]
    identity["normalized_korean_base_identity"] = "7. 黎明之剑"
    identity["korean_title_text"] = "黎明之剑"
    assert len(_prepare(source, plan).edits) == 1


def test_rogue_v13_dry_run_validates_all_existing_and_recovery_anchors():
    root = Path(__file__).resolve().parents[1]
    final = root / "novels" / "rogue_first_prince" / "final"

    normalized = final / "normalized_chapters"
    titled = final / "titled_chapters"

    if not normalized.is_dir():
        pytest.skip("real Rogue normalized corpus is not present")

    titled_before = (
        app.corpus_fingerprint(titled, 351)
        if titled.exists()
        else None
    )
    plan_path = final / "reports" / "title_resolution_plan.json"
    plan, plan_sha = app.load_plan_file(
        plan_path,
        ROGUE_V13_PLAN_SHA256,
    )

    preview = app.prepare_application(
        normalized,
        plan,
        plan_sha,
        "rogue_first_prince",
        allow_manual_approved=True,
        plan_path=plan_path,
    )

    summary = app.dry_run_summary(preview)

    assert summary["approved_target_count"] == 264
    assert summary["approved_anchor_count"] == 264
    assert summary["existing_title_anchor_count"] == 261
    assert summary["automatic_approved_anchor_count"] == 120
    assert summary["manual_approved_anchor_count"] == 141
    assert summary["approved_noop_anchor_count"] == 151
    assert summary["approved_changed_anchor_count"] == 113
    assert summary["existing_title_changed_anchor_count"] == 110
    assert summary["missing_title_recovery_count"] == 3
    assert summary["recovery_review_required_units"] == []
    assert summary["ready_for_application"] is True
    assert summary["writes_performed"] == 0
    assert 334 not in summary["modified_units"]

    decision = next(
        item
        for item in plan["title_decisions"]
        if item["decision_id"] == "title-0087"
    )

    anchor = next(
        item
        for item in decision["application_anchors"]
        if item["unit"] == 334
    )

    assert anchor["current_english_title_text"] == "Epilogue"
    assert anchor["explicit_part_number"] == 2
    assert anchor["title_text_span"] == {
        "start": 0,
        "end": len("Epilogue"),
        "exact_text": "Epilogue",
    }

    if titled_before is None:
        assert not titled.exists()
    else:
        assert app.corpus_fingerprint(
            titled,
            351,
        ) == titled_before


def test_ordinary_replacement_rejects_span_that_consumes_explicit_part():
    anchor = {
        "title_text_span": {
            "start": 0,
            "end": len("Epilogue 2"),
            "exact_text": "Epilogue 2",
        },
        "explicit_part_number": 2,
    }

    with pytest.raises(
        app.TitleApplicationError,
        match="source-explicit part marker",
    ):
        app._ordinary_new_line(
            "Epilogue 2\n",
            anchor,
            "Epilogue",
        )

def test_v13_plan_revision_is_accepted(tmp_path):
    source, plan = _fixture(tmp_path)
    plan["plan_revision"] = "title-resolution-v1.3"

    preview = _prepare(source, plan)

    assert len(preview.edits) == 1
