"""G7S durable Stage-1 quarantine marker.

G7R already classifies a post-paid corrupt/unknown durable state as
``RUN_OUTCOME_CORRUPT_STATE`` (a quarantined subtype) at execution time, but that
classification lives only in the in-memory wave result.  After the process exits,
``derive_run_disposition()`` reconstructs status from durable evidence alone, and
the surviving artifacts of a corrupt paid boundary can look indistinguishable from
a healthy frontier.  A later ``plan_wave()`` could therefore select a run that the
wave already proved must never be retried.

G7S closes that gap with one explicit, durable, fail-closed per-run marker.  It is
deliberately *not* a fabricated paid-request journal: it is a separate immutable
provenance record that binds the exact run/failure identity and forbids the run
from ever being selected again.

Semantics:

* ``first_blocked_chapter = N`` means the target chapter ``N`` and every later
  target must not be ``ready_for_target``; because chronology is broken at ``N``
  the run cannot advance to ``N + 1``.  Targets below ``N`` keep their historical
  classification.
* A malformed, unreadable, identity-mismatched, hash-invalid, or conflicting
  marker fails closed as corrupt evidence, never ``ready_for_target``.
* Creation requires the blocked chapter's canonical G7F invocation ledger, so a
  quarantine is never recorded without real paid-boundary evidence.
* The observing G7R controller digest is explicit historical operator
  provenance; it is never derived from the current checkout.
* Publication is atomic and create-once: the marker is fsynced to a private temp
  file and then hard-linked into place, so an existing marker is never
  overwritten and a failed write can never leave a partial final marker.
* ``inspect`` and ``mark`` are zero-Provider and never read a credential.

Importing this module performs no filesystem writes, reads no credential, loads
no ``.env`` file, constructs no Provider client, and makes zero Provider calls.
The Command Code coding agent is not part of benchmark inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g7c_stage1_integration import (
    RUN_IDENTITY_FILENAME,
    stage1_run_specs,
)

G7S_SCHEMA_VERSION = 2
G7S_REVISION = "goat-g7s-stage1-durable-quarantine-v2"
G7S_REPOSITORY_ANCHOR = "adf261379089fe8668dbabe659e69b5f463ae2b5"

MARKER_FILENAME = "g7s_durable_quarantine.json"
MARKER_RELATIVE_PATH = f"data/{MARKER_FILENAME}"
# Distinguishes the run's immutable G7C source identity from the operator's
# exact G7R wave-bundle provenance.
_RUN_SOURCE_IDENTITY_SEMANTIC_REVISION = "g7s-run-source-identity-semantic-v2"
_EVIDENCE_FINGERPRINT_REVISION = "g7s-marker-evidence-v2"
_LEDGER_DIRECTORY = "data/g7f_invocation_ledgers"

# Only a truthful paid-boundary-corrupt source outcome may be recorded.  Genuine
# transport ambiguity is already represented by a durable ``started`` /
# ``uncertain`` paid-request journal and must never be relabelled here.
DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE = "corrupt_or_unknown_durable_state"
ALLOWED_SOURCE_OUTCOMES = frozenset({DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE})

MARKER_STATE_ABSENT = "absent"
MARKER_STATE_VALID = "valid"
MARKER_STATE_INVALID = "invalid"

# Immutable source-bundle identity fields read from ``g7c_run_identity.json``.
SOURCE_BUNDLE_SEMANTIC_FIELDS = (
    "run_index",
    "model_id",
    "novel_id",
    "run_sha256",
    "run_plan_sha256",
    "s0_identity_sha256",
    "source_sha256",
    "chapter_library_sha256",
    "profile",
    "audit_interval",
    "chapter_numbering",
    "stage1_chapters",
    "stage1_audit_checkpoints",
    "relative_root",
    "source_config_sha256",
    "source_addendum_sha256",
)

MARKER_REQUIRED_FIELDS = (
    "schema_version",
    "revision",
    "run_index",
    "relative_root",
    "model_id",
    "novel_id",
    "first_blocked_chapter",
    "source_outcome",
    "reason",
    "provider_sends_started",
    "accepted_credit_equivalent_usd",
    "run_source_identity_semantic_sha256",
    "source_wave_bundle_sha256",
    "run_identity_sha256",
    "g7r_controller_sha256",
    "g7s_policy_sha256",
    "evidence",
    "created_utc",
    "marker_sha256",
)

_SHA256_CHARSET = set("0123456789abcdef")
_HEX_RE_LENGTH = 64
_GIT_HEAD_LENGTH = 40

class G7SDurableQuarantineError(RuntimeError):
    """The durable quarantine marker is unsafe, inconsistent, or refused."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7SDurableQuarantineError(
            "G7S value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path | str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7SDurableQuarantineError(f"cannot read {label}: {path}") from exc


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise G7SDurableQuarantineError(
            f"{label} must contain a JSON object: {path}"
        )
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_RE_LENGTH
        and set(value) <= _SHA256_CHARSET
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")

# ---------------------------------------------------------------------------
# Policy identity
# ---------------------------------------------------------------------------


