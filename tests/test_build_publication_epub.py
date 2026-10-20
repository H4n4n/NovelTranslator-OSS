import hashlib
import json
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

import build_publication_epub as publication
from build_publication_chapters import framed_corpus_fingerprint
from novel_project import NovelConfig, NovelProject, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]
ROGUE_FINGERPRINT = "357bacfb8df185af6cefa69bc44fe362977fd11b27f2be73be1c41f2273f1710"


def make_project(
    tmp_path,
    novel_id="fixture_novel",
    *,
    title="Finished Fixture",
    author="Fixture Author",
    source_language="zh",
    target_language="en",
    final_chapter=1,
):
    root = tmp_path / "novels" / novel_id
    root.mkdir(parents=True)
    config = NovelConfig(
        id=novel_id,
        title=title,
        author=author,
        source_language=source_language,
        target_language=target_language,
        profile="historical_military",
        final_chapter=final_chapter,
    )
    return NovelProject(tmp_path, root, config)


def write_editorial_apply_report(project, payloads, *, mutate=None):
    paths = publication.publication_paths(project)
    paths["editorial_apply_report"].parent.mkdir(parents=True, exist_ok=True)
    fingerprint = framed_corpus_fingerprint(payloads)
    report = {
        "schema_version": 1,
        "revision": "final-editorial-apply-v1.2",
        "status": "success",
        "ready": True,
        "success": True,
        "blocker_count": 0,
        "affected_unit_count": 0,
        "unaffected_unit_count": len(payloads),
        "output_corpus_fingerprint": fingerprint,
    }
    if mutate:
        mutate(report)
    paths["editorial_apply_report"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def install_corpus(project, texts):
    source_dir = publication.publication_paths(project)["source_dir"]
    source_dir.mkdir(parents=True)
    payloads = []
    for unit, text in enumerate(texts, 1):
        name = f"{unit:04d}.md"
        data = text.encode("utf-8")
        (source_dir / name).write_bytes(data)
        payloads.append((name, data))
    report = write_editorial_apply_report(project, payloads)
    return source_dir, report


def sample_texts():
    return [
        "Prologue\r\n\r\nOPENING\r\n",
        "Chapter 2. Normal Title (1)\r\n\r\nSECOND\r\n",
        "Chapter 3\r\n3. Split Title (2)\r\n\r\nTHIRD\r\n",
        "Epilogue 2\r\n\r\nFOURTH\r\n",
        "Side Story 1. Afterward (3)\r\n\r\nFIFTH\r\n",
    ]


def member_text(epub_path, suffix):
    with zipfile.ZipFile(epub_path) as archive:
        name = next(name for name in archive.namelist() if name.endswith(suffix))
        return archive.read(name).decode("utf-8")


@pytest.mark.parametrize("marker", ["* * *", "**", "*", r"\*\*\*"])
def test_required_standalone_scene_break_forms_render_consistently(marker):
    assert publication.is_publication_scene_break(marker)
    assert publication._render_body(marker) == '<p class="scene-break">* * *</p>'


@pytest.mark.parametrize("marker", ["***", "* *", "---", "==================="])
def test_existing_corpus_scene_break_forms_remain_supported(marker):
    assert publication.is_publication_scene_break(marker)


def test_inline_and_whole_paragraph_asterisk_italics():
    assert publication._render_body("*whole paragraph*") == "<p><em>whole paragraph</em></p>"
    assert publication._render_body("Before *inline* after.") == (
        "<p>Before <em>inline</em> after.</p>"
    )


def test_underscore_italics_bold_and_multiple_emphasis_spans():
    rendered = publication._render_body(
        "_first_ then **bold** and *second* plus _third_."
    )
    assert rendered == (
        "<p><em>first</em> then <strong>bold</strong> and <em>second</em> "
        "plus <em>third</em>.</p>"
    )


def test_emphasis_escapes_html_sensitive_characters_and_preserves_quotes():
    rendered = publication._render_body(
        '<outside & "quoted"> *inside & <safe> with\' apostrophe*'
    )
    assert rendered == (
        '<p>&lt;outside &amp; "quoted"&gt; '
        "<em>inside &amp; &lt;safe&gt; with' apostrophe</em></p>"
    )


def test_unmatched_and_ordinary_inline_asterisks_remain_literal_and_safe():
    assert publication._render_body("Unmatched *marker and _marker.") == (
        "<p>Unmatched *marker and _marker.</p>"
    )
    assert publication._render_body("2 * 3 and a*b*c") == "<p>2 * 3 and a*b*c</p>"
    assert publication._render_body(r"Literal \*marker\* and \_marker\_") == (
        r"<p>Literal \*marker\* and \_marker\_</p>"
    )
    assert not publication.is_publication_scene_break("Inline *italic* text")


@pytest.mark.parametrize(
    "paragraph,expected",
    [
        ("**", ["unrendered-scene-break"]),
        ("*", ["unrendered-scene-break"]),
        (r"\*\*\*", ["unrendered-scene-break"]),
        ("*italic*", ["unrendered-emphasis"]),
        ("_italic_", ["unrendered-emphasis"]),
        ("**bold**", ["unrendered-emphasis"]),
    ],
)
def test_reader_visible_markdown_artifact_classifier_catches_bad_xhtml(
    paragraph, expected
):
    document = ElementTree.fromstring(f"<html><body><p>{paragraph}</p></body></html>")
    assert publication.reader_visible_markdown_artifacts(document) == expected


def test_scene_break_element_is_not_reported_as_visible_markdown_artifact():
    document = ElementTree.fromstring(
        '<html><body><p class="scene-break">* * *</p></body></html>'
    )
    assert publication.reader_visible_markdown_artifacts(document) == []


def test_missing_editorial_chapters_fails_without_fallback(tmp_path):
    project = make_project(tmp_path)
    for old_dir in (
        project.root / "final" / "publication_chapters",
        project.root / "final" / "titled_chapters",
        project.root / "final" / "normalized_chapters",
        project.root / "output" / "chapters",
    ):
        old_dir.mkdir(parents=True)
        (old_dir / "0001.md").write_text("Chapter 1: Wrong\n\nBODY", encoding="utf-8")
    write_editorial_apply_report(project, [("0001.md", b"Chapter 1: Expected\n\nBODY")])
    with pytest.raises(publication.PublicationEpubError, match="editorial_chapters"):
        publication.load_publication_corpus(project)


def test_missing_publication_report_fails(tmp_path):
    project = make_project(tmp_path)
    source = publication.publication_paths(project)["source_dir"]
    source.mkdir(parents=True)
    (source / "0001.md").write_text("Prologue\n\nBODY", encoding="utf-8")
    with pytest.raises(publication.PublicationEpubError, match="editorial apply report"):
        publication.load_publication_corpus(project)


@pytest.mark.parametrize(
    "contents,match",
    [
        ("not json", "malformed"),
        ("[]", "JSON object"),
        (json.dumps({"schema_version": 99}), "editorial apply schema"),
    ],
)
def test_malformed_or_unsupported_report_fails(tmp_path, contents, match):
    project = make_project(tmp_path)
    report = publication.publication_paths(project)["editorial_apply_report"]
    report.parent.mkdir(parents=True)
    report.write_text(contents, encoding="utf-8")
    with pytest.raises(publication.PublicationEpubError, match=match):
        publication.load_publication_corpus(project)


def test_unsuccessful_editorial_apply_report_fails(tmp_path):
    project = make_project(tmp_path)
    source, _ = install_corpus(project, ["Prologue\n\nBODY"])
    report_path = publication.publication_paths(project)["editorial_apply_report"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["success"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(publication.PublicationEpubError, match="success flag"):
        publication.load_publication_corpus(project)


def test_fingerprint_mismatch_fails(tmp_path):
    project = make_project(tmp_path)
    source, _ = install_corpus(project, ["Prologue\n\nBODY"])
    (source / "0001.md").write_text("Prologue\n\nCHANGED", encoding="utf-8")
    with pytest.raises(publication.PublicationEpubError, match="fingerprint"):
        publication.load_publication_corpus(project)


def test_missing_unit_fails(tmp_path):
    project = make_project(tmp_path)
    source, _ = install_corpus(project, sample_texts()[:2])
    (source / "0002.md").unlink()
    with pytest.raises(publication.PublicationEpubError, match="missing"):
        publication.load_publication_corpus(project)


def test_extra_unit_fails(tmp_path):
    project = make_project(tmp_path)
    source, _ = install_corpus(project, ["Prologue\n\nBODY"])
    (source / "0002.md").write_text("Chapter 2: Extra\n\nBODY", encoding="utf-8")
    with pytest.raises(publication.PublicationEpubError, match="unexpected"):
        publication.load_publication_corpus(project)


def test_non_contiguous_report_inventory_fails(tmp_path):
    project = make_project(tmp_path)
    source = publication.publication_paths(project)["source_dir"]
    source.mkdir(parents=True)
    payloads = [
        ("0001.md", b"Prologue\n\nBODY"),
        ("0003.md", b"Chapter 3: Gap\n\nBODY"),
    ]
    for name, data in payloads:
        (source / name).write_bytes(data)
    write_editorial_apply_report(project, payloads)
    with pytest.raises(publication.PublicationEpubError, match="file record 2"):
        publication.load_publication_corpus(project)


@pytest.mark.parametrize(
    "heading",
    [
        "# Chapter 1: Wrapped",
        "Chapter 1<br>",
        "#!/GEN_METHOD_TITLE_START",
        "Chapter 1. < Wrapped >",
        "Unknown Heading",
    ],
)
def test_unknown_or_technical_heading_fails_closed(tmp_path, heading):
    project = make_project(tmp_path)
    install_corpus(project, [f"{heading}\n\nBODY"])
    with pytest.raises(publication.PublicationEpubError):
        publication.load_publication_corpus(project)


def test_publication_count_is_independent_of_config_final_chapter(tmp_path):
    project = make_project(tmp_path, final_chapter=1)
    install_corpus(project, sample_texts())
    corpus = publication.load_publication_corpus(project)
    assert len(corpus.units) == 5
    assert project.config.final_chapter == 1


def test_navigation_titles_come_exactly_from_publication_text(tmp_path):
    project = make_project(tmp_path, final_chapter=1)
    install_corpus(project, sample_texts())
    corpus = publication.load_publication_corpus(project)
    assert [unit.navigation_title for unit in corpus.units] == [
        "Prologue",
        "Chapter 2. Normal Title (1)",
        "Chapter 3 — 3. Split Title (2)",
        "Epilogue 2",
        "Side Story 1. Afterward (3)",
    ]


def test_final_epub_metadata_navigation_spine_and_zip_are_exact(tmp_path):
    project = make_project(
        tmp_path,
        title="Exact Finished Title",
        author="Exact Author",
        target_language="fr",
        final_chapter=1,
    )
    source, editorial_apply = install_corpus(project, sample_texts())
    before = framed_corpus_fingerprint(
        [(path.name, path.read_bytes()) for path in sorted(source.glob("*.md"))]
    )
    output = tmp_path / "temporary" / "fixture.epub"
    built, report = publication.build_publication_epub(
        project, output_path=output, write_reports=False
    )
    assert built == output
    assert report["content_document_count"] == 5
    assert report["spine_count"] == 6
    assert report["unit_spine_count"] == 5
    assert report["first_spine_item"] == "title_page"
    assert report["navigation_document_in_spine"] is False
    assert report["toc_count"] == 5
    assert report["ncx_toc_count"] == 5
    assert report["broken_internal_resource_reference_count"] == 0
    assert report["reader_visible_markdown_artifact_count"] == 0
    assert report["split_heading_document_count"] == 1
    assert report["metadata_title"] == "Exact Finished Title"
    assert report["metadata_language"] == "fr"
    assert report["source_unchanged"] is True
    assert report["input_file_count"] == 5
    assert before == editorial_apply["output_corpus_fingerprint"]
    after = framed_corpus_fingerprint(
        [(path.name, path.read_bytes()) for path in sorted(source.glob("*.md"))]
    )
    assert after == before

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist()[0] == "mimetype"
        info = archive.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
    opf = member_text(output, ".opf")
    assert ">Exact Finished Title</dc:title>" in opf
    assert ">fr</dc:language>" in opf
    assert "Exact Author" in opf
    assert "Chapters 1-5" not in opf
    assert "Checkpoint" not in opf
    opf_xml = ElementTree.fromstring(opf)
    manifest_items = opf_xml.findall(".//{*}manifest/{*}item")
    nav_items = [item for item in manifest_items if "nav" in item.get("properties", "").split()]
    assert len(nav_items) == 1
    assert nav_items[0].get("href") == "nav.xhtml"
    spine_ids = [item.get("idref") for item in opf_xml.findall(".//{*}spine/{*}itemref")]
    assert spine_ids == ["title_page", "unit_0001", "unit_0002", "unit_0003", "unit_0004", "unit_0005"]
    assert nav_items[0].get("id") not in spine_ids
    title_page = member_text(output, "title.xhtml")
    assert "Generated locally from committed translation files." not in title_page
    assert "Checkpoint" not in title_page
    chapter = member_text(output, "text/unit_0003.xhtml")
    chapter_xml = ElementTree.fromstring(chapter)
    stylesheet_links = chapter_xml.findall(".//{*}link")
    assert [link.get("href") for link in stylesheet_links] == ["../style/main.css"]
    heading = chapter_xml.find(".//{*}h1")
    assert heading.get("class") == "split-heading"
    assert [span.get("class") for span in heading.findall("./{*}span")] == [
        "chapter-label",
        "chapter-subtitle",
    ]
    assert ["".join(span.itertext()) for span in heading.findall("./{*}span")] == [
        "Chapter 3",
        "3. Split Title (2)",
    ]
    assert "(2)" in chapter

    validation = publication.verify_existing_publication_epub(project, output)
    assert validation["navigation_titles"][0] == "Prologue"
    assert validation["navigation_titles"][2] == "Chapter 3 — 3. Split Title (2)"
    assert validation["navigation_titles"][3] == "Epilogue 2"
    assert validation["navigation_titles"][4] == "Side Story 1. Afterward (3)"
    assert len(validation["content_members"]) == 5
    assert validation["broken_internal_resource_reference_count"] == 0


def test_internal_resource_reference_checker_detects_broken_css(tmp_path):
    archive_path = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "EPUB/text/unit_0001.xhtml",
            '<html><head><link rel="stylesheet" href="style/missing.css"/></head></html>',
        )
    with zipfile.ZipFile(archive_path) as archive:
        broken = publication.find_broken_internal_resource_references(
            archive, ["EPUB/text/unit_0001.xhtml"]
        )
    assert broken == [
        {
            "document": "EPUB/text/unit_0001.xhtml",
            "attribute": "href",
            "reference": "style/missing.css",
        }
    ]


def test_absent_author_is_not_invented(tmp_path):
    project = make_project(tmp_path, author=None)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "anonymous.epub"
    publication.build_publication_epub(project, output_path=output, write_reports=False)
    opf = member_text(output, ".opf")
    assert "<dc:creator" not in opf
    assert "Unknown" not in opf


def test_existing_output_and_report_refusal(tmp_path):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "final.epub"
    output.write_bytes(b"existing")
    with pytest.raises(publication.PublicationEpubError, match="refusing to overwrite"):
        publication.build_publication_epub(project, output_path=output, write_reports=False)
    assert output.read_bytes() == b"existing"


def test_transactional_temp_cleanup_on_validation_failure(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output_dir = tmp_path / "transaction"
    output = output_dir / "final.epub"

    def fail_validation(*args, **kwargs):
        raise publication.PublicationEpubError("simulated verification failure")

    monkeypatch.setattr(publication, "verify_epub_archive", fail_validation)
    with pytest.raises(publication.PublicationEpubError, match="simulated"):
        publication.build_publication_epub(project, output_path=output, write_reports=False)
    assert not output.exists()
    assert not list(output_dir.glob("*.tmp"))


def test_synthetic_build_writes_reports_only_when_requested(tmp_path):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"
    _, report = publication.build_publication_epub(
        project,
        output_path=output,
        write_reports=True,
        report_json_path=report_json,
        report_md_path=report_md,
    )
    assert json.loads(report_json.read_text(encoding="utf-8")) == report
    assert report_md.read_text(encoding="utf-8").startswith(
        "# Final Publication EPUB Report\n"
    )
    assert report["epub_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_hard_stop_during_final_source_check_does_not_expose_epub(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"
    real_load = publication.load_publication_corpus
    call_count = {"value": 0}

    class SimulatedHardStop(BaseException):
        pass

    def stop_on_final_source_check(project_arg):
        call_count["value"] += 1
        if call_count["value"] == 3:
            raise SimulatedHardStop("simulated process stop before EPUB commit")
        return real_load(project_arg)

    monkeypatch.setattr(publication, "load_publication_corpus", stop_on_final_source_check)

    with pytest.raises(SimulatedHardStop, match="before EPUB commit"):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert call_count["value"] == 3
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()


def _leave_exact_publication_prefix(
    project, output, report_json, report_md, monkeypatch
):
    real_load = publication.load_publication_corpus
    call_count = {"value": 0}

    class SimulatedHardStop(BaseException):
        pass

    def stop_on_final_source_check(project_arg):
        call_count["value"] += 1
        if call_count["value"] == 3:
            raise SimulatedHardStop("simulated process stop before EPUB commit")
        return real_load(project_arg)

    monkeypatch.setattr(publication, "load_publication_corpus", stop_on_final_source_check)
    with pytest.raises(SimulatedHardStop):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )
    monkeypatch.setattr(publication, "load_publication_corpus", real_load)
    temps = list(output.parent.glob(f".{output.name}.*.tmp"))
    assert len(temps) == 1
    return temps[0]


def test_exact_publication_report_temp_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    assert stale_temp.is_file()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()

    result_path, report = publication.build_publication_epub(
        project,
        output_path=output,
        write_reports=True,
        report_json_path=report_json,
        report_md_path=report_md,
    )

    assert result_path == output
    assert output.is_file()
    assert report["success"] is True
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_temp.exists()
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_interrupted_publication_cleanup_remains_recoverable(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    real_unlink = Path.unlink

    def fail_json_cleanup(path, *args, **kwargs):
        if Path(path) == report_json:
            raise OSError("injected publication recovery cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_cleanup)
    with pytest.raises(OSError, match="recovery cleanup"):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert report_json.is_file()
    assert not report_md.exists()
    assert stale_temp.is_file()
    assert not output.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result_path, report = publication.build_publication_epub(
        project,
        output_path=output,
        write_reports=True,
        report_json_path=report_json,
        report_md_path=report_md,
    )

    assert result_path == output
    assert output.is_file()
    assert report["success"] is True
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_temp.exists()


def test_interrupted_publication_cleanup_temp_only_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    real_unlink = Path.unlink

    class SimulatedHardStop(BaseException):
        pass

    def stop_on_temp_cleanup(path, *args, **kwargs):
        if Path(path) == stale_temp:
            raise SimulatedHardStop(
                "simulated hard stop after publication reports were removed"
            )
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stop_on_temp_cleanup)
    with pytest.raises(SimulatedHardStop, match="reports were removed"):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert stale_temp.is_file()
    assert not report_json.exists()
    assert not report_md.exists()
    assert not output.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result_path, report = publication.build_publication_epub(
        project,
        output_path=output,
        write_reports=True,
        report_json_path=report_json,
        report_md_path=report_md,
    )

    assert result_path == output
    assert output.is_file()
    assert report["success"] is True
    assert report_json.is_file()
    assert report_md.is_file()
    assert not stale_temp.exists()
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_invalid_publication_temp_only_prefix_fails_closed(
    tmp_path,
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"
    output.parent.mkdir(parents=True)
    stale_temp = output.parent / f".{output.name}.invalid.tmp"
    stale_temp.write_bytes(b"not an epub")

    with pytest.raises(
        publication.PublicationEpubError,
        match="temp-only prefix is not a validated EPUB",
    ):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert stale_temp.read_bytes() == b"not an epub"
    assert not output.exists()
    assert not report_json.exists()
    assert not report_md.exists()


def test_multiple_publication_temp_only_candidates_fail_closed(
    tmp_path,
    monkeypatch,
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    duplicate = output.parent / f".{output.name}.duplicate.tmp"
    duplicate.write_bytes(stale_temp.read_bytes())
    report_md.unlink()
    report_json.unlink()

    with pytest.raises(
        publication.PublicationEpubError,
        match="temp-only prefix: candidates=2",
    ):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert stale_temp.is_file()
    assert duplicate.is_file()
    assert not output.exists()
    assert not report_json.exists()
    assert not report_md.exists()


def test_divergent_publication_prefix_blocks_and_preserves_evidence(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["epub_sha256"] = "0" * 64
    report_json.write_bytes(
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    report_md.write_bytes(publication.render_markdown_report(report).encode("utf-8"))

    with pytest.raises(
        publication.PublicationEpubError,
        match="matches=0",
    ):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert stale_temp.is_file()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()


def test_publication_prefix_without_temp_evidence_fails_closed(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    stale_temp.unlink()

    with pytest.raises(
        publication.PublicationEpubError,
        match="matches=0",
    ):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()


def test_multiple_matching_publication_temps_fail_closed(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    report_json = tmp_path / "release" / "publication_epub.json"
    report_md = tmp_path / "release" / "publication_epub.md"

    stale_temp = _leave_exact_publication_prefix(
        project, output, report_json, report_md, monkeypatch
    )
    duplicate = output.parent / f".{output.name}.duplicate.tmp"
    duplicate.write_bytes(stale_temp.read_bytes())

    with pytest.raises(
        publication.PublicationEpubError,
        match="matches=2",
    ):
        publication.build_publication_epub(
            project,
            output_path=output,
            write_reports=True,
            report_json_path=report_json,
            report_md_path=report_md,
        )

    assert stale_temp.is_file()
    assert duplicate.is_file()
    assert report_json.is_file()
    assert report_md.is_file()
    assert not output.exists()


def test_publication_source_validation_finishes_before_terminal_epub_commit(
    tmp_path, monkeypatch
):
    project = make_project(tmp_path)
    install_corpus(project, ["Prologue\n\nBODY"])
    output = tmp_path / "release" / "final.epub"
    real_load = publication.load_publication_corpus
    real_install = publication.atomic_replace_prepared_file
    committed = {"value": False}

    def guarded_load(project_arg):
        if committed["value"]:
            pytest.fail("publication source was read after terminal EPUB commit")
        return real_load(project_arg)

    def observed_install(temp_path, output_path):
        real_install(temp_path, output_path)
        committed["value"] = True

    monkeypatch.setattr(publication, "load_publication_corpus", guarded_load)
    monkeypatch.setattr(publication, "atomic_replace_prepared_file", observed_install)

    result_path, report = publication.build_publication_epub(
        project,
        output_path=output,
        write_reports=False,
    )

    assert committed["value"] is True
    assert result_path == output
    assert output.is_file()
    assert report["source_unchanged"] is True



def test_real_rogue_publication_temporary_integration(tmp_path):
    if os.environ.get("NOVELTRANSLATOR_ALLOW_INSTALLED_CORPUS") != "1":
        pytest.skip("installed-corpus integration is explicitly opt-in")
    source_dir = REPO_ROOT / "novels" / "rogue_first_prince" / "final" / "editorial_chapters"
    editorial_apply_report = (
        REPO_ROOT / "novels" / "rogue_first_prince" / "final" / "reports"
        / "final_editorial_apply.json"
    )
    if not source_dir.is_dir() or not editorial_apply_report.is_file():
        pytest.skip("real Rogue editorial corpus is not present")
    project = load_novel_project("rogue_first_prince", REPO_ROOT)
    corpus = publication.load_publication_corpus(project)
    assert len(corpus.units) == 351
    assert corpus.fingerprint["aggregate_sha256"] == ROGUE_FINGERPRINT
    assert corpus.fingerprint["aggregate_byte_count"] == 5223621
    assert project.config.final_chapter == 350
    assert project.config.title == "I Became the Rogue First Prince"

    output = tmp_path / "rogue_first_prince.epub"
    _, report = publication.build_publication_epub(
        project, output_path=output, write_reports=False
    )
    expected_split_units = [
        39, 48, 49, 83, 89, 90, 99, 102,
        117, 129, 130, 142, 155, 156, 158, 224,
    ]
    assert report["input_file_count"] == 351
    assert report["content_document_count"] == 351
    assert report["spine_count"] == 352
    assert report["unit_spine_count"] == 351
    assert report["first_spine_item"] == "title_page"
    assert report["navigation_document_in_spine"] is False
    assert report["toc_count"] == 351
    assert report["ncx_toc_count"] == 351
    assert report["book_title"] == "I Became the Rogue First Prince"
    assert report["broken_internal_resource_reference_count"] == 0
    assert report["reader_visible_markdown_artifact_count"] == 0
    assert report["split_heading_document_count"] == 16
    assert report["split_heading_units"] == expected_split_units
    assert report["source_unchanged"] is True
    assert publication.load_publication_corpus(project).fingerprint == corpus.fingerprint
