# NovelTranslator

A local multi-novel CJK-to-English webnovel translation and publication pipeline. DeepSeek V4 Pro is the production default; DeepSeek V4 Flash remains an explicit opt-in/reference model.

The project is designed for long unattended runs while preserving glossary consistency, paid translation recovery, automatic QA, and checkpoint EPUB creation.

The shared engine supports isolated projects under `novels/<novel_id>/`. The
original repository-level layout remains available as a compatibility baseline
until the project migration has been reviewed.

## Operator workflow

Use [docs/OPERATIONS.md](docs/OPERATIONS.md) for the complete verified command
sequence: preflight, translation, pause/resume, bounded paid-pending recovery,
controlled trials, output validation, and both EPUB workflows. Always select the
novel explicitly. Read-only preflight does not authorize paid execution.

```powershell
$Novel = 'example_project_b'  # Select the intended novel.
.\.venv\Scripts\python.exe operator_preflight.py --novel $Novel --model pro
```

Only direct DeepSeek `PRODUCTION_READY` routes to normal translation.
Initial GOAT rollout routes to `CONTROLLED_TRIAL_READY` when an explicit
credit allowance is supplied, or `BOUNDED_ALLOWANCE_REQUIRED` otherwise. `CLEANUP_READY` routes
to proof-complete local pending cleanup with zero provider calls.
`RECOVERY_READY` routes to a separate plan and explicit approval; `BLOCKED`
requires investigation.

State output classification and bounded recovery are documented in
[docs/STATE_EXTRACTION_RECOVERY.md].

The immutable paid-operation control catalog and translation-saturation design
are documented in [docs/PRODUCTION_OPERATION_POLICY.md](docs/PRODUCTION_OPERATION_POLICY.md).

AUT-1 recovery behavior, existing automatic routes, and remaining operator-controlled
boundaries are documented in [Autonomous recovery](docs/AUTONOMOUS_RECOVERY.md).

## Development Environment

The supported engineering baseline is Python 3.14.x. The current reproducible
baseline was validated on Python 3.14.4.

Create an isolated virtual environment and install the project from its
canonical dependency manifest:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Runtime dependencies and the development test dependency are declared in
`pyproject.toml`. Pytest configuration remains in `pytest.ini`.

Windows is the production and initial CI baseline. OpenCC is optional on
Windows because source-side Traditional-to-Simplified comparison has a native
Windows fallback when OpenCC is unavailable.

Tests and CI must run without `DEEPSEEK_API_KEY` and without a repository
`.env`. Real provider calls belong only in explicitly authorized production or
paid-test workflows.

---

## Engine, Profiles, and Novels

NovelTranslator separates three kinds of responsibility:

- the repository-level Python engine supplies shared workflow logic;
- `profiles/<profile_id>/` supplies reusable, immutable translation policy;
- `novels/<novel_id>/` owns all mutable state and reader-facing output.

Profiles define translation style, terminology policy, allowed and
high-priority glossary categories, capitalization rules, review priorities,
and audit priorities. Model-facing prose lives separately in each profile's
`translation_instructions.txt`, rather than being mixed into one large JSON
prompt. Profiles never own progress, glossary entries, continuity, issues,
pending or committed translations, costs, audits, backups, logs, EPUBs, or API
credentials.

Available profiles:

- `historical_military`: the compatibility policy for the current Ming novel;
- `xianxia_xuanhuan`: cultivation-fantasy policy with high-priority realms,
  stages, techniques, sects, artifacts, and related terminology;
- `infinite_flow`: cross-world survival and mission fiction with stable system,
  scenario, reward, currency, enhancement, bloodline, skill, equipment, rank,
  and difficulty terminology while preserving each embedded world's genre.
- `korean_fantasy`: Korean fantasy with stable royal, noble, military, knight,
  mana, magic, progression, artifact, species, and fantasy-name terminology.
- `korean_xianxia`: Korean cultivation/xianxia fantasy with stable realms,
  stages, sects, techniques, artifacts, formations, pills, Dao concepts, Laws,
  Intents, Domains, Tribulations, and calibrated Korean-name handling.

A novel selects its reusable profile in `novel.json`:

```json
{
  "id": "example_project_d",
  "title": "Ming Historical Novel",
  "source_language": "zh",
  "target_language": "en",
  "profile": "historical_military",
  "final_chapter": 1406,
  "chapter_numbering": "global",
  "source_type": "epub"
}
```

`source_type` may be `epub` or `txt`. It defaults to `epub`, so existing
projects require no migration and need not add the field.

An optional `translation_addendum.txt` in a novel directory can add policy
that applies only to that novel. The path is fixed by the engine; profiles
cannot redirect it or any mutable project path.

The complete addendum is used only for the main translation prompt. To share a
small terminology distinction with state extraction, glossary review, and
periodic audits, place concise list items in an explicit bounded block:

```text
[AUXILIARY_TERMINOLOGY_POLICY]
- Keep one novel-specific political title distinct from cultivation grades.
- Use one established rendering consistently for a recurring divine office.
[/AUXILIARY_TERMINOLOGY_POLICY]
```

Only those list items are propagated to auxiliary prompts. Translation-only
prose outside the block is not duplicated, and each novel loads only its own
addendum.

To add a future reusable profile, create
`profiles/<profile_id>/profile.json` and
`profiles/<profile_id>/translation_instructions.txt`, use the existing profile
schema, and select the ID from a novel's `novel.json`. Missing, malformed, or
unknown profiles fail locally before the production adapter can start API
work.

## Multi-Novel Projects

Each project has a `novel.json` and owns its complete mutable state:

```text
novels/<novel_id>/
    novel.json
    translation_addendum.txt    # optional novel-only policy
    source/
    data/
    output/
        chapters/
        pending/
        qa/
        audits/
        retroactive_fixes/
        epub/
    backup/
    logs/
```

Select a project explicitly for new workflows:

```powershell
python translate.py --novel example_project_d
python validate_project.py --novel example_project_d
python status_project.py --novel example_project_d
python operator_preflight.py --novel example_project_d
python build_checkpoint_epub.py --novel example_project_d
python extract_epub.py --novel example_project_d
```

`translate.py` remains the project-selection and subprocess isolation adapter
around the validated sequential production translator. It runs the production
launcher with the selected novel directory as its working directory, so pending
translations, state, logs, backups, and all output remain isolated.
`run_production_engine.py` first installs the validated model, provenance, and
semantic-shadow runtime layers, then lazily loads the now import-safe legacy
production module. The launcher owns the outer lifecycle boundary by explicitly
invoking `initialize_production_runtime_state()` followed by
`run_unattended_production()`. Detailed unattended sequencing now lives in
`production_lifecycle.py`: startup reconciliation/self-check, due-audit
dispatch, the sequential chapter loop, final-audit dispatch, and safe shutdown
handling. The legacy module keeps `run_unattended_production()` as the stable
compatibility callable that supplies live callbacks/state to the shared
lifecycle; its `main()` remains a compatibility entry point.

