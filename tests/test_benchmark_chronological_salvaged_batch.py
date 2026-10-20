import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_chronological_runtime as strict
import benchmark_chronological_salvaged_batch as batch
from benchmark_novel import BenchmarkSafetyError


def _request():
    return {
        "model": "gpt-5.6-luna",
        "messages": [
            {"role": "system", "content": "PROFILE + STATE PROTOCOL"},
            {"role": "user", "content": "STATE + SOURCE"},
        ],
        "max_completion_tokens": 12000,
        "reasoning_effort": "none",
    }


def _write_request(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(strict._canonical_json_bytes(_request()))


def test_effective_request_builder_uses_34_strict_one_recovery_15_continuation(
    monkeypatch, tmp_path
):
    strict_root = tmp_path / "strict"
    continuation_root = tmp_path / "continuation"
    strict_root.mkdir()
    continuation_root.mkdir()

    ordered = list(strict._case_order())
    assert len(ordered) == 50

    strict_cases = set(ordered[:34])
    recovery_case = ordered[34]
    continuation_cases = set(ordered[35:])
    assert recovery_case == ("great_king_zhuxia", 18)

    for novel_id, chapter in strict_cases:
        case_dir = strict._case_dir(
            strict_root, strict.LUNA_ARM, novel_id, chapter
        )
        case_dir.mkdir(parents=True)
        (case_dir / "complete.json").write_text("{}", encoding="utf-8")
        _write_request(case_dir / "request.json")

    recovery_dir = (
        continuation_root
        / "foundation"
        / "recoveries"
        / strict.LUNA_ARM
        / "great_king_zhuxia-ch0018"
    )
    recovery_dir.mkdir(parents=True)
    _write_request(recovery_dir / "strict_request.json")
    (recovery_dir / "recovery.json").write_text(
        json.dumps(
            {
                "status": "recovered_historical_failure",
                "identity": {"state_before_identity_sha256": "r" * 64},
                "source_state_before_path": str(tmp_path / "recovery-before.json"),
                "artifacts": {"translation_sha256": "t" * 64},
            }
        ),
        encoding="utf-8",
    )

    for novel_id, chapter in continuation_cases:
        case_dir = batch.continuation._case_dir(
            continuation_root, strict.LUNA_ARM, novel_id, chapter
        )
        case_dir.mkdir(parents=True)
        _write_request(case_dir / "request.json")

    monkeypatch.setattr(
        strict,
        "_validate_sync_complete",
        lambda root, canonical_root, canonical_plan, arm, novel, chapter: {
            "artifacts": {"translation_sha256": f"strict-{novel}-{chapter}"}
        },
    )
    monkeypatch.setattr(
        strict,
        "_state_before_for_case",
        lambda root, canonical_root, canonical_plan, arm, novel, chapter: (
            {},
            f"state-{novel}-{chapter}",
            str(tmp_path / f"before-{novel}-{chapter}.json"),
        ),
    )
    monkeypatch.setattr(
        batch.continuation,
        "_foundation_recovery_dir",
        lambda root, arm: recovery_dir,
    )
    monkeypatch.setattr(
        batch.continuation,
        "_validate_complete",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "Phase59H must not call Phase59E active _validate_complete"
            )
        ),
    )
    monkeypatch.setattr(
        batch,
        "_validate_historical_continuation_complete",
        lambda root, plan, arm, novel, chapter, **kwargs: {
            "identity": {"state_before_identity_sha256": f"cont-state-{novel}-{chapter}"},
            "source_state_before_path": str(tmp_path / f"cont-before-{novel}-{chapter}.json"),
            "artifacts": {"translation_sha256": f"cont-{novel}-{chapter}"},
        },
    )

    rows, counts = batch._build_effective_requests(
        continuation_root,
        {},
        strict_root,
        {},
        tmp_path / "canonical",
        {},
    )

    assert len(rows) == 50
    assert counts == {
        "strict": 34,
        "deterministic_recovery": 1,
        "salvage_continuation": 15,
    }
    assert rows[0]["lineage_kind"] == "strict"
    assert rows[34]["case_id"] == "great_king_zhuxia-ch0018"
    assert rows[34]["lineage_kind"] == "deterministic_recovery"
    assert rows[35]["lineage_kind"] == "salvage_continuation"
    assert rows[-1]["case_id"] == "rogue_first_prince-ch0025"
    assert len({row["custom_id"] for row in rows}) == 50
    assert all(row["line"]["body"] == _request() for row in rows)


