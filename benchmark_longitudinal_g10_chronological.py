"""Zero-provider G10B1 four-track chronological controller contract.

G10B1 freezes HOW four independent model x novel tracks advance through the real
NovelTranslator lifecycle. It does not install Provider transport, read
credentials, create paid requests, or authorize paid execution.

Every target chapter is still executed later through the real bounded production
entry point:

    run_unattended_production(target_chapter=<exact next chapter>)

The production lifecycle itself owns the Ch25/Ch50 audit trigger. G10B1 never
inserts synthetic audit checkpoints and never advances beyond an audit boundary
unless durable progress proves the real audit committed.

Audit request counts are state-dependent because the real audit chunks the
then-current glossary and may split, JSON-repair, or semantically correct a
chunk. Therefore G10B1 deliberately does NOT invent a static audit send ceiling.
Every future Provider request, including each audit subrequest, must pass G10A1
request-level CE reservation immediately before transport. G10E remains
responsible for freezing the overall/arm budget and paid authorization.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from benchmark_longitudinal_g10_preregistration import (
    CHRONOLOGICAL_TRACKS,
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    MODEL_POLICY,
    NOVELS,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import (
    G10A1_SAFETY_SHA256,
)


G10B1_SCHEMA_VERSION = 1
G10B1_REVISION = "g10-four-track-real-engine-controller-v1"
G10B1_FIRST_CHAPTER = 1
G10B1_LAST_CHAPTER = 50
G10B1_AUDIT_CHECKPOINTS = (25, 50)
G10B1_AUDIT_WINDOWS = ((1, 25), (26, 50))
G10B1_TRACK_COUNT = 4
G10B1_TOTAL_CANONICAL_CHAPTERS = 200
G10B1_TOTAL_AUDITS_IF_COMPLETE = 8
G10B1_PRODUCTION_CALL = "run_unattended_production(target_chapter=<exact next chapter>)"

# Deliberately pair the two models for each novel in the base order. Wave order
# rotates by chapter so every track occupies every execution position once per
# four chapters.
_BASE_TRACK_KEYS = (
    (QWEN_MODEL_ID, "ace_evolution"),
    (GLM_MODEL_ID, "ace_evolution"),
    (QWEN_MODEL_ID, "rogue_first_prince"),
    (GLM_MODEL_ID, "rogue_first_prince"),
)


class G10ChronologicalError(RuntimeError):
    """G10B1 state or planning violates the frozen chronological contract."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10ChronologicalError(
            "G10B1 value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class G10TrackSpec:
    run_index: int
    run_id: str
    model_id: str
    novel_id: str
    profile_id: str
    audit_interval: int
    translation_effort: str
    structured_effort: str
    semantic_effort: str

    @property
    def prereg_track_id(self) -> str:
        return f"{self.model_id}::{self.novel_id}"

    def identity(self) -> dict[str, Any]:
        return {
            "run_index": self.run_index,
            "run_id": self.run_id,
            "prereg_track_id": self.prereg_track_id,
            "model_id": self.model_id,
            "novel_id": self.novel_id,
            "profile_id": self.profile_id,
            "audit_interval": self.audit_interval,
            "first_chapter": G10B1_FIRST_CHAPTER,
            "last_chapter": G10B1_LAST_CHAPTER,
            "audit_checkpoints": G10B1_AUDIT_CHECKPOINTS,
            "translation_effort": self.translation_effort,
            "structured_effort": self.structured_effort,
            "semantic_effort": self.semantic_effort,
            "independent_fresh_s0": True,
        }


def _model_slug(model_id: str) -> str:
    if model_id == QWEN_MODEL_ID:
        return "qwen"
    if model_id == GLM_MODEL_ID:
        return "glm"
    raise G10ChronologicalError(f"unexpected G10B1 model {model_id!r}")


