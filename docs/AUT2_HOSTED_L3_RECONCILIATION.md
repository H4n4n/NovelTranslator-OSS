# PR 15 hosted L3 reconciliation

Status: investigation and local repair; independent Hermes review required.
No hosted L3 clearance, L4, paid execution, push, PR update or CI rerun authorized.

Baseline: b3447b6975c19ad70b15fe390c613e50107d75d4; tree df55f5808646479ed5d339d4709cf3587ebbd786.
Clean checkout verified on D1.1 before switching to codex/aut2-pr15-hosted-l3-reconciliation.
D1 d7a792c400ea069762bdb2ecbb902cee68ec29f3 and D1.1 c10f0b9aa13abc133c2b4fc7da560fa64506becf remain reachable on codex/aut2-d1-publication-freshness.
No private novel state accessed; synthetic checkpoints only.

## Failure-to-fix matrix (constructed before source edits)

| Cluster | Hosted evidence / root cause | Intended narrow repair | Required evidence / blockers |
| --- | --- | --- | --- |
| A: 70 auxiliary tests | Direct audit publication lacks caller-held OS novel lease. Production launcher owns it around runtime. | File-local fixture owns real production_run_lock for the complete direct test lifetime; leave shared engine and validators intact. | Preserve hydration, identity, replay, uncertainty and exactly-once accounting assertions; independent missing-lease refusal. |
| B: 33 history tests | Correcting audits start at 476 without source-owned prior intents, despite mutable last_audited_chapter=475. HISTORICAL_OWNERSHIP_LINEAGE_GAP is valid refusal. | Build a genuine sequential zero-send prefix using audit checkpoint/finalization owners before the correcting window; retain original disposition/journal bytes. Target damage at the correcting event, not the new prefix. | Positive chronology must independently reprove. Legacy gap, disconnected reverts and unowned transactions must refuse without fabricating witnesses or rehashing a positive plan. |
| C1: two batch tests | Shared historical audit fixture writes {synthetic_zero_send:true}; batch expects checkpoint audit_window. | Replace incomplete synthetic prefix checkpoint with the real local zero-candidate checkpoint owner. No invented provider record or production fallback. | Both compatibility variants and malformed checkpoint rejection; read-only planning. |
| C2: experimental audit | Chapter commits, malformed audit JSON is repaired, then run stops. Track lease is already held; exact downstream exception still under investigation. | Determine stop cause before deciding fixture versus runtime compatibility correction. | Keep genuine request journal and replay assertions. |
| C3: G10 rehearsal | Baseline snapshot/recovery owners require real caller-held novel lease. | Hold original lease for snapshot and shadow lease for recovery at the appropriate harness boundary. | Shadow-original isolation, authorization, missing-lease negative. |
| D: shard 0 | Retained result: timeout after 4500.153 s. 80 audit-finalization cases started; 17 call failures before timeout. | Diagnosis only: aggregate setup/call timings and trace repeated proof work; propose source-bound optimization/repartition. | No CI schedule, timeout, coverage or cache changes. Additional failures remain hosted blockers. |

Hosted run: https://github.com/H4n4n/NovelTranslator-OSS/actions/runs/40395198100
Retained artifacts: public-ci-40395198100-1-{0,1,2}, public-decision-40395198100-1.
Shard 1: 33 history, 2 batch and 1 G10 call failures. Shard 2: 70 auxiliary and 1 experimental call failures.

## Repairs and next-stage findings

A uses a file-local real OS lease, leaving the shared engine fixture unlocked
for executors that acquire their own lease. Once that missing context was fixed,
three representative hydration cases exposed WRITER_AUTHORIZATION_DRIFT: the
synthetic rewind discarded the outer publication but retained its authorization
bound to the old checkpoint. The rewind now first proves the exact FINALIZED,
zero-edit plan and deletes only that disposable publication's exact derived
plan/authorization/state/result/reconciliation files. Paid request journals and
unrelated receipts remain byte-exact. No production cleanup logic changed.

