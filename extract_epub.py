"""Safely extract one novel project's local source EPUB without API calls."""

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from ebooklib import epub

from chapter_library import (
    UNIT_KIND_CHAPTER,
    UNIT_KIND_SUPPLEMENT,
    production_unit_kind,
)

from novel_project import (
    CHAPTER_NUMBERING_GLOBAL,
    CHAPTER_NUMBERING_SPINE_SEQUENTIAL,
    select_project,
)
from noveltranslator_core import atomic_write_bytes, atomic_write_text
from production_run_lock import ProductionRunLockedError, production_run_lock
from translation_profile import load_translation_profile


# Chapter numbers are authoritative only when recognized in the document content.
CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHINESE_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
CHINESE_LARGE_UNITS = {"萬": 10_000, "万": 10_000, "億": 100_000_000, "亿": 100_000_000}
CHINESE_NUMERAL_CHARACTERS = "".join(
    (*CHINESE_DIGITS, *CHINESE_SMALL_UNITS, *CHINESE_LARGE_UNITS)
)
CHAPTER_HEADING_RE = re.compile(
    rf"^第\s*(?P<numeral>[0-9]+|[{CHINESE_NUMERAL_CHARACTERS}\s]+?)"
    rf"\s*章(?P<title>.*)$"
)
CHAPTER_RANGE_HEADING_RE = re.compile(
    rf"^第\s*(?P<start>[0-9]+|[{CHINESE_NUMERAL_CHARACTERS}\s]+?)\s*"
    rf"[-\u2010-\u2015\uff0d~\uff5e至]\s*"
    rf"(?P<end>[0-9]+|[{CHINESE_NUMERAL_CHARACTERS}\s]+?)\s*"
    rf"章(?P<title>.*)$"
)
CHINESE_CHAPTER_WITHOUT_MARKER_RE = re.compile(
    rf"^第\s*(?P<numeral>[{CHINESE_NUMERAL_CHARACTERS}\s]+)"
    rf"(?P<title>[^{CHINESE_NUMERAL_CHARACTERS}\s].+)$"
)
EXTRA_HEADING_RE = re.compile(
    rf"^番外\s*(?P<numeral>[0-9]+|[{CHINESE_NUMERAL_CHARACTERS}\s]+)?(?P<title>.*)$"
)
META_HEADING_RE = re.compile(
    r"^(?:"
    r"上架感(?:言|想)|完本感言|關於完結|关于完结|"
    r"作者(?:的)?[话話]|作者有[话話]说|[请請]假|通知|公告"
    r").*$"
)
HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
DOCUMENT_MEDIA_TYPES = frozenset(("application/xhtml+xml", "text/html"))
BLOCK_TAGS = frozenset(
    ("article", "aside", "blockquote", "content", "div", "h1", "h2", "h3",
     "h4", "h5", "h6", "li", "main", "p", "section")
)
MIN_VISIBLE_TEXT_COVERAGE = 0.85
SPLIT_PAGE_SUFFIX_RE = re.compile(
    r"\s*[\(（]\s*(?P<part>[0-9]+)\s*/\s*(?P<total>[0-9]+)\s*[\)）]\s*$"
)
CONFIRMED_SITE_NOTICE_RE = re.compile(
    r"^溫馨提示\s*[:：]\s*(?:"
    r"iphone手機下某些版本的Safari瀏覽器會卡死的問題.*|"
    r"優化了VIP會員的閱讀體驗.*|"
    r"登錄用戶的「站內信」功能已經優化.*|"
    r"大家遇到\s*「你是真人嗎」\s*這樣的廣告.*|"
    r"搜書名找不到,?\s*可以試試搜作者.*|"
    r"登錄用戶跨設備永久保存書架的數據.*|"
    r"如果覺得本書不錯,?\s*避免下次找不到,?\s*請記得加入書架哦"
    r")$"
)
CONFIRMED_EXACT_SITE_PARAGRAPHS = frozenset(
    ("應廣大讀者的要求, 現推出VIP會員免廣告功能",)
)


class ExtractionError(RuntimeError):
    """Raised when a source cannot be safely extracted or installed."""


class ExtractionValidationError(ExtractionError):
    """Raised when extracted content is not safe to install."""

    def __init__(self, errors, report=None):
        self.errors = tuple(errors)
        self.report = report
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class SourceOverrides:
    chapter_numbers: dict
    supplements: dict
    exact_boilerplate: tuple
    exact_text_fragments: dict


@dataclass(frozen=True)
class ExtractionResult:
    chapters: list
    source: Path
    source_sha256: str
    numbering_mode: str
    missing_chapters: tuple
    configured_missing_chapters: tuple
    duplicate_chapters: tuple
    applied_overrides: tuple
    applied_content_cleanups: tuple
    unknown_override_files: tuple
    unapplied_override_files: tuple
    extra_documents: tuple
    meta_documents: tuple
    unmatched_documents: tuple
    supplement_documents: tuple
    combined_chapter_documents: tuple
    volume_boundaries: tuple
    local_numbering_warnings: tuple
    suspicious_hangul_characters: int
    source_visible_characters: int
    extracted_visible_characters: int
    content_coverage: float
    content_loss_documents: tuple
    validation_errors: tuple

    @property
    def chapter_numbers(self):
        return tuple(chapter["chapter"] for chapter in self.chapters)

    @property
    def production_indexes(self):
        return self.chapter_numbers

    @property
    def local_chapter_numbers(self):
        return tuple(
            chapter.get("local_chapter_number")
            if production_unit_kind(chapter) == UNIT_KIND_SUPPLEMENT
            else chapter.get("local_chapter_number", chapter["chapter"])
            for chapter in self.chapters
        )

    @property
    def ordinary_chapters(self):
        return tuple(
            item for item in self.chapters
            if production_unit_kind(item) == UNIT_KIND_CHAPTER
        )

    @property
    def supplements(self):
        return tuple(
            item for item in self.chapters
            if production_unit_kind(item) == UNIT_KIND_SUPPLEMENT
        )

    @property
    def unique_chapter_count(self):
        return len(set(self.chapter_numbers))

    @property
    def first_chapter(self):
        return self.chapter_numbers[0] if self.chapter_numbers else None

    @property
    def last_chapter(self):
        return self.chapter_numbers[-1] if self.chapter_numbers else None

    @property
    def first_heading(self):
        return self.chapters[0]["heading"] if self.chapters else None

    @property
    def last_heading(self):
        return self.chapters[-1]["heading"] if self.chapters else None

    @property
    def continuous_from_one(self):
        return self.first_chapter == 1 and not self.missing_chapters


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_chinese_integer(value):
    """Convert a conventional Chinese integer numeral to an Arabic integer."""
    if not isinstance(value, str):
        raise ValueError("Chinese numeral must be text")
    numeral = re.sub(r"\s+", "", value)
    if not numeral:
        raise ValueError("Chinese numeral is empty")
    invalid = set(numeral) - (
        set(CHINESE_DIGITS) | set(CHINESE_SMALL_UNITS) | set(CHINESE_LARGE_UNITS)
    )
    if invalid:
        raise ValueError(f"invalid Chinese numeral characters: {''.join(sorted(invalid))}")

    if all(character in CHINESE_DIGITS for character in numeral):
        return int("".join(str(CHINESE_DIGITS[character]) for character in numeral))

    total = 0
    section = 0
    pending_digit = None
    last_small_unit = 10_000
    last_large_unit = float("inf")
    previous_was_zero = False

    for character in numeral:
        if character in CHINESE_DIGITS:
            digit = CHINESE_DIGITS[character]
            if digit == 0:
                if pending_digit not in (None, 0):
                    raise ValueError(f"invalid zero placement in Chinese numeral: {value}")
                pending_digit = 0
                previous_was_zero = True
            else:
                if pending_digit not in (None, 0):
                    raise ValueError(f"consecutive digits in Chinese numeral: {value}")
                pending_digit = digit
                previous_was_zero = False
            continue

        if character in CHINESE_SMALL_UNITS:
            unit = CHINESE_SMALL_UNITS[character]
            if unit >= last_small_unit:
                raise ValueError(f"invalid unit order in Chinese numeral: {value}")
            if pending_digit is None:
                if unit != 10 or section != 0:
                    raise ValueError(f"missing digit before unit in Chinese numeral: {value}")
                digit = 1
            elif pending_digit == 0:
                raise ValueError(f"invalid zero before unit in Chinese numeral: {value}")
            else:
                digit = pending_digit
            section += digit * unit
            pending_digit = None
            previous_was_zero = False
            last_small_unit = unit
            continue

        unit = CHINESE_LARGE_UNITS[character]
        if unit >= last_large_unit:
            raise ValueError(f"invalid large-unit order in Chinese numeral: {value}")
        if pending_digit is not None:
            section += pending_digit
        if section == 0:
            raise ValueError(f"missing value before large unit in Chinese numeral: {value}")
        total += section * unit
        section = 0
        pending_digit = None
        previous_was_zero = False
        last_small_unit = 10_000
        last_large_unit = unit

    if previous_was_zero:
        raise ValueError(f"Chinese numeral cannot end in zero: {value}")
    if pending_digit is not None:
        section += pending_digit
    return total + section


