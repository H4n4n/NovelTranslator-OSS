# G10F / G10G — Paid Execution Boundary

G10F and G10G are the reviewed paid-execution layer that sits on top of the frozen
G10E gate. They are granular, single-step executors: each external invocation
authorizes **exactly one** frozen paid unit of work. Neither executor ever loops,
auto-advances, or retries.

Adding the modules, building a plan, inspecting readiness, or running the test
suite never authorizes paid execution. Every real transport requires an injected
provider client, an explicitly authorized invocation, and the exact deterministic
approval token for that invocation.

## Shared authorization layer — `benchmark_longitudinal_g10_paid_authorization.py`

`frozen_execution_binding(...)` performs a read-only proof that the durable ledger
and every frozen identity still match the G10E freeze. It pins:

- the exact G10E freeze ID
  `22fd63371c9e38fe7051f0d14194e3d6ded9f07805e48fc6f45b3f325eff25ca`, its file
  SHA-256, and the *genesis* budget-ledger SHA-256
  `b255ddbd5d2a47d03201315438f073d05e8716e569c77e90502d4ee395f7d9ce`;
- the exact matched plan SHA-256 and S0 manifest SHA-256;
- the G10A1 safety contract and the G10E pricing snapshot / rate card;
- the global CE ceiling (`30`) and the arm ceilings (`G10A = 5`, `G10B = 25`).

The ledger is proven in two deliberately separate parts, because `g10_budget.json`
is *legitimately mutable*: every reservation, send-started marker, accepted
settlement, and ambiguity record rewrites it.

1. **Genesis lineage (frozen, re-proven on every open).**
   `CreditReservationLedger.open` already validates the schema, the internal
   `ledger_sha256`, the authorization, the scope, record consistency, and the
   accepted sums; `assert_ledger_genesis_lineage(...)` then recomputes
   `EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256`
   (`08e8ad93d7879dfcfb9ede8c66a095bde15a62b51081acd95b5267ba217ee6ff`) over
   **only** the immutable genesis-identity fields — `schema_version`,
   `revision`, `benchmark_id`, `g10_preregistration_sha256`,
   `g10a1_safety_sha256`, `authorization_sha256`, `scope_id`,
   `maximum_credit_equivalent`, `arm_credit_equivalent_ceilings`, `created_utc`
   — plus a lineage revision marker, proving the current document still carries
   the exact frozen genesis identity. The deliberately mutable fields
   (`accepted_credit_equivalent`, `records`, `updated_utc`, `ledger_sha256`)
   never participate. `open_frozen_budget_ledger` re-runs this proof on every
   open, before the ceiling checks.
2. **Live file (evidence only).** The current file SHA-256 is exposed as
   `budget_ledger_current_sha256`. It is evidence and is never compared to a
   frozen constant, because it changes on every legitimate ledger write.

`budget_ledger_sha256` keeps the historical pristine-genesis file SHA-256 as the
contract evidence bound into every paid authorization; it is the hash of the
genesis artifact, not a claim about the current file.

The literal "reconstruct the genesis bytes and assert they equal `b255dd…`" is
deliberately *not* implemented, and is in fact unsatisfiable:
`CreditReservationLedger.create` re-stamps `updated_utc` inside `_persist` (the
frozen ledger records `created_utc = 2026-09-29T23:51:36.557042+00:00` but
`updated_utc = 2026-09-29T23:51:36.557207+00:00`), and the file is written with
CRLF line endings, so reconstructed JSON bytes can never reproduce the genesis
bytes. The immutable-field lineage digest is the equivalent lineage proof: it is
satisfiable across legitimate ledger mutation (where byte reconstruction is not),
and tampering with any immutable field is still rejected even when the tampered
document's internal `ledger_sha256` is recomputed to match.

It never requires an untouched (zero-commit) ledger, so it remains valid across
restarts after paid requests have already been committed — the second, third, …
paid invocation keeps working after legitimate ledger mutation.

