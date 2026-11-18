"""Local glossary checks for one novel: zero API calls, nothing written.

preflight (before translating):
  traps          Korean keys that mostly occur inside longer words (이란 in "…이란", 신시 in 통신시설),
                 so the glossary would send them to chapters where they mean something else.
  source English entries whose English contradicts English the source itself gives in parentheses
                 (악마의 눈(Diablo eye) against "Devil's Eye"), and recurring source pairs the
                 glossary lacks.
consistency (after translating):
                 entries whose English is missing from many committed chapters whose source has the
                 term, so either the entry or the prose needs a look.

Every result is a flag for review; the command never stops a run and always exits 0 on success.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher

HANGUL = re.compile(r"[가-힣]")
# Endings that may follow a standalone word: a key followed by one of these is used as itself.
PARTICLES = frozenset("""은 는 이 가 을 를 의 에 에서 에게 에게서 께 께서 으로 로 과 와 도 만 까지 부터 이나 나 이랑 랑 하고
처럼 보다 마저 조차 이라 라 이라고 라고 이란 란 이며 며 이다 다 였다 이었다 이야 야 아 여 님 씨 들 들은 들이 들을 들의
들에게 에서는 에서도 에는 에도 으로는 로는 으로도 로도 과는 와는 이라는 라는 인 이었던 였던 이고 고 이지 지 이자 이든
이나마 에의 으로서 로서 으로써 로써 한테 한테서 이었 였""".split())
ACRONYM = re.compile(r"[A-Z0-9][A-Z0-9 .&/-]{1,9}")
SOURCE_PAIR = re.compile(r"([가-힣]+(?: [가-힣]+){0,3})\s?\(\s*([A-Za-z][A-Za-z0-9 .,'’&/-]{1,60}?)\s*\)")


def hangul_length(text):
    return len(HANGUL.findall(text))


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _context(text, start, end, width=12):
    return text[max(0, start - width):end + width].replace("\n", " ")


def _covering(entries, key):
    """Longer glossary keys that contain `key`, with each offset of `key` inside them."""
    out = []
    for other in entries:
        if other != key and key in other:
            out += [(other, m.start()) for m in re.finditer(f"(?={re.escape(key)})", other)]
    return out


def _covered(text, start, covering):
    """Whether a longer glossary key spans this occurrence, so that entry, not this one, applies."""
    return any(text.startswith(other, start - offset) for other, offset in covering if start - offset >= 0)


def _inside_word(text, match, key):
    before = text[match.start() - 1] if match.start() else ""
    if before == "제" and key[:1].isdigit():
        before = ""
    tail = re.match(r"[가-힣]*", text[match.end():]).group(0)
    return bool(HANGUL.match(before) or (tail and tail not in PARTICLES))


def uses_term(text, key, covering):
    """Whether the chapter source uses the Korean key as itself at least once: not inside a longer
    glossary key and not inside another word. Non-Korean keys only need to occur."""
    if not HANGUL.search(key):
        return key in text
    for match in re.finditer(re.escape(key), text):
        if covering and _covered(text, match.start(), covering):
            continue
        if not _inside_word(text, match, key):
            return True
    return False


def trap_report(entries, chapters, *, max_syllables=3, min_count=3, inside_ratio=0.4, examples=3):
    """Short Korean keys used more often inside other words than as themselves.

    An occurrence inside a longer glossary key (동해 in 동해함대, 준우 in 신준우) is covered by that
    entry and is not counted; an ordinal 제 before a numbered key (제1 야전군) is part of the name.
    """
    texts = [(c["chapter"], c["text"]) for c in chapters]
    rows = []
    for key, entry in entries.items():
        if not HANGUL.search(key) or hangul_length(key) > max_syllables or entry.get("status") == "contextual":
            continue
        covering = _covering(entries, key)
        total, inside, chapters_with, sample = 0, 0, set(), []
        pattern = re.compile(re.escape(key))
        for number, text in texts:
            for match in pattern.finditer(text):
                if covering and _covered(text, match.start(), covering):
                    continue
                total += 1
                chapters_with.add(number)
                if _inside_word(text, match, key):
                    inside += 1
                    if len(sample) < examples:
                        sample.append({"chapter": number, "context": _context(text, match.start(), match.end())})
        if total >= min_count and inside / total >= inside_ratio:
            rows.append({"korean": key, "english": entry.get("english"), "status": entry.get("status"),
                         "category": entry.get("category"), "occurrences": total, "inside_other_words": inside,
                         "inside_ratio": round(inside / total, 2), "chapters": len(chapters_with),
                         "examples": sample})
    return sorted(rows, key=lambda r: (-r["inside_ratio"], -r["occurrences"]))


def source_english_pairs(chapters):
    """Every Korean phrase the source follows with English in parentheses: {phrase: Counter(english)}."""
    pairs = defaultdict(Counter)
    where = defaultdict(set)
    for chapter in chapters:
        for match in SOURCE_PAIR.finditer(chapter["text"]):
            phrase, english = match.group(1).strip(), match.group(2).strip()
            if sum(ch.isalpha() for ch in english) < 2:
                continue
            pairs[phrase][english] += 1
            where[phrase].add(chapter["chapter"])
    return pairs, where


def source_english_report(entries, chapters, *, min_missing=2):
    """Entries contradicting the source's own English, and recurring source pairs the glossary lacks."""
    pairs, where = source_english_pairs(chapters)
    keys = sorted((k for k in entries if HANGUL.search(k)), key=len, reverse=True)
    conflicts, matched_phrases = defaultdict(lambda: {"forms": Counter(), "chapters": set()}), set()
    for phrase, forms in pairs.items():
        # The longest glossary key the phrase ends with names the term the English belongs to.
        key = next((k for k in keys if phrase == k or phrase.endswith(k) and len(k) >= 2), None)
        if key is None:
            continue
        matched_phrases.add(phrase)
        english = _norm(entries[key].get("english") or "")
        for form, count in forms.items():
            source = _norm(form)
            if not english or not source or source in english or english in source:
                continue
            if ACRONYM.fullmatch(form.strip()) or SequenceMatcher(None, source, english).ratio() >= 0.8:
                continue  # An abbreviation (CSIS) or the source's own typo ("Compelx").
            conflicts[key]["forms"][form] += count
            conflicts[key]["chapters"] |= where[phrase]
    conflict_rows = [{"korean": key, "english": entries[key].get("english"), "status": entries[key].get("status"),
                      "source_english": dict(value["forms"].most_common()), "chapters": sorted(value["chapters"])}
                     for key, value in conflicts.items()]
    missing = defaultdict(lambda: {"forms": Counter(), "chapters": set()})
    for phrase, forms in pairs.items():
        if phrase in matched_phrases:
            continue
        # A phrase can carry leading words ("프로젝트 니들리스"); its last word is the likely term.
        term = phrase.split()[-1]
        if term in entries or hangul_length(term) < 2:
            continue
        missing[term]["forms"].update(forms)
        missing[term]["chapters"] |= where[phrase]
    missing_rows = [{"korean": term, "source_english": dict(value["forms"].most_common()),
                     "chapters": sorted(value["chapters"])}
                    for term, value in missing.items()
                    # A term the source glosses the same way at least twice; a word followed by a different
                    # example each time (비행기(UH-60), 비행기(F-35B)) is not a term.
                    if value["forms"].most_common(1)[0][1] >= min_missing]
    return (sorted(conflict_rows, key=lambda r: r["korean"]),
            sorted(missing_rows, key=lambda r: (-sum(r["source_english"].values()), r["korean"])))


