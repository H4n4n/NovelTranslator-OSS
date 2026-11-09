"""Review finished chapters against their source with DeepSeek on Command Code (docs/CHAPTER_REVIEW.md).

It reads committed chapters and never changes them. Findings, a report and a list in the
`--apply-corrections` format go under output/review/<run>/. Each chapter gets one request
that finds problems and, when it found any that pass the local checks, one that verifies
them. Every request goes through the paid request journal (replay or send once), reserves
its worst-case cost before it is sent, and waits for pause.flag and the novel's off-peak
setting. Rerunning the same command replays recorded answers and sends only what is missing.

    python chapter_review.py --novel <id> --chapters 11-50 --budget-usd 2
"""
import argparse
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from approved_corrections import correct_chapter
from chapter_text_checks import missing_dialogue_lines
from noveltranslator_core import api_permission_blocker, atomic_write_json
from production_paid_request_journal import (PaidRequestJournalError, PaidRequestUncertainError,
                                             paid_request_journal_path, release_ambiguous_requests,
                                             request_identity, run_journaled_paid_request)
from term_repair import _normalized, term_is_present

REVISION = 1  # A saved chapter result from an older revision is reviewed again.
MODEL = "deepseek/deepseek-v4.1-flash"
FIND_TOKENS = 40000
VERIFY_TOKENS = 24000
MAX_GLOSSARY_LINES = 150
WAIT_SECONDS = 30
SOURCE_SCRIPT = re.compile(r"[㐀-鿿가-힣]")
TOKEN_EDGE = "\"'“”‘’「」『』()（）[]<>《》〈〉.,!?…·:;—-~"

FIND_PROMPT = """You review a finished English translation of one chapter of a novel against its source.
Report only real problems a careful bilingual editor would fix:
- "meaning": the English says something the source does not (wrong subject or object, negation, number, a misread word);
- "omission": a source sentence or line of dialogue with no English;
- "rule": a break of the project rules below;
- "term": a name or term rendered against the glossary below, or against the Chinese characters the source gives.
Do not report style, word choice, register, or anything that is merely stiff. A chapter with nothing wrong gets no findings.
Paragraphs are numbered S1, S2… (source) and E1, E2… (English). For each problem give:
 "kind"; "s" and "e", the paragraph numbers; "source", the source words copied exactly;
 "english", the EXACT English words to replace, copied character for character from one E paragraph,
   4 to 40 words, and occurring once in the chapter (for an omission: the English sentence the missing text follows);
 "fix", the replacement for exactly those English words, fitting the sentence (for an omission: that sentence
   followed by the missing translation); "why", under 15 words.
Answer with JSON only: {"findings": [...]}"""

VERIFY_PROMPT = """You check proposed corrections to a translated chapter. For each item decide:
- "keep": the problem is real and the fix is right;
- "fix": the problem is real but the fix is wrong or awkward; give a better replacement for exactly the same English words;
- "drop": the English was already right, the item is a style preference, or you cannot tell.
Be strict: when in doubt, drop.
Answer with JSON only: {"verdicts": [{"id": 1, "verdict": "keep", "fix": "", "why": "under 15 words"}]}"""


class ReviewStop(RuntimeError):
    """The provider refused definitely; nothing was billed and the slot can be sent again."""
    definite_no_response = True


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_range(value):
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", value or "")
    if not match:
        raise ValueError("--chapters takes a chapter or a range such as 11-50")
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    if first < 1 or last < first:
        raise ValueError("--chapters range is empty")
    return first, last


def paragraphs(text):
    return [part.strip() for part in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if part.strip()]


def numbered(label, parts):
    return "\n\n".join(f"[{label}{index}] {part}" for index, part in enumerate(parts, 1))


def parse_json(content):
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("the answer is not a JSON object")
    return value


def chapter_glossary(entries, source):
    lines = [f"{term} = {entry.get('english')}" for term, entry in entries.items()
             if len(term) >= 2 and term in source and entry.get("english")]
    return sorted(lines, key=len)[:MAX_GLOSSARY_LINES]


