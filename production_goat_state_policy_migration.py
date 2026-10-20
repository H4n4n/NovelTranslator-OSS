"""One new production state generation after definitive old-policy LENGTHs.

Only the dedicated locked executor may install this adapter. The existing
translation and domain pipeline remain authoritative; no translation, startup
audit, JSON repair, or ordinary state recovery can replace this fixed generation.
"""
import copy
import hashlib
import json

from production_paid_request_journal import request_identity, _sha256_json
from benchmark_longitudinal_provider import build_provider_request_identity
from production_state_extraction import classify_state_response, validate_state, StateExtractionError

REVISION = "goat-state-auxiliary-policy-migration-v1"
KIND = "state_extraction_auxiliary_policy_migration_v1"
REASON = "auxiliary_completion_policy_migration_after_definitive_length"
MARKER = "state_auxiliary_policy_migration"


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def journal_identity(proof, wire):
    result = request_identity(operation_kind=KIND, novel_id=proof["novel_id"],
        chapter=proof["chapter"], provider=proof["new_policy"]["provider"],
        model=proof["new_policy"]["wire_model"], profile_id=proof["profile_id"],
        source_sha256=proof["source_sha256"], translation_sha256=proof["translation_sha256"],
        request_payload={"wire_request": wire, "provider_policy": proof["new_policy"],
            "state_policy_migration": proof})
    result.update(provider_policy=copy.deepcopy(proof["new_policy"]),
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256,
        state_policy_migration=copy.deepcopy(proof))
    return result


def install_state_policy_migration(module, plan):
    """Bind checkpoint replacement explicitly, preserving its exact old bytes.

    Must run after new GOAT runtime installation and before process_chapter.
    Both an existing PLANNED slot and definitive success may replay this one
    generation. Failure/ambiguity cannot authorize another generation.
    """
    from pathlib import Path
    from production_goat_runtime import normalize_production_request
    proof = copy.deepcopy(plan["migration_proof"])
    chapter = proof["chapter"]
    path = Path(module.PENDING_DIR) / f"{chapter:04d}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    translation = path.with_suffix(".md").read_text(encoding="utf-8")
    source = module.chapter_map[chapter]["text"]
    if (text_sha(source) != proof["source_sha256"] or text_sha(translation) != proof["translation_sha256"]
        or module.progress["next_chapter"] != chapter
        or module.continuity["latest_chapter"] != chapter - 1):
        raise RuntimeError("state migration drift before checkpoint binding")
    if MARKER not in meta:
        if _sha256_json(meta) != proof["original_pending_metadata_sha256"]:
            raise RuntimeError("state migration pending metadata drift")
        original_json = path.read_bytes().decode("utf-8")
        if text_sha(original_json) != proof["original_pending_file_sha256"]:
            raise RuntimeError("state migration original pending file bytes drift")
        meta[MARKER] = {"proof": proof, "original_pending_metadata": copy.deepcopy(meta),
            "original_pending_json": original_json}
        # This is an explicitly authorized revision, never implicit replay of
        # the saved 8K checkpoint under the new wire policy.
        meta["state_checkpoint_identity"] = module.current_state_checkpoint_identity(
            chapter, source, translation, prior_continuity=module.continuity)
        module.atomic_write_json(path, meta)
    elif meta[MARKER]["proof"] != proof:
        raise RuntimeError("state migration proof drift")
    module._goat_state_migration_proof = proof
    module._goat_state_migration_phase = "state_only"

    def extract(chapter_number, chinese_text, paid_text, *, prior_continuity=None):
        if (chapter_number != chapter or text_sha(chinese_text) != proof["source_sha256"]
            or text_sha(paid_text) != proof["translation_sha256"]
            or _sha256_json(prior_continuity) != proof["continuity_sha256"]):
            raise RuntimeError("state migration exact extraction inputs drift")
        kwargs = dict(model=module.MODEL, messages=[
            {"role": "system", "content": module.STATE_PROMPT},
            {"role": "user", "content": module.state_extraction_user_prompt(
                chapter=chapter, existing_terms=list(module.glossary["entries"]),
                prior=prior_continuity, source=chinese_text, translation=paid_text)}],
            **module.operation_request_controls("state_extraction"))
        if normalize_production_request("state_extraction", kwargs) != plan["recovery_wire"]:
            raise RuntimeError("state migration wire drift")
        result = module.journaled_chat_request(KIND, chapter, chinese_text, kwargs, translation_text=paid_text)
        envelope = result["response"]
        if classify_state_response(envelope, module.clean_json_text) != "visible_state":
            raise StateExtractionError("migration_generation_terminal")
        content = module.clean_json_text(envelope["content"])
        try:
            state = json.loads(content)
        except json.JSONDecodeError:
            state = module.salvage_state_json_without_issues(content)
        validate_state(state)
        module._goat_state_migration_phase = "post_state"
        # Old LENGTH responses were accepted/charged before content rejection.
        # Carry all three distinct state operation totals exactly once into the
        # ordinary checkpoint accounting; no inferred historical billing.
        stats = module.combine_stats(*proof["old_state_stats"], envelope["stats"])
        return state, stats

    module.extract_state = extract
    if meta.get("stage") in {"state_extracted", "reviewed"}:
        module._goat_state_migration_phase = "post_state"