B creates an actual empty-candidate checkpoint and a 1–475 zero-send finalization
through the checkpoint and finalization owners before acquiring the correction.
It does not rehash an unowned positive plan or create standalone witnesses.
Subsequent advancement preserves existing English files and the proven audit
marker. Damage cases select the exact correcting event instead of accidentally
targeting the new prefix. Disconnected prose and unowned named transactions are
now checked at the writer's earlier refusal boundary, with valid source,
inventory and real lease, before any write. An independent unwitnessed-marker
control retains HISTORICAL_OWNERSHIP_LINEAGE_GAP and zero-write refusal.

C1 replaces the shared fixture's incomplete prefix checkpoint with the genuine
zero-candidate checkpoint format. This exposed a second consumer defect:
goat_batch_plan.py expected transaction_id in every transaction JSON, although
writer authorization receipts deliberately carry plan_sha256. The consumer now
requires the exact owner revision/schema, canonical request-slot filename,
complete proven-plan hash, well-formed digest fields and canonical receipt hash.
The existing preflight/historical proof remains mandatory. The test additionally
rehashes a foreign plan hash and requires read-only refusal. No transaction or
provider producer schema was changed and no provider evidence was invented.

C3 supplies complete disposable novel config, source library and committed
inventory, holds the original lease for baseline snapshot, and holds only the
shadow lease for authorized recovery. Missing and foreign lease controls require
zero-write refusal. No real recovery authorization was bypassed.

## Unresolved experimental ownership boundary (C2)

The unchanged due-audit test still fails. The exact caught stop is
`GoatRuntimeError: GOAT legacy request logical model drift`, after the chapter
commit and all five synthetic sends (translation, state, review, audit, repair).
The caller already owns the correct track lease. Stack:

- experimental_goat_experiment.run_chronological -> engine.run_due_audits;
- production_periodic_audit_finalization.finalize_periodic_audit;
- retroactive_transactions.execute_retroactive_transaction;
- historical_reconciliation.writer_authorization (current accepted-audit reproof);
- goat_periodic_audit_finalize_plan.checkpoint_proof;
- production_goat_runtime.normalize_production_request.

Experimental accounting uses provider=command_code_provider with its own model
and pricing/plan identity. The writer's native GOAT proof dispatch therefore
selects the DeepSeek-only proof owner for the experimental Qwen request. The
safety refusal is correct for incompatible proof; expected experimental audit
completion is a real compatibility gap. Merely excluding experimental runtimes
from accepted paid-evidence reproof would weaken the ownership safeguard.
A separately reviewed, source-qualified experimental accepted-response proof
owner/dispatch contract is required before claiming completion. This assignment
leaves that boundary unchanged and stops for independent Hermes review. The
original committed/journal/replay assertions are retained; no xfail or skip.

## Shard 0 evidence and concrete follow-up plan (diagnosis only)

The retained shard-0 result is timeout, exit 124, elapsed 4500.153 seconds,
source_unchanged=true. Its event stream collected 97 finalization cases, started
80, and completed 79. Completed phases consumed 2075.513 s in setup (mean
26.272, max 34.942), 2403.934 s in calls (mean 30.430, max 80.218) and 0.031 s
in teardown. Thus 4479.478 s of the budget is directly attributed to this one
module before the incomplete 80th case.

Five test_shared_finalizer_reconciles_interrupted_new_pr155_transaction cases
consumed 104.591–105.461 s each, including about 34 s setup and 70–71 s calls.
The nine fresh-baseline crash cases included five at 97.474–98.960 s and several
at 82–83 s. This is repeated full-corpus proof cost, not provider waiting.

