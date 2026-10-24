"""Deterministic source-backed final editorial planning and application.

This module is local-only.  It never imports translation clients and never
modifies the publication corpus that supplies its immutable input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from build_publication_chapters import framed_corpus_fingerprint
from novel_project import NovelProject
from proven_stage_cleanup import (
    cleanup_proof_id,
    cleanup_proven_stages,
    inspect_stage_cleanup,
)


PLAN_SCHEMA_VERSION = 1
PLAN_REVISION = "final-editorial-plan-v1.2"
APPLY_SCHEMA_VERSION = 1
APPLY_REVISION = "final-editorial-apply-v1.2"
AUTHORIZATION_SCHEMA_VERSION = 1
FINGERPRINT_ALGORITHM = "sha256-filename-length-bytes-v1"
NUMBERED_NAME_RE = re.compile(r"^(\d{4})\.md$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FinalEditorialError(ValueError):
    """A fail-closed editorial authorization or integrity error."""


@dataclass(frozen=True)
class EditorialPaths:
    publication_dir: Path
    cleanup_report: Path
    authorization: Path
    korean_source: Path
    output_dir: Path
    plan_json: Path
    plan_md: Path
    apply_json: Path
    apply_md: Path


def editorial_paths(project: NovelProject) -> EditorialPaths:
    final_dir = getattr(project, "publication_final_dir", project.root / "final")
    reports = final_dir / "reports"
    return EditorialPaths(
        publication_dir=final_dir / "publication_chapters",
        cleanup_report=reports / "publication_cleanup.json",
        authorization=project.root / "config" / "final_editorial.json",
        korean_source=project.root / "source" / "novel.txt",
        output_dir=final_dir / "editorial_chapters",
        plan_json=reports / "final_editorial_plan.json",
        plan_md=reports / "final_editorial_plan.md",
        apply_json=reports / "final_editorial_apply.json",
        apply_md=reports / "final_editorial_apply.md",
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FinalEditorialError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalEditorialError(f"malformed {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalEditorialError(f"{label} must contain a JSON object")
    return value


def _require(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise FinalEditorialError(
            f"{label} mismatch: expected {expected!r}, found {actual!r}"
        )


def _validate_fingerprint_shape(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FinalEditorialError(f"{label} must be an object")
    _require(value.get("algorithm"), FINGERPRINT_ALGORITHM, f"{label} algorithm")
    if not isinstance(value.get("file_count"), int) or isinstance(
        value.get("file_count"), bool
    ):
        raise FinalEditorialError(f"{label} file_count is invalid")
    if not isinstance(value.get("aggregate_byte_count"), int) or isinstance(
        value.get("aggregate_byte_count"), bool
    ):
        raise FinalEditorialError(f"{label} aggregate_byte_count is invalid")
    if not isinstance(value.get("aggregate_sha256"), str) or not SHA256_RE.fullmatch(
        value["aggregate_sha256"]
    ):
        raise FinalEditorialError(f"{label} aggregate_sha256 is invalid")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != value["file_count"]:
        raise FinalEditorialError(f"{label} files inventory is invalid")
    return value


def _inventory_payloads(directory: Path, expected_count: int) -> list[tuple[str, bytes]]:
    if not directory.is_dir():
        raise FinalEditorialError(f"corpus directory does not exist: {directory}")
    entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    expected_names = [f"{unit:04d}.md" for unit in range(1, expected_count + 1)]
    invalid = [
        item.name
        for item in entries
        if not item.is_file() or item.is_symlink() or not NUMBERED_NAME_RE.fullmatch(item.name)
    ]
    if invalid:
        raise FinalEditorialError(f"unexpected corpus entries: {invalid}")
    actual_names = [item.name for item in entries]
    if actual_names != expected_names:
        missing = sorted(set(expected_names) - set(actual_names))
        extra = sorted(set(actual_names) - set(expected_names))
        raise FinalEditorialError(
            f"corpus inventory mismatch: missing={missing}, unexpected={extra}"
        )
    return [(name, (directory / name).read_bytes()) for name in expected_names]


def _expected_checkpoint(manifest: dict[str, Any]) -> dict[str, Any]:
    checkpoint = manifest.get("immutable_publication_checkpoint")
    if not isinstance(checkpoint, dict):
        raise FinalEditorialError("authorization lacks immutable_publication_checkpoint")
    required = ("file_count", "aggregate_byte_count", "aggregate_sha256")
    if any(key not in checkpoint for key in required):
        raise FinalEditorialError("authorization publication checkpoint is incomplete")
    if not isinstance(checkpoint["file_count"], int) or checkpoint["file_count"] < 1:
        raise FinalEditorialError("authorization publication file_count is invalid")
    if not isinstance(checkpoint["aggregate_byte_count"], int):
        raise FinalEditorialError("authorization publication byte count is invalid")
    if not isinstance(checkpoint["aggregate_sha256"], str) or not SHA256_RE.fullmatch(
        checkpoint["aggregate_sha256"]
    ):
        raise FinalEditorialError("authorization publication SHA-256 is invalid")
    return checkpoint


def load_authorization(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes() if path.is_file() else b""
    manifest = _load_json_object(path, "final editorial authorization manifest")
    _require(
        manifest.get("schema_version"),
        AUTHORIZATION_SCHEMA_VERSION,
        "authorization schema",
    )
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not revision:
        raise FinalEditorialError("authorization revision is invalid")
    return manifest, sha256_bytes(raw)


def _validate_cleanup_chain(
    report: dict[str, Any], actual: dict[str, Any], checkpoint: dict[str, Any]
) -> None:
    _require(report.get("schema_version"), 1, "publication cleanup schema")
    _require(report.get("revision"), "publication-cleanup-v1", "publication cleanup revision")
    _require(report.get("status"), "success", "publication cleanup status")
    _require(report.get("ready"), True, "publication cleanup ready")
    _require(report.get("success"), True, "publication cleanup success")
    _require(report.get("blocker_count"), 0, "publication cleanup blocker count")
    output = _validate_fingerprint_shape(
        report.get("output_corpus_fingerprint"), "publication cleanup output fingerprint"
    )
    _require(output, actual, "publication cleanup output versus actual publication corpus")
    for key in ("file_count", "aggregate_byte_count", "aggregate_sha256"):
        _require(actual.get(key), checkpoint[key], f"immutable publication {key}")


def _validate_authorized_set(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    edits = manifest.get("edits")
    if not isinstance(edits, list):
        raise FinalEditorialError("authorization edits must be an array")
    contract = manifest.get("authorized_set")
    if not isinstance(contract, dict):
        raise FinalEditorialError("authorization lacks authorized_set contract")
    edit_ids: list[str] = []
    units: list[int] = []
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise FinalEditorialError(f"authorization edit {index} is malformed")
        edit_id = edit.get("edit_id")
        unit = edit.get("unit")
        category = edit.get("edit_category")
        scope = edit.get("scope")
        if not isinstance(edit_id, str) or not edit_id:
            raise FinalEditorialError(f"authorization edit {index} has invalid edit_id")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 1:
            raise FinalEditorialError(f"authorization edit {edit_id} has invalid unit")
        if category not in {"STRUCTURAL_HEADING", "CANONICAL_TITLE", "PROSE_TYPO"}:
            raise FinalEditorialError(f"authorization edit {edit_id} has invalid category")
        if scope not in {"heading", "prose"}:
            raise FinalEditorialError(f"authorization edit {edit_id} has invalid scope")
        if not isinstance(edit.get("expected_english"), str) or not edit["expected_english"]:
            raise FinalEditorialError(f"authorization edit {edit_id} lacks expected English")
        if not isinstance(edit.get("replacement"), str) or not edit["replacement"]:
            raise FinalEditorialError(f"authorization edit {edit_id} lacks replacement")
        evidence = edit.get("source_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise FinalEditorialError(f"authorization edit {edit_id} lacks source evidence")
        edit_ids.append(edit_id)
        units.append(unit)
    if len(edit_ids) != len(set(edit_ids)):
        raise FinalEditorialError("authorization edit IDs are not unique")
    if len(units) != len(set(units)):
        raise FinalEditorialError("authorization permits more than one operation per unit")
    affected = sorted(units)
    heading_count = sum(edit["scope"] == "heading" for edit in edits)
    prose_count = sum(edit["scope"] == "prose" for edit in edits)
    _require(len(edits), contract.get("operation_count"), "authorized operation count")
    _require(heading_count, contract.get("heading_operation_count"), "heading operation count")
    _require(prose_count, contract.get("prose_operation_count"), "prose operation count")
    _require(affected, contract.get("affected_units"), "authorized affected-unit set")
    return edits


def _validate_source_evidence(
    source_bytes: bytes,
    manifest: dict[str, Any],
    edits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_contract = manifest.get("korean_source")
    if not isinstance(source_contract, dict):
        raise FinalEditorialError("authorization lacks Korean source contract")
    _require(len(source_bytes), source_contract.get("byte_count"), "Korean source byte count")
    _require(sha256_bytes(source_bytes), source_contract.get("sha256"), "Korean source SHA-256")
    try:
        lines = source_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise FinalEditorialError(f"Korean source is not valid UTF-8: {exc}") from exc

    validated: list[dict[str, Any]] = []

    preservation = source_contract.get("preservation_anchors", [])
    if not isinstance(preservation, list):
        raise FinalEditorialError("Korean source preservation anchors must be an array")
    for anchor in preservation:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("anchor_id"), str):
            raise FinalEditorialError("malformed Korean source preservation anchor")
        text = anchor.get("anchor_text")
        line_number = anchor.get("line_number_hint")
        if not isinstance(text, str) or not isinstance(line_number, int):
            raise FinalEditorialError(
                f"{anchor.get('anchor_id')} Korean source preservation anchor is incomplete"
            )
        _require(
            sha256_bytes(text.encode("utf-8")),
            anchor.get("anchor_sha256"),
            f"{anchor['anchor_id']} source-anchor SHA-256",
        )
        if line_number < 1 or line_number > len(lines) or lines[line_number - 1] != text:
            raise FinalEditorialError(
                f"{anchor['anchor_id']} Korean source preservation anchor mismatch"
            )
        validated.append(
            {
                "edit_id": None,
                "preservation_anchor_id": anchor["anchor_id"],
                "evidence": [{**anchor, "validated": True}],
            }
        )
    for edit in edits:
        evidence_records: list[dict[str, Any]] = []
        for evidence in edit["source_evidence"]:
            if not isinstance(evidence, dict):
                raise FinalEditorialError(f"{edit['edit_id']} has malformed source evidence")
            text = evidence.get("anchor_text")
            digest = evidence.get("anchor_sha256")
            locator = evidence.get("source_locator")
            if not isinstance(text, str) or not text:
                raise FinalEditorialError(f"{edit['edit_id']} has empty source anchor")
            if sha256_bytes(text.encode("utf-8")) != digest:
                raise FinalEditorialError(f"{edit['edit_id']} source-anchor SHA-256 mismatch")
            if not isinstance(locator, dict):
                raise FinalEditorialError(f"{edit['edit_id']} lacks a source locator")
            line_number = locator.get("line_number_hint")
            if not isinstance(line_number, int) or line_number < 1 or line_number > len(lines):
                raise FinalEditorialError(f"{edit['edit_id']} source locator is invalid")
            if lines[line_number - 1] != text:
                raise FinalEditorialError(
                    f"{edit['edit_id']} Korean source anchor mismatch at line {line_number}"
                )
            occurrences = [index + 1 for index, line in enumerate(lines) if line == text]
            expected_occurrences = locator.get("exact_line_occurrence_count", 1)
            _require(
                len(occurrences),
                expected_occurrences,
                f"{edit['edit_id']} source anchor occurrence count",
            )
            occurrence_index = locator.get("occurrence_index", 1)
            if occurrences[occurrence_index - 1] != line_number:
                raise FinalEditorialError(
                    f"{edit['edit_id']} source anchor occurrence index mismatch"
                )
            evidence_records.append({**evidence, "validated": True})
        validated.append({"edit_id": edit["edit_id"], "evidence": evidence_records})
    return validated


def _heading_end(data: bytes) -> int:
    candidates = [index for marker in (b"\r\n\r\n", b"\n\n", b"\r\r") if (index := data.find(marker)) >= 0]
    if not candidates:
        raise FinalEditorialError("chapter lacks a heading/body separator")
    return min(candidates)


def apply_edit_to_bytes(data: bytes, edit: dict[str, Any]) -> bytes:
    expected = edit["expected_english"].encode("utf-8")
    replacement = edit["replacement"].encode("utf-8")
    count = data.count(expected)
    if count != 1:
        raise FinalEditorialError(
            f"{edit['edit_id']} expected English anchor must occur exactly once; found {count}"
        )
    position = data.find(expected)
    heading_end = _heading_end(data)
    if edit["scope"] == "heading" and position >= heading_end:
        raise FinalEditorialError(f"{edit['edit_id']} expected heading anchor is in body prose")
    if edit["scope"] == "prose" and position < heading_end:
        raise FinalEditorialError(f"{edit['edit_id']} expected prose anchor is in the heading")
    output = data[:position] + replacement + data[position + len(expected):]
    expected_delta = len(replacement) - len(expected)
    if len(output) - len(data) != expected_delta:
        raise FinalEditorialError(f"{edit['edit_id']} byte delta is invalid")
    return output


def _guard_heading(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FinalEditorialError(f"guarded unit is not valid UTF-8: {exc}") from exc
    return text.splitlines()[:2]


def _validate_guards(
    manifest: dict[str, Any],
    source_payloads: dict[int, bytes],
    output_payloads: dict[int, bytes],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    guards = manifest.get("preservation_guards")
    if not isinstance(guards, list) or not guards:
        raise FinalEditorialError("authorization lacks preservation guards")
    for guard in guards:
        if not isinstance(guard, dict) or not isinstance(guard.get("guard_id"), str):
            raise FinalEditorialError("malformed preservation guard")
        units = guard.get("units")
        expected = guard.get("expected_heading_lines")
        if not isinstance(units, list) or not isinstance(expected, dict):
            raise FinalEditorialError(f"{guard['guard_id']} is malformed")
        for unit in units:
            expected_lines = expected.get(str(unit))
            if _guard_heading(source_payloads[unit]) != expected_lines:
                raise FinalEditorialError(
                    f"{guard['guard_id']} source heading mismatch in unit {unit}"
                )
            if _guard_heading(output_payloads[unit]) != expected_lines:
                raise FinalEditorialError(
                    f"{guard['guard_id']} output heading changed in unit {unit}"
                )
            if source_payloads[unit] != output_payloads[unit]:
                raise FinalEditorialError(
                    f"{guard['guard_id']} guarded unit {unit} is not byte-identical"
                )
        results.append(
            {"guard_id": guard["guard_id"], "units": units, "status": "preserved"}
        )
    return results


def simulate(
    manifest: dict[str, Any], payloads: list[tuple[str, bytes]]
) -> tuple[list[tuple[str, bytes]], list[dict[str, Any]], list[dict[str, Any]]]:
    edits = _validate_authorized_set(manifest)
    by_unit = {edit["unit"]: edit for edit in edits}
    source_by_unit = {int(Path(name).stem): data for name, data in payloads}
    output_by_unit: dict[int, bytes] = {}
    operations: list[dict[str, Any]] = []
    for name, data in payloads:
        unit = int(Path(name).stem)
        edit = by_unit.get(unit)
        if edit is None:
            output_by_unit[unit] = data
            continue
        output = apply_edit_to_bytes(data, edit)
        output_by_unit[unit] = output
        operations.append(
            {
                **edit,
                "input_file_sha256": sha256_bytes(data),
                "output_file_sha256": sha256_bytes(output),
                "input_byte_count": len(data),
                "output_byte_count": len(output),
            }
        )
    affected = sorted(unit for unit in source_by_unit if source_by_unit[unit] != output_by_unit[unit])
    expected_affected = manifest["authorized_set"]["affected_units"]
    _require(affected, expected_affected, "simulated affected-unit set")
    for unit in source_by_unit:
        if unit not in set(expected_affected) and source_by_unit[unit] != output_by_unit[unit]:
            raise FinalEditorialError(f"unapproved output difference in unit {unit}")
    guards = _validate_guards(manifest, source_by_unit, output_by_unit)
    outputs = [(name, output_by_unit[int(Path(name).stem)]) for name, _ in payloads]
    return outputs, operations, guards


def _base_plan_report(project: NovelProject, authorization_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "revision": PLAN_REVISION,
        "novel": project.config.id,
        "mode": "read-only-source-backed-final-editorial-plan-zero-api",
        "authorization_fingerprint": {
            "algorithm": "sha256-file-bytes-v1",
            "sha256": authorization_sha256,
        },
        "ready": False,
        "blocker_count": 0,
        "blockers": [],
        "api_calls": 0,
    }


def build_plan_report(project: NovelProject) -> dict[str, Any]:
    paths = editorial_paths(project)
    manifest, authorization_sha256 = load_authorization(paths.authorization)
    report = _base_plan_report(project, authorization_sha256)
    checkpoint = _expected_checkpoint(manifest)
    source_before: dict[str, Any] | None = None
    try:
        edits = _validate_authorized_set(manifest)
        payloads = _inventory_payloads(paths.publication_dir, checkpoint["file_count"])
        source_before = framed_corpus_fingerprint(payloads)
        cleanup = _load_json_object(paths.cleanup_report, "publication cleanup report")
        _validate_cleanup_chain(cleanup, source_before, checkpoint)
        korean_bytes = paths.korean_source.read_bytes()
        evidence = _validate_source_evidence(korean_bytes, manifest, edits)
        outputs, operations, guards = simulate(manifest, payloads)
        output_fingerprint = framed_corpus_fingerprint(outputs)
        source_after = framed_corpus_fingerprint(
            _inventory_payloads(paths.publication_dir, checkpoint["file_count"])
        )
        _require(source_after, source_before, "publication corpus after planning")
        affected = manifest["authorized_set"]["affected_units"]
        heading_count = sum(item["scope"] == "heading" for item in operations)
        prose_count = sum(item["scope"] == "prose" for item in operations)
        report.update(
            {
                "status": "ready",
                "ready": True,
                "source_corpus_fingerprint": source_before,
                "publication_cleanup_output_corpus_fingerprint": cleanup[
                    "output_corpus_fingerprint"
                ],
                "korean_source_fingerprint": {
                    "algorithm": "sha256-file-bytes-v1",
                    "byte_count": len(korean_bytes),
                    "sha256": sha256_bytes(korean_bytes),
                },
                "source_evidence_status": "validated",
                "source_evidence": evidence,
                "operation_count": len(operations),
                "heading_operation_count": heading_count,
                "prose_operation_count": prose_count,
                "affected_unit_count": len(affected),
                "affected_units": affected,
                "unaffected_unit_count": checkpoint["file_count"] - len(affected),
                "unaffected_units_byte_identical": True,
                "operations": operations,
                "preservation_guards": guards,
                "unapproved_change_count": 0,
                "expected_output_corpus_fingerprint": output_fingerprint,
                "publication_source_unchanged": True,
            }
        )
    except (FinalEditorialError, OSError) as exc:
        report["status"] = "blocked"
        report["blockers"].append(str(exc))
        if source_before is not None:
            report["source_corpus_fingerprint"] = source_before
    report["blocker_count"] = len(report["blockers"])
    report["ready"] = report["blocker_count"] == 0 and report.get("ready") is True
    return report


def render_plan_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Final Editorial Plan",
        "",
        f"- Schema/revision: `{report['schema_version']}` / `{report['revision']}`",
        f"- Novel: `{report['novel']}`",
        f"- Status: `{report['status']}`",
        f"- Ready: `{str(report['ready']).lower()}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- API calls: `{report['api_calls']}`",
    ]
    if report.get("source_corpus_fingerprint"):
        source = report["source_corpus_fingerprint"]
        lines.extend(
            [
                f"- Source files/bytes/SHA-256: `{source['file_count']}` / "
                f"`{source['aggregate_byte_count']}` / `{source['aggregate_sha256']}`",
                f"- Operations (heading/prose): `{report.get('operation_count')}` "
                f"(`{report.get('heading_operation_count')}` / `{report.get('prose_operation_count')}`)",
                f"- Affected/unaffected units: `{report.get('affected_unit_count')}` / "
                f"`{report.get('unaffected_unit_count')}`",
            ]
        )
    if report.get("expected_output_corpus_fingerprint"):
        output = report["expected_output_corpus_fingerprint"]
        lines.append(
            f"- Expected output files/bytes/SHA-256: `{output['file_count']}` / "
            f"`{output['aggregate_byte_count']}` / `{output['aggregate_sha256']}`"
        )
    lines.extend(["", "## Authorized operations", ""])
    for item in report.get("operations", []):
        lines.append(
            f"- `{item['edit_id']}` — unit `{item['unit']:04d}` — "
            f"`{item['edit_category']}`"
        )
    if report["blockers"]:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {blocker}" for blocker in report["blockers"])
    return "\n".join(lines) + "\n"


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise FinalEditorialError(f"refusing to overwrite existing report: {path}") from exc


def write_plan_reports(project: NovelProject, report: dict[str, Any]) -> None:
    paths = editorial_paths(project)
    if paths.output_dir.exists():
        raise FinalEditorialError(
            f"planner refuses pre-existing editorial output: {paths.output_dir}"
        )
    if paths.apply_json.exists() or paths.apply_md.exists():
        raise FinalEditorialError("planner refuses pre-existing final editorial apply reports")
    _write_new(
        paths.plan_json,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    try:
        _write_new(paths.plan_md, render_plan_markdown(report).encode("utf-8"))
    except Exception:
        paths.plan_json.unlink(missing_ok=True)
        raise


def _validate_ready_plan(
    plan: dict[str, Any], project: NovelProject, authorization_sha256: str
) -> None:
    _require(plan.get("schema_version"), PLAN_SCHEMA_VERSION, "plan schema")
    _require(plan.get("revision"), PLAN_REVISION, "plan revision")
    _require(plan.get("novel"), project.config.id, "plan novel")
    _require(plan.get("status"), "ready", "plan status")
    _require(plan.get("ready"), True, "plan ready")
    _require(plan.get("blocker_count"), 0, "plan blocker count")
    fingerprint = plan.get("authorization_fingerprint")
    if not isinstance(fingerprint, dict):
        raise FinalEditorialError("plan lacks authorization fingerprint")
    _require(
        fingerprint.get("sha256"), authorization_sha256, "plan authorization fingerprint"
    )
    _validate_fingerprint_shape(
        plan.get("source_corpus_fingerprint"), "plan source corpus fingerprint"
    )
    _validate_fingerprint_shape(
        plan.get("expected_output_corpus_fingerprint"),
        "plan expected output corpus fingerprint",
    )


def _build_apply_report(
    project: NovelProject,
    plan: dict[str, Any],
    authorization_sha256: str,
    output_fingerprint: dict[str, Any],
    guards: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "revision": APPLY_REVISION,
        "novel": project.config.id,
        "status": "success",
        "ready": True,
        "success": True,
        "blocker_count": 0,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "plan_revision": PLAN_REVISION,
        "authorization_fingerprint": {
            "algorithm": "sha256-file-bytes-v1",
            "sha256": authorization_sha256,
        },
        "korean_source_fingerprint": plan["korean_source_fingerprint"],
        "source_corpus_fingerprint": plan["source_corpus_fingerprint"],
        "output_corpus_fingerprint": output_fingerprint,
        "operation_count": plan["operation_count"],
        "heading_operation_count": plan["heading_operation_count"],
        "prose_operation_count": plan["prose_operation_count"],
        "affected_unit_count": plan["affected_unit_count"],
        "affected_units": plan["affected_units"],
        "unaffected_unit_count": plan["unaffected_unit_count"],
        "unaffected_units_byte_identical": True,
        "unapproved_change_count": 0,
        "operations": [
            {
                "edit_id": item["edit_id"],
                "unit": item["unit"],
                "edit_category": item["edit_category"],
                "scope": item["scope"],
            }
            for item in plan["operations"]
        ],
        "preservation_guards": guards,
        "publication_source_unchanged": True,
        "transactional_publish": True,
        "api_calls": 0,
    }


def render_apply_markdown(report: dict[str, Any]) -> str:
    source = report["source_corpus_fingerprint"]
    output = report["output_corpus_fingerprint"]
    lines = [
        "# Final Editorial Apply",
        "",
        f"- Schema/revision: `{report['schema_version']}` / `{report['revision']}`",
        f"- Novel: `{report['novel']}`",
        f"- Status: `{report['status']}`",
        f"- Source SHA-256: `{source['aggregate_sha256']}`",
        f"- Output files/bytes/SHA-256: `{output['file_count']}` / "
        f"`{output['aggregate_byte_count']}` / `{output['aggregate_sha256']}`",
        f"- Operations (heading/prose): `{report['operation_count']}` "
        f"(`{report['heading_operation_count']}` / `{report['prose_operation_count']}`)",
        f"- Affected/unaffected units: `{report['affected_unit_count']}` / "
        f"`{report['unaffected_unit_count']}`",
        f"- Publication source unchanged: `{str(report['publication_source_unchanged']).lower()}`",
        f"- API calls: `{report['api_calls']}`",
        "",
        "## Applied operations",
        "",
    ]
    lines.extend(
        f"- `{item['edit_id']}` — unit `{item['unit']:04d}` — `{item['edit_category']}`"
        for item in report["operations"]
    )
    return "\n".join(lines) + "\n"


def _recover_exact_interrupted_apply_prefix(
    paths: EditorialPaths,
    *,
    report_json_bytes: bytes,
    report_md_bytes: bytes,
    output_fingerprint: dict[str, Any],
    expected_count: int,
) -> bool:
    """Remove only an independently proven uncommitted apply prefix."""
    if paths.output_dir.exists():
        raise FinalEditorialError(
            f"refusing to overwrite existing final editorial output: {paths.output_dir}"
        )

    report_json_temp = paths.apply_json.with_name(f".{paths.apply_json.name}.tmp")
    report_md_temp = paths.apply_md.with_name(f".{paths.apply_md.name}.tmp")
    cleanup_identity = {
        "report_json_sha256": sha256_bytes(report_json_bytes),
        "report_md_sha256": sha256_bytes(report_md_bytes),
        "output_fingerprint": output_fingerprint,
        "expected_count": expected_count,
    }
    stage_cleanup = inspect_stage_cleanup(
        paths.output_dir.parent,
        namespace="final-editorial",
        stage_prefix=".editorial-stage-",
        proof_id=cleanup_proof_id("final-editorial", cleanup_identity),
        current_stage=None,
        error_cls=FinalEditorialError,
    )
    stage_paths = list(stage_cleanup.stage_candidates)

    final_json = paths.apply_json.exists()
    final_md = paths.apply_md.exists()
    temp_json = report_json_temp.exists()
    temp_md = report_md_temp.exists()
    residue_present = (
        final_json
        or final_md
        or temp_json
        or temp_md
        or stage_cleanup.residue_present
    )
    if not residue_present:
        return False

    if final_md and not final_json:
        raise FinalEditorialError(
            "ambiguous interrupted editorial prefix: Markdown apply report exists without JSON"
        )
    if final_json and temp_json:
        raise FinalEditorialError(
            "ambiguous interrupted editorial prefix: final and temporary JSON reports coexist"
        )
    if final_md and temp_md:
        raise FinalEditorialError(
            "ambiguous interrupted editorial prefix: final and temporary Markdown reports coexist"
        )
    if temp_md and not (final_json or temp_json):
        raise FinalEditorialError(
            "ambiguous interrupted editorial prefix: Markdown temporary report lacks JSON predecessor"
        )

    expected_reports = (
        (paths.apply_json, report_json_bytes, "JSON apply report"),
        (paths.apply_md, report_md_bytes, "Markdown apply report"),
        (report_json_temp, report_json_bytes, "temporary JSON apply report"),
        (report_md_temp, report_md_bytes, "temporary Markdown apply report"),
    )
    present_reports: list[Path] = []
    for path, expected, label in expected_reports:
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            raise FinalEditorialError(
                f"ambiguous interrupted editorial prefix: {label} is not a regular file"
            )
        if path.read_bytes() != expected:
            raise FinalEditorialError(
                f"interrupted editorial prefix {label} does not match reconstructed bytes"
            )
        present_reports.append(path)

    proven_stages: list[Path] = []
    for stage in stage_paths:
        if stage.is_symlink() or not stage.is_dir():
            raise FinalEditorialError(
                f"ambiguous interrupted editorial stage artifact: {stage}"
            )
        staged = _inventory_payloads(stage, expected_count)
        if framed_corpus_fingerprint(staged) != output_fingerprint:
            raise FinalEditorialError(
                f"interrupted editorial stage does not match reconstructed output: {stage}"
            )
        proven_stages.append(stage)

    cleanup_order = (
        report_md_temp,
        paths.apply_md,
        report_json_temp,
        paths.apply_json,
    )
    present_set = set(present_reports)
    for path in cleanup_order:
        if path in present_set:
            path.unlink()
    cleanup_proven_stages(
        stage_cleanup,
        proven_stages,
        error_cls=FinalEditorialError,
    )
    return True


def _prove_terminal_editorial_commit_after_replace_error(
    paths: EditorialPaths,
    *,
    stage: Path,
    report_json_temp: Path,
    report_md_temp: Path,
    report_json_bytes: bytes,
    report_md_bytes: bytes,
    output_fingerprint: dict[str, Any],
    source_fingerprint: dict[str, Any],
    expected_count: int,
) -> bool:
    """Prove that the terminal editorial rename committed despite a reported error."""
    if stage.exists():
        return False
    if (
        not paths.output_dir.exists()
        or paths.output_dir.is_symlink()
        or not paths.output_dir.is_dir()
    ):
        return False
    if report_json_temp.exists() or report_md_temp.exists():
        return False
    expected_reports = (
        (paths.apply_json, report_json_bytes),
        (paths.apply_md, report_md_bytes),
    )
    for path, expected in expected_reports:
        if not path.exists() or path.is_symlink() or not path.is_file():
            return False
        try:
            if path.read_bytes() != expected:
                return False
        except OSError:
            return False
    try:
        installed = _inventory_payloads(paths.output_dir, expected_count)
        if framed_corpus_fingerprint(installed) != output_fingerprint:
            return False
        source_now = _inventory_payloads(paths.publication_dir, expected_count)
        if framed_corpus_fingerprint(source_now) != source_fingerprint:
            return False
    except (OSError, FinalEditorialError):
        return False
    return True


def apply_ready_plan(project: NovelProject) -> dict[str, Any]:
    paths = editorial_paths(project)
    if paths.output_dir.exists():
        raise FinalEditorialError(
            f"refusing to overwrite existing final editorial output: {paths.output_dir}"
        )
    plan = _load_json_object(paths.plan_json, "final editorial plan")
    if not paths.plan_md.is_file():
        raise FinalEditorialError(f"missing final editorial Markdown plan: {paths.plan_md}")
    manifest, authorization_sha256 = load_authorization(paths.authorization)
    _validate_ready_plan(plan, project, authorization_sha256)
    checkpoint = _expected_checkpoint(manifest)
    edits = _validate_authorized_set(manifest)
    payloads = _inventory_payloads(paths.publication_dir, checkpoint["file_count"])
    source_fingerprint = framed_corpus_fingerprint(payloads)
    _require(source_fingerprint, plan["source_corpus_fingerprint"], "plan source fingerprint")
    cleanup = _load_json_object(paths.cleanup_report, "publication cleanup report")
    _validate_cleanup_chain(cleanup, source_fingerprint, checkpoint)
    korean_bytes = paths.korean_source.read_bytes()
    _validate_source_evidence(korean_bytes, manifest, edits)
    _require(
        {
            "algorithm": "sha256-file-bytes-v1",
            "byte_count": len(korean_bytes),
            "sha256": sha256_bytes(korean_bytes),
        },
        plan["korean_source_fingerprint"],
        "plan Korean source fingerprint",
    )
    outputs, independent_operations, guards = simulate(manifest, payloads)
    _require(len(independent_operations), plan["operation_count"], "independent operation count")
    _require(
        independent_operations,
        plan.get("operations"),
        "independently reconstructed operation records",
    )
    _require(
        manifest["authorized_set"]["affected_units"],
        plan.get("affected_units"),
        "independently reconstructed affected-unit set",
    )
    _require(
        sum(item["scope"] == "heading" for item in independent_operations),
        plan.get("heading_operation_count"),
        "independently reconstructed heading operation count",
    )
    _require(
        sum(item["scope"] == "prose" for item in independent_operations),
        plan.get("prose_operation_count"),
        "independently reconstructed prose operation count",
    )
    output_fingerprint = framed_corpus_fingerprint(outputs)
    _require(
        output_fingerprint,
        plan["expected_output_corpus_fingerprint"],
        "planned output fingerprint",
    )
    report = _build_apply_report(
        project, plan, authorization_sha256, output_fingerprint, guards
    )
    report_json_bytes = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    report_md_bytes = render_apply_markdown(report).encode("utf-8")
    _recover_exact_interrupted_apply_prefix(
        paths,
        report_json_bytes=report_json_bytes,
        report_md_bytes=report_md_bytes,
        output_fingerprint=output_fingerprint,
        expected_count=checkpoint["file_count"],
    )

    paths.output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".editorial-stage-", dir=paths.output_dir.parent))
    published = False
    try:
        for name, data in outputs:
            (stage / name).write_bytes(data)
        staged = _inventory_payloads(stage, checkpoint["file_count"])
        _require(
            framed_corpus_fingerprint(staged), output_fingerprint, "staged output fingerprint"
        )
        source_after_stage = framed_corpus_fingerprint(
            _inventory_payloads(paths.publication_dir, checkpoint["file_count"])
        )
        _require(source_after_stage, source_fingerprint, "publication source after staging")
        report_json_temp = paths.apply_json.with_name(f".{paths.apply_json.name}.tmp")
        report_md_temp = paths.apply_md.with_name(f".{paths.apply_md.name}.tmp")
        if report_json_temp.exists() or report_md_temp.exists():
            raise FinalEditorialError("stale final editorial report temporary file exists")
        paths.apply_json.parent.mkdir(parents=True, exist_ok=True)
        report_json_temp.write_bytes(report_json_bytes)
        report_md_temp.write_bytes(report_md_bytes)
        try:
            if paths.output_dir.exists() or paths.apply_json.exists() or paths.apply_md.exists():
                raise FinalEditorialError("final editorial output/report appeared during apply")
            os.replace(report_json_temp, paths.apply_json)
            os.replace(report_md_temp, paths.apply_md)
            final_source = framed_corpus_fingerprint(
                _inventory_payloads(paths.publication_dir, checkpoint["file_count"])
            )
            _require(final_source, source_fingerprint, "publication source before commit")
            os.replace(stage, paths.output_dir)
            published = True
        except Exception as exc:
            if paths.output_dir.exists():
                if _prove_terminal_editorial_commit_after_replace_error(
                    paths,
                    stage=stage,
                    report_json_temp=report_json_temp,
                    report_md_temp=report_md_temp,
                    report_json_bytes=report_json_bytes,
                    report_md_bytes=report_md_bytes,
                    output_fingerprint=output_fingerprint,
                    source_fingerprint=source_fingerprint,
                    expected_count=checkpoint["file_count"],
                ):
                    published = True
                    return report
                raise FinalEditorialError(
                    "ambiguous terminal editorial commit outcome after replace error; "
                    "existing output and reports were preserved"
                ) from exc
            report_json_temp.unlink(missing_ok=True)
            report_md_temp.unlink(missing_ok=True)
            paths.apply_json.unlink(missing_ok=True)
            paths.apply_md.unlink(missing_ok=True)
            raise
        return report
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def summarize_for_cli(report: dict[str, Any], keys: Iterable[str]) -> str:
    return json.dumps({key: report.get(key) for key in keys}, ensure_ascii=False, indent=2)
