"""G7L GOAT auxiliary budget and fail-closed auxiliary classifier tests.

Synthetic and self-contained (no network, no credentials, no live evidence
directory): every fixture below is built under ``tmp_path`` with the real file
SHAPES and the exact recorded values of the GOAT Run-1 Chapter-1 auxiliary
incident.
"""

import copy
import hashlib
import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7j_reasoning_budget as g7j
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS, PROVIDER_NAME
from benchmark_longitudinal_g7a_readiness import TRANSLATION_EFFORT_BY_MODEL
from benchmark_longitudinal_provider import build_provider_request_identity
from production_paid_request_journal import request_identity


NOW = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)

AUXILIARY_OPERATIONS = tuple(g7j.GOAT_AUXILIARY_OPERATIONS)
STATE_EXTRACTION = "state_extraction"
STATE_JSON_REPAIR = "state_json_repair"
CHAPTER = 1
SOURCE_TEXT = "Chapter 1 source text"
TRANSLATION_TEXT = "Chapter 1 committed English translation"

# The exact recorded incident response: accepted, EMPTY visible content,
# finish_reason "length", all 3,000 completion tokens reasoning at the frozen
# 3,000-token legacy auxiliary ceiling.
INCIDENT_COMPLETION_TOKENS = 3_000
INCIDENT_REASONING_TOKENS = 3_000
INCIDENT_FINISH_REASON = "length"
INCIDENT_CONTENT = ""


def _clock():
    return NOW


# --------------------------------------------------------------------------
# classify_auxiliary_response
# --------------------------------------------------------------------------


def test_empty_length_at_ceiling_not_repaired_fail_closed():
    """The real incident: empty length-terminated at the ceiling is never repaired."""
    diagnostic = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content=INCIDENT_CONTENT,
        finish_reason=INCIDENT_FINISH_REASON,
        completion_tokens=INCIDENT_COMPLETION_TOKENS,
        max_completion_tokens=(
            g7j.HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
        ),
        reasoning_tokens=INCIDENT_REASONING_TOKENS,
    )

    assert diagnostic["status"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert diagnostic["status"] in g7j.AUXILIARY_FAIL_CLOSED_STATUSES
    assert diagnostic["fail_closed"] is True
    assert diagnostic["repair_allowed"] is False
    assert diagnostic["visible_content_empty"] is True
    assert diagnostic["near_completion_ceiling"] is True
    assert diagnostic["completion_tokens"] == 3_000
    assert diagnostic["max_completion_tokens"] == 3_000
    assert diagnostic["reasoning_tokens"] == 3_000
    assert diagnostic["valid_json_object"] is False

    # Whitespace-only content is empty content.
    assert g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="   \n",
        finish_reason="length",
        completion_tokens=3_000,
        max_completion_tokens=3_000,
    )["status"] == g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT

    # The corrected 4,000 ceiling: at/inside the 64-token tolerance is exhausted.
    at_boundary = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason="length",
        completion_tokens=4_000 - 64,
        max_completion_tokens=4_000,
    )
    assert at_boundary["status"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert at_boundary["fail_closed"] is True

    # The frozen incident classification is the recorded constant.
    assert g7j.HISTORICAL_AUXILIARY_CLASSIFICATION == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert g7j.HISTORICAL_AUXILIARY_COMPLETION_TOKENS == 3_000
    assert g7j.HISTORICAL_AUXILIARY_REPAIR_FED_EMPTY_INPUT is True


def test_empty_length_below_ceiling_fail_closed():
    diagnostic = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason="length",
        completion_tokens=1_000,
        max_completion_tokens=4_000,
    )
    assert diagnostic["status"] == g7j.AUXILIARY_EMPTY_CONTENT_LENGTH
    assert diagnostic["fail_closed"] is True
    assert diagnostic["repair_allowed"] is False
    assert diagnostic["near_completion_ceiling"] is False

    just_outside = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason="length",
        completion_tokens=4_000 - 65,
        max_completion_tokens=4_000,
    )
    assert just_outside["status"] == g7j.AUXILIARY_EMPTY_CONTENT_LENGTH
    assert just_outside["fail_closed"] is True

    # An unknown completion total is never guessed into "ok".
    unknown_total = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason="length",
        completion_tokens=None,
        max_completion_tokens=4_000,
    )
    assert unknown_total["status"] == g7j.AUXILIARY_EMPTY_CONTENT_LENGTH
    assert unknown_total["fail_closed"] is True


