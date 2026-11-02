"""Approved term corrections on a small synthetic project, with no provider."""
import json
from types import SimpleNamespace

import pytest

import approved_corrections
import build_checkpoint_epub
from noveltranslator_core import atomic_write_json

REPLACEMENTS = [
    {"source": ["百戶"], "old": "Centurion", "new": "Hundred-Household Commander"},
    {"source": ["天師道"], "old": "Tianshi Dao", "new": "Tianshi Dao (Way of the Celestial Masters)"},
    {"source": ["太陰罡籙"], "old": "Taiyin Gang Register", "new": "Taiyin Ganglu (Greater Yin Astral Register)"},
    {"source": ["太陰罡"], "old": "Taiyin Gang", "new": "Taiyin Gang (Greater Yin Astral Force)"},
    {"source": ["鳥銃"], "old": "bird guns", "new": "fowling muskets"},
]


def test_only_terms_in_the_chapter_source_change():
    text, changes = approved_corrections.correct_chapter("百戶來了。", "The Centurion met a Centurionate. Tianshi Dao.", REPLACEMENTS)
    assert text == "The Hundred-Household Commander met a Centurionate. Tianshi Dao."
    assert changes == [{"offset": 4, "old": "Centurion", "new": "Hundred-Household Commander"}]


def test_longer_terms_win_and_a_correction_never_feeds_another():
    text, _ = approved_corrections.correct_chapter("太陰罡籙，太陰罡。", "Taiyin Gang Register and Taiyin Gang.", REPLACEMENTS)
    assert text == "Taiyin Ganglu (Greater Yin Astral Register) and Taiyin Gang (Greater Yin Astral Force)."


def test_a_form_already_carrying_the_english_is_left_alone():
    before = "Tianshi Dao (Way of the Celestial Masters) scribes, then Tianshi Dao."
    text, changes = approved_corrections.correct_chapter("天師道", before, REPLACEMENTS)
    assert text == "Tianshi Dao (Way of the Celestial Masters) scribes, then Tianshi Dao (Way of the Celestial Masters)."
    assert len(changes) == 1


def test_a_correction_that_removes_a_bracket_applies_once():
    removal = [{"source": ["經略"], "old": "Military Superintendent (Jinglue)", "new": "Military Superintendent"}]
    text, changes = approved_corrections.correct_chapter("經略", "The Military Superintendent (Jinglue) arrived.", removal)
    assert text == "The Military Superintendent arrived." and len(changes) == 1
    assert approved_corrections.correct_chapter("經略", text, removal) == (text, [])


def test_a_lowercase_term_also_changes_at_the_start_of_a_sentence():
    text, _ = approved_corrections.correct_chapter("鳥銃", "Bird guns fired. More bird guns.", REPLACEMENTS)
    assert text == "Fowling muskets fired. More fowling muskets."


def test_one_old_form_cannot_have_two_new_ones(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps({"replacements": REPLACEMENTS + [{"source": ["百戶"], "old": "Centurion", "new": "Captain"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        approved_corrections.load_corrections(path)


def test_changed_chapters_are_backed_up_logged_and_a_rerun_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(build_checkpoint_epub, "rebuild_reader_epub", lambda project, take_lock: {"status": "OK"})
    root = tmp_path / "novel"
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "data").mkdir()
    project = SimpleNamespace(root=root, chapter_dir=root / "output" / "chapters", backup_dir=root / "backup",
                              retroactive_dir=root / "output" / "retroactive_fixes", progress_file=root / "data" / "progress.json",
                              chapters_file=root / "data" / "chapters.json", glossary_file=root / "data" / "glossary.json")
    atomic_write_json(project.chapters_file, [dict(chapter=1, text="百戶"), dict(chapter=2, text="無")])
    atomic_write_json(project.progress_file, dict(last_completed=2))
    atomic_write_json(project.glossary_file, {"entries": {
        "百戶": dict(english="Centurion", category="rank", status="locked"),
        "百戶所": dict(english="Centurion Office", category="organization", status="provisional"),
        "塔拜": dict(english="Fulin", category="character", status="provisional")}})
    for n, english in ((1, "The Centurion came."), (2, "Nobody came.")):
        (project.chapter_dir / f"{n:04d}.md").write_text(english, encoding="utf-8")
    corrections = {"glossary": {"塔拜": "Tabai"}, "replacements": REPLACEMENTS}

    summary = approved_corrections.apply_corrections(project, corrections, log=lambda _: None)
    assert summary["chapters_changed"] == 1 and summary["replacements"] == 1 and summary["glossary_changed"] == 3
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert entries["百戶所"]["english"] == "Hundred-Household Commander Office" and entries["塔拜"]["english"] == "Tabai"
    assert (project.chapter_dir / "0001.md").read_text(encoding="utf-8") == "The Hundred-Household Commander came."
    assert [path.read_text(encoding="utf-8") for path in project.backup_dir.rglob("0001.md")] == ["The Centurion came."]
    record = json.loads(next(project.retroactive_dir.glob("approved_corrections_*.json")).read_text(encoding="utf-8"))
    assert list(record["chapters"]) == ["0001"] and record["glossary"]["百戶"]["old"] == "Centurion"
    entry = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]["百戶"]
    assert entry["english"] == "Hundred-Household Commander" and entry["status"] == "locked"

    again = approved_corrections.apply_corrections(project, corrections, log=lambda _: None)
    assert again["chapters_changed"] == 0 and again["glossary_changed"] == 0
