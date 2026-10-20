"""G7K zero-provider fresh-S0 recovery boundary for the first GOAT incident.

G7K preserves the historical Run-1 / Chapter-1 evidence byte-for-byte, creates
an entirely fresh twelve-run G7C S0 generation for the corrected G7J 24k
translation policy, proves that generation is pristine, and materializes a
zero-provider Run-1 / Chapter-1 G7F plan plus a separate recovery binding.

The recovery binding exists because the frozen G7E/G7F executor plan is
intentionally deterministic. A pristine replacement S0 root with the same
ceilings can therefore reproduce the historical G7E approval token. G7K does
not mutate the frozen executor contract merely to manufacture a different
token. Instead it binds any later fresh explicit operator authorization to a
new recovery-generation identifier and recovery-binding SHA-256 that includes
both the corrected fresh root and the consumed historical incident.

Nothing in this module decrypts credentials, constructs a Provider client,
authorizes paid work, executes Chapter 1, retries the historical request, or
modifies the historical Stage-1 root.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence
import uuid

import benchmark_longitudinal_g7c_stage1_integration as g7c
import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7j_reasoning_budget as g7j
from benchmark_longitudinal_config import LIFECYCLE_OPERATION_TRANSLATION
from noveltranslator_core import atomic_write_json


G7K_SCHEMA_VERSION = 1
G7K_REVISION = "goat-g7k-fresh-s0-recovery-v1"

TARGET_RUN_INDEX = 1
TARGET_CHAPTER = 1
EXPECTED_FRESH_S0_FILE_COUNT = 121
# Frozen G7H byte-tree fingerprint of the pristine 12-run Stage-1 generation.
EXPECTED_FRESH_S0_TREE_SHA256 = (
    "12a81545d0a43fabb42462bc45f5b55963be2b5c97a07b49cc3297ea320da0a8"
)

HISTORICAL_MODEL_ID = "deepseek/deepseek-v4.1-flash"
HISTORICAL_REQUEST_SHA256 = (
    "c6c043a03df4e196642f0e408106b7e3c54482d1ba634b3a6a16efbb4cfecc5b"
)
HISTORICAL_RESPONSE_SHA256 = (
    "68f2b400a2daf1cda48f7d6c7fd3a620166a1d8177086224dcf5534f8c44bbe1"
)
HISTORICAL_ACCEPTED_CE_USD = "0.007597068"
HISTORICAL_CONSUMED_APPROVAL_TOKEN = (
    "0e392c30e0f5a9a1db99c30ec9f53adce01bdcf91db33a57e6dd6bfa7cf07a97"
)
HISTORICAL_JOURNAL_RELATIVE = (
    "data/provider_request_journal/0001.g7c_translation.json"
)
HISTORICAL_LEDGER_RELATIVE = "data/g7f_invocation_ledgers/0001.json"

# Frozen forensic identity of the real post-incident historical Run-1 tree: the
# byte-tree fingerprint of the complete 125-file Stage-1 root left behind by the
# first GOAT paid Run-1/Chapter-1 incident.  Any drift in these two literals'
# observed values fails closed before anything is copied.
HISTORICAL_STAGE1_FILE_COUNT = 125
HISTORICAL_STAGE1_TREE_SHA256 = (
    "7105190009524ca4a7742ad5931addabd05f7621f28de3bc0a0b368f5b28ffe7"
)

# The ten run-relative files a pristine zero-provider G7C S0 Run-1 root holds.
PRISTINE_RUN1_BASE_RELATIVES = (
    "data/audit_history.json",
    "data/chapters.json",
    "data/continuity.json",
    "data/glossary.json",
    "data/issues.json",
    "data/progress.json",
    "g7c_run_identity.json",
    "novel.json",
    "source/novel.epub",
    "translation_addendum.txt",
)

# The FOUR proven post-S0 incident artifacts.  Every identity field is a frozen
# forensic literal (size and SHA-256), never a wildcard, glob, or computed
# pattern: recognition is by exact path, exact size, and exact bytes.
HISTORICAL_INCIDENT_ARTIFACTS = (
    {
        "relative_posix": ".production.lock",
        "size_bytes": 0,
        "sha256": (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ),
    },
    {
        "relative_posix": "data/g7f_invocation_ledgers/0001.json",
        "size_bytes": 2230,
        "sha256": (
            "ae6ebb7c0eab47fd8b308cb22a209f861f750c6d43045102c31386237427dbb5"
        ),
    },
    {
        "relative_posix": "data/provider_request_journal/0001.g7c_translation.json",
        "size_bytes": 2601,
        "sha256": (
            "5aa79041d4b00523d064d8d49adf6b4767726a901402cf656c93d9f04aa2f719"
        ),
    },
    {
        "relative_posix": "logs/run_20260922_052555.jsonl",
        "size_bytes": 1099,
        "sha256": (
            "474b0a74fe745f6aec83fd07366325778db16268df9703a099fe1c7cf6d5101d"
        ),
    },
)

# Exactly the ten pristine base paths union the four incident artifacts.
EXPECTED_RUN1_RELATIVE_FILES = frozenset(PRISTINE_RUN1_BASE_RELATIVES) | {
    artifact["relative_posix"] for artifact in HISTORICAL_INCIDENT_ARTIFACTS
}

# The G7F ledger directory itself (empty after the ledger file is removed) is
# the final removed entry; it must stay in sync with the executor constant.
REMOVED_LEDGER_DIRECTORY_RELATIVE = "data/g7f_invocation_ledgers"

RECOVERY_MANIFEST_FILENAME = "G7K_RECOVERY_MANIFEST.json"
RECOVERY_PLAN_FILENAME = "G7K_RUN1_CHAPTER1_PLAN.json"

RAW_PASS = "G7K_RECOVERY=PASS"
RAW_FAIL = "G7K_RECOVERY=FAIL"
RAW_PROVIDER_CALLS = "BENCHMARK_PROVIDER_CALLS=0"
RAW_CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS=NO"
RAW_PAID_AUTHORIZED = "PAID_EXECUTION_AUTHORIZED=NO"
RAW_CHAPTER_EXECUTED = "CHAPTER_1_EXECUTED=NO"
RAW_HISTORICAL_MODIFIED = "HISTORICAL_STAGE1_MODIFIED=NO"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


class G7KRecoveryError(RuntimeError):
    """Fresh-S0 recovery evidence is missing, inconsistent, or unsafe."""


def _assert_frozen_incident_artifact_model() -> None:
    """Fail closed at import if the frozen incident artifact model is inconsistent."""
    relatives = [
        artifact["relative_posix"] for artifact in HISTORICAL_INCIDENT_ARTIFACTS
    ]
    if len(relatives) != len(set(relatives)):
        raise G7KRecoveryError("frozen incident artifact model repeats a path")
    if HISTORICAL_JOURNAL_RELATIVE not in relatives:
        raise G7KRecoveryError(
            "HISTORICAL_JOURNAL_RELATIVE must be a frozen incident artifact"
        )
    if HISTORICAL_LEDGER_RELATIVE not in relatives:
        raise G7KRecoveryError(
            "HISTORICAL_LEDGER_RELATIVE must be a frozen incident artifact"
        )
    if REMOVED_LEDGER_DIRECTORY_RELATIVE != g7f.LEDGER_DIRECTORY:
        raise G7KRecoveryError(
            "REMOVED_LEDGER_DIRECTORY_RELATIVE must stay in sync with g7f.LEDGER_DIRECTORY"
        )
    if len(EXPECTED_RUN1_RELATIVE_FILES) != (
        len(PRISTINE_RUN1_BASE_RELATIVES) + len(HISTORICAL_INCIDENT_ARTIFACTS)
    ):
        raise G7KRecoveryError(
            "the pristine Run-1 base set and the incident artifact set must not overlap"
        )
    for artifact in HISTORICAL_INCIDENT_ARTIFACTS:
        if not _SHA256_RE.fullmatch(str(artifact.get("sha256"))):
            raise G7KRecoveryError("frozen incident artifact SHA-256 is invalid")
        size = artifact.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise G7KRecoveryError("frozen incident artifact size is invalid")


_assert_frozen_incident_artifact_model()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G7KRecoveryError("value is not canonically JSON-serializable") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise G7KRecoveryError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_object(path: Path | str, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G7KRecoveryError(f"cannot read {label}: {target}") from exc
    if not isinstance(value, dict):
        raise G7KRecoveryError(f"{label} must contain a JSON object: {target}")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise G7KRecoveryError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_generation_id(value: Any) -> str:
    if not isinstance(value, str) or not _GENERATION_RE.fullmatch(value):
        raise G7KRecoveryError(
            "recovery_generation_id must be 8..128 safe characters "
            "(letters, digits, dot, underscore, hyphen)"
        )
    return value


def _path_is_within(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or root in path.parents


def _assert_separate_roots(
    *,
    production_root: Path,
    historical_stage1_root: Path,
    fresh_stage1_root: Path,
    evidence_root: Path,
) -> None:
    values = {
        "production": production_root.resolve(),
        "historical": historical_stage1_root.resolve(),
        "fresh": fresh_stage1_root.resolve(),
        "evidence": evidence_root.resolve(),
    }
    if len(set(values.values())) != len(values):
        raise G7KRecoveryError("production/historical/fresh/evidence roots must differ")

    if _path_is_within(fresh_stage1_root, historical_stage1_root) or _path_is_within(
        historical_stage1_root, fresh_stage1_root
    ):
        raise G7KRecoveryError("fresh and historical Stage-1 roots must not nest")

    if _path_is_within(evidence_root, historical_stage1_root) or _path_is_within(
        historical_stage1_root, evidence_root
    ):
        raise G7KRecoveryError("evidence root must not overlap the historical root")

    if _path_is_within(evidence_root, fresh_stage1_root) or _path_is_within(
        fresh_stage1_root, evidence_root
    ):
        raise G7KRecoveryError("evidence root must not overlap the fresh root")


def tree_fingerprint(root: Path | str) -> dict[str, Any]:
    """Return the same relative-path byte-tree fingerprint used by G7H."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise G7KRecoveryError(f"tree root is not a directory: {root_path}")

    rows: list[dict[str, Any]] = []
    try:
        files = sorted(
            (item for item in root_path.rglob("*") if item.is_file()),
            key=lambda item: str(item.resolve()),
        )
    except OSError as exc:
        raise G7KRecoveryError(f"cannot enumerate tree {root_path}: {exc}") from exc

    for path in files:
        resolved = path.resolve()
        rows.append(
            {
                "relative_posix": resolved.relative_to(root_path).as_posix(),
                "sha256": _sha256_file(resolved),
                "size": resolved.stat().st_size,
            }
        )

    return {
        "root": str(root_path),
        "file_count": len(rows),
        "fingerprint": _sha256_text(
            "\n".join(
                f"{row['relative_posix']}\t{row['size']}\t{row['sha256']}"
                for row in rows
            )
        ),
    }


