"""Secondary Batch replay of the completed salvaged Luna chronological lineage.

The strict chronological runtime and Phase 59E salvage-continuation runtime remain
immutable source evidence. This module creates a separate external Batch root and
replays exactly the 50 provider-visible Luna request bodies that formed the
*effective salvaged* synchronous lineage:

- 34 strict synchronous successes;
- the original strict CN18 request whose response was deterministically recovered
  by Phase 59D;
- 15 Phase 59E continuation requests.

Batch results never mutate the synchronous state lineage. Known Phase 59D
mechanical schema repairs may be applied while collecting Batch output and are
logged per result; unrelated result failures remain explicit. Automatic retries
are forbidden and ambiguous remote operations fail closed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import benchmark_chronological_runtime as strict
import benchmark_chronological_salvage as foundation
import benchmark_chronological_salvage_continuation as continuation
from benchmark_novel import BenchmarkSafetyError

SCHEMA_VERSION = 1
KIND = "chronological-state-growth-salvaged-luna-batch-v1"
PLAN_FILENAME = "salvaged_luna_batch_plan.json"
INPUT_FILENAME = "batch_input.jsonl"
OUTPUT_FILENAME = "batch_output.jsonl"
JOURNAL_FILENAME = "batch_journal.jsonl"
RESULTS_DIRNAME = "results"
AUTHORIZATION = "AUTHORIZE_ONE_SALVAGED_LUNA_BATCH_REPLAY"
DEFAULT_BUDGET_USD = 0.75
REQUEST_COUNT = 50


class SalvagedBatchError(strict.ChronologicalRuntimeError):
    pass


def _plan_path(root: Path):
    return Path(root) / PLAN_FILENAME


def _journal_path(root: Path):
    return Path(root) / JOURNAL_FILENAME


def _input_path(root: Path):
    return Path(root) / INPUT_FILENAME


def _load_plan(root: Path):
    value = strict._load_json(_plan_path(root), "salvaged Luna Batch plan")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != KIND:
        raise SalvagedBatchError("salvaged Luna Batch plan schema/kind mismatch")
    if strict._sha256_file(_input_path(root)) != value["identity"]["input_sha256"]:
        raise BenchmarkSafetyError("salvaged Luna Batch input drifted")
    return value


def _ensure_external_root(repo_root, canonical_root, strict_root, continuation_root, root):
    root = Path(root)
    if not root.is_absolute():
        raise SalvagedBatchError("Batch root must be an absolute external path")
    root = root.resolve()
    protected = [
        Path(repo_root).resolve(),
        Path(canonical_root).resolve(),
        Path(strict_root).resolve(),
        Path(continuation_root).resolve(),
    ]
    for item in protected:
        if root == item or item in root.parents:
            raise BenchmarkSafetyError("Batch root must be outside all source/protected roots")
    if root.exists():
        raise BenchmarkSafetyError(f"Batch root already exists: {root}")
    return root


def _validate_historical_static_sources(root: Path, plan: dict):
    """Validate frozen Phase59E evidence without requiring its historical repo HEAD.

    Phase59E execution itself deliberately pins the live repository to the HEAD that
    created that lineage. Phase59H is a later, read-only Batch replay, so its own
    current repo HEAD is validated separately by prepare()/submit(). Requiring the
    live checkout to also equal Phase59E's historical HEAD would make every
    post-merge replay impossible.

    All evidence-bearing trees, identities, model snapshots, and protected
    production novel state remain fail-closed here.
    """

    root = Path(root).resolve()
    repo_root = Path(plan["repo_root"])

    # Historical source evidence remains bound to the same production novel
    # bytes/state, but not to the old Git checkout HEAD.
    strict._ensure_protected_state(
        repo_root,
        plan["protected_state_before"],
        "while validating salvaged Batch historical sources",
    )

    strict_root = Path(plan["strict_source"]["root"])
    strict_tree = strict._external_tree_sha(strict_root)

    if strict_tree != plan["strict_source"]["tree_sha256"]:
        raise BenchmarkSafetyError("strict source runtime drifted")

    strict_plan = strict._load_run_plan(strict_root)

    if strict_plan["run_id"] != plan["strict_source"]["run_id"]:
        raise BenchmarkSafetyError("strict source run identity drifted")

    foundation._strict_terminal_status(
        strict_root,
        strict_plan,
    )

    foundation_root = continuation._foundation_root(root)
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
            raise BenchmarkSafetyError(
                f"embedded recovery identity drifted for {arm}"
            )

        if (
            actual["state_after_identity_sha256"]
            != expected["state_after_identity_sha256"]
        ):
            raise BenchmarkSafetyError(
                f"embedded recovered state drifted for {arm}"
            )

    canonical_root = Path(plan["canonical"]["root"])

    canonical_plan, plan_sha, tree_sha = strict._validate_canonical(
        canonical_root,
        expected_plan_sha256=plan["canonical"]["plan_sha256"],
        expected_identity_sha256=plan["canonical"]["identity_sha256"],
        expected_tree_sha256=plan["canonical"]["tree_sha256"],
    )

    if (
        plan_sha != plan["canonical"]["plan_sha256"]
        or tree_sha != plan["canonical"]["tree_sha256"]
    ):
        raise BenchmarkSafetyError("canonical identity drifted")

    for arm in strict.SYNC_ARMS:
        _, snapshot = strict._model_snapshot(arm)

        if snapshot != plan["identity"]["model_configs"][arm]:
            raise BenchmarkSafetyError(
                f"continuation model configuration drifted for {arm}"
            )

    return (
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
        foundation_status,
    )



def _historical_state_before_for_case(
    root: Path,
    plan: dict,
    arm: str,
    novel_id: str,
    chapter: int,
    *,
    _cache=None,
):
    """Resolve frozen continuation state without Phase59E's live-HEAD gate."""

    root = Path(root).resolve()

    previous_chapter = chapter - 1

    if previous_chapter < 1:
        raise BenchmarkSafetyError(
            "historical continuation unexpectedly requires Chapter 1 predecessor"
        )

    if _cache is None:
        _cache = {}

    # Once a novel has entered the continuation lineage, its immediate
    # predecessor is another continuation completion.
    previous_continuation = continuation._case_dir(
        root,
        arm,
        novel_id,
        previous_chapter,
    )

    if previous_continuation.exists():
        previous_complete_path = (
            previous_continuation
            / "complete.json"
        )

        if not previous_complete_path.is_file():
            raise BenchmarkSafetyError(
                "historical continuation predecessor exists without complete artifact"
            )

        previous_complete = (
            _validate_historical_continuation_complete(
                root,
                plan,
                arm,
                novel_id,
                previous_chapter,
                _cache=_cache,
            )
        )

        state_path = (
            previous_continuation
            / "state_after.json"
        )

        state = strict._load_json(
            state_path,
            "historical prior continuation state",
        )

        identity = previous_complete[
            "artifacts"
        ][
            "state_after_identity_sha256"
        ]

        if (
            strict._canonical_json_sha256(
                state
            )
            != identity
        ):
            raise BenchmarkSafetyError(
                "historical prior continuation state identity drifted"
            )

        return (
            state,
            identity,
            str(state_path),
        )

    # The Chinese continuation starts immediately after recovered CN18.
    failure = foundation.FAILURES[arm]

    if (
        failure["novel_id"] == novel_id
        and failure["chapter_number"] == previous_chapter
    ):
        recovery_dir = (
            continuation._foundation_recovery_dir(
                root,
                arm,
            )
        )

        recovery = strict._load_json(
            recovery_dir / "recovery.json",
            "historical continuation recovery",
        )

        if (
            recovery.get("status")
            != "recovered_historical_failure"
        ):
            raise BenchmarkSafetyError(
                "historical continuation recovery status drifted"
            )

        state_path = (
            recovery_dir
            / "state_after.json"
        )

        state = strict._load_json(
            state_path,
            "historical recovered state",
        )

        identity = recovery[
            "artifacts"
        ][
            "state_after_identity_sha256"
        ]

        if (
            strict._canonical_json_sha256(
                state
            )
            != identity
        ):
            raise BenchmarkSafetyError(
                "historical recovered state identity drifted"
            )

        return (
            state,
            identity,
            str(state_path),
        )

    # KR18 begins from strict KR17. Strict terminal evidence has already
    # been validated globally above; validate this predecessor artifact
    # again without recursive state walking.
    strict_root = Path(
        plan["strict_source"]["root"]
    )

    canonical_root = Path(
        plan["canonical"]["root"]
    )

    canonical_plan = strict._load_json(
        canonical_root
        / "chronological_state_plan.json",
        "canonical plan for historical continuation",
    )

    strict_complete = (
        strict._validate_sync_complete(
            strict_root,
            canonical_root,
            canonical_plan,
            arm,
            novel_id,
            previous_chapter,
            recurse_state=False,
        )
    )

    state_path = (
        strict._case_dir(
            strict_root,
            arm,
            novel_id,
            previous_chapter,
        )
        / "state_after.json"
    )

    state = strict._load_json(
        state_path,
        "historical strict predecessor state",
    )

    identity = strict_complete[
        "artifacts"
    ][
        "state_after_identity_sha256"
    ]

    if (
        strict._canonical_json_sha256(
            state
        )
        != identity
    ):
        raise BenchmarkSafetyError(
            "historical strict predecessor state identity drifted"
        )

    return (
        state,
        identity,
        str(state_path),
    )


