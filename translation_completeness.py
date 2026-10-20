"""Deterministic provider-agnostic catastrophic translation completeness checks.

This module is import-safe and pure: it reads no credentials, performs no API
calls, and touches no filesystem state.  It intentionally targets only gross
completeness failures that are safe to block automatically.  It is not a
literary-quality scorer and does not try to judge ordinary abridgement or style.

The policy combines independent structural signals instead of a fixed minimum
word count.  Legitimately short source chapters therefore remain eligible, while
substantial source chapters that collapse to a heading or tiny fragment are
blocked before any glossary/state mutation can occur.
"""

from __future__ import annotations

import re
from typing import Any


COMPLETENESS_POLICY_VERSION = "catastrophic-completeness-v1"

# These thresholds describe when a *source* contains enough body to make a very
# small target structurally implausible.  They are deliberately conservative:
# no target is rejected merely for falling below an absolute word count.
HEADING_ONLY_MIN_SOURCE_BODY_CHARS = 240
GROSS_COLLAPSE_MIN_SOURCE_BODY_CHARS = 800
GROSS_COLLAPSE_MAX_TARGET_SOURCE_RATIO = 0.12
STRUCTURE_COLLAPSE_MIN_SOURCE_BODY_CHARS = 600
STRUCTURE_COLLAPSE_MIN_SOURCE_UNITS = 4
STRUCTURE_COLLAPSE_MAX_TARGET_UNITS = 1
STRUCTURE_COLLAPSE_MAX_TARGET_SOURCE_RATIO = 0.20

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*")
_MARKDOWN_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
_TARGET_HEADING_RE = re.compile(
    r"^(?:chapter\b|prologue\b|epilogue\b).{0,220}$",
    flags=re.IGNORECASE,
)
_CHINESE_SOURCE_HEADING_RE = re.compile(
    r"^第\s*[^\s]{1,24}\s*[章节回卷](?:\s|[.．:：、-]|$)"
)
_KOREAN_SOURCE_HEADING_RE = re.compile(
    r"^(?:제\s*)?\d+\s*(?:화|장)(?:\s|[.．:：、-]|$)"
)


def _nonspace_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _paragraphs(text: str) -> list[str]:
    value = (text or "").strip()
    if not value:
        return []
    return [
        item.strip()
        for item in re.split(r"\n\s*\n+", value)
        if item.strip()
    ]


def _looks_like_source_heading(line: str) -> bool:
    candidate = (line or "").strip()
    return bool(
        _CHINESE_SOURCE_HEADING_RE.match(candidate)
        or _KOREAN_SOURCE_HEADING_RE.match(candidate)
    )


def _source_body_text(source_text: str) -> str:
    """Drop one recognizable standalone source heading for ratio purposes."""
    lines = (source_text or "").splitlines()
    first_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_index is None:
        return ""
    if not _looks_like_source_heading(lines[first_index]):
        return source_text or ""
    remaining = lines[:first_index] + lines[first_index + 1 :]
    body = "\n".join(remaining)
    return body if body.strip() else source_text or ""


def _normalized_heading_candidate(line: str) -> str:
    candidate = (line or "").strip()
    candidate = _MARKDOWN_HEADING_PREFIX_RE.sub("", candidate, count=1).strip()
    if candidate.startswith("**") and candidate.endswith("**") and len(candidate) > 4:
        candidate = candidate[2:-2].strip()
    return candidate


def _heading_only_target(translation: str) -> bool:
    lines = _nonempty_lines(translation)
    if len(lines) != 1:
        return False
    candidate = _normalized_heading_candidate(lines[0])
    return bool(_TARGET_HEADING_RE.fullmatch(candidate))


def catastrophic_completeness_report(
    source_text: str,
    translation: str,
) -> dict[str, Any]:
    """Return deterministic evidence for only gross translation omissions.

    ``blocked`` becomes true only when at least one conservative catastrophic
    signal fires.  Output word count is reported for diagnostics but is never
    itself used as an absolute minimum.
    """
    source = source_text or ""
    target = translation or ""
    source_body = _source_body_text(source)

    source_chars = _nonspace_chars(source)
    source_body_chars = _nonspace_chars(source_body)
    target_chars = _nonspace_chars(target)
    target_words = len(_WORD_RE.findall(target))
    source_lines = len(_nonempty_lines(source_body))
    target_lines = len(_nonempty_lines(target))
    source_paragraphs = len(_paragraphs(source_body))
    target_paragraphs = len(_paragraphs(target))
    source_structure_units = max(source_lines, source_paragraphs)
    target_structure_units = max(target_lines, target_paragraphs)

    denominator = source_body_chars or source_chars
    target_source_ratio = (
        target_chars / denominator
        if denominator > 0
        else 1.0 if target_chars else 0.0
    )

    heading_only = (
        source_body_chars >= HEADING_ONLY_MIN_SOURCE_BODY_CHARS
        and _heading_only_target(target)
    )
    gross_output_collapse = (
        source_body_chars >= GROSS_COLLAPSE_MIN_SOURCE_BODY_CHARS
        and target_source_ratio < GROSS_COLLAPSE_MAX_TARGET_SOURCE_RATIO
    )
    paragraph_or_line_collapse = (
        source_body_chars >= STRUCTURE_COLLAPSE_MIN_SOURCE_BODY_CHARS
        and source_structure_units >= STRUCTURE_COLLAPSE_MIN_SOURCE_UNITS
        and target_structure_units <= STRUCTURE_COLLAPSE_MAX_TARGET_UNITS
        and target_source_ratio < STRUCTURE_COLLAPSE_MAX_TARGET_SOURCE_RATIO
    )

    reasons = []
    if heading_only:
        reasons.append("heading_only_translation")
    if gross_output_collapse:
        reasons.append("gross_output_collapse")
    if paragraph_or_line_collapse:
        reasons.append("paragraph_structure_collapse")

    observations = {
        "source_nonspace_characters": source_chars,
        "source_body_nonspace_characters": source_body_chars,
        "translation_nonspace_characters": target_chars,
        "translation_word_count": target_words,
        "source_structure_units": source_structure_units,
        "translation_structure_units": target_structure_units,
        "source_nonempty_lines": source_lines,
        "translation_nonempty_lines": target_lines,
        "source_paragraphs": source_paragraphs,
        "translation_paragraphs": target_paragraphs,
        "translation_to_source_body_char_ratio": target_source_ratio,
        "heading_only_translation": heading_only,
    }
    return {
        "policy_version": COMPLETENESS_POLICY_VERSION,
        "blocked": bool(reasons),
        "reasons": reasons,
        "observations": observations,
    }
