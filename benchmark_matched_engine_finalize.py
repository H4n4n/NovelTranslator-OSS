"""Recovery-aware zero-API deterministic finalization for matched benchmark corpora.

Consumes the immutable recovery manifest plus the frozen RAW matched-corpus
manifest. It preserves RAW outputs in place, supports an in-place replacement
of an ambiguous sample chapter, and reads DeepSeek's immutable one-chapter
shards without aggregating or rewriting them.

Importing this module performs no credential loading, network activity, writes,
or production-state mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from benchmark_engine_finalize import (
    FINALIZATION_SCHEMA_VERSION,
    _deterministic_finalize_one,
    _provider_request_hash,
    _summary,
)
from benchmark_novel import (
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    resolve_output_dir,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION
from noveltranslator_core import atomic_write_json, atomic_write_text


RECOVERY_BENCHMARK_ID = "mature-glossary-ambiguous-recovery-v2"
RAW_CORPUS_BENCHMARK_ID = "mature-glossary-raw-matched-corpus-v1"
OUTPUT_FILENAME = "engine_finalization.json"


class MatchedFinalizationError(RuntimeError):
    pass


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_text(text: str) -> str:
    return _sha_bytes(text.encode("utf-8"))


def _load_json(path: Path, label: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatchedFinalizationError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise MatchedFinalizationError(f"{label} must be a JSON object")
    return value


def _load_bound(path: Path, expected_sha: str, label: str):
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise MatchedFinalizationError(f"{label} could not be read: {exc}") from exc
    actual = _sha_bytes(payload)
    if actual != expected_sha:
        raise BenchmarkSafetyError(
            f"{label} SHA256 mismatch: {actual} != {expected_sha}"
        )
    return _load_json(path, label)


def _effective_plan(inputs, plan, recovery):
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise MatchedFinalizationError("unsupported sample-plan schema version")
    if plan.get("benchmark") != "mature-glossary-engine-benchmark-v2":
        raise MatchedFinalizationError("sample plan belongs to a different benchmark")
    if plan.get("novel_id") != inputs.project.config.id:
        raise MatchedFinalizationError("sample plan novel identity mismatch")
    if capture_protected_state(inputs.project) != plan.get("protected_state_binding"):
        raise BenchmarkSafetyError("frozen mature-glossary state has drifted")

    selected = tuple(int(v) for v in plan.get("selected_chapters") or ())
    rows = plan.get("selections")
    if not selected or not isinstance(rows, list) or len(rows) != len(selected):
        raise MatchedFinalizationError("sample plan selection rows are incomplete")
    by_chapter = {int(row["chapter_number"]): row for row in rows}
    if set(by_chapter) != set(selected):
        raise MatchedFinalizationError("sample plan chapter rows do not match selection")

    if recovery.get("benchmark") != RECOVERY_BENCHMARK_ID:
        raise MatchedFinalizationError("recovery manifest belongs to a different benchmark")
    if recovery.get("status") != "frozen_zero_api":
        raise MatchedFinalizationError("recovery manifest is not frozen_zero_api")
    if recovery.get("authorization", {}).get("ambiguous_target_retry_forbidden") is not True:
        raise BenchmarkSafetyError("recovery manifest does not forbid ambiguous retry")

    novel_id = inputs.project.config.id
    final = recovery.get("final_matched_sample", {}).get("chapters_by_novel", {}).get(
        novel_id
    )
    if not isinstance(final, list) or not final:
        raise MatchedFinalizationError(
            f"recovery manifest lacks final sample for {novel_id}"
        )
    final = tuple(int(v) for v in final)
    replacement = recovery.get("replacement") or {}

    if replacement.get("novel_id") != novel_id:
        if final != selected:
            raise BenchmarkSafetyError("recovery changed an unaffected novel sample")
        return final, by_chapter

    retired = int(replacement["retired_chapter"])
    new = int(replacement["chapter_number"])
    if retired not in selected or new in selected:
        raise BenchmarkSafetyError("invalid retired/replacement sample topology")
    slot = selected.index(retired)
    expected = list(selected)
    expected[slot] = new
    if tuple(expected) != final:
        raise BenchmarkSafetyError("recovery is not an exact in-place replacement")

    frozen = by_chapter[retired]
    if int(frozen["stratum"]) != int(replacement["stratum"]):
        raise BenchmarkSafetyError("replacement stratum changed")
    if frozen["role"] != replacement["role"]:
        raise BenchmarkSafetyError("replacement role changed")

    replacement_row = dict(frozen)
    replacement_row.update(
        {
            "chapter_number": new,
            "stratum": int(replacement["stratum"]),
            "role": replacement["role"],
            "luna_request_sha256": replacement["luna_request_sha256"],
            "deepseek_request_sha256": replacement["deepseek_request_sha256"],
        }
    )
    effective = {k: v for k, v in by_chapter.items() if k != retired}
    effective[new] = replacement_row
    if set(effective) != set(final):
        raise BenchmarkSafetyError("effective recovered sample rows are incomplete")
    return final, effective


def _corpus_pairs(inputs, corpus, recovery, selected):
    if corpus.get("benchmark") != RAW_CORPUS_BENCHMARK_ID:
        raise MatchedFinalizationError("RAW corpus belongs to a different benchmark")
    if corpus.get("status") != "frozen_raw_complete":
        raise MatchedFinalizationError("RAW corpus is not frozen complete")
    if corpus.get("recovery_id") != recovery.get("recovery_id"):
        raise BenchmarkSafetyError("RAW corpus recovery ID mismatch")
    pairs = corpus.get("pairs")
    if not isinstance(pairs, list):
        raise MatchedFinalizationError("RAW corpus pairs are missing")
    if corpus.get("source_count") != len(pairs):
        raise MatchedFinalizationError("RAW corpus source count is inconsistent")
    if corpus.get("translation_count") != len(pairs) * 2:
        raise MatchedFinalizationError("RAW corpus translation count is inconsistent")

    novel_id = inputs.project.config.id
    rows = [p for p in pairs if p.get("novel_id") == novel_id]
    by_chapter = {}
    for pair in rows:
        chapter = int(pair["chapter_number"])
        if chapter in by_chapter:
            raise MatchedFinalizationError("RAW corpus duplicates a chapter")
        by_chapter[chapter] = pair
    if set(by_chapter) != set(selected):
        raise BenchmarkSafetyError("RAW corpus does not match effective sample")

    for chapter in selected:
        source = inputs.chapters[chapter]["text"]
        if by_chapter[chapter].get("source_sha256") != _sha_text(source):
            raise BenchmarkSafetyError(f"source SHA drift for Chapter {chapter}")
    return by_chapter


def _raw_file_sha256(path: Path, chapter: int) -> str:
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise MatchedFinalizationError(
            f"RAW Chapter {chapter} could not be read: {exc}"
        ) from exc
    if not payload:
        raise MatchedFinalizationError(f"RAW Chapter {chapter} is empty")
    return _sha_bytes(payload)


def _read_text(path: Path, chapter: int):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MatchedFinalizationError(
            f"RAW Chapter {chapter} could not be read: {exc}"
        ) from exc
    if not text.strip():
        raise MatchedFinalizationError(f"RAW Chapter {chapter} is empty")
    return text


def _luna_raw(inputs, raw_output_dir, pairs, selected):
    raw_dir = resolve_output_dir(inputs.project, raw_output_dir)
    result = _load_json(raw_dir / "results.json", "Luna RAW results")
    if result.get("status") != "complete":
        raise MatchedFinalizationError("Luna RAW results are not complete")
    if result.get("provider") != "openai" or result.get("model") != "gpt-5.6-luna":
        raise MatchedFinalizationError("Luna RAW provider/model mismatch")
    if tuple(result.get("chapters_requested") or ()) != selected:
        raise MatchedFinalizationError("Luna RAW chapter order does not match sample")
    rows = result.get("chapters")
    if not isinstance(rows, list) or len(rows) != len(selected):
        raise MatchedFinalizationError("Luna RAW metadata is incomplete")
    meta = {int(row["chapter_number"]): row for row in rows}
    if set(meta) != set(selected):
        raise MatchedFinalizationError("Luna RAW metadata chapter set mismatch")
    batch_id = result.get("transport", {}).get("batch_id")
    paths = {}
    for chapter in selected:
        info = pairs[chapter].get("luna") or {}
        if info.get("provider") != "openai" or info.get("model") != "gpt-5.6-luna":
            raise BenchmarkSafetyError(f"Luna corpus identity mismatch Chapter {chapter}")
        if info.get("batch_id") != batch_id:
            raise BenchmarkSafetyError(f"Luna Batch ID mismatch Chapter {chapter}")
        path = raw_dir / f"chapter_{chapter:04d}.txt"
        _read_text(path, chapter)
        if info.get("translation_sha256") != _raw_file_sha256(path, chapter):
            raise BenchmarkSafetyError(f"Luna RAW SHA mismatch Chapter {chapter}")
        paths[chapter] = path
    return result, meta, paths


def _deepseek_raw(inputs, pairs, selected):
    meta = {}
    paths = {}
    versions = set()
    for chapter in selected:
        info = pairs[chapter].get("deepseek") or {}
        if info.get("provider") != "deepseek" or info.get("model") != "deepseek-v4-pro":
            raise BenchmarkSafetyError(
                f"DeepSeek corpus identity mismatch Chapter {chapter}"
            )
        raw_dir = resolve_output_dir(inputs.project, info.get("output_dir"))
        result = _load_json(
            raw_dir / "results.json",
            f"DeepSeek RAW Chapter {chapter} results",
        )
        if result.get("status") != "complete":
            raise MatchedFinalizationError(f"DeepSeek Chapter {chapter} is not complete")
        if result.get("provider") != "deepseek" or result.get("model") != "deepseek-v4-pro":
            raise MatchedFinalizationError(
                f"DeepSeek Chapter {chapter} provider/model mismatch"
            )
        if result.get("chapters_requested") != [chapter]:
            raise MatchedFinalizationError(
                f"DeepSeek Chapter {chapter} requested identity mismatch"
            )
        rows = result.get("chapters")
        if not isinstance(rows, list) or len(rows) != 1:
            raise MatchedFinalizationError(
                f"DeepSeek Chapter {chapter} metadata is incomplete"
            )
        row = rows[0]
        if row.get("chapter_number") != chapter or row.get("finish_reason") != "stop":
            raise MatchedFinalizationError(
                f"DeepSeek Chapter {chapter} did not finish normally"
            )
        if not result.get("safety", {}).get("protected_state_verified_unchanged"):
            raise BenchmarkSafetyError(
                f"DeepSeek Chapter {chapter} lacks protected-state proof"
            )
        path = raw_dir / f"chapter_{chapter:04d}.txt"
        _read_text(path, chapter)
        if info.get("translation_sha256") != _raw_file_sha256(path, chapter):
            raise BenchmarkSafetyError(f"DeepSeek RAW SHA mismatch Chapter {chapter}")
        meta[chapter] = row
        paths[chapter] = path
        if result.get("model_version") is not None:
            versions.add(result["model_version"])

    if len(versions) > 1:
        raise BenchmarkSafetyError("DeepSeek shards disagree on model version")
    result = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "model_version": next(iter(versions), None),
        "transport": {"type": "synchronous_sharded"},
    }
    return result, meta, paths


def finalize_matched(
    inputs,
    *,
    plan_path,
    recovery_path,
    recovery_sha256,
    raw_corpus_path,
    raw_corpus_sha256,
    provider,
    output_dir,
    raw_output_dir=None,
):
    plan = _load_json(Path(plan_path), "sample plan")
    recovery = _load_bound(recovery_path, recovery_sha256, "recovery manifest")
    corpus = _load_bound(raw_corpus_path, raw_corpus_sha256, "RAW corpus manifest")
    selected, plan_rows = _effective_plan(inputs, plan, recovery)
    pairs = _corpus_pairs(inputs, corpus, recovery, selected)

    if provider == "openai":
        if not raw_output_dir:
            raise MatchedFinalizationError("Luna finalization requires raw-output-dir")
        raw_result, raw_meta, raw_paths = _luna_raw(
            inputs, raw_output_dir, pairs, selected
        )
    elif provider == "deepseek":
        raw_result, raw_meta, raw_paths = _deepseek_raw(inputs, pairs, selected)
    else:
        raise MatchedFinalizationError(f"unsupported provider: {provider}")

    hash_field = (
        "luna_request_sha256" if provider == "openai" else "deepseek_request_sha256"
    )
    for chapter in selected:
        if plan_rows[chapter].get(hash_field) != _provider_request_hash(
            inputs, chapter, provider
        ):
            raise BenchmarkSafetyError(
                f"Chapter {chapter} request no longer matches frozen sample"
            )

    target = resolve_output_dir(inputs.project, output_dir)
    if target.exists():
        raise BenchmarkSafetyError(f"finalization target already exists: {target}")

    before = capture_protected_state(inputs.project)
    prepared = []
    chapter_rows = []
    for chapter in selected:
        raw = _read_text(raw_paths[chapter], chapter)
        final_text, row = _deterministic_finalize_one(inputs, chapter, raw)
        row["raw_finish_reason"] = raw_meta[chapter].get("finish_reason")
        row["raw_remaining_source_script_count"] = raw_meta[chapter].get(
            "remaining_cjk_character_count"
        )
        row["sample_role"] = plan_rows[chapter].get("role")
        row["sample_stratum"] = plan_rows[chapter].get("stratum")
        prepared.append((chapter, final_text))
        chapter_rows.append(row)

    if capture_protected_state(inputs.project) != before:
        raise BenchmarkSafetyError("production-owned state changed during analysis")

    target.mkdir(parents=True, exist_ok=False)
    for chapter, text in prepared:
        atomic_write_text(target / f"chapter_{chapter:04d}.txt", text)

    document = {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "benchmark": "mature-glossary-matched-engine-finalization-v2",
        "novel_id": inputs.project.config.id,
        "provider": raw_result["provider"],
        "model": raw_result.get("model"),
        "model_version": raw_result.get("model_version"),
        "raw_transport": raw_result.get("transport", {"type": "synchronous"}),
        "chapters_requested": list(selected),
        "recovery_id": recovery["recovery_id"],
        "recovery_manifest_sha256": recovery_sha256,
        "raw_corpus_manifest_sha256": raw_corpus_sha256,
        "scope": {
            "locked_term_canonicalization": True,
            "deterministic_strict_terminology_repair": True,
            "commit_qa": True,
            "model_assisted_residual_repair": False,
            "production_commit": False,
        },
        "chapters": chapter_rows,
        "summary": _summary(chapter_rows),
        "safety": {
            "protected_state_before": before,
            "protected_state_verified_unchanged": (
                capture_protected_state(inputs.project) == before
            ),
        },
    }
    if not document["safety"]["protected_state_verified_unchanged"]:
        raise BenchmarkSafetyError("production-owned state changed while writing output")
    atomic_write_json(target / OUTPUT_FILENAME, document)
    return document, target


def build_parser():
    parser = argparse.ArgumentParser(
        description="Recovery-aware zero-API deterministic matched-corpus finalization"
    )
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--recovery-manifest", type=Path, required=True)
    parser.add_argument("--recovery-manifest-sha256", required=True)
    parser.add_argument("--raw-corpus-manifest", type=Path, required=True)
    parser.add_argument("--raw-corpus-manifest-sha256", required=True)
    parser.add_argument("--provider", choices=("openai", "deepseek"), required=True)
    parser.add_argument("--raw-output-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        inputs = load_benchmark_inputs(args.novel, args.repo_root)
        document, target = finalize_matched(
            inputs,
            plan_path=args.plan,
            recovery_path=args.recovery_manifest,
            recovery_sha256=args.recovery_manifest_sha256,
            raw_corpus_path=args.raw_corpus_manifest,
            raw_corpus_sha256=args.raw_corpus_manifest_sha256,
            provider=args.provider,
            output_dir=args.output_dir,
            raw_output_dir=args.raw_output_dir,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary = document["summary"]
    print("=" * 92)
    print("RECOVERY-AWARE DETERMINISTIC ENGINE FINALIZATION COMPLETE")
    print("=" * 92)
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
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
