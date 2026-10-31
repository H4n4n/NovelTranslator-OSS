"""Generic, local-only UTF-8 TXT extraction into canonical chapters.json.

TXT structure ends at the same production-unit boundary as EPUB extraction.
This module is import-safe: it performs no I/O, network access, or project
mutation until an explicit extraction/install function is called.
"""

import argparse
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import uuid

from chapter_library import (
    UNIT_KIND_CHAPTER,
    UNIT_KIND_EPILOGUE,
    UNIT_KIND_PROLOGUE,
    UNIT_KIND_SIDE_STORY,
    validate_chapter_library,
)
import unicodedata

from extract_epub import CHINESE_NUMERAL_CHARACTERS, parse_chinese_integer
from novel_project import (
    CHAPTER_NUMBERING_SPINE_SEQUENTIAL,
    SOURCE_TYPE_TXT,
    select_project,
)
from production_run_lock import ProductionRunLockedError, production_run_lock


MIN_NARRATIVE_COVERAGE = 0.85
PROLOGUE_RE = re.compile(r"^(?:Prologue|프롤로그)$", re.IGNORECASE)
NUMBERED_DOT_HEADING_RE = re.compile(
    r"^(?P<number>\d+)\s*[화회]\s*\.\s*(?P<title>.*)$"
)
NUMBERED_ANGLE_HEADING_RE = re.compile(
    r"^(?P<number>\d+)\s*[화회]\s+(?P<title><.+>)\s*$"
)
NUMBERED_PLAIN_HWA_HEADING_RE = re.compile(
    r"^(?P<number>\d+)\s*\uD654\s+(?P<title>.+?)\s*$"
)
TWO_LINE_NUMBER_RE = re.compile(r"^제\s*(?P<number>\d+)\s*[화회]\s*$")
_ZH_NUMERAL = rf"(?:[0-9０-９]+|[{CHINESE_NUMERAL_CHARACTERS}]+)"
ZH_NUMBERED_HEADING_RE = re.compile(
    rf"^第\s*(?P<number>{_ZH_NUMERAL})\s*[章回节節]\s*[:：\-—、.．]?\s*(?P<title>.*)$"
)
ZH_NAMED_HEADING_RE = re.compile(
    rf"^(?P<name>序章|序幕|楔子|番外|尾声|尾聲|后记|後記)\s*(?:{_ZH_NUMERAL})?"
    r"(?:$|(?:\s+|\s*[:：\-—、.．]\s*)(?P<title>.+)$)"
)
ZH_MAX_HEADING_CHARS = 40
ZH_PROSE_END = "。！？!?…”」』"
ZH_KIND_BY_NAME = {
    "序章": UNIT_KIND_PROLOGUE, "序幕": UNIT_KIND_PROLOGUE, "楔子": UNIT_KIND_PROLOGUE,
    "番外": UNIT_KIND_SIDE_STORY,
    "尾声": UNIT_KIND_EPILOGUE, "尾聲": UNIT_KIND_EPILOGUE,
    "后记": UNIT_KIND_EPILOGUE, "後記": UNIT_KIND_EPILOGUE,
}
HANGUL_RE = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
COMPLETION_MARKER_RE = re.compile(r"^<\s*완결\s*>$")
KIND_VALUES = frozenset((
    UNIT_KIND_CHAPTER,
    UNIT_KIND_PROLOGUE,
    UNIT_KIND_EPILOGUE,
    UNIT_KIND_SIDE_STORY,
))
LINE_CLASSIFICATIONS = frozenset((
    "metadata",
    "end_marker",
    "author_afterword_start",
))


class TXTExtractionError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with Path(path).open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def visible_character_count(text):
    """Count non-whitespace source characters for normalization-safe coverage."""
    return len(re.sub(r"\s", "", text or ""))