After all deterministic/local translation repairs have finished,
`production_commit_qa_checkpoint.py` owns the commit-QA checkpoint protocol
immediately before state extraction. It calls the existing legacy `local_qa()`
callback without owning QA policy, attaches the QA result to pending metadata,
invalidates stale downstream state/review metadata through the existing reset
callback when commit is blocked, and persists exactly one pending-metadata
checkpoint. Human-facing blocker output and logging remain in
`process_chapter()`.

Phase 12 adds `production_paid_request_journal.py` as a novel-owned fail-closed boundary around chapter translation, state extraction, and state-JSON repair. Before any of those provider calls, the exact request identity is durably recorded under `data/provider_request_journal/`; immediately before transport it becomes `started`. A durable `response_recorded` envelope can be replayed locally after a crash without another provider call. A `started` or `uncertain` record has no proven local response and therefore blocks automatic reissue until an operator investigates it. Request identity binds novel, chapter, operation kind, provider/model, profile, source hash, dependent translation/content hash when applicable, and the canonical provider payload. The active OpenAI-compatible client is constructed with `max_retries=0`, and the active compatibility retry wrapper now performs one scheduled application-visible attempt, so hidden SDK retries and broad same-process retries cannot bypass the journal. This is a local fail-closed duplicate-billing safeguard, not a claim of provider-side exactly-once billing.

After commit QA passes, `production_state_extraction_checkpoint.py` owns only
the recovery/persistence checkpoint around state extraction. It reuses saved
state for eligible pending stages only when the durable state-checkpoint
identity exactly matches the current novel/chapter source, pending translation,
profile/model, state prompt, ordered glossary-key context, and state request
settings. Missing or stale identity fails closed before any replacement provider
call. Newly extracted state persists that identity together with
`stage=state_extracted`. Saved chapter glossary review has a parallel durable
identity that additionally binds the review input hash, exact rendered relevant
glossary context, review prompt/schema, and review request settings. Text/QA
resets remove both identities with the downstream checkpoint data. Provider
prompts, provider calls, JSON salvage/repair, state-content policy, and
human-facing state-extraction/review policy remain in `process_chapter()`.

Phase 17 extracts the restart-safe glossary-review checkpoint sequencing into
`production_glossary_review_checkpoint.py`. The helper owns only exact saved
review identity verification, legacy saved-decision candidate-ID restoration,
saved accounting reuse, candidate-preparation checkpoint persistence, durable
chapter-review recovery-record wiring, and final `stage=reviewed` pending
checkpoint persistence. `process_chapter()` still supplies all validation,
repricing, provider-review, persistence, and operator-reporting callbacks, so
glossary policy, prompts, provider semantics, and Phase 16 failure
classification remain caller-owned.

Phase 16 separates controlled provider/request failures from non-provider
production failures. `production_failure_classification.py` defines typed
local persistence, local callback, provider-response/accounting, integrity, and
control failures. Paid-request journal safety errors and chapter-review
validation/recovery errors are non-provider failures. The shared lifecycle no
longer treats generic `RuntimeError` as an API error: provider transport errors
use provider diagnostics, typed local/recovery failures use local diagnostics,
and unclassified runtime defects propagate as unexpected errors. State/review
checkpoint persistence and saved-accounting repricing callbacks are wrapped at
their local boundaries without changing retry, prompt, pricing, or translation
policy.

`production_candidate_preparation.py` owns the next protocol boundary. It
prepares once, calls the existing conflict resolver only for a same-source
semantic conflict, and requires a second authoritative preparation after a
claimed resolution. Filtering, exact source identity, human-sidecar precedence,
automatic selection policy, and provider requests remain in existing callbacks.
Only known domain errors become controlled stops; unrelated exceptions escape.

After a chapter has passed that state checkpoint and glossary review,
deterministic in-memory reviewed-state finalization is owned by
`production_reviewed_state.py`. It invokes the existing
`apply_term_review()` policy callback without owning glossary policy, appends
prefilter rejections to reporting, updates continuity, assembles chapter issues,
and replay-safely replaces that chapter's issue records. The resulting mutable
state then passes directly to `production_chapter_commit.py`, which preserves
the established durable commit protocol: QA evidence first, final chapter text,
staged chapter accounting on a detached progress copy, associated
glossary/continuity/issues/audit files, then `progress.json` as the final durable
commit marker. Live in-memory progress is published only after that write. Pending files
are removed only after that progress write succeeds, Markdown first and metadata
second. Phase 27 proves and removes exact surviving reviewed residue before the
next model resolution; the metadata-only state created by an interrupted second
unlink is explicitly retryable. Provider/API stages,
commit-QA policy, state-extraction policy, and glossary policy remain in
`process_chapter()`.

Safety-critical one-chapter workflows use the same callable production-module
boundary rather than executing transformed copies of the production source.
Normal existing-pending recovery invokes only its approved target through
`process_chapter()`; the separately proven reviewed partial-commit mode invokes
only the local commit-tail reconciler. Controlled paid trials invoke
`run_unattended_production(target_chapter=...)`, whose shared lifecycle retains
the normal startup and audit sequencing while bounding chapter processing to
the exact approved next chapter. Trial approvals are bound to the selected scheduler
policy and the SHA-256 identity of the safety-critical trial/production code, so
an engine or execution-mode change invalidates stale approvals.

### Unified Safe Operator Preflight

Use the read-only operator router before deciding whether a novel should enter
normal production or bounded pending recovery:

```powershell
python operator_preflight.py --novel example_project_d --model pro
```

It composes the existing production-path preflight and, when a valid pending
translation exists, the bounded pending-recovery planner. It does **not** import
either paid executor, load provider credentials, authorize paid work, make API
calls, or write project state.

The top-level operator states are:

- `PRODUCTION_READY`: no pending translation exists and the validated
  production path is ready. The recommended entry point remains `translate.py`.
- `RECOVERY_READY`: an existing paid pending translation is valid and the
  bounded recovery planner proves an exact recovery route. The recommended next
  step is to review `pending_recovery_plan.py`; recovery execution remains a
  separate explicit authorization step.
- `CLEANUP_READY`: durable progress already committed a reviewed chapter but
  exact post-commit pending residue remains. Run
  `completed_pending_cleanup.py --novel <id>`; it is local-only and makes zero
  provider calls. Cleanup is required before production/recovery/complete routing.
- `PAUSED`: `pause.flag` is active for an unfinished novel. The report retains
  the route that becomes available after the pause is removed.
- `COMPLETE`: committed/progress state covers the configured final chapter.
- `BLOCKED`: contradictory or unproven state was detected. The router never
  guesses a source, model, target, or recovery path.

A trustworthy pending translation remains pinned to the model that generated
the paid text. For example, requesting Pro while a valid pending chapter was
generated by Flash produces an explicit Pro-to-Flash recovery override:
the operator request remains bound in the recovery authorization, but auxiliary
recovery pricing/runtime use the validated effective Flash model. Already-paid
translation generation remains forbidden during bounded recovery.

