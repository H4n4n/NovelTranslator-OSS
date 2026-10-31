# AUT-2 source-bound local publication generations

This is a bounded downstream slice of Issue #11, not completion of all AUT-2.
Public baseline: commit `654639e6b565dfe0b8b17232af4b6b0c9fbbf229`, tree
`a4d38b5cc3aa411989bb6916a265a09f8294dc93`. The original local `main` was
still the initial public export; it was left unchanged. Development uses a new
worktree based on the verified remote commit. No production project is present
in that checkout; no private progress or pending state was inspected.

## Dependency and ownership map

| Dependency | Exact binding / validation | Existing owner |
| --- | --- | --- |
| Committed production | Complete 1..final inventory, filename/length/bytes framed hash; exact installed source units and original TXT hash; current mandatory local commit QA | `chapter_library`, `noveltranslator_core` |
| Accepted historical corrections | Immutable audit intent/checkpoint/outer record/history; BEFORE backups; exact original transaction plan, FINALIZED receipt/result and connected BEFORE/AFTER chain | `production_periodic_audit_finalization.transaction_proof`, `retroactive_transactions` |
| Whole-history decision | Existing `whole_history/<canonical-report-hash>.json`, CHECKED with zero deferrals; exact source/glossary/English hashes and transaction identities; explicitly selected by downstream approval | `historical_reconciliation.save_final_consistency` |
| Final corpus | New, byte-identical copy of the current complete committed inventory; manifest and per-file bytes independently compared | `build_finalization_corpus.build_identity_finalization_corpus` |
| Normalized corpus | Existing approved prose plan, exact baseline fingerprints, inventory when required, independently verified allowed spans; full reconstructed report semantics | `apply_normalization_plan` |
| Titles | Existing generic title applicator, exact normalized inventory and title anchors; explicit manual approval where required; unresolved decisions block | `apply_title_resolution_plan` |
| Presentation | Reconstructed transformations; body and explicit part markers preserved; exact cleanup report | `build_publication_chapters` |
| Editorial | Immutable publication checkpoint and original TXT/source anchors; approved edits/preservation guards; reconstructed plan, apply report and output bytes; current commit QA repeated | `final_editorial` |
| EPUB | Exact source-bound editorial apply report plus production ancestry; ZIP, OPF, spine, TOC/NCX, references, headings, rendered content, metadata and report validation | `build_publication_epub` |
| Publication | Durable generation intent, source provenance, validated EPUB and reports; novel-local writer lock; atomic current pointer last | `production_run_lock`, existing atomic JSON writer |

Existing final corpora and EPUBs keep their ownership and bytes. Outputs live at
`output/publication_generations/<input-proof-hash>/final/`. Only
`output/publication_current.json` is replaceable. Prior generations remain.
The generation identity binds every root Python module, selected profile files,
source/config/state/audit evidence, novel addendum/source overrides, whole-history
decision and stage approvals.
An interrupted generation resumes via the existing stage owners; completed
stages are independently verified before reuse. The EPUB owner reconciles its
own exact interrupted report/temp prefix. A completed EPUB without a current
pointer remains unpromoted; retry revalidates it and publishes the pointer.
Divergent residue is preserved and blocks.

`publication_freshness` reports captured downstream artifacts as STALE when
unchanged bytes predate a historical correction, MISSING when absent, and
NOT_VERIFIED when changed or unowned. Only the selected generation can become
CURRENT, after full source-to-EPUB revalidation. Old default EPUBs and unrelated
checkpoint EPUBs are never relabeled CURRENT. A new chapter/source/audit/approval
or engine revision invalidates a prior generation even if its editorial corpus
and EPUB remain internally self-consistent. The existing EPUB builder rechecks
ancestry immediately before replacement and at the end of verify-only.

## Explicit prerequisites and local command

Supported regeneration is deliberately limited to complete Chinese/Korean TXT
novels with unchanged production spines and existing approved downstream plans.
Run the existing whole-history owner first; this tool does not perform a new
semantic audit or resolve its deferrals. Review its exact report hash.

