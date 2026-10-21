# Public CI reliability

L3 retains all three mandatory PUBLIC Windows Python 3.14.4 shards. Private and
historical modules remain excluded, not passed. The zero-provider wrapper,
explicit DPAPI deselection, external temporary roots, publication history gate,
credential exclusion and read-only workflow permissions are retained.

## Interim scheduling and limits

The prior source-byte heuristic did not measure execution cost. Whole-shard
observations from run 39363803488 were approximately 2m13s, 39m34s and 8m19s;
run 39372349993 exceeded the 60-minute cap in shard 1. These observations do not
identify the slow module or explain runtime variation.

`tests/public_shard_schedule.json` freezes the canonical Git-blob allocation at
baseline commit `2967422172c42f30611d3ad574634541323b0615`, tree
`47c488d15235cd94e8ab4f66186e5a8ec1af5ad3`. Its original 189 modules remain in
three sets of 63; the new synthetic reliability module is added to shard 0.
This is an **interim schedule with no per-module timing weights**. It deliberately
does not claim a measured rebalance. Windows newline conversion changed the old
on-disk heuristic to 64/63/62 in the development checkout, so the explicit schedule
also removes checkout-format dependence. Historical hosted membership has not
been independently reconstructed from a module manifest.

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

## Measured rebalance procedure

After an approved complete three-shard run, independently reconcile all three
selection manifests and exact tested source identities. Check that every public
module appears once, all exclusions and 15 node deselections match, and every
required shard finished successfully. Use `timings.json` module collection and
setup/call/teardown totals, examining `events.jsonl` for outliers and shared-fixture
costs. Incomplete modules provide lower bounds only, never final timing weights.

For a later reviewed schedule update, sort complete module measurements by
measured descending duration (path breaks ties), then allocate each module to
the currently least-loaded of three buckets (bucket index breaks ties). Retain
measurement source SHA/tree/run and repeat on independent Windows runs to assess
variance and order effects. New or unmeasured modules require explicit review;
they must not silently get invented costs or disappear. Keep finite caps and
full coverage until the replacement schedule actually completes.

## Risk-based public qualification

`scripts/public_ci.py` and `scripts/public_ci_policy.json` implement a deliberately
small reviewed policy. Every run performs the publication/history, inventory,
private-path and credential guards. Each selected tier compiles Python and uses
the existing zero-provider wrapper, bounded supervisor and metadata receiver.
The default `run_public_tests.py` invocation still runs the complete public suite.

| Tier | Eligible delta | Required tests |
| --- | --- | --- |
| L1 | Only modifications to `docs/DEVELOPMENT_TIPS.md`, a navigation-only page | Public CI synthetic controls and runtime hardening smoke |
| L2 | Only modifications to `source_language_filter.py` | The same smoke plus all three direct callers' public test modules |
| L3 | Every other case | Existing 190-module three-shard partition, exactly once |

The L2 helper is a pure language-tag predicate used by
`audit_semantic_completeness_all.py`, `audit_semantic_completeness_candidates.py`
and `audit_semantic_kinship.py`. Their three test modules exercise bare-language
and tagged-language discovery. No production writer imports this helper. The
policy and validator both require that exact nonempty module set; broad source
coverage is not claimed. Adding mappings requires an explicit policy/code review.

Unknown or mixed paths, added/renamed/deleted/type-changed files, dependencies,
configuration, tests, infrastructure, credentials/security, publication policy,
CI policy and normative documentation (including README and AGENTS) take L3.
Pushes to main and manual runs also take L3. A missing, malformed, or changed
impact policy falls back to L3. Missing or stale test classification/scheduling
blocks qualification entirely; new tests cannot disappear from the public
partition. This PR itself takes L3. The original classification, schedule,
2 private + 7 historical module exclusions and 15 node deselections are unchanged.

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
python tests/zero_provider_validation.py --basetemp "$env:TEMP/nt-focused-unique" --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q -p no:cacheprovider tests/test_public_ci_reliability.py tests/test_runtime_hardening_smoke.py
```

Synthetic tests cover tier happy paths, risky/unknown/mixed/rename/delete fallback,
invalid policy, source/run/merge conflicts, cancelled/skipped required jobs,
tampered and incomplete evidence, exact public exclusions and partition coverage.
Real temporary Git repositories exercise all three tiers through decision,
guarded child execution and the gate, including base-only changes and merge-tree
drift. Existing complete-runner negative controls remain intact. These local
checks do not replace the one required hosted L3 on this CI change's final
candidate. Measured shard rebalancing remains separate.

L4 stays separately authorized, off-public and source-pinned. Every decision
and successful PUBLIC gate explicitly report L4 as **NOT VERIFIED**. No private
inputs enter Actions and no frozen literary scores are regenerated. This
CI/selector change still requires separately authorized L4 evidence or an explicit
owner-approved exception before merge, alongside independent review and green
hosted L3. PUBLIC success does not supply that exception.
