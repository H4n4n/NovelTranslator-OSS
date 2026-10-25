import json

import pytest
from pathlib import Path

from novel_project import NovelConfig, NovelProject
from production_path_preflight import (
    ProductionPreflightError,
    run_production_path_preflight,
)
from production_saved_checkpoint_identity import (
    build_review_checkpoint_identity,
    build_state_checkpoint_identity,
)
from semantic_integrity_audit import protected_state_snapshot
from translation_model_config import (
    pending_translation_model_record,
    resolve_translation_model,
)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _project(
    tmp_path,
    *,
    glossary=None,
    completed=(1,),
    outputs=None,
):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture Novel",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=2,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(project.config_file, config.to_dict())
    _write_json(
        project.chapters_file,
        [
            {"chapter": 1, "text": "甲 entered."},
            {"chapter": 2, "text": "乙 followed."},
        ],
    )
    _write_json(project.glossary_file, {
        "entries": glossary or {
            "甲": {
                "english": "Jia",
                "category": "character",
                "status": "locked",
            }
        }
    })
    completed = list(completed)
    _write_json(project.progress_file, {
        "completed": completed,
        "last_completed": max(completed) if completed else 0,
        "next_chapter": (max(completed) + 1) if completed else 1,
    })
    _write_json(project.continuity_file, {"latest_chapter": 0, "summary": ""})
    _write_json(project.issues_file, {"issues": []})
    _write_json(project.audit_history_file, {"audits": []})
    project.translation_addendum_file.write_text("", encoding="utf-8")
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    project.pending_dir.mkdir(parents=True, exist_ok=True)
    if outputs is None:
        outputs = {1: "Jia entered."} if 1 in completed else {}
    for chapter, text in outputs.items():
        (project.chapter_dir / f"{chapter:04d}.md").write_text(text, encoding="utf-8")
    return project


def _install_completed_pending_residue(
    project,
    *,
    chapter=1,
    state="pair",
    model="pro",
):
    chapters = json.loads(
        project.chapters_file.read_text(encoding="utf-8")
    )
    source_text = next(
        item["text"]
        for item in chapters
        if item["chapter"] == chapter
    )
    final_path = project.chapter_dir / f"{chapter:04d}.md"
    translation = final_path.read_text(encoding="utf-8")
    model_id = resolve_translation_model(model).model
    local_qa = {
        "chapter": chapter,
        "commit_allowed": True,
        "issues": [],
    }
    state_identity = build_state_checkpoint_identity(
        novel_id=project.config.id,
        chapter=chapter,
        source_text=source_text,
        translation=translation,
        profile_id=project.config.profile,
        model_id=model_id,
        state_prompt="state prompt",
        existing_glossary_keys=[],
        prior_continuity={"latest_chapter": chapter - 1, "summary": "Previous memory."},
    )
    review_identity = build_review_checkpoint_identity(
        novel_id=project.config.id,
        chapter=chapter,
        source_text=source_text,
        translation=translation,
        profile_id=project.config.profile,
        model_id=model_id,
        review_prompt="review prompt",
        review_schema="{}",
        review_input_hash=f"review-{chapter}",
        relevant_glossary_text="",
    )
    provenance = pending_translation_model_record(model)
    metadata = {
        "chapter": chapter,
        "stage": "reviewed",
        "translation_stats": {},
        "local_qa": local_qa,
        "state": {},
        "state_stats": {},
        "state_checkpoint_identity": state_identity,
        "eligible_candidates": [],
        "prefilter_rejected": [],
        "review_result": {},
        "review_stats": {},
        "review_checkpoint_identity": review_identity,
        **provenance,
    }
    pending_md = project.pending_dir / f"{chapter:04d}.md"
    pending_json = project.pending_dir / f"{chapter:04d}.json"
    if state == "pair":
        pending_md.write_text(translation, encoding="utf-8")
    elif state != "metadata_only":
        raise ValueError(state)
    _write_json(pending_json, metadata)
    _write_json(
        project.qa_dir / f"{chapter:04d}.json",
        {**local_qa, **provenance},
    )
    return pending_md, pending_json


def test_preflight_proves_completed_pair_without_model_override(tmp_path):
    project = _project(tmp_path)
    _install_completed_pending_residue(
        project,
        chapter=1,
        state="pair",
        model="flash",
    )
    before = protected_state_snapshot(project)

    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["completed_pending_cleanup_required"] is True
    assert report["completed_pending_cleanup_chapters"] == [1]
    assert report["completed_pending_cleanup_states"] == [
        {"chapter": 1, "state": "pair"}
    ]
    assert report["pending_status"] == "requested"
    assert report["pending_chapter"] is None
    assert report["effective_model"] == "deepseek-v4-pro"
    assert report["project_writes"] == 0
    assert report["api_calls"] == 0


