"""Read-only state-bound plan for committed-QA model provenance backfill.

This helper never calls a provider and never writes project data.  It is for
rare cases where a chapter was already translated and independently verified
under a known model before committed-QA provenance persistence was introduced.

The plan binds the exact QA file, committed chapter, source text, progress state,
model/version/pricing record, and pending absence into a deterministic state
fingerprint and approval token.  A future executor must rebuild this exact plan
before changing any production metadata.
"""

import argparse
import hashlib
import json
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project
from production_path_preflight import run_production_path_preflight
from translation_model_config import (
    pending_translation_model_record,
    resolve_translation_model,
)


PLAN_MODE = "committed_qa_provenance_backfill_plan"
PROVENANCE_FIELDS = tuple(pending_translation_model_record("pro").keys())


class QAProvenanceBackfillPlanError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise QAProvenanceBackfillPlanError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise QAProvenanceBackfillPlanError(f"cannot read {label}: {path}: {exc}") from exc
    return value


def _sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _existing_provenance(qa):
    return {
        key: qa[key]
        for key in PROVENANCE_FIELDS
        if isinstance(qa, dict) and key in qa
    }


def build_qa_provenance_backfill_plan(
    novel_id,
    chapter,
    *,
    repo_root=None,
    model_selection=None,
):
    """Return a deterministic, zero-write provenance-backfill plan."""
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise QAProvenanceBackfillPlanError("chapter must be a positive integer")

    config = resolve_translation_model(model_selection)
    project = load_novel_project(novel_id, repo_root=repo_root)
    preflight = run_production_path_preflight(
        novel_id,
        repo_root=repo_root,
        model_selection=config.model,
    )
    if preflight.get("status") != "PASS":
        raise QAProvenanceBackfillPlanError(
            "production-path preflight is not clean: "
            + "; ".join(preflight.get("errors", []) or ["unknown preflight failure"])
        )

    progress = _read_json(project.progress_file, "progress")
    chapters = _read_json(project.chapters_file, "chapter library")
    qa_path = Path(project.qa_dir) / f"{chapter:04d}.json"
    output_path = Path(project.chapter_dir) / f"{chapter:04d}.md"
    pending_md = Path(project.pending_dir) / f"{chapter:04d}.md"
    pending_json = Path(project.pending_dir) / f"{chapter:04d}.json"
    qa = _read_json(qa_path, "committed QA")

    if not isinstance(progress, dict):
        raise QAProvenanceBackfillPlanError("progress must be an object")
    if not isinstance(chapters, list):
        raise QAProvenanceBackfillPlanError("chapter library must be a list")
    if not isinstance(qa, dict):
        raise QAProvenanceBackfillPlanError("committed QA must be a JSON object")
    if not output_path.is_file():
        raise QAProvenanceBackfillPlanError(
            f"committed Chapter {chapter} output does not exist: {output_path}"
        )
    if pending_md.exists() or pending_json.exists():
        raise QAProvenanceBackfillPlanError(
            f"Chapter {chapter} still has pending state; refusing provenance backfill"
        )

    completed = progress.get("completed", [])
    if not isinstance(completed, list) or chapter not in completed:
        raise QAProvenanceBackfillPlanError(
            f"Chapter {chapter} is not present in progress.completed"
        )
    if qa.get("chapter") != chapter:
        raise QAProvenanceBackfillPlanError(
            f"committed QA chapter field does not match requested Chapter {chapter}"
        )
    if qa.get("commit_allowed") is not True:
        raise QAProvenanceBackfillPlanError(
            f"Chapter {chapter} QA is not recorded as commit_allowed"
        )

    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    source_entry = chapter_map.get(chapter)
    source_text = source_entry.get("text", "") if isinstance(source_entry, dict) else ""
    if not isinstance(source_text, str) or not source_text.strip():
        raise QAProvenanceBackfillPlanError(
            f"Chapter {chapter} has no validated source text"
        )

    proposed = pending_translation_model_record(config.model)
    existing = _existing_provenance(qa)
    conflicts = {
        key: {"existing": value, "proposed": proposed[key]}
        for key, value in existing.items()
        if value != proposed[key]
    }
    if conflicts:
        details = ", ".join(
            f"{key}={item['existing']!r} (expected {item['proposed']!r})"
            for key, item in sorted(conflicts.items())
        )
        raise QAProvenanceBackfillPlanError(
            "committed QA contains conflicting model provenance: " + details
        )

    already_complete = all(qa.get(key) == value for key, value in proposed.items())
    state_binding = {
        "novel_id": project.config.id,
        "chapter": chapter,
        "model": config.model,
        "model_version": config.version,
        "pricing_version": config.pricing_version,
        "qa_sha256": _sha256_file(qa_path),
        "output_sha256": _sha256_file(output_path),
        "progress_sha256": _sha256_file(project.progress_file),
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "completed": completed,
        "last_completed": progress.get("last_completed"),
        "next_chapter": progress.get("next_chapter"),
        "pending_md_exists": pending_md.exists(),
        "pending_json_exists": pending_json.exists(),
        "existing_provenance": existing,
        "proposed_provenance": proposed,
    }
    state_fingerprint = _canonical_hash(state_binding)
    approval_payload = {
        "mode": PLAN_MODE,
        "state_fingerprint": state_fingerprint,
        "novel_id": project.config.id,
        "chapter": chapter,
        "model": config.model,
    }
    approval_token = _canonical_hash(approval_payload)

    return {
        "mode": PLAN_MODE,
        "status": "ALREADY_PINNED" if already_complete else "READY",
        "novel_id": project.config.id,
        "title": project.config.title,
        "project_root": str(project.root),
        "chapter": chapter,
        "qa_path": str(qa_path),
        "qa_sha256": state_binding["qa_sha256"],
        "output_sha256": state_binding["output_sha256"],
        "source_text_sha256": state_binding["source_text_sha256"],
        "model": config.model,
        "model_version": config.version,
        "thinking": config.thinking,
        "max_output_tokens": config.max_tokens,
        "pricing_version": config.pricing_version,
        "existing_provenance": existing,
        "proposed_provenance": proposed,
        "state_fingerprint": state_fingerprint,
        "approval_token": approval_token,
        "backfill_authorized": False,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": preflight.get("protected_state_unchanged", False),
    }


