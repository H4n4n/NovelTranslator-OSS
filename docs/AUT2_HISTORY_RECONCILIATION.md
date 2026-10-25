# AUT-2: historical evidence and publication invalidation (first vertical slice)

## AUT-2 C: durable standalone source lineage (local candidate)

This candidate starts exactly at the independently verified clean A+B worktree:

- commit `cb0d1d9773a7059791dae58e0928f6dd65b21648`;
- tree `3d6e832ab4d36ef4db4994d4277ea01290ad74f5`;
- parent `67c60bdb7c67b63b69157dba3acdaa75aa70f78a`.

The separate engineering checkout is `D:/NTOSS_AUT2_C_Lineage_Codex`, on
`codex/aut2-c-durable-lineage`. The original A+B worktree/commit are unchanged.
The supplied GOAT revision `e5aca8b35c4de0129dc83a0e551aefba012e4c2c` is not
available in the local object database. Local `main` points to another revision;
no ancestry relationship to that supplied SHA is asserted. No GOAT merge,
rebase, fetch, production activation or provider call belongs to this change.

### Ownership seams and risks

| Owner | Responsibility retained or added |
| --- | --- |
| Maintained translator startup | The existing known-fix policy is extracted into a pure function. It supplies the original complete glossary, staged glossary and exact rules before installing state. Recovery rederives that same policy. |
| `production_standalone_lineage.py` | Captures prepared evidence, independently proves source/byte ownership and original authorization, resumes only the exact original known-fix tail, and seals completed receipts. |
| `historical_reconciliation.py` | Composes standalone events between original ordered audits, preserves removed referent reservations, validates complete source/English inventory and binds physical evidence into later writer authorization. |
| `retroactive_span_planning.py`, `retroactive_transactions.py` | Unchanged positive eligibility, exact UTF-8 span transformation, immutable plan, BEFORE backups and transaction prefix recovery. |
| Shared periodic finalizer | Unchanged accepted-decision/accounting, transaction, associated-state and progress-last publication protocol. Its existing writer guard now consumes source-qualified standalone history. |
| `production_historical_lineage.py` | Read-only paid-disposition readers use the same verified event chain, including the decoded-text/BEFORE-byte bridge. No journal or pending-response repair is added. |
| Publication/whole-history readers | Reprove standalone history, include historical corrections in consistency reporting, and conservatively retain stale/unverified downstream status. No EPUB regeneration is added. |

The writer still requires the authentic process/thread/descriptor novel lease.
Read-only inventory verification is factored from that check for proof readers;
it cannot acquire or grant a lease. The existing transaction-directory lock is
unchanged. No nested novel lock or parallel production writer is introduced.

### Durable contract

Only a newly authorized source-owning startup operation may create:

```text
output/retroactive_fixes/standalone_lineage/<original-slot>.prepared.json
output/retroactive_fixes/standalone_lineage/<original-slot>.complete.json
```

The prepared witness is written with the existing fsync/atomic-replace writer
before the transaction begins. Its strict schema binds the original complete
glossary (including candidate identities and revision metadata), staged glossary,
canonical plan/transaction and slot, original complete committed English BEFORE
inventory, full original source-unit/provenance inventory, novel configuration,
immutable profile file identities, source and English byte spans/hashes, existing
writer authorization, prior ordered audit/witness identities, and downstream
input inventory. Current authoritative source units and real BEFORE backups,
not a result digest, reconstruct the ownership proof.

For this new path only, the existing authorization revision additionally binds
`standalone_witness_sha256` and recomputes its own digest. This binds the entire
prepared witness into the original transaction authorization. It prevents a
rehash of a modified glossary identity, source inventory or witness/seal from
standing in for the original authorization. Existing plans and authorizations
are not rewritten or migrated.

Backup-path comparison recognizes the same canonical novel-owned file across
relative production paths and absolute reader paths. The stored receipt bytes
and their physical hashes are never rewritten.

The completion seal is a separate immutable artifact written only after the
transaction is independently FINALIZED, every result/reconciliation/backup is
validated, all chapters match exact AFTER bytes, and the staged glossary has
been installed. It binds exact physical receipts and their chronology. Distinct
canonical reconciliation diagnostics from legitimate interrupted retries are
all verified; exactly one must match the FINALIZED state's reconciliation hash.
A noncanonical copied, renamed or contradictory receipt cannot qualify.

A prepared witness is never accepted as a completed proof by an audit. The
startup owner may resume only its matching original source/profile/inventory,
known-fix policy, glossary prefix and immutable transaction authorization under
the real lease. Crashes around prepared persistence, transaction prefixes,
glossary publication and completion persistence either resume that exact tail
or fail closed. An orphan/partial atomic scratch file is not promoted or
removed automatically; it blocks for investigation. No reader fabricates a
seal or backfills a legacy standalone transaction.

Later audit authorization independently reconstructs all intervening events in
order, checks prior physical evidence and complete source ownership, joins the
BEFORE/AFTER byte chain, reserves every historical referent family, and compares
that proof with the current immutable plan before any new historical write.
Missing/partial/foreign/forked/duplicated/tampered/unbound evidence raises
`HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN`. Ownership reservations remain vetoes,
never a source of expanded positive edit eligibility.

The Korean acceptance case exposed the existing startup normalization's hardcoded
`technique` category. The pure policy now retains the entry's actual normalized
category (for example `martial technique`), allowing the existing Korean profile
allowlist to evaluate it. Capitalization, source ownership, target cardinality
and the category allowlists themselves are unchanged.

### Failure-to-fix evidence

