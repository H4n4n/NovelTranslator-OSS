#!/usr/bin/env python3
"""Config-driven glossary upgrade utility.

Novel-specific mappings are intentionally NOT embedded in this public source.
Provide them in an operator-local, versioned JSON file that is ignored by Git.
The entire configuration and every proposed edit are validated before the
target file is touched; a failed validation leaves the original bytes unchanged.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from private_inputs import PrivateInputError, load_glossary_upgrade_config

DEFAULT_GLOSSARY = Path(os.environ.get("NOVELTRANSLATOR_GLOSSARY_FILE", "data/glossary.json"))
DEFAULT_CONFIG = Path(os.environ.get(
    "NOVELTRANSLATOR_GLOSSARY_UPGRADE_CONFIG",
    Path(__file__).resolve().parent / "local_data" / "glossary_upgrade.json",
))

KNOWN_CONFIG_KEYS = {
    "remove_terms", "locked_names", "locked_terms", "locked_entries",
    "provisional_entries", "normalize_capitalization", "overrides", "replace_entries",
}


class GlossaryValidationError(ValueError):
    pass


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _require_str(value, label):
    if not isinstance(value, str):
        raise GlossaryValidationError(f"{label} must be a string")
    return value


def _validate_entry(term: str, entry) -> None:
    if not isinstance(entry, dict) or not entry:
        raise GlossaryValidationError(f"entry for {term!r} must be a non-empty object")
    for field in ("english", "notes", "status", "category"):
        if field in entry and not isinstance(entry[field], str):
            raise GlossaryValidationError(f"{field} for {term!r} must be a string")
    if "approved_renderings" in entry:
        renderings = entry["approved_renderings"]
        if not isinstance(renderings, list) or not all(isinstance(item, str) for item in renderings):
            raise GlossaryValidationError(f"approved_renderings for {term!r} must be a list of strings")
    if "first_chapter" in entry:
        value = entry["first_chapter"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise GlossaryValidationError(f"first_chapter for {term!r} must be an integer")


def validate_entries(entries) -> None:
    if not isinstance(entries, dict):
        raise GlossaryValidationError("glossary entries must be an object")
    for term, entry in entries.items():
        if not isinstance(term, str) or not term:
            raise GlossaryValidationError("glossary term keys must be non-empty strings")
        _validate_entry(term, entry)


def _validate_entry_list(items, label, require_entry=False):
    if not isinstance(items, (list, tuple)):
        raise GlossaryValidationError(f"{label} must be a list")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise GlossaryValidationError(f"{label} entries must be objects")
        term = _require_str(item.get("term"), f"{label}.term")
        if term in seen:
            raise GlossaryValidationError(f"duplicate term in {label}")
        seen.add(term)
        if "english" in item and item["english"] is not None and not isinstance(item["english"], str):
            raise GlossaryValidationError(f"{label}.english must be a string or null")
        if "notes" in item and item["notes"] is not None and not isinstance(item["notes"], str):
            raise GlossaryValidationError(f"{label}.notes must be a string or null")
        if require_entry:
            if "entry" not in item:
                raise GlossaryValidationError(f"{label} entries require an entry object")
            _validate_entry(term, item["entry"])


def validate_config(config) -> None:
    if not isinstance(config, dict):
        raise GlossaryValidationError("glossary configuration must be an object")
    unknown = set(config) - KNOWN_CONFIG_KEYS
    if unknown:
        raise GlossaryValidationError("glossary configuration contains unknown keys")
    missing = KNOWN_CONFIG_KEYS - set(config)
    if missing:
        raise GlossaryValidationError("glossary configuration is incomplete")
    if not isinstance(config["remove_terms"], (list, tuple)) or not all(
        isinstance(item, str) for item in config["remove_terms"]
    ):
        raise GlossaryValidationError("remove_terms must be a list of strings")
    for field in ("locked_names", "locked_terms", "normalize_capitalization"):
        value = config[field]
        if not isinstance(value, dict) or not all(
            isinstance(term, str) and isinstance(english, str) for term, english in value.items()
        ):
            raise GlossaryValidationError(f"{field} must map strings to strings")
    _validate_entry_list(config["locked_entries"], "locked_entries")
    _validate_entry_list(config["provisional_entries"], "provisional_entries")
    _validate_entry_list(config["overrides"], "overrides", require_entry=True)
    _validate_entry_list(config["replace_entries"], "replace_entries", require_entry=True)


def _lock(entries, term, english=None, notes=None) -> None:
    if term not in entries:
        return
    if english is not None:
        entries[term]["english"] = english
    entries[term]["status"] = "locked"
    if notes is not None:
        entries[term]["notes"] = notes


def _provisional(entries, term, english=None, notes=None) -> None:
    if term not in entries:
        return
    if english is not None:
        entries[term]["english"] = english
    entries[term]["status"] = "provisional"
    if notes is not None:
        entries[term]["notes"] = notes


def apply_config(entries, config):
    """Validate and apply a configuration to a copy; never mutate the input."""
    validate_config(config)
    validate_entries(entries)
    working = copy.deepcopy(entries)
    removed = []
    for term in config["remove_terms"]:
        if term in working:
            del working[term]
            removed.append(term)
    for term, english in config["locked_names"].items():
        _lock(working, term, english=english)
    for term, english in config["locked_terms"].items():
        _lock(working, term, english=english)
    for item in config["locked_entries"]:
        _lock(working, item["term"], item.get("english"), item.get("notes"))
    for item in config["provisional_entries"]:
        _provisional(working, item["term"], item.get("english"), item.get("notes"))
    for term, english in config["normalize_capitalization"].items():
        if term in working:
            working[term]["english"] = english
    for item in config["overrides"]:
        term = item["term"]
        if term in working:
            working[term] = {**working[term], **item["entry"]}
    for item in config["replace_entries"]:
        term = item["term"]
        if term in working:
            working[term] = copy.deepcopy(item["entry"])
    validate_entries(working)
    return working, removed


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    glossary_path = Path(argv[0]) if argv else DEFAULT_GLOSSARY
    config_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CONFIG

    if not glossary_path.exists():
        print("GLOSSARY_UNAVAILABLE: selected glossary file does not exist.", file=sys.stderr)
        return 2
    if not config_path.exists():
        print(
            "PRIVATE_INPUTS_UNAVAILABLE: provide the operator-local glossary upgrade JSON "
            "(no novel-specific mappings are embedded in this public source).",
            file=sys.stderr,
        )
        return 2

    try:
        glossary = load_json(glossary_path)
        if not isinstance(glossary, dict) or not isinstance(glossary.get("entries"), dict):
            raise GlossaryValidationError("glossary must contain an entries object")
        config = load_glossary_upgrade_config(config_path)
        working, removed = apply_config(glossary["entries"], config)
    except (PrivateInputError, GlossaryValidationError, OSError, ValueError) as exc:
        print(f"GLOSSARY_VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 2

    original_bytes = glossary_path.read_bytes()
    backup = glossary_path.with_name(glossary_path.stem + "_before_upgrade" + glossary_path.suffix)
    glossary["entries"] = working
    if backup != glossary_path:
        try:
            backup.write_bytes(original_bytes)
        except OSError:
            pass
    save_json(glossary_path, glossary)

    locked = sum(1 for entry in working.values() if entry.get("status") == "locked")
    provisional_count = sum(1 for entry in working.values() if entry.get("status") == "provisional")
    contextual = sum(1 for entry in working.values() if entry.get("status") == "contextual")

    print("GLOSSARY UPGRADE COMPLETE")
    print(f"Backup: {backup}")
    print(f"Entries removed: {len(removed)}")
    print(f"Current glossary entries: {len(working)}")
    print(f"Locked: {locked}")
    print(f"Provisional: {provisional_count}")
    print(f"Contextual: {contextual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