def g7s_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G7S quarantine policy contract."""
    return {
        "schema_version": G7S_SCHEMA_VERSION,
        "revision": G7S_REVISION,
        "repository_anchor": G7S_REPOSITORY_ANCHOR,
        "marker_relative_path": MARKER_RELATIVE_PATH,
        "run_source_identity_semantic_revision": (
            _RUN_SOURCE_IDENTITY_SEMANTIC_REVISION
        ),
        "evidence_fingerprint_revision": _EVIDENCE_FINGERPRINT_REVISION,
        "allowed_source_outcomes": sorted(ALLOWED_SOURCE_OUTCOMES),
        "semantics": {
            "first_blocked_chapter_blocks_target_and_later": True,
            "blocked_chapter_never_advances_to_next": True,
            "invalid_marker_fails_closed_as_corrupt": True,
            "create_once_never_overwrite": True,
            "atomic_create_once_publication": True,
            "observing_controller_must_be_explicit": True,
            "blocked_chapter_canonical_ledger_required": True,
            "fake_paid_request_journal_state_forbidden": True,
            "status_and_plan_remain_read_only": True,
        },
        "provider_calls": 0,
    }


G7S_POLICY_SHA256 = _sha256_json(g7s_policy_identity())


def durable_quarantine_marker_path(run_root: Path | str) -> Path:
    """Return the deterministic per-run durable quarantine marker path."""
    return Path(run_root) / "data" / MARKER_FILENAME

# ---------------------------------------------------------------------------
# Run-root resolution and immutable source binding
# ---------------------------------------------------------------------------


def _run_root_for(stage1_root: Path | str, spec: Mapping[str, Any]) -> Path:
    root = Path(stage1_root).resolve()
    return (root / Path(str(spec["relative_root"]))).resolve()


def _spec_for_run_index(run_index: int) -> dict[str, Any]:
    if not _is_int(run_index) or run_index < 1:
        raise G7SDurableQuarantineError("run_index must be a positive integer")
    specs = stage1_run_specs()
    if run_index > len(specs):
        raise G7SDurableQuarantineError(
            f"run_index must be within 1..{len(specs)}"
        )
    return dict(specs[run_index - 1])

def _validated_run_identity(run_root: Path) -> dict[str, Any]:
    """Read and self-verify one run's immutable G7C identity."""
    path = run_root / RUN_IDENTITY_FILENAME
    identity = _read_object(path, "G7C run identity")
    stored = identity.get("identity_sha256")
    check = dict(identity)
    check.pop("identity_sha256", None)
    if not _is_sha256(stored) or _sha256_json(check) != stored:
        raise G7SDurableQuarantineError(
            f"G7C run identity SHA does not recompute: {path}"
        )
    missing = [
        field for field in SOURCE_BUNDLE_SEMANTIC_FIELDS if field not in identity
    ]
    if missing:
        raise G7SDurableQuarantineError(
            f"G7C run identity is missing source bundle fields: {missing}"
        )
    return identity


def _run_source_identity_semantic_sha256(identity: Mapping[str, Any]) -> str:
    payload = {
        "revision": _RUN_SOURCE_IDENTITY_SEMANTIC_REVISION,
        **{
            field: json.loads(json.dumps(identity[field]))
            for field in SOURCE_BUNDLE_SEMANTIC_FIELDS
        },
    }
    return _sha256_json(payload)


def run_source_identity_semantic_sha256(run_root: Path | str) -> str:
    """Return the deterministic run source identity semantic hash of one run."""
    identity = _validated_run_identity(Path(run_root))
    return _run_source_identity_semantic_sha256(identity)


def run_identity_sha256(run_root: Path | str) -> str:
    """Return one run's self-verified immutable identity SHA."""
    return str(_validated_run_identity(Path(run_root))["identity_sha256"])

# ---------------------------------------------------------------------------
# Evidence capture
# ---------------------------------------------------------------------------


def capture_quarantine_evidence(
    run_root: Path | str, target_chapter: int
) -> dict[str, Any]:
    """Capture bounded durable evidence references for one target chapter.

    All references are read-only file names plus content hashes.  Nothing here
    reaches a Provider, a credential, or the network.
    """
    run_root = Path(run_root)
    references: dict[str, Any] = {}

    identity_path = run_root / RUN_IDENTITY_FILENAME
    references["run_identity"] = (
        {"path": RUN_IDENTITY_FILENAME, "sha256": _sha256_file(identity_path)}
        if identity_path.is_file()
        else None
    )

    progress_path = run_root / "data" / "progress.json"
    references["progress"] = (
        {"path": "data/progress.json", "sha256": _sha256_file(progress_path)}
        if progress_path.is_file()
        else None
    )
    ledger_path = (
        run_root
        / _LEDGER_DIRECTORY
        / f"{int(target_chapter):04d}.json"
    )
    ledger_reference: dict[str, Any] | None = None
    if ledger_path.is_file():
        ledger = _read_object(ledger_path, "target G7F invocation ledger")
        ledger_reference = {
            "path": (
                f"data/g7f_invocation_ledgers/{int(target_chapter):04d}.json"
            ),
            "sha256": _sha256_file(ledger_path),
            "target_chapter": ledger.get("target_chapter"),
            "new_provider_sends_started": ledger.get(
                "new_provider_sends_started"
            ),
            "accepted_credit_equivalent_usd": ledger.get(
                "accepted_credit_equivalent_usd"
            ),
        }
    references["target_chapter_ledger"] = ledger_reference

    journal_dir = run_root / "data" / "provider_request_journal"
    journals: list[dict[str, Any]] = []
    if journal_dir.is_dir():
        for path in sorted(journal_dir.iterdir(), key=lambda item: item.name):
            if path.is_file() and path.suffix == ".json":
                journals.append(
                    {"file": path.name, "sha256": _sha256_file(path)}
                )
    references["journals"] = journals

    return {
        "revision": _EVIDENCE_FINGERPRINT_REVISION,
        "references": references,
        "evidence_fingerprint_sha256": _sha256_json(
            {
                "revision": _EVIDENCE_FINGERPRINT_REVISION,
                "references": references,
            }
        ),
    }


def _evidence_fingerprint(evidence: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            "revision": _EVIDENCE_FINGERPRINT_REVISION,
            "references": json.loads(json.dumps(evidence.get("references"))),
        }
    )

