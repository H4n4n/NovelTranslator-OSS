"""Build a presentation-only, byte-preserving publication chapter corpus.

The default contract is deliberately specific to Rogue First Prince.  Parsing
and transformation helpers are generic and are exercised with small fixture
corpora in the test suite.  This module never imports the translation engine.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from production_run_lock import ProductionRunLockedError, production_run_lock
from proven_stage_cleanup import (
    cleanup_proof_id,
    cleanup_proven_stages,
    inspect_stage_cleanup,
)


SCHEMA_VERSION = 1
REVISION = "publication-cleanup-v1"
GEN_START = "#!/GEN_METHOD_TITLE_START"
GEN_END = "#!/GEN_METHOD_TITLE_END"

ROOT = Path(__file__).resolve().parent
DEFAULT_FINAL_DIR = ROOT / "novels" / "rogue_first_prince" / "final"
DEFAULT_SOURCE_DIR = DEFAULT_FINAL_DIR / "titled_chapters"
DEFAULT_OUTPUT_DIR = DEFAULT_FINAL_DIR / "publication_chapters"
DEFAULT_REPORT_JSON = DEFAULT_FINAL_DIR / "reports" / "publication_cleanup.json"
DEFAULT_REPORT_MD = DEFAULT_FINAL_DIR / "reports" / "publication_cleanup.md"

TRANSFORMATION_CATEGORIES = (
    "markdown_heading_wrapper",
    "whole_title_bold_wrapper",
    "whole_title_quote_wrapper",
    "angle_title_wrapper",
    "html_break_scaffold",
    "generator_title_wrapper",
    "split_scaffold_spacing",
)

ROGUE_EXPECTED_INVENTORY: Mapping[str, tuple[int, ...]] = {
    "CHAPTER_COLON_TITLE": tuple(),  # Count-only category; filled below.
    "CHAPTER_DOT_TITLE": tuple(),
    "CHAPTER_HTML_BR": (99,),
    "CHAPTER_INLINE_ANGLE": (66,),
    "CHAPTER_THEN_ANGLE": (83,),
    "EPILOGUE": (333, 334),
    "GENERATOR_WRAPPED": (148,),
    "GENERIC_CHAPTER": (39, 48, 49, 89, 90, 102, 129, 130, 142, 155, 156, 158, 224),
    "MARKDOWN_CHAPTER": tuple(),
    "MARKDOWN_SIDE_STORY": tuple(),
    "NONSTANDARD_BOLD": (216,),
    "NONSTANDARD_QUOTE": (344,),
    "OTHER_CHAPTER": (44, 51, 53, 57, 64, 81),
    "PROLOGUE": (1,),
    "SIDE_STORY": tuple(),
}

ROGUE_EXPECTED_INVENTORY_COUNTS = {
    "CHAPTER_COLON_TITLE": 5,
    "CHAPTER_DOT_TITLE": 179,
    "CHAPTER_HTML_BR": 1,
    "CHAPTER_INLINE_ANGLE": 1,
    "CHAPTER_THEN_ANGLE": 1,
    "EPILOGUE": 2,
    "GENERATOR_WRAPPED": 1,
    "GENERIC_CHAPTER": 13,
    "MARKDOWN_CHAPTER": 123,
    "MARKDOWN_SIDE_STORY": 3,
    "NONSTANDARD_BOLD": 1,
    "NONSTANDARD_QUOTE": 1,
    "OTHER_CHAPTER": 6,
    "PROLOGUE": 1,
    "SIDE_STORY": 13,
}

NUMBERED_NAME_RE = re.compile(r"^(\d{4})\.md$", re.IGNORECASE)
MARKDOWN_RE = re.compile(r"^(#{1,6})[ \t]+(.+)$")
CHAPTER_RE = re.compile(r"^Chapter (\d+)(.*)$")
ANGLE_RE = re.compile(r"^<([ \t]*)(\S(?:.*\S)?)([ \t]*)>$")
NUMBERED_TITLE_RE = re.compile(r"^\d+(?:\\?\.)[ \t]+\S.*$")
PART_RE = re.compile(r"\(\d+\)")
NAMED_PART_RE = re.compile(r"\b(?:Prologue|Epilogue)[ \t]+\d+\b")


class PublicationCleanupError(ValueError):
    """A fail-closed corpus, structure, or publication error."""


def _artifact_exists(path: Path) -> bool:
    """Return True for filesystem entries including dangling symlinks."""
    return path.is_symlink() or path.exists()


@dataclass(frozen=True)
class PhysicalLine:
    content: bytes
    ending: bytes

    @property
    def raw(self) -> bytes:
        return self.content + self.ending

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    @property
    def blank(self) -> bool:
        return self.content == b""


@dataclass(frozen=True)
class UnitPlan:
    unit: int
    source_path: Path
    source_bytes: bytes
    output_bytes: bytes
    inventory_category: str
    transformations: tuple[str, ...]
    before_heading_lines: tuple[str, ...]
    after_heading_lines: tuple[str, ...]
    source_body_offset: int
    output_body_offset: int
    part_markers: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.source_bytes != self.output_bytes


@dataclass(frozen=True)
class CorpusPlan:
    source_dir: Path
    expected_count: int
    units: tuple[UnitPlan, ...]
    source_fingerprint: dict[str, Any]
    output_fingerprint: dict[str, Any]
    inventory_counts: dict[str, int]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_physical_lines(data: bytes) -> list[PhysicalLine]:
    """Split bytes without newline conversion or loss."""
    lines: list[PhysicalLine] = []
    start = 0
    for match in re.finditer(rb"\r\n|\n|\r", data):
        lines.append(PhysicalLine(data[start : match.start()], match.group(0)))
        start = match.end()
    if start < len(data):
        lines.append(PhysicalLine(data[start:], b""))
    elif not lines:
        lines.append(PhysicalLine(b"", b""))
    return lines


def _decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationCleanupError(f"{label} is not valid UTF-8: {exc}") from exc


def _line_offset(lines: list[PhysicalLine], index: int) -> int:
    return sum(len(line.raw) for line in lines[:index])


def _heading_lines(data: bytes, body_offset: int) -> tuple[str, ...]:
    prefix = data[:body_offset]
    return tuple(_decode_utf8(line.content, "heading line") for line in split_physical_lines(prefix))


def _unwrap_angle(text: str) -> str | None:
    match = ANGLE_RE.fullmatch(text)
    return match.group(2) if match else None


def _unwrap_outer_wrappers(text: str) -> tuple[str, list[str]]:
    transformations: list[str] = []
    markdown = MARKDOWN_RE.fullmatch(text)
    if markdown:
        text = markdown.group(2)
        transformations.append("markdown_heading_wrapper")

    if len(text) >= 4 and text.startswith("**") and text.endswith("**"):
        text = text[2:-2]
        transformations.append("whole_title_bold_wrapper")

    quote_pairs = (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"))
    for opening, closing in quote_pairs:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            text = text[len(opening) : len(text) - len(closing)]
            transformations.append("whole_title_quote_wrapper")
            break
    return text, transformations


def _recognized_complete_heading(text: str) -> bool:
    if text == "Prologue" or text.startswith("Prologue "):
        return True
    if text == "Epilogue" or text.startswith("Epilogue "):
        return True
    if text == "Side Story" or text.startswith("Side Story "):
        return True
    match = CHAPTER_RE.fullmatch(text)
    return bool(match and match.group(2))


def _recognized_secondary_title(text: str) -> bool:
    cleaned, wrappers = _unwrap_outer_wrappers(text)
    angle_title = _unwrap_angle(cleaned)
    if angle_title is not None:
        return True
    return bool(cleaned and wrappers) or bool(NUMBERED_TITLE_RE.fullmatch(cleaned))


def _clean_angle_in_complete_heading(text: str) -> tuple[str, bool]:
    """Unwrap one complete trailing title wrapper in a Chapter heading."""
    match = re.fullmatch(r"(Chapter \d+(?:\.|:| —))([ \t]*)(<.*>)", text)
    if not match:
        return text, False
    title = _unwrap_angle(match.group(3))
    if title is None:
        raise PublicationCleanupError("malformed angle wrapper in chapter title region")
    # The wrapper's padding is presentation-only.  Its replacement is one
    # separator space, while all punctuation and title text stay untouched.
    return match.group(1) + " " + title, True


def _classify_plain_heading(
    original: str,
    cleaned: str,
    unit: int,
    lines: list[PhysicalLine],
) -> tuple[str, int | None]:
    """Return the supplied-inventory category and optional secondary line."""
    wrapper_category: str | None = None
    if original.startswith("#"):
        if cleaned.startswith("Chapter "):
            wrapper_category = "MARKDOWN_CHAPTER"
        elif cleaned.startswith("Side Story "):
            wrapper_category = "MARKDOWN_SIDE_STORY"
        else:
            raise PublicationCleanupError(f"unit {unit}: unexpected Markdown heading target")
    elif original.startswith("**"):
        wrapper_category = "NONSTANDARD_BOLD"
    elif original[:1] in {"\"", "'", "“", "‘"}:
        wrapper_category = "NONSTANDARD_QUOTE"
    if cleaned == "Prologue" or cleaned.startswith("Prologue "):
        return wrapper_category or "PROLOGUE", None
    if cleaned == "Epilogue" or cleaned.startswith("Epilogue "):
        return wrapper_category or "EPILOGUE", None
    if cleaned == "Side Story" or cleaned.startswith("Side Story "):
        return wrapper_category or "SIDE_STORY", None

    chapter = CHAPTER_RE.fullmatch(cleaned)
    if not chapter:
        raise PublicationCleanupError(f"unit {unit}: unexpected leading heading {original!r}")
    displayed = int(chapter.group(1))
    if displayed != unit:
        raise PublicationCleanupError(
            f"unit {unit}: displayed Chapter number is {displayed}"
        )
    suffix = chapter.group(2)
    if suffix == "<br>":
        return wrapper_category or "CHAPTER_HTML_BR", 1
    if suffix == "":
        if len(lines) < 3:
            raise PublicationCleanupError(f"unit {unit}: generic Chapter scaffold is incomplete")
        secondary = 2 if lines[1].blank else 1
        if secondary >= len(lines) or not _recognized_secondary_title(lines[secondary].text):
            raise PublicationCleanupError(
                f"unit {unit}: generic Chapter scaffold lacks a recognized title line"
            )
        if wrapper_category:
            return wrapper_category, secondary
        if secondary == 1 and _unwrap_angle(lines[secondary].text) is not None:
            return "CHAPTER_THEN_ANGLE", secondary
        return "GENERIC_CHAPTER", secondary
    if wrapper_category:
        # The Markdown/bold wrapper is the only authorized edit here.  The
        # existing nonempty Chapter suffix is retained byte-for-byte, including
        # its punctuation, capitalization, and numbering.
        return wrapper_category, None
    if suffix.startswith(":"):
        return "CHAPTER_COLON_TITLE", None
    if suffix.startswith("."):
        return "CHAPTER_DOT_TITLE", None
    if suffix.startswith(" —"):
        if re.fullmatch(r" —[ \t]*<.*>", suffix):
            return "CHAPTER_INLINE_ANGLE", None
        return "OTHER_CHAPTER", None
    raise PublicationCleanupError(f"unit {unit}: unexpected Chapter heading suffix {suffix!r}")


def _require_separator_and_body(
    lines: list[PhysicalLine], separator_index: int, unit: int
) -> int:
    if separator_index >= len(lines) or not lines[separator_index].blank:
        raise PublicationCleanupError(f"unit {unit}: heading is not followed by one blank separator line")
    body_index = separator_index + 1
    if body_index >= len(lines) or lines[body_index].blank:
        raise PublicationCleanupError(f"unit {unit}: missing body or unexpected extra heading spacing")
    return body_index


def _part_markers(lines: tuple[str, ...]) -> tuple[str, ...]:
    text = "\n".join(lines)
    markers = PART_RE.findall(text) + NAMED_PART_RE.findall(text)
    return tuple(markers)


def transform_unit(unit: int, source_path: Path, data: bytes) -> UnitPlan:
    """Plan one byte-bounded heading cleanup and verify its body immediately."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise PublicationCleanupError(f"unit {unit}: UTF-8 BOM is not an authorized heading wrapper")
    _decode_utf8(data, f"unit {unit}")
    lines = split_physical_lines(data)
    if not lines or lines[0].blank:
        raise PublicationCleanupError(f"unit {unit}: missing leading heading")

    transformations: list[str] = []
    if lines[0].text == GEN_START:
        if len(lines) < 7 or not (
            lines[1].blank
            and not lines[2].blank
            and lines[3].blank
            and lines[4].text == GEN_END
            and lines[5].blank
            and not lines[6].blank
        ):
            raise PublicationCleanupError(f"unit {unit}: malformed generator title wrapper")
        title = lines[2].text
        if not _recognized_complete_heading(title):
            raise PublicationCleanupError(f"unit {unit}: generator wrapper contains an unexpected title")
        chapter = CHAPTER_RE.fullmatch(title)
        if chapter and int(chapter.group(1)) != unit:
            raise PublicationCleanupError(f"unit {unit}: generator title Chapter number mismatch")
        inventory_category = "GENERATOR_WRAPPED"
        transformations.append("generator_title_wrapper")
        body_index = 6
        source_body_offset = _line_offset(lines, body_index)
        output_prefix = lines[2].raw + lines[5].raw
    else:
        original_heading = lines[0].text
        cleaned_heading, outer = _unwrap_outer_wrappers(original_heading)
        transformations.extend(outer)
        inventory_category, secondary_index = _classify_plain_heading(
            original_heading, cleaned_heading, unit, lines
        )

        if cleaned_heading.endswith("<br>"):
            cleaned_heading = cleaned_heading[:-4]
            transformations.append("html_break_scaffold")

        cleaned_heading, angle_changed = _clean_angle_in_complete_heading(cleaned_heading)
        if angle_changed:
            transformations.append("angle_title_wrapper")

        first_line = cleaned_heading.encode("utf-8") + lines[0].ending
        if secondary_index is None:
            if not _recognized_complete_heading(cleaned_heading):
                raise PublicationCleanupError(f"unit {unit}: cleaned heading is not recognized")
            body_index = _require_separator_and_body(lines, 1, unit)
            source_body_offset = _line_offset(lines, body_index)
            output_prefix = first_line + lines[1].raw
        else:
            if secondary_index == 2 and not lines[1].blank:
                raise PublicationCleanupError(f"unit {unit}: malformed split heading scaffold")
            if secondary_index == 1 and lines[1].blank:
                raise PublicationCleanupError(f"unit {unit}: malformed adjacent heading scaffold")
            secondary_text = lines[secondary_index].text
            secondary_text, secondary_wrappers = _unwrap_outer_wrappers(secondary_text)
            transformations.extend(secondary_wrappers)
            unwrapped_title = _unwrap_angle(secondary_text)
            had_angle_wrapper = unwrapped_title is not None
            if unwrapped_title is not None:
                secondary_text = unwrapped_title
                transformations.append("angle_title_wrapper")
            if not secondary_text or not (
                had_angle_wrapper
                or secondary_wrappers
                or NUMBERED_TITLE_RE.fullmatch(secondary_text)
            ):
                raise PublicationCleanupError(f"unit {unit}: unsafe secondary title line")
            separator_index = secondary_index + 1
            body_index = _require_separator_and_body(lines, separator_index, unit)
            source_body_offset = _line_offset(lines, body_index)
            if secondary_index == 2:
                transformations.append("split_scaffold_spacing")
            second_line = secondary_text.encode("utf-8") + lines[secondary_index].ending
            output_prefix = first_line + second_line + lines[separator_index].raw

    body = data[source_body_offset:]
    output = output_prefix + body
    output_body_offset = len(output_prefix)
    if output[output_body_offset:] != body:
        raise PublicationCleanupError(f"unit {unit}: planned transformation changes body bytes")

    before_lines = _heading_lines(data, source_body_offset)
    after_lines = _heading_lines(output, output_body_offset)
    before_parts = _part_markers(before_lines)
    after_parts = _part_markers(after_lines)
    if before_parts != after_parts:
        raise PublicationCleanupError(
            f"unit {unit}: source-explicit part marker would disappear or change"
        )
    unique_transformations = tuple(dict.fromkeys(transformations))
    if data != output and not unique_transformations:
        raise PublicationCleanupError(f"unit {unit}: unclassified heading change")
    if data == output and unique_transformations:
        raise PublicationCleanupError(f"unit {unit}: reported transformation is a no-op")
    return UnitPlan(
        unit=unit,
        source_path=source_path,
        source_bytes=data,
        output_bytes=output,
        inventory_category=inventory_category,
        transformations=unique_transformations,
        before_heading_lines=before_lines,
        after_heading_lines=after_lines,
        source_body_offset=source_body_offset,
        output_body_offset=output_body_offset,
        part_markers=before_parts,
    )


