# AUT-1 unattended recovery

AUT-1 adds a pure supervisor around the maintained chapter recovery stages,
removes a deterministic terminology/CJK sequencing dead end, preserves recovered
state accounting across restart, and permits one proof-checked local audit
publication reconciliation in the same run. It adds no paid send authorization.

## Recovery inventory (baseline audit and AUT-1 changes)

| Failure / trigger | Existing owner and bounded route | Baseline automation / human dependency | Durable result and prohibited cases |
|---|---|---|---|
| Translation LENGTH | `production_translation_recovery.run_translation_stage`; exact recorded initial response and approved model policy | Already automatic for the exact registered V4.1 Flash 12K to 24K route. Not a generic Pro recovery. `production_goat_cap_recovery` separately owns exact GOAT cap eligibility. | Separate identity-bound initial/recovery journals; accepted operation costs retained. No partial stitching, guessed truncation recovery, model fallback, HIGH reasoning, or extra ceiling. Unapproved model/policy or spent recovery blocks. |
| Empty/refusal/unproved partial translation | Translation classifier and mandatory commit QA | Already blocked; remains blocked | Preserve recorded responses/pending. No new whole-chapter generation from a QA failure. |
| Strict term mismatch | `strict_terminology_repair`: deterministic source-backed aliases, recorded candidate validation, bounded micro-patch and existing corrective budgets | Already automatic. **New:** deterministic alias repair may advance through unchanged unrelated CJK blockers using the same `_strict_qa_transition` proof already used for paid patches, avoiding an unnecessary paid terminology call. | Metadata invalidated before pending text write; source/target/line/ownership checks unchanged. Every unrelated blocker must remain identical. Remaining strict failures still block full commit QA. No change to locked/contextual terminology policy. |
| Residual Chinese/Hangul | `residual_cjk_micro_repair`: deterministic annotation cleanup, opt-in bounded patch/correction, rescan and full QA | Already automatic when the novel opts in and ownership/size limits pass; opt-in remains operator-controlled | Pending repair evidence and transport journals retained. Failed ownership, exhausted budgets, unresolved fragments or new QA blockers stop. No opt-in is created by AUT-1. |
| State LENGTH/blank STOP, substantive malformed JSON | `production_state_extraction`: one bounded extraction recovery, local salvage, one compact JSON repair | Already automatic. **New:** accepted direct state recovery/repair components survive checkpoint/restart with their individual rate windows and exact sums. | `state_accepted_operations` stores ordered operation kinds/IDs/stats for two or three responses. Startup/reuse validates the roster, distinct IDs, nesting, rates and sums. No aggregate repricing or historical cost reconstruction. Old unproved checkpoints still block. GOAT accounting keeps its existing owner. |
| Chapter glossary review schema/semantic error | `chapter_glossary_review`, `production_glossary_review_checkpoint`, `production_candidate_preparation` | Existing bounded corrections/adjudication and saved-result reuse remain automatic | Exact source candidate IDs, saved checkpoint identities and shared domain attempt budgets remain authoritative. Identity mutation, unowned candidates and failed transactions stop. No generic exception retry. |
| Periodic audit malformed/length/eligible semantic output | `glossary_audit` chunk checkpointing, split-on-known-LENGTH, existing JSON/semantic corrections | Already automatic; validated chunks survive restart | No live publication until all chunks pass. Unknown/omitted/duplicate candidate identities remain hard stops. |
| Audit local publication OSError | `production_periodic_audit_finalization` durable intent and exact old/new prefix proofs | Previously required another guarded invocation. **New:** supervisor invokes its local-only finalizer once after loading the saved intent; never invokes acquisition again. | Original finalization intent, paid evidence where required, owned-span retroactive transaction, historic chapter hashes, backups and final progress marker must all reprove. Missing intent, drift, ambiguous evidence or a second failure stops. No filesystem failure authorizes another paid call. |
| Chapter commit interruption | `production_partial_commit_reconciliation`, completed-pending cleanup | Existing startup reconciliation remains automatic on restart; no same-process chapter resend/retry is added | Exact final/pending/QA and associated-state proof; progress remains the last durable marker. Failed glossary/continuity commit cannot start the next chapter. |
| Started/uncertain paid request | `production_paid_request_journal` | Blocked; explicit investigation/disposition remains operator-controlled | `response_recorded` may replay locally only under the exact existing identity. SDK retries remain disabled. Neither logs nor supervisor status can authorize reissue. |
| GOAT terminal state / historic review or saturation recovery / lifetime batch | Existing separate plan/executor and `production_goat_batch_boundary` | Explicit authorization remains required; unqualified lifetime batch remains NO_SEND/NO_REPLAY | AUT-1 never calls these planners/executors or changes the unattended GOAT gate. Unsupported authority is BLOCKED_UNVERIFIED. |

