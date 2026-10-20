"""G7P GOAT Stage-1 runtime auxiliary subrequest-compatibility tests.

Synthetic and self-contained: no network, no credentials, no Provider call, and
no live evidence directory. Every fixture is built under ``tmp_path`` with the
real file SHAPES. The injected ``FakeCompletions`` client is the only transport,
and every assertion about "transport_sends" counts those in-process fake calls.

The defect this file guards against: the frozen G7N 8K auxiliary policy accepted
only the per-operation frozen baselines plus the historical 4K/8K transition
totals, so the legitimate production auxiliary subrequests the real lifecycle
nests under the same logical context operation (glossary schema/semantic
correction at 2,000, generic JSON repair at 5,000, strict terminology / residual
CJK micro-repairs at 800) were rejected by the benchmark adapter before
transport.
"""

import ast
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7j_reasoning_budget as g7j
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import PROVIDER_NAME
from benchmark_longitudinal_provider import build_provider_request_identity
from production_paid_request_journal import request_identity


NOW = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)

CHAPTER = 1
SOURCE_TEXT = "Chapter 1 source text"
TRANSLATION_TEXT = "Chapter 1 committed English translation"

# The frozen identities that must not drift. The G7N 8K policy SHA is the base
# the G7P runtime policy binds; the G7F executor and G7J/4K identities stay
# byte-frozen.
FROZEN_G7J_POLICY_SHA256 = (
    "13d8c63f629f26f6e4bc098deba80a865075276859ab858a3e2c8ddc03b0539a"
)
FROZEN_G7J_AUXILIARY_8K_POLICY_SHA256 = (
    "9c6829a834e6fe76711b1711d1d39158c627c74a2c632e91aa50fd222f9f65e2"
)
FROZEN_G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256 = (
    "1246c5a6ec736e5e3246c02536eb72cb6a088a99f08f0bf5bcb2795d91f1a4e9"
)
FROZEN_G7F_EXECUTOR_SHA256 = (
    "7000b15e953b70312247322df0748f403826aba31118176a87186cd76e380219"
)

PRODUCTION_SCRIPT_NAME = g7f.PRODUCTION_SCRIPT_NAME

# Centralized, not duplicated: the whitelist under test is the module's own
# production subrequest-ceiling table.
PRODUCTION_SUBREQUEST_TABLE = tuple(
    g7j.GOAT_AUXILIARY_STAGE1_RUNTIME_PRODUCTION_SUBREQUEST_CEILING_COMPLETION_TOKENS
)

# Derive the context entrypoint and the direct legacy kind for every logical
# operation from the frozen G7C mappings instead of hard-coding names.
_CONTEXT_ENTRYPOINT_FOR_OPERATION = {}
for _name, _operation in g7f.CONTEXT_ENTRYPOINTS.items():
    _CONTEXT_ENTRYPOINT_FOR_OPERATION.setdefault(_operation, _name)

_DIRECT_KIND_FOR_OPERATION = {
    operation: kind
    for kind, operation in g7f.DIRECT_LEGACY_OPERATION_MAP.items()
}


def _recognized(operation, incoming):
    """Return whether the centralized whitelist recognizes one incoming ceiling."""
    return incoming in (
        {
            requested
            for listed_operation, requested in PRODUCTION_SUBREQUEST_TABLE
            if listed_operation == operation
        }
        | set(g7j._goat_auxiliary_8k_accepted_incoming_tokens(operation))
    )


def _clock():
    return NOW


class _TransportFailure(RuntimeError):
    pass


def _failing_factory(_kwargs):
    raise _TransportFailure("synthetic transport failure")


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
    content="{}",
    finish_reason="stop",
    completion_tokens=25,
    prompt_tokens=100,
):
    def factory(kwargs):
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
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


def _plan(*, run_index=1, sends=8, cost="1", target=CHAPTER):
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


