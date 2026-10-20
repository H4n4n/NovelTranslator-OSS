"""Plan the authorized final editorial layer without creating editorial chapters."""

from __future__ import annotations

import argparse
import sys

from final_editorial import (
    FinalEditorialError,
    build_plan_report,
    summarize_for_cli,
    write_plan_reports,
)
from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True, help="novel identifier under novels/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            report = build_plan_report(project)
            write_plan_reports(project, report)
        print(
            summarize_for_cli(
                report,
                (
                    "status",
                    "ready",
                    "blocker_count",
                    "operation_count",
                    "heading_operation_count",
                    "prose_operation_count",
                    "affected_unit_count",
                    "unaffected_unit_count",
                ),
            )
        )
        return 0 if report["ready"] else 1
    except (FinalEditorialError, OSError, ProductionRunLockedError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
