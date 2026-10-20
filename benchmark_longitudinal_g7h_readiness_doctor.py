"""G7H zero-provider Stage-1 readiness doctor.

G7H replaces repeated one-off operator readiness harnesses with one reusable,
fail-closed, read-only Python doctor for the frozen G7F/G7G Stage-1 benchmark
execution path.

Importing this module performs no filesystem writes, reads no credential, loads
no local environment file, constructs no Provider client, makes zero network
calls, and authorizes nothing. Everything happens only when the CLI or
:func:`run_readiness` is invoked explicitly.

The readiness operation is READ-ONLY with respect to production, the GOAT
benchmark root, the Stage-1 root, the invocation plan, the active credential,
the legacy credential backup, the paid-request journal, the accounting ledger,
and the repository. The only permitted write is the explicit JSON evidence path
supplied through ``--json-output``.

The doctor never performs translation inference, never authorizes a paid run,
never constructs a Provider client, never advances Chapter 1, and never mutates
benchmark state. It calls only the existing zero-Provider G7G preflight and
requires its exact fail-closed unauthorized result. The G7G paid-execution entry
point is never called, and no operator-authorization or paid-authorization flag
is ever set true anywhere in this module.

Credential handling follows the G7G Windows DPAPI boundary. The active
credential is validated for local readability through the existing G7G secure
reader, which materializes the key in memory; the returned value is discarded
immediately after validation and is never printed, hashed, written, reported,
length-reported, prefixed, or embedded in JSON or in an exception. Only
encrypted-file metadata (path, encrypted byte count, encrypted SHA-256) is
reported. The legacy backup is never decrypted; only its encrypted byte count
and encrypted SHA-256 are validated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7g_stage1_secure_launcher as g7g
from benchmark_longitudinal_g7c_stage1_integration import stage1_run_specs
from production_paid_request_journal import inspect_paid_request_journals


G7H_SCHEMA_VERSION = 1
G7H_REVISION = "goat-g7h-zero-provider-readiness-doctor-v1"

READINESS_OK_STATUS = "READINESS_OK"
REQUIRED_PREFLIGHT_STATUS = "PREFLIGHT_OK"

RAW_READINESS_PASS = "G7H_READINESS=PASS"
RAW_READINESS_FAIL = "G7H_READINESS=FAIL"
RAW_PROVIDER_CALLS = "BENCHMARK_PROVIDER_CALLS=0"
RAW_PAID_AUTHORIZED = "PAID_EXECUTION_AUTHORIZED=NO"
RAW_CHAPTER1 = "CHAPTER_1_EXECUTED=NO"

RUN_INDEX = 1
TARGET_CHAPTER = 1

G7G_SOURCE_FILENAME = "benchmark_longitudinal_g7g_stage1_secure_launcher.py"
G7F_SOURCE_FILENAME = "benchmark_longitudinal_g7f_stage1_paid_executor.py"

JOURNAL_SUBDIRECTORY = ("data", "provider_request_journal")
CHAPTER_OUTPUT_SUBDIRECTORIES = ("output/chapters", "output/pending")

ABSENT = "ABSENT"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class G7HReadinessError(RuntimeError):
    """G7H readiness state is malformed, drifted, or unsafe to approve."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7HReadinessError(f"{label} must be a lowercase SHA-256")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G7HReadinessError(f"{label} must be a positive integer")
    return value


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _tree_rows(root: Path | str) -> tuple[Path, list[dict[str, Any]]]:
    """Collect one deterministic file inventory for a directory tree.

    Path representation reproduces the frozen Windows evidence-tree fingerprint
    this project already uses: files are ordered by resolved path and each row
    is ``relative-posix-path<TAB>size<TAB>sha256``. Do not switch the path
    separators or the column order here; either changes the aggregate SHA while
    file bytes remain identical.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise G7HReadinessError(f"tree root is not a directory: {root_path}")

    rows: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in root_path.rglob("*") if item.is_file()),
        key=lambda item: str(item.resolve()),
    ):
        resolved = path.resolve()
        rows.append(
            {
                "relative_posix": resolved.relative_to(root_path).as_posix(),
                "sha256": _sha256_file(resolved),
                "size": resolved.stat().st_size,
            }
        )
    return root_path, rows


def stage1_tree_fingerprint(root: Path | str) -> dict[str, Any]:
    """Return the frozen deterministic byte-tree fingerprint of the Stage-1 root."""
    root_path, rows = _tree_rows(root)
    return {
        "root": str(root_path),
        "file_count": len(rows),
        "fingerprint": _sha256_text(
            "\n".join(
                f"{row['relative_posix']}\t{row['size']}\t{row['sha256']}"
                for row in rows
            )
        ),
    }


def count_chapter_pending_outputs(root: Path | str) -> int:
    """Count committed chapter or pending translation files anywhere below root."""
    _root_path, rows = _tree_rows(root)
    total = 0
    for row in rows:
        lowered = f"/{row['relative_posix']}"
        if any(
            f"/{subdirectory}/" in lowered
            for subdirectory in CHAPTER_OUTPUT_SUBDIRECTORIES
        ):
            total += 1
    return total


def _stage1_run_spec(run_index: int) -> dict[str, Any]:
    try:
        specs = stage1_run_specs()
    except Exception:
        raise G7HReadinessError(
            "Stage-1 run specifications are unavailable"
        ) from None
    run_index = _require_positive_int(run_index, "run_index")
    if run_index > len(specs):
        raise G7HReadinessError(f"run_index must be within 1..{len(specs)}")
    return specs[run_index - 1]


def _stage1_run_root(
    stage1_root: Path | str,
    run_index: int,
) -> tuple[Path, str]:
    root = Path(stage1_root).resolve()
    if not root.is_dir():
        raise G7HReadinessError(f"Stage-1 root missing: {root}")

    relative_root = _stage1_run_spec(run_index)["relative_root"]
    run_root = (root / Path(relative_root)).resolve()
    if not _path_is_within(run_root, root):
        raise G7HReadinessError(
            "selected Stage-1 run root escapes the Stage-1 root"
        )
    if not run_root.is_dir():
        raise G7HReadinessError(f"selected Stage-1 run root missing: {run_root}")
    return run_root, relative_root


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise G7HReadinessError(f"cannot read {label}: {path}") from None
    if not isinstance(value, dict):
        raise G7HReadinessError(f"{label} must contain a JSON object: {path}")
    return value


def _load_plan_object(plan_path: Path | str) -> dict[str, Any]:
    return _read_object(Path(plan_path), "G7F invocation plan")


def _verify_source_identity(
    code_root: Path,
    expected_g7g_sha256: Any,
    expected_g7f_sha256: Any,
) -> dict[str, Any]:
    g7g_source = code_root / G7G_SOURCE_FILENAME
    g7f_source = code_root / G7F_SOURCE_FILENAME
    if not g7g_source.is_file():
        raise G7HReadinessError(f"G7G launcher source missing: {g7g_source}")
    if not g7f_source.is_file():
        raise G7HReadinessError(f"G7F executor source missing: {g7f_source}")

    expected_g7g = _require_sha256(expected_g7g_sha256, "expected-g7g-sha256")
    expected_g7f = _require_sha256(expected_g7f_sha256, "expected-g7f-sha256")

    observed_g7g = _sha256_file(g7g_source)
    if observed_g7g != expected_g7g:
        raise G7HReadinessError("G7G secure-launcher source SHA-256 changed")

    observed_g7f = _sha256_file(g7f_source)
    if observed_g7f != expected_g7f:
        raise G7HReadinessError("G7F paid-executor source SHA-256 changed")

    return {
        "g7g_source_sha256": observed_g7g,
        "g7f_source_sha256": observed_g7f,
        "g7f_executor_sha256": g7f.G7F_EXECUTOR_SHA256,
        "g7f_revision": g7f.G7F_REVISION,
    }


def _validate_plan_identity(
    plan: Mapping[str, Any],
    *,
    expected_plan_sha256: Any,
    expected_approval_token: Any,
    expected_relative_root: str,
) -> None:
    wanted_sha = _require_sha256(expected_plan_sha256, "expected-plan-sha256")
    wanted_token = _require_sha256(
        expected_approval_token,
        "expected-approval-token",
    )

    if plan.get("plan_sha256") != wanted_sha:
        raise G7HReadinessError("invocation plan SHA-256 does not match")

    stored_token = plan.get("approval_token")
    if stored_token != wanted_token:
        raise G7HReadinessError("invocation approval token does not match")

    if plan.get("run_index") != RUN_INDEX:
        raise G7HReadinessError("invocation run index is not the frozen Run 1")

    if plan.get("target_chapter") != TARGET_CHAPTER:
        raise G7HReadinessError(
            "invocation target chapter is not the frozen Chapter 1"
        )

    if plan.get("relative_root") != expected_relative_root:
        raise G7HReadinessError(
            "invocation relative root is not the frozen Run 1 root"
        )

    plan_executor = plan.get("g7f_executor_sha256")
    if plan_executor is not None and plan_executor != g7f.G7F_EXECUTOR_SHA256:
        raise G7HReadinessError(
            "invocation plan binds another repository G7F executor identity"
        )


def _read_run_progress(run_root: Path) -> dict[str, Any]:
    return _read_object(run_root / "data" / "progress.json", "Stage-1 progress")


def _inspect_paid_journal(run_root: Path) -> str:
    journal_dir = run_root.joinpath(*JOURNAL_SUBDIRECTORY)
    try:
        inspection = inspect_paid_request_journals(journal_dir)
    except Exception:
        raise G7HReadinessError(
            "paid request journal could not be inspected read-only"
        ) from None
    if not isinstance(inspection, Mapping):
        raise G7HReadinessError(
            "paid request journal inspection returned no mapping"
        )

    errors = inspection.get("errors") or []
    uncertain = inspection.get("uncertain") or []
    records = inspection.get("records") or []

    if errors:
        raise G7HReadinessError(
            "malformed paid request journal state blocks readiness"
        )
    if uncertain or any(
        isinstance(record, Mapping)
        and record.get("status") in {"started", "uncertain"}
        for record in records
    ):
        raise G7HReadinessError(
            "ambiguous paid request journal state (started/uncertain) "
            "blocks readiness"
        )
    if records:
        raise G7HReadinessError(
            "unexpected pre-existing paid request journal before Chapter 1"
        )
    return ABSENT


def _validate_ledger_document(path: Path) -> None:
    document = _read_object(path, "G7F invocation budget ledger")
    if document.get("schema_version") != g7f.LEDGER_SCHEMA_VERSION:
        raise G7HReadinessError("malformed accounting ledger schema")
    if document.get("revision") != g7f.LEDGER_REVISION:
        raise G7HReadinessError("malformed accounting ledger revision")
    stored = document.get("ledger_sha256")
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7HReadinessError("malformed accounting ledger SHA-256")
    if g7f._ledger_hash(document) != stored:
        raise G7HReadinessError("malformed accounting ledger SHA-256")


def _inspect_paid_ledger(run_root: Path) -> str:
    ledger_dir = run_root / Path(g7f.LEDGER_DIRECTORY)
    if not ledger_dir.exists():
        return ABSENT
    if not ledger_dir.is_dir():
        raise G7HReadinessError(
            "G7F invocation budget ledger path is not a directory"
        )

    files = sorted(
        (item for item in ledger_dir.rglob("*") if item.is_file()),
        key=lambda item: item.as_posix(),
    )
    if not files:
        return ABSENT

    for path in files:
        try:
            _validate_ledger_document(path)
        except G7HReadinessError:
            raise G7HReadinessError(
                "malformed accounting ledger blocks readiness"
            ) from None

    raise G7HReadinessError(
        "unexpected pre-existing G7F invocation budget ledger before Chapter 1"
    )


def _default_preflight_runner(**kwargs: Any) -> Mapping[str, Any]:
    return g7g.run_preflight(**kwargs)


def _run_preflight(
    *,
    runner: Callable[..., Mapping[str, Any]],
    code_root: Path,
    stage1_root: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    expected_approval_token: str,
) -> dict[str, Any]:
    try:
        result = runner(
            code_root=code_root,
            stage1_root=stage1_root,
            plan_path=plan_path,
            expected_plan_sha256=expected_plan_sha256,
            expected_approval_token=expected_approval_token,
        )
    except G7HReadinessError:
        raise
    except BaseException as exc:
        raise G7HReadinessError(
            "G7G zero-provider preflight failed: "
            f"{type(exc).__name__}"
        ) from None

    if not isinstance(result, Mapping):
        raise G7HReadinessError("G7G preflight returned no result mapping")
    if result.get("status") != REQUIRED_PREFLIGHT_STATUS:
        raise G7HReadinessError("G7G preflight status is not PREFLIGHT_OK")
    if result.get("authorization_required") is not True:
        raise G7HReadinessError(
            "G7G preflight did not require operator authorization"
        )
    if result.get("paid_execution_authorized") is not False:
        raise G7HReadinessError(
            "G7G preflight did not fail closed on paid authorization"
        )
    if result.get("provider_calls") != 0:
        raise G7HReadinessError("G7G preflight reported Provider calls")
    if result.get("run_index") != RUN_INDEX:
        raise G7HReadinessError("G7G preflight run index is not Run 1")
    if result.get("target_chapter") != TARGET_CHAPTER:
        raise G7HReadinessError("G7G preflight target chapter is not Chapter 1")
    if result.get("plan_sha256") != expected_plan_sha256:
        raise G7HReadinessError("G7G preflight plan SHA-256 does not match")
    if result.get("approval_token") != expected_approval_token:
        raise G7HReadinessError("G7G preflight approval token does not match")

    return {
        "status": REQUIRED_PREFLIGHT_STATUS,
        "authorization_required": True,
        "paid_execution_authorized": False,
        "provider_calls": 0,
        "run_index": RUN_INDEX,
        "target_chapter": TARGET_CHAPTER,
        "plan_sha256": expected_plan_sha256,
        "approval_token": expected_approval_token,
    }


def _encrypted_file_identity(
    path: Path | str,
    label: str,
    *,
    require_safe_target: bool = True,
) -> dict[str, Any]:
    """Return encrypted-file metadata only; never decrypt or report plaintext."""
    target = Path(path)
    if require_safe_target:
        try:
            target = g7g.assert_safe_secret_target(target)
        except g7g.G7GSecureLauncherError:
            raise G7HReadinessError(f"unsafe {label} path") from None
    if not target.is_file():
        raise G7HReadinessError(f"{label} is missing: {target}")

    try:
        payload = target.read_bytes()
    except OSError:
        raise G7HReadinessError(f"cannot read {label}") from None
    if not payload:
        raise G7HReadinessError(f"{label} is empty")

    return {
        "path": str(target),
        "encrypted_bytes": len(payload),
        "sha256": _sha256_bytes(payload),
    }


def _assert_active_secret_readable(path: Path | str, dpapi: Any) -> None:
    """Validate readability through the G7G secure reader, then discard it."""
    try:
        materialized = g7g._read_secret_key(path, dpapi)
    except g7g.G7GSecureLauncherError:
        raise G7HReadinessError(
            "active credential is not readable through the G7G secure reader"
        ) from None
    except BaseException as exc:
        raise G7HReadinessError(
            "active credential readability check failed: "
            f"{type(exc).__name__}"
        ) from None

    if not isinstance(materialized, str) or not materialized.strip():
        raise G7HReadinessError(
            "active credential is not readable through the G7G secure reader"
        )
    # The plaintext is never printed, hashed, written, reported, or embedded.
    del materialized


def run_readiness(
    *,
    code_root: Path | str,
    stage1_root: Path | str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_approval_token: str,
    secret_path: Path | str,
    expected_secret_sha256: str,
    expected_secret_bytes: int,
    legacy_backup: Path | str,
    expected_legacy_sha256: str,
    expected_legacy_bytes: int,
    expected_stage1_files: int,
    expected_stage1_fingerprint: str,
    expected_g7g_sha256: str,
    expected_g7f_sha256: str,
    preflight_runner: Callable[..., Mapping[str, Any]] | None = None,
    dpapi: Any | None = None,
    platform_resolver: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the read-only Stage-1 readiness validation and return evidence.

    This function performs no writes, constructs no Provider client, and never
    authorizes paid work. Callers persist the returned evidence themselves.
    """
    code = Path(code_root).resolve()
    if not code.is_dir():
        raise G7HReadinessError(f"code root is not a directory: {code}")

    source_identity = _verify_source_identity(
        code,
        expected_g7g_sha256,
        expected_g7f_sha256,
    )

    stage1_path = Path(stage1_root).resolve()
    stage1_identity = stage1_tree_fingerprint(stage1_path)
    expected_files = _require_positive_int(
        expected_stage1_files,
        "expected-stage1-files",
    )
    expected_fingerprint = _require_sha256(
        expected_stage1_fingerprint,
        "expected-stage1-fingerprint",
    )
    if stage1_identity["file_count"] != expected_files:
        raise G7HReadinessError(
            "Stage-1 file count drifted: "
            f"{stage1_identity['file_count']} != {expected_files}"
        )
    if stage1_identity["fingerprint"] != expected_fingerprint:
        raise G7HReadinessError(
            "Stage-1 deterministic fingerprint drifted"
        )

    chapter_pending_outputs = count_chapter_pending_outputs(stage1_path)
    if chapter_pending_outputs != 0:
        raise G7HReadinessError(
            "unexpected chapter or pending output before Chapter 1: "
            f"{chapter_pending_outputs}"
        )

    run_root, relative_root = _stage1_run_root(stage1_path, RUN_INDEX)
    progress = _read_run_progress(run_root)
    if progress.get("last_completed") != 0 or progress.get("next_chapter") != 1:
        raise G7HReadinessError(
            "Run 1 progress is not the pristine pre-Chapter-1 state"
        )

    journal_state = _inspect_paid_journal(run_root)
    ledger_state = _inspect_paid_ledger(run_root)

    plan_file = Path(plan_path)
    plan = _load_plan_object(plan_file)
    _validate_plan_identity(
        plan,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
        expected_relative_root=relative_root,
    )

    preflight = _run_preflight(
        runner=(
            preflight_runner
            if preflight_runner is not None
            else _default_preflight_runner
        ),
        code_root=code,
        stage1_root=stage1_path,
        plan_path=plan_file,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
    )

    resolver = (
        platform_resolver
        if platform_resolver is not None
        else g7g.current_platform_is_supported
    )
    if not resolver():
        raise G7HReadinessError(
            "G7H readiness doctor requires Windows DPAPI (CurrentUser scope)"
        )
    backend = dpapi if dpapi is not None else g7g.load_platform_dpapi()

    active_secret = _encrypted_file_identity(
        secret_path,
        "active credential",
    )
    expected_secret_size = _require_positive_int(
        expected_secret_bytes,
        "expected-secret-bytes",
    )
    if active_secret["encrypted_bytes"] != expected_secret_size:
        raise G7HReadinessError(
            "active credential encrypted byte count changed"
        )
    if active_secret["sha256"] != _require_sha256(
        expected_secret_sha256,
        "expected-secret-sha256",
    ):
        raise G7HReadinessError("active credential encrypted SHA-256 changed")
    _assert_active_secret_readable(secret_path, backend)

    legacy = _encrypted_file_identity(
        legacy_backup,
        "legacy credential backup",
        require_safe_target=False,
    )
    expected_legacy_size = _require_positive_int(
        expected_legacy_bytes,
        "expected-legacy-bytes",
    )
    if legacy["encrypted_bytes"] != expected_legacy_size:
        raise G7HReadinessError(
            "legacy credential backup encrypted byte count changed"
        )
    if legacy["sha256"] != _require_sha256(
        expected_legacy_sha256,
        "expected-legacy-sha256",
    ):
        raise G7HReadinessError(
            "legacy credential backup encrypted SHA-256 changed"
        )

    return {
        "status": READINESS_OK_STATUS,
        "schema_version": G7H_SCHEMA_VERSION,
        "revision": G7H_REVISION,
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "chapter1_executed": False,
        "authorization_required": True,
        "run_index": RUN_INDEX,
        "target_chapter": TARGET_CHAPTER,
        "plan_sha256": expected_plan_sha256,
        "approval_token": expected_approval_token,
        "source_identity": source_identity,
        "preflight": preflight,
        "stage1": {
            **stage1_identity,
            "root": str(stage1_path),
            "run_root": str(run_root),
            "relative_root": relative_root,
            "chapter_pending_outputs": chapter_pending_outputs,
            "progress": {
                "last_completed": progress.get("last_completed"),
                "next_chapter": progress.get("next_chapter"),
            },
        },
        "active_secret": {
            "readable": True,
            "encrypted_bytes": active_secret["encrypted_bytes"],
            "sha256": active_secret["sha256"],
            "path": active_secret["path"],
        },
        "legacy_backup": {
            "decrypted": False,
            "encrypted_bytes": legacy["encrypted_bytes"],
            "sha256": legacy["sha256"],
            "path": legacy["path"],
        },
        "paid_artifacts": {
            "journal": journal_state,
            "ledger": ledger_state,
            "run_root": str(run_root),
        },
    }


