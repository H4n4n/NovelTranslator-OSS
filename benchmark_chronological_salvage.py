"""Zero-provider deterministic salvage foundation for the chronological benchmark.

The strict Phase 59B/59C runtime remains authoritative and immutable. This module
consumes that runtime only as frozen source evidence and creates a separate
external salvage root. Phase 59D deliberately exposes no provider-call command.

Frozen recovery policy:
- missing top-level state_update with a valid translation -> deterministic no-op
  update: zero glossary upserts and carry prior continuity/issues forward.
- duplicate glossary_upserts source values -> keep the first occurrence by raw
  response order and discard later occurrences without semantic selection.
- every other validation error remains fail-closed.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import benchmark_chronological_runtime as strict
from benchmark_novel import BenchmarkSafetyError

SALVAGE_SCHEMA_VERSION = 1
SALVAGE_KIND = "chronological-state-growth-salvage-v1"
PLAN_FILENAME = "salvage_plan.json"
JOURNAL_FILENAME = "salvage_journal.jsonl"

FLASH_FAILURE = {
    "arm": strict.FLASH_ARM,
    "novel_id": "rogue_first_prince",
    "chapter_number": 5,
    "case_id": "rogue_first_prince-ch0005",
    "expected_error": "state_update must be a JSON object",
    "expected_recovery": "missing_state_update_noop",
    "strict_complete": 9,
    "strict_paid_attempts": 10,
}

LUNA_FAILURE = {
    "arm": strict.LUNA_ARM,
    "novel_id": "great_king_zhuxia",
    "chapter_number": 18,
    "case_id": "great_king_zhuxia-ch0018",
    "expected_error": (
        "glossary_upserts contains duplicate source term in one response: "
        "'入土三分'"
    ),
    "expected_recovery": "duplicate_source_first_wins",
    "strict_complete": 34,
    "strict_paid_attempts": 35,
}

FAILURES = {
    strict.FLASH_ARM: FLASH_FAILURE,
    strict.LUNA_ARM: LUNA_FAILURE,
}


class SalvageRuntimeError(strict.ChronologicalRuntimeError):
    pass


def _external_root(repo_root: Path, canonical_root: Path, strict_root: Path, root: Path):
    root = Path(root)
    if not root.is_absolute():
        raise SalvageRuntimeError("salvage root must be an absolute external path")
    root = root.resolve()
    for protected in (
        Path(repo_root).resolve(),
        Path(canonical_root).resolve(),
        Path(strict_root).resolve(),
    ):
        if root == protected or protected in root.parents:
            raise BenchmarkSafetyError(
                "salvage root must be outside repository, canonical, and strict roots"
            )
    if root.exists():
        raise BenchmarkSafetyError(f"salvage root already exists: {root}")
    return root


def _noop_update(state_before: dict) -> dict:
    continuity = state_before.get("continuity_summary", "")
    issues = state_before.get("issues", [])
    if not isinstance(continuity, str) or not isinstance(issues, list):
        raise BenchmarkSafetyError("state-before is malformed for no-op recovery")
    return {
        "glossary_upserts": [],
        "continuity_summary": continuity,
        "issues": copy.deepcopy(issues),
    }


def parse_salvage_document(text: str, state_before: dict):
    """Return strict-normalized document plus deterministic transformation log."""
    raw = strict._strip_json_fence(text)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SalvageRuntimeError(f"model response is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SalvageRuntimeError("model response must be a JSON object")

    working = copy.deepcopy(document)
    transformations = []

    if "state_update" not in working:
        strict._bounded_string(
            working.get("translation"), "translation", limit=200_000
        )
        working["state_update"] = _noop_update(state_before)
        transformations.append(
            {
                "kind": "missing_state_update_noop",
                "detail": (
                    "state_update omitted; prior continuity/issues carried forward "
                    "with zero glossary upserts"
                ),
            }
        )
    else:
        update = working.get("state_update")
        if not isinstance(update, dict):
            raise SalvageRuntimeError("state_update must be a JSON object")
        upserts = update.get("glossary_upserts")
        if isinstance(upserts, list):
            seen = set()
            kept = []
            discarded = []
            for index, row in enumerate(upserts):
                if not isinstance(row, dict):
                    kept.append(row)
                    continue
                source = row.get("source")
                if isinstance(source, str) and source in seen:
                    discarded.append(
                        {
                            "index": index,
                            "source": source,
                            "entry_sha256": strict._canonical_json_sha256(row),
                            "entry": copy.deepcopy(row),
                        }
                    )
                    continue
                if isinstance(source, str):
                    seen.add(source)
                kept.append(row)
            if discarded:
                update["glossary_upserts"] = kept
                transformations.append(
                    {
                        "kind": "duplicate_source_first_wins",
                        "discarded_count": len(discarded),
                        "discarded": discarded,
                    }
                )

    normalized = strict.parse_model_document(
        json.dumps(working, ensure_ascii=False)
    )
    return normalized, transformations


def _strict_terminal_status(strict_root: Path, strict_plan: dict):
    expected = {
        strict.FLASH_ARM: {
            "complete": 9,
            "response_failed": 1,
            "planned": 40,
            "paid": 10,
        },
        strict.LUNA_ARM: {
            "complete": 34,
            "response_failed": 1,
            "planned": 15,
            "paid": 35,
        },
    }
    result = {}
    for arm in strict.SYNC_ARMS:
        status = strict.sync_arm_status(strict_plan, strict_root, arm)
        exp = expected[arm]
        if (
            status["counts"]["complete"] != exp["complete"]
            or status["counts"]["response_failed"] != exp["response_failed"]
            or status["counts"]["planned"] != exp["planned"]
            or status["paid_attempts"] != exp["paid"]
            or status["blocked"] is not True
            or status["next_case"] is not None
        ):
            raise BenchmarkSafetyError(
                f"strict terminal status drifted for {arm}: {status!r}"
            )
        result[arm] = status
    return result


def _strict_failure_evidence(strict_root: Path, strict_plan: dict, arm: str):
    failure = FAILURES[arm]
    case_dir = strict._case_dir(
        strict_root,
        arm,
        failure["novel_id"],
        failure["chapter_number"],
    )
    paths = strict._sync_complete_paths(case_dir)
    for key in ("request", "response", "journal", "response_failed"):
        if not paths[key].is_file():
            raise BenchmarkSafetyError(
                f"strict failure artifact missing for {arm}: {paths[key]}"
            )
    failed = strict._load_json(paths["response_failed"], "strict response_failed")
    error = failed.get("error") or {}
    if (
        failed.get("status") != "response_failed"
        or error.get("message") != failure["expected_error"]
    ):
        raise BenchmarkSafetyError(
            f"strict failure classification drifted for {arm}"
        )
    if strict._journal_events(paths["journal"]) != [
        "planned",
        "started",
        "response_recorded",
        "response_failed",
    ]:
        raise BenchmarkSafetyError(f"strict failure journal drifted for {arm}")
    response = strict._load_json(paths["response"], "strict failed response")
    provider = strict._provider_identity(response)
    if (
        provider["response_model"] != strict.EXPECTED_MODEL_CONFIG[arm]["model"]
        or provider["finish_reason"] != "stop"
    ):
        raise BenchmarkSafetyError(
            f"strict failed response provider identity drifted for {arm}"
        )
    return case_dir, paths, failed, response


def _recover_one(
    salvage_root: Path,
    strict_root: Path,
    strict_plan: dict,
    canonical_root: Path,
    canonical_plan: dict,
    arm: str,
):
    failure = FAILURES[arm]
    strict_case, paths, failed, response = _strict_failure_evidence(
        strict_root, strict_plan, arm
    )
    state_before, state_before_sha, state_before_path = strict._state_before_for_case(
        strict_root,
        canonical_root,
        canonical_plan,
        arm,
        failure["novel_id"],
        failure["chapter_number"],
    )
    document, transformations = parse_salvage_document(
        strict._response_content(response), state_before
    )
    kinds = [item["kind"] for item in transformations]
    if kinds != [failure["expected_recovery"]]:
        raise BenchmarkSafetyError(
            f"unexpected deterministic recovery for {arm}: {kinds!r}"
        )
    state_after, metrics = strict.apply_state_update(
        state_before, document, failure["chapter_number"]
    )

    out = (
        Path(salvage_root)
        / "recoveries"
        / arm
        / failure["case_id"]
    )
    out.mkdir(parents=True, exist_ok=False)
    strict._write_exclusive(out / "strict_request.json", paths["request"].read_bytes())
    strict._write_exclusive(out / "strict_response.json", paths["response"].read_bytes())
    strict._write_text_exclusive(out / "translation.txt", document["translation"])
    strict._write_json_exclusive(out / "state_update.json", document["state_update"])
    strict._write_json_exclusive(out / "state_after.json", state_after)

    identity = {
        "kind": SALVAGE_KIND,
        "arm": arm,
        "case_id": failure["case_id"],
        "novel_id": failure["novel_id"],
        "chapter_number": failure["chapter_number"],
        "strict_run_id": strict_plan["run_id"],
        "strict_request_sha256": strict._sha256_file(paths["request"]),
        "strict_response_sha256": strict._sha256_file(paths["response"]),
        "strict_response_failed_sha256": strict._sha256_file(
            paths["response_failed"]
        ),
        "state_before_identity_sha256": state_before_sha,
        "recovery_policy": failure["expected_recovery"],
    }
    recovery = {
        "schema_version": SALVAGE_SCHEMA_VERSION,
        "kind": SALVAGE_KIND,
        "status": "recovered_historical_failure",
        "identity": identity,
        "identity_sha256": strict._canonical_json_sha256(identity),
        "source_state_before_path": state_before_path,
        "recorded_strict_error": copy.deepcopy(failed.get("error")),
        "transformations": transformations,
        "provider_response": {
            **strict._provider_identity(response),
            "usage": strict._usage_snapshot(response),
        },
        "state_metrics": metrics,
        "artifacts": {
            "translation_sha256": strict._sha256_file(out / "translation.txt"),
            "state_update_sha256": strict._sha256_file(out / "state_update.json"),
            "state_update_identity_sha256": metrics[
                "state_update_identity_sha256"
            ],
            "state_after_sha256": strict._sha256_file(out / "state_after.json"),
            "state_after_identity_sha256": metrics[
                "state_after_identity_sha256"
            ],
        },
        "safety": {
            "new_provider_calls": 0,
            "new_paid_requests": 0,
            "strict_failure_preserved": True,
            "human_semantic_selection": False,
        },
    }
    strict._write_json_exclusive(out / "recovery.json", recovery)
    return recovery


def prepare_salvage(
    *,
    repo_root: Path,
    expected_repo_head: str,
    strict_run_root: Path,
    expected_strict_tree_sha256: str,
    salvage_root: Path,
):
    repo_root = Path(repo_root).resolve()
    head = strict._ensure_repo_head(repo_root, expected_repo_head)
    strict_root = Path(strict_run_root).resolve()
    strict_tree = strict._external_tree_sha(strict_root)
    if strict_tree != str(expected_strict_tree_sha256).lower():
        raise BenchmarkSafetyError(
            f"strict runtime tree drifted: {strict_tree} != {expected_strict_tree_sha256}"
        )

    strict_plan = strict._load_run_plan(strict_root)
    _strict_terminal_status(strict_root, strict_plan)
    for arm in strict.SYNC_ARMS:
        _strict_failure_evidence(strict_root, strict_plan, arm)

    canonical_root = Path(strict_plan["canonical"]["root"])
    canonical_plan, canonical_plan_sha, canonical_tree_sha = strict._validate_canonical(
        canonical_root,
        expected_plan_sha256=strict_plan["canonical"]["plan_sha256"],
        expected_identity_sha256=strict_plan["canonical"]["identity_sha256"],
        expected_tree_sha256=strict_plan["canonical"]["tree_sha256"],
    )
    root = _external_root(repo_root, canonical_root, strict_root, salvage_root)
    protected = strict._protected_state(repo_root)

    identity = {
        "kind": SALVAGE_KIND,
        "repo_head": head,
        "strict_run_id": strict_plan["run_id"],
        "strict_runtime_tree_sha256": strict_tree,
        "canonical_identity_sha256": canonical_plan["identity_sha256"],
        "canonical_plan_sha256": canonical_plan_sha,
        "canonical_tree_sha256": canonical_tree_sha,
        "state_protocol_version": strict.STATE_PROTOCOL_VERSION,
        "state_protocol_sha256": strict._sha256_text(strict.STATE_PROTOCOL),
        "recovery_policy": {
            "missing_state_update": "noop_carry_prior_continuity_and_issues",
            "duplicate_source": "first_occurrence_wins_by_response_order",
            "human_semantic_selection": False,
            "other_validation_errors": "fail_closed",
        },
    }
    run_id = strict._canonical_json_sha256(identity)
    plan = {
        "schema_version": SALVAGE_SCHEMA_VERSION,
        "kind": SALVAGE_KIND,
        "status": "prepared_zero_provider",
        "run_id": run_id,
        "identity": identity,
        "repo_root": str(repo_root),
        "repo_head": head,
        "strict_source": {
            "root": str(strict_root),
            "run_id": strict_plan["run_id"],
            "tree_sha256": strict_tree,
        },
        "canonical": {
            "root": str(canonical_root),
            "identity_sha256": canonical_plan["identity_sha256"],
            "plan_sha256": canonical_plan_sha,
            "tree_sha256": canonical_tree_sha,
        },
        "protected_state_before": protected,
        "authorization": {
            "provider_calls_enabled": False,
            "historical_recovery_provider_calls": 0,
            "historical_recovery_paid_requests": 0,
        },
    }

    root.mkdir(parents=True, exist_ok=False)
    strict._write_json_exclusive(root / PLAN_FILENAME, plan)
    strict._write_exclusive(root / JOURNAL_FILENAME, b"")
    strict._append_journal(
        root / JOURNAL_FILENAME,
        {
            "event": "prepared_zero_provider",
            "run_id": run_id,
            "strict_run_id": strict_plan["run_id"],
            "strict_tree_sha256": strict_tree,
            "provider_calls": 0,
        },
    )

    recoveries = {
        arm: _recover_one(
            root,
            strict_root,
            strict_plan,
            canonical_root,
            canonical_plan,
            arm,
        )
        for arm in strict.SYNC_ARMS
    }
    strict._append_journal(
        root / JOURNAL_FILENAME,
        {
            "event": "historical_failures_recovered",
            "provider_calls": 0,
            "recoveries": {
                arm: item["identity_sha256"]
                for arm, item in recoveries.items()
            },
        },
    )
    strict._ensure_protected_state(
        repo_root, protected, "after zero-provider salvage preparation"
    )
    return plan, recoveries


def status(salvage_root: Path):
    root = Path(salvage_root).resolve()
    plan = strict._load_json(root / PLAN_FILENAME, "salvage plan")
    if (
        plan.get("schema_version") != SALVAGE_SCHEMA_VERSION
        or plan.get("kind") != SALVAGE_KIND
    ):
        raise SalvageRuntimeError("salvage plan schema/kind mismatch")
    strict_root = Path(plan["strict_source"]["root"])
    current_tree = strict._external_tree_sha(strict_root)
    if current_tree != plan["strict_source"]["tree_sha256"]:
        raise BenchmarkSafetyError("strict source runtime drifted")
    result = {
        "run_id": plan["run_id"],
        "status": plan["status"],
        "provider_calls_enabled": False,
        "strict_run_id": plan["strict_source"]["run_id"],
        "strict_runtime_tree_sha256": current_tree,
        "recoveries": {},
    }
    for arm in strict.SYNC_ARMS:
        failure = FAILURES[arm]
        path = root / "recoveries" / arm / failure["case_id"] / "recovery.json"
        recovery = strict._load_json(path, "salvage recovery")
        result["recoveries"][arm] = {
            "status": recovery["status"],
            "case_id": failure["case_id"],
            "policy": recovery["identity"]["recovery_policy"],
            "identity_sha256": recovery["identity_sha256"],
            "state_after_identity_sha256": recovery["artifacts"][
                "state_after_identity_sha256"
            ],
            "new_provider_calls": recovery["safety"]["new_provider_calls"],
        }
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        description="Zero-provider chronological salvage foundation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo-root", required=True)
    prepare.add_argument("--expected-repo-head", required=True)
    prepare.add_argument("--strict-run-root", required=True)
    prepare.add_argument("--expected-strict-tree-sha256", required=True)
    prepare.add_argument("--salvage-root", required=True)

    show = sub.add_parser("status")
    show.add_argument("--salvage-root", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            plan, recoveries = prepare_salvage(
                repo_root=Path(args.repo_root),
                expected_repo_head=args.expected_repo_head,
                strict_run_root=Path(args.strict_run_root),
                expected_strict_tree_sha256=args.expected_strict_tree_sha256,
                salvage_root=Path(args.salvage_root),
            )
            print(
                json.dumps(
                    {
                        "status": plan["status"],
                        "run_id": plan["run_id"],
                        "provider_calls": 0,
                        "recoveries": {
                            arm: item["identity_sha256"]
                            for arm, item in recoveries.items()
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "status":
            print(
                json.dumps(
                    status(Path(args.salvage_root)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
