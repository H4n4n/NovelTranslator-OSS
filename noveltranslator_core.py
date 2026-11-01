"""Dependency-free, import-safe helpers for NovelTranslator.

This module must never load credentials, call an API, or mutate project data.
It provides the small decision functions shared by production and local tests.
"""

import json
import os
import time
import re
import uuid
from bisect import bisect_right
from datetime import timedelta
from hashlib import sha256
from pathlib import Path


PEAK_UTC_WINDOWS = ((1, 4), (6, 10))
PEAK_UTC_WEEKDAYS = frozenset(range(5))
PRODUCTION_TRANSLATION_MODEL = "deepseek-v4-pro"
PRODUCTION_TRANSLATION_MAX_TOKENS = 12000
PRODUCTION_TRANSLATION_THINKING = "disabled"
V4_PRO_PRICES = {
    "off_peak": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
    "peak": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
}
PRICING_VERSION = "deepseek-v4-pro-0813-peak-offpeak"
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
PERMANENT_API_ERROR_MARKERS = (
    "authentication", "api key", "invalid request", "invalid_request",
    "insufficient", "balance", "billing", "credit",
)
ACCOUNTING_FIELDS = (
    "prompt_tokens", "completion_tokens", "cache_hit_tokens",
    "cache_miss_tokens", "cost_usd", "rate_correct_cost_usd",
)

ALLOWED_GLOSSARY_CATEGORIES = {
    "character", "alias", "form of address", "historical person",
    "historical office", "military rank", "military unit", "state",
    "organization", "faction", "sect", "location", "technique", "artifact",
    "specialized recurring term",
}

RETROACTIVE_SAFE_CATEGORIES = ALLOWED_GLOSSARY_CATEGORIES - {"form of address"}
RETROACTIVE_UNSAFE_GENERIC_ENGLISH = {
    "captain", "commander", "general", "master", "lord", "chief", "officer",
    "soldier", "guard", "army", "city", "town", "village", "fort", "island",
    "river", "mountain", "temple", "palace", "pavilion",
}

GENERIC_SUB_LOCATION_SUFFIXES = (
    "後門", "后门", "前門", "前门", "後院", "后院", "前院", "大門", "大门",
    "側門", "侧门", "內院", "内院", "外院", "倉庫", "仓库", "廚房", "厨房",
)
GENERIC_SUB_LOCATION_ENGLISH = (
    "back gate", "front gate", "rear courtyard", "front courtyard",
    "inner courtyard", "outer courtyard", "warehouse", "kitchen",
)
GENERIC_ARTIFACT_WORDS = (
    "chair", "table", "mirror", "bowl", "cup", "door", "gate", "box",
    "basket", "bed", "lamp", "lantern", "pot", "jar",
)
GENERIC_ACTIVITY_WORDS = (
    "duty", "patrol", "work", "strategy", "attack", "retreat", "guard duty",
    "night patrol",
)
DEFAULT_LOW_VALUE_CATEGORY_WORDS = {
    "artifact": GENERIC_ARTIFACT_WORDS,
    "specialized recurring term": GENERIC_ACTIVITY_WORDS,
}
DEFAULT_LONG_PHRASE_CATEGORIES = {
    "organization", "military unit", "historical office",
    "specialized recurring term",
}
# Narrow legacy compatibility allowlist; profile importance cannot expand it.
STRICT_LOCKED_GLOSSARY_CATEGORIES = frozenset({"cultivation realm"})
REFERENTIAL_STRICT_CATEGORIES = frozenset({"character"})
AUTO_PLURAL_STRICT_CATEGORIES = frozenset({
    "progression rank", "rank", "class", "profession", "military rank",
    "system term",
})
AUTO_PLURAL_SINGLE_WORD_CATEGORIES = AUTO_PLURAL_STRICT_CATEGORIES - {
    "system term",
}
OBVIOUS_ENGLISH_PLURALS = frozenset({
    "men", "women", "people", "children", "feet", "teeth", "geese",
    "mice", "oxen", "data", "criteria", "phenomena", "series", "species",
})
NONCOUNTABLE_TERMINOLOGY_FINAL_WORDS = frozenset({
    "qi", "mana", "energy", "power", "magic", "knowledge", "information",
    "equipment", "progress", "currency", "gold", "silver", "water", "fire",
    "earth", "air",
})
NESTED_STRICT_SOURCE_OWNER_FIELD = "owns_nested_strict_source_terms"
STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD = "repair_localization_anchors"

CHINESE_CHAPTER_HEADING_RE = re.compile(r"第\s*(\d+)\s*章")
ENGLISH_CHAPTER_HEADING_RE = re.compile(
    r"^Chapter\s+(\d+)(?:\s*:\s*(.+))?$",
    flags=re.IGNORECASE,
)
CJK_FRAGMENT_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+"
)
HANGUL_FRAGMENT_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]+")
KOREAN_SOURCE_FRAGMENT_RE = re.compile(
    r"[\u1100-\u11ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002fa1f]+"
)
METALINGUISTIC_TARGET_CUE_RE = re.compile(
    r"(?<![A-Za-z])(?:the\s+)?(?:(?:Chinese|Japanese|Korean)\s+)?"
    r"(?:character|glyph|kanji|hanja|hangul(?:\s+(?:character|glyph))?)"
    r"\s*(?:[:=\-\u2013\u2014]\s*)?[\"'\u2018\u201c\u300c\u300e\u300a\u3008"
    r"\(\[\{*_`]{0,3}\s*$",
    flags=re.IGNORECASE,
)
SOURCE_GLYPH_WRAPPER_RE = r"[\s\"'\u2018\u2019\u201c\u201d\u300c\u300d\u300e\u300f\u300a\u300b\u3008\u3009\(\)\[\]\{\}\u3010\u3011]{0,4}"
CHINESE_SOURCE_GLYPH_AFTER_RE = re.compile(
    rf"^{SOURCE_GLYPH_WRAPPER_RE}(?:\u5b57(?:\u5f62|\u578b)?|\u5b57\u7b26|\u6c49\u5b57|\u6f22\u5b57)"
)
CHINESE_SOURCE_GLYPH_BEFORE_RE = re.compile(
    rf"(?:\u5b57\u7b26|\u6c49\u5b57|\u6f22\u5b57|\u5b57){SOURCE_GLYPH_WRAPPER_RE}$"
)
KOREAN_SOURCE_GLYPH_AFTER_RE = re.compile(
    rf"^{SOURCE_GLYPH_WRAPPER_RE}(?:(?:\uc774)?\ub77c\ub294\s*)?"
    rf"(?:\uae00\uc790|\ubb38\uc790|\ud55c\uae00|\ud55c\uc790|\uc790\ud615)"
)
KOREAN_SOURCE_GLYPH_BEFORE_RE = re.compile(
    rf"(?:\uae00\uc790|\ubb38\uc790|\ud55c\uae00|\ud55c\uc790|\uc790\ud615)"
    rf"{SOURCE_GLYPH_WRAPPER_RE}$"
)
CANONICAL_TERM_WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)?"
)
SAFE_CANONICAL_SEPARATOR_RE = (
    r"(?:[ \t]*[,\uff0c\-\u2010\u2011\u2012\u2013\u2014][ \t]*|[ \t]+)"
)


def normalize_category(category):
    return (category or "").strip().lower().replace("_", " ").replace("-", " ")


def format_technique_name(text):
    if not text:
        return text
    return " ".join(word[:1].upper() + word[1:] for word in text.strip().split())


def deepseek_pricing_period(moment):
    """Return ``peak`` or ``off_peak`` for an aware UTC datetime.

    DeepSeek V4 Pro peak pricing applies only Monday through Friday, during
    the two half-open UTC windows in ``PEAK_UTC_WINDOWS``. The helper is
    pure and intentionally rejects naive or non-UTC datetimes so the local
    machine timezone can never silently affect billing classification.
    """
    if moment.utcoffset() is None:
        raise ValueError("DeepSeek pricing requires a timezone-aware UTC datetime")
    if moment.utcoffset() != timedelta(0):
        raise ValueError("DeepSeek pricing datetime must use UTC")
    if moment.weekday() not in PEAK_UTC_WEEKDAYS:
        return "off_peak"
    utc_hour = (
        moment.hour
        + moment.minute / 60
        + moment.second / 3600
        + moment.microsecond / 3_600_000_000
    )
    if any(start <= utc_hour < end for start, end in PEAK_UTC_WINDOWS):
        return "peak"
    return "off_peak"


def is_deepseek_peak_time(moment):
    return deepseek_pricing_period(moment) == "peak"


def next_off_peak_time(moment):
    if not is_deepseek_peak_time(moment):
        return moment
    utc_hour = (
        moment.hour
        + moment.minute / 60
        + moment.second / 3600
        + moment.microsecond / 3_600_000_000
    )
    for start, end in PEAK_UTC_WINDOWS:
        if start <= utc_hour < end:
            return moment.replace(hour=end, minute=0, second=0, microsecond=0)
    return moment


def api_permission_blocker(pause_exists, auto_off_peak_only, moment):
    """Return the local reason an API request must wait, or None."""
    if pause_exists:
        return "manual_pause"
    if auto_off_peak_only and is_deepseek_peak_time(moment):
        return "peak"
    return None


def pricing_snapshot(moment):
    period = deepseek_pricing_period(moment)
    rates = V4_PRO_PRICES[period]
    return {
        "pricing_version": PRICING_VERSION,
        "pricing_period": period,
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": rates["cache_hit"],
        "cache_miss_rate_per_million": rates["cache_miss"],
        "output_rate_per_million": rates["output"],
    }


def calculate_cost_from_token_counts(
    prompt_tokens, completion_tokens, cache_hits, cache_misses, pricing_info
):
    if cache_hits == 0 and cache_misses == 0:
        cache_misses = prompt_tokens
    cost = (
        cache_misses / 1_000_000 * pricing_info["cache_miss_rate_per_million"]
        + cache_hits / 1_000_000 * pricing_info["cache_hit_rate_per_million"]
        + completion_tokens / 1_000_000 * pricing_info["output_rate_per_million"]
    )
    return cost, cache_hits, cache_misses


_NAME_PARTS_THAT_ARE_TITLES = frozenset({"남작", "자작", "백작", "후작", "공작", "왕자", "공자", "공주", "기사", "영주", "가문의"})


def _glossary_source_forms(chinese_term, category, variant_categories=()):
    forms = [chinese_term]
    category = normalize_category(category)
    variant_categories = {normalize_category(item) for item in variant_categories}
    if category in variant_categories and chinese_term.endswith("境"):
        if chinese_term.endswith("之境"):
            forms.append(chinese_term[:-2] + "境")
        else:
            forms.append(chinese_term[:-1] + "之境")
    if category == "character":
        # A chapter may use only part of a full name, and then still needs the agreed spelling.
        forms.extend(part for part in re.split(r"[\s·・.]+", chinese_term)
                     if len(part) >= 2 and part not in _NAME_PARTS_THAT_ARE_TITLES)
    return tuple(dict.fromkeys(forms))


def relevant_glossary_entries(chinese_text, entries, source_variant_categories=()):
    """Select glossary entries whose exact Chinese key occurs in the source."""
    relevant = []
    for chinese_term, entry in entries.items():
        source_forms = _glossary_source_forms(
            chinese_term,
            entry.get("category", ""),
            source_variant_categories,
        )
        if not any(source_form in chinese_text for source_form in source_forms):
            continue
        item = {
            "chinese": chinese_term,
            "english": entry.get("english", ""),
            "category": entry.get("category", ""),
            "status": entry.get("status", "provisional"),
            "notes": entry.get("notes", ""),
            "alias_of": entry.get("alias_of", ""),
        }
        if "strict_qa" in entry:
            item["strict_qa"] = entry["strict_qa"]
        if "strict_qa_from_chapter" in entry:
            item["strict_qa_from_chapter"] = entry["strict_qa_from_chapter"]
        if "auto_repair" in entry:
            item["auto_repair"] = entry["auto_repair"]
        if "repair_aliases" in entry:
            item["repair_aliases"] = list(entry["repair_aliases"])
        if STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD in entry:
            declarations = entry[STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD]
            item[STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD] = (
                [
                    dict(declaration)
                    if isinstance(declaration, dict) else declaration
                    for declaration in declarations
                ]
                if isinstance(declarations, list) else declarations
            )
        if "accepted_inflections" in entry:
            item["accepted_inflections"] = list(entry["accepted_inflections"])
        if "auto_plural" in entry:
            item["auto_plural"] = entry["auto_plural"]
        if entry.get("status") == "contextual":
            item["approved_renderings"] = entry.get("approved_renderings", [])
        relevant.append(item)
    return relevant


