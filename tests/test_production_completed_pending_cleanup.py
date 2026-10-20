import json
from pathlib import Path

import pytest

from novel_project import NovelConfig, NovelProject
from production_completed_pending_cleanup import (
    CompletedPendingCleanupError,
    cleanup_completed_pending,
    inspect_completed_pending_cleanup,
)
from production_saved_checkpoint_identity import (
    build_review_checkpoint_identity,
    build_state_checkpoint_identity,
)
from translation_model_config import pending_translation_model_record


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fixture(tmp_path, *, state="pair", pinned=True):
    root = tmp_path / "novels" / "fixture"
    config = NovelConfig(
        id="fixture",
        title="Fixture",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=1,
    )
    project = NovelProject(tmp_path, root, config)
    root.mkdir(parents=True)

    source_text = "甲 source"
    translation = "Final reviewed English"
    _write_json(project.config_file, config.to_dict())
    _write_json(
        project.chapters_file,
        [{"chapter": 1, "text": source_text}],
    )
    _write_json(
        project.progress_file,
        {
            "completed": [1],
            "last_completed": 1,
            "next_chapter": 2,
        },
    )

    project.chapter_dir.mkdir(parents=True)
    project.pending_dir.mkdir(parents=True)
    project.qa_dir.mkdir(parents=True)

    final = project.chapter_dir / "0001.md"
    final.write_text(translation, encoding="utf-8")

    local_qa = {
        "chapter": 1,
        "commit_allowed": True,
        "issues": [],
    }
    model = "deepseek-v4-pro"
    state_identity = build_state_checkpoint_identity(
        novel_id="fixture",
        chapter=1,
        source_text=source_text,
        translation=translation,
        profile_id=config.profile,
        model_id=model,
        state_prompt="state prompt",
        existing_glossary_keys=[],
        prior_continuity={"latest_chapter": 0, "summary": ""},
    )
    review_identity = build_review_checkpoint_identity(
        novel_id="fixture",
        chapter=1,
        source_text=source_text,
        translation=translation,
        profile_id=config.profile,
        model_id=model,
        review_prompt="review prompt",
        review_schema="{}",
        review_input_hash="review-input",
        relevant_glossary_text="",
    )
    meta = {
        "chapter": 1,
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
    }
    qa = dict(local_qa)
    if pinned:
        provenance = pending_translation_model_record("pro")
        meta.update(provenance)
        qa.update(provenance)

    pending_md = project.pending_dir / "0001.md"
    pending_json = project.pending_dir / "0001.json"
    if state == "pair":
        pending_md.write_text(translation, encoding="utf-8")
    elif state == "md_only":
        pending_md.write_text(translation, encoding="utf-8")
    elif state != "metadata_only":
        raise ValueError(state)
    if state != "md_only":
        _write_json(pending_json, meta)

    qa_path = project.qa_dir / "0001.json"
    _write_json(qa_path, qa)

    return {
        "project": project,
        "source_text": source_text,
        "translation": translation,
        "final": final,
        "pending_md": pending_md,
        "pending_json": pending_json,
        "qa_path": qa_path,
        "meta": meta,
        "qa": qa,
    }


def test_no_completed_pending_residue_is_empty(tmp_path):
    parts = _fixture(tmp_path)
    parts["pending_md"].unlink()
    parts["pending_json"].unlink()

    inspection = inspect_completed_pending_cleanup(parts["project"])

    assert inspection.required is False
    assert inspection.chapters == ()
    assert inspection.records == ()


@pytest.mark.parametrize(
    "state, expected_state",
    [
        ("pair", "pair"),
        ("metadata_only", "metadata_only"),
    ],
)
def test_exact_completed_pending_residue_is_proven(
    tmp_path,
    state,
    expected_state,
):
    parts = _fixture(tmp_path, state=state)

    inspection = inspect_completed_pending_cleanup(parts["project"])

    assert inspection.required is True
    assert inspection.chapters == (1,)
    assert inspection.records[0].state == expected_state


def test_exact_pair_cleanup_removes_markdown_then_metadata(tmp_path):
    parts = _fixture(tmp_path)

    inspection = inspect_completed_pending_cleanup(parts["project"])
    removed = cleanup_completed_pending(inspection)

    assert removed == (1,)
    assert not parts["pending_md"].exists()
    assert not parts["pending_json"].exists()
    assert parts["final"].is_file()
    assert parts["qa_path"].is_file()


