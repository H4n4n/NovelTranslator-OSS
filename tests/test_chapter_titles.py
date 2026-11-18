"""Settled chapter titles on synthetic headings: parsing, the request line, and the local report."""
import json
from types import SimpleNamespace

import pytest

import chapter_titles as ct
import glossary_checks as gc
import novel_project
import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from production_translation_request import build_translation_request

TITLES = {"푸른 강의 전투": "The Battle of the Blue River", "새 출발": "A New Start"}


@pytest.mark.parametrize("heading, parts", [
    ("3. 푸른 강의 전투 - 2", ("푸른 강의 전투", "", "2")),
    ("1. 푸른 강의 전투(Ⅱ) - 10", ("푸른 강의 전투", "II", "10")),
    ("9. 푸른 강의 전투Ⅰ", ("푸른 강의 전투", "I", "")),
    ("4. 푸른 강의 전투 -1", ("푸른 강의 전투", "", "1")),
    ("새 출발 (12)", ("새 출발", "", "12")),
    ("841화 새 출발", ("새 출발", "", "")),
    ("< 새 출발 >", ("< 새 출발 >", "", "")),
    ("5. 군국(軍國)의 망령(妄靈)", ("군국(軍國)의 망령(妄靈)", "", "")),
    ("12.", None),
])
def test_a_heading_splits_into_title_volume_and_part(heading, parts):
    assert ct.split_heading(heading) == parts


def test_a_settled_title_gives_the_exact_english_heading():
    assert ct.settled_heading(42, "1. 푸른 강의 전투(Ⅱ) - 3", TITLES) == "Chapter 42: The Battle of the Blue River II (3)"
    assert ct.settled_heading(7, "새 출발 (2)", TITLES) == "Chapter 7: A New Start (2)"
    assert ct.settled_heading(8, "8. 낯선 손님", TITLES) is None
    assert ct.settled_heading(8, "3. 푸른 강의 전투 - 2", {}) is None


def test_the_titles_file_is_read_and_a_malformed_one_is_ignored(tmp_path, capsys):
    assert ct.load_chapter_titles(tmp_path) == {}
    (tmp_path / ct.TITLES_FILE).write_text(json.dumps({"titles": {" 새 출발 ": "A New Start", "빈": ""}}),
                                           encoding="utf-8")
    assert ct.load_chapter_titles(tmp_path) == {"새 출발": "A New Start"}
    (tmp_path / ct.TITLES_FILE).write_text("{not json", encoding="utf-8")
    assert ct.load_chapter_titles(tmp_path) == {} and "not usable" in capsys.readouterr().err


def _prompt(settled):
    request = build_translation_request(
        chapter={"chapter": 3, "heading": "3. 푸른 강의 전투 - 2", "text": "3. 푸른 강의 전투 - 2\n본문."},
        config=SimpleNamespace(chapter_numbering="global", source_language="ko", target_language="en"),
        profile=None, system_prompt="system", continuity={"summary": ""}, model="m", controls={},
        rendered_glossary="None.", settled_heading=settled)
    return request["messages"][1]["content"]


def test_the_request_names_the_settled_heading_only_when_there_is_one():
    assert "THIS CHAPTER'S HEADING" not in _prompt(None)
    prompt = _prompt("Chapter 3: The Battle of the Blue River (2)")
    assert "THIS CHAPTER'S HEADING (settled):\n\nChapter 3: The Battle of the Blue River (2)\n" in prompt
    assert prompt.index("THIS CHAPTER'S HEADING") < prompt.index("본문.")


def test_the_engine_reads_the_novel_titles_file(tmp_path, monkeypatch):
    monkeypatch.setattr(production, "GLOSSARY_FILE", tmp_path / "glossary.json")
    monkeypatch.setattr(production, "chapter_map", {3: {"heading": "3. 푸른 강의 전투 - 2"}})
    assert production.settled_chapter_heading(3) is None
    (tmp_path / ct.TITLES_FILE).write_text(json.dumps({"titles": TITLES}, ensure_ascii=False), encoding="utf-8")
    assert production.settled_chapter_heading(3) == "Chapter 3: The Battle of the Blue River (2)"
    assert production.settled_chapter_heading(4) is None


def test_the_titles_command_lists_titles_and_headings_that_differ(tmp_path, monkeypatch, capsys):
    root = tmp_path / "novel"
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "data").mkdir()
    project = SimpleNamespace(root=root, chapter_dir=root / "output" / "chapters",
                              progress_file=root / "data" / "progress.json",
                              chapters_file=root / "data" / "chapters.json", glossary_file=root / "data" / "glossary.json")
    headings = ["1. 푸른 강의 전투 - 1", "2. 푸른 강의 전투 - 2", "3. 낯선 손님"]
    project.chapters_file.write_text(json.dumps([{"chapter": n, "heading": h, "text": h + "\n본문."}
                                                 for n, h in enumerate(headings, 1)], ensure_ascii=False),
                                     encoding="utf-8")
    project.glossary_file.write_text(json.dumps({"entries": {}}), encoding="utf-8")
    project.progress_file.write_text(json.dumps({"last_completed": 2}), encoding="utf-8")
    (root / "data" / ct.TITLES_FILE).write_text(json.dumps({"titles": TITLES}, ensure_ascii=False), encoding="utf-8")
    (project.chapter_dir / "0001.md").write_text("Chapter 1: The Battle of the Blue River (1)\n\nProse.", encoding="utf-8")
    (project.chapter_dir / "0002.md").write_text("Chapter 2: Blue River Battle - 2\n\nProse.", encoding="utf-8")
    monkeypatch.setattr(novel_project, "select_project", lambda novel, repo_root=None: project)
    assert gc.main(["--novel", "synthetic", "--json", "titles"]) == 0
    rows = json.loads(capsys.readouterr().out)["titles"]
    assert [(row["source"], row["chapters"], row["english"]) for row in rows] == [
        ("푸른 강의 전투", [1, 2], "The Battle of the Blue River")]
    assert rows[0]["mismatches"] == [{"chapter": 2, "expected": "Chapter 2: The Battle of the Blue River (2)",
                                      "found": "Chapter 2: Blue River Battle - 2"}]
