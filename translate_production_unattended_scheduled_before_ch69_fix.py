import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError

MODEL = "deepseek-v4-pro"
FINAL_CHAPTER = 1406
AUDIT_INTERVAL = 25

# True = automatically start DeepSeek API requests only during off-peak hours.
# False = ignore DeepSeek peak/off-peak hours and run whenever active.
AUTO_OFF_PEAK_ONLY = True
PEAK_UTC_WINDOWS = [(1, 4), (6, 10)]

# Manual pause: create pause.flag while the script is running.
# Current API request may finish; no NEXT request starts until the file is deleted.
MANUAL_PAUSE_FILE = Path("pause.flag")
PAUSE_CHECK_SECONDS = 5
SCHEDULE_CHECK_SECONDS = 30
MAX_API_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]
PRICE_CACHE_MISS_INPUT = 0.435
PRICE_CACHE_HIT_INPUT = 0.003625
PRICE_OUTPUT = 0.87

CHAPTERS_FILE = Path("data/chapters.json")
GLOSSARY_FILE = Path("data/glossary.json")
PROGRESS_FILE = Path("data/progress.json")
CONTINUITY_FILE = Path("data/continuity.json")
ISSUES_FILE = Path("data/issues.json")
AUDIT_HISTORY_FILE = Path("data/audit_history.json")
TRANSLATION_PROMPT_FILE = Path("production_translation.txt")
OUTPUT_DIR = Path("output/chapters")
PENDING_DIR = Path("output/pending")
QA_DIR = Path("output/qa")
AUDIT_DIR = Path("output/audits")
BACKUP_DIR = Path("backup")
MAX_AUDIT_GLOSSARY_CHARS = 50000
MAX_TERM_CONTEXTS = 2
TERM_CONTEXT_RADIUS = 180

for directory in (OUTPUT_DIR, PENDING_DIR, QA_DIR, AUDIT_DIR, BACKUP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

load_dotenv()
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise RuntimeError("DEEPSEEK_API_KEY was not found in .env")
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def load_json(path, default=None):
    if not path.exists():
        if default is None:
            raise FileNotFoundError(path)
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    temp.replace(path)


def zero_stats():
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
    }


def combine_stats(*items):
    out = zero_stats()
    for stats in items:
        if not stats:
            continue
        for key in out:
            out[key] += stats.get(key, 0) or 0
    return out


with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)
chapter_map = {item["chapter"]: item for item in chapters}
glossary = load_json(GLOSSARY_FILE, {"entries": {}})
progress = load_json(PROGRESS_FILE, {})
continuity = load_json(CONTINUITY_FILE, {"latest_chapter": 0, "summary": ""})
issues_data = load_json(ISSUES_FILE, {"issues": []})
audit_history = load_json(AUDIT_HISTORY_FILE, {"audits": []})
system_prompt = TRANSLATION_PROMPT_FILE.read_text(encoding="utf-8")

progress.setdefault("completed", [])
progress.setdefault("failed", [])
progress.setdefault("last_completed", 0)
progress.setdefault("next_chapter", progress["last_completed"] + 1)
progress.setdefault("total_prompt_tokens", 0)
progress.setdefault("total_completion_tokens", 0)
progress.setdefault("estimated_cost_usd", 0.0)
progress.setdefault("last_audited_chapter", progress["last_completed"])
issues_data.setdefault("issues", [])
glossary.setdefault("entries", {})
audit_history.setdefault("audits", [])


def apply_known_glossary_fixes():
    entries = glossary["entries"]
    changed = []
    removed = []
    if "蓋州游擊" in entries:
        if entries["蓋州游擊"].get("english") != "Gaizhou Mobile Corps Commander":
            entries["蓋州游擊"]["english"] = "Gaizhou Mobile Corps Commander"
            entries["蓋州游擊"]["status"] = "provisional"
            entries["蓋州游擊"]["notes"] = "Corrected automatically: 蓋州 is Gaizhou, not Ganzhou."
            changed.append("蓋州游擊")
    if "三十六計" in entries:
        entries["三十六計"]["english"] = "Thirty-Six Stratagems"
        entries["三十六計"]["status"] = "locked"
        entries["三十六計"]["notes"] = "Traditional Thirty-Six Stratagems; not 'the thirty-sixth stratagem'."
        changed.append("三十六計")
    remove_terms = {
        "紅票", "红票", "負距離", "负距离", "磚家", "砖家", "手藝人", "手艺人",
        "狼皮座椅", "商隊", "商队", "突進之策", "突进之策", "白毛風", "白毛风",
        "寶豐堂後門", "宝丰堂后门", "倒夜壺", "倒夜壶", "西洋鏡", "西洋镜",
    }
    for term in remove_terms:
        if term in entries:
            del entries[term]
            removed.append(term)
    if changed or removed:
        save_json(GLOSSARY_FILE, glossary)
        print("\n" + "=" * 70)
        print("STARTUP GLOSSARY CLEANUP")
        print("=" * 70)
        for term in changed:
            print(f"Corrected: {term}")
        for term in removed:
            print(f"Removed low-value term: {term}")
        print("=" * 70)


apply_known_glossary_fixes()

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")


def utc_now():
    return datetime.now(timezone.utc)


