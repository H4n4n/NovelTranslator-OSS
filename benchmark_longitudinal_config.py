"""Immutable, import-safe configuration for the longitudinal model benchmark.

This module is the single source of truth for the G-series longitudinal
benchmark (frozen experiment id ``goat-longitudinal-cjk-v1``) that compares six
Command Code Provider models inside the real NovelTranslator lifecycle.  It is
deliberately separate from ``translation_model_config`` (production) and
``benchmark_model_config`` (the earlier DeepSeek/Luna comparison), so adding a
longitudinal candidate can never widen production model selection, pending
recovery, controlled trials, or operator preflight.

Design rules enforced here:

* the exact six candidate model IDs are declared once;
* the two planned novels are declared once, with their target language;
* the chronological stage/finalist targets and the audit interval are declared
  once;
* the exact Command Code Provider base URL and its OpenAI-compatible
  chat-completions path are declared once; no other base URL is accepted for
  benchmark paid transport;
* the original G3 global reasoning candidates (``low`` and ``medium``) remain
  UNVERIFIED placeholders; the model-specific G5B1 capability freeze owns the
  current calibration matrix and direct Provider support still requires live
  verification;
* an unsupported or unverified reasoning configuration must fail closed and
  must never silently fall back to another level;
* a pure-model run binds exactly ONE model for every provider-assisted
  operation, but each operation's reasoning effort is frozen independently;
  omitting an operation is fatal rather than silently defaulted.

No credentials are represented here.  Importing this module reads no
environment variables, performs no network activity, and writes no files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


BENCHMARK_ID = "goat-longitudinal-cjk-v1"
BENCHMARK_SCHEMA_VERSION = 1

PROVIDER_NAME = "command_code_provider"
PROVIDER_BASE_URL = "https://api.commandcode.ai/provider/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"

STAGE1_TARGET_CHAPTER = 30
FINALIST_TARGET_CHAPTER = 60
AUDIT_INTERVAL = 25

REASONING_CAPABILITY_UNVERIFIED = "unverified"
REASONING_CAPABILITY_VERIFIED = "verified"
REASONING_CAPABILITIES = frozenset(
    {
        REASONING_CAPABILITY_UNVERIFIED,
        REASONING_CAPABILITY_VERIFIED,
    }
)

BENCHMARK_MODEL_IDS = (
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.8-max-0902",
    "google/gemini-3.8-flash",
    "tencent/hy3-paid",
    "xiaomi/mimo-v2.5-pro",
)

BENCHMARK_NOVEL_IDS = (
    "ace_evolution",
    "rogue_first_prince",
)

# Every provider-assisted lifecycle operation that a pure-model run performs.
# A run binds one model for all of them, but each operation's reasoning level is
# frozen independently (for example, primary translation calibrating low versus
# medium while a periodic audit later uses a stronger verified level).
LIFECYCLE_OPERATION_TRANSLATION = "translation"
LIFECYCLE_OPERATION_STATE_EXTRACTION = "state_extraction"
LIFECYCLE_OPERATION_STATE_JSON_REPAIR = "state_json_repair"
LIFECYCLE_OPERATION_GLOSSARY_REVIEW = "glossary_review"
LIFECYCLE_OPERATION_PERIODIC_AUDIT = "periodic_audit"
LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR = "terminology_repair"

PURE_RUN_LIFECYCLE_OPERATIONS = (
    LIFECYCLE_OPERATION_TRANSLATION,
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
)


class BenchmarkConfigurationError(ValueError):
    """The longitudinal benchmark configuration is malformed or incomplete."""


class UnsupportedReasoningConfiguration(BenchmarkConfigurationError):
    """A reasoning configuration cannot be used and must never fall back."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkConfigurationError(
            "benchmark configuration value is not canonically JSON-serializable"
        ) from exc


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _require_non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class BenchmarkModel:
    """One benchmark candidate model, identified by its exact provider ID."""

    model_id: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.model_id, "benchmark model id")


@dataclass(frozen=True)
class BenchmarkNovelSource:
    """One benchmark source novel and its translation direction."""

    novel_id: str
    source_language: str
    target_language: str = "en"

    def __post_init__(self) -> None:
        _require_non_empty_text(self.novel_id, "benchmark novel id")
        _require_non_empty_text(self.source_language, "benchmark source language")
        _require_non_empty_text(self.target_language, "benchmark target language")

    def identity(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
        }


