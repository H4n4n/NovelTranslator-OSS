"""Production-only, one-slot GOAT 50K cap-failure recovery.

Pure proofs and orchestration; transport remains owned by the durable runtime.
No credentials, IO, retry, fallback, or reasoning text lives here.
"""
import copy
import hashlib
import json
from dataclasses import replace

from production_goat_policy import (NEAR_CEILING_TOLERANCE as NEAR_COMPLETION_CEILING_TOLERANCE,
    REVISION as PRODUCTION_POLICY_REVISION, policy_sha256 as production_policy_sha256)
from benchmark_longitudinal_provider import build_provider_request_identity, normalize_provider_response
from command_code_provider_transport import accept_command_code_response, account_command_code_usage
from benchmark_longitudinal_g7j_reasoning_budget import reasoning_usage_diagnostics
from production_provider_runtime import provider_identity, goat_provenance
from production_provider_contract import PROVIDER_COMMAND_CODE, COMMAND_CODE_DEEPSEEK_V41_FLASH
from production_paid_request_journal import request_identity
from production_translation_recovery import (
    TranslationRecoveryError, TranslationStageResult, response_sha256, aggregate_translation_stats,
)

REVISION = "goat-cap-failure-recovery-v2"
STRATEGY = "fresh_full_translation_same_controls"
KIND = "chapter_translation_cap_recovery_v2"


def policy_identity():
    return dict(schema_version=2, revision=REVISION, strategy=STRATEGY, provider=PROVIDER_COMMAND_CODE,
        wire_model=COMMAND_CODE_DEEPSEEK_V41_FLASH, reasoning_effort="low",
        max_completion_tokens=50000, max_recovery_attempts=1, operation_kind=KIND,
        trigger="near_ceiling_length", production_policy_revision=PRODUCTION_POLICY_REVISION,
        production_policy_sha256=production_policy_sha256(), near_ceiling_tolerance=NEAR_COMPLETION_CEILING_TOLERANCE,
        requires_exposed_all_reasoning_when_available=True, requires_exact_durable_response=True)


def policy_sha256():
    return response_sha256(policy_identity())


def accepted_envelope(recorded, wire, *, provider_policy=None):
    """Recompute exact provider/model/accounting acceptance from durable raw data."""
    identity = provider_policy or provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    transport = build_provider_request_identity(request_body=wire)
    accepted = accept_command_code_response(transport,
        normalize_provider_response(recorded["raw_response"], request_identity=transport))
    stats = account_command_code_usage(identity["wire_model"], accepted.usage, recorded["request_started_utc"])
    stats.update(reasoning_usage_diagnostics(completion_tokens=accepted.usage.completion_tokens,
        reasoning_tokens=accepted.usage.reasoning_tokens,
        reasoning_tokens_exposed=accepted.usage.reasoning_tokens_exposed))
    stats.update(provider=PROVIDER_COMMAND_CODE, accounting=identity["accounting"],
        provider_invoice_claimed=False, request_sha256=transport.request_sha256,
        pricing_unit=identity["pricing_unit"])
    return {**recorded, "content": accepted.content, "finish_reason": accepted.finish_reason,
        "stats": stats, "provider_response_model": accepted.response_model,
        "provider_response_id": accepted.provider_response_id,
        "request_sha256": transport.request_sha256, "provider_identity": identity}


def cap_failure_shape(envelope):
    content = envelope.get("content")
    if not isinstance(content, str):
        return ""
    if content.strip():
        # Visible LENGTH is always incomplete; only substantive English qualifies.
        import re
        return "visible_truncated" if len(re.findall(r"[A-Za-z]+", content)) >= 3 and len(content.strip()) >= 20 else ""
    stats = envelope.get("stats", {})
    if stats.get("reasoning_tokens_exposed"):
        if stats.get("reasoning_tokens") != stats.get("completion_tokens") or stats.get("visible_completion_tokens") != 0:
            return ""
    return "blank_all_reasoning"


def eligible_envelope(envelope, wire, *, kind="chapter_translation"):
    """Exact accepted recorded LENGTH within 64 tokens of 50K; never ambiguity."""
    try:
        stats = envelope["stats"]
        return (kind == "chapter_translation"
            and wire["model"] == COMMAND_CODE_DEEPSEEK_V41_FLASH
            and wire["reasoning_effort"] == "low"
            and type(wire["max_completion_tokens"]) is int and wire["max_completion_tokens"] == 50000
            and envelope["provider_identity"] == provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
            and envelope["provider_response_model"] == wire["model"]
            and envelope["finish_reason"] == "length"
            and type(stats["completion_tokens"]) is int
            and 50000 - NEAR_COMPLETION_CEILING_TOLERANCE <= stats["completion_tokens"] <= 50000
            and bool(cap_failure_shape(envelope))
            and accepted_envelope(envelope, wire) == envelope)
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def journal_identity(*, kind, novel_id, chapter, profile_id, source_sha256, wire, binding=""):
    identity = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    result = request_identity(operation_kind=kind, novel_id=novel_id, chapter=chapter,
        provider=PROVIDER_COMMAND_CODE, model=identity["wire_model"], profile_id=profile_id,
        source_sha256=source_sha256, translation_sha256=(
            hashlib.sha256(binding.encode("utf-8")).hexdigest() if binding else ""),
        request_payload={"wire_request": wire, "provider_policy": identity})
    if kind == KIND:
        result["reasoning_saturation_recovery"] = json.loads(binding)
        result["recovery_controls"] = {key: wire[key] for key in ("model", "reasoning_effort", "max_completion_tokens")}
    result["provider_policy"] = copy.deepcopy(identity)
    result["wire_request_sha256"] = build_provider_request_identity(request_body=wire).request_sha256
    return result


