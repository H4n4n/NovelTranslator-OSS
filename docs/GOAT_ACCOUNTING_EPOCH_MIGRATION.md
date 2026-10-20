# Explicit direct to GOAT accounting epoch migration

PR #148 post-merge preflight exposed a deliberate activation guard: nonzero
direct DeepSeek cumulative accounting cannot adopt Command Code Provider
(GOAT). Direct costs are USD estimates under peak/off-peak pricing. GOAT costs
are USD **credit-equivalent** estimates under the frozen public rate card;
`provider_invoice_claimed=false`. Adding these domains or converting old direct
USD into GOAT credit-equivalent USD would misrepresent the accounting.

This local-only tool provides one explicit, one-way transition. It never invokes
inference, imports a credential/client module, reads credentials, or loads DPAPI.
Preflight and normal accounting never invoke migration implicitly.

## Version 2 progress schema

`accounting_schema_version=2` requires exactly one `accounting_history` entry
(`sealed-direct-accounting-epoch-v1`, index 0) and one
`active_accounting_epoch` (`active-goat-accounting-epoch-v1`, index 1).
The migration revision is `direct-to-goat-accounting-epoch-v1`; its canonical
policy identity is an independent complete frozen JSON literal in
`production_accounting.py`, returned by `migration_v1_policy_identity()` as an
isolated dictionary. Its exact pinned canonical SHA256 is
`9c2a545f43369a3371e65adab4525e776e8485a485cb99a3e14e53b8e104e1f5`.
Every snapshot read recomputes and checks that pin; changing v1 semantics fails
closed and requires a new migration revision/version. Neither current GOAT
metadata nor a future rate-card dependency edit can redefine this historical
identity. Existing v1 descriptors validate against the frozen snapshot, while
current operation accounting retains its separate current GOAT metadata checks.

The sealed epoch preserves all six old cumulative values, the exact pricing
version, the old rate-correct starting chapter, and the exact operation-ID list
at close. `legacy_field_presence` distinguishes absence from explicit zero or
metadata; absent totals stay absent in historical `totals`. `legacy_metadata`
preserves the original provider, accounting, pricing unit, pricing version and
invoice flag when present. Normalized direct labels do not replace those values.
`source_progress_sha256` identifies the exact original file bytes.
`epoch_payload_sha256` hashes every historical field except itself, using UTF-8
JSON with sorted keys, compact separators, non-ASCII characters preserved and
nonfinite values forbidden. Runtime accounting verifies that hash before binding
or adding, including duplicate auxiliary-operation replays.

The top-level six counters become **active GOAT counters**, intentionally reset
to zero. They remain the engine's compatibility surface. The top-level provider,
accounting, unit, pricing version and invoice flag are the exact GOAT identity;
`rate_correct_cost_start_chapter` becomes the previous `next_chapter`.
No chapter progress or unrelated field changes. The global
`accounted_api_operation_ids` is preserved exactly, including absence; future
auxiliary IDs append to it once. Historical close IDs remain an immutable prefix
of the current registry when originally present.

The migration's starting boundary is fixed in both sealed and active metadata.
Current progress must remain continuous and at or beyond that boundary as
chapters advance. It must not reset the epoch boundary to each current chapter.
History and active metadata must agree exactly on their original boundary.

Direct projects with all-zero totals keep the existing no-history GOAT adoption
path; they do not acquire a fake historical epoch. Native GOAT remains valid.
Unmigrated nonzero direct state still fails GOAT preflight with
`cross-provider cumulative accounting mixing forbidden`. GOAT to direct,
GOAT epoch rollover, and multiple historical migrations remain forbidden.

## Plan and authorization

Run the read-only planner on the intended checkout. The output must be external
to that repository and must not already exist:

```powershell
python goat_accounting_migration_plan.py --novel <id> --repo-root <root> --output <external-plan.json>
```

The deterministic plan binds the executing code's HEAD and code-tree fingerprint,
the selected repository HEAD/root, novel ID/config bytes, exact progress bytes,
parsed state, full pending and paid-journal inventories, proposed after-state,
canonical/byte hashes, migration policy, fingerprint and approval token. A final
readback rejects state drift. The plan SHA covers the entire plan except its own
SHA field. These are integrity/explicit operator approval bindings, not signatures.

Classifications:

| Classification | Meaning |
| --- | --- |
| `READY_DIRECT_TO_GOAT_ACCOUNTING_MIGRATION` | Eligible nonzero direct history |
| `ZERO_DIRECT_STATE_NO_MIGRATION_REQUIRED` | Existing zero-state adoption suffices |
| `ALREADY_GOAT_ACTIVE` | Native or valid migrated GOAT state |
| `BLOCKED_PENDING_PAID_WORK` | Any pending entry, including historical GOAT or legacy prose |
| `BLOCKED_AMBIGUOUS_PAID_STATE` | STARTED/UNCERTAIN or malformed/unrecognized journal evidence |
| `BLOCKED_EXISTING_PAID_JOURNAL` | Otherwise valid PLANNED/RESPONSE_RECORDED records or nonempty journal inventory |
| `BLOCKED_UNRECOGNIZED_ACCOUNTING_STATE` | Accounting identity/schema/boundary invalid |
| `BLOCKED_INVALID_PROGRESS` | Invalid JSON or interrupted progress temp |

