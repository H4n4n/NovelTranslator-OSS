"""Read-only corpus audit for deterministic semantic-completeness diagnostics.

The current policy only measures explicit Chinese parenthetical term glosses.
It is intentionally non-blocking and makes zero provider calls.  This command is
for calibration: run it over already committed chapters, inspect the findings,
and decide whether the signal is useful before ever considering stronger policy.
"""

import argparse
import json
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project
from semantic_completeness import explicit_source_gloss_completeness_report
from semantic_integrity_audit import protected_state_snapshot


AUDIT_MODE = "semantic_completeness_read_only_audit"


class SemanticCompletenessAuditError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise SemanticCompletenessAuditError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticCompletenessAuditError(f"cannot read {label}: {path}: {exc}") from exc


def audit_semantic_completeness(novel_id, *, repo_root=None, chapter=None):
    if chapter is not None and (
        not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1
    ):
        raise SemanticCompletenessAuditError("chapter must be a positive integer")

    project = load_novel_project(novel_id, repo_root=repo_root)
    before = protected_state_snapshot(project)
    chapters = _read_json(project.chapters_file, "chapter library")
    progress = _read_json(project.progress_file, "progress")
    if not isinstance(chapters, list):
        raise SemanticCompletenessAuditError("chapter library must be a list")
    if not isinstance(progress, dict):
        raise SemanticCompletenessAuditError("progress must be an object")

    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise SemanticCompletenessAuditError("progress.completed must be a list")
    completed = sorted({
        value for value in completed
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    })
    if chapter is not None:
        if chapter not in completed:
            raise SemanticCompletenessAuditError(
                f"Chapter {chapter} is not committed according to progress.completed"
            )
        completed = [chapter]

    findings = []
    structural_errors = []
    chapters_scanned = 0
    explicit_glosses = 0
    diagnostic_count = 0
    chapters_with_diagnostics = set()
    policy_version = ""

    for chapter_number in completed:
        source_entry = chapter_map.get(chapter_number)
        output_path = Path(project.chapter_dir) / f"{chapter_number:04d}.md"
        if not isinstance(source_entry, dict):
            structural_errors.append(
                f"Chapter {chapter_number} is missing from validated source library"
            )
            continue
        if not output_path.is_file():
            structural_errors.append(
                f"Chapter {chapter_number} committed output is missing: {output_path}"
            )
            continue
        source_text = source_entry.get("text", "")
        translation = output_path.read_text(encoding="utf-8")
        report = explicit_source_gloss_completeness_report(source_text, translation)
        policy_version = policy_version or report.get("policy_version", "")
        chapters_scanned += 1
        explicit_glosses += report.get("explicit_source_gloss_count", 0)
        diagnostics = report.get("diagnostics", [])
        diagnostic_count += len(diagnostics)
        if diagnostics:
            chapters_with_diagnostics.add(chapter_number)
        for item in diagnostics:
            findings.append({"chapter": chapter_number, **dict(item)})

    after = protected_state_snapshot(project)
    unchanged = before == after
    if not unchanged:
        structural_errors.append("protected production state changed during audit")

    return {
        "mode": AUDIT_MODE,
        "status": "PASS" if not structural_errors else "FAIL",
        "novel_id": project.config.id,
        "title": project.config.title,
        "project_root": str(project.root),
        "policy_version": policy_version,
        "chapters_scanned": chapters_scanned,
        "explicit_source_gloss_count": explicit_glosses,
        "completeness_diagnostic_count": diagnostic_count,
        "chapters_with_diagnostics": sorted(chapters_with_diagnostics),
        "findings": findings,
        "structural_errors": structural_errors,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": unchanged,
    }


def _print_human(report):
    print("=" * 70)
    print("SEMANTIC COMPLETENESS AUDIT — READ ONLY")
    print("=" * 70)
    print(f"Status: {report['status']}")
    print(f"Novel: {report['novel_id']} — {report['title']}")
    print(f"Policy: {report['policy_version']}")
    print(f"Chapters scanned: {report['chapters_scanned']}")
    print(f"Explicit source glosses: {report['explicit_source_gloss_count']}")
    print(f"Possible omitted gloss diagnostics: {report['completeness_diagnostic_count']}")
    if report["chapters_with_diagnostics"]:
        print(
            "Chapters with diagnostics: "
            + ", ".join(map(str, report["chapters_with_diagnostics"]))
        )
        for item in report["findings"]:
            print(
                f"  Chapter {item['chapter']}: {item['type']} — "
                f"source label {item.get('source_label', '')!r}"
            )
    print("API calls: 0")
    print("Project writes: 0")
    print(
        "Protected production state unchanged: "
        + ("YES" if report["protected_state_unchanged"] else "NO")
    )
    if report["structural_errors"]:
        print("Errors:")
        for error in report["structural_errors"]:
            print(f"  - {error}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Scan committed translations for conservative explicit-source-gloss "
            "completeness diagnostics with zero provider calls and zero writes."
        )
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--chapter", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = audit_semantic_completeness(
            args.novel,
            repo_root=args.repo_root,
            chapter=args.chapter,
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
            print("SEMANTIC COMPLETENESS AUDIT — READ ONLY")
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
