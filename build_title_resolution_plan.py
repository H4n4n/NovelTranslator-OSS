"""Build a source-backed, zero-API editorial title-resolution plan.

The builder reads the accepted normalization inventory/plan and immutable
source-alignment evidence.  It never edits chapter prose.  Novel-specific
editorial findings are data consumed by generic validation and anchor helpers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock


import os as _os
import sys as _sys

from private_inputs import PrivateInputError, load_title_inputs

_PRIVATE_INPUTS_ENV = "NOVELTRANSLATOR_TITLE_INPUTS"
_PRIVATE_INPUTS_PATH = _os.environ.get(_PRIVATE_INPUTS_ENV) or _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "local_data", "title_resolution_inputs.json")

_PRIVATE = {}
_PRIVATE_INPUTS_ERROR = ""
if _os.path.isfile(_PRIVATE_INPUTS_PATH):
    try:
        _PRIVATE = load_title_inputs(_PRIVATE_INPUTS_PATH)
        PRIVATE_INPUTS_AVAILABLE = True
    except PrivateInputError as exc:
        PRIVATE_INPUTS_AVAILABLE = False
        _PRIVATE_INPUTS_ERROR = str(exc)
else:
    PRIVATE_INPUTS_AVAILABLE = False
    _PRIVATE_INPUTS_ERROR = "private input file is unavailable"


def _private_value(name, default, as_set=False):
    value = _PRIVATE.get(name, default)
    return set(value) if as_set else value


EXPECTED_NORMALIZED = _private_value("EXPECTED_NORMALIZED", {})
EXPECTED_BASELINE = _private_value("EXPECTED_BASELINE", {})
EXPECTED_INPUT_COUNTS = _private_value("EXPECTED_INPUT_COUNTS", {})
HUMAN_DOWNGRADED_IDS = _private_value("HUMAN_DOWNGRADED_IDS", (), as_set=True)
TITLE_REGION_MAX_PHYSICAL_LINES = _private_value("TITLE_REGION_MAX_PHYSICAL_LINES", 12)
ROGUE_AUTHORIZED_RECOVERIES = _private_value("ROGUE_AUTHORIZED_RECOVERIES", {})
ROGUE_REVIEW_RESOLUTIONS = _private_value("ROGUE_REVIEW_RESOLUTIONS", {})
ROGUE_UNRESOLVED_NOTES = _private_value("ROGUE_UNRESOLVED_NOTES", {})
UNIT_99_GUARD = _private_value("UNIT_99_GUARD", None)
REQUIRED_DISCONTINUITY_GUARDS = _private_value("REQUIRED_DISCONTINUITY_GUARDS", ())

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fold_minor_title_variation(value: str) -> str:
    """Ignore only case, Unicode form, punctuation, and spacing."""
    value = unicodedata.normalize("NFKC", value).replace("’", "'").casefold()
    return re.sub(r"[^\w']+", " ", value, flags=re.UNICODE).strip()


def corpus_fingerprint(chapter_dir: Path, expected_count: int | None = None) -> dict:
    paths = sorted(chapter_dir.glob("[0-9][0-9][0-9][0-9].md"))
    if expected_count is not None:
        expected_names = [f"{unit:04d}.md" for unit in range(1, expected_count + 1)]
        if [path.name for path in paths] != expected_names:
            raise ValueError(f"corpus must contain exactly sequential units 0001-{expected_count:04d}")
    digest = hashlib.sha256()
    files = []
    byte_count = 0
    for path in paths:
        data = path.read_bytes()
        digest.update(data)
        byte_count += len(data)
        files.append({"unit": int(path.stem), "sha256": sha256_bytes(data), "byte_count": len(data)})
    return {
        "file_count": len(paths),
        "aggregate_byte_count": byte_count,
        "aggregate_sha256": digest.hexdigest(),
        "files": files,
    }


def require_fingerprint(actual: dict, expected: dict, label: str) -> None:
    summary = {key: actual[key] for key in expected}
    if summary != expected:
        raise ValueError(f"{label} fingerprint mismatch: expected {expected}, found {summary}")


def source_identity_key(decision: dict) -> tuple:
    return (
        decision.get("title_kind"),
        decision.get("source_title_number"),
        decision.get("side_story_number"),
        decision.get("korean_title_text"),
    )


def detect_source_number_discontinuities(decisions: list[dict]) -> list[dict]:
    """Detect number reuse without collapsing distinct exact title identities."""
    by_number = defaultdict(list)
    for item in decisions:
        if item.get("title_kind") == "main_story" and item.get("source_title_number") is not None:
            by_number[item["source_title_number"]].append(item)
    guards = []
    for number, items in sorted(by_number.items()):
        identities = defaultdict(list)
        for item in items:
            identities[item["korean_title_text"]].extend(item["unit_parts"])
        if len(identities) > 1:
            guards.append({
                "source_title_number": number,
                "distinct_korean_title_count": len(identities),
                "title_identities": [
                    {"korean_title_text": title, "unit_parts": parts}
                    for title, parts in sorted(identities.items())
                ],
                "automatic_cross_identity_recovery_forbidden": True,
                "guard": "Exact Korean title identity, not source number, owns each repeated-title family.",
            })
    return guards


def repeated_identity_supports_missing_recovery(decision: dict) -> bool:
    """Legacy diagnostic only; it never grants physical insertion authority."""
    return bool(
        decision.get("canonical_english_title_text")
        and any(not part.get("usable_english_title_present") for part in decision.get("unit_parts", []))
    )


def review_is_non_executable(decision: dict) -> bool:
    return (
        decision.get("decision") == "REVIEW_REQUIRED"
        and decision.get("canonical_english_title_text") is None
        and decision.get("automatic_application_forbidden") is True
        and decision.get("editorial_decision_required") is True
        and not decision.get("application_anchors")
    )


def first_nonblank_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _title_regex(title: str) -> re.Pattern:
    pieces = []
    for character in title:
        if character in "'’":
            pieces.append(r"(?:'|’|â€™)")
        else:
            pieces.append(re.escape(character))
    return re.compile("".join(pieces))


def _structural_title_line(line: str, unit: int, decision: dict) -> bool:
    """Require visible heading/wrapper evidence; plain body prose is ineligible."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#!/GEN_METHOD_TITLE_"):
        return False
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
        stripped = stripped[2:-2].strip()
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    if re.match(rf"(?i)^Chapter\s+{unit}\b", stripped):
        return True
    if decision.get("title_kind") == "side_story" and re.match(r"(?i)^Side\s+Story\b", stripped):
        return True
    if decision.get("title_kind") == "epilogue" and re.match(r"(?i)^Epilogue\b", stripped):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    source_number = decision.get("source_title_number")
    if isinstance(source_number, int) and re.match(rf"^<?\s*0*{source_number}\s*[.:]", stripped):
        return True
    return False


