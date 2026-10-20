# Production operation policy

`production_operation_policy.py` describes the active production request controls
as immutable, validated dataclasses. `policy_for_model("v41-flash")` returns a
`ProductionOperationPolicy`; `for_operation(id).request_controls()` returns fresh
provider kwargs. `identity()` is deterministic JSON data; `identity_sha256()`
uses sorted compact JSON. Schema 1 / `production-operation-policy-v2` identifies
this architecture. Unknown operations, incomplete/duplicate catalogs, cyclic
followups, invalid budgets, disabled thinking with reasoning effort and unsupported
reasoning values fail closed. HIGH is never authorized by this policy.

The catalog describes limits, not permission to send. The existing journal,
model-identity guard, scheduling and domain protocols still enforce authorization,
replay, eligibility, mutually exclusive branches and durable attempt limits.
The focused translation recovery protocol is described below. Every current operation uses **disabled**
thinking and **no reasoning_effort**. Each catalog node permits one logical
attempt; repeated audit subdivisions are separately identified domain chunks.

## Model policy and operation policy

`translation_model_config.py` owns model identity/version, aliases, availability,
request cutoffs, translation allowance, selected thinking and pricing. The model registry, runtime model selection and provider identity guard remain authoritative.
Pro remains the default; its existing routing cutoff still blocks unsafe new
requests. V4.1 Flash remains an explicit opt-in (`v41-flash` / `deepseek-flash`),
with model `deepseek-flash`, version `DeepSeek-V4.1-Flash`, 12000 tokens and disabled
thinking. Retired V4 Flash remains resolvable for historical recovery, never new
paid requests. Benchmark models are not added to production selection.

The operation layer snapshots request controls without copying pricing or route
logic. The engine's `current_production_operation_policy()` reads already-prepared
runtime translation globals and the authoritative state policy. It does not select
a model. This preserves the launcher/core preparation seam and isolated experiment
adapters. The public model factory includes exact model/version binding; the engine
snapshot binds its actual requested model and the canonical version from the same registry.
Unregistered historical experiment adapters retain their existing controls without a
fabricated version or translation recovery. Provider request model continues to
come from the established `MODEL` value and is guarded exactly as before.

## Complete active request inventory

All rows below use disabled thinking, reasoning None, and a single application
attempt per identified request. Ceilings are completion/output tokens.

| Operation ID | Ceiling | Mode / bounded followup |
|---|---:|---|
| chapter_translation | selected model: 12000 | Normal |
| chapter_translation_recovery | 24000 | V4.1 Flash only; one fresh full translation after durable substantive LENGTH |
| state_extraction | 4000 | Normal |
| state_extraction_recovery | 8000 | One recovery for length or blank STOP |
| state_json_repair | 5000 | One repair after substantive malformed state and failed local salvage |
| chapter_glossary_review | 4000 | Original review |
| chapter_glossary_review_json_repair | 5000 | One generic-format repair of malformed original review |
| chapter_glossary_review_schema_correction | 2000 | Eligible schema correction; shared primary correction budget |
| chapter_glossary_review_semantic_correction | 2000 | Eligible semantic correction; shared primary correction budget |
| chapter_glossary_review_semantic_correction_retry | 2000 | Existing additional correction only for an eligible ineffective semantic patch |
| periodic_audit | 4000 | One request per identified chunk |
| periodic_audit_json_repair | 4000 | One malformed nonlength chunk repair |
| periodic_audit_semantic_correction | 4000 | One allowlisted field correction per chunk |
| residual_cjk_micro_repair | 800 | Existing bounded anchored patch |
| residual_cjk_micro_repair_correction | 800 | Existing eligible corrective retry |
| strict_terminology_micro_repair | 800 | Existing bounded terminology patch |
| strict_terminology_micro_repair_correction | 800 | Existing eligible second attempt; total domain maximum two |
| glossary_candidate_conflict_adjudication | 800 | One selection-only adjudication; no repair/retry |
| generic_json_repair | 5000 | Compatibility entry point; current active caller is original glossary review |

The review schema/semantic alternatives cannot both spend their primary correction
budget. Only the existing semantic ineffective-patch rules can permit its second
correction. The catalog records distinct operations rather than presenting that
second correction as an unbounded retry. `chapter_glossary_review.py` remains the
authoritative lifecycle/eligibility owner. Residual and strict repair similarly
retain their existing domain guards. The shared micro-repair transport now selects
the appropriate operation and correction node using the domain's `request_kind`.

Audit length responses still subdivide the failing chunk without JSON repair;
chunk sizes, maximum serialized input, target counts and audit intervals are
unchanged. Audit repair is **4000**, not generic JSON repair's 5000. Adjudication
retains its direct scheduled single-send path. Every active raw paid send in the
legacy production module is either journaled translation/state work or one of
these existing scheduled domain callbacks. Historical standalone scripts and
benchmark-specific adapters remain outside this catalog.

## State integration and compatibility

`StateExtractionPolicy` is the sole authority for 4000 / 8000 / 5000 and its
thinking/reasoning controls. The top-level policy references the exact immutable
state object and derives its three request nodes; it contains no duplicate state
budget constants. PR #135's classifier, salvage, recovery, journal callbacks,
validation and pending preservation are unchanged. Length and blank STOP allow
one recovery. All other non-stop reasons fail closed immediately without repair.