def _validate_historical_continuation_complete(
    root: Path,
    plan: dict,
    arm: str,
    novel_id: str,
    chapter: int,
    *,
    _cache=None,
):
    """Fully validate one frozen Phase59E completion without live HEAD checks."""

    root = Path(root).resolve()

    if _cache is None:
        _cache = {}

    cache_key = (
        arm,
        novel_id,
        int(chapter),
    )

    if cache_key in _cache:
        return _cache[cache_key]

    case_dir = continuation._case_dir(
        root,
        arm,
        novel_id,
        chapter,
    )

    paths = continuation._paths(
        case_dir
    )

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
            raise BenchmarkSafetyError(
                f"historical continuation complete artifact missing: {paths[key]}"
            )

    manifest_path = (
        case_dir
        / "manifest.json"
    )

    if not manifest_path.is_file():
        raise BenchmarkSafetyError(
            f"historical continuation manifest missing: {manifest_path}"
        )

    if (
        paths["uncertain"].exists()
        or paths["response_failed"].exists()
    ):
        raise BenchmarkSafetyError(
            "historical continuation completion conflicts with failure marker"
        )

    complete = strict._load_json(
        paths["complete"],
        "historical continuation complete",
    )

    manifest = strict._load_json(
        manifest_path,
        "historical continuation manifest",
    )

    if (
        complete.get("status") != "complete"
        or complete.get("kind") != continuation.KIND
    ):
        raise BenchmarkSafetyError(
            "historical continuation completion status/kind mismatch"
        )

    if (
        complete.get("identity")
        != manifest.get("identity")
    ):
        raise BenchmarkSafetyError(
            "historical continuation manifest/complete identity mismatch"
        )

    identity = complete.get(
        "identity"
    ) or {}

    if (
        strict._canonical_json_sha256(
            identity
        )
        != complete.get(
            "identity_sha256"
        )
    ):
        raise BenchmarkSafetyError(
            "historical continuation transaction identity drifted"
        )

    if (
        identity.get("run_id")
        != plan["run_id"]
        or identity.get("arm")
        != arm
    ):
        raise BenchmarkSafetyError(
            "historical continuation run/arm identity drifted"
        )

    if (
        identity.get("novel_id")
        != novel_id
        or identity.get("chapter_number")
        != chapter
    ):
        raise BenchmarkSafetyError(
            "historical continuation case identity drifted"
        )

    expected_model = plan[
        "identity"
    ][
        "model_configs"
    ][
        arm
    ]

    for key, expected_value in expected_model.items():
        if (
            identity.get(key)
            != expected_value
        ):
            raise BenchmarkSafetyError(
                f"historical continuation model identity drifted: {key}"
            )

    (
        state_before,
        state_before_sha,
        state_before_path,
    ) = _historical_state_before_for_case(
        root,
        plan,
        arm,
        novel_id,
        chapter,
        _cache=_cache,
    )

    if (
        identity.get(
            "state_before_identity_sha256"
        )
        != state_before_sha
    ):
        raise BenchmarkSafetyError(
            "historical continuation state-before identity drifted"
        )

    recorded_state_path = complete.get(
        "source_state_before_path"
    )

    if not isinstance(
        recorded_state_path,
        str,
    ):
        raise BenchmarkSafetyError(
            "historical continuation source-state path missing"
        )

    if (
        Path(recorded_state_path).resolve()
        != Path(state_before_path).resolve()
    ):
        raise BenchmarkSafetyError(
            "historical continuation source-state path drifted"
        )

    canonical_root = Path(
        plan["canonical"]["root"]
    )

    canonical_plan = strict._load_json(
        canonical_root
        / "chronological_state_plan.json",
        "canonical plan for historical completion",
    )

    (
        expected_request,
        record,
        config,
        _,
    ) = strict._build_request(
        canonical_root,
        canonical_plan,
        arm,
        novel_id,
        chapter,
        state_before,
    )

    request = strict._load_json(
        paths["request"],
        "historical continuation request",
    )

    if request != expected_request:
        raise BenchmarkSafetyError(
            "historical continuation request no longer matches deterministic request"
        )

    request_sha = (
        strict._canonical_json_sha256(
            request
        )
    )

    if (
        request_sha
        != identity.get(
            "request_sha256"
        )
    ):
        raise BenchmarkSafetyError(
            "historical continuation request identity drifted"
        )

    if (
        record["source_sha256"]
        != identity.get(
            "source_sha256"
        )
    ):
        raise BenchmarkSafetyError(
            "historical continuation source identity drifted"
        )

    response = strict._load_json(
        paths["response"],
        "historical continuation raw response",
    )

    provider = strict._provider_identity(
        response
    )

    if (
        provider["response_model"]
        != config.model
    ):
        raise BenchmarkSafetyError(
            "historical continuation provider model drifted"
        )

    if (
        provider["finish_reason"]
        != "stop"
    ):
        raise BenchmarkSafetyError(
            "historical continuation finish reason drifted"
        )

    complete_provider = (
        complete.get(
            "provider_response"
        )
        or {}
    )

    for key in (
        "response_id",
        "response_model",
        "system_fingerprint",
        "finish_reason",
    ):
        if (
            complete_provider.get(key)
            != provider.get(key)
        ):
            raise BenchmarkSafetyError(
                f"historical continuation provider evidence drifted: {key}"
            )

    document, transformations = (
        foundation.parse_salvage_document(
            strict._response_content(
                response
            ),
            state_before,
        )
    )

    if (
        transformations
        != complete.get(
            "salvage_transformations",
            [],
        )
    ):
        raise BenchmarkSafetyError(
            "historical continuation salvage transformation log drifted"
        )

    translation = paths[
        "translation"
    ].read_text(
        encoding="utf-8"
    )

    if (
        translation
        != document["translation"]
    ):
        raise BenchmarkSafetyError(
            "historical continuation translation drifted"
        )

    state_update = strict._load_json(
        paths["state_update"],
        "historical continuation state update",
    )

    if (
        state_update
        != document["state_update"]
    ):
        raise BenchmarkSafetyError(
            "historical continuation state update drifted"
        )

    expected_after, metrics = (
        strict.apply_state_update(
            state_before,
            document,
            chapter,
        )
    )

    state_after = strict._load_json(
        paths["state_after"],
        "historical continuation state after",
    )

    if (
        state_after
        != expected_after
    ):
        raise BenchmarkSafetyError(
            "historical continuation deterministic state replay drifted"
        )

    if (
        metrics
        != complete.get(
            "state_metrics"
        )
    ):
        raise BenchmarkSafetyError(
            "historical continuation state metrics drifted"
        )

    artifacts = (
        complete.get(
            "artifacts"
        )
        or {}
    )

    checks = {
        "request_sha256":
            strict._sha256_file(
                paths["request"]
            ),
        "response_sha256":
            strict._sha256_file(
                paths["response"]
            ),
        "translation_sha256":
            strict._sha256_file(
                paths["translation"]
            ),
        "state_update_file_sha256":
            strict._sha256_file(
                paths["state_update"]
            ),
        "state_after_file_sha256":
            strict._sha256_file(
                paths["state_after"]
            ),
    }

    for key, actual in checks.items():
        if (
            artifacts.get(key)
            != actual
        ):
            raise BenchmarkSafetyError(
                f"historical continuation artifact hash drifted: {key}"
            )

    if (
        artifacts.get(
            "state_update_identity_sha256"
        )
        != metrics[
            "state_update_identity_sha256"
        ]
    ):
        raise BenchmarkSafetyError(
            "historical continuation state-update identity drifted"
        )

    if (
        artifacts.get(
            "state_after_identity_sha256"
        )
        != metrics[
            "state_after_identity_sha256"
        ]
    ):
        raise BenchmarkSafetyError(
            "historical continuation state-after identity drifted"
        )

    safety = (
        complete.get(
            "safety"
        )
        or {}
    )

    if (
        safety.get(
            "provider_calls_attempted"
        )
        != 1
        or safety.get(
            "paid_requests_attempted"
        )
        != 1
        or safety.get(
            "provider_response_recorded"
        )
        is not True
        or safety.get(
            "protected_state_verified_unchanged"
        )
        is not True
        or safety.get(
            "automatic_retry_forbidden"
        )
        is not True
    ):
        raise BenchmarkSafetyError(
            "historical continuation paid-call safety evidence drifted"
        )

    if (
        strict._journal_events(
            paths["journal"]
        )
        != [
            "planned",
            "started",
            "response_recorded",
            "complete",
        ]
    ):
        raise BenchmarkSafetyError(
            "historical continuation journal sequence drifted"
        )

    _cache[cache_key] = complete

    return complete


