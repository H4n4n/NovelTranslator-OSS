"""Build a lossless final corpus against a corrected TXT source spine.

This is a local-only post-production tool.  It never changes the installed
source library, production chapters, or mutable production state.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import tempfile
import unicodedata

import extract_txt
from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock


CHAPTER_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s*)?Chapter\s+(?P<number>\d+)\b[^\r\n]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)


class FinalizationError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def normalized_title_identity(title):
    value = unicodedata.normalize("NFKC", title or "").strip()
    return re.sub(r"\s+", " ", value).casefold()


def canonical_spine_hash(units):
    identities = [
        {
            "production_index": unit["production_index"],
            "unit_kind": unit["unit_kind"],
            "source_number": unit.get("source_number"),
            "source_heading": unit["source_heading"],
            "source_line_start": unit["source_line_start"],
            "source_line_end": unit["source_line_end"],
            "source_unit_sha256": unit["source_unit_sha256"],
            "source_document_sha256": unit["source_document_sha256"],
        }
        for unit in units
    ]
    return sha256_bytes(json.dumps(
        identities, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _require_sequential_indexes(units, label):
    indexes = [unit.get("production_index") for unit in units]
    expected = list(range(1, len(units) + 1))
    if indexes != expected:
        raise FinalizationError(f"{label} spine indexes are not sequential")


def _unique_hash_index(units, label):
    counts = Counter(unit.get("source_unit_sha256") for unit in units)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    if None in counts:
        raise FinalizationError(f"{label} spine contains a unit without a source hash")
    if duplicates:
        raise FinalizationError(
            f"{label} spine contains duplicate source identity: {duplicates[0]}"
        )
    return {
        unit["source_unit_sha256"]: unit["production_index"] for unit in units
    }


@dataclass(frozen=True)
class RepartitionPlan:
    historical_to_corrected: dict[int, int]
    merged_production_index: int
    split_final_indexes: tuple[int, int]


def plan_source_repartition(historical_units, corrected_units):
    """Prove one historical source unit became two corrected source units."""
    if len(corrected_units) != len(historical_units) + 1:
        raise FinalizationError(
            "finalization requires exactly one additional corrected source unit"
        )
    _require_sequential_indexes(historical_units, "historical")
    _require_sequential_indexes(corrected_units, "corrected")
    corrected_by_hash = _unique_hash_index(corrected_units, "corrected")
    _unique_hash_index(historical_units, "historical")

    matched = {}
    unmatched_historical = []
    for unit in historical_units:
        corrected_index = corrected_by_hash.get(unit["source_unit_sha256"])
        if corrected_index is None:
            unmatched_historical.append(unit["production_index"])
        else:
            matched[unit["production_index"]] = corrected_index

    matched_corrected = set(matched.values())
    unmatched_corrected = [
        unit["production_index"]
        for unit in corrected_units
        if unit["production_index"] not in matched_corrected
    ]
    if len(unmatched_historical) != 1 or len(unmatched_corrected) != 2:
        raise FinalizationError(
            "source spines do not contain one provable one-to-two repartition"
        )
    if unmatched_corrected[1] != unmatched_corrected[0] + 1:
        raise FinalizationError("corrected split units are not adjacent")

    ordered_matches = [matched[index] for index in sorted(matched)]
    if ordered_matches != sorted(ordered_matches):
        raise FinalizationError("source hash alignment changes narrative order")

    merged_index = unmatched_historical[0]
    split_indexes = tuple(unmatched_corrected)
    historical = historical_units[merged_index - 1]
    first = corrected_units[split_indexes[0] - 1]
    second = corrected_units[split_indexes[1] - 1]
    if historical["source_document_sha256"] != first["source_document_sha256"]:
        raise FinalizationError("historical and corrected spines use different sources")
    if first["source_document_sha256"] != second["source_document_sha256"]:
        raise FinalizationError("corrected split units use different source documents")
    if historical["source_line_start"] != first["source_line_start"]:
        raise FinalizationError("merged source range does not begin at split unit A")
    if historical["source_line_end"] != second["source_line_end"]:
        raise FinalizationError("merged source range does not end at split unit B")
    if first["source_line_end"] + 1 != second["source_line_start"]:
        raise FinalizationError("corrected source split ranges are not contiguous")
    if historical["source_heading"] != first["source_heading"]:
        raise FinalizationError("merged source heading does not identify split unit A")

    before = matched.get(merged_index - 1)
    after = matched.get(merged_index + 1)
    if before is not None and before != split_indexes[0] - 1:
        raise FinalizationError("source alignment before the split is inconsistent")
    if after is not None and after != split_indexes[1] + 1:
        raise FinalizationError("source alignment after the split is inconsistent")
    return RepartitionPlan(matched, merged_index, split_indexes)


def first_nonblank_line(data):
    text = data.decode("utf-8")
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def chapter_number_from_heading(line):
    match = CHAPTER_HEADING_RE.fullmatch((line or "").strip())
    return int(match.group("number")) if match else None


def split_merged_production(data, first_source_unit, second_source_unit):
    """Split at one exact embedded translated chapter heading, byte-losslessly."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FinalizationError(f"production chapter is not UTF-8: {exc}") from exc
    if text.encode("utf-8") != data:
        raise FinalizationError("production chapter cannot be losslessly decoded")
    first_number = first_source_unit.get("source_number")
    second_number = second_source_unit.get("source_number")
    if first_number is None or second_number is None:
        raise FinalizationError("split source units require numbered chapter identities")
    opening = first_nonblank_line(data)
    if chapter_number_from_heading(opening) != first_number:
        raise FinalizationError("merged production opening heading does not match split unit A")

    matches = [
        match for match in CHAPTER_HEADING_RE.finditer(text)
        if int(match.group("number")) == second_number and match.start() > 0
    ]
    if len(matches) != 1:
        raise FinalizationError(
            "merged production split heading is missing or ambiguous"
        )
    match = matches[0]
    byte_offset = len(text[:match.start()].encode("utf-8"))
    before = data[:byte_offset]
    after = data[byte_offset:]
    if not before.strip() or not after.strip():
        raise FinalizationError("merged production split would create an empty unit")
    if first_nonblank_line(after) != match.group(0).strip():
        raise FinalizationError("split marker would not become the second unit heading")
    if before + after != data:
        raise FinalizationError("production split is not byte-lossless")
    return before, after, {
        "byte_offset": byte_offset,
        "line_number": text.count("\n", 0, match.start()) + 1,
        "marker": match.group(0).strip(),
        "marker_occurrences": len(matches),
    }


