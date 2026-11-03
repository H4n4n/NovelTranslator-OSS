"""Terminology corrections the owner has read and approved, applied to finished chapters locally.

An audit's own past-chapter corrections only touch spans they can prove belong to one
term, so they skip single-word ranks and terms inside longer names. A list the owner
approved can go further: every whole-word use of the old English, in each chapter whose
source has the term. Each changed chapter is backed up first, every change is logged
under output/retroactive_fixes/, and running the same list again changes nothing.
Audit proofs replay each logged change from its backup, so an audited chapter can be
corrected without blocking the novel's next start.

The list is JSON: {"replacements": [{"source": ["<source term>", ...], "old": "...", "new": "..."}],
"glossary": {"<source term>": "<new English>"}}. Glossary entries change with the
replacements; "glossary" is only for entries the prose doesn't show.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from noveltranslator_core import atomic_write_bytes, atomic_write_json
from production_run_lock import production_run_lock
from retroactive_span_planning import digest


def load_corrections(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    news = {}
    for item in data.get("replacements", []):
        if not item.get("source") or not item.get("old") or not item.get("new") or item["old"] == item["new"]:
            raise ValueError(f"a replacement needs source terms, old and new English: {item}")
        if news.setdefault(item["old"], item["new"]) != item["new"]:
            raise ValueError(f"{item['old']} has two different replacements")
    return data


def _variants(item):
    """A lowercase term also starts sentences: "bird guns" and "Bird guns"."""
    pairs = [(item["old"], item["new"])]
    if item["old"][0].islower():
        pairs.append((item["old"][0].upper() + item["old"][1:], item["new"][0].upper() + item["new"][1:]))
    return pairs


def correct_chapter(source, text, replacements):
    """Return the corrected text and its changes. Terms the source lacks are left alone."""
    pairs = dict(pair for item in replacements if any(form in source for form in item["source"])
                 for pair in _variants(item))
    if not pairs:
        return text, []
    # Longest first, in one pass, so a correction never feeds another.
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(old) for old in sorted(pairs, key=len, reverse=True)) + r")(?![\w-])")
    changes = []

    def replace(match):
        old, new = match.group(1), pairs[match.group(1)]
        if len(new) > len(old) and text.startswith(new, match.start()):
            return old  # Already corrected, as when the new form adds English in brackets.
        changes.append({"offset": match.start(), "old": old, "new": new})
        return new
    return pattern.sub(replace, text), changes


def apply_corrections(project, corrections, *, log=print):
    from build_checkpoint_epub import rebuild_reader_epub
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_path = project.retroactive_dir / f"approved_corrections_{stamp}.json"
    record = dict(utc=stamp, glossary={}, chapters={})
    with production_run_lock(project.root):
        progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
        sources = {item["chapter"]: item["text"] for item in json.loads(project.chapters_file.read_text(encoding="utf-8"))}
        backups = project.backup_dir / "approved_corrections" / stamp
        backups.mkdir(parents=True, exist_ok=True)
        project.retroactive_dir.mkdir(parents=True, exist_ok=True)
        for chapter in range(1, progress["last_completed"] + 1):
            path = project.chapter_dir / f"{chapter:04d}.md"
            before = path.read_bytes()
            text, changes = correct_chapter(sources[chapter], before.decode("utf-8"), corrections["replacements"])
            if not changes:
                continue
            # Backup and log first, so a crash never leaves an unlogged edit.
            atomic_write_bytes(backups / path.name, before)
            record["chapters"][f"{chapter:04d}"] = changes
            atomic_write_json(record_path, record)
            atomic_write_bytes(path, text.encode("utf-8"))
            log(f"Chapter {chapter}: {len(changes)} changed")
        raw_glossary = project.glossary_file.read_bytes()
        atomic_write_bytes(backups / project.glossary_file.name, raw_glossary)
        glossary = json.loads(raw_glossary)
        # Entries whose own source has a corrected term change the same way, like a technique named after its art.
        renamed = {term: correct_chapter(term, entry.get("english") or "", corrections["replacements"])[0]
                   for term, entry in glossary["entries"].items()}
        for term, english in {**renamed, **corrections.get("glossary", {})}.items():
            entry = glossary["entries"].get(term)
            if entry is None or entry.get("english") == english:
                continue
            record["glossary"][term] = {"old": entry.get("english"), "new": english}
            entry.update(english=english, status="locked", notes=f"Owner-approved correction, {stamp[:8]}.")
        atomic_write_json(project.glossary_file, glossary)
        atomic_write_json(record_path, record)
    epub = rebuild_reader_epub(project, take_lock=True)
    return dict(chapters_changed=len(record["chapters"]), replacements=sum(map(len, record["chapters"].values())),
                glossary_changed=len(record["glossary"]), record=str(record_path), epub=epub.get("status"))


def _replay(text, changes):
    """The text the logged changes make, or None when they don't fit it exactly."""
    parts, position = [], 0
    for change in changes:
        offset, old = change["offset"], change["old"]
        if offset < position or text[offset:offset + len(old)] != old:
            return None
        parts += [text[position:offset], change["new"]]
        position = offset + len(old)
    return "".join(parts) + text[position:]


def audit_lineage(retroactive_dir, backup_dir, read):
    """Each logged correction's backed-up bytes, and its step from those bytes to the corrected ones.

    An audit proof rebuilds old chapters from bytes it can find, and a chapter corrected
    after its audit keeps its earlier bytes only in the backup. A step counts only when
    the backup and the logged changes make it exactly, so an unlogged edit still fails.
    """
    blobs, steps = {}, {}
    for path in sorted(Path(retroactive_dir).glob("approved_corrections_*.json")):
        record = json.loads(read(path))
        for name, changes in record.get("chapters", {}).items():
            backup = Path(backup_dir) / "approved_corrections" / str(record.get("utc")) / f"{name}.md"
            if not backup.is_file():
                continue
            raw = read(backup)
            after = _replay(raw.decode("utf-8"), changes)
            if after is not None:
                blobs[digest(raw)] = raw
                steps.setdefault(digest(raw), set()).add(digest(after.encode("utf-8")))
    return blobs, steps


def reaches(steps, start, target):
    """Whether logged corrections lead from one chapter digest to another; no step at all counts."""
    seen, todo = set(), [start]
    while todo:
        sha = todo.pop()
        if sha == target:
            return True
        if sha not in seen:
            seen.add(sha)
            todo.extend(steps.get(sha, ()))
    return False
