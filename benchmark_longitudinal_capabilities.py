"""Frozen G5B1 capability evidence for the GOAT longitudinal benchmark.

This module records capability facts established without provider inference by
G5A/G5B0 against Command Code 1.54.2. It deliberately distinguishes the frozen
benchmark model id from the provider catalog id, Command Code CLI-selectable
reasoning efforts from direct Provider API support, static Command Code ZDR
classification from benchmark routing policy, and G5C capability probes from
the later G6 literary calibration.

Nothing in this module authorizes a paid request. Direct Provider acceptance of
canonical model spelling, returned model identity, usage accounting, and the
reasoning_effort field remains unverified until journal-protected G5C probes
succeed.

G5B0 also exposed a static-extractor limitation: its broad-window
model_definitions reasoning_efforts field could capture a neighbouring model's
array. This module therefore binds only the exact effort Map plus actual helper
semantics, not those contaminated broad-window values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS


CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_EVIDENCE_PHASE = "G5B0"
COMMAND_CODE_VERSION = "1.54.2"
COMMAND_CODE_CLI_SHA256 = (
    "551a17d143fd6687e4dc9085bf5f2e601a01f9f95faa6528a1c1011ff8a54828"
)

REASONING_WIRE_FIELD = "reasoning_effort"
REASONING_WIRE_STATIC_STATUS = "cli_discovered_direct_provider_unverified"
DIRECT_PROVIDER_STATUS_UNVERIFIED = "unverified"

BENCHMARK_ZDR_ENABLED = False
BENCHMARK_ZDR_POLICY = "disabled_for_cross_model_fairness"

G5C_PROBE_REVISION = "goat-g5c-capability-probe-v1"
G6_CALIBRATION_REVISION = "goat-g6-literary-calibration-v2"
G6_DIFFICULT_CHAPTERS_PER_MODEL = 4

_ALLOWED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class CapabilityConfigurationError(ValueError):
    """Frozen capability evidence is malformed or queried unsafely."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityConfigurationError(
            "capability value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


def _validate_efforts(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple):
        raise CapabilityConfigurationError(f"{label} must be a tuple")
    if len(values) != len(set(values)):
        raise CapabilityConfigurationError(f"{label} contains duplicates")
    unknown = sorted(set(values) - _ALLOWED_EFFORTS)
    if unknown:
        raise CapabilityConfigurationError(
            f"{label} contains unsupported effort labels: {unknown}"
        )


