"""Controlled-trial wrapper that explicitly permits peak-time execution.

This override is scoped only to the controlled one-chapter executor. It does
not modify normal production defaults. Dynamic pricing still uses the actual
peak/off-peak period and the approved provider-call/cost ceilings remain
authoritative.
"""

from contextlib import contextmanager

import controlled_trial_execute as executor
import controlled_trial_repo_env as repo_env


@contextmanager
def peak_allowed_execution():
    """Temporarily request the explicit controlled-trial scheduler override."""

    previous = executor._CONTROLLED_TRIAL_ALLOW_PEAK
    executor._CONTROLLED_TRIAL_ALLOW_PEAK = True
    try:
        yield
    finally:
        executor._CONTROLLED_TRIAL_ALLOW_PEAK = previous


def main(argv=None):
    print(
        "CONTROLLED TRIAL SCHEDULER OVERRIDE: "
        "AUTO_OFF_PEAK_ONLY = False"
    )
    print(
        "Peak pricing still applies and the approved dollar ceiling "
        "remains enforced."
    )
    with peak_allowed_execution():
        return repo_env.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
