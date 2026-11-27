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


def test_a_sequel_settle_scans_only_the_new_chapters(tmp_path):
    """From chapter 3 on, a term found only in chapters 1-2 is neither sent nor proposed."""
    novel = project(tmp_path)
    first_only = SOURCES[0][5:7]  # a stem of chapter 1 that chapters 3-5 never use
    assert first_only not in "".join(SOURCES[2:]) and first_only in SOURCES[0]
    client = FakeSettler(TERMS + [{"korean": first_only, "english": "Shone", "category": "location"}])
    summary = gs.propose(novel, client, GoatBudget(10, 5.0), run="sequel", profile=PROFILE, min_count=2,
                         from_chapter=3, clock=OFF_PEAK, log=lambda *_: None)
    sent = client.requests[0]["messages"][1]["content"]
    assert "first ch. 3)" in sent
    assert not any(f"first ch. {n})" in sent for n in (1, 2))
    result = json.loads(open(summary["proposals_file"], encoding="utf-8").read())
    assert first_only not in result["terms"] and summary["from_chapter"] == 3
    assert {"korean": first_only, "english": "Shone", "category": "location",
            "dropped": "not in the source"} in result["dropped"]
    assert gs.chapters_from([{"chapter": 1, "text": ""}, {"chapter": 2, "production_index": 2, "text": ""}], 2) == [
        {"chapter": 2, "production_index": 2, "text": ""}]


