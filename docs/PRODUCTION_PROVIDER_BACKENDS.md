# Production provider backends

NovelTranslator keeps a hard separation between the Command Code **coding
agent** and the **translation inference transport**.

- The Command Code coding agent is used for engineering, analysis, repository
  work, and local orchestration only. Its own model consumption is not an
  inference transport for chapters.
- Translation inference (chapter translation, state extraction, state JSON
  repair, glossary review, terminology repair, residual-script repair, periodic
  audits, and every other model-assisted lifecycle operation) runs through a
  **provider backend**.

## Backends

| provider | base URL | credential | wire model (example) | accounting |
| --- | --- | --- | --- | --- |
| `direct_deepseek` | `http<LOCAL_PATH>` | `DEEPSEEK_API_KEY` (`.env`) | `deepseek-flash` | `deepseek_usd_peak_offpeak` |
| `command_code_provider` | `http<LOCAL_PATH>` | DPAPI secret (see below) | `deepseek/deepseek-v4.1-flash` | `command_code_credit_equivalent` |

The GOAT path is `command_code_provider` with model
`deepseek/deepseek-v4.1-flash`. It never routes through `api.deepseek.com` and
never consumes `DEEPSEEK_API_KEY`.

`production_provider_contract.py` resolves a backend fail-closed: an unknown
provider, or a Command Code Provider request whose wire model is not the exact
GOAT DeepSeek identity, is refused. There is **no silent fallback** to the
direct DeepSeek endpoint.

## Reasoning and budget policy (GOAT)

GOAT does not reuse the direct-DeepSeek `thinking = disabled` semantics. The
Command Code Provider exposes a verified `reasoning_effort` control; the frozen
GOAT policy for `deepseek/deepseek-v4.1-flash` uses primary translation effort
`low`. Completion budgets follow the production-owned GOAT policy (translation 50k,
auxiliary provider-unbounded request). Both remain LOW. Auxiliary wires omit
`max_tokens` and `max_completion_tokens`, including JSON repairs and review
corrections. Domain attempt counts, credit authorization, and provider hard limits
still apply; no retry or fallback is added.

The active completion policy lives in `production_goat_policy.py` and
`production_goat_auxiliary_policy.py`. Verified model/reasoning serialization
still uses `benchmark_longitudinal_capability_results.py`. Frozen G7J/G7P
completion budgets retain their historical semantics. The reviewed physical
context bound is 1,000,000 tokens; a separate output bound remains unknown.
The immutable capability registry binds a local evidence path, exact SHA and
all parsed facts/provenance. Every uncapped authorization recomputes and checks
them; drift fails before credentials/STARTED/transport, with no runtime fetch.
Financial exposure reserves 1M prompt + 1M completion, without a wire cap. See
[details](GOAT_UNBOUNDED_AUXILIARY_POLICY.md).

## Credential handling (GOAT)

The Command Code Provider key is a DPAPI-protected secret at
`%LOCALAPPDATA%\NovelTranslator\Secrets\command_code_api_key.dpapi`.

`command_code_provider_credentials.py` is the production seam. The plaintext:

- exists only in memory during a paid operation;
- is never printed, logged, returned in reports, or written to project files;
- is never placed on a command line;
- is never copied into `.env`;
- is never assigned to `DEEPSEEK_API_KEY`.

The reviewed DPAPI read/validate machinery is reused from
`benchmark_longitudinal_g7g_stage1_secure_launcher.py`. Tests inject a fake
DPAPI backend and a fake secret path; the real secret is never read while
zero-provider tests run.

## Durable paid-request safety

A provider backend supplies **transport and provider-specific
request/response normalization only**. Every real paid request still executes
under the existing production durable journal
(`production_paid_request_journal.run_journaled_paid_request`):

```
PLANNED -> STARTED -> physical transport -> RESPONSE_RECORDED
                                        \-> UNCERTAIN
```

Pre-send refusals never become `STARTED`; `RESPONSE_RECORDED` replays without a
physical send; `UNCERTAIN` blocks automatic resend; missing usage and provider
model mismatch fail closed. Auxiliary operations retain PR140-grade journaling.

## Selecting GOAT for a controlled production trial

A controlled trial must bind the provider transport as part of its immutable
authorization identity (provider, base URL, wire model, reasoning policy,
budget policy, accounting policy, engine identity). A direct-DeepSeek
authorization token must not authorize GOAT transport, or vice versa. Provider
drift invalidates the token.

## Accounting terminology

GOAT requests are billed against plan usage, not a direct cash tariff. GOAT
accounting therefore reports `usd_credit_equivalent_per_1m_tokens` from the
frozen `command-code-provider-public-ratecard-2026-09-19-v1` rate card. Do not
report GOAT usage as if it were direct DeepSeek API dollars.


