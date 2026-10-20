"""Frozen live G5C Provider capability results for the GOAT benchmark.

G5B1 remains the immutable static capability/design freeze. G5C then executed
exactly ten journal-protected, non-literary direct-Provider probes and accepted
all ten. This module records that live evidence without mutating the original
G5B1 capability identity.

The live probes verified canonical direct-Provider model spelling for all six
candidates, exact returned-model identity, prompt/completion usage reporting,
reasoning_effort acceptance for the exact eight adjustable-model cases, and
default/no-reasoning-field requests for Tencent and MiMo.

ZDR was deliberately disabled and therefore is NOT live-verified here.

This module performs no provider access, reads no credentials, and writes no
files. It also does not authorize G6 literary calls; G6 source-case selection,
evaluation protocol, and execution evidence must be frozen separately first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from benchmark_longitudinal_capabilities import (
    BENCHMARK_ZDR_ENABLED,
    CAPABILITY_FREEZE_SHA256,
    CAPABILITY_PROFILES,
    REASONING_WIRE_FIELD,
    capability_profile,
)
from benchmark_longitudinal_provider import (
    ModelReasoningCapability,
    ReasoningLevelBinding,
)


LIVE_CAPABILITY_SCHEMA_VERSION = 1
LIVE_CAPABILITY_PHASE = "G5C"

G5C_REPOSITORY_ANCHOR = (
    "28138a495f76ab8df9d84fc82abc074a6efc1bf2"
)
G5C_EXPECTED_CAPABILITY_FREEZE_SHA256 = (
    "348d864cc7969cf65fb5be3fedda8a28d436cb0f00bcaf3e40d31f1e98dc7176"
)
G5C_PROBE_PLAN_SHA256 = (
    "7893943e94a896e60599089813f9e3e23c6a37eec8580066d2fa4fd26028d0ac"
)

G5C_EXECUTION_REVIEW_SHA256 = (
    "5633728b0378c16fdc232dce5bc6daf462d32f44650f93576f0a303b5e9447f4"
)
G5C_WRAPPER_MANIFEST_SHA256 = (
    "5731caff6bf9c1a4c3c5efd24d6f87e6af5f94180044d148b89b5570d7c1a019"
)
G5C_CONSOLE_LOG_SHA256 = (
    "f1d728ecbe29f5652839b2108d11c367341f10d4253dac9479dfd632c499fbbb"
)

G5C_PRECHECK_MANIFEST_SHA256 = (
    "e59c194cca419eeaa6076627cf1418793ffe0c0f602cd9a0df272a5bfa8d4cb3"
)
G5C_PRECHECK_TEST_LOG_SHA256 = (
    "397d57298a71af35348b78c175b6b00c740ae8e3527c9a47776a8413b1e572ea"
)
G5C_PRECHECK_DRY_RUN_SHA256 = (
    "d0165dc4e0cfab39c27753eb55da106c6ed6a06fd96472175c5ba4e099c2acce"
)

G5C_BOUND_REQUEST_COUNT = 10
G5C_ACCEPTED_COUNT = 10
G5C_REJECTED_COUNT = 0
G5C_AMBIGUOUS_COUNT = 0
G5C_AUTOMATIC_RETRY_COUNT = 0

G5C_PROMPT_TOKENS = 589
G5C_COMPLETION_TOKENS = 136
G5C_TOTAL_TOKENS = 725
G5C_CACHED_TOKENS = 192

G5C_ZDR_SENT = False
G5C_ZDR_LIVE_STATUS = "not_tested"

G6_PROVIDER_CALLS_AUTHORIZED = False


class LiveCapabilityConfigurationError(ValueError):
    """Frozen live capability evidence is malformed or queried unsafely."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LiveCapabilityConfigurationError(
            "live capability value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveProviderCapability:
    """One model's exact direct-Provider facts established by G5C."""

    benchmark_model_id: str
    provider_model_id: str
    verified_reasoning_efforts: tuple[str, ...]
    default_effort_request_verified: bool
    returned_model_identity_verified: bool = True
    usage_reporting_verified: bool = True
    zdr_live_status: str = G5C_ZDR_LIVE_STATUS

    def __post_init__(self) -> None:
        static = capability_profile(self.benchmark_model_id)

        if self.provider_model_id != static.provider_catalog_model_id:
            raise LiveCapabilityConfigurationError(
                "live provider model id must match the frozen G5B1 catalog id"
            )

        if self.zdr_live_status != G5C_ZDR_LIVE_STATUS:
            raise LiveCapabilityConfigurationError(
                "G5C did not live-test ZDR; the result must stay not_tested"
            )

        if not self.returned_model_identity_verified:
            raise LiveCapabilityConfigurationError(
                "a frozen G5C live capability requires exact returned-model identity"
            )

        if not self.usage_reporting_verified:
            raise LiveCapabilityConfigurationError(
                "a frozen G5C live capability requires prompt/completion usage"
            )

        expected_efforts = static.translation_calibration_efforts

        if expected_efforts:
            if self.verified_reasoning_efforts != expected_efforts:
                raise LiveCapabilityConfigurationError(
                    "live reasoning efforts must exactly equal the frozen G5B1 "
                    "translation calibration pair"
                )
            if self.default_effort_request_verified:
                raise LiveCapabilityConfigurationError(
                    "adjustable G5C models were not probed at default effort"
                )
        else:
            if self.verified_reasoning_efforts:
                raise LiveCapabilityConfigurationError(
                    "default-only G5C models must not invent a reasoning effort"
                )
            if not self.default_effort_request_verified:
                raise LiveCapabilityConfigurationError(
                    "default-only G5C models require a verified default request"
                )

    @property
    def reasoning_control_verified(self) -> bool:
        return bool(self.verified_reasoning_efforts)

    def identity(self) -> dict[str, Any]:
        return {
            "benchmark_model_id": self.benchmark_model_id,
            "provider_model_id": self.provider_model_id,
            "verified_reasoning_efforts": list(self.verified_reasoning_efforts),
            "default_effort_request_verified": self.default_effort_request_verified,
            "returned_model_identity_verified": self.returned_model_identity_verified,
            "usage_reporting_verified": self.usage_reporting_verified,
            "zdr_live_status": self.zdr_live_status,
        }


