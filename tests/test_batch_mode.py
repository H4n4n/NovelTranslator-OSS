"""Source-first batch mode: pure logic, parallel budget, and a zero-network batch
through the real production module. Names are synthetic placeholders."""
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

import batch_runner
import batch_translation as bt
import run_production_engine as launcher
from controlled_trial_execute import _capture_core_runtime, _restore_core_runtime
from novel_project import BatchModeConfig, NovelConfig
from production_goat_runtime import GoatAllowanceExhausted, GoatBudget, install_goat_runtime
from production_provider_contract import PROVIDER_COMMAND_CODE as GOAT
from production_translation_request import build_production_translation_user_prompt
from review_queue import read_items
from test_controlled_trial_plan import _empty_project
from test_production_goat_runtime import CLOCK, response, zero_network  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")

CHAPTERS = [
    {"chapter": 1, "text": "张三走进禁卫营。张三说：“殿下来了。”"},
    {"chapter": 2, "text": "禁卫营改名为龙骧营。李四成为龙骧营统领。"},
    {"chapter": 3, "text": "龙骧营出城。李四向张三行礼。\n“走！”\n“快走！”\n“出发！”"},
    {"chapter": 4, "text": "李四回到城门的龙骧营。"},
]


def sheet(number, terms=(), people=(), events=(), time_marker="", issues=()):
    return {"chapter": number, "terms": list(terms), "people": list(people), "events": list(events),
            "time": time_marker, "issues": list(issues)}


def term(source, english, category="military unit", sentence=""):
    return {"source": source, "english": english, "category": category, "sentence": sentence}


# ---------------------------------------------------------------- pure logic

def test_prepass_keeps_only_literal_source_keys():
    content = json.dumps({"terms": [term("禁卫营", "Imperial Guard Camp", sentence="张三走进禁卫营。"),
                                    term("不存在", "Missing"), term("禁卫营", "Duplicate")],
                          "people": [{"source": "张三", "english": "Zhang San", "status": "weird"}],
                          "events": [{"type": "nonsense", "detail": "Zhang San arrived."}], "issues": ["x"] * 9})
    parsed = bt.parse_prepass("```json\n" + content + "\n```", 1, CHAPTERS[0]["text"])
    assert [item["source"] for item in parsed["terms"]] == ["禁卫营"]
    assert parsed["people"][0]["status"] == "unknown"
    assert parsed["events"][0]["type"] == "other" and len(parsed["issues"]) == 3
    with pytest.raises(bt.BatchResponseError):
        bt.parse_prepass("not json", 1, CHAPTERS[0]["text"])


def test_consolidation_dedupes_and_never_touches_existing_terms():
    sources = {item["chapter"]: item["text"] for item in CHAPTERS[:3]}
    sheets = {1: sheet(1, [term("禁卫营", "Imperial Guard Camp"), term("张三", "Zhang San", "character")]),
              2: sheet(2, [term("禁卫营", "Guards Camp"), term("龙骧营", "Dragon Soar Battalion"), term("虚构", "Fiction")]),
              3: sheet(3, [term("龙骧营", "Dragon Soar Battalion")])}
    groups = bt.consolidate_candidates(sheets, sources, {"张三"})
    by_key = {group["source"]: group for group in groups}
    assert set(by_key) == {"禁卫营", "龙骧营"}  # existing key skipped, invented key dropped
    assert by_key["禁卫营"]["disputed"] and by_key["禁卫营"]["first_chapter"] == 1
    assert by_key["龙骧营"]["first_chapter"] == 2 and by_key["龙骧营"]["chapters_present"] == 2
    assert [group["id"] for group in groups] == ["t001", "t002"]


def test_settle_terms_handles_reject_missing_contextual_and_late_merge():
    groups = [{"id": "t001", "source": "甲营", "first_chapter": 1, "disputed": False,
               "proposals": [{"english": "First Camp", "category": "military unit", "chapters": [1]}]},
              {"id": "t002", "source": "乙营", "first_chapter": 1, "disputed": True,
               "proposals": [{"english": "Second Camp", "category": "military unit", "chapters": [1]}]},
              {"id": "t003", "source": "丙营", "first_chapter": 2, "disputed": False,
               "proposals": [{"english": "Third Camp", "category": "military unit", "chapters": [2]}]},
              {"id": "t004", "source": "丁营", "first_chapter": 2, "disputed": False,
               "proposals": [{"english": "Fourth", "category": "military unit", "chapters": [2]}]}]
    decisions = {"t001": {"action": "merge", "english": "", "category": "", "approved_renderings": [],
                          "merge_with": "丙营", "confidence": "high", "notes": ""},
                 "t003": {"action": "contextual", "english": "", "category": "", "approved_renderings": ["A", "B"],
                          "merge_with": "", "confidence": "high", "notes": ""},
                 "t004": {"action": "reject", "english": "", "category": "", "approved_renderings": [],
                          "merge_with": "", "confidence": "high", "notes": ""}}
    settled = bt.settle_terms(groups, decisions, {})
    assert "丁营" not in settled
    assert settled["甲营"]["merge_with"] == ""  # target is first seen later, so no alias
    assert settled["乙营"]["confidence"] == "low" and settled["乙营"]["english"] == "Second Camp"
    assert settled["丙营"]["status"] == "contextual" and settled["丙营"]["approved_renderings"] == ["A", "B"]
    assert [key for key, _ in bt.flagged_terms(settled)] == ["乙营"]


def test_timeline_gives_each_chapter_the_story_before_it():
    rename = {"type": "rename", "detail": "The Imperial Guard Camp was renamed.", "old": "禁卫营", "new": "龙骧营"}
    sheets = {1: sheet(1, people=[{"source": "张三", "english": "Zhang San", "title": "", "role": "officer",
                                   "location": "camp", "status": "alive"}]),
              2: sheet(2, events=[rename]),
              3: sheet(3, people=[{"source": "张三", "english": "", "title": "", "role": "", "location": "",
                                   "status": "unknown"}])}
    packets, after = bt.build_timeline("Base memory.", sheets, [1, 2, 3])
    assert packets[1] == "Base memory."
    assert "renamed" not in packets[2] and "Zhang San" in packets[2]
    assert "禁卫营 → 龙骧营" in packets[3] and "renamed" in after[2]


