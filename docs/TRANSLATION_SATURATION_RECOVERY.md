# Bounded V4.1 Flash translation saturation recovery

This implementation has only been validated with local fakes and durable journals.
It must remain unmerged until independent review and separately authorized direct
DeepSeek validation confirm the 24000-token request is supported. Production default
remains `deepseek-v4-pro`; V4.1 Flash is explicit opt-in.

## Requests and eligibility

| Operation | Model | Ceiling | Thinking | Reasoning |
|---|---|---:|---|---|
| chapter_translation | deepseek-flash | 12000 | disabled | omitted |
| chapter_translation_recovery | deepseek-flash | 24000 | disabled | omitted |

Recovery uses exactly the initial full system/user messages: authoritative source,
heading, glossary, continuity, profile and policy context. No partial English is
included, continued, stitched or persisted as final prose. Fresh full translation
avoids guessing a source cursor or inventing an English overlap/seam. Its higher
cost is explicitly retained in accounting.

The initial classifier permits normal nonblank STOP. Only LENGTH with at least 20
non-whitespace characters and three visible ASCII alphabetic words is a substantive
saturation candidate. Blank/tiny/punctuation-only LENGTH fails closed. Blank STOP
and every other non-stop reason fail closed immediately. The exact reviewed model,
canonical version, 12000 disabled/None initial controls and 24000 disabled/None
recovery policy must also match. Pro, retired Flash and benchmark adapters do not
receive recovery. A current valid paid pending translation wins before generation.

## Durable identity and transport

Each novel has distinct fixed journal slots in `data/provider_request_journal/`:
`NNNN.chapter_translation.json` and `NNNN.chapter_translation_recovery.json`.
Initial bytes are never edited, replaced, superseded or deleted by recovery.
Both sends use the established journal transport and exact provider-model identity
guard. An observed model other than `deepseek-flash` is durably recorded, then blocks
execution and replay; no alternate transport or guard bypass is introduced.

Before recovery, the initial journal is re-read through `load_matching_journal`
using the exact reconstructed request identity. Its status, operation ID, complete
normalized envelope and response SHA must match. Recovery's request identity still
binds novel/chapter/profile/source/model and exact full provider kwargs. The existing
`translation_sha256` component additionally hashes canonical JSON containing:

- initial operation ID;
- entire initial response-envelope SHA-256 (including content, usage and identity);
- translation-recovery-specific policy SHA-256.

Only these identities are used for the binding, never provider payload metadata.
A changed initial response or translation-recovery semantic policy cannot silently reuse or bypass the fixed
recovery journal. Ordinary initial request identity remains unchanged.

The dedicated identity has schema 1 and revision `translation-recovery-policy-v1`.
Its exact fields are: `schema_version`, `revision`, `model_binding` (requested model
and canonical version), `initial_translation`, `recovery_translation`, `strategy`,
`saturation_minimum_characters` and `saturation_minimum_words`. Both operation nodes
bind operation ID, max tokens, thinking, reasoning, mode, parent, max attempts and
trigger. The strategy is `fresh_full_translation`; eligibility thresholds remain
20 non-whitespace characters and three alphabetic words. Canonical sorted compact
JSON is hashed with SHA-256. Pending provenance uses this same hash and dedicated
revision, and prefix proof verifies them against the reviewed translation semantics.

Global production policy revision, state policy and auxiliary-operation settings
are excluded. Changing state recovery, glossary review, audit or micro-repair budgets
alone preserves both paid journals' replay with zero sends. Changing initial or
recovery controls, model/version, strategy, dedicated schema/revision or eligibility
semantics fails closed. The pre-review whole-policy recovery binding is not silently
migrated or re-authorized; no paid validation existed for that draft implementation.

## Restart and terminal outcomes

PLANNED means no send started and can resume the same logical operation. STARTED is
persisted before transport; STARTED or UNCERTAIN on restart blocks automatic resend.
RESPONSE_RECORDED replays with zero sends, even when its finish reason is unsafe.

| Durable state at interruption | Restart |
|---|---|
| Initial LENGTH recorded, recovery absent | Replay initial, one recovery send |
| Both responses recorded, pending absent | Replay both, zero sends |
| Recovery LENGTH/blank STOP/unexpected finish recorded | Replay, terminal stop, zero sends |
| Initial STARTED/UNCERTAIN | Block; no recovery or initial resend |
| Recovery STARTED/UNCERTAIN | Replay initial, block recovery resend |
| Valid paid pending text | Reuse pending; no translation generation |

Recovery has one fixed logical slot and no recursive path or larger escalation.
Only nonblank recovery STOP is successful; its genuine finish reason and full fresh
English enter the ordinary catastrophic-completeness/residual-CJK/locked-term/
semantic-shadow/state/review/audit/commit protocol. No QA boundary is bypassed.

