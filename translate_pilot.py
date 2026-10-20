import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# ============================================================
# CONFIG
# ============================================================

MODEL = "deepseek-v4-pro"

START_CHAPTER = 1
END_CHAPTER = 10

PRICE_CACHE_MISS_INPUT = 0.435
PRICE_CACHE_HIT_INPUT = 0.003625
PRICE_OUTPUT = 0.87

CHAPTERS_FILE = Path("data/chapters.json")
GLOSSARY_FILE = Path("data/glossary.json")
PROGRESS_FILE = Path("data/progress.json")
CONTINUITY_FILE = Path("data/continuity.json")
ISSUES_FILE = Path("data/issues.json")

TRANSLATION_PROMPT_FILE = Path(
    "production_translation.txt"
)

OUTPUT_DIR = Path("output/chapters")
QA_DIR = Path("output/qa")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
QA_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# API
# ============================================================

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key:
    raise RuntimeError(
        "DEEPSEEK_API_KEY was not found in .env"
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)


# ============================================================
# LOAD DATA
# ============================================================

with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)

chapter_map = {
    item["chapter"]: item
    for item in chapters
}


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(path)


glossary = load_json(GLOSSARY_FILE)
progress = load_json(PROGRESS_FILE)
continuity = load_json(CONTINUITY_FILE)
issues_data = load_json(ISSUES_FILE)

system_prompt = TRANSLATION_PROMPT_FILE.read_text(
    encoding="utf-8"
)


# ============================================================
# REGEX
# ============================================================

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

HANGUL_RE = re.compile(
    r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"
)


# ============================================================
# COST
# ============================================================

def usage_and_cost(usage):
    prompt_tokens = getattr(
        usage,
        "prompt_tokens",
        0,
    ) or 0

    completion_tokens = getattr(
        usage,
        "completion_tokens",
        0,
    ) or 0

    cache_hits = getattr(
        usage,
        "prompt_cache_hit_tokens",
        0,
    ) or 0

    cache_misses = getattr(
        usage,
        "prompt_cache_miss_tokens",
        0,
    ) or 0

    if cache_hits == 0 and cache_misses == 0:
        cache_misses = prompt_tokens

    cost = (
        cache_misses / 1_000_000
        * PRICE_CACHE_MISS_INPUT
        +
        cache_hits / 1_000_000
        * PRICE_CACHE_HIT_INPUT
        +
        completion_tokens / 1_000_000
        * PRICE_OUTPUT
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
    }


# ============================================================
# GLOSSARY RETRIEVAL
# ============================================================

def relevant_glossary(chinese_text):
    """
    Only include glossary entries whose Chinese term appears
    in the current chapter.

    This is deliberately simple for the 10-chapter pilot.
    """

    relevant = []

    for chinese_term, entry in glossary["entries"].items():
        if chinese_term in chinese_text:
            relevant.append(
                {
                    "chinese": chinese_term,
                    "english": entry["english"],
                    "category": entry.get(
                        "category",
                        "",
                    ),
                    "status": entry.get(
                        "status",
                        "provisional",
                    ),
                    "notes": entry.get(
                        "notes",
                        "",
                    ),
                }
            )

    return relevant


def glossary_to_text(entries):
    if not entries:
        return "No established glossary entries apply."

    lines = []

    for entry in entries:
        lines.append(
            f"{entry['chinese']} → "
            f"{entry['english']}"
        )

        if entry.get("category"):
            lines.append(
                f"  Category: {entry['category']}"
            )

        if entry.get("notes"):
            lines.append(
                f"  Notes: {entry['notes']}"
            )

    return "\n".join(lines)


# ============================================================
# TRANSLATION
# ============================================================

def translate_chapter(chapter):
    chapter_number = chapter["chapter"]
    chinese_text = chapter["text"]

    relevant = relevant_glossary(chinese_text)
    glossary_text = glossary_to_text(relevant)

    continuity_text = continuity.get(
        "summary",
        "",
    ).strip()

    if not continuity_text:
        continuity_text = (
            "No previous continuity summary. "
            "This is the beginning of the production run."
        )

    user_prompt = f"""
ESTABLISHED GLOSSARY FOR THIS CHAPTER:

{glossary_text}


RECENT CONTINUITY:

{continuity_text}


CHINESE SOURCE — CHAPTER {chapter_number}:

{chinese_text}
""".strip()

    print()
    print("=" * 70)
    print(f"TRANSLATING CHAPTER {chapter_number}")
    print("=" * 70)

    start = time.perf_counter()

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        max_tokens=12000,
        extra_body={
            "thinking": {
                "type": "disabled"
            }
        },
    )

    elapsed = time.perf_counter() - start

    choice = response.choices[0]
    translation = choice.message.content or ""

    stats = usage_and_cost(response.usage)

    print(
        f"Translation finished in "
        f"{elapsed:.2f} sec"
    )

    print(
        f"Input: "
        f"{stats['prompt_tokens']:,}"
    )

    print(
        f"Output: "
        f"{stats['completion_tokens']:,}"
    )

    print(
        f"Cost: "
        f"${stats['cost_usd']:.6f}"
    )

    return (
        translation,
        stats,
        elapsed,
        choice.finish_reason,
    )


