"""Explicit terminology policy regressions; all prose is synthetic."""
import copy
import json
from pathlib import Path

import pytest

import noveltranslator_core as core
from production_commit_qa_checkpoint import run_commit_qa_checkpoint
from strict_terminology_repair import perform_deterministic_strict_terminology_repair
from translation_profile import load_translation_profile, profile_state_instructions
from validate_project import terminology_policy_counts
from test_production_goat_runtime import engine, zero_network, IDENTITY
from production_provider_runtime import goat_provenance

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COUNTS = {
    "ace_evolution": 0, "great_king_zhuxia": 10, "guhuo_bird": 6,
    "ming_novel": 0, "rogue_first_prince": 11,
}


def youxi(**metadata):
    return {"有熙氏": {"english": "Youxi Clan", "category": "clan",
                       "status": "locked", **metadata}}


def qa(text, entries=None, source="有熙氏", priority=("clan",)):
    return core.translation_commit_qa(source, text, entries or youxi(strict_qa=True), priority)


@pytest.mark.parametrize("priority", [(), ("clan",), ("clan", "character", "cultivation realm")])
@pytest.mark.parametrize("flag", [None, False, True])
def test_clan_requires_explicit_approval_independently_of_priority(priority, flag):
    entries = youxi(**({} if flag is None else {"strict_qa": flag}))
    assert bool(core.effective_strict_glossary_entry_items(entries, priority)) is (flag is True)
    assert qa("A Youxi clansman arrived.", entries, priority=priority)["commit_allowed"] is (flag is not True)


@pytest.mark.parametrize("status", ["provisional", "contextual"])
def test_nonlocked_strict_opt_in_is_invalid_and_never_selected(status):
    entries = youxi(status=status, strict_qa=True, approved_renderings=["Youxi clansman"])
    assert core.effective_strict_glossary_entry_items(entries, ("clan",)) == ()
    assert "strict_qa=true requires status=locked" in core.glossary_strict_qa_errors(entries)[0]
    assert core.strict_glossary_entry_origin(entries["有熙氏"]) is None


@pytest.mark.parametrize("status", ["provisional", "contextual"])
def test_nonlocked_priority_entries_remain_soft(status):
    entries = youxi(status=status, approved_renderings=["Youxi clansman"])
    assert core.glossary_strict_qa_errors(entries, high_priority_categories=("clan",)) == []
    assert core.effective_strict_glossary_entry_items(entries, ("clan",)) == ()


@pytest.mark.parametrize("flag,origin", [(None, "legacy"), (False, "legacy"), (True, "legacy+explicit")])
def test_legacy_realm_protection_keeps_existing_profile_gate(flag, origin):
    entry = {"english": "First Realm", "category": "Cultivation Realm", "status": "locked"}
    if flag is not None:
        entry["strict_qa"] = flag
    legacy = core.legacy_strict_glossary_categories(("Cultivation Realm", "clan"))
    assert legacy == {"cultivation realm"}
    assert core.strict_glossary_entry_origin(entry, legacy) == origin
    assert core.effective_strict_glossary_entry_items({"初境": entry}, legacy)
    # Historical military profiles did not activate cultivation enforcement.
    assert bool(core.effective_strict_glossary_entry_items({"初境": entry}, ("clan",))) is (flag is True)


@pytest.mark.parametrize("novel,expected", BASELINE_COUNTS.items())
def test_repository_strict_set_equals_merged_main_predicate(novel, expected):
    config = json.loads((ROOT / "novels" / novel / "novel.json").read_text(encoding="utf-8"))
    profile = load_translation_profile(config["profile"], ROOT)
    entries = json.loads((ROOT / "novels" / novel / "data/glossary.json").read_text(encoding="utf-8"))["entries"]
    # Independent reproduction of the pinned merged-main selection predicate.
    baseline = tuple((source, entry) for source, entry in entries.items()
        if isinstance(entry, dict) and entry.get("status") == "locked" and (
            (core.normalize_category(entry.get("category", "")) in {core.normalize_category(c) for c in profile.high_priority_categories}
             and core.normalize_category(entry.get("category", "")) in {"cultivation realm"})
            or entry.get("strict_qa") is True))
    assert core.effective_strict_glossary_entry_items(entries, profile.high_priority_categories) == baseline
    assert len(baseline) == expected


def test_rejected_broad_selector_is_not_reintroduced():
    effective = broad = 0
    for novel in BASELINE_COUNTS:
        directory = ROOT / "novels" / novel
        config = json.loads((directory / "novel.json").read_text(encoding="utf-8"))
        profile = load_translation_profile(config["profile"], ROOT)
        entries = json.loads((directory / "data/glossary.json").read_text(encoding="utf-8"))["entries"]
        effective += len(core.effective_strict_glossary_entry_items(entries, profile.high_priority_categories))
        broad += sum(e.get("status") == "locked" and (
            core.normalize_category(e.get("category", "")) in profile.high_priority_categories
            or e.get("strict_qa") is True) for e in entries.values())
    assert effective == 27
    assert broad == 181  # Git snapshots; earlier live-state analysis reported ~185.


