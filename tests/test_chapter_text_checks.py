from chapter_text_checks import curl_quotes, missing_dialogue_lines, review_flags, romanized_sound_lines
from noveltranslator_core import pending_meta_after_typography_normalization


def test_curl_quotes_handles_speech_thoughts_apostrophes_and_cut_off_lines():
    text = ("\"That is—\" he said.\n'Damn it,' Mina thought.\n"
            "\"'Quoted' inside\" and Jun's horse.\n\"No, I—\"\n# Heading")
    assert curl_quotes(text) == ("“That is—” he said.\n‘Damn it,’ Mina thought.\n"
                                 "“‘Quoted’ inside” and Jun’s horse.\n“No, I—”\n# Heading")


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


def test_quote_normalization_invalidates_work_derived_from_old_text():
    meta = {"stage": "reviewed", "state": {"x": 1}, "translation_stats": {"cost_usd": 0.01}}
    after = pending_meta_after_typography_normalization(meta, [{"repair_type": "curly_quotes"}])
    assert after["stage"] == "translated" and "state" not in after
    assert after["translation_stats"] == {"cost_usd": 0.01}
    assert after["deterministic_typography_normalizations"] == [{"repair_type": "curly_quotes"}]
