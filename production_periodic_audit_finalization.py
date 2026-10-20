"""Provider-aware accounting and local, restart-safe periodic audit publication.

Acquisition stays in the production adapter. This module has no transport and
never rolls back prose. A durable intent proves exact old/new publication
prefixes; progress is written only after state and checkpoint backup reconcile.
"""
import copy
import hashlib
import json
import math
import os
import time
from types import SimpleNamespace
from pathlib import Path

from glossary_audit import STAT_TOTAL_FIELDS
from noveltranslator_core import atomic_write_json, atomic_write_bytes
from production_accounting import (
    canonical_sha256, validate_goat_stats, validate_progress_accounting,
)
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_failure_classification import ProductionIntegrityError
import retroactive_transactions as tx
from retroactive_span_planning import normalized_rules, ownership_policy

REVISION = "periodic-audit-local-finalization-v1"


class AuditFinalizationBlocked(ProductionIntegrityError):
    pass


def require(condition, message):
    if not condition:
        raise AuditFinalizationBlocked(message)


def file_sha(path):
    path = Path(path)
    require(not any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)), f"linked audit evidence: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def read_json(path):
    file_sha(path)
    return json.loads(Path(path).read_bytes())


def json_bytes(value):
    """Bytes emitted by the existing atomic JSON writer on this platform."""
    return json.dumps(value, ensure_ascii=False, indent=2).replace("\n", os.linesep).encode("utf-8")


def authoritative_audit_stats(run, combine, *, provider):
    """Rebuild GOAT identity from paid components, never numeric inference.

    The generic direct aggregate remains byte/semantic compatible. GOAT totals
    must equal it exactly (including floating point accumulation order).
    """
    if provider != PROVIDER_COMMAND_CODE:
        return run["stats"]
    records = run["checkpoint"].get("attempt_records")
    require(isinstance(records, list) and records, "GOAT audit lacks paid attempt records")
    ids = [item.get("operation_id") for item in records]
    require(all(isinstance(value, str) and value for value in ids)
            and len(ids) == len(set(ids)), "duplicate/missing audit attempt identity")
    items = []
    for record in records:
        stats = record.get("stats")
        require(isinstance(stats, dict) and stats.get("provider") == PROVIDER_COMMAND_CODE,
                "paid audit component must explicitly identify GOAT")
        require("accounting_components" not in stats, "paid audit attempt must be one response, not a subtotal")
        validate_goat_stats(stats)
        require(stats["cache_hit_tokens"] + stats["cache_miss_tokens"] == stats["prompt_tokens"],
                "audit component cache usage mismatch")
        items.append(stats)
    result = combine(*items)
    validate_goat_stats(result)
    for field in STAT_TOTAL_FIELDS:
        require(result[field] == run["stats"].get(field), f"audit aggregate numeric mismatch: {field}")
    require(run["stats"].get("api_request_count") == len(records), "audit request count mismatch")
    result["api_request_count"] = len(records)
    return result


def intent_path(module, start, end):
    return module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.finalization.json"


def publication_timestamp(module, start, end):
    path = module.periodic_audit_checkpoint_path(start, end)
    return path.stat().st_mtime if path.exists() else time.time()


def operation_id(module, start, end, chunking):
    novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
    return f"periodic_glossary_audit:{novel}:{start}:{end}:{chunking['candidate_set_hash']}"


def state_paths(module):
    return dict(glossary=module.GLOSSARY_FILE, progress=module.PROGRESS_FILE,
                issues=module.ISSUES_FILE, history=module.AUDIT_HISTORY_FILE)


def chapter_manifest(module):
    return {p.name: file_sha(p) for p in sorted(module.OUTPUT_DIR.glob("*.md"))}


