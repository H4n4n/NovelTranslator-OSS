"""Install calibrated semantic QA as a non-blocking production shadow.

The production engine already owns commit eligibility through
``noveltranslator_core.translation_commit_qa``.  This module wraps that pure
function before the legacy runner imports it, computes calibrated entity
integrity plus conservative source-information completeness diagnostics, and
attaches/reports that evidence without changing any existing commit field or
blocker.

The wrapper makes no API calls and performs no filesystem I/O.  A caller may
supply a reporter (for example a concise console reporter) to observe findings.
Cached idempotence is accepted only when the cached wrapper is still the active
callable.  Known stale state can be reinstalled around the active catastrophic
completeness authority; an unknown wrapper fails closed.
"""

from collections import Counter
from hashlib import sha256

from semantic_completeness import explicit_source_gloss_completeness_report
from semantic_integrity_policy import calibrated_entity_integrity_report


SEMANTIC_SHADOW_MODE = "calibrated_non_blocking_shadow"
_STATE_ATTR = "_semantic_shadow_runtime_state"
_COMPLETENESS_STATE_ATTR = "_catastrophic_completeness_runtime_state"


class SemanticShadowRuntimeError(RuntimeError):
    """Cached semantic-shadow state disagrees with the active QA callable."""


def _count_by(items, key):
    return dict(sorted(Counter(
        str(item.get(key, "unknown") or "unknown")
        for item in items
        if isinstance(item, dict)
    ).items()))


def _candidate_fingerprint(source_text, translation):
    payload = (str(source_text or "") + "\0" + str(translation or "")).encode(
        "utf-8"
    )
    return sha256(payload).hexdigest()


def build_semantic_shadow_report(
    source_text,
    translation,
    glossary_entries,
    legacy_qa,
    *,
    include_experimental=False,
):
    """Return calibrated semantic evidence while preserving legacy authority.

    ``legacy_qa`` is treated as authoritative for commit eligibility.  Even a
    semantic strict-parity mismatch or a possible source-information omission is
    surfaced only as shadow evidence here; neither can upgrade or downgrade the
    established commit decision.
    """
    if not isinstance(legacy_qa, dict):
        raise ValueError("legacy QA result must be an object")
    if not isinstance(legacy_qa.get("commit_allowed"), bool):
        raise ValueError("legacy QA result must contain boolean commit_allowed")

    semantic = calibrated_entity_integrity_report(
        source_text,
        translation,
        glossary_entries,
        include_experimental=include_experimental,
    )
    completeness = explicit_source_gloss_completeness_report(
        source_text,
        translation,
    )
    diagnostics = [
        dict(item) for item in semantic.get("diagnostics", [])
        if isinstance(item, dict)
    ]
    advisories = [
        dict(item) for item in semantic.get("advisories", [])
        if isinstance(item, dict)
    ]
    experimental = [
        dict(item) for item in semantic.get("experimental_signals", [])
        if isinstance(item, dict)
    ]
    strict_blockers = [
        dict(item) for item in semantic.get("strict_blockers", [])
        if isinstance(item, dict)
    ]
    completeness_diagnostics = [
        dict(item) for item in completeness.get("diagnostics", [])
        if isinstance(item, dict)
    ]

    legacy_commit_allowed = legacy_qa["commit_allowed"]
    strict_parity_mismatch = bool(strict_blockers) and legacy_commit_allowed
    return {
        "mode": SEMANTIC_SHADOW_MODE,
        "candidate_fingerprint": _candidate_fingerprint(source_text, translation),
        "source_backed_identity_count": semantic.get(
            "source_backed_identity_count", 0
        ),
        "diagnostic_count": len(diagnostics),
        "diagnostic_types": _count_by(diagnostics, "type"),
        "diagnostic_categories": _count_by(diagnostics, "category"),
        "actionable_advisory_count": len(advisories),
        "actionable_advisory_types": _count_by(advisories, "type"),
        "experimental_signal_count": len(experimental),
        "suppressed_experimental_signal_count": semantic.get(
            "suppressed_experimental_signal_count", 0
        ),
        "semantic_strict_blocker_count": len(strict_blockers),
        "completeness_policy_version": completeness.get("policy_version", ""),
        "explicit_source_gloss_count": completeness.get(
            "explicit_source_gloss_count", 0
        ),
        "completeness_diagnostic_count": len(completeness_diagnostics),
        "completeness_diagnostic_types": _count_by(
            completeness_diagnostics, "type"
        ),
        "legacy_commit_allowed": legacy_commit_allowed,
        "strict_parity_mismatch": strict_parity_mismatch,
        # This invariant is intentionally literal: this shadow layer never owns
        # or changes production commit eligibility.
        "commit_decision_changed": False,
        "diagnostics": diagnostics,
        "actionable_advisories": advisories,
        "experimental_signals": experimental,
        "semantic_strict_blockers": strict_blockers,
        "completeness_diagnostics": completeness_diagnostics,
        "completeness_observations": completeness.get("observations", []),
    }