| Failure/requirement | Evidence and resulting behavior |
| --- | --- |
| Standalone correction loses original ownership before the next audit | `test_standalone_known_fix_continuation_has_source_owned_lineage` exercises the actual startup and later audit; the known-fix chapter mtime is unchanged by the audit. |
| Exact Chinese/Korean referent and byte ownership | `test_standalone_lineage_exact_source_spans_receipts_and_readers[zh/ko]` checks original glossary candidate ID, source offsets, UTF-8 BEFORE/AFTER span hashes, BOM/CRLF/Markdown preservation, independent readers and final whole-history reporting. |
| Repeated execution duplicates edits or authorization | Actual startup and FINALIZED audit replay preserve complete file bytes and mtimes; the external harness also proves a second legitimate standalone after an intervening audit and chapter commit. |
| Interrupted standalone persistence/publication | Twelve crash points cover before/after prepared persistence, PLANNED, backup, chapter, APPLIED, result, reconciliation, FINALIZED, glossary and before/after seal. The later audit blocks while incomplete; exact startup recovery finishes once. |
| Interrupted subsequent audit | Five crash points cover intent, invalidation, chapter, glossary and progress; restart preserves the already corrected standalone chapter. |
| Unproven history | Twenty-three witness/receipt/source/config/competing-plan damages preserve the entire protected tree; rehashing the prepared witness and seal still cannot replace its original transaction authorization. External probes add missing-directory, nested duplicate, orphan seal, foreign copy, missing source, extra chapter, no-lease, relative-path and original-layout cases. Malformed orphan plans and canonical foreign plans borrowing an existing transaction ID also fail with the provenance reason code. |
| D4/D5/J3/J4 and accepted A/B recovery | Existing transaction/lifecycle and B recovery suites retain their original adversarial assertions. Reservations and full original-source/English inventory remain mandatory. |
| Legacy transaction/snapshot evidence | The old standalone source-owned transaction is deliberately created without the new adapter; startup refuses retrospective witness creation and the later audit remains blocked. Existing unsupported snapshot tests remain unchanged. |
| Paid evidence/publication freshness | Pending Markdown/metadata and journal sentinels remain byte-identical. Paid-disposition readers prove the new byte bridge; existing output remains stale and publication is refused. |

### Qualification and limits

All pytest invocations use Windows Python 3.14.4, the existing
`tests/zero_provider_validation.py`, a fresh GUID-named external system-TEMP
basetemp, and explicit mandatory real-DPAPI deselection. No private novel,
provider transport, credential, GOAT send/replay, full hosted campaign or
independent frozen benchmark matrix is used.

| Focused run | Passed | Failed | Skipped | Deselected |
| --- | ---: | ---: | ---: | ---: |
| Complete transaction/lifecycle modules (before final edge hardening) | 397 | 0 | 0 | 0 |
| Affected writer-safety/startup/standalone selection after strict inventory and legacy fixes | 120 | 0 | 0 | 192 |
| New permanent legacy/malformed/forged-plan regressions | 3 | 0 | 0 | 312 |
| Final standalone selection after relative-path receipt fix | 46 | 0 | 0 | 270 |
| Final B recovery, Luna caller-runtime, paid journal and historical identity modules | 120 | 0 | 0 | 0 |
| Three bounded shared-finalizer cases | 3 | 0 | 0 | 94 |
| Shared-finalizer after-chapter recovery rerun after strict inventory fix | 1 | 0 | 0 | 96 |
| Bounded public EPUB fingerprint/ZIP/source-stop checks | 5 | 0 | 0 | 189 |
| Final external black-box probes | 17 | 0 | 0 | 0 |

These rows overlap; they are invocation counts, not distinct-case totals. The
complete core run took 864.71 seconds; the affected writer rerun took 436.92
seconds; the final recovery group took 16.03 seconds; the shared-finalizer group
and its final affected rerun took 369.72 and 153.78 seconds respectively. The
final standalone selection took 278.45 seconds; the external final run took
18.50 seconds. Early development failures in the Korean
category and legitimate interrupted reconciliation cases were corrected before
these qualification runs. Three added compatibility/security regressions pass.
Syntax validation covers all five changed Python files. Whitespace and staged publication checks pass for 491 public files. The
exact committed-tree guard is also run at delivery.

No private novel data was accessed. The C checkout has no repository-root
production data or pending paths. Every mutable state used above is disposable.

For a fresh independent focused run from the C checkout:

