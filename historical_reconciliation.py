"""AUT-2 local evidence: full-range discovery and conservative freshness.

This module never authorizes a prose edit, provider request, or regeneration.
The existing audit intent and owned-span transaction remain the only writers.
Reports contain opaque identities/counts/hashes, never source or English prose.
"""
import copy
import json
import math
from pathlib import Path

from noveltranslator_core import atomic_write_json, strict_term_valid_targets
from production_failure_classification import ProductionIntegrityError
from retroactive_span_planning import canonical_hash, digest, target_spans, ownership_policy, plan_chapter, normalized_rules
import retroactive_transactions as tx

REVISION = "aut2-history-evidence-v1"
PREFLIGHT_REVISION = "aut2-impact-preflight-v1"
MAX_DISCOVERY_CHAPTERS = 10000
MAX_DISCOVERY_CHANGES = 64
MAX_DISCOVERY_ROWS = 100000


def require(condition, reason):
    if not condition:
        raise ProductionIntegrityError("AUT2: " + reason)


def plain(path):
    path = Path(path)
    require(not any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
                    for p in (path, *path.parents)), "LINKED_EVIDENCE")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite evidence number")
    return number


def _invalid_constant(value):
    raise ValueError("nonfinite evidence constant")


def read_value(path):
    try:
        # Decode explicitly: json.loads(bytes) also accepts UTF-16/32. Existing
        # UTF-8/BOM, whitespace, CRLF and pretty printing remain valid.
        value = json.loads(plain(path).read_bytes().decode("utf-8-sig"),
                           object_pairs_hook=_unique_object, parse_float=_finite_float,
                           parse_constant=_invalid_constant)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OSError, RecursionError) as exc:
        raise ProductionIntegrityError("AUT2: UNREADABLE_EVIDENCE") from exc
    return value


def read_object(path):
    value = read_value(path)
    require(isinstance(value, dict), "INVALID_EVIDENCE")
    return value



def audit_intent(module, path):
    from production_periodic_audit_finalization import load_intent
    value = read_object(path)
    require(type(value.get("start")) is int and type(value.get("end")) is int
            and 1 <= value["start"] <= value["end"], "INVALID_AUDIT_BOUNDARY")
    try:
        intent = load_intent(module, value["start"], value["end"])
        transaction = intent["transaction_plan"]["transaction_id"]
        require(isinstance(transaction, str) and len(transaction) == 64
                and all(c in "0123456789abcdef" for c in transaction), "INVALID_TRANSACTION_ID")
        require(path.name == f"audit_{intent['start']:04d}_{intent['end']:04d}.finalization.json",
                "AUDIT_PATH_MISMATCH")
        require(isinstance(intent["record"], dict), "INVALID_AUDIT_RECORD")
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_AUDIT_INTENT") from exc
    return intent


def source_inventory(chapter_map):
    require(all(type(n) is int and n > 0 and isinstance(unit, dict)
                and isinstance(unit.get("text"), str) for n, unit in chapter_map.items()),
            "INVALID_SOURCE_INVENTORY")
    return [{"chapter": n, "unit_sha256": canonical_hash(unit),
             "source_sha256": digest(unit["text"].encode("utf-8"))}
            for n, unit in sorted(chapter_map.items())]


def _discovery_intent(module, start, end, expected_sha):
    """Reprove the existing finalizer's intent; never acquire or finalize it.

    This adapter supports the current audit publication prefix only. Older
    superseded state needs the separate lineage owner, not a guessed baseline.
    """
    from types import SimpleNamespace
    from production_periodic_audit_finalization import (
        intent_path, state_paths, verify_publication, transaction_proof,
        prepare_finalization,
    )
    path = plain(intent_path(module, start, end))
    require(path.is_file(), "MISSING_ACCEPTED_CHANGE_PROVENANCE")
    intent = audit_intent(module, path)
    require(intent["intent_sha256"] == expected_sha, "DISCOVERY_INTENT_DRIFT")
    novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
    request = intent["transaction_plan"]["request"]
    require(request["novel_id"] == novel and request["audit_label"] == f"{start:04d}_{end:04d}"
            and request["through_chapter"] == end, "FOREIGN_DISCOVERY_INTENT")
    require("historical_proof" in intent and "historical_reconciliation" in intent["record"],
            "DISCOVERY_SOURCE_UNIT_WITNESS_MISSING")
    require(module.progress["last_completed"] == end, "SUPERSEDED_AUDIT_REQUIRES_LINEAGE_REVIEW")
    verify_publication(module, intent)
    for record in intent["baseline"]["history"]["audits"]:
        record_path = plain(module.AUDIT_DIR /
            f"audit_{record['start_chapter']:04d}_{record['end_chapter']:04d}.json")
        require(read_object(record_path) == record, "DISCOVERY_PRIOR_AUDIT_RECORD_DRIFT")
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        require(read_object(state_paths(module)[key]) == getattr(module, name), "DISCOVERY_STATE_DRIFT")
    # Reject ambiguous JSON even when the older transaction reader would accept it.
    for evidence in plain(module.RETROACTIVE_DIR / "transactions").glob("*.json"):
        read_object(evidence)
    detached = SimpleNamespace(**vars(module))
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
    if detached.progress.get("provider") == "command_code_provider" and intent["chunking"].get("candidate_count", 0):
        from goat_periodic_audit_finalize_plan import checkpoint_proof
        paid = checkpoint_proof(detached, start, end)
        require(all(paid[k] == intent[k] for k in ("audit_result", "audit_stats")),
                "PAID_AUDIT_EVIDENCE_DRIFT")
    proof = transaction_proof(detached, start, end, intent["baseline"]["glossary"],
                              intent["post"]["glossary"], intent["record"]["changes"])
    prepared = prepare_finalization(detached, start, end, intent["audit_result"], intent["audit_stats"],
                                   intent["chunking"], timestamp=intent["record"]["timestamp_unix"])
    for record in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
        record["retroactive"] = intent["record"]["retroactive"]
    require(prepared == {k: v for k, v in intent.items() if k != "intent_sha256"},
            "DISCOVERY_AUDIT_AUTHORIZATION_DRIFT")
    return intent, proof


