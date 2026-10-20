import builtins

import pytest

from production_output_runtime import (
    LEGACY_RATE_PREFIX,
    model_aware_legacy_print,
    model_display_name,
    rewrite_legacy_rate_label,
)
from translation_model_config import resolve_translation_model


def test_model_display_names_are_explicit():
    assert model_display_name(resolve_translation_model("pro")) == "DeepSeek V4 Pro"
    assert model_display_name(resolve_translation_model("flash")) == "DeepSeek V4 Flash"
    assert (
        model_display_name(resolve_translation_model("v41-flash"))
        == "DeepSeek V4.1 Flash"
    )


def test_rewrite_changes_only_known_legacy_prefix():
    flash = resolve_translation_model("flash")
    assert rewrite_legacy_rate_label(
        "V4 Pro rates / 1M: cache hit $0.007",
        flash,
    ) == "DeepSeek V4 Flash rates / 1M: cache hit $0.007"
    assert rewrite_legacy_rate_label("Pricing period: OFF-PEAK", flash) == (
        "Pricing period: OFF-PEAK"
    )


def test_v41_flash_rewrite_uses_explicit_version_name():
    flash = resolve_translation_model("v41-flash")
    assert rewrite_legacy_rate_label(
        "V4 Pro rates / 1M: cache hit $0.003",
        flash,
    ) == "DeepSeek V4.1 Flash rates / 1M: cache hit $0.003"


def test_pro_label_is_also_made_explicit():
    pro = resolve_translation_model("pro")
    assert rewrite_legacy_rate_label(
        "V4 Pro rates / 1M: cache hit $0.022",
        pro,
    ) == "DeepSeek V4 Pro rates / 1M: cache hit $0.022"


def test_context_manager_restores_builtin_print_after_success(capsys):
    original = builtins.print
    flash = resolve_translation_model("flash")
    with model_aware_legacy_print(flash):
        print("V4 Pro rates / 1M: cache miss $0.22")
    assert builtins.print is original
    assert "DeepSeek V4 Flash rates / 1M" in capsys.readouterr().out


def test_context_manager_restores_builtin_print_after_exception():
    original = builtins.print
    pro = resolve_translation_model("pro")
    with pytest.raises(RuntimeError, match="boom"):
        with model_aware_legacy_print(pro):
            raise RuntimeError("boom")
    assert builtins.print is original
