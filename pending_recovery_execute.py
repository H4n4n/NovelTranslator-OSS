"""Execute exactly one existing paid pending chapter under hard safety ceilings.

The recovery executor uses the same import-safe callable production-module
boundary as normal production. After exact authorization is revalidated, it
installs a hard provider budget and translation-generation blocker before
production runtime initialization. Normal pending recovery invokes only the
approved target through ``process_chapter``; a separately proven reviewed
partial-commit mode invokes only ``reconcile_reviewed_partial_commit_tail``.

The legacy module's ``main()`` and unattended runner are never dispatched.
The initialized production client must remain bound to the budget proxy, and
the translation-generation blocker must survive initialization; either
invariant failing causes recovery to stop before target processing.
"""

from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import sys
import json
from contextlib import nullcontext
from pathlib import Path

import noveltranslator_core as core
from controlled_trial_execute import (
    ControlledTrialBudget,
    _BudgetedCompletionsProxy,
    _capture_core_runtime,
    _changed_paths,
    _numeric_files,
    _read_progress,
    _restore_core_runtime,
    _snapshot_mutable_tree,
    _working_directory,
)
from controlled_trial_repo_env import production_api_key_environment
from novel_project import load_novel_project, repository_root
from pending_recovery_plan import build_pending_recovery_plan
from production_partial_commit_reconciliation import (
    PARTIAL_COMMIT_RECOVERY_MODE,
)
from production_run_lock import production_run_lock
from production_output_runtime import model_aware_legacy_print
from run_production_engine import (
    PRODUCTION_MODULE_NAME,
    PRODUCTION_SCRIPT,
    load_production_module,
    prepare_production_runtime,
)
from translation_model_config import resolve_translation_model


EXECUTION_MODE = "bounded_existing_pending_recovery"


class PendingRecoveryExecutionError(RuntimeError):
    pass


class _BudgetedChatProxy:
    def __init__(self, real_client, budget):
        self._real_chat = real_client.chat
        self.completions = _BudgetedCompletionsProxy(real_client, budget)

    def __getattr__(self, name):
        return getattr(self._real_chat, name)


class _BudgetedClientProxy:
    def __init__(self, real_client, budget):
        self._real_client = real_client
        self.chat = _BudgetedChatProxy(real_client, budget)

    def __getattr__(self, name):
        return getattr(self._real_client, name)


def _budgeted_openai_factory(real_factory, budget):
    """Wrap every production OpenAI client in the hard provider budget."""

    def create_client(*args, **kwargs):
        real_client = real_factory(*args, **kwargs)
        return _BudgetedClientProxy(real_client, budget)

    return create_client


def validate_recovery_authorization(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
):
    plan = build_pending_recovery_plan(
        novel_id,
        repo_root=repo_root,
        model_selection=model_selection,
        max_api_calls=max_api_calls,
        max_cost_usd=max_cost_usd,
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
        **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
    )
    if plan["state_fingerprint"] != expected_state_fingerprint:
        raise PendingRecoveryExecutionError(
            "state fingerprint does not match current production state; regenerate the read-only plan"
        )
    if plan["approval_token"] != approval_token:
        raise PendingRecoveryExecutionError(
            "approval token does not match the current bounded-recovery plan"
        )
    return plan


