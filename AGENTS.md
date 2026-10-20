# AGENTS.md

## Purpose

This repository contains a long-running multi-novel CJK-to-English webnovel
translation and publication pipeline.

The primary goals are:

- preserve translation quality and readability;
- preserve terminology consistency across the entire novel;
- avoid wasting paid API calls;
- resume safely after interruption;
- keep glossary behavior stable;
- preserve previously committed English chapters unless a narrowly-scoped, logged retroactive terminology correction is explicitly justified.

Read this file before modifying the project.

## Architecture Invariant

PROFILES DEFINE TRANSLATION POLICY. NOVELS OWN MUTABLE STATE. THE ENGINE IS SHARED.

Profile loading must never cause glossary, progress, chapter, pending, audit,
backup, log, EPUB, cost, credential, continuity, or issue state to leak between
novels. Profiles are reusable immutable policy; each novel owns its mutable
production state independently.

---

## Non-Negotiable Safety Rules

1. NEVER retranslate a chapter if a valid paid pending translation already exists in `<project>/output/pending/`. If a next-chapter final file also exists, preserve both; only the reviewed partial-commit reconciliation path may finish the proven commit tail when final/pending/QA and associated-state evidence match exactly.
2. NEVER delete or overwrite `<project>/data/progress.json`, `data/glossary.json`, `data/continuity.json`, or `data/issues.json` without first understanding the current state.
3. NEVER reset translation progress unless explicitly instructed by the user.
4. NEVER silently discard completed chapter files in `output/chapters/`.
5. NEVER perform broad find-and-replace across committed novel prose without source-anchored safeguards and backups.
6. NEVER convert the workflow to parallel workers unless the user explicitly asks. Current production is intentionally single-worker and sequential for glossary consistency.
7. NEVER change translation terminology philosophy without explicit instruction.
8. NEVER remove the pending-translation recovery mechanism.
9. NEVER remove API failure handling, JSON repair handling, off-peak scheduling, manual pause support, glossary auditing, or checkpoint backup behavior unless explicitly instructed. For paid production requests, retry policy must remain compatible with durable request journaling: ambiguous remote outcomes must fail closed rather than being silently reissued.
10. After modifying Python code, always run a syntax check:
   `python -m py_compile <scriptname.py>`
11. Prefer the smallest safe code change that solves the issue.
12. Before major refactors, create or recommend a Git commit / backup point.

---

## Translation Philosophy

Novel-specific source language and translation policy are selected through
reusable profiles. Current projects include Chinese and Korean sources targeting
natural, readable English.

For Chinese historical and cultivation projects, the user prefers terminology
to remain conceptually Chinese rather than heavily Westernized.

Examples:

- Prefer `Hundred-Household Commander` over `Centurion`.
- Prefer semantic English names such as `Azure Cloud Sect` over pinyin-only forms such as `Biyun Sect`, unless pinyin is genuinely preferable for a proper noun.
- Proper personal names normally use standard romanization/pinyin.
- Historical Chinese institutions should not be casually replaced with approximate Western institutions.

Formal named martial arts techniques, combat moves, formations, secret arts, cultivation arts, and named skills use the user's preferred capitalization style:

- `Black Tiger Seizes The Heart`
- capitalize the initial letter of EVERY word in the formal English technique name.

Do not use conventional title-case exceptions such as lowercase `the` for formal technique names.

---

## Glossary Rules

Glossary file:
`data/glossary.json`

Important glossary statuses:

- `locked`: authoritative; do not change silently.
- `provisional`: current preferred rendering; may be reviewed later.
- `contextual`: multiple approved English renderings depending on context.

Useful glossary categories include:

- character
- alias
- form of address
- historical person
- historical office
- military rank
- military unit
- state
- organization
- faction
- sect
- location
- technique
- artifact
- specialized recurring term

Do not bloat the glossary with ordinary vocabulary, generic objects, generic tactical phrases, promotional language, or one-off descriptions.

