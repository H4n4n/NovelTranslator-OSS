"""Import-safe helpers for production-indexed source chapter libraries.

The legacy ``chapter`` field remains the engine's canonical key.  In
``spine_sequential`` projects it stores the global production index, while
the reader-facing source identity lives in explicit local-number fields.
"""

from collections import Counter

from novel_project import (
    CHAPTER_NUMBERING_GLOBAL,
    CHAPTER_NUMBERING_MODES,
    CHAPTER_NUMBERING_SPINE_SEQUENTIAL,
)


UNIT_KIND_CHAPTER = "chapter"
UNIT_KIND_SUPPLEMENT = "supplement"
UNIT_KIND_PROLOGUE = "prologue"
UNIT_KIND_EPILOGUE = "epilogue"
UNIT_KIND_SIDE_STORY = "side_story"
NAMED_UNIT_KINDS = frozenset((
    UNIT_KIND_SUPPLEMENT,
    UNIT_KIND_PROLOGUE,
    UNIT_KIND_EPILOGUE,
    UNIT_KIND_SIDE_STORY,
))
UNIT_KINDS = frozenset((UNIT_KIND_CHAPTER, *NAMED_UNIT_KINDS))


SOURCE_LANGUAGE_PROMPT_LABELS = {
    "zh": "CHINESE SOURCE",
    "ko": "KOREAN SOURCE",
}


def source_language_prompt_label(source_language):
    """Return a truthful provider-facing source label for supported languages."""
    if not isinstance(source_language, str) or not source_language.strip():
        return "SOURCE TEXT"
    normalized = source_language.strip().replace("_", "-").casefold()
    primary = normalized.split("-", 1)[0]
    return SOURCE_LANGUAGE_PROMPT_LABELS.get(primary, "SOURCE TEXT")


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def chapter_production_index(chapter):
    """Return one chapter's engine key, rejecting disagreeing aliases."""
    if not isinstance(chapter, dict):
        raise ValueError("chapter entry must be an object")
    legacy_index = chapter.get("chapter")
    explicit_index = chapter.get("production_index", legacy_index)
    if not _positive_integer(legacy_index):
        raise ValueError("chapter entry has an invalid chapter/production index")
    if not _positive_integer(explicit_index):
        raise ValueError("chapter entry has an invalid production_index")
    if explicit_index != legacy_index:
        raise ValueError("chapter and production_index disagree")
    return legacy_index


def production_unit_kind(unit):
    """Return a unit kind while treating every legacy entry as a chapter."""
    if not isinstance(unit, dict):
        raise ValueError("chapter entry must be an object")
    kind = unit.get("unit_kind", UNIT_KIND_CHAPTER)
    if kind not in UNIT_KINDS:
        raise ValueError(f"unsupported production unit kind: {kind!r}")
    return kind


def reader_facing_unit_identity(unit, chapter_numbering):
    """Return a safe fallback identity that never exposes a named-unit index."""
    production_index = chapter_production_index(unit)
    kind = production_unit_kind(unit)
    if kind in NAMED_UNIT_KINDS:
        return {
            UNIT_KIND_SUPPLEMENT: "Supplement",
            UNIT_KIND_PROLOGUE: "Prologue",
            UNIT_KIND_EPILOGUE: "Epilogue",
            UNIT_KIND_SIDE_STORY: "Side Story",
        }[kind]
    if chapter_numbering != CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
        return f"Chapter {production_index}"
    local_start = unit.get("local_chapter_start")
    local_end = unit.get("local_chapter_end")
    if not _positive_integer(local_start) or not _positive_integer(local_end):
        raise ValueError("reset-numbered chapter lacks a valid local chapter range")
    if local_end < local_start:
        raise ValueError("reset-numbered chapter has an invalid local chapter range")
    return (
        f"Chapter {local_start}"
        if local_start == local_end
        else f"Chapters {local_start}-{local_end}"
    )


