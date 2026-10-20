# G11 — Final Freeze

G11 is the practical DeepSeek-V4-Pro-replacement benchmark: four independent
tracks (`GLM-5.3-Flash` and `DeepSeek-V4.1-Flash`, each against *Example Project A*
and *I Became the Example Novel*) run Chapters 1-100 through the real
production lifecycle. When the 400-chapter paid experiment finished, an immutable
evidence boundary is needed **before** any interpretation of the result.

`benchmark_longitudinal_g11_final_freeze.py` creates exactly that boundary. It is
create-once, read-only afterwards, and makes **zero** Provider calls and **zero**
credential reads. It is deliberately *not*: an anonymity/A-B mapping, a blind
literary scoring pass, a longitudinal scoring pass, a score freeze, an identity
reveal, or a final model synthesis. Those are later phases and none of them is
performed or authorized here.

## Purpose / evidence boundary

The final freeze sits after the completed paid execution and before every
downstream analytical phase. It proves, from durable evidence alone, that:

* all four tracks finished exactly Chapters 1-100 with the exact literary and
  state/audit checkpoints and no active quarantine;
* the global completed shape is `400/400` canonical chapter translations,
  `16/16` scheduled audits, `28/28` matched literary comparison checkpoints, and
  `8/8` matched state/longitudinal comparison checkpoints;
* the whole execution root is preserved as a byte-identical physical snapshot
  whose identity can be re-proved later, file by file;
* the two model-level ledgers are frozen once each and are never double-counted.

It makes no claim about translation quality, and it names no literary winner.

## Audited source semantics

The freeze is built on the already-audited G11 implementation and does **not**
reinterpret it:

* `benchmark_longitudinal_g11_runner._tree_fingerprint(...)` returns a single
  SHA-256 string, not a rich object. The freeze does not treat it as a structured
  fingerprint.
* `status_g11(...)` and `verify_g11(...)` are read-only with `provider_calls == 0`.
  `verify_g11` proves source bindings, execution isolation, no symlinks, manifest
  track/checkpoint/budget shape, residual repair enablement, frozen effort
  identities, model-ledger validity, and (when the historical pristine path is
  supplied) a pristine proof. The freeze claims only what those functions
  actually return.
* `G11Ledger.summary()` durably returns the ceiling, committed and remaining
  credit-equivalent, budget violation, operation counts, transport attempts,
  retries, first-attempt/eventual successes and rates, regenerations, and the
  prompt/completion/cache-hit token totals. The freeze preserves that summary
  exactly.
* A successful scheduled audit is durably represented by the committed
  `output/audits/audit_XXXX_YYYY.json` record, the matching
  `data/audit_history.json` entry, and the progress marker. Production has a
  valid zero-candidate fast path that returns before chunk execution; such an
  audit has no `*.checkpoint.json`. Candidate-bearing audits additionally
  require a completed chunk checkpoint whose window and completed leaf-chunk
  roster agree with the committed audit record.
* DeepSeek adaptive primary-translation LOW mode is real experimental behaviour
  and stays visible: both DeepSeek tracks activate it at an early chapter, and GLM
  is never marked adaptive-low.
* The GLM final audit-content recovery for the reviewed project is real
  experimental evidence and is preserved, not flattened away.

## Physical snapshot semantics

The execution root is copied byte-for-byte into `execution_snapshot/`. Its
identity uses a freeze-specific deterministic physical-tree fingerprint:

* every relative POSIX **file** path, its exact byte size, and the SHA-256 of its
  bytes;
* the records are sorted deterministically and a whole-tree SHA-256 is computed
  from the sorted records;
* the root symlink and any descendant symlink are rejected rather than followed;
* no timestamps or machine-dependent metadata participate in the identity.

`execution_tree_manifest.json` retains the complete per-file manifest plus
`file_count`, `byte_count`, and the whole-tree SHA-256, so individual files can be
re-proved later. The freeze computes the source fingerprint before the copy (A),
the snapshot fingerprint after the copy (B), and the source fingerprint again (C)
and requires `A == B == C`.

## Model-ledger deduplication

