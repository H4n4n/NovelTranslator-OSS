# G10 Qwen vs GLM Head-to-Head — Preregistration v1

**Status:** zero-provider design freeze. Paid execution is blocked.

Benchmark ID: `g10-qwen-glm-headtohead-v1`

Repository baseline: `e6a332924a5866be0fb96543ca4a3338d52ebd1c`

G10 separates intrinsic matched literary quality from real NovelTranslator longitudinal engine performance. The old GOAT experiment remains permanently closed and read-only.

## Models

| Model | Translation | Structured auxiliary | Semantic auxiliary |
| --- | --- | --- | --- |
| `qwen/qwen3.8-max-0902` | medium | low | medium |
| `z-ai/glm-5.3-flash` | high | low | high |

Structured auxiliary work means state extraction and state-JSON repair. Semantic auxiliary work includes glossary review, periodic audit, and terminology repair.

## G10A — matched state-free literary arm

Primary endpoint: 48 fresh matched cases, 24 per novel. Chapters 1-2 are excluded from the primary endpoint so old GOAT direct-comparison overlap does not enter the main significance test.

Example Project A primary chapters:

`3, 56, 108, 161, 214, 266, 319, 372, 424, 477, 530, 582, 635, 687, 740, 793, 845, 898, 951, 1003, 1056, 1109, 1161, 1214`

Example Novel primary chapters:

`3, 18, 33, 48, 63, 78, 94, 109, 124, 139, 154, 169, 184, 199, 214, 229, 244, 259, 275, 290, 305, 320, 335, 350`

Three blinded bridge/replication cases are descriptive only: Ace Ch1, Ace Ch2, and Rogue Ch1.

G10A has no evolving glossary, continuity, state extraction, periodic audit, or retroactive mutation. A completed arm contains 51 pairs / 102 translation calls.

## G10B — real engine chronological arm

Four independent fresh-S0 tracks:

- Qwen × Example Project A, Ch1-50
- Qwen × Example Novel, Ch1-50
- GLM × Example Project A, Ch1-50
- GLM × Example Novel, Ch1-50

This is 200 canonical chronological chapter executions if all tracks complete.

Both novels use production `audit_interval = 25`, so G10B preserves the real cadence:

`Ch1 ... Ch25 -> production periodic audit -> Ch26 ... Ch50 -> production periodic audit`

There are two audits per track and eight audits total. Synthetic Ch10/20/30 checkpoints are forbidden.

The production audit may change glossary decisions and apply every eligible source-anchored retroactive correction that the normal engine would apply. G10 must not suppress those fixes. Contextual blind find-and-replace remains forbidden.

For every Ch25/Ch50 audit, freeze pre/post state, exact audit request/response evidence, decisions, proposed/eligible/applied/rejected retroactive replacements, modified chapter identities, before/after SHA-256 values, backup evidence, regressions, following-chapter terminology/entity behavior, provider sends, and accepted credit-equivalent cost.

## Blind evaluation

The public bundle hides model identity, effort, cost, and transport metadata. Literary scoring reuses the existing seven-dimension 0-10 rubric: fidelity 30%, completeness 15%, terminology 15%, naturalness 15%, dialogue/tone 10%, relationships/pronouns 7.5%, and cultural/technical handling 7.5%.

Longitudinal state/audit scoring uses entity/name continuity 20%, terminology/mechanics/rank continuity 20%, factual/plot continuity 20%, relationships/status/pronouns 15%, state precision/hygiene 15%, and audit correction quality/no regression 10%.

Both literary and longitudinal blind scores must freeze before identity reveal.

Every planned literary/checkpoint slot remains represented even if provider ambiguity or a terminal reliability event prevents a comparable A/B pair. Only slots with both candidates are quality-scoreable. An unavailable slot receives **no synthetic zero, no imputed score, and no fabricated candidate**; it is preserved anonymously and attributed only after score freeze/reveal for the separate reliability analysis.

## Statistics

Primary G10A inference uses only the 48 fresh matched pairs. Report W/T/L by language first, use a two-sided exact sign test on non-ties, and a 10,000-replicate stratified bootstrap 95% CI for pooled paired-point rate. The three bridge cases remain descriptive.

G10B remains descriptive because there are only two novel tracks per model. Literary quality, longitudinal state/audit quality, reliability, and cost remain separate result families.

## Provider safety

Every provider-assisted operation must be durably journaled before sending and bound to an exact `request_sha256`. Application auto-retry is zero. Deterministic rejection is terminal. A genuine post-send ambiguous outcome is never silently resent.

G10 permits at most **one paid transport at a time**, and the durable global CE reservation ledger has exactly one writer. This prevents concurrent reservation races while preserving sequential engine state.

G10A quarantines only the affected model/case. G10B quarantines the affected model/novel chronological track. Independent non-quarantined work may continue.

Durable `response_recorded` replay is local-only and must be proven using a physical shadow copy with poisoned transport. Child Python processes require UTF-8 hardening equivalent to `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`.

## Budget

This preregistration authorizes no paid ceiling yet. Before any paid run, G10 must recapture current public provider pricing, compute exact operation-aware maximum reservations from the real corpus and completion ceilings, freeze an operator-approved global/G10A/G10B ceiling, and prove that accepted CE plus full unresolved/ambiguous reservation cannot exceed it.

## Implementation order

1. G10A0 — preregistration/source binding freeze.
2. G10A1 — generic safety/accounting/recovery tooling.
3. G10A2 — matched-literary executor/ledger.
4. G10B1 — four-track Ch1-50 real-engine controller.
5. G10C — generic blind bundle/scoring/reveal tooling.
6. G10D — physical-shadow + poisoned-transport rehearsal.
7. G10E — tooling/source/budget/root freeze.
8. G10F — paid matched-literary execution.
9. G10G — paid chronological execution.
10. G10H — blind corpus freeze.
11. G10I — blind literary + longitudinal score freeze.
12. G10J — identity reveal.
13. G10K — exact reliability/cost join.
14. G10L — final immutable freeze.

G10F/G10G remain unauthorized until a separate paid-authorization contract passes every G10E gate.