def local_flags(source, english, entries):
    """Free checks: leftover source script, missing dialogue, paragraph gap, glossary terms not rendered."""
    flags = []
    left = sorted(set(SOURCE_SCRIPT.findall(english)))
    if left:
        flags.append({"kind": "source_script_left", "detail": "".join(left[:40])})
    missing = missing_dialogue_lines(source, english)
    if missing:
        flags.append({"kind": "possible_missing_dialogue", "detail": f"{missing} fewer dialogue lines than the source"})
    source_count, english_count = len(paragraphs(source)), len(paragraphs(english))
    if english_count < source_count * 0.9:
        flags.append({"kind": "paragraph_gap", "detail": f"{english_count} English paragraphs for {source_count} source"})
    folded = _normalized(english)
    absent = [f"{term} = {entry['english']}" for term, entry in entries.items()
              if len(term) >= 2 and term in source and entry.get("english") and not term_is_present(entry, folded)]
    if absent:
        flags.append({"kind": "glossary_term_not_rendered", "detail": "; ".join(absent[:20])})
    return flags


def finding_problem(item, source, english):
    """Why a finding cannot be used as an exact replacement, or None."""
    old, new = item.get("english"), item.get("fix")
    if not isinstance(old, str) or not isinstance(new, str) or not old.strip() or not new.strip():
        return "missing English or fix"
    if old.strip() == new.strip():
        return "fix is the same as the English"
    if english.replace("\r\n", "\n").count(old.replace("\r\n", "\n")) != 1:
        return "English not found exactly once in the chapter"
    tokens = [token.strip(TOKEN_EDGE) for token in str(item.get("source") or "").split()]
    if not any(len(token) >= 2 and token in source for token in tokens):
        return "source words not found in the chapter source"
    return None


def source_form(text, chapter_source, other_sources):
    """A source string from the finding that is in this chapter, preferably in no other chapter."""
    whole = (text or "").strip().strip(TOKEN_EDGE)
    if len(whole) >= 2 and whole in chapter_source and not any(whole in other for other in other_sources):
        return whole
    tokens = sorted({token.strip(TOKEN_EDGE) for token in (text or "").split()}, key=len, reverse=True)
    present = [token for token in tokens if len(token) >= 2 and token in chapter_source]
    for token in present:
        if not any(token in other for other in other_sources):
            return token
    compact = (text or "").strip()
    for width in range(6, 31, 2):
        for start in range(0, max(1, len(compact) - width + 1)):
            window = compact[start:start + width]
            if window in chapter_source and not any(window in other for other in other_sources):
                return window
    return present[0] if present else None


def file_form(raw, text):
    """The text as it appears in a chapter file, which may use CRLF line endings."""
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if "\r\n" in raw else text


def word_edges(raw, old, new):
    """Grow old (and new with it) to whole words, as the corrections tool matches whole words only."""
    start = raw.find(old)
    end = start + len(old)
    prefix = suffix = ""
    while start > 0 and (raw[start - 1].isalnum() or raw[start - 1] in "_-") and old[:1].isalnum():
        start -= 1
        prefix = raw[start] + prefix
    while end < len(raw) and (raw[end].isalnum() or raw[end] in "_-") and old[-1:].isalnum():
        suffix += raw[end]
        end += 1
    return prefix + old, prefix + new + suffix if prefix or suffix else new


def build_candidates(kept, sources, raws):
    """Turn kept findings into a dry-run-checked --apply-corrections list."""
    replacements, refused = [], []
    for item in kept:
        chapter = item["chapter"]
        raw = raws[chapter]
        old, new = file_form(raw, item["english"]), file_form(raw, item["fix"])
        if raw.count(old) != 1:
            refused.append({**item, "refused": "English not found exactly once in the chapter file"})
            continue
        old, new = word_edges(raw, old, new)
        others = [text for number, text in sources.items() if number != chapter]
        form = source_form(item.get("source"), sources[chapter], others)
        if form is None:
            refused.append({**item, "refused": "no source words to tie the fix to"})
            continue
        replacement = {"source": [form], "old": old, "new": new}
        _, changes = correct_chapter(sources[chapter], raw, [replacement])
        elsewhere = [number for number, text in raws.items() if number != chapter
                     and form in sources[number] and correct_chapter(sources[number], text, [replacement])[1]]
        if len(changes) != 1 or elsewhere:
            reason = f"would also change chapter {elsewhere[0]}" if elsewhere else "does not apply exactly once"
            refused.append({**item, "refused": reason})
            continue
        if any(other["old"] == old and other["new"] != new for other in replacements):
            refused.append({**item, "refused": "the same English has another fix"})
            continue
        replacements.append(replacement)
    return replacements, refused


