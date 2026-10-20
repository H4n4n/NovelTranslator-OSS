"""Import-safe, bounded residual-CJK micro-repair orchestration.

This module has no credentials, API client, filesystem paths, or import-time
I/O. Production supplies small callbacks for its established API, atomic
pending writes, progress accounting, and mandatory local QA.
"""

from copy import deepcopy

from production_auxiliary_paid_request import (invoke_request, planned_attempt, resumable_attempt, failure_values, success_values)
import json
import re

from chinese_script_normalization import (
    TRADITIONAL_TO_SIMPLIFIED,
    traditional_to_simplified,
    traditional_to_simplified_backend,
)
from noveltranslator_core import (
    CJK_FRAGMENT_RE,
    clean_json_text,
    pending_meta_after_residual_cjk_repair,
    pending_meta_after_residual_parenthetical_cleanup,
    qa_paragraph_segment_spans,
    residual_source_script_report,
    stable_text_hash,
)


ATTEMPT_STATE_KEY = "residual_cjk_micro_repair"
REPAIR_EVENT_TYPE = "residual_cjk_micro_repair"
# Increment deliberately whenever the paid repair prompt/contract changes in a
# way that justifies one new attempt for an unchanged pending translation.
MICRO_REPAIR_POLICY_VERSION = "target-language-only-v5"
REPEATED_ANCHOR_ASSIGNMENT_POLICY_VERSION = "repeated-anchor-ordered-v1"
PARENTHETICAL_CLEANUP_POLICY_VERSION = "redundant-parenthetical-v2"
LEGACY_MICRO_REPAIR_POLICY_VERSION = "legacy-unversioned-v1"
MAX_REPAIR_OLD_TEXT_CHARS = 180
MAX_REPAIR_NEW_TEXT_CHARS = 220
MAX_TOTAL_PATCH_CHARS = 600
MAX_AFFECTED_LINE_CHARS = 1000
MAX_INSERTED_CHARS_PER_REPAIR = 64
MIN_TARGET_LETTERS_IN_TRANSLATION = 40
MIN_TARGET_LETTERS_PER_AFFECTED_LINE = 8
MAX_AFFECTED_LINE_CJK_RATIO = 0.20
MAX_FRAGMENT_JOIN_GAP = 160
MAX_REPAIR_REGION_SPAN = 480
REPAIR_REGION_CONTEXT_CHARS = 100
MAX_SOURCE_ANCHOR_SPAN = 500
MAX_ALIGNMENT_SIGNATURE_RADIUS = 6
MAX_DIAGNOSTIC_RESPONSE_CHARS = 2000
MAX_PARENTHETICAL_ANNOTATION_CHARS = 32
HYBRID_LEXICAL_UNIT_REVALIDATION_POLICY_VERSION = "hybrid-lexical-unit-v1"
MAX_HYBRID_CHANGED_SPAN_CHARS = 48
MAX_HYBRID_ADJACENT_ENGLISH_TOKENS = 1
MAX_HYBRID_SOURCE_COMPOUND_CHARS = 16
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)?")
HYBRID_ENGLISH_TOKEN_RE = re.compile(
    r"[A-Za-z]+(?:[-'\u2019][A-Za-z]+)*"
)
HYBRID_PUNCTUATION_BOUNDARIES = frozenset(
    ",;:.!?\uff0c\uff1b\uff1a\u3002\uff01\uff1f"
    "\"'\u2018\u2019\u201c\u201d()[]{}\uff08\uff09\u2013\u2014"
)
HYBRID_HIGH_RISK_RE = re.compile(
    r"(?:https?://|www\.|\d|[%=_@#$\\/`])", re.IGNORECASE
)
UNAFFECTED_ENGLISH_REWRITE_ERROR_RE = re.compile(
    r"repairs\[(?P<index>\d+)\]\.new_text rewrites unaffected English words"
)
PARENTHETICAL_ANNOTATION_RE = re.compile(
    r"\((?P<ascii_body>[^()\r\n]{1,32})\)"
    r"|\uff08(?P<wide_body>[^\uff08\uff09\r\n]{1,32})\uff09"
)
ANNOTATION_INTERNAL_PUNCTUATION = frozenset(
    " \t\u00a0\u00b7\u30fb\u3001,\uff0c/\uff0f-\u2010\u2011\u2013\u2014:"
    "\uff1a;\uff1b'\u2019\"\u201c\u201d!?\uff01\uff1f"
)
UNSAFE_ANNOTATION_INTRO_RE = re.compile(
    r"\b(?:is|was|are|were|be|been|being|called|named|known\s+as|means?|"
    r"reads?|says?|said|shouts?|shouted|cries|cried|yells?|yelled|utters?|"
    r"uttered|word|words|name|term|phrase|inscription|written|labelled|"
    r"labeled)\b[^.!?]{0,48}$",
    re.IGNORECASE,
)
ANNOTATION_EVIDENCE_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by",
    "for", "from", "he", "her", "his", "i", "in", "is", "it", "its",
    "my", "of", "on", "or", "our", "she", "that", "the", "their",
    "them", "they", "this", "to", "was", "we", "were", "with", "you",
    "your",
})
STYLIZED_TERM_WRAPPER_CHARACTERS = frozenset(
    " \t\"'\u2018\u2019\u201c\u201d\u300c\u300d\u300e\u300f([{"
)
UNSAFE_STANDALONE_TERM_WORDS = frozenset({
    "art", "inscription", "label", "name", "phrase", "poem", "skill",
    "spell", "technique", "term", "title", "word",
})


def _setting(config, name):
    if isinstance(config, dict):
        return config[name]
    return getattr(config, name)


def _annotation_contains_only_residual_script(
    body,
    source_language,
    approved_reader_facing_terms,
):
    report = residual_source_script_report(
        body, source_language, approved_reader_facing_terms
    )
    occurrences = report["residual_cjk_occurrences"]
    if not occurrences:
        return None
    covered = set()
    for occurrence in occurrences:
        covered.update(range(occurrence["start"], occurrence["end"]))
    for index, character in enumerate(body):
        if index in covered:
            continue
        if character.isspace() or character in ANNOTATION_INTERNAL_PUNCTUATION:
            continue
        return None
    return report


def _is_stylized_standalone_target_term(evidence, trailing_gap):
    """Accept one title-like English token only in a tightly wrapped phrase."""
    if len(trailing_gap) > 1:
        return False
    matches = list(ASCII_WORD_RE.finditer(evidence))
    if len(matches) != 1:
        return False
    match = matches[0]
    word = match.group(0)
    letters = [character for character in word if character.isalpha()]
    if (
        len(letters) < 3
        or not word[0].isupper()
        or word.casefold() in ANNOTATION_EVIDENCE_STOPWORDS
        or word.casefold() in UNSAFE_STANDALONE_TERM_WORDS
    ):
        return False
    outside_word = evidence[:match.start()] + evidence[match.end():]
    return all(
        character in STYLIZED_TERM_WRAPPER_CHARACTERS
        for character in outside_word
    )


def _has_preceding_target_rendering(text, annotation_start, target_language):
    """Require a nearby English phrase and reject label/name introducers."""
    if str(target_language).casefold() != "en":
        return False
    line_start = text.rfind("\n", 0, annotation_start) + 1
    raw_prefix = text[line_start:annotation_start]
    prefix = raw_prefix.rstrip(" \t")
    trailing_gap = raw_prefix[len(prefix):]
    if not prefix:
        return False
    evidence = prefix[-160:]
    sentence_probe = evidence.rstrip("\"'\u2019\u201d")
    if sentence_probe.endswith(("!", "?")):
        sentence_probe = sentence_probe[:-1].rstrip()
    prior_boundary = max(
        sentence_probe.rfind("."),
        sentence_probe.rfind("!"),
        sentence_probe.rfind("?"),
    )
    if prior_boundary >= 0:
        evidence = sentence_probe[prior_boundary + 1:]
    words = ASCII_WORD_RE.findall(evidence)
    meaningful_words = [
        word for word in words
        if word.casefold() not in ANNOTATION_EVIDENCE_STOPWORDS
    ]
    if (
        len(words) < 2 or len(meaningful_words) < 2
    ) and not _is_stylized_standalone_target_term(evidence, trailing_gap):
        return False
    punctuation_tail = evidence.rstrip("\"'\u2019\u201d")
    if punctuation_tail.endswith((".", ":", "=", "(")):
        return False
    if UNSAFE_ANNOTATION_INTRO_RE.search(evidence):
        return False
    return True


