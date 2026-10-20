"""G7F reviewed Stage-1 paid executor implementation.

G7F composes the frozen G7C integration, G7D accounting, and G7E
authorization contracts into one fail-closed executor boundary. Importing this
module performs no filesystem writes, credential reads, Provider calls, or
client construction.

The Command Code coding agent is not part of benchmark inference. Real
inference uses the direct Command Code Provider OpenAI-compatible transport
from benchmark_longitudinal_provider and always runs underneath the existing
durable paid-request journal.

Nothing in this module authorizes paid work by itself. A paid invocation still
requires a materialized plan, its exact G7F plan SHA, its exact G7E approval
token, an unchanged pre-invocation state fingerprint, finite persisted
send/cost ceilings, and a separate explicit operator authorization flag.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from benchmark_longitudinal_config import PROVIDER_NAME
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_g7c_stage1_integration import (
    CONTEXT_ENTRYPOINTS,
    DIRECT_LEGACY_OPERATION_MAP,
    G7C_INTEGRATION_SHA256,
    RUN_IDENTITY_FILENAME,
    normalize_legacy_request,
    stage1_run_specs,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    G7D_ACCOUNTING_SHA256,
    PRICING_VERSION,
    account_provider_usage,
    pricing_snapshot as g7d_pricing_snapshot,
)
from benchmark_longitudinal_g7e_paid_authorization import (
    G7E_AUTHORIZATION_SHA256,
    assert_contract_ready_for_invocation_planning,
    build_invocation_authorization,
    classify_chapter_resume_state,
    g7e_authorization_identity,
    verify_invocation_authorization,
)
from benchmark_longitudinal_g7j_reasoning_budget import (
    G7J_AUXILIARY_8K_POLICY_SHA256,
    G7JPolicyError,
    G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256,
    GOAT_AUXILIARY_8K_REVISION,
    GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS,
    GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION,
    GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS,
    PRIMARY_TRANSLATION_OPERATION,
    REASONING_DIAGNOSTIC_CLASSIFICATIONS,
    REASONING_DIAGNOSTIC_OK,
    STATUS_OK,
    TRANSLATION_RESPONSE_STATUSES,
    augment_accounting_stats_with_reasoning,
    classify_auxiliary_response,
    classify_translation_response,
    goat_auxiliary_completion_budget,
    goat_auxiliary_stage1_runtime_completion_budget,
    goat_auxiliary_stage1_runtime_completion_budget_for_identity,
    reasoning_diagnostic_classification,
    reasoning_diagnostic_outcome,
    reasoning_usage_diagnostics,
    translation_completion_budget,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)
from full_pipeline_luna_runtime import (
    initialize_engine_without_provider,
    load_experiment_engine_module,
)
from novel_project import load_novel_config
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    inspect_paid_request_journals,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from production_run_lock import production_run_lock


G7F_SCHEMA_VERSION = 1
# Frozen G7F identity: G7J extends the executor's translation-response policy
# without changing the executor contract identity, plan envelope, ledger schema,
# or any predecessor binding. Keep this revision exactly frozen.
G7F_REVISION = "goat-g7f-stage1-paid-executor-v2"
G7F_REPOSITORY_ANCHOR = "8184eb05d4288099516a43261e400addffd5defb"
EXPECTED_G7C_INTEGRATION_SHA256 = (
    "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
)
EXPECTED_G7D_ACCOUNTING_SHA256 = (
    "1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0"
)
EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256 = (
    "35745ce2ba6ff8112752d6642365ede875ad4cc603336f5feb4b40d946c3edf5"
)
STAGE1_ROOTS_MANIFEST_FILENAME = "g7c_stage1_roots_manifest.json"
PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
STATE_FINGERPRINT_REVISION = "g7f-stage1-state-fingerprint-v1"
LEDGER_SCHEMA_VERSION = 1
LEDGER_REVISION = "g7f-persisted-invocation-budget-v1"
LEDGER_DIRECTORY = "data/g7f_invocation_ledgers"
PROMPT_TOKEN_SAFETY_OVERHEAD = 8192

_ALLOWED_OPERATIONS = frozenset(DIRECT_LEGACY_OPERATION_MAP.values()) | frozenset(
    CONTEXT_ENTRYPOINTS.values()
)
_EXCLUDED_FINGERPRINT_PREFIXES = (
    "data/provider_request_journal/",
    f"{LEDGER_DIRECTORY}/",
    "logs/",
)
_EXCLUDED_FINGERPRINT_FILES = frozenset({".production.lock"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Distinguishes an explicitly absent durable field from a present ``None``.
_ABSENT = object()


class G7FExecutionError(RuntimeError):
    """G7F state, authorization, or execution is unsafe."""


class G7FBudgetError(G7FExecutionError):
    """A persisted invocation budget cannot safely authorize another send."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7FExecutionError(
            "G7F value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7FExecutionError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise G7FExecutionError(
            f"{label} must contain a JSON object: {path}"
        )
    return value