def _run_root(stage1_root: Path | str, run_index: int = TARGET_RUN_INDEX) -> Path:
    specs = g7c.stage1_run_specs()
    if run_index < 1 or run_index > len(specs):
        raise G7KRecoveryError("run index is out of range")
    root = Path(stage1_root).resolve()
    candidate = (root / Path(specs[run_index - 1]["relative_root"])).resolve()
    if not _path_is_within(candidate, root):
        raise G7KRecoveryError("selected run root escapes Stage-1 root")
    if not candidate.is_dir():
        raise G7KRecoveryError(f"selected run root is missing: {candidate}")
    return candidate


def _verify_record_hash(record: Mapping[str, Any], label: str) -> None:
    value = copy.deepcopy(dict(record))
    stored = value.pop("record_sha256", None)
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7KRecoveryError(f"{label} record_sha256 is invalid")
    if _sha256_json(value) != stored:
        raise G7KRecoveryError(f"{label} record_sha256 does not recompute")


def _verify_ledger_hash(document: Mapping[str, Any]) -> None:
    value = copy.deepcopy(dict(document))
    stored = value.pop("ledger_sha256", None)
    if not isinstance(stored, str) or not _SHA256_RE.fullmatch(stored):
        raise G7KRecoveryError("historical ledger_sha256 is invalid")
    if _sha256_json(value) != stored:
        raise G7KRecoveryError("historical ledger_sha256 does not recompute")


