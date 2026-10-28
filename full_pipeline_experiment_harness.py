"""External S0 harness for the chronological GPT-5.6 Luna Batch experiment.

The harness deliberately does not implement a second translation pipeline.  It
creates isolated project roots with NovelTranslator's native state schema,
freezes canonical Batch requests from the accepted state of each root, installs
collected Batch output as ordinary paid pending evidence, and then invokes the
real production lifecycle for exactly that chapter.

All mutable experiment files live below the caller-selected experiment root.
The source production projects are read-only inputs and are guarded by stable
mutable-tree fingerprints before/after experiment operations.

Importing this module performs no filesystem writes, credential loading, or
provider calls.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Callable, Mapping, Sequence

from chapter_library import index_chapter_library, validate_chapter_library
from create_novel import PROJECT_DIRECTORIES, initial_project_state
from full_pipeline_batch_acceptance import (
    ACCEPTANCE_FIELD,
    build_batch_acceptance_evidence,
    install_batch_acceptance_provenance,
    validate_batch_acceptance_evidence,
)
from full_pipeline_batch_wave import (
    BATCH_MODEL,
    BATCH_PRICING_VERSION,
    RESULTS_FILENAME,
    prepare_wave,
)
from full_pipeline_luna_runtime import (
    LUNA_MAX_TOKENS,
    LUNA_REASONING,
    initialize_engine_without_provider,
    install_luna_auxiliary_runtime,
    install_luna_batch_qa_provenance,
    load_experiment_engine_module,
    luna_batch_pending_provenance,
    validate_saved_luna_stats,
)
from novel_project import load_novel_config
from noveltranslator_core import atomic_write_json, atomic_write_text
from production_run_lock import production_run_lock
from production_translation_request import build_production_translation_request
from translation_profile import effective_translation_instructions, load_translation_profile


EXPERIMENT_SCHEMA_VERSION = 1
EXPERIMENT_REVISION = "full-pipeline-luna-batch-s0-v1"
EXPERIMENT_PRICING_VERSION = "openai-gpt-5.6-luna-full-pipeline-mixed-v1"
PROCESSING_SCHEMA_VERSION = 1
PROCESSING_FILENAME = "processing_manifest.json"
MANIFEST_FILENAME = "experiment_manifest.json"
DEFAULT_NOVEL_IDS = ("great_king_zhuxia", "rogue_first_prince")
DEFAULT_TARGET_CHAPTERS = 25
EXPECTED_AUDIT_INTERVAL = 25
PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)


class FullPipelineExperimentError(RuntimeError):
    """Experiment state is missing, inconsistent, or unsafe to continue."""


class FullPipelineExperimentNotCommitted(FullPipelineExperimentError):
    """A collected Batch chapter remains pending because commit QA did not pass."""


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
        raise FullPipelineExperimentError(
            "experiment identity is not canonically JSON-serializable"
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise FullPipelineExperimentError(f"cannot hash {path}: {exc}") from exc


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullPipelineExperimentError(f"cannot read {label} {path}: {exc}") from exc


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise FullPipelineExperimentError(f"{label} must contain an object")
    return value


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    root = Path(root)
    if not root.exists():
        return {"file_count": 0, "byte_count": 0, "sha256": _sha256_bytes(b"")}
    if not root.is_dir():
        raise FullPipelineExperimentError(f"tree root is not a directory: {root}")
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        file_count += 1
        byte_count += len(payload)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return {
        "file_count": file_count,
        "byte_count": byte_count,
        "sha256": digest.hexdigest(),
    }


def _selected_project_guard(project_root: Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    if not (project_root / "novel.json").is_file():
        raise FullPipelineExperimentError(
            f"source production project lacks novel.json: {project_root}"
        )
    records: dict[str, Any] = {}
    for relative in ("novel.json", "translation_addendum.txt"):
        path = project_root / relative
        records[relative] = (
            {"exists": True, "sha256": _sha256_file(path), "bytes": path.stat().st_size}
            if path.is_file()
            else {"exists": False}
        )
    for relative in ("data", "output"):
        records[relative] = _tree_fingerprint(project_root / relative)
    return records


def capture_production_guard(
    repository_root: Path | str,
    novel_ids: Sequence[str] = DEFAULT_NOVEL_IDS,
) -> dict[str, Any]:
    """Fingerprint only mutable/config trees of source production projects."""
    repo = Path(repository_root).resolve()
    projects = {}
    for novel_id in novel_ids:
        projects[novel_id] = _selected_project_guard(repo / "novels" / novel_id)
    return {
        "novel_ids": list(novel_ids),
        "projects": projects,
    }


def _verify_guard(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if dict(expected) != dict(actual):
        raise FullPipelineExperimentError(
            "source production projects changed since experiment binding; "
            "refusing to continue"
        )


def _experiment_project_root(experiment_root: Path | str, novel_id: str) -> Path:
    return Path(experiment_root).resolve() / "projects" / novel_id


def _waves_root(experiment_root: Path | str) -> Path:
    return Path(experiment_root).resolve() / "waves"


def _write_initial_project(
    destination: Path,
    source_project: Path,
    *,
    target_chapters: int,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    config_file = source_project / "novel.json"
    config = load_novel_config(config_file)
    if config.audit_interval != EXPECTED_AUDIT_INTERVAL:
        raise FullPipelineExperimentError(
            f"novel {config.id} audit_interval changed from the experiment's "
            f"genuine expected value {EXPECTED_AUDIT_INTERVAL}: {config.audit_interval}"
        )
    if config.final_chapter < target_chapters:
        raise FullPipelineExperimentError(
            f"novel {config.id} has only {config.final_chapter} configured chapters, "
            f"below target {target_chapters}"
        )

    chapters_source = source_project / "data" / "chapters.json"
    chapters = _read_json(chapters_source, "source chapter library")
    errors = validate_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    if errors:
        raise FullPipelineExperimentError(
            f"source chapter library for {config.id} is invalid: " + "; ".join(errors)
        )
    chapter_map = index_chapter_library(
        chapters,
        config.final_chapter,
        config.chapter_numbering,
    )
    missing = [number for number in range(1, target_chapters + 1) if number not in chapter_map]
    if missing:
        raise FullPipelineExperimentError(
            f"source project {config.id} lacks target production indexes: {missing}"
        )

    for relative in PROJECT_DIRECTORIES:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    (destination / "data" / "glossary_candidate_resolutions").mkdir(
        parents=True, exist_ok=True
    )
    (destination / "data" / "provider_request_journal").mkdir(
        parents=True, exist_ok=True
    )

    shutil.copyfile(config_file, destination / "novel.json")
    addendum_source = source_project / "translation_addendum.txt"
    if addendum_source.is_file():
        shutil.copyfile(addendum_source, destination / "translation_addendum.txt")
    else:
        (destination / "translation_addendum.txt").write_text("", encoding="utf-8")

    state = initial_project_state()
    state["progress.json"] = copy.deepcopy(state["progress.json"])
    state["progress.json"]["pricing_version"] = EXPERIMENT_PRICING_VERSION
    for name, value in state.items():
        if name == "chapters.json":
            continue
        atomic_write_json(destination / "data" / name, value)
    shutil.copyfile(chapters_source, destination / "data" / "chapters.json")
    if (destination / "data" / "chapters.json").read_bytes() != chapters_source.read_bytes():
        raise FullPipelineExperimentError(
            f"experiment chapter library copy differs for {config.id}"
        )
    return {
        "id": config.id,
        "source_config_sha256": _sha256_file(config_file),
        "source_chapters_sha256": _sha256_file(chapters_source),
        "source_addendum_sha256": (
            _sha256_file(addendum_source) if addendum_source.is_file() else _sha256_bytes(b"")
        ),
        "audit_interval": config.audit_interval,
        "chapter_numbering": config.chapter_numbering,
        "source_language": config.source_language,
        "profile": config.profile,
    }


def capture_experiment_lineage(project_root: Path | str) -> dict[str, Any]:
    """Capture only accepted state that may influence later chronological waves."""
    root = Path(project_root).resolve()
    config = load_novel_config(root / "novel.json")
    progress = _read_object(root / "data" / "progress.json", "progress")
    data_hashes = {}
    for name in (
        "glossary.json",
        "continuity.json",
        "issues.json",
        "progress.json",
        "audit_history.json",
    ):
        data_hashes[name] = _sha256_file(root / "data" / name)
    lineage = {
        "novel_id": config.id,
        "last_completed": progress.get("last_completed"),
        "next_chapter": progress.get("next_chapter"),
        "last_audited_chapter": progress.get("last_audited_chapter"),
        "glossary_sha256": data_hashes["glossary.json"],
        "continuity_sha256": data_hashes["continuity.json"],
        "issues_sha256": data_hashes["issues.json"],
        "progress_sha256": data_hashes["progress.json"],
        "audit_history_sha256": data_hashes["audit_history.json"],
        "committed_chapters_tree": _tree_fingerprint(root / "output" / "chapters"),
        "committed_qa_tree": _tree_fingerprint(root / "output" / "qa"),
    }
    lineage["lineage_sha256"] = _sha256_text(_canonical_json(lineage))
    return lineage


def create_experiment(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    repo_anchor: str,
    novel_ids: Sequence[str] = DEFAULT_NOVEL_IDS,
    target_chapters: int = DEFAULT_TARGET_CHAPTERS,
    experiment_id: str = "phase60-full-pipeline-luna-batch-v1",
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Atomically create isolated S0 roots from authoritative chapter libraries."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    if root.exists():
        raise FullPipelineExperimentError(
            f"experiment root already exists and was not overwritten: {root}"
        )
    if (
        not isinstance(target_chapters, int)
        or isinstance(target_chapters, bool)
        or target_chapters < 1
    ):
        raise FullPipelineExperimentError("target_chapters must be positive")
    if not isinstance(repo_anchor, str) or not repo_anchor.strip():
        raise FullPipelineExperimentError("repo_anchor must be non-empty")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise FullPipelineExperimentError("experiment_id must be non-empty")
    novel_ids = tuple(novel_ids)
    if len(novel_ids) != len(set(novel_ids)) or not novel_ids:
        raise FullPipelineExperimentError("novel_ids must be unique and non-empty")

    production_guard = capture_production_guard(repo, novel_ids)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".{root.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "projects").mkdir()
        (staging / "waves").mkdir()
        source_bindings = []
        for novel_id in novel_ids:
            source_project = repo / "novels" / novel_id
            binding = _write_initial_project(
                staging / "projects" / novel_id,
                source_project,
                target_chapters=target_chapters,
            )
            if binding["id"] != novel_id:
                raise FullPipelineExperimentError(
                    f"source path id {novel_id!r} contains config id {binding['id']!r}"
                )
            source_bindings.append(binding)

        manifest = {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiment_revision": EXPERIMENT_REVISION,
            "experiment_id": experiment_id.strip(),
            "repo_anchor": repo_anchor.strip(),
            "created_at_utc": now().astimezone(timezone.utc).isoformat(),
            "novel_ids": list(novel_ids),
            "target_chapters": target_chapters,
            "expected_audit_interval": EXPECTED_AUDIT_INTERVAL,
            "pricing_version": EXPERIMENT_PRICING_VERSION,
            "source_bindings": source_bindings,
            "production_guard": production_guard,
            "initial_lineages": {
                novel_id: capture_experiment_lineage(staging / "projects" / novel_id)
                for novel_id in novel_ids
            },
        }
        stable = dict(manifest)
        stable.pop("created_at_utc")
        manifest["experiment_manifest_sha256"] = _sha256_text(
            _canonical_json(stable)
        )
        atomic_write_json(staging / MANIFEST_FILENAME, manifest)
        if root.exists():
            raise FullPipelineExperimentError(
                f"experiment root appeared during creation: {root}"
            )
        os.rename(staging, root)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    _verify_guard(production_guard, capture_production_guard(repo, novel_ids))
    return load_experiment_manifest(root)


def load_experiment_manifest(experiment_root: Path | str) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    manifest = _read_object(root / MANIFEST_FILENAME, "experiment manifest")
    if manifest.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise FullPipelineExperimentError("unsupported experiment schema version")
    if manifest.get("experiment_revision") != EXPERIMENT_REVISION:
        raise FullPipelineExperimentError("unsupported experiment revision")
    return manifest


def _assert_wave_sequence(root: Path, manifest: Mapping[str, Any], wave_number: int) -> None:
    target = manifest["target_chapters"]
    if (
        not isinstance(wave_number, int)
        or isinstance(wave_number, bool)
        or wave_number < 1
        or wave_number > target
    ):
        raise FullPipelineExperimentError(
            f"wave number must be within 1..{target}"
        )
    if wave_number > 1:
        previous = _waves_root(root) / f"wave_{wave_number - 1:04d}" / PROCESSING_FILENAME
        state = _read_object(previous, "previous wave processing manifest")
        if state.get("status") != "complete":
            raise FullPipelineExperimentError(
                f"Wave {wave_number - 1} is not fully processed; Wave {wave_number} "
                "cannot be prepared"
            )


def build_wave_requests(
    experiment_root: Path | str,
    engine_root: Path | str,
    wave_number: int,
) -> list[dict[str, Any]]:
    """Build canonical Luna requests from each novel's accepted current lineage."""
    root = Path(experiment_root).resolve()
    engine = Path(engine_root).resolve()
    manifest = load_experiment_manifest(root)
    _assert_wave_sequence(root, manifest, wave_number)
    requests = []
    for novel_id in manifest["novel_ids"]:
        project = _experiment_project_root(root, novel_id)
        config = load_novel_config(project / "novel.json")
        progress = _read_object(project / "data" / "progress.json", "progress")
        if (
            progress.get("last_completed") != wave_number - 1
            or progress.get("next_chapter") != wave_number
        ):
            raise FullPipelineExperimentError(
                f"{novel_id} is not at the start of Wave {wave_number}: "
                f"last_completed={progress.get('last_completed')!r}, "
                f"next_chapter={progress.get('next_chapter')!r}"
            )
        numeric_pending = sorted(
            path.name
            for path in (project / "output" / "pending").glob("*.md")
            if path.stem.isdigit()
        )
        if numeric_pending:
            raise FullPipelineExperimentError(
                f"{novel_id} still has pending translation evidence: {numeric_pending}"
            )
        chapters = _read_json(project / "data" / "chapters.json", "chapter library")
        chapter_map = index_chapter_library(
            chapters,
            config.final_chapter,
            config.chapter_numbering,
        )
        chapter = chapter_map[wave_number]
        glossary = _read_object(project / "data" / "glossary.json", "glossary")
        continuity = _read_object(project / "data" / "continuity.json", "continuity")
        profile = load_translation_profile(config.profile, engine)
        system_prompt = effective_translation_instructions(
            profile,
            project / "translation_addendum.txt",
        )
        body = build_production_translation_request(
            chapter,
            provider="openai",
            model=BATCH_MODEL,
            max_tokens=LUNA_MAX_TOKENS,
            reasoning=LUNA_REASONING,
            system_prompt=system_prompt,
            chapter_numbering=config.chapter_numbering,
            source_language=config.source_language,
            target_language=config.target_language,
            glossary_entries=glossary.get("entries", {}),
            profile=profile,
            continuity_summary=continuity.get("summary", ""),
        )
        requests.append(
            {
                "novel_id": novel_id,
                "chapter": wave_number,
                "body": body,
                "lineage_before": capture_experiment_lineage(project),
            }
        )
    return requests


