# G11 — Blind Bundle

G11 is the practical DeepSeek-V4-Pro-replacement benchmark: `GLM-5.3-Flash` and
`DeepSeek-V4.1-Flash`, each against *Example Project A* and *I Became the Rogue First
Prince*, running Chapters 1-100 through the real production lifecycle. When the
400-chapter paid experiment finished, `benchmark_longitudinal_g11_final_freeze.py`
created the immutable evidence boundary.

`benchmark_longitudinal_g11_blind_bundle_freeze.py` is the **next** phase. It
turns the immutable final freeze into a single frozen, blind evaluation package
and nothing more. It is create-once, read-only afterwards, and makes **zero**
Provider calls and **zero** credential reads. The blind protocol itself lives in
`benchmark_longitudinal_g11_blind.py`.

## Phase chain

```
final freeze  ->  blind bundle  ->  blind score freeze  ->  reveal  ->  synthesis
```

- **final freeze (immutable, done):** proves the completed `400/16/28/8` shape and
  preserves a byte-identical `execution_snapshot/`.
- **blind bundle (this phase):** builds the anonymous A/B corpus from the frozen
  snapshot and freezes it. It scores nothing, reveals nothing, and authorizes no
  paid call.
- **blind score freeze:** an evaluator scores every anonymous `side_a`/`side_b`
  independently and records `A`/`B`/`TIE`, then freezes the scores. This happens
  in a later phase; the protocol only provides the freeze/reveal functions.
- **reveal:** the private A/B identity mapping is applied to frozen scores, and
  only then.
- **synthesis:** the revealed per-model results are interpreted.

### Zero provider calls

This engineering phase makes **zero** Provider calls and reads **zero**
credentials. Importing either module performs no filesystem write, credential
read, network activity, or Provider call, and no function here scores or reveals
anything during bundle construction.

The **real** bundle-creation phase against the immutable final freeze also makes
**zero** Provider calls: it only reads the already-frozen snapshot and writes a
new external output root. There is no paid execution path in either module.

## What the blind bundle freezes

- the immutable G11 final freeze: its freeze id, the exact SHA-256 of
  `g11_final_freeze.json`, `execution_tree_manifest.json`, and
  `G11_FINAL_FREEZE_SUMMARY.txt`, the physical execution-snapshot tree identity,
  and the frozen track/model/completion evidence re-derived with
  `benchmark_longitudinal_g11_final_freeze.build_execution_evidence()`;
- **28 literary cases** (14 per novel) drawn from the frozen literary checkpoints
  `1, 5, 10, 20, 25, 26, 40, 50, 51, 65, 75, 76, 90, 100`;
- **8 longitudinal cases** from the frozen Ch25/50/75/100 state checkpoints
  (4 per novel), each with its exact 25-chapter reference source window;
- one private A/B identity key (only its cryptographic commitment is public);
- the frozen literary and longitudinal rubrics and the statistics policy.

## Source material

The blind bundle depends only on `<final-freeze>/execution_snapshot/`. The two
model tracks of the same novel must bind **identical** source chapter libraries;
this is proven independently for Chapters 1-100 of each novel by comparing the
track `data/chapters.json` byte-for-byte and cross-checking each run identity's
`source_bindings.chapter_library_sha256`.

The phase never consults a live G11 paid execution root.

## Literary evidence: checkpoint `chapter.md`, not final `output/chapters`

For every literary checkpoint the case is built from the **pre-extracted
checkpoint evidence**:

```
execution_snapshot/g11_evidence/<run_id>/<chapter>/chapter.md
```

It deliberately does **not** use the final, mutable-by-audit artifact:

```
runs/<run_id>/output/chapters/<chapter>.md
```

The literary checkpoint was frozen at its chronological checkpoint and is the
preregistered evaluation artifact. The `output/chapters/<chapter>.md` file can be
changed later by a retroactive audit or correction, so using it would evaluate
post-hoc content instead of the chronology the experiment actually ran. Each
`checkpoint.json` is validated (`quality_checkpoint == true`) and its `chapter.md`
SHA is checked against the checkpoint artifact record before the text is used.

## Why the public state evidence is sanitized

The raw checkpoint state contains operational metadata (timestamps, stats,
chunking, provider/request/cost accounting, recovery bookkeeping, run/track
identities) that could identify a candidate or invite operational rather than
quality judgement. Each longitudinal `side_a`/`side_b` therefore exposes a
**blind-safe semantic projection**:

1. **glossary** — only semantic fields: `source_key`, `source_term`, `english`,
   `category`, `status`, `approved_renderings`, `alias_of`, `notes`;
2. **continuity** — the semantic continuity facts/summaries, with operational
   identity/accounting keys recursively removed;
3. **issues** — only `chapter`, `type`, `details`;
4. **audit history** — a sanitized semantic history: `start_chapter`,
   `end_chapter`, `changes` (`kept`/`corrected`/`rejected`/`contextualized`/
   `merged`), `retroactive` (`chapters_changed`/`replacement_count`), `issues`.