def verify_historical_incident(stage1_root: Path | str) -> dict[str, Any]:
    """Verify the exact accepted Run-1 incident without mutating it."""
    root = Path(stage1_root).resolve()
    if not root.is_dir():
        raise G7KRecoveryError(f"historical Stage-1 root missing: {root}")

    run_root = _run_root(root)
    progress = _read_object(run_root / "data" / "progress.json", "historical progress")
    if progress.get("last_completed") != 0 or progress.get("next_chapter") != 1:
        raise G7KRecoveryError("historical Run-1 progress no longer points to Chapter 1")

    journal_path = run_root / Path(HISTORICAL_JOURNAL_RELATIVE)
    journal = _read_object(journal_path, "historical paid journal")
    _verify_record_hash(journal, "historical journal")

    if journal.get("operation_kind") != LIFECYCLE_OPERATION_TRANSLATION:
        raise G7KRecoveryError("historical journal operation kind changed")
    if journal.get("chapter") != TARGET_CHAPTER:
        raise G7KRecoveryError("historical journal chapter changed")
    if journal.get("model") != HISTORICAL_MODEL_ID:
        raise G7KRecoveryError("historical journal model changed")
    if journal.get("status") != "response_recorded":
        raise G7KRecoveryError("historical journal is no longer response_recorded")
    if journal.get("request_sha256") != HISTORICAL_REQUEST_SHA256:
        raise G7KRecoveryError("historical request SHA-256 changed")
    if journal.get("response_sha256") != HISTORICAL_RESPONSE_SHA256:
        raise G7KRecoveryError("historical response SHA-256 changed")

    response = journal.get("response")
    if not isinstance(response, Mapping):
        raise G7KRecoveryError("historical durable response is missing")
    if _sha256_json(dict(response)) != HISTORICAL_RESPONSE_SHA256:
        raise G7KRecoveryError("historical durable response body hash changed")
    if response.get("content") != "":
        raise G7KRecoveryError("historical durable visible content is no longer empty")
    if response.get("finish_reason") != "length":
        raise G7KRecoveryError("historical finish_reason changed")
    stats = response.get("stats")
    if not isinstance(stats, Mapping):
        raise G7KRecoveryError("historical durable response lacks accounting stats")
    if stats.get("prompt_tokens") != 6058:
        raise G7KRecoveryError("historical prompt token count changed")
    if stats.get("completion_tokens") != 11994:
        raise G7KRecoveryError("historical completion token count changed")

    ledger_path = run_root / Path(HISTORICAL_LEDGER_RELATIVE)
    ledger = _read_object(ledger_path, "historical G7F ledger")
    _verify_ledger_hash(ledger)
    if ledger.get("run_index") != TARGET_RUN_INDEX:
        raise G7KRecoveryError("historical ledger run index changed")
    if ledger.get("target_chapter") != TARGET_CHAPTER:
        raise G7KRecoveryError("historical ledger target chapter changed")
    if ledger.get("model_id") != HISTORICAL_MODEL_ID:
        raise G7KRecoveryError("historical ledger model changed")
    if ledger.get("approval_token") != HISTORICAL_CONSUMED_APPROVAL_TOKEN:
        raise G7KRecoveryError("historical consumed approval token changed")
    if ledger.get("new_provider_sends_started") != 1:
        raise G7KRecoveryError("historical ledger send count changed")
    if ledger.get("accepted_credit_equivalent_usd") != HISTORICAL_ACCEPTED_CE_USD:
        raise G7KRecoveryError("historical accepted credit-equivalent cost changed")

    records = ledger.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise G7KRecoveryError("historical ledger must contain exactly one send record")
    send = records[0]
    if not isinstance(send, Mapping):
        raise G7KRecoveryError("historical ledger send record is malformed")
    if send.get("status") != "accepted":
        raise G7KRecoveryError("historical ledger send is no longer accepted")
    if send.get("logical_operation") != LIFECYCLE_OPERATION_TRANSLATION:
        raise G7KRecoveryError("historical ledger logical operation changed")
    if send.get("chapter") != TARGET_CHAPTER:
        raise G7KRecoveryError("historical ledger send chapter changed")
    if send.get("request_sha256") != HISTORICAL_REQUEST_SHA256:
        raise G7KRecoveryError("historical ledger request SHA-256 changed")
    if send.get("accepted_cost_usd") != HISTORICAL_ACCEPTED_CE_USD:
        raise G7KRecoveryError("historical ledger accepted cost changed")

    send_stats = send.get("stats")
    if not isinstance(send_stats, Mapping):
        raise G7KRecoveryError("historical accepted send lacks accounting stats")
    if dict(send_stats) != dict(stats):
        raise G7KRecoveryError("historical journal/ledger accounting stats diverged")

    for relative in ("output/chapters", "output/pending"):
        directory = run_root / relative
        if not directory.is_dir():
            raise G7KRecoveryError(f"historical output directory missing: {directory}")
        if any(item.is_file() for item in directory.rglob("*")):
            raise G7KRecoveryError(
                "historical Chapter-1 output appeared after the failed incident"
            )

    root_identity = tree_fingerprint(root)
    return {
        "stage1_tree": root_identity,
        "run_root": str(run_root),
        "progress": {"last_completed": 0, "next_chapter": 1},
        "journal_path": str(journal_path),
        "journal_file_sha256": _sha256_file(journal_path),
        "request_sha256": HISTORICAL_REQUEST_SHA256,
        "response_sha256": HISTORICAL_RESPONSE_SHA256,
        "ledger_path": str(ledger_path),
        "ledger_file_sha256": _sha256_file(ledger_path),
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": HISTORICAL_ACCEPTED_CE_USD,
        "consumed_approval_token": HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "pending_artifact_present": False,
        "final_artifact_present": False,
    }


def _verify_frozen_source_bindings(
    *,
    production_root: Path | str,
    stage1_manifest: Mapping[str, Any],
) -> None:
    """Verify only immutable source/config/addendum bindings in production.

    G7K intentionally does not require current mutable production data/output
    trees to equal the historical G7C production guard. Production has continued
    to evolve independently since S0 was frozen. The replacement benchmark root
    instead reproduces the exact historical S0 bytes and independently proves
    that the authoritative source/config/chapter/addendum inputs still match the
    frozen G7C manifest.
    """
    production = Path(production_root).resolve()
    source_bindings = stage1_manifest.get("source_bindings")
    if not isinstance(source_bindings, Mapping):
        raise G7KRecoveryError("fresh G7C manifest lacks source bindings")

    novel_ids = tuple(dict.fromkeys(spec["novel_id"] for spec in g7c.stage1_run_specs()))
    for novel_id in novel_ids:
        frozen = source_bindings.get(novel_id)
        if not isinstance(frozen, Mapping):
            raise G7KRecoveryError(f"fresh manifest lacks source binding for {novel_id}")
        try:
            current = g7c._validate_source_project(production, novel_id)
        except Exception as exc:
            raise G7KRecoveryError(
                f"current production immutable source binding failed for {novel_id}: {exc}"
            ) from exc

        expected = {
            "config_sha256": current["config_sha256"],
            "source_sha256": current["source_sha256"],
            "chapter_library_sha256": current["chapter_library_sha256"],
            "addendum_sha256": current["addendum_sha256"],
        }
        for field, value in expected.items():
            if frozen.get(field) != value:
                raise G7KRecoveryError(
                    f"current production {novel_id} immutable binding changed: {field}"
                )


