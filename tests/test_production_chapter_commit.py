import copy
import json

import pytest

import noveltranslator_core as core
from noveltranslator_observability import startup_integrity_errors
from production_chapter_commit import (
    commit_reconciled_reviewed_chapter_tail,
    commit_reviewed_chapter,
)


def _stats(value):
    return {
        "prompt_tokens": value,
        "completion_tokens": value,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": value,
        "cost_usd": float(value),
        "rate_correct_cost_usd": float(value),
    }


def _fixture(tmp_path):
    paths = {
        "final_file": tmp_path / "output" / "chapters" / "0001.md",
        "qa_file": tmp_path / "output" / "qa" / "0001.json",
        "glossary_file": tmp_path / "data" / "glossary.json",
        "continuity_file": tmp_path / "data" / "continuity.json",
        "issues_file": tmp_path / "data" / "issues.json",
        "audit_history_file": tmp_path / "data" / "audit_history.json",
        "progress_file": tmp_path / "data" / "progress.json",
        "pending_file": tmp_path / "output" / "pending" / "0001.md",
        "pending_meta_file": tmp_path / "output" / "pending" / "0001.json",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    paths["pending_file"].write_text("translated", encoding="utf-8")
    paths["pending_meta_file"].write_text(
        json.dumps({"chapter": 1, "stage": "reviewed"}),
        encoding="utf-8",
    )
    paths["progress_file"].write_text(
        json.dumps(
            {
                "completed": [],
                "last_completed": 0,
                "next_chapter": 1,
            }
        ),
        encoding="utf-8",
    )

    progress = {
        "completed": [],
        "failed": [],
        "last_completed": 0,
        "next_chapter": 1,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cache_hit_tokens": 0,
        "total_cache_miss_tokens": 0,
        "estimated_cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }

    values = {
        "progress": progress,
        "glossary": {"entries": {"甲": {"english": "A"}}},
        "continuity": {"latest_chapter": 1, "summary": "summary"},
        "issues_data": {"issues": []},
        "audit_history": {"audits": []},
        "qa": {"commit_allowed": True},
        "translation_stats": _stats(1),
        "state_stats": _stats(2),
        "review_stats": _stats(3),
    }
    return paths, values


def _combine(*items):
    result = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }
    for item in items:
        for key in result:
            result[key] += item.get(key, 0) or 0
    return result


def _account(progress, chapter_number, stats):
    if chapter_number in progress["completed"]:
        return False
    progress["completed"].append(chapter_number)
    progress["last_completed"] = chapter_number
    progress["next_chapter"] = chapter_number + 1
    progress["total_prompt_tokens"] += stats["prompt_tokens"]
    progress["total_completion_tokens"] += stats["completion_tokens"]
    progress["total_cache_hit_tokens"] += stats["cache_hit_tokens"]
    progress["total_cache_miss_tokens"] += stats["cache_miss_tokens"]
    progress["estimated_cost_usd"] += stats["cost_usd"]
    progress["rate_correct_cost_usd"] += stats[
        "rate_correct_cost_usd"
    ]
    return True


def _run(paths, values, *, fail_json_name=None, duplicate=False):
    events = []

    def save_json(path, value):
        events.append(f"json:{path.name}")
        if path.name == fail_json_name:
            raise OSError(f"synthetic failure at {path.name}")
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_text(path, value):
        events.append(f"text:{path.name}")
        path.write_text(value, encoding="utf-8")

    def combine(*items):
        events.append("combine")
        return _combine(*items)

    def account(progress, chapter_number, stats):
        events.append("account")
        if duplicate:
            return False
        return _account(progress, chapter_number, stats)

    result = commit_reviewed_chapter(
        chapter_number=1,
        translation="translated",
        qa=values["qa"],
        translation_stats=values["translation_stats"],
        state_stats=values["state_stats"],
        review_stats=values["review_stats"],
        progress=values["progress"],
        glossary=values["glossary"],
        continuity=values["continuity"],
        issues_data=values["issues_data"],
        audit_history=values["audit_history"],
        final_file=paths["final_file"],
        qa_file=paths["qa_file"],
        glossary_file=paths["glossary_file"],
        continuity_file=paths["continuity_file"],
        issues_file=paths["issues_file"],
        audit_history_file=paths["audit_history_file"],
        progress_file=paths["progress_file"],
        pending_file=paths["pending_file"],
        pending_meta_file=paths["pending_meta_file"],
        combine_stats=combine,
        atomic_write_text=write_text,
        save_json=save_json,
        commit_chapter_accounting=account,
    )
    return events, result


def test_commit_order_keeps_progress_as_final_durable_state_marker(tmp_path):
    paths, values = _fixture(tmp_path)

    progress_write_observation = {}

    events = []

    def save_json(path, value):
        events.append(f"json:{path.name}")
        if path == paths["progress_file"]:
            progress_write_observation["pending_md"] = (
                paths["pending_file"].exists()
            )
            progress_write_observation["pending_json"] = (
                paths["pending_meta_file"].exists()
            )
            progress_write_observation["associated_files"] = all(
                path_.is_file()
                for path_ in (
                    paths["glossary_file"],
                    paths["continuity_file"],
                    paths["issues_file"],
                    paths["audit_history_file"],
                )
            )
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_text(path, value):
        events.append(f"text:{path.name}")
        path.write_text(value, encoding="utf-8")

    def combine(*items):
        events.append("combine")
        return _combine(*items)

    def account(progress, chapter_number, stats):
        events.append("account")
        return _account(progress, chapter_number, stats)

    chapter_stats = commit_reviewed_chapter(
        chapter_number=1,
        translation="translated",
        qa=values["qa"],
        translation_stats=values["translation_stats"],
        state_stats=values["state_stats"],
        review_stats=values["review_stats"],
        progress=values["progress"],
        glossary=values["glossary"],
        continuity=values["continuity"],
        issues_data=values["issues_data"],
        audit_history=values["audit_history"],
        final_file=paths["final_file"],
        qa_file=paths["qa_file"],
        glossary_file=paths["glossary_file"],
        continuity_file=paths["continuity_file"],
        issues_file=paths["issues_file"],
        audit_history_file=paths["audit_history_file"],
        progress_file=paths["progress_file"],
        pending_file=paths["pending_file"],
        pending_meta_file=paths["pending_meta_file"],
        combine_stats=combine,
        atomic_write_text=write_text,
        save_json=save_json,
        commit_chapter_accounting=account,
    )

    assert events == [
        "json:0001.json",
        "combine",
        "text:0001.md",
        "account",
        "json:glossary.json",
        "json:continuity.json",
        "json:issues.json",
        "json:audit_history.json",
        "json:progress.json",
    ]
    assert progress_write_observation == {
        "pending_md": True,
        "pending_json": True,
        "associated_files": True,
    }
    assert paths["final_file"].read_text(encoding="utf-8") == "translated"
    assert not paths["pending_file"].exists()
    assert not paths["pending_meta_file"].exists()
    assert values["progress"]["completed"] == [1]
    assert values["progress"]["next_chapter"] == 2
    assert chapter_stats["prompt_tokens"] == 6
    assert chapter_stats["completion_tokens"] == 6
    assert chapter_stats["cost_usd"] == 6.0


@pytest.mark.parametrize(
    "fail_json_name",
    [
        "glossary.json",
        "continuity.json",
        "issues.json",
        "audit_history.json",
        "progress.json",
    ],
)
def test_failure_before_durable_progress_preserves_pending(
    tmp_path,
    fail_json_name,
):
    paths, values = _fixture(tmp_path)
    before_progress = paths["progress_file"].read_bytes()
    before_memory = copy.deepcopy(values["progress"])

    with pytest.raises(OSError, match="synthetic failure"):
        _run(
            paths,
            values,
            fail_json_name=fail_json_name,
        )

    assert paths["pending_file"].is_file()
    assert paths["pending_meta_file"].is_file()
    assert paths["final_file"].is_file()

    assert paths["progress_file"].read_bytes() == before_progress
    assert values["progress"] == before_memory
    # Retrying with the same live object must not skip or double-account it.
    _run(paths, values)
    assert values["progress"]["completed"] == [1]
    assert values["progress"]["total_prompt_tokens"] == 6
    assert not paths["pending_file"].exists()


def test_duplicate_accounting_fails_before_associated_state_or_progress(tmp_path):
    paths, values = _fixture(tmp_path)
    before_progress = paths["progress_file"].read_bytes()

    with pytest.raises(
        RuntimeError,
        match="already accounted before commit",
    ):
        _run(paths, values, duplicate=True)

    assert paths["qa_file"].is_file()
    assert paths["final_file"].is_file()
    assert not paths["glossary_file"].exists()
    assert not paths["continuity_file"].exists()
    assert not paths["issues_file"].exists()
    assert not paths["audit_history_file"].exists()
    assert paths["progress_file"].read_bytes() == before_progress
    assert paths["pending_file"].is_file()
    assert paths["pending_meta_file"].is_file()


def test_missing_pending_files_are_safe_after_durable_commit(tmp_path):
    paths, values = _fixture(tmp_path)
    paths["pending_file"].unlink()
    paths["pending_meta_file"].unlink()

    _events, chapter_stats = _run(paths, values)

    assert values["progress"]["completed"] == [1]
    assert chapter_stats["prompt_tokens"] == 6


def test_pending_metadata_cleanup_failure_leaves_retryable_json_only_prefix(
    tmp_path,
    monkeypatch,
):
    paths, values = _fixture(tmp_path)
    original = type(paths["pending_meta_file"]).unlink

    def unlink(path, *args, **kwargs):
        if path == paths["pending_meta_file"]:
            raise PermissionError("pending metadata cleanup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(
        type(paths["pending_meta_file"]),
        "unlink",
        unlink,
    )

    with pytest.raises(
        PermissionError,
        match="pending metadata cleanup",
    ):
        _run(paths, values)

    assert values["progress"]["completed"] == [1]
    assert json.loads(
        paths["progress_file"].read_text(encoding="utf-8")
    )["completed"] == [1]
    assert not paths["pending_file"].exists()
    assert paths["pending_meta_file"].is_file()
    assert paths["final_file"].is_file()




def _persistable_stats():
    rates = core.V4_PRO_PRICES["off_peak"]
    prompt_tokens = 1
    completion_tokens = 1
    cache_hits = 0
    cache_misses = 1
    cost = (
        cache_misses / 1_000_000
        * rates["cache_miss"]
        + completion_tokens / 1_000_000
        * rates["output"]
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cost_usd": cost,
        "rate_correct_cost_usd": cost,
        "pricing_version": core.PRICING_VERSION,
        "pricing_period": "off_peak",
        "request_started_utc": "2026-09-07T00:30:00+00:00",
        "cache_hit_rate_per_million": rates["cache_hit"],
        "cache_miss_rate_per_million": rates["cache_miss"],
        "output_rate_per_million": rates["output"],
    }


def test_reconciled_tail_skips_final_and_qa_rewrites(tmp_path):
    paths, values = _fixture(tmp_path)
    paths["final_file"].write_text("translated", encoding="utf-8")
    paths["qa_file"].write_text(
        json.dumps(values["qa"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    final_before = paths["final_file"].read_bytes()
    qa_before = paths["qa_file"].read_bytes()
    events = []

    def save_json(path, value):
        events.append(f"json:{path.name}")
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def combine(*items):
        events.append("combine")
        return _combine(*items)

    def account(progress, chapter_number, stats):
        events.append("account")
        return _account(progress, chapter_number, stats)

    stats = commit_reconciled_reviewed_chapter_tail(
        chapter_number=1,
        translation_stats=values["translation_stats"],
        state_stats=values["state_stats"],
        review_stats=values["review_stats"],
        progress=values["progress"],
        glossary=values["glossary"],
        continuity=values["continuity"],
        issues_data=values["issues_data"],
        audit_history=values["audit_history"],
        final_file=paths["final_file"],
        pending_file=paths["pending_file"],
        pending_meta_file=paths["pending_meta_file"],
        glossary_file=paths["glossary_file"],
        continuity_file=paths["continuity_file"],
        issues_file=paths["issues_file"],
        audit_history_file=paths["audit_history_file"],
        progress_file=paths["progress_file"],
        combine_stats=combine,
        save_json=save_json,
        commit_chapter_accounting=account,
    )

    assert events == [
        "combine",
        "account",
        "json:glossary.json",
        "json:continuity.json",
        "json:issues.json",
        "json:audit_history.json",
        "json:progress.json",
    ]
    assert paths["final_file"].read_bytes() == final_before
    assert paths["qa_file"].read_bytes() == qa_before
    assert values["progress"]["completed"] == [1]
    assert not paths["pending_file"].exists()
    assert not paths["pending_meta_file"].exists()
    assert stats["prompt_tokens"] == 6


@pytest.mark.parametrize(
    "fail_json_name",
    [
        "glossary.json",
        "continuity.json",
        "issues.json",
        "audit_history.json",
        "progress.json",
    ],
)
def test_reconciled_tail_is_retryable_after_each_pre_progress_failure(
    tmp_path,
    fail_json_name,
):
    paths, values = _fixture(tmp_path)
    paths["final_file"].write_text("translated", encoding="utf-8")
    paths["qa_file"].write_text(
        json.dumps(values["qa"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    final_before = paths["final_file"].read_bytes()
    qa_before = paths["qa_file"].read_bytes()
    progress_before = paths["progress_file"].read_bytes()
    fail_once = {"active": True}

    def save_json(path, value):
        if path.name == fail_json_name and fail_once["active"]:
            fail_once["active"] = False
            raise OSError(f"synthetic failure at {path.name}")
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    with pytest.raises(OSError, match="synthetic failure"):
        commit_reconciled_reviewed_chapter_tail(
            chapter_number=1,
            translation_stats=values["translation_stats"],
            state_stats=values["state_stats"],
            review_stats=values["review_stats"],
            progress=values["progress"],
            glossary=values["glossary"],
            continuity=values["continuity"],
            issues_data=values["issues_data"],
            audit_history=values["audit_history"],
            final_file=paths["final_file"],
            pending_file=paths["pending_file"],
            pending_meta_file=paths["pending_meta_file"],
            glossary_file=paths["glossary_file"],
            continuity_file=paths["continuity_file"],
            issues_file=paths["issues_file"],
            audit_history_file=paths["audit_history_file"],
            progress_file=paths["progress_file"],
            combine_stats=_combine,
            save_json=save_json,
            commit_chapter_accounting=_account,
        )

    assert paths["pending_file"].is_file()
    assert paths["pending_meta_file"].is_file()
    assert paths["final_file"].read_bytes() == final_before
    assert paths["qa_file"].read_bytes() == qa_before
    assert paths["progress_file"].read_bytes() == progress_before
    assert values["progress"]["completed"] == []

    stats = commit_reconciled_reviewed_chapter_tail(
        chapter_number=1,
        translation_stats=values["translation_stats"],
        state_stats=values["state_stats"],
        review_stats=values["review_stats"],
        progress=values["progress"],
        glossary=values["glossary"],
        continuity=values["continuity"],
        issues_data=values["issues_data"],
        audit_history=values["audit_history"],
        final_file=paths["final_file"],
        pending_file=paths["pending_file"],
        pending_meta_file=paths["pending_meta_file"],
        glossary_file=paths["glossary_file"],
        continuity_file=paths["continuity_file"],
        issues_file=paths["issues_file"],
        audit_history_file=paths["audit_history_file"],
        progress_file=paths["progress_file"],
        combine_stats=_combine,
        save_json=save_json,
        commit_chapter_accounting=_account,
    )

    assert stats["prompt_tokens"] == 6
    assert values["progress"]["completed"] == [1]
    assert values["progress"]["total_prompt_tokens"] == 6
    assert not paths["pending_file"].exists()
    assert not paths["pending_meta_file"].exists()
    assert paths["final_file"].read_bytes() == final_before
    assert paths["qa_file"].read_bytes() == qa_before


def test_reconciled_tail_rejects_final_pending_byte_drift_before_writes(tmp_path):
    paths, values = _fixture(tmp_path)
    paths["final_file"].write_text("different", encoding="utf-8")
    before_progress = paths["progress_file"].read_bytes()

    with pytest.raises(RuntimeError, match="no longer match"):
        commit_reconciled_reviewed_chapter_tail(
            chapter_number=1,
            translation_stats=values["translation_stats"],
            state_stats=values["state_stats"],
            review_stats=values["review_stats"],
            progress=values["progress"],
            glossary=values["glossary"],
            continuity=values["continuity"],
            issues_data=values["issues_data"],
            audit_history=values["audit_history"],
            final_file=paths["final_file"],
            pending_file=paths["pending_file"],
            pending_meta_file=paths["pending_meta_file"],
            glossary_file=paths["glossary_file"],
            continuity_file=paths["continuity_file"],
            issues_file=paths["issues_file"],
            audit_history_file=paths["audit_history_file"],
            progress_file=paths["progress_file"],
            combine_stats=_combine,
            save_json=lambda path, value: path.write_text(
                json.dumps(value), encoding="utf-8"
            ),
            commit_chapter_accounting=_account,
        )

    assert paths["progress_file"].read_bytes() == before_progress
    assert not paths["glossary_file"].exists()
    assert paths["pending_file"].is_file()


def test_partial_final_write_remains_recoverable_only_with_identical_pending(
    tmp_path,
):
    paths, values = _fixture(tmp_path)

    saved = _persistable_stats()
    zero_review = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }
    paths["pending_meta_file"].write_text(
        json.dumps(
            {
                "chapter": 1,
                "stage": "reviewed",
                "translation_stats": saved,
                "state": {},
                "state_stats": saved,
                "state_checkpoint_identity": {
                    "version": "fixture-state-v1",
                    "identity_sha256": "a" * 64,
                },
                "review_result": {},
                "review_stats": zero_review,
                "review_checkpoint_identity": {
                    "version": "fixture-review-v1",
                    "identity_sha256": "b" * 64,
                },
            }
        ),
        encoding="utf-8",
    )

    # Simulate the established partial-commit state: final text was durably
    # replaced, but progress.json still points to Chapter 1 and pending remains.
    paths["final_file"].write_text(
        paths["pending_file"].read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    disk_progress = json.loads(
        paths["progress_file"].read_text(encoding="utf-8")
    )

    assert startup_integrity_errors(
        disk_progress,
        paths["final_file"].parent,
        paths["pending_file"].parent,
    ) == []

    paths["final_file"].write_text(
        "different committed-looking bytes",
        encoding="utf-8",
    )

    errors = startup_integrity_errors(
        disk_progress,
        paths["final_file"].parent,
        paths["pending_file"].parent,
    )
    assert any(
        "could risk overwrite or duplicate billing" in error
        for error in errors
    )


def test_completed_chapter_rejected_before_overwriting_prose_or_qa(tmp_path):
    paths, values = _fixture(tmp_path)
    values["progress"]["completed"] = [1]
    paths["final_file"].write_text("valuable committed prose", encoding="utf-8")
    paths["qa_file"].write_text("valuable QA", encoding="utf-8")
    before = {key: path.read_bytes() for key, path in paths.items() if path.exists()}
    memory = copy.deepcopy(values)
    with pytest.raises(RuntimeError, match="already accounted"):
        _run(paths, values)
    assert {key: path.read_bytes() for key, path in paths.items() if path.exists()} == before
    assert values == memory


@pytest.mark.parametrize("error", [OSError("disk full"), KeyboardInterrupt(), SystemExit(9)])
def test_accounting_callback_partial_mutation_never_leaks_to_live_progress(tmp_path, error):
    paths, values = _fixture(tmp_path)
    before = copy.deepcopy(values["progress"])
    def account(progress, chapter, stats):
        progress["completed"].append(chapter)
        progress["total_prompt_tokens"] = 999
        raise error
    with pytest.raises(type(error)) as caught:
        commit_reviewed_chapter(
            chapter_number=1, translation="translated", **paths, **values,
            combine_stats=_combine, atomic_write_text=core.atomic_write_text,
            save_json=core.atomic_write_json, commit_chapter_accounting=account,
        )
    assert caught.value is error
    assert values["progress"] == before
    assert paths["pending_file"].exists()
    assert json.loads(paths["progress_file"].read_text())["completed"] == []


def test_pending_cleanup_failure_retains_successful_commit_marker(tmp_path, monkeypatch):
    paths, values = _fixture(tmp_path)
    original = type(paths["pending_file"]).unlink
    def unlink(path, *args, **kwargs):
        if path == paths["pending_file"]:
            raise PermissionError("pending cleanup")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(paths["pending_file"]), "unlink", unlink)
    with pytest.raises(PermissionError, match="pending cleanup"):
        _run(paths, values)
    assert values["progress"]["completed"] == [1]
    assert json.loads(paths["progress_file"].read_text())["completed"] == [1]
    assert paths["pending_file"].exists()
    assert core.pending_matches_committed(1, paths["pending_file"].parent,
                                          paths["final_file"].parent)
