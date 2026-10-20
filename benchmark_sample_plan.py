"""Deterministic mature-glossary benchmark sample planning.

This module is benchmark-only and import-safe. It performs no credential loading,
network activity, provider calls, or production-state mutation.

The planner deliberately mixes stress and representative sampling:
- ten chronological strata across ordinary chapters;
- one glossary/terminology-heavy chapter per stratum;
- one dialogue-heavy chapter per stratum;
- one deterministic representative chapter per stratum.

Existing benchmark chapters can be explicitly excluded so a larger study uses
independent source units. Every selected row binds the exact source, mature
relevant glossary context, and Luna/DeepSeek request bodies by SHA256.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import statistics
import sys

from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    relevant_glossary_for_chapter,
    request_kwargs,
    resolve_output_dir,
)
from chapter_library import UNIT_KIND_CHAPTER, production_unit_kind
from noveltranslator_core import (
    STRICT_LOCKED_GLOSSARY_CATEGORIES,
    atomic_write_json,
    effective_strict_glossary_entry_items,
    glossary_entries_to_text,
    normalize_category,
)


PLAN_SCHEMA_VERSION = 2
DEFAULT_STRATA = 10
DEFAULT_PER_STRATUM = 3
DEFAULT_SEED = "mature-glossary-engine-benchmark-v2"
PLAN_FILENAME = "sample_plan.json"


class SamplePlanError(BenchmarkError):
    pass


@dataclass(frozen=True)
class ChapterMetrics:
    chapter_number: int
    ordinal_position: int
    source_character_count: int
    source_paragraph_count: int
    distinct_relevant_glossary_terms: int
    relevant_glossary_occurrences: int
    relevant_locked_terms: int
    relevant_provisional_terms: int
    relevant_contextual_terms: int
    high_priority_relevant_terms: int
    effective_strict_relevant_terms: int
    relevant_glossary_character_count: int
    prompt_message_character_count: int
    glossary_density_per_1000_chars: float
    dialogue_marker_count: int
    dialogue_density_per_1000_chars: float
    source_sha256: str
    relevant_glossary_sha256: str
    luna_request_sha256: str
    deepseek_request_sha256: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _canonical_json_sha256(value) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _paragraph_count(text: str) -> int:
    return len([part for part in (text or "").split("\n\n") if part.strip()])


def _dialogue_marker_count(text: str) -> int:
    # These are intentionally language-agnostic visible dialogue markers.
    # Counting both opening/closing characters is fine because only relative
    # density inside one novel/stratum is used for sampling.
    markers = ('"', "“", "”", "「", "」", "『", "』")
    return sum((text or "").count(marker) for marker in markers)


def _source_forms(source_term: str, entry: dict) -> tuple[str, ...]:
    values = [source_term]
    for field in ("source_variants", "source_aliases"):
        raw = entry.get(field, [])
        if isinstance(raw, (list, tuple)):
            values.extend(item for item in raw if isinstance(item, str) and item)
    # Longest first prevents a short alias from inflating a compound too much.
    return tuple(sorted(dict.fromkeys(values), key=lambda value: (-len(value), value)))


def _relevant_occurrence_count(source_text: str, relevant, glossary_entries) -> int:
    count = 0
    for item in relevant:
        if not isinstance(item, dict):
            continue
        source_term = item.get("chinese", "")
        if not isinstance(source_term, str) or not source_term:
            continue
        original_entry = glossary_entries.get(source_term, {})
        if not isinstance(original_entry, dict):
            original_entry = {}
        forms = _source_forms(source_term, original_entry)
        # For planning, count every exact visible source occurrence. This is a
        # stress metric only, not a correctness/ownership algorithm.
        count += sum((source_text or "").count(form) for form in forms)
    return count


def _render_relevant_glossary(inputs, chapter_number: int) -> tuple[dict, str]:
    relevant = relevant_glossary_for_chapter(inputs, chapter_number)
    rendered = glossary_entries_to_text(
        relevant,
        high_priority_categories=inputs.profile.high_priority_categories,
        strict_locked_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
        chapter_number=chapter_number,
    )
    return relevant, rendered


def chapter_metrics(inputs, chapter_number: int, ordinal_position: int) -> ChapterMetrics:
    chapter = inputs.chapters[chapter_number]
    source = chapter["text"]
    relevant, rendered = _render_relevant_glossary(inputs, chapter_number)
    chars = len(source)
    dialogue = _dialogue_marker_count(source)
    high_priority = {
        normalize_category(category)
        for category in inputs.profile.high_priority_categories
    }
    high_priority_count = 0
    status_counts = {"locked": 0, "provisional": 0, "contextual": 0}
    for item in relevant:
        if not isinstance(item, dict):
            continue
        category = normalize_category(item.get("category", ""))
        if category in high_priority:
            high_priority_count += 1
        status = item.get("status", "provisional")
        if status in status_counts:
            status_counts[status] += 1

    effective_strict_count = len(
        effective_strict_glossary_entry_items(
            relevant,
            inputs.profile.high_priority_categories,
            STRICT_LOCKED_GLOSSARY_CATEGORIES,
            chapter_number=chapter_number,
        )
    )
    occurrences = _relevant_occurrence_count(
        source, relevant, inputs.glossary_entries
    )
    luna_request = request_kwargs(inputs, chapter_number, "luna")
    deepseek_request = request_kwargs(inputs, chapter_number, "deepseek-pro")
    prompt_message_chars = sum(
        len(message.get("content", "") or "")
        for message in luna_request.get("messages", [])
        if isinstance(message, dict)
    )
    return ChapterMetrics(
        chapter_number=chapter_number,
        ordinal_position=ordinal_position,
        source_character_count=chars,
        source_paragraph_count=(
            chapter.get("paragraph_count")
            if isinstance(chapter.get("paragraph_count"), int)
            else _paragraph_count(source)
        ),
        distinct_relevant_glossary_terms=len(relevant),
        relevant_glossary_occurrences=occurrences,
        relevant_locked_terms=status_counts["locked"],
        relevant_provisional_terms=status_counts["provisional"],
        relevant_contextual_terms=status_counts["contextual"],
        high_priority_relevant_terms=high_priority_count,
        effective_strict_relevant_terms=effective_strict_count,
        relevant_glossary_character_count=len(rendered),
        prompt_message_character_count=prompt_message_chars,
        glossary_density_per_1000_chars=(occurrences * 1000.0 / max(chars, 1)),
        dialogue_marker_count=dialogue,
        dialogue_density_per_1000_chars=(dialogue * 1000.0 / max(chars, 1)),
        source_sha256=_sha256_text(source),
        relevant_glossary_sha256=_sha256_text(rendered),
        luna_request_sha256=_canonical_json_sha256(luna_request),
        deepseek_request_sha256=_canonical_json_sha256(deepseek_request),
    )


def _ordinary_chapters(inputs, excluded: set[int]) -> list[int]:
    chapters = []
    for number in sorted(inputs.chapters):
        if number in excluded:
            continue
        chapter = inputs.chapters[number]
        if production_unit_kind(chapter) != UNIT_KIND_CHAPTER:
            continue
        chapters.append(number)
    return chapters


def _partition(values: list[int], count: int) -> list[list[int]]:
    if count < 1:
        raise SamplePlanError("strata must be at least 1")
    if len(values) < count:
        raise SamplePlanError(
            f"only {len(values)} eligible ordinary chapters remain for {count} strata"
        )
    result = []
    n = len(values)
    for index in range(count):
        start = index * n // count
        end = (index + 1) * n // count
        bucket = values[start:end]
        if not bucket:
            raise SamplePlanError("chronological stratum unexpectedly became empty")
        result.append(bucket)
    return result


def _stable_rank(seed: str, novel_id: str, chapter_number: int) -> str:
    return _sha256_text(f"{seed}|{novel_id}|{chapter_number}")


def _representative_key(metric: ChapterMetrics, med_chars: float, med_glossary: float, seed: str, novel_id: str):
    char_distance = abs(metric.source_character_count - med_chars) / max(med_chars, 1)
    glossary_distance = abs(
        metric.glossary_density_per_1000_chars - med_glossary
    ) / max(med_glossary, 1.0)
    return (
        char_distance + glossary_distance,
        _stable_rank(seed, novel_id, metric.chapter_number),
        metric.chapter_number,
    )


def plan_sample(
    inputs,
    *,
    sample_size: int = 30,
    strata: int = DEFAULT_STRATA,
    excluded=(),
    seed: str = DEFAULT_SEED,
):
    if sample_size != strata * DEFAULT_PER_STRATUM:
        raise SamplePlanError(
            "v2 planner requires exactly three chapters per stratum "
            f"(sample_size={sample_size}, strata={strata})"
        )
    excluded = {int(value) for value in excluded}
    ordinary = _ordinary_chapters(inputs, excluded)
    buckets = _partition(ordinary, strata)
    ordinal = {chapter: index + 1 for index, chapter in enumerate(ordinary)}
    selected = []
    selections = []
    all_metrics = {}

    for stratum_index, bucket in enumerate(buckets, start=1):
        metrics = [
            chapter_metrics(inputs, chapter, ordinal[chapter])
            for chapter in bucket
        ]
        for metric in metrics:
            all_metrics[metric.chapter_number] = metric

        terminology = max(
            metrics,
            key=lambda metric: (
                metric.glossary_density_per_1000_chars,
                metric.high_priority_relevant_terms,
                metric.effective_strict_relevant_terms,
                metric.source_character_count,
                -metric.chapter_number,
            ),
        )
        remaining = [metric for metric in metrics if metric.chapter_number != terminology.chapter_number]
        dialogue = max(
            remaining,
            key=lambda metric: (
                metric.dialogue_density_per_1000_chars,
                metric.dialogue_marker_count,
                metric.source_character_count,
                -metric.chapter_number,
            ),
        )
        remaining = [
            metric
            for metric in remaining
            if metric.chapter_number != dialogue.chapter_number
        ]
        if not remaining:
            raise SamplePlanError(
                f"stratum {stratum_index} lacks a third independent chapter"
            )
        med_chars = statistics.median(
            metric.source_character_count for metric in metrics
        )
        med_glossary = statistics.median(
            metric.glossary_density_per_1000_chars for metric in metrics
        )
        representative = min(
            remaining,
            key=lambda metric: _representative_key(
                metric,
                med_chars,
                med_glossary,
                seed,
                inputs.project.config.id,
            ),
        )

        for role, metric in (
            ("terminology_stress", terminology),
            ("dialogue_stress", dialogue),
            ("representative", representative),
        ):
            selected.append(metric.chapter_number)
            selections.append(
                {
                    "stratum": stratum_index,
                    "role": role,
                    **asdict(metric),
                }
            )

    if len(selected) != sample_size or len(set(selected)) != sample_size:
        raise SamplePlanError("planner did not produce a unique full sample")

    before = capture_protected_state(inputs.project)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "benchmark": "mature-glossary-engine-benchmark-v2",
        "planner": {
            "strategy": "10 chronological strata x terminology/dialogue/representative",
            "sample_size": sample_size,
            "strata": strata,
            "seed": seed,
            "excluded_chapters": sorted(excluded),
        },
        "novel_id": inputs.project.config.id,
        "novel_title": inputs.project.config.title,
        "profile_id": inputs.profile.id,
        "source_language": inputs.project.config.source_language,
        "final_chapter": inputs.project.config.final_chapter,
        "eligible_ordinary_chapter_count": len(ordinary),
        "selected_chapters": selected,
        "selections": selections,
        "protected_state_binding": before,
    }


def write_plan(inputs, plan, output_dir: str):
    target = resolve_output_dir(inputs.project, output_dir)
    target.mkdir(parents=True, exist_ok=False)
    path = target / PLAN_FILENAME
    atomic_write_json(path, plan)
    after = capture_protected_state(inputs.project)
    if after != plan["protected_state_binding"]:
        raise BenchmarkSafetyError(
            "production-owned state changed while writing benchmark sample plan"
        )
    return path


def build_parser():
    parser = argparse.ArgumentParser(
        description="Plan a deterministic 30-chapter mature-glossary benchmark sample"
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--sample-size", type=int, default=30)
    parser.add_argument("--strata", type=int, default=10)
    parser.add_argument("--exclude", type=int, nargs="*", default=[])
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        inputs = load_benchmark_inputs(args.novel, args.repo_root)
        plan = plan_sample(
            inputs,
            sample_size=args.sample_size,
            strata=args.strata,
            excluded=args.exclude,
            seed=args.seed,
        )
        path = write_plan(inputs, plan, args.output_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=" * 88)
    print("MATURE-GLOSSARY ENGINE BENCHMARK SAMPLE PLAN")
    print("=" * 88)
    print(f"Novel      : {plan['novel_id']}")
    print(f"Language   : {plan['source_language']}")
    print(f"Selected   : {len(plan['selected_chapters'])}")
    print("Chapters   : " + ", ".join(map(str, plan["selected_chapters"])))
    print(f"Output     : {path}")
    print("Provider calls made: 0")
    print("Production state changed: NO")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
