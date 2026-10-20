"""Fail-closed executor for one committed-QA provenance backfill.

This command never calls a provider.  It rebuilds the read-only plan, requires
an exact state fingerprint and approval token, and without explicit
``--authorize-backfill`` performs zero writes.

With authorization it changes exactly one existing numeric committed-QA JSON
file by adding the immutable model/request/pricing provenance bound by the
approved plan.  Translation text, progress, pending state, source data, and all
other project files are left untouched.
"""

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from novel_project import load_novel_project
from noveltranslator_core import atomic_write_json
from production_run_lock import production_run_lock
from qa_provenance_backfill_plan import (
    QAProvenanceBackfillPlanError,
    build_qa_provenance_backfill_plan,
)


EXECUTION_MODE = "committed_qa_provenance_backfill_executor"


class QAProvenanceBackfillExecutionError(RuntimeError):
    pass


def _read_json(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise QAProvenanceBackfillExecutionError(
            f"cannot read committed QA {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise QAProvenanceBackfillExecutionError(
            f"committed QA must be a JSON object: {path}"
        )
    return value


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bound_non_qa_state(project, chapter):
    output = Path(project.chapter_dir) / f"{chapter:04d}.md"
    pending_md = Path(project.pending_dir) / f"{chapter:04d}.md"
    pending_json = Path(project.pending_dir) / f"{chapter:04d}.json"
    return {
        "output_sha256": _sha256_file(output),
        "progress_sha256": _sha256_file(project.progress_file),
        "pending_md_exists": pending_md.exists(),
        "pending_json_exists": pending_json.exists(),
    }


def _execute_qa_provenance_backfill_under_lock(
    novel_id,
    chapter,
    *,
    repo_root=None,
    model_selection=None,
    state_fingerprint,
    approval_token,
    authorize_backfill=False,
    write_json=atomic_write_json,
):
    """Rebuild and optionally execute while the caller owns any required lock."""
    try:
        plan = build_qa_provenance_backfill_plan(
            novel_id,
            chapter,
            repo_root=repo_root,
            model_selection=model_selection,
        )
    except QAProvenanceBackfillPlanError as exc:
        raise QAProvenanceBackfillExecutionError(str(exc)) from exc

    if plan["state_fingerprint"] != state_fingerprint:
        raise QAProvenanceBackfillExecutionError(
            "state fingerprint does not match current production state"
        )
    if plan["approval_token"] != approval_token:
        raise QAProvenanceBackfillExecutionError(
            "approval token does not match the rebuilt plan"
        )

    base_report = {
        "mode": EXECUTION_MODE,
        "novel_id": plan["novel_id"],
        "title": plan["title"],
        "chapter": plan["chapter"],
        "qa_path": plan["qa_path"],
        "model": plan["model"],
        "model_version": plan["model_version"],
        "pricing_version": plan["pricing_version"],
        "state_fingerprint": plan["state_fingerprint"],
        "approval_token": plan["approval_token"],
        "api_calls": 0,
        "project_writes": 0,
        "changed_paths": [],
    }

    if plan["status"] == "ALREADY_PINNED":
        return {
            **base_report,
            "status": "ALREADY_PINNED",
            "backfill_authorized": bool(authorize_backfill),
            "qa_sha256_before": plan["qa_sha256"],
            "qa_sha256_after": plan["qa_sha256"],
        }
    if plan["status"] != "READY":
        raise QAProvenanceBackfillExecutionError(
            f"backfill plan is not executable: {plan['status']}"
        )
    if not authorize_backfill:
        return {
            **base_report,
            "status": "AUTHORIZATION_REQUIRED",
            "backfill_authorized": False,
            "qa_sha256_before": plan["qa_sha256"],
            "qa_sha256_after": plan["qa_sha256"],
        }

    project = load_novel_project(novel_id, repo_root=repo_root)
    qa_path = Path(plan["qa_path"])
    if qa_path.resolve() != (Path(project.qa_dir) / f"{chapter:04d}.json").resolve():
        raise QAProvenanceBackfillExecutionError(
            "planned QA path does not match the selected project/chapter"
        )

    before_non_qa = _bound_non_qa_state(project, chapter)
    qa = _read_json(qa_path)
    if _sha256_file(qa_path) != plan["qa_sha256"]:
        raise QAProvenanceBackfillExecutionError(
            "committed QA changed after plan validation and before write"
        )

    proposed = dict(plan["proposed_provenance"])
    original_items = dict(qa)
    payload = dict(qa)
    payload.update(proposed)
    if any(payload.get(key) != value for key, value in original_items.items()):
        raise QAProvenanceBackfillExecutionError(
            "provenance backfill would alter an existing committed-QA field"
        )

    write_json(qa_path, payload)
    after_non_qa = _bound_non_qa_state(project, chapter)
    saved = _read_json(qa_path)

    if before_non_qa != after_non_qa:
        raise QAProvenanceBackfillExecutionError(
            "non-QA bound project state changed during provenance backfill"
        )
    for key, value in original_items.items():
        if saved.get(key) != value:
            raise QAProvenanceBackfillExecutionError(
                f"existing committed-QA field changed unexpectedly: {key}"
            )
    for key, value in proposed.items():
        if saved.get(key) != value:
            raise QAProvenanceBackfillExecutionError(
                f"provenance field was not persisted exactly: {key}"
            )
    expected_keys = set(original_items) | set(proposed)
    if set(saved) != expected_keys:
        raise QAProvenanceBackfillExecutionError(
            "committed QA contains unexpected keys after provenance backfill"
        )

    return {
        **base_report,
        "status": "BACKFILLED",
        "backfill_authorized": True,
        "project_writes": 1,
        "changed_paths": [str(qa_path)],
        "qa_sha256_before": plan["qa_sha256"],
        "qa_sha256_after": _sha256_file(qa_path),
    }


def execute_qa_provenance_backfill(
    novel_id,
    chapter,
    *,
    repo_root=None,
    model_selection=None,
    state_fingerprint,
    approval_token,
    authorize_backfill=False,
    write_json=atomic_write_json,
):
    """Validate and optionally execute exactly one provenance-only QA write.

    Explicit write authorization acquires the novel-local writer lock before
    rebuilding the plan so the validated state cannot race another supported
    writer. Read-only verification remains lock-free.
    """
    lock = (
        production_run_lock(
            load_novel_project(
                novel_id,
                repo_root=repo_root,
            ).root
        )
        if authorize_backfill
        else nullcontext()
    )
    with lock:
        return _execute_qa_provenance_backfill_under_lock(
            novel_id,
            chapter,
            repo_root=repo_root,
            model_selection=model_selection,
            state_fingerprint=state_fingerprint,
            approval_token=approval_token,
            authorize_backfill=authorize_backfill,
            write_json=write_json,
        )


def _print_human(report):
    print("=" * 70)
    print("COMMITTED QA MODEL PROVENANCE BACKFILL EXECUTOR")
    print("=" * 70)
    print(f"Status: {report['status']}")
    print(f"Novel: {report['novel_id']} — {report['title']}")
    print(f"Chapter: {report['chapter']}")
    print(f"QA file: {report['qa_path']}")
    print(f"Pinned model: {report['model']}")
    print(f"Model version: {report['model_version']}")
    print(f"Pricing version: {report['pricing_version']}")
    print(
        "Backfill authorized: "
        + ("YES" if report.get("backfill_authorized") else "NO")
    )
    print(f"API calls: {report['api_calls']}")
    print(f"Project writes: {report['project_writes']}")
    print(f"QA SHA256 before: {report['qa_sha256_before']}")
    print(f"QA SHA256 after:  {report['qa_sha256_after']}")
    if report["changed_paths"]:
        print("Changed paths:")
        for path in report["changed_paths"]:
            print(f"  {path}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Execute one state-bound committed-QA model-provenance backfill. "
            "Makes zero provider calls and requires explicit write authorization."
        )
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument("--state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-backfill", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = execute_qa_provenance_backfill(
            args.novel,
            args.chapter,
            repo_root=args.repo_root,
            model_selection=args.model,
            state_fingerprint=args.state_fingerprint,
            approval_token=args.approval_token,
            authorize_backfill=args.authorize_backfill,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": EXECUTION_MODE,
                "status": "REFUSED",
                "api_calls": 0,
                "project_writes": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("COMMITTED QA MODEL PROVENANCE BACKFILL EXECUTOR")
            print("=" * 70)
            print("Status: REFUSED")
            print(f"Error: {type(exc).__name__}: {exc}")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