def _validate_historical_luna_terminal(
    root: Path,
    plan: dict,
):
    """Prove the frozen Luna continuation is exactly 15 valid completed cases."""

    root = Path(root).resolve()

    arm = strict.LUNA_ARM

    expected_cases = list(
        continuation._remaining_cases(
            arm
        )
    )

    if len(expected_cases) != 15:
        raise BenchmarkSafetyError(
            "historical Luna continuation case count drifted"
        )

    expected_ids = {
        strict._case_id(
            novel_id,
            chapter,
        )
        for (
            novel_id,
            chapter,
        ) in expected_cases
    }

    cases_root = (
        root
        / continuation.CONTINUATION_DIRNAME
        / "arms"
        / arm
        / "cases"
    )

    if not cases_root.is_dir():
        raise BenchmarkSafetyError(
            "historical Luna continuation cases root is missing"
        )

    actual_ids = {
        item.name
        for item in cases_root.iterdir()
        if item.is_dir()
    }

    if actual_ids != expected_ids:
        raise BenchmarkSafetyError(
            "historical Luna continuation case set drifted"
        )

    cache = {}

    measured_cost = 0.0

    transformations = {}

    for (
        novel_id,
        chapter,
    ) in expected_cases:

        complete = (
            _validate_historical_continuation_complete(
                root,
                plan,
                arm,
                novel_id,
                chapter,
                _cache=cache,
            )
        )

        measured_cost += float(
            (
                complete.get(
                    "cost"
                )
                or {}
            ).get(
                "total_cost_usd",
                0.0,
            )
        )

        for item in complete.get(
            "salvage_transformations",
            [],
        ):
            kind = item.get(
                "kind",
                "unknown",
            )

            transformations[
                kind
            ] = (
                transformations.get(
                    kind,
                    0,
                )
                + 1
            )

    return {
        "arm": arm,
        "expected_continuation_cases": 15,
        "counts": {
            "planned": 0,
            "complete": 15,
            "uncertain": 0,
            "response_failed": 0,
            "invalid_complete": 0,
            "incomplete_started": 0,
        },
        "completed_total": 15,
        "paid_attempts": 15,
        "measured_cost_usd": measured_cost,
        "salvage_transformations": transformations,
        "blocked": False,
        "next_case": None,
        "complete": True,
    }