def deterministic_parenthetical_residual_cleanup(
    translation,
    source_language="zh",
    target_language="en",
    approved_reader_facing_terms=(),
    source_text=None,
):
    """Remove only compact source-script annotations after existing English."""
    original = translation or ""
    before = residual_source_script_report(
        original,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    intentional_ranges = [
        (item["start"], item["end"])
        for item in before["intentional_reader_facing_occurrences"]
    ]
    removals = []
    for match in PARENTHETICAL_ANNOTATION_RE.finditer(original):
        if any(
            match.start() < intentional_end and intentional_start < match.end()
            for intentional_start, intentional_end in intentional_ranges
        ):
            continue
        body = match.group("ascii_body") or match.group("wide_body") or ""
        if len(body) > MAX_PARENTHETICAL_ANNOTATION_CHARS:
            continue
        if (
            (match.start() and original[match.start() - 1] in "(\uff08")
            or (
                match.end() < len(original)
                and original[match.end()] in ")\uff09"
            )
        ):
            continue
        annotation_report = _annotation_contains_only_residual_script(
            body,
            source_language,
            approved_reader_facing_terms,
        )
        if annotation_report is None:
            continue
        if not _has_preceding_target_rendering(
            original, match.start(), target_language
        ):
            continue
        if match.end() < len(original) and original[match.end()].isalnum():
            continue

        line_start = original.rfind("\n", 0, match.start()) + 1
        removal_start = match.start()
        while (
            removal_start > line_start
            and original[removal_start - 1] in " \t"
        ):
            removal_start -= 1
        if removal_start > line_start and original[removal_start - 1] in "\u2013\u2014":
            removal_start -= 1
            while (
                removal_start > line_start
                and original[removal_start - 1] in " \t"
            ):
                removal_start -= 1
        removal_end = match.end()
        prefix = original[line_start:removal_start].rstrip(" \t")
        if (
            prefix
            and removal_end < len(original)
            and original[removal_end] in "!?"
            and prefix.rstrip("\"'\u2019\u201d").endswith(original[removal_end])
        ):
            removal_end += 1
        removals.append({
            "start": removal_start,
            "end": removal_end,
            "old_text": original[removal_start:removal_end],
            "new_text": "",
            "annotation": match.group(0),
            "residual_fragments": annotation_report["residual_cjk_fragments"],
            "line_number": original.count("\n", 0, match.start()) + 1,
            "repair_type": "deterministic_residual_parenthetical_cleanup",
            "policy_version": PARENTHETICAL_CLEANUP_POLICY_VERSION,
        })

    cleaned = original
    for removal in reversed(removals):
        cleaned = (
            cleaned[:removal["start"]]
            + removal["new_text"]
            + cleaned[removal["end"]:]
        )
    after = residual_source_script_report(
        cleaned,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    if after["remaining_cjk_character_count"] > before[
        "remaining_cjk_character_count"
    ]:
        return {
            "changed": False,
            "cleaned_text": original,
            "removals": [],
            "before": before,
            "after": before,
            "policy_version": PARENTHETICAL_CLEANUP_POLICY_VERSION,
        }
    public_removals = [
        {key: value for key, value in item.items() if key not in {"start", "end"}}
        for item in removals
    ]
    return {
        "changed": bool(removals),
        "cleaned_text": cleaned,
        "removals": public_removals,
        "before": before,
        "after": after,
        "policy_version": PARENTHETICAL_CLEANUP_POLICY_VERSION,
    }


def _translation_repair_regions(translation, occurrences):
    """Group nearby same-line fragments without merging distant locations."""
    groups = []
    for occurrence in sorted(occurrences, key=lambda item: item["start"]):
        if not groups:
            groups.append([occurrence])
            continue
        previous = groups[-1][-1]
        proposed_span = occurrence["end"] - groups[-1][0]["start"]
        if (
            occurrence["line_number"] == previous["line_number"]
            and occurrence["start"] - previous["end"] <= MAX_FRAGMENT_JOIN_GAP
            and proposed_span <= MAX_REPAIR_REGION_SPAN
        ):
            groups[-1].append(occurrence)
        else:
            groups.append([occurrence])

    regions = []
    for index, group in enumerate(groups, start=1):
        first = group[0]
        last = group[-1]
        scope_start = max(
            first["line_start"], first["start"] - REPAIR_REGION_CONTEXT_CHARS
        )
        scope_end = min(
            first["line_end"], last["end"] + REPAIR_REGION_CONTEXT_CHARS
        )
        regions.append({
            "region_id": f"line-{first['line_number']}-region-{index}",
            "line_number": first["line_number"],
            "fragment_start": first["start"],
            "fragment_end": last["end"],
            "scope_start": scope_start,
            "scope_end": scope_end,
            "affected_text": (translation or "")[scope_start:scope_end],
            "residual_fragments": [item["fragment"] for item in group],
        })

    # Keep scopes on one line disjoint so a response anchor belongs to at most
    # one repair region even when two raw context windows would overlap.
    for left, right in zip(regions, regions[1:]):
        if left["line_number"] != right["line_number"]:
            continue
        boundary = (left["fragment_end"] + right["fragment_start"]) // 2
        left["scope_end"] = min(left["scope_end"], boundary)
        right["scope_start"] = max(right["scope_start"], boundary)
        left["affected_text"] = (translation or "")[
            left["scope_start"]:left["scope_end"]
        ]
        right["affected_text"] = (translation or "")[
            right["scope_start"]:right["scope_end"]
        ]
    return regions


def _ordered_source_anchor(source_text, fragments):
    """Find a compact source span containing the fragments in translation order."""
    source = source_text or ""
    if not fragments:
        return None
    first = fragments[0]
    search_from = 0
    while True:
        start = source.find(first, search_from)
        if start < 0:
            return None
        cursor = start + len(first)
        end = cursor
        matched = True
        for fragment in fragments[1:]:
            position = source.find(fragment, cursor)
            if position < 0 or position + len(fragment) - start > MAX_SOURCE_ANCHOR_SPAN:
                matched = False
                break
            cursor = position + len(fragment)
            end = cursor
        if matched and end - start <= MAX_SOURCE_ANCHOR_SPAN:
            context_start = max(0, start - 120)
            context_end = min(len(source), end + 120)
            return {
                "start": start,
                "end": end,
                "context": source[context_start:context_end],
            }
        search_from = start + len(first)


def _is_supported_chinese_source_language(source_language):
    language = (source_language or "").strip().casefold()
    return language == "zh" or language.startswith("zh-")


def _normalized_fragment_source_candidates(source, fragment, normalizer):
    """Return same-length raw source spans equivalent to one CJK fragment."""
    normalized = normalizer(fragment)
    if normalized is None:
        return {"status": "normalization_unavailable", "candidates": []}
    if not normalized:
        return {"status": "normalized_fragment_empty", "candidates": []}
    if len(normalized) != len(fragment):
        return {"status": "normalized_source_anchor_offset_unsafe", "candidates": []}

    candidates = []
    span_length = len(fragment)
    for start in range(0, max(0, len(source) - span_length + 1)):
        end = start + span_length
        raw = source[start:end]
        if CJK_FRAGMENT_RE.fullmatch(raw) is None:
            continue
        candidate_normalized = normalizer(raw)
        if candidate_normalized is None:
            continue
        # Raw Python offsets remain authoritative.  A backend result whose
        # code-point length changes cannot safely reuse those offsets.
        if len(candidate_normalized) != len(raw):
            continue
        if candidate_normalized == normalized:
            candidates.append({
                "start": start,
                "end": end,
                "residual_raw": fragment,
                "source_raw": raw,
                "normalized_anchor": normalized,
            })
    return {
        "status": "candidates_found" if candidates else "not_found",
        "normalized_fragment": normalized,
        "candidates": candidates,
    }


def normalized_source_anchor_match(
    source_text,
    fragments,
    source_language="zh",
    normalizer=None,
):
    """Find one offset-safe t2s-equivalent compact source anchor.

    Exact raw ordered anchors deliberately take priority.  Normalization is a
    source-location fallback only and never changes final target-text QA.
    """
    source = source_text or ""
    raw_fragments = list(fragments or ())
    result = {
        "status": "not_found",
        "reason_code": "normalized_source_anchor_not_found",
        "normalization": TRADITIONAL_TO_SIMPLIFIED,
        "normalization_backend": traditional_to_simplified_backend(),
    }
    if not _is_supported_chinese_source_language(source_language):
        return {
            **result,
            "status": "disabled",
            "reason_code": "normalized_source_anchor_language_unsupported",
        }
    if not raw_fragments or any(
        not fragment or CJK_FRAGMENT_RE.fullmatch(fragment) is None
        for fragment in raw_fragments
    ):
        return {
            **result,
            "status": "disabled",
            "reason_code": "normalized_source_anchor_fragment_ineligible",
        }
    if _ordered_source_anchor(source, raw_fragments) is not None:
        return {
            **result,
            "status": "exact_source_anchor_available",
            "reason_code": "exact_source_anchor_available",
        }

    normalize = normalizer or traditional_to_simplified
    per_fragment = []
    for fragment in raw_fragments:
        candidate_result = _normalized_fragment_source_candidates(
            source, fragment, normalize
        )
        if candidate_result["status"] != "candidates_found":
            reason_code = (
                "normalized_source_anchor_not_found"
                if candidate_result["status"] == "not_found"
                else candidate_result["status"]
            )
            return {
                **result,
                "status": candidate_result["status"],
                "reason_code": reason_code,
            }
        per_fragment.append(candidate_result["candidates"])

    combinations = []

    def extend(index, chosen):
        if len(combinations) > 1:
            return
        if index == len(per_fragment):
            if any(
                match["source_raw"] != match["residual_raw"]
                for match in chosen
            ):
                combinations.append(list(chosen))
            return
        for candidate in per_fragment[index]:
            if len(combinations) > 1:
                return
            if chosen:
                if candidate["start"] < chosen[-1]["end"]:
                    continue
                if candidate["end"] - chosen[0]["start"] > MAX_SOURCE_ANCHOR_SPAN:
                    continue
            extend(index + 1, [*chosen, candidate])

    extend(0, [])
    unique = {}
    for combination in combinations:
        identity = tuple(
            (item["start"], item["end"], item["source_raw"])
            for item in combination
        )
        unique[identity] = combination
    combinations = list(unique.values())
    if not combinations:
        return result
    if len(combinations) != 1:
        return {
            **result,
            "status": "ambiguous",
            "reason_code": "normalized_source_anchor_ambiguous",
            "candidate_count": len(combinations),
        }

    matches = combinations[0]
    start = matches[0]["start"]
    end = matches[-1]["end"]
    context_start = max(0, start - 120)
    context_end = min(len(source), end + 120)
    provenance = {
        "anchor_origin": "script_normalized_source_anchor",
        "normalization": TRADITIONAL_TO_SIMPLIFIED,
        "normalization_backend": traditional_to_simplified_backend(),
        "residual_raw": (
            matches[0]["residual_raw"]
            if len(matches) == 1
            else [item["residual_raw"] for item in matches]
        ),
        "source_raw": (
            matches[0]["source_raw"]
            if len(matches) == 1
            else [item["source_raw"] for item in matches]
        ),
        "normalized_anchor": (
            matches[0]["normalized_anchor"]
            if len(matches) == 1
            else [item["normalized_anchor"] for item in matches]
        ),
        "source_start": start,
        "source_end": end,
        "matches": matches,
    }
    return {
        "status": "matched",
        "reason_code": "",
        "start": start,
        "end": end,
        "context": source[context_start:context_end],
        "provenance": provenance,
    }


def _segment_structure_kind(segment_text):
    """Return a language-independent structural signature for one QA segment."""
    text = (segment_text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "empty"
    if lines[0].startswith("#") or (
        len(lines) >= 2 and re.fullmatch(r"={3,}", lines[-1])
    ):
        return "heading"
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"(?:\*{2,}|-{3,}|~{3,})", compact):
        return "divider"
    quote_pairs = (("\"", "\""), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
    if any(text.startswith(left) and text.endswith(right) for left, right in quote_pairs):
        return "dialogue"
    if text.startswith("[") and text.endswith("]"):
        return "bracketed"
    return "prose"


def _unique_local_structure_alignment(source_kinds, target_kinds, index):
    """Return a bounded unique matching window radius, or None if ambiguous."""
    if len(source_kinds) != len(target_kinds) or not source_kinds:
        return None
    if len(source_kinds) == 1:
        return 0 if source_kinds[0] == target_kinds[0] else None
    for radius in range(1, MAX_ALIGNMENT_SIGNATURE_RADIUS + 1):
        # Multi-segment alignment needs structural evidence on both sides; a
        # window is not considered unique merely because it consumes the whole
        # short chapter or touches one of its boundaries.
        if index - radius <= 0 or index + radius >= len(source_kinds) - 1:
            continue
        source_window = source_kinds[index - radius:index + radius + 1]
        target_window = target_kinds[index - radius:index + radius + 1]
        if source_window != target_window or len(set(source_window)) < 2:
            continue
        source_matches = [
            candidate
            for candidate in range(radius, len(source_kinds) - radius)
            if source_kinds[candidate - radius:candidate + radius + 1]
            == source_window
        ]
        target_matches = [
            candidate
            for candidate in range(radius, len(target_kinds) - radius)
            if target_kinds[candidate - radius:candidate + radius + 1]
            == target_window
        ]
        if source_matches == [index] and target_matches == [index]:
            return radius
    return None


def _aligned_source_segment(source_text, translation, region):
    """Return one conservatively aligned source paragraph for a target region."""
    source_segments = qa_paragraph_segment_spans(source_text)
    target_segments = qa_paragraph_segment_spans(translation)
    if not source_segments or len(source_segments) != len(target_segments):
        return None
    target_matches = [
        (index, segment)
        for index, segment in enumerate(target_segments)
        if segment["start"] <= region["scope_start"]
        and region["scope_end"] <= segment["end"]
    ]
    if len(target_matches) != 1:
        return None
    index, target_segment = target_matches[0]
    source_segment = source_segments[index]
    if (
        not source_segment["text"].strip()
        or len(source_segment["text"]) > MAX_SOURCE_ANCHOR_SPAN
        or len(target_segment["text"]) > MAX_AFFECTED_LINE_CHARS
    ):
        return None
    source_kinds = tuple(
        _segment_structure_kind(segment["text"]) for segment in source_segments
    )
    target_kinds = tuple(
        _segment_structure_kind(segment["text"]) for segment in target_segments
    )
    radius = _unique_local_structure_alignment(source_kinds, target_kinds, index)
    if radius is None:
        return None
    return {
        "source_anchor": {
            "start": source_segment["start"],
            "end": source_segment["end"],
            "context": source_segment["text"],
        },
        "target_segment": {
            "start": target_segment["start"],
            "end": target_segment["end"],
            "context": target_segment["text"],
        },
        "source_segment_index": index,
        "target_segment_index": index,
        "structure_window_radius": radius,
    }


def _corresponding_source_segment_bounds(source_text, translation, region):
    """Return same-index paragraph bounds as repeated-anchor evidence only.

    Unlike ``_aligned_source_segment``, this is not a standalone locator.  It
    may only constrain a chapter-wide repeated-anchor assignment whose target
    and source occurrence identities are also proven together.
    """
    source_segments = qa_paragraph_segment_spans(source_text)
    target_segments = qa_paragraph_segment_spans(translation)
    if not source_segments or len(source_segments) != len(target_segments):
        return None
    target_matches = [
        (index, segment)
        for index, segment in enumerate(target_segments)
        if segment["start"] <= region["scope_start"]
        and region["scope_end"] <= segment["end"]
    ]
    if len(target_matches) != 1:
        return None
    index, target_segment = target_matches[0]
    source_segment = source_segments[index]
    if (
        not source_segment["text"].strip()
        or len(source_segment["text"]) > MAX_SOURCE_ANCHOR_SPAN
        or len(target_segment["text"]) > MAX_AFFECTED_LINE_CHARS
    ):
        return None
    return {
        "start": source_segment["start"],
        "end": source_segment["end"],
        "source_segment_index": index,
        "target_segment_index": index,
    }


def _region_occurrences(region, occurrences):
    return [
        occurrence for occurrence in occurrences
        if region["fragment_start"] <= occurrence["start"]
        and occurrence["end"] <= region["fragment_end"]
    ]


def _enumerate_monotonic_assignments(entries, candidates, choices):
    """Return at most two strict, injective, non-crossing assignments."""
    solutions = []

    def extend(index, previous_source_index, chosen):
        if len(solutions) > 1:
            return
        if index == len(entries):
            solutions.append(tuple(chosen))
            return
        for source_index in choices[index]:
            if source_index <= previous_source_index:
                continue
            extend(index + 1, source_index, [*chosen, source_index])

    extend(0, -1, [])
    return solutions


def resolve_repeated_normalized_anchors(
    source_text,
    translation,
    repair_regions,
    occurrences,
    unresolved_region_ids,
    source_language="zh",
    normalizer=None,
):
    """Prove grouped repeated normalized anchors without first-match guesses.

    Stronger region locators are retained as fixed source-identity constraints.
    Only regions that failed those locators are eligible for assignment here.
    Raw source and target offsets remain authoritative throughout.
    """
    result = {
        "status": "not_applicable",
        "reason_code": "",
        "policy": REPEATED_ANCHOR_ASSIGNMENT_POLICY_VERSION,
        "region_assignments": {},
        "group_provenance": [],
    }
    unresolved_ids = set(unresolved_region_ids or ())
    if not unresolved_ids:
        return result
    if not _is_supported_chinese_source_language(source_language):
        return {
            **result,
            "status": "disabled",
            "reason_code": "repeated_normalized_anchor_language_unsupported",
        }

    normalize = normalizer or traditional_to_simplified
    regions_by_id = {region["region_id"]: region for region in repair_regions}
    entries = []
    for region in repair_regions:
        for occurrence in _region_occurrences(region, occurrences):
            normalized = normalize(occurrence["fragment"])
            if (
                normalized is None
                or not normalized
                or len(normalized) != len(occurrence["fragment"])
                or CJK_FRAGMENT_RE.fullmatch(occurrence["fragment"]) is None
            ):
                if region["region_id"] in unresolved_ids:
                    return {
                        **result,
                        "status": "failed",
                        "reason_code": "repeated_normalized_anchor_fragment_ineligible",
                    }
                continue
            entries.append({
                "region_id": region["region_id"],
                "target_start": occurrence["start"],
                "target_end": occurrence["end"],
                "target_raw": occurrence["fragment"],
                "normalized_anchor": normalized,
            })

    groups = {}
    for entry in entries:
        groups.setdefault(entry["normalized_anchor"], []).append(entry)
    needed_groups = [
        (normalized, group)
        for normalized, group in groups.items()
        if any(item["region_id"] in unresolved_ids for item in group)
    ]
    unresolved_with_group = {
        item["region_id"]
        for _normalized, group in needed_groups
        for item in group
        if item["region_id"] in unresolved_ids
    }
    if unresolved_with_group != unresolved_ids:
        return {
            **result,
            "status": "failed",
            "reason_code": "repeated_normalized_anchor_group_incomplete",
        }

    all_region_assignments = {}
    group_provenance = []
    for normalized, raw_group in needed_groups:
        group = sorted(raw_group, key=lambda item: item["target_start"])
        if any(
            left["target_start"] >= right["target_start"]
            or left["target_end"] > right["target_start"]
            for left, right in zip(group, group[1:])
        ):
            return {
                **result,
                "status": "failed",
                "reason_code": "repeated_normalized_anchor_target_offsets_unsafe",
            }
        for region_id in unresolved_ids:
            region_entries = [
                item for item in group if item["region_id"] == region_id
            ]
            if region_entries and len(region_entries) != len(
                _region_occurrences(regions_by_id[region_id], occurrences)
            ):
                return {
                    **result,
                    "status": "failed",
                    "reason_code": "repeated_normalized_anchor_group_mixed",
                }

        candidate_result = _normalized_fragment_source_candidates(
            source_text or "", group[0]["target_raw"], normalize
        )
        if candidate_result["status"] != "candidates_found":
            return {
                **result,
                "status": "failed",
                "reason_code": (
                    "repeated_normalized_anchor_not_found"
                    if candidate_result["status"] == "not_found"
                    else candidate_result["status"]
                ),
            }
        candidates = sorted(
            candidate_result["candidates"], key=lambda item: item["start"]
        )
        if any(
            left["start"] >= right["start"]
            or left["end"] > right["start"]
            for left, right in zip(candidates, candidates[1:])
        ):
            return {
                **result,
                "status": "failed",
                "reason_code": "repeated_normalized_anchor_source_offsets_unsafe",
            }
        if any(item["normalized_anchor"] != normalized for item in candidates):
            return {
                **result,
                "status": "failed",
                "reason_code": "repeated_normalized_anchor_normalization_mismatch",
            }

        fixed = {}
        for region_id in {item["region_id"] for item in group}:
            if region_id in unresolved_ids:
                continue
            region = regions_by_id[region_id]
            anchor = region.get("source_anchor")
            if not anchor:
                continue
            target_indexes = [
                index for index, item in enumerate(group)
                if item["region_id"] == region_id
            ]
            source_indexes = [
                index for index, candidate in enumerate(candidates)
                if anchor["start"] <= candidate["start"]
                and candidate["end"] <= anchor["end"]
            ]
            if len(source_indexes) != len(target_indexes):
                return {
                    **result,
                    "status": "failed",
                    "reason_code": "repeated_normalized_anchor_strong_identity_ambiguous",
                }
            for target_index, source_index in zip(target_indexes, source_indexes):
                fixed[target_index] = source_index

        choices = []
        segment_evidence = {}
        full_candidate_indexes = tuple(range(len(candidates)))
        for target_index, entry in enumerate(group):
            if target_index in fixed:
                choices.append((fixed[target_index],))
                continue
            region = regions_by_id[entry["region_id"]]
            segment_bounds = _corresponding_source_segment_bounds(
                source_text, translation, region
            )
            if segment_bounds is None:
                return {
                    **result,
                    "status": "failed",
                    "reason_code": "repeated_normalized_anchor_region_unproven",
                }
            compatible = tuple(
                index for index, candidate in enumerate(candidates)
                if segment_bounds["start"] <= candidate["start"]
                and candidate["end"] <= segment_bounds["end"]
            )
            if not compatible:
                return {
                    **result,
                    "status": "failed",
                    "reason_code": "repeated_normalized_anchor_region_incompatible",
                }
            choices.append(compatible)
            segment_evidence[target_index] = segment_bounds

        # Equal counts plus order are deliberately not proof when every local
        # region admits every source candidate and no stronger locator fixes an
        # identity.
        if (
            not fixed
            and len(group) > 1
            and all(choice == full_candidate_indexes for choice in choices)
        ):
            return {
                **result,
                "status": "ambiguous",
                "reason_code": "repeated_normalized_anchor_assignment_ambiguous",
            }

        solutions = _enumerate_monotonic_assignments(group, candidates, choices)
        if not solutions:
            return {
                **result,
                "status": "failed",
                "reason_code": "repeated_normalized_anchor_assignment_incompatible",
            }
        if len(solutions) != 1:
            return {
                **result,
                "status": "ambiguous",
                "reason_code": "repeated_normalized_anchor_assignment_ambiguous",
            }

        solution = solutions[0]
        raw_identity_count = len({
            candidates[source_index]["source_raw"] for source_index in solution
        })
        if raw_identity_count > 1 and any(
            len(choices[index]) != 1 and index not in fixed
            for index in range(len(group))
        ):
            return {
                **result,
                "status": "ambiguous",
                "reason_code": "repeated_normalized_anchor_source_identity_ambiguous",
            }

        mappings = []
        for target_index, source_index in enumerate(solution):
            entry = group[target_index]
            candidate = candidates[source_index]
            mapping = {
                "anchor_origin": "ordered_repeated_normalized_anchor",
                "policy": REPEATED_ANCHOR_ASSIGNMENT_POLICY_VERSION,
                "normalization": TRADITIONAL_TO_SIMPLIFIED,
                "normalization_backend": traditional_to_simplified_backend(),
                "normalized_anchor": normalized,
                "target_occurrence_index": target_index + 1,
                "source_occurrence_index": source_index + 1,
                "target_offset": entry["target_start"],
                "target_end": entry["target_end"],
                "residual_raw": entry["target_raw"],
                "source_raw": candidate["source_raw"],
                "source_start": candidate["start"],
                "source_end": candidate["end"],
                "region_id": entry["region_id"],
            }
            if target_index in fixed:
                mapping["stronger_locator"] = regions_by_id[
                    entry["region_id"]
                ].get("anchor_mode", "")
            evidence = segment_evidence.get(target_index)
            if evidence is not None:
                mapping["source_segment_index"] = evidence[
                    "source_segment_index"
                ]
                mapping["target_segment_index"] = evidence[
                    "target_segment_index"
                ]
            else:
                region = regions_by_id[entry["region_id"]]
                if "source_segment_index" in region:
                    mapping["source_segment_index"] = region[
                        "source_segment_index"
                    ]
                if "target_segment_index" in region:
                    mapping["target_segment_index"] = region[
                        "target_segment_index"
                    ]
            mappings.append(mapping)
            all_region_assignments.setdefault(entry["region_id"], []).append(mapping)
        group_provenance.append({
            "policy": REPEATED_ANCHOR_ASSIGNMENT_POLICY_VERSION,
            "normalized_anchor": normalized,
            "target_occurrence_count": len(group),
            "source_occurrence_count": len(candidates),
            "valid_monotonic_assignment_count": 1,
            "assignment_unique": True,
            "mappings": mappings,
        })

    return {
        **result,
        "status": "matched",
        "region_assignments": all_region_assignments,
        "group_provenance": group_provenance,
    }


def assess_micro_repair_eligibility(
    source_text,
    translation,
    config,
    approved_reader_facing_terms=(),
    source_language="zh",
):
    """Return a conservative eligibility decision with precise repair regions."""
    report = residual_source_script_report(
        translation,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    repair_regions = _translation_repair_regions(
        translation, report["residual_cjk_occurrences"]
    )
    reasons = []
    if not _setting(config, "enabled"):
        reasons.append("automatic residual-CJK micro-repair is disabled")
    cjk_count = report["remaining_cjk_character_count"]
    occurrences = report["residual_cjk_occurrences"]
    if cjk_count == 0:
        reasons.append("translation contains no unexpected residual CJK")
    if cjk_count > _setting(config, "max_total_cjk_chars"):
        reasons.append("residual CJK character limit exceeded")
    if len(occurrences) > _setting(config, "max_fragments"):
        reasons.append("residual CJK fragment limit exceeded")
    if len(report["affected_line_numbers"]) > _setting(
        config, "max_affected_lines"
    ):
        reasons.append("affected-line limit exceeded")

    target_letters = len(re.findall(r"[A-Za-z]", translation or ""))
    if cjk_count and (
        target_letters < MIN_TARGET_LETTERS_IN_TRANSLATION
        or target_letters < cjk_count * 10
    ):
        reasons.append("translation is not overwhelmingly target-language prose")

    affected_lines = {}
    for occurrence in occurrences:
        line_number = occurrence["line_number"]
        line = occurrence["line_text"]
        affected_lines[line_number] = line
    for line_number, line in affected_lines.items():
        line_letters = len(re.findall(r"[A-Za-z]", line))
        line_cjk = residual_source_script_report(
            line,
            source_language,
            approved_reader_facing_terms,
            source_text=source_text,
        )["remaining_cjk_character_count"]
        ratio_denominator = max(1, line_letters + line_cjk)
        if line_letters < MIN_TARGET_LETTERS_PER_AFFECTED_LINE:
            reasons.append(
                f"affected line {line_number} is not a bounded English repair region"
            )
        if line_cjk / ratio_denominator > MAX_AFFECTED_LINE_CJK_RATIO:
            reasons.append(
                f"affected line {line_number} contains too much untranslated CJK"
            )
        if len(line) > MAX_AFFECTED_LINE_CHARS:
            reasons.append(f"affected line {line_number} is too large for micro-repair")

    source = source_text or ""
    repeated_fallback_region_ids = []
    for region in repair_regions:
        source_anchor = _ordered_source_anchor(
            source, region["residual_fragments"]
        )
        if source_anchor is not None:
            region["anchor_mode"] = "exact_source"
            region["source_anchor"] = source_anchor
            continue
        alignment = _aligned_source_segment(source, translation, region)
        if alignment is not None:
            region["anchor_mode"] = "aligned_segment"
            region.update(alignment)
            continue

        normalized_anchor = normalized_source_anchor_match(
            source,
            region["residual_fragments"],
            source_language=source_language,
        )
        if normalized_anchor["status"] == "matched":
            provenance = normalized_anchor["provenance"]
            region["anchor_mode"] = "script_normalized_source_anchor"
            region["anchor_origin"] = provenance["anchor_origin"]
            region["source_anchor_provenance"] = provenance
            region["source_anchor"] = {
                "start": normalized_anchor["start"],
                "end": normalized_anchor["end"],
                "context": normalized_anchor["context"],
            }
            continue

        if normalized_anchor["reason_code"] == "normalized_source_anchor_ambiguous":
            repeated_fallback_region_ids.append(region["region_id"])
            continue
        else:
            for fragment in dict.fromkeys(region["residual_fragments"]):
                if fragment not in source:
                    reasons.append(
                        f"residual fragment {fragment!r} has no exact source anchor"
                    )
            if normalized_anchor["reason_code"] in {
                "normalization_unavailable",
                "normalized_source_anchor_offset_unsafe",
            }:
                reasons.append(normalized_anchor["reason_code"])
        reasons.append(
            f"repair region {region['region_id']} lacks a compact ordered source "
            "anchor and a reliable aligned source segment"
        )

    repeated_resolution = resolve_repeated_normalized_anchors(
        source,
        translation,
        repair_regions,
        occurrences,
        repeated_fallback_region_ids,
        source_language=source_language,
    )
    if repeated_fallback_region_ids:
        if repeated_resolution["status"] == "matched":
            for region in repair_regions:
                assignments = repeated_resolution["region_assignments"].get(
                    region["region_id"], []
                )
                if not assignments:
                    continue
                region["repeated_anchor_assignment_provenance"] = deepcopy(
                    assignments
                )
                if region["region_id"] not in repeated_fallback_region_ids:
                    continue
                source_start = min(item["source_start"] for item in assignments)
                source_end = max(item["source_end"] for item in assignments)
                context_start = max(0, source_start - 120)
                context_end = min(len(source), source_end + 120)
                region["anchor_mode"] = "ordered_repeated_normalized_anchor"
                region["anchor_origin"] = "ordered_repeated_normalized_anchor"
                region["source_anchor_provenance"] = (
                    deepcopy(assignments[0])
                    if len(assignments) == 1
                    else {
                        "anchor_origin": "ordered_repeated_normalized_anchor",
                        "policy": REPEATED_ANCHOR_ASSIGNMENT_POLICY_VERSION,
                        "normalized_anchor": assignments[0]["normalized_anchor"],
                        "matches": deepcopy(assignments),
                    }
                )
                region["source_anchor"] = {
                    "start": source_start,
                    "end": source_end,
                    "context": source[context_start:context_end],
                }
        else:
            reason_code = repeated_resolution.get("reason_code") or (
                "repeated_normalized_anchor_assignment_failed"
            )
            reasons.append(reason_code)
            for region_id in repeated_fallback_region_ids:
                reasons.append(
                    f"normalized_source_anchor_ambiguous for repair region {region_id}"
                )
                reasons.append(
                    f"repair region {region_id} lacks a compact ordered source "
                    "anchor and a reliable aligned source segment"
                )

    return {
        "eligible": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "pending_content_hash": stable_text_hash(translation),
        "repair_regions": repair_regions,
        "repeated_anchor_assignment_groups": deepcopy(
            repeated_resolution.get("group_provenance", [])
        ),
        **report,
    }


def build_micro_repair_request(
    *,
    novel_id,
    profile_id,
    target_language,
    source_text,
    translation,
    eligibility,
    relevant_locked_glossary=(),
    auxiliary_terminology_policy="",
    approved_reader_facing_terms=(),
    policy_version=MICRO_REPAIR_POLICY_VERSION,
    source_language="zh",
):
    """Build the bounded structured request supplied to production's API helper."""
    lines = (translation or "").splitlines()
    regions = []
    for region in eligibility["repair_regions"]:
        line_number = region["line_number"]
        index = line_number - 1
        regions.append({
            "region_id": region["region_id"],
            "line_number": line_number,
            "anchor_mode": region.get("anchor_mode", "exact_source"),
            "source_anchor_provenance": deepcopy(
                region.get("source_anchor_provenance", {})
            ),
            "repeated_anchor_assignment_provenance": deepcopy(
                region.get("repeated_anchor_assignment_provenance", [])
            ),
            "previous_line": lines[index - 1] if index > 0 else "",
            "affected_text": region["affected_text"],
            "next_line": lines[index + 1] if index + 1 < len(lines) else "",
            "residual_fragments": region["residual_fragments"],
            "source_context": region.get("source_anchor", {}).get("context", ""),
            "target_context": region.get("target_segment", {}).get(
                "context", region["affected_text"]
            ),
        })
    schema = {
        "repairs": [{
            "region_id": "line-1-region-1",
            "line_number": 1,
            "old_text": "exact short anchor containing residual CJK",
            "new_text": "minimal target-language replacement",
        }]
    }
    system_prompt = (
        "Repair ONLY the listed untranslated residual source-script fragments in an "
        f"otherwise complete {target_language} translation. Preserve all existing "
        f"target-language wording. Every new_text value must be entirely in the target "
        f"language ({target_language}). Source-language text is semantic evidence only; "
        "never copy it into final target-language output. Translate the residual concept "
        "into natural target-language wording rather than preserving or transliterating "
        "its source script, unless it is a proper name whose approved terminology policy "
        "explicitly requires romanization. EVERY detected unexpected source-script fragment "
        "targeted by a repair MUST be absent from new_text. new_text must contain ZERO "
        "unexpected source-script characters. For Korean sources this includes Hangul, "
        "Jamo, and Hanja/CJK ideographs. Quotation marks do not exempt residual text: "
        "do not preserve "
        "it merely because it appears as an inscription, label, symbol, proper name, "
        "cultural term, or source-faithful annotation. Only terms explicitly listed in "
        "approved_reader_facing_cjk_terms are exempt. The purpose of this operation is "
        "target-language-only text for each affected region. When residual CJK is "
        "immediately accompanied by an existing target-language translation or gloss, "
        "preserve that meaning and eliminate the redundant CJK. Example input shape: "
        "This object bears the mark \"漢\" (Example Meaning). Valid target-language-only "
        "new_text: This object bears the mark \"Example Meaning\". A new_text that still "
        "contains 漢 is invalid. Make only the smallest grammatical adjustment immediately "
        "around a residual fragment when necessary. Preserve meaning, punctuation, "
        "paragraph structure, and existing target-language wording as much as possible. "
        "Do not improve or rewrite unrelated prose. Do not summarize, expand, add "
        "information, remove story content, or change paragraph boundaries. "
        "When anchor_mode is aligned_segment, source_context is the uniquely aligned "
        "source paragraph and target_context is its current translated paragraph; use "
        "that source evidence to translate the residual without copying or merely "
        "transliterating source script. Preserve all correct surrounding target-language "
        "wording. "
        "The authorized patch scope remains affected_text only. "
        "Return one JSON object only, with no Markdown or commentary. Each old_text must be an exact "
        "short substring of its affected_text region and must contain a listed residual "
        "fragment."
    )
    payload = {
        "novel_id": novel_id,
        "translation_profile": profile_id,
        "source_language": source_language,
        "target_language": target_language,
        "micro_repair_policy_version": policy_version,
        "repair_regions": regions,
        "approved_reader_facing_cjk_terms": list(approved_reader_facing_terms),
        "relevant_locked_glossary": list(relevant_locked_glossary),
        "auxiliary_terminology_policy": auxiliary_terminology_policy or "",
        "required_response_schema": schema,
    }
    return {
        "system_prompt": system_prompt,
        "user_prompt": json.dumps(payload, ensure_ascii=False, indent=2),
        "payload": payload,
        "schema": schema,
    }


def build_corrective_micro_repair_request(original_request, validation):
    """Build one same-scope retry for a locally rejected residual-only patch."""
    payload = deepcopy(original_request["payload"])
    payload["request_kind"] = "corrective_retry"
    payload["rejected_repairs"] = deepcopy(
        validation.get("diagnostic_repairs", [])
    )
    payload["previous_validation_errors"] = list(validation.get("errors", []))
    source_language = str(payload.get("source_language", "")).casefold()
    target_language = str(payload.get("target_language", ""))
    script_requirement = (
        "For Korean-to-English repair, new_text must contain no Hangul, Jamo, "
        "Hanja, or CJK ideographs. "
        if source_language.startswith("ko") and target_language.casefold() == "en"
        else "new_text must contain no unexpected source-script characters. "
    )
    system_prompt = (
        "Correct ONE previously rejected bounded residual-script repair. Use exactly the "
        "same authorized repair_regions and return repairs against the ORIGINAL "
        "affected_text, not against a rewritten chapter. Correct only the listed local "
        "validation errors. Do not broaden, summarize, or rewrite the surrounding prose. "
        f"Every new_text value must be entirely in the target language "
        f"({target_language}). Source-language text and the rejected "
        "candidate are evidence only and must never be copied into final output. For "
        + script_requirement
        + "Translate residual concepts into natural target-language wording; do not merely "
        "transliterate them unless an approved proper-name policy explicitly requires it. "
        "Preserve all correct target-language wording, punctuation, structure, and "
        "approved terminology. "
        "Return only the required JSON object with no Markdown or commentary."
    )
    return {
        "system_prompt": system_prompt,
        "user_prompt": json.dumps(payload, ensure_ascii=False, indent=2),
        "payload": payload,
        "schema": deepcopy(original_request["schema"]),
    }


def _word_subsequence(old_text, new_text):
    old_words = [item.casefold() for item in ASCII_WORD_RE.findall(old_text)]
    new_words = [item.casefold() for item in ASCII_WORD_RE.findall(new_text)]
    position = 0
    for word in new_words:
        if position < len(old_words) and word == old_words[position]:
            position += 1
    return position == len(old_words)


def _decode_response(response):
    if isinstance(response, str):
        return json.loads(clean_json_text(response))
    if isinstance(response, dict):
        return deepcopy(response)
    raise ValueError("repair response must be a JSON object or JSON text")


def _bounded_text_diagnostic(value):
    if not isinstance(value, str):
        return value
    if len(value) <= MAX_DIAGNOSTIC_RESPONSE_CHARS:
        return value
    return value[:MAX_DIAGNOSTIC_RESPONSE_CHARS] + "...[truncated]"


def _response_diagnostic(response):
    """Retain bounded repair evidence without persisting unrelated chapter text."""
    if isinstance(response, dict):
        repairs = response.get("repairs")
        if isinstance(repairs, list):
            safe_repairs = []
            for item in repairs[:8]:
                if not isinstance(item, dict):
                    safe_repairs.append(_bounded_text_diagnostic(repr(item)))
                    continue
                safe_repairs.append({
                    key: _bounded_text_diagnostic(item[key])
                    for key in ("region_id", "line_number", "old_text", "new_text")
                    if key in item
                })
            return {
                "response_type": "structured",
                "repairs": safe_repairs,
                "repair_count": len(repairs),
                "repairs_truncated": len(repairs) > len(safe_repairs),
            }
        return {
            "response_type": "structured",
            "top_level_keys": sorted(str(key) for key in response)[:20],
        }
    if isinstance(response, str):
        return {
            "response_type": "text",
            "response_excerpt": _bounded_text_diagnostic(response),
            "response_truncated": len(response) > MAX_DIAGNOSTIC_RESPONSE_CHARS,
        }
    return {
        "response_type": type(response).__name__,
        "response_excerpt": _bounded_text_diagnostic(repr(response)),
    }


def _public_repairs(validated):
    return [
        {key: value for key, value in item.items() if key not in {"start", "end"}}
        for item in validated
    ]


def minimal_contiguous_text_diff(old_text, new_text):
    """Return one deterministic longest-prefix/longest-suffix text diff."""
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise TypeError("minimal diff inputs must be text")
    prefix_end = 0
    shared_limit = min(len(old_text), len(new_text))
    while (
        prefix_end < shared_limit
        and old_text[prefix_end] == new_text[prefix_end]
    ):
        prefix_end += 1

    suffix_length = 0
    old_remaining = len(old_text) - prefix_end
    new_remaining = len(new_text) - prefix_end
    while (
        suffix_length < old_remaining
        and suffix_length < new_remaining
        and old_text[len(old_text) - suffix_length - 1]
        == new_text[len(new_text) - suffix_length - 1]
    ):
        suffix_length += 1

    old_end = len(old_text) - suffix_length
    new_end = len(new_text) - suffix_length
    raw_old = old_text[prefix_end:old_end]
    raw_new = new_text[prefix_end:new_end]
    return {
        "unchanged_prefix": old_text[:prefix_end],
        "unchanged_suffix": old_text[old_end:],
        "old_start": prefix_end,
        "old_end": old_end,
        "new_start": prefix_end,
        "new_end": new_end,
        "old_changed_text": raw_old,
        "new_changed_text": raw_new,
        # Human-facing provenance excludes boundary whitespace; the raw spans
        # and offsets above remain authoritative for validation/application.
        "minimal_old_span": raw_old.strip(),
        "minimal_new_span": raw_new.strip(),
    }


def _source_fragment_candidates_in_scope(
    source_text, fragment, scope_start, scope_end, source_language
):
    scope = (source_text or "")[scope_start:scope_end]
    exact = []
    search_from = 0
    while fragment:
        position = scope.find(fragment, search_from)
        if position < 0:
            break
        exact.append({
            "start": scope_start + position,
            "end": scope_start + position + len(fragment),
            "residual_raw": fragment,
            "source_raw": fragment,
            "normalized_anchor": fragment,
        })
        search_from = position + len(fragment)
    if exact:
        return exact
    if not _is_supported_chinese_source_language(source_language):
        return []
    normalized = _normalized_fragment_source_candidates(
        scope, fragment, traditional_to_simplified
    )
    if normalized["status"] != "candidates_found":
        return []
    return [
        {
            **item,
            "start": scope_start + item["start"],
            "end": scope_start + item["end"],
        }
        for item in normalized["candidates"]
    ]


def source_compound_support_for_hybrid_rewrite(
    source_text, region, source_language="zh"
):
    """Prove one compact source-script run supports the residual anchor."""
    if not _is_supported_chinese_source_language(source_language):
        return {
            "supported": False,
            "reason": "hybrid lexical-unit salvage is limited to Chinese sources",
        }
    source = source_text or ""
    anchor = region.get("source_anchor")
    if not isinstance(anchor, dict):
        return {"supported": False, "reason": "source anchor is missing"}
    anchor_start = anchor.get("start")
    anchor_end = anchor.get("end")
    if (
        not isinstance(anchor_start, int)
        or isinstance(anchor_start, bool)
        or not isinstance(anchor_end, int)
        or isinstance(anchor_end, bool)
        or not (0 <= anchor_start < anchor_end <= len(source))
    ):
        return {"supported": False, "reason": "source anchor offsets are invalid"}

    if region.get("anchor_mode") == "aligned_segment":
        scope_start, scope_end = anchor_start, anchor_end
        if anchor.get("context") != source[scope_start:scope_end]:
            return {
                "supported": False,
                "reason": "aligned source segment identity no longer matches",
            }
    else:
        # Without a paragraph alignment, uniqueness must hold across the whole
        # current source unit rather than choosing the first matching compound.
        scope_start, scope_end = 0, len(source)

    fragments = list(region.get("residual_fragments", ()))
    if not fragments:
        return {"supported": False, "reason": "source residual identity is missing"}
    per_fragment = [
        _source_fragment_candidates_in_scope(
            source, fragment, scope_start, scope_end, source_language
        )
        for fragment in fragments
    ]
    if any(not candidates for candidates in per_fragment):
        return {
            "supported": False,
            "reason": "source residual has no current compact source match",
        }

    combinations = []

    def extend(index, chosen):
        if len(combinations) > 1:
            return
        if index == len(per_fragment):
            combinations.append(list(chosen))
            return
        for candidate in per_fragment[index]:
            if chosen and candidate["start"] < chosen[-1]["end"]:
                continue
            proposed_start = chosen[0]["start"] if chosen else candidate["start"]
            if candidate["end"] - proposed_start > MAX_HYBRID_SOURCE_COMPOUND_CHARS:
                continue
            extend(index + 1, [*chosen, candidate])

    extend(0, [])
    unique = {
        tuple((item["start"], item["end"], item["source_raw"]) for item in value): value
        for value in combinations
    }
    combinations = list(unique.values())
    if len(combinations) != 1:
        return {
            "supported": False,
            "reason": "source compound anchor is ambiguous",
            "candidate_count": len(combinations),
        }

    matches = combinations[0]
    matched_start = matches[0]["start"]
    matched_end = matches[-1]["end"]
    compound_start = matched_start
    compound_end = matched_end
    while (
        compound_start > scope_start
        and CJK_FRAGMENT_RE.fullmatch(source[compound_start - 1]) is not None
    ):
        compound_start -= 1
    while (
        compound_end < scope_end
        and CJK_FRAGMENT_RE.fullmatch(source[compound_end]) is not None
    ):
        compound_end += 1
    compound = source[compound_start:compound_end]
    if len(compound) > MAX_HYBRID_SOURCE_COMPOUND_CHARS:
        return {
            "supported": False,
            "reason": "source-side expression exceeds the compact lexical bound",
        }
    if compound_start == matched_start and compound_end == matched_end:
        return {
            "supported": False,
            "reason": "source residual is not part of a larger compact expression",
        }
    return {
        "supported": True,
        "anchor_mode": region.get("anchor_mode", ""),
        "scope_start": scope_start,
        "scope_end": scope_end,
        "matched_start": matched_start,
        "matched_end": matched_end,
        "compound_start": compound_start,
        "compound_end": compound_end,
        "compound_text": compound,
        "source_matches": matches,
        "source_identity": stable_text_hash(source[scope_start:scope_end]),
    }


def _locked_glossary_forms(relevant_locked_glossary):
    forms = set()
    for entry in relevant_locked_glossary or ():
        if not isinstance(entry, dict) or entry.get("status") != "locked":
            continue
        for key in ("chinese", "source", "english"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                forms.add(value)
        for key in ("accepted_inflections", "approved_renderings"):
            values = entry.get(key, ())
            if isinstance(values, list):
                forms.update(value for value in values if isinstance(value, str) and value)
    return sorted(forms, key=len, reverse=True)


def _contains_locked_form(text, relevant_locked_glossary):
    folded = (text or "").casefold()
    return any(form.casefold() in folded for form in _locked_glossary_forms(
        relevant_locked_glossary
    ))


def _ordinary_lowercase_hybrid_tokens(text):
    matches = list(HYBRID_ENGLISH_TOKEN_RE.finditer(text or ""))
    if any(match.group(0) != match.group(0).casefold() for match in matches):
        return None
    return matches


def validate_hybrid_lexical_unit_rewrite(
    translation,
    response,
    eligibility,
    relevant_locked_glossary=(),
    approved_reader_facing_terms=(),
    source_language="zh",
    source_text=None,
):
    """Revalidate only the English-word guard for one tiny hybrid lexical unit."""
    ordinary = validate_and_apply_repair_response(
        translation,
        response,
        eligibility,
        approved_reader_facing_terms,
        source_language,
        source_text=source_text,
    )
    expected_error = "repairs[0].new_text rewrites unaffected English words"
    if ordinary.get("errors") != [expected_error]:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": ["recorded candidate is not rejected only by the English-word guard"],
            "ordinary_validation": ordinary,
        }
    try:
        parsed = _decode_response(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    repairs = parsed.get("repairs") if isinstance(parsed, dict) else None
    if not isinstance(repairs, list) or len(repairs) != 1:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": ["hybrid lexical-unit salvage requires exactly one repair"],
            "ordinary_validation": ordinary,
        }
    repair = repairs[0]
    region = next(
        (
            item for item in eligibility.get("repair_regions", ())
            if item.get("region_id") == repair.get("region_id")
            and item.get("line_number") == repair.get("line_number")
        ),
        None,
    )
    if region is None:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": ["recorded repair region identity no longer matches"],
            "ordinary_validation": ordinary,
        }
    old_text = repair["old_text"]
    new_text = repair["new_text"]
    scope = (translation or "")[region["scope_start"]:region["scope_end"]]
    if scope.count(old_text) != 1:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": ["recorded old_text is not exact and unique in the current region"],
            "ordinary_validation": ordinary,
        }
    absolute_start = region["scope_start"] + scope.index(old_text)
    absolute_end = absolute_start + len(old_text)
    diff = minimal_contiguous_text_diff(old_text, new_text)
    changed_old = diff["old_changed_text"]
    changed_new = diff["new_changed_text"]
    changed_start = absolute_start + diff["old_start"]
    changed_end = absolute_start + diff["old_end"]
    errors = []
    if not changed_old or not changed_new:
        errors.append("hybrid lexical-unit diff must replace one non-empty span")
    if (
        len(changed_old) > MAX_HYBRID_CHANGED_SPAN_CHARS
        or len(changed_new) > MAX_HYBRID_CHANGED_SPAN_CHARS
    ):
        errors.append("hybrid lexical-unit diff exceeds the local character bound")
    if any(mark in changed_old + changed_new for mark in HYBRID_PUNCTUATION_BOUNDARIES):
        errors.append("hybrid lexical-unit diff crosses punctuation or a quote boundary")
    if HYBRID_HIGH_RISK_RE.search(changed_old + changed_new):
        errors.append("hybrid lexical-unit diff contains high-risk immutable material")
    if _contains_locked_form(changed_old + changed_new, relevant_locked_glossary):
        errors.append("hybrid lexical-unit diff overlaps locked glossary material")

    region_occurrences = [
        occurrence for occurrence in eligibility.get("residual_cjk_occurrences", ())
        if region["scope_start"] <= occurrence["start"]
        and occurrence["end"] <= region["scope_end"]
    ]
    if not region_occurrences or any(
        not (changed_start <= occurrence["start"] and occurrence["end"] <= changed_end)
        for occurrence in region_occurrences
    ):
        errors.append("minimal old diff does not contain every blocking region character")

    intentional = eligibility.get("intentional_reader_facing_occurrences", ())
    if any(
        changed_start < occurrence["end"] and occurrence["start"] < changed_end
        for occurrence in intentional
    ):
        errors.append("hybrid lexical-unit diff overlaps protected reader-facing script")

    local_occurrence_spans = [
        (occurrence["start"] - changed_start, occurrence["end"] - changed_start)
        for occurrence in region_occurrences
        if changed_start <= occurrence["start"] and occurrence["end"] <= changed_end
    ]
    old_tokens = _ordinary_lowercase_hybrid_tokens(changed_old)
    new_tokens = _ordinary_lowercase_hybrid_tokens(changed_new)
    if old_tokens is None or new_tokens is None:
        errors.append("hybrid lexical-unit diff changes capitalized or proper-name tokens")
        old_tokens = old_tokens or []
        new_tokens = new_tokens or []
    if not (1 <= len(old_tokens) <= MAX_HYBRID_ADJACENT_ENGLISH_TOKENS):
        errors.append("hybrid lexical-unit diff changes too many adjacent English tokens")
    if not (1 <= len(new_tokens) <= MAX_HYBRID_ADJACENT_ENGLISH_TOKENS):
        errors.append("hybrid lexical-unit replacement is not one ordinary lowercase token")

    if local_occurrence_spans and len(old_tokens) == 1:
        token = old_tokens[0]
        residual_start = min(start for start, _end in local_occurrence_spans)
        residual_end = max(end for _start, end in local_occurrence_spans)
        if token.end() <= residual_start:
            gap = changed_old[token.end():residual_start]
        elif residual_end <= token.start():
            gap = changed_old[residual_end:token.start()]
        else:
            gap = None
        if gap is None or any(character not in " \t-" for character in gap):
            errors.append("changed English token is not immediately adjacent to the residual")

    claimed_old = [False] * len(changed_old)
    for start, end in local_occurrence_spans:
        for index in range(max(0, start), min(len(claimed_old), end)):
            claimed_old[index] = True
    for token in old_tokens:
        for index in range(token.start(), token.end()):
            claimed_old[index] = True
    if any(
        not claimed and character not in " \t-"
        for claimed, character in zip(claimed_old, changed_old)
    ):
        errors.append("minimal old diff contains material outside one compact lexical unit")
    claimed_new = [False] * len(changed_new)
    for token in new_tokens:
        for index in range(token.start(), token.end()):
            claimed_new[index] = True
    if any(
        not claimed and character not in " \t-"
        for claimed, character in zip(claimed_new, changed_new)
    ):
        errors.append("minimal new diff contains non-lexical replacement material")

    source_support = source_compound_support_for_hybrid_rewrite(
        source_text, region, source_language
    )
    if not source_support.get("supported"):
        errors.append(source_support.get("reason", "source compound support failed"))
    if errors:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": list(dict.fromkeys(errors)),
            "ordinary_validation": ordinary,
            "minimal_diff": diff,
            "source_compound_support": source_support,
        }

    repaired = (
        (translation or "")[:absolute_start]
        + new_text
        + (translation or "")[absolute_end:]
    )
    remaining = residual_source_script_report(
        repaired,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    if remaining["remaining_cjk_character_count"]:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "errors": ["hybrid lexical-unit candidate still contains blocking source script"],
            "ordinary_validation": ordinary,
            "minimal_diff": diff,
            "source_compound_support": source_support,
        }
    public_repair = {
        **repair,
        "residual_fragments": [item["fragment"] for item in region_occurrences],
        "repair_type": REPAIR_EVENT_TYPE,
    }
    return {
        "accepted": True,
        "repaired_text": repaired,
        "repairs_applied": [public_repair],
        "errors": [],
        "ordinary_validation": ordinary,
        "minimal_diff": diff,
        "source_compound_support": source_support,
        "remaining_report": remaining,
    }


def validate_and_apply_repair_response(
    translation,
    response,
    eligibility,
    approved_reader_facing_terms=(),
    source_language="zh",
    source_text=None,
):
    """Validate every anchored patch, rejecting the whole response on any error."""
    errors = []
    try:
        parsed = _decode_response(response)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "diagnostic_repairs": [],
            "response_diagnostic": _response_diagnostic(response),
            "errors": [f"malformed repair JSON: {exc}"],
        }
    if not isinstance(parsed, dict):
        errors.append("repair response must be a JSON object")
    elif set(parsed) != {"repairs"}:
        errors.append("repair response contains missing or unexpected top-level fields")
    repairs = parsed.get("repairs") if isinstance(parsed, dict) else None
    if not isinstance(repairs, list):
        errors.append("repairs must be a list")
        repairs = []
    if not repairs:
        errors.append("repairs must not be empty while residual CJK remains")
    if len(repairs) > len(eligibility["residual_cjk_occurrences"]):
        errors.append("repair count exceeds the detected residual-fragment count")

    lines = (translation or "").splitlines()
    affected = set(eligibility["affected_line_numbers"])
    detected_fragments = set(eligibility["residual_cjk_fragments"])
    repair_regions = eligibility.get("repair_regions", [])
    regions_by_id = {item["region_id"]: item for item in repair_regions}
    validated = []
    occupied = []
    total_patch_chars = 0
    for index, item in enumerate(repairs):
        label = f"repairs[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        allowed_fields = {"region_id", "line_number", "old_text", "new_text"}
        if set(item) - allowed_fields:
            errors.append(f"{label} contains unexpected fields")
        if "old_text" not in item or "new_text" not in item:
            errors.append(f"{label} is missing old_text or new_text")
            continue
        old_text = item["old_text"]
        new_text = item["new_text"]
        if not isinstance(old_text, str) or not old_text:
            errors.append(f"{label}.old_text must be non-empty text")
            continue
        if not isinstance(new_text, str) or not new_text:
            errors.append(f"{label}.new_text must be non-empty text")
            continue
        if len(old_text) > MAX_REPAIR_OLD_TEXT_CHARS:
            errors.append(f"{label}.old_text is too large")
        if len(new_text) > MAX_REPAIR_NEW_TEXT_CHARS:
            errors.append(f"{label}.new_text is too large")
        if "\n" in old_text or "\r" in old_text or "\n" in new_text or "\r" in new_text:
            errors.append(f"{label} may not contain line or paragraph boundaries")
        contained_fragments = [
            fragment for fragment in detected_fragments if fragment in old_text
        ]
        if not contained_fragments:
            errors.append(f"{label}.old_text contains no detected residual fragment")
        if residual_source_script_report(
            new_text,
            source_language,
            approved_reader_facing_terms,
            source_text=source_text,
        )["remaining_cjk_character_count"]:
            errors.append(
                f"{label}.new_text contains unexpected source-script text"
            )
        if len(new_text) - len(old_text) > MAX_INSERTED_CHARS_PER_REPAIR:
            errors.append(f"{label}.new_text expands the patch too much")
        if not _word_subsequence(old_text, new_text):
            errors.append(f"{label}.new_text rewrites unaffected English words")

        region_id = item.get("region_id")
        line_number = item.get("line_number")
        absolute_start = None
        if line_number is not None and (
            not isinstance(line_number, int) or isinstance(line_number, bool)
        ):
            errors.append(f"{label}.line_number must be an integer")
            line_number = None
        if line_number is not None and (
            line_number not in affected or line_number > len(lines)
        ):
            errors.append(f"{label} targets a line outside the allowed repair regions")

        candidate_regions = repair_regions
        if region_id is not None:
            if not isinstance(region_id, str) or region_id not in regions_by_id:
                errors.append(f"{label}.region_id is not an allowed repair region")
                candidate_regions = []
            else:
                candidate_regions = [regions_by_id[region_id]]
        if line_number is not None:
            candidate_regions = [
                region for region in candidate_regions
                if region["line_number"] == line_number
            ]
        matching_regions = []
        for region in candidate_regions:
            scope = (translation or "")[region["scope_start"]:region["scope_end"]]
            if scope.count(old_text) == 1:
                matching_regions.append((region, scope.index(old_text)))
        if len(matching_regions) != 1:
            if not matching_regions:
                errors.append(f"{label}.old_text is outside an allowed repair region")
            elif line_number is None and region_id is None:
                errors.append(
                    f"{label}.old_text is not globally unique and has no region anchor"
                )
            else:
                errors.append(f"{label}.old_text is not unique within its repair region")
        else:
            selected_region, relative_start = matching_regions[0]
            region_id = selected_region["region_id"]
            line_number = selected_region["line_number"]
            absolute_start = selected_region["scope_start"] + relative_start
        if absolute_start is not None:
            absolute_end = absolute_start + len(old_text)
            covered_occurrences = [
                occurrence for occurrence in eligibility["residual_cjk_occurrences"]
                if absolute_start <= occurrence["start"]
                and occurrence["end"] <= absolute_end
            ]
            if not covered_occurrences:
                errors.append(f"{label}.old_text does not cover a residual occurrence")
            if any(absolute_start < end and start < absolute_end for start, end in occupied):
                errors.append(f"{label} overlaps another repair")
            occupied.append((absolute_start, absolute_end))
            validated.append({
                "region_id": region_id,
                "line_number": line_number,
                "old_text": old_text,
                "new_text": new_text,
                "start": absolute_start,
                "end": absolute_end,
                "residual_fragments": [
                    occurrence["fragment"] for occurrence in covered_occurrences
                ],
                "repair_type": REPAIR_EVENT_TYPE,
            })
        total_patch_chars += len(old_text) + len(new_text)

    if total_patch_chars > MAX_TOTAL_PATCH_CHARS:
        errors.append("total patch size exceeds the micro-repair limit")
    if errors:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "diagnostic_repairs": _public_repairs(validated),
            "response_diagnostic": _response_diagnostic(parsed),
            "errors": list(dict.fromkeys(errors)),
        }

    repaired = translation or ""
    for item in sorted(validated, key=lambda value: value["start"], reverse=True):
        repaired = repaired[:item["start"]] + item["new_text"] + repaired[item["end"]:]
    remaining = residual_source_script_report(
        repaired,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    if remaining["remaining_cjk_character_count"]:
        return {
            "accepted": False,
            "repaired_text": translation,
            "repairs_applied": [],
            "diagnostic_repairs": _public_repairs(validated),
            "response_diagnostic": _response_diagnostic(parsed),
            "errors": ["candidate still contains unexpected residual source script"],
        }
    public_repairs = _public_repairs(validated)
    return {
        "accepted": True,
        "repaired_text": repaired,
        "repairs_applied": public_repairs,
        "diagnostic_repairs": public_repairs,
        "response_diagnostic": _response_diagnostic(parsed),
        "errors": [],
    }


def corrective_retry_eligibility(validation):
    """Allow one retry only for a fully bounded source-script-only rejection."""
    errors = list(validation.get("errors", []))
    allowed = []
    for error in errors:
        if re.fullmatch(
            r"repairs\[\d+\]\.new_text contains unexpected source-script text",
            error,
        ) or error == "candidate still contains unexpected residual source script":
            allowed.append(error)
    reasons = []
    if not errors:
        reasons.append("the candidate has no local validation rejection")
    if len(allowed) != len(errors):
        reasons.append("the rejection includes a non-correctable safety failure")
    if not validation.get("diagnostic_repairs"):
        reasons.append("the rejected response has no fully bounded repair candidate")
    return {
        "eligible": not reasons,
        "errors": errors,
        "correctable_errors": allowed,
        "reasons": reasons,
    }


def repair_operation_id(
    novel_id,
    chapter,
    pending_hash,
    policy_version,
    attempt_number,
):
    return (
        f"{REPAIR_EVENT_TYPE}:{novel_id}:{chapter}:"
        f"{pending_hash}:{policy_version}:{attempt_number}"
    )


def corrective_repair_operation_id(operation_id, corrective_attempt=1):
    """Return the stable accounting identity for the sole corrective request."""
    return f"{operation_id}:corrective:{corrective_attempt}"


def _combined_repair_stats(*items):
    usable = [deepcopy(item) for item in items if isinstance(item, dict)]
    if not usable:
        return None
    combined = deepcopy(usable[-1])
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cost_usd",
        "rate_correct_cost_usd",
    ):
        combined[key] = sum(item.get(key, 0) or 0 for item in usable)
    periods = {item.get("pricing_period") for item in usable if item.get("pricing_period")}
    if len(periods) > 1:
        combined["pricing_period"] = "mixed"
    combined["request_count"] = len(usable)
    return combined


