"""Read-only exact recorded original glossary-review empty STOP planner.

No Provider calls, credential reads, production initialization or project writes.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from chapter_library import index_chapter_library
from chapter_glossary_review import (
    CHAPTER_REVIEW_RECOVERY_KEY, CHAPTER_REVIEW_POLICY_VERSION,
    chapter_review_input_hash, chapter_review_operation_ids,
)
from glossary_integrity import canonicalize_glossary_review_candidates
from model_candidate_ids import build_model_candidate_id_map, model_facing_candidates, MODEL_CANDIDATE_ID_TRANSPORT_VERSION
from glossary_candidate_resolution import prepare_glossary_review_candidates, candidate_resolution_path
from novel_project import load_novel_project, repository_root
from pending_recovery_plan import _read_json, _sha256_file, _canonical_hash, _recovery_runtime_identity, _immutable_inputs
from production_paid_request_journal import load_matching_journal, paid_request_journal_path
from production_goat_glossary_review_recovery import (
    KIND, REVISION, STRATEGY, policy_sha256, initial_kind, journal_identity, prove_initial, recovery_proof, pending_binding,
)
from production_goat_runtime import normalize_production_request, GoatBudget
from production_goat_cap_recovery import accepted_envelope
from production_provider_runtime import provider_identity, inspect_pending_provider
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_uncertain_request_disposition import effective_paid_request_inventory
from production_path_preflight import run_production_path_preflight
from production_saved_checkpoint_identity import _finalize_identity, require_saved_checkpoint_identity
from translation_profile import (
    load_translation_profile, load_novel_terminology_policy, profile_state_instructions,
    profile_glossary_review_instructions, project_low_value_terms,
)

MODE = "goat_glossary_review_reasoning_empty_recovery_plan"


def project_snapshot(project):
    return {str(path.relative_to(project.root)): _sha256_file(path)
        for root in (project.data_dir, project.root / "output", project.source_dir)
        for path in sorted(root.rglob("*")) if path.is_file()}


def recorded_attempt_present(project):
    progress = _read_json(project.progress_file, "progress")
    path = project.pending_dir / f"{progress['next_chapter']:04d}.json"
    if not path.is_file():
        return False
    record = _read_json(path, "pending").get(CHAPTER_REVIEW_RECOVERY_KEY, {})
    return isinstance(record, dict) and record.get("status") == "original_started"


def inspect_target(project):
    progress = _read_json(project.progress_file, "progress")
    chapter = progress["next_chapter"]
    if project.pause_file.exists() or chapter in progress.get("completed", []) or chapter > project.config.final_chapter:
        raise RuntimeError("glossary recovery target is paused or completed")
    if (project.chapter_dir / f"{chapter:04d}.md").exists():
        raise RuntimeError("glossary recovery target already committed")
    for suffix in (".md", ".json"):
        if {p.stem for p in project.pending_dir.glob("*" + suffix) if p.stem.isdigit()} != {f"{chapter:04d}"}:
            raise RuntimeError("glossary recovery requires exact one pending pair")
    meta = _read_json(project.pending_dir / f"{chapter:04d}.json", "pending")
    translation = (project.pending_dir / f"{chapter:04d}.md").read_text(encoding="utf-8")
    if (meta.get("stage") != "state_extracted" or not isinstance(meta.get("state"), dict)
        or "review_result" in meta or not translation.strip()
        or meta.get("local_qa", {}).get("commit_allowed") is not True):
        raise RuntimeError("glossary recovery requires QA-approved pending and saved state")
    provider = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    inspect_pending_provider(project.pending_dir, provider)
    journals = project.data_dir / "provider_request_journal"
    inspection = effective_paid_request_inventory(project)
    if inspection["errors"] or inspection["uncertain"]:
        raise RuntimeError("ambiguous or corrupt journals forbid glossary recovery")
    chapters = index_chapter_library(_read_json(project.chapters_file, "chapters"),
        project.config.final_chapter, project.config.chapter_numbering)
    source = chapters[chapter]["text"]
    # Import-safe definitions only: NEVER call initialize_production_runtime_state.
    spec = importlib.util.spec_from_file_location("_goat_glossary_plan_definitions",
        Path(__file__).with_name("translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MODEL = "deepseek-flash"
    module.ACTIVE_NOVEL_CONFIG = project.config
    module.ACTIVE_PROFILE = load_translation_profile(project.config.profile, Path(__file__).parent)
    module.ACTIVE_NOVEL_POLICY = load_novel_terminology_policy(module.ACTIVE_PROFILE, project.translation_addendum_file)
    module.glossary = _read_json(project.glossary_file, "glossary")
    module.STATE_PROMPT = (module.STATE_PROMPT_BASE + profile_state_instructions(
        module.ACTIVE_PROFILE, module.ACTIVE_NOVEL_POLICY)).strip()
    review_prompt = (module.TERM_REVIEW_PROMPT_BASE + profile_glossary_review_instructions(
        module.ACTIVE_PROFILE, module.ACTIVE_NOVEL_POLICY)).strip()
    module.TERM_REVIEW_MODEL_PROMPT = review_prompt.replace('"candidate_id":"source:..."',
        '"candidate_id":"c01"') + module.TERM_REVIEW_MODEL_PROMPT_SUFFIX
    prior_continuity = _read_json(project.continuity_file, "continuity")
    if prior_continuity.get("latest_chapter") != progress.get("last_completed"):
        raise RuntimeError("glossary recovery continuity does not match committed progress")
    state_identity = module.current_state_checkpoint_identity(
        chapter, source, translation, prior_continuity=prior_continuity)
    from production_goat_policy import bind_auxiliary_checkpoint
    require_saved_checkpoint_identity(saved=meta.get("state_checkpoint_identity"),
        expected=bind_auxiliary_checkpoint(state_identity, provider), label="saved state extraction")
    from production_state_extraction import validate_state
    validate_state(meta["state"])
    prepared = prepare_glossary_review_candidates(novel_id=project.config.id, chapter=chapter,
        state=meta["state"], source_text=source, translation=translation, profile=module.ACTIVE_PROFILE,
        existing_terms=module.glossary["entries"],
        resolution_path=candidate_resolution_path(project.data_dir, chapter),
        low_value_terms=project_low_value_terms(project.config.id))
    if _canonical_hash(prepared["diagnostics"]) != _canonical_hash(meta.get("candidate_preparation")):
        raise RuntimeError("saved candidate preparation drift")
    candidates = canonicalize_glossary_review_candidates(prepared["candidates"])["candidates"]
    ids = [item["candidate_id"] for item in candidates]
    input_hash = chapter_review_input_hash(candidates, module.glossary["entries"].keys(), source, translation)
    domain = meta.get(CHAPTER_REVIEW_RECOVERY_KEY, {})
    operations = chapter_review_operation_ids(project.config.id, chapter, input_hash)
    from auxiliary_response_reconciliation import original_review_attempt_marker
    if (domain.get("policy_version") != CHAPTER_REVIEW_POLICY_VERSION
        or domain.get("model_id_transport_version") != MODEL_CANDIDATE_ID_TRANSPORT_VERSION
        or domain.get("input_hash") != input_hash or domain.get("candidate_ids") != ids
        or domain.get("operation_ids") != operations or domain.get("status") != "original_started"
        or original_review_attempt_marker(domain.get("original", {}))
            != {"attempted": True, "operation_id": operations["original"]}):
        raise RuntimeError("original-started domain checkpoint exact proof failed")
    id_map = build_model_candidate_id_map(ids)
    metadata = dict(chapter=chapter, checkpoint_binding=pending_binding(meta), input_hash=input_hash, operation_id=operations["original"],
        model_candidate_ids=list(id_map.alias_to_internal))
    kwargs = module.build_original_glossary_review_request(chapter_number=chapter, chinese_text=source,
        translation=translation, model_candidates=model_facing_candidates(candidates, id_map), metadata=metadata,
        existing_relevant=module.glossary_to_text(module.relevant_glossary(source), chapter_number=chapter),
        system_prompt=module.TERM_REVIEW_MODEL_PROMPT, model="deepseek-flash",
        controls=module.operation_request_controls("chapter_glossary_review"))
    wire = normalize_production_request("glossary_review", kwargs)
    source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    expected = journal_identity(kind=initial_kind(wire), novel_id=project.config.id, chapter=chapter,
        profile_id=project.config.profile, source_sha256=source_sha, wire=wire)
    path = paid_request_journal_path(journals, initial_kind(wire), chapter)
    initial = prove_initial(load_matching_journal(path, expected), wire, expected)
    binding = domain["original"].get("transport_binding")
    if binding is not None:
        if (not isinstance(binding, dict) or set(binding) != {"journal_path", "identity"}
            or Path(binding["journal_path"]).resolve() != path.resolve()
            or binding["identity"] != {**expected,
                "auxiliary_domain_binding": initial.get("auxiliary_domain_binding")}
            or initial.get("auxiliary_domain_binding", {}).get("operation_id") != operations["original"]):
            raise RuntimeError("original chapter-review transport binding mismatch")
        expected = binding["identity"]
    proof = recovery_proof(initial, wire, expected, metadata=metadata, translation=translation, journal_path=path)
    recovery_identity = journal_identity(kind=KIND, novel_id=project.config.id, chapter=chapter,
        profile_id=project.config.profile, source_sha256=source_sha, wire=wire, proof=proof)
    recovered = load_matching_journal(paid_request_journal_path(journals, KIND, chapter), recovery_identity)
    if recovered and recovered["status"] == "response_recorded":
        envelope = accepted_envelope(recovered["response"], wire)
        if envelope["finish_reason"] != "stop" or not envelope["content"].strip():
            raise RuntimeError("recorded glossary recovery is terminal")
    return dict(chapter=chapter, wire=wire, initial=initial, recovered=recovered,
        recovery_identity=recovery_identity, proof=proof)


def build_goat_glossary_review_recovery_plan(novel_id, *, repo_root=None, max_api_calls, max_credit_equivalent_usd):
    project = load_novel_project(novel_id, repo_root=repo_root)
    before = project_snapshot(project)
    preflight = run_production_path_preflight(novel_id, repo_root=repo_root, model_selection="v41-flash",
        provider_selection=PROVIDER_COMMAND_CODE)
    if preflight["status"] != "PASS" or preflight["api_calls"] or preflight["project_writes"]:
        if preflight.get("provider_manual_review_required") is True:
            from production_provider_runtime import HistoricalGoatManualReviewRequired
            raise HistoricalGoatManualReviewRequired()
        raise RuntimeError("production preflight rejected recorded glossary recovery")
    target = inspect_target(project)
    budget = GoatBudget(max_api_calls, max_credit_equivalent_usd)
    remaining = 0 if target["recovered"] and target["recovered"]["status"] == "response_recorded" else 1
    if remaining:
        with budget.authorize(target["wire"], target["initial"]["response"]["request_started_utc"]):
            pass
    provider = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    state = dict(mode=MODE, project_root=str(project.root.resolve()), files=before,
        immutable_inputs=_immutable_inputs(project), runtime=_recovery_runtime_identity(project.config.profile),
        proof=target["proof"], recovery_identity=target["recovery_identity"],
        policy_revision=REVISION, policy_sha256=policy_sha256(),
        max_api_calls=max_api_calls, max_credit_equivalent_usd=budget.max_cost_usd)
    fingerprint = _canonical_hash(state)
    if project_snapshot(project) != before:
        raise RuntimeError("project drift during read-only glossary recovery planning")
    return dict(mode=MODE, status="READY", api_calls=0, credential_reads=0, project_writes=0,
        paid_execution_authorized=False, protected_state_unchanged=True, novel_id=novel_id,
        project_root=str(project.root), target_chapter=target["chapter"], requested_model="deepseek-flash",
        auxiliary_model="deepseek-flash", provider=PROVIDER_COMMAND_CODE, provider_identity=provider,
        budget_unit=provider["pricing_unit"], max_api_calls=max_api_calls, max_cost_usd=budget.max_cost_usd,
        max_credit_equivalent_usd=budget.max_cost_usd, allow_peak=False, scheduler_mode="provider_unrestricted",
        translation_generation_allowed=False, state_extraction_generation_allowed=False,
        startup_periodic_audits_allowed=False, glossary_review_recovery_sends_remaining=remaining,
        pending_stage="state_extracted", recovery_mode="finish_existing_pending",
        initial_operation_id=target["initial"]["operation_id"],
        initial_response_sha256=target["initial"]["response_sha256"],
        recovery_operation_id=target["recovery_identity"]["operation_id"],
        initial_wire_sha256=target["proof"]["initial_wire_sha256"],
        recovery_wire_sha256=target["proof"]["recovery_wire_sha256"],
        recovery_controls={**{key: target["wire"][key] for key in ("model", "reasoning_effort")},
            "auxiliary_completion_policy": "provider_unbounded", "auxiliary_completion_tokens": None},
        state_binding=state, state_fingerprint=fingerprint,
        approval_token=_canonical_hash(dict(mode=MODE, state_fingerprint=fingerprint, state=state)),
        recommended_entrypoint="goat_glossary_review_recovery_execute.py")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--max-credit-equivalent-usd", type=float, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        plan = build_goat_glossary_review_recovery_plan(args.novel, repo_root=args.repo_root,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd)
    except Exception:
        print(json.dumps(dict(status="REFUSED", api_calls=0, credential_reads=0, project_writes=0,
            error="Exact recorded glossary recovery proof failed")))
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