def inventory_production_chapters(chapter_dir, expected_count):
    chapter_dir = Path(chapter_dir)
    numbered = {}
    for path in chapter_dir.glob("*.md"):
        if re.fullmatch(r"\d{4}\.md", path.name):
            numbered[int(path.stem)] = path
    expected = set(range(1, expected_count + 1))
    actual = set(numbered)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise FinalizationError(f"missing production unit {missing[0]:04d}.md")
    if extra:
        raise FinalizationError(f"unexpected production unit {extra[0]:04d}.md")
    inventory = []
    payloads = {}
    for index in range(1, expected_count + 1):
        data = numbered[index].read_bytes()
        payloads[index] = data
        text = data.decode("utf-8")
        headings = [match.group(0).strip() for match in CHAPTER_HEADING_RE.finditer(text)]
        inventory.append({
            "production_index": index,
            "file": numbered[index].name,
            "sha256": sha256_bytes(data),
            "byte_count": len(data),
            "character_count": len(text),
            "paragraph_count": len([part for part in re.split(r"\r?\n\s*\r?\n", text) if part.strip()]),
            "first_heading": first_nonblank_line(data),
            "chapter_heading_markers": headings,
        })
    return inventory, payloads


def build_final_segments(historical_units, corrected_units, production_payloads):
    plan = plan_source_repartition(historical_units, corrected_units)
    expected = set(range(1, len(historical_units) + 1))
    if set(production_payloads) != expected:
        missing = sorted(expected - set(production_payloads))
        if missing:
            raise FinalizationError(f"missing production unit {missing[0]:04d}.md")
        raise FinalizationError("production payload set does not match historical spine")

    first_index, second_index = plan.split_final_indexes
    first_source = corrected_units[first_index - 1]
    second_source = corrected_units[second_index - 1]
    merged_data = production_payloads[plan.merged_production_index]
    before, after, split = split_merged_production(
        merged_data, first_source, second_source
    )

    corrected_to_historical = {
        corrected: historical
        for historical, corrected in plan.historical_to_corrected.items()
    }
    segments = []
    for final_index, source_unit in enumerate(corrected_units, 1):
        if final_index == first_index:
            production_index = plan.merged_production_index
            data = before
            relationship = "slice_a"
            byte_start, byte_end = 0, split["byte_offset"]
        elif final_index == second_index:
            production_index = plan.merged_production_index
            data = after
            relationship = "slice_b"
            byte_start, byte_end = split["byte_offset"], len(merged_data)
        else:
            production_index = corrected_to_historical.get(final_index)
            if production_index is None:
                raise FinalizationError(f"corrected source unit {final_index} is unmapped")
            data = production_payloads[production_index]
            relationship = "whole"
            byte_start, byte_end = 0, len(data)
        segments.append({
            "final_index": final_index,
            "source_unit": source_unit,
            "production_index": production_index,
            "relationship": relationship,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "data": data,
        })

    verification = verify_lossless_segments(production_payloads, segments)
    if not verification["aggregate_byte_stream_equal"]:
        raise FinalizationError("final corpus is not a lossless production repartition")
    return segments, plan, split, verification