def test_memory_trims_passing_events_before_lasting_ones():
    people = {}
    events = [{"type": "move", "detail": "x" * 200, "old": "", "new": "", "chapter": n} for n in range(40)]
    events.append({"type": "death", "detail": "The old general died.", "old": "", "new": "", "chapter": 41})
    text = bt.render_memory("Base.", people, events, first_chapter=1)
    assert len(text) <= bt.CONTEXT_MAX_CHARS and "The old general died." in text


def test_term_checks_and_variant_repair_are_source_anchored():
    settled = {"龙骧营": {"english": "Dragon Soar Battalion", "status": "provisional", "approved_renderings": [],
                          "proposals": ["Dragon Soar Battalion", "Longxiang Camp", "Battalion"]}}
    source = CHAPTERS[2]["text"]
    text = "The Longxiang Camp marched out."
    misses = bt.term_misses(text, source, settled)
    assert misses == ["龙骧营"]
    repaired, repairs = bt.repair_variants(text, source, settled, misses)
    assert repaired == "The Dragon Soar Battalion marched out." and repairs[0]["count"] == 1
    # "Battalion" is part of the settled rendering, so it is never treated as a variant.
    assert bt.repair_variants("The Battalion left.", source, settled, misses)[1] == []
    assert bt.repair_variants(text, "无关的原文", settled, misses)[1] == []


def test_decision_under_the_wrong_id_is_ignored():
    groups = [{"id": "t001", "source": "张三"}, {"id": "t002", "source": "禁卫营"}]
    content = json.dumps({"terms": [{"id": "t001", "source": "禁卫营", "english": "Imperial Guard Camp"},
                                    {"id": "t002", "source": "禁卫营", "english": "Imperial Guard Camp"}]})
    assert set(bt.parse_decision(content, groups)["terms"]) == {"t002"}


def test_state_and_review_come_from_the_batch_decision():
    settled = {"龙骧营": {"english": "Dragon Soar Battalion", "category": "military unit", "status": "provisional",
                          "approved_renderings": [], "merge_with": "", "first_chapter": 2, "notes": ""}}
    state = bt.state_for_chapter(2, settled, "Memory after 2.", sheet(2, issues=["unclear glyph"]))
    assert state["new_terms"][0]["chinese"] == "龙骧营" and state["continuity_summary"] == "Memory after 2."
    assert bt.state_for_chapter(3, settled, "m", None)["new_terms"] == []
    review = bt.review_for_candidates([{"chinese": "龙骧营", "candidate_id": "source:x", "english": "Other"}],
                                      settled, set(), lambda candidate: candidate["candidate_id"])
    assert review["decisions"][0] == {"candidate_id": "source:x", "action": "accept",
                                      "english": "Dragon Soar Battalion", "category": "military unit",
                                      "notes": "Settled for the batch from the source."}


def test_prompt_without_source_tail_is_unchanged():
    arguments = dict(chapter_numbering="global", source_language="zh", target_language="en",
                     glossary_entries={}, profile=NS(high_priority_categories=()), continuity_summary="m")
    plain = build_production_translation_user_prompt(CHAPTERS[1], **arguments)
    assert build_production_translation_user_prompt(CHAPTERS[1], previous_source_tail=None, **arguments) == plain
    with_tail = build_production_translation_user_prompt(CHAPTERS[1], previous_source_tail="张三说话。", **arguments)
    assert "END OF THE PREVIOUS CHAPTER" in with_tail and with_tail.endswith(CHAPTERS[1]["text"])


def test_batch_mode_is_opt_in_and_validated():
    base = dict(id="fixture", title="T", source_language="zh", target_language="en",
                profile="historical_military", final_chapter=3)
    assert NovelConfig.from_dict(base).batch_mode.enabled is False
    assert "batch_mode" not in NovelConfig.from_dict(base).to_dict()
    config = NovelConfig.from_dict({**base, "batch_mode": {"enabled": True, "chapters": 20}})
    assert config.batch_mode == BatchModeConfig(enabled=True, chapters=20, max_parallel=32)
    for bad in ({"enabled": "yes"}, {"chapters": 0}, {"speed": 2}):
        with pytest.raises(ValueError):
            NovelConfig.from_dict({**base, "batch_mode": bad})


# ---------------------------------------------------------------- parallel budget

def wire(max_completion=50_000):
    return {"model": "deepseek/deepseek-v4.1-flash", "messages": [{"role": "user", "content": "x"}],
            "max_completion_tokens": max_completion, "reasoning_effort": "low"}


def test_budget_reserves_each_request_in_flight_until_accounted():
    started = CLOCK().isoformat()
    bound = GoatBudget(10, 1.0).request_bound(wire(), started)["credit_equivalent_upper"]
    budget = GoatBudget(10, bound * 1.5)
    holding, release = threading.Event(), threading.Event()

    def in_flight():
        with budget.authorize(wire(), started):
            budget.begin("a" * 64)
        holding.set()
        release.wait(5)
        budget.account({"cost_usd": 0.001, "prompt_tokens": 10, "completion_tokens": 10})

    worker = threading.Thread(target=in_flight)
    worker.start()
    assert holding.wait(5)
    assert budget.reserved_usd == pytest.approx(bound)
    with pytest.raises(GoatAllowanceExhausted):
        with budget.authorize(wire(), started):
            pass
    release.set()
    worker.join(5)
    assert budget.reserved_usd == pytest.approx(0) and budget.actual_cost_usd == pytest.approx(0.001)
    with budget.authorize(wire(), started):
        pass  # Room again once the first request is accounted; an unsent authorization holds nothing.
    assert budget.reserved_usd == pytest.approx(0) and budget.records[0]["status"] == "success"


def test_definite_failure_releases_its_reservation():
    budget = GoatBudget(10, 1.0)
    with budget.authorize(wire(), CLOCK().isoformat()):
        budget.begin("b" * 64)
        budget.retract()
    assert budget.reserved_usd == pytest.approx(0) and budget.records == [] and budget.api_calls == 0


# ---------------------------------------------------------------- through the production module