def _validated_source_paths(source_dir: Path, expected_count: int) -> list[Path]:
    if not source_dir.is_dir():
        raise PublicationCleanupError(f"source corpus directory does not exist: {source_dir}")
    entries = sorted(source_dir.iterdir(), key=lambda path: path.name.casefold())
    seen: dict[int, str] = {}
    errors: list[str] = []
    for path in entries:
        match = NUMBERED_NAME_RE.fullmatch(path.name)
        if not match or not path.is_file() or path.is_symlink():
            errors.append(path.name)
            continue
        unit = int(match.group(1))
        if unit in seen:
            raise PublicationCleanupError(
                f"duplicate unit {unit}: {seen[unit]!r} and {path.name!r}"
            )
        seen[unit] = path.name
    if errors:
        raise PublicationCleanupError(f"unexpected source corpus entries: {errors}")
    expected_names = [f"{unit:04d}.md" for unit in range(1, expected_count + 1)]
    actual_names = [seen[unit] for unit in sorted(seen)]
    if len(entries) != expected_count:
        raise PublicationCleanupError(
            f"input file count must be exactly {expected_count}; found {len(entries)}"
        )
    if actual_names != expected_names:
        missing = sorted(set(expected_names) - set(actual_names))
        unexpected = sorted(set(actual_names) - set(expected_names))
        raise PublicationCleanupError(
            f"numbered corpus must be exactly 0001-{expected_count:04d}; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return [source_dir / name for name in expected_names]


def framed_corpus_fingerprint(payloads: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Fingerprint named corpus bytes with unambiguous filename/length framing."""
    aggregate = hashlib.sha256()
    files: list[dict[str, Any]] = []
    byte_count = 0
    for name, data in payloads:
        encoded_name = name.encode("ascii")
        aggregate.update(len(encoded_name).to_bytes(2, "big"))
        aggregate.update(encoded_name)
        aggregate.update(len(data).to_bytes(8, "big"))
        aggregate.update(data)
        byte_count += len(data)
        files.append(
            {
                "unit": int(Path(name).stem),
                "file": name,
                "byte_count": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    return {
        "algorithm": "sha256-filename-length-bytes-v1",
        "aggregate_sha256": aggregate.hexdigest(),
        "aggregate_byte_count": byte_count,
        "file_count": len(payloads),
        "files": files,
    }


def _validate_rogue_inventory(units: tuple[UnitPlan, ...]) -> None:
    by_category: dict[str, list[int]] = {}
    for plan in units:
        by_category.setdefault(plan.inventory_category, []).append(plan.unit)
    counts = {key: len(by_category.get(key, [])) for key in ROGUE_EXPECTED_INVENTORY_COUNTS}
    if counts != ROGUE_EXPECTED_INVENTORY_COUNTS:
        raise PublicationCleanupError(
            f"Rogue heading inventory drift: expected {ROGUE_EXPECTED_INVENTORY_COUNTS}, found {counts}"
        )
    for category, expected_units in ROGUE_EXPECTED_INVENTORY.items():
        if expected_units and tuple(by_category.get(category, [])) != expected_units:
            raise PublicationCleanupError(
                f"Rogue {category} unit inventory drift: expected {expected_units}, "
                f"found {tuple(by_category.get(category, []))}"
            )


def build_plan(
    source_dir: Path,
    expected_count: int,
    *,
    enforce_rogue_inventory: bool = False,
) -> CorpusPlan:
    paths = _validated_source_paths(source_dir, expected_count)
    units = tuple(transform_unit(int(path.stem), path, path.read_bytes()) for path in paths)
    if enforce_rogue_inventory:
        if expected_count != 351:
            raise PublicationCleanupError("the Rogue inventory contract requires exactly 351 units")
        _validate_rogue_inventory(units)
    inventory_counts = dict(sorted(Counter(item.inventory_category for item in units).items()))
    source_payloads = [(item.source_path.name, item.source_bytes) for item in units]
    output_payloads = [(item.source_path.name, item.output_bytes) for item in units]
    return CorpusPlan(
        source_dir=source_dir,
        expected_count=expected_count,
        units=units,
        source_fingerprint=framed_corpus_fingerprint(source_payloads),
        output_fingerprint=framed_corpus_fingerprint(output_payloads),
        inventory_counts=inventory_counts,
    )


def _newline_style(data: bytes) -> str:
    crlf = len(re.findall(rb"\r\n", data))
    bare_lf = len(re.findall(rb"(?<!\r)\n", data))
    bare_cr = len(re.findall(rb"\r(?!\n)", data))
    used = sum(bool(count) for count in (crlf, bare_lf, bare_cr))
    if used > 1:
        return "MIXED"
    if crlf:
        return "CRLF"
    if bare_lf:
        return "LF"
    if bare_cr:
        return "CR"
    return "NONE"


def build_report(plan: CorpusPlan) -> dict[str, Any]:
    changed = [item for item in plan.units if item.changed]
    transformation_counts = Counter(
        transformation for item in changed for transformation in item.transformations
    )
    body_failed = [
        item.unit
        for item in plan.units
        if item.source_bytes[item.source_body_offset :]
        != item.output_bytes[item.output_body_offset :]
    ]
    part_failed = [
        item.unit
        for item in plan.units
        if _part_markers(item.before_heading_lines) != _part_markers(item.after_heading_lines)
    ]
    unit_334 = next((item for item in plan.units if item.unit == 334), None)
    unit_334_ok = bool(
        unit_334
        and unit_334.before_heading_lines[0] == "Epilogue 2"
        and unit_334.after_heading_lines[0] == "Epilogue 2"
        and "Epilogue 2" in unit_334.part_markers
    )
    if plan.expected_count == 351 and not unit_334_ok:
        raise PublicationCleanupError("unit 334 does not preserve the explicit Epilogue 2 marker")
    blockers = body_failed + part_failed
    if blockers:
        raise PublicationCleanupError(f"verification blockers remain for units {sorted(set(blockers))}")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": REVISION,
        "status": "success",
        "ready": True,
        "success": True,
        "blocker_count": 0,
        "source_corpus_fingerprint": plan.source_fingerprint,
        "output_corpus_fingerprint": plan.output_fingerprint,
        "total_files": len(plan.units),
        "changed_file_count": len(changed),
        "unchanged_file_count": len(plan.units) - len(changed),
        "heading_inventory_counts": plan.inventory_counts,
        "transformation_count_basis": "affected_files_per_category",
        "transformation_counts": {
            category: transformation_counts.get(category, 0)
            for category in TRANSFORMATION_CATEGORIES
        },
        "changed_units": [item.unit for item in changed],
        "changes": [
            {
                "unit": item.unit,
                "file": item.source_path.name,
                "inventory_category": item.inventory_category,
                "transformations": list(item.transformations),
                "before_heading": {
                    "lines": list(item.before_heading_lines),
                    "newline_style": _newline_style(
                        item.source_bytes[: item.source_body_offset]
                    ),
                },
                "after_heading": {
                    "lines": list(item.after_heading_lines),
                    "newline_style": _newline_style(
                        item.output_bytes[: item.output_body_offset]
                    ),
                },
            }
            for item in changed
        ],
        "verification": {
            "unit_334_preserves_part_2": {
                "passed": unit_334_ok if unit_334 else None,
                "before": unit_334.before_heading_lines[0] if unit_334 else None,
                "after": unit_334.after_heading_lines[0] if unit_334 else None,
            },
            "body_preservation": {
                "passed": not body_failed,
                "checked_file_count": len(plan.units),
                "preserved_file_count": len(plan.units) - len(body_failed),
                "failed_units": body_failed,
            },
            "source_explicit_part_markers": {
                "passed": not part_failed,
                "checked_file_count": len(plan.units),
                "failed_units": part_failed,
            },
        },
    }


def _markdown_cell(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Publication Cleanup Report",
        "",
        f"- Schema/revision: `{report['schema_version']}` / `{report['revision']}`",
        f"- Status: `{report['status']}`",
        f"- Ready/success: `{report['ready']}` / `{report['success']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Files: `{report['total_files']}` total; `{report['changed_file_count']}` changed; `{report['unchanged_file_count']}` unchanged",
        f"- Source fingerprint: `{report['source_corpus_fingerprint']['aggregate_sha256']}`",
        f"- Output fingerprint: `{report['output_corpus_fingerprint']['aggregate_sha256']}`",
        "",
        "## Transformation Counts",
        "",
    ]
    for category, count in report["transformation_counts"].items():
        lines.append(f"- `{category}`: `{count}`")
    verification = report["verification"]
    lines.extend(
        [
            "",
            "## Verification",
            "",
            f"- Unit 334 preserves `Epilogue 2`: `{verification['unit_334_preserves_part_2']['passed']}`",
            f"- Body bytes preserved: `{verification['body_preservation']['passed']}` ({verification['body_preservation']['preserved_file_count']}/{verification['body_preservation']['checked_file_count']})",
            f"- Source-explicit part markers preserved: `{verification['source_explicit_part_markers']['passed']}`",
            "",
            "## Changed Headings",
            "",
            "| Unit | Transformations | Before | After |",
            "|---:|---|---|---|",
        ]
    )
    for change in report["changes"]:
        before = " ⏎ ".join(change["before_heading"]["lines"])
        after = " ⏎ ".join(change["after_heading"]["lines"])
        transformations = ", ".join(change["transformations"])
        lines.append(
            f"| {change['unit']:04d} | {_markdown_cell(transformations)} | "
            f"{_markdown_cell(before)} | {_markdown_cell(after)} |"
        )
    return "\n".join(lines) + "\n"


def _refuse_existing_targets(output_dir: Path, report_json: Path, report_md: Path) -> None:
    existing = [path for path in (output_dir, report_json, report_md) if _artifact_exists(path)]
    if existing:
        raise PublicationCleanupError(
            "refusing to overwrite existing publication target(s): "
            + ", ".join(str(path) for path in existing)
        )


def _verify_exact_staged_publication_prefix(plan: CorpusPlan, stage: Path) -> None:
    if stage.is_symlink() or not stage.is_dir():
        raise PublicationCleanupError(
            f"ambiguous interrupted publication stage artifact: {stage}"
        )
    actual = sorted(stage.iterdir(), key=lambda item: item.name.casefold())
    expected_names = [item.source_path.name for item in plan.units]
    if [path.name for path in actual] != expected_names:
        raise PublicationCleanupError(
            f"interrupted publication stage inventory mismatch: {stage}"
        )
    for path, item in zip(actual, plan.units):
        if path.is_symlink() or not path.is_file():
            raise PublicationCleanupError(
                f"ambiguous interrupted publication stage member: {path}"
            )
        if path.read_bytes() != item.output_bytes:
            raise PublicationCleanupError(
                f"interrupted publication stage bytes do not match plan: {path}"
            )


def _recover_exact_interrupted_publication_prefix(
    plan: CorpusPlan,
    *,
    output_dir: Path,
    report_json: Path,
    report_md: Path,
    current_stage: Path,
    expected_json_bytes: bytes,
    expected_md_bytes: bytes,
) -> bool:
    """Remove only independently proven uncommitted publication residue."""
    if _artifact_exists(output_dir):
        raise PublicationCleanupError(
            f"refusing to overwrite existing publication target: {output_dir}"
        )

    json_candidates = [report_json] if _artifact_exists(report_json) else []
    md_candidates = [report_md] if _artifact_exists(report_md) else []
    cleanup_identity = {
        "source_fingerprint": plan.source_fingerprint,
        "output_fingerprint": plan.output_fingerprint,
        "expected_count": plan.expected_count,
        "expected_json_sha256": hashlib.sha256(expected_json_bytes).hexdigest(),
        "expected_markdown_sha256": hashlib.sha256(expected_md_bytes).hexdigest(),
    }
    stage_cleanup = inspect_stage_cleanup(
        output_dir.parent,
        namespace="publication-chapters",
        stage_prefix=f".{output_dir.name}.staging-",
        proof_id=cleanup_proof_id("publication-chapters", cleanup_identity),
        current_stage=current_stage,
        error_cls=PublicationCleanupError,
    )
    stage_candidates = list(stage_cleanup.stage_candidates)

    if not (
        json_candidates
        or md_candidates
        or stage_cleanup.residue_present
    ):
        return False

    for path in json_candidates:
        if path.is_symlink() or not path.is_file():
            raise PublicationCleanupError(
                f"ambiguous interrupted publication JSON artifact: {path}"
            )
        if path.read_bytes() != expected_json_bytes:
            raise PublicationCleanupError(
                f"interrupted publication JSON artifact does not match reconstructed bytes: {path}"
            )

    for path in md_candidates:
        if path.is_symlink() or not path.is_file():
            raise PublicationCleanupError(
                f"ambiguous interrupted publication Markdown artifact: {path}"
            )
        if path.read_bytes() != expected_md_bytes:
            raise PublicationCleanupError(
                f"interrupted publication Markdown artifact does not match reconstructed bytes: {path}"
            )

    proven_stages: list[Path] = []
    for stage in stage_candidates:
        _verify_exact_staged_publication_prefix(plan, stage)
        proven_stages.append(stage)

    for path in md_candidates:
        path.unlink()
    for path in json_candidates:
        path.unlink()
    cleanup_proven_stages(
        stage_cleanup,
        proven_stages,
        error_cls=PublicationCleanupError,
    )
    return True


def _prove_terminal_publication_commit_after_rename_error(
    plan: CorpusPlan,
    *,
    stage: Path,
    output_dir: Path,
    report_json: Path,
    report_md: Path,
    expected_json_bytes: bytes,
    expected_md_bytes: bytes,
) -> bool:
    """Prove that terminal publication-chapter commit completed despite a reported rename error."""
    if _artifact_exists(stage):
        return False
    if (
        not output_dir.exists()
        or output_dir.is_symlink()
        or not output_dir.is_dir()
    ):
        return False

    for path, expected in (
        (report_json, expected_json_bytes),
        (report_md, expected_md_bytes),
    ):
        if not path.exists() or path.is_symlink() or not path.is_file():
            return False
        try:
            if path.read_bytes() != expected:
                return False
        except OSError:
            return False

    try:
        _verify_exact_staged_publication_prefix(plan, output_dir)
        current = build_plan(
            plan.source_dir,
            plan.expected_count,
            enforce_rogue_inventory=plan.expected_count == 351,
        )
        if current.source_fingerprint != plan.source_fingerprint:
            return False
        if current.output_fingerprint != plan.output_fingerprint:
            return False
    except (OSError, PublicationCleanupError):
        return False
    return True


def publish_plan(
    plan: CorpusPlan,
    output_dir: Path,
    report_json: Path,
    report_md: Path,
) -> dict[str, Any]:
    """Verify a complete plan, then install a new corpus and two new reports."""
    if _artifact_exists(output_dir):
        raise PublicationCleanupError(
            f"refusing to overwrite existing publication target: {output_dir}"
        )
    current = build_plan(
        plan.source_dir,
        plan.expected_count,
        enforce_rogue_inventory=plan.expected_count == 351,
    )
    if current.source_fingerprint != plan.source_fingerprint:
        raise PublicationCleanupError("source corpus changed after the publication plan was built")
    report = build_report(plan)
    json_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    md_bytes = render_markdown_report(report).encode("utf-8")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    report_parent_existed = report_json.parent.exists()
    report_json.parent.mkdir(parents=True, exist_ok=True)
    if report_md.parent != report_json.parent:
        report_md.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    written_reports: list[Path] = []
    try:
        for item in plan.units:
            (stage / item.source_path.name).write_bytes(item.output_bytes)
        staged_payloads = [(path.name, path.read_bytes()) for path in sorted(stage.iterdir())]
        if framed_corpus_fingerprint(staged_payloads) != plan.output_fingerprint:
            raise PublicationCleanupError("staged output corpus fingerprint mismatch")
        for item in plan.units:
            staged = (stage / item.source_path.name).read_bytes()
            if staged[item.output_body_offset :] != item.source_bytes[item.source_body_offset :]:
                raise PublicationCleanupError(f"unit {item.unit}: staged body bytes changed")

        _recover_exact_interrupted_publication_prefix(
            plan,
            output_dir=output_dir,
            report_json=report_json,
            report_md=report_md,
            current_stage=stage,
            expected_json_bytes=json_bytes,
            expected_md_bytes=md_bytes,
        )
        _refuse_existing_targets(output_dir, report_json, report_md)
        with report_json.open("xb") as handle:
            handle.write(json_bytes)
        written_reports.append(report_json)
        with report_md.open("xb") as handle:
            handle.write(md_bytes)
        written_reports.append(report_md)
        if _artifact_exists(output_dir):
            raise PublicationCleanupError(f"publication output appeared during build: {output_dir}")
        stage.rename(output_dir)
        return report
    except Exception as exc:
        if _artifact_exists(output_dir):
            if _prove_terminal_publication_commit_after_rename_error(
                plan,
                stage=stage,
                output_dir=output_dir,
                report_json=report_json,
                report_md=report_md,
                expected_json_bytes=json_bytes,
                expected_md_bytes=md_bytes,
            ):
                return report
            raise PublicationCleanupError(
                "ambiguous terminal publication commit outcome after rename error; "
                "existing output and reports were preserved"
            ) from exc

        if stage.exists():
            shutil.rmtree(stage)
        for path in reversed(written_reports):
            path.unlink(missing_ok=True)
        if not report_parent_existed and report_json.parent.exists():
            try:
                report_json.parent.rmdir()
            except OSError:
                pass
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="build and print the plan only")
    mode.add_argument("--apply", action="store_true", help="create the corpus and reports")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lock = production_run_lock(DEFAULT_FINAL_DIR.parent) if args.apply else nullcontext()
        with lock:
            if args.dry_run:
                _refuse_existing_targets(
                    DEFAULT_OUTPUT_DIR, DEFAULT_REPORT_JSON, DEFAULT_REPORT_MD
                )
            elif _artifact_exists(DEFAULT_OUTPUT_DIR):
                raise PublicationCleanupError(
                    f"refusing to overwrite existing publication target: {DEFAULT_OUTPUT_DIR}"
                )
            plan = build_plan(DEFAULT_SOURCE_DIR, 351, enforce_rogue_inventory=True)
            report = build_report(plan)
            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "ready": True,
                            "total_files": report["total_files"],
                            "changed_file_count": report["changed_file_count"],
                            "unchanged_file_count": report["unchanged_file_count"],
                            "transformation_counts": report["transformation_counts"],
                            "source_corpus_fingerprint": report["source_corpus_fingerprint"]["aggregate_sha256"],
                            "planned_output_corpus_fingerprint": report["output_corpus_fingerprint"]["aggregate_sha256"],
                            "blocker_count": 0,
                        },
                        indent=2,
                    )
                )
            else:
                report = publish_plan(plan, DEFAULT_OUTPUT_DIR, DEFAULT_REPORT_JSON, DEFAULT_REPORT_MD)
                print(
                    json.dumps(
                        {
                            "status": "success",
                            "total_files": report["total_files"],
                            "changed_file_count": report["changed_file_count"],
                            "output_corpus_fingerprint": report["output_corpus_fingerprint"]["aggregate_sha256"],
                            "report_json": str(DEFAULT_REPORT_JSON.relative_to(ROOT)),
                            "report_md": str(DEFAULT_REPORT_MD.relative_to(ROOT)),
                        },
                        indent=2,
                    )
                )
    except (PublicationCleanupError, OSError, ProductionRunLockedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