A guarded local cProfile of just
test_live_shaped_local_recovery_and_already_finalized passed (137.99 s under
profiling and concurrent local focused validation; this is not a hosted speed
comparison). It recorded 3 finalizer calls, 15 writer_authorization calls,
15 _historical_reservations, 47 prepare_finalization, 90 transaction_proof,
125 build_plan, 71 chapter_manifest and 54840 file_sha calls. Overlapping
cumulative times: writer_authorization 74.492 s, file_sha 55.627 s,
transaction_proof 46.267 s, chapter_manifest 29.155 s. They must not be summed.
The measured fixture SHA-256 was
7017f733dec0568cdd2957bdd7bd6a6cd869a1fe65aeaf38b812f277d58171aa,
before the final source-selector assertion/outer prefix lease tightening.
The production proof owners are unchanged. prepare_finalization callers were:
finalizer 5, local planner 5, fixture 1, _historical_reservations 8,
_prove_audit 16, writer_authorization 12.
The original 1–375 source-owned prefix is repeatedly reconstructed as later
publication, transaction, preflight and replay boundaries reprove authorization.

Necessary checks: real lease/descriptor, canonical paths and complete inventory,
current chapter and associated-state prefixes, pending/journal identity and
uncertainty, original source/profile, accepted response/accounting, ownership
reservations, immutable plan/BEFORE/AFTER spans, and post-write reconciliation.
These remain fresh at every mutation and recovery boundary.

Concrete optimization proposal for a separate reviewed change: introduce an
explicit read-only proof input bundle for ONE no-mutation proof traversal. Read
and hash every canonical file once for that traversal; retain immutable bytes
and their parsed JSON, decoded-text bridge, deterministic source/span plan and
transformed AFTER bytes keyed by complete byte digest plus path/domain,
novel root/ID, source/profile/policy identities and proof revision. Pass the bundle through the
repeated prepare/transaction/prior-intent proof calls. Do not cache a positive
ownership decision, missing-file result, mutable progress, live request status,
or lease validity. Discard the bundle at any write, recovery boundary or lease
exit, and reread/reprove the complete dependency inventory before the next
mutation. Require drift, forged-plan, uncertain-request, interruption and alias
controls against the same corpus. Measure file-read/parse counts and hosted
phase times before choosing a broader scope; no speedup is claimed here.

An alternative future source-bound repartition can move complete finalization
function families (including every damage/crash parameter) into separate modules,
then regenerate the reviewed mapping and exact collected-node evidence against
the new source identity. Preserve all 97 cases and every assertion. This would
require explicit CI/schedule review and is not implemented here.

The event stream also reports 17 failed calls, all
`test_recovery_drift_fails_closed`, before the timeout. Source inspection finds
an asymmetric snapshot assertion: before includes the novel .production.lock
created by prefix finalization, while after removes it. No failure traceback
was emitted before the timed-out pytest process could print its summary, so
this is a source-based diagnosis, not a claimed validation of all 17 cases.
They remain separate hosted blockers. No frozen schedule, timeout, CI policy,
coverage, production cache or parallel translation worker was changed.

## Source and artifact identity

The hosted decision binds base e5aca8b35c4de0129dc83a0e551aefba012e4c2c,
PR head b3447b6975c19ad70b15fe390c613e50107d75d4 and hosted merge commit
8429f53793da28f1d632d7642c880f3e613a2958 to the same tested/index tree
df55f5808646479ed5d339d4709cf3587ebbd786. Policy SHA-256:
260f33a4ac7fa49757a0557a1e402b64493988c7ca66f712ea34bc090cd2ac7e.
The supplied main commit was also verified as an ancestor of the exact PR base.
The historical proof owners, request journal, lock implementation, pending
recovery and experimental source are unchanged from that base.

Retained event stream SHA-256 digests:

| Shard | Artifact ID | events.jsonl SHA-256 |
| --- | --- | --- |
| 0 | 12690613894 | 13582e63cb27c6d9d7d334b74cde79ed5fa6ae21228a34508c3ddd3ea7a64cf0 |
| 1 | 12685544863 | 754074d05606cf75908545f4de8f9bb93a7f2abd75867b787337a8f453326550 |
| 2 | 12689176276 | c3c59dab082ef4efe71ed29cf3934e2863f56e0398024cb31dc60ca1275385cd |

## Reproduction and review boundary

Every local test command uses Windows C:/Python314/python.exe 3.14.4, the
repository zero-provider guard, a fresh external TEMP child and explicit real
DPAPI deselection. The guard clears credentials/provider environment controls
and denies network, real DPAPI and private production-root access. No unguarded
pytest, private novel access, provider execution or whole-suite run was used.

Reproduce the focused selection from this checkout (the unchanged experimental
case is expected to remain a reported failure until a separately reviewed fix):

```powershell
$taskTemp = Join-Path $env:TEMP ('aut2-pr15-review-' + [guid]::NewGuid().ToString('N'))
C:/Python314/python.exe tests/zero_provider_validation.py --basetemp $taskTemp --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q tests/test_auxiliary_response_reconciliation.py tests/test_completed_uncertain_history.py tests/test_goat_batch_plan.py tests/test_benchmark_longitudinal_g10_recovery_rehearsal.py tests/test_experimental_goat.py::test_L_natural_due_audit_and_JSON_repair_are_journaled --tb=short
```

Next action is independent Hermes review of this local commit and this report,
especially synthetic zero-edit rewind ownership, earlier negative refusal
boundaries and the strict receipt consumer. No push, PR update, merge, hosted CI
rerun, D1/D1.1 rewrite, D2 regeneration, paid run or L4 was performed or authorized.

## Final local failure-to-fix evidence

| Cluster | Changed source / fixture | Corrected evidence | Remaining blocker |
| --- | --- | --- | --- |
| A | tests/test_auxiliary_response_reconciliation.py | All 72 cases pass, including real missing-lease refusal; original hydration/journal/replay/uncertainty/accounting assertions retained. | None in this focused module. |
| B | tests/test_completed_uncertain_history.py | All 73 cases pass, including all 33 formerly failing cases, genuine positive chronology, exact damage targeting, nine crash tails and the new unwitnessed-marker refusal. | None in this focused module; no ownership proof owner changed. |
| C1 | goat_batch_plan.py; tests/test_goat_batch_plan.py; shared prefix in tests/test_goat_periodic_audit_finalization.py | All 96 batch cases pass. Both affected variants pass again after final source-selector/lease tightening; rehashed foreign-plan receipt remains blocked. | No new provider or owner contract. |
| C2 | No source or test assertion change | Original case still fails; exact native/experimental proof-owner mismatch diagnosed above. | Source-qualified experimental acceptance owner/dispatch needs separate review. |
| C3 | tests/test_benchmark_longitudinal_g10_recovery_rehearsal.py | All 9 cases pass, including complete source/inventory, original-shadow isolation and new missing/foreign-lease refusal. | None in this focused module. |
| D | Report and diagnostic profile only | Hosted phase timings, repeat counts and concrete future plan recorded above. Representative finalization case passes under cProfile. | Timeout and 17 additional hosted functional failures remain; no L3 clearance. |

Final focused runs: auxiliary + G10 81 passed in 46.73 s; history 73 passed in
2751.51 s; batch 96 passed in 235.55 s. These are 250 unique passing focused
cases, repairing 106 of the 107 reproduced Cluster A–C failures. Final tightened
prefix recheck: 2 passed in 230.22 s. Experimental confirmation: 1 failed in
4.65 s with its real assertions intact. No full 5700-test suite was run.
The 45m51s local history run also demonstrates the cost now reached by valid
positive fixtures; it is not a calibrated hosted timing prediction.