## Production selection and safe planning

The provider is a separate selection from the logical model. `--provider`
(or `NOVELTRANSLATOR_PROVIDER_BACKEND`) accepts `direct_deepseek` and
`command_code_provider`; GOAT / `v41-flash` is the default pair. Direct Pro
requires explicit `--provider direct_deepseek --model pro`. The engine keeps logical `deepseek-flash`; only the
transport adapter emits `deepseek/deepseek-v4.1-flash` to
`http<LOCAL_PATH>`.

These commands are read-only and do not access DPAPI or provider credentials:

```powershell
python production_path_preflight.py --novel <novel_id> --model v41-flash --provider command_code_provider
python operator_preflight.py --novel <novel_id> --model v41-flash --provider command_code_provider
python controlled_trial_plan.py --novel <novel_id> --model v41-flash --provider command_code_provider --max-api-calls 4 --max-credit-equivalent-usd 1
python pending_recovery_plan.py --novel <novel_id> --model v41-flash --provider command_code_provider --max-api-calls 4 --max-credit-equivalent-usd 1
```

The last command requires an existing proven GOAT paid pending translation.
The allowance above is an illustrative ceiling, not live execution approval.
GOAT planning and execution reject `--max-cost-usd`; direct rejects
`--max-credit-equivalent-usd`. Existing numeric cost fields remain compatible,
with GOAT journals, pending stats, QA and reports explicitly marking credit
semantics and `provider_invoice_claimed=false`. Historical totals are not
reconstructed or relabeled as exact provider invoices.

Controlled execution requires the matching state fingerprint, approval token,
explicit paid authorization flag, provider, model and both ceilings. Executors
rebuild the plan under the existing novel lock before checking the token. Runtime
manifests include the new runtime, execution, credentials and transport Python
files automatically. Provider, policy, source, state, runtime byte or budget-unit
drift invalidates approval before DPAPI. The bounded unattended route now uses
`translate.py --plan-goat-run` followed by matching fingerprint/token and
`--authorize-goat-run`, with explicit operator-selected send/credit ceilings.
The shared GOAT executor invokes the existing lifecycle without a chapter target.
The bare launcher still refuses GOAT; only the authorized route installs its
transport and budget. See [commands and restart boundaries](GOAT_UNATTENDED.md).
Direct DeepSeek unattended production remains explicitly selectable.

## Credential activation and response recovery

`production_goat_execution.py` prepares the existing production lifecycle in
provider-neutral mode. That initialization skips dotenv, the direct key and
OpenAI construction. After authorization, provider/pending/target/budget checks,
`activated_goat_transport` enters `command_code_api_key_scope`. This is the first
DPAPI access point. It constructs the reviewed exact-endpoint client with SDK
retries disabled. The key is never inserted into either provider environment
variable. Finally it releases the adapter's client reference, clears the module
client, closes the SDK client and drops local key/client references. Python/SDK
memory zeroization is not claimed. The older environment helper remains for
compatibility; production GOAT uses the memory-only scope.

Every wire request is normalized before identity hashing. Primary translation
accepts only the exact reviewed legacy 12,000 request shape and becomes 50,000
completion tokens through `production_goat_policy.py`, independently of frozen G7J.
Only exact G7P stage-1 operation/ceiling pairs become 8,000 auxiliary tokens;
arbitrary ceilings are refused. G7B sets reasoning effort `low` for the current
DeepSeek primary and auxiliary operations. Direct-only `extra_body.thinking`
and `max_tokens` never appear in the GOAT wire payload.

Domain-owned journal slots retain their existing operation names. The adapter
covers chapter translation, state extraction/repair, strict terminology repair
and corrections, residual-script repair and corrections, candidate conflict
adjudication, glossary review and JSON/schema/semantic corrections, and periodic
audits and repairs. Context-owned subrequests use deterministic request-hash
slots. Unknown operations fail before sending.

The durable sequence is `PLANNED`, local budget authorization, durable `STARTED`,
one physical send, durable raw `RESPONSE_RECORDED`, then model/usage acceptance,
G7D accounting and completion guards. The accepted/accounted envelope enriches
the already saved raw evidence. Replay uses the same raw response and original
request-start rate snapshot without sending. Surviving `STARTED`, `UNCERTAIN`,
wrong-model, missing-usage and rejected completion records never trigger an
automatic replacement send. Empty reasoning-budget exhaustion also fails closed.
Manual pause remains effective; GOAT bypasses the direct off-peak permission gate
while recording G7D's own pricing period.