def test_preflight_proves_completed_metadata_only_residue(tmp_path):
    project = _project(tmp_path)
    _install_completed_pending_residue(
        project,
        chapter=1,
        state="metadata_only",
        model="pro",
    )
    before = protected_state_snapshot(project)

    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["completed_pending_cleanup_chapters"] == [1]
    assert report["completed_pending_cleanup_states"] == [
        {"chapter": 1, "state": "metadata_only"}
    ]
    assert report["pending_status"] == "requested"


def test_preflight_ignores_stale_completed_model_but_honors_active_pending(
    tmp_path,
):
    project = _project(tmp_path)
    _install_completed_pending_residue(
        project,
        chapter=1,
        state="pair",
        model="flash",
    )
    (project.pending_dir / "0002.md").write_text(
        "active paid Pro translation",
        encoding="utf-8",
    )
    _write_json(
        project.pending_dir / "0002.json",
        {
            "chapter": 2,
            "stage": "translated",
            "translation_model": "deepseek-v4-pro",
        },
    )

    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="flash",
    )

    assert report["status"] == "PASS"
    assert report["completed_pending_cleanup_chapters"] == [1]
    assert report["pending_status"] == "pending_model_override"
    assert report["pending_chapter"] == 2
    assert report["effective_model"] == "deepseek-v4-pro"


def test_preflight_blocks_ambiguous_completed_pending_before_runtime_prep(
    tmp_path,
    monkeypatch,
):
    project = _project(tmp_path)
    _, pending_json = _install_completed_pending_residue(project)
    pending_json.write_text("{broken", encoding="utf-8")
    before = protected_state_snapshot(project)

    monkeypatch.setattr(
        "production_path_preflight.prepare_production_runtime",
        lambda *args, **kwargs: pytest.fail(
            "model runtime preparation reached after cleanup proof failure"
        ),
    )

    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == "FAIL"
    assert report["completed_pending_cleanup_required"] is False
    assert report["completed_pending_cleanup_chapters"] == []
    assert any(
        "completed-pending cleanup proof failed" in error
        for error in report["errors"]
    )
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0



def test_preflight_pro_exercises_real_core_without_writing(tmp_path):
    project = _project(tmp_path)
    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["requested_model"] == "deepseek-v4-pro"
    assert report["effective_model"] == "deepseek-v4-pro"
    assert report["pending_status"] == "requested"
    assert report["pricing_version"] == "deepseek-v4-pro-0813-peak-offpeak"
    assert report["off_peak_pricing"]["cache_miss_rate_per_million"] == 0.66
    assert report["peak_pricing"]["output_rate_per_million"] == 3.96
    assert report["rate_label"] == "DeepSeek V4 Pro rates / 1M:"
    assert report["committed_qa_provenance_mode"] == "committed_qa_model_provenance"
    assert report["committed_qa_provenance_qa_root"] == str(
        (project.pending_dir.parent / "qa").resolve()
    )
    assert report["committed_qa_provenance_excluded_chapters"] == []
    assert report["sample_chapter"] == 1
    assert report["sample_legacy_commit_allowed"] is True
    assert report["sample_semantic_commit_decision_changed"] is False
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["blocked_write_attempts"] == 0
    assert report["protected_state_unchanged"] is True


def test_preflight_flash_uses_flash_pricing_and_label(tmp_path):
    _project(tmp_path)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="flash",
    )
    assert report["status"] == "PASS"
    assert report["effective_model"] == "deepseek-v4-flash"
    assert report["pricing_version"] == "deepseek-v4-flash-0731-peak-offpeak"
    assert report["off_peak_pricing"] == {
        "pricing_version": "deepseek-v4-flash-0731-peak-offpeak",
        "pricing_period": "off_peak",
        "request_started_utc": "2026-09-07T00:30:00+00:00",
        "cache_hit_rate_per_million": 0.007,
        "cache_miss_rate_per_million": 0.22,
        "output_rate_per_million": 0.66,
    }
    assert report["peak_pricing"]["cache_hit_rate_per_million"] == 0.014
    assert report["peak_pricing"]["cache_miss_rate_per_million"] == 0.44
    assert report["peak_pricing"]["output_rate_per_million"] == 1.32
    assert report["rate_label"] == "DeepSeek V4 Flash rates / 1M:"
    assert report["committed_qa_provenance_excluded_chapters"] == []


def test_preflight_honors_pinned_pending_model_without_mutation(tmp_path):
    project = _project(tmp_path)
    (project.pending_dir / "0002.md").write_text(
        "already paid Flash translation", encoding="utf-8"
    )
    _write_json(project.pending_dir / "0002.json", {
        "chapter": 2,
        "stage": "translated",
        "translation_model": "deepseek-v4-flash",
    })
    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["requested_model"] == "deepseek-v4-pro"
    assert report["effective_model"] == "deepseek-v4-flash"
    assert report["pending_status"] == "pending_model_override"
    assert report["pending_chapter"] == 2
    assert report["rate_label"] == "DeepSeek V4 Flash rates / 1M:"
    assert report["committed_qa_provenance_excluded_chapters"] == []