def verify_lossless_segments(production_payloads, segments):
    ranges = defaultdict(list)
    for segment in segments:
        ranges[segment["production_index"]].append(
            (segment["byte_start"], segment["byte_end"], segment["final_index"])
        )
    gaps = []
    overlaps = []
    for production_index, data in sorted(production_payloads.items()):
        cursor = 0
        for start, end, final_index in sorted(ranges.get(production_index, [])):
            if start > cursor:
                gaps.append({"production_index": production_index, "start": cursor, "end": start})
            if start < cursor:
                overlaps.append({
                    "production_index": production_index,
                    "final_index": final_index,
                    "start": start,
                    "previous_end": cursor,
                })
            cursor = max(cursor, end)
        if cursor < len(data):
            gaps.append({"production_index": production_index, "start": cursor, "end": len(data)})
    production_stream = b"".join(production_payloads[index] for index in sorted(production_payloads))
    final_stream = b"".join(segment["data"] for segment in segments)
    return {
        "production_aggregate_byte_stream_sha256": sha256_bytes(production_stream),
        "final_aggregate_byte_stream_sha256": sha256_bytes(final_stream),
        "production_aggregate_byte_count": len(production_stream),
        "final_aggregate_byte_count": len(final_stream),
        "aggregate_byte_stream_equal": production_stream == final_stream,
        "unmapped_production_byte_ranges": gaps,
        "overlapping_production_byte_ranges": overlaps,
        "unmapped_source_units": [],
        "duplicate_final_source_units": [],
    }


def _ratio_outliers(rows, key):
    values = [row[key] for row in rows if row[key] > 0]
    if not values:
        return [], None
    logs = [math.log(value) for value in values]
    median_log = statistics.median(logs)
    deviations = [abs(value - median_log) for value in logs]
    mad = statistics.median(deviations)
    flagged = []
    median_value = math.exp(median_log)
    for row in rows:
        value = row[key]
        robust_distance = (
            abs(math.log(value) - median_log) / mad if value > 0 and mad else 0.0
        )
        relative_to_median = value / median_value if median_value else 0.0
        materially_different = relative_to_median < 0.67 or relative_to_median > 1.5
        extreme = relative_to_median < 0.5 or relative_to_median > 2.0
        if value > 0 and (extreme or (robust_distance > 6 and materially_different)):
            flagged.append({
                "final_index": row["final_index"],
                "value": value,
                "relative_to_median": relative_to_median,
                "robust_log_distance": robust_distance,
            })
    return flagged, median_value