def historical_impact_preflight(module, changes, *, through_chapter,
                               audit_start=None, audit_end=None, expected_intent_sha256=None):
    """Bounded read-only discovery over 1..through_chapter, with no cache.

    ``changes`` uses the existing retroactive rule shape (including ``chinese``
    for Korean source identities). Supply the independently selected immutable
    periodic intent hash and window to reprove accepted provenance. Unwitnessed
    proposals remain deferred. A missing/drifted *supplied* witness blocks.

    Caller supplies the existing initialized local module under the cooperative
    novel writer exclusion; this API does not load a provider, take a writing
    lock, persist a report, or expand transaction authority. Eligibility means
    only that CURRENT plan_chapter policy can prove the current bytes.
    """
    from noveltranslator_core import _claim_longest_non_overlapping_occurrences
    from retroactive_span_planning import POLICY_VERSION
    from production_periodic_audit_finalization import state_paths

    require(type(through_chapter) is int and 1 <= through_chapter <= MAX_DISCOVERY_CHAPTERS,
            "DISCOVERY_BOUNDARY_LIMIT")
    require(isinstance(changes, list) and 0 < len(changes) <= MAX_DISCOVERY_CHANGES,
            "DISCOVERY_CHANGE_LIMIT")
    require(all(isinstance(change, dict) and set(change) <= {
        "chinese", "old_english", "new_english", "category", "reason", "change_kind"
    } for change in changes), "INVALID_DISCOVERY_CHANGE_FIELDS")
    require(through_chapter * len(changes) <= MAX_DISCOVERY_ROWS, "DISCOVERY_ROW_LIMIT")
    rules = normalized_rules(changes)
    require(len(rules) == len(changes), "DUPLICATE_DISCOVERY_CHANGE")
    completed = module.progress["completed"]
    last = module.progress["last_completed"]
    require(type(last) is int and through_chapter <= last <= MAX_DISCOVERY_CHAPTERS
            and all(type(n) is int for n in completed)
            and completed == list(range(1, last + 1)), "NONSEQUENTIAL_COMMITTED_INVENTORY")
    numbers = list(range(1, through_chapter + 1))
    require(set(completed) <= module.chapter_map.keys(), "MISSING_SOURCE")
    inventory = source_inventory({n: module.chapter_map[n] for n in completed})
    require({p.relative_to(module.OUTPUT_DIR).as_posix() for p in plain(module.OUTPUT_DIR).rglob("*.md")} ==
            {f"{n:04d}.md" for n in completed}, "COMMITTED_FILE_INVENTORY_DRIFT")

    # Bind all evidence read by the owners and recheck after computation. No
    # directory is created, no stale cache or report is trusted, no prose leaks.
    def snapshot():
        paths = set(state_paths(module).values()) | {module.CONTINUITY_FILE}
        for folder, pattern in ((module.OUTPUT_DIR, "*.md"), (module.AUDIT_DIR, "*.json"),
                                (module.RETROACTIVE_DIR / "transactions", "*.json"),
                                (module.BACKUP_DIR, "retroactive_*/chapters_before/*.md")):
            paths.update(p for p in plain(folder).glob(pattern) if p.is_file())
        require(len(paths) <= MAX_DISCOVERY_ROWS, "DISCOVERY_EVIDENCE_FILE_LIMIT")
        return {str(p): digest(plain(p).read_bytes()) for p in paths}

    output_root = plain(module.OUTPUT_DIR).resolve().parent
    require(plain(module.AUDIT_DIR).resolve().parent == output_root
            and plain(module.RETROACTIVE_DIR).resolve().parent == output_root
            and plain(module.BACKUP_DIR).resolve().parent == output_root.parent
            and all(plain(p).resolve().is_relative_to(output_root.parent)
                    for p in state_paths(module).values()), "FOREIGN_DISCOVERY_ROOT")
    witness = snapshot()
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        require(read_object(state_paths(module)[key]) == getattr(module, name), "DISCOVERY_STATE_DRIFT")
    supplied = expected_intent_sha256 is not None
    require(supplied or (audit_start is None and audit_end is None), "INCOMPLETE_DISCOVERY_PROVENANCE")
    intent, proof = None, None
    if supplied:
        require(type(audit_start) is int and type(audit_end) is int
                and 1 <= audit_start <= audit_end and through_chapter <= audit_end
                and isinstance(expected_intent_sha256, str) and len(expected_intent_sha256) == 64
                and all(c in "0123456789abcdef" for c in expected_intent_sha256),
                "INVALID_DISCOVERY_PROVENANCE")
        intent, proof = _discovery_intent(module, audit_start, audit_end, expected_intent_sha256)
    baseline = intent["baseline"]["glossary"] if intent else module.glossary
    staged = intent["post"]["glossary"] if intent else module.glossary
    policy = ownership_policy(baseline, staged)
    categories = sorted(module.ACTIVE_PROFILE.retroactive_safe_categories)
    accepted = normalized_rules(intent["record"]["changes"]["retroactive_replacements"]) if intent else []
    provenance = []
    for rule in rules:
        reason = "MISSING_ACCEPTED_CHANGE_PROVENANCE"
        original = next(item for item in changes if normalized_rules([item])[0] == rule)
        kind = original.get("change_kind", "canonical")
        require(kind in {"canonical", "alias_merge", "alias_split", "contextual"}, "INVALID_DISCOVERY_CHANGE_KIND")
        if intent:
            before = baseline["entries"].get(rule["chinese"], {})
            after = staged["entries"].get(rule["chinese"], {})
            if rule in accepted:
                reason = ""
            elif (before != after and rule["old_english"] == before.get("english", "")
                  and rule["new_english"] == after.get("english", "")
                  and rule["category"] == after.get("category", before.get("category", ""))
                  and after.get("status") == "contextual"):
                reason = ""  # Accepted contextualization still never permits a global edit.
            else:
                reason = "CHANGE_NOT_IN_VERIFIED_AUDIT"
        if kind == "alias_split":
            reason = "ALIAS_SPLIT_PROVENANCE_UNAVAILABLE"
        provenance.append(reason)

    rows = []
    for n in numbers:
        raw = plain(module.OUTPUT_DIR / f"{n:04d}.md").read_bytes()
        require(len(raw) <= 16 * 1024 * 1024, "DISCOVERY_CHAPTER_BYTE_LIMIT")
        source, text = module.chapter_map[n]["text"], raw.decode("utf-8")
        claims = _claim_longest_non_overlapping_occurrences(
            source, [(key, item["source_forms"]) for key, item in policy.items()])
        _, decisions = plan_chapter(source, raw, rules, policy, categories)
        findings = []
        for rule, decision, missing in zip(rules, decisions, provenance):
            anchors = claims.get(rule["chinese"], [])
            old_count = decision["target_candidate_count"]
            new_count = len(target_spans(text, rule["new_english"])) if rule["new_english"] else 0
            entry = policy.get(rule["chinese"], {})
            potential = bool(anchors or rule["chinese"] in source or old_count or new_count)
            reasons = []
            if missing:
                reasons.append(missing)
            if entry.get("contextual"):
                reasons.append("contextual_identity_requires_manual_review")
            if entry.get("alias_of"):
                reasons.append("alias_identity_requires_manual_review")
            if len(anchors) > 1:
                reasons.append("repeated_source_occurrences_require_independent_pairing")
            if old_count > 1 or new_count > 1:
                reasons.append("repeated_target_occurrences_require_independent_pairing")
            outcome = "deferred"
            if not missing and decision["status"] == "planned":
                outcome, reasons = "proven_eligible", ["OWNED_SINGLE_SPAN"]
            elif not missing and not potential:
                outcome, reasons = "unaffected", ["NO_SOURCE_OR_BOUNDED_TARGET_CANDIDATE"]
            elif not missing and not old_count and new_count:
                reverse = {**rule, "old_english": rule["new_english"], "new_english": rule["old_english"]}
                owned, reverse_decisions = plan_chapter(source, raw, [reverse], policy, categories)
                if owned:
                    outcome, reasons = "already_current", ["OWNED_APPROVED_RENDERING"]
                else:
                    reasons.append(reverse_decisions[0]["skip_reason"])
            else:
                reasons.append(decision.get("skip_reason", "OWNERSHIP_REQUIRES_REVIEW"))
            findings.append({"term_id": canonical_hash(rule["chinese"]), "change_id": canonical_hash(rule),
                "potential_impact": potential, "outcome": outcome,
                "reason_codes": sorted(set(reasons)), "source_owned_count": len(anchors),
                "source_claims_sha256": canonical_hash(anchors),
                "english_candidate_count": old_count, "approved_candidate_count": new_count})
        rows.append({"chapter": n, "unit_sha256": canonical_hash(module.chapter_map[n]),
                     "source_sha256": digest(source.encode("utf-8")), "english_sha256": digest(raw),
                     "decisions": findings})
    require(snapshot() == witness and source_inventory({n: module.chapter_map[n] for n in completed}) == inventory,
            "DISCOVERY_INPUT_DRIFT")
    report = {"revision": PREFLIGHT_REVISION, "policy_version": POLICY_VERSION,
        "phase": "DISCOVERY_NOT_COMMIT_AUTHORITY", "scope": "whole_committed_history",
        "novel_id_sha256": canonical_hash(module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"),
        "audit_boundary": through_chapter, "examined_chapters": len(rows),
        "source_inventory_sha256": canonical_hash(inventory), "evidence_sha256": canonical_hash(witness),
        "baseline_glossary_sha256": canonical_hash(baseline), "staged_glossary_sha256": canonical_hash(staged),
        "current_glossary_sha256": canonical_hash(module.glossary), "ownership_policy_sha256": canonical_hash(policy),
        "safe_categories_sha256": canonical_hash(categories), "selected_changes_sha256": canonical_hash(
            sorted(changes, key=canonical_hash)),
        "intent_sha256": intent["intent_sha256"] if intent else None,
        "transaction_id": proof["plan"]["transaction_id"] if proof else None,
        "chapters": rows, "corrected_chapters": 0, "provider_calls": 0}
    for outcome in ("proven_eligible", "deferred", "already_current", "unaffected"):
        report[outcome + "_chapters"] = sum(any(d["outcome"] == outcome for d in row["decisions"]) for row in rows)
    report["potential_impact_chapters"] = sum(any(d["potential_impact"] for d in row["decisions"]) for row in rows)
    report["report_sha256"] = canonical_hash(report)
    return report


def impact_report(plan, chapter_map, staged_glossary, chapter_bytes=None):
    """Read-only discovery from the exact plan the transaction must reprove.

    A potential occurrence is not edit authority. Cardinality/alias/contextual
    ambiguity stays deferred under the unchanged owned-single-span-v1 policy.
    """
    tx.validate_plan(plan, plan["request"], chapter_map)
    # Later source acquisition must not change an immutable earlier audit.
    # validate_plan proves every committed source hash; retain full unit identity
    # in the inventory so in-scope provenance drift still changes the proof.
    audited_sources = {n: chapter_map[n] for n in plan["request"]["committed_chapters"]}
    rules = plan["request"]["rules"]
    rows = []
    for item in plan["chapters"]:
        decisions = []
        for rule, decision in zip(rules, item["decisions"]):
            if decision["status"] == "planned":
                outcome = "proven_eligible"
            elif decision["target_candidate_count"]:
                outcome = "deferred"
            elif decision["source_owned_count"]:
                outcome = "needs_further_review"
                if chapter_bytes is not None:
                    raw = chapter_bytes[item["chapter"]]
                    require(digest(raw) == item["before_sha256"], "DISCOVERY_BEFORE_DRIFT")
                    reverse = {**rule, "old_english": rule["new_english"], "new_english": rule["old_english"]}
                    owned, _ = plan_chapter(chapter_map[item["chapter"]]["text"], raw, [reverse],
                                            plan["request"]["ownership_policy"], plan["request"]["safe_categories"])
                    if owned:
                        outcome = "already_current"
            else:
                outcome = "unchanged"
            decisions.append({"term_id": canonical_hash(rule["chinese"]),
                              "rule_id": canonical_hash(rule), "outcome": outcome,
                              "reason_code": "OWNED_APPROVED_RENDERING" if outcome == "already_current" else decision.get("skip_reason", "OWNED_SINGLE_SPAN"),
                              "source_owned_count": decision["source_owned_count"],
                              "english_candidate_count": decision["target_candidate_count"]})
        rows.append({"chapter": item["chapter"], "source_sha256": item["source_sha256"],
                     "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"],
                     "decisions": decisions})
    counts = {kind + "_chapters": sum(any(d["outcome"] == kind for d in row["decisions"])
                                     for row in rows)
              for kind in ("proven_eligible", "deferred", "needs_further_review", "unchanged", "already_current")}
    if not rules:
        counts["unchanged_chapters"] = len(rows)
    return {"revision": REVISION, "scope": "whole_committed_history",
            "audit_boundary": plan["request"]["through_chapter"],
            "transaction_id": plan["transaction_id"],
            "source_inventory_sha256": canonical_hash(source_inventory(audited_sources)),
            "glossary_revision_sha256": canonical_hash(staged_glossary),
            "examined_chapters": len(rows), **counts, "chapters": rows,
            "phase": "DISCOVERY_NOT_COMMIT_AUTHORITY", "provider_calls": 0}


