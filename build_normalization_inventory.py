"""Build a zero-API, read-only normalization inventory for a final corpus.

The scanner never edits chapter prose.  It records source-aligned evidence and
publication-format findings so a later, separately reviewed normalization step
can distinguish deterministic changes from semantic decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from build_finalization_corpus import load_corrected_txt_spine
from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock


CLASSIFICATIONS = (
    "DETERMINISTIC_SAFE",
    "CONTEXT_REQUIRED",
    "AMBIGUOUS",
    "STRUCTURAL_FORMATTING",
    "NO_CHANGE",
)
IDENTITY_CATEGORIES = {
    "character",
    "historical person",
    "noble title",
    "military rank",
    "organization",
    "faction",
    "location",
    "state",
}
SOURCE_ROLE_SUFFIXES = (
    " 대원수", " 기사단장", " 후작가", " 백작가", " 공작가", " 후작", " 백작",
    " 공작", " 가문",
)
ENGLISH_ROLE_WORDS = {
    "baron", "count", "duke", "house", "kingdom", "lord", "marshal",
    "marquis", "of", "the",
}
WORD_RE = re.compile(r"[A-Za-z\u00c0-\u024f]+(?:[’'][A-Za-z\u00c0-\u024f]+)?")
HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff]")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text_exact(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def short_context(text: str, start: int, end: int, radius: int = 72) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    value = re.sub(r"\s+", " ", text[left:right]).strip()
    if left:
        value = "…" + value
    if right < len(text):
        value += "…"
    return value[:180]


def exact_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    if not value:
        return []
    if value.isascii() and any(character.isalpha() for character in value):
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])", re.I)
    else:
        pattern = re.compile(re.escape(value))
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def ascii_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character)).casefold()


def edit_distance(left: str, right: str) -> int:
    left, right = ascii_fold(left), ascii_fold(right)
    previous = list(range(len(right) + 1))
    for index, a in enumerate(left, 1):
        current = [index]
        for offset, b in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[offset] + 1,
                previous[offset - 1] + (a != b),
            ))
        previous = current
    return previous[-1]


def regular_inflection_of(candidate: str, canonical: str) -> bool:
    candidate_folded = ascii_fold(candidate)
    canonical_folded = ascii_fold(canonical)
    if candidate_folded.endswith(("'s", "’s")):
        candidate_folded = candidate_folded[:-2]
        return candidate_folded == canonical_folded
    return candidate_folded in {
        canonical_folded + "s",
        canonical_folded + "es",
    }


def source_identity_core(source_key: str, category: str) -> str:
    if category == "character":
        return source_key
    for suffix in SOURCE_ROLE_SUFFIXES:
        if source_key.endswith(suffix):
            return source_key[:-len(suffix)]
    return source_key


def english_identity_forms(english: str, category: str) -> list[tuple[str, str]]:
    words = WORD_RE.findall(english)
    forms = [(english, "full_name")]
    if category == "character" and len(words) > 1:
        forms.append((words[0], "short_name"))
    if category != "character":
        core = [word for word in words if ascii_fold(word) not in ENGLISH_ROLE_WORDS]
        if core:
            forms.append((" ".join(core), "identity_core"))
    seen = set()
    return [(value, kind) for value, kind in forms if value and not (ascii_fold(value) in seen or seen.add(ascii_fold(value)))]


def _candidate_spelling_words(canonical_form: str, aligned_texts: list[str]) -> set[str]:
    canonical_words = WORD_RE.findall(canonical_form)
    if not canonical_words:
        return set()
    candidates = set()
    for text in aligned_texts:
        for word in WORD_RE.findall(text):
            if not word[:1].isupper():
                continue
            for canonical in canonical_words:
                if len(canonical) < 4:
                    continue
                limit = 1 if len(canonical) < 5 else 2
                if edit_distance(word, canonical) <= limit:
                    candidates.add(word)
    return candidates


def _variant_record(value: str, relationship: str, texts: dict[int, str], source_units: set[int]) -> dict | None:
    occurrences = []
    units = []
    examples = []
    for unit, text in texts.items():
        matches = exact_occurrences(text, value)
        if matches:
            units.append(unit)
            occurrences.extend((unit, start, end) for start, end in matches)
            if unit in source_units and len(examples) < 2:
                start, end = matches[0]
                examples.append({"unit": unit, "context": short_context(text, start, end)})
    if not occurrences:
        return None
    overlap = len(set(units) & source_units)
    return {
        "rendering": value,
        "relationship": relationship,
        "occurrence_count": len(occurrences),
        "units": sorted(set(units)),
        "source_aligned_unit_count": overlap,
        "source_alignment_share": overlap / len(set(units)) if units else 0.0,
        "representative_contexts": examples,
    }


def _paragraph_aligned_variant_support(
    candidate: str,
    source_forms: list[str],
    source_units: list[dict],
    texts: dict[int, str],
) -> list[dict]:
    """Return evidence only where preserved paragraph alignment is exact.

    Unit-level co-occurrence is too weak for common names and sentence-initial
    English words.  This deliberately declines to infer a spelling variant in
    units whose source and target paragraph counts differ.
    """
    evidence = []
    for unit_index, source in enumerate(source_units, 1):
        source_parts = [part for part in re.split(r"\r?\n\s*\r?\n", source.get("text", "")) if part.strip()]
        target_parts = [part for part in re.split(r"\r?\n\s*\r?\n", texts.get(unit_index, "")) if part.strip()]
        if not source_parts or len(source_parts) != len(target_parts):
            continue
        for paragraph_index, (source_part, target_part) in enumerate(zip(source_parts, target_parts), 1):
            if not any(form in source_part for form in source_forms):
                continue
            matches = exact_occurrences(target_part, candidate)
            if matches:
                start, end = matches[0]
                evidence.append({
                    "unit": unit_index,
                    "paragraph": paragraph_index,
                    "context": short_context(target_part, start, end),
                })
    return evidence


def discover_entity_inventory(
    glossary: dict,
    source_units: list[dict],
    texts: dict[int, str],
    historical_variants: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Inventory glossary-backed identities; fuzzy spelling is evidence only when source-aligned."""
    reverse_english = defaultdict(list)
    english_words_to_sources = defaultdict(set)
    for source_key, entry in glossary.items():
        english = entry.get("english")
        if english:
            root = entry.get("alias_of") or source_key
            reverse_english[ascii_fold(english)].append(root)
            for word in WORD_RE.findall(str(english)):
                if len(word) >= 3:
                    english_words_to_sources[ascii_fold(word)].add(root)

    historical_variants = historical_variants or {}
    records = []
    for source_key, entry in sorted(glossary.items()):
        if entry.get("alias_of") in glossary:
            continue
        category = str(entry.get("category", ""))
        english = str(entry.get("english", "")).strip()
        if category not in IDENTITY_CATEGORIES or not english:
            continue
        source_forms = [source_key]
        if category == "character" and " " in source_key:
            short = source_key.split()[0]
            if any(short in item.get("text", "") for item in source_units):
                source_forms.append(short)
        aliases = [key for key, value in glossary.items() if value.get("alias_of") == source_key]
        source_forms.extend(aliases)
        aligned = {
            index for index, unit in enumerate(source_units, 1)
            if any(form in unit.get("text", "") for form in source_forms)
        }
        if not aligned:
            continue

        variants = []
        grammatical_inflections = []
        known_folded = set()
        for form, relationship in english_identity_forms(english, category):
            record = _variant_record(form, relationship, texts, aligned)
            if record:
                variants.append(record)
                known_folded.add(ascii_fold(form))
            for suffix in ("'s", "’s"):
                inflected = _variant_record(form + suffix, "possessive_inflection", texts, aligned)
                if inflected:
                    grammatical_inflections.append(inflected)
        for alias in entry.get("repair_aliases", []):
            record = _variant_record(str(alias), "known_noncanonical_alias", texts, aligned)
            if record:
                variants.append(record)
                known_folded.add(ascii_fold(str(alias)))
        for form in source_forms:
            for evidence in historical_variants.get(form, []):
                old = str(evidence.get("old_english", "")).strip()
                new = str(evidence.get("new_english", "")).strip()
                if not old or new != english or ascii_fold(old) in known_folded:
                    continue
                record = _variant_record(old, "historical_retroactive_variant", texts, aligned)
                if record:
                    record["evidence_level"] = "audit_history"
                    record["confidence"] = "glossary_supported"
                    record["audit_reason"] = evidence.get("reason")
                    variants.append(record)
                    known_folded.add(ascii_fold(old))

        aligned_texts = [texts[index] for index in sorted(aligned) if index in texts]
        comparison_forms = english_identity_forms(english, category)
        for canonical_form, relationship in comparison_forms:
            canonical_words = WORD_RE.findall(canonical_form)
            if len(canonical_words) != 1:
                continue
            for candidate in sorted(_candidate_spelling_words(canonical_form, aligned_texts)):
                folded = ascii_fold(candidate)
                if folded in known_folded or regular_inflection_of(candidate, canonical_form):
                    continue
                record = _variant_record(candidate, "spelling_variant_candidate", texts, aligned)
                paragraph_evidence = _paragraph_aligned_variant_support(
                    candidate, source_forms, source_units, texts
                )
                if record and record["source_alignment_share"] >= 0.60:
                    record["paragraph_aligned_evidence"] = paragraph_evidence
                    record["evidence_level"] = (
                        "paragraph_aligned" if paragraph_evidence else "unit_only_unaligned"
                    )
                    record["confidence"] = (
                        "source_context_supported" if paragraph_evidence else "ambiguous"
                    )
                    record["competing_source_keys"] = sorted(
                        english_words_to_sources[folded] - {source_key}
                    )
                    variants.append(record)
                    known_folded.add(folded)

        distinct_spellings = [
            item for item in variants
            if item["relationship"] in {
                "known_noncanonical_alias", "spelling_variant_candidate",
                "historical_retroactive_variant",
            }
        ]
        shared = sorted(set(reverse_english[ascii_fold(english)]))
        has_competing_identity = any(item.get("competing_source_keys") for item in distinct_spellings)
        has_unaligned_candidate = any(
            item.get("evidence_level") == "unit_only_unaligned"
            for item in distinct_spellings
        )
        has_historical_evidence = any(
            item.get("evidence_level") == "audit_history"
            for item in distinct_spellings
        )
        has_paragraph_evidence = any(
            item.get("evidence_level") == "paragraph_aligned"
            for item in distinct_spellings
        )
        has_known_alias = any(
            item["relationship"] == "known_noncanonical_alias"
            for item in distinct_spellings
        )
        for variant in variants:
            relationship = variant["relationship"]
            if relationship in {"full_name", "short_name", "identity_core"}:
                variant["classification"] = "NO_CHANGE"
                variant["normalization_recommendation"] = "NO_CHANGE"
            elif variant.get("competing_source_keys") or variant.get("evidence_level") == "unit_only_unaligned":
                variant["classification"] = "AMBIGUOUS"
                variant["normalization_recommendation"] = "NEEDS_REVIEW"
            elif variant.get("evidence_level") == "audit_history" or entry.get("status") == "locked":
                variant["classification"] = "DETERMINISTIC_SAFE"
                variant["normalization_recommendation"] = english
            else:
                variant["classification"] = "CONTEXT_REQUIRED"
                variant["normalization_recommendation"] = english
        if has_competing_identity:
            classification = "AMBIGUOUS"
        elif has_historical_evidence or (
            (has_paragraph_evidence or has_known_alias) and entry.get("status") == "locked"
        ):
            classification = "DETERMINISTIC_SAFE"
        elif has_paragraph_evidence or has_known_alias:
            classification = "CONTEXT_REQUIRED"
        elif has_unaligned_candidate:
            classification = "AMBIGUOUS"
        elif distinct_spellings:
            classification = "DETERMINISTIC_SAFE" if entry.get("status") == "locked" else "CONTEXT_REQUIRED"
        elif len(shared) > 1:
            classification = "NO_CHANGE"
        else:
            classification = "NO_CHANGE"
        confidence = (
            "glossary_locked" if entry.get("status") == "locked"
            else "glossary_supported" if entry.get("status") in {"provisional", "contextual"}
            else "source_context_supported"
        )
        has_strong_noncompeting_evidence = (
            not has_competing_identity
            and (has_historical_evidence or has_paragraph_evidence or has_known_alias)
        )
        proposed = english if has_strong_noncompeting_evidence and confidence in {
            "glossary_locked", "glossary_supported"
        } else None
        form_counts = Counter()
        effective_count = 0
        for unit in source_units:
            text = unit.get("text", "")
            claimed = []
            for form in sorted(set(source_forms), key=lambda value: (-len(value), value)):
                for start, end in exact_occurrences(text, form):
                    if any(start < old_end and end > old_start for old_start, old_end in claimed):
                        continue
                    claimed.append((start, end))
                    form_counts[form] += 1
                    effective_count += 1
        records.append({
            "source_identity": source_identity_core(source_key, category),
            "source_key": source_key,
            "source_forms": sorted(set(source_forms)),
            "category": category,
            "glossary_status": entry.get("status"),
            "glossary_canonical": english,
            "source_occurrence_count": effective_count,
            "source_form_occurrence_counts": dict(sorted(form_counts.items())),
            "source_units": sorted(aligned),
            "english_variants": sorted(variants, key=lambda item: (item["relationship"], ascii_fold(item["rendering"]))),
            "legitimate_grammatical_inflections": sorted(
                grammatical_inflections, key=lambda item: ascii_fold(item["rendering"])
            ),
            "confidence": confidence,
            "proposed_canonical": proposed,
            "normalization_recommendation": proposed or (
                "NO_CHANGE" if classification == "NO_CHANGE" else "NEEDS_REVIEW"
            ),
            "classification": classification,
            "same_english_source_identities": shared if len(shared) > 1 else [],
        })

    shared_groups = [
        {"english": next(glossary[key]["english"] for key in sorted(set(keys))), "source_keys": sorted(set(keys)), "classification": "NO_CHANGE"}
        for keys in reverse_english.values() if len(set(keys)) > 1
    ]
    return records, sorted(shared_groups, key=lambda item: ascii_fold(item["english"]))