def test_install_adds_only_missing_keys_backs_up_and_leaves_traps_out(tmp_path):
    novel, _, _, _ = proposed(tmp_path)
    before = novel.glossary_file.read_bytes()
    result = gs.install(novel, run="settle", skip={"서진"}, clock=OFF_PEAK)
    entries = json.loads(novel.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert result["added"] == 1 and set(entries) == {"백운성", "흑룡검"}
    assert entries["흑룡검"]["status"] == "provisional"
    assert entries["흑룡검"]["source_provenance"]["origin"] == gs.INSTALL_ORIGIN
    assert entries["흑룡검"]["source_provenance"]["verification"] == "model_settled"
    assert Path(result["backup"]).read_bytes() == before
    assert entries["백운성"]["english"] == "White Cloud Fortress"


def test_the_scan_skips_one_syllable_forms_and_keys_that_start_mid_word_but_not_compound_parts():
    texts = ["조선군 조선군 조선군. 조선이 크다. 조선이 멀다. 검 검 검.",
             "여진족 여진족 여진족. 여진은 강하다. 여진은 많다.",
             "대총관이 왔다. 대총관이 갔다. 대총관이 웃었다. 총관은 늙었다. 총관은 웃었다.",
             "요하에 간다. 요하에 닿았다. 필요하다 필요하다 필요하다 필요하다 필요하다. 중요하다 중요하다.",
             "흑룡검이 운다. 흑룡검을 든다. 검이 운다."]
    skipped = []
    found = {item["term"] for item in gs.scan([{"chapter": n, "text": t} for n, t in enumerate(texts, 1)], {},
                                              min_count=2, skipped=skipped)}
    assert {"조선", "여진", "총관", "흑룡검"} <= found  # A compound's first or last part is not mid-word.
    assert "요하" not in found and "검" not in found
    assert next(item for item in skipped if item["term"] == "요하") == {
        "term": "요하", "count": 2, "occurrences": 9, "mid_word": 7}


def test_the_two_contexts_are_the_first_and_the_middle_occurrence():
    texts = ["흑룡검 하나.", "흑룡검 둘.", "흑룡검 셋.", "흑룡검 넷.", "흑룡검 다섯.", "흑룡검 여섯."]
    item = next(i for i in gs.scan([{"chapter": n, "text": t} for n, t in enumerate(texts, 1)], {}, min_count=2)
                if i["term"] == "흑룡검")
    assert len(item["contexts"]) == 2 and "하나" in item["contexts"][0] and "넷" in item["contexts"][1]


def test_a_one_syllable_proposal_is_dropped():
    kept, dropped = gs.check_proposals([{"korean": "검", "english": "sword", "category": "weapon"}], "검이 운다.",
                                       {}, PROFILE)
    assert kept == {} and dropped[0]["dropped"].startswith("a 1-syllable key")


class ThinkingSettler(FakeSettler):
    """All reasoning and no content for a request with more candidates than `most`."""

    def __init__(self, terms, most):
        super().__init__(terms)
        self.most = most

    def create(self, **wire):
        candidates = wire["messages"][1]["content"].split("CANDIDATES:\n", 1)[1].count("\n") + 1
        if candidates > self.most:
            self.requests.append(wire)
            return response(None, finish="length")
        return super().create(**wire)


def test_an_all_reasoning_chunk_is_sent_once_more_as_two_halves(tmp_path):
    novel, logs = project(tmp_path), []
    client = ThinkingSettler(TERMS, most=1)
    summary = gs.propose(novel, client, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
                         clock=OFF_PEAK, log=logs.append)
    assert summary["requests"] == 1 and len(client.requests) == 3 and not summary["failed_requests"]
    assert any("two halves" in line for line in logs) and "흑룡검" in json.loads(
        open(summary["proposals_file"], encoding="utf-8").read())["terms"]
    journal = novel.root / "output" / "glossary_settle" / "settle" / "journal"
    assert any("glossary_settle_split" in path.name for path in journal.rglob("*"))
    again = ThinkingSettler([], most=1)
    gs.propose(novel, again, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
               clock=OFF_PEAK, log=lambda *_: None)
    assert again.requests == []


def test_halves_still_without_an_answer_are_reported_and_not_split_again(tmp_path):
    client = ThinkingSettler(TERMS, most=0)
    summary = gs.propose(project(tmp_path), client, GoatBudget(10, 5.0), run="settle", profile=PROFILE,
                         min_count=2, clock=OFF_PEAK, log=lambda *_: None)
    assert len(client.requests) == 3 and summary["proposed"] == 0
    assert summary["failed_requests"] and "also after splitting" in summary["failed_requests"][0][1]


class CutOffSettler(FakeSettler):
    """Cut off partway through the JSON (content, finish_reason "length") for a request with more candidates
    than `most`, or with a candidate in `stubborn`."""

    def __init__(self, terms, most, stubborn=()):
        super().__init__(terms)
        self.most, self.stubborn = most, set(stubborn)

    def create(self, **wire):
        listed = wire["messages"][1]["content"].split("CANDIDATES:\n", 1)[1].splitlines()
        if len(listed) > self.most or any(line.split(". ", 1)[1].split(" (", 1)[0] in self.stubborn for line in listed):
            self.requests.append(wire)
            return response('{"terms": [{"korean": "흑룡', finish="length")
        return super().create(**wire)


def test_a_chunk_cut_off_partway_is_sent_once_more_as_two_halves(tmp_path):
    novel, logs = project(tmp_path), []
    client = CutOffSettler(TERMS, most=1)
    summary = gs.propose(novel, client, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
                         clock=OFF_PEAK, log=logs.append)
    assert summary["requests"] == 1 and len(client.requests) == 3 and not summary["failed_requests"]
    assert any("cut off partway" in line and "two halves" in line for line in logs)
    assert "흑룡검" in json.loads(open(summary["proposals_file"], encoding="utf-8").read())["terms"]
    journal = novel.root / "output" / "glossary_settle" / "settle" / "journal"
    assert len([path for path in journal.rglob("*glossary_settle_split*")]) == 2


def test_a_half_cut_off_again_is_recorded_as_not_done(tmp_path):
    novel = project(tmp_path)
    entries = json.loads(novel.glossary_file.read_text(encoding="utf-8"))["entries"]
    candidates = [item["term"] for item in gs.scan(json.loads(novel.chapters_file.read_text(encoding="utf-8")),
                                                    entries, min_count=2)]
    second_half = candidates[len(candidates) // 2:]
    client = CutOffSettler(TERMS, most=len(candidates) - 1, stubborn=second_half[:1])
    summary = gs.propose(novel, client, GoatBudget(10, 5.0), run="settle", profile=PROFILE, min_count=2,
                         clock=OFF_PEAK, log=lambda *_: None)
    assert len(client.requests) == 3  # the chunk, then its halves; nothing is split further
    assert summary["failed_requests"] == [(1, f"{len(second_half)} of {len(candidates)} candidates got no answer, "
                                              "also after splitting the request in two")]
    saved = json.loads((novel.root / "output" / "glossary_settle" / "settle" / "chunks" / "0001.json")
                       .read_text(encoding="utf-8"))
    assert saved["not_done_terms"] == second_half and saved["terms"]


def test_installed_terms_are_not_held_from_audits_like_hand_settled_ones(tmp_path):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
    novel, _, _, _ = proposed(tmp_path)
    gs.install(novel, run="settle", clock=OFF_PEAK)
    entries = json.loads(novel.glossary_file.read_text(encoding="utf-8"))["entries"]
    assert not production.settled_before_translation(entries["흑룡검"])
    assert production.settled_before_translation(
        {"source_provenance": {"origin": "manual", "verification": "explicit_manual"}})


def test_install_refuses_while_a_run_holds_the_production_lock(tmp_path):
    import threading
    import pytest
    from production_run_lock import ProductionRunLockedError, production_run_lock
    novel, _, _, _ = proposed(tmp_path)
    before = novel.glossary_file.read_bytes()
    held, release = threading.Event(), threading.Event()

    def run():
        with production_run_lock(novel.root):
            held.set()
            release.wait(10)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert held.wait(10)
        with pytest.raises(ProductionRunLockedError):
            gs.install(novel, run="settle", clock=OFF_PEAK)
    finally:
        release.set()
        worker.join()
    assert novel.glossary_file.read_bytes() == before
    assert not (novel.root / "backup" / "glossary_settle").exists()
