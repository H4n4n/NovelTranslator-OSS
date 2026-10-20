"""Import-safe reusable translation policy profiles.

Profiles contain immutable policy and model instructions only. They never own
or resolve novel progress, glossary, chapter, pending, audit, backup, log, EPUB,
credential, or other mutable production paths.
"""

from dataclasses import dataclass
import json
import re
from pathlib import Path

from noveltranslator_core import normalize_category, prefilter_candidates


PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
REQUIRED_PROFILE_TOP_LEVEL_KEYS = {
    "id", "display_name", "description", "translation_policy", "glossary",
    "capitalization", "audit",
}
PROFILE_TOP_LEVEL_KEYS = REQUIRED_PROFILE_TOP_LEVEL_KEYS | {"auxiliary_policy"}
TRANSLATION_POLICY_KEYS = {
    "style", "terminology_preservation_rules", "pinyin_preference",
    "semantic_translation_preference",
}
GLOSSARY_KEYS = {
    "allowed_categories", "high_priority_categories", "review_priorities",
    "retroactive_safe_categories", "long_phrase_categories",
    "low_value_category_words",
}
CAPITALIZATION_KEYS = {"style", "formal_named_categories"}
AUDIT_KEYS = {"priorities"}
AUXILIARY_POLICY_KEYS = {"terminology_distinctions"}
NOVEL_POLICY_START = "[AUXILIARY_TERMINOLOGY_POLICY]"
NOVEL_POLICY_END = "[/AUXILIARY_TERMINOLOGY_POLICY]"
MAX_NOVEL_POLICY_RULES = 16
MAX_NOVEL_POLICY_RULE_CHARS = 300
LEGACY_MING_LOW_VALUE_TERMS = frozenset({
    "紅票", "红票", "負距離", "负距离", "磚家", "砖家", "手藝人", "手艺人",
    "狼皮座椅", "商隊", "商队", "突進之策", "突进之策", "白毛風", "白毛风",
    "寶豐堂後門", "宝丰堂后门", "倒夜壺", "倒夜壶", "西洋鏡", "西洋镜",
})


def validate_profile_id(profile_id):
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError(
            "profile identifier must contain only lowercase letters, digits, '-' or '_', "
            "and must start with a letter or digit"
        )
    return profile_id


def project_low_value_terms(novel_id):
    """Return the legacy per-project blocklist used by candidate preparation."""
    return LEGACY_MING_LOW_VALUE_TERMS if novel_id == "ming_novel" else frozenset()


def _object(value, label, allowed_keys):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    unknown = sorted(set(value) - set(allowed_keys))
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _text_tuple(value, label, normalize=False):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list of strings")
    result = []
    for index, item in enumerate(value):
        text = _text(item, f"{label}[{index}]")
        result.append(normalize_category(text) if normalize else text)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicate values")
    return tuple(result)


def _require_subset(values, allowed, label):
    unsupported = sorted(set(values) - set(allowed))
    if unsupported:
        raise ValueError(f"{label} contains categories outside the allowlist: {unsupported}")