def _source_number_title_span(line: str, unit: int, decision: dict, part_number: int | None):
    """Extract exact title text from a source-number/part-anchored heading."""
    source_number = decision.get("source_title_number")
    if not isinstance(source_number, int) or not isinstance(part_number, int):
        return None
    pattern = re.compile(
        rf"^\s*(?:#{{1,6}}\s*)?(?:\*\*)?"
        rf"(?:Chapter\s+{unit}\s*(?:[:.\-–—]\s*)?)?"
        rf"(?:<\s*)?0*{source_number}\s*[.:]\s*"
        rf"(?P<title>.+?)\s*\({part_number}\)\s*(?:>\s*)?(?:\*\*)?\s*$",
        flags=re.IGNORECASE,
    )
    match = pattern.match(line)
    if not match:
        return None
    start, end = match.span("title")
    return start, end, line[start:end]


def _part_preserving_known_title_matches(
    line: str,
    known_found: list[tuple[int, int, str, str]],
    part_number: int | None,
) -> list[tuple[int, int, str, str]]:
    """Prefer title spans that leave a source-explicit part marker untouched."""
    if not isinstance(part_number, int):
        return []

    suffix_pattern = re.compile(
        rf"""\s*
            (?:\(\s*{part_number}\s*\)|{part_number})
            \s*
            (?:>\s*)?
            (?:\*\*)?
            \s*
            (?:["'\u201d\u2019])?
            \s*
            $
        """,
        flags=re.VERBOSE,
    )

    return [
        item
        for item in known_found
        if suffix_pattern.fullmatch(line[item[1]:])
    ]


def _first_line_classification(line: str, unit: int, decision: dict) -> str:
    stripped = line.strip()
    if not stripped:
        return "EMPTY"
    if stripped.startswith("#!/GEN_METHOD_TITLE_"):
        return "GENERATOR_CONTROL_MARKER"
    if re.fullmatch(rf"(?i)(?:#{{1,6}}\s*)?Chapter\s+{unit}\s*(?:<br\s*/?>)?", stripped):
        return "GENERIC_CHAPTER_HEADING"
    if _structural_title_line(line, unit, decision):
        return "STRUCTURED_TITLE_BEARING_LINE"
    return "PROSE_OR_UNRECOGNIZED"


