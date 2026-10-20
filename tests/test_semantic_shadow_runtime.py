from types import SimpleNamespace

from semantic_shadow_runtime import (
    SEMANTIC_SHADOW_MODE,
    build_semantic_shadow_report,
    install_semantic_shadow_qa,
    make_console_semantic_shadow_reporter,
)


def locked(english, category="character", **extra):
    return {
        "english": english,
        "category": category,
        "status": "locked",
        **extra,
    }


def legacy_qa(commit_allowed=True):
    return {
        "commit_allowed": commit_allowed,
        "commit_blockers": [] if commit_allowed else [{"type": "legacy"}],
        "remaining_cjk_character_count": 0,
        "locked_high_priority_term_violations": [],
    }


def test_shadow_clean_candidate_preserves_legacy_commit_authority():
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Jia entered.",
        {"甲": locked("Jia")},
        legacy_qa(True),
    )
    assert shadow["mode"] == SEMANTIC_SHADOW_MODE
    assert shadow["diagnostic_count"] == 0
    assert shadow["completeness_diagnostic_count"] == 0
    assert shadow["actionable_advisory_count"] == 0
    assert shadow["semantic_strict_blocker_count"] == 0
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def test_missing_canonical_family_is_diagnostic_only_in_shadow():
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Someone entered.",
        {"甲": locked("Jia")},
        legacy_qa(True),
    )
    assert shadow["diagnostic_count"] == 1
    assert shadow["diagnostic_types"] == {
        "source_entity_missing_canonical_family": 1
    }
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def test_known_repair_alias_is_actionable_but_still_non_blocking():
    glossary = {
        "鳴鶴拳": locked(
            "Crying Crane Fist",
            category="martial technique",
            repair_aliases=["Singing Crane Fist"],
        )
    }
    shadow = build_semantic_shadow_report(
        "鳴鶴拳 was used.",
        "He used the Singing Crane Fist.",
        glossary,
        legacy_qa(True),
    )
    assert shadow["actionable_advisory_count"] == 1
    assert shadow["actionable_advisory_types"] == {
        "known_noncanonical_entity_alias": 1
    }
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def test_explicit_source_gloss_omission_is_completeness_diagnostic_only():
    source = (
        "前文。" * 15
        + "他已经做墩子（即饭店中负责切菜、切肉和配菜的人）半年了。"
        + "后文。" * 15
    )
    translation = (
        "Earlier context. " * 15
        + "You've been a chopper for half a year. "
        + "Later context. " * 15
    )
    shadow = build_semantic_shadow_report(
        source,
        translation,
        {},
        legacy_qa(True),
    )
    assert shadow["explicit_source_gloss_count"] == 1
    assert shadow["completeness_diagnostic_count"] == 1
    assert shadow["completeness_diagnostic_types"] == {
        "possible_omitted_explicit_source_gloss": 1
    }
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def test_shadow_strict_parity_mismatch_never_overrides_legacy_commit():
    shadow = build_semantic_shadow_report(
        "甲 appeared.",
        "Someone appeared.",
        {"甲": locked("Jia", strict_qa=True)},
        legacy_qa(True),
    )
    assert shadow["semantic_strict_blocker_count"] == 1
    assert shadow["strict_parity_mismatch"] is True
    assert shadow["legacy_commit_allowed"] is True
    assert shadow["commit_decision_changed"] is False


def test_experimental_substitution_is_suppressed_by_default():
    glossary = {
        "甲": locked("Jia"),
        "乙": locked("Yi"),
    }
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Yi entered.",
        glossary,
        legacy_qa(True),
    )
    assert shadow["diagnostic_count"] == 1
    assert shadow["experimental_signal_count"] == 0
    assert shadow["suppressed_experimental_signal_count"] == 1


def test_install_wrapper_preserves_all_legacy_fields_and_adds_shadow_only():
    calls = []

    def original(source, translation, glossary, high_priority=()):
        calls.append((source, translation, glossary, high_priority))
        return {
            "commit_allowed": False,
            "commit_blockers": [{"type": "residual_source_script"}],
            "marker": "legacy-result",
        }

    core = SimpleNamespace(translation_commit_qa=original)
    install_semantic_shadow_qa(core)
    glossary = {"甲": locked("Jia")}
    result = core.translation_commit_qa("甲", "Jia", glossary, ("character",))

    assert calls == [("甲", "Jia", glossary, ("character",))]
    assert result["commit_allowed"] is False
    assert result["commit_blockers"] == [{"type": "residual_source_script"}]
    assert result["marker"] == "legacy-result"
    assert result["semantic_shadow"]["legacy_commit_allowed"] is False
    assert result["semantic_shadow"]["commit_decision_changed"] is False


def test_install_is_idempotent_and_does_not_stack_wrappers():
    call_count = 0

    def original(source, translation, glossary):
        nonlocal call_count
        call_count += 1
        return legacy_qa(True)

    core = SimpleNamespace(translation_commit_qa=original)
    first = install_semantic_shadow_qa(core)
    second = install_semantic_shadow_qa(core)
    assert first is second
    core.translation_commit_qa("甲", "Jia", {"甲": locked("Jia")})
    assert call_count == 1


def test_console_reporter_deduplicates_and_states_commit_decision_unchanged():
    lines = []
    reporter = make_console_semantic_shadow_reporter(lines.append)
    shadow = build_semantic_shadow_report(
        "甲 entered.",
        "Someone entered.",
        {"甲": locked("Jia")},
        legacy_qa(True),
    )
    reporter(shadow)
    reporter(shadow)
    assert len(lines) == 1
    assert "SEMANTIC SHADOW QA" in lines[0]
    assert "diagnostics=1" in lines[0]
    assert "completeness=0" in lines[0]
    assert "commit-decision=UNCHANGED" in lines[0]