def test_prepare_is_zero_provider_and_freezes_exact_50_lines(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    canonical = tmp_path / "canonical"
    strict_root = tmp_path / "strict"
    continuation_root = tmp_path / "continuation"
    output = tmp_path / "batch"
    for path in (repo, canonical, strict_root, continuation_root):
        path.mkdir()

    continuation_plan = {
        "run_id": "c" * 64,
        "protected_state_before": {"stable": True},
        "strict_source": {"tree_sha256": "s" * 64},
        "foundation": {"tree_sha256": "f" * 64},
        "canonical": {
            "identity_sha256": "i" * 64,
            "tree_sha256": "k" * 64,
        },
    }
    strict_plan = {"run_id": "r" * 64}
    foundation_status = {"run_id": "d" * 64}

    monkeypatch.setattr(strict, "_ensure_repo_head", lambda root, head: head)
    monkeypatch.setattr(
        batch,
        "_validate_sources",
        lambda root, tree: (
            continuation_plan,
            strict_root,
            strict_plan,
            canonical,
            {},
            foundation_status,
        ),
    )
    monkeypatch.setattr(
        strict,
        "_ensure_protected_state",
        lambda root, expected, stage: expected,
    )
    monkeypatch.setattr(strict, "_external_tree_sha", lambda root: "x" * 64)

    rows = []
    for ordinal, (novel, chapter) in enumerate(strict._case_order(), 1):
        req = _request()
        line = {
            "custom_id": f"{ordinal:02d}-case",
            "method": "POST",
            "url": strict.BATCH_ENDPOINT,
            "body": req,
        }
        rows.append(
            {
                "ordinal": ordinal,
                "custom_id": line["custom_id"],
                "case_id": strict._case_id(novel, chapter),
                "novel_id": novel,
                "chapter_number": chapter,
                "lineage_kind": "strict" if ordinal <= 34 else (
                    "deterministic_recovery" if ordinal == 35 else "salvage_continuation"
                ),
                "request_path": "fixture.json",
                "request_sha256": strict._canonical_json_sha256(req),
                "batch_line_sha256": strict._canonical_json_sha256(line),
                "state_before_identity_sha256": "b" * 64,
                "state_before_path": "before.json",
                "sync_translation_sha256": "t" * 64,
                "line": line,
            }
        )
    monkeypatch.setattr(
        batch,
        "_build_effective_requests",
        lambda *args: (
            rows,
            {"strict": 34, "deterministic_recovery": 1, "salvage_continuation": 15},
        ),
    )

    document = batch.prepare(
        repo_root=repo,
        expected_repo_head="1" * 40,
        continuation_root=continuation_root,
        expected_continuation_tree_sha256="x" * 64,
        batch_root=output,
        now=lambda: datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc),
    )

    assert document["status"] == "prepared"
    assert document["identity"]["request_count"] == 50
    assert document["identity"]["lineage_counts"] == {
        "strict": 34,
        "deterministic_recovery": 1,
        "salvage_continuation": 15,
    }
    assert document["identity"]["batch_state_policy"] == "exact_request_replay_no_state_mutation"
    assert document["identity"]["result_normalization_policy"] == "phase59d_deterministic_salvage_only"
    lines = (output / batch.INPUT_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    assert all(json.loads(line)["url"] == strict.BATCH_ENDPOINT for line in lines)
    assert strict._journal_events(output / batch.JOURNAL_FILENAME) == ["prepared"]


def test_wrong_submit_authorization_fails_before_client(monkeypatch, tmp_path):
    root = tmp_path / "batch"
    root.mkdir()
    input_path = root / batch.INPUT_FILENAME
    input_path.write_text("{}\n", encoding="utf-8")
    plan = {
        "schema_version": batch.SCHEMA_VERSION,
        "kind": batch.KIND,
        "status": "prepared",
        "run_id": "r" * 64,
        "identity": {
            "input_sha256": strict._sha256_file(input_path),
            "request_count": 50,
            "lineage_counts": {},
        },
    }
    (root / batch.PLAN_FILENAME).write_text(json.dumps(plan), encoding="utf-8")

    client = SimpleNamespace()
    with pytest.raises(batch.SalvagedBatchError, match="exact salvaged Luna Batch authorization"):
        batch.submit(root, authorization="WRONG", client=client)


def test_parser_exposes_batch_only_commands():
    parser = batch.build_parser()
    help_text = parser.format_help()
    for command in ("prepare", "status", "submit", "refresh", "collect"):
        assert command in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["execute-next"])


