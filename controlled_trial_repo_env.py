"""Credential bridge for cross-worktree controlled production trials.

The hardened executor may run engine code from one worktree while targeting a
production project rooted in another checkout.  python-dotenv's implicit search
then starts from the engine worktree, so it may not see the production checkout's
untracked ``.env`` file.  This module explicitly loads only ``DEEPSEEK_API_KEY``
from the selected ``--repo-root`` when the process environment does not already
provide it.

The key is never printed, returned, persisted, or copied into project files.
Any temporary environment mutation is restored on exit.
"""

import argparse
import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

import controlled_trial_execute as executor
from novel_project import repository_root


API_KEY_ENV = "DEEPSEEK_API_KEY"


class ControlledTrialCredentialError(RuntimeError):
    pass


@contextmanager
def production_api_key_environment(repo_root):
    """Ensure ``DEEPSEEK_API_KEY`` is available for one controlled execution.

    Existing process configuration wins.  Otherwise only the named key is read
    from ``<repo-root>/.env``.  The previous environment state is restored in
    ``finally`` so this bridge cannot permanently alter the caller's shell.
    """
    repo_root = Path(repo_root).resolve()
    previous_present = API_KEY_ENV in os.environ
    previous_value = os.environ.get(API_KEY_ENV)

    current = (previous_value or "").strip()
    injected = False
    source = "process_environment"
    if not current:
        dotenv_path = repo_root / ".env"
        if not dotenv_path.is_file():
            raise ControlledTrialCredentialError(
                f"{API_KEY_ENV} is not set and repository .env does not exist: {dotenv_path}"
            )
        values = dotenv_values(dotenv_path)
        candidate = values.get(API_KEY_ENV)
        if not isinstance(candidate, str) or not candidate.strip():
            raise ControlledTrialCredentialError(
                f"{API_KEY_ENV} was not found in repository .env: {dotenv_path}"
            )
        os.environ[API_KEY_ENV] = candidate.strip()
        injected = True
        source = "repository_env_file"

    try:
        yield {"source": source, "repo_root": str(repo_root)}
    finally:
        if injected:
            if previous_present:
                os.environ[API_KEY_ENV] = previous_value or ""
            else:
                os.environ.pop(API_KEY_ENV, None)


def _preparse(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--authorize-paid-trial", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv=None):
    """Delegate to the hardened executor, loading credentials only when paid."""
    pre = _preparse(argv)
    if not pre.authorize_paid_trial:
        return executor.main(argv)
    try:
        with production_api_key_environment(pre.repo_root):
            return executor.main(argv)
    except ControlledTrialCredentialError as exc:
        print("=" * 70)
        print("CONTROLLED ONE-CHAPTER PRODUCTION TRIAL EXECUTOR")
        print("=" * 70)
        print("Status: REFUSED_BEFORE_PROVIDER")
        print(f"Error: {exc}")
        print("API calls: 0")
        print("Actual accounted cost: $0.000000")
        print("=" * 70)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