def _parse_heading_numeral(value):
    numeral = re.sub(r"\s+", "", value)
    try:
        return (
            int(numeral)
            if numeral.isascii() and numeral.isdigit()
            else parse_chinese_integer(numeral)
        )
    except ValueError:
        return None


def parse_chapter_heading(heading):
    """Return the authoritative main chapter number from a content heading."""
    if not isinstance(heading, str):
        return None
    original = heading.strip()
    match = CHAPTER_HEADING_RE.fullmatch(original)
    if not match and "章" not in original:
        match = CHINESE_CHAPTER_WITHOUT_MARKER_RE.fullmatch(original)
    if not match:
        return None
    number = _parse_heading_numeral(match.group("numeral"))
    return number if number is not None and number > 0 else None


def parse_chapter_range_heading(heading):
    """Return an inclusive local chapter range from one combined heading."""
    if not isinstance(heading, str):
        return None
    match = CHAPTER_RANGE_HEADING_RE.fullmatch(heading.strip())
    if not match:
        return None
    start = _parse_heading_numeral(match.group("start"))
    end = _parse_heading_numeral(match.group("end"))
    if start is None or end is None or start < 1 or end <= start:
        return None
    return start, end


def chapter_heading_title(heading):
    """Preserve the title portion without treating it as an engine key."""
    if not isinstance(heading, str):
        return ""
    original = heading.strip()
    for pattern in (CHAPTER_RANGE_HEADING_RE, CHAPTER_HEADING_RE):
        match = pattern.fullmatch(original)
        if match:
            return match.group("title").lstrip(" \t\u3000:：-—")
    match = CHINESE_CHAPTER_WITHOUT_MARKER_RE.fullmatch(original)
    return match.group("title").strip() if match else ""


def parse_extra_heading(heading):
    """Return an optional side-story number for an authoritative 番外 heading."""
    if not isinstance(heading, str):
        return None
    match = EXTRA_HEADING_RE.fullmatch(heading.strip())
    if not match:
        return None
    numeral = (match.group("numeral") or "").strip()
    if not numeral:
        return 0
    try:
        number = int(numeral) if numeral.isascii() and numeral.isdigit() else (
            parse_chinese_integer(numeral)
        )
    except ValueError:
        return None
    return number if number >= 0 else None


def classify_document_heading(heading):
    """Classify only an authoritative content heading, never its filename."""
    chapter_range = parse_chapter_range_heading(heading)
    if chapter_range is not None:
        return "chapter_range", chapter_range
    chapter_number = parse_chapter_heading(heading)
    if chapter_number is not None:
        return "chapter", chapter_number
    extra_number = parse_extra_heading(heading)
    if extra_number is not None:
        return "extra", extra_number
    if isinstance(heading, str) and META_HEADING_RE.fullmatch(heading.strip()):
        return "meta", None
    return "unmatched", None


def _normalize_visible_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _is_confirmed_boilerplate(text, exact_boilerplate=()):
    normalized = _normalize_visible_text(text)
    prefixed_site_notice = bool(
        normalized.startswith(("溫馨提示:", "溫馨提示：", "温馨提示:", "温馨提示："))
        and any(marker in normalized for marker in (
            "網站", "书架", "書架", "瀏覽器", "浏览器", "VIP", "站內信",
            "站内信", "搜書名", "搜书名", "廣告", "广告", "登錄用戶",
            "登录用户",
        ))
    )
    return bool(
        normalized
        and (
            normalized in CONFIRMED_EXACT_SITE_PARAGRAPHS
            or normalized in exact_boilerplate
            or CONFIRMED_SITE_NOTICE_RE.fullmatch(normalized)
            or prefixed_site_notice
        )
    )


def _prepare_content_soup(
    html_bytes, exact_boilerplate=(), exact_text_fragments=()
):
    soup = BeautifulSoup(html_bytes, "xml")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    # Remove only a complete element whose entire content is a confirmed notice.
    # Its narrative parent remains in the tree and is traversed normally.
    for element in reversed(soup.find_all(True)):
        if element.name in {"body", "html"}:
            continue
        text = element.get_text(" ", strip=True)
        if _is_confirmed_boilerplate(text, exact_boilerplate):
            element.decompose()
    # Source-bound inline cleanup rules remove only an exact verified fragment
    # from a text node. They never remove the surrounding narrative element.
    for node in soup.find_all(string=True):
        cleaned = str(node)
        for fragment in exact_text_fragments:
            cleaned = cleaned.replace(fragment, "")
        if cleaned != str(node):
            node.replace_with(cleaned)
    return soup


def _exact_text_fragment_counts(html_bytes, fragments):
    """Count exact fragment anchors in visible text nodes without modifying source."""
    soup = BeautifulSoup(html_bytes, "xml")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    return {
        fragment: sum(str(node).count(fragment) for node in soup.find_all(string=True))
        for fragment in fragments
    }


def _tree_to_paragraphs(root, exact_boilerplate=()):
    paragraphs = []
    buffer = []

    def flush():
        text = _normalize_visible_text("".join(buffer))
        buffer.clear()
        if text and not _is_confirmed_boilerplate(text, exact_boilerplate):
            paragraphs.append(text)

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            buffer.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = (node.name or "").lower()
        if name == "br":
            flush()
            return
        is_block = name in BLOCK_TAGS
        if is_block:
            flush()
        for child in node.children:
            walk(child)
        if is_block:
            flush()

    for child in root.children:
        walk(child)
    flush()
    return paragraphs


def _legacy_extract_paragraphs(soup):
    """Compatibility parser retained when it already preserves source content."""
    paragraphs = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "div"]):
        if element.name == "div" and element.find(
            ["h1", "h2", "h3", "h4", "p", "div"], recursive=False
        ):
            continue
        text = element.get_text(" ", strip=True)
        if text:
            paragraphs.append(text)
    body = soup.find("body")
    if body is not None and len(paragraphs) <= 1:
        body_text = body.get_text("\n", strip=False)
        fallback = [
            line.strip()
            for line in body_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if line.strip()
        ]
        if len(fallback) > len(paragraphs):
            paragraphs = fallback
    return paragraphs