def _fold(text):
    return text.lower().replace("’", "'").replace("‘", "'")


def english_forms(english):
    """The forms prose may use for an entry: alternatives split on "/", a bracketed gloss
    ("Sinsi (God's City)") and a leading "the" dropped, apostrophes folded."""
    forms = set()
    for part in re.split(r"\s*/\s*", english):
        part = _fold(part).strip()
        for form in (part, re.sub(r"\s*\([^)]*\)\s*$", "", part)):
            form = re.sub(r"^the\s+", "", form).strip()
            if form:
                forms.add(form)
    return forms


def consistency_report(entries, chapters, translations, *, min_chapters=3, threshold=0.8, categories=None):
    """Entries whose English is absent from too many committed chapters whose source has the term."""
    rows = []
    lowered = {number: _fold(text) for number, text in translations.items()}
    for key, entry in entries.items():
        english = (entry.get("english") or "").strip()
        if (not english or len(key) < 2 or entry.get("status") == "contextual"
                or (categories and entry.get("category") not in categories)):
            continue
        covering = _covering(entries, key) if HANGUL.search(key) else []
        present = [c["chapter"] for c in chapters
                   if c["chapter"] in lowered and key in c["text"] and uses_term(c["text"], key, covering)]
        if len(present) < min_chapters:
            continue
        forms = english_forms(english)
        missed = [n for n in present if not any(form in lowered[n] for form in forms)]
        rate = 1 - len(missed) / len(present)
        if rate < threshold:
            rows.append({"korean": key, "english": english, "status": entry.get("status"),
                         "category": entry.get("category"), "chapters": len(present),
                         "match_rate": round(rate, 2), "missed_chapters": missed[:20]})
    return sorted(rows, key=lambda r: (r["match_rate"], -r["chapters"]))


