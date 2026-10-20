"""Read-only project-level semantic/entity integrity audit helpers.

This module performs no API work and never writes project data. It scans only
already-committed translations, applies the calibrated semantic-integrity
policy, and aggregates known-rendering drift evidence across chapters.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from chapter_library import chapter_production_index
from semantic_integrity import rendering_drift_report
from semantic_integrity_policy import calibrated_entity_integrity_report


PROTECTED_MUTABLE_FILES = (
    "glossary",
    "progress",
    "continuity",
    "issues",
    "audit_history",
)


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_tree(path):
    path = Path(path)
    if not path.exists():
        return {"exists": False, "files": {}}
    if not path.is_dir():
        return {"exists": True, "files": {".": _sha256_file(path)}}
    files = {}
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        files[item.relative_to(path).as_posix()] = _sha256_file(item)
    return {"exists": True, "files": files}


def protected_state_snapshot(project):
    """Fingerprint mutable production state without modifying any file."""
    file_map = {
        "glossary": project.glossary_file,
        "progress": project.progress_file,
        "continuity": project.continuity_file,
        "issues": project.issues_file,
        "audit_history": project.audit_history_file,
    }
    files = {}
    for label in PROTECTED_MUTABLE_FILES:
        path = Path(file_map[label])
        files[label] = (
            {"exists": True, "sha256": _sha256_file(path)}
            if path.is_file()
            else {"exists": False, "sha256": ""}
        )
    return {
        "files": files,
        "pending": _snapshot_tree(project.pending_dir),
    }


def _validated_completed(progress):
    completed = progress.get("completed", []) if isinstance(progress, dict) else []
    if not isinstance(completed, list):
        raise ValueError("progress.completed must be a list")
    result = []
    for value in completed:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("progress.completed must contain positive chapter integers")
        result.append(value)
    if len(set(result)) != len(result):
        raise ValueError("progress.completed contains duplicate chapters")
    return tuple(sorted(result))


def _selected_chapters(completed, start=None, end=None):
    if start is not None and (
        not isinstance(start, int) or isinstance(start, bool) or start < 1
    ):
        raise ValueError("start must be a positive integer")
    if end is not None and (
        not isinstance(end, int) or isinstance(end, bool) or end < 1
    ):
        raise ValueError("end must be a positive integer")
    if start is not None and end is not None and end < start:
        raise ValueError("end must be greater than or equal to start")
    return tuple(
        chapter
        for chapter in completed
        if (start is None or chapter >= start) and (end is None or chapter <= end)
    )


def _decorate_issue(issue, chapter):
    return {"chapter": chapter, **dict(issue)}


def _drift_observations(chapter, report):
    records = []
    observation_by_source = {
        item.get("source_key"): item
        for item in report.get("observations", [])
        if isinstance(item, dict) and item.get("source_key")
    }
    for observation in observation_by_source.values():
        for rendering in observation.get("matched_renderings", []) or ():
            if not isinstance(rendering, str) or not rendering.strip():
                continue
            records.append({
                "chapter": chapter,
                "source_key": observation.get("source_key", ""),
                "category": observation.get("category", ""),
                "canonical": observation.get("canonical", ""),
                "rendering": rendering.strip(),
                "evidence": "approved_rendering",
            })

    # A known configured repair alias is valuable cross-chapter drift evidence
    # even though it deliberately does not count as an approved final rendering.
    for issue in (*report.get("advisories", []), *report.get("strict_blockers", [])):
        if not isinstance(issue, dict):
            continue
        source_key = issue.get("source_key")
        observation = observation_by_source.get(source_key, {})
        for hit in issue.get("known_alias_hits", []) or ():
            if not isinstance(hit, dict):
                continue
            rendering = hit.get("rendering")
            if not isinstance(rendering, str) or not rendering.strip():
                continue
            records.append({
                "chapter": chapter,
                "source_key": source_key or "",
                "category": issue.get("category", ""),
                "canonical": issue.get("canonical", observation.get("canonical", "")),
                "rendering": rendering.strip(),
                "evidence": "known_noncanonical_alias",
            })
    return records


def audit_committed_project(
    project,
    start=None,
    end=None,
    *,
    include_experimental=False,
):
    """Audit committed chapters and return a deterministic in-memory report.

    Diagnostics and advisories never modify or block the project. Existing
    glossary ``strict_qa`` rules remain blockers. Cross-entity substitution
    signals are suppressed unless ``include_experimental`` is explicitly true.
    Structural inconsistencies are returned under ``errors``.
    """
    if not isinstance(include_experimental, bool):
        raise ValueError("include_experimental must be boolean")

    chapters = _read_json(project.chapters_file, "chapter library")
    glossary_data = _read_json(project.glossary_file, "glossary")
    progress = _read_json(project.progress_file, "progress")
    if not isinstance(chapters, list):
        raise ValueError("chapter library must be a list")
    if not isinstance(glossary_data, dict) or not isinstance(
        glossary_data.get("entries"), dict
    ):
        raise ValueError("glossary must contain an entries object")

    source_by_chapter = {}
    for entry in chapters:
        number = chapter_production_index(entry)
        if number in source_by_chapter:
            raise ValueError(f"duplicate source chapter/production index: {number}")
        source_by_chapter[number] = entry

    completed = _validated_completed(progress)
    selected = _selected_chapters(completed, start, end)
    glossary_entries = glossary_data["entries"]

    errors = []
    diagnostics = []
    advisories = []
    experimental_signals = []
    strict_blockers = []
    chapter_summaries = []
    drift_observations = []
    source_backed_total = 0
    suppressed_experimental_total = 0

    for chapter in selected:
        source_entry = source_by_chapter.get(chapter)
        if source_entry is None:
            errors.append({
                "chapter": chapter,
                "type": "missing_source_chapter",
            })
            continue
        source_text = source_entry.get("text")
        if not isinstance(source_text, str) or not source_text.strip():
            errors.append({
                "chapter": chapter,
                "type": "missing_source_text",
            })
            continue
        translation_path = Path(project.chapter_dir) / f"{chapter:04d}.md"
        if not translation_path.is_file():
            errors.append({
                "chapter": chapter,
                "type": "missing_committed_translation",
                "path": str(translation_path),
            })
            continue
        translation = translation_path.read_text(encoding="utf-8")
        report = calibrated_entity_integrity_report(
            source_text,
            translation,
            glossary_entries,
            include_experimental=include_experimental,
        )
        chapter_diagnostics = [
            _decorate_issue(item, chapter) for item in report["diagnostics"]
        ]
        chapter_advisories = [
            _decorate_issue(item, chapter) for item in report["advisories"]
        ]
        chapter_experimental = [
            _decorate_issue(item, chapter)
            for item in report["experimental_signals"]
        ]
        chapter_blockers = [
            _decorate_issue(item, chapter) for item in report["strict_blockers"]
        ]
        diagnostics.extend(chapter_diagnostics)
        advisories.extend(chapter_advisories)
        experimental_signals.extend(chapter_experimental)
        strict_blockers.extend(chapter_blockers)
        drift_observations.extend(_drift_observations(chapter, report))
        source_backed_total += report["source_backed_identity_count"]
        suppressed_experimental_total += report[
            "suppressed_experimental_signal_count"
        ]
        chapter_summaries.append({
            "chapter": chapter,
            "source_backed_identity_count": report["source_backed_identity_count"],
            "diagnostic_count": len(chapter_diagnostics),
            "advisory_count": len(chapter_advisories),
            "experimental_signal_count": len(chapter_experimental),
            "suppressed_experimental_signal_count": report[
                "suppressed_experimental_signal_count"
            ],
            "strict_blocker_count": len(chapter_blockers),
        })

    drift = rendering_drift_report(drift_observations)
    diagnostic_types = Counter(item.get("type", "unknown") for item in diagnostics)
    advisory_types = Counter(item.get("type", "unknown") for item in advisories)
    experimental_types = Counter(
        item.get("type", "unknown") for item in experimental_signals
    )
    blocker_types = Counter(item.get("type", "unknown") for item in strict_blockers)
    diagnostic_categories = Counter(
        item.get("category", "unknown") for item in diagnostics
    )
    advisory_categories = Counter(item.get("category", "unknown") for item in advisories)
    experimental_categories = Counter(
        item.get("category", "unknown") for item in experimental_signals
    )
    chapters_with_diagnostics = sorted({item["chapter"] for item in diagnostics})
    chapters_with_advisories = sorted({item["chapter"] for item in advisories})
    chapters_with_experimental = sorted(
        {item["chapter"] for item in experimental_signals}
    )
    chapters_with_blockers = sorted({item["chapter"] for item in strict_blockers})

    return {
        "novel_id": project.config.id,
        "title": project.config.title,
        "mode": "read_only_semantic_integrity_audit",
        "api_calls": 0,
        "writes_performed": 0,
        "requested_range": {"start": start, "end": end},
        "experimental_signals_enabled": include_experimental,
        "completed_chapters_total": len(completed),
        "selected_chapters": list(selected),
        "selected_chapter_count": len(selected),
        "audited_chapter_count": len(chapter_summaries),
        "source_backed_identity_observations": source_backed_total,
        "chapters_with_diagnostics": chapters_with_diagnostics,
        "chapters_with_diagnostic_count": len(chapters_with_diagnostics),
        "diagnostic_count": len(diagnostics),
        "diagnostic_types": dict(sorted(diagnostic_types.items())),
        "diagnostic_categories": dict(sorted(diagnostic_categories.items())),
        "chapters_with_advisories": chapters_with_advisories,
        "chapters_with_advisory_count": len(chapters_with_advisories),
        "advisory_count": len(advisories),
        "advisory_types": dict(sorted(advisory_types.items())),
        "advisory_categories": dict(sorted(advisory_categories.items())),
        "chapters_with_experimental_signals": chapters_with_experimental,
        "chapters_with_experimental_signal_count": len(chapters_with_experimental),
        "experimental_signal_count": len(experimental_signals),
        "experimental_signal_types": dict(sorted(experimental_types.items())),
        "experimental_signal_categories": dict(
            sorted(experimental_categories.items())
        ),
        "suppressed_experimental_signal_count": suppressed_experimental_total,
        "chapters_with_strict_blockers": chapters_with_blockers,
        "chapters_with_strict_blocker_count": len(chapters_with_blockers),
        "strict_blocker_count": len(strict_blockers),
        "strict_blocker_types": dict(sorted(blocker_types.items())),
        "drift_term_count": drift["drift_term_count"],
        "drift": drift["drift"],
        "errors": errors,
        "chapter_summaries": chapter_summaries,
        "diagnostics": diagnostics,
        "advisories": advisories,
        "experimental_signals": experimental_signals,
        "strict_blockers": strict_blockers,
    }
