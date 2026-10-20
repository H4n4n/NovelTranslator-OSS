"""Apply an audited normalization plan to an immutable finalization corpus.

Only exact, occurrence-anchored prose corrections are supported.  The source
corpus is never modified: a complete normalized copy is staged, independently
verified, and atomically published only after every precondition succeeds.
This module is local-only and has no translation or network dependencies.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock
from proven_stage_cleanup import (
    cleanup_proof_id,
    cleanup_proven_stages,
    inspect_stage_cleanup,
)


APPLICATION_SCHEMA_VERSION = 1
SUPPORTED_PLAN_SCHEMA_VERSION = 3
SUPPORTED_PLAN_REVISION = "2.1"
SUPPORTED_LAYER = "prose"
APPROVED_STATUS = "APPROVED_CORRECTION"
REVIEW_STATUS = "REVIEW_REQUIRED"
NO_CHANGE_STATUS = "FALSE_POSITIVE_NO_CHANGE"
ALLOWED_TEXT_STATUSES = {APPROVED_STATUS, REVIEW_STATUS, NO_CHANGE_STATUS}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PARAGRAPH_SEPARATOR_RE = re.compile(r"\r?\n\s*\r?\n")
ASCII_WORD_TEMPLATE = r"(?<![A-Za-z]){value}(?![A-Za-z])"


class NormalizationError(ValueError):
    """A fail-closed normalization precondition or verification failure."""


def _artifact_exists(path: Path) -> bool:
    """Return True for filesystem entries including dangling symlinks."""
    return path.is_symlink() or path.exists()


@dataclass(frozen=True)
class Expectations:
    baseline_files: int | None = None
    baseline_bytes: int | None = None
    baseline_sha256: str | None = None
    approved_records: int | None = None
    automatic_occurrences: int | None = None
    manual_occurrences: int | None = None
    total_occurrences: int | None = None


@dataclass(frozen=True)
class ResolvedEdit:
    owner_id: str
    occurrence_id: str
    kind: str
    unit: int
    filename: str
    target_paragraph: int
    paragraph_start: int
    start: int
    end: int
    old_text: str
    replacement: str
    occurrence_weight: int = 1

    @property
    def global_start(self) -> int:
        return self.paragraph_start + self.start

    @property
    def global_end(self) -> int:
        return self.paragraph_start + self.end


@dataclass
class ApplicationPreview:
    project_id: str
    layer: str
    baseline_dir: Path
    plan_path: Path | None
    plan_sha256: str
    plan: dict[str, Any]
    baseline_fingerprint: dict[str, Any]
    edits: list[ResolvedEdit]
    approved_record_count: int
    automatic_occurrence_count: int
    manual_record_count: int
    manual_occurrence_count: int
    review_record_count: int
    review_occurrence_count: int
    no_change_record_count: int
    no_change_occurrence_count: int
    examined_unit_count: int
    inventory_sha256: str | None = None
    inventory_verified: bool = False

    @property
    def total_occurrence_count(self) -> int:
        return self.automatic_occurrence_count + self.manual_occurrence_count

    @property
    def edit_span_count(self) -> int:
        return len(self.edits)

    @property
    def modified_units(self) -> list[int]:
        return sorted({edit.unit for edit in self.edits})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise NormalizationError(f"{label} must be a lowercase 64-character SHA-256")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise NormalizationError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise NormalizationError(f"{label} must be a nonnegative integer")
    return value


def exact_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    """Match the exact-plan scanner's conservative ASCII word boundaries."""
    if not isinstance(value, str) or not value:
        return []
    if value.isascii() and any(character.isalpha() for character in value):
        pattern = re.compile(
            ASCII_WORD_TEMPLATE.format(value=re.escape(value)),
            re.IGNORECASE,
        )
        return [(match.start(), match.end()) for match in pattern.finditer(text)]
    return [
        (match.start(), match.end())
        for match in re.finditer(re.escape(value), text)
    ]


def paragraph_spans(text: str) -> list[tuple[str, int, int]]:
    """Return the same paragraph sequence as the plan builder, with offsets."""
    spans: list[tuple[str, int, int]] = []
    start = 0
    for match in PARAGRAPH_SEPARATOR_RE.finditer(text):
        part = text[start:match.start()]
        if part.strip():
            spans.append((part, start, match.start()))
        start = match.end()
    part = text[start:]
    if part.strip():
        spans.append((part, start, len(text)))
    return spans


