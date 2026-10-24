"""Build and verify a complete final-publication EPUB with zero API calls.

Publication text and inventory come only from ``final/editorial_chapters``
and its successful ``final_editorial_apply.json`` integrity contract. Runtime
translation progress and earlier corpora supply only source-provenance checks;
EPUB text still comes exclusively from the editorial corpus.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from ebooklib import epub

from build_checkpoint_epub import split_blocks
from build_publication_chapters import (
    GEN_END,
    GEN_START,
    framed_corpus_fingerprint,
    split_physical_lines,
)
from novel_project import NovelProject, select_project
from noveltranslator_core import atomic_replace_prepared_file
from production_run_lock import ProductionRunLockedError, production_run_lock
from production_failure_classification import ProductionIntegrityError


SCHEMA_VERSION = 1
REVISION = "publication-epub-v1.2"
SUPPORTED_EDITORIAL_APPLY_SCHEMA = 1
SUPPORTED_EDITORIAL_APPLY_REVISION = "final-editorial-apply-v1.2"
FINGERPRINT_ALGORITHM = "sha256-filename-length-bytes-v1"
MIMETYPE = b"application/epub+zip"
EPUB_NS = "http://www.idpf.org/2007/ops"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

NUMBERED_NAME_RE = re.compile(r"^(\d{4})\.md$", re.IGNORECASE)
CHAPTER_HEADING_RE = re.compile(r"^Chapter (\d+)(.*)$")
NAMED_HEADING_RE = re.compile(r"^(?:Prologue(?: .+)?|Epilogue(?: .+)?|Side Story(?: .+)?)$")
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+")
UNIT_MEMBER_RE = re.compile(r"^(?:.*/)?text/unit_(\d{4})\.xhtml$")
SPLIT_NAVIGATION_SEPARATOR = " — "

# Publication-only scene-break syntax. The first four forms are the known
# reader-visible variants. The remaining forms occur in the verified Rogue
# corpus and were already treated as separators by the V1 renderer.
PUBLICATION_SCENE_BREAK_FORMS = frozenset(
    {"* * *", "**", "*", r"\*\*\*", "***", "* *", "---"}
)
PUBLICATION_SCENE_BREAK_EQUALS_RE = re.compile(r"={3,}")

# Delimiters are deliberately conservative: no newlines, delimiter nesting,
# whitespace immediately inside a delimiter, or intraword matches.
INLINE_EMPHASIS_RE = re.compile(
    r"(?<![\\\w*])\*\*(?=\S)(?P<strong>[^*\r\n]*?[^\\*\s])\*\*(?![\w*])"
    r"|(?<![\\\w*])\*(?=\S)(?P<star>[^*\r\n]*?[^\\*\s])\*(?![\w*])"
    r"|(?<![\\\w_])_(?=\S)(?P<underscore>[^_\r\n]*?[^\\_\s])_(?![\w_])"
)
CHECKPOINT_RANGE_RE = re.compile(r"[—-][ \t]*Chapters[ \t]+\d+[–-]\d+", re.IGNORECASE)


class PublicationEpubError(ValueError):
    """A fail-closed publication corpus, EPUB, or transaction error."""


@dataclass(frozen=True)
class PublicationUnit:
    unit: int
    path: Path
    source_bytes: bytes
    heading_lines: tuple[str, ...]
    navigation_title: str
    body_text: str


@dataclass(frozen=True)
class PublicationCorpus:
    project: NovelProject
    source_dir: Path
    editorial_apply_report_path: Path
    editorial_apply_report: dict[str, Any]
    fingerprint: dict[str, Any]
    units: tuple[PublicationUnit, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publication_paths(project: NovelProject) -> dict[str, Path]:
    final_dir = getattr(project, "publication_final_dir", project.root / "final")
    return {
        "source_dir": final_dir / "editorial_chapters",
        "editorial_apply_report": final_dir / "reports" / "final_editorial_apply.json",
        "output": final_dir / "epub" / f"{project.config.id}.epub",
        "report_json": final_dir / "reports" / "publication_epub.json",
        "report_md": final_dir / "reports" / "publication_epub.md",
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PublicationEpubError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationEpubError(f"malformed {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationEpubError(f"{label} must contain a JSON object")
    return value


def _require_exact(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise PublicationEpubError(
            f"{label} mismatch: expected {expected!r}, found {actual!r}"
        )


def _validate_editorial_apply_report(
    report: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    _require_exact(
        report.get("schema_version"),
        SUPPORTED_EDITORIAL_APPLY_SCHEMA,
        "editorial apply schema",
    )
    _require_exact(
        report.get("revision"),
        SUPPORTED_EDITORIAL_APPLY_REVISION,
        "editorial apply revision",
    )
    _require_exact(report.get("status"), "success", "editorial apply status")
    _require_exact(report.get("ready"), True, "editorial apply ready flag")
    _require_exact(report.get("success"), True, "editorial apply success flag")
    _require_exact(report.get("blocker_count"), 0, "editorial apply blocker count")

    fingerprint = report.get("output_corpus_fingerprint")
    if not isinstance(fingerprint, dict):
        raise PublicationEpubError("editorial apply report lacks an output corpus fingerprint")
    _require_exact(
        fingerprint.get("algorithm"), FINGERPRINT_ALGORITHM,
        "editorial apply fingerprint algorithm",
    )
    file_count = fingerprint.get("file_count")
    byte_count = fingerprint.get("aggregate_byte_count")
    aggregate = fingerprint.get("aggregate_sha256")
    files = fingerprint.get("files")
    if not isinstance(file_count, int) or isinstance(file_count, bool) or file_count < 1:
        raise PublicationEpubError("editorial apply fingerprint file_count must be positive")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 1:
        raise PublicationEpubError("editorial apply fingerprint aggregate_byte_count must be positive")
    if not isinstance(aggregate, str) or not re.fullmatch(r"[0-9a-f]{64}", aggregate):
        raise PublicationEpubError("editorial apply fingerprint aggregate_sha256 is invalid")
    if not isinstance(files, list) or len(files) != file_count:
        raise PublicationEpubError("editorial apply fingerprint files do not match file_count")
    affected = report.get("affected_unit_count")
    unaffected = report.get("unaffected_unit_count")
    if not isinstance(affected, int) or not isinstance(unaffected, int):
        raise PublicationEpubError("editorial apply affected/unaffected counts are invalid")
    _require_exact(affected + unaffected, file_count, "editorial apply total file count")

    expected_names = [f"{unit:04d}.md" for unit in range(1, file_count + 1)]
    for unit, (record, expected_name) in enumerate(zip(files, expected_names), 1):
        if not isinstance(record, dict):
            raise PublicationEpubError(f"editorial fingerprint file record {unit} is malformed")
        _require_exact(record.get("unit"), unit, f"editorial file record {unit} unit")
        _require_exact(record.get("file"), expected_name, f"editorial file record {unit} name")
        if (
            not isinstance(record.get("byte_count"), int)
            or isinstance(record.get("byte_count"), bool)
            or record["byte_count"] < 1
        ):
            raise PublicationEpubError(f"editorial file record {unit} byte_count is invalid")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", record["sha256"]
        ):
            raise PublicationEpubError(f"editorial file record {unit} SHA-256 is invalid")
    return fingerprint, expected_names


def _validate_source_entries(source_dir: Path, expected_names: list[str]) -> list[Path]:
    if not source_dir.is_dir():
        raise PublicationEpubError(
            f"editorial_chapters directory does not exist: {source_dir}"
        )
    entries = sorted(source_dir.iterdir(), key=lambda path: path.name.casefold())
    logical_units: dict[int, str] = {}
    invalid: list[str] = []
    for path in entries:
        match = NUMBERED_NAME_RE.fullmatch(path.name)
        if not match or not path.is_file() or path.is_symlink():
            invalid.append(path.name)
            continue
        unit = int(match.group(1))
        if unit in logical_units:
            raise PublicationEpubError(
                f"duplicate logical publication unit {unit}: "
                f"{logical_units[unit]!r}, {path.name!r}"
            )
        logical_units[unit] = path.name
    if invalid:
        raise PublicationEpubError(f"unexpected publication corpus entries: {invalid}")
    actual_names = [path.name for path in entries]
    if actual_names != expected_names:
        missing = sorted(set(expected_names) - set(actual_names))
        extra = sorted(set(actual_names) - set(expected_names))
        raise PublicationEpubError(
            "editorial inventory does not match final editorial apply report: "
            f"missing={missing}, unexpected={extra}"
        )
    return [source_dir / name for name in expected_names]


def _has_title_wrapper_leftover(text: str) -> bool:
    stripped = text.strip()
    return bool(
        MARKDOWN_HEADING_RE.match(stripped)
        or (len(stripped) >= 4 and stripped.startswith("**") and stripped.endswith("**"))
        or (len(stripped) >= 2 and stripped.startswith("<") and stripped.endswith(">"))
        or re.search(r"<[^<>]*>[ \t]*$", stripped)
        or GEN_START in stripped
        or GEN_END in stripped
        or re.search(r"<br\s*/?>", stripped, flags=re.IGNORECASE)
    )


def parse_publication_unit(unit: int, path: Path, data: bytes) -> PublicationUnit:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationEpubError(f"unit {unit} is not valid UTF-8: {exc}") from exc
    lines = split_physical_lines(data)
    if len(lines) < 3 or lines[0].blank:
        raise PublicationEpubError(f"unit {unit} lacks a complete publication heading")
    first = lines[0].text
    if _has_title_wrapper_leftover(first):
        raise PublicationEpubError(f"unit {unit} contains a technical heading wrapper")

    chapter = CHAPTER_HEADING_RE.fullmatch(first)
    if chapter:
        displayed = int(chapter.group(1))
        if displayed != unit:
            raise PublicationEpubError(
                f"unit {unit} displays mismatched Chapter number {displayed}"
            )
        suffix = chapter.group(2)
        if suffix == "":
            if len(lines) < 4 or lines[1].blank or not lines[2].blank:
                raise PublicationEpubError(f"unit {unit} has a malformed split heading")
            second = lines[1].text
            if _has_title_wrapper_leftover(second):
                raise PublicationEpubError(
                    f"unit {unit} split title contains a technical wrapper"
                )
            heading_lines = (first, second)
            body_index = 3
            navigation_title = first + SPLIT_NAVIGATION_SEPARATOR + second
        else:
            if not (
                suffix.startswith(".")
                or suffix.startswith(":")
                or suffix.startswith(" —")
                or suffix.startswith(" -")
            ):
                raise PublicationEpubError(
                    f"unit {unit} has unsupported Chapter heading punctuation: {first!r}"
                )
            if not suffix.strip(" .:—-"):
                raise PublicationEpubError(f"unit {unit} Chapter heading lacks a title")
            if not lines[1].blank:
                raise PublicationEpubError(f"unit {unit} heading lacks a blank separator")
            heading_lines = (first,)
            body_index = 2
            navigation_title = first
    elif NAMED_HEADING_RE.fullmatch(first):
        if not lines[1].blank:
            raise PublicationEpubError(f"unit {unit} named heading lacks a blank separator")
        heading_lines = (first,)
        body_index = 2
        navigation_title = first
    else:
        raise PublicationEpubError(f"unit {unit} has an unknown heading structure: {first!r}")

    if body_index >= len(lines) or lines[body_index].blank:
        raise PublicationEpubError(f"unit {unit} has an empty body or excess heading spacing")
    body_offset = sum(len(line.raw) for line in lines[:body_index])
    try:
        body_text = data[body_offset:].decode("utf-8")
    except UnicodeDecodeError as exc:  # Defensive: the full file was already checked.
        raise PublicationEpubError(f"unit {unit} body is not valid UTF-8") from exc
    return PublicationUnit(
        unit=unit,
        path=path,
        source_bytes=data,
        heading_lines=heading_lines,
        navigation_title=navigation_title,
        body_text=body_text,
    )


def load_publication_corpus(project: NovelProject) -> PublicationCorpus:
    paths = publication_paths(project)
    report = _load_json_object(
        paths["editorial_apply_report"], "final editorial apply report"
    )
    expected_fingerprint, expected_names = _validate_editorial_apply_report(report)
    source_paths = _validate_source_entries(paths["source_dir"], expected_names)
    payloads = [(path.name, path.read_bytes()) for path in source_paths]
    actual_fingerprint = framed_corpus_fingerprint(payloads)
    if actual_fingerprint != expected_fingerprint:
        raise PublicationEpubError(
            "editorial corpus fingerprint does not match final_editorial_apply.json"
        )
    units = tuple(
        parse_publication_unit(unit, path, data)
        for unit, (path, (_, data)) in enumerate(zip(source_paths, payloads), 1)
    )
    return PublicationCorpus(
        project=project,
        source_dir=paths["source_dir"],
        editorial_apply_report_path=paths["editorial_apply_report"],
        editorial_apply_report=report,
        fingerprint=actual_fingerprint,
        units=units,
    )


def is_publication_scene_break(block: str) -> bool:
    """Return true only for an allowlisted standalone publication separator."""

    stripped = block.strip()
    if "\n" in stripped or "\r" in stripped:
        return False
    normalized_spaces = re.sub(r"[ \t]+", " ", stripped)
    return bool(
        normalized_spaces in PUBLICATION_SCENE_BREAK_FORMS
        or PUBLICATION_SCENE_BREAK_EQUALS_RE.fullmatch(normalized_spaces)
    )


def render_inline_emphasis(text: str) -> str:
    """Escape text and render only conservative matched emphasis delimiters."""

    rendered: list[str] = []
    offset = 0
    for match in INLINE_EMPHASIS_RE.finditer(text):
        rendered.append(escape(text[offset:match.start()], quote=False))
        if match.group("strong") is not None:
            tag = "strong"
            content = match.group("strong")
        elif match.group("star") is not None:
            tag = "em"
            content = match.group("star")
        else:
            tag = "em"
            content = match.group("underscore")
        rendered.append(f"<{tag}>{escape(content, quote=False)}</{tag}>")
        offset = match.end()
    rendered.append(escape(text[offset:], quote=False))
    return "".join(rendered)


def _render_body(body_text: str) -> str:
    parts: list[str] = []
    for block in split_blocks(body_text):
        if is_publication_scene_break(block):
            parts.append('<p class="scene-break">* * *</p>')
            continue
        safe = "<br/>".join(render_inline_emphasis(line) for line in block.split("\n"))
        parts.append(f"<p>{safe}</p>")
    if not parts:
        raise PublicationEpubError("publication unit body rendered empty")
    return "\n".join(parts)


def _publication_css() -> str:
    return """