def _validate_sources(
    continuation_root: Path,
    expected_tree_sha256: str | None = None,
):
    continuation_root = Path(continuation_root).resolve()

    if expected_tree_sha256 is not None:
        actual = strict._external_tree_sha(continuation_root)

        if actual != str(expected_tree_sha256).lower():
            raise BenchmarkSafetyError(
                f"continuation source tree drifted: {actual} != {expected_tree_sha256}"
            )

    plan = continuation._load_plan(
        continuation_root
    )

    (
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
        foundation_status,
    ) = _validate_historical_static_sources(
        continuation_root,
        plan,
    )

    luna = _validate_historical_luna_terminal(
        continuation_root,
        plan,
    )

    if (
        not luna["complete"]
        or luna["counts"]["complete"] != 15
    ):
        raise BenchmarkSafetyError(
            "salvaged Luna continuation must be complete 15/15"
        )

    if (
        luna["blocked"]
        or luna["paid_attempts"] != 15
    ):
        raise BenchmarkSafetyError(
            "salvaged Luna continuation terminal status is unexpected"
        )

    return (
        plan,
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
        foundation_status,
    )


def _effective_request_source(
    continuation_root,
    plan,
    strict_root,
    strict_plan,
    canonical_root,
    canonical_plan,
    novel_id,
    chapter,
):
    case_id = strict._case_id(novel_id, chapter)
    strict_case = strict._case_dir(strict_root, strict.LUNA_ARM, novel_id, chapter)
    strict_complete_path = strict_case / "complete.json"
    if strict_complete_path.is_file():
        complete = strict._validate_sync_complete(
            strict_root,
            canonical_root,
            canonical_plan,
            strict.LUNA_ARM,
            novel_id,
            chapter,
        )
        _, state_before_sha, state_before_path = strict._state_before_for_case(
            strict_root,
            canonical_root,
            canonical_plan,
            strict.LUNA_ARM,
            novel_id,
            chapter,
        )
        return {
            "lineage_kind": "strict",
            "case_id": case_id,
            "request_path": strict_case / "request.json",
            "state_before_identity_sha256": state_before_sha,
            "state_before_path": state_before_path,
            "sync_translation_sha256": complete["artifacts"]["translation_sha256"],
        }

    failure = foundation.FAILURES[strict.LUNA_ARM]
    if case_id == failure["case_id"]:
        recovery_dir = continuation._foundation_recovery_dir(
            continuation_root, strict.LUNA_ARM
        )
        recovery = strict._load_json(recovery_dir / "recovery.json", "Luna recovery")
        if recovery.get("status") != "recovered_historical_failure":
            raise BenchmarkSafetyError("Luna deterministic recovery status drifted")
        return {
            "lineage_kind": "deterministic_recovery",
            "case_id": case_id,
            "request_path": recovery_dir / "strict_request.json",
            "state_before_identity_sha256": recovery["identity"][
                "state_before_identity_sha256"
            ],
            "state_before_path": recovery["source_state_before_path"],
            "sync_translation_sha256": recovery["artifacts"]["translation_sha256"],
        }

    complete = _validate_historical_continuation_complete(
        continuation_root,
        plan,
        strict.LUNA_ARM,
        novel_id,
        chapter,
    )
    case_dir = continuation._case_dir(
        continuation_root, strict.LUNA_ARM, novel_id, chapter
    )
    return {
        "lineage_kind": "salvage_continuation",
        "case_id": case_id,
        "request_path": case_dir / "request.json",
        "state_before_identity_sha256": complete["identity"][
            "state_before_identity_sha256"
        ],
        "state_before_path": complete["source_state_before_path"],
        "sync_translation_sha256": complete["artifacts"]["translation_sha256"],
    }