def glossary_strict_qa_errors(
    entries,
    novel_id="(unknown novel)",
    high_priority_categories=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
):
    """Return errors for per-entry strict QA and automatic-repair metadata."""
    errors = []
    if not isinstance(entries, dict):
        return [f"novel {novel_id}: glossary entries must be an object"]
    effective_strict_items = configured_strict_glossary_entry_items(
        entries, high_priority_categories, strict_categories
    )
    effective_strict_sources = {source for source, _ in effective_strict_items}
    for source_term, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        nested_owner = entry.get(NESTED_STRICT_SOURCE_OWNER_FIELD, False)
        if NESTED_STRICT_SOURCE_OWNER_FIELD in entry:
            if not isinstance(nested_owner, bool):
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    f"{NESTED_STRICT_SOURCE_OWNER_FIELD} must be a boolean"
                )
                nested_owner = False
            elif nested_owner:
                approved_renderings = entry.get("approved_renderings", [])
                if not isinstance(approved_renderings, (list, tuple)):
                    approved_renderings = []
                configured_targets = [
                    item for item in (
                        (entry.get("english", "") or "").strip(),
                        *approved_renderings,
                    )
                    if isinstance(item, str) and item.strip()
                ]
                nested_strict_sources = []
                for strict_source, strict_entry in effective_strict_items:
                    strict_forms = _glossary_source_forms(
                        strict_source,
                        normalize_category(strict_entry.get("category", "")),
                        variant_categories={
                            normalize_category(item) for item in strict_categories
                        },
                    )
                    if any(
                        source_form != source_term and source_form in source_term
                        for source_form in strict_forms
                    ):
                        nested_strict_sources.append(strict_source)
                if source_term in effective_strict_sources:
                    errors.append(
                        f"novel {novel_id}, source term {source_term!r}: "
                        f"{NESTED_STRICT_SOURCE_OWNER_FIELD}=true is redundant "
                        "on an effective-strict entry"
                    )
                elif not nested_strict_sources:
                    errors.append(
                        f"novel {novel_id}, source term {source_term!r}: "
                        f"{NESTED_STRICT_SOURCE_OWNER_FIELD}=true requires a "
                        "proper compound containing an effective-strict source term"
                    )
                if not configured_targets:
                    errors.append(
                        f"novel {novel_id}, source term {source_term!r}: "
                        f"{NESTED_STRICT_SOURCE_OWNER_FIELD}=true requires at least "
                        "one configured English rendering"
                    )
        strict_qa = entry.get("strict_qa", False)
        if "strict_qa" in entry:
            if not isinstance(strict_qa, bool):
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "strict_qa must be a boolean"
                )
                strict_qa = False
            elif strict_qa and entry.get("status") != "locked":
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "strict_qa=true requires status=locked"
                )
        if "strict_qa_from_chapter" in entry:
            boundary = entry["strict_qa_from_chapter"]
            if entry.get("status") != "locked" or entry.get("strict_qa") is not True:
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "strict_qa_from_chapter requires status=locked and strict_qa=true"
                )
            if type(boundary) is not int or boundary < 1:
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "strict_qa_from_chapter must be a positive integer (not boolean)"
                )
        auto_plural_present = "auto_plural" in entry
        auto_plural = entry.get("auto_plural")
        if auto_plural_present and not isinstance(auto_plural, bool):
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "auto_plural must be a boolean"
            )
        auto_repair_present = "auto_repair" in entry
        auto_repair = entry.get("auto_repair")
        if auto_repair_present and not isinstance(auto_repair, bool):
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "auto_repair must be a boolean"
            )
            continue
        aliases_present = "repair_aliases" in entry
        aliases = entry.get("repair_aliases", [])
        if aliases_present and auto_repair is False:
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "repair_aliases conflicts with auto_repair=false"
            )
        if not isinstance(aliases, list):
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "repair_aliases must be a list of non-empty strings"
            )
            continue
        normalized_aliases = []
        for index, alias in enumerate(aliases):
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or alias != alias.strip()
                or "\n" in alias
                or "\r" in alias
            ):
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    f"repair_aliases[{index}] must be a non-empty single-line string "
                    "without surrounding whitespace"
                )
                continue
            normalized_aliases.append(alias.casefold())
        if len(set(normalized_aliases)) != len(normalized_aliases):
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "repair_aliases contains duplicates"
            )
        canonical = (entry.get("english", "") or "").strip()
        if canonical and canonical.casefold() in normalized_aliases:
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "repair_aliases must not contain the canonical target"
            )
        inflections_present = "accepted_inflections" in entry
        inflections = entry.get("accepted_inflections", [])
        normalized_inflections = []
        if not isinstance(inflections, list):
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                "accepted_inflections must be a list of non-empty strings"
            )
        else:
            for index, inflection in enumerate(inflections):
                if (
                    not isinstance(inflection, str)
                    or not inflection.strip()
                    or inflection != inflection.strip()
                    or "\n" in inflection
                    or "\r" in inflection
                ):
                    errors.append(
                        f"novel {novel_id}, source term {source_term!r}: "
                        f"accepted_inflections[{index}] must be a non-empty "
                        "single-line string without surrounding whitespace"
                    )
                    continue
                normalized_inflections.append(inflection.casefold())
            if len(set(normalized_inflections)) != len(normalized_inflections):
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "accepted_inflections contains duplicates"
                )
            if canonical and canonical.casefold() in normalized_inflections:
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "accepted_inflections must not contain the canonical target"
                )
            overlap = sorted(set(normalized_aliases) & set(normalized_inflections))
            if overlap:
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: "
                    "accepted_inflections must not overlap repair_aliases"
                )
        localization_anchors_present = (
            STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD in entry
        )
        localization_anchors = entry.get(
            STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD, []
        )
        if localization_anchors_present:
            _, declaration_errors = _parse_repair_localization_anchors(entry)
            errors.extend(
                f"novel {novel_id}, source term {source_term!r}: {message}"
                for message in declaration_errors
            )
        if (
            auto_repair_present
            or aliases_present
            or inflections_present
            or auto_plural_present
            or localization_anchors_present
        ):
            if source_term not in effective_strict_sources:
                field = (
                    "auto_repair" if auto_repair_present else
                    "repair_aliases" if aliases_present else
                    "accepted_inflections" if inflections_present else
                    STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD
                    if localization_anchors_present else
                    "auto_plural"
                )
                errors.append(
                    f"novel {novel_id}, source term {source_term!r}: {field} "
                    "requires a locked effective-strict glossary entry"
                )
        if localization_anchors_present and auto_repair is False:
            errors.append(
                f"novel {novel_id}, source term {source_term!r}: "
                f"{STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD} conflicts with "
                "auto_repair=false"
            )
    return errors


def legacy_strict_glossary_categories(
    high_priority_categories=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
):
    """Retain the established profile gate on the narrow legacy allowlist.

    This intersection is compatibility policy, not permission to enforce every
    high-priority category. New hard constraints require entry-level strict_qa.
    """
    return frozenset(normalize_category(item) for item in strict_categories) & {
        normalize_category(item) for item in high_priority_categories
    }


def configured_strict_glossary_entry_origin(entry, legacy_categories=()):
    """Classify hard enforcement as legacy, explicit, both, or None.

    Locked establishes canonical authority only. Contextual and provisional
    entries never become hard constraints, even with invalid opt-in metadata;
    glossary_strict_qa_errors separately rejects that metadata at startup.
    legacy_categories must be the normalized compatibility intersection.
    """
    if not isinstance(entry, dict) or entry.get("status") != "locked":
        return None
    legacy_hard = normalize_category(entry.get("category", "")) in legacy_categories
    explicit_hard = entry.get("strict_qa") is True
    if legacy_hard and explicit_hard:
        return "legacy+explicit"
    if legacy_hard:
        return "legacy"
    if explicit_hard:
        return "explicit"
    return None


def explicit_strict_active(entry, chapter_number=None):
    """Evaluate explicit policy; bounded rules require an artifact chapter.

    Unbounded entries retain PR #153 behavior. No progress/global state is read.
    Metadata validation uses configured selectors instead of this runtime gate.
    """
    if chapter_number is not None and (type(chapter_number) is not int or chapter_number < 1):
        raise ValueError("chapter_number must be a positive integer (not boolean)")
    if "strict_qa_from_chapter" in entry:
        boundary = entry["strict_qa_from_chapter"]
        if (entry.get("status") != "locked" or entry.get("strict_qa") is not True
                or type(boundary) is not int or boundary < 1):
            raise ValueError("invalid strict_qa_from_chapter metadata")
        if chapter_number is None:
            raise ValueError("chapter_number is required for activation-bounded strict QA")
        return chapter_number >= boundary
    return entry.get("status") == "locked" and entry.get("strict_qa") is True


def strict_glossary_entry_origin(entry, legacy_categories=(), *, chapter_number=None):
    """Return chapter-active legacy/explicit origins without disabling legacy."""
    if not isinstance(entry, dict):
        return None
    explicit = explicit_strict_active(entry, chapter_number)
    if entry.get("status") != "locked":
        return None
    legacy = normalize_category(entry.get("category", "")) in legacy_categories
    return ("legacy+explicit" if legacy and explicit else
            "legacy" if legacy else "explicit" if explicit else None)


def _strict_entry_items(entries):
    return entries.items() if isinstance(entries, dict) else (
        (entry.get("chinese", ""), entry) for entry in entries
    )


def configured_strict_glossary_entry_items(
    entries, high_priority_categories=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
):
    """Configured hard metadata independent of chapter, for validation/inventory."""
    legacy = legacy_strict_glossary_categories(high_priority_categories, strict_categories)
    return tuple((source, entry) for source, entry in _strict_entry_items(entries)
                 if configured_strict_glossary_entry_origin(entry, legacy) is not None)


def effective_strict_glossary_entry_items(
    entries, high_priority_categories=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES, *, chapter_number=None,
):
    """Chapter-effective hard policy; missing context fails closed for bounded rules.

    High priority alone never promotes an entry. Unbounded entries retain their
    historical semantics; an explicit future boundary cannot disable legacy.
    """
    legacy = legacy_strict_glossary_categories(high_priority_categories, strict_categories)
    return tuple((source, entry) for source, entry in _strict_entry_items(entries)
                 if strict_glossary_entry_origin(entry, legacy,
                                                chapter_number=chapter_number) is not None)


def repair_eligible_strict_glossary_entry_items(
    entries, high_priority_categories=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES, *, chapter_number=None,
):
    """Chapter-effective strict entries not explicitly opted out of repair."""
    return tuple(
        (source_term, entry)
        for source_term, entry in effective_strict_glossary_entry_items(
            entries, high_priority_categories, strict_categories, chapter_number=chapter_number
        )
        if entry.get("auto_repair") is not False
    )