LIVE_PROVIDER_CAPABILITIES = (
    LiveProviderCapability(
        benchmark_model_id="deepseek/deepseek-v4.1-flash",
        provider_model_id="deepseek/deepseek-v4.1-flash",
        verified_reasoning_efforts=("low", "high"),
        default_effort_request_verified=False,
    ),
    LiveProviderCapability(
        benchmark_model_id="z-ai/glm-5.3-flash",
        provider_model_id="z-ai/glm-5.3-flash",
        verified_reasoning_efforts=("low", "high"),
        default_effort_request_verified=False,
    ),
    LiveProviderCapability(
        benchmark_model_id="qwen/qwen3.8-max-0902",
        provider_model_id="Qwen/Qwen3.8-Max-0902",
        verified_reasoning_efforts=("low", "medium"),
        default_effort_request_verified=False,
    ),
    LiveProviderCapability(
        benchmark_model_id="google/gemini-3.8-flash",
        provider_model_id="google/gemini-3.8-flash",
        verified_reasoning_efforts=("low", "medium"),
        default_effort_request_verified=False,
    ),
    LiveProviderCapability(
        benchmark_model_id="tencent/hy3-paid",
        provider_model_id="tencent/hy3-paid",
        verified_reasoning_efforts=(),
        default_effort_request_verified=True,
    ),
    LiveProviderCapability(
        benchmark_model_id="xiaomi/mimo-v2.5-pro",
        provider_model_id="xiaomi/mimo-v2.5-pro",
        verified_reasoning_efforts=(),
        default_effort_request_verified=True,
    ),
)

if tuple(item.benchmark_model_id for item in LIVE_PROVIDER_CAPABILITIES) != tuple(
    profile.benchmark_model_id for profile in CAPABILITY_PROFILES
):
    raise LiveCapabilityConfigurationError(
        "G5C live capability roster must exactly match the G5B1 roster"
    )

