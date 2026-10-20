"""Read-only calibration for Chinese kinship age/order preservation.

This audit scans already committed Chinese-source chapters for mixed-gender
kinship compounds whose source form explicitly encodes relative age/order. It
classifies whether nearby English visibly preserves both gender and order,
gender only, generic siblinghood, or neither.

The signal is exploratory and non-blocking. The command never calls a provider
and never writes project data.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_config, load_novel_project, repository_root
from semantic_integrity_audit import protected_state_snapshot
from semantic_kinship import ordered_kinship_completeness_report
from source_language_filter import source_language_matches


AUDIT_MODE = "semantic_kinship_order_read_only_audit"


class SemanticKinshipAuditError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise SemanticKinshipAuditError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticKinshipAuditError(f"cannot read {label}: {path}: {exc}") from exc


def _discover(repo_root, source_language="zh"):
    novels_root = Path(repo_root) / "novels"
    selected = []
    errors = []
    if not novels_root.is_dir():
        raise SemanticKinshipAuditError(f"novels directory does not exist: {novels_root}")
    for directory in sorted(item for item in novels_root.iterdir() if item.is_dir()):
        config_path = directory / "novel.json"
        if not config_path.is_file():
            continue
        try:
            config = load_novel_config(config_path)
        except Exception as exc:
            errors.append(f"{config_path}: {exc}")
            continue
        if source_language_matches(config.source_language, source_language):
            selected.append(config.id)
    return selected, errors


def audit_project(novel_id, *, repo_root=None):
    project = load_novel_project(novel_id, repo_root=repo_root)
    if not source_language_matches(project.config.source_language, "zh"):
        raise SemanticKinshipAuditError(
            f"kinship-order audit currently supports Chinese source projects only: {novel_id}"
        )
    before = protected_state_snapshot(project)
    chapters = _read_json(project.chapters_file, "chapter library")
    progress = _read_json(project.progress_file, "progress")
    if not isinstance(chapters, list):
        raise SemanticKinshipAuditError("chapter library must be a list")
    if not isinstance(progress, dict):
        raise SemanticKinshipAuditError("progress must be an object")

    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise SemanticKinshipAuditError("progress.completed must be a list")
    completed = sorted({
        value for value in completed
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    })

    findings = []
    structural_errors = []
    chapters_scanned = 0
    occurrence_count = 0
    diagnostic_count = 0
    chapters_with_occurrences = set()
    chapters_with_diagnostics = set()
    classifications = Counter()

    for chapter in completed:
        source_entry = chapter_map.get(chapter)
        output_path = Path(project.chapter_dir) / f"{chapter:04d}.md"
        if not isinstance(source_entry, dict):
            structural_errors.append(f"Chapter {chapter} is missing from source library")
            continue
        if not output_path.is_file():
            structural_errors.append(f"Chapter {chapter} committed output is missing: {output_path}")
            continue
        chapters_scanned += 1
        source_text = source_entry.get("text", "")
        translation = output_path.read_text(encoding="utf-8")
        report = ordered_kinship_completeness_report(source_text, translation)
        observations = report.get("observations", [])
        diagnostics = report.get("diagnostics", [])
        occurrence_count += len(observations)
        diagnostic_count += len(diagnostics)
        if observations:
            chapters_with_occurrences.add(chapter)
        if diagnostics:
            chapters_with_diagnostics.add(chapter)
        for item in observations:
            classifications[item.get("classification", "unknown")] += 1
        for item in diagnostics:
            findings.append({"chapter": chapter, **dict(item)})

    unchanged = before == protected_state_snapshot(project)
    if not unchanged:
        structural_errors.append("protected production state changed during audit")
    return {
        "mode": AUDIT_MODE,
        "status": "PASS" if not structural_errors else "FAIL",
        "novel_id": project.config.id,
        "title": project.config.title,
        "source_language": project.config.source_language,
        "chapters_scanned": chapters_scanned,
        "ordered_kinship_occurrence_count": occurrence_count,
        "diagnostic_count": diagnostic_count,
        "chapters_with_occurrences": sorted(chapters_with_occurrences),
        "chapters_with_diagnostics": sorted(chapters_with_diagnostics),
        "classification_counts": dict(sorted(classifications.items())),
        "findings": findings,
        "structural_errors": structural_errors,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": unchanged,
    }


def audit_all(repo_root=None, *, source_language="zh"):
    root = Path(repo_root or repository_root()).resolve()
    novel_ids, discovery_errors = _discover(root, source_language)
    reports = []
    errors = list(discovery_errors)
    classifications = Counter()
    for novel_id in novel_ids:
        try:
            report = audit_project(novel_id, repo_root=root)
        except Exception as exc:
            errors.append(f"{novel_id}: {exc}")
            continue
        reports.append(report)
        classifications.update(report["classification_counts"])
        if report["status"] != "PASS":
            errors.extend(f"{novel_id}: {item}" for item in report["structural_errors"])

    findings = [
        {"novel_id": report["novel_id"], **item}
        for report in reports
        for item in report["findings"]
    ]
    occurrences = sum(report["ordered_kinship_occurrence_count"] for report in reports)
    diagnostics = sum(report["diagnostic_count"] for report in reports)
    return {
        "mode": AUDIT_MODE + "_all",
        "status": "PASS" if not errors else "FAIL",
        "source_language_filter": source_language,
        "novel_ids": novel_ids,
        "novels_discovered": len(novel_ids),
        "novels_scanned": len(reports),
        "chapters_scanned": sum(report["chapters_scanned"] for report in reports),
        "ordered_kinship_occurrence_count": occurrences,
        "diagnostic_count": diagnostics,
        "diagnostic_rate": diagnostics / occurrences if occurrences else 0.0,
        "classification_counts": dict(sorted(classifications.items())),
        "reports": reports,
        "findings": findings,
        "errors": errors,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": all(
            report["protected_state_unchanged"] for report in reports
        ),
    }


def _print_human(report, sample_limit=30):
    print("=" * 70)
    print("SEMANTIC KINSHIP ORDER CALIBRATION — READ ONLY")
    print("=" * 70)
    print(f"Status: {report['status']}")
    if "source_language_filter" in report:
        print(f"Source language filter: {report['source_language_filter']}")
        print(f"Novels discovered: {report['novels_discovered']}")
        print(f"Novels scanned: {report['novels_scanned']}")
    else:
        print(f"Novel: {report['novel_id']} — {report['title']}")
    print(f"Chapters scanned: {report['chapters_scanned']}")
    print(f"Mixed-gender ordered kinship occurrences: {report['ordered_kinship_occurrence_count']}")
    print(f"Visibility diagnostics: {report['diagnostic_count']}")
    if "diagnostic_rate" in report:
        print(f"Diagnostic rate: {report['diagnostic_rate']:.1%}")
    counts = report.get("classification_counts", {})
    print("Classifications: " + (
        ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"
    ))
    if report.get("reports"):
        print("Per novel:")
        for item in report["reports"]:
            print(
                f"  {item['novel_id']}: chapters={item['chapters_scanned']} | "
                f"occurrences={item['ordered_kinship_occurrence_count']} | "
                f"diagnostics={item['diagnostic_count']}"
            )
    if report.get("findings"):
        print(f"Diagnostic samples (up to {sample_limit}):")
        for item in report["findings"][:max(sample_limit, 0)]:
            prefix = f"{item.get('novel_id')} / " if item.get("novel_id") else ""
            print(
                f"  {prefix}Chapter {item['chapter']}: term={item['source_term']!r} | "
                f"relation={item['relation']} | classification={item['classification']}"
            )
    print("API calls: 0")
    print("Project writes: 0")
    print(
        "Protected production state unchanged: "
        + ("YES" if report.get("protected_state_unchanged", False) else "NO")
    )
    if report.get("errors"):
        print("Errors:")
        for error in report["errors"]:
            print(f"  - {error}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Calibrate visible preservation of Chinese mixed-gender kinship age/order."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--novel", default=None)
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--sample-limit", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = (
            audit_project(args.novel, repo_root=args.repo_root)
            if args.novel
            else audit_all(args.repo_root, source_language=args.source_language)
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": AUDIT_MODE,
                "status": "FAIL",
                "api_calls": 0,
                "project_writes": 0,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("SEMANTIC KINSHIP ORDER CALIBRATION — READ ONLY")
            print("=" * 70)
            print("Status: FAIL")
            print(f"Error: {exc}")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, sample_limit=args.sample_limit)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