@pytest.mark.parametrize(
    "finish_reason",
    (None, "stop", "content_filter", "tool_calls"),
)
def test_empty_stop_finish_anomalous_fail_closed(finish_reason):
    diagnostic = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason=finish_reason,
        completion_tokens=10,
        max_completion_tokens=4_000,
    )
    assert diagnostic["status"] == (
        g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH
    )
    assert diagnostic["fail_closed"] is True
    assert diagnostic["repair_allowed"] is False
    assert diagnostic["finish_reason"] == finish_reason

    at_ceiling = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content="",
        finish_reason=finish_reason,
        completion_tokens=4_000,
        max_completion_tokens=4_000,
    )
    # A non-length empty response is anomalous whatever the token total.
    assert at_ceiling["status"] == (
        g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH
    )
    assert at_ceiling["fail_closed"] is True


def test_malformed_nonempty_json_still_repaired():
    malformed = '{"new_terms": [{"chinese": "x"'
    diagnostic = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content=malformed,
        finish_reason="length",
        completion_tokens=3_000,
        max_completion_tokens=4_000,
    )
    assert diagnostic["status"] == g7j.AUXILIARY_MALFORMED_JSON_REPAIRED
    assert diagnostic["repair_allowed"] is True
    assert diagnostic["fail_closed"] is False
    assert diagnostic["visible_content_empty"] is False

    for non_object in ("not json at all", "[1, 2, 3]", '"a string"', "17"):
        assert g7j.classify_auxiliary_response(
            logical_operation=STATE_JSON_REPAIR,
            content=non_object,
            finish_reason="stop",
            completion_tokens=25,
            max_completion_tokens=4_000,
        )["status"] == g7j.AUXILIARY_MALFORMED_JSON_REPAIRED

    accepted = g7j.classify_auxiliary_response(
        logical_operation=STATE_EXTRACTION,
        content='{"new_terms": [], "continuity_summary": "s", "issues": []}',
        finish_reason="stop",
        completion_tokens=25,
        max_completion_tokens=4_000,
    )
    assert accepted["status"] == g7j.AUXILIARY_STATE_ACCEPTED
    assert accepted["valid_json_object"] is True
    assert accepted["repair_allowed"] is False
    assert accepted["fail_closed"] is False

    # Exactly the five contract statuses and the three fail-closed ones.
    assert g7j.AUXILIARY_RESPONSE_STATUSES == frozenset(
        {
            g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
            g7j.AUXILIARY_EMPTY_CONTENT_LENGTH,
            g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH,
            g7j.AUXILIARY_MALFORMED_JSON_REPAIRED,
            g7j.AUXILIARY_STATE_ACCEPTED,
        }
    )
    assert g7j.AUXILIARY_FAIL_CLOSED_STATUSES == frozenset(
        {
            g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
            g7j.AUXILIARY_EMPTY_CONTENT_LENGTH,
            g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH,
        }
    )


