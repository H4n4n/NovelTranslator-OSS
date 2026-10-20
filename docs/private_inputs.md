# Operator-local private inputs (not shipped)

Novel-specific editorial tables, title mappings, glossary upgrade records and
corpus identities are **not** embedded in the public source. The operator
supplies them through ignored, versioned JSON files:

| Feature | Environment variable | Default path |
|---|---|---|
| Normalization plan | `NOVELTRANSLATOR_NORMALIZATION_INPUTS` | `local_data/normalization_inputs.json` |
| Title-resolution plan | `NOVELTRANSLATOR_TITLE_INPUTS` | `local_data/title_resolution_inputs.json` |
| Glossary upgrade | `NOVELTRANSLATOR_GLOSSARY_UPGRADE_CONFIG` | `local_data/glossary_upgrade.json` |
| Glossary file | `NOVELTRANSLATOR_GLOSSARY_FILE` | `data/glossary.json` |

When a file is absent, the utilities fail closed with an explicit
`PRIVATE_INPUTS_UNAVAILABLE` / `GLOSSARY_UNAVAILABLE` message. No invented
mappings are applied. `local_data/` and `data/` are ignored by Git.

## Versioned schema

Every file is an object with `schema_version: 1`, a `kind`, plain `values`, and
typed `records` (needed because JSON object keys cannot be tuples or integers):

```json
{
  "schema_version": 1,
  "kind": "normalization",
  "values": {
    "APPROVED_RULES": [["synthetic-source", "Synthetic Target"]],
    "SEMANTICALLY_APPROVED_TITLES": {"unit-1": {"title": "Synthetic"}}
  },
  "records": {
    "REVIEW_FALSE_POSITIVE_SCOPES": [
      {"key": {"type": "tuple", "items": [
        {"type": "str", "value": "synthetic-source"},
        {"type": "str", "value": "Synthetic Target"}]},
       "value": {"units": [1]}}
    ]
  }
}
```

Tagged key types are `str`, `int` and `tuple` (items may nest `str`/`int`).
Lists inside plain values are restored to tuples; fields such as
`EXPLICIT_REVIEW_VARIANTS`, `REVIEW_APPROVED_KEYS`, `HUMAN_DOWNGRADED_IDS` and
`REVIEW_FALSE_POSITIVE_KEYS` become sets. No `eval`, `exec` or `pickle` is used.

The loaders reject duplicate JSON keys, duplicate decoded record keys, unknown
top-level fields, unknown settings, unsupported versions/kinds, missing required
settings, empty required tables and malformed records.

### Required settings

Normalization:

```
EXPECTED_FINAL_COUNT, EXPECTED_FINAL_SHA256, APPROVED_RULES,
FALSE_POSITIVE_RULES, EXPLICIT_REVIEW_VARIANTS, ROGUE_EDITORIAL_FAMILY_SPECS,
SEMANTICALLY_APPROVED_TITLES, TITLE_REVIEW_OVERRIDES, SCOPED_REVIEW_CORRECTIONS,
REVIEW_FALSE_POSITIVE_KEYS, REVIEW_FALSE_POSITIVE_SCOPES, REVIEW_APPROVED_KEYS
```

Optional normalization settings (only when the reviewed workflow uses them):
`SPECIAL_IDENTITY_CORES`, `SPECIAL_NAME_CASES`, `SPECIAL_HANDLED_KEYS`,
`FALSE_REASONS` (typed tuple-key records), `PRONOUN_CASES`,
`EXACT_SOURCE_DISPROOFS` (reviewed exact-source disproof records).

Title resolution:

```
EXPECTED_NORMALIZED, EXPECTED_BASELINE, EXPECTED_INPUT_COUNTS,
HUMAN_DOWNGRADED_IDS, ROGUE_AUTHORIZED_RECOVERIES (integer-key records),
ROGUE_REVIEW_RESOLUTIONS, ROGUE_UNRESOLVED_NOTES
```

Optional: `TITLE_REGION_MAX_PHYSICAL_LINES`, `UNIT_99_GUARD`,
`REQUIRED_DISCONTINUITY_GUARDS`.
No title unit number, title-location fact or source-number discontinuity number
is embedded by default; supply those records locally when the reviewed workflow
uses them.

### Validation contract

- `APPROVED_RULES` entries require 4 elements; `FALSE_POSITIVE_RULES` 3;
  `SCOPED_REVIEW_CORRECTIONS` 5; review-key tables 2.
- `EXPECTED_FINAL_COUNT`, `EXPECTED_*["file_count"]`, input counts and
  `TITLE_REGION_MAX_PHYSICAL_LINES` must be non-boolean integers.
- `FALSE_REASONS` must cover every `REVIEW_FALSE_POSITIVE_KEYS` entry.
- `ROGUE_AUTHORIZED_RECOVERIES` keys must be integers.
- `REQUIRED_DISCONTINUITY_GUARDS` entries require positive integer
  `source_title_number` and `min_distinct_korean_title_count` fields.
- `schema_version` must be the integer `1`; `true` is rejected.
- Glossary values keep native JSON lists (for example `approved_renderings`).
- Any missing, empty, malformed or mistyped setting fails explicitly before
  the planner or glossary utility mutates anything.

Glossary upgrade (`values` only):

```
remove_terms, locked_names, locked_terms, locked_entries,
provisional_entries, normalize_capitalization, overrides, replace_entries
```

`upgrade_glossary.py` validates the entire configuration and every proposed
edit before touching the target. Non-object replacements, malformed renderings,
unexpected types and duplicate terms are rejected. The target is replaced
atomically; a failed validation leaves the original bytes unchanged.

## Operator transfer (later, local only)

1. Keep the private production checkout unchanged.
2. Export the original in-memory tables into the versioned JSON shape above,
   including tuple and integer keys as typed records. Do not copy values into
   tracked files, reports or Git history.
3. Store the files under the ignored `local_data/` directory or point the
   environment variables at private locations.
4. Run the plan CLI or glossary utility locally; verify plans against the
   private checkout before any live use.
5. Never commit `local_data/`, `data/`, novels, translations or credentials.

This public repository intentionally contains no private values and cannot
recreate them from defaults.
