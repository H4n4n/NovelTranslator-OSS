#!/usr/bin/env python3
"""Print the test module paths for one CI shard (greedy by file size, weighted for slow modules)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXCLUSIONS = REPO / "tests" / "ci_exclusions.json"
# File size badly underestimates these, and two landing together exceed the job time limit.
SLOW_MODULES = {f"tests/test_goat_periodic_audit_finalization_part_{n}.py" for n in (1, 2, 3, 4)} | {
    "tests/test_publication_readiness.py", "tests/test_retroactive_transactions.py"}
SLOW_MODULE_WEIGHT = 5


def load_exclusions(repo=REPO):
    return json.loads((Path(repo) / "tests" / "ci_exclusions.json").read_text(encoding="utf-8"))


def selected_modules(repo=REPO):
    repo = Path(repo)
    excluded = set(load_exclusions(repo)["excluded"])
    return sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").glob("test_*.py")
                  if p.relative_to(repo).as_posix() not in excluded)


def shards(shard_count, repo=REPO):
    repo = Path(repo)
    buckets = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    def weight(module):
        return (repo / module).stat().st_size * (SLOW_MODULE_WEIGHT if module in SLOW_MODULES else 1)
    # Heaviest first, ties by name; each file goes to the lightest shard.
    for path in sorted(selected_modules(repo), key=lambda m: (-weight(m), m)):
        i = loads.index(min(loads))
        buckets[i].append(path)
        loads[i] += weight(path)
    return [sorted(b) for b in buckets]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True)
    args = parser.parse_args(argv)
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("need --shards >= 1 and 0 <= --shard < --shards")
    print("\n".join(shards(args.shards)[args.shard]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
