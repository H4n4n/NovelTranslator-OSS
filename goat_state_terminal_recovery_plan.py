"""Read-only GOAT empty-content-filter state recovery planner. Zero transport."""
import argparse
import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from chapter_library import index_chapter_library
from novel_project import load_novel_project
from pending_recovery_plan import _read_json, _sha256_file, _recovery_runtime_identity, _immutable_inputs
from production_paid_request_journal import _sha256_json, request_identity, load_matching_journal, paid_request_journal_path
from goat_state_policy_migration_plan import definitions
from production_goat_state_policy_migration import text_sha
from production_goat_state_terminal_recovery import REVISION, KIND, MODE, MARKER, journal_identity, verify_settlement
from production_goat_runtime import normalize_production_request, GoatBudget
from production_goat_policy import bind_auxiliary_checkpoint
from production_goat_cap_recovery import accepted_envelope
from production_provider_runtime import provider_identity, inspect_pending_provider
from production_accounting import validate_progress_accounting, validate_goat_stats, combine_goat_stats
from production_saved_checkpoint_identity import require_saved_checkpoint_identity
from production_state_extraction import validate_state, parse_state_response, StateExtractionError
from benchmark_longitudinal_provider import build_provider_request_identity


def snapshot(project):
    paths = [project.progress_file, project.glossary_file, project.continuity_file,
        project.issues_file, project.audit_history_file, project.chapters_file, project.pause_file]
    for directory in (project.pending_dir, project.data_dir / "provider_request_journal",
                      project.chapter_dir, project.qa_dir):
        paths.extend(p for p in directory.glob("*") if p.is_file() and p.suffix in {".md", ".json"})
    return {str(p): _sha256_file(p) for p in sorted(set(paths))}


def identity(kind, project, chapter, source, translation, wire, policy):
    value = request_identity(operation_kind=kind, novel_id=project.config.id, chapter=chapter,
        provider=policy["provider"], model=policy["wire_model"], profile_id=project.config.profile,
        source_sha256=text_sha(source), translation_sha256=text_sha(translation),
        request_payload={"wire_request": wire, "provider_policy": policy})
    value.update(provider_policy=policy,
        wire_request_sha256=build_provider_request_identity(request_body=wire).request_sha256)
    return value


