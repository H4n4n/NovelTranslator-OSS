# Public CI reliability

All three PUBLIC Windows Python 3.14.4 shards remain mandatory. Private and
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

The pytest telemetry plugin emits flushed collection/test/phase start records
and JSONL phase timings, plus a collected-node manifest and module totals.
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

## Separate follow-on policy design (not activated here)

A separate policy change should keep an always-on publication/integrity job and
a stable required final status using `if: always()`. Its audited impact map must
account for every path; unknown, mixed, renamed or deleted paths and CI, selector,
security or safety-normative `AGENTS.md` changes default to full PUBLIC L3.
Conditional L2/L3 jobs must be checked against the trusted decision: cancelled,
missing, skipped, timed-out or stale required evidence fails the final status.
Bind PR head, base and actual tested merge-commit/tree identities. A changed merge
tree requires new evidence. Restrict supersession cancellation to PR runs;
protect main/release evidence, with postmerge reuse only after proven tree
identity. Ruleset activation needs separate owner approval. L4 stays separately
authorized, off-public and source-pinned; no private inputs enter PUBLIC Actions
and no frozen literary scores are regenerated. This CI/selector change itself
requires successful L3 and separately authorized L4 evidence (or an explicit
owner-approved exception) before merge. Unavailable L4 is NOT VERIFIED.
