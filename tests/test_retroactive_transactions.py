"""Synthetic historical prose only; no production or paid inference."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import retroactive_transactions as tx
from noveltranslator_core import RETROACTIVE_SAFE_CATEGORIES, source_anchored_replacement
from retroactive_span_planning import (
    canonical_hash, digest, normalized_rules, ownership_policy, plan_chapter, transform_bytes,
)


def rule(source="韓", old="Han", new="Hann", category="character", **extra):
    return {"chinese": source, "old_english": old, "new_english": new,
            "category": category, **extra}


def glossary(**entries):
    return {"entries": entries or {"韓": {"english": "Han", "category": "character"}}}


def plan(source="韓來了", text="Han entered. His Hand was injured.", rules=None, data=None):
    return plan_chapter(source, text.encode("utf-8"), normalized_rules(rules or [rule()]),
                        ownership_policy(data or glossary()), RETROACTIVE_SAFE_CATEGORIES)


@pytest.mark.parametrize("source,text,rules,data,reason", [
    ("無", "Han.", None, None, "source_absent_or_owned_by_longer_identity"),
    ("韓", "Han.", [rule(category="ordinary vocabulary")], None, "category_or_generic_safety_gate"),
    ("韓", "Hand.", None, None, "cardinality_requires_manual_review"),
    ("韓", "Han spoke to Han.", None, None, "cardinality_requires_manual_review"),
    ("韓韓", "Han.", None, None, "cardinality_requires_manual_review"),
    ("有熙氏祖廟", "Youxi.", [rule("有熙氏", "Youxi", "Yuxi")],
     glossary(**{"有熙氏": {"english": "Youxi"}, "有熙氏祖廟": {"english": "Youxi Ancestral Temple"}}),
     "source_absent_or_owned_by_longer_identity"),
    ("韓", "Han.", None, glossary(**{"韓": {"english": "Han"}, "漢": {"english": "Han"}}),
     "competing_or_nested_target_ownership"),
    ("韓", "Han.", None, glossary(**{"韓": {"english": "Han"}, "漢": {"english": "HAN"}}),
     "competing_or_nested_target_ownership"),
    ("雲", "Azure Cloud Sect.", [rule("雲", "Azure Cloud", "Blue Cloud", "sect")],
     glossary(**{"雲": {"english": "Azure Cloud"}, "宗": {"english": "Azure Cloud Sect"}}),
     "competing_or_nested_target_ownership"),
    ("韓", "Han.", None, glossary(**{"韓": {"english": "Han", "status": "contextual"}}),
     "contextual_identity_requires_manual_review"),
    ("韓", "Han.", None, glossary(**{"韓": {"english": "Han", "alias_of": "漢"}}),
     "alias_identity_requires_manual_review"),
    ("韓", "Han.", None, glossary(**{"韓": {"english": "Other"}}),
     "old_rendering_not_owned_by_glossary_identity"),
    ("韓", "Han.", None, glossary(**{"漢": {"english": "Han"}}), "missing_glossary_identity"),
])
def test_ownership_fail_closed(source, text, rules, data, reason):
    edits, decisions = plan(source, text, rules, data)
    assert edits == []
    assert decisions[0]["skip_reason"] == reason


@pytest.mark.parametrize("field", ["english", "approved_renderings", "repair_aliases",
                                   "accepted_inflections", "aliases"])
def test_every_competing_target_family_reserves_identity(field):
    other = {field: "Han" if field == "english" else ["Han"]}
    edits, decisions = plan(data=glossary(**{"韓": {"english": "Han"}, "漢": other}))
    assert not edits
    assert decisions[0]["skip_reason"] == "competing_or_nested_target_ownership"


@pytest.mark.parametrize("old,category", [("Captain", "military rank"), ("Temple", "location"),
                                          ("Fire", "artifact"), ("Chair", "artifact"),
                                          ("work", "specialized recurring term")])
def test_common_targets_rejected(old, category):
    assert not plan("韓", old, [rule(old=old, category=category)],
                    glossary(**{"韓": {"english": old}}))[0]


@pytest.mark.parametrize("text", ["Han entered. His Hand was injured.",
                                 "# Héading 🦉\r\n\r\n**Han** — Hand.\r\n尾\r\n",
                                 "\ufeffHan.\n", "Hand Han Han_name xHan 漢Han"])
def test_exact_byte_span_only(text):
    before = text.encode("utf-8")
    edits, decisions = plan(text=text)
    assert len(edits) == 1
    edit = edits[0]
    assert before[edit["start"]:edit["end"]] == b"Han"
    after = transform_bytes(before, edits)
    assert after == before[:edit["start"]] + b"Hann" + before[edit["end"]:]
    assert after[:edit["start"]] == before[:edit["start"]]
    assert after[edit["start"] + 4:] == before[edit["end"]:]
    assert b"Hand" not in before or b"Hand" in after
    assert decisions[0]["status"] == "planned"


def test_two_same_identity_rules_cannot_claim_same_span():
    edits, decisions = plan(rules=[rule(), rule(new="Han Kingdom")])
    assert not edits
    assert {item["skip_reason"] for item in decisions} == {"overlapping_retroactive_rules"}


def test_two_different_identity_overlapping_rules_cannot_edit():
    edits, decisions = plan("雲宗", "Azure Cloud Sect.",
                            [rule("雲", "Azure Cloud", "Blue Cloud", "sect"),
                             rule("宗", "Cloud Sect", "Cloud School", "sect")],
                            glossary(**{"雲": {"english": "Azure Cloud"},
                                        "宗": {"english": "Cloud Sect"}}))
    assert not edits
    assert all(item["status"] == "skipped" for item in decisions)


def test_category_alone_and_compatibility_helper_cannot_authorize():
    assert source_anchored_replacement("韓", "韓", "Han.", "Han", "Hann", "character") == ("Han.", 0)
    assert source_anchored_replacement("韓", "韓", "Hand Han.", "Han", "Hann", "character",
                                       glossary=glossary()) == ("Hand Hann.", 1)
    assert not plan_chapter("韓", b"Han", normalized_rules([rule()]),
                           ownership_policy(glossary()), set())[0]


@pytest.fixture
def sandbox(tmp_path):
    output = tmp_path / "chapters"
    output.mkdir()
    (output / "0010.md").write_bytes("# Héading\r\n\r\nHan entered. His Hand was injured.\r\n".encode())
    return dict(novel_id="synthetic", audit_label="audit-10", through_chapter=30,
                replacements=[rule()], glossary=glossary(), safe_categories=RETROACTIVE_SAFE_CATEGORIES,
                committed_chapters=[10], chapter_map={10: {"text": "韓來了"}}, output_dir=output,
                retroactive_dir=tmp_path / "retroactive", backup_dir=tmp_path / "backup")


def records(sandbox):
    directory = sandbox["retroactive_dir"] / "transactions"
    return (next(directory.glob("*.plan.json")), next(directory.glob("*.state.json"), None))


def test_durable_plan_backup_hash_result_and_idempotence(sandbox, monkeypatch):
    original = tx.atomic_write_bytes
    before = (sandbox["output_dir"] / "0010.md").read_bytes()
    events = []

    def checked_write(path, content):
        if path.parent == sandbox["output_dir"]:
            plan_path, _ = records(sandbox)
            durable = json.loads(plan_path.read_bytes())
            backup = next(sandbox["backup_dir"].rglob("0010.md"))
            assert backup.read_bytes() == before
            assert digest(backup.read_bytes()) == durable["chapters"][0]["before_sha256"]
            assert digest(content) == durable["chapters"][0]["after_sha256"]
            events.append("chapter")
        else:
            events.append("backup")
        original(path, content)

    monkeypatch.setattr(tx, "atomic_write_bytes", checked_write)
    result = tx.execute_retroactive_transaction(**sandbox)
    plan_path, state_path = records(sandbox)
    plan_bytes = plan_path.read_bytes()
    backup = next(sandbox["backup_dir"].rglob("0010.md"))
    backup_time = backup.stat().st_mtime_ns
    assert events == ["backup", "chapter"]
    assert result["chapter_results"][0]["before_sha256"] == digest(before)
    assert result["chapter_results"][0]["after_sha256"] == digest((sandbox["output_dir"] / "0010.md").read_bytes())
    assert result["chapter_results"][0]["status"] == "newly_applied"
    assert result["api_request_count"] == 0
    assert {"audit_label", "through_chapter", "replacement_rules_considered", "replacement_rules_applied",
            "chapters_changed", "replacement_count", "details"} <= result.keys()
    again = tx.execute_retroactive_transaction(**sandbox)
    assert events == ["backup", "chapter"]
    assert again["transaction_id"] == result["transaction_id"]
    assert again["chapter_results"][0]["status"] == "already_applied"
    assert again["already_applied_edits_reconciled"] == 1
    assert plan_path.read_bytes() == plan_bytes
    assert backup.stat().st_mtime_ns == backup_time
    assert json.loads(state_path.read_bytes())["state"] == "FINALIZED"


@pytest.mark.parametrize("stage", ["after_plan", "after_backup", "after_chapter", "after_applied", "after_result"])
def test_crash_at_every_durability_boundary(sandbox, monkeypatch, stage):
    original_json, original_bytes = tx.atomic_write_json, tx.atomic_write_bytes

    def crash_json(path, data):
        original_json(path, data)
        if ((stage == "after_plan" and path.name.endswith(".plan.json")) or
                (stage == "after_applied" and data.get("state") == "APPLIED") or
                (stage == "after_result" and path.name.endswith(".result.json"))):
            raise KeyboardInterrupt("injected crash")

    def crash_bytes(path, content):
        original_bytes(path, content)
        if ((stage == "after_backup" and path.parent != sandbox["output_dir"]) or
                (stage == "after_chapter" and path.parent == sandbox["output_dir"])):
            raise KeyboardInterrupt("injected crash")

    monkeypatch.setattr(tx, "atomic_write_json", crash_json)
    monkeypatch.setattr(tx, "atomic_write_bytes", crash_bytes)
    with pytest.raises(KeyboardInterrupt):
        tx.execute_retroactive_transaction(**sandbox)
    plan_path, state_path = records(sandbox)
    saved_plan = plan_path.read_bytes()
    assert not state_path or json.loads(state_path.read_bytes())["state"] != "FINALIZED"
    monkeypatch.setattr(tx, "atomic_write_json", original_json)
    monkeypatch.setattr(tx, "atomic_write_bytes", original_bytes)
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["transactions_resumed"] == 1
    assert result["replacement_count"] == 1
    expected = "already_applied" if stage in {"after_chapter", "after_applied", "after_result"} else "newly_applied"
    assert result["chapter_results"][0]["status"] == expected
    assert plan_path.read_bytes() == saved_plan
    assert b"Hann entered" in (sandbox["output_dir"] / "0010.md").read_bytes()
    assert b"Hannn" not in (sandbox["output_dir"] / "0010.md").read_bytes()


def test_partial_multichapter_crash_reconciles_only_remaining_before(sandbox, monkeypatch):
    for number in (20, 30):
        (sandbox["output_dir"] / f"{number:04d}.md").write_bytes(b"Han.\r\n")
        sandbox["chapter_map"][number] = {"text": "韓"}
    sandbox["committed_chapters"] = [10, 20, 30]
    original = tx.atomic_write_bytes
    writes = []

    def crash_after_twenty(path, content):
        original(path, content)
        if path.parent == sandbox["output_dir"]:
            writes.append(path.name)
            if path.name == "0020.md":
                raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_bytes", crash_after_twenty)
    with pytest.raises(KeyboardInterrupt):
        tx.execute_retroactive_transaction(**sandbox)
    assert writes == ["0010.md", "0020.md"]
    monkeypatch.setattr(tx, "atomic_write_bytes", original)
    result = tx.execute_retroactive_transaction(**sandbox)
    assert [item["status"] for item in result["chapter_results"]] == [
        "already_applied", "already_applied", "newly_applied"]
    assert result["already_applied_edits_reconciled"] == 2
    assert result["replacement_count"] == 3


@pytest.mark.parametrize("change", ["chapter", "backup", "source", "glossary", "rules", "plan", "missing_backup"])
def test_restart_contradictions_block_without_prose_writes(sandbox, monkeypatch, change):
    tx.execute_retroactive_transaction(**sandbox)
    path = sandbox["output_dir"] / "0010.md"
    backup = next(sandbox["backup_dir"].rglob("0010.md"))
    plan_path, _ = records(sandbox)
    if change == "chapter":
        path.write_bytes(b"external changed bytes")
    elif change == "backup":
        backup.write_bytes(b"wrong backup")
    elif change == "source":
        sandbox["chapter_map"][10]["text"] = "韓外部"
    elif change == "glossary":
        sandbox["glossary"]["entries"]["漢"] = {"english": "Han"}
    elif change == "rules":
        sandbox["replacements"] = [rule(new="Different")]
    elif change == "plan":
        data = json.loads(plan_path.read_bytes())
        data["chapters"][0]["edits"][0]["start"] += 1
        plan_path.write_text(json.dumps(data), encoding="utf-8")
    else:
        backup.unlink()
    unchanged = path.read_bytes()
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("blocked write"))
    with pytest.raises(tx.RetroactiveBlocked):
        tx.execute_retroactive_transaction(**sandbox)
    assert path.read_bytes() == unchanged


def test_preflight_wrong_backup_blocks_before_any_other_chapter_write(sandbox):
    tx.execute_retroactive_transaction(**sandbox)
    plan_path, _ = records(sandbox)
    durable = json.loads(plan_path.read_bytes())
    backup = next(sandbox["backup_dir"].rglob("0010.md"))
    backup.write_bytes(b"inconsistent")
    (sandbox["output_dir"] / "0010.md").write_bytes(b"external")
    with pytest.raises(tx.RetroactiveBlocked, match="neither BEFORE"):
        tx.execute_retroactive_transaction(**sandbox)
    assert durable["transaction_id"]


def test_transaction_identity_changes_with_material_inputs_not_noise(sandbox):
    request = {"novel_id": "test", "audit_label": "audit", "through_chapter": 10,
               "rules": normalized_rules([rule()]), "ownership_policy": ownership_policy(glossary()),
               "safe_categories": sorted(RETROACTIVE_SAFE_CATEGORIES), "committed_chapters": [10]}
    first = tx.build_plan(request, {10: b"Han."}, sandbox["chapter_map"])
    assert first == tx.build_plan(copy.deepcopy(request), {10: b"Han."}, sandbox["chapter_map"])
    assert "timestamp" not in json.dumps(first)
    request["rules"] = normalized_rules([rule(new="Han Kingdom")])
    second = tx.build_plan(request, {10: b"Han."}, sandbox["chapter_map"])
    assert second["transaction_id"] != first["transaction_id"]
    assert normalized_rules([rule(), rule()]) == normalized_rules([rule()])


@pytest.mark.parametrize("field", ["aliases", "source_variants"])
def test_source_variants_reserve_longer_identity(field):
    edits, decisions = plan("有熙氏祖庙", "Youxi.", [rule("有熙氏", "Youxi", "Yuxi")],
                            glossary(**{"有熙氏": {"english": "Youxi"},
                                        "有熙氏祖廟": {"english": "Youxi Temple", field: ["有熙氏祖庙"]}}))
    assert not edits
    assert decisions[0]["skip_reason"] == "source_absent_or_owned_by_longer_identity"


def test_equal_source_alias_ownership_is_never_lexical_tie_break_authorization():
    edits, decisions = plan(data=glossary(**{"韓": {"english": "Han"},
                                              "齊": {"english": "Qi", "source_variants": ["韓"]}}))
    assert not edits
    assert decisions[0]["skip_reason"] == "competing_source_ownership"


def test_case_variant_longer_rendering_reserves_target():
    edits, decisions = plan("雲", "Azure Cloud Sect.", [rule("雲", "Azure Cloud", "Blue Cloud", "sect")],
                            glossary(**{"雲": {"english": "Azure Cloud"},
                                        "宗": {"english": "azure cloud sect"}}))
    assert not edits
    assert decisions[0]["skip_reason"] == "competing_or_nested_target_ownership"


def test_automatic_accepted_plural_reserves_shared_target():
    edits, decisions = plan("甲", "Azure Knights.", [rule("甲", "Azure Knights", "Blue Knights", "military unit")],
                            glossary(**{"甲": {"english": "Azure Knights"},
                                        "乙": {"english": "Azure Knight", "category": "military rank"}}))
    assert not edits
    assert decisions[0]["skip_reason"] == "competing_or_nested_target_ownership"


def test_first_final_evidence_preserved_after_reconciliation(sandbox):
    first = tx.execute_retroactive_transaction(**sandbox)
    result_path = next((sandbox["retroactive_dir"] / "transactions").glob("*.result.json"))
    original_bytes = result_path.read_bytes()
    second = tx.execute_retroactive_transaction(**sandbox)
    assert first["chapter_results"][0]["status"] == "newly_applied"
    assert second["chapter_results"][0]["status"] == "already_applied"
    assert result_path.read_bytes() == original_bytes
    assert len(list(result_path.parent.glob("*.reconcile.*.json"))) == 2


def test_corrupt_final_result_not_overwritten(sandbox):
    tx.execute_retroactive_transaction(**sandbox)
    result_path = next((sandbox["retroactive_dir"] / "transactions").glob("*.result.json"))
    result_path.write_bytes(b"broken")
    with pytest.raises(tx.RetroactiveBlocked, match="result hash mismatch"):
        tx.execute_retroactive_transaction(**sandbox)
    assert result_path.read_bytes() == b"broken"


def test_atomic_failure_before_replace_preserves_exact_before(sandbox, monkeypatch):
    import noveltranslator_core as core
    path = sandbox["output_dir"] / "0010.md"
    before = path.read_bytes()
    original = core.os.replace

    def fail_replace(temp, destination):
        if Path(destination) == path:
            assert Path(temp).read_bytes() != before
            raise OSError("injected pre-replace failure")
        return original(temp, destination)

    monkeypatch.setattr(core.os, "replace", fail_replace)
    with pytest.raises(tx.LocalPersistenceError):
        tx.execute_retroactive_transaction(**sandbox)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))
    monkeypatch.setattr(core.os, "replace", original)
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["chapter_results"][0]["status"] == "newly_applied"


def test_batch_preflight_blocks_remaining_writes_on_unrelated_mismatch(sandbox, monkeypatch):
    sandbox["committed_chapters"] += [20, 30]
    for number in (20, 30):
        (sandbox["output_dir"] / f"{number:04d}.md").write_bytes(b"Han.")
        sandbox["chapter_map"][number] = {"text": "韓"}
    original = tx.atomic_write_json

    def stop_after_plan(path, data):
        original(path, data)
        if path.name.endswith(".plan.json"):
            raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_json", stop_after_plan)
    with pytest.raises(KeyboardInterrupt):
        tx.execute_retroactive_transaction(**sandbox)
    monkeypatch.setattr(tx, "atomic_write_json", original)
    (sandbox["output_dir"] / "0030.md").write_bytes(b"external change")
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("batch should block before writes"))
    with pytest.raises(tx.RetroactiveBlocked, match="Chapter 30"):
        tx.execute_retroactive_transaction(**sandbox)
    assert (sandbox["output_dir"] / "0020.md").read_bytes() == b"Han."


def test_wrong_backup_on_before_restart_never_overwritten(sandbox, monkeypatch):
    original = tx.atomic_write_bytes

    def crash_after_backup(path, content):
        original(path, content)
        raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_bytes", crash_after_backup)
    with pytest.raises(KeyboardInterrupt):
        tx.execute_retroactive_transaction(**sandbox)
    backup = next(sandbox["backup_dir"].rglob("0010.md"))
    backup.write_bytes(b"wrong")
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("must preserve bad backup"))
    with pytest.raises(tx.RetroactiveBlocked, match="backup hash mismatch"):
        tx.execute_retroactive_transaction(**sandbox)
    assert backup.read_bytes() == b"wrong"


def test_complete_transaction_revalidates_skipped_chapter_identity(sandbox):
    sandbox["committed_chapters"].append(20)
    sandbox["chapter_map"][20] = {"text": "無"}
    path = sandbox["output_dir"] / "0020.md"
    path.write_bytes(b"Hand.")
    tx.execute_retroactive_transaction(**sandbox)
    path.write_bytes(b"changed even though not eligible")
    with pytest.raises(tx.RetroactiveBlocked, match="Chapter 20"):
        tx.execute_retroactive_transaction(**sandbox)


def test_empty_committed_inventory_never_edits_existing_files(sandbox):
    sandbox["committed_chapters"] = []
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["chapters_changed"] == []
    assert result["replacement_count"] == 0
    assert b"Han entered" in (sandbox["output_dir"] / "0010.md").read_bytes()


def test_uncommitted_and_out_of_range_chapters_untouched(sandbox):
    for number in (1, 40):
        (sandbox["output_dir"] / f"{number:04d}.md").write_bytes(b"Han")
    sandbox["committed_chapters"].append(40)
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["chapters_changed"] == [10]
    assert (sandbox["output_dir"] / "0001.md").read_bytes() == b"Han"
    assert (sandbox["output_dir"] / "0040.md").read_bytes() == b"Han"


def install_engine(monkeypatch, sandbox):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", SimpleNamespace(id="synthetic"))
    monkeypatch.setattr(engine, "ACTIVE_PROFILE", SimpleNamespace(
        retroactive_safe_categories=RETROACTIVE_SAFE_CATEGORIES, formal_named_categories=set()))
    monkeypatch.setattr(engine, "progress", {"completed": [10], "last_completed": 10, "next_chapter": 11})
    monkeypatch.setattr(engine, "glossary", copy.deepcopy(sandbox["glossary"]))
    monkeypatch.setattr(engine, "chapter_map", sandbox["chapter_map"])
    monkeypatch.setattr(engine, "audit_history", {"audits": []})
    monkeypatch.setattr(engine, "issues_data", {"issues": []})
    for attr, key in (("OUTPUT_DIR", "output_dir"), ("RETROACTIVE_DIR", "retroactive_dir"), ("BACKUP_DIR", "backup_dir")):
        monkeypatch.setattr(engine, attr, sandbox[key])
    root = sandbox["output_dir"].parent
    monkeypatch.setattr(engine, "AUDIT_DIR", root / "audits")
    for attr, value in (("GLOSSARY_FILE", engine.glossary), ("PROGRESS_FILE", engine.progress),
                        ("CONTINUITY_FILE", {"latest_chapter": 10}), ("ISSUES_FILE", engine.issues_data),
                        ("AUDIT_HISTORY_FILE", engine.audit_history)):
        path = root / (attr.lower() + ".json")
        monkeypatch.setattr(engine, attr, path)
        engine.atomic_write_json(path, value)
    return engine


def test_periodic_audit_replays_same_local_transaction_after_write_crash(sandbox, monkeypatch):
    engine = install_engine(monkeypatch, sandbox)
    initial_progress, initial_glossary = copy.deepcopy(engine.progress), copy.deepcopy(engine.glossary)
    monkeypatch.setattr(engine, "log_event", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(engine, "call_periodic_audit", lambda *args: (
        {"issues": []}, {"cost_usd": 0}, {"candidate_set_hash": "validated-local-fixture"}))
    monkeypatch.setattr(engine, "apply_periodic_audit", lambda *args, **kwargs: {
        "retroactive_replacements": [rule()], "kept": [], "corrected": [], "rejected": [],
        "contextualized": [], "merged": []})
    accounting, save = engine.add_accounting_totals_once, engine.save_json
    def account(*args):
        calls.append("accounting")
        return accounting(*args)
    def saved(*args):
        calls.append("outer_state")
        return save(*args)
    monkeypatch.setattr(engine, "add_accounting_totals_once", account)
    monkeypatch.setattr(engine, "save_json", saved)
    original = tx.atomic_write_bytes

    def crash(path, content):
        original(path, content)
        if path.parent == sandbox["output_dir"]:
            raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_bytes", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.run_periodic_audit(1, 10)
    assert calls and all(call == "accounting" for call in calls)
    assert engine.progress == initial_progress and engine.glossary == initial_glossary
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("duplicate prose write"))
    engine.run_periodic_audit(1, 10)
    assert calls[0] == "accounting"
    directory = sandbox["retroactive_dir"] / "transactions"
    result = json.loads(next(directory.glob("*.result.json")).read_bytes())
    assert result["chapter_results"][0]["status"] == "already_applied"
    assert engine.progress["last_audited_chapter"] == 10


def test_periodic_local_hash_failure_never_installs_or_advances(sandbox, monkeypatch):
    engine = install_engine(monkeypatch, sandbox)
    engine.apply_retroactive_replacements("0001_0010", 10, [rule()], staged_glossary=engine.glossary)
    (sandbox["output_dir"] / "0010.md").write_bytes(b"external")
    initial = copy.deepcopy(engine.progress)
    monkeypatch.setattr(engine, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "call_periodic_audit", lambda *args: ({"issues": []}, {}, {"candidate_set_hash": "validated-local-fixture"}))
    monkeypatch.setattr(engine, "apply_periodic_audit", lambda *args, **kwargs: {"retroactive_replacements": [rule()]})
    monkeypatch.setattr(engine, "save_json", lambda *args: pytest.fail("outer state write"))
    from production_periodic_audit_finalization import AuditFinalizationBlocked
    with pytest.raises((tx.RetroactiveBlocked, AuditFinalizationBlocked)):
        engine.run_periodic_audit(1, 10)
    assert engine.progress == initial


def test_startup_known_fix_uses_hardened_engine_before_policy_install(sandbox, monkeypatch):
    engine = install_engine(monkeypatch, sandbox)
    monkeypatch.setattr(engine, "MING_COMPATIBILITY_MODE", True)
    monkeypatch.setattr(engine, "glossary", glossary(**{
        "蓋州游擊": {"english": "Ganzhou Mobile Corps Commander", "category": "historical office"}}))
    sandbox["chapter_map"][10] = {"text": "蓋州游擊來了"}
    path = sandbox["output_dir"] / "0010.md"
    path.write_bytes(b"Ganzhou Mobile Corps Commander arrived.\r\n")
    saved = []
    monkeypatch.setattr(engine, "save_json", lambda path, value: saved.append(copy.deepcopy(value)))
    original = tx.atomic_write_bytes

    def crash(path, content):
        original(path, content)
        if path.parent == sandbox["output_dir"]:
            raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_bytes", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.apply_known_glossary_fixes()
    assert saved == []
    assert engine.glossary["entries"]["蓋州游擊"]["english"] == "Ganzhou Mobile Corps Commander"
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("duplicate startup write"))
    engine.apply_known_glossary_fixes()
    assert saved
    assert path.read_bytes() == b"Gaizhou Mobile Corps Commander arrived.\r\n"


def test_staged_contextual_policy_never_becomes_global_retroactive(sandbox):
    sandbox["staged_glossary"] = glossary(**{"韓": {"english": "Hann", "status": "contextual",
                                                         "approved_renderings": ["Han", "Hann"]}})
    before = (sandbox["output_dir"] / "0010.md").read_bytes()
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["replacement_count"] == 0
    assert (sandbox["output_dir"] / "0010.md").read_bytes() == before
    assert result["details"][0]["decisions"][0]["skip_reason"] == "contextual_identity_requires_manual_review"


def audit_recovery_fixture(sandbox):
    from full_pipeline_periodic_audit_recovery import _snapshot_baseline
    root = sandbox["retroactive_dir"].parent
    output = root / "output/chapters"
    output.mkdir(parents=True)
    (output / "0010.md").write_bytes((sandbox["output_dir"] / "0010.md").read_bytes())
    sandbox.update(output_dir=output, retroactive_dir=root / "output/retroactive_fixes",
                   audit_label="0001_0010", through_chapter=10)
    data = root / "data"
    data.mkdir()
    (root / "novel.json").write_text(json.dumps({"id": "synthetic"}), encoding="utf-8")
    state = {"glossary": sandbox["glossary"], "progress": {"last_completed": 10, "next_chapter": 11,
             "last_audited_chapter": 0, "completed": [10]}, "issues": {"issues": []},
             "audit_history": {"audits": []}, "continuity": {}}
    for name, value in state.items():
        (data / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    (data / "chapters.json").write_text(json.dumps([{"chapter": 10, "text": "韓來了"}]), encoding="utf-8")
    manifest, created = _snapshot_baseline(root, 1, 10)
    assert created
    return root, manifest


def test_experiment_audit_recovery_preserves_after_prose_and_immutable_plan(sandbox, monkeypatch):
    from full_pipeline_periodic_audit_recovery import _restore_baseline
    root, manifest = audit_recovery_fixture(sandbox)
    original = tx.atomic_write_bytes

    def crash(path, content):
        original(path, content)
        if path.parent == sandbox["output_dir"]:
            raise KeyboardInterrupt()

    monkeypatch.setattr(tx, "atomic_write_bytes", crash)
    with pytest.raises(KeyboardInterrupt):
        tx.execute_retroactive_transaction(**sandbox)
    plan_path, _ = records(sandbox)
    plan_bytes = plan_path.read_bytes()
    path = sandbox["output_dir"] / "0010.md"
    after = path.read_bytes()
    _restore_baseline(root, manifest)
    assert path.read_bytes() == after
    assert plan_path.read_bytes() == plan_bytes
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *args: pytest.fail("duplicate edit after wrapper recovery"))
    result = tx.execute_retroactive_transaction(**sandbox)
    assert result["chapter_results"][0]["status"] == "already_applied"


@pytest.mark.parametrize("change", ["prose", "extra_chapter", "backup", "missing_plan", "source"])
def test_experiment_recovery_hash_ambiguity_blocks_before_associated_state_restore(sandbox, change):
    from full_pipeline_periodic_audit_recovery import _restore_baseline, FullPipelinePeriodicAuditRecoveryError
    root, manifest = audit_recovery_fixture(sandbox)
    tx.execute_retroactive_transaction(**sandbox)
    if change == "prose":
        (sandbox["output_dir"] / "0010.md").write_bytes(b"external")
    elif change == "extra_chapter":
        (sandbox["output_dir"] / "0011.md").write_bytes(b"new unexpected")
    elif change == "backup":
        next(sandbox["backup_dir"].rglob("0010.md")).write_bytes(b"bad backup")
    elif change == "missing_plan":
        records(sandbox)[0].unlink()
    else:
        (root / "data/chapters.json").write_text(json.dumps([{"chapter": 10, "text": "別"}]), encoding="utf-8")
    marker = root / "data/issues.json"
    marker.write_bytes(b'{"issues":["must not restore before block"]}')
    before = marker.read_bytes()
    with pytest.raises(FullPipelinePeriodicAuditRecoveryError, match="local retroactive"):
        _restore_baseline(root, manifest)
    assert marker.read_bytes() == before


def test_audit_diagnostics_do_not_read_translation_source_or_policy(sandbox, monkeypatch):
    engine = install_engine(monkeypatch, sandbox)
    class NoSourceLookup:
        def __getitem__(self, key):
            pytest.fail("audit diagnostics looked up a translation source")
    monkeypatch.setattr(engine, "chapter_map", NoSourceLookup())
    monkeypatch.setattr(engine, "current_production_operation_policy",
                        lambda: pytest.fail("audit diagnostics reconstructed translation policy"))
    events = []
    monkeypatch.setattr(engine, "log_event", lambda event, **fields: events.append(fields))
    supervisor = engine.autonomous_audit_recovery_supervisor(1, 10)
    # Construction/diagnostic output grants neither acquisition nor publication.
    from production_autonomous_recovery import stopped
    supervisor.report("audit_publication", stopped("IDENTITY_OR_PROOF_MISMATCH"))
    assert events[-1]["audit_start_chapter"] == 1
    assert events[-1]["audit_end_chapter"] == 10
    assert events[-1]["disposition"] == "BLOCKED_UNVERIFIED"


@pytest.mark.parametrize("damage", ["missing_source", "changed_source", "intent_range"])
def test_historical_audit_range_cannot_bypass_source_or_intent_proof(sandbox, monkeypatch, damage):
    engine = install_engine(monkeypatch, sandbox)
    assert 1 not in engine.chapter_map  # The legitimate range start is absent.
    baseline = copy.deepcopy(engine.progress)
    monkeypatch.setattr(engine, "log_event", lambda *args, **kwargs: None)
    acquisitions = []
    def acquired(*args):
        acquisitions.append(args)
        return {"issues": []}, {"cost_usd": 0}, {"candidate_set_hash": "validated-local-fixture"}
    monkeypatch.setattr(engine, "call_periodic_audit", acquired)
    monkeypatch.setattr(engine, "apply_periodic_audit", lambda *args, **kwargs: {
        "retroactive_replacements": [rule()], "kept": [], "corrected": [], "rejected": [],
        "contextualized": [], "merged": []})
    original = tx.atomic_write_bytes
    def interrupted(path, content):
        original(path, content)
        if path.parent == sandbox["output_dir"]:
            raise KeyboardInterrupt()
    monkeypatch.setattr(tx, "atomic_write_bytes", interrupted)
    with pytest.raises(KeyboardInterrupt):
        engine.run_periodic_audit(1, 10)
    if damage == "missing_source":
        engine.chapter_map.pop(10)
    elif damage == "changed_source":
        engine.chapter_map[10] = {"text": "Different synthetic source."}
    else:
        from production_accounting import canonical_sha256
        path = engine.AUDIT_DIR / "audit_0001_0010.finalization.json"
        value = json.loads(path.read_bytes())
        value["end"] = 11
        value["intent_sha256"] = canonical_sha256({k: v for k, v in value.items() if k != "intent_sha256"})
        path.write_text(json.dumps(value), encoding="utf-8")
    root = sandbox["output_dir"].parent
    def snapshot():
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    before = snapshot()
    monkeypatch.setattr(engine, "call_periodic_audit", lambda *a: pytest.fail("audit reacquisition"))
    monkeypatch.setattr(engine, "save_json", lambda *a: pytest.fail("unproved state write"))
    monkeypatch.setattr(tx, "atomic_write_bytes", lambda *a: pytest.fail("unproved prose write"))
    from production_periodic_audit_finalization import AuditFinalizationBlocked
    with pytest.raises((tx.RetroactiveBlocked, AuditFinalizationBlocked)):
        engine.run_periodic_audit(1, 10)
    assert acquisitions == [(1, 10)]
    assert engine.progress == baseline
    assert snapshot() == before


@pytest.mark.parametrize("language,source_key", [("zh", "韓"), ("ko", "한민")])
def test_aut2_full_history_discovery_is_source_bound_and_uses_existing_writer(tmp_path, language, source_key):
    from historical_reconciliation import impact_report
    output = tmp_path / language / "output/chapters"
    output.mkdir(parents=True)
    numbers = list(range(1, 53))
    chapter_map = {n: {"production_index": n, "text": source_key if n in (1, 2, 26, 51, 52) else ""}
                   for n in numbers}
    before = {n: ("\ufeff# Title 🦉\r\n\r\n**Han** — Hand, café.\r\n" if n in (1, 26, 51)
                  else "Han met Han.\r\n" if n == 2 else "Hann.\r\n" if n == 52
                  else "Untouched ordinary prose.\r\n").encode() for n in numbers}
    for n, raw in before.items():
        (output / f"{n:04d}.md").write_bytes(raw)
    data = glossary(**{source_key: {"english": "Han", "category": "character", "status": "provisional"}})
    staged = copy.deepcopy(data)
    staged["entries"][source_key]["english"] = "Hann"
    args = dict(novel_id=language, audit_label="0026_0052", through_chapter=52,
                replacements=[rule(source_key)], glossary=data, staged_glossary=staged,
                safe_categories=RETROACTIVE_SAFE_CATEGORIES, committed_chapters=numbers, chapter_map=chapter_map,
                output_dir=output, retroactive_dir=tmp_path / language / "output/retroactive_fixes",
                backup_dir=tmp_path / language / "backup")
    result = tx.execute_retroactive_transaction(**args)
    plan_file = next((args["retroactive_dir"] / "transactions").glob("*.plan.json"))
    planned = json.loads(plan_file.read_bytes())
    report = impact_report(planned, chapter_map, staged, before)
    assert report["examined_chapters"] == 52
    assert report["proven_eligible_chapters"] == 3
    assert report["deferred_chapters"] == 1
    assert report["already_current_chapters"] == 1
    assert result["chapters_changed"] == [1, 26, 51]
    assert source_key not in json.dumps(report, ensure_ascii=False)
    assert "Hand, café" not in json.dumps(report, ensure_ascii=False)
    for n, original in before.items():
        actual = (output / f"{n:04d}.md").read_bytes()
        assert actual == (original.replace(b"**Han**", b"**Hann**") if n in (1, 26, 51) else original)
    backups = list(args["backup_dir"].rglob("*.md"))
    assert len(backups) == 3
    assert all(p.read_bytes() == before[int(p.stem)] for p in backups)
    untouched = {n: (output / f"{n:04d}.md").stat().st_mtime_ns for n in numbers}
    assert tx.execute_retroactive_transaction(**args)["transaction_id"] == result["transaction_id"]
    assert untouched == {n: (output / f"{n:04d}.md").stat().st_mtime_ns for n in numbers}
    changed = copy.deepcopy(chapter_map)
    changed[52]["source_document_sha256"] = "changed-provenance"
    assert impact_report(planned, changed, staged)["source_inventory_sha256"] != report["source_inventory_sha256"]


@pytest.fixture
def aut2_review_intent(sandbox, monkeypatch):
    """Small local audit interrupted after intent, before any receipt or prose write."""
    import production_periodic_audit_finalization as final
    root = sandbox["output_dir"].parent / "review_novel"
    output = root / "output/chapters"
    output.mkdir(parents=True)
    (output / "0010.md").write_bytes((sandbox["output_dir"] / "0010.md").read_bytes())
    sandbox.update(output_dir=output, retroactive_dir=root / "output/retroactive_fixes", backup_dir=root / "backup")
    engine = install_engine(monkeypatch, sandbox)
    changes = {"retroactive_replacements": [rule()], "kept": [], "corrected": [],
               "rejected": [], "contextualized": [], "merged": []}
    monkeypatch.setattr(engine, "apply_periodic_audit", lambda *a, **k: copy.deepcopy(changes))
    monkeypatch.setattr(engine, "call_periodic_audit", lambda *a: pytest.fail("provider reacquisition"))
    monkeypatch.setattr(engine, "send_api_request_once", lambda **k: pytest.fail("provider send"))
    engine.AUDIT_DIR.mkdir(parents=True)
    engine.save_json(engine.periodic_audit_checkpoint_path(1, 10), {"synthetic": "accepted local decision"})
    args = ({"issues": []}, {"cost_usd": 0}, {"candidate_set_hash": "synthetic-local-decision"})
    write = final.atomic_write_json
    def stop(path, value):
        write(path, value)
        if path.name.endswith(".finalization.json"):
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", stop)
        with pytest.raises(KeyboardInterrupt):
            final.finalize_periodic_audit(engine, 1, 10, *args)
    return root, engine, args


def _review_snapshot(root):
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes()
            for p in root.rglob("*")}


def _review_rehash_intent(engine, value):
    import production_periodic_audit_finalization as final
    value["historical_proof"]["plan_sha256"] = canonical_hash(value["transaction_plan"])
    value["intent_sha256"] = canonical_hash({k: v for k, v in value.items() if k != "intent_sha256"})
    final.atomic_write_json(final.intent_path(engine, 1, 10), value)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":1}', b'{"a":[{"nested":1,"nested":1}]}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e400}',
    b'{"x":-1e400}', b'{"x":"\xff"}', b'{"x":"\\ud800"}', b'{"x":', b'[]', b'null',
    '{"x":1}'.encode("utf-16")])
def test_aut2_review_strict_json_rejects_ambiguous_evidence(tmp_path, raw):
    from historical_reconciliation import read_object
    from production_failure_classification import ProductionIntegrityError
    path = tmp_path / "evidence.json"
    path.write_bytes(raw)
    with pytest.raises(ProductionIntegrityError):
        read_object(path)
    assert path.read_bytes() == raw


def test_aut2_review_strict_json_keeps_valid_historical_formatting(tmp_path):
    from historical_reconciliation import read_object
    path = tmp_path / "pretty.json"
    raw = '\ufeff{\r\n  "한": [1.25, {"中文": "café"}], "flag": true\r\n}\r\n'.encode()
    path.write_bytes(raw)
    assert read_object(path) == {"한": [1.25, {"中文": "café"}], "flag": True}
    assert path.read_bytes() == raw


@pytest.mark.parametrize("damage", ["wrong_hex", "traversal", "absolute", "uppercase", "short", "foreign_novel",
    "audit_label", "boundary", "post", "baseline", "checkpoint", "plan", "transaction_json", "duplicate_intent", "nested_duplicate_intent", "duplicate_transaction", "foreign_root", "linked_root", "bool_boundary"])
def test_aut2_review_rejects_contradictory_intent_without_any_write(aut2_review_intent, monkeypatch, damage):
    import production_periodic_audit_finalization as final
    import historical_reconciliation as history
    from production_failure_classification import ProductionIntegrityError
    root, engine, args = aut2_review_intent
    value = final.load_intent(engine, 1, 10)
    plan = value["transaction_plan"]
    if damage in {"wrong_hex", "traversal", "absolute", "uppercase", "short"}:
        plan["transaction_id"] = {"wrong_hex": "0" * 64, "traversal": "../escaped",
            "absolute": str(root.parent / "escaped"), "uppercase": "A" * 64, "short": "a" * 63}[damage]
        value["record"]["historical_reconciliation"]["transaction_id"] = plan["transaction_id"]
    elif damage == "foreign_novel": plan["request"]["novel_id"] = "foreign"
    elif damage == "audit_label": plan["request"]["audit_label"] = "0002_0010"
    elif damage == "boundary": value["end"] = 11
    elif damage == "bool_boundary": value["start"] = True
    elif damage in {"post", "baseline"}: value[damage]["glossary"]["entries"]["韓"]["english"] = "Contradictory"
    elif damage == "checkpoint": engine.periodic_audit_checkpoint_path(1, 10).write_bytes(b"changed")
    elif damage == "plan":
        plan["chapters"][0]["edits"][0]["start"] += 1
        plan["transaction_id"] = canonical_hash({k: v for k, v in plan.items() if k != "transaction_id"})
        value["record"]["historical_reconciliation"]["transaction_id"] = plan["transaction_id"]
    elif damage in {"transaction_json", "duplicate_transaction"}:
        directory = engine.RETROACTIVE_DIR / "transactions"
        directory.mkdir(parents=True)
        slot = canonical_hash({k: plan["request"][k] for k in ("novel_id", "audit_label", "through_chapter")})
        raw = json.dumps(plan).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
        assert json.loads(raw) == plan
        (directory / (slot + ".plan.json")).write_bytes(b"{broken" if damage == "transaction_json" else raw.encode())
    _review_rehash_intent(engine, value)
    if damage == "foreign_root":
        monkeypatch.setattr(engine, "RETROACTIVE_DIR", root.parent / "foreign/retroactive_fixes")
    if damage == "linked_root":
        is_link = Path.is_symlink
        target = root / "output/publication_freshness"
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == target or is_link(p))
    if damage in {"duplicate_intent", "nested_duplicate_intent"}:
        path = final.intent_path(engine, 1, 10)
        raw = path.read_text(encoding="utf-8")
        needle = '"start": 1' if damage == "duplicate_intent" else '"audit_label": "0001_0010"'
        path.write_text(raw.replace(needle, needle + ', ' + needle, 1), encoding="utf-8")
    before = _review_snapshot(root.parent)
    with pytest.raises((ProductionIntegrityError, ValueError)):
        final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root.parent) == before
    assert not (root / "output/publication_freshness").exists()


@pytest.mark.parametrize("field", ["revision", "status", "transaction_id", "before_sha256"])
def test_aut2_review_duplicate_identical_receipt_never_certifies(aut2_review_intent, field):
    import production_periodic_audit_finalization as final
    import historical_reconciliation as history
    import build_publication_epub as publication
    from production_failure_classification import ProductionIntegrityError
    root, engine, args = aut2_review_intent
    final.finalize_periodic_audit(engine, 1, 10, *args)
    intent = final.load_intent(engine, 1, 10)
    path = history.invalidation_path(engine, intent)
    value = history.read_object(path)
    raw = json.dumps(value, ensure_ascii=False)
    import re
    match = re.search(r'"' + field + r'": "[^"\\]*"', raw)
    assert match is not None
    corrupted = raw[:match.end()] + ", " + match[0] + raw[match.end():]
    assert json.loads(corrupted) == value  # Demonstrates the old equality bypass.
    path.write_text(corrupted, encoding="utf-8")
    project = SimpleNamespace(root=root, audit_dir=engine.AUDIT_DIR, epub_dir=root / "output/epub",
                              config=engine.ACTIVE_NOVEL_CONFIG, is_legacy=False)
    before = _review_snapshot(root)
    for call in (publication.build_publication_epub, publication.verify_existing_publication_epub):
        with pytest.raises(ProductionIntegrityError):
            call(project)
    with pytest.raises(ProductionIntegrityError):
        final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root) == before


@pytest.mark.parametrize("stage", ["before_receipt", "after_receipt", "after_backup", "after_chapter"])
def test_aut2_review_invalidation_crash_restarts_in_original_transaction(aut2_review_intent, monkeypatch, stage):
    import production_periodic_audit_finalization as final
    import historical_reconciliation as history
    root, engine, args = aut2_review_intent
    intent = final.load_intent(engine, 1, 10)
    receipt = history.invalidation_path(engine, intent)
    write_json, write_bytes = history.atomic_write_json, tx.atomic_write_bytes
    def json_crash(path, value):
        if stage == "before_receipt": raise KeyboardInterrupt()
        write_json(path, value)
        if stage == "after_receipt": raise KeyboardInterrupt()
    def byte_crash(path, raw):
        assert history.read_object(receipt) == history.invalidation_record(intent)
        write_bytes(path, raw)
        if stage == "after_backup" or (stage == "after_chapter" and path.parent == engine.OUTPUT_DIR):
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(history, "atomic_write_json", json_crash)
        mp.setattr(tx, "atomic_write_bytes", byte_crash)
        with pytest.raises(KeyboardInterrupt):
            final.finalize_periodic_audit(engine, 1, 10, *args)
    original_intent = final.intent_path(engine, 1, 10).read_bytes()
    final.finalize_periodic_audit(engine, 1, 10, *args)
    assert final.intent_path(engine, 1, 10).read_bytes() == original_intent
    assert (engine.OUTPUT_DIR / "0010.md").read_bytes().count(b"Hann") == 1
    before = _review_snapshot(root)
    final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root) == before


def test_aut2_review_new_foreign_namespace_does_not_persist_intent(aut2_review_intent, monkeypatch):
    import production_periodic_audit_finalization as final
    from production_failure_classification import ProductionIntegrityError
    root, engine, args = aut2_review_intent
    final.intent_path(engine, 1, 10).unlink()
    monkeypatch.setattr(engine, "RETROACTIVE_DIR", root.parent / "foreign/retroactive_fixes")
    before = _review_snapshot(root.parent)
    with pytest.raises(ProductionIntegrityError, match="FOREIGN_INVALIDATION_ROOT"):
        final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root.parent) == before


def test_aut2_audit_inventory_ignores_uncommitted_source(aut2_review_intent):
    import production_periodic_audit_finalization as final
    root, engine, args = aut2_review_intent
    original = final.intent_path(engine, 1, 10).read_bytes()
    # Both an uncommitted row within the boundary and a future row are outside
    # this sparse fixture's exact committed set, [10].
    engine.chapter_map.update({1: {"text": "Earlier uncommitted source"},
                               11: {"text": "Future source"}})
    final.finalize_periodic_audit(engine, 1, 10, *args)
    engine.chapter_map[12] = {"text": "Later acquired source"}
    before = _review_snapshot(root)
    final.finalize_periodic_audit(engine, 1, 10, *args)
    assert final.intent_path(engine, 1, 10).read_bytes() == original
    assert _review_snapshot(root) == before


@pytest.mark.parametrize("damage", ["text", "missing", "identity"])
def test_aut2_audit_inventory_rejects_original_source_drift(aut2_review_intent, damage):
    import production_periodic_audit_finalization as final
    from production_failure_classification import ProductionIntegrityError
    root, engine, args = aut2_review_intent
    if damage == "missing":
        del engine.chapter_map[10]
    elif damage == "text":
        engine.chapter_map[10]["text"] += " changed"
    else:
        # Same source text substituted from a different original source unit.
        engine.chapter_map[10] = {**engine.chapter_map[10], "source_document_sha256": "different-unit"}
    before = _review_snapshot(root)
    with pytest.raises(ProductionIntegrityError, match="source hash mismatch|deterministic finalization drift"):
        final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root) == before


@pytest.fixture
def discovery_novel(tmp_path, monkeypatch):
    """Real local finalizer intent, stopped before ANY transaction writes."""
    import production_periodic_audit_finalization as final
    import historical_reconciliation as history
    serial = []

    def make(language="zh", count=26, extra=None, sources=None, english=None, action="correct", prior_record=False, prior_extra=None, committed_subset=None, prior_midpoint=None):
        key = "韓" if language == "zh" else "한민"
        root = tmp_path / f"public_novel_{len(serial)}"
        serial.append(root)
        output = root / "output/chapters"
        output.mkdir(parents=True)
        entries = {key: {"english": "Han", "category": "character", "status": "provisional"}, **(extra or {}), **(prior_extra or {})}
        units = {n: {"chapter": n, "production_index": n, "text": key if n in {1, 2, 25, 26, 388, 401} else "",
                     "source_document_sha256": canonical_hash([language, n])} for n in range(1, count + 1)}
        if prior_extra:
            units[3]["text"] = " ".join(prior_extra)
        for n, source in (sources or {}).items():
            units[n]["text"] = source.replace("KEY", key)
        for n in units:
            text = (english or {}).get(n, "Han met Han.\r\n" if n == 2 else
                "\ufeff# Héading 🦉\r\n\r\n**Han** — Hand, café.\r\n" if units[n]["text"] else "Ordinary prose.\r\n")
            (output / f"{n:04d}.md").write_bytes(text.encode())
        setup = dict(output_dir=output, retroactive_dir=root / "output/retroactive_fixes",
                     backup_dir=root / "backup", glossary={"entries": entries}, chapter_map=units)
        engine = install_engine(monkeypatch, setup)
        monkeypatch.setattr(engine.ACTIVE_PROFILE, "requires_formal_capitalization", lambda category: False, raising=False)
        monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", SimpleNamespace(id=root.name))
        engine.progress.update(completed=list(units), last_completed=count, next_chapter=count + 1,
                               last_audited_chapter=0)
        engine.save_json(engine.PROGRESS_FILE, engine.progress)
        original_apply = engine.apply_periodic_audit
        # If another novel was built in the same test, unwrap the explicit target adapter.
        original_apply = getattr(original_apply, "discovery_original", original_apply)
        # Finalize a genuine prior publication. A marker in audit_history cannot
        # witness historical ownership. No provider is used by these fixtures.
        prior_end = 25 if count == 26 else 375
        engine.AUDIT_DIR.mkdir(parents=True)
        def prior_apply(start, end, result, glossary_data=None):
            return original_apply(start, end, result, glossary_data=glossary_data,
                                  targets=[{"chinese": d["chinese"]} for d in result["decisions"]] or [{"chinese": key}])
        monkeypatch.setattr(engine, "apply_periodic_audit", prior_apply)
        prior_result = {"decisions": [{"chinese": term, "action": "reject"} for term in (prior_extra or {})], "issues": []}
        prior_stats = {field: 0 for field in final.STAT_TOTAL_FIELDS}
        prior_chunking = {"candidate_count": len(prior_extra or {}), "api_request_count": 0,
                          "candidate_set_hash": canonical_hash(prior_result)}
        prior_start = 1
        for end in ([prior_midpoint, prior_end] if prior_midpoint else [prior_end]):
            engine.save_json(engine.periodic_audit_checkpoint_path(prior_start, end), {"synthetic_prior_checkpoint": True})
            final.finalize_periodic_audit(engine, prior_start, end, prior_result, prior_stats, prior_chunking)
            prior_start = end + 1
            prior_result = {"decisions": [], "issues": []}
            prior_chunking = {"candidate_count": 0, "api_request_count": 0,
                              "candidate_set_hash": canonical_hash(prior_result)}
        def apply(start, end, result, glossary_data=None):
            return original_apply(start, end, result, glossary_data=glossary_data,
                                  targets=[{"chinese": d["chinese"]} for d in result["decisions"]] or [{"chinese": key}])
        apply.discovery_original = original_apply
        monkeypatch.setattr(engine, "apply_periodic_audit", apply)
        if prior_record:
            old_record = {"start_chapter": 1, "end_chapter": 25, "synthetic_accepted_history": True}
            engine.audit_history["audits"] = [old_record]
            final.intent_path(engine, 1, prior_end).unlink()
            engine.save_json(engine.AUDIT_HISTORY_FILE, engine.audit_history)
            engine.save_json(engine.AUDIT_DIR / "audit_0001_0025.json", old_record)
        if committed_subset is not None:
            engine.progress["completed"] = committed_subset
            engine.save_json(engine.PROGRESS_FILE, engine.progress)
        start = engine.progress["last_audited_chapter"] + 1
        checkpoint = engine.periodic_audit_checkpoint_path(start, count)
        engine.save_json(checkpoint, {"synthetic_accepted_decision": True})
        decision = {"chinese": key, "action": action, "english": "Hann"}
        if action == "contextual": decision["approved_renderings"] = ["Han", "Hann"]
        if action == "merge": decision["merge_with"] = next(iter(extra))
        result = {"decisions": [decision], "issues": []}
        stats = {field: 0 for field in final.STAT_TOTAL_FIELDS}
        chunking = {"candidate_count": 1, "api_request_count": 0, "candidate_set_hash": canonical_hash(result)}
        for folder, name in (("output/pending", "0402.md"), ("backup/preserved", "old.md"),
                             ("output/epub", "prior.epub")):
            directory = root / folder
            directory.mkdir(parents=True)
            (directory / name).write_bytes(b"Unrelated synthetic evidence; preserve exactly.\r\n")
        write = final.atomic_write_json
        def stop(path, value):
            write(path, value)
            if path.name.endswith(".finalization.json"): raise KeyboardInterrupt()
        with monkeypatch.context() as mp:
            mp.setattr(final, "atomic_write_json", stop)
            with pytest.raises(KeyboardInterrupt):
                final.finalize_periodic_audit(engine, start, count, result, stats, chunking)
        intent = final.load_intent(engine, start, count)
        proposals = intent["record"]["changes"]["retroactive_replacements"] or [
            rule(key, "Han", "", reason="Accepted contextual classification.")]
        kwargs = dict(through_chapter=count, audit_start=start, audit_end=count,
                      expected_intent_sha256=intent["intent_sha256"])
        def preflight(changes=None, **overrides):
            return history.historical_impact_preflight(engine, proposals if changes is None else changes,
                                                       **{**kwargs, **overrides})
        return SimpleNamespace(root=root, engine=engine, key=key, proposals=proposals, kwargs=kwargs,
                               preflight=preflight, args=(result, stats, chunking), intent=intent)
    return make


def _discovery_snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in root.rglob("*")}


@pytest.mark.parametrize("language", ["zh", "ko"])
def test_discovery_entire_history_crosses_windows_without_writing(discovery_novel, language):
    case = discovery_novel(language, count=401)
    before = _discovery_snapshot(case.root)
    memory = copy.deepcopy((case.engine.glossary, case.engine.progress, case.engine.audit_history, case.engine.chapter_map))
    report = case.preflight()
    assert report["examined_chapters"] == 401
    assert [row["chapter"] for row in report["chapters"] if row["decisions"][0]["outcome"] == "proven_eligible"] == [1, 25, 26, 388, 401]
    assert report["chapters"][1]["decisions"][0]["outcome"] == "deferred"
    assert report["potential_impact_chapters"] == 6
    assert report["corrected_chapters"] == report["provider_calls"] == 0
    assert case.preflight() == report
    assert _discovery_snapshot(case.root) == before
    assert (case.engine.glossary, case.engine.progress, case.engine.audit_history, case.engine.chapter_map) == memory
    encoded = json.dumps(report, ensure_ascii=False)
    for private in (case.key, "Han", "Héading", "café", "Unrelated synthetic", str(case.root)):
        assert private not in encoded
    assert len(list((case.engine.RETROACTIVE_DIR / "transactions").glob("*.plan.json"))) == 1
    assert report["phase"] == "DISCOVERY_NOT_COMMIT_AUTHORITY"


@pytest.mark.parametrize("sources,english,extra,reason", [
    ({1: "KEYKEY"}, {1: "Han met Han."}, {}, "repeated_source_occurrences_require_independent_pairing"),
    ({1: "KEY"}, {1: "Han met Han."}, {}, "repeated_target_occurrences_require_independent_pairing"),
    ({1: "KEY"}, {1: "Hand."}, {}, "cardinality_requires_manual_review"),
    ({1: "韓先生"}, {1: "Han."}, {"韓先生": {"english": "Master Han"}}, "source_absent_or_owned_by_longer_identity"),
    ({1: "KEY"}, {1: "Han."}, {"漢": {"english": "Han"}}, "competing_or_nested_target_ownership"),
    ({1: "KEY"}, {1: "Han's House."}, {"漢": {"english": "Han's House"}}, "competing_or_nested_target_ownership"),
    ({1: "KEY"}, {1: "Han."}, {"高": {"english": "Other", "aliases": ["韓"]}}, "competing_source_ownership"),
])
def test_discovery_preserves_existing_ownership_gates(discovery_novel, sources, english, extra, reason):
    case = discovery_novel(sources=sources, english=english, extra=extra)
    before = _discovery_snapshot(case.root)
    item = case.preflight()["chapters"][0]["decisions"][0]
    assert item["potential_impact"] and item["outcome"] == "deferred"
    assert reason in item["reason_codes"]
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("action,extra,reason", [
    ("contextual", {}, "contextual_identity_requires_manual_review"),
    ("merge", {"漢": {"english": "Hann", "category": "character"}}, "alias_identity_requires_manual_review"),
])
def test_discovery_accepted_alias_and_contextual_changes_defer(discovery_novel, action, extra, reason):
    case = discovery_novel(action=action, extra=extra)
    before = _discovery_snapshot(case.root)
    item = case.preflight()["chapters"][0]["decisions"][0]
    assert item["outcome"] == "deferred" and reason in item["reason_codes"]
    assert "MISSING_ACCEPTED_CHANGE_PROVENANCE" not in item["reason_codes"]
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("mode,reason", [("missing", "MISSING_ACCEPTED_CHANGE_PROVENANCE"),
    ("unaccepted", "CHANGE_NOT_IN_VERIFIED_AUDIT"), ("split", "ALIAS_SPLIT_PROVENANCE_UNAVAILABLE")])
def test_discovery_unwitnessed_proposals_never_gain_eligibility(discovery_novel, mode, reason):
    case = discovery_novel()
    proposal = copy.deepcopy(case.proposals)
    if mode == "unaccepted": proposal[0]["new_english"] = "Unaccepted Name"
    if mode == "split": proposal[0]["change_kind"] = "alias_split"
    before = _discovery_snapshot(case.root)
    kwargs = dict(audit_start=None, audit_end=None, expected_intent_sha256=None) if mode == "missing" else {}
    report = case.preflight(proposal, **kwargs)
    assert report["proven_eligible_chapters"] == report["already_current_chapters"] == 0
    assert reason in report["chapters"][0]["decisions"][0]["reason_codes"]
    assert _discovery_snapshot(case.root) == before


def test_discovery_current_bytes_after_existing_transaction_and_selected_boundary(discovery_novel):
    import production_periodic_audit_finalization as final
    case = discovery_novel()
    final.finalize_periodic_audit(case.engine, 26, 26, *case.args)
    before = _discovery_snapshot(case.root)
    report = case.preflight(through_chapter=25)
    assert report["examined_chapters"] == 25 and report["already_current_chapters"] == 2
    assert report["chapters"][0]["decisions"][0]["outcome"] == "already_current"
    assert report["proven_eligible_chapters"] == 0
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("damage", ["source", "source_unit", "missing_source", "english", "missing_chapter",
    "extra_chapter", "reordered", "duplicate", "glossary", "intent", "record", "checkpoint", "missing_intent",
    "before_backup", "duplicate_json", "foreign_novel", "foreign_root", "plan", "policy", "nested_chapter"])
def test_discovery_drift_blocks_without_mutation(discovery_novel, damage, monkeypatch):
    import production_periodic_audit_finalization as final
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel()
    e = case.engine
    if damage in {"before_backup", "record", "plan"}:
        final.finalize_periodic_audit(e, 26, 26, *case.args)
    if damage == "source": e.chapter_map[1]["text"] += "changed"
    elif damage == "source_unit": e.chapter_map[1]["source_document_sha256"] = "different"
    elif damage == "missing_source": del e.chapter_map[1]
    elif damage == "english": (e.OUTPUT_DIR / "0001.md").write_bytes(b"edited outside")
    elif damage == "missing_chapter": (e.OUTPUT_DIR / "0001.md").unlink()
    elif damage == "extra_chapter": (e.OUTPUT_DIR / "0027.md").write_bytes(b"foreign")
    elif damage == "nested_chapter":
        (e.OUTPUT_DIR / "hidden").mkdir()
        (e.OUTPUT_DIR / "hidden/0001.md").write_bytes(b"unrecorded")
    elif damage == "reordered": e.progress["completed"].reverse()
    elif damage == "duplicate": e.progress["completed"].append(1)
    elif damage == "glossary": e.glossary["entries"][case.key]["english"] = "Changed"
    elif damage == "intent":
        value = copy.deepcopy(case.intent)
        value["record"]["changes"]["corrected"].append("tampered")
        final.atomic_write_json(final.intent_path(e, 26, 26), value)
    elif damage == "record": (e.AUDIT_DIR / "audit_0026_0026.json").write_bytes(b"{}")
    elif damage == "checkpoint": e.periodic_audit_checkpoint_path(26, 26).write_bytes(b"{}")
    elif damage == "missing_intent": final.intent_path(e, 26, 26).unlink()
    elif damage == "before_backup": next(e.BACKUP_DIR.rglob("0001.md")).unlink()
    elif damage == "duplicate_json":
        path = final.intent_path(e, 26, 26)
        path.write_bytes(path.read_bytes().replace(b'"start": 26', b'"start": 26, "start": 26'))
    elif damage == "foreign_novel": e.ACTIVE_NOVEL_CONFIG.id = "foreign"
    elif damage == "foreign_root": monkeypatch.setattr(e, "RETROACTIVE_DIR", case.root.parent / "other/output/retroactive_fixes")
    elif damage == "plan": next((e.RETROACTIVE_DIR / "transactions").glob("*.plan.json")).write_bytes(b"{}")
    elif damage == "policy": e.ACTIVE_PROFILE.retroactive_safe_categories = set()
    before = _discovery_snapshot(case.root.parent)
    with pytest.raises((ProductionIntegrityError, OSError, KeyError, ValueError)):
        case.preflight()
    assert _discovery_snapshot(case.root.parent) == before


def test_discovery_duplicate_changes_and_bounds_fail_closed(discovery_novel):
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel()
    before = _discovery_snapshot(case.root)
    for changes, options in [(case.proposals * 2, {}), ([], {}), (case.proposals, {"through_chapter": True}),
                             (case.proposals, {"through_chapter": 10001})]:
        with pytest.raises(ProductionIntegrityError): case.preflight(changes, **options)
    assert _discovery_snapshot(case.root) == before


def test_discovery_multi_novel_isolation(discovery_novel):
    first = discovery_novel("zh")
    original = _discovery_snapshot(first.root)
    second = discovery_novel("ko")
    report = second.preflight()
    assert report["proven_eligible_chapters"] == 3
    assert report["novel_id_sha256"] != canonical_hash(first.root.name)
    assert _discovery_snapshot(first.root) == original
    from production_failure_classification import ProductionIntegrityError
    with pytest.raises(ProductionIntegrityError, match="DISCOVERY_INTENT_DRIFT"):
        second.preflight(expected_intent_sha256=first.intent["intent_sha256"])
    assert _discovery_snapshot(first.root) == original


def test_discovery_locked_entry_cannot_manufacture_accepted_change(discovery_novel):
    case = discovery_novel(extra={"韓": {"english": "Han", "category": "character", "status": "locked"}})
    before = _discovery_snapshot(case.root)
    report = case.preflight([rule()])
    assert report["proven_eligible_chapters"] == 0
    assert "CHANGE_NOT_IN_VERIFIED_AUDIT" in report["chapters"][0]["decisions"][0]["reason_codes"]
    assert _discovery_snapshot(case.root) == before


def test_discovery_already_current_requires_independent_ownership(discovery_novel):
    case = discovery_novel(english={1: "Hann."}, extra={"漢": {"english": "Hann"}})
    before = _discovery_snapshot(case.root)
    item = case.preflight()["chapters"][0]["decisions"][0]
    assert item["outcome"] == "deferred"
    assert "competing_or_nested_target_ownership" in item["reason_codes"]
    assert _discovery_snapshot(case.root) == before


def test_discovery_rechecks_evidence_after_computation(discovery_novel, monkeypatch):
    import historical_reconciliation as history
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel()
    original = history.plan_chapter
    fired = []
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            (case.engine.OUTPUT_DIR / "0001.md").write_bytes(b"outside writer")
        return result
    monkeypatch.setattr(history, "plan_chapter", changed)
    with pytest.raises(ProductionIntegrityError, match="DISCOVERY_INPUT_DRIFT"):
        case.preflight()
    assert (case.engine.OUTPUT_DIR / "0001.md").read_bytes() == b"outside writer"


def test_discovery_selection_order_does_not_change_report(discovery_novel):
    case = discovery_novel()
    proposals = case.proposals + [rule(case.key, "Han", "Unsupported Name")]
    before = _discovery_snapshot(case.root)
    assert case.preflight(proposals) == case.preflight(list(reversed(proposals)))
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("damage", ["missing", "tampered", "duplicate"])
def test_discovery_reproves_prior_audit_records(discovery_novel, damage):
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel(prior_record=True)
    path = case.engine.AUDIT_DIR / "audit_0001_0025.json"
    if damage == "missing": path.unlink()
    elif damage == "tampered": path.write_bytes(b"{}")
    else: path.write_bytes(b'{"start_chapter":1,"start_chapter":1,"end_chapter":25,"synthetic_accepted_history":true}')
    before = _discovery_snapshot(case.root)
    with pytest.raises(ProductionIntegrityError): case.preflight()
    assert _discovery_snapshot(case.root) == before


def test_aut2_retained_contradictory_receipt_blocks_restart_without_writes(aut2_review_intent):
    import production_periodic_audit_finalization as final
    import historical_reconciliation as history
    from production_failure_classification import ProductionIntegrityError
    root, engine, args = aut2_review_intent
    final.finalize_periodic_audit(engine, 1, 10, *args)
    intent = final.load_intent(engine, 1, 10)
    receipt = history.invalidation_path(engine, intent)
    value = history.read_object(receipt)
    value["audit_intent_sha256"] = "0" * 64
    receipt.write_text(json.dumps(value), encoding="utf-8")
    before = _review_snapshot(root)
    with pytest.raises(ProductionIntegrityError, match="FRESHNESS_MANIFEST_DRIFT"):
        final.finalize_periodic_audit(engine, 1, 10, *args)
    assert _review_snapshot(root) == before


@pytest.mark.parametrize("competitor,entry,source,english,reason", [
    ("韓先生", {"english": "Master Han", "category": "character"}, "韓先生", "Han.",
     "source_absent_or_owned_by_longer_identity"),
    ("漢", {"english": "Han", "category": "character"}, "韓", "Han.",
     "competing_or_nested_target_ownership"),
    ("漢", {"english": "Other", "category": "character", "accepted_inflections": ["Han"]}, "韓", "Han.",
     "competing_or_nested_target_ownership"),
    ("漢", {"english": "Other", "category": "character", "source_variants": ["韓先生"]}, "韓先生", "Han.",
     "source_absent_or_owned_by_longer_identity"),
    ("漢", {"english": "Han's House", "category": "character"}, "韓", "Han's House.",
     "competing_or_nested_target_ownership"),
])
def test_discovery_d4_d5_verified_prior_intent_reserves_removed_identity(
        discovery_novel, competitor, entry, source, english, reason):
    import production_periodic_audit_finalization as final
    case = discovery_novel(prior_extra={competitor: entry}, sources={1: source}, english={1: english})
    prior = final.load_intent(case.engine, 1, 25)
    assert competitor in prior["baseline"]["glossary"]["entries"]
    assert competitor not in prior["post"]["glossary"]["entries"]
    assert competitor not in case.intent["baseline"]["glossary"]["entries"]
    # This exposes the old writer's unsafe proposal without executing it.
    assert case.intent["transaction_plan"]["chapters"][0]["edits"]
    before = _discovery_snapshot(case.root)
    report = case.preflight()
    item = report["chapters"][0]["decisions"][0]
    assert item["outcome"] == "deferred" and item["potential_impact"]
    assert "HISTORICAL_OWNERSHIP_RESERVATION" in item["reason_codes"]
    assert reason in item["reason_codes"]
    assert report["corrected_chapters"] == report["provider_calls"] == 0
    assert case.preflight() == report
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("damage", ["missing_intent", "intent_hash", "rehashed_glossary", "missing_checkpoint",
    "missing_transaction", "missing_state_backup", "source_unit", "history_fork", "foreign_novel", "missing_record"])
def test_discovery_historical_ownership_missing_or_contradictory_blocks(discovery_novel, damage):
    import production_periodic_audit_finalization as final
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel(prior_extra={"漢": {"english": "Han", "category": "character"}})
    e = case.engine
    path = final.intent_path(e, 1, 25)
    value = final.load_intent(e, 1, 25)
    if damage == "missing_intent": path.unlink()
    elif damage == "intent_hash": path.write_bytes(b"{}")
    elif damage == "missing_checkpoint": e.periodic_audit_checkpoint_path(1, 25).unlink()
    elif damage == "missing_transaction":
        next((e.RETROACTIVE_DIR / "transactions").glob("*.plan.json")).unlink()
    elif damage == "missing_state_backup": (e.BACKUP_DIR / "checkpoint_0025" / e.GLOSSARY_FILE.name).unlink()
    elif damage == "source_unit": e.chapter_map[1]["source_document_sha256"] = "changed"
    elif damage == "missing_record": (e.AUDIT_DIR / "audit_0001_0025.json").unlink()
    else:
        if damage == "rehashed_glossary": value["baseline"]["glossary"]["entries"]["漢"]["english"] = "Changed"
        elif damage == "history_fork": value["baseline"]["history"]["audits"] = [{"unproven": True}]
        elif damage == "foreign_novel": value["transaction_plan"]["request"]["novel_id"] = "foreign"
        value["historical_proof"]["plan_sha256"] = canonical_hash(value["transaction_plan"])
        value["intent_sha256"] = canonical_hash({k: v for k, v in value.items() if k != "intent_sha256"})
        final.atomic_write_json(path, value)
    before = _discovery_snapshot(case.root)
    with pytest.raises(ProductionIntegrityError): case.preflight()
    assert _discovery_snapshot(case.root) == before


def test_discovery_history_marker_alone_is_not_a_witness(discovery_novel):
    from production_failure_classification import ProductionIntegrityError
    case = discovery_novel(prior_record=True)
    before = _discovery_snapshot(case.root)
    with pytest.raises(ProductionIntegrityError, match="HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN"):
        case.preflight()
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("language,unknown", [("zh", "韩"), ("zh", "無關"), ("ko", "한"), ("ko", "한민")])
def test_discovery_j5b_unknown_source_identity_is_coverage_unproven(discovery_novel, language, unknown):
    case = discovery_novel(language, sources={1: unknown}, english={1: "Nobody arrived."})
    before = _discovery_snapshot(case.root)
    report = case.preflight()
    row = report["chapters"][0]
    item = row["decisions"][0]
    assert item["outcome"] == "coverage_unproven"
    assert item["reason_codes"] == ["SOURCE_IDENTITY_COVERAGE_INCOMPLETE"]
    assert not item["potential_impact"] and item["source_owned_count"] == 0
    assert row["source_identity_coverage"] == "incomplete" and row["unowned_source_script_count"] > 0
    assert report["coverage_unproven_chapters"] == 1
    assert _discovery_snapshot(case.root) == before


def test_discovery_unaffected_requires_literal_coverage(discovery_novel):
    case = discovery_novel(extra={"無關": {"english": "Other Person", "category": "character"}},
                           sources={1: "無關"}, english={1: "Other Person arrived."})
    before = _discovery_snapshot(case.root)
    row = case.preflight()["chapters"][0]
    assert row["decisions"][0]["outcome"] == "unaffected"
    assert row["source_identity_coverage"] == "literal_inventory_covered"
    assert _discovery_snapshot(case.root) == before


def test_discovery_chapter_300_stays_eligible(discovery_novel):
    case = discovery_novel(count=401, sources={300: "KEY"}, english={300: "Han arrived."})
    before = _discovery_snapshot(case.root)
    report = case.preflight()
    assert report["chapters"][299]["decisions"][0]["outcome"] == "proven_eligible"
    assert _discovery_snapshot(case.root) == before


@pytest.mark.parametrize("competitor,entry,source", [
    ("韓先生", {"english": "Master Han", "category": "character"}, "韓先生"),
    ("漢", {"english": "Han", "category": "character"}, "韓"),
])
def test_legacy_writer_risk_d4_d5_reaches_actual_finalizer(discovery_novel, competitor, entry, source):
    """Characterize an unresolved safety gap on disposable data, not approval."""
    import production_periodic_audit_finalization as final
    case = discovery_novel(prior_extra={competitor: entry}, sources={1: source}, english={1: "Han."})
    e = case.engine
    before = _discovery_snapshot(case.root)
    assert case.preflight()["chapters"][0]["decisions"][0]["outcome"] == "deferred"
    assert _discovery_snapshot(case.root) == before
    # The production finalizer validates this same source-bound immutable audit,
    # then the unchanged writer incorrectly edits the ambiguous referent.
    result = final.finalize_periodic_audit(e, 26, 26, *case.args)
    assert result["status"] == "FINALIZED"
    assert (e.OUTPUT_DIR / "0001.md").read_bytes() == b"Hann."
    assert final.load_intent(e, 26, 26) == case.intent
    proof = final.transaction_proof(e, 26, 26, case.intent["baseline"]["glossary"],
        case.intent["post"]["glossary"], case.intent["record"]["changes"])
    assert proof["state"]["state"] == "FINALIZED"


@pytest.mark.parametrize("subset", [[], list(range(2, 27))])
def test_legacy_writer_risk_j3_j4_sparse_progress_reaches_finalizer_seam(discovery_novel, subset):
    """Normal startup blocks, but the mutation boundary must also verify it."""
    import production_periodic_audit_finalization as final
    from production_failure_classification import ProductionIntegrityError
    from noveltranslator_observability import progress_commit_integrity_errors
    case = discovery_novel(committed_subset=subset)
    e = case.engine
    assert progress_commit_integrity_errors(e.progress, e.OUTPUT_DIR, case.root / "output/pending")
    before = _discovery_snapshot(case.root)
    with pytest.raises(ProductionIntegrityError, match="NONSEQUENTIAL_COMMITTED_INVENTORY"):
        case.preflight()
    assert _discovery_snapshot(case.root) == before
    assert case.intent["transaction_plan"]["request"]["committed_chapters"] == subset
    result = final.finalize_periodic_audit(e, 26, 26, *case.args)
    assert result["status"] == "FINALIZED"
    assert (e.OUTPUT_DIR / "0001.md").read_bytes() == before["output/chapters/0001.md"][0]
    if subset:
        assert b"Hann" in (e.OUTPUT_DIR / "0025.md").read_bytes()



def test_discovery_reservations_survive_multiple_verified_prior_intents(discovery_novel):
    import production_periodic_audit_finalization as final
    case = discovery_novel(prior_extra={"漢": {"english": "Han", "category": "character"}}, prior_midpoint=12)
    first = final.load_intent(case.engine, 1, 12)
    second = final.load_intent(case.engine, 13, 25)
    assert "漢" in first["baseline"]["glossary"]["entries"]
    assert "漢" not in second["baseline"]["glossary"]["entries"]
    before = _discovery_snapshot(case.root)
    item = case.preflight()["chapters"][0]["decisions"][0]
    assert item["outcome"] == "deferred"
    assert "HISTORICAL_OWNERSHIP_RESERVATION" in item["reason_codes"]
    assert _discovery_snapshot(case.root) == before