def _known_title_candidates(decision: dict, part: dict) -> list[str]:
    values = []
    for value in [
        part.get("current_english_title_text"),
        decision.get("canonical_english_title_text"),
        *(item.get("english_title_text") for item in decision.get("ranked_existing_candidates", [])),
    ]:
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return values


def discover_physical_title_target(
    text: str,
    unit: int,
    decision: dict,
    part: dict,
    file_sha256: str,
) -> dict:
    """Discover an exact title line only inside a conservative opening region.

    At most the first 12 physical lines are inspected. Candidate text must be
    exact already-recorded English title evidence and the containing line must
    carry independent heading/wrapper structure. Arbitrary body prose is never
    searched or treated as a title.
    """
    lines = text.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    first = lines[first_index] if first_index is not None else ""
    region = lines[:TITLE_REGION_MAX_PHYSICAL_LINES]
    candidates = _known_title_candidates(decision, part)
    plausible = []
    matches = []
    for index, line in enumerate(region):
        if not line.strip():
            continue
        known_found = []
        for logical_title in candidates:
            for match in _title_regex(logical_title).finditer(line):
                known_found.append((match.start(), match.end(), logical_title, match.group(0)))
        stripped = line.strip()
        quoted_safe = False
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'", "“", "”"}:
            inner = stripped[1:-1].strip()
            quoted_safe = any(_title_regex(value).fullmatch(inner) for value in candidates)
        structural = _structural_title_line(line, unit, decision) or quoted_safe
        if structural and not line.strip().startswith("#!/GEN_METHOD_TITLE_"):
            plausible.append({
                "line_number": index + 1,
                "line": line,
                "line_sha256": sha256_bytes(line.encode("utf-8")),
            })
        if not structural:
            continue
        found = []
        part_number = part.get("part_number")
        structural_span = _source_number_title_span(
            line, unit, decision, part_number
        )

        if structural_span is not None:
            start, end, exact = structural_span
            found.append(
                (
                    start,
                    end,
                    exact.replace("\u00e2\u20ac\u2122", "'").replace("\u2019", "'"),
                    exact,
                )
            )
        else:
            part_preserving = _part_preserving_known_title_matches(
                line,
                known_found,
                part_number,
            )
            found.extend(part_preserving or known_found)

        if not found:
            continue
        # Prefer the longest exact known title when one candidate is contained
        # in another. Distinct physical spans remain ambiguous and fail closed.
        longest = max(end - start for start, end, _, _ in found)
        found = [item for item in found if item[1] - item[0] == longest]
        unique = {(start, end, exact): logical for start, end, logical, exact in found}
        for (start, end, exact), logical in unique.items():
            matches.append({
                "line_number": index + 1,
                "line": line,
                "line_sha256": sha256_bytes(line.encode("utf-8")),
                "title_text_span": {"start": start, "end": end, "exact_text": exact},
                "current_english_title_text": logical,
            })

    source_identity = {
        "normalized_korean_base_identity": decision["normalized_korean_base_title"],
        "korean_title_text": decision["korean_title_text"],
        "source_title_number": decision["source_title_number"],
        "side_story_number": decision["side_story_number"],
    }
    base = {
        "decision_id": decision["decision_id"],
        "unit": unit,
        "target_layer": "final/normalized_chapters",
        "baseline_normalized_chapter_file_sha256": file_sha256,
        "first_nonblank_line": first,
        "first_nonblank_line_number": None if first_index is None else first_index + 1,
        "first_nonblank_line_classification": _first_line_classification(first, unit, decision),
        "bounded_title_region_max_physical_lines": TITLE_REGION_MAX_PHYSICAL_LINES,
        "plausible_title_bearing_lines": plausible,
        "source_title_identity": source_identity,
        "title_kind": decision["title_kind"],
        "source_title_number": decision["source_title_number"],
        "side_story_number": decision["side_story_number"],
        "explicit_part_number": part["part_number"],
        "approved_canonical_english_title_text": decision.get("canonical_english_title_text"),
        "legacy_selected_heading_had_usable_title": bool(part.get("usable_english_title_present")),
    }
    physical_keys = {(item["line_number"], item["title_text_span"]["start"], item["title_text_span"]["end"]) for item in matches}
    if len(physical_keys) > 1:
        return {
            **base,
            "title_target_status": "AMBIGUOUS_TITLE_LOCATION",
            "title_location_classification": "AMBIGUOUS_TITLE_LOCATION",
            "matching_title_candidates": matches,
            "title_truly_absent": False,
            "recovery_required": False,
            "recovery_authorized": False,
            "recovery_review_required": True,
            "automatic_application_forbidden": True,
        }
    if not matches:
        return {
            **base,
            "title_target_status": "TRUE_TITLE_ABSENT",
            "title_location_classification": "TRUE_TITLE_ABSENT",
            "matching_title_candidates": [],
            "title_truly_absent": True,
            "recovery_required": True,
            "recovery_authorized": False,
            "recovery_review_required": True,
            "automatic_application_forbidden": True,
        }
    selected = matches[0]
    before = region[: selected["line_number"] - 1]
    generator_wrapped = any(line.strip() == "#!/GEN_METHOD_TITLE_START" for line in before)
    generic_before = any(
        re.fullmatch(rf"(?i)\s*(?:#{{1,6}}\s*)?Chapter\s+{unit}\s*(?:<br\s*/?>)?\s*", line)
        for line in before
    )
    stripped_selected = selected["line"].strip()
    quoted_nonstandard = (
        len(stripped_selected) >= 2
        and stripped_selected[0] == stripped_selected[-1]
        and stripped_selected[0] in {'"', "'", "“", "”"}
    )
    if generator_wrapped:
        location = "EXISTING_TITLE_GENERATOR_WRAPPED"
    elif quoted_nonstandard:
        location = "EXISTING_TITLE_NONSTANDARD_WRAPPER"
    elif selected["line_number"] == (None if first_index is None else first_index + 1):
        location = "EXISTING_TITLE_PRIMARY_HEADING"
    elif generic_before:
        location = "EXISTING_TITLE_SECONDARY_LINE"
    else:
        location = "EXISTING_TITLE_NONSTANDARD_WRAPPER"
    return {
        **base,
        **selected,
        "title_target_status": "EXISTING_TITLE",
        "title_location_classification": location,
        "title_truly_absent": False,
        "recovery_required": False,
        "recovery_authorized": False,
        "recovery_review_required": False,
        "automatic_application_forbidden": False,
    }


