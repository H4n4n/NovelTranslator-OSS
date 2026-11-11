from pathlib import Path

from translation_profile import load_translation_profile


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_korean_modern_profile_loads_with_its_own_categories_and_rules():
    profile = load_translation_profile("korean_modern", REPO_ROOT)

    assert profile.id == "korean_modern"
    assert profile.display_name == "Korean Modern"
    assert {"character", "form of address", "title", "real person", "company", "organization",
            "political party", "team", "product"}.issubset(profile.allowed_categories)

    instructions = profile.translation_instructions
    assert "Korean modern-day webnovel" in instructions
    assert "Never\nguess a character's gender" in instructions or "Never guess a character's gender" in instructions.replace("\n", " ")
    assert "Lee, Park, Choi, Yoon" in instructions
    assert "Unexpected residual Korean/Hangul is a commit blocker." in instructions


def test_product_and_event_names_keep_their_own_capitalization():
    # Every-word capitalization would turn iPhone into IPhone and War on Terror into War On Terror.
    profile = load_translation_profile("korean_modern", REPO_ROOT)
    assert "product" not in profile.formal_named_categories and "event" not in profile.formal_named_categories
