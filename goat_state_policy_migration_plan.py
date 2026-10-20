"""Read-only, zero-provider production state-policy migration planner."""
import argparse
import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project
from pending_recovery_plan import _read_json, _sha256_file, _recovery_runtime_identity, _immutable_inputs
from production_paid_request_journal import (
    _sha256_json, request_identity, load_matching_journal, paid_request_journal_path)
from production_goat_state_policy_migration import REVISION, KIND, REASON, MARKER, text_sha, journal_identity
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_provider_runtime import provider_identity, historical_capped_goat_provider_identity, inspect_pending_provider
from production_goat_runtime import normalize_production_request, GoatBudget
from production_goat_policy import bind_auxiliary_checkpoint
from production_goat_cap_recovery import accepted_envelope
from benchmark_longitudinal_provider import build_provider_request_identity
from production_saved_checkpoint_identity import require_saved_checkpoint_identity
from production_accounting import validate_progress_accounting, validate_goat_stats
from translation_profile import load_translation_profile, load_novel_terminology_policy, profile_state_instructions

MODE = "goat_state_auxiliary_policy_migration_plan"


def project_snapshot(project):
    # All novel-owned inputs and production artifacts; exclude only run lock.
    return {path.relative_to(project.root).as_posix(): _sha256_file(path)
        for path in sorted(project.root.rglob("*")) if path.is_file() and path.name != ".production.lock"}