def test_classifier_never_coerces_malformed_evidence():
    with pytest.raises(g7j.G7JPolicyError, match="content must be a string"):
        g7j.classify_auxiliary_response(
            logical_operation=STATE_EXTRACTION,
            content=None,
            finish_reason="length",
            completion_tokens=1,
            max_completion_tokens=4_000,
        )
    with pytest.raises(g7j.G7JPolicyError, match="finish_reason"):
        g7j.classify_auxiliary_response(
            logical_operation=STATE_EXTRACTION,
            content="{}",
            finish_reason=5,
            completion_tokens=1,
            max_completion_tokens=4_000,
        )
    with pytest.raises(g7j.G7JPolicyError, match="max_completion_tokens"):
        g7j.classify_auxiliary_response(
            logical_operation=STATE_EXTRACTION,
            content="{}",
            finish_reason="stop",
            completion_tokens=1,
            max_completion_tokens=0,
        )
    with pytest.raises(g7j.G7JPolicyError, match="cannot exceed"):
        g7j.classify_auxiliary_response(
            logical_operation=STATE_EXTRACTION,
            content="{}",
            finish_reason="stop",
            completion_tokens=10,
            max_completion_tokens=4_000,
            reasoning_tokens=11,
        )
    with pytest.raises(g7j.G7JPolicyError, match="GOAT auxiliary operation"):
        g7j.classify_auxiliary_response(
            logical_operation=g7j.PRIMARY_TRANSLATION_OPERATION,
            content="{}",
            finish_reason="stop",
            completion_tokens=1,
            max_completion_tokens=4_000,
        )


def test_auxiliary_budget_correction_and_drift_rejection():
    assert g7j.GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS == 4_000
    assert g7j.GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS == 3_000
    assert g7j.PRODUCTION_AUXILIARY_COMPLETION_TOKENS == 3_000
    assert AUXILIARY_OPERATIONS == (
        STATE_EXTRACTION,
        "glossary_review",
        "periodic_audit",
        STATE_JSON_REPAIR,
        "terminology_repair",
    )
    baselines = dict(
        g7j.GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS
    )
    assert baselines[STATE_EXTRACTION] == 3_000
    assert baselines[STATE_JSON_REPAIR] == 5_000
    assert baselines["glossary_review"] == 800
    assert baselines["periodic_audit"] == 4_000
    assert baselines["terminology_repair"] == 4_000

    for operation in AUXILIARY_OPERATIONS:
        assert g7j.goat_auxiliary_completion_budget(
            logical_operation=operation,
            requested_tokens=baselines[operation],
        ) == 4_000
        # The frozen legacy 3,000 auxiliary baseline is always accepted.
        assert g7j.goat_auxiliary_completion_budget(
            logical_operation=operation,
            requested_tokens=3_000,
        ) == 4_000

    for drifted in (1, 2_999, 3_001, 6_000, 12_000, 32_000):
        with pytest.raises(g7j.G7JPolicyError, match="drift|unsupported"):
            g7j.goat_auxiliary_completion_budget(
                logical_operation=STATE_EXTRACTION,
                requested_tokens=drifted,
            )

    # The explicit opt-in form is the same single-call entrypoint.
    assert g7j.translation_completion_budget(
        logical_operation=STATE_EXTRACTION,
        requested_tokens=3_000,
        reasoning_effort="low",
        goat=True,
    ) == 4_000
    # Primary translation keeps its corrected 24,000 and 32,000 stays rejected.
    assert g7j.translation_completion_budget(
        logical_operation=g7j.PRIMARY_TRANSLATION_OPERATION,
        requested_tokens=12_000,
        reasoning_effort="low",
        goat=True,
    ) == 24_000
    with pytest.raises(g7j.G7JPolicyError, match="unsupported"):
        g7j.translation_completion_budget(
            logical_operation=g7j.PRIMARY_TRANSLATION_OPERATION,
            requested_tokens=32_000,
            reasoning_effort="low",
            goat=True,
        )