def verify_fresh_s0(
    *,
    production_root: Path | str,
    fresh_stage1_root: Path | str,
) -> dict[str, Any]:
    """Prove the replacement root is the exact frozen pristine G7C S0 tree."""
    root = Path(fresh_stage1_root).resolve()
    if not root.is_dir():
        raise G7KRecoveryError(f"fresh Stage-1 root missing: {root}")

    manifest_path = root / g7c.ROOTS_MANIFEST_FILENAME
    manifest = _read_object(manifest_path, "fresh G7C Stage-1 roots manifest")
    check = copy.deepcopy(manifest)
    observed_manifest_sha = check.pop("manifest_sha256", None)
    if not isinstance(observed_manifest_sha, str) or not _SHA256_RE.fullmatch(
        observed_manifest_sha
    ):
        raise G7KRecoveryError("fresh G7C manifest SHA-256 is invalid")
    if _sha256_json(check) != observed_manifest_sha:
        raise G7KRecoveryError("fresh G7C manifest SHA-256 does not recompute")
    if observed_manifest_sha != g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256:
        raise G7KRecoveryError("fresh G7C manifest differs from the frozen S0 manifest")
    if manifest.get("g7c_integration_sha256") != g7c.G7C_INTEGRATION_SHA256:
        raise G7KRecoveryError("fresh G7C manifest binds another integration identity")
    if manifest.get("run_count") != 12:
        raise G7KRecoveryError("fresh G7C manifest does not contain twelve runs")
    if manifest.get("provider_calls") != 0:
        raise G7KRecoveryError("fresh G7C manifest reports Provider calls")
    if manifest.get("paid_stage1_authorized") is not False:
        raise G7KRecoveryError("fresh G7C manifest authorizes paid Stage-1")

    identity = tree_fingerprint(root)
    if identity["file_count"] != EXPECTED_FRESH_S0_FILE_COUNT:
        raise G7KRecoveryError(
            "fresh S0 file count changed: "
            f"{identity['file_count']} != {EXPECTED_FRESH_S0_FILE_COUNT}"
        )
    if identity["fingerprint"] != EXPECTED_FRESH_S0_TREE_SHA256:
        raise G7KRecoveryError("fresh S0 deterministic tree fingerprint changed")

    specs = g7c.stage1_run_specs()
    expected_relatives = {spec["relative_root"] for spec in specs}
    observed_relatives = {
        item.get("relative_root")
        for item in manifest.get("runs", [])
        if isinstance(item, Mapping)
    }
    if observed_relatives != expected_relatives:
        raise G7KRecoveryError("fresh external run-root set changed")

    for spec in specs:
        run_root = root / Path(spec["relative_root"])
        if not run_root.is_dir():
            raise G7KRecoveryError(f"fresh run root missing: {run_root}")
        run_identity = _read_object(
            run_root / g7c.RUN_IDENTITY_FILENAME,
            "fresh G7C run identity",
        )
        if run_identity.get("model_id") != spec["model_id"]:
            raise G7KRecoveryError(f"fresh run model identity changed: {run_root}")
        if run_identity.get("novel_id") != spec["novel_id"]:
            raise G7KRecoveryError(f"fresh run novel identity changed: {run_root}")
        if run_identity.get("run_sha256") != spec["run_sha256"]:
            raise G7KRecoveryError(f"fresh run SHA changed: {run_root}")
        if run_identity.get("s0_identity_sha256") != spec["s0_identity_sha256"]:
            raise G7KRecoveryError(f"fresh S0 identity changed: {run_root}")

        identity_check = copy.deepcopy(run_identity)
        stored_identity_sha = identity_check.pop("identity_sha256", None)
        if _sha256_json(identity_check) != stored_identity_sha:
            raise G7KRecoveryError(f"fresh run identity SHA does not recompute: {run_root}")

        progress = _read_object(run_root / "data" / "progress.json", "fresh progress")
        if progress.get("last_completed") != 0 or progress.get("next_chapter") != 1:
            raise G7KRecoveryError(f"fresh run progress is not pristine S0: {run_root}")

        source_binding = manifest["source_bindings"][spec["novel_id"]]
        if _sha256_file(run_root / "novel.json") != source_binding["config_sha256"]:
            raise G7KRecoveryError(f"fresh novel config changed: {run_root}")
        if _sha256_file(run_root / "data" / "chapters.json") != source_binding[
            "chapter_library_sha256"
        ]:
            raise G7KRecoveryError(f"fresh chapter library changed: {run_root}")
        try:
            staged_source = g7c._source_file_for_project(run_root)
        except Exception as exc:
            raise G7KRecoveryError(
                f"cannot resolve fresh staged source file: {run_root}"
            ) from exc
        if _sha256_file(staged_source) != source_binding["source_sha256"]:
            raise G7KRecoveryError(f"fresh staged source changed: {run_root}")
        addendum = run_root / "translation_addendum.txt"
        addendum_sha = _sha256_file(addendum) if addendum.is_file() else _sha256_text("")
        if addendum_sha != source_binding["addendum_sha256"]:
            raise G7KRecoveryError(f"fresh translation addendum changed: {run_root}")

        for relative in (
            "output/chapters",
            "output/pending",
            "output/qa",
            "output/audits",
            "output/retroactive_fixes",
            "data/provider_request_journal",
            "data/glossary_candidate_resolutions",
        ):
            directory = run_root / relative
            if not directory.is_dir():
                raise G7KRecoveryError(f"fresh S0 directory missing: {directory}")
            if any(directory.iterdir()):
                raise G7KRecoveryError(f"fresh S0 directory is not empty: {directory}")

        ledger_dir = run_root / Path(g7f.LEDGER_DIRECTORY)
        if ledger_dir.exists():
            if any(ledger_dir.iterdir()):
                raise G7KRecoveryError(
                    f"fresh S0 contains G7F paid ledger state: {run_root}"
                )
            raise G7KRecoveryError(
                f"fresh S0 unexpectedly contains an empty G7F ledger directory: {run_root}"
            )

    _verify_frozen_source_bindings(
        production_root=production_root,
        stage1_manifest=manifest,
    )

    return {
        "g7c_manifest_sha256": observed_manifest_sha,
        "run_count": len(specs),
        "frozen_source_bindings_verified": True,
        "historical_manifest_reproduced": True,
        "provider_calls": 0,
        "paid_stage1_authorized": False,
        "tree": identity,
    }


def _enumerate_relative_files(root: Path) -> set[str]:
    """Return the posix-relative path set of every file under an already-resolved root."""
    try:
        return {
            item.resolve().relative_to(root).as_posix()
            for item in root.rglob("*")
            if item.is_file()
        }
    except OSError as exc:
        raise G7KRecoveryError(f"cannot enumerate tree {root}: {exc}") from exc


def _root_level_file_names(root: Path) -> set[str]:
    """Return the names of the direct file children of an already-resolved root."""
    try:
        return {item.name for item in root.iterdir() if item.is_file()}
    except OSError as exc:
        raise G7KRecoveryError(f"cannot enumerate Stage-1 root {root}: {exc}") from exc


