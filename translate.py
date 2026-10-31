"""Select and run one isolated novel project with the validated production engine."""

from production_provider_runtime import PROVIDER_ENV, provider_identity
import argparse
import json
import os
import subprocess
import sys

from pathlib import Path
from production_provider_contract import PROVIDER_COMMAND_CODE

from chapter_library import index_chapter_library
from novel_project import load_novel_project, repository_root
from translation_model_config import (
    TRANSLATION_MODEL_ENV,
    resolve_translation_model,
)
from translation_profile import (
    effective_translation_instructions,
    load_novel_terminology_policy,
    load_translation_profile,
)


PRODUCTION_RUNNER = repository_root() / "run_production_engine.py"


def build_parser():
    parser = argparse.ArgumentParser(description="Run the production translator for one novel")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--novel", required=True, help="novel identifier under novels/")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "translation model: v41-flash (GOAT default), pro (explicit direct provider), legacy flash (recovery identity), "
            "or v41-flash; full DeepSeek model IDs and v4-pro/v4-flash/v4.1-flash "
            "aliases are also accepted"
        ),
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--max-api-calls", type=int)
    parser.add_argument("--max-credit-equivalent-usd", type=float)
    parser.add_argument("--start", action="store_true",
                        help="Run this novel until it finishes, is paused, or the budget ends")
    parser.add_argument("--budget-usd", type=float,
                        help="With --start: allow this much credit-equivalent spending from now on")
    parser.add_argument("--trial", type=int, metavar="CHAPTERS",
                        help="With --start: translate only this many chapters, then stop so you can read them")
    parser.add_argument("--correct-term", nargs=2, metavar=("SOURCE_TERM", "NEW_ENGLISH"),
                        help="Change a glossary term and its past uses at the next audit")
    parser.add_argument("--plan-goat-run", action="store_true", help="Read-only bounded unattended plan")
    parser.add_argument("--authorize-goat-run", action="store_true")
    parser.add_argument("--state-fingerprint")
    parser.add_argument("--approval-token")
    return parser


def production_environment(model_selection=None, base_environment=None, *, provider_selection=None):
    """Return child-process environment with one validated model pinned."""
    environment = dict(os.environ if base_environment is None else base_environment)
    config = resolve_translation_model(model_selection if model_selection is not None
        else environment.get(TRANSLATION_MODEL_ENV))
    backend = provider_identity(provider_selection if provider_selection is not None
        else environment.get(PROVIDER_ENV), config.model)
    environment[PROVIDER_ENV] = backend["provider"]
    environment[TRANSLATION_MODEL_ENV] = config.model
    return environment


def bounded_goat_run(args):
    """Reuse the existing exact plan/execute protocol and novel writer lock."""
    from controlled_trial_plan import build_controlled_trial_plan
    from production_run_lock import production_run_lock
    from production_goat_execution import execute_authorized_goat

    if args.max_api_calls is None or args.max_credit_equivalent_usd is None:
        raise ValueError("BOUNDED_ALLOWANCE_REQUIRED: supply --max-api-calls and "
                         "--max-credit-equivalent-usd, then --plan-goat-run")
    def plan():
        return build_controlled_trial_plan(args.novel, repo_root=args.repo_root,
            model_selection=args.model, provider_selection=args.provider,
            max_api_calls=args.max_api_calls, max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            unattended=True)

    if args.plan_goat_run:
        if args.authorize_goat_run:
            raise ValueError("plan and execution are separate invocations")
        result = plan()
    elif not args.authorize_goat_run:
        raise ValueError("AUTHORIZATION_REQUIRED: review --plan-goat-run, then supply "
                         "--state-fingerprint, --approval-token and --authorize-goat-run")
    else:
        if not args.state_fingerprint or not args.approval_token:
            raise ValueError("review --plan-goat-run and supply its --state-fingerprint and --approval-token")
        project = load_novel_project(args.novel, repo_root=args.repo_root)
        with production_run_lock(project.root):
            current = plan()
            if (current["state_fingerprint"] != args.state_fingerprint
                    or current["approval_token"] != args.approval_token):
                raise ValueError("GOAT authorization drift or consumed allowance; regenerate --plan-goat-run")
            result = execute_authorized_goat(current, repo_root=project.repository_root, recovery=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"READY", "COMMITTED", "ALLOWANCE_EXHAUSTED"} else 2


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.correct_term:
        from requested_corrections import add_request
        project = load_novel_project(args.novel, repo_root=args.repo_root)
        if args.correct_term[0] not in json.loads(project.glossary_file.read_text(encoding="utf-8"))["entries"]:
            print(f"{args.correct_term[0]} isn't in this novel's glossary.")
            return 2
        add_request(project.data_dir, chinese=args.correct_term[0], new_english=args.correct_term[1])
        print("Queued. The next audit changes the term and corrects past chapters.")
        return 0
    if args.start:
        from novel_runner import STOPPED, run_novel
        result = run_novel(args.novel, repo_root=args.repo_root, budget_usd=args.budget_usd, trial_chapters=args.trial)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == STOPPED else 0
    backend = provider_identity(args.provider, args.model)
    if backend["provider"] == PROVIDER_COMMAND_CODE:
        return bounded_goat_run(args)
    if args.plan_goat_run or args.authorize_goat_run or args.max_api_calls is not None or args.max_credit_equivalent_usd is not None:
        raise ValueError("GOAT allowance flags require --provider command_code_provider --model v41-flash")
    if args.model is None and not os.environ.get(TRANSLATION_MODEL_ENV):
        raise ValueError("direct DeepSeek requires explicit --model pro (or v41-flash)")
    model_config = resolve_translation_model(args.model)
    project = load_novel_project(args.novel, repo_root=args.repo_root)
    # Block incompatible paid history/pending before spawning a writer.
    from production_accounting import validate_progress_accounting
    from production_provider_runtime import inspect_pending_provider
    validate_progress_accounting(json.loads(project.progress_file.read_text(encoding="utf-8")),
        expected_provider=backend["provider"])
    inspect_pending_provider(project.pending_dir, backend)
    # Complete profile preflight before the side-effectful production process
    # can initialize credentials, logging, directories, or an API client.
    profile = load_translation_profile(
        project.config.profile, project.repository_root
    )
    effective_translation_instructions(
        profile, project.translation_addendum_file
    )
    load_novel_terminology_policy(
        profile, project.translation_addendum_file
    )
    with project.chapters_file.open("r", encoding="utf-8") as handle:
        source_chapters = json.load(handle)
    index_chapter_library(
        source_chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    if not PRODUCTION_RUNNER.is_file():
        raise FileNotFoundError(PRODUCTION_RUNNER)
    print(
        f"Translation model requested: {model_config.model} "
        f"({model_config.key})"
    )
    return subprocess.run(
        [sys.executable, str(PRODUCTION_RUNNER)],
        cwd=project.root,
        env=production_environment(model_config.model, provider_selection=args.provider),
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
