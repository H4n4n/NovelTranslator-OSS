import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import (
    OpenAI,
    APIError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)


# ============================================================
# CONFIGURATION
# ============================================================

MODEL = "deepseek-v4-pro"

# For the next controlled test only.
START_CHAPTER = 11
END_CHAPTER = 20

MAX_API_RETRIES = 3

RETRY_DELAYS = [
    5,
    15,
    30,
]

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
PENDING_DIR = Path("output/pending")
QA_DIR = Path("output/qa")


OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

PENDING_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

QA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# API INITIALIZATION
# ============================================================

load_dotenv()

api_key = os.getenv(
    "DEEPSEEK_API_KEY"
)

if not api_key:
    raise RuntimeError(
        "DEEPSEEK_API_KEY was not found in .env"
    )


client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def save_json(path, data):
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(path)


# ============================================================
# LOAD PROJECT DATA
# ============================================================

with CHAPTERS_FILE.open(
    "r",
    encoding="utf-8",
) as f:

    chapters = json.load(f)


chapter_map = {
    item["chapter"]: item
    for item in chapters
}


glossary = load_json(
    GLOSSARY_FILE
)

progress = load_json(
    PROGRESS_FILE
)

continuity = load_json(
    CONTINUITY_FILE
)

issues_data = load_json(
    ISSUES_FILE
)


system_prompt = (
    TRANSLATION_PROMPT_FILE
    .read_text(
        encoding="utf-8"
    )
)


# ============================================================
# REGEX
# ============================================================

CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]"
)

HANGUL_RE = re.compile(
    r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"
)


# ============================================================
# API RETRY WRAPPER
# ============================================================

def call_api_with_retries(**kwargs):
    last_error = None

    for attempt in range(
        1,
        MAX_API_RETRIES + 1,
    ):

        try:
            return (
                client
                .chat
                .completions
                .create(
                    **kwargs
                )
            )

        except (
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
        ) as exc:

            last_error = exc

            print()
            print(
                f"Temporary API error "
                f"(attempt "
                f"{attempt}/"
                f"{MAX_API_RETRIES})"
            )

            print(exc)

            if (
                attempt
                < MAX_API_RETRIES
            ):

                delay = (
                    RETRY_DELAYS[
                        attempt - 1
                    ]
                )

                print(
                    f"Waiting "
                    f"{delay} seconds "
                    f"before retrying..."
                )

                time.sleep(
                    delay
                )

        except APIError:
            # Billing errors, invalid requests, and other
            # non-transient API failures should stop cleanly
            # instead of being retried repeatedly.
            raise

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "API call failed without a captured exception."
    )


# ============================================================
# TOKEN USAGE / COST
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

    if (
        cache_hits == 0
        and cache_misses == 0
    ):
        cache_misses = (
            prompt_tokens
        )

    cost = (
        (
            cache_misses
            / 1_000_000
            * PRICE_CACHE_MISS_INPUT
        )
        +
        (
            cache_hits
            / 1_000_000
            * PRICE_CACHE_HIT_INPUT
        )
        +
        (
            completion_tokens
            / 1_000_000
            * PRICE_OUTPUT
        )
    )

    return {
        "prompt_tokens":
            prompt_tokens,

        "completion_tokens":
            completion_tokens,

        "cache_hit_tokens":
            cache_hits,

        "cache_miss_tokens":
            cache_misses,

        "cost_usd":
            cost,
    }


# ============================================================
# GLOSSARY RETRIEVAL
# ============================================================

def relevant_glossary(
    chinese_text,
):
    relevant = []

    for (
        chinese_term,
        entry,
    ) in glossary[
        "entries"
    ].items():

        if (
            chinese_term
            not in chinese_text
        ):
            continue

        item = {
            "chinese":
                chinese_term,

            "english":
                entry.get(
                    "english",
                    "",
                ),

            "category":
                entry.get(
                    "category",
                    "",
                ),

            "status":
                entry.get(
                    "status",
                    "provisional",
                ),

            "notes":
                entry.get(
                    "notes",
                    "",
                ),
        }

        if (
            entry.get(
                "status"
            )
            == "contextual"
        ):
            item[
                "approved_renderings"
            ] = entry.get(
                "approved_renderings",
                [],
            )

        relevant.append(
            item
        )

    return relevant


