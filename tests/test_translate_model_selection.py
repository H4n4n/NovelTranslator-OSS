import pytest

from translate import build_parser, production_environment
from translation_model_config import TRANSLATION_MODEL_ENV


def test_translate_cli_resolves_goat_v41_flash_by_default():
    args = build_parser().parse_args(["--novel", "sample"])
    assert args.novel == "sample"
    assert args.model is None
    environment = production_environment(args.model, {"KEEP": "yes"})
    assert environment["KEEP"] == "yes"
    assert environment[TRANSLATION_MODEL_ENV] == "deepseek-flash"
    assert environment["NOVELTRANSLATOR_PROVIDER_BACKEND"] == "command_code_provider"


def test_translate_cli_preserves_legacy_flash_identity():
    args = build_parser().parse_args(
        ["--novel", "sample", "--model", "flash"]
    )
    environment = production_environment(args.model, {}, provider_selection="direct_deepseek")
    assert environment[TRANSLATION_MODEL_ENV] == "deepseek-v4-flash"


def test_translate_cli_accepts_legacy_full_provider_model_id():
    args = build_parser().parse_args(
        ["--novel", "sample", "--model", "deepseek-v4-flash"]
    )
    environment = production_environment(args.model, {}, provider_selection="direct_deepseek")
    assert environment[TRANSLATION_MODEL_ENV] == "deepseek-v4-flash"


@pytest.mark.parametrize(
    "selection",
    ("v41-flash", "v4.1-flash", "deepseek-flash", "deepseek-v4.1-flash"),
)
def test_translate_cli_accepts_explicit_v41_flash(selection):
    args = build_parser().parse_args(
        ["--novel", "sample", "--model", selection]
    )
    environment = production_environment(args.model, {})
    assert environment[TRANSLATION_MODEL_ENV] == "deepseek-flash"


def test_translate_cli_unknown_model_fails_before_child_process_environment():
    with pytest.raises(ValueError, match="unsupported translation model"):
        production_environment("unknown-model", {})