```powershell
$aut2Temp = Join-Path ([System.IO.Path]::GetTempPath()) ('aut2-c-review-' + [guid]::NewGuid().ToString('N'))
python tests/zero_provider_validation.py --basetemp $aut2Temp `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_retroactive_transactions.py -k 'writer_safety or standalone or startup_known_fix'
```


The separate external black-box harness is retained under system TEMP at
`aut2-c-probes-1eaac9956bdf47ea8e8b47a3bd279134/test_external_aut2_c.py`;
SHA-256 `e301d680eecd5a677e681fc3802adfbbcc2d950c1138b3b187363b48249ec7a7`.
It constructs its own canonical disposable novels and uses native policy/public
entry points rather than the repository's C fixture or internal witness builders.
Its 17 cases pass, including two interleaved standalone transactions, original-layout compatibility and relative-writer/absolute-reader composition. Its initial snapshot helper attempted to read a held Windows
lock file and was corrected to retain lock-file mtime without reading its locked
bytes; the resulting 10 harness errors are not implementation regressions.

Six optional source-bound publication selection cases fail at inherited fixture
setup, before their assertions: the Chinese/Korean forms of
`aut2_self_consistent_old_editorial_is_not_current`,
`aut2_conflicting_novel_lock_blocks_before_generation`, and
`aut2_newer_revision_invalidates_every_stage_without_rewriting_them`.
The fixture invokes the shared finalizer without a real novel lease. The exact
same **6 errors / 188 deselected** were independently reproduced under the guard
from an untouched public archive of the exact baseline. Those tests and their
production guards are unchanged; the errors are reported, not suppressed.

The original repository-level project layout is supported for new source-owned
witnesses using its existing legacy configuration/profile identity; this does
not migrate any legacy snapshot or old transaction. Publication regeneration's
existing source snapshot still expects audit-only provenance, so a standalone
history is not promoted into a fresh generation by this change.

Source/profile/configuration drift is deliberately conservative: this witness
revision requires its saved complete source-library identity and does not
migrate changed libraries or profiles. A different plan in the same original
startup slot remains an immutable conflict. Original legacy snapshots and old
standalone transactions without original source evidence remain blocked.
Hashes prove consistency under the existing cooperative-writer model, not
signatures against replacement of an entire evidence tree. Hosted L3, Hermes
exact-commit qualification, integration with current main and L4 remain separate.
This is no claim that AUT-2 Issue #18 or final EPUB publication is complete.

## Recovery regression correction (local descendant of the writer candidate)

The independent review of `67c60bdb7c67b63b69157dba3acdaa75aa70f78a`
(tree `59119ffbeeeffdec439b52a5d248c7deb659215d`) reported the mandatory
writer guard LOCAL_ACCEPT, 43/43 independent harness checks, 271 transaction tests
and 85 lifecycle tests passing, but all four original experiment recovery tests
failing. That commit and its evidence remain unchanged. This correction is a
new local descendant awaiting independent exact-commit qualification.

The root cause was not fixture-only. The disposable Luna helper provided one
source unit while the recovery fixture asserted 25 committed chapters, supplied
no complete English inventory, and held no novel lock. In the actual experiment
caller, `_run_real_engine_for_result` also installed the v3 recovery adapter
before acquiring its novel lock; installation can restore state immediately.
The existing lock now encloses engine loading, initialization, adapter
installation and the lifecycle. No second novel lock is acquired. Ordinary
production entry points already hold their novel lock before initialization.
Recovery APIs verify the authentic caller-held lease before snapshot reuse,
restoration, reconciliation, staging cleanup or post-marker backup repair.

After repairing the fixture and caller, the unchanged rollback assertion exposed
a further regression: the parent blanket-blocked restoration of the adapter's
own pre-audit prose snapshot. New recovery snapshots now retain the zero-edit
writer authorization that was independently derived before snapshot creation.
Restoration rederives that exact proof from the immutable original baseline,
current authoritative configuration/source inventory and verified historical
intents. Partial current glossary/history/accounting publication cannot become
a new baseline. Snapshot hashes, source drift or proof drift block before any
restoration. The snapshot covers the entire committed inventory even when the
due audit covers an earlier prefix. Post-marker backup repair independently
rechecks that original complete inventory as well.

This recovery-only reproof accepts no replacement rules or edits. It cannot
authorize a historical correction. A matching owned-span transaction still uses
the original BEFORE/AFTER, immutable-plan and backup recovery path; its prose and
evidence are preserved. Conflicting/unbound plans and missing historical lineage
still block. Old snapshots are not rewritten or given a manufactured proof;
without the new binding, changed-prose rollback remains blocked. Paid journals
and acquisition checkpoints remain outside the rollback set. No new transaction
plan schema, operation identity, positive span policy or standalone lineage
adapter is introduced.

The shared public Luna fixture helper now accepts an optional chapter count;
the recovery fixture creates a canonical 25-chapter configuration, complete
source units and committed English, and holds the actual novel lock. The four
original tests retain their assertions for recorded-response hydration without
reissue, uncertain-journal rejection, exact pre-audit byte restoration with paid
evidence preservation, and post-marker backup completion. Additional negative
tests cover absent locks (including saved snapshots and post-marker repair),
missing/sparse/contradictory source libraries, source drift after the snapshot,
changed/missing snapshot proof, baseline tampering and rejection of edit-bearing
snapshot proofs. An actual harness-call test exercises the v3 installation seam
under the real lease. The related v3 focused fixture also uses complete
committed English/progress and a genuine lock; its absent-paid-journal rejection
assertions are unchanged. No production data or GOAT replay is used.

Python 3.14.4 zero-provider qualification used a fresh external system-TEMP
GUID basetemp for every invocation and explicitly deselected the real-DPAPI
round-trip node. No private fixture was bypassed or accessed.

- Complete recovery module: **27 passed, 0 failed, exit 0** (14.76 s), including
  all four original cases. The unchanged four-case selection also passed alone
  before adding the negative regressions: **4 passed, exit 0** (4.38 s).
- Shared Luna-runtime fixture module: **12 passed, exit 0** (2.63 s).
- Related v3 carried-response/absent-paid-journal selection: **1 passed,
  4 deselected, exit 0** (3.02 s).
- Actual experiment caller restart-after-commit selection: **1 passed,
  4 deselected, exit 0** (3.07 s).
- Affected complete transaction/lifecycle modules: **356 passed, exit 0**
  (706.80 s), comprising 271 transaction tests and 85 lifecycle tests. This
  includes D4/D5/J3/J4, immutable/conflicting-plan and BEFORE/AFTER recovery,
  Chinese/Korean/isolation cases, and the standalone known-fix lineage blocker.
- Focused synthetic finalizer selection: **3 passed, 94 deselected, exit 0**
  (458.65 s): `fresh_audit_mixed_old_and_preferred_baseline_finalizes_once`,
  `zero_candidate_goat_audit_no_fabricated_paid_accounting`, and
  `shared_finalizer_reconciles_interrupted_new_pr155_transaction[after_chapter]`.

Syntax and whitespace checks passed. The staged publication guard passed for
490 public files; the exact committed-tree guard is also required at delivery. These bounded synthetic results do not requalify the frozen external
writer matrix or the full experiment/paid-replay suite.

**The standalone known-fix continuation remains LOCAL_BLOCKED** with
`HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN`. This correction does not supply the
separately designed durable standalone lineage adapter. No unrestricted writer
safety, production activation, provider activity or publication-currentness
claim follows from these tests. Stop after the local correction commit for
Command Code's independent retest; do not push or create a PR.

## Historical writer safety candidate (separate local descendant of PR #15)

This slice starts exactly at `8b60e9c2add101c32792e4698725f97a2cc5d307`
(tree `a5d895ebbf9cbdf65ecbc86cd8358d12a8ba080d`). PR #15's worktree,
branch, commits and evidence are unchanged. This is a local implementation
candidate awaiting Command Code's independent writer qualification; **L4 remains
NOT VERIFIED**. The independently frozen legacy matrix remains recorded as
50 PASS / 12 FAIL. It has not been modified, replaced or reclassified. Its
J5/J8 literal-source-variant limitations remain outside this correction.

### Boundaries and proof

The active lifecycle dispatches through the existing shared periodic finalizer.
That finalizer, the direct transaction entry, its internal execution seam, the
known-fix compatibility adapter, and the experimental baseline/restoration
adapter now require safety proof before new transaction/publication artifacts.
The local finalization executor still owns its existing novel lock and calls
that same finalizer; it has no alternate prose writer. Archived standalone
translator versions remain unsupported operator entry points.

The guard requires the actual caller-held novel lock lease, verified against
the existing process/thread registry and open descriptor. A rendezvous file's
existence is insufficient. The existing nested transaction lock is retained.
Direct callers can derive local context from canonical novel files; supplying
a discovery report or an eligibility count never grants authority.

Authoritative on-disk progress must be typed, ordered and continuous from
Chapter 1 through `last_completed`, with the exact next boundary. Its complete
canonical committed Markdown inventory must agree, including chapters skipped
by the span planner. Caller inventories must equal the complete current list
or its exact `1..through_chapter` prefix; empty, sparse, duplicated, reordered,
non-integer, omitted, extra and nested chapter inventories block. The original
on-disk source library is independently indexed under the novel configuration;
all source-unit metadata, configuration and safe-category policy are bound and
must agree with the live context. The engine does not trust a caller's subset.

All prior applicable immutable finalization intents are independently reproved
using the qualified reservation loader: ordered history, original source/unit
witnesses, baseline/post glossary ownership, deterministic plans, BEFORE backups,
byte chains, checkpoint state and paid leaf proofs where required. Removed
longer source identities and shared English target families remain reservations.
They can only veto edits independently selected by the existing
`owned-single-span-v1` planner. They cannot add anchors, accepted changes, aliases,
normalization, repeated-span pairing, or new positive eligibility. D4/D5 stop the
whole operation with `HISTORICAL_OWNERSHIP_RESERVATION`; conflicting saved plans
stop with `IMMUTABLE_PLAN_POLICY_CONFLICT`. Missing/unproven historical evidence
fails closed. No old plan is trimmed, rehashed, overwritten or migrated.

New authorization uses an immutable per-slot `.authorization.json` sidecar with
revision `historical-writer-authorization-v1`. It binds the exact original plan,
complete inventory/BEFORE revision, accepted-change/source witness, reservation
policy, prior intent identities and physical historical evidence hashes. It is
always rederived under the actual lock. A changed receipt/evidence/rule/config/
source inventory blocks; the receipt is never an independent authorization token.
A crash before plan publication can leave a proof-bound authorization prefix;
restart may reuse it only after exact reproof, never repair or replace it.

The transaction plan schema, operation slots, transaction IDs, exact UTF-8 byte
spans, original backups and result records remain unchanged. The finalizer also
binds authorization before an already-applied transaction's associated-state
publication tail. Invalidation still precedes prose; progress remains last.
Finalized direct replay verifies result/reconciliation hashes, exact state,
AFTER prose and BEFORE backups, and returns diagnostics without rewriting any
finalized artifact. BLOCKED or contradictory old states require disposition.

Zero-candidate audits remain local-only. The established no-request path may
have no acquisition checkpoint; absence is accepted only for an immutable,
reconstructed zero-send/zero-decision/zero-edit audit with unchanged glossary and
zero usage. Its absence is explicitly hashed as `ABSENT_ZERO_SEND`. No fake
checkpoint is manufactured. This exception is writer-specific; the qualified
preflight's default reservation behavior is unchanged.

### Deliberate compatibility limits — LOCAL_BLOCKED until proof exists

- Historical mutation must run under the actual novel lock with canonical,
  independently readable disk state. Sparse ad-hoc fixtures and unhydrated API
  contexts no longer authorize writes.
- Direct periodic requests require their matching immutable accepted intent.
  The existing legacy GOAT before-intent path can derive the exact candidate
  from the transport-free paid-checkpoint owner. An unprovable Direct periodic
  request fails closed rather than treating a checkpoint marker as approval.
- An old standalone plan without a bound authorization or original immutable
  source-unit witness cannot acquire a newly invented baseline. Unbound plans,
  incomplete/superseded/forked intent chains and legacy missing provenance remain
  blocked; this slice supplies no evidence migration or operator override.
  This also limits continuation after a newly guarded standalone known-fix
  transaction: a later periodic audit cannot bridge that standalone operation
  using only its plan and authorization digest. It remains blocked instead of
  guessing the intervening original glossary/source-unit state. A separately
  qualified standalone lineage adapter must persist/reconstruct the original
  ownership and inventory witnesses and compose their ordered BEFORE/AFTER chain
  with periodic intents. No such format/evidence migration is implemented here.
- Extra next-chapter final output belongs to the existing reviewed partial-commit
  startup reconciler, which still runs before due audits. Historical mutation
  blocks until that owner completes its independently proven tail. Matching
  pending Markdown alone is not an inventory exception or committed edit input.
  Pending work is preserved; the writer does not finish, delete or retranslate it.
- Experimental legacy prose rollback is blocked when a complete source/ownership
  proof cannot be established. Valid owned-span BEFORE/AFTER recovery is retained.

These are explicit fail-closed boundaries, not a claim that all historical
production data can already be migrated or that unrestricted historical mutation
is safe. No provider execution, production input, paid replay, publication
currentness assertion, CI policy change, push or new PR is authorized here.

### Writer regression qualification

New `writer_safety` tests map to WS-D4/D5, WS-J3/J4, WS-HIST, WS-ATOM, WS-GUARD,
WS-HASH, WS-LOCK, WS-IDEM and WS-GREEN from the external frozen handoff. They cover
the actual finalizer, direct/internal transaction, startup known-fix and real
restoration boundaries, including unsafe PLANNED/APPLIED/FINALIZED legacy
prefixes. Blocked cases compare complete disposable novel bytes and mtimes;
held Windows rendezvous files are compared by inode/size/mtime because their
locked bytes intentionally cannot be read. Journals, pending work, backups,
receipts, audit evidence, final/EPUB stand-ins and unrelated novels are preserved.

Existing sparse synthetic fixtures now use actual complete canonical disk
inventories. Integration fixtures that previously asserted an audited prefix
without intents now finalize a genuine zero-send prior audit. Their deletion /
corruption injections target the current transaction and preserve prior lineage.
The four original unsafe characterization nodes now assert blocking; their old
unsafe observations and the independent 62-case evidence remain preserved.

Python 3.14.4 guarded zero-provider qualification (each run used a fresh GUID
child of the external system TEMP directory and explicitly deselected
`tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip`):

- `tests/test_retroactive_transactions.py -k writer_safety`: **77 passed,
  193 deselected, exit 0** (169.15 s).
- `tests/test_retroactive_transactions.py tests/test_production_lifecycle.py`:
  **355 passed, exit 0** (844.78 s). This collection preceded the single
  standalone-continuation regression appended below; runtime code did not change.
- `tests/test_retroactive_transactions.py -k standalone_known_fix_continuation`:
  **1 passed, 270 deselected, exit 0** (8.00 s), confirming the documented
  compatibility block without mutation.
- `tests/test_goat_periodic_audit_finalization.py -k
  'fresh_audit_mixed_old_and_preferred_baseline_finalizes_once or
  zero_candidate_goat_audit_no_fabricated_paid_accounting or
  aut2_review_paid_proof_failure_precedes_first_receipt or
  shared_finalizer_reconciles_interrupted_new_pr155_transaction'`:
  **9 passed, 88 deselected, exit 0** (1285.28 s).

