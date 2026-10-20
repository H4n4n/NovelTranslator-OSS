"""Deterministic semantic-integrity helpers for translated chapters.

The first version is intentionally conservative. It does not ask an LLM to
judge prose and it does not rewrite translations. It reports source-backed
identity inconsistencies that can be proven from the persistent glossary.

This module is import-safe and performs no API work or filesystem mutation.
"""

import re
from collections import defaultdict

from noveltranslator_core import normalize_category, strict_term_valid_targets


DEFAULT_IDENTITY_CATEGORIES = frozenset({
    "character",
    "alias",
    "state",
    "dynasty",
    "sect",
    "clan",
    "faction",
    "organization",
    "location",
    "cultivation realm",
    "cultivation stage",
    "cultivation system",
    "cultivation technique",
    "martial technique",
    "movement technique",
    "sword technique",
    "secret art",
    "divine ability",
    "forbidden technique",
    "noble title",
    "government office",
    "historical office",
    "military rank",
    "military unit",
    "political institution",
    "divine office",
    "mythological figure",
    "mythological species",
})


def _bounded_target_pattern(phrase):
    if not phrase:
        return None
    left = r"(?<![A-Za-z0-9])" if phrase[0].isascii() and phrase[0].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if phrase[-1].isascii() and phrase[-1].isalnum() else ""
    return re.compile(left + re.escape(phrase) + right, flags=re.IGNORECASE)


def _target_occurrences(text, phrase):
    pattern = _bounded_target_pattern(phrase)
    if pattern is None:
        return ()
    return tuple(
        {
            "start": match.start(),
            "end": match.end(),
            "text": match.group(0),
        }
        for match in pattern.finditer(text or "")
    )


def _entry_targets(entry):
    targets = list(strict_term_valid_targets(entry))
    approved = entry.get("approved_renderings", [])
    if isinstance(approved, list):
        targets.extend(
            item.strip() for item in approved
            if isinstance(item, str) and item.strip()
        )
    result = []
    seen = set()
    for target in targets:
        key = target.casefold()
        if not target or key in seen:
            continue
        seen.add(key)
        result.append(target)
    return tuple(result)


def source_backed_identity_entries(
    source_text,
    glossary_entries,
    identity_categories=DEFAULT_IDENTITY_CATEGORIES,
):
    """Return locked identity-sensitive entries whose exact source key occurs."""
    categories = {normalize_category(item) for item in identity_categories}
    selected = []
    if not isinstance(glossary_entries, dict):
        return tuple()
    for source_key, entry in glossary_entries.items():
        if not isinstance(source_key, str) or not source_key:
            continue
        if not isinstance(entry, dict) or entry.get("status") != "locked":
            continue
        category = normalize_category(entry.get("category", ""))
        if category not in categories or source_key not in (source_text or ""):
            continue
        canonical = str(entry.get("english", "") or "").strip()
        if not canonical:
            continue
        selected.append((source_key, entry))
    return tuple(selected)