The automatic reviewer may:

- accept
- correct
- reject
- contextualize
- merge aliases/variants

Periodic glossary audits run automatically at configured intervals.

Locked terms must not be overridden by later automated decisions.

---

## Retroactive Corrections

Retroactive edits are allowed only for safe, narrow terminology corrections.

The current design requires:

1. exact old-English to new-English replacement;
2. the original Chinese chapter must contain the exact Chinese glossary term associated with the correction;
3. only committed chapters are eligible;
4. chapter backups must be created before modification;
5. every change must be logged under `output/retroactive_fixes/`.

Do NOT retroactively rewrite contextual terms through blind replacement.

Do NOT treat glossary rejection as proof that old prose should be changed.

---

## Recovery / Transactional Behavior

A translated chapter is first saved to:

`<project>/output/pending/NNNN.md`

with metadata in:

`<project>/output/pending/NNNN.json`

Only after all required state/glossary/QA steps succeed is the chapter committed to:

`output/chapters/NNNN.md`

Then `progress.json` is advanced.

If a later stage fails after translation succeeds, the paid translation must remain in `<project>/output/pending/` and be reused on the next run.

This is a core requirement.

---

## API / Scheduling

DeepSeek V4 Pro is the production default through the OpenAI-compatible client.
DeepSeek V4 Flash remains supported as an explicit opt-in/reference model;
existing paid pending translations remain bound to their recorded generating
model.

The unattended production script supports:

- automatic off-peak-only mode;
- a manual pause file: `pause.flag`;
- local waiting with zero API calls while paused;
- retries for temporary API/network failures.

Peak/off-peak pricing logic must be preserved.

DeepSeek V4 Pro peak windows currently configured in the project, Monday
through Friday only:

- 01:00-04:00 UTC
- 06:00-10:00 UTC

All other hours are off-peak. Saturday and Sunday are entirely off-peak.

The user wants the schedule restriction to be toggleable.

---

## Cost Tracking

The project now distinguishes:

- legacy cumulative cost estimates from older script versions;
- rate-correct cost tracking for new calls under dynamic peak/off-peak pricing.

Do not rewrite historical cost totals as if exact reconstruction were possible unless underlying per-request data supports it.

Token counts returned by the API are authoritative for local accounting.

DeepSeek's own billing dashboard remains authoritative for actual billed dollars.

---

## Source Corruption

The EPUB source contains substantial encoding corruption, often Chinese characters replaced by Hangul-like glyphs.

The translation model is expected to infer intended Chinese from context when confidence is high.

The state extractor must NOT enumerate hundreds of repeated corrupted glyphs.

Routine source corruption is counted locally by QA.

Only materially unresolved corruption should appear in model-reported issues.

Malformed state JSON should first attempt local recovery where safe, then one compact API repair if needed.

---

## EPUB Creation

Checkpoint EPUB creation is local-only and must make zero translation API calls.

Script:
`build_checkpoint_epub.py`

It should:

- use committed chapter files only;
- ignore `<project>/output/pending/`;
- build the continuous available range from Chapter 1 by default;
- never translate or reconstruct missing prose.

---

## Current Workflow Preference

Current production mode is:

- one worker;
- sequential chapters;
- automatic glossary review;
- automatic periodic glossary audit;
- automatic checkpoint backups;
- automatic retroactive safe terminology corrections;
- automatic off-peak scheduling when enabled;
- manual pause support;
- unattended continuation until the configured final chapter.

Preserve this architecture unless the user asks to change it.

---

## Before Changing Anything

When asked to modify the translator:

1. Read `AGENTS.md`.
2. Read `README.md`.
3. Read `README.md`.
4. Inspect the current script actually present in the repository.
5. Inspect `<project>/data/progress.json`.
6. Check `<project>/output/pending/`.
7. Understand the current checkpoint before editing.
8. Make the smallest safe change.
9. Run syntax validation.
10. Explain exactly what changed and what command the user should run next.

---