def reconstruct_fresh_s0_from_historical(
    *,
    historical_stage1_root: Path | str,
    fresh_stage1_root: Path | str,
) -> dict[str, Any]:
    """Reconstruct the exact pre-run S0 by copying history and removing only the four proven incident artifacts.

    The historical root itself is never modified. The copy is accepted only if
    recognizing and deleting the FOUR proven post-S0 Run-1 incident artifacts --
    the empty `.production.lock`, the single Run-1 G7F ledger
    (`data/g7f_invocation_ledgers/0001.json`), the single Run-1 translation
    journal (`data/provider_request_journal/0001.g7c_translation.json`), and the
    single Run-1 log file (`logs/run_20260922_052555.jsonl`) -- plus the
    now-empty G7F ledger directory restores the exact frozen 121-file S0
    byte-tree fingerprint. The emptied `logs/` directory is deliberately kept.

    No wildcard, glob, or computed pattern participates in recognition or
    removal. In addition to the frozen 125-file historical tree identity gate
    and the frozen SHA-256/size literal of every artifact, the historical Run-1
    relative file set must be exactly the ten pristine base paths union the four
    artifact paths, and the Stage-1 root must hold exactly the G7C roots
    manifest, so any missing, modified, unexpected, or otherwise drifted
    historical byte fails closed before the copy is accepted.
    """
    historical = Path(historical_stage1_root).resolve()
    fresh = Path(fresh_stage1_root).resolve()
    if not historical.is_dir():
        raise G7KRecoveryError(f"historical Stage-1 root missing: {historical}")
    if fresh.exists():
        raise G7KRecoveryError(f"fresh Stage-1 root already exists: {fresh}")

    incident = verify_historical_incident(historical)

    historical_tree = incident.get("stage1_tree")
    if not isinstance(historical_tree, Mapping):
        raise G7KRecoveryError("historical incident evidence lacks a Stage-1 tree identity")
    if historical_tree.get("file_count") != HISTORICAL_STAGE1_FILE_COUNT:
        raise G7KRecoveryError(
            "historical Stage-1 tree does not match the frozen incident tree: "
            f"file_count {historical_tree.get('file_count')} "
            f"!= {HISTORICAL_STAGE1_FILE_COUNT}"
        )
    if historical_tree.get("fingerprint") != HISTORICAL_STAGE1_TREE_SHA256:
        raise G7KRecoveryError(
            "historical Stage-1 tree does not match the frozen incident tree: "
            f"fingerprint {historical_tree.get('fingerprint')} "
            f"!= {HISTORICAL_STAGE1_TREE_SHA256}"
        )

    run_root = _run_root(historical)
    observed_run1_files = _enumerate_relative_files(run_root)
    if observed_run1_files != set(EXPECTED_RUN1_RELATIVE_FILES):
        unexpected = sorted(observed_run1_files - set(EXPECTED_RUN1_RELATIVE_FILES))
        missing = sorted(set(EXPECTED_RUN1_RELATIVE_FILES) - observed_run1_files)
        raise G7KRecoveryError(
            "historical Run-1 file set is not the frozen incident set: "
            f"unexpected={unexpected} missing={missing}"
        )

    observed_root_level = _root_level_file_names(historical)
    if observed_root_level != {g7c.ROOTS_MANIFEST_FILENAME}:
        raise G7KRecoveryError(
            "historical Stage-1 root-level file set is not the frozen roots "
            f"manifest alone: {sorted(observed_root_level)}"
        )

    artifact_identities: list[dict[str, Any]] = []
    for artifact in HISTORICAL_INCIDENT_ARTIFACTS:
        relative_posix = artifact["relative_posix"]
        artifact_path = run_root / Path(relative_posix)
        if not artifact_path.is_file():
            raise G7KRecoveryError(
                f"historical incident artifact is missing: {relative_posix}"
            )
        observed_size = artifact_path.stat().st_size
        if observed_size != artifact["size_bytes"]:
            raise G7KRecoveryError(
                "historical incident artifact size changed: "
                f"{relative_posix}: {observed_size} != {artifact['size_bytes']}"
            )
        observed_sha = _sha256_file(artifact_path)
        if observed_sha != artifact["sha256"]:
            raise G7KRecoveryError(
                "historical incident artifact SHA-256 changed: "
                f"{relative_posix}: {observed_sha} != {artifact['sha256']}"
            )
        artifact_identities.append(
            {
                "relative_posix": relative_posix,
                "size_bytes": observed_size,
                "sha256": observed_sha,
            }
        )

    fresh.parent.mkdir(parents=True, exist_ok=True)
    staging = fresh.parent / f".{fresh.name}.reconstructing-{uuid.uuid4().hex}"
    try:
        shutil.copytree(historical, staging)

        staged_run_root = _run_root(staging)
        for identity in artifact_identities:
            relative_posix = identity["relative_posix"]
            staged_artifact = staged_run_root / Path(relative_posix)
            if not staged_artifact.is_file():
                raise G7KRecoveryError(
                    f"copied historical incident artifact is missing: {relative_posix}"
                )
            staged_size = staged_artifact.stat().st_size
            if staged_size != identity["size_bytes"]:
                raise G7KRecoveryError(
                    "copied historical incident artifact size changed: "
                    f"{relative_posix}: {staged_size} != {identity['size_bytes']}"
                )
            if _sha256_file(staged_artifact) != identity["sha256"]:
                raise G7KRecoveryError(
                    "copied historical incident artifact bytes changed: "
                    f"{relative_posix}"
                )

        staged_journal = staged_run_root / Path(HISTORICAL_JOURNAL_RELATIVE)
        staged_ledger = staged_run_root / Path(HISTORICAL_LEDGER_RELATIVE)
        if _sha256_file(staged_journal) != incident["journal_file_sha256"]:
            raise G7KRecoveryError("copied historical journal bytes changed")
        if _sha256_file(staged_ledger) != incident["ledger_file_sha256"]:
            raise G7KRecoveryError("copied historical ledger bytes changed")

        for identity in artifact_identities:
            (staged_run_root / Path(identity["relative_posix"])).unlink()

        staged_ledger_dir = staged_run_root / Path(REMOVED_LEDGER_DIRECTORY_RELATIVE)
        if not staged_ledger_dir.is_dir():
            raise G7KRecoveryError("copied Run-1 G7F ledger directory is missing")
        if any(staged_ledger_dir.iterdir()):
            raise G7KRecoveryError("staged G7F ledger directory contains unexpected entries")
        staged_ledger_dir.rmdir()

        staged_logs_dir = staged_run_root / "logs"
        if not staged_logs_dir.is_dir():
            raise G7KRecoveryError("copied Run-1 log directory is missing")
        if any(staged_logs_dir.iterdir()):
            raise G7KRecoveryError(
                "staged Run-1 log directory still contains incident artifacts"
            )

        remaining_run1_files = _enumerate_relative_files(staged_run_root)
        if remaining_run1_files != set(PRISTINE_RUN1_BASE_RELATIVES):
            unexpected = sorted(
                remaining_run1_files - set(PRISTINE_RUN1_BASE_RELATIVES)
            )
            missing = sorted(
                set(PRISTINE_RUN1_BASE_RELATIVES) - remaining_run1_files
            )
            raise G7KRecoveryError(
                "staged Run-1 tree is not the pristine S0 base set: "
                f"unexpected={unexpected} missing={missing}"
            )

        restored = tree_fingerprint(staging)
        if restored["file_count"] != EXPECTED_FRESH_S0_FILE_COUNT:
            raise G7KRecoveryError(
                "historical incident artifact removal did not restore frozen S0 file count"
            )
        if restored["fingerprint"] != EXPECTED_FRESH_S0_TREE_SHA256:
            raise G7KRecoveryError(
                "historical incident artifact removal did not restore frozen S0 tree"
            )

        os.rename(staging, fresh)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        "method": "historical_exact_copy_minus_allowlisted_incident_artifacts",
        "removed_from_copy": [
            artifact["relative_posix"] for artifact in HISTORICAL_INCIDENT_ARTIFACTS
        ]
        + [f"{REMOVED_LEDGER_DIRECTORY_RELATIVE}/"],
        "provider_calls": 0,
        "credential_access": False,
        "historical_stage1_modified": False,
        "tree": tree_fingerprint(fresh),
    }