def extract_paragraphs(
    html_bytes, exact_boilerplate=(), exact_text_fragments=()
):
    """Extract visible text once, in DOM order, preserving ``br`` boundaries."""
    soup = _prepare_content_soup(
        html_bytes, exact_boilerplate, exact_text_fragments
    )
    body = soup.find("body")
    if body is not None and not body.find(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "content"]
    ):
        return []
    legacy = _legacy_extract_paragraphs(soup)
    visible_count = len(re.sub(r"\s+", "", (body or soup).get_text(" ", strip=True)))
    legacy_count = extracted_visible_character_count(legacy)
    br_heavy_container = bool(
        body is not None
        and len(legacy) <= 2
        and len(body.find_all("br")) >= 3
        and body.find(["div", "content"])
    )
    if (
        not br_heavy_container
        and visible_count
        and legacy_count / visible_count >= MIN_VISIBLE_TEXT_COVERAGE
    ):
        return legacy
    return _tree_to_paragraphs(body or soup, exact_boilerplate)


def source_visible_character_count(
    html_bytes, exact_boilerplate=(), exact_text_fragments=()
):
    """Independent source-visible count used to detect catastrophic parser loss."""
    soup = _prepare_content_soup(
        html_bytes, exact_boilerplate, exact_text_fragments
    )
    body = soup.find("body")
    text = _normalize_visible_text((body or soup).get_text(" ", strip=True))
    for exact in (*CONFIRMED_EXACT_SITE_PARAGRAPHS, *exact_boilerplate):
        text = text.replace(_normalize_visible_text(exact), "")
    return len(re.sub(r"\s+", "", text))


def extracted_visible_character_count(paragraphs):
    return len(re.sub(r"\s+", "", "".join(paragraphs)))


def _strip_split_page_suffix(heading):
    if not isinstance(heading, str):
        return heading, None
    match = SPLIT_PAGE_SUFFIX_RE.search(heading.strip())
    if not match:
        return heading.strip(), None
    return heading.strip()[:match.start()].rstrip(), (
        int(match.group("part")), int(match.group("total"))
    )


def consolidate_split_page_paragraphs(paragraphs, source_file="document"):
    """Merge page halves contained in one XHTML without merging real chapters."""
    candidates = []
    for index, paragraph in enumerate(paragraphs):
        base_heading, page = _strip_split_page_suffix(paragraph)
        if page is not None and parse_chapter_heading(base_heading) is not None:
            candidates.append((index, paragraph, base_heading, page))
    if not candidates:
        return list(paragraphs), None

    total_values = {item[3][1] for item in candidates}
    if len(total_values) != 1:
        raise ExtractionError(
            f"split-page headings disagree on total page count: {source_file}"
        )
    total = total_values.pop()
    parts = [item[3][0] for item in candidates]
    if parts != list(range(1, total + 1)):
        raise ExtractionError(
            f"split-page headings are incomplete or out of order: {source_file}"
        )
    numbers = {parse_chapter_heading(item[2]) for item in candidates}
    titles = {
        _normalize_visible_text(chapter_heading_title(item[2])).casefold()
        for item in candidates
    }
    if len(numbers) != 1:
        raise ExtractionError(
            f"split-page headings disagree on chapter number: {source_file}"
        )
    if len(titles) != 1:
        raise ExtractionError(
            f"split-page headings disagree on chapter title: {source_file}"
        )

    first_index = candidates[0][0]
    skip = {item[0] for item in candidates[1:]}
    combined = []
    for index, paragraph in enumerate(paragraphs):
        if index == first_index:
            combined.append(candidates[0][2])
        elif index not in skip:
            combined.append(paragraph)
    return combined, {
        "page_count": total,
        "original_headings": [item[1] for item in candidates],
        "heading": candidates[0][2],
    }


def _assert_project_path(project, path, label):
    project_root = project.root.resolve()
    resolved = Path(path).resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise ExtractionError(f"{label} escapes the selected novel directory: {path}")
    return resolved


