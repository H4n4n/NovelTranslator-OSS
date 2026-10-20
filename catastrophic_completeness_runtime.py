"""Install deterministic catastrophic-completeness QA as a hard commit gate.

The existing production engine owns commit eligibility through
``noveltranslator_core.translation_commit_qa``.  This module wraps that pure
function before the production module imports it and appends a blocker only for
conservative, provider-agnostic catastrophic completeness findings.

The wrapper performs no API calls and no filesystem I/O.  Installing it before
semantic-shadow QA means shadow telemetry observes the same authoritative commit
decision used by production.

Idempotence is verified against the currently active callable chain rather than
trusting cached module state blindly.  This matters in long-lived test/tooling
processes that may temporarily restore ``translation_commit_qa`` while leaving
runtime-state attributes behind.  Known stale state can be repaired; an unknown
wrapper fails closed instead of being overwritten.
"""

from translation_completeness import catastrophic_completeness_report


RUNTIME_MODE = "catastrophic_completeness_hard_gate"
_STATE_ATTR = "_catastrophic_completeness_runtime_state"
_SEMANTIC_STATE_ATTR = "_semantic_shadow_runtime_state"


class CatastrophicCompletenessRuntimeError(RuntimeError):
    """Cached runtime state disagrees with the active QA callable chain."""


def _active_existing_state(core_module, existing):
    """Return true only when the cached completeness wrapper is actually active."""
    current = getattr(core_module, "translation_commit_qa", None)
    wrapped = existing.get("wrapped_translation_commit_qa")
    original = existing.get("original_translation_commit_qa")
    if current is wrapped:
        return True

    semantic = getattr(core_module, _SEMANTIC_STATE_ATTR, None)
    if isinstance(semantic, dict):
        semantic_wrapped = semantic.get("wrapped_translation_commit_qa")
        semantic_original = semantic.get("original_translation_commit_qa")
        if current is semantic_wrapped and semantic_original is wrapped:
            return True
        # A currently active semantic wrapper around some other authority is not
        # safe to rewrite underneath because doing so could discard an unknown
        # policy/reporter chain.  Fail closed and make the ordering problem
        # explicit.
        if current is semantic_wrapped:
            raise CatastrophicCompletenessRuntimeError(
                "semantic-shadow QA is active without the cached catastrophic "
                "completeness wrapper underneath it"
            )

    # Known stale state: callers/tests restored the exact original callable but
    # left the cache attribute behind.  Clearing only our stale cache allows a
    # fresh wrapper to be installed around the currently authoritative function.
    if current is original:
        try:
            delattr(core_module, _STATE_ATTR)
        except AttributeError:
            pass
        return False

    raise CatastrophicCompletenessRuntimeError(
        "cached catastrophic-completeness runtime state does not match the "
        "active translation_commit_qa callable; refusing to overwrite an "
        "unknown QA wrapper"
    )


def install_catastrophic_completeness_qa(core_module):
    """Wrap ``translation_commit_qa`` once and verify cached idempotence."""
    existing = getattr(core_module, _STATE_ATTR, None)
    if isinstance(existing, dict) and _active_existing_state(core_module, existing):
        return existing

    original = getattr(core_module, "translation_commit_qa", None)
    if not callable(original):
        raise AttributeError("core module has no callable translation_commit_qa")

    def wrapped_translation_commit_qa(*args, **kwargs):
        legacy = original(*args, **kwargs)
        if not isinstance(legacy, dict):
            raise ValueError("translation_commit_qa must return an object")
        if len(args) >= 2:
            source_text, translation = args[:2]
        else:
            source_text = kwargs.get(
                "chinese_source",
                kwargs.get("source_text", ""),
            )
            translation = kwargs.get("translation", "")

        completeness = catastrophic_completeness_report(
            source_text or "",
            translation or "",
        )
        result = dict(legacy)
        blockers = [dict(item) for item in legacy.get("commit_blockers", [])]
        if completeness["blocked"]:
            blockers.append({
                "type": "catastrophic_incompleteness",
                "policy_version": completeness["policy_version"],
                "reasons": list(completeness["reasons"]),
                "observations": dict(completeness["observations"]),
            })
        result["catastrophic_completeness"] = completeness
        result["commit_blockers"] = blockers
        result["commit_allowed"] = not blockers
        return result

    core_module.translation_commit_qa = wrapped_translation_commit_qa
    state = {
        "original_translation_commit_qa": original,
        "wrapped_translation_commit_qa": wrapped_translation_commit_qa,
        "mode": RUNTIME_MODE,
    }
    setattr(core_module, _STATE_ATTR, state)
    return state
