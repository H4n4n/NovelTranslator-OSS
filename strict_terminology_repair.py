"""Import-safe, bounded strict-terminology micro-repair orchestration.

This module follows the existing residual-CJK repair architecture: production
supplies its scheduled API callback, idempotent accounting, atomic pending
writes, logging, and mandatory local QA.  No credentials, project paths, API
client, or import-time I/O live here.
"""

from copy import deepcopy

from production_auxiliary_paid_request import (invoke_request, planned_attempt, resumable_attempt, failure_values, success_values)
import json
import re

from noveltranslator_core import (
    STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD,
    _claim_longest_non_overlapping_occurrences,
    _strict_occurrence_inventory,
    clean_json_text,
    deterministic_strict_terminology_repair,
    effective_strict_glossary_entry_items,
    locked_high_priority_term_violations,
    pending_meta_after_strict_terminology_repair,
    qa_paragraph_segment_spans,
    repair_eligible_strict_glossary_entry_items,
    residual_cjk_report,
    is_exact_strict_term_possessive_normalization,
    stable_text_hash,
    strict_repair_localization_anchor_mappings,
    strict_term_repair_aliases,
    strict_term_valid_targets,
    touches_strict_term_possessive_morphology,
)


ATTEMPT_STATE_KEY = "strict_terminology_micro_repair"
REPAIR_EVENT_TYPE = "strict_terminology_micro_repair"
MICRO_REPAIR_POLICY_VERSION = "strict-terminology-v7"
MAX_ATTEMPTS_PER_POLICY = 2
MAX_FAILURES_PER_REQUEST = 3
MAX_REGIONS_PER_REQUEST = 6
MAX_REGION_CHARS = 600
MAX_OLD_TEXT_CHARS = 180
MAX_NEW_TEXT_CHARS = 240
MAX_TOTAL_PATCH_CHARS = 600
MAX_INSERTED_CHARS = 96
MAX_UNALIASED_OLD_TEXT_WORDS = 6
REGION_CONTEXT_CHARS = 120
EMPTY_PATCH_VALIDATION_ERROR = "patches must not be empty while strict failures remain"
INEFFECTIVE_CANONICAL_DELTA_CODE = "ineffective_canonical_target_delta"
EMPTY_PATCH_CORRECTION_REASON = "exact_empty_patch_list"
INEFFECTIVE_PATCH_CORRECTION_REASON = "safe_ineffective_canonical_delta"
BOUNDED_COMPLIANCE_CORRECTION_REASON = "bounded_instruction_compliance"
BOUNDED_COMPLIANCE_ERROR_CODES = frozenset({
    "canonical_target_occurrence_count",
    INEFFECTIVE_CANONICAL_DELTA_CODE,
    "old_text_word_limit",
    "missing_canonical_target_delta",
    "protected_target_span_overlap",
})
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)?")


def _violation_severity(violations):
    return sum(item.get("missing_canonical_count", 1) for item in violations)


def _line_break_sequence(text):
    """Return exact line separators so repairs cannot reshape paragraphs."""
    return tuple(re.findall(r"\r\n|\r|\n", text or ""))


def _strict_qa_transition(baseline_qa, candidate_qa, eligibility):
    """Classify a fully clean or safely partial strict-repair candidate.

    Partial acceptance removes only the explicitly targeted strict blockers
    from the baseline inventory. Every unrelated blocker and non-target strict
    violation must remain exactly equal, including residual counts/fragments.
    """
    if candidate_qa.get("commit_allowed", False):
        return {
            "accepted": True,
            "partial": False,
            "reason": "mandatory_qa_clean",
        }

    targeted_sources = {
        item.get("chinese")
        for item in eligibility.get("eligible_failures", [])
        if isinstance(item, dict) and item.get("chinese")
    }
    expected_baseline_strict = eligibility.get("violations")
    baseline_strict = baseline_qa.get("locked_high_priority_term_violations")
    candidate_strict = candidate_qa.get("locked_high_priority_term_violations")
    baseline_blockers = baseline_qa.get("commit_blockers")
    candidate_blockers = candidate_qa.get("commit_blockers")
    if (
        not targeted_sources
        or not isinstance(expected_baseline_strict, list)
        or not isinstance(baseline_strict, list)
        or not isinstance(candidate_strict, list)
        or not isinstance(baseline_blockers, list)
        or not isinstance(candidate_blockers, list)
    ):
        return {
            "accepted": False,
            "partial": False,
            "reason": "mandatory_qa_inventory_missing",
        }
    if baseline_strict != expected_baseline_strict:
        return {
            "accepted": False,
            "partial": False,
            "reason": "baseline_strict_inventory_mismatch",
        }
    baseline_sources = {
        item.get("chinese") for item in baseline_strict if isinstance(item, dict)
    }
    if not targeted_sources <= baseline_sources:
        return {
            "accepted": False,
            "partial": False,
            "reason": "targeted_strict_blocker_missing_from_baseline",
        }
    if any(
        item.get("chinese") in targeted_sources
        for item in candidate_strict
        if isinstance(item, dict)
    ):
        return {
            "accepted": False,
            "partial": False,
            "reason": "targeted_strict_blocker_remains",
        }

    baseline_unrelated_strict = [
        item for item in baseline_strict
        if item.get("chinese") not in targeted_sources
    ]
    candidate_unrelated_strict = [
        item for item in candidate_strict
        if item.get("chinese") not in targeted_sources
    ]
    if candidate_unrelated_strict != baseline_unrelated_strict:
        return {
            "accepted": False,
            "partial": False,
            "reason": "unrelated_strict_inventory_changed",
        }

    def unrelated(blockers):
        return [
            item for item in blockers
            if not (
                isinstance(item, dict)
                and item.get("type") == "locked_terminology"
                and item.get("chinese") in targeted_sources
            )
        ]

    baseline_unrelated = unrelated(baseline_blockers)
    candidate_unrelated = unrelated(candidate_blockers)
    if not candidate_unrelated:
        return {
            "accepted": False,
            "partial": False,
            "reason": "candidate_failed_qa_without_identified_unrelated_blocker",
        }
    if candidate_unrelated != baseline_unrelated:
        return {
            "accepted": False,
            "partial": False,
            "reason": "unrelated_mandatory_blocker_inventory_changed",
            "baseline_unrelated_blockers": deepcopy(baseline_unrelated),
            "candidate_unrelated_blockers": deepcopy(candidate_unrelated),
        }
    return {
        "accepted": True,
        "partial": True,
        "reason": "targeted_strict_blockers_resolved_unrelated_blockers_unchanged",
        "targeted_sources": sorted(targeted_sources),
        "baseline_unrelated_blockers": deepcopy(baseline_unrelated),
        "candidate_unrelated_blockers": deepcopy(candidate_unrelated),
    }


def perform_deterministic_strict_terminology_repair(
    *, source_text, translation, meta, glossary_entries,
    high_priority_categories, persist_meta, persist_translation, qa_checker,
    approved_reader_facing_terms=(), log_event=lambda event, **fields: None,
    chapter_number=None,
):
    """Validate and atomically persist a zero-cost eligible terminology repair."""
    before = locked_high_priority_term_violations(
        source_text, translation, glossary_entries, high_priority_categories,
        chapter_number=chapter_number,
    )
    result = deterministic_strict_terminology_repair(
        source_text,
        translation,
        glossary_entries,
        high_priority_categories,
        approved_reader_facing_cjk_terms=approved_reader_facing_terms,
        chapter_number=chapter_number,
    )
    if not result["repairs_applied"]:
        return {
            "status": "unchanged",
            "translation": translation,
            "meta": meta,
            "repairs_applied": [],
            "ambiguous_terms": result["ambiguous_terms"],
            "qa": qa_checker(translation),
        }
    candidate = result["repaired_text"]
    after = result["unresolved_strict_terms"]
    before_cjk = residual_cjk_report(translation, approved_reader_facing_terms)
    after_cjk = residual_cjk_report(candidate, approved_reader_facing_terms)
    if (
        _violation_severity(after) >= _violation_severity(before)
        or after_cjk["remaining_cjk_character_count"] > before_cjk["remaining_cjk_character_count"]
        or _line_break_sequence(candidate) != _line_break_sequence(translation)
    ):
        return {
            "status": "rejected",
            "translation": translation,
            "meta": meta,
            "repairs_applied": [],
            "ambiguous_terms": result["ambiguous_terms"],
            "qa": qa_checker(translation),
        }

    original_hash = stable_text_hash(translation)
    repaired_hash = stable_text_hash(candidate)
    repairs = []
    for raw in result["repairs_applied"]:
        repair = {
            **raw,
            "original_pending_hash": original_hash,
            "repaired_pending_hash": repaired_hash,
        }
        repairs.append(repair)
    qa = qa_checker(candidate)
    partial_strict_improvement = False
    if not qa.get("commit_allowed", False):
        blockers = qa.get("commit_blockers", [])
        qa_strict = qa.get("locked_high_priority_term_violations", [])
        expected_blockers = [
            {"type": "locked_terminology", **item}
            for item in after
        ]
        partial_strict_improvement = bool(
            after
            and blockers == expected_blockers
            and qa_strict == after
        )
        if not partial_strict_improvement:
            # Reuse the paid-patch transition proof for a zero-cost exact alias
            # repair too. Unchanged residual CJK must not force a paid strict
            # repair before the normal CJK stage can run. Every unrelated QA
            # blocker, including CJK counts/fragments, must remain identical.
            unresolved_sources = {item["chinese"] for item in after}
            transition = _strict_qa_transition(qa_checker(translation), qa, {
                "violations": before,
                "eligible_failures": [item for item in before
                                      if item["chinese"] not in unresolved_sources],
            })
            partial_strict_improvement = transition["accepted"]
        if not partial_strict_improvement:
            return {
                "status": "qa_rejected",
                "translation": translation,
                "meta": meta,
                "repairs_applied": [],
                "ambiguous_terms": result["ambiguous_terms"],
                "qa": qa,
            }
    repaired_meta = pending_meta_after_strict_terminology_repair(meta, repairs)
    # Metadata is invalidated first. The authoritative pending text is then
    # replaced atomically by the production-supplied callback.
    persist_meta(repaired_meta)
    persist_translation(candidate)
    for repair in repairs:
        _safe_log(
            log_event,
            "deterministic_strict_terminology_repair",
            chinese_term=repair["chinese_term"],
            old_variant=repair["old_variant"],
            canonical_english=repair["canonical_english"],
            alias_origin=repair["alias_origin"],
            replacement_count=repair["replacement_count"],
            original_pending_hash=original_hash,
            repaired_pending_hash=repaired_hash,
            repair_type=repair["repair_type"],
            api_cost=0,
        )
    return {
        "status": (
            "partial_succeeded"
            if partial_strict_improvement
            else "succeeded"
        ),
        "translation": candidate,
        "meta": repaired_meta,
        "repairs_applied": repairs,
        "ambiguous_terms": result["ambiguous_terms"],
        "qa": qa,
    }


