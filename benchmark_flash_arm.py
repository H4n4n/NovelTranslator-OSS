"""Restart-safe 60-case DeepSeek V4.1 Flash mature-glossary benchmark arm.

The existing one-call Flash smoke harness is the paid transaction primitive.
This coordinator only freezes the 60-case arm, adopts the already-completed
Chapter 63 smoke, selects the next still-unused case, and finalizes the corpus.
Importing this module performs no credential loading, writes, network activity,
or provider calls.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from benchmark_flash_smoke import (
    EXPECTED_FLASH_API_MODEL,
    EXPECTED_FLASH_MAX_TOKENS,
    EXPECTED_FLASH_THINKING,
    EXPECTED_FLASH_VERSION,
    FlashSmokeError,
    FlashSmokeUncertainError,
    build_smoke_plan,
    execute_paid_smoke,
)
from benchmark_model_config import resolve_benchmark_model
from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    result_totals,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION


ARM_SCHEMA_VERSION = 1
ARM_KIND = "deepseek-v4.1-flash-mature-glossary-arm-v1"
FLASH_SELECTOR = "deepseek-v4.1-flash"
CASES_PER_NOVEL = 30
TOTAL_CASES = 60
EXECUTE_CONFIRMATION = "AUTHORIZE_ONE_DEEPSEEK_V41_FLASH_ARM_REQUEST"
PLAN_FILENAME = "arm_plan.json"
JOURNAL_FILENAME = "arm_journal.jsonl"
FINAL_RESULTS_FILENAME = "flash_results.json"
FINAL_CORPUS_FILENAME = "flash_corpus.jsonl"
FINAL_MANIFEST_FILENAME = "final_manifest.json"


class FlashArmError(BenchmarkError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_json_sha256(value) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _load_json(path: Path, label: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FlashArmError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise FlashArmError(f"{label} must be a JSON object")
    return value


def _git_head(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FlashArmError(f"repository HEAD could not be resolved: {exc}") from exc
    value = result.stdout.strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise FlashArmError(f"repository HEAD is malformed: {value!r}")
    return value


def _ensure_head(repo_root: Path, expected: str) -> str:
    expected = (expected or "").strip().lower()
    if len(expected) != 40 or any(ch not in "0123456789abcdef" for ch in expected):
        raise FlashArmError("--expected-repo-head must be an exact SHA")
    actual = _git_head(repo_root)
    if actual != expected:
        raise BenchmarkSafetyError(f"repository HEAD drifted: {actual} != {expected}")
    return actual


def _external_root(repo_root: Path, output_root: Path, *, exists: bool) -> Path:
    repo_root = Path(repo_root).resolve()
    output_root = Path(output_root)
    if not output_root.is_absolute():
        raise FlashArmError("--output-root must be absolute")
    output_root = output_root.resolve()
    if output_root == repo_root or repo_root in output_root.parents:
        raise BenchmarkSafetyError("Flash arm output must remain outside the repository")
    if exists and not output_root.is_dir():
        raise FlashArmError(f"Flash arm output root does not exist: {output_root}")
    if not exists and output_root.exists():
        raise BenchmarkSafetyError(f"Flash arm output root already exists: {output_root}")
    return output_root


def _write_exclusive(path: Path, payload: bytes):
    try:
        with Path(path).open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise BenchmarkSafetyError(f"artifact already exists: {path}") from exc


def _write_json_exclusive(path: Path, value):
    _write_exclusive(
        path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    )


def _append_journal(path: Path, event: dict):
    with Path(path).open("ab") as handle:
        handle.write(_canonical_json_bytes(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _journal_sequence(path: Path):
    if not Path(path).is_file():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line).get("event"))
    return rows


def _load_sample_plan(inputs, path: Path, expected_sha: str):
    path = Path(path)
    if not path.is_file():
        raise FlashArmError(f"sample plan is missing: {path}")
    actual_sha = _sha256_file(path)
    if actual_sha != (expected_sha or "").lower():
        raise BenchmarkSafetyError(
            f"{inputs.project.config.id} sample-plan SHA mismatch: {actual_sha} != {expected_sha}"
        )
    plan = _load_json(path, f"{inputs.project.config.id} sample plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise FlashArmError("sample plan schema mismatch")
    if plan.get("benchmark") != "mature-glossary-engine-benchmark-v2":
        raise FlashArmError("sample plan belongs to another benchmark")
    if plan.get("novel_id") != inputs.project.config.id:
        raise FlashArmError("sample plan novel identity mismatch")
    if plan.get("protected_state_binding") != capture_protected_state(inputs.project):
        raise BenchmarkSafetyError(
            f"{inputs.project.config.id} production-owned source/glossary state drifted"
        )
    selected = plan.get("selected_chapters")
    rows = plan.get("selections")
    if not isinstance(selected, list) or len(selected) != CASES_PER_NOVEL or len(set(selected)) != CASES_PER_NOVEL:
        raise FlashArmError("sample plan must contain exactly 30 unique chapters")
    if not isinstance(rows, list) or len(rows) != CASES_PER_NOVEL:
        raise FlashArmError("sample plan selection rows are incomplete")
    by_chapter = {row.get("chapter_number"): row for row in rows if isinstance(row, dict)}
    if len(by_chapter) != CASES_PER_NOVEL or set(by_chapter) != set(selected):
        raise FlashArmError("sample plan chapter rows do not match selected_chapters")
    return plan, actual_sha, by_chapter


def _expected_config():
    config = resolve_benchmark_model(FLASH_SELECTOR)
    expected = (
        "deepseek",
        EXPECTED_FLASH_API_MODEL,
        EXPECTED_FLASH_VERSION,
        EXPECTED_FLASH_THINKING,
        EXPECTED_FLASH_MAX_TOKENS,
    )
    actual = (config.provider, config.model, config.version, config.reasoning, config.max_tokens)
    if actual != expected:
        raise BenchmarkSafetyError(f"Flash benchmark configuration drifted: {actual!r}")
    return config


def _verify_smoke(smoke_dir: Path, case: dict, expected_identity: str, expected_translation: str):
    smoke_dir = Path(smoke_dir).resolve()
    complete_path = smoke_dir / "complete.json"
    request_path = smoke_dir / "request.json"
    translation_path = smoke_dir / "translation.txt"
    journal_path = smoke_dir / "journal.jsonl"
    for path in (complete_path, request_path, translation_path, journal_path):
        if not path.is_file():
            raise FlashArmError(f"smoke evidence is incomplete: {path}")
    complete = _load_json(complete_path, "Flash smoke complete artifact")
    identity = complete.get("identity") or {}
    if complete.get("status") != "complete" or complete.get("identity_sha256") != expected_identity:
        raise BenchmarkSafetyError("Flash smoke terminal identity mismatch")
    checks = {
        "novel_id": case["novel_id"],
        "chapter_number": case["chapter_number"],
        "source_sha256": case["source_sha256"],
        "relevant_glossary_sha256": case["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": case["frozen_pro_request_sha256"],
        "flash_request_sha256": case["flash_request_sha256"],
        "api_model": EXPECTED_FLASH_API_MODEL,
        "model_version": EXPECTED_FLASH_VERSION,
        "thinking": EXPECTED_FLASH_THINKING,
        "max_tokens": EXPECTED_FLASH_MAX_TOKENS,
    }
    for key, expected in checks.items():
        if identity.get(key) != expected:
            raise BenchmarkSafetyError(f"Flash smoke {key} binding mismatch")
    if _sha256_file(request_path) != case["flash_request_sha256"]:
        raise BenchmarkSafetyError("Flash smoke request hash mismatch")
    if _sha256_file(translation_path) != expected_translation:
        raise BenchmarkSafetyError("Flash smoke translation hash mismatch")
    provider = complete.get("provider_response") or {}
    safety = complete.get("safety") or {}
    if provider.get("response_model") != EXPECTED_FLASH_API_MODEL or provider.get("finish_reason") != "stop":
        raise BenchmarkSafetyError("Flash smoke provider identity/finish mismatch")
    if safety.get("provider_calls_attempted") != 1 or safety.get("paid_requests_attempted") != 1:
        raise BenchmarkSafetyError("Flash smoke paid-call accounting mismatch")
    if safety.get("provider_response_recorded") is not True or safety.get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError("Flash smoke safety proof is incomplete")
    if _journal_sequence(journal_path) != ["planned", "started", "response_recorded", "complete"]:
        raise BenchmarkSafetyError("Flash smoke journal sequence mismatch")
    return complete, translation_path


def prepare_arm(*, repo_root: Path, expected_repo_head: str, output_root: Path, specs,
                smoke_dir: Path, expected_smoke_identity: str,
                expected_smoke_translation: str, now=utc_now):
    repo_root = Path(repo_root).resolve()
    head = _ensure_head(repo_root, expected_repo_head)
    output_root = _external_root(repo_root, output_root, exists=False)
    _expected_config()
    if len(specs) != 2 or len({spec[0] for spec in specs}) != 2:
        raise FlashArmError("Flash arm requires exactly two distinct novel specs")

    cases = []
    frozen_requests = {}
    plan_bindings = []
    protected_bindings = {}
    ordinal = 0
    for novel_id, plan_path, plan_sha in specs:
        inputs = load_benchmark_inputs(novel_id, repo_root)
        plan, actual_sha, by_chapter = _load_sample_plan(inputs, plan_path, plan_sha)
        protected_bindings[novel_id] = capture_protected_state(inputs.project)
        plan_bindings.append({
            "novel_id": novel_id,
            "path": str(Path(plan_path).resolve()),
            "sha256": actual_sha,
            "selected_chapters": list(plan["selected_chapters"]),
        })
        for chapter in plan["selected_chapters"]:
            ordinal += 1
            case_id = f"{ordinal:02d}-{novel_id}-ch{chapter:04d}"
            case_dir = output_root / "cases" / case_id
            tx = build_smoke_plan(
                repo_root=repo_root,
                novel_id=novel_id,
                chapter_number=chapter,
                plan_path=Path(plan_path),
                expected_plan_sha256=actual_sha,
                output_dir=case_dir,
                expected_repo_head=head,
            )
            row = by_chapter[chapter]
            case = {
                "ordinal": ordinal,
                "case_id": case_id,
                "novel_id": novel_id,
                "chapter_number": chapter,
                "stratum": int(row["stratum"]),
                "role": row["role"],
                "sample_plan_sha256": actual_sha,
                "sample_plan_path": str(Path(plan_path).resolve()),
                "source_sha256": tx["row"]["source_sha256"],
                "relevant_glossary_sha256": tx["row"]["relevant_glossary_sha256"],
                "frozen_pro_request_sha256": tx["pro_request_sha256"],
                "flash_request_sha256": tx["request_sha256"],
                "transaction_identity_sha256": tx["identity_sha256"],
                "case_dir": f"cases/{case_id}",
            }
            cases.append(case)
            frozen_requests[case_id] = tx["request"]

    if len(cases) != TOTAL_CASES or len({case["case_id"] for case in cases}) != TOTAL_CASES:
        raise FlashArmError("Flash arm did not resolve to exactly 60 unique cases")
    smoke_matches = [case for case in cases if case["novel_id"] == "great_king_zhuxia" and case["chapter_number"] == 63]
    if len(smoke_matches) != 1:
        raise FlashArmError("frozen arm must contain great_king_zhuxia Chapter 63 exactly once")
    smoke_case = smoke_matches[0]
    smoke_complete, smoke_translation_path = _verify_smoke(
        smoke_dir, smoke_case, expected_smoke_identity, expected_smoke_translation
    )

    identity = {
        "kind": ARM_KIND,
        "repo_head": head,
        "provider": "deepseek",
        "api_model": EXPECTED_FLASH_API_MODEL,
        "model_version": EXPECTED_FLASH_VERSION,
        "thinking": EXPECTED_FLASH_THINKING,
        "max_tokens": EXPECTED_FLASH_MAX_TOKENS,
        "output_root": str(output_root),
        "sample_plans": plan_bindings,
        "protected_state_sha256": {
            key: _canonical_json_sha256(value)
            for key, value in sorted(protected_bindings.items())
        },
        "case_count": TOTAL_CASES,
        "cases": cases,
        "imported_smoke": {
            "case_id": smoke_case["case_id"],
            "smoke_identity_sha256": expected_smoke_identity,
            "smoke_translation_sha256": expected_smoke_translation,
            "smoke_complete_sha256": _sha256_file(Path(smoke_dir) / "complete.json"),
        },
    }
    arm_id = _canonical_json_sha256(identity)
    plan_document = {
        "schema_version": ARM_SCHEMA_VERSION,
        "kind": ARM_KIND,
        "status": "prepared",
        "arm_id": arm_id,
        "identity": identity,
        "authorization": {
            "total_cases": TOTAL_CASES,
            "imported_completed_cases": 1,
            "new_paid_requests_required": TOTAL_CASES - 1,
            "automatic_retry_forbidden": True,
            "ambiguous_or_response_failed_blocks_arm": True,
        },
        "protected_state_bindings": protected_bindings,
    }

    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "cases").mkdir()
    (output_root / "requests").mkdir()
    _write_json_exclusive(output_root / PLAN_FILENAME, plan_document)
    _write_exclusive(output_root / JOURNAL_FILENAME, b"")
    for case in cases:
        path = output_root / "requests" / f"{case['case_id']}.json"
        _write_exclusive(path, _canonical_json_bytes(frozen_requests[case["case_id"]]))
        if _sha256_file(path) != case["flash_request_sha256"]:
            raise BenchmarkSafetyError(f"request freeze failed for {case['case_id']}")

    imported_dir = output_root / smoke_case["case_dir"]
    imported_dir.mkdir(parents=True)
    shutil.copyfile(smoke_translation_path, imported_dir / "translation.txt")
    if _sha256_file(imported_dir / "translation.txt") != expected_smoke_translation:
        raise BenchmarkSafetyError("imported smoke translation copy hash mismatch")
    _write_json_exclusive(imported_dir / "import_record.json", {
        "status": "imported_complete",
        "case_id": smoke_case["case_id"],
        "source_smoke_dir": str(Path(smoke_dir).resolve()),
        "smoke_identity_sha256": expected_smoke_identity,
        "smoke_complete_sha256": _sha256_file(Path(smoke_dir) / "complete.json"),
        "request_sha256": smoke_case["flash_request_sha256"],
        "translation_sha256": expected_smoke_translation,
        "provider_response": copy.deepcopy(smoke_complete["provider_response"]),
        "result": copy.deepcopy(smoke_complete["result"]),
        "safety": copy.deepcopy(smoke_complete["safety"]),
    })
    _append_journal(output_root / JOURNAL_FILENAME, {
        "event": "prepared",
        "at_utc": now().isoformat(),
        "arm_id": arm_id,
        "case_count": TOTAL_CASES,
        "new_paid_requests_required": TOTAL_CASES - 1,
    })
    _append_journal(output_root / JOURNAL_FILENAME, {
        "event": "smoke_imported",
        "at_utc": now().isoformat(),
        "case_id": smoke_case["case_id"],
        "translation_sha256": expected_smoke_translation,
    })
    for novel_id, expected in protected_bindings.items():
        if capture_protected_state(load_benchmark_inputs(novel_id, repo_root).project) != expected:
            raise BenchmarkSafetyError(f"{novel_id} protected state changed during arm prepare")
    return {
        "arm_id": arm_id,
        "plan_sha256": _sha256_file(output_root / PLAN_FILENAME),
        "output_root": str(output_root),
        "imported_case_id": smoke_case["case_id"],
        "pending_paid": TOTAL_CASES - 1,
    }


def _load_arm(repo_root: Path, expected_repo_head: str, output_root: Path, expected_plan_sha: str):
    repo_root = Path(repo_root).resolve()
    head = _ensure_head(repo_root, expected_repo_head)
    output_root = _external_root(repo_root, output_root, exists=True)
    plan_path = output_root / PLAN_FILENAME
    actual_sha = _sha256_file(plan_path)
    if actual_sha != (expected_plan_sha or "").lower():
        raise BenchmarkSafetyError(f"arm plan SHA mismatch: {actual_sha} != {expected_plan_sha}")
    plan = _load_json(plan_path, "Flash arm plan")
    if plan.get("schema_version") != ARM_SCHEMA_VERSION or plan.get("kind") != ARM_KIND:
        raise FlashArmError("Flash arm plan schema/kind mismatch")
    identity = plan.get("identity") or {}
    if plan.get("arm_id") != _canonical_json_sha256(identity):
        raise BenchmarkSafetyError("Flash arm ID mismatch")
    if identity.get("repo_head") != head:
        raise BenchmarkSafetyError("Flash arm repository-head binding mismatch")
    _expected_config()
    cases = identity.get("cases")
    if not isinstance(cases, list) or len(cases) != TOTAL_CASES:
        raise FlashArmError("Flash arm does not contain exactly 60 cases")
    protected = plan.get("protected_state_bindings") or {}
    if len(protected) != 2:
        raise FlashArmError("Flash arm protected-state bindings are malformed")
    for novel_id, expected in protected.items():
        if capture_protected_state(load_benchmark_inputs(novel_id, repo_root).project) != expected:
            raise BenchmarkSafetyError(f"{novel_id} protected state drifted after prepare")
    return plan, output_root, actual_sha


def _validate_persisted_complete(output_root: Path, case: dict):
    case_dir = output_root / case["case_dir"]
    complete_path = case_dir / "complete.json"
    request_path = case_dir / "request.json"
    translation_path = case_dir / "translation.txt"
    journal_path = case_dir / "journal.jsonl"
    frozen_request_path = output_root / "requests" / f"{case['case_id']}.json"

    for path in (complete_path, request_path, translation_path, journal_path, frozen_request_path):
        if not path.is_file():
            raise BenchmarkSafetyError(
                f"{case['case_id']} complete evidence is missing: {path.name}"
            )

    complete = _load_json(complete_path, f"{case['case_id']} complete artifact")
    if complete.get("status") != "complete":
        raise BenchmarkSafetyError(f"{case['case_id']} complete marker has non-complete status")

    identity = complete.get("identity")
    if not isinstance(identity, dict):
        raise BenchmarkSafetyError(f"{case['case_id']} complete identity is malformed")
    identity_sha = complete.get("identity_sha256")
    if identity_sha != case.get("transaction_identity_sha256"):
        raise BenchmarkSafetyError(f"{case['case_id']} transaction identity mismatch")
    if identity_sha != _canonical_json_sha256(identity):
        raise BenchmarkSafetyError(f"{case['case_id']} complete identity hash mismatch")

    checks = {
        "novel_id": case["novel_id"],
        "chapter_number": case["chapter_number"],
        "sample_plan_sha256": case["sample_plan_sha256"],
        "source_sha256": case["source_sha256"],
        "relevant_glossary_sha256": case["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": case["frozen_pro_request_sha256"],
        "flash_request_sha256": case["flash_request_sha256"],
        "provider": "deepseek",
        "api_model": EXPECTED_FLASH_API_MODEL,
        "model_version": EXPECTED_FLASH_VERSION,
        "thinking": EXPECTED_FLASH_THINKING,
        "max_tokens": EXPECTED_FLASH_MAX_TOKENS,
    }
    for key, expected in checks.items():
        if identity.get(key) != expected:
            raise BenchmarkSafetyError(f"{case['case_id']} complete {key} binding mismatch")

    safety = complete.get("safety") or {}
    provider = complete.get("provider_response") or {}
    artifacts = complete.get("artifacts") or {}
    if safety.get("provider_calls_attempted") != 1 or safety.get("paid_requests_attempted") != 1:
        raise BenchmarkSafetyError(f"{case['case_id']} paid-call accounting mismatch")
    if safety.get("provider_response_recorded") is not True:
        raise BenchmarkSafetyError(f"{case['case_id']} provider response is not durably recorded")
    if safety.get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError(f"{case['case_id']} protected-state proof is incomplete")
    if safety.get("automatic_retry_forbidden") is not True:
        raise BenchmarkSafetyError(f"{case['case_id']} automatic-retry proof is missing")

    protected_before = safety.get("protected_state_before_sha256")
    protected_after = safety.get("protected_state_after_sha256")
    if not protected_before or protected_before != protected_after:
        raise BenchmarkSafetyError(f"{case['case_id']} protected-state hashes do not match")
    if identity.get("protected_state_sha256") != protected_before:
        raise BenchmarkSafetyError(f"{case['case_id']} protected-state identity binding mismatch")

    if provider.get("response_model") != EXPECTED_FLASH_API_MODEL:
        raise BenchmarkSafetyError(f"{case['case_id']} provider response model drifted")
    if provider.get("finish_reason") != "stop":
        raise BenchmarkSafetyError(f"{case['case_id']} finish_reason is not stop")
    if _journal_sequence(journal_path) != ["planned", "started", "response_recorded", "complete"]:
        raise BenchmarkSafetyError(f"{case['case_id']} journal sequence mismatch")

    translation_sha = artifacts.get("translation_sha256")
    if not translation_sha or _sha256_file(translation_path) != translation_sha:
        raise BenchmarkSafetyError(f"{case['case_id']} translation hash drifted")
    if _sha256_file(request_path) != case["flash_request_sha256"]:
        raise BenchmarkSafetyError(f"{case['case_id']} request hash drifted")
    if _sha256_file(frozen_request_path) != case["flash_request_sha256"]:
        raise BenchmarkSafetyError(f"{case['case_id']} frozen request hash drifted")
    if request_path.read_bytes() != frozen_request_path.read_bytes():
        raise BenchmarkSafetyError(f"{case['case_id']} request bytes differ from frozen request")
    return complete


def _case_status(output_root: Path, case: dict):
    case_dir = output_root / case["case_dir"]
    if not case_dir.exists():
        return "planned"
    if not case_dir.is_dir():
        raise BenchmarkSafetyError(f"case path is not a directory: {case_dir}")
    markers = {
        "imported": case_dir / "import_record.json",
        "complete": case_dir / "complete.json",
        "uncertain": case_dir / "uncertain.json",
        "response_failed": case_dir / "response_failed.json",
    }
    present = [name for name, path in markers.items() if path.is_file()]
    if len(present) != 1:
        raise BenchmarkSafetyError(f"case {case['case_id']} has ambiguous terminal markers: {present}")
    terminal = present[0]
    if terminal == "complete":
        try:
            _validate_persisted_complete(output_root, case)
        except Exception:
            return "invalid_complete"
    return terminal


def arm_status(plan: dict, output_root: Path):
    counts = {
        name: 0
        for name in (
            "planned",
            "imported",
            "complete",
            "invalid_complete",
            "uncertain",
            "response_failed",
        )
    }
    next_case = None
    for case in plan["identity"]["cases"]:
        status = _case_status(output_root, case)
        counts[status] += 1
        if status == "planned" and next_case is None:
            next_case = case
    return {
        "counts": counts,
        "next_case": next_case,
        "blocked": bool(
            counts["invalid_complete"]
            or counts["uncertain"]
            or counts["response_failed"]
        ),
        "completed_total": counts["imported"] + counts["complete"],
    }


def _rebuild_case_transaction(repo_root: Path, output_root: Path, case: dict, expected_head: str):
    tx = build_smoke_plan(
        repo_root=repo_root,
        novel_id=case["novel_id"],
        chapter_number=case["chapter_number"],
        plan_path=Path(case["sample_plan_path"]),
        expected_plan_sha256=case["sample_plan_sha256"],
        output_dir=output_root / case["case_dir"],
        expected_repo_head=expected_head,
    )
    checks = {
        "source_sha256": tx["row"]["source_sha256"],
        "relevant_glossary_sha256": tx["row"]["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": tx["pro_request_sha256"],
        "flash_request_sha256": tx["request_sha256"],
        "transaction_identity_sha256": tx["identity_sha256"],
    }
    for key, actual in checks.items():
        if case.get(key) != actual:
            raise BenchmarkSafetyError(f"{case['case_id']} {key} drifted")
    frozen_request = output_root / "requests" / f"{case['case_id']}.json"
    if not frozen_request.is_file() or _sha256_file(frozen_request) != case["flash_request_sha256"]:
        raise BenchmarkSafetyError(f"{case['case_id']} frozen request artifact drifted")
    if frozen_request.read_bytes() != _canonical_json_bytes(tx["request"]):
        raise BenchmarkSafetyError(f"{case['case_id']} frozen request bytes drifted")
    return tx


def execute_next(*, repo_root: Path, expected_repo_head: str, output_root: Path,
                 expected_plan_sha: str, confirmation: str, now=utc_now):
    if confirmation != EXECUTE_CONFIRMATION:
        raise FlashArmError("one-request Flash arm execution was not explicitly authorized")
    plan, output_root, _ = _load_arm(
        repo_root, expected_repo_head, output_root, expected_plan_sha
    )
    status = arm_status(plan, output_root)
    if status["blocked"]:
        raise BenchmarkSafetyError(
            "Flash arm contains invalid_complete/uncertain/response_failed evidence; "
            "further paid calls are blocked"
        )
    case = status["next_case"]
    if case is None:
        raise FlashArmError("Flash arm has no remaining planned case")
    tx = _rebuild_case_transaction(Path(repo_root), output_root, case, expected_repo_head)
    try:
        result = execute_paid_smoke(tx)
    except (FlashSmokeUncertainError, FlashSmokeError) as exc:
        terminal = _case_status(output_root, case)
        _append_journal(output_root / JOURNAL_FILENAME, {
            "event": f"case_{terminal}",
            "at_utc": now().isoformat(),
            "case_id": case["case_id"],
            "retry_forbidden": True,
        })
        raise FlashArmError(
            f"{case['case_id']} ended as {terminal}; arm is blocked and retry is forbidden"
        ) from exc
    if result.get("status") != "complete":
        raise BenchmarkSafetyError(f"{case['case_id']} returned a non-complete terminal result")
    if (result.get("provider_response") or {}).get("response_model") != EXPECTED_FLASH_API_MODEL:
        raise BenchmarkSafetyError(f"{case['case_id']} provider response model drifted")
    if (result.get("provider_response") or {}).get("finish_reason") != "stop":
        raise BenchmarkSafetyError(f"{case['case_id']} finish_reason is not stop")
    _append_journal(output_root / JOURNAL_FILENAME, {
        "event": "case_complete",
        "at_utc": now().isoformat(),
        "case_id": case["case_id"],
        "novel_id": case["novel_id"],
        "chapter_number": case["chapter_number"],
        "translation_sha256": (result.get("artifacts") or {}).get("translation_sha256"),
        "system_fingerprint": (result.get("provider_response") or {}).get("system_fingerprint"),
    })
    return case, result


def _materialize_case(output_root: Path, case: dict):
    case_dir = output_root / case["case_dir"]
    status = _case_status(output_root, case)
    if status == "imported":
        record = _load_json(case_dir / "import_record.json", f"{case['case_id']} import record")
        translation_path = case_dir / "translation.txt"
        if _sha256_file(translation_path) != record.get("translation_sha256"):
            raise BenchmarkSafetyError(f"{case['case_id']} imported translation drifted")
        return "imported_smoke", record, translation_path
    if status == "invalid_complete":
        raise BenchmarkSafetyError(
            f"{case['case_id']} has persisted complete evidence that failed semantic validation"
        )
    if status != "complete":
        raise FlashArmError(f"{case['case_id']} is not complete: {status}")
    complete = _load_json(case_dir / "complete.json", f"{case['case_id']} complete artifact")
    identity = complete.get("identity") or {}
    checks = {
        "novel_id": case["novel_id"],
        "chapter_number": case["chapter_number"],
        "source_sha256": case["source_sha256"],
        "relevant_glossary_sha256": case["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": case["frozen_pro_request_sha256"],
        "flash_request_sha256": case["flash_request_sha256"],
    }
    for key, expected in checks.items():
        if identity.get(key) != expected:
            raise BenchmarkSafetyError(f"{case['case_id']} complete {key} binding mismatch")
    safety = complete.get("safety") or {}
    provider = complete.get("provider_response") or {}
    if safety.get("provider_calls_attempted") != 1 or safety.get("paid_requests_attempted") != 1:
        raise BenchmarkSafetyError(f"{case['case_id']} paid-call accounting mismatch")
    if safety.get("provider_response_recorded") is not True or safety.get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError(f"{case['case_id']} safety proof is incomplete")
    if provider.get("response_model") != EXPECTED_FLASH_API_MODEL or provider.get("finish_reason") != "stop":
        raise BenchmarkSafetyError(f"{case['case_id']} provider identity/finish drifted")
    if _journal_sequence(case_dir / "journal.jsonl") != ["planned", "started", "response_recorded", "complete"]:
        raise BenchmarkSafetyError(f"{case['case_id']} journal sequence mismatch")
    translation_path = case_dir / "translation.txt"
    if _sha256_file(translation_path) != (complete.get("artifacts") or {}).get("translation_sha256"):
        raise BenchmarkSafetyError(f"{case['case_id']} translation hash drifted")
    if _sha256_file(case_dir / "request.json") != case["flash_request_sha256"]:
        raise BenchmarkSafetyError(f"{case['case_id']} request hash drifted")
    return "paid_arm", complete, translation_path


def finalize_arm(*, repo_root: Path, expected_repo_head: str, output_root: Path,
                 expected_plan_sha: str, now=utc_now):
    plan, output_root, plan_sha = _load_arm(
        repo_root, expected_repo_head, output_root, expected_plan_sha
    )
    status = arm_status(plan, output_root)
    if status["blocked"]:
        raise BenchmarkSafetyError(
            "Flash arm is blocked by invalid_complete/uncertain/response_failed evidence"
        )
    if status["completed_total"] != TOTAL_CASES:
        raise FlashArmError(f"Flash arm is incomplete: {status['completed_total']}/{TOTAL_CASES}")
    for name in (FINAL_RESULTS_FILENAME, FINAL_CORPUS_FILENAME, FINAL_MANIFEST_FILENAME):
        if (output_root / name).exists():
            raise BenchmarkSafetyError(f"final artifact already exists: {name}")

    cases_out, results, corpus, fingerprints = [], [], [], set()
    imported = paid = 0
    for case in plan["identity"]["cases"]:
        mode, document, translation_path = _materialize_case(output_root, case)
        if mode == "imported_smoke":
            imported += 1
        else:
            paid += 1
        provider = document.get("provider_response") or {}
        result = document.get("result") or {}
        translation = translation_path.read_text(encoding="utf-8")
        translation_sha = _sha256_file(translation_path)
        if provider.get("system_fingerprint"):
            fingerprints.add(str(provider["system_fingerprint"]))
        results.append(result)
        cases_out.append({
            **case,
            "execution_mode": mode,
            "translation_sha256": translation_sha,
            "provider_response": copy.deepcopy(provider),
            "result": copy.deepcopy(result),
        })
        corpus.append(json.dumps({
            "case_id": case["case_id"],
            "ordinal": case["ordinal"],
            "novel_id": case["novel_id"],
            "chapter_number": case["chapter_number"],
            "stratum": case["stratum"],
            "role": case["role"],
            "source_sha256": case["source_sha256"],
            "relevant_glossary_sha256": case["relevant_glossary_sha256"],
            "frozen_pro_request_sha256": case["frozen_pro_request_sha256"],
            "flash_request_sha256": case["flash_request_sha256"],
            "translation_sha256": translation_sha,
            "translation": translation,
        }, ensure_ascii=False, separators=(",", ":")))
    if imported != 1 or paid != 59:
        raise BenchmarkSafetyError(f"execution accounting mismatch: imported={imported}, paid={paid}")

    results_document = {
        "schema_version": ARM_SCHEMA_VERSION,
        "kind": ARM_KIND,
        "status": "complete",
        "arm_id": plan["arm_id"],
        "arm_plan_sha256": plan_sha,
        "provider": "deepseek",
        "api_model": EXPECTED_FLASH_API_MODEL,
        "model_version": EXPECTED_FLASH_VERSION,
        "thinking": EXPECTED_FLASH_THINKING,
        "max_tokens": EXPECTED_FLASH_MAX_TOKENS,
        "case_count": TOTAL_CASES,
        "new_paid_requests": paid,
        "imported_paid_requests": imported,
        "distinct_system_fingerprints": sorted(fingerprints),
        "totals": result_totals(results),
        "cases": cases_out,
        "protected_state_verified_unchanged": True,
    }
    results_path = output_root / FINAL_RESULTS_FILENAME
    corpus_path = output_root / FINAL_CORPUS_FILENAME
    _write_json_exclusive(results_path, results_document)
    _write_exclusive(corpus_path, ("\n".join(corpus) + "\n").encode("utf-8"))
    final = {
        "schema_version": ARM_SCHEMA_VERSION,
        "kind": ARM_KIND,
        "status": "finalized",
        "arm_id": plan["arm_id"],
        "arm_plan_sha256": plan_sha,
        "finalized_at_utc": now().isoformat(),
        "case_count": TOTAL_CASES,
        "new_paid_requests": paid,
        "imported_paid_requests": imported,
        "results_sha256": _sha256_file(results_path),
        "corpus_sha256": _sha256_file(corpus_path),
        "distinct_system_fingerprints": sorted(fingerprints),
        "protected_state_verified_unchanged": True,
    }
    _write_json_exclusive(output_root / FINAL_MANIFEST_FILENAME, final)
    _append_journal(output_root / JOURNAL_FILENAME, {
        "event": "finalized",
        "at_utc": now().isoformat(),
        "arm_id": plan["arm_id"],
        "results_sha256": final["results_sha256"],
        "corpus_sha256": final["corpus_sha256"],
    })
    return final


def build_parser():
    parser = argparse.ArgumentParser(description="DeepSeek V4.1 Flash 60-case benchmark arm")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--expected-repo-head", required=True)
    p.add_argument("--output-root", required=True)
    p.add_argument("--great-king-plan", required=True)
    p.add_argument("--great-king-plan-sha256", required=True)
    p.add_argument("--rogue-plan", required=True)
    p.add_argument("--rogue-plan-sha256", required=True)
    p.add_argument("--smoke-dir", required=True)
    p.add_argument("--expected-smoke-identity-sha256", required=True)
    p.add_argument("--expected-smoke-translation-sha256", required=True)
    for name in ("status", "execute-next", "finalize"):
        q = sub.add_parser(name)
        q.add_argument("--repo-root", required=True)
        q.add_argument("--expected-repo-head", required=True)
        q.add_argument("--output-root", required=True)
        q.add_argument("--expected-arm-plan-sha256", required=True)
        if name == "execute-next":
            q.add_argument("--confirmation", required=True)
    return parser


def _print_status(status):
    c = status["counts"]
    print("=" * 70)
    print("DEEPSEEK V4.1 FLASH 60-CASE ARM STATUS")
    print("=" * 70)
    print(f"Imported complete : {c['imported']}")
    print(f"Paid complete     : {c['complete']}")
    print(f"Invalid complete  : {c['invalid_complete']}")
    print(f"Planned           : {c['planned']}")
    print(f"Uncertain         : {c['uncertain']}")
    print(f"Response failed   : {c['response_failed']}")
    print(f"Completed total   : {status['completed_total']}/{TOTAL_CASES}")
    print(f"Arm blocked       : {status['blocked']}")
    case = status["next_case"]
    print("Next case         : NONE" if case is None else
          f"Next case         : {case['case_id']} ({case['novel_id']} / Chapter {case['chapter_number']})")
    print("Provider calls    : 0 (status command)")
    print("Paid requests     : 0 (status command)")
    print("=" * 70)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            doc = prepare_arm(
                repo_root=Path(args.repo_root), expected_repo_head=args.expected_repo_head,
                output_root=Path(args.output_root),
                specs=(("great_king_zhuxia", Path(args.great_king_plan), args.great_king_plan_sha256),
                       ("rogue_first_prince", Path(args.rogue_plan), args.rogue_plan_sha256)),
                smoke_dir=Path(args.smoke_dir),
                expected_smoke_identity=args.expected_smoke_identity_sha256,
                expected_smoke_translation=args.expected_smoke_translation_sha256,
            )
            print("=" * 70)
            print("DEEPSEEK V4.1 FLASH 60-CASE ARM PREPARED — ZERO PROVIDER")
            print("=" * 70)
            print(f"Arm ID SHA256   : {doc['arm_id']}")
            print(f"Arm plan SHA256 : {doc['plan_sha256']}")
            print(f"Imported case   : {doc['imported_case_id']}")
            print(f"Pending paid    : {doc['pending_paid']}")
            print(f"Output root     : {doc['output_root']}")
            print("Provider calls  : 0")
            print("Paid requests   : 0")
            print("=" * 70)
            return 0
        plan, root, plan_sha = _load_arm(
            Path(args.repo_root), args.expected_repo_head, Path(args.output_root),
            args.expected_arm_plan_sha256,
        )
        if args.command == "status":
            _print_status(arm_status(plan, root))
            return 0
        if args.command == "execute-next":
            case, result = execute_next(
                repo_root=Path(args.repo_root), expected_repo_head=args.expected_repo_head,
                output_root=root, expected_plan_sha=plan_sha,
                confirmation=args.confirmation,
            )
            provider = result["provider_response"]
            data = result["result"]
            print("=" * 70)
            print("DEEPSEEK V4.1 FLASH ARM CASE: COMPLETE")
            print("=" * 70)
            print(f"Case ID            : {case['case_id']}")
            print(f"Novel              : {case['novel_id']}")
            print(f"Chapter            : {case['chapter_number']}")
            print(f"Response model     : {provider['response_model']}")
            print(f"System fingerprint : {provider['system_fingerprint']}")
            print(f"Finish reason      : {provider['finish_reason']}")
            print(f"Prompt tokens      : {data['prompt_tokens']}")
            print(f"Completion tokens  : {data['completion_tokens']}")
            print(f"Cost USD           : {data['total_cost_usd']}")
            print(f"Residual CJK chars : {data['remaining_cjk_character_count']}")
            print("Provider calls     : 1")
            print("Paid requests      : 1")
            print("Automatic retry    : FORBIDDEN")
            print("=" * 70)
            return 0
        if args.command == "finalize":
            doc = finalize_arm(
                repo_root=Path(args.repo_root), expected_repo_head=args.expected_repo_head,
                output_root=root, expected_plan_sha=plan_sha,
            )
            print("=" * 70)
            print("DEEPSEEK V4.1 FLASH 60-CASE ARM: FINALIZED")
            print("=" * 70)
            print(f"Arm ID SHA256     : {doc['arm_id']}")
            print(f"Arm plan SHA256   : {doc['arm_plan_sha256']}")
            print(f"Results SHA256    : {doc['results_sha256']}")
            print(f"Corpus SHA256     : {doc['corpus_sha256']}")
            print(f"New paid requests : {doc['new_paid_requests']}")
            print(f"Imported requests : {doc['imported_paid_requests']}")
            print(f"Fingerprints      : {', '.join(doc['distinct_system_fingerprints'])}")
            print("Provider calls    : 0 (finalize command)")
            print("Paid requests     : 0 (finalize command)")
            print("=" * 70)
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