## Maintained flow and diagnostics

`production_autonomous_recovery.py` is import-inert and has no client, filesystem,
credentials, schedule or transport capability. The maintained production adapter
supplies existing callbacks and a hash of novel/chapter/source/model/profile/
operation-policy context for chapter processing. Audit diagnostics use only the
novel and explicit audit range, with no chapter-source lookup or fabricated source
hash; source/intent/transaction proof remains in the existing domain finalizer.
Successful pending/checkpoint results also contribute
hashed translation/checkpoint evidence. Exact payload/model/source matching,
durable attempt counters and local replay still belong to domain journals and
validators; supervisor hashes are diagnostic bindings, never proof substitutes.

Each supervisor instance dispatches a stage at most once. Domain callbacks may
perform their own already-authorized bounded suboperations. Restart creates a
fresh supervisor but cannot reset durable domain budgets or paid journal slots.
Known result enums and typed failures are classified without exception-text
matching. Repair-stage blocks still flow to mandatory commit QA; a repair result
never grants commit permission. No general retry loop exists.

Per-novel existing structured logs gain `autonomous_recovery` events with
`recovery_stage`, `disposition`, `reason_code`, `next_safe_step`, context/evidence
hashes, chapter and committed/next progress. They contain no source prose,
response bodies or exception messages. Logging remains best effort; deleting a
log never changes recovery authority. `ALREADY_HANDLED` means the owning stage
accepted/reused evidence, not that the chapter has committed.

Typical stop codes are `AMBIGUOUS_PAID_REQUEST`, `IDENTITY_OR_PROOF_MISMATCH`,
`REPAIR_BUDGET_EXHAUSTED`, `REPAIR_NOT_PROVEN`, `MANDATORY_QA_UNRESOLVED`,
`STATE_RECOVERY_EXHAUSTED`, `STATE_OUTPUT_UNPROVEN`, and
`GLOSSARY_IDENTITY_UNRESOLVED`. Unknown outcomes are blocked/unverified rather
than classified as an eligible retry. Inspect the named domain's preserved
pending/checkpoint/journal evidence and run read-only preflight before any
separately authorized operator workflow:

```powershell
python operator_preflight.py --novel <novel_id> --model pro
```

No new activation command or override is introduced. Existing pause/off-peak
controls and explicit operator controls are unchanged. Normal production still
uses its existing guarded launcher. This PR does not qualify or activate paid
production, historic evaluations, GOAT lifetime batch, or L4.

The lifecycle now rejects callbacks that return success without advancing to the
exact chapter or audit marker. Translation QA, state extraction, glossary review,
continuity/issue publication, durable chapter commit and pending settlement must
finish before the next chronological chapter. Every configured complete
25-chapter audit and the final partial audit remain in sequence. Existing backup
and owned-span retroactive correction paths remain untouched. Their logged
unowned/ambiguous historical referents remain action items; AUT-1 does not
reinterpret rejection as permission to rewrite history.

## Focused evidence

`tests/test_production_lifecycle.py` adds new supervisor and end-to-end tests:

- A 26-chapter Chinese sequence uses the real translation/state journals, cap
  recovery, deterministic strict repair plus CJK cleanup, malformed state repair,
  blank/LENGTH state recovery, commit QA and durable chapter commits. Audit
  scheduling callbacks verify chapter 25 and final boundaries and create real
  checkpoint backups. Separate tests exercise actual audit acquisition and
  proof-bearing local publication with an injected failure and historical drift.
- A three-chapter Korean sequence exercises the same combined repair path.
  Another sequence repairs a real glossary semantic error, commits the new term,
  and supplies it to the next chapter, including restart after a glossary write failure.
- Crash injections before/after state, final prose and progress persistence,
  and failed glossary persistence reload durable files and continue without
  duplicate fake requests. Ambiguous journals, source/model/policy drift, spent
  caps, insubstantial truncation, failed CJK ownership and unresolved terminology
  never advance or resend. Mixed-window state costs are preserved and tampered
  rosters, sums, rates and nesting fail closed.
- Existing focused state/translation/auxiliary journal, commit reconciliation,
  periodic publication, import-safety and GOAT batch-boundary tests remain the
  regression checks. These are synthetic zero-provider tests, not evidence of
  real-provider quality, paid authorization, or independent qualification.

Run under the existing guard (select an unused external temporary directory):

```powershell
python tests/zero_provider_validation.py --basetemp "$env:TEMP/aut1-focused" `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_production_lifecycle.py tests/test_production_state_recovery.py tests/test_production_translation_recovery.py
```

No CI selection policy, classification, shard allocation or Public CI gate is
changed. Existing classified lifecycle tests carry the new cases; conservative
L3 selection remains the expected hosted qualification.
