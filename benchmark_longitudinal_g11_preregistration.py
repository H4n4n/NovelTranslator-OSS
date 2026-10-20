"""G11 preregistration: GLM-5.3-Flash vs DeepSeek-V4.1-Flash as a practical
replacement for the retired DeepSeek V4 Pro production model.

This module freezes the experimental design BEFORE any G11 paid request exists.
It intentionally performs no filesystem, credential, network, or provider
activity on import: every value here is immutable, local identity data.

G11 is deliberately NOT the G10 "stop at the first ambiguity" design and NOT a
new multi-phase framework.  It is one practical longitudinal benchmark:

* four independent tracks = (GLM | DeepSeek V4.1 Flash) x (Ace | Rogue);
* Chapters 1 through 100 per track through the real production lifecycle;
* production 25-chapter periodic audits, which land at 25/50/75/100;
* the real evolving glossary, continuity, issues, QA, terminology handling,
  retroactive corrections, and residual-source-script micro-repair;
* visible transport retries owned by the benchmark wrapper (the underlying
  OpenAI-compatible client keeps ``max_retries=0``);
* one explicit translation regeneration for a length/empty translation;
* hard per-model credit-equivalent ceilings (GLM 40, DeepSeek V4.1 Flash 60).

Both models were already live-verified for ``reasoning_effort`` ``low``/``high``
by the frozen G5C capability results, so this module binds those exact levels
and never invents a provider-specific reasoning parameter shape.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from benchmark_longitudinal_config import (
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
    LIFECYCLE_OPERATION_TRANSLATION,
    PURE_RUN_LIFECYCLE_OPERATIONS,
)
from benchmark_longitudinal_g7d_accounting_policy import (
    DEEPSEEK_PEAK_UTC_WEEKDAYS,
    DEEPSEEK_PEAK_UTC_WINDOWS,
)


G11_SCHEMA_VERSION = 1
G11_REVISION = "g11-v4pro-replacement-practical-v1"
G11_BENCHMARK_ID = "g11-v4pro-replacement-v1"
G11_REPOSITORY_ANCHOR = "40c9c12dbc1191b5d742db460f564636d11af888"

GLM_MODEL_ID = "z-ai/glm-5.3-flash"
DEEPSEEK_MODEL_ID = "deepseek/deepseek-v4.1-flash"
MODEL_ORDER = (GLM_MODEL_ID, DEEPSEEK_MODEL_ID)

# Provider-facing model identities, frozen exactly as the live G5C probes
# verified them.  A response whose model differs fails closed upstream.
PROVIDER_MODEL_IDS = {
    GLM_MODEL_ID: "z-ai/glm-5.3-flash",
    DEEPSEEK_MODEL_ID: "deepseek/deepseek-v4.1-flash",
}

MODEL_SLUGS = {
    GLM_MODEL_ID: "glm",
    DEEPSEEK_MODEL_ID: "deepseek",
}

# --------------------------------------------------------------------------
# Reasoning policy (identical for both models: the comparison must not differ
# in reasoning effort, only in the model itself).
# --------------------------------------------------------------------------
TRANSLATION_EFFORT = "high"
SEMANTIC_EFFORT = "high"
STRUCTURED_EFFORT = "low"

SEMANTIC_OPERATIONS = (
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR,
)
STRUCTURED_OPERATIONS = (
    LIFECYCLE_OPERATION_STATE_EXTRACTION,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
)

if set(SEMANTIC_OPERATIONS + STRUCTURED_OPERATIONS) != (
    set(PURE_RUN_LIFECYCLE_OPERATIONS) - {LIFECYCLE_OPERATION_TRANSLATION}
):
    raise RuntimeError("G11 auxiliary operation partition drifted")

_EFFORT_BY_OPERATION = {
    LIFECYCLE_OPERATION_TRANSLATION: TRANSLATION_EFFORT,
    LIFECYCLE_OPERATION_GLOSSARY_REVIEW: SEMANTIC_EFFORT,
    LIFECYCLE_OPERATION_PERIODIC_AUDIT: SEMANTIC_EFFORT,
    LIFECYCLE_OPERATION_TERMINOLOGY_REPAIR: SEMANTIC_EFFORT,
    LIFECYCLE_OPERATION_STATE_EXTRACTION: STRUCTURED_EFFORT,
    LIFECYCLE_OPERATION_STATE_JSON_REPAIR: STRUCTURED_EFFORT,
}

if set(_EFFORT_BY_OPERATION) != set(PURE_RUN_LIFECYCLE_OPERATIONS):
    raise RuntimeError("G11 effort policy must cover every lifecycle operation")

EFFORT_BY_OPERATION = MappingProxyType(dict(_EFFORT_BY_OPERATION))

MODEL_EFFORT_POLICY = MappingProxyType(
    {
        model_id: dict(EFFORT_BY_OPERATION)
        for model_id in MODEL_ORDER
    }
)

# --------------------------------------------------------------------------
# Transport token ceilings.
# --------------------------------------------------------------------------
TRANSLATION_COMPLETION_CEILING = 24000
AUXILIARY_COMPLETION_CEILING = 8000

# --------------------------------------------------------------------------
# Retry policy.  The wrapper owns the retries; the SDK keeps max_retries=0.
# --------------------------------------------------------------------------
MAX_TRANSPORT_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 5)
RETRYABLE_HTTP_STATUSES = (408, 429)
RETRYABLE_HTTP_STATUS_RANGES = ((500, 599),)
NON_RETRYABLE_HTTP_STATUSES = (400, 401, 403, 404, 405, 409, 413, 422)
RETRY_CONNECTION_ERRORS = True
RETRY_TIMEOUT_ERRORS = True
RETRY_RATE_LIMIT_ERRORS = True
MAX_RETRIES = MAX_TRANSPORT_ATTEMPTS - 1

# --------------------------------------------------------------------------
# Repair / content policy.
# --------------------------------------------------------------------------
TRANSLATION_REGENERATION_LIMIT = 1
TRANSLATION_REGENERATION_TRIGGERS = ("finish_reason_length", "empty_or_blank_content")

RESIDUAL_CJK_MICRO_REPAIR = MappingProxyType(
    {
        "enabled": True,
        "max_total_cjk_chars": 12,
        "max_fragments": 6,
        "max_affected_lines": 3,
        "max_attempts_per_pending_version": 1,
    }
)

REPAIR_POLICY = MappingProxyType(
    {
        "state_json_repair_enabled": True,
        "terminology_repair_enabled": True,
        "glossary_review_correction_enabled": True,
        "periodic_audit_correction_enabled": True,
        "retroactive_chapter_fixes_enabled": True,
        "normal_production_qa_enabled": True,
        "residual_source_script_micro_repair_enabled": True,
        "translation_regeneration_limit": TRANSLATION_REGENERATION_LIMIT,
        "new_heuristic_quality_gates_added": False,
    }
)

# --------------------------------------------------------------------------
# Budgets.  Hard credit-equivalent ceilings, not spending targets.
# --------------------------------------------------------------------------
MODEL_BUDGETS = MappingProxyType(
    {
        GLM_MODEL_ID: "40.00",
        DEEPSEEK_MODEL_ID: "60.00",
    }
)

# --------------------------------------------------------------------------
# Accounting snapshot.  G11 inherits published Command Code rates: the GLM flat
# rate from the frozen G10E 2026-09-29 capture and the DeepSeek V4.1 Flash
# peak/off-peak rate from the frozen G7D capture.  No new provider call is made
# to "refresh" pricing; the inherited snapshot is named as inherited.
# --------------------------------------------------------------------------
G11_PRICING_VERSION = "g11-inherited-command-code-ratecard-v1"
G11_PRICING_UNIT = "usd_credit_equivalent_per_1m_tokens"
G11_PRICING_INHERITANCE = MappingProxyType(
    {
        GLM_MODEL_ID: "benchmark_longitudinal_g10e_freeze.MODEL_RATECARD[glm]",
        DEEPSEEK_MODEL_ID: (
            "benchmark_longitudinal_g7d_accounting_policy.MODEL_RATECARD[deepseek]"
        ),
    }
)

G11_MODEL_RATECARD = MappingProxyType(
    {
        GLM_MODEL_ID: {
            "pricing_mode": "flat",
            "periods": {
                "flat": {
                    "input": "0.15",
                    "output": "0.50",
                    "cache_read": "0.03",
                },
            },
        },
        DEEPSEEK_MODEL_ID: {
            "pricing_mode": "utc_peak_offpeak",
            "periods": {
                "off_peak": {
                    "input": "0.15",
                    "output": "0.60",
                    "cache_read": "0.003",
                },
                "peak": {
                    "input": "0.30",
                    "output": "1.20",
                    "cache_read": "0.006",
                },
            },
            "peak_weekdays_utc": list(DEEPSEEK_PEAK_UTC_WEEKDAYS),
            "peak_windows_utc": [list(item) for item in DEEPSEEK_PEAK_UTC_WINDOWS],
        },
    }
)

# --------------------------------------------------------------------------
# Novels and tracks.
# --------------------------------------------------------------------------
NOVELS = MappingProxyType(
    {
        "ace_evolution": {
            "title": "Ace Evolution",
            "source_language": "zh",
            "target_language": "en",
            "profile": "infinite_flow",
            "final_chapter": 1214,
            "source_type": "epub",
            "audit_interval": 25,
            "chapter_numbering": "spine_sequential",
        },
        "rogue_first_prince": {
            "title": "I Became the Rogue First Prince",
            "source_language": "ko",
            "target_language": "en",
            "profile": "korean_fantasy",
            "final_chapter": 350,
            "source_type": "txt",
            "audit_interval": 25,
            "chapter_numbering": "spine_sequential",
        },
    }
)

FIRST_CHAPTER = 1
LAST_CHAPTER = 100
CHAPTER_COUNT_PER_TRACK = LAST_CHAPTER - FIRST_CHAPTER + 1
AUDIT_INTERVAL = 25
AUDIT_CHECKPOINTS = (25, 50, 75, 100)
STATE_EVIDENCE_CHECKPOINTS = (25, 50, 75, 100)
QUALITY_CHECKPOINTS = (1, 5, 10, 20, 25, 26, 40, 50, 51, 65, 75, 76, 90, 100)

TRACK_COUNT = len(MODEL_ORDER) * len(NOVELS)
MAXIMUM_CANONICAL_CHAPTERS = TRACK_COUNT * CHAPTER_COUNT_PER_TRACK


def _track_id(model_id: str, novel_id: str) -> str:
    return f"g11-run{_track_index(model_id, novel_id):02d}-{MODEL_SLUGS[model_id]}-{novel_id}"


def _track_order() -> tuple[tuple[str, str], ...]:
    """Pair both models against each novel before moving to the next novel."""
    return tuple(
        (model_id, novel_id)
        for novel_id in NOVELS
        for model_id in MODEL_ORDER
    )


def _track_index(model_id: str, novel_id: str) -> int:
    return _track_order().index((model_id, novel_id)) + 1


def build_tracks() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "run_index": index,
            "run_id": _track_id(model_id, novel_id),
            "model_id": model_id,
            "provider_model_id": PROVIDER_MODEL_IDS[model_id],
            "novel_id": novel_id,
            "profile_id": NOVELS[novel_id]["profile"],
            "prereg_track_id": f"{model_id}::{novel_id}",
            "first_chapter": FIRST_CHAPTER,
            "last_chapter": LAST_CHAPTER,
            "audit_interval": AUDIT_INTERVAL,
            "audit_checkpoints": list(AUDIT_CHECKPOINTS),
            "independent_fresh_s0": True,
            "translation_effort": EFFORT_BY_OPERATION[LIFECYCLE_OPERATION_TRANSLATION],
            "structured_effort": STRUCTURED_EFFORT,
            "semantic_effort": SEMANTIC_EFFORT,
        }
        for index, (model_id, novel_id) in enumerate(_track_order(), start=1)
    )


TRACKS = build_tracks()
TRACK_BY_ID = {track["run_id"]: track for track in TRACKS}


# --------------------------------------------------------------------------
# Metrics that the final comparison consumes.
# --------------------------------------------------------------------------
RELIABILITY_COST_METRICS = (
    "chapters_committed",
    "first_attempt_success_rate",
    "eventual_success_rate",
    "transport_attempts",
    "retries",
    "translation_regenerations",
    "state_json_repairs",
    "terminology_repairs",
    "residual_script_repairs",
    "audit_provider_calls",
    "audits_completed",
    "retroactive_fixes",
    "terminal_failures",
    "total_prompt_tokens",
    "total_completion_tokens",
    "cache_tokens_reported",
    "total_credit_equivalent_including_retries",
    "elapsed_provider_latency_seconds",
    "last_completed_chapter",
    "last_audited_chapter",
)

QUALITY_COMPARISON_AXES = (
    "quality",
    "reliability",
    "repair_burden",
    "engine_state_behavior",
    "cost",
    "latency",
)

# Published repository objects this experiment is built on.  ``sha256`` is the
# working-tree byte identity of each frozen source file, so a drifted engine or
# helper fails closed instead of silently changing the experiment.
SOURCE_BINDINGS = {
    "production_engine": {
        "path": "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py",
        "sha256": "5d5720b7de0c4d0c1054af1ce2a239d0a45cb06650dcc709ba256cfcdd7db126",
    },
    "production_lifecycle": {
        "path": "production_lifecycle.py",
        "sha256": "2c9c76ba2d07d0266eab9cd4eb2d188d091b42f242d5738d6b015a97e9db0b0e",
    },
    "provider_adapter": {
        "path": "benchmark_longitudinal_provider.py",
        "sha256": "59489eb732cdc4cfe383ed3e791dec6a3933f717b9d690ae7fa613335180a02c",
    },
    "live_capability_results": {
        "path": "benchmark_longitudinal_capability_results.py",
        "sha256": "a53aaba0be3fc2430c7b60a9e1a7070bb30c4131c836a60d2934924dd73c4fe6",
    },
    "accounting_policy": {
        "path": "benchmark_longitudinal_g7d_accounting_policy.py",
        "sha256": "c2e4fcb584173923a565d8d1cd0633b2251e2d6c941fc13a84f3d96e0eb09212",
    },
    "safety_primitives": {
        "path": "benchmark_longitudinal_g10_safety.py",
        "sha256": "285f779d50a1e2c979e7c38357d0fe63076440e50a2ae017846af25ae839f0bc",
    },
    "s0_root_isolation_reference": {
        "path": "benchmark_longitudinal_g10_s0.py",
        "sha256": "b688a3afb0b1a6674530ee1b528d868b128f92213e6b9202ffee4ec306279315",
    },
    "paid_request_journal": {
        "path": "production_paid_request_journal.py",
        "sha256": "a8d892b0e0ed90ca991592e982550ec5ab7b6e6695f97390f289e556eb0b4093",
    },
    "experiment_engine_loader": {
        "path": "full_pipeline_luna_runtime.py",
        "sha256": "b4e03e97fa6ecc3c3048c7f299f89359d075d94b2a02e8ebf6b4d158545c3494",
    },
    "residual_cjk_micro_repair": {
        "path": "residual_cjk_micro_repair.py",
        "sha256": "d46dc84c9184f451892070652496be564ebe2941b84638510822d1970cd03818",
    },
    "chapter_library": {
        "path": "chapter_library.py",
        "sha256": "9613cec47217157c5724a63f427b2f96fb81344ed55c264d64145f10182e4359",
    },
    "novel_creation": {
        "path": "create_novel.py",
        "sha256": "aea27a4f96745bb688da9a57bff8e1600d4d313c3856d50bf0dcdca7a38ec82d",
    },
    "run_lock": {
        "path": "production_run_lock.py",
        "sha256": "199a8b413efae2fdd723f223322a52813656439683b25b6720b67276b986ed98",
    },
}


class G11PreregistrationError(RuntimeError):
    """The frozen G11 preregistration is internally inconsistent."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate() -> None:
    if tuple(MODEL_ORDER) != (GLM_MODEL_ID, DEEPSEEK_MODEL_ID):
        raise G11PreregistrationError("G11 model order drifted")
    if set(MODEL_BUDGETS) != set(MODEL_ORDER):
        raise G11PreregistrationError("G11 must freeze one ceiling per model")
    if set(NOVELS) != {"ace_evolution", "rogue_first_prince"}:
        raise G11PreregistrationError("G11 novel roster drifted")

    for model_id in MODEL_ORDER:
        policy = MODEL_EFFORT_POLICY[model_id]
        if policy[LIFECYCLE_OPERATION_TRANSLATION] != TRANSLATION_EFFORT:
            raise G11PreregistrationError("translation effort must be high")
        for operation in SEMANTIC_OPERATIONS:
            if policy[operation] != SEMANTIC_EFFORT:
                raise G11PreregistrationError(
                    f"semantic operation {operation} must use high effort"
                )
        for operation in STRUCTURED_OPERATIONS:
            if policy[operation] != STRUCTURED_EFFORT:
                raise G11PreregistrationError(
                    f"structured operation {operation} must use low effort"
                )
        if PROVIDER_MODEL_IDS[model_id] != model_id:
            raise G11PreregistrationError(
                "G11 provider model identity must match the benchmark model id"
            )

    if MAX_TRANSPORT_ATTEMPTS != 3:
        raise G11PreregistrationError("G11 must allow at most three transport attempts")
    if MAX_RETRIES != 2:
        raise G11PreregistrationError("G11 first attempt plus two retries expected")
    if RETRY_BACKOFF_SECONDS != (2, 5):
        raise G11PreregistrationError("G11 backoff schedule drifted")
    for status in RETRYABLE_HTTP_STATUSES:
        if status in NON_RETRYABLE_HTTP_STATUSES:
            raise G11PreregistrationError("retry status classification is ambiguous")
    for low, high in RETRYABLE_HTTP_STATUS_RANGES:
        if low > high:
            raise G11PreregistrationError("retryable status range is inverted")

    if MODEL_BUDGETS[GLM_MODEL_ID] != "40.00":
        raise G11PreregistrationError("GLM ceiling must be 40.00 CE")
    if MODEL_BUDGETS[DEEPSEEK_MODEL_ID] != "60.00":
        raise G11PreregistrationError("DeepSeek ceiling must be 60.00 CE")

    if TRANSLATION_COMPLETION_CEILING != 24000:
        raise G11PreregistrationError("translation completion ceiling drifted")
    if AUXILIARY_COMPLETION_CEILING != 8000:
        raise G11PreregistrationError("auxiliary completion ceiling drifted")

    if len(TRACKS) != 4:
        raise G11PreregistrationError("G11 must contain exactly four tracks")
    if len({track["run_id"] for track in TRACKS}) != 4:
        raise G11PreregistrationError("G11 run ids must be unique")
    if {(track["model_id"], track["novel_id"]) for track in TRACKS} != {
        (model_id, novel_id) for model_id in MODEL_ORDER for novel_id in NOVELS
    }:
        raise G11PreregistrationError("G11 track matrix drifted")
    for track in TRACKS:
        if track["first_chapter"] != 1 or track["last_chapter"] != LAST_CHAPTER:
            raise G11PreregistrationError("G11 tracks must run Chapters 1-100")
        if tuple(track["audit_checkpoints"]) != AUDIT_CHECKPOINTS:
            raise G11PreregistrationError("G11 audit checkpoints drifted")
        if track["audit_interval"] != NOVELS[track["novel_id"]]["audit_interval"]:
            raise G11PreregistrationError("G11 audit interval drifted from novel config")

    if MAXIMUM_CANONICAL_CHAPTERS != 400:
        raise G11PreregistrationError("G11 canonical chapter total must be 400")
    if AUDIT_CHECKPOINTS != (25, 50, 75, 100):
        raise G11PreregistrationError("G11 audit checkpoints must be 25/50/75/100")
    if QUALITY_CHECKPOINTS != (
        1, 5, 10, 20, 25, 26, 40, 50, 51, 65, 75, 76, 90, 100,
    ):
        raise G11PreregistrationError("G11 quality checkpoints drifted")
    if any(
        chapter < FIRST_CHAPTER or chapter > LAST_CHAPTER
        for chapter in QUALITY_CHECKPOINTS
    ):
        raise G11PreregistrationError("quality checkpoint out of the 1..100 scope")
    if any(
        chapter < FIRST_CHAPTER or chapter > LAST_CHAPTER
        for chapter in STATE_EVIDENCE_CHECKPOINTS
    ):
        raise G11PreregistrationError("state checkpoint out of the 1..100 scope")

    if not REPAIR_POLICY["residual_source_script_micro_repair_enabled"]:
        raise G11PreregistrationError("G11 must enable residual-script micro-repair")
    if REPAIR_POLICY["translation_regeneration_limit"] != 1:
        raise G11PreregistrationError("G11 must permit exactly one regeneration")
    if REPAIR_POLICY["new_heuristic_quality_gates_added"] is not False:
        raise G11PreregistrationError("G11 must not add new heuristic quality gates")
    if not RESIDUAL_CJK_MICRO_REPAIR["enabled"]:
        raise G11PreregistrationError("residual repair config must be enabled")

    for model_id, model in G11_MODEL_RATECARD.items():
        for period, rates in model["periods"].items():
            if set(rates) != {"input", "output", "cache_read"}:
                raise G11PreregistrationError("G11 rate fields drifted")
    if G11_MODEL_RATECARD[DEEPSEEK_MODEL_ID]["pricing_mode"] != "utc_peak_offpeak":
        raise G11PreregistrationError("DeepSeek must keep peak/off-peak pricing")