def _build_effective_requests(continuation_root, plan, strict_root, strict_plan, canonical_root, canonical_plan):
    rows = []
    counts = {"strict": 0, "deterministic_recovery": 0, "salvage_continuation": 0}
    seen = set()
    expected_config = strict.EXPECTED_MODEL_CONFIG[strict.LUNA_ARM]
    for ordinal, (novel_id, chapter) in enumerate(strict._case_order(), 1):
        source = _effective_request_source(
            continuation_root,
            plan,
            strict_root,
            strict_plan,
            canonical_root,
            canonical_plan,
            novel_id,
            chapter,
        )
        request_path = Path(source["request_path"])
        request = strict._load_json(request_path, "effective Luna request")
        if request.get("model") != expected_config["model"]:
            raise BenchmarkSafetyError("effective Luna request model drifted")
        if request.get("reasoning_effort") != expected_config["reasoning"]:
            raise BenchmarkSafetyError("effective Luna reasoning setting drifted")
        if int(request.get("max_completion_tokens", 0)) != expected_config["max_tokens"]:
            raise BenchmarkSafetyError("effective Luna max token setting drifted")
        request_sha = strict._canonical_json_sha256(request)
        custom_id = f"{ordinal:02d}-{source['case_id']}-{request_sha[:16]}"
        if custom_id in seen:
            raise BenchmarkSafetyError("duplicate salvaged Luna Batch custom_id")
        seen.add(custom_id)
        counts[source["lineage_kind"]] += 1
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": strict.BATCH_ENDPOINT,
            "body": request,
        }
        rows.append(
            {
                "ordinal": ordinal,
                "custom_id": custom_id,
                "case_id": source["case_id"],
                "novel_id": novel_id,
                "chapter_number": chapter,
                "lineage_kind": source["lineage_kind"],
                "request_path": str(request_path),
                "request_sha256": request_sha,
                "batch_line_sha256": strict._canonical_json_sha256(line),
                "state_before_identity_sha256": source["state_before_identity_sha256"],
                "state_before_path": str(source["state_before_path"]),
                "sync_translation_sha256": source["sync_translation_sha256"],
                "line": line,
            }
        )
    if len(rows) != REQUEST_COUNT:
        raise BenchmarkSafetyError("effective Luna Batch lineage did not resolve to 50 requests")
    if counts != {"strict": 34, "deterministic_recovery": 1, "salvage_continuation": 15}:
        raise BenchmarkSafetyError(f"effective Luna lineage composition drifted: {counts!r}")
    return rows, counts


def prepare(
    *,
    repo_root: Path,
    expected_repo_head: str,
    continuation_root: Path,
    expected_continuation_tree_sha256: str,
    batch_root: Path,
    batch_budget_usd: float = DEFAULT_BUDGET_USD,
    now=strict.utc_now,
):
    repo_root = Path(repo_root).resolve()
    head = strict._ensure_repo_head(repo_root, expected_repo_head)
    (
        continuation_plan,
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
        foundation_status,
    ) = _validate_sources(continuation_root, expected_continuation_tree_sha256)
    if float(batch_budget_usd) <= 0 or float(batch_budget_usd) > 10:
        raise SalvagedBatchError("Batch budget must be >0 and <=10 USD")
    batch_root = _ensure_external_root(
        repo_root, canonical_root, strict_root, continuation_root, batch_root
    )
    protected = strict._ensure_protected_state(
        repo_root,
        continuation_plan["protected_state_before"],
        "before salvaged Luna Batch preparation",
    )
    rows, lineage_counts = _build_effective_requests(
        Path(continuation_root).resolve(),
        continuation_plan,
        strict_root,
        strict_plan,
        canonical_root,
        canonical_plan,
    )
    input_text = "\n".join(
        json.dumps(row["line"], ensure_ascii=False, separators=(",", ":"))
        for row in rows
    ) + "\n"
    if len(input_text.encode("utf-8")) > 200 * 1024 * 1024:
        raise SalvagedBatchError("Batch replay JSONL exceeds 200 MB API limit")
    identity_rows = [
        {key: value for key, value in row.items() if key != "line"}
        for row in rows
    ]
    identity = {
        "kind": KIND,
        "repo_head": head,
        "continuation_run_id": continuation_plan["run_id"],
        "continuation_tree_sha256": strict._external_tree_sha(Path(continuation_root)),
        "strict_run_id": strict_plan["run_id"],
        "strict_tree_sha256": continuation_plan["strict_source"]["tree_sha256"],
        "foundation_run_id": foundation_status["run_id"],
        "foundation_tree_sha256": continuation_plan["foundation"]["tree_sha256"],
        "canonical_identity_sha256": continuation_plan["canonical"]["identity_sha256"],
        "canonical_tree_sha256": continuation_plan["canonical"]["tree_sha256"],
        "model": strict.EXPECTED_MODEL_CONFIG[strict.LUNA_ARM],
        "endpoint": strict.BATCH_ENDPOINT,
        "completion_window": strict.BATCH_COMPLETION_WINDOW,
        "request_count": REQUEST_COUNT,
        "lineage_counts": lineage_counts,
        "requests": identity_rows,
        "batch_state_policy": "exact_request_replay_no_state_mutation",
        "result_normalization_policy": "phase59d_deterministic_salvage_only",
    }
    run_id = strict._canonical_json_sha256(identity)
    batch_root.mkdir(parents=True, exist_ok=False)
    strict._write_text_exclusive(_input_path(batch_root), input_text)
    identity["input_sha256"] = strict._sha256_file(_input_path(batch_root))
    # Rebind run identity after the input hash exists.
    run_id = strict._canonical_json_sha256(identity)
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "prepared",
        "run_id": run_id,
        "identity": identity,
        "repo_root": str(repo_root),
        "repo_head": head,
        "continuation_root": str(Path(continuation_root).resolve()),
        "batch_budget_usd": float(batch_budget_usd),
        "protected_state_before": protected,
        "input_file_id": None,
        "batch_id": None,
        "provider_batch": None,
        "authorization": {
            "one_batch_authorized_request_count": REQUEST_COUNT,
            "automatic_retry_forbidden": True,
            "ambiguous_upload_retry_forbidden": True,
            "ambiguous_submission_retry_forbidden": True,
        },
        "prepared_at_utc": now().isoformat(),
    }
    strict._write_json_exclusive(_plan_path(batch_root), document)
    strict._write_exclusive(_journal_path(batch_root), b"")
    strict._append_journal(
        _journal_path(batch_root),
        {
            "event": "prepared",
            "at_utc": now().isoformat(),
            "run_id": run_id,
            "input_sha256": identity["input_sha256"],
            "provider_calls": 0,
        },
    )
    strict._ensure_protected_state(repo_root, protected, "after salvaged Luna Batch preparation")
    return document


