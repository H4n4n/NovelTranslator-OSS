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


def read_object(path):
    try:
        # Decode explicitly: json.loads(bytes) also accepts UTF-16/32. Existing
        # UTF-8/BOM, whitespace, CRLF and pretty printing remain valid.
        value = json.loads(plain(path).read_bytes().decode("utf-8-sig"),
                           object_pairs_hook=_unique_object, parse_float=_finite_float,
                           parse_constant=_invalid_constant)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OSError, RecursionError) as exc:
        raise ProductionIntegrityError("AUT2: UNREADABLE_EVIDENCE") from exc
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


def publication_freshness(project):
    """Recompute from audit intents, never accept a cached CURRENT flag.

    This first slice deliberately cannot certify or rebuild downstream corpora.
    Missing/renamed/deleted artifacts are explicit. A receipt never grants write
    authority, even if its own hash is internally consistent.
    """
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
    return {"revision": REVISION, "status": "STALE" if revisions else "NOT_VERIFIED",
            "transaction_ids": [r["transaction_id"] for r in revisions],
            "impacted_chapters": sorted({n for r in revisions for n in r["impacted_chapters"]}),
            "artifacts": artifacts, "rebuilt_epubs": 0,
            "reason_code": "DOWNSTREAM_PROVENANCE_REQUIRES_REVALIDATION" if revisions else "NO_CURRENT_OUTPUT_PROOF",
            "provider_calls": 0}


def require_publication_freshness(project):
    """No successful publication claim from an old self-consistent editorial copy."""
    report = publication_freshness(project)
    require(not report["transaction_ids"], "PUBLICATION_STALE_REVALIDATE_DOWNSTREAM_PLANS")


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
