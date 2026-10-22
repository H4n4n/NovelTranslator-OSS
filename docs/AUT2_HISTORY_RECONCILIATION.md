# AUT-2: historical evidence and publication invalidation (first vertical slice)

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
