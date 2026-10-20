# G10H — Blind Corpus Freeze

G10H is the final zero-provider corpus-preparation step of the G10 Qwen-vs-GLM
head-to-head experiment. It turns the completed G10F matched-literary evidence and
the completed G10G chronological evidence into a *single frozen, blind* corpus
without calling a Provider, reading a credential, scoring a translation, or
revealing an identity.

It is created once by `benchmark_longitudinal_g10h_blind_corpus_freeze.py` and can
only be re-checked, read-only, afterwards.

## What G10H freezes

- the frozen G10 contract identities (preregistration, G10A1 safety, G10A2
  matched, G10B1 chronological, G10C blind, G10D recovery, G10S0, G10E);
- the exact G10E freeze and the current shared credit-equivalent ledger identity
  and genesis lineage;
- the G10A matched-plan binding and the G10B S0 manifest/root binding;
- deterministic file-count / byte-count / SHA-256 tree identities for the
  completed G10F matched, G10G chronological, and R2 control evidence roots;
- the SHA-256 of the R2 final/checkpoint JSON and the R2 recovery manifest;
- the 51 planned private literary rows and the 4 planned longitudinal rows;
- one private A/B identity key (only its cryptographic commitment is public);
- the public blind bundle.

## Availability treatment

Literary rows are rebuilt from the frozen source text and the completed G10F
accepted artifacts:

- `availability = paired_translation_available` **only** when both model outputs
  exist and validate;
- otherwise `availability = unavailable_model_case`, and the missing candidate
  must carry a valid durable G10F quarantine marker;
- a missing candidate is never manufactured or imputed, and every one of the 51
  planned slots is preserved.

Longitudinal rows (`example_project_a`/`example_project_e` at Ch25 and Ch50) are
comparable **only** when both model tracks genuinely reached the checkpoint with
the required committed audit boundary (`last_completed >= C` and
`last_audited_chapter >= C`). Durable progress is cross-checked against the frozen
completed R2 record. For this frozen run every checkpoint is unavailable because
the tracks stopped before the required checkpoint depth; these are **reliability**
outcomes, not synthetic quality losses.

The expected real counts are asserted fail-closed by the freeze execution itself:

| quantity | value |
| --- | ---: |
| literary planned | 51 |
| literary scoreable | 39 |
| literary unavailable | 12 |
| primary scoreable | 36 |
| bridge scoreable | 3 |
| longitudinal planned | 4 |
| longitudinal scoreable | 0 |
| longitudinal unavailable | 4 |

## Frozen source of truth

The copyrighted source books and the rogue chapter library are intentionally
gitignored and absent from the review worktree, so G10H re-materializes the frozen
source text from the pristine **G10B S0 run chapter libraries**. Each library is
hash-bound by its frozen S0 run identity (`source_bindings.chapter_library_sha256`)
and every source window is proven against the completed G10F accepted artifacts
(`text_sha256`).

## Blindness

The public bundle is built with the existing frozen
`benchmark_longitudinal_g10_blind` protocol and validated by
`validate_public_blind_bundle()`. It exposes A/B candidate content only for the 39
fully paired literary slots, exposes **no** longitudinal side evidence, has
`identity_revealed=false` and `provider_calls=0`, and passes an additional literal
identity-leak scan.

That scan rejects the exact model/provider identifiers (`qwen/qwen3.8-max-0902`,
`Qwen/Qwen3.8-Max-0902`, `z-ai/glm-5.3-flash`) and the forbidden accounting
metadata names (`reasoning_effort`, `credit_equivalent`, `cost_usd`,
`provider_model`, `request_sha`, `reservation_id`) anywhere, and the bare model
tokens `qwen`/`glm` outside ordinary story prose. The frozen benchmark identifier
`g10-qwen-glm-headtohead-v1` is a mandatory part of the blind protocol, so it is
excluded from the bare-token check; ordinary story text is never rejected merely
for containing an unrelated word.

## Private freeze artifacts

Written externally (never committed):

```
private_identity_key.json      # private A/B mapping; raw secret never persisted
literary_input_private.json    # 51 literary rows + per-case evidence
longitudinal_input_private.json
public_blind_bundle.json       # public, blind-safe
g10h_freeze.json               # every bound identity + deterministic freeze_id
G10H_SUMMARY.txt
```

The raw 256-bit secret is generated in memory, used to derive the private key, and
immediately discarded. `g10h_freeze.json` binds the repository HEAD, contract
identities, the G10E freeze, the shared-ledger identity and genesis lineage, the
evidence tree identities, the R2 hashes, the private-key file SHA-256 and
commitment, the private-input SHA-256s, the public-bundle SHA-256, every count,
and the `identity_revealed=false` / `scoring_performed=false` / `provider_calls=0`
flags.

## CLI

```
python benchmark_longitudinal_g10h_blind_corpus_freeze.py create \
  --repo-root ... --g10e-root ... --matched-root ... \
  --chronological-root ... --s0-root ... --g10g-control-root ... \
  --output-root ...

python benchmark_longitudinal_g10h_blind_corpus_freeze.py verify \
  --repo-root ... --g10e-root ... --matched-root ... \
  --chronological-root ... --s0-root ... --g10g-control-root ... \
  --output-root ...
```

`create` fails if a non-empty freeze already exists. `verify` is read-only and
recomputes every important identity, rejecting any drift in a predecessor root, a
source window, an accepted artifact, a durable track record, the private key, the
private inputs, the public bundle, or the freeze document itself.