class FakeProvider:
    """Answers batch requests by kind; any state extraction or review request fails the test."""

    def __init__(self, module):
        self.module = module
        self.sends = []
        self.lock = threading.Lock()
        self.active = {}
        self.max_active = {}
        self.reject_prepass = set()
        self.cut_off_prepass = set()
        self.reasoning_only_prepass = set()
        self.explode_prepass = set()
        self.audit_answers = {}  # source key -> audit decision; every other target is kept
        self.cut_off_once = set()
        self.decision = {"terms": [], "headings": [], "continuity": "Memory after the batch."}

    def kind(self, body):
        system = body["messages"][0]["content"]
        if system.startswith(bt.PREPASS_PROMPT[:60]):
            return "prepass"
        if system.startswith(bt.DECISION_PROMPT[:60]):
            return "decision"
        if system == self.module.AUDIT_MODEL_PROMPT:
            return "audit"
        if body.get("max_completion_tokens") == 50_000:
            return "translation"
        return "other"

    def chapter_of(self, body):
        user = body["messages"][-1]["content"]
        return next(item["chapter"] for item in reversed(CHAPTERS) if item["text"] in user)

    def create(self, **body):
        kind = self.kind(body)
        with self.lock:
            self.sends.append((kind, body))
            for name in (kind, "all"):
                self.active[name] = self.active.get(name, 0) + 1
                self.max_active[name] = max(self.max_active.get(name, 0), self.active[name])
        try:
            time.sleep(0.02)
            if kind == "prepass":
                number = self.chapter_of(body)
                if number in self.reject_prepass:
                    return response(content="not json at all")
                if number in self.cut_off_prepass:
                    return response(content='{"terms": [', finish="length", completion=16_000)
                if number in self.explode_prepass:
                    raise RuntimeError("connection dropped")
                if number in self.reasoning_only_prepass:  # as in the first paid rehearsal
                    return response(content=None, finish="length", completion=64_000, reasoning=64_000)
                return response(content=json.dumps(PREPASS[number], ensure_ascii=False))
            if kind == "decision":
                return response(content=json.dumps(self.decision, ensure_ascii=False))
            if kind == "audit":
                user = body["messages"][-1]["content"]
                targets = json.loads(user[user.index("AUDIT TARGETS:") + 14:user.index("STRUCTURED FAMILY CONTEXT:")])
                return response(content=json.dumps({"decisions": [
                    {"candidate_id": item["candidate_id"], **self.audit_answers.get(item["source_key"], {"action": "keep"})}
                    for item in targets], "issues": []}, ensure_ascii=False))
            if kind == "translation":
                number = self.chapter_of(body)
                if number in self.cut_off_once:
                    self.cut_off_once.discard(number)
                    return response(content="Chapter", finish="length", completion=50_000)
                return response(content=TRANSLATIONS[number])
            if "new_terms" in body["messages"][0]["content"]:  # sequential state extraction
                return response(content=json.dumps({"new_terms": [], "continuity_summary": "Sequential memory.",
                                                    "issues": []}))
            return response(content='{"decisions": [], "issues": []}')
        finally:
            with self.lock:
                self.active[kind] -= 1
                self.active["all"] -= 1

    def count(self, kind):
        return sum(1 for item, _ in self.sends if item == kind)


PREPASS = {
    1: {"terms": [term("禁卫营", "Imperial Guard Camp", sentence="张三走进禁卫营。"),
                  term("张三", "Zhang San", "character")],
        "people": [{"source": "张三", "english": "Zhang San", "role": "officer", "location": "the camp",
                    "status": "alive"}], "events": [], "time": "", "issues": []},
    2: {"terms": [term("龙骧营", "Dragon Soar Battalion"), term("李四", "Li Si", "character")],
        "people": [{"source": "李四", "english": "Li Si", "title": "Commander", "status": "alive"}],
        "events": [{"type": "rename", "detail": "The Imperial Guard Camp was renamed the Dragon Soar Battalion.",
                    "old": "禁卫营", "new": "龙骧营"}], "time": "", "issues": []},
    3: {"terms": [term("龙骧营", "Longxiang Camp")], "people": [], "events": [], "time": "", "issues": []},
    4: {"terms": [term("城门", "City Gate", "location")], "people": [], "events": [], "time": "", "issues": []},
}
TRANSLATIONS = {
    1: "Chapter 1\n\nZhang San walked into the Imperial Guard Camp. Zhang San said, “His Highness is here.”",
    2: "Chapter 2\n\nThe Imperial Guard Camp was renamed the Longxiang Camp. Li Si became its commander.",
    3: "Chapter 3\n\nThe Dragon Soar Battalion left the city. Li Si saluted Zhang San.",
    4: "Chapter 4\n\nLi Si returned to the Dragon Soar Battalion at the City Gate.",
}


@pytest.fixture
def batch_engine(tmp_path, monkeypatch, request):
    """Overrides come from indirect parametrization: final_chapter, batch_mode, off_peak_only."""
    from conftest import install_synthetic_goat_hard_bound
    install_synthetic_goat_hard_bound(monkeypatch, tmp_path)
    project = _empty_project(tmp_path)
    overrides = dict(final_chapter=3, batch_mode=BatchModeConfig(enabled=True, chapters=3, max_parallel=4))
    overrides.update(getattr(request, "param", {}))
    config = replace(project.config, **overrides)
    project.config_file.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    project.chapters_file.write_text(json.dumps(CHAPTERS[:config.final_chapter], ensure_ascii=False), encoding="utf-8")
    before = _capture_core_runtime()
    monkeypatch.chdir(project.root)
    launcher.prepare_production_runtime("v41-flash", project.pending_dir, provider_selection=GOAT)
    module = launcher.load_production_module()
    monkeypatch.setattr(module, "load_dotenv", lambda: pytest.fail("dotenv forbidden"))
    monkeypatch.setattr(module, "OpenAI", lambda *a, **k: pytest.fail("direct client forbidden"))
    module.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
    provider = FakeProvider(module)
    budget = GoatBudget(100, 2.0)
    install_goat_runtime(module, provider_client=NS(chat=NS(completions=NS(create=provider.create))),
                         budget=budget, clock=CLOCK)
    # Ids follow first chapter, then source key order.
    provider.decision["terms"] = [
        {"id": "t001", "source": "张三", "action": "accept", "english": "Zhang San", "category": "character",
         "confidence": "high"},
        {"id": "t002", "source": "禁卫营", "action": "accept", "english": "Imperial Guard Camp",
         "category": "military unit", "confidence": "high"},
        {"id": "t003", "source": "李四", "action": "accept", "english": "Li Si", "category": "character",
         "confidence": "high"},
        {"id": "t004", "source": "龙骧营", "action": "accept", "english": "Dragon Soar Battalion",
         "category": "military unit", "confidence": "low"}]
    yield project, module, provider, budget
    _restore_core_runtime(before)


