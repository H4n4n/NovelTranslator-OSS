"""Paid repair of finished chapters where a glossary term's agreed English is missing.

The source shows which terms a chapter should contain. Where the agreed English is
absent, the model sees the source passage and the English chapter, and either gives a
small exact edit or says the chapter is fine as it is. Exact replacement can't handle
plurals, unknown misspellings or phrases another term also claims; this can.

Each answer is saved before it is applied, so a rerun never pays for a chapter twice.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from noveltranslator_core import _claim_longest_non_overlapping_occurrences, _glossary_source_forms, atomic_write_json
from production_run_lock import production_run_lock
from review_queue import add_item

REPAIR_DIR = "term_repairs"
REVISION = 4  # A saved answer from an older revision is asked again.
# Their words are too ordinary to repair reliably: "in a daze" isn't always the Stun effect.
ORDINARY_WORD_CATEGORIES = frozenset({"status effect", "rank", "stat"})
PERSON_CATEGORIES = frozenset({"character", "alias", "historical person"})
NAME_CATEGORIES = frozenset({"character", "alias", "faction", "title", "noble title", "organization", "location", "clan", "sect"})
MAX_OLD_CHARS = 400
MAX_PASSAGES_PER_TERM = 2
MAX_PASSAGE_CHARS = 500
MAX_COMPLETION_TOKENS = 12000
SOURCE_SCRIPT = re.compile(r"[㐀-鿿가-힣぀-ヿ]")
SYSTEM_PROMPT = """You check glossary terms in one finished chapter of a translated novel.

Each item gives a source term, its agreed English, and the source passages where it appears. The agreed English doesn't appear in the English chapter. Decide for each item:

- If the chapter renders the term another way that should use the agreed English, such as a plural, a misspelling or a different name for the same thing, give a fix.
- If the source word is being used in its ordinary sense there and not as this term, or the English is right as written, such as a pronoun, a nickname the source really uses, or a natural paraphrase, leave it alone. Text that only happens to contain the source term's letters inside a longer word is not the term.

A fix changes as little as possible. "old" is an exact, unique snippet copied character for character from the English chapter. It holds only as many words as it takes to be unique, at most one sentence. "new" is the same snippet with only the term changed. Keep the snippet's own singular or plural, even if the agreed English is the other one: a single Trait stays "Trait". The sentence must still mean the same and read naturally. If it wouldn't, leave it alone. If the source passage uses only part of a person's name, use only the matching part of the agreed name. Change nothing else, and never rewrite for style.

Reply with JSON only: {"fixes": [{"term": "...", "old": "...", "new": "..."}], "fine": [{"term": "...", "why": "a few words"}]}. Every item goes in one of the two lists."""


def repair_dir(project):
    return Path(project.root) / "output" / REPAIR_DIR


def _normalized(text):
    return text.replace("’", "'").replace("‘", "'").casefold()


def accepted_forms(entry):
    return [re.sub(r"\s*\([^)]*\)", "", entry["english"]).strip(),
            *(entry.get("accepted_inflections") or []), *(entry.get("repair_aliases") or [])]


def _singular(word):
    if word.endswith("ies") and len(word) > 4:
        return word[:-3]  # Authorities, Authority: compare the shared stem.
    if word.endswith("y") and len(word) > 3:
        return word[:-1]
    if word.endswith("men"):
        return word[:-2]
    if word.endswith("man"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _name_word(form):
    """The word that identifies a name, like Gyreun in "Count Gyreun family"."""
    words = re.findall(r"[^\W\d_]+", form)
    capitals = [word for word in words[1:] if word[0].isupper()] or words
    return max(capitals, key=len) if capitals else ""


def term_is_present(entry, folded):
    for form in accepted_forms(entry):
        normal = _normalized(form)
        if normal in folded or " ".join(_singular(word) for word in normal.split()) in folded:
            return True
        if normal.rstrip("s") and normal.rstrip("s") in folded:
            return True
        words = (re.findall(r"[^\W\d_]+", form) if entry.get("category") in PERSON_CATEGORIES
                 else [_name_word(form)] if entry.get("category") in NAME_CATEGORIES else [])
        if any(len(word) >= 3 and re.search(r"(?<!\w)" + re.escape(_normalized(word)) + r"(?!\w)", folded) for word in words):
            return True
    return False


def repairable_terms(entries):
    return {key: value for key, value in entries.items() if value.get("english")
            and value.get("status") != "contextual" and not value.get("alias_of")
            and value.get("category") not in ORDINARY_WORD_CATEGORIES}


def find_missing_terms(source, english, terms):
    """Terms this chapter's source owns, with none of their agreed forms in the English."""
    claims = _claim_longest_non_overlapping_occurrences(source, [
        (key, list(_glossary_source_forms(key, value.get("category", "")))) for key, value in terms.items()])
    folded = _normalized(english)
    return [term for term, anchors in claims.items() if anchors and not term_is_present(terms[term], folded)]


