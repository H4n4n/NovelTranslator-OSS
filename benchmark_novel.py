"""Run isolated, translation-only benchmarks against a selected novel.

Importing this module loads no credentials, initializes no API client, performs
no network activity, and writes no files. Benchmark execution deliberately
bypasses production pending, QA, state extraction, glossary review, audit, and
commit paths.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from benchmark_model_config import (
    benchmark_pricing_snapshot,
    economy_pricing_snapshot,
    resolve_benchmark_model,
)
from chapter_library import (
    chapter_production_index,
    source_language_prompt_label,
    source_prompt_heading,
)
from novel_project import NovelProject, SOURCE_TYPE_TXT, load_novel_project
from production_run_lock import production_run_lock
from noveltranslator_core import (
    atomic_write_json,
    atomic_write_text,
    calculate_cost_from_token_counts,
    glossary_strict_qa_errors,
    glossary_entries_to_text,
    relevant_glossary_entries,
    residual_source_script_report,
    STRICT_LOCKED_GLOSSARY_CATEGORIES,
)
from translation_profile import (
    TranslationProfile,
    effective_translation_instructions,
    load_translation_profile,
)


RESULTS_SCHEMA_VERSION = 3
RESULTS_FILENAME = "results.json"
PROTECTED_FILE_ATTRIBUTES = (
    "progress_file",
    "glossary_file",
    "continuity_file",
    "issues_file",
    "audit_history_file",
    "chapters_file",
)
PROTECTED_DIRECTORY_ATTRIBUTES = (
    "chapter_dir",
    "pending_dir",
    "qa_dir",
    "audit_dir",
)


class BenchmarkError(RuntimeError):
    pass


class BenchmarkSafetyError(BenchmarkError):
    pass


@dataclass(frozen=True)
class BenchmarkInputs:
    project: NovelProject
    profile: TranslationProfile
    system_prompt: str
    chapters: dict
    glossary_entries: dict


def utc_now():
    return datetime.now(timezone.utc)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_snapshot(path):
    path = Path(path)
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): _sha256_file(item)
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def capture_protected_state(project):
    """Hash production-owned files and output trees without changing them."""
    files = {}
    for attribute in PROTECTED_FILE_ATTRIBUTES:
        path = getattr(project, attribute)
        files[attribute] = _sha256_file(path) if path.is_file() else None
    directories = {
        attribute: _directory_snapshot(getattr(project, attribute))
        for attribute in PROTECTED_DIRECTORY_ATTRIBUTES
    }
    return {"files": files, "directories": directories}


def _load_json(path, expected_type, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, expected_type):
        raise BenchmarkError(f"{label} has an invalid top-level structure")
    return value


def load_benchmark_inputs(novel_id, repo_root=None):
    """Load one novel's immutable benchmark inputs without credentials or writes."""
    project = load_novel_project(novel_id, repo_root)
    profile = load_translation_profile(project.config.profile, project.repository_root)
    system_prompt = effective_translation_instructions(
        profile, project.translation_addendum_file
    )

    if project.chapters_file.is_file():
        chapter_items = _load_json(project.chapters_file, list, "chapter library")
    elif project.config.source_type == SOURCE_TYPE_TXT:
        from extract_txt import extract_source_txt

        extracted = extract_source_txt(project)
        chapter_items = extracted["chapters"]
    else:
        raise BenchmarkError("chapter library could not be read: file is missing")
    chapter_map = {}
    for item in chapter_items:
        if not isinstance(item, dict):
            raise BenchmarkError("chapter library contains a non-object entry")
        try:
            chapter_number = chapter_production_index(item)
        except ValueError as exc:
            raise BenchmarkError(f"chapter library contains an invalid entry: {exc}") from exc
        if chapter_number in chapter_map:
            raise BenchmarkError(
                f"chapter library contains duplicate Chapter {chapter_number}"
            )
        if not isinstance(item.get("text"), str):
            raise BenchmarkError(f"Chapter {chapter_number} has no valid source text")
        chapter_map[chapter_number] = item

    glossary = _load_json(project.glossary_file, dict, "glossary")
    entries = glossary.get("entries")
    if not isinstance(entries, dict):
        raise BenchmarkError("glossary.entries must be a JSON object")
    strict_errors = glossary_strict_qa_errors(
        entries, project.config.id, profile.high_priority_categories
    )
    if strict_errors:
        raise BenchmarkError("; ".join(strict_errors))
    return BenchmarkInputs(project, profile, system_prompt, chapter_map, entries)


