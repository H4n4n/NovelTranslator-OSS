# Public CI reliability

L3 retains all three mandatory PUBLIC Windows Python 3.14.4 shards. Private and
historical modules remain excluded, not passed. The zero-provider wrapper,
explicit DPAPI deselection, external temporary roots, publication history gate,
credential exclusion and read-only workflow permissions are retained.

## Original hosted scheduling evidence

The current complete allocation is documented in [PR 15 public CI repartition](AUT2_PUBLIC_CI_REPARTITION.md): 197 public modules, source-bound local refreshes, and conservative per-shard budgets. The tables and projections below retain the original hosted scheduling evidence; they are not current shard estimates. The runner algorithm, three shards and 4500-second execution deadline remain unchanged.

The version-2 `tests/public_shard_schedule.json` uses complete per-module
collection + setup/call/teardown seconds from successful hosted run
[39696534289, attempt 1](https://github.com/H4n4n/NovelTranslator-OSS/actions/runs/39696534289).
Its tested merge commit was `ff7e0975dcf172038b2376e3b3c5730b37cfff56`, tree
`e80c1515fdf8d621dc02848de141f7d053609a6a`. That tree equals verified main
`2776ab2c4f106cdd0d2f295576df6e8f13849772`; the PR head SHA alone is not the
tested merge identity. The schedule retains only bounded public metadata:
per-module costs, artifact-file hashes, exact source/run identity, old shard
summaries and the proposed allocation. Original downloaded artifacts and
historical benchmark evidence are untouched.

Before using the measurements, all three `selection.json` files were reconciled
against that baseline's source hashes (allowing Windows LF/CRLF conversion),
classification and frozen schedule. `events.jsonl` was replayed through the bounded
receiver and compared with `collected.json` and `timings.json`; terminal success,
all node phases, exactly-once membership, exclusions and deselections passed.
The run reports 5,327 passing tests, 12 skips and 15 reviewed deselections.

| Shard | Old session seconds | Measured module seconds | Unattributed session seconds | New projected module seconds | New modules |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 428.647 | 425.177 | 3.470 | 1541.852 | 61 |
| 1 | 3631.409 | 3627.432 | 3.977 | 1541.851 | 64 |
| 2 | 576.966 | 572.938 | 4.029 | 1541.844 | 66 |

Shard 1's largest measured costs were:

| Module (`tests/`) | Collection + phase seconds | New shard |
| --- | ---: | ---: |
| `test_goat_periodic_audit_finalization.py` | 1433.518 | 0 |
| `test_completed_uncertain_history.py` | 1131.631 | 1 |
| `test_production_goat_batch_journal.py` | 369.178 | 2 |
| `test_production_goat_batch_ledger.py` | 281.030 | 2 |
| `test_experimental_goat.py` | 278.197 | 2 |

Allocation sorts modules by descending measured cost, with path ties, then puts
each module in the least-loaded bucket, with bucket-index ties. The selector
recomputes that allocation and rejects missing/unknown/invalid weights, stale
measurement identity or altered membership/order. It never measures file sizes.
Regression assertions pin the measured-weight digest and reviewed partitions.
All 191 public modules, 2 private / 7 historical exclusions and all 15 node
deselections are preserved. No test body, workload or safety guard is shortened.

The projected maximum is **25m42s**, plus unallocated session overhead (about
four seconds in the old run), versus the measured old **60m31s** session. That is
about a **57.5% reduction** under the same-cost assumption, not a measured new
hosted runtime. Costs come from one Windows run. Runner variation, import caching,
fixture ownership and changed order can alter them; module collection costs can
include imports first encountered there. The expanded synthetic CI controls also
add some work to their module. Dependency installation, job queue/setup and the
final gate are outside these session projections. No independence or repeat-run
variance bound is claimed. One hosted full L3 on the final candidate merge tree
is required before accepting this projection; independent review owns that check.

The test process has a 4,500-second (75-minute) wall-clock deadline. The finite
90-minute job cap allows 15 minutes for setup, dependency installation, cleanup
and diagnostics. This gives headroom above the proven 60-minute cancellation,
but does not guarantee completion or bound future runtime variability. Timeout
returns 124; interrupted/incomplete collection or execution cannot report success.
Windows deadline cleanup terminates the test process and its descendants.

## Evidence

The selector writes `selection.json` before starting tests, containing actual
checkout SHA/tree/index tree, shard membership, exclusions, all reviewed node
deselections and SHA-256 hashes of selected source and selection infrastructure.
The checked-out Git identity may be a PR merge commit rather than the PR head;
`GITHUB_SHA` is also recorded. A head-only pass is not evidence for a different
merge tree. The selector requires clean tracked sources before execution and
verifies the same identity and cleanliness afterward, including failure paths.
CI repeats the cleanliness check with `if: always()`.

The pytest telemetry plugin emits sanitized, flushed records through a reporting
pipe duplicated before pytest descriptor capture. It does not open or write
any diagnostic files during test phases or session finalization. This preserves
import-safety tests that forbid file I/O until monkeypatch fixture cleanup.
Only the supervising process persists JSONL events, collected-node manifests
and module totals. The pipe is not a filesystem handle or a credential access
bypass; the zero-provider and import-safety protections remain unchanged.

The supervisor accepts only reviewed metadata fields, public module identities,
hashed parameter identities, finite durations and valid outcomes. Frames are
limited to 64 KiB, collected nodes to 20,000, and the event stream to 100,000
records / 64 MiB per run. Raw pytest output is forwarded to the console without
being retained in diagnostic artifacts. Malformed/oversized frames, missing
completion and receiver failures prevent a zero process exit from passing.
Reporting exceptions inside pytest are sticky failures, not hook exceptions
that interrupt fixture cleanup or obscure a primary assertion failure.
A 60-second parent heartbeat remains visible during a stalled import or test.
Parameter IDs are hashed; diagnostics do not retain captured output, fixtures,
tracebacks, credential values, or arbitrary local paths. Test names and public
module paths identify the last active phase. Skipped phase counts are retained;
no skip or deselection is counted as a pass. Collection must match the exact
module set and reviewed deselection list.

Only these metadata files under the external runner-temp diagnostic directory
are uploaded, with `if: always()` and 14-day retention. Normal inner timeouts
leave time for upload; a hard runner loss or outer cancellation may prevent
artifact upload. Flushed progress still remains in Actions logs when available.
Missing artifacts or a missing terminal result is incomplete evidence, never a
passing run. No job uses `continue-on-error`.

## Risk-based public qualification

`scripts/public_ci.py` and `scripts/public_ci_policy.json` implement a deliberately
small reviewed policy. Every run performs the publication/history, inventory,
private-path and credential guards. Each selected tier compiles Python and uses
the existing zero-provider wrapper, bounded supervisor and metadata receiver.
The default `run_public_tests.py` invocation still runs the complete public suite.

| Tier | Eligible delta | Required tests |
| --- | --- | --- |
| L1 | Only modifications to `docs/DEVELOPMENT_TIPS.md`, a navigation-only page | Public CI synthetic controls and runtime hardening smoke |
| L2 | Only modifications to the five reviewed paths below | The same smoke plus the union of their exact affected public test modules |
| L3 | Every other case | All 191 public modules in the measured three-shard partition, exactly once |

The impact map is deliberately confined to advisory, read-only calibration:

| Source path | Complete affected tests (`tests/`) |
| --- | --- |
| `source_language_filter.py` (existing) | `test_audit_semantic_completeness_all.py`, `test_audit_semantic_completeness_candidates.py`, `test_audit_semantic_kinship.py` |
| `semantic_kinship.py` | `test_semantic_kinship.py`, `test_audit_semantic_kinship.py` |
| `audit_semantic_kinship.py` | `test_audit_semantic_kinship.py` |
| `audit_semantic_completeness_all.py` | `test_audit_semantic_completeness_all.py` |
| `audit_semantic_completeness_candidates.py` | `test_audit_semantic_completeness_candidates.py` |

The three audit CLIs have no downstream source callers. Their tests call the
actual per-project and aggregate functions using synthetic projects and verify
protected state is unchanged. The source-language predicate has only those three
callers. The pure kinship detector has only the kinship audit as a source caller;
its direct tests exercise extraction, window/classification behavior and the
non-blocking result. Repository import/use inspection and a reverse-transitive
import-closure regression verify those complete source-to-test sets. These paths
support translation-quality calibration without changing commit QA policy.

The policy validator requires the exact hardcoded source set and each nonempty
complete test set. An AST-only check of each mapped source's reviewed import
footprint rejects new dependencies, relative/dynamic imports or unparseable
source to L3 without executing it. Changes to any dependency itself, including
`novel_project.py`, `chapter_library.py`, `semantic_integrity_audit.py`,
`semantic_completeness.py` or the underlying completeness audit, still take L3;
adding a mapped source to that delta cannot lower it. Every L2 selection includes
the invariant two smoke modules and every affected public module, deduplicated.
Every selected module must supply an actual passing call phase at the gate.

This is a bounded expansion from one to five source paths, not a claim that all
translation maintenance is low risk. Production QA, paid-request handling,
credentials, recovery, accounting, state authority, glossary mutation and
historical writers are intentionally outside L2. Imports and tests are review
evidence, not a sandbox proving arbitrary replacement Python is harmless.

Unknown or mixed paths, added/renamed/deleted/type-changed files, dependencies,
configuration, tests, infrastructure, credentials/security, publication policy,
CI policy and normative documentation (including README and AGENTS) take L3.
Pushes to main and manual runs also take L3. A missing, malformed, or changed
impact policy falls back to L3. Missing or stale test classification/scheduling
blocks qualification entirely; new tests cannot disappear from the public
partition. This PR itself takes L3. The classification, 2 private + 7 historical
module exclusions and 15 node deselections are unchanged; only the measured
allocation changes.

## Exact evidence and stable gate

The decision validates the PR merge commit's two parents against the event's
exact base and head. Its affected paths come from `git diff base tested-merge`,
not a head-only diff or API filename heuristic. Checkout SHA, tree, index tree,
head, base, run ID/attempt, policy blob SHA and canonical policy SHA-256, reasons,
selected modules, exclusions and deselections are retained. Dirty sources or
conflicting identities fail closed. Both execution and the gate independently
recompute the decision; a changed merge tree needs new qualification.

`Public CI gate` is an always-present `if: always()` job. It requires the guard
and exactly the selected tier's jobs to succeed. Only unselected tiers may be
skipped. Missing, skipped-required, cancelled, failed, pending or unknown results
cannot pass. Artifact names bind the exact workflow run and attempt; partial
reruns cannot combine old-attempt evidence with new evidence. Rerun the complete
workflow attempt if qualification needs to be retried.

The gate reconstructs every expected selection manifest, checks source hashes
and exact partition/deselection coverage, and replays the bounded telemetry
receiver against the original event stream. It compares the replayed collection
and terminal timing records with the uploaded files, requires a successful
terminal result and complete node phase evidence, and rejects missing, truncated,
altered, empty or duplicate coverage. L1/L2 also require actual passing call
coverage in every mapped/smoke module. L3 preserves legitimate test skips and
reports them as skips; neither skips nor deselections count as passes.

A read-only GitHub API snapshot must still match the open PR's head, base and
tested merge commit when the gate runs. A superseded PR run can be cancelled;
main/manual runs do not cancel one another. The check certifies its recorded
snapshot, not future base changes. Future protection must require up-to-date
qualification for the merge tree; this PR enables no branch protection or ruleset.
An old green head check alone must never authorize a different merge tree.

Source/workflow changes remain a trusted-review boundary: artifact replay detects
incomplete or inconsistent evidence, not an adversary rewriting both workflow
code and all evidence consistently. GitHub-hosted artifact origin, read-only
permissions, source binding and independent review supply the other boundaries.
The gate has no deployment, merge, provider or policy-administration authority.

## Focused development and separate L4

For local feedback, compile changed Python files, then invoke the existing guard
on the affected tests (choose a fresh external temp child):

```powershell
python -m py_compile scripts/public_ci.py scripts/run_public_tests.py tests/test_public_ci_reliability.py
python tests/zero_provider_validation.py --basetemp "$env:TEMP/nt-focused-unique" --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q -p no:cacheprovider tests/test_public_ci_reliability.py tests/test_runtime_hardening_smoke.py tests/test_semantic_kinship.py tests/test_audit_semantic_kinship.py tests/test_audit_semantic_completeness_all.py tests/test_audit_semantic_completeness_candidates.py
```

Synthetic tests cover tier happy paths, risky/unknown/mixed/rename/delete fallback,
invalid policy, source/run/merge conflicts, cancelled/skipped required jobs,
tampered and incomplete evidence, exact public exclusions and partition coverage.
Real temporary Git repositories exercise all three tiers through decision,
guarded child execution and the gate, including base-only changes and merge-tree
drift. Existing complete-runner negative controls remain intact. These local
checks do not replace the one required hosted L3 on this CI change's final
candidate. Local focused tests do not establish the new hosted shard runtimes.

L4 stays separately authorized, off-public and source-pinned. Every decision
and successful PUBLIC gate explicitly report L4 as **NOT VERIFIED**. No private
inputs enter Actions and no frozen literary scores are regenerated. This
CI/selector change still requires separately authorized L4 evidence or an explicit
owner-approved exception before merge, alongside independent review and green
hosted L3. PUBLIC success does not supply that exception.
