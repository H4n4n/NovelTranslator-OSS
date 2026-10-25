"""Durable source-owned startup known-fix evidence; never infer legacy ownership.

Prepared evidence precedes the existing transaction. The separate immutable seal
follows FINALIZED receipts and glossary installation. Only the source-owning
known-fix boundary can prepare this witness; readers never repair or promote it.
"""
import copy
from functools import wraps
from pathlib import Path

import retroactive_transactions as tx
from retroactive_span_planning import canonical_hash, digest, ownership_policy, normalized_rules, plan_chapter
from noveltranslator_core import atomic_write_json
from production_failure_classification import ProductionIntegrityError

REVISION = "standalone-source-lineage-v1"
WITNESS_FIELDS = {"revision", "plan", "authorization", "inventory", "glossary", "staged_glossary",
    "before_chapters", "profile", "history", "after_audit", "prior_lineage", "ming_compatibility",
    "publication_inputs_before", "witness_sha256"}


def require(value, reason):
    if not value:
        raise ProductionIntegrityError("AUT2: HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN: " + reason)


def guarded(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProductionIntegrityError("AUT2: HISTORICAL_OWNERSHIP_UNAVAILABLE_UNPROVEN: INVALID_STANDALONE_EVIDENCE") from exc
    return checked


@guarded
def read_witness(path):
    from historical_reconciliation import read_object
    value = read_object(path)
    require(set(value) == WITNESS_FIELDS and value["revision"] == REVISION
            and type(value["ming_compatibility"]) is bool and type(value["after_audit"]) is int
            and value["after_audit"] >= 0 and isinstance(value["prior_lineage"], list)
            and isinstance(value["history"], list)
            and all(isinstance(value[key], dict) for key in
                    ("plan", "authorization", "inventory", "glossary", "staged_glossary", "before_chapters", "profile", "publication_inputs_before"))
            and value["witness_sha256"] == canonical_hash({k: v for k, v in value.items() if k != "witness_sha256"}),
            "WITNESS_SCHEMA_OR_HASH")
    return value


def bound_authorization(proof, witness_sha):
    proof = {k: v for k, v in proof.items() if k != "authorization_sha256"}
    proof["standalone_witness_sha256"] = witness_sha
    proof["authorization_sha256"] = canonical_hash(proof)
    return proof


@guarded
def bind_authorization(module, plan, proof):
    from historical_reconciliation import read_object
    path = directory(module) / (slot_for(plan) + ".prepared.json")
    if not path.exists():
        return proof
    value = read_witness(path)
    require(value.get("revision") == REVISION and value.get("plan") == plan
            and value.get("witness_sha256") == canonical_hash({k: v for k, v in value.items() if k != "witness_sha256"}),
            "ACTIVE_WITNESS_DRIFT")
    require(value["authorization"] == proof, "ACTIVE_AUTHORIZATION_DRIFT")
    return bound_authorization(proof, value["witness_sha256"])


def directory(module):
    from historical_reconciliation import plain
    return plain(module.RETROACTIVE_DIR / "standalone_lineage")


def slot_for(plan):
    return canonical_hash({k: plan["request"][k] for k in ("novel_id", "audit_label", "through_chapter")})


def profile_identity(module):
    from historical_reconciliation import plain, read_object
    from novel_project import repository_root, legacy_project
    novel_root = plain(Path(module.OUTPUT_DIR).absolute()).parent.parent
    config_path = plain(novel_root / "novel.json")
    profile_id = (read_object(config_path)["profile"] if config_path.is_file()
                  else legacy_project(novel_root).config.profile)
    root = plain(repository_root() / "profiles" / profile_id)
    return {p.relative_to(root).as_posix(): digest(plain(p).read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


@guarded
def witnesses(module, *, active_plan=None):
    """Strict canonical inventory. An active prepared witness is not a proof."""
    from historical_reconciliation import read_object, plain
    folder = directory(module)
    result, expected = [], set()
    for path in sorted(folder.glob("*.prepared.json")):
        value = read_witness(path)
        require(value.get("revision") == REVISION, "WITNESS_REVISION")
        require(value.get("witness_sha256") == canonical_hash({k: v for k, v in value.items()
                                                              if k != "witness_sha256"}), "WITNESS_HASH")
        slot = slot_for(value["plan"])
        require(path.name == slot + ".prepared.json", "WITNESS_SLOT")
        expected.add(path.name)
        seal = folder / (slot + ".complete.json")
        if seal.exists():
            expected.add(seal.name)
        if active_plan is not None and value["plan"] == active_plan:
            # Still validate a complete seal before any active replay writes.
            if seal.exists():
                verify_seal(module, value)
            continue
        require(seal.is_file(), "INCOMPLETE_STANDALONE_WITNESS")
        verify_seal(module, value)
        result.append(value)
    require({p.name for p in plain(folder).iterdir()} == expected if folder.exists() else not expected,
            "ORPHAN_OR_PARTIAL_WITNESS")
    return result


def evidence_files(module, lineage):
    """Physical evidence identity shared with the existing authorization owner."""
    from historical_reconciliation import plain
    root = Path(module.OUTPUT_DIR).absolute().parent.parent
    result = {}
    for path in directory(module).glob("*.prepared.json"):
        from historical_reconciliation import read_object
        value = read_witness(path)
        if value["witness_sha256"] in lineage:
            paths = [path, directory(module) / (slot_for(value["plan"]) + ".complete.json")]
            paths += transaction_files(module, value["plan"])
            for item in paths:
                result[item.absolute().relative_to(root).as_posix()] = digest(plain(item).read_bytes())
    return result


@guarded
def verify_transaction_inventory(module, plans):
    """A known result ID alone cannot qualify a foreign/duplicated saved plan."""
    from historical_reconciliation import read_object, plain
    known = {p["transaction_id"]: p for p in plans}
    folder = plain(module.RETROACTIVE_DIR / "transactions")
    for path in folder.rglob("*.plan.json"):
        saved = read_object(path)
        identity = saved.get("transaction_id")
        require(path == folder / (slot_for(saved) + ".plan.json")
                and isinstance(identity, str) and identity in known
                and canonical_hash(saved) == canonical_hash(known[identity]),
                "UNBOUND_OR_DUPLICATED_TRANSACTION")


def transaction_files(module, plan):
    from historical_reconciliation import plain
    paths = list(plain(module.RETROACTIVE_DIR / "transactions").glob(slot_for(plan) + ".*.json"))
    paths += list(plain(module.BACKUP_DIR / ("retroactive_" + plan["transaction_id"]) /
                        "chapters_before").glob("*.md"))
    return sorted(paths)


def transaction_receipts(module, value):
    """Reprove original plan, exact outcomes, receipt chronology and backups."""
    from historical_reconciliation import read_object, plain
    plan, authorization = value["plan"], value["authorization"]
    slot = slot_for(plan)
    folder = plain(module.RETROACTIVE_DIR / "transactions")
    require(read_object(folder / (slot + ".plan.json")) == plan, "PLAN_DRIFT")
    require(read_object(folder / (slot + ".authorization.json")) == bound_authorization(authorization, value["witness_sha256"]), "AUTHORIZATION_DRIFT")
    state = read_object(folder / (slot + ".state.json"))
    result_path = folder / (slot + ".result.json")
    require(set(state) == {"transaction_id", "state", "result_sha256", "reconciliation_sha256"}
            and state["state"] == "FINALIZED" and state["transaction_id"] == plan["transaction_id"]
            and state["result_sha256"] == digest(plain(result_path).read_bytes()), "RECEIPT_CHRONOLOGY")
    def summary(value):
        rows = value.get("chapter_results", [])
        require(len(rows) == len(plan["chapters"]) and value == tx._summary(plan, rows,
                bool(value.get("transactions_resumed"))), "RESULT_SCHEMA")
        for item, row in zip(plan["chapters"], rows):
            edited = bool(item["edits"])
            backup = module.BACKUP_DIR / ("retroactive_" + plan["transaction_id"]) / "chapters_before" / f"{item['chapter']:04d}.md"
            expected = dict(chapter=item["chapter"], status=row.get("status"), before_sha256=item["before_sha256"],
                after_sha256=item["after_sha256"], backup_sha256=item["before_sha256"] if edited else None,
                backup=str(backup.absolute()) if edited else None, replacement_count=len(item["edits"]),
                reconciliation="expected_after" if edited else "unchanged")
            actual = dict(row)
            if edited:
                require(isinstance(row.get("backup"), str), "RESULT_BACKUP_PATH")
                saved_backup = Path(row["backup"])
                if not saved_backup.is_absolute():
                    saved_backup = Path(module.OUTPUT_DIR).absolute().parent.parent / saved_backup
                require(saved_backup.absolute() == backup.absolute(), "RESULT_BACKUP_PATH")
                # Match the canonical novel-owned path across relative writer
                # and absolute reader contexts; never rewrite receipt bytes.
                actual["backup"] = expected["backup"]
            require(row.get("status") in ({"newly_applied", "already_applied"} if edited else {"skipped"})
                    and canonical_hash(actual) == canonical_hash(expected), "RESULT_OWNERSHIP")
            if edited:
                require(digest(plain(backup).read_bytes()) == item["before_sha256"], "BEFORE_BACKUP")
    summary(read_object(result_path))
    reconciliations = list(folder.glob(slot + ".reconcile.*.json"))
    require(reconciliations and sum(digest(plain(p).read_bytes()) == state["reconciliation_sha256"]
                                    for p in reconciliations) == 1, "DUPLICATE_OR_MISSING_RECONCILIATION")
    for path in reconciliations:
        row = read_object(path)
        summary(row)
        require(path.name == slot + ".reconcile." + canonical_hash(row) + ".json", "RECONCILIATION_HASH")
    expected = {slot + suffix for suffix in (".plan.json", ".authorization.json", ".state.json", ".result.json")}
    expected.update(p.name for p in reconciliations)
    require({p.name for p in folder.glob(slot + ".*")} == expected, "EXTRA_TRANSACTION_EVIDENCE")
    backup_folder = module.BACKUP_DIR / ("retroactive_" + plan["transaction_id"]) / "chapters_before"
    require({p.name for p in backup_folder.iterdir()} == {f"{i['chapter']:04d}.md" for i in plan["chapters"] if i["edits"]}
            if backup_folder.exists() else not any(i["edits"] for i in plan["chapters"]), "BACKUP_INVENTORY")
    root = Path(module.OUTPUT_DIR).absolute().parent.parent
    return {p.absolute().relative_to(root).as_posix(): digest(plain(p).read_bytes())
            for p in transaction_files(module, plan)}


def completion(module, value):
    return dict(revision=REVISION, witness_sha256=value["witness_sha256"],
                transaction_id=value["plan"]["transaction_id"],
                glossary_sha256=canonical_hash(value["staged_glossary"]),
                receipts=transaction_receipts(module, value))


def verify_seal(module, value):
    from historical_reconciliation import read_object
    path = directory(module) / (slot_for(value["plan"]) + ".complete.json")
    require(read_object(path) == completion(module, value), "COMPLETION_SEAL_DRIFT")


def add_blobs(module, values, blobs):
    from historical_reconciliation import plain
    for value in values:
        for item in value["plan"]["chapters"]:
            if item["edits"]:
                path = plain(module.BACKUP_DIR / ("retroactive_" + value["plan"]["transaction_id"]) /
                             "chapters_before" / f"{item['chapter']:04d}.md")
                raw = path.read_bytes()
                require(digest(raw) == item["before_sha256"], "BEFORE_BACKUP")
                blobs[digest(raw)] = raw


def prove_gap(module, values, after_audit, history, blobs, latest, policy, lineage, consumed, *, events=None):
    """Insert independently reproved standalone events between ordered audits."""
    from historical_reconciliation import _reserve_policy, read_only_inventory, writer_evidence_files, WRITER_AUTHORIZATION_REVISION
    pending = [w for w in values if w["after_audit"] == after_audit]
    while pending:
        heads = [w for w in pending if w["prior_lineage"] == lineage]
        require(len(heads) == 1, "FORKED_OR_REORDERED_WITNESS")
        value = heads[0]
        plan, request = value["plan"], value["plan"]["request"]
        require(request["audit_label"] == "startup" and value["history"] == history, "WITNESS_HISTORY_FORK")
        live = read_only_inventory(module, module.progress["completed"], request["through_chapter"])
        inventory = value["inventory"]
        require(inventory == {**live, "committed": request["committed_chapters"]}
                and inventory["committed"] == list(range(1, request["through_chapter"] + 1))
                and value["profile"] == profile_identity(module), "SOURCE_PROFILE_INVENTORY_DRIFT")
        require(request["ownership_policy"] == ownership_policy(value["glossary"], value["staged_glossary"]),
                "ORIGINAL_GLOSSARY_OWNERSHIP_DRIFT")
        require(set(value["before_chapters"]) == {f"{n:04d}.md" for n in inventory["committed"]}, "WITNESS_INVENTORY")
        before = {}
        for n in inventory["committed"]:
            sha = value["before_chapters"][f"{n:04d}.md"]
            require(sha in blobs and (n not in latest or latest[n] == sha), "DISCONNECTED_STANDALONE_BYTE_CHAIN")
            before[n] = blobs[sha]
        tx.validate_plan(plan, request, module.chapter_map)
        require(tx.build_plan(request, before, module.chapter_map) == plan, "ORIGINAL_SOURCE_SPANS_UNPROVEN")
        reserved = copy.deepcopy(request["ownership_policy"])
        _reserve_policy(reserved, policy)
        for item in plan["chapters"]:
            edits, _ = plan_chapter(module.chapter_map[item["chapter"]]["text"], before[item["chapter"]],
                                    request["rules"], reserved, request["safe_categories"])
            require(all(edit in edits for edit in item["edits"]), "HISTORICAL_OWNERSHIP_RESERVATION")
        proof = dict(revision=WRITER_AUTHORIZATION_REVISION, plan_sha256=canonical_hash(plan),
            accepted_change_sha256=canonical_hash({"standalone_exact_policy": request["ownership_policy"]}),
            historical_evidence_sha256=canonical_hash(writer_evidence_files(module, lineage)),
            inventory_sha256=canonical_hash({**inventory, "before_chapters": value["before_chapters"]}),
            reservations_sha256=canonical_hash(reserved), prior_intents_sha256=canonical_hash(lineage))
        proof["authorization_sha256"] = canonical_hash(proof)
        require(proof == value["authorization"], "ORIGINAL_WRITER_AUTHORIZATION_UNPROVEN")
        verify_seal(module, value)
        after_bytes = {}
        for item in plan["chapters"]:
            after = tx.transform_bytes(before[item["chapter"]], item["edits"])
            require(digest(after) == item["after_sha256"], "STANDALONE_AFTER_DRIFT")
            latest[item["chapter"]] = item["after_sha256"]
            blobs[digest(after)] = after
            after_bytes[item["chapter"]] = after
        if events is not None:
            events.append((plan, before, after_bytes))
        _reserve_policy(policy, request["ownership_policy"])
        lineage.append(value["witness_sha256"])
        consumed.add(value["witness_sha256"])
        pending.remove(value)


@guarded
def apply_known_fix(module, staged, rules):
    """Capture at the source-owning boundary, then use the unchanged span writer."""
    from historical_reconciliation import writer_inventory, writer_authorization, read_object, publication_input_snapshot
    through = module.progress["last_completed"]
    inventory = writer_inventory(module, module.progress["completed"], through)
    require(read_object(module.GLOSSARY_FILE) == module.glossary, "KNOWN_FIX_GLOSSARY_DRIFT")
    request = dict(novel_id=inventory["novel_id"], audit_label="startup", through_chapter=through,
        rules=normalized_rules(rules), ownership_policy=ownership_policy(module.glossary, staged),
        safe_categories=sorted(module.ACTIVE_PROFILE.retroactive_safe_categories), committed_chapters=inventory["committed"])
    before = {n: (module.OUTPUT_DIR / f"{n:04d}.md").read_bytes() for n in inventory["committed"]}
    plan = tx.build_plan(request, before, module.chapter_map)
    authorization = writer_authorization(module, plan)
    # No retrospective creation for an existing transaction, including a partial one.
    slot = slot_for(plan)
    require(not list((module.RETROACTIVE_DIR / "transactions").glob(slot + ".*")), "LEGACY_STANDALONE_HAS_NO_ORIGINAL_WITNESS")
    from historical_reconciliation import _historical_reservations
    selected = dict(start=module.progress.get("last_audited_chapter", 0) + 1, end=through,
                    baseline=dict(history=module.audit_history), transaction_plan=plan)
    _, lineage = _historical_reservations(module, selected, unpublished=True, writer_zero_send=True)
    value = dict(revision=REVISION, plan=plan, authorization=authorization, inventory=inventory,
        glossary=copy.deepcopy(module.glossary), staged_glossary=copy.deepcopy(staged),
        before_chapters={f"{n:04d}.md": digest(raw) for n, raw in before.items()},
        profile=profile_identity(module), history=copy.deepcopy(module.audit_history["audits"]),
        after_audit=module.progress.get("last_audited_chapter", 0), prior_lineage=lineage,
        ming_compatibility=bool(module.MING_COMPATIBILITY_MODE),
        publication_inputs_before=publication_input_snapshot(module.OUTPUT_DIR.parent.parent))
    value["witness_sha256"] = canonical_hash(value)
    folder = directory(module)
    require(not folder.exists() or not (folder / (slot + ".prepared.json")).exists(), "COMPETING_WITNESS")
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_json(folder / (slot + ".prepared.json"), value)
    _finish(module, value)


def _finish(module, value):
    from historical_reconciliation import read_object, writer_inventory, writer_authorization
    from types import SimpleNamespace
    plan, request = value["plan"], value["plan"]["request"]
    inventory = writer_inventory(module, module.progress["completed"], request["through_chapter"])
    require(inventory == value["inventory"] and profile_identity(module) == value["profile"]
            and module.audit_history["audits"] == value["history"]
            and module.progress.get("last_audited_chapter", 0) == value["after_audit"], "RECOVERY_BOUNDARY_DRIFT")
    require(plan["request"]["audit_label"] == "startup" and
            writer_authorization(module, plan) == bound_authorization(value["authorization"], value["witness_sha256"]), "RECOVERY_AUTHORIZATION_DRIFT")
    staged, _, _, rules = module._prepare_known_glossary_fixes(value["glossary"],
        profile=module.ACTIVE_PROFILE, ming_compatibility=value["ming_compatibility"])
    require(staged == value["staged_glossary"] and normalized_rules(rules) == request["rules"],
            "KNOWN_FIX_POLICY_DRIFT")
    current = read_object(module.GLOSSARY_FILE)
    require(current in (value["glossary"], value["staged_glossary"]), "RECOVERY_GLOSSARY_DRIFT")
    require(request["ownership_policy"] == ownership_policy(value["glossary"], value["staged_glossary"]), "RECOVERY_OWNERSHIP_DRIFT")
    if current == value["glossary"]:
        detached = SimpleNamespace(**vars(module))
        detached.glossary = copy.deepcopy(value["glossary"])
        # execute independently rederives the original authorization and all
        # BEFORE/AFTER prefixes under the authentic caller-held novel lease.
        tx.execute_retroactive_transaction(novel_id=request["novel_id"], audit_label="startup",
            through_chapter=request["through_chapter"], replacements=request["rules"],
            glossary=value["glossary"], staged_glossary=value["staged_glossary"],
            safe_categories=request["safe_categories"], committed_chapters=request["committed_chapters"],
            chapter_map=module.chapter_map, output_dir=module.OUTPUT_DIR,
            retroactive_dir=module.RETROACTIVE_DIR, backup_dir=module.BACKUP_DIR, writer_context=detached)
    # Even an installed glossary cannot turn an incomplete transaction into proof.
    seal = completion(module, value)
    require(all(digest((module.OUTPUT_DIR / f"{i['chapter']:04d}.md").read_bytes()) == i["after_sha256"]
                for i in plan["chapters"]), "RECOVERY_AFTER_DRIFT")
    if current != value["staged_glossary"]:
        module.save_json(module.GLOSSARY_FILE, value["staged_glossary"])
    module.glossary.clear()
    module.glossary.update(copy.deepcopy(value["staged_glossary"]))
    path = directory(module) / (slot_for(plan) + ".complete.json")
    require(not path.exists(), "DUPLICATE_COMPLETION")
    atomic_write_json(path, seal)


@guarded
def recover_known_fix(module):
    """Only the original owner may finish its exact prepared transaction tail."""
    from historical_reconciliation import read_object, writer_inventory
    # Validate the entire inventory before selecting a recovery, including orphans.
    folder = directory(module)
    paths = sorted(folder.glob("*.prepared.json"))
    incomplete = []
    for path in paths:
        value = read_witness(path)
        require(value.get("revision") == REVISION and value.get("witness_sha256") == canonical_hash(
            {k: v for k, v in value.items() if k != "witness_sha256"}) and path.name == slot_for(value["plan"]) + ".prepared.json",
            "RECOVERY_WITNESS_DRIFT")
        if not (folder / (slot_for(value["plan"]) + ".complete.json")).exists():
            incomplete.append(value)
    require(len(incomplete) <= 1, "COMPETING_INCOMPLETE_WITNESSES")
    if incomplete:
        value = incomplete[0]
        writer_inventory(module, module.progress["completed"], value["plan"]["request"]["through_chapter"])
        witnesses(module, active_plan=value["plan"])
        _finish(module, value)
    else:
        witnesses(module)


@guarded
def verified_events(module, *, pending_window=None):
    """Read-only source-qualified chain for paid disposition/freshness readers."""
    from historical_reconciliation import _historical_reservations, read_only_inventory, read_object
    from production_periodic_audit_finalization import load_intent
    numbers = module.progress["completed"]
    through = module.progress["last_completed"]
    inventory = read_only_inventory(module, numbers, through)
    selected = load_intent(module, *pending_window) if pending_window else None
    if pending_window:
        require(selected is not None, "PENDING_INTENT_MISSING")
    unpublished = selected is None
    if unpublished:
        request = dict(novel_id=inventory["novel_id"], audit_label="lineage-read-only",
            through_chapter=through, committed_chapters=numbers, rules=[],
            ownership_policy=ownership_policy(module.glossary),
            safe_categories=sorted(module.ACTIVE_PROFILE.retroactive_safe_categories))
        plan = tx.build_plan(request, {n: (module.OUTPUT_DIR / f"{n:04d}.md").read_bytes() for n in numbers}, module.chapter_map)
        selected = dict(start=module.progress.get("last_audited_chapter", 0) + 1, end=through,
            baseline=dict(history=module.audit_history), transaction_plan=plan)
    events = []
    _historical_reservations(module, selected, unpublished=unpublished, writer_zero_send=True, _events=events)
    plans = [plan for plan, _, _ in events]
    if not unpublished:
        plans.append(selected["transaction_plan"])
    verify_transaction_inventory(module, plans)
    return events


@guarded
def publication_revisions(project):
    """The prepared witness itself conservatively invalidates derived outputs."""
    from types import SimpleNamespace
    root_module = SimpleNamespace(RETROACTIVE_DIR=project.root / "output/retroactive_fixes")
    if not directory(root_module).exists():
        from historical_reconciliation import read_object, plain
        for path in plain(project.root / "output/retroactive_fixes/transactions").glob("*.plan.json"):
            plan = read_object(path)
            require(plan["request"]["audit_label"] != "startup", "MISSING_STANDALONE_SOURCE_WITNESS")
        return []
    from goat_periodic_audit_finalize_plan import local_module
    module = local_module(project)
    if project.is_legacy:
        module.ACTIVE_NOVEL_CONFIG = None
    verified_events(module)
    return [dict(revision=REVISION, novel_id=value["inventory"]["novel_id"],
        transaction_id=value["plan"]["transaction_id"], audit_boundary=value["after_audit"],
        witness_sha256=value["witness_sha256"],
        glossary_revision_sha256=canonical_hash(value["staged_glossary"]),
        chapters=[{k: item[k] for k in ("chapter", "before_sha256", "after_sha256")} for item in value["plan"]["chapters"]],
        impacted_chapters=[i["chapter"] for i in value["plan"]["chapters"] if i["edits"]],
        publication_inputs_before=value["publication_inputs_before"], status="STALE",
        provider_calls=0) for value in witnesses(module)]
