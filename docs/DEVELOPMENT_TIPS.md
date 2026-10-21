# Development tips

Use `rg --files` to find source and test modules, and `rg` to inspect their
callers. Inspect `git diff --stat` and `git diff` while preparing a review.

For a focused public test run, first read `docs/public_ci_reliability.md` for
the guarded runner command. A focused local run gives feedback on those tests;
the hosted qualification records its own source identity and coverage.

This page contains navigation tips only. CI, translation, recovery, publication,
credential, and test-exclusion policy is defined elsewhere. Changes to those
policies require their own review.