def build_track_specs() -> tuple[G10TrackSpec, ...]:
    prereg = {
        (row["model_id"], row["novel_id"]): row
        for row in CHRONOLOGICAL_TRACKS
    }
    if len(prereg) != G10B1_TRACK_COUNT:
        raise G10ChronologicalError(
            "G10B1 preregistered track roster changed"
        )

    rows = []
    for run_index, (model_id, novel_id) in enumerate(
        _BASE_TRACK_KEYS,
        start=1,
    ):
        if (model_id, novel_id) not in prereg:
            raise G10ChronologicalError(
                "G10B1 base track is absent from preregistration"
            )
        frozen = prereg[(model_id, novel_id)]
        if (
            frozen["first_chapter"] != G10B1_FIRST_CHAPTER
            or frozen["last_chapter"] != G10B1_LAST_CHAPTER
            or tuple(frozen["audit_checkpoints"]) != G10B1_AUDIT_CHECKPOINTS
            or frozen["audit_interval"] != 25
            or frozen["independent_fresh_s0"] is not True
        ):
            raise G10ChronologicalError(
                f"G10B1 preregistered track drifted: {model_id}/{novel_id}"
            )
        policy = MODEL_POLICY[model_id]
        rows.append(
            G10TrackSpec(
                run_index=run_index,
                run_id=(
                    f"g10b-run{run_index:02d}-"
                    f"{_model_slug(model_id)}-{novel_id}"
                ),
                model_id=model_id,
                novel_id=novel_id,
                profile_id=NOVELS[novel_id]["profile"],
                audit_interval=NOVELS[novel_id]["audit_interval"],
                translation_effort=policy["primary_translation_effort"],
                structured_effort=policy["structured_auxiliary_effort"],
                semantic_effort=policy["semantic_auxiliary_effort"],
            )
        )

    if len({row.run_id for row in rows}) != G10B1_TRACK_COUNT:
        raise G10ChronologicalError("G10B1 run IDs are not unique")
    return tuple(rows)


TRACK_SPECS = build_track_specs()
TRACK_BY_ID = {track.run_id: track for track in TRACK_SPECS}


def expected_last_audited_chapter(last_completed: int) -> int:
    if (
        isinstance(last_completed, bool)
        or not isinstance(last_completed, int)
        or last_completed < 0
        or last_completed > G10B1_LAST_CHAPTER
    ):
        raise G10ChronologicalError(
            "last_completed must be within 0..50"
        )
    if last_completed < 25:
        return 0
    if last_completed < 50:
        return 25
    return 50


def audit_window_for_target(
    target_chapter: int,
) -> tuple[int, int] | None:
    if (
        isinstance(target_chapter, bool)
        or not isinstance(target_chapter, int)
        or target_chapter < G10B1_FIRST_CHAPTER
        or target_chapter > G10B1_LAST_CHAPTER
    ):
        raise G10ChronologicalError(
            "target chapter must be within 1..50"
        )
    if target_chapter == 25:
        return (1, 25)
    if target_chapter == 50:
        return (26, 50)
    return None


def wave_track_order(chapter: int) -> tuple[str, ...]:
    if (
        isinstance(chapter, bool)
        or not isinstance(chapter, int)
        or chapter < G10B1_FIRST_CHAPTER
        or chapter > G10B1_LAST_CHAPTER
    ):
        raise G10ChronologicalError(
            "wave chapter must be within 1..50"
        )
    base = [track.run_id for track in TRACK_SPECS]
    offset = (chapter - 1) % len(base)
    return tuple(base[offset:] + base[:offset])


def target_contract(track: G10TrackSpec, chapter: int) -> dict[str, Any]:
    window = audit_window_for_target(chapter)
    return {
        "run_id": track.run_id,
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "target_chapter": chapter,
        "production_call": G10B1_PRODUCTION_CALL.replace(
            "<exact next chapter>",
            str(chapter),
        ),
        "reasoning": {
            "translation": track.translation_effort,
            "structured_auxiliary": track.structured_effort,
            "semantic_auxiliary": track.semantic_effort,
        },
        "audit": {
            "due_after_target_commit": window is not None,
            "window": window,
            "real_production_trigger_only": True,
            "synthetic_audit_forbidden": True,
            "pre_audit_snapshot_required": window is not None,
            "post_audit_snapshot_required": window is not None,
            "retroactive_fix_evidence_required": window is not None,
            "dynamic_request_level_budget_admission_required": window is not None,
            "static_audit_send_ceiling_preregistered": False,
        },
        "provider_calls_authorized": False,
    }