@pytest.mark.parametrize("metadata,passes", [({}, False), ({"accepted_inflections": ["Youxi clansman"]}, True),
    ({"repair_aliases": ["Youxi clansman"]}, False), ({"auto_plural": True}, False)])
def test_semantic_derivative_needs_separate_accepted_form(metadata, passes):
    entries = youxi(strict_qa=True, **metadata)
    assert core.glossary_strict_qa_errors(entries) == []
    assert qa("A Youxi clansman arrived.", entries)["commit_allowed"] is passes
    assert qa("The Youxi Clan arrived.", entries)["commit_allowed"]


@pytest.mark.parametrize("category,metadata,passes", [
    ("noble title", {}, False), ("noble title", {"accepted_inflections": ["feudal lords"]}, True),
    ("military rank", {}, True), ("military rank", {"auto_plural": False}, False),
    ("military rank", {"auto_plural": False, "accepted_inflections": ["feudal lords"]}, True),
])
def test_feudal_lord_plural_is_category_gated_or_explicit(category, metadata, passes):
    entries = {"封君": {"english": "feudal lord", "category": category, "status": "locked", "strict_qa": True, **metadata}}
    assert core.translation_commit_qa("封君", "The feudal lords arrived.", entries, (category,))["commit_allowed"] is passes
    assert not core.translation_commit_qa("封君", "A feudal lordship emerged.", entries, (category,))["commit_allowed"]


def test_alias_drives_only_bounded_repair_and_final_qa_runs():
    entries = youxi(strict_qa=True, repair_aliases=["Youxi clansman"])
    before = "A Youxi clansman arrived.\nThe guard waited."
    seen, saved = [], []
    def check(text):
        seen.append(text)
        return qa(text, entries)
    result = perform_deterministic_strict_terminology_repair(
        source_text="有熙氏", translation=before, meta={"stage": "translated"},
        glossary_entries=entries, high_priority_categories=("clan",),
        persist_meta=lambda meta: saved.append("meta"),
        persist_translation=lambda text: saved.append(text), qa_checker=check)
    assert result["status"] == "succeeded"
    assert result["translation"] == "A Youxi Clan arrived.\nThe guard waited."
    assert seen == [result["translation"]]
    assert result["qa"]["commit_allowed"]
    assert saved == ["meta", result["translation"]]
    assert len(result["repairs_applied"]) == 1
    ambiguous = core.deterministic_strict_terminology_repair("有熙氏", before + " A Youxi clansman left.", entries, ("clan",))
    assert ambiguous["repaired_text"] == before + " A Youxi clansman left."
    assert ambiguous["repairs_applied"] == []


def test_disabled_repair_remains_hard_stop():
    entries = youxi(strict_qa=True, auto_repair=False)
    assert core.repair_eligible_strict_glossary_entry_items(entries, ("clan",)) == ()
    assert not qa("A Youxi clansman arrived.", entries)["commit_allowed"]
    result = core.deterministic_strict_terminology_repair("有熙氏", "A Youxi clansman arrived.", entries, ("clan",))
    assert result["repairs_applied"] == []
    assert result["unresolved_strict_terms"]


def test_explicit_compound_owns_nested_source_once():
    entries = youxi(strict_qa=True)
    entries["有熙氏长老"] = {"english": "Youxi Elder", "category": "clan", "status": "locked", "strict_qa": True}
    selected = core.effective_strict_glossary_entry_items(entries, ("clan",))
    inventory = core._strict_occurrence_inventory("有熙氏长老", "The Youxi Elder arrived.", selected, glossary_entries=entries)
    assert inventory["有熙氏"]["source_anchor_count"] == 0
    assert inventory["有熙氏长老"]["source_anchor_count"] == 1
    assert core.glossary_strict_qa_errors(entries) == []
    assert qa("The Youxi Elder arrived.", entries, source="有熙氏长老")["commit_allowed"]
    assert not qa("The Youxi Clan arrived.", entries, source="有熙氏长老")["commit_allowed"]


def test_character_priority_and_explicit_referential_identity_stay_distinct():
    entry = {"english": "Lin Wei", "category": "character", "status": "locked"}
    source, target = "林伟\n\n林伟\n\n林伟", "Lin Wei arrived.\n\nHe spoke.\n\nHe left."
    assert core.effective_strict_glossary_entry_items({"林伟": entry}, ("character",)) == ()
    entry["strict_qa"] = True
    assert core.translation_commit_qa(source, target, {"林伟": entry}, ("character",))["commit_allowed"]
    assert not core.translation_commit_qa(source, target.replace("Lin Wei", "He"), {"林伟": entry}, ("character",))["commit_allowed"]


