"""AUT-1 supervisor: exact stage outcomes, no provider or filesystem capability.

Domain callbacks retain eligibility, operation budgets, durable identities and
paid journals. A supervisor invocation never retries a provider callback. The
only optional second action is the existing local audit finalization proof.
Status is diagnostic, never send authority or a replacement recovery journal.
"""
from dataclasses import dataclass
import hashlib
import json

from production_failure_classification import (
    LocalPersistenceError, NonProviderProductionError, ProductionControlError,
)
from production_paid_request_journal import PaidRequestJournalError, PaidRequestUncertainError
from production_saved_checkpoint_identity import SavedCheckpointIdentityError
from production_state_extraction import StateExtractionError

REVISION = "aut1-recovery-supervisor-v1"
STAGES = frozenset({"translation", "strict_deterministic", "strict_recorded",
                    "strict_micro", "residual_cjk", "commit_qa", "state",
                    "candidates", "glossary_review", "reviewed_state", "chapter_commit",
                    "audit_acquisition", "audit_publication"})
REPAIR_SUCCESS = frozenset({"clean", "succeeded", "succeeded_deterministic_cleanup",
    "succeeded_from_recorded_candidate", "succeeded_from_revalidated_recorded_candidate",
    "succeeded_with_unrelated_blockers", "succeeded_from_recorded_candidate_with_unrelated_blockers",
    "succeeded_from_revalidated_recorded_candidate_with_unrelated_blockers"})


def identity_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveryDecision:
    disposition: str
    reason: str
    next_safe_step: str


def stopped(reason):
    return RecoveryDecision("BLOCKED_UNVERIFIED", reason, "inspect_preserved_evidence")


def classify_exception(exc):
    """Typed evidence only. Never infer resend eligibility from exception text."""
    if isinstance(exc, PaidRequestUncertainError):
        return stopped("AMBIGUOUS_PAID_REQUEST")
    if isinstance(exc, (PaidRequestJournalError, SavedCheckpointIdentityError)):
        return stopped("IDENTITY_OR_PROOF_MISMATCH")
    if isinstance(exc, StateExtractionError):
        return stopped("STATE_RECOVERY_EXHAUSTED" if exc.category in {
            "recovery_completion_budget_exhausted", "recovery_blank_visible_content",
            "repair_completion_budget_exhausted", "repair_blank_visible_content",
        } else "STATE_OUTPUT_UNPROVEN")
    if isinstance(exc, (LocalPersistenceError, OSError)):
        return stopped("LOCAL_PERSISTENCE_FAILURE")
    if isinstance(exc, NonProviderProductionError):
        return stopped("DOMAIN_RECOVERY_BLOCKED")
    return stopped("UNCLASSIFIED_FAILURE")


def decide_recovery(stage, result):
    """Classify existing validated outcomes; this is not an eligibility validator."""
    if stage not in STAGES:
        return stopped("UNSUPPORTED_STAGE")
    if stage == "audit_acquisition" and isinstance(result, tuple) and len(result) == 3:
        return RecoveryDecision("CONTINUE", "AUDIT_ACQUIRED", "prove_local_finalization")
    if not isinstance(result, dict):
        return stopped("UNSUPPORTED_STAGE_RESULT")
    if stage == "translation":
        if result.get("source") == "pending":
            return RecoveryDecision("ALREADY_HANDLED", "PAID_PENDING_REUSED", "continue_required_qa")
        if result.get("source") != "generated":
            return stopped("UNSUPPORTED_STAGE_RESULT")
        generated = result.get("generated_result")
        if getattr(generated, "failure", ""):
            return stopped("TRANSLATION_RECOVERY_UNAVAILABLE_OR_EXHAUSTED")
        return RecoveryDecision("CONTINUE", "TRANSLATION_STAGE_RETURNED", "continue_required_qa")
    if stage in {"strict_micro", "strict_recorded", "residual_cjk"}:
        status = result.get("status")
        if status in REPAIR_SUCCESS:
            return RecoveryDecision("ALREADY_HANDLED", "REPAIR_STAGE_ACCEPTED", "continue_required_qa")
        if status in {"disabled", "not_applicable", "no_recorded_candidate", "no_revalidatable_recorded_candidate"}:
            return RecoveryDecision("CONTINUE", "NO_AUTOMATIC_ROUTE", "continue_required_qa")
        # Even a blocked patch cannot bypass the normal hard QA checkpoint.
        return RecoveryDecision("BLOCKED_UNVERIFIED",
            "REPAIR_BUDGET_EXHAUSTED" if status == "attempt_limit_reached" else "REPAIR_NOT_PROVEN",
            "continue_required_qa")
    if stage == "strict_deterministic":
        status = result.get("status")
        if status in {"succeeded", "partial_succeeded"}:
            return RecoveryDecision("ALREADY_HANDLED", "LOCAL_TERMINOLOGY_REPAIRED", "continue_required_qa")
        return RecoveryDecision("CONTINUE", "LOCAL_REPAIR_DEFERRED", "continue_required_qa")
    if stage == "commit_qa":
        return (RecoveryDecision("CONTINUE", "QA_PASSED", "continue_state_and_review")
                if result.get("commit_allowed") is True else stopped("MANDATORY_QA_UNRESOLVED"))
    if stage in {"state", "glossary_review"}:
        if result.get("status") == "failed":
            return classify_exception(result["error"])
        if result.get("status") == "recovered":
            return RecoveryDecision("ALREADY_HANDLED", "SAVED_CHECKPOINT_REUSED", "continue_chronologically")
        if result.get("status") not in {"extracted", "reviewed", "skipped"}:
            return stopped("UNSUPPORTED_STAGE_RESULT")
    if stage == "candidates" and result.get("status") != "prepared":
        return stopped("GLOSSARY_IDENTITY_UNRESOLVED")
    return RecoveryDecision("CONTINUE", "DOMAIN_STAGE_RETURNED", "continue_chronologically")


