# AUT-2 PR 15 functional L3 closure

Local engineering only; independent Hermes review required. No hosted L3/L4
clearance, paid execution, provider traffic, push, PR update, merge or CI rerun.

## Verified starting point

Workspace: D:/NTOSS_AUT2_C_Lineage_Codex, existing branch
codex/aut2-pr15-hosted-l3-reconciliation; no new worktree.
Clean accepted baseline: 915b6aeb2c458d27371b4441bbc2119251d5648b,
tree 6dc69fa980de45b6ba497d0f9df3164dd9dfb474,
parent b3447b6975c19ad70b15fe390c613e50107d75d4.
The accepted baseline is the backup point for this extension.
D1 d7a792c400ea069762bdb2ecbb902cee68ec29f3 remains reachable and D1.1
c10f0b9aa13abc133c2b4fc7da560fa64506becf remains the unchanged tip of
codex/aut2-d1-publication-freshness.

Read AGENTS.md, the accepted AUT2_HOSTED_L3_RECONCILIATION.md and the independent
Hermes review from external TEMP (aut2_pr15_hermes_review.md).
Hermes review SHA-256: 72a6b9cb3c6eaae5fea0f7158c53fa0c95a7f5a2cc53e45be98e8385baecfb95.
The prior 106 repaired cases are preserved; the expensive completed-uncertain
history module was neither changed nor rerun.

## A: accepted experimental paid-audit proof

The shared checkpoint_proof retains the original native DeepSeek GOAT transport
proof. For installed experimental accounting it selects ExperimentalAuditProof,
which independently reconstructs the original request, accepted response and
settlement. Both initial publication and recovery now require that proof; the
two former experimental exclusions at those gates are removed.

The experimental journal protocol stores experiment_plan_sha256 and
model_policy_sha256, not a native GOAT provider_policy envelope. The complete
original ModelPolicy is retained in immutable plan.json. The proof verifies that
recorded plan, model, provider, rates, efforts, ceilings, code/profile/input
inventory, installed runtime, accounting identity, novel/root and budget identity
all agree. It never invents a journal provider_policy or converts a native GOAT
journal into experimental evidence. Old plans with source/input/policy drift
remain blocked; this adds no approval migration.

| Evidence | Independent verification |
| --- | --- |
| Request and policy | Rebuild original operation-specific wire with the recorded ModelPolicy; reconstruct request/operation identities and the exact deterministic original journal path; verify original plan/model hashes. |
| Candidates/chunks | Existing shared proof recomputes source-backed candidate hash, source keys, families, short-ID transport, chunk topology and deterministic leaf result. Experimental proof additionally checks exact metadata schema, dynamic leaf positions, serialized request order and domain-ID uniqueness. |
| Paid response | load_matching_journal rechecks identity, record/raw-response hashes and RESPONSE_RECORDED. Native provider-policy/domain envelope fields are refused in the experimental protocol. |
| Settlement | Independently rebuild prompt reservation and reserved CE from the exact wire and model; require the original accepted budget record/path/track/chapter/request hash, raw-response digest and actual CE. Read-only ledger load, no reserve/settle/send call. |
| Envelope/accounting | Re-run accept_response over original SDK raw evidence and recorded policy. Reconstruct normalized envelope from durable checkpoint content/finish/stats plus original response identity, compare canonical hashes, and independently recompute ordered token/Decimal CE totals. Installed accounting must agree. |
| Repair/correction | Prove original malformed STOP parent for JSON repair; rederive retryable semantic errors, authorized fields, source/family context, merged result, parsed/effective hashes and exact operation IDs using the shared validator. LENGTH splitting retains original responses and never invokes JSON repair. |
| Intent/replay | Existing finalizer reconstructs detached baseline, deterministic audit result/statistics, transaction plan and immutable intent; rereads lower evidence on restart. The proof snapshots evidence digests and refuses drift during a proof traversal. |

There is no runtime.request/client/transport, budget settlement or credential
activation in this proof owner. Native DeepSeek GOAT still uses its original
normalizer, provider identity, journal binding and accepted_envelope path.
Explicit direct-Pro policy and generic direct accounting are unchanged.

Focused controls use actual synthetic paid request journaling and accounting,
then poison transport. They cover plain, JSON repair, semantic correction and
length-triggered two-child splitting; model/provider/runtime mismatch, recorded
policy disagreement, malformed/missing journals/responses, STARTED/UNCERTAIN,
wrong operation ID, self-rehashed raw response, parsed/accounting/metadata damage,
duplicate/reordered operations, settlement and plan-policy damage. Each
adversarial case first proves the undamaged evidence, then checks no writes.
Genuine accepted native and experimental journals are substituted in both
directions; each is valid under its own owner and refused by the other.

Four durable crash boundaries (intent, plan, audit record, progress) are recovered
with a fresh runtime, then replayed again, with original budget and every paid
journal byte preserved and no duplicate sends. Separate post-intent controls
refuse a changed paid journal or model on fresh-runtime recovery.

Snapshot helpers exclude only the exact caller-held Windows novel rendezvous,
whose byte-range lock prevents independent reads. No transaction, backup, paid
journal, chapter, glossary, audit state or budget evidence is excluded. The new
native cross-provider fixture uses a non-writing synthetic logger; proof,
provider/model, journal and lock validators remain real.

## B: the 17 original drift-refusal assertions

Only the final snapshot assertion changes: BOTH before and after remove exactly
.production.lock at the novel root. All 17 original damage injections, exception
conditions and all other paths remain unchanged. Production lock enforcement,
transaction ownership and recovery code are not weakened.

Parameters: missing_tx, chapter, backup, result, state, extra_prose, glossary,
checkpoint, candidate, journal, started, uncertain, corrupt, window, progress,
continuity, policy. Final results are recorded below.

