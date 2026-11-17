#!/usr/bin/env python3
"""Print the test module paths for one CI shard (greedy by each file's measured running time)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXCLUSIONS = REPO / "tests" / "ci_exclusions.json"
# Seconds each file took in a full local run on 2026-11-16, for the files that took 10 seconds or more.
# To refresh: download a CI run's test-times-shard-N artifacts and run scripts/ci_test_times.py on them.
MEASURED_SECONDS = {
    "tests/test_completed_uncertain_history.py": 1537,
    "tests/test_goat_periodic_audit_finalization_part_2.py": 933,
    "tests/test_publication_readiness.py": 844,
    "tests/test_goat_periodic_audit_finalization_part_4.py": 752,
    "tests/test_retroactive_transactions.py": 685,
    "tests/test_goat_periodic_audit_finalization_part_1.py": 662,
    "tests/test_goat_periodic_audit_finalization_part_3.py": 615,
    "tests/test_build_publication_epub.py": 345,
    "tests/test_uncertain_request_disposition.py": 170,
    "tests/test_production_goat_batch_journal.py": 163,
    "tests/test_experimental_goat.py": 162,
    "tests/test_production_goat_batch_ledger.py": 118,
    "tests/test_goat_batch_plan.py": 106,
    "tests/test_goat_state_terminal_recovery.py": 105,
    "tests/test_continue_and_flag.py": 78,
    "tests/test_production_lifecycle.py": 71,
    "tests/test_experimental_goat_audit_proof.py": 67,
    "tests/test_production_goat_batch_boundary.py": 54,
    "tests/test_goat_unattended.py": 48,
    "tests/test_auxiliary_response_reconciliation.py": 46,
    "tests/test_ui_api.py": 37,
    "tests/test_novel_runner.py": 32,
    "tests/test_full_pipeline_experiment_contract_v3.py": 30,
    "tests/test_benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller.py": 28,
    "tests/test_goat_chapter_review_response_reconciliation.py": 19,
    "tests/test_goat_glossary_review_recovery.py": 18,
    "tests/test_batch_mode.py": 14,
    "tests/test_goat_cap_recovery.py": 12,
    "tests/test_production_goat_runtime.py": 12,
    "tests/test_goat_saturation_recovery.py": 11,
}
# Every other file is estimated from its size: together they took 173 seconds for 2.86 MB.
UNMEASURED_BYTES_PER_SECOND = 16500


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
        return MEASURED_SECONDS.get(module) or (repo / module).stat().st_size / UNMEASURED_BYTES_PER_SECOND
    # Slowest first, ties by name; each file goes to the lightest shard.
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
