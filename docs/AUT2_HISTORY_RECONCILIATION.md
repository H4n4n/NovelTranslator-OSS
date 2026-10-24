# AUT-2: historical evidence and publication invalidation (first vertical slice)

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
