import builtins
import hashlib
import io
import json
import os
import socket
from pathlib import Path

import pytest

import benchmark_longitudinal_g7j_reasoning_budget as g7j
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS
from benchmark_longitudinal_g7a_readiness import TRANSLATION_EFFORT_BY_MODEL


TRANSLATION = g7j.PRIMARY_TRANSLATION_OPERATION


def _budget(model_id):
    return g7j.translation_completion_budget(
        logical_operation=TRANSLATION,
        requested_tokens=(
            g7j.GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS
        ),
        reasoning_effort=TRANSLATION_EFFORT_BY_MODEL[model_id],
    )


def test_every_goat_model_is_corrected_to_exactly_24000():
    assert len(BENCHMARK_MODEL_IDS) == 6
    assert g7j.GOAT_TRANSLATION_BASELINE_COMPLETION_TOKENS == 12_000
    assert g7j.GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS == 24_000
    for model_id in BENCHMARK_MODEL_IDS:
        assert _budget(model_id) == 24_000


def test_unsupported_32000_is_rejected_and_never_selected():
    assert g7j.UNSUPPORTED_TRANSLATION_COMPLETION_TOKENS == 32_000
    assert g7j.GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS != 32_000
    for model_id in BENCHMARK_MODEL_IDS:
        assert _budget(model_id) != 32_000
    with pytest.raises(g7j.G7JPolicyError, match="unsupported"):
        g7j.translation_completion_budget(
            logical_operation=TRANSLATION,
            requested_tokens=32_000,
            reasoning_effort="low",
        )


def test_all_six_models_receive_the_same_corrected_total():
    assert {_budget(model_id) for model_id in BENCHMARK_MODEL_IDS} == {
        24_000
    }


def test_frozen_translation_reasoning_bindings_are_preserved():
    recorded = g7j.translation_reasoning_effort_by_model()
    assert recorded == {
        "deepseek/deepseek-v4.1-flash": "low",
        "z-ai/glm-5.3-flash": "high",
        "qwen/qwen3.8-max-0902": "medium",
        "google/gemini-3.8-flash": "low",
        "tencent/hy3-paid": None,
        "xiaomi/mimo-v2.5-pro": None,
    }
    assert recorded == {
        model_id: TRANSLATION_EFFORT_BY_MODEL[model_id]
        for model_id in BENCHMARK_MODEL_IDS
    }
    assert tuple(
        g7j.EXPECTED_TRANSLATION_REASONING_EFFORT_BY_MODEL
    ) == tuple(recorded.items())


def test_deepseek_low_translation_effort_is_preserved():
    assert (
        TRANSLATION_EFFORT_BY_MODEL["deepseek/deepseek-v4.1-flash"]
        == "low"
    )
    assert _budget("deepseek/deepseek-v4.1-flash") == 24_000
    assert g7j.translation_completion_budget(
        logical_operation=TRANSLATION,
        requested_tokens=12_000,
        reasoning_effort="low",
    ) == 24_000


def test_default_only_models_receive_24000_without_a_reasoning_field():
    for model_id in ("tencent/hy3-paid", "xiaomi/mimo-v2.5-pro"):
        assert TRANSLATION_EFFORT_BY_MODEL[model_id] is None
        assert g7j.translation_completion_budget(
            logical_operation=TRANSLATION,
            requested_tokens=12_000,
            reasoning_effort=None,
        ) == 24_000


def test_auxiliary_operation_budgets_are_unchanged():
    for operation, tokens in (
        ("state_extraction", 3_000),
        ("state_json_repair", 5_000),
        ("glossary_review", 800),
        ("periodic_audit", 4_000),
        ("terminology_repair", 4_000),
    ):
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=tokens,
            reasoning_effort="low",
        ) == tokens
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=tokens,
            reasoning_effort=None,
        ) == tokens


@pytest.mark.parametrize(
    "drifted",
    (1, 11_999, 12_001, 16_000, 23_999),
)
def test_incoming_translation_baseline_drift_fails_closed(drifted):
    with pytest.raises(g7j.G7JPolicyError, match="drifted"):
        g7j.translation_completion_budget(
            logical_operation=TRANSLATION,
            requested_tokens=drifted,
            reasoning_effort="low",
        )