A read-only inspection emits the source snapshot hash and required approval paths:

```powershell
python publication_regeneration.py --novel <id> --inspect-whole-history <report-hash>
```

The operator-authored `config/publication_regeneration.json` must contain:

- `revision`: `source-bound-publication-v1`
- `novel_id`: exact selected novel ID
- `enabled`: `true`
- `finalization`: `committed-identity-v1`
- `allow_manual_approved`: explicit boolean
- `whole_history_sha256`: canonical hash of the existing checked report
- `source_snapshot_sha256`: canonical hash returned by inspection
- `normalization_plan_sha256`, `title_plan_sha256`,
  `editorial_authorization_sha256`: SHA-256 of exact approval file bytes

Approval files are fixed novel-owned paths:

- `config/publication_regeneration/normalization_plan.json`
- `config/publication_regeneration/title_plan.json`
- `config/publication_regeneration/normalization_inventory.json` when required
- `config/final_editorial.json`

These must be actual reviewed approvals for the CURRENT intermediate bytes.
Copying or rehashing an outdated plan never changes its embedded corpus/anchor
requirements. The existing title applicator is already generic; the fixed
Rogue title-plan generator is not used to fabricate new decisions.
The editorial owner retains its legacy `korean_source` field name for both
supported languages; the bytes are the selected novel's exact UTF-8 TXT.

Once those prerequisites are present, explicit local-only regeneration is:

```powershell
python publication_regeneration.py --novel <id>
```

The command acquires the existing novel writer lock before mutable reads. It
imports no paid executor and reads no provider journal, credential or benchmark
data. Failure diagnostics contain bounded reason codes/dependencies, never
source prose or expected English anchors. Inspection is a read-only snapshot,
not an authorization or reservation against external editors.

## Limits and bounded next design

- No automatic new title/editorial/normalization approval, glossary authority,
  semantic rereview or historical transaction engine is introduced.
- Source repartition, EPUB-source novels, partial-volume regeneration and
  checkpoint rebuilding are not supported by this slice. Existing checkpoint
  EPUB bytes remain preserved and unverified. These cases require a separately
  approved source-unit mapping and its proof through the complete downstream
  chain. The existing one-to-two repartition builder remains unchanged.
- Existing whole-history decisions must be CHECKED with no deferrals. This
  slice validates and explicitly binds that decision; it does not turn the
  prior local consistency pass into independent semantic rereview.
- Generations are retained; garbage collection and default-path replacement are
  outside scope. Readers must use the validated current pointer's generation.
- Publication relies on cooperative novel locks and existing atomic filesystem
  primitives, not protection from hostile external writers or a new power-loss
  durability guarantee.
- Early unprovable stage residue blocks; no broad cleanup is authorized. Exact
  interruption recovery stays owned by each existing stage protocol.

## Local validation and Command Code handoff

Use the exact local candidate in a separate public worktree, or an agreed
exclusive ownership window. Do not change either private novel/GOAT tree, use
credentials, make provider calls, weaken public classification, push or open a PR.
First record `git rev-parse HEAD 'HEAD^{tree}' 'HEAD^'` and clean `git status`.

```powershell
python -m py_compile publication_regeneration.py historical_reconciliation.py build_finalization_corpus.py build_publication_epub.py final_editorial.py tests/test_build_publication_epub.py
$Aut2Temp = Join-Path ([System.IO.Path]::GetTempPath()) ('ntoss-aut2-commandcode-' + [guid]::NewGuid().ToString('N'))
python tests/zero_provider_validation.py --basetemp $Aut2Temp --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- tests/test_build_publication_epub.py -k aut2 -q
```

Then run the relevant public integration modules with a DIFFERENT fresh external
basetemp and the same mandatory DPAPI deselection:

```powershell
$Aut2Temp = Join-Path ([System.IO.Path]::GetTempPath()) ('ntoss-aut2-integration-' + [guid]::NewGuid().ToString('N'))
python tests/zero_provider_validation.py --basetemp $Aut2Temp --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- tests/test_build_publication_epub.py tests/test_finalization_corpus.py tests/test_apply_title_resolution_plan.py tests/test_build_publication_chapters.py tests/test_apply_normalization_plan.py tests/test_retroactive_transactions.py tests/test_production_lifecycle.py tests/test_goat_periodic_audit_finalization.py -q
python scripts/publication_guard.py --tree HEAD
```

Do not edit root Python files while these tests run: the exact builder bytes
are intentionally part of each synthetic generation's identity.
`tests/test_final_editorial.py` is already classified PRIVATE in
`tests/ci_exclusions.json`; 26 tests require an omitted novel-owned
manifest. An exploratory guarded run correctly denied those reads (409 passed,
4 skipped, 26 blocked failures). Do not copy private inputs in to make it pass.
The new public tests exercise the real editorial planner/applier with disposable
source-backed approvals, including title and prose changes and preserved guards.

Independently audit: approval provenance (particularly rehashed old plans),
source/audit/BEFORE witness chain, stale-but-self-consistent editorial rejection,
strict JSON and path ownership, old pointer contradictions, existing stage
recovery, actual EPUB rendered content, novel lock contention/isolation, and
zero-provider guards. Return exact failing node IDs, traceback/repro and smallest
remediation. Qualification/Issue #11 completion still requires independent review
and the existing hosted policy after explicit owner publication authorization.

### Recorded local validation

Python 3.14.4 on Windows. Syntax checks and the staged publication guard pass.
The broad eight-module integration command above passed **578 tests**, with
**4 existing skips**, in 1578.85 seconds. This included the complete synthetic
GOAT audit-finalization module; its expensive 400-chapter cases ran to completion.

Final review then changed reporting metadata only: a missing prerequisite's
filename is retained in bounded diagnostics, and a stale selected generation's
EPUB path is included in the freshness inventory. The final candidate selection of `test_build_publication_epub.py`,
`test_retroactive_transactions.py` and `test_production_lifecycle.py` passed
**358 tests**, with **1 existing skip**, in 227.03 seconds using the same guarded
runner, fresh external temp and explicit DPAPI deselection. This includes all
**99 new AUT-2 cases** and the existing publication/history contracts.
The unchanged slow audit module is not rerun for those reporting-only additions.

The local handoff commit/tree/parent are reported in the chat after committing;
use those exact identities for independent validation. For a separate checkout:

```powershell
git -C D:\NovelTranslator-OSS worktree add --detach D:\NTOSS_AUT2_CommandCode <exact-candidate-sha-from-handoff>
Set-Location D:\NTOSS_AUT2_CommandCode
```

No branch was pushed, PR created, hosted CI triggered, private novel mutated or
real paid request executed as part of this local assignment.

## AUT-2 D2: historical recertification and publication preflight

This is the first D2 increment for Issue #18. Its approved main baseline is
`a887d0e34110f3adb8a5e74093f0429bd5929b8c`. Development uses the separate
`codex/aut2-d2-history-preflight` branch. D1/D1.1 publication authority and
pointer semantics remain owned by their existing validators.

### Local operations

```powershell
python publication_regeneration.py --novel <id> --report
python publication_regeneration.py --novel <id> --recertify-history
```

`--report` reads evidence without creating files, temporary corpora, or even a
production lock rendezvous. It reports:

| Status | Meaning |
| --- | --- |
| CURRENT | The existing CURRENT verifier independently proves the selected generation, EPUB, and exact D1 review. |
| STALE | A generation or dependency has changed and must be rebuilt/revalidated. |
| REVIEW_REQUIRED | A historical report, source binding, stage approval, or graph review needs explicit selection, review, or reissuance. |
| BLOCKED | Evidence is malformed, contradictory, ambiguous, unsupported, or otherwise unprovable. |