def inspect_utf8_txt(path):
    path = Path(path)
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TXTExtractionError(f"TXT source is not valid UTF-8: {exc}") from exc
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    bare_cr = raw.count(b"\r") - crlf
    styles = []
    if crlf:
        styles.append("CRLF")
    if bare_lf:
        styles.append("LF")
    if bare_cr:
        styles.append("CR")
    newline_style = "+".join(styles) if styles else "none"
    return {
        "raw": raw,
        "text": text,
        "source_sha256": sha256_bytes(raw),
        "byte_count": len(raw),
        "character_count": len(text),
        "has_utf8_bom": has_bom,
        "encoding": "UTF-8 with BOM" if has_bom else "UTF-8",
        "newline_style": newline_style,
        "line_record_count": len(text.splitlines()),
        "logical_line_count": len(text.split("\n")),
        "has_terminal_newline": text.endswith(("\n", "\r")),
    }


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TXTExtractionError(f"invalid source overrides JSON: {exc}") from exc


def load_txt_overrides(project, source_sha256, line_count):
    """Load exact, SHA-bound TXT rules; absence means no source-specific rules."""
    path = project.source_overrides_file
    if not path.is_file():
        return {"line_classifications": [], "heading_overrides": []}
    data = _load_json(path)
    if not isinstance(data, dict):
        raise TXTExtractionError("source_overrides.json must contain an object")
    configured_hash = data.get("source_sha256")
    if configured_hash != source_sha256:
        raise TXTExtractionError(
            "source_overrides.json is stale: source_sha256 does not match novel.txt"
        )
    raw_txt = data.get("txt", {})
    if not isinstance(raw_txt, dict):
        raise TXTExtractionError("source_overrides.json txt must be an object")
    unknown = sorted(set(raw_txt) - {"line_classifications", "heading_overrides"})
    if unknown:
        raise TXTExtractionError(
            "unsupported TXT override fields: " + ", ".join(unknown)
        )

    result = {"line_classifications": [], "heading_overrides": []}
    seen_lines = set()
    for index, rule in enumerate(raw_txt.get("line_classifications", [])):
        if not isinstance(rule, dict):
            raise TXTExtractionError(f"TXT line classification {index} must be an object")
        if set(rule) != {"line", "expected_text", "classification"}:
            raise TXTExtractionError(
                f"TXT line classification {index} has unsupported or missing fields"
            )
        line = rule["line"]
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= line_count:
            raise TXTExtractionError(f"TXT line classification {index} has invalid line")
        if line in seen_lines:
            raise TXTExtractionError(f"duplicate TXT override for line {line}")
        seen_lines.add(line)
        if rule["classification"] not in LINE_CLASSIFICATIONS:
            raise TXTExtractionError(
                f"TXT line {line} has unsupported classification {rule['classification']!r}"
            )
        if not isinstance(rule["expected_text"], str):
            raise TXTExtractionError(f"TXT line {line} expected_text must be a string")
        result["line_classifications"].append(dict(rule))

    for index, rule in enumerate(raw_txt.get("heading_overrides", [])):
        if not isinstance(rule, dict):
            raise TXTExtractionError(f"TXT heading override {index} must be an object")
        allowed = {
            "line", "expected_text", "unit_kind", "source_number", "title",
            "title_line", "expected_title_text",
        }
        unknown = sorted(set(rule) - allowed)
        required = {"line", "expected_text", "unit_kind"}
        if unknown or not required.issubset(rule):
            raise TXTExtractionError(
                f"TXT heading override {index} has unsupported or missing fields"
            )
        line = rule["line"]
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= line_count:
            raise TXTExtractionError(f"TXT heading override {index} has invalid line")
        if line in seen_lines:
            raise TXTExtractionError(f"duplicate TXT override for line {line}")
        seen_lines.add(line)
        if rule["unit_kind"] not in KIND_VALUES:
            raise TXTExtractionError(f"TXT heading override {index} has invalid unit_kind")
        number = rule.get("source_number")
        if number is not None and (
            not isinstance(number, int) or isinstance(number, bool) or number < 1
        ):
            raise TXTExtractionError(f"TXT heading override {index} has invalid source_number")
        result["heading_overrides"].append(dict(rule))
    return result


