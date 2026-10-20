import json
from types import SimpleNamespace

import pytest

import noveltranslator_core as real_core
from committed_qa_provenance import (
    CommittedQAProvenanceError,
    PROVENANCE_MODE,
    committed_qa_model_record,
    install_committed_qa_model_provenance,
)
from translation_model_runtime import install_pending_model_provenance


def _capturing_core(calls):
    def atomic_write_json(path, data):
        calls.append((path, data))

    return SimpleNamespace(atomic_write_json=atomic_write_json)


@pytest.mark.parametrize(
    ("selection", "model", "version", "pricing_version"),
    [
        (
            "pro",
            "deepseek-v4-pro",
            "DeepSeek-V4-Pro-0813",
            "deepseek-v4-pro-0813-peak-offpeak",
        ),
        (
            "flash",
            "deepseek-v4-flash",
            "DeepSeek-V4-Flash-0731",
            "deepseek-v4-flash-0731-peak-offpeak",
        ),
    ],
)
def test_numeric_committed_qa_write_gets_exact_model_provenance(
    tmp_path, selection, model, version, pricing_version
):
    calls = []
    core = _capturing_core(calls)
    qa_dir = tmp_path / "output" / "qa"
    qa_dir.mkdir(parents=True)
    state = install_committed_qa_model_provenance(core, selection, qa_dir)
    payload = {
        "chapter": 1,
        "commit_allowed": True,
        "commit_blockers": [],
        "issues": [],
    }
    target = qa_dir / "0001.json"
    core.atomic_write_json(target, payload)

    assert state["mode"] == PROVENANCE_MODE
    assert state["model"] == model
    assert state["model_version"] == version
    assert state["pricing_version"] == pricing_version
    assert calls == [(target, {**payload, **committed_qa_model_record(selection)})]
    assert payload == {
        "chapter": 1,
        "commit_allowed": True,
        "commit_blockers": [],
        "issues": [],
    }


def test_non_qa_and_non_numeric_json_writes_are_unchanged(tmp_path):
    calls = []
    core = _capturing_core(calls)
    qa_dir = tmp_path / "output" / "qa"
    qa_dir.mkdir(parents=True)
    install_committed_qa_model_provenance(core, "pro", qa_dir)

    progress = tmp_path / "data" / "progress.json"
    named_qa = qa_dir / "latest.json"
    progress.parent.mkdir(parents=True)
    payload = {"next_chapter": 2}
    core.atomic_write_json(progress, payload)
    core.atomic_write_json(named_qa, payload)

    assert calls == [(progress, payload), (named_qa, payload)]


def test_legacy_unpinned_exclusion_stays_unlabelled_but_later_chapter_is_pinned(
    tmp_path,
):
    calls = []
    core = _capturing_core(calls)
    qa_dir = tmp_path / "output" / "qa"
    qa_dir.mkdir(parents=True)
    state = install_committed_qa_model_provenance(
        core, "pro", qa_dir, excluded_chapters={393}
    )
    legacy_payload = {"chapter": 393, "commit_allowed": True}
    new_payload = {"chapter": 394, "commit_allowed": True}
    legacy_path = qa_dir / "0393.json"
    new_path = qa_dir / "0394.json"

    core.atomic_write_json(legacy_path, legacy_payload)
    core.atomic_write_json(new_path, new_payload)

    assert state["excluded_chapters"] == frozenset({393})
    assert calls[0] == (legacy_path, legacy_payload)
    assert calls[1] == (
        new_path,
        {**new_payload, **committed_qa_model_record("pro")},
    )


def test_committed_qa_writer_is_idempotent_for_same_model_directory_and_exclusions(
    tmp_path,
):
    calls = []
    core = _capturing_core(calls)
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    first = install_committed_qa_model_provenance(
        core, "pro", qa_dir, excluded_chapters={7}
    )
    wrapped = core.atomic_write_json
    second = install_committed_qa_model_provenance(
        core, "deepseek-v4-pro", qa_dir, excluded_chapters={7}
    )
    assert second is first
    assert core.atomic_write_json is wrapped


def test_committed_qa_writer_fails_closed_if_reused_for_different_model(tmp_path):
    calls = []
    core = _capturing_core(calls)
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    install_committed_qa_model_provenance(core, "pro", qa_dir)
    with pytest.raises(CommittedQAProvenanceError, match="different model"):
        install_committed_qa_model_provenance(core, "flash", qa_dir)


def test_real_core_pending_and_committed_qa_writer_stack_compose(tmp_path):
    pending_dir = tmp_path / "output" / "pending"
    qa_dir = tmp_path / "output" / "qa"
    pending_dir.mkdir(parents=True)
    qa_dir.mkdir(parents=True)
    original_text = real_core.atomic_write_text
    original_json = real_core.atomic_write_json
    try:
        install_pending_model_provenance(real_core, "pro", pending_dir)
        install_committed_qa_model_provenance(real_core, "pro", qa_dir)

        pending_md = pending_dir / "0001.md"
        pending_json = pending_dir / "0001.json"
        qa_json = qa_dir / "0001.json"
        real_core.atomic_write_text(pending_md, "paid translation")
        real_core.atomic_write_json(
            pending_json, {"chapter": 1, "stage": "translated"}
        )
        real_core.atomic_write_json(
            qa_json,
            {
                "chapter": 1,
                "commit_allowed": True,
                "commit_blockers": [],
                "issues": [],
            },
        )

        pending_saved = json.loads(pending_json.read_text(encoding="utf-8"))
        qa_saved = json.loads(qa_json.read_text(encoding="utf-8"))
        expected = committed_qa_model_record("pro")
        assert all(pending_saved[key] == value for key, value in expected.items())
        assert all(qa_saved[key] == value for key, value in expected.items())
        assert qa_saved["commit_allowed"] is True
        assert not (pending_dir / "0001.json.tmp").exists()
        assert not (qa_dir / "0001.json.tmp").exists()
    finally:
        real_core.atomic_write_text = original_text
        real_core.atomic_write_json = original_json