def _account_recorded_attempt(account_api_usage, attempt):
    stats = attempt.get("stats")
    if isinstance(stats, dict):
        account_api_usage(attempt["operation_id"], stats)
    corrective = attempt.get("corrective_retry")
    if isinstance(corrective, dict) and isinstance(corrective.get("stats"), dict):
        account_api_usage(corrective["operation_id"], corrective["stats"])


def _attempts(meta, pending_hash):
    state = meta.get(ATTEMPT_STATE_KEY, {})
    by_hash = state.get("attempts_by_pending_hash", {})
    attempts = by_hash.get(pending_hash, [])
    return attempts if isinstance(attempts, list) else []


def attempt_policy_version(attempt):
    """Interpret unversioned historical records without mutating them."""
    version = attempt.get("policy_version") if isinstance(attempt, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return LEGACY_MICRO_REPAIR_POLICY_VERSION


def repair_attempts_for_policy(meta, pending_hash, policy_version):
    """Return attempts consumed by one content hash and deterministic policy."""
    return [
        attempt for attempt in _attempts(meta, pending_hash)
        if attempt_policy_version(attempt) == policy_version
    ]


def micro_repair_attempt_allowance(
    meta,
    pending_hash,
    policy_version,
    max_attempts,
):
    """Describe restart-safe paid-attempt allowance without writing state."""
    attempts = repair_attempts_for_policy(meta, pending_hash, policy_version)
    consumed = len(attempts)
    return {
        "policy_version": policy_version,
        "attempts_consumed": consumed,
        "max_attempts": max_attempts,
        "attempts_remaining": max(0, max_attempts - consumed),
        "allowed": consumed < max_attempts,
        "attempts": deepcopy(attempts),
    }


def _append_attempt(meta, pending_hash, attempt):
    updated = deepcopy(meta)
    state = deepcopy(updated.get(ATTEMPT_STATE_KEY, {}))
    by_hash = deepcopy(state.get("attempts_by_pending_hash", {}))
    attempts = list(by_hash.get(pending_hash, []))
    attempts.append(deepcopy(attempt))
    by_hash[pending_hash] = attempts
    state["attempts_by_pending_hash"] = by_hash
    updated[ATTEMPT_STATE_KEY] = state
    return updated


def _replace_attempt(meta, pending_hash, operation_id, values):
    updated = deepcopy(meta)
    state = deepcopy(updated.get(ATTEMPT_STATE_KEY, {}))
    by_hash = deepcopy(state.get("attempts_by_pending_hash", {}))
    attempts = list(by_hash.get(pending_hash, []))
    for index, attempt in enumerate(attempts):
        if attempt.get("operation_id") == operation_id:
            attempts[index] = {**attempt, **deepcopy(values)}
            break
    else:
        raise ValueError("micro-repair attempt record is missing")
    by_hash[pending_hash] = attempts
    state["attempts_by_pending_hash"] = by_hash
    updated[ATTEMPT_STATE_KEY] = state
    return updated


def _safe_log(log_event, event, **fields):
    try:
        log_event(event, **fields)
    except Exception:
        pass


def _recorded_revalidation_response(
    attempt, novel_id, chapter, pending_hash, policy_version, eligibility
):
    """Return one identity-complete saved diagnostic or fail closed."""
    if attempt.get("status") != "locally_rejected":
        return None
    attempt_number = attempt.get("attempt")
    if (
        not isinstance(attempt_number, int)
        or isinstance(attempt_number, bool)
        or attempt_number < 1
    ):
        return None
    expected_operation_id = repair_operation_id(
        novel_id, chapter, pending_hash, policy_version, attempt_number
    )
    if not (
        attempt.get("novel_id") == novel_id
        and attempt.get("chapter") == chapter
        and attempt.get("pending_content_hash") == pending_hash
        and attempt_policy_version(attempt) == policy_version
        and attempt.get("operation_id") == expected_operation_id
        and attempt.get("api_called") is True
        and isinstance(attempt.get("stats"), dict)
    ):
        return None
    errors = attempt.get("validation_errors")
    if not (
        isinstance(errors, list)
        and len(errors) == 1
        and UNAFFECTED_ENGLISH_REWRITE_ERROR_RE.fullmatch(errors[0])
        and errors[0].startswith("repairs[0].")
    ):
        return None
    diagnostic = attempt.get("response_diagnostic")
    if not (
        isinstance(diagnostic, dict)
        and set(diagnostic) == {
            "response_type", "repairs", "repair_count", "repairs_truncated"
        }
        and diagnostic.get("response_type") == "structured"
        and diagnostic.get("repair_count") == 1
        and diagnostic.get("repairs_truncated") is False
        and isinstance(diagnostic.get("repairs"), list)
        and len(diagnostic["repairs"]) == 1
    ):
        return None
    repair = diagnostic["repairs"][0]
    if not (
        isinstance(repair, dict)
        and set(repair) == {"region_id", "line_number", "old_text", "new_text"}
        and isinstance(repair.get("region_id"), str)
        and isinstance(repair.get("line_number"), int)
        and not isinstance(repair.get("line_number"), bool)
        and isinstance(repair.get("old_text"), str)
        and bool(repair["old_text"])
        and isinstance(repair.get("new_text"), str)
        and bool(repair["new_text"])
    ):
        return None
    regions = [
        region for region in eligibility.get("repair_regions", ())
        if region.get("region_id") == repair["region_id"]
        and region.get("line_number") == repair["line_number"]
    ]
    if len(regions) != 1:
        return None
    return {"repairs": [deepcopy(repair)]}


def _meta_with_recorded_candidate_revalidation(meta, pending_hash, provenance):
    """Add unbilled provenance without rewriting the historical attempt."""
    updated = deepcopy(meta)
    state = deepcopy(updated.get(ATTEMPT_STATE_KEY, {}))
    by_hash = deepcopy(
        state.get("recorded_candidate_revalidations_by_pending_hash", {})
    )
    records = list(by_hash.get(pending_hash, []))
    identity = (
        provenance.get("policy"),
        provenance.get("original_operation_id"),
        provenance.get("candidate_content_hash"),
    )
    if not any(
        (
            item.get("policy"),
            item.get("original_operation_id"),
            item.get("candidate_content_hash"),
        ) == identity
        for item in records
        if isinstance(item, dict)
    ):
        records.append(deepcopy(provenance))
    by_hash[pending_hash] = records
    state["recorded_candidate_revalidations_by_pending_hash"] = by_hash
    updated[ATTEMPT_STATE_KEY] = state
    return updated


def _try_revalidate_recorded_residual_cjk_candidate(
    *, novel_id, chapter, source_text, translation, meta, eligibility,
    relevant_locked_glossary, persist_meta, persist_translation,
    account_api_usage, qa_checker, approved_reader_facing_terms,
    policy_version, source_language, revalidation_policy_version, log_event,
):
    """Apply one exact paid diagnostic through the local hybrid-only policy."""
    pending_hash = eligibility["pending_content_hash"]
    attempts = repair_attempts_for_policy(meta, pending_hash, policy_version)
    # The v1 salvage policy deliberately refuses duplicated or multi-attempt
    # histories instead of choosing a convenient response.
    if len(attempts) != 1:
        return None
    previous = attempts[0]
    response = _recorded_revalidation_response(
        previous,
        novel_id,
        chapter,
        pending_hash,
        policy_version,
        eligibility,
    )
    if response is None:
        return None
    validation = validate_hybrid_lexical_unit_rewrite(
        translation,
        response,
        eligibility,
        relevant_locked_glossary,
        approved_reader_facing_terms,
        source_language,
        source_text=source_text,
    )
    if not validation["accepted"]:
        return None
    candidate = validation["repaired_text"]
    qa = qa_checker(candidate)
    if not qa.get("commit_allowed", False):
        return None
    before_report = residual_source_script_report(
        translation,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    after_report = residual_source_script_report(
        candidate,
        source_language,
        approved_reader_facing_terms,
        source_text=source_text,
    )
    if (
        after_report["remaining_cjk_character_count"] != 0
        or after_report["intentional_reader_facing_character_count"]
        != before_report["intentional_reader_facing_character_count"]
        or after_report["intentional_reader_facing_fragments"]
        != before_report["intentional_reader_facing_fragments"]
    ):
        return None

    _account_recorded_attempt(account_api_usage, previous)
    diff = validation["minimal_diff"]
    source_support = validation["source_compound_support"]
    candidate_hash = stable_text_hash(candidate)
    provenance = {
        "method": "recorded_candidate_revalidation",
        "policy": revalidation_policy_version,
        "original_operation_id": previous["operation_id"],
        "original_status": previous["status"],
        "original_validation_error": previous["validation_errors"][0],
        "region_id": response["repairs"][0]["region_id"],
        "line_number": response["repairs"][0]["line_number"],
        "minimal_old_span": diff["minimal_old_span"],
        "minimal_new_span": diff["minimal_new_span"],
        "unchanged_prefix": diff["unchanged_prefix"],
        "unchanged_suffix": diff["unchanged_suffix"],
        "source_compound_start": source_support["compound_start"],
        "source_compound_end": source_support["compound_end"],
        "source_compound_text": source_support["compound_text"],
        "source_identity": source_support["source_identity"],
        "candidate_content_hash": candidate_hash,
        "api_calls": 0,
    }
    repairs = []
    for repair in validation["repairs_applied"]:
        repairs.append({
            **deepcopy(repair),
            "recorded_candidate_revalidation": deepcopy(provenance),
        })
    repaired_meta = _meta_with_recorded_candidate_revalidation(
        meta, pending_hash, provenance
    )
    repaired_meta = pending_meta_after_residual_cjk_repair(
        repaired_meta, repairs
    )
    persist_meta(repaired_meta)
    persist_translation(candidate)
    _safe_log(
        log_event,
        REPAIR_EVENT_TYPE,
        chapter=chapter,
        stage="revalidated_recorded_diagnostic_candidate",
        operation_id=previous["operation_id"],
        policy_version=policy_version,
        revalidation_policy_version=revalidation_policy_version,
        api_cost=0,
        api_called=False,
        candidate_content_hash=candidate_hash,
        region_id=provenance["region_id"],
    )
    return {
        "status": "succeeded_from_revalidated_recorded_candidate",
        "translation": candidate,
        "meta": repaired_meta,
        "eligibility": eligibility,
        "api_called": False,
        "repair_stats": _combined_repair_stats(previous.get("stats")),
        "validation": validation,
        "qa": qa,
        "operation_id": previous["operation_id"],
        "policy_version": policy_version,
        "revalidation_policy_version": revalidation_policy_version,
        "revalidation_provenance": provenance,
    }


def revalidate_recorded_residual_cjk_candidate(
    *, novel_id, chapter, source_text, translation, meta, config,
    relevant_locked_glossary, persist_meta, persist_translation,
    account_api_usage, qa_checker, approved_reader_facing_terms=(),
    policy_version=MICRO_REPAIR_POLICY_VERSION, source_language="zh",
    revalidation_policy_version=HYBRID_LEXICAL_UNIT_REVALIDATION_POLICY_VERSION,
    log_event=lambda event, **fields: None,
):
    """Try only zero-API recovery of a recorded residual-CJK diagnostic."""
    eligibility = assess_micro_repair_eligibility(
        source_text,
        translation,
        config,
        approved_reader_facing_terms,
        source_language,
    )
    if not eligibility["eligible"] or not eligibility["remaining_cjk_character_count"]:
        return {
            "status": "no_revalidatable_recorded_candidate",
            "translation": translation,
            "meta": meta,
            "eligibility": eligibility,
            "api_called": False,
        }
    recovered = _try_revalidate_recorded_residual_cjk_candidate(
        novel_id=novel_id,
        chapter=chapter,
        source_text=source_text,
        translation=translation,
        meta=meta,
        eligibility=eligibility,
        relevant_locked_glossary=relevant_locked_glossary,
        persist_meta=persist_meta,
        persist_translation=persist_translation,
        account_api_usage=account_api_usage,
        qa_checker=qa_checker,
        approved_reader_facing_terms=approved_reader_facing_terms,
        policy_version=policy_version,
        source_language=source_language,
        revalidation_policy_version=revalidation_policy_version,
        log_event=log_event,
    )
    if recovered is not None:
        return recovered
    return {
        "status": "no_revalidatable_recorded_candidate",
        "translation": translation,
        "meta": meta,
        "eligibility": eligibility,
        "api_called": False,
    }


def perform_residual_cjk_micro_repair(
    *,
    novel_id,
    chapter,
    profile_id,
    target_language,
    source_text,
    translation,
    meta,
    config,
    relevant_locked_glossary,
    auxiliary_terminology_policy,
    request_api,
    persist_meta,
    persist_translation,
    account_api_usage,
    qa_checker,
    approved_reader_facing_terms=(),
    policy_version=MICRO_REPAIR_POLICY_VERSION,
    source_language="zh",
    log_event=lambda event, **fields: None,
    allow_corrective_retry=True,
):
    """Attempt one bounded patch plus at most one residual-only correction.

    The callback contract keeps API, filesystem, and project-state mutation in
    production. ``account_api_usage`` must be idempotent by operation ID.
    """
    deterministic_cleanup = deterministic_parenthetical_residual_cleanup(
        translation,
        source_language=source_language,
        target_language=target_language,
        approved_reader_facing_terms=approved_reader_facing_terms,
        source_text=source_text,
    )
    repair_translation = translation
    cleanup_qa = None
    if deterministic_cleanup["changed"]:
        cleanup_qa = qa_checker(deterministic_cleanup["cleaned_text"])
        non_residual_blockers = [
            blocker
            for blocker in cleanup_qa.get("commit_blockers", [])
            if blocker.get("type") not in {"residual_source_script", "residual_cjk"}
        ]
        if cleanup_qa.get("locked_high_priority_term_violations"):
            non_residual_blockers.append({"type": "locked_terminology"})
        if non_residual_blockers:
            return {
                "status": "deterministic_cleanup_rejected",
                "translation": translation,
                "meta": meta,
                "deterministic_cleanup": deterministic_cleanup,
                "qa": cleanup_qa,
                "api_called": False,
                "repair_stats": None,
                "policy_version": policy_version,
            }
        repair_translation = deterministic_cleanup["cleaned_text"]
        if deterministic_cleanup["after"]["remaining_cjk_character_count"] == 0:
            if not cleanup_qa.get("commit_allowed", False):
                return {
                    "status": "deterministic_cleanup_rejected",
                    "translation": translation,
                    "meta": meta,
                    "deterministic_cleanup": deterministic_cleanup,
                    "qa": cleanup_qa,
                    "api_called": False,
                    "repair_stats": None,
                    "policy_version": policy_version,
                }
            repaired_meta = pending_meta_after_residual_parenthetical_cleanup(
                meta, deterministic_cleanup["removals"]
            )
            persist_meta(repaired_meta)
            persist_translation(repair_translation)
            for removal in deterministic_cleanup["removals"]:
                _safe_log(
                    log_event,
                    "deterministic_residual_parenthetical_cleanup",
                    chapter=chapter,
                    stage="local_qa",
                    line_number=removal["line_number"],
                    annotation=removal["annotation"],
                    policy_version=PARENTHETICAL_CLEANUP_POLICY_VERSION,
                    api_cost=0,
                )
            return {
                "status": "succeeded_deterministic_cleanup",
                "translation": repair_translation,
                "meta": repaired_meta,
                "deterministic_cleanup": deterministic_cleanup,
                "qa": cleanup_qa,
                "api_called": False,
                "repair_stats": None,
                "policy_version": policy_version,
            }

    eligibility = assess_micro_repair_eligibility(
        source_text,
        repair_translation,
        config,
        approved_reader_facing_terms,
        source_language,
    )
    if eligibility["remaining_cjk_character_count"] == 0:
        return {
            "status": "clean", "translation": translation, "meta": meta,
            "eligibility": eligibility, "api_called": False, "repair_stats": None,
            "deterministic_cleanup": deterministic_cleanup,
            "policy_version": policy_version,
        }
    if not eligibility["eligible"]:
        return {
            "status": "ineligible", "translation": translation, "meta": meta,
            "eligibility": eligibility, "api_called": False, "repair_stats": None,
            "deterministic_cleanup": deterministic_cleanup,
            "policy_version": policy_version,
        }

    pending_hash = eligibility["pending_content_hash"]
    attempts = repair_attempts_for_policy(meta, pending_hash, policy_version)
    raw_attempts = _attempts(meta, pending_hash)
    policy_identity_conflicts = []
    if not attempts:
        for raw_attempt in raw_attempts:
            raw_number = raw_attempt.get("attempt")
            if (
                isinstance(raw_number, int)
                and not isinstance(raw_number, bool)
                and raw_number >= 1
                and raw_attempt.get("operation_id") == repair_operation_id(
                    novel_id,
                    chapter,
                    pending_hash,
                    policy_version,
                    raw_number,
                )
            ):
                policy_identity_conflicts.append(raw_attempt)
    if policy_identity_conflicts:
        previous = policy_identity_conflicts[-1]
        _account_recorded_attempt(account_api_usage, previous)
        return {
            "status": "attempt_limit_reached",
            "translation": translation,
            "meta": meta,
            "eligibility": eligibility,
            "api_called": False,
            "repair_stats": _combined_repair_stats(previous.get("stats")),
            "policy_version": policy_version,
            "identity_error": "recorded attempt policy identity is inconsistent",
        }
    if not deterministic_cleanup["changed"]:
        recovered = _try_revalidate_recorded_residual_cjk_candidate(
            novel_id=novel_id,
            chapter=chapter,
            source_text=source_text,
            translation=repair_translation,
            meta=meta,
            eligibility=eligibility,
            relevant_locked_glossary=relevant_locked_glossary,
            persist_meta=persist_meta,
            persist_translation=persist_translation,
            account_api_usage=account_api_usage,
            qa_checker=qa_checker,
            approved_reader_facing_terms=approved_reader_facing_terms,
            policy_version=policy_version,
            source_language=source_language,
            revalidation_policy_version=(
                HYBRID_LEXICAL_UNIT_REVALIDATION_POLICY_VERSION
            ),
            log_event=log_event,
        )
        if recovered is not None:
            recovered["deterministic_cleanup"] = deterministic_cleanup
            return recovered
    if attempts and attempts[-1].get("status") in {"candidate_validated", "applied"}:
        previous = attempts[-1]
        saved_repairs = previous.get("repairs", [])
        replay = validate_and_apply_repair_response(
            repair_translation,
            {"repairs": saved_repairs},
            eligibility,
            approved_reader_facing_terms,
            source_language,
            source_text=source_text,
        )
        if replay["accepted"] and stable_text_hash(replay["repaired_text"]) == (
            previous.get("candidate_content_hash")
        ):
            qa = qa_checker(replay["repaired_text"])
            if qa.get("commit_allowed", False):
                _account_recorded_attempt(account_api_usage, previous)
                repaired_meta = pending_meta_after_residual_cjk_repair(
                    meta, replay["repairs_applied"]
                )
                if deterministic_cleanup["changed"]:
                    repaired_meta = pending_meta_after_residual_parenthetical_cleanup(
                        repaired_meta, deterministic_cleanup["removals"]
                    )
                repaired_meta = _replace_attempt(
                    repaired_meta,
                    pending_hash,
                    previous["operation_id"],
                    {"status": "applied", "candidate_qa": deepcopy(qa)},
                )
                persist_meta(repaired_meta)
                persist_translation(replay["repaired_text"])
                return {
                    "status": "succeeded_from_recorded_candidate",
                    "translation": replay["repaired_text"],
                    "meta": repaired_meta,
                    "eligibility": eligibility,
                    "deterministic_cleanup": deterministic_cleanup,
                    "api_called": False,
                    "repair_stats": _combined_repair_stats(
                        previous.get("stats"),
                        (previous.get("corrective_retry") or {}).get("stats"),
                    ),
                    "validation": replay,
                    "qa": qa,
                    "operation_id": previous["operation_id"],
                    "policy_version": policy_version,
                }
    limit = _setting(config, "max_attempts_per_pending_version")
    resume_transport = bool(attempts and resumable_attempt(attempts[-1], request_api))
    if len(attempts) >= limit and not resume_transport:
        if attempts:
            _account_recorded_attempt(account_api_usage, attempts[-1])
        return {
            "status": "attempt_limit_reached", "translation": translation,
            "meta": meta, "eligibility": eligibility, "api_called": False,
            "repair_stats": (
                _combined_repair_stats(
                    attempts[-1].get("stats"),
                    (attempts[-1].get("corrective_retry") or {}).get("stats"),
                )
                if attempts else None
            ),
            "policy_version": policy_version,
        }

    attempt_number = attempts[-1]["attempt"] if resume_transport else len(attempts) + 1
    operation_id = repair_operation_id(
        novel_id, chapter, pending_hash, policy_version, attempt_number
    )
    attempt = {
        "attempt": attempt_number,
        "operation_id": operation_id,
        "novel_id": novel_id,
        "chapter": chapter,
        "pending_content_hash": pending_hash,
        "policy_version": policy_version,
        **planned_attempt(request_api),
    }
    normalized_provenance = [
        deepcopy(region["source_anchor_provenance"])
        for region in eligibility["repair_regions"]
        if region.get("source_anchor_provenance")
    ]
    if normalized_provenance:
        attempt["source_anchor_provenance"] = normalized_provenance
    current_meta = meta if resume_transport else _append_attempt(meta, pending_hash, attempt)
    if not resume_transport:
        persist_meta(current_meta)

    request = build_micro_repair_request(
        novel_id=novel_id,
        profile_id=profile_id,
        target_language=target_language,
        source_text=source_text,
        translation=repair_translation,
        eligibility=eligibility,
        relevant_locked_glossary=relevant_locked_glossary,
        auxiliary_terminology_policy=auxiliary_terminology_policy,
        approved_reader_facing_terms=approved_reader_facing_terms,
        policy_version=policy_version,
        source_language=source_language,
    )
    try:
        api_result = invoke_request(request_api, request, operation_id)
        response, stats = api_result
    except Exception as exc:
        transport_failure = failure_values(exc, next(
            item for item in _attempts(current_meta, pending_hash)
            if item["operation_id"] == operation_id))
        if transport_failure.get("stats"):
            account_api_usage(operation_id, transport_failure["stats"])
        current_meta = _replace_attempt(
            current_meta, pending_hash, operation_id,
            {
                **transport_failure,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            },
        )
        persist_meta(current_meta)
        _safe_log(
            log_event, REPAIR_EVENT_TYPE, chapter=chapter, stage="api_failed",
            operation_id=operation_id, error_type=type(exc).__name__,
            error_message=str(exc)[:500], api_cost=transport_failure.get("stats", {}).get("cost_usd", 0),
            policy_version=policy_version,
        )
        return {
            "status": "api_failed", "translation": translation,
            "meta": current_meta, "eligibility": eligibility, "api_called": transport_failure["api_called"],
            "repair_stats": transport_failure.get("stats"), "error": str(exc),
            "error_type": type(exc).__name__, "policy_version": policy_version,
        }

    validation = validate_and_apply_repair_response(
        repair_translation,
        response,
        eligibility,
        approved_reader_facing_terms,
        source_language,
        source_text=source_text,
    )
    attempt_values = {
        **success_values(stats),
        "status": "candidate_validated" if validation["accepted"] else "locally_rejected",
        "stats": deepcopy(stats),
        "validation_errors": validation["errors"],
        "response_diagnostic": deepcopy(validation["response_diagnostic"]),
    }
    if validation["accepted"]:
        attempt_values["repairs"] = [
            {
                "region_id": item["region_id"],
                "line_number": item["line_number"],
                "old_text": item["old_text"],
                "new_text": item["new_text"],
            }
            for item in validation["repairs_applied"]
        ]
        attempt_values["candidate_content_hash"] = stable_text_hash(
            validation["repaired_text"]
        )
    current_meta = _replace_attempt(
        current_meta, pending_hash, operation_id, attempt_values
    )
    persist_meta(current_meta)
    account_api_usage(operation_id, stats)

    _safe_log(
        log_event,
        REPAIR_EVENT_TYPE,
        chapter=chapter,
        stage=attempt_values["status"],
        operation_id=operation_id,
        residual_cjk_fragments=eligibility["residual_cjk_fragments"],
        prompt_tokens=stats.get("prompt_tokens", 0),
        completion_tokens=stats.get("completion_tokens", 0),
        cache_hit_tokens=stats.get("cache_hit_tokens", 0),
        cache_miss_tokens=stats.get("cache_miss_tokens", 0),
        calculated_cost=stats.get("cost_usd", 0),
        pricing_period=stats.get("pricing_period"),
        api_cost=stats.get("cost_usd", 0),
        validation_errors=validation["errors"],
        source_anchor_provenance=normalized_provenance,
        policy_version=policy_version,
    )

    corrective_stats = None
    corrective_operation_id = None
    if not validation["accepted"]:
        retry_eligibility = corrective_retry_eligibility(validation)
        if allow_corrective_retry and retry_eligibility["eligible"]:
            corrective_operation_id = corrective_repair_operation_id(operation_id)
            corrective_record = {
                "attempt": 1,
                "operation_id": corrective_operation_id,
                **planned_attempt(request_api),
                "initial_validation_errors": deepcopy(validation["errors"]),
                "initial_response_diagnostic": deepcopy(
                    validation["response_diagnostic"]
                ),
            }
            if resume_transport and attempts[-1].get("corrective_retry"):
                corrective_record = deepcopy(attempts[-1]["corrective_retry"])
            current_meta = _replace_attempt(
                current_meta,
                pending_hash,
                operation_id,
                {
                    "status": "corrective_retry_started",
                    "corrective_retry": corrective_record,
                },
            )
            persist_meta(current_meta)
            corrective_request = build_corrective_micro_repair_request(
                request, validation
            )
            try:
                corrective_response, corrective_stats = invoke_request(
                    request_api, corrective_request, corrective_operation_id
                )
            except Exception as exc:
                transport_failure = failure_values(exc, corrective_record)
                if transport_failure.get("stats"):
                    account_api_usage(corrective_operation_id, transport_failure["stats"])
                corrective_record.update({
                    **transport_failure,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                })
                current_meta = _replace_attempt(
                    current_meta,
                    pending_hash,
                    operation_id,
                    {
                        "status": "api_failed",
                        "corrective_retry": corrective_record,
                    },
                )
                persist_meta(current_meta)
                _safe_log(
                    log_event,
                    REPAIR_EVENT_TYPE,
                    chapter=chapter,
                    stage="corrective_api_failed",
                    operation_id=corrective_operation_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    api_cost=transport_failure.get("stats", {}).get("cost_usd", 0),
                    policy_version=policy_version,
                )
                return {
                    "status": "api_failed",
                    "translation": translation,
                    "meta": current_meta,
                    "eligibility": eligibility,
                    "api_called": True,
                    "repair_stats": _combined_repair_stats(stats, transport_failure.get("stats")),
                    "validation": validation,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "operation_id": operation_id,
                    "corrective_operation_id": corrective_operation_id,
                    "policy_version": policy_version,
                }

            corrective_validation = validate_and_apply_repair_response(
                repair_translation,
                corrective_response,
                eligibility,
                approved_reader_facing_terms,
                source_language,
                source_text=source_text,
            )
            corrective_record.update({
                **success_values(corrective_stats),
                "status": (
                    "candidate_validated"
                    if corrective_validation["accepted"]
                    else "locally_rejected"
                ),
                "stats": deepcopy(corrective_stats),
                "validation_errors": corrective_validation["errors"],
                "response_diagnostic": deepcopy(
                    corrective_validation["response_diagnostic"]
                ),
            })
            final_values = {
                "status": corrective_record["status"],
                "validation_errors": corrective_validation["errors"],
                "corrective_retry": corrective_record,
            }
            if corrective_validation["accepted"]:
                final_values["repairs"] = [
                    {
                        "region_id": item["region_id"],
                        "line_number": item["line_number"],
                        "old_text": item["old_text"],
                        "new_text": item["new_text"],
                    }
                    for item in corrective_validation["repairs_applied"]
                ]
                final_values["candidate_content_hash"] = stable_text_hash(
                    corrective_validation["repaired_text"]
                )
            current_meta = _replace_attempt(
                current_meta,
                pending_hash,
                operation_id,
                final_values,
            )
            persist_meta(current_meta)
            account_api_usage(corrective_operation_id, corrective_stats)
            _safe_log(
                log_event,
                REPAIR_EVENT_TYPE,
                chapter=chapter,
                stage=f"corrective_{corrective_record['status']}",
                operation_id=corrective_operation_id,
                residual_cjk_fragments=eligibility["residual_cjk_fragments"],
                prompt_tokens=corrective_stats.get("prompt_tokens", 0),
                completion_tokens=corrective_stats.get("completion_tokens", 0),
                cache_hit_tokens=corrective_stats.get("cache_hit_tokens", 0),
                cache_miss_tokens=corrective_stats.get("cache_miss_tokens", 0),
                calculated_cost=corrective_stats.get("cost_usd", 0),
                pricing_period=corrective_stats.get("pricing_period"),
                api_cost=corrective_stats.get("cost_usd", 0),
                validation_errors=corrective_validation["errors"],
                policy_version=policy_version,
            )
            validation = corrective_validation

    if not validation["accepted"]:
        return {
            "status": "locally_rejected", "translation": translation,
            "meta": current_meta, "eligibility": eligibility, "api_called": True,
            "repair_stats": _combined_repair_stats(stats, corrective_stats),
            "validation": validation,
            "initial_validation_errors": attempt_values["validation_errors"],
            "operation_id": operation_id,
            "corrective_operation_id": corrective_operation_id,
            "policy_version": policy_version,
        }

    candidate = validation["repaired_text"]
    qa = qa_checker(candidate)
    if not qa.get("commit_allowed", False):
        current_meta = _replace_attempt(
            current_meta, pending_hash, operation_id,
            {"status": "qa_rejected", "candidate_qa": deepcopy(qa)},
        )
        persist_meta(current_meta)
        return {
            "status": "qa_rejected", "translation": translation,
            "meta": current_meta, "eligibility": eligibility, "api_called": True,
            "repair_stats": _combined_repair_stats(stats, corrective_stats),
            "validation": validation, "qa": qa,
            "operation_id": operation_id,
            "corrective_operation_id": corrective_operation_id,
            "policy_version": policy_version,
        }

    repaired_meta = pending_meta_after_residual_cjk_repair(
        current_meta, validation["repairs_applied"]
    )
    if deterministic_cleanup["changed"]:
        repaired_meta = pending_meta_after_residual_parenthetical_cleanup(
            repaired_meta, deterministic_cleanup["removals"]
        )
    repaired_meta = _replace_attempt(
        repaired_meta, pending_hash, operation_id,
        {"status": "applied", "candidate_qa": deepcopy(qa)},
    )
    # Metadata is invalidated first. If the atomic text write is interrupted,
    # the old paid text is still authoritative and the attempt record prevents
    # another paid call for the same hash.
    persist_meta(repaired_meta)
    persist_translation(candidate)
    return {
        "status": "succeeded", "translation": candidate,
        "meta": repaired_meta, "eligibility": eligibility, "api_called": True,
        "deterministic_cleanup": deterministic_cleanup,
        "repair_stats": _combined_repair_stats(stats, corrective_stats),
        "validation": validation, "qa": qa,
        "operation_id": operation_id,
        "corrective_operation_id": corrective_operation_id,
        "policy_version": policy_version,
    }