def _module(*, model_id, journal_dir, novel_id):
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
        "messages": [{"role": "user", "content": "auxiliary payload"}],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _runtime(
    tmp_path,
    *,
    operation,
    incoming,
    factory=None,
    failing=False,
    goat=True,
    sends=8,
):
    """Install a Stage-1 runtime and return the invocation plumbing.

    Returns ``(module, plan, ledger, completions, request_kwargs)``. The injected
    fake client is the only transport; nothing here touches the network,
    credentials, or the real production state.
    """
    use_factory = _failing_factory if failing else (factory or _provider_response())
    plan = _plan(sends=sends)
    model_id = plan["g7e_plan"]["model_id"]
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    journal_dir = tmp_path / "journals"
    module = _module(
        model_id=model_id,
        journal_dir=journal_dir,
        novel_id=plan["g7e_plan"]["novel_id"],
    )
    request_kwargs = _legacy_kwargs(model_id, max_tokens=incoming)

    entrypoint = _CONTEXT_ENTRYPOINT_FOR_OPERATION.get(operation)
    if entrypoint is not None:

        def wrapped(*_args, **_kwargs):
            return module.call_api_with_retries(
                **copy.deepcopy(request_kwargs)
            )

        setattr(module, entrypoint, wrapped)

    client, completions = _fake_client(use_factory)
    g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=client,
        budget_ledger=ledger,
        clock=_clock,
        goat_auxiliary_policy=goat,
    )
    return module, plan, ledger, completions, request_kwargs


def _invoke(module, operation, request_kwargs):
    """Perform exactly one logical request for the current chapter."""
    entrypoint = _CONTEXT_ENTRYPOINT_FOR_OPERATION.get(operation)
    if entrypoint is not None:
        return getattr(module, entrypoint)(CHAPTER)
    return module.journaled_chat_request(
        _DIRECT_KIND_FOR_OPERATION[operation],
        CHAPTER,
        SOURCE_TEXT,
        copy.deepcopy(request_kwargs),
        translation_text=TRANSLATION_TEXT,
    )


# --------------------------------------------------------------------------
# Identity: additive, versioned, and base-frozen
# --------------------------------------------------------------------------


def test_g7p_identity_is_additive_and_bases_are_frozen():
    # The base G7N 8K policy and the 4K/primary payloads are byte-frozen.
    assert g7j.G7J_POLICY_SHA256 == FROZEN_G7J_POLICY_SHA256
    assert (
        g7j.G7J_AUXILIARY_8K_POLICY_SHA256
        == FROZEN_G7J_AUXILIARY_8K_POLICY_SHA256
    )
    assert g7f.G7F_EXECUTOR_SHA256 == FROZEN_G7F_EXECUTOR_SHA256

    # The additive runtime policy is separately versioned and binds the frozen
    # G7N 8K policy as its unchanged base.
    assert (
        g7j.G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256
        == FROZEN_G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256
    )
    assert g7j.GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_SHA256 == (
        g7j.G7J_AUXILIARY_8K_POLICY_SHA256
    )
    assert g7j.GOAT_AUXILIARY_STAGE1_RUNTIME_BASE_POLICY_REVISION == (
        g7j.GOAT_AUXILIARY_8K_REVISION
    )
    assert g7j.GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION == (
        "goat-g7j-auxiliary-8k-stage1-runtime-subrequest-compat-v1"
    )

    identity = g7j.g7p_stage1_runtime_auxiliary_policy_identity()
    assert identity["goat_only"] is True
    assert identity["corrected_total_completion_tokens"] == 8_000
    assert identity["production_auxiliary_completion_tokens"] == 3_000
    assert identity["production_state_checkpoint_identity_ceiling"] == 3_000
    assert identity["goat_state_checkpoint_identity_ceiling"] == 8_000
    assert identity["translation_total_completion_tokens"] == 24_000
    assert identity["max_selection_used"] is False
    assert identity["arbitrary_below_total_accepted"] is False
    assert identity["incoming_baseline_drift_fails_closed"] is True
    assert identity["explicitly_unsupported_completion_tokens"] == [32_000]
    assert identity["base_policy"] == {
        "revision": g7j.GOAT_AUXILIARY_8K_REVISION,
        "sha256": g7j.G7J_AUXILIARY_8K_POLICY_SHA256,
        "additive": True,
        "reinterpreted": False,
    }
    assert identity["provider_authorization"] == {
        "provider_calls_authorized": False,
        "provider_calls_observed": 0,
        "credential_access": False,
        "retry_authorized": False,
    }
    assert identity["scope"]["production_policy_changed"] is False
    assert identity["scope"]["historical_8k_policy_changed"] is False
    assert identity["scope"]["historical_4k_policy_changed"] is False
    assert identity["scope"]["applied_before_request_identity"] is True
    # The frozen G7F executor identity gains no G7P field.
    assert "g7p" not in json.dumps(g7f.g7f_executor_identity()).lower()


