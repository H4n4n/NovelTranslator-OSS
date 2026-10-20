"""Select and run one isolated novel project with the validated production engine."""

from production_provider_runtime import PROVIDER_ENV, provider_identity, reject_unattended_goat
import argparse
import json
import os
import subprocess
import sys

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
            "translation model: pro (default), legacy flash (recovery identity), "
            "or v41-flash; full DeepSeek model IDs and v4-pro/v4-flash/v4.1-flash "
            "aliases are also accepted"
        ),
    )
    return parser


def production_environment(model_selection=None, base_environment=None, *, provider_selection=None):
    """Return child-process environment with one validated model pinned."""
    config = resolve_translation_model(model_selection)
    environment = dict(os.environ if base_environment is None else base_environment)
    backend = provider_identity(provider_selection, config.model)
    environment[PROVIDER_ENV] = backend["provider"]
    environment[TRANSLATION_MODEL_ENV] = config.model
    return environment


def main(argv=None):
    args = build_parser().parse_args(argv)
    reject_unattended_goat(args.provider)
    model_config = resolve_translation_model(args.model)
    project = load_novel_project(args.novel)
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