def publication_input_snapshot(root):
    """Hashes only: inventory known local downstream stages before invalidation."""
    root = plain(root)
    folders = ("output/epub", "final/chapters", "final/normalized_chapters",
               "final/titled_chapters", "final/publication_chapters", "final/editorial_chapters",
               "final/reports", "final/epub")
    result = {}
    for name in folders:
        directory = plain(root / name)
        result[name] = {p.name: digest(plain(p).read_bytes()) for p in sorted(directory.glob("*"))
                        if p.is_file() and p.suffix in {".md", ".json", ".epub"}}
    manifest = plain(root / "final/manifest.json")
    result["final/manifest.json"] = digest(manifest.read_bytes()) if manifest.is_file() else None
    return result


def invalidation_path(module, intent):
    try:
        transaction = intent["transaction_plan"]["transaction_id"]
        require(isinstance(transaction, str) and len(transaction) == 64
                and all(c in "0123456789abcdef" for c in transaction), "INVALID_TRANSACTION_ID")
        root = plain(module.OUTPUT_DIR).resolve().parent
        require(plain(module.RETROACTIVE_DIR).resolve().parent == root
                and plain(module.AUDIT_DIR).resolve().parent == root, "FOREIGN_INVALIDATION_ROOT")
        directory = plain(root / "publication_freshness")
        path = plain(directory / (transaction + ".json"))
        require(path.resolve().parent == directory.resolve()
                and path.resolve().is_relative_to(root), "INVALIDATION_PATH_ESCAPE")
        return path
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_INVALIDATION_IDENTITY") from exc


