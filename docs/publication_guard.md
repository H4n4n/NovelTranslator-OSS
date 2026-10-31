# Publication guard

`scripts/publication_guard.py` is a fail-closed pre-publication check.

## Modes

- `--index` (default): validates the staged index. The reviewed inventory is
  read from the **staged blob** `public_inventory.txt`, not from the working
  tree. The staged path set must equal the staged inventory exactly.
- `--tree <rev>`: validates a commit tree against that commit's inventory.
- `--push-check`: validates all local branches, tags and `HEAD` (including a
  detached HEAD). It walks **every reachable commit**, not only tip trees, and
  follows every annotated-tag object in a chain. Used by CI.
- `--regenerate-inventory`: rewrites `public_inventory.txt` from the staged index
  (header comments kept, paths sorted). It refuses on any forbidden path, bad
  mode or secret match in the index. Then `git add public_inventory.txt`.
- `--pre-push`: consumes the actual Git pre-push stdin ref-update records and
  validates every newly reachable outgoing commit/tree and annotated tag,
  including detached and custom-reference tips. It recursively inspects nested
  annotated tags and fails closed in a shallow repository because missing
  ancestry cannot be certified.

## Guarantees

- Any Git command failure, missing repository/index/blob, empty index, unmerged
  entry, unsupported mode, symlink/gitlink, invalid UTF-8, binary content,
  prohibited path component (any depth, case-insensitive), unexpected path, or
  detected credential pattern causes a non-zero exit.
- Annotated tag chains are followed object by object; every intermediate tag
  message is scanned before the final target is reached.
- `--push-check` inspects complete reachable history, so a clean tip cannot hide
  a secret-bearing ancestor.
- Deletion-only ref updates are allowed; reviewed file deletion and renaming
  remain supported when the inventory is updated in the same commit.
- Historical commits are validated against their own committed inventories, so
  legitimate deletion and renaming work when the inventory is updated in the
  same reviewed commit.
- Synthetic credential fixtures are allowed only by exact SHA-256 literal,
  scoped to the expected path.
- Base64, short hexadecimal and split-string credential forms are heuristically
  detected for secret-like assignments.
- Diagnostics never print matched secret values.

## Normal development workflow

Stage your change, then run `python scripts/publication_guard.py --regenerate-inventory`
and `git add public_inventory.txt`. In other words, adding a reviewed module, test or document requires adding its path to
`public_inventory.txt` in the same commit. Deleting or renaming a file requires
updating that file in the same commit and removing/renaming the old entry.
Normal `git add` does not stage ignored private directories; `git add -f` does,
and the guard rejects the result.

## Installing the pre-push check

```
git config core.hooksPath .githooks
```

The hook calls `python scripts/publication_guard.py --pre-push`.
GitHub CI runs only after upload, so the local pre-push check is the primary
barrier against accidental disclosure.

## Trust and bypass

Hooks are opt-in per clone. A contributor can remove or modify
`.githooks/pre-push`, `core.hooksPath`, the guard, the allowlist, the inventory
or the workflow, and can use `git --no-verify`. These are not immutable.
Security-critical changes to the guard, hook, allowlist, inventory or workflow
require trusted human review. CI runs after upload and cannot prevent initial
disclosure.

## Limitations (must be paired with human review)

- Cannot prove absence of novel/translation prose or unknown secret formats.
- Encoded, encrypted, split or unfamiliar credential formats are only
  heuristically detected.
- Python audit hooks in the test wrapper are defense-in-depth, not an operating
  system sandbox against arbitrary native code.
- Already-published history cannot be un-published by this guard.
- It is one defense, not a confidentiality proof.


## Bounded full-history scan reuse

PR #15's hosted Windows guard was cancelled within its 10-minute job limit in
`validate_commit` while waiting for another `git cat-file blob` subprocess.
The decision artifact was absent, the test shards were skipped, and the final
gate failed closed. Run: https://github.com/H4n4n/NovelTranslator-OSS/actions/runs/40270348413.
This local correction starts at accepted commit
`57187ab7fc67979c72abf3ee71063bf2a23adcdb`, tree
`b05226770f78ef3b1085353a074d2b5b98f49e40`, on the existing
`codex/aut2-goat-main-integration` branch in `D:/NTOSS_AUT2_C_Lineage_Codex`.
No new engineering worktree or clone is used.

The only optimization is a per-validation-walk dictionary of successful
`(full Git blob object ID, exact path)` scans. The path is part of the key
because synthetic credential exceptions are path-scoped. Before a success can
be cached, the complete bytes are read, their size is compared with the tree,
the Git blob header/content hash is independently recomputed (SHA-1 or SHA-256),
and the original complete content scanner passes. Failures are never cached.

