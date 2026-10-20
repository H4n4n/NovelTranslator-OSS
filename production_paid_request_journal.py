"""Durable fail-closed journaling for one logical paid provider request.

The journal closes the local restart window between provider processing and
domain-specific persistence. It does not claim exactly-once provider billing.

A logical request is bound to one fixed journal slot. Request identity changes
for that slot fail closed instead of silently creating a new paid operation.

Lifecycle:
    planned -> started -> response_recorded
                         -> uncertain

A saved started or uncertain record has no durable response and therefore
blocks automatic reissue. response_recorded is replayable without provider
access. Callers remain responsible for downstream translation/state persistence.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping

from production_failure_classification import (
    LocalPersistenceError,
    NonProviderProductionError,
)


SCHEMA_VERSION = 1
REVISION = "paid-request-journal-v1"
ALLOWED_STATUSES = frozenset({
    "planned",
    "started",
    "response_recorded",
    "uncertain",
})
KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PaidRequestJournalError(NonProviderProductionError):
    """Journal state is malformed, stale, conflicting, or otherwise unsafe."""

    failure_kind = "local_safety"


class PaidRequestUncertainError(PaidRequestJournalError):
    """A paid request may have completed remotely but lacks a durable response."""


@dataclass(frozen=True, slots=True)
class JournalObservation:
    """Detached immutable snapshot; never a transport or authorization grant.

    JSON is retained as strings so nested data cannot be mutated through a
    frozen dataclass. Decoding properties return new copies on every access.
    """
    phase: str
    journal_path: str
    identity_json: str
    attempt_context_json: str
    record_json: str

    @property
    def identity(self):
        return json.loads(self.identity_json)

    @property
    def attempt_context(self):
        return json.loads(self.attempt_context_json)

    @property
    def record(self):
        return json.loads(self.record_json)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PaidRequestJournalError(
            "paid request identity is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def paid_request_journal_path(
    journal_dir: Path | str,
    operation_kind: str,
    chapter: int,
) -> Path:
    """Return one deterministic journal slot for a logical chapter operation."""
    if not isinstance(operation_kind, str) or not KIND_RE.fullmatch(operation_kind):
        raise PaidRequestJournalError(
            f"invalid paid request operation kind: {operation_kind!r}"
        )
    if (
        not isinstance(chapter, int)
        or isinstance(chapter, bool)
        or chapter < 1
    ):
        raise PaidRequestJournalError(
            f"invalid paid request chapter: {chapter!r}"
        )
    return Path(journal_dir) / f"{chapter:04d}.{operation_kind}.json"


def request_identity(
    *,
    operation_kind: str,
    novel_id: str,
    chapter: int,
    provider: str,
    model: str,
    profile_id: str,
    source_sha256: str,
    request_payload: Mapping[str, Any],
    translation_sha256: str = "",
) -> dict[str, Any]:
    """Build the exact immutable identity for one logical paid request."""
    paid_request_journal_path(".", operation_kind, chapter)
    values = {
        "novel_id": novel_id,
        "provider": provider,
        "model": model,
        "profile_id": profile_id,
        "source_sha256": source_sha256,
        "translation_sha256": translation_sha256,
    }
    for label, value in values.items():
        if not isinstance(value, str):
            raise PaidRequestJournalError(
                f"paid request {label} must be a string"
            )
    if not novel_id or not provider or not model or not profile_id:
        raise PaidRequestJournalError(
            "paid request novel/provider/model/profile identity must be non-empty"
        )
    for label, digest in (
        ("source_sha256", source_sha256),
        ("translation_sha256", translation_sha256),
    ):
        if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PaidRequestJournalError(
                f"paid request {label} is not a lowercase SHA-256"
            )
    request_payload = copy.deepcopy(dict(request_payload))
    request_sha256 = _sha256_json(request_payload)
    stable = {
        "revision": REVISION,
        "operation_kind": operation_kind,
        "novel_id": novel_id,
        "chapter": chapter,
        "provider": provider,
        "model": model,
        "profile_id": profile_id,
        "source_sha256": source_sha256,
        "translation_sha256": translation_sha256,
        "request_sha256": request_sha256,
    }
    return {
        **stable,
        "operation_id": _sha256_json(stable),
    }


def _record_hash(record: Mapping[str, Any]) -> str:
    return _sha256_json({
        key: copy.deepcopy(value)
        for key, value in dict(record).items()
        if key != "record_sha256"
    })


def _with_record_hash(record: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(record))
    value["record_sha256"] = _record_hash(value)
    return value


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaidRequestJournalError(
            f"cannot read paid request journal {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PaidRequestJournalError(
            f"paid request journal must contain an object: {path}"
        )
    return value


def _validate_common_record(record: Mapping[str, Any], *, path: Path) -> dict[str, Any]:
    value = copy.deepcopy(dict(record))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PaidRequestJournalError(
            f"unsupported paid request journal schema at {path}"
        )
    if value.get("revision") != REVISION:
        raise PaidRequestJournalError(
            f"unsupported paid request journal revision at {path}"
        )
    status = value.get("status")
    if status not in ALLOWED_STATUSES:
        raise PaidRequestJournalError(
            f"invalid paid request journal status {status!r} at {path}"
        )
    expected_hash = value.get("record_sha256")
    if (
        not isinstance(expected_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or _record_hash(value) != expected_hash
    ):
        raise PaidRequestJournalError(
            f"paid request journal hash mismatch at {path}"
        )
    response = value.get("response")
    response_hash = value.get("response_sha256")
    if status == "response_recorded":
        if not isinstance(response, dict):
            raise PaidRequestJournalError(
                f"paid request journal response is missing at {path}"
            )
        if (
            not isinstance(response_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", response_hash)
            or _sha256_json(response) != response_hash
        ):
            raise PaidRequestJournalError(
                f"paid request journal response hash mismatch at {path}"
            )
    else:
        if response not in ({}, None) or response_hash not in ("", None):
            raise PaidRequestJournalError(
                f"non-response paid request journal contains response data at {path}"
            )
    return value


def load_matching_journal(
    path: Path | str,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Load one journal and prove it still represents the exact request."""
    path = Path(path)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PaidRequestJournalError(f"cannot inspect paid request journal {path}") from exc
    if not stat.S_ISREG(mode):
        raise PaidRequestJournalError(f"paid request journal is not a file: {path}")
    value = _validate_common_record(_load_json_object(path), path=path)
    for key, expected in dict(expected_identity).items():
        if value.get(key) != expected:
            raise PaidRequestJournalError(
                f"paid request journal identity mismatch for {key} at {path}: "
                f"expected {expected!r}, found {value.get(key)!r}"
            )
    return value