def _post_report(
    project,
    plan,
    config,
    budget,
    before,
    *,
    translation_generation_attempted=False,
    run_error=None,
):
    after = _snapshot_mutable_tree(project)
    try:
        progress = _read_progress(project)
    except Exception as exc:
        progress = {}
        run_error = run_error or f"cannot read post-recovery progress: {exc}"

    target = plan["target_chapter"]
    committed = _numeric_files(project.chapter_dir, ".md")
    pending = _numeric_files(project.pending_dir, ".md") | _numeric_files(
        project.pending_dir, ".json"
    )
    raw_completed = progress.get("completed", [])
    completed = raw_completed if isinstance(raw_completed, list) else []
    completed_numeric = {
        value
        for value in completed
        if isinstance(value, int) and not isinstance(value, bool)
    }
    escaped = sorted(
        number
        for number in committed | pending | completed_numeric
        if number > target
    )
    target_committed = (
        target in committed
        and target in completed_numeric
        and progress.get("last_completed") == target
        and progress.get("next_chapter") == target + 1
        and target not in pending
    )

    if translation_generation_attempted:
        status = "TRANSLATION_GENERATION_BLOCKED"
        run_error = run_error or (
            "production attempted to generate a translation instead of recovering pending"
        )
    elif escaped:
        status = "BOUNDARY_VIOLATION"
        run_error = run_error or (
            "bounded recovery escaped target chapter: " + ", ".join(map(str, escaped))
        )
    elif run_error:
        status = "FAILED_AFTER_AUTHORIZATION"
    elif target_committed:
        status = "COMMITTED"
    else:
        status = "INCOMPLETE"

    return {
        "mode": EXECUTION_MODE,
        "status": status,
        "paid_execution_authorized": True,
        "novel_id": plan["novel_id"],
        "target_chapter": target,
        "recovery_mode": plan.get("recovery_mode", "resume_pending_pipeline"),
        "auxiliary_model": config.model,
        "model_version": config.version,
        "pricing_version": config.pricing_version,
        "max_api_calls": budget.max_api_calls,
        "api_calls": budget.api_calls,
        "max_cost_usd": budget.max_cost_usd,
        "actual_cost_usd": budget.actual_cost_usd,
        "provider_calls": list(budget.records),
        "translation_generation_allowed": False,
        "translation_generation_attempted": translation_generation_attempted,
        "startup_periodic_audits_executed": False,
        "target_committed": target_committed,
        "target_pending_preserved": target in pending,
        "escaped_chapters": escaped,
        "changed_paths": _changed_paths(before, after),
        "state_fingerprint": plan["state_fingerprint"],
        "approval_token": plan["approval_token"],
        "error": run_error or "",
    }


def execute_pending_recovery(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
    authorize_paid_recovery=False,
):
    """Revalidate and execute while holding the novel lock only for paid opt-in."""
    lock = (
        production_run_lock(load_novel_project(novel_id, repo_root=repo_root).root)
        if authorize_paid_recovery else nullcontext()
    )
    with lock:
        return _execute_pending_recovery_under_lock(
            novel_id,
            repo_root=repo_root,
            model_selection=model_selection,
            max_api_calls=max_api_calls,
            max_cost_usd=max_cost_usd,
            **({"provider_selection": provider_selection} if provider_selection is not None else {}),
            **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
            expected_state_fingerprint=expected_state_fingerprint,
            approval_token=approval_token,
            authorize_paid_recovery=authorize_paid_recovery,
        )


