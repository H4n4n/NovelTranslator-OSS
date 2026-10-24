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
`tests/public_test_classification.json`; 26 tests require an omitted novel-owned
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
