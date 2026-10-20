"""B2B2 partial identity/lock boundary. Unconditionally NO_SEND / NO_REPLAY.

No enrollment, checkpoint writer, context writer, ledger mutation, credentials,
transport, CLI or environment activation. An original B1 binding is not paid
permission. A separately qualified independent head/domain protocol is absent.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import production_goat_batch_ledger as ledger
from production_failure_classification import NonProviderProductionError
from production_paid_request_journal import JournalObservation
from production_run_lock import held_production_run_lock, verify_held_production_lock


STATUS = "B2B2_PARTIAL_NO_SEND"
BLOCKERS = (
    "INDEPENDENT_MONOTONIC_HEAD_AUTHORITY_REQUIRED",
    "DOMAIN_PROGRESSION_AND_PUBLICATION_PROOF_REQUIRED",
    "DURABLE_CONTEXT_RESERVE_ATTEMPT_SETTLEMENT_STOP_PROTOCOL_NOT_QUALIFIED",
)


class BatchBoundaryBlocked(NonProviderProductionError):
    failure_kind = "control"


@dataclass(frozen=True, slots=True)
class BatchObservation:
    """Frozen original parent + exact request + live caller-owned lock lease."""
    journal: JournalObservation
    wire_json: str
    batch_id: str
    original_binding_json: str
    original_fingerprint: str
    lock_lease: object

    @property
    def wire_request(self):
        return json.loads(self.wire_json)

    @property
    def authorization_binding(self):
        return json.loads(self.original_binding_json)


@dataclass(frozen=True, slots=True)
class BatchNoSendContext:
    """Explicit original-ledger context; no option or subclass can enable send.

    Runtime accepts this exact type only. Reopen uses retained original bytes,
    never a fresh B1 plan or automatically created missing batch/anchor.
    """
    batch_id: str
    original_binding_json: str
    original_identity_json: str

    @classmethod
    def from_ledger_locked(cls, batch_ledger):
        ledger.require(type(batch_ledger) is ledger.BatchLedger, "original BatchLedger required")
        held_production_run_lock(batch_ledger.root)
        batch_ledger.snapshot_locked()
        return cls(batch_ledger.identity["batch_id"],
            ledger.canonical(batch_ledger.binding).decode("utf-8"),
            ledger.canonical(batch_ledger.identity).decode("utf-8"))

    @property
    def novel_root(self):
        return Path(json.loads(self.original_identity_json)["novel_root"])

    def validate_runtime_locked(self, module):
        root = ledger.safe_path(Path(module.PAID_REQUEST_JOURNAL_DIR).absolute()).parent.parent
        ledger.require(root == self.novel_root, "batch/runtime novel path drift")
        binding = json.loads(self.original_binding_json)
        ledger.require(module.ACTIVE_NOVEL_CONFIG.id == binding["novel_id"]
            and module.ACTIVE_PROFILE.id == binding["profile"], "batch/runtime novel/profile drift")
        self._open_locked()

    def _open_locked(self):
        held_production_run_lock(self.novel_root)
        obj = ledger.BatchLedger.open_locked(self.novel_root, self.batch_id,
            json.loads(self.original_binding_json))
        ledger.require(ledger.canonical(obj.identity).decode("utf-8") == self.original_identity_json,
                       "original batch identity drift")
        return obj

    def observe_locked(self, journal_observation, wire):
        """Runtime callback. Validate what is provable, then always refuse."""
        lease = held_production_run_lock(self.novel_root)
        observation = BatchObservation(journal=journal_observation,
            wire_json=ledger.canonical(wire).decode("utf-8"), batch_id=self.batch_id,
            original_binding_json=self.original_binding_json,
            original_fingerprint=ledger.digest(json.loads(self.original_binding_json)),
            lock_lease=lease)
        self.inspect_observation_locked(observation)
        raise BatchBoundaryBlocked(STATUS + ": " + "; ".join(BLOCKERS))

    def inspect_observation_locked(self, observation):
        """Read-only diagnostics, never a send/replay capability or settlement.

        Even an exact settled receipt is not consumed here. Full bidirectional
        inventory, domain evolution and expected-head checks remain mandatory
        for any future consuming protocol; there is no permissive fallback.
        """
        from production_goat_batch_journal import BatchJournalEvidence, exposure_evidence, read_object
        from production_paid_request_journal import load_matching_journal, paid_request_journal_path
        from chapter_library import index_chapter_library

        ledger.require(type(observation) is BatchObservation
            and type(observation.journal) is JournalObservation, "exact frozen observation required")
        verify_held_production_lock(observation.lock_lease, self.novel_root)
        ledger.require(observation.batch_id == self.batch_id
            and observation.original_binding_json == self.original_binding_json
            and observation.original_fingerprint == ledger.digest(json.loads(self.original_binding_json)),
            "original B1 parent/batch drift")
        obj = self._open_locked()
        snapshot = obj.snapshot_locked()
        adapter = BatchJournalEvidence(obj)
        project = adapter._original_project()
        event = observation.journal
        identity, wire = event.identity, observation.wire_request
        path = ledger.safe_path(event.journal_path)
        ledger.require(event.phase in {"before_started", "replay", "response_recorded"}, "unknown observer phase")
        expected_path = paid_request_journal_path(project.data_dir / "provider_request_journal",
            identity["operation_kind"], identity["chapter"])
        ledger.require(str(path) == event.journal_path == str(expected_path), "fixed journal slot drift")
        attempt = event.attempt_context
        ledger.fields(attempt, {"request_started_utc"})
        context = exposure_evidence(batch_id=self.batch_id, journal_path=path,
            identity=identity, wire=wire, request_started_utc=attempt["request_started_utc"])
        request = dict(batch_id=self.batch_id, operation_id=identity["operation_id"],
            chapter=identity["chapter"], journal_path=str(path), journal_identity=identity,
            wire_request_sha256=identity["wire_request_sha256"],
            provider_policy_sha256=ledger.digest(identity["provider_policy"]),
            exposure=dict(unit=ledger.UNIT, nano_usd=context["nano_usd"],
                evidence_sha256=ledger.digest(context)))
        obj._request(request)  # pure original B2A range/policy/request validation
        ledger.require(identity["profile_id"] == obj.binding["profile"], "profile drift")
        sources = index_chapter_library(json.loads(project.chapters_file.read_text(encoding="utf-8")),
            project.config.final_chapter, project.config.chapter_numbering)
        ledger.require(hashlib.sha256(sources[identity["chapter"]]["text"].encode("utf-8")).hexdigest()
            == identity["source_sha256"], "source provenance drift")
        disk, raw = read_object(path)
        loaded = load_matching_journal(path, identity)
        ledger.require(ledger.canonical(disk) == ledger.canonical(loaded) == ledger.canonical(event.record)
            and path.read_bytes() == raw, "physical journal/observation drift")
        if event.phase == "before_started":
            ledger.require(disk["status"] == "planned" and disk["attempt_context"] == {}
                and disk["error"] == {}, "pre-STARTED journal drift")
        else:
            ledger.require(disk["status"] == "response_recorded" and disk["attempt_context"] == attempt,
                "replay/response attempt drift")
        return dict(status=STATUS, send_authorized=False, paid_execution_authorized=False,
            replay_consumption_authorized=False, execution_blockers=list(BLOCKERS),
            original_fingerprint=observation.original_fingerprint, batch_id=self.batch_id,
            ledger_head_sha256=snapshot["head_sha256"], phase=event.phase,
            operation_id=identity["operation_id"], request_started_utc=attempt["request_started_utc"],
            exposure_context_sha256=ledger.digest(context))
