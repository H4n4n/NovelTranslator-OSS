"""Zero-provider G6I blind evaluation bundle preparation.

G6I consumes the frozen G6 calibration/blind protocol and the authoritative
post-execution evidence. It creates a private identity key plus a public blind
literary-evaluation bundle. It never contacts a Provider, reads credentials, or
changes production state.

The preregistered G6 blind protocol remains immutable. Execution-aware handling
is layered on top: all 16 adjustable-model pairs must have durable outputs, six
of eight default-only singleton outputs are literary-scoreable, and the two
frozen Tencent HTTP-524 outcomes remain anonymous terminal-unavailable singleton
slots with no fabricated translation or score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any, Mapping, Sequence

from benchmark_longitudinal_g6_blind import (
    DEFECT_FLAGS,
    DIMENSIONS,
    G6_BLIND_PROTOCOL_SHA256,
    PAIR_COUNT,
    RUBRIC,
    SINGLETON_COUNT,
    derive_private_blind_mapping,
    private_mapping_commitment,
)
from benchmark_longitudinal_g6_calibration import (
    G6_CALIBRATION_CASES,
    G6_CALIBRATION_FREEZE_SHA256,
    G6_SOURCE_CASES,
)
from benchmark_longitudinal_g6_execution import (
    EXECUTION_REVISION,
    _canonical_json,
    _load_json_object,
    _read_chapter_library,
    _sha256_file,
    _sha256_json,
    _sha256_text,
    assert_external_evidence_root,
    build_runtime_cases,
    evaluate_translation_response,
)
from chapter_library import chapter_production_index
from novel_project import SOURCE_TYPE_TXT, load_novel_project
from production_paid_request_journal import load_matching_journal


G6I_SCHEMA_VERSION = 1
G6I_REVISION = "goat-g6-blind-bundle-v1"

# This is the independently re-verified main immediately before G6I engineering.
G6I_REPOSITORY_ANCHOR = "aaff4e6602b9d7849304b3971c7391416492eb18"

EXPECTED_CALIBRATION_FREEZE_SHA256 = (
    "fbd3127427cbc210dfdedcb80a6052e61f9851d4eb4551d9e26b25865d607c4c"
)
EXPECTED_BLIND_PROTOCOL_SHA256 = (
    "e1272d13d93bbbcbef28d85132f5af3500e73882c4983111c89ba5e7952bd45d"
)
EXPECTED_EXECUTION_PLAN_SHA256 = (
    "c47533cfc2acd1f211a23ee54ebf13594a4c52ab9947d52c6c8d2bc95aff92ca"
)
EXPECTED_EXECUTION_PLAN_FILE_SHA256 = (
    "01ec1435f2be0430839f14bf6005c22ed34544d34377f3b6e10842136c3bfd6c"
)

R6_JSON_SHA256 = (
    "a220385da2e4efbbd6c78679d71ca75ff5696f0af15a82db6f1262a5e7e86aa4"
)
R6_TEXT_SHA256 = (
    "5762009050c1f270eed6aec3a95cdb37d61ef67671a42f412efd001de9c738b8"
)
R6_MANIFEST_SHA256 = (
    "8a91f643bf2f11fde42d9cd4dca9613ff2107af7864be234d300b18c607c49a1"
)

EXECUTION_TREE_FILE_COUNT = 82
EXECUTION_TREE_SHA256 = (
    "d8ea212941a4cc3fae00753cdac468525f5a274717696bd4785515347bb16daf"
)
SECOND_RECOVERY_TREE_FILE_COUNT = 3
SECOND_RECOVERY_TREE_SHA256 = (
    "e4e7c619a8178ecf2106e460052b6f3545ce6e385c41e55924db595304c937db"
)
G6H_WRAPPER_TREE_FILE_COUNT = 4
G6H_WRAPPER_TREE_SHA256 = (
    "88e44a7926918a1e4e095a1ebdb59401005b73191515ba73693630daa699bec4"
)

EXPECTED_LOGICAL_CASES = 40
EXPECTED_ACCEPTED_RESULTS = 38
EXPECTED_TERMINAL_CASE_IDS = ("G6C035", "G6C036")
EXPECTED_TERMINAL_HTTP_STATUS = 524
EXPECTED_AVAILABLE_SINGLETONS = 6
EXPECTED_UNAVAILABLE_SINGLETONS = 2

R6_FILENAMES = {
    "json": "g6h_reconciliation_r6.json",
    "text": "g6h_reconciliation_r6.txt",
    "manifest": "g6h_reconciliation_r6_manifest.json",
}
R6_HASHES = {
    "json": R6_JSON_SHA256,
    "text": R6_TEXT_SHA256,
    "manifest": R6_MANIFEST_SHA256,
}

PUBLIC_BUNDLE_FILENAME = "g6i_public_blind_bundle.json"
PRIVATE_KEY_FILENAME = "g6i_private_identity_key.json"
MANIFEST_FILENAME = "g6i_bundle_manifest.json"
SUMMARY_FILENAME = "g6i_freeze_summary.txt"

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "benchmark_model_id",
        "provider_model_id",
        "effort",
        "lower_effort",
        "higher_effort",
        "side_a_effort",
        "side_b_effort",
        "mapping_nonce",
        "case_id",
        "provider_response_id",
        "response_model",
        "http_status",
        "provider_rejection_reasons",
    }
)


class G6IBundleError(RuntimeError):
    """Base error for G6I fail-closed bundle preparation."""


class G6IEvidenceError(G6IBundleError):
    """Frozen or durable evidence does not match the G6I contract."""


class G6IBlindingError(G6IBundleError):
    """A public artifact would leak private model/effort identity."""


def _historical_windows_relative_path(path: Path, root: Path) -> str:
    """Reproduce the PowerShell tree-fingerprint path representation.

    The frozen G6 execution/recovery/wrapper tree SHAs were produced on Windows
    by taking FullName.Substring(root.Length).TrimStart('\\', '/').  Those
    records therefore contain Windows backslashes.  Do not use Path.as_posix()
    here: changing separators changes the aggregate SHA while file bytes remain
    identical.
    """
    relative = str(path.resolve().relative_to(root))
    return relative.replace("/", "\\")


def _external_tree_identity(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise G6IEvidenceError(f"evidence tree is not a directory: {root}")
    rows: list[str] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: str(item.resolve()),
    ):
        relative = _historical_windows_relative_path(path, root)
        rows.append(f"{relative}\t{_sha256_file(path)}\t{path.stat().st_size}")
    return {
        "root": str(root),
        "file_count": len(rows),
        "tree_sha256": _sha256_text("\n".join(rows)),
    }


def _require_tree(
    root: Path | str,
    *,
    expected_count: int,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    identity = _external_tree_identity(root)
    if identity["file_count"] != expected_count:
        raise G6IEvidenceError(
            f"{label} file count changed: "
            f"{identity['file_count']} != {expected_count}"
        )
    if identity["tree_sha256"] != expected_sha256:
        raise G6IEvidenceError(
            f"{label} tree SHA256 changed: "
            f"{identity['tree_sha256']} != {expected_sha256}"
        )
    return identity


def _require_file_hash(path: Path, expected: str, label: str) -> str:
    if not path.is_file():
        raise G6IEvidenceError(f"missing {label}: {path}")
    actual = _sha256_file(path)
    if actual != expected:
        raise G6IEvidenceError(
            f"{label} SHA256 changed: {actual} != {expected}"
        )
    return actual


def _candidate_roots(base: Path, marker: str) -> list[Path]:
    return sorted(
        {path.parent.resolve() for path in base.rglob(marker) if path.is_file()},
        key=str,
    )


def _discover_exact_tree(
    base: Path,
    marker: str,
    *,
    expected_count: int,
    expected_sha256: str,
    label: str,
) -> Path:
    matches: list[Path] = []
    for candidate in _candidate_roots(base, marker):
        identity = _external_tree_identity(candidate)
        if (
            identity["file_count"] == expected_count
            and identity["tree_sha256"] == expected_sha256
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise G6IEvidenceError(
            f"expected exactly one {label} tree under {base}, found {len(matches)}"
        )
    return matches[0]


def discover_evidence_roots(evidence_base: Path | str) -> dict[str, Path]:
    """Discover frozen external roots by exact byte-tree identity."""
    base = Path(evidence_base).resolve()
    if not base.is_dir():
        raise G6IEvidenceError(f"evidence base is not a directory: {base}")

    execution = _discover_exact_tree(
        base,
        "execution_plan.json",
        expected_count=EXECUTION_TREE_FILE_COUNT,
        expected_sha256=EXECUTION_TREE_SHA256,
        label="G6 execution",
    )
    second_recovery = _discover_exact_tree(
        base,
        "second_recovery_plan.json",
        expected_count=SECOND_RECOVERY_TREE_FILE_COUNT,
        expected_sha256=SECOND_RECOVERY_TREE_SHA256,
        label="G6 second-recovery control",
    )
    wrapper = _discover_exact_tree(
        base,
        "g6h_execution_wrapper_manifest.json",
        expected_count=G6H_WRAPPER_TREE_FILE_COUNT,
        expected_sha256=G6H_WRAPPER_TREE_SHA256,
        label="G6H wrapper",
    )

    r6_candidates = _candidate_roots(base, R6_FILENAMES["manifest"])
    r6_matches: list[Path] = []
    for candidate in r6_candidates:
        try:
            for key, name in R6_FILENAMES.items():
                _require_file_hash(
                    candidate / name,
                    R6_HASHES[key],
                    f"R6 {key}",
                )
        except G6IEvidenceError:
            continue
        r6_matches.append(candidate)
    if len(r6_matches) != 1:
        raise G6IEvidenceError(
            f"expected exactly one authoritative R6 root under {base}, "
            f"found {len(r6_matches)}"
        )

    return {
        "execution": execution,
        "second_recovery_control": second_recovery,
        "g6h_wrapper": wrapper,
        "r6": r6_matches[0],
    }


def verify_repository_anchor(repo_root: Path | str) -> dict[str, str]:
    """Require a clean isolated worktree descended from the audited G6I anchor."""
    repo = Path(repo_root).resolve()

    def run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise G6IEvidenceError(f"git repository verification failed: {exc}") from exc
        return completed.stdout.strip()

    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise G6IEvidenceError(
            "G6I requires a clean isolated worktree; local changes were detected"
        )

    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                G6I_REPOSITORY_ANCHOR,
                head,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise G6IEvidenceError(
            "current worktree HEAD is not descended from the audited G6I anchor"
        ) from exc

    return {"repository_anchor": G6I_REPOSITORY_ANCHOR, "head": head}


def _verify_frozen_code_bindings() -> None:
    if G6_CALIBRATION_FREEZE_SHA256 != EXPECTED_CALIBRATION_FREEZE_SHA256:
        raise G6IEvidenceError("G6 calibration freeze SHA drifted")
    if G6_BLIND_PROTOCOL_SHA256 != EXPECTED_BLIND_PROTOCOL_SHA256:
        raise G6IEvidenceError("G6 blind protocol SHA drifted")
    if PAIR_COUNT != 16 or SINGLETON_COUNT != 8:
        raise G6IEvidenceError("frozen G6 blind identity cardinality drifted")


def _load_frozen_source_texts(repo_root: Path | str) -> dict[tuple[str, int], dict[str, Any]]:
    repo = Path(repo_root).resolve()
    loaded: dict[tuple[str, int], dict[str, Any]] = {}

    for source in G6_SOURCE_CASES:
        project = load_novel_project(source.novel_id, repo)
        source_path = (
            project.source_txt
            if project.config.source_type == SOURCE_TYPE_TXT
            else project.source_epub
        )
        if _sha256_file(source_path) != source.source_sha256:
            raise G6IEvidenceError(
                f"source SHA drift for {source.novel_id}"
            )
        if _sha256_file(project.chapters_file) != source.chapter_library_sha256:
            raise G6IEvidenceError(
                f"chapter-library SHA drift for {source.novel_id}"
            )
        chapters = _read_chapter_library(project)
        chapter = chapters.get(source.chapter)
        if chapter is None:
            raise G6IEvidenceError(
                f"missing frozen source chapter {source.novel_id}/{source.chapter}"
            )
        text = chapter.get("text")
        if not isinstance(text, str) or _sha256_text(text) != source.text_sha256:
            raise G6IEvidenceError(
                f"source text drift for {source.novel_id}/{source.chapter}"
            )
        loaded[(source.novel_id, source.chapter)] = {
            "novel_id": source.novel_id,
            "source_language": source.source_language,
            "stress_axis": source.stress_axis,
            "chapter": source.chapter,
            "text_sha256": source.text_sha256,
            "text": text,
        }

    if len(loaded) != 4:
        raise G6IEvidenceError("G6I requires exactly four frozen source texts")
    return loaded


def _load_result(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6IEvidenceError(f"cannot read G6 result {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise G6IEvidenceError(f"G6 result is not an object: {path}")
    return value


def collect_execution_records(
    *,
    repo_root: Path | str,
    execution_root: Path | str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile all 40 durable journals/results without any Provider call."""
    execution = Path(execution_root).resolve()
    _require_tree(
        execution,
        expected_count=EXECUTION_TREE_FILE_COUNT,
        expected_sha256=EXECUTION_TREE_SHA256,
        label="G6 execution",
    )

    plan_path = execution / "execution_plan.json"
    _require_file_hash(
        plan_path,
        EXPECTED_EXECUTION_PLAN_FILE_SHA256,
        "G6 execution plan",
    )
    plan = _load_json_object(plan_path, "G6 execution plan")
    if _sha256_json(plan) != EXPECTED_EXECUTION_PLAN_SHA256:
        raise G6IEvidenceError("G6 execution-plan semantic SHA drifted")

    runtime_cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=execution,
    )
    if len(runtime_cases) != EXPECTED_LOGICAL_CASES:
        raise G6IEvidenceError("G6 runtime case count is not 40")

    records: dict[str, dict[str, Any]] = {}
    inventory: list[dict[str, Any]] = []
    accepted_count = 0
    terminal_ids: list[str] = []

    for item in runtime_cases:
        case = item.calibration_case
        case_id = case.case_id
        journal = load_matching_journal(item.journal_path, item.identity)
        if journal is None:
            raise G6IEvidenceError(f"missing durable journal for {case_id}")
        if journal.get("status") != "response_recorded":
            raise G6IEvidenceError(
                f"{case_id} journal is not response_recorded"
            )

        result_path = execution / "results" / f"{item.index:04d}.json"
        if not result_path.is_file():
            raise G6IEvidenceError(f"missing result for {case_id}")
        result = _load_result(result_path)

        if result.get("execution_revision") != EXECUTION_REVISION:
            raise G6IEvidenceError(f"{case_id} execution revision drifted")
        if result.get("g6_calibration_freeze_sha256") != G6_CALIBRATION_FREEZE_SHA256:
            raise G6IEvidenceError(f"{case_id} calibration binding drifted")
        if result.get("g6_blind_protocol_sha256") != G6_BLIND_PROTOCOL_SHA256:
            raise G6IEvidenceError(f"{case_id} blind-protocol binding drifted")
        if result.get("case") != item.plan_identity():
            raise G6IEvidenceError(f"{case_id} case identity drifted")
        if result.get("response") != journal.get("response"):
            raise G6IEvidenceError(f"{case_id} result/journal response mismatch")

        recomputed = evaluate_translation_response(
            journal["response"],
            runtime_case=item,
        )
        if result.get("evaluation") != recomputed:
            raise G6IEvidenceError(f"{case_id} stored evaluation drifted")

        accepted = bool(recomputed["provider_accepted"])
        if accepted:
            accepted_count += 1
        else:
            terminal_ids.append(case_id)
            if case_id not in EXPECTED_TERMINAL_CASE_IDS:
                raise G6IEvidenceError(
                    f"unexpected rejected G6 case: {case_id}"
                )
            if journal["response"].get("http_status") != EXPECTED_TERMINAL_HTTP_STATUS:
                raise G6IEvidenceError(
                    f"{case_id} terminal HTTP status drifted"
                )

        journal_sha = _sha256_file(item.journal_path)
        result_sha = _sha256_file(result_path)
        inventory.append(
            {
                "index": item.index,
                "case_id": case_id,
                "journal_file": item.journal_path.name,
                "journal_sha256": journal_sha,
                "result_file": result_path.name,
                "result_sha256": result_sha,
            }
        )
        records[case_id] = {
            "case": case,
            "response": journal["response"],
            "evaluation": recomputed,
            "journal_sha256": journal_sha,
            "result_sha256": result_sha,
        }

    if accepted_count != EXPECTED_ACCEPTED_RESULTS:
        raise G6IEvidenceError(
            f"accepted result count changed: {accepted_count}"
        )
    if tuple(terminal_ids) != EXPECTED_TERMINAL_CASE_IDS:
        raise G6IEvidenceError(
            f"terminal case set/order changed: {terminal_ids}"
        )
    if len(records) != EXPECTED_LOGICAL_CASES:
        raise G6IEvidenceError("G6 durable record cardinality changed")

    return records, inventory


