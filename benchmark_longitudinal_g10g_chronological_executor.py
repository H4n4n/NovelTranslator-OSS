"""G10G paid chronological executor (arm G10B): one track x one chapter.

G10G runs the *real* bounded production entry point

    run_unattended_production(target_chapter=<exact approved next chapter>)

exactly once for one model x novel track, underneath the durable paid-request
journal and the frozen single-writer credit-equivalent ledger. Importing this
module performs no credential read, no client construction, no network activity,
and no filesystem write.

Boundaries that this module never crosses:

* the frozen G10B S0 root is read-only and is never an execution root; a writable
  working root must be resolved explicitly through :func:`resolve_track_root`,
  which fails closed when the layout is unclear;
* the 25-chapter audit cadence belongs to the production lifecycle. G10G never
  synthesizes an audit subrequest, never inserts an extra checkpoint, and never
  preregisters a fixed audit/subrequest count: the plan carries no such field and
  :func:`assert_no_preregistered_audit_count` proves it;
* every lifecycle subrequest (translation, state extraction, JSON repair, glossary
  review, terminology repair, and every real audit subrequest) gets its own durable
  journal slot keyed by its exact ``request_sha256``, its own request-level G10B
  CE reservation, a persisted ``send_started`` marker before transport, exactly one
  transport attempt, and exact accepted-usage accounting;
* an ambiguous or unusable paid outcome keeps its full reservation, writes one
  durable ``chronological_model_novel_track`` quarantine with the first blocked
  chapter, and stops that track. Other tracks continue in their own invocations;
* ``response_recorded`` journal entries replay with deliberately poisoned
  transport and zero new sends, after G10D local-replay preconditions are proven;
* nothing here auto-advances a chapter, auto-retries a request, or replaces a
  failed operation with a new request slot.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from benchmark_longitudinal_config import (
    LIFECYCLE_OPERATION_PERIODIC_AUDIT,
    PROVIDER_NAME,
)
from benchmark_longitudinal_g10_chronological import (
    G10B1_CONTRACT_SHA256,
    G10B1_LAST_CHAPTER,
    G10ChronologicalError,
    TRACK_BY_ID,
    TRACK_SPECS,
    audit_window_for_target,
    classify_track_progress,
)
from benchmark_longitudinal_g10_matched import G10A2_OPERATION_KIND
from benchmark_longitudinal_g10_paid_authorization import (
    EXPECTED_FREEZE_FILE_SHA256,
    EXPECTED_FREEZE_ID,
    EXPECTED_LEDGER_SHA256,
    EXPECTED_MATCHED_PLAN_SHA256,
    EXPECTED_PRICING_VERSION,
    EXPECTED_S0_MANIFEST_SHA256,
    G10PAID_AUTHORIZATION_SHA256,
    G10PAID_REPOSITORY_ANCHOR,
    G10PAID_REVISION,
    G10PAID_SCHEMA_VERSION,
    G10PaidAuthorizationError,
    G10PaidBudgetError,
    MODEL_RATECARD,
    accepted_credit_equivalent,
    assert_operator_authorization_present,
    build_paid_authorization,
    frozen_execution_binding,
    load_paid_quarantine,
    mark_send_started,
    open_frozen_budget_ledger,
    preflight_and_reserve,
    quarantine_chronological_track,
    reservation_record,
    settle_accepted,
    settle_ambiguous,
    target_state_fingerprint,
    validate_execution_binding,
    verify_paid_authorization,
)
from benchmark_longitudinal_g10_preregistration import G10_BENCHMARK_ID
from benchmark_longitudinal_g10_safety import (
    G10ReplayError,
    assert_local_replay_preconditions,
)
from benchmark_longitudinal_g10_s0 import (
    G10S0_MANIFEST_FILENAME,
    G10S0_RUN_IDENTITY_FILENAME,
)
from benchmark_longitudinal_g7c_stage1_integration import (
    CONTEXT_ENTRYPOINTS,
    DIRECT_LEGACY_OPERATION_MAP,
    normalize_legacy_request,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    inspect_paid_request_journals,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from production_run_lock import production_run_lock


G10G_SCHEMA_VERSION = 1
G10G_REVISION = "g10-chronological-paid-executor-v1"
G10G_REPOSITORY_ANCHOR = G10PAID_REPOSITORY_ANCHOR
G10G_ARM_ID = "G10B"
G10G_OPERATION_PREFIX = "g10b"
G10G_QUARANTINE_FILENAME = "g10_quarantine.json"
G10G_INVOCATION_LEDGER_DIRECTORY = "data/g10_invocation_ledgers"
PRODUCTION_SCRIPT_NAME = (
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)

G10G_TRACK_EXCLUDED_PREFIXES = (
    "data/provider_request_journal/",
    f"{G10G_INVOCATION_LEDGER_DIRECTORY}/",
    "logs/",
)
G10G_TRACK_EXCLUDED_FILES = (".production.lock",)

# Request fields that would freeze a state-dependent audit/subrequest count.
# The production audit request count is state dependent, so no plan may declare
# one; these markers fail the plan closed wherever they appear as a key.
FORBIDDEN_PREREGISTERED_COUNT_KEY_MARKERS = (
    "audit_send",
    "audit_count",
    "audit_subrequest",
    "audit_request",
    "audit_ceiling",
    "audit_budget",
    "audit_max",
    "max_audit",
    "maximum_audit",
    "fixed_audit",
    "synthetic_audit",
    "static_audit",
    "subrequest_count",
    "subrequest_ceiling",
    "subrequest_limit",
    "max_subrequest",
    "maximum_subrequest",
    "expected_audit",
    "audit_chunk_count",
    "audit_block_count",
)


class G10GExecutionError(RuntimeError):
    """G10G state, authorization, accounting, or execution is unsafe."""


class G10GAmbiguousOutcome(G10GExecutionError):
    """A paid subrequest may have completed remotely with no durable response."""

    def __init__(
        self,
        message: str,
        *,
        request_sha256: str,
        reservation_id: str,
        chapter: int,
        journal_status: str,
    ):
        super().__init__(message)
        self.request_sha256 = request_sha256
        self.reservation_id = reservation_id
        self.chapter = chapter
        self.journal_status = journal_status

    def identity(self) -> dict[str, Any]:
        return {
            "request_sha256": self.request_sha256,
            "reservation_id": self.reservation_id,
            "chapter": self.chapter,
            "journal_status": self.journal_status,
            "reason": str(self),
        }


class G10GTerminalFailure(G10GExecutionError):
    """A deterministic provider/policy failure with no retry and no advance."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10GExecutionError(
            "G10G value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10GExecutionError(f"cannot read {label}: {target}") from exc
    if not isinstance(value, dict):
        raise G10GExecutionError(
            f"{label} must contain a JSON object: {target}"
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise G10GExecutionError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G10GExecutionError(f"{label} must be a positive integer")
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10GExecutionError(
            f"{label} must be a positive finite decimal"
        )
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - Decimal raises several types
        raise G10GExecutionError(
            f"{label} must be a positive finite decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G10GExecutionError(
            f"{label} must be a positive finite decimal"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G10GExecutionError(
            f"G10G track root must not be a symlink: {root}"
        )
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G10GExecutionError(
                f"G10G track tree contains a symlink: {item}"
            )


def _aware_utc_now(clock: Any) -> datetime:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G10GExecutionError(
            "G10G clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def g10g_executor_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G10G executor contract."""
    return {
        "schema_version": G10G_SCHEMA_VERSION,
        "revision": G10G_REVISION,
        "repository_anchor": G10G_REPOSITORY_ANCHOR,
        "arm_id": G10G_ARM_ID,
        "predecessor": {
            "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
            "g10_paid_authorization_revision": G10PAID_REVISION,
            "g10b1_contract_sha256": G10B1_CONTRACT_SHA256,
            "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
            "freeze_id": EXPECTED_FREEZE_ID,
            "freeze_file_sha256": EXPECTED_FREEZE_FILE_SHA256,
            "budget_ledger_sha256": EXPECTED_LEDGER_SHA256,
            "s0_manifest_sha256": EXPECTED_S0_MANIFEST_SHA256,
            "pricing_version": EXPECTED_PRICING_VERSION,
        },
        "execution_scope": {
            "one_track_per_invocation": True,
            "one_target_chapter_per_invocation": True,
            "single_lifecycle_invocation": True,
            "lifecycle_entrypoint": "run_unattended_production",
            "automatic_chapter_advance": False,
            "automatic_loop": False,
            "automatic_retry": False,
            "provider_client_max_retries": 0,
            "track_root_must_be_explicit_writable_working_copy": True,
            "frozen_s0_root_is_never_an_execution_root": True,
        },
        "subrequest_accounting": {
            "request_level_reservation_before_transport": True,
            "send_started_persisted_before_transport": True,
            "one_application_visible_transport_attempt": True,
            "exact_request_sha256_journal_identity": True,
            "accepted_ce_from_frozen_ratecard_only": True,
            "audit_subrequests_use_the_same_path": True,
            "preregistered_audit_count_forbidden": True,
        },
        "quarantine": {
            "scope_kind": "chronological_model_novel_track",
            "first_blocked_chapter_required": True,
            "create_once_never_overwrite": True,
            "other_tracks_unaffected": True,
        },
        "replay": {
            "durable_response_recorded_required": True,
            "accepted_ledger_record_required": True,
            "poisoned_transport_required": True,
            "new_provider_send_forbidden": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


G10G_EXECUTOR_SHA256 = _sha256_json(g10g_executor_identity())


def g10g_target_state_fingerprint(track_root: Path | str) -> str:
    """Fingerprint the unchanged target state of one track root."""
    return target_state_fingerprint(
        track_root,
        excluded_prefixes=G10G_TRACK_EXCLUDED_PREFIXES,
        excluded_files=G10G_TRACK_EXCLUDED_FILES,
    )


def track_quarantine_path(track_root: Path | str) -> Path:
    return Path(track_root) / "data" / G10G_QUARANTINE_FILENAME


def invocation_ledger_path(
    track_root: Path | str,
    target_chapter: int,
) -> Path:
    target = _positive_int(target_chapter, "target_chapter")
    return (
        Path(track_root)
        / G10G_INVOCATION_LEDGER_DIRECTORY
        / f"{target:04d}.json"
    )


def _assert_real_production_root(root: Path) -> None:
    """Fail closed when a root looks like the real production project."""
    if (root / "novels").is_dir():
        raise G10GExecutionError(
            "a real NovelTranslator production root is never a G10B execution "
            "root; resolve an explicit writable working copy instead"
        )


def resolve_track_root(
    *,
    production_root: Path | str,
    run_id: str,
    s0_root: Path | str | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> Path:
    """Resolve ``<execution root>/runs/<run_id>`` for exactly one G10B track.

    ``production_root`` is the G10B *execution* root: the writable working copy
    that owns the frozen ``runs/`` layout. It is deliberately not the real
    NovelTranslator production root and never the frozen S0 root -- both are
    rejected rather than silently reused, and an unresolvable layout fails closed.
    """
    if run_id not in TRACK_BY_ID:
        raise G10GExecutionError(f"unknown G10B run id: {run_id!r}")
    root = Path(production_root).expanduser().resolve()
    if not root.is_dir():
        raise G10GExecutionError(
            f"G10B execution root is missing: {root}"
        )
    code_root = Path(__file__).resolve().parent
    if _is_within(root, code_root) or _is_within(code_root, root):
        raise G10GExecutionError(
            "G10B execution root must be external to the code worktree"
        )
    _assert_real_production_root(root)
    if s0_root is not None:
        frozen = Path(s0_root).expanduser().resolve()
        if root == frozen or _is_within(root, frozen) or _is_within(
            frozen, root
        ):
            raise G10GExecutionError(
                "the frozen G10B S0 root is never an execution root; provide an "
                "explicit writable working copy"
            )
    for forbidden in forbidden_roots:
        frozen = Path(forbidden).expanduser().resolve()
        if root == frozen or _is_within(root, frozen) or _is_within(
            frozen, root
        ):
            raise G10GExecutionError(
                "G10B execution root overlaps a forbidden/frozen root"
            )

    runs_dir = root / "runs"
    if not runs_dir.is_dir():
        raise G10GExecutionError(
            "G10B execution root must own the frozen runs/ layout"
        )
    run_root = (runs_dir / run_id).resolve()
    if not _is_within(run_root, root) or not run_root.is_dir():
        raise G10GExecutionError(
            f"G10B track root is missing or unsafe: {run_root}"
        )
    _assert_no_symlinks(run_root)
    if not (run_root / "data").is_dir():
        raise G10GExecutionError(
            "G10B track root lacks a native project data/ directory"
        )

    track = TRACK_BY_ID[run_id]
    identity = _read_object(
        run_root / G10S0_RUN_IDENTITY_FILENAME,
        "G10 S0 run identity",
    )
    check = copy.deepcopy(identity)
    stored = check.pop("identity_sha256", None)
    if _sha256_json(check) != stored:
        raise G10GExecutionError(
            "G10B track S0 origin identity SHA does not recompute"
        )
    for field, expected in {
        "run_id": run_id,
        "run_index": track.run_index,
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "relative_root": f"runs/{run_id}",
        "first_chapter": 1,
        "last_chapter": G10B1_LAST_CHAPTER,
        "audit_checkpoints": [25, 50],
        "audit_interval": 25,
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }.items():
        if identity.get(field) != expected:
            raise G10GExecutionError(
                f"G10B track origin identity changed for {field}"
            )

    if s0_root is not None:
        manifest = _read_object(
            Path(s0_root) / G10S0_MANIFEST_FILENAME,
            "G10 S0 roots manifest",
        )
        records = [
            record
            for record in manifest.get("runs", [])
            if isinstance(record, Mapping) and record.get("run_id") == run_id
        ]
        if len(records) != 1:
            raise G10GExecutionError(
                "G10B track is not uniquely bound in the frozen S0 manifest"
            )
        record = records[0]
        if (
            record.get("relative_root") != f"runs/{run_id}"
            or record.get("model_id") != track.model_id
            or record.get("novel_id") != track.novel_id
            or record.get("run_identity_sha256") != stored
        ):
            raise G10GExecutionError(
                "G10B working track differs from the frozen S0 manifest run"
            )

    return run_root


def _track_progress(track_root: Path) -> dict[str, Any]:
    return _read_object(
        track_root / "data" / "progress.json",
        "G10B track progress",
    )


def _classify_track(
    *,
    run_id: str,
    progress: Mapping[str, Any],
    quarantine: Mapping[str, Any] | None,
) -> dict[str, Any]:
    try:
        return classify_track_progress(
            progress,
            quarantine=quarantine,
            expected_run_id=run_id,
        )
    except G10ChronologicalError as exc:
        raise G10GExecutionError(
            f"G10B track progress could not be classified: {exc}"
        ) from exc


def assert_no_preregistered_audit_count(value: Any) -> None:
    """Fail closed when any key freezes a state-dependent audit send count."""
    offenders: list[str] = []

    def walk(item: Any, prefix: tuple[str, ...]) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    offenders.append(".".join((*prefix, str(key))))
                    continue
                path = (*prefix, key)
                lowered = key.lower()
                if any(
                    marker in lowered
                    for marker in FORBIDDEN_PREREGISTERED_COUNT_KEY_MARKERS
                ):
                    offenders.append(".".join(path))
                walk(nested, path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                walk(nested, (*prefix, str(index)))

    walk(value, ())
    if offenders:
        raise G10GExecutionError(
            "G10G plans must not preregister a fixed audit/subrequest count; "
            f"offending fields: {sorted(offenders)}"
        )


def build_g10g_invocation(
    *,
    binding: Mapping[str, Any],
    production_root: Path | str,
    run_id: str,
    target_chapter: int,
    target_state_fingerprint_sha256: str,
    maximum_credit_equivalent: Any,
    s0_root: Path | str | None = None,
    progress: Mapping[str, Any] | None = None,
    quarantine: Mapping[str, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Build one zero-Provider G10G invocation plan for one track chapter."""
    binding = validate_execution_binding(binding)
    track_root = resolve_track_root(
        production_root=production_root,
        run_id=run_id,
        s0_root=s0_root,
        forbidden_roots=forbidden_roots,
    )
    track = TRACK_BY_ID[run_id]
    target = _positive_int(target_chapter, "target_chapter")
    if target > G10B1_LAST_CHAPTER:
        raise G10GExecutionError(
            f"G10B target chapter must be within 1..{G10B1_LAST_CHAPTER}"
        )

    current_progress = (
        _track_progress(track_root)
        if progress is None
        else copy.deepcopy(dict(progress))
    )
    current_quarantine = (
        load_paid_quarantine(track_quarantine_path(track_root))
        if quarantine is None
        else (None if quarantine is None else copy.deepcopy(dict(quarantine)))
    )
    classification = _classify_track(
        run_id=run_id,
        progress=current_progress,
        quarantine=current_quarantine,
    )
    if classification["status"] != "ready_for_target":
        raise G10GExecutionError(
            "G10B track is not ready for a target chapter: "
            f"{classification['status']}"
        )
    if classification["target_chapter"] != target:
        raise G10GExecutionError(
            "G10B target chapter is not the track's planned next chapter: "
            f"planned={classification['target_chapter']}, requested={target}"
        )

    window = audit_window_for_target(target)
    provider_model_id = MODEL_RATECARD[track.model_id][
        "provider_model_id"
    ]
    authorization = build_paid_authorization(
        binding=binding,
        arm_id=G10G_ARM_ID,
        model_id=track.model_id,
        provider_model_id=provider_model_id,
        logical_operation=G10A2_OPERATION_KIND,
        novel_id=track.novel_id,
        chapter=target,
        run_id=run_id,
        track_effort=track.translation_effort,
        target_state_fingerprint_sha256=target_state_fingerprint_sha256,
        maximum_credit_equivalent=maximum_credit_equivalent,
        maximum_new_provider_sends=1,
    )

    plan = {
        "schema_version": G10G_SCHEMA_VERSION,
        "revision": G10G_REVISION,
        "g10g_executor_sha256": G10G_EXECUTOR_SHA256,
        "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "arm_id": G10G_ARM_ID,
        "run_id": run_id,
        "run_index": track.run_index,
        "model_id": track.model_id,
        "provider_model_id": provider_model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "target_chapter": target,
        "reasoning": {
            "translation": track.translation_effort,
            "structured_auxiliary": track.structured_effort,
            "semantic_auxiliary": track.semantic_effort,
        },
        "audit": {
            "due_after_target": window is not None,
            "window": None if window is None else list(window),
            "real_production_trigger_only": True,
            "no_preregistered_request_count": True,
            "no_synthetic_checkpoints": True,
            "dynamic_request_level_budget_admission_required": (
                window is not None
            ),
        },
        "production_call": (
            "run_unattended_production(target_chapter="
            f"{target})"
        ),
        "single_lifecycle_invocation": True,
        "automatic_chapter_advance": False,
        "automatic_retry": False,
        "track_relative_root": f"runs/{run_id}",
        "target_state_fingerprint_sha256": (
            authorization["target_state_fingerprint_sha256"]
        ),
        "state_fingerprint_excluded_prefixes": list(
            G10G_TRACK_EXCLUDED_PREFIXES
        ),
        "state_fingerprint_excluded_files": list(G10G_TRACK_EXCLUDED_FILES),
        "maximum_credit_equivalent": authorization[
            "maximum_credit_equivalent"
        ],
        "maximum_new_provider_sends": authorization[
            "maximum_new_provider_sends"
        ],
        "freeze_id": binding["freeze_id"],
        "freeze_file_sha256": binding["freeze_file_sha256"],
        "budget_ledger_sha256": binding["budget_ledger_sha256"],
        "matched_plan_sha256": binding["matched_plan_sha256"],
        "s0_manifest_sha256": binding["s0_manifest_sha256"],
        "pricing_version": binding["pricing_version"],
        "classification": {
            "status": classification["status"],
            "last_completed": classification["last_completed"],
            "last_audited_chapter": classification["last_audited_chapter"],
        },
        "authorization": authorization,
        "approval_token": authorization["approval_token"],
        "provider_calls": 0,
    }
    assert_no_preregistered_audit_count(plan)
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def _validate_plan_envelope(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise G10GExecutionError("G10G invocation plan must be a mapping")
    value = copy.deepcopy(dict(plan))
    stored = value.pop("plan_sha256", None)
    _require_sha256(stored, "G10G invocation plan SHA")
    if _sha256_json(value) != stored:
        raise G10GExecutionError(
            "G10G invocation plan SHA does not recompute"
        )
    if value.get("schema_version") != G10G_SCHEMA_VERSION:
        raise G10GExecutionError("unsupported G10G invocation plan schema")
    if value.get("revision") != G10G_REVISION:
        raise G10GExecutionError("unsupported G10G invocation plan revision")
    if value.get("g10g_executor_sha256") != G10G_EXECUTOR_SHA256:
        raise G10GExecutionError(
            "invocation plan binds another G10G executor"
        )
    if value.get("g10_paid_authorization_sha256") != (
        G10PAID_AUTHORIZATION_SHA256
    ):
        raise G10GExecutionError(
            "invocation plan binds another G10F/G10G authorization contract"
        )
    if value.get("arm_id") != G10G_ARM_ID:
        raise G10GExecutionError("G10G plan binds another arm")
    if value.get("provider_calls") != 0:
        raise G10GExecutionError(
            "materialized G10G plan must report zero Provider calls"
        )
    if value.get("single_lifecycle_invocation") is not True:
        raise G10GExecutionError(
            "G10G plan must authorize exactly one lifecycle invocation"
        )
    if value.get("automatic_chapter_advance") is not False:
        raise G10GExecutionError(
            "G10G plan chapter-advance prohibition changed"
        )
    if value.get("automatic_retry") is not False:
        raise G10GExecutionError(
            "G10G plan retry prohibition changed"
        )
    for field in (
        "freeze_id",
        "freeze_file_sha256",
        "budget_ledger_sha256",
        "matched_plan_sha256",
        "s0_manifest_sha256",
        "pricing_version",
        "run_id",
        "model_id",
        "novel_id",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise G10GExecutionError(
                f"G10G plan lacks its frozen binding field {field}"
            )
    nested = value.get("authorization")
    if not isinstance(nested, Mapping):
        raise G10GExecutionError(
            "G10G plan lacks its nested paid authorization"
        )
    if value.get("approval_token") != nested.get("approval_token"):
        raise G10GExecutionError(
            "G10G plan approval token binding changed"
        )
    _positive_int(value.get("target_chapter"), "G10G target chapter")
    _positive_decimal(
        value.get("maximum_credit_equivalent"),
        "G10G maximum_credit_equivalent",
    )
    if value.get("maximum_new_provider_sends") != 1:
        raise G10GExecutionError(
            "G10G invocation authorizes exactly one transport attempt per "
            "subrequest"
        )
    audit = value.get("audit")
    if not isinstance(audit, Mapping):
        raise G10GExecutionError("G10G plan lacks its audit policy block")
    if audit.get("real_production_trigger_only") is not True:
        raise G10GExecutionError(
            "G10G audit policy must bind the real production trigger only"
        )
    if audit.get("no_preregistered_request_count") is not True:
        raise G10GExecutionError(
            "G10G audit policy must not preregister a request count"
        )
    assert_no_preregistered_audit_count(value)
    value["plan_sha256"] = stored
    return value


def verify_g10g_invocation(
    *,
    plan: Mapping[str, Any],
    binding: Mapping[str, Any],
    production_root: Path | str,
    s0_root: Path | str | None = None,
    progress: Mapping[str, Any] | None = None,
    quarantine: Mapping[str, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
    expected_plan_sha256: str | None = None,
    expected_approval_token: str | None = None,
    operator_authorized: Any = None,
    current_state_fingerprint_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify one G10G invocation exactly, before any side effect.

    ``operator_authorized=None`` performs the operator-independent verification
    only; the explicit operator gate is applied separately by
    :func:`execute_g10g_invocation`.
    """
    value = _validate_plan_envelope(plan)
    binding = validate_execution_binding(binding)
    run_id = value["run_id"]
    if run_id not in TRACK_BY_ID:
        raise G10GExecutionError(f"unknown G10B run id: {run_id!r}")
    track = TRACK_BY_ID[run_id]
    for field, expected in {
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "run_index": track.run_index,
        "track_relative_root": f"runs/{run_id}",
    }.items():
        if value.get(field) != expected:
            raise G10GExecutionError(
                f"G10G track binding changed for {field}"
            )
    for field in (
        "freeze_id",
        "freeze_file_sha256",
        "budget_ledger_sha256",
        "matched_plan_sha256",
        "s0_manifest_sha256",
        "pricing_version",
    ):
        if value.get(field) != binding[field]:
            raise G10GExecutionError(
                f"G10G invocation binding changed for {field}"
            )

    if expected_plan_sha256 is not None:
        if value["plan_sha256"] != _require_sha256(
            expected_plan_sha256, "expected_plan_sha256"
        ):
            raise G10GExecutionError(
                "G10G invocation plan SHA mismatch"
            )
    if expected_approval_token is not None:
        if value["approval_token"] != _require_sha256(
            expected_approval_token, "expected_approval_token"
        ):
            raise G10GExecutionError(
                "G10G invocation approval token mismatch"
            )

    track_root = resolve_track_root(
        production_root=production_root,
        run_id=run_id,
        s0_root=s0_root,
        forbidden_roots=forbidden_roots,
    )
    target = value["target_chapter"]
    current_progress = (
        _track_progress(track_root)
        if progress is None
        else copy.deepcopy(dict(progress))
    )
    current_quarantine = (
        load_paid_quarantine(track_quarantine_path(track_root))
        if quarantine is None
        else (None if quarantine is None else copy.deepcopy(dict(quarantine)))
    )
    classification = _classify_track(
        run_id=run_id,
        progress=current_progress,
        quarantine=current_quarantine,
    )
    if classification["status"] != "ready_for_target":
        raise G10GExecutionError(
            "G10B track is not ready for a target chapter: "
            f"{classification['status']}"
        )
    if classification["target_chapter"] != target:
        raise G10GExecutionError(
            "G10B target chapter is not the track's planned next chapter: "
            f"planned={classification['target_chapter']}, requested={target}"
        )

    fingerprint = (
        g10g_target_state_fingerprint(track_root)
        if current_state_fingerprint_sha256 is None
        else _require_sha256(
            current_state_fingerprint_sha256,
            "current_state_fingerprint_sha256",
        )
    )
    verified_authorization = verify_paid_authorization(
        plan=value["authorization"],
        expected_approval_token=value["approval_token"],
        current_state_fingerprint_sha256=fingerprint,
        operator_authorized=(
            True if operator_authorized is None else operator_authorized
        ),
        binding=binding,
    )

    return {
        "plan": value,
        "plan_sha256": value["plan_sha256"],
        "approval_token": value["approval_token"],
        "track": track,
        "track_root": track_root,
        "target_chapter": target,
        "progress": current_progress,
        "quarantine": current_quarantine,
        "classification": classification,
        "current_state_fingerprint_sha256": fingerprint,
        "authorization": verified_authorization,
        "provider_calls": 0,
    }


@dataclass(frozen=True)
class G10GReservationContext:
    """Per-subrequest CE reservation context for one G10G invocation."""

    ledger: Any
    plan: Mapping[str, Any]
    model_id: str
    novel_id: str
    run_id: str
    target_chapter: int
    reserved_credit_equivalent: str
    quarantine_path: Path
    arm_id: str = G10G_ARM_ID
    scope_id: str = G10_BENCHMARK_ID

    def authorization_plan_for(
        self,
        logical_operation: str,
        chapter: int,
    ) -> dict[str, Any]:
        """Return the reservation identity for one journaled subrequest."""
        if not isinstance(logical_operation, str) or not logical_operation.strip():
            raise G10GExecutionError(
                "G10G subrequest logical operation must be non-empty"
            )
        return {
            "arm_id": self.arm_id,
            "scope_id": self.scope_id,
            "logical_operation": logical_operation,
            "model_id": self.model_id,
            "novel_id": self.novel_id,
            "chapter": _positive_int(chapter, "subrequest chapter"),
        }


def build_g10g_reservation_context(
    *,
    binding: Mapping[str, Any],
    plan: Mapping[str, Any],
    track_root: Path | str,
    ledger: Any = None,
    clock: Any = None,
) -> G10GReservationContext:
    """Build the per-subrequest reservation context for one invocation."""
    binding = validate_execution_binding(binding)
    value = _validate_plan_envelope(plan)
    use_ledger = (
        open_frozen_budget_ledger(binding, clock=clock)
        if ledger is None
        else ledger
    )
    root = Path(track_root).resolve()
    return G10GReservationContext(
        ledger=use_ledger,
        plan=value,
        model_id=value["model_id"],
        novel_id=value["novel_id"],
        run_id=value["run_id"],
        target_chapter=value["target_chapter"],
        reserved_credit_equivalent=_decimal_text(
            _positive_decimal(
                value["maximum_credit_equivalent"],
                "subrequest reserved_credit_equivalent",
            )
        ),
        quarantine_path=track_quarantine_path(root),
    )


def _logical_operation_kind(
    logical_operation: str,
    request_sha256: str,
    *,
    multi_request_slot: bool,
) -> str:
    if logical_operation not in _ALLOWED_OPERATIONS:
        raise G10GExecutionError(
            f"unknown G10B lifecycle operation: {logical_operation!r}"
        )
    prefix = G10G_OPERATION_PREFIX
    if not multi_request_slot:
        return f"{prefix}_{logical_operation}"
    return f"{prefix}_{logical_operation}_{request_sha256[:20]}"


_ALLOWED_OPERATIONS = frozenset(DIRECT_LEGACY_OPERATION_MAP.values()) | frozenset(
    CONTEXT_ENTRYPOINTS.values()
)

_HOOK_SYMBOLS = (
    "journaled_chat_request",
    "call_api_with_retries",
    "send_api_request_once",
    "usage_and_cost",
    "usage_and_cost_for_pricing",
    "reprice_saved_stats_if_needed",
    "pricing_snapshot",
)

_SAVED_STATS_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cost_usd",
    "rate_correct_cost_usd",
)


def _validate_saved_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Validate durable accepted stats; an all-zero placeholder stays valid."""
    if not isinstance(stats, Mapping):
        raise G10GExecutionError("saved G10B stats must be a mapping")
    value = copy.deepcopy(dict(stats))
    for name in _SAVED_STATS_FIELDS:
        item = value.get(name, 0)
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or item < 0
        ):
            raise G10GExecutionError(
                f"saved G10B stats contain invalid {name}"
            )
    if all((value.get(name, 0) or 0) == 0 for name in _SAVED_STATS_FIELDS):
        return value
    if value.get("pricing_version") != EXPECTED_PRICING_VERSION:
        raise G10GExecutionError(
            "saved G10B stats bind another pricing version"
        )
    return value


def _stats_from_accepted(
    *,
    model_id: str,
    usage: Any,
    exact_ce: Decimal,
    request_started_utc: str,
) -> dict[str, Any]:
    row = MODEL_RATECARD[model_id]
    prompt_tokens = int(getattr(usage, "prompt_tokens"))
    completion_tokens = int(getattr(usage, "completion_tokens"))
    exposed = bool(getattr(usage, "cached_tokens_exposed", False))
    cached = getattr(usage, "cached_tokens", None)
    cache_hits = int(cached) if exposed and cached is not None else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": prompt_tokens - cache_hits,
        "cost_usd": float(exact_ce),
        "rate_correct_cost_usd": float(exact_ce),
        "pricing_version": EXPECTED_PRICING_VERSION,
        "pricing_period": "flat",
        "request_started_utc": request_started_utc,
        "cache_hit_rate_per_million": float(row["cache_read"]),
        "cache_miss_rate_per_million": float(row["input"]),
        "output_rate_per_million": float(row["output"]),
        "credit_equivalent": _decimal_text(exact_ce),
        "provider_transport": "command_code_provider_direct",
    }


def _response_from_envelope(envelope: Mapping[str, Any]) -> Any:
    stats = _validate_saved_stats(envelope.get("stats"))
    usage = SimpleNamespace(
        prompt_tokens=int(stats["prompt_tokens"]),
        completion_tokens=int(stats["completion_tokens"]),
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=int(stats["cache_hit_tokens"])
        ),
        _g10b_stats=copy.deepcopy(stats),
    )
    return SimpleNamespace(
        id=envelope.get("provider_response_id", ""),
        model=envelope.get("provider_response_model", ""),
        choices=[
            SimpleNamespace(
                finish_reason=envelope.get("finish_reason"),
                message=SimpleNamespace(content=envelope.get("content", "")),
            )
        ],
        usage=usage,
    )


def _poisoned_transport(*_args: Any, **_kwargs: Any) -> Any:
    raise G10GExecutionError(
        "G10G replay transport is deliberately poisoned; no send is authorized"
    )


def install_g10g_paid_runtime(
    production_module: Any,
    *,
    model_id: str,
    provider_client: Any,
    reservation_context: G10GReservationContext,
    clock: Any = None,
) -> dict[str, Any]:
    """Install the direct Provider + journal + CE-reservation runtime.

    Every provider-shaped lifecycle call is normalized to the frozen Command Code
    request shape, journaled under its exact ``request_sha256``, reserved at the
    request level, marked ``send_started``, and then sent exactly once. Nothing
    here retries, reissues, or substitutes a request.
    """
    if not getattr(production_module, "_RUNTIME_INITIALIZED", False):
        raise G10GExecutionError(
            "real production engine must be initialized first"
        )
    if getattr(production_module, "client", None) is not None:
        raise G10GExecutionError(
            "provider-suppressed engine unexpectedly has a client"
        )
    if getattr(production_module, "api_key", None) is not None:
        raise G10GExecutionError(
            "provider-suppressed engine unexpectedly retained a key"
        )
    if provider_client is None:
        raise G10GExecutionError(
            "G10G runtime requires an injected Provider client"
        )
    if not isinstance(reservation_context, G10GReservationContext):
        raise G10GExecutionError(
            "G10G runtime requires a G10GReservationContext"
        )
    if model_id not in MODEL_RATECARD:
        raise G10GExecutionError(f"unknown G10B model id: {model_id!r}")
    if reservation_context.model_id != model_id:
        raise G10GExecutionError(
            "G10G reservation context binds another model"
        )
    if getattr(production_module, "_g10g_paid_runtime_state", None) is not None:
        raise G10GExecutionError("G10G paid runtime is already installed")

    missing = [
        name
        for name in (*_HOOK_SYMBOLS, *CONTEXT_ENTRYPOINTS)
        if not callable(getattr(production_module, name, None))
    ]
    if missing:
        raise G10GExecutionError(
            "the real production engine lacks G10G hook symbols: "
            f"{sorted(missing)}"
        )

    use_clock = clock or (lambda: datetime.now(timezone.utc))
    ledger = reservation_context.ledger
    originals = {
        name: getattr(production_module, name)
        for name in (*_HOOK_SYMBOLS, *CONTEXT_ENTRYPOINTS)
    }
    operation_stack: list[tuple[str, int]] = []
    counters: dict[str, Any] = {
        "transport_attempts": 0,
        "new_sends": 0,
        "replays": 0,
        "reservations": [],
    }

    def current_context() -> tuple[str, int]:
        if not operation_stack:
            raise G10GExecutionError(
                "provider-shaped auxiliary request occurred outside an "
                "explicit G10G lifecycle context"
            )
        return operation_stack[-1]

    @contextmanager
    def operation_context(logical_operation: str, chapter: int):
        if logical_operation not in _ALLOWED_OPERATIONS:
            raise G10GExecutionError(
                "invalid G10G operation context"
            )
        if (
            isinstance(chapter, bool)
            or not isinstance(chapter, int)
            or chapter < 1
        ):
            raise G10GExecutionError(
                "G10G operation context chapter must be positive"
            )
        operation_stack.append((logical_operation, chapter))
        try:
            yield
        finally:
            popped = operation_stack.pop()
            if popped != (logical_operation, chapter):
                raise G10GExecutionError(
                    "G10G operation context stack corrupted"
                )

    def reservation_for(request_sha256: str) -> dict[str, Any] | None:
        matches = [
            record
            for record in ledger.document["records"]
            if record.get("request_sha256") == request_sha256
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise G10GExecutionError(
                "G10G request does not resolve to exactly one reservation"
            )
        return matches[0]

    def journaled_subrequest(
        logical_operation: str,
        chapter_number: int,
        source_text: str,
        request_kwargs: Mapping[str, Any],
        *,
        translation_text: str = "",
        multi_request_slot: bool,
    ) -> dict[str, Any]:
        actual_request = normalize_legacy_request(
            model_id=model_id,
            logical_operation=logical_operation,
            request_kwargs=request_kwargs,
        )
        provider_identity = build_provider_request_identity(
            request_body=actual_request
        )
        novel_id = (
            production_module.ACTIVE_NOVEL_CONFIG.id
            if production_module.ACTIVE_NOVEL_CONFIG is not None
            else "unknown"
        )
        profile_id = (
            getattr(production_module.ACTIVE_PROFILE, "id", "") or "unknown"
        )
        request_sha = provider_identity.request_sha256
        journal_identity = request_identity(
            operation_kind=logical_operation,
            novel_id=novel_id,
            chapter=chapter_number,
            provider=PROVIDER_NAME,
            model=provider_identity.requested_model,
            profile_id=profile_id,
            source_sha256=_sha256_text(source_text),
            translation_sha256=(
                _sha256_text(translation_text) if translation_text else ""
            ),
            request_payload=actual_request,
        )
        if journal_identity["request_sha256"] != request_sha:
            raise G10GExecutionError(
                "G10G journal and provider request identities differ"
            )
        journal_kind = _logical_operation_kind(
            logical_operation,
            request_sha,
            multi_request_slot=multi_request_slot,
        )
        journal_path = paid_request_journal_path(
            production_module.PAID_REQUEST_JOURNAL_DIR,
            journal_kind,
            chapter_number,
        )
        existing = load_matching_journal(journal_path, journal_identity)
        journal_status = None if existing is None else existing["status"]

        if journal_status == "response_recorded":
            record = reservation_for(request_sha)
            if record is None or record.get("status") != "accepted" or not record.get(
                "send_started"
            ):
                raise G10GTerminalFailure(
                    "G10G durable response exists without its accepted "
                    "reservation; no replay and no resend is allowed"
                )
            try:
                gate = assert_local_replay_preconditions(
                    journal_status="response_recorded",
                    ledger_status="accepted",
                    exact_request_match=True,
                    transport_poisoned=True,
                )
            except G10ReplayError as exc:
                raise G10GTerminalFailure(
                    f"G10G replay preconditions are not proven: {exc}"
                ) from exc
            result = run_journaled_paid_request(
                journal_path=journal_path,
                identity=journal_identity,
                save_json=lambda path, value: atomic_write_json(path, value),
                prepare_attempt=lambda: _poisoned_transport(),
                send_once=lambda: _poisoned_transport(),
                normalize_response=(
                    lambda *_args, **_kwargs: _poisoned_transport()
                ),
            )
            if result.get("replayed") is not True or result.get("api_called"):
                raise G10GTerminalFailure(
                    "G10G replay unexpectedly attempted provider transport"
                )
            counters["replays"] += 1
            counters["reservations"].append(
                {
                    "logical_operation": logical_operation,
                    "journal_kind": journal_kind,
                    "journal_file": journal_path.name,
                    "request_sha256": request_sha,
                    "reservation_id": record["reservation_id"],
                    "reservation_status": record["status"],
                    "accepted_credit_equivalent": record[
                        "accepted_credit_equivalent"
                    ],
                    "replayed": True,
                    "replay_gate": gate,
                }
            )
            return result

        if journal_status in {"started", "uncertain"}:
            record = reservation_for(request_sha)
            if (
                record is None
                or not record.get("send_started")
                or record.get("status") != "reserved"
            ):
                raise G10GTerminalFailure(
                    "G10G journal holds an unresolved subrequest without a "
                    "started reservation; fail closed without reissue"
                )
            raise G10GAmbiguousOutcome(
                "G10G paid subrequest has an unresolved "
                f"{journal_status} durable outcome; automatic reissue is "
                "forbidden",
                request_sha256=request_sha,
                reservation_id=record["reservation_id"],
                chapter=chapter_number,
                journal_status=journal_status,
            )

        reservation = preflight_and_reserve(
            ledger=ledger,
            plan=reservation_context.authorization_plan_for(
                logical_operation,
                chapter_number,
            ),
            request_sha256=request_sha,
            reserved_credit_equivalent=(
                reservation_context.reserved_credit_equivalent
            ),
        )
        reservation_id = reservation["reservation_id"]
        mark_send_started(ledger=ledger, reservation_id=reservation_id)
        counters["new_sends"] += 1
        request_started_utc = _aware_utc_now(use_clock).isoformat()
        box: dict[str, Any] = {}

        def prepare_attempt() -> dict[str, Any]:
            return {
                "mode": G10G_REVISION,
                "arm_id": G10G_ARM_ID,
                "run_id": reservation_context.run_id,
                "target_chapter": reservation_context.target_chapter,
                "logical_operation": logical_operation,
                "chapter": chapter_number,
                "request_sha256": request_sha,
                "journal_kind": journal_kind,
                "reservation_id": reservation_id,
                "pricing_version": EXPECTED_PRICING_VERSION,
                "request_started_utc": request_started_utc,
            }

        def send_once() -> Any:
            counters["transport_attempts"] += 1
            return send_chat_completion_once(provider_client, actual_request)

        def normalize_response(
            raw_response: Any,
            _attempt_metadata: Mapping[str, Any],
        ) -> dict[str, Any]:
            normalized = normalize_provider_response(
                raw_response,
                request_identity=provider_identity,
            )
            accepted = accept_provider_response(
                provider_identity,
                normalized,
            )
            exact_ce = accepted_credit_equivalent(model_id, accepted.usage)
            envelope = {
                "content": accepted.content,
                "finish_reason": accepted.finish_reason,
                "stats": _stats_from_accepted(
                    model_id=model_id,
                    usage=accepted.usage,
                    exact_ce=exact_ce,
                    request_started_utc=request_started_utc,
                ),
                "provider_response_id": accepted.provider_response_id,
                "provider_response_model": accepted.response_model,
                "request_sha256": request_sha,
                "logical_operation": logical_operation,
                "reservation_id": reservation_id,
                "accepted_credit_equivalent": _decimal_text(exact_ce),
            }
            box.clear()
            box.update(envelope)
            return envelope

        try:
            result = run_journaled_paid_request(
                journal_path=journal_path,
                identity=journal_identity,
                save_json=lambda path, value: atomic_write_json(path, value),
                prepare_attempt=prepare_attempt,
                send_once=send_once,
                normalize_response=normalize_response,
            )
        except BaseException as exc:  # noqa: BLE001 - ambiguity is durable
            current = reservation_record(ledger, reservation_id)
            if current.get("send_started"):
                if current.get("status") == "reserved":
                    settle_ambiguous(
                        ledger=ledger,
                        reservation_id=reservation_id,
                        reason=(
                            "G10G subrequest did not reach a durable response: "
                            f"{type(exc).__name__}: {str(exc)[:400]}"
                        ),
                    )
                raise G10GAmbiguousOutcome(
                    "G10G paid subrequest did not reach a durable response: "
                    f"{type(exc).__name__}: {str(exc)[:400]}",
                    request_sha256=request_sha,
                    reservation_id=reservation_id,
                    chapter=chapter_number,
                    journal_status="uncertain",
                ) from exc
            raise

        if not isinstance(result, Mapping) or result.get("api_called") is not True:
            raise G10GTerminalFailure(
                "G10G paid journal did not report exactly one transport attempt"
            )
        envelope = result.get("response")
        if not isinstance(envelope, Mapping) or not box:
            raise G10GTerminalFailure(
                "G10G paid journal returned no accepted response envelope"
            )
        try:
            settled = settle_accepted(
                ledger=ledger,
                reservation_id=reservation_id,
                accepted_credit_equivalent=envelope[
                    "accepted_credit_equivalent"
                ],
            )
        except G10PaidBudgetError as exc:
            raise G10GTerminalFailure(
                "G10G accepted usage exceeded its conservative reservation or "
                f"the G10 budget ceiling: {exc}"
            ) from exc
        counters["reservations"].append(
            {
                "logical_operation": logical_operation,
                "journal_kind": journal_kind,
                "journal_file": journal_path.name,
                "request_sha256": request_sha,
                "reservation_id": reservation_id,
                "reservation_status": settled["status"],
                "accepted_credit_equivalent": envelope[
                    "accepted_credit_equivalent"
                ],
                "replayed": False,
            }
        )
        return result

    def journaled_chat_request(
        operation_kind: str,
        chapter_number: int,
        source_text: str,
        request_kwargs: Mapping[str, Any],
        *,
        translation_text: str = "",
    ) -> dict[str, Any]:
        try:
            logical = DIRECT_LEGACY_OPERATION_MAP[operation_kind]
        except KeyError as exc:
            raise G10GExecutionError(
                "unmapped direct legacy operation "
                f"{operation_kind!r}"
            ) from exc
        return journaled_subrequest(
            logical,
            chapter_number,
            source_text,
            request_kwargs,
            translation_text=translation_text,
            multi_request_slot=False,
        )

    def auxiliary_request(request_kwargs: Mapping[str, Any]) -> Any:
        logical, chapter = current_context()
        messages = request_kwargs.get("messages")
        if not isinstance(messages, list):
            raise G10GExecutionError("auxiliary request lacks messages")
        source_text = "\n".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, Mapping)
        )
        result = journaled_subrequest(
            logical,
            chapter,
            source_text,
            request_kwargs,
            multi_request_slot=True,
        )
        return _response_from_envelope(result["response"])

    def call_api_with_retries(**kwargs: Any) -> Any:
        # Historical name only. G10G performs one journal-owned attempt.
        return auxiliary_request(kwargs)

    def send_api_request_once(**kwargs: Any) -> Any:
        return auxiliary_request(kwargs)

    def usage_and_cost(usage: Any) -> dict[str, Any]:
        stats = getattr(usage, "_g10b_stats", None)
        if stats is None:
            raise G10GExecutionError(
                "G10G response usage lacks bound accounting stats"
            )
        return _validate_saved_stats(stats)

    def usage_and_cost_for_pricing(usage: Any, _pricing: Any) -> dict[str, Any]:
        return usage_and_cost(usage)

    def reprice_saved_stats_if_needed(stats: Any) -> dict[str, Any]:
        return _validate_saved_stats(stats)

    def pricing_snapshot(moment: Any = None) -> dict[str, Any]:
        row = MODEL_RATECARD[model_id]
        return {
            "pricing_version": EXPECTED_PRICING_VERSION,
            "pricing_period": "flat",
            "request_started_utc": _aware_utc_now(
                lambda: (moment if moment is not None else use_clock())
            ).isoformat(),
            "cache_hit_rate_per_million": float(row["cache_read"]),
            "cache_miss_rate_per_million": float(row["input"]),
            "output_rate_per_million": float(row["output"]),
            "model_id": model_id,
        }

    production_module.MODEL = MODEL_RATECARD[model_id]["provider_model_id"]
    production_module.client = None
    production_module.api_key = None
    production_module.AUTO_OFF_PEAK_ONLY = False
    production_module.journaled_chat_request = journaled_chat_request
    production_module.call_api_with_retries = call_api_with_retries
    production_module.send_api_request_once = send_api_request_once
    production_module.usage_and_cost = usage_and_cost
    production_module.usage_and_cost_for_pricing = usage_and_cost_for_pricing
    production_module.reprice_saved_stats_if_needed = (
        reprice_saved_stats_if_needed
    )
    production_module.pricing_snapshot = pricing_snapshot

    wrapped_entrypoints: dict[str, Any] = {}
    for name, logical_operation in CONTEXT_ENTRYPOINTS.items():
        original = originals[name]

        def make_wrapper(original_callable: Any, operation: str):
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                if args:
                    chapter = args[0]
                else:
                    chapter = kwargs.get("chapter_number")
                    if (
                        chapter is None
                        and operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT
                    ):
                        chapter = kwargs.get("end_chapter")
                if (
                    operation == LIFECYCLE_OPERATION_PERIODIC_AUDIT
                    and len(args) >= 2
                ):
                    chapter = args[1]
                with operation_context(operation, int(chapter)):
                    return original_callable(*args, **kwargs)

            return wrapped

        wrapper = make_wrapper(original, logical_operation)
        setattr(production_module, name, wrapper)
        wrapped_entrypoints[name] = wrapper

    state = {
        "mode": G10G_REVISION,
        "arm_id": G10G_ARM_ID,
        "model_id": model_id,
        "provider_model_id": MODEL_RATECARD[model_id]["provider_model_id"],
        "run_id": reservation_context.run_id,
        "novel_id": reservation_context.novel_id,
        "target_chapter": reservation_context.target_chapter,
        "journaled_operations": sorted(_ALLOWED_OPERATIONS),
        "context_entrypoints": dict(CONTEXT_ENTRYPOINTS),
        "budget_ledger_path": str(getattr(ledger, "path", "")),
        "reserved_credit_equivalent": (
            reservation_context.reserved_credit_equivalent
        ),
        "counters": counters,
        "originals": originals,
        "wrapped_entrypoints": wrapped_entrypoints,
        "provider_transport": "command_code_provider_direct",
        "coding_agent_used_for_inference": False,
        "provider_calls_authorized": True,
    }
    production_module._g10g_paid_runtime_state = state
    return state


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def default_production_module_loader(
    *,
    code_root: Path | str,
    track_root: Path | str,
) -> dict[str, Any]:
    """Load and initialize the real production engine with provider suppressed."""
    from full_pipeline_luna_runtime import (
        initialize_engine_without_provider,
        load_experiment_engine_module,
    )

    production_script = Path(code_root).resolve() / PRODUCTION_SCRIPT_NAME
    if not production_script.is_file():
        raise G10GExecutionError(
            f"real production script is missing: {production_script}"
        )
    with _working_directory(Path(track_root).resolve()):
        loaded = load_experiment_engine_module(production_script)
        initialize_engine_without_provider(loaded["module"])
    return loaded


def _write_invocation_ledger(
    *,
    track_root: Path,
    plan: Mapping[str, Any],
    context: G10GReservationContext,
    counters: Mapping[str, Any],
    report: Mapping[str, Any],
) -> Path:
    path = invocation_ledger_path(track_root, plan["target_chapter"])
    record = {
        "schema_version": G10G_SCHEMA_VERSION,
        "revision": G10G_REVISION,
        "g10g_executor_sha256": G10G_EXECUTOR_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "arm_id": G10G_ARM_ID,
        "run_id": plan["run_id"],
        "model_id": plan["model_id"],
        "novel_id": plan["novel_id"],
        "target_chapter": plan["target_chapter"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "freeze_id": plan["freeze_id"],
        "budget_ledger_sha256": plan["budget_ledger_sha256"],
        "pricing_version": plan["pricing_version"],
        "subrequests": copy.deepcopy(list(counters.get("reservations", []))),
        "lifecycle_invocations": 1,
        "provider_transport_attempts": int(
            counters.get("transport_attempts", 0)
        ),
        "new_provider_sends": int(counters.get("new_sends", 0)),
        "replayed_subrequests": int(counters.get("replays", 0)),
        "provider_calls": int(counters.get("transport_attempts", 0)),
        "committed_credit_equivalent": report.get(
            "committed_credit_equivalent"
        ),
        "remaining_credit_equivalent": report.get(
            "remaining_credit_equivalent"
        ),
        "automatic_chapter_advance": False,
        "automatic_retry": False,
        "observed_subrequest_count": len(
            counters.get("reservations", [])
        ),
    }
    # The durable record is written after the lifecycle ran: it reports what
    # actually happened and deliberately carries no preregistered expectation of
    # how many subrequests a chapter needs.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    return path


def execute_g10g_invocation(
    *,
    code_root: Path | str,
    production_root: Path | str,
    freeze_root: Path | str,
    matched_evidence_root: Path | str,
    s0_root: Path | str,
    plan: Mapping[str, Any],
    expected_plan_sha256: str,
    expected_approval_token: str,
    operator_authorized: Any,
    api_key: str | None = None,
    client_factory: Any = None,
    production_module_loader: Callable[..., Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
    clock: Any = None,
) -> dict[str, Any]:
    """Execute exactly one approved G10B track chapter through the real engine.

    ``production_root`` here is the G10B *execution* root (the writable working
    copy that owns ``runs/``); the read-only real NovelTranslator production root
    is bound inside the frozen execution binding and is never mutated.
    ``production_module_loader`` exists only so tests can inject a scripted
    lifecycle module with the same patched symbol vocabulary; an injected module
    still goes through :func:`install_g10g_paid_runtime`, journaling, request-level
    reservation, and accepted-usage settlement exactly like the real one.
    """
    code = Path(code_root).resolve()
    if not code.is_dir():
        raise G10GExecutionError(f"G10G code root is missing: {code}")
    if not (code / PRODUCTION_SCRIPT_NAME).is_file():
        raise G10GExecutionError(
            "G10G code root lacks the real production script"
        )

    # The writable G10B execution root and the authoritative NovelTranslator
    # source/repository root are deliberately different roots.  The frozen
    # matched-plan/source binding must be rebuilt from the authoritative code
    # checkout, while `production_root` remains the mutable chronological
    # execution root used by resolve_track_root().
    binding = frozen_execution_binding(
        production_root=code,
        freeze_root=freeze_root,
        matched_evidence_root=matched_evidence_root,
        s0_root=s0_root,
        forbidden_roots=forbidden_roots,
        repo_root=code,
    )

    verified = verify_g10g_invocation(
        plan=plan,
        binding=binding,
        production_root=production_root,
        s0_root=s0_root,
        forbidden_roots=forbidden_roots,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
        operator_authorized=None,
    )
    plan_value = verified["plan"]
    track = verified["track"]
    track_root = verified["track_root"]

    try:
        assert_operator_authorization_present(operator_authorized)
    except G10PaidAuthorizationError as exc:
        return {
            "status": "AUTHORIZATION_REQUIRED",
            "arm_id": G10G_ARM_ID,
            "run_id": plan_value["run_id"],
            "model_id": plan_value["model_id"],
            "novel_id": plan_value["novel_id"],
            "target_chapter": plan_value["target_chapter"],
            "plan_sha256": plan_value["plan_sha256"],
            "approval_token": plan_value["approval_token"],
            "track_status": verified["classification"]["status"],
            "track_root": str(track_root),
            "provider_calls": 0,
            "provider_transport_attempts": 0,
            "paid_execution_authorized": False,
            "reason": str(exc),
        }

    if not isinstance(api_key, str) or not api_key.strip():
        raise G10GExecutionError(
            "explicitly authorized G10G execution requires an injected API key"
        )

    context = build_g10g_reservation_context(
        binding=binding,
        plan=plan_value,
        track_root=track_root,
        clock=clock,
    )
    provider_client = create_provider_client(
        api_key=api_key,
        client_factory=client_factory,
    )
    loader = production_module_loader or default_production_module_loader
    target = plan_value["target_chapter"]

    with production_run_lock(track_root):
        verified_under_lock = verify_g10g_invocation(
            plan=plan_value,
            binding=binding,
            production_root=production_root,
            s0_root=s0_root,
            forbidden_roots=forbidden_roots,
            expected_plan_sha256=expected_plan_sha256,
            expected_approval_token=expected_approval_token,
            operator_authorized=True,
        )
        verified = verified_under_lock
        track_root = verified["track_root"]
        progress_before = verified["progress"]

        loaded: Mapping[str, Any] | None = None
        runtime_state: Mapping[str, Any] | None = None
        try:
            loaded = loader(
                code_root=code,
                track_root=track_root,
            )
            if not isinstance(loaded, Mapping) or "module" not in loaded:
                raise G10GExecutionError(
                    "G10G production module loader returned no module"
                )
            module = loaded["module"]
            runtime_state = install_g10g_paid_runtime(
                module,
                model_id=track.model_id,
                provider_client=provider_client,
                reservation_context=context,
                clock=clock,
            )
            runner = getattr(module, "run_unattended_production", None)
            if not callable(runner):
                raise G10GExecutionError(
                    "real production module lacks run_unattended_production"
                )
            # The legacy production engine owns project paths as relative
            # paths. Loading/initializing under the track root is not enough:
            # the entire lifecycle must execute there as well, otherwise
            # journals, pending files, logs, QA, audits, and retroactive fixes
            # resolve against the caller's process CWD and can collide across
            # tracks.
            with _working_directory(track_root):
                runner(target_chapter=target)
        except G10GAmbiguousOutcome as exc:
            marker = quarantine_chronological_track(
                path=context.quarantine_path,
                model_id=plan_value["model_id"],
                novel_id=plan_value["novel_id"],
                run_id=plan_value["run_id"],
                first_blocked_chapter=target,
                request_sha256=exc.request_sha256,
                reservation_id=exc.reservation_id,
                reason=str(exc),
                clock=clock,
            )
            return {
                "status": "QUARANTINED",
                "arm_id": G10G_ARM_ID,
                "run_id": plan_value["run_id"],
                "model_id": plan_value["model_id"],
                "novel_id": plan_value["novel_id"],
                "target_chapter": target,
                "plan_sha256": plan_value["plan_sha256"],
                "approval_token": plan_value["approval_token"],
                "track_root": str(track_root),
                "quarantine": marker,
                "ambiguity": exc.identity(),
                "first_blocked_chapter": target,
                "full_reservation_retained": True,
                "progress_advanced": False,
                "provider_calls": _runtime_counter(
                    runtime_state, "transport_attempts"
                ),
                "provider_transport_attempts": _runtime_counter(
                    runtime_state, "transport_attempts"
                ),
                "paid_execution_authorized": True,
                "automatic_chapter_advance": False,
                "automatic_retry": False,
            }
        except BaseException as exc:  # noqa: BLE001 - terminal by contract
            return {
                "status": "TERMINAL_FAILURE",
                "arm_id": G10G_ARM_ID,
                "run_id": plan_value["run_id"],
                "model_id": plan_value["model_id"],
                "novel_id": plan_value["novel_id"],
                "target_chapter": target,
                "plan_sha256": plan_value["plan_sha256"],
                "approval_token": plan_value["approval_token"],
                "track_root": str(track_root),
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc)[:1000],
                },
                "progress_advanced": False,
                "provider_calls": _runtime_counter(
                    runtime_state, "transport_attempts"
                ),
                "provider_transport_attempts": _runtime_counter(
                    runtime_state, "transport_attempts"
                ),
                "paid_execution_authorized": True,
                "terminal": True,
                "automatic_chapter_advance": False,
                "automatic_retry": False,
                "operation_replaced": False,
            }
        finally:
            if loaded is not None and loaded.get("module_name"):
                sys.modules.pop(loaded["module_name"], None)

        progress_after = _track_progress(track_root)
        if progress_after.get("last_completed") != target:
            raise G10GExecutionError(
                "the real lifecycle did not finish exactly the approved "
                f"target chapter: last_completed={progress_after.get('last_completed')!r}"
            )
        if progress_after.get("next_chapter") != target + 1:
            raise G10GExecutionError(
                "the real lifecycle advanced to an unexpected next chapter: "
                f"next_chapter={progress_after.get('next_chapter')!r}"
            )
        if progress_after.get("next_chapter") != (
            progress_before.get("next_chapter", 0) + 1
        ):
            raise G10GExecutionError(
                "the real lifecycle advanced by more than one chapter"
            )

        counters = dict((runtime_state or {}).get("counters", {}))
        report = {
            "status": "COMPLETED",
            "arm_id": G10G_ARM_ID,
            "run_id": plan_value["run_id"],
            "model_id": plan_value["model_id"],
            "novel_id": plan_value["novel_id"],
            "target_chapter": target,
            "next_chapter": progress_after.get("next_chapter"),
            "last_audited_chapter": progress_after.get(
                "last_audited_chapter"
            ),
            "plan_sha256": plan_value["plan_sha256"],
            "approval_token": plan_value["approval_token"],
            "track_root": str(track_root),
            "lifecycle_invocations": 1,
            "subrequests": copy.deepcopy(
                list(counters.get("reservations", []))
            ),
            "new_provider_sends": int(counters.get("new_sends", 0)),
            "replayed_subrequests": int(counters.get("replays", 0)),
            "transport_attempts": int(
                counters.get("transport_attempts", 0)
            ),
            "provider_calls": int(counters.get("transport_attempts", 0)),
            "committed_credit_equivalent": _decimal_text(
                context.ledger.committed_credit_equivalent
            ),
            "remaining_credit_equivalent": _decimal_text(
                context.ledger.remaining_credit_equivalent
            ),
            "paid_execution_authorized": True,
            "automatic_chapter_advance": False,
            "automatic_retry": False,
        }
        ledger_path = _write_invocation_ledger(
            track_root=track_root,
            plan=plan_value,
            context=context,
            counters=counters,
            report=report,
        )
        report["invocation_ledger_path"] = str(ledger_path)
        return report


def _runtime_counter(runtime_state: Any, name: str) -> int:
    if not isinstance(runtime_state, Mapping):
        return 0
    counters = runtime_state.get("counters")
    if not isinstance(counters, Mapping):
        return 0
    value = counters.get(name, 0)
    return int(value) if isinstance(value, int) else 0


def inspect_track_journals(track_root: Path | str) -> dict[str, Any]:
    """Read one track's durable journals without mutating anything."""
    return inspect_paid_request_journals(
        Path(track_root) / "data" / "provider_request_journal"
    )


__all__ = [
    "G10G_ARM_ID",
    "G10G_EXECUTOR_SHA256",
    "G10G_INVOCATION_LEDGER_DIRECTORY",
    "G10G_OPERATION_PREFIX",
    "G10G_QUARANTINE_FILENAME",
    "G10G_REVISION",
    "G10G_SCHEMA_VERSION",
    "G10G_TRACK_EXCLUDED_FILES",
    "G10G_TRACK_EXCLUDED_PREFIXES",
    "G10GAmbiguousOutcome",
    "G10GExecutionError",
    "G10GReservationContext",
    "G10GTerminalFailure",
    "FORBIDDEN_PREREGISTERED_COUNT_KEY_MARKERS",
    "assert_no_preregistered_audit_count",
    "build_g10g_invocation",
    "build_g10g_reservation_context",
    "default_production_module_loader",
    "execute_g10g_invocation",
    "g10g_executor_identity",
    "g10g_target_state_fingerprint",
    "inspect_track_journals",
    "install_g10g_paid_runtime",
    "invocation_ledger_path",
    "resolve_track_root",
    "track_quarantine_path",
    "verify_g10g_invocation",
]