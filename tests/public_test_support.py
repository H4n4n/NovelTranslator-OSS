"""Public test support helpers (synthetic only; no private data)."""
from novel_project import ResidualCJKMicroRepairConfig


def enabled_config(**overrides):
    """Synthetic residual-CJK micro-repair configuration used by public tests."""
    values = {
        "enabled": True,
        "max_total_cjk_chars": 12,
        "max_fragments": 3,
        "max_affected_lines": 3,
        "max_attempts_per_pending_version": 1,
    }
    values.update(overrides)
    return ResidualCJKMicroRepairConfig(**values)
