"""Deterministic advisory checks for explicit Chinese kinship age/order detail.

Chinese mixed-gender kinship compounds such as ``姐弟`` and ``兄妹`` encode
both gender and relative age. English can naturally collapse these to
``siblings`` or to an age-neutral ``sister and brother``. That may be acceptable
stylistically, so this module never blocks commits. It only measures whether the
source-encoded detail is visibly preserved near the corresponding translation
region.

The helper is import-safe, makes no provider calls, and performs no filesystem
I/O.
"""

import re


KINSHIP_POLICY_VERSION = "mixed-gender-kinship-order-v1"

# Restrict v1 to the least ambiguous mixed-gender pair compounds. Same-gender
# compounds such as 兄弟/姐妹 are common lexicalized plurals in which insisting
# on explicit relative age in English would be substantially noisier.
_ORDERED_KINSHIP_TERMS = (
    ("姐弟", "older_sister_younger_brother"),
    ("姊弟", "older_sister_younger_brother"),
    ("兄妹", "older_brother_younger_sister"),
)

_SISTER_RE = re.compile(r"\b(?:sister|sis)\b", flags=re.IGNORECASE)
_BROTHER_RE = re.compile(r"\b(?:brother|bro)\b", flags=re.IGNORECASE)
_SIBLING_RE = re.compile(r"\bsiblings?\b", flags=re.IGNORECASE)
_OLDER_RE = re.compile(
    r"\b(?:older|elder|eldest|big\s+(?:sister|brother)|senior\s+(?:sister|brother))\b",
    flags=re.IGNORECASE,
)
_YOUNGER_RE = re.compile(
    r"\b(?:younger|youngest|little\s+(?:sister|brother)|kid\s+(?:sister|brother))\b",
    flags=re.IGNORECASE,
)


def ordered_kinship_occurrences(source_text):
    """Return explicit mixed-gender kinship pair occurrences in source text."""
    source_text = source_text or ""
    findings = []
    for term, relation in _ORDERED_KINSHIP_TERMS:
        start = 0
        while True:
            index = source_text.find(term, start)
            if index < 0:
                break
            findings.append({
                "source_term": term,
                "relation": relation,
                "start": index,
                "end": index + len(term),
            })
            start = index + len(term)
    findings.sort(key=lambda item: (item["start"], item["end"], item["source_term"]))
    return tuple(findings)


def _translation_window(source_text, translation, start, end):
    source_text = source_text or ""
    translation = translation or ""
    if not translation:
        return 0, 0, ""
    source_length = max(len(source_text), 1)
    center_ratio = ((start + end) / 2) / source_length
    center = int(center_ratio * len(translation))
    radius = max(320, min(900, int(len(translation) * 0.08)))
    left = max(0, center - radius)
    right = min(len(translation), center + radius)
    return left, right, translation[left:right]


def _classify_window(window):
    window = window or ""
    sister = bool(_SISTER_RE.search(window))
    brother = bool(_BROTHER_RE.search(window))
    siblings = bool(_SIBLING_RE.search(window))
    older = bool(_OLDER_RE.search(window))
    younger = bool(_YOUNGER_RE.search(window))
    gender_pair = sister and brother
    age_pair = older and younger

    if gender_pair and age_pair:
        classification = "age_and_gender_visible"
    elif gender_pair:
        classification = "gender_visible_age_not_visible"
    elif siblings:
        classification = "generic_siblings"
    else:
        classification = "relation_not_visible"

    return classification, {
        "sister_visible": sister,
        "brother_visible": brother,
        "siblings_visible": siblings,
        "older_visible": older,
        "younger_visible": younger,
    }


def ordered_kinship_completeness_report(source_text, translation):
    """Return non-blocking diagnostics for source-encoded kinship age/order."""
    source_text = source_text or ""
    translation = translation or ""
    occurrences = ordered_kinship_occurrences(source_text)
    observations = []
    diagnostics = []

    for item in occurrences:
        left, right, window = _translation_window(
            source_text,
            translation,
            item["start"],
            item["end"],
        )
        classification, evidence = _classify_window(window)
        observation = {
            **item,
            "translation_window_start": left,
            "translation_window_end": right,
            "classification": classification,
            **evidence,
        }
        observations.append(observation)
        if classification != "age_and_gender_visible":
            diagnostics.append({
                "type": "kinship_age_order_detail_not_visible",
                "policy_version": KINSHIP_POLICY_VERSION,
                "source_term": item["source_term"],
                "relation": item["relation"],
                "source_start": item["start"],
                "source_end": item["end"],
                "translation_window_start": left,
                "translation_window_end": right,
                "classification": classification,
                **evidence,
                "reason": (
                    "source kinship compound explicitly encodes mixed-gender "
                    "relative age/order, but equivalent age-and-gender detail is "
                    "not visibly present in the aligned English region; advisory only"
                ),
            })

    return {
        "policy_version": KINSHIP_POLICY_VERSION,
        "ordered_kinship_count": len(occurrences),
        "diagnostic_count": len(diagnostics),
        "observations": observations,
        "diagnostics": diagnostics,
        "commit_decision_changed": False,
    }
