"""Extend a finished novel with a sequel file (zero API calls).

    python extend_novel.py --novel <id> --file <sequel.txt> [--number-offset N] [--rules rules.json]
    python extend_novel.py --novel <id> --file <sequel.txt> ... --write

The sequel becomes the next continuation file, source/novel_2.txt (then
novel_3.txt, ...), converted to UTF-8 the way new novels are imported. The
original source is never touched. Without --write it only shows what the sequel
would add. With --write it backs up novel.json and source_overrides.json under
backup/extend_novel/<time>/, writes the file and its `continuations` entry,
raises final_chapter, and re-extracts in memory to prove the novel's existing
units come out unchanged; if they don't, it puts everything back. It never
installs the chapter library: run the usual extract command afterwards.

`--rules` is a JSON file of TXT rules for the sequel ({"line_classifications":
[...], "heading_overrides": [...]}), line numbers counted within that file, for
headings the extractor doesn't recognize on its own.
"""
import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import extract_txt
from chapter_library import validate_chapter_library
from noveltranslator_core import atomic_write_bytes, atomic_write_text
from novel_project import SOURCE_TYPE_TXT, load_novel_project
from production_run_lock import ProductionRunLockedError, production_run_lock


class ExtendError(RuntimeError):
    pass


def _extract(project):
    """The whole library as the normal extractor would build it now, and its blocking errors."""
    if project.config.source_type == SOURCE_TYPE_TXT:
        return extract_txt.extract_source_txt(project)["chapters"], ()
    import extract_epub
    result = extract_epub.extract_source_epub(project)
    return result.chapters, result.validation_errors


def plan_extension(project, sequel, *, number_offset=0, rules=None):
    """What adding `sequel` would do, read-only. Problems go in plan["errors"]."""
    from novel_intake import detect_encoding_and_decode
    existing = extract_txt.load_txt_continuations(project)
    name = f"novel_{len(existing) + 2}.txt"
    destination = project.source_dir / name
    library = json.loads(project.chapters_file.read_text(encoding="utf-8")) \
        if project.chapters_file.is_file() else []
    plan = {"name": name, "destination": destination, "number_offset": number_offset,
            "library_units": len(library), "added": [], "errors": []}
    if destination.exists():
        plan["errors"].append(f"{destination} already exists")
    if len(library) != project.config.final_chapter:
        plan["errors"].append(
            f"the installed chapter library has {len(library)} units but final_chapter is "
            f"{project.config.final_chapter}; extract and install the current source first")
    text, encoding, confidence = detect_encoding_and_decode(Path(sequel).read_bytes())
    plan.update(encoding=encoding, data=text.encode("utf-8"))
    if confidence != "high":
        plan["errors"].append(f"the file's encoding is unclear ({encoding}, {confidence} confidence); "
                              "convert it to UTF-8 first")
        return plan
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / name
        path.write_bytes(plan["data"])
        source = extract_txt.inspect_utf8_txt(path)
    plan["source_sha256"] = source["source_sha256"]
    try:
        checked = extract_txt._validate_txt_rules(rules or {}, source["line_record_count"])
        added, parts = extract_txt.extend_with_continuations(
            library, [(name, source, checked, number_offset)])
    except extract_txt.TXTExtractionError as exc:
        plan["errors"].append(str(exc))
        return plan
    plan.update(added=added, report=parts[0], rules=rules or {},
                final_chapter=len(library) + len(added))
    plan["errors"] += list(validate_chapter_library(
        library + added, len(library) + len(added), project.config.chapter_numbering))
    return plan


def print_plan(project, plan):
    print(f"Novel: {project.config.id} ({project.config.source_type}, "
          f"{plan['library_units']} units, final_chapter {project.config.final_chapter})")
    print(f"Sequel file: source/{plan['name']} (read as {plan['encoding']})")
    added = plan["added"]
    if added:
        kinds = {}
        for unit in added:
            kinds[unit["unit_kind"]] = kinds.get(unit["unit_kind"], 0) + 1
        chapters = [unit for unit in added if unit["unit_kind"] == "chapter"]
        print(f"New units: {len(added)} ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}), "
              f"production units {added[0]['production_index']}-{added[-1]['production_index']}")
        if chapters:
            print(f"Reader-facing chapters: {chapters[0]['local_chapter_start']}-"
                  f"{chapters[-1]['local_chapter_end']} (volume {added[0]['volume_index']}); "
                  f"source numbers {chapters[0]['source_number']}-{chapters[-1]['source_number']}")
        report = plan["report"]
        print(f"Source-number gaps: {report['source_number_missing'] or 'none'}; "
              f"duplicates: {report['source_number_duplicates'] or 'none'}; "
              f"coverage {report['narrative_coverage']:.2%}")
        print(f"First new heading: {added[0]['heading']}")
        print(f"Last new heading: {added[-1]['heading']}")
        print(f"final_chapter would become {plan['final_chapter']}")
    for error in plan["errors"]:
        print(f"BLOCKER: {error}")


