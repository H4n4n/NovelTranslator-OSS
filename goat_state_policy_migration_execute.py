"""Locked, explicitly authorized execution of one state-policy migration plan."""
import json
from contextlib import nullcontext

from goat_state_policy_migration_plan import build_goat_state_policy_migration_plan, build_parser
from novel_project import load_novel_project
from production_run_lock import production_run_lock


def execute_goat_state_policy_migration(novel_id, *, repo_root, max_api_calls,
        max_credit_equivalent_usd, expected_state_fingerprint, approval_token,
        authorize_paid_recovery=False):
    project = load_novel_project(novel_id, repo_root=repo_root)
    with production_run_lock(project.root) if authorize_paid_recovery else nullcontext():
        plan = build_goat_state_policy_migration_plan(novel_id, repo_root=repo_root,
            max_api_calls=max_api_calls, max_credit_equivalent_usd=max_credit_equivalent_usd)
        if (plan["state_fingerprint"] != expected_state_fingerprint or plan["approval_token"] != approval_token):
            raise RuntimeError("state migration authorization drift; regenerate read-only plan")
        if not authorize_paid_recovery:
            return dict(status="AUTHORIZATION_REQUIRED", api_calls=0, credential_reads=0, project_writes=0)
        if plan["send_authorization_status"] != "READY":
            raise RuntimeError("HOLD_FOR_ARCHITECTURE_REVIEW: no authoritative provider hard bound; zero sends")
        from production_goat_execution import execute_authorized_goat
        return execute_authorized_goat(plan, repo_root=repo_root, recovery=True, state_policy_migration=True)


def main(argv=None):
    parser = build_parser()
    parser.add_argument("--state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-paid-recovery", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = execute_goat_state_policy_migration(args.novel, repo_root=args.repo_root,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            expected_state_fingerprint=args.state_fingerprint, approval_token=args.approval_token,
            authorize_paid_recovery=args.authorize_paid_recovery)
    except Exception:
        report = dict(status="REFUSED_BEFORE_EXECUTION", api_calls=0, credential_reads=0, project_writes=0,
            error="state migration authorization rejected; inspect read-only plan")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"AUTHORIZATION_REQUIRED", "COMMITTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