_validate()


def g11_preregistration_identity() -> dict[str, Any]:
    return {
        "schema_version": G11_SCHEMA_VERSION,
        "revision": G11_REVISION,
        "benchmark_id": G11_BENCHMARK_ID,
        "repository_anchor": G11_REPOSITORY_ANCHOR,
        "source_bindings": SOURCE_BINDINGS,
        "models": list(MODEL_ORDER),
        "provider_model_ids": dict(PROVIDER_MODEL_IDS),
        "effort_policy": {
            "translation": TRANSLATION_EFFORT,
            "semantic": SEMANTIC_EFFORT,
            "structured": STRUCTURED_EFFORT,
            "semantic_operations": list(SEMANTIC_OPERATIONS),
            "structured_operations": list(STRUCTURED_OPERATIONS),
            "by_operation": dict(EFFORT_BY_OPERATION),
        },
        "token_ceilings": {
            "translation_completion": TRANSLATION_COMPLETION_CEILING,
            "auxiliary_completion": AUXILIARY_COMPLETION_CEILING,
        },
        "retry_policy": {
            "provider_client_max_retries": 0,
            "wrapper_owns_retries": True,
            "max_transport_attempts": MAX_TRANSPORT_ATTEMPTS,
            "max_retries": MAX_RETRIES,
            "backoff_seconds": list(RETRY_BACKOFF_SECONDS),
            "retryable_http_statuses": list(RETRYABLE_HTTP_STATUSES),
            "retryable_http_status_ranges": [
                list(item) for item in RETRYABLE_HTTP_STATUS_RANGES
            ],
            "non_retryable_http_statuses": list(NON_RETRYABLE_HTTP_STATUSES),
            "retry_connection_errors": RETRY_CONNECTION_ERRORS,
            "retry_timeout_errors": RETRY_TIMEOUT_ERRORS,
            "retry_rate_limit_errors": RETRY_RATE_LIMIT_ERRORS,
            "every_physical_attempt_recorded": True,
        },
        "repair_policy": dict(REPAIR_POLICY),
        "residual_cjk_micro_repair": dict(RESIDUAL_CJK_MICRO_REPAIR),
        "translation_regeneration": {
            "limit": TRANSLATION_REGENERATION_LIMIT,
            "triggers": list(TRANSLATION_REGENERATION_TRIGGERS),
            "regeneration_is_a_paid_call_and_is_counted": True,
        },
        "budgets": dict(MODEL_BUDGETS),
        "pricing": {
            "version": G11_PRICING_VERSION,
            "unit": G11_PRICING_UNIT,
            "ratecard": {
                model_id: dict(spec)
                for model_id, spec in G11_MODEL_RATECARD.items()
            },
            "inheritance": dict(G11_PRICING_INHERITANCE),
            "fresh_provider_call_used": False,
        },
        "novels": {novel_id: dict(spec) for novel_id, spec in NOVELS.items()},
        "scope": {
            "first_chapter": FIRST_CHAPTER,
            "last_chapter": LAST_CHAPTER,
            "chapters_per_track": CHAPTER_COUNT_PER_TRACK,
            "track_count": TRACK_COUNT,
            "maximum_canonical_chapters": MAXIMUM_CANONICAL_CHAPTERS,
            "audit_interval": AUDIT_INTERVAL,
            "audit_checkpoints": list(AUDIT_CHECKPOINTS),
            "state_evidence_checkpoints": list(STATE_EVIDENCE_CHECKPOINTS),
            "quality_checkpoints": list(QUALITY_CHECKPOINTS),
        },
        "tracks": [dict(track) for track in TRACKS],
        "reliability_cost_metrics": list(RELIABILITY_COST_METRICS),
        "quality_comparison_axes": list(QUALITY_COMPARISON_AXES),
        "provider_calls": 0,
        "provider_calls_authorized": False,
        "credential_access": False,
    }


