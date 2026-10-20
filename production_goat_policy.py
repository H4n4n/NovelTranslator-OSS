"""Versioned production GOAT policy, independent of frozen benchmark budgets."""
import hashlib
import json

SCHEMA_VERSION = 2
REVISION = "goat-production-low-50k-unbounded-auxiliary-v2"
AUXILIARY_COMPLETION_POLICY = "provider_unbounded"
WIRE_POLICY_REVISION = "goat-auxiliary-wire-provider-unbounded-v1"
TRANSLATION_MAX_COMPLETION_TOKENS = 50_000
NEAR_CEILING_TOLERANCE = 64


def policy_identity():
    return dict(schema_version=SCHEMA_VERSION, revision=REVISION,
        provider="command_code_provider", wire_model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="low", max_completion_tokens=TRANSLATION_MAX_COMPLETION_TOKENS,
        auxiliary_completion_policy=AUXILIARY_COMPLETION_POLICY,
        auxiliary_completion_tokens=None, auxiliary_wire_policy_revision=WIRE_POLICY_REVISION,
        max_retries=0, fallback=None,
        legacy_translation_max_tokens=12000)


def policy_sha256():
    return hashlib.sha256(json.dumps(policy_identity(), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def historical_policy_identity_v1():
    """Exact 50K/8K production snapshot, never active request controls."""
    return dict(schema_version=1, revision="goat-production-low-50k-v1",
        provider="command_code_provider", wire_model="deepseek/deepseek-v4.1-flash",
        reasoning_effort="low", max_completion_tokens=50000,
        auxiliary_max_completion_tokens=8000, max_retries=0, fallback=None,
        legacy_translation_max_tokens=12000)


def bind_auxiliary_checkpoint(value, identity):
    from production_saved_checkpoint_identity import _finalize_identity
    value = dict(value)
    value.pop("identity_sha256", None)
    value["provider_identity"] = identity
    if identity.get("auxiliary_completion_policy") == "provider_unbounded":
        value["wire_completion_policy"] = dict(revision=WIRE_POLICY_REVISION,
            auxiliary_completion_policy="provider_unbounded", auxiliary_completion_tokens=None)
    else:
        # Explicit historical reconstruction only; never relabel old evidence.
        value["wire_max_completion_tokens"] = identity["auxiliary_completion_tokens"]
    return _finalize_identity(value)


def classify_auxiliary_response(*, logical_operation, content, finish_reason, **diagnostics):
    """Production classification has no benchmark token-ceiling assumption.

    State LENGTH/blank STOP reaches its existing one-recovery controller. Audit
    LENGTH reaches its existing bounded split protocol. Other domains fail closed.
    """
    status = ("auxiliary_length" if finish_reason == "length" else
        "auxiliary_unsupported_finish" if finish_reason != "stop" else
        "auxiliary_empty_content_anomalous_finish" if not isinstance(content, str) or not content.strip()
        else "ok")
    return dict(status=status, fail_closed=status != "ok", repair_allowed=status == "ok")


def translation_completion_budget(*, logical_operation, requested_tokens, reasoning_effort, goat=True):
    # Only the reviewed legacy bridge or the production-owned physical ceiling.
    if (not goat or logical_operation != "translation" or reasoning_effort != "low"
        or type(requested_tokens) is not int or requested_tokens not in (12000, 50000)):
        raise ValueError("production GOAT translation controls drift")
    return TRANSLATION_MAX_COMPLETION_TOKENS


def classify_translation_response(*, content, finish_reason, **diagnostics):
    if finish_reason == "length":
        return {"status": "incomplete_length_translation"}
    if finish_reason != "stop":
        return {"status": "unsupported_translation_finish_reason"}
    if not isinstance(content, str) or not content.strip():
        return {"status": "empty_visible_content"}
    return {"status": "ok"}
