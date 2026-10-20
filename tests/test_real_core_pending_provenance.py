import json

import pytest

import noveltranslator_core as core
from translation_model_config import pending_translation_model_record
from translation_model_runtime import (
    TranslationModelRuntimeError,
    install_pending_model_provenance,
)


@pytest.fixture
def restore_core_atomic_writers():
    original_text = core.atomic_write_text
    original_json = core.atomic_write_json
    try:
        yield original_text, original_json
    finally:
        core.atomic_write_text = original_text
        core.atomic_write_json = original_json


@pytest.mark.parametrize("selection", ["pro", "flash"])
def test_real_core_nested_atomic_json_path_pins_new_paid_pending_metadata(
    tmp_path, restore_core_atomic_writers, selection
):
    pending = tmp_path / "pending"
    pending.mkdir()
    state = install_pending_model_provenance(core, selection, pending)

    text_path = pending / "0001.md"
    meta_path = pending / "0001.json"
    core.atomic_write_text(text_path, "paid translation")
    core.atomic_write_json(meta_path, {"chapter": 1, "stage": "translated"})

    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved == {
        "chapter": 1,
        "stage": "translated",
        **pending_translation_model_record(selection),
    }
    assert state["generated_chapters"] == {1}
    assert not (pending / "0001.md.tmp").exists()
    assert not (pending / "0001.json.tmp").exists()


def test_real_core_existing_legacy_pending_is_never_relabelled(
    tmp_path, restore_core_atomic_writers
):
    original_text, original_json = restore_core_atomic_writers
    pending = tmp_path / "pending"
    pending.mkdir()
    text_path = pending / "0002.md"
    meta_path = pending / "0002.json"
    original_text(text_path, "legacy paid translation")
    original_json(meta_path, {"chapter": 2, "stage": "translated"})

    install_pending_model_provenance(core, "pro", pending)
    core.atomic_write_json(meta_path, {"chapter": 2, "stage": "qa"})

    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved == {"chapter": 2, "stage": "qa"}
    assert "translation_model" not in saved


def test_real_core_conflicting_pinned_pending_metadata_fails_closed(
    tmp_path, restore_core_atomic_writers
):
    original_text, original_json = restore_core_atomic_writers
    pending = tmp_path / "pending"
    pending.mkdir()
    text_path = pending / "0003.md"
    meta_path = pending / "0003.json"
    original_text(text_path, "already paid")
    conflicting = {
        "chapter": 3,
        "stage": "translated",
        **pending_translation_model_record("flash"),
    }
    original_json(meta_path, conflicting)

    install_pending_model_provenance(core, "pro", pending)
    with pytest.raises(TranslationModelRuntimeError, match="conflicts"):
        core.atomic_write_json(meta_path, conflicting)
    assert json.loads(meta_path.read_text(encoding="utf-8")) == conflicting


def test_real_core_unknown_pinned_pending_metadata_fails_closed(
    tmp_path, restore_core_atomic_writers
):
    original_text, original_json = restore_core_atomic_writers
    pending = tmp_path / "pending"
    pending.mkdir()
    text_path = pending / "0004.md"
    meta_path = pending / "0004.json"
    original_text(text_path, "already paid")
    unknown = {
        "chapter": 4,
        "stage": "translated",
        "translation_model": "deepseek-v999-unknown",
    }
    original_json(meta_path, unknown)

    install_pending_model_provenance(core, "pro", pending)
    with pytest.raises(TranslationModelRuntimeError, match="conflicts"):
        core.atomic_write_json(meta_path, unknown)
    assert json.loads(meta_path.read_text(encoding="utf-8")) == unknown