def prove_initial(record, wire):
    if not isinstance(record, dict) or record.get("status") != "response_recorded":
        raise TranslationRecoveryError("GOAT saturation requires RESPONSE_RECORDED")
    expected = journal_identity(kind="chapter_translation", novel_id=record["novel_id"],
        chapter=record["chapter"], profile_id=record["profile_id"],
        source_sha256=record["source_sha256"], wire=wire)
    if (any(record.get(key) != value for key, value in expected.items())
        or record.get("response_sha256") != response_sha256(record.get("response"))
        or not eligible_envelope(record["response"], wire)):
        raise TranslationRecoveryError("GOAT saturation initial exact proof failed")
    return record


def recovery_binding(record, wire):
    prove_initial(record, wire)
    return binding_from_record(record, build_provider_request_identity(request_body=wire).request_sha256)


def binding_from_record(record, request_sha):
    response = record["response"]
    stats = response["stats"]
    return json.dumps(dict(initial_operation_id=record["operation_id"],
        initial_response_sha256=record["response_sha256"],
        initial_response_model=response["provider_response_model"],
        initial_finish_reason=response["finish_reason"],
        initial_completion_tokens=stats["completion_tokens"],
        initial_reasoning_tokens=stats.get("reasoning_tokens"),
        initial_visible_completion_tokens=stats.get("visible_completion_tokens"),
        recovery_request_sha256=request_sha,
        recovery_policy_revision=REVISION, recovery_policy_sha256=policy_sha256(), strategy=STRATEGY,
        production_policy_revision=PRODUCTION_POLICY_REVISION, production_policy_sha256=production_policy_sha256(),
        cap_failure_shape=cap_failure_shape(response)),
        sort_keys=True, separators=(",", ":"))


def pending_provenance(result):
    initial, recovery = result.initial, result.recovery
    out = dict(goat_reasoning_saturation_recovery_used=True, translation_recovery_used=True,
        initial_translation_operation_id=initial["operation_id"],
        recovery_translation_operation_id=recovery["operation_id"],
        translation_recovery_policy_revision=REVISION, translation_recovery_policy_sha256=policy_sha256(),
        translation_recovery_strategy=STRATEGY, translation_recovery_max_tokens=50000,
        production_policy_revision=PRODUCTION_POLICY_REVISION, production_policy_sha256=production_policy_sha256(),
        cap_failure_shape=cap_failure_shape(initial["response"]), reasoning_effort="low")
    for label, value in (("initial", initial), ("recovery", recovery)):
        response = value["response"]
        out.update({f"{label}_response_sha256": response_sha256(response),
            f"{label}_request_sha256": response["request_sha256"],
            f"{label}_finish_reason": response["finish_reason"]})
        for key in ("completion_tokens", "reasoning_tokens_exposed", "reasoning_tokens", "visible_completion_tokens"):
            out[f"{label}_{key}"] = response["stats"].get(key)
    identity = initial["response"]["provider_identity"]
    out.update(goat_provenance(identity, recovery["response"]["request_sha256"]))
    out.update(translation_thinking="low", translation_max_tokens=50000,
               translation_pricing_version=identity["pricing_version"])
    return out


def run_goat_translation_stage(*, initial, initial_kwargs, wire, request, prove,
                               combine_stats, elapsed):
    record = prove()
    prove_initial(record, wire)
    if record["response"] != initial["response"] or record["operation_id"] != initial["operation_id"]:
        raise TranslationRecoveryError("GOAT initial replay/proof mismatch")
    binding = recovery_binding(record, wire)
    # Exactly one distinct slot; no loop, no recursion, no changed physical body.
    recovery = request(KIND, copy.deepcopy(initial_kwargs), binding)
    response = recovery["response"]
    failure = ("goat_recovery_terminal_response" if response.get("finish_reason") != "stop"
        or not isinstance(response.get("content"), str) or not response["content"].strip() else "")
    result = TranslationStageResult(content=response["content"], stats=response["stats"],
        elapsed_seconds=elapsed(), finish_reason=response["finish_reason"], initial=initial,
        recovery=recovery, recovery_policy_sha256=policy_sha256(), policy_revision=REVISION, failure=failure)
    return replace(result, stats=aggregate_translation_stats([
        {"operation_id": value["operation_id"], "stats": value["response"]["stats"]}
        for value in (initial, recovery)], combine_stats))