def load_source_overrides(project, source_sha256):
    """Load strict, project-owned source exceptions without inferring any."""
    path = _assert_project_path(
        project, project.source_overrides_file, "source override file"
    )
    if not path.exists():
        return SourceOverrides({}, {}, (), {})
    if not path.is_file():
        raise ExtractionError(f"source override path is not a file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"invalid source override JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExtractionError("source_overrides.json must contain a JSON object")
    unsupported = sorted(set(value) - {
        "source_sha256", "chapter_number_overrides", "supplement_documents",
        "content_cleanup",
    })
    if unsupported:
        raise ExtractionError(
            "source_overrides.json contains unsupported fields: "
            + ", ".join(unsupported)
        )
    overrides = value.get("chapter_number_overrides", {})
    if not isinstance(overrides, dict):
        raise ExtractionError("chapter_number_overrides must be a JSON object")
    supplements = value.get("supplement_documents", {})
    if not isinstance(supplements, dict):
        raise ExtractionError("supplement_documents must be a JSON object")
    content_cleanup = value.get("content_cleanup", {})
    if not isinstance(content_cleanup, dict):
        raise ExtractionError("content_cleanup must be a JSON object")
    unsupported_cleanup = sorted(
        set(content_cleanup) - {"exact_paragraphs", "exact_text_fragments"}
    )
    if unsupported_cleanup:
        raise ExtractionError(
            "content_cleanup contains unsupported fields: "
            + ", ".join(unsupported_cleanup)
        )
    exact_boilerplate = content_cleanup.get("exact_paragraphs", [])
    if not isinstance(exact_boilerplate, list) or not all(
        isinstance(item, str)
        and len(item.strip()) <= 1000
        and (
            len(item.strip()) >= 12
            or re.fullmatch(r"https?:?", item.strip(), re.IGNORECASE)
        )
        for item in exact_boilerplate
    ):
        raise ExtractionError(
            "content_cleanup.exact_paragraphs must be a list of specific text "
            "strings between 12 and 1000 characters (or an exact bare HTTP(S) remnant)"
        )
    if len(set(exact_boilerplate)) != len(exact_boilerplate):
        raise ExtractionError("content_cleanup.exact_paragraphs contains duplicates")

    exact_text_fragments = content_cleanup.get("exact_text_fragments", {})
    if not isinstance(exact_text_fragments, dict):
        raise ExtractionError(
            "content_cleanup.exact_text_fragments must be an object mapping "
            "source member paths to exact fragment lists"
        )
    validated_text_fragments = {}
    for source_file, fragments in exact_text_fragments.items():
        _validate_source_rule_path(source_file, "exact text cleanup")
        if (
            not isinstance(fragments, list)
            or not fragments
            or not all(
                isinstance(item, str)
                and item == item.strip()
                and 5 <= len(item) <= 500
                for item in fragments
            )
        ):
            raise ExtractionError(
                "content_cleanup.exact_text_fragments values must be non-empty "
                "lists of exact 5-500 character strings without outer whitespace"
            )
        if len(set(fragments)) != len(fragments):
            raise ExtractionError(
                f"exact text cleanup for {source_file!r} contains duplicates"
            )
        validated_text_fragments[source_file] = tuple(fragments)

    expected_source_hash = value.get("source_sha256")
    if overrides or supplements or exact_boilerplate or validated_text_fragments:
        if (
            not isinstance(expected_source_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_hash)
        ):
            raise ExtractionError(
                "source_overrides.json source_sha256 must be a 64-character "
                "hexadecimal hash"
            )
        if expected_source_hash.lower() != source_sha256.lower():
            raise ExtractionError(
                "source_overrides.json source_sha256 does not match the selected "
                "source EPUB"
            )

    validated = {}
    for source_file, chapter_number in overrides.items():
        if not isinstance(source_file, str) or not source_file:
            raise ExtractionError("source override keys must be non-empty strings")
        member = PurePosixPath(source_file)
        if (
            "\\" in source_file
            or member.is_absolute()
            or source_file != member.as_posix()
            or any(part in ("", ".", "..") for part in member.parts)
        ):
            raise ExtractionError(
                f"source override member path is unsafe or non-canonical: {source_file!r}"
            )
        if (
            not isinstance(chapter_number, int)
            or isinstance(chapter_number, bool)
            or chapter_number < 1
        ):
            raise ExtractionError(
                f"source override for {source_file!r} must be a positive integer"
            )
        if chapter_number > project.config.final_chapter:
            raise ExtractionError(
                f"source override for {source_file!r} exceeds configured final_chapter "
                f"{project.config.final_chapter}"
            )
        validated[source_file] = chapter_number

    counts = Counter(validated.values())
    duplicate_targets = sorted(
        number for number, count in counts.items() if count > 1
    )
    if duplicate_targets:
        raise ExtractionError(
            "source overrides contain duplicate corrected chapter numbers: "
            + _format_numbers(duplicate_targets)
        )
    validated_supplements = {}
    for source_file, specification in supplements.items():
        _validate_source_rule_path(source_file, "supplement")
        if isinstance(specification, str):
            expected_heading = specification
        elif isinstance(specification, dict) and set(specification) == {"heading"}:
            expected_heading = specification["heading"]
        else:
            raise ExtractionError(
                f"supplement declaration for {source_file!r} must contain only "
                "an expected heading"
            )
        if not isinstance(expected_heading, str) or not expected_heading.strip():
            raise ExtractionError(
                f"supplement declaration for {source_file!r} has an invalid heading"
            )
        validated_supplements[source_file] = expected_heading.strip()

    overlap = sorted(set(validated) & set(validated_supplements))
    if overlap:
        raise ExtractionError(
            "source members cannot be both chapter-number overrides and supplements: "
            + ", ".join(overlap)
        )
    return SourceOverrides(
        validated, validated_supplements,
        tuple(_normalize_visible_text(item) for item in exact_boilerplate),
        validated_text_fragments,
    )


def _validate_source_rule_path(source_file, label):
    if not isinstance(source_file, str) or not source_file:
        raise ExtractionError(f"{label} source paths must be non-empty strings")
    member = PurePosixPath(source_file)
    if (
        "\\" in source_file
        or member.is_absolute()
        or source_file != member.as_posix()
        or any(part in ("", ".", "..") for part in member.parts)
    ):
        raise ExtractionError(
            f"{label} source path is unsafe or non-canonical: {source_file!r}"
        )


def _read_source_book(project):
    """Perform all source/profile checks without changing project files."""
    load_translation_profile(project.config.profile, project.repository_root)
    source = _assert_project_path(project, project.source_epub, "source EPUB")
    if not source.is_file():
        raise FileNotFoundError(f"source EPUB does not exist: {source}")
    if not zipfile.is_zipfile(source):
        raise ExtractionError(f"source is not a readable EPUB/ZIP: {source}")
    source_hash = sha256_file(source)
    try:
        book = epub.read_epub(str(source))
    except Exception as exc:
        raise ExtractionError(f"could not read source EPUB {source}: {exc}") from exc
    return source, source_hash, book


def _epub_package_root(source):
    """Return the OPF directory so source rules may use full ZIP member paths."""
    try:
        with zipfile.ZipFile(source) as archive:
            container = BeautifulSoup(
                archive.read("META-INF/container.xml"), "xml"
            )
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ExtractionError(f"could not inspect EPUB container: {exc}") from exc
    rootfile = container.find("rootfile")
    full_path = rootfile.get("full-path") if rootfile else None
    if not isinstance(full_path, str) or not full_path.strip():
        raise ExtractionError("EPUB container has no valid OPF rootfile")
    parent = PurePosixPath(full_path).parent
    return "" if str(parent) == "." else parent.as_posix()


def _source_member_path(package_root, item_name):
    return (
        PurePosixPath(package_root, item_name).as_posix()
        if package_root
        else PurePosixPath(item_name).as_posix()
    )


def _matching_source_rule(mapping, source_file, source_member):
    matches = [key for key in (source_file, source_member) if key in mapping]
    if len(set(matches)) > 1:
        raise ExtractionError(
            "duplicate source rules refer to the same EPUB member: "
            + ", ".join(sorted(set(matches)))
        )
    return matches[0] if matches else None


def _validate_spine_sequential_chapters(chapters, final_chapter):
    errors = []
    production_indexes = []
    previous_volume = None
    for position, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict):
            errors.append(f"chapter entry {position} is not an object")
            continue
        production_index = chapter.get("chapter")
        explicit_index = chapter.get("production_index")
        if (
            not isinstance(production_index, int)
            or isinstance(production_index, bool)
            or production_index < 1
        ):
            errors.append(f"chapter entry {position} has an invalid production index")
        else:
            production_indexes.append(production_index)
            if production_index != position:
                errors.append(
                    f"chapter entry {position} is not in production-index order"
                )
        if explicit_index != production_index:
            errors.append(f"chapter entry {position} production_index disagrees with chapter")

        try:
            unit_kind = production_unit_kind(chapter)
        except ValueError as exc:
            errors.append(f"chapter entry {position}: {exc}")
            continue
        volume_index = chapter.get("volume_index")
        valid_volume = (
            isinstance(volume_index, int)
            and not isinstance(volume_index, bool)
            and volume_index > 0
        )
        if not valid_volume:
            errors.append(f"chapter entry {position} has invalid volume numbering")
        elif previous_volume is None and volume_index != 1:
            errors.append("the first production unit must use volume_index 1")
        elif previous_volume is not None and (
            volume_index < previous_volume or volume_index > previous_volume + 1
        ):
            errors.append(f"chapter entry {position} has a non-sequential volume index")

        if unit_kind == UNIT_KIND_SUPPLEMENT:
            forbidden = (
                "local_chapter_number", "local_chapter_start", "local_chapter_end"
            )
            if any(chapter.get(field) is not None for field in forbidden):
                errors.append(
                    f"supplement entry {position} must not define local chapter numbering"
                )
            if valid_volume:
                previous_volume = volume_index
            valid_local_numbering = False
            local_number = local_start = local_end = None
        else:
            local_number = chapter.get("local_chapter_number")
            local_start = chapter.get("local_chapter_start")
            local_end = chapter.get("local_chapter_end")
            valid_local_numbering = all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in (local_number, local_start, local_end)
            )
            if not valid_local_numbering:
                errors.append(
                    f"chapter entry {position} has invalid local chapter numbering"
                )
            else:
                if local_number != local_start or local_end < local_start:
                    errors.append(
                        f"chapter entry {position} has an invalid local chapter range"
                    )
                if (
                    previous_volume is not None
                    and volume_index == previous_volume + 1
                    and local_start != 1
                ):
                    errors.append(
                        f"chapter entry {position} begins a volume without local Chapter 1"
                    )
            if valid_volume:
                previous_volume = volume_index

        source_file = chapter.get("source_file")
        if not isinstance(source_file, str) or not source_file:
            errors.append(f"chapter entry {position} has an invalid source filename")
        text = chapter.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"chapter entry {position} has empty or invalid text")
        paragraph_count = chapter.get("paragraph_count")
        if not isinstance(paragraph_count, int) or paragraph_count < 1:
            errors.append(f"chapter entry {position} has an invalid paragraph count")
        if isinstance(text, str) and chapter.get("character_count") != len(text):
            errors.append(f"chapter entry {position} has an invalid character count")

        heading = chapter.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            errors.append(f"chapter entry {position} has an invalid heading")
            continue
        if unit_kind == UNIT_KIND_SUPPLEMENT:
            if chapter.get("title") != heading.strip():
                errors.append(
                    f"supplement entry {position} has an invalid preserved source title"
                )
            continue
        if chapter.get("title") != chapter_heading_title(heading):
            errors.append(f"chapter entry {position} has an invalid preserved title")
        detected_range = parse_chapter_range_heading(heading)
        detected_number = parse_chapter_heading(heading)
        source_override = chapter.get("source_override")
        if source_override is None and valid_local_numbering:
            if local_end > local_start:
                if detected_range != (local_start, local_end):
                    errors.append(
                        f"chapter entry {position} heading does not match its local range"
                    )
            elif detected_number != local_start:
                errors.append(
                    f"chapter entry {position} heading does not match its local number"
                )
        elif not isinstance(source_override, dict):
            errors.append(f"chapter entry {position} has invalid source override metadata")
        elif source_override.get("corrected_chapter") != local_start:
            errors.append(
                f"chapter entry {position} source override does not match its local number"
            )

    counts = Counter(production_indexes)
    duplicates = tuple(
        sorted(number for number, count in counts.items() if count > 1)
    )
    if not chapters:
        errors.append("no numbered chapters were extracted")
    if duplicates:
        errors.append(
            "duplicate production indexes were extracted: "
            + ", ".join(str(number) for number in duplicates)
        )
    detected_last = max(production_indexes, default=0)
    missing = tuple(
        sorted(set(range(1, detected_last + 1)) - set(production_indexes))
    )
    configured_missing = tuple(
        sorted(set(range(1, final_chapter + 1)) - set(production_indexes))
    )
    if missing:
        errors.append(
            "missing production indexes within the detected range: "
            + _format_numbers(missing)
        )
    out_of_range = sorted(number for number in counts if number > final_chapter)
    if out_of_range:
        errors.append(
            f"production indexes exceed configured final_chapter {final_chapter}: "
            + _format_numbers(out_of_range)
        )
    if detected_last and detected_last != final_chapter:
        errors.append(
            f"detected production-unit endpoint {detected_last} does not match "
            f"configured final_chapter {final_chapter}"
        )
    return missing, configured_missing, duplicates, tuple(errors)


