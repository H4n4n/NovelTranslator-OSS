"""Read-only composition of immutable audit BEFORE/AFTER proofs.

No provider activation, cached acceptance, evidence repair or journal writes.
Hashes establish local consistency under cooperating single-writer assumptions;
they are not signatures against wholesale hostile evidence replacement.
"""
import copy
import io

from production_accounting import canonical_sha256
from production_periodic_audit_finalization import (
    require, read_json, file_sha, load_intent, transaction_proof, prepare_finalization,
    verify_publication, state_paths,
)
import retroactive_transactions as tx
from historical_reconciliation import proof_traversal, proof_input_scope


def decoded_text_sha(raw):
    # Exactly Path.read_text(encoding="utf-8"): universal newlines; UTF-8 BOM
    # remains U+FEFF. Byte and decoded-text digests are distinct domains.
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline=None) as stream:
        return tx.digest(stream.read().encode("utf-8"))


def _audits(module, *, _inputs=None):
    read = _inputs.read_json if _inputs is not None else read_json
    audits = []
    for path in sorted(module.AUDIT_DIR.glob("*.finalization.json")):
        value = read(path)
        start, end = value["start"], value["end"]
        require(type(start) is int and type(end) is int and 1 <= start <= end,
                "historical audit window invalid")
        require(path.name == f"audit_{start:04d}_{end:04d}.finalization.json",
                "historical audit intent path/provenance mismatch")
        intent = load_intent(module, start, end, _inputs=_inputs)
        require(intent["transaction_plan"]["request"]["novel_id"] == module.ACTIVE_NOVEL_CONFIG.id,
                "historical audit novel mismatch")
        audits.append(intent)
    return sorted(audits, key=lambda i: (i["start"], i["end"]))


def _module_before(module, intent):
    from types import SimpleNamespace
    detached = SimpleNamespace(**vars(module))
    for key, name in (("glossary", "glossary"), ("progress", "progress"),
                      ("issues", "issues_data"), ("history", "audit_history")):
        setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
    return detached