If a crash occurs after the final chapter text and committed QA have already
been written but before `progress.json` advances, bounded recovery has a separate
local-only reconciliation mode. It is available only when the final chapter
bytes exactly equal the paid pending Markdown, pending metadata is already
`reviewed`, committed QA exactly matches the saved passing QA checkpoint plus only its canonical pinned-model provenance when applicable, and
the saved reviewed/state inputs are complete and their durable Phase 15
checkpoint identities still match the reconstructed pre-commit source/text/
profile/glossary context. The planner records
`reconcile_reviewed_partial_commit_tail` in the approval fingerprint. Execution
then skips translation, state extraction, glossary review, and final/QA rewrites.
On normal unattended restart this reconciliation runs after startup integrity
checks but before any due periodic audits. It reconstructs the deterministic
reviewed state, accepts only associated-state
prefixes that are absent or already exactly equal to the expected result, and
finishes the remaining glossary/continuity/issues/audit/progress tail with
`progress.json` still acting as the final durable marker. Divergent or unproven
state remains blocked.

Bounded pending-recovery approvals retain exact SHA-256 identities for the
planner and high-risk recovery tools as operator diagnostics. They also bind a
conservative repository-owned runtime manifest covering every repository-root
Python engine module, every file in the selected immutable translation profile,
and `pyproject.toml` as the dependency-policy manifest. Any change to those
repository-owned runtime, profile-policy, or dependency-policy bytes makes the
previous state fingerprint and approval token stale, so a new read-only
recovery plan is required before paid recovery can proceed. The manifest does
not claim to fingerprint installed third-party package bytes or the whole host
environment.

Optional controlled-trial planning remains separate and read-only; the operator
router may point to `controlled_trial_plan.py` for a production-ready project,
but it never creates or executes a paid-trial authorization itself.

To copy the legacy root project without deleting or modifying it:

```powershell
python migrate_single_novel.py --novel example_project_d
```

Migration stages the copy, compares every migrated file byte-for-byte, and
atomically installs it. Repeating the command is safe when the trees are still
identical; an existing divergent destination is reported and never overwritten.

### Create a New Novel

Create a complete empty project without manually preparing directories or JSON:

```powershell
python create_novel.py `
    --id immortal_path `
    --title "Immortal Path" `
    --profile xianxia_xuanhuan `
    --final-chapter 1800
```

Optional arguments are `--author`, `--source-language`, `--target-language`,
`--audit-interval`, `--chapter-numbering`, `--source-type`, and `--source`.
Source and target languages
default to `zh` and `en`; the audit interval defaults to the current
production-safe value of 25, and chapter numbering defaults to `global`.

Novel IDs are lowercase slug-like identifiers containing only letters,
digits, underscores, and hyphens. They must start with a letter or digit.
Separators, traversal components, spaces, and uppercase letters are rejected.

Choose an existing profile such as `historical_military` or
`xianxia_xuanhuan`, or `infinite_flow`. For example, a future Infinite Flow
project can use `--profile infinite_flow`; its novel-specific system names and
mechanics still belong in that novel's glossary and `translation_addendum.txt`,
not in the reusable profile. The profile is fully validated before project
creation begins. Creation uses a verified sibling staging directory and an
atomic final rename, never overwrites an existing novel, and makes no API or
network calls.

Every created novel receives independent empty progress, glossary, continuity,
issues, audit history, chapter library, pending area, output directories,
backups, and logs. Creating one novel cannot initialize or change another.

### Import a Source EPUB

The safe setup sequence for a new novel is:

1. Create the project with `python create_novel.py ...`.
2. Copy its source EPUB to `novels/<novel_id>/source/novel.epub`.
3. Extract it with `python extract_epub.py --novel <novel_id>`.
4. Run `python validate_project.py --novel <novel_id>`.
5. Inspect the extraction report and source chapter library before later
   starting translation.

For example:

```powershell
python extract_epub.py --novel immortal_path
python validate_project.py --novel immortal_path
```

Extraction is local-only, makes zero API calls, and reads and writes only the
selected novel. It processes the EPUB in spine order, recognizes common
Chinese chapter headings, and reports the source SHA-256, chapter range,
missing or duplicate numbers, unmatched documents, and suspicious Hangul-like
corruption. A new library is fully extracted and structurally validated before
`data/chapters.json` is atomically installed. Extraction compares source-visible
text with extracted text and blocks installation when structural parsing loses
a severe proportion of a document. XHTML content containers may safely contain
nested elements and `<br>`-separated prose; confirmed nested site notices are
removed without discarding the surrounding narrative.

Most novels use `"chapter_numbering": "global"`: the visible source chapter
number is also the unique production chapter number. Novels whose visible
numbering restarts by volume or arc must opt in explicitly with
`"chapter_numbering": "spine_sequential"` (or pass
`--chapter-numbering spine_sequential` when creating the project). In that
mode, validated EPUB spine order assigns the continuous `production_index`
used by progress, pending recovery, committed filenames, QA, audits,
benchmarks, and checkpoint EPUB order. The library separately preserves each
unit's local chapter number/range, original heading, title, and inferred
numeric volume index. Repeated local numbers are therefore safe and never
become engine keys. Author notes remain excluded, and a combined heading such
as Chapters 71–72 remains one production unit unless the source contains a
real internal boundary.

When one XHTML contains matching `(1/2)` and `(2/2)` page portions of the same
numbered chapter, the extractor validates the number and normalized title,
keeps both prose portions in source order, and creates one production unit. A
number or title conflict blocks installation rather than being guessed.

Confirmed source-specific heading mistakes can be corrected without changing
the EPUB or weakening global heading detection. Add a novel-owned
`source_overrides.json` beside `novel.json`:

```json
{
  "source_sha256": "<64-character SHA-256 of source/novel.epub>",
  "chapter_number_overrides": {
    "Text/0139_.xhtml": 139
  },
  "supplement_documents": {
    "OEBPS/Text/status.xhtml": {
      "heading": "角色狀態"
    }
  },
  "content_cleanup": {
    "exact_paragraphs": [
      "A verified source-specific scraper line"
    ],
    "exact_text_fragments": {
      "OEBPS/Text/chapter.xhtml": [
        "an exact verified inline scraper fragment"
      ]
    }
  }
}
```

Overrides are exact EPUB member-to-chapter mappings. They apply only to the
selected novel, retain the original heading in `chapters.json`, and are listed
in the extraction report. The required source hash prevents overrides from
carrying over to a different EPUB edition. Invalid, duplicate, unknown, or
unapplied overrides stop installation. The extractor never creates overrides
or treats filename numbers as authoritative automatically.

Non-numbered documents remain excluded by default. A hash-bound
`supplement_documents` declaration can opt a genuine mechanics/status document
into a `spine_sequential` library as `unit_kind: "supplement"`. It receives a
continuous internal `production_index`, but no fake local chapter number.
Translation, recovery, QA, benchmarks, and EPUB ordering use the production
index; the reader sees only the translated supplemental title. Optional
`content_cleanup.exact_paragraphs` rules are likewise source-hash-bound and are
intended only for specific verified scraper remnants, never broad vocabulary
deletion. When verified debris is embedded inside an otherwise valid narrative
paragraph, `exact_text_fragments` binds an exact fragment to one exact EPUB
member. The fragment must occur exactly once in that member or extraction is
blocked; only the matched fragment is removed, leaving surrounding prose and
the original EPUB unchanged.

An existing populated chapter library is never replaced by default. After
reviewing the source and intent, explicitly opt in with:

```powershell
python extract_epub.py --novel immortal_path --overwrite
```