def _execute_pending_recovery_under_lock(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
    authorize_paid_recovery=False,
):
    """Revalidate exact state and, only with explicit opt-in, finish one pending chapter."""
    plan = validate_recovery_authorization(
        novel_id,
        repo_root=repo_root,
        model_selection=model_selection,
        max_api_calls=max_api_calls,
        max_cost_usd=max_cost_usd,
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
        **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
        expected_state_fingerprint=expected_state_fingerprint,
        approval_token=approval_token,
    )
    if not authorize_paid_recovery:
        return {
            "mode": EXECUTION_MODE,
            "status": "AUTHORIZATION_REQUIRED",
            "paid_execution_authorized": False,
            **({key: plan[key] for key in ("provider", "provider_identity", "budget_unit",
                "max_credit_equivalent_usd")} if "provider" in plan else {}),
            "api_calls": 0,
            "actual_cost_usd": 0.0,
            "project_writes": 0,
            "novel_id": plan["novel_id"],
            "target_chapter": plan["target_chapter"],
            "recovery_mode": plan.get("recovery_mode", "resume_pending_pipeline"),
            "auxiliary_model": plan["auxiliary_model"],
            "translation_generation_allowed": False,
            "startup_periodic_audits_executed": False,
            "state_fingerprint": plan["state_fingerprint"],
            "approval_token": plan["approval_token"],
            "changed_paths": [],
            "error": "",
        }

    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        from production_goat_execution import execute_authorized_goat
        return execute_authorized_goat(plan, repo_root=repo_root, recovery=True)

    project = load_novel_project(novel_id, repo_root=repo_root)
    config = resolve_translation_model(plan["auxiliary_model"])
    before = _snapshot_mutable_tree(project)
    core_state = _capture_core_runtime()
    budget = ControlledTrialBudget(config, max_api_calls, max_cost_usd)
    run_error = None
    generation_attempted = False
    missing_production_module = object()
    previous_production_module = sys.modules.get(
        PRODUCTION_MODULE_NAME,
        missing_production_module,
    )

    def forbidden_translation_generation(*_args, **_kwargs):
        nonlocal generation_attempted
        generation_attempted = True
        raise PendingRecoveryExecutionError(
            "translation generation is forbidden during existing-pending recovery"
        )

    try:
        with production_api_key_environment(repo_root):
            with _working_directory(project.root):
                prepared = prepare_production_runtime(
                    config.model,
                    project.pending_dir,
                    core_module=core,
                    reporter=None,
                )
                if prepared["runtime"]["pending_chapter"] != plan["target_chapter"]:
                    raise PendingRecoveryExecutionError(
                        "runtime pending chapter changed after approval"
                    )
                if prepared["runtime"]["effective_model"] != config.model:
                    raise PendingRecoveryExecutionError(
                        "effective auxiliary model changed after approval"
                    )
                with model_aware_legacy_print(config):
                    production_module = load_production_module(PRODUCTION_SCRIPT)

                    initializer = getattr(
                        production_module,
                        "initialize_production_runtime_state",
                        None,
                    )
                    process_target = getattr(
                        production_module,
                        "process_chapter",
                        None,
                    )
                    reconcile_partial_commit = getattr(
                        production_module,
                        "reconcile_reviewed_partial_commit_tail",
                        None,
                    )
                    translation_generator = getattr(
                        production_module,
                        "translate_chapter",
                        None,
                    )
                    real_openai_factory = getattr(
                        production_module,
                        "OpenAI",
                        None,
                    )

                    if not callable(initializer):
                        raise PendingRecoveryExecutionError(
                            "production runtime initializer was not defined"
                        )
                    if not callable(process_target):
                        raise PendingRecoveryExecutionError(
                            "production process_chapter function was not defined"
                        )
                    if (
                        plan.get("recovery_mode")
                        == PARTIAL_COMMIT_RECOVERY_MODE
                        and not callable(reconcile_partial_commit)
                    ):
                        raise PendingRecoveryExecutionError(
                            "production partial-commit reconciler was not defined"
                        )
                    if not callable(translation_generator):
                        raise PendingRecoveryExecutionError(
                            "production translate_chapter function was not defined"
                        )
                    if not callable(real_openai_factory):
                        raise PendingRecoveryExecutionError(
                            "production OpenAI client factory was not defined"
                        )

                    production_module.OpenAI = _budgeted_openai_factory(
                        real_openai_factory,
                        budget,
                    )
                    production_module.translate_chapter = (
                        forbidden_translation_generation
                    )

                    initializer()

                    if (
                        getattr(production_module, "translate_chapter", None)
                        is not forbidden_translation_generation
                    ):
                        raise PendingRecoveryExecutionError(
                            "translation-generation blocker was replaced during "
                            "production initialization"
                        )

                    runtime_client = getattr(
                        production_module,
                        "client",
                        None,
                    )
                    if not isinstance(runtime_client, _BudgetedClientProxy):
                        raise PendingRecoveryExecutionError(
                            "production client escaped bounded provider proxy"
                        )

                    if (
                        getattr(production_module, "process_chapter", None)
                        is not process_target
                    ):
                        raise PendingRecoveryExecutionError(
                            "production process_chapter changed during initialization"
                        )

                    runtime_progress = getattr(
                        production_module,
                        "progress",
                        None,
                    )
                    if not isinstance(runtime_progress, dict):
                        raise PendingRecoveryExecutionError(
                            "production progress state was not initialized"
                        )
                    if (
                        runtime_progress.get("next_chapter")
                        != plan["target_chapter"]
                    ):
                        raise PendingRecoveryExecutionError(
                            "production next chapter changed before bounded "
                            "target invocation"
                        )

                    if plan.get("recovery_mode") == PARTIAL_COMMIT_RECOVERY_MODE:
                        result = reconcile_partial_commit(plan["target_chapter"])
                        if result is not True:
                            run_error = (
                                "production partial-commit reconciler returned "
                                "a safe incomplete result"
                            )
                    else:
                        result = process_target(plan["target_chapter"])
                        if result is not True:
                            run_error = (
                                "production process_chapter returned a safe "
                                "incomplete result"
                            )
    except KeyboardInterrupt:
        run_error = "operator interrupted bounded pending recovery"
    except SystemExit as exc:
        run_error = f"unexpected SystemExit during bounded pending recovery: {exc.code!r}"
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
    finally:
        _restore_core_runtime(core_state)
        if previous_production_module is missing_production_module:
            sys.modules.pop(PRODUCTION_MODULE_NAME, None)
        else:
            sys.modules[PRODUCTION_MODULE_NAME] = previous_production_module

    return _post_report(
        project,
        plan,
        config,
        budget,
        before,
        translation_generation_attempted=generation_attempted,
        run_error=run_error,
    )