Current state checkpoint identity still binds `StateExtractionPolicy.identity()`
unchanged. Historical 3000-token state journal and checkpoint matching stays in its
explicit existing compatibility path. It never creates another initial slot to
bypass an uncertain historical request. Legacy kwargs remain byte-equivalent.
The extra operation-policy event describes configured controls; the existing state
attempt event separately reports the actual replayed request ceiling (including
3000). Reading a historical record does not authorize a new 3000-token request.

Provider kwargs contain no policy revision/hash metadata. The existing journal
already hashes the entire request payload, including ceilings, thinking, model and
messages; unchanged requests keep the same request and operation identities.
Original chapter-review checkpoint inputs remain exactly 4000 / 2000 / disabled.
Existing state/review checkpoint identity is unchanged. Recovered pending metadata
additionally binds its translation recovery policy and exact paid response chain. The
provider model guard and pending-model resolver retain their existing behavior.
Already-paid pending text is still preferred and never regenerated.

Runtime manifests automatically include the new root Python module. As with any
engine edit, previous executable recovery/trial **approvals** that pin engine bytes
must be re-planned; this is distinct from changing paid journal/checkpoint identity
or discarding paid pending work. No approval weakening was introduced.

## Observability and later experiments

The engine emits a compact `production_operation_policy` event when constructing
controls with an initialized logger: operation kind, policy revision, completion
ceiling, thinking type, optional reasoning effort and normal/recovery/repair/
correction mode. It carries no messages, source text, translation, credentials,
pricing or huge policy object. Existing response, replay, accounting and domain
lifecycle events remain authoritative. The event means controls were selected,
not that another send occurred; journal replay can make zero sends.

A future reviewed experiment can replace one immutable auxiliary node, or supply
one state subpolicy, and compare deterministic policy identities. Translation
controls continue to derive from model policy. The V4.1 Flash factory now enables
one recovery node with mode `recovery`, parent `chapter_translation`, trigger
`durable_length_response`, 24000 tokens, disabled thinking and reasoning None.
Pro and retired V4 Flash retain `translation_recovery=None`. Recognition of LOW
for the existing state-policy representation does not enable reasoning; all
factory/runtime defaults remain disabled/None.

## Translation saturation recovery

`production_translation_recovery.py` owns the focused classifier/result protocol;
the engine supplies the existing durable journal and provider-model guarded
transport. Only explicit `deepseek-flash` / `DeepSeek-V4.1-Flash` with reviewed
12000-token disabled-thinking controls can use the 24000-token recovery. Normal
STOP requests retain their historical messages, kwargs and journal identity.

Recovery requires an exact reconstructed initial request and a validated durable
`response_recorded` LENGTH envelope containing substantive visible English. A
local re-read proves that operation ID and entire response match the returned
journal envelope. Blank STOP, unexpected finish reasons, tiny/non-English-looking
fragments, corrupt/stale records and uncertain initial sends never recover.

The initial journal is immutable paid evidence. Recovery uses the distinct fixed
`chapter_translation_recovery` slot and the exact original full messages, changing
only output controls. Its existing `translation_sha256` identity component hashes a
compact binding of initial operation ID, entire initial response-envelope SHA-256,
and the translation-recovery-specific policy SHA-256. The provider sees no policy
metadata or partial translation.
Completed recovery replays locally; STARTED/UNCERTAIN blocks resend. Recovery
LENGTH, blank STOP or unexpected finish is terminal, with no third request.

Only nonblank recovery STOP becomes pending English and passes the same downstream
QA/state/review/commit path. Accepted initial and recovery usage are aggregated once
for chapter accounting, retaining both operation IDs and each operation's actual
price snapshot. Mixed-window charges are never repriced as a single operation.
Unrelated state/glossary/audit/micro-repair budgets and the global production policy
revision do not participate in recovery identity. Its dedicated schema/revision binds
model/version, both translation operation nodes, strategy and saturation thresholds.
Pending aggregate validation checks both ordinary token/rate/cost records and their
exact sum; chapter commit remains idempotent. Uncommitted failed spend remains in
its journals, and ambiguous charges remain unknown.

Recovered pending writes persist metadata first with the transient
`translation_recovery_recorded` stage, then final Markdown, then the normal
`translated` stage. Startup accepts that transient prefix only after proving both
completed journal hashes, operation identities, provider models, response chain,
accepted usage and any present Markdown against the recorded recovery output.
Missing Markdown still requires exact current request reconstruction and zero-send
journal replay before creation. Other orphan metadata remains invalid. This prevents
paid text from becoming reusable without its aggregate accounting or model pin.
The shared read-only prefix loader is also used before fresh-process runtime model
selection: proven JSON-only paid V4.1 recovery overrides a new/default Pro request.
Malformed/unproven orphan JSON cannot select a model and blocks execution.
Ordinary Markdown pending write order and old pending compatibility remain unchanged.

See [TRANSLATION_SATURATION_RECOVERY.md](TRANSLATION_SATURATION_RECOVERY.md) for the
complete protocol, provenance fields and separately authorized validation plan.
Implementation validation is zero-provider only. Direct DeepSeek acceptance of a
24000 ceiling must be independently validated after review and before merge.
