"""G10H zero-provider blind corpus freeze.

G10H is the final zero-provider corpus-preparation step of the G10 Qwen-vs-GLM
head-to-head experiment. It does not call a Provider, read a credential, score a
translation, reveal an identity, or mutate any frozen predecessor root.

G10H:

* re-verifies the frozen G10 predecessor contracts, the G10E freeze, the current
  shared credit-equivalent ledger lineage, the G10A matched-plan binding, the
  G10B S0 binding, and the completed G10F/G10G/R2 evidence trees (read-only);
* rebuilds the 51 planned private literary input rows from the *frozen* S0
  chapter libraries and the completed G10F accepted artifacts, preserving every
  planned slot, proving each source window against the accepted artifact
  evidence, and never manufacturing a missing translation;
* rebuilds the four planned longitudinal checkpoint rows from the actual durable
  G10B track state, marking a checkpoint comparable only when both model tracks
  genuinely reached it with the committed audit boundary;
* derives one private A/B identity key from a fresh in-memory 256-bit secret,
  persists it only inside the private freeze root, and records only its
  cryptographic commitment publicly;
* builds the public blind bundle with the existing frozen
  :mod:`benchmark_longitudinal_g10_blind` protocol and rejects any literal model,
  provider, or accounting identity leak.

The copyrighted source books are intentionally gitignored and absent from the
review worktree, so the frozen source text is re-materialized from the pristine
G10B S0 run chapter libraries (whose SHA-256 is bound by the frozen S0 run
identity) and proven against the completed G10F accepted artifacts.

Importing this module performs no filesystem write, credential read, network
activity, or Provider call.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g10_blind import (
    LITERARY_AVAILABLE,
    LITERARY_UNAVAILABLE,
    LONGITUDINAL_AVAILABLE,
    LONGITUDINAL_UNAVAILABLE,
    LONGITUDINAL_CHECKPOINTS,
    G10C_CONTRACT_SHA256,
    G10BlindProtocolError,
    build_private_identity_key,
    build_public_blind_bundle,
    private_identity_key_commitment,
    public_bundle_sha256,
    validate_public_blind_bundle,
)
from benchmark_longitudinal_g10_chronological import (
    G10B1_CONTRACT_SHA256,
    TRACK_BY_ID,
    TRACK_SPECS,
    classify_track_progress,
)
from benchmark_longitudinal_g10e_freeze import (
    G10E_BUDGET_LEDGER_FILENAME,
    G10E_CONTRACT_SHA256,
    G10E_FREEZE_FILENAME,
    G10E_REPOSITORY_BASELINE,
)
from benchmark_longitudinal_g10f_matched_executor import (
    G10F_EXECUTOR_SHA256,
    G10F_REVISION,
)
from benchmark_longitudinal_g10_matched import (
    G10A2_CONTRACT_SHA256,
    PAIR_SPECS,
)
from benchmark_longitudinal_g10_paid_authorization import (
    EXPECTED_FREEZE_ID,
    EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256,
    EXPECTED_MATCHED_PLAN_SHA256,
    EXPECTED_PRICING_VERSION,
    EXPECTED_S0_MANIFEST_SHA256,
    LITERARY_CASE_QUARANTINE_SCOPE,
    MODEL_RATECARD,
    ledger_genesis_lineage_sha256,
    load_paid_quarantine,
    paid_artifact_dir,
)
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    MODEL_ORDER,
    NOVELS,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_recovery_rehearsal import (
    G10D_CONTRACT_SHA256,
    tree_fingerprint,
)
from benchmark_longitudinal_g10_s0 import (
    G10S0_CONTRACT_SHA256,
    G10S0_MANIFEST_FILENAME,
    G10S0_RUN_IDENTITY_FILENAME,
)
from benchmark_longitudinal_g10_safety import G10A1_SAFETY_SHA256
from chapter_library import chapter_production_index
from noveltranslator_core import atomic_write_json


G10H_SCHEMA_VERSION = 1
G10H_REVISION = "g10-blind-corpus-freeze-v1"

PRIVATE_IDENTITY_KEY_FILENAME = "private_identity_key.json"
LITERARY_PRIVATE_FILENAME = "literary_input_private.json"
LONGITUDINAL_PRIVATE_FILENAME = "longitudinal_input_private.json"
PUBLIC_BUNDLE_FILENAME = "public_blind_bundle.json"
FREEZE_FILENAME = "g10h_freeze.json"
SUMMARY_FILENAME = "G10H_SUMMARY.txt"

LITERARY_PRIVATE_REVISION = "g10h-literary-private-input-v1"
LONGITUDINAL_PRIVATE_REVISION = "g10h-longitudinal-private-input-v1"
REFERENCE_EVIDENCE_REVISION = "g10h-frozen-source-window-v1"

G10H_ARM_ID = "G10A"
LITERARY_PLANNED = 51
LITERARY_SCOREABLE_COUNT = 39
LITERARY_UNAVAILABLE_COUNT = 12
PRIMARY_SCOREABLE = 36
BRIDGE_SCOREABLE = 3
LONGITUDINAL_PLANNED = 4
LONGITUDINAL_SCOREABLE = 0
LONGITUDINAL_UNAVAILABLE_COUNT = 4

LITERARY_QUARANTINE_REVISION = "g10-durable-quarantine-v1"
LITERARY_QUARANTINE_OUTCOMES = (
    "ambiguous_remote_outcome",
    "corrupt_or_unknown_durable_state",
    "accepted_budget_boundary_corrupt",
)
CHRONOLOGICAL_QUARANTINE_SCOPE = "chronological_model_novel_track"

R2_FINAL_FILENAME = "g10g_batch_final.json"
R2_STATE_FILENAME = "g10g_batch_state.json"
R2_RECOVERY_MANIFEST_FILENAME = "qwen_ace_ch1_recovery_manifest.json"
R2_FINAL_PROVIDER_CALLS = 171

# Exact identity/accounting literals that must never appear in the public bundle.
_EXACT_LEAK_LITERALS = (
    "qwen/qwen3.8-max-0902",
    "Qwen/Qwen3.8-Max-0902",
    "z-ai/glm-5.3-flash",
    "reasoning_effort",
    "credit_equivalent",
    "cost_usd",
    "provider_model",
    "request_sha",
    "reservation_id",
)
# Bare model tokens: only rejected outside ordinary story prose and outside the
# frozen benchmark identifier (which legitimately contains "qwen" and "glm").
_BARE_MODEL_TOKENS = ("qwen", "glm")
# Keys whose values are ordinary story prose or the frozen benchmark identity.
_BARE_TOKEN_SKIP_KEYS = frozenset(
    {"source_text", "translation", "notes", "benchmark_id"}
)


RUN_ID_BY_TRACK = {
    (track.model_id, track.novel_id): track.run_id for track in TRACK_SPECS
}


class G10HFreezeError(RuntimeError):
    """The G10H blind corpus freeze is incomplete, unsafe, or inconsistent."""


class G10HIdentityLeakError(G10HFreezeError):
    """The public blind bundle leaks a model, provider, or accounting identity."""


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10HFreezeError(
            "G10H value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G10HFreezeError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _read_json(path: Path | str, label: str) -> Any:
    target = Path(path)
    if not target.is_file():
        raise G10HFreezeError(f"{label} is missing: {target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10HFreezeError(f"{label} cannot be read: {target}") from exc


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise G10HFreezeError(f"{label} must be a lowercase SHA-256")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G10HFreezeError(f"{label} must be a JSON object")
    return dict(value)


def _model_slug(model_id: str) -> str:
    if model_id == QWEN_MODEL_ID:
        return "qwen"
    if model_id == GLM_MODEL_ID:
        return "glm"
    raise G10HFreezeError(f"unexpected G10 model identity: {model_id!r}")


def _repository_head(repo_root: Path | str) -> str:
    repo = Path(repo_root).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git unavailable
        raise G10HFreezeError(f"cannot resolve repository HEAD: {exc}") from exc
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        raise G10HFreezeError("repository HEAD cannot be resolved")
    return head


# --------------------------------------------------------------------------- #
# Frozen source evidence (pristine G10B S0 chapter libraries)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FrozenSource:
    """One re-materialized frozen literary source identity."""

    novel_id: str
    chapter: int
    source_text: str
    text_sha256: str


def _s0_run_id_for_novel(novel_id: str) -> str:
    candidates = sorted(
        (track for track in TRACK_SPECS if track.novel_id == novel_id),
        key=lambda track: track.run_index,
    )
    if not candidates:
        raise G10HFreezeError(f"no G10B S0 track for novel {novel_id!r}")
    return candidates[0].run_id


def _validate_run_identity(run_root: Path, run_id: str) -> dict[str, Any]:
    track = TRACK_BY_ID[run_id]
    identity = _require_mapping(
        _read_json(run_root / G10S0_RUN_IDENTITY_FILENAME, "G10B run identity"),
        "G10B run identity",
    )
    stored = identity.get("identity_sha256")
    _require_sha256(stored, "G10B run identity sha")
    check = {k: v for k, v in identity.items() if k != "identity_sha256"}
    if _sha256_json(check) != stored:
        raise G10HFreezeError(f"{run_id} run identity does not recompute")
    expected = {
        "run_id": run_id,
        "run_index": track.run_index,
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "relative_root": f"runs/{run_id}",
        "first_chapter": 1,
        "last_chapter": 50,
        "audit_interval": 25,
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }
    for key, wanted in expected.items():
        if identity.get(key) != wanted:
            raise G10HFreezeError(f"{run_id} run identity changed {key}")
    return identity


def load_s0_chapter_library(
    s0_root: Path | str,
    novel_id: str,
) -> dict[int, dict[str, Any]]:
    """Load the pristine, hash-bound S0 chapter library for one novel."""
    if novel_id not in NOVELS:
        raise G10HFreezeError(f"unknown G10 novel id: {novel_id!r}")
    root = Path(s0_root).expanduser().resolve()
    run_id = _s0_run_id_for_novel(novel_id)
    run_root = root / "runs" / run_id
    identity = _validate_run_identity(run_root, run_id)
    bindings = identity.get("source_bindings")
    if not isinstance(bindings, Mapping):
        raise G10HFreezeError(f"{run_id} lacks frozen source bindings")
    chapters_path = run_root / "data" / "chapters.json"
    if _sha256_file(chapters_path) != bindings.get("chapter_library_sha256"):
        raise G10HFreezeError(
            f"{novel_id} frozen S0 chapter library drifted"
        )
    raw = _read_json(chapters_path, "S0 chapter library")
    if not isinstance(raw, list):
        raise G10HFreezeError("S0 chapter library must contain a JSON array")
    library: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise G10HFreezeError("S0 chapter library entry is invalid")
        try:
            number = chapter_production_index(item)
        except ValueError as exc:
            raise G10HFreezeError(f"invalid S0 chapter entry: {exc}") from exc
        if number in library:
            raise G10HFreezeError(f"duplicate S0 Chapter {number}")
        library[number] = dict(item)
    return library


def _library_text(library: Mapping[int, Mapping[str, Any]], chapter: int, novel_id: str) -> str:
    row = library.get(chapter)
    text = row.get("text") if isinstance(row, Mapping) else None
    if not isinstance(text, str) or not text.strip():
        raise G10HFreezeError(
            f"frozen source text is missing for {novel_id} Ch{chapter}"
        )
    return text


def load_frozen_sources(
    s0_root: Path | str,
) -> dict[tuple[str, int], FrozenSource]:
    """Re-materialize the exact frozen source text for the 51 literary pairs."""
    libraries: dict[str, dict[int, dict[str, Any]]] = {}
    result: dict[tuple[str, int], FrozenSource] = {}
    for pair in PAIR_SPECS:
        if pair.novel_id not in libraries:
            libraries[pair.novel_id] = load_s0_chapter_library(
                s0_root, pair.novel_id
            )
        text = _library_text(
            libraries[pair.novel_id], pair.chapter, pair.novel_id
        )
        result[(pair.novel_id, pair.chapter)] = FrozenSource(
            novel_id=pair.novel_id,
            chapter=pair.chapter,
            source_text=text,
            text_sha256=_sha256_text(text),
        )
    if len(result) != LITERARY_PLANNED:
        raise G10HFreezeError(
            "frozen source roster must contain exactly 51 source bundles"
        )
    return result


def _audit_window(checkpoint: int) -> list[int]:
    if checkpoint == 25:
        return [1, 25]
    if checkpoint == 50:
        return [26, 50]
    raise G10HFreezeError(f"unexpected longitudinal checkpoint: {checkpoint!r}")


def build_reference_evidence(
    *,
    s0_root: Path | str,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Build model-independent, blind-safe reference evidence per checkpoint."""
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for novel_id in sorted(NOVELS):
        library = load_s0_chapter_library(s0_root, novel_id)
        for checkpoint in LONGITUDINAL_CHECKPOINTS:
            window = _audit_window(checkpoint)
            chapter_hashes = {
                str(chapter): _sha256_text(
                    _library_text(library, chapter, novel_id)
                )
                for chapter in range(window[0], window[1] + 1)
            }
            result[(novel_id, checkpoint)] = {
                "reference_revision": REFERENCE_EVIDENCE_REVISION,
                "novel_id": novel_id,
                "checkpoint": checkpoint,
                "audit_window": window,
                "chapter_source_sha256": chapter_hashes,
                "window_source_sha256": _sha256_json(chapter_hashes),
            }
    return result


