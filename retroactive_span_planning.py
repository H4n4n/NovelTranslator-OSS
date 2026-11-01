"""Deterministic, conservative historical terminology span authorization.

No provider work or filesystem writes. Offsets are UTF-8 byte offsets; every
decision is made against the complete glossary inventory, never category alone.
"""

from functools import lru_cache
import json
import re
from hashlib import sha256

from noveltranslator_core import (
    _claim_longest_non_overlapping_occurrences,
    GENERIC_ARTIFACT_WORDS,
    NONCOUNTABLE_TERMINOLOGY_FINAL_WORDS,
    normalize_category,
    retroactive_replacement_is_safe,
    strict_term_valid_targets,
)

POLICY_VERSION = "owned-single-span-v1"
COUNT_MATCH_POLICY_VERSION = "owned-count-match-v2"
SPAN_POLICIES = (POLICY_VERSION, COUNT_MATCH_POLICY_VERSION)
OVER_COUNT_REASON = "more_target_spans_than_source_anchors"
SCHEMA_VERSION = 1
TARGET_FIELDS = ("english", "approved_renderings", "accepted_inflections",
                 "repair_aliases", "aliases")


def span_policy_of(request):
    """Requests made before count matching have no field and stay on the first rule."""
    return request.get("span_policy", POLICY_VERSION)


def digest(content):
    return sha256(content).hexdigest()


def canonical_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))


def normalized_rules(replacements):
    rules = []
    for raw in replacements:
        rule = {field: str(raw.get(field, "") or "").strip()
                for field in ("chinese", "old_english", "new_english", "reason")}
        rule["category"] = normalize_category(raw.get("category", ""))
        if any(len(value) > 1024 or "\n" in value or "\r" in value
               for value in rule.values()):
            raise ValueError("retroactive rule fields must be bounded single-line terms")
        rules.append(rule)
    # Order and duplicate proposals do not change the operation identity.
    unique = {canonical_hash(rule): rule for rule in rules}
    return [unique[key] for key in sorted(unique)]


def ownership_policy(glossary, staged_glossary=None):
    """Bind only ownership inputs, including both pre/post audit renderings.

    Distinct alias keys remain competitors: merging them does not prove which
    historical target occurrence belonged to which source key.
    """
    result = {}
    for data in (glossary, staged_glossary):
        if data is None:
            continue
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("retroactive ownership requires glossary entries")
        for key, entry in sorted(entries.items()):
            if not isinstance(entry, dict):
                raise ValueError(f"invalid glossary ownership entry: {key}")
            item = result.setdefault(key, {"targets": [], "contextual": False,
                                           "alias_of": [], "source_forms": [key]})
            item["contextual"] |= entry.get("status") == "contextual"
            alias = entry.get("alias_of", "")
            if alias:
                item["alias_of"].append(alias)
            for field in TARGET_FIELDS:
                values = entry.get(field, [])
                if isinstance(values, str):
                    values = [values]
                if not isinstance(values, (list, tuple)) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise ValueError(f"invalid glossary target inventory: {key}/{field}")
                item["targets"].extend(value.strip() for value in values if value.strip())
            item["targets"].extend(strict_term_valid_targets(entry))
            for field in ("aliases", "source_variants"):
                values = entry.get(field, [])
                if not isinstance(values, (list, tuple)) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise ValueError(f"invalid glossary source inventory: {key}/{field}")
                item["source_forms"].extend(value.strip() for value in values if value.strip())
    for item in result.values():
        item["targets"] = sorted(set(item["targets"]))
        item["alias_of"] = sorted(set(item["alias_of"]))
        item["source_forms"] = sorted(set(item["source_forms"]))
        if any(len(value) > 1024 or "\n" in value or "\r" in value
               for field in ("targets", "alias_of", "source_forms") for value in item[field]):
            raise ValueError("glossary ownership terms must be bounded single-line strings")
    return result


@lru_cache(maxsize=16384)
def _phrase_pattern(phrase, flags):
    # A large glossary overflows the re module's own cache, and an audit plans every chapter many times.
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", flags)


def _folded(text):
    # Dotless i is the one letter IGNORECASE matches that casefold keeps apart.
    return text.casefold().replace("ı", "i")


@lru_cache(maxsize=16384)
def _folded_phrase(phrase):
    return _folded(phrase)


def target_spans(text, phrase, *, reserve_case_variants=False):
    """Exact phrase boundaries include Unicode letters/digits and underscores."""
    return [(match.start(), match.end()) for match in _phrase_pattern(
        phrase, re.IGNORECASE if reserve_case_variants else 0).finditer(text)]


def overlaps(left, right):
    return left[0] < right[1] and right[0] < left[1]


def rule_eligibility(rule, policy, safe_categories):
    source, old, new = (rule[key] for key in ("chinese", "old_english", "new_english"))
    if not source:
        return "missing_source_identity"
    if not retroactive_replacement_is_safe(old, new, rule["category"], safe_categories):
        return "category_or_generic_safety_gate"
    if ((not old[0].isupper() and " " not in old) or old.casefold() in
            set(GENERIC_ARTIFACT_WORDS) | set(NONCOUNTABLE_TERMINOLOGY_FINAL_WORDS)):
        return "generic_or_common_target"
    if " " not in old and "-" not in old and rule["category"] not in {
        "character", "historical person", "alias"
    }:
        return "single_word_nonreferential_target_requires_manual_review"
    entry = policy.get(source)
    if entry is None:
        return "missing_glossary_identity"
    if entry["contextual"]:
        return "contextual_identity_requires_manual_review"
    if entry["alias_of"]:
        return "alias_identity_requires_manual_review"
    if old not in entry["targets"]:
        return "old_rendering_not_owned_by_glossary_identity"
    return ""