def _source_context(source, term):
    position = (source or "").find(term)
    if position < 0:
        return ""
    return (source or "")[
        max(0, position - REGION_CONTEXT_CHARS):
        min(len(source or ""), position + len(term) + REGION_CONTEXT_CHARS)
    ]


def _line_bounds(text, position):
    start = (text or "").rfind("\n", 0, position) + 1
    end = (text or "").find("\n", position)
    if end < 0:
        end = len(text or "")
    return start, end


def _phrase_pattern(phrase):
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )


def _line_records(text):
    """Return physical line text and exact offsets for LF or CRLF content."""
    records = []
    offset = 0
    for raw_line in (text or "").splitlines(keepends=True):
        if raw_line.endswith("\r\n"):
            line = raw_line[:-2]
        elif raw_line.endswith(("\r", "\n")):
            line = raw_line[:-1]
        else:
            line = raw_line
        records.append((offset, line))
        offset += len(raw_line)
    if text and not records:
        records.append((0, text))
    return records


def _owned_occurrence_line_counts(records, occurrences):
    """Group already longest-owned strict occurrences by physical line."""
    counts = {}
    for occurrence in occurrences or ():
        start = occurrence.get("start")
        end = occurrence.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        for index, (line_start, line) in enumerate(records):
            if line_start <= start and end <= line_start + len(line):
                counts[index] = counts.get(index, 0) + 1
                break
        else:
            return None
    return counts


def _owned_occurrence_segment_counts(segments, occurrences):
    """Group already longest-owned strict occurrences by QA segment."""
    counts = {}
    for occurrence in occurrences or ():
        start = occurrence.get("start")
        end = occurrence.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        for index, segment in enumerate(segments):
            if segment["start"] <= start and end <= segment["end"]:
                counts[index] = counts.get(index, 0) + 1
                break
        else:
            return None
    return counts


def _line_index_for_offset(records, offset):
    for index, (line_start, line) in enumerate(records):
        if line_start <= offset <= line_start + len(line):
            return index
    return None


def _aligned_segment_localization_region(
    source,
    translation,
    source_term,
    entry,
    missing_count,
    source_anchor_count,
    accepted_target_count,
    source_occurrences,
    target_occurrences,
):
    """Return one exact aligned segment corroborated by configured partial forms.

    This fallback requires exact segment and physical-line structure, matching
    accepted-term groups on both sides of one deficit, no displaced accepted
    groups, and exactly the configured insufficient target anchor in the
    candidate segment. Any paragraph drift or ambiguous partial anchor keeps
    the failure ineligible.
    """
    mappings = strict_repair_localization_anchor_mappings(entry)
    if not mappings or missing_count != 1:
        return None
    anchors = tuple(item["anchor"] for item in mappings)
    source_segments = qa_paragraph_segment_spans(source)
    target_segments = qa_paragraph_segment_spans(translation)
    source_records = _line_records(source)
    target_records = _line_records(translation)
    if (
        not source_segments
        or len(source_segments) != len(target_segments)
        or len(source_records) != len(target_records)
    ):
        return None
    source_counts = _owned_occurrence_segment_counts(
        source_segments, source_occurrences
    )
    target_counts = _owned_occurrence_segment_counts(
        target_segments, target_occurrences
    )
    if (
        source_counts is None
        or target_counts is None
        or sum(source_counts.values()) != source_anchor_count
        or sum(target_counts.values()) != accepted_target_count
        or len(source_counts) <= 1
        or not set(target_counts).issubset(source_counts)
    ):
        return None

    deficits = []
    matched_indexes = []
    for segment_index, source_count in sorted(source_counts.items()):
        target_count = target_counts.get(segment_index, 0)
        if target_count > source_count:
            return None
        deficit = source_count - target_count
        if deficit:
            deficits.append((segment_index, source_count, target_count, deficit))
        else:
            matched_indexes.append(segment_index)
    if (
        len(deficits) != 1
        or deficits[0][3] != missing_count
        or sum(item[3] for item in deficits) != missing_count
    ):
        return None
    segment_index, source_count, target_count, _ = deficits[0]
    if (
        not any(index < segment_index for index in matched_indexes)
        or not any(index > segment_index for index in matched_indexes)
    ):
        return None

    # Every subject-term segment must begin on the same physical line ordinal.
    # This rejects split/merged paragraphs even if total segment counts happen
    # to coincide elsewhere in the chapter.
    for index in source_counts:
        source_line_index = _line_index_for_offset(
            source_records, source_segments[index]["start"]
        )
        target_line_index = _line_index_for_offset(
            target_records, target_segments[index]["start"]
        )
        if source_line_index is None or source_line_index != target_line_index:
            return None

    source_segment = source_segments[segment_index]
    target_segment = target_segments[segment_index]
    affected_text = translation[target_segment["start"]:target_segment["end"]]
    source_context = source[source_segment["start"]:source_segment["end"]]
    if (
        not affected_text
        or len(affected_text) > MAX_REGION_CHARS
        or "\n" in affected_text
        or "\r" in affected_text
        or "\n" in source_context
        or "\r" in source_context
    ):
        return None

    claims = _claim_longest_non_overlapping_occurrences(
        affected_text,
        [("configured_partial", tuple(anchors))],
        bounded_ascii=True,
    ).get("configured_partial", [])
    protected_global_spans = [
        (item["start"], item["end"])
        for item in target_occurrences or ()
        if (
            target_segment["start"]
            <= item["start"]
            < item["end"]
            <= target_segment["end"]
        )
    ]
    claims = [
        item for item in claims
        if not any(
            target_segment["start"] + item["start"] < protected_end
            and protected_start < target_segment["start"] + item["end"]
            for protected_start, protected_end in protected_global_spans
        )
    ]
    if len(claims) != 1:
        return None
    mapping_by_anchor = {item["anchor"]: item["target"] for item in mappings}
    permitted_target = mapping_by_anchor.get(claims[0]["phrase"])
    if not permitted_target:
        return None
    return {
        "source_term": source_term,
        "required_english": (entry.get("english", "") or "").strip(),
        "alias": "",
        "alias_origin": "aligned_segment_localization_anchor",
        "scope_start": target_segment["start"],
        "scope_end": target_segment["end"],
        "affected_text": affected_text,
        "source_context": source_context,
        "source_occurrence_count_in_region": source_count,
        "accepted_target_count_in_region": target_count,
        "permitted_repair_targets": [permitted_target],
        "localization_anchor_matches": [
            {
                "relative_start": item["start"],
                "relative_end": item["end"],
                "text": item["phrase"],
                "target": mapping_by_anchor[item["phrase"]],
            }
            for item in claims
        ],
    }


def _attach_region_target_protection(region, translation, target_occurrences):
    """Attach QA-owned target spans and their bounded editable complement."""
    scope_start = region["scope_start"]
    scope_end = region["scope_end"]
    affected_text = region["affected_text"]
    protected = []
    for occurrence in target_occurrences or ():
        start = occurrence.get("start")
        end = occurrence.get("end")
        phrase = occurrence.get("phrase")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if not (scope_start <= start < end <= scope_end):
            continue
        relative_start = start - scope_start
        relative_end = end - scope_start
        exact_text = affected_text[relative_start:relative_end]
        if (
            translation[start:end] != exact_text
            or phrase != exact_text
        ):
            continue
        protected.append({
            "relative_start": relative_start,
            "relative_end": relative_end,
            "text": exact_text,
            "accepted_target": phrase,
        })
    protected.sort(key=lambda item: (item["relative_start"], item["relative_end"]))
    if any(
        left["relative_end"] > right["relative_start"]
        for left, right in zip(protected, protected[1:])
    ):
        protected = []

    editable = []
    position = 0
    for span in protected:
        if position < span["relative_start"]:
            text = affected_text[position:span["relative_start"]]
            if text.strip():
                editable.append({
                    "relative_start": position,
                    "relative_end": span["relative_start"],
                    "text": text,
                })
        position = span["relative_end"]
    if position < len(affected_text):
        text = affected_text[position:]
        if text.strip():
            editable.append({
                "relative_start": position,
                "relative_end": len(affected_text),
                "text": text,
            })
    region["protected_target_spans"] = protected
    region["editable_segments"] = editable