def build_physical_title_targets(
    decision: dict, normalized_dir: Path, file_hashes: dict[int, str]
) -> tuple[list[dict], list[dict], list[dict]]:
    targets = []
    anchors = []
    recovery_anchors = []
    parts = {item["final_unit"]: item for item in decision["unit_parts"]}
    for unit in decision["units"]:
        path = normalized_dir / f"{unit:04d}.md"
        text = path.read_text(encoding="utf-8")
        target = discover_physical_title_target(text, unit, decision, parts[unit], file_hashes[unit])
        recovery = ROGUE_AUTHORIZED_RECOVERIES.get(unit)
        if recovery is not None:
            if decision["decision"] != "APPROVED_CANONICAL" or target["title_target_status"] != "TRUE_TITLE_ABSENT":
                raise ValueError(f"unit {unit}: recovery authority does not target an approved absent title")
            checks = {
                "input SHA-256": (file_hashes[unit], recovery["input_sha256"]),
                "first line": (target["first_nonblank_line"], recovery["expected_original_first_line"]),
                "source identity": (target["source_title_identity"]["normalized_korean_base_identity"], recovery["source_identity"]),
                "part number": (target["explicit_part_number"], recovery["part_number"]),
            }
            for label, (actual, expected) in checks.items():
                if actual != expected:
                    raise ValueError(f"unit {unit}: authorized recovery {label} mismatch")
            raw = path.read_bytes()
            payload = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
            newline = "CRLF" if b"\r\n" in payload else "LF" if b"\n" in payload else None
            if newline is None:
                raise ValueError(f"unit {unit}: authorized recovery input has no newline convention")
            target.update({
                "title_target_status": "AUTHORIZED_RECOVERY_INSERTION",
                "recovery_authorization": "HUMAN_AUTHORIZED",
                "recovery_authorized": True,
                "recovery_review_required": False,
                "automatic_application_forbidden": False,
                "authorized_inserted_title_line": recovery["inserted_title_line"],
                "authorized_insertion_point": "LOGICAL_TEXT_START",
            })
            recovery_anchors.append({
                "decision_id": decision["decision_id"],
                "unit": unit,
                "target_layer": "final/normalized_chapters",
                "anchor_type": "HUMAN_AUTHORIZED_RECOVERY_INSERTION",
                "baseline_normalized_chapter_file_sha256": file_hashes[unit],
                "expected_original_first_line": recovery["expected_original_first_line"],
                "expected_original_first_line_number": 1,
                "inserted_title_line": recovery["inserted_title_line"],
                "insertion_point": "LOGICAL_TEXT_START",
                "blank_line_count_after_title": 1,
                "expected_input_bom": raw.startswith(b"\xef\xbb\xbf"),
                "expected_newline_convention": newline,
                "preserve_bom": True,
                "preserve_original_newline_convention": True,
                "immutable_original_content_suffix": True,
                "source_title_identity": target["source_title_identity"],
                "explicit_part_number": target["explicit_part_number"],
                "approved_canonical_english_title_text": target["approved_canonical_english_title_text"],
                "title_target_status": "AUTHORIZED_RECOVERY_INSERTION",
                "recovery_required": True,
                "recovery_authorized": True,
                "recovery_authorization": "HUMAN_AUTHORIZED",
                "automatic_application_forbidden": False,
            })
        targets.append(target)
        if decision["decision"] != "APPROVED_CANONICAL" or target["title_target_status"] != "EXISTING_TITLE":
            continue
        anchor = {
            key: target[key]
            for key in (
                "decision_id", "unit", "target_layer", "baseline_normalized_chapter_file_sha256",
                "source_title_identity", "explicit_part_number", "approved_canonical_english_title_text",
                "title_location_classification", "line_number", "line_sha256", "title_text_span",
                "current_english_title_text",
            )
        }
        anchor.update({
            "exact_current_heading_line": target["line"],
            "exact_title_bearing_line": target["line"],
            "exact_current_heading_line_occurrence_count": text.splitlines().count(target["line"]),
            "expected_occurrence_count": 1,
            "title_target_status": "EXISTING_TITLE",
            "recovery_required": False,
            "recovery_authorized": False,
            "expected_heading_structure": {
                "title_kind": decision["title_kind"],
                "source_title_number": decision["source_title_number"],
                "side_story_number": decision["side_story_number"],
                "canonical_english_title_text": decision["canonical_english_title_text"],
                "part_number": parts[unit]["part_number"],
            },
        })
        anchors.append(anchor)
    return targets, anchors, recovery_anchors


