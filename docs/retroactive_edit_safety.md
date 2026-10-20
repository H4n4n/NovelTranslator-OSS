# Historical terminology edits: ownership and crash recovery

The maintained production engine delegates startup known fixes and periodic
audit corrections to `retroactive_span_planning.py` and
`retroactive_transactions.py`. This is entirely local. It adds no provider
requests, retries, repair models, fallback, or paid-response replay paths.

The previous implementation tested whether a Chinese key appeared somewhere
in a chapter, then globally replaced every matching English substring. That
could change `Han` inside `Hand`, another referent's standalone `Han`, or a
short name inside a longer glossary identity. Whole-word matching alone cannot
establish referent ownership. It also wrote chapter text before recording a
durable result, leaving an interrupted pass without sufficient restart evidence.

## Authorization policy

`retroactive_safe_categories` is only an initial eligibility filter. The
planner requires a complete ownership inventory from both the original and
staged glossary. It reserves exact glossary keys, aliases and source variants;
canonical targets, contextual/approved renderings, repair aliases, explicit
accepted inflections, and core-derived accepted regular plurals.

Source occurrences use the existing longest non-overlapping claiming helper.
A short key embedded in a longer identity cannot claim that occurrence. Equal
source aliases and partially overlapping source identities cannot acquire
authorization through the helper's deterministic lexical tie-break. An exact
owned source key remains mandatory; an alias alone cannot authorize editing.

The initial automatic policy accepts exactly one owned source occurrence and
one exact bounded English occurrence. Word boundaries include Unicode letters,
digits and underscores. English competitors are reserved without regard to
case, even when their source is absent from this chapter. A shared target, any
overlapping longer/shorter rendering family, contextual entry, alias identity,
or overlap between proposed rules requires manual review. Overlapping rules
are never chained or resolved by proposal order.

Existing generic-term exclusions remain active. Additional common object and
noncountable targets are excluded; lowercase targets and single-word targets
outside character/historical-person/alias categories require manual review.
Category approval cannot override these checks. This deliberately sacrifices
repair coverage: the glossary inventory and exact cardinality establish the
deterministic authorization boundary, without claiming semantic alignment of
unknown or undeclared referents. Ambiguity remains a skip, never an inference.

The compatibility `source_anchored_replacement` helper is now pure span-based
and requires an explicit glossary inventory; absence of evidence returns no
edits. Historical filesystem writes use the transaction engine.

## Immutable intent and identity

Schema version: **1**. Policy version: **owned-single-span-v1**.

Each plan contains:

- novel identity, audit label, through chapter, normalized/deduplicated rules,
  category policy, and ownership-only glossary inputs;
- committed chapter inventory and each source text SHA-256;
- exact committed English BEFORE and complete expected AFTER byte SHA-256;
- approved UTF-8 byte offsets, source identity and source character offsets,
  old/replacement bounded terms and their hashes, and cardinality evidence;
- all per-rule/per-chapter rejection decisions and their reasons.

Plans contain no chapter prose. Timestamps, glossary notes/contexts and runtime
timing do not participate in identity.

The transaction ID is the SHA-256 of canonical immutable plan JSON. It binds
rules, ownership policy, source identities and expected BEFORE chapter bytes.
An operation slot derived only from novel/audit label/through chapter locates
that original plan on restart, including after chapter bytes have changed.
Different rules under an existing operation slot fail closed; the engine never
silently replaces the plan. Changed rules produce different transaction IDs
when planning a new operation.

## Durability protocol

Evidence lives in `output/retroactive_fixes/transactions/`:

- `<operation-slot>.plan.json`: immutable durable intent, saved before any
  historical chapter or backup write;
- `<operation-slot>.state.json`: `PLANNED` -> `APPLIED` -> `FINALIZED`, or
  `BLOCKED` with chapter/hash diagnostics;
- `<operation-slot>.result.json`: first durable final evidence, preserved on
  replay;
- `<operation-slot>.reconcile.<hash>.json`: deterministic subsequent
  observations; identical observations reuse their evidence file.

The local transaction lock uses the existing nonblocking OS-lock helper.
Supported production callers also retain the enclosing novel writer lock.
External editors must remain quiescent during application, as with the existing
production writer protocol.

The whole batch is preflighted before additional chapter writes. For every
planned chapter, current bytes must equal BEFORE or AFTER. Any other hash,
missing chapter, changed source or changed immutable input stops execution.
Even chapters with no eligible spans remain bound to their original identities.

Backups live in `backup/retroactive_<transaction-id>/chapters_before/` and must
equal exact BEFORE bytes. Existing correct backups are reused; inconsistent
backups are never overwritten. AFTER without its proven BEFORE backup blocks.

