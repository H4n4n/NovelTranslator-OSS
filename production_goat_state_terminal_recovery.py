"""Explicit GOAT terminal-state recovery; never installed by ordinary production."""
import copy
import json
from pathlib import Path
from production_paid_request_journal import request_identity, _sha256_json, load_matching_journal, paid_request_journal_path
from production_accounting import validate_goat_stats, validate_progress_accounting, TOTAL_FIELDS
from production_goat_cap_recovery import accepted_envelope
from production_goat_state_policy_migration import text_sha
from benchmark_longitudinal_provider import build_provider_request_identity
from production_state_extraction import parse_state_response, StateExtractionError

REVISION = "goat-state-terminal-response-recovery-v1"
KIND = "state_extraction_terminal_recovery"
MODE = "goat_state_terminal_response_recovery"
MARKER = "state_terminal_recovery"
RECEIPTS = "terminal_state_accounting_receipts"


def journal_identity(proof, wire):
    policy = proof["provider_policy"]
    result = request_identity(operation_kind=KIND, novel_id=proof["novel_id"],
        chapter=proof["chapter"], provider=policy["provider"], model=policy["wire_model"],
        profile_id=proof["profile_id"], source_sha256=proof["source_sha256"],
        translation_sha256=proof["translation_sha256"],
        request_payload={"wire_request": wire, "provider_policy": policy, "terminal_state_recovery": proof})
    result.update(provider_policy=copy.deepcopy(policy),
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256,
        terminal_state_recovery=copy.deepcopy(proof))
    return result


def accounting_receipt(record, envelope):
    validate_goat_stats(envelope["stats"])
    return dict(revision=REVISION, operation_id=record["operation_id"],
        operation_kind=record["operation_kind"], request_sha256=record["request_sha256"],
        wire_request_sha256=record["wire_request_sha256"],
        raw_response_sha256=_sha256_json(envelope["raw_response"]),
        accepted_envelope_sha256=_sha256_json(envelope), stats=copy.deepcopy(envelope["stats"]))


def verify_settlement(progress, record, envelope):
    validate_progress_accounting(progress, expected_provider="command_code_provider")
    expected = accounting_receipt(record, envelope)
    receipts = progress.get(RECEIPTS, {})
    if not isinstance(receipts, dict):
        raise RuntimeError("terminal accounting receipts malformed")
    receipt = receipts.get(record["operation_id"])
    ids = progress.get("accounted_api_operation_ids", [])
    if not isinstance(ids, list) or ids.count(record["operation_id"]) > 1:
        raise RuntimeError("terminal accounting operation registry malformed")
    accounted = record["operation_id"] in ids
    if accounted != (receipt is not None) or (receipt is not None and receipt != expected):
        raise RuntimeError("terminal usage settlement conflict")
    if accounted:
        fields = {"prompt_tokens":"total_prompt_tokens", "completion_tokens":"total_completion_tokens",
            "cache_hit_tokens":"total_cache_hit_tokens", "cache_miss_tokens":"total_cache_miss_tokens",
            "cost_usd":"estimated_cost_usd", "rate_correct_cost_usd":"rate_correct_cost_usd"}
        for saved in receipts.values():
            validate_goat_stats(saved.get("stats", {}))
            if saved.get("operation_id") not in ids:
                raise RuntimeError("terminal accounting receipt lacks registry ID")
        if any(progress.get(total, 0) + 1e-12 < sum(r["stats"][field] for r in receipts.values())
               for field, total in fields.items()):
            raise RuntimeError("terminal accounting totals contradict settlement receipts")
    return accounted


def settle(module, record, envelope):
    """Detached atomic progress update: receipt and once-only ID share one marker."""
    from noveltranslator_core import add_accounting_totals_once
    if verify_settlement(module.progress, record, envelope):
        return False
    staged = copy.deepcopy(module.progress)
    if not add_accounting_totals_once(staged, record["operation_id"], envelope["stats"]):
        raise RuntimeError("terminal accounting ID conflict")
    staged.setdefault(RECEIPTS, {})[record["operation_id"]] = accounting_receipt(record, envelope)
    allowed = set(TOTAL_FIELDS) | {"accounted_api_operation_ids", RECEIPTS}
    if {k:v for k,v in staged.items() if k not in allowed} != {
            k:v for k,v in module.progress.items() if k not in allowed}:
        raise RuntimeError("terminal settlement attempted non-accounting mutation")
    module.atomic_write_json(module.PROGRESS_FILE, staged)
    module.progress.clear()
    module.progress.update(staged)
    return True