def _conservative_regular_english_plural(canonical):
    """Return one safe regular plural, or an empty string when uncertain."""
    canonical = (canonical or "").strip()
    if not re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+)*", canonical):
        return ""
    prefix, separator, final_word = canonical.rpartition(" ")
    lower = final_word.casefold()
    if (
        len(final_word) < 3
        or lower in OBVIOUS_ENGLISH_PLURALS
        or lower in NONCOUNTABLE_TERMINOLOGY_FINAL_WORDS
        or lower.endswith(("ed", "ing"))
    ):
        return ""
    if lower.endswith("y"):
        if len(final_word) < 2:
            return ""
        plural_word = (
            final_word + "s"
            if lower[-2] in "aeiou"
            else final_word[:-1] + "ies"
        )
    elif lower.endswith(("ch", "sh", "x")):
        plural_word = final_word + "es"
    elif lower.endswith("z"):
        plural_word = final_word + ("es" if lower.endswith("zz") else "zes")
    elif lower.endswith("s"):
        # Most final-s forms are already plural. The conservative exceptions
        # cover common regular singular endings without pluralizing Points,
        # Knights, and similar established plural labels again.
        if lower.endswith(("ss", "us")):
            plural_word = final_word + "es"
        else:
            return ""
    else:
        plural_word = final_word + "s"
    return (prefix + separator + plural_word) if separator else plural_word


def automatic_strict_term_inflections(entry):
    """Return category-gated regular plurals valid as final strict output."""
    if not isinstance(entry, dict) or entry.get("auto_plural") is False:
        return ()
    category = normalize_category(entry.get("category", ""))
    if category not in AUTO_PLURAL_STRICT_CATEGORIES:
        return ()
    canonical = (entry.get("english", "") or "").strip()
    if category not in AUTO_PLURAL_SINGLE_WORD_CATEGORIES and " " not in canonical:
        return ()
    plural = _conservative_regular_english_plural(canonical)
    if not plural or plural.casefold() == canonical.casefold():
        return ()
    return (plural,)


def strict_term_valid_targets(entry):
    """Build the canonical, explicit, and safely automatic target family."""
    candidates = [(entry.get("english", "") or "").strip()]
    candidates.extend(
        item for item in entry.get("accepted_inflections", [])
        if isinstance(item, str) and item
    )
    candidates.extend(automatic_strict_term_inflections(entry))
    result = []
    seen = set()
    for candidate in candidates:
        key = candidate.casefold()
        if not candidate or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def _parse_repair_localization_anchors(entry):
    """Parse explicit partial-anchor -> accepted-target declarations.

    Startup validation and runtime localization share this parser. Any
    malformed declaration invalidates the complete set, so bypassing startup
    validation cannot enable a partially trusted locator configuration.
    """
    field = STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD
    declarations = entry.get(field, []) if isinstance(entry, dict) else []
    if not isinstance(declarations, list) or not declarations:
        return (), (
            f"{field} must be a non-empty list of objects with exactly "
            "anchor and target fields",
        )
    valid_targets = strict_term_valid_targets(entry)
    valid_target_set = set(valid_targets)
    accepted_casefold = {target.casefold() for target in valid_targets}
    mappings = []
    errors = []
    anchor_keys = []
    mapping_keys = []
    for index, declaration in enumerate(declarations):
        label = f"{field}[{index}]"
        if not isinstance(declaration, dict) or set(declaration) != {
            "anchor", "target"
        }:
            errors.append(
                f"{label} must be an object with exactly anchor and target fields"
            )
            continue
        anchor = declaration["anchor"]
        target = declaration["target"]
        invalid_text = any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or "\n" in value
            or "\r" in value
            for value in (anchor, target)
        )
        if invalid_text:
            errors.append(
                f"{label} anchor and target must be non-empty single-line "
                "strings without surrounding whitespace"
            )
            continue
        anchor_words = CANONICAL_TERM_WORD_RE.findall(anchor)
        if len(anchor) > 80 or not anchor_words or len(anchor_words) > 6:
            errors.append(f"{label}.anchor must be terminology-sized")
            continue
        if target not in valid_target_set:
            errors.append(
                f"{label}.target {target!r} is not an exact accepted target"
            )
            continue
        if anchor.casefold() in accepted_casefold:
            errors.append(f"{label}.anchor must not itself be an accepted target")
            continue
        anchor_claims = _claim_longest_non_overlapping_occurrences(
            target,
            (("anchor", (anchor,)),),
            bounded_ascii=True,
        ).get("anchor", ())
        if len(anchor_claims) != 1:
            errors.append(
                f"{label}.anchor must occur exactly once as an exact "
                "phrase-boundary component of its target"
            )
            continue
        anchor_key = anchor.casefold()
        mapping_key = (anchor_key, target.casefold())
        anchor_keys.append(anchor_key)
        mapping_keys.append(mapping_key)
        mappings.append({"anchor": anchor, "target": target})
    if len(set(mapping_keys)) != len(mapping_keys):
        errors.append(f"{field} contains duplicate mappings")
    if len(set(anchor_keys)) != len(anchor_keys):
        errors.append(f"{field} contains conflicting duplicate anchors")
    repair_aliases = entry.get("repair_aliases", [])
    alias_keys = {
        alias.casefold()
        for alias in repair_aliases
        if isinstance(alias, str) and alias
    } if isinstance(repair_aliases, list) else set()
    if set(anchor_keys) & alias_keys:
        errors.append(f"{field} anchors must not overlap repair_aliases")
    if errors:
        return (), tuple(errors)
    return tuple(mappings), ()


def strict_repair_localization_anchor_mappings(entry):
    """Return validated mappings, or nothing when any declaration is unsafe."""
    mappings, errors = _parse_repair_localization_anchors(entry)
    return () if errors else mappings


def glossary_entries_to_text(
    entries, high_priority_categories=(), strict_locked_categories=(), *, chapter_number=None
):
    """Format selected glossary entries exactly as production supplies them."""
    if not entries:
        return "No established glossary entries apply."
    strict_source_terms = {
        source_term
        for source_term, _ in effective_strict_glossary_entry_items(
            entries,
            high_priority_categories,
            strict_locked_categories,
            chapter_number=chapter_number,
        )
    }
    lines = []
    for entry in entries:
        status = entry.get("status", "provisional")
        category = normalize_category(entry.get("category", ""))
        chinese = entry["chinese"]
        if status == "contextual":
            approved = entry.get("approved_renderings", [])
            lines.append(chinese)
            lines.append("  Status: CONTEXTUAL")
            if approved:
                lines.append("  Approved renderings: " + "; ".join(approved))
            if entry.get("notes"):
                lines.append("  Notes: " + entry["notes"])
        else:
            lines.append(f"{chinese} → {entry.get('english', '')}")
            lines.append(f"  Status: {status.upper()}")
            if entry.get("alias_of"):
                lines.append("  Alias of: " + entry["alias_of"])
            if entry.get("category"):
                lines.append("  Category: " + entry["category"])
            if entry.get("notes"):
                lines.append("  Notes: " + entry["notes"])
            if chinese in strict_source_terms:
                canonical = (entry.get("english", "") or "").strip()
                accepted = [
                    item for item in strict_term_valid_targets(entry)
                    if item.casefold() != canonical.casefold()
                ]
                if accepted:
                    lines.append(
                        "  Accepted final grammatical forms: " + "; ".join(accepted)
                    )
                    lines.append(
                        "  Enforcement: MANDATORY CANONICAL FAMILY; use the exact "
                        "English above or one listed form when grammar requires it."
                    )
                else:
                    lines.append(
                        "  Enforcement: MANDATORY CANONICAL RENDERING; use the exact "
                        "English above for this named term."
                    )
    return "\n".join(lines)


def _source_metalinguistic_glyph_occurrence(
    source_text, fragment, source_language, claimed_source_spans
):
    """Return one unclaimed, explicitly metalinguistic source occurrence."""
    language = (source_language or "zh").strip().lower()
    before_pattern, after_pattern = (
        (KOREAN_SOURCE_GLYPH_BEFORE_RE, KOREAN_SOURCE_GLYPH_AFTER_RE)
        if language == "ko" or language.startswith("ko-")
        else (CHINESE_SOURCE_GLYPH_BEFORE_RE, CHINESE_SOURCE_GLYPH_AFTER_RE)
    )
    search_from = 0
    while True:
        start = source_text.find(fragment, search_from)
        if start < 0:
            return None
        end = start + len(fragment)
        search_from = end
        if any(start < claimed_end and claimed_start < end
               for claimed_start, claimed_end in claimed_source_spans):
            continue
        before = source_text[max(0, start - 20):start]
        after = source_text[end:min(len(source_text), end + 20)]
        if before_pattern.search(before) or after_pattern.search(after):
            claimed_source_spans.append((start, end))
            return {"start": start, "end": end}


def _script_occurrence(translation, match, line_starts, original_lines):
    line_index = bisect_right(line_starts, match.start()) - 1
    line_start = line_starts[line_index]
    line_text = original_lines[line_index] if line_index < len(original_lines) else ""
    return {
        "fragment": match.group(0),
        "start": match.start(),
        "end": match.end(),
        "line_number": line_index + 1,
        "line_start": line_start,
        "line_end": line_start + len(line_text),
        "line_text": line_text,
    }


def _residual_script_report(
    translation,
    pattern,
    approved_reader_facing_terms=(),
    script="CJK",
    source_text=None,
    source_language="zh",
):
    """Report unexpected CJK fragments and exact target-text locations.

    Approved reader-facing terms are replaced by same-length spaces, so all
    offsets continue to refer to the original translation.  The original
    count/fragments fields retain their established commit-QA semantics.
    """
    original = translation or ""
    inspected = original
    line_starts = [0]
    for match in re.finditer(r"\n", inspected):
        line_starts.append(match.end())
    original_lines = original.splitlines() or [""]
    raw_occurrences = [
        _script_occurrence(original, match, line_starts, original_lines)
        for match in pattern.finditer(original)
    ]
    approved = sorted(
        {
            term for term in approved_reader_facing_terms
            if isinstance(term, str) and term
        },
        key=len,
        reverse=True,
    )
    for term in approved:
        inspected = inspected.replace(term, " " * len(term))

    intentional_occurrences = []
    claimed_source_spans = []
    for match in list(pattern.finditer(inspected)):
        fragment = match.group(0)
        if len(fragment) != 1:
            continue
        line_start = inspected.rfind("\n", 0, match.start()) + 1
        cue = METALINGUISTIC_TARGET_CUE_RE.search(
            inspected[max(line_start, match.start() - 96):match.start()]
        )
        if cue is None:
            continue
        source_evidence = None
        if source_text is not None:
            source_evidence = _source_metalinguistic_glyph_occurrence(
                source_text or "",
                fragment,
                source_language,
                claimed_source_spans,
            )
            if source_evidence is None:
                continue
        occurrence = _script_occurrence(
            original, match, line_starts, original_lines
        )
        occurrence.update({
            "evidence": "explicit_metalinguistic_target_cue",
            "source_supported": source_text is not None,
        })
        if source_evidence is not None:
            occurrence["source_start"] = source_evidence["start"]
            occurrence["source_end"] = source_evidence["end"]
        intentional_occurrences.append(occurrence)
        inspected = (
            inspected[:match.start()]
            + " " * (match.end() - match.start())
            + inspected[match.end():]
        )

    occurrences = []
    for match in pattern.finditer(inspected):
        occurrences.append(
            _script_occurrence(original, match, line_starts, original_lines)
        )
    fragments = [item["fragment"] for item in occurrences]
    affected_line_numbers = list(dict.fromkeys(
        item["line_number"] for item in occurrences
    ))
    raw_fragments = [item["fragment"] for item in raw_occurrences]
    intentional_fragments = [
        item["fragment"] for item in intentional_occurrences
    ]
    blocking_count = sum(len(item) for item in fragments)
    return {
        "raw_source_script_character_count": sum(len(item) for item in raw_fragments),
        "raw_source_script_fragments": raw_fragments,
        "raw_source_script_occurrences": raw_occurrences,
        "intentional_reader_facing_character_count": sum(
            len(item) for item in intentional_fragments
        ),
        "intentional_reader_facing_fragments": intentional_fragments,
        "intentional_reader_facing_occurrences": intentional_occurrences,
        "blocking_source_script_character_count": blocking_count,
        "blocking_source_script_fragments": fragments,
        "blocking_source_script_occurrences": occurrences,
        "remaining_cjk_character_count": blocking_count,
        "residual_cjk_fragments": fragments,
        "residual_cjk_occurrences": occurrences,
        "affected_line_numbers": affected_line_numbers,
        "source_script": script,
    }