def test_runtime_accepted_set_is_exactly_g7n_set_union_production_whitelist():
    production_by_operation = {
        operation: {
            requested
            for listed_operation, requested in PRODUCTION_SUBREQUEST_TABLE
            if listed_operation == operation
        }
        for operation in g7j.GOAT_AUXILIARY_OPERATIONS
    }
    for operation in g7j.GOAT_AUXILIARY_OPERATIONS:
        frozen_8k = g7j._goat_auxiliary_8k_accepted_incoming_tokens(operation)
        runtime = (
            g7j._goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
                operation
            )
        )
        assert runtime == frozen_8k | production_by_operation[operation]


# --------------------------------------------------------------------------
# A / C / D: accepted ceilings, primary translation, production passthrough
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,incoming",
    tuple(PRODUCTION_SUBREQUEST_TABLE),
    ids=lambda value: str(value),
)
def test_production_subrequest_ceiling_accepted_and_rewritten_to_8000(
    operation, incoming
):
    assert g7j.goat_auxiliary_stage1_runtime_completion_budget(
        logical_operation=operation,
        requested_tokens=incoming,
    ) == 8_000


def test_primary_translation_unchanged_and_32000_rejected():
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


def test_primary_translation_runtime_transport_is_24000_and_32000_rejected(
    tmp_path,
):
    module, _plan_value, ledger, completions, _kwargs = _runtime(
        tmp_path, operation="state_extraction", incoming=3_000
    )
    model_id = _plan()["g7e_plan"]["model_id"]

    module.journaled_chat_request(
        "chapter_translation",
        CHAPTER,
        SOURCE_TEXT,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 24_000

    with pytest.raises(g7f.G7FExecutionError):
        module.journaled_chat_request(
            "chapter_translation",
            CHAPTER,
            SOURCE_TEXT,
            _legacy_kwargs(model_id, max_tokens=32_000),
            translation_text=TRANSLATION_TEXT,
        )
    # The rejected 32,000 baseline never reached transport.
    assert len(completions.calls) == 1
    assert ledger.new_provider_sends_started == 1


def test_production_non_goat_auxiliary_ceilings_unchanged():
    for operation, incoming in PRODUCTION_SUBREQUEST_TABLE:
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=incoming,
            reasoning_effort="low",
        ) == incoming
        assert g7j.translation_completion_budget(
            logical_operation=operation,
            requested_tokens=incoming,
            reasoning_effort=None,
        ) == incoming


def test_production_non_goat_runtime_transport_keeps_original_ceiling(tmp_path):
    module, _plan_value, _ledger, completions, request_kwargs = _runtime(
        tmp_path,
        operation="glossary_review",
        incoming=2_000,
        goat=False,
    )
    _invoke(module, "glossary_review", request_kwargs)
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 2_000


@pytest.mark.parametrize(
    "operation,incoming",
    tuple(PRODUCTION_SUBREQUEST_TABLE),
    ids=lambda value: str(value),
)
def test_valid_production_subrequest_never_fails_at_the_adapter(
    tmp_path, operation, incoming
):
    """The real lifecycle request must reach transport with the 8K ceiling."""
    module, _plan_value, ledger, completions, request_kwargs = _runtime(
        tmp_path,
        operation=operation,
        incoming=incoming,
    )
    _invoke(module, operation, request_kwargs)

    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 8_000
    assert ledger.new_provider_sends_started == 1


