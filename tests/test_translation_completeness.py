from types import SimpleNamespace

from catastrophic_completeness_runtime import install_catastrophic_completeness_qa
from translation_completeness import catastrophic_completeness_report


KNOWN_FAILURE_TRANSLATION = "Chapter Eighteen: Buried Three Inches Deep!"


def substantial_chinese_source():
    body = (
        "王城之中风声骤起，众人沿着长廊匆匆前行。"
        "殿外的侍卫交换了几个眼神，却没有一个人敢先开口。"
        "少年停在门前，想起昨日发生的一切，又重新握紧了手中的剑。"
        "远处钟声响起，院中的人群随即分开，让出一条通往正殿的路。"
    )
    return "第十八章 入土三分\n\n" + "\n\n".join(body for _ in range(8))


def test_known_heading_only_failure_is_blocked():
    report = catastrophic_completeness_report(
        substantial_chinese_source(),
        KNOWN_FAILURE_TRANSLATION,
    )

    assert report["blocked"] is True
    assert "heading_only_translation" in report["reasons"]
    assert report["observations"]["translation_word_count"] == 6
    assert report["observations"]["translation_to_source_body_char_ratio"] < 0.12


def test_tiny_fragment_from_large_source_is_blocked_without_heading_signal():
    report = catastrophic_completeness_report(
        substantial_chinese_source(),
        "He opened the door and stopped.",
    )

    assert report["blocked"] is True
    assert "gross_output_collapse" in report["reasons"]


def test_short_source_can_have_short_translation():
    report = catastrophic_completeness_report(
        "프롤로그\n\n끝이었다.",
        "Prologue\n\nIt was over.",
    )

    assert report["blocked"] is False


def test_substantial_source_with_structurally_complete_target_is_not_blocked():
    source = substantial_chinese_source()
    target = "\n\n".join(
        (
            "The wind rose abruptly through the royal city as everyone hurried "
            "along the corridor. The guards outside the hall exchanged glances, "
            "but none dared speak first."
        )
        for _ in range(12)
    )
    report = catastrophic_completeness_report(source, target)

    assert report["blocked"] is False


def test_runtime_gate_preserves_existing_blockers_and_adds_completeness_blocker():
    def legacy_qa(source_text, translation, glossary_entries, categories, **kwargs):
        return {
            "commit_allowed": False,
            "commit_blockers": [{"type": "locked_terminology", "chinese": "甲"}],
            "remaining_cjk_character_count": 0,
        }

    core = SimpleNamespace(translation_commit_qa=legacy_qa)
    state = install_catastrophic_completeness_qa(core)
    result = core.translation_commit_qa(
        substantial_chinese_source(),
        KNOWN_FAILURE_TRANSLATION,
        {},
        (),
    )

    assert state["mode"] == "catastrophic_completeness_hard_gate"
    assert result["commit_allowed"] is False
    assert [item["type"] for item in result["commit_blockers"]] == [
        "locked_terminology",
        "catastrophic_incompleteness",
    ]


def test_runtime_gate_keeps_clean_short_translation_allowed():
    def legacy_qa(source_text, translation, glossary_entries, categories, **kwargs):
        return {
            "commit_allowed": True,
            "commit_blockers": [],
            "remaining_cjk_character_count": 0,
        }

    core = SimpleNamespace(translation_commit_qa=legacy_qa)
    install_catastrophic_completeness_qa(core)
    result = core.translation_commit_qa(
        "프롤로그\n\n끝이었다.",
        "Prologue\n\nIt was over.",
        {},
        (),
    )

    assert result["commit_allowed"] is True
    assert result["commit_blockers"] == []
    assert result["catastrophic_completeness"]["blocked"] is False
