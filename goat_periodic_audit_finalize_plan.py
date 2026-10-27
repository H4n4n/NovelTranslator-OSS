"""Read-only, credential-free proof for completed GOAT periodic audit publication."""
import argparse
import copy
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import glossary_audit as audit
from chapter_library import index_chapter_library
from model_candidate_ids import build_model_candidate_id_map, model_facing_candidates, MODEL_CANDIDATE_ID_TRANSPORT_VERSION
from novel_project import load_novel_project, repository_root
from production_accounting import canonical_sha256, combine_goat_stats, validate_progress_accounting
from production_paid_request_journal import load_matching_journal, request_identity
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_provider_runtime import provider_identity
from production_goat_runtime import normalize_production_request
from production_goat_cap_recovery import accepted_envelope
from benchmark_longitudinal_provider import build_provider_request_identity
from production_periodic_audit_finalization import (
    require, file_sha, read_json, authoritative_audit_stats, prepare_finalization,
    publication_timestamp, load_intent, verify_publication, state_paths,
    transaction_proof, intent_path,
    json_bytes,
)
from translation_profile import load_translation_profile, load_novel_terminology_policy, profile_audit_instructions
from run_production_engine import load_production_module

MODE = "finalize_completed_periodic_audit"


def blocked_send(*args, **kwargs):
    raise RuntimeError("local audit finalization provider-send tripwire")


def local_module(project):
    """Load policy/source without the runtime initializer's writes or fixes."""
    module = load_production_module()
    for name, path in dict(CHAPTERS_FILE=project.chapters_file, GLOSSARY_FILE=project.glossary_file,
        PROGRESS_FILE=project.progress_file, CONTINUITY_FILE=project.continuity_file,
        ISSUES_FILE=project.issues_file, AUDIT_HISTORY_FILE=project.audit_history_file,
        OUTPUT_DIR=project.chapter_dir, PENDING_DIR=project.pending_dir, AUDIT_DIR=project.audit_dir,
        RETROACTIVE_DIR=project.retroactive_dir, BACKUP_DIR=project.backup_dir,
        PAID_REQUEST_JOURNAL_DIR=project.data_dir / "provider_request_journal").items():
        setattr(module, name, path)
    module.ACTIVE_NOVEL_CONFIG = project.config
    module.ACTIVE_PROFILE = load_translation_profile(project.config.profile, repository_root())
    module.ACTIVE_NOVEL_POLICY = load_novel_terminology_policy(module.ACTIVE_PROFILE, project.translation_addendum_file)
    module.MODEL = "deepseek-flash"
    module.chapters = read_json(project.chapters_file)
    module.chapter_map = index_chapter_library(module.chapters, project.config.final_chapter, project.config.chapter_numbering)
    for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
        setattr(module, name, read_json(state_paths(module)[key]))
    module.continuity = read_json(project.continuity_file)
    module.AUDIT_PROMPT = (module.AUDIT_PROMPT_BASE + profile_audit_instructions(module.ACTIVE_PROFILE, module.ACTIVE_NOVEL_POLICY)).strip()
    module.AUDIT_MODEL_PROMPT = module.AUDIT_PROMPT.replace('"candidate_id":"source:..."', '"candidate_id":"c01"') + module.AUDIT_MODEL_PROMPT_SUFFIX
    numeric = module.combine_stats
    module.combine_stats = lambda *items: combine_goat_stats(numeric, *items)
    # No provider activation, credentials, initialization, logging or client.
    module.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=blocked_send)))
    for name in ("call_api_with_retries", "journaled_chat_request", "request_periodic_audit_chunk",
                 "repair_periodic_audit_chunk", "correct_periodic_audit_chunk_semantics", "process_chapter",
                 "initialize_production_runtime_state", "run_unattended_production"):
        setattr(module, name, blocked_send)
    return module


