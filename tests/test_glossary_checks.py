"""Local glossary checks on small synthetic projects: no provider, nothing written."""
import hashlib
import json
from types import SimpleNamespace

import glossary_checks as gc
import novel_project


def entry(english, category="location", status="locked"):
    return {"english": english, "category": category, "status": status}


def chapters(*texts):
    return [{"chapter": n, "text": t} for n, t in enumerate(texts, 1)]


def test_a_short_key_used_inside_other_words_is_flagged():
    entries = {"인도": entry("India"), "토성": entry("Saturn")}
    rows = gc.trap_report(entries, chapters("직장인도 많다.", "주인도 왔다.", "증인도 없다.", "인도로 간다.",
                                             "토성이 보인다.", "토성에 간다.", "토성은 멀다."))
    assert [r["korean"] for r in rows] == ["인도"]
    assert rows[0]["occurrences"] == 4 and rows[0]["inside_other_words"] == 3 and rows[0]["examples"]


def test_an_occurrence_inside_a_longer_glossary_key_is_covered():
    entries = {"동해": entry("East Sea"), "동해함대": entry("East Sea Fleet", "military unit")}
    rows = gc.trap_report(entries, chapters("동해함대가 왔다.", "동해함대는 크다.", "동해함대의 배.", "동해에 갔다."))
    assert rows == []


def test_an_ordinal_before_a_numbered_key_is_part_of_the_name():
    entries = {"1 야전군": entry("1st Field Army", "military unit")}
    rows = gc.trap_report(entries, chapters("제1 야전군이 간다.", "제1 야전군은 강하다.", "제1 야전군의 전차."))
    assert rows == []


def test_the_source_english_contradicting_an_entry_is_reported_but_typos_and_acronyms_are_not():
    entries = {"악마의 눈": entry("Devil's Eye", "product"), "메디컬 콤플렉스": entry("Medical Complex", "company"),
               "연구소": entry("Center for Strategic Studies", "organization")}
    source = chapters("상품명은 악마의 눈(Diablo eye)이다.", "회사는 메디컬 콤플렉스(Medical Compelx)다.",
                      "연구소(CSIS)가 발표했다.", "작전명 블루문(Blue Moon)과 다시 블루문(Blue Moon).")
    conflicts, missing = gc.source_english_report(entries, source)
    assert [(r["korean"], r["source_english"]) for r in conflicts] == [("악마의 눈", {"Diablo eye": 1})]
    assert [(r["korean"], r["source_english"], r["chapters"]) for r in missing] == [("블루문", {"Blue Moon": 2}, [4])]


def test_a_word_glossed_with_a_different_example_each_time_is_not_a_missing_term():
    _, missing = gc.source_english_report({}, chapters("비행기(UH-60)와 비행기(F-35B)", "비행기(MV-22)"))
    assert missing == []


def test_english_forms_fold_apostrophes_drop_the_and_glosses_and_split_alternatives():
    assert gc.english_forms("the People's Republic of China") == {"people's republic of china"}
    assert gc.english_forms("Sinsi (God's City)") == {"sinsi (god's city)", "sinsi"}
    assert gc.english_forms("vanguard / Sonbong") == {"vanguard", "sonbong"}


def test_consistency_counts_only_chapters_that_use_the_term_and_reports_misses():
    entries = {"단호": entry("Danho"), "청림각": entry("Azure Grove Pavilion"), "흑룡": entry("Black Dragon")}
    source = chapters("단호가 왔다. 청림각에 갔다. 흑룡이 났다.", "단호는 웃었다. 청림각의 문.", "단호의 칼. 청림각.",
                      "그는 단호하게 말했다. 청림각이다.", "흑룡이다.", "흑룡이 울었다.")
    english = {1: "Danho came to Azure Grove Pavilion. A Black Dragon rose.",
               2: "Danho laughed at the Azure Grove Pavilion’s gate.", 3: "Danho’s blade. Azure Grove Pavilion.",
               4: "He said firmly. The Azure Grove Pavilion.", 5: "The Dark Wyrm.", 6: "The Dark Wyrm cried."}
    rows = gc.consistency_report(entries, source, english, min_chapters=3, threshold=0.8)
    # 단호 in 단호하게 is the word "firmly": chapter 4 does not count for the name.
    assert [(r["korean"], r["chapters"], r["missed_chapters"]) for r in rows] == [("흑룡", 3, [5, 6])]
    summary = gc.consistency_summary(entries, source, english)
    assert summary["entries"] == 3 and summary["fully_consistent"] == 2


def test_the_command_reads_a_project_and_writes_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "novel"
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "data").mkdir()
    project = SimpleNamespace(root=root, chapter_dir=root / "output" / "chapters",
                              progress_file=root / "data" / "progress.json",
                              chapters_file=root / "data" / "chapters.json", glossary_file=root / "data" / "glossary.json")
    project.chapters_file.write_text(json.dumps(chapters("인도로 간다.", "직장인도 많다.", "주인도 왔다.", "증인도 없다."),
                                                ensure_ascii=False), encoding="utf-8")
    project.glossary_file.write_text(json.dumps({"entries": {"인도": entry("India")}}, ensure_ascii=False),
                                     encoding="utf-8")
    project.progress_file.write_text(json.dumps({"last_completed": 1}), encoding="utf-8")
    (project.chapter_dir / "0001.md").write_text("Chapter 1\n\nTo India.", encoding="utf-8")
    monkeypatch.setattr(novel_project, "select_project", lambda novel, repo_root=None: project)

    def snapshot():
        return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}

    before = snapshot()
    assert gc.main(["--novel", "synthetic", "--json", "preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [r["korean"] for r in report["traps"]] == ["인도"]
    assert gc.main(["--novel", "synthetic", "--json", "consistency", "--min-chapters", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["entries"] == 1 and report["below_threshold"] == []
    assert snapshot() == before
