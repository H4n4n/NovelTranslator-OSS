# Operations

Run these commands from the production repository root after reviewed code has
been merged and fast-forwarded. Engineering worktrees do not contain live
ignored output and must not be used to continue the real novels.

## Select and inspect a novel

```powershell
Set-Location <LOCAL_PATH>
$Python = '<LOCAL_PATH>'
$Novel = 'example_project_b'  # Change this deliberately.
& $Python list_novels.py
& $Python validate_project.py --novel $Novel
& $Python status_project.py --novel $Novel
& $Python operator_preflight.py --novel $Novel
```

These commands make no provider calls. Preflight is a read-only decision, not
spending authorization. Default GOAT routes to `BOUNDED_ALLOWANCE_REQUIRED`
unless both limits are explicitly supplied, then `BOUNDED_GOAT_READY` recommends
the [bounded unattended plan](GOAT_UNATTENDED.md). `PRODUCTION_READY` routes to normal production;
`CLEANUP_READY` routes first to the local cleanup command below;
`RECOVERY_READY` routes to the pending plan; `COMPLETE` needs no further
translation. `PAUSED` retains its underlying route. Investigate `BLOCKED`
before running a writer. A successful preflight is a snapshot, not a reservation.

If `CLEANUP_READY` is reported, run only the proven local cleanup and then rerun
preflight:

```powershell
& $Python completed_pending_cleanup.py --novel $Novel
& $Python operator_preflight.py --novel $Novel --provider direct_deepseek --model pro
```

This route exists for the narrow state after durable `progress.json` where
reviewed pending residue survived cleanup. It accepts an exact Markdown+JSON pair
or the JSON-only retry left when Markdown deletion succeeded first. Current
source, committed final, committed QA, saved QA/checkpoint identities, and pinned
model provenance must agree. Markdown-only, malformed, divergent, symlinked, or
otherwise unproven residue is preserved and blocks. The cleanup makes zero
provider calls and does not change progress or committed output.

## Paid translation benchmark

`benchmark_novel.py` is a paid, translation-only comparison route. It does not
commit chapters, progress, QA, glossary, continuity, issues, or audit state; its
artifacts stay under the selected novel's `output/benchmark/` tree. The CLI
acquires the selected novel's `.production.lock` before loading mutable source,
profile, and glossary inputs and keeps that lock through target claim, provider
transport, result publication, and final protected-state verification. A
concurrent supported writer for the same novel therefore stops the benchmark
before stale benchmark inputs can reach a paid provider request.

Each benchmark chapter owns exactly one application-visible provider attempt.
The OpenAI-compatible client is configured with `max_retries=0`, and transport,
timeout, rate-limit, or server failures are not automatically reissued. Before
the first provider transport, the selected benchmark target is also claimed by an
exclusive-create `results.json` write that flushes, file-fsyncs, and verifies
exact readback. That file remains defense-in-depth for direct helper use and
same-target collisions. If claim durability/readback cannot be proven, the
final-named claim is deliberately preserved as blocking evidence and provider
execution does not begin.

If an attempt later fails after its remote outcome becomes uncertain, the
benchmark records a failed result and preserves the existing output path. Do not
delete or overwrite that evidence merely to retry; choose a new deliberate
benchmark output target only after deciding that another paid request is
appropriate. This avoids automatic duplicate spend but does not promise
exactly-once billing or parent-directory sudden-power-loss durability.

## Normal production and pause

Only after `PRODUCTION_READY`, start the intended paid run:

```powershell
& $Python translate.py --novel $Novel --provider direct_deepseek --model pro
```

GOAT V4.1 Flash is the default; follow [bounded GOAT authorization](GOAT_UNATTENDED.md).
This command explicitly selects direct DeepSeek Pro. Legacy `flash` is recovery-only. Production is
sequential through the configured final chapter and runs due glossary audits.
Pending provenance pins already-paid text to its generating model.

From another terminal in the repository root, select the same `$Novel`:

```powershell
New-Item "novels\$Novel\pause.flag" -ItemType File -Force
```

Wait for `MANUAL PAUSE ACTIVE` before Ctrl+C for a planned shutdown. Pausing
waits locally before the next provider operation; the current request may
finish first. To resume, remove only this novel's pause file and rerun preflight:

```powershell
Remove-Item -LiteralPath "novels\$Novel\pause.flag"
& $Python operator_preflight.py --novel $Novel --provider direct_deepseek --model pro
```

Follow its new route; a paused/stopped run may now have paid pending work.
The root `pause.flag` controls only the legacy root workflow.

The current production schedule toggle is the existing
`AUTO_OFF_PEAK_ONLY` constant in the current legacy production module. There is
no normal-production `--allow-peak` CLI option. With the toggle enabled, new
requests wait during configured weekday UTC windows 01:00-04:00 and 06:00-10:00.
Weekends are off-peak. Toggle changes are code changes and invalidate affected
approvals; pricing still uses the actual request period when waiting is disabled.

Supported mutating novel CLIs acquire the same novel-local OS lock. This now
includes normal production, paid recovery/trials, the maintained paid benchmark,
authorized committed-QA provenance backfill, source EPUB/TXT installation,
glossary-candidate conflict sidecars, checkpoint EPUB creation, normalization
inventory/plan/application writers, completed-pending cleanup, and
finalization/title/publication/editorial writers. The benchmark lock starts
before mutable benchmark inputs are loaded and spans provider execution plus the
final protected-state verification. A competing writer fails immediately.
Normalization application `--dry-run` and `--verify-only` remain lock-free
snapshots; normalization inventory/plan commands acquire the lock because they
publish reports. A crashed process releases ownership; the empty
`.production.lock` rendezvous file remaining on disk is normal and must never be
deleted to bypass an active writer. Other read-only planning, dry-run, and
verify-only modes remain snapshots and do not reserve the novel. The lock is
cooperative: direct internal calls, external editors, historical standalone
scripts, and create/migration staging do not participate.


## Recover an existing paid pending chapter

Inspect the exact paid pair and logs without altering them:

```powershell
Get-ChildItem "novels\$Novel\output\pending"
Get-Content "novels\$Novel\data\progress.json"
Get-ChildItem "novels\$Novel\logs" | Sort-Object LastWriteTime -Descending
& $Python operator_preflight.py --novel $Novel --provider direct_deepseek --model pro
```

1. **Plan, zero provider calls.** Use explicit future auxiliary limits:

```powershell
& $Python pending_recovery_plan.py --novel $Novel --provider direct_deepseek --model pro `
    --max-api-calls 6 --max-cost-usd 0.10
```

2. **Review and approve.** Confirm the exact pending target, requested and
   effective model, saved stages, future budget, state fingerprint and token.
   Saved `state_extracted` or `reviewed` stages must also carry the durable
   Phase 15 checkpoint identities expected by current source/text/profile policy.
   Missing or stale identity is a blocker: do not delete it, invent one, or
   authorize a replacement provider call just to continue.
   The token binds a specific plan; creating it does not authorize spending.
   Copy its full values into variables after review. The dollar amount here is
   a ceiling, not a promise that it is enough for every recovery.

```powershell
$Fingerprint = 'PASTE_STATE_FINGERPRINT_FROM_THE_REVIEWED_PLAN'
$Approval = 'PASTE_APPROVAL_TOKEN_FROM_THE_SAME_PLAN'
& $Python pending_recovery_execute.py --novel $Novel --provider direct_deepseek --model pro `
    --max-api-calls 6 --max-cost-usd 0.10 `
    --state-fingerprint $Fingerprint --approval-token $Approval
```

Without the authorization flag, this revalidates and reports
`AUTHORIZATION_REQUIRED` with zero provider calls and no execution.

3. **Execute, paid auxiliary operations permitted.** Only after approval:

```powershell
& $Python pending_recovery_execute.py --novel $Novel --provider direct_deepseek --model pro `
    --max-api-calls 6 --max-cost-usd 0.10 `
    --state-fingerprint $Fingerprint --approval-token $Approval `
    --authorize-paid-recovery
```

Use the same requested `--model` and ceilings as the plan, even when trustworthy
pending provenance overrides the effective model. Recovery cannot regenerate
the translation, run startup periodic audits, or move to another chapter. It
revalidates authorization while holding the novel lock. State changes, any
repository-root Python change, selected-profile policy change, or
`pyproject.toml` change require a new plan; never reuse stale tokens. The
runtime manifest is repository-owned scope only and does not certify arbitrary
host/package bytes. After success run validation and preflight again before
normal production.

