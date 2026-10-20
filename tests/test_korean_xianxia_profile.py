from pathlib import Path

from translation_profile import load_translation_profile


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_korean_xianxia_profile_loads_as_independent_reusable_profile():
    profile = load_translation_profile("korean_xianxia", REPO_ROOT)

    assert profile.id == "korean_xianxia"
    assert profile.display_name == "Korean Xianxia / Cultivation Fantasy"

    assert {
        "character",
        "alias",
        "sect",
        "clan",
        "faction",
        "organization",
        "location",
        "cultivation realm",
        "cultivation stage",
        "cultivation system",
        "cultivation technique",
        "martial technique",
        "secret art",
        "divine ability",
        "artifact",
        "formation",
        "pill",
        "beast",
        "energy",
        "dao concept",
        "law",
        "intent",
        "domain",
        "tribulation",
        "system term",
    }.issubset(profile.allowed_categories)

    instructions = profile.translation_instructions
    assert "Korean-language xianxia/cultivation fantasy" in instructions
    assert "Do not apply Chinese pinyin mechanically to Korean source text" in instructions
    assert "Do not leave unexplained Hangul or Hanja" in instructions
