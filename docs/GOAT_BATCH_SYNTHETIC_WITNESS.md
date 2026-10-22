# B2B2B-2: synthetic witness / NO_SEND enrollment probes

Status: **UNTRUSTED SYNTHETIC SIMULATOR ONLY — NO_SEND / NO_REPLAY**.
Issue #5 starts from public commit `6af1375e9c5c86b443b0f96d2ebc75f8748189db`,
tree `05913cda5257dd90823653aa62f245eb6000d0e5`. This is a safety enabler,
not production enrollment, an independent witness implementation, or permission
to execute a paid request. No active production module changes.

## Small design and trust separation

1. `tests/synthetic_goat_batch_witness.py` is a disposable, explicitly untrusted
   local service simulator. The caller must supply an existing empty test-owned
   directory below the system temporary root (or an explicit zero-provider
   wrapper disposable test root). Create is exclusive; reopen uses
   SQLite `mode=rw` and never creates missing history. There is no CLI, provider,
   credential loader, activation setting, or production adapter.
2. Existing B2B2B-1 `Authority`, `Enrollment`, `Checkpoint`, `LocalHistory`,
   `WitnessHead`, serialization, domain hashes and proof names remain unchanged.
   The simulator retains their exact canonical bytes. Its acknowledgment envelope
   and toy ledger use a separate B2B2B-2 synthetic revision/hash domain. Original
   B1 digests remain opaque bytes/strings; no B1 reserialization or regeneration.
3. `qualify_ack` compares an exact, challenge-bound synthetic envelope and returns
   the existing non-truthy `Diagnostic`. Every authority, freshness, transition-
   committed, paid and send predicate stays false, including simulated success.
   `probe_append` makes one attempt; lost, malformed or mismatched ACK means stop,
   without retry, repair, reset or renewed allowance. `BaseException` propagates.
4. Real external authority is unavailable. `BatchNoSendContext` and the exact-type
   runtime check remain unchanged. Neither a simulator, its ACK, its head, nor a
   successful diagnostic can become a runtime batch context. Existing B2B2
   pre-STARTED and settled-replay tests continue to prove unconditional refusal.

## What the simulator demonstrates

SQLite `BEGIN IMMEDIATE`, uniqueness constraints and `synchronous=FULL` serialize
all service operations across separate connections. Lock contention fails closed;
there is no busy retry. Enrollment and genesis are one transaction. Enrollment
identity, grant digest, original B1 digest, ledger identity and the tuple
`(novel_id, storage_id, batch_id)` are individually unique for this store's
lifetime. Exact reenrollment also refuses. There is no delete, renew, migration,
cap-change, epoch-change, reset or history-repair API.

Each append checks the exact retained current head, transition ID and canonical
synthetic evidence inside the same transaction that inserts the successor.
Retained history is replayed from exact genesis on read/append. Missing ancestors,
conflicting IDs, noncanonical evidence, changed counters, hash/parent drift and
unsupported domain assertions stop without editing retained records. An exact
transition-ID duplicate returns an explicitly historical receipt, even if its
head happens still to be current. It cannot satisfy current-ACK qualification.
A caller-retained later head detects consistent suffix/whole-store rollback;
if BOTH service store and caller anchor roll back together, detection is
**UNVERIFIED** and the test explicitly demonstrates that limitation.

Only RESERVE, SETTLE and STOP toy events are supported. Amounts are integer
nano-USD; prompt/completion tokens are nonnegative integers. Reservations retain
physical slots and outstanding exposure against the original shared ceiling.
Settlement requires an open reservation, replaces its exposure with supplied
synthetic actual cost exactly once and retains slot consumption. Supplied actual
above reserved exposure latches the existing `VERIFIED_OVERRUN` proof name; it
remains a synthetic arithmetic label, not a claim of verified provider cost.
STOP reasons are monotonic; STOP blocks all new reservations. Settlement of
already reserved synthetic operations remains possible after STOP. No action
changes source/domain, journal inventory, chapter progress, audit or publication
fields. Unsupported proof-bearing assertions refuse rather than being trusted.

Fault probes cover enrollment/append interruption after insertion, before commit,
and after commit. Before-commit exceptions roll back the complete transaction;
after-commit lost acknowledgment leaves the exact committed event retained.
Hard process exits exercise SQLite journal recovery and fresh-process reads.
This is local database transaction recovery, never paid-request replay or
resubmission. Tests assert exact retained byte/hash ancestry and preservation of
existing source/ledger/runtime evidence through the unchanged regression suites.
No novel prose, PII or provider metadata is printed.

## Guarantees still UNVERIFIED

- **Independence / authenticity / freshness:** all authority records, challenges,
  hashes and receipts are locally forgeable. Echoing a challenge hash is no
  authenticated, replay-resistant network protocol. Read-current success is only
  a simulated current value. Storing a database on the same host is not an
  independent rollback anchor, split-brain defense or external trust domain.
- **Global identity / no reenrollment:** uniqueness holds only for retained
  identifiers in one intact store. Changing every identifier or deleting/restoring
  the whole store cannot establish owner identity or grant revocation. There is
  no authenticated global grant registry or independent tombstone retention.
- **External financial / domain truth:** supplied toy costs/tokens and opaque
  hashes do not prove billing, physical sends, journal completeness, pricing,
  QA, glossary/audit publication, historical correction or chapter success.
- **Hardware durability / distributed availability:** SQLite tests do not prove
  disk/controller power-cut durability, backups, disaster recovery, service
  availability, authenticated operators, key rotation, multi-region consensus,
  linearizability under partitions, or tamper resistance. SQLite may perform its
  own local journal recovery; the application never silently retries an append.
- **Production qualification:** no reserve/attempt/context/settlement coordinator
  or durable production ambiguous-ACK stop latch is implemented. A synthetic
  diagnostic is never authority, even if the caller repeats a probe. L4 and live
  rollout remain unverified.

A real witness requires a separately qualified external service with an
independent durable registry/head/tombstones, authenticated pinned authority and
epoch management, fresh challenge-bound authenticated responses, atomic CAS and
unique-ID semantics across crashes/partitions, independent validation of every
advertised financial/domain guarantee, durable ambiguous-outcome stop behavior,
retention/disaster-recovery policy, adversarial operational qualification and an
owner-authorized limited rollout. Production send integration requires a separate
reviewed milestone and explicit authorization. Synthetic success supplies none
of these requirements.

## Focused validation and handoff

Run under the existing zero-provider wrapper (choose a fresh temp child):

```powershell
python -m py_compile tests/synthetic_goat_batch_witness.py tests/test_synthetic_goat_batch_witness.py
python tests/zero_provider_validation.py --basetemp "$env:TEMP/nt_b2b2b2_focused" --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip -- -q -p no:cacheprovider tests/test_synthetic_goat_batch_witness.py tests/test_production_goat_batch_witness.py tests/test_production_goat_batch_ledger.py tests/test_production_goat_batch_journal.py tests/test_production_goat_batch_boundary.py
python scripts/publication_guard.py --index
```

The new test module is explicitly added to the public classification and shard 2;
existing shard membership, historical exclusions and node deselections are
preserved. The CI partition tests now require 191 public modules, while retaining
the exact original per-shard baseline hashes (excluding the two additive modules). This safety/test addition requires hosted **L3** under the unchanged
risk policy. Public success does not establish L4. After the focused checks,
publish a draft PR, check Actions at most once and stop for independent review.