Before replacement, the prior bytes are preserved under the selected novel's
`backup/source_extraction/` directory. A failed extraction leaves the current
library unchanged.

### Import a Source TXT

Native UTF-8 TXT sources use the same canonical `chapters.json` boundary and
the same translator, QA, recovery, accounting, and English checkpoint-EPUB
builder as EPUB projects. They are not wrapped in a fake EPUB.

Create a TXT project and copy its authoritative source byte-for-byte in the
same atomic creation transaction:

```powershell
python create_novel.py `
    --id example_korean_novel `
    --title "Example Korean Novel" `
    --profile korean_fantasy `
    --final-chapter 350 `
    --source-language ko `
    --target-language en `
    --chapter-numbering spine_sequential `
    --source-type txt `
    --source "<LOCAL_PATH>"
```

Audit without installation, then install the validated canonical library:

```powershell
python extract_txt.py --novel example_korean_novel --dry-run
python extract_txt.py --novel example_korean_novel
python validate_project.py --novel example_korean_novel
python status_project.py --novel example_korean_novel
```

Extraction accepts UTF-8 with or without a BOM, records LF/CRLF style, retains
paragraphs, dialogue, Unicode, and scene separators, and assigns continuous
internal production indexes in physical source order. It supports ordinary
Korean `N화` headings, two-line `제 N화` headings, Prologues, Epilogues, and Side
Stories without inventing reader-facing chapter numbers for named units.
Source-number gaps or duplicates remain explicit metadata rather than being
silently renumbered.

TXT exceptions use the novel-owned `source_overrides.json`, with a required
`source_sha256` and a `txt` object containing exact `line_classifications` and,
when truly necessary, exact `heading_overrides`. Every rule binds a physical
line number to its exact expected text. A changed source hash or mismatched line
blocks extraction; no fuzzy override is applied. Metadata, duplicate closing
markers, and author afterwords remain in the authoritative TXT and are only
excluded from the canonical translation queue.

Authoritative TXT files and source-derived libraries can contain copyrighted
novel text and should remain local. This repository ignores imported TXT files,
installed `source/novel.txt` files, and the Example Novel extracted
library/inventory; add equally precise per-project ignores before committing a
future TXT import. Commit only reviewed configuration and non-text metadata.

After extraction, production remains format-independent:

```powershell
python translate.py --novel example_korean_novel
python build_checkpoint_epub.py --novel example_korean_novel
```

### List Novel Projects

List all configured projects and their independent checkpoints:

```powershell
python list_novels.py
```

The listing includes ID, title, profile, completed chapter, next chapter, final
chapter, and percentage complete. A malformed project is clearly marked
invalid without preventing other projects from being displayed. Listing is
read-only and makes zero API calls.

---

## 1. Open the Project

Open this folder in VS Code:

`<LOCAL_PATH>`

If the terminal prompt begins with:

`(.venv)`

the virtual environment is already active.

Example:

`(.venv) PS <LOCAL_PATH>>`

If it is not active, run:

```powershell
.\.venv\Scripts\Activate.ps1
```

---

## 2. Before Running Translation

From the repository root, choose one novel and inspect its actual checkpoint:

```powershell
$Novel = 'example_project_b'
Get-Content "novels\$Novel\data\progress.json"
Get-ChildItem "novels\$Novel\output\pending"
python validate_project.py --novel $Novel
python operator_preflight.py --novel $Novel --model pro
```

Keep paid pending files. Follow the preflight route and the
[operations guide](docs/OPERATIONS.md) before starting a writer.

---

## Paid Translation Benchmark

The maintained `benchmark_novel.py` command is a paid, non-committing
translation benchmark. It writes only to the selected novel's benchmark output
tree and verifies that protected production state remains unchanged. The CLI
acquires the selected novel's `.production.lock` before loading mutable
benchmark inputs and holds it through target claiming, provider transport, and
the final protected-state verification. A concurrent supported writer for the
same novel therefore stops the benchmark before stale glossary/source state can
be used for paid work.

Benchmark provider calls are deliberately single-attempt. The client disables
SDK retries with `max_retries=0`, and the runner does not automatically retry
ambiguous transport, timeout, rate-limit, or server failures. Before any provider
transport, the benchmark durably creates `results.json` with exclusive-create
semantics. That first file remains the same-target concurrency claim for direct
helper use and defense-in-depth: if another process has already claimed the
output target, the competing benchmark stops before any provider call. Failed or
interrupted benchmark evidence is preserved and the same output path is not
automatically reused. Choose any later paid rerun deliberately with a new
benchmark output target. This reduces duplicate-spend risk; it is not an
exactly-once billing guarantee and does not claim parent-directory power-loss
durability.

---

## 3. Run the Unattended Translator

For `PRODUCTION_READY`, explicitly select the novel and model:

```powershell
python translate.py --novel $Novel --model pro
```

This is a paid production command. Pro remains the default; `--model flash` is
an explicit opt-in. Use the model recorded in a pending recovery plan for its
approved route. Historical standalone translator variants are not recommended
operator entry points.

Supported mutating novel CLIs share the novel-local `.production.lock`.
Besides production, direct current-legacy `main()`, bounded recovery,
controlled trials, and the maintained paid benchmark, the same lock now covers
authorized committed-QA provenance backfill, source installation, glossary
conflict-resolution sidecars, checkpoint EPUB creation, normalization
inventory/plan/application writers, and the supported
finalization/title/publication/editorial writers. A second writer stops
immediately. Normalization `--dry-run` and `--verify-only` remain read-only
snapshots and do not request the lock; inventory/plan CLI runs do lock because
they publish reports. The OS releases ownership when the process exits,
including crashes; the empty file remains and must not be deleted to bypass an
active writer. Other read-only planning/dry-run/verify-only modes do not reserve
the novel. External editors and historical scripts do not participate, so keep
them away from active runs.

---

## 4. Automatic Off-Peak Mode

The translator has a toggle similar to:

```python
AUTO_OFF_PEAK_ONLY = True
```

When `True`:

- no new DeepSeek API request starts during configured peak windows;
- the program sleeps locally;
- waiting consumes zero API tokens.

When `False`:

- the translator can run at any time;
- dynamic cost calculation should still select the correct peak/off-peak rate.

Configured DeepSeek V4 Pro peak windows, Monday through Friday only:

- 01:00-04:00 UTC
- 06:00-10:00 UTC

All other times are off-peak. This includes the weekday 04:00-06:00 UTC gap
and every hour on Saturday and Sunday. Classification always uses UTC, never
the local machine timezone.

---

## 5. Manual Pause

To request a safe pause from another PowerShell terminal:

```powershell
New-Item "novels\$Novel\pause.flag" -ItemType File -Force
```

The program will finish the active API operation and stop before the next one.

To resume:

```powershell
Remove-Item -LiteralPath "novels\$Novel\pause.flag"
```

If you want to shut down the PC:

1. create `pause.flag`;
2. wait for the translator to report that manual pause is active;
3. press `Ctrl+C`;
4. shut down;
5. later reopen VS Code;
6. remove `pause.flag`;
7. run the same translator command again.

---

## 6. Recovery

The translator uses:

`novels/<novel_id>/output/pending/NNNN.md`

and:

`novels/<novel_id>/output/pending/NNNN.json`

If translation succeeded but a later step failed, the paid English translation is kept there.

On restart, the program should reuse it instead of calling DeepSeek for translation again.

Use the [bounded recovery workflow](docs/OPERATIONS.md#recover-an-existing-paid-pending-chapter)
for an existing paid chapter. It separates read-only planning, exact approval,
and paid execution. Never clear pending files to force another translation.

Original glossary review now checkpoints its operation intent before the
request. If it has no valid durable response after interruption or a failed
write, restart and operator preflight stop rather than repeating possible paid
work. Preserve the record and inspect its operation ID; unknown usage is not
invented. Valid previously saved responses remain reusable.

### Explicit glossary-candidate conflict resolution

Conflicting semantic proposals for one exact source glossary key stop before
glossary review. They are never resolved by candidate order or by automatic
category preferences. After source review, select one of the reported complete
semantic fingerprints with the local-only command:

```powershell
python resolve_glossary_candidate_conflict.py `
  --novel <novel_id> `
  --chapter <number> `
  --source "<exact source key>" `
  --choose <full semantic SHA-256> `
  --rationale "<human/source-review basis>"
```