def test_six_model_determinism():
    """The corrected auxiliary ceiling is uniform and model/effort independent."""
    assert len(BENCHMARK_MODEL_IDS) == 6
    observed = set()
    for model_id in BENCHMARK_MODEL_IDS:
        effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
        for operation in AUXILIARY_OPERATIONS:
            observed.add(
                g7j.goat_auxiliary_completion_budget(
                    logical_operation=operation,
                    requested_tokens=3_000,
                )
            )
        # The entrypoint takes no model and no effort at all.
        assert g7j.auxiliary_completion_budget_for_identity(goat=True) == 4_000
        assert g7j.auxiliary_completion_budget_for_identity(goat=False) == 3_000
        assert effort in ("low", "medium", "high", None)

    assert observed == {4_000}

    classifier_parameters = set(
        inspect.signature(g7j.classify_auxiliary_response).parameters
    )
    assert classifier_parameters == {
        "logical_operation",
        "content",
        "finish_reason",
        "completion_tokens",
        "max_completion_tokens",
        "reasoning_tokens",
    }
    assert not {
        name
        for name in classifier_parameters
        if "model" in name or "effort" in name
    }

    budget_parameters = set(
        inspect.signature(g7j.goat_auxiliary_completion_budget).parameters
    )
    assert budget_parameters == {"logical_operation", "requested_tokens"}

    policy = g7j.g7j_policy_identity()["auxiliary_completion_budget_policy"]
    assert policy["uniform_for_all_six_goat_models"] is True
    assert policy["model_dependent"] is False
    assert policy["effort_dependent"] is False
    assert policy["corrected_goat_auxiliary_total"] == 4_000
    assert policy["translation_keeps_corrected_total"] == 24_000
    assert policy["explicitly_unsupported_completion_tokens"] == [32_000]
    assert policy["max_selection_used"] is False
    assert policy["empty_auxiliary_response_never_repaired"] is True
    assert policy["production_state_checkpoint_identity_ceiling"] == 3_000
    assert policy["goat_state_checkpoint_identity_ceiling"] == 4_000

    # The frozen historical Run-1 translation evidence is untouched by G7L.
    historical = g7j.g7j_policy_identity()["historical_run_1_evidence"]
    assert historical["classification"] == (
        "completion_budget_exhausted_before_visible_content"
    )
    auxiliary_incident = g7j.g7j_policy_identity()[
        "historical_auxiliary_incident_evidence"
    ]
    assert auxiliary_incident["classification"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert auxiliary_incident["fail_closed"] is True
    assert auxiliary_incident["repair_allowed"] is False
    assert auxiliary_incident["repair_fed_empty_input"] is True
    assert auxiliary_incident["local_salvage_returned_none"] is True


def test_production_extract_state_unchanged_and_identity_bytes_stable(tmp_path):
    """The production auxiliary path and identity bytes are unchanged."""
    # (1) The production (non-GOAT) auxiliary ceilings are returned untouched.
    non_goat_ceilings = {
        STATE_EXTRACTION: 3_000,
        STATE_JSON_REPAIR: 5_000,
        "glossary_review": 800,
        "periodic_audit": 4_000,
        "terminology_repair": 4_000,
    }
    for operation, ceiling in non_goat_ceilings.items():
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=ceiling,
            reasoning_effort="low",
        ) == ceiling
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=ceiling,
            reasoning_effort=None,
        ) == ceiling

    # (2) The production state-checkpoint identity ceiling stays exactly 3,000.
    assert g7j.auxiliary_completion_budget_for_identity(goat=False) == 3_000
    assert g7j.PRODUCTION_AUXILIARY_COMPLETION_TOKENS == 3_000
    assert g7j.auxiliary_completion_budget_for_identity(goat=False) == (
        g7j.HISTORICAL_AUXILIARY_REQUEST_MAX_COMPLETION_TOKENS
    )

    # (3) The executor's frozen identity gains no G7L field.
    identity = g7f.g7f_executor_identity()
    assert "g7l" not in json.dumps(identity).lower()
    assert "auxiliary_budget" not in json.dumps(identity).lower()

    # (4) Without the explicit opt-in the runtime is byte-identical: the
    # auxiliary ceiling is preserved, no guard is installed, and the engine's own
    # empty-response path is untouched (exactly what production does today).
    module, plan_value, _ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content=INCIDENT_CONTENT,
            finish_reason=INCIDENT_FINISH_REASON,
            completion_tokens=INCIDENT_COMPLETION_TOKENS,
            reasoning_tokens=INCIDENT_REASONING_TOKENS,
        ),
    )
    model_id = plan_value["g7e_plan"]["model_id"]
    state = module._g7f_stage1_runtime_state
    assert state["goat_auxiliary_policy"] is False
    assert state["auxiliary_response_guard_installed"] is False
    assert state["goat_auxiliary_total_completion_tokens"] is None
    assert state["production_auxiliary_budget_unchanged"] is True

    response = module.journaled_chat_request(
        STATE_EXTRACTION,
        CHAPTER,
        SOURCE_TEXT,
        _legacy_kwargs(model_id, max_tokens=3_000),
        translation_text=TRANSLATION_TEXT,
    )
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 3_000
    # The production path returns the raw empty response to the engine, exactly
    # as before G7L: nothing is classified and nothing is rejected here.
    envelope = response["response"]
    assert envelope["content"] == ""
    assert envelope["finish_reason"] == "length"
    assert envelope["stats"]["completion_tokens"] == 3_000
    assert "diagnostic" not in envelope
    assert "fail_closed" not in json.dumps(envelope)


