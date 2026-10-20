"""Versioned, typed loader for operator-local private inputs.

JSON cannot represent tuple or integer object keys. This module defines a small,
explicit schema that restores those key types deterministically without eval,
exec or pickle, and validates the shape each consuming planner actually requires.

Document shape (schema_version 1):

    {
      "schema_version": 1,
      "kind": "normalization" | "title_resolution" | "glossary_upgrade",
      "values": { "<NAME>": <plain JSON value>, ... },
      "records": {
        "<NAME>": [
          {"key": {"type": "str", "value": "..."} , "value": <plain JSON value>},
          {"key": {"type": "int", "value": 7}       , "value": ...},
          {"key": {"type": "tuple", "items": [ ... ]}, "value": ...}
        ]
      }
    }

Normalization/title values are frozen to tuples (and named fields to sets).
Glossary values keep native JSON lists because the glossary contract requires
lists such as approved_renderings. Duplicate keys, boolean versions, unknown
fields, missing/empty required settings, wrong rule arity, wrong numeric types,
wrong record-key types and incomplete false-reason coverage are rejected.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1
_TOP_LEVEL = {"schema_version", "kind", "values", "records"}

REQUIRED = {
    "normalization": {
        "EXPECTED_FINAL_COUNT", "EXPECTED_FINAL_SHA256", "APPROVED_RULES",
        "FALSE_POSITIVE_RULES", "EXPLICIT_REVIEW_VARIANTS", "ROGUE_EDITORIAL_FAMILY_SPECS",
        "SEMANTICALLY_APPROVED_TITLES", "TITLE_REVIEW_OVERRIDES", "SCOPED_REVIEW_CORRECTIONS",
        "REVIEW_FALSE_POSITIVE_KEYS", "REVIEW_FALSE_POSITIVE_SCOPES", "REVIEW_APPROVED_KEYS",
    },
    "title_resolution": {
        "EXPECTED_NORMALIZED", "EXPECTED_BASELINE", "EXPECTED_INPUT_COUNTS",
        "HUMAN_DOWNGRADED_IDS", "ROGUE_AUTHORIZED_RECOVERIES",
        "ROGUE_REVIEW_RESOLUTIONS", "ROGUE_UNRESOLVED_NOTES",
    },
    "glossary_upgrade": {
        "remove_terms", "locked_names", "locked_terms", "locked_entries",
        "provisional_entries", "normalize_capitalization", "overrides", "replace_entries",
    },
}

NONEMPTY = {
    "normalization": {
        "APPROVED_RULES", "FALSE_POSITIVE_RULES", "SEMANTICALLY_APPROVED_TITLES",
        "TITLE_REVIEW_OVERRIDES", "SCOPED_REVIEW_CORRECTIONS", "REVIEW_FALSE_POSITIVE_KEYS",
        "REVIEW_FALSE_POSITIVE_SCOPES", "REVIEW_APPROVED_KEYS",
    },
    "title_resolution": {
        "EXPECTED_NORMALIZED", "EXPECTED_BASELINE", "EXPECTED_INPUT_COUNTS",
        "ROGUE_AUTHORIZED_RECOVERIES", "ROGUE_REVIEW_RESOLUTIONS",
    },
    "glossary_upgrade": set(),
}

SET_FIELDS = {
    "normalization": {"EXPLICIT_REVIEW_VARIANTS", "REVIEW_FALSE_POSITIVE_KEYS", "REVIEW_APPROVED_KEYS",
                       "SPECIAL_IDENTITY_CORES", "SPECIAL_HANDLED_KEYS"},
    "title_resolution": {"HUMAN_DOWNGRADED_IDS"},
    "glossary_upgrade": set(),
}

RECORD_FIELDS = {
    "normalization": {"REVIEW_FALSE_POSITIVE_SCOPES", "FALSE_REASONS"},
    "title_resolution": {"ROGUE_AUTHORIZED_RECOVERIES"},
    "glossary_upgrade": set(),
}

OPTIONAL_DEFAULTS = {
    "normalization": {
        "SPECIAL_IDENTITY_CORES": (), "SPECIAL_NAME_CASES": (), "SPECIAL_HANDLED_KEYS": (),
        "FALSE_REASONS": {}, "PRONOUN_CASES": (), "EXACT_SOURCE_DISPROOFS": (),
    },
    "title_resolution": {"TITLE_REGION_MAX_PHYSICAL_LINES": 12, "UNIT_99_GUARD": None,
                          "REQUIRED_DISCONTINUITY_GUARDS": ()},
    "glossary_upgrade": {},
}


class PrivateInputError(ValueError):
    """Raised for unavailable, malformed or incomplete private inputs."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(name, value, positive=False):
    if not _is_int(value) or (positive and value <= 0):
        raise PrivateInputError("setting must be a non-boolean integer: " + name)


