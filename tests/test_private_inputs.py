"""Synthetic tests for typed private-input schemas and glossary validation."""
import json

import pytest

import upgrade_glossary
from private_inputs import (
    PrivateInputError,
    load_glossary_upgrade_config,
    load_normalization_inputs,
    load_title_inputs,
)


def _norm_doc():
    return {
        "schema_version": 1,
        "kind": "normalization",
        "values": {
            "EXPECTED_FINAL_COUNT": 3,
            "EXPECTED_FINAL_SHA256": "a" * 64,
            "APPROVED_RULES": [["alpha", "Alpha", "Alpha Prime", ["alpha"]]],
            "FALSE_POSITIVE_RULES": [["beta", "Beta", "reviewed"]],
            "EXPLICIT_REVIEW_VARIANTS": [["gamma", "Gamma"]],
            "ROGUE_EDITORIAL_FAMILY_SPECS": [{
                "family_id": "family-1",
                "source_identity": "delta",
                "source_forms": ["delta"],
                "source_form_roles": {"delta": "core"},
                "target_variants": ["Delta"],
            }],
            "SEMANTICALLY_APPROVED_TITLES": {"t1": {"title": "One"}},
            "TITLE_REVIEW_OVERRIDES": {"t2": {"title": "Two"}},
            "SCOPED_REVIEW_CORRECTIONS": [["epsilon", ["epsilon"], "Epsilon", "Epsilon Prime", {"unit": 1}]],
            "REVIEW_FALSE_POSITIVE_KEYS": [["zeta", "Zeta"]],
            "REVIEW_APPROVED_KEYS": [["eta", "Eta"]],
        },
        "records": {
            "REVIEW_FALSE_POSITIVE_SCOPES": [
                {"key": {"type": "tuple", "items": [
                    {"type": "str", "value": "zeta"}, {"type": "str", "value": "Zeta"}]},
                 "value": {"units": [1]}},
            ],
            "FALSE_REASONS": [
                {"key": {"type": "tuple", "items": [
                    {"type": "str", "value": "zeta"}, {"type": "str", "value": "Zeta"}]},
                 "value": "reviewed false positive"},
            ],
        },
    }


def _title_doc():
    return {
        "schema_version": 1,
        "kind": "title_resolution",
        "values": {
            "EXPECTED_NORMALIZED": {"file_count": 1},
            "EXPECTED_BASELINE": {"file_count": 1},
            "EXPECTED_INPUT_COUNTS": {"approved": 1},
            "HUMAN_DOWNGRADED_IDS": ["g1"],
            "ROGUE_REVIEW_RESOLUTIONS": {"r1": {"status": "review"}},
            "ROGUE_UNRESOLVED_NOTES": {},
        },
        "records": {
            "ROGUE_AUTHORIZED_RECOVERIES": [
                {"key": {"type": "int", "value": 7}, "value": {"title": "Seven"}},
                {"key": {"type": "int", "value": 9}, "value": {"title": "Nine"}},
            ],
        },
    }


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_tuple_and_integer_record_keys_round_trip(tmp_path):
    norm = load_normalization_inputs(_write(tmp_path, "n.json", _norm_doc()))
    assert norm["REVIEW_FALSE_POSITIVE_SCOPES"][("zeta", "Zeta")] == {"units": (1,)}
    assert norm["APPROVED_RULES"] == (("alpha", "Alpha", "Alpha Prime", ("alpha",)),)
    title = load_title_inputs(_write(tmp_path, "t.json", _title_doc()))
    assert title["ROGUE_AUTHORIZED_RECOVERIES"][7] == {"title": "Seven"}
    assert title["ROGUE_AUTHORIZED_RECOVERIES"][9] == {"title": "Nine"}
    assert title["HUMAN_DOWNGRADED_IDS"] == {"g1"}


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "dup.json"
    path.write_text('{"schema_version": 1, "kind": "normalization", "values": {}, "values": {}}', encoding="utf-8")
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(path)


def test_duplicate_record_key_is_rejected(tmp_path):
    doc = _norm_doc()
    doc["records"]["REVIEW_FALSE_POSITIVE_SCOPES"].append(
        dict(doc["records"]["REVIEW_FALSE_POSITIVE_SCOPES"][0])
    )
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "dup_record.json", doc))


def test_version_kind_and_required_settings_are_enforced(tmp_path):
    doc = _norm_doc()
    doc["schema_version"] = 2
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "v2.json", doc))
    doc = _norm_doc()
    doc["kind"] = "title_resolution"
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "kind.json", doc))
    doc = _norm_doc()
    del doc["values"]["APPROVED_RULES"]
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "missing.json", doc))
    doc = _norm_doc()
    doc["values"]["APPROVED_RULES"] = []
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "empty.json", doc))


def test_malformed_record_and_unsupported_key_are_rejected(tmp_path):
    doc = _norm_doc()
    doc["records"]["REVIEW_FALSE_POSITIVE_SCOPES"][0]["key"] = {"type": "float", "value": 1.5}
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "bad_key.json", doc))
    doc = _norm_doc()
    doc["values"]["UNKNOWN_SETTING"] = 1
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "unknown.json", doc))


def test_malformed_approved_rule_arity_is_rejected(tmp_path):
    doc = _norm_doc()
    doc["values"]["APPROVED_RULES"] = [["alpha", "Alpha"]]
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "arity.json", doc))


