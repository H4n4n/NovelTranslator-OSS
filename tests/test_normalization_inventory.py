from build_normalization_inventory import (
    discover_entity_inventory,
    group_repeated_titles,
    parse_english_heading,
    regular_inflection_of,
    scan_artifacts,
    scan_pronoun_candidates,
    scan_residual_and_leakage,
    structural_inventory,
)


def source(text, title="Test (1)", chars=None, paragraphs=1):
    return {
        "text": text,
        "title": title,
        "source_character_count": chars or len(text),
        "paragraph_count": paragraphs,
    }


def test_source_identity_reports_source_aligned_spelling_variants():
    glossary = {"미라": {"english": "Mira", "category": "character", "status": "locked"}}
    entities, _ = discover_entity_inventory(
        glossary,
        [source("미라"), source("미라"), source("다른 사람")],
        {1: "Mira arrived.", 2: "Myra answered.", 3: "Someone else arrived."},
    )
    variants = {item["rendering"] for item in entities[0]["english_variants"]}
    assert {"Mira", "Myra"} <= variants
    assert entities[0]["proposed_canonical"] == "Mira"


def test_historical_retroactive_variant_is_strong_source_key_evidence():
    glossary = {"하늘": {"english": "Haneul", "category": "character", "status": "provisional"}}
    entities, _ = discover_entity_inventory(
        glossary,
        [source("하늘")],
        {1: "Hanul answered."},
        {"하늘": [{"old_english": "Hanul", "new_english": "Haneul", "reason": "reviewed correction"}]},
    )
    assert entities[0]["classification"] == "DETERMINISTIC_SAFE"
    assert entities[0]["proposed_canonical"] == "Haneul"


def test_same_english_different_source_identities_are_not_collapsed():
    glossary = {
        "탑주": {"english": "Tower Master", "category": "character", "status": "locked"},
        "마탑주": {"english": "Tower Master", "category": "character", "status": "locked"},
    }
    entities, shared = discover_entity_inventory(
        glossary, [source("탑주 마탑주")], {1: "Tower Master"}
    )
    assert len(entities) == 2
    assert shared == [{
        "english": "Tower Master",
        "source_keys": ["마탑주", "탑주"],
        "classification": "NO_CHANGE",
    }]


def test_regular_plural_and_possessive_are_legitimate_inflections():
    assert regular_inflection_of("Swordmasters", "Swordmaster")
    assert regular_inflection_of("Mira's", "Mira")
    assert not regular_inflection_of("Sword Master", "Swordmaster")


def test_repeated_title_identity_preserves_parts_and_detects_consistency():
    result = group_repeated_titles(
        [source("a", "붉은 잔 (1)"), source("b", "붉은 잔 (2)")],
        {1: "# Chapter 1: The Red Cup (1)\n", 2: "# Chapter 2: The Red Cup (2)\n"},
    )[0]
    assert result["part_numbers"] == [1, 2]
    assert result["difference"] == "consistent"


def test_repeated_title_missing_part_is_reported_without_translation():
    result = group_repeated_titles(
        [source("a", "붉은 잔 (1)"), source("b", "붉은 잔 (2)")],
        {1: "# Chapter 1: The Red Cup (1)\n", 2: "The story starts immediately.\n"},
    )[0]
    assert result["difference"] == "missing_title"
    assert result["missing_titles"][0]["unit"] == 2
    assert result["candidate_canonical_english_base"] == "The Red Cup"


def test_terminal_html_break_is_not_parsed_as_a_chapter_title():
    for suffix in ("<br>", "<br/>", "<br />"):
        parsed = parse_english_heading(f"Chapter 99{suffix}\n< 32. Moonlight (2) >\n")
        assert parsed == {"raw": f"Chapter 99{suffix}", "title": None, "kind": "chapter_heading"}


def test_html_break_heading_keeps_authoritative_repeated_source_title_identity():
    result = group_repeated_titles(
        [
            source("a", "32. 달빛(1)"),
            source("b", "32. 달빛(2)"),
            source("c", "32. 달빛(3)"),
        ],
        {
            1: "Chapter 98. 32. Moonlight (1)\n",
            2: "Chapter 99<br>\n< 32. Moonlight (2) >\n",
            3: "Chapter 100. 32. Moonlight (3)\n",
        },
    )[0]
    assert result["normalized_korean_base_title"] == "32. 달빛"
    assert result["part_numbers"] == [1, 2, 3]
    assert result["missing_titles"] == [
        {"unit": 2, "raw_heading": "Chapter 99<br>", "kind": "chapter_heading"}
    ]
    assert result["candidate_canonical_english_base"] == "32. Moonlight"
    assert result["deterministic_missing_title_recovery"] is True


def test_markdown_artifact_classes_are_counted():
    result = {item["artifact"]: item for item in scan_artifacts({1: "# Chapter 1\n\n**\n\n* * *\n"})}
    assert result["markdown_heading"]["occurrence_count"] == 1
    assert result["literal_double_asterisk_separator"]["occurrence_count"] == 1
    assert result["spaced_scene_separator"]["occurrence_count"] == 1


def test_generator_marker_is_detected():
    result = {item["artifact"]: item for item in scan_artifacts({1: "#!/GEN_METHOD_TITLE_START\n"})}
    assert result["generator_marker"]["occurrence_count"] == 1


def test_pronoun_scan_reports_candidate_but_does_not_correct():
    entity = {
        "category": "character",
        "glossary_canonical": "Mira",
        "source_identity": "미",
        "english_variants": [{"rendering": "Mira", "relationship": "full_name"}],
    }
    texts = {index: f"Mira raised his sword. He held his ground.\n" for index in range(1, 10)}
    texts[10] = "Mira rallied her soldiers from atop her horse.\n"
    result = scan_pronoun_candidates([entity], texts)
    assert result[0]["unit"] == 10
    assert result[0]["classification"] == "CONTEXT_REQUIRED"
    assert texts[10].startswith("Mira rallied her")


def test_residual_and_leakage_detection_is_exact():
    result = scan_residual_and_leakage({1: "English 한 漢 �\n```json\n", 2: "Plain English."})
    assert result["hangul"]["occurrence_count"] == 1
    assert result["han_cjk"]["occurrence_count"] == 1
    assert result["replacement_character"]["occurrence_count"] == 1
    assert result["code_fence"]["occurrence_count"] == 1


def test_structural_outlier_calculation_and_unit_268_assessment():
    sources = [source("x" * 100, chars=100, paragraphs=10) for _ in range(268)]
    texts = {index: ("y" * 100 + "\n\n") * 10 for index in range(1, 269)}
    texts[268] = "y" * 1000
    result = structural_inventory(sources, texts)
    assert result["unit_268"]["paragraph_ratio"] == 0.1
    assert result["unit_268"]["assessment"] == "normal_paragraph_consolidation"