# --------------------------------------------------------------------------
# G7F runtime: budget application point and post-response enforcement
# --------------------------------------------------------------------------


class FakeCompletions:
    def __init__(self, factory):
        self.calls = []
        self._factory = factory

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self._factory(kwargs)


def _fake_client(factory):
    completions = FakeCompletions(factory)
    return (
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        completions,
    )


def _provider_response(
    *,
    content,
    finish_reason,
    completion_tokens=25,
    prompt_tokens=100,
    reasoning_tokens=None,
    reasoning_exposed=False,
):
    def factory(kwargs):
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        if reasoning_exposed:
            usage.completion_tokens_details = SimpleNamespace(
                reasoning_tokens=reasoning_tokens
            )
        return SimpleNamespace(
            id="fake-response-1",
            model=kwargs["model"],
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=usage,
        )

    return factory


def _plan(*, run_index=1, sends=4, cost="1", target=CHAPTER):
    spec = g7f.stage1_run_specs()[run_index - 1]
    nested = {
        "model_id": spec["model_id"],
        "novel_id": spec["novel_id"],
        "maximum_new_provider_sends": sends,
        "maximum_credit_equivalent_usd": cost,
    }
    plan = {
        "schema_version": g7f.G7F_SCHEMA_VERSION,
        "revision": g7f.G7F_REVISION,
        "g7f_executor_sha256": g7f.G7F_EXECUTOR_SHA256,
        "stage1_roots_manifest_sha256": (
            g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
        ),
        "run_index": run_index,
        "relative_root": spec["relative_root"],
        "target_chapter": target,
        "pre_invocation_state_fingerprint_sha256": "1" * 64,
        "chapter_resume_status": "ready_or_resumable",
        "journal_resume_status": "resumable",
        "approval_token": "a" * 64,
        "g7e_plan": nested,
        "provider_calls": 0,
    }
    plan["plan_sha256"] = g7f._sha256_json(plan)
    return plan


def _module(*, journal_dir, novel_id="ace_evolution"):
    module = SimpleNamespace(
        _RUNTIME_INITIALIZED=True,
        client=None,
        api_key=None,
        MODEL=None,
        AUTO_OFF_PEAK_ONLY=True,
        journaled_chat_request=lambda *a, **k: None,
        call_api_with_retries=lambda **k: None,
        send_api_request_once=lambda **k: None,
        usage_and_cost=lambda usage: {},
        usage_and_cost_for_pricing=lambda usage, pricing: {},
        reprice_saved_stats_if_needed=lambda stats: stats,
        pricing_snapshot=lambda moment=None: {},
        ACTIVE_NOVEL_CONFIG=SimpleNamespace(id=novel_id),
        ACTIVE_PROFILE=SimpleNamespace(id="fixture"),
        PAID_REQUEST_JOURNAL_DIR=journal_dir,
    )
    for name in g7f.CONTEXT_ENTRYPOINTS:
        setattr(module, name, lambda *a, **k: None)
    return module