def run_all(module):
    for number in (1, 2, 3):
        assert module.process_chapter_after_batch_preparation(number) is True


def test_batch_translates_in_parallel_and_commits_in_order(batch_engine):
    project, module, provider, budget = batch_engine
    run_all(module)
    assert provider.count("prepass") == 3 and provider.count("decision") == 1
    assert provider.count("translation") == 3 and provider.count("other") == 0  # no state or review requests
    assert module.progress["completed"] == [1, 2, 3]

    # Chapter 2 learned the rename from the source before chapter 1 was translated.
    second = next(body for kind, body in provider.sends
                  if kind == "translation" and provider.chapter_of(body) == 2)["messages"][-1]["content"]
    assert "Dragon Soar Battalion" in second and "END OF THE PREVIOUS CHAPTER" in second
    third = next(body for kind, body in provider.sends
                 if kind == "translation" and provider.chapter_of(body) == 3)["messages"][-1]["content"]
    assert "禁卫营 → 龙骧营" in third

    entries = module.glossary["entries"]
    assert entries["龙骧营"]["english"] == "Dragon Soar Battalion" and entries["龙骧营"]["first_chapter"] == 2
    assert entries["禁卫营"]["first_chapter"] == 1
    assert module.continuity["summary"] == "Memory after the batch."

    # The settled-term check replaced chapter 2's stray variant before commit.
    assert "Dragon Soar Battalion" in (project.chapter_dir / "0002.md").read_text(encoding="utf-8")
    flags = {(item["chapter"], item["kind"]) for item in read_items(project.data_dir)}
    assert (2, "batch_term") in flags  # low-confidence decision flagged, run continued
    manifest = batch_runner.manifests(project.data_dir)[0]
    assert manifest["status"] == batch_runner.COMPLETE and set(manifest["timings"]) >= {"prepass", "translation"}
    assert budget.reserved_usd == pytest.approx(0)
    assert batch_runner.uncommitted_batch_cost(project.data_dir, 3) == 0


def test_restart_reuses_every_paid_batch_answer(batch_engine, monkeypatch):
    project, module, provider, _ = batch_engine
    real = batch_runner._run_translation
    monkeypatch.setattr(batch_runner, "_run_translation", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        module.process_chapter_after_batch_preparation(1)
    assert provider.count("prepass") == 3 and provider.count("decision") == 1
    monkeypatch.setattr(batch_runner, "_run_translation", real)
    assert module.process_chapter_after_batch_preparation(1) is True
    assert provider.count("prepass") == 3 and provider.count("decision") == 1 and provider.count("translation") == 3
    # Crash after chapter 1 committed: chapters 2-3 commit from their recorded translations.
    assert batch_runner.uncommitted_batch_cost(project.data_dir, 1) > 0
    for number in (2, 3):
        assert module.process_chapter_after_batch_preparation(number) is True
    assert provider.count("translation") == 3


def test_unusable_first_prepass_falls_back_to_the_sequential_path(batch_engine):
    project, module, provider, _ = batch_engine
    provider.reject_prepass = {1}
    assert module.process_chapter_after_batch_preparation(1) is True
    assert provider.count("prepass") == 1  # the warm-up request failed, so the rest were never sent
    assert batch_runner.manifests(project.data_dir)[0]["status"] == batch_runner.ABANDONED
    assert provider.count("other") >= 1  # chapter 1 read its state the sequential way
    assert (1, "batch_prepass") in {(item["chapter"], item["kind"]) for item in read_items(project.data_dir)}


def test_unusable_later_prepass_shortens_the_batch(batch_engine):
    project, module, provider, _ = batch_engine
    provider.reject_prepass = {3}
    run_all(module)
    manifests = {item["start"]: item for item in batch_runner.manifests(project.data_dir)}
    assert manifests[1]["end"] == 2 and manifests[1]["status"] == batch_runner.COMPLETE
    assert 3 not in manifests  # a batch of one is just the sequential path
    assert module.progress["completed"] == [1, 2, 3]


@pytest.mark.parametrize("allowance, widest", [(2.0, 2), (0.1, 1)])
def test_requests_in_flight_fit_the_remaining_budget(batch_engine, allowance, widest):
    project, module, provider, budget = batch_engine
    budget.max_cost_usd = allowance  # 0.1 holds one translation's worst case at peak rates, not two
    run_all(module)
    assert provider.max_active["translation"] <= widest
    assert module.progress["completed"] == [1, 2, 3]


def test_batch_mode_off_leaves_the_sequential_path_alone(batch_engine):
    project, module, provider, _ = batch_engine
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, batch_mode=BatchModeConfig())
    assert module.process_chapter_after_batch_preparation(1) is True
    assert provider.count("prepass") == 0 and not (project.data_dir / "batch").exists()


def test_failed_batch_translation_is_sent_again_with_the_batch_terms(batch_engine):
    project, module, provider, _ = batch_engine
    provider.cut_off_once = {2}
    run_all(module)
    assert module.progress["completed"] == [1, 2, 3] and provider.count("translation") == 4
    retry = [body for kind, body in provider.sends if kind == "translation" and provider.chapter_of(body) == 2][-1]
    assert "Dragon Soar Battalion" in retry["messages"][-1]["content"]  # first seen in this very chapter
    assert list((project.root / "backup" / "superseded_translations").rglob("0002.chapter_translation.json"))
    assert (2, "batch_translation") in {(item["chapter"], item["kind"]) for item in read_items(project.data_dir)}
    assert batch_runner.manifests(project.data_dir)[0]["status"] == batch_runner.COMPLETE


def test_glossary_correction_after_batch_translation_reaches_later_chapters(batch_engine):
    project, module, provider, _ = batch_engine
    assert module.process_chapter_after_batch_preparation(1) is True
    module.glossary["entries"]["禁卫营"]["english"] = "Palace Guard Camp"  # as a periodic audit would
    assert module.process_chapter_after_batch_preparation(2) is True
    text = (project.chapter_dir / "0002.md").read_text(encoding="utf-8")
    assert "Palace Guard Camp" in text and "Imperial Guard Camp" not in text


