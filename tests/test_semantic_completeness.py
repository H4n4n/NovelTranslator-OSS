from semantic_completeness import (
    COMPLETENESS_POLICY_VERSION,
    explicit_source_gloss_completeness_report,
    explicit_source_glosses,
)


def test_extracts_explicit_cjk_parenthetical_gloss():
    source = "他已经做墩子（即饭店中负责切菜、切肉和配菜的人）半年了。"
    glosses = explicit_source_glosses(source)
    assert len(glosses) == 1
    assert glosses[0]["label"] == "墩子"
    assert glosses[0]["gloss"].startswith("即饭店中")


def test_ace_shape_without_explanation_is_advisory_only():
    source = (
        "前文。" * 20
        + "他已经做墩子（即饭店中负责切菜、切肉和配菜的人）半年了。"
        + "后文。" * 20
    )
    translation = (
        "Earlier context. " * 20
        + "You've been a chopper for half a year and still cannot handle the work. "
        + "Later context. " * 20
    )
    report = explicit_source_gloss_completeness_report(source, translation)
    assert report["policy_version"] == COMPLETENESS_POLICY_VERSION
    assert report["explicit_source_gloss_count"] == 1
    assert report["possible_omitted_gloss_count"] == 1
    assert report["diagnostics"][0]["type"] == "possible_omitted_explicit_source_gloss"
    assert report["diagnostics"][0]["source_label"] == "墩子"
    assert report["commit_decision_changed"] is False


def test_preserved_parenthetical_explanation_is_not_flagged():
    source = (
        "前文。" * 10
        + "他做墩子（即饭店中负责切菜和配菜的人）。"
        + "后文。" * 10
    )
    translation = (
        "Earlier. " * 10
        + "He worked as a prep cook (the person responsible for cutting and preparing ingredients). "
        + "Later. " * 10
    )
    report = explicit_source_gloss_completeness_report(source, translation)
    assert report["explicit_source_gloss_count"] == 1
    assert report["possible_omitted_gloss_count"] == 0
    assert report["diagnostics"] == []


def test_integrated_explanatory_cue_is_accepted_without_parentheses():
    source = "所谓刀手（也就是负责切配食材的人）站在案板旁。"
    translation = "The prep cook, meaning the person responsible for preparing ingredients, stood at the board."
    report = explicit_source_gloss_completeness_report(source, translation)
    assert report["possible_omitted_gloss_count"] == 0


def test_ordinary_parenthetical_without_explicit_gloss_cue_is_ignored():
    source = "他走进厨房（今天格外安静），拿起菜刀。"
    report = explicit_source_gloss_completeness_report(source, "He entered the quiet kitchen and took the knife.")
    assert report["explicit_source_gloss_count"] == 0
    assert report["possible_omitted_gloss_count"] == 0