# ---------------------------------------------------------------------------
# Marker building / semantic hashing
# ---------------------------------------------------------------------------


def marker_semantic_payload(marker: Mapping[str, Any]) -> dict[str, Any]:
    """Return the marker payload used for the deterministic marker SHA."""
    return {
        key: json.loads(json.dumps(value))
        for key, value in dict(marker).items()
        if key != "marker_sha256"
    }


def compute_marker_sha256(marker: Mapping[str, Any]) -> str:
    return _sha256_json(marker_semantic_payload(marker))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _blocked_chapter_ledger_path(
    run_root: Path, target_chapter: int
) -> Path:
    return (
        run_root
        / _LEDGER_DIRECTORY
        / f"{int(target_chapter):04d}.json"
    )


def _validate_blocked_chapter_ledger(
    *,
    run_root: Path,
    identity: Mapping[str, Any],
    target_chapter: int,
    provider_sends_started: int,
    accepted: Decimal,
) -> dict[str, Any]:
    """Validate the blocked chapter's canonical G7F ledger without creating it.

    The frozen ``InvocationBudgetLedger`` validator is imported lazily so this
    module's own import stays zero-write, zero-Provider, and credential-free.
    G7S never creates or mutates the ledger.
    """
    path = _blocked_chapter_ledger_path(run_root, target_chapter)
    if not path.is_file():
        raise G7SDurableQuarantineError(
            "the blocked chapter canonical G7F invocation ledger is missing; "
            "refusing to mark without real paid-boundary evidence: "
            f"{path}"
        )
    document = _read_object(path, "blocked chapter G7F invocation ledger")
    try:
        from benchmark_longitudinal_g7f_stage1_paid_executor import (
            G7FBudgetError,
            InvocationBudgetLedger,
        )
    except Exception as exc:  # noqa: BLE001 - validator must exist to mark
        raise G7SDurableQuarantineError(
            "the frozen G7F invocation ledger validator is unavailable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        InvocationBudgetLedger(
            path=path,
            document=document,
            clock=_utc_now,
        )
    except G7FBudgetError as exc:
        raise G7SDurableQuarantineError(
            "the blocked chapter G7F invocation ledger is not canonical: "
            f"{exc}"
        ) from exc

    if document.get("target_chapter") != int(target_chapter):
        raise G7SDurableQuarantineError(
            "the blocked chapter ledger binds another chapter"
        )
    for field in ("run_index", "relative_root", "model_id", "novel_id"):
        if document.get(field) != identity.get(field):
            raise G7SDurableQuarantineError(
                "the blocked chapter ledger "
                f"{field} disagrees with the run identity"
            )
    if document.get("new_provider_sends_started") != int(
        provider_sends_started
    ):
        raise G7SDurableQuarantineError(
            "provider_sends_started disagrees with the durable target ledger"
        )
    try:
        ledger_accepted = Decimal(
            str(document.get("accepted_credit_equivalent_usd"))
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G7SDurableQuarantineError(
            "durable target ledger accepted cost is not a number"
        ) from exc
    if ledger_accepted != accepted:
        raise G7SDurableQuarantineError(
            "accepted_credit_equivalent_usd disagrees with the durable "
            "target ledger"
        )
    return document


def build_durable_quarantine_marker(
    *,
    run_root: Path | str,
    spec: Mapping[str, Any],
    first_blocked_chapter: int,
    source_outcome: str,
    reason: str,
    provider_sends_started: int,
    accepted_credit_equivalent_usd: Any,
    run_source_identity_semantic_sha256: str,
    source_wave_bundle_sha256: str,
    g7r_controller_sha256: str,
    expected_run_identity_sha256: str | None = None,
    expected_evidence_fingerprint_sha256: str | None = None,
    repository_head: str | None = None,
    created_utc: str | None = None,
) -> dict[str, Any]:
    """Build one durable quarantine marker cross-checked against evidence.

    The build validates every explicit binding against the run's immutable
    identity and against the blocked chapter's canonical G7F invocation ledger.
    It never mutates state.
    """
    run_root = Path(run_root)
    if not run_root.is_dir():
        raise G7SDurableQuarantineError(f"run root is missing: {run_root}")

    identity = _validated_run_identity(run_root)
    identity_sha = str(identity["identity_sha256"])
    if not _is_int(first_blocked_chapter) or first_blocked_chapter < 1:
        raise G7SDurableQuarantineError(
            "first_blocked_chapter must be a positive integer"
        )
    if source_outcome not in ALLOWED_SOURCE_OUTCOMES:
        raise G7SDurableQuarantineError(
            "source_outcome must be one of "
            f"{sorted(ALLOWED_SOURCE_OUTCOMES)}: a durable marker may not claim "
            "an untruthful transport ambiguity"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise G7SDurableQuarantineError("reason must be a non-empty string")
    if not _is_int(provider_sends_started) or provider_sends_started < 1:
        raise G7SDurableQuarantineError(
            "provider_sends_started must be a positive integer (>= 1): a G7S "
            "quarantine requires real paid-boundary evidence"
        )
    try:
        accepted = Decimal(str(accepted_credit_equivalent_usd))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise G7SDurableQuarantineError(
            "accepted_credit_equivalent_usd must be a non-negative number"
        ) from exc
    if not accepted.is_finite() or accepted < 0:
        raise G7SDurableQuarantineError(
            "accepted_credit_equivalent_usd must be a non-negative number"
        )
    # Bind the marker to the exact run identity, never to a caller guess.
    for field, wanted in (
        ("run_index", spec["run_index"]),
        ("relative_root", spec["relative_root"]),
        ("model_id", spec["model_id"]),
        ("novel_id", spec["novel_id"]),
    ):
        if identity.get(field) != wanted:
            raise G7SDurableQuarantineError(
                f"run identity {field} disagrees with the selected run"
            )

    if expected_run_identity_sha256 is not None and (
        expected_run_identity_sha256 != identity_sha
    ):
        raise G7SDurableQuarantineError(
            "expected run identity SHA does not match the selected run"
        )

    recomputed_source = _run_source_identity_semantic_sha256(identity)
    if not _is_sha256(run_source_identity_semantic_sha256) or (
        run_source_identity_semantic_sha256 != recomputed_source
    ):
        raise G7SDurableQuarantineError(
            "run source identity semantic SHA does not match the selected run"
        )
    if not _is_sha256(source_wave_bundle_sha256):
        raise G7SDurableQuarantineError(
            "source_wave_bundle_sha256 must be an explicit lowercase SHA-256"
        )
    if not _is_sha256(g7r_controller_sha256):
        raise G7SDurableQuarantineError(
            "g7r_controller_sha256 must be a SHA-256"
        )
    _validate_blocked_chapter_ledger(
        run_root=run_root,
        identity=identity,
        target_chapter=first_blocked_chapter,
        provider_sends_started=provider_sends_started,
        accepted=accepted,
    )
    evidence = capture_quarantine_evidence(run_root, first_blocked_chapter)
    if evidence["references"]["target_chapter_ledger"] is None:
        raise G7SDurableQuarantineError(
            "the blocked chapter canonical G7F invocation ledger is missing"
        )

    if expected_evidence_fingerprint_sha256 is not None and (
        expected_evidence_fingerprint_sha256
        != evidence["evidence_fingerprint_sha256"]
    ):
        raise G7SDurableQuarantineError(
            "expected evidence fingerprint does not match captured evidence"
        )

    if created_utc is None:
        resolved_created_utc = _now_utc_iso()
    elif isinstance(created_utc, str) and created_utc.strip():
        resolved_created_utc = created_utc
    else:
        raise G7SDurableQuarantineError(
            "created_utc must be a non-empty string when supplied"
        )
    if repository_head is not None and not _is_git_commit_id(repository_head):
        raise G7SDurableQuarantineError(
            "repository_head must be a 40-character lowercase hex commit id"
        )

    marker: dict[str, Any] = {
        "schema_version": G7S_SCHEMA_VERSION,
        "revision": G7S_REVISION,
        "run_index": int(spec["run_index"]),
        "relative_root": str(spec["relative_root"]),
        "model_id": str(spec["model_id"]),
        "novel_id": str(spec["novel_id"]),
        "first_blocked_chapter": int(first_blocked_chapter),
        "source_outcome": source_outcome,
        "reason": reason.strip(),
        "provider_sends_started": int(provider_sends_started),
        "accepted_credit_equivalent_usd": _decimal_text(accepted),
        "run_source_identity_semantic_sha256": recomputed_source,
        "source_wave_bundle_sha256": source_wave_bundle_sha256,
        "run_identity_sha256": identity_sha,
        "g7r_controller_sha256": g7r_controller_sha256,
        "g7s_policy_sha256": G7S_POLICY_SHA256,
        "evidence": evidence,
        "created_utc": resolved_created_utc,
        # Non-required provenance: the operator's checked-out commit, when known.
        "repository_head": repository_head,
        "marker_sha256": None,
    }
    marker["marker_sha256"] = compute_marker_sha256(marker)
    return marker

# ---------------------------------------------------------------------------
# Create-once marker writing
# ---------------------------------------------------------------------------


def _is_git_commit_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _GIT_HEAD_LENGTH
        and set(value) <= _SHA256_CHARSET
    )


def _publication_temp_path(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )


def _safe_unlink_temp(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _fsync(fd: int) -> None:
    os.fsync(fd)


def write_durable_quarantine_marker(
    run_root: Path | str, marker: Mapping[str, Any]
) -> Path:
    """Write one durable quarantine marker exactly once.

    Publication is atomic and create-once: the payload is written and fsynced to
    a unique private temp file, then hard-linked into place with ``os.link``,
    which refuses an existing destination.  An existing marker is therefore
    never overwritten, truncated, or partially replaced, and a failed write can
    never leave a partial final marker behind.  The canonical bytes are read back
    and re-verified before the writer reports success.
    """
    if not isinstance(marker, Mapping):
        raise G7SDurableQuarantineError("marker must be a JSON object")
    root = Path(run_root)
    path = durable_quarantine_marker_path(root)
    if not path.parent.is_dir():
        raise G7SDurableQuarantineError(
            f"the run data directory is missing: {path.parent}"
        )
    if path.exists():
        raise G7SDurableQuarantineError(
            "refusing to overwrite an existing durable quarantine marker: "
            f"{path}"
        )

    payload = json.dumps(marker, indent=2, sort_keys=True, ensure_ascii=False)
    temp_path = _publication_temp_path(path)

    try:
        fd = os.open(
            str(temp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError as exc:
        raise G7SDurableQuarantineError(
            f"cannot create the durable quarantine marker temp file: {exc}"
        ) from exc
    except OSError as exc:
        raise G7SDurableQuarantineError(
            f"cannot write the durable quarantine marker {path}: {exc}"
        ) from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            _fsync(handle.fileno())
    except Exception as exc:
        _safe_unlink_temp(temp_path)
        if isinstance(exc, G7SDurableQuarantineError):
            raise
        raise G7SDurableQuarantineError(
            f"cannot write the durable quarantine marker {path}: {exc}"
        ) from exc

    try:
        # os.link is atomic and refuses an existing destination, so an existing
        # final marker can never be replaced.  Never fall back to os.replace.
        os.link(str(temp_path), str(path))
    except FileExistsError as exc:
        _safe_unlink_temp(temp_path)
        raise G7SDurableQuarantineError(
            "refusing to overwrite an existing durable quarantine marker: "
            f"{path}"
        ) from exc
    except OSError as exc:
        _safe_unlink_temp(temp_path)
        raise G7SDurableQuarantineError(
            f"cannot publish the durable quarantine marker {path}: {exc}"
        ) from exc

    # The final marker is now published.  Removing the extra directory entry is
    # best-effort and never changes the published marker.
    _safe_unlink_temp(temp_path)

    try:
        written = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise G7SDurableQuarantineError(
            "cannot read back the durable quarantine marker "
            f"{path}: {exc}"
        ) from exc
    if written != payload:
        raise G7SDurableQuarantineError(
            "the durable quarantine marker on disk does not match the written "
            f"payload; the ambiguous on-disk state is left in place: {path}"
        )
    computed = compute_marker_sha256(marker)
    if marker.get("marker_sha256") != computed:
        raise G7SDurableQuarantineError(
            "the durable quarantine marker SHA does not recompute"
        )
    return path


def mark_durable_quarantine(
    *,
    stage1_root: Path | str,
    run_index: int,
    first_blocked_chapter: int,
    provider_sends_started: int,
    accepted_credit_equivalent_usd: Any,
    source_wave_bundle_sha256: str,
    g7r_controller_sha256: str,
    reason: str,
    source_outcome: str = DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE,
    expected_run_identity_sha256: str | None = None,
    expected_evidence_fingerprint_sha256: str | None = None,
    repository_head: str | None = None,
    created_utc: str | None = None,
) -> dict[str, Any]:
    """Create the create-once durable quarantine marker for one Stage-1 run.

    This is an explicit operator act: it is never invoked by a wave execution
    path.  Zero Provider calls, zero credential access, zero network, and no
    journal/ledger write are performed; only the operator's immutable provenance
    refusal record is added.
    """
    spec = _spec_for_run_index(run_index)
    run_root = _run_root_for(stage1_root, spec)
    marker = build_durable_quarantine_marker(
        run_root=run_root,
        spec=spec,
        first_blocked_chapter=first_blocked_chapter,
        source_outcome=source_outcome,
        reason=reason,
        provider_sends_started=provider_sends_started,
        accepted_credit_equivalent_usd=accepted_credit_equivalent_usd,
        run_source_identity_semantic_sha256=(
            run_source_identity_semantic_sha256(run_root)
        ),
        source_wave_bundle_sha256=source_wave_bundle_sha256,
        g7r_controller_sha256=g7r_controller_sha256,
        expected_run_identity_sha256=expected_run_identity_sha256,
        expected_evidence_fingerprint_sha256=(
            expected_evidence_fingerprint_sha256
        ),
        repository_head=repository_head,
        created_utc=created_utc,
    )
    path = write_durable_quarantine_marker(run_root, marker)
    return {
        "state": MARKER_STATE_VALID,
        "marker_path": str(path),
        "run_root": str(run_root),
        "marker": marker,
    }

# ---------------------------------------------------------------------------
# Read-only inspection (total, never raising, never writing)
# ---------------------------------------------------------------------------


def _inspect_result(
    result: dict[str, Any],
    state: str,
    reason: str | None,
    detail: str,
) -> dict[str, Any]:
    result["state"] = state
    result["reason"] = reason
    result["detail"] = detail
    return result


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _informational_number_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _marker_informational_fields(
    marker: Mapping[str, Any]
) -> dict[str, Any]:
    evidence = marker.get("evidence")
    references = (
        evidence.get("references") if isinstance(evidence, Mapping) else None
    )
    return {
        "first_blocked_chapter": (
            marker.get("first_blocked_chapter")
            if _is_int(marker.get("first_blocked_chapter"))
            else None
        ),
        "source_outcome": (
            marker.get("source_outcome")
            if isinstance(marker.get("source_outcome"), str)
            else None
        ),
        "provider_sends_started": (
            marker.get("provider_sends_started")
            if _is_int(marker.get("provider_sends_started"))
            else None
        ),
        "accepted_credit_equivalent_usd": _informational_number_text(
            marker.get("accepted_credit_equivalent_usd")
        ),
        "run_identity_sha256": (
            marker.get("run_identity_sha256")
            if isinstance(marker.get("run_identity_sha256"), str)
            else None
        ),
        "marker_sha256": (
            marker.get("marker_sha256")
            if isinstance(marker.get("marker_sha256"), str)
            else None
        ),
        "created_utc": (
            marker.get("created_utc")
            if isinstance(marker.get("created_utc"), str)
            else None
        ),
        "evidence_references": (
            json.loads(json.dumps(references))
            if isinstance(references, Mapping)
            else None
        ),
    }


def _marker_malformed_detail(marker: Mapping[str, Any]) -> str | None:
    """Return a stable detail when a required field is missing or ill-typed.

    ``marker_sha256`` is deliberately excluded: a missing or non-SHA-256 marker
    hash is reported as a hash mismatch, which is the reason the contract
    reserves for it.
    """
    for field in MARKER_REQUIRED_FIELDS:
        if field == "marker_sha256":
            continue
        if field not in marker:
            return f"required field is missing: {field}"
    if not _is_int(marker.get("schema_version")):
        return "schema_version must be an integer"
    if not _is_non_empty_str(marker.get("revision")):
        return "revision must be a non-empty string"
    if not _is_int(marker.get("run_index")) or int(marker["run_index"]) < 1:
        return "run_index must be a positive integer"
    if not _is_non_empty_str(marker.get("relative_root")):
        return "relative_root must be a non-empty string"
    if not _is_non_empty_str(marker.get("model_id")):
        return "model_id must be a non-empty string"
    if not _is_non_empty_str(marker.get("novel_id")):
        return "novel_id must be a non-empty string"
    if (
        not _is_int(marker.get("first_blocked_chapter"))
        or int(marker["first_blocked_chapter"]) < 1
    ):
        return "first_blocked_chapter must be a positive integer"
    if marker.get("source_outcome") not in ALLOWED_SOURCE_OUTCOMES:
        return (
            "source_outcome must be one of "
            f"{sorted(ALLOWED_SOURCE_OUTCOMES)}: a durable marker may not claim "
            "an untruthful transport ambiguity"
        )
    if not _is_non_empty_str(marker.get("reason")):
        return "reason must be a non-empty string"
    if (
        not _is_int(marker.get("provider_sends_started"))
        or int(marker["provider_sends_started"]) < 1
    ):
        return "provider_sends_started must be a positive integer"
    accepted = marker.get("accepted_credit_equivalent_usd")
    if isinstance(accepted, bool):
        return "accepted_credit_equivalent_usd must be a non-negative number"
    try:
        accepted_value = Decimal(str(accepted))
    except (InvalidOperation, TypeError, ValueError):
        return "accepted_credit_equivalent_usd must be a non-negative number"
    if not accepted_value.is_finite() or accepted_value < 0:
        return "accepted_credit_equivalent_usd must be a non-negative number"
    for field in (
        "run_source_identity_semantic_sha256",
        "source_wave_bundle_sha256",
        "run_identity_sha256",
    ):
        if not _is_sha256(marker.get(field)):
            return f"{field} must be a lowercase SHA-256"
    if not _is_non_empty_str(marker.get("g7r_controller_sha256")):
        return "g7r_controller_sha256 must be a non-empty string"
    if not _is_non_empty_str(marker.get("g7s_policy_sha256")):
        return "g7s_policy_sha256 must be a non-empty string"
    if not isinstance(marker.get("evidence"), Mapping):
        return "evidence must be a JSON object"
    if not _is_non_empty_str(marker.get("created_utc")):
        return "created_utc must be a non-empty string"
    return None


def _evidence_drift(
    run_root: Path, references: Mapping[str, Any]
) -> list[str]:
    """Report informational drift of the cited evidence since marking.

    The marker is a historical provenance record, not a live snapshot, so drift
    here is reported only and never changes the marker state.
    """
    drift: list[str] = []
    for field in ("run_identity", "progress", "target_chapter_ledger"):
        reference = references.get(field)
        if not isinstance(reference, Mapping):
            continue
        relative = reference.get("path")
        recorded = reference.get("sha256")
        if not isinstance(relative, str):
            continue
        path = run_root / relative
        if not path.is_file():
            drift.append(f"{relative}: evidence file is missing")
        elif _sha256_file(path) != recorded:
            drift.append(f"{relative}: evidence bytes changed since marking")

    recorded_journals: dict[str, Any] = {}
    journals = references.get("journals")
    if isinstance(journals, list):
        for item in journals:
            if isinstance(item, Mapping) and isinstance(item.get("file"), str):
                recorded_journals[str(item["file"])] = item.get("sha256")

    journal_dir = run_root / "data" / "provider_request_journal"
    present: set[str] = set()
    if journal_dir.is_dir():
        for path in sorted(
            (item for item in journal_dir.iterdir() if item.is_file()),
            key=lambda item: item.name,
        ):
            if path.suffix != ".json":
                continue
            present.add(path.name)
            relative = f"data/provider_request_journal/{path.name}"
            if path.name not in recorded_journals:
                drift.append(f"{relative}: new journal added since marking")
            elif _sha256_file(path) != recorded_journals[path.name]:
                drift.append(f"{relative}: journal bytes changed since marking")
    for name in sorted(set(recorded_journals) - present):
        drift.append(
            f"data/provider_request_journal/{name}: recorded journal is missing"
        )
    return drift


def _safe_evidence_drift(
    run_root: Path, references: Mapping[str, Any]
) -> list[str]:
    try:
        return _evidence_drift(run_root, references)
    except Exception:  # noqa: BLE001 - informational only, never corrupting
        return []


def _inspect_marker_object(
    run_root: Path,
    path: Path,
    result: dict[str, Any],
    *,
    spec: Mapping[str, Any] | None,
    expected_g7r_controller_sha256: str | None,
) -> dict[str, Any]:
    """Run the ordered fail-closed checks for one present marker file."""
    if not path.exists():
        return _inspect_result(
            result,
            MARKER_STATE_ABSENT,
            None,
            "no durable quarantine marker exists",
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_unreadable",
            "the durable quarantine marker cannot be read: "
            f"{type(exc).__name__}: {exc}",
        )
    try:
        marker = json.loads(text)
    except json.JSONDecodeError as exc:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_unreadable",
            f"the durable quarantine marker is not valid JSON: {exc}",
        )
    if not isinstance(marker, Mapping):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_not_an_object",
            "the durable quarantine marker must be a JSON object",
        )

    result.update(_marker_informational_fields(marker))
    references = result.get("evidence_references")
    references = references if isinstance(references, Mapping) else {}
    result["evidence_drift"] = _safe_evidence_drift(run_root, references)

    malformed = _marker_malformed_detail(marker)
    if malformed is not None:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_malformed",
            f"the durable quarantine marker is malformed: {malformed}",
        )
    if (
        marker.get("schema_version") != G7S_SCHEMA_VERSION
        or marker.get("revision") != G7S_REVISION
    ):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "unsupported_marker_identity",
            "the durable quarantine marker has an unsupported identity: "
            f"schema_version={marker.get('schema_version')!r} "
            f"revision={marker.get('revision')!r}",
        )

    stored_sha = marker.get("marker_sha256")
    if not _is_sha256(stored_sha) or stored_sha != compute_marker_sha256(marker):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_sha256_mismatch",
            "the durable quarantine marker SHA is missing, malformed, or does "
            "not recompute",
        )
    if marker.get("g7s_policy_sha256") != G7S_POLICY_SHA256:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "g7s_policy_drift",
            "the durable quarantine marker binds another G7S policy: "
            f"{marker.get('g7s_policy_sha256')!r}",
        )

    try:
        identity = _validated_run_identity(run_root)
    except G7SDurableQuarantineError as exc:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "wrong_run_identity",
            f"the run's own G7C immutable identity cannot be verified: {exc}",
        )
    binding: Mapping[str, Any] = spec if isinstance(spec, Mapping) else identity
    for field in ("run_index", "relative_root", "model_id", "novel_id"):
        if marker.get(field) != binding.get(field):
            return _inspect_result(
                result,
                MARKER_STATE_INVALID,
                "wrong_run_identity",
                "the durable quarantine marker "
                f"{field} disagrees with the run: {marker.get(field)!r}",
            )
    if marker.get("run_identity_sha256") != identity.get("identity_sha256"):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "wrong_run_identity",
            "the durable quarantine marker binds another immutable run identity",
        )

    if marker.get("run_source_identity_semantic_sha256") != (
        _run_source_identity_semantic_sha256(identity)
    ):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "run_source_identity_drift",
            "the durable quarantine marker binds different run source "
            "identity semantics than the run",
        )

    ledger_reference = references.get("target_chapter_ledger")
    if isinstance(ledger_reference, Mapping) and (
        ledger_reference.get("target_chapter")
        != marker.get("first_blocked_chapter")
    ):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "conflicting_evidence_reference",
            "the durable quarantine marker cites a target-chapter ledger for "
            "another chapter",
        )

    evidence = marker.get("evidence")
    if not isinstance(evidence, Mapping):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_malformed",
            "the durable quarantine marker evidence is not a JSON object",
        )
    if evidence.get("revision") != _EVIDENCE_FINGERPRINT_REVISION:
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "unsupported_evidence_revision",
            "the durable quarantine marker evidence has an unsupported "
            f"revision: {evidence.get('revision')!r}",
        )
    if not isinstance(evidence.get("references"), Mapping):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "evidence_references_invalid",
            "the durable quarantine marker evidence references are not a JSON "
            "object",
        )
    stored_fingerprint = evidence.get("evidence_fingerprint_sha256")
    if not _is_sha256(stored_fingerprint) or (
        stored_fingerprint != _evidence_fingerprint(evidence)
    ):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "evidence_fingerprint_mismatch",
            "the durable quarantine marker evidence fingerprint is missing, "
            "malformed, or does not recompute",
        )

    controller = marker.get("g7r_controller_sha256")
    if not _is_sha256(controller):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_malformed",
            "the durable quarantine marker g7r_controller_sha256 is not a "
            "SHA-256",
        )
    if (
        expected_g7r_controller_sha256 is not None
        and expected_g7r_controller_sha256 != controller
    ):
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "g7r_controller_drift",
            "the durable quarantine marker was written by another observing "
            "G7R controller",
        )

    return _inspect_result(
        result,
        MARKER_STATE_VALID,
        None,
        "the durable quarantine marker is valid and self-consistent",
    )