## C: precise standalone authorization schema

Batch planning retains the exact base seven-digest plus revision schema for
ordinary audit-owned authorizations. A second alternative is available only for
a real source-owned standalone witness and includes standalone_witness_sha256.
The consumer calls the existing read-only verified_events and witnesses owners
to reprove original source/profile/policy, full chronology, spans, BEFORE backups,
transaction/authorization receipts and completion seal. It then requires exact
receipt equality with bound_authorization of that actual durable witness.

Only the re-proven plan is added to the owned-plan inventory. Canonical slot,
complete plan hash, revision, exact key set and canonical authorization hash
checks remain. An unowned extra witness field, absent binding, arbitrary extra
key, foreign plan/novel/source, orphan receipt, duplicated slot or self-rehashed
foreign evidence does not qualify. The reader never creates or backfills a
witness. Existing independent paid execution authorization remains mandatory;
batch specification still reports paid_execution_authorized=false.

The positive test creates a real known-fix through apply_known_glossary_fixes
under the actual novel lease, with real backup, prepared/completed witness and
receipts. Ten damage controls retain byte-identical project snapshots after
refusal. Existing base receipt variants and native GOAT/direct-Pro standalone
lineage controls are also exercised.

## Source changes and boundaries

- experimental_goat_audit_proof.py: new read-only experimental evidence owner.
- goat_periodic_audit_finalize_plan.py: shared source/chunk/result proof with
  provider-bound evidence selection; original native transport branch retained.
- production_periodic_audit_finalization.py: require accepted experimental paid
  proof before publication and during recovery, replacing the former exclusions.
- goat_batch_plan.py: recognize only fully re-proven witnessed authorizations.
- tests/test_experimental_goat_audit_proof.py: new focused controls.
- tests/test_goat_batch_standalone_receipts.py: new focused controls.
- tests/test_goat_periodic_audit_finalization.py: symmetric root-lock exclusion
  in the existing 17-parameter test only.
- This report and public_inventory.txt: reviewed public engineering artifacts.

historical_reconciliation.py, production_historical_lineage.py,
production_standalone_lineage.py, retroactive_transactions.py,
production_run_lock.py, production_paid_request_journal.py, provider send ceilings,
profiles, pending recovery, CI classification/schedule/timeouts, test deselections
and L4 policy are unchanged. No private novel was accessed. No D2 implementation.

## Remaining shard-0 performance work

Fixing the 17 postconditions does not make the full shard fit its execution
budget. Accepted telemetry still shows 79 completed finalization cases consuming
4479.478 seconds before the 4500.153-second timeout; 97 were collected. Hermes's
minimum projection is about 5500 seconds, and the retained profile records
54840 file_sha calls in one representative case. Full hosted coverage remains
unverified. The accepted report's single-traversal immutable proof-input bundle
or explicitly reviewed source-bound repartition remains future work. No cache,
coverage reduction, worker conversion, timeout increase or CI policy change is
implemented here.

## Validation and handoff

Every test uses Windows CPython 3.14.4, tests/zero_provider_validation.py,
a fresh external system TEMP child and explicit deselection of
 tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip.
The guard denies live network/provider access, real DPAPI and private roots.
Runtime Python bytes and HEAD were frozen through the long 17-case selection,
so its refusals cannot be attributed to unrelated source/approval drift.
Final counts, hashes, guard results and commit identity accompany delivery.
Independent Hermes review is the next action. No production command is proposed.

## Final focused evidence

| Selection | Result | Evidence |
| --- | --- | --- |
| A: original C2 plus experimental proof, repair/correction/split, twenty damage controls, semantic ancestry and real cross-provider substitution | 34 passed in 96.72 s | aut2-close-A-final2.xml |
| A: fresh-runtime stale proof after durable intent (changed journal/model) | 2 passed in 6.56 s | aut2-close-stale-recovery.xml |
| B: all 17 original drift parameters | 17 passed in 909.53 s | aut2-close-drift17.xml |
| C: real standalone receipts + ten damage controls; both original base receipt variants; generic direct aggregate; GOAT/direct-Pro standalone readers | 18 passed in 249.84 s | aut2-close-C-regression.xml |

Total: **71 unique focused cases passed**, zero failures/errors/skips. All 17
B cases reached their original refusal and exact symmetric preservation check.
C2 is resolved through actual accepted experimental paid-audit reproof; there is
no exclusion, xfail, additional deselection or reduced assertion. Four honest
interruption tails replay locally with zero duplicate sends; stale/foreign
paid evidence remains blocked before further mutation.

The JUnit/log files are retained under external system TEMP. The JSON summary
`aut2-close-final-validation.json` records each JUnit SHA-256 digest.
`aut2-close-frozen-source.json` binds all 236 root Python files and the fixed
915b6ae starting HEAD throughout the source-bound tests. Final syntax, index/tree/
reachable-history publication checks, clean state and exact new commit/tree/parent
are reported with delivery. The previous expensive 73-case history selection
was not repeated. No entire repository suite or hosted CI rerun was performed.

Reproduce the new and originally failing functional selections:

```powershell
$taskTemp = Join-Path $env:TEMP ('aut2-functional-review-' + [guid]::NewGuid().ToString('N'))
C:/Python314/python.exe tests/zero_provider_validation.py --basetemp $taskTemp --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q tests/test_experimental_goat_audit_proof.py tests/test_experimental_goat.py::test_L_natural_due_audit_and_JSON_repair_are_journaled tests/test_goat_periodic_audit_finalization.py::test_recovery_drift_fails_closed tests/test_goat_batch_standalone_receipts.py --tb=short
```

Review next; no production execution is authorized:

```powershell
git -C D:/NTOSS_AUT2_C_Lineage_Codex show --stat HEAD
```
