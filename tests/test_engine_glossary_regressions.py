from __future__ import annotations

import pytest

from glossary_integrity import (
    GlossaryReviewValidationError,
    normalize_glossary_review_merge_decision,
)


def test_same_batch_merge_degrades_to_accept_without_losing_alias_fields():
    decision = {
        "candidate_id": "c03",
        "action": "merge",
        "english": "A'Yu",
        "category": "alias",
        "notes": "Nickname for Li Jiangyu.",
        "approved_renderings": [],
        "merge_with": "歷江宇",
    }
    result = normalize_glossary_review_merge_decision(
        decision,
        source_key="阿宇",
        candidate_source_keys={"歷江宇", "阿宇"},
        existing_source_keys=set(),
        candidate_id="source:test-alias",
    )
    assert result["action"] == "accept"
    assert result["merge_with"] == ""
    assert result["english"] == "A'Yu"
    assert result["category"] == "alias"
    assert result["notes"] == "Nickname for Li Jiangyu."
    assert result["approved_renderings"] == []


def test_existing_merge_target_remains_merge():
    decision = {
        "candidate_id": "c03",
        "action": "merge",
        "english": "A'Yu",
        "category": "alias",
        "notes": "",
        "approved_renderings": [],
        "merge_with": "歷江宇",
    }
    result = normalize_glossary_review_merge_decision(
        decision,
        source_key="阿宇",
        candidate_source_keys={"阿宇"},
        existing_source_keys={"歷江宇"},
        candidate_id="source:test-alias",
    )
    assert result["action"] == "merge"
    assert result["merge_with"] == "歷江宇"


def test_self_merge_remains_invalid():
    decision = {
        "candidate_id": "c01",
        "action": "merge",
        "english": "Name",
        "category": "alias",
        "notes": "",
        "approved_renderings": [],
        "merge_with": "同詞",
    }
    with pytest.raises(
        GlossaryReviewValidationError,
        match="invalid glossary review merge target",
    ):
        normalize_glossary_review_merge_decision(
            decision,
            source_key="同詞",
            candidate_source_keys={"同詞"},
            existing_source_keys=set(),
            candidate_id="source:self",
        )


def test_unknown_merge_target_remains_invalid():
    decision = {
        "candidate_id": "c01",
        "action": "merge",
        "english": "Name",
        "category": "alias",
        "notes": "",
        "approved_renderings": [],
        "merge_with": "不存在",
    }
    with pytest.raises(
        GlossaryReviewValidationError,
        match="invalid glossary review merge target",
    ):
        normalize_glossary_review_merge_decision(
            decision,
            source_key="別名",
            candidate_source_keys={"別名"},
            existing_source_keys=set(),
            candidate_id="source:unknown",
        )