class Sender:
    """One journaled, budgeted, paused-when-asked Command Code request per call."""

    def __init__(self, project, client, budget, journal_dir, *, reasoning, sleep=time.sleep,
                 clock=lambda: datetime.now(timezone.utc), log=print):
        from benchmark_longitudinal_capability_results import verified_reasoning_capability
        self.project, self.client, self.budget, self.journal_dir = project, client, budget, Path(journal_dir)
        self.capability = verified_reasoning_capability(MODEL)
        if self.capability is None or reasoning not in self.capability.levels:
            raise ValueError(f"reasoning {reasoning!r} is not verified for {MODEL}")
        self.reasoning, self.sleep, self.clock, self.log = reasoning, sleep, clock, log
        self.cost_usd = 0.0
        self.calls = 0
        self._lock = threading.Lock()
        self._waiting = set()

    def wait_for_permission(self):
        while True:
            blocker = api_permission_blocker(self.project.pause_file.exists(),
                                             self.project.config.off_peak_only, self.clock())
            if blocker is None:
                return
            with self._lock:
                if blocker not in self._waiting:
                    self._waiting.add(blocker)
                    self.log("Waiting: pause.flag is present." if blocker == "manual_pause"
                             else "Waiting for off-peak hours; no requests are sent meanwhile.")
            self.sleep(WAIT_SECONDS)

    def ask(self, kind, chapter, messages, max_tokens, *, source_sha256, english_sha256):
        from benchmark_longitudinal_provider import (build_provider_request, build_provider_request_identity,
                                                     normalize_provider_response)
        from command_code_provider_transport import account_command_code_usage, accept_command_code_response
        from production_goat_runtime import raw_response_record
        from provider_retry_policy import DefiniteProviderFailure, send_with_retries
        wire = build_provider_request(model=MODEL, messages=messages, max_output_tokens=max_tokens,
                                      reasoning_capability=self.capability, reasoning_level=self.reasoning)
        transport = build_provider_request_identity(request_body=wire)
        identity = request_identity(operation_kind=kind, novel_id=self.project.config.id, chapter=chapter,
                                    provider="command_code_provider", model=MODEL,
                                    profile_id=self.project.config.profile, source_sha256=source_sha256,
                                    request_payload=wire, translation_sha256=english_sha256)
        attempt = {}

        def prepare():
            self.wait_for_permission()
            attempt["request_started_utc"] = self.clock().astimezone(timezone.utc).isoformat()
            return dict(attempt)

        def send():
            def send_once():
                self.budget.begin(transport.request_sha256)
                return self.client.chat.completions.create(**wire)

            def log_retry(entry):
                self.log(f"Chapter {chapter} {kind}: provider {entry['action']} ({entry.get('status')}); retrying.")
            try:
                return send_with_retries(send_once, log=log_retry, before_retry=self.wait_for_permission,
                                         on_definite_failure=self.budget.retract)
            except DefiniteProviderFailure as exc:
                raise ReviewStop(str(exc)) from None
            except BaseException as exc:
                if getattr(exc, "definite_no_response", False) is not True:
                    self.budget.blocked = True
                raise

        @contextmanager
        def authorize():
            with self.budget.authorize(wire, attempt["request_started_utc"]):
                yield send

        result = run_journaled_paid_request(
            journal_path=paid_request_journal_path(self.journal_dir, kind, chapter), identity=identity,
            save_json=atomic_write_json, prepare_attempt=prepare, send_once=send, authorize_send=authorize,
            normalize_response=lambda raw, context: {"raw_response": raw_response_record(raw), **context})
        recorded = result["response"]
        normalized = normalize_provider_response(recorded["raw_response"], request_identity=transport)
        accepted = accept_command_code_response(transport, normalized)
        stats = account_command_code_usage(MODEL, accepted.usage, recorded["request_started_utc"])
        if result["api_called"]:
            self.budget.account(stats)
            with self._lock:
                self.cost_usd += stats["cost_usd"]
                self.calls += 1
        return {"content": accepted.content, "finish_reason": accepted.finish_reason,
                "cost_usd": stats["cost_usd"], "replayed": result["replayed"]}


