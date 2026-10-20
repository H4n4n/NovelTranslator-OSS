"""Local-only cleanup for proven completed-chapter pending residue."""

import argparse
import json

from novel_project import load_novel_project
from production_completed_pending_cleanup import (
    CompletedPendingCleanupError,
    cleanup_completed_pending,
    inspect_completed_pending_cleanup,
)
from production_run_lock import ProductionRunLockedError, production_run_lock


MODE = "completed_pending_cleanup"


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Remove only exact reviewed pending residue for chapters whose "
            "durable progress commit already succeeded."
        )
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def run_cleanup(novel_id, *, repo_root=None):
    project = load_novel_project(novel_id, repo_root=repo_root)
    with production_run_lock(project.root):
        inspection = inspect_completed_pending_cleanup(project)
        removed = cleanup_completed_pending(inspection)
    return {
        "mode": MODE,
        "status": "CLEAN" if not removed else "CLEANED",
        "novel_id": project.config.id,
        "removed_chapters": list(removed),
        "removed_count": len(removed),
        "api_calls": 0,
    }


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_cleanup(args.novel)
    except (
        CompletedPendingCleanupError,
        ProductionRunLockedError,
        OSError,
        ValueError,
    ) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": MODE,
                        "status": "BLOCKED",
                        "api_calls": 0,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"COMPLETED-PENDING CLEANUP BLOCKED: {exc}")
            print("API calls: 0")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print("COMPLETED-PENDING CLEANUP")
        print("=" * 70)
        print(f"Novel: {report['novel_id']}")
        print(f"Status: {report['status']}")
        if report["removed_chapters"]:
            print(
                "Removed chapters: "
                + ", ".join(str(value) for value in report["removed_chapters"])
            )
        else:
            print("Removed chapters: none")
        print("API calls: 0")
        print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