def plan_chapter(source, before, rules, policy, safe_categories, span_policy=POLICY_VERSION):
    """Return disjoint authorized edits and per-rule decisions, without guessing.

    Longest source ownership uses the mature core claiming machinery across ALL
    glossary keys. Equal/overlapping target claims are rejected, never resolved
    by lexical tie-breaking.

    Count matching edits every English occurrence when the chapter has at least
    as many owned source occurrences. English can have fewer, because a term can
    be referred to by pronoun. English can also have more, where the translation
    names what the source left implied: that is safe for a multi-word rendering
    nothing else owns, but a single word may be something else, so it is skipped.
    """
    count_match = span_policy == COUNT_MATCH_POLICY_VERSION
    text = before.decode("utf-8")  # Preserve BOM, CRLF, Markdown and all other bytes.
    source_claims = _claim_longest_non_overlapping_occurrences(
        source, [(key, item["source_forms"]) for key, item in policy.items()]
    )
    raw_source_claims = [claim for key, item in policy.items()
                         for claim in _claim_longest_non_overlapping_occurrences(
                             source, [(key, item["source_forms"])]
                         ).get(key, [])]
    folded_text = _folded(text)
    families = [(key, target, span) for key, item in policy.items()
                for target in item["targets"] if _folded_phrase(target) in folded_text
                for span in target_spans(text, target, reserve_case_variants=True)]
    # Include proposed old/new targets in competing families even if the staged
    # glossary has not installed them yet. Never chain historical corrections.
    families += [(rule["chinese"], rule[field], span) for rule in rules
                 for field in ("old_english", "new_english")
                 if rule[field] for span in target_spans(text, rule[field], reserve_case_variants=True)]
    decisions, edits = [], []
    for index, rule in enumerate(rules):
        reason = rule_eligibility(rule, policy, safe_categories)
        decision = {"rule_index": index, "category_eligible":
                    rule["category"] in safe_categories, "status": "skipped"}
        anchors = source_claims.get(rule["chinese"], [])
        spans = target_spans(text, rule["old_english"]) if rule["old_english"] else []
        decision.update(source_owned_count=len(anchors), target_candidate_count=len(spans))
        if not reason and not anchors:
            reason = "source_absent_or_owned_by_longer_identity"
        if not reason and not any(anchor["phrase"] == rule["chinese"] for anchor in anchors):
            reason = "exact_source_identity_not_owned"
        if not reason:
            if count_match and len(spans) > len(anchors) and " " not in rule["old_english"]:
                reason = OVER_COUNT_REASON
            elif not spans or (not count_match and (len(anchors) != 1 or len(spans) != 1)):
                reason = "cardinality_requires_manual_review"
        if not reason:
            if any(other["owner"] != rule["chinese"] and
                   overlaps((anchor["start"], anchor["end"]), (other["start"], other["end"])) and
                   not (anchor["start"] <= other["start"] and other["end"] <= anchor["end"] and
                        other["end"] - other["start"] < anchor["end"] - anchor["start"])
                   for anchor in anchors for other in raw_source_claims):
                reason = "competing_source_ownership"
        if not reason:
            # Reserve case variants as well, to reject competing same-string
            # identities even when their canonical target has different casing.
            competing = [key for key, item in policy.items() if key != rule["chinese"]
                         and any(target.casefold() == rule["old_english"].casefold()
                                 for target in item["targets"])]
            if competing or any(overlaps(span, other) and
                                (owner != rule["chinese"] or other != span)
                                for span in spans for owner, _target, other in families):
                reason = "competing_or_nested_target_ownership"
        if reason:
            decision["skip_reason"] = reason
        else:
            old, new = rule["old_english"].encode("utf-8"), rule["new_english"].encode("utf-8")
            for position, (start, end) in enumerate(spans):
                anchor = anchors[min(position, len(anchors) - 1)]
                edits.append({"rule_index": index, "source_identity": rule["chinese"],
                              "start": len(text[:start].encode("utf-8")),
                              "end": len(text[:end].encode("utf-8")),
                              "old_text": rule["old_english"], "old_sha256": digest(old),
                              "replacement": rule["new_english"], "replacement_sha256": digest(new),
                              "source_span": {key: anchor[key] for key in ("start", "end")},
                              "ownership": ("owned_source_count_covers_bounded_targets" if count_match
                                            else "one_owned_source_one_unique_bounded_target")})
            decision["status"] = "planned"
        decisions.append(decision)
    conflicts = {edit["rule_index"] for edit in edits for other in edits
                 if edit is not other and overlaps((edit["start"], edit["end"]),
                                                   (other["start"], other["end"]))}
    for index in conflicts:
        decisions[index].update(status="skipped", skip_reason="overlapping_retroactive_rules")
    return sorted((edit for edit in edits if edit["rule_index"] not in conflicts),
                  key=lambda edit: edit["start"]), decisions


def transform_bytes(before, edits):
    """Apply only verified disjoint byte spans from right to left."""
    cursor = 0
    for edit in edits:
        start, end = edit["start"], edit["end"]
        old, new = edit["old_text"].encode("utf-8"), edit["replacement"].encode("utf-8")
        if not (cursor <= start < end <= len(before)) or before[start:end] != old:
            raise ValueError("planned span mismatch or overlap")
        if digest(old) != edit["old_sha256"] or digest(new) != edit["replacement_sha256"]:
            raise ValueError("planned span hash mismatch")
        cursor = end
    result = before
    for edit in reversed(edits):
        result = result[:edit["start"]] + edit["replacement"].encode("utf-8") + result[edit["end"]:]
    return result