def _validate_chapters(
    chapters, final_chapter, numbering_mode=CHAPTER_NUMBERING_GLOBAL
):
    if numbering_mode == CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
        return _validate_spine_sequential_chapters(chapters, final_chapter)
    errors = []
    chapter_numbers = []
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            errors.append(f"chapter entry {index} is not an object")
            continue
        number = chapter.get("chapter")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            errors.append(f"chapter entry {index} has an invalid chapter number")
        else:
            chapter_numbers.append(number)
        if not isinstance(chapter.get("source_file"), str) or not chapter["source_file"]:
            errors.append(f"chapter entry {index} has an invalid source filename")
        text = chapter.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"chapter entry {index} has empty or invalid text")
        paragraph_count = chapter.get("paragraph_count")
        if not isinstance(paragraph_count, int) or paragraph_count < 1:
            errors.append(f"chapter entry {index} has an invalid paragraph count")
        character_count = chapter.get("character_count")
        if isinstance(text, str) and character_count != len(text):
            errors.append(f"chapter entry {index} has an invalid character count")
        heading = chapter.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            errors.append(f"chapter entry {index} has an invalid heading")
        else:
            detected_number = parse_chapter_heading(heading)
            override = chapter.get("source_override")
            if override is None:
                if isinstance(number, int) and detected_number != number:
                    errors.append(
                        f"chapter entry {index} heading does not match its number"
                    )
            elif not isinstance(override, dict):
                errors.append(f"chapter entry {index} has invalid source override metadata")
            else:
                expected_keys = {
                    "source_file", "original_heading", "original_detected_chapter",
                    "corrected_chapter",
                }
                if set(override) != expected_keys:
                    errors.append(
                        f"chapter entry {index} has invalid source override metadata fields"
                    )
                if override.get("source_file") != chapter.get("source_file"):
                    errors.append(
                        f"chapter entry {index} source override filename does not match"
                    )
                if override.get("original_heading") != heading:
                    errors.append(
                        f"chapter entry {index} source override heading does not match"
                    )
                if override.get("original_detected_chapter") != detected_number:
                    errors.append(
                        f"chapter entry {index} source override original number does not match"
                    )
                original_number = override.get("original_detected_chapter")
                if original_number is not None and (
                    not isinstance(original_number, int)
                    or isinstance(original_number, bool)
                    or original_number < 1
                ):
                    errors.append(
                        f"chapter entry {index} source override original number is invalid"
                    )
                corrected_number = override.get("corrected_chapter")
                if (
                    not isinstance(corrected_number, int)
                    or isinstance(corrected_number, bool)
                    or corrected_number < 1
                ):
                    errors.append(
                        f"chapter entry {index} source override corrected number is invalid"
                    )
                if corrected_number != number:
                    errors.append(
                        f"chapter entry {index} source override corrected number does not match"
                    )

    counts = Counter(chapter_numbers)
    duplicates = tuple(sorted(number for number, count in counts.items() if count > 1))
    if not chapters:
        errors.append("no numbered chapters were extracted")
    if duplicates:
        errors.append(
            "duplicate chapter numbers were extracted: "
            + ", ".join(str(number) for number in duplicates)
        )
    detected_last = max(chapter_numbers, default=0)
    missing = tuple(sorted(set(range(1, detected_last + 1)) - set(chapter_numbers)))
    configured_missing = tuple(
        sorted(set(range(1, final_chapter + 1)) - set(chapter_numbers))
    )
    if missing:
        errors.append(
            "missing numbered chapters within the detected main range: "
            + _format_numbers(missing)
        )
    out_of_range = sorted(number for number in counts if number > final_chapter)
    if out_of_range:
        errors.append(
            f"chapter numbers exceed configured final_chapter {final_chapter}: "
            + ", ".join(str(number) for number in out_of_range)
        )
    if detected_last and detected_last != final_chapter:
        errors.append(
            f"detected main-story endpoint {detected_last} does not match "
            f"configured final_chapter {final_chapter}"
        )
    return missing, configured_missing, duplicates, tuple(errors)