def test_exact_metadata_only_cleanup_is_supported(tmp_path):
    parts = _fixture(tmp_path, state="metadata_only")

    inspection = inspect_completed_pending_cleanup(parts["project"])
    removed = cleanup_completed_pending(inspection)

    assert removed == (1,)
    assert not parts["pending_json"].exists()
    assert parts["final"].is_file()
    assert parts["qa_path"].is_file()


def test_json_cleanup_failure_after_markdown_unlink_is_retryable(
    tmp_path,
    monkeypatch,
):
    parts = _fixture(tmp_path)
    inspection = inspect_completed_pending_cleanup(parts["project"])
    real_unlink = Path.unlink
    failed = {"done": False}

    def fail_metadata_once(path, *args, **kwargs):
        path = Path(path)
        if path == parts["pending_json"] and not failed["done"]:
            failed["done"] = True
            raise PermissionError("injected metadata cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_metadata_once)

    with pytest.raises(
        CompletedPendingCleanupError,
        match="metadata cleanup failure",
    ):
        cleanup_completed_pending(inspection)

    assert not parts["pending_md"].exists()
    assert parts["pending_json"].is_file()
    assert parts["final"].is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)

    retry = inspect_completed_pending_cleanup(parts["project"])
    assert retry.records[0].state == "metadata_only"
    assert cleanup_completed_pending(retry) == (1,)
    assert not parts["pending_json"].exists()


def test_markdown_without_metadata_is_ambiguous_and_preserved(tmp_path):
    parts = _fixture(tmp_path, state="md_only")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="Markdown without reviewed metadata",
    ):
        inspect_completed_pending_cleanup(parts["project"])

    assert parts["pending_md"].is_file()


def test_different_pending_markdown_blocks_without_cleanup(tmp_path):
    parts = _fixture(tmp_path)
    parts["pending_md"].write_text("different", encoding="utf-8")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="Markdown differs",
    ):
        inspect_completed_pending_cleanup(parts["project"])

    assert parts["pending_md"].is_file()
    assert parts["pending_json"].is_file()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda meta: meta.__setitem__("stage", "state_extracted"),
            "not a durable reviewed checkpoint",
        ),
        (
            lambda meta: meta.__setitem__("chapter", 2),
            "chapter does not match filename",
        ),
        (
            lambda meta: meta.__setitem__(
                "local_qa",
                {"chapter": 1, "commit_allowed": False, "issues": []},
            ),
            "no passing saved local QA",
        ),
        (
            lambda meta: meta.pop("review_checkpoint_identity"),
            "missing review_checkpoint_identity",
        ),
    ],
)
def test_incomplete_or_wrong_reviewed_metadata_blocks(
    tmp_path,
    mutate,
    message,
):
    parts = _fixture(tmp_path)
    meta = json.loads(parts["pending_json"].read_text(encoding="utf-8"))
    mutate(meta)
    _write_json(parts["pending_json"], meta)

    with pytest.raises(CompletedPendingCleanupError, match=message):
        inspect_completed_pending_cleanup(parts["project"])

    assert parts["pending_md"].is_file()
    assert parts["pending_json"].is_file()


def test_malformed_metadata_blocks_and_is_preserved(tmp_path):
    parts = _fixture(tmp_path)
    parts["pending_json"].write_text("{broken", encoding="utf-8")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="cannot read completed pending metadata",
    ):
        inspect_completed_pending_cleanup(parts["project"])

    assert parts["pending_json"].is_file()


def test_divergent_pinned_model_provenance_blocks(tmp_path):
    parts = _fixture(tmp_path)
    meta = json.loads(
        parts["pending_json"].read_text(encoding="utf-8")
    )
    meta["translation_model_version"] = "tampered-version"
    _write_json(parts["pending_json"], meta)

    with pytest.raises(
        CompletedPendingCleanupError,
        match="pinned model provenance field translation_model_version",
    ):
        inspect_completed_pending_cleanup(parts["project"])

    assert parts["pending_md"].is_file()
    assert parts["pending_json"].is_file()


def test_checkpoint_identity_self_hash_tamper_blocks(tmp_path):
    parts = _fixture(tmp_path)
    meta = json.loads(parts["pending_json"].read_text(encoding="utf-8"))
    meta["state_checkpoint_identity"]["translation_sha256"] = "f" * 64
    _write_json(parts["pending_json"], meta)

    with pytest.raises(
        CompletedPendingCleanupError,
        match="identity_sha256 does not match",
    ):
        inspect_completed_pending_cleanup(parts["project"])


def test_checkpoint_source_binding_drift_blocks(tmp_path):
    parts = _fixture(tmp_path)
    chapters = json.loads(parts["project"].chapters_file.read_text(encoding="utf-8"))
    chapters[0]["text"] = "changed source"
    _write_json(parts["project"].chapters_file, chapters)

    with pytest.raises(
        CompletedPendingCleanupError,
        match="source hash does not match current source",
    ):
        inspect_completed_pending_cleanup(parts["project"])