def test_external_root_refuses_source_ancestry(tmp_path):
    repo = tmp_path / "repo"
    canonical = tmp_path / "canonical"
    strict_root = tmp_path / "strict"
    continuation_root = tmp_path / "continuation"
    for path in (repo, canonical, strict_root, continuation_root):
        path.mkdir()

    with pytest.raises(BenchmarkSafetyError, match="outside all source/protected roots"):
        batch._ensure_external_root(
            repo,
            canonical,
            strict_root,
            continuation_root,
            continuation_root / "batch",
        )


def test_historical_source_validation_does_not_require_historical_repo_head(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    strict_root = tmp_path / "strict"
    continuation_root = tmp_path / "continuation"
    foundation_root = continuation_root / "foundation"
    canonical_root = tmp_path / "canonical"

    for item in (
        repo,
        strict_root,
        continuation_root,
        foundation_root,
        canonical_root,
    ):
        item.mkdir(parents=True, exist_ok=True)

    recoveries = {
        arm: {
            "identity_sha256": f"{arm}-recovery-id",
            "state_after_identity_sha256": f"{arm}-state-id",
        }
        for arm in strict.SYNC_ARMS
    }

    model_configs = {
        arm: {"arm": arm, "snapshot": "frozen"}
        for arm in strict.SYNC_ARMS
    }

    plan = {
        "repo_root": str(repo),
        # Deliberately historical and different from today's checkout.
        "repo_head": "a" * 40,
        "protected_state_before": {"stable": True},
        "strict_source": {
            "root": str(strict_root),
            "tree_sha256": "s" * 64,
            "run_id": "r" * 64,
        },
        "foundation": {
            "tree_sha256": "f" * 64,
            "run_id": "d" * 64,
            "recoveries": recoveries,
        },
        "canonical": {
            "root": str(canonical_root),
            "plan_sha256": "p" * 64,
            "identity_sha256": "i" * 64,
            "tree_sha256": "k" * 64,
        },
        "identity": {
            "model_configs": model_configs,
        },
    }

    # This is the regression guard. Phase59H must never ask the historical
    # continuation validator to force the live checkout back to plan["repo_head"].
    monkeypatch.setattr(
        strict,
        "_ensure_repo_head",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("historical repo HEAD must not be required")
        ),
    )

    protected_calls = []

    def fake_protected(root, expected, stage):
        protected_calls.append(
            (Path(root), expected, stage)
        )
        return expected

    monkeypatch.setattr(
        strict,
        "_ensure_protected_state",
        fake_protected,
    )

    def fake_tree(root):
        root = Path(root)

        if root == strict_root:
            return "s" * 64

        if root == foundation_root:
            return "f" * 64

        raise AssertionError(
            f"unexpected tree hash request: {root}"
        )

    monkeypatch.setattr(
        strict,
        "_external_tree_sha",
        fake_tree,
    )

    monkeypatch.setattr(
        strict,
        "_load_run_plan",
        lambda root: {
            "run_id": "r" * 64,
        },
    )

    monkeypatch.setattr(
        batch.foundation,
        "_strict_terminal_status",
        lambda root, loaded_plan: None,
    )

    monkeypatch.setattr(
        batch.foundation,
        "status",
        lambda root: {
            "run_id": "d" * 64,
            "recoveries": recoveries,
        },
    )

    monkeypatch.setattr(
        strict,
        "_validate_canonical",
        lambda root, **kwargs: (
            {"canonical": True},
            "p" * 64,
            "k" * 64,
        ),
    )

    monkeypatch.setattr(
        strict,
        "_model_snapshot",
        lambda arm: (
            None,
            model_configs[arm],
        ),
    )

    (
        actual_strict_root,
        actual_strict_plan,
        actual_canonical_root,
        actual_canonical_plan,
        actual_foundation_status,
    ) = batch._validate_historical_static_sources(
        continuation_root,
        plan,
    )

    assert actual_strict_root == strict_root
    assert actual_strict_plan["run_id"] == "r" * 64
    assert actual_canonical_root == canonical_root
    assert actual_canonical_plan == {"canonical": True}
    assert actual_foundation_status["run_id"] == "d" * 64

    assert len(protected_calls) == 1
    assert protected_calls[0][0] == repo
    assert protected_calls[0][1] == {"stable": True}


