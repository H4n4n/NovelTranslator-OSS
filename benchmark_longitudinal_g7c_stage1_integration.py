"""G7C zero-provider Stage-1 execution integration proof.

G7C resolves the three integration blockers left by G7B without authorizing a
paid request.  It binds the twelve model×novel Stage-1 runs to external
NovelTranslator-native S0 roots, proves the real production lifecycle can be
loaded/initialized with provider construction suppressed, and installs a
synthetic-only Command Code runtime boundary whose every provider-shaped call
is protected by the production durable paid-request journal.

G7C deliberately does NOT contain a real Provider transport.  Paid Stage-1
remains blocked until a later phase freezes authoritative Command Code
accounting and an explicit paid execution/authorization contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import uuid
from typing import Any, Callable, Mapping, Sequence

from benchmark_longitudinal_capability_results import (
    live_provider_capability,
    verified_reasoning_capability,
)
from benchmark_longitudinal_config import (
    BENCHMARK_MODEL_IDS,
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
    PROVIDER_NAME,
)
from benchmark_longitudinal_g7a_readiness import (
    G7A_READINESS_SHA256,
    SOURCE_BINDINGS,
    STAGE1_LAST_CHAPTER,
    g7a_readiness_identity,
)
from benchmark_longitudinal_g7b_auxiliary_policy import (
    G7B_POLICY_SHA256,
    g7b_policy_identity,
    reasoning_by_operation,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request,
    build_provider_request_identity,
    normalize_provider_response,
)
from chapter_library import index_chapter_library, validate_chapter_library
from create_novel import PROJECT_DIRECTORIES, initial_project_state
from full_pipeline_experiment_harness import (
    capture_production_guard,
)
from full_pipeline_luna_runtime import (
    initialize_engine_without_provider,
    load_experiment_engine_module,
)
from novel_project import load_novel_config
from noveltranslator_core import atomic_write_json


G7C_SCHEMA_VERSION = 1
G7C_REVISION = "goat-g7c-stage1-execution-integration-v1"
G7C_REPOSITORY_ANCHOR = "d70506e43cc840127d93e657007f68b466a89871"

EXPECTED_G7A_READINESS_SHA256 = (
    "f43a7f1130b0e20aeca7ba068d278b1c41c2ff064b351eaa816748b99f6cd1e0"
)
EXPECTED_G7B_POLICY_SHA256 = (
    "a93a2b2a09acc3e80324e295e995aeb0f3366287becb8531d75ef157258b6500"
)

PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
ROOTS_MANIFEST_FILENAME = "g7c_stage1_roots_manifest.json"
RUN_IDENTITY_FILENAME = "g7c_run_identity.json"
PROOF_ACCOUNTING_VERSION = "g7c-synthetic-proof-accounting-v1"

RESOLVED_G7B_INTEGRATION_BLOCKERS = (
    "bind_stage1_execution_to_real_production_lifecycle",
    "prove_external_isolated_s0_roots_and_production_guard",
    "prove_durable_paid_request_journal_for_every_provider_assisted_operation",
)

PAID_STAGE1_REMAINING_BLOCKERS = (
    "freeze_command_code_provider_accounting_policy",
    "freeze_stage1_paid_execution_authorization_contract",
)

DIRECT_LEGACY_OPERATION_MAP = {
    "chapter_translation": LIFECYCLE_OPERATION_TRANSLATION,
    "state_extraction": LIFECYCLE_OPERATION_STATE_EXTRACTION,
    "state_json_repair": LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
}

CONTEXT_ENTRYPOINTS = {
    "review_new_terms": LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    "attempt_automatic_glossary_candidate_resolution": (
        LIFECYCLE_OPERATION_GLOSSARY_REVIEW
    ),
    "run_periodic_audit": LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    "maybe_repair_strict_terminology": LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    "maybe_repair_residual_cjk": LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
}

_SAFE_COMPONENT_RE = re.compile(r"[^a-z0-9]+")


class G7CIntegrationError(RuntimeError):
    """Stage-1 integration proof is incomplete, inconsistent, or unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7CIntegrationError(
            "G7C value is not canonically JSON-serializable"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _sha256_file(path: Path | str) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise G7CIntegrationError(f"cannot hash {path}: {exc}") from exc


def _read_json(path: Path | str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7CIntegrationError(f"cannot read {label}: {exc}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise G7CIntegrationError(f"{label} must contain a JSON object")
    return value


def _path_is_within(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or root in path.parents


def _safe_component(value: str) -> str:
    result = _SAFE_COMPONENT_RE.sub("_", value.lower()).strip("_")
    if not result:
        raise G7CIntegrationError(f"unsafe empty path component from {value!r}")
    return result


def _stable_run_relative_root(index: int, run: Mapping[str, Any]) -> str:
    return (
        f"runs/{index:02d}_"
        f"{_safe_component(str(run['model_id']))}__"
        f"{_safe_component(str(run['novel_id']))}__"
        f"{str(run['run_sha256'])[:12]}"
    )


def _validate_predecessors() -> tuple[dict[str, Any], dict[str, Any]]:
    if G7A_READINESS_SHA256 != EXPECTED_G7A_READINESS_SHA256:
        raise G7CIntegrationError("G7A readiness identity changed")
    if G7B_POLICY_SHA256 != EXPECTED_G7B_POLICY_SHA256:
        raise G7CIntegrationError("G7B policy identity changed")

    g7a = g7a_readiness_identity()
    g7b = g7b_policy_identity()
    if g7a["stage1"]["provider_calls_authorized"] is not False:
        raise G7CIntegrationError("G7A unexpectedly authorizes Provider calls")
    if g7b["stage1"]["provider_calls_authorized"] is not False:
        raise G7CIntegrationError("G7B unexpectedly authorizes Provider calls")
    if not g7b["reasoning_policy_complete"]:
        raise G7CIntegrationError("G7B reasoning policy is incomplete")
    if tuple(g7b["execution_gate"]["required_before_paid_stage1"]) != (
        RESOLVED_G7B_INTEGRATION_BLOCKERS
    ):
        raise G7CIntegrationError("G7B integration blocker set changed")
    return g7a, g7b


def stage1_run_specs() -> tuple[dict[str, Any], ...]:
    """Return deterministic external-root specifications for all twelve runs."""
    g7a, _g7b = _validate_predecessors()
    runs = g7a["runs"]
    if not isinstance(runs, list) or len(runs) != 12:
        raise G7CIntegrationError("G7A must contain exactly twelve Stage-1 runs")

    specs = []
    seen_roots: set[str] = set()
    for index, run in enumerate(runs, start=1):
        relative_root = _stable_run_relative_root(index, run)
        if relative_root in seen_roots:
            raise G7CIntegrationError("duplicate external Stage-1 run root")
        seen_roots.add(relative_root)
        specs.append(
            {
                "run_index": index,
                "model_id": run["model_id"],
                "novel_id": run["novel_id"],
                "run_sha256": run["run_sha256"],
                "run_plan_sha256": run["run_plan_sha256"],
                "s0_identity_sha256": run["s0_identity_sha256"],
                "source_sha256": run["source_sha256"],
                "chapter_library_sha256": run["chapter_library_sha256"],
                "profile": run["profile"],
                "audit_interval": run["audit_interval"],
                "chapter_numbering": run["chapter_numbering"],
                "stage1_chapters": list(run["stage1_chapters"]),
                "stage1_audit_checkpoints": list(
                    run["stage1_audit_checkpoints"]
                ),
                "relative_root": relative_root,
            }
        )
    return tuple(specs)


def g7c_integration_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider Stage-1 integration contract."""
    g7a, g7b = _validate_predecessors()
    specs = stage1_run_specs()

    if tuple(item["model_id"] for item in g7b["model_policies"]) != (
        BENCHMARK_MODEL_IDS
    ):
        raise G7CIntegrationError("G7B model policy roster/order changed")

    return {
        "schema_version": G7C_SCHEMA_VERSION,
        "revision": G7C_REVISION,
        "repository_anchor": G7C_REPOSITORY_ANCHOR,
        "predecessor": {
            "g7a_readiness_sha256": EXPECTED_G7A_READINESS_SHA256,
            "g7b_policy_sha256": EXPECTED_G7B_POLICY_SHA256,
        },
        "benchmark_id": g7a["benchmark_id"],
        "stage1": {
            "run_count": len(specs),
            "first_chapter": 1,
            "last_chapter": STAGE1_LAST_CHAPTER,
            "audit_checkpoints": g7a["stage1"]["audit_checkpoints"],
            "all_six_models_start_stage1": True,
            "real_production_lifecycle_required": True,
            "external_s0_roots_required": True,
            "durable_paid_request_journal_required": True,
            "provider_calls_authorized": False,
            "model_ranking_or_elimination_performed": False,
            "finalist_continuation_authorized": False,
        },
        "real_lifecycle_binding": {
            "engine_script": PRODUCTION_SCRIPT_NAME,
            "engine_loader": "full_pipeline_luna_runtime.load_experiment_engine_module",
            "provider_suppressed_initializer": (
                "full_pipeline_luna_runtime.initialize_engine_without_provider"
            ),
            "entrypoint": "run_unattended_production",
            "shared_lifecycle": "production_lifecycle.run_unattended_lifecycle",
            "duplicate_translation_pipeline_allowed": False,
        },
        "journal_contract": {
            "journal_module": "production_paid_request_journal",
            "states": [
                "planned",
                "started",
                "response_recorded",
                "uncertain",
            ],
            "response_recorded_replays_without_provider": True,
            "started_or_uncertain_blocks_reissue": True,
            "exactly_once_claimed": False,
            "logical_operations": list(PURE_RUN_LIFECYCLE_OPERATIONS),
            "direct_legacy_mapping": dict(DIRECT_LEGACY_OPERATION_MAP),
            "context_entrypoints": dict(CONTEXT_ENTRYPOINTS),
            "auxiliary_subrequest_slot": (
                "g7c_<logical_operation>_<request_sha256_prefix>"
            ),
            "normalized_wire_request_is_journal_identity": True,
        },
        "runtime_contract": {
            "same_model_all_provider_operations": True,
            "g7b_reasoning_policy_required": True,
            "provider_model_identity_from_g5c": True,
            "provider_usage_required": True,
            "hidden_retries": 0,
            "proof_runtime_transport": "synthetic_only",
            "real_provider_transport_present": False,
        },
        "external_runs": list(specs),
        "resolved_by_g7c": list(RESOLVED_G7B_INTEGRATION_BLOCKERS),
        "newly_exposed_paid_execution_blockers": list(
            PAID_STAGE1_REMAINING_BLOCKERS
        ),
        "execution_gate": {
            "status": "blocked",
            "reason": "provider_accounting_and_paid_authorization_not_frozen",
            "required_before_paid_stage1": list(
                PAID_STAGE1_REMAINING_BLOCKERS
            ),
        },
        "provider_calls": 0,
    }


G7C_INTEGRATION_SHA256 = _sha256_json(g7c_integration_identity())


def assert_paid_stage1_blocked() -> None:
    identity = g7c_integration_identity()
    if identity["execution_gate"]["status"] != "blocked":
        raise G7CIntegrationError("G7C must keep paid Stage-1 blocked")
    if identity["stage1"]["provider_calls_authorized"] is not False:
        raise G7CIntegrationError("G7C must authorize zero Provider calls")
    if identity["provider_calls"] != 0:
        raise G7CIntegrationError("G7C must report zero Provider calls")


def _source_file_for_project(project_root: Path) -> Path:
    config = load_novel_config(project_root / "novel.json")
    return project_root / "source" / f"novel.{config.source_type}"


def _validate_source_project(
    production_root: Path,
    novel_id: str,
) -> dict[str, Any]:
    project = production_root / "novels" / novel_id
    if not project.is_dir():
        raise G7CIntegrationError(f"production project missing: {project}")
    config = load_novel_config(project / "novel.json")
    if config.id != novel_id:
        raise G7CIntegrationError(
            f"project path {novel_id!r} contains config id {config.id!r}"
        )
    binding = SOURCE_BINDINGS[novel_id]
    if config.profile != binding["profile"]:
        raise G7CIntegrationError(f"profile changed for {novel_id}")
    if config.audit_interval != binding["audit_interval"]:
        raise G7CIntegrationError(f"audit interval changed for {novel_id}")
    if config.chapter_numbering != binding["chapter_numbering"]:
        raise G7CIntegrationError(f"chapter numbering changed for {novel_id}")
    if config.source_language != binding["source_language"]:
        raise G7CIntegrationError(f"source language changed for {novel_id}")
    if config.target_language != binding["target_language"]:
        raise G7CIntegrationError(f"target language changed for {novel_id}")

    source = _source_file_for_project(project)
    chapters_path = project / "data" / "chapters.json"
    if _sha256_file(source) != binding["source_sha256"]:
        raise G7CIntegrationError(f"source SHA changed for {novel_id}")
    if _sha256_file(chapters_path) != binding["chapter_library_sha256"]:
        raise G7CIntegrationError(f"chapter library SHA changed for {novel_id}")

    chapters = _read_json(chapters_path, f"{novel_id} chapter library")
    errors = validate_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    if errors:
        raise G7CIntegrationError(
            f"invalid chapter library for {novel_id}: " + "; ".join(errors)
        )
    chapter_map = index_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    missing = [
        number
        for number in range(1, STAGE1_LAST_CHAPTER + 1)
        if number not in chapter_map
    ]
    if missing:
        raise G7CIntegrationError(
            f"{novel_id} lacks Stage-1 chapter indexes: {missing}"
        )

    addendum = project / "translation_addendum.txt"
    return {
        "project": project,
        "config": config,
        "source": source,
        "chapters": chapters_path,
        "addendum": addendum,
        "config_sha256": _sha256_file(project / "novel.json"),
        "source_sha256": _sha256_file(source),
        "chapter_library_sha256": _sha256_file(chapters_path),
        "addendum_sha256": (
            _sha256_file(addendum) if addendum.is_file() else _sha256_bytes(b"")
        ),
    }


def _write_s0_run(
    destination: Path,
    *,
    source_project: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    for relative in PROJECT_DIRECTORIES:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    (destination / "data" / "glossary_candidate_resolutions").mkdir(
        parents=True, exist_ok=True
    )
    (destination / "data" / "provider_request_journal").mkdir(
        parents=True, exist_ok=True
    )

    source_root = Path(source_project["project"])
    shutil.copyfile(source_root / "novel.json", destination / "novel.json")
    addendum = Path(source_project["addendum"])
    if addendum.is_file():
        shutil.copyfile(addendum, destination / "translation_addendum.txt")
    else:
        (destination / "translation_addendum.txt").write_text(
            "", encoding="utf-8", newline="\n"
        )

    source_file = Path(source_project["source"])
    staged_source = (
        destination
        / "source"
        / f"novel.{source_project['config'].source_type}"
    )
    shutil.copyfile(source_file, staged_source)

    state = initial_project_state()
    state["progress.json"] = copy.deepcopy(state["progress.json"])
    state["progress.json"]["pricing_version"] = (
        "command-code-provider-stage1-accounting-pending-v1"
    )
    for name, value in state.items():
        if name == "chapters.json":
            continue
        atomic_write_json(destination / "data" / name, value)
    shutil.copyfile(
        Path(source_project["chapters"]),
        destination / "data" / "chapters.json",
    )

    run_identity = {
        "schema_version": G7C_SCHEMA_VERSION,
        "g7c_revision": G7C_REVISION,
        "g7c_integration_sha256": G7C_INTEGRATION_SHA256,
        **{
            key: copy.deepcopy(spec[key])
            for key in (
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
            )
        },
        "source_config_sha256": source_project["config_sha256"],
        "source_addendum_sha256": source_project["addendum_sha256"],
        "provider_calls": 0,
        "state": "s0_unexecuted",
    }
    run_identity["identity_sha256"] = _sha256_json(run_identity)
    atomic_write_json(destination / RUN_IDENTITY_FILENAME, run_identity)
    return run_identity


def prepare_external_s0_roots(
    *,
    production_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Atomically create the twelve real-schema external Stage-1 S0 roots."""
    production = Path(production_root).resolve()
    output = Path(output_root).resolve()
    code_root = Path(__file__).resolve().parent
    if output.exists():
        raise G7CIntegrationError(
            f"G7C Stage-1 root already exists and is never overwritten: {output}"
        )
    if _path_is_within(output, production):
        raise G7CIntegrationError("Stage-1 roots must be external to production")
    if _path_is_within(output, code_root):
        raise G7CIntegrationError("Stage-1 roots must be external to the code worktree")

    specs = stage1_run_specs()
    novel_ids = tuple(dict.fromkeys(spec["novel_id"] for spec in specs))
    production_guard = capture_production_guard(production, novel_ids)
    source_projects = {
        novel_id: _validate_source_project(production, novel_id)
        for novel_id in novel_ids
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        run_records = []
        for spec in specs:
            destination = staging / Path(spec["relative_root"])
            run_identity = _write_s0_run(
                destination,
                source_project=source_projects[spec["novel_id"]],
                spec=spec,
            )
            run_records.append(
                {
                    "relative_root": spec["relative_root"],
                    "run_identity_sha256": run_identity["identity_sha256"],
                    "model_id": spec["model_id"],
                    "novel_id": spec["novel_id"],
                    "run_sha256": spec["run_sha256"],
                    "s0_identity_sha256": spec["s0_identity_sha256"],
                }
            )

        source_bindings = {
            novel_id: {
                "config_sha256": source_projects[novel_id]["config_sha256"],
                "source_sha256": source_projects[novel_id]["source_sha256"],
                "chapter_library_sha256": source_projects[novel_id][
                    "chapter_library_sha256"
                ],
                "addendum_sha256": source_projects[novel_id]["addendum_sha256"],
            }
            for novel_id in novel_ids
        }
        manifest = {
            "schema_version": G7C_SCHEMA_VERSION,
            "revision": G7C_REVISION,
            "g7c_integration_sha256": G7C_INTEGRATION_SHA256,
            "g7a_readiness_sha256": EXPECTED_G7A_READINESS_SHA256,
            "g7b_policy_sha256": EXPECTED_G7B_POLICY_SHA256,
            "run_count": len(run_records),
            "source_bindings": source_bindings,
            "production_guard": production_guard,
            "runs": run_records,
            "provider_calls": 0,
            "paid_stage1_authorized": False,
        }
        manifest["manifest_sha256"] = _sha256_json(manifest)
        atomic_write_json(staging / ROOTS_MANIFEST_FILENAME, manifest)

        current_guard = capture_production_guard(production, novel_ids)
        if current_guard != production_guard:
            raise G7CIntegrationError(
                "production projects changed while preparing Stage-1 roots"
            )
        if output.exists():
            raise G7CIntegrationError("Stage-1 output root appeared during creation")
        os.rename(staging, output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return verify_external_s0_roots(
        production_root=production,
        output_root=output,
    )


def verify_external_s0_roots(
    *,
    production_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Verify all twelve roots remain independent, native, and unexecuted."""
    production = Path(production_root).resolve()
    output = Path(output_root).resolve()
    if not output.is_dir():
        raise G7CIntegrationError(f"Stage-1 root missing: {output}")

    manifest = _read_object(
        output / ROOTS_MANIFEST_FILENAME,
        "G7C Stage-1 roots manifest",
    )
    check = dict(manifest)
    observed_manifest_sha = check.pop("manifest_sha256", None)
    if _sha256_json(check) != observed_manifest_sha:
        raise G7CIntegrationError("G7C roots manifest SHA does not recompute")
    if manifest.get("g7c_integration_sha256") != G7C_INTEGRATION_SHA256:
        raise G7CIntegrationError("G7C roots bind a different integration identity")
    if manifest.get("run_count") != 12:
        raise G7CIntegrationError("G7C roots must contain twelve runs")
    if manifest.get("provider_calls") != 0:
        raise G7CIntegrationError("S0 preparation must report zero Provider calls")
    if manifest.get("paid_stage1_authorized") is not False:
        raise G7CIntegrationError("S0 preparation cannot authorize paid Stage-1")

    specs = stage1_run_specs()
    expected_relatives = {spec["relative_root"] for spec in specs}
    observed_relatives = {
        item.get("relative_root")
        for item in manifest.get("runs", [])
        if isinstance(item, Mapping)
    }
    if observed_relatives != expected_relatives:
        raise G7CIntegrationError("external run-root set changed")

    for spec in specs:
        root = output / Path(spec["relative_root"])
        if not root.is_dir():
            raise G7CIntegrationError(f"run root missing: {root}")
        identity = _read_object(root / RUN_IDENTITY_FILENAME, "run identity")
        if identity.get("model_id") != spec["model_id"]:
            raise G7CIntegrationError(f"model identity changed in {root}")
        if identity.get("novel_id") != spec["novel_id"]:
            raise G7CIntegrationError(f"novel identity changed in {root}")
        if identity.get("run_sha256") != spec["run_sha256"]:
            raise G7CIntegrationError(f"run SHA changed in {root}")
        if identity.get("s0_identity_sha256") != spec["s0_identity_sha256"]:
            raise G7CIntegrationError(f"S0 identity changed in {root}")
        identity_check = dict(identity)
        stored_identity_sha = identity_check.pop("identity_sha256", None)
        if _sha256_json(identity_check) != stored_identity_sha:
            raise G7CIntegrationError(f"run identity SHA does not recompute: {root}")

        config = load_novel_config(root / "novel.json")
        binding = SOURCE_BINDINGS[spec["novel_id"]]
        source_path = root / "source" / f"novel.{config.source_type}"
        if _sha256_file(source_path) != binding["source_sha256"]:
            raise G7CIntegrationError(f"staged source changed in {root}")
        if _sha256_file(root / "data" / "chapters.json") != binding[
            "chapter_library_sha256"
        ]:
            raise G7CIntegrationError(f"staged chapters changed in {root}")

        expected_state = initial_project_state()
        for name, expected in expected_state.items():
            if name == "chapters.json":
                continue
            actual = _read_json(root / "data" / name, f"{name} in {root}")
            if name == "progress.json":
                expected = copy.deepcopy(expected)
                expected["pricing_version"] = (
                    "command-code-provider-stage1-accounting-pending-v1"
                )
            if actual != expected:
                raise G7CIntegrationError(f"{name} is not pristine S0 in {root}")

        for relative in (
            "output/chapters",
            "output/pending",
            "output/qa",
            "output/audits",
            "output/retroactive_fixes",
            "data/provider_request_journal",
            "data/glossary_candidate_resolutions",
        ):
            directory = root / relative
            if not directory.is_dir():
                raise G7CIntegrationError(f"required S0 directory missing: {directory}")
            if any(directory.iterdir()):
                raise G7CIntegrationError(f"S0 directory is not empty: {directory}")

    novel_ids = tuple(dict.fromkeys(spec["novel_id"] for spec in specs))
    # The shared mutable-tree guard intentionally excludes source/; revalidate
    # the frozen source/chapter identities here as well so a source-byte change
    # can never slip past G7C merely because it is immutable in production.
    for novel_id in novel_ids:
        _validate_source_project(production, novel_id)

    current_guard = capture_production_guard(production, novel_ids)
    if current_guard != manifest.get("production_guard"):
        raise G7CIntegrationError(
            "production projects changed since external S0 root binding"
        )

    return {
        "manifest": manifest,
        "manifest_sha256": observed_manifest_sha,
        "run_count": len(specs),
        "production_guard_verified": True,
        "provider_calls": 0,
        "paid_stage1_authorized": False,
    }


def _validate_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        raise G7CIntegrationError("accounting callback must return a mapping")
    value = copy.deepcopy(dict(stats))
    required_nonnegative = (
        "prompt_tokens",
        "completion_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cost_usd",
        "rate_correct_cost_usd",
    )
    for name in required_nonnegative:
        item = value.get(name)
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or item < 0
        ):
            raise G7CIntegrationError(
                f"accounting callback returned invalid {name}"
            )
    if not isinstance(value.get("pricing_version"), str) or not value[
        "pricing_version"
    ].strip():
        raise G7CIntegrationError(
            "accounting callback must provide a pricing_version"
        )
    return value


def _response_from_envelope(envelope: Mapping[str, Any]) -> Any:
    stats = _validate_stats(envelope["stats"])
    usage = SimpleNamespace(
        prompt_tokens=int(stats["prompt_tokens"]),
        completion_tokens=int(stats["completion_tokens"]),
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=int(stats["cache_hit_tokens"])
        ),
        _g7c_stats=stats,
    )
    return SimpleNamespace(
        id=envelope.get("provider_response_id", ""),
        model=envelope.get("provider_response_model", ""),
        choices=[
            SimpleNamespace(
                finish_reason=envelope.get("finish_reason"),
                message=SimpleNamespace(content=envelope.get("content", "")),
            )
        ],
        usage=usage,
    )


def normalize_legacy_request(
    *,
    model_id: str,
    logical_operation: str,
    request_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert a legacy production request into the frozen Command Code shape."""
    if logical_operation not in PURE_RUN_LIFECYCLE_OPERATIONS:
        raise G7CIntegrationError(
            f"unsupported lifecycle operation {logical_operation!r}"
        )
    if not isinstance(request_kwargs, Mapping):
        raise G7CIntegrationError("legacy request kwargs must be a mapping")

    policy = reasoning_by_operation(model_id)
    if logical_operation not in policy:
        raise G7CIntegrationError(
            f"G7B policy lacks operation {logical_operation!r}"
        )
    effort = policy[logical_operation]
    live = live_provider_capability(model_id)

    request = copy.deepcopy(dict(request_kwargs))
    legacy_model = request.pop("model", None)
    if legacy_model not in (model_id, live.provider_model_id):
        raise G7CIntegrationError(
            f"legacy request model {legacy_model!r} does not match "
            f"{model_id!r}/{live.provider_model_id!r}"
        )
    messages = request.pop("messages", None)

    max_tokens = request.pop("max_tokens", None)
    max_completion_tokens = request.pop("max_completion_tokens", None)
    if (
        max_tokens is not None
        and max_completion_tokens is not None
        and max_tokens != max_completion_tokens
    ):
        raise G7CIntegrationError(
            "legacy request has conflicting max token controls"
        )
    output_tokens = (
        max_completion_tokens
        if max_completion_tokens is not None
        else max_tokens
    )

    legacy_extra_body = request.pop("extra_body", None)
    if legacy_extra_body not in (
        None,
        {"thinking": {"type": "disabled"}},
    ):
        raise G7CIntegrationError(
            "unexpected legacy provider-specific extra_body"
        )
    if "reasoning_effort" in request:
        raise G7CIntegrationError(
            "legacy request must not pre-populate reasoning_effort; G7B owns it"
        )

    capability = verified_reasoning_capability(model_id)
    if effort is None:
        if capability is not None:
            raise G7CIntegrationError(
                f"adjustable model {model_id!r} cannot silently use default effort"
            )
        if not live.default_effort_request_verified:
            raise G7CIntegrationError(
                f"default request was not live-verified for {model_id!r}"
            )
    else:
        if capability is None:
            raise G7CIntegrationError(
                f"explicit effort {effort!r} lacks verified capability"
            )

    return build_provider_request(
        model=live.provider_model_id,
        messages=messages,
        max_output_tokens=output_tokens,
        request_extras=request,
        reasoning_capability=capability if effort is not None else None,
        reasoning_level=effort,
    )


def _logical_operation_kind(
    logical_operation: str,
    request_sha256: str,
    *,
    multi_request_slot: bool,
) -> str:
    if logical_operation not in PURE_RUN_LIFECYCLE_OPERATIONS:
        raise G7CIntegrationError("unknown logical operation")
    if not multi_request_slot:
        # Translation/state operations are one logical paid slot per chapter.
        # A changed request must collide with the existing fixed journal path
        # and fail identity validation rather than creating a second paid slot.
        return f"g7c_{logical_operation}"
    # Glossary/audit/repair protocols can legitimately make multiple distinct
    # bounded subrequests in one chapter.  Their normalized request identity
    # creates deterministic independent slots; an exact retry reuses its slot.
    return f"g7c_{logical_operation}_{request_sha256[:20]}"


def install_stage1_synthetic_proof_runtime(
    production_module,
    *,
    model_id: str,
    synthetic_send_once: Callable[[Mapping[str, Any]], Any],
    usage_to_stats: Callable[[str, str, Any], Mapping[str, Any]],
    validate_saved_stats: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Install a journal-protected synthetic runtime into the real engine.

    This function intentionally accepts no API key and no Provider client.  The
    caller-supplied transport is for deterministic local/CI proof responses only.
    A later paid phase must provide a separately reviewed real transport and
    accounting policy.
    """
    if not getattr(production_module, "_RUNTIME_INITIALIZED", False):
        raise G7CIntegrationError(
            "real production engine must be locally initialized first"
        )
    if model_id not in BENCHMARK_MODEL_IDS:
        raise G7CIntegrationError(f"unknown benchmark model {model_id!r}")
    for label, callback in (
        ("synthetic_send_once", synthetic_send_once),
        ("usage_to_stats", usage_to_stats),
        ("validate_saved_stats", validate_saved_stats),
    ):
        if not callable(callback):
            raise G7CIntegrationError(f"{label} must be callable")

    existing = getattr(production_module, "_g7c_stage1_runtime_state", None)
    if isinstance(existing, dict):
        if existing.get("model_id") != model_id:
            raise G7CIntegrationError(
                "G7C runtime already installed for another model"
            )
        return existing

    live = live_provider_capability(model_id)
    operation_stack: list[tuple[str, int]] = []

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
            raise G7CIntegrationError(
                "provider-shaped auxiliary request occurred outside an "
                "explicit G7C lifecycle operation context"
            )
        return operation_stack[-1]

    @contextmanager
    def operation_context(logical_operation: str, chapter: int):
        if logical_operation not in PURE_RUN_LIFECYCLE_OPERATIONS:
            raise G7CIntegrationError(
                f"invalid G7C operation context {logical_operation!r}"
            )
        if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
            raise G7CIntegrationError("operation context chapter must be positive")
        operation_stack.append((logical_operation, chapter))
        try:
            yield
        finally:
            popped = operation_stack.pop()
            if popped != (logical_operation, chapter):
                raise G7CIntegrationError("G7C operation context stack corrupted")

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
        provider_identity = build_provider_request_identity(
            request_body=actual_request
        )
        novel_id = (
            production_module.ACTIVE_NOVEL_CONFIG.id
            if production_module.ACTIVE_NOVEL_CONFIG is not None
            else "unknown"
        )
        profile_id = (
            getattr(production_module.ACTIVE_PROFILE, "id", "")
            or "unknown"
        )
        source_sha256 = _sha256_text(source_text)
        translation_sha256 = (
            _sha256_text(translation_text) if translation_text else ""
        )
        journal_identity = production_module.request_identity(
            operation_kind=logical_operation,
            novel_id=novel_id,
            chapter=chapter_number,
            provider=PROVIDER_NAME,
            model=live.provider_model_id,
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
        journal_path = production_module.paid_request_journal_path(
            production_module.PAID_REQUEST_JOURNAL_DIR,
            journal_kind,
            chapter_number,
        )

        def normalize_response(raw, _attempt_metadata):
            normalized = normalize_provider_response(
                raw,
                request_identity=provider_identity,
            )
            accepted = accept_provider_response(
                provider_identity,
                normalized,
            )
            stats = _validate_stats(
                usage_to_stats(
                    model_id,
                    logical_operation,
                    accepted.usage,
                )
            )
            if stats["pricing_version"] != PROOF_ACCOUNTING_VERSION:
                raise G7CIntegrationError(
                    "G7C proof runtime accepts only explicit synthetic-proof "
                    "accounting; real paid accounting belongs to a later phase"
                )
            return {
                "content": accepted.content,
                "finish_reason": accepted.finish_reason,
                "stats": stats,
                "provider_response_id": accepted.provider_response_id,
                "provider_response_model": accepted.response_model,
                "request_sha256": provider_identity.request_sha256,
                "logical_operation": logical_operation,
            }

        result = production_module.run_journaled_paid_request(
            journal_path=journal_path,
            identity=journal_identity,
            save_json=lambda path, value: production_module.atomic_write_json(
                path, value
            ),
            prepare_attempt=lambda: {
                "mode": "g7c_synthetic_proof",
                "pricing_version": PROOF_ACCOUNTING_VERSION,
            },
            send_once=lambda: synthetic_send_once(copy.deepcopy(actual_request)),
            normalize_response=normalize_response,
        )
        response = result.get("response") if isinstance(result, dict) else None
        if not isinstance(response, dict):
            raise G7CIntegrationError(
                "durable journal returned no response envelope"
            )
        if response.get("provider_response_model") != live.provider_model_id:
            raise G7CIntegrationError(
                "journaled response model does not match frozen Provider model"
            )
        return result

    def journaled_chat_request(
        operation_kind,
        chapter_number,
        source_text,
        request_kwargs,
        *,
        translation_text="",
    ):
        try:
            logical = DIRECT_LEGACY_OPERATION_MAP[operation_kind]
        except KeyError as exc:
            raise G7CIntegrationError(
                f"unmapped direct legacy operation {operation_kind!r}"
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
            raise G7CIntegrationError("auxiliary request lacks messages")
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
        return _response_from_envelope(result["response"])

    def call_api_with_retries(**kwargs):
        # Legacy name only.  There is exactly one synthetic transport attempt
        # inside run_journaled_paid_request, never an application retry loop.
        return auxiliary_request(kwargs)

    def send_api_request_once(**kwargs):
        # Direct legacy sends (candidate adjudication) are still journaled.
        return auxiliary_request(kwargs)

    def usage_and_cost(usage):
        stats = getattr(usage, "_g7c_stats", None)
        if stats is None:
            raise G7CIntegrationError(
                "G7C response usage lacks bound accounting stats"
            )
        return _validate_stats(stats)

    def usage_and_cost_for_pricing(usage, _pricing):
        return usage_and_cost(usage)

    def reprice_saved_stats_if_needed(stats):
        return _validate_stats(validate_saved_stats(copy.deepcopy(dict(stats))))

    def pricing_snapshot(_moment=None):
        return {
            "pricing_version": PROOF_ACCOUNTING_VERSION,
            "pricing_period": "synthetic_proof",
            "request_started_utc": "",
            "cache_hit_rate_per_million": 0.0,
            "cache_miss_rate_per_million": 0.0,
            "output_rate_per_million": 0.0,
        }

    production_module.MODEL = live.provider_model_id
    production_module.client = None
    production_module.api_key = None
    production_module.AUTO_OFF_PEAK_ONLY = False
    production_module.journaled_chat_request = journaled_chat_request
    production_module.call_api_with_retries = call_api_with_retries
    production_module.send_api_request_once = send_api_request_once
    production_module.usage_and_cost = usage_and_cost
    production_module.usage_and_cost_for_pricing = usage_and_cost_for_pricing
    production_module.reprice_saved_stats_if_needed = reprice_saved_stats_if_needed
    production_module.pricing_snapshot = pricing_snapshot

    wrapped_entrypoints = {}
    for name, logical_operation in CONTEXT_ENTRYPOINTS.items():
        original = originals[name]

        def make_wrapper(original_callable, operation):
            def wrapped(*args, **kwargs):
                if args:
                    chapter = args[0]
                else:
                    chapter = kwargs.get("chapter_number")
                    if chapter is None and operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT:
                        chapter = kwargs.get("end_chapter")
                if (
                    operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT
                    and len(args) >= 2
                ):
                    chapter = args[1]
                with operation_context(operation, int(chapter)):
                    return original_callable(*args, **kwargs)
            return wrapped

        wrapper = make_wrapper(original, logical_operation)
        setattr(production_module, name, wrapper)
        wrapped_entrypoints[name] = wrapper

    state = {
        "mode": "g7c_synthetic_stage1_integration_proof",
        "model_id": model_id,
        "provider_model_id": live.provider_model_id,
        "reasoning_by_operation": dict(reasoning_by_operation(model_id)),
        "journaled_operations": list(PURE_RUN_LIFECYCLE_OPERATIONS),
        "context_entrypoints": dict(CONTEXT_ENTRYPOINTS),
        "provider_calls_authorized": False,
        "real_provider_transport_present": False,
        "accounting_policy_frozen": False,
        "originals": originals,
        "wrapped_entrypoints": wrapped_entrypoints,
    }
    production_module._g7c_stage1_runtime_state = state
    return state


def prove_real_engine_binding(
    *,
    code_root: Path | str,
    project_root: Path | str,
    module_name: str | None = None,
) -> dict[str, Any]:
    """Load/initialize the real engine in one external S0 root with no provider."""
    code = Path(code_root).resolve()
    project = Path(project_root).resolve()
    production_script = code / PRODUCTION_SCRIPT_NAME
    if not production_script.is_file():
        raise G7CIntegrationError(
            f"real production script missing: {production_script}"
        )
    if _path_is_within(project, code):
        raise G7CIntegrationError(
            "G7C integration proof project must be external to code worktree"
        )

    old_cwd = Path.cwd()
    try:
        os.chdir(project)
        loaded = load_experiment_engine_module(
            production_script,
            module_name=module_name,
        )
        module = loaded["module"]
        initialized = initialize_engine_without_provider(module)
    finally:
        os.chdir(old_cwd)

    for name in (
        "process_chapter",
        "run_unattended_production",
        "run_due_audits",
        "journaled_chat_request",
        "run_journaled_paid_request",
    ):
        if not callable(getattr(module, name, None)):
            raise G7CIntegrationError(
                f"real engine integration callable missing: {name}"
            )
    if module.client is not None or module.api_key is not None:
        raise G7CIntegrationError(
            "provider-suppressed real engine retained credentials/client"
        )
    return {
        "module": module,
        "module_name": loaded["module_name"],
        "initialized": initialized,
        "production_script_sha256": _sha256_file(production_script),
        "real_lifecycle_bound": True,
        "provider_constructor_suppressed": True,
        "provider_calls": 0,
    }


def build_proof_stats(
    _model_id: str,
    _logical_operation: str,
    usage: Any,
) -> dict[str, Any]:
    """Synthetic accounting only; never valid for a real paid Stage-1 call."""
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = getattr(usage, "cached_tokens", None)
    cached = int(cached or 0)
    if cached > prompt:
        raise G7CIntegrationError("synthetic cached tokens exceed prompt tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": cached,
        "cache_miss_tokens": prompt - cached,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
        "pricing_version": PROOF_ACCOUNTING_VERSION,
        "pricing_period": "synthetic_proof",
        "request_started_utc": "",
        "cache_hit_rate_per_million": 0.0,
        "cache_miss_rate_per_million": 0.0,
        "output_rate_per_million": 0.0,
    }


__all__ = [
    "CONTEXT_ENTRYPOINTS",
    "DIRECT_LEGACY_OPERATION_MAP",
    "EXPECTED_G7B_POLICY_SHA256",
    "G7C_INTEGRATION_SHA256",
    "G7C_REVISION",
    "PAID_STAGE1_REMAINING_BLOCKERS",
    "RESOLVED_G7B_INTEGRATION_BLOCKERS",
    "ROOTS_MANIFEST_FILENAME",
    "RUN_IDENTITY_FILENAME",
    "assert_paid_stage1_blocked",
    "build_proof_stats",
    "g7c_integration_identity",
    "install_stage1_synthetic_proof_runtime",
    "normalize_legacy_request",
    "prepare_external_s0_roots",
    "prove_real_engine_binding",
    "stage1_run_specs",
    "verify_external_s0_roots",
]