def extract_source_epub(project):
    """Extract and validate in memory; never write the chapter library."""
    source, source_hash, book = _read_source_book(project)
    numbering_mode = project.config.chapter_numbering
    rules = load_source_overrides(project, source_hash)
    package_root = _epub_package_root(source)
    source_aliases = {}
    for item in book.get_items():
        source_file = item.get_name()
        source_aliases[source_file] = source_file
        source_aliases[_source_member_path(package_root, source_file)] = source_file
    configured_rule_paths = (
        set(rules.chapter_numbers)
        | set(rules.supplements)
        | set(rules.exact_text_fragments)
    )
    unknown_override_files = tuple(sorted(configured_rule_paths - set(source_aliases)))
    seen_override_files = set()
    applied_overrides = []
    applied_content_cleanups = []
    override_errors = []
    chapters = []
    supplement_documents = []
    extra_documents = []
    meta_documents = []
    unmatched_documents = []
    combined_chapter_documents = []
    volume_boundaries = []
    local_numbering_warnings = []
    content_loss_documents = []
    source_visible_total = 0
    extracted_visible_total = 0
    volume_index = 1
    previous_local_end = None

    for spine_position, spine_entry in enumerate(book.spine, 1):
        item = book.get_item_with_id(spine_entry[0])
        if item is None or getattr(item, "media_type", "") not in DOCUMENT_MEDIA_TYPES:
            continue
        content = item.get_content()
        source_file = item.get_name()
        source_member = _source_member_path(package_root, source_file)
        cleanup_rule = _matching_source_rule(
            rules.exact_text_fragments, source_file, source_member
        )
        exact_text_fragments = ()
        if cleanup_rule is not None:
            configured_fragments = rules.exact_text_fragments[cleanup_rule]
            fragment_counts = _exact_text_fragment_counts(content, configured_fragments)
            invalid_fragments = [
                (fragment, count)
                for fragment, count in fragment_counts.items()
                if count != 1
            ]
            if invalid_fragments:
                for fragment, count in invalid_fragments:
                    override_errors.append(
                        f"exact text cleanup fragment must occur exactly once in "
                        f"{source_member}, found {count}: {fragment!r}"
                    )
            else:
                exact_text_fragments = configured_fragments
                seen_override_files.add(cleanup_rule)
                applied_content_cleanups.extend(
                    {
                        "source_member": source_member,
                        "fragment": fragment,
                        "characters_removed": len(fragment),
                    }
                    for fragment in configured_fragments
                )
        if exact_text_fragments:
            paragraphs = extract_paragraphs(
                content, rules.exact_boilerplate, exact_text_fragments
            )
        elif rules.exact_boilerplate:
            paragraphs = extract_paragraphs(content, rules.exact_boilerplate)
        else:
            paragraphs = extract_paragraphs(content)
        if not paragraphs:
            continue
        try:
            paragraphs, split_page = consolidate_split_page_paragraphs(
                paragraphs, source_member
            )
        except ExtractionError as exc:
            override_errors.append(str(exc))
            split_page = None
        full_text = "\n\n".join(paragraphs)
        heading = paragraphs[0]
        classification, number = classify_document_heading(heading)
        source_visible = source_visible_character_count(
            content, rules.exact_boilerplate, exact_text_fragments
        )
        if split_page:
            source_visible -= (
                sum(
                    len(re.sub(r"\s+", "", original))
                    for original in split_page["original_headings"]
                )
                - len(re.sub(r"\s+", "", split_page["heading"]))
            )
        extracted_visible = extracted_visible_character_count(paragraphs)
        coverage = min(1.0, extracted_visible / source_visible) if source_visible else 1.0
        source_visible_total += source_visible
        extracted_visible_total += extracted_visible
        if source_visible and coverage < MIN_VISIBLE_TEXT_COVERAGE:
            content_loss_documents.append({
                "source_file": source_file,
                "source_member": source_member,
                "source_visible_characters": source_visible,
                "extracted_visible_characters": extracted_visible,
                "coverage": coverage,
            })

        chapter_rule = _matching_source_rule(
            rules.chapter_numbers, source_file, source_member
        )
        supplement_rule = _matching_source_rule(
            rules.supplements, source_file, source_member
        )
        source_override = None
        if chapter_rule is not None:
            seen_override_files.add(chapter_rule)
            if classification in ("extra", "meta", "chapter_range"):
                override_errors.append(
                    f"source override cannot reclassify {classification} document: "
                    f"{source_member}"
                )
            else:
                source_override = {
                    "source_file": source_file,
                    "original_heading": heading,
                    "original_detected_chapter": (
                        number if classification == "chapter" else None
                    ),
                    "corrected_chapter": rules.chapter_numbers[chapter_rule],
                }
                classification = "chapter"
                number = rules.chapter_numbers[chapter_rule]
                applied_overrides.append(source_override)

        document = {
            "heading": heading,
            "source_file": source_file,
            "source_member": source_member,
            "spine_position": spine_position,
            "source_document_sha256": hashlib.sha256(content).hexdigest(),
            "paragraph_count": len(paragraphs),
            "character_count": len(full_text),
            "text": full_text,
            "source_visible_characters": source_visible,
            "extracted_visible_characters": extracted_visible,
            "content_coverage": coverage,
        }

        if supplement_rule is not None:
            seen_override_files.add(supplement_rule)
            expected_heading = rules.supplements[supplement_rule]
            if heading.strip() != expected_heading:
                override_errors.append(
                    f"supplement heading mismatch for {source_member}: expected "
                    f"{expected_heading!r}, found {heading.strip()!r}"
                )
                unmatched_documents.append({
                    **document, "file": source_file, "preview": full_text[:500],
                })
                continue
            if classification in ("chapter", "chapter_range", "extra"):
                override_errors.append(
                    "numbered/extra document cannot be reclassified as a supplement: "
                    f"{source_member}"
                )
                continue
            if numbering_mode != CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
                override_errors.append(
                    "translatable supplements require spine_sequential numbering: "
                    f"{source_member}"
                )
                continue
            production_index = len(chapters) + 1
            unit = {
                "chapter": production_index,
                "production_index": production_index,
                "unit_kind": UNIT_KIND_SUPPLEMENT,
                "volume_index": volume_index,
                "heading": heading.strip(),
                "title": heading.strip(),
                **{key: document[key] for key in (
                    "source_file", "source_member", "spine_position",
                    "source_document_sha256", "paragraph_count", "character_count",
                    "text", "source_visible_characters",
                    "extracted_visible_characters", "content_coverage",
                )},
            }
            chapters.append(unit)
            supplement_documents.append({
                "production_index": production_index,
                "heading": heading.strip(),
                "source_file": source_file,
                "source_member": source_member,
                "spine_position": spine_position,
                "volume_index": volume_index,
            })
            continue

        if classification == "extra":
            document["extra_number"] = number
            extra_documents.append(document)
            continue
        if classification == "meta":
            meta_documents.append(document)
            continue
        if classification == "unmatched":
            unmatched_documents.append({
                **document, "file": source_file, "preview": full_text[:500],
            })
            continue
        if (
            classification == "chapter_range"
            and numbering_mode != CHAPTER_NUMBERING_SPINE_SEQUENTIAL
        ):
            unmatched_documents.append({
                **document, "file": source_file, "preview": full_text[:500],
            })
            override_errors.append(
                f"combined chapter heading requires spine_sequential mode: {source_member}"
            )
            continue

        if classification == "chapter_range":
            local_start, local_end = number
        else:
            local_start = local_end = number
        cleaned_paragraphs = [
            paragraph for paragraph in paragraphs
            if not _is_confirmed_boilerplate(paragraph, rules.exact_boilerplate)
        ]
        chapter_text = "\n\n".join(cleaned_paragraphs)
        if len(cleaned_paragraphs) < 2:
            override_errors.append(
                f"numbered chapter has no extracted body text: {source_member}"
            )

        provenance = {
            "source_file": source_file,
            "source_member": source_member,
            "spine_position": spine_position,
            "source_document_sha256": hashlib.sha256(content).hexdigest(),
            "paragraph_count": len(cleaned_paragraphs),
            "character_count": len(chapter_text),
            "text": chapter_text,
            "source_visible_characters": source_visible,
            "extracted_visible_characters": extracted_visible,
            "content_coverage": coverage,
        }
        if numbering_mode == CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
            production_index = len(chapters) + 1
            is_volume_reset = (
                previous_local_end is not None
                and previous_local_end > 1
                and local_start == 1
            )
            if is_volume_reset:
                volume_index += 1
                volume_boundaries.append({
                    "volume_index": volume_index,
                    "production_index": production_index,
                    "source_file": source_file,
                    "heading": heading,
                    "previous_local_chapter_end": previous_local_end,
                })
            elif previous_local_end is not None and local_start != previous_local_end + 1:
                local_numbering_warnings.append({
                    "production_index": production_index,
                    "volume_index": volume_index,
                    "source_file": source_file,
                    "heading": heading,
                    "previous_local_chapter_end": previous_local_end,
                    "local_chapter_start": local_start,
                })

            chapter = {
                "chapter": production_index,
                "production_index": production_index,
                "unit_kind": UNIT_KIND_CHAPTER,
                "local_chapter_number": local_start,
                "local_chapter_start": local_start,
                "local_chapter_end": local_end,
                "volume_index": volume_index,
                "heading": heading,
                "title": chapter_heading_title(heading),
                **provenance,
                **({"source_override": source_override} if source_override else {}),
                **({"split_page": split_page} if split_page else {}),
            }
            chapters.append(chapter)
            if split_page:
                combined_chapter_documents.append({
                    "production_index": production_index,
                    "source_file": source_file,
                    "source_member": source_member,
                    "heading": heading,
                    "local_chapter_start": local_start,
                    "local_chapter_end": local_end,
                    "split_page_count": split_page["page_count"],
                    "original_headings": split_page["original_headings"],
                    "internal_chapter_headings": [],
                })
            if local_end > local_start:
                internal_headings = []
                for paragraph_number, paragraph in enumerate(cleaned_paragraphs[1:], 2):
                    if paragraph.strip() == heading.strip():
                        continue
                    internal_classification, internal_number = classify_document_heading(
                        paragraph
                    )
                    if internal_classification in ("chapter", "chapter_range"):
                        internal_headings.append({
                            "paragraph": paragraph_number,
                            "heading": paragraph,
                            "classification": internal_classification,
                            "number": internal_number,
                        })
                combined = {
                    "production_index": production_index,
                    "source_file": source_file,
                    "heading": heading,
                    "local_chapter_start": local_start,
                    "local_chapter_end": local_end,
                    "internal_chapter_headings": internal_headings,
                }
                combined_chapter_documents.append(combined)
                if internal_headings:
                    override_errors.append(
                        "combined chapter document contains internal chapter headings "
                        f"and requires manual structural review: {source_member}"
                    )
            previous_local_end = local_end
        else:
            chapters.append({
                "chapter": number,
                "heading": heading,
                **provenance,
                **({"source_override": source_override} if source_override else {}),
                **({"split_page": split_page} if split_page else {}),
            })

    if numbering_mode == CHAPTER_NUMBERING_GLOBAL:
        chapters.sort(key=lambda item: item["chapter"])
    missing, configured_missing, duplicates, errors = _validate_chapters(
        chapters, project.config.final_chapter, numbering_mode
    )
    unapplied_override_files = tuple(sorted(
        configured_rule_paths - seen_override_files - set(unknown_override_files)
    ))
    errors = list(errors)
    if unknown_override_files:
        errors.append(
            "source overrides reference unknown EPUB members: "
            + ", ".join(unknown_override_files)
        )
    if unapplied_override_files:
        errors.append(
            "source overrides were not applied to spine documents: "
            + ", ".join(unapplied_override_files)
        )
    errors.extend(override_errors)
    if content_loss_documents:
        errors.append(
            "severe source/extracted visible-text loss detected in: "
            + ", ".join(item["source_member"] for item in content_loss_documents)
        )
    hangul_count = sum(len(HANGUL_RE.findall(unit["text"])) for unit in chapters)
    return ExtractionResult(
        chapters=chapters,
        source=source,
        source_sha256=source_hash,
        numbering_mode=numbering_mode,
        missing_chapters=missing,
        configured_missing_chapters=configured_missing,
        duplicate_chapters=duplicates,
        applied_overrides=tuple(applied_overrides),
        applied_content_cleanups=tuple(applied_content_cleanups),
        unknown_override_files=unknown_override_files,
        unapplied_override_files=unapplied_override_files,
        extra_documents=tuple(extra_documents),
        meta_documents=tuple(meta_documents),
        unmatched_documents=tuple(unmatched_documents),
        supplement_documents=tuple(supplement_documents),
        combined_chapter_documents=tuple(combined_chapter_documents),
        volume_boundaries=tuple(volume_boundaries),
        local_numbering_warnings=tuple(local_numbering_warnings),
        suspicious_hangul_characters=hangul_count,
        source_visible_characters=source_visible_total,
        extracted_visible_characters=extracted_visible_total,
        content_coverage=(
            min(1.0, extracted_visible_total / source_visible_total)
            if source_visible_total else 1.0
        ),
        content_loss_documents=tuple(content_loss_documents),
        validation_errors=tuple(errors),
    )