def is_deepseek_peak_time(now=None):
    if now is None:
        now = utc_now()
    utc_hour = now.hour + now.minute / 60 + now.second / 3600
    return any(start <= utc_hour < end for start, end in PEAK_UTC_WINDOWS)


def next_off_peak_time(now=None):
    if now is None:
        now = utc_now()
    if not is_deepseek_peak_time(now):
        return now
    if 1 <= now.hour < 4:
        return now.replace(hour=4, minute=0, second=0, microsecond=0)
    if 6 <= now.hour < 10:
        return now.replace(hour=10, minute=0, second=0, microsecond=0)
    return now


def wait_for_api_permission():
    """Wait locally until an API request is allowed. Waiting uses no API."""
    manual_message_shown = False
    schedule_message_shown = False

    while True:
        if MANUAL_PAUSE_FILE.exists():
            if not manual_message_shown:
                print("\n" + "=" * 70)
                print("MANUAL PAUSE ACTIVE")
                print("=" * 70)
                print(f"Detected: {MANUAL_PAUSE_FILE}")
                print("No new DeepSeek API request will start while pause.flag exists.")
                print("Delete pause.flag to resume.")
                print("To shut down safely, wait for this message and then press Ctrl+C.")
                print("=" * 70)
                manual_message_shown = True
            time.sleep(PAUSE_CHECK_SECONDS)
            continue

        if manual_message_shown:
            print("Manual pause removed. API work may resume.")
            manual_message_shown = False

        if AUTO_OFF_PEAK_ONLY:
            now = utc_now()
            if is_deepseek_peak_time(now):
                resume_at = next_off_peak_time(now)
                remaining = max(0, int((resume_at - now).total_seconds()))
                if not schedule_message_shown:
                    print("\n" + "=" * 70)
                    print("DEEPSEEK PEAK PERIOD — AUTOMATIC PAUSE")
                    print("=" * 70)
                    print("AUTO_OFF_PEAK_ONLY = True, so no new API request will start during the peak window.")
                    print("Off-peak resumes at: " + resume_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
                    print(f"Approximate wait remaining: {remaining // 60} minutes")
                    print("Waiting is local and consumes zero DeepSeek API tokens.")
                    print("=" * 70)
                    schedule_message_shown = True
                time.sleep(min(SCHEDULE_CHECK_SECONDS, max(1, remaining)))
                continue

        if schedule_message_shown:
            print("DeepSeek off-peak period active. API work resuming.")
        return


def call_api_with_retries(**kwargs):
    last_error = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        wait_for_api_permission()
        try:
            return client.chat.completions.create(**kwargs)
        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            last_error = exc
            print(f"\nTemporary API error (attempt {attempt}/{MAX_API_RETRIES})")
            print(exc)
            if attempt < MAX_API_RETRIES:
                delay = RETRY_DELAYS[attempt - 1]
                print(f"Waiting {delay} seconds before retrying...")
                time.sleep(delay)
        except APIError:
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("API call failed without a captured exception.")


def usage_and_cost(usage):
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    cache_hits = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    cache_misses = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    if cache_hits == 0 and cache_misses == 0:
        cache_misses = prompt_tokens
    cost = (
        cache_misses / 1_000_000 * PRICE_CACHE_MISS_INPUT
        + cache_hits / 1_000_000 * PRICE_CACHE_HIT_INPUT
        + completion_tokens / 1_000_000 * PRICE_OUTPUT
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
    }


def add_stats_to_project(stats):
    progress["total_prompt_tokens"] += stats.get("prompt_tokens", 0)
    progress["total_completion_tokens"] += stats.get("completion_tokens", 0)
    progress["estimated_cost_usd"] += stats.get("cost_usd", 0.0)


def clean_json_text(content):
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


GENERIC_JSON_REPAIR_PROMPT = """
You repair malformed JSON.
Return ONLY one valid JSON object.
Preserve the information. Do not add commentary. Do not use Markdown fences.
Correct only what is necessary to make the JSON valid.
""".strip()


def repair_json_content(label, malformed_content, schema_hint=""):
    print(f"\n{label} was malformed.")
    print("Attempting one automatic JSON repair...")
    prompt = GENERIC_JSON_REPAIR_PROMPT
    if schema_hint:
        prompt += "\n\nRequired structure:\n" + schema_hint
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Repair this malformed JSON:\n\n" + malformed_content},
        ],
        max_tokens=5000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    repaired_content = clean_json_text(response.choices[0].message.content or "")
    try:
        repaired = json.loads(repaired_content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Automatic JSON repair also failed for {label}.\n\nReturned text:\n{repaired_content}"
        ) from exc
    stats = usage_and_cost(response.usage)
    print(f"JSON repair succeeded for {label}.")
    return repaired, stats


def relevant_glossary(chinese_text):
    relevant = []
    for chinese_term, entry in glossary["entries"].items():
        if chinese_term not in chinese_text:
            continue
        item = {
            "chinese": chinese_term,
            "english": entry.get("english", ""),
            "category": entry.get("category", ""),
            "status": entry.get("status", "provisional"),
            "notes": entry.get("notes", ""),
            "alias_of": entry.get("alias_of", ""),
        }
        if entry.get("status") == "contextual":
            item["approved_renderings"] = entry.get("approved_renderings", [])
        relevant.append(item)
    return relevant


