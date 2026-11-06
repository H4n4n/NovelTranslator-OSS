"""List glossary keys that the source mostly uses inside longer words. Read-only.

A Korean key such as 각하 ("Your Excellency") also occurs inside 생각하다 ("to think"),
so a plain search finds it in nearly every chapter. Such a key is sent to chapters
that do not use it and can make a term look inconsistent. Locked keys change only
with the owner's decision, so the report is for review; it never edits the glossary.

    python glossary_homonym_report.py --novel <id> [--min-share 0.5] [--out report.md]
"""
import argparse
import json
from pathlib import Path

from batch_translation import INSIDE_WORDS_SHARE, MIN_KEY_CHARS, word_occurrences
from novel_project import load_novel_project


def homonym_report(project, min_share=INSIDE_WORDS_SHARE):
    """Rows for keys whose occurrences in committed chapters are mostly inside longer words."""
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    chapters = [item for item in json.loads(project.chapters_file.read_text(encoding="utf-8"))
                if isinstance(item, dict) and item.get("chapter", 0) <= progress["last_completed"]]
    source = "\n".join(item.get("text", "") for item in chapters)
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]
    rows = []
    for key, entry in entries.items():
        if len(key) < MIN_KEY_CHARS:
            continue
        as_word, inside = word_occurrences(key, source)
        if inside and inside >= min_share * (as_word + inside):
            rows.append({"source": key, "english": entry.get("english") or " / ".join(entry.get("approved_renderings", [])),
                         "status": entry.get("status", "provisional"), "category": entry.get("category", ""),
                         "as_word": as_word, "inside_words": inside})
    rows.sort(key=lambda row: (row["status"] != "locked", -row["inside_words"]))
    return rows


def as_markdown(rows, novel_id):
    lines = [f"# Glossary keys mostly found inside other words: {novel_id}", "",
             f"{len(rows)} keys. Uses as a word vs inside longer words, in committed chapters.", "",
             "| Key | English | Status | Category | As a word | Inside words |", "|---|---|---|---|---|---|"]
    lines += [f"| {row['source']} | {row['english']} | {row['status']} | {row['category']} | "
              f"{row['as_word']} | {row['inside_words']} |" for row in rows]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--min-share", type=float, default=INSIDE_WORDS_SHARE)
    parser.add_argument("--out", type=Path, default=None, help="write the report here instead of printing it")
    args = parser.parse_args(argv)
    project = load_novel_project(args.novel, repo_root=args.repo_root)
    text = as_markdown(homonym_report(project, args.min_share), args.novel)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
