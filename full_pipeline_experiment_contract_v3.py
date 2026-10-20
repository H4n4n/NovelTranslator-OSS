"""Phase 60H contract for the chronological GPT-5.6 Luna Batch experiment.

Phase 60G's v2 contract remains the authority for causal lineage, committed
evidence, immutable Batch waves, and reviewed-partial-chapter recovery.  This
module adds exactly one operational contract on top of it: every real periodic
glossary audit executed by the experiment must use the Phase 60H paid-journal
and publication-recovery adapter.

The wrapper preserves the historical extra paid-journal proof for unparsed
response bytes carried by an interrupted experiment checkpoint. Recovery itself
uses the shared exact-request capability, preserving the original attempt rather
than rewinding its counters or discarding durable domain responses.

Ordinary production model selection and the v2 experiment lifecycle are not
modified.  Importing this module performs no filesystem writes, credential
loading, provider calls, or monkeypatching.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import full_pipeline_experiment_contract_v2 as v2
import full_pipeline_experiment_harness as harness
import full_pipeline_periodic_audit_recovery as audit_recovery
from production_paid_request_journal import (
    PaidRequestJournalError,
    load_matching_journal,
)


CONTRACT_SCHEMA_VERSION = 1
CONTRACT_REVISION = "full-pipeline-experiment-contract-v3-audit-recovery-v1"
CONTRACT_FILENAME = "experiment_contract_v3.json"
AUDIT_REPLAY_REVISION = "periodic-audit-exact-journal-replay-v3"


class FullPipelineExperimentContractV3Error(v2.FullPipelineExperimentContractV2Error):
    """The Phase 60H experiment contract is missing or inconsistent."""


def _binding_hash(value: Mapping[str, Any]) -> str:
    return harness._sha256_text(harness._canonical_json(dict(value)))


def _contract_path(experiment_root: Path | str) -> Path:
    return Path(experiment_root).resolve() / CONTRACT_FILENAME


def _write_contract(experiment_root: Path | str) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    manifest = harness.load_experiment_manifest(root)
    prior = v2.load_experiment_contract(root)
    stable = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "experiment_id": manifest["experiment_id"],
        "experiment_manifest_sha256": manifest["experiment_manifest_sha256"],
        "v2_contract_sha256": prior["contract_sha256"],
        "v2_contract_revision": prior["contract_revision"],
        "periodic_audit_recovery_revision": audit_recovery.RECOVERY_REVISION,
        "periodic_audit_replay_revision": AUDIT_REPLAY_REVISION,
    }
    document = copy.deepcopy(stable)
    document["contract_sha256"] = _binding_hash(stable)
    harness.atomic_write_json(_contract_path(root), document)
    return document


def load_experiment_contract(experiment_root: Path | str) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    prior = v2.load_experiment_contract(root)
    document = harness._read_object(
        _contract_path(root),
        "Phase 60H experiment contract",
    )
    if document.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise FullPipelineExperimentContractV3Error(
            "unsupported Phase 60H experiment contract schema"
        )
    if document.get("contract_revision") != CONTRACT_REVISION:
        raise FullPipelineExperimentContractV3Error(
            "unsupported Phase 60H experiment contract revision"
        )
    stable = dict(document)
    expected_hash = stable.pop("contract_sha256", None)
    if expected_hash != _binding_hash(stable):
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H experiment contract hash mismatch"
        )
    manifest = harness.load_experiment_manifest(root)
    if document.get("experiment_id") != manifest.get("experiment_id"):
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract belongs to a different experiment"
        )
    if document.get("experiment_manifest_sha256") != manifest.get(
        "experiment_manifest_sha256"
    ):
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract is bound to a different experiment manifest"
        )
    if document.get("v2_contract_sha256") != prior.get("contract_sha256"):
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract is bound to a different v2 contract"
        )
    if document.get("v2_contract_revision") != prior.get("contract_revision"):
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract carries the wrong v2 revision"
        )
    if document.get("periodic_audit_recovery_revision") != audit_recovery.RECOVERY_REVISION:
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract carries the wrong audit-recovery revision"
        )
    if document.get("periodic_audit_replay_revision") != AUDIT_REPLAY_REVISION:
        raise FullPipelineExperimentContractV3Error(
            "Phase 60H contract carries the wrong audit-replay revision"
        )
    return document


def _strict_reconcile_audit_checkpoint(
    production_module,
    project_root: Path,
    checkpoint_path: Path,
    start_chapter: int,
    end_chapter: int,
) -> dict[str, Any]:
    """Read-only experiment provenance check before shared reconciliation."""
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.is_file():
        return {"rewound": 0, "journal_status": "absent"}

    slot = audit_recovery._slot_path(project_root, start_chapter, end_chapter)
    manifest = audit_recovery._load_manifest(slot)
    if manifest.get("status") != "active":
        return {"rewound": 0, "journal_status": "committed"}

    checkpoint = audit_recovery._read_object(
        checkpoint_path,
        "periodic-audit checkpoint",
    )
    expected_window = {
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
    }
    if checkpoint.get("audit_window") != expected_window:
        raise FullPipelineExperimentContractV3Error(
            "periodic-audit checkpoint window differs from Phase 60H recovery slot"
        )

    unresolved: list[tuple[dict[str, Any], bool]] = []
    for record in checkpoint.get("attempt_records", []):
        if not isinstance(record, dict):
            continue
        if record.get("kind") not in audit_recovery._AUDIT_KINDS:
            continue
        # A parsed result is durable local evidence that the generic audit
        # engine can continue validation without crossing the provider boundary.
        if "parsed_result" in record:
            continue
        lifecycle = str(record.get("lifecycle") or "")
        try:
            chunks = [item for item in checkpoint.get("chunks", [])
                      if item.get("chunk_id") == record.get("chunk_id")]
            if len(chunks) != 1:
                raise audit_recovery.FullPipelinePeriodicAuditRecoveryError(
                    "cannot resolve exact audit chunk")
            chunk_status = str(chunks[0].get("status") or "")
        except audit_recovery.FullPipelinePeriodicAuditRecoveryError as exc:
            raise FullPipelineExperimentContractV3Error(
                "cannot resolve chunk for unresolved periodic-audit attempt"
            ) from exc

        local_response_carried = (
            isinstance(record.get("raw_content"), str)
            or lifecycle in {"response_received", "response_recorded"}
            or chunk_status in {"response_received", "response_recorded"}
        )
        unresolved_state = (
            lifecycle
            in {
                "prepared",
                "in_flight",
                "response_received",
                "response_recorded",
                "remote_outcome_uncertain",
            }
            or chunk_status
            in {
                "response_received",
                "response_recorded",
                "remote_outcome_uncertain",
            }
        )
        if unresolved_state:
            unresolved.append((record, local_response_carried))

    if len(unresolved) > 1:
        raise FullPipelineExperimentContractV3Error(
            "multiple unresolved periodic-audit paid attempts cannot be replayed"
        )
    if not unresolved:
        return {"rewound": 0, "journal_status": "resolved"}

    record, local_response_carried = unresolved[0]
    operation_id = record.get("operation_id")
    kind = record.get("kind")
    if not isinstance(operation_id, str) or not operation_id:
        raise FullPipelineExperimentContractV3Error(
            "unresolved periodic-audit attempt lacks operation_id"
        )

    path = audit_recovery._journal_path_for_attempt(
        production_module,
        kind,
        operation_id,
        end_chapter,
    )
    expected = audit_recovery._journal_expected_subset(
        production_module,
        kind,
        operation_id,
        end_chapter,
    )
    try:
        journal = load_matching_journal(path, expected)
    except PaidRequestJournalError as exc:
        raise FullPipelineExperimentContractV3Error(
            f"cannot validate periodic-audit paid journal {path}"
        ) from exc

    status = "absent" if journal is None else str(journal.get("status") or "")
    if status in {"started", "uncertain"}:
        raise FullPipelineExperimentContractV3Error(
            "periodic-audit paid request has an uncertain remote outcome; "
            f"automatic reissue is blocked: {path}"
        )
    if status not in {"absent", "planned", "response_recorded"}:
        raise FullPipelineExperimentContractV3Error(
            f"unsupported periodic-audit paid journal status at {path}: {status!r}"
        )
    if local_response_carried and status != "response_recorded":
        raise FullPipelineExperimentContractV3Error(
            "periodic-audit checkpoint carries provider response evidence but "
            "the durable paid journal does not prove response_recorded; "
            "automatic replay or reissue is blocked"
        )

    # Never discard a response or rewind an attempt. The journal-owning callback
    # reconstructs and validates the complete request at the shared seam.
    return {"rewound": 0, "journal_status": status,
            "local_response_discarded": False,
            "exact_request_replay_required": not local_response_carried}



@contextmanager
def _audit_runtime_contract():
    """Install Phase 60H only around v2's real engine loading seam."""
    original_install = harness.install_luna_auxiliary_runtime

    def install_with_audit_recovery(production_module, client, *args, **kwargs):
        state = original_install(production_module, client, *args, **kwargs)
        audit_recovery.install_periodic_audit_recovery(
            production_module,
            Path.cwd(),
            checkpoint_validator=_strict_reconcile_audit_checkpoint,
        )
        return state

    harness.install_luna_auxiliary_runtime = install_with_audit_recovery
    try:
        yield
    finally:
        harness.install_luna_auxiliary_runtime = original_install