def _new_planned_record(identity: Mapping[str, Any]) -> dict[str, Any]:
    now = _timestamp()
    return _with_record_hash({
        "schema_version": SCHEMA_VERSION,
        **copy.deepcopy(dict(identity)),
        "status": "planned",
        "attempt_context": {},
        "response": {},
        "response_sha256": "",
        "error": {},
        "created_utc": now,
        "updated_utc": now,
    })


def _updated_record(record: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(dict(record))
    value.update(copy.deepcopy(changes))
    value["updated_utc"] = _timestamp()
    return _with_record_hash(value)


def _persist(
    path: Path,
    record: Mapping[str, Any],
    save_json: Callable[[Path, Mapping[str, Any]], None],
    *,
    expected_identity: Mapping[str, Any],
) -> None:
    # The disk, not the caller's last assignment, owns the transition. In
    # particular, atomic replacement can succeed and then raise before returning.
    # Validate all evidence before any write; malformed/conflicting evidence is
    # never replaced to make recovery easier. The existing single-writer lock
    # contract applies here; this is not a concurrent-writer compare-and-swap.
    current = load_matching_journal(path, expected_identity)
    proposed = _validate_common_record(record, path=path)
    if any(proposed.get(key) != value for key, value in expected_identity.items()):
        raise PaidRequestJournalError(f"paid request journal proposed identity mismatch at {path}")
    if current is None:
        allowed = proposed["status"] == "planned"
    else:
        if current == proposed:
            return
        allowed = proposed["status"] in {
            "planned": {"started"},
            "started": {"response_recorded", "uncertain"},
        }.get(current["status"], set())
    if not allowed:
        raise PaidRequestJournalError(f"paid request journal transition blocked: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, copy.deepcopy(dict(record)))
    except Exception as exc:
        raise LocalPersistenceError(
            f"paid request journal persistence failed: {path}"
        ) from exc


def run_journaled_paid_request(
    *,
    journal_path: Path | str,
    identity: Mapping[str, Any],
    save_json: Callable[[Path, Mapping[str, Any]], None],
    prepare_attempt: Callable[[], Mapping[str, Any]],
    send_once: Callable[[], Any],
    normalize_response: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]],
    authorize_send: Callable[[], ContextManager[Callable[[], Any]]] | None = None,
    observe_request: Callable[[JournalObservation], None] | None = None,
) -> dict[str, Any]:
    """Replay or send once; optional local authorization runs while PLANNED.

    The authorization scope yields the exact bound transport callback and must
    never perform provider IO itself. A refusal leaves PLANNED untouched. The
    scope also releases transient authorization if STARTED persistence fails.
    Failures after STARTED remain uncertain unless a verified response is
    already durable. Stronger or unverified on-disk evidence is never erased.

    Optional observers may refuse before STARTED or replay consumption. They
    receive frozen identity/context/record snapshots, never a send callback.
    Post-response observation is outside the uncertainty handler: a local
    observer failure cannot demote an already durable response or cause retry.
    """
    path = Path(journal_path)
    if observe_request is not None:
        # Bind the observed fixed slot before prepare/authorization callbacks.
        path = path.absolute()
    expected = copy.deepcopy(dict(identity))
    record = load_matching_journal(path, expected)

    def observe(phase, context):
        if observe_request is not None:
            observe_request(JournalObservation(phase=phase,
                journal_path=str(path.absolute()), identity_json=_canonical_json(expected),
                attempt_context_json=_canonical_json(context), record_json=_canonical_json(record)))

    if record is not None:
        status = record["status"]
        if status in {"started", "uncertain"}:
            raise PaidRequestUncertainError(
                "paid provider request has an uncertain remote outcome; "
                f"automatic reissue is blocked: {path}"
            )
        if status == "response_recorded":
            observe("replay", record["attempt_context"])
            return {
                "response": copy.deepcopy(record["response"]),
                "operation_id": record["operation_id"],
                "request_sha256": record["request_sha256"],
                "api_called": False,
                "replayed": True,
                "journal_path": path,
            }
    else:
        record = _new_planned_record(expected)
        _persist(path, record, save_json, expected_identity=expected)

    attempt_context = prepare_attempt()
    if not isinstance(attempt_context, Mapping):
        raise PaidRequestJournalError(
            "paid request prepare_attempt must return a mapping"
        )
    # Freeze before observers and use the same detached context for STARTED and
    # normalization. An observer cannot change the prepared request-start UTC.
    attempt_context = copy.deepcopy(dict(attempt_context))
    observe("before_started", attempt_context)
    authorization = authorize_send() if authorize_send is not None else nullcontext(send_once)
    with authorization as authorized_send:
        if not callable(authorized_send):
            raise PaidRequestJournalError("paid request authorization must yield a send callback")
        record = _updated_record(
            record,
            status="started",
            attempt_context=copy.deepcopy(dict(attempt_context)),
            error={},
        )
        _persist(path, record, save_json, expected_identity=expected)

        try:
            raw_response = authorized_send()
            normalized = normalize_response(
                raw_response,
                copy.deepcopy(dict(attempt_context)),
            )
            if not isinstance(normalized, Mapping):
                raise PaidRequestJournalError(
                    "paid request response normalizer must return a mapping"
                )
            normalized = copy.deepcopy(dict(normalized))
            response_hash = _sha256_json(normalized)
            record = _updated_record(
                record,
                status="response_recorded",
                response=normalized,
                response_sha256=response_hash,
                error={},
            )
            _persist(path, record, save_json, expected_identity=expected)
        except BaseException as exc:
            try:
                uncertain = _updated_record(
                    record,
                    status="uncertain",
                    response={},
                    response_sha256="",
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc)[:1000],
                    },
                )
                _persist(path, uncertain, save_json, expected_identity=expected)
            except Exception as journal_exc:
                exc.add_note(f"Paid journal evidence retained; uncertainty update refused or failed: {journal_exc}")
            raise
    observe("response_recorded", attempt_context)
    return {
        "response": copy.deepcopy(normalized),
        "operation_id": record["operation_id"],
        "request_sha256": record["request_sha256"],
        "api_called": True,
        "replayed": False,
        "journal_path": path,
    }