Every commit still reads and parses its own inventory and complete tree;
every entry still undergoes path, membership, kind and mode checks. `ls-tree -l`
continues to query object presence/size for every referenced blob. Missing or
nonnumeric size metadata fails closed, and a cached proof cannot satisfy a
different size. This catches a blob that disappears even after an earlier
successful scan. Every commit message, parent ancestry check and annotated tag
object/message remains independently checked by the original traversal.
All branch/tag/HEAD and pre-push custom-reference traversal is unchanged.

The dictionary stores only IDs, paths and sizes, never file contents. It is
limited to 8,192 entries; when full it is cleared and the current successful
entry is inserted. Eviction causes subsequent full reads, hash checks and
content scans. It cannot omit a commit or path. The dictionary is created
inside each top-level walk, is not shared between separate validations or
repositories, and is never serialized or loaded from evidence files.
Immutable Git object identity within a validation remains the trust boundary;
this is not a cache of filenames, apparent similarity or unchecked digests.

The existing 180-second Git subprocess deadline and 10-minute hosted guard job
limit are unchanged. No timeout or interruption is treated as success. No AUT-2
production code, GOAT policy, workflow, CI classification/exclusion, historical
fixture or frozen evidence file changes in this correction.

### Windows Python 3.14.4 evidence (2026-10-26, America/Lima)

Both complete command-line checks below used the same captured local branch/tag
ref set and HEAD: 34 reachable public commits. No history was truncated or
sampled. The baseline is an untouched copy of the accepted guard and allowlist;
the optimized checker validated the same committed input before its own commit.

| Complete `--push-check --json` | Result | File validations | Elapsed |
| --- | --- | ---: | ---: |
| Original accepted guard | PASS, exit 0 | 16,559 | 392.45 s |
| Bounded scan cache | PASS, exit 0 | 16,559 | 26.44 s |

This is about 14.8 times faster on the local Windows host (Git
`2.55.0.windows.3`). These initial processes overlapped; timings are engineering
measurements, not a controlled cross-machine benchmark. A subsequent complete
instrumented walk took 27.13 seconds and accounted for all 16,559 entries:
15,914 cache hits, 645 independently read/hashed/scanned misses, peak 645 cached
entries. Inventory, tree, message and ancestry verification were not cached.
Hosted Windows performance of the new exact commit remains unverified pending
focused Hermes review and separately authorized publication; no hosted rerun is
started here.

Guarded synthetic verification used fresh external system-TEMP basetemps and
explicit mandatory real-Windows-DPAPI deselection. The complete existing public
CI reliability module passed **276 tests**, with zero skips or deselections.
A final benign/malicious nested-tag selection passed **2 tests**, overlapping one
prior case: **277 distinct passing cases** in the final effective selection.
No public classification, exclusion, shard weight or frozen fixture was changed.

Nineteen parameterized histories compare the frozen accepted guard with the new
guard: repeated blobs, legitimate inventory evolution, missing inventory
membership, duplicate/empty/invalid inventories, forbidden nested paths, changed
unsafe content hidden by a clean tip, invalid UTF-8, binary data, unsupported
modes, unsafe commit messages, a divergent unsafe branch, nested annotated tag
messages, malformed tag evidence, missing blobs/parents, shallow history and
path-scoped exception reuse. Both guards agree on all cases: two acceptances
and seventeen rejections. Benign nested tags and custom outgoing refs also
retain matching positive decisions; malicious intermediate tags retain matching
negative decisions. Additional cases prove per-commit inventory/message work,
forced eviction, invocation isolation, cache failure/size/hash handling,
SHA-1/SHA-256 identity, post-scan object disappearance and timeout failure.

The first focused run recorded 22 passes and two setup failures before the
relevant assertions: native Git refused creation of a malformed tag ref, and
Windows read-only loose-object attributes prevented synthetic deletion. The
fixtures now install only their disposable malformed ref bytes and explicitly
make their own disposable object writable before removal. Both scenarios pass
without changing either guard's policy. Raw console/JUnit results, original
source/refs, measurements, and final candidate/guard identities are retained
under system TEMP at `aut2-publication-guard-20261026/`. Exact new commit/tree and
post-commit guard results are recorded there to avoid a self-referential hash.

For focused independent review, choose a fresh external TEMP child and run:

```powershell
$guardTemp = Join-Path ([System.IO.Path]::GetTempPath()) ('publication-cache-review-' + [guid]::NewGuid().ToString('N'))
python tests/zero_provider_validation.py --basetemp $guardTemp `
  --deselect tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip `
  -- -q tests/test_publication_guard.py
python scripts/publication_guard.py --push-check --json
```

This is a local correction awaiting focused Hermes review. It does not authorize
a push, PR merge, provider call, production mutation, L4 or Issue #18 work.