def transaction_proof(module, start, end, baseline_glossary, staged, changes, *, chapter_bytes=None):
    """Pure PR155 proof, including original plan regeneration from BEFORE bytes."""
    novel = module.ACTIVE_NOVEL_CONFIG.id if module.ACTIVE_NOVEL_CONFIG else "legacy_single_novel"
    label = f"{start:04d}_{end:04d}"
    request = dict(novel_id=novel, audit_label=label, through_chapter=end,
        rules=normalized_rules(changes["retroactive_replacements"]),
        ownership_policy=ownership_policy(baseline_glossary, staged),
        safe_categories=sorted(module.ACTIVE_PROFILE.retroactive_safe_categories),
        committed_chapters=sorted(n for n in module.progress["completed"] if 1 <= n <= end))
    slot = canonical_sha256(dict(novel_id=novel, audit_label=label, through_chapter=end))
    directory = module.RETROACTIVE_DIR / "transactions"
    path = directory / f"{slot}.plan.json"
    state_path = directory / f"{slot}.state.json"
    result_path = directory / f"{slot}.result.json"
    evidence = {}
    originals = {}
    intent = load_intent(module, start, end)
    current_cache = {}
    def current_bytes(number):
        if number not in current_cache:
            if chapter_bytes is None:
                file_sha(module.OUTPUT_DIR / f"{number:04d}.md")
            current_cache[number] = (chapter_bytes[number] if chapter_bytes is not None else
                                    (module.OUTPUT_DIR / f"{number:04d}.md").read_bytes())
        return current_cache[number]
    expected_plan = intent["transaction_plan"] if intent else None
    existing = path.exists()
    if existing:
        plan = read_json(path)
        tx.validate_plan(plan, request, module.chapter_map)
        backup_root = module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before"
        for item in plan["chapters"]:
            number = item["chapter"]
            current_path = module.OUTPUT_DIR / f"{number:04d}.md"
            current = current_bytes(number)
            backup = backup_root / current_path.name
            sha = tx.digest(current)
            require(sha in {item["before_sha256"], item["after_sha256"]}, "retroactive current chapter drift")
            if backup.exists():
                require(file_sha(backup) == item["before_sha256"], "retroactive backup mismatch")
                evidence[str(backup)] = file_sha(backup)
            if item["edits"] and sha == item["after_sha256"]:
                require(backup.is_file(), "AFTER chapter lacks transaction backup")
                originals[number] = backup.read_bytes()
            else:
                originals[number] = current
        require(tx.build_plan(request, originals, module.chapter_map) == plan, "retroactive deterministic plan mismatch")
    else:
        require(not state_path.exists() and not result_path.exists()
                and not list(directory.glob(f"{slot}.reconcile.*.json")), "retroactive evidence lacks original plan")
        originals = {n: current_bytes(n) for n in request["committed_chapters"]}
        if intent:
            # Only an untouched intent-before-plan interruption is recoverable.
            require(chapter_bytes is None, "historical lineage lacks immutable plan")
            require("historical_proof" in intent, "legacy intent cannot regenerate missing transaction plan")
            require(chapter_manifest(module) == intent["chapters"], "missing plan after historical mutation")
            require(all(read_json(p) == intent["baseline"][k] for k, p in state_paths(module).items())
                    and not (module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.json").exists(),
                    "missing plan after audit publication")
            require(not any(p.is_file() for p in (module.BACKUP_DIR / f"retroactive_{expected_plan['transaction_id']}").rglob("*")),
                    "missing plan with orphan backup")
            require(not any(p.is_file() for p in (module.BACKUP_DIR / f"checkpoint_{end:04d}").rglob("*")),
                    "missing plan with orphan audit checkpoint")
        else:
            # Backups whose immutable plan disappeared cannot authorize a new
            # baseline. Existing other audits are identified by plan provenance.
            known = {read_json(p).get("transaction_id") for p in directory.glob("*.plan.json")}
            require(not any(p.name.removeprefix("retroactive_") not in known
                            and any(p.rglob("*.md")) for p in module.BACKUP_DIR.glob("retroactive_*")),
                    "orphan historical backup without immutable plan")
        plan = tx.build_plan(request, originals, module.chapter_map)
    if intent:
        require(plan == expected_plan, "retroactive plan differs from immutable audit intent")
        if "historical_proof" in intent:
            require(all(intent["chapters"].get(f"{i['chapter']:04d}.md") == i["before_sha256"]
                        for i in plan["chapters"]), "audit BEFORE manifest/plan mismatch")
    outcomes = [dict(chapter=item["chapter"], status="newly_applied" if item["edits"] else "skipped",
        before_sha256=item["before_sha256"], after_sha256=item["after_sha256"],
        backup_sha256=item["before_sha256"] if item["edits"] else None,
        backup=str(module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" / f"{item['chapter']:04d}.md") if item["edits"] else None,
        replacement_count=len(item["edits"]), reconciliation="expected_after" if item["edits"] else "unchanged") for item in plan["chapters"]]
    result = tx._summary(plan, outcomes, False)
    state = read_json(state_path) if state_path.exists() else None
    prior = read_json(result_path) if result_path.exists() else None
    def prove_summary(summary):
        require(summary == tx._summary(plan, summary.get("chapter_results", []), bool(summary.get("transactions_resumed")))
                and len(summary["chapter_results"]) == len(plan["chapters"]), "retroactive result mismatch")
        for item, outcome, expected in zip(plan["chapters"], summary["chapter_results"], outcomes):
            comparable = dict(outcome)
            if item["edits"]:
                backup_path = Path(str(outcome.get("backup", "")))
                if not backup_path.is_absolute():
                    backup_path = module.PROGRESS_FILE.resolve().parent.parent / backup_path
                require(backup_path.resolve() == Path(expected["backup"]).resolve(), "retroactive result backup path mismatch")
                comparable["backup"] = expected["backup"]
            require(outcome.get("status") in ({"newly_applied", "already_applied"} if item["edits"] else {"skipped"})
                    and comparable == {**expected, "status": outcome["status"]}, "retroactive chapter result mismatch")
    if intent:
        prove_summary(intent["record"]["retroactive"])
    if state is not None:
        require(state.get("transaction_id") == plan["transaction_id"] and state.get("state") in
                {"PLANNED", "APPLIED", "FINALIZED"}, "retroactive transaction state mismatch")
        fields = {"PLANNED": {"transaction_id", "state"},
                  "APPLIED": {"transaction_id", "state", "chapter_results"},
                  "FINALIZED": {"transaction_id", "state", "result_sha256", "reconciliation_sha256"}}
        require(set(state) == fields[state["state"]], "retroactive state schema mismatch")
    edited = [i for i in plan["chapters"] if i["edits"]]
    after_flags = [tx.digest(current_bytes(i["chapter"])) == i["after_sha256"] for i in edited]
    require(after_flags == sorted(after_flags, reverse=True), "out-of-order historical edit prefix")
    if any(after_flags):
        require(state is not None, "edited chapter lacks transaction state")
    if state is None:
        require(not any((module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" /
                         f"{i['chapter']:04d}.md").exists() for i in edited),
                "BEFORE backup lacks transaction state")
    untouched_seen = False
    for item in edited:
        backup = module.BACKUP_DIR / f"retroactive_{plan['transaction_id']}" / "chapters_before" / f"{item['chapter']:04d}.md"
        is_before = tx.digest(current_bytes(item["chapter"])) == item["before_sha256"]
        require(not (untouched_seen and backup.exists()), "out-of-order BEFORE backup prefix")
        untouched_seen |= is_before
    if state and state["state"] in {"APPLIED", "FINALIZED"}:
        require(all(after_flags), "applied transaction has BEFORE chapters")
    if state and state["state"] == "APPLIED":
        prove_summary(tx._summary(plan, state.get("chapter_results", []), False))
    if prior is not None:
        require(state and state["state"] in {"APPLIED", "FINALIZED"}, "result precedes APPLIED state")
    reconciliations_present = list(directory.glob(f"{slot}.reconcile.*.json"))
    require(not reconciliations_present or prior is not None, "reconciliation lacks transaction result")
    for reconciliation_path in reconciliations_present:
        reconciliation = read_json(reconciliation_path)
        prove_summary(reconciliation)
        require(reconciliation_path.name == f"{slot}.reconcile.{canonical_sha256(reconciliation)}.json",
                "retroactive reconciliation identity drift")
    if prior is not None:
        prove_summary(prior)
        result = prior
    if state and state["state"] == "FINALIZED":
        require(prior is not None and file_sha(result_path) == state.get("result_sha256"), "retroactive final result hash mismatch")
        reconciliations = [p for p in directory.glob(f"{slot}.reconcile.*.json") if file_sha(p) == state.get("reconciliation_sha256")]
        require(len(reconciliations) == 1, "retroactive reconciliation hash mismatch")
        reconciliation = read_json(reconciliations[0])
        prove_summary(reconciliation)
        require(reconciliations[0].name == f"{slot}.reconcile.{canonical_sha256(reconciliation)}.json", "retroactive reconciliation filename mismatch")
        for item in plan["chapters"]:
            require(tx.digest(current_bytes(item["chapter"])) == item["after_sha256"], "FINALIZED transaction chapter is not AFTER")
    for p in (path, state_path, result_path):
        evidence[str(p)] = file_sha(p)
    for p in directory.glob(f"{slot}.reconcile.*.json"):
        evidence[str(p)] = file_sha(p)
    return dict(plan=plan, result=result, state=state, evidence=evidence, existing=existing)


def prepare_finalization(module, start, end, audit_result, audit_stats, chunking, *, timestamp=0, chapter_bytes=None):
    """Detached validation; this function never creates a transaction or writes."""
    baseline = dict(glossary=copy.deepcopy(module.glossary), progress=copy.deepcopy(module.progress),
                    issues=copy.deepcopy(module.issues_data), history=copy.deepcopy(module.audit_history))
    require(start == baseline["progress"].get("last_audited_chapter", 0) + 1 and end <= baseline["progress"]["last_completed"], "audit window/progress mismatch")
    require(not any(row.get("start_chapter") == start and row.get("end_chapter") == end
                    for row in baseline["history"]["audits"]), "audit already present without finalization intent")
    candidate = copy.deepcopy(baseline["progress"])
    native_goat = (candidate.get("provider") == PROVIDER_COMMAND_CODE
                   and getattr(module, "_experimental_runtime", None) is None)
    op = operation_id(module, start, end, chunking)
    require(op not in candidate.get("accounted_api_operation_ids", []), "audit accounting exists without final marker")
    zero_send = chunking.get("candidate_count") == 0 and chunking.get("api_request_count", 0) == 0
    if zero_send:
        require(all(audit_stats.get(k, 0) == 0 for k in STAT_TOTAL_FIELDS)
                and not audit_stats.get("accounting_components") and not audit_result["decisions"], "zero-send audit contains paid usage")
        # No GOAT call occurred: do not bind a provider or register paid usage.
        # Direct production retains its established zero-operation registry.
        if not native_goat:
            module.add_accounting_totals_once(candidate, op, audit_stats)
        else:
            validate_progress_accounting(candidate)
    else:
        if native_goat:
            validate_goat_stats(audit_stats)
        # This established production callback validates the detached progress
        # through the unchanged provider-mixing guard. Isolated historical
        # adapters retain their own already-installed accounting validator.
        module.add_accounting_totals_once(candidate, op, audit_stats)
    candidate["last_audited_chapter"] = end
    staged = copy.deepcopy(baseline["glossary"])
    changes = module.apply_periodic_audit(start, end, audit_result, glossary_data=staged)
    proof = transaction_proof(module, start, end, baseline["glossary"], staged, changes, chapter_bytes=chapter_bytes)
    record = dict(start_chapter=start, end_chapter=end, timestamp_unix=timestamp,
                  stats=copy.deepcopy(audit_stats), chunking=copy.deepcopy(chunking), changes=changes,
                  retroactive=proof["result"], issues=copy.deepcopy(audit_result.get("issues", [])))
    post_history = copy.deepcopy(baseline["history"])
    post_history["audits"].append(record)
    post_issues = copy.deepcopy(baseline["issues"])
    post_issues["issues"].extend(dict(chapter=end, type="periodic_glossary_audit", details=issue)
                               for issue in audit_result.get("issues", []))
    post = dict(glossary=staged, progress=candidate, issues=post_issues, history=post_history)
    manifest = chapter_manifest(module)
    manifest.update({f"{i['chapter']:04d}.md": i["before_sha256"] for i in proof["plan"]["chapters"]})
    historical_proof = dict(schema_version=1, novel_id=proof["plan"]["request"]["novel_id"],
        before_chapters=manifest, plan_sha256=canonical_sha256(proof["plan"]),
        checkpoint_sha256=file_sha(module.periodic_audit_checkpoint_path(start, end)))
    return dict(revision=REVISION, start=start, end=end, baseline=baseline, post=post,
        record=record, transaction_plan=proof["plan"], operation_id=op,
        chapters=manifest, historical_proof=historical_proof, continuity_sha256=file_sha(module.CONTINUITY_FILE),
        checkpoint_sha256=file_sha(module.periodic_audit_checkpoint_path(start, end)),
        audit_result=copy.deepcopy(audit_result), audit_stats=copy.deepcopy(audit_stats), chunking=copy.deepcopy(chunking))


def load_intent(module, start, end):
    path = intent_path(module, start, end)
    if not path.exists():
        return None
    value = read_json(path)
    require(value.get("intent_sha256") == canonical_sha256({k: v for k, v in value.items() if k != "intent_sha256"})
            and value.get("revision") == REVISION and (value.get("start"), value.get("end")) == (start, end), "audit finalization intent mismatch")
    if "historical_proof" in value:
        require(value["historical_proof"] == dict(schema_version=1,
            novel_id=value["transaction_plan"]["request"]["novel_id"], before_chapters=value["chapters"],
            plan_sha256=canonical_sha256(value["transaction_plan"]), checkpoint_sha256=value["checkpoint_sha256"]),
            "audit historical proof binding mismatch")
    return value


def verify_publication(module, intent):
    """Accept only ordered exact old/new prefixes and planned prose states."""
    paths = state_paths(module)
    record_path = module.AUDIT_DIR / f"audit_{intent['start']:04d}_{intent['end']:04d}.json"
    require(file_sha(module.CONTINUITY_FILE) == intent["continuity_sha256"], "continuity drift")
    require(file_sha(module.periodic_audit_checkpoint_path(intent['start'], intent['end'])) == intent["checkpoint_sha256"], "completed checkpoint drift")
    current_chapters = chapter_manifest(module)
    require(current_chapters.keys() == intent["chapters"].keys(), "historical chapter inventory drift")
    planned = {f"{i['chapter']:04d}.md": i for i in intent["transaction_plan"]["chapters"]}
    for name, sha in current_chapters.items():
        item = planned.get(name)
        accepted = {item["before_sha256"], item["after_sha256"]} if item else {intent["chapters"][name]}
        require(sha in accepted, "unexpected historical chapter mutation")
    tx_after = [current_chapters[f"{i['chapter']:04d}.md"] == i["after_sha256"]
                for i in intent["transaction_plan"]["chapters"] if i["edits"]]
    require(tx_after == sorted(tx_after, reverse=True), "out-of-order historical edit prefix")
    flags = []
    for key in ("glossary", "issues", "history"):
        current = read_json(paths[key])
        require(current in (intent["baseline"][key], intent["post"][key]), f"audit {key} publication drift")
        flags.append(current == intent["post"][key] or intent["post"][key] == intent["baseline"][key])
    if record_path.exists():
        require(read_json(record_path) == intent["record"], "audit record drift")
        flags.append(True)
    else:
        flags.append(False)
    # Unchanged artifacts are neutral. A changed artifact cannot be published
    # after an earlier changed artifact remains at its baseline.
    changed = [flags[i] for i, k in enumerate(("glossary", "issues", "history")) if intent["post"][k] != intent["baseline"][k]] + [flags[3]]
    require(changed == sorted(changed, reverse=True), "out-of-order audit publication prefix")
    require(not any(changed) or all(tx_after), "audit publication precedes historical edits")
    current_progress = read_json(paths["progress"])
    require(current_progress in (intent["baseline"]["progress"], intent["post"]["progress"]), "audit progress drift")
    finalized = current_progress == intent["post"]["progress"]
    require(not finalized or all(flags), "progress marker precedes final audit artifacts")
    return finalized


def finalize_periodic_audit(module, start, end, audit_result, audit_stats, chunking):
    """Common normal/recovery local seam. Caller owns the novel production lock."""
    intent = load_intent(module, start, end)
    if intent is None:
        intent = prepare_finalization(module, start, end, audit_result, audit_stats, chunking,
                                      timestamp=publication_timestamp(module, start, end))
        record_path = module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.json"
        require(not record_path.exists(), "audit record exists without proven intent")
        intent["intent_sha256"] = canonical_sha256(intent)
        module.AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(intent_path(module, start, end), intent)
    require(audit_result == intent["audit_result"] and audit_stats == intent["audit_stats"]
            and chunking == intent["chunking"], "audit finalization inputs drift")
    finalized = verify_publication(module, intent)
    # Revalidate deterministic accounting on the original detached baseline,
    # including on restart; never infer a baseline from partially installed state.
    detached = SimpleNamespace(**vars(module))
    for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
        setattr(detached, name, copy.deepcopy(intent["baseline"][key]))
    if (detached.progress.get("provider") == PROVIDER_COMMAND_CODE and chunking.get("candidate_count", 0)
            and getattr(detached, "_experimental_runtime", None) is None):
        # A completed checkpoint cannot authorize altered lower paid evidence
        # after a local-publication crash. This is a read-only proof, never an
        # acquisition/recovery callback capable of sending a request.
        from goat_periodic_audit_finalize_plan import checkpoint_proof
        paid = checkpoint_proof(detached, start, end)
        require(paid["audit_result"] == audit_result and paid["audit_stats"] == audit_stats,
                "audit finalization paid-evidence drift")
    # apply_periodic_audit is a function bound to module globals but receives
    # the explicit baseline/staged glossary and uses immutable source context.
    prepared = prepare_finalization(detached, start, end, audit_result, audit_stats, chunking,
                                    timestamp=intent["record"]["timestamp_unix"])
    for key in ("post", "transaction_plan", "operation_id"):
        left, right = prepared[key], intent[key]
        if key == "post":
            # PR155 replay diagnostics live in its own durable result. The
            # publication record keeps the first intent's semantic snapshot.
            left["history"]["audits"][-1]["retroactive"] = right["history"]["audits"][-1]["retroactive"]
        require(left == right, f"audit deterministic finalization drift: {key}")
    proof = transaction_proof(detached, start, end, intent["baseline"]["glossary"], intent["post"]["glossary"], intent["record"]["changes"])
    if finalized:
        require(proof["state"] and proof["state"]["state"] == "FINALIZED", "final audit lacks finalized transaction")
    elif not proof["state"] or proof["state"]["state"] != "FINALIZED":
        tx.execute_retroactive_transaction(novel_id=proof["plan"]["request"]["novel_id"],
            audit_label=f"{start:04d}_{end:04d}", through_chapter=end,
            replacements=intent["record"]["changes"]["retroactive_replacements"],
            glossary=intent["baseline"]["glossary"], staged_glossary=intent["post"]["glossary"],
            safe_categories=module.ACTIVE_PROFILE.retroactive_safe_categories,
            committed_chapters=intent["baseline"]["progress"]["completed"], chapter_map=module.chapter_map,
            output_dir=module.OUTPUT_DIR, retroactive_dir=module.RETROACTIVE_DIR, backup_dir=module.BACKUP_DIR)
    # Reprove the immutable transaction, its result, backup and AFTER bytes.
    transaction_proof(detached, start, end, intent["baseline"]["glossary"], intent["post"]["glossary"], intent["record"]["changes"])
    paths = state_paths(module)
    if not finalized:
        for key in ("glossary", "issues", "history"):
            if read_json(paths[key]) != intent["post"][key]:
                module.save_json(paths[key], intent["post"][key])
        record_path = module.AUDIT_DIR / f"audit_{start:04d}_{end:04d}.json"
        if not record_path.exists():
            module.save_json(record_path, intent["record"])
    # Build exact normal checkpoint state before progress (the final marker).
    checkpoint_dir = module.BACKUP_DIR / f"checkpoint_{end:04d}"
    expected_backup = {paths[k].name: (json_bytes(intent["post"][k])
                       if k == "progress" else paths[k].read_bytes()) for k in paths}
    expected_backup[module.CONTINUITY_FILE.name] = module.CONTINUITY_FILE.read_bytes()
    require(not checkpoint_dir.is_symlink(), "symlink audit checkpoint backup")
    if checkpoint_dir.exists():
        require({p.name for p in checkpoint_dir.iterdir()} <= expected_backup.keys(), "unexpected audit backup artifact")
    elif not finalized:
        checkpoint_dir.mkdir(parents=True)
    for name, value in expected_backup.items():
        path = checkpoint_dir / name
        if path.exists():
            require(file_sha(path) == hashlib.sha256(value).hexdigest(), "audit checkpoint backup mismatch")
        else:
            require(not finalized, "final audit checkpoint backup missing")
            atomic_write_bytes(path, value)
    if not finalized:
        module.save_json(paths["progress"], intent["post"]["progress"])
    for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
        value = getattr(module, name)
        value.clear()
        value.update(copy.deepcopy(intent["post"][key]))
    return dict(status="ALREADY_FINALIZED" if finalized else "FINALIZED", audit_record=intent["record"], provider_calls=0)