def residual_cjk_report(
    translation, approved_reader_facing_terms=(), source_text=None
):
    """Preserve the established Chinese/Han residual-report contract."""
    return _residual_script_report(
        translation,
        CJK_FRAGMENT_RE,
        approved_reader_facing_terms,
        script="CJK",
        source_text=source_text,
        source_language="zh",
    )


def residual_source_script_report(
    translation, source_language, approved_reader_facing_terms=(), source_text=None
):
    """Report untranslated source script using the configured source language.

    The legacy ``remaining_cjk_*`` field names are retained so existing QA,
    observability, and recovery metadata remain backward compatible.
    """
    language = (source_language or "zh").strip().lower()
    if language == "ko" or language.startswith("ko-"):
        return _residual_script_report(
            translation,
            KOREAN_SOURCE_FRAGMENT_RE,
            approved_reader_facing_terms,
            script="Korean",
            source_text=source_text,
            source_language=language,
        )
    return residual_cjk_report(
        translation, approved_reader_facing_terms, source_text=source_text
    )


def stable_text_hash(text):
    """Return the stable UTF-8 identity used for a paid pending version."""
    return sha256((text or "").encode("utf-8")).hexdigest()


def _claim_longest_non_overlapping_occurrences(
    text, phrases_by_owner, bounded_ascii=False, owner_capacities=None
):
    """Claim exact phrase spans longest-first and return them by owner.

    This keeps a base term from claiming characters or words already owned by
    a longer compound (for example, 閻浮 inside 閻浮點數, or Yanfu inside
    Yanfu Points). Equal spans are deterministically owned by the longer
    phrase and then the stable owner key, making glossary order irrelevant.
    When source-derived owner capacities are supplied, an owner cannot claim
    more target spans than it has source anchors. This lets glossary source
    aliases share one canonical English target without an absent alias
    stealing every exact target occurrence from the source form that appears.
    """
    candidates = []
    for owner, phrases in phrases_by_owner:
        for phrase in dict.fromkeys(item for item in phrases if item):
            start = 0
            while True:
                position = (text or "").find(phrase, start)
                if position < 0:
                    break
                end = position + len(phrase)
                if bounded_ascii:
                    before = (text or "")[position - 1] if position else ""
                    after = (text or "")[end] if end < len(text or "") else ""
                    if (
                        phrase[0].isalnum() and before.isascii() and before.isalnum()
                    ) or (
                        phrase[-1].isalnum() and after.isascii() and after.isalnum()
                    ):
                        start = position + 1
                        continue
                candidates.append({
                    "owner": owner,
                    "phrase": phrase,
                    "start": position,
                    "end": end,
                })
                start = position + 1
    capacities = owner_capacities if isinstance(owner_capacities, dict) else None
    claimed_counts = {}
    claimed = []
    for item in sorted(
        candidates,
        key=lambda value: (
            -(value["end"] - value["start"]),
            value["start"],
            value["owner"],
            value["phrase"],
        ),
    ):
        if capacities is not None and claimed_counts.get(item["owner"], 0) >= max(
            0, capacities.get(item["owner"], 0)
        ):
            continue
        if any(
            item["start"] < other["end"] and other["start"] < item["end"]
            for other in claimed
        ):
            continue
        claimed.append(item)
        claimed_counts[item["owner"]] = claimed_counts.get(item["owner"], 0) + 1
    grouped = {}
    for item in sorted(claimed, key=lambda value: (value["start"], value["end"])):
        grouped.setdefault(item["owner"], []).append(item)
    return grouped


