"""Term repair with a fake sender, on a small synthetic project."""
import json
from types import SimpleNamespace

import term_repair
from noveltranslator_core import atomic_write_json


def project_with(tmp_path, chapters, glossary, established=True):
    if established:  # Chapters already using the agreed form, which a repair needs before it changes anything.
        chapters = {**chapters, **{len(chapters) + n: ("藍衣嘍羅來了。", "A Blue-Clothed Minion came.") for n in (1, 2)}}
    root = tmp_path / "novel"
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "data").mkdir()
    project = SimpleNamespace(root=root, chapter_dir=root / "output" / "chapters", backup_dir=root / "backup",
                              data_dir=root / "data", progress_file=root / "data" / "progress.json",
                              chapters_file=root / "data" / "chapters.json", glossary_file=root / "data" / "glossary.json")
    atomic_write_json(project.chapters_file, [dict(chapter=n, text=source) for n, (source, _) in chapters.items()])
    atomic_write_json(project.glossary_file, {"entries": glossary})
    atomic_write_json(project.progress_file, dict(last_completed=len(chapters), rate_correct_cost_usd=1.0))
    for n, (_, english) in chapters.items():
        (project.chapter_dir / f"{n:04d}.md").write_bytes(english.encode("utf-8"))
    return project


GLOSSARY = {"藍衣嘍羅": dict(english="Blue-Clothed Minion", category="monster", status="locked"),
            "混亂": dict(english="Confusion", category="status effect", status="locked"),
            "老胡": dict(english="Hu Huahao", category="alias", status="provisional", alias_of="胡華豪"),
            "精力": dict(english="Energy", category="stat", status="contextual")}


def sender(answers):
    calls = []

    def send(messages):
        calls.append(messages)
        return json.dumps(answers.pop(0)), "stop", dict(rate_correct_cost_usd=0.002, prompt_tokens=100, completion_tokens=50)
    send.calls = calls
    return send


def quiet(_):
    pass


def test_only_terms_the_source_owns_and_the_english_lacks_are_sent(tmp_path):
    project = project_with(tmp_path, {1: ("兩名藍衣嘍羅，老胡和精力。", "Two blue-shirted minions came."),
                                      2: ("藍衣嘍羅來了。", "A Blue-Clothed Minion came.")}, GLOSSARY)
    send = sender([{"fixes": [], "fine": []}])
    term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert len(send.calls) == 1
    sent = json.loads(send.calls[0][1]["content"].split("ITEMS:\n")[1].split("\n\nENGLISH CHAPTER")[0])
    assert [item["term"] for item in sent] == ["藍衣嘍羅"]


def test_a_plural_is_fixed_and_the_chapter_is_backed_up(tmp_path):
    english = "Two blue-shirted minions came.\n\nThey left."
    project = project_with(tmp_path, {1: ("兩名藍衣嘍羅。", english)}, GLOSSARY)
    send = sender([{"fixes": [{"term": "藍衣嘍羅", "old": "blue-shirted minions", "new": "Blue-Clothed Minions"}], "fine": []}])
    summary = term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert (project.chapter_dir / "0001.md").read_text(encoding="utf-8") == "Two Blue-Clothed Minions came.\n\nThey left."
    assert summary["fixes_applied"] == 1
    assert next((project.backup_dir / "term_repairs").glob("0001_*.md")).read_text(encoding="utf-8") == english
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["rate_correct_cost_usd"] == 1.002


def test_a_rerun_does_not_pay_again(tmp_path):
    project = project_with(tmp_path, {1: ("藍衣嘍羅。", "A goon.")}, GLOSSARY)
    send = sender([{"fixes": [], "fine": [{"term": "藍衣嘍羅", "why": "ordinary word"}]}])
    term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert len(send.calls) == 1


def test_a_saved_answer_is_applied_after_a_crash_without_paying_again(tmp_path, monkeypatch):
    project = project_with(tmp_path, {1: ("兩名藍衣嘍羅。", "Two blue-shirted minions.")}, GLOSSARY)
    send = sender([{"fixes": [{"term": "藍衣嘍羅", "old": "blue-shirted minions", "new": "Blue-Clothed Minions"}]}])
    real = term_repair.apply_fixes

    def crash(*args):
        raise OSError("crash")
    monkeypatch.setattr(term_repair, "apply_fixes", crash)
    try:
        term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    except OSError:
        pass
    monkeypatch.setattr(term_repair, "apply_fixes", real)
    term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert len(send.calls) == 1
    assert "Blue-Clothed Minions" in (project.chapter_dir / "0001.md").read_text(encoding="utf-8")


def test_unsafe_fixes_are_rejected():
    entry = GLOSSARY["藍衣嘍羅"]
    text = "A minion and a minion. The blue-shirted minion left."

    def reason(old, new):
        return term_repair.rejected_reason({"old": old, "new": new}, text, entry)
    assert reason("minion", "Blue-Clothed Minion") == "old_not_unique"
    assert reason("not in the text", "Blue-Clothed Minion") == "old_not_unique"
    assert reason("The Blue-Clothed Minion", "The Blue-Clothed Minions") == "old_not_unique"
    assert reason("The blue-shirted minion left.", "He left.") == "agreed_english_missing"
    assert reason("The blue-shirted minion", "The Blue-Clothed Minion, a monster of terrible power who came from very far away") == "too_much_added"
    assert reason("The blue-shirted minion", "The Blue-Clothed Minion 藍") == "adds_source_script"
    assert reason("The blue-shirted minion", "The Blue-Clothed Minion") == ""


