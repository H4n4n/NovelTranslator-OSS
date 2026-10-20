"""Create one explicit zero-API glossary-candidate conflict approval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from chapter_library import index_chapter_library
from glossary_candidate_resolution import (
    GlossaryCandidateResolutionError,
    apply_candidate_conflict_resolutions,
    candidate_resolution_path,
    create_candidate_resolution,
    pending_content_identity,
    state_extraction_identity,
)
from glossary_integrity import (
    GlossaryCandidateIdentityError,
    canonicalize_glossary_review_candidates,
    source_backed_candidate_proposals,
)
from novel_project import load_novel_project
from production_run_lock import ProductionRunLockedError, production_run_lock
from translation_profile import (
    filter_profile_candidates,
    load_translation_profile,
    project_low_value_terms,
)


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _positive_chapter(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("chapter must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("chapter must be positive")
    return result


def reconstruct_candidate_conflicts(project, chapter: int):
    pending_md = project.pending_dir / f"{chapter:04d}.md"
    pending_json = project.pending_dir / f"{chapter:04d}.json"
    if not pending_md.is_file() or not pending_json.is_file():
        raise GlossaryCandidateResolutionError(
            "both pending translation and metadata are required",
            code="resolution_pending_missing",
        )
    meta = _load_json(pending_json)
    if meta.get("chapter") != chapter:
        raise GlossaryCandidateResolutionError(
            "pending metadata chapter does not match the requested chapter",
            code="resolution_chapter_mismatch",
        )
    state = meta.get("state")
    if not isinstance(state, dict):
        raise GlossaryCandidateResolutionError(
            "pending metadata has no persisted state extraction",
            code="resolution_state_missing",
        )
    chapters = _load_json(project.chapters_file)
    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    if chapter not in chapter_map:
        raise GlossaryCandidateResolutionError(
            "requested chapter is absent from the authoritative chapter library",
            code="resolution_source_chapter_missing",
        )
    source_text = chapter_map[chapter]["text"]
    translation = pending_md.read_text(encoding="utf-8")
    glossary = _load_json(project.glossary_file)
    profile = load_translation_profile(
        project.config.profile,
        project.repository_root,
    )
    source_backed, source_rejected = source_backed_candidate_proposals(
        state.get("new_terms", []),
        source_text,
        chapter,
    )
    eligible, profile_rejected = filter_profile_candidates(
        profile,
        source_backed,
        existing_terms=glossary.get("entries", {}),
        low_value_terms=project_low_value_terms(project.config.id),
    )
    try:
        canonicalize_glossary_review_candidates(eligible)
    except GlossaryCandidateIdentityError as exc:
        conflicts = list(exc.diagnostics.get("conflicts", []))
        diagnostics = exc.diagnostics
    else:
        raise GlossaryCandidateResolutionError(
            "the current pending state has no candidate conflict to resolve",
            code="resolution_without_matching_conflict",
        )
    return {
        "pending_md": pending_md,
        "pending_json": pending_json,
        "state": state,
        "translation": translation,
        "eligible": eligible,
        "conflicts": conflicts,
        "diagnostics": diagnostics,
        "source_rejected": source_rejected,
        "profile_rejected": profile_rejected,
        "pending_identity": pending_content_identity(translation),
        "state_identity": state_extraction_identity(state),
    }


def resolve_from_args(args) -> dict:
    project = load_novel_project(args.novel, args.repo_root)
    reconstructed = reconstruct_candidate_conflicts(project, args.chapter)
    conflicts = [
        item for item in reconstructed["conflicts"]
        if item.get("source_term") == args.source
    ]
    if len(conflicts) != 1:
        raise GlossaryCandidateResolutionError(
            "requested source must identify exactly one current conflict",
            code=(
                "resolution_source_not_present"
                if not conflicts
                else "resolution_source_ambiguous"
            ),
            diagnostics={"source": args.source, "matches": len(conflicts)},
        )
    conflict = conflicts[0]
    available = {
        item.get("semantic_identity_fingerprint")
        for item in conflict.get("proposals", [])
    }
    if args.choose not in available:
        raise GlossaryCandidateResolutionError(
            "--choose must be one complete semantic fingerprint shown below",
            code="resolution_selected_fingerprint_absent",
            diagnostics={
                "available_proposals": [
                    {
                        "semantic_identity_fingerprint": item.get(
                            "semantic_identity_fingerprint", ""
                        ),
                        "english": item.get("english", ""),
                        "category": item.get("category", ""),
                        "status": item.get("status", ""),
                    }
                    for item in conflict.get("proposals", [])
                ]
            },
        )
    path = candidate_resolution_path(project.data_dir, args.chapter)
    created = create_candidate_resolution(
        path,
        novel_id=project.config.id,
        chapter=args.chapter,
        pending_identity=reconstructed["pending_identity"],
        state_identity=reconstructed["state_identity"],
        conflict=conflict,
        selected_semantic_fingerprint=args.choose,
        rationale=args.rationale,
    )
    verified = apply_candidate_conflict_resolutions(
        reconstructed["eligible"],
        created["record"],
        novel_id=project.config.id,
        chapter=args.chapter,
        expected_pending_identity=reconstructed["pending_identity"],
        expected_state_identity=reconstructed["state_identity"],
    )
    return {
        "project": project,
        "conflict": conflict,
        "created": created,
        "verified": verified,
        "reconstructed": reconstructed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Approve one exact persisted glossary-candidate semantic proposal. "
            "This command performs no API calls and does not edit pending state."
        )
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapter", required=True, type=_positive_chapter)
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--choose",
        required=True,
        help="Complete semantic_identity_fingerprint of the approved proposal",
    )
    parser.add_argument(
        "--rationale",
        required=True,
        help="Bounded human/source-review rationale retained in the audit record",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        project = load_novel_project(args.novel, args.repo_root)
        with production_run_lock(project.root):
            result = resolve_from_args(args)
    except (
        GlossaryCandidateResolutionError,
        OSError,
        ValueError,
        ProductionRunLockedError,
    ) as exc:
        print("GLOSSARY CANDIDATE CONFLICT RESOLUTION FAILED", file=sys.stderr)
        print(f"Code: {getattr(exc, 'code', 'resolution_error')}", file=sys.stderr)
        print(f"Reason: {exc}", file=sys.stderr)
        diagnostics = getattr(exc, "diagnostics", None)
        if diagnostics:
            print(
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
        return 2
    conflict = result["conflict"]
    record = result["created"]["record"]
    approved = next(
        item for item in record["resolutions"]
        if item["conflict_fingerprint"] == conflict["conflict_fingerprint"]
    )
    print("GLOSSARY CANDIDATE CONFLICT RESOLUTION VERIFIED")
    print(f"Novel: {record['novel_id']}")
    print(f"Chapter: {record['chapter']}")
    print(f"Source: {approved['source_key']}")
    print(f"Conflict fingerprint: {approved['conflict_fingerprint']}")
    print("Conflicting proposals:")
    for proposal in sorted(
        conflict.get("proposals", []),
        key=lambda item: item.get("semantic_identity_fingerprint", ""),
    ):
        print(
            "  "
            f"{proposal.get('semantic_identity_fingerprint', '')}: "
            f"english={proposal.get('english', '')!r}; "
            f"category={proposal.get('category', '')!r}; "
            f"status={proposal.get('status', '')!r}"
        )
    print("Selected semantic fingerprint:")
    print(approved["selected_semantic_fingerprint"])
    print("Approved resolution:")
    for field in ("english", "category", "status"):
        print(f"  {field}: {approved['approved_resolution'][field]}")
    print(f"Resolution record: {result['created']['path']}")
    print(f"Record SHA-256: {record['record_sha256']}")
    print(f"Write status: {result['created']['status']}")
    print(
        "Canonical candidates after resolution: "
        f"{len(result['verified']['candidates'])}"
    )
    print("API calls: 0")
    print("Pending translation/state changes: 0")
    print("Progress changes: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
