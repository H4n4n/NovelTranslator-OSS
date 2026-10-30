"""Create one isolated NovelTranslator project without API or network access."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from novel_project import (
    CHAPTER_NUMBERING_GLOBAL,
    CHAPTER_NUMBERING_MODES,
    DEFAULT_AUDIT_INTERVAL,
    NovelConfig,
    SOURCE_TYPE_EPUB,
    SOURCE_TYPES,
    load_novel_config,
    novels_root_for,
    repository_root,
    validate_novel_id,
)
from noveltranslator_core import PRICING_VERSION, validate_progress_consistency
from translation_profile import load_translation_profile


PROJECT_DIRECTORIES = (
    "source",
    "data",
    "output/chapters",
    "output/pending",
    "output/qa",
    "output/audits",
    "output/retroactive_fixes",
    "output/epub",
    "backup",
    "logs",
)


class NovelCreationError(RuntimeError):
    pass


def initial_project_state():
    """Return fresh independent containers compatible with production."""
    return {
        "chapters.json": [],
        "glossary.json": {"entries": {}},
        "progress.json": {
            "completed": [],
            "last_completed": 0,
            "next_chapter": 1,
            "failed": [],
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cache_hit_tokens": 0,
            "total_cache_miss_tokens": 0,
            "estimated_cost_usd": 0.0,
            "rate_correct_cost_usd": 0.0,
            "rate_correct_cost_start_chapter": 1,
            "pricing_version": PRICING_VERSION,
            "last_audited_chapter": 0,
        },
        "continuity.json": {"latest_chapter": 0, "summary": ""},
        "issues.json": {"issues": []},
        "audit_history.json": {"audits": []},
    }


def _write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def _build_staged_project(staging_root, config, source_path=None):
    for relative in PROJECT_DIRECTORIES:
        (staging_root / relative).mkdir(parents=True, exist_ok=True)
    _write_json(staging_root / "novel.json", config.to_dict())
    (staging_root / "translation_addendum.txt").write_text(
        "", encoding="utf-8", newline="\n"
    )
    for name, value in initial_project_state().items():
        _write_json(staging_root / "data" / name, value)
    if source_path is not None:
        source_path = Path(source_path).resolve()
        if not source_path.is_file():
            raise NovelCreationError(f"source file does not exist: {source_path}")
        destination = staging_root / "source" / f"novel.{config.source_type}"
        shutil.copyfile(source_path, destination)
        if destination.read_bytes() != source_path.read_bytes():
            raise NovelCreationError("staged source copy differs from authoritative source")


def _verify_staged_project(staging_root, config, source_path=None):
    missing_directories = [
        relative for relative in PROJECT_DIRECTORIES
        if not (staging_root / relative).is_dir()
    ]
    if missing_directories:
        raise NovelCreationError(
            "staged project is missing directories: " + ", ".join(missing_directories)
        )
    saved_config = load_novel_config(staging_root / "novel.json")
    if saved_config != config:
        raise NovelCreationError("staged novel.json does not match requested configuration")
    addendum_file = staging_root / "translation_addendum.txt"
    if not addendum_file.is_file() or addendum_file.read_bytes() != b"":
        raise NovelCreationError(
            "staged translation_addendum.txt must exist and begin empty"
        )
    expected_state = initial_project_state()
    for name, expected in expected_state.items():
        path = staging_root / "data" / name
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NovelCreationError(f"staged data/{name} is invalid: {exc}") from exc
        if actual != expected:
            raise NovelCreationError(f"staged data/{name} does not match safe empty state")
    progress_errors = validate_progress_consistency(
        expected_state["progress.json"], set()
    )["errors"]
    if progress_errors:
        raise NovelCreationError(
            "staged progress is inconsistent: " + "; ".join(progress_errors)
        )
    if source_path is not None:
        expected = Path(source_path).resolve().read_bytes()
        installed = (
            staging_root / "source" / f"novel.{config.source_type}"
        ).read_bytes()
        if installed != expected:
            raise NovelCreationError("staged source copy failed byte verification")


def create_novel_project(config, repo_root=None, source_path=None):
    """Create and atomically install one fully initialized novel project."""
    if not isinstance(config, NovelConfig):
        raise TypeError("config must be a NovelConfig")
    validate_novel_id(config.id)
    repo_root = Path(repo_root or repository_root()).resolve()

    # Profile validation is intentionally the first filesystem-dependent step.
    load_translation_profile(config.profile, repo_root)

    novels_root = novels_root_for(repo_root)
    target_root = (novels_root / config.id).resolve()
    if target_root.parent != novels_root:
        raise NovelCreationError("novel target escapes the novels directory")
    if target_root.exists():
        raise NovelCreationError(
            f"novel project already exists and was not overwritten: {target_root}"
        )

    novels_root.mkdir(parents=True, exist_ok=True)
    staging_root = novels_root / f".{config.id}.creating-{uuid.uuid4().hex}"
    try:
        staging_root.mkdir()
        _build_staged_project(staging_root, config, source_path=source_path)
        _verify_staged_project(staging_root, config, source_path=source_path)
        if target_root.exists():
            raise NovelCreationError(
                f"novel project appeared during creation and was not overwritten: {target_root}"
            )
        os.rename(staging_root, target_root)
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise
    return target_root


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create a new isolated NovelTranslator project (zero API calls)"
    )
    parser.add_argument("--id", required=True, help="lowercase novel identifier")
    parser.add_argument("--title", required=True, help="reader-facing novel title")
    parser.add_argument("--profile", required=True, help="translation profile identifier")
    parser.add_argument("--final-chapter", required=True, type=int)
    parser.add_argument("--author")
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--target-language", default="en")
    parser.add_argument(
        "--source-type",
        choices=sorted(SOURCE_TYPES),
        default=SOURCE_TYPE_EPUB,
        help="authoritative source container type (default: epub)",
    )
    parser.add_argument(
        "--source",
        help="optional source file copied byte-for-byte into source/novel.<type>",
    )
    parser.add_argument("--audit-interval", type=int, default=DEFAULT_AUDIT_INTERVAL)
    parser.add_argument(
        "--chapter-numbering",
        choices=sorted(CHAPTER_NUMBERING_MODES),
        default=CHAPTER_NUMBERING_GLOBAL,
        help="global visible numbering or spine-sequential production indexing",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = NovelConfig.from_dict({
            "id": args.id,
            "title": args.title,
            "author": args.author,
            "source_language": args.source_language,
            "target_language": args.target_language,
            "profile": args.profile,
            "final_chapter": args.final_chapter,
            "audit_interval": args.audit_interval,
            "chapter_numbering": args.chapter_numbering,
            "source_type": args.source_type,
        })
        target = create_novel_project(config, source_path=args.source)
    except (OSError, ValueError, TypeError, NovelCreationError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Created novel: {config.id}")
    print(f"Project root: {target}")
    print(f"Profile: {config.profile}")
    if args.source:
        print(f"Installed source: source/novel.{config.source_type}")
    else:
        print(
            f"Next step: place the source {config.source_type.upper()} under "
            f"source/novel.{config.source_type} and run extraction."
        )
    print("API calls used: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