def _legacy_kwargs(model_id, *, max_tokens):
    return {
        "model": live_provider_capability(model_id).provider_model_id,
        "messages": [{"role": "user", "content": SOURCE_TEXT}],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _runtime(tmp_path, *, factory, run_index=1, goat_auxiliary_policy=False):
    plan = _plan(run_index=run_index)
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    journal_dir = tmp_path / "journals"
    module = _module(
        journal_dir=journal_dir,
        novel_id=plan["g7e_plan"]["novel_id"],
    )
    client, completions = _fake_client(factory)
    g7f.install_stage1_paid_runtime(
        module,
        model_id=plan["g7e_plan"]["model_id"],
        provider_client=client,
        budget_ledger=ledger,
        clock=_clock,
        goat_auxiliary_policy=goat_auxiliary_policy,
    )
    return module, plan, ledger, completions


def test_aux_budget_applied_before_identity_journal_ledger_transport(tmp_path):
    """The corrected 8000 auxiliary ceiling precedes identity/journal/ledger/send."""
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(content="{}", finish_reason="stop"),
        goat_auxiliary_policy=True,
    )
    model_id = plan["g7e_plan"]["model_id"]
    assert model_id == "deepseek/deepseek-v4.1-flash"
    assert module._g7f_stage1_runtime_state[
        "goat_auxiliary_total_completion_tokens"
    ] == 8_000

    module.journaled_chat_request(
        STATE_EXTRACTION,
        CHAPTER,
        SOURCE_TEXT,
        _legacy_kwargs(model_id, max_tokens=3_000),
        translation_text=TRANSLATION_TEXT,
    )

    # Transport carried the corrected ceiling.
    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == 8_000

    corrected_sha = build_provider_request_identity(
        request_body=sent
    ).request_sha256
    legacy_body = copy.deepcopy(sent)
    legacy_body["max_completion_tokens"] = 3_000
    legacy_sha = build_provider_request_identity(
        request_body=legacy_body
    ).request_sha256
    assert corrected_sha != legacy_sha

    # Journal identity was computed from the corrected body, not the legacy one.
    journals = sorted(
        (tmp_path / "journals").glob("0001.g7c_state_extraction*.json")
    )
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["request_sha256"] == corrected_sha
    assert journal["request_sha256"] != legacy_sha
    assert journal["status"] == "response_recorded"
    assert journal["operation_kind"] == STATE_EXTRACTION
    assert journal["chapter"] == CHAPTER

    # The ledger and its preflight saw the corrected body.
    records = ledger.document["records"]
    assert len(records) == 1
    assert records[0]["request_sha256"] == corrected_sha
    assert records[0]["logical_operation"] == STATE_EXTRACTION
    assert records[0]["status"] == "accepted"
    assert ledger.new_provider_sends_started == 1

    # The corrected identity is the journal identity the engine reuses.
    identity = request_identity(
        operation_kind=STATE_EXTRACTION,
        novel_id=plan["g7e_plan"]["novel_id"],
        chapter=CHAPTER,
        provider=PROVIDER_NAME,
        model=sent["model"],
        profile_id="fixture",
        source_sha256=hashlib.sha256(
            SOURCE_TEXT.encode("utf-8")
        ).hexdigest(),
        translation_sha256=hashlib.sha256(
            TRANSLATION_TEXT.encode("utf-8")
        ).hexdigest(),
        request_payload=sent,
    )
    assert journal["operation_id"] == identity["operation_id"]


def test_aux_budget_drift_fails_closed_before_journal_ledger_transport(tmp_path):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(content="{}", finish_reason="stop"),
        goat_auxiliary_policy=True,
    )
    model_id = plan["g7e_plan"]["model_id"]

    with pytest.raises(
        g7f.G7FExecutionError,
        match="GOAT auxiliary 8K completion budget",
    ):
        module.journaled_chat_request(
            STATE_EXTRACTION,
            CHAPTER,
            SOURCE_TEXT,
            _legacy_kwargs(model_id, max_tokens=1_234),
            translation_text=TRANSLATION_TEXT,
        )

    assert completions.calls == []
    assert ledger.new_provider_sends_started == 0
    assert not (tmp_path / "journals").exists()