def validate_invalidation_identity(module, intent, start, end):
    """Read-only bindings; a recomputed checksum alone grants no authority."""
    from production_periodic_audit_finalization import intent_path
    try:
        path = invalidation_path(module, intent)  # Check before any ID-derived lookup.
        plan = intent["transaction_plan"]
        request = plan["request"]
        novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
        require(type(start) is int and type(end) is int and 1 <= start <= end
                and type(intent["start"]) is int and type(intent["end"]) is int
                and (intent["start"], intent["end"]) == (start, end), "INVALID_AUDIT_BOUNDARY")
        require(request["novel_id"] == novel and request["audit_label"] == f"{start:04d}_{end:04d}"
                and type(request["through_chapter"]) is int and request["through_chapter"] == end,
                "INVALIDATION_OPERATION_MISMATCH")
        require(type(intent["record"]["start_chapter"]) is int and type(intent["record"]["end_chapter"]) is int
                and (intent["record"]["start_chapter"], intent["record"]["end_chapter"]) == (start, end),
                "INVALIDATION_RECORD_MISMATCH")
        report = intent["record"].get("historical_reconciliation")
        if report is not None:
            require(report["transaction_id"] == plan["transaction_id"]
                    and type(report["audit_boundary"]) is int and report["audit_boundary"] == end,
                    "INVALIDATION_REPORT_MISMATCH")
        tx.validate_plan(plan, request, module.chapter_map)
        require(audit_intent(module, intent_path(module, start, end)) == intent, "DURABLE_INTENT_MISMATCH")
        return path
    except (KeyError, TypeError) as exc:
        raise ProductionIntegrityError("AUT2: INVALID_INVALIDATION_IDENTITY") from exc