def validate_provenance(meta):
    from production_accounting import validate_goat_stats
    if (meta.get("goat_reasoning_saturation_recovery_used") is not True
        or meta.get("translation_recovery_policy_revision") != REVISION
        or meta.get("translation_recovery_policy_sha256") != policy_sha256()
        or meta.get("translation_recovery_strategy") != STRATEGY
        or meta.get("translation_recovery_max_tokens") != 50000
        or meta.get("initial_finish_reason") != "length" or meta.get("recovery_finish_reason") != "stop"
        or meta.get("translation_thinking") != "low"
        or meta.get("translation_provider_identity") != provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")):
        raise TranslationRecoveryError("GOAT recovery provenance policy mismatch")
    stats = meta.get("translation_stats", {})
    operations = stats.get("translation_accepted_operations", [])
    ids = [meta.get("initial_translation_operation_id"), meta.get("recovery_translation_operation_id")]
    if len(operations) != 2 or [item.get("operation_id") for item in operations] != ids or ids[0] == ids[1]:
        raise TranslationRecoveryError("GOAT recovery accounting chain mismatch")
    import re
    for key in ("initial_translation_operation_id", "recovery_translation_operation_id", "initial_response_sha256",
                "recovery_response_sha256", "initial_request_sha256", "recovery_request_sha256"):
        if not isinstance(meta.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", meta[key]):
            raise TranslationRecoveryError("GOAT recovery provenance identity malformed")
    validate_goat_stats(stats)
    if stats.get("accounting_components") != [item["stats"] for item in operations]:
        raise TranslationRecoveryError("GOAT recovery aggregate must contain exactly both accepted attempts")
    for label, item in zip(("initial", "recovery"), operations):
        validate_goat_stats(item["stats"])
        if meta[f"{label}_request_sha256"] != item["stats"].get("request_sha256"):
            raise TranslationRecoveryError("GOAT recovery request/accounting chain mismatch")
        for key in ("completion_tokens", "reasoning_tokens_exposed", "reasoning_tokens", "visible_completion_tokens"):
            if meta.get(f"{label}_{key}") != item["stats"].get(key):
                raise TranslationRecoveryError("GOAT recovery diagnostic chain mismatch")
    if (meta.get("production_policy_revision") != PRODUCTION_POLICY_REVISION
        or meta.get("production_policy_sha256") != production_policy_sha256()
        or meta.get("translation_max_tokens") != 50000 or meta.get("reasoning_effort") != "low"
        or meta.get("cap_failure_shape") not in ("blank_all_reasoning", "visible_truncated")
        or (meta["cap_failure_shape"] == "blank_all_reasoning" and meta.get("initial_reasoning_tokens_exposed") is True
            and (meta.get("initial_completion_tokens") != meta.get("initial_reasoning_tokens")
                 or meta.get("initial_visible_completion_tokens") != 0))
        or not 50000 - NEAR_COMPLETION_CEILING_TOLERANCE <= meta["initial_completion_tokens"] <= 50000
        or meta["initial_request_sha256"] != meta["recovery_request_sha256"]):
        raise TranslationRecoveryError("GOAT recovery trigger provenance mismatch")


def prove_pending_prefix(meta, initial, recovered, text=None):
    validate_provenance(meta)
    identity = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    for label, record in (("initial", initial), ("recovery", recovered)):
        if (record.get("status") != "response_recorded"
            or record.get("provider_policy") != identity
            or record.get("response_sha256") != meta[f"{label}_response_sha256"]
            or record.get("wire_request_sha256") != meta[f"{label}_request_sha256"]
            or record["response"].get("provider_response_model") != identity["wire_model"]
            or record["response"].get("provider_identity") != identity
            or record["response"].get("finish_reason") != meta[f"{label}_finish_reason"]):
            raise TranslationRecoveryError("GOAT pending prefix response/provider mismatch")
    for key in ("novel_id", "chapter", "profile_id", "source_sha256", "request_sha256"):
        if initial.get(key) != recovered.get(key):
            raise TranslationRecoveryError("GOAT pending prefix source/request mismatch")
    binding = binding_from_record(initial, recovered["wire_request_sha256"])
    if (initial.get("translation_sha256") != ""
        or recovered.get("translation_sha256") != hashlib.sha256(binding.encode("utf-8")).hexdigest()
        or recovered.get("reasoning_saturation_recovery") != json.loads(binding)
        or recovered.get("recovery_controls") != {"model": identity["wire_model"], "reasoning_effort": "low", "max_completion_tokens": 50000}
        or cap_failure_shape(initial["response"]) != meta.get("cap_failure_shape")
        or not recovered["response"]["content"].strip()
        or meta.get("finish_reason") != "stop"
        or [entry["stats"] for entry in meta["translation_stats"]["translation_accepted_operations"]]
           != [initial["response"]["stats"], recovered["response"]["stats"]]
        or (text is not None and text != recovered["response"]["content"])):
        raise TranslationRecoveryError("GOAT pending prefix chain mismatch")