def test_filtered_prompt_metadata_is_not_autonomous_promotion():
    entries = youxi(strict_qa=True, accepted_inflections=["Youxi clansman"], auto_repair=False)
    entries["另一氏"] = {"english": "Other Clan", "category": "clan", "status": "locked"}
    original = copy.deepcopy(entries)
    relevant = core.relevant_glossary_entries("有熙氏和另一氏", entries)
    text = core.glossary_entries_to_text(relevant, ("clan",), core.STRICT_LOCKED_GLOSSARY_CATEGORIES)
    assert text.count("MANDATORY CANONICAL FAMILY") == 1
    assert relevant[0]["accepted_inflections"] == ["Youxi clansman"]
    assert relevant[0]["auto_repair"] is False
    assert "strict_qa" not in relevant[1]
    assert entries == original
    profile = load_translation_profile("xianxia_xuanhuan", ROOT)
    assert "clan" in profile_state_instructions(profile)
    assert "high-priority" in profile_state_instructions(profile)


def test_a_chapter_using_part_of_a_full_name_still_gets_the_entry():
    entries = {
        "도윤 카렐": {"english": "Doyun Karel", "category": "character", "status": "provisional"},
        "하든 남작": {"english": "Baron Harden", "category": "character", "status": "provisional"},
        "安.雷石": {"english": "An Leishi", "category": "character", "status": "provisional"},
        "달빛 탑": {"english": "Moon Tower", "category": "magic", "status": "provisional"},
    }
    chosen = lambda text: [item["english"] for item in core.relevant_glossary_entries(text, entries)]
    assert chosen("도윤이 웃었다.") == ["Doyun Karel"]
    assert chosen("카렐 경이 왔다.") == ["Doyun Karel"]
    assert chosen("雷石走了。") == ["An Leishi"]
    assert chosen("다른 남작이 왔다. 달빛이 흘렀다.") == []
    assert chosen("하든이 왔다.") == ["Baron Harden"]


def test_a_term_first_seen_with_a_bracket_is_found_without_it():
    entries = {
        "달검(月劍)의 명인(名人)": {"english": "Moon Sword Master", "category": "title", "status": "provisional"},
        "업(業)": {"english": "karma", "category": "system term", "status": "provisional"},
        "돌거인（GOLEM）": {"english": "Stone Giant", "category": "monster", "status": "provisional"},
        "探測器（E型）": {"english": "Detector (Type E)", "category": "item", "status": "provisional"},
        "雷石（山）": {"english": "Thunder Stone (Mountain)", "category": "item", "status": "provisional"},
    }
    chosen = lambda text: [item["english"] for item in core.relevant_glossary_entries(text, entries)]
    assert chosen("달검의 명인이 왔다.") == ["Moon Sword Master"]
    assert chosen("돌거인이 왔다.") == ["Stone Giant"]
    assert chosen("업이 쌓였다. 探測器。雷石。") == []


def test_a_term_written_with_zhi_is_found_without_it():
    entries = {"XY之契約": {"english": "XY Pact", "category": "item", "status": "provisional"}}
    assert [item["english"] for item in core.relevant_glossary_entries("雙方簽了XY契約。", entries)] == ["XY Pact"]


def test_reporting_separates_overlap_acceptance_and_repair():
    entries = youxi(strict_qa=True, accepted_inflections=["Youxi clansman"], auto_repair=False)
    entries.update({
        "初境": {"english": "First Realm", "category": "cultivation realm", "status": "locked", "strict_qa": True},
        "次境": {"english": "Second Realm", "category": "cultivation realm", "status": "locked"},
        "甲": {"english": "A", "category": "clan", "status": "locked"},
        "乙": {"english": "B", "category": "clan", "status": "contextual"},
        "丙": {"english": "C", "category": "clan", "status": "provisional"},
    })
    assert terminology_policy_counts(entries, ("clan", "cultivation realm")) == {
        "Locked canonical entries": 4, "High-priority locked entries (review importance)": 4,
        "Legacy hard terminology entries": 2, "Explicit strict terminology entries": 2,
        "Legacy and explicit hard entries (overlap)": 1, "Configured hard terminology entries": 3,
        "Entries with accepted inflections": 1, "Configured automatic strict-term repair eligible": 2,
        "Explicit automatic-repair opt-ins": 0, "Explicit automatic-repair opt-outs": 1,
        "Entries with approved repair aliases": 0,
        "Activation-bounded explicit strict entries": 0,
    }