def glossary_to_text(entries):
    if not entries:
        return "No established glossary entries apply."
    lines = []
    for entry in entries:
        status = entry.get("status", "provisional")
        chinese = entry["chinese"]
        if status == "contextual":
            approved = entry.get("approved_renderings", [])
            lines.append(chinese)
            lines.append("  Status: CONTEXTUAL")
            if approved:
                lines.append("  Approved renderings: " + "; ".join(approved))
            if entry.get("notes"):
                lines.append("  Notes: " + entry["notes"])
        else:
            lines.append(f"{chinese} → {entry.get('english', '')}")
            lines.append(f"  Status: {status.upper()}")
            if entry.get("alias_of"):
                lines.append("  Alias of: " + entry["alias_of"])
            if entry.get("category"):
                lines.append("  Category: " + entry["category"])
            if entry.get("notes"):
                lines.append("  Notes: " + entry["notes"])
    return "\n".join(lines)


def translate_chapter(chapter):
    chapter_number = chapter["chapter"]
    chinese_text = chapter["text"]
    glossary_text = glossary_to_text(relevant_glossary(chinese_text))
    continuity_text = continuity.get("summary", "").strip() or "No previous continuity summary is available."
    user_prompt = f"""
ESTABLISHED GLOSSARY FOR THIS CHAPTER:

{glossary_text}

RECENT CONTINUITY:

{continuity_text}

CHINESE SOURCE — CHAPTER {chapter_number}:

{chinese_text}
""".strip()
    print("\n" + "=" * 70)
    print(f"TRANSLATING CHAPTER {chapter_number}")
    print("=" * 70)
    start = time.perf_counter()
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=12000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    elapsed = time.perf_counter() - start
    choice = response.choices[0]
    translation = choice.message.content or ""
    stats = usage_and_cost(response.usage)
    print(f"Translation finished in {elapsed:.2f} sec")
    print(f"Input: {stats['prompt_tokens']:,}")
    print(f"Output: {stats['completion_tokens']:,}")
    print(f"Cost: ${stats['cost_usd']:.6f}")
    return translation, stats, elapsed, choice.finish_reason


STATE_PROMPT = """
You maintain terminology and immediate continuity for a Chinese webnovel translation.
Analyze the supplied Chinese chapter and final English translation.
Return ONLY valid JSON:
{
  "new_terms": [{"chinese":"...","english":"...","category":"...","status":"provisional","notes":"..."}],
  "continuity_summary":"...",
  "issues":[]
}
Only propose a term if it is genuinely useful for consistency in future chapters.
Use ONLY these categories: character, alias, form of address, historical person,
historical office, military rank, military unit, state, organization, faction,
sect, location, technique, artifact, specialized recurring term.
Do not propose ordinary vocabulary, one-time descriptions, ordinary objects,
generic duties, generic tactical phrases, website/promotional language, or ordinary idioms.
Do not propose a term already present in the supplied glossary list.
All new terms must remain provisional.
Keep the continuity summary concise and useful only for the NEXT chapter.
Report damaged or ambiguous source text under issues.
Return JSON only.
""".strip()

STATE_SCHEMA_HINT = '{"new_terms":[],"continuity_summary":"...","issues":[]}'


def extract_state(chapter_number, chinese_text, translation):
    existing_terms = list(glossary["entries"].keys())
    user_prompt = f"""
CHAPTER: {chapter_number}
EXISTING GLOSSARY TERMS:
{json.dumps(existing_terms, ensure_ascii=False)}
CHINESE SOURCE:
{chinese_text}
FINAL ENGLISH:
{translation}
""".strip()
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": STATE_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=3000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    content = clean_json_text(response.choices[0].message.content or "")
    original_stats = usage_and_cost(response.usage)
    try:
        return json.loads(content), original_stats
    except json.JSONDecodeError:
        repaired, repair_stats = repair_json_content(
            f"State JSON for Chapter {chapter_number}", content, STATE_SCHEMA_HINT
        )
        return repaired, combine_stats(original_stats, repair_stats)


def local_qa(chapter_number, chinese_text, translation, finish_reason):
    found = []
    if finish_reason != "stop":
        found.append("Unexpected finish reason: " + str(finish_reason))
    if not translation.strip():
        found.append("Translation is empty")
    if len(translation) < 1000:
        found.append("Translation suspiciously short")
    remaining_chinese = len(CJK_RE.findall(translation))
    if remaining_chinese > 10:
        found.append(f"Remaining Chinese characters: {remaining_chinese}")
    if "[TRANSLATION REVIEW REQUIRED]" in translation:
        found.append("Translation contains review marker")
    source_hangul = len(HANGUL_RE.findall(chinese_text))
    return {
        "chapter": chapter_number,
        "source_hangul_characters": source_hangul,
        "remaining_chinese_characters": remaining_chinese,
        "translation_characters": len(translation),
        "source_characters": len(chinese_text),
        "issues": found,
    }


ALLOWED_GLOSSARY_CATEGORIES = {
    "character", "alias", "form of address", "historical person", "historical office",
    "military rank", "military unit", "state", "organization", "faction", "sect",
    "location", "technique", "artifact", "specialized recurring term",
}
LOW_VALUE_TERMS = {
    "紅票", "红票", "負距離", "负距离", "磚家", "砖家", "手藝人", "手艺人",
    "狼皮座椅", "商隊", "商队", "突進之策", "突进之策", "白毛風", "白毛风",
    "寶豐堂後門", "宝丰堂后门", "倒夜壺", "倒夜壶", "西洋鏡", "西洋镜",
}