def resolve_title_decisions(
    input_decisions: list[dict],
    resolutions: dict[str, tuple[str, str]],
    unresolved_candidates: dict[str, list[str]],
    normalized_dir: Path,
    normalized_fingerprint: dict,
) -> tuple[list[dict], list[dict]]:
    original_reviews = {item["title_id"] for item in input_decisions if item["decision"] == "REVIEW_REQUIRED"}
    if set(resolutions) | set(unresolved_candidates) != original_reviews:
        raise ValueError("novel title evidence must account for every original REVIEW_REQUIRED group exactly once")
    file_hashes = {item["unit"]: item["sha256"] for item in normalized_fingerprint["files"]}
    approved_by_exact_korean_text = defaultdict(set)
    for item in input_decisions:
        if item.get("canonical_english_title_text"):
            approved_by_exact_korean_text[item["korean_title_text"]].add(item["canonical_english_title_text"])
    output = []
    review_table = []
    for old in input_decisions:
        item = json.loads(json.dumps(old, ensure_ascii=False))
        item["decision_id"] = item.pop("title_id")
        item["exact_korean_title_variants"] = [item["normalized_korean_base_title"]]
        item["explicit_part_numbers"] = list(item.pop("part_numbers"))
        item["units"] = list(item.pop("final_units"))
        item["automatic_application_safe"] = False
        item["automatic_application_forbidden"] = item["decision"] == "REVIEW_REQUIRED"
        item["editorial_decision_required"] = item["decision"] == "REVIEW_REQUIRED"
        item["editorial_candidates"] = list(item.pop("human_editorial_candidates", []))
        item["evidence_type"] = "accepted_normalization_title_plan"
        if item["decision_id"] in resolutions:
            canonical, reason = resolutions[item["decision_id"]]
            existing = {candidate["english_title_text"] for candidate in item["ranked_existing_candidates"]}
            # Exact Korean title-text reuse can establish an already-approved
            # canonical; otherwise novel evidence must select an existing form.
            reused = canonical in approved_by_exact_korean_text[item["korean_title_text"]]
            minor_match = fold_minor_title_variation(canonical) in {
                fold_minor_title_variation(value) for value in existing
            }
            if canonical not in existing and not reused and not minor_match:
                raise ValueError(f"promotion invents a non-existing title: {item['decision_id']}")
            item.update({
                "decision": "APPROVED_CANONICAL",
                "canonical_english_title_text": canonical,
                "confidence": "source_backed_editorial",
                "reason": reason,
                "evidence_type": (
                    "exact_korean_title_reuse_of_approved_canonical"
                    if reused and canonical not in existing
                    else "minor_punctuation_spacing_or_capitalization_normalization"
                    if minor_match and canonical not in existing
                    else "exact_source_identity_and_existing_english_semantic_comparison"
                ),
                "automatic_application_safe": True,
                "automatic_application_forbidden": False,
                "editorial_decision_required": False,
                "promoted_from_review_required": True,
            })
        elif item["decision_id"] in unresolved_candidates:
            item.update({
                "canonical_english_title_text": None,
                "confidence": "unresolved",
                "editorial_candidates": unresolved_candidates[item["decision_id"]],
                "automatic_application_safe": False,
                "automatic_application_forbidden": True,
                "editorial_decision_required": True,
                "promoted_from_review_required": False,
            })
        else:
            item["confidence"] = "accepted_v2"
            item["promoted_from_review_required"] = False

        # Editorial approval and physical insertion authority are deliberately
        # separate. Full-file discovery creates exact existing-title anchors;
        # true absence/ambiguity creates non-executable per-target review data.
        item["missing_title_recovery_supported"] = False
        item["physical_title_targets"], item["application_anchors"], item["recovery_insertion_anchors"] = build_physical_title_targets(
            item, normalized_dir, file_hashes
        )
        if item["decision"] == "REVIEW_REQUIRED" and not review_is_non_executable(item):
            raise ValueError(f"review-required title exposes executable authority: {item['decision_id']}")
        if old["decision"] == "REVIEW_REQUIRED":
            review_table.append({
                "group_identifier": item["decision_id"],
                "korean_title": item["normalized_korean_base_title"],
                "units": item["units"],
                "parts": item["explicit_part_numbers"],
                "current_english_variants": item["ranked_existing_candidates"],
                "decision": "PROMOTE_TO_APPROVED_CANONICAL" if item["decision"] == "APPROVED_CANONICAL" else "KEEP_REVIEW_REQUIRED",
                "proposed_canonical_english_title_text": item["canonical_english_title_text"],
                "editorial_candidates": item["editorial_candidates"],
                "reason": item["reason"],
            })
        output.append(item)
    return output, review_table