def inspect_target(project):
    policy = provider_identity("command_code_provider", "v41-flash")
    progress = _read_json(project.progress_file, "progress")
    validate_progress_accounting(progress, expected_provider="command_code_provider")
    chapter = progress.get("next_chapter")
    if (type(chapter) is not int or chapter < 1 or chapter > project.config.final_chapter
        or progress.get("last_completed") != chapter - 1
        or progress.get("completed") != list(range(1, chapter))
        or project.pause_file.exists() or (project.chapter_dir / f"{chapter:04d}.md").exists()):
        raise RuntimeError("terminal recovery requires unfinished sequential pending target")
    for suffix in (".md", ".json"):
        if {p.stem for p in project.pending_dir.glob("*" + suffix) if p.stem.isdigit()} != {f"{chapter:04d}"}:
            raise RuntimeError("terminal recovery requires exactly one paid pending pair")
    path = project.pending_dir / f"{chapter:04d}.json"
    meta = _read_json(path, "pending")
    marker = meta.get(MARKER)
    original = marker.get("original_pending_metadata") if isinstance(marker, dict) else meta
    original_json = marker.get("original_pending_json") if isinstance(marker, dict) else path.read_bytes().decode("utf-8")
    if not isinstance(original_json, str) or json.loads(original_json) != original:
        raise RuntimeError("terminal original pending snapshot drift")
    if (not isinstance(original, dict) or original.get("chapter") != chapter
        or original.get("stage") != "translated" or "state" in original or "review_result" in original
        or original.get("local_qa", {}).get("commit_allowed") is not True
        or meta.get("stage") not in {"translated", "state_extracted"}
        or any(k in meta for k in ("review_result", "review_checkpoint_identity", "chapter_glossary_review_recovery"))):
        raise RuntimeError("terminal recovery requires translated checkpoint without later review work")
    # Only this adapter's checkpoint fields may differ from original pending.
    allowed = {MARKER, "stage", "state", "state_stats", "state_checkpoint_identity"}
    if {k:v for k,v in meta.items() if k not in allowed} != {k:v for k,v in original.items() if k not in allowed}:
        raise RuntimeError("terminal pending provenance drift")
    translation = path.with_suffix(".md").read_text(encoding="utf-8")
    if not translation.strip():
        raise RuntimeError("terminal recovery requires nonempty existing translation")
    inspect_pending_provider(project.pending_dir, policy)
    continuity = _read_json(project.continuity_file, "continuity")
    if continuity.get("latest_chapter") != chapter - 1:
        raise RuntimeError("terminal recovery continuity drift")
    source = index_chapter_library(_read_json(project.chapters_file, "chapters"),
        project.config.final_chapter, chapter_numbering=project.config.chapter_numbering)[chapter]["text"]
    module = definitions(project)
    from noveltranslator_core import glossary_strict_qa_errors
    if glossary_strict_qa_errors(module.glossary.get("entries", {}), project.config.id,
                                module.ACTIVE_PROFILE.high_priority_categories):
        raise RuntimeError("terminal recovery invalid strict glossary configuration")
    qa = module.local_qa(chapter, source, translation, original.get("finish_reason"))
    if qa["commit_allowed"] is not True:
        raise RuntimeError("terminal recovery local commit QA blocked")
    checkpoint = bind_auxiliary_checkpoint(module.current_state_checkpoint_identity(
        chapter, source, translation, prior_continuity=continuity), policy)
    require_saved_checkpoint_identity(saved=original.get("state_checkpoint_identity"),
        expected=checkpoint, label="original terminal state")
    require_saved_checkpoint_identity(saved=meta.get("state_checkpoint_identity"),
        expected=checkpoint, label="current terminal state")
    wire = normalize_production_request("state_extraction", dict(model="deepseek-flash", messages=[
        {"role":"system", "content":module.STATE_PROMPT},
        {"role":"user", "content":module.state_extraction_user_prompt(chapter=chapter,
            existing_terms=list(module.glossary["entries"]), prior=continuity,
            source=source, translation=translation)}], **module.operation_request_controls("state_extraction")))
    journals = project.data_dir / "provider_request_journal"
    from production_uncertain_request_disposition import effective_paid_request_inventory
    inventory = effective_paid_request_inventory(project)
    if inventory["errors"] or inventory["uncertain"]:
        raise RuntimeError("terminal recovery ambiguous/corrupt journal inventory blocks")
    permitted = {"chapter_translation", "chapter_translation_cap_recovery_v2",
        "strict_terminology_micro_repair", "residual_cjk_micro_repair", "state_extraction", KIND, "state_json_repair"}
    target_records = [r for r in inventory["records"] if r["chapter"] == chapter]
    if any(r["chapter"] > chapter or (r["chapter"] == chapter and r["operation_kind"] not in permitted)
           for r in inventory["records"]):
        raise RuntimeError("terminal recovery later state/review work blocks")
    expected = identity("state_extraction", project, chapter, source, translation, wire, policy)
    old_path = paid_request_journal_path(journals, "state_extraction", chapter)
    record = load_matching_journal(old_path, expected)
    if not record or record["status"] != "response_recorded":
        raise RuntimeError("terminal recovery requires original RESPONSE_RECORDED")
    envelope = accepted_envelope(record["response"], wire)
    if (envelope != record["response"] or envelope["finish_reason"] != "content_filter"
        or not isinstance(envelope["content"], str) or envelope["content"].strip()):
        raise RuntimeError("terminal original must be exact accepted empty content_filter")
    accounted = verify_settlement(progress, record, envelope)
    if accounted and not marker:
        raise RuntimeError("terminal original accounted without recovery proof")
    paid = load_matching_journal(paid_request_journal_path(journals, "chapter_translation", chapter), {})
    if (not paid or paid["status"] != "response_recorded"
        or paid.get("novel_id") != project.config.id or paid.get("chapter") != chapter
        or paid.get("profile_id") != project.config.profile or paid.get("operation_kind") != "chapter_translation"
        or paid.get("source_sha256") != text_sha(source) or paid.get("provider_policy") != policy
        or paid.get("model") != policy["wire_model"]
        or paid["response"].get("provider_identity") != policy
        or paid["response"].get("provider_response_model") != policy["wire_model"]
        or paid["response"].get("finish_reason") != "stop"
        or paid["response"].get("request_sha256") != paid.get("wire_request_sha256")
        or paid["response"].get("request_sha256") != original.get("translation_request_sha256")):
        raise RuntimeError("terminal recovery paid translation journal/provenance mismatch")
    validate_goat_stats(paid["response"]["stats"])
    from benchmark_longitudinal_provider import ProviderRequestIdentity, normalize_provider_response
    from command_code_provider_transport import accept_command_code_response
    transport = ProviderRequestIdentity(provider=policy["provider"], base_url=policy["base_url"],
        endpoint_path=policy["chat_completions_path"], requested_model=policy["wire_model"],
        request_sha256=paid["wire_request_sha256"])
    accepted_translation = accept_command_code_response(transport,
        normalize_provider_response(paid["response"]["raw_response"], request_identity=transport))
    if (accepted_translation.content != paid["response"]["content"]
        or accepted_translation.finish_reason != paid["response"]["finish_reason"]
        or accepted_translation.usage.prompt_tokens != paid["response"]["stats"]["prompt_tokens"]
        or accepted_translation.usage.completion_tokens != paid["response"]["stats"]["completion_tokens"]):
        raise RuntimeError("terminal translation raw response drift")
    # The paid translation and repair chain is bound, never regenerated/resubmitted.
    proof = dict(revision=REVISION, novel_id=project.config.id, chapter=chapter, profile_id=project.config.profile,
        source_sha256=text_sha(source), translation_sha256=text_sha(translation),
        continuity_sha256=_sha256_json(continuity), provider_policy=policy,
        original_pending_file_sha256=text_sha(original_json),
        original_identity=expected, original_journal_path=str(old_path),
        original_response_sha256=record["response_sha256"], original_record_sha256=record["record_sha256"],
        original_raw_response_sha256=_sha256_json(envelope["raw_response"]),
        original_accepted_envelope_sha256=_sha256_json(envelope),
        original_stats=copy.deepcopy(envelope["stats"]), original_finish_reason="content_filter",
        original_visible_characters=0, max_generations=1)
    if marker and marker.get("proof") != proof:
        raise RuntimeError("terminal recovery saved proof drift")
    recovery_identity = journal_identity(proof, wire)
    recovered = load_matching_journal(paid_request_journal_path(journals, KIND, chapter), recovery_identity)
    if (recovery_identity["operation_id"] in progress.get("accounted_api_operation_ids", [])
        and (not recovered or recovered["status"] != "response_recorded")):
        raise RuntimeError("terminal recovery slot already consumed; missing/downgraded journal blocks resend")
    terminal = False
    known = []
    parsed_state = None
    pending_repair = False
    if recovered and recovered["status"] == "response_recorded":
        accepted = accepted_envelope(recovered["response"], wire)
        if "stats" in recovered["response"] and accepted != recovered["response"]:
            raise RuntimeError("terminal recovery recorded acceptance drift")
        verify_settlement(progress, recovered, accepted)
        known.append((recovered, accepted))
        class RepairNotRecorded(Exception):
            pass
        def replay_repair(content, ceiling):
            nonlocal pending_repair
            body = dict(model="deepseek-flash", messages=[
                {"role":"system", "content":module.GENERIC_JSON_REPAIR_PROMPT +
                    "\n\nRequired structure:\n" + module.STATE_SCHEMA_HINT},
                {"role":"user", "content":"Repair this malformed JSON:\n\n" + content}],
                **module.operation_request_controls("state_json_repair", ceiling=ceiling))
            repair_wire = normalize_production_request("state_json_repair", body)
            expected_repair = identity("state_json_repair", project, chapter, source, content, repair_wire, policy)
            repair = load_matching_journal(paid_request_journal_path(journals, "state_json_repair", chapter), expected_repair)
            if not repair or repair["status"] == "planned":
                pending_repair = True
                raise RepairNotRecorded()
            repaired = accepted_envelope(repair["response"], repair_wire)
            if "stats" in repair["response"] and repaired != repair["response"]:
                raise RuntimeError("terminal JSON repair acceptance drift")
            verify_settlement(progress, repair, repaired)
            known.append((repair, repaired))
            return dict(response=repaired, operation_id=repair["operation_id"], replayed=True)
        try:
            parsed_state, _ = parse_state_response(envelope=accepted, stats=accepted["stats"],
                repair=replay_repair, clean_text=module.clean_json_text,
                salvage=module.salvage_state_json_without_issues,
                combine_stats=lambda *items: combine_goat_stats(module.combine_stats, *items),
                emit=lambda *a, **k: None, policy=module.STATE_EXTRACTION_POLICY)
        except RepairNotRecorded:
            pass
        except StateExtractionError:
            terminal = True
    repair_record = load_matching_journal(paid_request_journal_path(journals, "state_json_repair", chapter), {})
    if repair_record and not recovered:
        raise RuntimeError("terminal recovery unexpected JSON repair slot")
    if meta["stage"] == "state_extracted":
        validate_state(meta.get("state"))
        stats = combine_goat_stats(module.combine_stats, *(e["stats"] for r,e in known
            if not verify_settlement(progress, r, e)))
        if meta.get("state") != parsed_state or meta.get("state_stats") != stats:
            raise RuntimeError("terminal saved state/usage disagrees with exact journal replay")
        if not recovered or recovered["status"] != "response_recorded" or terminal:
            raise RuntimeError("terminal saved state lacks successful recovery journal")
    return dict(chapter=chapter, proof=proof, wire=wire, recovered=recovered,
        terminal=terminal, saved=meta["stage"] == "state_extracted", accounted=accounted,
        qa=qa, inventory=inventory, progress=progress, continuity=continuity, checkpoint=checkpoint,
        translation_journal=paid, target_journals=target_records, pending_repair=pending_repair,
        failure_usage_settled=bool(known) and all(verify_settlement(progress, r,e) for r,e in known))


