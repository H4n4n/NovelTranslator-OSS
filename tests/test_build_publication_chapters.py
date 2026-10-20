import json
import os
from pathlib import Path

import pytest

import build_publication_chapters as publication
import proven_stage_cleanup as stage_cleanup
from build_publication_chapters import (
    DEFAULT_SOURCE_DIR,
    PublicationCleanupError,
    build_plan,
    build_report,
    publish_plan,
    transform_unit,
)


def unit(data: bytes, number: int = 1):
    return transform_unit(number, Path(f"{number:04d}.md"), data)


def test_markdown_heading_removal_preserves_crlf_and_body():
    source = b"# Chapter 1: Exact Title\r\n\r\nBODY\r\n"
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1: Exact Title\r\n\r\nBODY\r\n"
    assert planned.transformations == ("markdown_heading_wrapper",)


def test_whole_title_markdown_bold_wrapper_removal():
    source = b"**Chapter 1 \xe2\x80\x94 Exact Title**\r\n\r\nBODY\r\n"
    assert unit(source).output_bytes == b"Chapter 1 \xe2\x80\x94 Exact Title\r\n\r\nBODY\r\n"


@pytest.mark.parametrize(
    ("opening", "closing"),
    [(b'"', b'"'), ("“".encode(), "”".encode())],
)
def test_whole_title_quote_wrapper_removal(opening, closing):
    source = opening + b"Side Story 1. Exact Title" + closing + b"\n\nBODY\n"
    assert unit(source).output_bytes == b"Side Story 1. Exact Title\n\nBODY\n"


def test_inline_title_angle_wrapper_removal():
    source = b"Chapter 1. < Exact Title (2) >\r\n\r\nBODY\r\n"
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1. Exact Title (2)\r\n\r\nBODY\r\n"
    assert planned.part_markers == ("(2)",)


def test_body_angle_line_is_byte_identical():
    body = b"First body line\r\n\r\n< BODY ANGLE LINE >\r\n"
    source = b"Chapter 1. < Exact Title >\r\n\r\n" + body
    planned = unit(source)
    assert planned.output_bytes[planned.output_body_offset :] == body
    assert b"< BODY ANGLE LINE >" in planned.output_bytes


def test_html_break_scaffold_and_angle_wrapper():
    source = b"Chapter 1<br>\r\n< 32. Exact Title (2) >\r\n\r\nBODY\r\n"
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1\r\n32. Exact Title (2)\r\n\r\nBODY\r\n"
    assert planned.transformations == ("html_break_scaffold", "angle_title_wrapper")


def test_generator_wrapper_removal_preserves_actual_title_line():
    source = (
        b"#!/GEN_METHOD_TITLE_START\r\n\r\n"
        b"Chapter 1: Exact Title (2)\r\n\r\n"
        b"#!/GEN_METHOD_TITLE_END\r\n\r\nBODY\r\n"
    )
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1: Exact Title (2)\r\n\r\nBODY\r\n"
    assert planned.transformations == ("generator_title_wrapper",)


def test_split_chapter_title_scaffold_collapses_only_redundant_blank():
    source = b"Chapter 1\r\n\r\n1. Existing Title\r\n\r\nBODY\r\n"
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1\r\n1. Existing Title\r\n\r\nBODY\r\n"
    assert planned.transformations == ("split_scaffold_spacing",)


def test_split_scaffold_accepts_angle_wrapper_as_structural_title_evidence():
    source = b"Chapter 1\n< Existing Title >\n\nBODY\n"
    planned = unit(source)
    assert planned.output_bytes == b"Chapter 1\nExisting Title\n\nBODY\n"


def test_already_clean_title_is_noop():
    source = b"Chapter 1: Existing Title\n\nBODY\n"
    planned = unit(source)
    assert planned.output_bytes == source
    assert planned.transformations == ()


def test_epilogue_2_part_preservation():
    source = b"Epilogue 2\r\n\r\nBODY\r\n"
    planned = transform_unit(334, Path("0334.md"), source)
    assert planned.output_bytes == source
    assert planned.part_markers == ("Epilogue 2",)


@pytest.mark.parametrize(
    "source",
    [
        b"Unexpected Heading\n\nBODY\n",
        b"Chapter 1\n\nThis is body prose without a title separator.\n",
        b"#!/GEN_METHOD_TITLE_START\nChapter 1: Title\n#!/GEN_METHOD_TITLE_END\n\nBODY\n",
        b"Chapter 2: Wrong Number\n\nBODY\n",
    ],
)
def test_malformed_or_unexpected_structures_fail_closed(source):
    with pytest.raises(PublicationCleanupError):
        unit(source)


def _fixture_corpus(root: Path) -> Path:
    source = root / "titled_chapters"
    source.mkdir()
    (source / "0001.md").write_bytes(b"# Chapter 1: Exact Title\r\n\r\nBODY\r\n")
    return source


