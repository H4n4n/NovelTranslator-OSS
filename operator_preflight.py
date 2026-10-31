"""Read-only unified operator routing for one NovelTranslator project.

This module composes existing read-only production preflight and bounded pending
recovery planning. It never imports a paid executor, never loads credentials,
never calls a provider, and never writes project state.
"""

from production_provider_runtime import selected_provider, authorization_provider
from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import json
from pathlib import Path

from novel_project import load_novel_project
from pending_recovery_plan import (
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_COST_USD,
    PendingRecoveryPlanError,
    build_pending_recovery_plan,
)
from production_path_preflight import (
    ProductionPreflightError,
    run_production_path_preflight,
)
from translation_model_config import resolve_translation_model


OPERATOR_MODE = "unified_safe_operator_preflight"

STATUS_BLOCKED = "BLOCKED"
STATUS_PAUSED = "PAUSED"
STATUS_RECOVERY_READY = "RECOVERY_READY"
STATUS_CLEANUP_READY = "CLEANUP_READY"
STATUS_PRODUCTION_READY = "PRODUCTION_READY"
STATUS_COMPLETE = "COMPLETE"
STATUS_CONTROLLED_TRIAL_READY = "CONTROLLED_TRIAL_READY"
STATUS_BOUNDED_GOAT_READY = "BOUNDED_GOAT_READY"
STATUS_BOUNDED_ALLOWANCE_REQUIRED = "BOUNDED_ALLOWANCE_REQUIRED"
STATUS_GOAT_SATURATION_RECOVERY_READY = "GOAT_SATURATION_RECOVERY_READY"

RECOVERY_PENDING_STATUSES = {
    "legacy_unpinned",
    "pending_model_match",
    "pending_model_override",
}


def _base_report(project, requested_config, max_api_calls, max_cost_usd):
    return {
        "mode": OPERATOR_MODE,
        "status": STATUS_BLOCKED,
        "novel_id": project.config.id,
        "title": project.config.title,
        "project_root": str(project.root),
        "profile": project.config.profile,
        "final_chapter": project.config.final_chapter,
        "requested_model": requested_config.model,
        "requested_model_key": requested_config.key,
        "effective_model": "",
        "pending_status": "",
        "pending_chapter": None,
        "completed_pending_cleanup_required": False,
        "completed_pending_cleanup_chapters": [],
        "manual_pause_active": project.pause_file.is_file(),
        "completed_chapter_count": 0,
        "committed_output_count": 0,
        "preflight_status": "",
        "preflight_errors": [],
        "preflight_protected_state_unchanged": False,
        "recovery": None,
        "resume_route": "",
        "recommended_action": "resolve_blockers",
        "recommended_entrypoint": "",
        "recommended_args": [],
        "optional_controlled_trial_plan": None,
        "paid_execution_authorized": False,
        "api_calls": 0,
        "project_writes": 0,
        "errors": [],
        "warnings": [],
        "recovery_max_api_calls": max_api_calls,
        "recovery_max_cost_usd": max_cost_usd,
    }


def _translation_needs_saturation_recovery(journal_path):
    """A slot with no answer, or a finished answer, is handled by the normal run."""
    if not journal_path.exists():
        return False
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    if record.get("status") == "planned":
        return False
    response = record.get("response") or {}
    finish = response.get("finish_reason") or ((response.get("raw_response") or {}).get("choices") or [{}])[0].get("finish_reason")
    return not (record.get("status") == "response_recorded" and finish == "stop")


def _blocked(report, *errors):
    report["status"] = STATUS_BLOCKED
    report["recommended_action"] = "resolve_blockers"
    report["recommended_entrypoint"] = ""
    report["recommended_args"] = []
    report["errors"].extend(str(error) for error in errors if str(error))
    return report


def _production_args(report):
    return ["--provider", report.get("effective_provider") or "direct_deepseek"] + [
        "--novel",
        report["novel_id"],
        "--model",
        report["requested_model_key"],
    ]