Every pending entry blocks this first version, including completed residue;
resolve it separately through the reviewed cleanup/recovery workflow. A Ming-like
Chapter 205 translated legacy/unpinned pending is intentionally blocked. This
change does not resolve Ming 205 or migrate any actual production novel.

Migration-v1 additionally requires an **empty paid-request journal inventory**.
Malformed journals and STARTED/UNCERTAIN have the strongest classification,
`BLOCKED_AMBIGUOUS_PAID_STATE`. Otherwise any valid record blocks as
`BLOCKED_EXISTING_PAID_JOURNAL`, before pending classification. A direct
RESPONSE_RECORDED may contain real paid response usage not yet folded into
progress; sealing progress at that point would omit that operation. A PLANNED
record is unsent but still carries a resumable direct-era identity that must not
cross into GOAT. Resolve/reconcile/retire these records only through a separately
reviewed workflow. Migration never deletes, rewrites, reinterprets or retires
journals. This conservative eligibility gate is code-fingerprint-bound separately
from the unchanged reviewed migration-v1 historical identity.

Execution requires all explicit inputs and the apply flag:

```powershell
python goat_accounting_migration_execute.py --novel <id> --repo-root <root> --plan <external-plan.json> --state-fingerprint <exact> --approval-token <exact> --evidence-root <external-directory> --apply-accounting-migration
```

Without the apply flag there are no project or evidence writes. Apply requires an
**existing empty `.production.lock` rendezvous** and acquires the shared production
lock before rebuilding the plan. This precondition avoids creating another novel
file, preserving the sole persistent novel change as `data/progress.json`.
Existing production writer routes establish this rendezvous; this executor never
creates, deletes or repairs it. Linked/junction state paths, stale temps, changed
code/config/progress, new pending, and any journal inventory fail closed.

Before replacement, external evidence saves file-fsynced exact original progress
bytes and the plan. Inputs are checked again, then the shared binary atomic writer
fsyncs an exclusive unique sibling temp and atomically replaces only progress.
Immediate readback must match the exact expected bytes and validate the schema.
An external receipt binds before/after hashes, approval, plan and migration policy.
No glossary, chapters, QA, continuity, issues, pending, or journal writes occur.

## Restart and recovery

An exact approved after-state plus unchanged code/config/inventories and original
external backup returns `ALREADY_APPLIED` with zero project writes. It can finish
an absent receipt after a crash. Changed active totals or any other drift refuses
stale plan replay. No second epoch is constructed. Existing interrupted progress
temps block and are preserved for independent review, with no guessed cleanup.
Conflicting external evidence also blocks. An exact visible replacement after an
ordinary replace error is treated as committed and verified, never rolled back.

Before migration the evidence directory retains exact original progress bytes.
After migration history retains exact accounting values and the source SHA. Do
not manually edit history or restore old progress over newly paid GOAT work.
There is no automatic reverse migration. If post-replacement verification or
receipt persistence fails, preserve all state and rerun only the exact plan when
the exact after-state is independently confirmed.

The cooperative lock cannot serialize external editors. The shared writer does
not claim parent-directory fsync or power-loss metadata durability. Hashes detect
accidental corruption, not a deliberate rewrite of every hash and provenance
marker. Removing all epoch fields makes a file indistinguishable from native GOAT
using progress alone; external evidence remains authoritative for detecting that
manual tampering. Partial history stripping fails runtime validation.

Status, project validation and production reporting show sealed direct USD and
active GOAT credit-equivalent estimates separately, never a combined lifetime
cost. Recent GOAT chapter averages require exact GOAT accounting metadata and
exclude entries before the fixed migration start; unlabelled legacy log entries
are excluded. GOAT preflight can reach the ordinary bounded planning route after migration,
but this grants no paid execution authorization. Existing explicit fingerprint,
token and allowance gates still apply.

## Credential-free validation

```powershell
python tests/zero_provider_validation.py -q tests/test_goat_accounting_epoch_migration.py --basetemp=$env:TEMP/nt-epoch-check -k 'not test_real_windows_dpapi_round_trip'
```

All apply tests use synthetic disposable projects. The new import-boundary test
rejects client, credential, transport and real DPAPI imports. The broad suite uses
the existing zero-provider guard and leaves frozen G11 CI unchanged.

Rework coverage independently pins every v1 policy field and the exact SHA,
checks current metadata mutation/replacement and a fresh-process rate-card reload,
and proves snapshot copies cannot mutate validation identity. Altered v1 semantics
fail the pinned hash. Valid direct PLANNED and RESPONSE_RECORDED fixtures are
independently accepted by journal inspection before the planner blocks them;
executor rejection preserves all project/journal bytes and creates no evidence.
STARTED, UNCERTAIN and malformed journal priorities remain covered, as does
zero-journal READY. New valid journals after a READY plan also block stale apply.
Selected-root/progress symlink checks use disposable fixtures and skip only when
the test host cannot create symlinks.

Rework validation on Windows: migration tests **87 passed, 2 skipped**;
required LOW/50K, PR146/147, residual-CJK and preflight regressions
**649 passed, 2 skipped**; broad guarded suite **4,314 passed, 20 skipped,
1 deselected**. The two optional symlink cases lacked host privilege. Real Windows
DPAPI was explicitly deselected. Frozen G11 modules remain assigned to their
unchanged historical CI job. The broad local run used a process-only Git ownership
exception for the preserved G10H historical checkout, with no global Git change.
Changed Python modules passed `py_compile`; `git diff --check` passed.
