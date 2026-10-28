# AUT-2 Issue 18 — D1 publication dependency gate

This is a local D1 candidate based on clean commit
`b3447b6975c19ad70b15fe390c613e50107d75d4`. No D2 regeneration command is
implemented or executed by D1. PR 15 and its branch are unchanged.

## Qualified boundary and unresolved dependencies

The gate qualifies complete Chinese/Korean **TXT identity-spine, audit-lineage**
generations through the existing independent stage verifiers. Unsupported EPUB
source spines, source repartitioning and source snapshots that include standalone
known-fix events fail closed. Their existing durable C evidence remains valid
for historical readers; D1 does not silently reinterpret it as a supported
publication generation. Extending the publication source-snapshot owner to these
cases requires separate qualification. This is a bounded publication gate,
not a claim that Issue 18 or umbrella Issue 11 is complete.

J5/J8 literal/source-variant coverage remains **UNRESOLVED**. Deterministic
whole-history CHECKED is not semantic certification or proof that those cases
are resolved. Explicit review must retain unresolved decisions as blockers.
No semantic re-review, provider call, real novel access or EPUBCheck is performed.

## Exact graph and evidence owners

`publication_readiness.dependency_graph` returns a deterministic, read-only
candidate graph. Its SHA-256 covers all nodes, edges and historical invalidation
edges. It is not approval to publish. Paths are novel-relative; every physical
file is hashed as bytes, links/junctions are refused, JSON evidence must have
unique keys, and timestamp/file-presence checks never establish freshness.

| Node / dependency | Immutable or independently recomputed proof |
| --- | --- |
| Original source -> installed units | Original TXT byte hash; exact selected novel config, complete source inventory/unit hashes, source document identity, profile and novel policy hashes |
| Glossary decisions -> chapter revisions | Exact glossary and audit-history files; original transaction IDs; independent source/span authorization, BEFORE backups, FINALIZED receipts, complete ordered historical byte chain through `verified_events` |
| Every committed chapter | Exactly 1..configured-final inventory and progress/audit boundary; per-file bytes; exact source unit text hash; all corresponding historical BEFORE/AFTER transaction revisions; pending work blocks |
| Chapters + decisions -> state/continuity/issues | Exact state/progress/continuity/issues hashes in the source snapshot; separately required explicit semantic attestation for rolling state/continuity and issues |
| Chapters + decisions -> QA | Current mandatory local commit QA recomputed against current source, glossary and policy; result hashes; saved QA report inventory and bytes bound separately (old pass flags are never accepted as fresh QA) |
| QA + state -> finalization | Current complete committed bytes independently equal final chapters; exact identity-finalization manifest, generation intent and source provenance |
| Finalization -> normalization | Exact approved plan and baseline; independent reconstruction of allowed spans, output inventory and report semantics |
| Normalization -> titles | Exact title approval and source/input anchors; reconstructed output and report; unresolved/manual decisions retain existing blocking policy |
| Titles -> editorial input | Exact presentation transformation, source/output bytes and cleanup report |
| Editorial input -> verified final editorial | Source-anchored explicit authorization; reconstructed plan, apply report, guards and every output byte; current mandatory QA on editorial output; no production prose fallback |
| Decisions + editorial -> final glossary | `final/glossary.json` must parse identically to the current approved production glossary; its own exact byte hash is graph-bound and separately explicitly reviewed |
| Editorial + final glossary -> final EPUB target | Exact generation-owned target path; existing ZIP/OPF/spine/nav/NCX/reference/content/metadata/report validators remain owners; readiness checked before writes and immediately before terminal replacement, and twice for verify-only |

The source proof also binds every root Python module and every selected profile
file. Runtime changes invalidate the generation. The graph includes the hashes
of normalization/title/editorial authorizations through that generation identity.
Existing EPUBs are not included in their own prepublication graph (avoids a hash
cycle); their bytes and reports remain independently validated by the EPUB owner
and the current-pointer owner. A candidate graph never claims an existing EPUB
CURRENT. Checkpoint EPUBs remain preview-only.

## Durable invalidation

