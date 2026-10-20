"""Small output bridge for the legacy production runner.

The current legacy runner still contains one hard-coded ``V4 Pro rates / 1M``
label even though its numeric rates are supplied by the model runtime.  This
module changes only that exact display prefix while the runner executes.  It
never changes pricing values, API arguments, commit eligibility, or files.

This is intentionally a temporary compatibility bridge until the legacy runner
is consolidated behind model-neutral presentation helpers.
"""

from contextlib import contextmanager
import builtins


LEGACY_RATE_PREFIX = "V4 Pro rates / 1M:"


def model_display_name(config):
    """Return a concise human-facing name for a resolved model config."""
    key = str(getattr(config, "key", "") or "").strip().lower()
    if key == "pro":
        return "DeepSeek V4 Pro"
    if key == "flash":
        return "DeepSeek V4 Flash"
    if key == "v41-flash":
        return "DeepSeek V4.1 Flash"
    model = str(getattr(config, "model", "") or "").strip()
    return model or "DeepSeek model"


def rewrite_legacy_rate_label(text, config):
    """Replace only the known stale pricing label prefix."""
    if not isinstance(text, str) or not text.startswith(LEGACY_RATE_PREFIX):
        return text
    suffix = text[len(LEGACY_RATE_PREFIX):]
    return f"{model_display_name(config)} rates / 1M:{suffix}"


@contextmanager
def model_aware_legacy_print(config):
    """Temporarily rewrite the one stale legacy pricing label during runpy.

    ``builtins.print`` is restored in ``finally`` even when the runner raises.
    All calls other than strings beginning with ``LEGACY_RATE_PREFIX`` are
    forwarded unchanged.
    """
    original_print = builtins.print

    def wrapped_print(*args, **kwargs):
        if args and isinstance(args[0], str):
            args = (rewrite_legacy_rate_label(args[0], config), *args[1:])
        return original_print(*args, **kwargs)

    builtins.print = wrapped_print
    try:
        yield
    finally:
        builtins.print = original_print