def _candidate_regions(
    source,
    translation,
    source_term,
    entry,
    missing_count,
    source_anchor_count=None,
    accepted_target_count=None,
    source_occurrences=(),
    target_occurrences=(),
):
    canonical = (entry.get("english", "") or "").strip()
    if (
        STRICT_REPAIR_LOCALIZATION_ANCHORS_FIELD in entry
        and not strict_repair_localization_anchor_mappings(entry)
    ):
        # A configured-but-invalid declaration invalidates every locator for
        # this entry. This keeps a bypassed startup validation from falling
        # through to older alias or alignment behavior.
        return []
    regions = []
    seen_spans = set()
    canonical_spans = []
    for accepted_target in strict_term_valid_targets(entry):
        canonical_spans.extend(
            (match.start(), match.end())
            for match in re.finditer(re.escape(accepted_target), translation or "")
        )
    for alias, origin in strict_term_repair_aliases(entry):
        for match in _phrase_pattern(alias).finditer(translation or ""):
            span = (match.start(), match.end())
            if span in seen_spans:
                continue
            if any(span[0] < end and start < span[1] for start, end in canonical_spans):
                continue
            seen_spans.add(span)
            line_start, line_end = _line_bounds(translation, match.start())
            scope_start = max(line_start, match.start() - REGION_CONTEXT_CHARS)
            scope_end = min(line_end, match.end() + REGION_CONTEXT_CHARS)
            regions.append({
                "source_term": source_term,
                "required_english": canonical,
                "alias": match.group(0),
                "alias_origin": origin,
                "scope_start": scope_start,
                "scope_end": scope_end,
                "affected_text": translation[scope_start:scope_end],
                "source_context": _source_context(source, source_term),
            })

    # If no known alias locates the error, first allow the legacy same-line
    # positional mapping for a source term that occurs on exactly one line.
    if not regions:
        source_records = _line_records(source)
        translation_records = _line_records(translation)
        source_line_counts = _owned_occurrence_line_counts(
            source_records, source_occurrences
        )
        target_line_counts = _owned_occurrence_line_counts(
            translation_records, target_occurrences
        )
        source_groups = [
            (index, source_records[index][1], count)
            for index, count in sorted((source_line_counts or {}).items())
        ]
        translation_groups = [
            (
                index,
                translation_records[index][0],
                translation_records[index][1],
                count,
            )
            for index, count in sorted((target_line_counts or {}).items())
        ]
        if (
            len(source_groups) == 1
            and len(source_records) == len(translation_records)
            and source_groups[0][0] < len(translation_records)
        ):
            index, source_line, _ = source_groups[0]
            offset, line = translation_records[index]
            if 0 < len(line) <= MAX_REGION_CHARS:
                regions.append({
                    "source_term": source_term,
                    "required_english": canonical,
                    "alias": "",
                    "alias_origin": "aligned_line",
                    "scope_start": offset,
                    "scope_end": offset + len(line),
                    "affected_text": line,
                    "source_context": source_line,
                    "source_occurrence_count_in_region": source_groups[0][2],
                    "accepted_target_count_in_region": target_line_counts.get(
                        index, 0
                    ),
                })

    # Repeated source terms may survive paragraph insertions or omissions that
    # make absolute line indexes unsafe. Pair only source term-bearing lines
    # with accepted-target-bearing translation lines in occurrence order.
    # Proceed only when exactly one paired line accounts for the entire
    # chapter-level missing count.
    if not regions and missing_count > 0:
        raw_source_count = sum(item[2] for item in source_groups)
        raw_target_count = sum(item[3] for item in translation_groups)
        counts_match_strict_qa = (
            source_line_counts is not None
            and target_line_counts is not None
            and raw_source_count == source_anchor_count
            and raw_target_count == accepted_target_count
        )

        if (
            len(source_groups) > 1
            and len(source_groups) == len(translation_groups)
            and counts_match_strict_qa
        ):
            deficits = []
            invalid_pair = False
            total_deficit = 0
            for source_group, target_group in zip(
                source_groups, translation_groups
            ):
                _, _, source_count = source_group
                _, _, _, target_count = target_group
                if target_count > source_count:
                    invalid_pair = True
                    break
                deficit = source_count - target_count
                total_deficit += deficit
                if deficit:
                    deficits.append((source_group, target_group, deficit))

            if (
                not invalid_pair
                and total_deficit == missing_count
                and len(deficits) == 1
                and deficits[0][2] == missing_count
            ):
                source_group, target_group, _ = deficits[0]
                _, source_line, _ = source_group
                _, target_offset, target_line, _ = target_group
                if 0 < len(target_line) <= MAX_REGION_CHARS:
                    regions.append({
                        "source_term": source_term,
                        "required_english": canonical,
                        "alias": "",
                        "alias_origin": "ordered_occurrence_line",
                        "scope_start": target_offset,
                        "scope_end": target_offset + len(target_line),
                        "affected_text": target_line,
                        "source_context": source_line,
                        "source_occurrence_count_in_region": source_group[2],
                        "accepted_target_count_in_region": target_group[3],
                    })

    if not regions:
        aligned_segment_region = _aligned_segment_localization_region(
            source,
            translation,
            source_term,
            entry,
            missing_count,
            source_anchor_count,
            accepted_target_count,
            source_occurrences,
            target_occurrences,
        )
        if aligned_segment_region is not None:
            regions.append(aligned_segment_region)

    for index, region in enumerate(regions, start=1):
        _attach_region_target_protection(
            region, translation or "", target_occurrences
        )
        region["region_id"] = f"{stable_text_hash(source_term)[:10]}-region-{index}"
        region["missing_canonical_count"] = missing_count
    return regions


