# PR 15: source-bound complete public CI repartition

Local correction from accepted `0814f211e4ee645c2685ca9ae4eb3e61c0fadf58`, tree
`ad224c016d3146511f09422ab5bb242754214785`, on the existing
`codex/aut2-pr15-hosted-l3-reconciliation` branch. Its parent is
`a4f66eebbc34e6d5ba7d591b7485d82a5d172c19`. The accepted commit is the backup point.
D1/D1.1 remains on `codex/aut2-d1-publication-freshness` at
`c10f0b9aa13abc133c2b4fc7da560fa64506becf`.

This is local evidence for independent Hermes review. It is not hosted L3 or L4
qualification. No provider request, real novel, paid run, hosted CI, D2, push,
PR update or merge was performed. Existing timeouts and three Windows Python
3.14.4 shards remain unchanged.

## Source and coverage preservation

Read AGENTS.md, README.md, the latest AUT2_HOSTED_L3_RECONCILIATION.md, current
runner/classification/schedule/reliability contracts, and the independent review
`C:/Users/User/AppData/Local/Temp/aut2_review_0814f21/REVIEW.md` without changing it.
The review accepts functional/safety behavior and leaves hosted performance unqualified.
No root progress or pending directory exists in this engineering checkout; no
private novel state was read.

The retained four-part proposal was checked against actual accepted source before
editing: exact physical source hash, all 22 original function hashes, the ordered
97-node list, every parameter ID and exhaustive unique bucket membership. Its
ordered-node hash uses compact JSON; its function hashes normalize LF. Physical
CRLF and Git LF hashes are recorded separately, never treated as source drift.

`docs/AUT2_FINALIZATION_CASE_MAPPING.json` is the machine-checkable 97-to-97
bijection. It contains every old/new node, function, parameter identity, original
source identity, decorated function hashes and spans, new module hashes, and the
retained proposal digest. Original physical source SHA-256:
`2efc63b46eab54fd4890b445966b5c29cebec0ac20bc79e8bd8128cb39dfead0`.
Original Git blob SHA-256:
`1d444b8184e8175e30a3075b9ad22497b5ecbf6f4c0af7e5371560763b2c1070`.

Every decorated test function is copied exactly, including imports, assertions,
parametrizations and original fixture usage. The original 166-line header/helper
prefix is moved byte-for-byte to `tests/goat_audit_finalization_support.py`.
Every new part copies the original imports, constants and pytest module marker.
`incident` remains function-scoped; its globals and dependency imports are the
same code and objects. No fixture depends on the importing test module identity.
A repository-wide search of the accepted Git tree found exactly two external
imports, both in `tests/test_goat_batch_plan.py`; only their module path changes,
including the existing `audit_incident` alias and re-exported `local_module`.
Regression checks compare the actual original Git blob, exact normalized function
and helper bytes, decorators/AST, imports, parameter identities and consumer diff.

All 236 accepted root runtime files are byte-identical, including the four
optimization files: historical_reconciliation.py,
production_periodic_audit_finalization.py, production_historical_lineage.py and
goat_periodic_audit_finalize_plan.py. No accepted proof behavior changed.

## Complete public model and provenance

There are 197 public modules: the original 192, plus the two post-publication
experimental-proof/standalone-receipt modules, plus the net three modules from
splitting one module into four. All original classifications, two private and
seven historical exclusions, and all 15 deselections/reasons are unchanged.
The real-DPAPI node remains exactly
`tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip`.

The runner is unchanged. `measured_shards()` constructs all three buckets by
descending measured module cost with path ties, then least-loaded bucket with
index ties. No bucket is hand-authored. Reliability tests pin the new weight and
partition digests and verify full membership, provenance and source binding.

