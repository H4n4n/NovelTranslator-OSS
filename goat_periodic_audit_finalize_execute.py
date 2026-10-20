"""Explicitly authorized ZERO-provider local finalization of a completed audit."""
import json
from contextlib import contextmanager, ExitStack
from unittest.mock import patch

from goat_periodic_audit_finalize_plan import (
    build_goat_periodic_audit_finalize_plan, build_parser, local_module, blocked_send,
)
from novel_project import load_novel_project
from production_run_lock import production_run_lock


@contextmanager
def no_transport():
    """Physical transport and credential activation are fatal programming errors."""
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", blocked_send))
        stack.enter_context(patch("socket.create_connection", blocked_send))
        stack.enter_context(patch("command_code_provider_credentials.load_platform_dpapi", blocked_send))
        stack.enter_context(patch("dotenv.load_dotenv", blocked_send))
        yield


def execute_goat_periodic_audit_finalize(novel_id, *, repo_root, start_chapter, end_chapter,
        expected_state_fingerprint, approval_token, authorize_local_finalization=False):
    if authorize_local_finalization is not True:
        raise RuntimeError("explicit --authorize-local-finalization required; no paid authorization")
    project = load_novel_project(novel_id, repo_root=repo_root)
    with no_transport(), production_run_lock(project.root):
        plan = build_goat_periodic_audit_finalize_plan(novel_id, repo_root=repo_root,
            start_chapter=start_chapter, end_chapter=end_chapter)
        if (plan["state_fingerprint"] != expected_state_fingerprint or plan["approval_token"] != approval_token):
            raise RuntimeError("local audit finalization approval/state drift")
        proof = plan["state_binding"]["proof"]
        module = local_module(project)
        result = module.finalize_periodic_audit(start_chapter, end_chapter,
            proof["audit_result"], proof["audit_stats"], proof["chunking"])
        from production_uncertain_request_disposition import effective_paid_request_inventory
        from production_path_preflight import run_production_path_preflight
        inventory = effective_paid_request_inventory(project)
        if inventory["errors"] or inventory["uncertain"]:
            raise RuntimeError("strict inventory blocks after local audit finalization")
        preflight = run_production_path_preflight(novel_id, repo_root=repo_root,
            model_selection="v41-flash", provider_selection="command_code_provider")
        if preflight["status"] != "PASS":
            raise RuntimeError("strict preflight blocks after local audit finalization: " + str(preflight["errors"]))
        return result


def main(argv=None):
    parser = build_parser()
    parser.add_argument("--expected-state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-local-finalization", action="store_true")
    args = parser.parse_args(argv)
    result = execute_goat_periodic_audit_finalize(args.novel, repo_root=args.repo_root,
        start_chapter=args.start_chapter, end_chapter=args.end_chapter,
        expected_state_fingerprint=args.expected_state_fingerprint, approval_token=args.approval_token,
        authorize_local_finalization=args.authorize_local_finalization)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