def runtime_identity(project):
    engine = repository_root()
    files = list(engine.glob("*.py")) + list((engine / "profiles" / project.config.profile).rglob("*")) + [engine / "pyproject.toml"]
    manifest = {p.relative_to(engine).as_posix(): file_sha(p) for p in sorted(files) if p.is_file()}
    def git(ref):
        return subprocess.check_output(["git", "-C", str(engine), "rev-parse", ref], text=True).strip()
    target_git = {}
    if (project.repository_root / ".git").exists():
        for key, ref in (("head", "HEAD"), ("tree", "HEAD^{tree}")):
            target_git[key] = subprocess.check_output(["git", "-C", str(project.repository_root), "rev-parse", ref], text=True).strip()
    return dict(head=git("HEAD"), tree=git("HEAD^{tree}"), production_checkout=target_git,
                runtime_manifest=manifest, runtime_sha256=canonical_sha256(manifest))


def checkpoint_proof(module, start, end):
    """Prove paid leaves locally using native GOAT or the installed recorded experiment owner."""
    from experimental_goat_audit_proof import owner_for
    owner = owner_for(module)
    cp_path = module.periodic_audit_checkpoint_path(start, end)
    checkpoint = read_json(cp_path)
    targets, integrity = module.audit_targets(start, end, glossary_data=module.glossary, return_integrity=True)
    config = audit.AuditChunkConfig(max_candidates=module.MAX_AUDIT_CANDIDATES_PER_CHUNK,
        max_serialized_chars=module.MAX_AUDIT_CHUNK_SERIALIZED_CHARS)
    index = module.compact_glossary_index(allowed_source_keys=integrity["safe_glossary_keys"], glossary_data=module.glossary)
    context = hashlib.sha256((module.AUDIT_PROMPT + "\n" + index).encode()).hexdigest()
    candidate_hash = audit.audit_candidate_set_hash(targets, config, context)
    require(checkpoint.get("status") == "completed" and checkpoint.get("schema_version") == audit.AUDIT_CHECKPOINT_SCHEMA_VERSION
            and checkpoint.get("policy_version") == config.policy_version and checkpoint.get("config") == asdict(config)
            and checkpoint.get("candidate_set_hash") == candidate_hash
            and checkpoint.get("audit_window") == dict(start_chapter=start, end_chapter=end), "completed audit checkpoint identity drift")
    leaves = [c for c in checkpoint["chunks"] if c["status"] != "split"]
    require(leaves and all(c["status"] == "completed" for c in leaves), "audit leaves are not completed")
    ids = [t["candidate_id"] for t in sorted(targets, key=lambda t: t["chinese"])]
    leaf_ids = [i for c in leaves for i in c["candidate_ids"]]
    require(len(leaf_ids) == len(set(leaf_ids)) and set(leaf_ids) == set(ids), "audit leaf candidate inventory mismatch")
    chunks_by_id = {c["chunk_id"]: c for c in checkpoint["chunks"]}
    require(len(chunks_by_id) == len(checkpoint["chunks"]), "duplicate audit chunk identity")
    initial = audit.build_audit_chunks(targets, config, all_glossary_keys=integrity["safe_glossary_keys"], glossary_entries=module.glossary["entries"])
    queue, topology = list(initial), set()
    while queue:
        expected_chunk = queue.pop(0)
        actual = chunks_by_id.get(expected_chunk["chunk_id"])
        require(actual is not None and all(actual.get(k) == expected_chunk.get(k) for k in
                ("chunk_id", "candidate_ids", "family_keys", "depth", "parent_chunk_id")), "audit chunk topology drift")
        topology.add(actual["chunk_id"])
        require(actual["status"] in {"split", "completed"}, "nonterminal audit chunk")
        if actual["status"] == "split":
            require(actual["depth"] < config.max_split_depth and len(actual["candidate_ids"]) > 1, "invalid audit subdivision")
            queue.extend(audit._split_chunk(expected_chunk))
    require(topology == set(chunks_by_id), "unexpected audit chunk")
    if owner is not None:
        owner.start(checkpoint, initial)
    policy = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash") if owner is None else None
    target_by_id = {t["candidate_id"]: t for t in targets}
    sources = {t["candidate_id"]: t["chinese"] for t in targets}
    families = audit.resolve_audit_candidate_families(targets, all_glossary_keys=integrity["safe_glossary_keys"], glossary_entries=module.glossary["entries"])["members_by_candidate_id"]
    records = checkpoint["attempt_records"]
    journals = []
    parsed_by_chunk = {}
    operation_ids = set()
    for position, attempt in enumerate(records):
        require("carried_from_superseded_checkpoint" not in attempt, "superseded audit needs separate historical proof")
        chunk = chunks_by_id.get(attempt.get("chunk_id"))
        require(chunk is not None and attempt.get("kind") in {"audit", "json_repair", "semantic_correction"}
                and type(attempt.get("attempt")) is int and attempt["attempt"] >= 1, "invalid paid audit attempt")
        metadata = attempt["request_metadata"]
        require(metadata.get("audit_window") == checkpoint["audit_window"] and metadata.get("candidate_set_hash") == candidate_hash,
                "audit request metadata drift")
        id_map = build_model_candidate_id_map(chunk["candidate_ids"])
        require(attempt.get("model_id_transport_version") == metadata.get("model_id_transport_version")
                == MODEL_CANDIDATE_ID_TRANSPORT_VERSION, "audit alias transport drift")
        request_candidates = model_facing_candidates([target_by_id[i] for i in chunk["candidate_ids"]], id_map)
        family_context = []
        for internal, candidate in zip(chunk["candidate_ids"], request_candidates):
            family = copy.deepcopy(families[internal])
            family["candidate_id"] = id_map.internal_to_alias[internal]
            candidate.update({k: family[k] for k in ("source_key", "family_id", "family_root", "alias_of")})
            family_context.append(family)
        kind = attempt["kind"]
        prior = [r for r in records[:position] if r["chunk_id"] == chunk["chunk_id"] and r["kind"] in {"audit", "json_repair"}]
        original_parsed, model_errors = None, None
        require(metadata.get("candidate_ids") == chunk["candidate_ids"] and metadata.get("model_candidate_ids") == list(id_map.alias_to_internal), "audit request candidate drift")
        if kind == "audit":
            require(metadata.get("family_context") == family_context, "audit request family drift")
            kwargs = module.build_request_periodic_audit_chunk(start, end, index, request_candidates, metadata)
            require(attempt["operation_id"] == audit._audit_attempt_operation_id(candidate_hash, chunk["chunk_id"], attempt["attempt"], kind), "audit attempt operation ID drift")
        elif kind == "json_repair":
            if owner is not None:
                require(metadata.get("family_context") == family_context, "experimental repair source family drift")
            require(prior and prior[-1]["kind"] == "audit", "audit JSON repair lacks original response")
            require(attempt["operation_id"] == audit._audit_attempt_operation_id(candidate_hash, chunk["chunk_id"], attempt["attempt"], kind), "audit repair operation ID drift")
            kwargs = module.build_repair_periodic_audit_chunk(prior[-1]["raw_content"], metadata)
        else:
            require(prior and "parsed_result" in prior[-1], "semantic correction lacks original parsed paid result")
            original_parsed = prior[-1]["parsed_result"]
            resolved = audit._resolve_request_local_candidate_ids(original_parsed, id_map)
            try:
                audit.validate_audit_chunk_result(resolved, chunk["candidate_ids"], integrity["safe_glossary_keys"], sources)
            except audit.AuditResultValidationError as error:
                require(error.retryable, "semantic correction original is not retryable")
                model_errors = [error.as_dict()]
                internal = model_errors[0]["candidate_id"]
                model_errors[0]["candidate_id"] = id_map.internal_to_alias.get(internal, internal)
            else:
                raise RuntimeError("semantic correction has no original validation error")
            authorization = audit.audit_semantic_correction_authorization(model_errors)
            invalid = set(authorization)
            correction_input = dict(decisions=[copy.deepcopy(d) for d in original_parsed["decisions"] if d["candidate_id"] in invalid],
                                    issues=copy.deepcopy(original_parsed.get("issues", [])))
            expected_context = [c for c in family_context if c["candidate_id"] in invalid]
            expected_sources = {i: sources[id_map.alias_to_internal[i]] for i in sorted(invalid)}
            require(metadata.get("family_context") == expected_context and metadata.get("candidate_sources") == expected_sources
                    and metadata.get("authorized_fields") == {i: sorted(f) for i, f in authorization.items()}
                    and metadata.get("allowed_merge_targets") == {i: sorted(k for k in integrity["safe_glossary_keys"] if k != expected_sources[i]) for i in sorted(invalid)},
                    "semantic correction metadata drift")
            require(attempt["attempt"] == 1 and attempt["operation_id"] == f"periodic_glossary_audit_semantic_correction:{candidate_hash}:{chunk['chunk_id']}:semantic-fields-v1", "semantic correction operation ID drift")
            kwargs = module.build_correct_periodic_audit_chunk_semantics(correction_input, model_errors, metadata)
        if owner is not None:
            identity, path, journal, envelope = owner.accept(attempt, kwargs)
        else:
            wire = normalize_production_request("periodic_audit", kwargs)
            wire_sha = build_provider_request_identity(request_body=wire).request_sha256
            transport_kind = "context_periodic_audit_" + wire_sha
            identity = request_identity(operation_kind=transport_kind, novel_id=module.ACTIVE_NOVEL_CONFIG.id,
                chapter=end, provider=PROVIDER_COMMAND_CODE, model=policy["wire_model"], profile_id=module.ACTIVE_PROFILE.id,
                source_sha256=hashlib.sha256(module.chapter_map[end]["text"].encode()).hexdigest(),
                request_payload=dict(wire_request=wire, provider_policy=policy))
            identity.update(provider_policy=policy, wire_request_sha256=wire_sha,
                auxiliary_domain_binding={**{k: copy.deepcopy(attempt[k]) for k in ("chunk_id", "kind", "attempt", "operation_id", "model_id_transport_version")}, "request_metadata": metadata})
            path = module.PAID_REQUEST_JOURNAL_DIR / f"{end:04d}.{transport_kind}.json"
            binding = attempt.get("transport_binding", {})
            saved_path = Path(binding.get("journal_path", ""))
            if not saved_path.is_absolute():
                saved_path = module.PROGRESS_FILE.resolve().parent.parent / saved_path
            require(binding.get("identity") == identity and saved_path.resolve() == path.resolve(), "paid audit transport binding drift")
            journal = load_matching_journal(path, identity)
            require(journal and journal["status"] == "response_recorded", "audit journal is not terminal RESPONSE_RECORDED")
        require(identity["operation_id"] not in operation_ids, "duplicate paid audit component")
        operation_ids.add(identity["operation_id"])
        if owner is None:
            envelope = journal["response"]
            require(accepted_envelope(envelope, wire) == envelope, "paid audit raw response/accounting mismatch")
        require(attempt.get("stats") == envelope["stats"] and attempt.get("raw_content") == envelope["content"]
                and attempt.get("finish_reason") == envelope["finish_reason"], "audit attempt/paid journal mismatch")
        require(hashlib.sha256(attempt["raw_content"].encode()).hexdigest() == attempt.get("raw_content_hash"), "audit response content hash mismatch")
        if "parsed_result" in attempt:
            require(audit._attempt_result_hash(attempt["parsed_result"]) == attempt.get("result_hash")
                    and json.loads(module.clean_json_text(attempt["raw_content"])) == attempt["parsed_result"], "audit parsed response hash mismatch")
        if attempt.get("lifecycle") == "validated":
            effective = attempt["parsed_result"]
            if kind == "semantic_correction":
                effective, merge_proof = audit.merge_audit_semantic_correction(original_parsed, effective, model_errors)
                require(attempt.get("merge_audit") == merge_proof, "semantic correction merge proof drift")
            resolved = audit._resolve_request_local_candidate_ids(effective, id_map)
            parsed_by_chunk[chunk["chunk_id"]] = audit.validate_audit_chunk_result(resolved, chunk["candidate_ids"], integrity["safe_glossary_keys"], sources)
            if kind == "semantic_correction":
                require(attempt.get("effective_result_hash") == audit._attempt_result_hash(parsed_by_chunk[chunk["chunk_id"]]), "corrected audit result hash drift")
        elif chunk.get("deterministic_normalization") and "parsed_result" in attempt and kind in {"audit", "json_repair"}:
            resolved = audit._resolve_request_local_candidate_ids(attempt["parsed_result"], id_map)
            try:
                audit.validate_audit_chunk_result(resolved, chunk["candidate_ids"], integrity["safe_glossary_keys"], sources)
            except audit.AuditResultValidationError as error:
                errors = [error.as_dict()]
                errors[0]["candidate_id"] = id_map.internal_to_alias[error.candidate_id]
                normalized = audit.try_deterministic_glossary_semantic_normalization(attempt["parsed_result"], errors, request_candidates)
                require(normalized is not None, "saved local normalization cannot be reproduced")
                effective, normalization = normalized
                result = audit.validate_audit_chunk_result(audit._resolve_request_local_candidate_ids(effective, id_map), chunk["candidate_ids"], integrity["safe_glossary_keys"], sources)
                normalization.update(candidate_id=error.candidate_id, model_candidate_id=errors[0]["candidate_id"], effective_result_hash=audit._attempt_result_hash(result))
                require(normalization == chunk["deterministic_normalization"], "local normalization proof drift")
                parsed_by_chunk[chunk["chunk_id"]] = result
            else:
                raise RuntimeError("saved normalization has no validation error")
        journals.append(dict(path=str(path), journal_sha256=file_sha(path), record=journal))
    for leaf in leaves:
        require(parsed_by_chunk.get(leaf["chunk_id"]) == leaf.get("result"), "audit leaf result lacks exact validated paid response")
    combined = audit.combine_audit_chunk_results(checkpoint["chunks"], ids, integrity["safe_glossary_keys"])
    require(combined == checkpoint.get("combined_result"), "combined audit result drift")
    run = dict(checkpoint=checkpoint, stats=audit._combine_attempt_stats(records))
    stats = (owner.finish(records) if owner is not None else
             authoritative_audit_stats(run, module.combine_stats, provider=PROVIDER_COMMAND_CODE))
    chunking = dict(policy_version=config.policy_version, candidate_set_hash=candidate_hash, candidate_count=len(targets),
        total_chunks=len(leaves), completed_chunks=[c["chunk_id"] for c in leaves], api_request_count=len(records),
        resumed=True, checkpoint_file=str(cp_path), source_integrity=integrity, superseded_checkpoint=checkpoint.get("superseded_checkpoint", ""))
    return dict(checkpoint=checkpoint, checkpoint_sha256=file_sha(cp_path), context_fingerprint=context,
                journals=journals, audit_result=combined, audit_stats=stats, chunking=chunking)


