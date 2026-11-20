"""Judge a finished chapter review's candidate fixes with DeepSeek before any is applied.

`chapter_review.py` finds problems and checks each once; its candidates still hold nitpicks and
fixes that make the English worse. This sends each chapter's candidates, with the source and
English around them, the glossary and the project rules, to one final judging request that
approves, rewords or rejects each. A rewording is checked locally to apply exactly once.

It reads the review folder and never changes a chapter. Under output/review/<run>/judge/ it
writes decisions.json, report.md and approved_<range>.json in the `--apply-corrections` format.
Every request goes through the paid request journal (replay or send once), reserves its
worst-case cost first, and waits for pause.flag and the novel's off-peak setting.

    python review_judge.py --novel <id> --run <review run> --budget-usd 1
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from approved_corrections import correct_chapter
from chapter_review import (ALLOWANCE_REFUSAL, ReviewStop, Sender, _around, chapter_glossary, file_form,
                            paragraphs, parse_json, reasoning_only, sha256, source_form, word_edges)
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (PaidRequestJournalError, PaidRequestUncertainError,
                                             release_ambiguous_requests)

JUDGE_TOKENS = 24000
DECISIONS = ("approve", "reword", "reject")

JUDGE_PROMPT = """You are the final editor deciding which proposed corrections to apply to one translated chapter of a novel.
An automatic reviewer found each item and a second check kept it, but many are still nitpicks or make the English worse.
Approve when the fix corrects a real error: wrong meaning; a dropped clause, sentence or line of dialogue; wrong person,
pronoun or gender; wrong speaker or subject; wrong number, date, time, unit or rank; a name or term that contradicts the
glossary; a garbled sentence; a break of the project rules.
Reject when: the English is already a fair rendering and the fix only rewords it or is more literal; the fix makes the
English stiffer, wordier or less natural; the fix contradicts the glossary or the project rules, including a glossary
note on case or form (an entry marked locked is settled; its note outranks the bare rendering); the reviewer misread the
source; the source has an obvious slip the English already corrected; the dropped detail is trivial and already implied.
Items marked mechanical are automatic term swaps or dialogue splits: judge them too, and reject any that make the prose
awkward or use a term in the wrong sense.
Reword when the error is real but the proposed replacement is clumsy or wrong: give your own "new". It replaces exactly
the OLD text, so keep everything in OLD that should stay; write fluent English in the chapter's voice, with curly quotes.
Be strict: approve only what a careful editor would really change, and expect to reject a good share.
Answer with JSON only: {"decisions": [{"id": 1, "decision": "approve", "new": "", "reason": "under 15 words"}]}"""


def _replacement(item, raws, sources):
    """The `--apply-corrections` entry chapter_review built for one kept finding, or None."""
    raw = raws.get(item["chapter"])
    if raw is None or not item.get("english") or not item.get("fix"):
        return None
    old, new = word_edges(raw, file_form(raw, item["english"]), file_form(raw, item["fix"]))
    others = [text for number, text in sources.items() if number != item["chapter"]]
    form = source_form(item.get("source"), sources[item["chapter"]], others)
    return {"source": [form], "old": old, "new": new} if form else None


def pair_items(run_dir, raws, sources):
    """Each candidate of the review run with the finding it came from, in candidate order."""
    label = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["chapters"].replace("-", "_")
    first, last = (int(part) for part in label.split("_"))
    label = f"{first:04d}_{last:04d}"
    candidates = json.loads((run_dir / f"candidates_{label}.json").read_text(encoding="utf-8"))["replacements"]
    mechanical_file = run_dir / f"candidates_{label}_mechanical.json"
    mechanical = ({(c["old"], c["new"]) for c in json.loads(mechanical_file.read_text(encoding="utf-8"))["replacements"]}
                  if mechanical_file.is_file() else set())
    findings = {}
    for path in sorted((run_dir / "chapters").glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("kept", []):
            replacement = _replacement(item, raws, sources)
            if replacement is not None:
                findings.setdefault((replacement["old"], replacement["new"]), item)
    items = []
    for candidate in candidates:
        finding = findings.get((candidate["old"], candidate["new"]))
        if finding is None:
            continue  # Cannot happen for a run's own candidates; such an entry is simply not judged.
        items.append({"candidate": candidate, "finding": finding,
                      "mechanical": (candidate["old"], candidate["new"]) in mechanical})
    return label, items


def judge_messages(items, source, english, rules, glossary):
    source_parts, english_parts = paragraphs(source), paragraphs(english)
    blocks = []
    for number, item in enumerate(items, 1):
        finding, candidate = item["finding"], item["candidate"]
        blocks.append(
            f"ITEM {number} ({finding.get('kind')}{', mechanical' if item['mechanical'] else ''}): "
            f"reviewer: {finding.get('why') or ''}; check: {finding.get('verify_why') or ''}\n"
            f"SOURCE PARAGRAPHS:\n{_around(source_parts, finding.get('s'))}\n"
            f"ENGLISH PARAGRAPHS:\n{_around(english_parts, finding.get('e'))}\n"
            f"OLD: {candidate['old']}\nNEW: {candidate['new']}")
    user = ("PROJECT RULES:\n" + (rules.strip() or "(none)") + "\n\nGLOSSARY:\n" + ("\n".join(glossary) or "(none)")
            + "\n\n" + "\n\n".join(blocks))
    return [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}]


def _applies_once(replacement, chapter, sources, raws):
    """True when the entry changes exactly one place, in its own chapter only."""
    _, changes = correct_chapter(sources[chapter], raws[chapter], [replacement])
    if len(changes) != 1:
        return False
    return not any(number != chapter and correct_chapter(sources[number], text, [replacement])[1]
                   for number, text in raws.items() if number in sources)


def decide(items, answer, chapter, sources, raws):
    """Apply one chapter's answer to its items; anything unreadable or missing is rejected."""
    verdicts = {}
    if answer["finish_reason"] == "stop":
        try:
            verdicts = {int(v["id"]): v for v in parse_json(answer["content"]).get("decisions") or []
                        if isinstance(v, dict) and str(v.get("id", "")).isdigit()}
        except (ValueError, json.JSONDecodeError):
            verdicts = {}
    decided = []
    for number, item in enumerate(items, 1):
        verdict = verdicts.get(number) or {}
        choice = str(verdict.get("decision", "")).strip().lower()
        reason = str(verdict.get("reason") or "")[:200]
        candidate = item["candidate"]
        entry = {"chapter": chapter, "kind": item["finding"].get("kind"), "mechanical": item["mechanical"],
                 "source": candidate["source"], "old": candidate["old"], "proposed": candidate["new"],
                 "decision": choice if choice in DECISIONS else "reject", "reason": reason or "no decision"}
        if choice == "reword":
            new = str(verdict.get("new") or "").strip()
            replacement = {**candidate, "new": file_form(raws[chapter], new)} if new else None
            if replacement is None or replacement["new"] == candidate["old"] \
                    or not _applies_once(replacement, chapter, sources, raws):
                entry.update(decision="reject", reason="rewording did not apply exactly once: " + reason)
            else:
                entry["new"] = replacement["new"]
        elif choice == "approve":
            entry["new"] = candidate["new"]
        decided.append(entry)
    return decided


