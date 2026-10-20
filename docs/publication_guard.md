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

Adding a reviewed module, test or document requires adding its path to
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