def _cleanup_args(report):
    return [
        "--novel",
        report["novel_id"],
    ]


def _recovery_plan_args(report):
    return ["--provider", report.get("effective_provider") or "direct_deepseek"] + [
        "--novel",
        report["novel_id"],
        "--model",
        report["requested_model_key"],
        "--max-api-calls",
        str(report["recovery_max_api_calls"]),
        "--max-credit-equivalent-usd" if report.get("effective_provider") == PROVIDER_COMMAND_CODE else "--max-cost-usd",
        str(report["recovery_max_cost_usd"]),
    ]


def _controlled_trial_plan(report):
    return {
        "entrypoint": "controlled_trial_plan.py",
        "args": (_recovery_plan_args(report) if report.get("effective_provider") == PROVIDER_COMMAND_CODE
                 else _production_args(report)),
        "paid_execution_authorized": False,
        "note": (
            "Optional one-chapter trial planning remains a separate read-only "
            "authorization surface."
        ),
    }


def build_operator_preflight(
    novel_id,
    *,
    repo_root=None,
    model_selection=None,
    max_api_calls=None,
    max_cost_usd=None,
    provider_selection=None,
    max_credit_equivalent_usd=None,
):
    """Return one fail-closed read-only operator classification."""

    explicit_send_ceiling = max_api_calls is not None
    max_api_calls = DEFAULT_MAX_API_CALLS if max_api_calls is None else max_api_calls
    provider = selected_provider(provider_selection)
    if provider == PROVIDER_COMMAND_CODE:
        if max_cost_usd is not None:
            raise ValueError("GOAT operator preflight forbids direct cost units")
        # Structural inspection needs no allowance; runnable bounded plans do.
        if max_credit_equivalent_usd is not None:
            authorization_provider(provider, model_selection, None, max_credit_equivalent_usd)
        if type(max_api_calls) is not int or max_api_calls < 1:
            raise ValueError("GOAT physical-send ceiling must be a positive integer")
        max_cost_usd = max_credit_equivalent_usd
    else:
        if max_credit_equivalent_usd is not None:
            raise ValueError("direct preflight forbids credit-equivalent units")
        max_cost_usd = DEFAULT_MAX_COST_USD if max_cost_usd is None else max_cost_usd
    requested_config = resolve_translation_model(model_selection)
    project = load_novel_project(novel_id, repo_root=repo_root)
    report = _base_report(
        project,
        requested_config,
        max_api_calls,
        max_cost_usd,
    )

    try:
        preflight = run_production_path_preflight(
            novel_id,
            repo_root=repo_root,
            model_selection=requested_config.model,
            **({"provider_selection": provider_selection} if provider_selection is not None else {}),
        )
    except (ProductionPreflightError, ValueError, OSError) as exc:
        report["preflight_status"] = "FAIL"
        return _blocked(
            report,
            f"production preflight could not complete: {exc}",
        )
    except Exception as exc:
        report["preflight_status"] = "FAIL"
        return _blocked(
            report,
            "unexpected production preflight failure: "
            f"{type(exc).__name__}: {exc}",
        )

    if provider == PROVIDER_COMMAND_CODE:
        report.update(max_credit_equivalent_usd=max_credit_equivalent_usd,
            budget_unit="usd_credit_equivalent_per_1m_tokens",
            allowance_label="credit-equivalent USD", provider_invoice_claimed=False)
    report.update(requested_provider=provider,
        effective_provider=preflight.get("effective_provider", ""),
        provider_identity=preflight.get("provider_identity", {}),
        pending_providers=preflight.get("pending_providers", []),
        provider_compatible=preflight.get("provider_compatible", False))
    report["preflight_status"] = preflight.get("status", "")
    report["preflight_errors"] = list(preflight.get("errors", []) or [])
    report["effective_model"] = preflight.get("effective_model", "")
    report["pending_status"] = preflight.get("pending_status", "")
    report["pending_chapter"] = preflight.get("pending_chapter")
    report["completed_pending_cleanup_required"] = bool(
        preflight.get("completed_pending_cleanup_required", False)
    )
    report["completed_pending_cleanup_chapters"] = list(
        preflight.get("completed_pending_cleanup_chapters", []) or []
    )
    report["completed_chapter_count"] = preflight.get(
        "completed_chapter_count", 0
    )
    report["committed_output_count"] = preflight.get(
        "committed_output_count", 0
    )
    report["preflight_protected_state_unchanged"] = preflight.get(
        "protected_state_unchanged", False
    )

    if report["preflight_status"] != "PASS":
        if preflight.get("provider_manual_review_required") is True:
            report.update(status="GOAT_HISTORICAL_MANUAL_REVIEW",
                          recommended_action="manual_review_historical_goat_evidence",
                          provider_manual_review_required=True)
            report["errors"].extend(report["preflight_errors"])
            return report
        return _blocked(
            report,
            *(
                report["preflight_errors"]
                or ["production-path preflight did not pass"]
            ),
        )

    if preflight.get("requested_model") != requested_config.model:
        return _blocked(
            report,
            "production preflight requested-model identity changed: "
            f"expected {requested_config.model!r}, "
            f"got {preflight.get('requested_model')!r}",
        )

    if preflight.get("api_calls") != 0:
        return _blocked(
            report,
            "production preflight reported provider calls in a read-only route: "
            f"{preflight.get('api_calls')!r}",
        )
    if preflight.get("project_writes") != 0:
        return _blocked(
            report,
            "production preflight reported project writes in a read-only route: "
            f"{preflight.get('project_writes')!r}",
        )
    if report["preflight_protected_state_unchanged"] is not True:
        return _blocked(
            report,
            "production preflight did not prove protected state remained unchanged",
        )

    try:
        effective_config = resolve_translation_model(
            report["effective_model"]
        )
    except ValueError:
        return _blocked(
            report,
            "production preflight returned an invalid effective model: "
            f"{report['effective_model']!r}",
        )
    report["effective_model"] = effective_config.model

    if report["completed_pending_cleanup_required"]:
        cleanup_chapters = report[
            "completed_pending_cleanup_chapters"
        ]
        if (
            not cleanup_chapters
            or any(
                not isinstance(chapter, int)
                or isinstance(chapter, bool)
                or chapter < 1
                for chapter in cleanup_chapters
            )
        ):
            return _blocked(
                report,
                "preflight reported cleanup-ready state without valid "
                "completed cleanup chapters",
            )
        if report["manual_pause_active"]:
            report["status"] = STATUS_PAUSED
            report["resume_route"] = STATUS_CLEANUP_READY
            report["recommended_action"] = "remove_manual_pause"
            return report
        report["status"] = STATUS_CLEANUP_READY
        report["recommended_action"] = (
            "run_completed_pending_cleanup"
        )
        report["recommended_entrypoint"] = (
            "completed_pending_cleanup.py"
        )
        report["recommended_args"] = _cleanup_args(report)
        return report

    pending_status = report["pending_status"]
    pending_chapter = report["pending_chapter"]

    if pending_status == "requested":
        if pending_chapter is not None:
            return _blocked(
                report,
                "preflight reported no pending translation but also returned "
                f"pending Chapter {pending_chapter!r}",
            )
        if effective_config.model != requested_config.model:
            return _blocked(
                report,
                "preflight changed the effective model without a pending "
                "translation to justify an override: "
                f"{requested_config.model!r} -> {effective_config.model!r}",
            )
    elif pending_status in RECOVERY_PENDING_STATUSES:
        if (
            not isinstance(pending_chapter, int)
            or isinstance(pending_chapter, bool)
            or pending_chapter < 1
        ):
            return _blocked(
                report,
                "preflight reported recoverable pending state without a valid "
                f"pending chapter: {pending_chapter!r}",
            )

        if provider == PROVIDER_COMMAND_CODE:
            from goat_glossary_review_recovery_plan import (
                recorded_attempt_present, build_goat_glossary_review_recovery_plan,
            )
            project = load_novel_project(novel_id, repo_root=repo_root)
            if recorded_attempt_present(project):
                if project.pause_file.exists():
                    report.update(status=STATUS_PAUSED, resume_route="GOAT_GLOSSARY_REVIEW_RECOVERY_READY")
                    return report
                try:
                    plan = build_goat_glossary_review_recovery_plan(novel_id, repo_root=repo_root,
                        max_api_calls=max_api_calls, max_credit_equivalent_usd=max_credit_equivalent_usd)
                except Exception as exc:
                    return _blocked(report, "GOAT recorded glossary review requires exact recovery proof: " + str(exc))
                report.update(status="GOAT_GLOSSARY_REVIEW_RECOVERY_READY", recovery=plan,
                    recommended_action="review_goat_glossary_review_recovery_plan",
                    recommended_entrypoint="goat_glossary_review_recovery_plan.py",
                    recommended_command=("python goat_glossary_review_recovery_plan.py --novel " + novel_id
                        + " --max-api-calls " + str(max_api_calls)
                        + " --max-credit-equivalent-usd " + str(max_credit_equivalent_usd)))
                return report

        try:
            recovery_plan = build_pending_recovery_plan(
                novel_id,
                repo_root=repo_root,
                model_selection=requested_config.model,
                **({"provider_selection": provider_selection} if provider_selection is not None else {}),
                max_api_calls=max_api_calls,
                max_cost_usd=None if provider == PROVIDER_COMMAND_CODE else max_cost_usd,
                **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if provider == PROVIDER_COMMAND_CODE else {}),
            )
        except (PendingRecoveryPlanError, ValueError, OSError) as exc:
            return _blocked(
                report,
                "pending state exists but bounded recovery is not ready: "
                f"{exc}",
            )
        except Exception as exc:
            return _blocked(
                report,
                "unexpected bounded-recovery planning failure: "
                f"{type(exc).__name__}: {exc}",
            )

        if recovery_plan.get("status") != "READY":
            return _blocked(
                report,
                "bounded recovery planner did not return READY",
            )
        if recovery_plan.get("api_calls") != 0:
            return _blocked(
                report,
                "bounded recovery planner reported provider calls",
            )
        if recovery_plan.get("project_writes") != 0:
            return _blocked(
                report,
                "bounded recovery planner reported project writes",
            )
        if recovery_plan.get("protected_state_unchanged") is not True:
            return _blocked(
                report,
                "bounded recovery planner did not prove protected state "
                "remained unchanged",
            )
        if (
            recovery_plan.get("requested_auxiliary_model")
            != requested_config.model
        ):
            return _blocked(
                report,
                "bounded recovery requested-model binding changed after "
                "preflight",
            )
        if recovery_plan.get("target_chapter") != pending_chapter:
            return _blocked(
                report,
                "bounded recovery target changed after preflight: "
                f"expected Chapter {pending_chapter}, "
                f"got {recovery_plan.get('target_chapter')!r}",
            )
        if recovery_plan.get("auxiliary_model") != effective_config.model:
            return _blocked(
                report,
                "bounded recovery effective model does not match preflight: "
                f"{recovery_plan.get('auxiliary_model')!r} != "
                f"{effective_config.model!r}",
            )

        if recovery_plan.get("max_api_calls") != max_api_calls:
            return _blocked(
                report,
                "bounded recovery provider-call ceiling changed after "
                "operator preflight",
            )
        if recovery_plan.get("max_cost_usd") != max_cost_usd:
            return _blocked(
                report,
                "bounded recovery cost ceiling changed after operator preflight",
            )
        if recovery_plan.get("translation_generation_allowed") is not False:
            return _blocked(
                report,
                "bounded recovery unexpectedly allows translation generation",
            )
        if recovery_plan.get("startup_periodic_audits_allowed") is not False:
            return _blocked(
                report,
                "bounded recovery unexpectedly allows startup periodic audits",
            )

        report["recovery"] = {
            "status": "READY",
            "target_chapter": recovery_plan["target_chapter"],
            "pending_stage": recovery_plan["pending_stage"],
            "requested_auxiliary_model": recovery_plan[
                "requested_auxiliary_model"
            ],
            "effective_auxiliary_model": recovery_plan[
                "auxiliary_model"
            ],
            "minimum_new_logical_api_calls": recovery_plan[
                "minimum_new_logical_api_calls"
            ],
            "max_api_calls": recovery_plan["max_api_calls"],
            "max_cost_usd": recovery_plan["max_cost_usd"],
            "translation_generation_allowed": recovery_plan[
                "translation_generation_allowed"
            ],
            "startup_periodic_audits_allowed": recovery_plan[
                "startup_periodic_audits_allowed"
            ],
            "state_fingerprint": recovery_plan["state_fingerprint"],
        }

        if provider == PROVIDER_COMMAND_CODE:
            report["recovery"].update(max_credit_equivalent_usd=max_credit_equivalent_usd,
                budget_unit=report["budget_unit"], provider_invoice_claimed=False)
        if report["manual_pause_active"]:
            report["status"] = STATUS_PAUSED
            report["resume_route"] = STATUS_RECOVERY_READY
            report["recommended_action"] = "remove_manual_pause"
            return report

        report["status"] = STATUS_RECOVERY_READY
        report["recommended_action"] = "review_pending_recovery_plan"
        report["recommended_entrypoint"] = "pending_recovery_plan.py"
        report["recommended_args"] = _recovery_plan_args(report)
        return report
    else:
        return _blocked(
            report,
            "production preflight returned an unknown pending status: "
            f"{pending_status!r}",
        )

    completed = report["completed_chapter_count"]
    committed = report["committed_output_count"]
    final_chapter = report["final_chapter"]
    if (
        not isinstance(completed, int)
        or isinstance(completed, bool)
        or completed < 0
    ):
        return _blocked(
            report,
            "production preflight returned an invalid completed-chapter count: "
            f"{completed!r}",
        )
    if (
        not isinstance(committed, int)
        or isinstance(committed, bool)
        or committed < 0
    ):
        return _blocked(
            report,
            "production preflight returned an invalid committed-output count: "
            f"{committed!r}",
        )
    if committed != completed:
        return _blocked(
            report,
            "production preflight completed/committed counts disagree: "
            f"{completed} != {committed}",
        )
    if completed > final_chapter:
        return _blocked(
            report,
            "completed chapter count exceeds configured final chapter: "
            f"{completed} > {final_chapter}",
        )
    if completed == final_chapter:
        report["status"] = STATUS_COMPLETE
        report["recommended_action"] = "none"
        return report

    if provider == PROVIDER_COMMAND_CODE:
        from goat_saturation_recovery_plan import (
            target_initial_path, inspect_target, build_goat_saturation_recovery_plan,
        )
        if _translation_needs_saturation_recovery(target_initial_path(project)):
            if report["manual_pause_active"]:
                report.update(status=STATUS_PAUSED, recommended_action="remove_manual_pause",
                              resume_route="GOAT_SATURATION_RECOVERY_READY")
                return report
            try:
                inspect_target(project)
                if max_credit_equivalent_usd is None:
                    report.update(status=STATUS_BOUNDED_ALLOWANCE_REQUIRED,
                        recommended_action="supply_bounded_credit_allowance",
                        resume_route="GOAT_SATURATION_RECOVERY_READY")
                    return report
                plan = build_goat_saturation_recovery_plan(novel_id, repo_root=repo_root,
                    max_api_calls=max_api_calls, max_credit_equivalent_usd=max_credit_equivalent_usd)
            except Exception as exc:
                return _blocked(report, "GOAT recorded target requires exact cap-failure recovery proof: " + str(exc))
            report.update(status=STATUS_GOAT_SATURATION_RECOVERY_READY,
                recovery=dict(plan, pending_stage=("recorded_cap_failure"
                    if plan["recovery_policy_revision"] == "goat-cap-failure-recovery-v2"
                    else "recorded_reasoning_saturation"),
                    effective_auxiliary_model=plan["requested_model"], translation_generation_allowed=True),
                recommended_action="review_goat_saturation_recovery_plan",
                recommended_entrypoint="goat_saturation_recovery_plan.py",
                recommended_args=["--novel", novel_id, "--max-api-calls", str(max_api_calls),
                                  "--max-credit-equivalent-usd", str(max_credit_equivalent_usd)])
            return report
        report["structurally_ready"] = True
        bounded_status = (STATUS_BOUNDED_GOAT_READY if max_credit_equivalent_usd is not None and explicit_send_ceiling
                          else STATUS_BOUNDED_ALLOWANCE_REQUIRED)
        if report["manual_pause_active"]:
            report["status"] = STATUS_PAUSED
            report["resume_route"] = bounded_status
            report["recommended_action"] = "remove_manual_pause"
        elif max_credit_equivalent_usd is None or not explicit_send_ceiling:
            report["status"] = bounded_status
            report["recommended_action"] = "supply_bounded_credit_allowance"
            report["warnings"].append("Explicit --max-api-calls and credit-equivalent USD allowance required before a runnable unattended plan command.")
        else:
            report["status"] = bounded_status
            report["recommended_action"] = "review_bounded_goat_plan"
            report["recommended_entrypoint"] = "translate.py"
            report["recommended_args"] = _recovery_plan_args(report) + ["--plan-goat-run"]
            report["optional_controlled_trial_plan"] = _controlled_trial_plan(report)
        return report

    if report["manual_pause_active"]:
        report["status"] = STATUS_PAUSED
        report["resume_route"] = STATUS_PRODUCTION_READY
        report["recommended_action"] = "remove_manual_pause"
        return report

    report["status"] = STATUS_PRODUCTION_READY
    report["recommended_action"] = "run_production"
    report["recommended_entrypoint"] = "translate.py"
    report["recommended_args"] = _production_args(report)
    report["optional_controlled_trial_plan"] = _controlled_trial_plan(
        report
    )
    return report