def entity_integrity_report(
    source_text,
    translation,
    glossary_entries,
    identity_categories=DEFAULT_IDENTITY_CATEGORIES,
):
    """Report proven glossary/entity inconsistencies without rewriting text.

    The report is advisory unless the glossary entry already opted into
    ``strict_qa``. This lets production measure broader semantic drift before
    deciding whether any additional category should become a hard blocker.
    """
    source_text = source_text or ""
    translation = translation or ""
    source_present = source_backed_identity_entries(
        source_text, glossary_entries, identity_categories
    )
    present_keys = {source_key for source_key, _entry in source_present}
    advisories = []
    strict_blockers = []
    observations = []

    for source_key, entry in source_present:
        canonical = str(entry.get("english", "") or "").strip()
        category = normalize_category(entry.get("category", ""))
        targets = _entry_targets(entry)
        matched = []
        for target in targets:
            occurrences = _target_occurrences(translation, target)
            if occurrences:
                matched.append({
                    "rendering": target,
                    "count": len(occurrences),
                    "occurrences": list(occurrences),
                })
        observations.append({
            "source_key": source_key,
            "category": category,
            "canonical": canonical,
            "source_count": source_text.count(source_key),
            "matched_renderings": [item["rendering"] for item in matched],
        })
        if matched:
            continue

        known_alias_hits = []
        aliases = entry.get("repair_aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if not isinstance(alias, str) or not alias.strip():
                    continue
                occurrences = _target_occurrences(translation, alias.strip())
                if occurrences:
                    known_alias_hits.append({
                        "rendering": alias.strip(),
                        "count": len(occurrences),
                    })

        issue = {
            "type": (
                "known_noncanonical_entity_alias"
                if known_alias_hits else "source_entity_missing_canonical_family"
            ),
            "source_key": source_key,
            "category": category,
            "canonical": canonical,
            "source_count": source_text.count(source_key),
            "known_alias_hits": known_alias_hits,
            "strict_qa": entry.get("strict_qa") is True,
        }
        if entry.get("strict_qa") is True:
            strict_blockers.append(issue)
        else:
            advisories.append(issue)

    # A conservative cross-entity substitution signal: only consider a
    # canonical rendering owned by one other locked identity entry, require the
    # expected source-backed entity to be missing, and never claim that the
    # substitute is definitely wrong. This remains advisory.
    canonical_owners = defaultdict(list)
    if isinstance(glossary_entries, dict):
        categories = {normalize_category(item) for item in identity_categories}
        for source_key, entry in glossary_entries.items():
            if not isinstance(entry, dict) or entry.get("status") != "locked":
                continue
            category = normalize_category(entry.get("category", ""))
            canonical = str(entry.get("english", "") or "").strip()
            if category in categories and canonical:
                canonical_owners[canonical.casefold()].append((source_key, canonical, category))

    if advisories or strict_blockers:
        missing_keys = {
            item["source_key"] for item in (*advisories, *strict_blockers)
        }
        for owners in canonical_owners.values():
            if len(owners) != 1:
                continue
            owner_source, rendering, category = owners[0]
            if owner_source in present_keys or owner_source in missing_keys:
                continue
            occurrences = _target_occurrences(translation, rendering)
            if not occurrences:
                continue
            advisories.append({
                "type": "possible_cross_entity_substitution",
                "source_key": owner_source,
                "category": category,
                "rendering": rendering,
                "translation_count": len(occurrences),
                "reason": (
                    "translation contains the unique canonical rendering of a "
                    "different locked identity whose source key is absent while "
                    "at least one source-backed identity is missing"
                ),
            })

    return {
        "source_backed_identity_count": len(source_present),
        "observations": observations,
        "advisories": advisories,
        "strict_blockers": strict_blockers,
        "strict_commit_allowed": not strict_blockers,
    }


def rendering_drift_report(observations):
    """Aggregate chapter observations and surface cross-chapter rendering drift.

    ``observations`` are deliberately plain records so the future production
    integration can persist them without coupling this helper to a database or
    project layout.
    """
    grouped = defaultdict(list)
    for item in observations or ():
        if not isinstance(item, dict):
            continue
        source_key = str(item.get("source_key", "") or "").strip()
        rendering = str(item.get("rendering", "") or "").strip()
        if not source_key or not rendering:
            continue
        grouped[source_key].append(item)

    drift = []
    for source_key, items in sorted(grouped.items()):
        renderings = defaultdict(list)
        canonical = ""
        category = ""
        for item in items:
            rendering = str(item["rendering"]).strip()
            renderings[rendering.casefold()].append(item)
            canonical = canonical or str(item.get("canonical", "") or "").strip()
            category = category or normalize_category(item.get("category", ""))
        if len(renderings) <= 1:
            continue
        variants = []
        for records in renderings.values():
            rendering = str(records[0]["rendering"]).strip()
            variants.append({
                "rendering": rendering,
                "count": len(records),
                "chapters": sorted({
                    item.get("chapter") for item in records
                    if isinstance(item.get("chapter"), int)
                }),
            })
        variants.sort(key=lambda item: (-item["count"], item["rendering"].casefold()))
        drift.append({
            "source_key": source_key,
            "category": category,
            "canonical": canonical,
            "variants": variants,
            "variant_count": len(variants),
        })
    return {
        "terms_observed": len(grouped),
        "drift_term_count": len(drift),
        "drift": drift,
    }
