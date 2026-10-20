from types import SimpleNamespace

import pytest

from catastrophic_completeness_runtime import (
    CatastrophicCompletenessRuntimeError,
    install_catastrophic_completeness_qa,
)
from semantic_shadow_runtime import (
    SemanticShadowRuntimeError,
    install_semantic_shadow_qa,
)


def _base_qa(source_text, translation, glossary_entries, *_args, **_kwargs):
    return {
        "commit_allowed": True,
        "commit_blockers": [],
        "remaining_chinese_characters": 0,
        "residual_cjk_fragments": [],
        "locked_high_priority_term_violations": [],
        "issues": [],
    }


def _large_source():
    return "第十八章 埋入三寸\n\n" + (
        "姚雲走進城門，與守衛交談，記住了今日的約定。" * 55
    )


def test_stale_cached_states_reinstall_in_authoritative_order():
    core = SimpleNamespace(translation_commit_qa=_base_qa)
    first_completeness = install_catastrophic_completeness_qa(core)
    first_shadow = install_semantic_shadow_qa(core)
    assert core.translation_commit_qa is first_shadow["wrapped_translation_commit_qa"]
    assert first_shadow["original_translation_commit_qa"] is first_completeness[
        "wrapped_translation_commit_qa"
    ]

    # Simulate a long-lived test/tool process restoring the base callable while
    # accidentally leaving both runtime-state caches attached to the module.
    core.translation_commit_qa = _base_qa

    second_completeness = install_catastrophic_completeness_qa(core)
    second_shadow = install_semantic_shadow_qa(core)

    assert second_completeness is not first_completeness
    assert second_shadow is not first_shadow
    assert second_completeness["original_translation_commit_qa"] is _base_qa
    assert second_shadow["original_translation_commit_qa"] is second_completeness[
        "wrapped_translation_commit_qa"
    ]
    assert core.translation_commit_qa is second_shadow["wrapped_translation_commit_qa"]

    result = core.translation_commit_qa(
        _large_source(),
        "Chapter Eighteen: Buried Three Inches Deep!",
        {},
    )
    assert result["commit_allowed"] is False
    assert any(
        blocker.get("type") == "catastrophic_incompleteness"
        for blocker in result["commit_blockers"]
    )
    assert result["semantic_shadow"]["legacy_commit_allowed"] is False


def test_active_chain_remains_idempotent():
    core = SimpleNamespace(translation_commit_qa=_base_qa)
    completeness = install_catastrophic_completeness_qa(core)
    shadow = install_semantic_shadow_qa(core)
    assert install_catastrophic_completeness_qa(core) is completeness
    assert install_semantic_shadow_qa(core) is shadow
    assert core.translation_commit_qa is shadow["wrapped_translation_commit_qa"]


def test_unknown_active_wrapper_fails_closed_instead_of_overwrite():
    core = SimpleNamespace(translation_commit_qa=_base_qa)
    install_catastrophic_completeness_qa(core)
    install_semantic_shadow_qa(core)

    def unknown_wrapper(*args, **kwargs):
        return _base_qa(*args, **kwargs)

    core.translation_commit_qa = unknown_wrapper
    with pytest.raises(CatastrophicCompletenessRuntimeError, match="unknown QA wrapper"):
        install_catastrophic_completeness_qa(core)
    with pytest.raises(SemanticShadowRuntimeError, match="unknown QA wrapper"):
        install_semantic_shadow_qa(core)