def qa_paragraph_segment_spans(text):
    """Return conservative blank-line QA segments with original-text offsets.

    TXT chapter bodies retain a source heading followed by an equals-sign
    divider, while reader-facing Markdown uses one heading block. Folding only
    that exact leading structural pair lets their narrative paragraphs align
    without independently guessing sentence boundaries. Offsets always refer
    to the original text, including when it uses CRLF line endings.
    """
    original = text or ""
    separator = re.compile(r"(?:\r\n|\r|\n)[ \t]*(?:(?:\r\n|\r|\n))+")
    raw_spans = []
    cursor = 0
    for match in separator.finditer(original):
        raw_spans.append((cursor, match.start()))
        cursor = match.end()
    raw_spans.append((cursor, len(original)))

    segments = []
    for raw_start, raw_end in raw_spans:
        start = raw_start
        end = raw_end
        while start < end and original[start].isspace():
            start += 1
        while end > start and original[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        segment_text = original[start:end].replace("\r\n", "\n").replace("\r", "\n")
        segments.append({"start": start, "end": end, "text": segment_text})

    if len(segments) >= 2 and re.fullmatch(r"={3,}", segments[1]["text"]):
        first = segments[0]
        divider = segments[1]
        segments = [{
            "start": first["start"],
            "end": divider["end"],
            "text": first["text"] + "\n\n" + divider["text"],
        }, *segments[2:]]
    return tuple(segments)


def _strict_qa_paragraph_segments(text):
    """Return the text of conservative blank-line QA segments."""
    return tuple(item["text"] for item in qa_paragraph_segment_spans(text))


def _segment_has_conflicting_strict_alias(segment, entry):
    """Return whether a known noncanonical alias occurs outside valid targets."""
    owner = "valid"
    valid_claims = _claim_longest_non_overlapping_occurrences(
        segment or "",
        [(owner, strict_term_valid_targets(entry))],
        bounded_ascii=True,
    ).get(owner, [])
    valid_spans = [(item["start"], item["end"]) for item in valid_claims]
    for alias, _origin in strict_term_repair_aliases(entry):
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(segment or ""):
            span = (match.start(), match.end())
            if not any(
                span[0] < valid_end and valid_start < span[1]
                for valid_start, valid_end in valid_spans
            ):
                return True
    return False


def _strict_segment_coverage(
    chinese_source, translation, source_phrases, target_phrases, details
):
    """Return source-local strict coverage when paragraph structure aligns exactly."""
    source_segments = _strict_qa_paragraph_segments(chinese_source)
    target_segments = _strict_qa_paragraph_segments(translation)
    if not source_segments or len(source_segments) != len(target_segments):
        return None
    coverage = {
        chinese_term: {
            "source_segment_count": 0,
            "represented_source_segment_count": 0,
            "same_segment_repetition_credit": 0,
            "effective_accepted_target_count": 0,
        }
        for chinese_term in details
    }
    for source_segment, target_segment in zip(source_segments, target_segments):
        source_claims = _claim_longest_non_overlapping_occurrences(
            source_segment, source_phrases
        )
        target_claims = _claim_longest_non_overlapping_occurrences(
            target_segment,
            target_phrases,
            bounded_ascii=True,
            owner_capacities={
                chinese_term: len(source_claims.get(chinese_term, []))
                for chinese_term in details
            },
        )
        for chinese_term, detail in details.items():
            source_count = len(source_claims.get(chinese_term, []))
            if not source_count:
                continue
            target_count = len(target_claims.get(chinese_term, []))
            item = coverage[chinese_term]
            item["source_segment_count"] += 1
            if target_count:
                item["represented_source_segment_count"] += 1
            effective_count = min(source_count, target_count)
            if (
                source_count > 1
                and target_count > 0
                and not _segment_has_conflicting_strict_alias(
                    target_segment, detail["entry"]
                )
            ):
                effective_count = source_count
                item["same_segment_repetition_credit"] += max(
                    0, source_count - target_count
                )
            item["effective_accepted_target_count"] += effective_count
    return coverage


def _claim_conflicting_strict_alias_occurrences(
    translation, details, source_claims, target_claims
):
    """Claim known bad aliases longest-first for source-present strict entries."""
    protected_spans = [
        (item["start"], item["end"])
        for items in target_claims.values()
        for item in items
    ]
    candidates = []
    for chinese_term, detail in details.items():
        if not source_claims.get(chinese_term):
            continue
        for alias, origin in strict_term_repair_aliases(detail["entry"]):
            pattern = re.compile(
                r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
                flags=re.IGNORECASE,
            )
            for match in pattern.finditer(translation or ""):
                span = (match.start(), match.end())
                if any(
                    span[0] < protected_end and protected_start < span[1]
                    for protected_start, protected_end in protected_spans
                ):
                    continue
                candidates.append({
                    "owner": chinese_term,
                    "phrase": match.group(0),
                    "configured_alias": alias,
                    "alias_origin": origin,
                    "start": span[0],
                    "end": span[1],
                })

    claimed = []
    for item in sorted(
        candidates,
        key=lambda value: (
            -(value["end"] - value["start"]),
            value["start"],
            value["owner"],
            value["configured_alias"],
        ),
    ):
        if any(
            item["start"] < other["end"] and other["start"] < item["end"]
            for other in claimed
        ):
            continue
        claimed.append(item)
    grouped = {}
    for item in sorted(claimed, key=lambda value: (value["start"], value["end"])):
        grouped.setdefault(item["owner"], []).append(item)
    return grouped


def _strict_detail_missing_count(detail):
    """Return the canonical QA deficit under lexical or referential semantics."""
    if not detail["source_anchor_count"]:
        return 0
    if detail["category"] in REFERENTIAL_STRICT_CATEGORIES:
        conflicting = detail.get("conflicting_alias_count", 0)
        if conflicting:
            return conflicting
        return 0 if detail["accepted_target_count"] else 1
    return max(
        0,
        detail["source_anchor_count"]
        - detail["effective_accepted_target_count"],
    )


def declared_nested_strict_source_owner_items(
    entries,
    strict_items=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
):
    """Return explicitly configured longer semantic owners of strict substrings.

    A glossary entry with ``owns_nested_strict_source_terms=true`` declares that
    its complete source form is one semantic unit.  Exact occurrences of that
    longer form therefore own any nested strict source strings, just as a longer
    effective-strict term already does.  The declaration is intentionally
    explicit: provisional entries and coincidental longer glossary keys never
    suppress locked terminology QA merely by existing.
    """
    if not isinstance(entries, dict):
        return ()
    strict_items = tuple(strict_items)
    strict_sources = {source_term for source_term, _entry in strict_items}
    strict = {normalize_category(item) for item in strict_categories}
    owners = []
    for source_term, entry in entries.items():
        if not (
            isinstance(source_term, str)
            and source_term
            and source_term not in strict_sources
            and isinstance(entry, dict)
            and entry.get(NESTED_STRICT_SOURCE_OWNER_FIELD) is True
        ):
            continue
        approved = entry.get("approved_renderings", [])
        if not isinstance(approved, (list, tuple)):
            approved = []
        configured_targets = (
            (entry.get("english", "") or "").strip(), *approved
        )
        if not any(
            isinstance(item, str) and item.strip() for item in configured_targets
        ):
            continue
        contains_strict_source = any(
            source_form != source_term and source_form in source_term
            for strict_source, strict_entry in strict_items
            for source_form in _glossary_source_forms(
                strict_source,
                normalize_category(strict_entry.get("category", "")),
                variant_categories=strict,
            )
        )
        if not contains_strict_source:
            continue
        owner_id = "\0nested-strict-source-owner:" + source_term
        owners.append((owner_id, (source_term,), source_term))
    return tuple(sorted(owners, key=lambda item: item[2]))


def _strict_occurrence_inventory(
    chinese_source,
    translation,
    strict_items,
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
    glossary_entries=None,
):
    strict = {normalize_category(item) for item in strict_categories}
    source_phrases = []
    target_phrases = []
    details = {}
    for chinese_term, entry in strict_items:
        category = normalize_category(entry.get("category", ""))
        source_forms = _glossary_source_forms(
            chinese_term, category, variant_categories=strict
        )
        canonical = (entry.get("english", "") or "").strip()
        accepted_inflections = tuple(
            item for item in entry.get("accepted_inflections", [])
            if isinstance(item, str) and item
        )
        automatic_inflections = tuple(
            item for item in automatic_strict_term_inflections(entry)
            if item.casefold() not in {
                accepted.casefold() for accepted in accepted_inflections
            }
        )
        details[chinese_term] = {
            "entry": entry,
            "category": category,
            "source_forms": source_forms,
            "canonical": canonical,
            "accepted_inflections": accepted_inflections,
            "automatic_inflections": automatic_inflections,
        }
        source_phrases.append((chinese_term, source_forms))
        accepted_targets = strict_term_valid_targets(entry)
        if accepted_targets:
            target_phrases.append((chinese_term, accepted_targets))
    declared_owners = declared_nested_strict_source_owner_items(
        glossary_entries, strict_items, strict_categories
    )
    declared_owner_terms = {
        owner_id: source_term
        for owner_id, _source_forms, source_term in declared_owners
    }
    source_phrases.extend(
        (owner_id, source_forms)
        for owner_id, source_forms, _source_term in declared_owners
    )
    source_claims = _claim_longest_non_overlapping_occurrences(
        chinese_source or "", source_phrases
    )
    target_claims = _claim_longest_non_overlapping_occurrences(
        translation or "",
        target_phrases,
        bounded_ascii=True,
        owner_capacities={
            chinese_term: len(source_claims.get(chinese_term, []))
            for chinese_term in details
        },
    )
    conflicting_alias_claims = _claim_conflicting_strict_alias_occurrences(
        translation or "", details, source_claims, target_claims
    )
    segment_coverage = _strict_segment_coverage(
        chinese_source or "",
        translation or "",
        source_phrases,
        target_phrases,
        details,
    )
    for chinese_term, detail in details.items():
        source_items = source_claims.get(chinese_term, [])
        raw_source_items = _claim_longest_non_overlapping_occurrences(
            chinese_source or "", [(chinese_term, detail["source_forms"])]
        ).get(chinese_term, [])
        claimed_source_items = [
            item for items in source_claims.values() for item in items
        ]
        excluded_source_items = []
        final_spans = {(item["start"], item["end"]) for item in source_items}
        for raw_item in raw_source_items:
            raw_span = (raw_item["start"], raw_item["end"])
            if raw_span in final_spans:
                continue
            owner = next((
                item for item in claimed_source_items
                if item["owner"] != chinese_term
                and item["start"] <= raw_item["start"]
                and item["end"] >= raw_item["end"]
            ), None)
            if owner is None:
                continue
            declared_owner_term = declared_owner_terms.get(owner["owner"])
            excluded_source_items.append({
                **raw_item,
                "owner": declared_owner_term or owner["owner"],
                "owner_phrase": owner["phrase"],
                "owner_kind": (
                    "declared_nested_source_owner"
                    if declared_owner_term else "effective_strict_term"
                ),
            })
        target_items = target_claims.get(chinese_term, [])
        canonical = detail["canonical"]
        accepted_inflections = set(detail["accepted_inflections"])
        automatic_inflections = set(detail["automatic_inflections"])
        exact_canonical_count = sum(
            item["phrase"] == canonical for item in target_items
        )
        accepted_inflection_count = sum(
            item["phrase"] in accepted_inflections for item in target_items
        )
        automatic_inflection_count = sum(
            item["phrase"] in automatic_inflections for item in target_items
        )
        conflicting_alias_items = conflicting_alias_claims.get(chinese_term, [])
        coverage = (
            segment_coverage.get(chinese_term)
            if segment_coverage is not None
            else None
        )
        effective_target_count = (
            coverage["effective_accepted_target_count"]
            if coverage is not None else len(target_items)
        )
        # Blank-line segment counts can coincide even when a harmless paragraph
        # split shifts one source/target pair positionally. Segment coverage is
        # needed to prevent one rendering from satisfying anchors in multiple
        # source segments, but it must not erase a uniquely owned chapter-wide
        # canonical-family match for a term that occurs exactly once.
        if coverage is not None and len(source_items) == 1 and target_items:
            effective_target_count = max(effective_target_count, 1)
        detail.update({
            "source_occurrences": source_items,
            "excluded_source_occurrences": excluded_source_items,
            "excluded_source_occurrence_count": len(excluded_source_items),
            "target_occurrences": target_items,
            "matched_source_forms": list(dict.fromkeys(
                item["phrase"] for item in source_items
            )),
            "source_anchor_count": len(source_items),
            "canonical_target_count": exact_canonical_count,
            "accepted_inflection_target_count": accepted_inflection_count,
            "automatic_inflection_target_count": automatic_inflection_count,
            "accepted_target_count": len(target_items),
            "segment_alignment_available": coverage is not None,
            "source_segment_count": (
                coverage["source_segment_count"] if coverage is not None else None
            ),
            "represented_source_segment_count": (
                coverage["represented_source_segment_count"]
                if coverage is not None else None
            ),
            "same_segment_repetition_credit": (
                coverage["same_segment_repetition_credit"]
                if coverage is not None else 0
            ),
            "effective_accepted_target_count": effective_target_count,
            "strict_qa_mode": (
                "referential_identity"
                if detail["category"] in REFERENTIAL_STRICT_CATEGORIES
                else "lexical_occurrence"
            ),
            "required_target_count": (
                1
                if source_items
                and detail["category"] in REFERENTIAL_STRICT_CATEGORIES
                else len(source_items)
            ),
            "conflicting_alias_occurrences": conflicting_alias_items,
            "conflicting_alias_count": len(conflicting_alias_items),
            "matched_conflicting_aliases": list(dict.fromkeys(
                item["phrase"] for item in conflicting_alias_items
            )),
        })
    return details


def locked_high_priority_term_violations(
    chinese_source,
    translation,
    glossary_entries,
    high_priority_categories,
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
    *, chapter_number=None,
):
    """Enforce lexical coverage or referential identity for strict entries."""
    strict_items = effective_strict_glossary_entry_items(
        glossary_entries, high_priority_categories, strict_categories, chapter_number=chapter_number
    )
    inventory = _strict_occurrence_inventory(
        chinese_source,
        translation,
        strict_items,
        strict_categories,
        glossary_entries=glossary_entries,
    )
    violations = []
    for chinese_term, entry in strict_items:
        detail = inventory[chinese_term]
        category = detail["category"]
        required_english = detail["canonical"]
        if not required_english:
            continue
        source_anchor_count = detail["source_anchor_count"]
        canonical_target_count = detail["canonical_target_count"]
        accepted_inflection_count = detail["accepted_inflection_target_count"]
        automatic_inflection_count = detail["automatic_inflection_target_count"]
        accepted_target_count = detail["accepted_target_count"]
        missing_required_count = _strict_detail_missing_count(detail)
        if not source_anchor_count or not missing_required_count:
            continue
        violation = {
            "chinese": chinese_term,
            "matched_source_forms": detail["matched_source_forms"],
            "required_english": required_english,
            "category": category,
            "source_anchor_count": source_anchor_count,
            "canonical_target_count": canonical_target_count,
            "missing_canonical_count": missing_required_count,
        }
        if category in REFERENTIAL_STRICT_CATEGORIES:
            violation.update({
                "strict_qa_mode": "referential_identity",
                "required_target_count": detail["required_target_count"],
                "conflicting_alias_count": detail["conflicting_alias_count"],
                "matched_conflicting_aliases": detail[
                    "matched_conflicting_aliases"
                ],
            })
        if detail["accepted_inflections"] or detail["automatic_inflections"]:
            violation.update({
                "accepted_inflection_target_count": accepted_inflection_count,
                "automatic_inflection_target_count": automatic_inflection_count,
                "accepted_target_count": accepted_target_count,
            })
        violations.append(violation)
    return violations


def canonicalize_locked_high_priority_terms(
    chinese_source,
    translation,
    glossary_entries,
    high_priority_categories,
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
    *, chapter_number=None,
):
    """Repair only source-anchored surface variants of strict locked terms.

    A candidate must contain the exact canonical English word sequence. Only
    case and separators made from whitespace, commas, or hyphens may differ.
    This intentionally excludes edit distance, synonyms, reordered words, and
    all other semantic guesses.
    """
    source = chinese_source or ""
    repaired_text = translation or ""
    strict = {normalize_category(item) for item in strict_categories}
    strict_items = repair_eligible_strict_glossary_entry_items(
        glossary_entries, high_priority_categories, strict_categories, chapter_number=chapter_number
    )
    inventory = _strict_occurrence_inventory(
        source,
        repaired_text,
        strict_items,
        strict_categories,
        glossary_entries=glossary_entries,
    )
    eligible = []
    for chinese_term, entry in strict_items:
        detail = inventory[chinese_term]
        category = normalize_category(entry.get("category", ""))
        canonical = (entry.get("english", "") or "").strip()
        missing = max(
            0,
            detail["source_anchor_count"]
            - detail["effective_accepted_target_count"],
        )
        if not canonical or not missing:
            continue
        matched_forms = detail["matched_source_forms"]
        if not matched_forms:
            continue
        words = CANONICAL_TERM_WORD_RE.findall(canonical)
        # A one-word case correction is too broad for automatic prose editing.
        if len(words) < 2:
            continue
        pattern = re.compile(
            r"(?<![A-Za-z0-9])"
            + SAFE_CANONICAL_SEPARATOR_RE.join(re.escape(word) for word in words)
            + r"(?![A-Za-z0-9])",
            flags=re.IGNORECASE,
        )
        eligible.append((
            max(len(item) for item in matched_forms), chinese_term, entry,
            canonical, matched_forms, pattern, missing,
        ))

    repairs = []
    for _, chinese_term, entry, canonical, matched_forms, pattern, missing in sorted(
        eligible, key=lambda item: (-item[0], item[1])
    ):
        matches = list(pattern.finditer(repaired_text))
        changed_variants = [match.group(0) for match in matches if match.group(0) != canonical]
        if not changed_variants or len(changed_variants) != missing:
            continue
        repaired_text = pattern.sub(canonical, repaired_text)
        seen = []
        for old_variant in changed_variants:
            if old_variant in seen:
                continue
            seen.append(old_variant)
            repairs.append({
                "chinese_term": chinese_term,
                "matched_source_forms": list(matched_forms),
                "old_variant": old_variant,
                "canonical_english": canonical,
                "category": normalize_category(entry.get("category", "")),
                "replacement_count": changed_variants.count(old_variant),
                "repair_type": "deterministic_locked_term_canonicalization",
                "api_cost": 0,
            })

    unresolved = locked_high_priority_term_violations(
        source,
        repaired_text,
        glossary_entries,
        high_priority_categories,
        strict_categories,
        chapter_number=chapter_number,
    )
    return {
        "repaired_text": repaired_text,
        "repairs_applied": repairs,
        "unresolved_strict_terms": unresolved,
    }


DUAL_FORM_TARGET_RE = re.compile(r"^(.+?)\s+\(([^()\r\n]+)\)$")


def strict_term_possessive_normalizations(canonical):
    """Return exact component-to-phrase possessives for one dual-form term.

    This deliberately models only ``A (B)`` -> ``A (B)'s`` morphology.  Both
    components and the apostrophe style must match the canonical term exactly;
    nested parentheses, plural possessives, and mixed apostrophe styles are not
    inferred.
    """
    canonical = (canonical or "").strip()
    match = DUAL_FORM_TARGET_RE.fullmatch(canonical)
    if not match:
        return ()
    outer, inner = (part.strip() for part in match.groups())
    if not outer or not inner:
        return ()
    normalizations = []
    for apostrophe in ("'", "\u2019"):
        normalized = f"{canonical}{apostrophe}s"
        normalizations.extend((
            (f"{outer}{apostrophe}s ({inner}{apostrophe}s)", normalized),
            (f"{outer}{apostrophe}s ({inner})", normalized),
            (f"{outer} ({inner}{apostrophe}s)", normalized),
        ))
    return tuple(normalizations)


def _strict_term_span_has_unambiguous_boundaries(text, start, end):
    """Prove an exact morphology span is not embedded in a larger word."""
    return not (
        (start and text[start - 1].isalnum())
        or (end < len(text) and text[end].isalnum())
    )


def _strict_term_component_is_in_unproven_possessive_context(
    text, start, end, canonical
):
    """Keep component aliases from expanding malformed dual-form phrases.

    Exact proven morphology is collected before ordinary aliases. If the full
    dual form is not proven, a canonical component adjacent to possessive and
    parenthetical punctuation is reserved rather than expanded on its own.
    """
    match = DUAL_FORM_TARGET_RE.fullmatch((canonical or "").strip())
    if not match:
        return False
    component = text[start:end].casefold()
    components = {part.strip().casefold() for part in match.groups()}
    if component not in components:
        return False
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    before = text[line_start:start]
    after = text[end:line_end]
    if re.match(r"['\u2019]s[ \t]*\(", after):
        return True
    if re.search(r"['\u2019]s[ \t]*\([^\)\r\n]*$", before):
        return True
    if re.match(r"['\u2019]s[ \t]*\)", after) and "(" in before:
        return True
    return False


def is_exact_strict_term_possessive_normalization(
    old_text, new_text, canonical_targets
):
    """Prove that a patch only relocates morphology, preserving surrounding bytes."""
    for canonical in canonical_targets:
        for malformed, normalized in strict_term_possessive_normalizations(canonical):
            if old_text.count(malformed) != 1:
                continue
            start = old_text.index(malformed)
            end = start + len(malformed)
            if not _strict_term_span_has_unambiguous_boundaries(
                old_text, start, end
            ):
                continue
            if new_text == old_text[:start] + normalized + old_text[end:]:
                return True
    return False


def touches_strict_term_possessive_morphology(
    old_text, new_text, canonical_targets
):
    """Return whether a patch touches either side of a known possessive pair."""
    return any(
        malformed in old_text or normalized in new_text
        for canonical in canonical_targets
        for malformed, normalized in strict_term_possessive_normalizations(canonical)
    )


def strict_term_repair_aliases(entry):
    """Return approved aliases and conservative whole-phrase derivatives."""
    aliases = []
    for alias in entry.get("repair_aliases", []):
        aliases.append((alias, "explicit"))
    canonical = (entry.get("english", "") or "").strip()
    match = DUAL_FORM_TARGET_RE.fullmatch(canonical)
    if match:
        aliases.extend((part.strip(), "derived") for part in match.groups())
    words = canonical.split()
    if len(words) >= 2 and re.fullmatch(r"[A-Za-z]+", words[-1]):
        final = words[-1]
        if (
            final.casefold().endswith("s")
            and not final.casefold().endswith(("ss", "us", "is"))
            and len(final) > 3
        ):
            aliases.append((" ".join(words[:-1] + [final[:-1]]), "derived_inflection"))
        elif not final.casefold().endswith("s") and len(final) >= 4:
            aliases.append((" ".join(words[:-1] + [final + "s"]), "derived_inflection"))
    result = []
    seen = set()
    accepted = {item.casefold() for item in strict_term_valid_targets(entry)}
    for alias, origin in aliases:
        key = alias.casefold()
        if (
            not alias
            or key == canonical.casefold()
            or key in accepted
            or key in seen
        ):
            continue
        seen.add(key)
        result.append((alias, origin))
    return tuple(result)


def _bounded_phrase_pattern(phrase):
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )


def _exact_spans(text, phrase):
    spans = []
    start = 0
    while phrase:
        position = text.find(phrase, start)
        if position < 0:
            break
        spans.append((position, position + len(phrase)))
        start = position + len(phrase)
    return spans


def deterministic_strict_terminology_repair(
    chinese_source,
    translation,
    glossary_entries,
    high_priority_categories,
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
    approved_reader_facing_cjk_terms=(),
    *, chapter_number=None,
):
    """Apply only unambiguous source-counted variants for eligible strict terms."""
    source = chinese_source or ""
    original = translation or ""
    repaired = original
    repairs = []
    ambiguous = []

    strict_items = repair_eligible_strict_glossary_entry_items(
        glossary_entries, high_priority_categories, strict_categories, chapter_number=chapter_number
    )
    initial_inventory = _strict_occurrence_inventory(
        source,
        repaired,
        strict_items,
        strict_categories,
        glossary_entries=glossary_entries,
    )
    strict_items = sorted(
        strict_items,
        key=lambda item: (
            -max(
                (len(form) for form in initial_inventory[item[0]]["matched_source_forms"]),
                default=len(item[0]),
            ),
            item[0],
        ),
    )
    for chinese_term, entry in strict_items:
        canonical = (entry.get("english", "") or "").strip()
        category = normalize_category(entry.get("category", ""))
        inventory = _strict_occurrence_inventory(
            source,
            repaired,
            strict_items,
            strict_categories,
            glossary_entries=glossary_entries,
        )
        detail = inventory[chinese_term]
        matched_forms = detail["matched_source_forms"]
        source_count = detail["source_anchor_count"]
        canonical_before = detail["canonical_target_count"]
        accepted_before = detail["accepted_target_count"]
        missing = _strict_detail_missing_count(detail)
        if not missing:
            continue

        protected = []
        for accepted_target in strict_term_valid_targets(entry):
            protected.extend(_exact_spans(repaired, accepted_target))
        matches = []
        possessive_spans = []
        for malformed, normalized in strict_term_possessive_normalizations(canonical):
            for start, end in _exact_spans(repaired, malformed):
                if not _strict_term_span_has_unambiguous_boundaries(
                    repaired, start, end
                ):
                    continue
                span = (start, end)
                if any(
                    span[0] < protected_end and protected_start < span[1]
                    for protected_start, protected_end in protected
                ):
                    continue
                possessive_spans.append(span)
                matches.append({
                    "start": start,
                    "end": end,
                    "old_variant": malformed,
                    "configured_alias": malformed,
                    "alias_origin": "derived_possessive_morphology",
                    "replacement": normalized,
                })
        for alias, origin in strict_term_repair_aliases(entry):
            for match in _bounded_phrase_pattern(alias).finditer(repaired):
                span = (match.start(), match.end())
                if _strict_term_component_is_in_unproven_possessive_context(
                    repaired, span[0], span[1], canonical
                ):
                    continue
                if any(span[0] < end and start < span[1] for start, end in protected):
                    continue
                if any(
                    span[0] < end and start < span[1]
                    for start, end in possessive_spans
                ):
                    continue
                matches.append({
                    "start": span[0],
                    "end": span[1],
                    "old_variant": match.group(0),
                    "configured_alias": alias,
                    "alias_origin": origin,
                    "replacement": canonical,
                })

        matches.sort(key=lambda item: (
            item["start"],
            -(item["end"] - item["start"]),
            0 if item["alias_origin"] == "explicit" else 1,
        ))
        distinct = []
        overlap = False
        for item in matches:
            if distinct and item["start"] == distinct[-1]["start"] and item["end"] == distinct[-1]["end"]:
                continue
            if distinct and item["start"] < distinct[-1]["end"]:
                overlap = True
                break
            distinct.append(item)
        if overlap or len(distinct) != missing:
            ambiguous.append({
                "chinese_term": chinese_term,
                "canonical_english": canonical,
                "source_anchor_count": source_count,
                "canonical_target_count": canonical_before,
                "accepted_target_count": accepted_before,
                "missing_canonical_count": missing,
                "candidate_alias_count": len(distinct),
                "reason": (
                    "overlapping alias matches" if overlap else
                    "alias count does not uniquely match missing source occurrences"
                ),
            })
            continue

        candidate = repaired
        for item in reversed(distinct):
            candidate = (
                candidate[:item["start"]]
                + item["replacement"]
                + candidate[item["end"]:]
            )
        after_inventory = _strict_occurrence_inventory(
            source,
            candidate,
            strict_items,
            strict_categories,
            glossary_entries=glossary_entries,
        )
        canonical_after = after_inventory[chinese_term]["canonical_target_count"]
        accepted_after = after_inventory[chinese_term]["accepted_target_count"]
        missing_after = _strict_detail_missing_count(
            after_inventory[chinese_term]
        )
        before_cjk = residual_cjk_report(
            repaired, approved_reader_facing_cjk_terms
        )["remaining_cjk_character_count"]
        after_cjk = residual_cjk_report(
            candidate, approved_reader_facing_cjk_terms
        )["remaining_cjk_character_count"]
        if (
            missing_after >= missing
            or after_cjk > before_cjk
            or candidate.count("\n") != repaired.count("\n")
        ):
            ambiguous.append({
                "chinese_term": chinese_term,
                "canonical_english": canonical,
                "source_anchor_count": source_count,
                "canonical_target_count": canonical_before,
                "accepted_target_count": accepted_before,
                "missing_canonical_count": missing,
                "candidate_alias_count": len(distinct),
                "reason": "candidate failed deterministic safety validation",
            })
            continue

        repaired = candidate
        for item in distinct:
            repairs.append({
                "chinese_term": chinese_term,
                "matched_source_forms": list(matched_forms),
                "old_variant": item["old_variant"],
                "configured_alias": item["configured_alias"],
                "alias_origin": item["alias_origin"],
                "canonical_english": canonical,
                "applied_replacement": item["replacement"],
                "category": category,
                "replacement_count": 1,
                "source_anchor_count": source_count,
                "canonical_count_before": canonical_before,
                "canonical_count_after": canonical_after,
                "accepted_target_count_before": accepted_before,
                "accepted_target_count_after": accepted_after,
                "repair_type": "deterministic_strict_terminology_repair",
                "api_cost": 0,
            })

    unresolved = locked_high_priority_term_violations(
        source,
        repaired,
        glossary_entries,
        high_priority_categories,
        strict_categories,
        chapter_number=chapter_number,
    )
    return {
        "repaired_text": repaired,
        "repairs_applied": repairs,
        "ambiguous_terms": ambiguous,
        "unresolved_strict_terms": unresolved,
    }


def translation_commit_qa(
    chinese_source,
    translation,
    glossary_entries,
    high_priority_categories,
    approved_reader_facing_cjk_terms=(),
    strict_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
    source_language="zh",
    *, chapter_number=None,
):
    """Return local hard blockers that must pass before production state work."""
    residual = residual_source_script_report(
        translation,
        source_language,
        approved_reader_facing_cjk_terms,
        source_text=chinese_source,
    )
    terminology = locked_high_priority_term_violations(
        chinese_source,
        translation,
        glossary_entries,
        high_priority_categories,
        strict_categories,
        chapter_number=chapter_number,
    )
    blockers = []
    if residual["remaining_cjk_character_count"]:
        blockers.append({
            "type": (
                "residual_source_script"
                if residual["source_script"] != "CJK"
                else "residual_cjk"
            ),
            "count": residual["remaining_cjk_character_count"],
            "fragments": residual["residual_cjk_fragments"],
        })
    for violation in terminology:
        blockers.append({"type": "locked_terminology", **violation})
    return {
        **residual,
        "locked_high_priority_term_violations": terminology,
        "commit_allowed": not blockers,
        "commit_blockers": blockers,
    }


def pending_meta_after_commit_qa_failure(meta, qa):
    """Preserve paid translation accounting but invalidate downstream work."""
    reset = dict(meta)
    reset["local_qa"] = qa
    reset["stage"] = "translated"
    for stale_key in (
        "state", "state_stats", "state_checkpoint_identity",
        "eligible_candidates", "prefilter_rejected",
        "candidate_preparation",
        "review_result", "review_stats", "review_checkpoint_identity",
        "chapter_glossary_review_recovery",
    ):
        reset.pop(stale_key, None)
    return reset


def pending_meta_after_translation_repair(meta, repairs):
    """Invalidate state derived from pre-repair text while preserving paid stats."""
    return _pending_meta_after_text_change(
        meta, repairs, "deterministic_locked_term_repairs"
    )


def pending_meta_after_residual_cjk_repair(meta, repairs):
    """Invalidate derived work after an accepted contextual micro-repair."""
    return _pending_meta_after_text_change(
        meta, repairs, "residual_cjk_micro_repairs"
    )


def pending_meta_after_residual_parenthetical_cleanup(meta, repairs):
    """Invalidate derived work after zero-cost residual annotation cleanup."""
    return _pending_meta_after_text_change(
        meta, repairs, "deterministic_residual_parenthetical_cleanups"
    )