`python -m py_compile` passed for all eight changed Python files; whitespace
checks passed. The staged publication guard passed for 490 files. The exact
committed-tree guard must also pass after the local commit. The finalizer
selection is focused integration evidence, not a qualification claim for its
entire module or the frozen independent matrix. No private fixture, production
input, real provider or real DPAPI was accessed.

Stop after the local correction commit for Command Code's independent exact-tree
qualification. Do not push, open another PR, activate production or infer an
exception from any earlier PR's disposition.

## Phase 3 adversarial correction: local candidate, writer still blocked

The original candidate `505057f38250cda0664009dbc0065008b4f049d3`
(tree `288ca62d98d7e25ecd384a02098431aff962c93e`, baseline main
`e1d99f0f191efcacfd2265f079e611407ef018f4`) and its qualification evidence
below are retained. This correction is a separate local descendant, pending
independent Command Code retest. **LOCAL_BLOCKED for production historical
mutation/publication.** The frozen legacy matrix's reported 50/62 result is
not reclassified as passing by this read-only repair.

`aut2-impact-preflight-v2` adds a separate historical ownership veto inventory.
It discovers all prior finalization intents in the novel's audit directory and
requires an exact, contiguous ordered correspondence with the selected intent's
baseline audit history. It checks immutable intent/hash/path/novel identity,
source-bound deterministic transaction reconstruction, finalized transaction
state/results/reconciliation, original BEFORE backups and their chapter byte
chain to the selected intent, accepted audit-result regeneration, published
records, acquisition checkpoint hashes, and checkpoint state/continuity backups.
GOAT candidate-bearing audits retain the existing transport-free paid-checkpoint
proof. Missing legacy source-unit evidence, missing intents, contradictory
publications, orphan/forked intents and free-standing history markers cannot
witness ownership: discovery raises
`HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN` and returns no eligibility report.
All newly read checkpoint backup files participate in the before/after evidence
snapshot; no evidence is repaired or persisted by discovery.