def _passages(source, term, entry):
    forms = _glossary_source_forms(term, entry.get("category", ""))
    found = [line.strip() for line in source.splitlines() if any(form in line for form in forms)]
    return [line[:MAX_PASSAGE_CHARS] for line in found[:MAX_PASSAGES_PER_TERM]]


def build_messages(source, english, missing, terms):
    items = [{"term": term, "agreed_english": terms[term]["english"], "source_passages": _passages(source, term, terms[term])}
             for term in missing]
    user = f"ITEMS:\n{json.dumps(items, ensure_ascii=False, indent=1)}\n\nENGLISH CHAPTER:\n{english}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_answer(content):
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    answer = json.loads(text)
    if not isinstance(answer, dict):
        raise ValueError("answer is not an object")
    return answer


def rejected_reason(fix, text, entry, others=()):
    """Why this fix can't be applied, or an empty string if it's safe.

    others holds the English of the other glossary terms, which a fix must not rewrite.
    """
    old, new = fix.get("old"), fix.get("new")
    if not isinstance(old, str) or not isinstance(new, str) or not old or not new.strip():
        return "malformed"
    if len(old) > MAX_OLD_CHARS:
        return "old_too_long"
    if text.count(old) != 1:
        return "old_not_unique"
    if old == new:
        return "no_change"
    if SOURCE_SCRIPT.search(new) and not SOURCE_SCRIPT.search(old):
        return "adds_source_script"
    if not term_is_present(entry, _normalized(new)):
        return "agreed_english_missing"
    if any(other in _normalized(old) for other in others):
        return "belongs_to_another_term"
    if _normalized(old) in _normalized(new) or _plural_only(old, new):
        return "only_changes_number_or_adds_words"
    if len(new) > len(old) + len(entry["english"]) + 12:
        return "too_much_added"
    return ""


def _plural_only(old, new):
    words = lambda text: [_singular(word) for word in _normalized(text).split()]
    return words(old) == words(new)


def apply_fixes(project, chapter, text, answer, terms):
    applied, rejected = [], []
    for fix in answer.get("fixes") or []:
        entry = terms.get(fix.get("term")) if isinstance(fix, dict) else None
        others = () if entry is None else {_normalized(value["english"]) for value in terms.values()
                                           if value is not entry and " " in value["english"]
                                           and _normalized(value["english"]) not in _normalized(entry["english"])}
        reason = "unknown_term" if entry is None else rejected_reason(fix, text, entry, others)
        if reason:
            rejected.append({"fix": fix, "reason": reason})
            continue
        text = text.replace(fix["old"], fix["new"], 1)
        applied.append(fix)
    if applied:
        path = project.chapter_dir / f"{chapter:04d}.md"
        backup = project.backup_dir / REPAIR_DIR / f"{chapter:04d}_{int(time.time())}.md"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
        path.write_bytes(text.encode("utf-8"))
    return applied, rejected


def _add_cost(project, stats):
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress["rate_correct_cost_usd"] = progress.get("rate_correct_cost_usd", 0.0) + stats["rate_correct_cost_usd"]
    for field, key in (("total_prompt_tokens", "prompt_tokens"), ("total_completion_tokens", "completion_tokens")):
        progress[field] = progress.get(field, 0) + stats[key]
    atomic_write_json(project.progress_file, progress)