def pending_meta_after_strict_terminology_repair(meta, repairs):
    """Invalidate derived work after an accepted strict-terminology repair."""
    return _pending_meta_after_text_change(
        meta, repairs, "strict_terminology_repairs"
    )


def _pending_meta_after_text_change(meta, repairs, record_key):
    reset = dict(meta)
    reset["stage"] = "translated"
    reset.pop("local_qa", None)
    for stale_key in (
        "state", "state_stats", "state_checkpoint_identity",
        "eligible_candidates", "prefilter_rejected",
        "candidate_preparation",
        "review_result", "review_stats", "review_checkpoint_identity",
        "chapter_glossary_review_recovery",
    ):
        reset.pop(stale_key, None)
    recorded = list(reset.get(record_key, []))
    for repair in repairs:
        item = dict(repair)
        if item not in recorded:
            recorded.append(item)
    reset[record_key] = recorded
    return reset


def api_error_status_code(error):
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    return status


def is_retryable_api_error(error):
    """Classify status-bearing API failures without importing an API client."""
    message = str(error).lower()
    if any(marker in message for marker in PERMANENT_API_ERROR_MARKERS):
        return False
    return api_error_status_code(error) in RETRYABLE_HTTP_STATUS_CODES


def run_with_retries(
    operation, max_attempts, retry_delays, is_retryable, sleep,
    before_attempt=lambda: None, on_retry=lambda error, attempt, delay: None,
):
    """Run an operation with deterministic, injectable retry behavior."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        context = before_attempt()
        try:
            return operation(), context
        except Exception as error:
            if not is_retryable(error):
                raise
            delay = retry_delays[attempt - 1] if attempt < max_attempts else None
            on_retry(error, attempt, delay)
            if delay is None:
                raise
            sleep(delay)
    raise RuntimeError("retry loop ended unexpectedly")


def recovered_stats(stats, auto_off_peak_only, moment=None):
    """Normalize saved pending stats without charging or mutating progress."""
    if not stats:
        return {key: 0.0 if key.endswith("cost_usd") else 0 for key in ACCOUNTING_FIELDS}
    result = dict(stats)
    if result.get("pricing_version") == PRICING_VERSION and "rate_correct_cost_usd" in result:
        return result
    if auto_off_peak_only:
        period = "off_peak"
        rates = V4_PRO_PRICES[period]
        pricing_info = {
            "pricing_version": PRICING_VERSION,
            "pricing_period": period,
            "request_started_utc": "legacy-pending-repriced",
            "cache_hit_rate_per_million": rates["cache_hit"],
            "cache_miss_rate_per_million": rates["cache_miss"],
            "output_rate_per_million": rates["output"],
        }
    else:
        if moment is None:
            raise ValueError("moment is required to reprice legacy pending stats when off-peak-only is disabled")
        pricing_info = pricing_snapshot(moment)
        pricing_info["request_started_utc"] = "legacy-pending-repriced"
    cost, cache_hits, cache_misses = calculate_cost_from_token_counts(
        result.get("prompt_tokens", 0) or 0,
        result.get("completion_tokens", 0) or 0,
        result.get("cache_hit_tokens", 0) or 0,
        result.get("cache_miss_tokens", 0) or 0,
        pricing_info,
    )
    result.update(pricing_info)
    result.update({
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "repriced_from_legacy_pending": True,
    })
    return result


def commit_chapter_accounting(progress, chapter_number, stats):
    """Advance progress and account one chapter together, at most once."""
    completed = progress.setdefault("completed", [])
    if chapter_number in completed:
        return False
    add_accounting_totals(progress, stats)
    completed.append(chapter_number)
    completed.sort()
    progress["last_completed"] = chapter_number
    progress["next_chapter"] = chapter_number + 1
    failed = progress.setdefault("failed", [])
    if chapter_number in failed:
        failed.remove(chapter_number)
    return True


def add_accounting_totals(progress, stats):
    """Add one API operation's totals without advancing chapter progress."""
    from production_accounting import bind_progress_accounting
    bind_progress_accounting(progress, stats)
    field_to_total = {
        "prompt_tokens": "total_prompt_tokens",
        "completion_tokens": "total_completion_tokens",
        "cache_hit_tokens": "total_cache_hit_tokens",
        "cache_miss_tokens": "total_cache_miss_tokens",
        "cost_usd": "estimated_cost_usd",
        "rate_correct_cost_usd": "rate_correct_cost_usd",
    }
    for source, destination in field_to_total.items():
        progress[destination] = (progress.get(destination, 0) or 0) + (stats.get(source, 0) or 0)
    return progress


def add_accounting_totals_once(progress, operation_id, stats):
    """Account one paid auxiliary operation exactly once by stable ID."""
    from production_accounting import validate_progress_accounting
    validate_progress_accounting(progress)
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    recorded = progress.setdefault("accounted_api_operation_ids", [])
    if operation_id in recorded:
        return False
    add_accounting_totals(progress, stats)
    recorded.append(operation_id)
    return True


def _unique_sibling_temp_descriptor(path):
    """Create one exclusive temp file beside the target using normal file mode."""
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "nt":
        flags |= os.O_BINARY
    for _attempt in range(128):
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temp, flags, 0o666)
        except FileExistsError:
            continue
        return descriptor, temp
    raise FileExistsError(f"could not allocate unique sibling temp for {path}")


def create_unique_sibling_temp(path):
    """Return one created unique binary sibling temp path owned by the caller."""
    descriptor, temp = _unique_sibling_temp_descriptor(path)
    os.close(descriptor)
    return temp


def atomic_replace_prepared_file(temp_path, path):
    """Fsync one completed sibling file, then atomically replace the target."""
    temp_path = Path(temp_path)
    path = Path(path)
    if temp_path.parent.resolve() != path.parent.resolve():
        raise ValueError("prepared atomic replacement must use the target directory")
    with temp_path.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _replace_with_retry(temp, path, attempts=6, delay_seconds=0.5):
    """Antivirus and indexers can briefly lock a file on Windows."""
    for attempt in range(attempts):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


def atomic_write_text(path, text, encoding="utf-8"):
    """Fsync a unique sibling temp file, then atomically replace the target."""
    path = Path(path)
    descriptor, temp = _unique_sibling_temp_descriptor(path)
    replaced = False
    try:
        stream = os.fdopen(descriptor, "w", encoding=encoding)
        descriptor = None
        with stream as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
        replaced = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            temp.unlink(missing_ok=True)


def atomic_write_bytes(path, content):
    """Binary counterpart to atomic_write_text."""
    path = Path(path)
    descriptor, temp = _unique_sibling_temp_descriptor(path)
    replaced = False
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
        replaced = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            temp.unlink(missing_ok=True)


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def clean_json_text(content):
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def salvage_state_json_without_issues(content):
    issues_match = re.search(r'"issues"\s*:', content)
    if not issues_match:
        return None
    prefix = content[:issues_match.start()].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    try:
        result = json.loads(prefix + '\n  "issues": []\n}')
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    if "new_terms" not in result or "continuity_summary" not in result:
        return None
    return result


def prefilter_candidates(
    new_terms,
    existing_terms=(),
    low_value_terms=(),
    allowed_categories=None,
    low_value_category_words=None,
    long_phrase_categories=None,
):
    """Filter candidates using defaults or one immutable profile policy."""
    existing_terms = set(existing_terms)
    low_value_terms = set(low_value_terms)
    allowed_categories = set(allowed_categories or ALLOWED_GLOSSARY_CATEGORIES)
    category_words = (
        DEFAULT_LOW_VALUE_CATEGORY_WORDS
        if low_value_category_words is None
        else low_value_category_words
    )
    long_phrase_categories = set(
        long_phrase_categories or DEFAULT_LONG_PHRASE_CATEGORIES
    )
    eligible, rejected = [], []
    for raw_proposal in new_terms:
        proposal = dict(raw_proposal)
        chinese = str(proposal.get("chinese", "") or "").strip()
        english = str(proposal.get("english", "") or "").strip()
        category = normalize_category(proposal.get("category", ""))
        proposal.update(
            {"chinese": chinese, "english": english, "category": category,
             "status": "provisional"}
        )
        if not chinese or not english:
            rejected.append({"chinese": chinese, "reason": "missing Chinese or English rendering"})
        elif chinese in existing_terms:
            continue
        elif chinese in low_value_terms:
            rejected.append({"chinese": chinese, "reason": "low-value/blocklisted term"})
        elif category not in allowed_categories:
            rejected.append({"chinese": chinese, "reason": f"category not approved: {category or '(blank)'}"})
        elif category == "location" and (
            chinese.endswith(GENERIC_SUB_LOCATION_SUFFIXES)
            or any(part in english.lower() for part in GENERIC_SUB_LOCATION_ENGLISH)
        ):
            rejected.append({"chinese": chinese, "reason": "generic sub-location"})
        elif any(
            word in english.lower()
            for word in category_words.get(category, ())
        ):
            rejected.append({"chinese": chinese, "reason": "generic or mundane term"})
        elif len(chinese) > 12 and category not in long_phrase_categories:
            rejected.append({"chinese": chinese, "reason": "suspiciously long phrase-like term"})
        else:
            eligible.append(proposal)
    return eligible, rejected


def retroactive_replacement_is_safe(
    old_english, new_english, category, safe_categories=None
):
    old_english = (old_english or "").strip()
    new_english = (new_english or "").strip()
    category = normalize_category(category)
    if not old_english or not new_english or old_english == new_english:
        return False
    safe_categories = set(RETROACTIVE_SAFE_CATEGORIES if safe_categories is None else safe_categories)
    if category not in safe_categories:
        return False
    if old_english.lower() in RETROACTIVE_UNSAFE_GENERIC_ENGLISH:
        return False
    if category in {"military rank", "historical office"} and " " not in old_english and "-" not in old_english:
        return False
    return True


def source_anchored_replacement(chinese_term, chinese_source, english_text,
                                old_english, new_english, category,
                                safe_categories=None, *, glossary=None):
    """Compatibility pure helper; full ownership inventory is mandatory.

    Historical filesystem writes must use retroactive_transactions instead.
    Missing glossary evidence fails closed even when the category is eligible.
    """
    if glossary is None:
        return english_text, 0
    from retroactive_span_planning import (
        normalized_rules, ownership_policy, plan_chapter, transform_bytes,
    )
    rules = normalized_rules([{"chinese": chinese_term, "old_english": old_english,
                               "new_english": new_english, "category": category}])
    before = english_text.encode("utf-8")
    edits, _decisions = plan_chapter(
        chinese_source or "", before, rules, ownership_policy(glossary),
        RETROACTIVE_SAFE_CATEGORIES if safe_categories is None else safe_categories,
    )
    return transform_bytes(before, edits).decode("utf-8"), len(edits)


def recover_pending_translation(chapter_number, pending_dir):
    """Read an already-paid pending translation, or return None without generating."""
    pending_dir = Path(pending_dir)
    text_path = pending_dir / f"{chapter_number:04d}.md"
    if not text_path.exists():
        return None
    meta_path = pending_dir / f"{chapter_number:04d}.json"
    meta = {}
    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8") as handle:
            meta = json.load(handle)
    return {"translation": text_path.read_text(encoding="utf-8"), "meta": meta}


def obtain_translation(chapter_number, pending_dir, generate):
    """Prefer paid pending output and invoke generate only when none exists."""
    recovered = recover_pending_translation(chapter_number, pending_dir)
    if recovered is not None:
        return {"source": "pending", **recovered}
    return {"source": "generated", "generated_result": generate(), "meta": {}}


def pending_matches_committed(chapter_number, pending_dir, output_dir):
    """True only when a completed chapter has an identical stale pending copy."""
    pending_path = Path(pending_dir) / f"{chapter_number:04d}.md"
    committed_path = Path(output_dir) / f"{chapter_number:04d}.md"
    if not pending_path.exists() or not committed_path.exists():
        return False
    return pending_path.read_bytes() == committed_path.read_bytes()


def replace_chapter_issues(existing, chapter_number, new_items):
    """Make replay of a partially committed chapter idempotent for issue logs."""
    retained = [
        item for item in existing
        if not isinstance(item, dict) or item.get("chapter") != chapter_number
    ]
    return retained + list(new_items)


