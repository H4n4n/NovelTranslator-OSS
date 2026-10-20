# B2B1: real GOAT journal evidence (no send authority)

## Pre-implementation contract — 2026-10-16

Base commit `ba40ddcf4a2fe312b40d95018f0984696f9a0938`, tree
`23a77c80cb228ce4e06b152faaa6be231d1a3ef2`; local main and remote main
were independently checked. The complete local master charter, B1/B2A designs,
ledger/planner, journal/runtime/transport, G7D accounting, auxiliary bounds,
writer lock/atomic writers and PR164 closure were inspected before coding.
The product direction remains one finite parent authorization for unattended
Chinese/Korean literary translation, living glossary, historical corrections,
bounded recovery and validated EPUB publication.

This is the permitted **B2B1 validator/adapter substage**. It does not implement
B2B2's authorization hook. No runtime, journal or B2A changes; no executor,
activation context, environment switch, credentials, transport or CLI. All
adapter results retain `paid_execution_authorized=false`. A B1 hash is never
spend authority. An independent rollback checkpoint is not implemented here:
**all future sends remain STOP/no-send**, even when evidence validates.

### Existing call path and the required later hook

`install_goat_runtime -> journaled -> run_journaled_paid_request` first persists
PLANNED, calls `prepare_attempt` (wait/pause then UTC clock), enters the current
zero-argument `authorize` context, publishes STARTED, invokes its yielded send
callback once, then publishes raw RESPONSE_RECORDED. Runtime acceptance later
rewrites the response with derived accounting/content diagnostics. This rewrite
changes both physical journal/response hashes and `updated_utc` legitimately.
Domain progress, QA, glossary/audit publication and chapter commit happen later.

B2B2 must expose a frozen copy of exact operation ID, absolute path, immutable
identity, normalized wire body/SHA, provider policy and the *same* request-start
UTC instant at the pre-STARTED seam. It must verify the caller-held novel lock,
original B1 binding/runtime, immutable operator batch ID, ledger and independent
head checkpoint before accepting that context. No nested lock acquisition.
Persist immutable exposure context, then reserve and note attempt durably, then
advance the independent checkpoint, all before STARTED/possible transport.
Any interrupted publication or checkpoint mismatch stops without cleanup.

### Crash/identity transition table

| Surviving evidence | B2B1 observation | Required B2B2 behavior |
|---|---|---|
| No reservation, no journal | No operation | No permission from absence |
| Journal, no reservation | Reject journal-ahead (except exact B1 historical evidence) | STOP |
| Reservation, absent/PLANNED journal | Ledger-ahead/no-send; retain exposure | Never issue replacement permission |
| Intent/tmp/incomplete ledger event | B2A reload refuses | Preserve and STOP |
| ATTEMPT or STARTED, including replace-then-BaseException | Ambiguous/no-send | Retain slot and full exposure |
| Transport occurred, response not durable / UNCERTAIN | Ambiguous/no-send | Never resend |
| Raw RESPONSE_RECORDED, settlement missing | Independently validate receipt locally | Settle once, no transport |
| Derived envelope enrichment | Validate all derivations; same immutable receipt | No new slot/cost |
| SETTLE publication interrupted | Reload B2A; complete pair wins, partial pair blocks | Preserve evidence |
| Missing external rollback anchor / deleted consistent suffix | No rollback guarantee | STOP/no-send |
| Valid settlement, domain commit absent | Paid evidence only | Separately prove QA/audit/commit |

### B2B1 evidence format and limits

The adapter reads complete real journals using production `load_matching_journal`
plus stricter duplicate/nonfinite JSON, path, slot and exact identity validation.
It does not accept caller-supplied receipts or costs. An immutable context file
under `data/goat_batch_evidence/<batch_id>/<operation_id>.json` contains the exact
wire body, identity, UTC request start, policy, G7D pricing and capability/exposure
proof. Its canonical SHA must equal the ledger reservation's exposure evidence
SHA; its amount must equal that reservation. **No production writer for these
context files exists in B2B1**: tests construct disposable contexts; B2B2 owns
their safe publication/anchor protocol. Existing B2A synthetic reservations
cannot be silently migrated to this format.

