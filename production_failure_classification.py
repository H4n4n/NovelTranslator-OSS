"""Typed controlled failures for production paths that are not provider transport errors.

These classes exist only to keep operator diagnostics and shutdown handling
truthful. They do not change retry policy, provider scheduling, persistence
ordering, or recovery authorization.
"""

from __future__ import annotations


class NonProviderProductionError(RuntimeError):
    """Controlled production failure that must not be described as API transport."""

    failure_kind = "local_safety"


class LocalPersistenceError(NonProviderProductionError):
    """A local durable write/checkpoint callback failed."""

    failure_kind = "local_persistence"


class LocalCallbackError(NonProviderProductionError):
    """A local non-provider callback failed."""

    failure_kind = "local_callback"


class ProviderResponseError(NonProviderProductionError):
    """A provider responded, but response/accounting validation was unusable."""

    failure_kind = "provider_response"


class ProductionIntegrityError(NonProviderProductionError):
    """A local production integrity invariant failed."""

    failure_kind = "integrity"


class ProductionControlError(NonProviderProductionError):
    """An operator/bounded-run control invariant failed."""

    failure_kind = "control"


def describe_non_provider_error(exc: BaseException) -> str:
    """Return a truthful operator-facing description for a controlled local failure."""

    kind = getattr(exc, "failure_kind", "local_safety")
    if kind == "local_persistence":
        return (
            "A local checkpoint/persistence operation failed. This is not an "
            "API/network failure. Preserve the current pending/recovery files, "
            "fix the local filesystem or persistence problem, then re-run the "
            "same guarded workflow."
        )
    if kind == "local_callback":
        return (
            "A local processing callback failed. This is not an API/network "
            "failure. Inspect the local error and preserved pending state before "
            "retrying."
        )
    if kind == "provider_response":
        return (
            "The provider request may have completed, but the returned response "
            "or required accounting could not be validated safely. Do not assume "
            "a new paid request is safe; inspect the durable recovery/journal "
            "evidence before retrying."
        )
    if kind == "integrity":
        return (
            "A local production integrity check failed. No API/network diagnosis "
            "is implied. Inspect the reported local state mismatch before "
            "continuing."
        )
    if kind == "control":
        return (
            "A local bounded-run/control invariant failed. No provider error is "
            "implied. Re-check the approved target and current production state."
        )
    return (
        "A controlled local/recovery safety check failed. This is not an "
        "API/network failure. Inspect the preserved local state and the reported "
        "error before continuing."
    )