BLOCKED takes precedence over review and stale conditions. Diagnostics cover the
selected hash-named whole-history report, source snapshot authorization,
normalization plan, title plan, editorial authorization, input proof, and D1
publication review. Paths are relative to the selected novel. Diagnostics emit
only bounded codes, paths, hashes, counts, and validation statuses. They never
serialize source text, chapter bodies, plans, private anchors, or exception text.

`VERIFIED` on a diagnostic is local evidence for that dependency, not permission
to publish. Approval byte binding and stage validation are reported separately.
When an intermediate corpus does not exist, its exact path is reported and its
stage validation remains unperformed. Existing stage owners validate available
intermediates. `_inputs()` remains the input authority, and `current_generation()`
remains the only CURRENT authority. Neither diagnostic output nor a hash rebase
can satisfy the publication gate.

`--recertify-history` calls
`historical_reconciliation.save_final_consistency()` using
`goat_periodic_audit_finalize_plan.local_module(project)` under the existing
novel production lock. It does not initialize production runtime or activate a
provider. The local loader retains its provider-send and initializer tripwires.
It requires a complete supported TXT identity spine and exact original source
hashes; the history owner independently rechecks audit decisions, transactions,
BEFORE backups, state, and chapter bytes.

The only novel data it may create is the existing deterministic immutable file
`output/audits/whole_history/<canonical-report-sha256>.json`. The existing empty
`.production.lock` rendezvous may be created by the lock owner. Exact replay
preserves report bytes and modification time. It never writes committed chapters,
glossary, approvals, generations, or CURRENT. The CLI returns the canonical report
hash, path, bounded history status/counts, zero provider calls, and
`publication_authority: false`. Even CHECKED reports require human review;
deferrals always return REVIEW_REQUIRED. This does not perform semantic rereview.

All operations are mutually exclusive with the existing read-only
`--inspect-whole-history <hash>` snapshot operation. The default regeneration
command is unchanged. New operations return exit 1 for BLOCKED and exit 0 for a
completed advisory report or successfully saved history report. Exit 0 does not
mean publication-ready.

### Review sequence and boundaries

1. Run read-only preflight and preserve its precise prerequisite paths.
2. Recertify complete historical evidence locally when necessary.
3. Review the returned report and resolve deferrals through the existing owners.
4. Explicitly review/reissue source and stage approvals against the exact new
   bytes; the CLI never copies, rebases, regenerates, or signs approvals.
5. Existing regeneration, stage owners, and the D1 graph-review gate independently
   decide whether a generation can be built and promoted.

Standalone lineage may be valid historical evidence while still unsupported by
regeneration's periodic-only source snapshot. Preflight explicitly blocks that
route as `UNSUPPORTED_STANDALONE_REGENERATION`. If the existing history owner can
save a valid standalone report, recertification still reports that publication
limitation as BLOCKED. Malformed standalone evidence remains unprovable.

Supported publication remains limited to complete Chinese/Korean TXT novels
with unchanged identity spines. Other source layouts, partial checkpoints,
standalone certification, J5/J8, and automatic human-approval reissuance remain
outside this increment. No EPUB builder, stage owner, invalidation schema,
provider journal, credential policy, pointer protocol, or CI safety rule changes.

### Synthetic validation and handoff

D2 cases extend the existing public `test_publication_readiness.py` module and
reuse its Chinese/Korean disposable novels. They exercise independently verified
CURRENT reporting, stale runtime/history bindings, exact missing approval paths,
a newly discovered identity's correction into earlier prose, immutable report
creation/replay, historical deferrals, unsupported and malformed evidence,
source/checkpoint/backup/transaction drift, writer exclusion, prose redaction,
and whole-novel byte/mtime preservation. No real novel is opened.

