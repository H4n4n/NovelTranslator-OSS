"""Prospective policy proofs; no transport or production writes.

NT_READONLY_HISTORICAL_ROOT optionally supplies an operator-authorized read-only
corpus. Without it, all 396 historical artifacts are synthetic.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import noveltranslator_core as core
from production_commit_qa_checkpoint import run_commit_qa_checkpoint
from production_translation_request import build_production_translation_user_prompt
from strict_terminology_repair import (
    assess_strict_terminology_micro_repair_eligibility,
    perform_strict_terminology_micro_repair,
    revalidate_recorded_strict_terminology_candidate,
    validate_and_apply_strict_terminology_response,
)
from test_terminology_policy import youxi
from test_production_goat_runtime import engine, zero_network, IDENTITY
from production_provider_runtime import goat_provenance
from validate_project import terminology_policy_counts


def prospective(**metadata):
    return youxi(strict_qa=True, strict_qa_from_chapter=397, **metadata)


def qa(chapter, text="A Youxi clansman arrived.", entries=None):
    return core.translation_commit_qa("有熙氏", text, entries or prospective(),
                                      ("clan",), chapter_number=chapter)


@pytest.mark.parametrize("chapter,hard", [(1, False), (396, False), (397, True), (398, True)])
def test_activation_origin_prompt_qa_and_repair(chapter, hard):
    entries = prospective()
    before = copy.deepcopy(entries)
    assert bool(core.effective_strict_glossary_entry_items(entries, ("clan",), chapter_number=chapter)) is hard
    assert bool(core.repair_eligible_strict_glossary_entry_items(entries, ("clan",), chapter_number=chapter)) is hard
    assert qa(chapter)["commit_allowed"] is not hard
    assert qa(chapter, "The Youxi Clan arrived.")["commit_allowed"]
    prompt = build_production_translation_user_prompt(
        {"chapter": chapter, "text": "有熙氏"}, chapter_numbering="global",
        source_language="zh", target_language="en", glossary_entries=entries,
        profile=NS(high_priority_categories=("clan",)), continuity_summary="")
    assert ("MANDATORY CANONICAL" in prompt) is hard
    assert "Youxi Clan" in prompt  # soft canonical attention survives
    derivative = core.deterministic_strict_terminology_repair(
        "有熙氏", "A Youxi clansman arrived.", entries, ("clan",), chapter_number=chapter)
    assert derivative["repairs_applied"] == []  # no synthesized alias
    repair = core.deterministic_strict_terminology_repair(
        "有熙氏", "The Wrong Clan arrived.", prospective(repair_aliases=["Wrong Clan"]),
        ("clan",), chapter_number=chapter)
    assert bool(repair["repairs_applied"]) is hard
    canonicalize = core.canonicalize_locked_high_priority_terms(
        "有熙氏", "The Youxi-Clan arrived.", entries, ("clan",), chapter_number=chapter)
    assert bool(canonicalize["repairs_applied"]) is hard
    eligible = assess_strict_terminology_micro_repair_eligibility(
        "有熙氏", "A Youxi clansman arrived.", entries, ("clan",), chapter_number=chapter)
    assert bool(eligible["violations"]) is hard
    assert entries == before


@pytest.mark.parametrize("metadata", [
    {"strict_qa_from_chapter": 397}, {"strict_qa": False, "strict_qa_from_chapter": 397},
    {"strict_qa": True, "status": "provisional", "strict_qa_from_chapter": 397},
    {"strict_qa": True, "status": "contextual", "strict_qa_from_chapter": 397},
    *({"strict_qa": True, "strict_qa_from_chapter": v} for v in (0, -1, 397.0, "397", True, False, None)),
])
def test_invalid_metadata_is_rejected_without_mutation(metadata):
    entries = youxi(**metadata)
    before = copy.deepcopy(entries)
    assert any("strict_qa_from_chapter" in e for e in core.glossary_strict_qa_errors(entries))
    with pytest.raises(ValueError, match="invalid strict_qa_from_chapter"):
        core.effective_strict_glossary_entry_items(entries, chapter_number=397)
    assert entries == before


@pytest.mark.parametrize("operation", [
    lambda e: core.effective_strict_glossary_entry_items(e),
    lambda e: core.repair_eligible_strict_glossary_entry_items(e),
    lambda e: core.translation_commit_qa("有熙氏", "Youxi clansman", e, ()),
    lambda e: core.deterministic_strict_terminology_repair("有熙氏", "Youxi-Clan", e, ()),
    lambda e: core.canonicalize_locked_high_priority_terms("有熙氏", "Youxi-Clan", e, ()),
    lambda e: core.glossary_entries_to_text(core.relevant_glossary_entries("有熙氏", e)),
    lambda e: assess_strict_terminology_micro_repair_eligibility("有熙氏", "Youxi clansman", e, ()),
])
def test_missing_context_fails_closed(operation):
    with pytest.raises(ValueError, match="chapter_number is required"):
        operation(prospective())
    assert core.configured_strict_glossary_entry_items(prospective())
    assert core.glossary_strict_qa_errors(prospective()) == []


@pytest.mark.parametrize("chapter", [0, -1, True, False, 397.0, "397"])
def test_invalid_chapter_context_is_rejected(chapter):
    with pytest.raises(ValueError, match="chapter_number must be"):
        qa(chapter)


@pytest.mark.parametrize("metadata,passes,repairs", [
    ({}, False, False), ({"accepted_inflections": ["Youxi clansman"]}, True, False),
    ({"repair_aliases": ["Youxi clansman"]}, False, True),
    ({"auto_repair": False}, False, False),
])
def test_existing_acceptance_and_repair_contract_after_activation(metadata, passes, repairs):
    entries = prospective(**metadata)
    assert core.glossary_strict_qa_errors(entries) == []
    assert qa(397, entries=entries)["commit_allowed"] is passes
    result = core.deterministic_strict_terminology_repair(
        "有熙氏", "A Youxi clansman arrived.", entries, ("clan",), chapter_number=397)
    assert bool(result["repairs_applied"]) is repairs
    historical = core.deterministic_strict_terminology_repair(
        "有熙氏", "A Youxi clansman arrived.", entries, ("clan",), chapter_number=396)
    assert not historical["repairs_applied"]


@pytest.mark.parametrize("chapter,origin", [(396, "legacy"), (397, "legacy+explicit"), (398, "legacy+explicit")])
def test_legacy_is_independent_of_explicit_activation(chapter, origin):
    entries = {"初境": {"english": "First Realm", "status": "locked",
                         "category": "cultivation realm", "strict_qa": True,
                         "strict_qa_from_chapter": 397}}
    legacy = core.legacy_strict_glossary_categories(("cultivation realm",))
    assert core.strict_glossary_entry_origin(entries["初境"], legacy, chapter_number=chapter) == origin
    assert not core.translation_commit_qa("初境", "Another level.", entries, legacy,
                                          chapter_number=chapter)["commit_allowed"]


def test_inventory_and_chapter_effective_reporting():
    entries = prospective()
    configured = terminology_policy_counts(entries, ("clan",))
    assert configured["Configured hard terminology entries"] == 1
    assert configured["Explicit strict terminology entries"] == 1
    assert configured["Activation-bounded explicit strict entries"] == 1
    assert "Effective strict terminology entries" not in configured
    for chapter, active in [(396, 0), (397, 1)]:
        counts = terminology_policy_counts(entries, ("clan",), chapter_number=chapter)
        assert counts["Effective strict terminology entries"] == active
        assert counts["Active explicit strict entries at supplied chapter"] == active
        assert counts["Future explicit strict entries at supplied chapter"] == 1-active


@pytest.mark.parametrize("chapter,allowed", [(396, True), (397, False)])
def test_pending_checkpoint_uses_artifact_chapter_not_next_progress(tmp_path, chapter, allowed):
    text = "A Youxi clansman arrived."
    meta = {"chapter": chapter, "stage": "reviewed", "translation_stats": {"cost_usd": .02},
            "state": {}, "review_result": {}}
    path = tmp_path / f"{chapter:04d}.md"
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    seen = []
    def checker(number, source, translation, reason):
        seen.append(number)
        return qa(number, translation)
    for _ in range(2):
        result = run_commit_qa_checkpoint(chapter_number=chapter, source_text="有熙氏",
            translation=path.read_text(encoding="utf-8"), finish_reason="stop", meta=meta,
            qa_checker=checker, reset_failed_meta=core.pending_meta_after_commit_qa_failure,
            persist_meta=lambda value: core.atomic_write_json(tmp_path / f"{chapter:04d}.json", value))
        meta = result["meta"]
        assert result["commit_allowed"] is allowed
        assert meta["translation_stats"] == {"cost_usd": .02}
        assert path.read_bytes() == original
    assert seen == [chapter, chapter]


def test_real_pending_397_is_flagged_and_keeps_going_without_retranslating(engine, monkeypatch):
    from review_queue import read_items
    project, production, sends, client, budget, *_ = engine
    monkeypatch.setattr(production, "glossary", {"entries": prospective(auto_repair=False)})
    monkeypatch.setitem(production.chapter_map, 397, {"chapter": 397, "text": "有熙氏"})
    # Deliberately disagreeing next_chapter demonstrates artifact identity ownership.
    assert production.progress["next_chapter"] == 1
    text_path, meta_path = project.pending_dir / "0397.md", project.pending_dir / "0397.json"
    text_path.write_text("A Youxi clansman arrived.", encoding="utf-8")
    stats = production.zero_stats()
    core.atomic_write_json(meta_path, {"chapter": 397, "stage": "translated", "finish_reason": "stop",
        "translation_stats": stats, "translation_model": "deepseek-flash",
        **goat_provenance(IDENTITY, "a"*64)})
    class ReachedStateExtraction(BaseException):
        pass
    def reached_state(*args, **kwargs):
        raise ReachedStateExtraction()
    def forbidden(*args, **kwargs):
        pytest.fail("a flagged term must not retranslate or send")
    for name in ("translate_chapter", "send_api_request_once"):
        monkeypatch.setattr(production, name, forbidden)
    monkeypatch.setattr(production, "extract_state", reached_state)
    client.chat.completions.create = forbidden
    with pytest.raises(ReachedStateExtraction):
        production.process_chapter(397)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["local_qa"]["chapter"] == 397
    assert meta["local_qa"]["commit_allowed"] and meta["local_qa"]["flagged_blockers"]
    assert meta["translation_stats"] == stats
    assert text_path.read_text(encoding="utf-8") == "A Youxi clansman arrived."
    assert [item["kind"] for item in read_items(project.data_dir)] == ["commit_qa"]
    assert sends == [] and budget.api_calls == 0


def test_real_local_qa_and_review_identity_use_396_397(engine, monkeypatch):
    _, production, *_ = engine
    monkeypatch.setattr(production, "glossary", {"entries": prospective()})
    monkeypatch.setitem(production.progress, "next_chapter", 900)
    for chapter, allowed in [(396, True), (397, False)]:
        assert production.local_qa(chapter, "有熙氏", "Youxi clansman", "stop")["commit_allowed"] is allowed
    a = production.current_review_checkpoint_identity(396, "有熙氏", "Youxi clansman", [])
    b = production.current_review_checkpoint_identity(397, "有熙氏", "Youxi clansman", [])
    assert a["relevant_glossary_text_sha256"] != b["relevant_glossary_text_sha256"]


def test_historical_396_artifacts_have_zero_new_failures():
    live_root = os.environ.get("NT_READONLY_HISTORICAL_ROOT")
    entries, priority = youxi(), ("clan",)
    artifacts = [(n, "有熙氏", "A Youxi clansman arrived.") for n in range(1, 397)]
    protected = {}
    if live_root:
        from translation_profile import load_translation_profile
        root = Path(live_root)
        novel = root / "novels/great_king_zhuxia"
        glossary_file, chapter_file = novel / "data/glossary.json", novel / "data/chapters.json"
        entries = json.loads(glossary_file.read_text(encoding="utf-8"))["entries"]
        assert "strict_qa" not in entries["有熙氏"]
        config = json.loads((novel / "novel.json").read_text(encoding="utf-8"))
        priority = load_translation_profile(config["profile"], root).high_priority_categories
        library = json.loads(chapter_file.read_text(encoding="utf-8"))
        rows = library["chapters"] if isinstance(library, dict) else library
        sources = {row["chapter"]: row["text"] for row in rows}
        artifacts = []
        for n in range(1, 397):
            path = novel / "output/chapters" / f"{n:04d}.md"
            data = path.read_bytes()
            protected[path] = hashlib.sha256(data).hexdigest()
            artifacts.append((n, sources[n], data.decode("utf-8")))
        for path in (glossary_file, chapter_file, novel / "data/progress.json"):
            protected[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    future = copy.deepcopy(entries)
    future["有熙氏"].update(strict_qa=True, strict_qa_from_chapter=397)
    assert core.glossary_strict_qa_errors(future, high_priority_categories=priority) == []
    new_failures = []
    for number, source, target in artifacts:
        old = core.locked_high_priority_term_violations(source, target, entries, priority, chapter_number=number)
        new = core.locked_high_priority_term_violations(source, target, future, priority, chapter_number=number)
        if new != old:
            new_failures.append(number)
    assert new_failures == []
    assert not core.translation_commit_qa("有熙氏", "Youxi clansman", future, priority, chapter_number=397)["commit_allowed"]
    assert core.translation_commit_qa("有熙氏", "Youxi Clan", future, priority, chapter_number=397)["commit_allowed"]
    assert len(artifacts) == 396
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in protected.items())
    if live_root:
        assert len(core.effective_strict_glossary_entry_items(entries, priority, chapter_number=397)) == 10
        assert len(core.effective_strict_glossary_entry_items(future, priority, chapter_number=396)) == 10
        assert len(core.effective_strict_glossary_entry_items(future, priority, chapter_number=397)) == 11
    print(f"Historical scan: {len(artifacts)} artifacts; newly failing chapters: {new_failures}; source={'live read-only' if live_root else 'synthetic'}")


@pytest.mark.parametrize("chapter,active", [(396, False), (397, True)])
def test_recorded_repair_validation_and_replay_are_chapter_gated(chapter, active):
    entries = prospective(repair_aliases=["Youxi clansman"])
    source = "有熙氏"
    text = "A Youxi clansman arrived. The guard watched the gate and waited quietly nearby."
    eligibility = assess_strict_terminology_micro_repair_eligibility(
        source, text, entries, ("clan",), chapter_number=chapter)
    assert eligibility["eligible"] is active
    if active:
        region = eligibility["repair_regions"][0]
        patch = {"region_id": region["region_id"], "source_term": source,
                 "old_text": "Youxi clansman", "new_text": "Youxi Clan"}
        validation = validate_and_apply_strict_terminology_response(
            source, text, {"patches": [patch]}, eligibility, entries, ("clan",),
            chapter_number=chapter)
        assert validation["accepted"]
    def forbidden(*args, **kwargs):
        pytest.fail("recorded local replay must not send or write an unchanged candidate")
    context = dict(novel_id="synthetic", chapter=chapter, source_text=source,
        translation=text, meta={}, glossary_entries=entries, high_priority_categories=("clan",),
        persist_meta=forbidden, persist_translation=forbidden, account_api_usage=forbidden,
        qa_checker=lambda candidate: qa(chapter, candidate, entries))
    replay = revalidate_recorded_strict_terminology_candidate(**context)
    assert not replay["api_called"]
    if not active:
        result = perform_strict_terminology_micro_repair(**context,
            profile_id="synthetic", target_language="en", auxiliary_terminology_policy="",
            request_api=forbidden)
        assert result["status"] == "clean" and not result["api_called"]


@pytest.mark.parametrize("action", ["keep", "correct", "contextual", "merge"])
def test_audit_model_cannot_add_activation_metadata(monkeypatch, action):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
    from translation_profile import load_translation_profile
    monkeypatch.setattr(production, "ACTIVE_PROFILE", load_translation_profile("xianxia_xuanhuan", Path(__file__).resolve().parents[1]))
    data = {"entries": {"另氏": {"english": "Other Clan", "category": "clan", "status": "provisional"},
                        **prospective()}}
    target = {"chinese": "另氏"}
    decision = {"chinese": "另氏", "action": action, "english": "Other Clan", "category": "clan",
                "approved_renderings": ["Other Clan"], "merge_with": "有熙氏",
                "strict_qa": True, "strict_qa_from_chapter": 1}
    production.apply_periodic_audit(1, 1, {"decisions": [decision]}, glossary_data=data, targets=[target])
    assert "strict_qa" not in data["entries"]["另氏"]
    assert "strict_qa_from_chapter" not in data["entries"]["另氏"]
    assert data["entries"]["有熙氏"] == prospective()["有熙氏"]
