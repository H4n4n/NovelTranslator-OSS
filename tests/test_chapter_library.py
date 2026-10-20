import json

import pytest

from chapter_library import (
    chapter_production_index,
    production_unit_kind,
    source_prompt_heading,
    validate_chapter_library,
)
from noveltranslator_core import obtain_translation


def reset_unit(production_index, local_number, volume_index, title="Title"):
    heading = f"第{local_number}章 {title}"
    return {
        "chapter": production_index,
        "production_index": production_index,
        "local_chapter_number": local_number,
        "local_chapter_start": local_number,
        "local_chapter_end": local_number,
        "volume_index": volume_index,
        "heading": heading,
        "title": title,
    }


def test_reset_numbered_library_uses_production_index_as_unique_key():
    chapters = [
        reset_unit(1, 1, 1),
        reset_unit(2, 2, 1),
        reset_unit(3, 3, 1),
        reset_unit(4, 1, 2),
        reset_unit(5, 2, 2),
        reset_unit(6, 3, 2),
    ]
    assert not validate_chapter_library(chapters, 6, "spine_sequential")
    assert [chapter_production_index(item) for item in chapters] == list(range(1, 7))


def test_reset_source_prompt_preserves_local_reader_heading():
    chapter = reset_unit(587, 71, 8, "The Terrifying Fortress")
    label = source_prompt_heading(
        chapter,
        "spine_sequential",
        source_language="zh",
        target_language="en",
    )
    assert "PRODUCTION UNIT 587 (INTERNAL CONTROL INDEX" in label
    assert "READER-FACING LOCAL IDENTITY: Chapter 71" in label
    assert "SOURCE HEADING/TITLE TO TRANSLATE: 第71章 The Terrifying Fortress" in label
    assert "Translate the heading from zh into en" in label
    assert "do not copy source-language script" in label
    assert "Chapter 587" not in label


def test_global_numbering_heading_contract_used_by_existing_novels_is_unchanged():
    for chapter_number in (204, 1061):
        chapter = {"chapter": chapter_number}
        assert source_prompt_heading(
            chapter,
            "global",
            source_language="zh",
            target_language="en",
        ) == f"CHAPTER {chapter_number}"


def test_reset_source_prompt_preserves_local_range_without_exposing_production_index():
    chapter = reset_unit(589, 71, 8, "Fortress")
    chapter["local_chapter_end"] = 72
    chapter["heading"] = "第七十一-七十二章恐怖的堡壘"

    label = source_prompt_heading(
        chapter,
        "spine_sequential",
        source_language="zh",
        target_language="en",
    )

    assert "READER-FACING LOCAL IDENTITY: Chapters 71-72" in label
    assert "preserve a local range as a range" in label
    assert "SOURCE HEADING/TITLE TO TRANSLATE: 第七十一-七十二章恐怖的堡壘" in label
    assert "Chapter 589" not in label


def test_pending_recovery_uses_production_index_not_repeated_local_number(tmp_path):
    chapter = reset_unit(4, 1, 2)
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "0004.md").write_text("PAID TRANSLATION", encoding="utf-8")
    (pending / "0004.json").write_text(
        json.dumps({"chapter": 4, "stage": "translated"}), encoding="utf-8"
    )
    generation_calls = []

    recovered = obtain_translation(
        chapter_production_index(chapter),
        pending,
        lambda: generation_calls.append("called"),
    )

    assert recovered["source"] == "pending"
    assert recovered["translation"] == "PAID TRANSLATION"
    assert recovered["meta"]["chapter"] == 4
    assert generation_calls == []


def supplement_unit(production_index, volume_index=1, title="李閻全信息"):
    return {
        "chapter": production_index,
        "production_index": production_index,
        "unit_kind": "supplement",
        "volume_index": volume_index,
        "heading": title,
        "title": title,
        "source_file": "Text/info.xhtml",
        "spine_position": production_index,
    }


def test_legacy_entry_defaults_to_chapter_kind():
    assert production_unit_kind({"chapter": 1}) == "chapter"


def test_explicit_chapter_kind_is_supported():
    assert production_unit_kind({"chapter": 1, "unit_kind": "chapter"}) == "chapter"


def test_supplement_requires_no_fake_local_chapter_number():
    units = [reset_unit(1, 1, 1), supplement_unit(2), reset_unit(3, 2, 1)]
    assert not validate_chapter_library(units, 3, "spine_sequential")


def test_supplement_requires_source_title_and_rejects_fake_local_number():
    missing_title = supplement_unit(1)
    missing_title["heading"] = missing_title["title"] = ""
    fake_number = supplement_unit(1)
    fake_number["local_chapter_number"] = 1
    assert any(
        "lacks a source title" in error
        for error in validate_chapter_library([missing_title], 1, "spine_sequential")
    )
    assert any(
        "must not define local chapter numbering" in error
        for error in validate_chapter_library([fake_number], 1, "spine_sequential")
    )


def test_mixed_units_require_continuous_unique_production_indexes():
    duplicate = [reset_unit(1, 1, 1), supplement_unit(1)]
    errors = validate_chapter_library(duplicate, 2, "spine_sequential")
    assert any("duplicate production indexes" in error for error in errors)


def test_supplement_prompt_never_exposes_production_index_as_chapter_number():
    label = source_prompt_heading(
        supplement_unit(163),
        "spine_sequential",
        source_language="zh-Hant",
        target_language="en",
    )
    assert "UNIT KIND: SUPPLEMENTAL DOCUMENT" in label
    assert "SOURCE TITLE TO TRANSLATE: 李閻全信息" in label
    assert "translated supplemental title" in label
    assert "Chapter 163" not in label


@pytest.mark.parametrize(
    "kind,title,identity",
    [
        ("prologue", "Prologue", "Prologue"),
        ("epilogue", "에필로그(1)", "Epilogue"),
        ("side_story", "외전 1편", "Side Story"),
    ],
)
def test_named_txt_units_have_no_fake_chapter_identity(kind, title, identity):
    unit = supplement_unit(7, title=title)
    unit["unit_kind"] = kind
    label = source_prompt_heading(
        unit, "spine_sequential", source_language="ko", target_language="en"
    )
    assert f"READER-FACING IDENTITY: {identity}" in label
    assert "SOURCE TITLE TO TRANSLATE" in label
    assert "Chapter 7" not in label


def test_supplement_pending_recovery_uses_production_index(tmp_path):
    unit = supplement_unit(2)
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "0002.md").write_text("PAID SUPPLEMENT", encoding="utf-8")
    (pending / "0002.json").write_text(
        json.dumps({"chapter": 2, "stage": "translated"}), encoding="utf-8"
    )
    generation_calls = []
    recovered = obtain_translation(
        chapter_production_index(unit),
        pending,
        lambda: generation_calls.append("called"),
    )
    assert recovered["translation"] == "PAID SUPPLEMENT"
    assert generation_calls == []
