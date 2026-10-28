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

The previous report claimed **339 passed, 1 expected skip**. Retained JUnit
artifacts support **323 passed, 1 expected skip**: 316 publication passes plus
7 metadata passes. The separately reported 16 historical cross-integration
passes have no retained JUnit artifact and are excluded from the supported total.
The initial publication run was stopped and restarted with JUnit output when
main's measured schedule requirement was discovered; that partial run is not
included in counts. The original accepted branch and all main historical
reconciliation behavior are preserved. Hermes subsequently found BLOCKER-1:
test fixtures supplied a missing generation glossary snapshot and concealed a
fresh-regeneration certification failure. Unsupported source layouts/history,
J5/J8 and automatic D2 regeneration retain the separate-work boundaries stated
above.

## Hermes BLOCKER-1 correction

Starting from `eef837cb5ed42a67a72bf817beb7f9e00a4c6bbb` on the same integration
branch, `_regenerate_locked` now writes the immutable `final/glossary.json`
snapshot immediately before `source_bound_provenance.json`, using the existing
helper and current project glossary. Source authority, generation identity and
CURRENT pointer publication are unchanged.

The EPUB fixture no longer supplies that snapshot. The readiness fixture runs
the real locked regeneration path to a synthetic interruption immediately before
EPUB creation instead of assembling equivalent stages and a glossary itself.
The existing Chinese/Korean fresh `regenerate()` test starts without a generation
or pointer, checks the generated glossary and pointer, and independently requires
`current_generation()` to return CURRENT. Synthetic review evidence remains
explicit; no semantic review bypass was added.

At commit `37b4e1b`, the CI shard schedule remained unchanged. Its exact
test-source freshness assertion failed because the corrected fixtures differed
from the source hashes in
the retained timing evidence; the other six metadata checks pass. This is a
remaining CI metadata blocker, not a claimed green CI result. No timing hash was
relabelled, old weight reduced, or projected headroom increased. Hosted timing
remains unverified until a separately authorized push.

Correction validation on Windows Python 3.14.4:

- Removing fixture glossary injection first reproduced two expected fresh-path
  failures (Chinese/Korean), both due to missing `final/glossary.json`.
- After the one-line production fix, the fresh-path and readiness smoke checks
  passed all four cases. These overlap the complete suite and are not added to
  its total.
- The complete affected `test_publication_readiness.py` and
  `test_build_publication_epub.py` suites passed **316 tests, 1 expected skip**
  in 909.23 seconds. The skip is the unchanged private installed-corpus opt-in.
- Compilation passed for the production module and both changed test modules.
- The separate seven-case metadata selection recorded **6 passed, 1 failed**,
  solely at `test_refreshed_schedule_weights_bind_complete_current_sources` as
  explained above. This correction does not claim all CI checks pass.

Raw console/JUnit artifacts, compilation output, source fingerprints and copies
of the prior integration JUnit files are retained under system TEMP at
`nt-aut2-d1-glossary-fix-20261028/`. Final commit/tree and inventory-guard results
are recorded there separately to avoid self-referential commit identities.
No private novel, credential, paid provider, GitHub issue or push operation was
used. The original D1 branch remains at `c10f0b9aa13abc133c2b4fc7da560fa64506becf`.
Hermes independent review and hosted timing were outstanding at that commit.

## Final D1/D1.1 CI qualification

Hermes accepted BLOCKER-1 and independently confirmed fresh regeneration and
CURRENT recognition before this qualification repair. The readiness fixture now
receives the exact workspace from the real regeneration call at its existing
pre-EPUB interruption boundary, instead of repeating a complete `_inputs()` proof
solely to calculate that workspace's path. Every test still generates its own
isolated project. Shared generation copies would require rebinding absolute
paths in normalization application reports; removing the redundant proof avoids
that extra fixture machinery. No production validator, readiness gate, glossary
snapshot, negative test, or fresh-regeneration assertion is replaced or bypassed.

One complete Windows Python 3.14.4 zero-provider measurement retained the exact
same 317 case identities as the previous corrected run: **316 passed, 1 expected
private opt-in skip**, in 941.05 seconds (JUnit suite: 940.973 seconds).

| Module | Previous active weight | Previous unoptimized case sum | Fresh case sum | New weight |
| --- | ---: | ---: | ---: | ---: |
| Readiness | 505.011 s | 522.523 s | 535.119 s | 535.499 s |
| EPUB | 386.943 s | 386.315 s | 405.474 s | 405.854 s |

The complete measured 0.380-second suite overhead is included in EACH new
module weight. The EPUB weight is also bounded below by its prior active weight.
This run did not demonstrate a runtime gain from the smaller fixture setup;
there is no timing discount or estimate substituted for these actual results.
Prior measurement records, original hosted provenance and all other module
weights remain intact. The exact current module hashes and new JUnit are bound
by two appended measurement records.