Original hosted run 39696534289 source identity, artifact digests and old shard
summaries remain immutable. `published_schedule` retains every previous weight.
Existing local GOAT-unattended provenance is retained. Each newer local record
explicitly says `hosted_run_id: null`, records source hashes and retained artifact
digests, and describes its duration basis. `runtime_qualified_commit` identifies the
accepted engine version. Repartitioned test files are bound by their own hashes
and the pre-run working-tree manifest; they did not exist at that baseline
commit. Original measurement checkout identities remain in retained artifacts.
No new local timing is described as hosted. No hosted calibration discount or
profiler subtraction is used.

The complete 73-case historical-lineage measurement (2751.51 s) is reused with
its unchanged test hash, JUnit hash, reviewed-source manifest and accepted report.
It predates the optimization: no inferred speedup is applied and no whole-module
rerun was made. Hermes's complete 351-case retroactive result (680.26 s) is used;
the author's 162-case integration subset is explicitly not a full-module weight.
That independent review retains its wall summary but no JUnit/phase artifact.
A fresh 223-case run binds all 72 auxiliary cases, nine G10 rehearsal cases,
96 batch cases, 35 experimental-proof cases and 11 standalone-receipt cases to
the exact current source. The retained complete publication measurement covers the
194-case publication EPUB module (193 passes and one unchanged private opt-in
skip, never counted as a pass). See each schedule record for exact source and
measurement provenance. Every untouched weight remains independently pinned to
the original published value.

## Allocation and uncertainty

Nominal weights include fresh finalization setup/call/teardown sums plus a
conservative one-second collection allowance per module. The main projection
adds 25% to all module costs plus 60 seconds per shard for unattributed collection,
guard/telemetry/session overhead. Sensitivity uses 30% plus the same 60 seconds.
These are planning allowances from sparse Windows measurements, not confidence
intervals or a hosted guarantee. A 10.41-second full collection and a 0.35-second
source-control check overlapped the long local run; no contention discount is
applied. Queue/install/job setup is outside the unchanged
4500-second test execution deadline.

| Shard | Modules | Nominal s | 25% + 60 s | Headroom s | 30% + 60 s | Largest weighted modules |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 64 | 3200.201 | 4060.252 | 439.748 | 4220.262 | `test_completed_uncertain_history.py` (2751.51 s), `test_goat_state_terminal_recovery.py` (161.28 s) |
| 1 | 67 | 3200.200 | 4060.250 | 439.750 | 4220.260 | `test_goat_periodic_audit_finalization_part_4.py` (942.72 s), `test_goat_periodic_audit_finalization_part_1.py` (895.85 s) |
| 2 | 66 | 3200.194 | 4060.242 | 439.758 | 4220.252 | `test_goat_periodic_audit_finalization_part_2.py` (911.20 s), `test_goat_periodic_audit_finalization_part_3.py` (896.73 s) |

- Shard 0: Retained full pre-optimization history timing and Windows filesystem variability; no inferred optimization discount. Modules: `tests/test_completed_uncertain_history.py`.
- Shard 1: Combined fresh finalization groups measured on one local Windows host; hosted filesystem/order variability is unmeasured. Modules: `tests/test_goat_periodic_audit_finalization_part_4.py`, `tests/test_goat_periodic_audit_finalization_part_1.py`.
- Shard 2: Combined fresh finalization groups measured on one local Windows host; hosted filesystem/order variability is unmeasured. Modules: `tests/test_goat_periodic_audit_finalization_part_2.py`, `tests/test_goat_periodic_audit_finalization_part_3.py`.

Older hosted batch journal/ledger and experimental module weights remain another
uncertainty. Import caching, changed order and one-host sampling can affect every
shard. Only a separately authorized exact-source hosted run can resolve these.

| Finalization module | Cases | Actual phase seconds | Scheduled seconds (+1 s) |
| --- | ---: | ---: | ---: |
| `tests/test_goat_periodic_audit_finalization_part_1.py` | 26 | 894.847 | 895.847 |
| `tests/test_goat_periodic_audit_finalization_part_2.py` | 20 | 910.199 | 911.199 |
| `tests/test_goat_periodic_audit_finalization_part_3.py` | 31 | 895.733 | 896.733 |
| `tests/test_goat_periodic_audit_finalization_part_4.py` | 20 | 941.720 | 942.720 |

