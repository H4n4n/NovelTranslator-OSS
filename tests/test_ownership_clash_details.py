"""The ownership veto names what clashed and how to resolve it, keeping its reason code first."""
from historical_reconciliation import ownership_clash_details


def _edit(old, new, source="Synthetic Hall"):
    return {"source_identity": source, "old_text": old, "replacement": new}


def test_no_veto_adds_nothing():
    assert ownership_clash_details([]) == ""


def test_vetoed_edits_are_grouped_by_change_with_their_chapters():
    detail = ownership_clash_details([(291, _edit("Azure Hall", "Blue Hall")), (291, _edit("Azure Hall", "Blue Hall")),
                                      (397, _edit("Azure Hall", "Blue Hall")), (12, _edit("Red Gate", "Crimson Gate", "Gate"))])
    message = "HISTORICAL_OWNERSHIP_RESERVATION" + detail
    assert message.startswith("HISTORICAL_OWNERSHIP_RESERVATION: ")
    assert "Synthetic Hall: 'Azure Hall' -> 'Blue Hall' in chapter(s) 291, 397 (3 place(s))" in message
    assert "Gate: 'Red Gate' -> 'Crimson Gate' in chapter(s) 12 (1 place(s))" in message
    assert "--apply-corrections" in message and "goat_periodic_audit_finalize_execute.py" in message
