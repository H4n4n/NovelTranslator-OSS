"""Settled English chapter titles, so a titled part keeps one heading across its chapters.

A novel may keep ``data/chapter_titles.json``:

    {"titles": {"<source title>": "<English title>", ...}}

The source title is the chapter's source heading without its leading number and
without the part markers that follow it: a volume numeral such as ``(Ⅱ)`` or
``Ⅱ`` and a part number such as ``- 2``. When a chapter's heading is one of these
titles, the translation request gives the exact English heading to use:

    <reader-facing label>: <English title>[ <volume numeral>][ (<part>)]

where the label is the one the request already names ("Chapter 841",
"Chapters 12-13"). Named units such as a prologue or epilogue are left alone.

Novels without the file are unaffected. The file is novel-owned state, read
only; nothing here calls a provider or writes a file.
"""
import json
import re
import sys
from pathlib import Path

from chapter_library import NAMED_UNIT_KINDS, production_unit_kind, reader_facing_unit_identity

TITLES_FILE = "chapter_titles.json"
ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V", "Ⅵ": "VI", "Ⅶ": "VII", "Ⅷ": "VIII",
         "Ⅸ": "IX", "Ⅹ": "X", "Ⅺ": "XI", "Ⅻ": "XII"}
_NUMBER = re.compile(r"^\s*\d+\s*(?:[.)]|화)\s*")
_VOLUME = r"(?:\(\s*([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ])\s*\)|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]))"
_PART = r"(?:-\s*(\d+)|\(\s*(\d+)\s*\))"
_TAIL = re.compile(rf"\s*{_VOLUME}?\s*{_PART}?\s*$")


def load_chapter_titles(data_dir):
    """The novel's settled titles; empty when it has none. A malformed file is reported and ignored."""
    path = Path(data_dir) / TITLES_FILE
    if not path.exists():
        return {}
    try:
        titles = json.loads(path.read_text(encoding="utf-8")).get("titles")
        if not isinstance(titles, dict):
            raise ValueError("no titles object")
        return {key.strip(): value.strip() for key, value in titles.items()
                if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()}
    except (ValueError, AttributeError) as error:
        print(f"WARNING: {path} is not usable ({error}); chapter headings are left to the model.",
              file=sys.stderr)
        return {}


def split_heading(source_heading):
    """(title, volume numeral, part number) of a source heading, or None if it has no title."""
    rest = _NUMBER.sub("", source_heading or "", count=1).strip()
    tail = _TAIL.search(rest)
    title = rest[:tail.start()].strip()
    if not title:
        return None
    volume_paren, volume, part_dash, part_paren = tail.groups()
    return title, ROMAN.get(volume_paren or volume, ""), part_dash or part_paren or ""


def settled_heading(chapter, chapter_numbering, titles):
    """The exact English heading for a chapter whose source title is settled, else None.

    The label is the reader-facing one the request already uses ("Chapter 841",
    "Chapters 12-13"). Named units (prologue, epilogue, side story, supplement)
    keep their own heading and get none.
    """
    parts = split_heading(chapter.get("heading")) if titles else None
    if parts is None or parts[0] not in titles:
        return None
    try:
        if production_unit_kind(chapter) in NAMED_UNIT_KINDS:
            return None
        label = reader_facing_unit_identity(chapter, chapter_numbering)
    except ValueError:
        return None
    title, volume, part = parts
    return f"{label}: {titles[title]}" + (f" {volume}" if volume else "") + (f" ({part})" if part else "")


def title_report(chapters, titles, translations=None, chapter_numbering="global"):
    """Every source title with its chapters, its settled English, and committed headings that differ."""
    rows = {}
    for chapter in chapters:
        parts = split_heading(chapter.get("heading") or "")
        if parts is None:
            continue
        row = rows.setdefault(parts[0], {"source": parts[0], "english": titles.get(parts[0], ""),
                                         "chapters": [], "headings_used": {}, "mismatches": []})
        number = chapter["chapter"]
        row["chapters"].append(number)
        first_line = next((line.strip() for line in (translations or {}).get(number, "").splitlines()
                           if line.strip()), "")
        if first_line:
            row["headings_used"][number] = first_line
            expected = settled_heading(chapter, chapter_numbering, titles)
            if expected and first_line.lstrip("# ").strip() != expected:
                row["mismatches"].append({"chapter": number, "expected": expected, "found": first_line})
    return sorted(rows.values(), key=lambda row: row["chapters"][0])