def g7k_policy_identity() -> dict[str, Any]:
    """Return the deterministic zero-provider G7K recovery contract."""
    if g7j.HISTORICAL_RUN_1_MODEL_ID != HISTORICAL_MODEL_ID:
        raise G7KRecoveryError("G7J historical model identity changed")
    if g7j.HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS != 12000:
        raise G7KRecoveryError("G7J historical completion ceiling changed")
    if g7j.GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS != 24000:
        raise G7KRecoveryError("G7J corrected completion ceiling changed")
    if g7j.HISTORICAL_RUN_1_CONSUMED_PAID_AUTHORIZATION_REUSABLE is not False:
        raise G7KRecoveryError("G7J unexpectedly marks consumed authorization reusable")

    return {
        "schema_version": G7K_SCHEMA_VERSION,
        "revision": G7K_REVISION,
        "predecessors": {
            "g7c_integration_sha256": g7c.G7C_INTEGRATION_SHA256,
            "g7f_executor_sha256": g7f.G7F_EXECUTOR_SHA256,
            "g7j_policy_sha256": g7j.G7J_POLICY_SHA256,
        },
        "historical_incident_binding": {
            "run_index": TARGET_RUN_INDEX,
            "chapter": TARGET_CHAPTER,
            "model_id": HISTORICAL_MODEL_ID,
            "request_sha256": HISTORICAL_REQUEST_SHA256,
            "response_sha256": HISTORICAL_RESPONSE_SHA256,
            "accepted_credit_equivalent_usd": HISTORICAL_ACCEPTED_CE_USD,
            "consumed_approval_token": HISTORICAL_CONSUMED_APPROVAL_TOKEN,
            "consumed_authorization_reusable": False,
        },
        "fresh_generation_requirement": {
            "all_twelve_runs_recreated": True,
            "reconstruction_method": (
                "historical_exact_copy_minus_allowlisted_incident_artifacts"
            ),
            "historical_root_never_overwritten": True,
            "current_mutable_production_guard_not_required_to_match_historical": True,
            "current_immutable_source_bindings_must_match_frozen_manifest": True,
            "g7c_manifest_must_reproduce": g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
            "expected_file_count": EXPECTED_FRESH_S0_FILE_COUNT,
            "expected_tree_sha256": EXPECTED_FRESH_S0_TREE_SHA256,
            "pristine_progress_required": True,
            "empty_paid_journals_required": True,
            "empty_paid_ledgers_required": True,
        },
        "authorization_evidence": {
            "g7e_g7f_executor_tokens_remain_frozen_and_deterministic": True,
            "executor_token_may_repeat_on_identical_pristine_state": True,
            "fresh_operator_authorization_must_bind_recovery_generation": True,
            "fresh_operator_authorization_must_bind_recovery_sha256": True,
            "historical_authorization_never_reused": True,
        },
        "provider_boundary": {
            "provider_calls_authorized": False,
            "provider_calls": 0,
            "credential_access": False,
            "chapter_execution": False,
            "retry_authorized": False,
        },
    }


G7K_POLICY_SHA256 = _sha256_json(g7k_policy_identity())


def _recovery_binding_payload(
    *,
    recovery_generation_id: str,
    historical: Mapping[str, Any],
    fresh: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "revision": G7K_REVISION,
        "g7k_policy_sha256": G7K_POLICY_SHA256,
        "recovery_generation_id": recovery_generation_id,
        "historical_stage1_tree_sha256": historical["stage1_tree"]["fingerprint"],
        "historical_request_sha256": HISTORICAL_REQUEST_SHA256,
        "historical_response_sha256": HISTORICAL_RESPONSE_SHA256,
        "historical_consumed_approval_token": HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "fresh_stage1_tree_sha256": fresh["tree"]["fingerprint"],
        "fresh_g7c_manifest_sha256": fresh["g7c_manifest_sha256"],
        "fresh_g7f_plan_sha256": plan["plan_sha256"],
        "fresh_g7e_approval_token": plan["approval_token"],
        "run_index": TARGET_RUN_INDEX,
        "target_chapter": TARGET_CHAPTER,
    }