def render_human_summary(report: Mapping[str, Any]) -> str:
    """Render a concise sanitized human summary that never shows a credential."""
    stage1 = report.get("stage1") or {}
    paid_artifacts = report.get("paid_artifacts") or {}
    lines = [
        RAW_READINESS_PASS,
        RAW_PROVIDER_CALLS,
        RAW_PAID_AUTHORIZED,
        RAW_CHAPTER1,
        f"REVISION={report.get('revision')}",
        f"PLAN_SHA256={report.get('plan_sha256')}",
        f"APPROVAL_TOKEN={report.get('approval_token')}",
        f"RUN_INDEX={report.get('run_index')}",
        f"TARGET_CHAPTER={report.get('target_chapter')}",
        f"STAGE1_FILE_COUNT={stage1.get('file_count')}",
        f"STAGE1_FINGERPRINT={stage1.get('fingerprint')}",
        f"CHAPTER_PENDING_OUTPUTS={stage1.get('chapter_pending_outputs')}",
        "ACTIVE_SECRET_READABLE=True",
        "ACTIVE_SECRET_SHA256="
        f"{(report.get('active_secret') or {}).get('sha256')}",
        "LEGACY_BACKUP_DECRYPTED=False",
        "LEGACY_BACKUP_SHA256="
        f"{(report.get('legacy_backup') or {}).get('sha256')}",
        f"PAID_JOURNAL={paid_artifacts.get('journal')}",
        f"PAID_LEDGER={paid_artifacts.get('ledger')}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7h-readiness-doctor",
        description=(
            "Read-only zero-provider readiness doctor for the frozen "
            "G7F/G7G Stage-1 benchmark execution path"
        ),
    )
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--stage1-root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--expected-approval-token", required=True)
    parser.add_argument("--secret-path", required=True)
    parser.add_argument("--expected-secret-sha256", required=True)
    parser.add_argument("--expected-secret-bytes", type=int, required=True)
    parser.add_argument("--legacy-backup", required=True)
    parser.add_argument("--expected-legacy-sha256", required=True)
    parser.add_argument("--expected-legacy-bytes", type=int, required=True)
    parser.add_argument("--expected-stage1-files", type=int, required=True)
    parser.add_argument("--expected-stage1-fingerprint", required=True)
    parser.add_argument("--expected-g7g-sha256", required=True)
    parser.add_argument("--expected-g7f-sha256", required=True)
    parser.add_argument("--json-output", required=True)
    return parser


