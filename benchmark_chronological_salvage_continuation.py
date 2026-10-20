"""Paid secondary continuation for the chronological state-growth benchmark.

The strict Phase 59B/59C runtime remains authoritative and immutable. Phase 59D
provides deterministic zero-provider recovery of the two historical strict
failures. This module starts a *separate* continuation lineage from those
recovered states and never rewrites a strict case as successful.

Frozen secondary policy:
- provider-visible prompts/model settings remain identical to the strict run;
- missing top-level state_update -> deterministic no-op state update;
- duplicate glossary source terms -> first occurrence wins by raw response order;
- all other validation failures remain fail-closed;
- automatic retries are forbidden;
- each execute-next authorization permits exactly one paid request;
- Batch replay is intentionally out of scope for Phase 59E.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import benchmark_chronological_runtime as strict
import benchmark_chronological_salvage as foundation
from benchmark_novel import BenchmarkSafetyError

SCHEMA_VERSION = 1
KIND = "chronological-state-growth-salvage-continuation-v1"
PLAN_FILENAME = "continuation_plan.json"
JOURNAL_FILENAME = "continuation_journal.jsonl"
FOUNDATION_DIRNAME = "foundation"
CONTINUATION_DIRNAME = "continuation"

FLASH_AUTHORIZATION = "AUTHORIZE_ONE_CHRONOLOGICAL_FLASH_SALVAGE_REQUEST"
LUNA_AUTHORIZATION = "AUTHORIZE_ONE_CHRONOLOGICAL_LUNA_SALVAGE_REQUEST"
AUTHORIZATIONS = {
    strict.FLASH_ARM: FLASH_AUTHORIZATION,
    strict.LUNA_ARM: LUNA_AUTHORIZATION,
}

DEFAULT_FLASH_BUDGET_USD = 0.75
DEFAULT_LUNA_BUDGET_USD = 0.75


class SalvageContinuationError(strict.ChronologicalRuntimeError):
    pass


def _ensure_external_root(repo_root, canonical_root, strict_root, root, *, exists):
    root = strict._ensure_external_run_root(repo_root, canonical_root, root, exists=exists)
    strict_root = Path(strict_root).resolve()
    if root == strict_root or strict_root in root.parents:
        raise BenchmarkSafetyError("continuation root must be outside strict runtime")
    return root


def _remaining_cases(arm: str):
    if arm not in strict.SYNC_ARMS:
        raise SalvageContinuationError(f"unsupported continuation arm: {arm}")
    failure = foundation.FAILURES[arm]
    ordered = list(strict._case_order())
    needle = (failure["novel_id"], failure["chapter_number"])
    try:
        index = ordered.index(needle)
    except ValueError as exc:
        raise BenchmarkSafetyError(f"strict failure is not in canonical case order: {needle!r}") from exc
    return ordered[index + 1 :]


def _case_dir(root: Path, arm: str, novel_id: str, chapter: int):
    return (
        Path(root)
        / CONTINUATION_DIRNAME
        / "arms"
        / arm
        / "cases"
        / strict._case_id(novel_id, chapter)
    )


def _paths(case_dir: Path):
    return strict._sync_complete_paths(case_dir)


def _foundation_root(root: Path):
    return Path(root) / FOUNDATION_DIRNAME


def _foundation_recovery_dir(root: Path, arm: str):
    failure = foundation.FAILURES[arm]
    return (
        _foundation_root(root)
        / "recoveries"
        / arm
        / failure["case_id"]
    )


def _load_plan(root: Path):
    value = strict._load_json(Path(root) / PLAN_FILENAME, "salvage continuation plan")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != KIND:
        raise SalvageContinuationError("continuation plan schema/kind mismatch")
    return value


def _validate_static_sources(root: Path, plan: dict):
    repo_root = Path(plan["repo_root"])
    strict._ensure_repo_head(repo_root, plan["repo_head"])
    strict._ensure_protected_state(
        repo_root,
        plan["protected_state_before"],
        "while validating salvage continuation",
    )

    strict_root = Path(plan["strict_source"]["root"])
    strict_tree = strict._external_tree_sha(strict_root)
    if strict_tree != plan["strict_source"]["tree_sha256"]:
        raise BenchmarkSafetyError("strict source runtime drifted")
    strict_plan = strict._load_run_plan(strict_root)
    if strict_plan["run_id"] != plan["strict_source"]["run_id"]:
        raise BenchmarkSafetyError("strict source run identity drifted")
    foundation._strict_terminal_status(strict_root, strict_plan)

    foundation_root = _foundation_root(root)
    foundation_tree = strict._external_tree_sha(foundation_root)
    if foundation_tree != plan["foundation"]["tree_sha256"]:
        raise BenchmarkSafetyError("embedded salvage foundation drifted")
    foundation_status = foundation.status(foundation_root)
    if foundation_status["run_id"] != plan["foundation"]["run_id"]:
        raise BenchmarkSafetyError("embedded salvage foundation identity drifted")
    for arm in strict.SYNC_ARMS:
        expected = plan["foundation"]["recoveries"][arm]
        actual = foundation_status["recoveries"][arm]
        if actual["identity_sha256"] != expected["identity_sha256"]:
            raise BenchmarkSafetyError(f"embedded recovery identity drifted for {arm}")
        if actual["state_after_identity_sha256"] != expected["state_after_identity_sha256"]:
            raise BenchmarkSafetyError(f"embedded recovered state drifted for {arm}")

    canonical_root = Path(plan["canonical"]["root"])
    canonical_plan, plan_sha, tree_sha = strict._validate_canonical(
        canonical_root,
        expected_plan_sha256=plan["canonical"]["plan_sha256"],
        expected_identity_sha256=plan["canonical"]["identity_sha256"],
        expected_tree_sha256=plan["canonical"]["tree_sha256"],
    )
    if plan_sha != plan["canonical"]["plan_sha256"] or tree_sha != plan["canonical"]["tree_sha256"]:
        raise BenchmarkSafetyError("canonical identity drifted")

    for arm in strict.SYNC_ARMS:
        _, snapshot = strict._model_snapshot(arm)
        if snapshot != plan["identity"]["model_configs"][arm]:
            raise BenchmarkSafetyError(f"continuation model configuration drifted for {arm}")

    return strict_root, strict_plan, canonical_root, canonical_plan, foundation_status


def _state_before_for_case(root, plan, arm, novel_id, chapter):
    (
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
        _,
    ) = _validate_static_sources(root, plan)
    previous_chapter = chapter - 1
    if previous_chapter < 1:
        raise BenchmarkSafetyError("continuation unexpectedly requested Chapter 1 state")

    previous_continuation = _case_dir(root, arm, novel_id, previous_chapter)
    if previous_continuation.exists():
        complete = _validate_complete(
            root,
            plan,
            arm,
            novel_id,
            previous_chapter,
        )
        state_path = previous_continuation / "state_after.json"
        state = strict._load_json(state_path, "prior continuation state")
        identity = complete["artifacts"]["state_after_identity_sha256"]
        if strict._canonical_json_sha256(state) != identity:
            raise BenchmarkSafetyError("prior continuation state identity drifted")
        return state, identity, str(state_path)

    failure = foundation.FAILURES[arm]
    if (
        failure["novel_id"] == novel_id
        and failure["chapter_number"] == previous_chapter
    ):
        recovery_dir = _foundation_recovery_dir(root, arm)
        recovery = strict._load_json(recovery_dir / "recovery.json", "salvage recovery")
        state_path = recovery_dir / "state_after.json"
        state = strict._load_json(state_path, "recovered state")
        identity = recovery["artifacts"]["state_after_identity_sha256"]
        if strict._canonical_json_sha256(state) != identity:
            raise BenchmarkSafetyError("recovered state identity drifted")
        return state, identity, str(state_path)

    strict_complete = strict._validate_sync_complete(
        strict_root,
        canonical_root,
        canonical_plan,
        arm,
        novel_id,
        previous_chapter,
        recurse_state=False,
    )
    state_path = strict._case_dir(
        strict_root,
        arm,
        novel_id,
        previous_chapter,
    ) / "state_after.json"
    state = strict._load_json(state_path, "strict predecessor state")
    identity = strict_complete["artifacts"]["state_after_identity_sha256"]
    if strict._canonical_json_sha256(state) != identity:
        raise BenchmarkSafetyError("strict predecessor state identity drifted")
    return state, identity, str(state_path)


def _case_identity(plan, arm, novel_id, chapter, record, state_before_sha, request_sha):
    return {
        "kind": KIND,
        "run_id": plan["run_id"],
        "strict_run_id": plan["strict_source"]["run_id"],
        "foundation_run_id": plan["foundation"]["run_id"],
        "canonical_identity_sha256": plan["canonical"]["identity_sha256"],
        "arm": arm,
        "novel_id": novel_id,
        "chapter_number": chapter,
        "case_id": strict._case_id(novel_id, chapter),
        "source_sha256": record["source_sha256"],
        "state_before_identity_sha256": state_before_sha,
        "request_sha256": request_sha,
        **plan["identity"]["model_configs"][arm],
    }


def _validate_complete(root, plan, arm, novel_id, chapter):
    case_dir = _case_dir(root, arm, novel_id, chapter)
    paths = _paths(case_dir)
    for key in (
        "request",
        "journal",
        "response",
        "translation",
        "state_update",
        "state_after",
        "complete",
    ):
        if not paths[key].is_file():
            raise BenchmarkSafetyError(f"continuation complete artifact missing: {paths[key]}")
    if paths["uncertain"].exists() or paths["response_failed"].exists():
        raise BenchmarkSafetyError("continuation complete conflicts with failure marker")

    complete = strict._load_json(paths["complete"], "salvage continuation complete")
    manifest = strict._load_json(case_dir / "manifest.json", "salvage continuation manifest")
    if complete.get("status") != "complete" or complete.get("kind") != KIND:
        raise BenchmarkSafetyError("continuation completion status/kind mismatch")
    if complete.get("identity") != manifest.get("identity"):
        raise BenchmarkSafetyError("continuation manifest/complete identity mismatch")
    identity = complete["identity"]
    if strict._canonical_json_sha256(identity) != complete["identity_sha256"]:
        raise BenchmarkSafetyError("continuation identity hash drifted")
    if identity.get("run_id") != plan["run_id"] or identity.get("arm") != arm:
        raise BenchmarkSafetyError("continuation identity run/arm mismatch")
    if identity.get("novel_id") != novel_id or identity.get("chapter_number") != chapter:
        raise BenchmarkSafetyError("continuation identity case mismatch")

    state_before, state_before_sha, _ = _state_before_for_case(
        root, plan, arm, novel_id, chapter
    )
    if identity["state_before_identity_sha256"] != state_before_sha:
        raise BenchmarkSafetyError("continuation state-before identity drifted")

    canonical_root = Path(plan["canonical"]["root"])
    canonical_plan = strict._load_json(
        canonical_root / "chronological_state_plan.json",
        "canonical plan",
    )
    expected_request, record, config, _ = strict._build_request(
        canonical_root,
        canonical_plan,
        arm,
        novel_id,
        chapter,
        state_before,
    )
    request = strict._load_json(paths["request"], "continuation request")
    if request != expected_request:
        raise BenchmarkSafetyError("continuation request no longer matches deterministic request")
    request_sha = strict._canonical_json_sha256(request)
    if request_sha != identity["request_sha256"]:
        raise BenchmarkSafetyError("continuation request identity drifted")
    if record["source_sha256"] != identity["source_sha256"]:
        raise BenchmarkSafetyError("continuation source identity drifted")

    response = strict._load_json(paths["response"], "continuation response")
    provider = strict._provider_identity(response)
    if provider["response_model"] != config.model or provider["finish_reason"] != "stop":
        raise BenchmarkSafetyError("continuation provider identity drifted")
    document, transformations = foundation.parse_salvage_document(
        strict._response_content(response), state_before
    )
    if transformations != complete.get("salvage_transformations", []):
        raise BenchmarkSafetyError("continuation salvage transformation log drifted")
    if paths["translation"].read_text(encoding="utf-8") != document["translation"]:
        raise BenchmarkSafetyError("continuation translation drifted")
    state_update = strict._load_json(paths["state_update"], "continuation state update")
    if state_update != document["state_update"]:
        raise BenchmarkSafetyError("continuation state update drifted")
    expected_after, metrics = strict.apply_state_update(state_before, document, chapter)
    state_after = strict._load_json(paths["state_after"], "continuation state after")
    if state_after != expected_after:
        raise BenchmarkSafetyError("continuation state-after replay mismatch")
    if metrics != complete["state_metrics"]:
        raise BenchmarkSafetyError("continuation state metrics drifted")

    artifacts = complete["artifacts"]
    checks = {
        "request_sha256": strict._sha256_file(paths["request"]),
        "response_sha256": strict._sha256_file(paths["response"]),
        "translation_sha256": strict._sha256_file(paths["translation"]),
        "state_update_file_sha256": strict._sha256_file(paths["state_update"]),
        "state_after_file_sha256": strict._sha256_file(paths["state_after"]),
    }
    for key, actual in checks.items():
        if artifacts.get(key) != actual:
            raise BenchmarkSafetyError(f"continuation artifact hash drifted: {key}")
    if artifacts.get("state_update_identity_sha256") != metrics["state_update_identity_sha256"]:
        raise BenchmarkSafetyError("continuation state-update identity drifted")
    if artifacts.get("state_after_identity_sha256") != metrics["state_after_identity_sha256"]:
        raise BenchmarkSafetyError("continuation state-after identity drifted")
    if strict._journal_events(paths["journal"]) != [
        "planned",
        "started",
        "response_recorded",
        "complete",
    ]:
        raise BenchmarkSafetyError("continuation complete journal sequence mismatch")
    return complete


def _case_status(root, plan, arm, novel_id, chapter):
    case_dir = _case_dir(root, arm, novel_id, chapter)
    paths = _paths(case_dir)
    if paths["uncertain"].is_file():
        return "uncertain"
    if paths["response_failed"].is_file():
        return "response_failed"
    if paths["complete"].is_file():
        try:
            _validate_complete(root, plan, arm, novel_id, chapter)
        except Exception:
            return "invalid_complete"
        return "complete"
    if case_dir.exists():
        return "incomplete_started"
    return "planned"


def arm_status(plan, root: Path, arm: str):
    rows = []
    counts = {
        key: 0
        for key in (
            "planned",
            "complete",
            "uncertain",
            "response_failed",
            "invalid_complete",
            "incomplete_started",
        )
    }
    measured_cost = 0.0
    paid_attempts = 0
    transformations = {}
    for novel_id, chapter in _remaining_cases(arm):
        case_dir = _case_dir(root, arm, novel_id, chapter)
        status_value = _case_status(root, plan, arm, novel_id, chapter)
        counts[status_value] += 1
        rows.append(
            {
                "novel_id": novel_id,
                "chapter_number": chapter,
                "case_id": strict._case_id(novel_id, chapter),
                "status": status_value,
            }
        )
        paid_attempts += strict._journal_events(case_dir / "journal.jsonl").count("started")
        if status_value == "complete":
            complete = strict._load_json(case_dir / "complete.json", "continuation complete")
            measured_cost += float((complete.get("cost") or {}).get("total_cost_usd", 0.0))
            for item in complete.get("salvage_transformations", []):
                kind = item.get("kind", "unknown")
                transformations[kind] = transformations.get(kind, 0) + 1
    blocked = any(
        counts[name]
        for name in (
            "uncertain",
            "response_failed",
            "invalid_complete",
            "incomplete_started",
        )
    )
    next_case = None
    if not blocked:
        next_case = next((row for row in rows if row["status"] == "planned"), None)
    expected = len(rows)
    return {
        "arm": arm,
        "expected_continuation_cases": expected,
        "counts": counts,
        "completed_total": counts["complete"],
        "paid_attempts": paid_attempts,
        "measured_cost_usd": measured_cost,
        "salvage_transformations": transformations,
        "blocked": blocked,
        "next_case": (
            {
                "novel_id": next_case["novel_id"],
                "chapter_number": next_case["chapter_number"],
                "case_id": next_case["case_id"],
            }
            if next_case
            else None
        ),
        "complete": counts["complete"] == expected and not blocked,
    }


def status(root: Path):
    root = Path(root).resolve()
    plan = _load_plan(root)
    _validate_static_sources(root, plan)
    return {
        "run_id": plan["run_id"],
        "status": plan["status"],
        "strict_run_id": plan["strict_source"]["run_id"],
        "foundation_run_id": plan["foundation"]["run_id"],
        "provider_calls_enabled": True,
        "automatic_retry_forbidden": True,
        "batch_enabled": False,
        "arms": {
            arm: arm_status(plan, root, arm)
            for arm in strict.SYNC_ARMS
        },
    }


def prepare_continuation(
    *,
    repo_root: Path,
    expected_repo_head: str,
    strict_run_root: Path,
    expected_strict_tree_sha256: str,
    run_root: Path,
    flash_budget_usd: float = DEFAULT_FLASH_BUDGET_USD,
    luna_budget_usd: float = DEFAULT_LUNA_BUDGET_USD,
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
    foundation._strict_terminal_status(strict_root, strict_plan)
    canonical_root = Path(strict_plan["canonical"]["root"])
    canonical_plan, canonical_plan_sha, canonical_tree_sha = strict._validate_canonical(
        canonical_root,
        expected_plan_sha256=strict_plan["canonical"]["plan_sha256"],
        expected_identity_sha256=strict_plan["canonical"]["identity_sha256"],
        expected_tree_sha256=strict_plan["canonical"]["tree_sha256"],
    )
    root = _ensure_external_root(
        repo_root,
        canonical_root,
        strict_root,
        run_root,
        exists=False,
    )
    budgets = {
        strict.FLASH_ARM: float(flash_budget_usd),
        strict.LUNA_ARM: float(luna_budget_usd),
    }
    if any(value <= 0 or value > 10 for value in budgets.values()):
        raise SalvageContinuationError("each continuation budget must be >0 and <=10 USD")
    model_configs = {}
    for arm in strict.SYNC_ARMS:
        _, snapshot = strict._model_snapshot(arm)
        if snapshot != strict_plan["identity"]["model_configs"][arm]:
            raise BenchmarkSafetyError(f"strict model snapshot drifted for {arm}")
        model_configs[arm] = snapshot
    protected = strict._protected_state(repo_root)

    root.mkdir(parents=True, exist_ok=False)
    foundation_root = _foundation_root(root)
    foundation_plan, recoveries = foundation.prepare_salvage(
        repo_root=repo_root,
        expected_repo_head=head,
        strict_run_root=strict_root,
        expected_strict_tree_sha256=strict_tree,
        salvage_root=foundation_root,
    )
    foundation_tree = strict._external_tree_sha(foundation_root)

    case_sets = {}
    for arm in strict.SYNC_ARMS:
        rows = []
        for ordinal, (novel_id, chapter) in enumerate(_remaining_cases(arm), 1):
            record = next(
                row
                for row in strict._novel_record(canonical_plan, novel_id)["chapter_records"]
                if row["chapter_number"] == chapter
            )
            rows.append(
                {
                    "continuation_ordinal": ordinal,
                    "case_id": strict._case_id(novel_id, chapter),
                    "novel_id": novel_id,
                    "chapter_number": chapter,
                    "source_sha256": record["source_sha256"],
                }
            )
        case_sets[arm] = rows

    identity = {
        "kind": KIND,
        "repo_head": head,
        "strict_run_id": strict_plan["run_id"],
        "strict_runtime_tree_sha256": strict_tree,
        "foundation_run_id": foundation_plan["run_id"],
        "foundation_tree_sha256": foundation_tree,
        "foundation_recoveries": {
            arm: {
                "identity_sha256": recoveries[arm]["identity_sha256"],
                "state_after_identity_sha256": recoveries[arm]["artifacts"][
                    "state_after_identity_sha256"
                ],
            }
            for arm in strict.SYNC_ARMS
        },
        "canonical_identity_sha256": canonical_plan["identity_sha256"],
        "canonical_plan_sha256": canonical_plan_sha,
        "canonical_tree_sha256": canonical_tree_sha,
        "state_protocol_version": strict.STATE_PROTOCOL_VERSION,
        "state_protocol_sha256": strict._sha256_text(strict.STATE_PROTOCOL),
        "model_configs": model_configs,
        "recovery_policy": foundation_plan["identity"]["recovery_policy"],
        "continuation_cases": case_sets,
        "continuation_budgets_usd": budgets,
        "batch_enabled": False,
    }
    run_id = strict._canonical_json_sha256(identity)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "prepared",
        "run_id": run_id,
        "identity": identity,
        "repo_root": str(repo_root),
        "repo_head": head,
        "strict_source": {
            "root": str(strict_root),
            "run_id": strict_plan["run_id"],
            "tree_sha256": strict_tree,
        },
        "foundation": {
            "root": str(foundation_root),
            "run_id": foundation_plan["run_id"],
            "tree_sha256": foundation_tree,
            "recoveries": identity["foundation_recoveries"],
        },
        "canonical": {
            "root": str(canonical_root),
            "identity_sha256": canonical_plan["identity_sha256"],
            "plan_sha256": canonical_plan_sha,
            "tree_sha256": canonical_tree_sha,
        },
        "budgets_usd": budgets,
        "protected_state_before": protected,
        "authorization": {
            "automatic_retry_forbidden": True,
            "ambiguous_outcome_retry_forbidden": True,
            "maximum_new_paid_requests": {
                arm: len(case_sets[arm]) for arm in strict.SYNC_ARMS
            },
            "batch_enabled": False,
        },
    }
    strict._write_json_exclusive(root / PLAN_FILENAME, plan)
    strict._write_exclusive(root / JOURNAL_FILENAME, b"")
    strict._append_journal(
        root / JOURNAL_FILENAME,
        {
            "event": "prepared",
            "run_id": run_id,
            "strict_run_id": strict_plan["run_id"],
            "foundation_run_id": foundation_plan["run_id"],
            "strict_tree_sha256": strict_tree,
            "foundation_tree_sha256": foundation_tree,
            "provider_calls": 0,
            "batch_enabled": False,
        },
    )
    strict._ensure_protected_state(repo_root, protected, "after continuation preparation")
    return plan


def _assert_request_budget(plan, arm, arm_state, config, request):
    cap = float(plan["budgets_usd"][arm])
    maximum = int(plan["authorization"]["maximum_new_paid_requests"][arm])
    if arm_state["paid_attempts"] >= maximum:
        raise BenchmarkSafetyError(f"{arm} continuation paid-request count cap reached")
    if arm_state["measured_cost_usd"] >= cap:
        raise BenchmarkSafetyError(f"{arm} continuation measured budget cap reached")
    worst = strict._worst_case_sync_request_cost(config, request)
    if arm_state["measured_cost_usd"] + worst > cap:
        raise BenchmarkSafetyError(
            f"{arm} conservative continuation request ceiling would exceed budget: "
            f"${arm_state['measured_cost_usd'] + worst:.6f} > ${cap:.6f}"
        )
    return worst


def execute_next(
    root: Path,
    *,
    arm: str,
    authorization: str,
    now=strict.utc_now,
    timer=time.perf_counter,
    client_factory=strict.create_benchmark_client,
):
    root = Path(root).resolve()
    plan = _load_plan(root)
    if arm not in strict.SYNC_ARMS:
        raise SalvageContinuationError(f"unsupported continuation arm: {arm}")
    if authorization != AUTHORIZATIONS[arm]:
        raise SalvageContinuationError("exact one-call salvage authorization token is required")
    (
        _,
        _,
        canonical_root,
        canonical_plan,
        _,
    ) = _validate_static_sources(root, plan)
    arm_state = arm_status(plan, root, arm)
    if arm_state["blocked"]:
        raise BenchmarkSafetyError(
            f"{arm} salvage continuation contains uncertain/response_failed/invalid/incomplete evidence; further paid calls are blocked"
        )
    if arm_state["complete"]:
        raise SalvageContinuationError(f"{arm} salvage continuation is already complete")
    case = arm_state["next_case"]
    novel_id = case["novel_id"]
    chapter = case["chapter_number"]
    state_before, state_before_sha, state_before_path = _state_before_for_case(
        root, plan, arm, novel_id, chapter
    )
    request, source_record, config, _ = strict._build_request(
        canonical_root,
        canonical_plan,
        arm,
        novel_id,
        chapter,
        state_before,
    )
    ceiling = _assert_request_budget(plan, arm, arm_state, config, request)
    request_sha = strict._canonical_json_sha256(request)
    identity = _case_identity(
        plan,
        arm,
        novel_id,
        chapter,
        source_record,
        state_before_sha,
        request_sha,
    )
    identity_sha = strict._canonical_json_sha256(identity)
    case_dir = _case_dir(root, arm, novel_id, chapter)
    if case_dir.exists():
        raise BenchmarkSafetyError(f"continuation case directory already exists: {case_dir}")
    case_dir.mkdir(parents=True, exist_ok=False)
    paths = _paths(case_dir)
    strict._write_exclusive(paths["request"], strict._canonical_json_bytes(request))
    strict._write_exclusive(paths["journal"], b"")
    planned = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "planned",
        "identity_sha256": identity_sha,
        "identity": identity,
        "source_state_before_path": state_before_path,
        "authorization": {
            "paid_requests_authorized": 1,
            "automatic_retry_forbidden": True,
            "ambiguous_outcome_retry_forbidden": True,
            "conservative_request_cost_ceiling_usd": ceiling,
            "arm_budget_usd": float(plan["budgets_usd"][arm]),
            "measured_continuation_cost_before_usd": arm_state["measured_cost_usd"],
        },
    }
    strict._write_json_exclusive(case_dir / "manifest.json", planned)
    strict._append_journal(
        paths["journal"],
        {
            "event": "planned",
            "at_utc": now().isoformat(),
            "identity_sha256": identity_sha,
            "request_sha256": request_sha,
        },
    )
    repo_root = Path(plan["repo_root"])
    protected_before = strict._ensure_protected_state(
        repo_root,
        plan["protected_state_before"],
        "before salvage continuation request",
    )
    request_started = now()
    strict._append_journal(
        paths["journal"],
        {
            "event": "started",
            "at_utc": request_started.isoformat(),
            "attempt": 1,
            "request_sha256": request_sha,
            "model": config.model,
            "automatic_retry_forbidden": True,
        },
    )
    client = client_factory(config)
    started = timer()
    try:
        response = client.chat.completions.create(**request)
        elapsed = timer() - started
    except BaseException as exc:
        elapsed = timer() - started
        uncertain = {
            **planned,
            "status": "uncertain",
            "elapsed_seconds": elapsed,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "safety": {
                "provider_calls_attempted": 1,
                "paid_requests_attempted": 1,
                "provider_response_recorded": False,
                "retry_forbidden": True,
            },
        }
        strict._append_journal(
            paths["journal"],
            {
                "event": "uncertain",
                "at_utc": now().isoformat(),
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "retry_forbidden": True,
            },
        )
        strict._write_json_exclusive(paths["uncertain"], uncertain)
        raise strict.ChronologicalUncertainError(
            f"{arm} salvage transport outcome is ambiguous; retry is forbidden"
        ) from exc

    raw_response = strict._provider_dict(response)
    strict._write_json_exclusive(paths["response"], raw_response)
    provider = strict._provider_identity(response)
    usage = strict._usage_snapshot(response)
    strict._append_journal(
        paths["journal"],
        {
            "event": "response_recorded",
            "at_utc": now().isoformat(),
            "elapsed_seconds": elapsed,
            **provider,
            "usage": usage,
        },
    )
    try:
        if provider["response_model"] != config.model:
            raise BenchmarkSafetyError(
                f"provider model mismatch: {provider['response_model']!r} != {config.model!r}"
            )
        if provider["finish_reason"] != "stop":
            raise SalvageContinuationError(
                f"provider finish_reason is not stop: {provider['finish_reason']!r}"
            )
        content = strict._response_content(response)
        model_document, transformations = foundation.parse_salvage_document(
            content, state_before
        )
        state_after, state_metrics = strict.apply_state_update(
            state_before,
            model_document,
            chapter,
        )
        translation = model_document["translation"]
        state_update = model_document["state_update"]
        strict._write_text_exclusive(paths["translation"], translation)
        strict._write_json_exclusive(paths["state_update"], state_update)
        strict._write_json_exclusive(paths["state_after"], state_after)
        protected_after = strict._ensure_protected_state(
            repo_root,
            protected_before,
            "after salvage continuation response processing",
        )
        pricing = strict._pricing_result(config, request_started, usage, batch=False)
        residual = strict.residual_source_script_report(
            translation,
            strict._novel_record(canonical_plan, novel_id)["source_language"],
        )
    except BaseException as exc:
        protected_after = strict._protected_state(repo_root)
        failed = {
            **planned,
            "status": "response_failed",
            "elapsed_seconds": elapsed,
            "provider_response": {**provider, "usage": usage},
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "safety": {
                "provider_calls_attempted": 1,
                "paid_requests_attempted": 1,
                "provider_response_recorded": True,
                "protected_state_verified_unchanged": protected_after == protected_before,
                "retry_forbidden": True,
            },
        }
        strict._append_journal(
            paths["journal"],
            {
                "event": "response_failed",
                "at_utc": now().isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "retry_forbidden": True,
            },
        )
        strict._write_json_exclusive(paths["response_failed"], failed)
        raise SalvageContinuationError(
            f"{arm} salvage received a provider response but validation failed; retry is forbidden"
        ) from exc

    artifacts = {
        "request_sha256": strict._sha256_file(paths["request"]),
        "response_sha256": strict._sha256_file(paths["response"]),
        "translation_sha256": strict._sha256_file(paths["translation"]),
        "state_update_file_sha256": strict._sha256_file(paths["state_update"]),
        "state_update_identity_sha256": state_metrics["state_update_identity_sha256"],
        "state_after_file_sha256": strict._sha256_file(paths["state_after"]),
        "state_after_identity_sha256": state_metrics["state_after_identity_sha256"],
    }
    final = {
        **planned,
        "status": "complete",
        "request_started_utc": request_started.isoformat(),
        "response_recorded_utc": now().isoformat(),
        "elapsed_seconds": elapsed,
        "provider_response": {**provider, "usage": usage},
        "cost": pricing,
        "salvage_transformations": transformations,
        "state_metrics": state_metrics,
        "quality_signals": {
            "remaining_cjk_character_count": residual["remaining_cjk_character_count"],
            "residual_cjk_fragments": residual["residual_cjk_fragments"],
            "translation_character_count": len(translation),
            "translation_word_count": len(translation.split()),
        },
        "artifacts": artifacts,
        "safety": {
            "provider_calls_attempted": 1,
            "paid_requests_attempted": 1,
            "provider_response_recorded": True,
            "protected_state_verified_unchanged": True,
            "automatic_retry_forbidden": True,
            "strict_runtime_modified": False,
            "foundation_modified": False,
        },
    }
    strict._write_json_exclusive(paths["complete"], final)
    strict._append_journal(
        paths["journal"],
        {
            "event": "complete",
            "at_utc": now().isoformat(),
            "translation_sha256": artifacts["translation_sha256"],
            "state_after_identity_sha256": artifacts["state_after_identity_sha256"],
            "salvage_transformation_kinds": [
                item.get("kind") for item in transformations
            ],
            "protected_state_verified_unchanged": True,
            "retry_forbidden": True,
        },
    )
    return final


def build_parser():
    parser = argparse.ArgumentParser(
        description="Secondary paid chronological salvage continuation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo-root", required=True)
    prepare.add_argument("--expected-repo-head", required=True)
    prepare.add_argument("--strict-run-root", required=True)
    prepare.add_argument("--expected-strict-tree-sha256", required=True)
    prepare.add_argument("--run-root", required=True)
    prepare.add_argument("--flash-budget-usd", type=float, default=DEFAULT_FLASH_BUDGET_USD)
    prepare.add_argument("--luna-budget-usd", type=float, default=DEFAULT_LUNA_BUDGET_USD)

    show = sub.add_parser("status")
    show.add_argument("--run-root", required=True)

    execute = sub.add_parser("execute-next")
    execute.add_argument("--run-root", required=True)
    execute.add_argument("--arm", choices=list(strict.SYNC_ARMS), required=True)
    execute.add_argument("--authorization", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            plan = prepare_continuation(
                repo_root=Path(args.repo_root),
                expected_repo_head=args.expected_repo_head,
                strict_run_root=Path(args.strict_run_root),
                expected_strict_tree_sha256=args.expected_strict_tree_sha256,
                run_root=Path(args.run_root),
                flash_budget_usd=args.flash_budget_usd,
                luna_budget_usd=args.luna_budget_usd,
            )
            print(
                json.dumps(
                    {
                        "status": plan["status"],
                        "run_id": plan["run_id"],
                        "provider_calls": 0,
                        "flash_remaining": len(plan["identity"]["continuation_cases"][strict.FLASH_ARM]),
                        "luna_remaining": len(plan["identity"]["continuation_cases"][strict.LUNA_ARM]),
                        "batch_enabled": False,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "status":
            print(json.dumps(status(Path(args.run_root)), indent=2, ensure_ascii=False))
            return 0
        if args.command == "execute-next":
            complete = execute_next(
                Path(args.run_root),
                arm=args.arm,
                authorization=args.authorization,
            )
            print(
                json.dumps(
                    {
                        "status": complete["status"],
                        "arm": args.arm,
                        "case_id": complete["identity"]["case_id"],
                        "cost_usd": complete["cost"]["total_cost_usd"],
                        "salvage_transformations": [
                            item.get("kind") for item in complete["salvage_transformations"]
                        ],
                    },
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