def build_goat_periodic_audit_finalize_plan(novel_id, *, repo_root=None, start_chapter, end_chapter):
    project = load_novel_project(novel_id, repo_root=repo_root or repository_root())
    require(type(start_chapter) is int and type(end_chapter) is int and 1 <= start_chapter <= end_chapter, "invalid audit window")
    module = local_module(project)
    require(validate_progress_accounting(module.progress) == PROVIDER_COMMAND_CODE, "local finalization requires active GOAT progress")
    require(module.progress["last_completed"] == end_chapter and module.progress["next_chapter"] == end_chapter + 1
            and module.progress["completed"] == list(range(1, end_chapter + 1))
            and module.continuity["latest_chapter"] == end_chapter, "audit recovery chapter boundary drift")
    require(not any(project.pending_dir.iterdir()) if project.pending_dir.exists() else True, "audit recovery requires empty pending")
    from production_uncertain_request_disposition import effective_paid_request_inventory
    intent = load_intent(module, start_chapter, end_chapter)
    finalized = False
    if intent:
        finalized = verify_publication(module, intent)
        for key, name in (("glossary", "glossary"), ("progress", "progress"), ("issues", "issues_data"), ("history", "audit_history")):
            setattr(module, name, copy.deepcopy(intent["baseline"][key]))
    proof = checkpoint_proof(module, start_chapter, end_chapter)
    if intent:
        require(proof["audit_result"] == intent["audit_result"] and proof["audit_stats"] == intent["audit_stats"], "paid checkpoint/finalization intent drift")
        proof["chunking"] = intent["chunking"]
    timestamp = intent["record"]["timestamp_unix"] if intent else publication_timestamp(module, start_chapter, end_chapter)
    candidate = prepare_finalization(module, start_chapter, end_chapter, proof["audit_result"], proof["audit_stats"], proof["chunking"], timestamp=timestamp)
    tx_proof = transaction_proof(module, start_chapter, end_chapter, candidate["baseline"]["glossary"], candidate["post"]["glossary"], candidate["record"]["changes"])
    if intent:
        candidate["post"]["history"]["audits"][-1]["retroactive"] = intent["record"]["retroactive"]
        require(candidate["post"] == intent["post"] and candidate["transaction_plan"] == intent["transaction_plan"], "expected audit publication drift")
        candidate = intent
    else:
        require(not (project.audit_dir / f"audit_{start_chapter:04d}_{end_chapter:04d}.json").exists(), "final audit record exists without intent")
    if finalized:
        require(tx_proof["state"] and tx_proof["state"]["state"] == "FINALIZED", "final audit lacks finalized PR155 evidence")
        backup = project.backup_dir / f"checkpoint_{end_chapter:04d}"
        for key, path in state_paths(module).items():
            require(read_json(backup / path.name) == candidate["post"][key], "finalized checkpoint backup drift")
        require(file_sha(backup / project.continuity_file.name) == file_sha(project.continuity_file), "finalized continuity backup drift")
    try:
        inspection = effective_paid_request_inventory(project)
    except RuntimeError:
        # The only exception to strict inventory is a proved, identified local
        # tail. This verifies original dispositions and every lineage edge anew;
        # no ordinary inventory acceptance or runtime whitelist is installed.
        require(intent is not None and not finalized,
                "local lineage reconciliation requires exact unfinished audit intent")
        from production_paid_request_journal import inspect_paid_request_journals
        from production_uncertain_request_disposition import verified_completed_dispositions
        raw = inspect_paid_request_journals(project.data_dir / "provider_request_journal")
        inspection = verified_completed_dispositions(project, raw,
            _local_audit_window=(start_chapter, end_chapter))
    require(not inspection["errors"] and not inspection["uncertain"], "ambiguous/corrupt paid journal blocks local finalization")
    files = {str(p): file_sha(p) for p in [*state_paths(module).values(), project.continuity_file, project.chapters_file,
        project.config_file, project.translation_addendum_file, project.source_overrides_file,
        intent_path(module, start_chapter, end_chapter), project.audit_dir / f"audit_{start_chapter:04d}_{end_chapter:04d}.json"]}
    backup = project.backup_dir / f"checkpoint_{end_chapter:04d}"
    for p in backup.glob("*"):
        files[str(p)] = file_sha(p)
    binding = dict(novel_id=novel_id, start_chapter=start_chapter, end_chapter=end_chapter,
        production=runtime_identity(project), files=files, proof=proof, transaction=tx_proof, candidate=candidate,
        combined_result_sha256=canonical_sha256(proof["audit_result"]), decisions_sha256=canonical_sha256(proof["audit_result"]["decisions"]),
        aggregate_stats_sha256=canonical_sha256(proof["audit_stats"]), staged_glossary_sha256=canonical_sha256(candidate["post"]["glossary"]),
        expected_post_sha256={k: canonical_sha256(v) for k, v in candidate["post"].items()},
        expected_post_file_sha256={k: hashlib.sha256(json_bytes(v) if k == "progress" or v != candidate["baseline"][k]
            else Path(state_paths(module)[k]).read_bytes()).hexdigest() for k, v in candidate["post"].items()},
        final_audit_record_sha256=canonical_sha256(candidate["record"]))
    fingerprint = canonical_sha256(binding)
    return dict(status="ALREADY_FINALIZED" if finalized else "READY", recovery_mode=MODE,
        provider_calls_required=0, translation_generation_allowed=False, chapter_generation_allowed=False,
        audit_provider_generation_allowed=False, state_binding=binding, state_fingerprint=fingerprint,
        approval_token=canonical_sha256(dict(mode=MODE, fingerprint=fingerprint)),
        recommended_entrypoint="goat_periodic_audit_finalize_execute.py")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--start-chapter", type=int, required=True)
    parser.add_argument("--end-chapter", type=int, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = build_goat_periodic_audit_finalize_plan(args.novel, repo_root=args.repo_root,
            start_chapter=args.start_chapter, end_chapter=args.end_chapter)
    except Exception as exc:
        print(json.dumps(dict(status="BLOCKED", reason=str(exc), provider_calls_required=0)))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