def _print_human(report):
    if report.get("provider") == PROVIDER_COMMAND_CODE:
        print("Provider: command_code_provider; budget/accounting: credit-equivalent USD (no invoice claim)")
        print(json.dumps(report["provider_identity"], ensure_ascii=False, sort_keys=True))
    print("=" * 70)
    print("BOUNDED EXISTING-PENDING RECOVERY EXECUTOR")
    print("=" * 70)
    print(f"Status: {report['status']}")
    if "novel_id" in report:
        print(f"Novel: {report['novel_id']}")
    if "target_chapter" in report:
        print(f"Target chapter: {report['target_chapter']}")
    if "recovery_mode" in report:
        print(f"Recovery mode: {report['recovery_mode']}")
    print("Paid execution authorized: " + (
        "YES" if report.get("paid_execution_authorized") else "NO"
    ))
    print("Translation generation allowed: NO")
    print(
        "Startup periodic audits executed: "
        + ("YES" if report.get("startup_periodic_audits_executed") else "NO")
    )
    print(f"API calls: {report.get('api_calls', 0)}")
    if report.get("provider") == PROVIDER_COMMAND_CODE:
        print(f"Actual accounted usage: {report.get('actual_cost_usd', 0.0):.6f} credit-equivalent USD; provider_invoice_claimed=false")
        print("Unaccounted provider response: " + (
            "YES" if report.get("unaccounted_provider_response") else "NO"))
    else:
        print(f"Actual accounted cost: ${report.get('actual_cost_usd', 0.0):.6f}")
    if report.get("paid_execution_authorized"):
        print("Translation generation attempted: " + (
            "YES" if report.get("translation_generation_attempted") else "NO"
        ))
        print("Target committed: " + (
            "YES" if report.get("target_committed") else "NO"
        ))
        print("Target pending preserved: " + (
            "YES" if report.get("target_pending_preserved") else "NO"
        ))
        escaped = report.get("escaped_chapters", [])
        print("Escaped chapters: " + (
            ", ".join(map(str, escaped)) if escaped else "NONE"
        ))
        print(f"Changed paths: {len(report.get('changed_paths', []))}")
        for path in report.get("changed_paths", []):
            print(f"  {path}")
    else:
        print("No provider request was made. Explicit paid-recovery approval is still required.")
    if report.get("error"):
        print(f"Error: {report['error']}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Finish exactly one existing paid pending chapter under hard provider ceilings."
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--max-credit-equivalent-usd", type=float, default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-paid-recovery", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = execute_pending_recovery(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            max_api_calls=args.max_api_calls,
            max_cost_usd=args.max_cost_usd,
            provider_selection=args.provider,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            expected_state_fingerprint=args.state_fingerprint,
            approval_token=args.approval_token,
            authorize_paid_recovery=args.authorize_paid_recovery,
        )
    except Exception as exc:
        report = {
            "mode": EXECUTION_MODE,
            "status": "REFUSED_BEFORE_EXECUTION",
            "paid_execution_authorized": False,
            "api_calls": 0,
            "actual_cost_usd": 0.0,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_human(report)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["status"] in {"AUTHORIZATION_REQUIRED", "COMMITTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