class RecoverySupervisor:
    """One dispatch per stage in a chapter; restarts use domain journals only.

    identity contains novel/chapter/model/profile/source/policy hashes supplied
    by the maintained adapter. No prose, exception messages or credentials are
    emitted. Existing per-novel structured logging owns persistence and remains
    best effort. Deleting logs cannot grant a paid retry.
    """
    def __init__(self, *, identity, emit):
        self.identity = identity_sha256(identity)
        self.emit = emit
        self.seen = set()

    def report(self, stage, decision, *, evidence_sha256=""):
        try:
            self.emit("autonomous_recovery", recovery_revision=REVISION,
                recovery_identity_sha256=self.identity, recovery_stage=stage,
                evidence_sha256=evidence_sha256,
                disposition=decision.disposition, reason_code=decision.reason,
                next_safe_step=decision.next_safe_step)
        except Exception:
            # Existing observability is best effort. Logs never own permission
            # to retry; losing a diagnostic cannot reset a domain attempt.
            pass

    def run(self, stage, callback, *, recover_local=None):
        if stage not in STAGES or stage in self.seen:
            self.report(stage if stage in STAGES else "unsupported", stopped("DUPLICATE_OR_UNSUPPORTED_DISPATCH"))
            raise ProductionControlError("AUT-1 duplicate or unsupported stage dispatch")
        if recover_local is not None and stage != "audit_publication":
            raise ProductionControlError("AUT-1 local recovery is restricted to audit publication")
        self.seen.add(stage)
        try:
            result = callback()
        except Exception as exc:
            self.report(stage, classify_exception(exc))
            if not isinstance(exc, OSError) or recover_local is None:
                raise
            # No acquisition callback is invoked again. This separate callback
            # must load and prove the existing finalization intent locally.
            self.report(stage, RecoveryDecision("CONTINUE", "LOCAL_AUDIT_RECONCILIATION",
                                                "prove_saved_finalization_intent"))
            try:
                result = recover_local()
            except Exception as recovery_exc:
                self.report(stage, classify_exception(recovery_exc))
                raise
            self.report(stage, RecoveryDecision("ALREADY_HANDLED", "LOCAL_AUDIT_RECONCILED",
                                                "continue_chronologically"))
            return result
        evidence = {}
        if isinstance(result, dict):
            text = result.get("translation")
            if isinstance(text, str):
                evidence["pending_sha256"] = identity_sha256(text)
            meta = result.get("meta") or {}
            if isinstance(meta, dict):
                for key in ("state_checkpoint_identity", "review_checkpoint_identity"):
                    if key in meta:
                        evidence[key] = identity_sha256(meta[key])
        self.report(stage, decide_recovery(stage, result),
                    evidence_sha256=identity_sha256(evidence))
        return result
