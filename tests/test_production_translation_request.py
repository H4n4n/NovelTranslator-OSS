import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from production_translation_request import (
    DEFAULT_CONTINUITY_TEXT,
    build_production_translation_messages,
    build_production_translation_request,
    normalized_continuity_text,
    relevant_production_glossary,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PRODUCTION = (
    REPO_ROOT
    / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)


def _load_legacy_production_module():
    spec = importlib.util.spec_from_file_location(
        "_translation_request_parity_legacy",
        LEGACY_PRODUCTION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stats():
    return {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 100,
        "cost_usd": 0.0001,
        "rate_correct_cost_usd": 0.0001,
        "pricing_version": "test",
        "pricing_period": "off_peak",
        "request_started_utc": "2026-09-15T00:00:00+00:00",
        "cache_hit_rate_per_million": 0.0,
        "cache_miss_rate_per_million": 0.0,
        "output_rate_per_million": 0.0,
    }


def _capture_legacy_translation_request(
    *,
    chapter,
    chapter_numbering,
    source_language,
    target_language,
    glossary_entries,
    high_priority_categories,
    continuity_summary,
    system_prompt,
):
    module = _load_legacy_production_module()
    module.MODEL = "deepseek-v4-pro"
    module.ACTIVE_NOVEL_CONFIG = SimpleNamespace(
        id="request_parity_fixture",
        chapter_numbering=chapter_numbering,
        source_language=source_language,
        target_language=target_language,
    )
    module.ACTIVE_PROFILE = SimpleNamespace(
        high_priority_categories=tuple(high_priority_categories),
    )
    module.glossary = {"entries": glossary_entries}
    module.continuity = {"summary": continuity_summary}
    module.system_prompt = system_prompt
    module.log_event = lambda *args, **kwargs: None

    captured = {}

    def fake_journaled_chat_request(
        operation_kind,
        chapter_number,
        source_text,
        request_kwargs,
        *,
        translation_text="",
    ):
        captured["operation_kind"] = operation_kind
        captured["chapter_number"] = chapter_number
        captured["source_text"] = source_text
        captured["request_kwargs"] = request_kwargs
        return {
            "response": {
                "content": "A complete fixture translation.",
                "finish_reason": "stop",
                "stats": _stats(),
                "provider_response_id": "fixture-response",
            },
            "operation_id": "fixture-operation",
            "request_sha256": "0" * 64,
            "api_called": False,
            "replayed": True,
        }

    module.journaled_chat_request = fake_journaled_chat_request
    module.translate_chapter(chapter)
    return captured


def _glossary():
    return {
        "李明": {
            "english": "Li Ming",
            "category": "character",
            "status": "locked",
        },
        "天门": {
            "english": "Heavenly Gate",
            "category": "location",
            "status": "provisional",
        },
        "Unused": {
            "english": "Must Not Appear",
            "category": "organization",
            "status": "provisional",
        },
    }


@pytest.mark.parametrize(
    (
        "chapter",
        "chapter_numbering",
        "source_language",
        "target_language",
        "continuity_summary",
    ),
    [
        (
            {
                "chapter": 7,
                "text": "李明抵达天门。",
            },
            "global",
            "zh",
            "en",
            "Li Ming has begun his journey.",
        ),
        (
            {
                "chapter": 9,
                "text": "리안은 성문 앞에 멈춰 섰다.",
                "heading": "제3장 귀환",
                "local_chapter_start": 3,
                "local_chapter_end": 3,
            },
            "spine_sequential",
            "ko",
            "en",
            "The prince has returned to the capital.",
        ),
    ],
)
def test_shared_deepseek_request_is_exactly_legacy_production_request(
    chapter,
    chapter_numbering,
    source_language,
    target_language,
    continuity_summary,
):
    glossary_entries = _glossary()
    profile = SimpleNamespace(
        high_priority_categories=("character", "cultivation realm"),
    )
    system_prompt = "TRANSLATE THE NOVEL FAITHFULLY."

    captured = _capture_legacy_translation_request(
        chapter=chapter,
        chapter_numbering=chapter_numbering,
        source_language=source_language,
        target_language=target_language,
        glossary_entries=glossary_entries,
        high_priority_categories=profile.high_priority_categories,
        continuity_summary=continuity_summary,
        system_prompt=system_prompt,
    )
    shared = build_production_translation_request(
        chapter,
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=12000,
        reasoning="disabled",
        system_prompt=system_prompt,
        chapter_numbering=chapter_numbering,
        source_language=source_language,
        target_language=target_language,
        glossary_entries=glossary_entries,
        profile=profile,
        continuity_summary=continuity_summary,
    )

    assert captured["operation_kind"] == "chapter_translation"
    assert captured["chapter_number"] == chapter["chapter"]
    assert captured["source_text"] == chapter["text"]
    assert captured["request_kwargs"] == shared


def test_openai_and_deepseek_share_identical_chapter_context_messages():
    chapter = {
        "chapter": 18,
        "text": "李明再次经过天门。",
    }
    profile = SimpleNamespace(high_priority_categories=("character",))
    common = dict(
        chapter=chapter,
        model="ignored-by-common",
        max_tokens=12000,
        reasoning="disabled",
        system_prompt="SYSTEM",
        chapter_numbering="global",
        source_language="zh",
        target_language="en",
        glossary_entries=_glossary(),
        profile=profile,
        continuity_summary="Prior accepted Batch state.",
    )
    deepseek = build_production_translation_request(
        provider="deepseek",
        **{**common, "model": "deepseek-v4-pro"},
    )
    openai = build_production_translation_request(
        provider="openai",
        **{**common, "model": "gpt-5.6-luna", "reasoning": "none"},
    )

    assert deepseek["messages"] == openai["messages"]
    assert deepseek["max_tokens"] == 12000
    assert deepseek["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "max_completion_tokens" not in deepseek
    assert "reasoning_effort" not in deepseek

    assert openai["max_completion_tokens"] == 12000
    assert openai["reasoning_effort"] == "none"
    assert "max_tokens" not in openai
    assert "extra_body" not in openai


def test_empty_continuity_uses_the_existing_production_fallback():
    assert normalized_continuity_text("") == DEFAULT_CONTINUITY_TEXT
    assert normalized_continuity_text("   ") == DEFAULT_CONTINUITY_TEXT
    assert normalized_continuity_text(None) == DEFAULT_CONTINUITY_TEXT
    assert normalized_continuity_text("  remembered state  ") == "remembered state"

    messages = build_production_translation_messages(
        {"chapter": 1, "text": "短章。"},
        system_prompt="SYSTEM",
        chapter_numbering="global",
        source_language="zh",
        target_language="en",
        glossary_entries={},
        profile=SimpleNamespace(high_priority_categories=()),
        continuity_summary="",
    )
    assert DEFAULT_CONTINUITY_TEXT in messages[1]["content"]


def test_relevant_glossary_selection_remains_source_anchored():
    selected = relevant_production_glossary(
        "李明抵达天门。",
        _glossary(),
        SimpleNamespace(high_priority_categories=("character",)),
    )
    by_source = {item["chinese"] for item in selected}
    assert by_source == {"李明", "天门"}
    assert "Unused" not in by_source


def test_request_builder_rejects_unknown_provider_before_transport():
    with pytest.raises(ValueError, match="unsupported translation provider"):
        build_production_translation_request(
            {"chapter": 1, "text": "第一章"},
            provider="mystery",
            model="model",
            max_tokens=100,
            reasoning="none",
            system_prompt="SYSTEM",
            chapter_numbering="global",
            source_language="zh",
            target_language="en",
            glossary_entries={},
            profile=SimpleNamespace(high_priority_categories=()),
            continuity_summary="",
        )
