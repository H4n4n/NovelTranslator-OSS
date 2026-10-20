"""Zero-provider longitudinal benchmark planning.

This module creates and validates a deterministic PLAN for the G-series
longitudinal benchmark.  It never executes a translation, never constructs a
provider client, and never reads the real (copyrighted) source novels.

Each run plan models exactly one candidate model against one novel:

* an independent run identity and an independent S0 state requirement;
* stage-1 chronological growth from Chapter 1 through Chapter 30;
* an audit checkpoint at Chapter 25;
* finalist continuation from Chapter 31 through Chapter 60;
* a second audit checkpoint at Chapter 50;
* a causal sequential requirement (Chapter N+1 depends on Chapter N);
* a same-model pure-run lifecycle policy;
* no shared mutable state between models.

Because the real source files are intentionally absent from the engineering
worktree, planning works from synthetic :class:`SourceIdentityDescriptor`
values.  Importing this module reads no credentials, performs no network
activity, and writes no files; planning makes no provider call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from benchmark_longitudinal_config import (
    DEFAULT_BENCHMARK_CONFIGURATION,
    BenchmarkConfiguration,
    BenchmarkModel,
    BenchmarkNovelSource,
    validate_benchmark_configuration,
)


PLAN_SCHEMA_VERSION = 1
PLAN_REVISION = "goat-longitudinal-cjk-plan-v1"
S0_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class BenchmarkPlanError(RuntimeError):
    """The longitudinal benchmark plan is invalid or unsafe to continue."""


class BenchmarkPlanValidationError(BenchmarkPlanError):
    """A rebuilt plan does not match the plan under validation."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkPlanError(
            "benchmark plan value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkPlanError(f"{label} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class SourceIdentityDescriptor:
    """A synthetic, source-file-free identity for one novel.

    ``source_sha256`` may be empty while the authoritative source is absent
    from the engineering worktree; later integration must prove the descriptor
    against the real source before any provider work.
    """

    novel_id: str
    source_language: str
    target_language: str
    chapter_count: int
    source_sha256: str = ""
    label: str = "synthetic"

    def __post_init__(self) -> None:
        _require_non_empty_text(self.novel_id, "source descriptor novel id")
        _require_non_empty_text(self.source_language, "source descriptor source language")
        _require_non_empty_text(self.target_language, "source descriptor target language")
        if (
            not isinstance(self.chapter_count, int)
            or isinstance(self.chapter_count, bool)
            or self.chapter_count < 1
        ):
            raise BenchmarkPlanError(
                "source descriptor chapter_count must be a positive integer"
            )
        if self.source_sha256:
            if not isinstance(self.source_sha256, str) or not _SHA256_RE.fullmatch(
                self.source_sha256
            ):
                raise BenchmarkPlanError(
                    "source descriptor source_sha256 must be lowercase SHA-256 or empty"
                )
        _require_non_empty_text(self.label, "source descriptor label")

    def identity(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "chapter_count": self.chapter_count,
            "source_sha256": self.source_sha256,
            "label": self.label,
        }


@dataclass(frozen=True)
class S0StateIdentity:
    """The independent starting state requirement for one run."""

    benchmark_id: str
    model_id: str
    novel_id: str
    source_language: str
    target_language: str
    source_sha256: str
    chapter_count: int
    glossary_entries: int = 0
    continuity: str = ""
    issue_count: int = 0
    last_completed_chapter: int = 0
    last_audited_chapter: int = 0

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": S0_SCHEMA_VERSION,
            "benchmark_id": self.benchmark_id,
            "model_id": self.model_id,
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "source_sha256": self.source_sha256,
            "chapter_count": self.chapter_count,
            "glossary_entries": self.glossary_entries,
            "continuity": self.continuity,
            "issue_count": self.issue_count,
            "last_completed_chapter": self.last_completed_chapter,
            "last_audited_chapter": self.last_audited_chapter,
        }

    @property
    def identity_sha256(self) -> str:
        return _sha256_json(self.identity())


@dataclass(frozen=True)
class BenchmarkRunIdentity:
    """A run's independent identity: one model, one novel, one S0 state."""

    benchmark_id: str
    model_id: str
    novel_id: str
    source_sha256: str
    s0_identity_sha256: str

    def identity(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "model_id": self.model_id,
            "novel_id": self.novel_id,
            "source_sha256": self.source_sha256,
            "s0_identity_sha256": self.s0_identity_sha256,
        }

    @property
    def run_sha256(self) -> str:
        return _sha256_json(self.identity())