def test_unattended_lifecycle_runs_the_batch(batch_engine):
    project, module, provider, _ = batch_engine
    (project.data_dir / "trial_stop.json").write_text(json.dumps({"after_chapter": 3}), encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        module.run_unattended_production()
    assert stopped.value.code == 0 and module.progress["completed"] == [1, 2, 3]
    assert provider.count("prepass") == 3 and provider.count("translation") == 3


# ---------------------------------------------------------------- rehearsal comparison

def test_rehearsal_prepare_and_compare(tmp_path):
    import batch_rehearsal_compare as rehearsal
    root = tmp_path / "rehearsal"
    project = _empty_project(root)
    project.chapters_file.write_text(json.dumps(CHAPTERS, ensure_ascii=False), encoding="utf-8")
    novels, out = root / "novels", root / "batch_rehearsal"
    with pytest.raises(SystemExit):
        rehearsal.prepare(novels, "fixture", tmp_path / "elsewhere", 3)
    assert set(rehearsal.prepare(novels, "fixture", out, 3)["arms"]) == {"seq", "batch"}
    batch_config = json.loads((rehearsal.arm_root(out, "batch", "fixture") / "novel.json").read_text(encoding="utf-8"))
    assert batch_config["batch_mode"]["enabled"] and batch_config["id"] == "fixture"
    assert "batch_mode" not in json.loads((rehearsal.arm_root(out, "seq", "fixture") / "novel.json").read_text(encoding="utf-8"))
    unit = {"english": "Dragon Soar Battalion", "category": "military unit", "first_chapter": 2}
    for arm, third in (("seq", "The Royal Guards left."), ("batch", TRANSLATIONS[3])):
        arm_root = rehearsal.arm_root(out, arm, "fixture")
        for number, text in ((1, TRANSLATIONS[1]), (2, TRANSLATIONS[2].replace("Longxiang Camp", "Dragon Soar Battalion")),
                             (3, third)):
            (arm_root / "output" / "chapters" / f"{number:04d}.md").write_text(text, encoding="utf-8")
        (arm_root / "data" / "glossary.json").write_text(json.dumps({"entries": {"龙骧营": unit}}), encoding="utf-8")
        (arm_root / "data" / "progress.json").write_text(json.dumps(
            {"completed": [1, 2, 3], "last_completed": 3, "next_chapter": 4, "rate_correct_cost_usd": 0.06}),
            encoding="utf-8")
    report = rehearsal.compare(out, "fixture")
    assert report["arms"]["seq"]["own_term_consistency"]["rate"] == 0.5
    assert report["arms"]["batch"]["own_term_consistency"]["rate"] == 1.0
    assert report["arms"]["batch"]["cost_per_chapter_usd"] == 0.02
    written = rehearsal.write_report(report, out / "reports" / "batch_rehearsal_fixture")
    assert "| Term consistency (own glossary) | 1/2 (50%) | 2/2 (100%) |" in written.read_text(encoding="utf-8")


def test_crash_after_pending_is_saved_recovers_like_a_sequential_pending(batch_engine, monkeypatch):
    from production_provider_runtime import inspect_pending_provider, provider_identity
    project, module, provider, _ = batch_engine
    assert module.process_chapter_after_batch_preparation(1) is True
    real = batch_runner.bt.state_for_chapter
    monkeypatch.setattr(batch_runner.bt, "state_for_chapter", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        module.process_chapter(2)
    assert (project.pending_dir / "0002.md").is_file()
    inspect_pending_provider(project.pending_dir, provider_identity(GOAT, "v41-flash"))  # what recovery checks
    monkeypatch.setattr(batch_runner.bt, "state_for_chapter", real)
    sent = len(provider.sends)
    assert module.process_chapter(2) is True and module.process_chapter_after_batch_preparation(3) is True
    assert len(provider.sends) == sent and module.progress["completed"] == [1, 2, 3]


def test_crash_before_the_batch_is_marked_complete(batch_engine, monkeypatch):
    project, module, provider, _ = batch_engine
    real = batch_runner._finish_if_committed
    monkeypatch.setattr(batch_runner, "_finish_if_committed", lambda *a: None)
    run_all(module)
    assert batch_runner.active_manifest(project.data_dir)["status"] == batch_runner.COMMITTING
    monkeypatch.setattr(batch_runner, "_finish_if_committed", real)
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, final_chapter=4)
    assert batch_runner.prepare_batch_if_due(module, 4) is None
    assert batch_runner.active_manifest(project.data_dir) is None


def test_a_cut_off_helper_answer_is_still_counted(batch_engine):
    project, module, provider, _ = batch_engine
    provider.cut_off_prepass = {2}
    run_all(module)
    accounted = module.progress["accounted_api_operation_ids"]
    assert "batch_source_prepass:1:2" in accounted and "batch_source_prepass:1:3" in accounted
    manifests = {item["start"]: item for item in batch_runner.manifests(project.data_dir)}
    assert manifests[1]["end"] == 1  # the batch stops before the chapter it could not read
    assert module.progress["completed"] == [1, 2, 3]


def test_reasoning_only_helper_answer_is_counted_and_does_not_block_the_run(batch_engine):
    project, module, provider, budget = batch_engine
    provider.reasoning_only_prepass = {1}
    assert module.process_chapter_after_batch_preparation(1) is True  # sequential fallback still sends
    assert not budget.blocked and not budget.unaccounted_provider_response
    assert "batch_source_prepass:1:1" in module.progress["accounted_api_operation_ids"]
    assert batch_runner.manifests(project.data_dir)[0]["status"] == batch_runner.ABANDONED


def test_one_character_keys_never_become_batch_terms():
    sources = {1: "원래 그는 원을 그렸다."}
    groups = bt.consolidate_candidates({1: sheet(1, [term("원", "Circle"), term("그는", "he")])}, sources, set())
    assert [group["source"] for group in groups] == ["그는"]
    settled = {"원": {"english": "Circle", "status": "provisional", "approved_renderings": []}}
    assert bt.term_misses("He drew a ring.", sources[1], settled) == []


def test_retranslation_needs_a_large_share_of_misses():
    entry = lambda english: {"english": english, "status": "provisional", "approved_renderings": []}
    settled = {f"용어{index}": entry(f"Term {index}") for index in range(12)}
    source = " ".join(settled)
    assert not bt.needs_retranslation(["용어0", "용어1", "용어2"], source, settled)  # 3 of 12
    assert bt.needs_retranslation(["용어0", "용어1", "용어2"], "용어0 용어1 용어2 용어3", settled)  # 3 of 4


def test_common_words_and_variants_are_marked_for_the_decision():
    sources = {n: f"기사가 왔다. 철사자 성{'' if n % 2 else '과 철사자성'}." for n in range(1, 7)}
    sheets = {n: sheet(n, [term("기사들", "knight", "title"), term("철사자 성", "Iron Lion Castle", "location"),
                           term("철사자성", "Iron Lion Castle", "location")] if n == 2 else [])
              for n in range(1, 7)}
    for item in sheets.values():
        item["terms"] = [{**entry, "source": bt.base_form(entry["source"])} for entry in item["terms"]]
    groups = {group["source"]: group for group in bt.consolidate_candidates(sheets, sources, {"성채": {"english": "Iron Lion Castle"}})}
    assert groups["기사"]["widespread"] and not groups["철사자성"]["widespread"]
    assert set(groups["철사자 성"]["same_english_as"]) == {"철사자성", "성채"}
    request = bt.build_decision_request(list(groups.values()), model="m", policy="", established_glossary="",
                                        changes=[])["messages"][1]["content"]
    assert '"widespread": true' in request and '"same_english_as"' in request
    settled = bt.settle_terms(list(groups.values()), {}, {})
    assert settled["철사자성"]["merge_with"] == "철사자 성"  # spacing variant of the more widespread form


def test_plural_marker_is_dropped_from_keys():
    assert bt.base_form("레인저들") == "레인저" and bt.base_form("士兵们") == "士兵" and bt.base_form("들") == "들"
    parsed = bt.parse_prepass(json.dumps({"terms": [term("레인저들", "rangers")]}), 1, "레인저들이 왔다.")
    assert parsed["terms"][0]["source"] == "레인저"


def test_terms_only_rehearsal_settles_terms_and_never_translates(batch_engine):
    project, module, provider, _ = batch_engine
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, batch_mode=BatchModeConfig(
        enabled=True, chapters=3, max_parallel=4, terms_only=True))
    for attempt in range(2):  # a restart sends nothing more and still does not translate
        with pytest.raises(batch_runner.BatchTermsOnlyStop):
            module.process_chapter_after_batch_preparation(1)
    assert provider.count("prepass") == 3 and provider.count("decision") == 1
    assert provider.count("translation") == 0 and provider.count("other") == 0
    assert module.progress["completed"] == []
    settled = json.loads((project.data_dir / "batch" / "0001" / "settled.json").read_text(encoding="utf-8"))
    assert settled["settled"]["龙骧营"]["english"] == "Dragon Soar Battalion"
    assert batch_runner.manifests(project.data_dir)[0]["status"] == batch_runner.TERMS_ONLY


