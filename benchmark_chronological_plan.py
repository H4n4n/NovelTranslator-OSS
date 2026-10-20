"""Freeze a no-leak chronological state-growth benchmark corpus.

This module is benchmark-only and import-safe. Importing it reads no credentials,
performs no network activity, makes no provider calls, and writes no files.

The experiment deliberately starts each novel from an empty dynamic glossary and
empty continuity. Novel-specific mature translation addenda and mature glossaries
are frozen only as reference evidence and are explicitly excluded from provider-
visible context. The provider-visible system prompt is the immutable profile-only
translation instruction set.

The default corpus is Chapters 1-25 of:
- great_king_zhuxia (Chinese -> English)
- rogue_first_prince (Korean -> English)

Pre-merge validation may execute this code from a clean feature worktree while
reading authoritative novel data from the live production checkout. Those are
separate provenance identities: both the executing code HEAD and authoritative
data HEAD are independently verified and frozen into the experiment identity.
After merge, canonical execution normally has both roots at the same main HEAD.

All frozen artifacts are written to a new external output root, never inside the
code repository, data repository, or a live novel tree. Production-owned state is
hashed before and after freezing and must remain byte-identical.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    validate_chapter_selection,
)
from chapter_library import source_language_prompt_label, source_prompt_heading


PLAN_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
BENCHMARK_ID = "chronological-state-growth-v1"
PLAN_FILENAME = "chronological_state_plan.json"
DEFAULT_CHAPTER_COUNT = 25
DEFAULT_NOVELS = (
    "great_king_zhuxia",
    "rogue_first_prince",
)
CODE_REPO_ROOT = Path(__file__).resolve().parent


class ChronologicalPlanError(BenchmarkError):
    """The chronological corpus could not be frozen safely."""


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _repo_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChronologicalPlanError(f"could not resolve repository HEAD: {exc}") from exc
    head = completed.stdout.strip().lower()
    if len(head) != 40:
        raise ChronologicalPlanError("repository HEAD is not a full 40-character SHA")
    return head


def _verify_repo_head(repo_root: Path, expected_head: str, label: str) -> str:
    actual = _repo_head(repo_root)
    expected = str(expected_head or "").strip().lower()
    if actual != expected:
        raise BenchmarkSafetyError(
            f"{label} HEAD mismatch: expected {expected}, got {actual}"
        )
    return actual


def _paragraph_count(text: str) -> int:
    return len([part for part in (text or "").split("\n\n") if part.strip()])


def _write_new_bytes(path: Path, payload: bytes, label: str) -> str:
    """Exclusively write one frozen artifact and verify its exact bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise BenchmarkSafetyError(
            f"{label} already exists and was not overwritten: {path}"
        ) from exc
    except OSError as exc:
        raise BenchmarkSafetyError(f"could not write {label}: {path}: {exc}") from exc
    try:
        recorded = path.read_bytes()
    except OSError as exc:
        raise BenchmarkSafetyError(f"could not verify {label}: {path}: {exc}") from exc
    if recorded != payload:
        raise BenchmarkSafetyError(f"{label} byte verification failed: {path}")
    return _sha256_bytes(recorded)


def _write_new_text(path: Path, text: str, label: str) -> str:
    return _write_new_bytes(path, text.encode("utf-8"), label)


def _write_new_json(path: Path, value, label: str) -> str:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return _write_new_bytes(path, payload, label)


