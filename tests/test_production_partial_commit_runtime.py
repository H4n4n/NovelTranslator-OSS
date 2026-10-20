import copy
import json
from pathlib import Path

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from production_partial_commit_reconciliation import (
    PartialCommitReconciliationError,
)
from translation_profile import load_translation_profile


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
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


def _configure(monkeypatch, tmp_path, *, candidate=True):
    monkeypatch.chdir(tmp_path)

    for directory in (
        tmp_path / "data",
        tmp_path / "output" / "pending",
        tmp_path / "output" / "chapters",
        tmp_path / "output" / "qa",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    production.ACTIVE_PROFILE = load_translation_profile(
        "historical_military",
        REPO_ROOT,
    )
    production.ACTIVE_NOVEL_CONFIG = None
    production.MODEL = "test-model"
    production.STATE_PROMPT = "test state policy"
    production.TERM_REVIEW_MODEL_PROMPT = "test review policy"
    production.chapter_map = {
        1: {"chapter": 1, "text": "source"},
    }
    production.glossary = {"entries": {}}
    production.continuity = {"latest_chapter": 0, "summary": "before"}
    production.issues_data = {"issues": []}
    production.audit_history = {"audits": []}
    production.progress = {
        "completed": [],
        "last_completed": 0,
        "next_chapter": 1,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cache_hit_tokens": 0,
        "total_cache_miss_tokens": 0,
        "estimated_cost_usd": 0.0,
        "rate_correct_cost_usd": 0.0,
    }

    pending = tmp_path / "output" / "pending" / "0001.md"
    pending_meta = tmp_path / "output" / "pending" / "0001.json"
    final = tmp_path / "output" / "chapters" / "0001.md"
    qa_file = tmp_path / "output" / "qa" / "0001.json"

    pending.write_text("paid reviewed translation", encoding="utf-8")
    final.write_bytes(pending.read_bytes())

    local_qa = {"commit_allowed": True, "issues": ["qa issue"]}
    candidates = (
        [
            {
                "candidate_id": "source:a",
                "chinese": "甲",
                "english": "Alpha",
                "category": "character",
                "notes": "",
                "source_provenance": {},
            }
        ]
        if candidate
        else []
    )
    decisions = (
        [
            {
                "candidate_id": "source:a",
                "action": "accept",
                "english": "Alpha",
                "category": "character",
                "notes": "",
            }
        ]
        if candidate
        else []
    )
    translation = pending.read_text(encoding="utf-8")
    meta = {
        "chapter": 1,
        "stage": "reviewed",
        "translation_stats": _stats(1),
        "local_qa": local_qa,
        "state": {
            "new_terms": [],
            "continuity_summary": "after",
            "issues": ["model issue"],
        },
        "state_stats": _stats(2),
        "state_checkpoint_identity": production.current_state_checkpoint_identity(
            1,
            "source",
            translation,
            glossary_entries=production.glossary["entries"],
        ),
        "eligible_candidates": candidates,
        "prefilter_rejected": [],
        "review_result": {
            "decisions": decisions,
            "issues": ["review issue"],
        },
        "review_stats": _stats(3),
        "review_checkpoint_identity": production.current_review_checkpoint_identity(
            1,
            "source",
            translation,
            candidates,
            glossary_entries=production.glossary["entries"],
        ),
    }
    _write_json(pending_meta, meta)
    _write_json(qa_file, local_qa)

    monkeypatch.setattr(
        production,
        "reprice_saved_stats_if_needed",
        lambda value: copy.deepcopy(value),
    )

    monkeypatch.setattr(
        production,
        "log_event",
        lambda *_args, **_kwargs: None,
    )

    def combine(*values):
        keys = (
            "prompt_tokens",
            "completion_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "cost_usd",
            "rate_correct_cost_usd",
        )
        return {
            key: sum((value.get(key, 0) or 0) for value in values)
            for key in keys
        }

    def account(progress, chapter_number, stats):
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
        progress["rate_correct_cost_usd"] += stats["rate_correct_cost_usd"]
        return True

    monkeypatch.setattr(production, "combine_stats", combine)
    monkeypatch.setattr(production, "commit_chapter_accounting", account)

    return {
        "pending": pending,
        "pending_meta": pending_meta,
        "final": final,
        "qa_file": qa_file,
        "meta": meta,
    }


def test_process_chapter_routes_existing_final_to_local_reconciler_before_translation(
    monkeypatch,
    tmp_path,
):
    paths = _configure(monkeypatch, tmp_path)
    production.chapter_map = {1: {"chapter": 1, "text": "source"}}
    calls = []

    def reconcile(chapter_number):
        calls.append(chapter_number)
        return True

    monkeypatch.setattr(
        production,
        "reconcile_reviewed_partial_commit_tail",
        reconcile,
    )
    monkeypatch.setattr(
        production,
        "obtain_translation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("translation path must not run")
        ),
    )

    assert production.process_chapter(1) is True
    assert calls == [1]
    assert paths["pending"].is_file()