@pytest.mark.parametrize(
    "completion_tokens,expected_status",
    (
        # The frozen historical response: 3,000 tokens at the legacy 3,000
        # ceiling, i.e. below the corrected 8,000 ceiling once the budget is
        # corrected.
        (INCIDENT_COMPLETION_TOKENS, g7j.AUXILIARY_EMPTY_CONTENT_LENGTH),
        # A response that exhausts the corrected 8,000 ceiling exactly.
        (
            8_000,
            g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
        ),
    ),
)
def test_empty_length_auxiliary_response_can_never_reach_json_repair(
    tmp_path,
    completion_tokens,
    expected_status,
):
    """The incident response is rejected at the response-consumption point."""
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content=INCIDENT_CONTENT,
            finish_reason=INCIDENT_FINISH_REASON,
            completion_tokens=completion_tokens,
            reasoning_tokens=completion_tokens,
            reasoning_exposed=True,
        ),
        goat_auxiliary_policy=True,
    )
    model_id = plan["g7e_plan"]["model_id"]

    with pytest.raises(
        g7f.G7FExecutionError,
        match="auxiliary response rejected",
    ) as excinfo:
        module.journaled_chat_request(
            STATE_EXTRACTION,
            CHAPTER,
            SOURCE_TEXT,
            _legacy_kwargs(model_id, max_tokens=3_000),
            translation_text=TRANSLATION_TEXT,
        )
    assert expected_status in str(excinfo.value)
    # Every empty-content classification is fail-closed: the empty response can
    # never be handed to the engine's generic JSON repair branch.
    assert expected_status in g7j.AUXILIARY_FAIL_CLOSED_STATUSES

    # The rejection is post-persistence and definitely not a retry: exactly one
    # send happened, the durable response was recorded, and no second request,
    # second journal slot, or repair call exists.
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 8_000
    assert ledger.new_provider_sends_started == 1
    assert ledger.document["records"][0]["status"] == "accepted"
    journals = sorted((tmp_path / "journals").glob("*.json"))
    # Exactly one durable slot exists and it is the consumed fixed
    # state_extraction slot; no state_json_repair slot was ever created, so the
    # empty response never reached the engine's JSON repair branch.
    assert [path.name for path in journals] == [
        "0001.g7c_state_extraction.json"
    ]
    assert "state_json_repair" not in journals[0].name

    # A replay of the same durable slot is rejected identically with zero sends.
    with pytest.raises(g7f.G7FExecutionError, match="auxiliary response rejected"):
        module.journaled_chat_request(
            STATE_EXTRACTION,
            CHAPTER,
            SOURCE_TEXT,
            _legacy_kwargs(model_id, max_tokens=3_000),
            translation_text=TRANSLATION_TEXT,
        )
    assert len(completions.calls) == 1
    assert ledger.new_provider_sends_started == 1


def test_goat_auxiliary_response_guard_classifies_every_status():
    stats_ok = {"completion_tokens": 25}
    assert g7f.goat_auxiliary_response_guard(
        STATE_EXTRACTION,
        {
            "content": '{"new_terms": []}',
            "finish_reason": "stop",
            "stats": stats_ok,
        },
        4_000,
    )["status"] == g7j.AUXILIARY_STATE_ACCEPTED

    assert g7f.goat_auxiliary_response_guard(
        STATE_EXTRACTION,
        {
            "content": '{"new_terms": [',
            "finish_reason": "length",
            "stats": {"completion_tokens": 3_000},
        },
        4_000,
    )["status"] == g7j.AUXILIARY_MALFORMED_JSON_REPAIRED

    for content, finish_reason, completion, expected in (
        (
            "",
            "length",
            4_000,
            g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
        ),
        ("", "length", 10, g7j.AUXILIARY_EMPTY_CONTENT_LENGTH),
        ("", "stop", 10, g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH),
    ):
        with pytest.raises(g7f.G7FExecutionError, match=expected):
            g7f.goat_auxiliary_response_guard(
                STATE_EXTRACTION,
                {
                    "content": content,
                    "finish_reason": finish_reason,
                    "stats": {"completion_tokens": completion},
                },
                4_000,
            )
