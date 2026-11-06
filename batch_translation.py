"""Source-first batch translation: prompts, parsing, consolidation and checks.

Pure functions with no provider, filesystem or production state. batch_runner.py
drives them through the production module. See docs/BATCH_MODE.md.
"""
import json
import re

from noveltranslator_core import clean_json_text

PREPASS_KIND = "batch_source_prepass"
DECISION_KIND = "batch_glossary_decision"
TERM_AUDIT_KIND = "batch_term_audit"
TERM_AUDIT_MAX_TOKENS = 64_000
TERM_AUDIT_CHUNK = 20  # Targets per audit request, as in the periodic audit.
PREPASS_MAX_TOKENS = 64_000
DECISION_MAX_TOKENS = 64_000
DECISION_CHUNK = 60  # Candidates per decision request, all sent at once; the first also settles headings and memory.
MAX_TERMS_PER_CHAPTER = 15  # Sequential state reading settles about 2-3 new terms a chapter.
WIDESPREAD_SHARE = 0.6  # A new key in most of a batch's chapters is usually a common word.
WIDESPREAD_MIN_CHAPTERS = 5
PLURAL_SUFFIXES = ("들", "们")  # Korean and Chinese plural markers.
SOURCE_TAIL_CHARS = 600
CONTEXT_MAX_CHARS = 2400
BASE_SUMMARY_MAX_WORDS = 300
RETRANSLATE_MISSES = 3  # A chapter is translated again only if it misses at least this many settled terms
RETRANSLATE_SHARE = 0.3  # ...and at least this share of the settled terms its source contains.
RETRANSLATE_DIALOGUE_LINES = 3  # ...or if it has this many fewer lines of speech than the Korean source.
# A one-character key also occurs inside ordinary words (Korean 원, 격, 놀), so it
# would match nearly every chapter. In rehearsal such keys made 40 of 50 chapters
# look inconsistent.
MIN_KEY_CHARS = 2
EVENT_TYPES = ("rename", "death", "move", "title", "address", "time", "other")
LASTING_EVENTS = ("rename", "death", "title", "address")
CONFIDENCE = ("high", "medium", "low")
ACTIONS = ("accept", "contextual", "merge", "reject")
MAX_FIELD_CHARS = 300


class BatchResponseError(ValueError):
    """A batch helper answer could not be used."""


PREPASS_PROMPT = """
You read one chapter of a webnovel before it is translated into English. Several
consecutive chapters are prepared at once, so what you record here is the only way
later chapters learn what happened in this one. Do not translate the chapter.
Return ONLY JSON:
{"terms":[{"source":"...","english":"...","category":"...","sentence":"..."}],
 "people":[{"source":"...","english":"...","title":"...","role":"...","location":"...","status":"alive"}],
 "events":[{"type":"rename","detail":"...","old":"...","new":"..."}],
 "time":"",
 "issues":[]}
TERMS: only terms genuinely useful for consistency in later chapters: names of people,
places, organizations, military units, ranks, offices, titles, techniques, artifacts,
and specialized terms this novel coins or uses in its own sense. Do not propose
ordinary vocabulary, one-time descriptions, ordinary objects, generic duties, generic
tactical phrases or ordinary idioms. A common noun or generic title that any novel of
the genre uses (knight, king, prince, kingdom, lord, monster, mana, the north) is
ordinary vocabulary unless this novel uses it as the name of one specific thing.
"source" is an EXACT literal span copied from the chapter, never translated,
transliterated or normalized, in its base form: without a plural marker (레인저, not
레인저들) or a particle. "sentence" is one short exact source sentence containing it.
"english" is the rendering you propose under the policy below. Skip terms already in
the ESTABLISHED GLOSSARY. At most 15 terms, the most important first; most chapters
need far fewer.
PEOPLE: everyone who appears or acts in this chapter, as of the END of the chapter.
"source" is the name as written in the chapter. Use the established English for names
and titles when the glossary has one. "status" is alive, dead or unknown.
EVENTS: in story order, only durable changes later chapters need: a person, unit,
office, place or title renamed; a death; a move to another place; a promotion, demotion
or new title; a change in how someone is addressed (for example 전하 becoming 폐하); a
marker of story time such as a season, a year or days passing. "type" is one of rename,
death, move, title, address, time, other. "detail" is one English sentence. For rename
and address, "old" and "new" are exact source spans; otherwise leave them empty.
TIME: the story time at the end of the chapter if the source states it, else "".
ISSUES: only source corruption or ambiguity that materially affects meaning. At most 3.
""".strip()