`build_paid_authorization(...)` mints one plan whose deterministic
`approval_token` binds the exact arm, model, provider wire model, novel,
operation, chapter, case/track identity, `request_sha256` (where known before
transport), the current target-state fingerprint, and the finite conservative CE
reservation. The token is therefore not reusable for any other arm, model, novel,
case, track, chapter, request, or state.

`verify_paid_authorization(...)` recomputes and compares the token, re-validates
every bound field, requires the recorded target-state fingerprint to be unchanged,
and requires explicit operator authorization. Only then does
`preflight_and_reserve(...)` check the global and arm ceilings and create the
durable reservation — strictly before any transport.

Accounting helpers convert a reservation to exact accepted CE after a durable
response (`settle_accepted`), keep the full reservation on an ambiguous outcome
(`settle_ambiguous`), release only with proven pre-send evidence
(`settle_pre_send_release`), and write the correct durable quarantine scope
(`quarantine_literary_case`, `quarantine_chronological_track`).

Importing this module performs no filesystem write, reads no credential or
environment variable, opens no socket, and constructs no provider client.

## G10F — matched literary executor (arm `G10A`)

Executes the already-frozen 102-request matched plan. It reuses
`benchmark_longitudinal_g10_matched.build_runtime_cases()` and requires the
resulting matched-plan SHA-256 to equal the frozen value; it never rebuilds a
different corpus.

- One invocation executes exactly one `execution_index` in `1..102`. No automatic
  loop through all 102 inside one authorization.
- Before transport: bind the exact case/pair/model/novel/chapter and provider
  request SHA; open the shared `g10_budget.json`; reserve the conservative G10A CE.
- Transport: exactly one application-visible attempt through the existing durable
  `production_paid_request_journal`, provider `max_retries=0`, no hidden retry.
- On a durable response: accept only the exact returned model plus complete usage,
  convert the reservation to exact accepted CE, and persist the accepted artifact
  and metadata.
- Ambiguous transport: retain the full reservation, create a durable G10A
  `literary_model_case` quarantine for **only** that model/case, and never resend.
- `response_recorded`: local replay only, zero new transport, no second
  reservation/send.
- The paired partner model/case may still execute when one model/case is
  quarantined.

```
build_g10f_invocation(binding, repo_root, evidence_root, execution_index,
                      model_id, target_state_fingerprint_sha256,
                      maximum_credit_equivalent, ...)
verify_g10f_invocation(binding, repo_root, evidence_root, plan,
                       expected_plan_sha256, expected_approval_token,
                       operator_authorized, current_state_fingerprint_sha256)
execute_g10f_invocation(code_root, production_root, freeze_root,
                        matched_evidence_root, s0_root, repo_root, evidence_root,
                        plan, expected_plan_sha256, expected_approval_token,
                        operator_authorized, api_key=None, client_factory=None, ...)
```

## G10G — chronological real-engine executor (arm `G10B`)

Runs the four frozen tracks (`Qwen × Ace`, `GLM × Ace`, `Qwen × Rogue`,
`GLM × Rogue`) through Chapter 50 using the G10B1 planning contract.

- One invocation targets exactly one track and exactly one next chapter. No
  automatic advancement to the following chapter.
- The chapter is executed through the real NovelTranslator lifecycle:
  `run_unattended_production(target_chapter=<exact approved chapter>)`. The
  lifecycle is not emulated or reconstructed, so translation, state extraction,
  JSON repair, glossary review, terminology repair, the Ch25/Ch50 periodic
  audits, safe retroactive corrections, and audit backups/checkpoints all retain
  their real production semantics. At Ch25/Ch50 the lifecycle itself decides and
  runs the real audit; no synthetic audit is ever created.
- Every provider subrequest generated by the lifecycle independently passes the
  durable request journal, the exact `request_sha256`, a conservative
  request-level G10B CE reservation, a single-attempt transport, and exact
  accepted-usage accounting. The outer chapter authorization never assumes a
  fixed number of audit subrequests; budget admission happens request-by-request.