def test_singular_and_plural_count_as_the_term_and_a_family_name_is_enough():
    plural = dict(english="Traits", category="system term")
    assert term_repair.term_is_present(plural, "the slave mentality trait.")
    authority = dict(english="Authority", category="system term")
    assert term_repair.term_is_present(authority, "she lost her authorities.")
    family = dict(english="Count Gyreun family", category="faction")
    assert term_repair.term_is_present(family, "the son of count gyreun came.")
    assert not term_repair.term_is_present(dict(english="Count Ballerhard", category="title"), "count valerhard came.")
    assert not term_repair.term_is_present(dict(english="royal physician", category="government office"), "the court physician came.")


def test_the_budget_is_counted_and_a_repair_folder_marks_the_novel(tmp_path):
    project = project_with(tmp_path, {1: ("藍衣嘍羅。", "A goon.")}, GLOSSARY)
    term_repair.repair_novel(project, max_usd=1, send=sender([{"fixes": [], "fine": []}]), log=quiet)
    assert (project.root / "output" / "term_repairs" / "0001.json").is_file()


def test_a_fix_cannot_rewrite_another_terms_phrase():
    entry = dict(english="Demon Charm Technique", category="skill")
    fix = {"old": "the Powerful Charm Technique scroll", "new": "the Powerful Demon Charm Technique scroll"}
    text = "He traded the Powerful Charm Technique scroll."
    assert term_repair.rejected_reason(fix, text, entry, {"powerful charm technique"}) == "belongs_to_another_term"
    assert term_repair.rejected_reason(fix, text, entry) == ""


def test_a_form_the_book_has_not_adopted_is_not_imposed(tmp_path):
    chapters = {1: ("藍衣嘍羅。", "A goon."), 2: ("藍衣嘍羅！", "Goon!"), 3: ("藍衣嘍羅來了。", "A Blue-Clothed Minion came.")}
    project = project_with(tmp_path, chapters, GLOSSARY, established=False)
    send = sender([])
    summary = term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert send.calls == [] and summary["chapters_checked"] == 3


def test_an_agreed_form_with_an_explanation_is_not_repaired():
    glossary = {"視肉": dict(english="Shi Rou (Regenerating Beast)", category="monster", status="provisional")}
    assert term_repair.repairable_terms(glossary) == {}


def test_english_is_not_replaced_by_a_romanization():
    ordinary = term_repair.ordinary_words(["the anguish of a hate, parting", "the anguish of a hate, parting",
                                           "the anguish of a hate, parting", "of a minion"])
    entry = dict(english="Youhinao", category="alias")

    def reason(old, new, english="Youhinao"):
        return term_repair.rejected_reason({"old": old, "new": new}, old, dict(entry, english=english), (), ordinary)
    assert reason("death, anguish, birth", "death, Youhinao, birth") == "replaces_english_with_romanization"
    assert reason("Hate Of Parting came", "Henbieli came", "Henbieli") == "replaces_english_with_romanization"
    assert reason("Afaf came", "Afafu came", "Afafu") == ""
    assert reason("an OF Contract", "a KOF Contract", "KOF Contract") == ""
    assert reason("the anguish came", "the Hate came", "Hate") == ""


def test_a_repaired_novel_skips_the_old_disposition_chain(tmp_path):
    from production_historical_lineage import verify_disposition_lineage
    (tmp_path / "output" / "term_repairs").mkdir(parents=True)
    assert verify_disposition_lineage(SimpleNamespace(root=tmp_path), {"chapter": 1}) is None


def test_ordinary_word_categories_are_not_repaired():
    assert "混亂" not in term_repair.repairable_terms(GLOSSARY)
    assert "藍衣嘍羅" in term_repair.repairable_terms(GLOSSARY)


def test_a_chapter_using_part_of_a_name_is_checked_for_it():
    terms = {"도윤 카렐": dict(english="Doyun Karel", category="character")}
    assert term_repair.find_missing_terms("도윤이 왔다.", "Dohyun came.", terms) == ["도윤 카렐"]
    assert term_repair.find_missing_terms("도윤이 왔다.", "Doyun came.", terms) == []
    fix = {"old": "Dohyun came", "new": "Doyun came"}
    assert term_repair.rejected_reason(fix, "Dohyun came.", terms["도윤 카렐"]) == ""
    assert term_repair.find_missing_terms("카렐 경이 왔다.", "Sir Karel came.", terms) == []


def test_a_refused_chapter_is_flagged_and_the_run_continues(tmp_path):
    project = project_with(tmp_path, {1: ("藍衣嘍羅。", "A goon."), 2: ("藍衣嘍羅！", "Goon!")}, GLOSSARY)
    calls = []

    def send(messages):
        calls.append(messages)
        return "refused", ("content_filter" if len(calls) == 1 else "stop"), dict(rate_correct_cost_usd=0.0, prompt_tokens=0, completion_tokens=0)
    summary = term_repair.repair_novel(project, max_usd=1, send=send, log=quiet)
    assert summary["refused"] == [1] and len(calls) == 2


def test_the_budget_stops_new_requests(tmp_path):
    project = project_with(tmp_path, {1: ("藍衣嘍羅。", "A goon."), 2: ("藍衣嘍羅！", "Goon!")}, GLOSSARY)
    send = sender([{"fixes": [], "fine": []}, {"fixes": [], "fine": []}])
    summary = term_repair.repair_novel(project, max_usd=0.001, send=send, log=quiet)
    assert summary["stopped"] == "budget_reached" and len(send.calls) == 1
