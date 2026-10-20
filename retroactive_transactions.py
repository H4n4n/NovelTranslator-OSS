"""Local plan -> durable intent -> atomic apply -> reconcile -> finalize.

The operation slot locates the original plan after chapter bytes change. The
transaction ID additionally binds original BEFORE hashes. Never derive a new
plan from the current AFTER bytes on restart. No paid lifecycle is owned here.
"""

import json
from pathlib import Path

from noveltranslator_core import atomic_write_bytes, atomic_write_json
from production_failure_classification import ProductionIntegrityError, LocalPersistenceError
from production_run_lock import production_run_lock
from retroactive_span_planning import (
    POLICY_VERSION, SCHEMA_VERSION, canonical_hash, digest, normalized_rules,
    ownership_policy, plan_chapter, transform_bytes,
)


class RetroactiveBlocked(ProductionIntegrityError):
    """Historical bytes or immutable transaction inputs cannot be proven."""


def _read_json(path):
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, OSError) as exc:
        raise RetroactiveBlocked(f"unreadable retroactive evidence: {path}") from exc
    if not isinstance(value, dict):
        raise RetroactiveBlocked(f"retroactive evidence must be an object: {path}")
    return value


def _plain_path(path):
    if path.is_symlink():
        raise RetroactiveBlocked(f"symlink retroactive path: {path}")


def verify_audit_recovery_baseline(project_root, baseline, novel_id, audit_label, through):
    """Read-only proof before experiment recovery restores associated state.

    New span transactions keep their exact BEFORE/AFTER prose and evidence;
    the outer adapter must not delete them and manufacture a replacement plan.
    Legacy snapshots without such evidence keep their established recovery path.
    """
    from chapter_library import chapter_production_index
    root, baseline = Path(project_root), Path(baseline)
    directory = root / "output/retroactive_fixes/transactions"
    baseline_directory = baseline / "retroactive_fixes/transactions"
    slot = canonical_hash({"novel_id": novel_id, "audit_label": audit_label, "through_chapter": through})
    path = directory / f"{slot}.plan.json"
    _plain_path(directory)
    _plain_path(path)
    if not path.exists():
        # New evidence must never disappear through the legacy tree rollback.
        for evidence in directory.glob("*.json"):
            previous = baseline_directory / evidence.name
            if not previous.is_file() or previous.read_bytes() != evidence.read_bytes():
                raise RetroactiveBlocked("audit recovery has transaction evidence without the matching plan")
        return False
    plan = _read_json(path)
    request = plan.get("request", {})
    if (request.get("novel_id") != novel_id or request.get("audit_label") != audit_label or
            request.get("through_chapter") != through):
        raise RetroactiveBlocked("audit recovery retroactive operation identity mismatch")
    library = json.loads((root / "data/chapters.json").read_bytes())
    if not isinstance(library, list):
        raise RetroactiveBlocked("audit recovery source library must be a list")
    source_map = {chapter_production_index(item): item for item in library}
    if len(source_map) != len(library):
        raise RetroactiveBlocked("audit recovery source library has duplicate chapter identities")
    validate_plan(plan, request, source_map)
    expected_numbers = sorted(number for number in _read_json(baseline / "data/progress.json").get("completed", [])
                              if 1 <= number <= through)
    if request["committed_chapters"] != expected_numbers:
        raise RetroactiveBlocked("audit recovery committed inventory differs from transaction")
    baseline_chapters = baseline / "chapters"
    current_chapters = root / "output/chapters"
    originals = {}
    owned = {}
    for item in plan["chapters"]:
        number = item["chapter"]
        before = (baseline_chapters / f"{number:04d}.md").read_bytes()
        if digest(before) != item["before_sha256"]:
            raise RetroactiveBlocked(f"Chapter {number}: audit baseline/transaction BEFORE mismatch")
        originals[number] = before
        owned[f"{number:04d}.md"] = item
        backup = root / "backup" / f"retroactive_{plan['transaction_id']}" / "chapters_before" / f"{number:04d}.md"
        _plain_path(backup)
        if backup.exists() and digest(backup.read_bytes()) != item["before_sha256"]:
            raise RetroactiveBlocked(f"Chapter {number}: audit recovery backup hash mismatch")
        current = digest((current_chapters / f"{number:04d}.md").read_bytes())
        if item["edits"] and current == item["after_sha256"] and not backup.exists():
            raise RetroactiveBlocked(f"Chapter {number}: audit recovery AFTER lacks backup")
    # Validate, never replace/regenerate, the original immutable span plan.
    if build_plan(request, originals, source_map) != plan:
        raise RetroactiveBlocked("audit recovery deterministic span plan mismatch")
    baseline_files = {p.relative_to(baseline_chapters).as_posix(): p for p in baseline_chapters.rglob("*") if p.is_file()}
    current_files = {p.relative_to(current_chapters).as_posix(): p for p in current_chapters.rglob("*") if p.is_file()}
    if baseline_files.keys() != current_files.keys():
        raise RetroactiveBlocked("audit recovery chapter tree inventory mismatch")
    for name, current_path in current_files.items():
        _plain_path(current_path)
        current = digest(current_path.read_bytes())
        item = owned.get(name)
        accepted = {item["before_sha256"], item["after_sha256"]} if item else {digest(baseline_files[name].read_bytes())}
        if current not in accepted:
            raise RetroactiveBlocked(f"audit recovery {name}: current SHA {current} outside planned hashes {sorted(accepted)}")
    return True