def verify_authoritative_evidence(
    *,
    repo_root: Path | str,
    evidence_base: Path | str,
) -> dict[str, Any]:
    _verify_frozen_code_bindings()
    roots = discover_evidence_roots(evidence_base)

    for key, name in R6_FILENAMES.items():
        _require_file_hash(
            roots["r6"] / name,
            R6_HASHES[key],
            f"R6 {key}",
        )
    execution_tree = _require_tree(
        roots["execution"],
        expected_count=EXECUTION_TREE_FILE_COUNT,
        expected_sha256=EXECUTION_TREE_SHA256,
        label="G6 execution",
    )
    recovery_tree = _require_tree(
        roots["second_recovery_control"],
        expected_count=SECOND_RECOVERY_TREE_FILE_COUNT,
        expected_sha256=SECOND_RECOVERY_TREE_SHA256,
        label="G6 second-recovery control",
    )
    wrapper_tree = _require_tree(
        roots["g6h_wrapper"],
        expected_count=G6H_WRAPPER_TREE_FILE_COUNT,
        expected_sha256=G6H_WRAPPER_TREE_SHA256,
        label="G6H wrapper",
    )

    records, inventory = collect_execution_records(
        repo_root=repo_root,
        execution_root=roots["execution"],
    )
    inventory_sha = _sha256_json(inventory)

    return {
        "roots": roots,
        "records": records,
        "inventory": inventory,
        "inventory_sha256": inventory_sha,
        "execution_tree": execution_tree,
        "second_recovery_tree": recovery_tree,
        "g6h_wrapper_tree": wrapper_tree,
        "r6": dict(R6_HASHES),
    }


