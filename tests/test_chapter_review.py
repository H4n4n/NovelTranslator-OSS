"""The chapter reviewer on a small synthetic project, with a fake provider and no network."""
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

import chapter_review
from production_goat_runtime import GoatBudget

OFF_PEAK = lambda: datetime(2026, 11, 7, 12, tzinfo=timezone.utc)  # A Saturday.
SOURCES = {
    1: "제 1 장 시작\n\n“검을 들어라.”\n\n그는 이 장 밖으로 물러났다.\n\n백운검이 빛났다.",
    2: "제 2 장 끝\n\n그는 웃었다.\n\n“돌아가자.”",
}
ENGLISH = {
    1: "Chapter 1: The Start\r\n\r\n“Raise your sword.”\r\n\r\nHe stepped back out of the yard.\r\n\r\nThe White Cloud Sword shone.\r\n",
    2: "Chapter 2: The End\r\n\r\nHe laughed.\r\n\r\n“Let us go back.”\r\n",
}


@pytest.fixture(autouse=True)
def zero_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))


def project(tmp_path, *, off_peak_only=False, last_completed=2):
    root = tmp_path / "novel"
    (root / "output" / "chapters").mkdir(parents=True)
    (root / "data").mkdir()
    for number, text in ENGLISH.items():
        (root / "output" / "chapters" / f"{number:04d}.md").write_bytes(text.encode("utf-8"))
    (root / "data" / "chapters.json").write_text(json.dumps(
        [{"chapter": n, "text": t} for n, t in SOURCES.items()], ensure_ascii=False), encoding="utf-8")
    (root / "data" / "glossary.json").write_text(json.dumps(
        {"entries": {"백운검": {"english": "White Cloud Sword", "category": "weapon"}}}, ensure_ascii=False), encoding="utf-8")
    (root / "data" / "progress.json").write_text(json.dumps({"last_completed": last_completed}), encoding="utf-8")
    (root / "translation_addendum.txt").write_text("이 장 is two zhang.", encoding="utf-8")
    return NS(root=root, progress_file=root / "data" / "progress.json", chapters_file=root / "data" / "chapters.json",
              chapter_dir=root / "output" / "chapters", glossary_file=root / "data" / "glossary.json",
              translation_addendum_file=root / "translation_addendum.txt", pause_file=root / "pause.flag",
              config=NS(id="synthetic", profile="korean_fantasy", off_peak_only=off_peak_only))


def response(content, finish="stop"):
    return NS(id="fake", model=chapter_review.MODEL, choices=[NS(finish_reason=finish, message=NS(content=content))],
              usage=NS(prompt_tokens=900, completion_tokens=300, total_tokens=1200,
                       completion_tokens_details=NS(reasoning_tokens=200), prompt_tokens_details=NS(cached_tokens=0)))


class FakeClient:
    """Answers find and verify requests; findings and verdicts are given per chapter."""

    def __init__(self, findings=None, verdicts=None):
        self.findings, self.verdicts, self.requests = findings or {}, verdicts or {}, []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **wire):
        self.requests.append(wire)
        user = wire["messages"][1]["content"]
        if wire["messages"][0]["content"] == chapter_review.VERIFY_PROMPT:
            return response(json.dumps({"verdicts": self.verdicts.get("all", [{"id": 1, "verdict": "keep", "why": "real"}])}))
        chapter = 1 if "[S1] 제 1 장" in user else 2
        return response(json.dumps({"findings": self.findings.get(chapter, [])}, ensure_ascii=False))


ZHANG = {"kind": "meaning", "s": 3, "e": 3, "source": "이 장 밖으로", "english": "He stepped back out of the yard.",
         "fix": "He stepped back two zhang.", "why": "이 장 is two zhang"}


def run(tmp_path, client, *, budget=None, **options):
    novel = options.pop("novel", None) or project(tmp_path)
    summary = chapter_review.review_novel(novel, client, budget or GoatBudget(20, 5.0), first=1, last=2,
                                          clock=OFF_PEAK, log=lambda *_: None, **options)
    return novel, summary