def test_term_selection_report():
    import batch_rehearsal_compare as rehearsal
    settled = {"龙骧营": {"english": "Dragon Soar Battalion", "status": "provisional", "approved_renderings": [],
                          "first_chapter": 2},
               "李四": {"english": "Li Si", "status": "provisional", "approved_renderings": [], "first_chapter": 2},
               "王国": {"english": "kingdom", "status": "provisional", "approved_renderings": [], "first_chapter": 3}}
    reference = {"龙骧营": {"english": "Dragon Soar Battalion", "first_chapter": 2},
                 "李四": {"english": "Lee Si", "first_chapter": 2}, "旧词": {"english": "Old", "first_chapter": 1}}
    result = rehearsal.term_selection(settled, reference, 2, 3)
    assert (result["batch_terms"], result["sequential_terms"], result["shared"], result["same_english_on_shared"]) == (3, 2, 2, 1)
    assert result["batch_only"] == ["王国"] and result["different_english"][0][0] == "李四"



def pool_fixture(monkeypatch, delays, max_parallel=2, allowance=10.0, bound=0.01):
    """A fake module whose requests take the given seconds; records start and end times."""
    times, lock = {}, threading.Lock()
    origin = time.perf_counter()

    def journaled(kind, number, source, kwargs, journal_path=None):
        with lock:
            times[number] = [time.perf_counter() - origin, None]
        time.sleep(delays[number])
        with lock:
            times[number][1] = time.perf_counter() - origin
        return {"response": {"content": "ok", "finish_reason": "stop", "stats": {"cost_usd": 0.0}}}

    module = NS(_production_goat_runtime={"budget": GoatBudget(100, allowance)},
                chapter_map={number: {"text": "x"} for number in delays}, journaled_chat_request=journaled)
    monkeypatch.setattr(batch_runner, "_bound", lambda *a: bound)
    batch = NS(manifest={"max_parallel": max_parallel})
    jobs = [(number, "chapter_translation", {}, f"slot{number}") for number in delays]
    return module, batch, jobs, times


def test_a_slow_request_holds_up_only_itself(monkeypatch):
    module, batch, jobs, times = pool_fixture(monkeypatch, {1: 0.01, 2: 0.6, 3: 0.05, 4: 0.05, 5: 0.05})
    results, stop = batch_runner._send_all(module, batch, jobs, logical="translation", head_start=0)
    assert stop is None and set(results) == {1, 2, 3, 4, 5}
    assert max(times[n][1] for n in (3, 4, 5)) < times[2][1]  # all finished while 2 was still running


def test_the_rest_start_before_the_warm_up_answer(monkeypatch):
    module, batch, jobs, times = pool_fixture(monkeypatch, {1: 0.5, 2: 0.01, 3: 0.01}, max_parallel=3)
    batch_runner._send_all(module, batch, jobs, logical="translation", head_start=0.1)
    assert times[2][0] < times[1][1] and times[2][0] >= 0.09


def test_pool_never_holds_more_than_the_budget_covers(monkeypatch):
    module, batch, jobs, times = pool_fixture(monkeypatch, {n: 0.05 for n in range(1, 7)}, max_parallel=6,
                                              allowance=0.025, bound=0.01)
    batch_runner._send_all(module, batch, jobs, logical="translation", head_start=0)
    spans = sorted(times.values())
    assert max(sum(1 for start, end in spans if start <= moment < end) for moment, _ in spans) <= 2