def repair_novel(project, *, max_usd, send, log=print):
    """Repair finished chapters up to max_usd. send(messages) returns (content, finish_reason, stats)."""
    summary = dict(chapters_checked=0, chapters_sent=0, fixes_applied=0, fixes_rejected=0, fine=0, refused=[], cost_usd=0.0)
    with production_run_lock(project.root):
        progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
        sources = {item["chapter"]: item["text"] for item in json.loads(project.chapters_file.read_text(encoding="utf-8"))}
        terms = repairable_terms(json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"])
        directory = repair_dir(project)
        directory.mkdir(parents=True, exist_ok=True)
        for chapter in range(1, progress["last_completed"] + 1):
            path = project.chapter_dir / f"{chapter:04d}.md"
            text = path.read_bytes().decode("utf-8")
            summary["chapters_checked"] += 1
            missing = find_missing_terms(sources[chapter], text, terms)
            record_path = directory / f"{chapter:04d}.json"
            record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else None
            before = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if record and record.get("revision") != REVISION:
                record = None
            if record and record.get("status") == "done" and set(missing) <= set(record["terms"]):
                continue
            if not missing:
                continue
            if record is None or record.get("status") == "done":
                if summary["cost_usd"] >= max_usd:
                    summary["stopped"] = "budget_reached"
                    break
                content, finish, stats = send(build_messages(sources[chapter], text, missing, terms))
                summary["chapters_sent"] += 1
                summary["cost_usd"] += stats["rate_correct_cost_usd"]
                record = dict(revision=REVISION, chapter=chapter, terms=missing, status="answered", finish_reason=finish, content=content,
                              stats=stats, before_sha256=before, utc=datetime.now(timezone.utc).isoformat())
                atomic_write_json(record_path, record)
                _add_cost(project, stats)
            elif record["before_sha256"] != before:
                record.update(status="done", outcome="chapter_changed_before_applying")
                atomic_write_json(record_path, record)
                continue
            if record["finish_reason"] != "stop":
                summary["refused"].append(chapter)
                record.update(status="done", outcome=record["finish_reason"])
                atomic_write_json(record_path, record)
                add_item(project.data_dir, chapter=chapter, kind="term_repair",
                         detail=f"The provider didn't answer the term check ({record['finish_reason']}).")
                continue
            try:
                answer = parse_answer(record["content"])
            except ValueError as exc:
                record.update(status="done", outcome="unreadable_answer", error=str(exc)[:200])
                atomic_write_json(record_path, record)
                continue
            applied, rejected = apply_fixes(project, chapter, text, answer, terms)
            record.update(status="done", outcome="applied", applied=applied, rejected=rejected, fine=answer.get("fine") or [])
            atomic_write_json(record_path, record)
            summary["fixes_applied"] += len(applied)
            summary["fixes_rejected"] += len(rejected)
            summary["fine"] += len(answer.get("fine") or [])
            log(f"Chapter {chapter}: {len(applied)} fixed, {len(rejected)} rejected, {len(answer.get('fine') or [])} fine")
    return summary


def send_with_command_code(key):
    """The real sender: the production model at low reasoning, with the usual retries."""
    from command_code_provider_transport import (COMMAND_CODE_DEEPSEEK_V41_FLASH as MODEL, account_command_code_usage,
                                                  create_command_code_client)
    from provider_retry_policy import send_with_retries
    client = create_command_code_client(api_key=key)

    def send(messages):
        started = datetime.now(timezone.utc).isoformat()
        raw = send_with_retries(lambda: client.chat.completions.create(
            model=MODEL, messages=messages, reasoning_effort="low", max_completion_tokens=MAX_COMPLETION_TOKENS))
        choice = raw.choices[0]
        usage = dict(prompt_tokens=raw.usage.prompt_tokens, completion_tokens=raw.usage.completion_tokens)
        stats = _cost(raw, started, account_command_code_usage, MODEL)
        return choice.message.content, choice.finish_reason, {**usage, **stats}
    return send


def _cost(raw, started, account, model):
    from benchmark_longitudinal_provider import build_provider_request_identity, normalize_provider_response
    identity = build_provider_request_identity(request_body={"model": model, "messages": []})
    normalized = normalize_provider_response(raw.model_dump(), request_identity=identity)
    stats = account(model, normalized.usage, started)
    return {"rate_correct_cost_usd": stats["rate_correct_cost_usd"]}