# --------------------------------------------------------------------------- #
# G10F accepted artifacts and literary availability
# --------------------------------------------------------------------------- #


def _artifact_path(
    matched_root: Path | str,
    novel_id: str,
    model_id: str,
    chapter: int,
    case_kind: str,
) -> Path:
    return (
        paid_artifact_dir(matched_root, novel_id, _model_slug(model_id))
        / f"{chapter:04d}.{case_kind}.json"
    )


def _quarantine_path(
    matched_root: Path | str,
    novel_id: str,
    model_id: str,
    chapter: int,
    case_kind: str,
) -> Path:
    return (
        Path(matched_root)
        / "quarantine"
        / novel_id
        / _model_slug(model_id)
        / f"{chapter:04d}.{case_kind}.json"
    )


def _decimal_string(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise G10HFreezeError(f"{label} must be a decimal string")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise G10HFreezeError(f"{label} must be a decimal string") from exc
    if parsed < 0:
        raise G10HFreezeError(f"{label} must be non-negative")
    return value


def validate_accepted_artifact(
    artifact: Mapping[str, Any],
    *,
    pair: Any,
    model_id: str,
    source: FrozenSource,
) -> dict[str, Any]:
    """Prove one G10F accepted artifact is exactly the frozen accepted case."""
    value = _require_mapping(artifact, "G10F accepted artifact")
    checks = {
        "schema_version": 1,
        "revision": G10F_REVISION,
        "g10f_executor_sha256": G10F_EXECUTOR_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "arm_id": G10H_ARM_ID,
        "freeze_id": EXPECTED_FREEZE_ID,
        "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
        "pricing_version": EXPECTED_PRICING_VERSION,
        "pair_id": pair.pair_id,
        "pair_index": pair.pair_index,
        "case_kind": pair.case_kind,
        "novel_id": pair.novel_id,
        "chapter": pair.chapter,
        "model_id": model_id,
        "provider_model_id": MODEL_RATECARD[model_id]["provider_model_id"],
    }
    for key, expected in checks.items():
        if value.get(key) != expected:
            raise G10HFreezeError(
                f"{pair.pair_id} {model_id} accepted artifact changed {key}"
            )
    _require_sha256(value.get("plan_sha256"), "accepted artifact plan_sha256")
    _require_sha256(value.get("request_sha256"), "accepted artifact request_sha256")
    _require_sha256(
        value.get("journal_operation_id"),
        "accepted artifact journal_operation_id",
    )
    _require_sha256(
        value.get("messages_sha256"), "accepted artifact messages_sha256"
    )
    execution_index = value.get("execution_index")
    if (
        isinstance(execution_index, bool)
        or not isinstance(execution_index, int)
        or not 1 <= execution_index <= 102
    ):
        raise G10HFreezeError("accepted artifact execution_index is invalid")
    content = value.get("content")
    if not isinstance(content, str) or not content.strip():
        raise G10HFreezeError(
            f"{pair.pair_id} {model_id} accepted artifact has no content"
        )
    if value.get("provider_calls") != 1:
        raise G10HFreezeError(
            f"{pair.pair_id} {model_id} accepted artifact has no transport proof"
        )
    _decimal_string(
        value.get("accepted_credit_equivalent"),
        "accepted artifact accepted_credit_equivalent",
    )
    _decimal_string(
        value.get("maximum_credit_equivalent"),
        "accepted artifact maximum_credit_equivalent",
    )
    if not isinstance(value.get("usage"), Mapping):
        raise G10HFreezeError("accepted artifact usage identity is missing")
    if value.get("text_sha256") != source.text_sha256:
        raise G10HFreezeError(
            f"{pair.pair_id} {model_id} accepted artifact source identity drifted"
        )
    return value


def validate_literary_quarantine(
    marker: Mapping[str, Any] | None,
    *,
    pair: Any,
    model_id: str,
) -> dict[str, Any]:
    """Prove one missing literary candidate carries a valid durable quarantine."""
    if marker is None:
        raise G10HFreezeError(
            f"{pair.pair_id} {model_id} is missing without durable quarantine evidence"
        )
    value = _require_mapping(marker, "literary quarantine marker")
    if value.get("schema_version") != 1:
        raise G10HFreezeError("literary quarantine schema changed")
    if value.get("revision") != LITERARY_QUARANTINE_REVISION:
        raise G10HFreezeError("literary quarantine revision changed")
    if value.get("scope_kind") != LITERARY_CASE_QUARANTINE_SCOPE:
        raise G10HFreezeError("literary quarantine scope kind changed")
    expected_scope = (
        f"{model_id}::{pair.novel_id}::{pair.chapter:04d}::{pair.case_kind}"
    )
    if value.get("scope_id") != expected_scope:
        raise G10HFreezeError("literary quarantine scope identity changed")
    if value.get("outcome") not in LITERARY_QUARANTINE_OUTCOMES:
        raise G10HFreezeError("literary quarantine outcome is invalid")
    _require_sha256(value.get("request_sha256"), "literary quarantine request_sha256")
    _require_sha256(
        value.get("reservation_id"), "literary quarantine reservation_id"
    )
    return value


def build_literary_records(
    *,
    matched_root: Path | str,
    sources: Mapping[tuple[str, int], FrozenSource],
) -> dict[str, Any]:
    """Build the 51 private literary input rows from frozen accepted artifacts."""
    matched = Path(matched_root).expanduser().resolve()
    if not matched.is_dir():
        raise G10HFreezeError(f"matched evidence root is missing: {matched}")

    records: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    available_count = 0
    primary_available = 0
    bridge_available = 0

    for pair in PAIR_SPECS:
        source = sources.get((pair.novel_id, pair.chapter))
        if source is None:
            raise G10HFreezeError(f"frozen source is missing for {pair.pair_id}")
        translations: dict[str, str] = {}
        case_evidence: dict[str, Any] = {}
        accepted_models: list[str] = []
        missing_models: list[str] = []
        for model_id in MODEL_ORDER:
            artifact_path = _artifact_path(
                matched, pair.novel_id, model_id, pair.chapter, pair.case_kind
            )
            if artifact_path.is_file():
                artifact = validate_accepted_artifact(
                    _read_json(artifact_path, "accepted artifact"),
                    pair=pair,
                    model_id=model_id,
                    source=source,
                )
                translations[model_id] = artifact["content"]
                accepted_models.append(model_id)
                case_evidence[model_id] = {
                    "outcome": "accepted",
                    "execution_index": artifact["execution_index"],
                    "artifact_file_sha256": _sha256_file(artifact_path),
                }
            else:
                marker = load_paid_quarantine(
                    _quarantine_path(
                        matched, pair.novel_id, model_id, pair.chapter, pair.case_kind
                    )
                )
                validate_literary_quarantine(marker, pair=pair, model_id=model_id)
                missing_models.append(model_id)
                case_evidence[model_id] = {
                    "outcome": "unavailable_model_case",
                    "quarantine_outcome": marker["outcome"],
                    "quarantine_request_sha256": marker["request_sha256"],
                }
        if not accepted_models:
            raise G10HFreezeError(
                f"{pair.pair_id} has no accepted artifact to prove source identity"
            )

        available = len(accepted_models) == len(MODEL_ORDER)
        if available:
            availability = LITERARY_AVAILABLE
            available_count += 1
            if pair.case_kind == "primary":
                primary_available += 1
            else:
                bridge_available += 1
        else:
            availability = LITERARY_UNAVAILABLE

        records.append(
            {
                "pair_id": pair.pair_id,
                "pair_index": pair.pair_index,
                "case_kind": pair.case_kind,
                "novel_id": pair.novel_id,
                "chapter": pair.chapter,
                "source_text": source.source_text,
                "source_text_sha256": source.text_sha256,
                "availability": availability,
                "translations": translations,
            }
        )
        evidence[pair.pair_id] = {
            "accepted_models": accepted_models,
            "missing_models": missing_models,
            "case_evidence": case_evidence,
        }

    counts = {
        "literary_planned": len(records),
        "literary_scoreable": available_count,
        "literary_unavailable": LITERARY_PLANNED - available_count,
        "primary_scoreable": primary_available,
        "bridge_scoreable": bridge_available,
    }
    expected = {
        "literary_planned": LITERARY_PLANNED,
        "literary_scoreable": LITERARY_SCOREABLE_COUNT,
        "literary_unavailable": LITERARY_UNAVAILABLE_COUNT,
        "primary_scoreable": PRIMARY_SCOREABLE,
        "bridge_scoreable": BRIDGE_SCOREABLE,
    }
    if counts != expected:
        raise G10HFreezeError(
            "G10H literary evidence does not prove the frozen corpus counts: "
            f"{counts} != {expected}"
        )

    document = {
        "schema_version": G10H_SCHEMA_VERSION,
        "revision": LITERARY_PRIVATE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "pair_count": LITERARY_PLANNED,
        "records": records,
        "counts": counts,
        "case_evidence": evidence,
        "provider_calls": 0,
    }
    return {"records": records, "counts": counts, "document": document}


# --------------------------------------------------------------------------- #
# Longitudinal checkpoint availability
# --------------------------------------------------------------------------- #


def _cross_check_completed_track(
    *,
    run_id: str,
    progress: Mapping[str, Any],
    quarantine: Mapping[str, Any] | None,
    r2_track: Mapping[str, Any],
) -> None:
    """Bind durable track progress to the frozen completed R2 record."""
    for field in ("last_completed", "next_chapter", "last_audited_chapter"):
        if progress.get(field) != r2_track.get(field):
            raise G10HFreezeError(
                f"{run_id} durable progress changed {field} against R2"
            )
    r2_quarantine = r2_track.get("quarantine")
    if (quarantine is None) != (r2_quarantine is None):
        raise G10HFreezeError(f"{run_id} quarantine presence changed against R2")
    if quarantine is not None:
        if not isinstance(r2_quarantine, Mapping):
            raise G10HFreezeError(f"{run_id} R2 quarantine record is invalid")
        if quarantine.get("scope_kind") != CHRONOLOGICAL_QUARANTINE_SCOPE:
            raise G10HFreezeError(f"{run_id} quarantine scope kind changed")
        if quarantine.get("scope_id") != run_id:
            raise G10HFreezeError(f"{run_id} quarantine scope id changed")
        if quarantine.get("first_blocked_chapter") != r2_quarantine.get(
            "first_blocked_chapter"
        ):
            raise G10HFreezeError(f"{run_id} quarantine chapter changed against R2")


def _checkpoint_reached(classification: Mapping[str, Any], checkpoint: int) -> bool:
    if classification.get("status") == "corrupt_block":
        return False
    last_completed = classification.get("last_completed")
    last_audited = classification.get("last_audited_chapter")
    if not isinstance(last_completed, int) or not isinstance(last_audited, int):
        return False
    return last_completed >= checkpoint and last_audited >= checkpoint


def _track_state_evidence(run_root: Path, classification: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "last_completed": classification.get("last_completed"),
        "last_audited_chapter": classification.get("last_audited_chapter"),
        "track_state_fingerprint_sha256": _sha256_json(
            {
                "last_completed": classification.get("last_completed"),
                "last_audited_chapter": classification.get("last_audited_chapter"),
                "progress_sha256": _sha256_file(run_root / "data" / "progress.json"),
            }
        ),
    }


def build_longitudinal_rows(
    *,
    chronological_root: Path | str,
    s0_root: Path | str,
    reference_evidence: Mapping[tuple[str, int], Mapping[str, Any]],
    r2_tracks: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Derive the four longitudinal rows and their raw availability counts."""
    root = Path(chronological_root).expanduser().resolve()
    if not root.is_dir():
        raise G10HFreezeError(f"chronological evidence root is missing: {root}")

    records: list[dict[str, Any]] = []
    available_count = 0
    for novel_id in sorted(NOVELS):
        for checkpoint in LONGITUDINAL_CHECKPOINTS:
            reference = reference_evidence.get((novel_id, checkpoint))
            if not isinstance(reference, Mapping):
                raise G10HFreezeError(
                    f"reference evidence is missing for {novel_id} Ch{checkpoint}"
                )
            tracks: dict[str, dict[str, Any]] = {}
            reached: dict[str, bool] = {}
            for model_id in MODEL_ORDER:
                run_id = RUN_ID_BY_TRACK[(model_id, novel_id)]
                run_root = root / "runs" / run_id
                if not run_root.is_dir():
                    raise G10HFreezeError(f"G10B run root is missing: {run_root}")
                _validate_run_identity(run_root, run_id)
                progress = _require_mapping(
                    _read_json(run_root / "data" / "progress.json", "track progress"),
                    "track progress",
                )
                quarantine = load_paid_quarantine(
                    run_root / "data" / "g10_quarantine.json"
                )
                r2_track = r2_tracks.get(run_id)
                if not isinstance(r2_track, Mapping):
                    raise G10HFreezeError(
                        f"{run_id} is absent from the frozen R2 completion record"
                    )
                _cross_check_completed_track(
                    run_id=run_id,
                    progress=progress,
                    quarantine=quarantine,
                    r2_track=r2_track,
                )
                classification = classify_track_progress(
                    progress,
                    quarantine=quarantine,
                    expected_run_id=run_id,
                )
                reached[model_id] = _checkpoint_reached(classification, checkpoint)
                tracks[model_id] = _track_state_evidence(run_root, classification)

            comparable = all(reached.values())
            if comparable:
                available_count += 1
                availability = LONGITUDINAL_AVAILABLE
            else:
                availability = LONGITUDINAL_UNAVAILABLE
                tracks = {}
            records.append(
                {
                    "novel_id": novel_id,
                    "checkpoint": checkpoint,
                    "audit_window": _audit_window(checkpoint),
                    "reference_evidence": dict(reference),
                    "availability": availability,
                    "tracks": tracks,
                }
            )

    counts = {
        "longitudinal_planned": len(records),
        "longitudinal_scoreable": available_count,
        "longitudinal_unavailable": LONGITUDINAL_PLANNED - available_count,
    }
    return records, counts


def build_longitudinal_records(
    *,
    chronological_root: Path | str,
    s0_root: Path | str,
    reference_evidence: Mapping[tuple[str, int], Mapping[str, Any]],
    r2_tracks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the four private longitudinal rows and fail closed on the outcome."""
    records, counts = build_longitudinal_rows(
        chronological_root=chronological_root,
        s0_root=s0_root,
        reference_evidence=reference_evidence,
        r2_tracks=r2_tracks,
    )
    expected = {
        "longitudinal_planned": LONGITUDINAL_PLANNED,
        "longitudinal_scoreable": LONGITUDINAL_SCOREABLE,
        "longitudinal_unavailable": LONGITUDINAL_UNAVAILABLE_COUNT,
    }
    if counts != expected:
        raise G10HFreezeError(
            "G10H longitudinal evidence does not prove the frozen outcome: "
            f"{counts} != {expected}"
        )

    document = {
        "schema_version": G10H_SCHEMA_VERSION,
        "revision": LONGITUDINAL_PRIVATE_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "checkpoint_count": LONGITUDINAL_PLANNED,
        "records": records,
        "counts": counts,
        "provider_calls": 0,
    }
    return {"records": records, "counts": counts, "document": document}


# --------------------------------------------------------------------------- #
# Public identity-leak scan
# --------------------------------------------------------------------------- #


def _non_prose_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _BARE_TOKEN_SKIP_KEYS:
                continue
            found.append(str(key))
            found.extend(_non_prose_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_non_prose_strings(item))
    elif isinstance(value, str):
        found.append(value)
    return found


def assert_no_public_identity_leak(bundle: Mapping[str, Any]) -> None:
    """Reject any literal model/provider/accounting identity in the public bundle."""
    canonical = _canonical_json(bundle).casefold()
    for literal in _EXACT_LEAK_LITERALS:
        if literal.casefold() in canonical:
            raise G10HIdentityLeakError(
                f"public blind bundle leaks the literal identity {literal!r}"
            )
    for text in _non_prose_strings(bundle):
        if text == G10_BENCHMARK_ID:
            continue
        folded = text.casefold()
        for token in _BARE_MODEL_TOKENS:
            if token in folded:
                raise G10HIdentityLeakError(
                    f"public blind bundle leaks the model token {token!r}"
                )


# --------------------------------------------------------------------------- #
# Predecessor re-verification (read-only)
# --------------------------------------------------------------------------- #


def _g10e_freeze_document(g10e_root: Path | str) -> dict[str, Any]:
    path = Path(g10e_root) / G10E_FREEZE_FILENAME
    document = _require_mapping(_read_json(path, "G10E freeze"), "G10E freeze")
    freeze_id = document.get("freeze_id")
    payload = {k: v for k, v in document.items() if k != "freeze_id"}
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G10HFreezeError("G10E freeze ID does not recompute")
    if document.get("g10e_contract_sha256") != G10E_CONTRACT_SHA256:
        raise G10HFreezeError("G10E contract binding changed")
    if document.get("provider_calls") != 0:
        raise G10HFreezeError("G10E unexpectedly reports Provider calls")
    return document


def _s0_binding(s0_root: Path | str, chronological_root: Path | str) -> dict[str, Any]:
    s0_manifest_path = Path(s0_root) / G10S0_MANIFEST_FILENAME
    exec_manifest_path = Path(chronological_root) / G10S0_MANIFEST_FILENAME
    s0_manifest = _require_mapping(
        _read_json(s0_manifest_path, "G10B S0 manifest"), "G10B S0 manifest"
    )
    exec_manifest = _require_mapping(
        _read_json(exec_manifest_path, "G10B execution manifest"),
        "G10B execution manifest",
    )
    if s0_manifest != exec_manifest:
        raise G10HFreezeError(
            "G10B execution manifest differs from the frozen S0 manifest"
        )
    stored = s0_manifest.get("manifest_sha256")
    _require_sha256(stored, "G10B S0 manifest sha")
    check = {k: v for k, v in s0_manifest.items() if k != "manifest_sha256"}
    if _sha256_json(check) != stored:
        raise G10HFreezeError("G10B S0 manifest does not recompute")
    if stored != EXPECTED_S0_MANIFEST_SHA256:
        raise G10HFreezeError("G10B S0 manifest binding changed")
    runs = s0_manifest.get("runs")
    if not isinstance(runs, list) or len(runs) != 4:
        raise G10HFreezeError("G10B S0 manifest must contain four runs")
    for row in runs:
        if not isinstance(row, Mapping):
            raise G10HFreezeError("G10B S0 run entry is invalid")
        run_id = row.get("run_id")
        if run_id not in TRACK_BY_ID:
            raise G10HFreezeError(f"G10B S0 run id is invalid: {run_id!r}")
        identity = _validate_run_identity(
            Path(s0_root) / "runs" / run_id, run_id
        )
        if identity.get("identity_sha256") != row.get("run_identity_sha256"):
            raise G10HFreezeError(f"{run_id} S0 run identity binding changed")
    return {
        "manifest_file_sha256": _sha256_file(s0_manifest_path),
        "manifest_sha256": stored,
        "run_count": len(runs),
    }


def _shared_ledger_identity(g10e_root: Path | str, freeze_id: str) -> dict[str, Any]:
    path = Path(g10e_root) / G10E_BUDGET_LEDGER_FILENAME
    document = _require_mapping(
        _read_json(path, "G10 budget ledger"), "G10 budget ledger"
    )
    if document.get("authorization_sha256") != freeze_id:
        raise G10HFreezeError("G10 budget ledger binds another authorization")
    genesis = ledger_genesis_lineage_sha256(document)
    if genesis != EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256:
        raise G10HFreezeError("G10 budget ledger genesis lineage changed")
    records = document.get("records")
    if not isinstance(records, list):
        raise G10HFreezeError("G10 budget ledger records are invalid")
    return {
        "file_sha256": _sha256_file(path),
        "genesis_lineage_sha256": genesis,
        "authorization_sha256": freeze_id,
        "record_count": len(records),
    }


def _r2_control_identity(control_root: Path | str) -> dict[str, Any]:
    root = Path(control_root).expanduser().resolve()
    final_path = root / R2_FINAL_FILENAME
    state_path = root / R2_STATE_FILENAME
    recovery_path = root / R2_RECOVERY_MANIFEST_FILENAME
    final = _require_mapping(_read_json(final_path, "R2 final"), "R2 final")
    _require_mapping(_read_json(state_path, "R2 state"), "R2 state")
    recovery = _require_mapping(
        _read_json(recovery_path, "R2 recovery manifest"), "R2 recovery manifest"
    )
    if recovery.get("budget_genesis_lineage_sha256") != (
        EXPECTED_LEDGER_GENESIS_LINEAGE_SHA256
    ):
        raise G10HFreezeError("R2 recovery manifest ledger lineage changed")
    if final.get("provider_calls") != R2_FINAL_PROVIDER_CALLS:
        raise G10HFreezeError("R2 final provider-call record changed")
    tracks = final.get("tracks")
    if not isinstance(tracks, Mapping) or set(tracks) != set(TRACK_BY_ID):
        raise G10HFreezeError("R2 final track roster changed")
    return {
        "final_sha256": _sha256_file(final_path),
        "state_sha256": _sha256_file(state_path),
        "recovery_manifest_sha256": _sha256_file(recovery_path),
        "recovery_budget_live_sha256": recovery.get("budget_live_sha256"),
        "tracks": {str(k): dict(v) for k, v in tracks.items()},
    }


def verify_predecessors(
    *,
    repo_root: Path | str,
    g10e_root: Path | str,
    matched_root: Path | str,
    chronological_root: Path | str,
    s0_root: Path | str,
    g10g_control_root: Path | str,
) -> dict[str, Any]:
    """Re-verify every frozen predecessor identity without writing anything."""
    repo = Path(repo_root).expanduser().resolve()
    matched = Path(matched_root).expanduser().resolve()
    chronological = Path(chronological_root).expanduser().resolve()
    control = Path(g10g_control_root).expanduser().resolve()
    for label, path in {
        "repository": repo,
        "matched": matched,
        "chronological": chronological,
        "control": control,
    }.items():
        if not path.is_dir():
            raise G10HFreezeError(f"{label} root is missing: {path}")

    freeze = _g10e_freeze_document(g10e_root)
    contracts = {
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "g10a2_matched_sha256": G10A2_CONTRACT_SHA256,
        "g10b1_chronological_sha256": G10B1_CONTRACT_SHA256,
        "g10c_blind_sha256": G10C_CONTRACT_SHA256,
        "g10d_recovery_sha256": G10D_CONTRACT_SHA256,
        "g10s0_contract_sha256": G10S0_CONTRACT_SHA256,
    }
    if freeze.get("predecessor_contracts") != contracts:
        raise G10HFreezeError("G10E predecessor contract bindings changed")

    matched_plan_sha = freeze["matched"]["matched_plan_sha256"]
    if matched_plan_sha != EXPECTED_MATCHED_PLAN_SHA256:
        raise G10HFreezeError("G10A matched plan binding changed")
    if freeze["matched"]["pair_count"] != LITERARY_PLANNED:
        raise G10HFreezeError("G10A matched plan pair/count binding changed")

    s0 = _s0_binding(s0_root, chronological)
    if freeze["chronological"]["s0_manifest_sha256"] != s0["manifest_sha256"]:
        raise G10HFreezeError("G10E S0 manifest binding changed")

    ledger = _shared_ledger_identity(g10e_root, freeze_id=freeze["freeze_id"])
    r2 = _r2_control_identity(control)

    return {
        "repository_head": _repository_head(repo),
        "repository_baseline": G10E_REPOSITORY_BASELINE,
        "contracts": contracts,
        "g10e_freeze": {
            "file_sha256": _sha256_file(Path(g10e_root) / G10E_FREEZE_FILENAME),
            "freeze_id": freeze["freeze_id"],
            "contract_sha256": G10E_CONTRACT_SHA256,
            "matched_plan_sha256": matched_plan_sha,
            "s0_manifest_sha256": freeze["chronological"]["s0_manifest_sha256"],
        },
        "matched_plan_sha256": matched_plan_sha,
        "shared_ledger": ledger,
        "s0": s0,
        "r2": r2,
        "evidence_roots": {
            "matched": {"path": str(matched), "tree": tree_fingerprint(matched)},
            "chronological": {
                "path": str(chronological),
                "tree": tree_fingerprint(chronological),
            },
            "control": {"path": str(control), "tree": tree_fingerprint(control)},
        },
        "provider_calls": 0,
        "credential_access": False,
    }


# --------------------------------------------------------------------------- #
# Freeze document construction
# --------------------------------------------------------------------------- #


def _freeze_payload(
    *,
    predecessors: Mapping[str, Any],
    literary: Mapping[str, Any],
    longitudinal: Mapping[str, Any],
    public_bundle: Mapping[str, Any],
    private_key: Mapping[str, Any],
    freeze_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": G10H_SCHEMA_VERSION,
        "revision": G10H_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "repository": {
            "head": predecessors["repository_head"],
            "baseline": predecessors["repository_baseline"],
        },
        "contracts": dict(predecessors["contracts"]),
        "g10e_freeze": dict(predecessors["g10e_freeze"]),
        "matched_plan_sha256": predecessors["matched_plan_sha256"],
        "shared_ledger": dict(predecessors["shared_ledger"]),
        "s0": dict(predecessors["s0"]),
        "evidence_roots": dict(predecessors["evidence_roots"]),
        "r2_control": {
            "final_sha256": predecessors["r2"]["final_sha256"],
            "state_sha256": predecessors["r2"]["state_sha256"],
            "recovery_manifest_sha256": predecessors["r2"][
                "recovery_manifest_sha256"
            ],
            "recovery_budget_live_sha256": predecessors["r2"][
                "recovery_budget_live_sha256"
            ],
        },
        "private_key": {
            "file": PRIVATE_IDENTITY_KEY_FILENAME,
            "file_sha256": _sha256_file(freeze_root / PRIVATE_IDENTITY_KEY_FILENAME),
            "commitment_sha256": private_identity_key_commitment(private_key),
        },
        "literary_private_input": {
            "file": LITERARY_PRIVATE_FILENAME,
            "file_sha256": _sha256_file(freeze_root / LITERARY_PRIVATE_FILENAME),
            "document_sha256": _sha256_json(literary["document"]),
        },
        "longitudinal_private_input": {
            "file": LONGITUDINAL_PRIVATE_FILENAME,
            "file_sha256": _sha256_file(freeze_root / LONGITUDINAL_PRIVATE_FILENAME),
            "document_sha256": _sha256_json(longitudinal["document"]),
        },
        "public_bundle": {
            "file": PUBLIC_BUNDLE_FILENAME,
            "file_sha256": _sha256_file(freeze_root / PUBLIC_BUNDLE_FILENAME),
            "sha256": public_bundle_sha256(public_bundle),
        },
        "counts": {
            "literary_planned": literary["counts"]["literary_planned"],
            "literary_scoreable": literary["counts"]["literary_scoreable"],
            "literary_unavailable": literary["counts"]["literary_unavailable"],
            "primary_scoreable": literary["counts"]["primary_scoreable"],
            "bridge_scoreable": literary["counts"]["bridge_scoreable"],
            "longitudinal_planned": longitudinal["counts"]["longitudinal_planned"],
            "longitudinal_scoreable": longitudinal["counts"][
                "longitudinal_scoreable"
            ],
            "longitudinal_unavailable": longitudinal["counts"][
                "longitudinal_unavailable"
            ],
        },
        "identity_revealed": False,
        "scoring_performed": False,
        "provider_calls": 0,
        "credential_access": False,
        "paid_execution_authorized": False,
    }


def _summary_text(document: Mapping[str, Any]) -> str:
    counts = document["counts"]
    return (
        "G10H BLIND CORPUS FREEZE\n"
        f"freeze_id: {document['freeze_id']}\n"
        f"benchmark_id: {document['benchmark_id']}\n"
        f"literary planned {counts['literary_planned']} "
        f"scoreable {counts['literary_scoreable']} "
        f"unavailable {counts['literary_unavailable']} "
        f"(primary {counts['primary_scoreable']}, bridge {counts['bridge_scoreable']})\n"
        f"longitudinal planned {counts['longitudinal_planned']} "
        f"scoreable {counts['longitudinal_scoreable']} "
        f"unavailable {counts['longitudinal_unavailable']}\n"
        "identity_revealed false\n"
        "scoring_performed false\n"
        "provider_calls 0\n"
    )


def _freeze_document(
    *,
    predecessors: Mapping[str, Any],
    literary: Mapping[str, Any],
    longitudinal: Mapping[str, Any],
    public_bundle: Mapping[str, Any],
    private_key: Mapping[str, Any],
    freeze_root: Path,
) -> dict[str, Any]:
    payload = _freeze_payload(
        predecessors=predecessors,
        literary=literary,
        longitudinal=longitudinal,
        public_bundle=public_bundle,
        private_key=private_key,
        freeze_root=freeze_root,
    )
    return {**payload, "freeze_id": _sha256_json(payload)}


# --------------------------------------------------------------------------- #
# create / verify
# --------------------------------------------------------------------------- #


def _build_public_bundle(
    private_key: Mapping[str, Any],
    literary: Mapping[str, Any],
    longitudinal: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        bundle = build_public_blind_bundle(
            private_key=private_key,
            literary_records=literary["records"],
            longitudinal_records=longitudinal["records"],
        )
    except G10BlindProtocolError as exc:
        raise G10HFreezeError(f"public blind bundle is invalid: {exc}") from exc
    validate_public_blind_bundle(bundle)
    assert_no_public_identity_leak(bundle)
    return bundle


def _assert_output_root_available(output_root: Path) -> None:
    if output_root.exists():
        if not output_root.is_dir():
            raise G10HFreezeError("G10H output root is not a directory")
        if any(output_root.iterdir()):
            raise G10HFreezeError(
                "G10H freeze root already exists and is not empty"
            )


def create_freeze(
    *,
    repo_root: Path | str,
    g10e_root: Path | str,
    matched_root: Path | str,
    chronological_root: Path | str,
    s0_root: Path | str,
    g10g_control_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Create the immutable G10H blind corpus freeze once."""
    out = Path(output_root).expanduser()
    _assert_output_root_available(out)

    predecessors = verify_predecessors(
        repo_root=repo_root,
        g10e_root=g10e_root,
        matched_root=matched_root,
        chronological_root=chronological_root,
        s0_root=s0_root,
        g10g_control_root=g10g_control_root,
    )
    sources = load_frozen_sources(s0_root)
    literary = build_literary_records(matched_root=matched_root, sources=sources)
    reference = build_reference_evidence(s0_root=s0_root)
    longitudinal = build_longitudinal_records(
        chronological_root=chronological_root,
        s0_root=s0_root,
        reference_evidence=reference,
        r2_tracks=predecessors["r2"]["tracks"],
    )

    secret_hex = secrets.token_hex(32)
    try:
        private_key = build_private_identity_key(secret_hex)
    finally:
        del secret_hex
    public_bundle = _build_public_bundle(private_key, literary, longitudinal)
    commitment = private_identity_key_commitment(private_key)
    if public_bundle["private_identity_key_commitment_sha256"] != commitment:
        raise G10HFreezeError("public bundle private-key commitment changed")

    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out / PRIVATE_IDENTITY_KEY_FILENAME, private_key)
    atomic_write_json(out / LITERARY_PRIVATE_FILENAME, literary["document"])
    atomic_write_json(out / LONGITUDINAL_PRIVATE_FILENAME, longitudinal["document"])
    atomic_write_json(out / PUBLIC_BUNDLE_FILENAME, public_bundle)
    document = _freeze_document(
        predecessors=predecessors,
        literary=literary,
        longitudinal=longitudinal,
        public_bundle=public_bundle,
        private_key=private_key,
        freeze_root=out,
    )
    atomic_write_json(out / FREEZE_FILENAME, document)
    (out / SUMMARY_FILENAME).write_text(_summary_text(document), encoding="utf-8")
    return _freeze_result(out, document)


def verify_freeze(
    *,
    repo_root: Path | str,
    g10e_root: Path | str,
    matched_root: Path | str,
    chronological_root: Path | str,
    s0_root: Path | str,
    g10g_control_root: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Recompute every important G10H identity read-only."""
    out = Path(output_root).expanduser().resolve()
    document = _require_mapping(
        _read_json(out / FREEZE_FILENAME, "G10H freeze"), "G10H freeze"
    )
    freeze_id = document.get("freeze_id")
    payload = {k: v for k, v in document.items() if k != "freeze_id"}
    if not isinstance(freeze_id, str) or _sha256_json(payload) != freeze_id:
        raise G10HFreezeError("G10H freeze ID does not recompute")
    if document.get("schema_version") != G10H_SCHEMA_VERSION:
        raise G10HFreezeError("G10H freeze schema changed")
    if document.get("provider_calls") != 0:
        raise G10HFreezeError("G10H freeze unexpectedly reports Provider calls")
    if document.get("identity_revealed") is not False:
        raise G10HFreezeError("G10H freeze unexpectedly reveals identity")
    if document.get("scoring_performed") is not False:
        raise G10HFreezeError("G10H freeze unexpectedly reports scoring")

    predecessors = verify_predecessors(
        repo_root=repo_root,
        g10e_root=g10e_root,
        matched_root=matched_root,
        chronological_root=chronological_root,
        s0_root=s0_root,
        g10g_control_root=g10g_control_root,
    )
    if document.get("repository") != {
        "head": predecessors["repository_head"],
        "baseline": predecessors["repository_baseline"],
    }:
        raise G10HFreezeError("G10H repository binding changed")
    if document.get("contracts") != predecessors["contracts"]:
        raise G10HFreezeError("G10H contract bindings changed")
    if document.get("g10e_freeze") != predecessors["g10e_freeze"]:
        raise G10HFreezeError("G10H G10E freeze binding changed")
    if document.get("shared_ledger") != predecessors["shared_ledger"]:
        raise G10HFreezeError("G10H shared ledger binding changed")
    if document.get("s0") != predecessors["s0"]:
        raise G10HFreezeError("G10H S0 binding changed")
    if document.get("evidence_roots") != predecessors["evidence_roots"]:
        raise G10HFreezeError("G10H evidence tree binding changed")
    if document.get("matched_plan_sha256") != predecessors["matched_plan_sha256"]:
        raise G10HFreezeError("G10H matched plan binding changed")

    sources = load_frozen_sources(s0_root)
    literary = build_literary_records(matched_root=matched_root, sources=sources)
    reference = build_reference_evidence(s0_root=s0_root)
    longitudinal = build_longitudinal_records(
        chronological_root=chronological_root,
        s0_root=s0_root,
        reference_evidence=reference,
        r2_tracks=predecessors["r2"]["tracks"],
    )

    private_key = _require_mapping(
        _read_json(out / PRIVATE_IDENTITY_KEY_FILENAME, "G10H private key"),
        "G10H private key",
    )
    commitment = private_identity_key_commitment(private_key)
    if document["private_key"]["commitment_sha256"] != commitment:
        raise G10HFreezeError("G10H private-key commitment changed")
    if document["private_key"]["file_sha256"] != _sha256_file(
        out / PRIVATE_IDENTITY_KEY_FILENAME
    ):
        raise G10HFreezeError("G10H private-key file changed")
    if document["literary_private_input"]["document_sha256"] != _sha256_json(
        literary["document"]
    ):
        raise G10HFreezeError("G10H literary private input drifted")
    if document["literary_private_input"]["file_sha256"] != _sha256_file(
        out / LITERARY_PRIVATE_FILENAME
    ):
        raise G10HFreezeError("G10H literary private-input file changed")
    if document["longitudinal_private_input"]["document_sha256"] != _sha256_json(
        longitudinal["document"]
    ):
        raise G10HFreezeError("G10H longitudinal private input drifted")
    if document["longitudinal_private_input"]["file_sha256"] != _sha256_file(
        out / LONGITUDINAL_PRIVATE_FILENAME
    ):
        raise G10HFreezeError("G10H longitudinal private-input file changed")

    public_bundle = _build_public_bundle(private_key, literary, longitudinal)
    if commitment != public_bundle["private_identity_key_commitment_sha256"]:
        raise G10HFreezeError("G10H public bundle commitment changed")
    if document["public_bundle"]["sha256"] != public_bundle_sha256(public_bundle):
        raise G10HFreezeError("G10H public bundle identity drifted")
    if document["public_bundle"]["file_sha256"] != _sha256_file(
        out / PUBLIC_BUNDLE_FILENAME
    ):
        raise G10HFreezeError("G10H public bundle file changed")
    stored_bundle = _require_mapping(
        _read_json(out / PUBLIC_BUNDLE_FILENAME, "G10H public bundle"),
        "G10H public bundle",
    )
    if _sha256_json(stored_bundle) != _sha256_json(public_bundle):
        raise G10HFreezeError("G10H stored public bundle differs from evidence")

    expected_counts = {
        "literary_planned": LITERARY_PLANNED,
        "literary_scoreable": LITERARY_SCOREABLE_COUNT,
        "literary_unavailable": LITERARY_UNAVAILABLE_COUNT,
        "primary_scoreable": PRIMARY_SCOREABLE,
        "bridge_scoreable": BRIDGE_SCOREABLE,
        "longitudinal_planned": LONGITUDINAL_PLANNED,
        "longitudinal_scoreable": LONGITUDINAL_SCOREABLE,
        "longitudinal_unavailable": LONGITUDINAL_UNAVAILABLE_COUNT,
    }
    if document["counts"] != expected_counts:
        raise G10HFreezeError("G10H frozen counts changed")

    return _freeze_result(out, document, commitment=commitment)


def _freeze_result(
    out: Path,
    document: Mapping[str, Any],
    *,
    commitment: str | None = None,
) -> dict[str, Any]:
    counts = document["counts"]
    return {
        "freeze_id": document["freeze_id"],
        "output_root": str(out),
        "literary_planned": counts["literary_planned"],
        "literary_scoreable": counts["literary_scoreable"],
        "literary_unavailable": counts["literary_unavailable"],
        "primary_scoreable": counts["primary_scoreable"],
        "bridge_scoreable": counts["bridge_scoreable"],
        "longitudinal_planned": counts["longitudinal_planned"],
        "longitudinal_scoreable": counts["longitudinal_scoreable"],
        "longitudinal_unavailable": counts["longitudinal_unavailable"],
        "identity_revealed": False,
        "scoring_performed": False,
        "provider_calls": 0,
        "credential_access": False,
        "public_bundle_sha256": document["public_bundle"]["sha256"],
        "public_bundle_file_sha256": document["public_bundle"]["file_sha256"],
        "private_key_commitment_sha256": (
            commitment
            if commitment is not None
            else document["private_key"]["commitment_sha256"]
        ),
    }


def assert_paid_execution_blocked() -> None:
    if G10H_ARM_ID != "G10A":
        raise G10HFreezeError("G10H arm identity changed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/verify the zero-provider G10H blind corpus freeze."
    )
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--g10e-root", required=True)
    parser.add_argument("--matched-root", required=True)
    parser.add_argument("--chronological-root", required=True)
    parser.add_argument("--s0-root", required=True)
    parser.add_argument("--g10g-control-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {
        "repo_root": args.repo_root,
        "g10e_root": args.g10e_root,
        "matched_root": args.matched_root,
        "chronological_root": args.chronological_root,
        "s0_root": args.s0_root,
        "g10g_control_root": args.g10g_control_root,
        "output_root": args.output_root,
    }
    if args.action == "create":
        result = create_freeze(**kwargs)
    else:
        result = verify_freeze(**kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FREEZE_FILENAME",
    "G10HFreezeError",
    "G10HIdentityLeakError",
    "G10H_REVISION",
    "G10H_SCHEMA_VERSION",
    "LITERARY_PRIVATE_FILENAME",
    "LONGITUDINAL_PRIVATE_FILENAME",
    "PRIVATE_IDENTITY_KEY_FILENAME",
    "PUBLIC_BUNDLE_FILENAME",
    "SUMMARY_FILENAME",
    "assert_no_public_identity_leak",
    "assert_paid_execution_blocked",
    "build_literary_records",
    "build_longitudinal_records",
    "build_longitudinal_rows",
    "build_reference_evidence",
    "create_freeze",
    "load_frozen_sources",
    "load_s0_chapter_library",
    "validate_accepted_artifact",
    "validate_literary_quarantine",
    "verify_freeze",
    "verify_predecessors",
]