def test_exact_duplicates_become_aliases():
    def group(key, english, first, present, category="military unit"):
        return {"id": key, "source": key, "first_chapter": first, "chapters_present": present, "disputed": False,
                "proposals": [{"english": english, "category": category, "chapters": [first]}]}
    groups = [group("겨울기사단", "Winter Knight Order", 2, 5), group("겨울의 기사", "Winter Knight Order", 3, 2),
              group("철사단", "Iron Division", 2, 1), group("철의 사단", "Iron Division", 4, 1),
              group("검호", "Sword Master", 2, 3, "title")]
    settled = bt.settle_terms(groups, {}, {"강철사단": {"english": "Iron Division", "category": "military unit"}})
    assert settled["겨울의 기사"]["merge_with"] == "겨울기사단"  # the more widespread form
    assert settled["철사단"]["merge_with"] == settled["철의 사단"]["merge_with"] == "강철사단"  # an existing entry wins
    assert settled["겨울기사단"]["merge_with"] == "" and settled["검호"]["merge_with"] == ""


def test_contained_names_and_shared_endings_reach_the_decision():
    sources = {n: "궤른 가문의 3군단과 북부군단, 서부군단, 레인저 군단." for n in range(1, 4)}
    sheets = {1: sheet(1, [term("궤른 가문", "House Quern", "faction"), term("3군단", "3rd Corps"),
                           term("북부군단", "Northern Corps"), term("서부군단", "Western Legion"),
                           term("레인저 군단", "Ranger Legion")]),
              2: sheet(2), 3: sheet(3)}
    existing = {"궤른": {"english": "Gwern", "category": "character"}}
    groups = bt.consolidate_candidates(sheets, sources, existing)
    house = next(group for group in groups if group["source"] == "궤른 가문")
    assert house["contains"] == [{"source": "궤른", "english": "Gwern"}]
    endings = bt.shared_endings(groups, existing)
    assert len(endings) == 1 and endings[0].startswith("군단:") and "corp" in endings[0] and "legion" in endings[0]
    request = bt.build_decision_request(groups, model="m", policy="", established_glossary="", changes=[],
                                        endings=endings)["messages"][1]["content"]
    assert "SHARED ENDINGS:\n- 군단:" in request and '"contains"' in request


def test_shared_name_conflicts_are_found_after_the_decision():
    entry = lambda english, first=1: {"english": english, "category": "faction", "status": "provisional",
                                      "approved_renderings": [], "merge_with": "", "first_chapter": first}
    settled = {"궤른 가문": entry("House Quern", 2), "3군단": entry("3rd Corps"), "서부군단": entry("Western Legion"),
               "북부군단": entry("Northern Corps"), "레인저 군단": entry("Ranger Legion", 3)}
    conflicts = bt.shared_name_conflicts(settled, {"궤른": {"english": "Gwern"}})
    details = [item["detail"] for item in conflicts]
    assert "궤른 가문 → House Quern does not use 궤른 → Gwern" in details
    assert any(detail.startswith("군단 is rendered as corp, legion") for detail in details)
    names = {"테일하임": entry("Tailheim"), "멘델하임": entry("Mendelheim"), "바르하임": entry("Barheim")}
    assert bt.shared_name_conflicts(names, {}) == []  # names that merely share an ending



def test_a_chapter_missing_lines_of_speech_is_translated_once_more(batch_engine):
    project, module, provider, _ = batch_engine
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, source_language="ko")
    run_all(module)
    state = batch_runner.manifests(project.data_dir)[0]["chapters"]["3"]
    assert state["set_aside"] == "batch_missing_dialogue" and state["retranslation"] == "done"
    assert state["missing_dialogue"] == 3  # the fake answers the same way twice, so it stays flagged
    assert provider.count("translation") == 4 and module.progress["completed"] == [1, 2, 3]
    assert (3, "possible_missing_dialogue") in {(item["chapter"], item["kind"]) for item in read_items(project.data_dir)}


def test_dialogue_gap_counts_korean_only(batch_engine):
    _, module, _, _ = batch_engine
    source, english = "“가!”\n“빨리!”", "Go, quickly."
    assert batch_runner.dialogue_gap(module, source, english) == 0  # this fixture novel is Chinese
    module.ACTIVE_NOVEL_CONFIG = replace(module.ACTIVE_NOVEL_CONFIG, source_language="ko")
    assert batch_runner.dialogue_gap(module, source, english) == 2


# ---------------------------------------------------------------- overlapping batches and off-peak

OVERLAP = {"final_chapter": 4, "batch_mode": BatchModeConfig(enabled=True, chapters=2, max_parallel=4, overlap=True)}


def run_events(project):
    events = []
    for path in sorted((project.root / "logs").glob("*.jsonl")):
        events += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return events


@pytest.mark.parametrize("batch_engine", [OVERLAP], indirect=True)
def test_the_next_batch_is_prepared_while_this_one_is_saved(batch_engine):
    project, module, provider, budget = batch_engine
    for number in (1, 2, 3, 4):
        assert module.process_chapter_after_batch_preparation(number) is True
    assert module.progress["completed"] == [1, 2, 3, 4] and not any(batch_runner._AHEAD_RUNS.values())
    batches = {item["start"]: item for item in batch_runner.manifests(project.data_dir)}
    assert set(batches) == {1, 3} and batches[3]["ahead_of"] == 1
    assert {item["status"] for item in batches.values()} == {batch_runner.COMPLETE}
    events = [(event["event"], event.get("chapter")) for event in run_events(project)]
    # Batch 3 starts once batch 1's terms are audited, before batch 1 is translated.
    assert events.index(("batch_ahead_started", 3)) < events.index(("batch_ready", 1))
    assert provider.max_active["all"] <= 4  # both batches share one limit of max_parallel
    # Batch 1's terms are established for batch 3: never decided again, used as settled.
    base = json.loads((project.data_dir / "batch" / "0003" / "base_glossary.json").read_text(encoding="utf-8"))
    assert base["龙骧营"]["english"] == "Dragon Soar Battalion"
    decision = json.loads((project.data_dir / "batch" / "0003" / "requests" / "decision_00.json").read_text(encoding="utf-8"))
    assert '"source": "龙骧营"' not in decision["messages"][1]["content"]
    third = next(body for kind, body in provider.sends if kind == "translation" and provider.chapter_of(body) == 3)
    assert "Dragon Soar Battalion" in third["messages"][-1]["content"]
    assert batches[3]["base_continuity"] == "Memory after the batch."  # batch 1's end-of-batch memory
    # Batch 3's helper costs were counted by the main thread, not written by the background one.
    assert "batch_source_prepass:3:3" in module.progress["accounted_api_operation_ids"]
    assert module.glossary["entries"]["城门"]["first_chapter"] == 4


