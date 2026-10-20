"""Locked operator disposition + one state send; never commits a chapter."""
import json
from contextlib import nullcontext
from uncertain_request_disposition_plan import build_uncertain_request_disposition_plan, build_parser, read_operator_evidence
from novel_project import load_novel_project
from production_run_lock import production_run_lock


def execute_uncertain_request_disposition(novel_id, *, repo_root, max_api_calls,
        max_credit_equivalent_usd, expected_state_fingerprint, approval_token,
        operator_evidence, authorize_uncertain_no_provider_record_disposition=False):
    project = load_novel_project(novel_id, repo_root=repo_root)
    with production_run_lock(project.root) if authorize_uncertain_no_provider_record_disposition else nullcontext():
        plan = build_uncertain_request_disposition_plan(novel_id, repo_root=repo_root,
            operator_evidence=operator_evidence,
            max_api_calls=max_api_calls, max_credit_equivalent_usd=max_credit_equivalent_usd)
        if (plan["state_fingerprint"] != expected_state_fingerprint or plan["approval_token"] != approval_token):
            raise RuntimeError("uncertain disposition authorization drift; regenerate read-only plan")
        if not authorize_uncertain_no_provider_record_disposition:
            return dict(status="AUTHORIZATION_REQUIRED", api_calls=0, credential_reads=0, project_writes=0)
        if plan["saved_state"]:
            from production_uncertain_request_disposition import write_checkpoint_receipt
            wrote = write_checkpoint_receipt(plan)
            return dict(status="STATE_CHECKPOINT_SAVED", api_calls=0, credential_reads=0, project_writes=int(wrote))
        if plan["status"] == "TERMINAL" and plan["failure_usage_settled"]:
            return dict(status="TERMINAL", api_calls=0, credential_reads=0, project_writes=0)
        # A failed recorded response is replayed for settlement only, never resend.
        from production_goat_execution import execute_authorized_goat
        return execute_authorized_goat(plan, repo_root=repo_root, recovery=True, uncertain_disposition=True)


def main(argv=None):
    parser = build_parser()
    parser.add_argument("--state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-uncertain-no-provider-record-disposition", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = execute_uncertain_request_disposition(args.novel, repo_root=args.repo_root,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            expected_state_fingerprint=args.state_fingerprint, approval_token=args.approval_token,
            operator_evidence=read_operator_evidence(args.operator_evidence),
            authorize_uncertain_no_provider_record_disposition=args.authorize_uncertain_no_provider_record_disposition)
    except Exception:
        report = dict(status="REFUSED_BEFORE_EXECUTION", api_calls=0, credential_reads=0,
            project_writes=0, error="uncertain disposition refused; inspect read-only plan/journals")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"AUTHORIZATION_REQUIRED", "STATE_CHECKPOINT_SAVED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