DECISION_PROMPT = """
You settle glossary terms for a batch of consecutive webnovel chapters before they are
translated in parallel. Every chapter in the batch will follow your decisions, so be
conservative and consistent.
For each candidate choose one action:
accept = it belongs in the glossary; give the best English (you may improve the proposals).
contextual = it needs several approved renderings depending on context; list them in approved_renderings.
merge = it is a variant or alias of an existing glossary key or of another candidate; give that exact source key in merge_with.
reject = not worth a glossary entry: ordinary vocabulary, a generic title or common noun
any novel of the genre uses (knight, king, kingdom, monster, mana), a one-time
description. Frequency is not a reason to keep a term: a word in nearly every chapter
is usually ordinary vocabulary. "widespread": true marks such candidates; accept one
only if this novel uses it as the name of one specific thing.
A key that is also an ordinary word or a homonym (지배 "rule", 비약 "leap", 약조
"promise", 삭풍 "north wind", 대전 "great war") must not force the term's English onto
its ordinary uses: make it contextual with the ordinary meaning among the approved
renderings, or reject it. "inside_other_words": true marks a key found mostly inside
longer words (각하 inside 생각하다 "to think"); reject it unless the examples show it
used on its own as the term.
Candidates listed with "same_english_as" were proposed with the same English as other
candidates or existing entries; they usually name the same thing in different forms.
Accept the most common form and merge the others into it, unless they are different
things that need different English.
Candidates listed with "contains" include another term or an established entry, given
with its English. The shared part must use that English (if 궤른 is Gwern, 궤른 가문 is
House Gwern). Reject a form that only adds a house or family word, a title, a rank or
the rest of a full name to a name that already has an entry, when its English follows
from its parts; note "derivable". SHARED ENDINGS lists endings that several candidates
or established entries share, with the English each uses for the ending (for example
군단 as Corps in some and Legion in others). Give one ending one English rendering
across all of them, following established entries, unless they are different kinds of
thing.
Never change an existing glossary entry. When a candidate is the new name of something
renamed in this batch, give it its own rendering, consistent in style with the old name.
confidence: high when the rendering is clear; medium when reasonable alternatives exist;
low when you are unsure of the meaning or of the policy fit.
Formal named techniques and arts: capitalize the initial letter of EVERY word
(Black Tiger Seizes The Heart, not Black Tiger Seizes the Heart).
If CHAPTER HEADINGS are listed, give the English heading of every listed chapter,
formatted exactly like the last committed heading. A titled part keeps the same English
title across its numbered parts.
If PRIOR CONTINUITY is given, also return "continuity": the complete working memory as of
the end of the last chapter, revised from PRIOR CONTINUITY with the CHAPTER NOTES, at most
200 English words, durable facts only (injuries, titles, allegiances, locations,
relationships, possessions, unresolved threats); keep uncertainty as uncertainty.
Return ONLY JSON:
{"terms":[{"id":"t001","source":"...","action":"accept","english":"...","category":"...","approved_renderings":[],"merge_with":"","confidence":"high","notes":""}],
 "headings":[{"chapter":1,"english":"..."}],
 "continuity":""}
Return exactly one decision per candidate id, with its source copied exactly.
""".strip()


HANGUL_SYLLABLE = "\uac00-\ud7a3"
INSIDE_WORDS_SHARE = 0.5  # A key found mostly inside longer words (각하 in 생각하다) is rarely the term.


def word_occurrences(key, text):
    """(as a word, inside another word) counts. A Korean key used as a word starts
    one: it is not preceded by another Hangul syllable. Other scripts have no word
    boundaries in the source, so every occurrence counts as a word."""
    if not re.match(f"[{HANGUL_SYLLABLE}]", key or ""):
        return text.count(key), 0
    inside = len(re.findall(f"(?<=[{HANGUL_SYLLABLE}])" + re.escape(key), text))
    return text.count(key) - inside, inside


def mentions(key, text):
    """Whether the source uses the key as a word, not only inside longer words."""
    return word_occurrences(key, text)[0] > 0


def base_form(key):
    """The key without a plural marker. Still a literal span: the base is its prefix."""
    for suffix in PLURAL_SUFFIXES:
        if key.endswith(suffix) and len(key) - len(suffix) >= MIN_KEY_CHARS:
            return key[:-len(suffix)]
    return key