@pytest.mark.parametrize("existing_target", ["output", "json", "markdown"])
def test_existing_publication_target_refusal(tmp_path, existing_target):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    if existing_target == "output":
        output.mkdir()
        expected_error = "refusing to overwrite"
    elif existing_target == "json":
        report_json.parent.mkdir()
        report_json.write_text("existing", encoding="utf-8")
        expected_error = "does not match reconstructed bytes"
    else:
        report_md.parent.mkdir()
        report_md.write_text("existing", encoding="utf-8")
        expected_error = "does not match reconstructed bytes"
    with pytest.raises(PublicationCleanupError, match=expected_error):
        publish_plan(plan, output, report_json, report_md)


def test_publish_writes_deterministic_reports_and_verified_output(tmp_path):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    report = publish_plan(plan, output, report_json, report_md)
    assert (output / "0001.md").read_bytes() == b"Chapter 1: Exact Title\r\n\r\nBODY\r\n"
    assert json.loads(report_json.read_text(encoding="utf-8")) == report
    assert report["verification"]["body_preservation"]["passed"] is True
    assert report_md.read_text(encoding="utf-8").startswith("# Publication Cleanup Report\n")


def test_terminal_publication_commit_then_error_is_classified_as_success(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    real_rename = Path.rename

    def commit_then_error(path, target):
        target = Path(target)
        if target == output:
            real_rename(path, target)
            raise OSError("injected error after terminal publication rename")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", commit_then_error)

    report = publish_plan(plan, output, report_json, report_md)

    assert report["success"] is True
    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".publication_chapters.staging-*"))


def test_ambiguous_terminal_publication_commit_then_error_is_preserved(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    real_rename = Path.rename

    def commit_tamper_then_error(path, target):
        target = Path(target)
        if target == output:
            real_rename(path, target)
            first = sorted(output.iterdir())[0]
            first.write_bytes(first.read_bytes() + b"\nTAMPERED\n")
            raise OSError("injected ambiguous post-commit publication error")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", commit_tamper_then_error)

    with pytest.raises(
        PublicationCleanupError,
        match="ambiguous terminal publication commit outcome",
    ):
        publish_plan(plan, output, report_json, report_md)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert b"TAMPERED" in sorted(output.iterdir())[0].read_bytes()
    assert not list(tmp_path.glob(".publication_chapters.staging-*"))


def _leave_exact_publication_chapter_prefix(
    tmp_path, plan, output, report_json, report_md, monkeypatch
):
    real_rename = Path.rename

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(path, target):
        if Path(target) == output:
            raise SimulatedHardStop("simulated stop before publication corpus commit")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop, match="before publication corpus commit"):
        publish_plan(plan, output, report_json, report_md)
    monkeypatch.setattr(Path, "rename", real_rename)

    stages = list(tmp_path.glob(".publication_chapters.staging-*"))
    assert len(stages) == 1
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()
    return stages[0]


def test_exact_interrupted_publication_chapter_prefix_recovers(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )

    report = publish_plan(plan, output, report_json, report_md)

    assert report["success"] is True
    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()
    assert not list(tmp_path.glob(".publication_chapters.staging-*"))


def test_cli_apply_recovers_exact_interrupted_publication_prefix(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )

    monkeypatch.setattr(publication, "ROOT", tmp_path)
    monkeypatch.setattr(publication, "DEFAULT_FINAL_DIR", tmp_path / "final")
    monkeypatch.setattr(publication, "DEFAULT_SOURCE_DIR", source)
    monkeypatch.setattr(publication, "DEFAULT_OUTPUT_DIR", output)
    monkeypatch.setattr(publication, "DEFAULT_REPORT_JSON", report_json)
    monkeypatch.setattr(publication, "DEFAULT_REPORT_MD", report_md)
    monkeypatch.setattr(publication, "build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        publication,
        "production_run_lock",
        lambda *args, **kwargs: publication.nullcontext(),
    )

    assert publication.main(["--apply"]) == 0
    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".publication_chapters.staging-*"))


def test_exact_stale_publication_chapter_stage_recovers(tmp_path):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = tmp_path / ".publication_chapters.staging-stale"
    stale_stage.mkdir()
    for item in plan.units:
        (stale_stage / item.source_path.name).write_bytes(item.output_bytes)

    publish_plan(plan, output, report_json, report_md)

    assert output.is_dir()
    assert not stale_stage.exists()


def test_divergent_publication_chapter_report_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )
    original_md = report_md.read_bytes()
    report_json.write_bytes(report_json.read_bytes() + b" ")

    with pytest.raises(
        PublicationCleanupError,
        match="does not match reconstructed bytes",
    ):
        publish_plan(plan, output, report_json, report_md)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.read_bytes() == original_md
    assert not output.exists()