def glossary_to_text(
    entries,
):
    if not entries:
        return (
            "No established glossary "
            "entries apply."
        )

    lines = []

    for entry in entries:

        status = entry.get(
            "status",
            "provisional",
        )

        chinese = (
            entry["chinese"]
        )

        if (
            status
            == "contextual"
        ):

            approved = entry.get(
                "approved_renderings",
                [],
            )

            lines.append(
                chinese
            )

            lines.append(
                "  Status: CONTEXTUAL"
            )

            if approved:
                lines.append(
                    "  Approved renderings: "
                    + "; ".join(
                        approved
                    )
                )

            if entry.get(
                "notes"
            ):
                lines.append(
                    "  Notes: "
                    + entry["notes"]
                )

        else:

            lines.append(
                f"{chinese} → "
                f"{entry['english']}"
            )

            lines.append(
                f"  Status: "
                f"{status.upper()}"
            )

            if entry.get(
                "category"
            ):
                lines.append(
                    "  Category: "
                    + entry[
                        "category"
                    ]
                )

            if entry.get(
                "notes"
            ):
                lines.append(
                    "  Notes: "
                    + entry[
                        "notes"
                    ]
                )

    return "\n".join(
        lines
    )


# ============================================================
# TRANSLATE CHAPTER
# ============================================================

