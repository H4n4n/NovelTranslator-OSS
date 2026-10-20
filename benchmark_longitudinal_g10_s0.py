"""Zero-provider G10B1 fresh-S0 root preparation and verification.

This module creates the four independent Qwen/GLM x Ace/Rogue chronological
project roots required by G10B. It copies authoritative production inputs into
native NovelTranslator project layouts while resetting ALL mutable translation
state to pristine S0.

Safety properties:
- output root must be external to production, code worktree, and forbidden roots;
- creation is create-once through a sibling staging directory then atomic rename;
- production mutable/config trees are fingerprinted before and after creation;
- source file, chapter library, addendum, and full canonical config identities
  are independently bound and byte-verified;
- every run identity and the roots manifest are self-hashed;
- all four roots are verified pristine before the operation returns;
- no Provider client, credential, network call, or paid authorization exists here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
import uuid

from benchmark_longitudinal_g10_chronological import (
    G10B1_CONTRACT_SHA256,
    G10B1_LAST_CHAPTER,
    TRACK_SPECS,
)
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    NOVELS,
)
from benchmark_longitudinal_g10_safety import (
    G10A1_SAFETY_SHA256,
    assert_fresh_execution_root,
)
from chapter_library import index_chapter_library, validate_chapter_library
from create_novel import PROJECT_DIRECTORIES, initial_project_state
from full_pipeline_experiment_harness import capture_production_guard
from novel_project import SOURCE_TYPE_TXT, load_novel_config
from noveltranslator_core import atomic_write_json


G10S0_SCHEMA_VERSION = 1
G10S0_REVISION = "g10-four-track-fresh-s0-v1"
G10S0_MANIFEST_FILENAME = "g10_s0_roots_manifest.json"
G10S0_RUN_IDENTITY_FILENAME = "g10_s0_run_identity.json"
G10S0_PRICING_VERSION = "g10-command-code-accounting-pending-v1"
G10S0_RUN_COUNT = 4


class G10S0Error(RuntimeError):
    """G10 S0 source binding, creation, or verification is unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10S0Error("G10 S0 value is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path | str) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise G10S0Error(f"cannot hash file: {path}") from exc


def _read_json(path: Path | str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10S0Error(f"cannot read {label}: {path}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise G10S0Error(f"{label} must contain a JSON object")
    return value


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_tree_has_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G10S0Error(f"G10 S0 root must not be a symlink: {root}")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G10S0Error(f"G10 S0 tree contains a symlink: {item}")


def _source_file(project_root: Path, source_type: str) -> Path:
    suffix = "txt" if source_type == SOURCE_TYPE_TXT else "epub"
    return project_root / "source" / f"novel.{suffix}"


def _canonical_config_sha256(path: Path) -> str:
    value = _read_object(path, "novel config")
    return _sha256_json(value)


def _validate_source_project(
    production_root: Path,
    novel_id: str,
) -> dict[str, Any]:
    project = production_root / "novels" / novel_id
    if not project.is_dir():
        raise G10S0Error(f"production project missing: {project}")
    config_path = project / "novel.json"
    config = load_novel_config(config_path)
    if config.id != novel_id:
        raise G10S0Error(
            f"project path {novel_id!r} contains config id {config.id!r}"
        )

    frozen = NOVELS[novel_id]
    checks = {
        "source_language": frozen["source_language"],
        "target_language": frozen["target_language"],
        "profile": frozen["profile"],
        "final_chapter": frozen["final_chapter"],
        "audit_interval": frozen["audit_interval"],
        "chapter_numbering": frozen["chapter_numbering"],
    }
    for field, expected in checks.items():
        if getattr(config, field) != expected:
            raise G10S0Error(
                f"{novel_id} production config drifted for {field}"
            )
    canonical_config_sha = _canonical_config_sha256(config_path)
    if canonical_config_sha != frozen["config_canonical_sha256"]:
        raise G10S0Error(
            f"{novel_id} canonical novel config differs from G10A0 binding"
        )

    source = _source_file(project, config.source_type)
    chapters_path = project / "data" / "chapters.json"
    addendum = project / "translation_addendum.txt"
    for label, path in {
        "source": source,
        "chapter library": chapters_path,
        "translation addendum": addendum,
    }.items():
        if not path.is_file():
            raise G10S0Error(f"{novel_id} {label} is missing: {path}")

    chapters = _read_json(chapters_path, f"{novel_id} chapter library")
    errors = validate_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    if errors:
        raise G10S0Error(
            f"invalid chapter library for {novel_id}: " + "; ".join(errors)
        )
    chapter_map = index_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    missing = [
        number
        for number in range(1, G10B1_LAST_CHAPTER + 1)
        if number not in chapter_map
    ]
    if missing:
        raise G10S0Error(
            f"{novel_id} lacks G10B Chapter indexes: {missing}"
        )

    return {
        "project": project,
        "config": config,
        "config_path": config_path,
        "source": source,
        "chapters": chapters_path,
        "addendum": addendum,
        "config_sha256": _sha256_file(config_path),
        "config_canonical_sha256": canonical_config_sha,
        "source_sha256": _sha256_file(source),
        "chapter_library_sha256": _sha256_file(chapters_path),
        "addendum_sha256": _sha256_file(addendum),
    }


def _relative_run_root(run_id: str) -> Path:
    if not isinstance(run_id, str) or not run_id.startswith("g10b-run"):
        raise G10S0Error(f"unsafe G10B run id: {run_id!r}")
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in run_id):
        raise G10S0Error(f"unsafe G10B run id characters: {run_id!r}")
    return Path("runs") / run_id


def _copy_exact(source: Path, destination: Path, label: str) -> None:
    shutil.copyfile(source, destination)
    if destination.read_bytes() != source.read_bytes():
        raise G10S0Error(f"{label} copy differs from authoritative source")


def _expected_state() -> dict[str, Any]:
    state = initial_project_state()
    state["progress.json"] = copy.deepcopy(state["progress.json"])
    state["progress.json"]["pricing_version"] = G10S0_PRICING_VERSION
    return state


def _write_run(
    destination: Path,
    *,
    source_project: Mapping[str, Any],
    track,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    for relative in PROJECT_DIRECTORIES:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    (destination / "data" / "glossary_candidate_resolutions").mkdir(
        parents=True,
        exist_ok=True,
    )
    (destination / "data" / "provider_request_journal").mkdir(
        parents=True,
        exist_ok=True,
    )
    (destination / "data" / "g10_invocation_ledgers").mkdir(
        parents=True,
        exist_ok=True,
    )

    _copy_exact(
        Path(source_project["config_path"]),
        destination / "novel.json",
        "novel config",
    )
    _copy_exact(
        Path(source_project["addendum"]),
        destination / "translation_addendum.txt",
        "translation addendum",
    )
    source_file = Path(source_project["source"])
    staged_source = destination / "source" / source_file.name
    _copy_exact(source_file, staged_source, "source file")
    _copy_exact(
        Path(source_project["chapters"]),
        destination / "data" / "chapters.json",
        "chapter library",
    )

    for name, value in _expected_state().items():
        if name == "chapters.json":
            continue
        atomic_write_json(destination / "data" / name, value)

    relative_root = _relative_run_root(track.run_id).as_posix()
    run_identity = {
        "schema_version": G10S0_SCHEMA_VERSION,
        "revision": G10S0_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        "run_index": track.run_index,
        "run_id": track.run_id,
        "relative_root": relative_root,
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "audit_interval": track.audit_interval,
        "first_chapter": 1,
        "last_chapter": G10B1_LAST_CHAPTER,
        "audit_checkpoints": [25, 50],
        "translation_effort": track.translation_effort,
        "structured_effort": track.structured_effort,
        "semantic_effort": track.semantic_effort,
        "source_bindings": {
            key: source_project[key]
            for key in (
                "config_sha256",
                "config_canonical_sha256",
                "source_sha256",
                "chapter_library_sha256",
                "addendum_sha256",
            )
        },
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }
    run_identity["identity_sha256"] = _sha256_json(run_identity)
    atomic_write_json(
        destination / G10S0_RUN_IDENTITY_FILENAME,
        run_identity,
    )
    return run_identity


def assert_s0_output_root(
    *,
    production_root: Path | str,
    output_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> Path:
    production = Path(production_root).resolve()
    code_root = Path(__file__).resolve().parent
    output = assert_fresh_execution_root(
        output_root,
        forbidden_roots=(production, code_root, *forbidden_roots),
    )
    if _path_is_within(output, production) or _path_is_within(production, output):
        raise G10S0Error("G10 S0 root must be external to production")
    if _path_is_within(output, code_root) or _path_is_within(code_root, output):
        raise G10S0Error("G10 S0 root must be external to the code worktree")
    return output


def prepare_s0_roots(
    *,
    production_root: Path | str,
    output_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Atomically create and verify the four external pristine G10B roots."""

    production = Path(production_root).resolve()
    output = assert_s0_output_root(
        production_root=production,
        output_root=output_root,
        forbidden_roots=forbidden_roots,
    )
    if output.exists():
        raise G10S0Error(
            f"G10 S0 output root already exists and is never overwritten: {output}"
        )
    if not production.is_dir():
        raise G10S0Error(f"production root is missing: {production}")

    novel_ids = tuple(dict.fromkeys(track.novel_id for track in TRACK_SPECS))
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
        for track in TRACK_SPECS:
            relative = _relative_run_root(track.run_id)
            identity = _write_run(
                staging / relative,
                source_project=source_projects[track.novel_id],
                track=track,
            )
            run_records.append(
                {
                    "run_id": track.run_id,
                    "run_index": track.run_index,
                    "model_id": track.model_id,
                    "novel_id": track.novel_id,
                    "relative_root": relative.as_posix(),
                    "run_identity_sha256": identity["identity_sha256"],
                }
            )

        source_bindings = {
            novel_id: {
                key: source_projects[novel_id][key]
                for key in (
                    "config_sha256",
                    "config_canonical_sha256",
                    "source_sha256",
                    "chapter_library_sha256",
                    "addendum_sha256",
                )
            }
            for novel_id in novel_ids
        }
        manifest = {
            "schema_version": G10S0_SCHEMA_VERSION,
            "revision": G10S0_REVISION,
            "benchmark_id": G10_BENCHMARK_ID,
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
            "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
            "run_count": len(run_records),
            "chapters_per_track": G10B1_LAST_CHAPTER,
            "audit_checkpoints": [25, 50],
            "pricing_version": G10S0_PRICING_VERSION,
            "production_guard": production_guard,
            "source_bindings": source_bindings,
            "runs": run_records,
            "provider_calls": 0,
            "paid_execution_authorized": False,
        }
        manifest["manifest_sha256"] = _sha256_json(manifest)
        atomic_write_json(
            staging / G10S0_MANIFEST_FILENAME,
            manifest,
        )

        if capture_production_guard(production, novel_ids) != production_guard:
            raise G10S0Error(
                "production projects changed while preparing G10 S0 roots"
            )
        for novel_id in novel_ids:
            current = _validate_source_project(production, novel_id)
            for key in source_bindings[novel_id]:
                if current[key] != source_bindings[novel_id][key]:
                    raise G10S0Error(
                        f"{novel_id} source binding changed during S0 preparation"
                    )

        if output.exists():
            raise G10S0Error(
                "G10 S0 output root appeared during create-once staging"
            )
        os.rename(staging, output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return verify_s0_roots(
        production_root=production,
        output_root=output,
        forbidden_roots=forbidden_roots,
    )


def verify_s0_roots(
    *,
    production_root: Path | str,
    output_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Prove every G10B root is native, independent, pristine, and source-bound."""

    production = Path(production_root).resolve()
    output = assert_s0_output_root(
        production_root=production,
        output_root=output_root,
        forbidden_roots=forbidden_roots,
    )
    if not output.is_dir():
        raise G10S0Error(f"G10 S0 output root is missing: {output}")
    _assert_tree_has_no_symlinks(output)

    manifest = _read_object(
        output / G10S0_MANIFEST_FILENAME,
        "G10 S0 roots manifest",
    )
    check = copy.deepcopy(manifest)
    observed_manifest_sha = check.pop("manifest_sha256", None)
    if _sha256_json(check) != observed_manifest_sha:
        raise G10S0Error("G10 S0 roots manifest SHA does not recompute")
    expected_meta = {
        "schema_version": G10S0_SCHEMA_VERSION,
        "revision": G10S0_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        "run_count": G10S0_RUN_COUNT,
        "chapters_per_track": G10B1_LAST_CHAPTER,
        "audit_checkpoints": [25, 50],
        "pricing_version": G10S0_PRICING_VERSION,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }
    for key, expected in expected_meta.items():
        if manifest.get(key) != expected:
            raise G10S0Error(
                f"G10 S0 manifest binding changed for {key}"
            )

    expected_relatives = {
        _relative_run_root(track.run_id).as_posix()
        for track in TRACK_SPECS
    }
    records = manifest.get("runs")
    if not isinstance(records, list) or len(records) != G10S0_RUN_COUNT:
        raise G10S0Error("G10 S0 manifest must contain exactly four runs")
    observed_relatives = {
        row.get("relative_root")
        for row in records
        if isinstance(row, Mapping)
    }
    if observed_relatives != expected_relatives:
        raise G10S0Error("G10 S0 run-root set changed")

    expected_state = _expected_state()
    manifest_by_run = {
        row["run_id"]: row
        for row in records
        if isinstance(row, Mapping) and isinstance(row.get("run_id"), str)
    }

    for track in TRACK_SPECS:
        record = manifest_by_run.get(track.run_id)
        if record is None:
            raise G10S0Error(f"manifest lacks run {track.run_id}")
        root = output / _relative_run_root(track.run_id)
        if not root.is_dir():
            raise G10S0Error(f"G10 run root missing: {root}")
        _assert_tree_has_no_symlinks(root)

        identity = _read_object(
            root / G10S0_RUN_IDENTITY_FILENAME,
            "G10 S0 run identity",
        )
        identity_check = copy.deepcopy(identity)
        stored_identity_sha = identity_check.pop("identity_sha256", None)
        if _sha256_json(identity_check) != stored_identity_sha:
            raise G10S0Error(
                f"G10 S0 run identity SHA does not recompute: {root}"
            )
        if stored_identity_sha != record.get("run_identity_sha256"):
            raise G10S0Error(
                f"G10 S0 manifest/run identity mismatch: {root}"
            )
        for key, expected in {
            "run_id": track.run_id,
            "run_index": track.run_index,
            "model_id": track.model_id,
            "novel_id": track.novel_id,
            "profile_id": track.profile_id,
            "audit_interval": 25,
            "first_chapter": 1,
            "last_chapter": 50,
            "audit_checkpoints": [25, 50],
            "state": "s0_unexecuted",
            "provider_calls": 0,
            "paid_execution_authorized": False,
        }.items():
            if identity.get(key) != expected:
                raise G10S0Error(
                    f"G10 S0 run identity changed for {key}: {root}"
                )

        config = load_novel_config(root / "novel.json")
        source = _source_file(root, config.source_type)
        source_binding = manifest["source_bindings"][track.novel_id]
        staged_hashes = {
            "config_sha256": _sha256_file(root / "novel.json"),
            "config_canonical_sha256": _canonical_config_sha256(
                root / "novel.json"
            ),
            "source_sha256": _sha256_file(source),
            "chapter_library_sha256": _sha256_file(
                root / "data" / "chapters.json"
            ),
            "addendum_sha256": _sha256_file(
                root / "translation_addendum.txt"
            ),
        }
        if staged_hashes != source_binding:
            raise G10S0Error(
                f"staged source binding differs for {track.run_id}"
            )
        if identity.get("source_bindings") != source_binding:
            raise G10S0Error(
                f"run identity source binding differs for {track.run_id}"
            )

        for name, expected in expected_state.items():
            if name == "chapters.json":
                continue
            actual = _read_json(
                root / "data" / name,
                f"{name} in {track.run_id}",
            )
            if actual != expected:
                raise G10S0Error(
                    f"{name} is not pristine S0 in {track.run_id}"
                )

        for relative in (
            "output/chapters",
            "output/pending",
            "output/qa",
            "output/audits",
            "output/retroactive_fixes",
            "data/provider_request_journal",
            "data/glossary_candidate_resolutions",
            "data/g10_invocation_ledgers",
        ):
            directory = root / relative
            if not directory.is_dir():
                raise G10S0Error(
                    f"required G10 S0 directory missing: {directory}"
                )
            if any(directory.iterdir()):
                raise G10S0Error(
                    f"G10 S0 directory is not empty: {directory}"
                )

    novel_ids = tuple(dict.fromkeys(track.novel_id for track in TRACK_SPECS))
    current_guard = capture_production_guard(production, novel_ids)
    if current_guard != manifest.get("production_guard"):
        raise G10S0Error(
            "production projects changed since G10 S0 binding"
        )
    for novel_id in novel_ids:
        current = _validate_source_project(production, novel_id)
        expected = manifest["source_bindings"][novel_id]
        for key, wanted in expected.items():
            if current[key] != wanted:
                raise G10S0Error(
                    f"{novel_id} authoritative source changed since S0 binding"
                )

    return {
        "manifest": manifest,
        "manifest_sha256": observed_manifest_sha,
        "run_count": G10S0_RUN_COUNT,
        "production_guard_verified": True,
        "sources_verified": True,
        "all_roots_pristine_s0": True,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


def g10s0_contract_identity() -> dict[str, Any]:
    return {
        "schema_version": G10S0_SCHEMA_VERSION,
        "revision": G10S0_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessors": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
            "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
        },
        "run_count": G10S0_RUN_COUNT,
        "chapters_per_track": G10B1_LAST_CHAPTER,
        "pricing_version": G10S0_PRICING_VERSION,
        "creation": {
            "external_to_production_required": True,
            "external_to_code_worktree_required": True,
            "create_once_never_overwrite": True,
            "sibling_staging_then_atomic_rename": True,
            "production_guard_before_and_after": True,
            "source_file_copied_and_byte_verified": True,
            "chapter_library_copied_and_byte_verified": True,
            "full_canonical_config_bound": True,
            "all_four_runs_independent_fresh_s0": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


G10S0_CONTRACT_SHA256 = _sha256_json(g10s0_contract_identity())


def assert_paid_execution_blocked() -> None:
    identity = g10s0_contract_identity()
    if identity["provider_calls"] != 0:
        raise G10S0Error("G10 S0 layer unexpectedly reports Provider calls")
    if identity["paid_execution_authorized"] is not False:
        raise G10S0Error("G10 S0 layer unexpectedly authorizes paid execution")


__all__ = [
    "G10S0_CONTRACT_SHA256",
    "G10S0_MANIFEST_FILENAME",
    "G10S0_PRICING_VERSION",
    "G10S0_REVISION",
    "G10S0_RUN_COUNT",
    "G10S0_RUN_IDENTITY_FILENAME",
    "G10S0Error",
    "assert_paid_execution_blocked",
    "assert_s0_output_root",
    "g10s0_contract_identity",
    "prepare_s0_roots",
    "verify_s0_roots",
]
