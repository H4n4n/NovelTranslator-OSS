"""Read-only local consistency validation for NovelTranslator."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from chapter_library import (
    UNIT_KIND_CHAPTER,
    UNIT_KIND_EPILOGUE,
    UNIT_KIND_PROLOGUE,
    UNIT_KIND_SIDE_STORY,
    UNIT_KIND_SUPPLEMENT,
    production_unit_kind,
    validate_chapter_library,
)
from noveltranslator_core import (
    PRICING_VERSION,
    legacy_strict_glossary_categories,
    strict_glossary_entry_origin,
    configured_strict_glossary_entry_origin,
    glossary_strict_qa_errors,
    inspect_pending_state,
    normalize_category,
    validate_progress_consistency,
)
from novel_project import select_project
from translation_profile import load_translation_profile
from glossary_integrity import (
    LEGACY_NORMALIZED_OR_MANUAL,
    SOURCE_BACKED,
    UNBACKED_PROVISIONAL,
    UNRESOLVED,
    classify_glossary_source_entries,
)


JSON_FILES = (
    "chapters.json",
    "glossary.json",
    "progress.json",
    "continuity.json",
    "issues.json",
    "audit_history.json",
)


def load_json_report(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def numeric_chapter_files(directory):
    result = set()
    if not directory.exists():
        return result
    for path in directory.glob("*.md"):
        if path.stem.isdigit():
            result.add(int(path.stem))
    return result


def atomic_temp_files(*directories):
    return sorted(
        path
        for directory in directories
        if directory.exists()
        for path in directory.glob("*.tmp")
    )


def terminology_policy_counts(entries, high_priority_categories=(), *, chapter_number=None):
    """Report overlapping policy classes without source keys or chapter prose.

    Raw explicit declarations remain visible even when invalid; startup metadata
    validation reports those errors separately. Effective/legacy counts require
    locked status. Accepted-form counts describe non-empty list declarations.
    """
    high_priority = {normalize_category(item) for item in high_priority_categories}
    legacy_categories = legacy_strict_glossary_categories(high_priority_categories)
    counts = dict.fromkeys((
        "Locked canonical entries",
        "High-priority locked entries (review importance)",
        "Legacy hard terminology entries",
        "Explicit strict terminology entries",
        "Legacy and explicit hard entries (overlap)",
        "Effective strict terminology entries",
        "Entries with accepted inflections",
        "Automatic strict-term repair eligible",
        "Explicit automatic-repair opt-ins",
        "Explicit automatic-repair opt-outs",
        "Entries with approved repair aliases",
    ), 0)
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        locked = entry.get("status") == "locked"
        origin = configured_strict_glossary_entry_origin(entry, legacy_categories)
        counts["Locked canonical entries"] += locked
        counts["High-priority locked entries (review importance)"] += (
            locked and normalize_category(entry.get("category", "")) in high_priority
        )
        counts["Legacy hard terminology entries"] += origin in {"legacy", "legacy+explicit"}
        counts["Explicit strict terminology entries"] += entry.get("strict_qa") is True
        counts["Legacy and explicit hard entries (overlap)"] += origin == "legacy+explicit"
        counts["Effective strict terminology entries"] += origin is not None
        inflections = entry.get("accepted_inflections", [])
        counts["Entries with accepted inflections"] += isinstance(inflections, list) and bool(inflections)
        counts["Automatic strict-term repair eligible"] += (
            origin is not None and entry.get("auto_repair") is not False
        )
        counts["Explicit automatic-repair opt-ins"] += entry.get("auto_repair") is True
        counts["Explicit automatic-repair opt-outs"] += entry.get("auto_repair") is False
        counts["Entries with approved repair aliases"] += "repair_aliases" in entry
    counts["Activation-bounded explicit strict entries"] = sum(
        e.get("strict_qa") is True and "strict_qa_from_chapter" in e
        for e in entries.values() if isinstance(e, dict)
    )
    # These inventory counts include future policy; they do not claim it is active.
    counts["Configured hard terminology entries"] = counts.pop("Effective strict terminology entries")
    counts["Configured automatic strict-term repair eligible"] = counts.pop("Automatic strict-term repair eligible")
    if chapter_number is not None:
        origins = [strict_glossary_entry_origin(e, legacy_categories, chapter_number=chapter_number)
                   for e in entries.values() if isinstance(e, dict)]
        active_explicit = sum(o in {"explicit", "legacy+explicit"} for o in origins)
        counts["Active explicit strict entries at supplied chapter"] = active_explicit
        counts["Future explicit strict entries at supplied chapter"] = sum(
            e.get("status") == "locked" and e.get("strict_qa") is True
            for e in entries.values() if isinstance(e, dict)
        ) - active_explicit
        counts["Effective strict terminology entries"] = sum(o is not None for o in origins)
        counts["Automatic strict-term repair eligible"] = sum(
            o is not None and e.get("auto_repair") is not False
            for e, o in zip((e for e in entries.values() if isinstance(e, dict)), origins)
        )
    return counts


def validate_project(project):
    root = project.root
    data_dir = project.data_dir
    chapter_dir = project.chapter_dir
    pending_dir = project.pending_dir
    print("NovelTranslator local validation (zero API calls)")
    print("=" * 52)
    print(f"Project: {project.config.id} ({project.config.title})")
    print(f"Project root: {root}")
    print(f"Source type: {project.config.source_type}")
    profile = None
    profile_error = None
    try:
        profile = load_translation_profile(
            project.config.profile, project.repository_root
        )
        print(f"Profile: {profile.id} ({profile.display_name}) — valid")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        profile_error = f"translation profile {project.config.profile!r} is invalid: {exc}"
        print(f"Profile: {project.config.profile} — INVALID")
    loaded, json_errors = {}, []
    for name in JSON_FILES:
        value, error = load_json_report(data_dir / name)
        if error:
            json_errors.append(f"data/{name}: {error}")
            print(f"JSON {name}: INVALID")
        else:
            loaded[name] = value
            print(f"JSON {name}: valid")

    progress = loaded.get("progress.json")
    glossary = loaded.get("glossary.json")
    audit_history = loaded.get("audit_history.json")
    chapter_library = loaded.get("chapters.json")
    critical = list(json_errors)
    if profile_error:
        critical.append(profile_error)
    warnings = []

    chapter_library_errors = validate_chapter_library(
        chapter_library,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    critical.extend(chapter_library_errors)
    print("\nSource chapter library")
    print(f"  Numbering mode: {project.config.chapter_numbering}")
    print(
        "  Production units: "
        + (str(len(chapter_library)) if isinstance(chapter_library, list) else "unknown")
    )
    if isinstance(chapter_library, list):
        kind_counts = Counter()
        for unit in chapter_library:
            try:
                kind_counts[production_unit_kind(unit)] += 1
            except ValueError:
                kind_counts["invalid"] += 1
        print(f"  Chapter units: {kind_counts[UNIT_KIND_CHAPTER]}")
        print(f"  Prologue units: {kind_counts[UNIT_KIND_PROLOGUE]}")
        print(f"  Epilogue units: {kind_counts[UNIT_KIND_EPILOGUE]}")
        print(f"  Side-story units: {kind_counts[UNIT_KIND_SIDE_STORY]}")
        print(f"  Supplement units: {kind_counts[UNIT_KIND_SUPPLEMENT]}")
    print(
        "  Structure: "
        + ("valid" if not chapter_library_errors else "INVALID")
    )

    if not isinstance(progress, dict):
        critical.append("progress.json is unavailable or is not a JSON object")
        progress = {}
    completed = progress.get("completed", []) if isinstance(progress.get("completed", []), list) else []
    committed_files = numeric_chapter_files(chapter_dir)
    progress_report = validate_progress_consistency(progress, committed_files)
    critical.extend(progress_report["errors"])
    accounting_fields = (
        "total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
        "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd",
    )
    for field in accounting_fields:
        value = progress.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            critical.append(f"progress.{field} must be a non-negative number")
    cost_start = progress.get("rate_correct_cost_start_chapter")
    if not isinstance(cost_start, int) or not 1 <= cost_start <= progress.get("next_chapter", 0):
        critical.append("progress.rate_correct_cost_start_chapter is outside the valid range")
    from production_accounting import validate_progress_accounting, accounting_report_lines
    try:
        validate_progress_accounting(progress, direct_pricing_version=PRICING_VERSION)
        for line in accounting_report_lines(progress):
            print(line)
    except ValueError as exc:
        critical.append(str(exc))

    print("\nCheckpoint")
    print(f"  Current completed chapter: {progress.get('last_completed', 'unknown')}")
    print(f"  Next chapter: {progress.get('next_chapter', 'unknown')}")
    print(f"  Missing committed chapter files: {progress_report['missing_committed_files'] or 'none'}")
    print(f"  Unexpected extra committed files: {progress_report['unexpected_committed_files'] or 'none'}")

    pending_report = inspect_pending_state(
        pending_dir, completed, progress.get("next_chapter")
    )
    critical.extend(pending_report["errors"])
    warnings.extend(pending_report["warnings"])
    stale_temps = atomic_temp_files(data_dir, chapter_dir, pending_dir)
    if stale_temps:
        warnings.append(
            "interrupted atomic-write temp files exist: "
            + ", ".join(str(path.relative_to(root)) for path in stale_temps)
        )
    print("\nPending recovery state")
    if pending_report["pending"]:
        for item in pending_report["pending"]:
            print(f"  Chapter {item['chapter']}: {item['stage']}")
    else:
        print("  Pending chapters: none")

    entries = glossary.get("entries", {}) if isinstance(glossary, dict) else {}
    if not isinstance(entries, dict):
        critical.append("glossary.json entries must be an object")
        entries = {}
    invalid_categories = Counter()
    for entry in entries.values():
        if not isinstance(entry, dict):
            invalid_categories["(non-object entry)"] += 1
            continue
        raw_category = entry.get("category", "")
        if profile is not None and normalize_category(raw_category) not in profile.allowed_categories:
            invalid_categories[raw_category or "(blank)"] += 1
    if invalid_categories:
        warnings.append("glossary contains legacy or invalid category labels")
    high_priority_categories = (
        profile.high_priority_categories if profile is not None else ()
    )
    strict_qa_errors = glossary_strict_qa_errors(
        entries, project.config.id, high_priority_categories
    )
    critical.extend(strict_qa_errors)
    policy_counts = terminology_policy_counts(entries, high_priority_categories)
    if not strict_qa_errors and type(progress.get("next_chapter")) is int and progress["next_chapter"] >= 1:
        print(f"  Terminology policy chapter: {progress['next_chapter']}")
        policy_counts = terminology_policy_counts(
            entries, high_priority_categories, chapter_number=progress["next_chapter"])
    source_integrity_counts = {
        SOURCE_BACKED: 0,
        LEGACY_NORMALIZED_OR_MANUAL: 0,
        UNBACKED_PROVISIONAL: 0,
        UNRESOLVED: 0,
    }
    if isinstance(chapter_library, list):
        source_integrity = classify_glossary_source_entries(
            entries,
            (unit.get("text", "") for unit in chapter_library if isinstance(unit, dict)),
        )
        for item in source_integrity.values():
            source_integrity_counts[item["classification"]] += 1
        if source_integrity_counts[UNBACKED_PROVISIONAL]:
            warnings.append(
                f"{source_integrity_counts[UNBACKED_PROVISIONAL]} unbacked provisional "
                "glossary entries are excluded from periodic audits"
            )
        if source_integrity_counts[UNRESOLVED]:
            warnings.append(
                f"{source_integrity_counts[UNRESOLVED]} locked/historical source-absent "
                "glossary entries remain preserved but unresolved"
            )
    print("\nGlossary")
    print(f"  Entry count: {len(entries)}")
    for label, count in policy_counts.items():
        print(f"  {label}: {count}")
    print(f"  Source-backed entries: {source_integrity_counts[SOURCE_BACKED]}")
    print(
        "  Explicit legacy/manual source identities: "
        f"{source_integrity_counts[LEGACY_NORMALIZED_OR_MANUAL]}"
    )
    print(
        "  Unbacked provisional entries excluded from audits: "
        f"{source_integrity_counts[UNBACKED_PROVISIONAL]}"
    )
    print(
        "  Preserved unresolved source-absent entries: "
        f"{source_integrity_counts[UNRESOLVED]}"
    )
    if invalid_categories:
        print("  Invalid categories:")
        for category, count in sorted(invalid_categories.items()):
            print(f"    {category}: {count}")
    else:
        print("  Invalid categories: none")

    last_completed = progress.get("last_completed", 0)
    last_audited = progress.get("last_audited_chapter", 0)
    audits = audit_history.get("audits", []) if isinstance(audit_history, dict) else []
    history_last = max(
        (item.get("end_chapter", 0) for item in audits if isinstance(item, dict)),
        default=0,
    )
    if not isinstance(last_audited, int) or last_audited < 0 or last_audited > last_completed:
        critical.append("last_audited_chapter is outside the completed range")
    if history_last != last_audited:
        warnings.append(
            f"audit history ends at {history_last}, while progress records {last_audited}"
        )
    chapters_since_audit = last_completed - last_audited if isinstance(last_audited, int) else "unknown"
    audit_due = (
        isinstance(chapters_since_audit, int)
        and chapters_since_audit >= project.config.audit_interval
    )
    print("\nAudit checkpoint")
    print(f"  Last audited chapter: {last_audited}")
    print(f"  Audit history ends at: {history_last}")
    print(f"  Chapters since audit: {chapters_since_audit}")
    print(f"  Audit currently due: {'yes' if audit_due else 'no'}")

    print("\nFindings")
    if critical:
        for problem in critical:
            print(f"  ERROR: {problem}")
    if warnings:
        for problem in warnings:
            print(f"  WARNING: {problem}")
    if not critical and not warnings:
        print("  None")

    safe = not critical
    print("\nSAFE TO RESUME: " + ("YES" if safe else "NO"))
    return 0 if safe else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Validate one NovelTranslator project without API calls"
    )
    parser.add_argument(
        "--novel",
        help="novel identifier under novels/; omit for the legacy root project",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return validate_project(select_project(args.novel))


if __name__ == "__main__":
    sys.exit(main())
