"""Zero-API deterministic engine-finalization replay for benchmark outputs.

This benchmark helper measures how much of a provider's raw translation is
changed or blocked by NovelTranslator's deterministic glossary/commit-QA layer.

It deliberately stops before any model-assisted residual-script repair, state
extraction, glossary candidate review, continuity mutation, periodic audit, or
production commit. Those paid/stateful workflow stages are measured separately.

Importing this module performs no credential loading, network activity, writes,
or production-state mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
    resolve_output_dir,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION
from noveltranslator_core import (
    atomic_write_json,
    atomic_write_text,
    canonicalize_locked_high_priority_terms,
    translation_commit_qa,
)
from strict_terminology_repair import perform_deterministic_strict_terminology_repair


FINALIZATION_SCHEMA_VERSION = 1
FINALIZATION_FILENAME = "engine_finalization.json"
RAW_RESULTS_FILENAME = "results.json"
APPROVED_READER_FACING_CJK_TERMS = ()


class EngineFinalizationError(BenchmarkError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _canonical_json_sha256(value) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _load_json(path: Path, label: str):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineFinalizationError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise EngineFinalizationError(f"{label} must be a JSON object")
    return value


def _leading_duplicate_heading(text: str) -> bool:
    nonempty = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(nonempty) < 2:
        return False
    return nonempty[0].casefold() == nonempty[1].casefold()


def _qa(inputs, chapter_number: int, translation: str):
    return translation_commit_qa(
        inputs.chapters[chapter_number]["text"],
        translation,
        inputs.glossary_entries,
        inputs.profile.high_priority_categories,
        approved_reader_facing_cjk_terms=APPROVED_READER_FACING_CJK_TERMS,
        source_language=inputs.project.config.source_language,
        chapter_number=chapter_number,
    )


def _provider_request_hash(inputs, chapter_number: int, provider: str) -> str:
    model = "luna" if provider == "openai" else "deepseek-pro"
    return _canonical_json_sha256(request_kwargs(inputs, chapter_number, model))


def _verify_plan_binding(inputs, plan):
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise EngineFinalizationError("unsupported sample-plan schema version")
    if plan.get("benchmark") != "mature-glossary-engine-benchmark-v2":
        raise EngineFinalizationError("sample plan belongs to a different benchmark")
    if plan.get("novel_id") != inputs.project.config.id:
        raise EngineFinalizationError("sample plan novel identity mismatch")
    binding = plan.get("protected_state_binding")
    if not isinstance(binding, dict):
        raise EngineFinalizationError("sample plan lacks protected-state binding")
    current = capture_protected_state(inputs.project)
    if current != binding:
        raise BenchmarkSafetyError(
            "mature-glossary source/glossary/production-owned state drifted "
            "after sample planning"
        )
    selected = plan.get("selected_chapters")
    if not isinstance(selected, list) or not selected:
        raise EngineFinalizationError("sample plan contains no selected chapters")
    rows = plan.get("selections")
    if not isinstance(rows, list) or len(rows) != len(selected):
        raise EngineFinalizationError("sample plan selection rows are incomplete")
    by_chapter = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EngineFinalizationError("sample plan contains a malformed selection row")
        chapter = row.get("chapter_number")
        if chapter in by_chapter:
            raise EngineFinalizationError("sample plan contains duplicate chapter rows")
        by_chapter[chapter] = row
    if set(by_chapter) != set(selected):
        raise EngineFinalizationError("sample plan chapter rows do not match selection")
    return tuple(selected), by_chapter


def _verify_raw_results(inputs, raw_dir: Path, selected, provider_expected=None):
    results = _load_json(raw_dir / RAW_RESULTS_FILENAME, "raw benchmark results")
    if results.get("status") != "complete":
        raise EngineFinalizationError("raw benchmark results are not complete")
    if results.get("novel_id") != inputs.project.config.id:
        raise EngineFinalizationError("raw benchmark novel identity mismatch")
    provider = results.get("provider")
    if provider not in {"openai", "deepseek"}:
        raise EngineFinalizationError(f"unsupported raw benchmark provider: {provider!r}")
    if provider_expected and provider != provider_expected:
        raise EngineFinalizationError(
            f"raw benchmark provider {provider!r} does not match expected {provider_expected!r}"
        )
    if tuple(results.get("chapters_requested") or ()) != tuple(selected):
        raise EngineFinalizationError(
            "raw benchmark chapter order/set does not match frozen sample plan"
        )
    chapter_rows = results.get("chapters")
    if not isinstance(chapter_rows, list) or len(chapter_rows) != len(selected):
        raise EngineFinalizationError("raw benchmark chapter metadata is incomplete")
    metadata_by_chapter = {}
    for row in chapter_rows:
        number = row.get("chapter_number") if isinstance(row, dict) else None
        if number in metadata_by_chapter:
            raise EngineFinalizationError("raw benchmark metadata duplicates a chapter")
        metadata_by_chapter[number] = row
    if set(metadata_by_chapter) != set(selected):
        raise EngineFinalizationError("raw benchmark metadata chapter set mismatch")
    return results, metadata_by_chapter


def _read_raw_chapter(raw_dir: Path, chapter_number: int) -> str:
    path = raw_dir / f"chapter_{chapter_number:04d}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineFinalizationError(
            f"raw Chapter {chapter_number} could not be read: {exc}"
        ) from exc
    if not text.strip():
        raise EngineFinalizationError(f"raw Chapter {chapter_number} is empty")
    return text


def _deterministic_finalize_one(inputs, chapter_number: int, raw_translation: str):
    source = inputs.chapters[chapter_number]["text"]
    raw_qa = _qa(inputs, chapter_number, raw_translation)

    canonical = canonicalize_locked_high_priority_terms(
        source,
        raw_translation,
        inputs.glossary_entries,
        inputs.profile.high_priority_categories,
        chapter_number=chapter_number,
    )
    canonical_text = canonical["repaired_text"]
    canonical_qa = _qa(inputs, chapter_number, canonical_text)

    persisted = {"meta": None, "translation": None}
    strict = perform_deterministic_strict_terminology_repair(
        chapter_number=chapter_number,
        source_text=source,
        translation=canonical_text,
        meta={"stage": "translated", "benchmark_only": True},
        glossary_entries=inputs.glossary_entries,
        high_priority_categories=inputs.profile.high_priority_categories,
        persist_meta=lambda value: persisted.__setitem__("meta", value),
        persist_translation=lambda value: persisted.__setitem__("translation", value),
        qa_checker=lambda candidate: _qa(inputs, chapter_number, candidate),
        approved_reader_facing_terms=APPROVED_READER_FACING_CJK_TERMS,
    )
    final_text = strict["translation"]
    final_qa = strict["qa"]
    if persisted["translation"] is not None and persisted["translation"] != final_text:
        raise EngineFinalizationError(
            f"Chapter {chapter_number} strict-repair persistence simulation disagreed "
            "with returned translation"
        )

    blocker_types = [
        item.get("type")
        for item in final_qa.get("commit_blockers", [])
        if isinstance(item, dict)
    ]
    residual_blocked = any(
        item in {"residual_cjk", "residual_source_script"}
        for item in blocker_types
    )
    terminology_blocked = "locked_terminology" in blocker_types

    return final_text, {
        "chapter_number": chapter_number,
        "raw_sha256": _sha256_text(raw_translation),
        "deterministic_final_sha256": _sha256_text(final_text),
        "raw_character_count": len(raw_translation),
        "deterministic_final_character_count": len(final_text),
        "text_changed": final_text != raw_translation,
        "raw_qa": raw_qa,
        "canonicalization": {
            "repairs_applied": canonical.get("repairs_applied", []),
            "unresolved_strict_terms": canonical.get("unresolved_strict_terms", []),
            "post_canonicalization_qa": canonical_qa,
        },
        "strict_terminology_repair": {
            "status": strict.get("status"),
            "repairs_applied": strict.get("repairs_applied", []),
            "ambiguous_terms": strict.get("ambiguous_terms", []),
        },
        "deterministic_final_qa": final_qa,
        "deterministic_commit_allowed": bool(final_qa.get("commit_allowed")),
        "would_require_model_assisted_residual_repair": residual_blocked,
        "would_require_additional_terminology_resolution": terminology_blocked,
        "would_require_nonlocal_review": not bool(final_qa.get("commit_allowed")),
        "raw_duplicate_leading_heading_warning": _leading_duplicate_heading(
            raw_translation
        ),
        "final_duplicate_leading_heading_warning": _leading_duplicate_heading(
            final_text
        ),
        "zero_api_repairs_applied": (
            len(canonical.get("repairs_applied", []))
            + len(strict.get("repairs_applied", []))
        ),
    }


def _summary(chapters):
    return {
        "chapter_count": len(chapters),
        "raw_commit_allowed_count": sum(
            1 for row in chapters if row["raw_qa"].get("commit_allowed")
        ),
        "deterministic_commit_allowed_count": sum(
            1 for row in chapters if row["deterministic_commit_allowed"]
        ),
        "text_changed_count": sum(1 for row in chapters if row["text_changed"]),
        "zero_api_repair_count": sum(
            row["zero_api_repairs_applied"] for row in chapters
        ),
        "chapters_requiring_model_assisted_residual_repair": sum(
            1 for row in chapters if row["would_require_model_assisted_residual_repair"]
        ),
        "chapters_requiring_additional_terminology_resolution": sum(
            1 for row in chapters if row["would_require_additional_terminology_resolution"]
        ),
        "chapters_requiring_nonlocal_review": sum(
            1 for row in chapters if row["would_require_nonlocal_review"]
        ),
        "raw_duplicate_leading_heading_warnings": sum(
            1 for row in chapters if row["raw_duplicate_leading_heading_warning"]
        ),
        "final_duplicate_leading_heading_warnings": sum(
            1 for row in chapters if row["final_duplicate_leading_heading_warning"]
        ),
    }


def finalize_benchmark(
    inputs,
    *,
    plan_path,
    raw_output_dir,
    output_dir,
    expected_provider=None,
):
    plan = _load_json(Path(plan_path), "sample plan")
    selected, plan_by_chapter = _verify_plan_binding(inputs, plan)

    raw_dir = resolve_output_dir(inputs.project, raw_output_dir)
    if not raw_dir.is_dir():
        raise EngineFinalizationError(f"raw benchmark directory is missing: {raw_dir}")
    raw_results, raw_meta = _verify_raw_results(
        inputs,
        raw_dir,
        selected,
        provider_expected=expected_provider,
    )
    provider = raw_results["provider"]
    expected_hash_field = (
        "luna_request_sha256" if provider == "openai" else "deepseek_request_sha256"
    )
    for chapter_number in selected:
        actual_hash = _provider_request_hash(inputs, chapter_number, provider)
        if plan_by_chapter[chapter_number].get(expected_hash_field) != actual_hash:
            raise BenchmarkSafetyError(
                f"Chapter {chapter_number} provider request no longer matches frozen plan"
            )

    target = resolve_output_dir(inputs.project, output_dir)
    if target.exists():
        raise BenchmarkSafetyError(
            f"engine-finalization benchmark target already exists: {target}"
        )
    before = capture_protected_state(inputs.project)

    prepared = []
    chapter_results = []
    for chapter_number in selected:
        raw = _read_raw_chapter(raw_dir, chapter_number)
        final_text, result = _deterministic_finalize_one(
            inputs, chapter_number, raw
        )
        result["raw_finish_reason"] = raw_meta[chapter_number].get("finish_reason")
        result["raw_remaining_source_script_count"] = raw_meta[chapter_number].get(
            "remaining_cjk_character_count"
        )
        result["sample_role"] = plan_by_chapter[chapter_number].get("role")
        result["sample_stratum"] = plan_by_chapter[chapter_number].get("stratum")
        prepared.append((chapter_number, final_text))
        chapter_results.append(result)

    # Validate all chapters and protected state before materializing any candidate.
    if capture_protected_state(inputs.project) != before:
        raise BenchmarkSafetyError(
            "production-owned state changed during deterministic finalization analysis"
        )

    target.mkdir(parents=True, exist_ok=False)
    for chapter_number, final_text in prepared:
        atomic_write_text(target / f"chapter_{chapter_number:04d}.txt", final_text)

    document = {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "benchmark": "mature-glossary-engine-finalization-v2",
        "novel_id": inputs.project.config.id,
        "profile_id": inputs.profile.id,
        "source_language": inputs.project.config.source_language,
        "provider": raw_results["provider"],
        "model": raw_results.get("model"),
        "model_version": raw_results.get("model_version"),
        "raw_transport": raw_results.get("transport", {"type": "synchronous"}),
        "chapters_requested": list(selected),
        "raw_output_dir": str(raw_dir),
        "sample_plan": str(Path(plan_path)),
        "scope": {
            "locked_term_canonicalization": True,
            "deterministic_strict_terminology_repair": True,
            "commit_qa": True,
            "model_assisted_residual_repair": False,
            "state_extraction": False,
            "glossary_candidate_review": False,
            "continuity_mutation": False,
            "periodic_audit": False,
            "production_commit": False,
        },
        "chapters": chapter_results,
        "summary": _summary(chapter_results),
        "safety": {
            "protected_state_before": before,
            "protected_state_verified_unchanged": (
                capture_protected_state(inputs.project) == before
            ),
        },
    }
    if not document["safety"]["protected_state_verified_unchanged"]:
        raise BenchmarkSafetyError(
            "production-owned state changed while materializing finalization benchmark"
        )
    atomic_write_json(target / FINALIZATION_FILENAME, document)
    return document, target


def build_parser():
    parser = argparse.ArgumentParser(
        description="Replay deterministic NovelTranslator finalization on benchmark outputs"
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--raw-output-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--expected-provider",
        choices=("openai", "deepseek"),
        default=None,
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        inputs = load_benchmark_inputs(args.novel, args.repo_root)
        document, target = finalize_benchmark(
            inputs,
            plan_path=args.plan,
            raw_output_dir=args.raw_output_dir,
            output_dir=args.output_dir,
            expected_provider=args.expected_provider,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary = document["summary"]
    print("=" * 88)
    print("DETERMINISTIC ENGINE-FINALIZATION BENCHMARK COMPLETE")
    print("=" * 88)
    print(f"Novel                : {document['novel_id']}")
    print(f"Provider/model       : {document['provider']} / {document['model']}")
    print(f"Chapters             : {summary['chapter_count']}")
    print(f"Raw commit-eligible  : {summary['raw_commit_allowed_count']}")
    print(f"After zero-API layer : {summary['deterministic_commit_allowed_count']}")
    print(f"Chapters text changed: {summary['text_changed_count']}")
    print(f"Zero-API repairs     : {summary['zero_api_repair_count']}")
    print(
        "Need model residual repair: "
        f"{summary['chapters_requiring_model_assisted_residual_repair']}"
    )
    print(
        "Need nonlocal review      : "
        f"{summary['chapters_requiring_nonlocal_review']}"
    )
    print(f"Output               : {target}")
    print("Provider calls made  : 0")
    print("Production state changed: NO")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