The command makes zero API calls and writes an atomic novel-owned sidecar at:

`novels/<novel_id>/data/glossary_candidate_resolutions/NNNN.json`

The sidecar retains the original conflicting proposal evidence and binds the
approval to the novel, chapter, paid-pending text hash, order-independent state
extraction hash, source concept, complete proposal set, and conflict
fingerprint. The raw pending Markdown and state extraction are not modified.
An identical command is idempotent; a different selection cannot silently
overwrite an existing approval. Stale, malformed, or hash-mismatched sidecars
stop locally before a glossary-review request.

### Opt-in automatic glossary-candidate conflict resolution

A novel can opt in to the selection-only recovery path for
`same_source_semantic_conflict`:

```json
"glossary_candidate_auto_resolution": {
  "enabled": true,
  "allow_model_adjudication": true,
  "max_model_requests_per_chapter": 1,
  "category_normalizations": {}
}
```

An exact valid human v1 sidecar always runs first and is never replaced or
updated by the automatic resolver. Without a sidecar, Tier 1 costs $0 and may
select only a uniquely matching exact glossary identity or a uniquely proven
category supplied by the explicit `category_normalizations` map. An empty map
defines no category preference: for example, `creature` and `monster` remain
ambiguous. Candidate order, fingerprint order, note length, note wording,
majority, and first-position preferences are never evidence.

If Tier 1 is incomplete and model adjudication is explicitly enabled, all
remaining conflicts in that chapter are sent in one compact request. The model
sees short conflict/proposal IDs and can only select one existing proposal ID
or abstain. Unknown fields, missing/duplicate decisions, cross-conflict IDs,
semantic edits, new proposals, malformed JSON, and abstention all fail closed;
there is no JSON repair, correction request, or retry. The attempt and response
are checkpointed in pending metadata under a deterministic operation ID, and
usage is accounted once before an automatic v2 sidecar is installed. A restart
revalidates a recorded response locally and never reissues that operation.
The request is additionally capped at 20 conflicts, eight existing proposals
per conflict, 24,000 user-prompt characters, and 800 output tokens; an
oversized batch stops for manual resolution without making the request.

Automatic v2 sidecars retain structured method, policy, proof or adjudication
provenance while existing `glossary-candidate-resolution-v1` records remain
fully valid. The local manual command above remains the final fallback.

### Opt-in residual-CJK micro-repair

The residual-CJK commit blocker remains authoritative. A novel may explicitly
opt in to one small contextual repair request per paid-pending content hash:

```json
"residual_cjk_micro_repair": {
  "enabled": true,
  "max_total_cjk_chars": 12,
  "max_fragments": 3,
  "max_affected_lines": 3,
  "max_attempts_per_pending_version": 1
}
```

The repair request contains only precisely affected English lines, tiny source
windows, relevant locked terminology, and the compact novel terminology policy.
It never retranslates the full chapter. Every returned patch is locally
anchored and bounded, then residual-CJK and strict locked-term QA run again
before an atomic pending-text replacement. Ineligible or rejected repairs keep
the paid pending text unchanged and stop for manual review. Paid repair usage is
tracked separately by a stable operation ID, including when a response is
locally rejected; the full-translation statistics are never counted twice.

---

## 7. Build a Checkpoint EPUB

Checkpoint EPUB generation is local-only, uses zero API calls, and costs $0 in
API credit. Each novel reads only its own progress and committed chapter files
and writes its own independent EPUBs.

Build the complete safely committed checkpoint for a novel:

```powershell
python build_checkpoint_epub.py --novel example_project_d
```

The default range is Chapter 1 through the selected novel's
`progress.last_completed`. For example, Ming through Chapter 204 produces:

```text
novels/example_project_d/output/epub/example_project_d_chapters_0001_0204.epub
```

Build a smaller committed range:

```powershell
python build_checkpoint_epub.py --novel example_project_d --from-chapter 1 --to-chapter 100
python build_checkpoint_epub.py --novel example_project_d --from-chapter 101 --to-chapter 204
```

The script reads only the selected novel's:

- `novel.json`;
- `data/chapters.json` when available, for source-unit/title identity;
- `data/progress.json`;
- `output/chapters/*.md`.

It writes only under:

`novels/<novel_id>/output/epub/`

Pending translations are never included. Missing committed files,
unexpected numeric chapter files, inconsistent progress, and invalid requested
ranges stop the build before the checkpoint is written.

---

## 8. Important Directories

These paths are relative to the selected `novels/<novel_id>/` directory.

```text
data/
    chapters.json
    glossary.json
    continuity.json
    issues.json
    progress.json
    audit_history.json

output/
    chapters/
    pending/
    qa/
    audits/
    retroactive_fixes/
    epub/

backup/
```

---

## 9. Glossary Behavior

The glossary is persistent and automatically maintained.

Statuses:

- `locked`: the canonical translation decision is authoritative; automated
  review must preserve it. This alone does not impose hard lexical commit QA.
- `provisional`: the current preferred rendering, subject to review.
- `contextual`: approved alternatives selected by context, not exact constraints.

Profile `high_priority_categories` expresses terminology attention and review
importance. It never makes all locked entries in those categories hard blockers.
The established legacy compatibility gate remains the intersection of that
profile list with `STRICT_LOCKED_GLOSSARY_CATEGORIES` (currently only
`cultivation realm`). This preserves existing realm protection and the historical
profile boundary; it is not a mechanism for adding arbitrary hard categories.

Read-only `python validate_project.py --novel <novel_id>` reports locked,
high-priority locked, legacy hard, configured explicit strict, activation-bounded,
configured hard, accepted-form, and repair counts. With the explicitly reported
next-chapter identity it also reports active/future explicit and effective hard counts. Legacy and explicit counts overlap; the overlap is reported.
Raw explicit declarations are counted even when invalid, with validation errors
reported separately. Counts do not authorize production execution.

