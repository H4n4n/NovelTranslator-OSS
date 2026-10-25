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
    from historical_reconciliation import read_object
    try:
        return read_object(path)
    except ProductionIntegrityError as exc:
        raise RetroactiveBlocked(f"unreadable retroactive evidence: {path}") from exc


def _plain_path(path):
    if path.is_symlink():
        raise RetroactiveBlocked(f"symlink retroactive path: {path}")


def verify_audit_recovery_baseline(project_root, baseline, novel_id, audit_label, through, *, baseline_authorization=None):
    """Read-only proof before experiment recovery restores associated state.

    New span transactions keep their exact BEFORE/AFTER prose and evidence;
    the outer adapter must not delete them and manufacture a replacement plan.
    Legacy snapshots without such evidence keep their established recovery path.
    """
    from chapter_library import chapter_production_index
    root, baseline = Path(project_root), Path(baseline)
    from historical_reconciliation import writer_module, writer_inventory, writer_authorization, read_object
    context = writer_module(root / "output/chapters", novel_id)
    inventory = writer_inventory(context, read_object(context.PROGRESS_FILE).get("completed"), through)
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
        # Only an exact original pre-audit snapshot with its independently
        # rederived zero-edit authorization can restore legacy publication bytes.
        # Old snapshots without that binding retain the unchanged-corpus limit.
        from historical_reconciliation import plain
        current = {p.relative_to(root / "output/chapters").as_posix(): plain(p).read_bytes()
                   for p in (root / "output/chapters").rglob("*.md")}
        before = {p.relative_to(baseline / "chapters").as_posix(): plain(p).read_bytes()
                  for p in plain(baseline / "chapters").rglob("*.md")}
        recovery_before = None
        if baseline_authorization is not None:
            from types import SimpleNamespace
            context = SimpleNamespace(**vars(context))
            for attr, name in (("glossary", "glossary.json"), ("progress", "progress.json"),
                               ("issues_data", "issues.json"), ("audit_history", "audit_history.json")):
                setattr(context, attr, read_object(baseline / "data" / name))
            expected = {f"{n:04d}.md" for n in inventory["committed"]}
            if set(before) != expected or set(current) != expected:
                raise RetroactiveBlocked("COMMITTED_INVENTORY_DRIFT")
            recovery_before = {n: before[f"{n:04d}.md"] for n in inventory["committed"]}
        elif current != before:
            raise RetroactiveBlocked("AUTHORITATIVE_WRITER_CONTEXT_UNAVAILABLE: legacy prose rollback")
        probe = dict(novel_id=novel_id, audit_label=audit_label, through_chapter=through, rules=[],
            ownership_policy=ownership_policy(context.glossary),
            safe_categories=sorted(context.ACTIVE_PROFILE.retroactive_safe_categories),
            committed_chapters=list(range(1, through + 1)))
        original = recovery_before or {n: current[f"{n:04d}.md"] for n in probe["committed_chapters"]}
        authorization = writer_authorization(context, build_plan(probe,
            {n: original[n] for n in probe["committed_chapters"]}, context.chapter_map),
            recovery_before=recovery_before)
        if baseline_authorization is not None and authorization != baseline_authorization:
            raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT: recovery baseline")
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
    # Same fresh ownership veto before any associated-state restoration.
    authorization = writer_authorization(context, plan)
    auth_path = directory / f"{slot}.authorization.json"
    if auth_path.exists() and read_object(auth_path) != authorization:
        raise RetroactiveBlocked("IMMUTABLE_PLAN_POLICY_CONFLICT: WRITER_AUTHORIZATION_DRIFT")
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
                                   output_dir, retroactive_dir, backup_dir, writer_context=None):
    """Run under a local cooperating-writer lock; contradictions stop before writes.

    All chapter/source/backup identities are preflighted, including no-edit
    chapters. APPLIED means all writes reconcile; FINALIZED means the local
    result is durable, not that the outer paid audit has installed its state.
    """
    from historical_reconciliation import writer_module, writer_inventory, writer_authorization, read_object, plain
    context = writer_context or writer_module(output_dir, novel_id)
    directory = Path(retroactive_dir) / "transactions"
    require_paths = ((output_dir, context.OUTPUT_DIR), (retroactive_dir, context.RETROACTIVE_DIR),
                     (backup_dir, context.BACKUP_DIR))
    if any(plain(Path(left).absolute()) != plain(Path(right).absolute()) for left, right in require_paths):
        raise RetroactiveBlocked("FOREIGN_WRITER_ROOT")
    # Raw caller inventory is checked BEFORE filtering, deduplication, directory
    # creation, lock rendezvous, authorization receipt, or transaction writes.
    writer_inventory(context, committed_chapters, through_chapter)
    if chapter_map != context.chapter_map:
        raise RetroactiveBlocked("SOURCE_INVENTORY_DRIFT")
    from production_periodic_audit_finalization import load_intent
    import re
    match = re.fullmatch(r"(\d{4,})_(\d{4,})", str(audit_label))
    intent = load_intent(context, *map(int, match.groups())) if match else None
    expected = intent["baseline"]["glossary"] if intent else read_object(context.GLOSSARY_FILE)
    if glossary != expected or (intent and staged_glossary != intent["post"]["glossary"]):
        raise RetroactiveBlocked("WRITER_GLOSSARY_DRIFT")
    numbers = list(range(1, through_chapter + 1))
    request = dict(novel_id=novel_id, audit_label=str(audit_label), through_chapter=through_chapter,
        rules=normalized_rules(replacements), ownership_policy=ownership_policy(glossary, staged_glossary),
        safe_categories=sorted(safe_categories), committed_chapters=numbers)
    slot = canonical_hash({k: request[k] for k in ("novel_id", "audit_label", "through_chapter")})
    path = plain(directory / f"{slot}.plan.json")
    if path.exists():
        planned = read_object(path)
        validate_plan(planned, request, chapter_map)
    else:
        planned = build_plan(request, {n: plain(Path(output_dir) / f"{n:04d}.md").read_bytes() for n in numbers}, chapter_map)
    try:
        authorization = writer_authorization(context, planned)
    except ProductionIntegrityError as exc:
        if path.exists() and "HISTORICAL_OWNERSHIP_RESERVATION" in str(exc):
            raise RetroactiveBlocked("IMMUTABLE_PLAN_POLICY_CONFLICT: HISTORICAL_OWNERSHIP_RESERVATION") from exc
        raise
    authorization_path = plain(directory / f"{slot}.authorization.json")
    if authorization_path.exists() and read_object(authorization_path) != authorization:
        raise RetroactiveBlocked("IMMUTABLE_PLAN_POLICY_CONFLICT: WRITER_AUTHORIZATION_DRIFT")
    _plain_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with production_run_lock(directory):
            if writer_authorization(context, planned) != authorization:
                raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT")
            return _execute(novel_id, str(audit_label), through_chapter, replacements,
                            glossary, staged_glossary, safe_categories, committed_chapters,
                            chapter_map, Path(output_dir), directory, Path(backup_dir),
                            context, planned, authorization)
    except OSError as exc:
        raise LocalPersistenceError("local retroactive persistence failed; preserve transaction evidence") from exc


