"""Aggregate read-only semantic-completeness calibration across novel projects.

This helper discovers configured novels under ``<repo>/novels``, selects Chinese
source projects by default, runs the existing per-novel deterministic completeness
audit, and summarizes signal prevalence. It never calls a provider and never
writes project data.
"""

import argparse
import json
from pathlib import Path

from audit_semantic_completeness import audit_semantic_completeness
from novel_project import load_novel_config, repository_root
from source_language_filter import source_language_matches


AUDIT_MODE = "semantic_completeness_aggregate_read_only_audit"


class SemanticCompletenessAggregateAuditError(RuntimeError):
    pass


def discover_novel_ids(repo_root=None, *, source_language="zh"):
    root = Path(repo_root or repository_root()).resolve()
    novels_root = root / "novels"
    if not novels_root.is_dir():
        raise SemanticCompletenessAggregateAuditError(
            f"novels directory does not exist: {novels_root}"
        )
    selected = []
    discovery_errors = []
    for directory in sorted(
        (item for item in novels_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
    ):
        config_file = directory / "novel.json"
        if not config_file.is_file():
            continue
        try:
            config = load_novel_config(config_file)
        except Exception as exc:
            discovery_errors.append(f"{config_file}: {exc}")
            continue
        if not source_language_matches(config.source_language, source_language):
            continue
        selected.append(config.id)
    return selected, discovery_errors


def audit_all_semantic_completeness(repo_root=None, *, source_language="zh"):
    root = Path(repo_root or repository_root()).resolve()
    novel_ids, discovery_errors = discover_novel_ids(
        root, source_language=source_language
    )
    reports = []
    audit_errors = []
    totals = {
        "novels_scanned": 0,
        "chapters_scanned": 0,
        "explicit_source_gloss_count": 0,
        "completeness_diagnostic_count": 0,
        "chapters_with_diagnostics": 0,
    }

    for novel_id in novel_ids:
        try:
            report = audit_semantic_completeness(novel_id, repo_root=root)
        except Exception as exc:
            audit_errors.append(f"{novel_id}: {exc}")
            continue
        reports.append(report)
        totals["novels_scanned"] += 1
        totals["chapters_scanned"] += report.get("chapters_scanned", 0)
        totals["explicit_source_gloss_count"] += report.get(
            "explicit_source_gloss_count", 0
        )
        totals["completeness_diagnostic_count"] += report.get(
            "completeness_diagnostic_count", 0
        )
        totals["chapters_with_diagnostics"] += len(
            report.get("chapters_with_diagnostics", []) or []
        )
        if report.get("status") != "PASS":
            audit_errors.extend(
                f"{novel_id}: {error}"
                for error in report.get("structural_errors", []) or [
                    "per-novel audit did not pass"
                ]
            )

    glosses = totals["explicit_source_gloss_count"]
    diagnostics = totals["completeness_diagnostic_count"]
    diagnostic_rate = (diagnostics / glosses) if glosses else 0.0

    errors = [*discovery_errors, *audit_errors]
    return {
        "mode": AUDIT_MODE,
        "status": "PASS" if not errors else "FAIL",
        "repo_root": str(root),
        "source_language": source_language,
        "novel_ids": novel_ids,
        "novels_discovered": len(novel_ids),
        **totals,
        "diagnostic_rate_per_explicit_gloss": diagnostic_rate,
        "reports": reports,
        "errors": errors,
        "api_calls": 0,
        "project_writes": 0,
    }


def _print_human(report):
    print("=" * 70)
    print("SEMANTIC COMPLETENESS CALIBRATION — ALL MATCHING NOVELS")
    print("=" * 70)
    print(f"Status: {report['status']}")
    print(f"Source language filter: {report['source_language']}")
    print(f"Novels discovered: {report['novels_discovered']}")
    print(f"Novels scanned: {report['novels_scanned']}")
    print(f"Chapters scanned: {report['chapters_scanned']}")
    print(f"Explicit source glosses: {report['explicit_source_gloss_count']}")
    print(
        "Possible omitted gloss diagnostics: "
        f"{report['completeness_diagnostic_count']}"
    )
    print(
        "Diagnostic rate per explicit gloss: "
        f"{report['diagnostic_rate_per_explicit_gloss']:.1%}"
    )
    print(
        "Chapters with diagnostics: "
        f"{report['chapters_with_diagnostics']}"
    )
    if report["reports"]:
        print("Per novel:")
        for item in report["reports"]:
            print(
                "  "
                f"{item['novel_id']}: chapters={item['chapters_scanned']} | "
                f"glosses={item['explicit_source_gloss_count']} | "
                f"diagnostics={item['completeness_diagnostic_count']} | "
                f"diagnostic_chapters={len(item['chapters_with_diagnostics'])}"
            )
    print("API calls: 0")
    print("Project writes: 0")
    if report["errors"]:
        print("Errors:")
        for error in report["errors"]:
            print(f"  - {error}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate the deterministic semantic-completeness audit across all "
            "configured novels matching one source-language family or exact tag."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = audit_all_semantic_completeness(
            args.repo_root,
            source_language=args.source_language,
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
            print("SEMANTIC COMPLETENESS CALIBRATION — ALL MATCHING NOVELS")
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
        _print_human(report)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