def inspect_durable_quarantine_marker(
    run_root: Path | str,
    *,
    spec: Mapping[str, Any] | None = None,
    expected_g7r_controller_sha256: str | None = None,
) -> dict[str, Any]:
    """Read-only, total inspection of one run's durable quarantine marker.

    This never writes, never reads a credential, and never raises: any
    unexpected failure becomes the fail-closed ``invalid`` state, and any
    individually well-formed marker that contradicts the run's own immutable
    identity, its source-bundle semantics, the G7S policy identity, or the
    durable ledger chapter it cites is reported as ``invalid``.
    """
    root = Path(run_root)
    path = durable_quarantine_marker_path(root)
    result: dict[str, Any] = {
        "state": MARKER_STATE_ABSENT,
        "marker_path": str(path),
        "reason": None,
        "detail": "no durable quarantine marker exists",
        "first_blocked_chapter": None,
        "source_outcome": None,
        "provider_sends_started": None,
        "accepted_credit_equivalent_usd": None,
        "run_identity_sha256": None,
        "marker_sha256": None,
        "created_utc": None,
        "evidence_references": None,
        "evidence_drift": [],
    }
    try:
        return _inspect_marker_object(
            root,
            path,
            result,
            spec=spec,
            expected_g7r_controller_sha256=(
                expected_g7r_controller_sha256
            ),
        )
    except Exception as exc:  # noqa: BLE001 - inspection is total, fail-closed
        return _inspect_result(
            result,
            MARKER_STATE_INVALID,
            "marker_unreadable",
            "the durable quarantine marker could not be inspected safely: "
            f"{type(exc).__name__}: {exc}",
        )