def _format_command(entrypoint, arguments):
    if not entrypoint:
        return ""
    return "python " + " ".join([entrypoint, *arguments])


def _print_human(report):
    print("=" * 72)
    print("NOVELTRANSLATOR UNIFIED SAFE OPERATOR PREFLIGHT — READ ONLY")
    print("=" * 72)
    print(f"Status: {report['status']}")
    if report.get("requested_provider") == PROVIDER_COMMAND_CODE:
        print("Provider: command_code_provider; provider_invoice_claimed=false")
        allowance = report["max_credit_equivalent_usd"]
        print("Bounded allowance: " + ("explicit credit-equivalent USD allowance required"
            if allowance is None else f"{allowance:.4f} credit-equivalent USD"))
        print(f"Physical-send ceiling: {report['recovery_max_api_calls']}")
    print(f"Novel: {report['novel_id']} — {report['title']}")
    print(f"Profile: {report['profile']}")
    print(f"Requested model: {report['requested_model']}")
    print(f"Effective model: {report['effective_model'] or 'not resolved'}")
    print(
        "Pending: "
        f"{report['pending_status'] or 'not resolved'}"
        + (
            f" (Chapter {report['pending_chapter']})"
            if report["pending_chapter"] is not None
            else ""
        )
    )
    if report["completed_pending_cleanup_required"]:
        print(
            "Completed pending cleanup: "
            + ", ".join(
                f"Chapter {chapter}"
                for chapter in report[
                    "completed_pending_cleanup_chapters"
                ]
            )
        )
    print(
        "Manual pause active: "
        + ("YES" if report["manual_pause_active"] else "NO")
    )
    print(
        "Progress: "
        f"{report['completed_chapter_count']} / "
        f"{report['final_chapter']} chapters"
    )
    if report["recovery"]:
        recovery = report["recovery"]
        print(
            "Recovery route: Chapter "
            f"{recovery['target_chapter']} "
            f"({recovery['pending_stage']}; "
            f"{recovery['effective_auxiliary_model']})"
        )
        print(
            "Recovery budget: "
            f"{recovery['max_api_calls']} provider calls / "
            + (f"{recovery['max_credit_equivalent_usd']:.4f} credit-equivalent USD; provider_invoice_claimed=false"
               if report.get("requested_provider") == PROVIDER_COMMAND_CODE
               else f"USD {recovery['max_cost_usd']:.4f}")
        )
        print(
            "Recovery may generate translation: "
            + (
                "YES"
                if recovery["translation_generation_allowed"]
                else "NO"
            )
        )
    if report["resume_route"]:
        print(f"Route after pause removal: {report['resume_route']}")
    print(f"Recommended action: {report['recommended_action']}")
    command = _format_command(
        report["recommended_entrypoint"],
        report["recommended_args"],
    )
    if command:
        print(f"Next read/run command: {command}")
    if report["optional_controlled_trial_plan"]:
        optional = report["optional_controlled_trial_plan"]
        print(
            "Optional controlled-trial plan: "
            + _format_command(optional["entrypoint"], optional["args"])
        )
    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    if report["errors"]:
        print("Blockers:")
        for error in report["errors"]:
            print(f"  - {error}")
    print("Paid execution authorized: NO")
    print(f"API calls: {report['api_calls']}")
    print(f"Project writes: {report['project_writes']}")
    print("=" * 72)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Classify one NovelTranslator project into a safe read-only "
            "production/recovery operator route."
        )
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--max-credit-equivalent-usd", type=float, default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help="v41-flash (GOAT default), pro (explicit direct), or legacy flash recovery ID",
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=None,
        help="explicit unattended send ceiling; recovery retains its historical default",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="future bounded-recovery cost ceiling",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = build_operator_preflight(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            max_api_calls=args.max_api_calls,
            max_cost_usd=args.max_cost_usd,
            provider_selection=args.provider,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd,
        )
    except Exception as exc:
        report = {
            "mode": OPERATOR_MODE,
            "status": STATUS_BLOCKED,
            "paid_execution_authorized": False,
            "api_calls": 0,
            "project_writes": 0,
            "errors": [
                "operator preflight input/setup failure: "
                f"{type(exc).__name__}: {exc}"
            ],
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if "novel_id" in report:
            _print_human(report)
        else:
            print("=" * 72)
            print("NOVELTRANSLATOR UNIFIED SAFE OPERATOR PREFLIGHT — READ ONLY")
            print("=" * 72)
            print("Status: BLOCKED")
            for error in report.get("errors", []):
                print(f"Blocker: {error}")
            print("Paid execution authorized: NO")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 72)

    return 0 if report["status"] in {
        STATUS_CONTROLLED_TRIAL_READY,
        STATUS_BOUNDED_GOAT_READY,
        STATUS_PRODUCTION_READY,
        STATUS_RECOVERY_READY,
        STATUS_GOAT_SATURATION_RECOVERY_READY,
        "GOAT_GLOSSARY_REVIEW_RECOVERY_READY",
        STATUS_CLEANUP_READY,
        STATUS_COMPLETE,
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