def find_messages(source, english, rules, glossary):
    user = ("PROJECT RULES:\n" + (rules.strip() or "(none)") + "\n\nGLOSSARY (source = English):\n"
            + ("\n".join(glossary) or "(none)") + "\n\nSOURCE:\n" + numbered("S", paragraphs(source))
            + "\n\nENGLISH:\n" + numbered("E", paragraphs(english)))
    return [{"role": "system", "content": FIND_PROMPT}, {"role": "user", "content": user}]


def _around(parts, number):
    try:
        index = int(str(number).strip().lstrip("SEse")) - 1
    except ValueError:
        return ""
    return "\n".join(f"[{i + 1}] {parts[i]}" for i in range(max(0, index - 1), min(len(parts), index + 2)))


def verify_messages(items, source, english, rules):
    source_parts, english_parts = paragraphs(source), paragraphs(english)
    blocks = []
    for item in items:
        blocks.append(f"ITEM {item['id']} ({item.get('kind')}): {item.get('why', '')}\n"
                      f"SOURCE PARAGRAPHS:\n{_around(source_parts, item.get('s'))}\n"
                      f"ENGLISH PARAGRAPHS:\n{_around(english_parts, item.get('e'))}\n"
                      f"REPLACE: {item['english']}\nWITH: {item['fix']}")
    user = "PROJECT RULES:\n" + (rules.strip() or "(none)") + "\n\n" + "\n\n".join(blocks)
    return [{"role": "system", "content": VERIFY_PROMPT}, {"role": "user", "content": user}]


def review_chapter(sender, chapter, source, english, rules, entries, *, verify=True):
    """One chapter's findings, checked locally and verified. Returns the saved record."""
    keys = dict(source_sha256=sha256(source), english_sha256=sha256(english))
    record = {"revision": REVISION, "chapter": chapter, **keys, "local_flags": local_flags(source, english, entries),
              "kept": [], "dropped": [], "cost_usd": 0.0}
    answer = sender.ask("chapter_review", chapter, find_messages(source, english, rules,
                        chapter_glossary(entries, source)), FIND_TOKENS, **keys)
    record["cost_usd"] += answer["cost_usd"]
    if answer["finish_reason"] != "stop":
        record["status"] = f"no_answer_{answer['finish_reason']}"
        return record
    try:
        findings = parse_json(answer["content"]).get("findings") or []
    except (ValueError, json.JSONDecodeError) as exc:
        record["status"] = "unreadable_answer"
        record["error"] = str(exc)[:200]
        return record
    usable = []
    for index, item in enumerate(item for item in findings if isinstance(item, dict)):
        problem = finding_problem(item, source, english)
        item = {"id": index + 1, "chapter": chapter, **{k: item.get(k) for k in ("kind", "s", "e", "source", "english", "fix", "why")}}
        (record["dropped"].append({**item, "dropped": problem}) if problem else usable.append(item))
    if usable and verify:
        answer = sender.ask("chapter_review_verify", chapter, verify_messages(usable, source, english, rules),
                            VERIFY_TOKENS, **keys)
        record["cost_usd"] += answer["cost_usd"]
        verdicts = {}
        if answer["finish_reason"] == "stop":
            try:
                verdicts = {int(v.get("id")): v for v in parse_json(answer["content"]).get("verdicts") or []
                            if isinstance(v, dict) and str(v.get("id", "")).isdigit()}
            except (ValueError, json.JSONDecodeError):
                verdicts = {}
        for item in usable:
            verdict = verdicts.get(item["id"]) or {"verdict": "drop", "why": "no verdict"}
            choice = str(verdict.get("verdict", "")).lower()
            if choice == "fix" and isinstance(verdict.get("fix"), str) and verdict["fix"].strip():
                item = {**item, "fix": verdict["fix"].strip(), "verified": "fixed", "verify_why": verdict.get("why")}
            elif choice == "keep":
                item = {**item, "verified": "kept", "verify_why": verdict.get("why")}
            else:
                record["dropped"].append({**item, "dropped": "verifier: " + str(verdict.get("why") or "dropped")})
                continue
            problem = finding_problem(item, source, english)
            (record["dropped"].append({**item, "dropped": problem}) if problem else record["kept"].append(item))
    else:
        record["kept"] = [{**item, "verified": "not_verified"} for item in usable]
    record["status"] = "done"
    return record