def source_prompt_heading(
    chapter,
    chapter_numbering,
    source_language=None,
    target_language=None,
):
    """Build a control label that separates engine and reader-facing identity."""
    production_index = chapter_production_index(chapter)
    unit_kind = production_unit_kind(chapter)
    if unit_kind in NAMED_UNIT_KINDS:
        source_heading = chapter.get("heading") or chapter.get("title")
        if not isinstance(source_heading, str) or not source_heading.strip():
            raise ValueError(f"{unit_kind} lacks an authoritative source title")
        source_language = source_language.strip() if isinstance(
            source_language, str
        ) and source_language.strip() else "the source language"
        target_language = target_language.strip() if isinstance(
            target_language, str
        ) and target_language.strip() else "the target language"
        kind_label = (
            "SUPPLEMENTAL DOCUMENT"
            if unit_kind == UNIT_KIND_SUPPLEMENT
            else unit_kind.replace("_", " ").upper()
        )
        identity = reader_facing_unit_identity(chapter, chapter_numbering)
        heading_rule = (
            "Use only the natural translated supplemental title as the heading."
            if unit_kind == UNIT_KIND_SUPPLEMENT
            else "Use only the natural translated source title as the heading."
        )
        return (
            f"PRODUCTION UNIT {production_index} "
            "(INTERNAL CONTROL INDEX; NEVER USE IN OUTPUT HEADING)\n\n"
            f"UNIT KIND: {kind_label}\n"
            f"READER-FACING IDENTITY: {identity} "
            "(not an ordinary numbered chapter)\n"
            f"SOURCE TITLE TO TRANSLATE: {source_heading.strip()}\n"
            "OUTPUT HEADING REQUIREMENTS:\n"
            f"- Translate the source title from {source_language} into {target_language}.\n"
            f"- {heading_rule}\n"
            "- Do not prepend Chapter, invent a local number, or expose the "
            f"internal production unit {production_index}."
        )
    if chapter_numbering != CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
        return f"CHAPTER {production_index}"
    source_heading = chapter.get("heading")
    if not isinstance(source_heading, str) or not source_heading.strip():
        raise ValueError("reset-numbered chapter lacks an authoritative source heading")
    local_start = chapter.get("local_chapter_start")
    local_end = chapter.get("local_chapter_end")
    if not _positive_integer(local_start) or not _positive_integer(local_end):
        raise ValueError("reset-numbered chapter lacks a valid local chapter range")
    if local_end < local_start:
        raise ValueError("reset-numbered chapter has an invalid local chapter range")
    if local_start == local_end:
        local_identity = f"Chapter {local_start}"
    else:
        local_identity = f"Chapters {local_start}-{local_end}"

    source_language = (
        source_language.strip()
        if isinstance(source_language, str) and source_language.strip()
        else None
    )
    target_language = (
        target_language.strip()
        if isinstance(target_language, str) and target_language.strip()
        else None
    )
    if (
        source_language
        and target_language
        and source_language.casefold() != target_language.casefold()
    ):
        language_rule = (
            f"Translate the heading from {source_language} into {target_language}; "
            "do not copy source-language script into the reader-facing heading."
        )
    else:
        language_rule = (
            "Render the heading in the selected target language rather than "
            "copying untranslated source text."
        )
    return (
        f"PRODUCTION UNIT {production_index} "
        "(INTERNAL CONTROL INDEX; NEVER USE IN OUTPUT HEADING)\n\n"
        f"READER-FACING LOCAL IDENTITY: {local_identity}\n"
        f"SOURCE HEADING/TITLE TO TRANSLATE: {source_heading.strip()}\n"
        "OUTPUT HEADING REQUIREMENTS:\n"
        f"- Use the local identity {local_identity}; preserve a local range as a range.\n"
        f"- {language_rule}\n"
        "- Use the source heading as semantic input for the translated title, not "
        "as literal reader-facing text.\n"
        f"- Never substitute production unit {production_index} for the local identity."
    )


