# AUT-2 A+B+C integration with verified GOAT main

Local engineering handoff, 2026-10-25 (America/Lima). Independent focused Hermes
review remains required. No hosted qualification, push, PR update, production
activation, private-novel access, provider credential access or real provider call
was performed. Issue #18 regeneration and L4 remain separate.

## Immutable identities and ancestry

The existing `D:/NTOSS_AUT2_C_Lineage_Codex` checkout was clean at entry and was
reused; no additional worktree was created. It has no repository-root
`data/progress.json`, `output/pending/` or `.env`. No private novel was opened.
All state used by tests was disposable synthetic state in external system TEMP.

- Accepted C: `67c7786d7c75b350d569f858c799c01ea37f5496`.
- Accepted C tree: `6f49895448ee0e83ed9625d2c5ac0e5e0b1fa840`.
- Accepted C parent (B): `cb0d1d9773a7059791dae58e0928f6dd65b21648`.
- A: `67c60bdb7c67b63b69157dba3acdaa75aa70f78a`.
- Public main fetched with credential helpers disabled, exactly matching:
  `e5aca8b35c4de0129dc83a0e551aefba012e4c2c`.
- Integration branch: `codex/aut2-goat-main-integration`.
- Normal non-rewriting merge: `8e62222238df61d8c31017e3414e742597f4a118`.
- Merge tree: `a539099d6ffb8966dbadfc6db349b6ff5ffde1f7`.
- First merge parent: `67c7786d7c75b350d569f858c799c01ea37f5496`.
- Second merge parent: `e5aca8b35c4de0129dc83a0e551aefba012e4c2c`.

A, B, C and verified main are all proven ancestors of the integration merge.
The original accepted branch and commits are preserved. No rebase, squash,
amend, force-push, reset or reviewed-commit replacement was used. Final candidate
commit/tree and exact per-file identities are recorded separately in the local
handoff evidence, avoiding a self-referential document hash.

## Conflict and ownership resolutions

The sole textual conflict was the final append block in `public_inventory.txt`.
The resolution retains C's `production_standalone_lineage.py` plus main's
`docs/GOAT_UNATTENDED.md`, `docs/GOAT_UNATTENDED_VALIDATION.md` and
`tests/test_goat_unattended.py`. The merge inventory is the exact union of both
parent inventories: 494 public files, with no dropped entries. This handoff adds
only its own documentation entry to that union.

`tests/test_full_pipeline_luna_runtime.py` merged automatically. Inspection
confirmed that B's configurable complete synthetic chapter library remains
alongside main's provider-neutral initialization expectations and explicit
provider parameterization. The experiment caller's outer novel lock continues
to enclose engine loading, initialization, recovery installation and lifecycle;
no nested novel lock was introduced.

Ten safety owners remain byte-identical to accepted C:

- `historical_reconciliation.py`, `production_standalone_lineage.py`;
- `translate_production_unattended_scheduled_retroactive_dynamic_pricing.py`;
- `production_run_lock.py`, `retroactive_transactions.py`,
  `retroactive_span_planning.py`;
- `production_periodic_audit_finalization.py`, `production_historical_lineage.py`;
- `full_pipeline_periodic_audit_recovery.py`, `full_pipeline_experiment_harness.py`.

Seven startup/runtime owners remain byte-identical to verified main:
`run_production_engine.py`, `translate.py`, `production_provider_runtime.py`,
`translation_model_config.py`, `production_goat_execution.py`,
`production_goat_runtime.py`, and `full_pipeline_luna_runtime.py`.
The publication guard, its allowlist and zero-provider runner are unchanged.

Default routing therefore retains main's GOAT V4.1 Flash selection and bounded,
one-use, state-bound authorization. Explicit direct DeepSeek Pro remains an
explicit provider/model pair. Provider/pending/accounting checks still precede
runtime activation and cleanup. GOAT initializes provider-neutrally, then enters
its reviewed activation seam under the caller-held novel lease. Direct startup
also retains its outer lease. The startup known-fix owner, durable prepared and
complete witnesses, audit ownership reservations, original-source inventory,
progress-last finalization and paid-pending recovery remain C's reviewed paths.
No history compatibility override or authorization relaxation was added.

## Fixture correction and route regressions

On the merged source before repair, the Chinese/Korean forms of all three named
publication tests produced **6 setup errors / 188 deselected**, exit 1:

- `aut2_self_consistent_old_editorial_is_not_current`;
- `aut2_conflicting_novel_lock_blocks_before_generation`;
- `aut2_newer_revision_invalidates_every_stage_without_rewriting_them`.

Every error originated in `writer_inventory` refusing the missing authentic
caller-held novel lease before the test assertion. The only publication change
is to the synthetic `novel` fixture: acquire `production_run_lock(project.root)`
around historical finalization and its consistency snapshot, then release it
before returning. Publication still acquires its own lock, and the competing-lock
assertion still runs. All six original assertions remain selected and unchanged.
No production writer, lease registry, authorization or publication check changed.

`tests/test_retroactive_transactions.py` adds a test-only route fixture to the
existing exact-source witness/readers/replay and twelve-point interrupted
standalone tests. They now execute for both default GOAT and explicit direct Pro;
the selected provider/model pair is asserted and any historical provider send
fails the test. Existing paid-pending and journal byte-preservation assertions
remain active. Competing ownership, malformed/legacy evidence, lost lease,
source drift and prior audit authorization negatives remain in the full focused
historical and recovery selections.