@dataclass(frozen=True)
class ReasoningCandidate:
    """A declared reasoning calibration candidate.

    ``capability`` is ``unverified`` until the provider's actual control shape
    has been discovered and validated.  An unverified candidate may be named,
    but it must never be bound to a request or silently replaced by another
    level.
    """

    level: str
    capability: str = REASONING_CAPABILITY_UNVERIFIED

    def __post_init__(self) -> None:
        _require_non_empty_text(self.level, "reasoning candidate level")
        if self.capability not in REASONING_CAPABILITIES:
            allowed = ", ".join(sorted(REASONING_CAPABILITIES))
            raise BenchmarkConfigurationError(
                f"reasoning candidate capability must be one of: {allowed}"
            )

    def identity(self) -> dict[str, Any]:
        return {"level": self.level, "capability": self.capability}


def _require_unique(items: Sequence[Any], label: str, key) -> None:
    if not isinstance(items, tuple):
        raise BenchmarkConfigurationError(f"benchmark {label} collection must be a tuple")
    seen = set()
    for item in items:
        value = key(item)
        if value in seen:
            raise BenchmarkConfigurationError(f"duplicate benchmark {label}: {value!r}")
        seen.add(value)


@dataclass(frozen=True)
class BenchmarkConfiguration:
    """The complete immutable longitudinal benchmark policy."""

    benchmark_id: str
    models: tuple[BenchmarkModel, ...]
    novels: tuple[BenchmarkNovelSource, ...]
    stage1_target_chapter: int
    finalist_target_chapter: int
    audit_interval: int
    provider_base_url: str
    chat_completions_path: str
    reasoning_candidates: tuple[ReasoningCandidate, ...]
    schema_version: int = BENCHMARK_SCHEMA_VERSION
    same_model_all_provider_operations: bool = True
    lifecycle_operations: tuple[str, ...] = PURE_RUN_LIFECYCLE_OPERATIONS

    def __post_init__(self) -> None:
        validate_benchmark_configuration(self)

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(model.model_id for model in self.models)

    @property
    def novel_ids(self) -> tuple[str, ...]:
        return tuple(novel.novel_id for novel in self.novels)

    @property
    def chat_completions_url(self) -> str:
        return self.provider_base_url.rstrip("/") + self.chat_completions_path

    def model(self, model_id: str) -> BenchmarkModel:
        for model in self.models:
            if model.model_id == model_id:
                return model
        raise BenchmarkConfigurationError(
            f"unknown benchmark model id: {model_id!r}"
        )

    def novel(self, novel_id: str) -> BenchmarkNovelSource:
        for novel in self.novels:
            if novel.novel_id == novel_id:
                return novel
        raise BenchmarkConfigurationError(
            f"unknown benchmark novel id: {novel_id!r}"
        )

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "benchmark_id": self.benchmark_id,
            "models": list(self.model_ids),
            "novels": [novel.identity() for novel in self.novels],
            "stage1_target_chapter": self.stage1_target_chapter,
            "finalist_target_chapter": self.finalist_target_chapter,
            "audit_interval": self.audit_interval,
            "provider_base_url": self.provider_base_url,
            "chat_completions_path": self.chat_completions_path,
            "reasoning_candidates": [
                candidate.identity() for candidate in self.reasoning_candidates
            ],
            "same_model_all_provider_operations": self.same_model_all_provider_operations,
            "lifecycle_operations": list(self.lifecycle_operations),
        }

    @property
    def identity_sha256(self) -> str:
        return _sha256_json(self.identity())


