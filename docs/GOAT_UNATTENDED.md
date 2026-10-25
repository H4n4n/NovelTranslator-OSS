# Bounded GOAT unattended translation

The default pair is `command_code_provider` / `v41-flash`: logical
`deepseek-flash`, wire `deepseek/deepseek-v4.1-flash`, Command Code Provider API,
existing Command Code DPAPI credential, credit-equivalent accounting. CLI
selections override `NOVELTRANSLATOR_PROVIDER_BACKEND` and
`NOVELTRANSLATOR_TRANSLATION_MODEL`; unset selections use this default pair.
Bare `--model pro` under the GOAT default refuses with explicit direct-route
guidance. There is no provider or model fallback. Legacy `flash` retains its
V4 Flash recovery identity and cannot send new requests.

This route reuses `production_goat_execution.py`, `GoatBudget`, and the normal
sequential lifecycle with no one-chapter target. It is separate from frozen
GOAT lifetime-batch execution; B2B2 NO_SEND / NO_REPLAY remains unchanged.

## Plan and authorize one run

Start with a clean novel with complete source inventory and validated profile.
Read-only preflight does not load credentials, call providers, or write project
state. Without both operator-selected limits it reports
`BOUNDED_ALLOWANCE_REQUIRED`:

```powershell
python operator_preflight.py --novel my_novel
```

Choose the limits yourself; these variables deliberately have no suggested
amount. Planning returns a fingerprint and approval token binding the novel's
current state, source inventory, profile, provider/model/policy, engine code,
journals, output evidence, and prior authorization receipts.

```powershell
$Novel = 'my_novel'
$Calls = [int](Read-Host 'Maximum physical requests for this run')
$Credits = [double](Read-Host 'Maximum credit-equivalent USD for this run')
python translate.py --novel $Novel --max-api-calls $Calls `
  --max-credit-equivalent-usd $Credits --plan-goat-run
```

Review the plan, copy its `state_fingerprint` and `approval_token`, and execute
with the same limits:

```powershell
$Fingerprint = Read-Host 'Reviewed state_fingerprint'
$Approval = Read-Host 'Reviewed approval_token'
python translate.py --novel $Novel --max-api-calls $Calls `
  --max-credit-equivalent-usd $Credits --state-fingerprint $Fingerprint `
  --approval-token $Approval --authorize-goat-run
```

One authorization permits sequential chapters, glossary review, state extraction
and repairs, terminology/residual-script repairs, periodic audits, checkpoints,
and the existing final audit/history pass through the configured final chapter.
Every new physical operation uses the same budget and durable request journal.
LOW / 50K translation policy and provider-unbounded auxiliary completion policy
are unchanged. Conservative auxiliary exposure reserves the reviewed full
context bound, so a small remaining allowance can stop a run even when its
likely response would be cheaper.

The real novel writer lock covers plan revalidation, authorization consumption,
runtime initialization, transport, chapter/audit mutation and the final report.
An exclusive file-fsynced receipt under `data/goat_run_authorizations/` consumes
the authorization before credentials are activated. It is one-use evidence, not
a second credit ledger. Never delete receipts to reuse an allowance. A crash,
zero-send refusal, or credential error still requires a newly reviewed plan.
Receipts change the fingerprint even if no paid request began. The shared
executor verifies the caller-held lock instead of acquiring a nested lock.

## Stops and continuation

`ALLOWANCE_EXHAUSTED` is a normal local stop (CLI exit 0), with actual send and
credit-equivalent counters, pre-send refusals, and durable checkpoint details in
the report. It does not mean the novel is complete. The refused request remains
PLANNED, never STARTED/UNCERTAIN. Committed chapters remain committed; any paid
pending translation remains preserved. Provider/integrity failures exit 2.

No allowance is automatically renewed. Re-run preflight, choose new limits,
create a new plan, review it and explicitly authorize another invocation.
Between chapters or before a due/final audit, the new run resumes the same
lifecycle; due audits precede further chapters. A novel with all chapters
committed can still plan a run to finish its outstanding final audit.
Exact eligible RESPONSE_RECORDED journals replay locally without another send
or consuming the new run's physical allowance. STARTED/UNCERTAIN evidence blocks
automatic resend. Pending/partial-commit, rejected responses, saturation, old
policy, and other incidents needing dedicated planners keep those requirements;
this command does not clean up or migrate them. Use the recovery route reported
by `operator_preflight.py`, then replan unattended execution after recovery.

`pause.flag` remains a zero-API local pause. During a run, create it, wait for
MANUAL PAUSE ACTIVE, then interrupt for a planned stop. Remove the flag before
creating a new plan. Process interruption retains existing journal/pending
semantics. GOAT invocations without allowance, fingerprint/token and
`--authorize-goat-run` cannot activate credentials or transport.

## Explicit direct DeepSeek alternative

```powershell
python translate.py --novel my_novel --provider direct_deepseek --model pro
```

This uses `DEEPSEEK_API_KEY`, the existing direct workflow and direct USD pricing.
It requires a compatible novel; defaults never migrate paid histories. Nonzero
direct accounting/direct-pinned pending blocks GOAT; GOAT accounting/pending
blocks direct execution, including explicitly requested incompatible switches.
Cross-provider migration is outside this assignment.

DeepSeek's [official change log](https://api-docs.deepseek.com/updates/) and
[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/), reviewed
2026-10-24, confirm continued V4 Pro service after September 14. The obsolete
planned retirement cutoff is no longer applied to Pro. Exact `response.model`
validation remains: missing identity or Flash returned for Pro fails closed and
preserves durable paid evidence. Historical Flash still cannot send.

## Qualification boundary

Synthetic tests exercise actual entry points and the shared lifecycle with fake
DPAPI and fake transport for Chinese and Korean novels. They do not establish
translation quality, remaining subscription balance, provider model availability,
or exact invoices. Command Code credits, per-model allowances and rate limits
remain external constraints. Local credit-equivalent accounting is an estimate
under reviewed policy, not a provider balance or invoice.

This retains the public baseline's final audit and whole-history behavior.
It does not qualify AUT-2 historical rewriting, downstream regeneration, or
Issue #11 / PR #15 safety. Independent Command Code review and GPT-6 qualification
remain required before production activation. No paid request, private novel,
real DPAPI secret, deployment, push or PR is part of this work.