def _case_for(
    model_id: str,
    novel_id: str,
    chapter: int,
    effort: str | None,
):
    matches = [
        case
        for case in G6_CALIBRATION_CASES
        if case.benchmark_model_id == model_id
        and case.novel_id == novel_id
        and case.chapter == chapter
        and case.effort == effort
    ]
    if len(matches) != 1:
        raise G6IEvidenceError(
            "private mapping does not resolve to exactly one frozen G6 case"
        )
    return matches[0]


def _source_identities(
    source_texts: Mapping[tuple[str, int], Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], str]]:
    public_sources: list[dict[str, Any]] = []
    ids: dict[tuple[str, int], str] = {}
    for index, source in enumerate(G6_SOURCE_CASES, start=1):
        key = (source.novel_id, source.chapter)
        row = source_texts.get(key)
        if not isinstance(row, Mapping):
            raise G6IEvidenceError(f"missing source text for {key!r}")
        source_id = f"G6SRC{index:03d}"
        ids[key] = source_id
        public_sources.append(
            {
                "source_id": source_id,
                "novel_id": source.novel_id,
                "source_language": source.source_language,
                "stress_axis": source.stress_axis,
                "chapter": source.chapter,
                "text_sha256": source.text_sha256,
                "source_text": row["text"],
            }
        )
    return public_sources, ids


