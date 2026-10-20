"""Immutable zero-provider preflight for the Luna Batch chronological pilot.

This module does not submit, poll, collect, or process provider work.  It freezes
an operator-visible authorization envelope around a *fresh* Phase 60H/v3
experiment before the first paid boundary is crossed.

The freeze binds:
- the exact repository anchor already carried by the experiment manifest;
- the Phase 60H contract and experiment-manifest hashes;
- source-project/corpus fingerprints and isolated S0 state bindings;
- the exact first-wave Luna request identities;
- the chronological pilot wave plan and genuine audit schedule;
- explicit caps for provider API operations, synchronous Luna calls, and total
  paid cost.

Wave 2+ request bodies are intentionally not precomputed: they must be built
only after the preceding wave is accepted and committed, preserving the causal
state-growth experiment.  Importing this module performs no filesystem writes,
credential loading, subprocess execution, or provider calls.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_experiment_harness as harness


PREFLIGHT_SCHEMA_VERSION = 1
PREFLIGHT_REVISION = "full-pipeline-paid-pilot-preflight-v1"
PREFLIGHT_FILENAME = "paid_pilot_preflight.json"
AUTHORIZATION_PREFIX = "RUN-FULL-PIPELINE-LUNA-PAID-PILOT-V1"
DEFAULT_PILOT_WAVE_COUNT = 2


class FullPipelinePaidPilotPreflightError(RuntimeError):
    """The paid-pilot freeze is missing, inconsistent, or unsafe to use."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight identity is not canonically JSON-serializable"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise FullPipelinePaidPilotPreflightError(f"cannot hash {path}: {exc}") from exc


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullPipelinePaidPilotPreflightError(
            f"cannot read {label} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FullPipelinePaidPilotPreflightError(f"{label} must contain an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FullPipelinePaidPilotPreflightError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FullPipelinePaidPilotPreflightError(
            f"{label} must be a non-negative integer"
        )
    return value


def _money_text(value: Any, label: str) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FullPipelinePaidPilotPreflightError(f"{label} is not a valid decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise FullPipelinePaidPilotPreflightError(f"{label} must be finite and positive")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _preflight_path(experiment_root: Path | str) -> Path:
    return Path(experiment_root).resolve() / PREFLIGHT_FILENAME


def _exclusive_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Publish the freeze exactly once; partial failure remains blocking evidence."""
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FullPipelinePaidPilotPreflightError(
            f"paid-pilot preflight already exists and will not be overwritten: {path}"
        ) from exc
    except OSError as exc:
        raise FullPipelinePaidPilotPreflightError(
            f"cannot exclusively create paid-pilot preflight {path}: {exc}"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Preserve any final-named bytes as blocking evidence.  A later run must
        # investigate rather than silently replacing an interrupted authorization.
        raise
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise FullPipelinePaidPilotPreflightError(
            f"cannot verify paid-pilot preflight {path}: {exc}"
        ) from exc
    if observed != payload:
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight readback differs from the frozen payload"
        )


def _source_binding_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    bindings = manifest.get("source_bindings")
    if not isinstance(bindings, list):
        raise FullPipelinePaidPilotPreflightError(
            "experiment manifest source_bindings must be a list"
        )
    for item in bindings:
        if not isinstance(item, dict):
            raise FullPipelinePaidPilotPreflightError(
                "experiment source binding must be an object"
            )
        novel_id = item.get("id")
        if not isinstance(novel_id, str) or not novel_id or novel_id in result:
            raise FullPipelinePaidPilotPreflightError(
                "experiment source bindings require unique non-empty ids"
            )
        result[novel_id] = copy.deepcopy(item)
    return result


def _assert_production_guard(
    manifest: Mapping[str, Any],
    repository_root: Path,
) -> dict[str, Any]:
    novel_ids = manifest.get("novel_ids")
    if not isinstance(novel_ids, list) or not novel_ids:
        raise FullPipelinePaidPilotPreflightError("experiment manifest lacks novel_ids")
    current = harness.capture_production_guard(repository_root, novel_ids)
    if current != manifest.get("production_guard"):
        raise FullPipelinePaidPilotPreflightError(
            "source production projects differ from the experiment production guard"
        )
    return current


def _engine_binding(repository_root: Path) -> dict[str, Any]:
    script = repository_root / harness.PRODUCTION_SCRIPT_NAME
    profiles = repository_root / "profiles"
    if not script.is_file():
        raise FullPipelinePaidPilotPreflightError(
            f"production engine script is missing: {script}"
        )
    if not profiles.is_dir():
        raise FullPipelinePaidPilotPreflightError(
            f"translation profile directory is missing: {profiles}"
        )
    return {
        "production_script": {
            "relative_path": harness.PRODUCTION_SCRIPT_NAME,
            "sha256": _sha256_file(script),
            "bytes": script.stat().st_size,
        },
        "profiles_tree": harness._tree_fingerprint(profiles),
    }


def _fresh_s0_binding(
    experiment_root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    waves = experiment_root / "waves"
    if not waves.is_dir():
        raise FullPipelinePaidPilotPreflightError("experiment waves directory is missing")
    wave_residue = sorted(path.name for path in waves.iterdir())
    if wave_residue:
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight requires a fresh experiment with no wave artifacts: "
            + ", ".join(wave_residue)
        )

    source_bindings = _source_binding_map(manifest)
    initial_lineages = manifest.get("initial_lineages")
    if not isinstance(initial_lineages, dict):
        raise FullPipelinePaidPilotPreflightError(
            "experiment manifest lacks initial lineages"
        )

    projects: dict[str, Any] = {}
    for novel_id in manifest["novel_ids"]:
        project = experiment_root / "projects" / novel_id
        if not project.is_dir():
            raise FullPipelinePaidPilotPreflightError(
                f"experiment project root is missing: {project}"
            )

        # Phase 60G intentionally replaced the harness's original broad lineage
        # with the v2 causal-lineage contract.  The v3 experiment manifest thus
        # carries this causal shape, not harness.capture_experiment_lineage().
        causal = v3.capture_causal_lineage(project)
        if causal != initial_lineages.get(novel_id):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} is no longer at the frozen S0 causal lineage"
            )
        scheduler = causal.get("scheduler")
        if not isinstance(scheduler, Mapping):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} S0 causal lineage lacks scheduler state"
            )
        if (
            scheduler.get("completed") != []
            or scheduler.get("failed") != []
            or scheduler.get("last_completed") != 0
            or scheduler.get("next_chapter") != 1
            or scheduler.get("last_audited_chapter") != 0
        ):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} does not have fresh S0 scheduler state"
            )

        # Bind the complete Phase 60G wave-boundary identity too.  This records
        # committed evidence separately from causal prompt state, avoiding the
        # older experiment's mistake of conflating unrelated recovery artifacts
        # with causal lineage while still proving a pristine committed history.
        state_binding = v3.validate_experiment_state_binding(
            v3.capture_experiment_state_binding(project)
        )
        if state_binding.get("causal") != causal:
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} full state binding disagrees with its S0 causal lineage"
            )

        pending_tree = harness._tree_fingerprint(project / "output" / "pending")
        journal_tree = harness._tree_fingerprint(
            project / "data" / "provider_request_journal"
        )
        recovery_tree = harness._tree_fingerprint(
            project / "data" / "full_pipeline_audit_recovery"
        )
        if pending_tree["file_count"] or journal_tree["file_count"] or recovery_tree["file_count"]:
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} already contains pending/provider/recovery evidence"
            )

        binding = source_bindings.get(novel_id)
        if binding is None:
            raise FullPipelinePaidPilotPreflightError(
                f"experiment manifest lacks source binding for {novel_id}"
            )
        local = {
            "novel_json_sha256": _sha256_file(project / "novel.json"),
            "chapters_sha256": _sha256_file(project / "data" / "chapters.json"),
            "translation_addendum_sha256": _sha256_file(
                project / "translation_addendum.txt"
            ),
        }
        if local["novel_json_sha256"] != binding.get("source_config_sha256"):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} experiment novel.json differs from its source binding"
            )
        if local["chapters_sha256"] != binding.get("source_chapters_sha256"):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} experiment chapter corpus differs from its source binding"
            )
        if local["translation_addendum_sha256"] != binding.get(
            "source_addendum_sha256"
        ):
            raise FullPipelinePaidPilotPreflightError(
                f"{novel_id} experiment translation addendum differs from its source binding"
            )
        projects[novel_id] = {
            "source_binding": copy.deepcopy(binding),
            "experiment_copy": local,
            "initial_causal_lineage": copy.deepcopy(causal),
            "initial_state_binding": copy.deepcopy(state_binding),
            "pending_tree": pending_tree,
            "provider_journal_tree": journal_tree,
            "audit_recovery_tree": recovery_tree,
        }
    return projects


def _request_preview(requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    preview = []
    for item in requests:
        body = item.get("body")
        lineage = item.get("lineage_before")
        if not isinstance(body, Mapping) or not isinstance(lineage, Mapping):
            raise FullPipelinePaidPilotPreflightError(
                "Wave 1 preview request lacks body/lineage identity"
            )
        preview.append(
            {
                "novel_id": item.get("novel_id"),
                "chapter": item.get("chapter"),
                "body_sha256": _sha256_text(_canonical_json(dict(body))),
                "lineage_before_sha256": _sha256_text(
                    _canonical_json(dict(lineage))
                ),
            }
        )
    preview.sort(key=lambda item: str(item["novel_id"]))
    return preview


def _audit_schedule(
    manifest: Mapping[str, Any],
    pilot_end_wave: int,
) -> dict[str, Any]:
    target = _positive_int(manifest.get("target_chapters"), "target_chapters")
    bindings = _source_binding_map(manifest)
    by_novel = {}
    for novel_id in manifest["novel_ids"]:
        interval = _positive_int(
            bindings[novel_id].get("audit_interval"),
            f"{novel_id} audit_interval",
        )
        full = list(range(interval, target + 1, interval))
        by_novel[novel_id] = {
            "audit_interval": interval,
            "full_experiment_due_chapters": full,
            "pilot_due_chapters": [chapter for chapter in full if chapter <= pilot_end_wave],
        }
    return {
        "policy": "use-genuine-novel-audit-intervals-no-shortening",
        "by_novel": by_novel,
    }


def _wave_plan(
    manifest: Mapping[str, Any],
    pilot_wave_count: int,
    wave1_preview: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    novel_ids = list(manifest["novel_ids"])
    audit = _audit_schedule(manifest, pilot_wave_count)["by_novel"]
    plan = []
    for wave in range(1, pilot_wave_count + 1):
        due = [
            novel_id
            for novel_id in novel_ids
            if wave in audit[novel_id]["full_experiment_due_chapters"]
        ]
        entry = {
            "wave_number": wave,
            "novel_ids": novel_ids,
            "batch_model_request_count": len(novel_ids),
            "depends_on_prior_wave_commit": wave > 1,
            "audit_due_novels": due,
            "request_identity_policy": (
                "exact_s0_preview_bound"
                if wave == 1
                else "deferred_until_prior_wave_committed"
            ),
        }
        if wave == 1:
            entry["request_previews"] = copy.deepcopy(list(wave1_preview))
        plan.append(entry)
    return plan


def _operation_budget(
    *,
    pilot_wave_count: int,
    novel_count: int,
    max_sync_model_calls: int,
    max_batch_status_reads: int,
    max_batch_reconciliation_reads: int,
) -> dict[str, Any]:
    batch_model_requests = pilot_wave_count * novel_count
    if max_batch_status_reads < pilot_wave_count:
        raise FullPipelinePaidPilotPreflightError(
            "max_batch_status_reads must allow at least one status observation per wave"
        )
    return {
        "batch_input_upload_attempts_cap": pilot_wave_count,
        "batch_create_attempts_cap": pilot_wave_count,
        "batch_model_requests_exact": batch_model_requests,
        "batch_status_reads_cap": max_batch_status_reads,
        "batch_reconciliation_list_reads_cap": max_batch_reconciliation_reads,
        "batch_output_downloads_cap": pilot_wave_count,
        "sync_model_calls_cap": max_sync_model_calls,
        "paid_model_request_cap": batch_model_requests + max_sync_model_calls,
        "provider_api_call_cap": (
            pilot_wave_count
            + pilot_wave_count
            + max_batch_status_reads
            + max_batch_reconciliation_reads
            + pilot_wave_count
            + max_sync_model_calls
        ),
        "note": (
            "Batch upload/create/status/list/output operations are provider API calls; "
            "paid_model_request_cap counts Batch model requests plus synchronous Luna "
            "model calls. Actual synchronous calls remain data-dependent."
        ),
    }


def freeze_paid_pilot_preflight(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    expected_repo_anchor: str,
    pilot_wave_count: int = DEFAULT_PILOT_WAVE_COUNT,
    max_sync_model_calls: int,
    max_batch_status_reads: int,
    max_batch_reconciliation_reads: int,
    max_total_paid_cost_usd: Any,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Freeze one fresh paid-pilot authorization envelope without provider access."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    path = _preflight_path(root)
    if path.exists():
        raise FullPipelinePaidPilotPreflightError(
            f"paid-pilot preflight already exists and will not be overwritten: {path}"
        )
    pilot_wave_count = _positive_int(pilot_wave_count, "pilot_wave_count")
    max_sync_model_calls = _nonnegative_int(max_sync_model_calls, "max_sync_model_calls")
    max_batch_status_reads = _positive_int(
        max_batch_status_reads, "max_batch_status_reads"
    )
    max_batch_reconciliation_reads = _nonnegative_int(
        max_batch_reconciliation_reads,
        "max_batch_reconciliation_reads",
    )
    cost_cap = _money_text(max_total_paid_cost_usd, "max_total_paid_cost_usd")
    if not isinstance(expected_repo_anchor, str) or not expected_repo_anchor.strip():
        raise FullPipelinePaidPilotPreflightError(
            "expected_repo_anchor must be a non-empty commit SHA"
        )

    manifest = harness.load_experiment_manifest(root)
    contract = v3.load_experiment_contract(root)
    if manifest.get("repo_anchor") != expected_repo_anchor.strip():
        raise FullPipelinePaidPilotPreflightError(
            "experiment repo_anchor differs from the operator-observed repository anchor"
        )
    target = _positive_int(manifest.get("target_chapters"), "target_chapters")
    if pilot_wave_count > target:
        raise FullPipelinePaidPilotPreflightError(
            "pilot_wave_count cannot exceed the experiment target_chapters"
        )

    production_guard = _assert_production_guard(manifest, repo)
    engine_binding = _engine_binding(repo)
    projects = _fresh_s0_binding(root, manifest)
    wave1_requests = v3.build_wave_requests(root, repo, 1)
    wave1_preview = _request_preview(wave1_requests)
    if len(wave1_preview) != len(manifest["novel_ids"]):
        raise FullPipelinePaidPilotPreflightError(
            "Wave 1 preview coverage differs from experiment novel_ids"
        )

    operation_budget = _operation_budget(
        pilot_wave_count=pilot_wave_count,
        novel_count=len(manifest["novel_ids"]),
        max_sync_model_calls=max_sync_model_calls,
        max_batch_status_reads=max_batch_status_reads,
        max_batch_reconciliation_reads=max_batch_reconciliation_reads,
    )
    stable = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "preflight_revision": PREFLIGHT_REVISION,
        "created_at_utc": now().astimezone(timezone.utc).isoformat(),
        "experiment_id": manifest["experiment_id"],
        "experiment_root": str(root),
        "repository_root": str(repo),
        "repo_anchor": manifest["repo_anchor"],
        "experiment_manifest_sha256": manifest["experiment_manifest_sha256"],
        "v3_contract_sha256": contract["contract_sha256"],
        "v3_contract_revision": contract["contract_revision"],
        "pilot_scope": {
            "start_wave": 1,
            "end_wave": pilot_wave_count,
            "wave_count": pilot_wave_count,
            "novel_ids": list(manifest["novel_ids"]),
            "full_experiment_target_chapters": target,
        },
        "paths": {
            "waves_root": str(root / "waves"),
            "project_roots": {
                novel_id: str(root / "projects" / novel_id)
                for novel_id in manifest["novel_ids"]
            },
        },
        "production_guard": copy.deepcopy(production_guard),
        "engine_binding": engine_binding,
        "projects": projects,
        "wave_plan": _wave_plan(manifest, pilot_wave_count, wave1_preview),
        "audit_schedule": _audit_schedule(manifest, pilot_wave_count),
        "operation_budget": operation_budget,
        "max_total_paid_cost_usd": cost_cap,
        "execution_gate": {
            "provider_execution_authorized_by_this_module": False,
            "status": "frozen_preflight_only",
            "required_next_guard": (
                "provider execution must load this exact artifact, validate the "
                "authorization token, and enforce the frozen operation/cost caps"
            ),
        },
    }
    preflight_sha = _sha256_text(_canonical_json(stable))
    document = copy.deepcopy(stable)
    document["preflight_sha256"] = preflight_sha
    document["authorization_token"] = f"{AUTHORIZATION_PREFIX}:{preflight_sha}"

    # Re-check both protected source state and fresh experiment state immediately
    # before publishing the authorization artifact.
    if _assert_production_guard(manifest, repo) != production_guard:
        raise FullPipelinePaidPilotPreflightError(
            "production guard changed while paid-pilot preflight was being built"
        )
    if _fresh_s0_binding(root, manifest) != projects:
        raise FullPipelinePaidPilotPreflightError(
            "experiment S0 state changed while paid-pilot preflight was being built"
        )
    _exclusive_write_json(path, document)
    loaded = load_paid_pilot_preflight(root, repo, expected_repo_anchor=expected_repo_anchor)
    if loaded != document:
        raise FullPipelinePaidPilotPreflightError(
            "published paid-pilot preflight differs from verified readback"
        )
    return loaded


def load_paid_pilot_preflight(
    experiment_root: Path | str,
    repository_root: Path | str | None = None,
    *,
    expected_repo_anchor: str | None = None,
) -> dict[str, Any]:
    """Load and cryptographically/binding-validate a frozen preflight artifact."""
    root = Path(experiment_root).resolve()
    path = _preflight_path(root)
    document = _read_object(path, "paid-pilot preflight")
    if document.get("schema_version") != PREFLIGHT_SCHEMA_VERSION:
        raise FullPipelinePaidPilotPreflightError(
            "unsupported paid-pilot preflight schema"
        )
    if document.get("preflight_revision") != PREFLIGHT_REVISION:
        raise FullPipelinePaidPilotPreflightError(
            "unsupported paid-pilot preflight revision"
        )
    stable = copy.deepcopy(document)
    observed_hash = stable.pop("preflight_sha256", None)
    observed_token = stable.pop("authorization_token", None)
    expected_hash = _sha256_text(_canonical_json(stable))
    if observed_hash != expected_hash:
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight hash mismatch"
        )
    if observed_token != f"{AUTHORIZATION_PREFIX}:{expected_hash}":
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot authorization token does not match the frozen preflight"
        )

    manifest = harness.load_experiment_manifest(root)
    contract = v3.load_experiment_contract(root)
    if document.get("experiment_root") != str(root):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight belongs to a different experiment root"
        )
    if document.get("experiment_id") != manifest.get("experiment_id"):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight belongs to a different experiment"
        )
    if document.get("experiment_manifest_sha256") != manifest.get(
        "experiment_manifest_sha256"
    ):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight is bound to a different experiment manifest"
        )
    if document.get("v3_contract_sha256") != contract.get("contract_sha256"):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight is bound to a different Phase 60H contract"
        )
    if document.get("v3_contract_revision") != contract.get("contract_revision"):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight carries the wrong Phase 60H contract revision"
        )
    if document.get("repo_anchor") != manifest.get("repo_anchor"):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight repo anchor differs from the experiment manifest"
        )
    if expected_repo_anchor is not None and document.get("repo_anchor") != str(
        expected_repo_anchor
    ).strip():
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot preflight differs from the operator-observed repository anchor"
        )

    if repository_root is not None:
        repo = Path(repository_root).resolve()
        if document.get("repository_root") != str(repo):
            raise FullPipelinePaidPilotPreflightError(
                "paid-pilot preflight belongs to a different repository root"
            )
        if _assert_production_guard(manifest, repo) != document.get("production_guard"):
            raise FullPipelinePaidPilotPreflightError(
                "production guard differs from the frozen paid-pilot preflight"
            )
        if _engine_binding(repo) != document.get("engine_binding"):
            raise FullPipelinePaidPilotPreflightError(
                "engine/profile binding differs from the frozen paid-pilot preflight"
            )
    return document


def validate_paid_pilot_preflight_for_first_provider_operation(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    expected_repo_anchor: str,
    authorization_token: str,
) -> dict[str, Any]:
    """Final zero-provider gate immediately before the first remote operation."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    document = load_paid_pilot_preflight(
        root,
        repo,
        expected_repo_anchor=expected_repo_anchor,
    )
    if authorization_token != document.get("authorization_token"):
        raise FullPipelinePaidPilotPreflightError(
            "paid-pilot authorization token was not supplied exactly"
        )
    manifest = harness.load_experiment_manifest(root)
    if _fresh_s0_binding(root, manifest) != document.get("projects"):
        raise FullPipelinePaidPilotPreflightError(
            "experiment S0 state changed after the paid-pilot preflight freeze"
        )
    current_preview = _request_preview(v3.build_wave_requests(root, repo, 1))
    frozen_preview = document["wave_plan"][0].get("request_previews")
    if current_preview != frozen_preview:
        raise FullPipelinePaidPilotPreflightError(
            "Wave 1 request identity changed after the paid-pilot preflight freeze"
        )
    return document