def test_divergent_publication_chapter_stage_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )
    (stale_stage / "0001.md").write_bytes(b"divergent")

    with pytest.raises(PublicationCleanupError):
        publish_plan(plan, output, report_json, report_md)

    assert stale_stage.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()


def test_markdown_only_legacy_publication_prefix_is_recoverable(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )
    report_json.unlink()

    publish_plan(plan, output, report_json, report_md)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()


def test_interrupted_publication_chapter_cleanup_remains_recoverable(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )
    real_unlink = Path.unlink

    def fail_json_cleanup(path, *args, **kwargs):
        if Path(path) == report_json:
            raise OSError("injected publication-prefix cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_cleanup)
    with pytest.raises(OSError, match="publication-prefix cleanup"):
        publish_plan(plan, output, report_json, report_md)

    assert not report_md.exists()
    assert report_json.is_file()
    assert stale_stage.is_dir()
    assert not output.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    publish_plan(plan, output, report_json, report_md)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_stage.exists()


def test_partial_publication_stage_delete_resumes_from_cleanup_journal(
    tmp_path, monkeypatch
):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    stale_stage = _leave_exact_publication_chapter_prefix(
        tmp_path, plan, output, report_json, report_md, monkeypatch
    )
    real_rmtree = stage_cleanup.shutil.rmtree
    failed = {"done": False}

    def partial_then_fail(path):
        path = Path(path)
        if (
            path.name.startswith(".publication-chapters.cleanup-stage-")
            and not failed["done"]
        ):
            failed["done"] = True
            first = sorted(path.iterdir())[0]
            first.unlink()
            raise OSError("injected partial publication stage cleanup")
        return real_rmtree(path)

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", partial_then_fail)
    with pytest.raises(OSError, match="partial publication stage cleanup"):
        publish_plan(plan, output, report_json, report_md)

    assert not stale_stage.exists()
    assert not report_json.exists()
    assert not report_md.exists()
    assert not output.exists()
    assert len(
        list(tmp_path.glob(".publication-chapters.cleanup-journal-*.json"))
    ) == 1
    assert len(
        list(tmp_path.glob(".publication-chapters.cleanup-stage-*"))
    ) == 1

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", real_rmtree)
    publish_plan(plan, output, report_json, report_md)

    assert output.is_dir()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not list(tmp_path.glob(".publication-chapters.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".publication-chapters.cleanup-stage-*"))


def test_dangling_symlink_publication_artifacts_fail_closed(tmp_path, monkeypatch):
    source = _fixture_corpus(tmp_path)
    plan = build_plan(source, 1)
    output = tmp_path / "publication_chapters"
    report_json = tmp_path / "reports" / "publication_cleanup.json"
    report_md = tmp_path / "reports" / "publication_cleanup.md"
    real_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == report_json else real_is_symlink(path),
    )
    with pytest.raises(
        PublicationCleanupError,
        match="ambiguous interrupted publication JSON artifact",
    ):
        publish_plan(plan, output, report_json, report_md)
    assert not output.exists()

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: True if Path(path) == output else real_is_symlink(path),
    )
    with pytest.raises(
        PublicationCleanupError,
        match="refusing to overwrite existing publication target",
    ):
        publish_plan(plan, output, report_json, report_md)


def test_missing_or_extra_numbered_files_fail_closed(tmp_path):
    source = tmp_path / "titled_chapters"
    source.mkdir()
    (source / "0001.md").write_bytes(b"Chapter 1: Title\n\nBODY\n")
    (source / "0003.md").write_bytes(b"Chapter 3: Title\n\nBODY\n")
    with pytest.raises(PublicationCleanupError):
        build_plan(source, 2)


def test_full_real_rogue_corpus_integration_plan_only():
    if os.environ.get("NOVELTRANSLATOR_ALLOW_INSTALLED_CORPUS") != "1":
        pytest.skip("installed-corpus integration is explicitly opt-in")
    if not DEFAULT_SOURCE_DIR.is_dir():
        pytest.skip("installed corpus is not present")
    expected_units = len(list(DEFAULT_SOURCE_DIR.glob("[0-9][0-9][0-9][0-9].md")))
    plan = build_plan(DEFAULT_SOURCE_DIR, expected_units, enforce_rogue_inventory=True)
    report = build_report(plan)
    assert report["total_files"] == expected_units
    assert report["blocker_count"] == 0
    assert report["verification"]["body_preservation"]["preserved_file_count"] == expected_units
    assert all(item.after_heading_lines for item in plan.units)
    for item in plan.units:
        source_body = item.source_bytes[item.source_body_offset :]
        output_body = item.output_bytes[item.output_body_offset :]
        assert source_body == output_body