An individual locked entry may additionally set `"strict_qa": true`. Its
complete canonical English value then becomes a source-anchored commit
requirement without making the entire glossary category strict. The field is
optional: absence and `false` mean no explicit opt-in and never disable legacy
category-based strictness. `strict_qa: true` is invalid on a non-locked entry.
Explicitly strict characters retain referential identity QA: an established
canonical identity can cover repeated mentions rendered with pronouns; full names
need not repeat at every source occurrence.

To prospectively approve an additional hard constraint, review its exact source
identity, canonical target, category, and status, then separately authorize a
novel-owned metadata edit such as:

```json
{
  "english": "Youxi Clan",
  "category": "clan",
  "status": "locked",
  "strict_qa": true,
  "strict_qa_from_chapter": 397
}
```

For this prospective example, `Youxi clansman` fails beginning at Chapter 397.
Chapters 1–396 do not gain this explicit hard constraint. Setting the flag does not approve semantic
derivatives. Separately review any necessary final alternatives through
`accepted_inflections`; do not use repair aliases as acceptance evidence.
The model must not autonomously set `strict_qa` or `strict_qa_from_chapter`. Check validator counts and
source-anchored compatibility before/after the proposed opt-in. Keep live
metadata approval separate from engine implementation: this policy change
migrates no production glossary, rewrites no committed prose, and does not
authorize paid burn-in. Any later local Great King opt-in requires its own
reviewed before/after evidence after this engine PR is reviewed and merged.

`strict_qa_from_chapter` is an optional inclusive activation boundary for the
explicit origin only. It requires `status=locked`, `strict_qa=true`, and a
positive integer; booleans, null, floats, strings, zero, and negatives are invalid.
Malformed metadata is rejected without coercion. An absent boundary preserves
PR #153 unbounded enforcement. Legacy cultivation-realm protection remains
active before a future explicit boundary and reports both origins afterward.
Locked authority and high-priority attention retain their separate meanings.

Prompt rendering, commit QA, canonicalization, deterministic repair, and recorded
repair validation receive the actual operation/artifact chapter explicitly.
A pending Chapter 397 is checked as 397 even after restart; a historical 396
artifact uses 396, independently of current progress. Shared runtime helpers
raise if a bounded rule is evaluated without chapter context. Generic metadata
inventory uses the clearly named `configured_strict_glossary_entry_items` and
`configured_strict_glossary_entry_origin` helpers instead. Validator next-chapter
counts are a labeled forecast, never a substitute for artifact identity.

Before activation, the future explicit rule adds no mandatory canonical marker,
hard blocker, or strict repair eligibility. At activation it uses the existing
prompt/QA/repair machinery. Approved final grammatical forms and repair aliases
remain distinct. An activation boundary does not grandfather an unapproved form
as accepted English and does not authorize historical editing.

PR #153 correctly separated hard enforcement from glossary authority and review
importance. The later unbounded Youxi opt-in dry-run found 16 newly failing
committed chapters (120, 175, 214, 333, 337–341, 354, 358, 367, 368, 370, 373,
396) and was blocked without production mutation. Adding an accepted-inflection
exception merely to pass that old corpus would weaken future policy. The
prospective boundary addresses that historical impact without changing prose.

After independent review and merge, a separately authorized operator/local
metadata operation may propose both fields for Great King. Verify exact source,
canonical target, locked status, boundary 397, pending/journal compatibility, and
a zero-provider before/after scan first. Current hard selections remain Ace 0,
Great King 10, Guhuo 6, Ming 0, Rogue 11 (total 27). The future configuration adds
one configured explicit entry; Chapter 396 remains Great King 10 / total 27,
while Chapter 397 becomes Great King 11 / total 28. This PR performs no live
metadata operation, paid burn-in, or retroactive repair hardening.

The synthetic acceptance suite scans 396 artifacts in CI. An authorized local
read-only corpus can additionally reproduce the historical proof with:

```powershell
$env:NT_READONLY_HISTORICAL_ROOT = '<LOCAL_PATH>'
python tests/zero_provider_validation.py -q -s `
  --basetemp="$env:TEMP/nt-prospective-history" `
  tests/test_prospective_strict_qa.py `
  -k "historical_396 and not test_real_windows_dpapi_round_trip"