def make_console_semantic_shadow_reporter(print_fn=print):
    """Return a concise reporter that deduplicates repeated QA of one candidate."""
    seen = set()

    def report(shadow):
        fingerprint = shadow.get("candidate_fingerprint", "")
        if fingerprint and fingerprint in seen:
            return
        if fingerprint:
            seen.add(fingerprint)

        finding_count = (
            shadow.get("diagnostic_count", 0)
            + shadow.get("actionable_advisory_count", 0)
            + shadow.get("experimental_signal_count", 0)
            + shadow.get("suppressed_experimental_signal_count", 0)
            + shadow.get("semantic_strict_blocker_count", 0)
            + shadow.get("completeness_diagnostic_count", 0)
        )
        if not finding_count and not shadow.get("strict_parity_mismatch"):
            return

        parts = [
            "SEMANTIC SHADOW QA",
            f"identities={shadow.get('source_backed_identity_count', 0)}",
            f"diagnostics={shadow.get('diagnostic_count', 0)}",
            f"completeness={shadow.get('completeness_diagnostic_count', 0)}",
            f"actionable={shadow.get('actionable_advisory_count', 0)}",
            f"strict-shadow={shadow.get('semantic_strict_blocker_count', 0)}",
            f"experimental={shadow.get('experimental_signal_count', 0)}",
            "experimental-suppressed="
            f"{shadow.get('suppressed_experimental_signal_count', 0)}",
            "commit-decision=UNCHANGED",
        ]
        if shadow.get("strict_parity_mismatch"):
            parts.append("STRICT-PARITY-MISMATCH=YES")
        print_fn(" | ".join(parts))

    return report


def _cached_state_is_active(core_module, existing, *, include_experimental):
    current = getattr(core_module, "translation_commit_qa", None)
    wrapped = existing.get("wrapped_translation_commit_qa")
    original = existing.get("original_translation_commit_qa")
    if current is wrapped:
        if existing.get("include_experimental") != include_experimental:
            raise SemanticShadowRuntimeError(
                "semantic shadow is already active with different experimental policy"
            )
        return True

    completeness = getattr(core_module, _COMPLETENESS_STATE_ATTR, None)
    completeness_wrapped = (
        completeness.get("wrapped_translation_commit_qa")
        if isinstance(completeness, dict)
        else None
    )

    # Known stale states: a caller restored the wrapper's original callable, or
    # catastrophic completeness has just been safely reinstalled and is now the
    # active authority.  Remove only our stale cache and install a fresh semantic
    # wrapper around the current callable.
    if current is original or (
        callable(completeness_wrapped) and current is completeness_wrapped
    ):
        try:
            delattr(core_module, _STATE_ATTR)
        except AttributeError:
            pass
        return False

    raise SemanticShadowRuntimeError(
        "cached semantic-shadow runtime state does not match the active "
        "translation_commit_qa callable; refusing to overwrite an unknown QA wrapper"
    )


def install_semantic_shadow_qa(
    core_module,
    *,
    reporter=None,
    include_experimental=False,
):
    """Wrap ``core_module.translation_commit_qa`` without changing authority."""
    existing = getattr(core_module, _STATE_ATTR, None)
    if isinstance(existing, dict) and _cached_state_is_active(
        core_module,
        existing,
        include_experimental=include_experimental,
    ):
        return existing

    original = getattr(core_module, "translation_commit_qa", None)
    if not callable(original):
        raise AttributeError("core module has no callable translation_commit_qa")

    def wrapped_translation_commit_qa(*args, **kwargs):
        legacy = original(*args, **kwargs)
        if len(args) >= 3:
            source_text, translation, glossary_entries = args[:3]
        else:
            source_text = kwargs.get("chinese_source", kwargs.get("source_text", ""))
            translation = kwargs.get("translation", "")
            glossary_entries = kwargs.get("glossary_entries", {})

        shadow = build_semantic_shadow_report(
            source_text,
            translation,
            glossary_entries,
            legacy,
            include_experimental=include_experimental,
        )
        result = dict(legacy)
        result["semantic_shadow"] = shadow
        if reporter is not None:
            reporter(shadow)
        return result

    core_module.translation_commit_qa = wrapped_translation_commit_qa
    state = {
        "original_translation_commit_qa": original,
        "wrapped_translation_commit_qa": wrapped_translation_commit_qa,
        "include_experimental": include_experimental,
        "mode": SEMANTIC_SHADOW_MODE,
    }
    setattr(core_module, _STATE_ATTR, state)
    return state