def test_a_verified_finding_becomes_a_candidate_and_no_chapter_changes(tmp_path):
    client = FakeClient(findings={1: [ZHANG]})
    novel, summary = run(tmp_path, client)
    assert summary["candidates"] == 1 and summary["calls"] == 3 and not summary["not_reviewed"]
    candidates = json.loads(open(summary["candidates_file"], encoding="utf-8").read())["replacements"]
    assert candidates == [{"source": ["이 장 밖으로"], "old": "He stepped back out of the yard.", "new": "He stepped back two zhang."}]
    assert (novel.chapter_dir / "0001.md").read_bytes().decode("utf-8") == ENGLISH[1]
    report = open(summary["report"], encoding="utf-8").read()
    assert "He stepped back two zhang." in report and "1 meaning" in report


def test_requests_use_high_reasoning_and_an_explicit_ceiling(tmp_path):
    client = FakeClient(findings={1: [ZHANG]})
    run(tmp_path, client)
    find = next(r for r in client.requests if "[S1] 제 1 장" in r["messages"][1]["content"])
    assert find["model"] == chapter_review.MODEL and find["reasoning_effort"] == "high"
    assert {r["max_completion_tokens"] for r in client.requests} == {chapter_review.FIND_TOKENS, chapter_review.VERIFY_TOKENS}
    assert "[E3] He stepped back out of the yard." in find["messages"][1]["content"]
    assert "백운검 = White Cloud Sword" in find["messages"][1]["content"]


def test_a_rerun_sends_nothing_and_a_lost_record_replays_from_the_journal(tmp_path):
    client = FakeClient(findings={1: [ZHANG]})
    novel, first = run(tmp_path, client)
    sent = len(client.requests)
    _, again = run(tmp_path, client, novel=novel)
    assert len(client.requests) == sent and again["candidates"] == 1
    for path in (novel.root / "output" / "review" / "0001_0002" / "chapters").glob("*.json"):
        path.unlink()
    _, replayed = run(tmp_path, client, novel=novel)
    assert len(client.requests) == sent and replayed["calls"] == 0 and replayed["candidates"] == 1


def test_findings_that_cannot_apply_are_dropped_before_any_verify_request(tmp_path):
    bad = [dict(ZHANG, english="Words that are not in the chapter at all."),
           dict(ZHANG, fix="He stepped back out of the yard."),
           dict(ZHANG, source="없는 말입니다")]
    client = FakeClient(findings={1: bad})
    novel, summary = run(tmp_path, client)
    record = json.loads((novel.root / "output" / "review" / "0001_0002" / "chapters" / "0001.json").read_text(encoding="utf-8"))
    assert [item["dropped"] for item in record["dropped"]] == [
        "English not found exactly once in the chapter", "fix is the same as the English",
        "source words not found in the chapter source"]
    assert summary["candidates"] == 0 and len(client.requests) == 2  # Two finds, no verify.


def test_the_verifier_can_drop_or_improve_a_fix(tmp_path):
    client = FakeClient(findings={1: [ZHANG, dict(ZHANG, english="The White Cloud Sword shone.", fix="The sword shone white.",
                                                  source="백운검이", kind="rule")]},
                        verdicts={"all": [{"id": 1, "verdict": "fix", "fix": "He stepped two zhang back.", "why": "better"},
                                          {"id": 2, "verdict": "drop", "why": "name was right"}]})
    _, summary = run(tmp_path, client)
    candidates = json.loads(open(summary["candidates_file"], encoding="utf-8").read())["replacements"]
    assert [c["new"] for c in candidates] == ["He stepped two zhang back."] and summary["dropped"] == 1


def test_a_restored_passage_keeps_the_file_line_endings(tmp_path):
    omission = dict(ZHANG, kind="omission", english="He stepped back out of the yard.",
                    fix="He stepped back out of the yard.\n\n“Not yet,” he said.")
    _, summary = run(tmp_path, FakeClient(findings={1: [omission]}))
    candidate = json.loads(open(summary["candidates_file"], encoding="utf-8").read())["replacements"][0]
    assert candidate["new"] == "He stepped back out of the yard.\r\n\r\n“Not yet,” he said."


def test_a_fix_that_would_also_change_another_chapter_is_refused():
    shared = "제 2 장 끝\n\n그는 웃었다. 이 장 밖으로.\n\n“돌아가자.”"
    kept = [{"chapter": 1, "english": "He stepped back out of the yard.", "fix": "He left.", "source": "이 장 밖으로"}]
    candidates, refused = chapter_review.build_candidates(
        kept, {1: SOURCES[1], 2: shared}, {1: ENGLISH[1], 2: ENGLISH[2] + "He stepped back out of the yard.\r\n"})
    assert candidates == [] and refused[0]["refused"] == "would also change chapter 2"


