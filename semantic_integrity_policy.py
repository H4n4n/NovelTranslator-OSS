"""Calibrated policy for deterministic semantic-integrity signals.

The low-level detector intentionally collects broad evidence.  This module
classifies that evidence by how safely production can act on it:

- strict blockers remain blockers only when the glossary already opted into
  ``strict_qa``;
- configured rejected/repair aliases are actionable advisories;
- absence of an ordinary canonical rendering is diagnostic evidence only;
- cross-entity substitution heuristics are experimental and suppressed unless
  a caller explicitly opts in.

The module is import-safe, makes no API calls, and performs no filesystem I/O.
"""

from semantic_integrity import entity_integrity_report


DIAGNOSTIC_TYPES = frozenset({
    "source_entity_missing_canonical_family",
})

ACTIONABLE_ADVISORY_TYPES = frozenset({
    "known_noncanonical_entity_alias",
})

EXPERIMENTAL_TYPES = frozenset({
    "possible_cross_entity_substitution",
})


def classify_entity_integrity_report(raw_report, include_experimental=False):
    """Classify one detector report without upgrading uncertain evidence.

    Unknown future advisory types fail safely into ``diagnostics`` rather than
    becoming actionable by accident.  Existing strict blockers are preserved
    exactly as reported by the detector.
    """
    if not isinstance(raw_report, dict):
        raise ValueError("raw semantic integrity report must be an object")
    if not isinstance(include_experimental, bool):
        raise ValueError("include_experimental must be boolean")

    diagnostics = []
    advisories = []
    experimental_candidates = []

    for item in raw_report.get("advisories", []) or ():
        if not isinstance(item, dict):
            continue
        issue = dict(item)
        issue_type = issue.get("type")
        if issue_type in ACTIONABLE_ADVISORY_TYPES:
            advisories.append(issue)
        elif issue_type in EXPERIMENTAL_TYPES:
            experimental_candidates.append(issue)
        else:
            # Missing-canonical-family signals and any future unclassified
            # signals remain diagnostic until separately calibrated.
            diagnostics.append(issue)

    experimental_signals = (
        experimental_candidates if include_experimental else []
    )
    strict_blockers = [
        dict(item)
        for item in raw_report.get("strict_blockers", []) or ()
        if isinstance(item, dict)
    ]

    return {
        "source_backed_identity_count": raw_report.get(
            "source_backed_identity_count", 0
        ),
        "observations": list(raw_report.get("observations", []) or ()),
        "diagnostics": diagnostics,
        "advisories": advisories,
        "experimental_signals": experimental_signals,
        "suppressed_experimental_signal_count": (
            0 if include_experimental else len(experimental_candidates)
        ),
        "strict_blockers": strict_blockers,
        "strict_commit_allowed": not strict_blockers,
    }


def calibrated_entity_integrity_report(
    source_text,
    translation,
    glossary_entries,
    *,
    identity_categories=None,
    include_experimental=False,
):
    """Run the deterministic detector and apply the calibrated policy."""
    kwargs = {}
    if identity_categories is not None:
        kwargs["identity_categories"] = identity_categories
    raw_report = entity_integrity_report(
        source_text,
        translation,
        glossary_entries,
        **kwargs,
    )
    return classify_entity_integrity_report(
        raw_report,
        include_experimental=include_experimental,
    )