## Changed-file inventory

- `public_inventory.txt`: replace the old module and add the new public artifacts.
- `tests/public_test_classification.json`: four replacement public modules and two new public suites; existing statuses/deselections unchanged.
- `tests/public_shard_schedule.json`: complete measured-LPT model, immutable old provenance and explicit local refreshes.
- `tests/test_goat_periodic_audit_finalization.py`: removed after exact source extraction.
- `tests/goat_audit_finalization_support.py`: unchanged original helper/fixture prefix.
- `tests/test_goat_periodic_audit_finalization_part_1.py`: 26 original cases.
- `tests/test_goat_periodic_audit_finalization_part_2.py`: 20 original cases.
- `tests/test_goat_periodic_audit_finalization_part_3.py`: 31 original cases.
- `tests/test_goat_periodic_audit_finalization_part_4.py`: 20 original cases.
- `tests/test_goat_batch_plan.py`: exactly two helper import paths.
- `tests/test_public_ci_reliability.py`: refreshed frozen allocation and two new source/provenance controls; existing safety controls retained.
- `docs/AUT2_FINALIZATION_CASE_MAPPING.json`: complete source-qualified case bijection.
- `docs/AUT2_PUBLIC_CI_REPARTITION.md`: this report.
- `docs/public_ci_reliability.md`: point to current model and label old projections as historical.

## Validation and handoff

Every execution uses C:/Python314/python.exe 3.14.4,
tests/zero_provider_validation.py, a fresh external TEMP child, no cacheprovider,
and explicit real-DPAPI deselection. Full public collection uses all 15 existing
reviewed deselections; focused 97/223/reliability executions do not add any
selection exclusion. Network/provider, real DPAPI, credential files and private
roots remain denied by the unchanged guard.

The final guarded public collection selected **6045 of 6060 cases**, with exactly
the original **15 deselections**, across all **197 public modules**. Every public
module has selected cases and all 97 mapped finalization nodes occur exactly once. The two new CI reliability
controls add coverage; no existing case or safety assertion is removed. No new
skip, xfail, test exclusion or dropped parameter is introduced. Existing public
paid-journal, writer-lock, source-ownership, publication and replay modules remain
selected. Runner/telemetry negative controls still require exact collection,
source stability, mandatory deselection and terminal completion; missing phases,
source drift and incomplete/zero-exit sessions fail closed.

| Executed selection | Tests | Failures | Errors | Skips | JUnit session s |
| --- | ---: | ---: | ---: | ---: | ---: |
| `finalization97` | 97 | 0 | 0 | 0 | 3643.090 |
| `refresh223` | 223 | 0 | 0 | 0 | 287.979 |
| `reliability` | 279 | 0 | 0 | 0 | 92.105 |

All seven changed Python files pass `python -m py_compile`; `git diff --check`
passes. The 279-case reliability validation took 92.18 seconds in the pytest
summary. Its retained hosted nominal weight is 78.330 seconds; the model's 25%
allowance gives 97.912 seconds, covering this observed local variation. No new
hosted measurement is implied by that comparison.

`final-validation.json` in the external evidence directory machine-checks the
6045-case collection, exact deselections, 291 unique finalization phases, all
599 unique executed cases, frozen source, D1 tip and complete LPT allocation.
It binds the JUnit/log/phase/node/source manifests by SHA-256.

External diagnostics are retained in
`C:/Users/User/AppData/Local/Temp/aut2-public-repartition-20261027/`.
Original artifacts and the independent review are read-only and unchanged.
The schedule records timing evidence hashes; `coverage-proof.json` records the
full collection/deselection hashes and protected source identities. The final
handoff records syntax, diff, publication guard, exact commit/tree/parent and
clean-state results. These local checks do not establish hosted L3, which remains
NOT VERIFIED. Independent Hermes review of this correction is still required.

Independent Hermes review is the next action:

```powershell
git -C D:/NTOSS_AUT2_C_Lineage_Codex show --stat HEAD
```