def test_the_budget_refuses_before_sending_and_leaves_the_chapters_for_a_rerun(tmp_path):
    client = FakeClient(findings={1: [ZHANG]})
    _, summary = run(tmp_path, client, budget=GoatBudget(20, 0.0001))
    assert client.requests == [] and len(summary["not_reviewed"]) == 2
    assert "GoatAllowanceExhausted" in summary["not_reviewed"][0][1]


def test_a_pause_flag_holds_every_request_until_it_is_removed(tmp_path):
    novel = project(tmp_path)
    novel.pause_file.write_text("", encoding="utf-8")
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        novel.pause_file.unlink()
    client = FakeClient()
    run(tmp_path, client, novel=novel, sleep=sleep, parallel=1)
    assert slept == [chapter_review.WAIT_SECONDS] and len(client.requests) == 2


def test_chapters_past_the_last_committed_one_are_not_reviewed(tmp_path):
    client = FakeClient()
    _, summary = run(tmp_path, client, novel=project(tmp_path, last_completed=1))
    assert summary["chapters"] == "1-1" and len(client.requests) == 1


def test_local_flags_cost_nothing():
    flags = chapter_review.local_flags("“가라.”\n\n“멈춰.”\n\n백운검", "Go.\n\n“Stop.” 검", {"백운검": {"english": "White Cloud Sword"}})
    assert [flag["kind"] for flag in flags] == ["source_script_left", "possible_missing_dialogue", "paragraph_gap", "glossary_term_not_rendered"]


def test_ranges_and_answers_are_parsed_strictly():
    assert chapter_review.parse_range("11-50") == (11, 50) and chapter_review.parse_range("7") == (7, 7)
    with pytest.raises(ValueError):
        chapter_review.parse_range("50-11")
    assert chapter_review.parse_json("```json\n{\"findings\": []}\n```") == {"findings": []}


class SlowFirstFind(FakeClient):
    """Holds chapter 1's find request open long enough for chapter 2 to try to start."""

    def create(self, **wire):
        import time
        if "[S1] 제 1 장" in wire["messages"][1]["content"] and wire["messages"][0]["content"] != chapter_review.VERIFY_PROMPT:
            time.sleep(0.5)
        return super().create(**wire)


def test_a_chapter_refused_only_for_reserved_budget_goes_again_when_room_frees(tmp_path):
    logs = []
    novel = project(tmp_path)
    budget = GoatBudget(20, 0.04)  # Room for one find reservation at a time, not two.
    summary = chapter_review.review_novel(novel, SlowFirstFind(findings={1: [ZHANG]}), budget, first=1, last=2,
                                          clock=OFF_PEAK, log=logs.append, parallel=2)
    assert summary["not_reviewed"] == [] and summary["reviewed"] == 2
    assert any("waited for budget room" in line for line in logs)


def test_candidates_are_split_into_mechanical_and_to_judge(tmp_path):
    split = {"kind": "rule", "s": 2, "e": 2, "source": "검을 들어라", "english": "“Raise your sword.”",
             "fix": "“Raise your sword,” he said.", "why": "speaker"}  # Rewords, so it needs judging too.
    keep_both = {"all": [{"id": 1, "verdict": "keep", "why": "real"}, {"id": 2, "verdict": "keep", "why": "real"}]}
    _, summary = run(tmp_path, FakeClient(findings={1: [ZHANG, split]}, verdicts=keep_both))
    run_dir = (tmp_path / "novel" / "output" / "review" / "0001_0002")
    mechanical = json.loads((run_dir / "candidates_0001_0002_mechanical.json").read_text(encoding="utf-8"))["replacements"]
    to_judge = json.loads((run_dir / "candidates_0001_0002_to_judge.json").read_text(encoding="utf-8"))["replacements"]
    assert mechanical == [] and sorted(c["new"] for c in to_judge) == ["He stepped back two zhang.", "“Raise your sword,” he said."]
    assert summary["mechanical"] == 0 and summary["to_judge"] == 2