External JUnit/log/profile evidence is retained under
C:/Users/User/AppData/Local/Temp as aut2-pr15-A-G10, aut2-pr15-B,
aut2-pr15-C1, aut2-pr15-C1-prefix, aut2-pr15-C2 and aut2-pr15-finalization.prof.
The JSON summary aut2-pr15-final-validation.json binds each JUnit file's digest.
Source syntax and git diff --check pass. The staged publication index guard
passed with 496 public files; final commit/tree/history identities and final
post-commit guard results accompany delivery.

Review next, without running any production command:

```powershell
git -C D:/NTOSS_AUT2_C_Lineage_Codex show --stat HEAD
```


## Shard-0 performance qualification — 2026-10-27

This is the latest local performance evidence on accepted HEAD
`a4f66eebbc34e6d5ba7d591b7485d82a5d172c19`, tree
`6727d11f5de3d4ebb15af0bf970d95d248b3f104`. The earlier investigation above
is retained as historical evidence; the accepted functional fixes are documented
in AUT2_L3_FUNCTIONAL_CLOSURE.md. The 17 drift assertions are unchanged here.

**All 97 original finalization cases passed in 3725.97 seconds**, including
profiling of four cases. There were zero failures, errors, skips, xfails or
omissions in that module. Its complete ordered node-hash sequence matches hosted
run 30 exactly. This is 774.03 seconds inside the 4500-second local budget.

The conservative hosted projection is **3875.34 seconds with 25% variability
allowance**, leaving 624.66 seconds against 4500. Estimating out measured profiler
overhead gives 3816.24 seconds with the same allowance. The **sub-3600 aim is not
established**. No claim of complete hosted-shard or L3 readiness is made; other
shard work and host variability remain relevant. A concrete exhaustive fallback
is specified below without implementing a CI or test-layout change.

### Identity and bounded scope

The initial checkout was clean in D:/NTOSS_AUT2_C_Lineage_Codex on the existing
codex/aut2-pr15-hosted-l3-reconciliation branch. D1 d7a792c400ea069762bdb2ecbb902cee68ec29f3
and D1.1 c10f0b9aa13abc133c2b4fc7da560fa64506becf remain on their existing branch.
Published PR 15 remains b3447b6975c19ad70b15fe390c613e50107d75d4.
No branch switch, new worktree, clone, push, PR update, hosted CI rerun, merge,
private novel access, paid execution, D1/D1.1 change, D2 regeneration or L4 was done.
The accepted a4f66ee commit remains the backup point. Final commit/tree/parent
identities accompany the handoff.

Changed files:

- historical_reconciliation.py: novel-qualified read-only input scopes, actual-byte
  revalidation and pure-plan memoization; explicit sharing through historical
  reservations, writer proof and final consistency; independent writer inventory
  and actual lease checks remain fresh at entry and exit.
- production_periodic_audit_finalization.py: preparation/transaction/manifest helpers
  accept explicit inputs; read_json retains full path validation while removing
  its discarded digest read.
- production_historical_lineage.py: compose a read-only lineage traversal over the
  same captured bytes; hash the actual blob consumed rather than reading it twice.
  Every original chronology, source, paid-response, accounting and publication
  comparison remains. No paid-journal or provider-replay schema/contract changed.
- goat_periodic_audit_finalize_plan.py: candidate and transaction reconstruction
  share one no-write traversal; execution and authorization receive no bundle.
- tests/test_retroactive_transactions.py: append 21 adversarial cases. The existing
  test source remains an exact canonical prefix. The 97-case target is byte-unchanged.
- docs/AUT2_HOSTED_L3_RECONCILIATION.md: this evidence and independent-review handoff.

CI classification, frozen shard schedule, timeout, runner, guards, deselection
policy and publication inventory are unchanged. No new test module is introduced.

### Why the optimization preserves currentness

Each bundle belongs to one novel root/configuration, complete indexed source
inventory, in-memory immutable profile context and canonical path map. Actual
library bytes must agree with the supplied source map before consumers run.
Captured entries contain immutable bytes and the SHA-256 computed from those same
bytes. JSON parsing and all independent proof comparisons still execute.