def normalize_category(category):
    return (category or "").strip().lower().replace("_", " ").replace("-", " ")


def looks_like_sub_location(chinese, english):
    generic_suffixes = (
        "後門", "后门", "前門", "前门", "後院", "后院", "前院", "大門", "大门",
        "側門", "侧门", "內院", "内院", "外院", "倉庫", "仓库", "廚房", "厨房",
    )
    if chinese.endswith(generic_suffixes):
        return True
    english_lower = english.lower()
    generic_english_parts = (
        "back gate", "front gate", "rear courtyard", "front courtyard",
        "inner courtyard", "outer courtyard", "warehouse", "kitchen",
    )
    return any(part in english_lower for part in generic_english_parts)


def looks_like_generic_object(category, english):
    if category != "artifact":
        return False
    words = ("chair", "table", "mirror", "bowl", "cup", "door", "gate", "box", "basket", "bed", "lamp", "lantern", "pot", "jar")
    return any(word in english.lower() for word in words)


def looks_like_generic_activity(category, english):
    if category != "specialized recurring term":
        return False
    words = ("duty", "patrol", "work", "strategy", "attack", "retreat", "guard duty", "night patrol")
    return any(word in english.lower() for word in words)


def prefilter_candidates(new_terms):
    eligible, rejected = [], []
    for proposal in new_terms:
        chinese = proposal.get("chinese", "").strip()
        english = proposal.get("english", "").strip()
        category = normalize_category(proposal.get("category", ""))
        proposal = dict(proposal)
        proposal.update({"chinese": chinese, "english": english, "category": category, "status": "provisional"})
        if not chinese or not english:
            rejected.append({"chinese": chinese, "reason": "missing Chinese or English rendering"})
            continue
        if chinese in glossary["entries"]:
            continue
        if chinese in LOW_VALUE_TERMS:
            rejected.append({"chinese": chinese, "reason": "low-value/blocklisted term"})
            continue
        if category not in ALLOWED_GLOSSARY_CATEGORIES:
            rejected.append({"chinese": chinese, "reason": f"category not approved: {category or '(blank)'}"})
            continue
        if category == "location" and looks_like_sub_location(chinese, english):
            rejected.append({"chinese": chinese, "reason": "generic sub-location"})
            continue
        if looks_like_generic_object(category, english):
            rejected.append({"chinese": chinese, "reason": "generic object"})
            continue
        if looks_like_generic_activity(category, english):
            rejected.append({"chinese": chinese, "reason": "generic activity"})
            continue
        if len(chinese) > 12 and category not in {"organization", "military unit", "historical office", "specialized recurring term"}:
            rejected.append({"chinese": chinese, "reason": "suspiciously long phrase-like term"})
            continue
        eligible.append(proposal)
    return eligible, rejected


TERM_REVIEW_PROMPT = """
You are the conservative terminology reviewer for a Chinese-to-English historical webnovel.
Review ONLY new glossary candidates before they enter the persistent glossary.
Prefer Chinese historical/cultural fidelity. Do not unnecessarily Westernize institutions.
Proper names normally use established romanization. Descriptive terms should use meaningful English.
For each candidate choose exactly one action: accept, correct, reject, contextual, merge.
accept = good as proposed.
correct = belongs in glossary but English/romanization is wrong; supply corrected English.
reject = not worth keeping in master glossary.
contextual = needs multiple approved English renderings; supply approved_renderings.
merge = variant/alias/damaged-source form of an EXISTING Chinese glossary key; supply merge_with.
Never contradict a LOCKED existing term. New entries remain provisional unless aliasing a locked entry.
Return ONLY JSON:
{"decisions":[{"chinese":"...","action":"accept","english":"...","category":"...","approved_renderings":[],"merge_with":"","notes":""}],"issues":[]}
""".strip()
TERM_REVIEW_SCHEMA = '{"decisions":[],"issues":[]}'


def review_new_terms(chapter_number, chinese_text, translation, candidates):
    if not candidates:
        return {"decisions": [], "issues": []}, zero_stats()
    existing_relevant = glossary_to_text(relevant_glossary(chinese_text))
    user_prompt = f"""
CHAPTER: {chapter_number}
EXISTING RELEVANT GLOSSARY:
{existing_relevant}
NEW CANDIDATES:
{json.dumps(candidates, ensure_ascii=False, indent=2)}
CHINESE SOURCE:
{chinese_text}
FINAL ENGLISH:
{translation}
""".strip()
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": TERM_REVIEW_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    content = clean_json_text(response.choices[0].message.content or "")
    original_stats = usage_and_cost(response.usage)
    try:
        return json.loads(content), original_stats
    except json.JSONDecodeError:
        repaired, repair_stats = repair_json_content(
            f"Glossary review JSON for Chapter {chapter_number}", content, TERM_REVIEW_SCHEMA
        )
        return repaired, combine_stats(original_stats, repair_stats)


