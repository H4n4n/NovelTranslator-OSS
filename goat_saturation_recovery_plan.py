"""Read-only authorization plan for a recorded GOAT 50K cap failure.

No executor imports, credentials, Provider calls, or project writes.
"""
import argparse
import hashlib
import json
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project, repository_root
from controlled_trial_plan import (
    _read_json, _immutable_inputs, _engine_runtime_identity, _canonical_hash, _sha256_file,
)
from production_path_preflight import run_production_path_preflight
from production_paid_request_journal import load_matching_journal, paid_request_journal_path
from production_operation_policy import policy_for_model
from production_goat_runtime import normalize_production_request, GoatBudget
from production_goat_cap_recovery import (
    KIND, REVISION, STRATEGY, policy_sha256, prove_initial, recovery_binding, journal_identity,
)
from production_translation_request import build_translation_request
from production_provider_runtime import authorization_provider
from production_provider_contract import PROVIDER_COMMAND_CODE
from production_uncertain_request_disposition import effective_paid_request_inventory
from translation_profile import load_translation_profile, effective_translation_instructions
from semantic_integrity_audit import protected_state_snapshot

MODE = "goat_reasoning_saturation_recovery_plan"


class GoatSaturationPlanError(RuntimeError):
    pass


def target_initial_path(project):
    progress = _read_json(project.progress_file, "progress")
    return paid_request_journal_path(project.data_dir / "provider_request_journal",
        "chapter_translation", progress["next_chapter"])


def inspect_target(project, *, initial_only=False):
    """Prove exact current source/request/response binding without any writes."""
    progress = _read_json(project.progress_file, "progress")
    target = progress["next_chapter"]
    if project.pause_file.exists():
        raise GoatSaturationPlanError("manual pause is active")
    if target > project.config.final_chapter or target in progress.get("completed", []):
        raise GoatSaturationPlanError("saturation target is not an unfinished chapter")
    if any(path.stem.isdigit() for path in project.pending_dir.glob("*")):
        raise GoatSaturationPlanError("paid pending exists; use pending recovery")
    if (project.chapter_dir / f"{target:04d}.md").exists():
        raise GoatSaturationPlanError("saturation target has a committed output")
    journals = project.data_dir / "provider_request_journal"
    inspection = effective_paid_request_inventory(project)
    if inspection["errors"] or inspection["uncertain"]:
        raise GoatSaturationPlanError("ambiguous or corrupt paid journal state")
    chapters = index_chapter_library(_read_json(project.chapters_file, "chapters"),
        project.config.final_chapter, project.config.chapter_numbering)
    chapter = chapters[target]
    profile = load_translation_profile(project.config.profile, Path(__file__).parent)
    kwargs = build_translation_request(chapter=chapter, config=project.config, profile=profile,
        system_prompt=effective_translation_instructions(profile, project.translation_addendum_file),
        glossary_entries=_read_json(project.glossary_file, "glossary")["entries"],
        continuity=_read_json(project.continuity_file, "continuity"), model="deepseek-flash",
        controls=policy_for_model("v41-flash").translation.request_controls())
    wire = normalize_production_request("translation", kwargs)
    source_sha = hashlib.sha256(chapter["text"].encode("utf-8")).hexdigest()
    initial_identity = journal_identity(kind="chapter_translation", novel_id=project.config.id,
        chapter=target, profile_id=profile.id, source_sha256=source_sha, wire=wire)
    record = load_matching_journal(paid_request_journal_path(journals, "chapter_translation", target), initial_identity)
    prove_initial(record, wire)
    if initial_only:
        return {"initial": record}
    binding = recovery_binding(record, wire)
    recovery_identity = journal_identity(kind=KIND, novel_id=project.config.id, chapter=target,
        profile_id=profile.id, source_sha256=source_sha, wire=wire, binding=binding)
    recovered = load_matching_journal(paid_request_journal_path(journals, KIND, target), recovery_identity)
    if recovered and recovered["status"] == "response_recorded":
        from production_goat_cap_recovery import accepted_envelope
        envelope = accepted_envelope(recovered["response"], wire)
        if envelope["finish_reason"] != "stop" or not envelope["content"].strip():
            raise GoatSaturationPlanError("recorded recovery is terminal; no further translation send")
    # No auxiliary work may have started for this unfinished no-pending target.
    if any(item["chapter"] == target and item["operation_kind"] not in ("chapter_translation", KIND)
           for item in inspection["records"]):
        raise GoatSaturationPlanError("unexpected downstream journal before recovered pending")
    return dict(target_chapter=target, wire=wire, initial=record, recovery=recovered,
                initial_identity=initial_identity, recovery_identity=recovery_identity)


