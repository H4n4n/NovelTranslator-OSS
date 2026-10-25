# GOAT default and bounded unattended engineering validation

Date: 2026-10-24 (America/Lima). Local engineering only; no production activation.

## Immutable source

- Worktree: `D:/NTOSS_GOAT_Unattended_Codex`
- Branch: `codex/goat-default-unattended-20261024`
- Parent: `e1d99f0f191efcacfd2265f079e611407ef018f4`
- Parent tree: `0c5184b8d75c993ee2b3f29f7d299018e5830047`
- Public main matched this identity at initial verification and before commit.
- The original local checkout was at a different commit; it was not changed.
- The new isolated checkout contained no novel production state. All mutation
  tests used disposable synthetic novels in external system TEMP directories.

## Implemented boundary

Default selection is GOAT V4.1 Flash: `command_code_provider`, `v41-flash`,
logical `deepseek-flash`, wire `deepseek/deepseek-v4.1-flash`. CLI selections
and explicit environment selections remain authoritative; unsupported pairs
refuse. Direct Pro requires compatible explicit provider/model selection.

The existing controlled-trial planner has an unattended mode, bound to the
complete source inventory, current novel state/output/journals/receipt inventory,
selected profile, provider policy, engine runtime and exact operator limits.
`translate.py` exposes plan/execute via the same fingerprint/token protocol.
The shared GOAT executor runs the existing lifecycle without a chapter target.
One exclusive, file-fsynced receipt consumes each authorization before credential
activation. The actual caller-held novel lock covers all execution and mutation.
There is no new engine, scheduler, provider adapter, cost ledger or migration.

GoatBudget enforces every physical send before STARTED, including auxiliary and
audit requests. Exhaustion is a local ALLOWANCE_EXHAUSTED report, with send/cost
counters, pending chapter numbers and durable checkpoint details. A new reviewed
plan and authorization is required for another run. Eligible saved responses
replay locally; ambiguous evidence never resends. Existing pending and incident
recovery planners remain mandatory where previously required. No fallback or
allowance refill is introduced. LOW / 50K and auxiliary policies are unchanged.

The obsolete planned Pro cutoff was removed based on current official DeepSeek
updates/pricing. Exact response.model checks and direct USD accounting remain.
See [operator commands and supported boundaries](GOAT_UNATTENDED.md).

## Zero-provider validation

Windows Python 3.14.4. Every pytest invocation used
`tests/zero_provider_validation.py`, a fresh GUID-named child of system TEMP,
and the mandatory explicit deselection:
`tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip`.
The guard clears credentials/live controls, blocks provider network access and
real DPAPI, and denies private production/benchmark roots. No real provider
request, real secret read, paid experimentation or private novel access occurred.

Final successful selections (overlap is intentional; do not sum as unique tests):

| Selection | Result | Exit |
| --- | --- | --- |
| Model config/runtime, translate selection, exact response-model runtime, provider contract, production-path/operator preflight, controlled-trial plan/execute, GOAT runtime, launcher shadow | 365 passed | 0 |
| New unattended integration plus direct pending recovery plan/execute and production locks | 96 passed | 0 |
| Frozen-era saturation plus previously failing current GOAT cap/review/state/migration executors, historical disposition, accounting preflight and post-settlement journal cases | 83 passed, 451 unselected | 0 |
| Final partial-stop reporting/shared executor and periodic/final-audit resumption | 9 passed, 78 unselected | 0 |
| New integration file independently | 15 passed | 0 |
| Stable frozen batch boundary/journal/ledger/witness/plan and synthetic witness suite | 621 passed | 0 |

The new integration suite uses the actual translate entry point and lifecycle,
fake DPAPI and fake transport, with real QA and committed Markdown/QA/progress/
glossary state. Chinese and Korean novels each reach Chapter 26 under one
approval, with paid glossary review, a Chapter 1-25 audit and final audit.
Assertions cover profile isolation, secret non-disclosure, no duplicate committed
translations, zero-send response replay, budget stops before translation/state/
audit, separately authorized continuation, a final-audit-only restart, pause,
process interruption, ambiguity, wrong scope/token, cross-provider histories,
explicit environment/CLI selection and writer exclusion.

The wider diagnostic selection initially reported 1266 passed, 48 failed and
2 pre-existing Windows symlink skips (exit 1). Every failed case was subsequently
covered by successful selections above or the final batch selection below.
Failures exposed old implicit direct-route test contexts, obsolete preflight
expectations, the public private-input presence gate in two lock fixtures, and
a historical fixture late import that retained an old provider identity. Those
fixtures/expectations were corrected without changing production recovery or
frozen assertions. One batch runtime-drift refusal occurred while runtime files
were being edited; stable-code reruns preserve that refusal assertion. No skip
was added and no failed test was hidden by deselection. The two inherited skip
conditions are unchanged and concern unavailable host symlink privileges.

Final batch basetemp: `C:/Users/User/AppData/Local/Temp/b6ce9a32-ac9a-400b-bfe9-c4a679e2af1d`.
The staged publication guard passed for all 493 files. Syntax validation of every
changed Python source and `git diff --check` passed. Final committed-tree guard
and clean-status evidence are returned in the handoff.

## Preserved evidence and qualification limit

Frozen GOAT benchmarks, capability/policy files, historical hashes, lifetime-batch
implementation and test artifacts, and AUT-2 evidence/source remain untouched.
The existing historical fixture harness only preloads the lazy executor so its
monkeypatches are restored correctly; no frozen assertion or evidence changed.
B2B2 remains NO_SEND / NO_REPLAY. The public source inventory adds only the three new document/test paths, as
required by the documented normal development workflow. The guard, secret
allowlist, hooks and CI policy are unchanged. No push, PR, merge or deployment.

No unresolved local engineering blocker is known. Pending incidents still need
their dedicated recovery planner; cross-provider migration remains out of scope.
Synthetic validation does not qualify model quality, available subscription
credits, provider invoicing or AUT-2 historical writer safety. The selected
baseline's final-audit/history behavior is retained. Stop for independent Command
Code review and GPT-6 qualification before production activation.

## Exact changed-file list

- `AGENTS.md`
- `README.md`
- `controlled_trial_execute.py`
- `controlled_trial_plan.py`
- `docs/GOAT_UNATTENDED.md`
- `docs/GOAT_UNATTENDED_VALIDATION.md`
- `docs/OPERATIONS.md`
- `docs/PRODUCTION_PROVIDER_BACKENDS.md`
- `operator_preflight.py`
- `production_goat_execution.py`
- `production_goat_runtime.py`
- `production_provider_runtime.py`
- `public_inventory.txt`
- `run_production_engine.py`
- `tests/conftest.py`
- `tests/test_controlled_trial_execute.py`
- `tests/test_controlled_trial_plan.py`
- `tests/test_goat_accounting_epoch_migration.py`
- `tests/test_goat_unattended.py`
- `tests/test_operator_preflight.py`
- `tests/test_pending_recovery_execute.py`
- `tests/test_pending_recovery_plan.py`
- `tests/test_production_goat_runtime.py`
- `tests/test_production_launcher_shadow.py`
- `tests/test_production_path_preflight.py`
- `tests/test_production_run_lock.py`
- `tests/test_provider_model_identity_runtime.py`
- `tests/test_translate_model_selection.py`
- `tests/test_translation_model_config.py`
- `tests/test_translation_model_runtime.py`
- `translate.py`
- `translation_model_config.py`