def build_plan(project) -> dict:
    reports = project.root / "final" / "reports"
    normalized_dir = project.root / "final" / "normalized_chapters"
    baseline_dir = project.root / "final" / "chapters"
    inventory_path = reports / "normalization_inventory.json"
    normalization_plan_path = reports / "normalization_plan.json"
    alignment_path = reports / "source_alignment.json"
    production_path = reports / "production_inventory.json"
    inputs = {}
    for name, path in {
        "normalization_inventory": inventory_path,
        "normalization_plan": normalization_plan_path,
        "source_alignment": alignment_path,
        "production_inventory": production_path,
    }.items():
        data = path.read_bytes()
        inputs[name] = {"path": path.as_posix(), "sha256": sha256_bytes(data)}

    source_plan = json.loads(normalization_plan_path.read_text(encoding="utf-8"))
    input_counts = Counter(item["decision"] for item in source_plan["title_decisions"])
    if {key: input_counts[key] for key in EXPECTED_INPUT_COUNTS} != EXPECTED_INPUT_COUNTS:
        raise ValueError("accepted input title counts changed; record and review the upstream change first")
    baseline = corpus_fingerprint(baseline_dir, EXPECTED_BASELINE["file_count"])
    normalized = corpus_fingerprint(normalized_dir, EXPECTED_NORMALIZED["file_count"])
    require_fingerprint(baseline, EXPECTED_BASELINE, "immutable baseline")
    require_fingerprint(normalized, EXPECTED_NORMALIZED, "normalized title base")

    decisions, review_table = resolve_title_decisions(
        source_plan["title_decisions"],
        ROGUE_REVIEW_RESOLUTIONS,
        ROGUE_UNRESOLVED_NOTES,
        normalized_dir,
        normalized,
    )
    counts = Counter(item["decision"] for item in decisions)
    promoted = [item["decision_id"] for item in decisions if item["promoted_from_review_required"]]
    unresolved = [item["decision_id"] for item in decisions if item["decision"] == "REVIEW_REQUIRED"]
    approved_targets = [
        target
        for item in decisions if item["decision"] == "APPROVED_CANONICAL"
        for target in item["physical_title_targets"]
    ]
    anchors = [anchor for item in decisions for anchor in item["application_anchors"]]
    recovery_anchors = [anchor for item in decisions for anchor in item["recovery_insertion_anchors"]]
    true_absent = [target for target in approved_targets if target["title_truly_absent"]]
    ambiguous = [target for target in approved_targets if target["title_target_status"] == "AMBIGUOUS_TITLE_LOCATION"]
    recovery_reviews = [target for target in approved_targets if target["recovery_review_required"]]
    suspect_audit = [target for target in approved_targets if not target["legacy_selected_heading_had_usable_title"]]
    noops = [
        anchor for anchor in anchors
        if anchor["current_english_title_text"] == anchor["approved_canonical_english_title_text"]
    ]
    # The accepted source-spine scan includes singleton identities that are not
    # repeated-title groups, so it is authoritative for discontinuity guards.
    discontinuities = json.loads(json.dumps(source_plan["source_title_discontinuities"], ensure_ascii=False))
    for guard in discontinuities:
        guard["automatic_cross_identity_recovery_forbidden"] = True
    if not HUMAN_DOWNGRADED_IDS <= set(unresolved):
        raise ValueError("a human-downgraded semantic title was promoted without explicit override authority")
    for required in REQUIRED_DISCONTINUITY_GUARDS:
        number = required["source_title_number"]
        guard = next((item for item in discontinuities if item["source_title_number"] == number), None)
        if not guard or guard["distinct_korean_title_count"] < required["min_distinct_korean_title_count"]:
            raise ValueError(f"source-number-{number} discontinuity guard was lost")

    plan = {
        "schema_version": 2,
        "plan_revision": "title-resolution-v1.3",
        "mode": "source-backed-editorial-title-plan-read-only-zero-api",
        "inputs": inputs,
        "corpora": {"immutable_baseline": baseline, "normalized_title_base": normalized},
        "safety": {
            "applies_titles": False,
            "chapter_prose_modified": False,
            "normalized_chapters_are_future_application_target": True,
            "review_required_is_executable": False,
            "api_calls": 0,
        },
        "original_title_counts": EXPECTED_INPUT_COUNTS,
        "reviewed_review_required_group_count": len(review_table),
        "promoted_group_count": len(promoted),
        "remaining_review_required_group_count": len(unresolved),
        "final_title_counts": dict(sorted(counts.items())),
        "promoted_group_ids": promoted,
        "unresolved_group_ids": unresolved,
        "human_downgraded_group_ids": sorted(HUMAN_DOWNGRADED_IDS),
        "approved_title_target_count": len(approved_targets),
        "existing_title_executable_anchor_count": len(anchors),
        "existing_title_noop_anchor_count": len(noops),
        "existing_title_changing_anchor_count": len(anchors) - len(noops),
        "authorized_recovery_insertion_anchor_count": len(recovery_anchors),
        "authorized_recovery_insertion_units": [anchor["unit"] for anchor in recovery_anchors],
        "executable_title_target_count": len(anchors) + len(recovery_anchors),
        "true_title_absent_target_count": len(true_absent),
        "true_title_absent_units": [target["unit"] for target in true_absent],
        "ambiguous_title_target_count": len(ambiguous),
        "ambiguous_title_units": [target["unit"] for target in ambiguous],
        "recovery_review_required_target_count": len(recovery_reviews),
        "recovery_review_required_units": [target["unit"] for target in recovery_reviews],
        "legacy_parser_missed_target_count": len(suspect_audit),
        "suspect_title_target_audit": suspect_audit,
        "unresolved_non_executable_group_count": len(unresolved),
        "title_decisions": decisions,
        "review_of_original_48": review_table,
        "source_number_discontinuity_guards": discontinuities,
        "source_number_43_guard_preserved": True,
    }
    if UNIT_99_GUARD is not None:
        guard = dict(UNIT_99_GUARD)
        unit = guard.get("unit")
        if unit is not None:
            line = next((target["line_number"] for target in suspect_audit if target["unit"] == unit), None)
            if line is not None:
                guard["title_line_number"] = line
        plan["unit_99_guard"] = guard
    return plan