Rates come from the authoritative G7D decimal strings, with a fresh full-policy
hash check against the compiled accounting identity and current provider policy.
`pricing_snapshot` chooses the UTC period; its rates must agree with those strings.
Exposure uses `completion_exposure` on exact normalized wire bytes; auxiliaries
use the reviewed hard bound, never domain max-token intent. Multiply Decimal
rates by integer token counts, divide by 1,000,000 and ceil to integer nano-USD.
B2A's original parent ceiling continues to round down. Recorded actual is derived
from normalized/accepted durable raw usage, never envelope `cost_usd` assertions.
Unexposed cached usage prices all prompt tokens fresh, following G7D.

Receipt hashes describe an explicitly versioned immutable projection: raw response
and request-start timestamp; full journal identity, attempt context and created
timestamp; original journal schema/status/error. Exclude only derived envelope
fields, physical record hash and enrichment's mutable updated timestamp. Validate
physical production record/response hashes on *every* read first. Then validate
all present enrichment fields against independently recomputed production values
(including the existing legacy-stats subset). Invalid enrichment blocks. This
projection keeps receipt identity stable across legitimate enrichment; it is
not a claim that its hash is the current physical record hash or a signature.

Inventory must match every reservation to one fixed journal slot and context,
and every current journal to a reservation. Historical slots must match the
original B1 initial-evidence **raw file hash**, lie before the parent range, and
pass existing verified completed-disposition inventory checks. They never gain
a reservation or get relabelled as batch spend. Missing/drifting historical
evidence, foreign/tmp files, duplicate operation IDs or special routes block.

Supported validation identities: ordinary `chapter_translation`, `state_extraction`,
`state_json_repair`, domain auxiliary operations and deterministic
`context_<logical>_<wire-sha>` operations, including periodic audits and their
JSON/semantic corrections. Saturation recovery, reasoning-empty glossary recovery,
NO_RECORD_STATE, TERMINAL_STATE and STATE_MIGRATION fail closed: their additional
proofs need dedicated later adapters. No global weakening of B2A `_request`.

The adapter performs no ledger mutation, settlement orchestration or durable STOP
latching itself. Its verified receipt can be passed to existing B2A `settle_locked`
in guarded fixtures; invalid evidence must stop future orchestration. A read-only
success cannot establish lock ownership, paid authority, independent anti-rollback,
chapter success, historical-correction completion or publication freshness.
Windows file fsync/replace tests do not prove literal power-cut durability.

## Local qualification and remaining boundary

Status: **B2B_PARTIAL_VALIDATOR_ONLY**, not B2B_COMPLETE or independent-review
approval. The first focused run retained 53 passes and one failure (unknown raw
usage fields were accepted); exact serializer-shape validation corrected it.
The expanded focused run passed 81 tests; separate import and Chinese/Korean
binding deltas passed 1 and 2 tests. After scaling Decimal precision to preserve
all digits of unusually large counts, the final complete focused run passed
**85 tests in 70.95s**. One curated related run retained **593 passes / 4 failures
in 983.63s**: the external guard incorrectly blocked the existing disposable
`fake.dpapi` stub. An exact filename/location exception for that dummy fixture
(no native DPAPI) allowed the four targeted cases to pass in **3.45s**, with
114 deselected. No second broad run was launched. Those existing modules are
unchanged; the final helper change is covered by the final focused run. Exact
commit/tree, guard counters and full preserved logs are recorded in the external
engineering report. No frozen evaluation was
rerun or rescored; hosted current/frozen CI remains behind explicit push approval.

B2B2 must independently review the receipt projection before relying on it, add
an exact identity-bearing backward-compatible callback, durably publish contexts,
prove the independent expected-head protocol, reserve/note attempts before STARTED,
settle/reconcile every journal before possible sends, and latch STOP on invalid
or ambiguous evidence. Token-bound violations also need explicit STOP evidence
in that orchestration, beyond B2A's existing monetary-overrun latch. The legacy
`state_extraction_recovery` route is deliberately unsupported here alongside the
five named special recovery identities above. No batch coordinator, paid CLI,
automatic restart launcher or production qualification is supplied by B2B1.