body {
    font-family: serif;
    line-height: 1.5;
    margin: 5%;
}
h1 {
    text-align: center;
    margin-top: 1.2em;
    margin-bottom: 1.5em;
}
.split-heading .chapter-label,
.split-heading .chapter-subtitle {
    display: block;
}
.split-heading .chapter-subtitle {
    margin-top: 0.35em;
}
p {
    text-indent: 1.5em;
    margin-top: 0;
    margin-bottom: 0.65em;
}
.scene-break {
    text-indent: 0;
    text-align: center;
    margin-top: 1.4em;
    margin-bottom: 1.4em;
}
.title-page, .author {
    text-indent: 0;
    text-align: center;
}
""".strip()


def _render_unit_heading(unit: PublicationUnit) -> str:
    if len(unit.heading_lines) == 2:
        label, subtitle = unit.heading_lines
        return (
            '<h1 class="split-heading">'
            f'<span class="chapter-label">{escape(label)}</span>'
            f'<span class="chapter-subtitle">{escape(subtitle)}</span>'
            "</h1>"
        )
    return f"<h1>{escape(unit.heading_lines[0])}</h1>"


def create_epub_book(corpus: PublicationCorpus) -> epub.EpubBook:
    project = corpus.project
    config = project.config
    book = epub.EpubBook()
    book.set_identifier(
        f"noveltranslator-publication-{config.id}-"
        f"{corpus.fingerprint['aggregate_sha256'][:16]}"
    )
    book.set_title(config.title)
    book.set_language(config.target_language)
    book.add_metadata("DC", "source", config.source_language)
    if config.author:
        book.add_author(config.author)

    style = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=_publication_css(),
    )
    book.add_item(style)

    title_page = epub.EpubHtml(
        uid="title_page",
        title=config.title,
        file_name="title.xhtml",
        lang=config.target_language,
    )
    title_content = [f'<h1 class="title-page">{escape(config.title)}</h1>']
    if config.author:
        title_content.append(f'<p class="author">{escape(config.author)}</p>')
    title_page.content = "\n".join(title_content)
    title_page.add_item(style)
    book.add_item(title_page)

    content_items: list[epub.EpubHtml] = []
    for unit in corpus.units:
        item = epub.EpubHtml(
            uid=f"unit_{unit.unit:04d}",
            title=unit.navigation_title,
            file_name=f"text/unit_{unit.unit:04d}.xhtml",
            lang=config.target_language,
        )
        item.content = (
            _render_unit_heading(unit)
            + "\n"
            + _render_body(unit.body_text)
        )
        item.add_link(href="../style/main.css", rel="stylesheet", type="text/css")
        book.add_item(item)
        content_items.append(item)

    book.toc = tuple(content_items)
    book.spine = [title_page, *content_items]
    book.add_item(epub.EpubNcx(uid="ncx"))
    book.add_item(epub.EpubNav(uid="nav", file_name="nav.xhtml"))
    return book


def _xml(data: bytes, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise PublicationEpubError(f"invalid {label} XML: {exc}") from exc


def _resolve_member(base_member: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_member), href))


def _element_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_broken_internal_resource_references(
    archive: zipfile.ZipFile,
    xhtml_members: list[str],
) -> list[dict[str, str]]:
    """Resolve local XHTML href/src/data/poster targets against ZIP members."""

    archive_names = set(archive.namelist())
    broken: list[dict[str, str]] = []
    for member in xhtml_members:
        if member not in archive_names:
            broken.append({"document": member, "attribute": "document", "reference": ""})
            continue
        document = _xml(archive.read(member), f"resource document {member}")
        for element in document.iter():
            for attribute, reference in element.attrib.items():
                if _local_name(attribute) not in {"href", "src", "data", "poster"}:
                    continue
                parsed = urlsplit(reference)
                if parsed.scheme or reference.startswith("//") or not parsed.path:
                    continue
                target = _resolve_member(member, unquote(parsed.path))
                if target not in archive_names:
                    broken.append(
                        {
                            "document": member,
                            "attribute": _local_name(attribute),
                            "reference": reference,
                        }
                    )
    return broken


def _unformatted_text_segments(element: ElementTree.Element):
    """Yield visible text outside generated em/strong elements."""

    if element.text:
        yield element.text
    for child in element:
        if _local_name(child.tag) not in {"em", "strong"}:
            yield from _unformatted_text_segments(child)
        if child.tail:
            yield child.tail


def reader_visible_markdown_artifacts(document: ElementTree.Element) -> list[str]:
    """Classify recognized source markup left visible in generated XHTML."""

    artifacts: list[str] = []
    for paragraph in document.findall(".//{*}p"):
        classes = paragraph.get("class", "").split()
        if "scene-break" in classes:
            continue
        visible = _element_text(paragraph)
        if is_publication_scene_break(visible):
            artifacts.append("unrendered-scene-break")
            continue
        if any(INLINE_EMPHASIS_RE.search(segment) for segment in _unformatted_text_segments(paragraph)):
            artifacts.append("unrendered-emphasis")
    return artifacts


def _source_body_structure(body_text: str) -> list[tuple[str, Any]]:
    """Derive the body contract from editorial text, without calling the renderer.

    Share the supported source syntax, but never trust generated XHTML as the
    expected value. Text (including whitespace), style, and break positions are
    compared separately; escaping and document serialization are irrelevant.
    """

    blocks: list[tuple[str, Any]] = []
    for block in split_blocks(body_text):
        if is_publication_scene_break(block):
            blocks.append(("scene-break", "* * *"))
            continue
        tokens: list[tuple[str, str]] = []
        for index, line in enumerate(block.split("\n")):
            if index:
                tokens.append(("br", ""))
            offset = 0
            for match in INLINE_EMPHASIS_RE.finditer(line):
                if match.start() > offset:
                    tokens.append(("text", line[offset:match.start()]))
                if match.group("strong") is not None:
                    tokens.append(("strong", match.group("strong")))
                else:
                    tokens.append(("em", match.group("star") or match.group("underscore")))
                offset = match.end()
            if offset < len(line):
                tokens.append(("text", line[offset:]))
        blocks.append(("p", tokens))
    return blocks


def _verify_unit_body(document: ElementTree.Element, unit: PublicationUnit) -> None:
    """Account for the entire XHTML body, allowing neutral block wrappers only."""

    def fail() -> None:
        # Do not expose approved or unexpected novel prose in diagnostics.
        raise PublicationEpubError(
            f"unit {unit.unit} XHTML body differs from publication text"
        )

    namespace = "{http://www.w3.org/1999/xhtml}"
    bodies = document.findall(".//{*}body")
    if len(bodies) != 1 or bodies[0].tag != namespace + "body":
        fail()
    blocks: list[tuple[str, Any]] = []
    heading_seen = False

    def visit(container: ElementTree.Element) -> None:
        nonlocal heading_seen
        # Only XML indentation is ignorable outside paragraphs, not NBSP, etc.
        if (container.text or "").strip(" \t\r\n"):
            fail()
        for child in container:
            if child.tag in {namespace + "div", namespace + "section"}:
                visit(child)
            elif child.tag == namespace + "h1":
                if heading_seen or blocks:
                    fail()
                heading_seen = True  # Heading content is checked separately.
            elif child.tag == namespace + "p":
                if not heading_seen:
                    fail()
                if "scene-break" in child.get("class", "").split():
                    if len(child):
                        fail()
                    blocks.append(("scene-break", child.text or ""))
                else:
                    tokens: list[tuple[str, str]] = []
                    if child.text:
                        tokens.append(("text", child.text))
                    for inline in child:
                        if len(inline):
                            fail()
                        if inline.tag == namespace + "br":
                            if inline.text:
                                fail()
                            tokens.append(("br", ""))
                        elif inline.tag in {namespace + "em", namespace + "strong"}:
                            tokens.append((_local_name(inline.tag), inline.text or ""))
                        else:
                            fail()
                        if inline.tail:
                            tokens.append(("text", inline.tail))
                    blocks.append(("p", tokens))
            else:
                fail()
            if (child.tail or "").strip(" \t\r\n"):
                fail()

    visit(bodies[0])
    expected = _source_body_structure(unit.body_text)
    if not heading_seen or not expected or blocks != expected:
        fail()


def verify_epub_archive(
    epub_path: Path,
    corpus: PublicationCorpus,
) -> dict[str, Any]:
    expected_units = [unit.unit for unit in corpus.units]
    expected_titles = [unit.navigation_title for unit in corpus.units]
    expected_ids = [f"unit_{unit:04d}" for unit in expected_units]
    expected_hrefs = [f"text/unit_{unit:04d}.xhtml" for unit in expected_units]
    checks: dict[str, bool] = {}
    try:
        archive = zipfile.ZipFile(epub_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PublicationEpubError(f"EPUB is not a valid ZIP container: {exc}") from exc
    with archive:
        if archive.testzip() is not None:
            raise PublicationEpubError("EPUB ZIP member CRC validation failed")
        checks["valid_zip_container"] = True
        names = archive.namelist()
        if not names or names[0] != "mimetype":
            raise PublicationEpubError("mimetype must be the first EPUB ZIP member")
        mimetype_info = archive.getinfo("mimetype")
        if mimetype_info.compress_type != zipfile.ZIP_STORED:
            raise PublicationEpubError("mimetype must be stored without compression")
        if archive.read("mimetype") != MIMETYPE:
            raise PublicationEpubError("mimetype content is not exactly application/epub+zip")
        checks["mimetype_first_exact_stored"] = True

        if "META-INF/container.xml" not in names:
            raise PublicationEpubError("META-INF/container.xml is missing")
        container = _xml(archive.read("META-INF/container.xml"), "container")
        rootfiles = container.findall(f".//{{{CONTAINER_NS}}}rootfile")
        if len(rootfiles) != 1 or not rootfiles[0].get("full-path"):
            raise PublicationEpubError("container.xml must identify exactly one package document")
        opf_member = rootfiles[0].get("full-path")
        if opf_member not in names:
            raise PublicationEpubError("OPF package document identified by container.xml is missing")
        checks["container_and_opf_present"] = True

        opf = _xml(archive.read(opf_member), "OPF package")
        manifest_items = opf.findall(".//{*}manifest/{*}item")
        manifest: dict[str, dict[str, str]] = {}
        for item in manifest_items:
            item_id = item.get("id")
            href = item.get("href")
            if not item_id or not href or item_id in manifest:
                raise PublicationEpubError("OPF manifest contains a missing or duplicate item id")
            manifest[item_id] = dict(item.attrib)
        for item_id in expected_ids:
            if item_id not in manifest:
                raise PublicationEpubError(f"publication content manifest item is missing: {item_id}")
        actual_hrefs = [manifest[item_id]["href"] for item_id in expected_ids]
        _require_exact(actual_hrefs, expected_hrefs, "publication content manifest order")
        actual_members = [_resolve_member(opf_member, href) for href in actual_hrefs]
        zip_unit_members = sorted(name for name in names if UNIT_MEMBER_RE.fullmatch(name))
        _require_exact(zip_unit_members, actual_members, "publication content ZIP members")
        checks["publication_content_documents_exact"] = True

        nav_ids = [
            item_id for item_id, item in manifest.items()
            if "nav" in item.get("properties", "").split()
        ]
        if len(nav_ids) != 1:
            raise PublicationEpubError("OPF must identify exactly one navigation document")
        nav_id = nav_ids[0]
        nav_member = _resolve_member(opf_member, manifest[nav_ids[0]]["href"])
        if nav_member not in names:
            raise PublicationEpubError("navigation document is missing")
        checks["navigation_document_present"] = True

        ncx_ids = [
            item_id for item_id, item in manifest.items()
            if item.get("media-type") == "application/x-dtbncx+xml"
        ]
        if len(ncx_ids) != 1:
            raise PublicationEpubError("EPUB builder must include exactly one NCX document")
        ncx_member = _resolve_member(opf_member, manifest[ncx_ids[0]]["href"])
        if ncx_member not in names:
            raise PublicationEpubError("NCX document is missing")
        checks["ncx_present"] = True

        xhtml_members = [
            _resolve_member(opf_member, item["href"])
            for item in manifest.values()
            if item.get("media-type") == "application/xhtml+xml"
        ]
        broken_references = find_broken_internal_resource_references(
            archive, xhtml_members
        )
        if broken_references:
            first = broken_references[0]
            raise PublicationEpubError(
                "broken internal XHTML resource reference: "
                f"{first['document']} {first['attribute']}={first['reference']!r}"
            )
        checks["all_internal_xhtml_resources_resolve"] = True

        title_ids = [
            item_id for item_id, item in manifest.items() if item.get("href") == "title.xhtml"
        ]
        if len(title_ids) != 1:
            raise PublicationEpubError("final title page manifest item is missing or ambiguous")
        title_id = title_ids[0]
        spine_itemrefs = opf.findall(".//{*}spine/{*}itemref")
        spine_ids = [item.get("idref") for item in spine_itemrefs]
        unit_spine_ids = [item_id for item_id in spine_ids if item_id in set(expected_ids)]
        _require_exact(
            spine_ids,
            [title_id, *expected_ids],
            "publication spine with title page first",
        )
        _require_exact(unit_spine_ids, expected_ids, "publication unit spine")
        if len(spine_ids) != len(set(spine_ids)):
            raise PublicationEpubError("OPF spine contains duplicate item references")
        if any(item_id not in manifest for item_id in spine_ids):
            raise PublicationEpubError("OPF spine references an unknown manifest item")
        if nav_id in spine_ids:
            raise PublicationEpubError(
                "EPUB3 NAV document must not be a normal linear spine item"
            )
        if any(item.get("linear") == "no" for item in spine_itemrefs):
            raise PublicationEpubError(
                "publication spine contains a non-linear compatibility item"
            )
        checks["spine_exact_once_in_order"] = True
        checks["title_page_first_linear_spine_item"] = True
        checks["navigation_document_absent_from_spine"] = True

        nav = _xml(archive.read(nav_member), "navigation document")
        toc_navs = [
            item for item in nav.findall(".//{*}nav")
            if "toc" in item.get(f"{{{EPUB_NS}}}type", "").split()
        ]
        if len(toc_navs) != 1:
            raise PublicationEpubError("navigation document must contain exactly one TOC nav")
        anchors = toc_navs[0].findall(".//{*}a")
        nav_members = [_resolve_member(nav_member, anchor.get("href", "")) for anchor in anchors]
        nav_titles = [_element_text(anchor) for anchor in anchors]
        _require_exact(nav_members, actual_members, "navigation TOC targets")
        _require_exact(nav_titles, expected_titles, "navigation TOC titles")
        checks["navigation_toc_exact_once_in_order"] = True

        ncx = _xml(archive.read(ncx_member), "NCX")
        nav_points = ncx.findall(".//{*}navPoint")
        ncx_members = [
            _resolve_member(ncx_member, point.find("{*}content").get("src", ""))
            for point in nav_points
            if point.find("{*}content") is not None
        ]
        ncx_titles = [
            _element_text(point.find("{*}navLabel"))
            for point in nav_points
            if point.find("{*}navLabel") is not None
        ]
        _require_exact(ncx_members, actual_members, "NCX TOC targets")
        _require_exact(ncx_titles, expected_titles, "NCX TOC titles")
        checks["ncx_toc_exact_once_in_order"] = True

        titles = [_element_text(item) for item in opf.findall(f".//{{{DC_NS}}}title")]
        languages = [_element_text(item) for item in opf.findall(f".//{{{DC_NS}}}language")]
        _require_exact(titles, [corpus.project.config.title], "EPUB metadata title")
        _require_exact(languages, [corpus.project.config.target_language], "EPUB metadata language")
        checks["metadata_title_exact"] = True
        checks["metadata_language_exact"] = True
        if CHECKPOINT_RANGE_RE.search(titles[0]) or "Checkpoint" in titles[0]:
            raise PublicationEpubError("checkpoint title/range wording appears in final metadata")
        checks["no_checkpoint_title_or_range_suffix"] = True

        title_member = _resolve_member(opf_member, "title.xhtml")
        title_page = archive.read(title_member).decode("utf-8")
        if (
            "Generated locally from committed translation files." in title_page
            or CHECKPOINT_RANGE_RE.search(title_page)
            or ">Checkpoint<" in title_page
        ):
            raise PublicationEpubError("checkpoint-only wording appears on final title page")
        checks["no_checkpoint_title_page_wording"] = True

        scene_break_count = 0
        emphasis_element_count = 0
        strong_element_count = 0
        split_heading_units: list[int] = []
        markdown_artifacts: list[tuple[int, str]] = []
        for unit, member, expected_title in zip(corpus.units, actual_members, expected_titles):
            document = _xml(archive.read(member), f"unit {unit.unit} XHTML")
            headings = document.findall(".//{*}h1")
            if len(headings) != 1:
                raise PublicationEpubError(
                    f"unit {unit.unit} XHTML must contain exactly one heading"
                )
            heading = headings[0]
            if len(unit.heading_lines) == 2:
                spans = heading.findall("./{*}span")
                span_classes = [span.get("class") for span in spans]
                span_texts = [_element_text(span) for span in spans]
                if (
                    heading.get("class") != "split-heading"
                    or span_classes != ["chapter-label", "chapter-subtitle"]
                    or span_texts != list(unit.heading_lines)
                ):
                    raise PublicationEpubError(
                        f"unit {unit.unit} split XHTML heading differs from publication text"
                    )
                split_heading_units.append(unit.unit)
            elif _element_text(heading) != expected_title:
                raise PublicationEpubError(
                    f"unit {unit.unit} XHTML heading differs from publication text"
                )
            if any(_has_title_wrapper_leftover(part) for part in unit.heading_lines):
                raise PublicationEpubError(
                    f"unit {unit.unit} XHTML heading contains a technical wrapper"
                )

            _verify_unit_body(document, unit)
            scene_break_count += sum(
                "scene-break" in paragraph.get("class", "").split()
                for paragraph in document.findall(".//{*}p")
            )
            emphasis_element_count += len(document.findall(".//{*}em"))
            strong_element_count += len(document.findall(".//{*}strong"))
            markdown_artifacts.extend(
                (unit.unit, artifact)
                for artifact in reader_visible_markdown_artifacts(document)
            )
        checks["publication_bodies_match_editorial"] = True
        checks["no_technical_title_wrappers_introduced"] = True
        if markdown_artifacts:
            unit, artifact = markdown_artifacts[0]
            raise PublicationEpubError(
                f"unit {unit} contains reader-visible recognized Markdown artifact: {artifact}"
            )
        checks["reader_visible_markdown_artifacts_absent"] = True
        checks["split_headings_preserve_source_components"] = True
        checks["first_and_final_units_present"] = bool(
            actual_members and actual_members[0].endswith("unit_0001.xhtml")
            and actual_members[-1].endswith(f"unit_{expected_units[-1]:04d}.xhtml")
        )
        if not checks["first_and_final_units_present"]:
            raise PublicationEpubError("first or final publication unit is absent")

    return {
        "checks": checks,
        "content_document_count": len(expected_units),
        "spine_count": len(spine_ids),
        "unit_spine_count": len(unit_spine_ids),
        "first_spine_item": spine_ids[0],
        "navigation_document_in_spine": nav_id in spine_ids,
        "toc_count": len(nav_members),
        "ncx_toc_count": len(ncx_members),
        "metadata_title": titles[0],
        "metadata_language": languages[0],
        "navigation_titles": nav_titles,
        "content_members": actual_members,
        "zip_member_count": len(names),
        "broken_internal_resource_reference_count": len(broken_references),
        "scene_break_element_count": scene_break_count,
        "emphasis_element_count": emphasis_element_count,
        "strong_element_count": strong_element_count,
        "split_heading_document_count": len(split_heading_units),
        "split_heading_units": split_heading_units,
        "reader_visible_markdown_artifact_count": len(markdown_artifacts),
    }


def _relative_output(path: Path, project: NovelProject) -> str:
    try:
        return path.resolve().relative_to(project.repository_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_epub_report(
    corpus: PublicationCorpus,
    output_path: Path,
    epub_bytes: bytes,
    validation: dict[str, Any],
    source_after: dict[str, Any],
) -> dict[str, Any]:
    source_unchanged = corpus.fingerprint == source_after
    if not source_unchanged:
        raise PublicationEpubError("publication source fingerprint changed during EPUB build")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": REVISION,
        "novel_id": corpus.project.config.id,
        "book_title": corpus.project.config.title,
        "input_editorial_fingerprint": corpus.fingerprint,
        "input_file_count": len(corpus.units),
        "epub_output_path": _relative_output(output_path, corpus.project),
        "epub_sha256": sha256_bytes(epub_bytes),
        "epub_byte_size": len(epub_bytes),
        "content_document_count": validation["content_document_count"],
        "spine_count": validation["spine_count"],
        "unit_spine_count": validation["unit_spine_count"],
        "first_spine_item": validation["first_spine_item"],
        "navigation_document_in_spine": validation["navigation_document_in_spine"],
        "toc_count": validation["toc_count"],
        "ncx_toc_count": validation["ncx_toc_count"],
        "zip_member_count": validation["zip_member_count"],
        "broken_internal_resource_reference_count": validation[
            "broken_internal_resource_reference_count"
        ],
        "scene_break_element_count": validation["scene_break_element_count"],
        "emphasis_element_count": validation["emphasis_element_count"],
        "strong_element_count": validation["strong_element_count"],
        "split_heading_document_count": validation["split_heading_document_count"],
        "split_heading_units": validation["split_heading_units"],
        "reader_visible_markdown_artifact_count": validation[
            "reader_visible_markdown_artifact_count"
        ],
        "metadata_title": validation["metadata_title"],
        "metadata_language": validation["metadata_language"],
        "validation_checks": validation["checks"],
        "source_fingerprint_after_build": source_after,
        "source_unchanged": source_unchanged,
        "blocker_count": 0,
        "success": True,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Final Publication EPUB Report",
        "",
        f"- Schema/revision: `{report['schema_version']}` / `{report['revision']}`",
        f"- Novel: `{report['novel_id']}`",
        f"- Book title: {report['book_title']}",
        f"- Input files: `{report['input_file_count']}`",
        f"- Input fingerprint: `{report['input_editorial_fingerprint']['aggregate_sha256']}`",
        f"- EPUB: `{report['epub_output_path']}`",
        f"- EPUB SHA-256: `{report['epub_sha256']}`",
        f"- EPUB byte size: `{report['epub_byte_size']}`",
        f"- Content/spine/TOC/NCX: `{report['content_document_count']}` / `{report['spine_count']}` / `{report['toc_count']}` / `{report['ncx_toc_count']}`",
        f"- ZIP members: `{report['zip_member_count']}`",
        f"- Broken internal resource references: `{report['broken_internal_resource_reference_count']}`",
        f"- Scene breaks / em / strong: `{report['scene_break_element_count']}` / `{report['emphasis_element_count']}` / `{report['strong_element_count']}`",
        f"- Split-heading documents: `{report['split_heading_document_count']}`",
        f"- Reader-visible recognized Markdown artifacts: `{report['reader_visible_markdown_artifact_count']}`",
        f"- Metadata title/language: `{report['metadata_title']}` / `{report['metadata_language']}`",
        f"- Source unchanged: `{report['source_unchanged']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Success: `{report['success']}`",
        "",
        "## Validation",
        "",
    ]
    for check, passed in report["validation_checks"].items():
        lines.append(f"- `{check}`: `{passed}`")
    return "\n".join(lines) + "\n"


def _refuse_existing(paths: list[Path]) -> None:
    existing = [path for path in paths if path.exists()]
    if existing:
        raise PublicationEpubError(
            "refusing to overwrite existing final publication artifact(s): "
            + ", ".join(str(path) for path in existing)
        )


def _recover_exact_interrupted_publication_prefix(
    corpus: PublicationCorpus,
    *,
    output_path: Path,
    report_json_path: Path,
    report_md_path: Path,
) -> bool:
    """Remove only an exact report-bound or validated temp-only pre-commit prefix."""
    if output_path.exists():
        raise PublicationEpubError(
            f"refusing to overwrite existing final publication artifact: {output_path}"
        )

    json_exists = report_json_path.exists()
    md_exists = report_md_path.exists()
    candidates = sorted(
        output_path.parent.glob(f".{output_path.name}.*.tmp"),
        key=lambda item: item.name.casefold(),
    )
    if not json_exists and not md_exists:
        if not candidates:
            return False
        if len(candidates) != 1:
            raise PublicationEpubError(
                "ambiguous interrupted publication temp-only prefix: "
                f"candidates={len(candidates)}"
            )
        candidate = candidates[0]
        if candidate.is_symlink() or not candidate.is_file():
            raise PublicationEpubError(
                "ambiguous interrupted publication temp-only prefix: "
                "candidate is not a regular file"
            )
        try:
            verify_epub_archive(candidate, corpus)
        except PublicationEpubError as exc:
            raise PublicationEpubError(
                "interrupted publication temp-only prefix is not a validated "
                "EPUB for the current publication corpus"
            ) from exc
        candidate.unlink()
        return True
    if md_exists and not json_exists:
        raise PublicationEpubError(
            "ambiguous interrupted publication prefix: Markdown report exists without JSON"
        )
    if not report_json_path.is_file() or report_json_path.is_symlink():
        raise PublicationEpubError(
            "ambiguous interrupted publication prefix: JSON report is not a regular file"
        )

    report = _load_json_object(report_json_path, "interrupted publication EPUB report")
    canonical_json = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if report_json_path.read_bytes() != canonical_json:
        raise PublicationEpubError(
            "interrupted publication JSON report is not canonical expected serialization"
        )

    if md_exists:
        if not report_md_path.is_file() or report_md_path.is_symlink():
            raise PublicationEpubError(
                "ambiguous interrupted publication prefix: Markdown report is not a regular file"
            )
        expected_md = render_markdown_report(report).encode("utf-8")
        if report_md_path.read_bytes() != expected_md:
            raise PublicationEpubError(
                "interrupted publication Markdown report does not match JSON report"
            )

    matching: list[Path] = []
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            validation = verify_epub_archive(candidate, corpus)
        except PublicationEpubError:
            continue
        epub_bytes = candidate.read_bytes()
        expected_report = build_epub_report(
            corpus,
            output_path,
            epub_bytes,
            validation,
            corpus.fingerprint,
        )
        if expected_report == report:
            matching.append(candidate)

    if len(matching) != 1:
        raise PublicationEpubError(
            "interrupted publication prefix cannot be bound to exactly one "
            f"validated temporary EPUB; matches={len(matching)}"
        )

    if md_exists:
        report_md_path.unlink()
    report_json_path.unlink()
    matching[0].unlink()
    return True


def build_publication_epub(
    project: NovelProject,
    *,
    output_path: Path | None = None,
    write_reports: bool | None = None,
    report_json_path: Path | None = None,
    report_md_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    from historical_reconciliation import require_publication_freshness
    require_publication_freshness(project)
    paths = publication_paths(project)
    output_path = Path(output_path or paths["output"])
    if write_reports is None:
        write_reports = output_path == paths["output"]
    report_json_path = Path(report_json_path or paths["report_json"])
    report_md_path = Path(report_md_path or paths["report_md"])
    targets = [output_path]
    if write_reports:
        targets.extend((report_json_path, report_md_path))
    _refuse_existing([output_path])

    corpus = load_publication_corpus(project)
    if write_reports:
        _recover_exact_interrupted_publication_prefix(
            corpus,
            output_path=output_path,
            report_json_path=report_json_path,
            report_md_path=report_md_path,
        )
        _refuse_existing(targets)

    output_parent_existed = output_path.parent.exists()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_parent_existed = report_json_path.parent.exists()
    if write_reports:
        report_json_path.parent.mkdir(parents=True, exist_ok=True)
        report_md_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    installed_output = False
    written_reports: list[Path] = []
    try:
        book = create_epub_book(corpus)
        epub.write_epub(str(temp_path), book, {})
        validation = verify_epub_archive(temp_path, corpus)
        source_after_temp = load_publication_corpus(project).fingerprint
        if source_after_temp != corpus.fingerprint:
            raise PublicationEpubError("publication source changed while EPUB was being built")
        epub_bytes = temp_path.read_bytes()
        report = build_epub_report(
            corpus, output_path, epub_bytes, validation, source_after_temp
        )

        _refuse_existing(targets)
        if write_reports:
            with report_json_path.open("xb") as handle:
                handle.write(
                    (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                )
            written_reports.append(report_json_path)
            with report_md_path.open("xb") as handle:
                handle.write(render_markdown_report(report).encode("utf-8"))
            written_reports.append(report_md_path)
        final_source = load_publication_corpus(project).fingerprint
        if final_source != corpus.fingerprint:
            raise PublicationEpubError("publication source changed before final EPUB commit")
        if output_path.exists():
            raise PublicationEpubError(f"final EPUB appeared during build: {output_path}")
        require_publication_freshness(project)
        atomic_replace_prepared_file(temp_path, output_path)
        installed_output = True
        return output_path, report
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        if installed_output and output_path.exists():
            output_path.unlink()
        for path in written_reports:
            path.unlink(missing_ok=True)
        if not output_parent_existed and output_path.parent.exists():
            try:
                output_path.parent.rmdir()
            except OSError:
                pass
        if write_reports and not report_parent_existed and report_json_path.parent.exists():
            try:
                report_json_path.parent.rmdir()
            except OSError:
                pass
        raise


def verify_existing_publication_epub(
    project: NovelProject, output_path: Path | None = None
) -> dict[str, Any]:
    from historical_reconciliation import require_publication_freshness
    require_publication_freshness(project)
    paths = publication_paths(project)
    output_path = Path(output_path or paths["output"])
    if not output_path.is_file():
        raise PublicationEpubError(f"final publication EPUB does not exist: {output_path}")
    corpus = load_publication_corpus(project)
    before = corpus.fingerprint
    validation = verify_epub_archive(output_path, corpus)
    after = load_publication_corpus(project).fingerprint
    if before != after:
        raise PublicationEpubError("publication source changed during EPUB verification")
    require_publication_freshness(project)
    return validation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True, help="novel identifier under novels/")
    parser.add_argument("--output", type=Path, help="custom EPUB output; suppresses final reports")
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        lock = nullcontext() if args.verify_only else production_run_lock(project.root)
        with lock:
            if args.verify_only:
                validation = verify_existing_publication_epub(project, args.output)
                result = {"status": "verified", **{key: validation[key] for key in (
                    "content_document_count", "spine_count", "toc_count", "metadata_title",
                    "metadata_language",
                )}}
            else:
                output, report = build_publication_epub(
                    project,
                    output_path=args.output,
                    write_reports=None,
                )
                result = {
                    "status": "success",
                    "output": str(output),
                    "epub_sha256": report["epub_sha256"],
                    "content_document_count": report["content_document_count"],
                    "spine_count": report["spine_count"],
                    "toc_count": report["toc_count"],
                    "source_unchanged": report["source_unchanged"],
                    "api_calls": 0,
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (
        PublicationEpubError,
        ProductionIntegrityError,
        OSError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        ProductionRunLockedError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
