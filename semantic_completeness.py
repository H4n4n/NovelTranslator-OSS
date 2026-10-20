"""Conservative deterministic source-information completeness signals.

Version 1 looks only for an unusually explicit Chinese source structure: a
short term immediately followed by a parenthetical gloss introduced by cues
such as ``即`` (namely/that is) or ``也就是`` (that is).  It then asks whether
the roughly corresponding translation region contains any visible explanatory
structure.

This is intentionally advisory.  A good translator may integrate a gloss into
ordinary prose without parentheses or cue words, so absence of visible evidence
is not proof of omission.  The helper never changes text, never calls an API,
and never owns commit eligibility.
"""

import re


COMPLETENESS_POLICY_VERSION = "explicit-source-gloss-v1"

# CJK has no required word boundaries, so the raw label context can include a
# short verb phrase immediately before the actual term.  ``_clean_label`` below
# conservatively strips a small set of common introduction markers.
_EXPLICIT_CJK_GLOSS_RE = re.compile(
    r"(?P<label>[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{1,12})"
    r"[（(]"
    r"(?P<gloss>(?:即|即是|也就是|也就是說|也就是说|意為|意为|意思是)"
    r"[^）)\r\n]{2,120})"
    r"[）)]"
)

_ENGLISH_EXPLANATORY_CUE_RE = re.compile(
    r"\b(?:that\s+is(?:\s+to\s+say)?|i\.e\.|namely|in\s+other\s+words|"
    r"meaning|means|refers\s+to|responsible\s+for)\b",
    flags=re.IGNORECASE,
)
_ENGLISH_PARENTHETICAL_RE = re.compile(r"\(([^()]{8,220})\)")
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_LABEL_INTRO_MARKERS = (
    "叫做", "稱為", "称为", "所謂", "所谓", "作為", "作为",
    "做", "是", "為", "为", "當", "当",
)


def _clean_label(raw):
    raw = str(raw or "")
    best = raw
    best_position = -1
    for marker in _LABEL_INTRO_MARKERS:
        position = raw.rfind(marker)
        candidate = raw[position + len(marker):] if position >= 0 else ""
        if position >= best_position and 1 <= len(candidate) <= 8:
            best = candidate
            best_position = position
    return best


def explicit_source_glosses(source_text):
    """Return explicit short-term parenthetical source glosses."""
    source_text = source_text or ""
    return tuple({
        "label": _clean_label(match.group("label")),
        "label_context": match.group("label"),
        "gloss": match.group("gloss"),
        "start": match.start(),
        "end": match.end(),
    } for match in _EXPLICIT_CJK_GLOSS_RE.finditer(source_text))


def _translation_window(source_text, translation, start, end):
    source_text = source_text or ""
    translation = translation or ""
    if not translation:
        return 0, 0, ""
    source_length = max(len(source_text), 1)
    center_ratio = ((start + end) / 2) / source_length
    center = int(center_ratio * len(translation))
    radius = max(260, min(900, int(len(translation) * 0.07)))
    left = max(0, center - radius)
    right = min(len(translation), center + radius)
    return left, right, translation[left:right]


def _has_explanatory_evidence(window):
    if _ENGLISH_EXPLANATORY_CUE_RE.search(window or ""):
        return True
    for match in _ENGLISH_PARENTHETICAL_RE.finditer(window or ""):
        if len(_ENGLISH_WORD_RE.findall(match.group(1))) >= 3:
            return True
    return False


def explicit_source_gloss_completeness_report(source_text, translation):
    """Return non-blocking diagnostics for possibly omitted explicit glosses."""
    source_text = source_text or ""
    translation = translation or ""
    glosses = explicit_source_glosses(source_text)
    diagnostics = []
    observations = []
    for item in glosses:
        left, right, window = _translation_window(
            source_text,
            translation,
            item["start"],
            item["end"],
        )
        evidence = _has_explanatory_evidence(window)
        observation = {
            **item,
            "translation_window_start": left,
            "translation_window_end": right,
            "explanatory_evidence_found": evidence,
        }
        observations.append(observation)
        if not evidence:
            diagnostics.append({
                "type": "possible_omitted_explicit_source_gloss",
                "policy_version": COMPLETENESS_POLICY_VERSION,
                "source_label": item["label"],
                "source_label_context": item["label_context"],
                "source_gloss": item["gloss"],
                "source_start": item["start"],
                "source_end": item["end"],
                "translation_window_start": left,
                "translation_window_end": right,
                "reason": (
                    "source contains an explicit parenthetical term explanation "
                    "but the aligned translation region has no visible explanatory "
                    "cue or substantive parenthetical; advisory only"
                ),
            })
    return {
        "policy_version": COMPLETENESS_POLICY_VERSION,
        "explicit_source_gloss_count": len(glosses),
        "possible_omitted_gloss_count": len(diagnostics),
        "observations": observations,
        "diagnostics": diagnostics,
        "commit_decision_changed": False,
    }