Verified BEFORE and AFTER ownership inventories are accumulated across prior
intents, preserving removed keys, explicit source forms/variants, longer source
identities, contextual/alias constraints, and every existing target-family field.
The original selected-policy `plan_chapter()` must first authorize a span. A
second call with historical reservations may only veto that decision (including
reverse already-current probes). Reservations never grant a missing source
anchor, old rendering, accepted change or new edit. D4 (removed longer source
identity) and D5 (removed shared/nested target owner) now defer with
`HISTORICAL_OWNERSHIP_RESERVATION` and the existing span-owner reason. The
`owned-single-span-v1` transaction policy and production writer are unchanged.

J5b is reported as `coverage_unproven` with
`SOURCE_IDENTITY_COVERAGE_INCOMPLETE` when there is no source/English candidate
but literal inventory coverage leaves CJK unified/compatibility ideographs or
Hangul characters unclaimed. `potential_impact` remains false: the unknown text
has not been assigned to this referent. Reports add a bounded per-chapter
unowned-script count, literal coverage status, and `coverage_unproven_chapters`.
Known unrelated identities fully covering that script, or absence of such
script, retain the supported `unaffected` classification.

Discovery's exact limits: it compares literal registered forms and bounded
English target families, and preserves ownership found in verified prior audit
snapshots. It cannot infer undeclared Chinese variants, simplified/traditional
relationships, NFC/NFD equivalence, corrupted spellings, translations with
unregistered English targets, implicit referents, or semantic alignment. It does
not invent aliases or treat every unclaimed source character as an affected
identity. Ordinary CJK narrative outside registered forms will conservatively
leave coverage unproven for otherwise absent candidates. Even
`literal_inventory_covered` means coverage of recognized source-script positions
by literal forms, not linguistic/semantic completeness. Historical identities
never captured by an available immutable audit snapshot cannot be reconstructed;
missing required snapshots block. Checksums are local consistency evidence under
cooperating writer exclusion, not signatures against wholesale evidence forgery.

### Legacy writer reachability and immediate follow-up safety slice

Inspection of the actual active path establishes:

- `production_lifecycle.py` dispatches due audits through the production adapter's
  `run_periodic_audit()` / `finalize_periodic_audit()` into
  `production_periodic_audit_finalization.finalize_periodic_audit()`, then
  `retroactive_transactions.execute_retroactive_transaction()`.
- The finalizer's `transaction_proof()` constructs ownership only from the
  selected audit's BEFORE/AFTER glossaries. It does not consult historical
  ownership reservations. Its immutable intent, source hashes, transaction
  regeneration, backup checks and final-state proof can all agree with an unsafe
  D4/D5 span. The read-only preflight is not invoked by this writer.
- `test_legacy_writer_risk_d4_d5_reaches_actual_finalizer` finalizes real prior
  synthetic rejection intents (the removed identity has literal source evidence),
  then the current source-bound correction intent. The preflight defers without
  any writes, while the actual unmodified finalizer changes the ambiguous
  Chapter 1 `Han` to `Hann` and reaches FINALIZED with its original intent intact.
  These are disposable direct-provider-policy fixtures with synthetic accepted
  audit checkpoints, not provider calls or claims about paid model behavior.
- J3/J4 are accepted at the finalizer seam: `transaction_proof()` derives its
  committed list from `progress.completed`; the transaction filters/deduplicates
  the supplied list. The outer chapter manifest checks drift relative to the
  captured intent, not completeness against an independently proven inventory.
  `test_legacy_writer_risk_j3_j4_sparse_progress_reaches_finalizer_seam` proves
  both empty and Chapter-1-omitting inventories can publish a source-bound intent
  at that seam (the latter edits Chapters 25/26 while omitting Chapter 1).
  Normal unattended startup **does** reject these sparse progress/file states
  through `startup_integrity_errors()` / `validate_progress_consistency()`.
  Thus this is a boundary/recovery-caller gap, not evidence that normal validated
  startup accepts the same malformed inventory. The low-level writer also lacks
  an independent progress/file inventory authority.