Both the real periodic finalizer and startup known-fix owner already capture
`publication_input_snapshot` before their historical writer runs. New snapshots
also inventory actual QA reports, state/continuity/issues, final glossary, and
all existing generation corpora, reports, manifests and EPUB targets. The durable
audit intent/receipt or prepared C witness binds that snapshot to its transaction.
No legacy receipt or witness is rewritten or auto-upgraded.

The graph rederives source-qualified invalidation edges from independently
verified plans and durable captured target inventories. Each edge binds novel,
transaction, source chapter hash, English BEFORE/AFTER revisions, target path and
captured target hash. Identical surviving target bytes remain STALE; absent
bytes are MISSING; changed bytes are NOT_VERIFIED. Only independently verified
current stages plus explicit review can pass the final gate. Whole-book stages
and state are conservatively dependent on all changed chapters. No blind prose
replacement or target overwrite is authorized by an invalidation edge.

Read-only replays write nothing. Historical receipts retain their existing
idempotent writer/recovery contracts. An interrupted prepared witness, orphan,
foreign/forked/competing transaction or partial/inconsistent receipt blocks via
its existing owner. Missing old inventories never prove absent dependencies;
the complete current generation proof is still mandatory at publication.

## Explicit review contract and actual publication path

`historical_reconciliation.require_publication_freshness` now refuses an
editorial-only corpus even if no historical receipt exists. The actual
`build_publication_epub` and `verify_existing_publication_epub` paths invoke it
before successful publication/verification, preserving all existing editorial
integrity and writer-lock behavior. It independently verifies the generation,
then `publication_readiness.require_publication_ready` verifies the exact graph.

The novel-local operator-owned approval is `config/publication_readiness.json`:

```json
{
  "revision": "publication-readiness-v1",
  "novel_id": "selected_novel",
  "graph_sha256": "EXACT_REVIEWED_CANDIDATE_GRAPH_HASH",
  "reviewer": "explicit reviewer identity",
  "reviews": {
    "state_continuity": "explicitly_verified",
    "issues": "explicitly_verified",
    "whole_history_semantics": "explicitly_verified",
    "final_glossary": "explicitly_verified"
  }
}
```

No production function writes this approval or performs the attested review.
A changed/missing dependency, changed target or foreign/partial approval blocks.
The declaration is explicit local operator authority under the repository's
existing cooperative writer/config-approval model. Hashes do not establish
human comprehension or defend against wholesale malicious replacement of all
local approvals and evidence. Merely recomputing a forged report checksum does
not bypass the independent owner validators.

## D2 prerequisites and ordering

1. Preserve original production/pending/provider journals, prior published bytes,
   historical backups, authorization and all immutable transaction evidence.
2. Resolve current source inventory, historical lineage and J5/J8 deferrals.
   Qualify unsupported source snapshots separately; do not widen source ownership
   or the existing source-variant eligibility rules as part of rebuilding.
3. Re-run deterministic local QA against every current chapter and glossary.
   Saved acquisition QA is evidence of its old revision, not a fresh pass.
4. Obtain explicit semantic review of rolling state/continuity and outstanding
   issues after the historical change, and whole-history meaning/terminology
   decisions. Local consistency checks cannot perform this work. Any semantic
   provider use requires separate explicit authorization; D1 never sends.
5. Review fresh normalization/title/editorial plans against current inputs; use
   the existing transactional stage owners in dependency order. Deterministic
   no-op normalization or exact approved anchors can be reverified locally;
   changed semantic/editorial decisions require explicit editorial verification.
6. Materialize and explicitly review the exact final glossary. Inspect the full
   candidate graph, including source-qualified invalidations, and approve its
   exact hash only after every semantic obligation is actually verified.
7. Only then qualify a D2 generation workflow that consumes this review contract,
   builds the EPUB from verified final editorial text, rechecks freshness before
   replacement, verifies ZIP/OPF/spine/nav/NCX/content/reports and promotes its
   pointer last. Existing regeneration currently stops at the new gate without
   these prerequisites; D1 does not add an automatic review producer.
8. Exercise interrupted rebuild/publication recovery under D2, preserving prior
   published bytes. Run EPUBCheck only if certification will be claimed.

## Local qualification