def build_goat_state_terminal_recovery_plan(novel_id, *, repo_root=None, max_api_calls, max_credit_equivalent_usd):
    project = load_novel_project(novel_id, repo_root=repo_root)
    before = snapshot(project)
    target = inspect_target(project)
    budget = GoatBudget(max_api_calls, max_credit_equivalent_usd)
    from production_goat_auxiliary_policy import completion_exposure
    exposure = completion_exposure(target["wire"], prompt_upper=len(json.dumps(target["wire"], ensure_ascii=False).encode("utf-8")) + 8192)
    remaining = 0 if target["recovered"] and target["recovered"]["status"] == "response_recorded" else 1
    if remaining or target["pending_repair"]:
        with budget.authorize(target["wire"], datetime.now(timezone.utc)):
            pass
    root = Path(__file__).parent
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], text=True).strip()
    binding = dict(revision=REVISION, files=before, inputs=_immutable_inputs(project),
        runtime=_recovery_runtime_identity(project.config.profile), head=head, tree=tree,
        target=target, max_api_calls=max_api_calls, max_credit_equivalent_usd=budget.max_cost_usd,
        recovery_identity=journal_identity(target["proof"], target["wire"]), exposure=exposure)
    fingerprint = _sha256_json(binding)
    if before != snapshot(project):
        raise RuntimeError("terminal recovery state drift during read-only planning")
    policy = target["proof"]["provider_policy"]
    return dict(mode=MODE, recovery_mode=MODE, status="TERMINAL" if target["terminal"] else "READY",
        send_authorization_status="READY", novel_id=novel_id, target_chapter=target["chapter"],
        api_calls=0, credential_reads=0, project_writes=0, paid_execution_authorized=False,
        provider="command_code_provider", provider_identity=policy, budget_unit=policy["pricing_unit"],
        requested_model="deepseek-flash", auxiliary_model="deepseek-flash",
        max_api_calls=max_api_calls, max_credit_equivalent_usd=budget.max_cost_usd,
        state_fingerprint=fingerprint, approval_token=_sha256_json({"mode":MODE, "state":fingerprint}),
        translation_generation_allowed=False, ordinary_state_extraction_resend_allowed=False,
        startup_periodic_audits_allowed=False, terminal_state_recovery_new_send_limit=1,
        recovery_generation_sends_remaining=remaining, terminal_recovery_proof=target["proof"],
        recovery_wire=target["wire"], recovery_identity=binding["recovery_identity"],
        original_operation_accounted=target["accounted"], saved_state=target["saved"],
        failure_usage_settled=target["failure_usage_settled"], bindings=binding)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--max-credit-equivalent-usd", type=float, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = build_goat_state_terminal_recovery_plan(args.novel, repo_root=args.repo_root,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd)
    except (RuntimeError, ValueError, KeyError):
        result = dict(status="BLOCKED", api_calls=0, credential_reads=0, project_writes=0)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