def build_plan(request, chapters, chapter_map):
    """Pure immutable plan; chapters maps committed numbers to exact bytes."""
    items = []
    for number, before in sorted(chapters.items()):
        source = chapter_map.get(number, {}).get("text")
        if not isinstance(source, str):
            raise RetroactiveBlocked(f"missing source for committed Chapter {number}")
        edits, decisions = plan_chapter(source, before, request["rules"],
                                       request["ownership_policy"], request["safe_categories"])
        items.append({"chapter": number, "source_sha256": digest(source.encode("utf-8")),
                      "before_sha256": digest(before),
                      "after_sha256": digest(transform_bytes(before, edits)),
                      "edits": edits, "decisions": decisions})
    plan = {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
            "request": request, "chapters": items}
    plan["transaction_id"] = canonical_hash(plan)
    return plan


def validate_plan(plan, request, chapter_map):
    identity = {key: value for key, value in plan.items() if key != "transaction_id"}
    if (plan.get("schema_version") != SCHEMA_VERSION or
            plan.get("policy_version") != POLICY_VERSION or
            canonical_hash(identity) != plan.get("transaction_id") or
            plan.get("request") != request):
        raise RetroactiveBlocked("immutable retroactive plan/input identity mismatch")
    if [item["chapter"] for item in plan["chapters"]] != request["committed_chapters"]:
        raise RetroactiveBlocked("retroactive committed chapter inventory mismatch")
    for item in plan["chapters"]:
        source = chapter_map.get(item["chapter"], {}).get("text")
        if not isinstance(source, str) or digest(source.encode("utf-8")) != item["source_sha256"]:
            raise RetroactiveBlocked(f"Chapter {item['chapter']}: source hash mismatch")


def _summary(plan, outcomes, resumed):
    rules = plan["request"]["rules"]
    changed = [item["chapter"] for item in plan["chapters"] if item["edits"]]
    applied = {edit["rule_index"] for item in plan["chapters"] for edit in item["edits"]}
    details = []
    for index, rule in enumerate(rules):
        decisions = [{"chapter": item["chapter"], **item["decisions"][index]}
                     for item in plan["chapters"]]
        chapters = [item["chapter"] for item in plan["chapters"]
                    if any(edit["rule_index"] == index for edit in item["edits"])]
        details.append({**rule, "safe": bool(chapters), "chapters_changed": chapters,
                        "replacement_count": len(chapters), "decisions": decisions})
    rejected = sum(decision["status"] == "skipped" for item in plan["chapters"]
                   for decision in item["decisions"])
    return {"transaction_id": plan["transaction_id"], "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION, "state": "FINALIZED",
            "audit_label": plan["request"]["audit_label"],
            "through_chapter": plan["request"]["through_chapter"],
            "replacement_rules_considered": len(rules),
            "replacement_rules_applied": len(applied), "chapters_changed": changed,
            "replacement_count": sum(len(item["edits"]) for item in plan["chapters"]),
            "details": details, "planned_rules": rules, "chapter_results": outcomes,
            "transactions_resumed": int(resumed),
            "already_applied_edits_reconciled": sum(
                item["replacement_count"] for item in outcomes
                if item["status"] == "already_applied"),
            "rules_eligible_by_category": sum(rule["category"] in plan["request"]["safe_categories"]
                                               for rule in rules),
            "candidate_chapters": sum(any(d["source_owned_count"] and d["target_candidate_count"]
                                          for d in item["decisions"]) for item in plan["chapters"]),
            "retroactive_replacements_proposed": len(rules),
            "retroactive_replacements_eligible": sum(len(item["edits"]) for item in plan["chapters"]),
            "retroactive_replacements_applied": sum(len(item["edits"]) for item in plan["chapters"]),
            "retroactive_replacements_rejected": rejected,
            "historical_chapters_modified": changed, "api_request_count": 0}


