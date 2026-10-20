"""Operator interface: zero-provider plan/S0, separately authorized probe/run."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
from experimental_goat_policy import ExperimentError, canonical, external_path, qwen_catalog_policy
from experimental_goat_experiment import (authorization_token, build_plan, create_s0, read_json,
    require_authorization, require_live_verification, run_chronological, run_probes, runtime_for, validate_root)


@contextmanager
def activate_client(plan, action, token, authorize_paid):
    """Only credential activation seam. Called after all local authorization gates."""
    require_authorization(plan, action, token, authorize_paid)
    if action == "run":
        require_live_verification(plan, runtime_for(plan, None))
    from command_code_provider_credentials import command_code_api_key_scope
    from benchmark_longitudinal_provider import create_provider_client
    client = None
    local_failure = None
    with command_code_api_key_scope() as key:
        try:
            client = create_provider_client(api_key=key)
            try:
                yield client
            except ExperimentError as exc:
                # Trusted local errors leave only after the secret scope closes normally.
                local_failure = exc
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                client = None
                key = None
    if local_failure is not None:
        raise local_failure from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    template = sub.add_parser("qwen-config", help="print catalog-declared example; no live claim")
    plan = sub.add_parser("plan", help="freeze plan and build fresh S0 locally; zero provider")
    plan.add_argument("--production-root", required=True)
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--config", required=True)
    for action in ("probe", "run"):
        command = sub.add_parser(action)
        command.add_argument("--root", required=True)
        command.add_argument("--approval-token", required=True)
        command.add_argument("--authorize-paid", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "qwen-config":
        value = {"model":qwen_catalog_policy().to_dict(),
            "limits":{"global_ce":"3.00", "global_sends":150, "chapter_ce":"0.20", "chapter_sends":6},
            "tracks":[{"novel_id":"ace_evolution", "profile":"infinite_flow", "source_language":"zh", "last_chapter":13},
                      {"novel_id":"rogue_first_prince", "profile":"korean_fantasy", "source_language":"ko", "last_chapter":12}]}
    elif args.action == "plan":
        value = build_plan(production_root=args.production_root, output_root=args.output_root,
                           config=read_json(args.config))
        create_s0(value)
        value = {"root":value["output_root"], "plan_sha256":value["plan_sha256"],
            "probe_approval_token":authorization_token(value, "probe"),
            "run_approval_token":authorization_token(value, "run"),
            "capability_status":"catalog_declared", "provider_calls":0, "credential_reads":0}
    else:
        plan = validate_root(args.root)
        with activate_client(plan, args.action, args.approval_token, args.authorize_paid) as client:
            execute = run_probes if args.action == "probe" else run_chronological
            value = execute(args.root, client=client, token=args.approval_token, authorize_paid=args.authorize_paid)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