def build_alignment_report(segments):
    rows = []
    title_matches = []
    title_mismatches = []
    title_unparsed = []
    transitions = []
    previous_kind = None
    for segment in segments:
        source = segment["source_unit"]
        text = segment["data"].decode("utf-8")
        english_heading = first_nonblank_line(segment["data"])
        displayed_number = chapter_number_from_heading(english_heading)
        source_number = source.get("source_number")
        if source_number is not None and displayed_number == source_number:
            title_matches.append(segment["final_index"])
        elif source_number is not None and displayed_number is None:
            title_unparsed.append({
                "final_index": segment["final_index"],
                "source_number": source_number,
                "english_heading": english_heading,
            })
        elif source_number is not None:
            title_mismatches.append({
                "final_index": segment["final_index"],
                "source_number": source_number,
                "displayed_number": displayed_number,
                "english_heading": english_heading,
            })
        if source["unit_kind"] != previous_kind:
            transitions.append({
                "final_index": segment["final_index"],
                "unit_kind": source["unit_kind"],
                "source_number": source_number,
                "source_heading": source["source_heading"],
                "english_heading": english_heading,
            })
            previous_kind = source["unit_kind"]
        english_chars = len(text)
        english_paragraphs = len([
            part for part in re.split(r"\r?\n\s*\r?\n", text) if part.strip()
        ])
        source_chars = source["source_character_count"]
        source_paragraphs = source["paragraph_count"]
        rows.append({
            "final_index": segment["final_index"],
            "source_number": source_number,
            "unit_kind": source["unit_kind"],
            "source_character_count": source_chars,
            "english_character_count": english_chars,
            "character_ratio": english_chars / source_chars if source_chars else 0.0,
            "source_paragraph_count": source_paragraphs,
            "english_paragraph_count": english_paragraphs,
            "paragraph_ratio": english_paragraphs / source_paragraphs if source_paragraphs else 0.0,
        })
    character_outliers, median_character_ratio = _ratio_outliers(rows, "character_ratio")
    paragraph_outliers, median_paragraph_ratio = _ratio_outliers(rows, "paragraph_ratio")
    return {
        "unit_count": len(segments),
        "numeric_title_alignment": {
            "matched_count": len(title_matches),
            "matched_final_indexes": title_matches,
            "mismatches": title_mismatches,
            "unparsed": title_unparsed,
        },
        "unit_kind_transitions": transitions,
        "median_character_ratio": median_character_ratio,
        "major_character_ratio_outliers": character_outliers,
        "median_paragraph_ratio": median_paragraph_ratio,
        "major_paragraph_ratio_outliers": paragraph_outliers,
        "unit_metrics": rows,
    }


def load_corrected_txt_spine(project):
    source = extract_txt.inspect_utf8_txt(project.source_txt)
    overrides = extract_txt.load_txt_overrides(
        project, source["source_sha256"], source["line_record_count"]
    )
    result = extract_txt.extract_txt_text(
        source["text"], source["source_sha256"], overrides=overrides
    )
    result["report"].update({
        key: value for key, value in source.items() if key not in {"raw", "text"}
    })
    return result


def _manifest_mapping(segment):
    source = segment["source_unit"]
    data = segment["data"]
    return {
        "final_index": segment["final_index"],
        "final_file": f"chapters/{segment['final_index']:04d}.md",
        "final_sha256": sha256_bytes(data),
        "final_byte_count": len(data),
        "english_heading": first_nonblank_line(data),
        "source": {
            "unit_kind": source["unit_kind"],
            "source_number": source.get("source_number"),
            "source_heading": source["source_heading"],
            "normalized_title_identity": normalized_title_identity(source["title"]),
            "source_line_start": source["source_line_start"],
            "source_line_end": source["source_line_end"],
            "source_unit_sha256": source["source_unit_sha256"],
            "source_document_sha256": source["source_document_sha256"],
        },
        "production_provenance": {
            "production_index": segment["production_index"],
            "production_file": f"output/chapters/{segment['production_index']:04d}.md",
            "relationship": segment["relationship"],
            "byte_start": segment["byte_start"],
            "byte_end": segment["byte_end"],
        },
    }


