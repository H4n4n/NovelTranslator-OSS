"""Targeted inventory checks for the installed authoritative Korean TXT.

The module skips cleanly in source-free checkouts; it never embeds novel prose.
"""

import json
from pathlib import Path

import pytest

from extract_txt import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT / "novels" / "rogue_first_prince"
SOURCE_HASH = "ce939a2167c0eb11700bd1b5a0f463c891da232d1bbe84265e2c5e718714015e"


def installed_files():
    source = PROJECT_ROOT / "source" / "novel.txt"
    chapters = PROJECT_ROOT / "data" / "chapters.json"
    inventory = PROJECT_ROOT / "data" / "source_inventory.json"
    if not all(path.is_file() for path in (source, chapters, inventory)):
        pytest.skip("installed Rogue First Prince source is not present")
    return source, chapters, inventory


def test_installed_rogue_txt_inventory_and_hash():
    source, chapters_path, inventory_path = installed_files()
    chapters = json.loads(chapters_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert sha256_file(source) == SOURCE_HASH
    assert len(chapters) == 350
    assert [item["production_index"] for item in chapters] == list(range(1, 351))
    assert chapters[0]["unit_kind"] == "prologue"
    assert chapters[-1]["production_index"] == 350
    assert chapters[-1]["source_number"] == 350
    assert chapters[-1]["unit_kind"] == "side_story"
    assert inventory["narrative_coverage"] == 1.0


def test_installed_rogue_late_337_transition_and_afterword_exclusion():
    _, chapters_path, inventory_path = installed_files()
    chapters = json.loads(chapters_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    first_337, second_337 = chapters[335:337]
    assert (first_337["production_index"], first_337["source_number"]) == (336, 337)
    assert first_337["heading_format"] == "number_dot"
    assert (second_337["production_index"], second_337["source_number"]) == (337, 337)
    assert second_337["heading_format"] == "two_line_je_number"
    assert inventory["source_number_duplicates"] == [337]
    assert inventory["source_number_missing"] == [171]
    afterwords = [
        item for item in inventory["excluded_sections"]
        if item["classification"] == "author_afterword_start"
    ]
    assert [(item["line_start"], item["line_end"]) for item in afterwords] == [
        (136317, 136348)
    ]
    assert "외전 완결 후기" not in "\n".join(item["text"] for item in chapters)
