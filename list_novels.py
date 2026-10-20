"""List isolated NovelTranslator projects without API or network access."""

import argparse
import json
from pathlib import Path
import sys

from novel_project import load_novel_project, repository_root
from noveltranslator_core import validate_progress_consistency
from translation_profile import load_translation_profile


def _numeric_chapter_files(directory):
    if not directory.is_dir():
        return set()
    return {
        int(path.stem)
        for path in directory.glob("*.md")
        if path.is_file() and path.stem.isdigit()
    }


def inspect_novel_directory(directory, repo_root):
    novel_id = directory.name
    try:
        project = load_novel_project(novel_id, repo_root)
        profile = load_translation_profile(project.config.profile, repo_root)
        with project.progress_file.open("r", encoding="utf-8") as handle:
            progress = json.load(handle)
        if not isinstance(progress, dict):
            raise ValueError("data/progress.json must be a JSON object")
        consistency = validate_progress_consistency(
            progress, _numeric_chapter_files(project.chapter_dir)
        )
        if consistency["errors"]:
            raise ValueError("; ".join(consistency["errors"]))
        completed = progress["last_completed"]
        next_chapter = progress["next_chapter"]
        if completed > project.config.final_chapter:
            raise ValueError("completed chapter exceeds final_chapter")
        percent = completed / project.config.final_chapter * 100.0
        return {
            "valid": True,
            "id": project.config.id,
            "title": project.config.title,
            "profile": profile.id,
            "completed": completed,
            "next": next_chapter,
            "final": project.config.final_chapter,
            "percent": percent,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "id": novel_id, "error": str(exc)}


def collect_novels(repo_root=None):
    repo_root = Path(repo_root or repository_root()).resolve()
    novels_root = repo_root / "novels"
    if not novels_root.is_dir():
        return []
    return [
        inspect_novel_directory(directory, repo_root)
        for directory in sorted(novels_root.iterdir(), key=lambda path: path.name)
        if directory.is_dir() and not directory.name.startswith(".")
    ]


def show_novels(repo_root=None):
    entries = collect_novels(repo_root)
    print("NovelTranslator projects (zero API calls)")
    print("=" * 42)
    if not entries:
        print("No configured novels found.")
        return 0
    for index, entry in enumerate(entries):
        if index:
            print()
        print(entry["id"])
        if not entry["valid"]:
            print("  status: INVALID")
            print(f"  error: {entry['error']}")
            continue
        print(f"  title: {entry['title']}")
        print(f"  profile: {entry['profile']}")
        print(f"  completed: {entry['completed']}")
        print(f"  next: {entry['next']}")
        print(f"  final: {entry['final']}")
        print(f"  progress: {entry['percent']:.1f}%")
    invalid_count = sum(not entry["valid"] for entry in entries)
    if invalid_count:
        print(f"\nInvalid projects: {invalid_count}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List isolated NovelTranslator projects (zero API calls)"
    )
    parser.parse_args(argv)
    return show_novels()


if __name__ == "__main__":
    raise SystemExit(main())
