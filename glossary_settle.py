"""Settle glossary terms before translating, with DeepSeek, and install them only when approved.

`propose` scans the novel's source locally for recurring words and two-word phrases the glossary
lacks. It never sends a 1-syllable key, and it skips locally any short candidate that nearly always
starts mid-word, after another syllable (요하 in 필요하다, 여주 in 보여주다). A compound's first or last
part (조선 in 조선군, 총관 in 대총관) is not mid-word, and a candidate found mid-word less than 70% of
the time is still sent. The rest go in chunks to DeepSeek with two contexts each, the first occurrence and the middle one, so a term is seen early and later in the book. DeepSeek keeps
only the terms that need one stable English rendering and gives that English and a category. Local
checks then drop proposals whose key is not in the source or has one syllable, map unknown categories
to the profile's general one, capitalize formal names the way the engine stores them, and flag keys
that mostly match inside other words. Results go to output/glossary_settle/<run>/ (proposals.json,
report.md); the glossary is not touched.

A chunk whose answer is all reasoning and no content (every completion token spent thinking) is
sent once more as two half chunks, each its own journaled request; a half that again gets no answer
is reported as not done. Nothing is split further.

`install` adds the approved proposals of a run to data/glossary.json after backing it up: only
keys the glossary lacks, never a changed entry, and no flagged trap unless asked.

Every request goes through the paid request journal (replay or send once), reserves its
worst-case cost first, and waits for pause.flag and the novel's off-peak setting.

    python glossary_settle.py --novel <id> propose --budget-usd 1
    python glossary_settle.py --novel <id> install --run <run>
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from chapter_review import ALLOWANCE_REFUSAL, ReviewStop, Sender, parse_json, reasoning_only
from glossary_checks import HANGUL, hangul_length, trap_report
from noveltranslator_core import atomic_write_json, format_technique_name
from production_paid_request_journal import (PaidRequestJournalError, PaidRequestUncertainError,
                                             release_ambiguous_requests)

CHUNK = 150
SETTLE_TOKENS = 24000
CONTEXTS = 2
CONTEXT_RADIUS = 30
EXAMPLES = 60
PARTICLES = sorted("""은 는 이 가 을 를 의 에 에서 에게 에게서 께 께서 으로 로 과 와 도 만 까지 부터 이나 나 이랑 랑 하고 처럼
보다 마저 조차 이라 라 이라고 라고 이란 란 이며 며 이다 였다 이었다 이야 야 아 이여 님 씨 들 들은 들이 들을 들의 들에게
에서는 에서도 에는 에도 으로는 로는 으로도 로도 과는 와는 이라는 라는 인 이었던 였던""".split(), key=len, reverse=True)
WORD = re.compile(r"[가-힣A-Za-z0-9·]+")
PAIR = re.compile(r"[가-힣]{2,6} [가-힣]{2,6}")
SHORT_KEY = re.compile(r"[가-힣]{2,3}")  # Keys this short are checked for starting mid-word.
MID_WORD_MIN_COUNT = 3
# Skipped before sending only at a high share; a lower share (trap_report's 0.4) is still sent and flagged.
MID_WORD_RATIO = 0.7

SETTLE_PROMPT = """You settle the glossary of a novel before it is translated into English.
You get candidates from a local frequency scan of the source: a word stem (particles stripped; sometimes still a broken
piece) or a two-word phrase, its count, and short contexts. Most are ordinary vocabulary: skip them.
Keep only terms that need one stable English rendering across the whole novel: characters and their aliases and
nicknames, forms of address and titles, organizations, factions, places, ranks, named techniques, skills, spells,
artifacts and weapons, species, invented setting terms and recurring specialized terms. Skip ordinary words, generic
objects and actions, generic tactics and one-off descriptions. If a stem is a broken piece of a name, give the full name
as it appears in a context. Keep a bare given name or nickname only if the novel uses it alone.
Follow the project rules and match the style of the existing glossary entries. Capitalize every word of formal named
techniques, skills and spells (Black Tiger Seizes The Heart).
Answer with JSON only: {"terms": [{"korean": "exact source form", "english": "...", "category": "one of the listed",
"notes": "under 15 words"}]}"""


def stem(word):
    for particle in PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def _forms(text):
    """(form, start) for each word stem and two-word phrase of the text, without 1-syllable forms."""
    words = list(WORD.finditer(text))
    for index, match in enumerate(words):
        forms = [stem(match.group())]
        if index + 1 < len(words) and text[match.end():words[index + 1].start()] == " ":
            pair = match.group() + " " + stem(words[index + 1].group())
            if PAIR.fullmatch(pair):
                forms.append(pair)
        for form in forms:
            if len(form) >= 2 and hangul_length(form) != 1:
                yield form, match.start()


def mid_word_counts(chapters):
    """For every 2- and 3-syllable Korean string: (occurrences, occurrences that start mid-word, right
    after another Hangul syllable), from one pass over the distinct words. A compound's first part
    (조선 in 조선군) or last part (총관 in 대총관이, ending where the stem ends) is not mid-word."""
    words = Counter(word for chapter in chapters for word in WORD.findall(chapter["text"]))
    total, inside = Counter(), Counter()
    for word, count in words.items():
        stem_end = len(stem(word))
        for start in range(len(word) - 1):
            for width in (2, 3):
                piece = word[start:start + width]
                if len(piece) < width or not SHORT_KEY.fullmatch(piece):
                    continue
                total[piece] += count
                if start and HANGUL.match(word[start - 1]) and start + width != stem_end:
                    inside[piece] += count
    return total, inside


def scan(chapters, entries, *, min_count=8, skipped=None):
    """Recurring stems and two-word phrases the glossary lacks, most frequent first. No API calls.

    A short candidate that nearly always starts mid-word is left out (and appended to `skipped`).
    Each candidate gets the context of its first occurrence and of its middle one.
    """
    counts, first = Counter(), {}
    for chapter in chapters:
        for form, _ in _forms(chapter["text"]):
            counts[form] += 1
            first.setdefault(form, chapter["chapter"])
    total, inside = mid_word_counts(chapters)
    wanted = {}
    for term, count in counts.items():
        if count < min_count or term in entries:
            continue
        if (SHORT_KEY.fullmatch(term) and total[term] >= MID_WORD_MIN_COUNT
                and inside[term] / total[term] >= MID_WORD_RATIO):
            if skipped is not None:
                skipped.append({"term": term, "count": count, "occurrences": total[term], "mid_word": inside[term]})
            continue
        wanted[term] = {part * count // CONTEXTS for part in range(CONTEXTS)}
    seen, contexts = Counter(), defaultdict(list)
    for chapter in chapters:
        text = chapter["text"]
        for form, start in _forms(text):
            if form in wanted:
                if seen[form] in wanted[form]:
                    contexts[form].append(
                        text[max(0, start - CONTEXT_RADIUS): start + len(form) + CONTEXT_RADIUS].replace("\n", " "))
                seen[form] += 1
    if skipped is not None:
        skipped.sort(key=lambda item: -item["count"])
    return [{"term": term, "count": count, "first": first[term], "contexts": contexts[term]}
            for term, count in counts.most_common() if term in wanted]


def settle_messages(candidates, rules, categories, examples):
    lines = [f"{index}. {item['term']} ({item['count']}x, first ch. {item['first']}): " + " | ".join(item["contexts"])
             for index, item in enumerate(candidates, 1)]
    user = ("PROJECT RULES:\n" + (rules.strip() or "(none)") + "\n\nCATEGORIES: " + ", ".join(sorted(categories))
            + "\n\nEXISTING GLOSSARY ENTRIES (style examples):\n" + ("\n".join(examples) or "(none yet)")
            + "\n\nCANDIDATES:\n" + "\n".join(lines))
    return [{"role": "system", "content": SETTLE_PROMPT}, {"role": "user", "content": user}]


def check_proposals(raw_terms, source_text, entries, profile, fallback="specialized recurring term"):
    """Local checks on DeepSeek's terms: (kept, dropped)."""
    kept, dropped = {}, []
    for item in raw_terms:
        if not isinstance(item, dict):
            continue
        korean = str(item.get("korean") or "").strip()
        english = str(item.get("english") or "").strip()
        category = str(item.get("category") or "").strip().lower()
        if not korean or not english:
            dropped.append({**item, "dropped": "no source form or English"})
            continue
        if korean not in source_text:
            dropped.append({**item, "dropped": "not in the source"})
            continue
        if korean in entries:
            dropped.append({**item, "dropped": "already in the glossary"})
            continue
        if hangul_length(korean) == 1:
            dropped.append({**item, "dropped": "a 1-syllable key matches inside too many words"})
            continue
        if korean in kept:
            if kept[korean]["english"] != english:
                kept[korean].setdefault("alternatives", []).append(english)
            continue
        if category not in profile.allowed_categories:
            category = fallback if fallback in profile.allowed_categories else sorted(profile.allowed_categories)[0]
        if profile.requires_formal_capitalization(category):
            english = format_technique_name(english)
        kept[korean] = {"english": english, "category": category, "notes": str(item.get("notes") or "")[:200]}
    return kept, dropped