def test_string_count_is_rejected(tmp_path):
    doc = _norm_doc()
    doc["values"]["EXPECTED_FINAL_COUNT"] = "3"
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "count.json", doc))


def test_missing_false_reason_coverage_is_rejected(tmp_path):
    doc = _norm_doc()
    doc["records"]["FALSE_REASONS"] = []
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "reasons.json", doc))


def test_string_key_in_integer_recovery_field_is_rejected(tmp_path):
    doc = _title_doc()
    doc["records"]["ROGUE_AUTHORIZED_RECOVERIES"][0]["key"] = {"type": "str", "value": "7"}
    with pytest.raises(PrivateInputError):
        load_title_inputs(_write(tmp_path, "recovery_key.json", doc))


def test_boolean_schema_version_is_rejected(tmp_path):
    doc = _norm_doc()
    doc["schema_version"] = True
    with pytest.raises(PrivateInputError):
        load_normalization_inputs(_write(tmp_path, "bool_version.json", doc))


def _glossary_config(**overrides):
    values = {
        "remove_terms": [], "locked_names": {}, "locked_terms": {},
        "locked_entries": [], "provisional_entries": [],
        "normalize_capitalization": {}, "overrides": [], "replace_entries": [],
    }
    values.update(overrides)
    return {"schema_version": 1, "kind": "glossary_upgrade", "values": values}


def _glossary(tmp_path, entries):
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_apply_config_never_mutates_the_input(tmp_path):
    entries = {"alpha": {"english": "Alpha", "status": "locked"}, "beta": {"english": "Beta"}}
    config = load_glossary_upgrade_config(_write(tmp_path, "c.json", _glossary_config(
        remove_terms=["alpha"], locked_names={"beta": "Beta Reviewed"})))
    working, removed = upgrade_glossary.apply_config(entries, config)
    assert removed == ["alpha"]
    assert "alpha" not in working and working["beta"]["english"] == "Beta Reviewed"
    assert "alpha" in entries and entries["beta"]["english"] == "Beta"


def test_valid_glossary_approved_renderings_lists_are_preserved(tmp_path):
    entries = {"alpha": {"english": "Alpha", "status": "locked"}}
    config = load_glossary_upgrade_config(_write(tmp_path, "rendering.json", _glossary_config(
        overrides=[{"term": "alpha", "entry": {"approved_renderings": ["Alpha", "Alfa"]}}],
    )))
    assert isinstance(config["overrides"], list)
    assert isinstance(config["overrides"][0]["entry"]["approved_renderings"], list)
    working, _ = upgrade_glossary.apply_config(entries, config)
    assert working["alpha"]["approved_renderings"] == ["Alpha", "Alfa"]

    replacement = load_glossary_upgrade_config(_write(tmp_path, "replace.json", _glossary_config(
        replace_entries=[{"term": "alpha", "entry": {
            "english": "Alpha Canonical", "approved_renderings": ["Alpha Canonical"]}}],
    )))
    working, _ = upgrade_glossary.apply_config(entries, replacement)
    assert working["alpha"]["approved_renderings"] == ["Alpha Canonical"]


def test_invalid_glossary_edit_leaves_file_unchanged(tmp_path):
    glossary = _glossary(tmp_path, {"alpha": {"english": "Alpha"}})
    before = glossary.read_bytes()
    config = _write(tmp_path, "bad.json", _glossary_config(
        remove_terms=["alpha"],
        replace_entries=[{"term": "alpha", "entry": "not-an-object"}]))
    assert upgrade_glossary.main([str(glossary), str(config)]) == 2
    assert glossary.read_bytes() == before


def test_malformed_rendering_is_rejected(tmp_path):
    with pytest.raises(upgrade_glossary.GlossaryValidationError):
        upgrade_glossary.apply_config(
            {"alpha": {"english": "Alpha"}},
            load_glossary_upgrade_config(_write(tmp_path, "bad_render.json", _glossary_config(
                overrides=[{"term": "alpha", "entry": {"approved_renderings": "not-a-list"}}]))),
        )


def test_interrupted_write_leaves_target_unchanged(tmp_path, monkeypatch):
    glossary = _glossary(tmp_path, {"alpha": {"english": "Alpha"}})
    before = glossary.read_bytes()
    config = _write(tmp_path, "ok.json", _glossary_config(locked_names={"alpha": "Alpha Locked"}))

    def fail_fsync(handle):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(upgrade_glossary.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        upgrade_glossary.main([str(glossary), str(config)])
    assert glossary.read_bytes() == before


def test_valid_glossary_edit_is_applied_atomically(tmp_path):
    glossary = _glossary(tmp_path, {"alpha": {"english": "Alpha"}, "beta": {"english": "Beta"}})
    config = _write(tmp_path, "ok.json", _glossary_config(
        remove_terms=["alpha"], locked_terms={"beta": "Beta Locked"},
        normalize_capitalization={"beta": "Beta Canonical"}))
    assert upgrade_glossary.main([str(glossary), str(config)]) == 0
    result = json.loads(glossary.read_text(encoding="utf-8"))
    assert "alpha" not in result["entries"]
    assert result["entries"]["beta"]["english"] == "Beta Canonical"
    assert result["entries"]["beta"]["status"] == "locked"
    assert not (tmp_path / "glossary.json.tmp").exists()