# --------------------------------------------------------------------------
# B / H: closure, drift, and zero-provider fail-closed behavior
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    tuple(g7j.GOAT_AUXILIARY_OPERATIONS),
)
def test_unrecognized_ceiling_fails_closed_in_the_policy(operation):
    for drifted in (1, 2_999, 3_001, 6_000, 7_999, 12_000, 24_000, 32_000):
        if _recognized(operation, drifted):
            continue
        with pytest.raises(g7j.G7JPolicyError, match="drift|unsupported"):
            g7j.goat_auxiliary_stage1_runtime_completion_budget(
                logical_operation=operation,
                requested_tokens=drifted,
            )


@pytest.mark.parametrize(
    "operation",
    tuple(g7j.GOAT_AUXILIARY_OPERATIONS),
)
def test_unrecognized_ceiling_performs_zero_provider_calls(tmp_path, operation):
    drifted = 1_234
    assert not _recognized(operation, drifted)

    module, _plan_value, ledger, completions, request_kwargs = _runtime(
        tmp_path,
        operation=operation,
        incoming=drifted,
    )
    with pytest.raises(
        g7f.G7FExecutionError,
        match="GOAT auxiliary 8K completion budget",
    ):
        _invoke(module, operation, request_kwargs)

    assert completions.calls == []
    assert ledger.new_provider_sends_started == 0
    assert ledger.document["records"] == []
    assert not (tmp_path / "journals").exists()


# --------------------------------------------------------------------------
# E / F: frozen 4K and 8K policies are untouched
# --------------------------------------------------------------------------


