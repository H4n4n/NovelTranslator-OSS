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
# CONFIGURATION
# ============================================================

CHAPTER_NUMBER = 1
MODEL = "deepseek-v4-pro"

CHAPTERS_FILE = Path("data/chapters.json")
OUTPUT_DIR = Path("output/benchmark")

# ============================================================
# TRANSLATION PROMPT
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
- Do not add explanations that are absent from the source.
- Resolve omitted Chinese subjects and pronouns from context.
- Preserve characterization, relationships, hierarchy, emotional tone,
  humor, implications, historical context, and authorial intent.
- Preserve all narrative and dialogue.
- Naturalize awkward Chinese syntax where appropriate, while preserving
  the actual meaning.

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

Do not blindly apply these examples to unrelated terms. Determine each
term from its actual Chinese meaning and narrative context.

DAMAGED SOURCE TEXT

The source contains encoding corruption in which some intended Chinese
characters appear as Hangul or other incorrect glyphs.

When encountering corrupted text:

1. Examine the surrounding Chinese.
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
# INITIALIZATION
# ============================================================

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key:
    raise RuntimeError(
        "DEEPSEEK_API_KEY was not found. Check your .env file."
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOAD CHAPTER
# ============================================================

with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)

chapter = next(
    (
        item
        for item in chapters
        if item["chapter"] == CHAPTER_NUMBER
    ),
    None,
)

if chapter is None:
    raise RuntimeError(
        f"Chapter {CHAPTER_NUMBER} was not found in {CHAPTERS_FILE}"
    )

chinese_text = chapter["text"]


USER_PROMPT = f"""
Translate the following complete Chinese chapter.

This is Chapter {CHAPTER_NUMBER}.

CHINESE SOURCE:

{chinese_text}
""".strip()


# ============================================================
# HELPERS
# ============================================================

def get_usage_value(usage, name, default=0):
    """
    Safely read usage attributes because API/SDK versions may expose
    slightly different fields.
    """
    value = getattr(usage, name, None)

    if value is None:
        return default

    return value


def calculate_cost(usage, pricing_info):
    """
    Estimate cost using DeepSeek V4 Pro's current token prices.
    """

    prompt_tokens = get_usage_value(
        usage,
        "prompt_tokens",
        0,
    )

    completion_tokens = get_usage_value(
        usage,
        "completion_tokens",
        0,
    )

    cache_hit_tokens = get_usage_value(
        usage,
        "prompt_cache_hit_tokens",
        0,
    )

    cache_miss_tokens = get_usage_value(
        usage,
        "prompt_cache_miss_tokens",
        0,
    )

    # Fallback in case the API does not expose hit/miss details.
    if cache_hit_tokens == 0 and cache_miss_tokens == 0:
        cache_miss_tokens = prompt_tokens

    total_cost, cache_hit_tokens, cache_miss_tokens = (
        calculate_cost_from_token_counts(
            prompt_tokens,
            completion_tokens,
            cache_hit_tokens,
            cache_miss_tokens,
            pricing_info,
        )
    )
    input_cost = (
        cache_miss_tokens / 1_000_000
        * pricing_info["cache_miss_rate_per_million"]
    )
    cache_cost = (
        cache_hit_tokens / 1_000_000
        * pricing_info["cache_hit_rate_per_million"]
    )
    output_cost = (
        completion_tokens / 1_000_000
        * pricing_info["output_rate_per_million"]
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "input_cost_usd": input_cost,
        "cache_cost_usd": cache_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
        **pricing_info,
    }