def physical_occurrence_id(corpus_id: str, anchor: dict[str, Any]) -> str:
    payload = {
        "corpus_id": corpus_id,
        "unit": anchor["expected_final_unit"],
        "baseline_file_sha256": anchor["expected_baseline_file_sha256"],
        "target_paragraph": anchor["target_paragraph"],
        "target_paragraph_sha256": anchor["expected_target_paragraph_sha256"],
        "exact_old_text": anchor["exact_old_text"],
        "target_occurrence": anchor["target_occurrence"],
        "target_start": anchor.get("target_start"),
        "target_end": anchor.get("target_end"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"occurrence:{sha256_bytes(encoded)}"


def load_plan_file(
    plan_path: Path,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    try:
        raw = plan_path.read_bytes()
    except OSError as exc:
        raise NormalizationError(f"cannot read normalization plan: {plan_path}: {exc}") from exc
    actual_sha256 = sha256_bytes(raw)
    if expected_sha256 is not None:
        expected_sha256 = _require_sha256(expected_sha256, "expected plan SHA-256")
        if actual_sha256 != expected_sha256:
            raise NormalizationError(
                f"source plan SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
    try:
        plan = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"normalization plan is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise NormalizationError("normalization plan root must be an object")
    return plan, actual_sha256


def corpus_fingerprint(
    chapter_dir: Path,
    expected_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not chapter_dir.is_dir():
        raise NormalizationError(f"chapter corpus does not exist: {chapter_dir}")
    children = sorted(chapter_dir.iterdir(), key=lambda path: path.name)
    if any(not child.is_file() for child in children):
        raise NormalizationError(f"chapter corpus contains a non-file entry: {chapter_dir}")
    names = [child.name for child in children]
    if expected_files is not None:
        expected_names = list(expected_files)
        if names != expected_names:
            missing = sorted(set(expected_names) - set(names))
            added = sorted(set(names) - set(expected_names))
            raise NormalizationError(
                f"chapter file set mismatch; missing={missing}, added={added}"
            )
    digest = hashlib.sha256()
    total_bytes = 0
    files = []
    for path in children:
        data = path.read_bytes()
        digest.update(data)
        total_bytes += len(data)
        files.append({
            "name": path.name,
            "unit": int(path.stem) if re.fullmatch(r"[0-9]{4}", path.stem) else None,
            "sha256": sha256_bytes(data),
            "byte_count": len(data),
        })
    return {
        "file_count": len(children),
        "aggregate_byte_count": total_bytes,
        "aggregate_sha256": digest.hexdigest(),
        "files": files,
    }


def _expected_file_inventory(plan: dict[str, Any]) -> tuple[list[str], dict[int, dict[str, Any]]]:
    try:
        recorded = plan["corpus"]["verified_final_fingerprint"]
        records = recorded["files"]
    except (KeyError, TypeError) as exc:
        raise NormalizationError("plan is missing its verified baseline file inventory") from exc
    if not isinstance(records, list) or not records:
        raise NormalizationError("plan baseline file inventory must be a non-empty list")
    by_unit: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise NormalizationError(f"plan baseline file record {index} must be an object")
        unit = _require_positive_int(record.get("unit"), f"baseline file record {index} unit")
        if unit in by_unit:
            raise NormalizationError(f"duplicate baseline unit in plan: {unit}")
        _require_sha256(record.get("sha256"), f"baseline unit {unit} SHA-256")
        _require_nonnegative_int(record.get("byte_count"), f"baseline unit {unit} byte count")
        by_unit[unit] = record
    units = sorted(by_unit)
    if units != list(range(1, len(units) + 1)):
        raise NormalizationError("plan baseline units must be a complete sequence beginning at 1")
    names = [f"{unit:04d}.md" for unit in units]
    return names, by_unit


def _verify_baseline(
    baseline_dir: Path,
    plan: dict[str, Any],
    expectations: Expectations,
) -> dict[str, Any]:
    expected_names, expected_by_unit = _expected_file_inventory(plan)
    actual = corpus_fingerprint(baseline_dir, expected_names)
    recorded = plan["corpus"]["verified_final_fingerprint"]
    checks = (
        ("file count", actual["file_count"], recorded.get("file_count")),
        ("aggregate byte count", actual["aggregate_byte_count"], recorded.get("aggregate_byte_count")),
        ("aggregate SHA-256", actual["aggregate_sha256"], recorded.get("aggregate_sha256")),
    )
    for label, observed, expected in checks:
        if observed != expected:
            raise NormalizationError(
                f"baseline {label} mismatch: expected {expected}, got {observed}"
            )
    recorded_again = plan["corpus"].get("inventory_recorded_final_sha256")
    if recorded_again != actual["aggregate_sha256"]:
        raise NormalizationError(
            "plan inventory and verified baseline aggregate SHA-256 values disagree"
        )
    for actual_file in actual["files"]:
        unit = actual_file["unit"]
        expected = expected_by_unit[unit]
        if actual_file["sha256"] != expected["sha256"]:
            raise NormalizationError(f"baseline unit {unit} SHA-256 mismatch")
        if actual_file["byte_count"] != expected["byte_count"]:
            raise NormalizationError(f"baseline unit {unit} byte count mismatch")
    explicit = (
        ("baseline file count", actual["file_count"], expectations.baseline_files),
        ("baseline byte count", actual["aggregate_byte_count"], expectations.baseline_bytes),
        ("baseline SHA-256", actual["aggregate_sha256"], expectations.baseline_sha256),
    )
    for label, observed, expected in explicit:
        if expected is not None and observed != expected:
            raise NormalizationError(f"{label} invariant failed: expected {expected}, got {observed}")
    return actual


def _validate_plan_contract(plan: dict[str, Any], project_id: str, layer: str) -> None:
    if layer != SUPPORTED_LAYER:
        raise NormalizationError(f"only the {SUPPORTED_LAYER!r} layer is implemented")
    if plan.get("schema_version") != SUPPORTED_PLAN_SCHEMA_VERSION:
        raise NormalizationError(
            f"unsupported plan schema version: {plan.get('schema_version')!r}"
        )
    if plan.get("plan_revision") != SUPPORTED_PLAN_REVISION:
        raise NormalizationError(f"unsupported plan revision: {plan.get('plan_revision')!r}")
    corpus = plan.get("corpus")
    if not isinstance(corpus, dict) or corpus.get("project_id") != project_id:
        raise NormalizationError("normalization plan project identity does not match --novel")
    safety = plan.get("safety")
    if not isinstance(safety, dict):
        raise NormalizationError("plan is missing its safety contract")
    if safety.get("source_identity_required") is not True:
        raise NormalizationError("plan does not require source identity evidence")
    if safety.get("global_string_replacement_allowed") is not False:
        raise NormalizationError("plan does not forbid global string replacement")
    policy = plan.get("application_policy")
    if not isinstance(policy, dict):
        raise NormalizationError("plan is missing its application policy")
    required_policy = {
        "mode": "fail_closed_exact_occurrence_only",
        "paragraph_number_alone_is_sufficient": False,
        "global_replacement_authorized": False,
        "abort_on_any_precondition_mismatch": True,
        "review_required_is_executable": False,
    }
    for key, expected in required_policy.items():
        if policy.get(key) != expected:
            raise NormalizationError(f"plan application policy violates required {key!r} value")


def _validate_inventory(
    plan: dict[str, Any],
    inventory_path: Path | None,
) -> tuple[str | None, bool]:
    expected = plan.get("corpus", {}).get("inventory_sha256")
    if expected is None:
        return None, False
    expected = _require_sha256(expected, "plan inventory SHA-256")
    if inventory_path is None:
        raise NormalizationError("plan requires its source inventory to be supplied for hash verification")
    if not inventory_path.is_file():
        raise NormalizationError(f"normalization inventory does not exist: {inventory_path}")
    actual = sha256_bytes(inventory_path.read_bytes())
    if actual != expected:
        raise NormalizationError(
            f"normalization inventory SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual, True


def _file_contexts(
    baseline_dir: Path,
    fingerprint: dict[str, Any],
) -> dict[int, tuple[str, list[tuple[str, int, int]], str]]:
    contexts = {}
    for record in fingerprint["files"]:
        unit = record["unit"]
        if unit is None:
            raise NormalizationError(f"non-numeric chapter filename is not supported: {record['name']}")
        raw = (baseline_dir / record["name"]).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NormalizationError(f"baseline unit {unit} is not valid UTF-8") from exc
        contexts[unit] = (text, paragraph_spans(text), record["sha256"])
    return contexts


def _is_title_paragraph(paragraph: str, paragraph_number: int) -> bool:
    if paragraph_number != 1:
        return False
    first_line = paragraph.splitlines()[0] if paragraph.splitlines() else paragraph
    return bool(re.match(r"^\s*(?:#{1,6}\s+|Chapter\s+\d+\b|<h[1-6]\b)", first_line, re.IGNORECASE))


def _resolve_anchor(
    *,
    anchor: dict[str, Any],
    decision: dict[str, Any],
    kind: str,
    project_id: str,
    contexts: dict[int, tuple[str, list[tuple[str, int, int]], str]],
    occurrence_weight: int,
) -> ResolvedEdit:
    required = {
        "expected_final_unit",
        "expected_baseline_file_sha256",
        "target_paragraph",
        "expected_target_paragraph",
        "expected_target_paragraph_sha256",
        "exact_old_text",
        "exact_proposed_replacement",
    }
    missing = sorted(required - set(anchor))
    if missing:
        raise NormalizationError(
            f"{decision['decision_id']} anchor lacks fail-closed fields: {missing}"
        )
    unit = _require_positive_int(
        anchor["expected_final_unit"], f"{decision['decision_id']} expected final unit"
    )
    if anchor.get("unit", unit) != unit:
        raise NormalizationError(f"{decision['decision_id']} unit identity is inconsistent")
    if unit not in contexts:
        raise NormalizationError(f"{decision['decision_id']} targets unknown baseline unit {unit}")
    text, paragraphs, file_sha256 = contexts[unit]
    expected_file_sha256 = _require_sha256(
        anchor["expected_baseline_file_sha256"],
        f"{decision['decision_id']} baseline file SHA-256",
    )
    if file_sha256 != expected_file_sha256:
        raise NormalizationError(f"{decision['decision_id']} baseline file SHA-256 mismatch")
    paragraph_number = _require_positive_int(
        anchor["target_paragraph"], f"{decision['decision_id']} target paragraph"
    )
    if paragraph_number > len(paragraphs):
        raise NormalizationError(f"{decision['decision_id']} target paragraph is out of range")
    paragraph, paragraph_start, _ = paragraphs[paragraph_number - 1]
    expected_paragraph = anchor["expected_target_paragraph"]
    if not isinstance(expected_paragraph, str) or paragraph != expected_paragraph:
        raise NormalizationError(f"{decision['decision_id']} exact target paragraph mismatch")
    expected_paragraph_sha256 = _require_sha256(
        anchor["expected_target_paragraph_sha256"],
        f"{decision['decision_id']} paragraph SHA-256",
    )
    if sha256_bytes(paragraph.encode("utf-8")) != expected_paragraph_sha256:
        raise NormalizationError(f"{decision['decision_id']} target paragraph SHA-256 mismatch")
    old_text = anchor["exact_old_text"]
    replacement = anchor["exact_proposed_replacement"]
    if not isinstance(old_text, str) or not old_text:
        raise NormalizationError(f"{decision['decision_id']} exact old text must be non-empty")
    if not isinstance(replacement, str) or not replacement:
        raise NormalizationError(f"{decision['decision_id']} replacement must be non-empty")
    if old_text == replacement:
        raise NormalizationError(f"{decision['decision_id']} attempts a no-op edit")
    matches = exact_occurrences(paragraph, old_text)
    if not matches:
        raise NormalizationError(f"{decision['decision_id']} old text is missing from its paragraph")
    start = anchor.get("target_start")
    end = anchor.get("target_end")
    target_occurrence = anchor.get("target_occurrence")
    if start is None or end is None:
        if len(matches) != 1:
            raise NormalizationError(
                f"{decision['decision_id']} unoffset anchor does not resolve uniquely"
            )
        start, end = matches[0]
        target_occurrence = 1
    else:
        start = _require_nonnegative_int(start, f"{decision['decision_id']} target start")
        end = _require_positive_int(end, f"{decision['decision_id']} target end")
        target_occurrence = _require_positive_int(
            target_occurrence, f"{decision['decision_id']} target occurrence"
        )
        if target_occurrence > len(matches) or matches[target_occurrence - 1] != (start, end):
            raise NormalizationError(
                f"{decision['decision_id']} target occurrence number/offset mismatch"
            )
    if end > len(paragraph) or paragraph[start:end] != old_text:
        raise NormalizationError(f"{decision['decision_id']} exact old span mismatch")
    expected_paragraph_count = anchor.get("expected_paragraph_occurrence_count")
    if expected_paragraph_count is not None and expected_paragraph_count != len(matches):
        raise NormalizationError(
            f"{decision['decision_id']} paragraph occurrence count mismatch"
        )
    if _is_title_paragraph(paragraph, paragraph_number):
        raise NormalizationError(
            f"{decision['decision_id']} prose edit targets a chapter-title paragraph"
        )
    identity_anchor = dict(anchor)
    identity_anchor["target_occurrence"] = target_occurrence
    identity_anchor["target_start"] = start
    identity_anchor["target_end"] = end
    computed_occurrence_id = physical_occurrence_id(project_id, identity_anchor)
    recorded_occurrence_id = anchor.get("occurrence_id")
    if recorded_occurrence_id is not None and recorded_occurrence_id != computed_occurrence_id:
        raise NormalizationError(f"{decision['decision_id']} physical occurrence ID mismatch")
    return ResolvedEdit(
        owner_id=decision["decision_id"],
        occurrence_id=computed_occurrence_id,
        kind=kind,
        unit=unit,
        filename=f"{unit:04d}.md",
        target_paragraph=paragraph_number,
        paragraph_start=paragraph_start,
        start=start,
        end=end,
        old_text=old_text,
        replacement=replacement,
        occurrence_weight=occurrence_weight,
    )


def _validate_source_evidence(decision: dict[str, Any]) -> None:
    identity = decision.get("source_identity")
    if not isinstance(identity, str) or not identity.strip():
        raise NormalizationError(f"{decision['decision_id']} lacks source identity evidence")
    for anchor in decision.get("occurrence_anchors", []):
        evidence = anchor.get("source_evidence")
        forms = anchor.get("confirmed_source_forms")
        if not isinstance(evidence, str) or not evidence.strip():
            raise NormalizationError(f"{decision['decision_id']} anchor lacks source evidence class")
        if not isinstance(forms, list) or not forms or not all(
            isinstance(form, str) and form for form in forms
        ):
            raise NormalizationError(f"{decision['decision_id']} anchor lacks confirmed source forms")
        if evidence == "human_reviewed_exact_paragraph_pair":
            if not isinstance(anchor.get("expected_source_paragraph"), str):
                raise NormalizationError(
                    f"{decision['decision_id']} human-reviewed anchor lacks exact source paragraph"
                )


def _validate_file_occurrence_fields(
    decision: dict[str, Any],
    contexts: dict[int, tuple[str, list[tuple[str, int, int]], str]],
) -> None:
    anchors = decision["occurrence_anchors"]
    by_unit: dict[int, list[dict[str, Any]]] = {}
    for anchor in anchors:
        by_unit.setdefault(anchor["expected_final_unit"], []).append(anchor)
    for unit, unit_anchors in by_unit.items():
        _, paragraphs, _ = contexts[unit]
        old_text = decision.get("current_rendering")
        if not isinstance(old_text, str) or not old_text:
            raise NormalizationError(f"{decision['decision_id']} lacks current rendering")
        file_matches = [
            (paragraph_number, start, end)
            for paragraph_number, (paragraph, _, _) in enumerate(paragraphs, 1)
            for start, end in exact_occurrences(paragraph, old_text)
        ]
        for anchor in unit_anchors:
            if anchor["exact_old_text"] != old_text:
                raise NormalizationError(
                    f"{decision['decision_id']} anchor old text differs from current rendering"
                )
            expected_count = anchor.get("expected_file_occurrence_count")
            if expected_count is not None:
                if expected_count != len(file_matches):
                    raise NormalizationError(
                        f"{decision['decision_id']} file occurrence count mismatch"
                    )
                ordinal = _require_positive_int(
                    anchor.get("target_occurrence_number_in_file"),
                    f"{decision['decision_id']} file occurrence number",
                )
                expected_span = (
                    anchor["target_paragraph"],
                    anchor["target_start"],
                    anchor["target_end"],
                )
                if ordinal > len(file_matches) or file_matches[ordinal - 1] != expected_span:
                    raise NormalizationError(
                        f"{decision['decision_id']} file occurrence ordinal mismatch"
                    )
        scoped = [anchor for anchor in unit_anchors if "expected_review_scope_occurrence_count" in anchor]
        if scoped:
            expected_scope = {anchor["expected_review_scope_occurrence_count"] for anchor in scoped}
            ordinals = sorted(anchor.get("target_occurrence_number_in_review_scope") for anchor in scoped)
            if expected_scope != {len(scoped)} or ordinals != list(range(1, len(scoped) + 1)):
                raise NormalizationError(
                    f"{decision['decision_id']} reviewed-scope occurrence accounting mismatch"
                )


def _reject_nonapproved_execution_flags(decision: dict[str, Any]) -> None:
    if decision.get("status") == APPROVED_STATUS:
        return
    if decision.get("executable_edit") is True or decision.get("replacement_operation"):
        raise NormalizationError(
            f"{decision['decision_id']} attempts to execute non-approved status {decision.get('status')}"
        )
    for anchor in decision.get("occurrence_anchors", []):
        if anchor.get("executable_edit") is True or anchor.get("replacement_operation"):
            raise NormalizationError(
                f"{decision['decision_id']} anchor attempts to execute a non-approved decision"
            )


def _validate_text_occurrence_ownership(plan: dict[str, Any], project_id: str) -> None:
    owners: dict[str, str] = {}
    spans: dict[tuple[Any, ...], list[tuple[int, int, str]]] = {}
    for decision in plan.get("text_decisions", []):
        if decision.get("status") not in {APPROVED_STATUS, REVIEW_STATUS}:
            continue
        owner = decision["decision_id"]
        for anchor in decision.get("occurrence_anchors", []):
            required = {
                "expected_final_unit",
                "expected_baseline_file_sha256",
                "target_paragraph",
                "expected_target_paragraph_sha256",
                "exact_old_text",
                "target_occurrence",
            }
            if not required <= set(anchor):
                raise NormalizationError(f"{owner} has an incomplete physical occurrence identity")
            computed = physical_occurrence_id(project_id, anchor)
            recorded = anchor.get("occurrence_id")
            if recorded != computed:
                raise NormalizationError(f"{owner} has an invalid physical occurrence ID")
            if computed in owners:
                raise NormalizationError(
                    f"duplicate physical occurrence ownership: {computed} ({owners[computed]}, {owner})"
                )
            owners[computed] = owner
            start, end = anchor.get("target_start"), anchor.get("target_end")
            if start is None or end is None:
                continue
            scope = (
                anchor["expected_final_unit"],
                anchor["expected_baseline_file_sha256"],
                anchor["target_paragraph"],
                anchor["expected_target_paragraph_sha256"],
            )
            for old_start, old_end, old_owner in spans.get(scope, []):
                if start < old_end and old_start < end:
                    raise NormalizationError(
                        f"overlapping physical target spans: {old_owner} and {owner}"
                    )
            spans.setdefault(scope, []).append((start, end, owner))


def _validate_executable_edit_set(edits: list[ResolvedEdit]) -> None:
    occurrence_ids: set[str] = set()
    spans: dict[tuple[int, int], list[ResolvedEdit]] = {}
    for edit in edits:
        if edit.occurrence_id in occurrence_ids:
            raise NormalizationError(f"duplicate execution of physical occurrence {edit.occurrence_id}")
        occurrence_ids.add(edit.occurrence_id)
        scope = (edit.unit, edit.target_paragraph)
        for other in spans.get(scope, []):
            if edit.start < other.end and other.start < edit.end:
                raise NormalizationError(
                    f"overlapping executable spans: {other.owner_id} and {edit.owner_id}"
                )
        spans.setdefault(scope, []).append(edit)


def _validate_summary_counts(
    plan: dict[str, Any],
    approved_records: int,
    automatic_occurrences: int,
    manual_occurrences: int,
    review_records: int,
    no_change_records: int,
) -> None:
    summary = plan.get("summary_counts")
    if summary is None:
        return
    if not isinstance(summary, dict):
        raise NormalizationError("plan summary_counts must be an object")
    expected = {
        "APPROVED_CORRECTION": approved_records,
        "APPROVED_CORRECTION_OCCURRENCES": automatic_occurrences,
        "PRONOUN_APPROVED_CORRECTION_OCCURRENCES": manual_occurrences,
        "TOTAL_APPROVED_PROSE_OCCURRENCES": automatic_occurrences + manual_occurrences,
        "REVIEW_REQUIRED": review_records,
        "FALSE_POSITIVE_NO_CHANGE": no_change_records,
    }
    for key, observed in expected.items():
        if key in summary and summary[key] != observed:
            raise NormalizationError(
                f"plan summary count {key} mismatch: recorded {summary[key]}, resolved {observed}"
            )


def _check_expectations(preview: ApplicationPreview, expectations: Expectations) -> None:
    checks = (
        ("approved record", preview.approved_record_count, expectations.approved_records),
        ("automatic occurrence", preview.automatic_occurrence_count, expectations.automatic_occurrences),
        ("manual-approved occurrence", preview.manual_occurrence_count, expectations.manual_occurrences),
        ("total occurrence", preview.total_occurrence_count, expectations.total_occurrences),
    )
    for label, observed, expected in checks:
        if expected is not None and observed != expected:
            raise NormalizationError(
                f"{label} invariant failed: expected {expected}, got {observed}"
            )


def prepare_application(
    *,
    baseline_dir: Path,
    plan: dict[str, Any],
    plan_sha256: str,
    project_id: str,
    layer: str = SUPPORTED_LAYER,
    allow_manual_approved: bool = False,
    expectations: Expectations | None = None,
    plan_path: Path | None = None,
    inventory_path: Path | None = None,
) -> ApplicationPreview:
    """Resolve and validate the complete edit set without writing output."""
    expectations = expectations or Expectations()
    _require_sha256(plan_sha256, "source plan SHA-256")
    _validate_plan_contract(plan, project_id, layer)
    inventory_sha256, inventory_verified = _validate_inventory(plan, inventory_path)
    baseline = _verify_baseline(baseline_dir, plan, expectations)
    contexts = _file_contexts(baseline_dir, baseline)
    text_decisions = plan.get("text_decisions")
    if not isinstance(text_decisions, list):
        raise NormalizationError("plan text_decisions must be a list")
    decision_ids: set[str] = set()
    approved_records = 0
    automatic_occurrences = 0
    review_records = 0
    review_occurrences = 0
    no_change_records = 0
    no_change_occurrences = 0
    edits: list[ResolvedEdit] = []
    for index, decision in enumerate(text_decisions, 1):
        if not isinstance(decision, dict):
            raise NormalizationError(f"text decision {index} must be an object")
        decision_id = decision.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise NormalizationError(f"text decision {index} lacks a decision ID")
        if decision_id in decision_ids:
            raise NormalizationError(f"duplicate text decision ID: {decision_id}")
        decision_ids.add(decision_id)
        status = decision.get("status")
        if status not in ALLOWED_TEXT_STATUSES:
            raise NormalizationError(f"{decision_id} has unsupported status {status!r}")
        _reject_nonapproved_execution_flags(decision)
        anchors = decision.get("occurrence_anchors", [])
        if not isinstance(anchors, list):
            raise NormalizationError(f"{decision_id} occurrence_anchors must be a list")
        declared_count = _require_nonnegative_int(
            decision.get("exact_occurrence_count", 0), f"{decision_id} occurrence count"
        )
        if status == REVIEW_STATUS:
            review_records += 1
            review_occurrences += len(anchors)
            continue
        if status == NO_CHANGE_STATUS:
            no_change_records += 1
            no_change_occurrences += declared_count
            continue
        approved_records += 1
        if decision.get("replacement_safe_automatically") is not True:
            raise NormalizationError(
                f"{decision_id} is in the automatic decision set but is not automatic-safe"
            )
        if not anchors or declared_count != len(anchors):
            raise NormalizationError(f"{decision_id} approved occurrence count is incomplete")
        proposed = decision.get("proposed_rendering")
        if not isinstance(proposed, str) or not proposed:
            raise NormalizationError(f"{decision_id} lacks an approved proposed rendering")
        _validate_source_evidence(decision)
        _validate_file_occurrence_fields(decision, contexts)
        for anchor in anchors:
            if anchor.get("exact_proposed_replacement") != proposed:
                raise NormalizationError(f"{decision_id} anchor replacement differs from its decision")
            edits.append(_resolve_anchor(
                anchor=anchor,
                decision=decision,
                kind="automatic_approved",
                project_id=project_id,
                contexts=contexts,
                occurrence_weight=1,
            ))
        automatic_occurrences += declared_count
    _validate_text_occurrence_ownership(plan, project_id)

    pronoun_decisions = plan.get("pronoun_decisions", [])
    if not isinstance(pronoun_decisions, list):
        raise NormalizationError("plan pronoun_decisions must be a list")
    manual_records = 0
    manual_occurrences = 0
    manual_ids: set[str] = set()
    for index, original in enumerate(pronoun_decisions, 1):
        if not isinstance(original, dict):
            raise NormalizationError(f"manual prose decision {index} must be an object")
        decision = dict(original)
        decision_id = decision.get("pronoun_id") or decision.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise NormalizationError(f"manual prose decision {index} lacks an ID")
        decision["decision_id"] = decision_id
        if decision_id in decision_ids or decision_id in manual_ids:
            raise NormalizationError(f"duplicate prose decision ID: {decision_id}")
        manual_ids.add(decision_id)
        status = decision.get("decision") or decision.get("status")
        if status != APPROVED_STATUS:
            _reject_nonapproved_execution_flags({**decision, "status": status})
            continue
        if decision.get("automatic_safe") is not False:
            raise NormalizationError(
                f"{decision_id} manual-approved record must explicitly set automatic_safe:false"
            )
        if not isinstance(decision.get("source_identity"), str) or not decision["source_identity"].strip():
            raise NormalizationError(f"{decision_id} lacks source identity evidence")
        if not isinstance(decision.get("source_evidence"), str) or not decision["source_evidence"].strip():
            raise NormalizationError(f"{decision_id} lacks reviewed source evidence")
        manual_records += 1
        anchors = decision.get("occurrence_anchors")
        if not isinstance(anchors, list) or not anchors:
            raise NormalizationError(f"{decision_id} has no exact manual-approved anchors")
        declared_count = _require_positive_int(
            decision.get("exact_occurrence_count"), f"{decision_id} occurrence count"
        )
        token_count = _require_positive_int(
            decision.get("pronoun_token_change_count", declared_count),
            f"{decision_id} token change count",
        )
        if token_count != declared_count:
            raise NormalizationError(f"{decision_id} manual occurrence totals disagree")
        anchor_weight_total = 0
        for anchor in anchors:
            weight = _require_positive_int(
                anchor.get("pronoun_token_change_count", 1),
                f"{decision_id} anchor occurrence weight",
            )
            anchor_weight_total += weight
            if allow_manual_approved:
                edits.append(_resolve_anchor(
                    anchor=anchor,
                    decision=decision,
                    kind="manual_approved",
                    project_id=project_id,
                    contexts=contexts,
                    occurrence_weight=weight,
                ))
        if anchor_weight_total != declared_count:
            raise NormalizationError(f"{decision_id} manual anchor weights are incomplete")
        if allow_manual_approved:
            manual_occurrences += declared_count
    _validate_executable_edit_set(edits)
    _validate_summary_counts(
        plan,
        approved_records,
        automatic_occurrences,
        sum(
            item.get("exact_occurrence_count", 0)
            for item in pronoun_decisions
            if (item.get("decision") or item.get("status")) == APPROVED_STATUS
        ),
        review_records,
        no_change_records,
    )
    examined_units = {
        anchor.get("expected_final_unit") or anchor.get("unit")
        for decision in text_decisions
        for anchor in decision.get("occurrence_anchors", [])
    } | {
        anchor.get("expected_final_unit") or anchor.get("unit")
        for decision in pronoun_decisions
        for anchor in decision.get("occurrence_anchors", [])
    }
    examined_units.discard(None)
    preview = ApplicationPreview(
        project_id=project_id,
        layer=layer,
        baseline_dir=baseline_dir,
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        plan=plan,
        baseline_fingerprint=baseline,
        edits=edits,
        approved_record_count=approved_records,
        automatic_occurrence_count=automatic_occurrences,
        manual_record_count=manual_records,
        manual_occurrence_count=manual_occurrences,
        review_record_count=review_records,
        review_occurrence_count=review_occurrences,
        no_change_record_count=no_change_records,
        no_change_occurrence_count=no_change_occurrences,
        examined_unit_count=len(examined_units),
        inventory_sha256=inventory_sha256,
        inventory_verified=inventory_verified,
    )
    _check_expectations(preview, expectations)
    return preview


def _edits_by_unit(preview: ApplicationPreview) -> dict[int, list[ResolvedEdit]]:
    result: dict[int, list[ResolvedEdit]] = {}
    for edit in preview.edits:
        result.setdefault(edit.unit, []).append(edit)
    for edits in result.values():
        edits.sort(key=lambda edit: (edit.global_start, edit.global_end))
    return result


def _apply_edits_to_text(text: str, edits: list[ResolvedEdit]) -> str:
    result = text
    for edit in sorted(edits, key=lambda item: item.global_start, reverse=True):
        if result[edit.global_start:edit.global_end] != edit.old_text:
            raise NormalizationError(
                f"paid-plan precondition changed while staging {edit.filename}: {edit.occurrence_id}"
            )
        result = result[:edit.global_start] + edit.replacement + result[edit.global_end:]
    return result


def _markdown_structure_signature(text: str) -> dict[str, Any]:
    lines = text.splitlines(keepends=True)
    structural_lines = []
    for index, line in enumerate(lines, 1):
        value = line.rstrip("\r\n")
        if (
            re.match(r"^\s{0,3}#{1,6}\s+", value)
            or re.match(r"^\s*```", value)
            or re.match(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$", value)
            or re.match(r"^\s*Chapter\s+\d+\b", value, re.IGNORECASE)
        ):
            structural_lines.append((index, value))
    return {
        "structural_lines": structural_lines,
        "paragraph_separators": PARAGRAPH_SEPARATOR_RE.findall(text),
        "markdown_marker_counts": {
            marker: text.count(marker) for marker in ("#", "*", "_", "`", "~~")
        },
    }


def verify_normalized_corpus(
    preview: ApplicationPreview,
    normalized_dir: Path,
) -> dict[str, Any]:
    """Independently verify output with a two-cursor byte-preservation walk."""
    expected_names = [record["name"] for record in preview.baseline_fingerprint["files"]]
    normalized_fp = corpus_fingerprint(normalized_dir, expected_names)
    edits_by_unit = _edits_by_unit(preview)
    differing_units: list[int] = []
    verified_span_count = 0
    verified_occurrence_count = 0
    title_edits = 0
    markdown_structure_changes = 0
    whitespace_only_changes = 0
    for baseline_record in preview.baseline_fingerprint["files"]:
        unit = baseline_record["unit"]
        name = baseline_record["name"]
        baseline_bytes = (preview.baseline_dir / name).read_bytes()
        normalized_bytes = (normalized_dir / name).read_bytes()
        unit_edits = edits_by_unit.get(unit, [])
        if not unit_edits:
            if baseline_bytes != normalized_bytes:
                raise NormalizationError(f"independent verifier found unauthorized change in {name}")
            continue
        if baseline_bytes == normalized_bytes:
            raise NormalizationError(f"expected authorized edits were not applied in {name}")
        try:
            baseline_text = baseline_bytes.decode("utf-8")
            normalized_text = normalized_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NormalizationError(f"independent verifier found invalid UTF-8 in {name}") from exc
        baseline_cursor = 0
        normalized_cursor = 0
        for edit in unit_edits:
            unchanged = baseline_text[baseline_cursor:edit.global_start]
            if normalized_text[normalized_cursor:normalized_cursor + len(unchanged)] != unchanged:
                raise NormalizationError(
                    f"unauthorized difference before {edit.occurrence_id} in {name}"
                )
            normalized_cursor += len(unchanged)
            if baseline_text[edit.global_start:edit.global_end] != edit.old_text:
                raise NormalizationError(f"independent baseline span mismatch in {name}")
            replacement_end = normalized_cursor + len(edit.replacement)
            if normalized_text[normalized_cursor:replacement_end] != edit.replacement:
                raise NormalizationError(
                    f"authorized replacement missing for {edit.occurrence_id} in {name}"
                )
            baseline_cursor = edit.global_end
            normalized_cursor = replacement_end
            verified_span_count += 1
            verified_occurrence_count += edit.occurrence_weight
            if _is_title_paragraph(
                paragraph_spans(baseline_text)[edit.target_paragraph - 1][0],
                edit.target_paragraph,
            ):
                title_edits += edit.occurrence_weight
        if baseline_text[baseline_cursor:] != normalized_text[normalized_cursor:]:
            raise NormalizationError(f"unauthorized trailing difference in {name}")
        if _markdown_structure_signature(baseline_text) != _markdown_structure_signature(normalized_text):
            markdown_structure_changes += 1
        if baseline_text.split() == normalized_text.split():
            whitespace_only_changes += 1
        differing_units.append(unit)
    if verified_span_count != preview.edit_span_count:
        raise NormalizationError("independent verifier did not account for every physical edit span")
    if verified_occurrence_count != preview.total_occurrence_count:
        raise NormalizationError("independent verifier occurrence total does not match the plan")
    if title_edits:
        raise NormalizationError(f"independent verifier found {title_edits} title edits")
    if markdown_structure_changes:
        raise NormalizationError("independent verifier found Markdown structural changes")
    if whitespace_only_changes:
        raise NormalizationError("independent verifier found whitespace-only file changes")
    if differing_units != preview.modified_units:
        raise NormalizationError("independent verifier differing-unit set does not match the plan")
    return {
        "status": "passed",
        "method": "independent_two_cursor_exact_span_verifier_v1",
        "baseline_file_count": preview.baseline_fingerprint["file_count"],
        "normalized_file_count": normalized_fp["file_count"],
        "missing_units": [],
        "added_units": [],
        "differing_units": differing_units,
        "differing_unit_count": len(differing_units),
        "verified_physical_edit_spans": verified_span_count,
        "verified_prose_occurrences": verified_occurrence_count,
        "title_edits": 0,
        "publication_edits": 0,
        "markdown_structural_changes": 0,
        "unauthorized_whitespace_only_changes": 0,
        "unauthorized_binary_or_file_differences": 0,
        "all_differences_authorized": True,
        "normalized_fingerprint": normalized_fp,
    }


def _report_edit(edit: ResolvedEdit) -> dict[str, Any]:
    return {
        "owner_id": edit.owner_id,
        "occurrence_id": edit.occurrence_id,
        "classification": edit.kind,
        "unit": edit.unit,
        "file": edit.filename,
        "target_paragraph": edit.target_paragraph,
        "target_start": edit.start,
        "target_end": edit.end,
        "exact_old_text_sha256": sha256_bytes(edit.old_text.encode("utf-8")),
        "exact_replacement_sha256": sha256_bytes(edit.replacement.encode("utf-8")),
        "occurrence_weight": edit.occurrence_weight,
    }


def build_application_report(
    preview: ApplicationPreview,
    verification: dict[str, Any],
) -> dict[str, Any]:
    normalized_fp = verification["normalized_fingerprint"]
    return {
        "application_schema_version": APPLICATION_SCHEMA_VERSION,
        "application_kind": "fail_closed_exact_occurrence_normalization",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_id": preview.project_id,
        "layer": preview.layer,
        "source_plan": {
            "path": str(preview.plan_path) if preview.plan_path else None,
            "sha256": preview.plan_sha256,
            "schema_version": preview.plan.get("schema_version"),
            "plan_revision": preview.plan.get("plan_revision"),
        },
        "source_inventory": {
            "sha256": preview.inventory_sha256,
            "verified": preview.inventory_verified,
        },
        "baseline": {
            "path": str(preview.baseline_dir),
            "file_count": preview.baseline_fingerprint["file_count"],
            "aggregate_byte_count": preview.baseline_fingerprint["aggregate_byte_count"],
            "aggregate_sha256": preview.baseline_fingerprint["aggregate_sha256"],
        },
        "normalized": {
            "file_count": normalized_fp["file_count"],
            "aggregate_byte_count": normalized_fp["aggregate_byte_count"],
            "aggregate_sha256": normalized_fp["aggregate_sha256"],
        },
        "decisions_examined": {
            "approved_automatic_records": preview.approved_record_count,
            "approved_automatic_occurrences": preview.automatic_occurrence_count,
            "approved_manual_records": preview.manual_record_count,
            "approved_manual_occurrences": preview.manual_occurrence_count,
            "review_required_records": preview.review_record_count,
            "review_required_occurrences": preview.review_occurrence_count,
            "false_positive_no_change_records": preview.no_change_record_count,
            "false_positive_no_change_occurrences": preview.no_change_occurrence_count,
            "units_with_plan_evidence": preview.examined_unit_count,
        },
        "application_counts": {
            "automatic_approved_occurrences": preview.automatic_occurrence_count,
            "manual_approved_occurrences": preview.manual_occurrence_count,
            "total_applied_prose_occurrences": preview.total_occurrence_count,
            "physical_edit_spans": preview.edit_span_count,
            "review_required_edits_applied": 0,
            "false_positive_no_change_edits_applied": 0,
            "title_edits_applied": 0,
            "publication_edits_applied": 0,
            "units_modified": preview.modified_units,
            "unit_count_modified": len(preview.modified_units),
        },
        "edit_manifest": [_report_edit(edit) for edit in preview.edits],
        "independent_verification": {
            key: value for key, value in verification.items() if key != "normalized_fingerprint"
        },
        "safety": {
            "baseline_used_as_only_input": True,
            "normalized_output_used_as_input": False,
            "global_replacement_used": False,
            "fuzzy_matching_used": False,
            "api_calls": 0,
            "translation_performed": False,
            "baseline_modified": False,
        },
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    normalized = report["normalized"]
    counts = report["application_counts"]
    examined = report["decisions_examined"]
    verification = report["independent_verification"]
    units = ", ".join(f"{unit:04d}" for unit in counts["units_modified"])
    return (
        "# Normalization Application Report\n\n"
        f"- Project: `{report['project_id']}`\n"
        f"- Layer: `{report['layer']}`\n"
        f"- Source plan SHA-256: `{report['source_plan']['sha256']}`\n"
        f"- Baseline: {baseline['file_count']} files, {baseline['aggregate_byte_count']:,} bytes, "
        f"`{baseline['aggregate_sha256']}`\n"
        f"- Normalized: {normalized['file_count']} files, {normalized['aggregate_byte_count']:,} bytes, "
        f"`{normalized['aggregate_sha256']}`\n"
        f"- Approved automatic records: {examined['approved_automatic_records']}\n"
        f"- Automatic-approved occurrences: {counts['automatic_approved_occurrences']}\n"
        f"- Manual-approved occurrences: {counts['manual_approved_occurrences']}\n"
        f"- Total applied prose occurrences: {counts['total_applied_prose_occurrences']}\n"
        f"- Physical edit spans: {counts['physical_edit_spans']}\n"
        f"- REVIEW_REQUIRED edits applied: {counts['review_required_edits_applied']}\n"
        f"- FALSE_POSITIVE_NO_CHANGE edits applied: {counts['false_positive_no_change_edits_applied']}\n"
        f"- Title edits applied: {counts['title_edits_applied']}\n"
        f"- Publication edits applied: {counts['publication_edits_applied']}\n"
        f"- Units modified ({counts['unit_count_modified']}): {units}\n"
        f"- Independent verification: {verification['status']}\n"
        f"- All differences authorized: {str(verification['all_differences_authorized']).lower()}\n"
        "- API calls: 0\n"
        "- Translation performed: no\n"
    )


def _canonical_normalization_json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _normalization_report_semantics(report: dict[str, Any]) -> dict[str, Any]:
    timestamp = report.get("generated_at_utc")
    if not isinstance(timestamp, str) or not timestamp.endswith("+00:00"):
        raise NormalizationError(
            "interrupted normalization report has invalid generated_at_utc"
        )
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise NormalizationError(
            "interrupted normalization report has invalid generated_at_utc"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise NormalizationError(
            "interrupted normalization report generated_at_utc is not UTC"
        )
    semantic = dict(report)
    semantic.pop("generated_at_utc", None)
    return semantic


def _recover_exact_interrupted_normalization_prefix(
    preview: ApplicationPreview,
    *,
    output_dir: Path,
    report_json_path: Path,
    report_md_path: Path,
    current_stage: Path,
    expected_report: dict[str, Any],
    expected_verification: dict[str, Any],
) -> bool:
    """Remove only independently proven uncommitted normalization residue."""
    if _artifact_exists(output_dir):
        raise NormalizationError(
            f"normalized output already exists: {output_dir}"
        )

    json_candidates = []
    if _artifact_exists(report_json_path):
        json_candidates.append(report_json_path)
    json_candidates.extend(
        sorted(
            report_json_path.parent.glob(f".{report_json_path.name}.staging-*"),
            key=lambda item: item.name.casefold(),
        )
    )
    md_candidates = []
    if _artifact_exists(report_md_path):
        md_candidates.append(report_md_path)
    md_candidates.extend(
        sorted(
            report_md_path.parent.glob(f".{report_md_path.name}.staging-*"),
            key=lambda item: item.name.casefold(),
        )
    )
    expected_semantics = _normalization_report_semantics(expected_report)
    expected_md_bytes = render_markdown_report(expected_report).encode("utf-8")
    cleanup_identity = {
        "project_id": preview.project_id,
        "layer": preview.layer,
        "source_plan_sha256": preview.plan_sha256,
        "expected_report_semantics": expected_semantics,
        "expected_markdown_sha256": hashlib.sha256(expected_md_bytes).hexdigest(),
        "expected_verification": expected_verification,
    }
    stage_cleanup = inspect_stage_cleanup(
        output_dir.parent,
        namespace="normalization",
        stage_prefix=f".{output_dir.name}.staging-",
        proof_id=cleanup_proof_id("normalization", cleanup_identity),
        current_stage=current_stage,
        error_cls=NormalizationError,
    )
    stage_candidates = list(stage_cleanup.stage_candidates)

    if not (
        json_candidates
        or md_candidates
        or stage_cleanup.residue_present
    ):
        return False

    for path in json_candidates:
        if path.is_symlink() or not path.is_file():
            raise NormalizationError(
                f"ambiguous interrupted normalization JSON artifact: {path}"
            )
        raw = path.read_bytes()
        try:
            report = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NormalizationError(
                f"cannot parse interrupted normalization JSON artifact: {path}"
            ) from exc
        if not isinstance(report, dict):
            raise NormalizationError(
                f"interrupted normalization JSON artifact is not an object: {path}"
            )
        if _canonical_normalization_json_bytes(report) != raw:
            raise NormalizationError(
                f"interrupted normalization JSON artifact is not canonical: {path}"
            )
        if _normalization_report_semantics(report) != expected_semantics:
            raise NormalizationError(
                f"interrupted normalization JSON semantics do not match reconstruction: {path}"
            )

    for path in md_candidates:
        if path.is_symlink() or not path.is_file():
            raise NormalizationError(
                f"ambiguous interrupted normalization Markdown artifact: {path}"
            )
        if path.read_bytes() != expected_md_bytes:
            raise NormalizationError(
                f"interrupted normalization Markdown artifact does not match reconstruction: {path}"
            )

    proven_stages: list[Path] = []
    for stage in stage_candidates:
        if stage.is_symlink() or not stage.is_dir():
            raise NormalizationError(
                f"ambiguous interrupted normalization stage artifact: {stage}"
            )
        verification = verify_normalized_corpus(preview, stage)
        if verification != expected_verification:
            raise NormalizationError(
                f"interrupted normalization stage does not match reconstruction: {stage}"
            )
        proven_stages.append(stage)

    for path in md_candidates:
        path.unlink()
    for path in json_candidates:
        path.unlink()
    cleanup_proven_stages(
        stage_cleanup,
        proven_stages,
        error_cls=NormalizationError,
    )
    return True


def _prove_terminal_normalization_commit_after_rename_error(
    preview: ApplicationPreview,
    *,
    stage: Path,
    output_dir: Path,
    report_json_path: Path,
    report_md_path: Path,
    report_json_stage: Path,
    report_md_stage: Path,
    expected_report: dict[str, Any],
    expected_verification: dict[str, Any],
) -> bool:
    """Prove that terminal normalization publication committed despite a reported rename error."""
    if _artifact_exists(stage):
        return False
    if (
        not output_dir.exists()
        or output_dir.is_symlink()
        or not output_dir.is_dir()
    ):
        return False
    if _artifact_exists(report_json_stage) or _artifact_exists(report_md_stage):
        return False

    expected_json_bytes = _canonical_normalization_json_bytes(expected_report)
    expected_md_bytes = render_markdown_report(expected_report).encode("utf-8")
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

    try:
        installed_verification = verify_normalized_corpus(preview, output_dir)
        if installed_verification != expected_verification:
            return False
        baseline_now = _verify_baseline(
            preview.baseline_dir,
            preview.plan,
            Expectations(),
        )
        for key in ("file_count", "aggregate_byte_count", "aggregate_sha256"):
            if baseline_now.get(key) != preview.baseline_fingerprint.get(key):
                return False
    except (OSError, NormalizationError):
        return False
    return True


def publish_normalized_corpus(
    preview: ApplicationPreview,
    *,
    output_dir: Path,
    report_json_path: Path,
    report_md_path: Path,
) -> dict[str, Any]:
    """Stage, verify, and atomically publish a normalized corpus and reports."""
    if preview.manual_record_count and preview.manual_occurrence_count == 0:
        raise NormalizationError(
            "approved automatic_safe:false prose records remain unresolved; "
            "application requires explicit manual-approved authorization"
        )
    if _artifact_exists(output_dir):
        raise NormalizationError(f"normalized output already exists: {output_dir}")
    if output_dir.resolve() == preview.baseline_dir.resolve():
        raise NormalizationError("normalized output may not be the immutable baseline")
    current_baseline = _verify_baseline(preview.baseline_dir, preview.plan, Expectations())
    if current_baseline["aggregate_sha256"] != preview.baseline_fingerprint["aggregate_sha256"]:
        raise NormalizationError("baseline changed after dry-run resolution")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    report_json_stage = report_json_path.with_name(f".{report_json_path.name}.staging-{os.getpid()}")
    report_md_stage = report_md_path.with_name(f".{report_md_path.name}.staging-{os.getpid()}")
    published = False
    published_reports: list[Path] = []
    try:
        for record in preview.baseline_fingerprint["files"]:
            source = preview.baseline_dir / record["name"]
            shutil.copyfile(source, stage / record["name"])
        for unit, edits in _edits_by_unit(preview).items():
            path = stage / f"{unit:04d}.md"
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NormalizationError(f"staged unit {unit} is not valid UTF-8") from exc
            path.write_bytes(_apply_edits_to_text(text, edits).encode("utf-8"))
        verification = verify_normalized_corpus(preview, stage)
        report = build_application_report(preview, verification)
        _recover_exact_interrupted_normalization_prefix(
            preview,
            output_dir=output_dir,
            report_json_path=report_json_path,
            report_md_path=report_md_path,
            current_stage=stage,
            expected_report=report,
            expected_verification=verification,
        )
        for path in (
            output_dir,
            report_json_path,
            report_md_path,
            report_json_stage,
            report_md_stage,
        ):
            if _artifact_exists(path):
                raise NormalizationError(
                    f"refusing to overwrite normalization artifact after prefix recovery: {path}"
                )
        report_json_stage.write_bytes(_canonical_normalization_json_bytes(report))
        report_md_stage.write_bytes(render_markdown_report(report).encode("utf-8"))
        report_json_stage.replace(report_json_path)
        published_reports.append(report_json_path)
        report_md_stage.replace(report_md_path)
        published_reports.append(report_md_path)
        stage.rename(output_dir)
        published = True
        return report
    except Exception as exc:
        if _artifact_exists(output_dir):
            if _prove_terminal_normalization_commit_after_rename_error(
                preview,
                stage=stage,
                output_dir=output_dir,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                report_json_stage=report_json_stage,
                report_md_stage=report_md_stage,
                expected_report=report,
                expected_verification=verification,
            ):
                published = True
                return report
            raise NormalizationError(
                "ambiguous terminal normalization commit outcome after rename error; "
                "existing output and reports were preserved"
            ) from exc

        if not published and stage.exists():
            shutil.rmtree(stage)
        if report_md_stage.exists():
            report_md_stage.unlink()
        if report_json_stage.exists():
            report_json_stage.unlink()
        if not published:
            for path in reversed(published_reports):
                path.unlink(missing_ok=True)
        raise


def verify_existing_application(
    preview: ApplicationPreview,
    *,
    output_dir: Path,
    report_json_path: Path,
) -> dict[str, Any]:
    if not report_json_path.is_file():
        raise NormalizationError(f"application report does not exist: {report_json_path}")
    try:
        report = json.loads(report_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"cannot read application report: {exc}") from exc
    recorded_plan_sha = report.get("source_plan", {}).get("sha256")
    if recorded_plan_sha != preview.plan_sha256:
        raise NormalizationError(
            f"application report source plan SHA-256 mismatch: {recorded_plan_sha} != {preview.plan_sha256}"
        )
    verification = verify_normalized_corpus(preview, output_dir)
    recorded_normalized = report.get("normalized", {})
    actual = verification["normalized_fingerprint"]
    for key in ("file_count", "aggregate_byte_count", "aggregate_sha256"):
        if recorded_normalized.get(key) != actual[key]:
            raise NormalizationError(f"application report normalized {key} mismatch")
    return verification


def dry_run_summary(preview: ApplicationPreview) -> dict[str, Any]:
    return {
        "status": "dry_run_passed",
        "project_id": preview.project_id,
        "layer": preview.layer,
        "source_plan_sha256": preview.plan_sha256,
        "baseline_file_count": preview.baseline_fingerprint["file_count"],
        "baseline_aggregate_byte_count": preview.baseline_fingerprint["aggregate_byte_count"],
        "baseline_aggregate_sha256": preview.baseline_fingerprint["aggregate_sha256"],
        "approved_automatic_records": preview.approved_record_count,
        "automatic_approved_occurrences": preview.automatic_occurrence_count,
        "manual_approved_records": preview.manual_record_count,
        "manual_approved_occurrences": preview.manual_occurrence_count,
        "total_resolved_prose_occurrences": preview.total_occurrence_count,
        "physical_edit_spans": preview.edit_span_count,
        "review_required_edits_resolved": 0,
        "false_positive_no_change_edits_resolved": 0,
        "title_edits_resolved": 0,
        "publication_edits_resolved": 0,
        "units_to_modify": preview.modified_units,
        "writes_performed": 0,
    }


def _expectations_from_args(args: argparse.Namespace) -> Expectations:
    return Expectations(
        baseline_files=args.expect_baseline_files,
        baseline_bytes=args.expect_baseline_bytes,
        baseline_sha256=args.expect_baseline_sha256,
        approved_records=args.expect_approved_records,
        automatic_occurrences=args.expect_automatic_occurrences,
        manual_occurrences=args.expect_manual_occurrences,
        total_occurrences=args.expect_total_occurrences,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply an audited normalization plan to a separate normalized corpus."
    )
    parser.add_argument("--novel", required=True, help="Novel project ID under novels/.")
    parser.add_argument("--layer", choices=[SUPPORTED_LAYER], default=SUPPORTED_LAYER)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Validate and resolve; write nothing.")
    mode.add_argument("--apply", action="store_true", help="Transactionally publish normalized chapters.")
    mode.add_argument("--verify-only", action="store_true", help="Independently verify an existing application.")
    parser.add_argument(
        "--allow-manual-approved",
        action="store_true",
        help="Include exact APPROVED_CORRECTION records marked automatic_safe:false.",
    )
    parser.add_argument("--plan", type=Path, help="Plan path; defaults to the selected novel's final report.")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expect-baseline-files", type=int)
    parser.add_argument("--expect-baseline-bytes", type=int)
    parser.add_argument("--expect-baseline-sha256")
    parser.add_argument("--expect-approved-records", type=int)
    parser.add_argument("--expect-automatic-occurrences", type=int)
    parser.add_argument("--expect-manual-occurrences", type=int)
    parser.add_argument("--expect-total-occurrences", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        project = select_project(args.novel, Path(__file__).resolve().parent)
        final_root = project.root / "final"
        baseline_dir = final_root / "chapters"
        output_dir = final_root / "normalized_chapters"
        report_dir = final_root / "reports"
        plan_path = (args.plan or report_dir / "normalization_plan.json").resolve()
        inventory_path = plan_path.with_name("normalization_inventory.json")
        report_json = report_dir / "normalization_application.json"
        report_md = report_dir / "normalization_application.md"
        lock = production_run_lock(project.root) if args.apply else nullcontext()
        with lock:
            plan, plan_sha256 = load_plan_file(plan_path, args.expected_plan_sha256)
            preview = prepare_application(
                baseline_dir=baseline_dir,
                plan=plan,
                plan_sha256=plan_sha256,
                project_id=args.novel,
                layer=args.layer,
                allow_manual_approved=args.allow_manual_approved,
                expectations=_expectations_from_args(args),
                plan_path=plan_path,
                inventory_path=inventory_path,
            )
            if args.dry_run:
                result = dry_run_summary(preview)
            elif args.verify_only:
                verification = verify_existing_application(
                    preview,
                    output_dir=output_dir,
                    report_json_path=report_json,
                )
                result = {
                    "status": "verification_passed",
                    "normalized_aggregate_sha256": verification["normalized_fingerprint"]["aggregate_sha256"],
                    **{key: value for key, value in verification.items() if key != "normalized_fingerprint"},
                }
            else:
                report = publish_normalized_corpus(
                    preview,
                    output_dir=output_dir,
                    report_json_path=report_json,
                    report_md_path=report_md,
                )
                result = {
                    "status": "application_passed",
                    "report_json": str(report_json),
                    "report_markdown": str(report_md),
                    "normalized_corpus": str(output_dir),
                    **report["application_counts"],
                    "normalized_aggregate_sha256": report["normalized"]["aggregate_sha256"],
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (NormalizationError, ProductionRunLockedError) as exc:
        print(f"NORMALIZATION ABORTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