# ---------------------------------------------------------------------------
# CLI (zero Provider, zero credential)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7s-stage1-durable-quarantine",
        description=(
            "Durable Stage-1 quarantine marker: read-only inspection and the "
            "explicit create-once quarantine act"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="read-only durable quarantine marker inspection for one run",
    )
    inspect_parser.add_argument("--stage1-root", required=True)
    inspect_parser.add_argument("--run-index", type=int, required=True)

    mark_parser = subparsers.add_parser(
        "mark",
        help="create the create-once durable quarantine marker for one run",
    )
    mark_parser.add_argument("--stage1-root", required=True)
    mark_parser.add_argument("--run-index", type=int, required=True)
    mark_parser.add_argument(
        "--first-blocked-chapter", type=int, required=True
    )
    mark_parser.add_argument(
        "--provider-sends-started", type=int, required=True
    )
    mark_parser.add_argument(
        "--accepted-credit-equivalent-usd", required=True
    )
    mark_parser.add_argument("--reason", required=True)
    mark_parser.add_argument(
        "--source-outcome", default=DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE
    )
    mark_parser.add_argument(
        "--g7r-controller-sha256",
        required=True,
        help=(
            "HISTORICAL G7R controller SHA-256 that observed the unsafe paid "
            "state (must be supplied explicitly; never derived from the current "
            "checkout)"
        ),
    )
    mark_parser.add_argument("--source-wave-bundle-sha256", required=True)
    mark_parser.add_argument(
        "--expected-run-identity-sha256", default=None
    )
    mark_parser.add_argument(
        "--expected-evidence-fingerprint-sha256", default=None
    )
    mark_parser.add_argument("--repository-head", default=None)
    mark_parser.add_argument(
        "--authorize-durable-quarantine",
        action="store_true",
        help=(
            "explicitly authorize creating the durable quarantine marker for "
            "exactly one Stage-1 run"
        ),
    )
    return parser