def run_translation(label, thinking_enabled):
    print()
    print("=" * 70)
    print(f"STARTING: {label}")
    print("=" * 70)
    print()

    start_time = time.perf_counter()

    kwargs = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": USER_PROMPT,
            },
        ],
        "max_tokens": 12000,
    }

    if thinking_enabled:
        kwargs["reasoning_effort"] = "high"
        kwargs["extra_body"] = {
            "thinking": {
                "type": "enabled"
            }
        }
    else:
        kwargs["extra_body"] = {
            "thinking": {
                "type": "disabled"
            }
        }

    request_pricing = pricing_snapshot(datetime.now(timezone.utc))
    response = client.chat.completions.create(**kwargs)

    elapsed_seconds = time.perf_counter() - start_time

    message = response.choices[0].message

    translation = message.content or ""

    # Thinking responses expose reasoning_content separately.
    reasoning_content = getattr(
        message,
        "reasoning_content",
        None,
    )

    usage = response.usage

    cost = calculate_cost(usage, request_pricing)

    result = {
        "label": label,
        "model": MODEL,
        "chapter": CHAPTER_NUMBER,
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": (
            "high"
            if thinking_enabled
            else None
        ),
        "elapsed_seconds": elapsed_seconds,
        "translation_characters": len(translation),
        "translation_words": len(translation.split()),
        "prompt_tokens": cost["prompt_tokens"],
        "completion_tokens": cost["completion_tokens"],
        "cache_hit_tokens": cost["cache_hit_tokens"],
        "cache_miss_tokens": cost["cache_miss_tokens"],
        "input_cost_usd": cost["input_cost_usd"],
        "cache_cost_usd": cost["cache_cost_usd"],
        "output_cost_usd": cost["output_cost_usd"],
        "total_cost_usd": cost["total_cost_usd"],
        "pricing_version": cost["pricing_version"],
        "pricing_period": cost["pricing_period"],
        "request_started_utc": cost["request_started_utc"],
        "reasoning_returned": bool(reasoning_content),
    }

    print(f"Finished: {label}")
    print(f"Time: {elapsed_seconds:.2f} seconds")
    print(f"Input tokens: {cost['prompt_tokens']:,}")
    print(f"Output tokens: {cost['completion_tokens']:,}")
    print(
        f"Cache miss tokens: "
        f"{cost['cache_miss_tokens']:,}"
    )
    print(
        f"Cache hit tokens: "
        f"{cost['cache_hit_tokens']:,}"
    )
    print(
        f"Estimated cost: "
        f"${cost['total_cost_usd']:.6f}"
    )
    print(f"Pricing period: {cost['pricing_period'].upper().replace('_', '-')}")
    print(
        f"Translation words: "
        f"{len(translation.split()):,}"
    )

    return result, translation


# ============================================================
# RUN NON-THINKING
# ============================================================

nonthinking_result, nonthinking_translation = run_translation(
    label="V4 Pro - Thinking OFF",
    thinking_enabled=False,
)

nonthinking_file = (
    OUTPUT_DIR
    / f"chapter_{CHAPTER_NUMBER:04d}_nonthinking.txt"
)

nonthinking_file.write_text(
    nonthinking_translation,
    encoding="utf-8",
)


# ============================================================
# RUN THINKING HIGH
# ============================================================

thinking_result, thinking_translation = run_translation(
    label="V4 Pro - Thinking HIGH",
    thinking_enabled=True,
)

thinking_file = (
    OUTPUT_DIR
    / f"chapter_{CHAPTER_NUMBER:04d}_thinking_high.txt"
)

thinking_file.write_text(
    thinking_translation,
    encoding="utf-8",
)


# ============================================================
# SAVE RESULTS
# ============================================================

results = {
    "chapter": CHAPTER_NUMBER,
    "source_characters": len(chinese_text),
    "source_paragraph_count": chapter["paragraph_count"],
    "model": MODEL,
    "nonthinking": nonthinking_result,
    "thinking_high": thinking_result,
}

results_file = (
    OUTPUT_DIR
    / f"chapter_{CHAPTER_NUMBER:04d}_results.json"
)

with results_file.open(
    "w",
    encoding="utf-8",
) as f:
    json.dump(
        results,
        f,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# COMPARISON
# ============================================================

cost_off = nonthinking_result["total_cost_usd"]
cost_high = thinking_result["total_cost_usd"]

time_off = nonthinking_result["elapsed_seconds"]
time_high = thinking_result["elapsed_seconds"]

if cost_off > 0:
    cost_multiplier = cost_high / cost_off
else:
    cost_multiplier = 0

if time_off > 0:
    time_multiplier = time_high / time_off
else:
    time_multiplier = 0


print()
print()
print("=" * 70)
print("BENCHMARK COMPLETE")
print("=" * 70)

print()
print("THINKING OFF")
print(
    f"Time: "
    f"{nonthinking_result['elapsed_seconds']:.2f} sec"
)
print(
    f"Input: "
    f"{nonthinking_result['prompt_tokens']:,}"
)
print(
    f"Output: "
    f"{nonthinking_result['completion_tokens']:,}"
)
print(
    f"Cost: "
    f"${cost_off:.6f}"
)

print()
print("THINKING HIGH")
print(
    f"Time: "
    f"{thinking_result['elapsed_seconds']:.2f} sec"
)
print(
    f"Input: "
    f"{thinking_result['prompt_tokens']:,}"
)
print(
    f"Output: "
    f"{thinking_result['completion_tokens']:,}"
)
print(
    f"Cost: "
    f"${cost_high:.6f}"
)

print()
print(
    f"Thinking cost multiplier: "
    f"{cost_multiplier:.2f}x"
)

print(
    f"Thinking time multiplier: "
    f"{time_multiplier:.2f}x"
)

print()
print("FILES CREATED:")
print(nonthinking_file)
print(thinking_file)
print(results_file)

print()
print(
    "Read BOTH translations manually before deciding "
    "which mode to use."
)
print("=" * 70)
