"""Zero-cost, read-only semantic/entity integrity audit for one novel project."""

import argparse
import json
from pathlib import Path

from novel_project import load_novel_project
from semantic_integrity_audit import (
    audit_committed_project,
    protected_state_snapshot,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read committed translations and report calibrated source-backed "
            "entity/terminology integrity evidence. Makes zero API calls and writes "
            "no project files."
        )
    )
    parser.add_argument("--novel", required=True, help="novel identifier under novels/")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "optional repository/worktree root containing novels/. Use this to audit "
            "a project stored in a different worktree without copying or modifying it"
        ),
    )
    parser.add_argument("--start", type=int, default=None, help="first committed chapter to audit")
    parser.add_argument("--end", type=int, default=None, help="last committed chapter to audit")
    parser.add_argument(
        "--experimental-substitutions",
        action="store_true",
        help=(
            "show the low-confidence cross-entity substitution heuristic. Disabled by "
            "default and never treated as a blocker"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the complete machine-readable report to stdout",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum diagnostics/advisories/drift entries shown in human-readable mode",
    )
    return parser


def _print_issue_sample(items, limit, heading):
    if not items:
        return
    print(f"\n{heading}:")
    for item in items[:limit]:
        canonical = item.get("canonical") or item.get("rendering") or ""
        print(
            f"  Ch {item.get('chapter', '?')}: {item.get('type', 'unknown')} | "
            f"{item.get('category', '')} | source={item.get('source_key', '')!r} | "
            f"target={canonical!r}"
        )
    if len(items) > limit:
        print(f"  ... {len(items) - limit} more")


def _print_human(report, limit):
    print("=" * 78)
    print("SEMANTIC / ENTITY INTEGRITY AUDIT — READ ONLY")
    print("=" * 78)
    print(f"Novel: {report['novel_id']} — {report['title']}")
    requested = report["requested_range"]
    if requested["start"] is not None or requested["end"] is not None:
        print(f"Requested range: {requested['start'] or 'first'}-{requested['end'] or 'last'}")
    print(f"Committed chapters selected: {report['selected_chapter_count']}")
    print(f"Chapters successfully audited: {report['audited_chapter_count']}")
    print(
        "Source-backed locked identity observations: "
        f"{report['source_backed_identity_observations']}"
    )
    print(
        "Diagnostics: "
        f"{report['diagnostic_count']} across "
        f"{report['chapters_with_diagnostic_count']} chapters"
    )
    print(
        "Actionable advisories: "
        f"{report['advisory_count']} across "
        f"{report['chapters_with_advisory_count']} chapters"
    )
    print(
        "Experimental substitution signals: "
        f"{report['experimental_signal_count']} across "
        f"{report['chapters_with_experimental_signal_count']} chapters"
    )
    if report["suppressed_experimental_signal_count"]:
        print(
            "Experimental substitution signals suppressed by default: "
            f"{report['suppressed_experimental_signal_count']}"
        )
    print(
        "Existing strict blockers observed: "
        f"{report['strict_blocker_count']} across "
        f"{report['chapters_with_strict_blocker_count']} chapters"
    )
    print(f"Cross-chapter known-rendering drift terms: {report['drift_term_count']}")
    print(f"Structural audit errors: {len(report['errors'])}")
    print(f"API calls: {report['api_calls']}")
    print(f"Writes performed: {report['writes_performed']}")
    print(
        "Protected production state unchanged: "
        + ("YES" if report.get("protected_state_unchanged") else "NO")
    )

    if report["diagnostic_types"]:
        print("\nDiagnostics by type:")
        for key, value in report["diagnostic_types"].items():
            print(f"  {key}: {value}")
    if report["diagnostic_categories"]:
        print("\nDiagnostics by category:")
        for key, value in report["diagnostic_categories"].items():
            print(f"  {key}: {value}")
    if report["advisory_types"]:
        print("\nActionable advisories by type:")
        for key, value in report["advisory_types"].items():
            print(f"  {key}: {value}")
    if report["advisory_categories"]:
        print("\nActionable advisories by category:")
        for key, value in report["advisory_categories"].items():
            print(f"  {key}: {value}")
    if report["experimental_signal_types"]:
        print("\nExperimental signals by type:")
        for key, value in report["experimental_signal_types"].items():
            print(f"  {key}: {value}")

    if report["errors"]:
        print("\nStructural errors:")
        for item in report["errors"][:limit]:
            print(f"  Chapter {item.get('chapter', '?')}: {item.get('type', 'unknown')}")
        if len(report["errors"]) > limit:
            print(f"  ... {len(report['errors']) - limit} more")

    _print_issue_sample(report["diagnostics"], limit, "Sample diagnostics")
    _print_issue_sample(report["advisories"], limit, "Sample actionable advisories")
    _print_issue_sample(
        report["experimental_signals"],
        limit,
        "Sample experimental substitution signals",
    )

    if report["drift"]:
        print("\nSample cross-chapter known-rendering drift:")
        for item in report["drift"][:limit]:
            variants = ", ".join(
                f"{variant['rendering']} ({variant['count']})"
                for variant in item["variants"]
            )
            print(
                f"  {item['source_key']!r} [{item['category']}]: {variants}"
            )
        if len(report["drift"]) > limit:
            print(f"  ... {len(report['drift']) - limit} more")
    print("=" * 78)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        raise ValueError("--limit must be a positive integer")
    if args.repo_root is None:
        # Preserve the established loader call shape for ordinary in-worktree
        # audits and existing tests/integrations that replace the loader with a
        # single-argument callable. External worktree audits opt in explicitly.
        project = load_novel_project(args.novel)
    else:
        project = load_novel_project(args.novel, repo_root=args.repo_root)
    before = protected_state_snapshot(project)
    report = audit_committed_project(
        project,
        start=args.start,
        end=args.end,
        include_experimental=args.experimental_substitutions,
    )
    after = protected_state_snapshot(project)
    report["protected_state_unchanged"] = before == after
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, args.limit)
    return 0 if not report["errors"] and report["protected_state_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