def recognize_chinese_chapter_heading(text):
    match = CHINESE_CHAPTER_HEADING_RE.search((text or "")[:1000])
    return int(match.group(1)) if match else None


def recognize_english_chapter_heading(block):
    """Recognize plain, hash-prefixed, and bold Markdown chapter headings."""
    candidate = (block or "").strip()
    candidate = re.sub(r"^#{1,6}\s*", "", candidate, count=1).strip()
    if candidate.startswith("**") and candidate.endswith("**") and len(candidate) > 4:
        candidate = candidate[2:-2].strip()
    match = ENGLISH_CHAPTER_HEADING_RE.fullmatch(candidate)
    if not match:
        return None
    return {"chapter": int(match.group(1)), "title": (match.group(2) or "").strip()}


def validate_progress_consistency(progress, committed_chapters):
    """Validate progress metadata against numeric committed chapter files."""
    errors = []
    completed = progress.get("completed")
    if not isinstance(completed, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in (completed or [])
    ):
        completed = []
        errors.append("progress.completed must be a list of chapter integers")
    unique_completed = sorted(set(completed))
    expected_last = unique_completed[-1] if unique_completed else 0
    expected_range = list(range(1, expected_last + 1))
    if len(unique_completed) != len(completed):
        errors.append("progress.completed contains duplicate chapters")
    if completed != unique_completed:
        errors.append("progress.completed is not strictly increasing")
    if unique_completed != expected_range:
        errors.append("progress.completed is not continuous from Chapter 1")
    if progress.get("last_completed") != expected_last:
        errors.append(
            f"progress.last_completed is {progress.get('last_completed')}; completed ends at {expected_last}"
        )
    if progress.get("next_chapter") != expected_last + 1:
        errors.append(
            f"progress.next_chapter is {progress.get('next_chapter')}; expected {expected_last + 1}"
        )
    committed = set(committed_chapters)
    expected = set(unique_completed)
    missing = sorted(expected - committed)
    unexpected = sorted(committed - expected)
    if missing:
        errors.append("committed chapter files are missing for progress entries")
    if unexpected:
        errors.append("committed chapter files contain chapters absent from progress")
    return {
        "errors": errors,
        "completed": unique_completed,
        "last_completed": expected_last,
        "missing_committed_files": missing,
        "unexpected_committed_files": unexpected,
    }


def validate_stats_record(stats, label, allow_unpriced_zero=False):
    """Validate saved token/rate accounting without changing it."""
    errors = []
    if not isinstance(stats, dict):
        return [f"{label} must be a JSON object"]
    for key in ACCOUNTING_FIELDS:
        value = stats.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            errors.append(f"{label}.{key} must be a non-negative number")
    if errors:
        return errors
    prompt = stats["prompt_tokens"]
    hits = stats["cache_hit_tokens"]
    misses = stats["cache_miss_tokens"]
    if hits + misses != prompt:
        errors.append(f"{label} cache hit + miss tokens do not equal prompt tokens")
    if (allow_unpriced_zero and "translation_accepted_operations" not in stats
        and "state_accepted_operations" not in stats
        and not any(stats.get(key, 0) for key in ACCOUNTING_FIELDS)):
        return errors
    aggregate_keys = {"translation_accepted_operations", "state_accepted_operations"} & stats.keys()
    if aggregate_keys:
        if len(aggregate_keys) != 1:
            return errors + [f"{label} mixes accepted operation families"]
        aggregate_key = next(iter(aggregate_keys))
        operations = stats[aggregate_key]
        state_aggregate = aggregate_key == "state_accepted_operations"
        if (not isinstance(operations, list) or len(operations) not in ({2, 3} if state_aggregate else {2})
            or any(not isinstance(entry, dict) for entry in operations)):
            return errors + [f"{label} has invalid accepted translation operations"]
        ids = [entry.get("operation_id") for entry in operations]
        if (any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids)
            or len(ids) != len(set(ids))):
            return errors + [f"{label} has invalid accepted operation identities"]
        if state_aggregate and tuple(entry.get("operation_kind") for entry in operations) not in {
            ("state_extraction", "state_extraction_recovery"),
            ("state_extraction", "state_json_repair"),
            ("state_extraction", "state_extraction_recovery", "state_json_repair"),
        }:
            return errors + [f"{label} has invalid accepted state operation order"]
        individual = [entry.get("stats") for entry in operations]
        if state_aggregate and (stats.get("provider") == "command_code_provider" or
            stats.get("accounting") == "command_code_credit_equivalent" or
            any(isinstance(value, dict) and (
                value.get("provider") == "command_code_provider" or
                value.get("accounting") == "command_code_credit_equivalent") for value in individual)):
            return errors + [f"{label} mixes direct state and GOAT accounting"]
        if any(not isinstance(value, dict) or
               {"translation_accepted_operations", "state_accepted_operations"} & value.keys()
               for value in individual):
            return errors + [f"{label} has invalid/nested accepted usage"]
        import math
        for value in (stats, *individual):
            if any(isinstance(value.get(key), float) and not math.isfinite(value[key]) for key in ACCOUNTING_FIELDS):
                errors.append(f"{label} has nonfinite accepted accounting")
        for index, value in enumerate(individual):
            errors.extend(validate_stats_record(value, f"{label}.operation[{index}]"))
        if errors:
            return errors
        for key in ACCOUNTING_FIELDS:
            expected = sum(value[key] for value in individual)
            if abs(stats[key] - expected) > (1e-12 if key.endswith("cost_usd") else 0):
                errors.append(f"{label}.{key} does not equal accepted operation sum")
        return errors
    if stats.get("provider") == "command_code_provider" or stats.get("accounting") == "command_code_credit_equivalent":
        from production_accounting import validate_goat_stats
        try:
            validate_goat_stats(stats)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{label}: {exc}")
        return errors
    period = stats.get("pricing_period")
    if period not in V4_PRO_PRICES:
        errors.append(f"{label}.pricing_period must be peak or off_peak")
    if stats.get("pricing_version") != PRICING_VERSION:
        errors.append(f"{label} uses an unknown or legacy pricing version")
    if not isinstance(stats.get("request_started_utc"), str) or not stats["request_started_utc"]:
        errors.append(f"{label} lacks request_started_utc")
    required_rates = (
        "cache_hit_rate_per_million", "cache_miss_rate_per_million",
        "output_rate_per_million",
    )
    if any(not isinstance(stats.get(key), (int, float)) for key in required_rates):
        errors.append(f"{label} is missing numeric pricing rates")
    elif period in V4_PRO_PRICES:
        rate_keys = {
            "cache_hit_rate_per_million": "cache_hit",
            "cache_miss_rate_per_million": "cache_miss",
            "output_rate_per_million": "output",
        }
        for saved_key, official_key in rate_keys.items():
            if stats[saved_key] != V4_PRO_PRICES[period][official_key]:
                errors.append(f"{label}.{saved_key} does not match the {period} rate")
        expected_cost, _, _ = calculate_cost_from_token_counts(
            prompt, stats["completion_tokens"], hits, misses, stats
        )
        if abs(stats["cost_usd"] - expected_cost) > 1e-12:
            errors.append(f"{label}.cost_usd does not match saved tokens and rates")
        if abs(stats["rate_correct_cost_usd"] - expected_cost) > 1e-12:
            errors.append(f"{label}.rate_correct_cost_usd does not match saved tokens and rates")
    return errors


def inspect_pending_state(pending_dir, completed_chapters, next_chapter):
    """Read and validate transactional pending files without changing them."""
    pending_dir = Path(pending_dir)
    errors, warnings, items = [], [], []
    if not pending_dir.exists():
        return {"errors": [], "warnings": [], "pending": []}
    files = [path for path in pending_dir.iterdir() if path.is_file()]
    stems = sorted({path.stem for path in files})
    completed = set(completed_chapters)
    allowed_stages = {"translated", "state_extracted", "reviewed"}
    for stem in stems:
        related = [path for path in files if path.stem == stem]
        unsupported = sorted(path.suffix for path in related if path.suffix not in {".md", ".json"})
        if not stem.isdigit():
            warnings.append(f"ignoring non-chapter file in pending: {stem}")
            continue
        chapter = int(stem)
        text_path = pending_dir / f"{stem}.md"
        meta_path = pending_dir / f"{stem}.json"
        item = {"chapter": chapter, "stage": "unknown", "has_translation": text_path.exists(),
                "has_metadata": meta_path.exists()}
        items.append(item)
        if unsupported:
            warnings.append(f"pending Chapter {chapter} has unsupported files: {unsupported}")
        missing_text = not text_path.exists()
        if missing_text:
            errors.append(f"pending Chapter {chapter} has metadata/files but no paid translation .md")
        elif not text_path.read_text(encoding="utf-8").strip():
            errors.append(f"pending Chapter {chapter} translation is empty")
        if not meta_path.exists():
            errors.append(f"pending Chapter {chapter} has no metadata .json")
            continue
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"pending Chapter {chapter} metadata is invalid JSON: {exc}")
            continue
        stage = meta.get("stage")
        item["stage"] = stage or "unknown"
        if meta.get("chapter") != chapter:
            errors.append(f"pending Chapter {chapter} metadata chapter does not match filename")
        if stage == "translation_recovery_recorded":
            # Only the interrupted metadata-first recovery write is admissible.
            # Prove both completed immutable journals and every saved chain field;
            # missing text later reconstructs the exact requests before replay.
            from production_failure_classification import NonProviderProductionError
            from production_translation_recovery_prefix import load_recorded_recovery_prefix
            try:
                load_recorded_recovery_prefix(meta_path, meta)
            except (OSError, ValueError, NonProviderProductionError, KeyError, TypeError) as exc:
                errors.append(f"pending Chapter {chapter} recovery prefix proof failed: {exc}")
            else:
                if missing_text:
                    errors.remove(f"pending Chapter {chapter} has metadata/files but no paid translation .md")
                stage = "translated"
        if stage not in allowed_stages:
            errors.append(f"pending Chapter {chapter} has invalid stage: {stage!r}")
        if "translation_stats" not in meta:
            errors.append(f"pending Chapter {chapter} lacks translation_stats")
        else:
            errors.extend(validate_stats_record(
                meta["translation_stats"], f"pending Chapter {chapter} translation_stats"
            ))
        if stage in {"state_extracted", "reviewed"} and "state" not in meta:
            errors.append(f"pending Chapter {chapter} stage {stage} lacks saved state")
        if stage in {"state_extracted", "reviewed"}:
            if "state_stats" not in meta:
                errors.append(f"pending Chapter {chapter} stage {stage} lacks state_stats")
            else:
                errors.extend(validate_stats_record(
                    meta["state_stats"], f"pending Chapter {chapter} state_stats"
                ))
            if not isinstance(meta.get("state_checkpoint_identity"), dict):
                errors.append(
                    f"pending Chapter {chapter} stage {stage} lacks "
                    "state_checkpoint_identity"
                )
        if stage == "reviewed" and "review_result" not in meta:
            errors.append(f"pending Chapter {chapter} stage reviewed lacks review_result")
        if stage == "reviewed":
            if "review_stats" not in meta:
                errors.append(f"pending Chapter {chapter} stage reviewed lacks review_stats")
            else:
                errors.extend(validate_stats_record(
                    meta["review_stats"], f"pending Chapter {chapter} review_stats",
                    allow_unpriced_zero=True,
                ))
            if not isinstance(meta.get("review_checkpoint_identity"), dict):
                errors.append(
                    f"pending Chapter {chapter} stage reviewed lacks "
                    "review_checkpoint_identity"
                )
        if chapter in completed:
            errors.append(f"pending Chapter {chapter} is already marked completed")
        if chapter != next_chapter:
            errors.append(f"pending Chapter {chapter} does not match next_chapter {next_chapter}")
    if len(items) > 1:
        errors.append("multiple pending chapters conflict with sequential single-worker recovery")
    return {"errors": errors, "warnings": warnings, "pending": items}
