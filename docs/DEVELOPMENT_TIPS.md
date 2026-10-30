# Development tips

Use `rg --files` to find source and test modules, and `rg` to inspect their
callers. Inspect `git diff --stat` and `git diff` while preparing a review.

For a focused public test run, use `tests/zero_provider_validation.py` (see
`scripts/run_ci_shard.py` for the exact command line). Modules and nodes CI does
not run are listed in `tests/ci_exclusions.json`; every other `tests/test_*.py`
runs automatically.

This page contains navigation tips only. CI, translation, recovery, publication,
credential, and test-exclusion policy is defined elsewhere. Changes to those
policies require their own review.
