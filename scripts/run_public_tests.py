#!/usr/bin/env python3
"""Run one deterministic, balanced shard of the reviewed public test subset.

Selection comes from tests/public_test_classification.json. Private/historical
files are never run and are not declared passing. Tests are routed through the
guarded zero-provider wrapper with an explicit external --basetemp and DPAPI
deselection. Empty/invalid selections fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLASSIFICATION = REPO / "tests" / "public_test_classification.json"
WRAPPER = REPO / "tests" / "zero_provider_validation.py"
DPAPI_NODE = "tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip"


def load_classification():
    if not CLASSIFICATION.is_file():
        raise SystemExit("missing test classification: " + str(CLASSIFICATION))
    data = json.loads(CLASSIFICATION.read_text(encoding="utf-8"))
    files = data.get("files", {})
    discovered = sorted(p.relative_to(REPO).as_posix() for p in (REPO / "tests").glob("test_*.py"))
    if set(discovered) != set(files):
        missing = sorted(set(discovered) - set(files))
        stale = sorted(set(files) - set(discovered))
        raise SystemExit(f"classification mismatch: missing={missing} stale={stale}")
    for rel, meta in files.items():
        if meta.get("status") not in {"public", "private", "historical"}:
            raise SystemExit("invalid classification status for " + rel)
    deselect = list(data.get("deselect", []))
    if len(set(deselect)) != len(deselect):
        raise SystemExit("duplicate deselect entries")
    for node in deselect:
        rel = node.split("::", 1)[0]
        if files.get(rel, {}).get("status") != "public":
            raise SystemExit("deselect references non-public/unknown file: " + node)
    return files, deselect


SLOW_MODULES = {
    "tests/test_goat_periodic_audit_finalization.py",
    "tests/test_goat_batch_plan.py",
}


def balanced_shards(public, shards):
    # Seed known-slow file-backed modules round-robin, then greedily balance the rest.
    buckets = [[] for _ in range(shards)]
    load = [0] * shards
    slow = sorted(p for p in public if p in SLOW_MODULES)
    for index, rel in enumerate(slow):
        bucket = index % shards
        buckets[bucket].append(rel)
        load[bucket] += (REPO / rel).stat().st_size
    rest = [p for p in public if p not in SLOW_MODULES]
    for rel in sorted(rest, key=lambda p: (-(REPO / p).stat().st_size, p)):
        index = min(range(shards), key=lambda i: (load[i], len(buckets[i]), i))
        buckets[index].append(rel)
        load[index] += (REPO / rel).stat().st_size
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args()
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        print("invalid shard arguments", file=sys.stderr)
        return 2

    files, deselect = load_classification()
    public = sorted(rel for rel, meta in files.items() if meta["status"] == "public")
    if not public:
        print("no public test files selected", file=sys.stderr)
        return 2
    buckets = balanced_shards(public, args.shards)
    selected = buckets[args.shard]
    if not selected:
        print("empty shard rejected", file=sys.stderr)
        return 2

    selected_deselect = [n for n in deselect if n.split("::", 1)[0] in set(selected)]
    if DPAPI_NODE not in deselect:
        raise SystemExit("classification must deselect the real-DPAPI node")

    basetemp = Path(tempfile.gettempdir()) / ("nt_public_ci_" + uuid.uuid4().hex)
    deselect_args = []
    for node in dict.fromkeys([DPAPI_NODE, *selected_deselect]):
        deselect_args += ["--deselect", node]
    command = [sys.executable, str(WRAPPER), "--basetemp", str(basetemp),
               *deselect_args, "--", "-q", "-p", "no:cacheprovider", *selected]
    print(f"public CI shard {args.shard + 1}/{args.shards}: {len(selected)} files "
          f"({len(public)} public, {len(files) - len(public)} private/historical excluded), "
          f"{len(selected_deselect)} node deselections")
    return subprocess.call(command, cwd=REPO)


if __name__ == "__main__":
    raise SystemExit(main())