def definitions(project):
    """Load inert definitions only, never initialize an engine/client/credential."""
    spec = importlib.util.spec_from_file_location("_state_migration_definitions",
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
    return module


def inspect_target(project):
    progress = _read_json(project.progress_file, "progress")
    validate_progress_accounting(progress, expected_provider=PROVIDER_COMMAND_CODE)
    chapter = progress.get("next_chapter")
    if (type(chapter) is not int or chapter < 1 or progress.get("last_completed") != chapter - 1
        or progress.get("completed") != list(range(1, chapter)) or chapter > project.config.final_chapter
        or project.pause_file.exists() or (project.chapter_dir / f"{chapter:04d}.md").exists()):
        raise RuntimeError("state migration progress/target is not an unfinished sequential chapter")
    for suffix in (".md", ".json"):
        if {p.stem for p in project.pending_dir.glob("*" + suffix) if p.stem.isdigit()} != {f"{chapter:04d}"}:
            raise RuntimeError("state migration requires exact one pending pair")
    path = project.pending_dir / f"{chapter:04d}.json"
    meta = _read_json(path, "pending")
    marker = meta.get(MARKER)
    original = marker.get("original_pending_metadata") if isinstance(marker, dict) else meta
    original_json = marker.get("original_pending_json") if isinstance(marker, dict) else path.read_bytes().decode("utf-8")
    if not isinstance(original_json, str) or json.loads(original_json) != original:
        raise RuntimeError("state migration original pending byte snapshot drift")
    translation = path.with_suffix(".md").read_text(encoding="utf-8")
    if (not isinstance(original, dict) or original.get("chapter") != chapter
        or original.get("stage") != "translated" or "state" in original or "review_result" in original
        or original.get("local_qa", {}).get("commit_allowed") is not True or not translation.strip()):
        raise RuntimeError("state migration requires QA-accepted paid translated pending")
    old = historical_capped_goat_provider_identity()
    new = provider_identity(PROVIDER_COMMAND_CODE, "v41-flash")
    if old == new or new.get("auxiliary_completion_policy") != "provider_unbounded":
        raise RuntimeError("state migration policy drift")
    inspect_pending_provider(project.pending_dir, old)
    for key in original:
        if key.startswith("translation_") or key in {"chapter", "local_qa"}:
            if original[key] != meta.get(key):
                raise RuntimeError("state migration original pending provenance drift")
    continuity = _read_json(project.continuity_file, "continuity")
    if continuity.get("latest_chapter") != chapter - 1:
        raise RuntimeError("state migration continuity drift")
    sources = index_chapter_library(_read_json(project.chapters_file, "chapters"),
        project.config.final_chapter, chapter_numbering=project.config.chapter_numbering)
    source = sources[chapter]["text"]
    module = definitions(project)
    checkpoint = module.current_state_checkpoint_identity(chapter, source, translation, prior_continuity=continuity)
    require_saved_checkpoint_identity(saved=original.get("state_checkpoint_identity"),
        expected=bind_auxiliary_checkpoint(checkpoint, old), label="old state extraction")
    if marker:
        require_saved_checkpoint_identity(saved=meta.get("state_checkpoint_identity"),
            expected=bind_auxiliary_checkpoint(checkpoint, new), label="migration state extraction")
    messages = [{"role": "system", "content": module.STATE_PROMPT},
        {"role": "user", "content": module.state_extraction_user_prompt(chapter=chapter,
            existing_terms=list(module.glossary["entries"]), prior=continuity, source=source, translation=translation)}]
    kwargs = dict(model="deepseek-flash", messages=messages, **module.operation_request_controls("state_extraction"))
    wire = normalize_production_request("state_extraction", kwargs)
    old_wire = {**wire, "max_completion_tokens": 8000}
    journals = project.data_dir / "provider_request_journal"
    from production_uncertain_request_disposition import effective_paid_request_inventory
    inspection = effective_paid_request_inventory(project)
    if inspection["errors"] or inspection["uncertain"]:
        raise RuntimeError("state migration ambiguous/corrupt journals block")
    records = []
    for kind in ("state_extraction", "state_extraction_recovery"):
        expected = request_identity(operation_kind=kind, novel_id=project.config.id, chapter=chapter,
            provider=PROVIDER_COMMAND_CODE, model=old["wire_model"], profile_id=project.config.profile,
            source_sha256=text_sha(source), translation_sha256=text_sha(translation),
            request_payload={"wire_request": old_wire, "provider_policy": old})
        expected.update(provider_policy=old,
            wire_request_sha256=build_provider_request_identity(request_body=old_wire).request_sha256)
        journal = paid_request_journal_path(journals, kind, chapter)
        record = load_matching_journal(journal, expected)
        if not record or record["status"] != "response_recorded":
            raise RuntimeError("state migration requires both definitive old RESPONSE_RECORDED slots")
        if record["operation_id"] in progress.get("accounted_api_operation_ids", []):
            raise RuntimeError("state migration old state already cumulatively accounted")
        envelope = record["response"]
        validate_goat_stats(envelope.get("stats", {}))
        if (envelope.get("finish_reason") != "length"
            or envelope.get("provider_identity") != old
            or envelope.get("stats", {}).get("completion_tokens") != 8000
            or accepted_envelope(envelope, old_wire, provider_policy=old) != envelope):
            raise RuntimeError("state migration old response acceptance/identity is not exact capped LENGTH")
        records.append(dict(identity=expected, record_sha256=record["record_sha256"],
            response_sha256=record["response_sha256"], journal_sha256=_sha256_file(journal),
            stats=copy.deepcopy(envelope["stats"])))
    translation_record = load_matching_journal(paid_request_journal_path(journals, "chapter_translation", chapter), {})
    if (translation_record["response"].get("finish_reason") != "stop"
        or translation_record["response"].get("provider_identity") != old
        or translation_record["response"].get("provider_response_model") != old["wire_model"]):
        raise RuntimeError("state migration translation is not an accepted paid STOP")
    proof = dict(revision=REVISION, reason=REASON, novel_id=project.config.id, chapter=chapter,
        profile_id=project.config.profile, source_sha256=text_sha(source), translation_sha256=text_sha(translation),
        original_pending_metadata_sha256=_sha256_json(original),
        original_pending_file_sha256=text_sha(original_json),
        translation_journal_record_sha256=translation_record["record_sha256"],
        translation_journal_response_sha256=translation_record["response_sha256"],
        old_state_journals=records, old_state_stats=[r["stats"] for r in records],
        old_policy=old, new_policy=new, old_policy_sha256=_sha256_json(old), new_policy_sha256=_sha256_json(new),
        continuity_sha256=_sha256_json(continuity), progress_sha256=_sha256_file(project.progress_file),
        recovery_wire_sha256=build_provider_request_identity(request_body=wire).request_sha256,
        max_generations=1, translation_generation_allowed=False, startup_periodic_audits_allowed=False)
    if marker and marker.get("proof") != proof:
        raise RuntimeError("state migration saved proof/source/pending/progress/continuity/policy drift")
    expected = journal_identity(proof, wire)
    recovered = load_matching_journal(paid_request_journal_path(journals, KIND, chapter), expected)
    if recovered and recovered["status"] in {"started", "uncertain"}:
        raise RuntimeError("state migration generation is ambiguous; no resend")
    if recovered and recovered["status"] == "response_recorded":
        from production_state_extraction import validate_state
        envelope = accepted_envelope(recovered["response"], wire)
        if envelope != recovered["response"] or envelope["finish_reason"] != "stop":
            raise RuntimeError("state migration generation terminal; no second generation")
        try:
            state = json.loads(module.clean_json_text(envelope["content"]))
        except json.JSONDecodeError:
            state = module.salvage_state_json_without_issues(module.clean_json_text(envelope["content"]))
        validate_state(state)
    return dict(chapter=chapter, proof=proof, wire=wire, recovery_identity=expected, recovered=recovered)


def build_goat_state_policy_migration_plan(novel_id, *, repo_root=None, max_api_calls, max_credit_equivalent_usd):
    project = load_novel_project(novel_id, repo_root=repo_root)
    before = project_snapshot(project)
    target = inspect_target(project)
    budget = GoatBudget(max_api_calls, max_credit_equivalent_usd)
    from production_goat_auxiliary_policy import completion_exposure
    try:
        exposure = completion_exposure(target["wire"], prompt_upper=len(json.dumps(target["wire"], ensure_ascii=False).encode("utf-8")) + 8192)
        with budget.authorize(target["wire"], datetime.now(timezone.utc)):
            pass
        authorization = "READY"
    except ValueError:
        exposure = None
        authorization = "HOLD_FOR_ARCHITECTURE_REVIEW"
    # GoatRuntimeError is a RuntimeError, and its pre-send refusal is intentionally
    # propagated for insufficient credits; missing metadata is detected above.
    state = dict(mode=MODE, revision=REVISION, files=before,
        immutable_inputs=_immutable_inputs(project), runtime=_recovery_runtime_identity(project.config.profile),
        proof=target["proof"], recovery_identity=target["recovery_identity"],
        max_api_calls=max_api_calls, max_credit_equivalent_usd=budget.max_cost_usd, exposure=exposure)
    fingerprint = _sha256_json(state)
    if before != project_snapshot(project):
        raise RuntimeError("state migration project drift during read-only plan")
    provider = target["proof"]["new_policy"]
    return dict(mode=MODE, status="READY", send_authorization_status=authorization,
        api_calls=0, credential_reads=0, project_writes=0, paid_execution_authorized=False,
        protected_state_unchanged=True, state_fingerprint=fingerprint,
        approval_token=_sha256_json({"state": fingerprint, "mode": MODE}),
        novel_id=novel_id, target_chapter=target["chapter"], requested_model="deepseek-flash",
        auxiliary_model="deepseek-flash", provider=PROVIDER_COMMAND_CODE, provider_identity=provider,
        max_api_calls=max_api_calls, max_credit_equivalent_usd=budget.max_cost_usd, max_cost_usd=budget.max_cost_usd,
        budget_unit=provider["pricing_unit"], translation_generation_allowed=False,
        startup_periodic_audits_allowed=False, recovery_generation_send_limit=1,
        recovery_generation_sends_remaining=0 if target["recovered"] and target["recovered"]["status"] == "response_recorded" else 1,
        migration_proof=target["proof"], recovery_wire=target["wire"], recovery_identity=target["recovery_identity"],
        exposure=exposure, pending_stage="translated", recovery_mode="finish_existing_pending")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--max-credit-equivalent-usd", type=float, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    plan = build_goat_state_policy_migration_plan(args.novel, repo_root=args.repo_root,
        max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["send_authorization_status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