The bundle stores no positive payment, publication, recovery or writer verdict.
Pure plan memoization uses complete request values, chapter numbers, exact BEFORE
bytes, source text, planner function identity and schema/policy revisions. Returned
plans are detached copies, so a caller cannot poison the memo by mutating a result.

Before a successful read-only traversal returns, every captured file is reopened,
its actual bytes compared and digest recomputed. Directory membership and path-link
observations are checked afresh. Size and mtime are never acceptance keys. Missing
file results are not memoized. Every scope closes and clears its byte/plan caches.
Writer authorization rejects a supplied bundle and always starts fresh; its real
novel lease/descriptor and complete source/disk inventory are independently checked
again before returning. All existing mutation, publication and crash-recovery
boundaries retain their own proofs. No bundle survives a write, executor handoff,
lease exit or restart, and none is serialized into a receipt or journal.

Narrow read-only publication contexts intentionally omit some convenience paths.
Those are derived from the canonical novel root without adding attributes to the
caller or granting a writer context. Actual production writer validation remains
mandatory. This compatibility case was found by the broad integration run and
fixed before final frozen-source qualification.

### Paired measurements

Windows C:/Python314/python.exe 3.14.4; zero-provider guard; fresh external TEMP;
mandatory real-DPAPI deselection in every test command. The same four node IDs
were measured before and after. No full 97-case baseline was run. For the normal
baseline comparison only, accepted source bytes were temporarily restored in the
same checkout and then the candidate was restored byte-for-byte; HEAD and branch
never changed. Final-source profiles were captured only for these four cases
within the complete 97-case qualification. Other cases ran without cProfile.

| Case | Setup, profiled before/after (s) | Call, profiled before/after (s) | Unprofiled before/after (s) |
| --- | ---: | ---: | ---: |
| Success/replay | 61.82 / 33.90 | 60.24 / 36.72 | 92.92 / 51.73 |
| Source-owned historical correction [mixed] | 33.47 / 19.07 | 81.85 / 45.12 | 86.86 / 47.49 |
| Crash/restart [glossary] | 61.20 / 30.72 | 31.66 / 23.79 | 70.08 / 38.99 |
| Tampered historical backup [backup] | 61.90 / 30.83 | 4.93 / 2.71 | 50.54 / 25.16 |

The unprofiled selections passed in **300.63 s before / 163.59 s after** (45.6%
reduction). Profiled setup/call totals were **397.06 / 222.87 s** (43.9% reduction).
Fixture setup is distinct from the production call phases in the table.

| Case | Byte reads before/after | Bytes before/after |
| --- | ---: | ---: |
| Success/replay | 102,634 / 43,970 | 758,146,647 / 420,137,391 |
| Source-owned historical correction [mixed] | 96,017 / 40,649 | 754,754,656 / 383,028,574 |
| Crash/restart [glossary] | 76,718 / 30,921 | 407,312,479 / 211,736,426 |
| Tampered historical backup [backup] | 59,351 / 20,808 | 163,533,857 / 72,302,217 |

Aggregate byte reads: **334720 -> 136348** (59.3% fewer). Bytes traversed:
**2083747639 -> 1087204608** (47.8% fewer). Actual byte-read time under profiling:
128.57 -> 53.17 s. These include all Path.read_bytes calls in measured setup/call
phases, not only committed chapter reads.

| Counter across the four profiled cases | Before | After |
| --- | ---: | ---: |
| prepare_finalization | 139 | 139 |
| transaction_proof | 264 | 264 |
| writer_authorization | 57 | 57 |
| _historical_reservations | 57 | 57 |
| chapter_manifest | 236 | 236 |
| Raw tx.build_plan computations | 369 | 187 |
| file_sha calls (including absence checks) | 171144 | 9486 |
| SHA-256 constructor calls, all domains | 1338270 | 1193726 |