def apply_extension(project, plan, *, clock=lambda: datetime.now(timezone.utc)):
    """Write the sequel, its entry and the new final_chapter; undo it all if the novel's units move."""
    if plan["errors"]:
        raise ExtendError("the plan has blockers")
    with production_run_lock(project.root):
        backup = project.backup_dir / "extend_novel" / clock().strftime("%Y%m%dT%H%M%SZ")
        backup.mkdir(parents=True, exist_ok=False)
        saved = {}
        for path in (project.config_file, project.source_overrides_file):
            saved[path] = path.is_file()
            if saved[path]:
                shutil.copy2(path, backup / path.name)
        destination = plan["destination"]
        if destination.exists():
            raise ExtendError(f"{destination} already exists")
        try:
            atomic_write_bytes(destination, plan["data"])
            overrides = (json.loads(project.source_overrides_file.read_text(encoding="utf-8"))
                         if saved[project.source_overrides_file] else
                         {"source_sha256": extract_txt.sha256_file(project.source_file)})
            entry = {"file": plan["name"], "source_sha256": plan["source_sha256"]}
            if plan["rules"]:
                entry["txt"] = plan["rules"]
            if plan["number_offset"]:
                entry["number_offset"] = plan["number_offset"]
            overrides.setdefault("continuations", []).append(entry)
            atomic_write_text(project.source_overrides_file,
                              json.dumps(overrides, ensure_ascii=False, indent=2) + "\n")
            config = json.loads(project.config_file.read_text(encoding="utf-8"))
            config["final_chapter"] = plan["final_chapter"]
            atomic_write_text(project.config_file, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            reloaded = load_novel_project(project.config.id, project.repository_root)
            library = json.loads(project.chapters_file.read_text(encoding="utf-8"))
            fresh, errors = _extract(reloaded)
            if errors:
                raise ExtendError("extraction would be blocked: " + "; ".join(errors))
            if fresh[:len(library)] != library or len(fresh) != plan["final_chapter"]:
                raise ExtendError("re-extraction does not keep the novel's existing units unchanged")
        except Exception:
            destination.unlink(missing_ok=True)
            for path, existed in saved.items():
                if existed:
                    shutil.copy2(backup / path.name, path)
                else:
                    path.unlink(missing_ok=True)
            raise
    return backup


def build_parser():
    parser = argparse.ArgumentParser(description="Extend a finished novel with a sequel file (zero API calls)")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--file", required=True, help="the sequel as a TXT file")
    parser.add_argument("--number-offset", type=int, default=0,
                        help="shift the sequel's reader-facing chapter numbers by this much")
    parser.add_argument("--rules", help="a JSON file of TXT rules for the sequel")
    parser.add_argument("--write", action="store_true", help="write it; without this, only show the plan")
    parser.add_argument("--repo-root")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from cli_text_streams import configure_cli_text_streams
    configure_cli_text_streams()
    try:
        project = load_novel_project(args.novel, args.repo_root)
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8")) if args.rules else None
        plan = plan_extension(project, Path(args.file), number_offset=args.number_offset, rules=rules)
        print_plan(project, plan)
        if plan["errors"]:
            return 1
        if not args.write:
            print("Dry run: nothing was written. Add --write to extend the novel.")
            return 0
        backup = apply_extension(project, plan)
    except ProductionRunLockedError:
        print(f"{args.novel} is running (its production lock is held); nothing was changed.")
        return 1
    except (OSError, ValueError, RuntimeError) as exc:  # extraction errors are RuntimeErrors
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    script = "extract_txt.py" if project.config.source_type == SOURCE_TYPE_TXT else "extract_epub.py"
    first = plan["added"][0]["production_index"]
    print(f"Extended. Backups: {backup}")
    print(f"Next: python {script} --novel {args.novel} --overwrite")
    print(f"Then settle the new chapters: python glossary_settle.py --novel {args.novel} propose "
          f"--from-chapter {first} --budget-usd <limit>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
