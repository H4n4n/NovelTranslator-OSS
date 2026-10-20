"""Explicit operator ZERO-USAGE disposition; never installed by ordinary production."""
import copy
import json
from pathlib import Path
from production_paid_request_journal import request_identity, _sha256_json, load_matching_journal, paid_request_journal_path
from production_accounting import validate_goat_stats
from production_goat_cap_recovery import accepted_envelope
from production_goat_state_policy_migration import text_sha
from benchmark_longitudinal_provider import build_provider_request_identity
from production_state_extraction import parse_state_response
from production_goat_state_terminal_recovery import settle, verify_settlement

REVISION = "uncertain-no-provider-record-disposition-v1"
KIND = "state_extraction_uncertain_no_record_recovery"
MODE = "uncertain_no_provider_record_disposition"
MARKER = "uncertain_no_record_recovery"
REGISTRY = "uncertain_request_dispositions"


def registry_path(project, chapter):
    path = project.data_dir / REGISTRY / f"{chapter:04d}.json"
    require_safe_registry_path(path, novel_root=project.root)
    return path


def require_safe_registry_path(path, *, novel_root=None):
    """Check lexical containment and every ancestor before following any child.

    Reject directory links, Windows junctions/reparse points, dangling links and
    non-directory parents. This is a cooperative-writer filesystem boundary,
    not protection against a hostile process replacing directories concurrently.
    """
    import stat
    path = Path(path).absolute()
    if (".." in path.parts or path.parent.name != REGISTRY
        or path.parent.parent.name != "data"
        or (novel_root is not None and path.parents[2] != Path(novel_root).absolute())):
        raise RuntimeError("unsafe disposition registry path outside novel root")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise RuntimeError("disposition registry symlink/reparse ancestor blocks")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("disposition registry ancestor is not a directory")
        if item == path and not stat.S_ISREG(info.st_mode):
            raise RuntimeError("disposition evidence must be a regular file")
    return path


