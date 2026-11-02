from types import SimpleNamespace

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from production_translation_request import build_translation_request


CHAPTER = {"chapter": 3, "heading": "Settling In (2)", "text": "Settling In (2)\nSource prose."}


def _user_prompt(previous_heading):
    request = build_translation_request(
        chapter=CHAPTER, config=SimpleNamespace(chapter_numbering="global", source_language="ko", target_language="en"),
        profile=None, system_prompt="system", continuity={"summary": ""}, model="m", controls={},
        rendered_glossary="None.", previous_heading=previous_heading)
    return request["messages"][1]["content"]


def test_prompt_shows_the_previous_heading_only_when_there_is_one():
    assert "PREVIOUS CHAPTER HEADING" not in _user_prompt(None)
    prompt = _user_prompt(("Settling In (1)", "Taking Stock (1)"))
    assert "Source: Settling In (1)\nEnglish: Taking Stock (1)\n" in prompt
    assert prompt.index("PREVIOUS CHAPTER HEADING") < prompt.index("Source prose.")


def test_previous_heading_comes_from_the_committed_chapter(tmp_path, monkeypatch):
    monkeypatch.setattr(production, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(production, "chapter_map", {2: {"heading": "Settling In (1)"}, 3: CHAPTER})
    assert production.previous_chapter_heading(3) is None
    (tmp_path / "0002.md").write_text("\nTaking Stock (1)\n\nProse.\n", encoding="utf-8")
    assert production.previous_chapter_heading(3) == ("Settling In (1)", "Taking Stock (1)")
    assert production.previous_chapter_heading(2) is None