The complete changed module must be measured with `tests/zero_provider_validation.py`,
a fresh external GUID-named basetemp, mandatory real-DPAPI deselection, and retained
JUnit/source evidence. The existing measured-LPT schedule must receive the exact
current module source hash and measured duration. Classification, exclusions,
mandatory deselections, allocator, and execution deadline remain unchanged.

Validation results and exact local HEAD/tree are recorded in the local handoff.
Independent Hermes review is the next gate. The recommended next implementation
increment is a bounded approval-reissuance workflow that presents exact changed
dependencies for human review, then resumes the existing regeneration owners;
standalone source-snapshot certification requires its own separate proof adapter.
This increment does not complete Issue #18 or authorize production publication.

The D2 boundary additionally parses mutable state, audit JSON, and transaction
JSON through the existing strict UTF-8/unique-key reader before loading the
compatibility module. Boolean progress values, duplicate fields, UTF-16/32 JSON,
and malformed periodic transaction identities block without a report write.
This parsing guard does not replace any historical transaction validator.

### Local validation on 2026-10-29

All tests used Python 3.14.4 on Windows through the existing zero-provider
validation wrapper, mandatory real-DPAPI deselection, and separate GUID-named
external basetemps. No tests shared a temporary root or ran concurrently.

- Final complete readiness module after strict JSON hardening: **201 passed,
  zero skips**, 639.08 seconds. This includes all 123 inherited D1/D1.1 cases
  and 78 D2 cases.
- The unchanged EPUB module passed **193 tests with one existing private-corpus
  opt-in skip** in the earlier complete combined publication run (384 passed,
  one skipped, 1017.15 seconds). The subsequent patch changed only the new D2
  input boundary and its tests; final readiness re-exercised the real existing
  EPUB/readiness fixtures against that boundary.
- Supporting CI, normalization, titles, presentation, historical transactions,
  lifecycle and import-safety modules: **839 passed, three existing optional
  installed-corpus skips**, 839.74 seconds. No new exclusion was introduced.
- Dedicated ambiguity refusals after the final helper correction: **8 passed**.
- Compilation of all three changed Python files passed.

The final readiness weight is 639.014 seconds (638.790 seconds of JUnit case
time plus all 0.224 seconds of suite overhead). The EPUB weight retains the
higher prior 405.854 seconds. All original timing provenance is retained;
current records append complete, hash-bound local evidence. The same 198 public
modules, two private classifications, seven historical classifications, 15
mandatory deselections, four-shard measured-LPT allocator, sensitivity allowance
and 4500-second execution deadline remain in force. The largest sensitivity
projection remains 3636.963 seconds. These are local projections, not hosted L3.

Retained local evidence is under
`C:\Users\User\AppData\Local\Temp\nt-aut2-d2-evidence-2c3c3db1d5bd4819966b8eddf1d60d93`.
The final module's JUnit, console, runtime/test source hashes and measurement
summary are in `final-readiness/`. Earlier EPUB/supporting evidence and the
final CI integrity output are in the parent directory. Exact final HEAD/tree,
publication guards and the independent-review command are recorded in the
local `HERMES_HANDOFF.md` after commit. No hosted qualification or new L4
qualification is claimed.

Final CI integrity revalidation after the last measurement/pin refresh:
**283 passed, zero skips**, 102.82 seconds. The active projection vector is
`[2751.51, 2549.1534623, 2549.152223, 2549.1455858]`; all four buckets remain
below the unchanged deadline with the original sensitivity allowance.


## D2 Increment 2: bounded approval-review handoff

The existing `--report` output now enriches its dependency diagnostics with
`authorized_sha256`, `previously_authorized_sha256`, current `sha256`,
`approval_bytes_valid`, `decision_binding`, `decision_reissuance_required` and
`semantic_review`. A null value means unavailable or not independently proved;
it never means approval. `VALID` means the existing stage owner verified the
binding, with semantic review still `NOT_PERFORMED` by this advisory command.
The stage row's original status/reason and `stage_validation` remain available.
The CURRENT, STALE, REVIEW_REQUIRED and BLOCKED authorities are unchanged.