Immediately following, independently qualify a minimal writer safety slice:

1. At every mutation authorization entry (normal finalization, local recovery and
   compatibility/known-fix adapters), reprove historical ownership reservations
   and veto ambiguous planned spans. Bind a versioned reservation proof/hash to
   new transaction authorization. Keep positive span eligibility unchanged; never
   merge new evidence into an existing immutable request or regenerate a saved
   plan under a new policy.
2. Independently load authoritative disk progress under the novel lock; require
   typed contiguous `1..last_completed` and exact agreement with the caller's
   complete bounded inventory, authoritative source units and canonical committed
   chapter file inventory. Handle only the existing separately proven next-file /
   pending recovery exception; it cannot become committed edit input. Reject
   missing, extra, duplicate, nested or omitted committed chapters before writes.
   Bind all chapters, including skipped ones, to the immutable proof.
3. Recheck the reservation/inventory proofs under existing novel and transaction
   locks before any new backup/prose write, retaining current plan slots,
   source/BEFORE/AFTER hashes, exact byte spans, backups, results, reconciliation,
   invalidation ordering and progress-last publication. Old finalized evidence
   stays immutable; an old pending plan conflicting with the veto must stop for
   disposition, not be overwritten/migrated/reissued. No automatic prose rollback.
4. Qualify interruption at every existing durability boundary, prefix recovery,
   repeated-run idempotence, skipped-chapter drift, all writer adapters and the
   complete independent legacy matrix. No additional production historical
   correction is enabled until this separately reviewed slice resolves safety.

### Correction qualification

The regression selection below is synthetic and zero-provider. It asserts exact
bytes and modification times of the entire disposable novel tree around
preflight, including committed English, glossary/progress, unrelated paid-pending
stand-ins, immutable audit evidence, transaction backups and EPUB stand-ins.
Writer-risk characterization tests explicitly invoke the existing writer only
on separate disposable fixtures. The private fixture
`test_runtime_audit_prompt_reconstruction_uses_initializer_assignment` remains
excluded and was not accessed. CI, guard policy and private/GOAT data are untouched.

Final correction qualification on Python 3.14.4:

- `python -m py_compile historical_reconciliation.py tests/test_retroactive_transactions.py`:
  exit 0; diff whitespace checks: exit 0.
- Guarded `-q tests/test_retroactive_transactions.py tests/test_production_lifecycle.py --tb=short`:
  **278 passed**, zero skips, exit 0 (135.68 seconds).
- Guarded `-q tests/test_glossary_audit.py tests/test_chapter_glossary_review.py
  tests/test_goat_periodic_audit_finalization.py tests/test_build_publication_epub.py --tb=short`:
  **461 passed, 4 skipped, 1 deselected**, exit 0 (1549.26 seconds). The four
  existing skips are optional/live-fixture conditions; none is a new regression
  exemption. The excluded private node is named above.
- Each guarded invocation used `tests/zero_provider_validation.py`, its own fresh
  GUID-named external system-TEMP basetemp and explicit mandatory
  `--deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip`.
  The auxiliary invocation also explicitly deselected
  `tests/test_glossary_audit.py::test_runtime_audit_prompt_reconstruction_uses_initializer_assignment`.
  No guard/CI policy was modified and no private fixture was supplied.
- Staged publication guard: **PASS, 490 public files**. The committed-tree guard,
  exact commit/tree/parent identities and final clean status are recorded in the
  local handoff after commit; this document cannot embed its own commit hash.

Stop for independent Command Code retest of the exact correction commit; no
push, PR, provider run, or production historical activation is authorized by
this report. The legacy 50/62 matrix remains a separate unresolved qualification,
with the writer risks and immediately following safety slice described above.

## Phase 3: explicit read-only impact preflight

`historical_reconciliation.historical_impact_preflight()` is an additive API
based on `e1d99f0f191efcacfd2265f079e611407ef018f4`. It is not called by the
production writer and never saves a report, acquires a provider response,
retranslates, changes a glossary, or invokes a historical transaction.

The architecture audit found these existing authorities:

- `glossary_audit.py` validates immutable candidate IDs and alias families;
  `chapter_glossary_review.py` owns its separate accepted chapter-review result
  and source/translation/candidate input identity.
- The production adapter's `apply_periodic_audit()` owns accepted glossary
  changes, preserves locked entries, and emits retroactive proposals. Rejection
  and contextualization do not authorize blanket historical replacement.
- `production_periodic_audit_finalization.py` stages BEFORE/AFTER glossaries and
  binds accepted changes, original source inventory, audit checkpoint, state,
  progress, and immutable `retroactive_transactions.py` plan. Its existing
  `transaction_proof()` regenerates the original plan from exact BEFORE bytes
  and verified backups. Checksums are local consistency evidence, not signatures.
- `production_historical_lineage.py` owns proofs of older superseded audit
  chains. `final_consistency_report()` separately checks final whole-book audit
  history and reports unresolved renderings; the new preflight does not certify
  final consistency or publication freshness.
- `ownership_policy()` and `plan_chapter()` retain all source/target ownership
  policy, using the core longest non-overlapping source claims. Discovery adds
  classifications, not a second glossary owner or a more permissive writer.

Call under the existing cooperative novel writer exclusion, with an initialized
local module and an independently selected immutable periodic intent hash:

```python
from historical_reconciliation import historical_impact_preflight

report = historical_impact_preflight(
    module,
    selected_changes,  # exact rules from intent.record.changes.retroactive_replacements
    through_chapter=401,
    audit_start=376,
    audit_end=401,
    expected_intent_sha256=independently_selected_intent_hash,
)
```

The rule shape is `chinese`, `old_english`, `new_english`, `category`, `reason`;
the existing `chinese` field also carries exact Korean source identities.
For accepted contextualizations, supply the exact old/new canonical values
(normally the new canonical value is empty) and category from the intent's
BEFORE/AFTER entries. Optional `change_kind` is `canonical` (default),
`alias_merge`, `contextual`, or `alias_split`. Splits are explicitly deferred
with `ALIAS_SPLIT_PROVENANCE_UNAVAILABLE` because the existing periodic owner
does not issue a split decision. Other unaccepted proposals report
`CHANGE_NOT_IN_VERIFIED_AUDIT`. Calling without all three provenance arguments
reports `MISSING_ACCEPTED_CHANGE_PROVENANCE` and cannot prove eligibility.
Supplying a missing, mismatched, malformed or drifted witness blocks instead.