- Ambiguity on any post-send subrequest: preserve the unresolved reservation,
  create a durable `chronological_model_novel_track` quarantine for the entire
  model/novel track, do not retry, and do not advance later chapters on that
  track. Other independent tracks continue in their own invocations.
- A deterministic terminal provider/policy failure follows the frozen policy
  without silently generating a replacement request.
- `response_recorded` local recovery remains local-only and uses the proven G10D
  replay semantics.

```
build_g10g_invocation(binding, production_root, run_id, target_chapter,
                      target_state_fingerprint_sha256,
                      maximum_credit_equivalent, ...)
verify_g10g_invocation(...)
execute_g10g_invocation(code_root, production_root, freeze_root,
                        matched_evidence_root, s0_root, plan,
                        expected_plan_sha256, expected_approval_token,
                        operator_authorized, api_key=None, client_factory=None,
                        production_module_loader=None, ...)
```

`production_module_loader` exists so tests can inject a scripted fake lifecycle
with the same patched symbol vocabulary. The default loader
(`default_production_module_loader`) loads and initializes the **real** import-safe
production engine with the provider suppressed. The injected module still goes
through `install_g10g_paid_runtime`, the journal, the reservation, and settlement.

## Budget

There is exactly one ledger: `g10_budget.json` inside the G10E freeze root. No
second cost ledger exists. Committed CE is accepted CE plus the full reservation
of every unresolved/ambiguous request. Every new transport fails before sending
if its reservation would exceed either its arm ceiling or the global ceiling.

## Operator authorization

Real transport requires an injected API key plus `operator_authorized=True` and
the exact approval token. Merely importing a module, building a plan, inspecting
readiness, or running tests never authorizes paid execution. A plan/token is not
reusable for another arm, model, novel, case, track, chapter, request, or state
fingerprint.

Provider calls during engineering/review: `0`. No credential is read; `.env` and
secret stores are never accessed; no production chapter is executed.

## Local commands

The executors are library surfaces driven by the orchestration layer; there is no
standalone paid CLI. A typical authorized G10F step builds a plan with
`build_g10f_invocation`, then calls `execute_g10f_invocation` with the frozen
binding, the exact approved plan/token, `operator_authorized=True`, and an
injected client. G10G follows the same shape with `build_g10g_invocation` /
`execute_g10g_invocation`. Paid G10A/G10B execution remains a separate explicit
operator authorization boundary.

## Tests

All tests are zero-provider: fake clients are injected through `client_factory`
and the G10G lifecycle is a scripted fake module injected through
`production_module_loader`. Tests import the modules inertly
(`tests/test_benchmark_longitudinal_import_safety.py`) and never read
credentials, construct a real client, execute a production chapter, or mutate a
frozen/production external root.


### Historical matched-plan configuration reconstruction

G10 reconstructs each novel config identity from its preregistered local Git
blob (`config_blob_sha`), verifying the Git object SHA1, UTF-8 JSON object,
and frozen canonical SHA256. `G10PromptBundle.config_sha256` binds the exact
historical blob bytes, not today's mutable production config. Missing/invalid
objects fail closed with no network fallback; lazy Git fetching is disabled.

The current project still supplies source/library/addendum/profile paths and
must match all existing explicit prompt-relevant fields. Production-only config
additions, including Ace's 12/3/3/1 residual repair opt-in, do not change historical
config identity. All other prompt/source/request and matched-plan bindings remain
unchanged. No frozen constant or evidence is regenerated or repinned.

Ordinary CI verifies this config seam through all 102 matched cases using real
current configs/profiles and synthetic carriers for ignored local source files.
It does not claim synthetic source reproduces the frozen plan. Full frozen-plan
reconstruction is verified locally, read-only, where the historical source books
and Rogue chapter library are available; the existing external-root paid-binding
test exercises that proof. This separation avoids adding copyrighted source
fixtures or weakening source/request hashes. CI retains full Git history.
