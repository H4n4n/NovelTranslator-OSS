import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from noveltranslator_core import (
    calculate_cost_from_token_counts,
    pricing_snapshot,
)


# ============================================================
# CONFIG
# ============================================================

MODEL = "deepseek-v4-pro"
CHAPTER_NUMBERS = [139, 273]

CHAPTERS_FILE = Path("data/chapters.json")
OUTPUT_DIR = Path("output/benchmark")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are translating a Chinese webnovel into polished literary English.

The original Chinese is the sole authoritative source.

Translate the complete chapter directly from Chinese into natural,
reader-facing English.

ACCURACY RULES

- Do not summarize.
- Do not abridge.
- Do not omit information.
- Do not invent information.
- Do not add explanations absent from the source.
- Resolve omitted Chinese subjects and pronouns from context.
- Preserve characterization, relationships, hierarchy, emotional tone,
  humor, implications, historical context, worldbuilding, and authorial intent.
- Preserve all narrative and dialogue.
- Naturalize awkward Chinese syntax where appropriate while preserving meaning.

TERMINOLOGY PHILOSOPHY

Preserve the Chinese conceptual and cultural character of terminology.

Prefer meaningful English translations for Chinese institutions,
organizations, sects, techniques, titles, and descriptive names when
an accurate English rendering is natural.

Do not unnecessarily replace Chinese institutions with approximate
Western equivalents.

For example, where historically and contextually appropriate:

百户 → Hundred-Household Commander

rather than:

Centurion

For fictional descriptive organization names, prefer forms such as:

碧云宗 → Azure Cloud Sect

rather than:

Biyun Sect

Personal names normally remain transliterated:

李元庆 → Li Yuanqing

Do not blindly apply these examples to unrelated terms.
Determine each term from context.

DAMAGED SOURCE TEXT

The source contains encoding corruption in which some intended Chinese
characters appear as Hangul or other incorrect glyphs.

When encountering corrupted text:

1. Examine surrounding Chinese.
2. Use the complete sentence and scene.
3. Use known historical names and terminology where appropriate.
4. Reconstruct the intended meaning only when context supports it.

Do not reproduce obvious encoding corruption in the English.

If an important passage cannot be interpreted reliably, use:

[TRANSLATION REVIEW REQUIRED]

rather than inventing unsupported content.

STYLE

The finished translation should read like professionally edited English
historical/genre web fiction rather than raw machine translation.

Do not unnecessarily embellish the author's prose.

Dialogue should sound natural while preserving character voice,
relationships, hierarchy, and tone.

OUTPUT RULE

Output ONLY:

- the English chapter heading;
- the complete reader-facing English translation.

Do not output:
- Chinese source;
- translation analysis;
- reasoning;
- explanations;
- glossary commentary;
- QA commentary.
""".strip()


# ============================================================
# SETUP
# ============================================================

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key:
    raise RuntimeError("DEEPSEEK_API_KEY not found in .env")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)


# ============================================================
# LOAD CHAPTER DATA
# ============================================================

with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)

chapter_map = {
    item["chapter"]: item
    for item in chapters
}


# ============================================================
# HELPERS
# ============================================================

def calculate_cost(usage, pricing_info):
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0

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

    total_cost, cache_hits, cache_misses = calculate_cost_from_token_counts(
        prompt_tokens,
        completion_tokens,
        cache_hits,
        cache_misses,
        pricing_info,
    )
    input_cost = (
        cache_misses / 1_000_000
        * pricing_info["cache_miss_rate_per_million"]
    )
    cache_cost = (
        cache_hits / 1_000_000
        * pricing_info["cache_hit_rate_per_million"]
    )
    output_cost = (
        completion_tokens / 1_000_000
        * pricing_info["output_rate_per_million"]
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "input_cost_usd": input_cost,
        "cache_cost_usd": cache_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
        **pricing_info,
    }


def translate_chapter(chapter_number):
    if chapter_number not in chapter_map:
        raise RuntimeError(
            f"Chapter {chapter_number} not found."
        )

    chapter = chapter_map[chapter_number]
    chinese_text = chapter["text"]

    user_prompt = f"""
Translate the following complete Chinese chapter.

This is Chapter {chapter_number}.

CHINESE SOURCE:

{chinese_text}
""".strip()

    print()
    print("=" * 70)
    print(f"STARTING CHAPTER {chapter_number}")
    print("=" * 70)

    start = time.perf_counter()

    request_pricing = pricing_snapshot(datetime.now(timezone.utc))
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
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
    usage = response.usage
    cost = calculate_cost(usage, request_pricing)

    output_file = (
        OUTPUT_DIR
        / f"chapter_{chapter_number:04d}_nonthinking.txt"
    )

    output_file.write_text(
        translation,
        encoding="utf-8",
    )

    result = {
        "chapter": chapter_number,
        "model": MODEL,
        "thinking": False,
        "source_characters": len(chinese_text),
        "source_paragraph_count": chapter["paragraph_count"],
        "elapsed_seconds": elapsed,
        "finish_reason": choice.finish_reason,
        "translation_characters": len(translation),
        "translation_words": len(translation.split()),
        **cost,
        "output_file": str(output_file),
    }

    print(f"Finished Chapter {chapter_number}")
    print(f"Time: {elapsed:.2f} sec")
    print(f"Finish reason: {choice.finish_reason}")
    print(f"Input tokens: {cost['prompt_tokens']:,}")
    print(f"Output tokens: {cost['completion_tokens']:,}")
    print(f"Cache hit tokens: {cost['cache_hit_tokens']:,}")
    print(f"Cache miss tokens: {cost['cache_miss_tokens']:,}")
    print(f"Translation words: {len(translation.split()):,}")
    print(f"Estimated cost: ${cost['total_cost_usd']:.6f}")
    print(f"Pricing period: {cost['pricing_period'].upper().replace('_', '-')}")
    print(f"Saved to: {output_file}")

    if not translation.strip():
        print("WARNING: Translation is empty.")

    if choice.finish_reason != "stop":
        print(
            "WARNING: Generation did not finish normally. "
            "Inspect this chapter before using it."
        )

    return result


# ============================================================
# RUN BENCHMARK
# ============================================================

results = []

for chapter_number in CHAPTER_NUMBERS:
    results.append(
        translate_chapter(chapter_number)
    )


# ============================================================
# SUMMARY
# ============================================================

results_file = OUTPUT_DIR / "chapters_0139_0273_results.json"

results_file.write_text(
    json.dumps(
        results,
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

total_cost = sum(
    item["total_cost_usd"]
    for item in results
)

total_time = sum(
    item["elapsed_seconds"]
    for item in results
)

total_input = sum(
    item["prompt_tokens"]
    for item in results
)

total_output = sum(
    item["completion_tokens"]
    for item in results
)

print()
print()
print("=" * 70)
print("BENCHMARK COMPLETE")
print("=" * 70)

for item in results:
    print()
    print(f"Chapter {item['chapter']}")
    print(f"Time: {item['elapsed_seconds']:.2f} sec")
    print(f"Input: {item['prompt_tokens']:,}")
    print(f"Output: {item['completion_tokens']:,}")
    print(f"Words: {item['translation_words']:,}")
    print(f"Cost: ${item['total_cost_usd']:.6f}")

print()
print("TOTAL")
print(f"Time: {total_time:.2f} sec")
print(f"Input tokens: {total_input:,}")
print(f"Output tokens: {total_output:,}")
print(f"Cost: ${total_cost:.6f}")
print(f"Results saved to: {results_file}")
print("=" * 70)