def _validate_live_sources(plan):
    continuation_root = Path(plan["continuation_root"])
    actual = strict._external_tree_sha(continuation_root)
    if actual != plan["identity"]["continuation_tree_sha256"]:
        raise BenchmarkSafetyError("salvaged Luna continuation source drifted")
    continuation_plan, *_ = _validate_sources(continuation_root, actual)
    if continuation_plan["run_id"] != plan["identity"]["continuation_run_id"]:
        raise BenchmarkSafetyError("continuation run identity drifted")
    repo_root = Path(plan["repo_root"])
    strict._ensure_repo_head(repo_root, plan["repo_head"])
    strict._ensure_protected_state(
        repo_root, plan["protected_state_before"], "while validating salvaged Batch"
    )


def _conservative_ceiling(plan):
    config, _ = strict._model_snapshot(strict.LUNA_ARM)
    size = _input_path(Path(plan["batch_root"]) if "batch_root" in plan else Path(".")).stat().st_size
    return (
        (size + 4096) / 1_000_000 * float(config.economy_prices["cache_miss"])
        + config.max_tokens * REQUEST_COUNT / 1_000_000 * float(config.economy_prices["output"])
    )


def submit(root: Path, *, authorization: str, client=None, now=strict.utc_now):
    root = Path(root).resolve()
    plan = _load_plan(root)
    if authorization != AUTHORIZATION:
        raise SalvagedBatchError("exact salvaged Luna Batch authorization token is required")
    if plan.get("status") not in {"prepared", "input_uploaded"}:
        raise BenchmarkSafetyError(f"Batch cannot submit from status {plan.get('status')!r}")
    _validate_live_sources(plan)
    config, _ = strict._model_snapshot(strict.LUNA_ARM)
    input_bytes = _input_path(root).stat().st_size + 4096
    ceiling = (
        input_bytes / 1_000_000 * float(config.economy_prices["cache_miss"])
        + config.max_tokens * REQUEST_COUNT / 1_000_000 * float(config.economy_prices["output"])
    )
    if ceiling > float(plan["batch_budget_usd"]):
        raise BenchmarkSafetyError(
            f"conservative Batch cost ceiling ${ceiling:.6f} exceeds budget ${plan['batch_budget_usd']:.6f}"
        )
    plan["conservative_cost_ceiling_usd"] = ceiling
    if client is None:
        client = strict._create_openai_batch_client()
    if plan["status"] == "prepared":
        plan["status"] = "upload_started"
        strict.atomic_write_json(_plan_path(root), plan)
        strict._append_journal(_journal_path(root), {"event": "upload_started", "at_utc": now().isoformat(), "attempt": 1})
        try:
            with _input_path(root).open("rb") as handle:
                uploaded = client.files.create(file=handle, purpose="batch")
        except BaseException as exc:
            plan["status"] = "upload_uncertain"
            plan["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
            strict.atomic_write_json(_plan_path(root), plan)
            strict._append_journal(_journal_path(root), {"event": "upload_uncertain", "at_utc": now().isoformat(), "retry_forbidden": True})
            raise strict.ChronologicalUncertainError("Batch input upload outcome is ambiguous; retry forbidden") from exc
        file_id = strict._field(uploaded, "id")
        if not isinstance(file_id, str) or not file_id:
            plan["status"] = "upload_uncertain"
            strict.atomic_write_json(_plan_path(root), plan)
            raise strict.ChronologicalUncertainError("Batch upload returned no file ID")
        plan["input_file_id"] = file_id
        plan["uploaded_file"] = strict._provider_dict(uploaded)
        plan["status"] = "input_uploaded"
        strict.atomic_write_json(_plan_path(root), plan)
        strict._append_journal(_journal_path(root), {"event": "input_uploaded", "at_utc": now().isoformat(), "input_file_id": file_id})
    metadata = {
        "benchmark": KIND,
        "run_id": plan["run_id"],
        "continuation_run_id": plan["identity"]["continuation_run_id"],
    }
    plan["status"] = "submission_started"
    plan["submission_metadata"] = metadata
    strict.atomic_write_json(_plan_path(root), plan)
    strict._append_journal(_journal_path(root), {"event": "submission_started", "at_utc": now().isoformat(), "attempt": 1})
    try:
        batch = client.batches.create(
            input_file_id=plan["input_file_id"],
            endpoint=strict.BATCH_ENDPOINT,
            completion_window=strict.BATCH_COMPLETION_WINDOW,
            metadata=metadata,
        )
    except BaseException as exc:
        plan["status"] = "submission_uncertain"
        plan["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
        strict.atomic_write_json(_plan_path(root), plan)
        strict._append_journal(_journal_path(root), {"event": "submission_uncertain", "at_utc": now().isoformat(), "retry_forbidden": True})
        raise strict.ChronologicalUncertainError("Batch creation outcome is ambiguous; reissue forbidden") from exc
    batch_id = strict._field(batch, "id")
    if not isinstance(batch_id, str) or not batch_id:
        plan["status"] = "submission_uncertain"
        strict.atomic_write_json(_plan_path(root), plan)
        raise strict.ChronologicalUncertainError("Batch creation returned no batch ID")
    snapshot = strict._batch_snapshot(batch)
    if snapshot["endpoint"] not in {None, strict.BATCH_ENDPOINT}:
        raise BenchmarkSafetyError("provider Batch endpoint mismatch")
    plan["batch_id"] = batch_id
    plan["provider_batch"] = snapshot
    plan["status"] = "submitted"
    plan.pop("last_error", None)
    strict.atomic_write_json(_plan_path(root), plan)
    strict._append_journal(_journal_path(root), {"event": "submitted", "at_utc": now().isoformat(), "batch_id": batch_id, "input_file_id": plan["input_file_id"]})
    _validate_live_sources(plan)
    return plan


def refresh(root: Path, *, client=None, now=strict.utc_now):
    root = Path(root).resolve()
    plan = _load_plan(root)
    if not plan.get("batch_id"):
        raise SalvagedBatchError("Batch has no known batch_id")
    if plan.get("status") in {"upload_uncertain", "submission_uncertain"}:
        raise BenchmarkSafetyError("ambiguous Batch evidence must be resolved before refresh")
    _validate_live_sources(plan)
    if client is None:
        client = strict._create_openai_batch_client()
    batch = client.batches.retrieve(plan["batch_id"])
    snapshot = strict._batch_snapshot(batch)
    if snapshot["id"] != plan["batch_id"]:
        raise BenchmarkSafetyError("Batch status returned wrong identity")
    plan["provider_batch"] = snapshot
    provider_status = snapshot.get("status")
    plan["status"] = "provider_completed" if provider_status == "completed" else f"provider_{provider_status}"
    plan["status_checked_at_utc"] = now().isoformat()
    strict.atomic_write_json(_plan_path(root), plan)
    strict._append_journal(_journal_path(root), {"event": "status_checked", "at_utc": now().isoformat(), "provider_status": provider_status, "request_counts": snapshot["request_counts"]})
    _validate_live_sources(plan)
    return plan


def _state_before(mapping):
    path = Path(mapping["state_before_path"])
    state = strict._load_json(path, "Batch source state-before")
    if strict._canonical_json_sha256(state) != mapping["state_before_identity_sha256"]:
        raise BenchmarkSafetyError(f"Batch source state-before drifted for {mapping['case_id']}")
    return state


def collect(root: Path, *, client=None, now=strict.utc_now):
    root = Path(root).resolve()
    plan = _load_plan(root)
    if plan.get("status") != "provider_completed":
        raise SalvagedBatchError("Batch collection requires provider_completed status")
    _validate_live_sources(plan)
    provider_batch = plan.get("provider_batch") or {}
    output_file_id = provider_batch.get("output_file_id")
    if not isinstance(output_file_id, str) or not output_file_id:
        raise SalvagedBatchError("completed Batch has no output_file_id")
    if client is None:
        client = strict._create_openai_batch_client()
    payload = strict._file_content_bytes(client.files.content(output_file_id))
    output_path = root / OUTPUT_FILENAME
    strict._write_exclusive(output_path, payload)
    expected = {row["custom_id"]: row for row in plan["identity"]["requests"]}
    seen = set()
    results_dir = root / RESULTS_DIRNAME
    results_dir.mkdir(exist_ok=False)
    config, _ = strict._model_snapshot(strict.LUNA_ARM)
    canonical_plan = continuation._load_plan(Path(plan["continuation_root"]))
    canonical_root = Path(canonical_plan["canonical"]["root"])
    canonical_doc = strict._load_json(canonical_root / "chronological_state_plan.json", "canonical plan")
    total_cost = 0.0
    successful = 0
    failures = 0
    transformations = {}
    for line_number, raw in enumerate(payload.decode("utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        custom_id = row.get("custom_id")
        if custom_id not in expected or custom_id in seen:
            raise BenchmarkSafetyError(f"unexpected/duplicate Batch custom_id: {custom_id!r}")
        seen.add(custom_id)
        mapping = expected[custom_id]
        result_path = results_dir / f"{mapping['case_id']}.json"
        try:
            if row.get("error") is not None:
                raise SalvagedBatchError(f"provider row error: {row.get('error')!r}")
            wrapper = row.get("response")
            if not isinstance(wrapper, dict) or wrapper.get("status_code") != 200:
                raise SalvagedBatchError("Batch row was not HTTP 200")
            body = wrapper.get("body")
            if not isinstance(body, dict):
                raise SalvagedBatchError("Batch row has no response body")
            provider = strict._provider_identity(body)
            if provider["response_model"] != config.model or provider["finish_reason"] != "stop":
                raise BenchmarkSafetyError("Batch provider identity mismatch")
            document, applied = foundation.parse_salvage_document(
                strict._response_content(body), _state_before(mapping)
            )
            usage = strict._usage_snapshot(body)
            cost = strict._pricing_result(config, now(), usage, batch=True)
            total_cost += cost["total_cost_usd"]
            language = strict._novel_record(canonical_doc, mapping["novel_id"])["source_language"]
            residual = strict.residual_source_script_report(document["translation"], language)
            translation_path = results_dir / f"{mapping['case_id']}.translation.txt"
            strict._write_text_exclusive(translation_path, document["translation"])
            for item in applied:
                kind = item["kind"]
                transformations[kind] = transformations.get(kind, 0) + 1
            result = {
                "status": "complete",
                "custom_id": custom_id,
                "case_id": mapping["case_id"],
                "novel_id": mapping["novel_id"],
                "chapter_number": mapping["chapter_number"],
                "source_lineage_kind": mapping["lineage_kind"],
                "request_sha256": mapping["request_sha256"],
                "state_before_identity_sha256": mapping["state_before_identity_sha256"],
                "sync_translation_sha256": mapping["sync_translation_sha256"],
                "provider_response": {**provider, "usage": usage},
                "cost": cost,
                "translation_sha256": strict._sha256_file(translation_path),
                "normalized_state_update_identity_sha256": strict._canonical_json_sha256(document["state_update"]),
                "normalized_state_update": document["state_update"],
                "salvage_transformations": applied,
                "quality_signals": {
                    "remaining_cjk_character_count": residual["remaining_cjk_character_count"],
                    "residual_cjk_fragments": residual["residual_cjk_fragments"],
                    "translation_character_count": len(document["translation"]),
                    "translation_word_count": len(document["translation"].split()),
                },
                "batch_state_applied": False,
            }
            successful += 1
        except BaseException as exc:
            failures += 1
            result = {
                "status": "result_failed",
                "custom_id": custom_id,
                "case_id": mapping["case_id"],
                "novel_id": mapping["novel_id"],
                "chapter_number": mapping["chapter_number"],
                "source_lineage_kind": mapping["lineage_kind"],
                "request_sha256": mapping["request_sha256"],
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "raw_row": row,
                "retry_forbidden": True,
                "batch_state_applied": False,
            }
        strict._write_json_exclusive(result_path, result)
    if seen != set(expected):
        raise SalvagedBatchError(f"Batch output missing {len(set(expected) - seen)} requests")
    if total_cost > float(plan["batch_budget_usd"]):
        raise BenchmarkSafetyError("measured Batch cost exceeded frozen budget")
    final = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "complete" if failures == 0 else "complete_with_result_failures",
        "run_id": plan["run_id"],
        "batch_id": plan["batch_id"],
        "output_file_id": output_file_id,
        "output_sha256": strict._sha256_file(output_path),
        "request_count": REQUEST_COUNT,
        "successful_results": successful,
        "failed_results": failures,
        "total_cost_usd": total_cost,
        "salvage_transformations": transformations,
        "safety": {
            "provider_calls_attempted_for_collection": 1,
            "inference_retries": 0,
            "batch_state_applied": False,
            "source_lineages_mutated": False,
        },
        "completed_at_utc": now().isoformat(),
    }
    strict._write_json_exclusive(root / "complete.json", final)
    plan["status"] = final["status"]
    plan["final_complete_sha256"] = strict._sha256_file(root / "complete.json")
    strict.atomic_write_json(_plan_path(root), plan)
    strict._append_journal(_journal_path(root), {"event": "complete", "at_utc": now().isoformat(), "successful_results": successful, "failed_results": failures, "total_cost_usd": total_cost})
    _validate_live_sources(plan)
    return final


def status(root: Path):
    plan = _load_plan(Path(root).resolve())
    return {
        "run_id": plan["run_id"],
        "status": plan["status"],
        "request_count": plan["identity"]["request_count"],
        "lineage_counts": plan["identity"]["lineage_counts"],
        "batch_id": plan.get("batch_id"),
        "input_file_id": plan.get("input_file_id"),
        "provider_batch": plan.get("provider_batch"),
        "complete": (Path(root).resolve() / "complete.json").is_file(),
        "batch_state_policy": plan["identity"]["batch_state_policy"],
        "result_normalization_policy": plan["identity"]["result_normalization_policy"],
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Secondary Batch replay for the completed salvaged Luna lineage")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--expected-repo-head", required=True)
    p.add_argument("--continuation-root", required=True)
    p.add_argument("--expected-continuation-tree-sha256", required=True)
    p.add_argument("--batch-root", required=True)
    p.add_argument("--batch-budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    p = sub.add_parser("status")
    p.add_argument("--batch-root", required=True)
    p = sub.add_parser("submit")
    p.add_argument("--batch-root", required=True)
    p.add_argument("--authorization", required=True)
    p = sub.add_parser("refresh")
    p.add_argument("--batch-root", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--batch-root", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare(
            repo_root=Path(args.repo_root),
            expected_repo_head=args.expected_repo_head,
            continuation_root=Path(args.continuation_root),
            expected_continuation_tree_sha256=args.expected_continuation_tree_sha256,
            batch_root=Path(args.batch_root),
            batch_budget_usd=args.batch_budget_usd,
        )
    elif args.command == "status":
        result = status(Path(args.batch_root))
    elif args.command == "submit":
        result = submit(Path(args.batch_root), authorization=args.authorization)
    elif args.command == "refresh":
        result = refresh(Path(args.batch_root))
    elif args.command == "collect":
        result = collect(Path(args.batch_root))
    else:  # pragma: no cover
        raise SalvagedBatchError(f"unsupported command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