def validate_chapter_selection(inputs, chapter_numbers):
    if not chapter_numbers:
        raise BenchmarkError("at least one benchmark chapter is required")
    validated = []
    seen = set()
    for chapter_number in chapter_numbers:
        if (
            not isinstance(chapter_number, int)
            or isinstance(chapter_number, bool)
            or chapter_number < 1
        ):
            raise BenchmarkError("benchmark chapter numbers must be positive integers")
        if chapter_number > inputs.project.config.final_chapter:
            raise BenchmarkError(
                f"Chapter {chapter_number} exceeds configured final chapter "
                f"{inputs.project.config.final_chapter}"
            )
        if chapter_number not in inputs.chapters:
            raise BenchmarkError(f"Chapter {chapter_number} is absent from the source library")
        if chapter_number in seen:
            raise BenchmarkError(f"Chapter {chapter_number} was requested more than once")
        seen.add(chapter_number)
        validated.append(chapter_number)
    return tuple(validated)


def resolve_output_dir(project, output_dir=None):
    """Keep all benchmark output inside the selected novel's benchmark tree."""
    benchmark_root = project.benchmark_dir.resolve()
    if output_dir is None:
        candidate = benchmark_root / "pilot"
    else:
        requested = Path(output_dir)
        candidate = requested if requested.is_absolute() else benchmark_root / requested
    candidate = candidate.resolve()
    if candidate != benchmark_root and benchmark_root not in candidate.parents:
        raise BenchmarkSafetyError(
            f"benchmark output must remain under the selected novel: {benchmark_root}"
        )
    return candidate


def relevant_glossary_for_chapter(inputs, chapter_number):
    chapter = inputs.chapters[chapter_number]
    strict_categories = (
        set(inputs.profile.high_priority_categories)
        & set(STRICT_LOCKED_GLOSSARY_CATEGORIES)
    )
    return relevant_glossary_entries(
        chapter["text"],
        inputs.glossary_entries,
        source_variant_categories=strict_categories,
    )


def build_benchmark_user_prompt(
    chapter_number,
    source_text,
    glossary_text,
    source_heading=None,
    source_label="SOURCE TEXT",
):
    """Build the production translation prompt with continuity intentionally absent."""
    source_heading = source_heading or f"CHAPTER {chapter_number}"
    return f"""
ESTABLISHED GLOSSARY FOR THIS CHAPTER:

{glossary_text}

{source_label} — {source_heading}:

{source_text}
""".strip()


def request_kwargs(inputs, chapter_number, config=None):
    config = resolve_benchmark_model(config.model if hasattr(config, "model") else config)
    chapter = inputs.chapters[chapter_number]
    glossary_text = glossary_entries_to_text(
        relevant_glossary_for_chapter(inputs, chapter_number),
        high_priority_categories=inputs.profile.high_priority_categories,
        strict_locked_categories=STRICT_LOCKED_GLOSSARY_CATEGORIES,
        chapter_number=chapter_number,
    )
    return {
        "model": config.model,
        "messages": [
            {"role": "system", "content": inputs.system_prompt},
            {
                "role": "user",
                "content": build_benchmark_user_prompt(
                    chapter_number,
                    chapter["text"],
                    glossary_text,
                    source_prompt_heading(
                        chapter,
                        inputs.project.config.chapter_numbering,
                        source_language=inputs.project.config.source_language,
                        target_language=inputs.project.config.target_language,
                    ),
                    source_language_prompt_label(
                        inputs.project.config.source_language
                    ),
                ),
            },
        ],
        **(
            {
                "max_tokens": config.max_tokens,
                "extra_body": {"thinking": {"type": config.reasoning}},
            }
            if config.provider == "deepseek"
            else {
                "max_completion_tokens": config.max_tokens,
                "reasoning_effort": config.reasoning,
            }
        ),
    }


