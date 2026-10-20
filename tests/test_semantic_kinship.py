from semantic_kinship import (
    KINSHIP_POLICY_VERSION,
    ordered_kinship_completeness_report,
    ordered_kinship_occurrences,
)


def test_extracts_mixed_gender_ordered_kinship_terms_only():
    source = "他们是姐弟，另一家是兄妹，还有一对兄弟和一对姐妹。"
    items = ordered_kinship_occurrences(source)
    assert [(item["source_term"], item["relation"]) for item in items] == [
        ("姐弟", "older_sister_younger_brother"),
        ("兄妹", "older_brother_younger_sister"),
    ]


def test_ace_shape_generic_siblings_is_diagnostic_only():
    source = "前文。" * 20 + "他们是亲生的姐弟。" + "后文。" * 20
    translation = (
        "Earlier context. " * 20
        + "Siblings. Biological siblings. "
        + "Later context. " * 20
    )
    report = ordered_kinship_completeness_report(source, translation)
    assert report["policy_version"] == KINSHIP_POLICY_VERSION
    assert report["ordered_kinship_count"] == 1
    assert report["diagnostic_count"] == 1
    assert report["diagnostics"][0]["source_term"] == "姐弟"
    assert report["diagnostics"][0]["classification"] == "generic_siblings"
    assert report["commit_decision_changed"] is False


def test_gender_pair_without_age_is_partial_diagnostic():
    source = "两人是姐弟。"
    translation = "The two were a sister and brother."
    report = ordered_kinship_completeness_report(source, translation)
    assert report["ordered_kinship_count"] == 1
    assert report["diagnostic_count"] == 1
    assert report["diagnostics"][0]["classification"] == "gender_visible_age_not_visible"


def test_explicit_older_sister_younger_brother_is_preserved():
    source = "两人是亲生姐弟。"
    translation = "They were biological siblings, an older sister and a younger brother."
    report = ordered_kinship_completeness_report(source, translation)
    assert report["ordered_kinship_count"] == 1
    assert report["diagnostic_count"] == 0
    assert report["observations"][0]["classification"] == "age_and_gender_visible"


def test_explicit_older_brother_younger_sister_is_preserved():
    source = "他们是一对兄妹。"
    translation = "They were an older brother and a younger sister."
    report = ordered_kinship_completeness_report(source, translation)
    assert report["diagnostic_count"] == 0
    assert report["observations"][0]["relation"] == "older_brother_younger_sister"


def test_unrelated_source_has_no_kinship_signal():
    report = ordered_kinship_completeness_report(
        "他独自走进厨房。",
        "He entered the kitchen alone.",
    )
    assert report["ordered_kinship_count"] == 0
    assert report["diagnostic_count"] == 0
