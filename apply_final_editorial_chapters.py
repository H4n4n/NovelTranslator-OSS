"""Apply a valid ready final-editorial plan transactionally and with zero API calls."""

from __future__ import annotations

import argparse
import sys

import json

from final_editorial import FinalEditorialError, apply_ready_plan
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
            report = apply_ready_plan(project)
        output = report["output_corpus_fingerprint"]
        print(json.dumps({
            "status": report["status"],
            "operation_count": report["operation_count"],
            "heading_operation_count": report["heading_operation_count"],
            "prose_operation_count": report["prose_operation_count"],
            "affected_unit_count": report["affected_unit_count"],
            "unaffected_unit_count": report["unaffected_unit_count"],
            "output_file_count": output["file_count"],
            "output_byte_count": output["aggregate_byte_count"],
            "output_sha256": output["aggregate_sha256"],
        }, ensure_ascii=False, indent=2))
        return 0
    except (FinalEditorialError, OSError, ProductionRunLockedError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