## Accounting and pending crash boundary

Successful recovered translation stats contain sums of all six ordinary accounting
fields plus `translation_accepted_operations`: two distinct operation IDs and their
individual accepted stats. Each retains its actual pricing window/version/rates.
Recovery does not reprice this sum using either window, and replay reconstructs the
same sum rather than adding to progress. Validation applies existing price/token/
cost invariants to both individual records and checks their exact sum. Existing
chapter commit accounting adds the aggregate at most once.

A failed uncommitted attempt has no fabricated progress charge: accepted usage is
preserved in the paid journals, not lost or treated as free. Ambiguous remote cost
is not reconstructed. Provider billing remains authoritative.

Only recovery STOP English becomes pending Markdown. Metadata is written first in
the transient `translation_recovery_recorded` stage, then Markdown, then normal
`translated` metadata. A metadata-only or metadata-plus-Markdown interrupted prefix
is accepted by startup only when both completed journals, exact response/operation
chain, model identity, usage and any present Markdown prove it. Corrupt, uncertain,
missing or mismatched proof remains blocked. Missing Markdown is reconstructed
through the exact request-bound replay path; no new send is permitted by a proven
completed slot. Ordinary pending write order and older metadata remain compatible.

Recovery metadata adds the used flag, initial/recovery operation IDs, genuine finish
reasons, complete response-envelope hashes, recovery policy revision/hash, strategy
`fresh_full_translation` and 24000 recovery ceiling. Canonical model/version/thinking
remain pinned; `translation_max_tokens=24000` describes the final generating request.
The existing pending writer validates these fields before preserving that ceiling.
No source or partial English is duplicated in metadata. Later local QA repairs keep
the generation provenance; the original recovery-response hash records pre-repair
provider output. The transient prefix requires exact pre-repair text only.

`production_translation_recovery_prefix.py` is the single read-only filesystem
boundary used by both startup validation and fresh-process model resolution. It
requires a canonical numeric pending JSON filename, matching integer chapter and
`translation_recovery_recorded` stage; loads the exact initial and recovery slots
through the existing journal record/response-hash guard; proves both completed
operation IDs, V4.1 provider models, matching source/profile/novel identity, genuine
LENGTH -> STOP outcomes, dedicated policy hash/revision and response binding,
individual accepted usage, aggregate accounting, and any present Markdown.

Before preparing model globals, the launcher discovers numeric Markdown and JSON
pending candidates. A proven JSON-only recovery prefix pins the effective model to
`deepseek-flash` / `DeepSeek-V4.1-Flash`, even for default or explicit Pro requests.
Unproven/malformed numeric orphan JSON fails closed and cannot supply a model pin.
Existing ordinary Markdown pending and legacy unpinned behavior is unchanged.
A regression starts a genuinely new Python interpreter through
`prepare_production_runtime()` and `load_production_module()`, then reconstructs both
exact journal identities, makes zero sends and safely materializes normal translated
pending state. Missing/uncertain journals, bad hashes, forged provider/model/version,
binding and accounting errors block before model override.

The structured translation result exposes both envelopes (replay flags, operation
IDs, finish reasons and final provider identity), recovery attempted/used/failure,
aggregate stats and elapsed time. It retains historical four-value unpacking for
translation adapters. Uncertain transport failures use existing typed exceptions.

## Policy and observability

Policy schema remains 1; revision is `production-operation-policy-v2`. Runtime policy
now binds canonical model version from the existing model registry while preserving
launcher/pending selection and experimental control overrides. Unregistered
historical adapters have no fabricated version or translation recovery. State,
glossary, audit, micro-repair and adjudication controls and reasoning are unchanged.

Compact events cover saturation detection, recovery start/end/replay/failure. They
contain chapter, operation IDs, finish reasons, ceilings, policy identity and response
hashes; existing chapter-end stats describe aggregate accepted usage. No prompts,
source, English, partial prose, credentials or provider payloads enter these events.

## Proposed separately authorized direct-provider validation

After independent review, use an isolated harness and copied/frozen chapter inputs,
never live production state. Keep exact direct-provider model identity enforcement,
SDK retries disabled and fixed durable journal slots. Bound authorization and spend
to one normal 12000 V4.1 Flash translation, one deliberately constrained initial
translation that returns substantive LENGTH, and one fresh 24000 recovery using the
same full messages, disabled thinking and no reasoning key. The test-only constrained
initial must be explicitly authorized in the isolated harness, not weaken the
production 12000 eligibility guard. Validate endpoint acceptance, true STOP/full
output, both accepted usage records and downstream local QA. Then replay recorded
requests and verify zero additional sends. An unexpected identity, uncertain send,
unsafe response or unsupported 24000 ceiling stops validation without retry or
escalation. Merge remains an independent operator decision.
