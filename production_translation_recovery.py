"""Bounded full-translation recovery; import-safe, no transport or filesystem IO.

The caller supplies the existing durable journal transport and an exact initial
record proof. Initial partial prose is never added to provider messages.
"""
from dataclasses import dataclass, replace
import copy
import hashlib
import json
import math
import re
import time

from production_failure_classification import NonProviderProductionError


TRANSLATION_RECOVERY_SCHEMA_VERSION = 1
TRANSLATION_RECOVERY_POLICY_REVISION = "translation-recovery-policy-v1"
TRANSLATION_RECOVERY_STRATEGY = "fresh_full_translation"
MINIMUM_SATURATION_CHARACTERS = 20
MINIMUM_SATURATION_WORDS = 3


def translation_recovery_policy_identity(policy):
    """Only translation recovery semantics; auxiliary/global revisions excluded."""
    return {
        "schema_version": TRANSLATION_RECOVERY_SCHEMA_VERSION,
        "revision": TRANSLATION_RECOVERY_POLICY_REVISION,
        "model_binding": {"model": policy.model_id, "version": policy.model_version},
        "initial_translation": policy.translation.identity(),
        "recovery_translation": (None if policy.translation_recovery is None
                                 else policy.translation_recovery.identity()),
        "strategy": TRANSLATION_RECOVERY_STRATEGY,
        "saturation_minimum_characters": MINIMUM_SATURATION_CHARACTERS,
        "saturation_minimum_words": MINIMUM_SATURATION_WORDS,
    }


def translation_recovery_policy_sha256(policy):
    return response_sha256(translation_recovery_policy_identity(policy))


class TranslationRecoveryError(NonProviderProductionError):
    failure_kind = "local_safety"


def response_sha256(response):
    return hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def classify_initial_response(response):
    reason = response.get("finish_reason")
    content = response.get("content")
    if reason != "length" and reason != "stop":
        return "unexpected_finish_reason"
    if not isinstance(content, str) or not content.strip():
        return "blank_visible_content"
    if reason == "stop":
        return "success"
    # Disabled thinking must have produced substantive visible English. Reject
    # whitespace, punctuation and tiny fragments, even when labeled LENGTH.
    if len(re.sub(r"\s", "", content)) < MINIMUM_SATURATION_CHARACTERS or len(re.findall(r"[A-Za-z]+", content)) < MINIMUM_SATURATION_WORDS:
        return "insubstantial_length_response"
    return "durable_length_response"


def aggregate_translation_stats(operations, combine_stats):
    """Retain individual accepted prices: aggregate must never be repriced."""
    if not isinstance(operations, list) or len(operations) != 2:
        raise TranslationRecoveryError("invalid accepted translation accounting")
    ids = [entry.get("operation_id") for entry in operations if isinstance(entry, dict)]
    if len(ids) != 2 or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids
    ) or len(set(ids)) != 2:
        raise TranslationRecoveryError("invalid accepted translation operation identities")
    stats = [entry.get("stats") for entry in operations]
    if any(not isinstance(value, dict) for value in stats):
        raise TranslationRecoveryError("invalid accepted translation usage")
    for value in stats:
        for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
                    "cost_usd", "rate_correct_cost_usd"):
            number = value.get(key)
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number < 0):
                raise TranslationRecoveryError("invalid accepted translation accounting value")
        if "translation_accepted_operations" in value:
            raise TranslationRecoveryError("nested translation accounting is forbidden")
    result = combine_stats(*stats)
    result["translation_accepted_operations"] = copy.deepcopy(operations)
    return result


@dataclass(frozen=True)
class TranslationStageResult:
    content: str
    stats: dict
    elapsed_seconds: float
    finish_reason: str
    initial: dict
    recovery: dict | None = None
    recovery_policy_sha256: str = ""
    policy_revision: str = ""
    failure: str = ""

    # Preserve the historical four-value translation adapter interface.
    def __iter__(self):
        return iter((self.content, self.stats, self.elapsed_seconds, self.finish_reason))

    @property
    def recovery_used(self):
        return self.recovery is not None and not self.failure

    @property
    def recovery_attempted(self):
        return self.recovery is not None

    def pending_provenance(self):
        if not self.recovery_used:
            return {}
        if self.policy_revision in ("goat-reasoning-saturation-recovery-v1", "goat-cap-failure-recovery-v2"):
            from production_provider_runtime import recovery_module
            return recovery_module({"translation_recovery_policy_revision": self.policy_revision}).pending_provenance(self)
        return {
            "translation_recovery_used": True,
            "initial_translation_operation_id": self.initial["operation_id"],
            "recovery_translation_operation_id": self.recovery["operation_id"],
            "initial_finish_reason": self.initial["response"]["finish_reason"],
            "recovery_finish_reason": self.recovery["response"]["finish_reason"],
            "initial_response_sha256": response_sha256(self.initial["response"]),
            "recovery_response_sha256": response_sha256(self.recovery["response"]),
            "translation_recovery_policy_sha256": self.recovery_policy_sha256,
            "translation_recovery_policy_revision": self.policy_revision,
            "translation_recovery_max_tokens": 24000,
            "translation_recovery_strategy": TRANSLATION_RECOVERY_STRATEGY,
        }


