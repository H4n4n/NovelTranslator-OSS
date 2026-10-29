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


@pytest.fixture(autouse=True)
def isolated_rendering_and_regeneration_contract(request, monkeypatch):
    """These pre-D1 tests isolate stage rendering/recovery, not readiness.

    New test_publication_readiness tests exercise the real final gate with no
    readiness mock. Generation tests also retain the real gate, with explicit
    synthetic review evidence installed by the novel fixture.
    """
    import historical_reconciliation
    if "novel" not in request.fixturenames:
        monkeypatch.setattr(historical_reconciliation, "require_publication_freshness", lambda project: None)


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


BODY_PROOF_SOURCE = (
    'Plain & <safe> "quoted" café.\nNext  line.\n\n'
    'Before *soft* and _other_ then **firm** after.\n\n'
    '***\n\nLast paragraph.\n'
)
BODY_PROOF_XHTML = (
    '<p>Plain &amp; &lt;safe&gt; "quoted" café.<br/>Next  line.</p>'
    '<p>Before <em>soft</em> and <em>other</em> then <strong>firm</strong> after.</p>'
    '<p class="scene-break">* * *</p><p>Last paragraph.</p>'
)


def rewrite_archive_unit(epub_path, transform, unit=1):
    """Rewrite one synthetic member, preserving ZIP layout and all other bytes."""
    with zipfile.ZipFile(epub_path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    rewritten = 0
    with zipfile.ZipFile(epub_path, "w") as archive:
        for info, data in entries:
            if info.filename.endswith(f"text/unit_{unit:04d}.xhtml"):
                data = transform(data)
                rewritten += 1
            archive.writestr(info, data)
    assert rewritten == 1


def replace_archive_body(epub_path, body_xhtml):
    def transform(data):
        document = ElementTree.fromstring(data)
        body = document.find(".//{*}body")
        heading = body.find("{*}h1")
        body.clear()
        body.append(heading)
        fragment = ElementTree.fromstring(
            '<div xmlns="http://www.w3.org/1999/xhtml">' + body_xhtml + '</div>'
        )
        body.text = fragment.text
        body.extend(fragment)
        return ElementTree.tostring(document, encoding="utf-8")
    rewrite_archive_unit(epub_path, transform)


@pytest.fixture
def body_proof_epub(tmp_path):
    project = make_project(tmp_path)
    install_corpus(project, ["Chapter 1. Body Proof\n\n" + BODY_PROOF_SOURCE])
    corpus = publication.load_publication_corpus(project)
    output, _ = publication.build_publication_epub(
        project, output_path=tmp_path / "body-proof.epub", write_reports=False
    )
    return output, corpus


@pytest.mark.parametrize("marker", ["* * *", "**", "*", r"\*\*\*", "***", "* *", "---", "===="])
def test_body_proof_accepts_source_syntax_and_newlines(tmp_path, marker):
    project = make_project(tmp_path)
    body = BODY_PROOF_SOURCE.replace("***", marker)
    body += r"Literal \*escaped\* and unmatched *mark, a*b*c, 2 * 3." + "\n"
    install_corpus(project, [("Chapter 1. Body Proof\n\n" + body).replace("\n", "\r\n")])
    output, report = publication.build_publication_epub(
        project, output_path=tmp_path / "positive.epub", write_reports=False
    )
    assert report["validation_checks"]["publication_bodies_match_editorial"] is True
    assert publication.verify_epub_archive(output, publication.load_publication_corpus(project))[
        "checks"
    ]["publication_bodies_match_editorial"] is True


def test_body_proof_accepts_equivalent_xhtml_serialization_and_wrappers(body_proof_epub):
    output, corpus = body_proof_epub
    # Literal expected XHTML is independent of the production renderer.
    replace_archive_body(output, BODY_PROOF_XHTML)
    def transform(data):
        document = ElementTree.fromstring(data)
        namespace = "{http://www.w3.org/1999/xhtml}"
        body = document.find(".//{*}body")
        wrapper = ElementTree.Element(namespace + "section", {"id": "chapter-wrapper"})
        wrapper.text = "\n  "
        for child in list(body):
            body.remove(child)
            wrapper.append(child)
            child.tail = "\n  "
        body.append(wrapper)
        document.set("data-generator", "different harmless wrapper")
        serialized = ElementTree.tostring(document, encoding="unicode", short_empty_elements=False)
        return serialized.replace("café", "caf&#233;").encode("utf-8")
    rewrite_archive_unit(output, transform)
    assert publication.verify_epub_archive(output, corpus)["checks"][
        "publication_bodies_match_editorial"
    ] is True


@pytest.mark.parametrize(
    "old,new",
    [
        ('café', 'changed'),
        ('café', 'cafe'),
        ('Next  line.', 'Next line.'),
        ('<p>Last paragraph.</p>', ''),
        ('<p>Last paragraph.</p>', '<p>Last paragraph.</p><p>Last paragraph.</p>'),
        ('<p>Last paragraph.</p>', '<p>Last</p><p> paragraph.</p>'),
        ('</p><p>Before', '<br/>Before'),
        ('<em>soft</em>', 'soft'),
        ('<strong>firm</strong>', 'firm'),
        ('<em>soft</em>', '<strong>soft</strong>'),
        ('<strong>firm</strong>', '<em>firm</em>'),
        ('Before <em>soft</em>', '<em>Before </em>soft'),
        ('<em>soft</em>', '<em><strong>soft</strong></em>'),
        ('<br/>', ''),
        ('<br/>', '\n'),
        ('<br/>Next  line.', 'Next<br/>  line.'),
        ('class="scene-break"', ''),
        ('* * *</p>', '***</p>'),
        ('<p class="scene-break">* * *</p>', ''),
        ('<p class="scene-break">* * *</p>', '<p class="scene-break">* * *</p>' * 2),
        ('<p>Last paragraph.</p>', '<p class="scene-break">Last paragraph.</p>'),
        ('<p>Last paragraph.</p>', '<div>Last paragraph.</div>'),
        ('<p>Last paragraph.</p>', '<p>Last paragraph.</p>UNACCOUNTED TAIL'),
        ('<p>Last paragraph.</p>', '<p>Last paragraph.</p>\u00a0'),
        ('<p>Last paragraph.</p>', '<p>Last paragraph.</p><div>EXTRA</div>'),
        ('<em>soft</em>', '<span>soft</span>'),
        ('<em>soft</em>', '<em xmlns="urn:foreign">soft</em>'),
        ('<br/>', '<br>EXTRA</br>'),
        ('<p>Last paragraph.</p>', '<p>Last paragraph.</p><script>EXTRA</script>'),
    ],
    ids=[
        'substitution', 'unicode-change', 'whitespace-loss', 'omission', 'duplication',
        'paragraph-split', 'paragraph-merge', 'lost-emphasis', 'lost-strong',
        'emphasis-to-strong', 'strong-to-emphasis', 'shifted-emphasis', 'nested-style',
        'lost-line-break', 'literal-newline', 'shifted-line-break', 'lost-scene-class',
        'changed-scene-text', 'lost-scene', 'duplicate-scene', 'fake-scene',
        'unwrapped-paragraph', 'unaccounted-tail', 'nonbreaking-space-tail', 'unaccounted-container',
        'unsupported-inline', 'foreign-namespace', 'text-inside-break', 'extra-element',
    ],
)
def test_body_proof_rejects_archive_body_mutations(body_proof_epub, old, new):
    output, corpus = body_proof_epub
    assert BODY_PROOF_XHTML.count(old) == 1
    replace_archive_body(output, BODY_PROOF_XHTML.replace(old, new))
    with pytest.raises(publication.PublicationEpubError, match="unit 1 XHTML body differs") as error:
        publication.verify_epub_archive(output, corpus)
    assert str(error.value) == "unit 1 XHTML body differs from publication text"
    assert corpus.units[0].path.read_bytes() == corpus.units[0].source_bytes


@pytest.mark.parametrize("body", ["", "<p>OTHER UNIT BODY</p>",
    '<p>Last paragraph.</p>' + BODY_PROOF_XHTML.removesuffix('<p>Last paragraph.</p>')])
def test_body_proof_rejects_empty_substituted_or_reordered_body(body_proof_epub, body):
    output, corpus = body_proof_epub
    replace_archive_body(output, body)
    with pytest.raises(publication.PublicationEpubError, match="XHTML body differs"):
        publication.verify_epub_archive(output, corpus)


def test_body_proof_does_not_use_renderer_as_expected_value(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    install_corpus(project, ["Chapter 1. Renderer Defect\n\nApproved prose.\n"])
    monkeypatch.setattr(publication, "_render_body", lambda body: '<p>Wrong prose.</p>')
    output = tmp_path / "rejected.epub"
    with pytest.raises(publication.PublicationEpubError, match="XHTML body differs"):
        publication.build_publication_epub(project, output_path=output, write_reports=False)
    assert not output.exists()


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


# AUT-2: source-bound generations (all inputs are disposable).
"""Disposable AUT-2 source-to-EPUB regeneration contracts, no provider access."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import zipfile

import pytest

import historical_reconciliation as history
import publication_regeneration as regen
from novel_project import NovelProject
from retroactive_span_planning import canonical_hash, digest
from test_production_lifecycle import aut1_engine
from test_apply_title_resolution_plan import _fixture as title_fixture


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and p.name != ".production.lock"}


@pytest.fixture(params=["zh", "ko"])
def novel(aut1_engine, monkeypatch, request):
    import production_periodic_audit_finalization as final
    from production_run_lock import production_run_lock
    e, root = aut1_engine.engine, aut1_engine.root
    key = "韓" if request.param == "zh" else "한민"
    config = replace(e.ACTIVE_NOVEL_CONFIG, final_chapter=3, source_language=request.param, source_type="txt")
    monkeypatch.setattr(e, "ACTIVE_NOVEL_CONFIG", config)
    project = NovelProject(Path(__file__).resolve().parents[1], root, config)
    write(project.config_file, config.to_dict())
    source = (key + " arrived\n" + key + " departed\n" + key + " returned\n").encode()
    project.source_txt.parent.mkdir(parents=True, exist_ok=True)
    project.source_txt.write_bytes(source)
    units = [{"chapter": n, "production_index": n, "text": key + f" {n}",
              "title": "Synthetic", "source_document_sha256": digest(source)} for n in range(1, 4)]
    write(project.chapters_file, units)
    monkeypatch.setattr(e, "chapters", units)
    monkeypatch.setattr(e, "chapter_map", {u["chapter"]: u for u in units})
    monkeypatch.setattr(e, "glossary", {"entries": {key: {"english": "Han", "category": "character", "status": "provisional", "first_chapter": 1}}})
    original_apply = e.apply_periodic_audit
    monkeypatch.setattr(e, "apply_periodic_audit", lambda start, end, result, glossary_data=None:
        original_apply(start, end, result, glossary_data, targets=[{"chinese": key}]))
    e.progress.update(completed=[1, 2, 3], last_completed=3, next_chapter=4, last_audited_chapter=0)
    e.save_json(e.GLOSSARY_FILE, e.glossary)
    e.save_json(e.PROGRESS_FILE, e.progress)
    e.save_json(e.ISSUES_FILE, e.issues_data)
    e.save_json(e.AUDIT_HISTORY_FILE, e.audit_history)
    e.save_json(e.CONTINUITY_FILE, e.continuity)
    for n in range(1, 4):
        heading = "Chapter 1. 7. Old Title (1)" if n == 1 else f"# Chapter {n}. Stable"
        raw = (heading + "\r\n\r\nHan walked along the strete. **Hand** — café.\r\n").encode()
        (project.chapter_dir / f"{n:04d}.md").write_bytes(raw)
        write(project.qa_dir / f"{n:04d}.json", {"synthetic_pre_correction_qa": True})
    # Existing output ownership must survive every downstream operation.
    (root / "final/editorial_chapters").mkdir(parents=True)
    (root / "final/editorial_chapters/0001.md").write_bytes(b"stale but retained")
    (root / "final/epub").mkdir()
    (root / "final/epub/prior.epub").write_bytes(b"prior published bytes")
    project.epub_dir.mkdir(parents=True, exist_ok=True)
    (project.epub_dir / "unrelated.epub").write_bytes(b"unrelated volume")
    e.save_json(e.periodic_audit_checkpoint_path(1, 3), {"synthetic_accepted_decisions": True})
    stats = {field: 0 for field in final.STAT_TOTAL_FIELDS}
    stats["api_request_count"] = 0
    # The synthetic historical writer cooperates with production's real lease.
    # Release it before returning so publication owns its separate lock scope.
    with production_run_lock(project.root):
        final.finalize_periodic_audit(e, 1, 3, {"decisions": [{"chinese": key, "action": "correct", "english": "Hann"}], "issues": []},
            stats, {"candidate_count": 1, "candidate_set_hash": canonical_hash("accepted"), "api_request_count": 0})
        report = history.save_final_consistency(e)
    assert report["status"] == "CHECKED"
    report_sha = canonical_hash(report)
    install_approvals(project, report_sha)
    # Existing regeneration contracts provide explicit synthetic review after
    # non-EPUB stages exist. No production automatic semantic review is added.
    # D1 gate tests import this fixture but install reviews themselves.
    if request.node.path.name == "test_build_publication_epub.py":
        import publication_readiness as readiness
        original_immutable = regen._immutable_json
        def synthetic_review(path, value):
            original_immutable(path, value)
            if path.name == "source_bound_provenance.json":
                final_dir = path.parent.parent
                workspace = regen.PublicationWorkspace(project, final_dir)
                graph = readiness.dependency_graph(workspace)
                review = {"revision": readiness.REVISION,
                    "novel_id": project.config.id, "graph_sha256": canonical_hash(graph),
                    "reviewer": "synthetic fixture authority",
                    "reviews": {key: "explicitly_verified" for key in readiness.REQUIRED_REVIEWS}}
                review_path = project.root / readiness.REVIEW
                if not review_path.exists() or history.read_object(review_path) != review:
                    write(review_path, review)
        monkeypatch.setattr(regen, "_immutable_json", synthetic_review)
    return project, e, report_sha, aut1_engine


def install_approvals(project, report_sha):
    import apply_normalization_plan as normal
    import apply_title_resolution_plan as titles
    import build_publication_chapters as cleanup
    directory = project.root / "approval_preview"
    directory.mkdir(exist_ok=True)
    baseline = directory / "chapters"
    baseline.mkdir(exist_ok=True)
    for p in project.chapter_dir.glob("*.md"):
        (baseline / p.name).write_bytes(p.read_bytes())
    fp = normal.corpus_fingerprint(baseline)
    normal_plan = {"schema_version": 3, "plan_revision": "2.1",
        "corpus": {"project_id": project.config.id, "inventory_recorded_final_sha256": fp["aggregate_sha256"], "verified_final_fingerprint": fp},
        "safety": {"source_identity_required": True, "global_string_replacement_allowed": False},
        "application_policy": {"mode": "fail_closed_exact_occurrence_only", "paragraph_number_alone_is_sufficient": False,
            "global_replacement_authorized": False, "abort_on_any_precondition_mismatch": True, "review_required_is_executable": False},
        "text_decisions": [], "pronoun_decisions": [], "summary_counts": {"APPROVED_CORRECTION": 0,
            "APPROVED_CORRECTION_OCCURRENCES": 0, "PRONOUN_APPROVED_CORRECTION_OCCURRENCES": 0,
            "TOTAL_APPROVED_PROSE_OCCURRENCES": 0, "REVIEW_REQUIRED": 0, "FALSE_POSITIVE_NO_CHANGE": 0}}
    raw = (baseline / "0001.md").read_bytes()
    title_dir = directory / "title_fixture"
    title_dir.mkdir(exist_ok=True)
    title_source, title_plan = title_fixture(title_dir, heading=raw.decode().splitlines()[0], body="Hann")
    # Use the real CRLF corpus and exact title-line/file anchors.
    title_plan["corpora"]["normalized_title_base"] = titles.corpus_fingerprint(baseline, 3)
    anchor = title_plan["title_decisions"][0]["application_anchors"][0]
    anchor["baseline_normalized_chapter_file_sha256"] = digest(raw)
    write(project.root / regen.APPROVALS["normalization_plan"], normal_plan)
    write(project.root / regen.APPROVALS["title_plan"], title_plan)
    # Independently derive this fixture's intended post-title presentation checkpoint.
    outputs = []
    for p in sorted(baseline.glob("*.md")):
        data = p.read_bytes()
        if p.stem == "0001":
            data = data.replace(b"Old Title", b"New Title")
        outputs.append((p.name, cleanup.transform_unit(int(p.stem), p, data).output_bytes))
    fp = cleanup.framed_corpus_fingerprint(outputs)
    source = project.source_txt.read_bytes()
    text = source.decode().splitlines()[2]
    manifest = {"schema_version": 1, "revision": "synthetic-editorial-v1", "novel": project.config.id,
        "immutable_publication_checkpoint": {k: fp[k] for k in ("file_count", "aggregate_byte_count", "aggregate_sha256")},
        "korean_source": {"byte_count": len(source), "sha256": digest(source), "preservation_anchors": []},
        "authorized_set": {"operation_count": 1, "heading_operation_count": 0, "prose_operation_count": 1, "affected_units": [3]},
        "edits": [{"edit_id": "fixture-typo", "unit": 3, "edit_category": "PROSE_TYPO", "scope": "prose",
            "expected_english": "strete", "replacement": "street", "source_evidence": [{"anchor_text": text,
                "anchor_sha256": digest(text.encode()), "source_locator": {"line_number_hint": 3}}]}], "preservation_guards": [{"guard_id": "keep-title", "units": [2], "expected_heading_lines": {"2": ["Chapter 2. Stable", ""]}}]}
    write(project.root / regen.APPROVALS["editorial_authorization"], manifest)
    snapshot_value = regen.source_snapshot(project, report_sha)
    authorization = {"revision": regen.REVISION, "novel_id": project.config.id, "enabled": True,
        "finalization": "committed-identity-v1", "allow_manual_approved": False, "whole_history_sha256": report_sha,
        "source_snapshot_sha256": canonical_hash(snapshot_value)}
    authorization.update({k + "_sha256": digest((project.root / p).read_bytes()) for k, p in regen.APPROVALS.items()})
    write(project.root / regen.AUTHORIZATION, authorization)


def test_aut2_real_fresh_epub_from_approved_history_and_all_stage_owners(novel):
    project, e, _, h = novel
    before = snapshot(project.root)
    assert history.publication_freshness(project)["status"] == "STALE"
    assert not (project.root / regen.GENERATIONS).exists()
    assert not (project.root / regen.POINTER).exists()
    result = regen.regenerate(project)
    assert result["status"] == "CURRENT" and result["content_document_count"] == 3
    output = Path(result["epub"])
    assert history.read_object(output.parent.parent / "glossary.json") == history.read_object(project.glossary_file)
    assert history.read_object(project.root / regen.POINTER)["generation"] == result["generation"]
    current = regen.current_generation(project)
    assert current["status"] == "CURRENT" and current["generation"] == result["generation"]
    with zipfile.ZipFile(output) as archive:
        body = "\n".join(archive.read(n).decode() for n in archive.namelist() if "unit_" in n)
    assert "Hann walked" in body and "New Title" in body and "street" in body
    assert "**Hand**" not in body and "<strong>Hand</strong>" in body
    after = snapshot(project.root)
    assert all(after[p] == value for p, value in before.items())
    report = history.publication_freshness(project)
    assert report["status"] == "CURRENT" and report["rebuilt_epubs"] == 1
    assert report["artifacts"][-1]["status"] == "CURRENT"
    assert regen.regenerate(project) == result
    assert snapshot(project.root) == after
    assert h.calls == []


@pytest.mark.parametrize("damage", ["source", "chapter", "glossary", "title", "editorial", "missing_title", "report", "receipt", "checkpoint", "pending", "extra_chapter", "foreign_authorization"])
def test_aut2_stale_or_missing_dependencies_preserve_every_existing_byte(novel, damage):
    project, _, _, h = novel
    if damage == "source": project.source_txt.write_bytes(b"changed source")
    elif damage == "chapter": (project.chapter_dir / "0001.md").write_bytes(b"outside edit")
    elif damage == "glossary": write(project.glossary_file, {"entries": {}})
    elif damage in {"title", "editorial"}: (project.root / regen.APPROVALS["title_plan" if damage == "title" else "editorial_authorization"]).write_bytes(b"{}")
    elif damage == "missing_title": (project.root / regen.APPROVALS["title_plan"]).unlink()
    elif damage == "report": next((project.audit_dir / "whole_history").glob("*.json")).write_bytes(b'{}')
    elif damage == "receipt": next((project.root / "output/publication_freshness").glob("*.json")).write_bytes(b'{"status":1,"status":1}')
    elif damage == "checkpoint": epath = project.audit_dir / "audit_0001_0003.checkpoint.json"; epath.write_bytes(b"{}")
    elif damage == "pending": (project.pending_dir / "0004.md").write_bytes(b"paid pending")
    elif damage == "extra_chapter": (project.chapter_dir / "0004.md").write_bytes(b"uncommitted")
    else:
        auth = history.read_object(project.root / regen.AUTHORIZATION); auth["novel_id"] = "foreign"; write(project.root / regen.AUTHORIZATION, auth)
    before = snapshot(project.root)
    with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
        regen.regenerate(project)
    assert snapshot(project.root) == before and h.calls == []


@pytest.mark.parametrize("phase", ["before_epub", "after_epub", "before_pointer", "after_pointer"])
def test_aut2_interrupted_publication_replays_and_is_idempotent(novel, monkeypatch, phase):
    import build_publication_epub as epub
    project, _, _, h = novel
    original_build, original_write = epub.build_publication_epub, regen.atomic_write_json
    def build(*args, **kwargs):
        if phase == "before_epub": raise KeyboardInterrupt()
        value = original_build(*args, **kwargs)
        if phase == "after_epub": raise KeyboardInterrupt()
        return value
    def write_crash(path, value):
        if path == project.root / regen.POINTER:
            if phase == "before_pointer": raise KeyboardInterrupt()
            original_write(path, value)
            raise KeyboardInterrupt()
        original_write(path, value)
    with monkeypatch.context() as mp:
        mp.setattr(epub, "build_publication_epub", build)
        if "pointer" in phase: mp.setattr(regen, "atomic_write_json", write_crash)
        with pytest.raises(KeyboardInterrupt): regen.regenerate(project)
    assert history.publication_freshness(project)["status"] == ("CURRENT" if phase == "after_pointer" else "STALE")
    result = regen.regenerate(project)
    before = snapshot(project.root)
    assert regen.regenerate(project) == result
    assert snapshot(project.root) == before and h.calls == []


def test_aut2_self_consistent_old_editorial_is_not_current(novel):
    import build_publication_epub as epub
    project, _, _, _ = novel
    result = regen.regenerate(project)
    final_dir = Path(result["epub"]).parent.parent
    workspace = regen.PublicationWorkspace(project, final_dir)
    # Remains internally consistent, but its committed ancestor has changed.
    (project.chapter_dir / "0001.md").write_bytes(b"newer historical correction")
    assert epub.load_publication_corpus(workspace).fingerprint
    assert history.publication_freshness(project)["status"] == "STALE"
    before = snapshot(project.root)
    with pytest.raises(RuntimeError): epub.verify_existing_publication_epub(workspace)
    assert snapshot(project.root) == before


def test_aut2_conflicting_novel_lock_blocks_before_generation(novel):
    from production_run_lock import production_run_lock, ProductionRunLockedError
    project, _, _, _ = novel
    before = snapshot(project.root)
    with production_run_lock(project.root):
        with pytest.raises(ProductionRunLockedError): regen.regenerate(project)
    assert snapshot(project.root) == before

@pytest.mark.parametrize("damage", ["intent", "provenance", "duplicate_provenance", "manifest", "normal_report", "title_report", "cleanup", "editorial_plan", "editorial_apply", "epub_report", "missing_epub_report", "epub", "pointer", "traversal_pointer", "foreign_pointer", "linked_stage"])
def test_aut2_contradictory_generation_never_certifies_current(novel, monkeypatch, damage):
    project, _, _, h = novel
    result = regen.regenerate(project)
    final = Path(result["epub"]).parent.parent
    targets = {"intent": final.parent / "intent.json", "provenance": final / "reports/source_bound_provenance.json",
        "manifest": final / "manifest.json", "normal_report": final / "reports/normalization_application.json",
        "title_report": final / "reports/title_application.json", "cleanup": final / "reports/publication_cleanup.json",
        "editorial_plan": final / "reports/final_editorial_plan.json", "editorial_apply": final / "reports/final_editorial_apply.json",
        "epub_report": final / "reports/publication_epub.json"}
    if damage in targets:
        value = history.read_object(targets[damage]); value["contradictory"] = True; write(targets[damage], value)
    elif damage == "duplicate_provenance":
        path = targets["provenance"]
        raw = path.read_text(encoding="utf-8"); path.write_text(raw.replace('"revision": "source-bound-publication-v1"', '"revision": "source-bound-publication-v1", "revision": "source-bound-publication-v1"', 1), encoding="utf-8")
    elif damage == "missing_epub_report": targets["epub_report"].unlink()
    elif damage == "epub": Path(result["epub"]).write_bytes(b"broken zip")
    elif damage in {"pointer", "traversal_pointer", "foreign_pointer"}:
        path = project.root / regen.POINTER
        value = history.read_object(path)
        value["generation" if damage == "traversal_pointer" else "novel_id" if damage == "foreign_pointer" else "epub_sha256"] = "../escape" if damage == "traversal_pointer" else "foreign" if damage == "foreign_pointer" else "0" * 64
        write(path, value)
    else:
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == final / "normalized_chapters" or original(p))
    before = snapshot(project.root)
    assert history.publication_freshness(project)["status"] == "STALE"
    assert snapshot(project.root) == before and h.calls == []


@pytest.mark.parametrize("stage", ["title_plan", "editorial_authorization", "normalization_plan"])
def test_aut2_rehashing_old_plan_does_not_fabricate_fresh_approval(novel, stage):
    project, _, _, _ = novel
    path = project.root / regen.APPROVALS[stage]
    plan = history.read_object(path)
    if stage == "title_plan":
        plan["corpora"]["normalized_title_base"]["files"][0]["sha256"] = "0" * 64
    elif stage == "normalization_plan":
        plan["corpus"]["verified_final_fingerprint"]["aggregate_sha256"] = "0" * 64
    else:
        plan["immutable_publication_checkpoint"]["aggregate_sha256"] = "0" * 64
    write(path, plan)
    authorization = history.read_object(project.root / regen.AUTHORIZATION)
    authorization[stage + "_sha256"] = digest(path.read_bytes())
    write(project.root / regen.AUTHORIZATION, authorization)
    protected = {str(p): p.read_bytes() for p in [*project.chapter_dir.glob("*.md"), *project.epub_dir.glob("*.epub"), * (project.root / "final/epub").glob("*.epub")]}
    with pytest.raises((RuntimeError, ValueError)): regen.regenerate(project)
    assert {name: Path(name).read_bytes() for name in protected} == protected
    assert not (project.root / regen.POINTER).exists()


@pytest.mark.parametrize("after", [False, True])
def test_aut2_crash_at_existing_epub_owner_final_replace(novel, monkeypatch, after):
    import build_publication_epub as epub
    project, _, _, _ = novel
    original = epub.atomic_replace_prepared_file
    def interrupted(temp, target):
        if after: original(temp, target)
        raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(epub, "atomic_replace_prepared_file", interrupted)
        with pytest.raises(KeyboardInterrupt): regen.regenerate(project)
    assert history.publication_freshness(project)["status"] == "STALE"
    regen.regenerate(project)
    before = snapshot(project.root)
    regen.regenerate(project)
    assert snapshot(project.root) == before
    assert history.publication_freshness(project)["status"] == "CURRENT"


def test_aut2_multi_novel_isolation_and_foreign_generation_rejection(novel):
    from novel_project import NovelConfig
    project, _, _, _ = novel
    other = project.root.parent / (project.root.name + "_other_novel")
    other.mkdir()
    second = NovelProject(project.repository_root, other, replace(project.config, id="other_novel"))
    write(second.config_file, second.config.to_dict())
    (other / "untouched.epub").write_bytes(b"other novel published bytes")
    before = snapshot(other)
    result = regen.regenerate(project)
    assert snapshot(other) == before
    write(other / regen.POINTER, history.read_object(project.root / regen.POINTER))
    assert regen.current_generation(second)["status"] == "STALE"
    assert regen.current_generation(project)["status"] == "CURRENT"
    assert Path(result["epub"]).is_relative_to(project.root)


def test_aut2_newer_revision_invalidates_every_stage_without_rewriting_them(novel):
    project, _, _, _ = novel
    result = regen.regenerate(project)
    generation = Path(result["epub"]).parent.parent
    before = snapshot(generation)
    (project.chapter_dir / "0001.md").write_bytes(b"newer historical revision")
    report = history.publication_freshness(project)
    assert report["status"] == "STALE" and report["rebuilt_epubs"] == 0
    assert any(a["path"] == Path(result["epub"]).relative_to(project.root).as_posix() and a["status"] == "STALE" for a in report["artifacts"])
    assert any(r["path"] == "final/editorial_chapters/0001.md" and r["status"] == "STALE" for r in report["downstream_outputs"])
    with pytest.raises(RuntimeError): regen.regenerate(project)
    assert snapshot(generation) == before


def test_aut2_diagnostics_do_not_emit_expected_prose():
    import apply_title_resolution_plan as titles
    diagnostic = regen._blocker(titles.TitleApplicationError("unexpected secret source prose"))
    assert "secret source prose" not in json.dumps(diagnostic)

@pytest.mark.parametrize("operation", ["build", "verify"])
def test_aut2_source_drift_during_epub_operation_fails_closed(novel, monkeypatch, operation):
    project, _, _, _ = novel
    result = regen.regenerate(project)
    workspace = regen.PublicationWorkspace(project, Path(result["epub"]).parent.parent)
    old_epub = Path(result["epub"]).read_bytes()
    original = publication.verify_epub_archive
    def drift(*args, **kwargs):
        value = original(*args, **kwargs)
        (project.chapter_dir / "0001.md").write_bytes(b"external newer chapter revision")
        return value
    target = workspace.publication_final_dir / "epub/another.epub"
    with monkeypatch.context() as mp:
        mp.setattr(publication, "verify_epub_archive", drift)
        with pytest.raises(RuntimeError):
            if operation == "build": publication.build_publication_epub(workspace, output_path=target)
            else: publication.verify_existing_publication_epub(workspace)
    assert not target.exists() and Path(result["epub"]).read_bytes() == old_epub
    assert history.publication_freshness(project)["status"] == "STALE"


def test_aut2_report_checksum_cannot_resolve_internal_contradiction(novel):
    project, _, report_hash, _ = novel
    report = history.read_object(project.audit_dir / "whole_history" / (report_hash + ".json"))
    report["chapters"][0]["decisions"][0]["outcome"] = "deferred"
    changed_hash = canonical_hash(report)
    write(project.audit_dir / "whole_history" / (changed_hash + ".json"), report)
    before = snapshot(project.root)
    with pytest.raises(RuntimeError, match="CONTRADICTORY_WHOLE_HISTORY"):
        regen.source_snapshot(project, changed_hash)
    assert snapshot(project.root) == before

@pytest.mark.parametrize("name", ["translation_addendum.txt", "source_overrides.json"])
def test_aut2_novel_policy_drift_invalidates_source_proof(novel, name):
    project, _, _, _ = novel
    result = regen.regenerate(project)
    before = snapshot(Path(result["epub"]).parent.parent)
    path = project.root / name
    if path.suffix == ".json": write(path, {"new_policy": True})
    else: path.write_bytes(b"New novel-specific translation policy")
    assert history.publication_freshness(project)["status"] == "STALE"
    with pytest.raises(RuntimeError, match="SOURCE_APPROVAL_STALE"): regen.regenerate(project)
    assert snapshot(Path(result["epub"]).parent.parent) == before

@pytest.mark.parametrize("stage", ["title_plan", "editorial_authorization"])
def test_aut2_missing_approval_cli_identifies_prerequisite_file(novel, monkeypatch, capsys, stage):
    project, _, _, _ = novel
    missing = project.root / regen.APPROVALS[stage]
    missing.unlink()
    before = snapshot(project.root)
    monkeypatch.setattr(regen, "select_project", lambda _: project)
    capsys.readouterr()
    assert regen.main(["--novel", project.config.id]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "STALE" and result["blocker"]["file"] == missing.name
    assert snapshot(project.root) == before