def _text(value, limit=MAX_FIELD_CHARS):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _load_object(content):
    try:
        value = json.loads(clean_json_text(content or ""))
    except (TypeError, ValueError) as exc:
        raise BatchResponseError("answer is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BatchResponseError("answer is not a JSON object")
    return value


def _request(model, system, user, max_tokens):
    return {"model": model, "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}],
            "max_tokens": max_tokens, "extra_body": {"thinking": {"type": "disabled"}}}


def build_prepass_request(chapter, *, model, policy, established_glossary):
    """One source-only request per chapter. `policy` is the profile's state-extraction policy."""
    user = (f"CHAPTER: {chapter['chapter']}\n"
            f"ESTABLISHED GLOSSARY:\n{established_glossary or 'None.'}\n"
            f"SOURCE:\n{chapter['text']}")
    return _request(model, f"{PREPASS_PROMPT}\n\nPROFILE-SPECIFIC TERMINOLOGY POLICY:\n{policy}".strip(),
                    user, PREPASS_MAX_TOKENS)


def parse_prepass(content, chapter_number, source_text):
    """A usable fact sheet, keeping only terms whose key is literally in the chapter."""
    value = _load_object(content)
    terms, seen = [], set()
    for item in value.get("terms") or []:
        if not isinstance(item, dict):
            continue
        key = base_form(_text(item.get("source"), 80))
        english = _text(item.get("english"), 160)
        if not key or not english or key in seen or key not in source_text:
            continue
        seen.add(key)
        sentence = _text(item.get("sentence"))
        terms.append({"source": key, "english": english, "category": _text(item.get("category"), 60),
                      "sentence": sentence if sentence and sentence in source_text else ""})
        if len(terms) >= MAX_TERMS_PER_CHAPTER:
            break
    people = []
    for item in value.get("people") or []:
        if isinstance(item, dict) and _text(item.get("source"), 80):
            status = _text(item.get("status"), 10).lower()
            people.append({"source": _text(item.get("source"), 80), "english": _text(item.get("english"), 120),
                           "title": _text(item.get("title"), 120), "role": _text(item.get("role"), 160),
                           "location": _text(item.get("location"), 160),
                           "status": status if status in ("alive", "dead") else "unknown"})
    events = []
    for item in value.get("events") or []:
        if isinstance(item, dict) and _text(item.get("detail")):
            kind = _text(item.get("type"), 20).lower()
            events.append({"type": kind if kind in EVENT_TYPES else "other", "detail": _text(item.get("detail")),
                           "old": _text(item.get("old"), 80), "new": _text(item.get("new"), 80)})
    issues = [_text(item) for item in value.get("issues") or [] if _text(item)][:3]
    return {"chapter": chapter_number, "terms": terms, "people": people, "events": events,
            "time": _text(value.get("time"), 160), "issues": issues}


def consolidate_candidates(sheets, sources, existing):
    """Deduplicate proposals by exact source key, in order of first appearance.

    Keys already in the glossary (`existing`, its entries or just its keys) never
    become candidates, so locked and existing entries cannot change. A key must occur
    literally in some batch chapter. Each candidate is marked for the decision:
    `widespread` when it is in most chapters, as common words are, and
    `same_english_as` for other candidates or entries proposed with the same English.
    """
    chapters = sorted(sources)
    groups = {}
    for number in sorted(sheets):
        for term in sheets[number]["terms"]:
            key = term["source"]
            if key in existing or len(key) < MIN_KEY_CHARS:
                continue
            group = groups.setdefault(key, {"source": key, "proposals": [], "examples": []})
            proposal = next((item for item in group["proposals"]
                             if item["english"].casefold() == term["english"].casefold()), None)
            if proposal is None:
                proposal = {"english": term["english"], "category": term["category"], "chapters": []}
                group["proposals"].append(proposal)
            proposal["chapters"].append(number)
            if term["sentence"] and term["sentence"] not in group["examples"] and len(group["examples"]) < 2:
                group["examples"].append(term["sentence"])
    result = []
    for key, group in groups.items():
        counts = {number: word_occurrences(key, sources[number]) for number in chapters}
        present = [number for number in chapters if counts[number][0]]
        if not present:
            continue
        group["first_chapter"] = present[0]
        group["chapters_present"] = len(present)
        group["occurrences"] = sum(counts[number][0] for number in present)
        inside = sum(counts[number][1] for number in chapters)
        group["inside_other_words"] = inside >= INSIDE_WORDS_SHARE * (inside + group["occurrences"])
        group["proposals"].sort(key=lambda item: (-len(item["chapters"]), item["chapters"][0]))
        group["disputed"] = len(group["proposals"]) > 1
        group["widespread"] = (len(chapters) >= WIDESPREAD_MIN_CHAPTERS
                               and len(present) >= WIDESPREAD_SHARE * len(chapters))
        result.append(group)
    by_english = {}
    for key, entry in (existing.items() if isinstance(existing, dict) else ()):
        if entry.get("english"):
            by_english.setdefault(entry["english"].casefold(), []).append(key)
    for group in result:
        by_english.setdefault(group["proposals"][0]["english"].casefold(), []).append(group["source"])
    for group in result:
        group["same_english_as"] = [key for key in by_english[group["proposals"][0]["english"].casefold()]
                                    if key != group["source"]]
    known = {key: entry["english"] for key, entry in (existing.items() if isinstance(existing, dict) else ())
             if entry.get("english") and entry.get("status") != "contextual"}
    known.update({group["source"]: group["proposals"][0]["english"] for group in result})
    for group in result:
        group["contains"] = [{"source": key, "english": english} for key, english in contained_terms(group["source"], known)]
    result.sort(key=lambda item: (item["first_chapter"], item["source"]))
    for index, group in enumerate(result, 1):
        group["id"] = f"t{index:03d}"
    return result


def contained_terms(key, known):
    """Other known terms inside this key, longest first, skipping ones inside a longer match."""
    found = sorted(((other, english) for other, english in known.items()
                    if other != key and len(other) >= MIN_KEY_CHARS and other in key), key=lambda item: -len(item[0]))
    kept = []
    for other, english in found:
        if not any(other in longer for longer, _ in kept):
            kept.append((other, english))
    return kept[:4]


def _ending(key):
    return key.rsplit(" ", 1)[-1] if " " in key else key[-2:]


def _edge_words(english):
    """First and last English words: where an ending's rendering sits (House Gwern, 3rd Corps)."""
    words = [word.casefold() for word in re.findall(r"[A-Za-z]+", english or "")
             if word.casefold() not in ("of", "the", "a", "an")]
    return {word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 else word
            for word in words[:1] + words[-1:]}


def ending_families(terms, minimum=3):
    """Endings shared by several terms whose English uses competing words for them.

    군단 as Corps in 3군단 but Legion in 서부군단; 가문 as House in some, family in others.
    `terms` maps source key to English. Returns {ending: {word: [keys using it, not the other]}}.
    Names that merely share an ending (Tailheim, Mendelheim) share no word, so they never match.
    """
    families = {}
    for key, english in terms.items():
        if " " in key or len(key) > 2:
            families.setdefault(_ending(key), {})[key] = _edge_words(english)
    result = {}
    for ending, members in families.items():
        if len(members) < minimum:
            continue
        counts = {}
        for words in members.values():
            for word in words:
                counts[word] = counts.get(word, 0) + 1
        shared = sorted((word for word, count in counts.items() if count >= 2), key=lambda word: -counts[word])
        for index, first in enumerate(shared):
            for second in shared[index + 1:]:
                only_first = [key for key, words in members.items() if first in words and second not in words]
                only_second = [key for key, words in members.items() if second in words and first not in words]
                if only_first and only_second:
                    result[ending] = {first: only_first, second: only_second}
                    break
            if ending in result:
                break
    return result


def shared_endings(groups, existing):
    """Lines for the decision request: endings rendered more than one way so far."""
    terms = {key: entry["english"] for key, entry in existing.items()
             if entry.get("english") and entry.get("status") != "contextual"}
    terms.update({group["source"]: group["proposals"][0]["english"] for group in groups})
    return [f"{ending}: " + "; ".join(f"{word or '?'} in {', '.join(sorted(keys)[:6])}" for word, keys in words.items())
            for ending, words in sorted(ending_families(terms).items())]


def shared_name_conflicts(settled, existing_entries):
    """After the decision: a term whose English drops the English of a term inside it,
    and endings still rendered more than one way. For the review list; nothing changes."""
    known = {key: entry["english"] for key, entry in existing_entries.items()
             if entry.get("english") and entry.get("status") != "contextual"}
    known.update({key: entry["english"] for key, entry in settled.items()
                  if entry["english"] and entry["status"] != "contextual"})
    conflicts = []
    for key, entry in settled.items():
        if not entry["english"] or entry["merge_with"] or entry["status"] == "contextual":
            continue
        for other, english in contained_terms(key, known):
            if english.casefold() not in entry["english"].casefold():
                conflicts.append({"chapter": entry["first_chapter"],
                                  "detail": f"{key} → {entry['english']} does not use {other} → {english}"})
    batch_terms = {key: entry["english"] for key, entry in settled.items()
                   if entry["english"] and not entry["merge_with"] and entry["status"] != "contextual"}
    for ending, words in ending_families({**known, **batch_terms}).items():
        keys = [key for group in words.values() for key in group if key in settled]
        if keys:
            conflicts.append({"chapter": min(settled[key]["first_chapter"] for key in keys),
                              "detail": f"{ending} is rendered as " + ", ".join(sorted(word for word in words if word))})
    return conflicts


def ordered_changes(sheets):
    """Renames, title and address changes in story order, for the decision request."""
    return [f"Chapter {number}: {event['detail']}"
            + (f" ({event['old']} → {event['new']})" if event["old"] and event["new"] else "")
            for number in sorted(sheets) for event in sheets[number]["events"]
            if event["type"] in ("rename", "title", "address")]


def build_decision_request(groups, *, model, policy, established_glossary, changes, endings=(),
                           headings=None, last_heading=None, prior_continuity=None, chapter_notes=None):
    """One decision for a chunk of candidates. The first chunk also settles headings and memory."""
    candidates = [{"id": group["id"], "source": group["source"], "first_chapter": group["first_chapter"],
                   "chapters_present": group["chapters_present"], "occurrences": group["occurrences"],
                   "proposals": [{"english": item["english"], "category": item["category"],
                                  "chapters": item["chapters"][:8]} for item in group["proposals"]],
                   "examples": group["examples"],
                   **({"widespread": True} if group.get("widespread") else {}),
                   **({"inside_other_words": True} if group.get("inside_other_words") else {}),
                   **({"same_english_as": group["same_english_as"]} if group.get("same_english_as") else {}),
                   **({"contains": group["contains"]} if group.get("contains") else {})}
                  for group in groups]
    parts = []
    if headings:
        last = (f"Source: {last_heading[0]}\nEnglish: {last_heading[1]}" if last_heading else "None yet.")
        parts.append(f"LAST COMMITTED HEADING:\n{last}")
    parts += [f"ESTABLISHED GLOSSARY:\n{established_glossary or 'None.'}",
              "RENAMES, TITLE AND ADDRESS CHANGES IN STORY ORDER:\n" + ("\n".join(f"- {line}" for line in changes)
                                                                       or "None."),
              "SHARED ENDINGS:\n" + ("\n".join(f"- {line}" for line in endings) or "None."),
              "CANDIDATES:\n" + json.dumps(candidates, ensure_ascii=False, indent=1)]
    if headings:
        parts.append("CHAPTER HEADINGS:\n" + "\n".join(f"{number}: {text}" for number, text in headings))
    if prior_continuity is not None:
        parts.append(f"PRIOR CONTINUITY:\n{prior_continuity or 'None.'}\n\nCHAPTER NOTES:\n{chapter_notes or 'None.'}")
    return _request(model, f"{DECISION_PROMPT}\n\nPROFILE-SPECIFIC REVIEW POLICY:\n{policy}".strip(),
                    "\n\n".join(parts), DECISION_MAX_TOKENS)


def parse_decision(content, groups, heading_chapters=()):
    value = _load_object(content)
    by_id = {group["id"]: group for group in groups}
    terms = {}
    for item in value.get("terms") or []:
        if not isinstance(item, dict) or item.get("id") not in by_id or item["id"] in terms:
            continue
        if item.get("source") not in (None, by_id[item["id"]]["source"]):
            continue  # A decision under the wrong id would put one term's English on another.
        action = _text(item.get("action"), 20).lower()
        confidence = _text(item.get("confidence"), 10).lower()
        renderings = [_text(text, 160) for text in item.get("approved_renderings") or [] if _text(text, 160)]
        terms[item["id"]] = {"action": action if action in ACTIONS else "accept",
                             "english": _text(item.get("english"), 160), "category": _text(item.get("category"), 60),
                             "approved_renderings": renderings, "merge_with": _text(item.get("merge_with"), 80),
                             "confidence": confidence if confidence in CONFIDENCE else "low",
                             "notes": _text(item.get("notes"), 200)}
    headings = {}
    wanted = set(heading_chapters)
    for item in value.get("headings") or []:
        if isinstance(item, dict) and item.get("chapter") in wanted and _text(item.get("english"), 200):
            headings[item["chapter"]] = _text(item.get("english"), 200)
    continuity = value.get("continuity")
    return {"terms": terms, "headings": headings,
            "continuity": continuity.strip() if isinstance(continuity, str) else ""}


def settle_terms(groups, decisions, existing_entries, *, format_name=None):
    """Settled entries by source key. Unanswered or unclear keys are accepted at low confidence.

    format_name(category, english) applies the profile's formal-name capitalization.
    """
    format_name = format_name or (lambda category, english: english)
    settled = {}
    for group in groups:
        decision = decisions.get(group["id"])
        top = group["proposals"][0]
        if decision is None:
            decision = {"action": "accept", "english": "", "category": "", "approved_renderings": [],
                        "merge_with": "", "confidence": "low", "notes": "no batch decision"}
        if decision["action"] == "reject":
            continue
        category = decision["category"] or top["category"]
        entry = {"english": format_name(category, decision["english"] or top["english"]), "category": category,
                 "status": "provisional", "first_chapter": group["first_chapter"], "approved_renderings": [],
                 "merge_with": "", "confidence": decision["confidence"], "disputed": group["disputed"],
                 "notes": decision["notes"], "proposals": [item["english"] for item in group["proposals"]]}
        if decision["action"] == "contextual" and decision["approved_renderings"]:
            entry.update(status="contextual", english="",
                         approved_renderings=[format_name(category, text) for text in decision["approved_renderings"]])
        elif decision["action"] == "merge":
            entry["merge_with"] = decision["merge_with"]
        settled[group["source"]] = entry
    # Spacing variants of one name (철사자 성, 철사자성) share one entry: an existing
    # one, else the batch form found in the most chapters.
    spread = {group["source"]: group.get("chapters_present", 0) for group in groups}
    existing_compact = {re.sub(r"\s+", "", key): key for key in existing_entries}
    families = {}
    for key in settled:
        families.setdefault(re.sub(r"\s+", "", key), []).append(key)
    for compact, keys in families.items():
        primary = existing_compact.get(compact) or max(
            keys, key=lambda key: (spread.get(key, 0), -settled[key]["first_chapter"]))
        for key in keys:
            if key != primary and settled[key]["status"] != "contextual" and not settled[key]["merge_with"]:
                settled[key]["merge_with"] = primary
    # Keys settled on exactly the same English and category are forms of one thing
    # (겨울기사단, 겨울의 기사): alias them to an existing entry, else the most widespread.
    same = {}
    for key, entry in existing_entries.items():
        if entry.get("english") and entry.get("status") != "contextual":
            same.setdefault((entry["english"].casefold(), entry.get("category", "")), []).append(key)
    for key, entry in settled.items():
        if entry["english"] and entry["status"] != "contextual" and not entry["merge_with"]:
            same.setdefault((entry["english"].casefold(), entry["category"]), []).append(key)
    for keys in same.values():
        batch_keys = [key for key in keys if key in settled]
        if len(keys) < 2 or not batch_keys:
            continue
        primary = next((key for key in keys if key in existing_entries), None) or max(
            batch_keys, key=lambda key: (spread.get(key, 0), -settled[key]["first_chapter"]))
        for key in batch_keys:
            if key != primary:
                settled[key]["merge_with"] = primary
    # A merge target must exist when the alias is committed: an existing entry,
    # or a batch term first seen no later than the alias.
    for key, entry in settled.items():
        target = entry["merge_with"]
        if not target:
            continue
        known = existing_entries.get(target) or settled.get(target)
        if (target == key or known is None
                or (target in settled and settled[target]["first_chapter"] > entry["first_chapter"])):
            entry["merge_with"] = ""
        if known is not None and target != key:
            entry["english"] = known.get("english") or entry["english"]
    return settled


def glossary_entries_for_translation(existing_entries, settled):
    """Existing entries plus settled batch terms, in the glossary's own entry shape."""
    combined = {}
    for key, entry in settled.items():
        item = {"english": entry["english"], "category": entry["category"], "status": entry["status"],
                "first_chapter": entry["first_chapter"], "notes": entry["notes"]}
        if entry["status"] == "contextual":
            item["approved_renderings"] = list(entry["approved_renderings"])
        if entry["merge_with"]:
            item["alias_of"] = entry["merge_with"]
        combined[key] = item
    combined.update(existing_entries)
    return combined


def flagged_terms(settled):
    """Keys a person should look at: unsure or disputed decisions that now reach many chapters."""
    return [(key, entry) for key, entry in settled.items()
            if entry["confidence"] == "low" or (entry["disputed"] and entry["confidence"] != "high")]


def _apply_sheet(people, events, sheet):
    for person in sheet["people"]:
        current = people.setdefault(person["source"], {"source": person["source"]})
        current.update({key: value for key, value in person.items() if value and value != "unknown"})
        current["chapter"] = sheet["chapter"]
    for event in sheet["events"]:
        events.append({**event, "chapter": sheet["chapter"]})
    if sheet["time"]:
        events.append({"type": "time", "detail": sheet["time"], "old": "", "new": "", "chapter": sheet["chapter"]})


def _person_line(person):
    name = person.get("english") or person["source"]
    if person.get("english") and person["source"] != person["english"]:
        name = f"{person['english']} ({person['source']})"
    details = [person[key] for key in ("title", "role") if person.get(key)]
    if person.get("location"):
        details.append(f"at {person['location']}")
    if person.get("status") == "dead":
        details.append("dead")
    return f"- {name}" + (f": {', '.join(details)}" if details else "")


def _event_line(event):
    change = f" ({event['old']} → {event['new']})" if event["old"] and event["new"] else ""
    return f"- Ch {event['chapter']}: {event['detail']}{change}"


def trim_words(text, limit):
    words = (text or "").split()
    return " ".join(words[:limit]) + (" …" if len(words) > limit else "")


def render_memory(base_summary, people, events, focus=(), first_chapter=None, limit=CONTEXT_MAX_CHARS):
    """Committed memory plus what the source established since, trimmed to a fixed size.

    People in `focus` (the next chapter's cast) are kept first; old passing events
    are dropped before lasting ones (renames, deaths, titles, forms of address).
    """
    base = trim_words(base_summary, BASE_SUMMARY_MAX_WORDS) or "No previous continuity summary is available."
    if not people and not events:
        return base
    ordered = sorted(people.values(), key=lambda item: (item["source"] not in focus, -item["chapter"]))
    person_lines = [_person_line(item) for item in ordered]
    kept = list(events)

    def render():
        sections = [base, f"Since then (source notes from chapter {first_chapter} on):"]
        if person_lines:
            sections.append("People now:\n" + "\n".join(person_lines))
        if kept:
            sections.append("Events:\n" + "\n".join(_event_line(item) for item in kept))
        return "\n\n".join(sections)

    text = render()
    while len(text) > limit:
        passing = next((item for item in kept if item["type"] not in LASTING_EVENTS), None)
        if passing is not None:
            kept.remove(passing)
        elif len(person_lines) > len([1 for item in ordered if item["source"] in focus]):
            person_lines.pop()
        elif kept:
            kept.pop(0)
        elif person_lines:
            person_lines.pop()
        else:
            break
        text = render()
    return text


def build_timeline(base_summary, sheets, chapters):
    """Each chapter's context packet (story as of the chapter before) and memory after it."""
    people, events = {}, []
    first = chapters[0] if chapters else None
    packets, after = {}, {}
    for number in chapters:
        sheet = sheets[number]
        focus = {person["source"] for person in sheet["people"]}
        packets[number] = render_memory(base_summary, people, events, focus, first)
        _apply_sheet(people, events, sheet)
        after[number] = render_memory(base_summary, people, events, focus, first)
    return packets, after


def source_tail(text, limit=SOURCE_TAIL_CHARS):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return cut[newline + 1:] if 0 <= newline < limit // 2 else cut


def renderings(entry):
    return [text for text in (entry["approved_renderings"] if entry["status"] == "contextual"
                              else [entry["english"]]) if text]


def terms_in_source(source, settled):
    return [key for key, entry in settled.items()
            if len(key) >= MIN_KEY_CHARS and mentions(key, source) and renderings(entry)]


def term_misses(translation, source, settled):
    """Settled batch keys present in the source whose rendering the translation never uses."""
    folded = translation.casefold()
    return [key for key in terms_in_source(source, settled)
            if not any(text.casefold() in folded for text in renderings(settled[key]))]


def needs_retranslation(misses, source, settled):
    present = len(terms_in_source(source, settled))
    return len(misses) >= RETRANSLATE_MISSES and len(misses) >= RETRANSLATE_SHARE * present


def repair_variants(translation, source, settled, misses):
    """Replace another proposal for the same key with the settled rendering, source-anchored.

    Only for keys present in this chapter's source, and never a variant that is part
    of any settled rendering, so a fix cannot corrupt another term.
    """
    all_renderings = [text for entry in settled.values() for text in renderings(entry)]
    repairs = []
    for key in misses:
        entry = settled[key]
        if entry["status"] == "contextual" or key not in source:
            continue
        for variant in sorted(set(entry["proposals"]) - {entry["english"]}, key=len, reverse=True):
            if len(variant) < 3 or any(variant.casefold() in text.casefold() for text in all_renderings):
                continue
            pattern = re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)")
            translation, count = pattern.subn(entry["english"], translation)
            if count:
                repairs.append({"source": key, "old": variant, "new": entry["english"], "count": count})
                break
    return translation, repairs


