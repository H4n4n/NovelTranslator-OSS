"""Build a source-aware normalization decision plan for a final corpus.

This tool is deliberately read-only with respect to chapter prose and novel
state.  It turns the committed normalization inventory into explicit,
occurrence-scoped editorial decisions.  It never applies those decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from build_finalization_corpus import load_corrected_txt_spine
from build_normalization_inventory import exact_occurrences, short_context, split_title_part
from novel_project import select_project
from production_run_lock import ProductionRunLockedError, production_run_lock


import os as _os
import sys as _sys

from private_inputs import PrivateInputError, load_normalization_inputs

_PRIVATE_INPUTS_ENV = "NOVELTRANSLATOR_NORMALIZATION_INPUTS"
_PRIVATE_INPUTS_PATH = _os.environ.get(_PRIVATE_INPUTS_ENV) or _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "local_data", "normalization_inputs.json")

_PRIVATE = {}
_PRIVATE_INPUTS_ERROR = ""
if _os.path.isfile(_PRIVATE_INPUTS_PATH):
    try:
        _PRIVATE = load_normalization_inputs(_PRIVATE_INPUTS_PATH)
        PRIVATE_INPUTS_AVAILABLE = True
    except PrivateInputError as exc:
        PRIVATE_INPUTS_AVAILABLE = False
        _PRIVATE_INPUTS_ERROR = str(exc)
else:
    PRIVATE_INPUTS_AVAILABLE = False
    _PRIVATE_INPUTS_ERROR = "private input file is unavailable"


def _private_value(name, default, as_set=False):
    value = _PRIVATE.get(name, default)
    return set(value) if as_set else value


EXPECTED_FINAL_COUNT = _private_value("EXPECTED_FINAL_COUNT", None)
EXPECTED_FINAL_SHA256 = _private_value("EXPECTED_FINAL_SHA256", None)
APPROVED_RULES = _private_value("APPROVED_RULES", ())
FALSE_POSITIVE_RULES = _private_value("FALSE_POSITIVE_RULES", ())
EXPLICIT_REVIEW_VARIANTS = _private_value("EXPLICIT_REVIEW_VARIANTS", (), as_set=True)
ROGUE_EDITORIAL_FAMILY_SPECS = _private_value("ROGUE_EDITORIAL_FAMILY_SPECS", ())
SEMANTICALLY_APPROVED_TITLES = _private_value("SEMANTICALLY_APPROVED_TITLES", {})
TITLE_REVIEW_OVERRIDES = _private_value("TITLE_REVIEW_OVERRIDES", {})
SCOPED_REVIEW_CORRECTIONS = _private_value("SCOPED_REVIEW_CORRECTIONS", ())
REVIEW_FALSE_POSITIVE_KEYS = _private_value("REVIEW_FALSE_POSITIVE_KEYS", (), as_set=True)
REVIEW_FALSE_POSITIVE_SCOPES = _private_value("REVIEW_FALSE_POSITIVE_SCOPES", {})
REVIEW_APPROVED_KEYS = _private_value("REVIEW_APPROVED_KEYS", (), as_set=True)
SPECIAL_IDENTITY_CORES = _private_value("SPECIAL_IDENTITY_CORES", (), as_set=True)
SPECIAL_NAME_CASES = _private_value("SPECIAL_NAME_CASES", ())
SPECIAL_HANDLED_KEYS = _private_value("SPECIAL_HANDLED_KEYS", (), as_set=True)
FALSE_REASONS = _private_value("FALSE_REASONS", {})
PRONOUN_CASES = _private_value("PRONOUN_CASES", ())
EXACT_SOURCE_DISPROOFS = _private_value("EXACT_SOURCE_DISPROOFS", ())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_paragraphs(text: str) -> list[str]:
    return [part for part in re.split(r"\r?\n\s*\r?\n", text) if part.strip()]


def final_corpus_fingerprint(chapter_dir: Path) -> dict:
    paths = sorted(chapter_dir.glob("[0-9][0-9][0-9][0-9].md"))
    expected = [f"{index:04d}.md" for index in range(1, EXPECTED_FINAL_COUNT + 1)]
    if [path.name for path in paths] != expected:
        raise ValueError("final corpus must contain exactly sequential units 0001-0351")
    digest = hashlib.sha256()
    byte_count = 0
    file_hashes = []
    for path in paths:
        data = path.read_bytes()
        digest.update(data)
        byte_count += len(data)
        file_hashes.append({"unit": int(path.stem), "sha256": sha256_bytes(data), "byte_count": len(data)})
    return {
        "file_count": len(paths),
        "aggregate_byte_count": byte_count,
        "aggregate_sha256": digest.hexdigest(),
        "files": file_hashes,
    }


def _fold_title(value: str | None) -> str:
    value = re.sub(r"^\s*\d+\.\s*", "", value or "")
    value = unicodedata.normalize("NFKC", value).replace("’", "'")
    return re.sub(r"\s+", " ", value).strip().casefold()


def _source_forms_in(text: str, source_forms: tuple[str, ...]) -> list[str]:
    return sorted((form for form in source_forms if form and form in text), key=lambda value: (-len(value), value))


def physical_occurrence_id(corpus_id: str, anchor: dict) -> str:
    """Return an immutable identity for one physical target occurrence."""
    payload = {
        "corpus_id": corpus_id,
        "unit": anchor["expected_final_unit"],
        "baseline_file_sha256": anchor["expected_baseline_file_sha256"],
        "target_paragraph": anchor["target_paragraph"],
        "target_paragraph_sha256": anchor["expected_target_paragraph_sha256"],
        "exact_old_text": anchor["exact_old_text"],
        "target_occurrence": anchor["target_occurrence"],
        "target_start": anchor.get("target_start"),
        "target_end": anchor.get("target_end"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"occurrence:{sha256_bytes(encoded)}"


def derive_source_form_role(
    confirmed_source_forms: list[str],
    source_form_roles: dict[str, str],
    paragraph_scoped: bool = True,
) -> tuple[str | None, str]:
    """Derive a role from the longest exact source form, never its parent identity."""
    candidates = [(form, source_form_roles[form]) for form in confirmed_source_forms if form in source_form_roles]
    if not paragraph_scoped and len({role for _, role in candidates}) > 1:
        return None, "ambiguous_source_form_role"
    exact_form = candidates[0][0] if candidates else None
    if exact_form is None:
        return None, "literal_source_variant"
    return exact_form, source_form_roles[exact_form]


def review_decision_is_non_executable(decision: dict) -> bool:
    """Enforce the cross-project safety contract for unresolved prose decisions."""
    if decision.get("status") != "REVIEW_REQUIRED":
        return False
    forbidden = {"exact_proposed_replacement", "replacement_operation", "executable_edit"}
    return (
        decision.get("proposed_rendering") is None
        and decision.get("replacement_safe_automatically") is False
        and decision.get("automatic_application_forbidden") is True
        and not (forbidden & set(decision))
        and all(not (forbidden & set(anchor)) for anchor in decision.get("occurrence_anchors", []))
    )


def validate_unique_occurrence_ownership(decisions: list[dict], corpus_id: str) -> None:
    """Reject duplicate ownership across executable and unresolved decisions."""
    owners = {}
    owned_spans = {}
    for decision in decisions:
        if decision.get("status") not in {"APPROVED_CORRECTION", "REVIEW_REQUIRED"}:
            continue
        owner = decision.get("decision_id", "unassigned")
        for anchor in decision.get("occurrence_anchors", []):
            occurrence_id = anchor.get("occurrence_id") or physical_occurrence_id(corpus_id, anchor)
            previous = owners.get(occurrence_id)
            if previous is not None and previous != owner:
                raise ValueError(
                    f"physical target occurrence {occurrence_id} is owned by both {previous} and {owner}"
                )
            owners[occurrence_id] = owner
            if anchor.get("target_start") is not None and anchor.get("target_end") is not None:
                scope = (
                    anchor["expected_final_unit"],
                    anchor["expected_baseline_file_sha256"],
                    anchor["target_paragraph"],
                    anchor["expected_target_paragraph_sha256"],
                )
                for old_start, old_end, old_owner in owned_spans.get(scope, []):
                    if old_owner != owner and anchor["target_start"] < old_end and old_start < anchor["target_end"]:
                        raise ValueError(
                            f"overlapping physical target span is owned by both {old_owner} and {owner}"
                        )
                owned_spans.setdefault(scope, []).append(
                    (anchor["target_start"], anchor["target_end"], owner)
                )


def _target_occurrence_anchors(
    current: str,
    source_forms: tuple[str, ...],
    source_units: list[dict],
    texts: dict[int, str],
    proposed: str | None = None,
    file_hashes: dict[int, str] | None = None,
    corpus_id: str = "unspecified-corpus",
) -> list[dict]:
    """Classify each exact target occurrence against compact local source evidence."""
    anchors = []
    for unit, target_text in texts.items():
        target_parts = split_paragraphs(target_text)
        source_parts = split_paragraphs(source_units[unit - 1]["text"])
        unit_matches = []
        for paragraph_index, target_part in enumerate(target_parts):
            for occurrence_index, (start, end) in enumerate(exact_occurrences(target_part, current), 1):
                unit_matches.append((paragraph_index, occurrence_index, start, end, target_part))
        if not unit_matches:
            continue

        source_count = 0
        for source_part in source_parts:
            # Longest source form owns an overlapping source span.
            spans = []
            for form in sorted(source_forms, key=lambda value: (-len(value), value)):
                for start, end in exact_occurrences(source_part, form):
                    if not any(start < old_end and old_start < end for old_start, old_end in spans):
                        spans.append((start, end))
            source_count += len(spans)

        file_occurrence_count = len(unit_matches)
        for file_occurrence_number, (paragraph_index, occurrence_index, start, end, target_part) in enumerate(unit_matches, 1):
            delta = len(source_parts) - len(target_parts)
            radius = min(5, abs(delta) + 2)
            expected = paragraph_index + delta
            expected_forms = (
                _source_forms_in(source_parts[expected], source_forms)
                if 0 <= expected < len(source_parts) else []
            )
            if expected_forms:
                source_index, confirmed = expected, expected_forms
                evidence = (
                    "exact_aligned_paragraph"
                    if source_index == paragraph_index and len(source_parts) == len(target_parts)
                    else "offset_aligned_paragraph"
                )
            else:
                nearby = []
                for source_index in range(max(0, expected - radius), min(len(source_parts), expected + radius + 1)):
                    forms = _source_forms_in(source_parts[source_index], source_forms)
                    if forms:
                        nearby.append((source_index, forms))
                if len(nearby) == 1:
                    source_index, confirmed = nearby[0]
                    evidence = "nearby_unique_source_paragraph"
                elif source_count == len(unit_matches):
                    source_index, confirmed = None, _source_forms_in(source_units[unit - 1]["text"], source_forms)
                    evidence = "source_unit_exact_count"
                else:
                    source_index, confirmed, evidence = None, [], "unconfirmed_source_attribution"
            anchor = {
                "unit": unit,
                "expected_final_unit": unit,
                "expected_baseline_file_sha256": (file_hashes or {}).get(
                    unit, sha256_bytes(target_text.encode("utf-8"))
                ),
                "target_paragraph": paragraph_index + 1,
                "target_occurrence": occurrence_index,
                "target_start": start,
                "target_end": end,
                "target_occurrence_number_in_file": file_occurrence_number,
                "expected_file_occurrence_count": file_occurrence_count,
                "expected_paragraph_occurrence_count": len(exact_occurrences(target_part, current)),
                "expected_target_paragraph": target_part,
                "expected_target_paragraph_sha256": sha256_bytes(target_part.encode("utf-8")),
                "exact_old_text": current,
                "exact_proposed_replacement": proposed,
                "target_context": short_context(target_part, start, end, radius=55),
                "source_paragraph": source_index + 1 if source_index is not None else None,
                "confirmed_source_forms": confirmed,
                "source_evidence": evidence,
            }
            anchor["occurrence_id"] = physical_occurrence_id(corpus_id, anchor)
            anchors.append(anchor)
    return anchors


def _scoped_occurrence_anchors(
    current: str,
    proposed: str | None,
    source_units: list[dict],
    texts: dict[int, str],
    scopes: tuple[tuple[int, int, int, str], ...],
    file_hashes: dict[int, str] | None = None,
    corpus_id: str = "unspecified-corpus",
) -> list[dict]:
    """Build fail-closed anchors for manually reviewed paragraph pairs."""
    anchors = []
    by_unit = Counter(unit for unit, _, _, _ in scopes)
    seen_by_unit = Counter()
    for unit, target_paragraph, source_paragraph, source_fragment in scopes:
        target_text = texts[unit]
        target_parts = split_paragraphs(target_text)
        source_parts = split_paragraphs(source_units[unit - 1]["text"])
        if not (1 <= target_paragraph <= len(target_parts)) or not (1 <= source_paragraph <= len(source_parts)):
            raise ValueError(f"reviewed paragraph anchor out of range for unit {unit}")
        target_part = target_parts[target_paragraph - 1]
        source_part = source_parts[source_paragraph - 1]
        occurrences = exact_occurrences(target_part, current)
        if len(occurrences) != 1:
            raise ValueError(
                f"reviewed target anchor must contain {current!r} exactly once: unit {unit}, paragraph {target_paragraph}"
            )
        if source_fragment not in source_part:
            raise ValueError(
                f"reviewed source evidence missing {source_fragment!r}: unit {unit}, paragraph {source_paragraph}"
            )
        seen_by_unit[unit] += 1
        start, end = occurrences[0]
        anchor = {
            "unit": unit,
            "expected_final_unit": unit,
            "expected_baseline_file_sha256": (file_hashes or {}).get(
                unit, sha256_bytes(target_text.encode("utf-8"))
            ),
            "target_paragraph": target_paragraph,
            "target_occurrence": 1,
            "target_start": start,
            "target_end": end,
            "target_occurrence_number_in_review_scope": seen_by_unit[unit],
            "expected_review_scope_occurrence_count": by_unit[unit],
            "expected_paragraph_occurrence_count": 1,
            "expected_target_paragraph": target_part,
            "expected_target_paragraph_sha256": sha256_bytes(target_part.encode("utf-8")),
            "exact_old_text": current,
            "exact_proposed_replacement": proposed,
            "target_context": short_context(target_part, start, end, radius=55),
            "source_paragraph": source_paragraph,
            "expected_source_paragraph": source_part,
            "confirmed_source_forms": [source_fragment],
            "source_evidence": "human_reviewed_exact_paragraph_pair",
        }
        anchor["occurrence_id"] = physical_occurrence_id(corpus_id, anchor)
        anchors.append(anchor)
    return anchors


def approved_decision_is_fail_closed(decision: dict) -> bool:
    if decision.get("status") != "APPROVED_CORRECTION":
        return False
    required = {
        "expected_final_unit", "expected_baseline_file_sha256", "target_paragraph",
        "expected_target_paragraph", "expected_target_paragraph_sha256",
        "exact_old_text", "exact_proposed_replacement", "source_evidence",
    }
    return bool(decision.get("occurrence_anchors")) and all(
        required <= set(anchor) and anchor["exact_proposed_replacement"] == decision["proposed_rendering"]
        for anchor in decision["occurrence_anchors"]
    )


def _decision_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:04d}"


def _base_decision(source_identity: str, source_form: str, current: str, proposed, status: str, reason: str) -> dict:
    return {
        "source_identity": source_identity,
        "source_form": source_form,
        "current_rendering": current,
        "proposed_rendering": proposed,
        "action": status,
        "status": status,
        "final_units": [],
        "exact_occurrence_count": 0,
        "occurrence_anchors": [],
        "source_evidence_classification": "none",
        "reason": reason,
        "preserve_short_full_forms": True,
        "replacement_safe_automatically": False,
    }


def _inventory_variant(inventory: dict, identity: str, rendering: str):
    for entity in inventory["entities"]["identities"]:
        if entity["source_identity"] != identity:
            continue
        for variant in entity["english_variants"]:
            if variant["rendering"] == rendering:
                return entity, variant
    return None, None


def _promote_possessive_target_token(anchor: dict, base: str, suffixes: tuple[str, ...], corpus_id: str) -> dict:
    """Make a possessive token the owned span instead of its embedded base name."""
    paragraph = anchor["expected_target_paragraph"]
    base_matches = exact_occurrences(paragraph, base)
    base_index = anchor["target_occurrence"] - 1
    if not (0 <= base_index < len(base_matches)):
        raise ValueError("target occurrence index no longer resolves inside its paragraph")
    start, end = base_matches[base_index]
    suffix = next((value for value in sorted(suffixes, key=len, reverse=True) if paragraph.startswith(value, end)), None)
    if suffix is None:
        return anchor
    owned_text = base + suffix
    owned_matches = exact_occurrences(paragraph, owned_text)
    owned_index = next((index for index, span in enumerate(owned_matches, 1) if span[0] == start), None)
    if owned_index is None:
        raise ValueError("possessive target span could not be reconstructed")
    anchor = dict(anchor)
    anchor["base_target_rendering"] = base
    anchor["exact_old_text"] = owned_text
    anchor["target_start"] = start
    anchor["target_end"] = end + len(suffix)
    anchor["target_occurrence"] = owned_index
    anchor["expected_paragraph_occurrence_count"] = len(owned_matches)
    anchor["target_context"] = short_context(paragraph, start, end + len(suffix), radius=55)
    anchor["occurrence_id"] = physical_occurrence_id(corpus_id, anchor)
    return anchor


def build_editorial_family_decisions(
    spec: dict,
    source_units: list[dict],
    texts: dict[int, str],
    file_hashes: dict[int, str] | None = None,
    corpus_id: str = "unspecified-corpus",
    inventory: dict | None = None,
) -> tuple[list[dict], dict]:
    """Build non-executable decisions from unique physical target occurrences.

    The algorithm is language-agnostic.  Project evidence supplies exact
    source forms, their roles, target spellings, and editorial candidates.
    """
    source_forms = tuple(spec["source_forms"])
    role_map = dict(spec["source_form_roles"])
    suffixes = tuple(spec.get("possessive_suffixes", ("'s", "’s")))
    claims = []
    for current in spec["target_variants"]:
        anchors = _target_occurrence_anchors(
            current, source_forms, source_units, texts, None, file_hashes, corpus_id
        )
        for anchor in anchors:
            if anchor["source_evidence"] == "unconfirmed_source_attribution":
                raise ValueError(
                    f"editorial-family occurrence lacks exact source ownership: {spec['family_id']} / "
                    f"unit {anchor['unit']} paragraph {anchor['target_paragraph']}"
                )
            exact_source_form, source_role = derive_source_form_role(
                anchor["confirmed_source_forms"], role_map, anchor["source_paragraph"] is not None
            )
            if exact_source_form is None:
                raise ValueError(
                    f"editorial-family source form is not represented in its role map: {spec['family_id']}"
                )
            anchor = _promote_possessive_target_token(anchor, current, suffixes, corpus_id)
            if anchor["exact_old_text"] != current:
                source_role = "possessive_inflection"
                anchor["base_source_form_role"] = role_map[exact_source_form]
            anchor["exact_confirmed_source_form"] = exact_source_form
            anchor["source_form_role"] = source_role
            anchor.pop("exact_proposed_replacement", None)
            claims.append(anchor)

    unique = {}
    for anchor in claims:
        occurrence_id = anchor["occurrence_id"]
        if occurrence_id in unique:
            raise ValueError(f"duplicate physical occurrence generated inside editorial family: {occurrence_id}")
        unique[occurrence_id] = anchor

    groups = {}
    for anchor in unique.values():
        group_key = (
            "possessive_inflection"
            if anchor["source_form_role"] == "possessive_inflection"
            else anchor["exact_old_text"]
        )
        groups.setdefault(group_key, []).append(anchor)

    decisions = []
    candidates_by_role = {
        role: list(values)
        for role, values in spec.get("editorial_candidates_by_source_form_role", {}).items()
    }
    for group_key, anchors in sorted(groups.items(), key=lambda item: str(item[0]).casefold()):
        anchors.sort(key=lambda item: (item["unit"], item["target_paragraph"], item["target_occurrence"]))
        roles = sorted({item["source_form_role"] for item in anchors})
        exact_forms = sorted({item["exact_confirmed_source_form"] for item in anchors}, key=lambda value: (-len(value), value))
        current_renderings = sorted({item["exact_old_text"] for item in anchors})
        editorial_candidates = sorted({
            candidate
            for role in roles
            for candidate in candidates_by_role.get(role, [])
        })
        for anchor in anchors:
            anchor["editorial_candidates"] = candidates_by_role.get(anchor["source_form_role"], [])
        decision = _base_decision(
            spec["source_identity"],
            " / ".join(exact_forms),
            " / ".join(current_renderings),
            None,
            "REVIEW_REQUIRED",
            "The physical target occurrences are source-owned, but their orthographic canonical remains a human editorial choice; this record carries no edit authority.",
        )
        decision.update({
            "editorial_family_id": spec["family_id"],
            "editorial_decision_required": True,
            "editorial_candidates_by_source_form_role": {
                role: candidates_by_role.get(role, []) for role in roles
            },
            "source_form_roles": roles,
            "final_units": sorted({item["unit"] for item in anchors}),
            "exact_occurrence_count": len(anchors),
            "occurrence_anchors": anchors,
            "source_evidence_classification": sorted({item["source_evidence"] for item in anchors}),
            "review_blocker": "human_editorial_canonical_unresolved",
            "automatic_application_forbidden": True,
            "replacement_safe_automatically": False,
        })
        if len(roles) == 1:
            decision["editorial_candidates"] = editorial_candidates
        decisions.append(decision)

    legacy_claim_count = None
    if inventory is not None and spec.get("legacy_claims"):
        legacy_claim_count = 0
        for identity, rendering in spec["legacy_claims"]:
            _, variant = _inventory_variant(inventory, identity, rendering)
            if variant is None:
                raise ValueError(f"missing legacy editorial-family claim: {identity} / {rendering}")
            legacy_claim_count += variant.get("occurrence_count", 0)

    role_counts = Counter(anchor["source_form_role"] for anchor in unique.values())
    summary = {
        "family_id": spec["family_id"],
        "source_identity": spec["source_identity"],
        "canonical_editorial_choice": None,
        "editorial_decision_required": True,
        "application_authority": "none",
        "source_form_roles": role_map,
        "editorial_candidates_by_source_form_role": candidates_by_role,
        "short_to_full_expansion_automatically_authorized": False,
        "legacy_overlapping_claim_count": legacy_claim_count,
        "unique_physical_occurrence_count": len(unique),
        "duplicate_claims_removed": (
            legacy_claim_count - len(unique) if legacy_claim_count is not None else None
        ),
        "occurrence_role_counts": dict(sorted(role_counts.items())),
        "possessive_occurrence_count": role_counts["possessive_inflection"],
        "physical_occurrence_ids": sorted(unique),
    }
    return decisions, summary


def build_text_decisions(
    inventory: dict,
    source_units: list[dict],
    texts: dict[int, str],
    file_hashes: dict[int, str] | None = None,
    corpus_id: str = "unspecified-corpus",
    editorial_family_summaries: list[dict] | None = None,
) -> list[dict]:
    decisions = []
    handled = set()

    for identity, current, proposed, source_forms in APPROVED_RULES:
        entity, variant = _inventory_variant(inventory, identity, current)
        # Some reviewed identities are recorded as core-only candidates; the
        # exact complete phrase still owns the decision.
        if entity is None and identity in SPECIAL_IDENTITY_CORES:
            entity = next(item for item in inventory["entities"]["identities"] if item["source_identity"] == identity)
        if entity is None:
            raise ValueError(f"missing inventory identity for approved rule: {identity}")
        anchors = _target_occurrence_anchors(
            current, source_forms, source_units, texts, proposed, file_hashes, corpus_id
        )
        confirmed = [item for item in anchors if item["source_evidence"] != "unconfirmed_source_attribution"]
        unconfirmed = [item for item in anchors if item["source_evidence"] == "unconfirmed_source_attribution"]
        if confirmed:
            decision = _base_decision(
                identity, " / ".join(source_forms), current, proposed, "APPROVED_CORRECTION",
                "The exact target occurrence is locally bound to the authoritative Korean source identity; only that anchored occurrence may change.",
            )
            decision.update({
                "final_units": sorted({item["unit"] for item in confirmed}),
                "exact_occurrence_count": len(confirmed),
                "occurrence_anchors": confirmed,
                "source_evidence_classification": sorted({item["source_evidence"] for item in confirmed}),
                "replacement_safe_automatically": True,
            })
            decisions.append(decision)
        if unconfirmed and (identity, current) not in REVIEW_FALSE_POSITIVE_KEYS:
            decision = _base_decision(
                identity, " / ".join(source_forms), current, proposed, "REVIEW_REQUIRED",
                "The spelling is suspicious, but these exact occurrences could not be uniquely bound to the approved source form.",
            )
            decision.update({
                "final_units": sorted({item["unit"] for item in unconfirmed}),
                "exact_occurrence_count": len(unconfirmed),
                "occurrence_anchors": unconfirmed,
                "source_evidence_classification": "unconfirmed_source_attribution",
            })
            decisions.append(decision)
        handled.add((identity, current))

    for case in SPECIAL_NAME_CASES:
        anchors = _target_occurrence_anchors(
            case["proposed_rendering"], tuple(case["source_forms"]), source_units, texts,
            case["current_rendering"], file_hashes, corpus_id,
        )
        excluded = re.compile(case["excluded_regex"], re.I) if case.get("excluded_regex") else None
        confirmed = [
            item for item in anchors
            if item["source_evidence"] != "unconfirmed_source_attribution"
            and (excluded is None or not excluded.search(item["target_context"]))
        ]
        if confirmed:
            decision = _base_decision(
                case["identity"], case["identity"], case["proposed_rendering"], case["current_rendering"],
                "APPROVED_CORRECTION", case["reason"],
            )
            decision.update({
                "final_units": sorted({item["unit"] for item in confirmed}),
                "exact_occurrence_count": len(confirmed),
                "occurrence_anchors": confirmed,
                "source_evidence_classification": sorted({item["source_evidence"] for item in confirmed}),
                "replacement_safe_automatically": True,
            })
            decisions.append(decision)
        handled.add((case["identity"], case["current_rendering"]))

    for identity, current, reason in FALSE_POSITIVE_RULES:
        matches = []
        for unit, text in texts.items():
            for start, end in exact_occurrences(text, current):
                matches.append({"unit": unit, "target_context": short_context(text, start, end, radius=55)})
        decision = _base_decision(identity, identity, current, None, "FALSE_POSITIVE_NO_CHANGE", reason)
        decision.update({
            "final_units": sorted({item["unit"] for item in matches}),
            "exact_occurrence_count": len(matches),
            "occurrence_anchors": matches,
            "source_evidence_classification": "human_reviewed_distinct_identity_or_context",
        })
        decisions.append(decision)
        handled.add((identity, current))

    # Additional scanner hypotheses explicitly closed by reviewed decisions.
    handled.update(SPECIAL_HANDLED_KEYS)

    for case in EXACT_SOURCE_DISPROOFS:
        matches = []
        for unit, text in texts.items():
            for start, end in exact_occurrences(text, case["target_rendering"]):
                if case["source_form"] in source_units[unit - 1]["text"]:
                    matches.append({
                        "unit": unit,
                        "source_form": case["source_form"],
                        "target_context": short_context(text, start, end, radius=55),
                    })
        decision = _base_decision(
            case["identity"], case["identity"], case["target_rendering"], None,
            "FALSE_POSITIVE_NO_CHANGE", case["reason"],
        )
        decision.update({
            "final_units": sorted({item["unit"] for item in matches}),
            "exact_occurrence_count": len(matches),
            "occurrence_anchors": matches,
            "source_evidence_classification": "exact_authoritative_source_disproof",
        })
        decisions.append(decision)
        for pair in case.get("handled_keys", ()):
            handled.add(tuple(pair))

    # Resolve the source-attributable part of the V1 review queue.  Each
    # decision is rebuilt from exact source/target paragraph pairs and carries
    # enough baseline material for a future application tool to fail closed.
    for identity, source_forms, current, proposed, scopes in SCOPED_REVIEW_CORRECTIONS:
        anchors = _scoped_occurrence_anchors(
            current, proposed, source_units, texts, scopes, file_hashes, corpus_id
        )
        decision = _base_decision(
            identity, " / ".join(source_forms), current, proposed, "APPROVED_CORRECTION",
            "Human source review bound every listed target occurrence to an exact authoritative source paragraph and identity; no other occurrence is authorized.",
        )
        decision.update({
            "final_units": sorted({item["unit"] for item in anchors}),
            "exact_occurrence_count": len(anchors),
            "occurrence_anchors": anchors,
            "source_evidence_classification": "human_reviewed_exact_paragraph_pair",
            "replacement_safe_automatically": True,
        })
        decisions.append(decision)

    # The keys refer to the original V1 inventory decisions, even where a
    # reviewed correction uses a longer exact old phrase (for example the full
    # Dolocis or Quen name).  Marking them handled prevents a second, fuzzy
    # decision from being emitted.
    handled.update(REVIEW_APPROVED_KEYS)

    false_reasons = FALSE_REASONS
    for identity, current in sorted(REVIEW_FALSE_POSITIVE_KEYS):
        entity, variant = _inventory_variant(inventory, identity, current)
        if entity is None or variant is None:
            raise ValueError(f"missing V1 review variant for false-positive resolution: {identity} / {current}")
        if (identity, current) in REVIEW_FALSE_POSITIVE_SCOPES:
            anchors = _scoped_occurrence_anchors(
                current, None, source_units, texts,
                REVIEW_FALSE_POSITIVE_SCOPES[(identity, current)], file_hashes, corpus_id
            )
            occurrence_count = len(anchors)
            units = sorted({item["unit"] for item in anchors})
        else:
            anchors = variant.get("paragraph_aligned_evidence", variant.get("representative_contexts", []))
            occurrence_count = variant.get("occurrence_count", 0)
            units = variant.get("units", [])
        decision = _base_decision(
            identity, " / ".join(entity.get("source_forms", [identity])), current, None,
            "FALSE_POSITIVE_NO_CHANGE", false_reasons[(identity, current)],
        )
        decision.update({
            "final_units": units,
            "exact_occurrence_count": occurrence_count,
            "occurrence_anchors": anchors,
            "source_evidence_classification": "human_reviewed_exact_source_disproof_or_competing_identity",
        })
        decisions.append(decision)
    handled.update(REVIEW_FALSE_POSITIVE_KEYS)

    # Rebuild unresolved overlapping identity claims from physical target
    # occurrences.  Project-specific evidence is data; ownership, roles, and
    # non-executability are enforced by the generic family builder.
    for spec in ROGUE_EDITORIAL_FAMILY_SPECS:
        family_decisions, family_summary = build_editorial_family_decisions(
            spec, source_units, texts, file_hashes, corpus_id, inventory
        )
        decisions.extend(family_decisions)
        handled.update(spec.get("legacy_claims", ()))
        if editorial_family_summaries is not None:
            editorial_family_summaries.append(family_summary)

    # Preserve every remaining noncanonical inventory candidate for review.
    for entity in inventory["entities"]["identities"]:
        identity = entity["source_identity"]
        for variant in entity["english_variants"]:
            current = variant["rendering"]
            key = (identity, current)
            if key in handled or variant["relationship"] in {"full_name", "short_name", "identity_core"}:
                continue
            if variant.get("classification") == "NO_CHANGE":
                continue
            source_forms = tuple(entity.get("source_forms", [entity.get("source_key", identity)]))
            review_anchors = _target_occurrence_anchors(
                current, source_forms, source_units, texts,
                None, file_hashes, corpus_id
            )
            confirmed_count = sum(
                item["source_evidence"] != "unconfirmed_source_attribution" for item in review_anchors
            )
            if review_anchors and confirmed_count == len(review_anchors):
                blocker = "editorial_canonical_unresolved_despite_exact_source_attribution"
                reason = (
                    "Every occurrence is source-attributed, but the glossary itself preserves competing short/full spelling conventions; "
                    "source attribution cannot choose an editorial canonical."
                )
            elif review_anchors:
                blocker = "editorial_canonical_unresolved_and_some_paragraph_attribution_incomplete"
                reason = (
                    f"{confirmed_count} of {len(review_anchors)} exact target occurrences have local source attribution; the remainder cannot be "
                    "uniquely paired because source/target paragraph structure diverges, and the competing short/full canonical convention remains unresolved."
                )
            else:
                blocker = "no_exact_target_occurrence_anchor_recoverable"
                reason = "No exact target occurrence anchor could be reconstructed; automatic application is forbidden."
            decision = _base_decision(
                identity, " / ".join(entity.get("source_forms", [entity.get("source_key", identity)])), current,
                None, "REVIEW_REQUIRED",
                reason,
            )
            decision.update({
                "final_units": variant.get("units", []),
                "exact_occurrence_count": variant.get("occurrence_count", 0),
                "occurrence_anchors": review_anchors,
                "source_evidence_classification": sorted({item["source_evidence"] for item in review_anchors}),
                "review_blocker": blocker,
                "automatic_application_forbidden": True,
                "editorial_decision_required": True,
            })
            decisions.append(decision)

    for index, decision in enumerate(decisions, 1):
        decision["decision_id"] = _decision_id("text", index)
        if decision["status"] == "REVIEW_REQUIRED":
            decision["proposed_rendering"] = None
            decision["replacement_safe_automatically"] = False
            decision["automatic_application_forbidden"] = True
            decision.pop("exact_proposed_replacement", None)
            decision.pop("replacement_operation", None)
            decision.pop("executable_edit", None)
            for anchor in decision.get("occurrence_anchors", []):
                anchor.pop("exact_proposed_replacement", None)
                anchor.pop("replacement_operation", None)
                anchor.pop("executable_edit", None)
                anchor.setdefault("occurrence_id", physical_occurrence_id(corpus_id, anchor))
                anchor["occurrence_owner_id"] = decision["decision_id"]
    if not all(
        review_decision_is_non_executable(decision)
        for decision in decisions if decision["status"] == "REVIEW_REQUIRED"
    ):
        raise ValueError("review-required prose decision exposes executable edit authority")
    validate_unique_occurrence_ownership(decisions, corpus_id)
    return decisions


def parse_source_title_structure(normalized_korean_base_title: str) -> dict:
    main = re.match(r"^(\d+)\.\s*(.+)$", normalized_korean_base_title)
    if main:
        return {
            "title_kind": "main_story",
            "source_title_number": int(main.group(1)),
            "side_story_number": None,
            "korean_title_text": main.group(2).strip(),
        }
    side = re.match(r"^외전>?\s*(\d+)(?:편)?\.\s*(.+)$", normalized_korean_base_title)
    if side:
        return {
            "title_kind": "side_story",
            "source_title_number": None,
            "side_story_number": int(side.group(1)),
            "korean_title_text": side.group(2).strip(),
        }
    return {
        "title_kind": "epilogue" if normalized_korean_base_title == "에필로그" else "special",
        "source_title_number": None,
        "side_story_number": None,
        "korean_title_text": normalized_korean_base_title,
    }


def _english_title_text(value: str | None, structure: dict) -> str | None:
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).replace("’", "'").strip().strip("<>").strip()
    if structure["title_kind"] == "main_story":
        text = re.sub(r"^\d+\.\s*", "", text)
    elif structure["title_kind"] == "side_story":
        number = structure["side_story_number"]
        text = re.sub(rf"(?i)^side\s+story\s+{number}\s*[.:-]?\s*", "", text)
    return text.strip() or None


def _rank_existing_title_texts(group: dict, structure: dict) -> list[dict]:
    counts = Counter(
        text for item in group["english_renderings"]
        if (text := _english_title_text(item.get("english_base"), structure))
    )
    return [
        {"english_title_text": value, "occurrence_count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]


def find_source_title_discontinuities(source_units: list[dict]) -> list[dict]:
    by_number = {}
    for unit, source in enumerate(source_units, 1):
        base, part = split_title_part(str(source.get("title", "")))
        structure = parse_source_title_structure(base)
        number = structure["source_title_number"]
        if structure["title_kind"] != "main_story" or number is None:
            continue
        by_number.setdefault(number, {}).setdefault(structure["korean_title_text"], []).append({
            "final_unit": unit,
            "part_number": part,
        })
    results = []
    for number, titles in sorted(by_number.items()):
        if len(titles) <= 1:
            continue
        ordered = sorted(
            (member["final_unit"], member["part_number"], title)
            for title, members in titles.items() for member in members
        )
        parts = [part for _, part, _ in ordered]
        compact = {re.sub(r"[\W_]+", "", title) for title in titles}
        if len(compact) == 1:
            kind = "orthographic_or_spacing_variant"
        elif any("유료 시작 회차" in title for title in titles):
            kind = "source_metadata_suffix"
        elif all(part is not None for part in parts) and parts == list(range(1, len(parts) + 1)):
            kind = "continuous_part_sequence_across_distinct_titles"
        else:
            kind = "number_reuse_or_nonsequential_parts"
        results.append({
            "source_title_number": number,
            "distinct_korean_title_count": len(titles),
            "discontinuity_kind": kind,
            "title_identities": [
                {"korean_title_text": title, "units": members}
                for title, members in sorted(titles.items())
            ],
            "guard": "Never infer or synthesize a missing part across distinct Korean title identities.",
        })
    return results


def build_title_decisions(inventory: dict) -> list[dict]:
    decisions = []
    for group in inventory["titles"]["repeated_groups"]:
        base = group["normalized_korean_base_title"]
        structure = parse_source_title_structure(base)
        candidate = group.get("candidate_canonical_english_base")
        candidate_text = _english_title_text(candidate, structure)
        difference = group["difference"]
        override = TITLE_REVIEW_OVERRIDES.get(base, {})
        if base in override:
            status = "REVIEW_REQUIRED"
            canonical_text = None
        elif base in SEMANTICALLY_APPROVED_TITLES:
            status = "APPROVED_CANONICAL"
            canonical_text = SEMANTICALLY_APPROVED_TITLES[base]
        elif difference == "consistent":
            status = "ALREADY_CONSISTENT"
            canonical_text = candidate_text
        elif difference == "punctuation_or_capitalization_only":
            status = "STRUCTURAL_ONLY"
            canonical_text = candidate_text
        else:
            status = "REVIEW_REQUIRED"
            canonical_text = None

        unit_parts = []
        rendering_by_unit = {item["unit"]: item for item in group["english_renderings"]}
        missing_by_unit = {item["unit"]: item for item in group["missing_titles"]}
        for unit, part in zip(group["source_units"], group["part_numbers"]):
            rendering = rendering_by_unit.get(unit)
            unit_parts.append({
                "final_unit": unit,
                "part_number": part,
                "current_english_title_text": _english_title_text(
                    rendering.get("english_base") if rendering else None, structure
                ),
                "raw_heading": (rendering or missing_by_unit.get(unit, {})).get("raw_heading", ""),
                "usable_english_title_present": rendering is not None,
            })

        reason = {
            "ALREADY_CONSISTENT": "All existing parts already share one English title text after separating structural numbering.",
            "STRUCTURAL_ONLY": "The differences are limited to punctuation/capitalization and belong to publication normalization.",
            "APPROVED_CANONICAL": "A source-semantic audit confirmed that the approved title text preserves the meaning-bearing Korean content; frequency is supporting evidence only.",
            "REVIEW_REQUIRED": override.get(
                "semantic_note",
                "Existing renderings differ lexically/semantically or evidence is incomplete; no translation is invented or approved by frequency alone.",
            ),
        }[status]
        decisions.append({
            "title_id": _decision_id("title", len(decisions) + 1),
            "normalized_korean_base_title": base,
            **structure,
            "final_units": group["source_units"],
            "part_numbers": group["part_numbers"],
            "unit_parts": unit_parts,
            "current_english_titles": group["english_renderings"],
            "missing_titles": group["missing_titles"],
            "inventory_candidate_raw": candidate,
            "inventory_candidate_english_title_text": candidate_text,
            "ranked_existing_candidates": _rank_existing_title_texts(group, structure),
            "canonical_english_title_text": canonical_text,
            "human_editorial_candidates": override.get("human_editorial_candidates", []),
            "decision": status,
            "reason": reason,
            "part_numbers_are_source_explicit": True,
            "infer_missing_parts_from_sequence": False,
        })
    return decisions


def build_structural_decisions(inventory: dict) -> list[dict]:
    operations = {
        "literal_double_asterisk_separator": "normalize_scene_separator",
        "spaced_scene_separator": "normalize_scene_separator",
        "escaped_scene_separator": "normalize_scene_separator",
        "markdown_heading": "normalize_heading_structure",
        "markdown_bold_or_emphasis": "review_or_normalize_markdown_emphasis",
        "generator_marker": "remove_generator_control_marker",
        "literal_title_wrapper": "normalize_title_wrapper",
        "escaped_numbering": "unescape_heading_numbering",
        "generic_chapter_heading_followed_by_second_heading": "collapse_duplicate_heading_structure",
    }
    result = []
    for item in inventory["artifacts"]:
        if not item["occurrence_count"] or item["artifact"] not in operations:
            continue
        result.append({
            "structural_id": _decision_id("structure", len(result) + 1),
            "artifact": item["artifact"],
            "planned_operation": operations[item["artifact"]],
            "status": "STRUCTURAL_PUBLICATION",
            "occurrence_count": item["occurrence_count"],
            "affected_unit_count": item["affected_unit_count"],
            "final_units": item["affected_units"],
            "apply_in_prose_normalization": False,
            "reason": "Handle with a deterministic publication-format pass, never a global prose replacement.",
        })
    return result


def build_pronoun_decisions(
    inventory: dict,
    source_units: list[dict],
    texts: dict[int, str],
    file_hashes: dict[int, str] | None = None,
) -> list[dict]:
    cases = {case["character"]: case for case in PRONOUN_CASES}
    decisions = []
    for candidate in inventory["pronoun_candidates"]:
        case = cases.get(candidate["character"])
        if case is None:
            raise ValueError("pronoun candidate has no reviewed private case")
        unit = candidate["unit"]
        target = texts[unit]
        target_parts = split_paragraphs(target)
        source_parts = split_paragraphs(source_units[unit - 1]["text"])
        current_first = case["current_first_sentence"]
        current_second = case["current_second_sentence"]
        target_indexes = [index for index, part in enumerate(target_parts) if part == current_first]
        minimums = case.get("source_first_min_counts", {})
        source_indexes = [
            index for index, part in enumerate(source_parts)
            if all(token in part for token in case["source_first_tokens"])
            and all(part.count(token) >= count for token, count in minimums.items())
        ]
        if len(target_indexes) != 1 or len(source_indexes) != 1:
            raise ValueError("pronoun occurrence no longer matches the reviewed corpus")
        target_index = target_indexes[0]
        source_index = source_indexes[0]
        if target_index + 1 >= len(target_parts) or target_parts[target_index + 1] != current_second:
            raise ValueError("pronoun omitted-subject continuation no longer matches the reviewed corpus")
        if source_index + 1 >= len(source_parts) or case["source_continuation_token"] not in source_parts[source_index + 1]:
            raise ValueError("pronoun source continuation is no longer uniquely established")
        counts = candidate["dominant_counts"]
        expected = case["expected_gender"]
        others = [counts.get(name, 0) for name in ("masculine", "feminine", "neutral") if name != expected]
        if counts.get(expected, 0) <= max(others or [0]):
            raise ValueError("pronoun established evidence no longer matches the reviewed gender")
        decisions.append({
            "pronoun_id": case["pronoun_id"],
            "unit": unit,
            "character": case["character"],
            "source_identity": case["source_identity"],
            "current_rendering": case["current_rendering"],
            "proposed_rendering": case["proposed_rendering"],
            "exact_occurrence_count": case["exact_occurrence_count"],
            "pronoun_token_change_count": case["pronoun_token_change_count"],
            "decision": "APPROVED_CORRECTION",
            "automatic_safe": False,
            "occurrence_anchors": [
                {
                    "expected_final_unit": unit,
                    "expected_baseline_file_sha256": (file_hashes or {}).get(
                        unit, sha256_bytes(target.encode("utf-8"))
                    ),
                    "target_paragraph": target_index + 1,
                    "source_paragraph": source_index + 1,
                    "expected_target_paragraph": current_first,
                    "expected_target_paragraph_sha256": sha256_bytes(current_first.encode("utf-8")),
                    "target_sentence": current_first,
                    "proposed_sentence": case["proposed_first_sentence"],
                    "exact_old_text": current_first,
                    "exact_proposed_replacement": case["proposed_first_sentence"],
                    "pronoun_token_change_count": case["per_sentence_change_count"],
                },
                {
                    "expected_final_unit": unit,
                    "expected_baseline_file_sha256": (file_hashes or {}).get(
                        unit, sha256_bytes(target.encode("utf-8"))
                    ),
                    "target_paragraph": target_index + 2,
                    "source_paragraph": source_index + 2,
                    "expected_target_paragraph": current_second,
                    "expected_target_paragraph_sha256": sha256_bytes(current_second.encode("utf-8")),
                    "target_sentence": current_second,
                    "proposed_sentence": case["proposed_second_sentence"],
                    "exact_old_text": current_second,
                    "exact_proposed_replacement": case["proposed_second_sentence"],
                    "pronoun_token_change_count": case["per_sentence_change_count"],
                },
            ],
            "source_evidence": (
                f"The first aligned source sentence contains {case['source_identity']} and the reviewed "
                f"gender-neutral forms; the next continues the same omitted subject. Established English "
                f"references identify {case['character']} as {expected} "
                f"({counts.get('masculine', 0)} masculine, {counts.get('feminine', 0)} feminine, "
                f"{counts.get('neutral', 0)} neutral in the inventory scan)."
            ),
            "reason": case["reason"],
        })
    return decisions


def build_plan(project) -> dict:
    report_dir = project.root / "final" / "reports"
    inventory_path = report_dir / "normalization_inventory.json"
    inventory_bytes = inventory_path.read_bytes()
    inventory = json.loads(inventory_bytes.decode("utf-8"))
    fingerprint = final_corpus_fingerprint(project.root / "final" / "chapters")
    if fingerprint["aggregate_sha256"] != inventory["corpus"]["final_aggregate_sha256"]:
        raise ValueError("final corpus does not match the authoritative inventory")
    if fingerprint["aggregate_sha256"] != EXPECTED_FINAL_SHA256:
        raise ValueError("final corpus does not match the reviewed decision-plan baseline")

    source_units = load_corrected_txt_spine(project)["chapters"]
    texts = {
        index: (project.root / "final" / "chapters" / f"{index:04d}.md").read_text(encoding="utf-8")
        for index in range(1, EXPECTED_FINAL_COUNT + 1)
    }
    file_hashes = {item["unit"]: item["sha256"] for item in fingerprint["files"]}
    editorial_families = []
    text_decisions = build_text_decisions(
        inventory,
        source_units,
        texts,
        file_hashes,
        project.config.id,
        editorial_families,
    )
    for family in editorial_families:
        family["decision_ids"] = [
            item["decision_id"]
            for item in text_decisions
            if item.get("editorial_family_id") == family["family_id"]
        ]
    title_decisions = build_title_decisions(inventory)
    structural_decisions = build_structural_decisions(inventory)
    pronoun_decisions = build_pronoun_decisions(inventory, source_units, texts, file_hashes)

    text_counts = Counter(item["status"] for item in text_decisions)
    title_counts = Counter(item["decision"] for item in title_decisions)
    pronoun_counts = Counter(item["decision"] for item in pronoun_decisions)
    if not all(
        approved_decision_is_fail_closed(item)
        for item in text_decisions if item["status"] == "APPROVED_CORRECTION"
    ):
        raise ValueError("approved prose correction lacks fail-closed application preconditions")
    expected_text_counts = {
        "APPROVED_CORRECTION": 33,
        "FALSE_POSITIVE_NO_CHANGE": 26,
        "REVIEW_REQUIRED": 4,
    }
    if {key: text_counts[key] for key in expected_text_counts} != expected_text_counts:
        raise ValueError("accepted V2 text-decision counts changed unexpectedly")
    approved_occurrences = sum(
        item["exact_occurrence_count"]
        for item in text_decisions if item["status"] == "APPROVED_CORRECTION"
    )
    if approved_occurrences != 235:
        raise ValueError("accepted V2 approved terminology/name occurrence count changed unexpectedly")
    expected_title_counts = {
        "APPROVED_CANONICAL": 32,
        "REVIEW_REQUIRED": 48,
        "ALREADY_CONSISTENT": 10,
        "STRUCTURAL_ONLY": 2,
    }
    if {key: title_counts[key] for key in expected_title_counts} != expected_title_counts:
        raise ValueError("accepted V2 title-decision counts changed unexpectedly")
    title_discontinuities = find_source_title_discontinuities(source_units)
    plan = {
        "schema_version": 3,
        "plan_revision": "2.1",
        "mode": "decision-plan-read-only-zero-api",
        "corpus": {
            "project_id": project.config.id,
            "inventory_sha256": sha256_bytes(inventory_bytes),
            "inventory_recorded_final_sha256": inventory["corpus"]["final_aggregate_sha256"],
            "verified_final_fingerprint": fingerprint,
        },
        "safety": {
            "applies_changes": False,
            "source_identity_required": True,
            "global_string_replacement_allowed": False,
            "final_chapters_modified": False,
            "api_calls": 0,
        },
        "application_policy": {
            "mode": "fail_closed_exact_occurrence_only",
            "required_per_approved_prose_occurrence": [
                "expected_baseline_file_sha256",
                "expected_final_unit",
                "expected_target_paragraph",
                "expected_target_paragraph_sha256",
                "exact_old_text",
                "expected occurrence number/count",
                "source identity evidence",
                "exact_proposed_replacement",
            ],
            "paragraph_number_alone_is_sufficient": False,
            "global_replacement_authorized": False,
            "abort_on_any_precondition_mismatch": True,
            "review_required_is_executable": False,
            "review_required_forbidden_fields": [
                "exact_proposed_replacement",
                "replacement_operation",
                "executable_edit",
            ],
        },
        "text_decisions": text_decisions,
        "editorial_families": editorial_families,
        "title_decisions": title_decisions,
        "source_title_discontinuities": title_discontinuities,
        "structural_publication_decisions": structural_decisions,
        "pronoun_decisions": pronoun_decisions,
        "v1_text_review_refinement": {
            "input_review_decision_count": 30,
            "APPROVED_CORRECTION": len(REVIEW_APPROVED_KEYS),
            "FALSE_POSITIVE_NO_CHANGE": len(REVIEW_FALSE_POSITIVE_KEYS),
            "REVIEW_REQUIRED": 4,
            "note": "Counts classify the 30 V1 review records. A split occurrence decision may emit more than one V2 plan record.",
        },
        "summary_counts": {
            "APPROVED_CORRECTION": text_counts["APPROVED_CORRECTION"],
            "APPROVED_CORRECTION_OCCURRENCES": approved_occurrences,
            "PRONOUN_APPROVED_CORRECTION_OCCURRENCES": sum(
                item["pronoun_token_change_count"]
                for item in pronoun_decisions if item["decision"] == "APPROVED_CORRECTION"
            ),
            "TOTAL_APPROVED_PROSE_OCCURRENCES": (
                approved_occurrences
                + sum(
                    item["pronoun_token_change_count"]
                    for item in pronoun_decisions if item["decision"] == "APPROVED_CORRECTION"
                )
            ),
            "FALSE_POSITIVE_NO_CHANGE": text_counts["FALSE_POSITIVE_NO_CHANGE"],
            "REVIEW_REQUIRED": text_counts["REVIEW_REQUIRED"],
            "TITLE_APPROVED": title_counts["APPROVED_CANONICAL"],
            "TITLE_REVIEW_REQUIRED": title_counts["REVIEW_REQUIRED"],
            "TITLE_ALREADY_CONSISTENT": title_counts["ALREADY_CONSISTENT"],
            "TITLE_STRUCTURAL_ONLY": title_counts["STRUCTURAL_ONLY"],
            "STRUCTURAL_PUBLICATION": len(structural_decisions),
            "PRONOUN_DECISIONS": dict(sorted(pronoun_counts.items())),
        },
    }
    return plan


def render_markdown(plan: dict) -> str:
    counts = plan["summary_counts"]
    lines = [
        "# Rogue First Prince Normalization Decision Plan",
        "",
        "This plan is read-only. It applies no corrections and authorizes no global string replacement.",
        "",
        "## Verified baseline",
        "",
        f"- Final units: {plan['corpus']['verified_final_fingerprint']['file_count']}",
        f"- Aggregate bytes: {plan['corpus']['verified_final_fingerprint']['aggregate_byte_count']}",
        f"- Aggregate SHA-256: `{plan['corpus']['verified_final_fingerprint']['aggregate_sha256']}`",
        "- API calls: 0",
        "",
        "## Decision counts",
        "",
        f"- Approved source-scoped text corrections: {counts['APPROVED_CORRECTION']} ({counts['APPROVED_CORRECTION_OCCURRENCES']} exact occurrences)",
        f"- Approved prose occurrences including the scoped pronoun decision: {counts['TOTAL_APPROVED_PROSE_OCCURRENCES']}",
        f"- False-positive / no-change decisions: {counts['FALSE_POSITIVE_NO_CHANGE']}",
        f"- Text decisions still requiring review: {counts['REVIEW_REQUIRED']}",
        f"- Title approved: {counts['TITLE_APPROVED']}",
        f"- Title review required: {counts['TITLE_REVIEW_REQUIRED']}",
        f"- Title already consistent: {counts['TITLE_ALREADY_CONSISTENT']}",
        f"- Title structural only: {counts['TITLE_STRUCTURAL_ONLY']}",
        f"- Structural publication classes: {counts['STRUCTURAL_PUBLICATION']}",
        f"- V1 review decisions resolved as approved / false positive / still review: "
        f"{plan['v1_text_review_refinement']['APPROVED_CORRECTION']} / "
        f"{plan['v1_text_review_refinement']['FALSE_POSITIVE_NO_CHANGE']} / "
        f"{plan['v1_text_review_refinement']['REVIEW_REQUIRED']}",
        "",
        "## Approved source-scoped text corrections",
        "",
    ]
    for item in plan["text_decisions"]:
        if item["status"] != "APPROVED_CORRECTION":
            continue
        lines.append(
            f"- `{item['source_identity']}`: `{item['current_rendering']}` → `{item['proposed_rendering']}`; "
            f"{item['exact_occurrence_count']} occurrence(s), units {item['final_units']}."
        )
    lines += ["", "## False positives / no change", ""]
    for item in plan["text_decisions"]:
        if item["status"] == "FALSE_POSITIVE_NO_CHANGE":
            lines.append(f"- `{item['current_rendering']}` — {item['reason']}")
    lines += ["", "## Review required", ""]
    for item in plan["text_decisions"]:
        if item["status"] == "REVIEW_REQUIRED":
            lines.append(
                f"- `{item['source_identity']}` / `{item['current_rendering']}`; "
                f"{item['exact_occurrence_count']} occurrence(s), units {item['final_units']}; "
                "non-executable pending human editorial choice."
            )
    lines += ["", "## Unresolved editorial families", ""]
    for family in plan["editorial_families"]:
        lines.append(
            f"- `{family['family_id']}`: {family['legacy_overlapping_claim_count']} legacy claims → "
            f"{family['unique_physical_occurrence_count']} unique owned occurrences "
            f"({family['duplicate_claims_removed']} duplicate claims removed); roles "
            f"{family['occurrence_role_counts']}; application authority `{family['application_authority']}`."
        )
    lines += ["", "## Chapter-title decisions", ""]
    for item in plan["title_decisions"]:
        lines.append(
            f"- `{item['normalized_korean_base_title']}` — {item['decision']}; "
            f"units {item['final_units']}; canonical title text `{item['canonical_english_title_text']}`."
        )
        if item["decision"] == "REVIEW_REQUIRED" and item.get("human_editorial_candidates"):
            lines.append(f"  - Human editorial candidate(s): {item['human_editorial_candidates']}")
    lines += ["", "## Source-title discontinuity guards", ""]
    for item in plan["source_title_discontinuities"]:
        identities = "; ".join(
            f"{entry['korean_title_text']} {entry['units']}" for entry in item["title_identities"]
        )
        lines.append(f"- Source title number {item['source_title_number']}: {identities}. {item['guard']}")
    lines += ["", "## Structural publication plan", ""]
    for item in plan["structural_publication_decisions"]:
        lines.append(
            f"- `{item['artifact']}`: {item['occurrence_count']} occurrence(s) in "
            f"{item['affected_unit_count']} unit(s); `{item['planned_operation']}`."
        )
    lines += ["", "## Pronoun decision", ""]
    for item in plan["pronoun_decisions"]:
        lines.append(
            f"- Unit {item['unit']}, {item['character']}: **{item['decision']}** — {item['reason']}"
        )
    lines += [
        "",
        "The JSON report is authoritative and contains exact unit/paragraph anchors and source-evidence classifications.",
        "",
    ]
    return "\n".join(lines)


def write_plan(project, plan: dict) -> tuple[Path, Path]:
    reports = project.root / "final" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "normalization_plan.json"
    markdown_path = reports / "normalization_plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(plan), encoding="utf-8")
    return json_path, markdown_path


def main(argv=None) -> int:
    if not PRIVATE_INPUTS_AVAILABLE:
        print("PRIVATE_INPUTS_UNAVAILABLE: provide the operator-local JSON input file; no novel-specific mappings are embedded in this public source.", file=_sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Build a read-only source-aware normalization decision plan")
    parser.add_argument("--novel", required=True)
    args = parser.parse_args(argv)
    try:
        project = select_project(args.novel)
        with production_run_lock(project.root):
            plan = build_plan(project)
            json_path, markdown_path = write_plan(project, plan)
    except ProductionRunLockedError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Final units: {plan['corpus']['verified_final_fingerprint']['file_count']}")
    print(f"Final SHA-256: {plan['corpus']['verified_final_fingerprint']['aggregate_sha256']}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print("API calls used: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