def _emit(document: Mapping[str, Any], *, exit_code: int) -> int:
    sys.stdout.write(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()
    return exit_code


def _zero_provider_envelope() -> dict[str, Any]:
    return {
        "revision": G7S_REVISION,
        "policy_sha256": G7S_POLICY_SHA256,
        "provider_calls": 0,
        "credential_access": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            spec = _spec_for_run_index(args.run_index)
            run_root = _run_root_for(args.stage1_root, spec)
            snapshot = inspect_durable_quarantine_marker(run_root, spec=spec)
            invalid = snapshot["state"] == MARKER_STATE_INVALID
            return _emit(
                {
                    **_zero_provider_envelope(),
                    "status": "BLOCKED" if invalid else "OK",
                    "state": snapshot["state"],
                    "reason": snapshot["reason"],
                    "detail": snapshot["detail"],
                    "first_blocked_chapter": snapshot[
                        "first_blocked_chapter"
                    ],
                    "source_outcome": snapshot["source_outcome"],
                    "marker_path": snapshot["marker_path"],
                    "run_root": str(run_root),
                },
                exit_code=2 if invalid else 0,
            )
        if args.command == "mark":
            if not args.authorize_durable_quarantine:
                return _emit(
                    {
                        **_zero_provider_envelope(),
                        "status": "ERROR",
                        "message": (
                            "creating a durable quarantine marker requires the "
                            "explicit --authorize-durable-quarantine flag; "
                            "nothing was written"
                        ),
                    },
                    exit_code=1,
                )
            controller_sha256 = args.g7r_controller_sha256
            result = mark_durable_quarantine(
                stage1_root=args.stage1_root,
                run_index=args.run_index,
                first_blocked_chapter=args.first_blocked_chapter,
                provider_sends_started=args.provider_sends_started,
                accepted_credit_equivalent_usd=(
                    args.accepted_credit_equivalent_usd
                ),
                source_wave_bundle_sha256=args.source_wave_bundle_sha256,
                g7r_controller_sha256=controller_sha256,
                reason=args.reason,
                source_outcome=args.source_outcome,
                expected_run_identity_sha256=(
                    args.expected_run_identity_sha256
                ),
                expected_evidence_fingerprint_sha256=(
                    args.expected_evidence_fingerprint_sha256
                ),
                repository_head=args.repository_head,
            )
            marker = result["marker"]
            return _emit(
                {
                    **_zero_provider_envelope(),
                    "status": "OK",
                    "state": result["state"],
                    "marker_path": result["marker_path"],
                    "run_root": result["run_root"],
                    "marker_sha256": marker["marker_sha256"],
                    "first_blocked_chapter": marker["first_blocked_chapter"],
                    "source_outcome": marker["source_outcome"],
                    "reason": marker["reason"],
                    "g7r_controller_sha256": marker["g7r_controller_sha256"],
                },
                exit_code=0,
            )
        parser.error(f"unknown command: {args.command}")
        return 2
    except G7SDurableQuarantineError as exc:
        return _emit(
            {
                **_zero_provider_envelope(),
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            exit_code=1,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never a false claim
        # Every G7S path is zero-Provider and credential-free by construction, so
        # an unexpected failure can still be reported truthfully.
        return _emit(
            {
                **_zero_provider_envelope(),
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            exit_code=1,
        )


__all__ = [
    "ALLOWED_SOURCE_OUTCOMES",
    "DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE",
    "G7SDurableQuarantineError",
    "G7S_POLICY_SHA256",
    "G7S_REPOSITORY_ANCHOR",
    "G7S_REVISION",
    "G7S_SCHEMA_VERSION",
    "MARKER_FILENAME",
    "MARKER_RELATIVE_PATH",
    "MARKER_REQUIRED_FIELDS",
    "MARKER_STATE_ABSENT",
    "MARKER_STATE_INVALID",
    "MARKER_STATE_VALID",
    "SOURCE_BUNDLE_SEMANTIC_FIELDS",
    "build_durable_quarantine_marker",
    "build_parser",
    "capture_quarantine_evidence",
    "compute_marker_sha256",
    "durable_quarantine_marker_path",
    "g7s_policy_identity",
    "inspect_durable_quarantine_marker",
    "main",
    "mark_durable_quarantine",
    "marker_semantic_payload",
    "run_identity_sha256",
    "run_source_identity_semantic_sha256",
    "write_durable_quarantine_marker",
]


if __name__ == "__main__":
    raise SystemExit(main())