def state_for_chapter(number, settled, memory_after, sheet):
    """What state extraction would have returned, from the batch's source reading."""
    new_terms = [{"chinese": key, "english": entry["english"] or entry["approved_renderings"][0],
                  "category": entry["category"], "status": "provisional",
                  "notes": entry["notes"] or "Settled for the batch from the source."}
                 for key, entry in settled.items() if entry["first_chapter"] == number]
    return {"new_terms": new_terms, "continuity_summary": memory_after,
            "issues": list((sheet or {}).get("issues", []))[:5]}


def review_for_candidates(candidates, settled, existing_keys, candidate_id):
    """The batch decision for each candidate the commit path prepared."""
    decisions = []
    for candidate in candidates:
        entry = settled.get(candidate["chinese"])
        decision = {"candidate_id": candidate_id(candidate), "action": "accept",
                    "english": candidate.get("english", ""), "category": candidate.get("category", ""),
                    "notes": "Settled for the batch from the source."}
        if entry is not None:
            decision.update(english=entry["english"] or candidate.get("english", ""), category=entry["category"])
            if entry["status"] == "contextual":
                decision.update(action="contextual", approved_renderings=list(entry["approved_renderings"]))
            elif entry["merge_with"] and entry["merge_with"] in existing_keys:
                decision.update(action="merge", merge_with=entry["merge_with"])
        decisions.append(decision)
    return {"decisions": decisions, "issues": []}


