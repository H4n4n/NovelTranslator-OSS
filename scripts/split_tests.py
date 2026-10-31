#!/usr/bin/env python3
"""Print the test module paths for one CI shard (greedy by file size)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXCLUSIONS = REPO / "tests" / "ci_exclusions.json"


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
    # Largest first, ties by name; each file goes to the lightest shard.
    for path in sorted(selected_modules(repo), key=lambda m: (-(repo / m).stat().st_size, m)):
        i = loads.index(min(loads))
        buckets[i].append(path)
        loads[i] += (repo / path).stat().st_size
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