def execute_retroactive_transaction(*, novel_id, audit_label, through_chapter,
                                   replacements, glossary, staged_glossary=None,
                                   safe_categories, committed_chapters, chapter_map,
                                   output_dir, retroactive_dir, backup_dir):
    """Run under a local cooperating-writer lock; contradictions stop before writes.

    All chapter/source/backup identities are preflighted, including no-edit
    chapters. APPLIED means all writes reconcile; FINALIZED means the local
    result is durable, not that the outer paid audit has installed its state.
    """
    directory = Path(retroactive_dir) / "transactions"
    _plain_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with production_run_lock(directory):
            return _execute(novel_id, str(audit_label), through_chapter, replacements,
                            glossary, staged_glossary, safe_categories, committed_chapters,
                            chapter_map, Path(output_dir), directory, Path(backup_dir))
    except OSError as exc:
        raise LocalPersistenceError("local retroactive persistence failed; preserve transaction evidence") from exc


def _execute(novel_id, audit_label, through, replacements, glossary, staged, categories,
             committed, chapter_map, output, directory, backup_dir):
    numbers = sorted(set(number for number in committed if 1 <= number <= through))
    request = {"novel_id": novel_id, "audit_label": audit_label, "through_chapter": through,
               "rules": normalized_rules(replacements), "ownership_policy": ownership_policy(glossary, staged),
               "safe_categories": sorted(categories), "committed_chapters": numbers}
    # This slot must NOT depend on mutable chapter bytes or glossary inputs:
    # changes under the same audit operation must find and block the old plan.
    slot = canonical_hash({"novel_id": novel_id, "audit_label": audit_label, "through_chapter": through})
    plan_path = directory / f"{slot}.plan.json"
    state_path = directory / f"{slot}.state.json"
    result_path = directory / f"{slot}.result.json"
    for path in (plan_path, state_path, result_path, output, backup_dir):
        _plain_path(path)
    resumed = plan_path.exists()
    if resumed:
        plan = _read_json(plan_path)
        validate_plan(plan, request, chapter_map)
    else:
        if state_path.exists() or result_path.exists():
            raise RetroactiveBlocked("retroactive state/result exists without immutable plan")
        chapters = {}
        for number in numbers:
            path = output / f"{number:04d}.md"
            _plain_path(path)
            if not path.is_file():
                raise RetroactiveBlocked(f"missing committed Chapter {number}: {path}")
            chapters[number] = path.read_bytes()
        plan = build_plan(request, chapters, chapter_map)
        atomic_write_json(plan_path, plan)  # Durable intent BEFORE any historical write.
    transaction = plan["transaction_id"]
    backup_root = backup_dir / f"retroactive_{transaction}" / "chapters_before"
    _plain_path(backup_root.parent)
    _plain_path(backup_root)
    if state_path.exists():
        state = _read_json(state_path)
        if state.get("transaction_id") != transaction or state.get("state") not in {
            "PLANNED", "APPLIED", "FINALIZED", "BLOCKED"
        }:
            raise RetroactiveBlocked("retroactive state identity mismatch")
        if state["state"] == "FINALIZED" and (
            not result_path.is_file() or digest(result_path.read_bytes()) != state.get("result_sha256")
        ):
            raise RetroactiveBlocked("final retroactive result hash mismatch")
    prior_result = _read_json(result_path) if result_path.exists() else None
    if prior_result is not None:
        # Preserve the first durable final evidence, even after later replays.
        # A crash may have saved the result before the FINALIZED marker.
        expected = _summary(plan, prior_result.get("chapter_results", []),
                            bool(prior_result.get("transactions_resumed")))
        if prior_result != expected or len(prior_result["chapter_results"]) != len(plan["chapters"]):
            raise RetroactiveBlocked("existing retroactive result/plan mismatch")
        for item, outcome in zip(plan["chapters"], prior_result["chapter_results"]):
            if (outcome.get("chapter") != item["chapter"] or
                    outcome.get("before_sha256") != item["before_sha256"] or
                    outcome.get("after_sha256") != item["after_sha256"] or
                    outcome.get("status") not in {"newly_applied", "already_applied", "skipped"}):
                raise RetroactiveBlocked("existing retroactive chapter result mismatch")
    outcomes = []

    def block(message, **evidence):
        atomic_write_json(state_path, {"transaction_id": transaction, "state": "BLOCKED",
                                      "reason": message, **evidence})
        raise RetroactiveBlocked(message)

    # Validate the entire batch before making additional historical mutations.
    for item in plan["chapters"]:
        number = item["chapter"]
        path = output / f"{number:04d}.md"
        backup = backup_root / path.name
        _plain_path(path)
        _plain_path(backup)
        current = digest(path.read_bytes()) if path.is_file() else "missing"
        before, after = item["before_sha256"], item["after_sha256"]
        if current not in {before, after}:
            block(f"Chapter {number}: current SHA {current} is neither BEFORE {before} nor AFTER {after}",
                  chapter=number, current_sha256=current, before_sha256=before, after_sha256=after)
        if backup.exists() and digest(backup.read_bytes()) != before:
            block(f"Chapter {number}: backup hash mismatch", chapter=number,
                  expected_backup_sha256=before, actual_backup_sha256=digest(backup.read_bytes()))
        if item["edits"] and current == after and not backup.exists():
            block(f"Chapter {number}: AFTER bytes lack proven BEFORE backup", chapter=number)
    atomic_write_json(state_path, {"transaction_id": transaction, "state": "PLANNED"})
    for item in plan["chapters"]:
        number = item["chapter"]
        path = output / f"{number:04d}.md"
        backup = backup_root / path.name
        current_bytes = path.read_bytes()
        current = digest(current_bytes)
        before, after = item["before_sha256"], item["after_sha256"]
        if current not in {before, after}:
            block(f"Chapter {number}: hash changed during apply", chapter=number,
                  current_sha256=current, before_sha256=before, after_sha256=after)
        status = "skipped"
        if item["edits"]:
            if current == before:
                transformed = transform_bytes(current_bytes, item["edits"])
                if digest(transformed) != after:
                    block(f"Chapter {number}: planned AFTER hash mismatch", chapter=number)
                backup_root.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    atomic_write_bytes(backup, current_bytes)
                if digest(backup.read_bytes()) != before:
                    block(f"Chapter {number}: backup hash mismatch during apply", chapter=number)
                # Recheck immediately before atomic replacement; the enclosing
                # production/local locks exclude supported cooperating writers.
                if digest(path.read_bytes()) != before:
                    block(f"Chapter {number}: changed before atomic write", chapter=number)
                atomic_write_bytes(path, transformed)
                status = "newly_applied"
            else:
                status = "already_applied"
            if digest(path.read_bytes()) != after or digest(backup.read_bytes()) != before:
                block(f"Chapter {number}: post-write reconciliation mismatch", chapter=number)
        outcomes.append({"chapter": number, "status": status,
                         "before_sha256": before, "after_sha256": after,
                         "backup_sha256": before if item["edits"] else None,
                         "backup": str(backup) if item["edits"] else None,
                         "replacement_count": len(item["edits"]),
                         "reconciliation": "expected_after" if item["edits"] else "unchanged"})
    # No success marker until EVERY chapter is proven in its expected end state.
    for item in plan["chapters"]:
        if digest((output / f"{item['chapter']:04d}.md").read_bytes()) != item["after_sha256"]:
            block(f"Chapter {item['chapter']}: final reconciliation mismatch")
    atomic_write_json(state_path, {"transaction_id": transaction, "state": "APPLIED",
                                  "chapter_results": outcomes})
    result = _summary(plan, outcomes, resumed)
    if prior_result is None:
        atomic_write_json(result_path, result)
    # Read-only replay diagnostics are also durable, without erasing first-run
    # evidence. Identical reconciliations reuse a deterministic evidence file.
    reconciliation_path = directory / f"{slot}.reconcile.{canonical_hash(result)}.json"
    _plain_path(reconciliation_path)
    if reconciliation_path.exists():
        if _read_json(reconciliation_path) != result:
            raise RetroactiveBlocked("retroactive reconciliation evidence mismatch")
    else:
        atomic_write_json(reconciliation_path, result)
    atomic_write_json(state_path, {"transaction_id": transaction, "state": "FINALIZED",
                                  "result_sha256": digest(result_path.read_bytes()),
                                  "reconciliation_sha256": digest(reconciliation_path.read_bytes())})
    return result