Remove-Item Env:NT_READONLY_HISTORICAL_ROOT
```

The scan copies glossary metadata in memory, compares chapter-aware strict
violations before/after, verifies all 396 artifacts, tests canonical/derivative
Chapter 397 output, and checks read-only input hashes. It never writes novel data.

An effectively strict locked entry may explicitly list grammatical forms that
are also valid final English with `"accepted_inflections": ["..."]`. These
forms participate in source-occurrence accounting alongside the canonical
target and are never normalized back to it. Accepted inflections must be
unique, must not repeat the canonical target, and must not overlap
`repair_aliases`.

Strict QA also derives one conservative regular English plural automatically
for countable categories: `progression rank`, `rank`, `class`, `profession`,
and `military rank`, plus multiword `system term` entries. It pluralizes only
the final plain-ASCII word (`-y` to `-ies`, regular `-es` endings, otherwise
`-s`) and skips punctuation, obvious plural forms, noncountable endings, and
uncertain forms. Every other category remains exact. Set `"auto_plural":
false` on an effectively strict locked entry to disable this derivation;
explicit `accepted_inflections` continue to work independently.

Every locked entry in the effective strict set is eligible for automatic
pre-commit repair by default. This includes both legacy category-based strict
terms and entries with `"strict_qa": true`. Set `"auto_repair": false` only
for a deliberate per-entry hard stop; `"auto_repair": true` remains a valid,
redundant explicit opt-in.

Optional `"repair_aliases": ["..."]` values are approved wrong or partial
input renderings only; they never satisfy final QA and cannot be combined with
`"auto_repair": false`. Aliases must be unique non-empty strings and must not
repeat the canonical target. Safe whole-phrase case, spacing, punctuation,
hyphenation, parenthesis, and cautious singular/plural repair candidates may be
derived locally. A repair candidate is not valid final output unless it is an
explicit accepted inflection or a category-gated automatic plural. Source
anchors and approved target-family renderings are occurrence-counted with
longest matching terms claimed first, so a base term cannot steal an occurrence
from its longer compound. Unambiguous deterministic repair costs zero.
Unresolved bounded failures may use one restart-safe scheduled terminology
request plus at most one correction request when the first response is exactly
an empty patch list or fails only the explicit bounded instruction-compliance
code set. Requests expose validator-derived character/word/delta limits plus
QA-owned protected target spans and their bounded editable complement. A patch
cannot consume an already-correct protected span. Canonical gain uses the same
exact phrase boundaries as strict QA. Each request has its own idempotent
operation ID under the pending hash, policy version, and strict-failure
identity. Malformed, unsafe, unrelated, out-of-region, ambiguous,
CJK-introducing, API-failing, or mandatory-QA-failing responses never receive
a retry. Region-anchored rejected patch fields may be retained as bounded
diagnostics; full chapters and model reasoning are never retained there.

An effective-strict entry may explicitly configure mappings such as
`"repair_localization_anchors": [{"anchor": "partial form", "target":
"Exact Accepted Target"}]` for terminology-sized partial target forms that are
known to be insufficient and never satisfy strict QA. Each target must be an
exact member of that entry's accepted strict-QA target family. These
anchors do not authorize deterministic replacement. They may corroborate one
otherwise exact source/target segment alignment only when every other occurrence
group aligns, exactly one deficit accounts for the full missing count, matching
groups bracket it on both sides, physical line structure is unchanged, and the
candidate segment contains exactly the configured partial form outside protected
accepted targets. Ambiguous anchors or paragraph drift remain ineligible.
When such an anchor maps by exact phrase boundaries to an already approved
inflection (for example, a configured plural), the repair contract permits only
that complete target-family form and still requires a positive strict-QA target
delta. The partial localization anchor itself never becomes valid final prose.

```json
{
  "english": "Canonical Term",
  "category": "system term",
  "status": "locked",
  "strict_qa": true,
  "auto_repair": false
}
```

In this example strict QA remains active, but automatic repair is deliberately
disabled. Omitting `auto_repair` would enable the normal repair pipeline. A
valid paid pending translation is always reused; terminology repair never
requires regenerating the full chapter. Unsafe or ambiguous repairs preserve
the pending text and stop for manual review.

State extraction revises the exact prior committed continuity together with the
current source and accepted English translation in the existing provider operation.
This is bounded rolling working memory (prompt limit: 200 English words), not
concatenated chapter recaps. Still-relevant durable facts should survive silence;
source evidence supersedes old values and resolves obligations/threats. Offers,
plans, beliefs, and predictions retain their status, attribution, and uncertainty.
The current source outranks accidental overstatement in the English translation.

The versioned state identity persists the complete original continuity object and
its hash in pending metadata before extraction. It also binds novel/chapter,
profile/model, source/accepted translation, prompt/policy, and glossary-key context.
Recovery replays exact recorded responses locally; incompatible old state
checkpoints or journals block without automatically regenerating paid work.
Old reviewed pending residue also requires explicit resolution; no automatic
checkpoint migration is performed.
Translated-only pending prose remains reusable. Partial-commit reconciliation
reconstructs the old baseline from the saved identity even after continuity has
advanced, accepts only exact old/new continuity and ordered commit prefixes,
and keeps progress.json as the final durable chapter marker.

This adds no provider call and changes no terminology selection or production
model policy. Synthetic transition/replay tests protect request wiring and crash
safety; they cannot prove provider comprehension or indefinite memory retention.
Controlled consecutive-chapter burn-in must measure long-range quality before
sustained production. Explicit terminology enforcement is documented above.

Named martial arts / techniques use capitalization like:

`Black Tiger Seizes The Heart`

The automatic system performs periodic glossary audits and may safely propagate exact terminology corrections backward into already committed chapters.

Periodic audits keep the configured chapter window but divide its glossary
candidates into deterministic bounded chunks. Each chunk is independently
parsed and validated, and validated chunks are atomically checkpointed under
`output/audits/`. A restart resumes at the first incomplete chunk rather than
repaying for completed chunks. Known output-length truncation skips pointless
whole-response JSON repair and subdivides only the failed chunk; the live
glossary is updated once, after every chunk has validated.

New glossary source keys are accepted only when they are exact literal spans
of the authoritative source unit. The engine assigns each source-backed term an
immutable opaque candidate ID; review and audit models may change English,
category, action, and other approved metadata, but cannot create or rewrite the
source-side identity. Periodic audit requests expose deterministic, request-local
aliases (`c01`, `c02`, ...) instead of requiring the model to reproduce the
opaque ID. The engine resolves those aliases exactly back to durable IDs before
validation; unknown, duplicate, or omitted aliases are rejected, while
checkpoints and candidate-set hashes continue to use immutable internal IDs.
Periodic audits exclude unbacked provisional history and
preserve unresolved locked history without silently deleting it. A parsed audit
chunk with only an allowlisted semantic field error (for example, an empty
action) may receive one bounded corrective retry. Unknown, rewritten, omitted,
or duplicate candidate IDs remain hard stops.

Chapter-level glossary review uses the same exact request-local ID transport.
Its parsed original response and usage are checkpointed in the paid pending
metadata before validation. An identity-safe, allowlisted semantic field error
may receive exactly one corrective request; merge targets must remain exact
existing source glossary keys, never English renderings. The correction sees
only the bounded candidate/decision context. Valid saved results and their
accounting are reused after restart, while identity mutations remain hard stops.

Retroactive edits are backed up and logged.

---

## 10. Source Corruption

The source EPUB contains corrupted glyphs.

Do not panic if QA reports suspicious Hangul characters.

The translator is designed to reconstruct intended meaning from context where reliable.

Routine corrupted glyphs should not be dumped into giant state-extraction issue lists.

---

## 11. Recommended Codex Usage

Before asking Codex to modify the project, tell it:

```text
Read AGENTS.md, PROJECT_CONTEXT.md, and README.md first.
Then inspect the current repository state, data/progress.json, and output/pending/.
Do not modify anything until you understand the current checkpoint and recovery state.
```

For a code change, a good prompt is:

```text
Read AGENTS.md, PROJECT_CONTEXT.md, and README.md first.
Inspect the current implementation before editing.
Make the smallest safe change needed for the following task:

<describe task>

Preserve pending recovery, glossary consistency, off-peak scheduling, cost tracking,
automatic audits, and retroactive correction safeguards.
Run an appropriate syntax/test check when finished and summarize exactly what changed.
```

---

## 12. Apply an Audited Normalization Plan

`apply_normalization_plan.py` is a local-only, fail-closed finalization tool.
It always reads the immutable `novels/<novel_id>/final/chapters/` corpus and
publishes a separate `final/normalized_chapters/` corpus. It never calls a
translation API, never uses an existing normalized corpus as input, and
currently implements only the `prose` layer.

First resolve and validate every approved occurrence without writing chapters:

```powershell
python apply_normalization_plan.py `
    --novel example_project_e `
    --layer prose `
    --dry-run `
    --allow-manual-approved `
    --expect-baseline-files 351 `
    --expect-baseline-bytes 5224006 `
    --expect-baseline-sha256 01c286974c5bfd21de5957e20d811d66a8887a71980814fc36e3aa0a97449cc3 `
    --expect-approved-records 33 `
    --expect-automatic-occurrences 235 `
    --expect-manual-occurrences 4 `
    --expect-total-occurrences 239
```

After the dry-run passes, replace `--dry-run` with `--apply` to stage,
independently verify, and atomically publish the new corpus. The explicit
`--allow-manual-approved` option is required for approved exact anchors marked
`automatic_safe: false`; it does not authorize `REVIEW_REQUIRED` or
`FALSE_POSITIVE_NO_CHANGE` records.

Independently re-verify an already published corpus and its authoritative JSON
application report:

```powershell
python apply_normalization_plan.py `
    --novel example_project_e `
    --layer prose `
    --verify-only `
    --allow-manual-approved `
    --expect-automatic-occurrences 235 `
    --expect-manual-occurrences 4 `
    --expect-total-occurrences 239