G11_PREREGISTRATION_SHA256 = _sha256_json(g11_preregistration_identity())


def assert_zero_provider_design() -> None:
    identity = g11_preregistration_identity()
    if identity["provider_calls"] != 0:
        raise G11PreregistrationError("preregistration reports nonzero provider calls")
    if identity["provider_calls_authorized"] is not False:
        raise G11PreregistrationError("preregistration must not authorize provider calls")
    if identity["credential_access"] is not False:
        raise G11PreregistrationError("preregistration must not authorize credential access")


def file_sha256(path: Path | str) -> str:
    """Return the raw working-tree SHA-256 byte identity of one bound file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_source_bindings(code_root: Path | str | None = None) -> dict[str, Any]:
    """Fail closed when a frozen G11 source binding no longer matches."""
    root = (
        Path(__file__).resolve().parent
        if code_root is None
        else Path(code_root).resolve()
    )
    drifted: list[str] = []
    for name, spec in SOURCE_BINDINGS.items():
        target = root / spec["path"]
        if not target.is_file():
            drifted.append(f"{name}: missing {spec['path']}")
            continue
        observed = file_sha256(target)
        if observed != spec["sha256"]:
            drifted.append(
                f"{name}: {spec['path']} expected {spec['sha256']} observed {observed}"
            )
    if drifted:
        raise G11PreregistrationError(
            "G11 frozen source bindings drifted: " + "; ".join(sorted(drifted))
        )
    return {"verified": True, "binding_count": len(SOURCE_BINDINGS)}


__all__ = [
    "AUDIT_CHECKPOINTS",
    "AUDIT_INTERVAL",
    "AUXILIARY_COMPLETION_CEILING",
    "CHAPTER_COUNT_PER_TRACK",
    "DEEPSEEK_MODEL_ID",
    "EFFORT_BY_OPERATION",
    "FIRST_CHAPTER",
    "G11_BENCHMARK_ID",
    "G11_MODEL_RATECARD",
    "G11_PREREGISTRATION_SHA256",
    "G11_PRICING_VERSION",
    "G11_REPOSITORY_ANCHOR",
    "G11_REVISION",
    "GLM_MODEL_ID",
    "LAST_CHAPTER",
    "MAXIMUM_CANONICAL_CHAPTERS",
    "MAX_RETRIES",
    "MAX_TRANSPORT_ATTEMPTS",
    "MODEL_BUDGETS",
    "MODEL_EFFORT_POLICY",
    "MODEL_ORDER",
    "MODEL_SLUGS",
    "NON_RETRYABLE_HTTP_STATUSES",
    "NOVELS",
    "PROVIDER_MODEL_IDS",
    "QUALITY_CHECKPOINTS",
    "RELIABILITY_COST_METRICS",
    "REPAIR_POLICY",
    "RESIDUAL_CJK_MICRO_REPAIR",
    "RETRYABLE_HTTP_STATUS_RANGES",
    "RETRYABLE_HTTP_STATUSES",
    "RETRY_BACKOFF_SECONDS",
    "SEMANTIC_OPERATIONS",
    "SOURCE_BINDINGS",
    "STATE_EVIDENCE_CHECKPOINTS",
    "STRUCTURED_OPERATIONS",
    "TRACKS",
    "TRACK_BY_ID",
    "TRACK_COUNT",
    "TRANSLATION_COMPLETION_CEILING",
    "assert_source_bindings",
    "assert_zero_provider_design",
    "build_tracks",
    "file_sha256",
    "g11_preregistration_identity",
]