# ============================================================
# STATE EXTRACTION
# ============================================================

STATE_PROMPT = """
You are maintaining translation continuity for a Chinese webnovel.

Analyze the supplied Chinese chapter and its final English translation.

Return ONLY valid JSON.

The JSON must have this exact structure:

{
  "new_terms": [
    {
      "chinese": "...",
      "english": "...",
      "category": "...",
      "status": "provisional",
      "notes": "..."
    }
  ],
  "continuity_summary": "...",
  "issues": [
    "..."
  ]
}

GLOSSARY RULES:

Only include genuinely reusable terminology such as:

- character names
- aliases
- forms of address
- historical offices
- military ranks
- military units
- organizations
- factions
- sects
- locations
- techniques
- artifacts
- recurring specialized terminology
- historically important recurring terminology

Do NOT include ordinary vocabulary.

Do NOT include a term already present in the supplied existing glossary.

The English rendering MUST match the final English translation.

Prefer compact notes.

Set status to "provisional".

CONTINUITY SUMMARY:

Write a compact continuity summary for the NEXT chapter.

Maximum approximately 250 words.

Focus on:

- active characters
- locations
- relationships
- immediate events
- unresolved narrative facts
- information needed to interpret pronouns/references in the next chapter

Do not summarize the entire novel.

ISSUES:

Only include genuine translation uncertainties, source corruption,
possible ambiguity, or places marked [TRANSLATION REVIEW REQUIRED].

If there are none, use an empty list.

Return JSON only.
""".strip()


def extract_state(
    chapter_number,
    chinese_text,
    translation,
):
    existing_terms = list(
        glossary["entries"].keys()
    )

    user_prompt = f"""
CHAPTER: {chapter_number}

EXISTING GLOSSARY TERMS:

{json.dumps(existing_terms, ensure_ascii=False)}


CHINESE SOURCE:

{chinese_text}


FINAL ENGLISH:

{translation}
""".strip()

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": STATE_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        max_tokens=3000,
        extra_body={
            "thinking": {
                "type": "disabled"
            }
        },
    )

    content = response.choices[0].message.content or ""

    # Remove accidental Markdown fences.
    content = content.strip()

    if content.startswith("```json"):
        content = content[7:]

    elif content.startswith("```"):
        content = content[3:]

    if content.endswith("```"):
        content = content[:-3]

    content = content.strip()

    try:
        state = json.loads(content)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"State JSON parsing failed for "
            f"Chapter {chapter_number}:\n\n"
            f"{content}"
        ) from exc

    stats = usage_and_cost(response.usage)

    return state, stats


# ============================================================
# LOCAL QA
# ============================================================

def local_qa(
    chapter_number,
    chinese_text,
    translation,
    finish_reason,
):
    found = []

    if finish_reason != "stop":
        found.append(
            f"Unexpected finish reason: {finish_reason}"
        )

    if not translation.strip():
        found.append("Translation is empty")

    if len(translation) < 1000:
        found.append(
            "Translation suspiciously short"
        )

    remaining_chinese = len(
        CJK_RE.findall(translation)
    )

    if remaining_chinese > 10:
        found.append(
            f"Remaining Chinese characters: "
            f"{remaining_chinese}"
        )

    if "[TRANSLATION REVIEW REQUIRED]" in translation:
        found.append(
            "Translation contains review marker"
        )

    source_hangul = len(
        HANGUL_RE.findall(chinese_text)
    )

    return {
        "chapter": chapter_number,
        "source_hangul_characters": source_hangul,
        "remaining_chinese_characters": remaining_chinese,
        "issues": found,
    }


# ============================================================
# GLOSSARY UPDATE
# ============================================================

def apply_new_terms(
    chapter_number,
    new_terms,
):
    added = []

    for proposal in new_terms:
        chinese = proposal.get(
            "chinese",
            "",
        ).strip()

        english = proposal.get(
            "english",
            "",
        ).strip()

        if not chinese or not english:
            continue

        if chinese in glossary["entries"]:
            continue

        glossary["entries"][chinese] = {
            "english": english,
            "category": proposal.get(
                "category",
                "",
            ),
            "status": "provisional",
            "first_chapter": chapter_number,
            "notes": proposal.get(
                "notes",
                "",
            ),
        }

        added.append(
            f"{chinese} → {english}"
        )

    return added


# ============================================================
# PROCESS ONE CHAPTER
# ============================================================

