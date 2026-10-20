from noveltranslator_core import pending_meta_after_commit_qa_failure
from production_commit_qa_checkpoint import run_commit_qa_checkpoint


def _allowed_qa():
    return {
        "chapter": 7,
        "commit_allowed": True,
        "commit_blockers": [],
        "remaining_chinese_characters": 0,
        "residual_cjk_fragments": [],
        "locked_high_priority_term_violations": [],
        "issues": [],
    }


def _run(*, translation="A valid recovered translation.", finish_reason="stop"):
    original = {
        "chapter": 7,
        "stage": "reviewed",
        "translation_stats": {"prompt_tokens": 10, "cost_usd": 0.01},
        "finish_reason": finish_reason,
        "state": {"new_terms": [{"chinese": "甲"}]},
        "state_stats": {"prompt_tokens": 2},
        "eligible_candidates": [{"candidate_id": "source:a"}],
        "review_result": {"decisions": []},
        "review_stats": {"prompt_tokens": 1},
    }
    persisted = []
    result = run_commit_qa_checkpoint(
        chapter_number=7,
        source_text="源文",
        translation=translation,
        finish_reason=finish_reason,
        meta=original,
        qa_checker=lambda *_args: _allowed_qa(),
        reset_failed_meta=pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: persisted.append(value),
    )
    return original, persisted, result


def test_recovered_nonstop_finish_reason_hard_blocks_before_state_reuse():
    original, persisted, result = _run(finish_reason="length")

    assert result["commit_allowed"] is False
    qa = result["qa"]
    assert qa["commit_allowed"] is False
    assert qa["provider_completion_gate"] == {
        "commit_allowed": False,
        "blockers": [
            {
                "type": "unexpected_finish_reason",
                "expected": "stop",
                "observed": "length",
            }
        ],
    }
    assert any(
        item.get("type") == "unexpected_finish_reason"
        for item in qa["commit_blockers"]
    )
    assert any("finish_reason" in issue for issue in qa["issues"])

    reset = result["meta"]
    assert reset["stage"] == "translated"
    assert reset["translation_stats"] == original["translation_stats"]
    for key in (
        "state",
        "state_stats",
        "eligible_candidates",
        "review_result",
        "review_stats",
    ):
        assert key not in reset
    assert persisted == [reset]


def test_recovered_missing_finish_reason_is_also_fail_closed():
    _original, _persisted, result = _run(finish_reason=None)
    assert result["commit_allowed"] is False
    blocker = result["qa"]["provider_completion_gate"]["blockers"][0]
    assert blocker["type"] == "unexpected_finish_reason"
    assert blocker["observed"] is None


def test_recovered_empty_translation_hard_blocks_even_when_linguistic_qa_allows():
    _original, _persisted, result = _run(translation="   ", finish_reason="stop")
    assert result["commit_allowed"] is False
    assert result["qa"]["provider_completion_gate"]["blockers"] == [
        {"type": "empty_translation"}
    ]


def test_short_but_nonempty_stop_translation_is_not_blocked_by_completion_gate():
    original, persisted, result = _run(
        translation="A brief epilogue.",
        finish_reason="stop",
    )
    assert result["commit_allowed"] is True
    assert "provider_completion_gate" not in result["qa"]
    assert result["meta"] is original
    assert original["stage"] == "reviewed"
    assert persisted == [original]


def test_existing_linguistic_blocker_stays_authoritative_with_valid_completion():
    meta = {"chapter": 7, "stage": "translated"}
    qa = _allowed_qa()
    qa["commit_allowed"] = False
    qa["commit_blockers"] = [{"type": "locked_terminology"}]
    qa["issues"] = ["locked terminology"]
    persisted = []

    result = run_commit_qa_checkpoint(
        chapter_number=7,
        source_text="源文",
        translation="English translation.",
        finish_reason="stop",
        meta=meta,
        qa_checker=lambda *_args: qa,
        reset_failed_meta=pending_meta_after_commit_qa_failure,
        persist_meta=lambda value: persisted.append(value),
    )

    assert result["commit_allowed"] is False
    assert result["qa"] is qa
    assert result["qa"]["commit_blockers"] == [{"type": "locked_terminology"}]
    assert "provider_completion_gate" not in result["qa"]