Windows Python 3.14.4; disposable synthetic Chinese/Korean three-chapter fixtures;
`tests/zero_provider_validation.py`; unique external temporary directories;
mandatory deselection of
`tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip`.
No network, DPAPI, credentials or paid requests. Readiness tests exercise the real gate
without readiness mocks. D1.1 adds complete-generation reuse and valid synthetic
ZIP fixture setup as described below; no D2 regeneration is performed.
Existing renderer unit tests explicitly isolate their rendering boundary; the
existing generation fixtures retain the real gate with synthetic explicit
review evidence. Test-only attestations do not certify semantic quality.

The handoff records exact focused commands/counts and commit/tree separately.
This candidate stops for independent Hermes review. No push, PR, merge or hosted
CI operation is authorized or performed in D1.

## D1.1 — CURRENT certification and audit-residue integration

Hermes LOCAL_ACCEPT baseline: commit
`d7a792c400ea069762bdb2ecbb902cee68ec29f3`, tree
`78c3ea771cf800cda616c9e26d78ba93984f782c`.

The existing-generation route in `publication_regeneration` now calls the same
D1 `require_publication_ready` validator **before any stage/report write** when
an EPUB already exists, and again immediately before pointer publication and
returning CURRENT. The read-only `current_generation` route independently calls
that validator before returning CURRENT. The archive/report-only `_verify_epub`
helper remains useful for proving a previous pointer's original bytes; it is
explicitly not certification authority. No new approval is generated by these
paths. Missing, malformed, forged, foreign or stale review evidence refuses
reuse/promotion and reports STALE/NOT_VERIFIED. Refusal preserves the existing
EPUB, pointer and stage bytes/timestamps.

`historical_reconciliation.registered_publication_generations` now separates
unrelated residue from registration claims. A bare directory name, ordinary
file or `.gitkeep` does not register a generation, including an otherwise
hash-shaped empty directory. These entries are preserved and cannot create
CURRENT status. Reserved registration/provenance/current-pointer filenames,
final-stage content and durable references remain evidence claims, not residue.

Registered generations require the exact immutable input-hash directory,
intent schema/hash, novel/config identity and well-formed JSON/hash evidence.
A provenance document, when present, must equal the original intent. A terminal
EPUB additionally requires that provenance and the existing archive/report proof.
An interrupted generation with a valid original intent and no terminal EPUB is
captured conservatively as unverified partial evidence; its surviving stages
and empty stage inventories retain invalidation obligations. This never grants
freshness or supplies a missing editorial approval.

The current pointer, prior audit intents, invalidation receipts and standalone
prepared witnesses retain generation references. Deleting a referenced root or
its original intent cannot turn it into ignorable residue. Missing/foreign or
competing registered evidence, malformed JSON/provenance and linked paths still
fail closed. Links are refused even within otherwise unrelated residue; captured
generation references reject traversal. No evidence is cleaned up, repaired or
silently registered by the inventory reader. All surviving registered targets
continue into the real finalizer's durable pre-edit invalidation snapshot.

D1.1 tests exercise `regenerate` only against already-complete disposable
Chinese/Korean generations. Their valid ZIPs are fixture setup through the
existing local editorial EPUB builder; no D2 source-to-EPUB regeneration is run.
The tests verify absent/current/previous pointer cases, repeated byte/timestamp
preservation, loss of review during the archive check, and valid idempotent reuse.
They also exercise real saved-paid-shaped audit finalization with unrelated
residue and verify that malformed referenced evidence still blocks before writes.
The synthetic plan Markdown fixture now uses the owner's exact bytes so an
unrelated Windows newline mismatch cannot mask a missing readiness check.

Scope, unresolved J5/J8 coverage, unsupported source/history boundaries and D2
prerequisites above are unchanged. No CI policy or test exclusion is changed.

D1.1 final guarded qualification: **123 contract tests + 19 focused existing
lease/history/transaction/isolation regressions = 142 passed** on Windows
Python 3.14.4. All runs used external TEMP, the zero-provider runner and mandatory
real-DPAPI deselection. Syntax, staged diff and publication-inventory checks
passed. Exact commands and JUnit evidence accompany the local Hermes handoff.
Both findings are closed within the bounded D1 contract; no D2 workflow or
semantic approval producer was introduced.