def _execute(novel_id, audit_label, through, replacements, glossary, staged, categories,
             committed, chapter_map, output, directory, backup_dir, context=None, authorized_plan=None, authorization=None):
    from historical_reconciliation import writer_authorization, writer_inventory, plain
    if context is None or authorized_plan is None or authorization is None:
        raise RetroactiveBlocked("AUTHORITATIVE_WRITER_CONTEXT_UNAVAILABLE")
    writer_inventory(context, committed, through)
    if (plain(output.absolute()) != plain(Path(context.OUTPUT_DIR).absolute())
            or plain(directory.parent.absolute()) != plain(Path(context.RETROACTIVE_DIR).absolute())
            or plain(backup_dir.absolute()) != plain(Path(context.BACKUP_DIR).absolute())
            or chapter_map != context.chapter_map):
        raise RetroactiveBlocked("FOREIGN_WRITER_ROOT")
    if writer_authorization(context, authorized_plan) != authorization:
        raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT")
    numbers = list(range(1, through + 1))
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
        if plan != authorized_plan:
            raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT")
    if plan != authorized_plan:
        raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT")
    transaction = plan["transaction_id"]
    backup_root = backup_dir / f"retroactive_{transaction}" / "chapters_before"
    _plain_path(backup_root.parent)
    _plain_path(backup_root)
    if state_path.exists():
        state = _read_json(state_path)
        if state.get("transaction_id") != transaction or state.get("state") not in {
            "PLANNED", "APPLIED", "FINALIZED"
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
    def verify_outcomes(rows):
        if not isinstance(rows, list) or len(rows) != len(plan["chapters"]):
            raise RetroactiveBlocked("existing retroactive result inventory mismatch")
        for item, row in zip(plan["chapters"], rows):
            if not isinstance(row, dict) or row.get("status") not in (
                    {"newly_applied", "already_applied"} if item["edits"] else {"skipped"}):
                raise RetroactiveBlocked("existing retroactive result status mismatch")
            expected = dict(chapter=item["chapter"], status=row["status"], before_sha256=item["before_sha256"],
                after_sha256=item["after_sha256"], backup_sha256=item["before_sha256"] if item["edits"] else None,
                backup=str((backup_root / f"{item['chapter']:04d}.md").absolute()) if item["edits"] else None,
                replacement_count=len(item["edits"]), reconciliation="expected_after" if item["edits"] else "unchanged")
            actual = dict(row)
            if item["edits"]:
                backup = Path(str(actual.get("backup", "")))
                if not backup.is_absolute():
                    backup = Path(context.OUTPUT_DIR).absolute().parent.parent / backup
                actual["backup"] = str(backup.absolute())
            if canonical_hash(actual) != canonical_hash(expected):
                raise RetroactiveBlocked("existing retroactive result/plan mismatch")
    state = _read_json(state_path) if state_path.exists() else None
    if prior_result is not None:
        if not state or state["state"] not in {"APPLIED", "FINALIZED"}:
            raise RetroactiveBlocked("result precedes APPLIED state")
        verify_outcomes(prior_result["chapter_results"])
    if state:
        fields = {"PLANNED": {"transaction_id", "state"},
                  "APPLIED": {"transaction_id", "state", "chapter_results"},
                  "FINALIZED": {"transaction_id", "state", "result_sha256", "reconciliation_sha256"}}
        if set(state) != fields[state["state"]]:
            raise RetroactiveBlocked("retroactive state schema mismatch")
        if state["state"] == "APPLIED":
            verify_outcomes(state["chapter_results"])
        if state["state"] == "FINALIZED":
            matches = [p for p in directory.glob(f"{slot}.reconcile.*.json")
                       if digest(p.read_bytes()) == state["reconciliation_sha256"]]
            if len(matches) != 1:
                raise RetroactiveBlocked("final retroactive reconciliation hash mismatch")
            reconciliation = _read_json(matches[0])
            verify_outcomes(reconciliation.get("chapter_results"))
            if (matches[0].name != f"{slot}.reconcile.{canonical_hash(reconciliation)}.json" or
                    canonical_hash(reconciliation) != canonical_hash(_summary(plan,
                        reconciliation["chapter_results"], bool(reconciliation.get("transactions_resumed"))))):
                raise RetroactiveBlocked("final retroactive reconciliation mismatch")
    outcomes = []

    def block(message, **evidence):
        # Contradictions must not overwrite even the old diagnostic state.
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
        if state and state["state"] in {"APPLIED", "FINALIZED"} and current != after:
            block(f"Chapter {number}: applied transaction is not AFTER")
        if current not in {before, after}:
            block(f"Chapter {number}: current SHA {current} is neither BEFORE {before} nor AFTER {after}",
                  chapter=number, current_sha256=current, before_sha256=before, after_sha256=after)
        if backup.exists() and digest(backup.read_bytes()) != before:
            block(f"Chapter {number}: backup hash mismatch", chapter=number,
                  expected_backup_sha256=before, actual_backup_sha256=digest(backup.read_bytes()))
        if item["edits"] and current == after and not backup.exists():
            block(f"Chapter {number}: AFTER bytes lack proven BEFORE backup", chapter=number)
    if state_path.exists() and state.get("state") == "FINALIZED":
        # Reprove at the new guard, preserve every finalized byte and mtime.
        outcomes = [dict(chapter=i["chapter"], status="already_applied" if i["edits"] else "skipped",
            before_sha256=i["before_sha256"], after_sha256=i["after_sha256"],
            backup_sha256=i["before_sha256"] if i["edits"] else None,
            backup=str(backup_root / f"{i['chapter']:04d}.md") if i["edits"] else None,
            replacement_count=len(i["edits"]), reconciliation="expected_after" if i["edits"] else "unchanged")
            for i in plan["chapters"]]
        return _summary(plan, outcomes, True)
    if writer_authorization(context, plan) != authorization:
        raise RetroactiveBlocked("WRITER_AUTHORIZATION_DRIFT")
    authorization_path = directory / f"{slot}.authorization.json"
    if not authorization_path.exists():
        atomic_write_json(authorization_path, authorization)
    if not resumed:
        atomic_write_json(plan_path, plan)  # Same original plan, no schema migration.
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
