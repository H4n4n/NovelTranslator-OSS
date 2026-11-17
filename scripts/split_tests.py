#!/usr/bin/env python3
"""Print the test module paths for one CI shard (greedy by each file's measured running time)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXCLUSIONS = REPO / "tests" / "ci_exclusions.json"
# Seconds each file took on CI, the median of three runs on main on 2026-11-17, for the files that took
# 10 seconds or more. Single runs vary a lot: one runner can be twice as slow as another.
# To refresh: download the test-times-shard-N artifacts of a few CI runs and run scripts/ci_test_times.py
# on each run's files; take each file's median.
MEASURED_SECONDS = {
    "tests/test_completed_uncertain_history.py": 1455,
    "tests/test_goat_periodic_audit_finalization_part_4.py": 852,
    "tests/test_publication_readiness.py": 841,
    "tests/test_goat_periodic_audit_finalization_part_1.py": 715,
    "tests/test_goat_periodic_audit_finalization_part_2.py": 688,
    "tests/test_goat_periodic_audit_finalization_part_3.py": 638,
    "tests/test_retroactive_transactions.py": 492,
    "tests/test_build_publication_epub.py": 385,
    "tests/test_production_goat_batch_journal.py": 259,
    "tests/test_experimental_goat.py": 195,
    "tests/test_uncertain_request_disposition.py": 187,
    "tests/test_production_goat_batch_ledger.py": 186,
    "tests/test_goat_batch_plan.py": 115,
    "tests/test_goat_state_terminal_recovery.py": 115,
    "tests/test_experimental_goat_audit_proof.py": 94,
    "tests/test_production_goat_batch_boundary.py": 87,
    "tests/test_continue_and_flag.py": 86,
    "tests/test_production_lifecycle.py": 48,
    "tests/test_goat_unattended.py": 39,
    "tests/test_ui_api.py": 39,
    "tests/test_auxiliary_response_reconciliation.py": 37,
    "tests/test_novel_runner.py": 31,
    "tests/test_benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller.py": 27,
    "tests/test_full_pipeline_experiment_contract_v3.py": 26,
    "tests/test_goat_accounting_epoch_migration.py": 22,
    "tests/test_goat_glossary_review_recovery.py": 20,
    "tests/test_controlled_trial_execute.py": 17,
    "tests/test_production_goat_runtime.py": 16,
    "tests/test_goat_cap_recovery.py": 13,
    "tests/test_goat_chapter_review_response_reconciliation.py": 12,
    "tests/test_goat_saturation_recovery.py": 12,
    "tests/test_goat_state_policy_migration.py": 12,
}
# Every other file is estimated from its size: together they took 136 seconds for 2.81 MB.
UNMEASURED_BYTES_PER_SECOND = 20700


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
