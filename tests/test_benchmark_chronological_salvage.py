import json

import pytest

import benchmark_chronological_runtime as strict
import benchmark_chronological_salvage as salvage


def _state():
    return {
        "schema_version": 1,
        "policy_version": "chronological-state-v1",
        "benchmark": "chronological-state-growth-v1",
        "novel_id": "great_king_zhuxia",
        "source_language": "zh",
        "profile_id": "xianxia_xuanhuan",
        "through_chapter": 17,
        "glossary_entries": {},
        "continuity_summary": "Prior continuity.",
        "issues": ["Prior issue."],
        "parent_state_sha256": None,
        "state_events": [],
    }


def _document():
    return {
        "translation": "Complete English translation.",
        "state_update": {
            "glossary_upserts": [
                {
                    "source": "甲",
                    "english": "Jia",
                    "category": "character",
                    "confidence": "high",
                    "note": "first",
                }
            ],
            "continuity_summary": "Updated continuity.",
            "issues": [],
        },
    }


def test_missing_state_update_recovers_as_noop():
    state = _state()
    parsed, transformations = salvage.parse_salvage_document(
        json.dumps(
            {"translation": "Complete English translation."},
            ensure_ascii=False,
        ),
        state,
    )

    assert parsed["translation"] == "Complete English translation."
    assert parsed["state_update"] == {
        "glossary_upserts": [],
        "continuity_summary": "Prior continuity.",
        "issues": ["Prior issue."],
    }
    assert [item["kind"] for item in transformations] == [
        "missing_state_update_noop"
    ]


def test_duplicate_source_first_occurrence_wins_without_semantic_selection():
    state = _state()
    document = _document()
    document["state_update"]["glossary_upserts"] = [
        {
            "source": "入土三分",
            "english": "bury someone three inches into the ground",
            "category": "concept",
            "confidence": "high",
            "note": "first",
        },
        {
            "source": "入土三分",
            "english": "bury someone three inches into the ground",
            "category": "other",
            "confidence": "high",
            "note": "second",
        },
    ]

    parsed, transformations = salvage.parse_salvage_document(
        json.dumps(document, ensure_ascii=False),
        state,
    )

    assert parsed["state_update"]["glossary_upserts"] == [
        {
            "source": "入土三分",
            "english": "bury someone three inches into the ground",
            "category": "concept",
            "confidence": "high",
            "note": "first",
        }
    ]
    assert transformations[0]["kind"] == "duplicate_source_first_wins"
    assert transformations[0]["discarded_count"] == 1
    assert transformations[0]["discarded"][0]["index"] == 1
    assert transformations[0]["discarded"][0]["entry"]["category"] == "other"


def test_duplicate_policy_is_first_wins_even_if_english_conflicts():
    state = _state()
    document = _document()
    document["state_update"]["glossary_upserts"] = [
        {
            "source": "甲",
            "english": "First",
            "category": "concept",
            "confidence": "low",
            "note": "first",
        },
        {
            "source": "甲",
            "english": "Second",
            "category": "other",
            "confidence": "high",
            "note": "second",
        },
    ]

    parsed, transformations = salvage.parse_salvage_document(
        json.dumps(document, ensure_ascii=False),
        state,
    )

    assert parsed["state_update"]["glossary_upserts"][0]["english"] == "First"
    assert transformations[0]["discarded"][0]["entry"]["english"] == "Second"


def test_nonduplicate_document_matches_strict_parser():
    state = _state()
    text = json.dumps(_document(), ensure_ascii=False)

    parsed, transformations = salvage.parse_salvage_document(text, state)

    assert transformations == []
    assert parsed == strict.parse_model_document(text)


def test_present_wrong_type_state_update_remains_fail_closed():
    with pytest.raises(
        strict.ChronologicalRuntimeError,
        match="state_update must be a JSON object",
    ):
        salvage.parse_salvage_document(
            json.dumps(
                {
                    "translation": "Complete English translation.",
                    "state_update": [],
                }
            ),
            _state(),
        )


def test_duplicate_recovery_does_not_hide_unrelated_schema_error():
    document = _document()
    document["state_update"]["glossary_upserts"] = [
        {
            "source": "甲",
            "english": "Jia",
            "category": "character",
            "confidence": "high",
            "note": "first",
        },
        {
            "source": "甲",
            "english": "Jia",
            "category": "character",
            "confidence": "high",
            "note": "duplicate",
        },
    ]
    document["state_update"]["issues"] = "not-a-list"

    with pytest.raises(
        strict.ChronologicalRuntimeError,
        match="issues must be a list",
    ):
        salvage.parse_salvage_document(
            json.dumps(document, ensure_ascii=False),
            _state(),
        )
