import argparse
import json
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from ebooklib import epub

from chapter_library import (
    index_chapter_library,
    production_unit_kind,
    reader_facing_unit_identity,
    UNIT_KIND_CHAPTER,
)
from noveltranslator_core import (
    atomic_replace_prepared_file,
    atomic_write_text,
    create_unique_sibling_temp,
    recognize_english_chapter_heading,
    validate_progress_consistency,
)
from novel_project import legacy_project, select_project
from production_run_lock import production_run_lock
from epub_validate import validate_epub


# ============================================================
# CONFIGURATION
# ============================================================

BOOK_TITLE = "Checkpoint English Translation"

# This script uses ONLY local files. It makes NO API calls.
PROGRESS_FILE = Path("data/progress.json")
CHAPTER_DIR = Path("output/chapters")
EPUB_DIR = Path("output/epub")

# ============================================================
# HELPERS
# ============================================================

def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def chapter_file_number(path):
    """
    Extract chapter number from filenames such as:
    0001.md
    0047.md
    1406.md
    """
    match = re.fullmatch(r"(\d+)\.md", path.name)
    if not match:
        return None

    return int(match.group(1))


_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def normalize_newlines(text):
    text = _XML_ILLEGAL.sub("", text)
    return (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def split_blocks(text):
    """
    Split the chapter into paragraph-like blocks while preserving
    visible scene separators.
    """
    text = normalize_newlines(text).strip()

    if not text:
        return []

    blocks = re.split(r"\n\s*\n+", text)

    return [
        block.strip()
        for block in blocks
        if block.strip()
    ]


def is_separator(block):
    compact = re.sub(r"\s+", "", block.strip())
    separator_chars = set("~*.-_=\u2014\u2026\u00b7")
    return len(compact) >= 3 and all(char in separator_chars for char in compact)


def reader_facing_chapter_title(text, fallback_title):
    """Use the translated local heading while filenames stay production-indexed."""
    blocks = split_blocks(text)
    if not blocks:
        return fallback_title
    heading = recognize_english_chapter_heading(blocks[0])
    if heading is None:
        return fallback_title
    title = f"Chapter {heading['chapter']}"
    if heading["title"]:
        title += f": {heading['title']}"
    return title


def reader_facing_named_unit_title(text, fallback_title):
    """Read a translated Prologue/Epilogue/Side Story/Supplement heading."""
    blocks = split_blocks(text)
    if not blocks:
        return fallback_title
    candidate = re.sub(r"^#{1,6}\s*", "", blocks[0]).strip()
    if not candidate or "\n" in candidate or len(candidate) > 200:
        return fallback_title
    return candidate


def markdownish_to_xhtml(text, fallback_title, named_heading=False):
    """
    Convert the simple chapter .md files produced by the translator
    into safe EPUB XHTML.

    This intentionally avoids any remote/API service.
    """
    blocks = split_blocks(text)

    if not blocks:
        return (
            f"<h1>{escape(fallback_title)}</h1>"
            "<p>[Empty chapter file]</p>"
        )

    html_parts = []

    first = blocks[0]

    heading = recognize_english_chapter_heading(first)

    if named_heading:
        display_heading = reader_facing_named_unit_title(text, fallback_title)
        html_parts.append(f"<h1>{escape(display_heading)}</h1>")
        body_blocks = blocks[1:] if display_heading != fallback_title or (
            re.sub(r"^#{1,6}\s*", "", first).strip() == fallback_title
        ) else blocks
    elif heading is not None:
        display_heading = f"Chapter {heading['chapter']}"
        if heading["title"]:
            display_heading += f": {heading['title']}"
        html_parts.append(
            f"<h1>{escape(display_heading)}</h1>"
        )
        body_blocks = blocks[1:]
    else:
        html_parts.append(
            f"<h1>{escape(fallback_title)}</h1>"
        )
        body_blocks = blocks

    for block in body_blocks:
        if is_separator(block):
            html_parts.append(
                '<p class="scene-break">* * *</p>'
            )
            continue

        safe = escape(
            block,
            quote=False,
        ).replace(
            "\n",
            "<br/>"
        )

        html_parts.append(
            f"<p>{safe}</p>"
        )

    return "\n".join(html_parts)


def find_available_chapters(chapter_dir=CHAPTER_DIR):
    chapter_dir = Path(chapter_dir)
    if not chapter_dir.exists():
        raise RuntimeError(
            f"Chapter directory does not exist: {chapter_dir}"
        )

    available = {}

    for path in chapter_dir.glob("*.md"):
        chapter_number = chapter_file_number(path)

        if chapter_number is None:
            continue

        if chapter_number in available:
            raise RuntimeError(
                f"Multiple committed files represent Chapter {chapter_number}: "
                f"{available[chapter_number].name}, {path.name}"
            )
        available[chapter_number] = path

    return available


def determine_chapter_range(
    available,
    progress,
    from_chapter=None,
    to_chapter=None,
):
    if not isinstance(progress, dict):
        raise RuntimeError("progress.json must contain a JSON object.")
    report = validate_progress_consistency(progress, set(available))
    problems = list(report["errors"])
    if report["missing_committed_files"]:
        problems.append(
            "Missing committed chapter files: "
            + ", ".join(str(item) for item in report["missing_committed_files"])
        )
    if report["unexpected_committed_files"]:
        problems.append(
            "Unexpected extra committed chapter files: "
            + ", ".join(str(item) for item in report["unexpected_committed_files"])
        )
    if problems:
        raise RuntimeError(
            "Committed checkpoint is inconsistent:\n- " + "\n- ".join(problems)
        )

    last_committed = report["last_completed"]
    if last_committed < 1:
        raise RuntimeError("No safely committed chapters are available for EPUB creation.")

    first = 1 if from_chapter is None else from_chapter
    last = last_committed if to_chapter is None else to_chapter
    for value, label in ((first, "from_chapter"), (last, "to_chapter")):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
    if first < 1:
        raise ValueError("from_chapter must be at least 1")
    if last < first:
        raise ValueError("to_chapter must be greater than or equal to from_chapter")
    if last > last_committed:
        raise ValueError(
            f"to_chapter {last} exceeds the safely committed range ending at "
            f"Chapter {last_committed}"
        )
    return list(range(first, last + 1))


# ============================================================
# EPUB BUILD
# ============================================================

def checkpoint_output_path(epub_dir, first_chapter, last_chapter, novel_id=None):
    if novel_id:
        filename = f"{novel_id}_chapters_{first_chapter:04d}_{last_chapter:04d}.epub"
    else:
        filename = f"translation_chapters_{first_chapter:04d}-{last_chapter:04d}.epub"
    return Path(epub_dir) / filename


def latest_output_path(epub_dir, novel_id):
    return Path(epub_dir) / f"{novel_id}_latest.epub"


def build_epub(project=None, from_chapter=None, to_chapter=None, write_latest=False):
    project = project or legacy_project()
    progress = load_json(
        project.progress_file
    )
    source_units = load_json(project.chapters_file)
    source_unit_map = (
        index_chapter_library(
            source_units,
            project.config.final_chapter,
            project.config.chapter_numbering,
        )
        if source_units else {}
    )

    available = find_available_chapters(project.chapter_dir)

    chapters_to_include = determine_chapter_range(
        available,
        progress,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
    )

    first_chapter = chapters_to_include[0]
    last_chapter = chapters_to_include[-1]

    epub_dir = project.epub_dir
    output_file = checkpoint_output_path(
        epub_dir,
        first_chapter,
        last_chapter,
        None if project.is_legacy else project.config.id,
    )

    book_title = BOOK_TITLE if project.is_legacy else project.config.title
    book_language = project.config.target_language
    source_language = project.config.source_language
    book_author = project.config.author

    book = epub.EpubBook()

    # The stable "latest" book keeps one identifier so readers treat rebuilds
    # as the same book; range files keep a per-range identifier.
    book.set_identifier(
        f"noveltranslator-{project.config.id}"
        if write_latest
        else f"noveltranslator-{project.config.id}-{first_chapter}-{last_chapter}"
    )

    book.set_title(
        f"{book_title} — Chapters {first_chapter}-{last_chapter}"
    )

    book.set_language(
        book_language
    )

    book.add_metadata("DC", "source", source_language)

    if book_author:
        book.add_author(book_author)

    css = """
body {
    font-family: serif;
    line-height: 1.5;
    margin: 0 4%;
}

h1 {
    text-align: center;
    margin-top: 1.2em;
    margin-bottom: 1.5em;
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
""".strip()

    style = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=css,
    )

    book.add_item(
        style
    )

    title_page = epub.EpubHtml(
        title=book_title,
        file_name="title.xhtml",
        lang=book_language,
    )

    title_page.content = f"""
<h1>{escape(book_title)}</h1>
<p style="text-indent:0; text-align:center;">
Chapters {first_chapter}–{last_chapter}
</p>
<p style="text-indent:0; text-align:center;">
Generated locally from committed translation files.
</p>
""".strip()

    title_page.add_item(
        style
    )

    book.add_item(
        title_page
    )

    epub_chapters = []

    for chapter_number in chapters_to_include:
        path = available[
            chapter_number
        ]

        text = path.read_text(
            encoding="utf-8"
        )

        source_unit = source_unit_map.get(chapter_number)
        named_unit = False
        if source_unit is None:
            fallback_title = f"Chapter {chapter_number}"
        elif production_unit_kind(source_unit) != UNIT_KIND_CHAPTER:
            # A committed named unit should carry its translated title. The
            # neutral semantic fallback never exposes its production index.
            fallback_title = reader_facing_unit_identity(
                source_unit, project.config.chapter_numbering
            )
            named_unit = True
        else:
            fallback_title = reader_facing_unit_identity(
                source_unit, project.config.chapter_numbering
            )
        chapter_title = (
            reader_facing_named_unit_title(text, fallback_title)
            if named_unit
            else reader_facing_chapter_title(text, fallback_title)
        )

        chapter = epub.EpubHtml(
            title=chapter_title,
            file_name=(
                f"chapters/"
                f"chapter_{chapter_number:04d}.xhtml"
            ),
            lang=book_language,
        )

        chapter.content = markdownish_to_xhtml(
            text,
            chapter_title,
            named_heading=named_unit,
        )

        chapter.add_item(
            style
        )

        book.add_item(
            chapter
        )

        epub_chapters.append(
            chapter
        )

    book.toc = tuple(
        epub_chapters
    )

    book.spine = [
        title_page,
        *epub_chapters,
    ]

    book.add_item(
        epub.EpubNcx()
    )

    book.add_item(
        epub.EpubNav()
    )

    epub_dir.mkdir(parents=True, exist_ok=True)
    temp_output = create_unique_sibling_temp(output_file)
    published = False
    try:
        epub.write_epub(str(temp_output), book, {})
        atomic_replace_prepared_file(temp_output, output_file)
        published = True
    finally:
        if not published:
            temp_output.unlink(missing_ok=True)

    if write_latest:
        # Copy from the finished range file; any earlier failure never reaches
        # here, so the previous latest stays intact.
        latest_file = latest_output_path(epub_dir, project.config.id)
        latest_temp = create_unique_sibling_temp(latest_file)
        latest_done = False
        try:
            latest_temp.write_bytes(output_file.read_bytes())
            atomic_replace_prepared_file(latest_temp, latest_file)
            latest_done = True
        finally:
            if not latest_done:
                latest_temp.unlink(missing_ok=True)

    print()
    print("=" * 70)
    print("CHECKPOINT EPUB CREATED")
    print("=" * 70)
    print(f"Output: {output_file}")
    print(
        f"Chapters included: "
        f"{len(chapters_to_include)}"
    )
    print(
        f"Range: "
        f"{first_chapter}-{last_chapter}"
    )
    print("API calls used: 0")
    print("API cost: $0.00")
    print("=" * 70)

    return output_file


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _report_is_current(report_file, latest_file, project):
    try:
        report = load_json(report_file)
        progress = load_json(project.progress_file)
        if report.get("status") not in ("BUILT", "SKIPPED"):
            return None
        if report.get("last_completed") != progress.get("last_completed"):
            return None
        if not latest_file.is_file():
            return None
        built = latest_file.stat().st_mtime
        for path in Path(project.chapter_dir).glob("*.md"):
            if path.stat().st_mtime > built:
                return None
        return report
    except (OSError, ValueError, AttributeError):
        return None