def _run_dir(project, run):
    return project.root / "output" / "glossary_settle" / run


def propose(project, client, budget, *, run, profile, min_count=8, parallel=20, reasoning="high",
            sleep=time.sleep, clock=lambda: datetime.now(timezone.utc), log=print):
    """Scan, ask DeepSeek, check locally, and write proposals. The glossary is not touched."""
    run_dir = _run_dir(project, run)
    journal_dir = run_dir / "journal"
    (run_dir / "chunks").mkdir(parents=True, exist_ok=True)
    released = release_ambiguous_requests(journal_dir, next_chapter=1, save_json=atomic_write_json, max_resends=1)
    if released["released"]:
        log(f"Sending once more {len(released['released'])} request(s) that got no recorded answer.")
    chapters = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8")).get("entries", {})
    rules = project.translation_addendum_file.read_text(encoding="utf-8") if project.translation_addendum_file.is_file() else ""
    examples = [f"{key} = {entry.get('english')} ({entry.get('category')})" for key, entry in list(entries.items())[:EXAMPLES]
                if entry.get("english")]
    skipped = []
    candidates = scan(chapters, entries, min_count=min_count, skipped=skipped)
    if skipped:
        log(f"Skipped {len(skipped)} candidate(s) that nearly always start mid-word.")
    chunks = [candidates[start:start + CHUNK] for start in range(0, len(candidates), CHUNK)]
    log(f"{len(candidates)} candidates in {len(chunks)} request(s).")
    sender = Sender(project, client, budget, journal_dir, reasoning=reasoning, sleep=sleep, clock=clock, log=log)
    answers, failed = {}, []

    def split(index, chunk, key, cost):
        """Send an all-reasoning chunk once more as two halves, each its own journaled request."""
        log(f"Request {index + 1}/{len(chunks)}: the answer was all reasoning and no terms; "
            f"sending its {len(chunk)} candidates again as two halves.")
        middle, terms, left = len(chunk) // 2, [], 0
        for number, part in enumerate((chunk[:middle], chunk[middle:])):
            messages = settle_messages(part, rules, profile.allowed_categories, examples)
            part_key = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()
            answer = sender.ask("glossary_settle_split", 2 * index + 1 + number, messages, SETTLE_TOKENS,
                                source_sha256=part_key, english_sha256=part_key)
            cost += answer["cost_usd"]
            try:
                if answer["finish_reason"] != "stop":
                    raise ValueError(answer["finish_reason"])
                terms += parse_json(answer["content"]).get("terms") or []
            except (ValueError, json.JSONDecodeError):
                left += len(part)
        problem = (f"{left} of {len(chunk)} candidates got no answer, also after splitting the request in two"
                   if left else None)
        if left == len(chunk):
            return index, None, problem
        atomic_write_json(run_dir / "chunks" / f"{index + 1:04d}.json",
                          {"key": key, "terms": terms, "not_done": problem, "cost_usd": cost})
        log(f"Request {index + 1}/{len(chunks)}: {len(terms)} terms from its halves (${cost:.4f})")
        return index, terms, problem

    def one(index):
        chunk = chunks[index]
        messages = settle_messages(chunk, rules, profile.allowed_categories, examples)
        key = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()
        path = run_dir / "chunks" / f"{index + 1:04d}.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("key") == key:
                return index, saved["terms"], saved.get("not_done")
        try:
            answer = sender.ask("glossary_settle", index + 1, messages, SETTLE_TOKENS,
                                source_sha256=key, english_sha256=key)
            if reasoning_only(answer) and len(chunk) > 1:
                return split(index, chunk, key, answer["cost_usd"])
        except PaidRequestUncertainError:
            return index, None, "a request got no recorded answer; rerun to send it once more"
        except PaidRequestJournalError as exc:
            return index, None, f"journal: {str(exc)[:160]}"
        except ReviewStop as exc:
            return index, None, f"provider refused: {exc}"
        except Exception as exc:  # Budget refusals and anything else leave the chunk for a rerun.
            return index, None, f"{type(exc).__name__}: {str(exc)[:160]}"
        if answer["finish_reason"] != "stop":
            return index, None, f"no complete answer ({answer['finish_reason']}); rerun under a new --run"
        try:
            terms = parse_json(answer["content"]).get("terms") or []
        except (ValueError, json.JSONDecodeError) as exc:
            return index, None, f"unreadable answer: {str(exc)[:120]}"
        atomic_write_json(path, {"key": key, "terms": terms, "cost_usd": answer["cost_usd"]})
        log(f"Request {index + 1}/{len(chunks)}: {len(terms)} terms (${answer['cost_usd']:.4f})")
        return index, terms, None

    waiting = list(range(len(chunks)))
    while waiting:
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            results = list(pool.map(one, waiting))
        retry = [i for i, terms, problem in results if terms is None and str(problem).startswith(ALLOWANCE_REFUSAL)]
        for index, terms, problem in results:
            if terms is not None:
                answers[index] = terms
                if problem:  # Half of the chunk got no answer.
                    failed.append((index + 1, problem))
            elif index not in retry:
                failed.append((index + 1, problem))
        if retry and len(retry) < len(waiting):
            waiting = retry
        else:
            failed += [(i + 1, p) for i, t, p in results if i in retry]
            waiting = []
    raw_terms = [term for index in sorted(answers) for term in answers[index]]
    source_text = "\n".join(chapter["text"] for chapter in chapters)
    kept, dropped = check_proposals(raw_terms, source_text, entries, profile)
    together = {**entries, **{key: {**value, "status": "provisional"} for key, value in kept.items()}}
    traps = {row["korean"]: row for row in trap_report(together, chapters) if row["korean"] in kept}
    for key, row in traps.items():
        kept[key]["trap"] = {"occurrences": row["occurrences"], "inside_other_words": row["inside_other_words"],
                             "examples": row["examples"]}
    atomic_write_json(run_dir / "proposals.json", {"run": run, "terms": kept, "dropped": dropped})
    summary = dict(novel=project.config.id, run=run, candidates=len(candidates), requests=len(chunks),
                   skipped_mid_word=len(skipped),
                   proposed=len(kept), traps=len(traps), dropped=len(dropped), calls=sender.calls,
                   cost_usd=round(sender.cost_usd, 6), failed_requests=sorted(failed),
                   proposals_file=str(run_dir / "proposals.json"), report=str(run_dir / "report.md"))
    write_report(run_dir, kept, dropped, summary, skipped)
    atomic_write_json(run_dir / "summary.json", summary)
    return summary


