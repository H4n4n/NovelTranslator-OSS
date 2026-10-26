"""Authorized GOAT execution through the existing real production lifecycle.

Only called after the trial/recovery executor has rebuilt and verified its exact
plan under the novel lock. Imports and planning never activate credentials.
"""
import sys

import noveltranslator_core as core
from novel_project import load_novel_project
from production_goat_runtime import GoatBudget
from production_provider_runtime import provider_identity, inspect_pending_provider
from production_provider_contract import PROVIDER_COMMAND_CODE
from translation_model_config import resolve_translation_model


def execute_authorized_goat(plan, *, repo_root, recovery, saturation_recovery=False, glossary_review_recovery=False,
        state_policy_migration=False, terminal_state_recovery=False, uncertain_disposition=False):
    # Refuse missing uncapped exposure before credential activation, including
    # invocations which would first send a capped translation.
    from production_goat_auxiliary_policy import completion_exposure
    completion_exposure({"model": "deepseek/deepseek-v4.1-flash", "reasoning_effort": "low"}, prompt_upper=1)
    from controlled_trial_execute import (
        _capture_core_runtime, _restore_core_runtime, _working_directory,
        _snapshot_mutable_tree, _post_run_report,
    )
    from pending_recovery_execute import _post_report
    from run_production_engine import (
        prepare_production_runtime, load_production_module, PRODUCTION_SCRIPT,
        PRODUCTION_MODULE_NAME, activated_goat_transport,
    )
    model = plan["auxiliary_model"] if recovery else plan["requested_model"]
    config = resolve_translation_model(model)
    expected = provider_identity(PROVIDER_COMMAND_CODE, config.model)
    if plan["provider_identity"] != expected or plan["budget_unit"] != expected["pricing_unit"]:
        raise RuntimeError("GOAT authorization provider/accounting drift before credentials")
    project = load_novel_project(plan["novel_id"], repo_root=repo_root)
    if plan.get("mode") == "bounded_goat_unattended":
        from production_run_lock import held_production_run_lock
        held_production_run_lock(project.root)
    pending_identity = plan["migration_proof"]["old_policy"] if state_policy_migration else expected
    inspect_pending_provider(project.pending_dir, pending_identity)
    budget = GoatBudget(plan["max_api_calls"], plan["max_credit_equivalent_usd"])
    before = _snapshot_mutable_tree(project)
    core_state = _capture_core_runtime()
    missing = object()
    previous = sys.modules.get(PRODUCTION_MODULE_NAME, missing)
    error = None
    generation_attempted = False
    state_generation_attempted = False
    state_checkpoint_saved = False

    def forbidden_translation(*args, **kwargs):
        nonlocal generation_attempted
        generation_attempted = True
        raise RuntimeError("translation generation forbidden in GOAT paid-pending recovery")

    try:
        with _working_directory(project.root):
            prepared = prepare_production_runtime(config.model, project.pending_dir,
                core_module=core, provider_selection=PROVIDER_COMMAND_CODE,
                pending_provider_identity=pending_identity if state_policy_migration else None)
            if prepared["provider_identity"] != expected:
                raise RuntimeError("GOAT prepared provider identity drift")
            if recovery and prepared["runtime"]["pending_chapter"] != plan["target_chapter"]:
                raise RuntimeError("GOAT pending chapter drift before credentials")
            module = load_production_module(PRODUCTION_SCRIPT)
            module.initialize_production_runtime_state(activate_provider=False, provider_backend=PROVIDER_COMMAND_CODE)
            if module.progress.get("next_chapter") != plan["target_chapter"]:
                raise RuntimeError("GOAT target chapter drift before credentials")
            if recovery:
                module.translate_chapter = forbidden_translation
            if uncertain_disposition:
                from uncertain_request_disposition_plan import build_uncertain_request_disposition_plan
                current = build_uncertain_request_disposition_plan(plan["novel_id"], repo_root=repo_root,
                    operator_evidence=plan["operator_evidence"], max_api_calls=plan["max_api_calls"],
                    max_credit_equivalent_usd=plan["max_credit_equivalent_usd"])
                if (current["state_fingerprint"] != plan["state_fingerprint"]
                    or current["approval_token"] != plan["approval_token"]):
                    raise RuntimeError("uncertain disposition drift before credentials")
            if terminal_state_recovery:
                from goat_state_terminal_recovery_plan import build_goat_state_terminal_recovery_plan
                current = build_goat_state_terminal_recovery_plan(plan["novel_id"], repo_root=repo_root,
                    max_api_calls=plan["max_api_calls"], max_credit_equivalent_usd=plan["max_credit_equivalent_usd"])
                if (current["state_fingerprint"] != plan["state_fingerprint"]
                    or current["approval_token"] != plan["approval_token"]):
                    raise RuntimeError("terminal state recovery plan drift before credentials")
            if state_policy_migration:
                from goat_state_policy_migration_plan import build_goat_state_policy_migration_plan
                current = build_goat_state_policy_migration_plan(plan["novel_id"], repo_root=repo_root,
                    max_api_calls=plan["max_api_calls"], max_credit_equivalent_usd=plan["max_credit_equivalent_usd"])
                if (current["state_fingerprint"] != plan["state_fingerprint"]
                    or current["approval_token"] != plan["approval_token"]
                    or current["send_authorization_status"] != "READY"):
                    raise RuntimeError("state migration plan drift before credentials")
            if glossary_review_recovery:
                from goat_glossary_review_recovery_plan import build_goat_glossary_review_recovery_plan
                current_plan = build_goat_glossary_review_recovery_plan(plan["novel_id"], repo_root=repo_root,
                    max_api_calls=plan["max_api_calls"], max_credit_equivalent_usd=plan["max_credit_equivalent_usd"])
                if (current_plan["state_fingerprint"] != plan["state_fingerprint"]
                    or current_plan["approval_token"] != plan["approval_token"]):
                    raise RuntimeError("GOAT glossary review plan drift before credentials")
                def forbidden_state(*args, **kwargs):
                    nonlocal state_generation_attempted
                    state_generation_attempted = True
                    raise RuntimeError("state extraction generation forbidden in glossary recovery")
                module.extract_state = forbidden_state
                module._goat_glossary_recovery_only = True
            if saturation_recovery:
                from goat_saturation_recovery_plan import build_goat_saturation_recovery_plan
                current_plan = build_goat_saturation_recovery_plan(plan["novel_id"], repo_root=repo_root,
                    max_api_calls=plan["max_api_calls"], max_credit_equivalent_usd=plan["max_credit_equivalent_usd"])
                if (current_plan["state_fingerprint"] != plan["state_fingerprint"]
                    or current_plan["approval_token"] != plan["approval_token"]):
                    raise RuntimeError("GOAT saturation plan drift before credentials")
            if plan.get("mode") == "bounded_goat_unattended":
                from production_run_lock import held_production_run_lock
                held_production_run_lock(project.root)
                # Consume once, durably, before credential activation. Even a
                # zero-send stop needs a newly planned/operator-approved run.
                import json
                import os
                receipts = project.data_dir / "goat_run_authorizations"
                receipts.mkdir(exist_ok=True)
                with (receipts / (plan["approval_token"] + ".json")).open("x", encoding="utf-8") as handle:
                    json.dump(plan, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
            # All plan/state/provider/budget/target checks precede this scope.
            with activated_goat_transport(module, expected, budget=budget):
                if uncertain_disposition:
                    from production_uncertain_request_disposition import install_uncertain_recovery
                    install_uncertain_recovery(module, plan)
                if terminal_state_recovery:
                    from production_goat_state_terminal_recovery import install_terminal_state_recovery, save_terminal_state_checkpoint
                    install_terminal_state_recovery(module, plan)
                if state_policy_migration:
                    from production_goat_state_policy_migration import install_state_policy_migration
                    install_state_policy_migration(module, plan)
                if uncertain_disposition:
                    from production_uncertain_request_disposition import save_uncertain_state_checkpoint
                    save_uncertain_state_checkpoint(module, plan)
                    state_checkpoint_saved = True
                elif terminal_state_recovery:
                    save_terminal_state_checkpoint(module, plan)
                    state_checkpoint_saved = True
                elif saturation_recovery:
                    # Dedicated route does no startup audit before translation recovery.
                    result = module.process_chapter(plan["target_chapter"])
                    if result is not True:
                        error = "GOAT saturation recovery stopped safely without commit"
                elif recovery:
                    if plan.get("recovery_mode") == "reconcile_reviewed_partial_commit_tail":
                        result = module.reconcile_reviewed_partial_commit_tail(plan["target_chapter"])
                    else:
                        result = module.process_chapter(plan["target_chapter"])
                    if result is not True:
                        error = "GOAT pending recovery stopped safely without commit"
                else:
                    module.run_unattended_production(**({} if plan.get("mode") == "bounded_goat_unattended"
                        else {"target_chapter": plan["target_chapter"]}))
    except (Exception, KeyboardInterrupt, SystemExit):
        # Provider/SDK failures may contain credential-bearing bodies. Safe fixed
        # diagnostics accompany durable journal evidence; never stringify them.
        error = "GOAT authorized execution stopped; inspect durable journal/local QA"
    finally:
        _restore_core_runtime(core_state)
        if previous is missing:
            sys.modules.pop(PRODUCTION_MODULE_NAME, None)
        else:
            sys.modules[PRODUCTION_MODULE_NAME] = previous
    report = (_post_report(project, plan, config, budget, before,
                           translation_generation_attempted=generation_attempted, run_error=error)
              if recovery else _post_run_report(project, plan, config, budget, before, error))
    report["unaccounted_provider_response"] = (budget.unaccounted_provider_response
        or any(record["status"] != "success" for record in budget.records))
    if terminal_state_recovery or uncertain_disposition:
        report.update(ordinary_state_extraction_resend_allowed=False,
            terminal_state_recovery_new_send_limit=1, startup_periodic_audits_allowed=False,
            state_checkpoint_saved=state_checkpoint_saved)
        if state_checkpoint_saved and not report["error"] and not report["escaped_chapters"]:
            report["status"] = "STATE_CHECKPOINT_SAVED"
    if uncertain_disposition:
        report.update(maximum_new_recovery_sends=1, disposition_id=plan["disposition_id"],
            original_provider_usage="zero", original_journal_preserved=True)
    report["accepted_provider_responses"] = budget.response_records
    report.update(provider=PROVIDER_COMMAND_CODE, provider_identity=expected,
        pricing_version=expected["pricing_version"], pricing_unit=expected["pricing_unit"],
        accounting=expected["accounting"], provider_invoice_claimed=False,
        max_credit_equivalent_usd=budget.max_cost_usd,
        actual_credit_equivalent_usd=budget.actual_cost_usd,
        scheduler_mode="provider_unrestricted", real_credential_source="command_code_dpapi")
    if glossary_review_recovery:
        report.update(state_extraction_generation_allowed=False,
            state_extraction_generation_attempted=state_generation_attempted)
    if state_policy_migration:
        report.update(state_policy_migration_revision=plan["migration_proof"]["revision"],
            translation_generation_allowed=False, startup_periodic_audits_allowed=False,
            recovery_generation_send_limit=1)
    return report