def test_validate_sources_uses_batch_historical_validator_not_phase59e_live_head_validator(
    monkeypatch, tmp_path
):
    continuation_root = tmp_path / "continuation"
    continuation_root.mkdir()

    plan = {
        "run_id": "c" * 64,
    }

    monkeypatch.setattr(
        strict,
        "_external_tree_sha",
        lambda root: "x" * 64,
    )

    monkeypatch.setattr(
        batch.continuation,
        "_load_plan",
        lambda root: plan,
    )

    monkeypatch.setattr(
        batch.continuation,
        "_validate_static_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "Phase59E live-HEAD validator must not be used by Phase59H"
            )
        ),
    )

    expected_tuple = (
        tmp_path / "strict",
        {"run_id": "s" * 64},
        tmp_path / "canonical",
        {"canonical": True},
        {"run_id": "f" * 64},
    )

    monkeypatch.setattr(
        batch,
        "_validate_historical_static_sources",
        lambda root, loaded_plan: expected_tuple,
    )

    monkeypatch.setattr(
        batch.continuation,
        "arm_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "Phase59H must not call Phase59E active arm_status"
            )
        ),
    )
    monkeypatch.setattr(
        batch,
        "_validate_historical_luna_terminal",
        lambda root, loaded_plan: {
            "complete": True,
            "counts": {
                "complete": 15,
            },
            "blocked": False,
            "paid_attempts": 15,
        },
    )

    result = batch._validate_sources(
        continuation_root,
        "x" * 64,
    )

    assert result[0] is plan
    assert result[1:] == expected_tuple


def test_phase59h_source_contains_no_phase59e_active_completion_dependencies():
    source = Path(batch.__file__).read_text(
        encoding="utf-8"
    )

    assert "continuation.arm_status(" not in source
    assert "continuation._validate_complete(" not in source
    assert "continuation._validate_static_sources(" not in source


def test_validate_sources_uses_historical_terminal_validator(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "continuation"
    root.mkdir()

    plan = {
        "run_id": "c" * 64,
    }

    monkeypatch.setattr(
        strict,
        "_external_tree_sha",
        lambda path: "x" * 64,
    )

    monkeypatch.setattr(
        batch.continuation,
        "_load_plan",
        lambda path: plan,
    )

    static = (
        tmp_path / "strict",
        {"run_id": "s" * 64},
        tmp_path / "canonical",
        {"canonical": True},
        {"run_id": "f" * 64},
    )

    monkeypatch.setattr(
        batch,
        "_validate_historical_static_sources",
        lambda path, loaded_plan: static,
    )

    monkeypatch.setattr(
        batch.continuation,
        "arm_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "live Phase59E arm_status must never be called"
            )
        ),
    )

    calls = []

    def fake_terminal(path, loaded_plan):
        calls.append(
            (
                Path(path),
                loaded_plan,
            )
        )

        return {
            "complete": True,
            "counts": {
                "complete": 15,
            },
            "blocked": False,
            "paid_attempts": 15,
        }

    monkeypatch.setattr(
        batch,
        "_validate_historical_luna_terminal",
        fake_terminal,
    )

    result = batch._validate_sources(
        root,
        "x" * 64,
    )

    assert result[0] is plan
    assert result[1:] == static

    assert calls == [
        (
            root.resolve(),
            plan,
        )
    ]