RISK = {"omission": 0, "meaning": 1, "term": 2, "rule": 3}


def write_report(run_dir, records, candidates, refused, summary):
    lines = [f"# Chapter review: {summary['novel']} chapters {summary['chapters']}", "",
             f"- Reviewed: {summary['reviewed']} chapters, {summary['calls']} new requests, "
             f"${summary['cost_usd']:.4f} this run (replays cost nothing)",
             f"- Candidates: {len(candidates)} ready for `--apply-corrections`, {len(refused)} refused, "
             f"{sum(len(r['dropped']) for r in records)} findings dropped", ""]
    if summary.get("not_reviewed"):
        lines += ["## Not reviewed", ""] + [f"- Chapter {c}: {why}" for c, why in summary["not_reviewed"]] + [""]
    kept = sorted((item for record in records for item in record["kept"]),
                  key=lambda item: (RISK.get(item.get("kind"), 9), item["chapter"]))
    lines += ["## Fixes (riskiest first)", ""]
    refused_keys = {(item["chapter"], item["english"]) for item in refused}
    for item in kept:
        mark = " (refused: " + next(r["refused"] for r in refused if (r["chapter"], r["english"]) == (item["chapter"], item["english"])) + ")" \
            if (item["chapter"], item["english"]) in refused_keys else ""
        lines += [f"- **{item['chapter']} {item.get('kind')}**{mark}: {item.get('why') or ''}",
                  f"  - Source: {item.get('source')}", f"  - Old: {item['english']}", f"  - New: {item['fix']}"]
    lines += ["", "## Local flags (no request)", ""]
    lines += [f"- {r['chapter']} {flag['kind']}: {flag['detail']}" for r in records for flag in r["local_flags"]] or ["- none"]
    lines += ["", "## Dropped", ""]
    lines += [f"- {item['chapter']} {item.get('kind')}: {item.get('english')!r}: {item['dropped']}"
              for r in records for item in r["dropped"]] or ["- none"]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def review_novel(project, client, budget, *, first, last, run_name=None, parallel=32, reasoning="high",
                 verify=True, sleep=time.sleep, clock=lambda: datetime.now(timezone.utc), log=print):
    """Review committed chapters first..last; returns the run summary."""
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    last = min(last, progress["last_completed"])
    if last < first:
        raise ValueError(f"no committed chapters in that range (last committed: {progress['last_completed']})")
    run_dir = project.root / "output" / "review" / (run_name or f"{first:04d}_{last:04d}")
    journal_dir = run_dir / "journal"
    (run_dir / "chapters").mkdir(parents=True, exist_ok=True)
    released = release_ambiguous_requests(journal_dir, next_chapter=1, save_json=atomic_write_json, max_resends=1)
    if released["released"]:
        log(f"Sending once more {len(released['released'])} request(s) that got no recorded answer.")
    sources = {item["chapter"]: item["text"] for item in json.loads(project.chapters_file.read_text(encoding="utf-8"))}
    raws = {number: (project.chapter_dir / f"{number:04d}.md").read_bytes().decode("utf-8")
            for number in range(1, progress["last_completed"] + 1) if (project.chapter_dir / f"{number:04d}.md").is_file()}
    rules = project.translation_addendum_file.read_text(encoding="utf-8") if project.translation_addendum_file.is_file() else ""
    entries = json.loads(project.glossary_file.read_text(encoding="utf-8")).get("entries", {})
    sender = Sender(project, client, budget, journal_dir, reasoning=reasoning, sleep=sleep, clock=clock, log=log)
    records, not_reviewed = {}, []

    def one(chapter):
        path = run_dir / "chapters" / f"{chapter:04d}.json"
        english = raws[chapter]
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("revision") == REVISION and saved.get("english_sha256") == sha256(english):
                return chapter, saved, None
        try:
            record = review_chapter(sender, chapter, sources[chapter], english, rules, entries, verify=verify)
        except PaidRequestUncertainError:
            return chapter, None, "a request got no recorded answer; rerun to send it once more"
        except PaidRequestJournalError as exc:
            return chapter, None, f"journal: {str(exc)[:160]} (the chapter changed; review it under a new --run)"
        except ReviewStop as exc:
            return chapter, None, f"provider refused: {exc}"
        except Exception as exc:  # Budget refusals and anything else leave the chapter for a rerun.
            return chapter, None, f"{type(exc).__name__}: {str(exc)[:160]}"
        atomic_write_json(path, record)
        log(f"Chapter {chapter}: {len(record['kept'])} kept, {len(record['dropped'])} dropped "
            f"({record['status']}, ${record['cost_usd']:.4f})")
        return chapter, record, None

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for chapter, record, problem in pool.map(one, range(first, last + 1)):
            if record is not None:
                records[chapter] = record
            else:
                not_reviewed.append((chapter, problem))
    ordered = [records[number] for number in sorted(records)]
    kept = [item for record in ordered for item in record["kept"]]
    candidates, refused = build_candidates(kept, sources, raws)
    atomic_write_json(run_dir / f"candidates_{first:04d}_{last:04d}.json", {"replacements": candidates})
    summary = dict(novel=project.config.id, chapters=f"{first}-{last}", reviewed=len(ordered), calls=sender.calls,
                   cost_usd=round(sender.cost_usd, 6), candidates=len(candidates), refused=len(refused),
                   dropped=sum(len(r["dropped"]) for r in ordered), not_reviewed=not_reviewed,
                   report=str(run_dir / "report.md"), candidates_file=str(run_dir / f"candidates_{first:04d}_{last:04d}.json"))
    write_report(run_dir, ordered, candidates, refused, summary)
    atomic_write_json(run_dir / "summary.json", summary)
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description="Review finished chapters with DeepSeek on Command Code")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapters", required=True, help="a chapter or a range such as 11-50")
    parser.add_argument("--budget-usd", type=float, required=True, help="the most this run may spend")
    parser.add_argument("--parallel", type=int, default=32)
    parser.add_argument("--reasoning", choices=("low", "high"), default="high")
    parser.add_argument("--no-verify", action="store_true", help="skip the second request that checks findings")
    parser.add_argument("--run", help="name of the review folder under output/review (default: the range)")
    parser.add_argument("--repo-root")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.budget_usd <= 0:
        print("Give the most this review may spend with --budget-usd.")
        return 2
    first, last = parse_range(args.chapters)
    from command_code_provider_credentials import command_code_api_key_scope
    from command_code_provider_transport import create_command_code_client
    from novel_project import load_novel_project
    from production_goat_runtime import GoatBudget
    project = load_novel_project(args.novel, repo_root=args.repo_root)
    budget = GoatBudget(2 * (last - first + 1) + 2, args.budget_usd)
    with command_code_api_key_scope() as key:
        client = create_command_code_client(api_key=key)
        summary = review_novel(project, client, budget, first=first, last=last, run_name=args.run,
                               parallel=args.parallel, reasoning=args.reasoning, verify=not args.no_verify)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not summary["not_reviewed"] else 1


if __name__ == "__main__":
    sys.exit(main())
