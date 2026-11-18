"""A periodic audit may not change an entry settled before translation; it is held for review."""
from pathlib import Path

import pytest

import translate_production_unattended_scheduled_retroactive_dynamic_pricing as production
from translation_profile import load_translation_profile

SETTLED = {"origin": "manual", "verification": "explicit_manual", "note": "settled before translation"}
EXTRACTED = {"origin": "chapter_state_extraction", "chapter": 3, "verification": "exact_literal"}


@pytest.fixture(autouse=True)
def profile(monkeypatch):
    monkeypatch.setattr(production, "ACTIVE_PROFILE",
                        load_translation_profile("xianxia_xuanhuan", Path(__file__).resolve().parents[1]))


def glossary():
    return {"entries": {
        "青国": {"english": "Azure State", "category": "state", "status": "provisional", "source_provenance": dict(SETTLED)},
        "红国": {"english": "Crimson State", "category": "state", "status": "provisional",
                 "source_provenance": dict(EXTRACTED)},
        "青都": {"english": "Azure Capital", "category": "location", "status": "provisional",
                 "source_provenance": dict(SETTLED)},
    }}


def audit(*decisions):
    data = glossary()
    targets = [{"chinese": key} for key in data["entries"]]
    changes = production.apply_periodic_audit(1, 5, {"decisions": list(decisions)}, glossary_data=data, targets=targets)
    return data["entries"], changes


@pytest.mark.parametrize("decision, proposed", [
    ({"action": "correct", "english": "Blue Kingdom"}, "Blue Kingdom"),
    ({"action": "reject"}, "remove the entry"),
    ({"action": "merge", "merge_with": "青都"}, "merge into 青都"),
    ({"action": "contextual", "approved_renderings": ["Azure State", "Blue Kingdom"]},
     "contextual: Azure State / Blue Kingdom"),
])
def test_a_settled_entry_is_held_not_changed(decision, proposed):
    entries, changes = audit({"chinese": "青国", "notes": "Reads better.", **decision})
    assert entries["青国"] == glossary()["entries"]["青国"]
    assert changes["held"] == [{"chinese": "青国", "action": decision["action"], "current": "Azure State",
                                "proposed": proposed, "notes": "Reads better."}]
    assert "青国" not in changes["kept"] and changes["retroactive_replacements"] == []


def test_an_entry_the_run_found_is_still_corrected():
    entries, changes = audit({"chinese": "红国", "action": "correct", "english": "Scarlet State"})
    assert entries["红国"]["english"] == "Scarlet State" and changes["held"] == []
    assert [rule["new_english"] for rule in changes["retroactive_replacements"]] == ["Scarlet State"]


def test_a_decision_that_changes_nothing_is_not_held():
    entries, changes = audit({"chinese": "青国", "action": "correct", "english": "Azure State"},
                             {"chinese": "青都", "action": "merge", "merge_with": "无此"})
    assert changes["held"] == [] and entries == glossary()["entries"]


def test_a_capitalization_only_correction_to_a_technique_is_not_held():
    data = {"entries": {"黑虎掏心": {"english": "Black Tiger Seizes The Heart", "category": "martial technique",
                                    "status": "provisional", "source_provenance": dict(SETTLED)}}}
    changes = production.apply_periodic_audit(
        1, 5, {"decisions": [{"chinese": "黑虎掏心", "action": "correct", "english": "Black Tiger seizes the heart"}]},
        glossary_data=data, targets=[{"chinese": "黑虎掏心"}])
    assert changes["held"] == [] and data["entries"]["黑虎掏心"]["english"] == "Black Tiger Seizes The Heart"
