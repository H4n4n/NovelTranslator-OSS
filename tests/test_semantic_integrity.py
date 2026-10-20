from semantic_integrity import entity_integrity_report, rendering_drift_report


def locked(english, category="character", **extra):
    return {
        "english": english,
        "category": category,
        "status": "locked",
        **extra,
    }


def test_source_backed_locked_entity_with_canonical_rendering_is_clean():
    glossary = {"甲": locked("Jia")}
    report = entity_integrity_report("甲 entered the hall.", "Jia entered the hall.", glossary)
    assert report["source_backed_identity_count"] == 1
    assert report["advisories"] == []
    assert report["strict_blockers"] == []
    assert report["strict_commit_allowed"] is True


def test_locked_entity_missing_canonical_family_is_advisory_by_default():
    glossary = {"甲": locked("Jia")}
    report = entity_integrity_report("甲 entered the hall.", "Someone entered the hall.", glossary)
    assert report["strict_commit_allowed"] is True
    assert report["strict_blockers"] == []
    assert report["advisories"][0]["type"] == "source_entity_missing_canonical_family"
    assert report["advisories"][0]["source_key"] == "甲"


def test_explicit_strict_entity_missing_canonical_family_is_a_strict_blocker():
    glossary = {"甲": locked("Jia", strict_qa=True)}
    report = entity_integrity_report("甲 entered the hall.", "Someone entered the hall.", glossary)
    assert report["advisories"] == []
    assert report["strict_commit_allowed"] is False
    assert report["strict_blockers"][0]["source_key"] == "甲"


def test_known_noncanonical_alias_is_reported_without_automatic_rewrite():
    glossary = {
        "鳴鶴拳": locked(
            "Crying Crane Fist",
            category="martial technique",
            repair_aliases=["Singing Crane Fist"],
        )
    }
    report = entity_integrity_report(
        "鳴鶴拳 was used.",
        "He used the Singing Crane Fist.",
        glossary,
    )
    issue = report["advisories"][0]
    assert issue["type"] == "known_noncanonical_entity_alias"
    assert issue["known_alias_hits"] == [
        {"rendering": "Singing Crane Fist", "count": 1}
    ]


def test_accepted_inflection_counts_as_canonical_family():
    glossary = {
        "騎士": locked(
            "Knight",
            category="military rank",
            accepted_inflections=["Knights"],
        )
    }
    report = entity_integrity_report("騎士 gathered.", "The Knights gathered.", glossary)
    assert report["advisories"] == []


def test_provisional_entries_are_not_promoted_into_identity_enforcement():
    glossary = {
        "甲": {
            "english": "Jia",
            "category": "character",
            "status": "provisional",
        }
    }
    report = entity_integrity_report("甲 entered.", "Someone entered.", glossary)
    assert report["source_backed_identity_count"] == 0
    assert report["advisories"] == []


def test_contextual_or_mundane_categories_are_not_forced_by_default():
    glossary = {
        "兄長": locked("Elder Brother", category="form of address"),
        "椅子": locked("Chair", category="artifact"),
    }
    report = entity_integrity_report("兄長 sat on 椅子.", "His brother sat down.", glossary)
    assert report["source_backed_identity_count"] == 0
    assert report["advisories"] == []


def test_possible_cross_entity_substitution_requires_missing_expected_identity():
    glossary = {
        "甲": locked("Jia"),
        "乙": locked("Yi"),
    }
    report = entity_integrity_report("甲 entered.", "Yi entered.", glossary)
    types = [item["type"] for item in report["advisories"]]
    assert "source_entity_missing_canonical_family" in types
    substitution = next(
        item for item in report["advisories"]
        if item["type"] == "possible_cross_entity_substitution"
    )
    assert substitution["source_key"] == "乙"
    assert substitution["rendering"] == "Yi"


def test_cross_entity_signal_is_not_emitted_when_expected_identity_is_present():
    glossary = {
        "甲": locked("Jia"),
        "乙": locked("Yi"),
    }
    report = entity_integrity_report("甲 entered.", "Jia met Yi in passing.", glossary)
    assert not any(
        item["type"] == "possible_cross_entity_substitution"
        for item in report["advisories"]
    )


def test_duplicate_canonical_ownership_is_treated_as_ambiguous_not_substitution():
    glossary = {
        "甲": locked("Jia"),
        "乙": locked("Yi"),
        "丙": locked("Yi"),
    }
    report = entity_integrity_report("甲 entered.", "Yi entered.", glossary)
    assert not any(
        item["type"] == "possible_cross_entity_substitution"
        for item in report["advisories"]
    )


def test_target_matching_uses_boundaries_for_ascii_names():
    glossary = {"白": locked("Han")}
    report = entity_integrity_report("白 appeared.", "Hannah appeared.", glossary)
    assert report["advisories"][0]["type"] == "source_entity_missing_canonical_family"


def test_rendering_drift_report_surfaces_multiple_renderings_across_chapters():
    observations = [
        {
            "chapter": 10,
            "source_key": "鳴鶴拳",
            "rendering": "Crying Crane Fist",
            "canonical": "Crying Crane Fist",
            "category": "martial technique",
        },
        {
            "chapter": 40,
            "source_key": "鳴鶴拳",
            "rendering": "Crying Crane Fist",
            "canonical": "Crying Crane Fist",
            "category": "martial technique",
        },
        {
            "chapter": 80,
            "source_key": "鳴鶴拳",
            "rendering": "Singing Crane Fist",
            "canonical": "Crying Crane Fist",
            "category": "martial technique",
        },
    ]
    report = rendering_drift_report(observations)
    assert report["terms_observed"] == 1
    assert report["drift_term_count"] == 1
    drift = report["drift"][0]
    assert drift["source_key"] == "鳴鶴拳"
    assert drift["variant_count"] == 2
    assert drift["variants"] == [
        {"rendering": "Crying Crane Fist", "count": 2, "chapters": [10, 40]},
        {"rendering": "Singing Crane Fist", "count": 1, "chapters": [80]},
    ]


def test_rendering_drift_report_ignores_single_stable_rendering():
    report = rendering_drift_report([
        {"chapter": 1, "source_key": "甲", "rendering": "Jia"},
        {"chapter": 2, "source_key": "甲", "rendering": "Jia"},
    ])
    assert report == {
        "terms_observed": 1,
        "drift_term_count": 0,
        "drift": [],
    }