def create_experiment(
    experiment_root: Path | str,
    repository_root: Path | str,
    *,
    repo_anchor: str,
    novel_ids: Sequence[str] = harness.DEFAULT_NOVEL_IDS,
    target_chapters: int = harness.DEFAULT_TARGET_CHAPTERS,
    experiment_id: str = "phase60-full-pipeline-luna-batch-v3",
    now: Callable[[], Any] = harness.utc_now,
) -> dict[str, Any]:
    manifest = v2.create_experiment(
        experiment_root,
        repository_root,
        repo_anchor=repo_anchor,
        novel_ids=novel_ids,
        target_chapters=target_chapters,
        experiment_id=experiment_id,
        now=now,
    )
    _write_contract(experiment_root)
    return manifest


def build_wave_requests(
    experiment_root: Path | str,
    engine_root: Path | str,
    wave_number: int,
):
    load_experiment_contract(experiment_root)
    return v2.build_wave_requests(experiment_root, engine_root, wave_number)


def load_wave_binding(experiment_root: Path | str, wave_number: int):
    load_experiment_contract(experiment_root)
    return v2.load_wave_binding(experiment_root, wave_number)


def prepare_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    now: Callable[[], Any] = harness.utc_now,
):
    load_experiment_contract(experiment_root)
    return v2.prepare_experiment_wave(
        experiment_root,
        repository_root,
        wave_number,
        now=now,
    )