@dataclass(frozen=True)
class ModelCapabilityProfile:
    """One candidate's frozen static capability evidence and probe design."""

    benchmark_model_id: str
    provider_catalog_model_id: str
    cli_supported_efforts: tuple[str, ...]
    translation_calibration_efforts: tuple[str, ...]
    cli_known_model_supports_zdr: bool
    direct_provider_model_identity_status: str = DIRECT_PROVIDER_STATUS_UNVERIFIED
    direct_provider_reasoning_status: str = DIRECT_PROVIDER_STATUS_UNVERIFIED
    direct_provider_zdr_status: str = DIRECT_PROVIDER_STATUS_UNVERIFIED

    def __post_init__(self) -> None:
        _require_text(self.benchmark_model_id, "benchmark model id")
        _require_text(self.provider_catalog_model_id, "provider catalog model id")
        _validate_efforts(self.cli_supported_efforts, "CLI supported efforts")
        _validate_efforts(
            self.translation_calibration_efforts,
            "translation calibration efforts",
        )
        if not isinstance(self.cli_known_model_supports_zdr, bool):
            raise CapabilityConfigurationError(
                "CLI known-model ZDR support must be boolean"
            )
        for label, value in (
            ("direct provider model identity status", self.direct_provider_model_identity_status),
            ("direct provider reasoning status", self.direct_provider_reasoning_status),
            ("direct provider ZDR status", self.direct_provider_zdr_status),
        ):
            if value != DIRECT_PROVIDER_STATUS_UNVERIFIED:
                raise CapabilityConfigurationError(
                    f"{label} must remain unverified in the G5B1 static freeze"
                )

        calibration = self.translation_calibration_efforts
        if calibration:
            if len(calibration) != 2:
                raise CapabilityConfigurationError(
                    "an adjustable model must freeze exactly two translation "
                    "calibration efforts"
                )
            if not set(calibration).issubset(self.cli_supported_efforts):
                raise CapabilityConfigurationError(
                    "translation calibration efforts must be a subset of the "
                    "exact CLI-supported efforts"
                )
        elif self.cli_supported_efforts:
            raise CapabilityConfigurationError(
                "a model with selectable CLI efforts must freeze a two-level "
                "translation calibration pair"
            )

    @property
    def uses_default_effort_for_translation_calibration(self) -> bool:
        return not self.translation_calibration_efforts

    @property
    def has_selectable_cli_reasoning_effort(self) -> bool:
        return bool(self.cli_supported_efforts)

    def identity(self) -> dict[str, Any]:
        return {
            "benchmark_model_id": self.benchmark_model_id,
            "provider_catalog_model_id": self.provider_catalog_model_id,
            "cli_supported_efforts": list(self.cli_supported_efforts),
            "translation_calibration_efforts": list(
                self.translation_calibration_efforts
            ),
            "uses_default_effort_for_translation_calibration": (
                self.uses_default_effort_for_translation_calibration
            ),
            "cli_known_model_supports_zdr": self.cli_known_model_supports_zdr,
            "direct_provider_model_identity_status": (
                self.direct_provider_model_identity_status
            ),
            "direct_provider_reasoning_status": self.direct_provider_reasoning_status,
            "direct_provider_zdr_status": self.direct_provider_zdr_status,
        }


CAPABILITY_PROFILES = (
    ModelCapabilityProfile(
        benchmark_model_id="deepseek/deepseek-v4.1-flash",
        provider_catalog_model_id="deepseek/deepseek-v4.1-flash",
        cli_supported_efforts=("low", "high", "max"),
        translation_calibration_efforts=("low", "high"),
        cli_known_model_supports_zdr=True,
    ),
    ModelCapabilityProfile(
        benchmark_model_id="z-ai/glm-5.3-flash",
        provider_catalog_model_id="z-ai/glm-5.3-flash",
        cli_supported_efforts=("low", "high", "max"),
        translation_calibration_efforts=("low", "high"),
        cli_known_model_supports_zdr=True,
    ),
    ModelCapabilityProfile(
        benchmark_model_id="qwen/qwen3.8-max-0902",
        provider_catalog_model_id="Qwen/Qwen3.8-Max-0902",
        cli_supported_efforts=("low", "medium", "xhigh"),
        translation_calibration_efforts=("low", "medium"),
        cli_known_model_supports_zdr=False,
    ),
    ModelCapabilityProfile(
        benchmark_model_id="google/gemini-3.8-flash",
        provider_catalog_model_id="google/gemini-3.8-flash",
        cli_supported_efforts=("low", "medium", "high"),
        translation_calibration_efforts=("low", "medium"),
        cli_known_model_supports_zdr=True,
    ),
    ModelCapabilityProfile(
        benchmark_model_id="tencent/hy3-paid",
        provider_catalog_model_id="tencent/hy3-paid",
        cli_supported_efforts=(),
        translation_calibration_efforts=(),
        cli_known_model_supports_zdr=True,
    ),
    ModelCapabilityProfile(
        benchmark_model_id="xiaomi/mimo-v2.5-pro",
        provider_catalog_model_id="xiaomi/mimo-v2.5-pro",
        cli_supported_efforts=(),
        translation_calibration_efforts=(),
        cli_known_model_supports_zdr=True,
    ),
)

if tuple(profile.benchmark_model_id for profile in CAPABILITY_PROFILES) != tuple(
    BENCHMARK_MODEL_IDS
):
    raise CapabilityConfigurationError(
        "G5B1 capability profile roster must exactly match BENCHMARK_MODEL_IDS"
    )