def _existing_library_is_populated(path):
    path = Path(path)
    if not path.exists():
        return False
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return path.stat().st_size > 0
    return current != []



def _backup_existing_library(project, output):
    content = output.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    backup_dir = _assert_project_path(
        project, project.backup_dir / "source_extraction", "chapter-library backup"
    )
    backup = backup_dir / f"chapters_{digest}.json"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if backup.read_bytes() != content:
            raise ExtractionError(f"existing backup does not match prior library: {backup}")
    else:
        atomic_write_bytes(backup, content)
    return backup


def install_extraction(project, result, overwrite=False):
    """Atomically install one validated result into only the selected project."""
    output = _assert_project_path(project, project.chapters_file, "chapter library")
    if result.validation_errors:
        raise ExtractionValidationError(result.validation_errors, result)
    populated = _existing_library_is_populated(output)
    if populated and not overwrite:
        raise FileExistsError(
            f"chapter library is already populated: {output}; rerun with --overwrite"
        )

    backup = None
    if output.exists() and populated:
        backup = _backup_existing_library(project, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result.chapters, ensure_ascii=False, indent=2)
    # Round-trip the exact payload before replacing a valid existing library.
    decoded = json.loads(serialized)
    _, _, _, errors = _validate_chapters(
        decoded, project.config.final_chapter, project.config.chapter_numbering
    )
    if errors:
        raise ExtractionValidationError(errors, result)
    atomic_write_text(output, serialized)
    return output, backup