def apply_term_review(chapter_number, candidates, review_result):
    added, rejected, corrected, contextualized, merged = [], [], [], [], []
    decisions = {
        item.get("chinese", ""): item
        for item in review_result.get("decisions", [])
        if item.get("chinese")
    }
    for candidate in candidates:
        chinese = candidate["chinese"]
        decision = decisions.get(chinese, {
            "chinese": chinese, "action": "accept", "english": candidate["english"],
            "category": candidate["category"],
            "notes": "Reviewer omitted candidate; accepted provisionally using original proposal.",
        })
        action = decision.get("action", "accept").strip().lower()
        category = normalize_category(decision.get("category", candidate.get("category", "")))
        notes = (decision.get("notes", candidate.get("notes", "")) or "").strip()
        if action == "reject":
            rejected.append({"chinese": chinese, "reason": notes or "reviewer rejected"})
            continue
        if action == "merge":
            merge_with = decision.get("merge_with", "").strip()
            if merge_with and merge_with in glossary["entries"]:
                target = glossary["entries"][merge_with]
                glossary["entries"][chinese] = {
                    "english": target.get("english", candidate["english"]),
                    "category": target.get("category", category),
                    "status": "locked" if target.get("status") == "locked" else "provisional",
                    "first_chapter": chapter_number,
                    "alias_of": merge_with,
                    "notes": notes or f"Alias/variant of {merge_with}.",
                }
                merged.append(f"{chinese} → {merge_with}")
                continue
            action = "accept"
        if action == "contextual":
            approved = [x.strip() for x in decision.get("approved_renderings", []) if isinstance(x, str) and x.strip()]
            if not approved:
                approved = [candidate["english"]]
            glossary["entries"][chinese] = {
                "english": "", "approved_renderings": approved, "category": category,
                "status": "contextual", "first_chapter": chapter_number, "notes": notes,
            }
            contextualized.append(f"{chinese} → {' / '.join(approved)}")
            continue
        english = (decision.get("english", candidate["english"]) or candidate["english"]).strip()
        if action == "correct":
            corrected.append(f"{chinese}: {candidate['english']} → {english}")
        glossary["entries"][chinese] = {
            "english": english, "category": category, "status": "provisional",
            "first_chapter": chapter_number, "notes": notes,
        }
        added.append(f"{chinese} → {english}")
    return {"added": added, "rejected": rejected, "corrected": corrected, "contextualized": contextualized, "merged": merged}


AUDIT_PROMPT = """
You are performing a conservative periodic audit of a persistent Chinese-to-English historical-webnovel glossary.
Review ONLY the supplied audit-target entries. LOCKED terms are authoritative and must never be changed.
For each target choose exactly one action: keep, correct, reject, contextual, merge.
Correct only real semantic, historical, geographic, romanization, consistency, or glossary-value problems.
If uncertain, choose keep. For merge, merge_with must be an existing Chinese glossary key.
Return ONLY JSON:
{"decisions":[{"chinese":"...","action":"keep","english":"...","approved_renderings":[],"merge_with":"","notes":""}],"issues":[]}
""".strip()
AUDIT_SCHEMA = '{"decisions":[],"issues":[]}'


def compact_glossary_index(max_chars=MAX_AUDIT_GLOSSARY_CHARS):
    entries = glossary["entries"]
    prioritized = sorted(
        entries.items(),
        key=lambda kv: (
            0 if kv[1].get("status") == "locked" else 1 if kv[1].get("status") == "contextual" else 2,
            kv[1].get("first_chapter", 0),
        ),
    )
    lines, total = [], 0
    for chinese, entry in prioritized:
        english = entry.get("english", "")
        if entry.get("status") == "contextual":
            english = " / ".join(entry.get("approved_renderings", []))
        line = f"{chinese} → {english} [{entry.get('status','provisional')}; {entry.get('category','')}]"
        if entry.get("alias_of"):
            line += f" [alias_of={entry['alias_of']}]"
        line += "\n"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "".join(lines)


def source_contexts_for_term(chinese_term, start_chapter, end_chapter):
    contexts = []
    for chapter_number in range(start_chapter, end_chapter + 1):
        chapter = chapter_map.get(chapter_number)
        if not chapter:
            continue
        text = chapter.get("text", "")
        start = 0
        while len(contexts) < MAX_TERM_CONTEXTS:
            idx = text.find(chinese_term, start)
            if idx == -1:
                break
            left = max(0, idx - TERM_CONTEXT_RADIUS)
            right = min(len(text), idx + len(chinese_term) + TERM_CONTEXT_RADIUS)
            contexts.append({"chapter": chapter_number, "context": text[left:right]})
            start = idx + len(chinese_term)
        if len(contexts) >= MAX_TERM_CONTEXTS:
            break
    return contexts


def audit_targets(start_chapter, end_chapter):
    targets = []
    for chinese, entry in glossary["entries"].items():
        first_chapter = entry.get("first_chapter", 0)
        if not (start_chapter <= first_chapter <= end_chapter):
            continue
        if entry.get("status") == "locked":
            continue
        targets.append({
            "chinese": chinese,
            "english": entry.get("english", ""),
            "approved_renderings": entry.get("approved_renderings", []),
            "category": entry.get("category", ""),
            "status": entry.get("status", "provisional"),
            "alias_of": entry.get("alias_of", ""),
            "notes": entry.get("notes", ""),
            "contexts": source_contexts_for_term(chinese, start_chapter, end_chapter),
        })
    return targets