New GOAT pending/committed QA records bind logical model/version, provider,
base URL, wire model, reasoning/budget/accounting policy and normalized request
SHA. Recovery requires the original hashed translation journal matching novel,
profile, source, chapter, provider and policy. Provider-unpinned legacy pending
is treated as direct; direct and GOAT recovery cannot cross. Paid pending prose
is reused, translation generation is blocked, and startup audits remain disabled
in bounded recovery. Existing exact partial-commit and completed-pending proofs
use canonical provider-aware QA provenance without weakening their equality
requirements.

## Validation and rollout boundary

All implementation validation uses synthetic novels, fake transport and fake
DPAPI. Frozen benchmark modules and expected hashes remain unchanged. Match CI:
run current-source tests with the four historical G11 modules excluded; run those
four at exact historical commit `c7396e19f584ed6e6110b5a03eb333747a217dfa`
with CRLF checkout bytes. Do not repin frozen hashes to current production code.

This implementation is for draft-PR review. Next: independent GPT-5.6 Sol live
GitHub review, merge only after approval, Command Code local post-merge closure,
and a separately reviewed live GOAT smoke-test plan. No fresh novel roots or
Ace/Rogue execution are authorized by implementation validation.


### GOAT operator routing

A clean GOAT project with both explicit operator-selected limits returns
`BOUNDED_GOAT_READY` and recommends read-only `translate.py --plan-goat-run`.
The one-chapter controlled-trial planner remains an optional separate route. Without an allowance it returns
`BOUNDED_ALLOWANCE_REQUIRED`: structural readiness is reported, but no runnable
plan command or paid allowance is invented. For example, after choosing the
allowance explicitly:

```powershell
python operator_preflight.py --novel <novel_id> --model v41-flash --provider command_code_provider --max-api-calls 4 --max-credit-equivalent-usd <operator-selected-allowance>
```

Pending recovery uses the same explicit credit-equivalent units. Human reports
mark `provider_invoice_claimed=false`. A recorded response that fails
acceptance/accounting latches unaccounted evidence on fresh sends and on replay;
replay still sends zero and never fabricates usage or changes RESPONSE_RECORDED
back to STARTED/UNCERTAIN.


## Post-smoke evidence and accounting

Accepted GOAT usage retains `reasoning_tokens` and `reasoning_tokens_exposed`.
Only an exposed valid subtotal (0 <= reasoning <= completion) produces
`visible_completion_tokens = completion_tokens - reasoning_tokens`. Unexposed
reasoning is unknown, never zero by assumption. Charged completion usage remains
unchanged. Durable raw-response replay recovers these diagnostics with zero sends;
older accepted envelopes can be enriched only when all original accounting fields
match exactly. Run logs, saved stage stats, physical-call budget records and
trial/recovery `accepted_provider_responses` retain the diagnostic evidence.
The latter includes replay records separately from new physical Provider calls.
Aggregates expose a reasoning subtotal only if every paid component exposes it.

GOAT cumulative progress owns explicit `provider`, `accounting`, `pricing_unit`,
`pricing_version`, and `provider_invoice_claimed=false` metadata. The compatibility
fields `estimated_cost_usd` and `rate_correct_cost_usd` then mean credit-equivalent
USD under the frozen Command Code public rate card, not a Provider invoice.
Legacy progress without provider metadata remains direct-DeepSeek history. Only
zero-accounting direct/legacy state may adopt GOAT; nonzero cross-provider totals
fail closed before credential activation and before accounting mutation. Existing
direct progress retains its original semantics and metadata. This does not
relabel previously committed smoke or historical burn-in files.

Local short-output QA compares non-whitespace source and English character
counts. Nonempty output warns when source size is at least 40 characters and
English size is below 35% of source size. This conservative severe-truncation
screen supports Chinese and Korean and permits complete short prologues,
including the 215-source / 471-translation smoke dimensions. Empty translations
remain errors; residual-script and locked-term checks remain active. This is a
local heuristic, not a claim of semantic completeness.

### Baseline classes for future experiments

An execution-pristine baseline has no paid execution history. It is not
necessarily chronology-pristine: a mature initial glossary can contain knowledge
from later chapters. Those historical files remain frozen. The future GOAT 5+5
burn-in and later chronological multi-model experiment must explicitly bind their
chosen baseline class and initial glossary/continuity knowledge; this PR does not
redesign either benchmark.

The selected GOAT primary translation is **DeepSeek V4.1 Flash / low / 50K**,
with SDK `max_retries=0`, no fallback, and no automatic High reasoning escalation.
See [50K production policy](GOAT_PRODUCTION_50K_POLICY.md) for the versioned policy,
one-shot cap recovery, source-aware residual repair, and historical compatibility.

[Historical PR146 v1](GOAT_REASONING_SATURATION_RECOVERY.md) remains the frozen
24K blank/all-reasoning recovery contract. It cannot authorize a new 50K send.