There are two tracks per model but only one shared physical model-level ledger.
The freeze reads and hashes each physical ledger **exactly once per model** and
stores it under `model_ledgers[<model_id>]`. Track records reference their model
ledger by id. The total model cost is the single ledger summary; the two
track-level copies are never added together. Only metrics directly and
deterministically supported by the durable ledger document are derived
(reservation-status counts, attempt-outcome counts, operation-kind counts, total
durable attempt elapsed seconds).

## Exact-vs-derived metric policy

Exact, durably recorded facts (ledger summaries, progress markers, quarantine
state, checkpoint rosters, recovery counts, adaptive-low activation) are frozen
verbatim. Additional metrics are added **only** when they can be computed
directly and deterministically from durable records. No value is estimated,
imputed, or fabricated, and nothing that the durable evidence does not record is
invented.

## Adaptive-reasoning and recovery preservation

Adaptive-low activation is captured per track (activation chapter plus the
durable high-effort-saturation events). Content-recovery and final-audit-content-recovery
cycles are captured as counts and, for the final audit window, as the list of
cycle directories with their manifest SHA-256. This keeps the DeepSeek adaptive
behaviour and the GLM/Ace audit recovery history visible in the frozen evidence.

## Source and repository bindings

The freeze records separate, non-conflated identities:

* the current final-freeze worktree HEAD (the tool repository HEAD);
* the G11 preregistration revision and SHA-256;
* the G11 runner revision and file SHA-256;
* the **historical** G11 execution-preparation repository binding recovered from
  durable execution evidence (the preregistration `repository_anchor` that the
  durable run identities bind through `g11_preregistration_sha256`).

The historical paid-execution anchor is deliberately distinct from the
final-freeze HEAD; the freeze never pretends the final-freeze commit was the paid
execution commit.

## Create-once and path safety

The output root must be external to the repository, execution, pristine, and
production roots, and overlap in either direction is rejected. An existing output
root is refused. Creation happens in a sibling staging directory
(`.<name>.creating-<random>`) and is published by a single atomic rename. A failed
create removes only its own staging directory and never leaves anything that
looks like a completed freeze. The repository, execution, pristine, and
production input roots are never mutated.

## Output layout

```
execution_tree_manifest.json    # per-file manifest + counts + whole-tree SHA-256
g11_final_freeze.json           # every bound identity + deterministic freeze_id
G11_FINAL_FREEZE_SUMMARY.txt    # human-readable frozen operational facts
execution_snapshot/...          # byte-identical physical copy of the execution root
```

`freeze_id` is the SHA-256 of the canonical JSON of the final freeze payload with
`freeze_id` excluded, after which `freeze_id` is inserted. There are no circular
hashes: nothing requires a hash of `g11_final_freeze.json` inside itself, and
because the summary text depends on `freeze_id`, the summary is deliberately
**not** hashed back into the freeze payload.

## Create usage

```
python benchmark_longitudinal_g11_final_freeze.py create \
  --repo <repository-root> \
  --execution <completed-g11-execution-root> \
  --pristine <historical-pristine-root> \
  --production <live-production-root> \
  --output <new-external-freeze-root>
```

## Verify usage

```
python benchmark_longitudinal_g11_final_freeze.py verify \
  --repo <repository-root> \
  --execution <completed-g11-execution-root> \
  --pristine <historical-pristine-root> \
  --production <live-production-root> \
  --output <existing-freeze-root>
```

`verify` is independently read-only. It recomputes the snapshot manifest and
rejects byte tampering, missing or extra snapshot files, manifest tampering,
model-ledger and per-track evidence tampering, checkpoint tampering,
`g11_final_freeze.json` tampering, `freeze_id` tampering, and declared-hash drift
between the frozen source identity and the live execution root. It makes zero
Provider calls and reads no credentials.

## Zero-provider safety

The freeze explicitly records and asserts:

```
provider_calls           = 0
credential_access        = false
blind_bundle_created     = false
scoring_performed        = false
identity_revealed        = false
paid_execution_authorized = false
```

No private identity key is created in this phase. Importing the module performs
no filesystem write, credential read, network activity, or Provider call.

## Separation from later phases

The freeze is an evidence boundary only. Anonymity / A-B mapping, blind literary
scoring, longitudinal scoring, the score freeze, the identity reveal, and the
final model synthesis all happen strictly afterwards. This phase scores nothing,
reveals nothing, builds no blind bundle, and authorizes no paid call.