def validate_benchmark_configuration(
    configuration: BenchmarkConfiguration,
) -> BenchmarkConfiguration:
    """Fail closed unless the complete benchmark policy is well formed."""
    if not isinstance(configuration, BenchmarkConfiguration):
        raise BenchmarkConfigurationError(
            "benchmark configuration must be a BenchmarkConfiguration"
        )
    if not isinstance(configuration.schema_version, int) or isinstance(
        configuration.schema_version, bool
    ):
        raise BenchmarkConfigurationError("benchmark schema version must be an integer")
    _require_non_empty_text(configuration.benchmark_id, "benchmark id")

    _require_unique(configuration.models, "model id", lambda item: item.model_id)
    _require_unique(configuration.novels, "novel id", lambda item: item.novel_id)
    if not configuration.models:
        raise BenchmarkConfigurationError("benchmark must declare at least one model")
    if not configuration.novels:
        raise BenchmarkConfigurationError("benchmark must declare at least one novel")

    for label, value in (
        ("stage1 target chapter", configuration.stage1_target_chapter),
        ("finalist target chapter", configuration.finalist_target_chapter),
        ("audit interval", configuration.audit_interval),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise BenchmarkConfigurationError(f"benchmark {label} must be a positive integer")
    if configuration.stage1_target_chapter > configuration.finalist_target_chapter:
        raise BenchmarkConfigurationError(
            "benchmark stage1 target must not exceed the finalist target"
        )
    if configuration.audit_interval > configuration.finalist_target_chapter:
        raise BenchmarkConfigurationError(
            "benchmark audit interval must not exceed the finalist target"
        )

    if configuration.provider_base_url != PROVIDER_BASE_URL:
        raise BenchmarkConfigurationError(
            "benchmark provider base URL must be exactly the Command Code Provider "
            f"benchmark URL {PROVIDER_BASE_URL!r}; refusing an arbitrary HTTPS base URL"
        )
    path = configuration.chat_completions_path
    if not isinstance(path, str) or not path.startswith("/") or "://" in path:
        raise BenchmarkConfigurationError(
            "benchmark chat-completions path must be an absolute URL path"
        )

    _require_unique(
        configuration.reasoning_candidates,
        "reasoning candidate level",
        lambda item: item.level,
    )
    if not configuration.reasoning_candidates:
        raise BenchmarkConfigurationError(
            "benchmark must declare at least one reasoning candidate"
        )

    if configuration.same_model_all_provider_operations is not True:
        raise BenchmarkConfigurationError(
            "benchmark requires one model to own every provider-assisted lifecycle "
            "operation; same_model_all_provider_operations must be True"
        )
    _require_unique(
        configuration.lifecycle_operations,
        "lifecycle operation",
        lambda item: item,
    )
    if not configuration.lifecycle_operations:
        raise BenchmarkConfigurationError(
            "benchmark must declare its provider-assisted lifecycle operations"
        )
    for operation in configuration.lifecycle_operations:
        _require_non_empty_text(operation, "lifecycle operation")
    declared = set(configuration.lifecycle_operations)
    unknown = sorted(declared - set(PURE_RUN_LIFECYCLE_OPERATIONS))
    if unknown:
        raise BenchmarkConfigurationError(
            f"benchmark lifecycle operations include unknown entries: {unknown}"
        )
    missing = sorted(set(PURE_RUN_LIFECYCLE_OPERATIONS) - declared)
    if missing:
        raise BenchmarkConfigurationError(
            f"benchmark lifecycle operations are missing required entries: {missing}"
        )
    return configuration


def build_benchmark_roster(model_ids: Sequence[str]) -> tuple[BenchmarkModel, ...]:
    """Build a validated candidate roster, rejecting duplicate model IDs."""
    if isinstance(model_ids, (str, bytes)) or not isinstance(model_ids, Sequence):
        raise BenchmarkConfigurationError(
            "benchmark roster must be a sequence of model IDs"
        )
    if not model_ids:
        raise BenchmarkConfigurationError("benchmark roster must be non-empty")
    roster = []
    seen = set()
    for raw in model_ids:
        model_id = _require_non_empty_text(raw, "benchmark model id")
        if model_id in seen:
            raise BenchmarkConfigurationError(
                f"duplicate benchmark model id: {model_id!r}"
            )
        seen.add(model_id)
        roster.append(BenchmarkModel(model_id=model_id))
    return tuple(roster)


def resolve_reasoning_candidate(
    configuration: BenchmarkConfiguration,
    level: str,
) -> ReasoningCandidate:
    """Resolve one declared calibration candidate by its exact level.

    An undeclared level raises instead of falling back to any other candidate.
    """
    if not isinstance(level, str) or not level.strip():
        raise UnsupportedReasoningConfiguration(
            "reasoning level must be a non-empty string; the benchmark must never "
            "silently fall back to another level"
        )
    normalized = level.strip()
    for candidate in configuration.reasoning_candidates:
        if candidate.level == normalized:
            return candidate
    declared = ", ".join(candidate.level for candidate in configuration.reasoning_candidates)
    raise UnsupportedReasoningConfiguration(
        f"reasoning level {level!r} is not a declared calibration candidate "
        f"({declared}); the benchmark must never silently fall back to another level"
    )


def assert_reasoning_candidate_verified(
    candidate: ReasoningCandidate,
) -> ReasoningCandidate:
    """Fail closed unless the candidate's capability has been verified.

    Unverified capability is intentionally fatal: the benchmark must neither
    guess a provider-specific reasoning control nor degrade to a different
    level.
    """
    if not isinstance(candidate, ReasoningCandidate):
        raise UnsupportedReasoningConfiguration(
            "reasoning candidate must be a ReasoningCandidate"
        )
    if candidate.capability != REASONING_CAPABILITY_VERIFIED:
        raise UnsupportedReasoningConfiguration(
            f"reasoning candidate {candidate.level!r} capability is "
            f"{candidate.capability!r}; an unverified reasoning control must not be "
            "bound and must never silently fall back to another level"
        )
    return candidate


@dataclass(frozen=True)
class LifecycleOperationReasoning:
    """One explicitly frozen reasoning level for one lifecycle operation."""

    operation: str
    level: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.operation, "lifecycle operation")
        _require_non_empty_text(self.level, "lifecycle operation reasoning level")

    def identity(self) -> dict[str, Any]:
        return {"operation": self.operation, "level": self.level}


