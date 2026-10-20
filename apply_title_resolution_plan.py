"""Fail-closed, transactional application of an approved title-resolution plan.

This local-only tool copies ``final/normalized_chapters`` to a new
``final/titled_chapters`` layer and changes only title text authorized by exact
plan anchors.  It never imports or invokes translation code.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock
from proven_stage_cleanup import (
    cleanup_proof_id,
    cleanup_proven_stages,
    inspect_stage_cleanup,
)


class TitleApplicationError(ValueError):
    """A fail-closed title plan or corpus validation failure."""


def _artifact_exists(path: Path) -> bool:
    """Return True for filesystem entries including dangling symlinks."""
    return path.is_symlink() or path.exists()


@dataclass(frozen=True)
class TitleEdit:
    decision_id: str
    unit: int
    line_index: int
    old_line: str
    new_line: str
    input_file_sha256: str
    canonical_title: str
    source_identity: dict[str, Any]
    title_kind: str
    source_title_number: int | None
    side_story_number: int | None
    explicit_part: int
    classification: str
    missing_title_recovery: bool


@dataclass
class ApplicationPreview:
    project_id: str
    input_dir: Path
    plan_path: Path
    plan_sha256: str
    plan: dict[str, Any]
    input_fingerprint: dict[str, Any]
    authorized_anchors: list[dict[str, Any]]
    edits: list[TitleEdit]
    noops: list[dict[str, Any]]
    automatic_anchor_count: int
    manual_anchor_count: int
    blocked_manual_anchor_count: int
    decision_counts: dict[str, int]
    unresolved_groups: list[str]
    structural_groups: list[str]
    already_consistent_groups: list[str]
    recovery_review_targets: list[dict[str, Any]]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def corpus_fingerprint(chapter_dir: Path, expected_count: int | None = None) -> dict[str, Any]:
    paths = sorted(chapter_dir.glob("[0-9][0-9][0-9][0-9].md"))
    if expected_count is not None:
        expected = [f"{n:04d}.md" for n in range(1, expected_count + 1)]
        if [path.name for path in paths] != expected:
            raise TitleApplicationError(
                f"corpus must contain exactly sequential units 0001-{expected_count:04d}"
            )
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


def load_plan_file(path: Path, expected_sha256: str | None = None) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    actual = sha256_bytes(data)
    if expected_sha256 is not None and actual != expected_sha256.lower():
        raise TitleApplicationError(
            f"title plan SHA-256 mismatch: expected {expected_sha256.lower()}, found {actual}"
        )
    try:
        plan = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TitleApplicationError(f"invalid UTF-8 title plan JSON: {exc}") from exc
    return plan, actual


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise TitleApplicationError(f"{label} mismatch: expected {expected!r}, found {actual!r}")


def _validate_plan_contract(plan: dict[str, Any], project_id: str) -> None:
    if (
        plan.get("schema_version") != 2
        or plan.get("plan_revision")
        not in {"title-resolution-v1.2", "title-resolution-v1.3"}
    ):
        raise TitleApplicationError(
            "unsupported title-resolution plan schema/revision"
        )
    decisions = plan.get("title_decisions")
    if not isinstance(decisions, list):
        raise TitleApplicationError("title_decisions must be a list")
    allowed = {"APPROVED_CANONICAL", "REVIEW_REQUIRED", "ALREADY_CONSISTENT", "STRUCTURAL_ONLY"}
    ids: set[str] = set()
    counts = Counter()
    for decision in decisions:
        decision_id = decision.get("decision_id")
        status = decision.get("decision")
        if not isinstance(decision_id, str) or decision_id in ids:
            raise TitleApplicationError("decision IDs must be unique non-empty strings")
        ids.add(decision_id)
        if status not in allowed:
            raise TitleApplicationError(f"{decision_id}: unsupported decision {status!r}")
        counts[status] += 1
        anchors = decision.get("application_anchors")
        recovery_anchors = decision.get("recovery_insertion_anchors")
        targets = decision.get("physical_title_targets")
        if not isinstance(anchors, list):
            raise TitleApplicationError(f"{decision_id}: application_anchors must be a list")
        if not isinstance(recovery_anchors, list):
            raise TitleApplicationError(f"{decision_id}: recovery_insertion_anchors must be a list")
        if not isinstance(targets, list) or len(targets) != len(decision.get("units", [])):
            raise TitleApplicationError(f"{decision_id}: physical_title_targets must account for every unit")
        if status != "APPROVED_CANONICAL" and (anchors or recovery_anchors):
            raise TitleApplicationError(f"{decision_id}: non-approved decision contains executable anchors")
        if status in {"REVIEW_REQUIRED", "STRUCTURAL_ONLY"} and not decision.get(
            "automatic_application_forbidden", status == "STRUCTURAL_ONLY"
        ) and decision.get("executable_edit"):
            raise TitleApplicationError(f"{decision_id}: non-executable decision attempts execution")
    normalized_counts = {name: counts.get(name, 0) for name in sorted(allowed)}
    _require_equal(normalized_counts, plan.get("final_title_counts"), "title decision counts")
    anchor_count = sum(
        len(d["application_anchors"]) for d in decisions if d["decision"] == "APPROVED_CANONICAL"
    )
    target_count = sum(
        len(d["physical_title_targets"]) for d in decisions if d["decision"] == "APPROVED_CANONICAL"
    )
    _require_equal(target_count, plan.get("approved_title_target_count"), "approved target count")
    _require_equal(anchor_count, plan.get("existing_title_executable_anchor_count"), "existing-title anchor count")
    recovery_anchor_count = sum(
        len(d["recovery_insertion_anchors"]) for d in decisions if d["decision"] == "APPROVED_CANONICAL"
    )
    _require_equal(recovery_anchor_count, plan.get("authorized_recovery_insertion_anchor_count"), "authorized recovery anchor count")
    _require_equal(anchor_count + recovery_anchor_count, plan.get("executable_title_target_count"), "executable title target count")
    corpus = plan.get("corpora", {}).get("normalized_title_base", {})
    if not all(key in corpus for key in ("file_count", "aggregate_byte_count", "aggregate_sha256", "files")):
        raise TitleApplicationError("plan lacks an exact normalized title-base fingerprint")
    # project_id is deliberately not inferred from title content.  The selected
    # novel supplies paths; the plan supplies every novel-specific decision.
    if not project_id:
        raise TitleApplicationError("project ID is required")


def _verify_input(input_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    expected = plan["corpora"]["normalized_title_base"]
    actual = corpus_fingerprint(input_dir, expected["file_count"])
    for key in ("file_count", "aggregate_byte_count", "aggregate_sha256"):
        _require_equal(actual[key], expected[key], f"normalized input {key}")
    expected_files = {item["unit"]: item for item in expected["files"]}
    _require_equal(len(expected_files), actual["file_count"], "normalized input file inventory")
    for item in actual["files"]:
        expected_item = expected_files.get(item["unit"])
        if expected_item is None:
            raise TitleApplicationError(f"unit {item['unit']}: absent from plan file inventory")
        _require_equal(item["sha256"], expected_item["sha256"], f"unit {item['unit']} SHA-256")
        _require_equal(item["byte_count"], expected_item["byte_count"], f"unit {item['unit']} byte count")
    return actual


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _apostrophe_pattern(ch: str) -> str:
    if ch in "'\u2019":
        return r"(?:'|\u2019|â€™)"
    return re.escape(ch)


def _title_pattern(title: str) -> re.Pattern[str]:
    return re.compile("".join(_apostrophe_pattern(ch) for ch in title))


def _explicit_part_remains_outside_title_span(
    body: str,
    span_end: int,
    part_number: int,
) -> bool:
    """Return whether a source-explicit part marker survives after the edit span."""
    suffix = body[span_end:]

    pattern = re.compile(
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

    return pattern.fullmatch(suffix) is not None


def _ordinary_new_line(old_line: str, anchor: dict[str, Any], canonical: str) -> str:
    ending = _line_ending(old_line)
    body = old_line[: -len(ending)] if ending else old_line
    span = anchor["title_text_span"]
    start, end = span["start"], span["end"]
    _require_equal(body[start:end], span["exact_text"], "anchored title-text span")

    part_number = anchor.get("explicit_part_number")
    if (
        isinstance(part_number, int)
        and not _explicit_part_remains_outside_title_span(
            body,
            end,
            part_number,
        )
    ):
        raise TitleApplicationError(
            "source-explicit part marker must remain outside "
            "the editable title-text span"
        )

    return body[:start] + canonical + body[end:] + ending


def _missing_recovery_new_line(old_line: str, anchor: dict[str, Any]) -> str:
    structure = anchor["expected_heading_structure"]
    unit = anchor["unit"]
    canonical = anchor["approved_canonical_english_title_text"]
    part = anchor["explicit_part_number"]
    ending = _line_ending(old_line)
    body = old_line[: -len(ending)] if ending else old_line
    br_match = re.search(r"(?i)(\s*<br\s*/?>)\s*$", body)
    br = br_match.group(1) if br_match else ""
    base = body[: br_match.start()] if br_match else body
    if not re.fullmatch(rf"\s*(?:#{{1,6}}\s*)?Chapter\s+{unit}\s*", base, flags=re.IGNORECASE):
        raise TitleApplicationError(f"unit {unit}: missing-title recovery requires an exact generic chapter heading")
    kind = structure["title_kind"]
    source_number = structure.get("source_title_number")
    side_number = structure.get("side_story_number")
    if kind == "main_story" and isinstance(source_number, int):
        recovered = f"{base}. {source_number}. {canonical} ({part})"
    elif kind == "side_story" and isinstance(side_number, int):
        recovered = f"{base}. Side Story {side_number}. {canonical} ({part})"
    else:
        raise TitleApplicationError(f"unit {unit}: unsupported deterministic missing-title structure")
    return recovered + br + ending


def _recovery_newline(payload: bytes) -> tuple[str, bytes]:
    if b"\r\n" in payload:
        if payload.replace(b"\r\n", b"").find(b"\n") >= 0:
            raise TitleApplicationError("authorized recovery input has mixed newline conventions")
        return "CRLF", b"\r\n"
    if b"\n" in payload:
        return "LF", b"\n"
    raise TitleApplicationError("authorized recovery input has no newline convention")


def _validate_authorized_recovery_anchor(
    decision: dict[str, Any], target: dict[str, Any], anchor: dict[str, Any], input_dir: Path
) -> TitleEdit:
    decision_id = decision["decision_id"]
    unit = target.get("unit")
    for item, label in ((target, "target"), (anchor, "anchor")):
        _require_equal(item.get("decision_id"), decision_id, f"{decision_id}/{unit} recovery {label} decision ID")
        _require_equal(item.get("unit"), unit, f"{decision_id}/{unit} recovery {label} unit")
        _require_equal(item.get("title_target_status"), "AUTHORIZED_RECOVERY_INSERTION", f"{decision_id}/{unit} recovery {label} status")
        _require_equal(item.get("recovery_required"), True, f"{decision_id}/{unit} recovery {label} required")
        _require_equal(item.get("recovery_authorized"), True, f"{decision_id}/{unit} recovery {label} authorization")
        _require_equal(item.get("recovery_authorization"), "HUMAN_AUTHORIZED", f"{decision_id}/{unit} recovery {label} authority")
        _require_equal(item.get("automatic_application_forbidden"), False, f"{decision_id}/{unit} recovery {label} prohibition")
    _require_equal(target.get("recovery_review_required"), False, f"{decision_id}/{unit} recovery review")
    _require_equal(anchor.get("anchor_type"), "HUMAN_AUTHORIZED_RECOVERY_INSERTION", f"{decision_id}/{unit} recovery anchor type")
    _require_equal(anchor.get("target_layer"), "final/normalized_chapters", f"{decision_id}/{unit} recovery target layer")
    _require_equal(anchor.get("insertion_point"), "LOGICAL_TEXT_START", f"{decision_id}/{unit} insertion point")
    _require_equal(anchor.get("blank_line_count_after_title"), 1, f"{decision_id}/{unit} blank line count")
    _require_equal(anchor.get("preserve_bom"), True, f"{decision_id}/{unit} BOM preservation")
    _require_equal(anchor.get("preserve_original_newline_convention"), True, f"{decision_id}/{unit} newline preservation")
    _require_equal(anchor.get("immutable_original_content_suffix"), True, f"{decision_id}/{unit} immutable suffix")
    canonical = decision.get("canonical_english_title_text")
    _require_equal(anchor.get("approved_canonical_english_title_text"), canonical, f"{decision_id}/{unit} recovery canonical title")
    parts = {item["final_unit"]: item["part_number"] for item in decision.get("unit_parts", [])}
    _require_equal(anchor.get("explicit_part_number"), parts.get(unit), f"{decision_id}/{unit} recovery part")
    identity_expected = {
        "normalized_korean_base_identity": decision.get("normalized_korean_base_title"),
        "korean_title_text": decision.get("korean_title_text"),
        "source_title_number": decision.get("source_title_number"),
        "side_story_number": decision.get("side_story_number"),
    }
    _require_equal(anchor.get("source_title_identity"), identity_expected, f"{decision_id}/{unit} recovery source identity")
    path = input_dir / f"{unit:04d}.md"
    data = path.read_bytes()
    _require_equal(sha256_bytes(data), anchor.get("baseline_normalized_chapter_file_sha256"), f"{decision_id}/{unit} recovery input SHA-256")
    has_bom = data.startswith(b"\xef\xbb\xbf")
    _require_equal(has_bom, anchor.get("expected_input_bom"), f"{decision_id}/{unit} input BOM")
    payload = data[3:] if has_bom else data
    newline_name, newline = _recovery_newline(payload)
    _require_equal(newline_name, anchor.get("expected_newline_convention"), f"{decision_id}/{unit} newline convention")
    try:
        first_line = payload.split(newline, 1)[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TitleApplicationError(f"{decision_id}/{unit}: recovery input is not UTF-8") from exc
    _require_equal(anchor.get("expected_original_first_line_number"), 1, f"{decision_id}/{unit} original first-line number")
    _require_equal(first_line, anchor.get("expected_original_first_line"), f"{decision_id}/{unit} original first line")
    inserted = anchor.get("inserted_title_line")
    if not isinstance(inserted, str) or not inserted or "\n" in inserted or "\r" in inserted:
        raise TitleApplicationError(f"{decision_id}/{unit}: authorized inserted title must be exactly one line")
    return TitleEdit(
        decision_id=decision_id, unit=unit, line_index=-1, old_line="",
        new_line=inserted + newline.decode("ascii") * 2,
        input_file_sha256=anchor["baseline_normalized_chapter_file_sha256"],
        canonical_title=canonical, source_identity=anchor["source_title_identity"],
        title_kind=decision["title_kind"], source_title_number=decision.get("source_title_number"),
        side_story_number=decision.get("side_story_number"), explicit_part=anchor["explicit_part_number"],
        classification="human_authorized_recovery", missing_title_recovery=True,
    )


def _validate_anchor(decision: dict[str, Any], anchor: dict[str, Any], input_dir: Path) -> tuple[int, str]:
    decision_id = decision["decision_id"]
    _require_equal(anchor.get("decision_id"), decision_id, f"{decision_id} anchor decision ID")
    unit = anchor.get("unit")
    if not isinstance(unit, int) or unit not in decision.get("units", []):
        raise TitleApplicationError(f"{decision_id}: invalid anchor unit {unit!r}")
    _require_equal(anchor.get("target_layer"), "final/normalized_chapters", f"{decision_id}/{unit} target layer")
    _require_equal(anchor.get("title_target_status"), "EXISTING_TITLE", f"{decision_id}/{unit} target status")
    _require_equal(anchor.get("recovery_required"), False, f"{decision_id}/{unit} recovery required")
    _require_equal(anchor.get("recovery_authorized"), False, f"{decision_id}/{unit} recovery authorization")
    canonical = decision.get("canonical_english_title_text")
    if not isinstance(canonical, str) or not canonical:
        raise TitleApplicationError(f"{decision_id}: approved decision lacks canonical title text")
    _require_equal(anchor.get("approved_canonical_english_title_text"), canonical, f"{decision_id}/{unit} canonical title")
    expected_structure = anchor.get("expected_heading_structure")
    if not isinstance(expected_structure, dict):
        raise TitleApplicationError(f"{decision_id}/{unit}: missing expected heading structure")
    for key in ("title_kind", "source_title_number", "side_story_number"):
        _require_equal(expected_structure.get(key), decision.get(key), f"{decision_id}/{unit} structure {key}")
    _require_equal(expected_structure.get("canonical_english_title_text"), canonical, f"{decision_id}/{unit} structure title")
    parts = {item["final_unit"]: item["part_number"] for item in decision.get("unit_parts", [])}
    if unit not in parts or (parts[unit] is not None and not isinstance(parts[unit], int)):
        raise TitleApplicationError(f"{decision_id}/{unit}: source part evidence is invalid")
    for value, label in ((anchor.get("explicit_part_number"), "anchor part"), (expected_structure.get("part_number"), "structure part")):
        _require_equal(value, parts[unit], f"{decision_id}/{unit} {label}")
    identity = anchor.get("source_title_identity")
    if not isinstance(identity, dict):
        raise TitleApplicationError(f"{decision_id}/{unit}: source identity is required")
    identity_expected = {
        "normalized_korean_base_identity": decision.get("normalized_korean_base_title"),
        "korean_title_text": decision.get("korean_title_text"),
        "source_title_number": decision.get("source_title_number"),
        "side_story_number": decision.get("side_story_number"),
    }
    _require_equal(identity, identity_expected, f"{decision_id}/{unit} source title identity")
    path = input_dir / f"{unit:04d}.md"
    data = path.read_bytes()
    _require_equal(sha256_bytes(data), anchor.get("baseline_normalized_chapter_file_sha256"), f"{decision_id}/{unit} input file SHA-256")
    text = data.decode("utf-8")
    heading = anchor.get("exact_current_heading_line")
    if not isinstance(heading, str) or not heading:
        raise TitleApplicationError(f"{decision_id}/{unit}: exact current heading is required")
    _require_equal(anchor.get("exact_title_bearing_line"), heading, f"{decision_id}/{unit} title-bearing line")
    _require_equal(sha256_bytes(heading.encode("utf-8")), anchor.get("line_sha256"), f"{decision_id}/{unit} title-line SHA-256")
    lines = text.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == heading]
    _require_equal(len(indexes), anchor.get("exact_current_heading_line_occurrence_count"), f"{decision_id}/{unit} heading occurrence count")
    _require_equal(anchor.get("expected_occurrence_count"), 1, f"{decision_id}/{unit} expected occurrence count")
    if len(indexes) != 1:
        raise TitleApplicationError(f"{decision_id}/{unit}: exact heading must occur once")
    line_number = anchor.get("line_number")
    if not isinstance(line_number, int) or line_number < 1:
        raise TitleApplicationError(f"{decision_id}/{unit}: exact one-based line number is required")
    _require_equal(indexes[0], line_number - 1, f"{decision_id}/{unit} title-line locator")
    span = anchor.get("title_text_span")
    if not isinstance(span, dict) or not all(isinstance(span.get(key), int) for key in ("start", "end")):
        raise TitleApplicationError(f"{decision_id}/{unit}: exact title-text span is required")
    start, end = span["start"], span["end"]
    if not (0 <= start < end <= len(heading)):
        raise TitleApplicationError(f"{decision_id}/{unit}: title-text span is out of bounds")
    _require_equal(heading[start:end], span.get("exact_text"), f"{decision_id}/{unit} exact title-text span")
    if not isinstance(anchor.get("current_english_title_text"), str) or not anchor["current_english_title_text"]:
        raise TitleApplicationError(f"{decision_id}/{unit}: current English title evidence is required")
    return indexes[0], lines[indexes[0]]


def _validate_recovery_review_target(decision: dict[str, Any], target: dict[str, Any]) -> None:
    decision_id = decision["decision_id"]
    unit = target.get("unit")
    _require_equal(target.get("decision_id"), decision_id, f"{decision_id}/{unit} recovery target decision ID")
    if target.get("title_target_status") not in {"TRUE_TITLE_ABSENT", "AMBIGUOUS_TITLE_LOCATION"}:
        raise TitleApplicationError(f"{decision_id}/{unit}: unsupported non-existing title target status")
    _require_equal(target.get("recovery_authorized"), False, f"{decision_id}/{unit} recovery authorization")
    _require_equal(target.get("recovery_review_required"), True, f"{decision_id}/{unit} recovery review")
    _require_equal(target.get("automatic_application_forbidden"), True, f"{decision_id}/{unit} automatic recovery prohibition")


def prepare_application(
    input_dir: Path,
    plan: dict[str, Any],
    plan_sha256: str,
    project_id: str,
    allow_manual_approved: bool = False,
    plan_path: Path | None = None,
) -> ApplicationPreview:
    _validate_plan_contract(plan, project_id)
    fingerprint = _verify_input(input_dir, plan)
    decisions = plan["title_decisions"]
    edits: list[TitleEdit] = []
    noops: list[dict[str, Any]] = []
    authorized: list[dict[str, Any]] = []
    automatic = manual = blocked_manual = 0
    ownership: dict[tuple[int, str], str] = {}
    recovery_review_targets: list[dict[str, Any]] = []
    for decision in decisions:
        status = decision["decision"]
        if status != "APPROVED_CANONICAL":
            continue
        if decision.get("automatic_application_forbidden") is not False:
            raise TitleApplicationError(f"{decision['decision_id']}: approved decision is application-forbidden")
        is_manual = decision.get("automatic_application_safe") is not True
        anchors = decision["application_anchors"]
        recovery_anchors = decision["recovery_insertion_anchors"]
        target_by_unit = {target["unit"]: target for target in decision["physical_title_targets"]}
        if len(target_by_unit) != len(decision["physical_title_targets"]):
            raise TitleApplicationError(f"{decision['decision_id']}: duplicate physical title target unit")
        anchor_units = {anchor["unit"] for anchor in anchors}
        recovery_anchor_by_unit = {anchor["unit"]: anchor for anchor in recovery_anchors}
        if len(recovery_anchor_by_unit) != len(recovery_anchors):
            raise TitleApplicationError(f"{decision['decision_id']}: duplicate recovery insertion anchor unit")
        if anchor_units & set(recovery_anchor_by_unit):
            raise TitleApplicationError(f"{decision['decision_id']}: target has both existing and recovery anchors")
        for target in decision["physical_title_targets"]:
            if target["title_target_status"] == "EXISTING_TITLE":
                if target["unit"] not in anchor_units:
                    raise TitleApplicationError(f"{decision['decision_id']}/{target['unit']}: existing title lacks executable anchor")
            elif target["title_target_status"] == "AUTHORIZED_RECOVERY_INSERTION":
                anchor = recovery_anchor_by_unit.get(target["unit"])
                if anchor is None:
                    raise TitleApplicationError(f"{decision['decision_id']}/{target['unit']}: authorized recovery lacks exact anchor")
                edit = _validate_authorized_recovery_anchor(decision, target, anchor, input_dir)
                edits.append(edit)
                authorized.append(anchor)
            else:
                _validate_recovery_review_target(decision, target)
                if target["unit"] in anchor_units:
                    raise TitleApplicationError(f"{decision['decision_id']}/{target['unit']}: recovery-review target exposes an anchor")
                recovery_review_targets.append(target)
        if set(recovery_anchor_by_unit) - set(target_by_unit):
            raise TitleApplicationError(f"{decision['decision_id']}: recovery anchor lacks physical target")
        if is_manual and not allow_manual_approved:
            blocked_manual += len(anchors)
            continue
        if is_manual:
            manual += len(anchors)
        else:
            automatic += len(anchors)
        for anchor in anchors:
            line_index, old_line = _validate_anchor(decision, anchor, input_dir)
            key = (anchor["unit"], anchor["exact_current_heading_line"])
            if key in ownership:
                raise TitleApplicationError(
                    f"duplicate executable anchor ownership: {ownership[key]} and {decision['decision_id']}"
                )
            ownership[key] = decision["decision_id"]
            current = anchor["current_english_title_text"]
            if current == decision["canonical_english_title_text"]:
                noops.append({
                    "decision_id": decision["decision_id"], "unit": anchor["unit"],
                    "classification": "manual_approved" if is_manual else "automatic_approved",
                    "input_file_sha256": anchor["baseline_normalized_chapter_file_sha256"],
                    "source_title_identity": anchor["source_title_identity"],
                    "explicit_part": anchor["explicit_part_number"],
                    "canonical_title": decision["canonical_english_title_text"],
                })
                authorized.append(anchor)
                continue
            else:
                new_line = _ordinary_new_line(old_line, anchor, decision["canonical_english_title_text"])
            edit = TitleEdit(
                decision_id=decision["decision_id"], unit=anchor["unit"], line_index=line_index,
                old_line=old_line, new_line=new_line,
                input_file_sha256=anchor["baseline_normalized_chapter_file_sha256"],
                canonical_title=decision["canonical_english_title_text"],
                source_identity=anchor["source_title_identity"], title_kind=decision["title_kind"],
                source_title_number=decision.get("source_title_number"),
                side_story_number=decision.get("side_story_number"), explicit_part=anchor["explicit_part_number"],
                classification="manual_approved" if is_manual else "automatic_approved",
                missing_title_recovery=False,
            )
            if edit.old_line == edit.new_line:
                raise TitleApplicationError(f"{edit.decision_id}/{edit.unit}: byte edit unexpectedly became a no-op")
            edits.append(edit)
            authorized.append(anchor)
    by_line = defaultdict(list)
    for edit in edits:
        by_line[(edit.unit, edit.line_index)].append(edit)
    if any(len(group) != 1 for group in by_line.values()):
        raise TitleApplicationError("conflicting or overlapping title-line edits")
    decision_counts = dict(Counter(d["decision"] for d in decisions))
    return ApplicationPreview(
        project_id=project_id, input_dir=input_dir, plan_path=plan_path or Path("<memory>"),
        plan_sha256=plan_sha256, plan=plan, input_fingerprint=fingerprint,
        authorized_anchors=authorized, edits=edits, noops=noops,
        automatic_anchor_count=automatic, manual_anchor_count=manual,
        blocked_manual_anchor_count=blocked_manual, decision_counts=decision_counts,
        unresolved_groups=[d["decision_id"] for d in decisions if d["decision"] == "REVIEW_REQUIRED"],
        structural_groups=[d["decision_id"] for d in decisions if d["decision"] == "STRUCTURAL_ONLY"],
        already_consistent_groups=[d["decision_id"] for d in decisions if d["decision"] == "ALREADY_CONSISTENT"],
        recovery_review_targets=sorted(recovery_review_targets, key=lambda item: item["unit"]),
    )


def dry_run_summary(preview: ApplicationPreview) -> dict[str, Any]:
    recovery_count = sum(e.missing_title_recovery for e in preview.edits)
    return {
        "approved_target_count": preview.plan["approved_title_target_count"],
        "approved_anchor_count": len(preview.authorized_anchors),
        "existing_title_anchor_count": len(preview.authorized_anchors) - recovery_count,
        "automatic_approved_anchor_count": preview.automatic_anchor_count,
        "manual_approved_anchor_count": preview.manual_anchor_count,
        "manual_approved_blocked_anchor_count": preview.blocked_manual_anchor_count,
        "approved_noop_anchor_count": len(preview.noops),
        "approved_changed_anchor_count": len(preview.edits),
        "existing_title_changed_anchor_count": len(preview.edits) - recovery_count,
        "missing_title_recovery_count": recovery_count,
        "recovery_review_required_target_count": len(preview.recovery_review_targets),
        "recovery_review_required_units": [item["unit"] for item in preview.recovery_review_targets],
        "ready_for_application": not preview.recovery_review_targets and not preview.blocked_manual_anchor_count,
        "blocker_count": len(preview.recovery_review_targets) + preview.blocked_manual_anchor_count,
        "modified_units": sorted({e.unit for e in preview.edits}),
        "writes_performed": 0,
    }


def _apply_to_stage(preview: ApplicationPreview, stage: Path) -> None:
    shutil.copytree(preview.input_dir, stage)
    by_unit = defaultdict(list)
    for edit in preview.edits:
        by_unit[edit.unit].append(edit)
    for unit, edits in by_unit.items():
        path = stage / f"{unit:04d}.md"
        recovery_edits = [edit for edit in edits if edit.missing_title_recovery]
        if recovery_edits:
            if len(edits) != 1:
                raise TitleApplicationError(f"unit {unit}: recovery insertion overlaps another edit")
            edit = recovery_edits[0]
            original = (preview.input_dir / f"{unit:04d}.md").read_bytes()
            _require_equal(sha256_bytes(original), edit.input_file_sha256, f"unit {unit} staged recovery input SHA-256")
            bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
            payload = original[len(bom):]
            output = bom + edit.new_line.encode("utf-8") + payload
            if not output.endswith(payload):
                raise TitleApplicationError(f"unit {unit}: original content is not an immutable output suffix")
            path.write_bytes(output)
            continue
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        for edit in edits:
            if lines[edit.line_index] != edit.old_line:
                raise TitleApplicationError(f"unit {unit}: staging heading drift")
            lines[edit.line_index] = edit.new_line
        path.write_bytes("".join(lines).encode("utf-8"))


def verify_titled_corpus(preview: ApplicationPreview, output_dir: Path) -> dict[str, Any]:
    expected_names = [f"{n:04d}.md" for n in range(1, preview.input_fingerprint["file_count"] + 1)]
    names = sorted(path.name for path in output_dir.glob("*.md"))
    _require_equal(names, expected_names, "titled output file inventory")
    edits_by_unit = defaultdict(dict)
    for edit in preview.edits:
        if edit.line_index in edits_by_unit[edit.unit]:
            raise TitleApplicationError("independent verifier found conflicting authorized line edits")
        edits_by_unit[edit.unit][edit.line_index] = edit
    changed_units = []
    recovery_suffix_verified_units = []
    output_hashes = {}
    for unit in range(1, preview.input_fingerprint["file_count"] + 1):
        input_data = (preview.input_dir / f"{unit:04d}.md").read_bytes()
        output_data = (output_dir / f"{unit:04d}.md").read_bytes()
        output_hashes[unit] = sha256_bytes(output_data)
        if input_data == output_data:
            if edits_by_unit.get(unit):
                raise TitleApplicationError(f"unit {unit}: expected title edit is absent")
            continue
        changed_units.append(unit)
        recovery_edits = [edit for edit in edits_by_unit.get(unit, {}).values() if edit.missing_title_recovery]
        if recovery_edits:
            if len(recovery_edits) != 1 or len(edits_by_unit[unit]) != 1:
                raise TitleApplicationError(f"unit {unit}: invalid recovery edit ownership")
            edit = recovery_edits[0]
            bom = b"\xef\xbb\xbf" if input_data.startswith(b"\xef\xbb\xbf") else b""
            payload = input_data[len(bom):]
            expected = bom + edit.new_line.encode("utf-8") + payload
            _require_equal(output_data, expected, f"unit {unit} exact authorized recovery output")
            if not output_data.endswith(payload):
                raise TitleApplicationError(f"unit {unit}: original payload is not an immutable suffix")
            recovery_suffix_verified_units.append(unit)
            continue
        before = input_data.decode("utf-8").splitlines(keepends=True)
        after = output_data.decode("utf-8").splitlines(keepends=True)
        if len(before) != len(after):
            raise TitleApplicationError(f"unit {unit}: unauthorized line-count/body mutation")
        actual_differences = [i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        expected_differences = sorted(edits_by_unit.get(unit, {}))
        _require_equal(actual_differences, expected_differences, f"unit {unit} authorized title-line differences")
        for index in actual_differences:
            edit = edits_by_unit[unit][index]
            _require_equal(before[index], edit.old_line, f"unit {unit} old heading")
            _require_equal(after[index], edit.new_line, f"unit {unit} new heading")
    _require_equal(changed_units, sorted({e.unit for e in preview.edits}), "modified unit list")
    output_fingerprint = corpus_fingerprint(output_dir, preview.input_fingerprint["file_count"])
    return {
        "status": "passed",
        "file_count": output_fingerprint["file_count"],
        "changed_units": changed_units,
        "every_difference_maps_to_approved_anchor": True,
        "all_non_title_bytes_identical": True,
        "recovery_suffix_verified_units": recovery_suffix_verified_units,
        "output_file_hashes": output_hashes,
        "output_fingerprint": output_fingerprint,
    }


def _edit_report(edit: TitleEdit, output_hash: str) -> dict[str, Any]:
    return {
        "decision_id": edit.decision_id, "unit": edit.unit,
        "input_file_sha256": edit.input_file_sha256, "output_file_sha256": output_hash,
        "old_heading_sha256": sha256_bytes(edit.old_line.encode("utf-8")),
        "new_heading_sha256": sha256_bytes(edit.new_line.encode("utf-8")),
        "old_heading": edit.old_line.rstrip("\r\n"), "new_heading": edit.new_line.rstrip("\r\n"),
        "source_title_identity": edit.source_identity, "title_kind": edit.title_kind,
        "source_title_number": edit.source_title_number, "side_story_number": edit.side_story_number,
        "explicit_part": edit.explicit_part, "canonical_title": edit.canonical_title,
        "application_classification": edit.classification,
        "missing_title_recovery": edit.missing_title_recovery,
    }


def build_application_report(preview: ApplicationPreview, verification: dict[str, Any]) -> dict[str, Any]:
    output_fingerprint = verification["output_fingerprint"]
    recovery_count = sum(e.missing_title_recovery for e in preview.edits)
    return {
        "schema_version": 1, "application_revision": "title-application-v1",
        "project_id": preview.project_id, "title_plan_sha256": preview.plan_sha256,
        "normalized_input_fingerprint": preview.input_fingerprint,
        "titled_output_fingerprint": output_fingerprint,
        "title_decision_counts": preview.decision_counts,
        "approved_anchor_count": len(preview.authorized_anchors),
        "automatic_approved_anchor_count": preview.automatic_anchor_count,
        "manual_approved_anchor_count": preview.manual_anchor_count,
        "approved_noop_anchor_count": len(preview.noops),
        "approved_changed_anchor_count": len(preview.edits),
        "existing_title_changed_anchor_count": len(preview.edits) - recovery_count,
        "missing_title_recovery_count": recovery_count,
        "modified_unit_count": len(verification["changed_units"]),
        "modified_units": verification["changed_units"],
        "review_required_groups_skipped": preview.unresolved_groups,
        "structural_only_groups_skipped": preview.structural_groups,
        "already_consistent_groups": preview.already_consistent_groups,
        "review_required_groups_applied": 0, "structural_only_groups_applied": 0,
        "already_consistent_semantic_rewrites": 0,
        "no_op_approved_anchors": preview.noops,
        "edits": [_edit_report(e, verification["output_file_hashes"][e.unit]) for e in preview.edits],
        "independent_verification": {k: v for k, v in verification.items() if k not in {"output_file_hashes", "output_fingerprint"}},
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    fp_in = report["normalized_input_fingerprint"]
    fp_out = report["titled_output_fingerprint"]
    lines = [
        "# Title Application Report", "", f"- Project: `{report['project_id']}`",
        f"- Title plan SHA-256: `{report['title_plan_sha256']}`",
        f"- Approved anchors: {report['approved_anchor_count']}",
        f"- Automatic approved anchors: {report['automatic_approved_anchor_count']}",
        f"- Manual approved anchors: {report['manual_approved_anchor_count']}",
        f"- Approved no-op anchors: {report['approved_noop_anchor_count']}",
        f"- Approved changed anchors: {report['approved_changed_anchor_count']}",
        f"- Missing-title recoveries: {report['missing_title_recovery_count']}",
        f"- Modified units: {report['modified_units']}",
        f"- Normalized input: {fp_in['file_count']} files, {fp_in['aggregate_byte_count']} bytes, `{fp_in['aggregate_sha256']}`",
        f"- Titled output: {fp_out['file_count']} files, {fp_out['aggregate_byte_count']} bytes, `{fp_out['aggregate_sha256']}`",
        f"- Independent verification: **{report['independent_verification']['status'].upper()}**", "",
        "## Skipped groups", "",
        f"- REVIEW_REQUIRED: {report['review_required_groups_skipped']}",
        f"- STRUCTURAL_ONLY: {report['structural_only_groups_skipped']}",
        f"- ALREADY_CONSISTENT: {report['already_consistent_groups']}", "", "## Byte-changing title edits", "",
    ]
    for edit in report["edits"]:
        lines.extend([
            f"- Unit {edit['unit']} / `{edit['decision_id']}` / {edit['application_classification']}",
            f"  - old: `{edit['old_heading']}`",
            f"  - new: `{edit['new_heading']}`",
            f"  - old/new heading SHA-256: `{edit['old_heading_sha256']}` / `{edit['new_heading_sha256']}`",
        ])
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _recover_exact_interrupted_title_prefix(
    preview: ApplicationPreview,
    *,
    output_dir: Path,
    report_json_path: Path,
    report_md_path: Path,
    current_stage: Path,
    expected_verification: dict[str, Any],
    expected_json_bytes: bytes,
    expected_md_bytes: bytes,
) -> bool:
    """Remove only independently proven uncommitted title-application residue."""
    if _artifact_exists(output_dir):
        raise TitleApplicationError(
            f"refusing to overwrite existing output: {output_dir}"
        )

    report_json_candidates = []
    if _artifact_exists(report_json_path):
        report_json_candidates.append(report_json_path)
    report_json_candidates.extend(
        sorted(
            report_json_path.parent.glob(f".{report_json_path.name}.*"),
            key=lambda item: item.name.casefold(),
        )
    )
    report_md_candidates = []
    if _artifact_exists(report_md_path):
        report_md_candidates.append(report_md_path)
    report_md_candidates.extend(
        sorted(
            report_md_path.parent.glob(f".{report_md_path.name}.*"),
            key=lambda item: item.name.casefold(),
        )
    )
    cleanup_identity = {
        "project_id": preview.project_id,
        "title_plan_sha256": preview.plan_sha256,
        "input_fingerprint": preview.input_fingerprint,
        "expected_verification": expected_verification,
        "expected_json_sha256": sha256_bytes(expected_json_bytes),
        "expected_markdown_sha256": sha256_bytes(expected_md_bytes),
    }
    stage_cleanup = inspect_stage_cleanup(
        output_dir.parent,
        namespace="title-application",
        stage_prefix=f".{output_dir.name}.staging-",
        proof_id=cleanup_proof_id("title-application", cleanup_identity),
        current_stage=current_stage,
        error_cls=TitleApplicationError,
    )
    stage_candidates = list(stage_cleanup.stage_candidates)

    residue_present = bool(
        report_json_candidates
        or report_md_candidates
        or stage_cleanup.residue_present
    )
    if not residue_present:
        return False

    for path in report_json_candidates:
        if path.is_symlink() or not path.is_file():
            raise TitleApplicationError(
                f"ambiguous interrupted title JSON artifact: {path}"
            )
        if path.read_bytes() != expected_json_bytes:
            raise TitleApplicationError(
                f"interrupted title JSON artifact does not match reconstructed bytes: {path}"
            )

    for path in report_md_candidates:
        if path.is_symlink() or not path.is_file():
            raise TitleApplicationError(
                f"ambiguous interrupted title Markdown artifact: {path}"
            )
        if path.read_bytes() != expected_md_bytes:
            raise TitleApplicationError(
                f"interrupted title Markdown artifact does not match reconstructed bytes: {path}"
            )

    proven_stages: list[Path] = []
    for stage in stage_candidates:
        if stage.is_symlink() or not stage.is_dir():
            raise TitleApplicationError(
                f"ambiguous interrupted title stage artifact: {stage}"
            )
        verification = verify_titled_corpus(preview, stage)
        if verification != expected_verification:
            raise TitleApplicationError(
                f"interrupted title stage does not match reconstructed output: {stage}"
            )
        proven_stages.append(stage)

    for path in report_md_candidates:
        path.unlink()
    for path in report_json_candidates:
        path.unlink()
    cleanup_proven_stages(
        stage_cleanup,
        proven_stages,
        error_cls=TitleApplicationError,
    )
    return True


def _prove_terminal_title_commit_after_replace_error(
    preview: ApplicationPreview,
    *,
    stage: Path,
    output_dir: Path,
    report_json_path: Path,
    report_md_path: Path,
    expected_verification: dict[str, Any],
    expected_json_bytes: bytes,
    expected_md_bytes: bytes,
) -> bool:
    """Prove that terminal title publication committed despite a reported replace error."""
    if _artifact_exists(stage):
        return False
    if (
        not output_dir.exists()
        or output_dir.is_symlink()
        or not output_dir.is_dir()
    ):
        return False

    for path, expected in (
        (report_json_path, expected_json_bytes),
        (report_md_path, expected_md_bytes),
    ):
        if not path.exists() or path.is_symlink() or not path.is_file():
            return False
        try:
            if path.read_bytes() != expected:
                return False
        except OSError:
            return False
        if any(path.parent.glob(f".{path.name}.*")):
            return False

    try:
        current_input = corpus_fingerprint(
            preview.input_dir,
            preview.input_fingerprint["file_count"],
        )
        if current_input != preview.input_fingerprint:
            return False
        installed_verification = verify_titled_corpus(preview, output_dir)
        if installed_verification != expected_verification:
            return False
    except (OSError, TitleApplicationError):
        return False
    return True


def publish_titled_corpus(
    preview: ApplicationPreview, output_dir: Path, report_json_path: Path, report_md_path: Path
) -> dict[str, Any]:
    if _artifact_exists(output_dir):
        raise TitleApplicationError(f"refusing to overwrite existing output: {output_dir}")
    if preview.blocked_manual_anchor_count:
        raise TitleApplicationError("approved manual anchors remain blocked; pass explicit manual-approved authorization")
    if preview.recovery_review_targets:
        units = [item["unit"] for item in preview.recovery_review_targets]
        raise TitleApplicationError(f"recovery-review-required title targets remain non-executable: {units}")
    expected_executable = (
        preview.plan["existing_title_executable_anchor_count"]
        + preview.plan["authorized_recovery_insertion_anchor_count"]
    )
    if len(preview.authorized_anchors) != expected_executable:
        raise TitleApplicationError("not every existing/recovery title anchor is authorized/accounted for")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent)
    )
    stage.rmdir()
    reports_written: list[Path] = []
    verification: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    json_bytes: bytes | None = None
    md_bytes: bytes | None = None
    try:
        _apply_to_stage(preview, stage)
        verification = verify_titled_corpus(preview, stage)
        report = build_application_report(preview, verification)
        json_bytes = (
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        md_bytes = render_markdown_report(report).encode("utf-8")
        _recover_exact_interrupted_title_prefix(
            preview,
            output_dir=output_dir,
            report_json_path=report_json_path,
            report_md_path=report_md_path,
            current_stage=stage,
            expected_verification=verification,
            expected_json_bytes=json_bytes,
            expected_md_bytes=md_bytes,
        )
        for path in (output_dir, report_json_path, report_md_path):
            if _artifact_exists(path):
                raise TitleApplicationError(
                    f"refusing to overwrite existing output after prefix recovery: {path}"
                )
        _atomic_write(report_json_path, json_bytes)
        reports_written.append(report_json_path)
        _atomic_write(report_md_path, md_bytes)
        reports_written.append(report_md_path)
        os.replace(stage, output_dir)
        return report
    except Exception as exc:
        if _artifact_exists(output_dir):
            if (
                verification is not None
                and report is not None
                and json_bytes is not None
                and md_bytes is not None
                and _prove_terminal_title_commit_after_replace_error(
                    preview,
                    stage=stage,
                    output_dir=output_dir,
                    report_json_path=report_json_path,
                    report_md_path=report_md_path,
                    expected_verification=verification,
                    expected_json_bytes=json_bytes,
                    expected_md_bytes=md_bytes,
                )
            ):
                return report
            raise TitleApplicationError(
                "ambiguous terminal title commit outcome after replace error; "
                "existing output and reports were preserved"
            ) from exc

        if stage.exists():
            shutil.rmtree(stage)
        for path in reversed(reports_written):
            path.unlink(missing_ok=True)
        raise


def verify_existing_application(
    preview: ApplicationPreview, output_dir: Path, report_json_path: Path
) -> dict[str, Any]:
    if not output_dir.is_dir() or not report_json_path.is_file():
        raise TitleApplicationError("verify-only requires titled corpus and JSON application report")
    report = json.loads(report_json_path.read_text(encoding="utf-8"))
    _require_equal(report.get("title_plan_sha256"), preview.plan_sha256, "application report title plan SHA-256")
    _require_equal(report.get("normalized_input_fingerprint", {}).get("aggregate_sha256"), preview.input_fingerprint["aggregate_sha256"], "application report input SHA-256")
    verification = verify_titled_corpus(preview, output_dir)
    _require_equal(report.get("titled_output_fingerprint"), verification["output_fingerprint"], "application report output fingerprint")
    return verification


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--allow-manual-approved", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--expect-approved-targets", type=int)
    parser.add_argument("--expect-existing-anchors", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = select_project(args.novel)
    final_dir = project.root / "final"
    input_dir = final_dir / "normalized_chapters"
    output_dir = final_dir / "titled_chapters"
    reports = final_dir / "reports"
    plan_path = reports / "title_resolution_plan.json"
    report_json = reports / "title_application.json"
    report_md = reports / "title_application.md"
    try:
        lock = production_run_lock(project.root) if args.apply else nullcontext()
        with lock:
            plan, plan_sha = load_plan_file(plan_path, args.expect_plan_sha256)
            preview = prepare_application(
                input_dir, plan, plan_sha, args.novel, args.allow_manual_approved, plan_path
            )
            if args.expect_approved_targets is not None:
                _require_equal(plan["approved_title_target_count"], args.expect_approved_targets, "approved title target count")
            if args.expect_existing_anchors is not None:
                recovery_count = sum(e.missing_title_recovery for e in preview.edits)
                _require_equal(len(preview.authorized_anchors) - recovery_count, args.expect_existing_anchors, "authorized existing-title anchor count")
            if args.dry_run:
                print(json.dumps(dry_run_summary(preview), indent=2))
                if preview.recovery_review_targets:
                    print(
                        "ERROR: recovery-review-required title targets remain non-executable: "
                        f"{[item['unit'] for item in preview.recovery_review_targets]}"
                    )
                    return 1
            elif args.apply:
                report = publish_titled_corpus(preview, output_dir, report_json, report_md)
                print(json.dumps({
                    "status": "applied", "approved_anchor_count": report["approved_anchor_count"],
                    "changed_anchor_count": report["approved_changed_anchor_count"],
                    "modified_units": report["modified_units"],
                    "output_fingerprint": report["titled_output_fingerprint"],
                }, indent=2))
            else:
                verification = verify_existing_application(preview, output_dir, report_json)
                print(json.dumps({"status": "verified", **verification["output_fingerprint"]}, indent=2))
    except (
        TitleApplicationError,
        OSError,
        json.JSONDecodeError,
        ProductionRunLockedError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