def render_markdown(plan: dict) -> str:
    counts = plan["final_title_counts"]
    lines = [
        "# Reviewed Title Resolution Plan V1.2",
        "",
        "Read-only editorial plan: no chapter title or prose has been applied.",
        "",
        "## Summary",
        "",
        f"- Original review-required groups inspected: {plan['reviewed_review_required_group_count']}",
        f"- Promoted: {plan['promoted_group_count']}",
        f"- Remaining review-required: {plan['remaining_review_required_group_count']}",
        f"- Final counts: APPROVED_CANONICAL {counts.get('APPROVED_CANONICAL', 0)}; REVIEW_REQUIRED {counts.get('REVIEW_REQUIRED', 0)}; ALREADY_CONSISTENT {counts.get('ALREADY_CONSISTENT', 0)}; STRUCTURAL_ONLY {counts.get('STRUCTURAL_ONLY', 0)}",
        f"- Approved title target units: {plan['approved_title_target_count']}",
        f"- Exact existing-title executable anchors: {plan['existing_title_executable_anchor_count']}",
        f"- Existing-title semantic no-op anchors: {plan['existing_title_noop_anchor_count']}",
        f"- Existing-title title-changing anchors: {plan['existing_title_changing_anchor_count']}",
        f"- Human-authorized recovery insertion anchors: {plan['authorized_recovery_insertion_anchor_count']} ({plan['authorized_recovery_insertion_units']})",
        f"- True-title-absent recovery reviews: {plan['true_title_absent_target_count']} ({plan['true_title_absent_units']})",
        f"- Ambiguous title locations: {plan['ambiguous_title_target_count']} ({plan['ambiguous_title_units']})",
        "- Every unresolved record is explicitly non-executable.",
        "",
        "## Review of original unresolved groups",
        "",
        "| ID | Korean title | Units / parts | Existing English variants | Decision | Canonical / candidates | Reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in plan["review_of_original_48"]:
        variants = "; ".join(f"{x['english_title_text']} ({x['occurrence_count']})" for x in row["current_english_variants"])
        result = row["proposed_canonical_english_title_text"] or "; ".join(row["editorial_candidates"]) or "—"
        units = ", ".join(map(str, row["units"]))
        parts = ", ".join("—" if value is None else str(value) for value in row["parts"])
        cells = [row["group_identifier"], row["korean_title"], f"U {units}; P {parts}", variants, row["decision"], result, row["reason"]]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in cells) + " |")
    lines.extend([
        "",
        "## Full-file audit of the 19 legacy parser misses",
        "",
        "The discovery rule inspects only the first 12 physical lines and accepts exact already-recorded English title evidence only on independently structured heading/wrapper lines. It never searches arbitrary body prose.",
        "",
        "| Unit | Source identity | Part | First nonblank line | First-line class | Plausible opening title lines | Selected title line / locator | Classification | Truly absent | Recovery required | Recovery authorized | Ambiguous |",
        "|---:|---|---:|---|---|---|---|---|---|---|---|---|",
    ])
    for target in plan["suspect_title_target_audit"]:
        plausible = "; ".join(
            f"L{item['line_number']}: {item['line']}" for item in target["plausible_title_bearing_lines"]
        ) or "—"
        selected = (
            f"L{target['line_number']}: {target['line']}"
            if target["title_target_status"] == "EXISTING_TITLE" else
            f"INSERT@logical-start: {target['authorized_inserted_title_line']}"
            if target["title_target_status"] == "AUTHORIZED_RECOVERY_INSERTION" else "—"
        )
        identity = target["source_title_identity"]
        source = identity["normalized_korean_base_identity"]
        cells = [
            target["unit"], source, target["explicit_part_number"], target["first_nonblank_line"],
            target["first_nonblank_line_classification"],
            plausible, selected, target["title_location_classification"], target["title_truly_absent"],
            target["recovery_required"], target["recovery_authorized"],
            target["title_target_status"] == "AMBIGUOUS_TITLE_LOCATION",
        ]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in cells) + " |")
    lines.extend([
        "",
        "## Guards",
        "",
        "- Reviewed unit-level title-location guards apply only when their private specifics are supplied externally; `<br>` and generator markers remain untouched publication artifacts.",
        "- A reviewed source-number discontinuity remains split across reviewed title families; no missing part is synthesized.",
        "- All source-number reuse/discontinuity guards use exact source title identity and fail closed.",
        "- The human-downgraded groups remain REVIEW_REQUIRED.",
    ])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    if not PRIVATE_INPUTS_AVAILABLE:
        print("PRIVATE_INPUTS_UNAVAILABLE: provide the operator-local JSON input file; no novel-specific mappings are embedded in this public source.", file=_sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    args = parser.parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            plan = build_plan(project)
            report_dir = project.root / "final" / "reports"
            json_path = report_dir / "title_resolution_plan.json"
            md_path = report_dir / "title_resolution_plan.md"
            json_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            md_path.write_text(render_markdown(plan), encoding="utf-8")
    except (OSError, ValueError, ProductionRunLockedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(json.dumps({
        "reviewed": plan["reviewed_review_required_group_count"],
        "promoted": plan["promoted_group_count"],
        "remaining": plan["remaining_review_required_group_count"],
        "approved_targets": plan["approved_title_target_count"],
        "existing_anchors": plan["existing_title_executable_anchor_count"],
        "recovery_reviews": plan["recovery_review_required_target_count"],
        "authorized_recovery_insertions": plan["authorized_recovery_insertion_anchor_count"],
        "final_counts": plan["final_title_counts"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
