"""Which review findings a local check proves mechanical. Synthetic text only."""
from review_triage import is_dialogue_split, locked_restoration, triage

ENTRIES = {"백운검": {"english": "White Cloud Sword", "status": "locked"},
           "청풍": {"english": "Clear Wind", "status": "provisional"}}
SOURCE = "그는 백운검을 뽑았다. 청풍이 불었다."


def test_a_quotation_moved_into_its_own_paragraph_is_mechanical():
    assert is_dialogue_split("Han asked, “Where to?”", "Han asked.\r\n\r\n“Where to?”")
    assert is_dialogue_split("Han asked. “Where to?”", "Han asked.\n\n“Where to?”")


def test_a_split_that_also_rewords_is_not():
    assert not is_dialogue_split("Han asked, “Where to?”", "Han asked quietly.\n\n“Where to?”")
    assert not is_dialogue_split("Han asked, “Where to?”", "Han asked, “Where to?”")
    assert not is_dialogue_split("Han asked.\n\n“Where to?”", "Han asked.\n\n“Where now?”")


def test_restoring_a_locked_term_the_source_uses_is_mechanical():
    assert locked_restoration("He drew the White Cloud Blade.", "He drew the White Cloud Sword.", SOURCE, ENTRIES) == "백운검"


def test_unlocked_terms_long_rewrites_and_absent_terms_are_not():
    assert locked_restoration("The Pure Breeze blew.", "The Clear Wind blew.", SOURCE, ENTRIES) is None
    assert locked_restoration("He drew a blade. It was old.", "He drew the White Cloud Sword. It was new.", SOURCE, ENTRIES) is None
    assert locked_restoration("He drew the White Cloud Blade.", "He drew the White Cloud Sword.", "그는 웃었다.", ENTRIES) is None


def test_meaning_findings_always_need_judging():
    item = {"kind": "meaning", "english": "He drew the White Cloud Blade.", "fix": "He drew the White Cloud Sword."}
    assert triage(item, SOURCE, ENTRIES) is None
    assert triage({**item, "kind": "term"}, SOURCE, ENTRIES) == "locked_term:백운검"