def write_report(run_dir, kept, dropped, summary, skipped=()):
    lines = [f"# Glossary settling: {summary['novel']} run {summary['run']}", "",
             f"- {summary['candidates']} candidates in {summary['requests']} requests; {summary['calls']} new, "
             f"${summary['cost_usd']:.4f} this run (replays cost nothing)",
             f"- Proposed {summary['proposed']} terms ({summary['traps']} flagged as traps), dropped {summary['dropped']}", ""]
    if summary["failed_requests"]:
        lines += ["## Requests without an answer", ""] + [f"- {i}: {why}" for i, why in summary["failed_requests"]] + [""]
    lines += ["## Proposed", ""]
    for key, entry in sorted(kept.items(), key=lambda pair: (pair[1]["category"], pair[0])):
        flag = " **trap: mostly inside other words**" if entry.get("trap") else ""
        alternatives = f" (also proposed: {', '.join(entry['alternatives'])})" if entry.get("alternatives") else ""
        lines.append(f"- {key} = {entry['english']} [{entry['category']}]{alternatives}{flag}")
    lines += ["", "## Dropped by the local checks", ""]
    lines += [f"- {item.get('korean')} = {item.get('english')}: {item['dropped']}" for item in dropped] or ["- none"]
    lines += ["", "## Not sent: nearly always mid-word", ""]
    lines += [f"- {item['term']} ({item['count']}x): {item['mid_word']} of {item['occurrences']} "
              "occurrences start mid-word" for item in skipped] or ["- none"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def install(project, *, run, include_traps=False, skip=(), clock=lambda: datetime.now(timezone.utc)):
    """Add a run's proposals the glossary lacks, after backing it up. Returns what was added."""
    proposals = json.loads((_run_dir(project, run) / "proposals.json").read_text(encoding="utf-8"))["terms"]
    data = json.loads(project.glossary_file.read_text(encoding="utf-8"))
    entries = data.setdefault("entries", {})
    stamp = clock().strftime("%Y%m%dT%H%M%SZ")
    backup = project.root / "backup" / "glossary_settle" / f"glossary_{run}_{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project.glossary_file, backup)
    added, skipped = [], []
    for key, entry in proposals.items():
        if key in entries or key in skip or (entry.get("trap") and not include_traps):
            skipped.append(key)
            continue
        entries[key] = {"english": entry["english"], "category": entry["category"], "status": "provisional",
                        "notes": entry.get("notes", ""),
                        "source_provenance": {"origin": "manual", "verification": "explicit_manual",
                                              "note": f"settled before translation by glossary_settle run {run}, "
                                                      "approved for install"}}
        added.append(key)
    atomic_write_json(project.glossary_file, data)
    return {"added": len(added), "skipped": len(skipped), "backup": str(backup)}


def build_parser():
    parser = argparse.ArgumentParser(description="Settle glossary terms with DeepSeek before translating")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root")
    sub = parser.add_subparsers(dest="command", required=True)
    pro = sub.add_parser("propose", help="scan the source and ask DeepSeek; writes proposals only")
    pro.add_argument("--budget-usd", type=float, required=True, help="the most this run may spend")
    pro.add_argument("--run", default="settle", help="folder name under output/glossary_settle")
    pro.add_argument("--min-count", type=int, default=8, help="least occurrences for a candidate")
    pro.add_argument("--parallel", type=int, default=20)
    pro.add_argument("--reasoning", choices=("low", "high"), default="high")
    ins = sub.add_parser("install", help="add a run's approved proposals to the glossary (backs it up first)")
    ins.add_argument("--run", required=True)
    ins.add_argument("--include-traps", action="store_true", help="also add keys flagged as traps")
    ins.add_argument("--skip", action="append", default=[], help="a proposed Korean key to leave out")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from cli_text_streams import configure_cli_text_streams
    configure_cli_text_streams()
    from novel_project import load_novel_project
    project = load_novel_project(args.novel, repo_root=args.repo_root)
    if args.command == "install":
        print(json.dumps(install(project, run=args.run, include_traps=args.include_traps, skip=set(args.skip)),
                         indent=2, ensure_ascii=False))
        return 0
    if args.budget_usd <= 0:
        print("Give the most this run may spend with --budget-usd.")
        return 2
    from command_code_provider_credentials import command_code_api_key_scope
    from command_code_provider_transport import create_command_code_client
    from production_goat_runtime import GoatBudget
    from translation_profile import load_translation_profile
    profile = load_translation_profile(project.config.profile, project.repository_root)
    chapters = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8")).get("entries", {})
    requests = -(-len(scan(chapters, entries, min_count=args.min_count)) // CHUNK)
    # Each chunk may be sent once more as two halves; --budget-usd bounds the cost.
    budget = GoatBudget(3 * requests + 2, args.budget_usd)
    with command_code_api_key_scope() as key:
        client = create_command_code_client(api_key=key)
        summary = propose(project, client, budget, run=args.run, profile=profile, min_count=args.min_count,
                          parallel=args.parallel, reasoning=args.reasoning)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not summary["failed_requests"] else 1


if __name__ == "__main__":
    sys.exit(main())