def submit_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
    authorization: str,
):
    load_experiment_contract(experiment_root)
    return v2.submit_experiment_wave(
        experiment_root,
        repository_root,
        wave_number,
        client,
        authorization,
    )


def reconcile_experiment_wave_submission(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
):
    load_experiment_contract(experiment_root)
    return v2.reconcile_experiment_wave_submission(
        experiment_root,
        repository_root,
        wave_number,
        client,
    )


def refresh_experiment_wave_status(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
):
    load_experiment_contract(experiment_root)
    return v2.refresh_experiment_wave_status(
        experiment_root,
        repository_root,
        wave_number,
        client,
    )


def collect_experiment_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    client: Any,
    *,
    now: Callable[[], Any] = harness.utc_now,
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    load_experiment_contract(experiment_root)
    return v2.collect_experiment_wave(
        experiment_root,
        repository_root,
        wave_number,
        client,
        now=now,
        crash_hook=crash_hook,
    )


def process_collected_wave(
    experiment_root: Path | str,
    repository_root: Path | str,
    wave_number: int,
    *,
    client_factory: Callable[[str, int], Any],
    crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
):
    load_experiment_contract(experiment_root)
    with _audit_runtime_contract():
        return v2.process_collected_wave(
            experiment_root,
            repository_root,
            wave_number,
            client_factory=client_factory,
            crash_hook=crash_hook,
        )


# Read-only identity helpers remain exactly Phase 60G's implementation.
capture_causal_lineage = v2.capture_causal_lineage
capture_committed_evidence = v2.capture_committed_evidence
capture_experiment_state_binding = v2.capture_experiment_state_binding
capture_pre_wave_history_guard = v2.capture_pre_wave_history_guard
validate_experiment_state_binding = v2.validate_experiment_state_binding