```

Application always refuses an existing terminal `normalized_chapters` corpus.
If that corpus does not exist, an interrupted report/report-stage/staging prefix
may be removed only after the current plan and immutable baseline independently
prove every surviving artifact. Normalization JSON permits only its validated
UTC `generated_at_utc` value to differ; all other semantics, Markdown, and
staged corpus content must match. Divergent or unprovable residue fails closed.
After a stale stage is proven, Phase 26 records a proof-bound cleanup journal and
renames the stage to a cleanup tombstone before recursive deletion. Phase 28
publishes that journal from an exclusive file-fsynced pre-journal scratch file
before any stage rename; an interrupted pre-publication write therefore leaves
the original stage intact. Scratch bytes never authorize deletion and are removed
only after the stale-stage inventory re-passes its workflow proof. Orphan or
ambiguous scratch still blocks. If recursive tombstone deletion is interrupted, a
retry may finish only the journal-authorized tombstone; unknown tombstones or proof
drift still block. There is no generic overwrite or regeneration mode.

---

## 13. Apply an Approved Title-Resolution Plan

`apply_title_resolution_plan.py` is a local-only, generic, fail-closed title
applicator. It reads `final/normalized_chapters/`, validates the complete
authorized anchor set from `final/reports/title_resolution_plan.json`, and can
publish a separate `final/titled_chapters/` corpus transactionally. It never
calls a translation API and never overwrites an existing titled corpus. When no
terminal titled corpus exists, exact interrupted application-report/report-temp
or staging residue may be removed only after independent byte/corpus proof;
divergent residue remains a hard blocker. Proven stale-stage cleanup uses the
same Phase 26 proof-bound journal/tombstone protocol as normalization and
publication cleanup so a partial recursive delete can be resumed safely.

Validate every approved Rogue anchor without writing chapter files:

```powershell
python apply_title_resolution_plan.py `
    --novel example_project_e `
    --dry-run `
    --allow-manual-approved `
    --expect-plan-sha256 29d86848e2b856ba963c3a488d46eac421b8ca77588509ed7e0cff82f1449995 `
    --expect-approved-targets 264 `
    --expect-existing-anchors 261
```

After a complete dry-run passes, replace `--dry-run` with `--apply`. The
`--allow-manual-approved` flag deliberately authorizes approved canonical
decisions marked unsafe for unattended automatic application; it never
authorizes `REVIEW_REQUIRED`, `STRUCTURAL_ONLY`, or `ALREADY_CONSISTENT`
semantic edits. Use `--verify-only` to independently compare a published
titled corpus with the immutable normalized input and bind it to the exact
plan and application-report hashes. Missing physical titles remain
non-executable unless the plan contains a unit-specific human-authorized
recovery insertion anchored to the exact input bytes and original first line.

---

## 14. Safe Engineering Workflow

Keep production on `main`. Create verified Git and live-novel backups, then
use a separate branch/worktree for development. Git history does not protect
ignored or untracked paid translations.

```powershell
git status --short
git worktree add -b engineering/my-change <LOCAL_PATH> main
```

Run tests in that worktree with `DEEPSEEK_API_KEY` unset and no `.env` file.
Stage only reviewed code/documentation paths explicitly. Do not use broad
`git add .`, restore, reset, clean, or stash commands on production to conceal
its valuable dirty novel state. Review `git diff --cached` before committing.

---

## 15. Files That Should Not Be Committed Publicly

Keep `.env` private.

Your `.gitignore` should include at least:

```text
.env
.venv/
__pycache__/
*.pyc
```

Do not publish API keys.

---

## 16. Local Status and Run Logs

Show lightweight local observability status without making API calls:

```powershell
python status_project.py --novel <novel_id>
```

For a fail-closed decision about what the operator should do next, use:

```powershell
python operator_preflight.py --novel <novel_id> --model pro
```

Production runs write best-effort structured JSONL events under `logs/`.
Logging failures are non-fatal and never stop translation.

The safest candidates for a future small configuration file remain the
off-peak-only toggle and pause/schedule polling intervals. Paid-request retry
ownership is now safety-critical: the active production client disables SDK
retries and ambiguous paid outcomes are not automatically reissued. Final chapter and audit interval are
workflow invariants and should remain guarded against accidental changes.


## Production provider selection

Direct DeepSeek remains the default. The explicitly selected GOAT backend uses
Command Code Provider with logical `--model v41-flash`, separate provider-bound
approval and credit-equivalent allowances. Start with the read-only command:

```powershell
python operator_preflight.py --novel <novel_id> --model v41-flash --provider command_code_provider
```

See [Production provider backends](docs/PRODUCTION_PROVIDER_BACKENDS.md) for
credential activation, exact policy, journal/recovery guarantees and rollout
limits. Initial GOAT rollout requires independent review and a separately
reviewed live smoke-test plan before any paid chapter execution.

GOAT `translate.py`/normal unattended execution is disabled during initial
rollout. Supply operator-selected `--max-api-calls` and
`--max-credit-equivalent-usd` to operator preflight for a complete bounded
controlled-trial planning command. Paid execution still requires the matching
fingerprint/token and explicit authorization through the bounded executor.

## Selected GOAT production policy

Existing paid pending work with an exact empty terminal state content-filter
response has a separate [explicit GOAT terminal-state recovery].
Ordinary production remains fail-closed. The new route settles definitive usage
once and saves a normal state checkpoint before existing pending recovery owns
review/commit; it requires its own operator authorization.

Completed GOAT periodic audits with uncommitted local publication use the
[local-only audit finalization planner/executor].
Any paused production incident requires independent review and separate
operator authorization before pending paid requests are resent.

Nonzero direct accounting requires a separately approved one-way accounting epoch
migration before GOAT activation. The [migration guide](docs/GOAT_ACCOUNTING_EPOCH_MIGRATION.md)
describes the local-only planner/executor, sealed direct history, separate active
GOAT counters, and pending/journal blockers. Preflight never applies migration.

The selected Command Code Provider primary translation policy is **DeepSeek V4.1
Flash / low / 50K**, defined independently in `production_goat_policy.py`.
GOAT auxiliaries use **low / provider-unbounded request**: both `max_tokens`
and `max_completion_tokens` are omitted. Per-operation attempts remain bounded;
there is no automatic retry or fallback. Credit/send authorization remains mandatory.
The reviewed physical context bound is 1,000,000 tokens, cryptographically bound
to a local evidence artifact. No separate output bound is claimed. Evidence is
rehashed before authorization; worst-case credit reservation is $0.75 off-peak
or $1.50 peak, without an auxiliary output cap. See the [auxiliary policy and dedicated
paid-pending state migration].
Every translation
LENGTH response is incomplete, including fluent visible English. Exact accepted,
hash-verified near-ceiling LENGTH (49,936–50,000) may receive one separately
budgeted, durably journaled full translation with the identical 50K LOW wire body.
No continuation, stitching, larger/uncapped ceiling, High reasoning, hidden retry,
or direct-provider fallback is permitted. Ambiguous requests never resend.

The existing per-novel opt-in residual source-script micro-repair follows successful
STOP output, including recovered output. GOAT allows one repair request, with
source anchors, existing character/fragment/line/patch limits, residual re-scan,
and full local QA before commit. A rejected repair does not get a corrective send.
Frozen G7J 24K and PR146 v1 24K evidence retain their original semantics.
See [production policy details].
