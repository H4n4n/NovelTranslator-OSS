#!/usr/bin/env python3
"""Seconds per test file from pytest junit XML files, as the MEASURED_SECONDS block for split_tests.py.

Usage: python scripts/ci_test_times.py junit-shard-0.xml [junit-shard-1.xml ...]
Download the files from a CI run's "test-times-shard-N" artifacts. Files under 10 seconds are left
out: split_tests.py estimates those from their size.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

MIN_SECONDS = 10


def seconds_per_file(paths):
    totals = defaultdict(float)
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            module = next((part for part in (case.get("classname") or "").split(".") if part.startswith("test_")), None)
            if module:
                totals[f"tests/{module}.py"] += float(case.get("time") or 0)
    return dict(totals)


def main(argv=None):
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print(__doc__)
        return 2
    totals = seconds_per_file(paths)
    slow = sorted(((m, round(s)) for m, s in totals.items() if s >= MIN_SECONDS), key=lambda kv: (-kv[1], kv[0]))
    print("MEASURED_SECONDS = {")
    for module, value in slow:
        print(f'    "{module}": {value},')
    print("}")
    rest = sum(s for s in totals.values() if s < MIN_SECONDS)
    print(f"# {len(totals)} files, {round(sum(totals.values()))} s in all; the {len(totals) - len(slow)} "
          f"files under {MIN_SECONDS} s took {round(rest)} s together.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