def prepare_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    now: Callable[[], datetime] = utc_now,
) -> tuple[dict[str, Any], Path]:
    """Freeze one CN+KR chronological wave with source production guard binding."""
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    manifest = load_experiment_manifest(root)
    current_guard = capture_production_guard(repo, manifest["novel_ids"])
    _verify_guard(manifest["production_guard"], current_guard)
    requests = build_wave_requests(root, repo, wave_number)
    return prepare_wave(
        _waves_root(root),
        wave_number,
        requests,
        capture_protected_state=lambda: capture_production_guard(
            repo, manifest["novel_ids"]
        ),
        experiment_id=manifest["experiment_id"],
        now=now,
    )


def _processing_path(experiment_root: Path, wave_number: int) -> Path:
    return _waves_root(experiment_root) / f"wave_{wave_number:04d}" / PROCESSING_FILENAME


def _results_path(experiment_root: Path, wave_number: int) -> Path:
    return _waves_root(experiment_root) / f"wave_{wave_number:04d}" / RESULTS_FILENAME


def _load_collected(experiment_root: Path, wave_number: int) -> dict[str, Any]:
    path = _results_path(experiment_root, wave_number)
    document = _read_object(path, "collected wave results")
    if document.get("status") != "complete":
        raise FullPipelineExperimentError("Batch wave results are not complete")
    if document.get("transport") != "openai_batch":
        raise FullPipelineExperimentError("Batch wave results have wrong transport")
    if not isinstance(document.get("batch_id"), str) or not document["batch_id"]:
        raise FullPipelineExperimentError("Batch wave results lack batch_id")
    requests = document.get("requests")
    if not isinstance(requests, list) or not requests:
        raise FullPipelineExperimentError("Batch wave results contain no requests")
    return document