def _trim_title(raw):
    title = (raw or "").strip()
    if title.startswith("<") and title.endswith(">"):
        title = title[1:-1].strip()
    return title


def _kind_from_title(title):
    compact = (title or "").strip()
    if "에필로그" in compact:
        return UNIT_KIND_EPILOGUE
    if "외전" in compact:
        return UNIT_KIND_SIDE_STORY
    return UNIT_KIND_CHAPTER


def _normalize_body(lines):
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    normalized = []
    previous_blank = False
    for line in cleaned:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        normalized.append("" if blank else line)
        previous_blank = blank
    return "\n".join(normalized)


@dataclass(frozen=True)
class Heading:
    line: int
    end_line: int
    raw_heading: str
    title: str
    unit_kind: str
    source_number: int | None
    heading_format: str


def detect_heading_script(text):
    """Return "ko" or "zh" so Korean sources keep their exact patterns."""
    hangul = len(HANGUL_RE.findall(text))
    cjk = len(CJK_RE.findall(text))
    return "ko" if hangul and hangul >= 0.3 * (hangul + cjk) else "zh"


def _recognize_chinese_heading(lines, line_number):
    raw = lines[line_number - 1]
    line = raw.strip()
    if not line or len(line) > ZH_MAX_HEADING_CHARS or line[-1] in ZH_PROSE_END:
        return None
    normalized = unicodedata.normalize("NFKC", line)
    match = ZH_NUMBERED_HEADING_RE.fullmatch(normalized)
    if match is not None:
        numeral = match.group("number")
        number = int(numeral) if numeral.isdigit() else None
        if number is None:
            try:
                number = parse_chinese_integer(numeral)
            except ValueError:
                return None
        title = match.group("title").strip() or line
        return Heading(
            line_number, line_number, raw, title, UNIT_KIND_CHAPTER, number,
            "zh_numbered",
        )
    match = ZH_NAMED_HEADING_RE.fullmatch(normalized)
    if match is not None:
        title = (match.group("title") or "").strip() or match.group("name")
        return Heading(
            line_number, line_number, raw, title,
            ZH_KIND_BY_NAME[match.group("name")], None, "zh_named",
        )
    return None


def _recognize_heading(lines, line_number, heading_overrides, script="ko"):
    line = lines[line_number - 1].strip()
    override = heading_overrides.get(line_number)
    if override is not None:
        if lines[line_number - 1] != override["expected_text"]:
            raise TXTExtractionError(
                f"TXT heading override expected text mismatch at line {line_number}"
            )
        title = override.get("title")
        end_line = line_number
        title_line = override.get("title_line")
        if title_line is not None:
            if not isinstance(title_line, int) or not 1 <= title_line <= len(lines):
                raise TXTExtractionError(f"TXT heading override line {line_number} has invalid title_line")
            expected_title = override.get("expected_title_text")
            if expected_title is None or lines[title_line - 1] != expected_title:
                raise TXTExtractionError(
                    f"TXT heading override title text mismatch at line {title_line}"
                )
            title = expected_title
            end_line = title_line
        title = _trim_title(title if title is not None else line)
        return Heading(
            line_number, end_line, line, title, override["unit_kind"],
            override.get("source_number"), "source_override",
        )
    if script == "zh":
        return _recognize_chinese_heading(lines, line_number)
    if PROLOGUE_RE.fullmatch(line):
        return Heading(
            line_number, line_number, lines[line_number - 1], line,
            UNIT_KIND_PROLOGUE, None, "prologue",
        )
    match = NUMBERED_DOT_HEADING_RE.fullmatch(line)
    heading_format = "number_dot"
    if match is None:
        match = NUMBERED_ANGLE_HEADING_RE.fullmatch(line)
        heading_format = "number_angle"
    if match is None:
        match = NUMBERED_PLAIN_HWA_HEADING_RE.fullmatch(line)
        heading_format = "number_plain_hwa"
    if match is not None:
        title = _trim_title(match.group("title"))
        number = int(match.group("number"))
        if not title:
            title = lines[line_number - 1].strip()
        return Heading(
            line_number, line_number, lines[line_number - 1], title,
            _kind_from_title(title), number, heading_format,
        )
    match = TWO_LINE_NUMBER_RE.fullmatch(line)
    if match is not None:
        title_line = line_number + 1
        while title_line <= len(lines) and not lines[title_line - 1].strip():
            title_line += 1
        if title_line > len(lines):
            raise TXTExtractionError(
                f"two-line chapter heading at line {line_number} has no title"
            )
        title = lines[title_line - 1].strip()
        if (
            PROLOGUE_RE.fullmatch(title)
            or NUMBERED_DOT_HEADING_RE.fullmatch(title)
            or NUMBERED_ANGLE_HEADING_RE.fullmatch(title)
            or NUMBERED_PLAIN_HWA_HEADING_RE.fullmatch(title)
            or TWO_LINE_NUMBER_RE.fullmatch(title)
        ):
            raise TXTExtractionError(
                f"two-line chapter heading at line {line_number} has no safe title"
            )
        number = int(match.group("number"))
        return Heading(
            line_number, title_line,
            lines[line_number - 1].rstrip() + "\n" + lines[title_line - 1].strip(), title,
            _kind_from_title(title), number, "two_line_je_number",
        )
    return None