def _accepted_output(record: Mapping[str, Any], label: str) -> dict[str, Any]:
    evaluation = record["evaluation"]
    if evaluation.get("provider_accepted") is not True:
        raise G6IEvidenceError(f"{label} lacks an accepted literary output")
    response = record["response"]
    content = response.get("content")
    if not isinstance(content, str):
        raise G6IEvidenceError(f"{label} accepted output is not text")
    diagnostics = evaluation["literary_diagnostics"]
    if diagnostics.get("translation_sha256") != _sha256_text(content):
        raise G6IEvidenceError(f"{label} translation SHA drifted")
    return {
        "translation": content,
        "translation_sha256": diagnostics["translation_sha256"],
    }


def _assert_public_blind(value: Any, *, nonce: str) -> None:
    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in _FORBIDDEN_PUBLIC_KEYS:
                    raise G6IBlindingError(
                        f"public bundle leaks forbidden identity field {key!r}"
                    )
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    rendered = _canonical_json(value)
    if nonce in rendered:
        raise G6IBlindingError("public bundle leaks the private mapping nonce")


def build_bundle_documents(
    *,
    records: Mapping[str, Mapping[str, Any]],
    source_texts: Mapping[tuple[str, int], Mapping[str, Any]],
    inventory: Sequence[Mapping[str, Any]],
    inventory_sha256: str,
    mapping_nonce: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Build private/public documents from already-verified frozen evidence."""
    mapping = derive_private_blind_mapping(mapping_nonce)
    public_sources, source_ids = _source_identities(source_texts)

    private_pairs: list[dict[str, Any]] = []
    public_pairs: list[dict[str, Any]] = []

    for row in mapping["pairs"]:
        source_key = (row["novel_id"], row["chapter"])
        source_id = source_ids[source_key]
        case_a = _case_for(
            row["benchmark_model_id"],
            row["novel_id"],
            row["chapter"],
            row["side_a_effort"],
        )
        case_b = _case_for(
            row["benchmark_model_id"],
            row["novel_id"],
            row["chapter"],
            row["side_b_effort"],
        )
        record_a = records[case_a.case_id]
        record_b = records[case_b.case_id]
        output_a = _accepted_output(record_a, case_a.case_id)
        output_b = _accepted_output(record_b, case_b.case_id)

        public_pairs.append(
            {
                "visible_pair_id": row["visible_pair_id"],
                "source_id": source_id,
                "side_a": output_a,
                "side_b": output_b,
                "score_record_required": True,
            }
        )
        private_pairs.append(
            {
                **row,
                "source_id": source_id,
                "side_a_case_id": case_a.case_id,
                "side_b_case_id": case_b.case_id,
                "side_a_result_sha256": record_a["result_sha256"],
                "side_b_result_sha256": record_b["result_sha256"],
            }
        )

    private_singletons: list[dict[str, Any]] = []
    public_singletons: list[dict[str, Any]] = []
    available_singletons = 0
    unavailable_singletons = 0

    for row in mapping["singletons"]:
        source_key = (row["novel_id"], row["chapter"])
        source_id = source_ids[source_key]
        case = _case_for(
            row["benchmark_model_id"],
            row["novel_id"],
            row["chapter"],
            None,
        )
        record = records[case.case_id]
        evaluation = record["evaluation"]

        private_row = {
            **row,
            "source_id": source_id,
            "case_id": case.case_id,
            "journal_sha256": record["journal_sha256"],
            "result_sha256": record["result_sha256"],
        }

        if evaluation.get("provider_accepted") is True:
            available_singletons += 1
            public_row = {
                "visible_singleton_id": row["visible_singleton_id"],
                "source_id": source_id,
                "availability": "translation_available",
                "score_record_required": True,
                **_accepted_output(record, case.case_id),
            }
            private_row["availability"] = "translation_available"
        else:
            if case.case_id not in EXPECTED_TERMINAL_CASE_IDS:
                raise G6IEvidenceError(
                    f"unexpected unavailable singleton {case.case_id}"
                )
            unavailable_singletons += 1
            public_row = {
                "visible_singleton_id": row["visible_singleton_id"],
                "source_id": source_id,
                "availability": "terminal_provider_unavailable",
                "score_record_required": False,
            }
            private_row.update(
                {
                    "availability": "terminal_provider_unavailable",
                    "http_status": record["response"].get("http_status"),
                    "provider_rejection_reasons": evaluation.get(
                        "provider_rejection_reasons"
                    ),
                }
            )

        public_singletons.append(public_row)
        private_singletons.append(private_row)

    if len(public_pairs) != PAIR_COUNT:
        raise G6IEvidenceError("G6I pair count changed")
    if len(public_singletons) != SINGLETON_COUNT:
        raise G6IEvidenceError("G6I singleton count changed")
    if available_singletons != EXPECTED_AVAILABLE_SINGLETONS:
        raise G6IEvidenceError("G6I available singleton count changed")
    if unavailable_singletons != EXPECTED_UNAVAILABLE_SINGLETONS:
        raise G6IEvidenceError("G6I unavailable singleton count changed")

    private_key = {
        "schema_version": G6I_SCHEMA_VERSION,
        "revision": G6I_REVISION,
        "repository_anchor": G6I_REPOSITORY_ANCHOR,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "r6_manifest_sha256": R6_MANIFEST_SHA256,
        "mapping_nonce": mapping_nonce,
        "pairs": private_pairs,
        "singletons": private_singletons,
        "durable_evidence_inventory": [dict(item) for item in inventory],
        "durable_evidence_inventory_sha256": inventory_sha256,
    }
    commitment = private_mapping_commitment(private_key)

    public_bundle = {
        "schema_version": G6I_SCHEMA_VERSION,
        "revision": G6I_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "private_identity_key_commitment_sha256": commitment,
        "provider_calls": 0,
        "identity_reveal_authorized": False,
        "effort_selection_authorized": False,
        "model_ranking_or_elimination_authorized": False,
        "rubric": RUBRIC,
        "dimensions": list(DIMENSIONS),
        "defect_flags": list(DEFECT_FLAGS),
        "sources": public_sources,
        "pairs": public_pairs,
        "singletons": public_singletons,
        "execution_aware_singleton_policy": {
            "frozen_singleton_identity_count": 8,
            "translation_available_count": available_singletons,
            "terminal_provider_unavailable_count": unavailable_singletons,
            "terminal_provider_unavailable_scored": False,
            "missing_literary_output_fabricated": False,
            "provider_retry_authorized": False,
        },
    }
    _assert_public_blind(public_bundle, nonce=mapping_nonce)
    return private_key, public_bundle, commitment


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise G6IBundleError(f"artifact already exists: {path}") from exc
    if path.read_bytes() != payload:
        raise G6IBundleError(f"artifact byte verification failed: {path}")


def freeze_bundle(
    *,
    repo_root: Path | str,
    evidence_base: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    """Verify everything first, then freeze one new non-overwriting G6I root."""
    repo = Path(repo_root).resolve()
    output = assert_external_evidence_root(output_root, repo)
    if output.exists():
        raise G6IBundleError(
            f"G6I output root already exists and will not be overwritten: {output}"
        )

    repo_binding = verify_repository_anchor(repo)
    evidence = verify_authoritative_evidence(
        repo_root=repo,
        evidence_base=evidence_base,
    )
    source_texts = _load_frozen_source_texts(repo)

    # Randomness is created only after every deterministic precheck passes.
    nonce = secrets.token_hex(32)
    private_key, public_bundle, commitment = build_bundle_documents(
        records=evidence["records"],
        source_texts=source_texts,
        inventory=evidence["inventory"],
        inventory_sha256=evidence["inventory_sha256"],
        mapping_nonce=nonce,
    )

    private_path = output / "private" / PRIVATE_KEY_FILENAME
    public_path = output / "public" / PUBLIC_BUNDLE_FILENAME
    manifest_path = output / "public" / MANIFEST_FILENAME
    summary_path = output / "public" / SUMMARY_FILENAME

    private_bytes = _json_bytes(private_key)
    public_bytes = _json_bytes(public_bundle)
    _write_exclusive(private_path, private_bytes)
    _write_exclusive(public_path, public_bytes)

    manifest = {
        "schema_version": G6I_SCHEMA_VERSION,
        "revision": G6I_REVISION,
        "repository_anchor": G6I_REPOSITORY_ANCHOR,
        "repository_head": repo_binding["head"],
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "execution_plan_sha256": EXPECTED_EXECUTION_PLAN_SHA256,
        "execution_plan_file_sha256": EXPECTED_EXECUTION_PLAN_FILE_SHA256,
        "r6_json_sha256": R6_JSON_SHA256,
        "r6_text_sha256": R6_TEXT_SHA256,
        "r6_manifest_sha256": R6_MANIFEST_SHA256,
        "execution_tree_sha256": EXECUTION_TREE_SHA256,
        "second_recovery_control_tree_sha256": SECOND_RECOVERY_TREE_SHA256,
        "g6h_wrapper_tree_sha256": G6H_WRAPPER_TREE_SHA256,
        "durable_evidence_inventory_sha256": evidence["inventory_sha256"],
        "private_identity_key_commitment_sha256": commitment,
        "private_identity_key_file_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "public_blind_bundle_file_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "pair_count": PAIR_COUNT,
        "singleton_identity_count": SINGLETON_COUNT,
        "literary_singleton_count": EXPECTED_AVAILABLE_SINGLETONS,
        "terminal_provider_unavailable_singleton_count": (
            EXPECTED_UNAVAILABLE_SINGLETONS
        ),
        "provider_calls": 0,
        "scores_frozen": False,
        "identity_reveal_authorized": False,
        "effort_selection_authorized": False,
    }
    manifest_bytes = _json_bytes(manifest)
    _write_exclusive(manifest_path, manifest_bytes)

    summary = (
        "G6I BLIND BUNDLE FREEZE\n"
        "=======================\n"
        f"revision={G6I_REVISION}\n"
        f"repository_anchor={G6I_REPOSITORY_ANCHOR}\n"
        f"repository_head={repo_binding['head']}\n"
        f"g6_blind_protocol_sha256={G6_BLIND_PROTOCOL_SHA256}\n"
        f"r6_manifest_sha256={R6_MANIFEST_SHA256}\n"
        f"private_identity_key_commitment_sha256={commitment}\n"
        f"public_blind_bundle_file_sha256={manifest['public_blind_bundle_file_sha256']}\n"
        f"pair_count={PAIR_COUNT}\n"
        f"singleton_identity_count={SINGLETON_COUNT}\n"
        f"literary_singleton_count={EXPECTED_AVAILABLE_SINGLETONS}\n"
        f"terminal_provider_unavailable_singleton_count={EXPECTED_UNAVAILABLE_SINGLETONS}\n"
        "provider_calls=0\n"
        "scores_frozen=False\n"
        "identity_reveal_authorized=False\n"
    )
    _write_exclusive(summary_path, summary.encode("utf-8"))

    return {
        "output_root": str(output),
        "manifest": manifest,
        "manifest_file_sha256": _sha256_file(manifest_path),
        "summary_file_sha256": _sha256_file(summary_path),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare the zero-provider G6I blind evaluation bundle."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--evidence-base", required=True)
    parser.add_argument("--output-root", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = freeze_bundle(
        repo_root=args.repo_root,
        evidence_base=args.evidence_base,
        output_root=args.output_root,
    )
    manifest = result["manifest"]
    print(f"g6i_revision={G6I_REVISION}")
    print(f"repository_anchor={G6I_REPOSITORY_ANCHOR}")
    print(f"repository_head={manifest['repository_head']}")
    print(f"g6_blind_protocol_sha256={G6_BLIND_PROTOCOL_SHA256}")
    print(f"r6_manifest_sha256={R6_MANIFEST_SHA256}")
    print(
        "private_identity_key_commitment_sha256="
        f"{manifest['private_identity_key_commitment_sha256']}"
    )
    print(
        "public_blind_bundle_file_sha256="
        f"{manifest['public_blind_bundle_file_sha256']}"
    )
    print(f"pair_count={manifest['pair_count']}")
    print(f"singleton_identity_count={manifest['singleton_identity_count']}")
    print(f"literary_singleton_count={manifest['literary_singleton_count']}")
    print(
        "terminal_provider_unavailable_singleton_count="
        f"{manifest['terminal_provider_unavailable_singleton_count']}"
    )
    print("provider_calls=0")
    print("scores_frozen=False")
    print("identity_reveal_authorized=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