def _journal_snapshot(project):
    root = project.data_dir / "provider_request_journal"
    return {path.name: _sha256_file(path) for path in sorted(root.iterdir()) if path.is_file()}


def build_goat_saturation_recovery_plan(novel_id, *, repo_root=None, max_api_calls,
                                        max_credit_equivalent_usd):
    identity, allowance = authorization_provider(PROVIDER_COMMAND_CODE, "v41-flash", None,
                                                  max_credit_equivalent_usd)
    budget = GoatBudget(max_api_calls, allowance)
    project = load_novel_project(novel_id, repo_root=repo_root)
    before = protected_state_snapshot(project)
    preflight = run_production_path_preflight(novel_id, repo_root=repo_root,
        model_selection="v41-flash", provider_selection=PROVIDER_COMMAND_CODE)
    if preflight["status"] != "PASS" or preflight["api_calls"] or preflight["project_writes"]:
        raise GoatSaturationPlanError("production preflight rejected saturation recovery")
    target = inspect_target(project)
    if target["recovery"] is None or target["recovery"]["status"] == "planned":
        # Same conservative bound as runtime; refuse unexecutable allowance locally.
        with budget.authorize(target["wire"], target["initial"]["response"]["request_started_utc"]):
            pass
    state = dict(mode=MODE, project_root=str(project.root.resolve()), provider_identity=identity,
        max_api_calls=max_api_calls, max_credit_equivalent_usd=allowance,
        target_chapter=target["target_chapter"], policy_revision=REVISION,
        policy_sha256=policy_sha256(), strategy=STRATEGY,
        protected_state=before, immutable_inputs=_immutable_inputs(project),
        journals=_journal_snapshot(project), runtime=_engine_runtime_identity(project.config.profile),
        initial_identity=target["initial_identity"], recovery_identity=target["recovery_identity"],
        initial_response_sha256=target["initial"]["response_sha256"],
        committed={path.name: _sha256_file(path) for path in sorted(project.chapter_dir.glob("*.md"))},
        qa={path.name: _sha256_file(path) for path in sorted(project.qa_dir.glob("*.json"))})
    fingerprint = _canonical_hash(state)
    token = _canonical_hash(dict(mode=MODE, state_fingerprint=fingerprint, state=state))
    if protected_state_snapshot(project) != before:
        raise GoatSaturationPlanError("project drifted during read-only saturation planning")
    return dict(mode=MODE, status="READY", paid_execution_authorized=False, api_calls=0,
        credential_reads=0, project_writes=0, protected_state_unchanged=True,
        novel_id=novel_id, project_root=str(project.root), target_chapter=target["target_chapter"],
        requested_model="deepseek-flash", model_version="DeepSeek-V4.1-Flash", allow_peak=False,
        scheduler_mode="provider_unrestricted", provider=PROVIDER_COMMAND_CODE,
        provider_identity=identity, budget_unit=identity["pricing_unit"],
        max_api_calls=max_api_calls, max_credit_equivalent_usd=allowance, max_cost_usd=allowance,
        recovery_policy_revision=REVISION, recovery_policy_sha256=policy_sha256(), strategy=STRATEGY,
        initial_operation_id=target["initial"]["operation_id"],
        initial_response_sha256=target["initial"]["response_sha256"],
        recovery_operation_id=target["recovery_identity"]["operation_id"],
        recovery_request_sha256=target["recovery_identity"]["wire_request_sha256"],
        recovery_controls={key: target["wire"][key] for key in ("model", "reasoning_effort", "max_completion_tokens")},
        translation_sends_remaining=0 if target["recovery"] and target["recovery"]["status"] == "response_recorded" else 1,
        state_fingerprint=fingerprint, approval_token=token, state_binding=state,
        recommended_entrypoint="goat_saturation_recovery_execute.py")


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
        plan = build_goat_saturation_recovery_plan(args.novel, repo_root=args.repo_root,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd)
    except Exception as exc:
        print(json.dumps(dict(status="REFUSED", api_calls=0, credential_reads=0, project_writes=0, error=str(exc))))
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