def _print_human(plan):
    print("=" * 70)
    print("COMMITTED QA MODEL PROVENANCE BACKFILL — READ-ONLY PLAN")
    print("=" * 70)
    print(f"Status: {plan['status']}")
    print(f"Novel: {plan['novel_id']} — {plan['title']}")
    print(f"Chapter: {plan['chapter']}")
    print(f"QA file: {plan['qa_path']}")
    print(f"Pinned model to record: {plan['model']}")
    print(f"Model version: {plan['model_version']}")
    print(f"Thinking: {plan['thinking']}")
    print(f"Pricing version: {plan['pricing_version']}")
    print(f"QA SHA256: {plan['qa_sha256']}")
    print(f"Committed output SHA256: {plan['output_sha256']}")
    print(f"Source text SHA256: {plan['source_text_sha256']}")
    print(f"Existing provenance fields: {len(plan['existing_provenance'])}")
    print(f"State fingerprint: {plan['state_fingerprint']}")
    print(f"Approval token: {plan['approval_token']}")
    print("Backfill authorized: NO")
    print("API calls: 0")
    print("Project writes: 0")
    print(
        "Protected production state unchanged: "
        + ("YES" if plan["protected_state_unchanged"] else "NO")
    )
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Bind one already-committed QA file and known translation model into "
            "a zero-write provenance-backfill approval token."
        )
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        plan = build_qa_provenance_backfill_plan(
            args.novel,
            args.chapter,
            repo_root=args.repo_root,
            model_selection=args.model,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": PLAN_MODE,
                "status": "NOT_READY",
                "backfill_authorized": False,
                "api_calls": 0,
                "project_writes": 0,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("COMMITTED QA MODEL PROVENANCE BACKFILL — READ-ONLY PLAN")
            print("=" * 70)
            print("Status: NOT_READY")
            print(f"Error: {exc}")
            print("Backfill authorized: NO")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_human(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