@dataclass(frozen=True)
class PureRunLifecyclePolicy:
    """One run's single-model, per-operation reasoning lifecycle binding.

    The scientific rule is that exactly one model owns every provider-assisted
    operation.  Reasoning effort may differ per operation, but each operation
    must be frozen explicitly: a missing operation fails closed instead of
    silently inheriting another operation's setting.
    """

    model_id: str
    operations: tuple[LifecycleOperationReasoning, ...]

    def __post_init__(self) -> None:
        _require_non_empty_text(self.model_id, "pure run policy model id")
        _require_unique(
            self.operations,
            "lifecycle operation",
            lambda item: item.operation,
        )
        provided = {item.operation for item in self.operations}
        expected = set(PURE_RUN_LIFECYCLE_OPERATIONS)
        if provided != expected:
            raise BenchmarkConfigurationError(
                "pure run lifecycle policy must explicitly freeze every "
                "provider-assisted operation with no silent fallback; "
                f"missing={sorted(expected - provided)} "
                f"unknown={sorted(provided - expected)}"
            )

    def model_for(self, operation_kind: str) -> str:
        """Return the one model used for a bound provider-assisted operation."""
        self.reasoning_for(operation_kind)
        return self.model_id

    def reasoning_for(self, operation_kind: str) -> str:
        """Return the frozen reasoning level for exactly this operation."""
        for item in self.operations:
            if item.operation == operation_kind:
                return item.level
        raise BenchmarkConfigurationError(
            f"unknown provider-assisted lifecycle operation: {operation_kind!r}"
        )

    def identity(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "operations": [item.identity() for item in self.operations],
        }


def build_pure_run_lifecycle_policy(
    configuration: BenchmarkConfiguration,
    model_id: str,
    reasoning_by_operation: Mapping[str, str],
) -> PureRunLifecyclePolicy:
    """Bind one model and explicitly frozen per-operation reasoning to a run.

    Every provider-assisted operation must be named exactly once.  Any
    undeclared reasoning level, missing operation, unknown operation, or
    unverified capability fails closed; there is no silent fallback.
    """
    model = configuration.model(model_id)
    if not isinstance(reasoning_by_operation, Mapping):
        raise BenchmarkConfigurationError(
            "pure run reasoning must be a mapping of operation to reasoning level"
        )
    provided = set(reasoning_by_operation)
    expected = set(PURE_RUN_LIFECYCLE_OPERATIONS)
    unknown = sorted(provided - expected)
    if unknown:
        raise BenchmarkConfigurationError(
            f"unknown provider-assisted lifecycle operations: {unknown}"
        )
    missing = sorted(expected - provided)
    if missing:
        raise BenchmarkConfigurationError(
            "pure run lifecycle reasoning must explicitly configure every "
            f"provider-assisted operation with no silent fallback; missing={missing}"
        )
    operations = []
    for operation in PURE_RUN_LIFECYCLE_OPERATIONS:
        candidate = assert_reasoning_candidate_verified(
            resolve_reasoning_candidate(configuration, reasoning_by_operation[operation])
        )
        operations.append(
            LifecycleOperationReasoning(operation=operation, level=candidate.level)
        )
    return PureRunLifecyclePolicy(model_id=model.model_id, operations=tuple(operations))


# Historical G3 placeholders only. These remain deliberately unverified and
# are not the model-specific G6 calibration matrix. G5B1 freezes the current
# per-model design in benchmark_longitudinal_capabilities.py; later execution
# must validate both that profile and a live-verified Provider capability.
CALIBRATION_REASONING_CANDIDATES = (
    ReasoningCandidate(level="low", capability=REASONING_CAPABILITY_UNVERIFIED),
    ReasoningCandidate(level="medium", capability=REASONING_CAPABILITY_UNVERIFIED),
)


DEFAULT_BENCHMARK_CONFIGURATION = BenchmarkConfiguration(
    benchmark_id=BENCHMARK_ID,
    models=build_benchmark_roster(BENCHMARK_MODEL_IDS),
    novels=(
        BenchmarkNovelSource(
            novel_id="ace_evolution",
            source_language="zh",
            target_language="en",
        ),
        BenchmarkNovelSource(
            novel_id="rogue_first_prince",
            source_language="ko",
            target_language="en",
        ),
    ),
    stage1_target_chapter=STAGE1_TARGET_CHAPTER,
    finalist_target_chapter=FINALIST_TARGET_CHAPTER,
    audit_interval=AUDIT_INTERVAL,
    provider_base_url=PROVIDER_BASE_URL,
    chat_completions_path=CHAT_COMPLETIONS_PATH,
    reasoning_candidates=CALIBRATION_REASONING_CANDIDATES,
)
