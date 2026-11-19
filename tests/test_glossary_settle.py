"""Settling glossary terms on a small synthetic project, with a fake provider and no network."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import glossary_settle as gs
from production_goat_runtime import GoatBudget
from test_chapter_review import OFF_PEAK, response, zero_network  # noqa: F401
from translation_profile import load_translation_profile

PROFILE = load_translation_profile("korean_fantasy", Path(__file__).resolve().parents[1])
SOURCES = ["흑룡검이 빛났다. 서진은 웃었다.", "흑룡검을 들었다. 서진이 말했다.", "서진의 흑룡검. 백운성에 갔다.",
           "백운성은 멀다. 서진과 흑룡검.", "직장인도 많다. 주인도 왔다. 증인도 없다. 인도로 간다."]


def project(tmp_path):
    root = tmp_path / "novel"
    (root / "data").mkdir(parents=True)
    (root / "data" / "chapters.json").write_text(json.dumps(
        [{"chapter": n, "text": t} for n, t in enumerate(SOURCES, 1)], ensure_ascii=False), encoding="utf-8")
    (root / "data" / "glossary.json").write_text(json.dumps(
        {"entries": {"백운성": {"english": "White Cloud Fortress", "category": "location"}}}, ensure_ascii=False),
        encoding="utf-8")
    (root / "translation_addendum.txt").write_text("Names in Revised Romanization.", encoding="utf-8")
    return NS(root=root, chapters_file=root / "data" / "chapters.json", glossary_file=root / "data" / "glossary.json",
              translation_addendum_file=root / "translation_addendum.txt", pause_file=root / "pause.flag",
              config=NS(id="synthetic", profile="korean_fantasy", off_peak_only=False))


class FakeSettler:
    def __init__(self, terms):
        self.terms, self.requests = terms, []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **wire):
        self.requests.append(wire)
        assert wire["messages"][0]["content"] == gs.SETTLE_PROMPT
        return response(json.dumps({"terms": self.terms}, ensure_ascii=False))


TERMS = [{"korean": "흑룡검", "english": "black dragon sword", "category": "skill", "notes": "named sword art"},
         {"korean": "서진", "english": "Seo Jin", "category": "person"},
         {"korean": "서진", "english": "Seojin", "category": "character"},
         {"korean": "청룡문", "english": "Azure Dragon Gate", "category": "organization"},
         {"korean": "백운성", "english": "White Cloud Castle", "category": "location"},
         {"korean": "인도", "english": "India", "category": "location"}]


def proposed(tmp_path, terms=TERMS):
    novel = project(tmp_path)
    client = FakeSettler(terms)
    summary = gs.propose(novel, client, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
                         clock=OFF_PEAK, log=lambda *_: None)
    return novel, client, summary, json.loads(open(summary["proposals_file"], encoding="utf-8").read())


def test_the_scan_finds_recurring_stems_the_glossary_lacks():
    found = {item["term"]: item for item in gs.scan([{"chapter": n, "text": t} for n, t in enumerate(SOURCES, 1)],
                                                   {"백운성": {}}, min_count=2)}
    assert found["흑룡검"]["count"] == 4 and found["서진"]["first"] == 1 and "백운성" not in found
    assert len(found["흑룡검"]["contexts"]) == 2


def test_proposals_are_checked_locally_and_the_glossary_is_untouched(tmp_path):
    novel, client, summary, result = proposed(tmp_path)
    before = hashlib.sha256(novel.glossary_file.read_bytes()).hexdigest()
    terms = result["terms"]
    assert terms["흑룡검"] == {"english": "Black Dragon Sword", "category": "skill", "notes": "named sword art"}
    assert terms["서진"]["category"] == "specialized recurring term" and terms["서진"]["alternatives"] == ["Seojin"]
    assert {item["korean"]: item["dropped"] for item in result["dropped"]} == {
        "청룡문": "not in the source", "백운성": "already in the glossary"}
    assert terms["인도"]["trap"]["inside_other_words"] == 3
    assert summary["calls"] == 1 and "흑룡검 (4x, first ch. 1)" in client.requests[0]["messages"][1]["content"]
    assert hashlib.sha256(novel.glossary_file.read_bytes()).hexdigest() == before


def test_a_rerun_sends_nothing(tmp_path):
    novel, _, _, _ = proposed(tmp_path)
    again = FakeSettler([])
    summary = gs.propose(novel, again, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
                         clock=OFF_PEAK, log=lambda *_: None)
    assert again.requests == [] and summary["proposed"] == 3


def test_install_adds_only_missing_keys_backs_up_and_leaves_traps_out(tmp_path):
    novel, _, _, _ = proposed(tmp_path)
    before = novel.glossary_file.read_bytes()
    result = gs.install(novel, run="settle", skip={"서진"}, clock=OFF_PEAK)
    entries = json.loads(novel.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert result["added"] == 1 and set(entries) == {"백운성", "흑룡검"}
    assert entries["흑룡검"]["status"] == "provisional"
    assert entries["흑룡검"]["source_provenance"]["origin"] == "manual"
    assert Path(result["backup"]).read_bytes() == before
    assert entries["백운성"]["english"] == "White Cloud Fortress"