def test_reconciler_finishes_reviewed_commit_tail_without_rewriting_final_or_qa(
    monkeypatch,
    tmp_path,
):
    paths = _configure(monkeypatch, tmp_path)
    final_before = paths["final"].read_bytes()
    qa_before = paths["qa_file"].read_bytes()

    assert production.reconcile_reviewed_partial_commit_tail(1) is True

    assert paths["final"].read_bytes() == final_before
    assert paths["qa_file"].read_bytes() == qa_before
    assert not paths["pending"].exists()
    assert not paths["pending_meta"].exists()
    assert production.progress["completed"] == [1]
    assert production.progress["next_chapter"] == 2
    assert production.glossary["entries"]["甲"]["english"] == "Alpha"
    assert production.continuity == {
        "latest_chapter": 1,
        "summary": "after",
    }
    assert [
        item["type"]
        for item in production.issues_data["issues"]
        if item["chapter"] == 1
    ] == [
        "model_flag",
        "glossary_reviewer",
        "local_qa",
    ]


def test_reconciler_accepts_exact_already_applied_associated_state(
    monkeypatch,
    tmp_path,
):
    paths = _configure(monkeypatch, tmp_path)

    production.glossary["entries"]["甲"] = {
        "english": "Alpha",
        "category": "character",
        "status": "provisional",
        "first_chapter": 1,
        "notes": "",
        "source_provenance": {},
    }
    production.continuity = {
        "latest_chapter": 1,
        "summary": "after",
    }
    production.issues_data = {
        "issues": [
            {"chapter": 1, "type": "model_flag", "details": "model issue"},
            {
                "chapter": 1,
                "type": "glossary_reviewer",
                "details": "review issue",
            },
            {
                "chapter": 1,
                "type": "local_qa",
                "details": ["qa issue"],
            },
        ]
    }

    assert production.reconcile_reviewed_partial_commit_tail(1) is True
    assert production.progress["completed"] == [1]
    assert not paths["pending"].exists()


@pytest.mark.parametrize("prefix", ["glossary", "continuity", "issues"])
def test_reconciler_accepts_each_exact_persisted_commit_prefix(
    monkeypatch,
    tmp_path,
    prefix,
):
    paths = _configure(monkeypatch, tmp_path)

    production.glossary["entries"]["甲"] = {
        "english": "Alpha",
        "category": "character",
        "status": "provisional",
        "first_chapter": 1,
        "notes": "",
        "source_provenance": {},
    }
    if prefix in {"continuity", "issues"}:
        production.continuity = {
            "latest_chapter": 1,
            "summary": "after",
        }
    if prefix == "issues":
        production.issues_data = {
            "issues": [
                {"chapter": 1, "type": "model_flag", "details": "model issue"},
                {
                    "chapter": 1,
                    "type": "glossary_reviewer",
                    "details": "review issue",
                },
                {
                    "chapter": 1,
                    "type": "local_qa",
                    "details": ["qa issue"],
                },
            ]
        }

    assert production.reconcile_reviewed_partial_commit_tail(1) is True
    assert production.progress["completed"] == [1]
    assert not paths["pending"].exists()


def test_reconciler_blocks_stale_saved_checkpoint_identity_before_commit_tail(
    monkeypatch,
    tmp_path,
):
    paths = _configure(monkeypatch, tmp_path)
    meta = json.loads(paths["pending_meta"].read_text(encoding="utf-8"))
    meta["state_checkpoint_identity"]["source_sha256"] = "f" * 64
    _write_json(paths["pending_meta"], meta)
    progress_before = copy.deepcopy(production.progress)

    with pytest.raises(
        PartialCommitReconciliationError,
        match="saved checkpoint identity is stale or unbound",
    ):
        production.reconcile_reviewed_partial_commit_tail(1)

    assert production.progress == progress_before
    assert paths["pending"].is_file()
    assert paths["pending_meta"].is_file()