def translate_chapter(
    chapter,
):
    chapter_number = (
        chapter["chapter"]
    )

    chinese_text = (
        chapter["text"]
    )

    relevant = (
        relevant_glossary(
            chinese_text
        )
    )

    glossary_text = (
        glossary_to_text(
            relevant
        )
    )

    continuity_text = (
        continuity
        .get(
            "summary",
            "",
        )
        .strip()
    )

    if not continuity_text:
        continuity_text = (
            "No previous continuity "
            "summary is available."
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
    print(
        f"TRANSLATING CHAPTER "
        f"{chapter_number}"
    )
    print("=" * 70)

    start = (
        time.perf_counter()
    )

    response = (
        call_api_with_retries(
            model=MODEL,

            messages=[
                {
                    "role":
                        "system",

                    "content":
                        system_prompt,
                },
                {
                    "role":
                        "user",

                    "content":
                        user_prompt,
                },
            ],

            max_tokens=12000,

            extra_body={
                "thinking": {
                    "type":
                        "disabled"
                }
            },
        )
    )

    elapsed = (
        time.perf_counter()
        - start
    )

    choice = (
        response.choices[0]
    )

    translation = (
        choice
        .message
        .content
        or ""
    )

    stats = (
        usage_and_cost(
            response.usage
        )
    )

    print(
        f"Translation finished "
        f"in {elapsed:.2f} sec"
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
# STATE EXTRACTION PROMPT
# ============================================================

STATE_PROMPT = """
You maintain terminology and immediate continuity for a Chinese
webnovel translation.

Analyze the supplied Chinese chapter and its final English translation.

Return ONLY valid JSON.

Use this exact structure:

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

NEW TERM RULES

Add a glossary entry ONLY if it is genuinely useful for consistency
across future chapters.

Good glossary candidates include:

- character names
- aliases
- stable forms of address
- historical people
- historical offices
- military ranks
- military units
- dynasties/states
- organizations
- factions
- sects
- important locations
- recurring techniques
- recurring artifacts
- recurring specialized terminology
- recurring culturally specific concepts

DO NOT add:

- ordinary nouns
- ordinary verbs
- ordinary adjectives
- one-time descriptive phrases
- generic occupations
- generic expressions
- jokes that do not require future consistency
- website terminology
- author promotional language
- voting/bookmark/ranking terminology
- ordinary modern slang unless it becomes a recurring story-specific concept

Examples of terms that usually SHOULD NOT become glossary entries:

紅票
红票
負距離
负距离
磚家
砖家
手藝人
手艺人

unless later context establishes them as genuinely recurring
story-specific terminology.

Do NOT add a Chinese term already present in the supplied existing glossary.

For new terms:

status must be:

"provisional"

Do NOT mark newly discovered terms "locked".

The English rendering must match the final translation.

CONTINUITY SUMMARY

Maximum approximately 250 words.

Include only information useful for interpreting the NEXT chapter:

- active characters
- current location
- immediate relationships
- immediate events
- unresolved immediate facts
- identities/references likely to matter next

Do not summarize the entire novel.

ISSUES

Report only genuine:

- damaged Chinese that could not be resolved confidently
- ambiguous terminology
- possible conflicts with established glossary entries
- occurrences of [TRANSLATION REVIEW REQUIRED]

If there are none:

"issues": []

Return JSON only.
""".strip()


# ============================================================
# STATE EXTRACTION
# ============================================================

def extract_state(
    chapter_number,
    chinese_text,
    translation,
):

    existing_terms = list(
        glossary[
            "entries"
        ].keys()
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

    response = (
        call_api_with_retries(
            model=MODEL,

            messages=[
                {
                    "role":
                        "system",

                    "content":
                        STATE_PROMPT,
                },
                {
                    "role":
                        "user",

                    "content":
                        user_prompt,
                },
            ],

            max_tokens=3000,

            extra_body={
                "thinking": {
                    "type":
                        "disabled"
                }
            },
        )
    )

    content = (
        response
        .choices[0]
        .message
        .content
        or ""
    )

    content = (
        content.strip()
    )

    if content.startswith(
        "```json"
    ):
        content = (
            content[7:]
        )

    elif content.startswith(
        "```"
    ):
        content = (
            content[3:]
        )

    if content.endswith(
        "```"
    ):
        content = (
            content[:-3]
        )

    content = (
        content.strip()
    )

    try:
        state = json.loads(
            content
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"State JSON parsing "
            f"failed for Chapter "
            f"{chapter_number}:\n\n"
            f"{content}"
        ) from exc

    stats = (
        usage_and_cost(
            response.usage
        )
    )

    return (
        state,
        stats,
    )


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
            "Unexpected finish reason: "
            + str(
                finish_reason
            )
        )

    if not translation.strip():
        found.append(
            "Translation is empty"
        )

    if len(
        translation
    ) < 1000:
        found.append(
            "Translation suspiciously short"
        )

    remaining_chinese = len(
        CJK_RE.findall(
            translation
        )
    )

    if (
        remaining_chinese
        > 10
    ):
        found.append(
            "Remaining Chinese "
            f"characters: "
            f"{remaining_chinese}"
        )

    if (
        "[TRANSLATION REVIEW REQUIRED]"
        in translation
    ):
        found.append(
            "Translation contains "
            "review marker"
        )

    source_hangul = len(
        HANGUL_RE.findall(
            chinese_text
        )
    )

    return {
        "chapter":
            chapter_number,

        "source_hangul_characters":
            source_hangul,

        "remaining_chinese_characters":
            remaining_chinese,

        "issues":
            found,
    }


# ============================================================
# GLOSSARY UPDATE
# ============================================================

LOW_VALUE_TERMS = {
    "紅票",
    "红票",
    "負距離",
    "负距离",
    "磚家",
    "砖家",
    "手藝人",
    "手艺人",
}


def apply_new_terms(
    chapter_number,
    new_terms,
):
    added = []

    for proposal in new_terms:

        chinese = (
            proposal.get(
                "chinese",
                "",
            )
            .strip()
        )

        english = (
            proposal.get(
                "english",
                "",
            )
            .strip()
        )

        category = (
            proposal.get(
                "category",
                "",
            )
            .strip()
        )

        if (
            not chinese
            or not english
        ):
            continue

        if (
            chinese
            in glossary[
                "entries"
            ]
        ):
            continue

        if (
            chinese
            in LOW_VALUE_TERMS
        ):
            continue

        low_value_categories = {
            "ordinary vocabulary",
            "generic vocabulary",
            "promotional term",
            "website term",
            "promotional language",
            "ordinary noun",
            "ordinary verb",
            "ordinary adjective",
        }

        if (
            category.lower()
            in low_value_categories
        ):
            continue

        glossary[
            "entries"
        ][chinese] = {
            "english":
                english,

            "category":
                category,

            "status":
                "provisional",

            "first_chapter":
                chapter_number,

            "notes":
                proposal.get(
                    "notes",
                    "",
                ),
        }

        added.append(
            f"{chinese} → "
            f"{english}"
        )

    return added


# ============================================================
# ERROR DESCRIPTION
# ============================================================

def describe_api_error(
    exc,
):
    text = str(
        exc
    ).lower()

    if (
        "balance" in text
        or "credit" in text
        or "billing" in text
        or "insufficient" in text
    ):
        return (
            "The API may have run out of balance/credit. "
            "Add credit and run the script again."
        )

    return (
        "Check the API error above. "
        "After resolving it, run the script again."
    )


# ============================================================
# PROCESS ONE CHAPTER
# ============================================================

def process_chapter(
    chapter_number,
):
    chapter = (
        chapter_map[
            chapter_number
        ]
    )

    chinese_text = (
        chapter["text"]
    )

    final_file = (
        OUTPUT_DIR
        / f"{chapter_number:04d}.md"
    )

    pending_file = (
        PENDING_DIR
        / f"{chapter_number:04d}.md"
    )

    pending_meta_file = (
        PENDING_DIR
        / f"{chapter_number:04d}.json"
    )


    # ========================================================
    # ALREADY COMMITTED
    # ========================================================

    if (
        chapter_number
        in progress[
            "completed"
        ]
    ):

        print(
            f"Chapter "
            f"{chapter_number} "
            f"is already fully "
            f"committed. Skipping."
        )

        return True


    # ========================================================
    # RECOVER PREVIOUS PAID TRANSLATION
    # ========================================================

    if (
        pending_file.exists()
    ):

        print()
        print(
            f"Recovered pending "
            f"translation for "
            f"Chapter "
            f"{chapter_number}."
        )

        print(
            "The translation will "
            "NOT be generated again."
        )

        translation = (
            pending_file
            .read_text(
                encoding="utf-8"
            )
        )

        if (
            pending_meta_file
            .exists()
        ):

            pending_meta = (
                load_json(
                    pending_meta_file
                )
            )

            translation_stats = (
                pending_meta.get(
                    "translation_stats",
                    {
                        "prompt_tokens":
                            0,

                        "completion_tokens":
                            0,

                        "cost_usd":
                            0.0,
                    },
                )
            )

            elapsed = (
                pending_meta.get(
                    "elapsed_seconds",
                    0.0,
                )
            )

        else:

            translation_stats = {
                "prompt_tokens":
                    0,

                "completion_tokens":
                    0,

                "cost_usd":
                    0.0,
            }

            elapsed = 0.0

        finish_reason = (
            "stop"
        )


    else:

        # ====================================================
        # GENERATE TRANSLATION
        # ====================================================

        try:

            (
                translation,
                translation_stats,
                elapsed,
                finish_reason,
            ) = translate_chapter(
                chapter
            )

        except (
            APIError,
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
        ) as exc:

            print()
            print("=" * 70)
            print(
                "API ERROR DURING "
                "TRANSLATION"
            )
            print("=" * 70)

            print(
                f"Chapter: "
                f"{chapter_number}"
            )

            print(
                f"Error: {exc}"
            )

            print()
            print(
                describe_api_error(
                    exc
                )
            )

            print()
            print(
                "Previously completed "
                "chapters remain saved."
            )

            return False


        if not translation.strip():

            print()
            print(
                f"Chapter "
                f"{chapter_number} "
                f"returned an empty "
                f"translation."
            )

            print(
                "Stopping safely."
            )

            return False


        if (
            finish_reason
            != "stop"
        ):

            print()
            print(
                f"Chapter "
                f"{chapter_number} "
                f"ended with "
                f"finish_reason="
                f"{finish_reason}"
            )

            print(
                "Stopping safely."
            )

            return False


        # ====================================================
        # SAVE PAID TRANSLATION IMMEDIATELY
        # ====================================================

        pending_file.write_text(
            translation,
            encoding="utf-8",
        )

        pending_meta = {
            "chapter":
                chapter_number,

            "translation_stats":
                translation_stats,

            "elapsed_seconds":
                elapsed,

            "finish_reason":
                finish_reason,
        }

        save_json(
            pending_meta_file,
            pending_meta,
        )

        print(
            f"Pending translation "
            f"saved to: "
            f"{pending_file}"
        )


    # ========================================================
    # LOCAL QA
    # ========================================================

    qa = local_qa(
        chapter_number,
        chinese_text,
        translation,
        finish_reason,
    )


    # ========================================================
    # STATE / GLOSSARY EXTRACTION
    # ========================================================

    try:

        (
            state,
            state_stats,
        ) = extract_state(
            chapter_number,
            chinese_text,
            translation,
        )

    except (
        APIError,
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
    ) as exc:

        print()
        print("=" * 70)
        print(
            "API ERROR DURING "
            "STATE EXTRACTION"
        )
        print("=" * 70)

        print(
            f"Chapter: "
            f"{chapter_number}"
        )

        print(
            f"Error: {exc}"
        )

        print()
        print(
            describe_api_error(
                exc
            )
        )

        print()
        print(
            "The English translation "
            "has already been saved "
            "safely in:"
        )

        print(
            pending_file
        )

        print()
        print(
            "When you run the "
            "program again, that "
            "translation will be reused "
            "instead of generated again."
        )

        return False


    # ========================================================
    # APPLY NEW TERMS
    # ========================================================

    new_terms = (
        state.get(
            "new_terms",
            [],
        )
    )

    added_terms = (
        apply_new_terms(
            chapter_number,
            new_terms,
        )
    )


    # ========================================================
    # UPDATE CONTINUITY
    # ========================================================

    continuity[
        "latest_chapter"
    ] = chapter_number

    continuity[
        "summary"
    ] = state.get(
        "continuity_summary",
        "",
    )


    # ========================================================
    # MODEL-REPORTED ISSUES
    # ========================================================

    for issue in state.get(
        "issues",
        [],
    ):

        issues_data[
            "issues"
        ].append(
            {
                "chapter":
                    chapter_number,

                "type":
                    "model_flag",

                "details":
                    issue,
            }
        )


    # ========================================================
    # LOCAL QA ISSUES
    # ========================================================

    if qa["issues"]:

        issues_data[
            "issues"
        ].append(
            {
                "chapter":
                    chapter_number,

                "type":
                    "local_qa",

                "details":
                    qa[
                        "issues"
                    ],
            }
        )


    # ========================================================
    # SAVE QA FILE
    # ========================================================

    qa_file = (
        QA_DIR
        / f"{chapter_number:04d}.json"
    )

    save_json(
        qa_file,
        qa,
    )


    # ========================================================
    # TOKEN / COST ACCOUNTING
    # ========================================================

    chapter_prompt_tokens = (
        translation_stats[
            "prompt_tokens"
        ]
        +
        state_stats[
            "prompt_tokens"
        ]
    )

    chapter_completion_tokens = (
        translation_stats[
            "completion_tokens"
        ]
        +
        state_stats[
            "completion_tokens"
        ]
    )

    chapter_cost = (
        translation_stats[
            "cost_usd"
        ]
        +
        state_stats[
            "cost_usd"
        ]
    )


    progress[
        "total_prompt_tokens"
    ] += (
        chapter_prompt_tokens
    )

    progress[
        "total_completion_tokens"
    ] += (
        chapter_completion_tokens
    )

    progress[
        "estimated_cost_usd"
    ] += (
        chapter_cost
    )


    # ========================================================
    # WRITE FINAL COMMITTED CHAPTER
    # ========================================================

    final_file.write_text(
        translation,
        encoding="utf-8",
    )


    # ========================================================
    # UPDATE PROGRESS
    # ========================================================

    if (
        chapter_number
        not in progress[
            "completed"
        ]
    ):

        progress[
            "completed"
        ].append(
            chapter_number
        )

    progress[
        "completed"
    ].sort()


    progress[
        "last_completed"
    ] = chapter_number

    progress[
        "next_chapter"
    ] = (
        chapter_number
        + 1
    )


    if (
        chapter_number
        in progress[
            "failed"
        ]
    ):

        progress[
            "failed"
        ].remove(
            chapter_number
        )


    # ========================================================
    # SAVE PERSISTENT STATE
    # ========================================================

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


    # ========================================================
    # DELETE PENDING ONLY AFTER FULL COMMIT
    # ========================================================

    if (
        pending_file.exists()
    ):
        pending_file.unlink()


    if (
        pending_meta_file.exists()
    ):
        pending_meta_file.unlink()


    # ========================================================
    # SUCCESS REPORT
    # ========================================================

    print()
    print(
        f"Chapter "
        f"{chapter_number} "
        f"fully committed."
    )

    print(
        f"New glossary entries: "
        f"{len(added_terms)}"
    )

    for term in added_terms:
        print(
            f"  + {term}"
        )

    print(
        f"Chapter total cost: "
        f"${chapter_cost:.6f}"
    )

    print(
        f"Project total cost: "
        f"${progress['estimated_cost_usd']:.6f}"
    )

    print(
        f"Next chapter: "
        f"{progress['next_chapter']}"
    )

    return True


# ============================================================
# MAIN
# ============================================================

print()
print("=" * 70)
print(
    "DEEPSEEK NOVEL TRANSLATION "
    "— PRODUCTION TEST"
)
print("=" * 70)

print(
    f"Requested range: "
    f"{START_CHAPTER}–"
    f"{END_CHAPTER}"
)

print(
    f"Current checkpoint: "
    f"completed through "
    f"{progress.get('last_completed', 0)}"
)

print(
    f"Checkpoint next chapter: "
    f"{progress.get('next_chapter', 1)}"
)


start_from = max(
    START_CHAPTER,
    progress.get(
        "next_chapter",
        START_CHAPTER,
    ),
)


if (
    start_from
    > END_CHAPTER
):

    print()
    print(
        "Nothing to process "
        "in this range."
    )

else:

    for chapter_number in range(
        start_from,
        END_CHAPTER + 1,
    ):

        success = (
            process_chapter(
                chapter_number
            )
        )

        if not success:

            print()
            print("=" * 70)

            print(
                f"STOPPED SAFELY "
                f"AT CHAPTER "
                f"{chapter_number}"
            )

            print(
                f"Last committed: "
                f"{progress.get('last_completed')}"
            )

            print(
                f"Next chapter: "
                f"{progress.get('next_chapter')}"
            )

            print("=" * 70)

            break

    else:

        print()
        print("=" * 70)
        print(
            "PRODUCTION TEST COMPLETE"
        )
        print("=" * 70)

        print(
            f"Completed through "
            f"Chapter "
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
            f"Recorded API cost: "
            f"${progress['estimated_cost_usd']:.6f}"
        )

        print(
            f"Glossary entries: "
            f"{len(glossary['entries'])}"
        )

        print("=" * 70)