def audit_checkpoints_between(
    interval: int,
    first_chapter: int,
    last_chapter: int,
) -> tuple[int, ...]:
    """Return every audit checkpoint (a multiple of ``interval``) in a range."""
    for label, value in (("interval", interval), ("first", first_chapter), ("last", last_chapter)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise BenchmarkPlanError(f"audit checkpoint {label} must be a positive integer")
    if last_chapter < first_chapter:
        raise BenchmarkPlanError("audit checkpoint range is inverted")
    start = ((first_chapter + interval - 1) // interval) * interval
    return tuple(range(start, last_chapter + 1, interval))


@dataclass(frozen=True)
class BenchmarkRunPlan:
    """One deterministic model/novel run plan."""

    run_identity: BenchmarkRunIdentity
    s0_state: S0StateIdentity
    source_identity: SourceIdentityDescriptor
    stage1_chapters: tuple[int, ...]
    finalist_continuation_chapters: tuple[int, ...]
    stage1_audit_checkpoints: tuple[int, ...]
    finalist_audit_checkpoints: tuple[int, ...]
    requires_s0_state: bool = True
    causal_sequential: bool = True
    same_model_pure_run_lifecycle: bool = True
    independent_model_state: bool = True

    @property
    def model_id(self) -> str:
        return self.run_identity.model_id

    @property
    def novel_id(self) -> str:
        return self.run_identity.novel_id

    @property
    def chapters(self) -> tuple[int, ...]:
        return self.stage1_chapters + self.finalist_continuation_chapters

    @property
    def audit_checkpoints(self) -> tuple[int, ...]:
        return self.stage1_audit_checkpoints + self.finalist_audit_checkpoints

    def identity(self) -> dict[str, Any]:
        return {
            "run_identity": self.run_identity.identity(),
            "s0_state": self.s0_state.identity(),
            "source_identity": self.source_identity.identity(),
            "stage1_chapters": list(self.stage1_chapters),
            "finalist_continuation_chapters": list(self.finalist_continuation_chapters),
            "stage1_audit_checkpoints": list(self.stage1_audit_checkpoints),
            "finalist_audit_checkpoints": list(self.finalist_audit_checkpoints),
            "requires_s0_state": self.requires_s0_state,
            "causal_sequential": self.causal_sequential,
            "same_model_pure_run_lifecycle": self.same_model_pure_run_lifecycle,
            "independent_model_state": self.independent_model_state,
        }

    @property
    def run_plan_sha256(self) -> str:
        return _sha256_json(self.identity())


@dataclass(frozen=True)
class BenchmarkPlan:
    """The deterministic plan for every model/novel run."""

    schema_version: int
    revision: str
    benchmark_id: str
    configuration_sha256: str
    runs: tuple[BenchmarkRunPlan, ...]

    @property
    def run_count(self) -> int:
        return len(self.runs)

    def runs_for_model(self, model_id: str) -> tuple[BenchmarkRunPlan, ...]:
        return tuple(run for run in self.runs if run.model_id == model_id)

    def runs_for_novel(self, novel_id: str) -> tuple[BenchmarkRunPlan, ...]:
        return tuple(run for run in self.runs if run.novel_id == novel_id)

    def run_for(self, model_id: str, novel_id: str) -> BenchmarkRunPlan:
        for run in self.runs:
            if run.model_id == model_id and run.novel_id == novel_id:
                return run
        raise BenchmarkPlanError(
            f"benchmark plan has no run for model {model_id!r} novel {novel_id!r}"
        )

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "benchmark_id": self.benchmark_id,
            "configuration_sha256": self.configuration_sha256,
            "runs": [run.identity() for run in self.runs],
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256_json(self.identity())


def build_s0_state_identity(
    configuration: BenchmarkConfiguration,
    model: BenchmarkModel,
    novel: BenchmarkNovelSource,
    source: SourceIdentityDescriptor,
) -> S0StateIdentity:
    """Build one run's empty, independent S0 state requirement."""
    return S0StateIdentity(
        benchmark_id=configuration.benchmark_id,
        model_id=model.model_id,
        novel_id=novel.novel_id,
        source_language=novel.source_language,
        target_language=novel.target_language,
        source_sha256=source.source_sha256,
        chapter_count=source.chapter_count,
    )


def build_run_identity(
    configuration: BenchmarkConfiguration,
    model: BenchmarkModel,
    novel: BenchmarkNovelSource,
    source: SourceIdentityDescriptor,
    s0_state: S0StateIdentity,
) -> BenchmarkRunIdentity:
    return BenchmarkRunIdentity(
        benchmark_id=configuration.benchmark_id,
        model_id=model.model_id,
        novel_id=novel.novel_id,
        source_sha256=source.source_sha256,
        s0_identity_sha256=s0_state.identity_sha256,
    )


def _assert_source_covers_run(
    configuration: BenchmarkConfiguration,
    novel: BenchmarkNovelSource,
    source: SourceIdentityDescriptor,
) -> None:
    if source.novel_id != novel.novel_id:
        raise BenchmarkPlanError(
            f"source descriptor novel id {source.novel_id!r} does not match "
            f"configured novel {novel.novel_id!r}"
        )
    if source.source_language != novel.source_language:
        raise BenchmarkPlanError(
            f"source descriptor language {source.source_language!r} does not match "
            f"configured novel {novel.novel_id!r} language {novel.source_language!r}"
        )
    if source.target_language != novel.target_language:
        raise BenchmarkPlanError(
            f"source descriptor target language {source.target_language!r} does not match "
            f"configured novel {novel.novel_id!r} target language {novel.target_language!r}"
        )
    if source.chapter_count < configuration.finalist_target_chapter:
        raise BenchmarkPlanError(
            f"source descriptor for {novel.novel_id!r} covers only "
            f"{source.chapter_count} chapters, below the finalist target "
            f"{configuration.finalist_target_chapter}"
        )


def build_run_plan(
    configuration: BenchmarkConfiguration,
    model: BenchmarkModel,
    novel: BenchmarkNovelSource,
    source: SourceIdentityDescriptor,
) -> BenchmarkRunPlan:
    _assert_source_covers_run(configuration, novel, source)
    s0_state = build_s0_state_identity(configuration, model, novel, source)
    run_identity = build_run_identity(
        configuration, model, novel, source, s0_state
    )
    stage1_chapters = tuple(range(1, configuration.stage1_target_chapter + 1))
    finalist_continuation_chapters = tuple(
        range(
            configuration.stage1_target_chapter + 1,
            configuration.finalist_target_chapter + 1,
        )
    )
    stage1_audit_checkpoints = audit_checkpoints_between(
        configuration.audit_interval,
        1,
        configuration.stage1_target_chapter,
    )
    finalist_audit_checkpoints = audit_checkpoints_between(
        configuration.audit_interval,
        configuration.stage1_target_chapter + 1,
        configuration.finalist_target_chapter,
    )
    if not stage1_audit_checkpoints:
        raise BenchmarkPlanError(
            "stage-1 chapter range contains no audit checkpoint at the configured interval"
        )
    return BenchmarkRunPlan(
        run_identity=run_identity,
        s0_state=s0_state,
        source_identity=source,
        stage1_chapters=stage1_chapters,
        finalist_continuation_chapters=finalist_continuation_chapters,
        stage1_audit_checkpoints=stage1_audit_checkpoints,
        finalist_audit_checkpoints=finalist_audit_checkpoints,
    )


def _index_source_identities(
    configuration: BenchmarkConfiguration,
    source_identities: Sequence[SourceIdentityDescriptor],
) -> dict[str, SourceIdentityDescriptor]:
    if isinstance(source_identities, (str, bytes)) or not isinstance(
        source_identities, Sequence
    ):
        raise BenchmarkPlanError("source identities must be a sequence of descriptors")
    indexed: dict[str, SourceIdentityDescriptor] = {}
    for descriptor in source_identities:
        if not isinstance(descriptor, SourceIdentityDescriptor):
            raise BenchmarkPlanError(
                "each source identity must be a SourceIdentityDescriptor"
            )
        if descriptor.novel_id in indexed:
            raise BenchmarkPlanError(
                f"duplicate source identity for novel {descriptor.novel_id!r}"
            )
        indexed[descriptor.novel_id] = descriptor
    missing = [novel_id for novel_id in configuration.novel_ids if novel_id not in indexed]
    unexpected = [novel_id for novel_id in indexed if novel_id not in configuration.novel_ids]
    if missing:
        raise BenchmarkPlanError(
            f"source identities are missing configured novels: {sorted(missing)}"
        )
    if unexpected:
        raise BenchmarkPlanError(
            f"source identities include unknown novels: {sorted(unexpected)}"
        )
    return indexed


def _assert_independent_runs(plan: BenchmarkPlan) -> None:
    run_ids: set[str] = set()
    s0_ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for run in plan.runs:
        if run.run_identity.run_sha256 in run_ids:
            raise BenchmarkPlanError(
                "benchmark runs must have independent run identities"
            )
        run_ids.add(run.run_identity.run_sha256)
        if run.s0_state.identity_sha256 in s0_ids:
            raise BenchmarkPlanError(
                "benchmark runs must have independent S0 state identities"
            )
        s0_ids.add(run.s0_state.identity_sha256)
        pair = (run.model_id, run.novel_id)
        if pair in pairs:
            raise BenchmarkPlanError(
                f"duplicate benchmark run for model {pair[0]!r} novel {pair[1]!r}"
            )
        pairs.add(pair)
        if not (
            run.requires_s0_state
            and run.causal_sequential
            and run.same_model_pure_run_lifecycle
            and run.independent_model_state
        ):
            raise BenchmarkPlanError(
                f"run {pair} must require S0 state, be causally sequential, run one "
                "model lifecycle, and keep independent state"
            )


def build_benchmark_plan(
    configuration: BenchmarkConfiguration,
    source_identities: Sequence[SourceIdentityDescriptor],
) -> BenchmarkPlan:
    """Build the deterministic plan for every configured model/novel run."""
    validate_benchmark_configuration(configuration)
    indexed = _index_source_identities(configuration, source_identities)
    runs = []
    for model in configuration.models:
        for novel in configuration.novels:
            runs.append(
                build_run_plan(
                    configuration,
                    model,
                    novel,
                    indexed[novel.novel_id],
                )
            )
    plan = BenchmarkPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        revision=PLAN_REVISION,
        benchmark_id=configuration.benchmark_id,
        configuration_sha256=configuration.identity_sha256,
        runs=tuple(runs),
    )
    _assert_independent_runs(plan)
    return plan


def validate_benchmark_plan(
    plan: BenchmarkPlan,
    configuration: BenchmarkConfiguration,
    source_identities: Sequence[SourceIdentityDescriptor],
) -> BenchmarkPlan:
    """Rebuild and prove a plan deterministically; fail closed on any drift."""
    if not isinstance(plan, BenchmarkPlan):
        raise BenchmarkPlanValidationError("benchmark plan must be a BenchmarkPlan")
    rebuilt = build_benchmark_plan(configuration, source_identities)
    if rebuilt.plan_sha256 != plan.plan_sha256:
        raise BenchmarkPlanValidationError(
            "benchmark plan does not match its deterministic rebuild"
        )
    return plan


def synthetic_source_identities(
    configuration: BenchmarkConfiguration = DEFAULT_BENCHMARK_CONFIGURATION,
    *,
    chapter_count: int | None = None,
    label: str = "synthetic",
) -> tuple[SourceIdentityDescriptor, ...]:
    """Build conservative descriptors that need no real source files."""
    count = chapter_count if chapter_count is not None else configuration.finalist_target_chapter
    return tuple(
        SourceIdentityDescriptor(
            novel_id=novel.novel_id,
            source_language=novel.source_language,
            target_language=novel.target_language,
            chapter_count=count,
            source_sha256="",
            label=label,
        )
        for novel in configuration.novels
    )


def assert_causal_sequential_step(last_completed_chapter: int, next_chapter: int) -> int:
    """Require Chapter N+1 to depend causally on the completed Chapter N."""
    for label, value in (
        ("last completed chapter", last_completed_chapter),
        ("next chapter", next_chapter),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BenchmarkPlanError(f"{label} must be a non-negative integer")
    if next_chapter != last_completed_chapter + 1:
        raise BenchmarkPlanError(
            "benchmark runs are causally sequential: next chapter "
            f"{next_chapter} must equal last completed chapter + 1 "
            f"({last_completed_chapter + 1})"
        )
    return next_chapter


def build_default_plan(
    configuration: BenchmarkConfiguration = DEFAULT_BENCHMARK_CONFIGURATION,
) -> BenchmarkPlan:
    """Build the default plan from synthetic source identities."""
    return build_benchmark_plan(
        configuration,
        synthetic_source_identities(configuration),
    )