@pytest.mark.parametrize("batch_engine", [OVERLAP], indirect=True)
def test_no_batch_is_prepared_past_the_trial(batch_engine):
    project, module, provider, _ = batch_engine
    (project.data_dir / "trial_stop.json").write_text(json.dumps({"after_chapter": 2}), encoding="utf-8")
    for number in (1, 2):
        assert module.process_chapter_after_batch_preparation(number) is True
    assert [item["start"] for item in batch_runner.manifests(project.data_dir)] == [1]
    assert provider.count("prepass") == 2


@pytest.mark.parametrize("batch_engine", [OVERLAP], indirect=True)
def test_an_unanswered_request_ahead_stops_the_run_safely(batch_engine):
    """A request with no recorded answer blocks the shared budget, as in a sequential run.

    The batch ahead now reads its source while this batch translates, so this batch's
    remaining requests are refused too, or, if it finished first, the error surfaces
    when the batch ahead is due. Either way nothing past the stop is saved, refused
    requests were never sent, and the unanswered one waits for its one recorded resend.
    """
    from production_goat_runtime import GoatRuntimeError
    project, module, provider, _ = batch_engine
    provider.explode_prepass = {3}
    with pytest.raises(GoatRuntimeError):
        for number in (1, 2, 3):
            module.process_chapter_after_batch_preparation(number)
    batch_runner.finish_preparation_ahead(module)
    assert module.progress["completed"] in ([], [1, 2]) and not any(batch_runner._AHEAD_RUNS.values())
    record = json.loads((project.data_dir / "provider_request_journal" / "0003.batch_source_prepass_b0003.json")
                        .read_text(encoding="utf-8"))
    assert record["status"] == "uncertain"


@pytest.mark.parametrize("batch_engine", [{"off_peak_only": True}], indirect=True)
def test_off_peak_only_makes_command_code_runs_wait_for_off_peak(batch_engine):
    from noveltranslator_core import api_permission_blocker
    _, module, _, _ = batch_engine
    assert module.AUTO_OFF_PEAK_ONLY is True
    assert api_permission_blocker(False, module.AUTO_OFF_PEAK_ONLY, CLOCK()) == "peak"  # Monday 02:00 UTC


def test_off_peak_only_is_a_validated_novel_setting():
    base = dict(id="fixture", title="T", source_language="zh", target_language="en",
                profile="historical_military", final_chapter=3)
    assert NovelConfig.from_dict(base).off_peak_only is False and "off_peak_only" not in NovelConfig.from_dict(base).to_dict()
    assert NovelConfig.from_dict({**base, "off_peak_only": True}).to_dict()["off_peak_only"] is True
    with pytest.raises(ValueError):
        NovelConfig.from_dict({**base, "off_peak_only": "yes"})



def test_the_term_audit_fixes_a_rendering_before_any_chapter_uses_it(batch_engine):
    project, module, provider, _ = batch_engine
    provider.audit_answers = {"龙骧营": {"action": "correct", "english": "Dragon Rise Battalion"},
                              "李四": {"action": "reject"}}
    run_all(module)
    assert provider.count("audit") == 1  # 4 terms fit one chunk; chunks go one at a time
    second = next(body for kind, body in provider.sends if kind == "translation" and provider.chapter_of(body) == 2)
    assert "Dragon Rise Battalion" in second["messages"][-1]["content"]
    assert "Dragon Soar Battalion" not in second["messages"][-1]["content"]
    entries = module.glossary["entries"]
    assert entries["龙骧营"]["english"] == "Dragon Rise Battalion" and "李四" not in entries
    settled = json.loads((project.data_dir / "batch" / "0001" / "settled.json").read_text(encoding="utf-8"))
    assert sorted(settled["term_audit"]["changes"]) == ["李四: rejected", "龙骧营: Dragon Soar Battalion → Dragon Rise Battalion"]
    assert (1, "batch_term_audit") in {(item["chapter"], item["kind"]) for item in read_items(project.data_dir)}


def test_apply_term_audit_rules():
    entry = lambda english, first=1, merge="": {"english": english, "category": "military unit", "status": "provisional",
                                                "approved_renderings": [], "merge_with": merge, "first_chapter": first}
    settled = {"甲营": entry("First Camp"), "乙营": entry("Second Camp", 2), "丙营": entry("Third Camp", 2, merge="甲营")}
    decisions = {"甲营": {"action": "reject", "english": "", "approved_renderings": [], "merge_with": "", "notes": ""},
                 "乙营": {"action": "merge", "english": "", "approved_renderings": [], "merge_with": "旧营", "notes": ""}}
    audited, changes = bt.apply_term_audit(settled, decisions, {"旧营": {"english": "Old Camp"}})
    assert "甲营" not in audited and audited["丙营"]["merge_with"] == ""  # its target was rejected
    assert audited["乙营"]["merge_with"] == "旧营" and audited["乙营"]["english"] == "Old Camp"
    assert changes == ["甲营: rejected", "乙营: merged into 旧营"]


@pytest.mark.parametrize("batch_engine", [{"final_chapter": 4, "audit_interval": 2,
                                           "batch_mode": BatchModeConfig(enabled=True, chapters=3, max_parallel=4)}],
                         indirect=True)
def test_a_batch_ends_on_an_audit_boundary(batch_engine):
    project, module, provider, _ = batch_engine
    assert module.process_chapter_after_batch_preparation(1) is True
    assert [(item["start"], item["end"]) for item in batch_runner.manifests(project.data_dir)] == [(1, 2)]


def test_an_alias_merge_after_translation_does_not_rewrite_the_chapter(batch_engine):
    project, module, provider, _ = batch_engine
    assert module.process_chapter_after_batch_preparation(1) is True
    entries = module.glossary["entries"]
    entries["禁卫营"].update(english="Imperial Guard Camp Of Tang", alias_of="唐禁卫营")  # as an audit merge would
    entries["唐禁卫营"] = {"english": "Imperial Guard Camp Of Tang", "category": "military unit", "status": "provisional"}
    assert module.process_chapter_after_batch_preparation(2) is True
    text = (project.chapter_dir / "0002.md").read_text(encoding="utf-8")
    assert "Imperial Guard Camp was renamed" in text and "Of Tang" not in text
