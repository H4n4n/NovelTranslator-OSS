"""Production-only original chapter-review recovery; frozen G7J stays fail closed."""
import copy
import hashlib
from pathlib import Path

from production_goat_policy import classify_auxiliary_response
from benchmark_longitudinal_provider import build_provider_request_identity
from production_goat_cap_recovery import accepted_envelope
from production_failure_classification import NonProviderProductionError
from production_paid_request_journal import request_identity, _sha256_json
from production_provider_runtime import provider_identity
from production_provider_contract import PROVIDER_COMMAND_CODE

REVISION = "goat-glossary-review-reasoning-empty-stop-recovery-v2-unbounded"
STRATEGY = "fresh_full_glossary_review_same_controls"
KIND = "chapter_glossary_review_reasoning_empty_recovery"
CLASSIFICATION = "auxiliary_empty_content_anomalous_finish"


def policy_sha256():
    return _sha256_json(dict(revision=REVISION, strategy=STRATEGY, kind=KIND,
        trigger=CLASSIFICATION, provider=PROVIDER_COMMAND_CODE,
        model="deepseek/deepseek-v4.1-flash", reasoning_effort="low",
        auxiliary_completion_policy="provider_unbounded", auxiliary_completion_tokens=None,
        attempts=1, original_only=True))


def initial_kind(wire):
    # Preserve the original context journal identity, including pre-policy incidents.
    return "context_glossary_review_" + build_provider_request_identity(request_body=wire).request_sha256


def eligible_envelope(envelope, wire, *, original=False):
    try:
        stats = envelope["stats"]
        diagnostic = classify_auxiliary_response(logical_operation="glossary_review",
            content=envelope["content"], finish_reason=envelope["finish_reason"],
            completion_tokens=stats["completion_tokens"],
            reasoning_tokens=stats.get("reasoning_tokens"))
        return (original is True and wire["model"] == "deepseek/deepseek-v4.1-flash"
            and wire["reasoning_effort"] == "low"
            and "max_completion_tokens" not in wire and "max_tokens" not in wire
            and envelope["provider_identity"] == provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
            and envelope["provider_response_model"] == wire["model"]
            and envelope["finish_reason"] == "stop"
            and isinstance(envelope["content"], str) and not envelope["content"].strip()
            and type(stats["completion_tokens"]) is int and stats["completion_tokens"] > 0
            and stats["reasoning_tokens_exposed"] is True
            and type(stats["reasoning_tokens"]) is int
            and stats["reasoning_tokens"] == stats["completion_tokens"]
            and type(stats["visible_completion_tokens"]) is int and stats["visible_completion_tokens"] == 0
            and diagnostic["status"] == CLASSIFICATION
            and diagnostic["fail_closed"] is True and diagnostic["repair_allowed"] is False
            and accepted_envelope(envelope, wire) == envelope)
    except (KeyError, ValueError, TypeError, RuntimeError):
        return False


def journal_identity(*, kind, novel_id, chapter, profile_id, source_sha256, wire, proof=None):
    provider = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    result = request_identity(operation_kind=kind, novel_id=novel_id, chapter=chapter,
        profile_id=profile_id, provider=PROVIDER_COMMAND_CODE, model=provider["wire_model"],
        source_sha256=source_sha256, translation_sha256=_sha256_json(proof) if proof else "",
        request_payload={"wire_request": wire, "provider_policy": provider})
    result.update(provider_policy=copy.deepcopy(provider),
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
    if proof:
        result["reasoning_empty_recovery"] = copy.deepcopy(proof)
    return result


class GlossaryRecoveryProofError(NonProviderProductionError):
    failure_kind = "local_safety"


def prove_initial(record, wire, expected):
    if (not isinstance(record, dict) or record.get("status") != "response_recorded"
        or any(record.get(key) != value for key, value in expected.items())
        or record.get("response_sha256") != _sha256_json(record.get("response"))
        or not eligible_envelope(record["response"], wire, original=True)):
        raise GlossaryRecoveryProofError("original glossary review lacks exact durable GOAT empty STOP proof")
    return record


def recovery_proof(record, wire, expected, *, metadata, translation, journal_path):
    prove_initial(record, wire, expected)
    response = record["response"]
    stats = response["stats"]
    return dict(novel_id=record["novel_id"], chapter=record["chapter"], profile_id=record["profile_id"],
        source_sha256=record["source_sha256"],
        translation_sha256=hashlib.sha256(translation.encode("utf-8")).hexdigest(),
        checkpoint_binding=copy.deepcopy(metadata["checkpoint_binding"]),
        review_input_hash=metadata["input_hash"], original_domain_operation_id=metadata["operation_id"],
        initial_operation_id=record["operation_id"], initial_journal_path="data/provider_request_journal/" + Path(journal_path).name,
        initial_journal_identity=copy.deepcopy(expected), initial_wire_sha256=record["wire_request_sha256"],
        initial_response_sha256=record["response_sha256"], initial_returned_model=response["provider_response_model"],
        initial_finish_reason=response["finish_reason"], initial_completion_tokens=stats["completion_tokens"],
        initial_reasoning_tokens=stats["reasoning_tokens"], initial_visible_tokens=stats["visible_completion_tokens"],
        policy_revision=REVISION, policy_sha256=policy_sha256(), strategy=STRATEGY,
        recovery_wire_sha256=build_provider_request_identity(request_body=wire).request_sha256)


def pending_binding(meta):
    """Stable saved checkpoints; excludes additive review-result persistence."""
    return {key: _sha256_json(meta.get(key)) for key in (
        "state", "state_stats", "state_checkpoint_identity", "candidate_preparation",
        "translation_stats", "local_qa")}


def validate_recovery_accounting(stats, proof):
    """The effective original always carries both accepted paid attempts once."""
    from production_accounting import validate_goat_stats
    validate_goat_stats(stats)
    operations = stats.get("glossary_review_accepted_operations", [])
    expected_ids = [proof["initial_operation_id"], proof["recovery_operation_id"]]
    if (len(operations) != 2 or expected_ids[0] == expected_ids[1]
        or [item.get("operation_id") for item in operations] != expected_ids
        or stats.get("accounting_components", [])[:2] != [item.get("stats") for item in operations]
        or operations[0]["stats"].get("completion_tokens") != proof["initial_completion_tokens"]
        or operations[0]["stats"].get("reasoning_tokens") != proof["initial_reasoning_tokens"]
        or operations[0]["stats"].get("visible_completion_tokens") != 0
        or any(item["stats"].get("request_sha256") != proof["initial_wire_sha256"] for item in operations)):
        raise GlossaryRecoveryProofError("glossary recovery paid accounting chain mismatch")