def inspect_paid_request_journals(journal_dir: Path | str) -> dict[str, Any]:
    """Read all journals without mutation and report blocking uncertain state."""
    root = Path(journal_dir)
    if not root.exists():
        return {"errors": [], "uncertain": [], "records": []}
    if not root.is_dir():
        return {
            "errors": [f"paid request journal path is not a directory: {root}"],
            "uncertain": [],
            "records": [],
        }

    errors: list[str] = []
    uncertain: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.suffix == ".tmp":
            errors.append(f"stale paid request journal temporary file: {path.name}")
            continue
        if not path.is_file() or path.suffix != ".json":
            errors.append(f"unexpected paid request journal entry: {path.name}")
            continue
        try:
            record = _validate_common_record(_load_json_object(path), path=path)
        except PaidRequestJournalError as exc:
            errors.append(str(exc))
            continue
        summary = {
            "file": path.name,
            "operation_kind": record.get("operation_kind"),
            "operation_id": record.get("operation_id"),
            "chapter": record.get("chapter"),
            "status": record.get("status"),
            "model": record.get("model"),
            "request_sha256": record.get("request_sha256"),
        }
        records.append(summary)
        if record["status"] in {"started", "uncertain"}:
            uncertain.append(summary)

    return {
        "errors": errors,
        "uncertain": uncertain,
        "records": records,
    }
