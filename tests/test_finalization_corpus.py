import hashlib

import pytest

from build_finalization_corpus import (
    FinalizationError,
    build_final_segments,
    plan_source_repartition,
    split_merged_production,
    verify_lossless_segments,
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def unit(index, body, number=None, kind="chapter", start=None, end=None, heading=None):
    start = start if start is not None else index * 10
    end = end if end is not None else start + 9
    number = index if number is None and kind == "chapter" else number
    heading = heading or (
        f"{number}화. Title {number}" if number is not None else kind.title()
    )
    return {
        "production_index": index,
        "unit_kind": kind,
        "source_number": number,
        "source_heading": heading,
        "title": f"Title {number}" if number is not None else kind.title(),
        "source_line_start": start,
        "source_line_end": end,
        "source_unit_sha256": digest(body),
        "source_document_sha256": "document-hash",
        "source_character_count": len(body),
        "paragraph_count": 1,
    }


def synthetic_spines():
    corrected = [
        unit(1, "source one", 1, start=1, end=9),
        unit(2, "source two A", 2, start=10, end=19),
        unit(3, "source two B", 3, start=20, end=29),
        unit(4, "epilogue", 4, kind="epilogue", start=30, end=39),
        unit(5, "side story", 5, kind="side_story", start=40, end=49),
    ]
    historical = [
        dict(corrected[0]),
        unit(2, "historical merged body", 2, start=10, end=29),
        {**corrected[3], "production_index": 3},
        {**corrected[4], "production_index": 4},
    ]
    production = {
        1: b"# Chapter 1: One\n\nEnglish one.\n",
        2: (
            b"# Chapter 2: Two A\n\nFirst paragraph.\n\n"
            b"# Chapter 3: Two B\n\nSecond paragraph.\n"
        ),
        3: b"# Chapter 4: Epilogue\n\nEpilogue body.\n",
        4: b"# Chapter 5: Side Story\n\nSide-story body.\n",
    }
    return historical, corrected, production


def test_ordinary_units_map_one_to_one_by_source_hash():
    historical, corrected, _ = synthetic_spines()
    plan = plan_source_repartition(historical, corrected)
    assert plan.historical_to_corrected == {1: 1, 3: 4, 4: 5}


def test_one_production_unit_maps_to_two_logical_units():
    historical, corrected, production = synthetic_spines()
    segments, plan, _, _ = build_final_segments(historical, corrected, production)
    assert plan.merged_production_index == 2
    assert [(item["final_index"], item["production_index"]) for item in segments] == [
        (1, 1), (2, 2), (3, 2), (4, 3), (5, 4)
    ]


def test_split_boundary_is_the_exact_embedded_heading():
    historical, corrected, production = synthetic_spines()
    before, after, split = split_merged_production(
        production[2], corrected[1], corrected[2]
    )
    assert before.endswith(b"First paragraph.\n\n")
    assert after.startswith(b"# Chapter 3: Two B")
    assert split["marker"] == "# Chapter 3: Two B"


def test_split_boundary_preserves_crlf_bytes_without_normalization():
    _, corrected, _ = synthetic_spines()
    data = (
        b"# Chapter 2: Two A\r\n\r\nFirst paragraph.\r\n\r\n"
        b"# Chapter 3: Two B\r\n\r\nSecond paragraph.\r\n"
    )
    before, after, split = split_merged_production(data, corrected[1], corrected[2])
    assert before + after == data
    assert before.endswith(b"First paragraph.\r\n\r\n")
    assert after.startswith(b"# Chapter 3: Two B\r\n")
    assert split["line_number"] == 5


def test_split_preserves_every_byte_and_paragraph_once():
    historical, corrected, production = synthetic_spines()
    segments, _, _, verification = build_final_segments(
        historical, corrected, production
    )
    assert segments[1]["data"] + segments[2]["data"] == production[2]
    assert verification["aggregate_byte_stream_equal"] is True
    assert verification["unmapped_production_byte_ranges"] == []
    assert verification["overlapping_production_byte_ranges"] == []


def test_remainder_mapping_shifts_after_split_by_proven_source_hash():
    historical, corrected, production = synthetic_spines()
    segments, _, _, _ = build_final_segments(historical, corrected, production)
    assert segments[3]["production_index"] == 3
    assert segments[4]["production_index"] == 4


def test_transition_into_epilogue_is_preserved():
    historical, corrected, production = synthetic_spines()
    segments, _, _, _ = build_final_segments(historical, corrected, production)
    assert segments[3]["source_unit"]["unit_kind"] == "epilogue"
    assert segments[3]["data"] == production[3]


def test_transition_into_side_story_is_preserved():
    historical, corrected, production = synthetic_spines()
    segments, _, _, _ = build_final_segments(historical, corrected, production)
    assert segments[4]["source_unit"]["unit_kind"] == "side_story"
    assert segments[4]["data"] == production[4]


def test_350_to_351_shape_produces_351_final_units():
    corrected = [unit(index, f"body {index}", index) for index in range(1, 352)]
    historical = [dict(item) for item in corrected[:169]]
    historical.append(unit(170, "merged", 170, start=1700, end=1719))
    corrected[169].update(source_line_start=1700, source_line_end=1709)
    corrected[170].update(source_line_start=1710, source_line_end=1719)
    historical.extend(
        {**item, "production_index": item["production_index"] - 1}
        for item in corrected[171:]
    )
    production = {
        index: f"# Chapter {index}: Title\n\nBody {index}.\n".encode()
        for index in range(1, 351)
    }
    production[170] = (
        b"# Chapter 170: A\n\nA.\n\n# Chapter 171: B\n\nB.\n"
    )
    for index in range(171, 351):
        production[index] = (
            f"# Chapter {index + 1}: Title\n\nBody {index}.\n".encode()
        )
    segments, _, _, verification = build_final_segments(
        historical, corrected, production
    )
    assert len(segments) == 351
    assert verification["aggregate_byte_stream_equal"] is True


def test_ambiguous_split_heading_hard_fails():
    _, corrected, production = synthetic_spines()
    production[2] += b"\n# Chapter 3: Duplicate\n"
    with pytest.raises(FinalizationError, match="missing or ambiguous"):
        split_merged_production(production[2], corrected[1], corrected[2])


def test_missing_split_heading_hard_fails():
    _, corrected, _ = synthetic_spines()
    with pytest.raises(FinalizationError, match="missing or ambiguous"):
        split_merged_production(
            b"# Chapter 2: Only\n\nBody.\n", corrected[1], corrected[2]
        )


def test_missing_production_unit_hard_fails():
    historical, corrected, production = synthetic_spines()
    del production[3]
    with pytest.raises(FinalizationError, match="missing production unit 0003"):
        build_final_segments(historical, corrected, production)


def test_duplicate_corrected_source_identity_hard_fails():
    historical, corrected, _ = synthetic_spines()
    corrected[4]["source_unit_sha256"] = corrected[3]["source_unit_sha256"]
    with pytest.raises(FinalizationError, match="duplicate source identity"):
        plan_source_repartition(historical, corrected)


def test_lossless_verifier_detects_missing_and_overlapping_ranges():
    production = {1: b"abcdef"}
    segments = [
        {"production_index": 1, "byte_start": 0, "byte_end": 4, "final_index": 1, "data": b"abcd"},
        {"production_index": 1, "byte_start": 3, "byte_end": 5, "final_index": 2, "data": b"de"},
    ]
    result = verify_lossless_segments(production, segments)
    assert result["overlapping_production_byte_ranges"]
    assert result["unmapped_production_byte_ranges"]
    assert result["aggregate_byte_stream_equal"] is False