Chapter and backup writes reuse `atomic_write_bytes`: binary sibling temporary
file, flush/fsync, then atomic `os.replace`. UTF-8 BOM, CRLF, Markdown, Unicode
and every byte outside approved spans remain unchanged. This reuses the
repository's process-crash atomic-write guarantees; it does not introduce a new
filesystem or power-loss durability contract.

On restart:

| Current chapter | Action |
| --- | --- |
| BEFORE | Verify/create BEFORE backup, apply exact spans once, verify AFTER |
| AFTER | Verify BEFORE backup, reconcile without another chapter write |
| Neither | Record a blocked hash mismatch; preserve current prose |

Thus Chapters 10/20 already at AFTER and Chapter 30 still at BEFORE resume by
writing only Chapter 30. No success marker is published until the complete
batch reconciles. `FINALIZED` denotes durable **local** result evidence, not
completion of the outer paid audit publication.

Results retain `audit_label`, `through_chapter`,
`replacement_rules_considered`, `replacement_rules_applied`, `chapters_changed`,
`replacement_count` and `details`. Additional fields expose transaction/schema/
policy identity, planned rules, skip decisions, chapter BEFORE/AFTER and backup
hashes, newly applied/already applied/skipped outcomes, reconciliation counts,
and G10/G11-compatible proposed/eligible/applied/rejected modification evidence.
Hash blocks appear in state evidence and typed local integrity errors, rather
than a false successful result. Ordinary console diagnostics contain counts
and identifiers, not chapter prose.

## Production and experiment integration

Periodic audits pass original and staged glossary ownership inputs into the
local engine before accounting or glossary/issues/audit/progress installation.
Reaching the same validated result again reconciles the existing transaction.
Startup known fixes stage their glossary policy and install it only after the
local transaction succeeds; built-in fixes have no ownership bypass.

The maintained `full_pipeline_periodic_audit_recovery.py` experiment adapter
previously rolled back entire chapter and retroactive-evidence trees. For new
span transactions it now proves the immutable plan against the baseline/source,
verifies backup identities and the complete current chapter tree, then preserves
BEFORE/AFTER prose and transaction evidence while restoring the existing
associated-state baseline. Contradictions block before associated-state writes.
Legacy snapshots without span transaction evidence retain their established
recovery path. Provider journaling/callbacks are unchanged.

The separate lower-journal `RESPONSE_RECORDED` auxiliary-to-outer-result gap
(Astra Blocker 4) is **not implemented** here. Ordinary production's outer audit
publication is not redesigned. If outer publication leaves different glossary
ownership inputs, immutable-plan checks fail closed rather than adapting.

## Static historical-prose writer audit

The audit used tracked Python writer searches and AST call inventories:

| Path | Classification |
| --- | --- |
| Maintained production `apply_retroactive_replacements` | Only delegates to the new exact-span transaction |
| Maintained startup known fixes | Uses that same transaction before glossary installation |
| `production_chapter_commit.commit_reviewed_chapter` | Normal atomic chapter commit flow |
| `production_partial_commit_reconciliation` and `full_pipeline_partial_commit_v2` | Read/prove final prose; finish associated-state tail, without final prose rewrite |
| `full_pipeline_periodic_audit_recovery._restore_baseline` | Experiment-owned legacy snapshot restore; new span transactions are preserved and verified |
| `migrate_single_novel` / experimental track preparation | Copy into newly staged isolated project roots; do not edit existing production prose |
| Finalization, normalization, title/publication/editorial writers | Write derived `final/` corpora, not production `output/chapters/` |
| `inspect_chapter` | Writes source inspection text under `output/inspection/` |
| `translate_pilot`, `benchmark_more_chapters`, older `translate_production*` variants, `backup/` copies, `translate_production_before_*` | Historical standalone/pilot/snapshot paths, outside the maintained launcher; unchanged |

Validation uses synthetic disposable chapter copies only. No real historical
production prose, live glossary, frozen experiment evidence, or paid Chapter
397 work is created or changed. Independent review of the draft PR and CI is
required before any production adoption or paid burn-in.

## Local zero-provider verification

Run the guarded focused suite in the isolated checkout:

```powershell
python tests/zero_provider_validation.py -q `
  tests/test_retroactive_transactions.py tests/test_noveltranslator_core.py `
  tests/test_glossary_audit.py tests/test_full_pipeline_audit_boundary.py `
  tests/test_full_pipeline_periodic_audit_recovery.py `
  tests/test_benchmark_longitudinal_g10_recovery_rehearsal.py `
  -k "not test_real_windows_dpapi_round_trip" `
  --basetemp="$env:TEMP/nt-retroactive-review"
```

The CI current-source suite excludes the four Frozen G11 historical modules;
those run against exact `c7396e19f584ed6e6110b5a03eb333747a217dfa` with its
original CRLF working-tree bytes and all 13 preregistered source bindings.