def invalidation_record(intent):
    plan = intent["transaction_plan"]
    return {"revision": REVISION, "novel_id": plan["request"]["novel_id"],
            "audit_boundary": intent["end"], "audit_intent_sha256": intent["intent_sha256"],
            "transaction_id": plan["transaction_id"],
            "glossary_revision_sha256": canonical_hash(intent["post"]["glossary"]),
            "chapters": [{k: item[k] for k in ("chapter", "before_sha256", "after_sha256")}
                         for item in plan["chapters"]],
            "publication_inputs_before": intent.get("publication_inputs_before", {}),
            "impacted_chapters": [item["chapter"] for item in plan["chapters"] if item["edits"]],
            "dependent_stages": ["checkpoint_epub", "finalization", "normalization", "titles",
                                 "publication_chapters", "editorial", "publication_epub"],
            "status": "STALE" if any(i["edits"] for i in plan["chapters"]) else "NOT_VERIFIED",
            "reason_code": "DOWNSTREAM_PROVENANCE_REQUIRES_REVALIDATION",
            "next_safe_action": "Verify revised production inventory and approved downstream plans; preserve existing outputs.",
            "automatic_rebuild": "DEFERRED_REQUIRES_DOWNSTREAM_POLICY", "provider_calls": 0}


def ensure_invalidation(module, intent):
    """Durable stale intent BEFORE historical edits, audit/progress markers last.

    A missing derived manifest can be reconstructed from immutable intent.
    Corrupt or contradictory evidence is never overwritten.
    Caller owns the existing novel writer lock and must finish all read-only
    finalization proofs before invoking this writer.
    """
    path = validate_invalidation_identity(module, intent, intent["start"], intent["end"])
    if "historical_reconciliation" not in intent["record"]:
        return  # Legacy intents retain their original recovery contract.
    expected = invalidation_record(intent)
    if path.exists():
        require(read_object(path) == expected, "FRESHNESS_MANIFEST_DRIFT")
        return
    # The finalizer has already reproved publication prefixes, paid evidence,
    # deterministic authorization and the transaction. Only now may this
    # conservative receipt become durable, before any transaction mutation.
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, expected)