def write_report(judge_dir, decided, summary):
    counts = {choice: sum(1 for item in decided if item["decision"] == choice) for choice in DECISIONS}
    lines = [f"# Review judging: {summary['novel']} run {summary['run']}", "",
             f"- Judged: {len(decided)} candidates in {summary['chapters_judged']} chapters, "
             f"{summary['calls']} new requests, ${summary['cost_usd']:.4f} this run (replays cost nothing)",
             f"- Approved {counts['approve']}, reworded {counts['reword']}, rejected {counts['reject']}", ""]
    if summary["not_judged"]:
        lines += ["## Not judged", ""] + [f"- Chapter {c}: {why}" for c, why in summary["not_judged"]] + [""]
    for title, choice in (("Approved", "approve"), ("Reworded", "reword"), ("Rejected", "reject")):
        lines += [f"## {title}", ""]
        for item in (i for i in decided if i["decision"] == choice):
            lines += [f"- **{item['chapter']} {item['kind']}**: {item['reason']}", f"  - Old: {item['old']}",
                      f"  - Proposed: {item['proposed']}"] + ([f"  - New: {item['new']}"] if choice == "reword" else [])
        lines.append("")
    (judge_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def judge_review(project, client, budget, *, run, parallel=50, reasoning="high", sleep=time.sleep,
                 clock=lambda: datetime.now(timezone.utc), log=print):
    """Judge every candidate of review run `run`; returns the summary."""
    run_dir = project.root / "output" / "review" / run
    if not (run_dir / "summary.json").is_file():
        raise ValueError(f"no finished review run {run!r} under {run_dir.parent}")
    judge_dir = run_dir / "judge"
    journal_dir = judge_dir / "journal"
    (judge_dir / "chapters").mkdir(parents=True, exist_ok=True)
    released = release_ambiguous_requests(journal_dir, next_chapter=1, save_json=atomic_write_json, max_resends=1)
    if released["released"]:
        log(f"Sending once more {len(released['released'])} request(s) that got no recorded answer.")
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    sources = {item["chapter"]: item["text"] for item in json.loads(project.chapters_file.read_text(encoding="utf-8"))}
    raws = {number: (project.chapter_dir / f"{number:04d}.md").read_bytes().decode("utf-8")
            for number in range(1, progress["last_completed"] + 1) if (project.chapter_dir / f"{number:04d}.md").is_file()}
    rules = project.translation_addendum_file.read_text(encoding="utf-8") if project.translation_addendum_file.is_file() else ""
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8")).get("entries", {})
    label, items = pair_items(run_dir, raws, sources)
    by_chapter = {}
    for item in items:
        by_chapter.setdefault(item["finding"]["chapter"], []).append(item)
    sender = Sender(project, client, budget, journal_dir, reasoning=reasoning, sleep=sleep, clock=clock, log=log)
    decided, not_judged = {}, []

    def one(chapter):
        chapter_items, english = by_chapter[chapter], raws[chapter]
        key = sha256(english + json.dumps([i["candidate"] for i in chapter_items], ensure_ascii=False, sort_keys=True))
        path = judge_dir / "chapters" / f"{chapter:04d}.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("key") == key:
                return chapter, saved["decided"], saved.get("not_judged")
        glossary = chapter_glossary(entries, sources[chapter])

        def ask(kind, part):
            return sender.ask(kind, chapter, judge_messages(part, sources[chapter], english, rules, glossary),
                              JUDGE_TOKENS, source_sha256=sha256(sources[chapter]), english_sha256=key)
        try:
            answer = ask("review_judge", chapter_items)
            cost, result, left = answer["cost_usd"], [], []
            if not reasoning_only(answer):
                result = decide(chapter_items, answer, chapter, sources, raws)
            elif len(chapter_items) < 2:
                left = chapter_items
            else:
                # All reasoning and no decisions: judge each half once in its own journaled request.
                log(f"Chapter {chapter}: the judging answer was all reasoning and no decisions; "
                    f"sending its {len(chapter_items)} items again in two halves.")
                middle = len(chapter_items) // 2
                for number, part in enumerate((chapter_items[:middle], chapter_items[middle:]), 1):
                    half = ask(f"review_judge_half{number}", part)
                    cost += half["cost_usd"]
                    if reasoning_only(half):
                        left += part
                    else:
                        result += decide(part, half, chapter, sources, raws)
        except PaidRequestUncertainError:
            return chapter, None, "a request got no recorded answer; rerun to send it once more"
        except PaidRequestJournalError as exc:
            return chapter, None, f"journal: {str(exc)[:160]}"
        except ReviewStop as exc:
            return chapter, None, f"provider refused: {exc}"
        except Exception as exc:  # Budget refusals and anything else leave the chapter for a rerun.
            return chapter, None, f"{type(exc).__name__}: {str(exc)[:160]}"
        problem = (f"{len(left)} of {len(chapter_items)} item(s) not judged: the answers were all reasoning "
                   "and no decisions, also after splitting" if left else None)
        if not result:
            return chapter, None, problem
        atomic_write_json(path, {"chapter": chapter, "key": key, "decided": result, "not_judged": problem,
                                 "cost_usd": cost})
        log(f"Chapter {chapter}: " + ", ".join(f"{sum(1 for r in result if r['decision'] == c)} {c}" for c in DECISIONS))
        return chapter, result, problem

    waiting = sorted(by_chapter)
    while waiting:
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            results = list(pool.map(one, waiting))
        retry = [c for c, result, problem in results if result is None and str(problem).startswith(ALLOWANCE_REFUSAL)]
        for chapter, result, problem in results:
            if result is not None:
                decided[chapter] = result
                if problem:  # Some of its items could not be judged.
                    not_judged.append((chapter, problem))
            elif chapter not in retry:
                not_judged.append((chapter, problem))
        if retry and len(retry) < len(waiting):
            log(f"{len(retry)} chapter(s) waited for budget room; sending them now.")
            waiting = retry
        else:
            not_judged += [(c, p) for c, r, p in results if c in retry]
            waiting = []
    ordered = [entry for chapter in sorted(decided) for entry in decided[chapter]]
    approved = [{"source": e["source"], "old": e["old"], "new": e["new"]} for e in ordered if "new" in e]
    atomic_write_json(judge_dir / "decisions.json", ordered)
    atomic_write_json(judge_dir / f"approved_{label}.json", {"replacements": approved})
    summary = dict(novel=project.config.id, run=run, judged=len(ordered), chapters_judged=len(decided),
                   approved=sum(e["decision"] == "approve" for e in ordered),
                   reworded=sum(e["decision"] == "reword" for e in ordered),
                   rejected=sum(e["decision"] == "reject" for e in ordered),
                   calls=sender.calls, cost_usd=round(sender.cost_usd, 6), not_judged=sorted(not_judged),
                   approved_file=str(judge_dir / f"approved_{label}.json"), report=str(judge_dir / "report.md"))
    write_report(judge_dir, ordered, summary)
    atomic_write_json(judge_dir / "summary.json", summary)
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description="Judge a chapter review's candidate fixes with DeepSeek")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--run", required=True, help="the review folder under output/review")
    parser.add_argument("--budget-usd", type=float, required=True, help="the most this run may spend")
    parser.add_argument("--parallel", type=int, default=50)
    parser.add_argument("--reasoning", choices=("low", "high"), default="high")
    parser.add_argument("--repo-root")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from cli_text_streams import configure_cli_text_streams
    configure_cli_text_streams()
    if args.budget_usd <= 0:
        print("Give the most this judging may spend with --budget-usd.")
        return 2
    from command_code_provider_credentials import command_code_api_key_scope
    from command_code_provider_transport import create_command_code_client
    from novel_project import load_novel_project
    from production_goat_runtime import GoatBudget
    project = load_novel_project(args.novel, repo_root=args.repo_root)
    chapters = len(list((project.root / "output" / "review" / args.run / "chapters").glob("*.json")))
    # One request per chapter, or three when an all-reasoning answer is split; --budget-usd bounds the cost.
    budget = GoatBudget(3 * chapters + 2, args.budget_usd)
    with command_code_api_key_scope() as key:
        client = create_command_code_client(api_key=key)
        summary = judge_review(project, client, budget, run=args.run, parallel=args.parallel, reasoning=args.reasoning)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not summary["not_judged"] else 1


if __name__ == "__main__":
    sys.exit(main())