def _positive_progress_int(
    value: Any,
    label: str,
    *,
    allow_zero: bool,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise G10ChronologicalError(f"{label} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise G10ChronologicalError(
            f"{label} must be at least {minimum}"
        )
    return value


def classify_track_progress(
    progress: Mapping[str, Any],
    *,
    quarantine: Mapping[str, Any] | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Classify one durable track without mutating it or contacting a Provider."""

    if not isinstance(progress, Mapping):
        raise G10ChronologicalError(
            "chronological progress must be a mapping"
        )
    last_completed = _positive_progress_int(
        progress.get("last_completed"),
        "last_completed",
        allow_zero=True,
    )
    next_chapter = _positive_progress_int(
        progress.get("next_chapter"),
        "next_chapter",
        allow_zero=False,
    )
    last_audited = _positive_progress_int(
        progress.get("last_audited_chapter"),
        "last_audited_chapter",
        allow_zero=True,
    )
    if last_completed > G10B1_LAST_CHAPTER:
        return {
            "status": "corrupt_block",
            "reason": "last_completed_beyond_g10b_range",
        }
    expected_next = last_completed + 1
    if next_chapter != expected_next:
        return {
            "status": "corrupt_block",
            "reason": "progress_next_chapter_mismatch",
        }
    if last_audited not in {0, 25, 50}:
        return {
            "status": "corrupt_block",
            "reason": "unexpected_last_audited_chapter",
        }
    if last_audited > last_completed:
        return {
            "status": "corrupt_block",
            "reason": "audit_marker_ahead_of_completed_chapters",
        }

    if quarantine is not None:
        if not isinstance(quarantine, Mapping):
            return {
                "status": "corrupt_block",
                "reason": "malformed_quarantine",
            }
        if quarantine.get("scope_kind") != "chronological_model_novel_track":
            return {
                "status": "corrupt_block",
                "reason": "quarantine_scope_kind_mismatch",
            }
        if expected_run_id is not None and quarantine.get("scope_id") != expected_run_id:
            return {
                "status": "corrupt_block",
                "reason": "quarantine_scope_id_mismatch",
            }
        blocked = quarantine.get("first_blocked_chapter")
        if (
            isinstance(blocked, bool)
            or not isinstance(blocked, int)
            or blocked < 1
        ):
            return {
                "status": "corrupt_block",
                "reason": "malformed_quarantine_chapter",
            }
        if blocked != next_chapter:
            return {
                "status": "corrupt_block",
                "reason": "quarantine_blocked_chapter_mismatch",
                "first_blocked_chapter": blocked,
            }
        return {
            "status": "quarantined",
            "reason": quarantine.get(
                "outcome",
                "durable_quarantine",
            ),
            "first_blocked_chapter": blocked,
            "last_completed": last_completed,
            "last_audited_chapter": last_audited,
        }

    required_audit = expected_last_audited_chapter(last_completed)
    if last_audited < required_audit:
        window = (
            (1, 25)
            if required_audit == 25
            else (26, 50)
        )
        boundary = window[1]
        if last_completed != boundary:
            return {
                "status": "corrupt_block",
                "reason": "later_chapters_crossed_uncommitted_audit_boundary",
                "audit_window": window,
                "last_completed": last_completed,
                "last_audited_chapter": last_audited,
                "provider_send_authorized": False,
            }
        return {
            "status": "audit_recovery_required",
            "reason": "chapter_boundary_committed_but_real_audit_not_committed",
            "audit_window": window,
            "last_completed": last_completed,
            "last_audited_chapter": last_audited,
            "provider_send_authorized": False,
        }
    if last_audited > required_audit:
        return {
            "status": "corrupt_block",
            "reason": "audit_marker_inconsistent_with_completed_depth",
        }

    if last_completed == G10B1_LAST_CHAPTER:
        return {
            "status": "complete",
            "last_completed": last_completed,
            "last_audited_chapter": last_audited,
            "provider_send_authorized": False,
        }

    target = next_chapter
    return {
        "status": "ready_for_target",
        "target_chapter": target,
        "audit_after_target": audit_window_for_target(target),
        "last_completed": last_completed,
        "last_audited_chapter": last_audited,
        "provider_send_authorized": False,
    }


def plan_chapter_wave(
    chapter: int,
    track_progress: Mapping[str, Mapping[str, Any]],
    *,
    quarantines: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan one model-balanced zero-provider wave for an exact chapter."""

    order = wave_track_order(chapter)
    quarantines = dict(quarantines or {})
    unknown_progress = set(track_progress) - set(TRACK_BY_ID)
    unknown_quarantine = set(quarantines) - set(TRACK_BY_ID)
    if unknown_progress or unknown_quarantine:
        raise G10ChronologicalError(
            "G10B1 controller received unknown track IDs"
        )

    rows = []
    blocked = []
    for run_id in order:
        if run_id not in track_progress:
            blocked.append({
                "run_id": run_id,
                "status": "missing_progress",
            })
            continue
        classification = classify_track_progress(
            track_progress[run_id],
            quarantine=quarantines.get(run_id),
            expected_run_id=run_id,
        )
        status = classification["status"]
        if (
            status == "ready_for_target"
            and classification["target_chapter"] == chapter
        ):
            rows.append(
                target_contract(TRACK_BY_ID[run_id], chapter)
            )
        else:
            blocked.append({
                "run_id": run_id,
                **copy.deepcopy(classification),
            })

    return {
        "chapter": chapter,
        "execution_order": order,
        "ready_invocations": rows,
        "blocked_or_not_at_target": blocked,
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


def g10b1_contract_identity() -> dict[str, Any]:
    primary_positions = {
        track.run_id: [
            wave_track_order(chapter).index(track.run_id) + 1
            for chapter in range(1, G10B1_LAST_CHAPTER + 1)
        ]
        for track in TRACK_SPECS
    }
    return {
        "schema_version": G10B1_SCHEMA_VERSION,
        "revision": G10B1_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessors": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        },
        "track_count": G10B1_TRACK_COUNT,
        "chapters_per_track": G10B1_LAST_CHAPTER,
        "canonical_chapter_executions_if_complete": (
            G10B1_TOTAL_CANONICAL_CHAPTERS
        ),
        "audits_if_complete": G10B1_TOTAL_AUDITS_IF_COMPLETE,
        "audit_checkpoints": G10B1_AUDIT_CHECKPOINTS,
        "audit_windows": G10B1_AUDIT_WINDOWS,
        "production_call": G10B1_PRODUCTION_CALL,
        "tracks": [track.identity() for track in TRACK_SPECS],
        "wave_order": {
            "policy": "rotate_four_track_base_order_by_chapter",
            "base_order": [track.run_id for track in TRACK_SPECS],
            "positions_by_track": primary_positions,
        },
        "audit_policy": {
            "real_production_audit_trigger_only": True,
            "synthetic_audits_forbidden": True,
            "chapter_25_must_finish_audit_1_25_before_chapter_26": True,
            "chapter_50_must_finish_audit_26_50_before_track_complete": True,
            "pre_and_post_audit_snapshots_required": True,
            "normal_safe_retroactive_fixes_required": True,
            "retroactive_before_after_hashes_required": True,
            "audit_request_response_evidence_required": True,
            "audit_subrequests_use_request_level_g10a1_reservations": True,
            "state_dependent_audit_send_count_not_invented": True,
            "g10e_budget_authorization_still_required": True,
        },
        "failure_policy": {
            "one_track_failure_does_not_mutate_other_tracks": True,
            "ambiguous_track_is_durably_quarantined": True,
            "quarantined_track_never_advances": True,
            "audit_boundary_without_committed_audit_requires_recovery": True,
            "corrupt_progress_fails_closed": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "paid_execution_entrypoint_present": False,
    }


G10B1_CONTRACT_SHA256 = _sha256_json(
    g10b1_contract_identity()
)


def assert_paid_execution_blocked() -> None:
    identity = g10b1_contract_identity()
    if identity["provider_calls"] != 0:
        raise G10ChronologicalError(
            "G10B1 unexpectedly reports Provider calls"
        )
    if identity["paid_execution_authorized"] is not False:
        raise G10ChronologicalError(
            "G10B1 unexpectedly authorizes paid execution"
        )
    if identity["paid_execution_entrypoint_present"] is not False:
        raise G10ChronologicalError(
            "G10B1 must not expose paid execution"
        )


__all__ = [
    "G10B1_AUDIT_CHECKPOINTS",
    "G10B1_AUDIT_WINDOWS",
    "G10B1_CONTRACT_SHA256",
    "G10B1_LAST_CHAPTER",
    "G10B1_PRODUCTION_CALL",
    "G10B1_REVISION",
    "G10ChronologicalError",
    "G10TrackSpec",
    "TRACK_BY_ID",
    "TRACK_SPECS",
    "assert_paid_execution_blocked",
    "audit_window_for_target",
    "build_track_specs",
    "classify_track_progress",
    "expected_last_audited_chapter",
    "g10b1_contract_identity",
    "plan_chapter_wave",
    "target_contract",
    "wave_track_order",
]