The bounded shared-GOAT finalizer selection also exposed three inherited negative
setup failures in `test_aut2_final_whole_history_and_publication_stale_are_independent_of_tail`
(`mixed`, `mixed_competing`, `mixed_contextual`). The fixture now creates a genuine
1-375 prior intent so the historical writer can safely finalize 376-400, while
that test still assumed the prior intent was absent. A complete proof therefore
correctly returned rather than raising the expected missing-history exception.
The correction changes only this disposable setup: first assert a read-only
CHECKED_WITH_DEFERRALS proof of all 400 chapters, remove the synthetic 1-375 intent, then retain
the original missing-history refusal. The damaged fixture must also trigger the
existing ORPHAN_FRESHNESS_MANIFEST publication refusal. Only its exact saved
intent bytes are restored before the original independent stale-publication
assertions run.
No production check or expected refusal was relaxed.

A single representative case is also probed from an untouched `git archive` of
accepted C. Its runtime identity requires Git metadata; a bare metadata directory
with read-only access to existing public objects supplies the exact accepted
commit/tree without registering an additional worktree or changing archive source.
The corrected-metadata accepted-C probe reproduced the original DID NOT RAISE
failure, exit 1 (86.88 seconds). It reached the exact original assertion; this is
inherited setup drift rather than a new production compatibility exception.

## Guarded Windows qualification

All pytest invocations used `C:/Python314/python.exe` (Python 3.14.4),
`tests/zero_provider_validation.py`, a fresh GUID-named external system-TEMP
basetemp, and explicit mandatory real-DPAPI deselection:

```text
tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip
```

The runner clears credential/live controls and blocks network, real DPAPI,
credential files and private production roots. Synthetic transports alone are
used. The DPAPI node is outside these focused selections, so its explicit
exclusion does not add a reported deselection to those module totals.

| Focused invocation | Passed | Failed | Errors | Skipped | Deselected | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full historical transaction/lifecycle modules, including both routes | 415 | 0 | 0 | 0 | 0 | 1109.39 |
| B recovery, Luna runtime, paid request journal, historical identity | 121 | 0 | 0 | 0 | 0 | 15.93 |
| GOAT unattended, CLI/model/launcher identity and paid-pending planner/executor | 128 | 0 | 0 | 0 | 0 | 79.75 |
| Final six publication cases | 6 | 0 | 0 | 0 | 188 | 20.57 |
| Initial bounded shared-finalizer selection (including inherited mismatch) | 2 | 3 | 0 | 0 | 0 | 479.99 |
| Final repaired shared-finalizer legacy variants | 3 | 0 | 0 | 0 | 0 | 217.73 |

The two unaffected shared-finalizer cases in the initial selection passed:
mixed old/preferred baseline exact-once finalization, and first-chapter crash
invalidation/recovery with progress-last publication. Only the three modified
legacy variants were rerun after correcting their setup. No failing case was
skipped or deselected. The six publication cases' 188 deselections are the
intentional focused `-k` selection, with all six required nodes selected.
The final effective selection contains 675 distinct passing cases, zero skips
and zero unresolved failures. Console logs and JUnit files retain every failing
and successful invocation.

The five files changed after the merge are this report, its inventory entry,
`tests/test_build_publication_epub.py`, `tests/test_retroactive_transactions.py`,
and `tests/test_goat_periodic_audit_finalization.py`. The merge's 46 imported
file changes and final per-file Git identities are listed in the external
`candidate_identity.json`.

Local raw evidence is retained under system TEMP in
`aut2-integration-handoff-20261025-8e622222/`: complete console/JUnit results,
`test_commands.json`, `test_results.json`, source/candidate identities,
`verification.txt` and SHA-256 checksums. Final commit/tree hashes live there
because this tracked report cannot contain its own final hash.

Every integrated Python change was checked with `python -m py_compile` (40
files, exit 0). The committed-tree and staged publication guards, whitespace,
exact source identities, inventory union, ancestor checks and final clean status
are recorded with the final candidate in the external handoff evidence.

Development command errors are retained here: the first newline-sensitive fixture
edit assertion aborted before changing a file; that invocation consequently
reproduced the same six setup errors. The corrected newline-preserving edit
passed. An initial core-suite JUnit argument was split by PowerShell, giving
pytest exit 4 with no tests collected; the corrected invocation uses separate
`--junitxml` and path arguments. Neither run is counted as a successful result.
The first accepted-C archive probe stopped at runtime identity because a plain
archive has no Git metadata (1 failure before the target assertion). Bare identity
metadata fixed that harness limitation; its alternates file required an LF-only
rewrite after Git rejected the initial Windows CRLF form. These setup diagnostics
are separate from the reproduced or repaired test assertions. The first
strengthened positive test expected CHECKED and failed all three variants because
the correct conservative status is CHECKED_WITH_DEFERRALS (243.69 seconds).
The assertion now requires that exact deferred status and all 400 examined
chapters before removing the prior intent. The original refusal assertions are
unchanged. That development rerun then failed three times at the existing
ORPHAN_FRESHNESS_MANIFEST guard (223.22 seconds): the removed intent left its
receipt behind. The final setup explicitly asserts that stronger refusal, then
restores the saved fixture bytes before the independent stale-output checks.

## Independent focused review commands

From the integration checkout, each invocation must choose a fresh external TEMP
child. For example:

```powershell
$reviewTemp = Join-Path ([System.IO.Path]::GetTempPath()) ('aut2-integration-review-' + [guid]::NewGuid().ToString('N'))
python tests/zero_provider_validation.py --basetemp $reviewTemp `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_retroactive_transactions.py tests/test_production_lifecycle.py
```

The local handoff stores complete test commands, basetemps, console/JUnit logs,
per-module results, source/file hashes and final status. Scope is local synthetic
integration evidence; it does not qualify paid execution, translation quality,
production historical activation, Issue #18 completion or L4.
