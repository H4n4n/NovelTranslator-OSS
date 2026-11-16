from chapter_text_checks import (
    curl_quotes, cut_off_detail, missing_dialogue_lines, review_flags, romanized_sound_lines,
)
from noveltranslator_core import pending_meta_after_typography_normalization


def test_curl_quotes_handles_speech_thoughts_apostrophes_and_cut_off_lines():
    text = ("\"That is—\" he said.\n'Damn it,' Mina thought.\n"
            "\"'Quoted' inside\" and Jun's horse.\n\"No, I—\"\n# Heading")
    assert curl_quotes(text) == ("“That is—” he said.\n‘Damn it,’ Mina thought.\n"
                                 "“‘Quoted’ inside” and Jun’s horse.\n“No, I—”\n# Heading")


def test_curl_quotes_opens_quotes_inside_italics():
    text = "*'Damn it. Nothing's going right.'*\n_\"Run!\"_ he heard."
    assert curl_quotes(text) == "*‘Damn it. Nothing’s going right.’*\n_“Run!”_ he heard."


def test_curl_quotes_leaves_curly_text_alone():
    text = "“Long live the King!”\n‘It hurts…’"
    assert curl_quotes(text) == text


def test_romanized_sound_lines_flags_only_romanized_korean():
    text = "\n".join([
        "Kwa-kwa-kwang—.", "Peok—.", "Tatatang—.", "*Tang—*", "Chaeng-gang—.",  # romanized
        "Boom—boom—.", "Thwack—.", "Rat-a-tat—.", "*Meow—*", "Poof-poof-bang—",  # English
        "And yet, and yet—", "The overflowing thoughts—", "“How dare—”",  # cut-off speech or narration
    ])
    assert romanized_sound_lines(text) == ["Kwa-kwa-kwang—.", "Peok—.", "Tatatang—.", "*Tang—*", "Chaeng-gang—."]


def test_missing_dialogue_counts_lines_that_open_with_a_quote():
    source = '"Close the gate!"\nnarration\n"Your Highness!"\n\'A thought.\''
    english = "“Close the gate!”\nNarration.\n*‘A thought.’*"
    assert missing_dialogue_lines(source, english) == 1
    assert missing_dialogue_lines(english, source) == 0


def test_review_flags_check_dialogue_only_for_korean_sources():
    source = '"One."\n"Two."'
    english = "“One.”\nTatatang—."
    kinds = [kind for kind, _ in review_flags(source, english, "ko")]
    assert kinds == ["romanized_sound_effect", "possible_missing_dialogue"]
    assert [kind for kind, _ in review_flags(source, english, "zh")] == ["romanized_sound_effect"]


def _chapter(paragraphs, text):
    return "\n\n".join([text] * paragraphs)


def test_cut_off_flags_a_chapter_that_stops_early():
    source = _chapter(40, "가" * 50)  # 2,000 source characters
    whole = _chapter(40, "a" * 115)  # 2.3 English characters for each
    assert cut_off_detail(source, whole, "ko") is None
    # Ruler chapter 26 lost its last 64 paragraphs: 82% of the length, 88% of the paragraphs.
    detail = cut_off_detail(source, _chapter(35, "a" * 108), "ko")
    assert "35 paragraphs for the source's 40" in detail
    assert review_flags(source, _chapter(35, "a" * 108), "ko")[0][0] == "possible_cut_off"
    # Half the chapter is flagged even when the paragraph count matches.
    assert cut_off_detail(source, _chapter(40, "a" * 60), "ko")


def test_cut_off_leaves_complete_and_unmeasurable_chapters_alone():
    source = _chapter(40, "가" * 50)
    # 13% short with every paragraph present is normal variation; so are merged paragraphs at full length.
    assert cut_off_detail(source, _chapter(40, "a" * 100), "ko") is None
    assert cut_off_detail(source, _chapter(20, "a" * 230), "ko") is None
    # Chinese needs more English per character; short chapters and other languages are not measured.
    chinese = _chapter(40, "天" * 50)
    assert cut_off_detail(chinese, _chapter(40, "a" * 165), "zh-Hant") is None
    assert cut_off_detail(chinese, _chapter(40, "a" * 115), "zh")
    assert cut_off_detail("가" * 400, "a", "ko") is None
    assert cut_off_detail(source, "a", "ja") is None


def test_quote_normalization_invalidates_work_derived_from_old_text():
    meta = {"stage": "reviewed", "state": {"x": 1}, "translation_stats": {"cost_usd": 0.01}}
    after = pending_meta_after_typography_normalization(meta, [{"repair_type": "curly_quotes"}])
    assert after["stage"] == "translated" and "state" not in after
    assert after["translation_stats"] == {"cost_usd": 0.01}
    assert after["deterministic_typography_normalizations"] == [{"repair_type": "curly_quotes"}]