def process_chapter(chapter_number):
    chapter = chapter_map[chapter_number]
    chinese_text = chapter["text"]

    output_file = (
        OUTPUT_DIR
        / f"{chapter_number:04d}.md"
    )

    # Safety: don't overwrite existing work.
    if output_file.exists():
        print(
            f"Chapter {chapter_number} already exists. "
            f"Skipping."
        )

        return True

    translation, translation_stats, elapsed, finish_reason = (
        translate_chapter(chapter)
    )

    qa = local_qa(
        chapter_number,
        chinese_text,
        translation,
        finish_reason,
    )

    # Do not commit obviously broken output.
    serious_problem = (
        not translation.strip()
        or finish_reason != "stop"
    )

    if serious_problem:
        progress["failed"].append(
            chapter_number
        )

        issues_data["issues"].append(
            {
                "chapter": chapter_number,
                "type": "generation_failure",
                "details": qa["issues"],
            }
        )

        save_json(
            PROGRESS_FILE,
            progress,
        )

        save_json(
            ISSUES_FILE,
            issues_data,
        )

        return False

    # Save translation BEFORE secondary state call.
    output_file.write_text(
        translation,
        encoding="utf-8",
    )

    # Extract glossary/continuity.
    state, state_stats = extract_state(
        chapter_number,
        chinese_text,
        translation,
    )

    new_terms = state.get(
        "new_terms",
        [],
    )

    added_terms = apply_new_terms(
        chapter_number,
        new_terms,
    )

    continuity["latest_chapter"] = (
        chapter_number
    )

    continuity["summary"] = state.get(
        "continuity_summary",
        "",
    )

    # Save state-extraction issues.
    for issue in state.get(
        "issues",
        [],
    ):
        issues_data["issues"].append(
            {
                "chapter": chapter_number,
                "type": "model_flag",
                "details": issue,
            }
        )

    # Save local QA issues.
    if qa["issues"]:
        issues_data["issues"].append(
            {
                "chapter": chapter_number,
                "type": "local_qa",
                "details": qa["issues"],
            }
        )

    # Save QA report.
    qa_file = (
        QA_DIR
        / f"{chapter_number:04d}.json"
    )

    save_json(
        qa_file,
        qa,
    )

    # Update token/cost totals.
    chapter_prompt_tokens = (
        translation_stats["prompt_tokens"]
        +
        state_stats["prompt_tokens"]
    )

    chapter_completion_tokens = (
        translation_stats["completion_tokens"]
        +
        state_stats["completion_tokens"]
    )

    chapter_cost = (
        translation_stats["cost_usd"]
        +
        state_stats["cost_usd"]
    )

    progress["total_prompt_tokens"] += (
        chapter_prompt_tokens
    )

    progress["total_completion_tokens"] += (
        chapter_completion_tokens
    )

    progress["estimated_cost_usd"] += (
        chapter_cost
    )

    if chapter_number not in progress["completed"]:
        progress["completed"].append(
            chapter_number
        )

    progress["last_completed"] = chapter_number
    progress["next_chapter"] = (
        chapter_number + 1
    )

    if chapter_number in progress["failed"]:
        progress["failed"].remove(
            chapter_number
        )

    # Commit persistent files.
    save_json(
        GLOSSARY_FILE,
        glossary,
    )

    save_json(
        CONTINUITY_FILE,
        continuity,
    )

    save_json(
        ISSUES_FILE,
        issues_data,
    )

    save_json(
        PROGRESS_FILE,
        progress,
    )

    print()
    print(
        f"Chapter {chapter_number} committed."
    )

    print(
        f"New glossary entries: "
        f"{len(added_terms)}"
    )

    for term in added_terms:
        print(f"  + {term}")

    print(
        f"Chapter total cost: "
        f"${chapter_cost:.6f}"
    )

    print(
        f"Project total cost: "
        f"${progress['estimated_cost_usd']:.6f}"
    )

    return True


# ============================================================
# MAIN
# ============================================================

print()
print("=" * 70)
print("DEEPSEEK NOVEL TRANSLATION — 10 CHAPTER PILOT")
print("=" * 70)

start_from = max(
    START_CHAPTER,
    progress.get(
        "next_chapter",
        START_CHAPTER,
    ),
)

for chapter_number in range(
    start_from,
    END_CHAPTER + 1,
):
    success = process_chapter(
        chapter_number
    )

    if not success:
        print()
        print(
            f"STOPPED at Chapter "
            f"{chapter_number}."
        )

        print(
            "Inspect the issue before resuming."
        )

        break

else:
    print()
    print("=" * 70)
    print("PILOT COMPLETE")
    print("=" * 70)

    print(
        f"Completed through Chapter "
        f"{progress['last_completed']}"
    )

    print(
        f"Next chapter: "
        f"{progress['next_chapter']}"
    )

    print(
        f"Total input tokens: "
        f"{progress['total_prompt_tokens']:,}"
    )

    print(
        f"Total output tokens: "
        f"{progress['total_completion_tokens']:,}"
    )

    print(
        f"Estimated API cost: "
        f"${progress['estimated_cost_usd']:.6f}"
    )

    print(
        f"Glossary entries: "
        f"{len(glossary['entries'])}"
    )

    print("=" * 70)