## Common stops

| Stop | Safe next action |
| --- | --- |
| Residual source script or strict locked-term failure | Inspect the named QA evidence and pending prose. Preserve the paid pair; resolve only exact source-backed issues, then revalidate. |
| Candidate conflict | Review exact proposals/source. `resolve_glossary_candidate_conflict.py --help` describes the local selection-only sidecar command. Never select by candidate order. |
| `original_started` without a valid review response | Preserve `chapter_glossary_review_recovery` in pending metadata and its operation ID. The provider outcome/usage may be unknown. Automatic restart is intentionally blocked. Inspect logs/provider billing and obtain narrowly scoped recovery guidance; do not delete the record or blindly retry. |
| `started`/`uncertain` paid request journal or corrupt journal | Preserve `data/provider_request_journal/` exactly. The provider outcome may be unknown and automatic reissue is intentionally blocked. Record the operation ID, inspect logs/provider billing, and obtain a narrow recovery decision; do not delete or rewrite the journal to force another request. |
| Provider/request error | Treat as a transport/provider failure. Preserve pending and recovery evidence, resolve the reported provider/network/billing issue, then rerun the same guarded workflow. |
| Provider response/accounting failure | The request may have completed, but response structure or required usage accounting was not safe to accept. Preserve any paid-request journal/recovery evidence and do not assume another paid request is safe. |
| Local persistence/recovery error | This is not an API/network diagnosis. Preserve pending/journal/recovery files, repair local disk/permissions/checkpoint state, then rerun preflight or the same guarded route. |
| Saved/new glossary review checkpoint | Review recovery and final `stage=reviewed` persistence are restart-safe engine checkpoints. Do not manually rewrite `chapter_glossary_review_recovery`, `review_result`, or `review_checkpoint_identity` to bypass a blocker; fix the underlying source/policy/persistence issue and rerun the guarded route. |
| Missing/corrupt pending pair or wrong model/source identity | Stop and compare with backups. Do not create guessed metadata, clear pending, reset progress, or run a translation to repair it. |
| Final chapter file exists but progress did not advance | Preserve final, pending, QA, and associated state exactly. If final bytes equal pending and the saved pending checkpoint is already reviewed with committed QA matching the saved passing checkpoint (plus only canonical pinned-model provenance when applicable), the recovery planner may return `reconcile_reviewed_partial_commit_tail`. Review its fingerprint/token and use bounded recovery; it performs no translation/state/review provider work and finishes only the proven commit tail. Any byte mismatch, missing reviewed evidence, or divergent glossary/continuity/issues state remains BLOCKED. Never delete final or pending manually to force a route. |
| Filesystem/disk error | Restore capacity/permissions first, preserve pending, rerun preflight. Partial associated state writes require inspection even though progress stays behind. |
| Another writer owns the novel | Stop or finish that run. Do not remove `.production.lock`; OS ownership ends when the process exits. |

Pending metadata, saved stage usage, and paid-request journals are production
data. Valid saved translations and durable `response_recorded` envelopes are
reused. The active production client disables SDK retries, and ambiguous paid
outcomes are not automatically reissued. This closes known local restart and
hidden-retry windows without providing a global exactly-once billing guarantee.
Unknown provider usage is not guessed.

## Controlled trials

For a production-ready project with no pending translation, a bounded one-chapter
trial remains supported. Plan with the desired budget:

```powershell
& $Python controlled_trial_plan.py --novel $Novel --provider direct_deepseek --model pro `
    --max-api-calls 6 --max-cost-usd 0.10
```

Review that plan and set `$Fingerprint` and `$Approval` from it. Verify without
spending, then add `--authorize-paid-trial` only for the approved paid execution:

```powershell
& $Python controlled_trial_repo_env.py --novel $Novel --provider direct_deepseek --model pro `
    --max-api-calls 6 --max-cost-usd 0.10 `
    --state-fingerprint $Fingerprint --approval-token $Approval