## Integration onto merged main

Baseline: `3f41bcb74548c22c6aa747220ffc2ad7ddfc930c`, tree
`5b4134881c5bdfb5eb52ba9ae7655577677dfbc2`, verified against fetched
`origin/main` in the existing `D:\NTOSS_AUT2_C_Lineage_Codex` checkout.
The checkout was clean. Local `main` was older and checked out elsewhere;
this integration leaves that checkout/ref alone and starts directly at the
verified merge commit. No new clone or worktree was created.

Branch: `codex/aut2-d1-d11-main-integration`.
Accepted commits `d7a792c400ea069762bdb2ecbb902cee68ec29f3` and
`c10f0b9aa13abc133c2b4fc7da560fa64506becf` were cherry-picked as
`891a710` and `248b5c0`. The original D1 branch remains at `c10f0b9`.
Only `public_inventory.txt` conflicted: both branches appended entries, and
all entries were retained. Historical source merged cleanly. The accepted
publication code/test files are unchanged, and main's historical proof and
reconciliation sections outside the D1 publication sections are byte-identical.

Merged main requires every test module to be explicitly classified and measured.
The new synthetic D1 module therefore needs public classification, a local
measurement in the existing shard schedule, and refreshed exact partition
assertions. The existing allocation algorithm, timeout, historical timing
provenance, private/historical classifications and every deselection remain
unchanged. Current source hashes and retained external JUnit evidence bind the
new measurements; these are local timing projections, not hosted qualification.

The final focused publication command is:

```powershell
python tests/zero_provider_validation.py `
  --basetemp "$env:TEMP/nt-aut2-d1-integration-measured-20261028-a" `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_publication_readiness.py tests/test_build_publication_epub.py `
  --junitxml="$env:TEMP/nt-aut2-d1-integration-measured-20261028-a.xml" --durations=5
```

The installed-corpus integration remains an explicit opt-in skip; private novels
are never opened. A separate 16-case guarded selection covers interrupted audit
commit tails, relative evidence paths, standalone lineage readers and same-size,
restored-mtime proof-input tampering. Compilation includes all integrated Python
files and the modified public-test bookkeeping. No provider, issue, push or
GitHub Actions operation is performed.


Integration validation on Windows Python 3.14.4:

- Publication contracts and existing EPUB suite: **316 passed, 1 skipped**
  (891.71 seconds). The skip is the unchanged private installed-corpus opt-in.
- Focused historical cross-integration selection: **16 passed** (478.47 seconds).
- Changed public classification/partition/source-binding checks: **7 passed**.
- `python -m py_compile` passed for `historical_reconciliation.py`,
  `publication_readiness.py`, `publication_regeneration.py`,
  `tests/test_build_publication_epub.py`, `tests/test_publication_readiness.py`
  and `tests/test_public_ci_reliability.py`.
- Local publication inventory validation and `git diff --check` passed.

The measured JUnit case sums are 504.699 seconds for readiness and 386.631 seconds
for EPUB. The complete suite's measured 0.312-second overhead is conservatively
included in EACH module weight; earlier weights are never discounted. This
replaces a provisional arbitrary one-second allowance. The schedule keeps all
198 public modules, all 15 existing deselections, the same measured-LPT allocator,
and the 4,500-second deadline. Nominal shard projections are approximately
3,415 seconds. The existing 30% sensitivity projections are
`4499.952 / 4499.951 / 4499.958` seconds: very close to the limit, and not hosted
runtime qualification. No unrelated expensive suite was rerun to improve these
projections. Timing variability remains an explicit limitation, not a semantic
publication blocker or evidence that hosted L3 passed.

Total completed local validation: **339 passed, 1 expected skip**. The initial
publication run was stopped and restarted with JUnit output when main's measured
schedule requirement was discovered; that partial run is not included in counts.
The original accepted branch and all main historical reconciliation behavior are
preserved. No remaining local functional blocker was found. Unsupported source
layouts/history, J5/J8 and automatic D2 regeneration retain the separate-work
boundaries stated above.