@dataclass(frozen=True)
class TranslationProfile:
    id: str
    display_name: str
    description: str
    translation_style: tuple
    terminology_preservation_rules: tuple
    pinyin_preference: str
    semantic_translation_preference: str
    allowed_categories: frozenset
    high_priority_categories: tuple
    glossary_review_priorities: tuple
    retroactive_safe_categories: frozenset
    long_phrase_categories: frozenset
    low_value_category_words: tuple
    capitalization_style: str
    formal_named_categories: frozenset
    periodic_audit_priorities: tuple
    auxiliary_terminology_distinctions: tuple
    translation_instructions: str

    @classmethod
    def from_dict(cls, value, translation_instructions):
        value = _object(value, "profile.json", PROFILE_TOP_LEVEL_KEYS)
        missing = sorted(REQUIRED_PROFILE_TOP_LEVEL_KEYS - set(value))
        if missing:
            raise ValueError("profile.json is missing: " + ", ".join(missing))

        profile_id = validate_profile_id(value["id"])
        policy = _object(
            value["translation_policy"],
            "profile.json translation_policy",
            TRANSLATION_POLICY_KEYS,
        )
        glossary = _object(value["glossary"], "profile.json glossary", GLOSSARY_KEYS)
        capitalization = _object(
            value["capitalization"],
            "profile.json capitalization",
            CAPITALIZATION_KEYS,
        )
        audit = _object(value["audit"], "profile.json audit", AUDIT_KEYS)
        raw_auxiliary_policy = value.get("auxiliary_policy")
        if raw_auxiliary_policy is None:
            auxiliary_distinctions = ()
        else:
            auxiliary_policy = _object(
                raw_auxiliary_policy,
                "profile.json auxiliary_policy",
                AUXILIARY_POLICY_KEYS,
            )
            auxiliary_distinctions = _text_tuple(
                auxiliary_policy.get("terminology_distinctions"),
                "profile.json auxiliary_policy.terminology_distinctions",
            )

        allowed = _text_tuple(
            glossary.get("allowed_categories"),
            "profile.json glossary.allowed_categories",
            normalize=True,
        )
        high_priority = _text_tuple(
            glossary.get("high_priority_categories"),
            "profile.json glossary.high_priority_categories",
            normalize=True,
        )
        retroactive_safe = _text_tuple(
            glossary.get("retroactive_safe_categories"),
            "profile.json glossary.retroactive_safe_categories",
            normalize=True,
        )
        long_phrase = _text_tuple(
            glossary.get("long_phrase_categories"),
            "profile.json glossary.long_phrase_categories",
            normalize=True,
        )
        formal_named = _text_tuple(
            capitalization.get("formal_named_categories"),
            "profile.json capitalization.formal_named_categories",
            normalize=True,
        )
        for categories, label in (
            (high_priority, "high_priority_categories"),
            (retroactive_safe, "retroactive_safe_categories"),
            (long_phrase, "long_phrase_categories"),
            (formal_named, "formal_named_categories"),
        ):
            _require_subset(categories, allowed, label)

        raw_low_value = glossary.get("low_value_category_words")
        if not isinstance(raw_low_value, dict):
            raise ValueError("profile.json glossary.low_value_category_words must be an object")
        low_value = []
        for raw_category, raw_words in raw_low_value.items():
            category = normalize_category(raw_category)
            _require_subset((category,), allowed, "low_value_category_words")
            words = _text_tuple(
                raw_words,
                f"profile.json glossary.low_value_category_words.{raw_category}",
            )
            low_value.append((category, tuple(word.lower() for word in words)))

        capitalization_style = _text(
            capitalization.get("style"), "profile.json capitalization.style"
        )
        if capitalization_style != "capitalize_every_word":
            raise ValueError("unsupported capitalization style: " + capitalization_style)
        instructions = _text(
            translation_instructions, "translation_instructions.txt"
        )
        return cls(
            id=profile_id,
            display_name=_text(value["display_name"], "profile.json display_name"),
            description=_text(value["description"], "profile.json description"),
            translation_style=_text_tuple(
                policy.get("style"), "profile.json translation_policy.style"
            ),
            terminology_preservation_rules=_text_tuple(
                policy.get("terminology_preservation_rules"),
                "profile.json translation_policy.terminology_preservation_rules",
            ),
            pinyin_preference=_text(
                policy.get("pinyin_preference"),
                "profile.json translation_policy.pinyin_preference",
            ),
            semantic_translation_preference=_text(
                policy.get("semantic_translation_preference"),
                "profile.json translation_policy.semantic_translation_preference",
            ),
            allowed_categories=frozenset(allowed),
            high_priority_categories=high_priority,
            glossary_review_priorities=_text_tuple(
                glossary.get("review_priorities"),
                "profile.json glossary.review_priorities",
            ),
            retroactive_safe_categories=frozenset(retroactive_safe),
            long_phrase_categories=frozenset(long_phrase),
            low_value_category_words=tuple(low_value),
            capitalization_style=capitalization_style,
            formal_named_categories=frozenset(formal_named),
            periodic_audit_priorities=_text_tuple(
                audit.get("priorities"), "profile.json audit.priorities"
            ),
            auxiliary_terminology_distinctions=auxiliary_distinctions,
            translation_instructions=instructions,
        )

    def requires_formal_capitalization(self, category):
        return normalize_category(category) in self.formal_named_categories

    def low_value_words(self):
        return {category: words for category, words in self.low_value_category_words}