def rebuild_reader_epub(project, take_lock=False, epubcheck_jar=None):
    """Build, validate and report the always-current reader EPUB.

    Never raises. Pass take_lock=True only when the caller does NOT already hold
    the novel's production lock (the CLI does); a process that holds it must
    leave the default so the non-reentrant lock is not taken twice.
    """
    try:
        project = project or legacy_project()
        epub_dir = Path(project.epub_dir)
        latest_file = latest_output_path(epub_dir, project.config.id)
        report_file = epub_dir / "latest_report.json"
        current = _report_is_current(report_file, latest_file, project)
        if current is not None:
            return {**current, "status": "SKIPPED"}
        if take_lock:
            with production_run_lock(project.root):
                output = build_epub(project, write_latest=True)
        else:
            output = build_epub(project, write_latest=True)
        last = int(load_json(project.progress_file)["last_completed"])
        report = {
            "status": "BUILT",
            "last_completed": last,
            "chapters_covered": last,
            "path": str(latest_file),
            "range_path": str(output),
            "validation": validate_epub(latest_file, epubcheck_jar),
            "utc": _utc_now(),
        }
        atomic_write_text(report_file, json.dumps(report, indent=2) + "\n")
        return report
    except Exception as exc:
        return {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build a local checkpoint EPUB for one novel"
    )
    parser.add_argument(
        "--novel",
        help="novel identifier under novels/; omit for the legacy root project",
    )
    parser.add_argument("--from-chapter", type=int)
    parser.add_argument("--to-chapter", type=int)
    parser.add_argument(
        "--latest",
        action="store_true",
        help="build and validate <id>_latest.epub plus latest_report.json",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        if args.latest:
            report = rebuild_reader_epub(project, take_lock=True)
            print(json.dumps(report, indent=2))
            return 1 if report["status"] == "ERROR" else 0
        with production_run_lock(project.root):
            build_epub(
                project,
                from_chapter=args.from_chapter,
                to_chapter=args.to_chapter,
            )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
