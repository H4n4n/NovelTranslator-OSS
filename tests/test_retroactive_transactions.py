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
