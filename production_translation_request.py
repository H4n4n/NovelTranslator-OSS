"""Pure production translation-request construction shared across transports.

This module owns the provider-facing chapter context that must remain identical
whether a translation is sent synchronously or through an asynchronous Batch.
It performs no credential loading, API calls, filesystem I/O, or state mutation.

Provider transport is deliberately represented only by the final request-body
shape.  Glossary selection, continuity, chapter identity, and translation
instructions are transport-independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from chapter_library import source_language_prompt_label, source_prompt_heading
from noveltranslator_core import (
    STRICT_LOCKED_GLOSSARY_CATEGORIES,
    glossary_entries_to_text,
    relevant_glossary_entries,
)


SUPPORTED_TRANSLATION_PROVIDERS = frozenset({"deepseek", "openai"})
DEFAULT_CONTINUITY_TEXT = "No previous continuity summary is available."


def _profile_high_priority_categories(profile: Any) -> tuple[str, ...]:
    categories = getattr(profile, "high_priority_categories", ())
    if categories is None:
        return ()
    return tuple(categories)


def relevant_production_glossary(
    source_text: str,
    glossary_entries: Mapping[str, Mapping[str, Any]],
    profile: Any,
) -> list[dict[str, Any]]:
    """Select the same source-anchored glossary subset used by production."""
    high_priority = _profile_high_priority_categories(profile)
    strict_categories = (
        set(high_priority) & set(STRICT_LOCKED_GLOSSARY_CATEGORIES)
    )
    return relevant_glossary_entries(
        source_text,
        glossary_entries,
        source_variant_categories=strict_categories,
    )


def production_glossary_text(
    source_text: str,
    glossary_entries: Mapping[str, Mapping[str, Any]],
    profile: Any,
    *, chapter_number: int | None = None,
) -> str:
    """Render only glossary entries relevant to this exact source chapter."""
    high_priority = _profile_high_priority_categories(profile)
    return glossary_entries_to_text(
        relevant_production_glossary(source_text, glossary_entries, profile),
        high_priority_categories=high_priority,
        strict_locked_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
        chapter_number=chapter_number,
    )


def normalized_continuity_text(continuity_summary: str | None) -> str:
    """Match the production fallback for an empty continuity summary."""
    if isinstance(continuity_summary, str) and continuity_summary.strip():
        return continuity_summary.strip()
    return DEFAULT_CONTINUITY_TEXT


def build_production_translation_user_prompt(
    chapter: Mapping[str, Any],
    *,
    chapter_numbering: str,
    source_language: str | None,
    target_language: str | None,
    glossary_entries: Mapping[str, Mapping[str, Any]],
    profile: Any,
    continuity_summary: str | None,
    glossary_text_override: str | None = None,
) -> str:
    """Build the exact transport-independent user prompt for one chapter."""
    source_text = chapter.get("text")
    if not isinstance(source_text, str):
        raise ValueError("chapter text must be a string")

    source_heading = source_prompt_heading(
        dict(chapter),
        chapter_numbering,
        source_language=source_language,
        target_language=target_language,
    )
    source_label = source_language_prompt_label(source_language)
    glossary_text = (glossary_text_override if glossary_text_override is not None else
        production_glossary_text(source_text, glossary_entries, profile,
                                 chapter_number=chapter["chapter"]))
    continuity_text = normalized_continuity_text(continuity_summary)
    return f"""
ESTABLISHED GLOSSARY FOR THIS CHAPTER:

{glossary_text}

RECENT CONTINUITY:

{continuity_text}

{source_label} — {source_heading}:

{source_text}
""".strip()


def build_production_translation_messages(
    chapter: Mapping[str, Any],
    *,
    system_prompt: str,
    chapter_numbering: str,
    source_language: str | None,
    target_language: str | None,
    glossary_entries: Mapping[str, Mapping[str, Any]],
    profile: Any,
    continuity_summary: str | None,
    glossary_text_override: str | None = None,
) -> list[dict[str, str]]:
    """Return canonical system/user messages for one production translation."""
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("system_prompt must be a non-empty string")
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": build_production_translation_user_prompt(
                chapter,
                chapter_numbering=chapter_numbering,
                source_language=source_language,
                target_language=target_language,
                glossary_entries=glossary_entries,
                profile=profile,
                continuity_summary=continuity_summary,
                glossary_text_override=glossary_text_override,
            ),
        },
    ]


def build_production_translation_request(
    chapter: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    max_tokens: int,
    reasoning: str,
    system_prompt: str,
    chapter_numbering: str,
    source_language: str | None,
    target_language: str | None,
    glossary_entries: Mapping[str, Mapping[str, Any]],
    profile: Any,
    continuity_summary: str | None,
) -> dict[str, Any]:
    """Build one canonical Chat Completions request for Sync or Batch transport.

    DeepSeek production uses ``max_tokens`` plus its ``thinking`` extension.
    OpenAI Luna uses ``max_completion_tokens`` plus ``reasoning_effort``.  The
    messages are identical regardless of provider.
    """
    if provider not in SUPPORTED_TRANSLATION_PROVIDERS:
        raise ValueError(f"unsupported translation provider: {provider!r}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise ValueError("max_tokens must be a positive integer")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("reasoning must be a non-empty string")

    request: dict[str, Any] = {
        "model": model.strip(),
        "messages": build_production_translation_messages(
            chapter,
            system_prompt=system_prompt,
            chapter_numbering=chapter_numbering,
            source_language=source_language,
            target_language=target_language,
            glossary_entries=glossary_entries,
            profile=profile,
            continuity_summary=continuity_summary,
        ),
    }
    if provider == "deepseek":
        request.update(
            {
                "max_tokens": max_tokens,
                "extra_body": {"thinking": {"type": reasoning}},
            }
        )
    else:
        request.update(
            {
                "max_completion_tokens": max_tokens,
                "reasoning_effort": reasoning,
            }
        )
    return request


def build_translation_request(*, chapter, config, profile, system_prompt,
                              continuity, model, controls, glossary_entries=None, rendered_glossary=None):
    """Legacy operation controls around the shared canonical message builder."""
    return {"model": model, "messages": build_production_translation_messages(chapter,
        system_prompt=system_prompt, chapter_numbering=config.chapter_numbering if config else "global",
        source_language=config.source_language if config else None,
        target_language=config.target_language if config else None, glossary_entries=glossary_entries or {},
        profile=profile, continuity_summary=continuity.get("summary", ""),
        glossary_text_override=rendered_glossary), **controls}