def _format_numbers(numbers):
    if not numbers:
        return "None"
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def print_extraction_report(project, result, output=None, backup=None, status=None):
    print()
    print("===== EPUB EXTRACTION REPORT =====")
    print(f"Source: {result.source}")
    print(f"Source SHA-256: {result.source_sha256}")
    print(f"Novel ID: {project.config.id}")
    print(f"Chapter numbering mode: {result.numbering_mode}")
    print(f"Configured final chapter: {project.config.final_chapter}")
    print(f"Translatable production units recognized: {len(result.chapters)}")
    print(f"Ordinary chapter units: {len(result.ordinary_chapters)}")
    print(f"Supplement units: {len(result.supplements)}")
    print(f"Unique production indexes: {result.unique_chapter_count}")
    print(
        "First production index: "
        f"{result.first_chapter if result.first_chapter else 'None'}"
    )
    print(
        "Last production index: "
        f"{result.last_chapter if result.last_chapter else 'None'}"
    )
    print(f"First recognized heading: {result.first_heading or 'None'}")
    print(f"Last recognized heading: {result.last_heading or 'None'}")
    print(f"Missing within detected range: {_format_numbers(result.missing_chapters)}")
    print(
        "Missing against configured final: "
        f"{_format_numbers(result.configured_missing_chapters)}"
    )
    print(f"Duplicate production indexes: {_format_numbers(result.duplicate_chapters)}")
    if result.continuous_from_one:
        print(f"Continuous numbered sequence: 1 through {result.last_chapter}")
    else:
        print("Continuous numbered sequence: no")
    if result.numbering_mode == CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
        local_numbers = tuple(
            number for number in result.local_chapter_numbers if number is not None
        )
        print(
            "Visible local chapter range: "
            + (
                f"{min(local_numbers)}-{max(local_numbers)}"
                if local_numbers else "None"
            )
        )
        print(f"Detected local-number resets: {len(result.volume_boundaries)}")
        print(
            "Inferred numeric volume/arc indexes: "
            f"{len(result.volume_boundaries) + 1 if result.chapters else 0}"
        )
        print(
            "Suspicious local-number transitions: "
            f"{len(result.local_numbering_warnings)}"
        )
        print(
            "Combined chapter documents: "
            f"{len(result.combined_chapter_documents)}"
        )
    print(f"Applied source overrides: {len(result.applied_overrides)}")
    print(f"Applied exact text cleanups: {len(result.applied_content_cleanups)}")
    print(
        "Unknown override source files: "
        + (", ".join(result.unknown_override_files) or "None")
    )
    print(
        "Unapplied override source files: "
        + (", ".join(result.unapplied_override_files) or "None")
    )
    print(f"Extra/side-story documents: {len(result.extra_documents)}")
    print(f"Meta documents: {len(result.meta_documents)}")
    print(f"Unmatched spine documents: {len(result.unmatched_documents)}")
    print(f"Suspicious Hangul characters: {result.suspicious_hangul_characters:,}")
    print(f"Source visible characters: {result.source_visible_characters:,}")
    print(f"Extracted visible characters: {result.extracted_visible_characters:,}")
    print(f"Visible-text coverage: {result.content_coverage:.2%}")
    print(f"Severe content-loss documents: {len(result.content_loss_documents)}")
    if output is not None:
        print(f"Output path: {output}")
    if backup is not None:
        print(f"Prior library backup: {backup}")
    if status:
        print(f"Status: {status}")
    if result.validation_errors:
        print("Install blockers: " + "; ".join(result.validation_errors))
    print("==================================")
    if result.applied_overrides:
        print("\nApplied source chapter-number overrides:")
        for override in result.applied_overrides:
            original = override["original_detected_chapter"]
            print(
                f"\nFILE: {override['source_file']}\n"
                f"ORIGINAL HEADING: {override['original_heading']}\n"
                f"ORIGINAL DETECTED CHAPTER: "
                f"{original if original is not None else 'None'}\n"
                f"CORRECTED CHAPTER: {override['corrected_chapter']}"
            )
    if result.applied_content_cleanups:
        print("\nApplied exact source-text cleanups:")
        for cleanup in result.applied_content_cleanups:
            print(
                f"\nSOURCE MEMBER: {cleanup['source_member']}\n"
                f"REMOVED EXACT FRAGMENT: {cleanup['fragment']}\n"
                f"CHARACTERS REMOVED: {cleanup['characters_removed']}"
            )
    if result.supplement_documents:
        print("\nTranslatable supplemental documents:")
        for document in result.supplement_documents:
            print(
                f"\nPRODUCTION INDEX: {document['production_index']}\n"
                f"SOURCE MEMBER: {document['source_member']}\n"
                f"SPINE POSITION: {document['spine_position']}\n"
                f"HEADING: {document['heading']}"
            )
    if result.combined_chapter_documents:
        print("\nCombined chapter documents (one production unit each):")
        for document in result.combined_chapter_documents:
            print(
                f"\nPRODUCTION INDEX: {document['production_index']}\n"
                f"FILE: {document['source_file']}\n"
                f"LOCAL RANGE: {document['local_chapter_start']}-"
                f"{document['local_chapter_end']}\n"
                f"HEADING: {document['heading']}\n"
                "INTERNAL CHAPTER BOUNDARIES: "
                + (
                    str(len(document['internal_chapter_headings']))
                    if document['internal_chapter_headings'] else "None"
                )
            )
    if result.local_numbering_warnings:
        print("\nSuspicious local-number transitions (non-blocking):")
        for warning in result.local_numbering_warnings:
            print(
                f"\nPRODUCTION INDEX: {warning['production_index']}\n"
                f"FILE: {warning['source_file']}\n"
                f"PREVIOUS LOCAL END: {warning['previous_local_chapter_end']}\n"
                f"CURRENT LOCAL START: {warning['local_chapter_start']}\n"
                f"HEADING: {warning['heading']}"
            )
    if result.extra_documents:
        print("\nExtra/side-story documents (not installed as main chapters):")
        for document in result.extra_documents:
            number = document["extra_number"] or "un-numbered"
            print(
                f"\nFILE: {document['source_file']}\n"
                f"EXTRA: {number}\nHEADING: {document['heading']}"
            )
    if result.meta_documents:
        print("\nMeta documents (not installed as main chapters):")
        for document in result.meta_documents:
            print(
                f"\nFILE: {document['source_file']}\n"
                f"HEADING: {document['heading']}"
            )
    if result.unmatched_documents:
        print("\nUnmatched documents:")
        for document in result.unmatched_documents:
            print(f"\nFILE: {document['file']}")
            print(f"PREVIEW: {document['preview'][:200]}")


def extract_epub(project, overwrite=False):
    """Extract, validate, back up if needed, and atomically install chapters."""
    result = extract_source_epub(project)
    try:
        output, backup = install_extraction(project, result, overwrite=overwrite)
    except ExtractionValidationError:
        print_extraction_report(project, result, status="NOT INSTALLED")
        raise
    print_extraction_report(
        project, result, output=output, backup=backup, status="INSTALLED"
    )
    return result


@dataclass(frozen=True)
class _DryRunConfig:
    id: str
    profile: str
    final_chapter: int
    chapter_numbering: str


class _DryRunProject:
    """Minimal project view for analysing an EPUB that is not installed yet.

    It exposes only what extract_source_epub reads: the source path, the
    (absent) overrides file, the profile and the numbering settings.
    """

    is_legacy = False

    def __init__(self, source, *, final_chapter, chapter_numbering, profile, repo_root):
        source = Path(source).resolve()
        self.root = source.parent
        self.source_epub = source
        self.source_overrides_file = source.parent / (source.name + ".no-overrides.json")
        self.repository_root = Path(repo_root)
        self.config = _DryRunConfig(
            "dry-run", profile, final_chapter, chapter_numbering
        )


def dry_run_extract_epub(
    source,
    *,
    chapter_numbering=CHAPTER_NUMBERING_GLOBAL,
    final_chapter=10_000_000,
    profile="historical_military",
    repo_root=None,
):
    """Extract an EPUB in memory without an installed project or any writes.

    The default final_chapter is deliberately huge, so only the errors that
    mention final_chapter are artefacts of the placeholder.
    """
    repo_root = repo_root or Path(__file__).resolve().parent
    project = _DryRunProject(
        source,
        final_chapter=final_chapter,
        chapter_numbering=chapter_numbering,
        profile=profile,
        repo_root=repo_root,
    )
    return extract_source_epub(project)


def build_parser():
    parser = argparse.ArgumentParser(description="Extract a source EPUB for one novel")
    parser.add_argument(
        "--novel", required=True, help="novel identifier under novels/"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace a populated chapters.json after preserving a local backup",
    )
    return parser


def configure_cli_text_streams():
    """Keep Unicode extraction reports printable on legacy Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            # A redirected or test-owned stream may not permit reconfiguration.
            # Reporting still follows its existing behavior in that environment.
            continue


def main(argv=None):
    configure_cli_text_streams()
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            extract_epub(project, overwrite=args.overwrite)
    except (
        FileNotFoundError,
        ValueError,
        OSError,
        ExtractionError,
        ProductionRunLockedError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