def validate_chapter_library(
    chapters,
    final_chapter,
    chapter_numbering=CHAPTER_NUMBERING_GLOBAL,
):
    """Return structural errors without changing the supplied library."""
    if chapter_numbering not in CHAPTER_NUMBERING_MODES:
        return (f"unsupported chapter numbering mode: {chapter_numbering}",)
    if not isinstance(chapters, list):
        return ("chapter library must contain a JSON array",)
    if not _positive_integer(final_chapter):
        return ("configured final_chapter must be a positive integer",)

    errors = []
    indexes = []
    previous_volume = None
    for position, chapter in enumerate(chapters, 1):
        try:
            production_index = chapter_production_index(chapter)
        except ValueError as exc:
            errors.append(f"chapter entry {position}: {exc}")
            continue
        indexes.append(production_index)

        try:
            unit_kind = production_unit_kind(chapter)
        except ValueError as exc:
            errors.append(f"chapter entry {position}: {exc}")
            continue

        if chapter_numbering != CHAPTER_NUMBERING_SPINE_SEQUENTIAL:
            if unit_kind in NAMED_UNIT_KINDS:
                errors.append(
                    f"{unit_kind} entry {position} requires spine_sequential numbering"
                )
            continue

        required = ("production_index", "volume_index")
        if unit_kind == UNIT_KIND_CHAPTER:
            required += (
                "local_chapter_number",
                "local_chapter_start",
                "local_chapter_end",
            )
        missing = [field for field in required if field not in chapter]
        if missing:
            errors.append(
                f"chapter entry {position} lacks reset-numbering metadata: "
                + ", ".join(missing)
            )
            continue
        volume_index = chapter.get("volume_index")
        if not _positive_integer(volume_index):
            errors.append(f"chapter entry {position} has invalid volume numbering")
        if unit_kind in NAMED_UNIT_KINDS:
            forbidden = (
                "local_chapter_number", "local_chapter_start", "local_chapter_end"
            )
            present = [field for field in forbidden if chapter.get(field) is not None]
            if present:
                errors.append(
                    f"{unit_kind} entry {position} must not define local chapter numbering: "
                    + ", ".join(present)
                )
            source_title = chapter.get("heading") or chapter.get("title")
            if not isinstance(source_title, str) or not source_title.strip():
                errors.append(f"{unit_kind} entry {position} lacks a source title")
        else:
            local_number = chapter.get("local_chapter_number")
            local_start = chapter.get("local_chapter_start")
            local_end = chapter.get("local_chapter_end")
            if not all(_positive_integer(value) for value in (
                local_number, local_start, local_end
            )):
                errors.append(
                    f"chapter entry {position} has invalid local/volume numbering"
                )
            elif local_number != local_start or local_end < local_start:
                errors.append(
                    f"chapter entry {position} has an invalid local chapter range"
                )
        if previous_volume is not None and _positive_integer(volume_index):
            if volume_index < previous_volume or volume_index > previous_volume + 1:
                errors.append(f"chapter entry {position} has a non-sequential volume index")
            if (
                unit_kind == UNIT_KIND_CHAPTER
                and volume_index == previous_volume + 1
                and chapter.get("local_chapter_start") != 1
            ):
                errors.append(
                    f"chapter entry {position} begins a volume without local Chapter 1"
                )
        if _positive_integer(volume_index):
            previous_volume = volume_index

    counts = Counter(indexes)
    duplicates = sorted(index for index, count in counts.items() if count > 1)
    if duplicates:
        errors.append(
            "duplicate production indexes: " + ", ".join(map(str, duplicates))
        )
    if chapters:
        expected = list(range(1, len(chapters) + 1))
        if indexes != expected:
            errors.append("chapter library production indexes are not continuous in order")
        if len(chapters) != final_chapter:
            errors.append(
                f"chapter library contains {len(chapters)} production units; "
                f"configured final_chapter is {final_chapter}"
            )
    return tuple(errors)


def index_chapter_library(chapters, final_chapter, chapter_numbering):
    """Validate a complete library and return it keyed by production index."""
    errors = validate_chapter_library(chapters, final_chapter, chapter_numbering)
    if isinstance(chapters, list) and not chapters:
        errors = (*errors, "chapter library is empty")
    if errors:
        raise ValueError("invalid chapter library: " + "; ".join(errors))
    return {chapter_production_index(item): item for item in chapters}