def validate_recovery_provenance(meta):
    if meta.get("goat_reasoning_saturation_recovery_used") is True:
        from production_provider_runtime import recovery_module
        return recovery_module(meta).validate_provenance(meta)
    if meta.get("translation_recovery_used") is not True:
        raise TranslationRecoveryError("invalid translation recovery provenance flag")
    if (meta.get("translation_model") != "deepseek-flash"
        or meta.get("translation_model_version") != "DeepSeek-V4.1-Flash"
        or meta.get("translation_thinking") != "disabled"
        or meta.get("initial_finish_reason") != "length"
        or meta.get("recovery_finish_reason") != "stop"
        or meta.get("translation_recovery_max_tokens") != 24000
        or meta.get("translation_recovery_strategy") != TRANSLATION_RECOVERY_STRATEGY):
        raise TranslationRecoveryError("invalid translation recovery model/controls")
    for key in ("initial_translation_operation_id", "recovery_translation_operation_id",
                "initial_response_sha256", "recovery_response_sha256",
                "translation_recovery_policy_sha256"):
        if not isinstance(meta.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", meta[key]):
            raise TranslationRecoveryError("invalid translation recovery identity")
    if not isinstance(meta.get("translation_recovery_policy_revision"), str) or not meta["translation_recovery_policy_revision"]:
        raise TranslationRecoveryError("missing translation recovery policy revision")
    from production_operation_policy import policy_for_model
    if (meta["translation_recovery_policy_revision"] != TRANSLATION_RECOVERY_POLICY_REVISION
        or meta["translation_recovery_policy_sha256"] != translation_recovery_policy_sha256(
            policy_for_model("v41-flash"))):
        raise TranslationRecoveryError("translation recovery semantic identity mismatch")
    stats = meta.get("translation_stats")
    if not isinstance(stats, dict):
        raise TranslationRecoveryError("missing translation recovery accounting")
    operations = stats.get("translation_accepted_operations", [])
    if (not isinstance(operations, list) or len(operations) != 2
        or [entry.get("operation_id") for entry in operations if isinstance(entry, dict)] != [
            meta["initial_translation_operation_id"], meta["recovery_translation_operation_id"]]):
        raise TranslationRecoveryError("translation recovery accounting chain mismatch")
    if meta["initial_translation_operation_id"] == meta["recovery_translation_operation_id"]:
        raise TranslationRecoveryError("translation recovery must be a separate operation")


def run_translation_stage(*, policy, initial_kwargs, request, prove_initial,
                          combine_stats, emit=lambda *args, **kwargs: None, clock=time.perf_counter):
    """Exactly one initial slot and, only if eligible, one separate recovery slot.

    request(kind, kwargs, binding) must return a durably recorded/replayed envelope
    through the production journal/model guard. prove_initial must re-read and
    match the exact current initial request; no recorded response means no recovery.
    """
    started = clock()
    initial = request("chapter_translation", copy.deepcopy(initial_kwargs), "")
    response = initial["response"]
    if response.get("provider_identity", {}).get("provider") == "command_code_provider":
        from production_goat_runtime import normalize_production_request
        from production_goat_cap_recovery import eligible_envelope, run_goat_translation_stage
        wire = normalize_production_request("translation", initial_kwargs)
        if eligible_envelope(response, wire):
            return run_goat_translation_stage(initial=initial, initial_kwargs=initial_kwargs,
                wire=wire, request=request, prove=prove_initial, combine_stats=combine_stats,
                elapsed=lambda: clock()-started)
        if response.get("finish_reason") != "stop" or not response.get("content", "").strip():
            raise TranslationRecoveryError("GOAT translation rejected without recovery")
    classification = classify_initial_response(response)
    def result(final, recovery=None, failure=""):
        return TranslationStageResult(
            content=final["response"]["content"], stats=final["response"]["stats"],
            elapsed_seconds=clock()-started, finish_reason=final["response"]["finish_reason"],
            initial=initial, recovery=recovery, failure=failure,
            recovery_policy_sha256=translation_recovery_policy_sha256(policy) if recovery else "",
            policy_revision=TRANSLATION_RECOVERY_POLICY_REVISION,
        )
    if classification == "success":
        return result(initial)
    if classification != "durable_length_response":
        return result(initial, failure=classification)
    recovery_policy = policy.translation_recovery
    if (policy.model_id != "deepseek-flash" or policy.model_version != "DeepSeek-V4.1-Flash"
        or recovery_policy is None):
        return result(initial, failure="translation_recovery_unavailable")
    expected_initial = {"model": "deepseek-flash", "messages": initial_kwargs.get("messages"),
                        "max_tokens": 12000, "extra_body": {"thinking": {"type": "disabled"}}}
    if (initial_kwargs != expected_initial or policy.translation.request_controls() != {
        "max_tokens": 12000, "extra_body": {"thinking": {"type": "disabled"}}}
        or recovery_policy.request_controls() != {
        "max_tokens": 24000, "extra_body": {"thinking": {"type": "disabled"}}}
        or recovery_policy.trigger != "durable_length_response"):
        raise TranslationRecoveryError("unreviewed translation recovery controls")
    record = prove_initial()
    if (not isinstance(record, dict) or record.get("status") != "response_recorded"
        or record.get("operation_id") != initial.get("operation_id")
        or record.get("response") != response
        or record.get("response_sha256") != response_sha256(response)):
        raise TranslationRecoveryError("initial translation response lacks exact durable proof")
    policy_hash = translation_recovery_policy_sha256(policy)
    binding = json.dumps({"initial_operation_id": initial["operation_id"],
                          "initial_response_sha256": record["response_sha256"],
                          "recovery_policy_sha256": policy_hash}, sort_keys=True, separators=(",", ":"))
    fields = {"initial_operation_id": initial["operation_id"], "policy_revision": TRANSLATION_RECOVERY_POLICY_REVISION,
              "initial_finish_reason": "length", "initial_response_sha256": record["response_sha256"]}
    emit("translation_saturation_detected", **fields)
    kwargs = copy.deepcopy(initial_kwargs)
    kwargs.update(recovery_policy.request_controls())
    emit("translation_recovery_start", completion_ceiling=24000, **fields)
    try:
        recovery = request("chapter_translation_recovery", kwargs, binding)
    except BaseException as exc:
        emit("translation_recovery_failed", error_type=type(exc).__name__, **fields)
        raise
    recovered = recovery["response"]
    failure = ("unexpected_finish_reason" if recovered.get("finish_reason") not in ("stop", "length")
               else "completion_budget_exhausted" if recovered.get("finish_reason") == "length"
               else "blank_visible_content" if not isinstance(recovered.get("content"), str)
               or not recovered["content"].strip() else "")
    if recovery["replayed"]:
        emit("translation_recovery_replayed", recovery_operation_id=recovery["operation_id"], **fields)
    emit("translation_recovery_failed" if failure else "translation_recovery_end",
         recovery_operation_id=recovery["operation_id"], recovery_finish_reason=recovered["finish_reason"],
         replayed=recovery["replayed"], failure=failure, **fields)
    final = result(recovery, recovery, failure)
    operations = [{"operation_id": value["operation_id"], "stats": value["response"]["stats"]}
                  for value in (initial, recovery)]
    return replace(final, stats=aggregate_translation_stats(operations, combine_stats))


def prove_recorded_pending_prefix(meta, initial, recovered, text=None):
    """Pure proof for the metadata-first pending write prefix, never send authority.

    Both records must already have passed the existing journal hash/identity guard.
    Normal request reconstruction still runs before a missing Markdown is written.
    """
    validate_recovery_provenance(meta)
    if (initial.get("status") != "response_recorded" or recovered.get("status") != "response_recorded"
        or initial.get("response_sha256") != meta["initial_response_sha256"]
        or recovered.get("response_sha256") != meta["recovery_response_sha256"]
        or classify_initial_response(initial["response"]) != "durable_length_response"
        or recovered["response"].get("finish_reason") != "stop"
        or not isinstance(recovered["response"].get("content"), str)
        or not recovered["response"]["content"].strip()
        or meta.get("finish_reason") != "stop"):
        raise TranslationRecoveryError("unproven recorded translation recovery prefix")
    for key in ("novel_id", "chapter", "provider", "model", "profile_id", "source_sha256"):
        if initial.get(key) != recovered.get(key):
            raise TranslationRecoveryError("translation recovery prefix source/context mismatch")
    if initial.get("translation_sha256") != "":
        raise TranslationRecoveryError("invalid initial translation prefix binding")
    for record in (initial, recovered):
        if record["response"].get("provider_response_model") != "deepseek-flash":
            raise TranslationRecoveryError("translation recovery prefix provider model mismatch")
    binding = json.dumps({"initial_operation_id": initial["operation_id"],
        "initial_response_sha256": initial["response_sha256"],
        "recovery_policy_sha256": meta["translation_recovery_policy_sha256"]},
        sort_keys=True, separators=(",", ":"))
    if recovered.get("translation_sha256") != hashlib.sha256(binding.encode("utf-8")).hexdigest():
        raise TranslationRecoveryError("translation recovery prefix identity mismatch")
    operations = meta["translation_stats"]["translation_accepted_operations"]
    if [entry["stats"] for entry in operations] != [initial["response"]["stats"], recovered["response"]["stats"]]:
        raise TranslationRecoveryError("translation recovery prefix accepted usage mismatch")
    fields = ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
              "cost_usd", "rate_correct_cost_usd")
    aggregate = aggregate_translation_stats(operations,
        lambda *values: {key: sum(value[key] for value in values) for key in fields})
    if any(isinstance(meta["translation_stats"].get(key), bool)
           or meta["translation_stats"].get(key) != aggregate[key] for key in fields):
        raise TranslationRecoveryError("translation recovery prefix aggregate accounting mismatch")
    if text is not None and text != recovered["response"]["content"]:
        raise TranslationRecoveryError("translation recovery prefix pending text mismatch")