The unchanged three-shard measured-LPT allocator gives:

| Shard | Nominal seconds | 25% plus 60 s | 30% plus 60 s | Deadline margin |
| --- | ---: | ---: | ---: | ---: |
| 0 | 3431.8185225 | 4349.773 | 4521.364 | -21.364 s |
| 1 | 3431.8134749 | 4349.767 | 4521.358 | -21.358 s |
| 2 | 3431.8142737 | 4349.768 | 4521.359 | -21.359 s |

The relevant metadata selection records **6 passed, 1 failed**. Exact source
freshness now passes; the sole failure is the retained assertion that maximum
30% sensitivity must be below the unchanged 4,500-second deadline. Compilation
passes for the readiness, EPUB and CI metadata test modules. Public
classification, all 15 mandatory exclusions, factors and assertions are
unchanged apart from numerical measurement/allocation pins.

The three-shard candidate remained blocked and **uncommitted**. Its starting HEAD
was `37b4e1b1c8b5a5ace273266989b73e432b4549c9`, tree
`419c4be2918810928a1c5a75f1594c63ca84ae59`. The smallest next fixture candidate
is a real module-scoped baseline at a fixed path, restored as independent bytes
for each sequential test; that keeps absolute report paths valid without
fabricating evidence. It needs separate fixture work and guarded remeasurement.
At least 49.293 aggregate nominal seconds must be removed before balancing
effects; repartitioning alone cannot remove an aggregate overload. That attempt introduced no fourth
shard, relaxed deadline, changed factor or coverage reduction.

The sole fresh publication measurement, metadata JUnit/console output,
pre-run source hashes, old schedule, projection calculations and publication
guard result are retained under system TEMP at
`nt-aut2-d1-ci-qualification-20261028-a/`. No private novel, paid provider,
credential, GitHub write or push was used.

## Authorized four-shard L3 qualification

The user authorized four public Windows L3 shards after the preceding truthful
three-shard capacity failure. The starting staged tree was
`cfe62d7fc031583a2963e73b0ad72dc0e4780d97` at committed HEAD
`37b4e1b1c8b5a5ace273266989b73e432b4549c9`. All staged work and measurements are
preserved. No further publication fixture optimization was made.

The existing measured-LPT allocator now has four buckets. The qualification
selector and runner accept four for L3 and retain one for L1/L2; the workflow
matrix is 0–3. Exact source/run-bound manifests and gate artifact reconciliation
continue to require every selected shard, including shard 3. Obsolete three-shard
requests and missing evidence for any of the four shards fail closed.

The retained fresh publication JUnit still matches BOTH exact staged test-module
hashes and its recorded evidence hashes: **316 passed, 1 expected private opt-in
skip**, with all 317 case identities retained. Those cases were not rerun. Active
weights remain 535.499 seconds for readiness and 405.854 seconds for EPUB,
including the previously measured overhead. Every other weight, original hosted
measurement, historical record, classification and mandatory deselection remains
unchanged.

| Shard | Modules | Nominal seconds | 25% plus 60 s | 30% plus 60 s | Deadline margin |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 1 | 2751.5100000 | 3499.388 | 3636.963 | 863.037 s |
| 1 | 66 | 2514.6485398 | 3203.311 | 3329.043 | 1170.957 s |
| 2 | 66 | 2514.6471875 | 3203.309 | 3329.041 | 1170.959 s |
| 3 | 65 | 2514.6405438 | 3203.301 | 3329.033 | 1170.967 s |

The 4,500-second execution deadline, variability factors and 60-second allowance
are unchanged. These local projections clear the deadline; they are not hosted
runtime qualification. Final source/commit/tree identities, reused functional
proof and fresh CI reliability/guard evidence are retained under system TEMP at
`nt-aut2-d1-four-shards-20261028-a/`. No private novels, paid providers, GitHub
writes or push are authorized by this correction.

Fresh guarded Windows Python 3.14.4 CI reliability validation passed **283 tests,
zero skips**, in 105.43 seconds. This includes four-shard partition/source-binding
pins, all 15 unchanged deselections, rejection of obsolete three-shard requests,
real temporary-Git L1/L2/L3 decision → guarded runner → gate paths, and rejection
of missing evidence for each required shard. L1/L2 remain single-shard.
Compilation passed for both CI scripts and both changed test modules. Functional
publication results remain the retained **316 passed, 1 expected skip**; they are
reported separately and were not rerun. The prior three-shard capacity blocker
is resolved locally; hosted Windows timing remains unverified pending independent
review and a separately authorized push.