def load_immutable(path):
    """A malformed/partial create-once file blocks; it is never overwritten."""
    path = require_safe_registry_path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("disposition evidence must be a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    from production_paid_request_journal import _record_hash
    if not isinstance(value, dict) or value.get("record_sha256") != _record_hash(value):
        raise RuntimeError("immutable disposition evidence hash mismatch")
    return value


def publish_immutable(path, value):
    """Exclusive file creation + file fsync + exact readback; no directory-fsync claim."""
    import os
    from production_paid_request_journal import _canonical_json, _with_record_hash
    path = require_safe_registry_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_safe_registry_path(path)
    value = _with_record_hash(value)
    raw = (_canonical_json(value) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_bytes() != raw:
        raise RuntimeError("immutable disposition evidence readback failed")
    return value


def write_disposition(plan):
    from datetime import datetime, timezone
    path = require_safe_registry_path(plan["disposition_path"],
        novel_root=Path(plan["disposition_proof"]["original_journal_path"]).parents[2])
    existing = load_immutable(path)
    body = dict(revision=REVISION, disposition_id=plan["disposition_id"],
        proof=plan["disposition_proof"], provider_usage_disposition="not_recorded",
        provider_usage="zero", accounting_disposition="zero_provider_usage",
        provider_proof_claimed=False)
    if existing:
        if {k:v for k,v in existing.items() if k not in {"created_utc", "record_sha256"}} != body:
            raise RuntimeError("disposition conflict")
        return existing
    return publish_immutable(path, dict(body, created_utc=datetime.now(timezone.utc).isoformat()))


def claim_path(disposition_path, kind):
    path = Path(disposition_path).with_suffix("." + kind + ".claim.json")
    require_safe_registry_path(path)
    return path


def claim_send(module, kind, binding):
    """Runs only inside budget authorization, before STARTED/transport.

    A death after claim but before STARTED sacrifices the slot. No ambiguity is
    silently converted into another send, even if the journal is deleted.
    """
    from datetime import datetime, timezone
    plan = module._uncertain_disposition_plan
    write_disposition(plan)
    publish_immutable(claim_path(plan["disposition_path"], kind), dict(
        revision=REVISION, disposition_id=plan["disposition_id"],
        recovery_identity=copy.deepcopy(binding),
        created_utc=datetime.now(timezone.utc).isoformat(), consumed=True))


def validate_evidence(evidence, record, journal_sha, translation):
    """Validate an operator assertion, not a cryptographic provider receipt."""
    from datetime import datetime
    if not isinstance(evidence, dict) or evidence.get("type") != "operator_reviewed_external_provider_usage":
        raise RuntimeError("operator-reviewed external provider usage evidence required")
    if (not isinstance(evidence.get("operator"), str) or not evidence["operator"].strip()
        or evidence.get("attestation") != "I reviewed authoritative provider Usage and attest that this exact request was not recorded or metered."
        or evidence.get("provider_proof_claimed") is not False):
        raise RuntimeError("explicit operator attestation required; no provider proof claimed")
    for key in ("operation_id", "request_sha256", "wire_request_sha256", "provider", "model"):
        if evidence.get(key) != record.get(key):
            raise RuntimeError("operator evidence original identity mismatch: " + key)
    if evidence.get("journal_sha256") != journal_sha:
        raise RuntimeError("operator evidence journal hash mismatch")
    started = datetime.fromisoformat(record["attempt_context"]["request_started_utc"])
    reviewed = datetime.fromisoformat(evidence["reviewed_utc"])
    window = evidence["usage_window"]
    begin, end = (datetime.fromisoformat(window[k]) for k in ("start_local", "end_local"))
    if (started.tzinfo is None or reviewed.tzinfo is None or begin.tzinfo is None or end.tzinfo is None
        or window.get("timezone") != "America/Lima" or not begin <= started <= end <= reviewed
        or datetime.fromisoformat(window["uncertain_started_local"]) != started):
        raise RuntimeError("operator usage window does not bind the uncertain start")
    row = evidence["preceding_api"]
    if (row.get("type") != "api" or row.get("status") != "COMPLETED"
        or row.get("model") != record["model"] or row.get("matches_durable_translation") is not True
        or row.get("translation_request_sha256") != translation["wire_request_sha256"]
        or datetime.fromisoformat(row["timestamp_local"]) >= started
        or datetime.fromisoformat(row["timestamp_local"]) < begin
        or row.get("input_tokens") != translation["response"]["stats"]["prompt_tokens"]
        or row.get("output_tokens") != translation["response"]["stats"]["completion_tokens"]
        or row.get("displayed_cost") != f'{translation["response"]["stats"]["cost_usd"]:.4f}'
        or evidence.get("no_subsequent_api_row_attributable_before_agent_traffic") is not True
        or evidence.get("later_traffic_type") != "agent"):
        raise RuntimeError("operator chronology does not match durable translation")
    if "external_evidence_path" in evidence or "external_evidence_sha256" in evidence:
        # Metadata only: the operator attests the external artifact; never open
        # arbitrary paths (in particular credentials) on the planner's behalf.
        import re
        if not isinstance(evidence.get("external_evidence_path"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", evidence.get("external_evidence_sha256", "")):
            raise RuntimeError("optional external evidence path/SHA must be supplied together")


def original_has_usage(record):
    return any(record.get(k) not in (None, {}, "") for k in
        ("response", "response_sha256", "usage", "stats", "accounting", "provider_usage"))


def journal_identity(proof, wire):
    policy = proof["provider_policy"]
    result = request_identity(operation_kind=KIND, novel_id=proof["novel_id"],
        chapter=proof["chapter"], provider=policy["provider"], model=policy["wire_model"],
        profile_id=proof["profile_id"], source_sha256=proof["source_sha256"],
        translation_sha256=proof["translation_sha256"],
        request_payload={"wire_request": wire, "provider_policy": policy, "uncertain_no_record_recovery": proof})
    result.update(provider_policy=copy.deepcopy(policy),
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256,
        uncertain_no_record_recovery=copy.deepcopy(proof))
    return result


def install_uncertain_recovery(module, plan):
    """Only a freshly authorized locked executor installs this scoped adapter."""
    from production_goat_runtime import normalize_production_request
    module._uncertain_disposition_plan = copy.deepcopy(plan)
    proof = copy.deepcopy(plan["disposition_proof"])
    chapter = proof["chapter"]
    path = Path(module.PENDING_DIR) / f"{chapter:04d}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    write_disposition(plan)
    if MARKER not in meta:
        if text_sha(path.read_bytes().decode("utf-8")) != proof["original_pending_file_sha256"]:
            raise RuntimeError("uncertain recovery pending bytes drift")
        meta[MARKER] = {"proof": proof, "original_pending_metadata": copy.deepcopy(meta),
            "original_pending_json": path.read_bytes().decode("utf-8")}
        module.atomic_write_json(path, meta)
    elif meta[MARKER]["proof"] != proof:
        raise RuntimeError("uncertain recovery saved proof drift")
    module._goat_uncertain_state_proof = proof
    module._goat_uncertain_state_phase = "state"

    def extract(chapter_number, source, translation, *, prior_continuity=None):
        if (chapter_number != chapter or text_sha(source) != proof["source_sha256"]
            or text_sha(translation) != proof["translation_sha256"]
            or _sha256_json(prior_continuity) != proof["continuity_sha256"]):
            raise RuntimeError("uncertain recovery extraction inputs drift")
        kwargs = dict(model=module.MODEL, messages=[
            {"role":"system", "content":module.STATE_PROMPT},
            {"role":"user", "content":module.state_extraction_user_prompt(chapter=chapter,
                existing_terms=list(module.glossary["entries"]), prior=prior_continuity,
                source=source, translation=translation)}],
            **module.operation_request_controls("state_extraction"))
        if normalize_production_request("state_extraction", kwargs) != plan["recovery_wire"]:
            raise RuntimeError("uncertain recovery exact original wire drift")
        known = []
        def paid(kind, body, translation_input):
            slot = paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, kind, chapter)
            try:
                result = module.journaled_chat_request(kind, chapter, source, body, translation_text=translation_input)
                known.append((load_matching_journal(slot, {}), result["response"]))
                return result
            except BaseException:
                record = load_matching_journal(slot, {})
                if record and record["status"] == "response_recorded":
                    logical = "state_extraction" if kind == KIND else "state_json_repair"
                    wire = normalize_production_request(logical, body)
                    normalized = accepted_envelope(record["response"], wire)
                    if "stats" in record["response"] and normalized != record["response"]:
                        raise RuntimeError("uncertain failed-response acceptance drift")
                    known.append((record, normalized))
                raise
        def repair(content, ceiling):
            body = dict(model=module.MODEL, messages=[
                {"role":"system", "content":module.GENERIC_JSON_REPAIR_PROMPT +
                    "\n\nRequired structure:\n" + module.STATE_SCHEMA_HINT},
                {"role":"user", "content":"Repair this malformed JSON:\n\n" + content}],
                **module.operation_request_controls("state_json_repair", ceiling=ceiling))
            module._goat_uncertain_state_phase = "json_repair"
            return paid("state_json_repair", body, content)
        try:
            result = paid(KIND, kwargs, translation)
            state, stats = parse_state_response(envelope=result["response"], stats=result["response"]["stats"],
                repair=repair, clean_text=module.clean_json_text, salvage=module.salvage_state_json_without_issues,
                combine_stats=module.combine_stats, emit=lambda event, **fields: module.log_event(event, chapter=chapter, **fields),
                policy=module.STATE_EXTRACTION_POLICY)
            module._goat_uncertain_state_known = known
            return state, module.combine_stats(*(e["stats"] for r, e in known
                if not verify_settlement(module.progress, r, e)))
        except BaseException:
            for record, accepted in known:
                settle(module, record, accepted)
            raise
    module.extract_state = extract


def save_uncertain_state_checkpoint(module, plan):
    """Stop at the normal state checkpoint; pending recovery owns later review."""
    from production_state_extraction_checkpoint import run_state_extraction_checkpoint
    from production_state_extraction import validate_state
    chapter = plan["target_chapter"]
    path = Path(module.PENDING_DIR) / f"{chapter:04d}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    source = module.chapter_map[chapter]["text"]
    text = path.with_suffix(".md").read_text(encoding="utf-8")
    try:
        result = run_state_extraction_checkpoint(meta=meta,
            extract_state=lambda: module.extract_state(chapter, source, text, prior_continuity=module.continuity),
            reprice_saved_stats=module.reprice_saved_stats_if_needed, zero_stats=module.zero_stats,
            persist_meta=lambda value: module.atomic_write_json(path, value),
            checkpoint_identity=module.current_state_checkpoint_identity(chapter, source, text, prior_continuity=module.continuity),
            validate_saved_state=validate_state)
    except BaseException:
        # A local checkpoint failure also settles all accepted known usage.
        # Successful replay excludes these receipts from eventual chapter stats.
        for record, envelope in getattr(module, "_goat_uncertain_state_known", []):
            settle(module, record, envelope)
        raise
    write_checkpoint_receipt(plan)
    return result["status"]


def write_checkpoint_receipt(plan):
    """Preserve the proven state boundary after pending files are committed away."""
    from datetime import datetime, timezone
    from novel_project import load_novel_project
    from uncertain_request_disposition_plan import inspect_target
    project = load_novel_project(plan["novel_id"], repo_root=plan["project_repository_root"])
    target = inspect_target(project, plan["operator_evidence"])
    if not target["saved"] or target["terminal"]:
        raise RuntimeError("checkpoint receipt requires exact successful journal replay")
    meta = json.loads((project.pending_dir / f'{target["chapter"]:04d}.json').read_text(encoding="utf-8"))
    records = []
    for kind in (KIND, "state_json_repair"):
        record = load_matching_journal(paid_request_journal_path(
            project.data_dir / "provider_request_journal", kind, target["chapter"]), {})
        if record:
            records.append(dict(kind=kind, operation_id=record["operation_id"],
                response_sha256=record["response_sha256"]))
    body = dict(revision=REVISION, disposition_id=plan["disposition_id"],
        recovery_wire=copy.deepcopy(plan["recovery_wire"]),
        checkpoint={k:copy.deepcopy(meta[k]) for k in ("state", "state_stats", "state_checkpoint_identity")},
        records=records)
    path = Path(plan["disposition_path"]).with_suffix(".checkpoint.json")
    existing = load_immutable(path)
    if existing:
        if {k:v for k,v in existing.items() if k not in {"created_utc", "record_sha256"}} != body:
            raise RuntimeError("immutable checkpoint receipt drift")
        return False
    publish_immutable(path, dict(body, created_utc=datetime.now(timezone.utc).isoformat()))
    return True


def replay_completed_responses(project, proof, wire, disposition_path, progress, *, completed):
    """Reconstruct state and checkpoint usage using recorded responses only.

    There is no transport callback, journal write or request fallback here.
    Normal chapter accounting IDs are not detached settlements. Only an exact
    detached settlement receipt excludes an operation from checkpoint stats.
    """
    from goat_state_policy_migration_plan import definitions
    from production_goat_runtime import normalize_production_request
    from production_accounting import combine_goat_stats, validate_progress_accounting
    from production_goat_state_terminal_recovery import RECEIPTS
    module = definitions(project)
    chapter, policy = proof["chapter"], proof["provider_policy"]
    journal_dir = project.data_dir / "provider_request_journal"
    validate_progress_accounting(progress, expected_provider=policy["provider"])
    known = []

    def recorded(kind, expected, body):
        record = load_matching_journal(paid_request_journal_path(journal_dir, kind, chapter), expected)
        claim = load_immutable(claim_path(disposition_path, kind))
        if (not record or record["status"] != "response_recorded" or not claim
            or claim.get("revision") != REVISION or claim.get("disposition_id") != _sha256_json(proof)
            or claim.get("consumed") is not True or claim.get("recovery_identity") != expected):
            raise RuntimeError("completed recovery requires exact recorded response and claim")
        envelope = accepted_envelope(record["response"], body, provider_policy=policy)
        if "stats" in record["response"] and envelope != record["response"]:
            raise RuntimeError("completed recovery raw response acceptance drift")
        validate_goat_stats(envelope["stats"])
        known.append((record, envelope))
        return dict(response=envelope, operation_id=record["operation_id"], replayed=True)

    recovered = recorded(KIND, journal_identity(proof, wire), wire)

    def repair(content, ceiling):
        body = dict(model="deepseek-flash", messages=[
            {"role":"system", "content":module.GENERIC_JSON_REPAIR_PROMPT +
                "\n\nRequired structure:\n" + module.STATE_SCHEMA_HINT},
            {"role":"user", "content":"Repair this malformed JSON:\n\n" + content}],
            **module.operation_request_controls("state_json_repair", ceiling=ceiling))
        repair_wire = normalize_production_request("state_json_repair", body)
        expected = request_identity(operation_kind="state_json_repair", novel_id=proof["novel_id"],
            chapter=chapter, provider=policy["provider"], model=policy["wire_model"],
            profile_id=proof["profile_id"], source_sha256=proof["source_sha256"],
            translation_sha256=text_sha(content),
            request_payload={"wire_request":repair_wire, "provider_policy":policy})
        expected.update(provider_policy=copy.deepcopy(policy),
            wire_request_sha256=build_provider_request_identity(request_body=repair_wire).request_sha256)
        return recorded("state_json_repair", expected, repair_wire)

    combine = lambda *items: combine_goat_stats(module.combine_stats, *items)
    state, _ = parse_state_response(envelope=recovered["response"], stats=recovered["response"]["stats"],
        repair=repair, clean_text=module.clean_json_text, salvage=module.salvage_state_json_without_issues,
        combine_stats=combine, emit=lambda *a, **k: None, policy=module.STATE_EXTRACTION_POLICY)
    if len(known) == 1 and (paid_request_journal_path(journal_dir, "state_json_repair", chapter).exists()
        or claim_path(disposition_path, "state_json_repair").exists()):
        raise RuntimeError("unused JSON repair evidence conflicts with deterministic replay")

    def detached_settled(record, envelope):
        receipts = progress.get(RECEIPTS, {})
        ids = progress.get("accounted_api_operation_ids", [])
        if not isinstance(receipts, dict) or not isinstance(ids, list) or ids.count(record["operation_id"]) > 1:
            raise RuntimeError("completed recovery accounting registry malformed")
        if not completed or record["operation_id"] in receipts:
            return verify_settlement(progress, record, envelope)
        # A normal committed chapter can register this ID without a detached
        # receipt; those tokens remain in the historical checkpoint subtotal.
        return False

    stats = combine(*(e["stats"] for r,e in known if not detached_settled(r,e)))
    records = [dict(kind=r["operation_kind"], operation_id=r["operation_id"],
        response_sha256=r["response_sha256"]) for r,e in known]
    return state, stats, records


def effective_paid_request_inventory(project, *, inventory=None):
    """Read-only LIVE ambiguity overlay; raw history and journal IO stay unchanged.

    Only fully verified committed dispositions leave the active uncertainty set.
    Pending dispositions stay active, including their saved checkpoint/restart
    boundary. Errors and verification failures fail closed; there is no send,
    journal mutation, accounting mutation or cached historical approval.
    """
    from production_paid_request_journal import inspect_paid_request_journals
    raw = (inspect_paid_request_journals(project.data_dir / "provider_request_journal")
           if inventory is None else inventory)
    if raw["errors"]:
        return raw
    return verified_completed_dispositions(project, raw, allow_pending=False)


def disposition_registry_roots(project):
    """Discover obligations independently of journal status, without following links.

    Validate the registry boundary before enumeration and every child before
    opening any record. Only canonical root, checkpoint and supported claim
    names are allowed; evidence without its root is an integrity failure.
    """
    import re
    directory = registry_path(project, 1).parent
    if not directory.exists():
        return {}
    roots, evidence = {}, set()
    children = sorted(directory.iterdir())
    for path in children:
        require_safe_registry_path(path, novel_root=project.root)
        match = re.fullmatch(r"([0-9]{4,})(\.json|\.checkpoint\.json|\." + re.escape(KIND)
            + r"\.claim\.json|\.state_json_repair\.claim\.json)", path.name)
        if not match or int(match[1]) < 1 or match[1] != f"{int(match[1]):04d}":
            raise RuntimeError("unexpected disposition registry evidence")
        chapter = int(match[1])
        evidence.add(chapter)
        if match[2] == ".json":
            roots[chapter] = path
    if evidence != set(roots):
        raise RuntimeError("orphaned disposition registry evidence")
    return roots


def verified_completed_dispositions(project, inventory, *, allow_pending=False, _local_audit_window=None):
    """Only the explicit pending planner may accept a *completed* recovery.

    Merely creating a disposition does not resolve preflight. Reconstruct the
    exact state/usage from the claimed recovery journal and normal parser first.
    Ordinary journal inspection and controlled trials remain unchanged.
    """
    if inventory["errors"]:
        return inventory
    from production_historical_lineage import verify_audit_obligations
    verify_audit_obligations(project, pending_window=_local_audit_window)
    roots = disposition_registry_roots(project)
    if not roots and not inventory["uncertain"]:
        return inventory
    from pending_recovery_plan import _sha256_file
    from chapter_library import index_chapter_library
    from production_state_extraction import validate_state
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    resolved, resolved_journals = [], set()
    for chapter, path in roots.items():
        disposition = load_immutable(path)
        receipt = load_immutable(path.with_suffix(".checkpoint.json"))
        completed = chapter in progress.get("completed", [])
        if not disposition:
            raise RuntimeError("disposition root disappeared during verification")
        proof = disposition["proof"]
        original_path = paid_request_journal_path(project.data_dir / "provider_request_journal", "state_extraction", chapter)
        if original_path.is_symlink():
            raise RuntimeError("completed disposition original journal must not be a symlink")
        original = load_matching_journal(original_path, proof["original_identity"])
        if (disposition.get("revision") != REVISION or disposition.get("disposition_id") != _sha256_json(proof)
            or disposition.get("provider_usage_disposition") != "not_recorded" or disposition.get("provider_usage") != "zero"
            or disposition.get("accounting_disposition") != "zero_provider_usage" or disposition.get("provider_proof_claimed") is not False
            or proof["novel_id"] != project.config.id or proof["profile_id"] != project.config.profile or proof["chapter"] != chapter
            or not original or original["status"] != "uncertain" or original_has_usage(original)
            or proof["original_journal_path"] != str(original_path)
            or proof["original_operation_kind"] != "state_extraction"
            or original["operation_id"] != proof["original_operation_id"]
            or original["record_sha256"] != proof["original_record_sha256"]
            or original["request_sha256"] != proof["original_request_sha256"]
            or original["wire_request_sha256"] != proof["original_wire_request_sha256"]
            or original["provider_policy"] != proof["provider_policy"]
            or original["provider"] != proof["provider"] or original["model"] != proof["wire_model"]
            or proof["logical_model"] != "deepseek-flash"
            or proof["provider"] != proof["provider_policy"]["provider"]
            or proof["wire_model"] != proof["provider_policy"]["wire_model"]
            or original["source_sha256"] != proof["source_sha256"]
            or original["translation_sha256"] != proof["translation_sha256"]
            or original["attempt_context"]["request_started_utc"] != proof["started_utc"]
            or original["error"] != proof["uncertain_error"]
            or _sha256_json(original["error"]) != proof["uncertain_error_sha256"]
            or _sha256_file(original_path) != proof["original_journal_sha256"]
            or original["operation_id"] in progress.get("accounted_api_operation_ids", [])):
            raise RuntimeError("completed disposition original proof drift")
        if not any(item["operation_id"] == original["operation_id"] and item["file"] == original_path.name
                   and item["status"] == "uncertain" for item in inventory["uncertain"]):
            raise RuntimeError("disposition original missing from raw ambiguity inventory")
        if not completed and not allow_pending:
            continue
        if not receipt:
            if completed:
                raise RuntimeError("completed disposition checkpoint receipt missing")
            continue
        if receipt.get("revision") != REVISION or receipt.get("disposition_id") != disposition["disposition_id"]:
            raise RuntimeError("completed disposition checkpoint receipt identity drift")
        paid = load_matching_journal(paid_request_journal_path(project.data_dir / "provider_request_journal",
            "chapter_translation", chapter), {})
        validate_evidence(proof["operator_evidence"], original, _sha256_file(original_path), paid)
        validate_state(receipt["checkpoint"]["state"])
        wire = receipt["recovery_wire"]
        if build_provider_request_identity(request_body=wire).request_sha256 != proof["original_wire_request_sha256"]:
            raise RuntimeError("completed disposition exact original wire drift")
        if not receipt["records"] or receipt["records"][0]["kind"] != KIND or len(receipt["records"]) > 2:
            raise RuntimeError("completed disposition recovery receipt malformed")
        state, stats, records = replay_completed_responses(project, proof, wire, path, progress, completed=completed)
        if (receipt["checkpoint"]["state"] != state or receipt["checkpoint"]["state_stats"] != stats
            or receipt["records"] != records
            or receipt["checkpoint"]["state_checkpoint_identity"] !=
                json.loads(proof["original_pending_json"])["state_checkpoint_identity"]):
            raise RuntimeError("completed checkpoint state/usage differs from recorded response replay")
        sources = index_chapter_library(json.loads(project.chapters_file.read_text(encoding="utf-8")),
            project.config.final_chapter, project.config.chapter_numbering)
        if text_sha(sources[chapter]["text"]) != proof["source_sha256"]:
            raise RuntimeError("completed disposition source drift")
        if completed:
            final = project.chapter_dir / f"{chapter:04d}.md"
            if progress.get("last_completed", 0) < chapter:
                raise RuntimeError("completed disposition final/progress drift")
            from production_historical_lineage import verify_disposition_lineage
            if text_sha(final.read_text(encoding="utf-8")) != proof["translation_sha256"]:
                verify_disposition_lineage(project, proof, pending_window=_local_audit_window)
        else:
            meta = json.loads((project.pending_dir / f"{chapter:04d}.json").read_text(encoding="utf-8"))
            text = (project.pending_dir / f"{chapter:04d}.md").read_text(encoding="utf-8")
            if (progress.get("next_chapter") != chapter or meta.get("stage") not in {"state_extracted", "reviewed"}
                or text_sha(text) != proof["translation_sha256"]
                or any(meta.get(k) != v for k,v in receipt["checkpoint"].items())
                or meta.get(MARKER, {}).get("proof") != proof):
                raise RuntimeError("pending disposition saved checkpoint drift")
        resolved.append(disposition["disposition_id"])
        resolved_journals.add((original_path.name, original["operation_id"], "uncertain"))
    remaining = [item for item in inventory["uncertain"]
                 if (item["file"], item["operation_id"], item["status"]) not in resolved_journals]
    return {**inventory, "uncertain": remaining, "operator_zero_usage_dispositions": resolved}