def assess_strict_terminology_micro_repair_eligibility(
    source_text,
    translation,
    glossary_entries,
    high_priority_categories,
    *, chapter_number=None,
):
    """Locate bounded repair regions for remaining repair-eligible failures."""
    violations = locked_high_priority_term_violations(
        source_text, translation, glossary_entries, high_priority_categories,
        chapter_number=chapter_number,
    )
    strict_inventory = _strict_occurrence_inventory(
        source_text,
        translation,
        effective_strict_glossary_entry_items(
            glossary_entries, high_priority_categories, chapter_number=chapter_number
        ),
        glossary_entries=glossary_entries,
    )
    eligible_failures = []
    reasons = []
    regions = []
    eligible_sources = {
        source_term
        for source_term, _ in repair_eligible_strict_glossary_entry_items(
            glossary_entries, high_priority_categories, chapter_number=chapter_number
        )
    }
    for violation in violations:
        entry = glossary_entries.get(violation["chinese"], {})
        if violation["chinese"] not in eligible_sources:
            continue
        eligible_failures.append(violation)
        occurrence_detail = strict_inventory[violation["chinese"]]
        term_regions = _candidate_regions(
            source_text,
            translation,
            violation["chinese"],
            entry,
            violation.get("missing_canonical_count", 1),
            source_anchor_count=violation.get("source_anchor_count"),
            accepted_target_count=violation.get(
                "accepted_target_count",
                violation.get("canonical_target_count", 0),
            ),
            source_occurrences=occurrence_detail["source_occurrences"],
            target_occurrences=occurrence_detail["target_occurrences"],
        )
        if not term_regions:
            reasons.append(
                f"strict term {violation['chinese']!r} has no bounded repair region"
            )
        regions.extend(term_regions)

    if not eligible_failures:
        reasons.append("no remaining strict failures are eligible for automatic repair")
    if len(eligible_failures) > MAX_FAILURES_PER_REQUEST:
        reasons.append("strict terminology failure limit exceeded")
    if len(regions) > MAX_REGIONS_PER_REQUEST:
        reasons.append("candidate repair-region limit exceeded")
    if any(len(item["affected_text"]) > MAX_REGION_CHARS for item in regions):
        reasons.append("candidate repair region is too large")
    if len(re.findall(r"[A-Za-z]", translation or "")) < 40:
        reasons.append("translation is not predominantly target-language prose")

    failure_payload = sorted(
        (item["chinese"], item["required_english"])
        for item in eligible_failures
    )
    failure_identity = stable_text_hash(
        json.dumps(failure_payload, ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "eligible": bool(eligible_failures) and not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "violations": violations,
        "eligible_failures": eligible_failures,
        "repair_regions": regions,
        "pending_content_hash": stable_text_hash(translation),
        "failure_identity": failure_identity,
    }


def _region_old_text_word_limit(region):
    alias = region.get("alias", "")
    if alias:
        return len(ASCII_WORD_RE.findall(alias)) + 2
    return MAX_UNALIASED_OLD_TEXT_WORDS


def build_strict_terminology_micro_repair_request(
    *,
    novel_id,
    profile_id,
    target_language,
    eligibility,
    auxiliary_terminology_policy="",
    policy_version=MICRO_REPAIR_POLICY_VERSION,
    correction_context=None,
):
    """Build a small structured patch request; never include the whole chapter."""
    regions = []
    for item in eligibility["repair_regions"]:
        region = {
            key: item[key] for key in (
                "region_id", "source_term", "required_english", "affected_text",
                "source_context", "alias", "alias_origin",
                "missing_canonical_count",
            )
        }
        region.update({
            "locator": item["alias_origin"],
            "source_occurrence_count_in_region": item.get(
                "source_occurrence_count_in_region"
            ),
            "accepted_target_count_in_region": item.get(
                "accepted_target_count_in_region"
            ),
            "required_additional_canonical_occurrences": item[
                "missing_canonical_count"
            ],
            "protected_accepted_target_spans": deepcopy(
                item.get("protected_target_spans", [])
            ),
            "editable_segments": deepcopy(item.get("editable_segments", [])),
            "localization_anchor_matches": deepcopy(
                item.get("localization_anchor_matches", [])
            ),
            "permitted_repair_targets": list(
                item.get(
                    "permitted_repair_targets",
                    [item["required_english"]],
                )
            ),
            "constraints": {
                "max_old_text_characters": MAX_OLD_TEXT_CHARS,
                "max_new_text_characters": MAX_NEW_TEXT_CHARS,
                "max_old_text_words": _region_old_text_word_limit(item),
                "max_inserted_characters": MAX_INSERTED_CHARS,
                "old_text_must_be_exact_substring": True,
                "old_text_must_occur_exactly_once": True,
                "old_text_must_be_within_one_editable_segment": True,
                "old_text_may_overlap_protected_target_spans": False,
                "old_text_must_contain_localization_anchor": bool(
                    item.get("localization_anchor_matches")
                ),
                "may_not_cross_line_boundary": True,
                "required_accepted_target_occurrences_in_new_text": 1,
                "minimum_accepted_target_delta": 1,
                "required_total_accepted_target_delta": item[
                    "missing_canonical_count"
                ],
                "accepted_target_counting_rule": (
                    "Exact case-sensitive permitted_repair_targets occurrences with "
                    "the strict-QA longest-owned ASCII alphanumeric phrase-boundary "
                    "rule; longer ASCII words do not count."
                ),
                "accepted_target_delta_equation": (
                    "accepted_target_count(new_text) - "
                    "accepted_target_count(old_text) >= "
                    "minimum_accepted_target_delta"
                ),
                "preserve_unrelated_words": True,
            },
            "repair_instruction": (
                "Choose old_text entirely inside one editable_segment, never overlap "
                "a protected accepted-target span, and replace at most the configured "
                "word/character limits. The replacement must add the configured exact "
                "phrase-boundary accepted-target delta."
            ),
        })
        if region["permitted_repair_targets"] == [region["required_english"]]:
            # Preserve the v6 request vocabulary for canonical-only regions.
            # Expanded target-family regions use the accepted-target contract
            # above because the canonical singular is not their permitted form.
            region["constraints"].update({
                "required_canonical_occurrences_in_new_text": 1,
                "minimum_canonical_target_delta": 1,
                "required_total_canonical_target_delta": item[
                    "missing_canonical_count"
                ],
                "canonical_counting_rule": (
                    "Exact case-sensitive required_english phrase occurrences "
                    "with the strict-QA ASCII alphanumeric phrase-boundary rule; "
                    "a longer ASCII word does not count."
                ),
                "canonical_delta_equation": (
                    "count(new_text) - count(old_text) >= "
                    "minimum_canonical_target_delta"
                ),
            })
        regions.append(region)
    schema = {"patches": [{
        "region_id": "term-region-1",
        "source_term": "exact source-language term",
        "old_text": "exact short text from affected_text",
        "new_text": "minimal replacement containing one permitted repair target intact",
    }]}
    system_prompt = (
        f"Repair ONLY the listed strict terminology mismatch in an otherwise complete "
        f"{target_language} translation. This is not a translation or prose-revision "
        "request. Conservative local eligibility and region isolation have already "
        "passed; do not reject a region merely because required_english already appears "
        "elsewhere in affected_text. Existing protected_accepted_target_spans are already "
        "correct: old_text MUST NOT overlap or consume them. Choose old_text wholly inside "
        "one listed editable_segment. Obey response_constraints as well as every region "
        "constraint. Use the supplied local source/target counts to add "
        "exactly required_additional_canonical_occurrences. Choose ONE exact short "
        "old_text substring from affected_text and obey every numeric/boolean constraint "
        "in that region. max_old_text_words is an absolute limit, not guidance. For "
        "each patch, new_text MUST contain exactly one complete exact value from the "
        "region's permitted_repair_targets under the stated strict-QA phrase-boundary "
        "rule. accepted_target_count(new_text) - accepted_target_count(old_text) MUST "
        "be at least minimum_accepted_target_delta; merely preserving an existing correct "
        "occurrence is invalid. When localization_anchor_matches is nonempty, old_text "
        "must contain that exact partial anchor and new_text must add its mapped target. "
        "Preserve all unrelated wording, meaning, "
        "punctuation, and paragraph structure. Make no stylistic improvements, "
        "expansions, summaries, omissions, or other terminology changes. old_text must "
        "be one exact, short substring of affected_text. Do not emit Han/CJK in "
        "new_text. Return exactly one JSON object matching required_response_schema, "
        "without Markdown or commentary. Return an empty patches list only when no "
        "semantically faithful exact local substring can be minimally repaired; never "
        "invent a speculative referent or rewrite unrelated prose."
    )
    request_kind = (
        correction_context.get("request_kind", "bounded_response_correction")
        if correction_context else "initial"
    )
    payload = {
        "novel_id": novel_id,
        "translation_profile": profile_id,
        "target_language": target_language,
        "micro_repair_policy_version": policy_version,
        "request_kind": request_kind,
        "strict_failure_identity": eligibility["failure_identity"],
        "conservative_local_eligibility_established": True,
        "repair_regions": regions,
        "auxiliary_terminology_policy": auxiliary_terminology_policy or "",
        "response_constraints": {
            "max_patches": sum(
                item.get("missing_canonical_count", 1)
                for item in eligibility["eligible_failures"]
            ),
            "max_total_old_plus_new_characters": MAX_TOTAL_PATCH_CHARS,
            "patches_must_not_overlap": True,
        },
        "required_response_schema": schema,
    }
    if correction_context:
        previous_response = correction_context.get(
            "previous_response", {"patches": []}
        )
        payload["correction_context"] = {
            "correction_reason": correction_context.get("correction_reason"),
            "previous_response": deepcopy(previous_response),
            "local_validation_errors": list(
                correction_context.get("validation_errors", [])
            )[:5],
            "local_validation_codes": list(
                correction_context.get("validation_codes", [])
            )[:8],
            "instruction": (
                "The previous response was empty or failed only the explicitly listed "
                "bounded instruction-compliance rules. Re-examine only the already-"
                "eligible region. Obey its constraints exactly: old_text must be short, "
                "unique, wholly within one editable segment, and outside every protected "
                "target span. new_text must ADD the required exact phrase-boundary "
                "canonical delta. Do not rewrite the sentence. Empty patches remain "
                "allowed only if no safe semantic repair exists."
            ),
        }
    return {
        "system_prompt": system_prompt,
        "user_prompt": json.dumps(payload, ensure_ascii=False, indent=2),
        "payload": payload,
        "schema": schema,
    }


def _is_exact_empty_patch_response(response):
    try:
        parsed = _decode_response(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and parsed == {"patches": []}


def _decode_response(response):
    if isinstance(response, str):
        return json.loads(clean_json_text(response))
    if isinstance(response, dict):
        return deepcopy(response)
    raise ValueError("repair response must be a JSON object or JSON text")


def _is_subsequence(needles, haystack):
    position = 0
    for item in haystack:
        if position < len(needles) and item == needles[position]:
            position += 1
    return position == len(needles)


def _canonical_target_count(text, canonical):
    """Count an exact canonical phrase with the same ASCII boundaries as strict QA."""
    owner = "canonical_target"
    return len(_claim_longest_non_overlapping_occurrences(
        text,
        ((owner, (canonical,)),),
        bounded_ascii=True,
    ).get(owner, ()))


def _accepted_target_occurrences(text, accepted_targets):
    """Return longest-owned exact target-family matches under strict QA bounds."""
    owner = "accepted_target"
    return _claim_longest_non_overlapping_occurrences(
        text,
        ((owner, tuple(accepted_targets)),),
        bounded_ascii=True,
    ).get(owner, ())


def _validation_result(
    *, accepted, repaired_text, patches_applied, errors,
    error_codes=(), safe_correctable=False, safe_correctable_reason=None,
    attempted_patches=(), diagnostic_patches=(),
):
    return {
        "accepted": accepted,
        "repaired_text": repaired_text,
        "patches_applied": list(patches_applied),
        "errors": list(dict.fromkeys(errors)),
        "error_codes": list(dict.fromkeys(error_codes)),
        "safe_correctable": safe_correctable,
        "safe_correctable_reason": safe_correctable_reason,
        "attempted_patches": list(attempted_patches),
        "diagnostic_patches": list(diagnostic_patches),
    }


def validate_and_apply_strict_terminology_response(
    source_text,
    translation,
    response,
    eligibility,
    glossary_entries,
    high_priority_categories,
    approved_reader_facing_terms=(),
    *, chapter_number=None,
):
    """Validate anchored patches in memory and require strict-QA improvement."""
    errors = []
    error_codes = []
    ineffective_errors = []

    def add_error(message, code):
        errors.append(message)
        error_codes.append(code)

    try:
        parsed = _decode_response(response)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _validation_result(
            accepted=False, repaired_text=translation, patches_applied=[],
            errors=[f"malformed repair JSON: {exc}"],
            error_codes=["malformed_repair_json"],
        )
    if not isinstance(parsed, dict) or set(parsed) != {"patches"}:
        add_error(
            "repair response must contain exactly the patches field",
            "response_schema",
        )
        patches = []
    else:
        patches = parsed["patches"]
    if not isinstance(patches, list):
        add_error("patches must be a list", "response_schema")
        patches = []
    if not patches:
        add_error(EMPTY_PATCH_VALIDATION_ERROR, "empty_patch_list")
    max_patches = sum(
        item.get("missing_canonical_count", 1)
        for item in eligibility["eligible_failures"]
    )
    if len(patches) > max_patches:
        add_error(
            "patch count exceeds missing source-anchored occurrences",
            "patch_count_limit",
        )

    regions = {item["region_id"]: item for item in eligibility["repair_regions"]}
    validated = []
    diagnostic_patches = []
    occupied = []
    total_chars = 0
    for index, patch in enumerate(patches):
        label = f"patches[{index}]"
        diagnostic_safe = True
        required_fields = {"region_id", "source_term", "old_text", "new_text"}
        if not isinstance(patch, dict) or set(patch) != required_fields:
            add_error(
                f"{label} must contain exactly region_id, source_term, old_text, new_text",
                "patch_schema",
            )
            continue
        region = regions.get(patch["region_id"])
        if region is None:
            add_error(f"{label}.region_id is not allowed", "unknown_region")
            continue
        if patch["source_term"] != region["source_term"]:
            diagnostic_safe = False
            add_error(
                f"{label}.source_term does not match its repair region",
                "source_term_identity",
            )
        old_text = patch["old_text"]
        new_text = patch["new_text"]
        if not isinstance(old_text, str) or not old_text:
            add_error(
                f"{label}.old_text must be non-empty text", "invalid_old_text"
            )
            continue
        if not isinstance(new_text, str) or not new_text:
            add_error(
                f"{label}.new_text must be non-empty text", "invalid_new_text"
            )
            continue
        if len(old_text) > MAX_OLD_TEXT_CHARS:
            diagnostic_safe = False
            add_error(f"{label}.old_text is too large", "old_text_character_limit")
        if len(new_text) > MAX_NEW_TEXT_CHARS:
            diagnostic_safe = False
            add_error(f"{label}.new_text is too large", "new_text_character_limit")
        if any(mark in old_text + new_text for mark in ("\n", "\r")):
            diagnostic_safe = False
            add_error(
                f"{label} may not change line or paragraph boundaries",
                "patch_line_boundary",
            )
        canonical = region["required_english"]
        permitted_targets = tuple(
            region.get("permitted_repair_targets", (canonical,))
        )
        old_target_occurrences = _accepted_target_occurrences(
            old_text, permitted_targets
        )
        new_target_occurrences = _accepted_target_occurrences(
            new_text, permitted_targets
        )
        canonical_only_contract = permitted_targets == (canonical,)
        if len(new_target_occurrences) != 1:
            add_error(
                (
                    f"{label}.new_text must contain the complete canonical "
                    "target exactly once"
                    if canonical_only_contract else
                    f"{label}.new_text must contain exactly one complete "
                    "permitted repair target"
                ),
                "canonical_target_occurrence_count",
            )
        accepted_target_delta = (
            len(new_target_occurrences) - len(old_target_occurrences)
        )
        if accepted_target_delta <= 0:
            message = (
                (
                    f"{label} does not add a required canonical target "
                    f"occurrence (canonical delta {accepted_target_delta})"
                )
                if canonical_only_contract else
                (
                    f"{label} does not add a required accepted target "
                    f"occurrence (accepted-target delta {accepted_target_delta})"
                )
            )
            add_error(message, INEFFECTIVE_CANONICAL_DELTA_CODE)
            ineffective_errors.append(message)
        if residual_cjk_report(
            new_text, approved_reader_facing_terms
        )["remaining_cjk_character_count"]:
            diagnostic_safe = False
            add_error(
                f"{label}.new_text contains unexpected CJK", "unexpected_cjk"
            )
        if len(new_text) - len(old_text) > MAX_INSERTED_CHARS:
            add_error(
                f"{label}.new_text expands the patch too much",
                "inserted_character_limit",
            )
        scope = region["affected_text"]
        if scope.count(old_text) != 1:
            add_error(
                f"{label}.old_text must occur exactly once in its repair region",
                "old_text_not_unique_in_region",
            )
            continue
        relative = scope.index(old_text)
        start = region["scope_start"] + relative
        end = start + len(old_text)
        overlaps_patch = any(
            start < used_end and used_start < end
            for used_start, used_end in occupied
        )
        if overlaps_patch:
            diagnostic_safe = False
            add_error(f"{label} overlaps another patch", "overlapping_patch")
        occupied.append((start, end))

        protected_overlap = any(
            relative < span["relative_end"]
            and span["relative_start"] < relative + len(old_text)
            for span in region.get("protected_target_spans", [])
        )
        if protected_overlap:
            add_error(
                f"{label}.old_text overlaps an already-correct protected target span",
                "protected_target_span_overlap",
            )
        editable_segments = region.get("editable_segments", [])
        if not protected_overlap and editable_segments and not any(
            segment["relative_start"] <= relative
            and relative + len(old_text) <= segment["relative_end"]
            for segment in editable_segments
        ):
            add_error(
                f"{label}.old_text must stay within one editable segment",
                "old_text_outside_editable_segment",
            )

        old_words = [item.casefold() for item in ASCII_WORD_RE.findall(old_text)]
        region_alias = region.get("alias", "")
        if region_alias:
            alias_words = ASCII_WORD_RE.findall(region_alias)
            if region_alias.casefold() not in old_text.casefold():
                add_error(
                    f"{label}.old_text does not contain its anchored alias",
                    "old_text_missing_anchored_alias",
                )
            if len(old_words) > len(alias_words) + 2:
                add_error(
                    f"{label}.old_text reaches beyond a terminology-sized span",
                    "old_text_word_limit",
                )
        elif len(old_words) > MAX_UNALIASED_OLD_TEXT_WORDS:
            add_error(
                f"{label}.old_text reaches beyond a terminology-sized span",
                "old_text_word_limit",
            )

        if len(new_target_occurrences) == 1:
            target_occurrence = new_target_occurrences[0]
            applied_target = target_occurrence["phrase"]
            remainder = (
                new_text[:target_occurrence["start"]]
                + new_text[target_occurrence["end"]:]
            )
        else:
            applied_target = ""
            remainder = new_text
        localization_matches = region.get("localization_anchor_matches", [])
        if localization_matches:
            contained_anchors = [
                item for item in localization_matches
                if relative <= item["relative_start"]
                and item["relative_end"] <= relative + len(old_text)
            ]
            if len(contained_anchors) != 1:
                diagnostic_safe = False
                add_error(
                    f"{label}.old_text must contain its exact localization anchor",
                    "old_text_missing_localization_anchor",
                )
            elif applied_target != contained_anchors[0].get("target"):
                diagnostic_safe = False
                add_error(
                    f"{label}.new_text does not add the target mapped from its "
                    "localization anchor",
                    "localization_anchor_target_mismatch",
                )
        remainder_words = [item.casefold() for item in ASCII_WORD_RE.findall(remainder)]
        exact_possessive_normalization = (
            is_exact_strict_term_possessive_normalization(
                old_text, new_text, permitted_targets
            )
        )
        possessive_morphology_touched = touches_strict_term_possessive_morphology(
            old_text, new_text, permitted_targets
        )
        if (
            not exact_possessive_normalization
            and (
                possessive_morphology_touched
                or not _is_subsequence(remainder_words, old_words)
            )
        ):
            add_error(
                f"{label}.new_text rewrites unrelated English words",
                "unrelated_english_rewrite",
            )
        if diagnostic_safe:
            diagnostic_patches.append({
                key: patch[key]
                for key in ("region_id", "source_term", "old_text", "new_text")
            })
        validated.append({
            **patch,
            "start": start,
            "end": end,
            "required_english": canonical,
            "applied_target": applied_target,
            "accepted_target_delta": accepted_target_delta,
            # Retained for backward-compatible repair observability. Under v7
            # this is the strict-QA accepted target-family delta.
            "canonical_target_delta": accepted_target_delta,
            "repair_type": REPAIR_EVENT_TYPE,
        })
        total_chars += len(old_text) + len(new_text)
    if total_chars > MAX_TOTAL_PATCH_CHARS:
        add_error(
            "total patch size exceeds the terminology micro-repair limit",
            "total_patch_character_limit",
        )
    if patches:
        for failure in eligibility["eligible_failures"]:
            source_term = failure["chinese"]
            required_delta = failure.get("missing_canonical_count", 1)
            term_patches = [
                item for item in validated
                if item["source_term"] == source_term
            ]
            supplied_delta = sum(
                max(0, item.get("canonical_target_delta", 0))
                for item in term_patches
            )
            if supplied_delta < required_delta:
                message = (
                    f"patches do not add all required canonical target occurrences "
                    f"for strict term {source_term!r} "
                    f"(required {required_delta}, supplied {supplied_delta})"
                )
                add_error(message, "missing_canonical_target_delta")
                if term_patches and any(
                    item.get("canonical_target_delta", 0) <= 0
                    for item in term_patches
                ):
                    ineffective_errors.append(message)
    if errors:
        unique_errors = list(dict.fromkeys(errors))
        unique_codes = list(dict.fromkeys(error_codes))
        unique_ineffective = list(dict.fromkeys(ineffective_errors))
        zero_delta_only = bool(
            unique_ineffective and unique_errors == unique_ineffective
        )
        required_sources = {
            item["chinese"] for item in eligibility["eligible_failures"]
        }
        diagnostic_sources = {
            item["source_term"] for item in diagnostic_patches
        }
        bounded_compliance = bool(
            diagnostic_patches
            and len(diagnostic_patches) == len(patches)
            and required_sources <= diagnostic_sources
            and set(unique_codes) <= BOUNDED_COMPLIANCE_ERROR_CODES
        )
        safe_correctable = zero_delta_only or bounded_compliance
        if zero_delta_only:
            safe_reason = INEFFECTIVE_PATCH_CORRECTION_REASON
        elif bounded_compliance:
            safe_reason = BOUNDED_COMPLIANCE_CORRECTION_REASON
        else:
            safe_reason = None
        attempted = diagnostic_patches if safe_correctable else []
        return _validation_result(
            accepted=False, repaired_text=translation, patches_applied=[],
            errors=unique_errors,
            error_codes=unique_codes,
            safe_correctable=safe_correctable,
            safe_correctable_reason=safe_reason,
            attempted_patches=attempted,
            diagnostic_patches=diagnostic_patches,
        )

    candidate = translation
    for patch in sorted(validated, key=lambda item: item["start"], reverse=True):
        candidate = candidate[:patch["start"]] + patch["new_text"] + candidate[patch["end"]:]
    before = locked_high_priority_term_violations(
        source_text, translation, glossary_entries, high_priority_categories,
        chapter_number=chapter_number,
    )
    after = locked_high_priority_term_violations(
        source_text, candidate, glossary_entries, high_priority_categories,
        chapter_number=chapter_number,
    )
    targeted = {item["chinese"] for item in eligibility["eligible_failures"]}
    if any(item["chinese"] in targeted for item in after):
        add_error(
            "candidate still fails targeted strict terminology QA",
            "targeted_strict_qa_remaining",
        )
    if len(after) >= len(before):
        add_error(
            "candidate does not improve strict terminology QA",
            "strict_qa_not_improved",
        )
    before_cjk = residual_cjk_report(translation, approved_reader_facing_terms)
    after_cjk = residual_cjk_report(candidate, approved_reader_facing_terms)
    if after_cjk["remaining_cjk_character_count"] > before_cjk["remaining_cjk_character_count"]:
        add_error("candidate introduces unexpected CJK", "candidate_unexpected_cjk")
    if _line_break_sequence(candidate) != _line_break_sequence(translation):
        add_error(
            "candidate changes line or paragraph structure",
            "candidate_line_structure",
        )
    if errors:
        return _validation_result(
            accepted=False, repaired_text=translation, patches_applied=[],
            errors=errors, error_codes=error_codes,
            diagnostic_patches=diagnostic_patches,
        )
    public = [{key: value for key, value in item.items() if key not in {"start", "end"}}
              for item in validated]
    return _validation_result(
        accepted=True, repaired_text=candidate, patches_applied=public,
        errors=[], diagnostic_patches=diagnostic_patches,
    )


def repair_operation_id(novel_id, chapter, pending_hash, policy_version,
                        failure_identity, attempt_number):
    return (
        f"{REPAIR_EVENT_TYPE}:{novel_id}:{chapter}:{pending_hash}:"
        f"{policy_version}:{failure_identity}:{attempt_number}"
    )


def _attempts(meta, pending_hash, policy_version, failure_identity):
    state = meta.get(ATTEMPT_STATE_KEY, {})
    by_hash = state.get("attempts_by_pending_hash", {})
    attempts = by_hash.get(pending_hash, [])
    if not isinstance(attempts, list):
        return []
    return [item for item in attempts if (
        item.get("policy_version") == policy_version
        and item.get("failure_identity") == failure_identity
    )]


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
        raise ValueError("strict terminology repair attempt record is missing")
    by_hash[pending_hash] = attempts
    state["attempts_by_pending_hash"] = by_hash
    updated[ATTEMPT_STATE_KEY] = state
    return updated


def _safe_log(log_event, event, **fields):
    try:
        log_event(event, **fields)
    except Exception:
        pass


def _correction_request_kind(reason):
    if reason == EMPTY_PATCH_CORRECTION_REASON:
        return "empty_patch_correction"
    if reason == INEFFECTIVE_PATCH_CORRECTION_REASON:
        return "ineffective_patch_correction"
    if reason == BOUNDED_COMPLIANCE_CORRECTION_REASON:
        return "bounded_instruction_compliance_correction"
    return "bounded_response_correction"


def _correction_context_from_attempt(attempt):
    reason = attempt.get("safe_correctable_reason")
    return {
        "request_kind": _correction_request_kind(reason),
        "correction_reason": reason,
        "previous_response": deepcopy(
            attempt.get("response_summary", {"patches": []})
        ),
        "validation_errors": list(attempt.get("validation_errors", [])),
        "validation_codes": list(attempt.get("validation_codes", [])),
    }


def _qa_has_only_eligible_strict_blockers(qa, eligibility):
    """Allow correction only when baseline mandatory QA has no unrelated blocker."""
    violations = eligibility.get("violations", [])
    expected_blockers = [
        {"type": "locked_terminology", **item}
        for item in violations
    ]
    return bool(violations) and (
        qa.get("commit_allowed") is False
        and qa.get("locked_high_priority_term_violations") == violations
        and qa.get("commit_blockers") == expected_blockers
    )


def _account_unsubmitted_recorded_attempts(
    meta, pending_hash, policy_version, failure_identity,
    account_api_usage, persist_meta,
):
    """Finish only missing idempotent accounting for already-paid attempts."""
    current_meta = meta
    for attempt in _attempts(
        current_meta, pending_hash, policy_version, failure_identity
    ):
        if attempt.get("stats") and not attempt.get("accounting_submitted", False):
            newly_recorded = account_api_usage(
                attempt["operation_id"], attempt["stats"]
            )
            current_meta = _replace_attempt(
                current_meta,
                pending_hash,
                attempt["operation_id"],
                {
                    "accounting_submitted": True,
                    "accounting_newly_recorded": newly_recorded is True,
                },
            )
            persist_meta(current_meta)
    return current_meta


def _try_revalidate_recorded_diagnostic_candidate(
    *, novel_id, chapter, source_text, translation, meta, eligibility,
    glossary_entries, high_priority_categories, persist_meta,
    persist_translation, qa_checker, approved_reader_facing_terms,
    policy_version, log_event,
):
    """Revalidate one exact historical diagnostic response without an API call."""
    pending_hash = eligibility["pending_content_hash"]
    failure_identity = eligibility["failure_identity"]
    attempts = _attempts(meta, pending_hash, policy_version, failure_identity)
    if not attempts or attempts[-1].get("status") != "locally_rejected":
        return None
    previous = attempts[-1]
    attempt_number = previous.get("attempt")
    expected_operation_id = (
        repair_operation_id(
            novel_id,
            chapter,
            pending_hash,
            policy_version,
            failure_identity,
            attempt_number,
        )
        if isinstance(attempt_number, int) and attempt_number >= 1
        else None
    )
    summary = previous.get("response_summary")
    identity_matches = (
        previous.get("novel_id") == novel_id
        and previous.get("chapter") == chapter
        and previous.get("pending_content_hash") == pending_hash
        and previous.get("policy_version") == policy_version
        and previous.get("failure_identity") == failure_identity
        and previous.get("operation_id") == expected_operation_id
        and previous.get("response_retention")
        == "bounded_region_anchored_patch_fields"
        and isinstance(summary, dict)
        and set(summary) == {"patches"}
        and isinstance(summary.get("patches"), list)
        and bool(summary["patches"])
    )
    if not identity_matches:
        return None

    validation = validate_and_apply_strict_terminology_response(
        source_text,
        translation,
        {"patches": deepcopy(summary["patches"])},
        eligibility,
        glossary_entries,
        high_priority_categories,
        approved_reader_facing_terms,
        chapter_number=chapter,
    )
    if not validation["accepted"]:
        return None
    candidate = validation["repaired_text"]
    candidate_hash = stable_text_hash(candidate)
    qa = qa_checker(candidate)
    baseline_qa = qa_checker(translation)
    qa_transition = _strict_qa_transition(
        baseline_qa, qa, eligibility
    )
    if not qa_transition["accepted"]:
        return None
    targeted_sources = {
        item.get("chinese")
        for item in eligibility.get("eligible_failures", [])
        if isinstance(item, dict) and item.get("chinese")
    }

    def unrelated_blockers(qa_result):
        return [
            item for item in qa_result.get("commit_blockers", [])
            if not (
                isinstance(item, dict)
                and item.get("type") == "locked_terminology"
                and item.get("chinese") in targeted_sources
            )
        ]

    # Revalidation of historical model output is stricter than accepting a
    # fresh fully-clean candidate: every non-target blocker must have the same
    # identity and content before and after the recorded patches.
    if unrelated_blockers(baseline_qa) != unrelated_blockers(qa):
        return None

    repaired_meta = pending_meta_after_strict_terminology_repair(
        meta, validation["patches_applied"]
    )
    revalidation_status = (
        "applied_with_unrelated_blockers"
        if qa_transition["partial"] else "applied"
    )
    repaired_meta = _replace_attempt(
        repaired_meta,
        pending_hash,
        previous["operation_id"],
        {
            # Preserve the historical local rejection; record the later
            # validator-bugfix recovery as a separate, unbilled audit fact.
            "diagnostic_revalidation": {
                "status": revalidation_status,
                "policy_version": policy_version,
                "candidate_content_hash": candidate_hash,
                "patches": [{
                    key: item[key]
                    for key in ("region_id", "source_term", "old_text", "new_text")
                } for item in validation["patches_applied"]],
                "qa_transition": deepcopy(qa_transition),
            },
            "correction_eligible": False,
        },
    )
    persist_meta(repaired_meta)
    persist_translation(candidate)
    _safe_log(
        log_event,
        REPAIR_EVENT_TYPE,
        chapter=chapter,
        stage="revalidated_recorded_diagnostic_candidate",
        operation_id=previous["operation_id"],
        failure_identity=failure_identity,
        policy_version=policy_version,
        api_cost=0,
        api_called=False,
        candidate_content_hash=candidate_hash,
        qa_transition=qa_transition["reason"],
    )
    return {
        "status": (
            "succeeded_from_revalidated_recorded_candidate_with_unrelated_blockers"
            if qa_transition["partial"] else
            "succeeded_from_revalidated_recorded_candidate"
        ),
        "translation": candidate,
        "meta": repaired_meta,
        "eligibility": eligibility,
        "api_called": False,
        "repair_stats": previous.get("stats"),
        "repair_attempt_stats": [],
        "validation": validation,
        "qa": qa,
        "baseline_qa": baseline_qa,
        "qa_transition": qa_transition,
        "operation_id": previous["operation_id"],
    }


def revalidate_recorded_strict_terminology_candidate(
    *, novel_id, chapter, source_text, translation, meta, glossary_entries,
    high_priority_categories, persist_meta, persist_translation,
    account_api_usage, qa_checker, approved_reader_facing_terms=(),
    policy_version=MICRO_REPAIR_POLICY_VERSION,
    log_event=lambda event, **fields: None,
):
    """Try only safe local recovery of a previously rejected diagnostic response."""
    eligibility = assess_strict_terminology_micro_repair_eligibility(
        source_text, translation, glossary_entries, high_priority_categories,
        chapter_number=chapter,
    )
    if not eligibility["eligible_failures"] or not eligibility["eligible"]:
        return {
            "status": "no_revalidatable_recorded_candidate",
            "translation": translation,
            "meta": meta,
            "eligibility": eligibility,
            "api_called": False,
        }
    current_meta = _account_unsubmitted_recorded_attempts(
        meta,
        eligibility["pending_content_hash"],
        policy_version,
        eligibility["failure_identity"],
        account_api_usage,
        persist_meta,
    )
    recovered = _try_revalidate_recorded_diagnostic_candidate(
        novel_id=novel_id,
        chapter=chapter,
        source_text=source_text,
        translation=translation,
        meta=current_meta,
        eligibility=eligibility,
        glossary_entries=glossary_entries,
        high_priority_categories=high_priority_categories,
        persist_meta=persist_meta,
        persist_translation=persist_translation,
        qa_checker=qa_checker,
        approved_reader_facing_terms=approved_reader_facing_terms,
        policy_version=policy_version,
        log_event=log_event,
    )
    if recovered is not None:
        return recovered
    return {
        "status": "no_revalidatable_recorded_candidate",
        "translation": translation,
        "meta": current_meta,
        "eligibility": eligibility,
        "api_called": False,
    }


def perform_strict_terminology_micro_repair(
    *, novel_id, chapter, profile_id, target_language, source_text,
    translation, meta, glossary_entries, high_priority_categories,
    auxiliary_terminology_policy, request_api, persist_meta,
    persist_translation, account_api_usage, qa_checker,
    approved_reader_facing_terms=(), policy_version=MICRO_REPAIR_POLICY_VERSION,
    max_attempts=MAX_ATTEMPTS_PER_POLICY,
    log_event=lambda event, **fields: None,
):
    """Run one bounded request plus one narrowly safe correction at most."""
    if not isinstance(max_attempts, int) or not (
        1 <= max_attempts <= MAX_ATTEMPTS_PER_POLICY
    ):
        raise ValueError(
            f"max_attempts must be between 1 and {MAX_ATTEMPTS_PER_POLICY}"
        )
    eligibility = assess_strict_terminology_micro_repair_eligibility(
        source_text, translation, glossary_entries, high_priority_categories,
        chapter_number=chapter,
    )
    if not eligibility["eligible_failures"]:
        return {"status": "clean", "translation": translation, "meta": meta,
                "eligibility": eligibility, "api_called": False}
    if not eligibility["eligible"]:
        return {"status": "ineligible", "translation": translation, "meta": meta,
                "eligibility": eligibility, "api_called": False}

    pending_hash = eligibility["pending_content_hash"]
    failure_identity = eligibility["failure_identity"]
    current_meta = meta
    attempts = _attempts(
        current_meta, pending_hash, policy_version, failure_identity
    )

    # If a process stopped after recording paid stats but before submitting
    # accounting, replay only the idempotent accounting callback. Stable
    # operation IDs prevent an already-accounted request from being charged
    # locally twice.
    current_meta = _account_unsubmitted_recorded_attempts(
        current_meta,
        pending_hash,
        policy_version,
        failure_identity,
        account_api_usage,
        persist_meta,
    )
    attempts = _attempts(
        current_meta, pending_hash, policy_version, failure_identity
    )
    recovered = _try_revalidate_recorded_diagnostic_candidate(
        novel_id=novel_id,
        chapter=chapter,
        source_text=source_text,
        translation=translation,
        meta=current_meta,
        eligibility=eligibility,
        glossary_entries=glossary_entries,
        high_priority_categories=high_priority_categories,
        persist_meta=persist_meta,
        persist_translation=persist_translation,
        qa_checker=qa_checker,
        approved_reader_facing_terms=approved_reader_facing_terms,
        policy_version=policy_version,
        log_event=log_event,
    )
    if recovered is not None:
        return recovered
    replayable_statuses = {
        "candidate_validated",
        "qa_rejected",
        "applied",
        "applied_with_unrelated_blockers",
    }
    if attempts and attempts[-1].get("status") in replayable_statuses:
        previous = attempts[-1]
        replay = validate_and_apply_strict_terminology_response(
            source_text,
            translation,
            {"patches": previous.get("patches", [])},
            eligibility,
            glossary_entries,
            high_priority_categories,
            approved_reader_facing_terms,
            chapter_number=chapter,
        )
        if replay["accepted"] and stable_text_hash(replay["repaired_text"]) == (
            previous.get("candidate_content_hash")
        ):
            qa = qa_checker(replay["repaired_text"])
            baseline_qa = (
                None if qa.get("commit_allowed", False) else qa_checker(translation)
            )
            qa_transition = _strict_qa_transition(
                baseline_qa or {}, qa, eligibility
            )
            if qa_transition["accepted"]:
                repaired_meta = pending_meta_after_strict_terminology_repair(
                    current_meta, replay["patches_applied"]
                )
                attempt_status = (
                    "applied_with_unrelated_blockers"
                    if qa_transition["partial"] else "applied"
                )
                repaired_meta = _replace_attempt(
                    repaired_meta, pending_hash, previous["operation_id"],
                    {
                        "status": attempt_status,
                        "candidate_qa": deepcopy(qa),
                        "qa_transition": deepcopy(qa_transition),
                        "correction_eligible": False,
                    },
                )
                persist_meta(repaired_meta)
                persist_translation(replay["repaired_text"])
                return {
                    "status": (
                        "succeeded_from_recorded_candidate_with_unrelated_blockers"
                        if qa_transition["partial"] else
                        "succeeded_from_recorded_candidate"
                    ),
                    "translation": replay["repaired_text"],
                    "meta": repaired_meta,
                    "eligibility": eligibility,
                    "api_called": False,
                    "repair_stats": previous.get("stats"),
                    "repair_attempt_stats": [],
                    "validation": replay,
                    "qa": qa,
                    "baseline_qa": baseline_qa,
                    "qa_transition": qa_transition,
                    "operation_id": previous["operation_id"],
                }
    if not attempts:
        request_kind = "initial"
        correction_context = None
    elif resumable_attempt(attempts[-1], request_api):
        request_kind = attempts[-1]["request_kind"]
        correction_context = (_correction_context_from_attempt(attempts[-2])
                              if request_kind != "initial" else None)
    elif (
        len(attempts) < max_attempts
        and attempts[-1].get("status") == "locally_rejected"
        and attempts[-1].get("correction_eligible") is True
    ):
        correction_context = _correction_context_from_attempt(attempts[-1])
        request_kind = correction_context["request_kind"]
    else:
        return {"status": "attempt_limit_reached", "translation": translation,
                "meta": current_meta, "eligibility": eligibility,
                "api_called": False, "repair_attempt_stats": []}

    repair_attempt_stats = []
    operation_ids = []
    while True:
        attempts = _attempts(
            current_meta, pending_hash, policy_version, failure_identity
        )
        resume_transport = bool(attempts and resumable_attempt(attempts[-1], request_api))
        attempt_number = attempts[-1]["attempt"] if resume_transport else len(attempts) + 1
        operation_id = repair_operation_id(
            novel_id, chapter, pending_hash, policy_version, failure_identity,
            attempt_number,
        )
        request = build_strict_terminology_micro_repair_request(
            novel_id=novel_id,
            profile_id=profile_id,
            target_language=target_language,
            eligibility=eligibility,
            auxiliary_terminology_policy=auxiliary_terminology_policy,
            policy_version=policy_version,
            correction_context=correction_context,
        )
        request_hash = stable_text_hash(
            request["system_prompt"] + "\n" + request["user_prompt"]
        )
        attempt = {
            "attempt": attempt_number,
            "operation_id": operation_id,
            "novel_id": novel_id,
            "chapter": chapter,
            "pending_content_hash": pending_hash,
            "policy_version": policy_version,
            "failure_identity": failure_identity,
            "request_kind": request_kind,
            "request_payload_hash": request_hash,
            **planned_attempt(request_api),
        }
        if not resume_transport:
            current_meta = _append_attempt(current_meta, pending_hash, attempt)
            persist_meta(current_meta)
        try:
            response, stats = invoke_request(request_api, request, operation_id)
        except Exception as exc:
            transport_failure = failure_values(exc, next(
                item for item in _attempts(current_meta, pending_hash, policy_version, failure_identity)
                if item["operation_id"] == operation_id))
            if transport_failure.get("stats"):
                account_api_usage(operation_id, transport_failure["stats"])
            current_meta = _replace_attempt(
                current_meta,
                pending_hash,
                operation_id,
                {
                    **transport_failure,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "correction_eligible": False,
                },
            )
            persist_meta(current_meta)
            _safe_log(
                log_event, REPAIR_EVENT_TYPE, chapter=chapter,
                stage="api_failed", operation_id=operation_id,
                api_cost=transport_failure.get("stats", {}).get("cost_usd", 0),
                policy_version=policy_version,
                failure_identity=failure_identity,
                request_kind=request_kind, attempt=attempt_number,
            )
            return {
                "status": "api_failed", "translation": translation,
                "meta": current_meta, "eligibility": eligibility,
                "api_called": transport_failure["api_called"], "error": str(exc),
                "repair_attempt_stats": [deepcopy(item["stats"]) for item in
                    _attempts(current_meta, pending_hash, policy_version, failure_identity)
                    if item.get("stats")],
                "operation_ids": operation_ids + [operation_id],
            }

        repair_attempt_stats.append(deepcopy(stats))
        operation_ids.append(operation_id)
        validation = validate_and_apply_strict_terminology_response(
            source_text, translation, response, eligibility, glossary_entries,
            high_priority_categories, approved_reader_facing_terms,
            chapter_number=chapter,
        )
        empty_response = _is_exact_empty_patch_response(response)
        if (
            empty_response
            and validation["errors"] == [EMPTY_PATCH_VALIDATION_ERROR]
        ):
            safe_correctable_reason = EMPTY_PATCH_CORRECTION_REASON
        elif validation.get("safe_correctable") is True:
            safe_correctable_reason = validation.get("safe_correctable_reason")
        else:
            safe_correctable_reason = None
        correction_blocked_reason = None
        if safe_correctable_reason is not None:
            baseline_qa = qa_checker(translation)
            if not _qa_has_only_eligible_strict_blockers(
                baseline_qa, eligibility
            ):
                correction_blocked_reason = "unrelated_mandatory_qa_blocker"
        correction_eligible = bool(
            request_kind == "initial"
            and safe_correctable_reason in {
                EMPTY_PATCH_CORRECTION_REASON,
                INEFFECTIVE_PATCH_CORRECTION_REASON,
                BOUNDED_COMPLIANCE_CORRECTION_REASON,
            }
            and correction_blocked_reason is None
            and attempt_number < max_attempts
        )
        values = {
            **success_values(stats),
            "status": (
                "candidate_validated"
                if validation["accepted"]
                else "locally_rejected"
            ),
            "stats": deepcopy(stats),
            "validation_errors": validation["errors"],
            "validation_codes": validation.get("error_codes", []),
            "response_kind": (
                "empty_patches"
                if empty_response
                else "bounded_instruction_compliance_failure"
                if safe_correctable_reason == BOUNDED_COMPLIANCE_CORRECTION_REASON
                else "safe_ineffective_patches"
                if validation.get("safe_correctable") is True
                else "validated_patches"
                if validation["accepted"]
                else "rejected_nonempty_or_malformed"
            ),
            "correction_eligible": correction_eligible,
            "safe_correctable_reason": safe_correctable_reason,
            "correction_blocked_reason": correction_blocked_reason,
        }
        if empty_response:
            values["response_summary"] = {"patches": []}
        elif validation.get("diagnostic_patches"):
            values["response_summary"] = {
                "patches": deepcopy(validation["diagnostic_patches"])
            }
            values["response_retention"] = "bounded_region_anchored_patch_fields"
        if validation["accepted"]:
            values["patches"] = [{
                key: item[key]
                for key in ("region_id", "source_term", "old_text", "new_text")
            } for item in validation["patches_applied"]]
            values["candidate_content_hash"] = stable_text_hash(
                validation["repaired_text"]
            )
        current_meta = _replace_attempt(
            current_meta, pending_hash, operation_id, values
        )
        persist_meta(current_meta)
        newly_recorded = account_api_usage(operation_id, stats)
        current_meta = _replace_attempt(
            current_meta,
            pending_hash,
            operation_id,
            {
                "accounting_submitted": True,
                "accounting_newly_recorded": newly_recorded is True,
            },
        )
        persist_meta(current_meta)
        _safe_log(
            log_event, REPAIR_EVENT_TYPE, chapter=chapter,
            stage=values["status"], operation_id=operation_id,
            failure_identity=failure_identity, policy_version=policy_version,
            request_kind=request_kind, attempt=attempt_number,
            correction_eligible=correction_eligible,
            safe_correctable_reason=safe_correctable_reason,
            correction_blocked_reason=correction_blocked_reason,
            prompt_tokens=stats.get("prompt_tokens", 0),
            completion_tokens=stats.get("completion_tokens", 0),
            cache_hit_tokens=stats.get("cache_hit_tokens", 0),
            cache_miss_tokens=stats.get("cache_miss_tokens", 0),
            pricing_period=stats.get("pricing_period"),
            api_cost=stats.get("cost_usd", 0),
            validation_errors=validation["errors"],
            validation_codes=validation.get("error_codes", []),
        )
        if not validation["accepted"]:
            if correction_eligible:
                correction_context = _correction_context_from_attempt(values)
                request_kind = correction_context["request_kind"]
                continue
            return {
                "status": "locally_rejected", "translation": translation,
                "meta": current_meta, "eligibility": eligibility,
                "api_called": True, "repair_stats": stats,
                "repair_attempt_stats": repair_attempt_stats,
                "validation": validation, "operation_id": operation_id,
                "operation_ids": operation_ids,
            }

        candidate = validation["repaired_text"]
        qa = qa_checker(candidate)
        baseline_qa = (
            None if qa.get("commit_allowed", False) else qa_checker(translation)
        )
        qa_transition = _strict_qa_transition(
            baseline_qa or {}, qa, eligibility
        )
        if not qa_transition["accepted"]:
            current_meta = _replace_attempt(
                current_meta,
                pending_hash,
                operation_id,
                {
                    "status": "qa_rejected",
                    "candidate_qa": deepcopy(qa),
                    "qa_transition": deepcopy(qa_transition),
                    "correction_eligible": False,
                },
            )
            persist_meta(current_meta)
            return {
                "status": "qa_rejected", "translation": translation,
                "meta": current_meta, "eligibility": eligibility,
                "api_called": True, "repair_stats": stats,
                "repair_attempt_stats": repair_attempt_stats,
                "validation": validation, "qa": qa,
                "baseline_qa": baseline_qa,
                "qa_transition": qa_transition,
                "operation_id": operation_id, "operation_ids": operation_ids,
            }

        repaired_meta = pending_meta_after_strict_terminology_repair(
            current_meta, validation["patches_applied"]
        )
        repaired_meta = _replace_attempt(
            repaired_meta,
            pending_hash,
            operation_id,
            {
                "status": (
                    "applied_with_unrelated_blockers"
                    if qa_transition["partial"] else "applied"
                ),
                "candidate_qa": deepcopy(qa),
                "qa_transition": deepcopy(qa_transition),
                "correction_eligible": False,
            },
        )
        # Persist invalidated metadata before atomically replacing authoritative text.
        persist_meta(repaired_meta)
        persist_translation(candidate)
        return {
            "status": (
                "succeeded_with_unrelated_blockers"
                if qa_transition["partial"] else "succeeded"
            ),
            "translation": candidate,
            "meta": repaired_meta, "eligibility": eligibility,
            "api_called": True, "repair_stats": stats,
            "repair_attempt_stats": repair_attempt_stats,
            "validation": validation, "qa": qa,
            "baseline_qa": baseline_qa,
            "qa_transition": qa_transition,
            "operation_id": operation_id, "operation_ids": operation_ids,
        }
