"""State-binding v2 contract for the full-pipeline Luna Batch experiment.

Phase 60F proved that the genuine NovelTranslator lifecycle can consume one
chronological Luna Batch wave at a time.  This module sharpens the experiment
identity *before any paid wave is frozen* by separating three concerns:

* causal lineage: semantic/scheduler/static state that can affect later prompts,
  glossary behavior, or audit behavior;
* committed evidence: stable full-history/accounting evidence captured only at
  clean wave boundaries; and
* pre-wave history guard: previous committed outputs/QA/audits/backups that must
  remain byte-stable while the current chapter is in progress.

Current-wave pending files, provider journals, candidate-resolution sidecars,
and logs are recovery evidence owned by their existing protocols and are not
mistaken for causal state.  Current-wave paid accounting may legitimately move
before the chapter commit marker, so it is not used as a restart blocker after
the durable pending checkpoint.

A reviewed partial commit is the only case where current-wave glossary,
continuity, or issue files may be ahead of ``progress.json``.  Such a mismatch
is tolerated only after the production partial-commit proof succeeds; the real
engine then performs its stronger deterministic reconciliation checks before
writing the commit tail.

The Phase 60F harness remains the sole lifecycle implementation.  This adapter
changes only its lineage callback and experiment-only QA recovery callback for
the duration of an operation, restoring both in ``finally``.

Importing this module performs no filesystem writes, provider calls, credential
loading, or monkeypatching.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import full_pipeline_batch_wave as batch
import full_pipeline_experiment_harness as harness
from full_pipeline_batch_acceptance import build_batch_acceptance_evidence
from full_pipeline_batch_collection_v2 import collect_wave as collect_wave_v2
from full_pipeline_partial_commit_v2 import (
    FullPipelinePartialCommitV2Error,
    install_experiment_partial_commit_qa_adapter,
    prove_experiment_reviewed_partial_commit,
)
from novel_project import load_novel_config


STATE_BINDING_SCHEMA_VERSION = 2
STATE_BINDING_REVISION = "full-pipeline-causal-lineage-evidence-v2"
CONTRACT_SCHEMA_VERSION = 1
CONTRACT_REVISION = "full-pipeline-experiment-contract-v2"
CONTRACT_FILENAME = "experiment_contract_v2.json"
WAVE_BINDING_SCHEMA_VERSION = 1
WAVE_BINDING_REVISION = "full-pipeline-wave-state-binding-v2"
WAVE_BINDING_FILENAME = "wave_state_binding_v2.json"


class FullPipelineExperimentContractV2Error(harness.FullPipelineExperimentError):
    """Experiment v2 contract/state binding is missing or inconsistent."""


def _sha256_file(path: Path) -> str:
    return harness._sha256_file(Path(path))


def _binding_hash(value: Mapping[str, Any]) -> str:
    return harness._sha256_text(harness._canonical_json(dict(value)))


def _static_prompt_inputs(root: Path) -> dict[str, str]:
    addendum = root / "translation_addendum.txt"
    return {
        "novel_json_sha256": _sha256_file(root / "novel.json"),
        "chapters_json_sha256": _sha256_file(root / "data" / "chapters.json"),
        "translation_addendum_sha256": (
            _sha256_file(addendum)
            if addendum.is_file()
            else harness._sha256_bytes(b"")
        ),
    }


def capture_causal_lineage(project_root: Path | str) -> dict[str, Any]:
    """Capture only accepted state capable of changing later experiment behavior."""
    root = Path(project_root).resolve()
    config = load_novel_config(root / "novel.json")
    progress = harness._read_object(root / "data" / "progress.json", "progress")
    completed = progress.get("completed", [])
    failed = progress.get("failed", [])
    if not isinstance(completed, list) or not isinstance(failed, list):
        raise FullPipelineExperimentContractV2Error(
            "progress completed/failed fields must be lists"
        )
    return {
        "schema_version": STATE_BINDING_SCHEMA_VERSION,
        "binding_revision": STATE_BINDING_REVISION,
        "novel_id": config.id,
        "static_prompt_inputs": _static_prompt_inputs(root),
        "scheduler": {
            "completed": copy.deepcopy(completed),
            "failed": copy.deepcopy(failed),
            "last_completed": progress.get("last_completed"),
            "next_chapter": progress.get("next_chapter"),
            "last_audited_chapter": progress.get("last_audited_chapter"),
        },
        "glossary_sha256": _sha256_file(root / "data" / "glossary.json"),
        "continuity_sha256": _sha256_file(root / "data" / "continuity.json"),
        "issues_sha256": _sha256_file(root / "data" / "issues.json"),
        "audit_history_sha256": _sha256_file(
            root / "data" / "audit_history.json"
        ),
    }


def capture_committed_evidence(project_root: Path | str) -> dict[str, Any]:
    """Capture full stable-at-wave-boundary committed/accounting evidence."""
    root = Path(project_root).resolve()
    return {
        "progress_sha256": _sha256_file(root / "data" / "progress.json"),
        "committed_chapters_tree": harness._tree_fingerprint(
            root / "output" / "chapters"
        ),
        "committed_qa_tree": harness._tree_fingerprint(root / "output" / "qa"),
        "audits_tree": harness._tree_fingerprint(root / "output" / "audits"),
        "retroactive_fixes_tree": harness._tree_fingerprint(
            root / "output" / "retroactive_fixes"
        ),
        "backup_tree": harness._tree_fingerprint(root / "backup"),
    }


def capture_experiment_state_binding(project_root: Path | str) -> dict[str, Any]:
    """Return independently hashed causal lineage and committed evidence."""
    causal = capture_causal_lineage(project_root)
    evidence = capture_committed_evidence(project_root)
    causal_sha = _binding_hash(causal)
    evidence_sha = _binding_hash(evidence)
    identity = {
        "schema_version": STATE_BINDING_SCHEMA_VERSION,
        "binding_revision": STATE_BINDING_REVISION,
        "causal_lineage_sha256": causal_sha,
        "committed_evidence_sha256": evidence_sha,
    }
    return {
        **identity,
        "causal": causal,
        "committed_evidence": evidence,
        "state_binding_sha256": _binding_hash(identity),
    }


def validate_experiment_state_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FullPipelineExperimentContractV2Error(
            "experiment state binding must be an object"
        )
    binding = copy.deepcopy(dict(value))
    if binding.get("schema_version") != STATE_BINDING_SCHEMA_VERSION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported experiment state-binding schema"
        )
    if binding.get("binding_revision") != STATE_BINDING_REVISION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported experiment state-binding revision"
        )
    causal = binding.get("causal")
    evidence = binding.get("committed_evidence")
    if not isinstance(causal, Mapping) or not isinstance(evidence, Mapping):
        raise FullPipelineExperimentContractV2Error(
            "state binding lacks causal/evidence objects"
        )
    causal_sha = _binding_hash(causal)
    evidence_sha = _binding_hash(evidence)
    if binding.get("causal_lineage_sha256") != causal_sha:
        raise FullPipelineExperimentContractV2Error(
            "causal lineage hash does not match its state"
        )
    if binding.get("committed_evidence_sha256") != evidence_sha:
        raise FullPipelineExperimentContractV2Error(
            "committed evidence hash does not match its state"
        )
    identity = {
        "schema_version": STATE_BINDING_SCHEMA_VERSION,
        "binding_revision": STATE_BINDING_REVISION,
        "causal_lineage_sha256": causal_sha,
        "committed_evidence_sha256": evidence_sha,
    }
    if binding.get("state_binding_sha256") != _binding_hash(identity):
        raise FullPipelineExperimentContractV2Error(
            "combined state-binding hash mismatch"
        )
    return binding


def _tree_fingerprint_selected(root: Path, predicate) -> dict[str, Any]:
    """Fingerprint selected files using the same path/hash framing as the harness."""
    root = Path(root)
    if not root.exists():
        return {
            "file_count": 0,
            "byte_count": 0,
            "sha256": harness._sha256_bytes(b""),
        }
    if not root.is_dir():
        raise FullPipelineExperimentContractV2Error(
            f"history-guard root is not a directory: {root}"
        )
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if not predicate(path, relative):
            continue
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


def _prior_numeric_tree(root: Path, wave_number: int) -> dict[str, Any]:
    def include(path, _relative):
        return path.stem.isdigit() and int(path.stem) < wave_number

    return _tree_fingerprint_selected(root, include)


def capture_pre_wave_history_guard(
    project_root: Path | str,
    wave_number: int,
) -> dict[str, Any]:
    """Protect prior committed history while allowing current-wave recovery files."""
    root = Path(project_root).resolve()
    return {
        "prior_chapters_tree": _prior_numeric_tree(
            root / "output" / "chapters", wave_number
        ),
        "prior_qa_tree": _prior_numeric_tree(root / "output" / "qa", wave_number),
        "audits_tree": harness._tree_fingerprint(root / "output" / "audits"),
        "retroactive_fixes_tree": harness._tree_fingerprint(
            root / "output" / "retroactive_fixes"
        ),
        "backup_tree": harness._tree_fingerprint(root / "backup"),
    }


def _contract_path(experiment_root: Path | str) -> Path:
    return Path(experiment_root).resolve() / CONTRACT_FILENAME


def _wave_target(experiment_root: Path | str, wave_number: int) -> Path:
    return Path(experiment_root).resolve() / "waves" / f"wave_{wave_number:04d}"


def _wave_binding_path(experiment_root: Path | str, wave_number: int) -> Path:
    return _wave_target(experiment_root, wave_number) / WAVE_BINDING_FILENAME


def _write_contract(
    experiment_root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    initial_lineages = manifest.get("initial_lineages")
    if not isinstance(initial_lineages, Mapping) or not initial_lineages:
        raise FullPipelineExperimentContractV2Error(
            "experiment manifest lacks initial causal lineages"
        )
    initial_bindings = {}
    for novel_id in manifest["novel_ids"]:
        project = experiment_root / "projects" / novel_id
        binding = capture_experiment_state_binding(project)
        if binding["causal"] != initial_lineages.get(novel_id):
            raise FullPipelineExperimentContractV2Error(
                f"initial causal lineage differs from harness manifest for {novel_id}"
            )
        initial_bindings[novel_id] = binding
    stable = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "experiment_id": manifest["experiment_id"],
        "experiment_manifest_sha256": manifest["experiment_manifest_sha256"],
        "state_binding_revision": STATE_BINDING_REVISION,
        "initial_state_bindings": initial_bindings,
    }
    contract = dict(stable)
    contract["contract_sha256"] = _binding_hash(stable)
    harness.atomic_write_json(_contract_path(experiment_root), contract)
    return contract


def load_experiment_contract(experiment_root: Path | str) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    contract = harness._read_object(_contract_path(root), "experiment v2 contract")
    if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported experiment contract schema"
        )
    if contract.get("contract_revision") != CONTRACT_REVISION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported experiment contract revision"
        )
    stable = dict(contract)
    expected_hash = stable.pop("contract_sha256", None)
    if expected_hash != _binding_hash(stable):
        raise FullPipelineExperimentContractV2Error(
            "experiment v2 contract hash mismatch"
        )
    manifest = harness.load_experiment_manifest(root)
    if contract.get("experiment_id") != manifest.get("experiment_id"):
        raise FullPipelineExperimentContractV2Error(
            "experiment contract ID differs from harness manifest"
        )
    if contract.get("experiment_manifest_sha256") != manifest.get(
        "experiment_manifest_sha256"
    ):
        raise FullPipelineExperimentContractV2Error(
            "experiment contract is bound to a different harness manifest"
        )
    initial = contract.get("initial_state_bindings")
    if not isinstance(initial, Mapping) or set(initial) != set(manifest["novel_ids"]):
        raise FullPipelineExperimentContractV2Error(
            "experiment contract has wrong initial novel coverage"
        )
    for value in initial.values():
        validate_experiment_state_binding(value)
    return contract


@contextmanager
def _harness_contract(capture_callback=capture_causal_lineage):
    """Temporarily install v2 causal capture and experiment partial-QA adapter."""
    original_capture = harness.capture_experiment_lineage
    original_acceptance = harness.install_batch_acceptance_provenance

    def install_acceptance_with_partial_adapter(production_module, qa_dir, evidence):
        state = original_acceptance(production_module, qa_dir, evidence)
        install_experiment_partial_commit_qa_adapter(production_module, evidence)
        return state

    harness.capture_experiment_lineage = capture_callback
    harness.install_batch_acceptance_provenance = install_acceptance_with_partial_adapter
    try:
        yield
    finally:
        harness.capture_experiment_lineage = original_capture
        harness.install_batch_acceptance_provenance = original_acceptance


def create_experiment(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    repo_anchor: str,
    novel_ids: Sequence[str] = harness.DEFAULT_NOVEL_IDS,
    target_chapters: int = harness.DEFAULT_TARGET_CHAPTERS,
    experiment_id: str = "phase60-full-pipeline-luna-batch-v2",
    now: Callable[[], Any] = harness.utc_now,
) -> dict[str, Any]:
    """Create an S0 experiment whose manifest carries causal-only lineages."""
    with _harness_contract():
        manifest = harness.create_experiment(
            experiment_root,
            repository_root,
            repo_anchor=repo_anchor,
            novel_ids=novel_ids,
            target_chapters=target_chapters,
            experiment_id=experiment_id,
            now=now,
        )
    _write_contract(Path(experiment_root).resolve(), manifest)
    return manifest


def build_wave_requests(
    experiment_root: Path | str,
    engine_root: Path | str,
    wave_number: int,
) -> list[dict[str, Any]]:
    load_experiment_contract(experiment_root)
    with _harness_contract():
        return harness.build_wave_requests(
            experiment_root,
            engine_root,
            wave_number,
        )


def _write_wave_binding(
    experiment_root: Path,
    wave_number: int,
    wave_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    request_map = {
        item["novel_id"]: item
        for item in wave_manifest.get("requests", [])
        if isinstance(item, Mapping)
    }
    experiment_manifest = harness.load_experiment_manifest(experiment_root)
    if set(request_map) != set(experiment_manifest["novel_ids"]):
        raise FullPipelineExperimentContractV2Error(
            "prepared wave request coverage differs from experiment"
        )
    entries = []
    for novel_id in experiment_manifest["novel_ids"]:
        project = experiment_root / "projects" / novel_id
        binding = capture_experiment_state_binding(project)
        request = request_map[novel_id]
        if request.get("lineage_before") != binding["causal"]:
            raise FullPipelineExperimentContractV2Error(
                f"prepared Wave {wave_number} causal lineage mismatch for {novel_id}"
            )
        if request.get("lineage_before_sha256") != binding["causal_lineage_sha256"]:
            raise FullPipelineExperimentContractV2Error(
                f"prepared Wave {wave_number} causal hash mismatch for {novel_id}"
            )
        history_guard = capture_pre_wave_history_guard(project, wave_number)
        entries.append(
            {
                "novel_id": novel_id,
                "custom_id": request["custom_id"],
                "state_binding_before": binding,
                "history_guard_before": history_guard,
                "history_guard_sha256": _binding_hash(history_guard),
            }
        )
    stable = {
        "schema_version": WAVE_BINDING_SCHEMA_VERSION,
        "binding_revision": WAVE_BINDING_REVISION,
        "experiment_id": experiment_manifest["experiment_id"],
        "wave_number": wave_number,
        "run_id": wave_manifest["run_id"],
        "input_sha256": wave_manifest["input_sha256"],
        "requests": entries,
    }
    document = dict(stable)
    document["wave_binding_sha256"] = _binding_hash(stable)
    harness.atomic_write_json(
        _wave_binding_path(experiment_root, wave_number),
        document,
    )
    return document


def load_wave_binding(
    experiment_root: Path | str,
    wave_number: int,
) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    document = harness._read_object(
        _wave_binding_path(root, wave_number),
        "wave state binding",
    )
    if document.get("schema_version") != WAVE_BINDING_SCHEMA_VERSION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported wave state-binding schema"
        )
    if document.get("binding_revision") != WAVE_BINDING_REVISION:
        raise FullPipelineExperimentContractV2Error(
            "unsupported wave state-binding revision"
        )
    stable = dict(document)
    expected_hash = stable.pop("wave_binding_sha256", None)
    if expected_hash != _binding_hash(stable):
        raise FullPipelineExperimentContractV2Error(
            "wave state-binding hash mismatch"
        )
    wave_manifest = harness._read_object(
        _wave_target(root, wave_number) / batch.MANIFEST_FILENAME,
        "Batch wave manifest",
    )
    if document.get("run_id") != wave_manifest.get("run_id"):
        raise FullPipelineExperimentContractV2Error(
            "wave state binding belongs to a different Batch run"
        )
    if document.get("input_sha256") != wave_manifest.get("input_sha256"):
        raise FullPipelineExperimentContractV2Error(
            "wave state binding belongs to different frozen Batch input"
        )
    entries = document.get("requests")
    if not isinstance(entries, list) or not entries:
        raise FullPipelineExperimentContractV2Error(
            "wave state binding contains no request identities"
        )
    for item in entries:
        if not isinstance(item, Mapping):
            raise FullPipelineExperimentContractV2Error(
                "wave state-binding request must be an object"
            )
        validate_experiment_state_binding(item.get("state_binding_before"))
        guard = item.get("history_guard_before")
        if not isinstance(guard, Mapping) or item.get(
            "history_guard_sha256"
        ) != _binding_hash(guard):
            raise FullPipelineExperimentContractV2Error(
                "wave pre-history guard is malformed"
            )
    return document


def prepare_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    now: Callable[[], Any] = harness.utc_now,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    with _harness_contract():
        wave_manifest, target = harness.prepare_experiment_wave(
            root,
            repository_root,
            wave_number,
            now=now,
        )
    _write_wave_binding(root, wave_number, wave_manifest)
    return wave_manifest, target


def _wave_binding_map(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for item in document["requests"]:
        novel_id = item.get("novel_id")
        if not isinstance(novel_id, str) or not novel_id or novel_id in result:
            raise FullPipelineExperimentContractV2Error(
                "wave state binding contains malformed/duplicate novel identity"
            )
        result[novel_id] = dict(item)
    return result


def _verify_wave_state_before_provider(
    experiment_root: Path,
    wave_number: int,
) -> dict[str, Any]:
    document = load_wave_binding(experiment_root, wave_number)
    frozen = _wave_binding_map(document)
    manifest = harness.load_experiment_manifest(experiment_root)
    if set(frozen) != set(manifest["novel_ids"]):
        raise FullPipelineExperimentContractV2Error(
            "wave state-binding novel coverage differs from experiment"
        )
    for novel_id in manifest["novel_ids"]:
        project = experiment_root / "projects" / novel_id
        current = capture_experiment_state_binding(project)
        expected = validate_experiment_state_binding(
            frozen[novel_id]["state_binding_before"]
        )
        if current != expected:
            raise FullPipelineExperimentContractV2Error(
                f"{novel_id} state changed after Wave {wave_number} was frozen"
            )
        history = capture_pre_wave_history_guard(project, wave_number)
        if history != frozen[novel_id]["history_guard_before"]:
            raise FullPipelineExperimentContractV2Error(
                f"{novel_id} prior committed history changed after Wave "
                f"{wave_number} was frozen"
            )
    return document


def _protected_capture(experiment_root: Path | str, repository_root: Path | str):
    manifest = harness.load_experiment_manifest(experiment_root)
    return lambda: harness.capture_production_guard(
        repository_root,
        manifest["novel_ids"],
    )


def submit_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
    authorization: str,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    _verify_wave_state_before_provider(root, wave_number)
    return batch.submit_wave(
        _wave_target(root, wave_number),
        client,
        authorization,
        capture_protected_state=_protected_capture(root, repository_root),
    )


def reconcile_experiment_wave_submission(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    _verify_wave_state_before_provider(root, wave_number)
    return batch.reconcile_submission(
        _wave_target(root, wave_number),
        client,
        capture_protected_state=_protected_capture(root, repository_root),
    )


def refresh_experiment_wave_status(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    _verify_wave_state_before_provider(root, wave_number)
    return batch.refresh_status(
        _wave_target(root, wave_number),
        client,
        capture_protected_state=_protected_capture(root, repository_root),
    )


def collect_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
    *,
    now: Callable[[], Any] = batch.utc_now,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    _verify_wave_state_before_provider(root, wave_number)
    return collect_wave_v2(
        _wave_target(root, wave_number),
        client,
        capture_protected_state=_protected_capture(root, repository_root),
        now=now,
        crash_hook=crash_hook,
    )


def _processing_state(
    experiment_root: Path,
    wave_number: int,
) -> dict[str, Any] | None:
    path = (
        experiment_root
        / "waves"
        / f"wave_{wave_number:04d}"
        / harness.PROCESSING_FILENAME
    )
    if not path.is_file():
        return None
    return harness._read_object(path, "wave processing manifest")


def _processing_entry_status(
    processing: Mapping[str, Any] | None,
    novel_id: str,
) -> str:
    if not isinstance(processing, Mapping):
        return "ready"
    for item in processing.get("requests", []):
        if isinstance(item, Mapping) and item.get("novel_id") == novel_id:
            return str(item.get("status") or "ready")
    return "ready"


def _acceptance_for_result(
    processing: Mapping[str, Any] | None,
    experiment_manifest: Mapping[str, Any],
    document: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(processing, Mapping):
        for entry in processing.get("requests", []):
            if isinstance(entry, Mapping) and entry.get("novel_id") == item.get("novel_id"):
                evidence = entry.get("acceptance")
                if isinstance(evidence, Mapping):
                    return copy.deepcopy(dict(evidence))
    return build_batch_acceptance_evidence(
        experiment_id=experiment_manifest["experiment_id"],
        wave_number=document["wave_number"],
        batch_id=document["batch_id"],
        result=item,
    )


def _partial_causal_difference_is_allowed(
    actual: Mapping[str, Any],
    frozen: Mapping[str, Any],
) -> bool:
    mutable = {"glossary_sha256", "continuity_sha256", "issues_sha256"}
    actual_fixed = {key: value for key, value in actual.items() if key not in mutable}
    frozen_fixed = {key: value for key, value in frozen.items() if key not in mutable}
    return actual_fixed == frozen_fixed


def _prove_partial_causal_recovery(
    project: Path,
    wave_number: int,
    evidence: Mapping[str, Any],
    actual: Mapping[str, Any],
    frozen: Mapping[str, Any],
) -> bool:
    if not _partial_causal_difference_is_allowed(actual, frozen):
        return False
    try:
        prove_experiment_reviewed_partial_commit(
            project,
            wave_number,
            evidence,
        )
    except FullPipelinePartialCommitV2Error:
        return False
    return True


def _precheck_processing_restart(
    experiment_root: Path,
    wave_number: int,
    document: Mapping[str, Any],
    wave_binding: Mapping[str, Any],
    processing: Mapping[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    experiment_manifest = harness.load_experiment_manifest(experiment_root)
    results = harness._result_map(document)
    frozen = _wave_binding_map(wave_binding)
    evidence_by_novel = {}
    frozen_causal = {}

    for novel_id in experiment_manifest["novel_ids"]:
        project = experiment_root / "projects" / novel_id
        item = results[novel_id]
        evidence = _acceptance_for_result(
            processing,
            experiment_manifest,
            document,
            item,
        )
        evidence_by_novel[novel_id] = evidence
        before = validate_experiment_state_binding(
            frozen[novel_id]["state_binding_before"]
        )
        frozen_causal[novel_id] = copy.deepcopy(before["causal"])

        committed = harness._committed_acceptance(
            project,
            wave_number,
            evidence,
        )
        if committed is not None:
            continue

        history = capture_pre_wave_history_guard(project, wave_number)
        if history != frozen[novel_id]["history_guard_before"]:
            raise FullPipelineExperimentContractV2Error(
                f"{novel_id} prior committed history changed during Wave "
                f"{wave_number} processing"
            )

        actual_causal = capture_causal_lineage(project)
        expected_causal = before["causal"]
        if actual_causal != expected_causal and not _prove_partial_causal_recovery(
            project,
            wave_number,
            evidence,
            actual_causal,
            expected_causal,
        ):
            raise FullPipelineExperimentContractV2Error(
                f"{novel_id} causal state drifted after Wave {wave_number} was frozen"
            )

        if _processing_entry_status(processing, novel_id) == "ready":
            actual_evidence = capture_committed_evidence(project)
            if actual_evidence != before["committed_evidence"]:
                raise FullPipelineExperimentContractV2Error(
                    f"{novel_id} committed/accounting evidence changed before "
                    "the durable pending-processing checkpoint"
                )

    return evidence_by_novel, frozen_causal


def _processing_capture_callback(
    experiment_root: Path,
    wave_number: int,
    frozen_causal: Mapping[str, Mapping[str, Any]],
    evidence_by_novel: Mapping[str, Mapping[str, Any]],
):
    project_to_novel = {
        (experiment_root / "projects" / novel_id).resolve(): novel_id
        for novel_id in frozen_causal
    }

    def capture(project_root):
        project = Path(project_root).resolve()
        novel_id = project_to_novel.get(project)
        actual = capture_causal_lineage(project)
        if novel_id is None:
            return actual
        frozen = frozen_causal[novel_id]
        if actual == frozen:
            return actual
        if _prove_partial_causal_recovery(
            project,
            wave_number,
            evidence_by_novel[novel_id],
            actual,
            frozen,
        ):
            # The Phase 60F harness compares against the frozen pre-wave causal
            # identity before invoking the engine.  Returning the frozen value
            # here is safe only because production's exact partial-commit proof
            # succeeded; the real reconciler then validates associated state.
            return copy.deepcopy(dict(frozen))
        return actual

    return capture


def process_collected_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    client_factory: Callable[[str, int], Any],
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    root = Path(experiment_root).resolve()
    load_experiment_contract(root)
    wave_binding = load_wave_binding(root, wave_number)
    document = harness._load_collected(root, wave_number)
    processing = _processing_state(root, wave_number)
    evidence_by_novel, frozen_causal = _precheck_processing_restart(
        root,
        wave_number,
        document,
        wave_binding,
        processing,
    )
    capture = _processing_capture_callback(
        root,
        wave_number,
        frozen_causal,
        evidence_by_novel,
    )
    with _harness_contract(capture):
        result = harness.process_collected_wave(
            root,
            repository_root,
            wave_number,
            client_factory=client_factory,
            crash_hook=crash_hook,
        )

    processing_path = (
        root / "waves" / f"wave_{wave_number:04d}" / harness.PROCESSING_FILENAME
    )
    durable = harness._read_object(processing_path, "wave processing manifest")
    completed_bindings = {}
    for entry in durable.get("requests", []):
        if not isinstance(entry, dict):
            continue
        novel_id = entry.get("novel_id")
        if novel_id not in frozen_causal:
            continue
        binding = capture_experiment_state_binding(root / "projects" / novel_id)
        entry["state_binding_v2_after"] = binding
        completed_bindings[novel_id] = binding
    durable["state_binding_contract_revision"] = CONTRACT_REVISION
    durable["completed_state_bindings_v2"] = completed_bindings
    harness.atomic_write_json(processing_path, durable)
    return durable
