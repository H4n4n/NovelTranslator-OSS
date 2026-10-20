import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


CHAPTER_NUMBER = 1
MODEL = "deepseek-v4-pro"

PRICE_CACHE_MISS_INPUT = 0.435
PRICE_CACHE_HIT_INPUT = 0.003625
PRICE_OUTPUT = 0.87

CHAPTERS_FILE = Path("data/chapters.json")
OUTPUT_DIR = Path("output/benchmark")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM_PROMPT = """
You are translating a Chinese webnovel into polished literary English.

The original Chinese is the sole authoritative source.

Translate the complete chapter directly from Chinese into natural,
reader-facing English.

Do not summarize, abridge, omit, or invent information.

Preserve characterization, relationships, hierarchy, emotional tone,
historical context, worldbuilding, humor, and authorial intent.

Resolve omitted Chinese subjects and pronouns from context.

TERMINOLOGY

Preserve the Chinese conceptual character of terminology.

Prefer accurate semantic English translations over unnecessary
Western substitutions or unnecessary pinyin.

For example, where contextually appropriate:

百户 → Hundred-Household Commander

rather than Centurion.

碧云宗 → Azure Cloud Sect

rather than Biyun Sect.

Personal names normally remain transliterated.

DAMAGED SOURCE

The source contains corrupted glyphs replacing some Chinese characters.

Use surrounding Chinese, narrative context, established terminology,
and historical context to reconstruct intended meaning when confidence
is high.

If a passage cannot be resolved reliably, use:

[TRANSLATION REVIEW REQUIRED]

rather than inventing information.

OUTPUT

Output ONLY the English chapter heading and complete reader-facing
English translation.

Do not output reasoning, analysis, Chinese source, or commentary.
""".strip()


load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key:
    raise RuntimeError("DEEPSEEK_API_KEY not found.")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)


with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)

chapter = next(
    ch for ch in chapters
    if ch["chapter"] == CHAPTER_NUMBER
)

chinese_text = chapter["text"]


messages = [
    {
        "role": "system",
        "content": SYSTEM_PROMPT,
    },
    {
        "role": "user",
        "content": (
            f"Translate Chapter {CHAPTER_NUMBER}.\n\n"
            f"CHINESE SOURCE:\n\n{chinese_text}"
        ),
    },
]


print("=" * 70)
print("V4 PRO — THINKING HIGH")
print("=" * 70)

start = time.perf_counter()

response = client.chat.completions.create(
    model=MODEL,
    messages=messages,
    max_tokens=30000,
    reasoning_effort="high",
    extra_body={
        "thinking": {
            "type": "enabled"
        }
    },
)

elapsed = time.perf_counter() - start

choice = response.choices[0]
message = choice.message
usage = response.usage

translation = message.content or ""
reasoning = getattr(message, "reasoning_content", None) or ""

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


reasoning_tokens = 0

details = getattr(
    usage,
    "completion_tokens_details",
    None,
)

if details is not None:
    reasoning_tokens = getattr(
        details,
        "reasoning_tokens",
        0,
    ) or 0


input_cost = (
    cache_misses / 1_000_000
    * PRICE_CACHE_MISS_INPUT
)

cache_cost = (
    cache_hits / 1_000_000
    * PRICE_CACHE_HIT_INPUT
)

output_cost = (
    completion_tokens / 1_000_000
    * PRICE_OUTPUT
)

total_cost = input_cost + cache_cost + output_cost


translation_file = (
    OUTPUT_DIR
    / "chapter_0001_thinking_high_30k.txt"
)

translation_file.write_text(
    translation,
    encoding="utf-8",
)


results = {
    "chapter": CHAPTER_NUMBER,
    "model": MODEL,
    "thinking": "high",
    "max_tokens": 30000,
    "elapsed_seconds": elapsed,
    "finish_reason": choice.finish_reason,
    "prompt_tokens": prompt_tokens,
    "completion_tokens": completion_tokens,
    "reasoning_tokens": reasoning_tokens,
    "reasoning_characters": len(reasoning),
    "translation_characters": len(translation),
    "translation_words": len(translation.split()),
    "cache_hit_tokens": cache_hits,
    "cache_miss_tokens": cache_misses,
    "estimated_cost_usd": total_cost,
}

results_file = (
    OUTPUT_DIR
    / "chapter_0001_thinking_high_30k_results.json"
)

results_file.write_text(
    json.dumps(
        results,
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)


print()
print(f"Time: {elapsed:.2f} sec")
print(f"Finish reason: {choice.finish_reason}")
print(f"Input tokens: {prompt_tokens:,}")
print(f"Total generated tokens: {completion_tokens:,}")
print(f"Reasoning tokens: {reasoning_tokens:,}")
print(f"Final translation words: {len(translation.split()):,}")
print(f"Final translation characters: {len(translation):,}")
print(f"Estimated cost: ${total_cost:.6f}")

print()
print(f"Translation saved to: {translation_file}")
print(f"Results saved to: {results_file}")

if not translation.strip():
    print()
    print("WARNING: FINAL TRANSLATION IS EMPTY.")
    print(
        "The model likely exhausted the generation budget "
        "during reasoning."
    )