Timestamps, stats, chunking, cost/token/request counts, provider information,
checkpoint paths, and recovery metadata are never published. This allows blind
assessment of audit/state quality without exposing operational identity.

## Private orientation

The private key binds `schema/revision`, the benchmark, the G11 final-freeze id,
`secret_sha256`, the literary mapping, the longitudinal mapping, and
`provider_calls = 0`. The raw 256-bit secret is generated in memory, used, and
discarded; it is never persisted.

- **literary:** orientation is determined by HMAC-SHA256 secret-key ranking, and
  is exactly `7/7` within each novel (GLM is side A in seven of the 14 cases and
  DeepSeek is side A in the other seven, per novel).
- **longitudinal:** orientation is chosen once per novel and stays stable at
  checkpoints 25/50/75/100, with exactly one novel anchored to each side-A model;
  which novel gets which orientation is secret-keyed.

Public candidate labels are only `side_a` and `side_b`. Model names, provider
names, effort, cost, transport, request/journal/accounting metadata, original run
ids, and model-identifying track ids are forbidden from every evaluator-visible
artifact, and the exact model literals are rejected anywhere.

## Rubrics and statistics (frozen)

Literary (0..10), weights: `fidelity 0.30`, `completeness 0.15`,
`terminology 0.15`, `naturalness 0.15`, `dialogue_tone 0.10`,
`relationships_pronouns 0.075`, `cultural_technical 0.075`; defect flags:
`blocker`, `omission_or_truncation`, `invented_explanation`,
`terminology_inconsistency`.

Longitudinal (0..10), weights: `entity_name_continuity 0.20`,
`terminology_mechanics_rank_continuity 0.20`, `factual_plot_continuity 0.20`,
`relationships_status_pronouns 0.15`, `state_precision_hygiene 0.15`,
`audit_correction_quality_no_regression 0.10`.

Statistics policy: all 28 literary cases are primary; report W/T/L separately by
novel/language first and pool only afterwards; two-sided exact sign test on
non-ties; stratified bootstrap with 10,000 replicates at 0.95 confidence;
per-dimension paired differences are secondary; longitudinal inference is
`descriptive_two_novel_tracks_per_model_repeated_checkpoints`; there is no
composite overall winner score.

## Output layout

```
private/
    private_identity_key.json      # private A/B mapping; raw secret never persisted
    private_input_manifest.json    # per-candidate checkpoint path + SHA + public case id
public/
    public_blind_manifest.json     # points to each case by relative path + SHA-256
    G11_BLIND_EVALUATION_INSTRUCTIONS.md
    literary/   28 case JSON files
    longitudinal/   8 case JSON files
public_tree_manifest.json          # deterministic public tree identity
g11_blind_bundle_freeze.json       # every bound identity + bundle_freeze_id
G11_BLIND_BUNDLE_SUMMARY.txt
```

`bundle_freeze_id` is the canonical-JSON SHA-256 of the freeze payload with
`bundle_freeze_id` excluded. The private mapping itself is never written into the
root freeze document.

## What the evaluator must NEVER receive before scores freeze

The evaluator may receive only the `public/` subtree. Before anonymous scores are
frozen, they must **never** receive:

- `private/private_identity_key.json`;
- `private/private_input_manifest.json`;
- `g11_blind_bundle_freeze.json` **if** it contains identity-adjacent private
  hashes (this freeze document does contain private-key file/commitment hashes,
  so it is not evaluator material);
- operational ledgers;
- model labels.

The public manifest deliberately points to each case by relative path and
SHA-256 rather than embedding the corpus, so cases can be evaluated in manageable
batches without ever exposing the private mapping.

## CLI

```
python benchmark_longitudinal_g11_blind_bundle_freeze.py create \
  --repo <repo> \
  --final-freeze <immutable-G11-final-freeze-root> \
  --output <new-external-output-root>

python benchmark_longitudinal_g11_blind_bundle_freeze.py verify \
  --repo <repo> \
  --final-freeze <immutable-G11-final-freeze-root> \
  --output <existing-output-root>
```

`create` refuses an existing output root, refuses an output root overlapping the
repository or final-freeze roots, stages in a sibling directory, and publishes by
one atomic rename; a failed create removes only its own staging directory.
`verify` is independently read-only and rejects any drift in the final-freeze
bindings, the snapshot, the source libraries, a checkpoint artifact, the private
key/commitment, the private input manifest, the public manifest, any public case
file, the public tree (extra/missing files), the freeze document, or the
`bundle_freeze_id`. It makes zero Provider calls and reads zero credentials.

## Zero-provider safety flags

```
provider_calls            = 0
credential_access         = false
scoring_performed         = false
identity_revealed         = false
paid_execution_authorized = false
```
