"""Word-start matching, the homonym rule, curly apostrophes in strict QA, and the
read-only homonym report. Synthetic names only."""
import json

import batch_translation as bt
import batch_rehearsal_compare as rehearsal
from glossary_homonym_report import as_markdown, homonym_report
from noveltranslator_core import (locked_high_priority_term_violations, strict_term_present_forms,
                                  strict_term_valid_targets)
from test_controlled_trial_plan import _empty_project


def test_a_korean_key_counts_only_where_it_starts_a_word():
    assert bt.word_occurrences("각하", "생각하는 각하께서 자각하셨다") == (1, 2)
    assert not bt.mentions("각하", "그는 생각하고 자각했다.") and bt.mentions("각하", "각하, 오셨습니까.")
    assert bt.mentions("龙骧营", "禁卫营改名为龙骧营")  # no word boundaries in Chinese: every occurrence counts


def test_a_key_mostly_inside_other_words_is_marked_for_the_decision():
    sources = {n: "그는 생각하고 또 생각했다. 각하께서 오셨다." for n in range(1, 4)}
    sheets = {1: {"chapter": 1, "terms": [{"source": "각하", "english": "Your Excellency", "category": "form of address",
                                           "sentence": ""}], "people": [], "events": [], "time": "", "issues": []},
              2: {"chapter": 2, "terms": [], "people": [], "events": [], "time": "", "issues": []},
              3: {"chapter": 3, "terms": [], "people": [], "events": [], "time": "", "issues": []}}
    group = bt.consolidate_candidates(sheets, sources, {})[0]
    assert group["inside_other_words"] and group["occurrences"] == 3
    request = bt.build_decision_request([group], model="m", policy="", established_glossary="", changes=[])
    assert '"inside_other_words": true' in request["messages"][1]["content"]
    assert "ordinary word or a homonym" in request["messages"][0]["content"]


def test_term_check_ignores_a_key_that_only_appears_inside_other_words():
    settled = {"각하": {"english": "Your Excellency", "status": "provisional", "approved_renderings": []}}
    assert bt.term_misses("He thought about it.", "그는 생각했다.", settled) == []
    assert bt.term_misses("He nodded.", "각하께서 고개를 끄덕였다.", settled) == ["각하"]


def test_term_audit_request_marks_inside_words_and_states_the_rule():
    targets = [{"chinese": "각하", "english": "Your Excellency", "contexts": []},
               {"chinese": "김갑", "english": "Kim Gap", "contexts": []}]
    request, ids = bt.build_term_audit_request(targets, model="m", system_prompt="audit", glossary_index="",
                                               start=1, end=2, chunk_index=1, total_chunks=1, inside_words={"각하"})
    user = request["messages"][1]["content"]
    assert user.count('"inside_other_words": true') == 1 and "ordinary word or a homonym" in user
    assert ids == {"c01": "각하", "c02": "김갑"}


def test_strict_qa_accepts_either_apostrophe():
    entries = {"왕자궁": {"english": "Prince's Palace", "category": "location", "status": "locked", "strict_qa": True}}
    for text in ("They reached the Prince's Palace.", "They reached the Prince\u2019s Palace."):
        assert locked_high_priority_term_violations("그들은 왕자궁에 도착했다.", text, entries, ("location",)) == []
    assert locked_high_priority_term_violations("그들은 왕자궁에 도착했다.", "They reached the palace.",
                                                entries, ("location",))
    assert set(strict_term_present_forms(entries["왕자궁"])) == {"Prince's Palace", "Prince\u2019s Palace"}
    # Audits save correction plans and prompts built from these forms and replay them exactly,
    # so they stay as they were: the curly form is only accepted where presence is checked.
    assert strict_term_valid_targets(entries["왕자궁"]) == ("Prince's Palace",)


def test_rehearsal_measure_accepts_the_forms_a_chapter_really_uses():
    forms = rehearsal.accepted_forms
    assert "duke of the north" in forms({"english": "Duke of the North (Herzog Norden)", "status": "provisional"})
    assert "silver lion" in forms({"english": "Silver Lions", "status": "locked"})
    assert "excellency" in forms({"status": "contextual", "approved_renderings": ["Your Excellency", "His Excellency"]})


def test_homonym_report_is_read_only(tmp_path):
    project = _empty_project(tmp_path)
    project.chapters_file.write_text(json.dumps([{"chapter": 1, "text": "그는 생각하고 또 생각했다. 각하."},
                                                {"chapter": 2, "text": "김갑이 왔다."}], ensure_ascii=False), encoding="utf-8")
    project.progress_file.write_text(json.dumps({"completed": [1, 2], "last_completed": 2, "next_chapter": 3}), encoding="utf-8")
    project.glossary_file.write_text(json.dumps({"entries": {
        "각하": {"english": "Your Excellency", "status": "locked", "category": "form of address"},
        "김갑": {"english": "Kim Gap", "status": "provisional", "category": "character"}}}, ensure_ascii=False),
        encoding="utf-8")
    before = project.glossary_file.read_bytes()
    rows = homonym_report(project)
    assert [row["source"] for row in rows] == ["각하"] and rows[0]["inside_words"] == 1 and rows[0]["as_word"] == 1
    assert "| 각하 | Your Excellency | locked |" in as_markdown(rows, "fixture")
    assert project.glossary_file.read_bytes() == before