def _result_map(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for item in document["requests"]:
        if not isinstance(item, dict):
            raise FullPipelineExperimentError("collected request entry must be an object")
        novel_id = item.get("novel_id")
        if not isinstance(novel_id, str) or not novel_id:
            raise FullPipelineExperimentError("collected request lacks novel_id")
        if novel_id in result:
            raise FullPipelineExperimentError(
                f"collected wave contains duplicate novel result {novel_id}"
            )
        result[novel_id] = item
    return result


def _new_processing_manifest(
    experiment_root: Path,
    experiment_manifest: Mapping[str, Any],
    wave_number: int,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    result_map = _result_map(document)
    expected = set(experiment_manifest["novel_ids"])
    if set(result_map) != expected:
        raise FullPipelineExperimentError(
            "collected wave novel coverage differs from experiment manifest"
        )
    entries = []
    for novel_id in experiment_manifest["novel_ids"]:
        item = result_map[novel_id]
        if item.get("chapter") != wave_number:
            raise FullPipelineExperimentError(
                f"{novel_id} collected chapter {item.get('chapter')!r} does not match "
                f"Wave {wave_number}"
            )
        evidence = build_batch_acceptance_evidence(
            experiment_id=experiment_manifest["experiment_id"],
            wave_number=wave_number,
            batch_id=document["batch_id"],
            result=item,
        )
        entries.append(
            {
                "novel_id": novel_id,
                "chapter": wave_number,
                "custom_id": item["custom_id"],
                "status": "ready",
                "acceptance": evidence,
                "lineage_after": None,
            }
        )
    results_path = _results_path(experiment_root, wave_number)
    return {
        "schema_version": PROCESSING_SCHEMA_VERSION,
        "experiment_id": experiment_manifest["experiment_id"],
        "wave_number": wave_number,
        "batch_id": document["batch_id"],
        "results_sha256": _sha256_file(results_path),
        "status": "processing",
        "requests": entries,
    }


def _load_or_create_processing_manifest(
    experiment_root: Path,
    experiment_manifest: Mapping[str, Any],
    wave_number: int,
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    path = _processing_path(experiment_root, wave_number)
    if path.exists():
        processing = _read_object(path, "wave processing manifest")
        if processing.get("schema_version") != PROCESSING_SCHEMA_VERSION:
            raise FullPipelineExperimentError("unsupported processing schema version")
        if processing.get("results_sha256") != _sha256_file(
            _results_path(experiment_root, wave_number)
        ):
            raise FullPipelineExperimentError(
                "collected results changed after processing began"
            )
        return processing, path
    processing = _new_processing_manifest(
        experiment_root,
        experiment_manifest,
        wave_number,
        document,
    )
    atomic_write_json(path, processing)
    return processing, path


def _pending_meta_for_result(
    item: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    stats = validate_saved_luna_stats(item.get("stats", {}))
    if stats.get("pricing_version") != BATCH_PRICING_VERSION:
        raise FullPipelineExperimentError("pending translation is not Batch-priced")
    meta = {
        "chapter": item["chapter"],
        "stage": "translated",
        "translation_stats": stats,
        "elapsed_seconds": 0.0,
        "finish_reason": item.get("finish_reason"),
        "provider_request_id": str(item.get("provider_request_id") or ""),
        "provider_response_id": str(item.get("provider_response_id") or ""),
        "provider_response_model": str(item.get("provider_response_model") or ""),
        ACCEPTANCE_FIELD: copy.deepcopy(dict(evidence)),
    }
    meta.update(luna_batch_pending_provenance())
    return meta


def _pending_paths(project_root: Path, chapter: int) -> tuple[Path, Path]:
    stem = f"{chapter:04d}"
    return (
        project_root / "output" / "pending" / f"{stem}.md",
        project_root / "output" / "pending" / f"{stem}.json",
    )


def _install_or_verify_pending(
    project_root: Path,
    item: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    chapter = item["chapter"]
    text_path, meta_path = _pending_paths(project_root, chapter)
    expected_text = item.get("content")
    if not isinstance(expected_text, str):
        raise FullPipelineExperimentError("collected translation content must be a string")
    expected_meta = _pending_meta_for_result(item, evidence)

    if text_path.exists():
        if text_path.read_text(encoding="utf-8") != expected_text:
            raise FullPipelineExperimentError(
                f"pending Chapter {chapter} text differs from collected Batch output"
            )
        if meta_path.exists():
            existing_meta = _read_object(meta_path, "pending metadata")
            existing_evidence = validate_batch_acceptance_evidence(
                existing_meta.get(ACCEPTANCE_FIELD)
            )
            if existing_evidence != dict(evidence):
                raise FullPipelineExperimentError(
                    f"pending Chapter {chapter} carries different Batch evidence"
                )
            return {"text_path": text_path, "meta_path": meta_path, "reused": True}
        atomic_write_json(meta_path, expected_meta)
        return {"text_path": text_path, "meta_path": meta_path, "reused": True}

    if meta_path.exists():
        raise FullPipelineExperimentError(
            f"pending Chapter {chapter} metadata exists without its translation text"
        )
    atomic_write_text(text_path, expected_text)
    atomic_write_json(meta_path, expected_meta)
    return {"text_path": text_path, "meta_path": meta_path, "reused": False}


def _committed_acceptance(
    project_root: Path,
    chapter: int,
    evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    progress = _read_object(project_root / "data" / "progress.json", "progress")
    completed = progress.get("completed", [])
    if chapter not in completed:
        return None
    qa_path = project_root / "output" / "qa" / f"{chapter:04d}.json"
    output_path = project_root / "output" / "chapters" / f"{chapter:04d}.md"
    if not qa_path.is_file() or not output_path.is_file():
        raise FullPipelineExperimentError(
            f"progress marks Chapter {chapter} complete without committed QA/output"
        )
    qa = _read_object(qa_path, "committed QA")
    observed = validate_batch_acceptance_evidence(qa.get(ACCEPTANCE_FIELD))
    if observed != dict(evidence):
        raise FullPipelineExperimentError(
            f"committed Chapter {chapter} carries different Batch acceptance evidence"
        )
    return {
        "progress": progress,
        "qa": qa,
        "qa_path": qa_path,
        "output_path": output_path,
        "audit_complete": progress.get("last_audited_chapter", 0) >= chapter,
    }


def _audit_required(project_root: Path, chapter: int) -> bool:
    config = load_novel_config(project_root / "novel.json")
    return chapter % config.audit_interval == 0


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_real_engine_for_result(
    project_root: Path,
    engine_root: Path,
    chapter: int,
    evidence: Mapping[str, Any],
    client,
) -> None:
    script = engine_root / PRODUCTION_SCRIPT_NAME
    loaded = None
    with production_run_lock(project_root), _working_directory(project_root):
        try:
            loaded = load_experiment_engine_module(script)
            module = loaded["module"]
            initialize_engine_without_provider(module)
            install_luna_auxiliary_runtime(module, client)
            install_luna_batch_qa_provenance(module, project_root / "output" / "qa")
            install_batch_acceptance_provenance(
                module,
                project_root / "output" / "qa",
                evidence,
            )
            module.run_unattended_production(target_chapter=chapter)
        finally:
            if loaded is not None:
                sys.modules.pop(loaded["module_name"], None)


def process_collected_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    client_factory: Callable[[str, int], Any],
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Install collected Batch output and let the real lifecycle advance state.

    ``crash_hook`` is a zero-provider test/recovery seam.  It is called only
    after durable boundaries and production code never installs one.
    """
    root = Path(experiment_root).resolve()
    repo = Path(repository_root).resolve()
    manifest = load_experiment_manifest(root)
    _assert_wave_sequence(root, manifest, wave_number)
    _verify_guard(
        manifest["production_guard"],
        capture_production_guard(repo, manifest["novel_ids"]),
    )
    document = _load_collected(root, wave_number)
    result_map = _result_map(document)
    processing, processing_path = _load_or_create_processing_manifest(
        root, manifest, wave_number, document
    )

    def checkpoint():
        atomic_write_json(processing_path, processing)

    def hook(stage: str, context: Mapping[str, Any]):
        if crash_hook is not None:
            crash_hook(stage, context)

    for entry in processing["requests"]:
        novel_id = entry["novel_id"]
        project = _experiment_project_root(root, novel_id)
        item = result_map[novel_id]
        evidence = validate_batch_acceptance_evidence(entry["acceptance"])
        if item.get("custom_id") != entry["custom_id"]:
            raise FullPipelineExperimentError(
                f"processing custom_id changed for {novel_id}"
            )

        committed = _committed_acceptance(project, wave_number, evidence)
        if committed is not None:
            if _audit_required(project, wave_number) and not committed["audit_complete"]:
                client = client_factory(novel_id, wave_number)
                _run_real_engine_for_result(
                    project, repo, wave_number, evidence, client
                )
                committed = _committed_acceptance(project, wave_number, evidence)
                if committed is None or not committed["audit_complete"]:
                    raise FullPipelineExperimentError(
                        f"Chapter {wave_number} committed for {novel_id} but due audit "
                        "did not complete"
                    )
            entry["status"] = "committed"
            entry["lineage_after"] = capture_experiment_lineage(project)
            checkpoint()
            continue

        current_lineage = capture_experiment_lineage(project)
        expected_lineage = item.get("lineage_before")
        if current_lineage != expected_lineage:
            raise FullPipelineExperimentError(
                f"{novel_id} lineage changed after Wave {wave_number} was frozen"
            )
        if _sha256_text(_canonical_json(current_lineage)) != item.get(
            "lineage_before_sha256"
        ):
            raise FullPipelineExperimentError(
                f"{novel_id} lineage hash does not match collected request"
            )

        pending = _install_or_verify_pending(project, item, evidence)
        hook(
            "after_pending_files",
            {
                "novel_id": novel_id,
                "chapter": wave_number,
                "reused": pending["reused"],
                "acceptance_sha256": evidence["acceptance_sha256"],
            },
        )
        entry["status"] = "pending_installed"
        checkpoint()
        hook(
            "after_pending_checkpoint",
            {
                "novel_id": novel_id,
                "chapter": wave_number,
                "acceptance_sha256": evidence["acceptance_sha256"],
            },
        )

        client = client_factory(novel_id, wave_number)
        _run_real_engine_for_result(project, repo, wave_number, evidence, client)
        hook(
            "after_engine_run",
            {
                "novel_id": novel_id,
                "chapter": wave_number,
                "acceptance_sha256": evidence["acceptance_sha256"],
            },
        )
        committed = _committed_acceptance(project, wave_number, evidence)
        if committed is None:
            # Commit QA deliberately leaves the exact paid translation pending.
            # No automatic retranslation/retry is attempted here.
            raise FullPipelineExperimentNotCommitted(
                f"{novel_id} Chapter {wave_number} remains pending after real engine QA"
            )
        if _audit_required(project, wave_number) and not committed["audit_complete"]:
            raise FullPipelineExperimentError(
                f"{novel_id} Chapter {wave_number} committed but due audit is incomplete"
            )
        entry["status"] = "committed"
        entry["lineage_after"] = capture_experiment_lineage(project)
        checkpoint()

    if not all(item.get("status") == "committed" for item in processing["requests"]):
        raise FullPipelineExperimentError("wave processing did not commit every novel")
    processing["status"] = "complete"
    processing["completed_lineages"] = {
        item["novel_id"]: item["lineage_after"] for item in processing["requests"]
    }
    checkpoint()
    _verify_guard(
        manifest["production_guard"],
        capture_production_guard(repo, manifest["novel_ids"]),
    )
    return processing