def test_reconciler_revalidates_saved_review_before_commit_tail(
    monkeypatch,
    tmp_path,
):
    paths = _configure(monkeypatch, tmp_path)
    meta = json.loads(paths["pending_meta"].read_text(encoding="utf-8"))
    meta["review_result"]["decisions"][0]["candidate_id"] = "source:tampered"
    _write_json(paths["pending_meta"], meta)
    progress_before = copy.deepcopy(production.progress)

    with pytest.raises(
        PartialCommitReconciliationError,
        match="saved reviewed glossary decisions no longer validate",
    ):
        production.reconcile_reviewed_partial_commit_tail(1)

    assert production.progress == progress_before
    assert paths["pending"].is_file()
    assert paths["pending_meta"].is_file()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda: production.glossary["entries"].__setitem__(
                "甲",
                {
                    "english": "Tampered",
                    "category": "character",
                    "status": "provisional",
                },
            ),
            "divergent partially committed entry",
        ),
        (
            lambda: production.continuity.update(
                {"latest_chapter": 1, "summary": "tampered"}
            ),
            "does not match the reviewed pending state",
        ),
        (
            lambda: production.issues_data.update(
                {
                    "issues": [
                        {
                            "chapter": 1,
                            "type": "model_flag",
                            "details": "tampered",
                        }
                    ]
                }
            ),
            "issues diverge",
        ),
    ],
)
def test_reconciler_blocks_divergent_associated_state_before_commit_tail(
    monkeypatch,
    tmp_path,
    mutate,
    message,
):
    paths = _configure(monkeypatch, tmp_path)
    progress_before = copy.deepcopy(production.progress)
    pending_before = paths["pending"].read_bytes()
    mutate()

    with pytest.raises(PartialCommitReconciliationError, match=message):
        production.reconcile_reviewed_partial_commit_tail(1)

    assert production.progress == progress_before
    assert paths["pending"].read_bytes() == pending_before
    assert paths["pending_meta"].is_file()


@pytest.mark.parametrize("change", ["summary", "extra_field", "original_snapshot", "version"])
def test_reconciler_requires_exact_original_continuity_not_only_chapter(
        monkeypatch, tmp_path, change):
    paths = _configure(monkeypatch, tmp_path, candidate=False)
    meta = copy.deepcopy(paths["meta"])
    if change == "summary":
        production.continuity["summary"] = "Divergent pre-commit summary."
    elif change == "extra_field":
        production.continuity["unexpected"] = "divergence"
    elif change == "original_snapshot":
        meta["state_checkpoint_identity"]["prior_continuity"]["summary"] = "Changed original."
        _write_json(paths["pending_meta"], meta)
    else:
        meta["state_checkpoint_identity"]["version"] = "saved-state-checkpoint-v1"
        _write_json(paths["pending_meta"], meta)
    before = copy.deepcopy(production.progress)
    with pytest.raises(PartialCommitReconciliationError):
        production.reconcile_reviewed_partial_commit_tail(1)
    assert production.progress == before
    assert paths["pending"].exists() and paths["pending_meta"].exists()


@pytest.mark.parametrize("prefix", ["continuity_without_glossary", "issues_without_continuity"])
def test_reconciler_rejects_impossible_associated_write_order(monkeypatch, tmp_path, prefix):
    _configure(monkeypatch, tmp_path)
    if prefix == "continuity_without_glossary":
        production.continuity.update(latest_chapter=1, summary="after")
    else:
        production.issues_data["issues"] = [
            {"chapter": 1, "type": "model_flag", "details": "model issue"},
            {"chapter": 1, "type": "glossary_reviewer", "details": "review issue"},
            {"chapter": 1, "type": "local_qa", "details": ["qa issue"]},
        ]
    with pytest.raises(PartialCommitReconciliationError, match="preceding"):
        production.reconcile_reviewed_partial_commit_tail(1)
    assert production.progress["completed"] == []