def test_preflight_excludes_legacy_unpinned_pending_from_false_qa_attribution(tmp_path):
    project = _project(tmp_path)
    (project.pending_dir / "0002.md").write_text(
        "legacy paid translation", encoding="utf-8"
    )
    _write_json(project.pending_dir / "0002.json", {
        "chapter": 2,
        "stage": "translated",
    })
    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["pending_status"] == "legacy_unpinned"
    assert report["pending_chapter"] == 2
    assert report["effective_model"] == "deepseek-v4-pro"
    assert report["committed_qa_provenance_excluded_chapters"] == [2]


def test_preflight_keeps_existing_strict_qa_authoritative(tmp_path):
    _project(
        tmp_path,
        glossary={
            "甲": {
                "english": "Jia",
                "category": "character",
                "status": "locked",
                "strict_qa": True,
            }
        },
        outputs={1: "Someone entered."},
    )
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert report["status"] == "PASS"
    assert report["sample_chapter"] == 1
    assert report["sample_legacy_commit_allowed"] is False
    assert report["sample_semantic_commit_decision_changed"] is False


def test_preflight_accepts_identical_next_final_and_pending_partial_commit(
    tmp_path,
):
    project = _project(tmp_path)
    pending_text = "already-paid Chapter 2"
    (project.pending_dir / "0002.md").write_text(
        pending_text,
        encoding="utf-8",
    )
    _write_json(
        project.pending_dir / "0002.json",
        {"chapter": 2, "stage": "reviewed"},
    )
    (project.chapter_dir / "0002.md").write_text(
        pending_text,
        encoding="utf-8",
    )

    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == "PASS"
    assert report["pending_chapter"] == 2
    assert report["progress_errors"] == []
    assert report["progress_consistency_report"]["unexpected_committed_files"] == [2]
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_preflight_blocks_mismatched_next_final_and_pending_partial_commit(
    tmp_path,
):
    project = _project(tmp_path)
    (project.pending_dir / "0002.md").write_text(
        "already-paid Chapter 2",
        encoding="utf-8",
    )
    _write_json(
        project.pending_dir / "0002.json",
        {"chapter": 2, "stage": "reviewed"},
    )
    (project.chapter_dir / "0002.md").write_text(
        "different committed-looking bytes",
        encoding="utf-8",
    )

    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )

    assert protected_state_snapshot(project) == before
    assert report["status"] == "FAIL"
    assert any(
        "duplicate billing" in error
        for error in report["progress_errors"]
    )
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0


def test_preflight_fails_on_progress_output_inconsistency_without_repairing(tmp_path):
    project = _project(tmp_path, outputs={})
    before = protected_state_snapshot(project)
    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert protected_state_snapshot(project) == before
    assert report["status"] == "FAIL"
    assert report["progress_errors"]
    assert report["api_calls"] == 0
    assert report["project_writes"] == 0
    assert report["protected_state_unchanged"] is True



def test_preflight_blocks_uncertain_paid_request_journal(tmp_path):
    from production_paid_request_journal import (
        paid_request_journal_path,
        request_identity,
        run_journaled_paid_request,
    )

    project = _project(tmp_path)
    journal_path = paid_request_journal_path(
        project.root / "data" / "provider_request_journal",
        "chapter_translation",
        2,
    )
    identity = request_identity(
        operation_kind="chapter_translation",
        novel_id="fixture",
        chapter=2,
        provider="deepseek",
        model="deepseek-v4-pro",
        profile_id="historical_military",
        source_sha256="2" * 64,
        request_payload={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "source"}],
            "max_tokens": 100,
        },
    )

    def save(path, value):
        _write_json(path, value)

    with pytest.raises(TimeoutError):
        run_journaled_paid_request(
            journal_path=journal_path,
            identity=identity,
            save_json=save,
            prepare_attempt=lambda: {},
            send_once=lambda: (_ for _ in ()).throw(
                TimeoutError("ambiguous")
            ),
            normalize_response=lambda raw, attempt: {},
        )

    report = run_production_path_preflight(
        "fixture",
        repo_root=tmp_path,
        model_selection="pro",
    )
    assert report["status"] == "FAIL"
    assert len(report["uncertain_paid_requests"]) == 1
    assert report["uncertain_paid_requests"][0]["chapter"] == 2
    assert "uncertain paid provider request" in " ".join(report["errors"])


@pytest.fixture(autouse=True)
def explicit_direct_route(monkeypatch):
    """These compatibility proofs exercise the explicitly selected direct route."""
    monkeypatch.setenv("NOVELTRANSLATOR_PROVIDER_BACKEND", "direct_deepseek")
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", "pro")
