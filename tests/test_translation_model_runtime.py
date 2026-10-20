import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from translation_model_config import resolve_translation_model
from translation_model_runtime import (
    TranslationModelRuntimeError,
    apply_core_model_config,
    install_pending_model_provenance,
    resolve_runtime_model,
)


def write_pending(tmp_path, chapter, meta=None):
    pending = tmp_path / "output" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{chapter:04d}.md").write_text("paid translation", encoding="utf-8")
    if meta is not None:
        (pending / f"{chapter:04d}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
    return pending


def test_runtime_defaults_to_pro_without_pending(tmp_path):
    result = resolve_runtime_model(None, tmp_path / "missing")
    assert result["status"] == "requested"
    assert result["requested_model"] == "deepseek-v4-pro"
    assert result["effective_model"] == "deepseek-v4-pro"


def test_runtime_allows_explicit_flash_without_pending(tmp_path):
    result = resolve_runtime_model("flash", tmp_path / "missing")
    assert result["status"] == "requested"
    assert result["effective_model"] == "deepseek-v4-flash"


def test_pinned_flash_pending_overrides_new_pro_request(tmp_path):
    pending = write_pending(
        tmp_path,
        500,
        {
            "chapter": 500,
            "stage": "translated",
            "translation_model": "deepseek-v4-flash",
        },
    )
    result = resolve_runtime_model("pro", pending)
    assert result["status"] == "pending_model_override"
    assert result["requested_model"] == "deepseek-v4-pro"
    assert result["effective_model"] == "deepseek-v4-flash"
    assert result["pending_chapter"] == 500


def test_matching_pinned_model_is_retained(tmp_path):
    pending = write_pending(
        tmp_path,
        501,
        {"translation_model": "deepseek-v4-pro"},
    )
    result = resolve_runtime_model("pro", pending)
    assert result["status"] == "pending_model_match"
    assert result["effective_model"] == "deepseek-v4-pro"


def test_legacy_unpinned_pending_is_recovered_without_model_guess(tmp_path):
    pending = write_pending(
        tmp_path,
        502,
        {"chapter": 502, "stage": "translated"},
    )
    result = resolve_runtime_model("flash", pending)
    assert result["status"] == "legacy_unpinned"
    assert result["effective_model"] == "deepseek-v4-flash"
    assert result["pending_chapter"] == 502


def test_unknown_pending_model_fails_closed(tmp_path):
    pending = write_pending(
        tmp_path,
        503,
        {"translation_model": "deepseek-future-model"},
    )
    with pytest.raises(TranslationModelRuntimeError, match="unknown model"):
        resolve_runtime_model("pro", pending)


def test_multiple_pending_translations_fail_closed(tmp_path):
    pending = write_pending(tmp_path, 504, {"translation_model": "deepseek-v4-pro"})
    write_pending(tmp_path, 505, {"translation_model": "deepseek-v4-pro"})
    with pytest.raises(TranslationModelRuntimeError, match="multiple pending"):
        resolve_runtime_model("pro", pending)


def test_proven_completed_pending_can_be_ignored_for_model_resolution(tmp_path):
    pending = write_pending(
        tmp_path,
        500,
        {
            "chapter": 500,
            "stage": "reviewed",
            "translation_model": "deepseek-v4-flash",
        },
    )

    result = resolve_runtime_model(
        "pro",
        pending,
        ignored_pending_chapters={500},
    )

    assert result["status"] == "requested"
    assert result["requested_model"] == "deepseek-v4-pro"
    assert result["effective_model"] == "deepseek-v4-pro"
    assert result["pending_chapter"] is None


def test_ignored_completed_pending_does_not_hide_active_next_pending(tmp_path):
    pending = write_pending(
        tmp_path,
        500,
        {
            "chapter": 500,
            "stage": "reviewed",
            "translation_model": "deepseek-v4-flash",
        },
    )
    write_pending(
        tmp_path,
        501,
        {
            "chapter": 501,
            "stage": "translated",
            "translation_model": "deepseek-v4-pro",
        },
    )

    result = resolve_runtime_model(
        "flash",
        pending,
        ignored_pending_chapters={500},
    )

    assert result["status"] == "pending_model_override"
    assert result["requested_model"] == "deepseek-v4-flash"
    assert result["effective_model"] == "deepseek-v4-pro"
    assert result["pending_chapter"] == 501


@pytest.mark.parametrize("value", [0, -1, True, "500"])
def test_invalid_ignored_pending_chapter_fails_closed(tmp_path, value):
    pending = write_pending(
        tmp_path,
        500,
        {"translation_model": "deepseek-v4-flash"},
    )
    with pytest.raises(
        TranslationModelRuntimeError,
        match="positive integers",
    ):
        resolve_runtime_model(
            "pro",
            pending,
            ignored_pending_chapters={value},
        )



def test_apply_core_model_config_switches_all_model_dependent_constants():
    core = SimpleNamespace(
        PRODUCTION_TRANSLATION_MODEL="deepseek-v4-pro",
        PRODUCTION_TRANSLATION_MAX_TOKENS=1,
        PRODUCTION_TRANSLATION_THINKING="enabled",
        V4_PRO_PRICES={},
        PRICING_VERSION="old",
    )
    config = apply_core_model_config(core, resolve_translation_model("flash"))
    assert config.model == "deepseek-v4-flash"
    assert core.PRODUCTION_TRANSLATION_MODEL == "deepseek-v4-flash"
    assert core.PRODUCTION_TRANSLATION_MAX_TOKENS == 12000
    assert core.PRODUCTION_TRANSLATION_THINKING == "disabled"
    assert core.V4_PRO_PRICES["off_peak"] == {
        "cache_hit": 0.007,
        "cache_miss": 0.22,
        "output": 0.66,
    }
    assert core.PRICING_VERSION == "deepseek-v4-flash-0731-peak-offpeak"


def fake_core():
    def atomic_write_text(path, text, encoding="utf-8"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)

    def atomic_write_json(path, data):
        atomic_write_text(path, json.dumps(data), encoding="utf-8")

    return SimpleNamespace(
        atomic_write_text=atomic_write_text,
        atomic_write_json=atomic_write_json,
    )


def test_new_pending_translation_metadata_is_pinned_to_generating_model(tmp_path):
    pending = tmp_path / "output" / "pending"
    pending.mkdir(parents=True)
    core = fake_core()
    install_pending_model_provenance(core, "flash", pending)

    text_path = pending / "0600.md"
    meta_path = pending / "0600.json"
    core.atomic_write_text(text_path, "new paid translation")
    core.atomic_write_json(
        meta_path,
        {
            "chapter": 600,
            "stage": "translated",
            "translation_stats": {"cost_usd": 1},
        },
    )
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert saved["translation_model"] == "deepseek-v4-flash"
    assert saved["translation_model_key"] == "flash"
    assert saved["translation_model_version"] == "DeepSeek-V4-Flash-0731"
    assert saved["translation_thinking"] == "disabled"
    assert saved["translation_max_tokens"] == 12000


def test_legacy_pending_metadata_is_not_retroactively_mislabeled(tmp_path):
    pending = tmp_path / "output" / "pending"
    pending.mkdir(parents=True)
    text_path = pending / "0601.md"
    meta_path = pending / "0601.json"
    text_path.write_text("legacy paid translation", encoding="utf-8")

    core = fake_core()
    install_pending_model_provenance(core, "pro", pending)
    core.atomic_write_json(
        meta_path,
        {"chapter": 601, "stage": "translated", "translation_stats": {}},
    )
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "translation_model" not in saved


def test_existing_pinned_metadata_cannot_be_rewritten_under_wrong_model(tmp_path):
    pending = tmp_path / "output" / "pending"
    pending.mkdir(parents=True)
    meta_path = pending / "0602.json"
    core = fake_core()
    install_pending_model_provenance(core, "pro", pending)

    with pytest.raises(TranslationModelRuntimeError, match="conflicts"):
        core.atomic_write_json(
            meta_path,
            {
                "chapter": 602,
                "stage": "translated",
                "translation_model": "deepseek-v4-flash",
            },
        )