def test_crash_after_continuity_write_recovers_from_saved_original_baseline(monkeypatch, tmp_path):
    paths = _configure(monkeypatch, tmp_path, candidate=False)
    meta = paths["meta"]
    original = copy.deepcopy(production.continuity)
    production.finalize_reviewed_chapter_state(
        chapter_number=1, eligible_candidates=[], review_result=meta["review_result"],
        prefilter_rejected=[], state=meta["state"], qa=meta["local_qa"],
        continuity=production.continuity, issues_data=production.issues_data,
        apply_term_review=production.apply_term_review,
        replace_chapter_issues=production.replace_chapter_issues)
    save = production.save_json
    writes = []

    def crash_at_progress(path, value):
        writes.append(str(path))
        if Path(path) == production.PROGRESS_FILE:
            assert production.progress["completed"] == []
            raise OSError("crash before final marker")
        save(path, value)

    with pytest.raises(OSError, match="final marker"):
        production.commit_reviewed_chapter(
            chapter_number=1, translation=paths["pending"].read_text(encoding="utf-8"),
            qa=meta["local_qa"], translation_stats=meta["translation_stats"],
            state_stats=meta["state_stats"], review_stats=meta["review_stats"],
            progress=production.progress, glossary=production.glossary,
            continuity=production.continuity, issues_data=production.issues_data,
            audit_history=production.audit_history, final_file=paths["final"],
            qa_file=paths["qa_file"], glossary_file=production.GLOSSARY_FILE,
            continuity_file=production.CONTINUITY_FILE, issues_file=production.ISSUES_FILE,
            audit_history_file=production.AUDIT_HISTORY_FILE, progress_file=production.PROGRESS_FILE,
            pending_file=paths["pending"], pending_meta_file=paths["pending_meta"],
            combine_stats=production.combine_stats, atomic_write_text=production.atomic_write_text,
            save_json=crash_at_progress, commit_chapter_accounting=production.commit_chapter_accounting)
    assert writes[-1] == str(production.PROGRESS_FILE)
    production.continuity = json.loads(production.CONTINUITY_FILE.read_text(encoding="utf-8"))
    assert production.continuity == {"latest_chapter": 1, "summary": "after"}
    saved = json.loads(paths["pending_meta"].read_text(encoding="utf-8"))
    assert saved["state_checkpoint_identity"]["prior_continuity"] == original
    # Building a request from the advanced disk value would be a different
    # transition. Reconciliation must explicitly select the saved old baseline.
    assert production.current_state_checkpoint_identity(1, "source", "paid reviewed translation") != saved["state_checkpoint_identity"]
    monkeypatch.setattr(production, "extract_state", lambda *a, **k: pytest.fail("no provider work"))
    monkeypatch.setattr(production, "send_api_request_once", lambda **k: pytest.fail("no provider work"))
    assert production.reconcile_reviewed_partial_commit_tail(1) is True
    assert production.progress["completed"] == [1]
    assert not paths["pending_meta"].exists()


def test_failed_commit_qa_never_reaches_rolling_state_extraction(monkeypatch, tmp_path):
    from novel_project import NovelConfig
    paths = _configure(monkeypatch, tmp_path, candidate=False)
    monkeypatch.setattr(production, "ACTIVE_NOVEL_CONFIG", NovelConfig(
        id="fixture", title="Fixture", source_language="zh", target_language="en",
        profile="historical_military", final_chapter=1))
    paths["final"].unlink()  # Disposable fixture, no production chapter.
    meta = {"stage": "translated", "translation_stats": _stats(1)}
    pending_text = "English 心虚"
    paths["pending"].write_text(pending_text, encoding="utf-8")
    monkeypatch.setattr(production, "obtain_translation", lambda *a: {
        "source": "pending", "translation": pending_text, "meta": meta})
    monkeypatch.setattr(production, "zero_stats", lambda: _stats(0))
    monkeypatch.setattr(production, "extract_state", lambda *a, **k: pytest.fail("QA must block extraction"))
    monkeypatch.setattr(production, "send_api_request_once", lambda **k: pytest.fail("no provider calls"))
    before = copy.deepcopy(production.continuity)
    assert production.process_chapter(1) is False
    assert production.continuity == before
    assert production.progress["completed"] == []
    assert paths["pending"].read_text(encoding="utf-8") == pending_text