def build_term_audit_request(targets, *, model, system_prompt, glossary_index, start, end, chunk_index, total_chunks,
                             inside_words=()):
    """The periodic audit's request, for batch terms before any chapter uses them.

    Returns (request, {short id: source key}). Targets come from the periodic audit's
    own source-backed plan, so they carry the same source contexts. `inside_words`
    are keys found mostly inside longer words.
    """
    ids = {f"c{index:02d}": target["chinese"] for index, target in enumerate(targets, 1)}
    shown = [{"candidate_id": short, "source_key": target["chinese"], "family_id": target["chinese"],
              "family_root": target.get("alias_of") or target["chinese"], "alias_of": target.get("alias_of", ""),
              "english": target.get("english", ""), "approved_renderings": target.get("approved_renderings", []),
              "category": target.get("category", ""), "status": target.get("status", "provisional"),
              "notes": target.get("notes", ""), "contexts": target.get("contexts", []),
              **({"inside_other_words": True} if target["chinese"] in inside_words else {})}
             for short, target in zip(ids, targets)]
    user = (f"AUDIT RANGE: Chapters {start}-{end}\n"
            f"AUDIT CHUNK: {chunk_index} of {total_chunks}\n"
            "These terms were settled for a batch of chapters that is not translated yet.\n"
            "A key that is also an ordinary word or a homonym must not force the term's English onto its\n"
            "ordinary uses: make it contextual with the ordinary meaning among the approved renderings, or\n"
            "reject it. inside_other_words marks a key found mostly inside longer words; reject it unless\n"
            "the contexts show it used on its own as the term.\n"
            "Return exactly one decision for every supplied target in this chunk, identified\n"
            "only by its exact request-local candidate_id. Valid IDs for this request are:\n"
            f"{', '.join(ids)}\n"
            f"CURRENT GLOSSARY INDEX:\n{glossary_index}\n"
            f"AUDIT TARGETS:\n{json.dumps(shown, ensure_ascii=False, indent=2)}\n"
            "STRUCTURED FAMILY CONTEXT:\n[]")
    return _request(model, system_prompt, user, TERM_AUDIT_MAX_TOKENS), ids