def _require_sequence(name, value, arity=None):
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise PrivateInputError("setting must be a sequence: " + name)
    for item in value:
        if not isinstance(item, (tuple, list)):
            raise PrivateInputError("setting entries must be sequences: " + name)
        if arity is not None and len(item) != arity:
            raise PrivateInputError("setting entry has the wrong arity: " + name)


def _require_mapping(name, value, key_kind="str"):
    if not isinstance(value, dict):
        raise PrivateInputError("setting must be an object: " + name)
    for key in value:
        if key_kind == "str" and not isinstance(key, str):
            raise PrivateInputError("setting requires string keys: " + name)
        if key_kind == "int" and not _is_int(key):
            raise PrivateInputError("setting requires integer keys: " + name)
        if key_kind == "tuple" and not isinstance(key, tuple):
            raise PrivateInputError("setting requires tuple keys: " + name)


def _no_duplicate_json_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise PrivateInputError("duplicate JSON key in private input")
        out[key] = value
    return out


def _read_document(path):
    path = Path(path)
    if not path.is_file():
        raise PrivateInputError("private input file is unavailable")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PrivateInputError("private input file cannot be read as UTF-8") from exc
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_json_keys)
    except PrivateInputError:
        raise
    except Exception as exc:
        raise PrivateInputError("private input file is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise PrivateInputError("private input root must be an object")
    if set(doc) - _TOP_LEVEL:
        raise PrivateInputError("private input contains unknown top-level fields")
    return doc


def _decode_key(value):
    if not isinstance(value, dict) or "type" not in value:
        raise PrivateInputError("typed record key must be an object with a type")
    kind = value["type"]
    if kind == "str":
        if set(value) != {"type", "value"} or not isinstance(value["value"], str):
            raise PrivateInputError("malformed string record key")
        return value["value"]
    if kind == "int":
        if set(value) != {"type", "value"} or not _is_int(value["value"]):
            raise PrivateInputError("malformed integer record key")
        return value["value"]
    if kind == "tuple":
        if set(value) != {"type", "items"} or not isinstance(value["items"], list) or not value["items"]:
            raise PrivateInputError("malformed tuple record key")
        return tuple(_decode_key(item) for item in value["items"])
    raise PrivateInputError("unsupported typed record key")


def _freeze(value):
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise PrivateInputError("only string keys are allowed inside plain values")
        return {key: _freeze(item) for key, item in value.items()}
    return value


def _decode_record_list(name, items):
    if not isinstance(items, list):
        raise PrivateInputError("record field must be a list: " + name)
    decoded = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            raise PrivateInputError("malformed record entry in " + name)
        key = _decode_key(item["key"])
        if key in decoded:
            raise PrivateInputError("duplicate record key in " + name)
        decoded[key] = _freeze(item["value"])
    return decoded


def _validate_editorial_specs(specs):
    if not isinstance(specs, (tuple, list)):
        raise PrivateInputError("editorial family specs must be a sequence")
    for spec in specs:
        if not isinstance(spec, dict):
            raise PrivateInputError("editorial family spec must be an object")
        for key in ("family_id", "source_identity", "source_forms", "source_form_roles", "target_variants"):
            if key not in spec:
                raise PrivateInputError("editorial family spec is missing " + key)
        if not isinstance(spec["family_id"], str) or not isinstance(spec["source_identity"], str):
            raise PrivateInputError("editorial family identifiers must be strings")
        if not isinstance(spec["source_forms"], (tuple, list)) or not spec["source_forms"]:
            raise PrivateInputError("editorial family source_forms must be a non-empty sequence")
        if not isinstance(spec["source_form_roles"], dict):
            raise PrivateInputError("editorial family source_form_roles must be an object")
        if not isinstance(spec["target_variants"], (tuple, list)):
            raise PrivateInputError("editorial family target_variants must be a sequence")
        if "legacy_claims" in spec:
            _require_sequence("legacy_claims", spec["legacy_claims"], arity=2)


def _validate_special_cases(cases, name, required):
    if not isinstance(cases, (tuple, list)):
        raise PrivateInputError(name + " must be a sequence")
    for case in cases:
        if not isinstance(case, dict):
            raise PrivateInputError(name + " entries must be objects")
        for key in required:
            if key not in case:
                raise PrivateInputError(name + " entry is missing " + key)


def _validate_normalization(result):
    _require_int("EXPECTED_FINAL_COUNT", result["EXPECTED_FINAL_COUNT"], positive=True)
    if not isinstance(result["EXPECTED_FINAL_SHA256"], str) or not result["EXPECTED_FINAL_SHA256"]:
        raise PrivateInputError("EXPECTED_FINAL_SHA256 must be a non-empty string")
    _require_sequence("APPROVED_RULES", result["APPROVED_RULES"], arity=4)
    _require_sequence("FALSE_POSITIVE_RULES", result["FALSE_POSITIVE_RULES"], arity=3)
    _require_sequence("EXPLICIT_REVIEW_VARIANTS", result["EXPLICIT_REVIEW_VARIANTS"], arity=2)
    _require_sequence("SCOPED_REVIEW_CORRECTIONS", result["SCOPED_REVIEW_CORRECTIONS"], arity=5)
    _require_sequence("REVIEW_FALSE_POSITIVE_KEYS", result["REVIEW_FALSE_POSITIVE_KEYS"], arity=2)
    _require_sequence("REVIEW_APPROVED_KEYS", result["REVIEW_APPROVED_KEYS"], arity=2)
    _require_mapping("SEMANTICALLY_APPROVED_TITLES", result["SEMANTICALLY_APPROVED_TITLES"])
    _require_mapping("TITLE_REVIEW_OVERRIDES", result["TITLE_REVIEW_OVERRIDES"])
    _validate_editorial_specs(result["ROGUE_EDITORIAL_FAMILY_SPECS"])
    _require_mapping("REVIEW_FALSE_POSITIVE_SCOPES", result["REVIEW_FALSE_POSITIVE_SCOPES"], key_kind="tuple")
    _require_mapping("FALSE_REASONS", result["FALSE_REASONS"], key_kind="tuple")
    uncovered = set(result["REVIEW_FALSE_POSITIVE_KEYS"]) - set(result["FALSE_REASONS"])
    if uncovered:
        raise PrivateInputError("false-positive review reasons are incomplete")
    if not all(isinstance(item, str) for item in result["SPECIAL_IDENTITY_CORES"]):
        raise PrivateInputError("special identity cores must be strings")
    for item in result["SPECIAL_HANDLED_KEYS"]:
        if not isinstance(item, tuple) or len(item) != 2:
            raise PrivateInputError("special handled keys must be pairs")
    _validate_special_cases(
        result["SPECIAL_NAME_CASES"], "SPECIAL_NAME_CASES",
        ("identity", "source_forms", "current_rendering", "proposed_rendering", "reason"),
    )
    _validate_special_cases(
        result["EXACT_SOURCE_DISPROOFS"], "EXACT_SOURCE_DISPROOFS",
        ("identity", "source_form", "target_rendering", "reason"),
    )
    _validate_special_cases(
        result["PRONOUN_CASES"], "PRONOUN_CASES",
        ("character", "source_identity", "current_first_sentence", "current_second_sentence",
         "source_first_tokens", "source_continuation_token", "expected_gender", "pronoun_id",
         "current_rendering", "proposed_rendering", "exact_occurrence_count",
         "pronoun_token_change_count", "proposed_first_sentence", "proposed_second_sentence",
         "per_sentence_change_count", "reason"),
    )
    for case in result["PRONOUN_CASES"]:
        _require_int("PRONOUN_CASES.exact_occurrence_count", case["exact_occurrence_count"], positive=True)
        _require_int("PRONOUN_CASES.pronoun_token_change_count", case["pronoun_token_change_count"], positive=True)
        _require_int("PRONOUN_CASES.per_sentence_change_count", case["per_sentence_change_count"], positive=True)
        if case["expected_gender"] not in {"masculine", "feminine", "neutral"}:
            raise PrivateInputError("pronoun case has an unsupported expected_gender")


def _validate_title(result):
    for name in ("EXPECTED_NORMALIZED", "EXPECTED_BASELINE"):
        value = result[name]
        if not isinstance(value, dict):
            raise PrivateInputError(name + " must be an object")
        _require_int(name + ".file_count", value.get("file_count"), positive=True)
    counts = result["EXPECTED_INPUT_COUNTS"]
    if not isinstance(counts, dict) or not all(isinstance(k, str) and _is_int(v) for k, v in counts.items()):
        raise PrivateInputError("EXPECTED_INPUT_COUNTS must map strings to integers")
    _require_mapping("ROGUE_AUTHORIZED_RECOVERIES", result["ROGUE_AUTHORIZED_RECOVERIES"], key_kind="int")
    _require_mapping("ROGUE_REVIEW_RESOLUTIONS", result["ROGUE_REVIEW_RESOLUTIONS"])
    _require_mapping("ROGUE_UNRESOLVED_NOTES", result["ROGUE_UNRESOLVED_NOTES"])
    if not all(isinstance(item, str) for item in result["HUMAN_DOWNGRADED_IDS"]):
        raise PrivateInputError("HUMAN_DOWNGRADED_IDS must contain strings")
    _require_int("TITLE_REGION_MAX_PHYSICAL_LINES", result["TITLE_REGION_MAX_PHYSICAL_LINES"], positive=True)
    if result["UNIT_99_GUARD"] is not None:
        guard = result["UNIT_99_GUARD"]
        if not isinstance(guard, dict):
            raise PrivateInputError("UNIT_99_GUARD must be an object")
        if "unit" in guard and not _is_int(guard["unit"]):
            raise PrivateInputError("UNIT_99_GUARD.unit must be an integer")
    for required in result["REQUIRED_DISCONTINUITY_GUARDS"]:
        if not isinstance(required, dict):
            raise PrivateInputError("REQUIRED_DISCONTINUITY_GUARDS entries must be objects")
        _require_int("REQUIRED_DISCONTINUITY_GUARDS.source_title_number",
                     required.get("source_title_number"), positive=True)
        _require_int("REQUIRED_DISCONTINUITY_GUARDS.min_distinct_korean_title_count",
                     required.get("min_distinct_korean_title_count"), positive=True)


def load_document(path, kind):
    if kind not in REQUIRED:
        raise PrivateInputError("unsupported private input kind")
    doc = _read_document(path)
    version = doc.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise PrivateInputError("unsupported private input schema version")
    if doc.get("kind") != kind:
        raise PrivateInputError("private input kind does not match the requested operation")
    values = doc.get("values", {})
    records = doc.get("records", {})
    if not isinstance(values, dict) or not isinstance(records, dict):
        raise PrivateInputError("private input values/records must be objects")

    known = REQUIRED[kind] | set(OPTIONAL_DEFAULTS[kind]) | RECORD_FIELDS[kind]
    unknown = (set(values) | set(records)) - known
    if unknown:
        raise PrivateInputError("private input contains unknown settings")

    result = {}
    for name, value in values.items():
        if name in RECORD_FIELDS[kind]:
            raise PrivateInputError("typed record field must appear under records: " + name)
        result[name] = value if kind == "glossary_upgrade" else _freeze(value)
    for name, items in records.items():
        if name not in RECORD_FIELDS[kind]:
            raise PrivateInputError("plain field cannot appear under records: " + name)
        result[name] = _decode_record_list(name, items)

    for name, default in OPTIONAL_DEFAULTS[kind].items():
        result.setdefault(name, _freeze(default))

    missing = REQUIRED[kind] - set(result)
    if missing:
        raise PrivateInputError("private input is missing required settings")
    for name in NONEMPTY[kind]:
        if not result.get(name):
            raise PrivateInputError("private input setting must not be empty: " + name)
    for name in SET_FIELDS[kind]:
        value = result.get(name, ())
        if not isinstance(value, (tuple, list, set)):
            raise PrivateInputError("set setting must be a list: " + name)
        result[name] = set(value)

    if kind == "normalization":
        _validate_normalization(result)
    elif kind == "title_resolution":
        _validate_title(result)
    return result


def load_normalization_inputs(path):
    return load_document(path, "normalization")


def load_title_inputs(path):
    return load_document(path, "title_resolution")


def load_glossary_upgrade_config(path):
    return load_document(path, "glossary_upgrade")