Raw plan-building cumulative time fell **46.70 -> 23.30 s**. SHA-256 constructor
exclusive time was only **1.624 -> 1.348 s**: filesystem opens, repeated path checks
and repeated reconstruction were the real costs, not hash arithmetic. Cumulative
helper timings overlap and must not be added together. The unchanged validator
counts show that duplicate inputs/computations disappeared, not the proof traversals.

Remaining costs include fresh source/inventory/lease checks at independent writer
boundaries, actual end-of-traversal rereads, strict JSON/canonical comparisons,
source/span ownership computation and paid-evidence reproof. None is bypassed.

### Final regressions and projection

| Final selection | Result |
| --- | --- |
| Original finalization module, all parameters | 97 passed, 0 failed/errors/skips, 3725.97 s |
| Publication, writer/lock, standalone, experimental dispatch and 21 new adversarial controls | 473 passed, 0 failed/errors, 819.73 s |
| Completed-disposition restart, CRLF/BOM bridge, source/fork/accounting damage and intent/chapter/progress crash tails | 9 passed, 455.765 s |

The broad integration also encountered two existing private real-corpus opt-in
checks. They remain skipped because private novel access is explicitly prohibited;
they are outside the 97-case target and outside the public passing claim. No test
was changed to skip/xfail, no 97-case parameter was deselected, and no assertion was
weakened. The real-DPAPI deselection is the mandatory existing guard requirement.
The accepted 73-case history module and earlier 250-case reconciliation were not
rerun wholesale; the nine changed-consumer controls are focused regressions.

New adversarial controls first prove valid evidence, then exercise real same-size,
restored-mtime changes to chapter, library, glossary, history, continuity, BEFORE,
plan, result and intent bytes inside writer proof. They require refusal and exact
preservation of all other files. Additional controls cover competing intent creation,
changed bytes at separate authorization boundaries, stale source before capture,
in-memory source drift, fresh symlink/junction checks at exit, cross-novel/closed
scope refusal and independent writer-boundary refusal. Existing publication and
writer-lock contracts, native/experimental accepted proof dispatch and genuine
standalone authorization receipts all retain their original assertions.

The complete local run consumed setup **1753.38 s**, calls **1971.99 s**, teardown
**0.023 s**. A final unprofiled four-case rerun estimates profiling overhead at
29.98 setup + 29.53 call seconds. Thus a normal equivalent of about **3665.89 s**
is an estimate, not a second complete-module measurement.

Hosted run-30 telemetry was reread from retained shard 0 and verified against
SHA-256 13582e63cb27c6d9d7d334b74cde79ed5fa6ae21228a34508c3ddd3ea7a64cf0.
All four exact hosted node hashes match. To avoid using the unusually short refusal
call as a multiplicative whole-module factor, the conservative calibration uses
an upper envelope that covers all four measured baseline pairs:

- setup: hosted <= 0.72887455 * local;
- call: hosted <= 0.86125327 * local + 1.27700535 seconds per case;
- then add **25% Windows variability allowance** to the whole estimate.

Applied to all 97 measured phases, including cProfile overhead, this yields
**3100.27 * 1.25 = 3875.34 s**. Estimating out profiling gives **3816.24 s**.
The 30% sensitivity is 4030.36 s without profiler subtraction. A separate
phase-weighted projection using paired final unprofiled ratios gives 3639.12 s
with 20% and 3790.75 s with 25% allowance. All are projections, not hosted clearance.
The conservative sub-3600 target is not met; complete-shard readiness is unproven.

### Exhaustive fallback — proposal only

Retain complete function families and every parameter/assertion, with the same
function-scoped source-owned fixture. A measured LPT partition gives:

| Proposal-only module | Cases | Complete function families | Measured seconds |
| --- | ---: | ---: | ---: |
| `tests/test_goat_periodic_audit_finalization_part_1.py` | 26 | 5 | 919.87 |
| `tests/test_goat_periodic_audit_finalization_part_2.py` | 20 | 5 | 935.88 |
| `tests/test_goat_periodic_audit_finalization_part_3.py` | 31 | 6 | 932.64 |
| `tests/test_goat_periodic_audit_finalization_part_4.py` | 20 | 6 | 937.01 |

The external repartition-proposal.json contains every original node, original
function-source hashes and an exact one-to-one bucket assignment; 97 are assigned
once, with no omissions. The source SHA and ordered node-list digest bind this
proposal to this exact target module. Before implementation, separately review
fixture-helper extraction, new module classification, the full old/new node
bijection and frozen shard mapping against the new source. Require guarded exact
collection and exhaustive execution. No CI schedule, timeout, runner or deselection
change, D2 regeneration or test move is implemented by this commit.

### Evidence, limitations and review boundary

Artifacts are local under C:/Users/User/AppData/Local/Temp/aut2-shard0-perf-20261027.
The scripts, JUnit, phase telemetry, profiles, runtime manifest, hosted calibration
and exhaustive fallback are retained there. final-evidence.json binds the runtime
Python bytes and test identities. Root Python and target/regression test bytes
remained frozen during final qualification.

| External TEMP artifact | SHA-256 |
| --- | --- |
| `all97-final.xml` | `7318f5f1c7e63c5f25630764a0649f4b3ccc7de64b17a528e5a3bd6b03f09702` |
| `integration-final.xml` | `f3e9b211cd2ef38b4e748656927622c93e7d3c73852ff21146af745bd75e8e69` |
| `history-focused.xml` | `760e553b8ca9af8c1b501130d3951dcb98deae965da68a78b38cde825bbfb83b` |
| `baseline-wall.xml` | `2657ed3e44c098b7db1734cf99a92e1df644bbee17759ed1cbb4ec55066ae064` |
| `qualified-wall.xml` | `e7ef1052fe00f30513a2e416e66df207174193cef38472f959321bbf1475adf4` |
| `baseline.jsonl` | `881fccf19eda93b9f9e19adf69eb97475e88e110e85e67c63ce2afcd39adb3e4` |
| `qualified-profile.jsonl` | `c581bd6eec0c308e0debfdeb117cc5a09afa8d481b5110eab5633e6ca08ca140` |
| `qualified-runtime.json` | `1fc91a9ca7fcf4505fda28e0705c44cb969cecb861b6b9b3e19409e5f39f8b10` |
| `host-calibration.json` | `3e13d127022da0c4da6f3e93039d6e268c36f5f58555c9d16358d7d70a3fe9b3` |
| `repartition-proposal.json` | `738c28b4ed7bb344067d159bee92a3bee3f593e7d776b6010da6a3d67e4d85cf` |
| `final-evidence.json` | `99019b93a33d8ec337704092c2bf4e3af19a1d125c38299267edf229634039b5` |

Per-traversal memory scales with captured evidence and distinct pure plan inputs;
there is no cross-operation retention. Filesystem currentness retains the existing
cooperating single-writer lease assumption, not an atomic snapshot guarantee against
a hostile process ignoring the lock. Original source/profile/policy and lower paid
proof owners retain their independent obligations. No paid/provider replay or
GOAT authorization schema is changed.

Reproduce the 97-case functional run (without the optional profiler):

```powershell
$taskTemp = Join-Path $env:TEMP ('aut2-shard0-review-' + [guid]::NewGuid().ToString('N'))
C:/Python314/python.exe tests/zero_provider_validation.py --basetemp $taskTemp --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q tests/test_goat_periodic_audit_finalization.py --durations=20 --tb=short
```

Stop for independent Hermes review of the bounded local commit. Review next:

```powershell
git -C D:/NTOSS_AUT2_C_Lineage_Codex show --stat HEAD
```