def test_frozen_4k_policy_is_unchanged():
    assert g7j.GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS == 4_000
    baselines = dict(g7j.GOAT_AUXILIARY_INCOMING_BASELINE_COMPLETION_TOKENS)
    for operation in g7j.GOAT_AUXILIARY_OPERATIONS:
        assert g7j.goat_auxiliary_completion_budget(
            logical_operation=operation,
            requested_tokens=baselines[operation],
        ) == 4_000
        assert g7j.goat_auxiliary_completion_budget(
            logical_operation=operation,
            requested_tokens=3_000,
        ) == 4_000
    policy = g7j.g7j_policy_identity()["auxiliary_completion_budget_policy"]
    assert policy["corrected_goat_auxiliary_total"] == 4_000
    # The 4K policy never accepts the new runtime production subrequest ceilings.
    with pytest.raises(g7j.G7JPolicyError):
        g7j.goat_auxiliary_completion_budget(
            logical_operation="glossary_review",
            requested_tokens=2_000,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.goat_auxiliary_completion_budget(
            logical_operation="terminology_repair",
            requested_tokens=800,
        )


def test_frozen_8k_policy_is_unchanged():
    assert (
        g7j.G7J_AUXILIARY_8K_POLICY_SHA256
        == FROZEN_G7J_AUXILIARY_8K_POLICY_SHA256
    )
    for operation in g7j.GOAT_AUXILIARY_OPERATIONS:
        for incoming in g7j._goat_auxiliary_8k_accepted_incoming_tokens(
            operation
        ):
            assert g7j.goat_auxiliary_8k_completion_budget(
                logical_operation=operation,
                requested_tokens=incoming,
            ) == 8_000
    # The frozen G7N 8K policy rejects the two ceilings that provoked G7P.
    with pytest.raises(g7j.G7JPolicyError):
        g7j.goat_auxiliary_8k_completion_budget(
            logical_operation="glossary_review",
            requested_tokens=2_000,
        )
    with pytest.raises(g7j.G7JPolicyError):
        g7j.goat_auxiliary_8k_completion_budget(
            logical_operation="terminology_repair",
            requested_tokens=800,
        )
    identity = g7j.g7j_auxiliary_8k_policy_identity()
    assert identity["revision"] == g7j.GOAT_AUXILIARY_8K_REVISION
    assert identity["historical_4k_policy_unchanged"] == {
        "corrected_goat_auxiliary_total": 4_000,
        "legacy_baseline": 3_000,
    }


def test_g7n_and_g7o_historical_bindings_still_resolve():
    import benchmark_longitudinal_g7n_ch1_state_recovery_plan as g7n
    import benchmark_longitudinal_g7o_ch1_state_recovery_executor as g7o

    assert g7n.POLICY_REVISION == g7j.GOAT_AUXILIARY_8K_REVISION
    assert g7o.G7O_AUXILIARY_POLICY_BINDING["auxiliary_policy_revision"] == (
        g7j.GOAT_AUXILIARY_8K_REVISION
    )
    assert g7o.G7O_AUXILIARY_POLICY_BINDING["auxiliary_policy_sha256"] == (
        g7j.G7J_AUXILIARY_8K_POLICY_SHA256
    )
    assert g7o.RECOVERY_MAX_COMPLETION_TOKENS == 8_000
    assert g7j.goat_auxiliary_8k_completion_budget(
        logical_operation=g7o.RECOVERY_LOGICAL_OPERATION,
        requested_tokens=(
            g7j.GOAT_AUXILIARY_8K_PREVIOUS_CORRECTED_COMPLETION_TOKENS
        ),
    ) == 8_000


# --------------------------------------------------------------------------
# G: applied before request identity, journal identity, ledger, transport
# --------------------------------------------------------------------------


def test_runtime_policy_is_applied_before_identity_journal_ledger_transport(
    tmp_path,
):
    operation = "glossary_review"
    incoming = 2_000
    module, plan, ledger, completions, request_kwargs = _runtime(
        tmp_path, operation=operation, incoming=incoming
    )

    result = _invoke(module, operation, request_kwargs)

    # 1. Transport carried the corrected ceiling.
    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == 8_000

    # 2. Provider request identity was computed from the corrected body.
    corrected_sha = build_provider_request_identity(
        request_body=sent
    ).request_sha256
    legacy_body = copy.deepcopy(sent)
    legacy_body["max_completion_tokens"] = incoming
    legacy_sha = build_provider_request_identity(
        request_body=legacy_body
    ).request_sha256
    assert corrected_sha != legacy_sha

    # 3. Journal identity binding used the corrected body.
    journals = sorted((tmp_path / "journals").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["request_sha256"] == corrected_sha
    assert journal["request_sha256"] != legacy_sha
    assert journal["status"] == "response_recorded"
    assert journal["operation_kind"] == operation

    source_text = "\n".join(
        str(item.get("content", ""))
        for item in request_kwargs["messages"]
        if isinstance(item, dict)
    )
    identity = request_identity(
        operation_kind=operation,
        novel_id=plan["g7e_plan"]["novel_id"],
        chapter=CHAPTER,
        provider=PROVIDER_NAME,
        model=sent["model"],
        profile_id="fixture",
        source_sha256=hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        translation_sha256="",
        request_payload=sent,
    )
    assert journal["operation_id"] == identity["operation_id"]

    # 4. Ledger preflight/record saw the corrected body's request identity.
    records = ledger.document["records"]
    assert len(records) == 1
    assert records[0]["request_sha256"] == corrected_sha
    assert records[0]["logical_operation"] == operation
    assert records[0]["status"] == "accepted"
    assert ledger.new_provider_sends_started == 1
    # The engine-visible response envelope was consumed exactly once.
    assert result.choices[0].message.content == "{}"


# --------------------------------------------------------------------------
# J / K: exactly-once and no hidden retries
# --------------------------------------------------------------------------


def test_exactly_once_single_send_and_replay_reuses_durable_slot(tmp_path):
    # A nested context-operation subrequest (terminology micro-repair at 800)
    # reaches transport exactly once and a replay adds no Provider request.
    module, _plan_value, ledger, completions, request_kwargs = _runtime(
        tmp_path, operation="terminology_repair", incoming=800
    )
    _invoke(module, "terminology_repair", request_kwargs)
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 8_000
    journals_before = sorted((tmp_path / "journals").glob("*.json"))
    assert len(journals_before) == 1

    _invoke(module, "terminology_repair", request_kwargs)
    assert len(completions.calls) == 1
    assert ledger.new_provider_sends_started == 1
    assert sorted((tmp_path / "journals").glob("*.json")) == journals_before

    # A direct auxiliary operation reports its durable replay explicitly.
    direct_dir = tmp_path / "direct"
    module2, _plan2, ledger2, completions2, kwargs2 = _runtime(
        direct_dir, operation="state_extraction", incoming=3_000
    )
    first = _invoke(module2, "state_extraction", kwargs2)
    assert first["replayed"] is False
    assert len(completions2.calls) == 1
    second = _invoke(module2, "state_extraction", kwargs2)
    assert second["replayed"] is True
    assert len(completions2.calls) == 1
    assert ledger2.new_provider_sends_started == 1


def test_transport_exception_is_single_attempt_with_one_started_record(tmp_path):
    module, _plan_value, ledger, completions, request_kwargs = _runtime(
        tmp_path,
        operation="glossary_review",
        incoming=2_000,
        failing=True,
    )

    with pytest.raises(BaseException):
        _invoke(module, "glossary_review", request_kwargs)

    assert len(completions.calls) == 1
    records = ledger.document["records"]
    assert len(records) == 1
    assert records[0]["status"] == "transport_exception"
    assert ledger.new_provider_sends_started == 1


# --------------------------------------------------------------------------
# Centralized-constant coverage of the real production call sites
# --------------------------------------------------------------------------


def test_whitelist_covers_production_call_site_literals():
    """Every literal production ``max_tokens=`` ceiling is recognized.

    The whitelist must not omit a real call site, and must not lean on
    unexplained magic numbers: this test derives the call-site literals from the
    production script itself and requires each to be a recognized accepted
    ceiling of the runtime policy.
    """
    script = Path(__file__).resolve().parents[1] / PRODUCTION_SCRIPT_NAME
    tree = ast.parse(script.read_text(encoding="utf-8"))

    literals = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in ("max_tokens", "max_completion_tokens"):
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                literals.add(value.value)

    assert literals, "production script exposes no literal token ceilings"
    # The primary translation ceiling is a named constant, not a literal.
    assert 12_000 not in literals

    recognized = set()
    for operation in g7j.GOAT_AUXILIARY_OPERATIONS:
        recognized |= (
            g7j._goat_auxiliary_stage1_runtime_accepted_incoming_tokens(
                operation
            )
        )
    assert literals <= recognized, sorted(literals - recognized)


# --------------------------------------------------------------------------
# Runtime-state reporting
# --------------------------------------------------------------------------


def test_runtime_state_reports_the_applied_runtime_policy(tmp_path):
    module, _plan_value, _ledger, _completions, _kwargs = _runtime(
        tmp_path, operation="state_extraction", incoming=3_000
    )
    state = module._g7f_stage1_runtime_state
    assert state["goat_auxiliary_policy"] is True
    assert state["goat_auxiliary_total_completion_tokens"] == 8_000
    assert state["goat_auxiliary_runtime_policy_revision"] == (
        g7j.GOAT_AUXILIARY_STAGE1_RUNTIME_REVISION
    )
    assert state["goat_auxiliary_runtime_policy_sha256"] == (
        g7j.G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256
    )
    assert state["goat_auxiliary_runtime_subrequest_compat_applied"] is True
    # The frozen G7N 8K base binding is still reported unchanged.
    assert state["goat_auxiliary_policy_revision"] == (
        g7j.GOAT_AUXILIARY_8K_REVISION
    )
    assert state["goat_auxiliary_policy_sha256"] == (
        g7j.G7J_AUXILIARY_8K_POLICY_SHA256
    )
    assert state["production_auxiliary_budget_unchanged"] is True
