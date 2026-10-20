from semantic_integrity_policy import (
    calibrated_entity_integrity_report,
    classify_entity_integrity_report,
)


def locked(english, category="character", **extra):
    return {
        "english": english,
        "category": category,
        "status": "locked",
        **extra,
    }


def test_missing_canonical_family_is_diagnostic_not_advisory():
    report = calibrated_entity_integrity_report(
        "甲 entered.",
        "Someone entered.",
        {"甲": locked("Jia")},
    )
    assert report["advisories"] == []
    assert report["strict_blockers"] == []
    assert report["diagnostics"][0]["type"] == (
        "source_entity_missing_canonical_family"
    )
    assert report["strict_commit_allowed"] is True


def test_configured_rejected_alias_remains_actionable_advisory():
    report = calibrated_entity_integrity_report(
        "鳴鶴拳 was used.",
        "He used the Singing Crane Fist.",
        {
            "鳴鶴拳": locked(
                "Crying Crane Fist",
                category="martial technique",
                repair_aliases=["Singing Crane Fist"],
            )
        },
    )
    assert report["diagnostics"] == []
    assert report["advisories"][0]["type"] == (
        "known_noncanonical_entity_alias"
    )


def test_existing_strict_qa_violation_remains_blocker():
    report = calibrated_entity_integrity_report(
        "甲 entered.",
        "Someone entered.",
        {"甲": locked("Jia", strict_qa=True)},
    )
    assert report["diagnostics"] == []
    assert report["advisories"] == []
    assert report["strict_blockers"][0]["source_key"] == "甲"
    assert report["strict_commit_allowed"] is False


def test_cross_entity_substitution_is_suppressed_by_default():
    report = calibrated_entity_integrity_report(
        "甲 entered.",
        "Yi entered.",
        {
            "甲": locked("Jia"),
            "乙": locked("Yi"),
        },
    )
    assert report["experimental_signals"] == []
    assert report["suppressed_experimental_signal_count"] == 1
    assert [item["type"] for item in report["diagnostics"]] == [
        "source_entity_missing_canonical_family"
    ]


def test_cross_entity_substitution_can_be_explicitly_enabled_for_research():
    report = calibrated_entity_integrity_report(
        "甲 entered.",
        "Yi entered.",
        {
            "甲": locked("Jia"),
            "乙": locked("Yi"),
        },
        include_experimental=True,
    )
    assert report["suppressed_experimental_signal_count"] == 0
    assert report["experimental_signals"][0]["type"] == (
        "possible_cross_entity_substitution"
    )
    assert report["experimental_signals"][0]["rendering"] == "Yi"


def test_unknown_future_advisory_type_fails_safe_into_diagnostics():
    report = classify_entity_integrity_report(
        {
            "source_backed_identity_count": 1,
            "observations": [],
            "advisories": [{"type": "future_uncalibrated_signal"}],
            "strict_blockers": [],
        }
    )
    assert report["advisories"] == []
    assert report["diagnostics"] == [
        {"type": "future_uncalibrated_signal"}
    ]


def test_experimental_flag_must_be_boolean():
    try:
        classify_entity_integrity_report({}, include_experimental="yes")
    except ValueError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("non-boolean experimental flag should fail")