def consistency_summary(entries, chapters, translations, *, min_chapters=3, categories=None):
    """Chapter-level match rate over all qualifying entries, for a one-line headline."""
    rows = consistency_report(entries, chapters, translations, min_chapters=min_chapters, threshold=1.01,
                              categories=categories)
    total = sum(r["chapters"] for r in rows)
    hits = sum(r["match_rate"] * r["chapters"] for r in rows)
    return {"entries": len(rows), "chapter_checks": total, "match_rate": round(hits / total, 3) if total else None,
            "fully_consistent": sum(r["match_rate"] == 1 for r in rows)}


def _load(project):
    glossary = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    chapters = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    return glossary.get("entries", {}), chapters


def _translations(project):
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    last = int(progress.get("last_completed") or 0)
    out = {}
    for number in range(1, last + 1):
        path = project.chapter_dir / f"{number:04d}.md"
        if path.is_file():
            out[number] = path.read_text(encoding="utf-8")
    return out


def _print_preflight(traps, conflicts, missing):
    print(f"Short keys used mostly inside other words: {len(traps)}")
    for r in traps:
        print(f"  {r['korean']} = {r['english']} ({r['status']}): {r['inside_other_words']} of {r['occurrences']} "
              f"inside other words, {r['chapters']} chapters")
        for e in r["examples"]:
            print(f"      ch. {e['chapter']}: …{e['context']}…")
    print(f"\nEntries contradicting the source's own English: {len(conflicts)}")
    for r in conflicts:
        forms = ", ".join(f"{f} ×{n}" for f, n in r["source_english"].items())
        print(f"  {r['korean']} = {r['english']} ({r['status']}); source writes: {forms}; ch. {r['chapters'][:8]}")
    print(f"\nRecurring source pairs the glossary lacks: {len(missing)}")
    for r in missing[:40]:
        forms = ", ".join(f"{f} ×{n}" for f, n in r["source_english"].items())
        print(f"  {r['korean']}: {forms}; ch. {r['chapters'][:8]}")


def _print_consistency(summary, rows):
    rate = "n/a" if summary["match_rate"] is None else f"{summary['match_rate']:.1%}"
    print(f"Entries in {summary.get('min_chapters', 3)}+ committed chapters: {summary['entries']}; chapter-level "
          f"match {rate}; fully consistent {summary['fully_consistent']}")
    print(f"Below the threshold: {len(rows)}")
    for r in rows:
        print(f"  {r['match_rate']:.0%} of {r['chapters']} ch.  {r['korean']} = {r['english']} ({r['status']}, "
              f"{r['category']}); missed ch. {r['missed_chapters'][:10]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local glossary checks (zero API calls, read-only)")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight", help="before translating: traps and the source's own English")
    pre.add_argument("--max-syllables", type=int, default=3)
    pre.add_argument("--inside-ratio", type=float, default=0.4)
    con = sub.add_parser("consistency", help="after translating: entries the prose does not follow")
    con.add_argument("--threshold", type=float, default=0.8)
    con.add_argument("--min-chapters", type=int, default=3)
    con.add_argument("--category", action="append", help="limit to one or more glossary categories")
    args = parser.parse_args(argv)

    from novel_project import select_project
    project = select_project(args.novel, args.repo_root)
    entries, chapters = _load(project)
    if args.command == "preflight":
        traps = trap_report(entries, chapters, max_syllables=args.max_syllables, inside_ratio=args.inside_ratio)
        conflicts, missing = source_english_report(entries, chapters)
        if args.json:
            print(json.dumps({"traps": traps, "source_english_conflicts": conflicts,
                              "source_english_missing": missing}, ensure_ascii=False, indent=1))
        else:
            _print_preflight(traps, conflicts, missing)
    else:
        translations = _translations(project)
        categories = set(args.category) if args.category else None
        rows = consistency_report(entries, chapters, translations, min_chapters=args.min_chapters,
                                  threshold=args.threshold, categories=categories)
        summary = consistency_summary(entries, chapters, translations, min_chapters=args.min_chapters,
                                      categories=categories)
        summary["min_chapters"] = args.min_chapters
        if args.json:
            print(json.dumps({"summary": summary, "below_threshold": rows}, ensure_ascii=False, indent=1))
        else:
            _print_consistency(summary, rows)
    print("\nAPI calls: 0; files written: 0", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