`approval_review.source_changes` compares the current verified source snapshot
with the pointer-selected generation's immutable source evidence only after
checking the prior pointer, intent identity, matching source provenance, EPUB
archive/reports and recorded committed corpus fingerprint. It emits bounded
owner-defined project-relative paths and chapter numbers, counts and an explicit
truncation flag (at most 256 paths and 256 chapters). An unavailable comparison
has null change lists and one of `NO_PREVIOUS_GENERATION`,
`CURRENT_SOURCE_EVIDENCE_UNAVAILABLE` or `PREVIOUS_GENERATION_EVIDENCE_INVALID`.
Unknown historical path formats are unprovable by this comparison. The report
never serializes approval contents, private anchors, source/prose or validator
exception text. Even error paths create no lock, report, corpus or approval.

Approval-byte equality is independent of embedded decision validity. In
particular, an unchanged normalization plan may still fail against corrected
committed prose. Title/editorial decisions require their own exact intermediate
inputs: when those inputs are unavailable, the report requires explicit review
and leaves reissuance unproved until their owner can validate the actual corpus.
If committed bytes are unchanged, the real stage validators can independently
prove existing approvals against retained intermediates despite unrelated source
evidence drift. Such a valid binding does not demand new approval bytes merely
because another hash changed; the source authorization and D1 graph review still
need their own checks. No tool rebases fingerprints or makes human decisions.

### Practical operator sequence

1. **Preflight — deterministic verification.** Run
   `python publication_regeneration.py --novel <id> --report`. Read every bounded
   reason and the dependency-ordered `required_operator_actions`. Preserve the
   selected pointer and old generation. A report exit code of zero is advisory
   and does not authorize publication.
2. **History recertification — immutable local evidence creation.** Run
   `python publication_regeneration.py --novel <id> --recertify-history` when
   required. The existing history writer creates its hash-named report. Resolve
   deferrals through the existing owners, and rerun preflight with the selected
   current history report using the Python `publication_preflight(project,
   report_sha256)` interface, or after explicit selection in the authorization.
   CHECKED is deterministic evidence, not human semantic approval.
3. **Explicit human decisions — semantic review.** Review the correction and
   current history, continuity, issues and final glossary. Review normalization,
   titles and editorial choices against their exact input corpora. Decide which
   decisions to preserve and which to reissue. An unknown downstream input
   requires review once it exists; it is not evidence that an old decision fits.
4. **Approval reissuance — human-authored authorization.** As applicable, reissue
   `config/publication_regeneration/normalization_plan.json`,
   `config/publication_regeneration/title_plan.json`, `config/final_editorial.json`
   and `config/publication_regeneration.json` using their existing formats.
   Update complete source/corpus/anchor requirements following explicit review.
   Merely updating authorization hashes or aggregate fingerprints cannot repair
   invalid per-file or anchor bindings. No automatic approval command exists.
5. **Regeneration — existing stage owners.** Run
   `python publication_regeneration.py --novel <id>`. A fresh candidate may stop
   at the D1 readiness gate after its downstream corpora and evidence exist.
   Keep that candidate for resume. Resolve any earlier stage binding failure
   through its owner; do not overwrite or remove unproven residue.
6. **New dependency-graph review — human semantic review.** Use
   `publication_readiness.dependency_graph(PublicationWorkspace(project,
   actual_candidate_final_dir))` on the actual completed candidate. Do not
   predict its graph before its stage reports and corpora exist. Review the
   exact returned graph and explicitly reissue `config/publication_readiness.json`
   with its graph hash, reviewer and all existing required semantic decisions.
   The old readiness review cannot authorize this changed graph.
7. **Resume and certify — publication certification.** Run the same regeneration
   command. The existing engine revalidates completed stages, D1 readiness and
   the EPUB before atomically updating CURRENT. Verify with `--report`; use the
   pointer-selected EPUB. The previous generation and EPUB remain immutable.

