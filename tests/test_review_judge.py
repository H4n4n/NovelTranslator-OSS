"""Judging a review's candidate fixes on a small synthetic project, with a fake provider and no network."""
import json
from types import SimpleNamespace as NS

import review_judge
from production_goat_runtime import GoatBudget
from test_chapter_review import ENGLISH, OFF_PEAK, ZHANG, FakeClient, response, run, zero_network  # noqa: F401


class FakeJudge:
    """Answers judging requests with the given decisions."""

    def __init__(self, decisions=None, finish="stop"):
        self.decisions, self.finish, self.requests = decisions, finish, []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **wire):
        self.requests.append(wire)
        assert wire["messages"][0]["content"] == review_judge.JUDGE_PROMPT
        return response(json.dumps({"decisions": self.decisions}, ensure_ascii=False), self.finish)


def judged(tmp_path, decisions, **options):
    novel, _ = run(tmp_path, FakeClient(findings={1: [ZHANG]}))
    client = FakeJudge(decisions, **options)
    summary = review_judge.judge_review(novel, client, GoatBudget(10, 5.0), run="0001_0002",
                                        clock=OFF_PEAK, log=lambda *_: None)
    approved = json.loads(open(summary["approved_file"], encoding="utf-8").read())["replacements"]
    return novel, client, summary, approved


def test_an_approved_fix_goes_to_the_approved_list_and_no_chapter_changes(tmp_path):
    novel, client, summary, approved = judged(tmp_path, [{"id": 1, "decision": "approve", "reason": "real error"}])
    assert approved == [{"source": ["이 장 밖으로"], "old": "He stepped back out of the yard.",
                         "new": "He stepped back two zhang."}]
    assert summary["approved"] == 1 and summary["calls"] == 1 and not summary["not_judged"]
    user = client.requests[0]["messages"][1]["content"]
    assert "OLD: He stepped back out of the yard." in user and "이 장 is two zhang." in user
    assert "백운검 = White Cloud Sword" in user and client.requests[0]["reasoning_effort"] == "high"
    assert (novel.chapter_dir / "0001.md").read_bytes().decode("utf-8") == ENGLISH[1]


def test_a_rewording_is_used_only_when_it_applies_exactly_once(tmp_path):
    _, _, summary, approved = judged(tmp_path, [{"id": 1, "decision": "reword", "new": "He fell back two zhang.",
                                                 "reason": "clumsy fix"}])
    assert approved[0]["new"] == "He fell back two zhang." and summary["reworded"] == 1


def test_a_rewording_that_changes_nothing_is_rejected(tmp_path):
    _, _, summary, approved = judged(tmp_path, [{"id": 1, "decision": "reword", "new": "He stepped back out of the yard."}])
    assert approved == [] and summary["rejected"] == 1


def test_a_missing_or_cut_off_answer_rejects_its_items(tmp_path):
    _, _, summary, approved = judged(tmp_path, [{"id": 1, "decision": "approve"}], finish="length")
    assert approved == [] and summary["rejected"] == 1
    decisions = json.loads(open(summary["approved_file"].replace("approved_0001_0002", "decisions"),
                                encoding="utf-8").read())
    assert decisions[0]["reason"] == "no decision"


def test_a_rerun_sends_nothing(tmp_path):
    novel, _, first, _ = judged(tmp_path, [{"id": 1, "decision": "reject", "reason": "nitpick"}])
    again = FakeJudge([])
    second = review_judge.judge_review(novel, again, GoatBudget(10, 5.0), run="0001_0002", clock=OFF_PEAK,
                                       log=lambda *_: None)
    assert again.requests == [] and second["rejected"] == first["rejected"] == 1


def test_the_budget_refuses_before_sending(tmp_path):
    novel, _ = run(tmp_path, FakeClient(findings={1: [ZHANG]}))
    client = FakeJudge([{"id": 1, "decision": "approve"}])
    summary = review_judge.judge_review(novel, client, GoatBudget(10, 0.000001), run="0001_0002",
                                        clock=OFF_PEAK, log=lambda *_: None)
    assert client.requests == [] and summary["not_judged"] and summary["judged"] == 0


class SplitJudge(FakeJudge):
    """All reasoning and no decisions for a request with more items than `most`; else approves all."""

    def __init__(self, most):
        super().__init__([])
        self.most = most

    def create(self, **wire):
        self.requests.append(wire)
        items = wire["messages"][1]["content"].count("\nOLD: ")
        if items > self.most:
            return response(None, "length")
        return response(json.dumps({"decisions": [{"id": n, "decision": "approve", "reason": "real"}
                                                  for n in range(1, items + 1)]}))


SWORD = {"kind": "meaning", "s": 2, "e": 2, "source": "검을 들어라", "english": "“Raise your sword.”",
         "fix": "“Lift your sword.”", "why": "verb"}
KEEP_TWO = {"all": [{"id": 1, "verdict": "keep", "why": "real"}, {"id": 2, "verdict": "keep", "why": "real"}]}


def test_an_all_reasoning_answer_is_judged_again_in_two_halves(tmp_path):
    novel, _ = run(tmp_path, FakeClient(findings={1: [ZHANG, SWORD]}, verdicts=KEEP_TWO))
    logs, client = [], SplitJudge(most=1)
    summary = review_judge.judge_review(novel, client, GoatBudget(10, 5.0), run="0001_0002", clock=OFF_PEAK,
                                        log=logs.append)
    assert len(client.requests) == 3 and summary["approved"] == 2 and not summary["not_judged"]
    assert any("two halves" in line for line in logs)
    journal = novel.root / "output" / "review" / "0001_0002" / "judge" / "journal"
    assert any("review_judge_half2" in path.name for path in journal.rglob("*"))
    again = SplitJudge(most=1)
    assert review_judge.judge_review(novel, again, GoatBudget(10, 5.0), run="0001_0002", clock=OFF_PEAK,
                                     log=lambda *_: None)["approved"] == 2 and again.requests == []


def test_items_still_without_an_answer_after_the_split_are_reported_not_judged(tmp_path):
    novel, _ = run(tmp_path, FakeClient(findings={1: [ZHANG]}))
    client = SplitJudge(most=0)
    summary = review_judge.judge_review(novel, client, GoatBudget(10, 5.0), run="0001_0002", clock=OFF_PEAK,
                                        log=lambda *_: None)
    assert len(client.requests) == 1 and summary["judged"] == 0  # One item cannot be split.
    assert summary["not_judged"] and "all reasoning" in summary["not_judged"][0][1]
