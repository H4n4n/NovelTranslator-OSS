from public_test_support import supervised_callback_source
import copy
from pathlib import Path

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from translation_profile import load_translation_profile

from production_saved_checkpoint_identity import (
    SavedCheckpointIdentityError,
    build_review_checkpoint_identity,
    build_state_checkpoint_identity,
    require_saved_checkpoint_identity,
)


def _state(**overrides):
    values = {
        "novel_id": "fixture",
        "chapter": 7,
        "source_text": "源文",
        "translation": "English text",
        "profile_id": "historical_military",
        "model_id": "deepseek-v4-pro",
        "state_prompt": "state policy",
        "existing_glossary_keys": ["甲", "乙"],
        "prior_continuity": {"latest_chapter": 6, "summary": "Original memory."},
    }
    values.update(overrides)
    return build_state_checkpoint_identity(**values)


def _review(**overrides):
    values = {
        "novel_id": "fixture",
        "chapter": 7,
        "source_text": "源文",
        "translation": "English text",
        "profile_id": "historical_military",
        "model_id": "deepseek-v4-pro",
        "review_prompt": "review policy",
        "review_schema": '{"decisions":[],"issues":[]}',
        "review_input_hash": "a" * 64,
        "relevant_glossary_text": "甲 → A [locked]",
    }
    values.update(overrides)
    return build_review_checkpoint_identity(**values)


def test_state_checkpoint_identity_is_deterministic():
    first = _state()
    second = _state()

    assert first == second
    assert len(first["identity_sha256"]) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("novel_id", "other"),
        ("chapter", 8),
        ("source_text", "改源文"),
        ("translation", "Changed translation"),
        ("profile_id", "xianxia_xuanhuan"),
        ("model_id", "other-model"),
        ("state_prompt", "changed state policy"),
        ("existing_glossary_keys", ["乙", "甲"]),
        ("prior_continuity", {"latest_chapter": 6, "summary": "Changed memory."}),
        ("max_tokens", 3001),
        ("thinking_type", "enabled"),
    ],
)
def test_state_checkpoint_identity_changes_with_semantic_input(field, value):
    assert _state(**{field: value})["identity_sha256"] != _state()["identity_sha256"]


def test_review_checkpoint_identity_is_deterministic():
    first = _review()
    second = _review()

    assert first == second
    assert len(first["identity_sha256"]) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("novel_id", "other"),
        ("chapter", 8),
        ("source_text", "改源文"),
        ("translation", "Changed translation"),
        ("profile_id", "xianxia_xuanhuan"),
        ("model_id", "other-model"),
        ("review_prompt", "changed review policy"),
        ("review_schema", '{"decisions":[{}],"issues":[]}'),
        ("review_input_hash", "b" * 64),
        ("relevant_glossary_text", "甲 → Changed [locked]"),
        ("max_original_tokens", 4001),
        ("max_correction_tokens", 2001),
        ("thinking_type", "enabled"),
    ],
)
def test_review_checkpoint_identity_changes_with_semantic_input(field, value):
    assert _review(**{field: value})["identity_sha256"] != _review()["identity_sha256"]


def test_saved_checkpoint_identity_requires_exact_record():
    expected = _state()

    assert require_saved_checkpoint_identity(
        saved=copy.deepcopy(expected),
        expected=expected,
        label="saved state",
    ) == expected

    with pytest.raises(
        SavedCheckpointIdentityError,
        match="lacks a durable checkpoint identity",
    ):
        require_saved_checkpoint_identity(
            saved=None,
            expected=expected,
            label="saved state",
        )

    changed = copy.deepcopy(expected)
    changed["source_sha256"] = "f" * 64
    with pytest.raises(
        SavedCheckpointIdentityError,
        match="does not match current source/text/policy input",
    ):
        require_saved_checkpoint_identity(
            saved=changed,
            expected=expected,
            label="saved state",
        )


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_review_identity_binds_relevant_glossary_rendering():
    previous = {
        "ACTIVE_PROFILE": production.ACTIVE_PROFILE,
        "ACTIVE_NOVEL_CONFIG": production.ACTIVE_NOVEL_CONFIG,
        "MODEL": production.MODEL,
        "STATE_PROMPT": production.STATE_PROMPT,
        "TERM_REVIEW_MODEL_PROMPT": production.TERM_REVIEW_MODEL_PROMPT,
    }
    try:
        production.ACTIVE_PROFILE = load_translation_profile(
            "historical_military",
            REPO_ROOT,
        )
        production.ACTIVE_NOVEL_CONFIG = None
        production.MODEL = "test-model"
        production.STATE_PROMPT = "state policy"
        production.TERM_REVIEW_MODEL_PROMPT = "review policy"

        entries = {
            "甲": {
                "english": "Jia",
                "category": "character",
                "status": "locked",
            }
        }
        first = production.current_review_checkpoint_identity(
            1,
            "甲 entered.",
            "Jia entered.",
            [],
            glossary_entries=entries,
        )
        entries["甲"]["english"] = "Changed Jia"
        second = production.current_review_checkpoint_identity(
            1,
            "甲 entered.",
            "Jia entered.",
            [],
            glossary_entries=entries,
        )

        assert first["review_input_hash"] == second["review_input_hash"]
        assert (
            first["relevant_glossary_text_sha256"]
            != second["relevant_glossary_text_sha256"]
        )
        assert first["identity_sha256"] != second["identity_sha256"]
    finally:
        for name, value in previous.items():
            setattr(production, name, value)