def test_invalid_explicit_declaration_is_visible_but_not_effective():
    counts = terminology_policy_counts(youxi(status="provisional", strict_qa=True), ("clan",))
    assert counts["Explicit strict terminology entries"] == 1
    assert counts["Configured hard terminology entries"] == 0


def test_strict_failure_checkpoints_pending_without_advancing_state(tmp_path):
    pending = tmp_path / "pending"; pending.mkdir()
    text_path = pending / "0001.md"
    text_path.write_text("A Youxi clansman arrived.", encoding="utf-8")
    protected = {}
    for name in ("progress", "glossary", "continuity", "issues"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"sentinel": name}), encoding="utf-8")
        protected[path] = path.read_bytes()
    meta = {"stage": "reviewed", "translation_stats": {"cost_usd": 0.02}, "state": {}, "review_result": {}}
    result = run_commit_qa_checkpoint(chapter_number=1, source_text="有熙氏",
        translation=text_path.read_text(encoding="utf-8"), finish_reason="stop", meta=meta,
        qa_checker=lambda chapter, source, target, reason: qa(target),
        reset_failed_meta=core.pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: core.atomic_write_json(pending / "0001.json", value))
    assert not result["commit_allowed"]
    assert text_path.read_text(encoding="utf-8") == "A Youxi clansman arrived."
    assert result["meta"]["stage"] == "translated"
    assert result["meta"]["translation_stats"] == {"cost_usd": 0.02}
    assert "state" not in result["meta"] and "review_result" not in result["meta"]
    assert all(path.read_bytes() == original for path, original in protected.items())


@pytest.mark.parametrize("action", ["accept", "correct", "contextual", "merge"])
def test_model_metadata_cannot_promote_a_new_hard_entry(monkeypatch, action):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
    profile = load_translation_profile("xianxia_xuanhuan", ROOT)
    monkeypatch.setattr(production, "ACTIVE_PROFILE", profile)
    target = youxi(strict_qa=True)
    monkeypatch.setattr(production, "glossary", {"entries": target})
    candidate = {"chinese": "另氏", "english": "Other Clan", "category": "clan", "candidate_id": "synthetic:clan"}
    decision = {"candidate_id": "synthetic:clan", "action": action,
                "english": "Other Clan", "category": "clan", "strict_qa": True,
                "strict_qa_from_chapter": 397,
                "approved_renderings": ["Other Clan"], "merge_with": "有熙氏"}
    production.apply_term_review(1, [candidate], {"decisions": [decision]})
    new_entry = target["另氏"]
    assert "strict_qa_from_chapter" not in new_entry
    assert "strict_qa" not in new_entry
    assert core.effective_strict_glossary_entry_items({"另氏": new_entry}, profile.high_priority_categories) == ()
    assert target["有熙氏"]["strict_qa"] is True


def test_real_production_strict_gate_preserves_pending_and_blocks_auxiliary_work(engine, monkeypatch):
    project, production, sends, client, budget, *_ = engine
    entries = youxi(strict_qa=True, auto_repair=False)
    monkeypatch.setattr(production, "glossary", {"entries": entries})
    monkeypatch.setitem(production.chapter_map[1], "text", "有熙氏")
    pending_text = project.pending_dir / "0001.md"
    pending_meta = project.pending_dir / "0001.json"
    pending_text.write_text("A Youxi clansman arrived.", encoding="utf-8")
    stats = production.zero_stats()
    core.atomic_write_json(pending_meta, {"chapter": 1, "stage": "translated",
        "finish_reason": "stop", "translation_stats": stats,
        "translation_model": "deepseek-flash", **goat_provenance(IDENTITY, "a" * 64)})
    def forbidden(*args, **kwargs):
        pytest.fail("strict failure must precede generation, provider, state, review, and commit")
    for name in ("translate_chapter", "send_api_request_once", "extract_state",
                 "review_new_terms", "commit_reviewed_chapter"):
        monkeypatch.setattr(production, name, forbidden)
    client.chat.completions.create = forbidden
    paths = (project.progress_file, project.glossary_file, project.continuity_file,
             project.issues_file, project.audit_history_file)
    before = {path: path.read_bytes() for path in paths}
    memory = copy.deepcopy((production.progress, production.glossary, production.continuity, production.issues_data))
    for _ in range(2):
        assert production.process_chapter(1) is False
        assert pending_text.read_text(encoding="utf-8") == "A Youxi clansman arrived."
        meta = json.loads(pending_meta.read_text(encoding="utf-8"))
        assert not meta["local_qa"]["commit_allowed"]
        assert meta["translation_stats"] == stats
        assert meta["stage"] == "translated"
        assert all(path.read_bytes() == content for path, content in before.items())
        assert (production.progress, production.glossary, production.continuity, production.issues_data) == memory
    assert sends == [] and budget.api_calls == 0
    assert not (project.chapter_dir / "0001.md").exists()