def build_finalization_corpus(project, output_dir=None):
    output_dir = Path(output_dir or (project.root / "final"))
    if output_dir.exists():
        raise FinalizationError(f"finalization output already exists: {output_dir}")
    historical_units = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    corrected_result = load_corrected_txt_spine(project)
    corrected_units = corrected_result["chapters"]
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    completed = progress.get("completed", [])
    if completed != list(range(1, len(historical_units) + 1)):
        raise FinalizationError("production progress is not a complete sequential corpus")
    inventory, payloads = inventory_production_chapters(
        project.chapter_dir, len(historical_units)
    )
    segments, plan, split, verification = build_final_segments(
        historical_units, corrected_units, payloads
    )
    alignment = build_alignment_report(segments)
    mappings = [_manifest_mapping(segment) for segment in segments]
    manifest = {
        "schema_version": 1,
        "project_id": project.config.id,
        "migration_kind": "lossless-one-to-two-source-spine-repartition",
        "source": {
            "logical_unit_count": len(corrected_units),
            "source_document_sha256": corrected_result["report"]["source_sha256"],
            "corrected_spine_sha256": canonical_spine_hash(corrected_units),
            "narrative_coverage": corrected_result["report"]["narrative_coverage"],
        },
        "historical_production": {
            "logical_unit_count": len(historical_units),
            "historical_spine_sha256": canonical_spine_hash(historical_units),
            "chapter_file_count": len(inventory),
            "aggregate_byte_stream_sha256": verification["production_aggregate_byte_stream_sha256"],
        },
        "final": {
            "logical_unit_count": len(segments),
            "aggregate_byte_stream_sha256": verification["final_aggregate_byte_stream_sha256"],
            "lossless_byte_stream_equal": verification["aggregate_byte_stream_equal"],
        },
        "split": {
            "production_index": plan.merged_production_index,
            "final_indexes": list(plan.split_final_indexes),
            **split,
        },
        "mappings": mappings,
    }
    verification.update({
        "source_unit_count": len(corrected_units),
        "production_unit_count": len(historical_units),
        "final_unit_count": len(segments),
        "mapped_source_unit_count": len(mappings),
        "production_files_covered": len({item["production_index"] for item in segments}),
    })

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="finalization-", dir=output_dir.parent))
    try:
        chapters_dir = temp_root / "chapters"
        reports_dir = temp_root / "reports"
        chapters_dir.mkdir()
        reports_dir.mkdir()
        for segment in segments:
            (chapters_dir / f"{segment['final_index']:04d}.md").write_bytes(
                segment["data"]
            )
        (temp_root / "manifest.json").write_bytes(json_bytes(manifest))
        (reports_dir / "production_inventory.json").write_bytes(json_bytes({
            "unit_count": len(inventory), "units": inventory
        }))
        (reports_dir / "verification.json").write_bytes(json_bytes(verification))
        (reports_dir / "source_alignment.json").write_bytes(json_bytes(alignment))
        os.replace(temp_root, output_dir)
    except BaseException:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        raise
    return manifest, verification, alignment


def build_identity_finalization_corpus(project, output_dir):
    """Copy an unchanged committed spine losslessly to a NEW final workspace.

    The caller supplies source/audit/approval proofs and owns the novel lock.
    This seam grants no title, normalization, or historical-edit authority.
    """
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FinalizationError("finalization output already exists")
    count = project.config.final_chapter
    inventory, payloads = inventory_production_chapters(project.chapter_dir, count)
    expected_names = {f"{n:04d}.md" for n in range(1, count + 1)}
    if {p.name for p in project.chapter_dir.iterdir()} != expected_names:
        raise FinalizationError("identity finalization inventory drift")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="finalization-", dir=output_dir.parent))
    try:
        (temp_root / "chapters").mkdir()
        (temp_root / "reports").mkdir()
        for n, raw in payloads.items():
            (temp_root / "chapters" / f"{n:04d}.md").write_bytes(raw)
        actual = {n: (temp_root / "chapters" / f"{n:04d}.md").read_bytes() for n in payloads}
        if actual != payloads:
            raise FinalizationError("identity finalization staged bytes drift")
        (temp_root / "manifest.json").write_bytes(json_bytes({
            "schema_version": 1, "project_id": project.config.id,
            "migration_kind": "committed-identity-v1", "production_inventory": inventory,
        }))
        if inventory_production_chapters(project.chapter_dir, count)[1] != payloads:
            raise FinalizationError("production changed before identity finalization")
        os.rename(temp_root, output_dir)
    except BaseException:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        raise


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build a local-only lossless TXT finalization corpus (zero API calls)"
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--output-dir")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            manifest, verification, alignment = build_finalization_corpus(
                project, args.output_dir
            )
    except (
        OSError,
        ValueError,
        TypeError,
        FinalizationError,
        extract_txt.TXTExtractionError,
        ProductionRunLockedError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1
    print("NovelTranslator finalization corpus (zero API calls)")
    print("=" * 58)
    print(f"Project: {manifest['project_id']}")
    print(f"Source logical units: {manifest['source']['logical_unit_count']}")
    print(f"Historical production units: {manifest['historical_production']['logical_unit_count']}")
    print(f"Final English units: {manifest['final']['logical_unit_count']}")
    print(f"Split production unit: {manifest['split']['production_index']}")
    print(f"Split marker line: {manifest['split']['line_number']}")
    print(f"Lossless byte stream: {verification['aggregate_byte_stream_equal']}")
    print(f"Numeric heading mismatches: {len(alignment['numeric_title_alignment']['mismatches'])}")
    print("API calls used: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