def extract_txt_text(text, source_sha256, overrides=None, source_file="novel.txt"):
    """Parse decoded UTF-8 text and return units plus a complete local audit."""
    if not isinstance(text, str):
        raise TypeError("decoded TXT source must be a string")
    lines = text.splitlines()
    if not lines:
        raise TXTExtractionError("TXT source is empty")
    script = detect_heading_script(text)
    overrides = overrides or {"line_classifications": [], "heading_overrides": []}
    line_rules = {}
    for rule in overrides.get("line_classifications", []):
        line = rule["line"]
        if lines[line - 1] != rule["expected_text"]:
            raise TXTExtractionError(
                f"TXT line override expected text mismatch at line {line}"
            )
        line_rules[line] = rule["classification"]
    heading_overrides = {
        item["line"]: item for item in overrides.get("heading_overrides", [])
    }

    headings = []
    excluded = []
    consumed_title_lines = set()
    afterword_start = None
    for line_number in range(1, len(lines) + 1):
        classification = line_rules.get(line_number)
        if classification == "author_afterword_start":
            afterword_start = line_number
            excluded.append({
                "classification": classification,
                "line_start": line_number,
                "line_end": len(lines),
                "text": "\n".join(lines[line_number - 1:]),
            })
            break
        if classification in {"metadata", "end_marker"}:
            excluded.append({
                "classification": classification,
                "line_start": line_number,
                "line_end": line_number,
                "text": lines[line_number - 1],
            })
            continue
        if line_number in consumed_title_lines:
            continue
        if COMPLETION_MARKER_RE.fullmatch(lines[line_number - 1].strip()):
            excluded.append({
                "classification": "completion_marker",
                "line_start": line_number,
                "line_end": line_number,
                "text": lines[line_number - 1],
            })
            continue
        heading = _recognize_heading(lines, line_number, heading_overrides, script)
        if heading is not None:
            headings.append(heading)
            consumed_title_lines.update(range(line_number + 1, heading.end_line + 1))
            excluded.append({
                "classification": "heading",
                "line_start": heading.line,
                "line_end": heading.end_line,
                "text": "\n".join(lines[heading.line - 1:heading.end_line]),
            })

    if not headings:
        raise TXTExtractionError("TXT source contains no recognized production headings")
    headings.sort(key=lambda item: item.line)
    if len({item.line for item in headings}) != len(headings):
        raise TXTExtractionError("TXT source produced duplicate heading boundaries")

    excluded_lines = set()
    for item in excluded:
        excluded_lines.update(range(item["line_start"], item["line_end"] + 1))
    units = []
    candidate_visible = 0
    extracted_visible = 0
    for index, heading in enumerate(headings, 1):
        next_line = (
            headings[index].line if index < len(headings)
            else (afterword_start or len(lines) + 1)
        )
        body_line_numbers = [
            number
            for number in range(heading.end_line + 1, next_line)
            if number not in excluded_lines
        ]
        body_lines = [lines[number - 1] for number in body_line_numbers]
        candidate = "\n".join(body_lines)
        body = _normalize_body(body_lines)
        if not body.strip():
            raise TXTExtractionError(
                f"production unit at source line {heading.line} has an empty body"
            )
        candidate_visible += visible_character_count(candidate)
        extracted_visible += visible_character_count(body)
        unit = {
            "chapter": index,
            "production_index": index,
            "unit_kind": heading.unit_kind,
            "volume_index": 1,
            "heading": heading.raw_heading,
            "title": heading.title,
            "source_heading": heading.raw_heading,
            "source_number": heading.source_number,
            "source_file": source_file,
            "source_line_start": heading.line,
            "source_line_end": next_line - 1,
            "heading_line_start": heading.line,
            "heading_line_end": heading.end_line,
            "heading_format": heading.heading_format,
            "text": heading.raw_heading + "\n\n" + body,
            "source_character_count": len(body),
            "paragraph_count": len([part for part in body.split("\n\n") if part.strip()]),
            "source_unit_sha256": sha256_bytes(body.encode("utf-8")),
            "source_document_sha256": source_sha256,
        }
        if heading.unit_kind == UNIT_KIND_CHAPTER:
            if heading.source_number is None:
                raise TXTExtractionError(
                    f"ordinary chapter at line {heading.line} lacks a source number"
                )
            unit.update({
                "local_chapter_number": heading.source_number,
                "local_chapter_start": heading.source_number,
                "local_chapter_end": heading.source_number,
            })
        units.append(unit)

    coverage = extracted_visible / candidate_visible if candidate_visible else 0.0
    if coverage < MIN_NARRATIVE_COVERAGE:
        raise TXTExtractionError(
            f"narrative coverage {coverage:.2%} is below {MIN_NARRATIVE_COVERAGE:.0%}"
        )

    numeric = [unit["source_number"] for unit in units if unit["source_number"] is not None]
    counts = Counter(numeric)
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    present = set(numeric)
    missing = (
        sorted(set(range(min(numeric), max(numeric) + 1)) - present)
        if numeric else []
    )
    resets = sum(current < previous for previous, current in zip(numeric, numeric[1:]))
    body_sizes = [unit["source_character_count"] for unit in units]
    report = {
        "source_sha256": source_sha256,
        "source_file": source_file,
        "production_unit_count": len(units),
        "unit_kind_counts": dict(Counter(unit["unit_kind"] for unit in units)),
        "production_index_start": 1,
        "production_index_end": len(units),
        "source_number_start": min(numeric) if numeric else None,
        "source_number_end": max(numeric) if numeric else None,
        "source_number_duplicates": duplicates,
        "source_number_missing": missing,
        "source_number_resets": resets,
        "heading_format_counts": dict(Counter(unit["heading_format"] for unit in units)),
        "candidate_narrative_characters": candidate_visible,
        "extracted_narrative_characters": extracted_visible,
        "narrative_coverage": coverage,
        "explicitly_excluded_characters": sum(
            visible_character_count(item["text"]) for item in excluded
        ),
        "excluded_sections": excluded,
        "shortest_unit_characters": min(body_sizes),
        "median_unit_characters": statistics.median(body_sizes),
        "longest_unit_characters": max(body_sizes),
    }
    return {"chapters": units, "report": report}