_CAPABILITY_BY_MODEL = MappingProxyType(
    {profile.benchmark_model_id: profile for profile in CAPABILITY_PROFILES}
)


def capability_profile(model_id: str) -> ModelCapabilityProfile:
    """Resolve one exact frozen benchmark model id; never case-fold or alias it."""
    if not isinstance(model_id, str):
        raise CapabilityConfigurationError("benchmark model id must be a string")
    try:
        return _CAPABILITY_BY_MODEL[model_id]
    except KeyError as exc:
        raise CapabilityConfigurationError(
            f"unknown benchmark model id: {model_id!r}"
        ) from exc


def provider_model_id(model_id: str) -> str:
    """Return the frozen catalog spelling planned for direct Provider probing."""
    return capability_profile(model_id).provider_catalog_model_id


@dataclass(frozen=True)
class CapabilityProbeCase:
    """One tiny G5C direct-Provider capability probe plan item."""

    benchmark_model_id: str
    provider_model_id: str
    effort: str | None
    zdr_enabled: bool = False

    def identity(self) -> dict[str, Any]:
        return {
            "benchmark_model_id": self.benchmark_model_id,
            "provider_model_id": self.provider_model_id,
            "effort": self.effort,
            "zdr_enabled": self.zdr_enabled,
        }


def build_g5c_probe_plan() -> tuple[CapabilityProbeCase, ...]:
    """Build the deterministic ten-case capability probe matrix."""
    cases: list[CapabilityProbeCase] = []
    for profile in CAPABILITY_PROFILES:
        efforts: tuple[str | None, ...]
        if profile.translation_calibration_efforts:
            efforts = tuple(profile.translation_calibration_efforts)
        else:
            efforts = (None,)
        for effort in efforts:
            cases.append(
                CapabilityProbeCase(
                    benchmark_model_id=profile.benchmark_model_id,
                    provider_model_id=profile.provider_catalog_model_id,
                    effort=effort,
                    zdr_enabled=BENCHMARK_ZDR_ENABLED,
                )
            )
    return tuple(cases)


G5C_CAPABILITY_PROBE_CASES = build_g5c_probe_plan()
G5C_CAPABILITY_PROBE_CALL_COUNT = len(G5C_CAPABILITY_PROBE_CASES)

G6_PRIMARY_CALIBRATION_CALL_COUNT = sum(
    (
        len(profile.translation_calibration_efforts)
        if profile.translation_calibration_efforts
        else 1
    )
    * G6_DIFFICULT_CHAPTERS_PER_MODEL
    for profile in CAPABILITY_PROFILES
)


def capability_freeze_identity() -> dict[str, Any]:
    """Return the deterministic G5B1 static capability/design identity."""
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "evidence_phase": CAPABILITY_EVIDENCE_PHASE,
        "command_code_version": COMMAND_CODE_VERSION,
        "command_code_cli_sha256": COMMAND_CODE_CLI_SHA256,
        "reasoning_wire_field": REASONING_WIRE_FIELD,
        "reasoning_wire_static_status": REASONING_WIRE_STATIC_STATUS,
        "benchmark_zdr_enabled": BENCHMARK_ZDR_ENABLED,
        "benchmark_zdr_policy": BENCHMARK_ZDR_POLICY,
        "g5c_probe_revision": G5C_PROBE_REVISION,
        "g5c_probe_cases": [case.identity() for case in G5C_CAPABILITY_PROBE_CASES],
        "g6_calibration_revision": G6_CALIBRATION_REVISION,
        "g6_difficult_chapters_per_model": G6_DIFFICULT_CHAPTERS_PER_MODEL,
        "g6_primary_calibration_call_count": G6_PRIMARY_CALIBRATION_CALL_COUNT,
        "profiles": [profile.identity() for profile in CAPABILITY_PROFILES],
    }


CAPABILITY_FREEZE_SHA256 = _sha256_json(capability_freeze_identity())