def call_periodic_audit(start_chapter, end_chapter):
    targets = audit_targets(start_chapter, end_chapter)
    if not targets:
        return {"decisions": [], "issues": []}, zero_stats()
    user_prompt = f"""
AUDIT RANGE: Chapters {start_chapter}-{end_chapter}
CURRENT GLOSSARY INDEX:
{compact_glossary_index()}
AUDIT TARGETS:
{json.dumps(targets, ensure_ascii=False, indent=2)}
""".strip()
    response = call_api_with_retries(
        model=MODEL,
        messages=[
            {"role": "system", "content": AUDIT_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=6000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    content = clean_json_text(response.choices[0].message.content or "")
    original_stats = usage_and_cost(response.usage)
    try:
        return json.loads(content), original_stats
    except json.JSONDecodeError:
        repaired, repair_stats = repair_json_content(
            f"Periodic glossary audit JSON for Chapters {start_chapter}-{end_chapter}",
            content, AUDIT_SCHEMA,
        )
        return repaired, combine_stats(original_stats, repair_stats)


def apply_periodic_audit(start_chapter, end_chapter, audit_result):
    entries = glossary["entries"]
    kept, corrected, rejected, contextualized, merged = [], [], [], [], []
    decisions = {
        item.get("chinese", ""): item
        for item in audit_result.get("decisions", [])
        if item.get("chinese")
    }
    for target in audit_targets(start_chapter, end_chapter):
        chinese = target["chinese"]
        if chinese not in entries:
            continue
        entry = entries[chinese]
        if entry.get("status") == "locked":
            continue
        decision = decisions.get(chinese, {"action": "keep", "notes": "Auditor omitted entry; kept conservatively."})
        action = decision.get("action", "keep").strip().lower()
        notes = (decision.get("notes", "") or "").strip()
        if action == "reject":
            del entries[chinese]
            rejected.append(chinese)
            continue
        if action == "merge":
            merge_with = decision.get("merge_with", "").strip()
            if merge_with and merge_with in entries and merge_with != chinese:
                target_entry = entries[merge_with]
                entry["english"] = target_entry.get("english", entry.get("english", ""))
                entry["category"] = target_entry.get("category", entry.get("category", ""))
                entry["alias_of"] = merge_with
                entry["status"] = "locked" if target_entry.get("status") == "locked" else "provisional"
                if notes:
                    entry["notes"] = notes
                merged.append(f"{chinese} → {merge_with}")
                continue
            action = "keep"
        if action == "contextual":
            approved = [x.strip() for x in decision.get("approved_renderings", []) if isinstance(x, str) and x.strip()]
            if approved:
                entry["english"] = ""
                entry["approved_renderings"] = approved
                entry["status"] = "contextual"
                if notes:
                    entry["notes"] = notes
                contextualized.append(f"{chinese} → {' / '.join(approved)}")
                continue
            action = "keep"
        if action == "correct":
            new_english = (decision.get("english", "") or "").strip()
            if new_english:
                old_english = entry.get("english", "")
                entry["english"] = new_english
                entry["status"] = "provisional"
                if notes:
                    entry["notes"] = notes
                corrected.append(f"{chinese}: {old_english} → {new_english}")
                continue
            action = "keep"
        kept.append(chinese)
    return {"kept": kept, "corrected": corrected, "rejected": rejected, "contextualized": contextualized, "merged": merged}


def create_checkpoint(chapter_number):
    checkpoint_dir = BACKUP_DIR / f"checkpoint_{chapter_number:04d}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    for path in (GLOSSARY_FILE, PROGRESS_FILE, CONTINUITY_FILE, ISSUES_FILE, AUDIT_HISTORY_FILE):
        if path.exists():
            shutil.copy2(path, checkpoint_dir / path.name)


def run_periodic_audit(start_chapter, end_chapter):
    print("\n" + "=" * 70)
    print(f"AUTOMATIC GLOSSARY AUDIT — CHAPTERS {start_chapter}-{end_chapter}")
    print("=" * 70)
    audit_result, audit_stats = call_periodic_audit(start_chapter, end_chapter)
    changes = apply_periodic_audit(start_chapter, end_chapter, audit_result)
    add_stats_to_project(audit_stats)
    audit_record = {
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "timestamp_unix": time.time(),
        "stats": audit_stats,
        "changes": changes,
        "issues": audit_result.get("issues", []),
    }
    audit_history["audits"].append(audit_record)
    audit_file = AUDIT_DIR / f"audit_{start_chapter:04d}_{end_chapter:04d}.json"
    save_json(audit_file, audit_record)
    progress["last_audited_chapter"] = end_chapter
    for issue in audit_result.get("issues", []):
        issues_data["issues"].append({
            "chapter": end_chapter,
            "type": "periodic_glossary_audit",
            "details": issue,
        })
    save_json(GLOSSARY_FILE, glossary)
    save_json(PROGRESS_FILE, progress)
    save_json(ISSUES_FILE, issues_data)
    save_json(AUDIT_HISTORY_FILE, audit_history)
    create_checkpoint(end_chapter)
    print(f"Kept: {len(changes['kept'])}")
    print(f"Corrected: {len(changes['corrected'])}")
    print(f"Rejected/removed: {len(changes['rejected'])}")
    print(f"Contextualized: {len(changes['contextualized'])}")
    print(f"Merged aliases: {len(changes['merged'])}")
    print(f"Audit cost: ${audit_stats['cost_usd']:.6f}")
    print(f"Checkpoint saved: {BACKUP_DIR / f'checkpoint_{end_chapter:04d}'}")
    print("=" * 70)


def describe_api_error(exc):
    text = str(exc).lower()
    if any(x in text for x in ("balance", "credit", "billing", "insufficient")):
        return "The API may have run out of balance/credit. Add credit and run the same script again."
    return "Check the API error above. After resolving it, run the same script again."


def pending_paths(chapter_number):
    return (
        PENDING_DIR / f"{chapter_number:04d}.md",
        PENDING_DIR / f"{chapter_number:04d}.json",
    )


def process_chapter(chapter_number):
    chapter = chapter_map[chapter_number]
    chinese_text = chapter["text"]
    final_file = OUTPUT_DIR / f"{chapter_number:04d}.md"
    pending_file, pending_meta_file = pending_paths(chapter_number)
    if chapter_number in progress["completed"]:
        return True
    meta = load_json(pending_meta_file, {}) if pending_meta_file.exists() else {}

    if pending_file.exists():
        translation = pending_file.read_text(encoding="utf-8")
        translation_stats = meta.get("translation_stats", zero_stats())
        elapsed = meta.get("elapsed_seconds", 0.0)
        finish_reason = meta.get("finish_reason", "stop")
        print(f"\nRecovered pending translation for Chapter {chapter_number}.")
        print("The translation will NOT be generated again.")
    else:
        try:
            translation, translation_stats, elapsed, finish_reason = translate_chapter(chapter)
        except (APIError, APIConnectionError, APITimeoutError, RateLimitError) as exc:
            print("\n" + "=" * 70)
            print("API ERROR DURING TRANSLATION")
            print("=" * 70)
            print(f"Chapter: {chapter_number}\nError: {exc}\n")
            print(describe_api_error(exc))
            return False
        if not translation.strip():
            print(f"Chapter {chapter_number}: empty translation. Stopping safely.")
            return False
        if finish_reason != "stop":
            print(f"Chapter {chapter_number}: unexpected finish reason {finish_reason}. Stopping safely.")
            return False
        pending_file.write_text(translation, encoding="utf-8")
        meta = {
            "chapter": chapter_number,
            "stage": "translated",
            "translation_stats": translation_stats,
            "elapsed_seconds": elapsed,
            "finish_reason": finish_reason,
        }
        save_json(pending_meta_file, meta)
        print(f"Pending translation saved to: {pending_file}")

    qa = local_qa(chapter_number, chinese_text, translation, finish_reason)

    if meta.get("stage") in {"state_extracted", "reviewed"} and "state" in meta:
        state = meta["state"]
        state_stats = meta.get("state_stats", zero_stats())
        print(f"Recovered saved state extraction for Chapter {chapter_number}.")
    else:
        try:
            state, state_stats = extract_state(chapter_number, chinese_text, translation)
        except (APIError, APIConnectionError, APITimeoutError, RateLimitError, RuntimeError) as exc:
            print("\n" + "=" * 70)
            print("ERROR DURING STATE EXTRACTION")
            print("=" * 70)
            print(f"Chapter: {chapter_number}\nError: {exc}\n")
            print(describe_api_error(exc))
            print(f"\nThe paid translation remains safely saved in:\n{pending_file}")
            return False
        meta["stage"] = "state_extracted"
        meta["state"] = state
        meta["state_stats"] = state_stats
        save_json(pending_meta_file, meta)

    if meta.get("stage") == "reviewed" and "review_result" in meta:
        eligible_candidates = meta.get("eligible_candidates", [])
        prefilter_rejected = meta.get("prefilter_rejected", [])
        review_result = meta["review_result"]
        review_stats = meta.get("review_stats", zero_stats())
        print(f"Recovered saved glossary review for Chapter {chapter_number}.")
    else:
        eligible_candidates, prefilter_rejected = prefilter_candidates(state.get("new_terms", []))
        try:
            review_result, review_stats = review_new_terms(
                chapter_number, chinese_text, translation, eligible_candidates
            )
        except (APIError, APIConnectionError, APITimeoutError, RateLimitError, RuntimeError) as exc:
            print("\n" + "=" * 70)
            print("ERROR DURING GLOSSARY REVIEW")
            print("=" * 70)
            print(f"Chapter: {chapter_number}\nError: {exc}\n")
            print(describe_api_error(exc))
            print("\nTranslation and state extraction remain saved for recovery.")
            return False
        meta["stage"] = "reviewed"
        meta["eligible_candidates"] = eligible_candidates
        meta["prefilter_rejected"] = prefilter_rejected
        meta["review_result"] = review_result
        meta["review_stats"] = review_stats
        save_json(pending_meta_file, meta)

    glossary_changes = apply_term_review(chapter_number, eligible_candidates, review_result)
    glossary_changes["rejected"].extend(prefilter_rejected)
    continuity["latest_chapter"] = chapter_number
    continuity["summary"] = state.get("continuity_summary", "")
    for issue in state.get("issues", []):
        issues_data["issues"].append({"chapter": chapter_number, "type": "model_flag", "details": issue})
    for issue in review_result.get("issues", []):
        issues_data["issues"].append({"chapter": chapter_number, "type": "glossary_reviewer", "details": issue})
    if qa["issues"]:
        issues_data["issues"].append({"chapter": chapter_number, "type": "local_qa", "details": qa["issues"]})
    save_json(QA_DIR / f"{chapter_number:04d}.json", qa)

    chapter_stats = combine_stats(translation_stats, state_stats, review_stats)
    add_stats_to_project(chapter_stats)
    final_file.write_text(translation, encoding="utf-8")
    if chapter_number not in progress["completed"]:
        progress["completed"].append(chapter_number)
    progress["completed"].sort()
    progress["last_completed"] = chapter_number
    progress["next_chapter"] = chapter_number + 1
    if chapter_number in progress["failed"]:
        progress["failed"].remove(chapter_number)
    save_json(GLOSSARY_FILE, glossary)
    save_json(CONTINUITY_FILE, continuity)
    save_json(ISSUES_FILE, issues_data)
    save_json(PROGRESS_FILE, progress)
    save_json(AUDIT_HISTORY_FILE, audit_history)
    if pending_file.exists():
        pending_file.unlink()
    if pending_meta_file.exists():
        pending_meta_file.unlink()

    print(f"\nChapter {chapter_number} fully committed.")
    print(f"Glossary accepted: {len(glossary_changes['added'])}")
    print(f"Glossary corrected: {len(glossary_changes['corrected'])}")
    print(f"Glossary contextualized: {len(glossary_changes['contextualized'])}")
    print(f"Glossary merged aliases: {len(glossary_changes['merged'])}")
    print(f"Glossary rejected: {len(glossary_changes['rejected'])}")
    print(f"Chapter total cost: ${chapter_stats['cost_usd']:.6f}")
    print(f"Project total cost: ${progress['estimated_cost_usd']:.6f}")
    print(f"Next chapter: {progress['next_chapter']}")
    return True


def run_due_audits():
    while progress["last_completed"] - progress["last_audited_chapter"] >= AUDIT_INTERVAL:
        start_chapter = progress["last_audited_chapter"] + 1
        end_chapter = progress["last_audited_chapter"] + AUDIT_INTERVAL
        run_periodic_audit(start_chapter, end_chapter)


def run_final_audit_if_needed():
    if progress["last_completed"] < FINAL_CHAPTER:
        return
    if progress["last_audited_chapter"] >= FINAL_CHAPTER:
        return
    run_periodic_audit(progress["last_audited_chapter"] + 1, FINAL_CHAPTER)


print("\n" + "=" * 70)
print("DEEPSEEK NOVEL TRANSLATION — UNATTENDED PRODUCTION")
print("=" * 70)
print(f"Final chapter: {FINAL_CHAPTER}")
print("Automatic off-peak-only mode: " + ("ON" if AUTO_OFF_PEAK_ONLY else "OFF"))
print(f"Manual pause file: {MANUAL_PAUSE_FILE}")
print(f"Completed through: {progress['last_completed']}")
print(f"Next chapter: {progress['next_chapter']}")
print(f"Last audited chapter: {progress['last_audited_chapter']}")
print(f"Current glossary entries: {len(glossary['entries'])}")
print("=" * 70)

try:
    run_due_audits()
    start_chapter = progress["next_chapter"]
    if start_chapter <= FINAL_CHAPTER:
        for chapter_number in range(start_chapter, FINAL_CHAPTER + 1):
            if not process_chapter(chapter_number):
                print("\n" + "=" * 70)
                print(f"STOPPED SAFELY AT CHAPTER {chapter_number}")
                print(f"Last committed: {progress['last_completed']}")
                print(f"Next chapter: {progress['next_chapter']}")
                print("=" * 70)
                raise SystemExit(1)
            run_due_audits()
    run_final_audit_if_needed()
except KeyboardInterrupt:
    print("\n" + "=" * 70)
    print("MANUAL STOP REQUESTED")
    print("=" * 70)
    print(f"Last committed chapter: {progress['last_completed']}")
    print(f"Next chapter: {progress['next_chapter']}")
    print("Any already-saved pending stage will be reused when the script runs again.")
    print("Safest planned stop: create pause.flag, wait for 'MANUAL PAUSE ACTIVE', then press Ctrl+C.")
    print("=" * 70)
    raise SystemExit(0)

except (APIError, APIConnectionError, APITimeoutError, RateLimitError, RuntimeError) as exc:
    print("\n" + "=" * 70)
    print("UNATTENDED PRODUCTION STOPPED SAFELY")
    print("=" * 70)
    print(f"Error: {exc}\n")
    print(describe_api_error(exc))
    print(f"\nLast committed chapter: {progress['last_completed']}")
    print(f"Next chapter: {progress['next_chapter']}")
    print("Run this same script again after resolving the problem.")
    print("=" * 70)
    raise SystemExit(1)

print("\n" + "=" * 70)
print("NOVEL TRANSLATION COMPLETE")
print("=" * 70)
print(f"Completed through Chapter {progress['last_completed']}")
print(f"Last audited chapter: {progress['last_audited_chapter']}")
print(f"Total input tokens: {progress['total_prompt_tokens']:,}")
print(f"Total output tokens: {progress['total_completion_tokens']:,}")
print(f"Recorded API cost: ${progress['estimated_cost_usd']:.6f}")
print(f"Final glossary entries: {len(glossary['entries'])}")
print("=" * 70)