def prepare_fresh_s0_recovery(
    *,
    production_root: Path | str,
    historical_stage1_root: Path | str,
    fresh_stage1_root: Path | str,
    evidence_root: Path | str,
    recovery_generation_id: str,
    maximum_new_provider_sends: int,
    maximum_credit_equivalent_usd: Any,
) -> dict[str, Any]:
    """Create one fresh G7C generation and zero-provider recovery evidence."""
    generation_id = _validate_generation_id(recovery_generation_id)
    production = Path(production_root).resolve()
    historical_root = Path(historical_stage1_root).resolve()
    fresh_root = Path(fresh_stage1_root).resolve()
    evidence = Path(evidence_root).resolve()

    if not production.is_dir():
        raise G7KRecoveryError(f"production root missing: {production}")
    if not historical_root.is_dir():
        raise G7KRecoveryError(f"historical Stage-1 root missing: {historical_root}")
    if fresh_root.exists():
        raise G7KRecoveryError(f"fresh Stage-1 root already exists: {fresh_root}")
    if evidence.exists():
        raise G7KRecoveryError(f"G7K evidence root already exists: {evidence}")

    _assert_separate_roots(
        production_root=production,
        historical_stage1_root=historical_root,
        fresh_stage1_root=fresh_root,
        evidence_root=evidence,
    )

    historical_before = verify_historical_incident(historical_root)

    reconstructed = reconstruct_fresh_s0_from_historical(
        historical_stage1_root=historical_root,
        fresh_stage1_root=fresh_root,
    )
    if reconstructed.get("provider_calls") != 0:
        raise G7KRecoveryError("fresh S0 reconstruction unexpectedly reported Provider calls")
    if reconstructed.get("historical_stage1_modified") is not False:
        raise G7KRecoveryError("fresh S0 reconstruction claims historical mutation")

    fresh = verify_fresh_s0(
        production_root=production,
        fresh_stage1_root=fresh_root,
    )

    historical_after = verify_historical_incident(historical_root)
    if historical_after["stage1_tree"] != historical_before["stage1_tree"]:
        raise G7KRecoveryError("historical Stage-1 root changed during fresh preparation")
    if historical_after["journal_file_sha256"] != historical_before["journal_file_sha256"]:
        raise G7KRecoveryError("historical paid journal changed during fresh preparation")
    if historical_after["ledger_file_sha256"] != historical_before["ledger_file_sha256"]:
        raise G7KRecoveryError("historical G7F ledger changed during fresh preparation")

    plan = g7f.build_stage1_invocation_plan(
        stage1_root=fresh_root,
        run_index=TARGET_RUN_INDEX,
        target_chapter=TARGET_CHAPTER,
        maximum_new_provider_sends=maximum_new_provider_sends,
        maximum_credit_equivalent_usd=maximum_credit_equivalent_usd,
    )
    inspected = g7f.inspect_stage1_invocation_plan(
        stage1_root=fresh_root,
        plan=plan,
    )
    if inspected.get("provider_calls") != 0:
        raise G7KRecoveryError("fresh G7F plan inspection unexpectedly reported Provider calls")
    if inspected["progress"].get("last_completed") != 0:
        raise G7KRecoveryError("fresh Run-1 plan is not bound to pristine progress")
    if inspected["progress"].get("next_chapter") != 1:
        raise G7KRecoveryError("fresh Run-1 plan does not target Chapter 1")

    binding_payload = _recovery_binding_payload(
        recovery_generation_id=generation_id,
        historical=historical_before,
        fresh=fresh,
        plan=plan,
    )
    recovery_binding_sha256 = _sha256_json(binding_payload)

    evidence.parent.mkdir(parents=True, exist_ok=True)
    staging = evidence.parent / f".{evidence.name}.creating-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        plan_path = staging / RECOVERY_PLAN_FILENAME
        atomic_write_json(plan_path, plan)
        plan_file_sha256 = _sha256_file(plan_path)

        manifest = {
            "schema_version": G7K_SCHEMA_VERSION,
            "revision": G7K_REVISION,
            "g7k_policy_sha256": G7K_POLICY_SHA256,
            "recovery_generation_id": generation_id,
            "historical": {
                **copy.deepcopy(historical_before),
                "root": str(historical_root),
                "modified_by_g7k": False,
            },
            "fresh": {
                **copy.deepcopy(fresh),
                "root": str(fresh_root),
                "created_by_g7k": True,
                "all_twelve_runs_pristine": True,
                "reconstruction": copy.deepcopy(reconstructed),
            },
            "run1_chapter1_plan": {
                "file": RECOVERY_PLAN_FILENAME,
                "file_sha256": plan_file_sha256,
                "plan_sha256": plan["plan_sha256"],
                "approval_token": plan["approval_token"],
                "maximum_new_provider_sends": plan["g7e_plan"][
                    "maximum_new_provider_sends"
                ],
                "maximum_credit_equivalent_usd": plan["g7e_plan"][
                    "maximum_credit_equivalent_usd"
                ],
                "executor_approval_token_equals_consumed_historical_token": (
                    plan["approval_token"] == HISTORICAL_CONSUMED_APPROVAL_TOKEN
                ),
            },
            "fresh_operator_authorization": {
                "required": True,
                "granted": False,
                "historical_authorization_reusable": False,
                "recovery_binding_payload": binding_payload,
                "recovery_binding_sha256": recovery_binding_sha256,
                "required_future_authorization_fields": [
                    "recovery_generation_id",
                    "recovery_binding_sha256",
                    "run_index",
                    "target_chapter",
                    "model_id",
                    "maximum_new_provider_sends",
                    "maximum_credit_equivalent_usd",
                ],
            },
            "safety": {
                "provider_calls": 0,
                "credential_access": False,
                "dpapi_decrypt": False,
                "paid_execution_authorized": False,
                "chapter_1_executed": False,
                "historical_retry_performed": False,
                "historical_stage1_modified": False,
            },
        }
        manifest["recovery_manifest_sha256"] = _sha256_json(manifest)
        atomic_write_json(staging / RECOVERY_MANIFEST_FILENAME, manifest)
        os.rename(staging, evidence)
    except BaseException:
        if staging.exists():
            for child in sorted(staging.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            staging.rmdir()
        raise

    return verify_recovery(
        production_root=production,
        historical_stage1_root=historical_root,
        fresh_stage1_root=fresh_root,
        evidence_root=evidence,
    )


def verify_recovery(
    *,
    production_root: Path | str,
    historical_stage1_root: Path | str,
    fresh_stage1_root: Path | str,
    evidence_root: Path | str,
) -> dict[str, Any]:
    """Read-only re-verification of one materialized G7K recovery generation."""
    production = Path(production_root).resolve()
    historical_root = Path(historical_stage1_root).resolve()
    fresh_root = Path(fresh_stage1_root).resolve()
    evidence = Path(evidence_root).resolve()

    _assert_separate_roots(
        production_root=production,
        historical_stage1_root=historical_root,
        fresh_stage1_root=fresh_root,
        evidence_root=evidence,
    )
    if not evidence.is_dir():
        raise G7KRecoveryError(f"G7K evidence root missing: {evidence}")

    manifest_path = evidence / RECOVERY_MANIFEST_FILENAME
    plan_path = evidence / RECOVERY_PLAN_FILENAME
    manifest = _read_object(manifest_path, "G7K recovery manifest")
    plan = _read_object(plan_path, "G7K Run-1 Chapter-1 plan")

    stored_manifest_sha = manifest.get("recovery_manifest_sha256")
    check = copy.deepcopy(manifest)
    check.pop("recovery_manifest_sha256", None)
    if not isinstance(stored_manifest_sha, str) or not _SHA256_RE.fullmatch(
        stored_manifest_sha
    ):
        raise G7KRecoveryError("G7K recovery manifest SHA-256 is invalid")
    if _sha256_json(check) != stored_manifest_sha:
        raise G7KRecoveryError("G7K recovery manifest SHA-256 does not recompute")
    if manifest.get("g7k_policy_sha256") != G7K_POLICY_SHA256:
        raise G7KRecoveryError("G7K recovery manifest binds another policy")
    _validate_generation_id(manifest.get("recovery_generation_id"))

    historical = verify_historical_incident(historical_root)
    recorded_historical = manifest.get("historical")
    if not isinstance(recorded_historical, Mapping):
        raise G7KRecoveryError("G7K manifest lacks historical evidence")
    if recorded_historical.get("stage1_tree") != historical["stage1_tree"]:
        raise G7KRecoveryError("historical Stage-1 tree drifted after G7K preparation")
    if recorded_historical.get("journal_file_sha256") != historical[
        "journal_file_sha256"
    ]:
        raise G7KRecoveryError("historical paid journal drifted after G7K preparation")
    if recorded_historical.get("ledger_file_sha256") != historical[
        "ledger_file_sha256"
    ]:
        raise G7KRecoveryError("historical G7F ledger drifted after G7K preparation")

    fresh = verify_fresh_s0(
        production_root=production,
        fresh_stage1_root=fresh_root,
    )
    recorded_fresh = manifest.get("fresh")
    if not isinstance(recorded_fresh, Mapping):
        raise G7KRecoveryError("G7K manifest lacks fresh S0 evidence")
    if recorded_fresh.get("tree") != fresh["tree"]:
        raise G7KRecoveryError("fresh Stage-1 tree drifted after G7K preparation")
    if recorded_fresh.get("g7c_manifest_sha256") != fresh["g7c_manifest_sha256"]:
        raise G7KRecoveryError("fresh G7C manifest identity drifted")

    plan_meta = manifest.get("run1_chapter1_plan")
    if not isinstance(plan_meta, Mapping):
        raise G7KRecoveryError("G7K manifest lacks plan metadata")
    if _sha256_file(plan_path) != plan_meta.get("file_sha256"):
        raise G7KRecoveryError("G7K plan file SHA-256 changed")
    if plan.get("plan_sha256") != plan_meta.get("plan_sha256"):
        raise G7KRecoveryError("G7K plan semantic SHA-256 changed")
    if plan.get("approval_token") != plan_meta.get("approval_token"):
        raise G7KRecoveryError("G7K plan approval token changed")

    inspected = g7f.inspect_stage1_invocation_plan(
        stage1_root=fresh_root,
        plan=plan,
    )
    if inspected.get("provider_calls") != 0:
        raise G7KRecoveryError("G7K verification unexpectedly reported Provider calls")

    auth = manifest.get("fresh_operator_authorization")
    if not isinstance(auth, Mapping):
        raise G7KRecoveryError("G7K manifest lacks fresh authorization boundary")
    if auth.get("required") is not True or auth.get("granted") is not False:
        raise G7KRecoveryError("fresh operator authorization boundary changed")
    if auth.get("historical_authorization_reusable") is not False:
        raise G7KRecoveryError("historical authorization was incorrectly marked reusable")

    expected_binding = _recovery_binding_payload(
        recovery_generation_id=manifest["recovery_generation_id"],
        historical=historical,
        fresh=fresh,
        plan=plan,
    )
    expected_binding_sha = _sha256_json(expected_binding)
    if auth.get("recovery_binding_payload") != expected_binding:
        raise G7KRecoveryError("G7K recovery binding payload changed")
    if auth.get("recovery_binding_sha256") != expected_binding_sha:
        raise G7KRecoveryError("G7K recovery binding SHA-256 changed")

    safety = manifest.get("safety")
    if not isinstance(safety, Mapping):
        raise G7KRecoveryError("G7K safety record missing")
    expected_safety = {
        "provider_calls": 0,
        "credential_access": False,
        "dpapi_decrypt": False,
        "paid_execution_authorized": False,
        "chapter_1_executed": False,
        "historical_retry_performed": False,
        "historical_stage1_modified": False,
    }
    if dict(safety) != expected_safety:
        raise G7KRecoveryError("G7K safety record changed")

    return {
        "status": "READY_FOR_FRESH_OPERATOR_AUTHORIZATION",
        "g7k_policy_sha256": G7K_POLICY_SHA256,
        "recovery_manifest_sha256": stored_manifest_sha,
        "recovery_generation_id": manifest["recovery_generation_id"],
        "recovery_binding_sha256": expected_binding_sha,
        "historical_stage1_tree_sha256": historical["stage1_tree"]["fingerprint"],
        "fresh_stage1_tree_sha256": fresh["tree"]["fingerprint"],
        "fresh_stage1_file_count": fresh["tree"]["file_count"],
        "fresh_g7c_manifest_sha256": fresh["g7c_manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "executor_approval_token_equals_consumed_historical_token": (
            plan["approval_token"] == HISTORICAL_CONSUMED_APPROVAL_TOKEN
        ),
        "maximum_new_provider_sends": plan["g7e_plan"][
            "maximum_new_provider_sends"
        ],
        "maximum_credit_equivalent_usd": plan["g7e_plan"][
            "maximum_credit_equivalent_usd"
        ],
        "provider_calls": 0,
        "credential_access": False,
        "paid_execution_authorized": False,
        "chapter_1_executed": False,
        "historical_stage1_modified": False,
        "new_operator_authorization_required": True,
    }


def render_summary(report: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            RAW_PASS,
            RAW_PROVIDER_CALLS,
            RAW_CREDENTIAL_ACCESS,
            RAW_PAID_AUTHORIZED,
            RAW_CHAPTER_EXECUTED,
            RAW_HISTORICAL_MODIFIED,
            f"STATUS={report.get('status')}",
            f"G7K_POLICY_SHA256={report.get('g7k_policy_sha256')}",
            f"RECOVERY_GENERATION_ID={report.get('recovery_generation_id')}",
            f"RECOVERY_BINDING_SHA256={report.get('recovery_binding_sha256')}",
            f"HISTORICAL_STAGE1_TREE_SHA256={report.get('historical_stage1_tree_sha256')}",
            f"FRESH_STAGE1_FILE_COUNT={report.get('fresh_stage1_file_count')}",
            f"FRESH_STAGE1_TREE_SHA256={report.get('fresh_stage1_tree_sha256')}",
            f"FRESH_G7C_MANIFEST_SHA256={report.get('fresh_g7c_manifest_sha256')}",
            f"PLAN_SHA256={report.get('plan_sha256')}",
            f"APPROVAL_TOKEN={report.get('approval_token')}",
            "EXECUTOR_APPROVAL_TOKEN_EQUALS_CONSUMED_HISTORICAL_TOKEN="
            f"{report.get('executor_approval_token_equals_consumed_historical_token')}",
            f"MAX_NEW_PROVIDER_SENDS={report.get('maximum_new_provider_sends')}",
            "MAX_CREDIT_EQUIVALENT_USD="
            f"{report.get('maximum_credit_equivalent_usd')}",
            "NEW_OPERATOR_AUTHORIZATION_REQUIRED=YES",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7k-fresh-s0-recovery",
        description=(
            "Zero-provider fresh-S0 recovery preparation/verification for "
            "the consumed GOAT Run-1 Chapter-1 incident"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--production-root", required=True)
    prepare.add_argument("--historical-stage1-root", required=True)
    prepare.add_argument("--fresh-stage1-root", required=True)
    prepare.add_argument("--evidence-root", required=True)
    prepare.add_argument("--recovery-generation-id", required=True)
    prepare.add_argument("--maximum-new-provider-sends", type=int, required=True)
    prepare.add_argument("--maximum-credit-equivalent-usd", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--production-root", required=True)
    verify.add_argument("--historical-stage1-root", required=True)
    verify.add_argument("--fresh-stage1-root", required=True)
    verify.add_argument("--evidence-root", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare_fresh_s0_recovery(
                production_root=args.production_root,
                historical_stage1_root=args.historical_stage1_root,
                fresh_stage1_root=args.fresh_stage1_root,
                evidence_root=args.evidence_root,
                recovery_generation_id=args.recovery_generation_id,
                maximum_new_provider_sends=args.maximum_new_provider_sends,
                maximum_credit_equivalent_usd=args.maximum_credit_equivalent_usd,
            )
        else:
            report = verify_recovery(
                production_root=args.production_root,
                historical_stage1_root=args.historical_stage1_root,
                fresh_stage1_root=args.fresh_stage1_root,
                evidence_root=args.evidence_root,
            )
    except G7KRecoveryError as exc:
        print(RAW_FAIL)
        print(RAW_PROVIDER_CALLS)
        print(RAW_CREDENTIAL_ACCESS)
        print(RAW_PAID_AUTHORIZED)
        print(RAW_CHAPTER_EXECUTED)
        print(RAW_HISTORICAL_MODIFIED)
        print(f"G7K_BLOCKER={exc}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        print(RAW_FAIL)
        print(RAW_PROVIDER_CALLS)
        print(RAW_CREDENTIAL_ACCESS)
        print(RAW_PAID_AUTHORIZED)
        print(RAW_CHAPTER_EXECUTED)
        print(RAW_HISTORICAL_MODIFIED)
        print(f"G7K_BLOCKER={type(exc).__name__}: {str(exc)[:500]}")
        return 1

    print(render_summary(report))
    return 0


__all__ = [
    "EXPECTED_FRESH_S0_FILE_COUNT",
    "EXPECTED_FRESH_S0_TREE_SHA256",
    "EXPECTED_RUN1_RELATIVE_FILES",
    "G7K_POLICY_SHA256",
    "G7K_REVISION",
    "G7KRecoveryError",
    "HISTORICAL_INCIDENT_ARTIFACTS",
    "HISTORICAL_REQUEST_SHA256",
    "HISTORICAL_RESPONSE_SHA256",
    "HISTORICAL_STAGE1_FILE_COUNT",
    "HISTORICAL_STAGE1_TREE_SHA256",
    "PRISTINE_RUN1_BASE_RELATIVES",
    "RECOVERY_MANIFEST_FILENAME",
    "RECOVERY_PLAN_FILENAME",
    "REMOVED_LEDGER_DIRECTORY_RELATIVE",
    "build_parser",
    "g7k_policy_identity",
    "prepare_fresh_s0_recovery",
    "reconstruct_fresh_s0_from_historical",
    "render_summary",
    "tree_fingerprint",
    "verify_fresh_s0",
    "verify_historical_incident",
    "verify_recovery",
]


if __name__ == "__main__":
    raise SystemExit(main())
