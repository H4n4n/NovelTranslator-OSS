import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from benchmark_novel import load_benchmark_inputs, request_kwargs
from chapter_library import source_language_prompt_label
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source_language", "expected"),
    [
        ("zh", "CHINESE SOURCE"),
        ("zh-Hans", "CHINESE SOURCE"),
        ("zh_Hant", "CHINESE SOURCE"),
        ("ko", "KOREAN SOURCE"),
        ("ko-KR", "KOREAN SOURCE"),
        ("ja", "SOURCE TEXT"),
        ("", "SOURCE TEXT"),
        (None, "SOURCE TEXT"),
    ],
)
def test_source_language_prompt_label_is_truthful_and_fail_generic(
    source_language,
    expected,
):
    assert source_language_prompt_label(source_language) == expected


def _make_korean_benchmark_project(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")
    config = NovelConfig(
        id="korean_prompt_fixture",
        title="Korean Prompt Fixture",
        source_language="ko",
        target_language="en",
        profile="korean_fantasy",
        final_chapter=1,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)
    source_text = "검으로 환생했다."
    project.chapters_file.write_text(
        json.dumps(
            [
                {
                    "chapter": 1,
                    "heading": "1화",
                    "source_file": "Text/0001.xhtml",
                    "paragraph_count": 1,
                    "character_count": len(source_text),
                    "text": source_text,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps({"entries": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return project


def test_korean_benchmark_request_uses_korean_source_label(tmp_path):
    project = _make_korean_benchmark_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)

    prompt = request_kwargs(inputs, 1, "luna")["messages"][1]["content"]

    assert "KOREAN SOURCE — CHAPTER 1:" in prompt
    assert "CHINESE SOURCE" not in prompt
    assert "검으로 환생했다." in prompt


def test_maintained_production_request_uses_korean_source_label(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        production,
        "ACTIVE_NOVEL_CONFIG",
        SimpleNamespace(
            chapter_numbering="global",
            source_language="ko",
            target_language="en",
        ),
    )
    monkeypatch.setattr(production, "MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(production, "system_prompt", "Korean translation policy")
    monkeypatch.setattr(production, "continuity", {"summary": ""})
    monkeypatch.setattr(production, "relevant_glossary", lambda _text: {})
    monkeypatch.setattr(production, "glossary_to_text", lambda _entries, *, chapter_number: "")
    monkeypatch.setattr(production, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(production, "stats_event_fields", lambda _stats: {})

    stats = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "pricing_period": "off_peak",
        "cache_hit_rate_per_million": 0.022,
        "cache_miss_rate_per_million": 0.66,
        "output_rate_per_million": 1.98,
        "cost_usd": 0.00001,
    }

    def fake_journaled_chat_request(
        operation_kind,
        chapter_number,
        source_text,
        request,
    ):
        captured["operation_kind"] = operation_kind
        captured["chapter_number"] = chapter_number
        captured["source_text"] = source_text
        captured["request"] = request
        return {
            "response": {
                "content": "I was reincarnated as a sword.",
                "finish_reason": "stop",
                "stats": stats,
            },
            "replayed": False,
            "operation_id": "test:korean-source-label",
        }

    monkeypatch.setattr(
        production,
        "journaled_chat_request",
        fake_journaled_chat_request,
    )

    translation, returned_stats, _elapsed, finish_reason = production.translate_chapter(
        {"chapter": 1, "text": "검으로 환생했다."}
    )

    user_prompt = captured["request"]["messages"][1]["content"]
    assert captured["operation_kind"] == "chapter_translation"
    assert captured["chapter_number"] == 1
    assert captured["source_text"] == "검으로 환생했다."
    assert "KOREAN SOURCE — CHAPTER 1:" in user_prompt
    assert "CHINESE SOURCE" not in user_prompt
    assert "검으로 환생했다." in user_prompt
    assert translation == "I was reincarnated as a sword."
    assert returned_stats is stats
    assert finish_reason == "stop"