def publication_revisions(project):
    """Reprove immutable invalidation receipts; receipts grant no write authority."""
    from types import SimpleNamespace
    module = SimpleNamespace(AUDIT_DIR=project.audit_dir)
    revisions = []
    for path in sorted(plain(project.audit_dir).glob("*.finalization.json")):
        intent = audit_intent(module, path)
        require(intent["transaction_plan"]["request"]["novel_id"] == ("legacy_single_novel" if project.is_legacy else project.config.id),
                "FOREIGN_NOVEL_INTENT")
        if "historical_reconciliation" not in intent["record"]:
            continue
        expected = invalidation_record(intent)
        receipt = plain(project.root / "output/publication_freshness" / (expected["transaction_id"] + ".json"))
        require(read_object(receipt) == expected, "FRESHNESS_MANIFEST_DRIFT")
        revisions.append(expected)
    known = {r["transaction_id"] + ".json" for r in revisions}
    directory = plain(project.root / "output/publication_freshness")
    require({p.name for p in directory.glob("*.json")} == known, "ORPHAN_FRESHNESS_MANIFEST")
    return revisions


def publication_freshness(project):
    """Recompute freshness; a source-bound generation must reprove its inputs."""
    revisions = publication_revisions(project)
    artifacts = []
    for directory in (project.epub_dir, project.root / "final/epub"):
        for path in sorted(plain(directory).glob("*.epub")):
            artifacts.append({"path": path.relative_to(project.root).as_posix(),
                              "sha256": digest(plain(path).read_bytes()), "status": "NOT_VERIFIED"})
    actual_paths = {item["path"] for item in artifacts}
    for revision in revisions:
        for folder in ("output/epub", "final/epub"):
            for name, sha in revision["publication_inputs_before"].get(folder, {}).items():
                relative = folder + "/" + name
                if relative not in actual_paths:
                    artifacts.append({"path": relative, "before_sha256": sha, "status": "MISSING"})
                    actual_paths.add(relative)
    dependencies = []
    for revision in revisions:
        for folder, files in revision["publication_inputs_before"].items():
            if folder == "final/manifest.json":
                files = {"manifest.json": files} if files else {}
                folder = "final"
            require(folder in {"final", "output/epub", "final/chapters", "final/normalized_chapters",
                               "final/titled_chapters", "final/publication_chapters", "final/editorial_chapters",
                               "final/reports", "final/epub"} and isinstance(files, dict), "INVALID_DOWNSTREAM_INVENTORY")
            for name, before_sha in files.items():
                require(isinstance(name, str) and Path(name).name == name and "/" not in name and "\\" not in name,
                        "INVALID_DOWNSTREAM_NAME")
                path = plain(project.root / folder / name)
                after_sha = digest(path.read_bytes()) if path.is_file() else None
                status = ("MISSING" if after_sha is None else "STALE" if revision["impacted_chapters"]
                          and after_sha == before_sha else "NOT_VERIFIED")
                dependencies.append({"path": folder + "/" + name, "before_sha256": before_sha,
                                     "sha256": after_sha, "status": status, "transaction_id": revision["transaction_id"]})
    from publication_regeneration import current_generation
    generation = current_generation(project)
    if "artifact" in generation:
        artifacts.append(generation["artifact"])
    return {"revision": REVISION, "status": generation["status"] if generation["status"] != "NOT_VERIFIED" else ("STALE" if revisions else "NOT_VERIFIED"),
            "transaction_ids": [r["transaction_id"] for r in revisions],
            "impacted_chapters": sorted({n for r in revisions for n in r["impacted_chapters"]}),
            "artifacts": artifacts, "rebuilt_epubs": int(generation["status"] == "CURRENT"),
            "generation": generation, "downstream_outputs": dependencies,
            "reason_code": "SOURCE_BOUND_GENERATION_VERIFIED" if generation["status"] == "CURRENT" else ("DOWNSTREAM_PROVENANCE_REQUIRES_REVALIDATION" if revisions else "NO_CURRENT_OUTPUT_PROOF"),
            "provider_calls": 0}


def require_publication_freshness(project):
    """No successful publication claim from an old self-consistent editorial copy."""
    revisions = publication_revisions(project)
    final_dir = getattr(project, "publication_final_dir", project.root / "final")
    proof = plain(final_dir / "reports/source_bound_provenance.json")
    if proof.exists():
        from publication_regeneration import verify_generation_corpus
        verify_generation_corpus(project, final_dir)
    else:
        require(not revisions, "PUBLICATION_STALE_REVALIDATE_DOWNSTREAM_PLANS")