def extract_source_txt(project):
    if project.config.source_type != SOURCE_TYPE_TXT:
        raise TXTExtractionError(
            f"novel {project.config.id} is configured for {project.config.source_type}, not txt"
        )
    if project.config.chapter_numbering != CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
        raise TXTExtractionError(
            "TXT sources with irregular reader numbering require spine_sequential"
        )
    source = inspect_utf8_txt(project.source_txt)
    overrides = load_txt_overrides(
        project, source["source_sha256"], source["line_record_count"]
    )
    result = extract_txt_text(
        source["text"], source["source_sha256"], overrides=overrides
    )
    errors = validate_chapter_library(
        result["chapters"],
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    if errors:
        raise TXTExtractionError("invalid extracted chapter library: " + "; ".join(errors))
    result["report"].update({
        key: value for key, value in source.items()
        if key not in {"raw", "text"}
    })
    result["report"]["override_count"] = sum(len(value) for value in overrides.values())
    return result


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def install_extraction(project, result, overwrite=False):
    """Atomically install a fully validated canonical library and inventory."""
    chapters = result["chapters"]
    errors = validate_chapter_library(
        chapters, project.config.final_chapter, project.config.chapter_numbering
    )
    if errors:
        raise TXTExtractionError("refusing to install invalid library: " + "; ".join(errors))
    existing = project.chapters_file.read_bytes() if project.chapters_file.exists() else b""
    if existing.strip() not in {b"", b"[]"} and not overwrite:
        raise TXTExtractionError(
            "chapter library is populated; pass --overwrite only after reviewing extraction"
        )
    if existing.strip() not in {b"", b"[]"}:
        backup = project.backup_dir / "source_extraction"
        backup.mkdir(parents=True, exist_ok=True)
        backup_path = backup / f"chapters-{sha256_bytes(existing)[:16]}.json"
        if not backup_path.exists():
            backup_path.write_bytes(existing)

    project.data_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    chapters_tmp = project.data_dir / f"chapters.json.{token}.tmp"
    inventory_path = project.data_dir / "source_inventory.json"
    inventory_tmp = project.data_dir / f"source_inventory.json.{token}.tmp"
    try:
        chapters_tmp.write_bytes(_json_bytes(chapters))
        inventory_tmp.write_bytes(_json_bytes(result["report"]))
        # Install the diagnostic first; the canonical library is the final
        # atomic operation and is therefore never left partially written.
        os.replace(inventory_tmp, inventory_path)
        os.replace(chapters_tmp, project.chapters_file)
    finally:
        for path in (chapters_tmp, inventory_tmp):
            if path.exists():
                path.unlink()
    return project.chapters_file


def _print_report(result, project, installed):
    report = result["report"]
    print("NovelTranslator TXT extraction (zero API calls)")
    print("=" * 56)
    print(f"Project: {project.config.id}")
    print(f"Source: {project.source_txt}")
    print(f"Source SHA-256: {report['source_sha256']}")
    print(f"Encoding: {report['encoding']}")
    print(f"Newlines: {report['newline_style']}")
    print(f"Production units: {report['production_unit_count']}")
    print(f"Unit kinds: {report['unit_kind_counts']}")
    print(f"Source-number gaps: {report['source_number_missing'] or 'none'}")
    print(f"Source-number duplicates: {report['source_number_duplicates'] or 'none'}")
    print(f"Narrative coverage: {report['narrative_coverage']:.2%}")
    print("Installed canonical library: " + ("YES" if installed else "NO (dry run)"))
    print("API calls used: 0")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Extract one configured UTF-8 TXT novel (zero API calls)"
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        lock = nullcontext() if args.dry_run else production_run_lock(project.root)
        with lock:
            result = extract_source_txt(project)
            if not args.dry_run:
                install_extraction(project, result, overwrite=args.overwrite)
        _print_report(result, project, installed=not args.dry_run)
    except (
        OSError,
        ValueError,
        TypeError,
        TXTExtractionError,
        ProductionRunLockedError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