def _usage_value(usage, name):
    if isinstance(usage, dict):
        return usage.get(name, 0) or 0
    return getattr(usage, name, 0) or 0


def _word_count(text):
    return len(text.split())


def _chapter_result(
    chapter,
    response,
    pricing,
    economy_pricing,
    elapsed,
    output_path,
    project,
):
    if not getattr(response, "choices", None):
        raise BenchmarkError("translation response contained no choices")
    choice = response.choices[0]
    message = getattr(choice, "message", None)
    translation = (getattr(message, "content", None) or "") if message else ""
    usage = getattr(response, "usage", None)
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    cache_hits = _usage_value(usage, "prompt_cache_hit_tokens")
    cache_misses = _usage_value(usage, "prompt_cache_miss_tokens")
    if not cache_hits:
        prompt_details = (
            usage.get("prompt_tokens_details", {})
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens_details", None)
        )
        if isinstance(prompt_details, dict):
            cache_hits = prompt_details.get("cached_tokens", 0) or 0
        elif prompt_details is not None:
            cache_hits = getattr(prompt_details, "cached_tokens", 0) or 0
    if not cache_misses and prompt_tokens:
        cache_misses = max(prompt_tokens - cache_hits, 0)
    total_cost, cache_hits, cache_misses = calculate_cost_from_token_counts(
        prompt_tokens, completion_tokens, cache_hits, cache_misses, pricing
    )
    input_cost = (
        cache_hits / 1_000_000 * pricing["cache_hit_rate_per_million"]
        + cache_misses / 1_000_000 * pricing["cache_miss_rate_per_million"]
    )
    output_cost = (
        completion_tokens / 1_000_000 * pricing["output_rate_per_million"]
    )
    economy_total_cost, _, _ = calculate_cost_from_token_counts(
        prompt_tokens,
        completion_tokens,
        cache_hits,
        cache_misses,
        economy_pricing,
    )
    economy_uncached_total_cost, _, _ = calculate_cost_from_token_counts(
        prompt_tokens,
        completion_tokens,
        0,
        prompt_tokens,
        economy_pricing,
    )
    try:
        displayed_path = output_path.relative_to(project.root).as_posix()
    except ValueError:
        displayed_path = str(output_path)
    paragraph_count = chapter.get("paragraph_count")
    if not isinstance(paragraph_count, int) or isinstance(paragraph_count, bool):
        paragraph_count = len(
            [part for part in chapter["text"].split("\n\n") if part.strip()]
        )
    residual = residual_source_script_report(
        translation, project.config.source_language
    )
    return translation, {
        "chapter_number": chapter["chapter"],
        "source_character_count": len(chapter["text"]),
        "source_paragraph_count": paragraph_count,
        "elapsed_seconds": elapsed,
        "finish_reason": getattr(choice, "finish_reason", None),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "translation_character_count": len(translation),
        "translation_word_count": _word_count(translation),
        "remaining_cjk_character_count": residual[
            "remaining_cjk_character_count"
        ],
        "residual_cjk_fragments": residual["residual_cjk_fragments"],
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
        "pricing_version": pricing["pricing_version"],
        "pricing_period": pricing["pricing_period"],
        "pricing_mode": pricing["pricing_mode"],
        "request_started_utc": pricing["request_started_utc"],
        "cache_hit_rate_per_million": pricing["cache_hit_rate_per_million"],
        "cache_miss_rate_per_million": pricing["cache_miss_rate_per_million"],
        "output_rate_per_million": pricing["output_rate_per_million"],
        "economy_projected_cost_usd": economy_total_cost,
        "economy_uncached_projected_cost_usd": economy_uncached_total_cost,
        "economy_pricing_version": economy_pricing["pricing_version"],
        "economy_pricing_period": economy_pricing["pricing_period"],
        "economy_pricing_mode": economy_pricing["pricing_mode"],
        "economy_cache_hit_rate_per_million": economy_pricing[
            "cache_hit_rate_per_million"
        ],
        "economy_cache_miss_rate_per_million": economy_pricing[
            "cache_miss_rate_per_million"
        ],
        "economy_output_rate_per_million": economy_pricing[
            "output_rate_per_million"
        ],
        "output_path": displayed_path,
    }