def final_consistency_report(module):
    """Whole-book local consistency pass, distinct from acquisition's tail audit.

    Reprove original audit transactions and their byte chain, then examine every
    glossary identity in every committed source unit. Semantic re-review and
    expanded repeated-span eligibility are intentionally not claimed here.
    """
    from production_periodic_audit_finalization import (
        transaction_proof, prepare_finalization, file_sha, state_paths,
    )
    from types import SimpleNamespace
    numbers = module.progress["completed"]
    boundary = module.progress["last_completed"]
    require(numbers == list(range(1, boundary + 1)), "NONSEQUENTIAL_COMMITTED_INVENTORY")
    require(module.progress["last_audited_chapter"] == boundary, "UNFINISHED_WINDOW_AUDIT")
    inventory = source_inventory(module.chapter_map)
    require(set(numbers) <= set(module.chapter_map), "MISSING_SOURCE")
    files = {p.name for p in plain(module.OUTPUT_DIR).glob("*.md")}
    require(files == {f"{n:04d}.md" for n in numbers}, "COMMITTED_FILE_INVENTORY_DRIFT")
    current = {n: plain(module.OUTPUT_DIR / f"{n:04d}.md").read_bytes() for n in numbers}
    # Later BEFORE backups witness unchanged bytes in earlier audit plans.
    blobs = {digest(raw): raw for raw in current.values()}
    intents = []
    for path in sorted(plain(module.AUDIT_DIR).glob("*.finalization.json")):
        intent = audit_intent(module, path)
        require(intent["end"] <= boundary, "UNFINISHED_AUDIT_INTENT")
        for item in intent["transaction_plan"]["chapters"]:
            if item["edits"]:
                backup = plain(module.BACKUP_DIR / f"retroactive_{intent['transaction_plan']['transaction_id']}" /
                               "chapters_before" / f"{item['chapter']:04d}.md")
                require(backup.is_file() and digest(backup.read_bytes()) == item["before_sha256"],
                        "MISSING_OR_CORRUPT_BEFORE_BACKUP")
                blobs[item["before_sha256"]] = backup.read_bytes()
        intents.append(intent)
    latest, history_rules, previous_end, evidence = {}, [], 0, []
    for intent in intents:
        require(intent["start"] == previous_end + 1, "MISSING_OR_COMPETING_AUDIT_HISTORY")
        previous_end = intent["end"]
        detached = SimpleNamespace(**vars(module))
        for key, name in (("glossary", "glossary"), ("progress", "progress"),
                          ("issues", "issues_data"), ("history", "audit_history")):
            setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
        require(file_sha(module.periodic_audit_checkpoint_path(intent["start"], intent["end"])) == intent["checkpoint_sha256"],
                "AUDIT_CHECKPOINT_DRIFT")
        if detached.progress.get("provider") == "command_code_provider" and intent["chunking"].get("candidate_count", 0):
            from goat_periodic_audit_finalize_plan import checkpoint_proof
            paid = checkpoint_proof(detached, intent["start"], intent["end"])
            require(paid["audit_result"] == intent["audit_result"] and paid["audit_stats"] == intent["audit_stats"],
                    "PAID_AUDIT_EVIDENCE_DRIFT")
        checkpoint = module.BACKUP_DIR / f"checkpoint_{intent['end']:04d}"
        for key, path in state_paths(module).items():
            require(read_object(checkpoint / path.name) == intent["post"][key], "AUDIT_CHECKPOINT_BACKUP_DRIFT")
        require(file_sha(checkpoint / module.CONTINUITY_FILE.name) == intent["continuity_sha256"],
                "AUDIT_CONTINUITY_BACKUP_DRIFT")
        plan = intent["transaction_plan"]
        after = {}
        for item in plan["chapters"]:
            raw = blobs.get(item["before_sha256"])
            require(raw is not None, "HISTORICAL_BEFORE_WITNESS_MISSING")
            require(item["chapter"] not in latest or latest[item["chapter"]] == item["before_sha256"],
                    "DISCONNECTED_HISTORICAL_BYTE_CHAIN")
            after[item["chapter"]] = tx.transform_bytes(raw, item["edits"])
            latest[item["chapter"]] = item["after_sha256"]
        proof = transaction_proof(detached, intent["start"], intent["end"], intent["baseline"]["glossary"],
                                  intent["post"]["glossary"], intent["record"]["changes"], chapter_bytes=after)
        require(proof["state"] and proof["state"]["state"] == "FINALIZED", "TRANSACTION_NOT_FINALIZED")
        prepared = prepare_finalization(detached, intent["start"], intent["end"], intent["audit_result"],
            intent["audit_stats"], intent["chunking"], timestamp=intent["record"]["timestamp_unix"], chapter_bytes=after)
        for value in (prepared["record"], prepared["post"]["history"]["audits"][-1]):
            value["retroactive"] = intent["record"]["retroactive"]
        require(all(prepared[k] == intent[k] for k in ("post", "record", "transaction_plan", "operation_id")),
                "AUDIT_AUTHORIZATION_DRIFT")
        require(read_object(module.AUDIT_DIR / f"audit_{intent['start']:04d}_{intent['end']:04d}.json") == intent["record"],
                "OUTER_AUDIT_RECORD_DRIFT")
        require(module.audit_history["audits"][:len(intent["post"]["history"]["audits"])] == intent["post"]["history"]["audits"],
                "OUTER_AUDIT_HISTORY_DRIFT")
        history_rules.extend(plan["request"]["rules"])
        evidence.append({"transaction_id": plan["transaction_id"], "intent_sha256": intent["intent_sha256"]})
    require(intents and previous_end == boundary, "MISSING_FINAL_AUDIT_INTENT")
    require(all(digest(current[n]) == sha for n, sha in latest.items()), "CURRENT_PROSE_OUTSIDE_AUDIT_CHAIN")
    for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
        require(getattr(module, name) == intents[-1]["post"][key], "FINAL_STATE_OUTSIDE_AUDIT_CHAIN")
    entries = module.glossary["entries"]
    rows = []
    from noveltranslator_core import _claim_longest_non_overlapping_occurrences
    policy = ownership_policy(module.glossary)
    for n, raw in current.items():
        source = module.chapter_map[n]["text"]
        text = raw.decode("utf-8")
        decisions = []
        # Claims use the exact same longest-source machinery as the span owner.
        claims = _claim_longest_non_overlapping_occurrences(source,
            [(key, item["source_forms"]) for key, item in policy.items()])
        for key, anchors in sorted(claims.items()):
            entry = entries[key]
            targets = strict_term_valid_targets(entry)
            old = {r["old_english"] for r in history_rules if r["chinese"] == key
                   and r["old_english"] not in targets}
            old_count = sum(len(target_spans(text, value)) for value in old)
            present = sum(len(target_spans(text, value)) for value in set(targets) if value)
            canonical = entry.get("english", "")
            owned = []
            if canonical and present and not old_count:
                probe = {"chinese": key, "old_english": canonical, "new_english": "Ownership Probe",
                         "category": entry.get("category", ""), "reason": "local ownership check only"}
                owned, _ = plan_chapter(source, raw, normalized_rules([probe]), policy,
                                        module.ACTIVE_PROFILE.retroactive_safe_categories)
            reason = ("HISTORICAL_RENDERING_REQUIRES_OWNERSHIP_REVIEW" if old_count else
                      "APPROVED_RENDERING_NOT_FOUND" if not present else
                      "APPROVED_RENDERING_OWNERSHIP_UNPROVEN" if not owned else
                      "OWNED_APPROVED_RENDERING_PRESENT")
            decisions.append({"term_id": canonical_hash(key), "source_owned_count": len(anchors),
                              "old_candidate_count": old_count, "approved_candidate_count": present,
                              "outcome": "deferred" if old_count or not owned else "already_current",
                              "reason_code": reason})
        for key in sorted((set(entries) | {r["chinese"] for r in history_rules}) - claims.keys()):
            prior = [r for r in history_rules if r["chinese"] == key]
            if key in source or any(target_spans(text, r["old_english"]) for r in prior):
                decisions.append({"term_id": canonical_hash(key), "outcome": "deferred",
                                  "reason_code": "REMOVED_IDENTITY_REQUIRES_REVIEW" if key not in entries else
                                                 "HISTORICAL_SOURCE_OWNERSHIP_UNPROVEN"})
        rows.append({"chapter": n, "english_sha256": digest(raw),
                     "source_sha256": digest(source.encode("utf-8")), "decisions": decisions})
    deferred = sum(any(d["outcome"] == "deferred" for d in row["decisions"]) for row in rows)
    return {"revision": REVISION, "scope": "final_whole_committed_history",
            "audit_boundary": boundary, "novel_id": (module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"),
            "glossary_revision_sha256": canonical_hash(module.glossary),
            "source_inventory_sha256": canonical_hash(inventory),
            "examined_chapters": len(rows), "deferred_chapters": deferred,
            "corrected_chapters": 0,
            "historically_corrected_chapters": len({i["chapter"] for intent in intents
                                                     for i in intent["transaction_plan"]["chapters"] if i["edits"]}),
            "already_current_chapters": sum(bool(row["decisions"]) and all(d["outcome"] == "already_current"
                                                for d in row["decisions"]) for row in rows),
            "unchanged_chapters": sum(not row["decisions"] for row in rows),
            "transactions": evidence, "chapters": rows,
            "status": "CHECKED_WITH_DEFERRALS" if deferred else "CHECKED",
            "semantic_rereview": "NOT_PERFORMED", "publication_status": "NOT_VERIFIED",
            "provider_calls": 0}


def save_final_consistency(module):
    """Immutable derived report; replay produces zero additional writes."""
    report = final_consistency_report(module)
    directory = plain(module.AUDIT_DIR / "whole_history")
    path = plain(directory / (canonical_hash(report) + ".json"))
    if path.exists():
        require(read_object(path) == report, "FINAL_REPORT_DRIFT")
    else:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, report)
    return report