def _write_json_report(path: Path | str, report: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            dict(report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    target.write_text(payload, encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_readiness(
            code_root=args.code_root,
            stage1_root=args.stage1_root,
            plan_path=args.plan,
            expected_plan_sha256=args.expected_plan_sha256,
            expected_approval_token=args.expected_approval_token,
            secret_path=args.secret_path,
            expected_secret_sha256=args.expected_secret_sha256,
            expected_secret_bytes=args.expected_secret_bytes,
            legacy_backup=args.legacy_backup,
            expected_legacy_sha256=args.expected_legacy_sha256,
            expected_legacy_bytes=args.expected_legacy_bytes,
            expected_stage1_files=args.expected_stage1_files,
            expected_stage1_fingerprint=(
                args.expected_stage1_fingerprint
            ),
            expected_g7g_sha256=args.expected_g7g_sha256,
            expected_g7f_sha256=args.expected_g7f_sha256,
        )
    except G7HReadinessError as exc:
        print(RAW_READINESS_FAIL)
        print(RAW_PROVIDER_CALLS)
        print(RAW_PAID_AUTHORIZED)
        print(RAW_CHAPTER1)
        print(f"G7H_BLOCKER={exc}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        print(RAW_READINESS_FAIL)
        print(RAW_PROVIDER_CALLS)
        print(RAW_PAID_AUTHORIZED)
        print(RAW_CHAPTER1)
        print(f"G7H_BLOCKER={type(exc).__name__}")
        return 1

    written = _write_json_report(args.json_output, report)
    print(render_human_summary(report))
    print(f"JSON_OUTPUT={written}")
    return 0


__all__ = [
    "ABSENT",
    "G7HReadinessError",
    "G7H_REVISION",
    "G7H_SCHEMA_VERSION",
    "RAW_CHAPTER1",
    "RAW_PAID_AUTHORIZED",
    "RAW_PROVIDER_CALLS",
    "RAW_READINESS_FAIL",
    "RAW_READINESS_PASS",
    "READINESS_OK_STATUS",
    "RUN_INDEX",
    "TARGET_CHAPTER",
    "build_parser",
    "count_chapter_pending_outputs",
    "main",
    "render_human_summary",
    "run_readiness",
    "stage1_tree_fingerprint",
]


if __name__ == "__main__":
    raise SystemExit(main())