@dataclass(frozen=True)
class NovelTerminologyPolicy:
    """Bounded auxiliary-call policy with no mutable project paths or state."""

    profile_id: str
    profile_distinctions: tuple
    novel_rules: tuple


def _novel_policy_rules(addendum):
    """Extract only an explicit compact policy block from a prose addendum."""
    start_count = addendum.count(NOVEL_POLICY_START)
    end_count = addendum.count(NOVEL_POLICY_END)
    if start_count == 0 and end_count == 0:
        return ()
    if start_count != 1 or end_count != 1:
        raise ValueError("translation addendum has an invalid auxiliary policy block")
    start = addendum.index(NOVEL_POLICY_START) + len(NOVEL_POLICY_START)
    end = addendum.index(NOVEL_POLICY_END)
    if end < start:
        raise ValueError("translation addendum auxiliary policy markers are out of order")
    rules = []
    for line_number, raw_line in enumerate(addendum[start:end].splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("-"):
            raise ValueError(
                "auxiliary terminology policy rules must be non-empty '-' list items "
                f"(block line {line_number})"
            )
        rule = line[1:].strip()
        if not rule:
            raise ValueError("auxiliary terminology policy contains an empty rule")
        if len(rule) > MAX_NOVEL_POLICY_RULE_CHARS:
            raise ValueError(
                "auxiliary terminology policy rule exceeds "
                f"{MAX_NOVEL_POLICY_RULE_CHARS} characters"
            )
        rules.append(rule)
    if not rules:
        raise ValueError("translation addendum auxiliary policy block has no rules")
    if len(rules) > MAX_NOVEL_POLICY_RULES:
        raise ValueError(
            "translation addendum auxiliary policy exceeds "
            f"{MAX_NOVEL_POLICY_RULES} rules"
        )
    if len(set(rules)) != len(rules):
        raise ValueError("translation addendum auxiliary policy contains duplicate rules")
    return tuple(rules)


def load_novel_terminology_policy(profile, novel_addendum_file=None):
    """Build compact profile/novel policy without credentials, APIs, or writes."""
    novel_rules = ()
    if novel_addendum_file is not None:
        addendum_path = Path(novel_addendum_file)
        if addendum_path.is_file():
            novel_rules = _novel_policy_rules(
                addendum_path.read_text(encoding="utf-8")
            )
    return NovelTerminologyPolicy(
        profile_id=profile.id,
        profile_distinctions=profile.auxiliary_terminology_distinctions,
        novel_rules=novel_rules,
    )


def load_translation_profile(profile_id, repo_root=None):
    """Load one fixed-location profile without credentials, APIs, or writes."""
    profile_id = validate_profile_id(profile_id)
    repo_root = Path(repo_root or Path(__file__).resolve().parent).resolve()
    profiles_root = (repo_root / "profiles").resolve()
    profile_root = (profiles_root / profile_id).resolve()
    if profile_root.parent != profiles_root:
        raise ValueError("profile path escapes the repository profiles directory")
    config_file = profile_root / "profile.json"
    instructions_file = profile_root / "translation_instructions.txt"
    if not config_file.is_file():
        raise FileNotFoundError(f"translation profile does not exist: {config_file}")
    if not instructions_file.is_file():
        raise FileNotFoundError(
            f"translation profile instructions do not exist: {instructions_file}"
        )
    with config_file.open("r", encoding="utf-8") as handle:
        raw_config = json.load(handle)
    profile = TranslationProfile.from_dict(
        raw_config, instructions_file.read_text(encoding="utf-8")
    )
    if profile.id != profile_id:
        raise ValueError(
            f"profile.json id {profile.id!r} does not match selected profile {profile_id!r}"
        )
    return profile


def effective_translation_instructions(profile, novel_addendum_file=None):
    """Combine immutable profile policy with one fixed novel-owned addendum."""
    base = profile.translation_instructions
    if novel_addendum_file is None:
        return base
    addendum_path = Path(novel_addendum_file)
    if not addendum_path.is_file():
        return base
    addendum = addendum_path.read_text(encoding="utf-8").strip()
    if not addendum:
        return base
    return base.rstrip() + "\n\nNOVEL-SPECIFIC TRANSLATION ADDENDUM\n\n" + addendum


def filter_profile_candidates(profile, new_terms, existing_terms=(), low_value_terms=()):
    return prefilter_candidates(
        new_terms,
        existing_terms=existing_terms,
        low_value_terms=low_value_terms,
        allowed_categories=profile.allowed_categories,
        low_value_category_words=profile.low_value_words(),
        long_phrase_categories=profile.long_phrase_categories,
    )


def select_profile_category(profile, proposed_category, fallback_category):
    """Keep reviewer category changes inside the selected profile allowlist."""
    proposed = normalize_category(proposed_category)
    if proposed in profile.allowed_categories:
        return proposed
    fallback = normalize_category(fallback_category)
    if fallback in profile.allowed_categories:
        return fallback
    raise ValueError(
        f"neither proposed nor fallback category is allowed by profile {profile.id}"
    )


def _compact_auxiliary_policy(profile, novel_policy):
    if novel_policy is None:
        profile_rules = profile.auxiliary_terminology_distinctions
        novel_rules = ()
    else:
        if novel_policy.profile_id != profile.id:
            raise ValueError(
                "novel terminology policy profile does not match the selected profile"
            )
        profile_rules = novel_policy.profile_distinctions
        novel_rules = novel_policy.novel_rules
    sections = []
    if profile_rules:
        sections.append(
            "Profile terminology distinctions:\n"
            + "\n".join(f"- {item}" for item in profile_rules)
        )
    if novel_rules:
        sections.append(
            "Novel-specific terminology rules:\n"
            + "\n".join(f"- {item}" for item in novel_rules)
        )
    return "\n".join(sections)


def compact_auxiliary_terminology_policy(profile, novel_policy=None):
    """Return only bounded terminology distinctions for auxiliary API work."""
    return _compact_auxiliary_policy(profile, novel_policy)


def _append_auxiliary_policy(base, profile, novel_policy):
    compact = _compact_auxiliary_policy(profile, novel_policy)
    if not compact:
        return base
    return base + "\n" + compact


def profile_state_instructions(profile, novel_policy=None):
    categories = ", ".join(sorted(profile.allowed_categories))
    high_priority = ", ".join(profile.high_priority_categories)
    base = (
        f"Selected translation profile: {profile.display_name} ({profile.id}).\n"
        f"Use ONLY these glossary categories: {categories}.\n"
        f"Give special attention to recurring high-priority categories: {high_priority}.\n"
        "The high-priority designation does not mean every occurrence belongs in the glossary."
    )
    return _append_auxiliary_policy(base, profile, novel_policy)


def profile_glossary_review_instructions(profile, novel_policy=None):
    priorities = "\n".join(
        f"- {item}" for item in profile.glossary_review_priorities
    )
    preservation = "\n".join(
        f"- {item}" for item in profile.terminology_preservation_rules
    )
    base = (
        f"PROFILE: {profile.display_name} ({profile.id})\n"
        f"Pinyin / proper-name preference: {profile.pinyin_preference}\n"
        f"Semantic-translation preference: {profile.semantic_translation_preference}\n"
        f"Terminology preservation rules:\n{preservation}\n"
        f"Glossary-review priorities:\n{priorities}"
    )
    return _append_auxiliary_policy(base, profile, novel_policy)


def profile_audit_instructions(profile, novel_policy=None):
    priorities = "\n".join(
        f"{index}. {item}"
        for index, item in enumerate(profile.periodic_audit_priorities, start=1)
    )
    base = (
        f"PROFILE: {profile.display_name} ({profile.id})\n"
        "Apply the following profile-specific audit priorities in order:\n"
        + priorities
    )
    return _append_auxiliary_policy(base, profile, novel_policy)