def result_totals(results):
    return {
        "elapsed_seconds": sum(item["elapsed_seconds"] for item in results),
        "input_tokens": sum(item["prompt_tokens"] for item in results),
        "output_tokens": sum(item["completion_tokens"] for item in results),
        "cache_hit_tokens": sum(item["cache_hit_tokens"] for item in results),
        "cache_miss_tokens": sum(item["cache_miss_tokens"] for item in results),
        "input_cost_usd": sum(item["input_cost_usd"] for item in results),
        "output_cost_usd": sum(item["output_cost_usd"] for item in results),
        "total_cost_usd": sum(item["total_cost_usd"] for item in results),
        "economy_projected_cost_usd": sum(
            item["economy_projected_cost_usd"] for item in results
        ),
        "economy_uncached_projected_cost_usd": sum(
            item["economy_uncached_projected_cost_usd"] for item in results
        ),
    }


def results_document(
    inputs,
    chapter_numbers,
    results,
    before,
    config=None,
    verified=False,
):
    config = resolve_benchmark_model(config.model if hasattr(config, "model") else config)
    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "status": "complete" if len(results) == len(chapter_numbers) else "in_progress",
        "novel_id": inputs.project.config.id,
        "novel_title": inputs.project.config.title,
        "profile_id": inputs.profile.id,
        "provider": config.provider,
        "model": config.model,
        "model_version": config.version,
        "thinking": config.reasoning,
        "max_tokens": config.max_tokens,
        "continuity_mode": "omitted",
        "chapters_requested": list(chapter_numbers),
        "chapters": list(results),
        "totals": result_totals(results),
        "safety": {
            "protected_state_before": before,
            "protected_state_verified_unchanged": verified,
        },
    }


def _claim_results_path(results_path, document):
    """Exclusively and durably claim one benchmark output target before transport."""
    results_path = Path(results_path)
    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    try:
        with results_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise BenchmarkSafetyError(
            "benchmark output already exists and was not overwritten: "
            f"{results_path}"
        ) from exc
    except OSError as exc:
        raise BenchmarkSafetyError(
            f"benchmark output claim could not be durably written: {results_path}: {exc}"
        ) from exc

    try:
        recorded = results_path.read_bytes()
    except OSError as exc:
        raise BenchmarkSafetyError(
            f"benchmark output claim could not be verified: {results_path}: {exc}"
        ) from exc
    if recorded != payload:
        raise BenchmarkSafetyError(
            f"benchmark output claim verification failed: {results_path}"
        )