The supported witnessed scope is the current periodic audit publication prefix,
including an untouched immutable intent before its transaction and a finalized
intent at the current last-completed chapter. Existing partial transaction
prefixes must satisfy the existing transaction and publication validators.
An older superseded intent blocks with
`SUPERSEDED_AUDIT_REQUIRES_LINEAGE_REVIEW`; this API does not invent a bridge
over later glossary/chapter changes. Chapter-review, newly discovered referent,
and external alias-split provenance have no adapter here and remain deferred
proposals. This is a specific dependency boundary, not a claim to implement
all of Issue #11.

Every committed source unit and exact English bytes from Chapter 1 through the
selected boundary is examined, including chapters hundreds of chapters before
the decision window. Progress must be contiguous and the complete current
committed file inventory must match it. Full source units (including metadata),
English bytes, original/staged/current glossaries, safe categories, ownership
policy, selected changes, evidence, and audit/transaction identity are hashed.
No index or saved discovery report can omit history or grant authority. Evidence
is checked again after computation; callers must still exclude cooperating
writers, since this is not an operating-system atomic filesystem snapshot.

`potential_impact` is an advisory candidate flag, independent of `outcome`:
`proven_eligible`, `deferred`, `already_current`, `unaffected`, or
`coverage_unproven` (v2). Counts are
chapter sets and can overlap when selecting multiple changes. Eligibility and
already-current claims both require `plan_chapter()` ownership validation;
already-current detection uses the same validator in reverse without editing.
Repeated source or target occurrences, aliases, contextual entries, competing
target families, longer source names and missing provenance carry reason codes.
Two source mentions plus two English occurrences never prove independent pairing.

Hard limits are 64 selected changes, 10,000 committed chapters, 100,000
chapter/change rows, 100,000 evidence files and 16 MiB per examined English chapter; exceeding a limit
blocks rather than truncating. Reports contain only fixed status/reason codes,
counts, chapter numbers and opaque hashes, with zero corrected chapters and
zero provider calls. There are no chapter bodies, source prose, term strings,
filesystem paths, credentials or provider responses in the returned report.
The report is never edit authority: existing immutable transaction plans and
their final validators must independently reprove any actual modification.

Disposable public tests in `tests/test_retroactive_transactions.py` cover
Chinese/Korean full-history discovery, Chapters 1/25/26/388/401, pending and
finalized intents, exact BOM/CRLF/Unicode/Markdown preservation, repeated
occurrences, nested/shared names, aliases and contextual decisions, missing and
tampered provenance, inventory drift, bounds and multi-novel preservation.
They compare both bytes and modification times of the entire synthetic novel
tree, including unrelated pending work, backups and EPUBs.

Phase 3 local validation on Python 3.14.4 (all pytest runs use
`tests/zero_provider_validation.py`, unique external system-TEMP basetemps and
explicit real-Windows-DPAPI deselection):

- `python -m py_compile historical_reconciliation.py tests/test_retroactive_transactions.py`:
  exit 0.
- `python scripts/publication_guard.py --index`: PASS, 490 files, exit 0;
  staged diff whitespace check: exit 0.
- `-q tests/test_retroactive_transactions.py tests/test_production_lifecycle.py`:
  248 passed, zero skips, exit 0. After adding three prior-record negative tests,
  `-q tests/test_retroactive_transactions.py -k discovery_reproves_prior_audit_records`:
  3 passed, 163 deselected, zero skips, exit 0. The implementation did not change
  between those runs.
- `-q tests/test_glossary_audit.py tests/test_chapter_glossary_review.py -k
  'not runtime_audit_prompt_reconstruction_uses_initializer_assignment'`:
  171 passed, 3 existing skips, 1 deselected, exit 0.
- The unfiltered glossary/review run returned 171 passed, 3 skipped, 1 failed,
  exit 1. The failing node is
  `tests/test_glossary_audit.py::test_runtime_audit_prompt_reconstruction_uses_initializer_assignment`.
  It attempts to open a repository novel configuration under the private-data
  guard. Running that exact node from an untouched public archive of baseline
  `e1d99f0f191efcacfd2265f079e611407ef018f4` gives the identical
  `ZERO Provider suite: private project data access forbidden` failure, exit 1.
  No private fixture was read and no guard was weakened. The explicit public
  selection above excludes this independently confirmed baseline dependency.

Independent Command Code validation of the exact local commit/tree remains the
next handoff. This phase does not authorize a push, PR, production activation,
provider send/replay, or completion of Issue #11.

This change is a dependency-bounded first slice of Issue #11. **AUT-2 is not
complete.** Hosted independent L3 qualification and separate owner disposition
remain required. No AUT-1 PR #10 exception transfers to this branch.

## Architecture and safety inventory

The baseline is commit `a3d150389c83f532943f0d280120aff3708f081b`, tree
`32cff335215c365e5c55ec1529ae7eb0708a0cea`. Development uses the prepared
`codex/aut2-whole-history-epub-20261022` branch in an isolated public worktree.
There is no public production progress or pending translation in this checkout;
only disposable test novels are read or modified.

The existing periodic audit already passes every committed chapter up to the
audit boundary to `retroactive_transactions.py`. The 25-chapter restriction
belongs to audit acquisition's candidate window, not the historical transaction
inventory. `production_periodic_audit_finalization.py` stages glossary decisions,
reproves the deterministic owned-span plan, installs historical changes and
associated state, builds checkpoint backups, and publishes progress last.
`production_historical_lineage.py` independently verifies the existing GOAT audit
byte chain. These owners remain in place.

The final audit in `production_lifecycle.py` previously covered only an unaudited
tail. Downstream normalization, title, publication and editorial applicators
require explicit approved plans, immutable inputs and independent output proofs.
Their refusal to overwrite terminal corpora prevents treating them as generic
unattended rebuild callbacks. An editorial fingerprint can remain internally
valid while its production ancestors have changed.

## Implemented behavior