def test_committed_final_binding_drift_blocks(tmp_path):
    parts = _fixture(tmp_path, state="metadata_only")
    parts["final"].write_text("different final", encoding="utf-8")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="translation hash does not match committed final text",
    ):
        inspect_completed_pending_cleanup(parts["project"])


def test_committed_qa_mismatch_blocks(tmp_path):
    parts = _fixture(tmp_path)
    qa = json.loads(parts["qa_path"].read_text(encoding="utf-8"))
    qa["issues"] = ["different"]
    _write_json(parts["qa_path"], qa)

    with pytest.raises(
        CompletedPendingCleanupError,
        match="committed QA differs",
    ):
        inspect_completed_pending_cleanup(parts["project"])


def test_legacy_unpinned_reviewed_residue_accepts_exact_unlabelled_qa(tmp_path):
    parts = _fixture(tmp_path, pinned=False)

    inspection = inspect_completed_pending_cleanup(parts["project"])

    assert inspection.chapters == (1,)
    assert inspection.records[0].state == "pair"


def test_snapshot_change_after_proof_blocks_before_any_delete(tmp_path):
    parts = _fixture(tmp_path)
    inspection = inspect_completed_pending_cleanup(parts["project"])
    parts["qa_path"].write_text("{}", encoding="utf-8")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="committed QA changed after cleanup proof",
    ):
        cleanup_completed_pending(inspection)

    assert parts["pending_md"].is_file()
    assert parts["pending_json"].is_file()


def test_multiple_records_are_all_rechecked_before_first_delete(tmp_path):
    first = _fixture(tmp_path)
    project = first["project"]

    chapters = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    chapters.append({"chapter": 2, "text": "乙 source"})
    _write_json(project.chapters_file, chapters)
    _write_json(
        project.config_file,
        {
            **project.config.to_dict(),
            "final_chapter": 2,
        },
    )

    config2 = NovelConfig(
        id="fixture",
        title="Fixture",
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=2,
    )
    project = NovelProject(tmp_path, project.root, config2)
    _write_json(
        project.progress_file,
        {
            "completed": [1, 2],
            "last_completed": 2,
            "next_chapter": 3,
        },
    )

    source2 = "乙 source"
    translation2 = "Second final"
    (project.chapter_dir / "0002.md").write_text(
        translation2,
        encoding="utf-8",
    )
    (project.pending_dir / "0002.md").write_text(
        translation2,
        encoding="utf-8",
    )
    local_qa2 = {"chapter": 2, "commit_allowed": True, "issues": []}
    state2 = build_state_checkpoint_identity(
        novel_id="fixture",
        chapter=2,
        source_text=source2,
        translation=translation2,
        profile_id=config2.profile,
        model_id="deepseek-v4-pro",
        state_prompt="state prompt",
        existing_glossary_keys=[],
        prior_continuity={"latest_chapter": 1, "summary": "Previous memory."},
    )
    review2 = build_review_checkpoint_identity(
        novel_id="fixture",
        chapter=2,
        source_text=source2,
        translation=translation2,
        profile_id=config2.profile,
        model_id="deepseek-v4-pro",
        review_prompt="review prompt",
        review_schema="{}",
        review_input_hash="review-input-2",
        relevant_glossary_text="",
    )
    provenance = pending_translation_model_record("pro")
    _write_json(
        project.pending_dir / "0002.json",
        {
            "chapter": 2,
            "stage": "reviewed",
            "translation_stats": {},
            "local_qa": local_qa2,
            "state": {},
            "state_stats": {},
            "state_checkpoint_identity": state2,
            "eligible_candidates": [],
            "prefilter_rejected": [],
            "review_result": {},
            "review_stats": {},
            "review_checkpoint_identity": review2,
            **provenance,
        },
    )
    _write_json(
        project.qa_dir / "0002.json",
        {**local_qa2, **provenance},
    )

    inspection = inspect_completed_pending_cleanup(project)
    assert inspection.chapters == (1, 2)

    (project.qa_dir / "0002.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        CompletedPendingCleanupError,
        match="committed QA changed after cleanup proof",
    ):
        cleanup_completed_pending(inspection)

    assert (project.pending_dir / "0001.md").is_file()
    assert (project.pending_dir / "0001.json").is_file()
    assert (project.pending_dir / "0002.md").is_file()
    assert (project.pending_dir / "0002.json").is_file()
