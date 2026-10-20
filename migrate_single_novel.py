"""Safely copy the legacy single-novel layout into novels/<novel_id>/.

The legacy tree is never deleted or modified. Existing destinations are
verified and never overwritten. All copied files are compared byte-for-byte.
"""

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from novel_project import NovelConfig, load_novel_config, repository_root, validate_novel_id


MIGRATION_DIRECTORIES = ("source", "data", "output", "backup", "logs")
MIGRATION_FILES = ("production_translation.txt", "pause.flag")
REQUIRED_LEGACY_FILES = (
    "data/chapters.json",
    "data/glossary.json",
    "data/progress.json",
    "data/continuity.json",
    "data/issues.json",
    "data/audit_history.json",
    "production_translation.txt",
)
REQUIRED_PROJECT_DIRECTORIES = (
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


class MigrationError(RuntimeError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_source_files(source_root):
    source_root = Path(source_root)
    result = {}
    for directory_name in MIGRATION_DIRECTORIES:
        directory = source_root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise MigrationError(f"migration source contains a symlink: {path}")
            if path.is_file():
                result[path.relative_to(source_root)] = path
    for file_name in MIGRATION_FILES:
        path = source_root / file_name
        if path.is_symlink():
            raise MigrationError(f"migration source contains a symlink: {path}")
        if path.is_file():
            result[path.relative_to(source_root)] = path
    return result


def validate_legacy_layout(source_root):
    source_root = Path(source_root)
    missing = [name for name in REQUIRED_LEGACY_FILES if not (source_root / name).is_file()]
    if missing:
        raise MigrationError("legacy project is missing required files: " + ", ".join(missing))


def compare_migration(source_root, target_root, config):
    """Return differences between a legacy source and an installed project."""
    source_root = Path(source_root)
    target_root = Path(target_root)
    differences = []
    try:
        saved_config = load_novel_config(target_root / "novel.json")
        if saved_config != config:
            differences.append("novel.json does not match the requested configuration")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        differences.append(f"novel.json is invalid: {exc}")

    source_files = migration_source_files(source_root)
    expected_relative = set(source_files)
    target_relative = set()
    for directory_name in MIGRATION_DIRECTORIES:
        directory = target_root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                target_relative.add(path.relative_to(target_root))
    for file_name in MIGRATION_FILES:
        path = target_root / file_name
        if path.is_file():
            target_relative.add(path.relative_to(target_root))

    for relative, source_path in sorted(source_files.items(), key=lambda item: str(item[0])):
        target_path = target_root / relative
        if not target_path.is_file():
            differences.append(f"missing migrated file: {relative}")
        elif file_sha256(source_path) != file_sha256(target_path):
            differences.append(f"content differs: {relative}")
    for relative in sorted(target_relative - expected_relative, key=str):
        differences.append(f"unexpected migrated file: {relative}")
    return differences


def copy_legacy_tree(source_root, staging_root):
    source_root = Path(source_root)
    staging_root = Path(staging_root)
    for directory_name in MIGRATION_DIRECTORIES:
        source = source_root / directory_name
        if source.is_dir():
            shutil.copytree(source, staging_root / directory_name, copy_function=shutil.copy2)
    for file_name in MIGRATION_FILES:
        source = source_root / file_name
        if source.is_file():
            shutil.copy2(source, staging_root / file_name)


def migrate_single_novel(source_root, config, novels_root=None, require_legacy_layout=True):
    source_root = Path(source_root).resolve()
    validate_novel_id(config.id)
    if require_legacy_layout:
        validate_legacy_layout(source_root)
    novels_root = Path(novels_root or source_root / "novels").resolve()
    target_root = (novels_root / config.id).resolve()
    if target_root.parent != novels_root:
        raise MigrationError("migration target escapes the novels directory")
    novels_root.mkdir(parents=True, exist_ok=True)

    if target_root.exists():
        differences = compare_migration(source_root, target_root, config)
        if differences:
            raise MigrationError(
                "destination already exists and was not overwritten:\n- "
                + "\n- ".join(differences)
            )
        return {"status": "already_migrated", "target": target_root}

    staging_root = novels_root / f".{config.id}.migrating-{uuid.uuid4().hex}"
    try:
        staging_root.mkdir()
        copy_legacy_tree(source_root, staging_root)
        for relative in REQUIRED_PROJECT_DIRECTORIES:
            (staging_root / relative).mkdir(parents=True, exist_ok=True)
        config_text = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
        (staging_root / "novel.json").write_text(config_text, encoding="utf-8", newline="\n")
        differences = compare_migration(source_root, staging_root, config)
        if differences:
            raise MigrationError("staged migration verification failed:\n- " + "\n- ".join(differences))
        os.replace(staging_root, target_root)
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise
    return {"status": "migrated", "target": target_root}


def build_parser():
    parser = argparse.ArgumentParser(
        description="Copy the current legacy project into an isolated novel directory"
    )
    parser.add_argument("--novel", required=True, help="new novel identifier")
    parser.add_argument("--title", default="Ming Historical Novel")
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--target-language", default="en")
    parser.add_argument("--profile", default="historical_military")
    parser.add_argument("--final-chapter", type=int, default=1406)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = NovelConfig.from_dict({
        "id": args.novel,
        "title": args.title,
        "source_language": args.source_language,
        "target_language": args.target_language,
        "profile": args.profile,
        "final_chapter": args.final_chapter,
    })
    result = migrate_single_novel(repository_root(), config)
    print(f"Migration status: {result['status']}")
    print(f"Novel project: {result['target']}")
    print("Legacy project retained: yes")
    print("API calls used: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