```

The repository-env wrapper supports existing private credentials without
printing them. For explicitly peak-allowed trials, generate a new plan with
`--allow-peak` and execute via `controlled_trial_peak_allowed.py` with the same
arguments and optional paid authorization flag. Normal production scheduling
is unaffected. Trial startup/audit sequencing is retained and consumes the
same bounded request budget; a small ceiling can legitimately stop a trial.

The ordinary model `max_tokens` describes chapter translation, rather than the
complete production request policy. The trial budget proxy snapshots the exact
immutable `ProductionOperationPolicy` for the selected model and requires every
physical request to match one catalog operation's controls: an explicit integer
ceiling, exact thinking controls, and reasoning-effort presence/value. It does
not authorize arbitrary ceilings below a model-wide maximum. For explicit
`--model v41-flash`, 12000/disabled ordinary translation and 24000/disabled
recovery both match reviewed controls, with no `reasoning_effort` key. Arbitrary
16000, 24001, altered thinking, or unexpected reasoning controls fail before
transport. Pro has no translation recovery and cannot request 24000; retired
V4 Flash and unknown production models cannot make new trial requests.

A control match is only a budget gate. Production still proves saturation and
exact journal identity before its single fresh-full recovery. The trial's total
physical-call and dollar ceilings remain authoritative across translation,
recovery, startup, state, reviews, and audits. A recovery's conservative estimate
prices all 24000 potential output tokens plus the existing conservative input
bound; insufficient remaining budget or call capacity stops before transport.
Accepted usage from both responses is accounted at actual request-time rates.

Journaled requests authorize locally while their exact journal remains
`planned`: the provider-identity runtime checks the authoritative model route
and time-based cutoff first, then composes any controlled-trial budget scope.
Both scopes must succeed before `started`: exact controls, available physical-call
capacity, and the full conservative cost bound all finish before persistence.
A deterministic budget/control refusal is reported as
`deterministic_local_budget_refusal`; a route refusal retains the existing typed
provider-identity error. Both make zero physical sends and leave the planned
slot safely re-authorizable only if the relevant authorization later succeeds. A fresh
read-only plan with a larger approved envelope can replay a completed initial
LENGTH response and send the previously blocked recovery once; the initial
journal is never changed or resent.

The generic paid-journal protocol owns this optional pre-send scope and has no
controlled-trial dependency. The budget leases one immutable JSON request,
pricing snapshot, and conservative bound, preventing another call from consuming
capacity between authorization and transport. Only after durable `started` does
the proxy arm that single authorized transport and increment its physical-attempt
counter immediately before the SDK call. Failure to persist `started` releases
the transient lease without sending. Normal production also performs route
authorization before `started`, even without a controlled-trial budget. The raw
send retains its immediate route guard: a cutoff crossed after preauthorization
remains conservatively uncertain. Known-expired Pro and retired V4 Flash routes
leave `planned` with zero sends. Paid response replay skips both local
authorizers; provider response identity is still checked only after durable
response recording/replay. Historical journal schema/revision/states need no
migration; request, recovery, and pending identities are unchanged.

Transport, normalization, or response-persistence exceptions after `started`
still leave `uncertain` (or blocking `started` if uncertainty persistence fails).
Provider exceptions, missing usage, post-response cost overruns, and response
normalization failures never become definitely-unsent merely because an
exception class resembles a budget error. Recorded responses still replay with
zero new sends. Preserve existing ambiguous evidence; this change does not
reclassify old uncertain journals.

Planning stays deterministic, read-only, and non-authorizing. The existing
engine/runtime manifest fingerprints `production_operation_policy.py`, its model
and state helpers, and the executor along with all root Python engine files and
the selected profile. Changes to these bytes invalidate both state fingerprint
and approval token, so regenerate and review the plan after this update or a
later operation-policy change. No duplicate policy identity is needed in the
approval artifact.

## Durable auxiliary repair and adjudication requests

Residual-CJK and strict-terminology initial/correction requests, plus glossary
candidate conflict adjudication, use the same paid-journal transport as chapter
translation. Their reviewed operation controls remain 800 tokens, thinking
disabled, with no reasoning effort. Eligibility, source anchoring, patch scope,
mandatory QA and bounded correction rules are unchanged.

Each novel owns flat files under `data/provider_request_journal/`, named
`NNNN.<operation_kind>.<SHA256(domain_operation_id)>.json`. The suffix fixes the
logical domain attempt slot; a changed payload cannot silently create a new
replacement request. The immutable journal identity binds operation kind,
novel/chapter/provider/model/profile, full authoritative source SHA, pending
translation SHA, and exact provider payload SHA. Initial and correction kinds
are distinct. Ordinary chapter translation/recovery identities are unchanged.

The request adapter uses the existing route/budget authorization before durable
STARTED. A known local refusal leaves PLANNED and makes zero physical sends.
New domain attempt metadata is also initially planned with `api_called=false`;
errors reconcile it to durable transport evidence. A definitely-unsent planned
attempt can be resumed only through the exact journal under appropriate renewed
operator authorization. A possible transport/normalization/accounting failure
leaves UNCERTAIN, blocking reissue across restart. No hidden retry exists.

A successful provider response is recorded before provider-model checking and
before domain patch validation. Wrong-model evidence remains RESPONSE_RECORDED
and blocks automatic resend. Its accepted usage remains available for idempotent
accounting. Malformed patches and mandatory-QA rejection also preserve the paid
response, with their existing local rejection/provenance records. Domain cost
submission uses existing stable operation IDs, preventing duplicate local totals.
Uncertain usage is never fabricated.

A crash after the paid response but before domain persistence reconstructs the
same request and replays with zero sends and zero trial-capacity consumption.
New journal-aware domain attempts retain their logical attempt number; resuming
one is not an additional allowance. Existing recorded candidate revalidation,
content-hash provenance and legacy unjournaled failed/started attempt blockers
remain intact. Historical adjudication records require no migration; new
transport fields are optional, strictly validated and covered by its record hash.
Domain `api_called` on a recorded candidate denotes its historical paid origin.
This paid-origin flag is monotonic across failed replay as well: known paid
stats are retained when a blocked invocation supplies no new usage. Failure
results/logs include known paid initial and correction costs; uncertain costs
are never inferred. Transport evidence separately records whether the current journal call sent or
replayed. Uncertain evidence retains possible remote execution even on a
zero-send blocked restart.

Example Project A now opts into the existing residual policy: enabled, at most 12
CJK characters, 3 fragments, 3 affected lines, and 1 attempt per pending version.
Other Ace settings are unchanged. Frozen G10 configuration checks verify their
registered immutable Git blobs (including actual Git object identity and
line-ending-invariant JSON hashes), rather than mutable current novel configs.
Current CI retains Git history for this proof; no frozen constants are repinned. This repository change does not migrate or
recover the isolated Stage-A burn-in. Later authorized recovery must reuse its
already-paid pending chapter and cannot regenerate translation.

## Checkpoint EPUB and output validation

The checkpoint builder now acquires the same novel-local writer lock before it
reads progress/committed output and writes the EPUB. If another supported writer
owns the novel, the build refuses immediately. Validate progress and committed
output before building:

```powershell
& $Python validate_project.py --novel $Novel
& $Python build_checkpoint_epub.py --novel $Novel
```

Inputs are the novel configuration, progress, committed Markdown, and source
library when available for unit/title identity. The default range is 1 through
`last_completed`. Pending is excluded. Missing/duplicate numeric files,
uncommitted extras, inconsistent progress, and invalid ranges fail closed.
Output is `novels/<id>/output/epub/<id>_chapters_0001_NNNN.epub`; an existing
same-range checkpoint is replaced through a unique same-directory temporary
file that is file-fsynced before atomic replacement. Temp cleanup occurs only
before successful publication; once replacement succeeds, the checkpoint path
performs no further filesystem cleanup that could turn a committed EPUB into a
reported failure. This prevents deterministic temp-name collisions and
partial-temp publication. It does not claim parent-directory metadata durability
across sudden power loss. A subset is supported:

```powershell
& $Python build_checkpoint_epub.py --novel $Novel --from-chapter 1 --to-chapter 100
```

The requested end must be committed. Build uses zero provider calls. Inspect
the book's navigation, chapter headings, emphasis, Unicode, and sample prose
in your EPUB reader. A checkpoint build is not a formal EPUBCheck certification
or a bit-for-bit reproducibility guarantee, and has no `--verify-only` switch.

## Interrupted finalization-prefix recovery

The normalization, title-application, and publication-chapter writers all keep
their output corpus rename as the terminal visible commit boundary. If
`final/normalized_chapters`, `final/titled_chapters`, or
`final/publication_chapters` already exists, the corresponding apply command
still refuses to overwrite it.

When the terminal corpus is absent, a restart may clean an interrupted prefix
only after independent proof. Title application requires exact deterministic
JSON/Markdown bytes and independently verified stale titled stages. Publication
cleanup requires exact deterministic report bytes and exact planned bytes for
every stale staged chapter. Normalization treats only `generated_at_utc` as
non-authoritative: the JSON must be canonical, that value must be a valid UTC
timestamp, every other report field must match the current plan/baseline
reconstruction, Markdown must match exactly, and each stale normalized stage
must pass the independent normalization verifier.

Phase 35 also classifies an ordinary error reported by the terminal normalization
stage rename. If `final/normalized_chapters` is absent, the existing pre-commit
cleanup path removes any reports/staging files published by that failed attempt.
If the terminal corpus exists, reports are never deleted speculatively. The run
is accepted as committed success only when the original stage is gone, both
final normalization reports are regular files with exact reconstructed bytes,
their staging files are absent, independent verification of the installed corpus
exactly matches the pre-commit verification, and the immutable normalization
baseline still matches the fingerprint used to build the stage. Any
output-present state that fails that proof is preserved and reported as an
ambiguous terminal normalization outcome.

Phase 36 extends truthful terminal-outcome classification to title
application and publication chapters without sharing a generic proof helper.
For title application, a replace error after `final/titled_chapters` becomes
visible is accepted as committed success only when the original stage is gone,
both final title reports are exact reconstructed bytes, no report temp remains,
the normalized input fingerprint is unchanged, and independent verification of
the installed titled corpus exactly matches the pre-commit verification.
Otherwise the titled corpus/reports are preserved and the run stops as
ambiguous.

For publication chapters, a rename error after `final/publication_chapters`
becomes visible is accepted as committed success only when the stage is gone,
both final publication reports are exact reconstructed bytes, every installed
chapter exactly matches the original publication plan, and rebuilding the
current source plan reproduces both the original source and planned-output
fingerprints. Otherwise the publication corpus/reports are preserved and the run
stops as ambiguous. This matters operationally because publication chapters has
no verify-only mode: a proven committed rename must not be surfaced as a
stranded failed apply merely because the rename call reported an error.
All surviving candidates are validated before deletion begins. A malformed,
divergent, symlinked, unexpected, or otherwise unprovable candidate blocks the
restart and preserves the evidence. Report cleanup removes Markdown before JSON
so interrupted file cleanup remains classifiable. For stale stage directories,
Phase 26 persists a canonical proof-bound cleanup journal and renames the proven
stage to a unique tombstone before recursive deletion. Phase 28 writes and
file-fsyncs that journal first to an exclusive workflow-scoped pre-journal scratch
file, verifies its bytes, and atomically publishes it before any stage rename. If
a process dies before publication, the scratch file is non-authoritative and the
original stale stage remains; restart removes the scratch only after that stale
stage inventory re-passes the existing exact proof. Orphan/invalid/symlinked
scratch blocks rather than being guessed away. If tombstone deletion fails
partway, the published journal can authorize completion even though the tombstone
is no longer an exact corpus. Unknown tombstones, malformed journals, proof drift,
or a failed rename whose original stage no longer revalidates still block. This
does not add parent-directory fsync or power-loss crash atomicity.

Final-editorial application itself follows the same terminal-artifact rule:
its apply reports are installed and the publication source is revalidated before
`final/editorial_chapters` is renamed into place. The editorial corpus rename
is the visible commit boundary. If that terminal replace reports an ordinary
error, an absent output is treated as a pre-commit failure and the existing
cleanup path runs. An output-present state is never rolled back speculatively.
It is accepted as committed success only when the stage is gone, both final
reports exactly match reconstructed bytes, report temps are absent, the installed
editorial corpus matches the expected output fingerprint, and the publication
source still matches the pre-commit fingerprint. Otherwise the output/reports
are preserved and the run stops with an ambiguous terminal-outcome error.

On restart, report/report-temp/staging residue is auto-recovered only when the
approved plan and current immutable source evidence independently reconstruct
every surviving report byte and staged-corpus fingerprint exactly. Impossible
ordering or any mismatch blocks without cleanup. Proven cleanup occurs before a
fresh apply. Stale editorial stages use the same Phase 26 journal/tombstone
cleanup protocol, so an interrupted recursive delete remains classifiable
independently of the partial tombstone contents.

## Final editorial publication EPUB

This distinct workflow requires an already verified `final/editorial_chapters`
corpus and `final/reports/final_editorial_apply.json` whose fingerprint matches
it. It does not use pending or reconstruct missing content. Earlier finalization
and title/editorial plans require their own exact approvals; do not reuse the
historical Rogue-specific hashes from README for another novel.

```powershell
& $Python build_publication_epub.py --novel $Novel
& $Python build_publication_epub.py --novel $Novel --verify-only
```

Mutating final-publication builds acquire the same novel-local writer lock;
`--verify-only` remains read-only and lock-free. Output is
`novels/<id>/final/epub/<id>.epub` with reports in `final/reports/`.
Existing terminal EPUB output is refused, not silently replaced. The final
EPUB's unique temporary archive is file-fsynced before atomic replacement.
Reports are published and the final source identity is revalidated before the
EPUB is installed; the EPUB replacement is therefore the terminal visible commit
boundary and no source validation follows it. On restart, surviving report
prefixes are auto-recovered only when canonical report bytes bind to exactly one
surviving temporary EPUB that fully validates against the current editorial
corpus and reproduces the report exactly, including EPUB hash/size. Phase 33 also
classifies the final cleanup prefix in which both reports were already removed
but one temporary EPUB survived: with no terminal output and no reports, exactly
one regular temporary archive may be deleted only after it independently
validates against the current editorial corpus. Zero temp candidates is the
normal clean start; malformed, symlinked, invalid, or multiple temp-only
candidates fail closed and are preserved. This makes interruption of that final
single-temp unlink naturally retryable without treating scratch bytes as a
commit marker. Report-bound missing/multiple/divergent evidence still fails
closed and is not deleted. This remains a bounded multi-path transaction, not a
crash-consistent database transaction, and parent-directory fsync is not
claimed. Archive validation
checks corpus content, spine/TOC, metadata,
XHTML/resource references and reader artifacts; it does not claim external
EPUBCheck certification. The publication chain is still partly specialized to
Rogue and should not be assumed portable
to every novel without its required corpus and report validation.

## Engineering and rollback

Keep live production on `main` and develop in a separate worktree. Back up both
Git refs and all novel bytes including ignored/untracked output before changes.
Use only synthetic/copy data for tests, with no real provider key or `.env`.
Stage explicit reviewed code paths, not the production `novels/` tree.

Before applying a reviewed feature, stop production, make a fresh live backup,
check that changed paths do not overlap dirty novel state, and fast-forward
only. Do not reset or stash production to make an update fit. Keep the feature
branch, worktree and backups until post-merge validation succeeds. Code rollback
on published main should be a reviewed revert; restore novel data only after a
separate byte comparison and explicit narrow recovery decision.


## GOAT production provider

Use [Production provider backends](PRODUCTION_PROVIDER_BACKENDS.md) for the
explicit `command_code_provider` selection, read-only planning commands and
credit-equivalent ceilings. GOAT credentials activate only after bounded
trial/recovery authorization. Direct/unpinned paid pending translations remain
direct and cannot migrate to GOAT. Readiness does not authorize a live request;
the initial rollout stops at draft PR and independent review.

During initial GOAT rollout, public unattended translation fails before DPAPI
or network. Clean GOAT projects recommend the controlled-trial planner only
when an explicit credit-equivalent allowance is supplied; otherwise preflight
reports `BOUNDED_ALLOWANCE_REQUIRED` without inventing an allowance. Supply both
`--max-api-calls` and `--max-credit-equivalent-usd` for a complete bounded plan.
The direct DeepSeek operator route is unchanged.
