#!/usr/bin/env python3
"""Run one CI shard through the zero-provider wrapper and propagate its exit code."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_tests  # noqa: E402

DPAPI_NODE = "tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True)
    args = parser.parse_args(argv)
    modules = split_tests.shards(args.shards)[args.shard]
    if not modules:
        print("run_ci_shard: no modules in this shard")
        return 0
    wanted = set(modules)
    deselect = [n for n in split_tests.load_exclusions()["deselect"] if n.split("::")[0] in wanted]
    if DPAPI_NODE not in deselect:
        deselect.append(DPAPI_NODE)  # wrapper requires it in every invocation
    basetemp = Path(tempfile.gettempdir()) / ("ci-shard-%d-%s" % (args.shard, uuid.uuid4().hex[:8]))
    cmd = [sys.executable, str(split_tests.REPO / "tests" / "zero_provider_validation.py"),
           "--basetemp", str(basetemp)]
    for node in deselect:
        cmd += ["--deselect", node]
    cmd += ["--", "-q", "-p", "no:cacheprovider", *modules]
    return subprocess.call(cmd, cwd=split_tests.REPO)


if __name__ == "__main__":
    raise SystemExit(main())