def test_reasoning_diagnostics_are_present_only_when_reported():
    reported = g7j.reasoning_usage_diagnostics(
        completion_tokens=11_994,
        reasoning_tokens=11_500,
        reasoning_tokens_exposed=True,
    )
    assert reported == {
        "reasoning_tokens_exposed": True,
        "reasoning_tokens": 11_500,
        "visible_completion_tokens": 494,
    }

    zero_is_a_reported_value = g7j.reasoning_usage_diagnostics(
        completion_tokens=100,
        reasoning_tokens=0,
        reasoning_tokens_exposed=True,
    )
    assert zero_is_a_reported_value == {
        "reasoning_tokens_exposed": True,
        "reasoning_tokens": 0,
        "visible_completion_tokens": 100,
    }

    absent = g7j.reasoning_usage_diagnostics(
        completion_tokens=100,
        reasoning_tokens=None,
        reasoning_tokens_exposed=False,
    )
    assert absent == {
        "reasoning_tokens_exposed": False,
        "reasoning_tokens": None,
    }


def test_impossible_reasoning_subtotals_and_malformed_exposure_fail():
    with pytest.raises(g7j.G7JPolicyError, match="cannot exceed"):
        g7j.reasoning_usage_diagnostics(
            completion_tokens=100,
            reasoning_tokens=101,
            reasoning_tokens_exposed=True,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.reasoning_usage_diagnostics(
            completion_tokens=100,
            reasoning_tokens=-1,
            reasoning_tokens_exposed=True,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.reasoning_usage_diagnostics(
            completion_tokens=100,
            reasoning_tokens="10",
            reasoning_tokens_exposed=True,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.reasoning_usage_diagnostics(
            completion_tokens=100,
            reasoning_tokens=True,
            reasoning_tokens_exposed=True,
        )
    with pytest.raises(g7j.G7JPolicyError, match="absent rather than guessed"):
        g7j.reasoning_usage_diagnostics(
            completion_tokens=100,
            reasoning_tokens=0,
            reasoning_tokens_exposed=False,
        )
    # Malformed exposure metadata is never bool-coerced.
    for malformed in (1, 0, "yes", None):
        with pytest.raises(g7j.G7JPolicyError):
            g7j.reasoning_usage_diagnostics(
                completion_tokens=100,
                reasoning_tokens=None,
                reasoning_tokens_exposed=malformed,
            )


def test_accounting_stats_gain_diagnostics_only_when_reported():
    base = {"completion_tokens": 11_994, "cost_usd": 0.125}

    absent = g7j.augment_accounting_stats_with_reasoning(
        base,
        reasoning_tokens=None,
        reasoning_tokens_exposed=False,
    )
    assert absent == base
    assert "reasoning_tokens" not in absent
    assert "reasoning_tokens_exposed" not in absent
    assert "visible_completion_tokens" not in absent

    reported = g7j.augment_accounting_stats_with_reasoning(
        base,
        reasoning_tokens=11_500,
        reasoning_tokens_exposed=True,
    )
    assert reported["cost_usd"] == 0.125
    assert reported["completion_tokens"] == 11_994
    assert reported["reasoning_tokens"] == 11_500
    assert reported["reasoning_tokens_exposed"] is True
    assert reported["visible_completion_tokens"] == 494

    # The input mapping is copied, never mutated.
    assert base == {"completion_tokens": 11_994, "cost_usd": 0.125}


def test_run_1_incident_classifies_exactly_as_completion_budget_exhausted():
    diagnostic = g7j.classify_translation_response(
        content="",
        finish_reason="length",
        completion_tokens=11_994,
        max_completion_tokens=12_000,
    )
    assert diagnostic == {
        "status": (
            "completion_budget_exhausted_before_visible_content"
        ),
        "visible_content_empty": True,
        "finish_reason": "length",
        "completion_tokens": 11_994,
        "max_completion_tokens": 12_000,
        "near_completion_ceiling": True,
    }


def test_empty_length_away_from_the_corrected_ceiling_is_length_terminated():
    diagnostic = g7j.classify_translation_response(
        content="   ",
        finish_reason="length",
        completion_tokens=5_000,
        max_completion_tokens=24_000,
    )
    assert diagnostic["status"] == (
        "empty_visible_content_length_terminated"
    )
    assert diagnostic["near_completion_ceiling"] is False

    at_boundary = g7j.classify_translation_response(
        content="",
        finish_reason="length",
        completion_tokens=24_000 - 64,
        max_completion_tokens=24_000,
    )
    assert at_boundary["status"] == (
        "completion_budget_exhausted_before_visible_content"
    )

    just_outside = g7j.classify_translation_response(
        content="",
        finish_reason="length",
        completion_tokens=24_000 - 65,
        max_completion_tokens=24_000,
    )
    assert just_outside["status"] == (
        "empty_visible_content_length_terminated"
    )


def test_empty_non_length_response_is_empty_visible_content():
    diagnostic = g7j.classify_translation_response(
        content="",
        finish_reason="stop",
        completion_tokens=10,
        max_completion_tokens=24_000,
    )
    assert diagnostic["status"] == "empty_visible_content"
    assert diagnostic["visible_content_empty"] is True


def test_nonempty_length_response_is_truncation_not_ok():
    diagnostic = g7j.classify_translation_response(
        content="partial translation",
        finish_reason="length",
        completion_tokens=23_999,
        max_completion_tokens=24_000,
    )
    assert diagnostic["status"] == (
        "visible_content_truncated_by_completion_budget"
    )
    assert diagnostic["visible_content_empty"] is False
    assert diagnostic["near_completion_ceiling"] is True


def test_nonempty_stop_response_is_ok():
    diagnostic = g7j.classify_translation_response(
        content="complete translation",
        finish_reason="stop",
        completion_tokens=2_000,
        max_completion_tokens=24_000,
    )
    assert diagnostic["status"] == "ok"
    assert diagnostic["near_completion_ceiling"] is False


@pytest.mark.parametrize("finish_reason", (None, "content_filter", "tool_calls"))
def test_nonempty_unknown_finish_reason_is_unsupported(finish_reason):
    diagnostic = g7j.classify_translation_response(
        content="translated prose",
        finish_reason=finish_reason,
        completion_tokens=20,
        max_completion_tokens=24_000,
    )
    assert diagnostic["status"] == (
        "unsupported_translation_finish_reason"
    )


def test_invalid_content_and_bounds_are_never_coerced():
    with pytest.raises(g7j.G7JPolicyError, match="content must be a string"):
        g7j.classify_translation_response(
            content=None,
            finish_reason="stop",
            completion_tokens=1,
            max_completion_tokens=24_000,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.classify_translation_response(
            content="x",
            finish_reason=5,
            completion_tokens=1,
            max_completion_tokens=24_000,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.classify_translation_response(
            content="x",
            finish_reason="stop",
            completion_tokens=-1,
            max_completion_tokens=24_000,
        )


def test_identity_is_deterministic_and_authorizes_no_provider_work():
    first = g7j.g7j_policy_identity()
    second = g7j.g7j_policy_identity()
    assert first == second

    canonical = json.dumps(
        first,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert g7j.G7J_POLICY_SHA256 == hashlib.sha256(canonical).hexdigest()

    assert first["historical_run_1_evidence"]["classification"] == (
        "completion_budget_exhausted_before_visible_content"
    )
    assert first["historical_run_1_evidence"][
        "reasoning_text_persisted"
    ] is False

    policy = first["completion_budget_policy"]
    assert policy["incoming_goat_primary_translation_baseline"] == 12_000
    assert policy["corrected_goat_primary_translation_total"] == 24_000
    assert policy["explicitly_unsupported_completion_tokens"] == [32_000]
    assert policy["max_selection_used"] is False
    assert policy["auxiliary_operations_unchanged"] is True
    assert policy["production_completion_budget_unchanged"] == 12_000

    fairness = first["roster_fairness"]
    assert fairness["all_six_goat_models_corrected_to_total"] is True
    assert fairness["default_only_models_included"] is True
    assert len(fairness["models"]) == 6
    assert all(
        model["corrected_total_completion_tokens"] == 24_000
        for model in fairness["models"]
    )
    assert {
        model["model_id"]: model["reasoning_effort"]
        for model in fairness["models"]
    } == g7j.translation_reasoning_effort_by_model()

    assert first["scope"]["benchmark_only"] is True
    assert first["scope"]["production_policy_changed"] is False

    authorization = first["provider_authorization"]
    assert authorization["provider_calls_authorized"] is False
    assert authorization["provider_calls_observed"] == 0
    assert authorization["credential_access"] is False
    assert authorization["retry_authorized"] is False


# --------------------------------------------------------------------------
# Frozen historical Run-1 incident facts
# --------------------------------------------------------------------------

FROZEN_RUN_1_EVIDENCE = {
    "model_id": "deepseek/deepseek-v4.1-flash",
    "reasoning_effort": "low",
    "prompt_tokens": 6_058,
    "completion_tokens": 11_994,
    "request_max_completion_tokens": 12_000,
    "visible_content": "",
    "visible_content_empty": True,
    "finish_reason": "length",
    "near_completion_ceiling": True,
    "accepted_credit_equivalent_usd": "0.007597068",
    "ledger_status": "accepted",
    "ledger_new_provider_sends_started": 1,
    "journal_status": "response_recorded",
    "progress_last_completed": 0,
    "progress_next_chapter": 1,
    "pending_artifact_present": False,
    "final_artifact_present": False,
    "consumed_paid_authorization_reusable": False,
    "classification": (
        "completion_budget_exhausted_before_visible_content"
    ),
    "reasoning_text_persisted": False,
    "reasoning_tokens_optional_diagnostic_only": True,
}


def test_historical_run_1_evidence_pins_every_frozen_incident_fact():
    evidence = g7j.g7j_policy_identity()["historical_run_1_evidence"]

    assert evidence == FROZEN_RUN_1_EVIDENCE
    assert evidence["model_id"] == g7j.HISTORICAL_RUN_1_MODEL_ID
    assert evidence["reasoning_effort"] == (
        g7j.HISTORICAL_RUN_1_REASONING_EFFORT
    )
    assert TRANSLATION_EFFORT_BY_MODEL[evidence["model_id"]] == (
        evidence["reasoning_effort"]
    )
    assert evidence["prompt_tokens"] == g7j.HISTORICAL_RUN_1_PROMPT_TOKENS
    assert evidence["completion_tokens"] == (
        g7j.HISTORICAL_RUN_1_COMPLETION_TOKENS
    )
    assert evidence["request_max_completion_tokens"] == (
        g7j.HISTORICAL_RUN_1_REQUEST_MAX_COMPLETION_TOKENS
    )
    assert evidence["accepted_credit_equivalent_usd"] == (
        g7j.HISTORICAL_RUN_1_ACCEPTED_CREDIT_EQUIVALENT_USD
    )
    assert evidence["ledger_status"] == g7j.HISTORICAL_RUN_1_LEDGER_STATUS
    assert evidence["ledger_new_provider_sends_started"] == (
        g7j.HISTORICAL_RUN_1_LEDGER_NEW_PROVIDER_SENDS_STARTED
    )
    assert evidence["journal_status"] == (
        g7j.HISTORICAL_RUN_1_JOURNAL_STATUS
    )
    assert evidence["progress_last_completed"] == (
        g7j.HISTORICAL_RUN_1_PROGRESS_LAST_COMPLETED
    )
    assert evidence["progress_next_chapter"] == (
        g7j.HISTORICAL_RUN_1_PROGRESS_NEXT_CHAPTER
    )
    assert evidence["pending_artifact_present"] is False
    assert evidence["final_artifact_present"] is False
    assert (
        evidence["consumed_paid_authorization_reusable"]
        is g7j.HISTORICAL_RUN_1_CONSUMED_PAID_AUTHORIZATION_REUSABLE
    )

    # The recorded classification is exactly the frozen evidence classification.
    recomputed = g7j.classify_translation_response(
        content=evidence["visible_content"],
        finish_reason=evidence["finish_reason"],
        completion_tokens=evidence["completion_tokens"],
        max_completion_tokens=evidence["request_max_completion_tokens"],
    )
    assert recomputed["status"] == evidence["classification"]
    assert recomputed["status"] == g7j.HISTORICAL_RUN_1_CLASSIFICATION


def _forbid_io(monkeypatch):
    def _forbid(*_args, **_kwargs):
        raise AssertionError("G7J policy identity must perform no I/O")

    for owner, name in (
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "getenv"),
        (socket, "socket"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (Path, "open"),
        (Path, "read_text"),
        (Path, "read_bytes"),
        (Path, "write_text"),
        (Path, "write_bytes"),
    ):
        monkeypatch.setattr(owner, name, _forbid)


def test_frozen_run_1_evidence_is_read_without_touching_historical_state(
    monkeypatch,
    tmp_path,
):
    """The frozen facts are constants: no file, socket, or env access at all."""
    monkeypatch.chdir(tmp_path)
    before = sorted(path.name for path in tmp_path.rglob("*"))

    _forbid_io(monkeypatch)

    identity = g7j.g7j_policy_identity()
    assert identity["historical_run_1_evidence"] == FROZEN_RUN_1_EVIDENCE
    assert identity["provider_authorization"]["provider_calls_observed"] == 0

    assert sorted(path.name for path in tmp_path.rglob("*")) == before
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Bounded post-response reasoning-diagnostic classification
# --------------------------------------------------------------------------

BOUNDED_REASONING_CASES = (
    # completion, reported subtotal, exposed, exact classification
    (11_994, 11_500, True, "ok"),
    (11_994, 0, True, "ok"),
    (11_994, None, False, "ok"),
    (200, 201, True, "reasoning_tokens_exceed_completion_tokens"),
    (200, 150, False, "unexposed_reasoning_tokens_present"),
    (200, None, True, "reasoning_tokens_malformed"),
    (200, "150", True, "reasoning_tokens_malformed"),
    (200, 150.0, True, "reasoning_tokens_malformed"),
    (200, True, True, "reasoning_tokens_malformed"),
    (200, -1, True, "reasoning_tokens_malformed"),
    (200, None, 1, "reasoning_exposure_metadata_malformed"),
    (200, None, "yes", "reasoning_exposure_metadata_malformed"),
    (-1, None, False, "completion_total_malformed"),
    (True, None, False, "completion_total_malformed"),
)


@pytest.mark.parametrize(
    "completion,value,exposed,expected",
    BOUNDED_REASONING_CASES,
)
def test_bounded_reasoning_outcome_never_raises_and_pins_exact_classification(
    completion,
    value,
    exposed,
    expected,
):
    outcome = g7j.reasoning_diagnostic_outcome(
        completion_tokens=completion,
        reasoning_tokens=value,
        reasoning_tokens_exposed=exposed,
    )

    assert set(outcome) == {"classification", "diagnostics"}
    assert outcome["classification"] == expected
    assert expected in g7j.REASONING_DIAGNOSTIC_CLASSIFICATIONS
    assert g7j.reasoning_diagnostic_classification(
        completion_tokens=completion,
        reasoning_tokens=value,
        reasoning_tokens_exposed=exposed,
    ) == expected

    if expected == "ok":
        assert outcome["diagnostics"] is not None
    else:
        # An untrustworthy subtotal is never returned for use.
        assert outcome["diagnostics"] is None


@pytest.mark.parametrize(
    "completion,value,exposed,expected",
    BOUNDED_REASONING_CASES,
)
def test_raising_normalizer_and_bounded_outcome_share_one_rule_table(
    completion,
    value,
    exposed,
    expected,
):
    outcome = g7j.reasoning_diagnostic_outcome(
        completion_tokens=completion,
        reasoning_tokens=value,
        reasoning_tokens_exposed=exposed,
    )
    assert outcome["classification"] == expected

    if expected == "ok":
        assert g7j.reasoning_usage_diagnostics(
            completion_tokens=completion,
            reasoning_tokens=value,
            reasoning_tokens_exposed=exposed,
        ) == outcome["diagnostics"]
        return

    with pytest.raises(g7j.G7JPolicyError) as excinfo:
        g7j.reasoning_usage_diagnostics(
            completion_tokens=completion,
            reasoning_tokens=value,
            reasoning_tokens_exposed=exposed,
        )
    assert str(excinfo.value) == (
        g7j.REASONING_DIAGNOSTIC_ERROR_MESSAGES[expected]
    )


def test_impossible_subtotal_is_classified_not_raised():
    outcome = g7j.reasoning_diagnostic_outcome(
        completion_tokens=25,
        reasoning_tokens=26,
        reasoning_tokens_exposed=True,
    )
    assert outcome == {
        "classification": "reasoning_tokens_exceed_completion_tokens",
        "diagnostics": None,
    }
    assert (
        g7j.REASONING_DIAGNOSTIC_ERROR_MESSAGES[
            "reasoning_tokens_exceed_completion_tokens"
        ]
        == "reasoning_tokens cannot exceed completion_tokens"
    )