The synthetic tests are the only place that simulate human decisions. They use
real historical, normalization, title, presentation, editorial, EPUB and D1
validators, and inspect the resulting XHTML for the corrected Chapter 1 body.
They also interrupt after actual title publication and resume without replacing
the prior CURRENT pointer until certification succeeds. This adds no standalone
lineage adapter, J5/J8 support, EPUB-origin novels, partial spine, paid workflow,
or new publication authority. Hosted L3 and L4 remain independent qualification
gates; the PR #21 L4 exception does not apply to this increment.


The prior-pointer verifier now shares the handoff's well-formed-source and
retained-corpus check. This closes a specific defect: a self-hashed intent and
matching provenance could previously describe an incorrect committed-corpus
fingerprint while the archived editorial EPUB still passed its own integrity
checks. Such contradictory prior source evidence now blocks regeneration before
any generation write or pointer promotion. Focused public cases use self-hashed
malformed source metadata and a well-shaped but incorrect corpus hash, and
verify pointer and generation byte/mtime preservation. The pointer schema,
publication gate, writer ownership and promotion order are unchanged.


### Increment 2 local validation and handoff

The complete final readiness module passed **239 tests**, zero skips, in
941.16 seconds, with all final runtime hardening present. The original test file
prefix is byte-identical to verified main; all 37 original functions/helpers
have identical ASTs. The 38 added cases use public disposable Chinese/Korean
fixtures. UTF-8 assertions and documentation are preserved explicitly.

The final source-bound measurement is **941.069 seconds** (JUnit case time plus
all suite overhead). The active weight remains the higher prior **975.594
seconds**, without a speed discount. Original timing provenance remains intact;
the new complete record binds the exact current test bytes and captured runtime.
Four independent numeric-type probes also proved that malformed boolean/float
corpus metadata accepted by Python mapping equality is refused by canonical JSON
identity, without changing any project evidence.

Both qualified synthetic EPUBs were independently reopened with the standard
library. Corrected Chapter 1 body content, old archive wording, new pointer/archive
hashes and the complete old generation inventory all passed. The existing engine
resumed after an actual title-owner interruption and required fresh explicit D1
review of the completed candidate graph before publishing CURRENT.

Other focused results: **99 EPUB cases passed** (95 cases outside the selected
AUT-2 focus), **609 stage-owner tests passed with three existing optional corpus
skips**, and **284 final CI integrity tests passed** in 106.17 seconds. No new skip
or exclusion was added. Each suite used a fresh external GUID-named basetemp,
the zero-provider wrapper and mandatory real-DPAPI deselection. Captured sources
remained unchanged throughout each run. All changed Python files compile.

The same 198 public, two private and seven historical module classifications,
15 mandatory deselections, four-shard measured-LPT allocator, 25%/30% allowances
and 4,500-second execution deadline remain. The largest sensitivity projection
is **3,636.963 seconds**. Mechanical CI assertions pin the exact measured weights
and resulting partitions; none of their validation logic was removed. No hosted
L3 or L4 qualification is claimed, and PR #21's L4 exception does not extend here.

Hermes evidence is retained at
`C:\Users\User\AppData\Local\Temp\nt-aut2-d2i2-evidence-3513096913b048fcbda212605bbcfe8e`.
Use `validation-summary.json`, `measurement-summary.json`,
`measured-readiness-utf8-final/`, `focused-epub/`, `stage-owners/`,
`qualified-ci-integrity/`, `original-test-preservation.json`,
`numeric-type-probe-before.json` / `numeric-type-probe-after.json`,
`independent-qualified-epub-verification.json`, and `synthetic-recovery-qualified/`.
Earlier exploratory, interrupted and pre-restoration runs are retained as history;
they are not the final qualification evidence. The local `HERMES_HANDOFF.md`
records the final commit/tree/parent, guards, exact commands and review boundaries.
No push, PR, merge, paid call, private novel access or frozen GOAT evidence change
is authorized by this local increment.