def split_title_part(title: str) -> tuple[str, int | None]:
    title = unicodedata.normalize("NFKC", title).strip().strip("<>").strip()
    match = re.search(r"\s*\((\d+)\)\s*$", title)
    part = int(match.group(1)) if match else None
    base = title[:match.start()].strip() if match else title
    return re.sub(r"\s+", " ", base), part


def first_nonblank(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def parse_english_heading(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {"raw": "", "title": None, "kind": "missing"}
    raw = lines[0]
    line = raw
    generator = "GEN_METHOD" in line
    line = re.sub(r"^#{1,6}\s*", "", line)
    if line.startswith("**") and line.endswith("**") and len(line) > 4:
        line = line[2:-2].strip()
    # A few generated headings append an HTML line break directly to the
    # generic chapter label (for example ``Chapter 99<br>``).  Strip only a
    # terminal break element before angle-wrapper cleanup; otherwise
    # ``strip('<>')`` turns it into the bogus title ``br``.
    line = re.sub(r"(?i)\s*<br\s*/?>\s*$", "", line)
    line = line.strip().strip("<>").strip()
    chapter = re.match(r"(?i)^chapter\s+\d+\s*(?:[:\-–—.]\s*)?(.*)$", line)
    if chapter:
        title = chapter.group(1).strip() or None
        kind = "chapter_heading"
    elif re.match(r"(?i)^(epilogue|side story|final)", line):
        title = line
        kind = "special_heading"
    else:
        title = None
        kind = "unrecognized"
    if generator:
        kind = "generator_marker"
        title = None
    return {"raw": raw, "title": title, "kind": kind}


def normalize_english_title_base(title: str) -> str:
    base, _ = split_title_part(title)
    return re.sub(r"[^a-z0-9]+", " ", ascii_fold(base)).strip()


def group_repeated_titles(source_units: list[dict], texts: dict[int, str]) -> list[dict]:
    grouped = defaultdict(list)
    for index, unit in enumerate(source_units, 1):
        base, part = split_title_part(str(unit.get("title", "")))
        grouped[base].append((index, part, unit))
    results = []
    for base, items in sorted(grouped.items()):
        if len(items) < 2:
            continue
        renderings = []
        missing = []
        for index, part, _ in items:
            heading = parse_english_heading(texts.get(index, ""))
            if heading["title"]:
                english_base, english_part = split_title_part(heading["title"])
                renderings.append({
                    "unit": index,
                    "source_part": part,
                    "english_part": english_part,
                    "raw_heading": heading["raw"],
                    "english_base": english_base,
                })
            else:
                missing.append({"unit": index, "raw_heading": heading["raw"], "kind": heading["kind"]})
        counts = Counter(item["english_base"] for item in renderings)
        normalized = defaultdict(list)
        for value in counts:
            normalized[normalize_english_title_base(value)].append(value)
        if missing:
            difference = "missing_title"
        elif len(counts) == 1:
            difference = "consistent"
        elif len(normalized) == 1:
            difference = "punctuation_or_capitalization_only"
        else:
            difference = "lexical_or_semantic"
        top = counts.most_common()
        candidate = top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else None
        deterministic_missing = bool(missing and candidate and len(normalized) == 1)
        classification = (
            "DETERMINISTIC_SAFE" if deterministic_missing
            else "NO_CHANGE" if difference == "consistent"
            else "STRUCTURAL_FORMATTING" if difference == "punctuation_or_capitalization_only"
            else "CONTEXT_REQUIRED"
        )
        results.append({
            "normalized_korean_base_title": base,
            "source_units": [item[0] for item in items],
            "part_numbers": [item[1] for item in items],
            "english_renderings": renderings,
            "english_base_counts": dict(sorted(counts.items())),
            "missing_titles": missing,
            "difference": difference,
            "candidate_canonical_english_base": candidate,
            "deterministic_missing_title_recovery": deterministic_missing,
            "classification": classification,
        })
    return results


ARTIFACT_PATTERNS = {
    "literal_double_asterisk_separator": re.compile(r"(?m)^\s*\*\*\s*$"),
    "spaced_scene_separator": re.compile(r"(?m)^\s*\*\s+\*\s+\*\s*$"),
    "escaped_scene_separator": re.compile(r"(?m)^\s*\\\*\\?\*\\?\*\s*$"),
    "markdown_heading": re.compile(r"(?m)^#{1,6}\s+.+$"),
    "markdown_bold_or_emphasis": re.compile(r"(?m)(?<!\\)(?:\*\*[^\n*]+\*\*|(?<!\*)\*[^\n*]+\*(?!\*))"),
    "generator_marker": re.compile(r"GEN_METHOD[A-Z0-9_]*"),
    "literal_title_wrapper": re.compile(r"(?m)^\s*(?:#{1,6}\s*)?<[^\n<>]+>\s*$"),
    "escaped_numbering": re.compile(r"(?m)^\s*\d+\\\.\s+"),
    "code_fence": re.compile(r"(?m)^\s*```"),
}


def scan_artifacts(texts: dict[int, str]) -> list[dict]:
    results = []
    for name, pattern in ARTIFACT_PATTERNS.items():
        matches = []
        units = set()
        for unit, text in texts.items():
            for match in pattern.finditer(text):
                units.add(unit)
                if len(matches) < 3:
                    matches.append({"unit": unit, "context": short_context(text, match.start(), match.end(), 48)})
        results.append({
            "artifact": name,
            "occurrence_count": sum(len(pattern.findall(text)) for text in texts.values()),
            "affected_unit_count": len(units),
            "affected_units": sorted(units),
            "representative_examples": matches,
            "deterministic_conversion_appears_safe": name in {
                "literal_double_asterisk_separator", "spaced_scene_separator",
                "escaped_scene_separator", "generator_marker", "escaped_numbering",
            },
            "classification": "STRUCTURAL_FORMATTING" if units else "NO_CHANGE",
        })

    double_heading_units = []
    examples = []
    for unit, text in texts.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2 and re.match(r"^#{1,6}\s*Chapter\s+\d+", lines[0], re.I) and re.match(r"^#{1,6}\s+", lines[1]):
            double_heading_units.append(unit)
            if len(examples) < 3:
                examples.append({"unit": unit, "context": " / ".join(lines[:2])[:180]})
    results.append({
        "artifact": "generic_chapter_heading_followed_by_second_heading",
        "occurrence_count": len(double_heading_units),
        "affected_unit_count": len(double_heading_units),
        "affected_units": double_heading_units,
        "representative_examples": examples,
        "deterministic_conversion_appears_safe": True,
        "classification": "STRUCTURAL_FORMATTING" if double_heading_units else "NO_CHANGE",
    })
    return results


def scan_pronoun_candidates(entity_records: list[dict], texts: dict[int, str]) -> list[dict]:
    sets = {
        "masculine": re.compile(r"\b(?:he|him|his|himself)\b", re.I),
        "feminine": re.compile(r"\b(?:she|her|hers|herself)\b", re.I),
        "neutral": re.compile(r"\b(?:they|them|their|theirs|themself|themselves)\b", re.I),
    }
    results = []
    for entity in entity_records:
        if entity["category"] != "character":
            continue
        full_names = [item["rendering"] for item in entity["english_variants"] if item["relationship"] == "full_name"]
        names = full_names or [item["rendering"] for item in entity["english_variants"] if item["relationship"] == "short_name"]
        if len(names) == 1 and len(WORD_RE.findall(names[0])) == 1 and len(names[0]) < 4:
            continue
        if not names:
            continue
        windows = []
        counts = Counter()
        for unit, text in texts.items():
            for name in names:
                for start, end in exact_occurrences(text, name):
                    sentence_start = max(text.rfind(mark, 0, start) for mark in ("\n", ".", "!", "?")) + 1
                    endings = [position for mark in ("\n", ".", "!", "?") if (position := text.find(mark, end)) >= 0]
                    sentence_end = min(endings) + 1 if endings else min(len(text), end + 160)
                    window = text[sentence_start:sentence_end]
                    local = {kind: len(pattern.findall(window)) for kind, pattern in sets.items()}
                    name_matches = exact_occurrences(window, name)
                    tail = window[name_matches[0][1]:] if name_matches else ""
                    after_name = {kind: len(pattern.findall(tail)) for kind, pattern in sets.items()}
                    counts.update(local)
                    windows.append((unit, window, local, after_name, name_matches[0][0] if name_matches else len(window)))
        if not counts:
            continue
        dominant, dominant_count = counts.most_common(1)[0]
        gendered_total = counts["masculine"] + counts["feminine"]
        if dominant not in {"masculine", "feminine"} or dominant_count < 8 or not gendered_total or dominant_count / gendered_total < 0.80:
            continue
        conflicting = "feminine" if dominant == "masculine" else "masculine"
        seen_candidates = set()
        for unit, paragraph, local, after_name, name_offset in windows:
            # This is deliberately a candidate-only scan.  Requiring repeated
            # minority pronouns with no dominant-set pronoun in the same
            # sentence avoids reporting ordinary sentences that merely mention
            # a second character ("Name helped her").
            if name_offset <= 24 and after_name[conflicting] >= 2 and after_name[dominant] == 0:
                key = (unit, re.sub(r"\s+", " ", paragraph).strip())
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                results.append({
                    "unit": unit,
                    "character": entity["glossary_canonical"],
                    "source_identity": entity["source_identity"],
                    "local_conflicting_pronoun_set": conflicting,
                    "local_conflicting_count": local[conflicting],
                    "dominant_surrounding_pronoun_set": dominant,
                    "dominant_counts": dict(counts),
                    "confidence": "source_context_supported",
                    "context": re.sub(r"\s+", " ", paragraph).strip()[:220],
                    "classification": "CONTEXT_REQUIRED",
                })
    return results


def scan_residual_and_leakage(texts: dict[int, str]) -> dict:
    signatures = {
        "hangul": HANGUL_RE,
        "han_cjk": CJK_RE,
        "replacement_character": re.compile("�"),
        "code_fence": re.compile(r"(?m)^\s*```"),
        "generator_marker": re.compile(r"GEN_METHOD[A-Z0-9_]*"),
        "model_control_prompt_leakage": re.compile(r"(?i)(?:system prompt|assistant to=|<\|assistant\|>|deepseek|you are chatgpt)"),
        "json_or_tool_output": re.compile(r"(?m)^\s*\{\s*\"(?:tool|choices|usage|new_terms|continuity_summary)\"\s*:"),
    }
    result = {}
    for name, pattern in signatures.items():
        count = 0
        units = set()
        examples = []
        for unit, text in texts.items():
            found = list(pattern.finditer(text))
            if found:
                count += len(found)
                units.add(unit)
                for match in found[:max(0, 3 - len(examples))]:
                    examples.append({"unit": unit, "context": short_context(text, match.start(), match.end(), 45)})
        result[name] = {
            "occurrence_count": count,
            "affected_unit_count": len(units),
            "affected_units": sorted(units),
            "representative_examples": examples,
            "classification": "CONTEXT_REQUIRED" if count else "NO_CHANGE",
        }
    return result


def _robust_outliers(rows: list[dict], key: str) -> tuple[list[dict], float]:
    values = [row[key] for row in rows if row[key] > 0]
    median = statistics.median(values)
    logs = [math.log(value) for value in values]
    median_log = statistics.median(logs)
    mad = statistics.median(abs(value - median_log) for value in logs)
    found = []
    for row in rows:
        value = row[key]
        relative = value / median if median else 0
        distance = abs(math.log(value) - median_log) / mad if value > 0 and mad else 0
        if value > 0 and (relative < 0.5 or relative > 2.0 or (distance > 6 and (relative < 0.67 or relative > 1.5))):
            found.append({"unit": row["unit"], "value": value, "relative_to_median": relative, "robust_log_distance": distance})
    return found, median


def structural_inventory(source_units: list[dict], texts: dict[int, str]) -> dict:
    rows = []
    for unit, source in enumerate(source_units, 1):
        target = texts[unit]
        source_chars = int(source.get("source_character_count", len(source.get("text", ""))))
        source_paragraphs = int(source.get("paragraph_count", 0))
        target_paragraphs = len([item for item in re.split(r"\r?\n\s*\r?\n", target) if item.strip()])
        rows.append({
            "unit": unit,
            "source_character_count": source_chars,
            "english_character_count": len(target),
            "character_ratio": len(target) / source_chars if source_chars else 0,
            "source_paragraph_count": source_paragraphs,
            "english_paragraph_count": target_paragraphs,
            "paragraph_ratio": target_paragraphs / source_paragraphs if source_paragraphs else 0,
        })
    char_outliers, median_char = _robust_outliers(rows, "character_ratio")
    paragraph_outliers, median_paragraph = _robust_outliers(rows, "paragraph_ratio")
    unit268 = rows[267] if len(rows) >= 268 else None
    if unit268:
        char_normal = 0.67 <= unit268["character_ratio"] / median_char <= 1.5
        paragraph_low = unit268["paragraph_ratio"] / median_paragraph < 0.75
        assessment = "normal_paragraph_consolidation" if char_normal and paragraph_low else "inconclusive"
    else:
        assessment = "not_available"
    return {
        "unit_metrics": rows,
        "median_character_ratio": median_char,
        "median_paragraph_ratio": median_paragraph,
        "major_character_ratio_outliers": char_outliers,
        "major_paragraph_ratio_outliers": paragraph_outliers,
        "unit_268": {**(unit268 or {}), "assessment": assessment},
    }


def classification_summary(inventory: dict) -> dict:
    counts = Counter()
    for item in inventory["entities"]["identities"]:
        counts[item["classification"]] += 1
    for item in inventory["entities"]["shared_english_distinct_sources"]:
        counts[item["classification"]] += 1
    for item in inventory["titles"]["repeated_groups"]:
        counts[item["classification"]] += 1
    for item in inventory["artifacts"]:
        counts[item["classification"]] += 1
    for item in inventory["pronoun_candidates"]:
        counts[item["classification"]] += 1
    for item in inventory["residual_and_leakage"].values():
        counts[item["classification"]] += 1
    structural = inventory["structural"]
    counts["CONTEXT_REQUIRED"] += len(structural["major_character_ratio_outliers"])
    counts["STRUCTURAL_FORMATTING"] += len(structural["major_paragraph_ratio_outliers"])
    return {name: counts[name] for name in CLASSIFICATIONS}


def render_markdown(inventory: dict) -> str:
    lines = [
        "# Rogue First Prince normalization inventory",
        "",
        "Read-only, zero-API evidence inventory. No corrections have been applied.",
        "",
        "## Summary",
        "",
        f"- Source units: {inventory['corpus']['source_unit_count']}",
        f"- Final English units: {inventory['corpus']['final_unit_count']}",
        f"- Glossary-backed identities inventoried: {len(inventory['entities']['identities'])}",
        f"- Repeated source-title groups: {len(inventory['titles']['repeated_groups'])}",
        f"- Pronoun candidates: {len(inventory['pronoun_candidates'])}",
        "",
        "## Classification totals",
        "",
    ]
    for name, count in inventory["classification_counts"].items():
        lines.append(f"- {name}: {count}")
    lines += ["", "## Entity/name consistency", ""]
    for item in inventory["entities"]["identities"]:
        if item["classification"] == "NO_CHANGE" and not item["same_english_source_identities"]:
            continue
        variants = ", ".join(f"{v['rendering']} ({v['occurrence_count']})" for v in item["english_variants"])
        proposal = item["proposed_canonical"] or "NEEDS_REVIEW"
        lines.append(f"- `{item['source_key']}` → {variants or 'no English occurrence'}; proposed: {proposal}; {item['classification']}")
    lines += ["", "## Repeated chapter titles", ""]
    for item in inventory["titles"]["repeated_groups"]:
        if item["difference"] != "consistent":
            lines.append(f"- `{item['normalized_korean_base_title']}` units {item['source_units']}: {item['difference']}; proposed: {item['candidate_canonical_english_base'] or 'NEEDS_REVIEW'}")
    lines += ["", "## Publication artifacts", ""]
    for item in inventory["artifacts"]:
        lines.append(f"- {item['artifact']}: {item['occurrence_count']} occurrences in {item['affected_unit_count']} units")
    lines += ["", "## Pronoun candidates", ""]
    if inventory["pronoun_candidates"]:
        for item in inventory["pronoun_candidates"]:
            lines.append(f"- Unit {item['unit']}, {item['character']}: local {item['local_conflicting_pronoun_set']} vs dominant {item['dominant_surrounding_pronoun_set']} — {item['context']}")
    else:
        lines.append("- None met the conservative reporting threshold.")
    lines += ["", "## Residual/corruption/leakage", ""]
    for name, item in inventory["residual_and_leakage"].items():
        lines.append(f"- {name}: {item['occurrence_count']} occurrences in {item['affected_unit_count']} units")
    unit268 = inventory["structural"]["unit_268"]
    lines += [
        "", "## Structural outliers", "",
        f"- Major character-ratio outliers: {len(inventory['structural']['major_character_ratio_outliers'])}",
        f"- Major paragraph-ratio outliers: {len(inventory['structural']['major_paragraph_ratio_outliers'])}",
        f"- Final 268: character ratio {unit268.get('character_ratio', 0):.4f}; paragraph ratio {unit268.get('paragraph_ratio', 0):.4f}; assessment `{unit268.get('assessment')}`.",
        "", "The JSON report is authoritative and contains complete unit lists, counts, and short evidence snippets.", "",
    ]
    return "\n".join(lines)


def build_inventory(project) -> dict:
    final_root = project.root / "final"
    manifest = json.loads((final_root / "manifest.json").read_text(encoding="utf-8"))
    source_result = load_corrected_txt_spine(project)
    source_units = source_result["chapters"]
    chapter_paths = sorted((final_root / "chapters").glob("[0-9][0-9][0-9][0-9].md"))
    texts = {int(path.stem): read_text_exact(path) for path in chapter_paths}
    if len(source_units) != 351 or sorted(texts) != list(range(1, 352)):
        raise ValueError("normalization inventory requires the verified 351-unit final corpus")
    glossary_document = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    glossary = glossary_document.get("entries", glossary_document)
    if not isinstance(glossary, dict):
        raise ValueError("glossary entries must be an object")
    audit_document = json.loads(project.audit_history_file.read_text(encoding="utf-8"))
    historical_variants = defaultdict(list)
    def collect_historical(value):
        if isinstance(value, dict):
            if {"chinese", "old_english", "new_english"} <= value.keys():
                historical_variants[str(value["chinese"])].append(value)
            for child in value.values():
                collect_historical(child)
        elif isinstance(value, list):
            for child in value:
                collect_historical(child)
    collect_historical(audit_document)
    entities, shared = discover_entity_inventory(
        glossary, source_units, texts, historical_variants
    )
    heading_inventory = []
    for unit, text in texts.items():
        parsed = parse_english_heading(text)
        heading_inventory.append({"unit": unit, **parsed})
    inventory = {
        "schema_version": 1,
        "audit_mode": "read-only-zero-api",
        "corpus": {
            "project_id": project.config.id,
            "source_unit_count": len(source_units),
            "final_unit_count": len(texts),
            "source_sha256": source_result["report"]["source_sha256"],
            "final_manifest_sha256": sha256_bytes((final_root / "manifest.json").read_bytes()),
            "final_aggregate_sha256": manifest["final"]["aggregate_byte_stream_sha256"],
        },
        "entities": {"identities": entities, "shared_english_distinct_sources": shared},
        "titles": {
            "repeated_groups": group_repeated_titles(source_units, texts),
            "heading_inventory": heading_inventory,
            "nonstandard_or_missing_units": [
                item for item in heading_inventory
                if item["kind"] in {"missing", "unrecognized", "generator_marker"}
            ],
        },
        "artifacts": scan_artifacts(texts),
        "pronoun_candidates": scan_pronoun_candidates(entities, texts),
        "residual_and_leakage": scan_residual_and_leakage(texts),
        "structural": structural_inventory(source_units, texts),
    }
    inventory["classification_counts"] = classification_summary(inventory)
    return inventory


def write_inventory(project, inventory: dict) -> tuple[Path, Path]:
    reports = project.root / "final" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "normalization_inventory.json"
    markdown_path = reports / "normalization_inventory.md"
    json_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(inventory), encoding="utf-8")
    return json_path, markdown_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a read-only final-corpus normalization inventory")
    parser.add_argument("--novel", required=True)
    args = parser.parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            inventory = build_inventory(project)
            json_path, markdown_path = write_inventory(project, inventory)
    except ProductionRunLockedError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Source units: {inventory['corpus']['source_unit_count']}")
    print(f"Final units: {inventory['corpus']['final_unit_count']}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print("API calls used: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