def run_benchmark(
    inputs,
    chapter_numbers,
    client,
    output_dir=None,
    now=utc_now,
    timer=time.perf_counter,
    sleep=time.sleep,
    model=None,
):
    """Translate selected chapters without entering any production state path."""
    config = resolve_benchmark_model(
        model.model if hasattr(model, "model") else model
    )
    chapter_numbers = validate_chapter_selection(inputs, chapter_numbers)
    output_dir = resolve_output_dir(inputs.project, output_dir)
    results_path = output_dir / RESULTS_FILENAME
    targets = [results_path] + [
        output_dir / f"chapter_{chapter_number:04d}.txt"
        for chapter_number in chapter_numbers
    ]
    existing = [path for path in targets if path.exists()]
    if existing:
        raise BenchmarkSafetyError(
            "benchmark output already exists and was not overwritten: "
            + ", ".join(str(path) for path in existing)
        )

    before = capture_protected_state(inputs.project)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    _claim_results_path(
        results_path,
        results_document(inputs, chapter_numbers, results, before, config),
    )

    operation_error = None
    try:
        for chapter_number in chapter_numbers:
            chapter = inputs.chapters[chapter_number]
            kwargs = request_kwargs(inputs, chapter_number, config)

            start = timer()
            request_started = now()
            pricing = benchmark_pricing_snapshot(config, request_started)
            economy_pricing = economy_pricing_snapshot(config, request_started)
            response = client.chat.completions.create(**kwargs)
            elapsed = timer() - start
            output_path = output_dir / f"chapter_{chapter_number:04d}.txt"
            translation, result = _chapter_result(
                chapter,
                response,
                pricing,
                economy_pricing,
                elapsed,
                output_path,
                inputs.project,
            )
            atomic_write_text(output_path, translation)
            results.append(result)
            atomic_write_json(
                results_path,
                results_document(inputs, chapter_numbers, results, before, config),
            )
    except BaseException as exc:
        operation_error = exc

    after = capture_protected_state(inputs.project)
    unchanged = after == before
    document = results_document(
        inputs,
        chapter_numbers,
        results,
        before,
        config,
        verified=unchanged,
    )
    if operation_error is not None:
        document["status"] = "failed"
        document["error"] = {
            "type": type(operation_error).__name__,
            "message": str(operation_error),
        }
    if not unchanged:
        document["status"] = "failed"
        document["error"] = {
            "type": "BenchmarkSafetyError",
            "message": "protected production state changed during benchmark execution",
        }
    atomic_write_json(results_path, document)

    if not unchanged:
        raise BenchmarkSafetyError(
            "protected production state changed during benchmark execution"
        )
    if operation_error is not None:
        raise operation_error
    return document


def create_benchmark_client(config):
    """Initialize the API client only after the operator invokes the CLI."""
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    config = resolve_benchmark_model(
        config.model if hasattr(config, "model") else config
    )
    if config.provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise BenchmarkError("DEEPSEEK_API_KEY was not found in .env")
        return OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            max_retries=0,
        )
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise BenchmarkError("OPENAI_API_KEY was not found in .env")
    return OpenAI(api_key=api_key, max_retries=0)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run a translation-only novel benchmark without committing production state"
        )
    )
    parser.add_argument("--novel", required=True, help="novel project identifier")
    parser.add_argument(
        "--model",
        default="deepseek-pro",
        choices=("deepseek-pro", "pro", "luna", "gpt-5.6-luna"),
        help="benchmark-only model selection (production configuration is unchanged)",
    )
    parser.add_argument(
        "--chapters", required=True, nargs="+", type=int, help="source chapter numbers"
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "optional subdirectory under the selected novel's output/benchmark/ "
            "(default: pilot)"
        ),
    )
    return parser


def print_summary(document, output_dir):
    print("\n" + "=" * 70)
    print("NON-COMMITTING TRANSLATION BENCHMARK COMPLETE")
    print("=" * 70)
    print(f"Novel: {document['novel_id']}")
    print(f"Profile: {document['profile_id']}")
    print(f"Provider/model: {document['provider']} / {document['model']}")
    print(f"Chapters: {', '.join(str(item) for item in document['chapters_requested'])}")
    print(f"Input tokens: {document['totals']['input_tokens']:,}")
    print(f"Output tokens: {document['totals']['output_tokens']:,}")
    print(f"Measured synchronous cost: ${document['totals']['total_cost_usd']:.6f}")
    print(
        "Projected economy/bulk cost (observed cache): "
        f"${document['totals']['economy_projected_cost_usd']:.6f}"
    )
    print(
        "Projected economy/bulk cost (uncached): "
        f"${document['totals']['economy_uncached_projected_cost_usd']:.6f}"
    )
    print(f"Protected state unchanged: {document['safety']['protected_state_verified_unchanged']}")
    print(f"Output: {output_dir}")
    print("Production progress/accounting updated: NO")
    print("=" * 70)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        project = load_novel_project(args.novel)
        with production_run_lock(project.root):
            inputs = load_benchmark_inputs(args.novel)
            chapter_numbers = validate_chapter_selection(inputs, args.chapters)
            output_dir = resolve_output_dir(inputs.project, args.output_dir)
            config = resolve_benchmark_model(args.model)
            client = create_benchmark_client(config)
            document = run_benchmark(
                inputs,
                chapter_numbers,
                client,
                output_dir=output_dir,
                model=config,
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_summary(document, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