@proof_traversal
def _prove_audit(module, intent, blobs, *, pending=False, legacy_completion=False, _inputs=None):
    """Recompute authorization, paid evidence and publication independently."""
    file_sha, read_json = _inputs.sha, _inputs.read_json
    from goat_periodic_audit_finalize_plan import checkpoint_proof
    require(legacy_completion or intent.get("historical_proof", {}).get("schema_version") == 1,
            "legacy audit requires separate lineage review; no automatic migration")
    detached = _module_before(module, intent)
    start, end = intent["start"], intent["end"]
    require(file_sha(module.periodic_audit_checkpoint_path(start, end)) == intent["checkpoint_sha256"],
            "historical paid checkpoint drift")
    if intent["chunking"].get("candidate_count", 0):
        paid = checkpoint_proof(detached, start, end)
        require(paid["audit_result"] == intent["audit_result"] and paid["audit_stats"] == intent["audit_stats"],
                "historical paid response drift")
        saved_chunking, paid_chunking = copy.deepcopy(intent["chunking"]), copy.deepcopy(paid["chunking"])
        for value in (saved_chunking, paid_chunking):
            value.pop("resumed", None)
            value.pop("checkpoint_file", None)  # Exact checkpoint path/hash proved above.
        require(saved_chunking == paid_chunking, "historical paid chunking drift")
    plan = intent["transaction_plan"]
    before = {}
    after = {}
    for item in plan["chapters"]:
        number = item["chapter"]
        if item["edits"]:
            backup = module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" / f"{number:04d}.md"
            # A pending untouched BEFORE prefix may not yet have a backup.
            if pending and not backup.exists():
                raw = _inputs.read(module.OUTPUT_DIR / f"{number:04d}.md")
            else:
                raw = _inputs.read(backup)
            require(tx.digest(raw) == item["before_sha256"], "historical BEFORE backup mismatch")
        else:
            raw = blobs.get(item["before_sha256"])
            require(raw is not None, "historical unchanged chapter provenance missing")
        before[number] = raw
        after[number] = tx.transform_bytes(raw, item["edits"])
        require(tx.digest(after[number]) == item["after_sha256"], "historical approved AFTER spans mismatch")
    require(_inputs.build_plan(plan["request"], before, module.chapter_map) == plan,
            "historical deterministic ownership/span plan mismatch")
    snapshot = None if pending else after
    prepared = prepare_finalization(detached, start, end, intent["audit_result"], intent["audit_stats"],
        intent["chunking"], timestamp=intent["record"]["timestamp_unix"], chapter_bytes=snapshot, _inputs=_inputs)
    prepared["post"]["history"]["audits"][-1]["retroactive"] = intent["record"]["retroactive"]
    prepared["record"]["retroactive"] = intent["record"]["retroactive"]
    require(all(prepared[k] == intent[k] for k in ("post", "transaction_plan", "operation_id")),
            "historical audit authorization/accounting drift")
    require(prepared["record"] == intent["record"], "historical audit record authorization drift")
    proof = transaction_proof(detached, start, end, intent["baseline"]["glossary"],
        intent["post"]["glossary"], intent["record"]["changes"], chapter_bytes=snapshot, _inputs=_inputs)
    if pending:
        require(not verify_publication(module, intent, _inputs=_inputs), "pending audit already published")
    else:
        require(proof["state"] and proof["state"]["state"] == "FINALIZED", "historical transaction unfinished")
        require(read_json(module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.json") == intent["record"],
                "historical outer audit record drift")
        require(module.audit_history["audits"][:len(intent["post"]["history"]["audits"])] == intent["post"]["history"]["audits"]
                and module.progress.get("last_audited_chapter", 0) >= end
                and (not intent["chunking"].get("candidate_count", 0)
                     or intent["operation_id"] in module.progress.get("accounted_api_operation_ids", [])),
                "historical outer audit/accounting linkage missing")
        backup = module.BACKUP_DIR / f"checkpoint_{end:04d}"
        for key, path in state_paths(module).items():
            require(read_json(backup / path.name) == intent["post"][key], "historical audit checkpoint state drift")
        require(file_sha(backup / module.CONTINUITY_FILE.name) == intent["continuity_sha256"],
                "historical continuity checkpoint drift")
    return before, after


def _blobs(module, intents, *, _inputs=None):
    from historical_reconciliation import plain
    read = _inputs.read if _inputs is not None else lambda path: plain(path).read_bytes()
    blobs = {}
    for path in module.OUTPUT_DIR.glob("*.md"):
        raw = read(path)
        blobs[tx.digest(raw)] = raw
    from production_standalone_lineage import witnesses, add_blobs
    add_blobs(module, witnesses(module), blobs)
    for intent in intents:
        plan = intent["transaction_plan"]
        for item in plan["chapters"]:
            if item["edits"]:
                path = module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" / f"{item['chapter']:04d}.md"
                if path.exists():
                    raw = read(path)
                    blobs[tx.digest(raw)] = raw
    return blobs


def verify_audit_obligations(project, *, pending_window=None):
    """Ordinary inventory/preflight never step over an unfinished intent.

    The local-only variant independently proves the sole requested pending
    prefix; an unrelated pending audit still blocks. No blanket allow_pending.
    """
    standalone = project.root / "output/retroactive_fixes/standalone_lineage"
    if standalone.exists():
        from goat_periodic_audit_finalize_plan import local_module
        from production_standalone_lineage import verified_events
        module = local_module(project)
        if project.is_legacy:
            module.ACTIVE_NOVEL_CONFIG = None
        require(pending_window is None or module.progress.get("provider") == "command_code_provider",
                "local lineage reconciliation requires GOAT")
        verified_events(module, pending_window=pending_window)
        if pending_window:
            intent = load_intent(module, *pending_window)
            _prove_audit(module, intent, _blobs(module, _audits(module)), pending=True)
        return
    if not list(project.audit_dir.glob("*.finalization.json")):
        require(pending_window is None, "local reconciliation intent missing")
        return
    from goat_periodic_audit_finalize_plan import local_module
    module = local_module(project)
    if module.progress.get("provider") != "command_code_provider":
        require(pending_window is None, "local lineage reconciliation requires GOAT")
        return
    with proof_input_scope(module) as _inputs:
        intents = _audits(module, _inputs=_inputs)
        if (project.root / "output" / "term_repairs").is_dir():
            # A term repair changes chapters outside this history, with its own backups and
            # log, so the byte chain can't reach them. An unfinished audit still blocks.
            for intent in intents:
                unfinished = module.progress.get("last_audited_chapter", 0) < intent["end"]
                require(not unfinished or pending_window == (intent["start"], intent["end"]),
                        "unfinished periodic audit requires exact local finalization")
            return
        blobs = _blobs(module, intents, _inputs=_inputs)
        matched = False
        latest = {}
        previous_end = 0
        for intent in intents:
            window = (intent["start"], intent["end"])
            require(intent["start"] > previous_end, "competing or out-of-order audit intents")
            previous_end = intent["end"]
            pending = pending_window == window
            unfinished = module.progress.get("last_audited_chapter", 0) < intent["end"]
            require(not unfinished or pending, "unfinished periodic audit requires exact local finalization")
            require(not pending or unfinished, "local reconciliation is not an unfinished audit")
            before, after = _prove_audit(module, intent, blobs, pending=pending,
                                        legacy_completion="historical_proof" not in intent, _inputs=_inputs)
            for number, raw in before.items():
                require(number not in latest or latest[number] == tx.digest(raw),
                        "disconnected historical audit byte chain")
                latest[number] = (_inputs.sha(module.OUTPUT_DIR / f"{number:04d}.md") if pending
                                  else tx.digest(after[number]))
            matched |= pending
        require(pending_window is None or matched, "exact local reconciliation intent missing")
        require(all(_inputs.sha(module.OUTPUT_DIR / f"{number:04d}.md") == sha for number, sha in latest.items()),
                "historical audit chain does not reach current chapter bytes")


def verify_disposition_lineage(project, original, *, pending_window=None):
    """Prove a unique chain to current bytes; optional exact local audit tail.

    pending_window is used only by the transport-disabled audit planner, after
    its own exact checkpoint/intent/transaction proof, never ordinary preflight.
    Original disposition/receipt/journal validation remains with the caller.
    """
    from goat_periodic_audit_finalize_plan import local_module
    if (project.root / "output" / "term_repairs").is_dir():
        return  # A term repair changes chapters outside this chain, with its own backups and log.
    module = local_module(project)
    if project.is_legacy and (project.root / "output/retroactive_fixes/standalone_lineage").exists():
        module.ACTIVE_NOVEL_CONFIG = None
    with proof_input_scope(module) as _inputs:
        chapter = original["chapter"]
        if (project.root / "output/retroactive_fixes/standalone_lineage").exists():
            from production_standalone_lineage import verified_events
            events = verified_events(module, pending_window=pending_window)
            if pending_window:
                intent = load_intent(module, *pending_window, _inputs=_inputs)
                before, after = _prove_audit(module, intent, _blobs(module, _audits(module, _inputs=_inputs), _inputs=_inputs), pending=True, _inputs=_inputs)
                events.append((intent["transaction_plan"], before, after))
            previous = None
            for plan, before, after in events:
                item = next((i for i in plan["chapters"] if i["chapter"] == chapter and i["edits"]), None)
                if item is None:
                    continue
                require(decoded_text_sha(before[chapter]) == original["translation_sha256"] if previous is None
                        else item["before_sha256"] == previous, "disconnected standalone/disposition chain")
                previous = item["after_sha256"]
                if pending_window and plan == intent["transaction_plan"]:
                    require(_inputs.sha(module.OUTPUT_DIR / f"{chapter:04d}.md") in
                            {item["before_sha256"], item["after_sha256"]}, "pending lineage prose drift")
                    return
            require(previous is not None and previous == _inputs.sha(module.OUTPUT_DIR / f"{chapter:04d}.md"),
                    "standalone chain does not reach current bytes")
            return
        intents = _audits(module, _inputs=_inputs)
        by_id = {i["transaction_plan"]["transaction_id"]: i for i in intents}
        require(len(by_id) == len(intents), "competing historical audit intents")
        candidates = []
        for path in sorted((module.RETROACTIVE_DIR / "transactions").glob("*.plan.json")):
            plan = _inputs.read_json(path)
            # Discover by provenance, then verify the canonical slot as well.
            if any(i.get("chapter") == chapter and i.get("edits") for i in plan.get("chapters", [])):
                require(plan.get("transaction_id") in by_id, "historical edit lacks outer audit intent")
                request = plan["request"]
                slot = canonical_sha256({k: request[k] for k in ("novel_id", "audit_label", "through_chapter")})
                require(path.name == f"{slot}.plan.json", "historical plan path/provenance mismatch")
        for intent in intents:
            items = [i for i in intent["transaction_plan"]["chapters"] if i["chapter"] == chapter and i["edits"]]
            if items:
                require(len(items) == 1, "duplicate historical chapter")
                candidates.append((intent, items[0]))
        require(candidates, "completed disposition final/progress drift: no historical lineage")
        # Build byte witnesses for unchanged chapters of older audits. Later valid
        # BEFORE backups can witness them even when current prose has moved on.
        blobs = _blobs(module, intents, _inputs=_inputs)
        previous_sha, previous_end = None, 0
        for intent, item in candidates:
            window = (intent["start"], intent["end"])
            pending = pending_window == window
            require(intent["start"] > previous_end and intent["baseline"]["progress"].get("last_audited_chapter", 0) < intent["start"],
                    "historical audit order/fork conflict")
            before, after = _prove_audit(module, intent, blobs, pending=pending, _inputs=_inputs)
            if previous_sha is None:
                require(decoded_text_sha(before[chapter]) == original["translation_sha256"],
                        "disposition decoded-text/BEFORE byte bridge mismatch")
            else:
                require(item["before_sha256"] == previous_sha, "disconnected or competing historical chain")
            previous_sha = item["after_sha256"]
            previous_end = intent["end"]
            if pending:
                require(intent is candidates[-1][0], "pending audit is not terminal lineage")
                current = tx.digest(_inputs.read(module.OUTPUT_DIR / f"{chapter:04d}.md"))
                require(current in {item["before_sha256"], item["after_sha256"]}, "pending lineage prose drift")
                return
        require(previous_sha == _inputs.sha(module.OUTPUT_DIR / f"{chapter:04d}.md"), "historical chain does not reach current bytes")
