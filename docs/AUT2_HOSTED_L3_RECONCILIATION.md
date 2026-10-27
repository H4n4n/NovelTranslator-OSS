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