def _staged_source_type(config_path: Path) -> str:
    """Resolve staged source type using the canonical novel-config defaults."""
    try:
        return load_novel_config(config_path).source_type
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise G7FExecutionError(
            "staged novel config is invalid"
        ) from exc


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7FExecutionError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise G7FExecutionError(f"{label} must be a positive integer")
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G7FExecutionError(
            f"{label} must be a positive finite number"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G7FExecutionError(
            f"{label} must be a positive finite number"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G7FExecutionError(
            f"{label} must be a positive finite number"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G7FExecutionError(
            f"Stage-1 run root must not be a symlink: {root}"
        )
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G7FExecutionError(
                f"Stage-1 run contains a symlink: {item}"
            )


def _manifest_identity(stage1_root: Path) -> dict[str, Any]:
    manifest = _read_object(
        stage1_root / STAGE1_ROOTS_MANIFEST_FILENAME,
        "G7C Stage-1 roots manifest",
    )
    check = copy.deepcopy(manifest)
    stored = check.pop("manifest_sha256", None)
    if _sha256_json(check) != stored:
        raise G7FExecutionError(
            "Stage-1 roots manifest SHA does not recompute"
        )
    if stored != EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256:
        raise G7FExecutionError(
            "Stage-1 roots manifest is not the frozen G7C artifact"
        )
    if (
        manifest.get("g7c_integration_sha256")
        != EXPECTED_G7C_INTEGRATION_SHA256
    ):
        raise G7FExecutionError(
            "Stage-1 roots bind another G7C integration"
        )
    specs = stage1_run_specs()
    if manifest.get("run_count") != len(specs):
        raise G7FExecutionError("Stage-1 run count changed")
    return manifest


def _validate_run_binding(
    stage1_root: Path | str,
    run_index: int,
) -> dict[str, Any]:
    root = Path(stage1_root).resolve()
    if not root.is_dir():
        raise G7FExecutionError(f"Stage-1 root missing: {root}")

    run_index = _positive_int(run_index, "run_index")
    specs = stage1_run_specs()
    if run_index > len(specs):
        raise G7FExecutionError(
            f"run_index must be within 1..{len(specs)}"
        )
    spec = copy.deepcopy(specs[run_index - 1])
    manifest = _manifest_identity(root)

    run_root = (root / Path(spec["relative_root"])).resolve()
    if not _path_is_within(run_root, root):
        raise G7FExecutionError(
            "selected run root escapes the Stage-1 root"
        )
    if not run_root.is_dir():
        raise G7FExecutionError(
            f"selected Stage-1 run root missing: {run_root}"
        )
    _assert_no_symlinks(run_root)

    manifest_records = [
        record
        for record in manifest.get("runs", [])
        if isinstance(record, Mapping)
        and record.get("relative_root") == spec["relative_root"]
    ]
    if len(manifest_records) != 1:
        raise G7FExecutionError(
            "selected run is not uniquely bound in the Stage-1 manifest"
        )
    manifest_record = manifest_records[0]

    identity = _read_object(
        run_root / RUN_IDENTITY_FILENAME,
        "G7C run identity",
    )
    identity_check = copy.deepcopy(identity)
    stored_identity_sha = identity_check.pop("identity_sha256", None)
    if _sha256_json(identity_check) != stored_identity_sha:
        raise G7FExecutionError(
            "G7C run identity SHA does not recompute"
        )
    if (
        manifest_record.get("run_identity_sha256")
        != stored_identity_sha
    ):
        raise G7FExecutionError(
            "manifest/run identity SHA binding changed"
        )

    for field in (
        "run_index",
        "model_id",
        "novel_id",
        "run_sha256",
        "run_plan_sha256",
        "s0_identity_sha256",
        "source_sha256",
        "chapter_library_sha256",
        "profile",
        "audit_interval",
        "chapter_numbering",
        "stage1_chapters",
        "stage1_audit_checkpoints",
        "relative_root",
    ):
        if identity.get(field) != spec.get(field):
            raise G7FExecutionError(
                f"selected run identity changed: {field}"
            )

    if identity.get("g7c_integration_sha256") != G7C_INTEGRATION_SHA256:
        raise G7FExecutionError(
            "selected run binds another G7C integration"
        )
    if (
        identity.get("provider_calls") != 0
        or identity.get("state") != "s0_unexecuted"
    ):
        raise G7FExecutionError(
            "selected run static S0 origin identity changed"
        )

    config_path = run_root / "novel.json"
    config = _read_object(config_path, "staged novel config")
    if config.get("id") != spec["novel_id"]:
        raise G7FExecutionError(
            "staged novel id differs from selected run"
        )
    for field in ("profile", "audit_interval", "chapter_numbering"):
        if config.get(field) != spec[field]:
            raise G7FExecutionError(
                f"staged novel config changed: {field}"
            )

    source_type = _staged_source_type(config_path)
    source_path = run_root / "source" / f"novel.{source_type}"
    chapters_path = run_root / "data" / "chapters.json"
    addendum_path = run_root / "translation_addendum.txt"

    if _sha256_file(config_path) != identity.get(
        "source_config_sha256"
    ):
        raise G7FExecutionError(
            "staged novel config bytes changed"
        )
    if _sha256_file(source_path) != spec["source_sha256"]:
        raise G7FExecutionError("staged source bytes changed")
    if (
        _sha256_file(chapters_path)
        != spec["chapter_library_sha256"]
    ):
        raise G7FExecutionError(
            "staged chapter library bytes changed"
        )
    if _sha256_file(addendum_path) != identity.get(
        "source_addendum_sha256"
    ):
        raise G7FExecutionError(
            "staged translation addendum bytes changed"
        )

    return {
        "stage1_root": root,
        "run_root": run_root,
        "spec": spec,
        "manifest": manifest,
        "run_identity": identity,
    }


def state_fingerprint_sha256(run_root: Path | str) -> str:
    """Hash execution state except journal/ledger/log/lock state."""
    root = Path(run_root).resolve()
    if not root.is_dir():
        raise G7FExecutionError(f"run root missing: {root}")
    _assert_no_symlinks(root)

    files = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in _EXCLUDED_FINGERPRINT_FILES:
            continue
        if any(
            relative.startswith(prefix)
            for prefix in _EXCLUDED_FINGERPRINT_PREFIXES
        ):
            continue
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )

    return _sha256_json(
        {
            "revision": STATE_FINGERPRINT_REVISION,
            "files": files,
        }
    )


def g7f_executor_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G7F executor contract."""
    assert_contract_ready_for_invocation_planning()
    g7e = g7e_authorization_identity()

    if G7C_INTEGRATION_SHA256 != EXPECTED_G7C_INTEGRATION_SHA256:
        raise G7FExecutionError("G7C identity changed")
    if G7D_ACCOUNTING_SHA256 != EXPECTED_G7D_ACCOUNTING_SHA256:
        raise G7FExecutionError("G7D identity changed")
    if g7e["provider_calls"] != 0:
        raise G7FExecutionError(
            "G7E unexpectedly made Provider calls"
        )

    return {
        "schema_version": G7F_SCHEMA_VERSION,
        "revision": G7F_REVISION,
        "repository_anchor": G7F_REPOSITORY_ANCHOR,
        "predecessor": {
            "g7c_integration_sha256": (
                EXPECTED_G7C_INTEGRATION_SHA256
            ),
            "g7d_accounting_sha256": (
                EXPECTED_G7D_ACCOUNTING_SHA256
            ),
            "g7e_authorization_sha256": (
                G7E_AUTHORIZATION_SHA256
            ),
            "stage1_roots_manifest_sha256": (
                EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
            ),
        },
        "execution_scope": {
            "one_external_run_per_invocation": True,
            "one_target_chapter_per_invocation": True,
            "run_unattended_production_target_only": True,
            "automatic_chapter_advance": False,
            "provider_transport": (
                "command_code_provider_direct_openai_compatible"
            ),
            "coding_agent_used_for_inference": False,
            "provider_client_max_retries": 0,
            "production_paid_request_journal_is_retry_owner": True,
        },
        "state_fingerprint": {
            "revision": STATE_FINGERPRINT_REVISION,
            "excluded_prefixes": list(
                _EXCLUDED_FINGERPRINT_PREFIXES
            ),
            "excluded_files": sorted(
                _EXCLUDED_FINGERPRINT_FILES
            ),
        },
        "budget": {
            "ledger_revision": LEDGER_REVISION,
            "ledger_per_run_target_chapter": True,
            "restart_must_reuse_ledger": True,
            "new_send_persisted_before_network": True,
            "send_ceiling_checked_before_every_new_send": True,
            "cost_ceiling_checked_before_every_new_send": True,
            "accepted_usage_accounted_by_g7d": True,
            "cost_ceiling_rechecked_after_accepted_usage": True,
            "prompt_token_upper_bound": (
                "serialized_request_utf8_bytes_plus_fixed_safety_overhead"
            ),
            "prompt_token_safety_overhead": (
                PROMPT_TOKEN_SAFETY_OVERHEAD
            ),
        },
        "authorization": {
            "exact_plan_sha_required": True,
            "exact_g7e_approval_token_required": True,
            "explicit_operator_authorization_required": True,
            "credentials_not_stored_in_plan_or_ledger": True,
        },
        "provider_calls": 0,
    }


G7F_EXECUTOR_SHA256 = _sha256_json(g7f_executor_identity())


def _read_progress(run_root: Path) -> dict[str, Any]:
    return _read_object(
        run_root / "data" / "progress.json",
        "Stage-1 progress",
    )


def _journal_inspection(
    run_root: Path,
    target_chapter: int,
) -> dict[str, Any]:
    """Scope replay/planned accounting to one target chapter.

    The generic journal inspector stays whole-directory. Global safety is
    preserved exactly: ``errors`` and ``uncertain`` remain whole-run, and any
    unresolved ``started``/``uncertain`` journal anywhere in the run stays in
    ``records`` so it keeps blocking automatic continuation. Only the target
    chapter's ``response_recorded``/``planned`` records contribute replay and
    planned accounting, so a durable prior-chapter response is never mistaken
    for a target-chapter replay and cannot demand a target-chapter invocation
    ledger that does not exist.
    """
    target = _positive_int(target_chapter, "target_chapter")
    inspection = inspect_paid_request_journals(
        run_root / "data" / "provider_request_journal"
    )
    records = inspection.get("records")
    if not isinstance(records, list):
        return inspection
    scoped = [
        record
        for record in records
        if (
            not isinstance(record, Mapping)
            or record.get("chapter") == target
            or record.get("status") in {"started", "uncertain"}
        )
    ]
    return {**inspection, "records": scoped}


def build_stage1_invocation_plan(
    *,
    stage1_root: Path | str,
    run_index: int,
    target_chapter: int,
    maximum_new_provider_sends: int,
    maximum_credit_equivalent_usd: Any,
) -> dict[str, Any]:
    """Build one zero-Provider, read-only G7F invocation plan."""
    binding = _validate_run_binding(
        stage1_root,
        run_index,
    )
    run_root = binding["run_root"]
    progress = _read_progress(run_root)
    journal_inspection = _journal_inspection(
        run_root,
        target_chapter,
    )
    chapter_state = classify_chapter_resume_state(
        progress=progress,
        target_chapter=target_chapter,
        journal_inspection=journal_inspection,
    )

    if chapter_state["status"] != "ready_or_resumable":
        raise G7FExecutionError(
            "target chapter is not ready/resumable: "
            f"{chapter_state['status']}"
        )

    fingerprint = state_fingerprint_sha256(run_root)
    g7e_plan = build_invocation_authorization(
        run_index=run_index,
        target_chapter=target_chapter,
        pre_invocation_state_fingerprint_sha256=fingerprint,
        maximum_new_provider_sends=maximum_new_provider_sends,
        maximum_credit_equivalent_usd=(
            maximum_credit_equivalent_usd
        ),
    )

    if (
        g7e_plan["relative_root"]
        != binding["spec"]["relative_root"]
    ):
        raise G7FExecutionError(
            "G7E plan selected another Stage-1 run"
        )

    plan = {
        "schema_version": G7F_SCHEMA_VERSION,
        "revision": G7F_REVISION,
        "g7f_executor_sha256": G7F_EXECUTOR_SHA256,
        "stage1_roots_manifest_sha256": (
            EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
        ),
        "run_index": run_index,
        "relative_root": binding["spec"]["relative_root"],
        "target_chapter": target_chapter,
        "pre_invocation_state_fingerprint_sha256": (
            fingerprint
        ),
        "chapter_resume_status": chapter_state["status"],
        "journal_resume_status": (
            chapter_state["journal_state"]["status"]
        ),
        "approval_token": g7e_plan["approval_token"],
        "g7e_plan": g7e_plan,
        "provider_calls": 0,
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def _validate_plan_envelope(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise G7FExecutionError(
            "G7F invocation plan must be a mapping"
        )

    value = copy.deepcopy(dict(plan))
    stored = value.pop("plan_sha256", None)

    if (
        not isinstance(stored, str)
        or not _SHA256_RE.fullmatch(stored)
    ):
        raise G7FExecutionError(
            "G7F invocation plan SHA is invalid"
        )
    if _sha256_json(value) != stored:
        raise G7FExecutionError(
            "G7F invocation plan SHA does not recompute"
        )
    if value.get("schema_version") != G7F_SCHEMA_VERSION:
        raise G7FExecutionError(
            "unsupported G7F invocation plan schema"
        )
    if value.get("revision") != G7F_REVISION:
        raise G7FExecutionError(
            "unsupported G7F invocation plan revision"
        )
    if value.get("g7f_executor_sha256") != G7F_EXECUTOR_SHA256:
        raise G7FExecutionError(
            "invocation plan binds another G7F executor"
        )
    if (
        value.get("stage1_roots_manifest_sha256")
        != EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
    ):
        raise G7FExecutionError(
            "invocation plan binds another Stage-1 root manifest"
        )
    if value.get("provider_calls") != 0:
        raise G7FExecutionError(
            "materialized G7F plan must report zero Provider calls"
        )

    value["plan_sha256"] = stored
    return value


def inspect_stage1_invocation_plan(
    *,
    stage1_root: Path | str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify exact plan/state without pretending operator consent."""
    value = _validate_plan_envelope(plan)
    run_index = _positive_int(
        value.get("run_index"),
        "run_index",
    )
    target = _positive_int(
        value.get("target_chapter"),
        "target_chapter",
    )
    binding = _validate_run_binding(
        stage1_root,
        run_index,
    )

    if value.get("relative_root") != binding["spec"][
        "relative_root"
    ]:
        raise G7FExecutionError(
            "invocation relative root changed"
        )

    current_fingerprint = state_fingerprint_sha256(
        binding["run_root"]
    )
    if (
        value.get("pre_invocation_state_fingerprint_sha256")
        != current_fingerprint
    ):
        raise G7FExecutionError(
            "invocation state fingerprint changed after planning"
        )

    nested = value.get("g7e_plan")
    if not isinstance(nested, Mapping):
        raise G7FExecutionError(
            "G7F plan lacks nested G7E invocation plan"
        )
    nested = copy.deepcopy(dict(nested))

    if value.get("approval_token") != nested.get(
        "approval_token"
    ):
        raise G7FExecutionError(
            "G7F/G7E approval token binding changed"
        )
    if (
        nested.get("run_index") != run_index
        or nested.get("target_chapter") != target
    ):
        raise G7FExecutionError(
            "nested G7E run/target binding changed"
        )
    if nested.get("relative_root") != value.get(
        "relative_root"
    ):
        raise G7FExecutionError(
            "nested G7E relative root changed"
        )
    if (
        nested.get("pre_invocation_state_fingerprint_sha256")
        != current_fingerprint
    ):
        raise G7FExecutionError(
            "nested G7E state fingerprint changed"
        )

    expected_nested = build_invocation_authorization(
        run_index=run_index,
        target_chapter=target,
        pre_invocation_state_fingerprint_sha256=(
            current_fingerprint
        ),
        maximum_new_provider_sends=nested.get(
            "maximum_new_provider_sends"
        ),
        maximum_credit_equivalent_usd=nested.get(
            "maximum_credit_equivalent_usd"
        ),
    )
    if nested != expected_nested:
        raise G7FExecutionError(
            "nested G7E invocation plan does not recompute exactly"
        )

    progress = _read_progress(binding["run_root"])
    journal_inspection = _journal_inspection(
        binding["run_root"],
        target,
    )
    chapter_state = classify_chapter_resume_state(
        progress=progress,
        target_chapter=target,
        journal_inspection=journal_inspection,
    )
    if chapter_state["status"] != "ready_or_resumable":
        raise G7FExecutionError(
            "target chapter stopped being ready/resumable: "
            f"{chapter_state['status']}"
        )

    return {
        "plan": value,
        "binding": binding,
        "current_state_fingerprint_sha256": (
            current_fingerprint
        ),
        "progress": progress,
        "journal_inspection": journal_inspection,
        "chapter_state": chapter_state,
        "provider_calls": 0,
    }


def verify_stage1_invocation_plan(
    *,
    stage1_root: Path | str,
    plan: Mapping[str, Any],
    expected_plan_sha256: str,
    expected_approval_token: str,
    operator_authorized: bool,
) -> dict[str, Any]:
    """Verify exact plan/state plus explicit operator authorization."""
    inspected = inspect_stage1_invocation_plan(
        stage1_root=stage1_root,
        plan=plan,
    )
    value = inspected["plan"]

    if value["plan_sha256"] != _require_sha256(
        expected_plan_sha256,
        "expected_plan_sha256",
    ):
        raise G7FExecutionError(
            "G7F invocation plan SHA mismatch"
        )
    if value["approval_token"] != _require_sha256(
        expected_approval_token,
        "expected_approval_token",
    ):
        raise G7FExecutionError(
            "G7F invocation approval token mismatch"
        )

    verified_g7e = verify_invocation_authorization(
        value["g7e_plan"],
        expected_approval_token=expected_approval_token,
        current_state_fingerprint_sha256=inspected[
            "current_state_fingerprint_sha256"
        ],
        operator_authorized=operator_authorized,
    )

    return {
        **inspected,
        "g7e_authorization": verified_g7e,
        "authorized": True,
    }


def _ledger_hash(value: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            key: copy.deepcopy(item)
            for key, item in dict(value).items()
            if key != "ledger_sha256"
        }
    )


def _with_ledger_hash(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["ledger_sha256"] = _ledger_hash(result)
    return result


def invocation_ledger_path(
    run_root: Path | str,
    target_chapter: int,
) -> Path:
    target = _positive_int(
        target_chapter,
        "target_chapter",
    )
    return (
        Path(run_root)
        / LEDGER_DIRECTORY
        / f"{target:04d}.json"
    )


class InvocationBudgetLedger:
    """Persist new-send count and accepted G7D credit cost."""

    def __init__(
        self,
        *,
        path: Path,
        document: Mapping[str, Any],
        clock: Callable[[], datetime],
    ):
        self.path = Path(path)
        self.document = copy.deepcopy(dict(document))
        self.clock = clock
        self._validate()

    @classmethod
    def open_or_create(
        cls,
        *,
        run_root: Path | str,
        plan: Mapping[str, Any],
        clock: Callable[[], datetime] | None = None,
        allow_create: bool = True,
    ) -> "InvocationBudgetLedger":
        value = _validate_plan_envelope(plan)
        nested = value["g7e_plan"]
        path = invocation_ledger_path(
            run_root,
            value["target_chapter"],
        )
        use_clock = clock or (
            lambda: datetime.now(timezone.utc)
        )

        if path.is_file():
            document = _read_object(
                path,
                "G7F invocation budget ledger",
            )
            ledger = cls(
                path=path,
                document=document,
                clock=use_clock,
            )
            ledger._assert_plan_binding(value)
            return ledger

        if not allow_create:
            raise G7FBudgetError(
                "G7F invocation budget ledger is missing for "
                "an existing paid response"
            )

        now = use_clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise G7FBudgetError(
                "G7F budget clock must return a timezone-aware datetime"
            )

        document = _with_ledger_hash(
            {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "revision": LEDGER_REVISION,
                "plan_sha256": value["plan_sha256"],
                "approval_token": value["approval_token"],
                "run_index": value["run_index"],
                "relative_root": value["relative_root"],
                "model_id": nested["model_id"],
                "novel_id": nested["novel_id"],
                "target_chapter": value["target_chapter"],
                "maximum_new_provider_sends": nested[
                    "maximum_new_provider_sends"
                ],
                "maximum_credit_equivalent_usd": nested[
                    "maximum_credit_equivalent_usd"
                ],
                "new_provider_sends_started": 0,
                "accepted_credit_equivalent_usd": "0",
                "records": [],
                "created_utc": now.astimezone(
                    timezone.utc
                ).isoformat(),
                "updated_utc": now.astimezone(
                    timezone.utc
                ).isoformat(),
            }
        )
        ledger = cls(
            path=path,
            document=document,
            clock=use_clock,
        )
        ledger._persist()
        return ledger

    def _validate(self) -> None:
        value = self.document

        if value.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise G7FBudgetError(
                "unsupported G7F ledger schema"
            )
        if value.get("revision") != LEDGER_REVISION:
            raise G7FBudgetError(
                "unsupported G7F ledger revision"
            )

        stored = value.get("ledger_sha256")
        if (
            not isinstance(stored, str)
            or not _SHA256_RE.fullmatch(stored)
        ):
            raise G7FBudgetError("G7F ledger SHA is invalid")
        if _ledger_hash(value) != stored:
            raise G7FBudgetError(
                "G7F ledger SHA does not recompute"
            )

        for name in ("plan_sha256", "approval_token"):
            _require_sha256(
                value.get(name),
                f"ledger {name}",
            )

        _positive_int(
            value.get("run_index"),
            "ledger run_index",
        )
        _positive_int(
            value.get("target_chapter"),
            "ledger target_chapter",
        )

        maximum = _positive_int(
            value.get("maximum_new_provider_sends"),
            "ledger maximum_new_provider_sends",
        )
        used = value.get("new_provider_sends_started")
        if (
            isinstance(used, bool)
            or not isinstance(used, int)
            or used < 0
        ):
            raise G7FBudgetError(
                "ledger new_provider_sends_started is invalid"
            )
        if used > maximum:
            raise G7FBudgetError(
                "ledger send count exceeds its ceiling"
            )

        max_cost = _positive_decimal(
            value.get("maximum_credit_equivalent_usd"),
            "ledger maximum_credit_equivalent_usd",
        )
        try:
            accepted = Decimal(
                str(
                    value.get(
                        "accepted_credit_equivalent_usd"
                    )
                )
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
        ) as exc:
            raise G7FBudgetError(
                "ledger accepted cost is invalid"
            ) from exc

        if not accepted.is_finite() or accepted < 0:
            raise G7FBudgetError(
                "ledger accepted cost is invalid"
            )
        records = value.get("records")
        if not isinstance(records, list):
            raise G7FBudgetError(
                "ledger records must be a list"
            )
        if accepted > max_cost:
            if (
                not records
                or not isinstance(records[-1], Mapping)
                or records[-1].get("status")
                != "accepted_cost_ceiling_exceeded"
            ):
                raise G7FBudgetError(
                    "ledger accepted cost exceeds its ceiling without "
                    "terminal over-ceiling evidence"
                )

        if not all(
            isinstance(record, Mapping)
            for record in records
        ):
            raise G7FBudgetError(
                "ledger records must be objects"
            )
        if used != len(records):
            raise G7FBudgetError(
                "ledger send count does not match persisted records"
            )

        allowed_statuses = {
            "started",
            "transport_exception",
            "accepted",
            "accepted_cost_ceiling_exceeded",
        }
        for expected_index, record in enumerate(
            records,
            start=1,
        ):
            if record.get("send_index") != expected_index:
                raise G7FBudgetError(
                    "ledger send indexes are not contiguous"
                )
            if record.get("status") not in allowed_statuses:
                raise G7FBudgetError(
                    "ledger contains an unknown send status"
                )
            _require_sha256(
                record.get("request_sha256"),
                "ledger request_sha256",
            )

    def _assert_plan_binding(
        self,
        plan: Mapping[str, Any],
    ) -> None:
        nested = plan["g7e_plan"]
        expected = {
            "plan_sha256": plan["plan_sha256"],
            "approval_token": plan["approval_token"],
            "run_index": plan["run_index"],
            "relative_root": plan["relative_root"],
            "model_id": nested["model_id"],
            "novel_id": nested["novel_id"],
            "target_chapter": plan["target_chapter"],
            "maximum_new_provider_sends": nested[
                "maximum_new_provider_sends"
            ],
            "maximum_credit_equivalent_usd": nested[
                "maximum_credit_equivalent_usd"
            ],
        }
        for field, wanted in expected.items():
            if self.document.get(field) != wanted:
                raise G7FBudgetError(
                    "persisted ledger binds another plan: "
                    f"{field}"
                )

    @property
    def new_provider_sends_started(self) -> int:
        return int(
            self.document["new_provider_sends_started"]
        )

    @property
    def accepted_cost(self) -> Decimal:
        return Decimal(
            str(
                self.document[
                    "accepted_credit_equivalent_usd"
                ]
            )
        )

    def _now(self) -> datetime:
        value = self.clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise G7FBudgetError(
                "G7F budget clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc)

    def _persist(self) -> None:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.document["updated_utc"] = (
            self._now().isoformat()
        )
        self.document = _with_ledger_hash(
            self.document
        )
        atomic_write_json(
            self.path,
            self.document,
        )

    def _cost_bound(
        self,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> tuple[Decimal, dict[str, Any]]:
        if not isinstance(request_body, Mapping):
            raise G7FBudgetError(
                "provider request body must be a mapping"
            )

        max_output = _positive_int(
            request_body.get("max_completion_tokens"),
            "provider max_completion_tokens",
        )
        serialized_bytes = len(
            _canonical_json(
                dict(request_body)
            ).encode("utf-8")
        )
        prompt_upper = (
            serialized_bytes
            + PROMPT_TOKEN_SAFETY_OVERHEAD
        )
        snapshot = g7d_pricing_snapshot(
            self.document["model_id"],
            request_started_utc,
        )
        miss_rate = Decimal(
            str(
                snapshot[
                    "cache_miss_rate_per_million"
                ]
            )
        )
        output_rate = Decimal(
            str(
                snapshot["output_rate_per_million"]
            )
        )
        bound = (
            Decimal(prompt_upper) * miss_rate
            + Decimal(max_output) * output_rate
        ) / Decimal(1_000_000)

        return bound, {
            **snapshot,
            "serialized_request_utf8_bytes": (
                serialized_bytes
            ),
            "prompt_token_upper_bound": prompt_upper,
            "max_completion_tokens": max_output,
        }

    def preflight_next_send(
        self,
        *,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        if (
            self.new_provider_sends_started
            >= self.document[
                "maximum_new_provider_sends"
            ]
        ):
            raise G7FBudgetError(
                "G7F new Provider-send ceiling reached"
            )

        bound, pricing = self._cost_bound(
            request_body,
            request_started_utc,
        )
        maximum = Decimal(
            str(
                self.document[
                    "maximum_credit_equivalent_usd"
                ]
            )
        )
        remaining = maximum - self.accepted_cost

        if bound > remaining:
            raise G7FBudgetError(
                "G7F credit-equivalent cost ceiling would "
                "be unsafe for the next request: "
                f"remaining={_decimal_text(remaining)}, "
                f"conservative_bound={_decimal_text(bound)}"
            )

        return {
            "conservative_cost_bound_usd": (
                _decimal_text(bound)
            ),
            "remaining_credit_equivalent_usd": (
                _decimal_text(remaining)
            ),
            "pricing": pricing,
        }

    def begin_send(
        self,
        *,
        logical_operation: str,
        chapter: int,
        request_sha256: str,
        request_body: Mapping[str, Any],
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        request_sha256 = _require_sha256(
            request_sha256,
            "request_sha256",
        )

        preview = self.preflight_next_send(
            request_body=request_body,
            request_started_utc=request_started_utc,
        )
        send_index = (
            self.new_provider_sends_started + 1
        )

        if isinstance(request_started_utc, str):
            started = request_started_utc
        else:
            started = request_started_utc.astimezone(
                timezone.utc
            ).isoformat()

        record = {
            "send_index": send_index,
            "logical_operation": logical_operation,
            "chapter": chapter,
            "request_sha256": request_sha256,
            "request_started_utc": started,
            "status": "started",
            "conservative_cost_bound_usd": preview[
                "conservative_cost_bound_usd"
            ],
            "accepted_cost_usd": "0",
            "stats": {},
            "error": {},
        }
        self.document[
            "new_provider_sends_started"
        ] = send_index
        self.document["records"].append(record)
        self._persist()
        return copy.deepcopy(record)

    def _record_for(
        self,
        send_index: int,
        request_sha256: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(send_index, int)
            or isinstance(send_index, bool)
            or send_index < 1
            or send_index > len(
                self.document["records"]
            )
        ):
            raise G7FBudgetError(
                "invalid G7F send index"
            )

        record = self.document["records"][
            send_index - 1
        ]
        if (
            record.get("request_sha256")
            != request_sha256
        ):
            raise G7FBudgetError(
                "G7F send record request identity changed"
            )
        return record

    def record_transport_exception(
        self,
        *,
        send_index: int,
        request_sha256: str,
        error: BaseException,
    ) -> None:
        record = self._record_for(
            send_index,
            request_sha256,
        )
        if record.get("status") != "started":
            raise G7FBudgetError(
                "transport exception record is not in started state"
            )

        record["status"] = "transport_exception"
        record["error"] = {
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }
        self._persist()

    def record_accepted_usage(
        self,
        *,
        send_index: int,
        request_sha256: str,
        usage: Any,
        request_started_utc: datetime | str,
    ) -> dict[str, Any]:
        record = self._record_for(
            send_index,
            request_sha256,
        )
        if record.get("status") != "started":
            raise G7FBudgetError(
                "accepted usage record is not in started state"
            )

        stats = account_provider_usage(
            self.document["model_id"],
            usage,
            request_started_utc,
        )
        # G7D accounting stays authoritative and is priced from the total
        # completion tokens only. The Provider-reported reasoning subtotal is a
        # diagnostic; G7J classifies it without raising so a deterministic
        # post-response diagnostic inconsistency stays a definitive, durable,
        # exactly-once accounted response instead of becoming an uncertain paid
        # outcome. An untrustworthy subtotal is never written into the stats.
        reasoning_outcome = reasoning_diagnostic_outcome(
            completion_tokens=stats["completion_tokens"],
            reasoning_tokens=getattr(
                usage,
                "reasoning_tokens",
                None,
            ),
            reasoning_tokens_exposed=getattr(
                usage,
                "reasoning_tokens_exposed",
                False,
            ),
        )
        if (
            reasoning_outcome["classification"]
            == REASONING_DIAGNOSTIC_OK
        ):
            stats = augment_accounting_stats_with_reasoning(
                stats,
                reasoning_tokens=getattr(
                    usage,
                    "reasoning_tokens",
                    None,
                ),
                reasoning_tokens_exposed=getattr(
                    usage,
                    "reasoning_tokens_exposed",
                    False,
                ),
            )

        actual = Decimal(
            str(
                stats["rate_correct_cost_usd"]
            )
        )
        projected = self.accepted_cost + actual
        maximum = Decimal(
            str(
                self.document[
                    "maximum_credit_equivalent_usd"
                ]
            )
        )

        record["accepted_cost_usd"] = (
            _decimal_text(actual)
        )
        record["stats"] = copy.deepcopy(stats)

        if projected > maximum:
            record["status"] = (
                "accepted_cost_ceiling_exceeded"
            )
            self.document[
                "accepted_credit_equivalent_usd"
            ] = _decimal_text(projected)
            self._persist()
            raise G7FBudgetError(
                "accepted Provider usage exceeded the "
                "G7F credit-equivalent cost ceiling; "
                "no further paid work is allowed"
            )

        record["status"] = "accepted"
        self.document[
            "accepted_credit_equivalent_usd"
        ] = _decimal_text(projected)
        self._persist()
        return copy.deepcopy(stats)

    def validate_replayed_response(
        self,
        *,
        request_sha256: str,
        response: Mapping[str, Any],
    ) -> None:
        request_sha256 = _require_sha256(
            request_sha256,
            "request_sha256",
        )
        matches = [
            record
            for record in self.document["records"]
            if (
                record.get("request_sha256")
                == request_sha256
                and record.get("status") == "accepted"
            )
        ]

        if len(matches) != 1:
            raise G7FBudgetError(
                "durable response replay lacks exactly one "
                "accepted G7F ledger record"
            )

        stats = response.get("stats")
        if not isinstance(stats, Mapping):
            raise G7FBudgetError(
                "replayed response lacks accounting stats"
            )
        if dict(stats) != matches[0].get("stats"):
            raise G7FBudgetError(
                "replayed response accounting differs from "
                "the G7F ledger"
            )


def _replayed_completion_tokens(
    stats: Mapping[str, Any],
) -> int:
    """Return the exact durable authoritative completion total for a replay.

    Replay never coerces the persisted Provider-reported total: only a real
    nonnegative ``int`` that is not a ``bool`` is accepted, so a missing, ``None``,
    string, float, boolean, or negative value fails closed with one stable
    completion-token-specific error instead of an incidental ``KeyError``,
    ``TypeError``, or ``ValueError``. The validated integer is returned unchanged
    and is never passed through ``int()`` again.
    """
    value = stats.get("completion_tokens", _ABSENT)
    if (
        value is _ABSENT
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise G7FExecutionError(
            "journaled G7F stats have invalid completion_tokens"
        )
    return value


def _replayed_reasoning_diagnostics(
    stats: Mapping[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    """Validate the durable completion total and rebuild reasoning diagnostics.

    The durable authoritative completion total is validated straight from the
    persisted stats before any conversion, comparison, subtraction, or
    arithmetic, and is returned exactly as persisted.

    Replay then applies the same strict G7J validation that initial accounting
    applied: ``reasoning_tokens_exposed`` must be an actual boolean and must be
    ``True`` whenever a subtotal value is present, no subtotal value may exist
    when it is ``False`` or absent, ``reasoning_tokens`` must be a nonnegative
    integer that cannot exceed the authoritative completion total, and a
    persisted ``visible_completion_tokens`` must be valid and must equal
    ``completion_tokens - reasoning_tokens``. An envelope that predates G7J has
    no diagnostics at all and keeps exactly its original usage shape. Any other
    malformed durable state is rejected rather than silently tolerated.
    """
    completion_tokens = _replayed_completion_tokens(stats)
    exposed = stats.get("reasoning_tokens_exposed", _ABSENT)
    subtotal = stats.get("reasoning_tokens", _ABSENT)

    if exposed is _ABSENT:
        if subtotal is not _ABSENT and subtotal is not None:
            raise G7FExecutionError(
                "journaled G7F stats expose a reasoning subtotal "
                "without exposure metadata"
            )
        _reject_stale_visible_subtotal(stats)
        return completion_tokens, None
    if not isinstance(exposed, bool):
        raise G7FExecutionError(
            "journaled G7F stats have malformed reasoning "
            "exposure metadata"
        )

    try:
        diagnostics = reasoning_usage_diagnostics(
            completion_tokens=completion_tokens,
            reasoning_tokens=(
                None if subtotal is _ABSENT else subtotal
            ),
            reasoning_tokens_exposed=exposed,
        )
    except G7JPolicyError as exc:
        classification = reasoning_diagnostic_classification(
            completion_tokens=completion_tokens,
            reasoning_tokens=(
                None if subtotal is _ABSENT else subtotal
            ),
            reasoning_tokens_exposed=exposed,
        )
        raise G7FExecutionError(
            "journaled G7F stats have invalid reasoning_tokens: "
            f"{classification}"
        ) from exc

    if not diagnostics["reasoning_tokens_exposed"]:
        _reject_stale_visible_subtotal(stats)
        return completion_tokens, None

    visible = stats.get("visible_completion_tokens", _ABSENT)
    if visible is _ABSENT:
        return completion_tokens, diagnostics
    if (
        isinstance(visible, bool)
        or not isinstance(visible, int)
        or visible < 0
    ):
        raise G7FExecutionError(
            "journaled G7F stats have invalid visible_completion_tokens"
        )
    if visible != diagnostics["visible_completion_tokens"]:
        raise G7FExecutionError(
            "journaled G7F stats have inconsistent "
            "visible_completion_tokens"
        )
    return completion_tokens, diagnostics


def _reject_stale_visible_subtotal(
    stats: Mapping[str, Any],
) -> None:
    """Reject a visible subtotal that cannot be derived from any subtotal."""
    if "visible_completion_tokens" in stats:
        raise G7FExecutionError(
            "journaled G7F stats persist a visible_completion_tokens "
            "without an exposed reasoning subtotal"
        )


def _response_from_envelope(
    envelope: Mapping[str, Any],
) -> Any:
    stats = envelope.get("stats")
    if not isinstance(stats, Mapping):
        raise G7FExecutionError(
            "journaled response lacks G7F accounting stats"
        )

    # The durable authoritative completion total is validated before any
    # coercion, comparison, or usage construction, and the exact validated
    # integer is used. Replay never calls ``int()`` on this value.
    completion_tokens, diagnostics = (
        _replayed_reasoning_diagnostics(stats)
    )
    usage_fields: dict[str, Any] = {
        "prompt_tokens": int(
            stats["prompt_tokens"]
        ),
        "completion_tokens": completion_tokens,
        "prompt_tokens_details": SimpleNamespace(
            cached_tokens=int(
                stats["cache_hit_tokens"]
            )
        ),
        "_g7f_stats": copy.deepcopy(dict(stats)),
    }
    # Only a strictly validated, Provider-reported reasoning subtotal is
    # reconstructed. A pre-G7J envelope therefore keeps exactly its original
    # usage shape and never manufactures ``completion_tokens_details`` with a
    # guessed ``None``.
    if diagnostics is not None:
        usage_fields["completion_tokens_details"] = (
            SimpleNamespace(
                reasoning_tokens=diagnostics["reasoning_tokens"]
            )
        )

    usage = SimpleNamespace(**usage_fields)
    return SimpleNamespace(
        id=envelope.get(
            "provider_response_id",
            "",
        ),
        model=envelope.get(
            "provider_response_model",
            "",
        ),
        choices=[
            SimpleNamespace(
                finish_reason=envelope.get(
                    "finish_reason"
                ),
                message=SimpleNamespace(
                    content=envelope.get(
                        "content",
                        "",
                    )
                ),
            )
        ],
        usage=usage,
    )


def _validate_saved_stats(
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        raise G7FExecutionError(
            "saved G7F stats must be a mapping"
        )

    value = copy.deepcopy(dict(stats))
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cost_usd",
        "rate_correct_cost_usd",
    ):
        item = value.get(name)
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or item < 0
        ):
            raise G7FExecutionError(
                f"saved G7F stats contain invalid {name}"
            )

    if value.get("pricing_version") != PRICING_VERSION:
        raise G7FExecutionError(
            "saved G7F stats bind another pricing version"
        )
    return value


def _logical_operation_kind(
    logical_operation: str,
    request_sha256: str,
    *,
    multi_request_slot: bool,
) -> str:
    if logical_operation not in _ALLOWED_OPERATIONS:
        raise G7FExecutionError(
            "unknown G7F lifecycle operation: "
            f"{logical_operation}"
        )
    if not multi_request_slot:
        return f"g7c_{logical_operation}"
    return (
        f"g7c_{logical_operation}_"
        f"{request_sha256[:20]}"
    )


def _translation_response_diagnostic(
    *,
    content: Any,
    finish_reason: Any,
    stats: Any,
    max_completion_tokens: Any,
) -> dict[str, Any]:
    """Classify one translation response without coercing its content."""
    if not isinstance(stats, Mapping):
        raise G7FExecutionError(
            "G7F translation response lacks accounting stats"
        )
    try:
        return classify_translation_response(
            content=content,
            finish_reason=finish_reason,
            completion_tokens=stats.get("completion_tokens"),
            max_completion_tokens=max_completion_tokens,
        )
    except G7JPolicyError as exc:
        raise G7FExecutionError(
            "G7J translation-response diagnostic failed"
        ) from exc


def _validated_persisted_diagnostic(
    value: Any,
) -> dict[str, Any] | None:
    """Return one durable G7J diagnostic, or None for a legacy envelope.

    ``_ABSENT`` and an explicit ``None`` both mean the envelope predates the
    bounded G7J diagnostic and is classified locally instead.
    """
    if value is _ABSENT or value is None:
        return None
    if not isinstance(value, Mapping):
        raise G7FExecutionError(
            "persisted G7J diagnostic is malformed"
        )

    diagnostic = copy.deepcopy(dict(value))
    status = diagnostic.get("status")
    if (
        not isinstance(status, str)
        or status not in TRANSLATION_RESPONSE_STATUSES
    ):
        raise G7FExecutionError(
            "persisted G7J diagnostic has an unknown status"
        )
    if not isinstance(
        diagnostic.get("visible_content_empty"),
        bool,
    ) or not isinstance(
        diagnostic.get("near_completion_ceiling"),
        bool,
    ):
        raise G7FExecutionError(
            "persisted G7J diagnostic lost its boolean fields"
        )
    finish_reason = diagnostic.get("finish_reason")
    if finish_reason is not None and not isinstance(
        finish_reason,
        str,
    ):
        raise G7FExecutionError(
            "persisted G7J diagnostic finish_reason is invalid"
        )
    completion_tokens = diagnostic.get("completion_tokens")
    if completion_tokens is not None and (
        isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens < 0
    ):
        raise G7FExecutionError(
            "persisted G7J diagnostic completion_tokens is invalid"
        )
    maximum = diagnostic.get("max_completion_tokens")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < 1
    ):
        raise G7FExecutionError(
            "persisted G7J diagnostic max_completion_tokens is invalid"
        )
    return diagnostic


def _durable_translation_diagnostic(
    *,
    response: Mapping[str, Any],
    max_completion_tokens: Any,
) -> dict[str, Any]:
    """Return the exact evidence-derived diagnostic for one durable envelope.

    The classification is always recomputed from the durable envelope content,
    its finish reason, the authoritative completion stats, and the exact
    corrected request ceiling. A persisted diagnostic is structurally checked
    and is only usable when it equals that recomputation exactly; a persisted
    diagnostic that disagrees with its own durable evidence fails closed. A
    legacy envelope without a persisted diagnostic is classified locally and the
    historical journal is never rewritten.
    """
    recomputed = _translation_response_diagnostic(
        content=response.get("content", ""),
        finish_reason=response.get("finish_reason"),
        stats=response.get("stats"),
        max_completion_tokens=max_completion_tokens,
    )
    persisted = _validated_persisted_diagnostic(
        response.get("diagnostic", _ABSENT)
    )
    if persisted is not None and persisted != recomputed:
        raise G7FExecutionError(
            "persisted G7J diagnostic disagrees with the durable "
            "response evidence"
        )
    return recomputed


def _reject_invalid_reasoning_diagnostic(
    response: Mapping[str, Any],
) -> None:
    """Reject a durable response whose reasoning diagnostic is inconsistent.

    The record is only reached after the bounded durable envelope has been
    persisted and its authoritative usage accounted exactly once. Its
    classification is recomputed from its own bounded evidence, so a mangled or
    hand-edited record fails closed instead of being trusted.
    """
    record = response.get("reasoning_diagnostic_failure", _ABSENT)
    if record is _ABSENT:
        return
    if not isinstance(record, Mapping):
        raise G7FExecutionError(
            "durable G7F reasoning diagnostic record is malformed"
        )
    if set(record) != {
        "classification",
        "completion_tokens",
        "reasoning_tokens_reported",
        "reasoning_tokens_exposed_reported",
    }:
        raise G7FExecutionError(
            "durable G7F reasoning diagnostic record has an "
            "unexpected shape"
        )

    classification = record["classification"]
    if (
        not isinstance(classification, str)
        or classification not in REASONING_DIAGNOSTIC_CLASSIFICATIONS
        or classification == REASONING_DIAGNOSTIC_OK
    ):
        raise G7FExecutionError(
            "durable G7F reasoning diagnostic record has an "
            "unknown classification"
        )
    recomputed = reasoning_diagnostic_classification(
        completion_tokens=record["completion_tokens"],
        reasoning_tokens=record["reasoning_tokens_reported"],
        reasoning_tokens_exposed=record[
            "reasoning_tokens_exposed_reported"
        ],
    )
    if recomputed != classification:
        raise G7FExecutionError(
            "durable G7F reasoning diagnostic record disagrees "
            "with its own bounded evidence"
        )

    raise G7FExecutionError(
        "G7J reasoning diagnostic rejected: "
        f"{classification}; "
        f"completion_tokens={record['completion_tokens']!r}; "
        "reasoning_tokens="
        f"{record['reasoning_tokens_reported']!r}; "
        "reasoning_tokens_exposed="
        f"{record['reasoning_tokens_exposed_reported']!r}; "
        "the durable response was recorded and accounted exactly once and no "
        "retry is attempted"
    )


def goat_auxiliary_response_guard(
    logical_operation: str,
    response: Mapping[str, Any],
    max_completion_tokens: Any,
) -> dict[str, Any]:
    """GOAT-only, opt-in fail-closed enforcement for one durable aux response.

    This is the exact hook an opt-in G7F GOAT runtime installs through
    ``install_stage1_paid_runtime(..., auxiliary_response_guard=...)`` (or
    implicitly through ``goat_auxiliary_policy=True``). The production code path
    never installs it, so production behavior is byte-identical.

    It runs at the point where the durable response envelope is consumed,
    immediately after the envelope has been validated/replayed and before the
    engine's state pipeline can fall through to its generic JSON repair branch.
    An empty, length-terminated auxiliary response is therefore classified
    exactly and rejected deterministically: it can never be repaired from empty
    input into fabricated state. Nothing here retries, reissues, or mutates the
    durable journal or the ledger.
    """
    if not isinstance(response, Mapping):
        raise G7FExecutionError(
            "G7F auxiliary response envelope must be a mapping"
        )
    stats = response.get("stats")
    if not isinstance(stats, Mapping):
        raise G7FExecutionError(
            "G7F auxiliary response envelope lacks "
            "accounting stats"
        )
    try:
        diagnostic = classify_auxiliary_response(
            logical_operation=logical_operation,
            content=response.get("content", ""),
            finish_reason=response.get("finish_reason"),
            completion_tokens=stats.get("completion_tokens"),
            max_completion_tokens=max_completion_tokens,
            reasoning_tokens=stats.get("reasoning_tokens"),
        )
    except G7JPolicyError as exc:
        raise G7FExecutionError(
            "G7L auxiliary-response diagnostic failed"
        ) from exc

    if diagnostic["fail_closed"]:
        raise G7FExecutionError(
            "G7L auxiliary response rejected: "
            f"{diagnostic['status']}; "
            f"logical_operation={logical_operation!r}; "
            f"finish_reason={diagnostic['finish_reason']!r}; "
            f"completion_tokens={diagnostic['completion_tokens']!r}; "
            f"max_completion_tokens={diagnostic['max_completion_tokens']!r}; "
            f"near_completion_ceiling="
            f"{diagnostic['near_completion_ceiling']!r}; "
            "the durable response was recorded and accounted exactly once, it "
            "contains no content, and no JSON repair or retry is attempted"
        )
    return diagnostic


def _reject_non_ok_translation_response(
    diagnostic: Mapping[str, Any],
) -> None:
    """Reject a definitively classified non-ok response; never retry."""
    status = diagnostic["status"]
    if status == STATUS_OK:
        return
    raise G7FExecutionError(
        "G7J translation response rejected: "
        f"{status}; "
        f"finish_reason={diagnostic.get('finish_reason')!r}; "
        f"completion_tokens={diagnostic.get('completion_tokens')!r}; "
        f"max_completion_tokens={diagnostic.get('max_completion_tokens')!r}; "
        f"near_completion_ceiling="
        f"{diagnostic.get('near_completion_ceiling')!r}; "
        "the durable response was recorded and accounted exactly once and no "
        "retry is attempted"
    )


def install_stage1_paid_runtime(
    production_module: Any,
    *,
    model_id: str,
    provider_client: Any,
    budget_ledger: InvocationBudgetLedger,
    clock: Callable[[], datetime] | None = None,
    goat_auxiliary_policy: bool = False,
    auxiliary_response_guard: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Install direct Provider+journal+G7D runtime in the real engine.

    ``goat_auxiliary_policy`` is the explicit GOAT-only opt-in that (a) applies
    the versioned G7P GOAT Stage-1 runtime auxiliary 8K completion ceiling -- the
    additive successor of the frozen G7N 8K policy, which is itself the additive
    successor of the frozen 4K G7L/G7M auxiliary policy and keeps its exact
    historical semantics -- at the auxiliary budget point and (b) installs the
    GOAT fail-closed auxiliary response guard at the point where the durable
    response envelope is consumed. The G7P runtime policy accepts the frozen G7N
    accepted set plus the closed set of legitimate production auxiliary
    subrequest ceilings the real lifecycle nests under the same logical context
    operation, and rewrites every accepted request to exactly 8,000.
    Both defaults are off, so the production auxiliary path is byte-identical and
    existing callers are unaffected. ``auxiliary_response_guard`` may instead be
    supplied explicitly; ``None`` (the default) installs no guard at all.
    """
    if not isinstance(goat_auxiliary_policy, bool):
        raise G7FExecutionError(
            "goat_auxiliary_policy must be a boolean"
        )
    if (
        auxiliary_response_guard is not None
        and not callable(auxiliary_response_guard)
    ):
        raise G7FExecutionError(
            "auxiliary_response_guard must be callable when present"
        )
    if not getattr(
        production_module,
        "_RUNTIME_INITIALIZED",
        False,
    ):
        raise G7FExecutionError(
            "real production engine must be initialized first"
        )
    if getattr(production_module, "client", None) is not None:
        raise G7FExecutionError(
            "provider-suppressed engine unexpectedly has a client"
        )
    if getattr(production_module, "api_key", None) is not None:
        raise G7FExecutionError(
            "provider-suppressed engine unexpectedly retained a key"
        )
    if provider_client is None:
        raise G7FExecutionError(
            "real G7F runtime requires an injected Provider client"
        )

    known_models = {
        spec["model_id"]
        for spec in stage1_run_specs()
    }
    if model_id not in known_models:
        raise G7FExecutionError(
            f"unknown Stage-1 model id: {model_id!r}"
        )
    if (
        budget_ledger.document.get("model_id")
        != model_id
    ):
        raise G7FExecutionError(
            "G7F ledger/model binding mismatch"
        )
    if getattr(
        production_module,
        "_g7f_stage1_runtime_state",
        None,
    ) is not None:
        raise G7FExecutionError(
            "G7F paid runtime is already installed"
        )

    use_clock = clock or (
        lambda: datetime.now(timezone.utc)
    )
    operation_stack: list[tuple[str, int]] = []

    # GOAT-only opt-in auxiliary policy. With both defaults off these two values
    # reproduce the pre-G7L behavior exactly: no auxiliary ceiling correction and
    # no post-response auxiliary guard.
    goat_auxiliary_budget = goat_auxiliary_policy is True
    installed_auxiliary_guard = (
        auxiliary_response_guard
        if auxiliary_response_guard is not None
        else (
            goat_auxiliary_response_guard
            if goat_auxiliary_budget
            else None
        )
    )

    originals = {
        name: getattr(production_module, name)
        for name in (
            "journaled_chat_request",
            "call_api_with_retries",
            "send_api_request_once",
            "usage_and_cost",
            "usage_and_cost_for_pricing",
            "reprice_saved_stats_if_needed",
            "pricing_snapshot",
            *CONTEXT_ENTRYPOINTS,
        )
    }

    def current_context() -> tuple[str, int]:
        if not operation_stack:
            raise G7FExecutionError(
                "provider-shaped auxiliary request occurred "
                "outside an explicit G7F lifecycle context"
            )
        return operation_stack[-1]

    @contextmanager
    def operation_context(
        logical_operation: str,
        chapter: int,
    ):
        if logical_operation not in _ALLOWED_OPERATIONS:
            raise G7FExecutionError(
                "invalid G7F operation context"
            )
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or chapter < 1
        ):
            raise G7FExecutionError(
                "operation context chapter must be positive"
            )

        operation_stack.append(
            (logical_operation, chapter)
        )
        try:
            yield
        finally:
            popped = operation_stack.pop()
            if popped != (
                logical_operation,
                chapter,
            ):
                raise G7FExecutionError(
                    "G7F operation context stack corrupted"
                )

    def normalize_and_journal(
        logical_operation: str,
        chapter_number: int,
        source_text: str,
        request_kwargs: Mapping[str, Any],
        *,
        translation_text: str = "",
        multi_request_slot: bool,
    ) -> dict[str, Any]:
        actual_request = normalize_legacy_request(
            model_id=model_id,
            logical_operation=logical_operation,
            request_kwargs=request_kwargs,
        )

        # Benchmark-only G7J budget policy: applied after G7C normalization and
        # before provider request identity, journal identity, ledger preflight,
        # and transport. Auxiliary operations keep their original ceiling; the
        # frozen GOAT translation baseline is rewritten to exactly 24000.
        try:
            adjusted_completion_budget = translation_completion_budget(
                logical_operation=logical_operation,
                requested_tokens=int(
                    actual_request["max_completion_tokens"]
                ),
                reasoning_effort=actual_request.get(
                    "reasoning_effort"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise G7FExecutionError(
                "G7J reasoning-aware completion budget could not "
                "be applied safely"
            ) from exc

        if (
            adjusted_completion_budget
            != actual_request["max_completion_tokens"]
        ):
            actual_request = copy.deepcopy(
                dict(actual_request)
            )
            actual_request[
                "max_completion_tokens"
            ] = adjusted_completion_budget

        # GOAT-only Stage-1 runtime auxiliary budget policy: applied at the same
        # point, after G7C normalization and before provider request identity,
        # journal identity, ledger preflight, and transport, so the corrected
        # auxiliary ceiling is reflected in the request body that all of them
        # bind. The versioned Stage-1 runtime policy (G7P) is the additive
        # successor of the frozen G7N 8K policy: it accepts the frozen G7N
        # accepted set PLUS the closed set of legitimate production auxiliary
        # subrequest ceilings the real lifecycle nests under the same logical
        # context operation (for example the glossary schema/semantic correction
        # at 2,000, the generic JSON repair at 5,000, and the strict terminology
        # / residual CJK micro-repairs at 800), and rewrites every accepted
        # request to exactly 8,000. G7N/G7O historical/recovery paths stay bound
        # to the frozen G7N 8K policy. The production default never enters this
        # branch, so non-GOAT auxiliary requests keep their original ceiling
        # byte-for-byte.
        if goat_auxiliary_budget and (
            logical_operation != PRIMARY_TRANSLATION_OPERATION
        ):
            try:
                corrected_auxiliary_budget = (
                    goat_auxiliary_stage1_runtime_completion_budget(
                        logical_operation=logical_operation,
                        requested_tokens=int(
                            actual_request["max_completion_tokens"]
                        ),
                    )
                )
                goat_identity_ceiling = (
                    goat_auxiliary_stage1_runtime_completion_budget_for_identity(
                        goat=True
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise G7FExecutionError(
                    "G7P Stage-1 runtime GOAT auxiliary 8K completion budget "
                    "could not be applied safely"
                ) from exc

            if corrected_auxiliary_budget != goat_identity_ceiling:
                raise G7FExecutionError(
                    "the G7P Stage-1 runtime GOAT auxiliary 8K completion "
                    "ceiling drifted from the GOAT state-checkpoint identity "
                    "ceiling"
                )

            if (
                corrected_auxiliary_budget
                != actual_request["max_completion_tokens"]
            ):
                actual_request = copy.deepcopy(
                    dict(actual_request)
                )
                actual_request[
                    "max_completion_tokens"
                ] = corrected_auxiliary_budget

        provider_identity = (
            build_provider_request_identity(
                request_body=actual_request
            )
        )

        novel_id = (
            production_module.ACTIVE_NOVEL_CONFIG.id
            if (
                production_module.ACTIVE_NOVEL_CONFIG
                is not None
            )
            else "unknown"
        )
        profile_id = (
            getattr(
                production_module.ACTIVE_PROFILE,
                "id",
                "",
            )
            or "unknown"
        )
        source_sha256 = hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest()
        translation_sha256 = (
            hashlib.sha256(
                translation_text.encode("utf-8")
            ).hexdigest()
            if translation_text
            else ""
        )

        journal_identity = request_identity(
            operation_kind=logical_operation,
            novel_id=novel_id,
            chapter=chapter_number,
            provider=PROVIDER_NAME,
            model=provider_identity.requested_model,
            profile_id=profile_id,
            source_sha256=source_sha256,
            translation_sha256=translation_sha256,
            request_payload=actual_request,
        )
        journal_kind = _logical_operation_kind(
            logical_operation,
            provider_identity.request_sha256,
            multi_request_slot=multi_request_slot,
        )
        journal_path = paid_request_journal_path(
            production_module.PAID_REQUEST_JOURNAL_DIR,
            journal_kind,
            chapter_number,
        )

        existing = load_matching_journal(
            journal_path,
            journal_identity,
        )
        preflight_started = use_clock()
        if (
            not isinstance(
                preflight_started,
                datetime,
            )
            or preflight_started.tzinfo is None
            or preflight_started.utcoffset() is None
        ):
            raise G7FExecutionError(
                "G7F runtime clock must be timezone-aware"
            )

        if (
            existing is None
            or existing["status"] == "planned"
        ):
            budget_ledger.preflight_next_send(
                request_body=actual_request,
                request_started_utc=preflight_started,
            )

        attempt_box: dict[str, Any] = {}

        def prepare_attempt() -> dict[str, Any]:
            started = use_clock()
            if (
                not isinstance(started, datetime)
                or started.tzinfo is None
                or started.utcoffset() is None
            ):
                raise G7FExecutionError(
                    "G7F runtime clock must be timezone-aware"
                )
            started = started.astimezone(
                timezone.utc
            )
            snapshot = g7d_pricing_snapshot(
                model_id,
                started,
            )
            context = {
                "mode": G7F_REVISION,
                "pricing_version": PRICING_VERSION,
                "pricing_period": snapshot[
                    "pricing_period"
                ],
                "request_started_utc": (
                    started.isoformat()
                ),
                "request_sha256": (
                    provider_identity.request_sha256
                ),
                "plan_sha256": budget_ledger.document[
                    "plan_sha256"
                ],
            }
            attempt_box.clear()
            attempt_box.update(context)
            return context

        def send_once() -> Any:
            if not attempt_box:
                raise G7FExecutionError(
                    "G7F attempt context was not prepared"
                )

            record = budget_ledger.begin_send(
                logical_operation=logical_operation,
                chapter=chapter_number,
                request_sha256=(
                    provider_identity.request_sha256
                ),
                request_body=actual_request,
                request_started_utc=attempt_box[
                    "request_started_utc"
                ],
            )
            try:
                return send_chat_completion_once(
                    provider_client,
                    actual_request,
                )
            except BaseException as exc:
                budget_ledger.record_transport_exception(
                    send_index=record["send_index"],
                    request_sha256=(
                        provider_identity.request_sha256
                    ),
                    error=exc,
                )
                raise

        def normalize_response(
            raw_response: Any,
            attempt_metadata: Mapping[str, Any],
        ) -> dict[str, Any]:
            normalized = normalize_provider_response(
                raw_response,
                request_identity=provider_identity,
            )
            accepted = accept_provider_response(
                provider_identity,
                normalized,
            )

            records = budget_ledger.document[
                "records"
            ]
            if not records:
                raise G7FBudgetError(
                    "accepted response has no persisted send record"
                )
            send_index = records[-1]["send_index"]

            stats = budget_ledger.record_accepted_usage(
                send_index=send_index,
                request_sha256=(
                    provider_identity.request_sha256
                ),
                usage=accepted.usage,
                request_started_utc=attempt_metadata[
                    "request_started_utc"
                ],
            )

            # The same bounded G7J classification the ledger just accounted is
            # derived here from the same authoritative inputs, so the durable
            # envelope can never disagree with the persisted accounting.
            reasoning_outcome = reasoning_diagnostic_outcome(
                completion_tokens=stats["completion_tokens"],
                reasoning_tokens=accepted.usage.reasoning_tokens,
                reasoning_tokens_exposed=(
                    accepted.usage.reasoning_tokens_exposed
                ),
            )

            envelope = {
                "content": accepted.content,
                "finish_reason": accepted.finish_reason,
                "stats": stats,
                "provider_response_id": (
                    accepted.provider_response_id
                ),
                "provider_response_model": (
                    accepted.response_model
                ),
                "request_sha256": (
                    provider_identity.request_sha256
                ),
                "logical_operation": (
                    logical_operation
                ),
            }
            if (
                reasoning_outcome["classification"]
                != REASONING_DIAGNOSTIC_OK
            ):
                # A deterministic post-response diagnostic failure becomes a
                # bounded durable record instead of an uncertain paid outcome.
                # The authoritative G7D accounting above already happened
                # exactly once and is priced from the total completion tokens
                # only; the explicit G7F rejection happens after this envelope
                # reaches ``response_recorded``.
                envelope["reasoning_diagnostic_failure"] = {
                    "classification": (
                        reasoning_outcome["classification"]
                    ),
                    "completion_tokens": (
                        stats["completion_tokens"]
                    ),
                    "reasoning_tokens_reported": (
                        accepted.usage.reasoning_tokens
                    ),
                    "reasoning_tokens_exposed_reported": (
                        accepted.usage.reasoning_tokens_exposed
                    ),
                }
            if (
                logical_operation
                == PRIMARY_TRANSLATION_OPERATION
            ):
                # Attach the bounded diagnostic before the durable
                # ``response_recorded`` write so a replay reuses the persisted
                # classification instead of recomputing it.
                envelope["diagnostic"] = (
                    _translation_response_diagnostic(
                        content=accepted.content,
                        finish_reason=(
                            accepted.finish_reason
                        ),
                        stats=stats,
                        max_completion_tokens=(
                            actual_request[
                                "max_completion_tokens"
                            ]
                        ),
                    )
                )
            return envelope

        result = run_journaled_paid_request(
            journal_path=journal_path,
            identity=journal_identity,
            save_json=lambda path, value: (
                atomic_write_json(path, value)
            ),
            prepare_attempt=prepare_attempt,
            send_once=send_once,
            normalize_response=normalize_response,
        )

        response = (
            result.get("response")
            if isinstance(result, Mapping)
            else None
        )
        if not isinstance(response, Mapping):
            raise G7FExecutionError(
                "durable paid journal returned no "
                "response envelope"
            )
        if (
            response.get("provider_response_model")
            != provider_identity.requested_model
        ):
            raise G7FExecutionError(
                "journaled response model differs "
                "from the request"
            )

        if result.get("replayed"):
            budget_ledger.validate_replayed_response(
                request_sha256=(
                    provider_identity.request_sha256
                ),
                response=response,
            )

        # The envelope is already durable at this point (``response_recorded``)
        # and its authoritative usage was accounted exactly once. Every rejection
        # below is therefore an explicit post-persistence, definitive
        # classification with no automatic retry.
        _reject_invalid_reasoning_diagnostic(response)

        if logical_operation == PRIMARY_TRANSLATION_OPERATION:
            # Recompute the bounded diagnostic from the durable envelope
            # evidence and require any persisted diagnostic to match it exactly.
            # A legacy pre-G7J envelope without one is classified locally
            # without rewriting the historical journal.
            diagnostic = _durable_translation_diagnostic(
                response=response,
                max_completion_tokens=(
                    actual_request[
                        "max_completion_tokens"
                    ]
                ),
            )
            _reject_non_ok_translation_response(diagnostic)

        if (
            logical_operation != PRIMARY_TRANSLATION_OPERATION
            and installed_auxiliary_guard is not None
        ):
            # GOAT-only, opt-in, fail-closed auxiliary enforcement at the exact
            # point where the durable response envelope is consumed and handed
            # back to the engine. An empty, length-terminated auxiliary response
            # therefore raises here, inside the engine's journaled request call,
            # so the engine's state pipeline can never reach its generic JSON
            # repair branch with empty content. The production path installs no
            # guard and stays byte-identical.
            installed_auxiliary_guard(
                logical_operation,
                response,
                actual_request["max_completion_tokens"],
            )

        return copy.deepcopy(dict(result))

    def journaled_chat_request(
        operation_kind,
        chapter_number,
        source_text,
        request_kwargs,
        *,
        translation_text="",
    ):
        try:
            logical = DIRECT_LEGACY_OPERATION_MAP[
                operation_kind
            ]
        except KeyError as exc:
            raise G7FExecutionError(
                "unmapped direct legacy operation "
                f"{operation_kind!r}"
            ) from exc

        return normalize_and_journal(
            logical,
            chapter_number,
            source_text,
            request_kwargs,
            translation_text=translation_text,
            multi_request_slot=False,
        )

    def auxiliary_request(request_kwargs):
        logical, chapter = current_context()
        messages = request_kwargs.get("messages")
        if not isinstance(messages, list):
            raise G7FExecutionError(
                "auxiliary request lacks messages"
            )

        source_text = "\n".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, Mapping)
        )
        result = normalize_and_journal(
            logical,
            chapter,
            source_text,
            request_kwargs,
            multi_request_slot=True,
        )
        return _response_from_envelope(
            result["response"]
        )

    def call_api_with_retries(**kwargs):
        # Historical name only. G7F performs one
        # journal-owned transport attempt.
        return auxiliary_request(kwargs)

    def send_api_request_once(**kwargs):
        return auxiliary_request(kwargs)

    def usage_and_cost(usage):
        stats = getattr(
            usage,
            "_g7f_stats",
            None,
        )
        if stats is None:
            raise G7FExecutionError(
                "G7F response usage lacks bound "
                "accounting stats"
            )
        return _validate_saved_stats(stats)

    def usage_and_cost_for_pricing(
        usage,
        _pricing,
    ):
        return usage_and_cost(usage)

    def reprice_saved_stats_if_needed(stats):
        return _validate_saved_stats(stats)

    def pricing_snapshot(moment=None):
        use_moment = (
            moment
            if moment is not None
            else use_clock()
        )
        return g7d_pricing_snapshot(
            model_id,
            use_moment,
        )

    provider_model_id = live_provider_capability(model_id).provider_model_id
    production_module.MODEL = provider_model_id
    production_module.client = None
    production_module.api_key = None
    production_module.AUTO_OFF_PEAK_ONLY = False
    production_module.journaled_chat_request = (
        journaled_chat_request
    )
    production_module.call_api_with_retries = (
        call_api_with_retries
    )
    production_module.send_api_request_once = (
        send_api_request_once
    )
    production_module.usage_and_cost = usage_and_cost
    production_module.usage_and_cost_for_pricing = (
        usage_and_cost_for_pricing
    )
    production_module.reprice_saved_stats_if_needed = (
        reprice_saved_stats_if_needed
    )
    production_module.pricing_snapshot = (
        pricing_snapshot
    )

    wrapped_entrypoints = {}

    for name, logical_operation in (
        CONTEXT_ENTRYPOINTS.items()
    ):
        original = originals[name]

        def make_wrapper(
            original_callable,
            operation,
        ):
            def wrapped(*args, **kwargs):
                if args:
                    chapter = args[0]
                else:
                    chapter = kwargs.get(
                        "chapter_number"
                    )
                    if (
                        chapter is None
                        and operation
                        == "periodic_audit"
                    ):
                        chapter = kwargs.get(
                            "end_chapter"
                        )

                if (
                    operation == "periodic_audit"
                    and len(args) >= 2
                ):
                    chapter = args[1]

                with operation_context(
                    operation,
                    int(chapter),
                ):
                    return original_callable(
                        *args,
                        **kwargs,
                    )

            return wrapped

        wrapper = make_wrapper(
            original,
            logical_operation,
        )
        setattr(
            production_module,
            name,
            wrapper,
        )
        wrapped_entrypoints[name] = wrapper

    state = {
        "mode": G7F_REVISION,
        "model_id": model_id,
        "journaled_operations": sorted(
            _ALLOWED_OPERATIONS
        ),
        "context_entrypoints": dict(
            CONTEXT_ENTRYPOINTS
        ),
        "provider_calls_authorized": True,
        "provider_transport": (
            "command_code_provider_direct"
        ),
        "coding_agent_used_for_inference": False,
        "goat_auxiliary_policy": goat_auxiliary_budget,
        # G7N: the future GOAT auxiliary path binds the additive versioned 8K
        # policy. The policy version travels in the runtime state (and in the
        # materialized G7N plan) instead of a G7F revision bump, so G7F_REVISION,
        # the G7F plan envelope, the ledger schema, and every predecessor
        # binding stay byte-frozen.
        "goat_auxiliary_total_completion_tokens": (
            GOAT_AUXILIARY_8K_TOTAL_COMPLETION_TOKENS
            if goat_auxiliary_budget
            else None
        ),
        "goat_auxiliary_legacy_total_completion_tokens": (
            GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
            if goat_auxiliary_budget
            else None
        ),
        "goat_auxiliary_policy_revision": (
            GOAT_AUXILIARY_8K_REVISION
            if goat_auxiliary_budget
            else None
        ),
        "goat_auxiliary_policy_sha256": (
            G7J_AUXILIARY_8K_POLICY_SHA256
            if goat_auxiliary_budget
            else None
        ),
        # G7P: the Stage-1 runtime applies the additive, versioned runtime
        # subrequest-compatibility policy, whose unchanged base is the frozen
        # G7N 8K policy reported just above. The G7N/G7O historical/recovery
        # paths stay bound to that frozen 8K policy revision/SHA.
        "goat_auxiliary_runtime_policy_revision": (
            GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION
            if goat_auxiliary_budget
            else None
        ),
        "goat_auxiliary_runtime_policy_sha256": (
            G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256
            if goat_auxiliary_budget
            else None
        ),
        "goat_auxiliary_runtime_subrequest_compat_applied": (
            goat_auxiliary_budget
        ),
        "auxiliary_response_guard_installed": (
            installed_auxiliary_guard is not None
        ),
        "production_auxiliary_budget_unchanged": True,
        "g7d_accounting_sha256": (
            G7D_ACCOUNTING_SHA256
        ),
        "budget_ledger_path": str(
            budget_ledger.path
        ),
        "originals": originals,
        "wrapped_entrypoints": (
            wrapped_entrypoints
        ),
    }
    production_module._g7f_stage1_runtime_state = state
    return state


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def execute_stage1_invocation(
    *,
    code_root: Path | str,
    stage1_root: Path | str,
    plan: Mapping[str, Any],
    expected_plan_sha256: str,
    expected_approval_token: str,
    operator_authorized: bool,
    api_key: str | None = None,
    client_factory=None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Execute exactly one approved run and target chapter.

    With operator_authorized=False this remains a read-only authorization
    check: no Provider client is constructed and no budget ledger is created.
    """
    inspected = inspect_stage1_invocation_plan(
        stage1_root=stage1_root,
        plan=plan,
    )
    value = inspected["plan"]

    if value["plan_sha256"] != _require_sha256(
        expected_plan_sha256,
        "expected_plan_sha256",
    ):
        raise G7FExecutionError(
            "G7F invocation plan SHA mismatch"
        )
    if value["approval_token"] != _require_sha256(
        expected_approval_token,
        "expected_approval_token",
    ):
        raise G7FExecutionError(
            "G7F invocation approval token mismatch"
        )

    if operator_authorized is not True:
        return {
            "status": "AUTHORIZATION_REQUIRED",
            "paid_execution_authorized": False,
            "plan_sha256": value[
                "plan_sha256"
            ],
            "approval_token": value[
                "approval_token"
            ],
            "run_index": value["run_index"],
            "target_chapter": value[
                "target_chapter"
            ],
            "provider_calls": 0,
        }

    if (
        not isinstance(api_key, str)
        or not api_key.strip()
    ):
        raise G7FExecutionError(
            "explicitly authorized paid execution "
            "requires an injected API key"
        )

    code = Path(code_root).resolve()
    production_script = (
        code / PRODUCTION_SCRIPT_NAME
    )
    if not production_script.is_file():
        raise G7FExecutionError(
            f"production script missing: {production_script}"
        )

    run_root = inspected["binding"]["run_root"]
    target = value["target_chapter"]
    use_clock = clock or (
        lambda: datetime.now(timezone.utc)
    )

    with production_run_lock(run_root):
        verified = verify_stage1_invocation_plan(
            stage1_root=stage1_root,
            plan=value,
            expected_plan_sha256=(
                expected_plan_sha256
            ),
            expected_approval_token=(
                expected_approval_token
            ),
            operator_authorized=True,
        )

        replay_count = verified[
            "chapter_state"
        ]["journal_state"][
            "local_replay_only_count"
        ]
        ledger_path = invocation_ledger_path(
            run_root,
            target,
        )
        if (
            replay_count
            and not ledger_path.is_file()
        ):
            raise G7FBudgetError(
                "durable paid response exists but "
                "the required G7F budget ledger "
                "is missing"
            )

        budget = InvocationBudgetLedger.open_or_create(
            run_root=run_root,
            plan=value,
            clock=use_clock,
            allow_create=True,
        )

        provider_client = create_provider_client(
            api_key=api_key,
            client_factory=client_factory,
        )

        loaded = None
        try:
            with _working_directory(run_root):
                loaded = (
                    load_experiment_engine_module(
                        production_script
                    )
                )
                module = loaded["module"]
                initialize_engine_without_provider(
                    module
                )
                install_stage1_paid_runtime(
                    module,
                    model_id=value[
                        "g7e_plan"
                    ]["model_id"],
                    provider_client=provider_client,
                    budget_ledger=budget,
                    clock=use_clock,
                    # The real Stage-1 GOAT runtime is the only caller that opts
                    # in to the G7P Stage-1 runtime auxiliary policy: the
                    # corrected 8000 auxiliary ceiling -- including every
                    # legitimate production subrequest ceiling -- plus the
                    # fail-closed auxiliary response guard. Nothing else in the
                    # executor turns either on.
                    goat_auxiliary_policy=True,
                )

                runner = getattr(
                    module,
                    "run_unattended_production",
                    None,
                )
                if not callable(runner):
                    raise G7FExecutionError(
                        "real production module lacks "
                        "run_unattended_production"
                    )

                runner(
                    target_chapter=target
                )
        finally:
            if loaded is not None:
                sys.modules.pop(
                    loaded["module_name"],
                    None,
                )

        progress = _read_progress(run_root)
        if progress.get("last_completed") != target:
            raise G7FExecutionError(
                "real lifecycle did not finish exactly "
                "the approved target chapter"
            )
        if progress.get("next_chapter") != target + 1:
            raise G7FExecutionError(
                "real lifecycle advanced to an "
                "unexpected next chapter"
            )

        budget = (
            InvocationBudgetLedger.open_or_create(
                run_root=run_root,
                plan=value,
                clock=use_clock,
                allow_create=False,
            )
        )

        return {
            "status": "COMPLETED",
            "paid_execution_authorized": True,
            "plan_sha256": value[
                "plan_sha256"
            ],
            "approval_token": value[
                "approval_token"
            ],
            "run_index": value["run_index"],
            "relative_root": value[
                "relative_root"
            ],
            "target_chapter": target,
            "new_provider_sends_started": (
                budget.new_provider_sends_started
            ),
            "accepted_credit_equivalent_usd": (
                _decimal_text(
                    budget.accepted_cost
                )
            ),
            "budget_ledger_path": str(
                budget.path
            ),
            "automatic_chapter_advance": False,
        }


__all__ = [
    "EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256",
    "G7FBudgetError",
    "G7FExecutionError",
    "G7F_EXECUTOR_SHA256",
    "G7F_REVISION",
    "InvocationBudgetLedger",
    "build_stage1_invocation_plan",
    "execute_stage1_invocation",
    "g7f_executor_identity",
    "goat_auxiliary_response_guard",
    "inspect_stage1_invocation_plan",
    "install_stage1_paid_runtime",
    "invocation_ledger_path",
    "state_fingerprint_sha256",
    "verify_stage1_invocation_plan",
]