New audit intents bind a whole-committed-history impact report to their record
and history publication. The report binds complete original source-unit metadata
and text hashes, committed BEFORE/expected AFTER hashes, the staged glossary
revision and existing transaction identity. It distinguishes independently
eligible, deferred, already-current and needs-further-review chapter/term cases.
Counts are chapter sets per outcome and can overlap; they are not a partition.
Terms and rules use hashes. Reports contain no chapter/source prose.

The existing span planner and writer are unchanged. One owned source occurrence
and one unambiguous bounded English occurrence remain necessary. Multiple
independent chapters/terms retain their existing transaction support. Repeated
occurrences, competing referents, aliases, contextual entries, nested/overlapping
names and unsupported categories are deferred. No relaxed alignment policy,
blanket replacement, new glossary authority, provider operation or journal is
introduced. A discovery report never grants edit authority. The outer audit
still must reprove the transaction and publish progress last.

A durable per-transaction invalidation record under
`output/publication_freshness/` precedes new historical mutation. Its BEFORE and
expected AFTER chapter revisions, glossary revision and audit intent link the
correction to checkpoint EPUB, finalization, normalization, titles, publication
chapters, editorial and publication EPUB dependencies. The owning finalizer
reconstructs a missing derived invalidation from its validated immutable intent;
contradictory or malformed existing evidence blocks without overwrite. This
record can only withhold freshness; it cannot authorize a prose/output write.
The read-only publication status refuses missing, foreign, orphaned or tampered
receipts and never accepts a stored CURRENT flag.

After the final tail audit, normal unattended completion runs a separate local
whole-history pass. It checks the entire committed inventory and original source
inventory, revalidates original plans/results/backups, ordered audit byte chains,
audit checkpoint hashes and checkpoint-state backups, accepted glossary state and
outer audit records. GOAT paid audit evidence uses the existing transport-free
checkpoint proof. It then examines all source-owned current glossary identities
in every committed chapter, plus removed identities with recorded corrections.
Only the existing span owner can prove a current rendering; ambiguity remains
explicitly deferred. Reports under `output/audits/whole_history/<hash>.json` are
immutable derived evidence; identical replay creates no additional writes.
Missing legacy audit intent/plan history blocks proof rather than inventing a
baseline or declaring tail-only coverage sufficient.

The final pass does not change prose, glossary, progress, costs or paid journals.
It reports local consistency, not a new model semantic review. Normal completion
says `TRANSLATION COMMITTED — PUBLICATION NOT VERIFIED`. Bounded trial behavior
and GOAT lifetime NO_SEND / NO_REPLAY remain unchanged.

`status_project.py` displays conservative publication freshness. Existing EPUB
bytes remain preserved. Publication build **and verify-only** refuse to certify
an old editorial corpus after any AUT-2 audit invalidation, including an audit
with no prose edits: the changed audit/glossary completion still needs downstream
proof. Malformed receipts fail closed. Ordinary projects with no AUT-2 audit
intents keep their existing verified builder behavior. Checkpoint building
remains local-only and can run, but this slice never certifies a checkpoint as a
current whole-novel publication or as synchronized with the revised final corpus.

## Explicit remaining boundaries under Issue #11

1. Whole-history acquisition/review for newly discovered referents, alias splits,
   chapter-level canonical revisions outside periodic audit decisions, and
   independently aligned repeated spans. This slice discovers/reports; it does
   not manufacture the missing approved historical decision.
2. A separately reviewed downstream operation policy and provenance chain from
   revised committed chapters through finalization/repartition, normalization,
   title plans, publication and editorial application. Existing plans cannot be
   silently carried forward over revised source prose.
3. Verified local regeneration and freshness receipts for partial checkpoints,
   volumes and whole-novel EPUBs, using existing writers and ZIP/OPF/spine/TOC
   validators. No output is rebuilt here. AUT-2 invalidation deliberately has no
   operator override/reset/CURRENT promotion in this slice; publication remains
   blocked until that dependent provenance protocol is implemented.
4. A reviewed legacy-evidence migration where full immutable audit history is
   unavailable. Missing evidence currently blocks the final whole-history proof.
5. Independent hosted L3 and any separately required L4/owner disposition. These
   synthetic tests do not qualify paid operation or translation quality.

The next safe engineering step is the dependent downstream-provenance/rebuild
slice on this branch/PR or a separately reviewed dependent PR under Issue #11.
Do not close Issue #11 or activate production based on this first slice.

## Focused validation

Run only from the sanitized public checkout, using an unused external temp root:

```powershell
python tests/zero_provider_validation.py --basetemp "$env:TEMP/aut2-focused" `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_retroactive_transactions.py tests/test_production_lifecycle.py `
  tests/test_goat_periodic_audit_finalization.py tests/test_build_publication_epub.py
```

New Chinese/Korean fixtures cover early chapters across the 25-chapter boundary,
exact untouched BOM/CRLF/Unicode/Markdown bytes, verified backups and AFTER
hashes, deferred repeated occurrences, final full-range checks, restart without
new writes, invalid source/glossary/checkpoint/intent/backup state, foreign or
extra chapters and stale-publication refusal. Existing owned-span negatives,
GOAT audit crash/lineage tests, AUT-1 recovery tests and publication ZIP checks
remain the regression owners. New tests use existing classified public modules;
CI policy, shard classification and historical benchmark/evaluation artifacts
are unchanged.

## Local qualification limits

The focused core run passed 212 tests with one existing optional test skipped.
Separate guarded import/lifecycle checks passed 22 tests. After the final
schema correction, 28 whole-history/import cases and 3 proof-contract/lifecycle
cases passed. All 8 targeted audit invalidation/crash cases passed (7 in the
combined run, with the remaining case rerun successfully after a concurrent
source edit correctly invalidated its runtime approval). Superseded broad audit
runs were stopped rather than reported as passing. Syntax checks cover every
changed Python file. Staged publication guard: PASS, 488 public files.

Two `test_production_run_lock.py` cases expecting exit 1 from
`build_normalization_plan` and `build_title_resolution_plan` fail with exit 2
(`PRIVATE_INPUTS_UNAVAILABLE`). Both failures were independently reproduced
under the zero-provider guard from an untouched public archive of the exact
baseline commit. Their modules, tests and CI classification remain unchanged.
No private plan inputs were supplied and no test was deselected to conceal them.
These baseline failures remain an independent qualification concern.