if CAPABILITY_FREEZE_SHA256 != G5C_EXPECTED_CAPABILITY_FREEZE_SHA256:
    raise LiveCapabilityConfigurationError(
        "G5C result is bound to a different G5B1 capability freeze"
    )

if BENCHMARK_ZDR_ENABLED:
    raise LiveCapabilityConfigurationError(
        "G5C result requires the benchmark-wide ZDR policy to remain disabled"
    )

_LIVE_BY_MODEL = MappingProxyType(
    {item.benchmark_model_id: item for item in LIVE_PROVIDER_CAPABILITIES}
)


def live_provider_capability(model_id: str) -> LiveProviderCapability:
    """Resolve one exact benchmark/logical model id; aliases fail closed."""
    if not isinstance(model_id, str):
        raise LiveCapabilityConfigurationError("benchmark model id must be a string")
    try:
        return _LIVE_BY_MODEL[model_id]
    except KeyError as exc:
        raise LiveCapabilityConfigurationError(
            f"unknown benchmark model id: {model_id!r}"
        ) from exc


def verified_reasoning_capability(
    model_id: str,
) -> ModelReasoningCapability | None:
    """Return the exact live-verified Provider reasoning binding, if applicable."""
    live = live_provider_capability(model_id)

    if not live.verified_reasoning_efforts:
        return None

    bindings = tuple(
        ReasoningLevelBinding(
            level=level,
            wire_payload=((REASONING_WIRE_FIELD, level),),
        )
        for level in live.verified_reasoning_efforts
    )

    return ModelReasoningCapability(
        model_id=live.provider_model_id,
        verified=True,
        bindings=bindings,
    )


def g6_translation_efforts(model_id: str) -> tuple[str | None, ...]:
    """Return the exact G6 translation-effort choices authorized by G5C evidence."""
    live = live_provider_capability(model_id)
    if live.verified_reasoning_efforts:
        return tuple(live.verified_reasoning_efforts)
    return (None,)


def live_capability_result_identity() -> dict[str, Any]:
    """Return the deterministic, credential-free G5C result identity."""
    return {
        "schema_version": LIVE_CAPABILITY_SCHEMA_VERSION,
        "phase": LIVE_CAPABILITY_PHASE,
        "repository_anchor": G5C_REPOSITORY_ANCHOR,
        "g5b1_capability_freeze_sha256": G5C_EXPECTED_CAPABILITY_FREEZE_SHA256,
        "probe_plan_sha256": G5C_PROBE_PLAN_SHA256,
        "execution_review_sha256": G5C_EXECUTION_REVIEW_SHA256,
        "wrapper_manifest_sha256": G5C_WRAPPER_MANIFEST_SHA256,
        "console_log_sha256": G5C_CONSOLE_LOG_SHA256,
        "precheck_manifest_sha256": G5C_PRECHECK_MANIFEST_SHA256,
        "precheck_test_log_sha256": G5C_PRECHECK_TEST_LOG_SHA256,
        "precheck_dry_run_sha256": G5C_PRECHECK_DRY_RUN_SHA256,
        "request_count": G5C_BOUND_REQUEST_COUNT,
        "accepted_count": G5C_ACCEPTED_COUNT,
        "rejected_count": G5C_REJECTED_COUNT,
        "ambiguous_count": G5C_AMBIGUOUS_COUNT,
        "automatic_retry_count": G5C_AUTOMATIC_RETRY_COUNT,
        "usage": {
            "prompt_tokens": G5C_PROMPT_TOKENS,
            "completion_tokens": G5C_COMPLETION_TOKENS,
            "total_tokens": G5C_TOTAL_TOKENS,
            "cached_tokens": G5C_CACHED_TOKENS,
        },
        "reasoning_wire_field": REASONING_WIRE_FIELD,
        "zdr_sent": G5C_ZDR_SENT,
        "zdr_live_status": G5C_ZDR_LIVE_STATUS,
        "g6_provider_calls_authorized": G6_PROVIDER_CALLS_AUTHORIZED,
        "models": [item.identity() for item in LIVE_PROVIDER_CAPABILITIES],
    }


G5C_LIVE_RESULT_SHA256 = _sha256_json(live_capability_result_identity())