def parse_term_audit(content, ids):
    """Decisions by source key; unknown ids and actions are ignored, so a term stays as settled."""
    decisions = {}
    for item in _load_object(content).get("decisions") or []:
        if not isinstance(item, dict) or item.get("candidate_id") not in ids:
            continue
        action = _text(item.get("action"), 20).lower()
        if action in ("keep", "correct", "reject", "contextual", "merge"):
            decisions[ids[item["candidate_id"]]] = {
                "action": action, "english": _text(item.get("english"), 160),
                "approved_renderings": [_text(text, 160) for text in item.get("approved_renderings") or []
                                        if _text(text, 160)],
                "merge_with": _text(item.get("merge_with"), 80), "notes": _text(item.get("notes"), 200)}
    return decisions


def apply_term_audit(settled, decisions, existing_entries, *, format_name=None):
    """The settled terms after the audit, and a list of what it changed."""
    format_name = format_name or (lambda category, english: english)
    settled = {key: dict(entry) for key, entry in settled.items()}
    changes = []
    for key, decision in decisions.items():
        entry = settled.get(key)
        if entry is None or decision["action"] == "keep":
            continue
        if decision["action"] == "reject":
            settled.pop(key)
            changes.append(f"{key}: rejected")
        elif decision["action"] == "correct" and decision["english"]:
            english = format_name(entry["category"], decision["english"])
            if english != entry["english"]:
                changes.append(f"{key}: {entry['english']} → {english}")
                entry.update(english=english, status="provisional", approved_renderings=[])
        elif decision["action"] == "contextual" and decision["approved_renderings"]:
            renderings = [format_name(entry["category"], text) for text in decision["approved_renderings"]]
            changes.append(f"{key}: contextual {' / '.join(renderings)}")
            entry.update(status="contextual", english="", approved_renderings=renderings)
        elif decision["action"] == "merge":
            target = decision["merge_with"]
            known = existing_entries.get(target) or settled.get(target)
            if known is not None and target != key and (
                    target not in settled or settled[target]["first_chapter"] <= entry["first_chapter"]):
                entry.update(merge_with=target, english=known.get("english") or entry["english"])
                changes.append(f"{key}: merged into {target}")
    for entry in settled.values():
        if entry["merge_with"] and entry["merge_with"] not in settled and entry["merge_with"] not in existing_entries:
            entry["merge_with"] = ""  # Its target was rejected.
    return settled, changes