def install_terminal_state_recovery(module, plan):
    """Only a freshly authorized locked executor installs this scoped adapter."""
    from production_goat_runtime import normalize_production_request
    proof = copy.deepcopy(plan["terminal_recovery_proof"])
    chapter = proof["chapter"]
    path = Path(module.PENDING_DIR) / f"{chapter:04d}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    if MARKER not in meta:
        if text_sha(path.read_bytes().decode("utf-8")) != proof["original_pending_file_sha256"]:
            raise RuntimeError("terminal recovery pending bytes drift")
        meta[MARKER] = {"proof": proof, "original_pending_metadata": copy.deepcopy(meta),
            "original_pending_json": path.read_bytes().decode("utf-8")}
        module.atomic_write_json(path, meta)
    elif meta[MARKER]["proof"] != proof:
        raise RuntimeError("terminal recovery saved proof drift")
    module._goat_terminal_state_proof = proof
    module._goat_terminal_state_phase = "state"
    original = load_matching_journal(proof["original_journal_path"], proof["original_identity"])
    envelope = accepted_envelope(original["response"], plan["recovery_wire"])
    if envelope != original["response"] or original["response_sha256"] != proof["original_response_sha256"]:
        raise RuntimeError("terminal recovery original envelope drift")
    settle(module, original, envelope)

    def extract(chapter_number, source, translation, *, prior_continuity=None):
        if (chapter_number != chapter or text_sha(source) != proof["source_sha256"]
            or text_sha(translation) != proof["translation_sha256"]
            or _sha256_json(prior_continuity) != proof["continuity_sha256"]):
            raise RuntimeError("terminal recovery extraction inputs drift")
        kwargs = dict(model=module.MODEL, messages=[
            {"role":"system", "content":module.STATE_PROMPT},
            {"role":"user", "content":module.state_extraction_user_prompt(chapter=chapter,
                existing_terms=list(module.glossary["entries"]), prior=prior_continuity,
                source=source, translation=translation)}],
            **module.operation_request_controls("state_extraction"))
        if normalize_production_request("state_extraction", kwargs) != plan["recovery_wire"]:
            raise RuntimeError("terminal recovery exact original wire drift")
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
                        raise RuntimeError("terminal failed-response acceptance drift")
                    known.append((record, normalized))
                raise
        def repair(content, ceiling):
            body = dict(model=module.MODEL, messages=[
                {"role":"system", "content":module.GENERIC_JSON_REPAIR_PROMPT +
                    "\n\nRequired structure:\n" + module.STATE_SCHEMA_HINT},
                {"role":"user", "content":"Repair this malformed JSON:\n\n" + content}],
                **module.operation_request_controls("state_json_repair", ceiling=ceiling))
            module._goat_terminal_state_phase = "json_repair"
            return paid("state_json_repair", body, content)
        try:
            result = paid(KIND, kwargs, translation)
            state, stats = parse_state_response(envelope=result["response"], stats=result["response"]["stats"],
                repair=repair, clean_text=module.clean_json_text, salvage=module.salvage_state_json_without_issues,
                combine_stats=module.combine_stats, emit=lambda event, **fields: module.log_event(event, chapter=chapter, **fields),
                policy=module.STATE_EXTRACTION_POLICY)
            module._goat_terminal_state_known = known
            return state, module.combine_stats(*(e["stats"] for r, e in known
                if not verify_settlement(module.progress, r, e)))
        except BaseException:
            for record, accepted in known:
                settle(module, record, accepted)
            raise
    module.extract_state = extract


def save_terminal_state_checkpoint(module, plan):
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
        for record, envelope in getattr(module, "_goat_terminal_state_known", []):
            settle(module, record, envelope)
        raise
    return result["status"]