def _relative(root: Path, path: Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def _validate_external_output_root(repo_roots, output_root: Path) -> Path:
    roots = tuple(Path(root).resolve() for root in repo_roots)
    output_root = Path(output_root).resolve()
    for root in roots:
        if output_root == root or root in output_root.parents:
            raise BenchmarkSafetyError(
                "chronological benchmark output must be outside code and data repositories"
            )
    if output_root.exists():
        raise BenchmarkSafetyError(
            f"chronological benchmark output already exists and was not overwritten: {output_root}"
        )
    return output_root


def _reference_glossary(entries: dict, through_chapter: int) -> tuple[dict, dict]:
    """Freeze mature early-chapter evidence without making it provider-visible."""
    selected = {}
    anchored_future = 0
    unanchored = 0
    for source, raw_entry in entries.items():
        if not isinstance(source, str) or not isinstance(raw_entry, dict):
            continue
        first_chapter = raw_entry.get("first_chapter")
        if not isinstance(first_chapter, int) or isinstance(first_chapter, bool):
            unanchored += 1
            continue
        if 1 <= first_chapter <= through_chapter:
            selected[source] = copy.deepcopy(raw_entry)
        elif first_chapter > through_chapter:
            anchored_future += 1
    stats = {
        "eligible_reference_entries": len(selected),
        "anchored_future_entries_excluded": anchored_future,
        "entries_without_valid_first_chapter_excluded": unanchored,
    }
    return selected, stats


def initial_state(novel_id: str, source_language: str, profile_id: str) -> dict:
    """Return the shared zero-knowledge dynamic seed for one novel arm."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "policy_version": "chronological-state-v1",
        "benchmark": BENCHMARK_ID,
        "novel_id": novel_id,
        "source_language": source_language,
        "profile_id": profile_id,
        "through_chapter": 0,
        "glossary_entries": {},
        "continuity_summary": "",
        "issues": [],
        "parent_state_sha256": None,
        "state_events": [],
    }


def _freeze_reference_addendum(project, output_root: Path, novel_root: Path):
    source = Path(project.translation_addendum_file)
    if not source.is_file():
        return {
            "present": False,
            "provider_visible": False,
            "source_sha256": None,
            "snapshot_path": None,
            "snapshot_sha256": None,
        }
    payload = source.read_bytes()
    target = novel_root / "reference" / "translation_addendum_reference_only.txt"
    frozen_sha = _write_new_bytes(target, payload, "reference-only translation addendum")
    source_sha = _sha256_bytes(payload)
    if frozen_sha != source_sha:
        raise BenchmarkSafetyError("reference addendum snapshot hash mismatch")
    return {
        "present": True,
        "provider_visible": False,
        "source_sha256": source_sha,
        "snapshot_path": _relative(output_root, target),
        "snapshot_sha256": frozen_sha,
    }


def _freeze_one_novel(inputs, output_root: Path, chapter_count: int) -> dict:
    project = inputs.project
    config = project.config
    novel_id = config.id
    expected_chapters = tuple(range(1, chapter_count + 1))
    chapter_numbers = validate_chapter_selection(inputs, expected_chapters)
    if tuple(chapter_numbers) != expected_chapters:
        raise ChronologicalPlanError(
            f"{novel_id} does not provide the complete consecutive corpus 1-{chapter_count}"
        )
    before = capture_protected_state(project)
    before_sha = _canonical_json_sha256(before)

    novel_root = output_root / novel_id
    corpus_root = novel_root / "corpus"
    prompt_root = novel_root / "prompts"
    state_root = novel_root / "states"
    reference_root = novel_root / "reference"

    profile_only_prompt = inputs.profile.translation_instructions
    provider_prompt_path = prompt_root / "profile_only_system_prompt.txt"
    provider_prompt_sha = _write_new_text(
        provider_prompt_path,
        profile_only_prompt,
        "profile-only provider system prompt",
    )

    initial = initial_state(
        novel_id,
        config.source_language,
        inputs.profile.id,
    )
    initial_state_path = state_root / "state_0000.json"
    initial_state_file_sha = _write_new_json(
        initial_state_path,
        initial,
        "initial chronological state",
    )
    initial_state_identity_sha = _canonical_json_sha256(initial)

    reference_entries, reference_stats = _reference_glossary(
        inputs.glossary_entries,
        chapter_count,
    )
    reference_document = {
        "schema_version": 1,
        "benchmark": BENCHMARK_ID,
        "role": "reference_only_never_provider_visible",
        "novel_id": novel_id,
        "through_chapter": chapter_count,
        "entries": reference_entries,
    }
    reference_path = reference_root / "mature_glossary_early_reference.json"
    reference_file_sha = _write_new_json(
        reference_path,
        reference_document,
        "reference-only mature glossary subset",
    )
    reference_entries_sha = _canonical_json_sha256(reference_entries)

    addendum = _freeze_reference_addendum(project, output_root, novel_root)

    chapter_records = []
    source_hashes = {}
    for chapter_number in chapter_numbers:
        chapter = inputs.chapters[chapter_number]
        source_text = chapter["text"]
        source_path = corpus_root / f"chapter_{chapter_number:04d}.txt"
        source_sha = _write_new_text(
            source_path,
            source_text,
            f"Chapter {chapter_number} frozen source",
        )
        source_hashes[str(chapter_number)] = source_sha
        paragraph_count = chapter.get("paragraph_count")
        if not isinstance(paragraph_count, int) or isinstance(paragraph_count, bool):
            paragraph_count = _paragraph_count(source_text)
        chapter_records.append(
            {
                "chapter_number": chapter_number,
                "source_snapshot_path": _relative(output_root, source_path),
                "source_sha256": source_sha,
                "source_character_count": len(source_text),
                "source_paragraph_count": paragraph_count,
                "source_heading": source_prompt_heading(
                    chapter,
                    config.chapter_numbering,
                    source_language=config.source_language,
                    target_language=config.target_language,
                ),
                "source_label": source_language_prompt_label(config.source_language),
            }
        )

    after = capture_protected_state(project)
    if after != before:
        raise BenchmarkSafetyError(
            f"production-owned state changed while freezing {novel_id}"
        )

    mature_glossary_file_sha = (
        _sha256_file(project.glossary_file)
        if Path(project.glossary_file).is_file()
        else None
    )
    effective_prompt_sha = _sha256_text(inputs.system_prompt)
    return {
        "novel_id": novel_id,
        "novel_title": config.title,
        "source_language": config.source_language,
        "target_language": config.target_language,
        "profile_id": inputs.profile.id,
        "chapter_numbering": config.chapter_numbering,
        "chapters": list(chapter_numbers),
        "chapter_records": chapter_records,
        "source_bundle_sha256": _canonical_json_sha256(source_hashes),
        "provider_visible_system_prompt": {
            "mode": "profile_only",
            "path": _relative(output_root, provider_prompt_path),
            "sha256": provider_prompt_sha,
            "novel_specific_addendum_included": False,
        },
        "reference_only_effective_prompt_with_mature_addendum_sha256": effective_prompt_sha,
        "reference_only_translation_addendum": addendum,
        "initial_state": {
            "path": _relative(output_root, initial_state_path),
            "file_sha256": initial_state_file_sha,
            "identity_sha256": initial_state_identity_sha,
            "glossary_entry_count": 0,
            "continuity_is_empty": True,
        },
        "reference_only_mature_glossary": {
            "provider_visible": False,
            "mature_glossary_file_sha256": mature_glossary_file_sha,
            "snapshot_path": _relative(output_root, reference_path),
            "snapshot_file_sha256": reference_file_sha,
            "entries_identity_sha256": reference_entries_sha,
            **reference_stats,
        },
        "protected_state_binding": before,
        "protected_state_sha256": before_sha,
    }


def freeze_chronological_plan(
    *,
    data_repo_root: Path,
    output_root: Path,
    expected_code_repo_head: str,
    expected_data_repo_head: str,
    novel_ids=DEFAULT_NOVELS,
    chapter_count: int = DEFAULT_CHAPTER_COUNT,
) -> tuple[dict, Path]:
    """Freeze both source corpora and bind executing code + authoritative data."""
    code_repo_root = Path(CODE_REPO_ROOT).resolve()
    data_repo_root = Path(data_repo_root).resolve()
    if not code_repo_root.is_dir():
        raise ChronologicalPlanError(
            f"code repository root does not exist: {code_repo_root}"
        )
    if not data_repo_root.is_dir():
        raise ChronologicalPlanError(
            f"data repository root does not exist: {data_repo_root}"
        )
    if not isinstance(chapter_count, int) or isinstance(chapter_count, bool) or chapter_count < 1:
        raise ChronologicalPlanError("chapter_count must be a positive integer")
    novel_ids = tuple(novel_ids)
    if not novel_ids or len(novel_ids) != len(set(novel_ids)):
        raise ChronologicalPlanError("novel_ids must be a unique non-empty sequence")

    code_head = _verify_repo_head(
        code_repo_root,
        expected_code_repo_head,
        "code repository",
    )
    data_head = _verify_repo_head(
        data_repo_root,
        expected_data_repo_head,
        "data repository",
    )

    output_root = _validate_external_output_root(
        (code_repo_root, data_repo_root),
        output_root,
    )
    output_root.mkdir(parents=True, exist_ok=False)

    novels = []
    for novel_id in novel_ids:
        inputs = load_benchmark_inputs(novel_id, data_repo_root)
        novels.append(_freeze_one_novel(inputs, output_root, chapter_count))

    state_policy = {
        "initial_dynamic_glossary": "empty",
        "initial_continuity": "empty",
        "novel_specific_addendum_provider_visible": False,
        "mature_glossary_provider_visible": False,
        "provider_visible_system_prompt": "profile_only",
        "per_arm_state_lineage": "independent",
        "translation_uses_state_before_chapter": True,
        "state_update_occurs_after_translation": True,
        "human_corrections_feed_back_into_model_state": False,
        "reference_material_role": "scoring_and_audit_only",
    }
    identity_novels = []
    for novel in novels:
        identity_novels.append(
            {
                "novel_id": novel["novel_id"],
                "source_language": novel["source_language"],
                "profile_id": novel["profile_id"],
                "chapters": novel["chapters"],
                "source_bundle_sha256": novel["source_bundle_sha256"],
                "provider_visible_system_prompt_sha256": novel[
                    "provider_visible_system_prompt"
                ]["sha256"],
                "initial_state_identity_sha256": novel["initial_state"][
                    "identity_sha256"
                ],
                "reference_entries_identity_sha256": novel[
                    "reference_only_mature_glossary"
                ]["entries_identity_sha256"],
                "protected_state_sha256": novel["protected_state_sha256"],
            }
        )
    identity = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "code_repo_head": code_head,
        "data_repo_head": data_head,
        "chapter_count_per_novel": chapter_count,
        "novels": identity_novels,
        "state_policy": state_policy,
    }
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "status": "frozen",
        "identity": identity,
        "identity_sha256": _canonical_json_sha256(identity),
        "code_repo_root_name": code_repo_root.name,
        "data_repo_root_name": data_repo_root.name,
        "output_root_name": output_root.name,
        "novels": novels,
        "state_policy": state_policy,
        "paid_provider_calls": 0,
        "production_writes": 0,
    }
    plan_path = output_root / PLAN_FILENAME
    _write_new_json(plan_path, plan, "chronological experiment plan")
    return plan, plan_path


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Freeze a zero-provider, no-leak 25+25 chronological state-growth corpus"
        )
    )
    parser.add_argument(
        "--data-repo-root",
        type=Path,
        default=CODE_REPO_ROOT,
        help=(
            "authoritative novel-data checkout; may differ from the executing "
            "feature worktree during pre-merge validation"
        ),
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-code-repo-head", required=True)
    parser.add_argument("--expected-data-repo-head", required=True)
    parser.add_argument("--chapter-count", type=int, default=DEFAULT_CHAPTER_COUNT)
    parser.add_argument("--novels", nargs="+", default=list(DEFAULT_NOVELS))
    return parser


def print_summary(plan: dict, plan_path: Path):
    plan_sha = _sha256_file(plan_path)
    print("\n" + "=" * 70)
    print("CHRONOLOGICAL STATE-GROWTH CORPUS FROZEN")
    print("=" * 70)
    print(f"Code HEAD      : {plan['identity']['code_repo_head']}")
    print(f"Data HEAD      : {plan['identity']['data_repo_head']}")
    print(f"Identity SHA256: {plan['identity_sha256']}")
    print(f"Plan SHA256    : {plan_sha}")
    print(f"Plan           : {plan_path}")
    for novel in plan["novels"]:
        reference = novel["reference_only_mature_glossary"]
        print(
            f"{novel['novel_id']}: {len(novel['chapters'])} chapters, "
            f"reference terms through corpus={reference['eligible_reference_entries']}"
        )
    print("Initial dynamic glossary entries : 0")
    print("Initial continuity                : empty")
    print("Novel addenda provider-visible    : NO")
    print("Mature glossary provider-visible : NO")
    print("Provider calls                    : 0")
    print("Production writes                 : 0")
    print("=" * 70)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        plan, plan_path = freeze_chronological_plan(
            data_repo_root=args.data_repo_root,
            output_root=args.output_root,
            expected_code_repo_head=args.expected_code_repo_head,
            expected_data_repo_head=args.expected_data_repo_head,
            novel_ids=args.novels,
            chapter_count=args.chapter_count,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_summary(plan, plan_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
