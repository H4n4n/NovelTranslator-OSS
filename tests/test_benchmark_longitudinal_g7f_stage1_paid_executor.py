import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7d_accounting_policy as g7d
import benchmark_longitudinal_g7e_paid_authorization as g7e
import benchmark_longitudinal_g7j_reasoning_budget as g7j
import production_paid_request_journal as paid_journal
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS, PROVIDER_NAME
from benchmark_longitudinal_g7a_readiness import TRANSLATION_EFFORT_BY_MODEL
from benchmark_longitudinal_provider import (
    ProviderUsage,
    build_provider_request_identity,
)
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    PaidRequestJournalError,
    PaidRequestUncertainError,
    inspect_paid_request_journals,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _clock():
    return NOW


def _plan(
    *,
    sends=2,
    cost="1",
    target=1,
    approval_token="a" * 64,
    run_index=1,
):
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
        "approval_token": approval_token,
        "g7e_plan": nested,
        "provider_calls": 0,
    }
    plan["plan_sha256"] = g7f._sha256_json(plan)
    return plan


def test_staged_source_type_uses_canonical_epub_default(tmp_path):
    config = {
        "id": "fixture_novel",
        "title": "Fixture Novel",
        "source_language": "zh",
        "target_language": "en",
        "profile": "historical_military",
        "final_chapter": 30,
        "audit_interval": 25,
        "chapter_numbering": "global",
    }
    path = tmp_path / "novel.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    assert g7f._staged_source_type(path) == "epub"


def test_staged_source_type_preserves_explicit_txt(tmp_path):
    config = {
        "id": "fixture_novel",
        "title": "Fixture Novel",
        "source_language": "ko",
        "target_language": "en",
        "profile": "default",
        "final_chapter": 30,
        "source_type": "txt",
    }
    path = tmp_path / "novel.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    assert g7f._staged_source_type(path) == "txt"


def test_staged_source_type_rejects_invalid_config(tmp_path):
    config = {
        "id": "fixture_novel",
        "title": "Fixture Novel",
        "source_language": "zh",
        "target_language": "en",
        "profile": "historical_military",
        "final_chapter": 30,
        "source_type": "pdf",
    }
    path = tmp_path / "novel.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(g7f.G7FExecutionError, match="staged novel config is invalid"):
        g7f._staged_source_type(path)


def _request(model_id):
    provider_model = live_provider_capability(model_id).provider_model_id
    return {
        "model": provider_model,
        "messages": [{"role": "user", "content": "translate"}],
        "max_completion_tokens": 100,
    }


def _usage(
    prompt=100,
    completion=25,
    cached=0,
    cached_exposed=True,
):
    return ProviderUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached if cached_exposed else None,
        cached_tokens_exposed=cached_exposed,
        total_tokens=prompt + completion,
    )


def test_g7f_identity_is_zero_provider_and_direct_transport():
    identity = g7f.g7f_executor_identity()

    assert identity["provider_calls"] == 0
    assert identity["revision"] == "goat-g7f-stage1-paid-executor-v2"
    assert identity["repository_anchor"] == (
        "8184eb05d4288099516a43261e400addffd5defb"
    )
    assert identity["predecessor"]["g7c_integration_sha256"] == (
        "658f2477f3b538d7ebe792087312d74dbe372a37fd7b6f8758c151284d56f814"
    )
    assert identity["predecessor"]["g7d_accounting_sha256"] == (
        "1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0"
    )
    scope = identity["execution_scope"]
    assert scope["one_external_run_per_invocation"] is True
    assert scope["one_target_chapter_per_invocation"] is True
    assert scope["run_unattended_production_target_only"] is True
    assert scope["automatic_chapter_advance"] is False
    assert scope["coding_agent_used_for_inference"] is False
    assert scope["provider_client_max_retries"] == 0
    assert scope["production_paid_request_journal_is_retry_owner"] is True


def test_plan_envelope_is_tamper_evident():
    plan = _plan()
    assert g7f._validate_plan_envelope(plan)["plan_sha256"] == plan["plan_sha256"]

    tampered = copy.deepcopy(plan)
    tampered["target_chapter"] = 2
    with pytest.raises(g7f.G7FExecutionError, match="does not recompute"):
        g7f._validate_plan_envelope(tampered)


def test_budget_ledger_persists_send_and_accepted_g7d_cost(tmp_path):
    plan = _plan()
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])
    request_sha = g7f._sha256_json(request)

    started = ledger.begin_send(
        logical_operation="translation",
        chapter=1,
        request_sha256=request_sha,
        request_body=request,
        request_started_utc=NOW,
    )
    stats = ledger.record_accepted_usage(
        send_index=started["send_index"],
        request_sha256=request_sha,
        usage=_usage(),
        request_started_utc=NOW,
    )

    assert ledger.new_provider_sends_started == 1
    assert ledger.accepted_cost > 0
    assert stats["pricing_version"] == g7f.PRICING_VERSION
    assert stats["rate_correct_cost_usd"] == stats["cost_usd"]

    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
        allow_create=False,
    )
    assert reloaded.new_provider_sends_started == 1
    assert reloaded.accepted_cost == ledger.accepted_cost
    assert reloaded.document["records"][0]["status"] == "accepted"


def test_budget_send_ceiling_survives_restart(tmp_path):
    plan = _plan(sends=1)
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])
    request_sha = g7f._sha256_json(request)

    ledger.begin_send(
        logical_operation="translation",
        chapter=1,
        request_sha256=request_sha,
        request_body=request,
        request_started_utc=NOW,
    )

    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
        allow_create=False,
    )
    with pytest.raises(g7f.G7FBudgetError, match="send ceiling"):
        reloaded.preflight_next_send(
            request_body=request,
            request_started_utc=NOW,
        )


def test_budget_cost_ceiling_blocks_before_send_is_recorded(tmp_path):
    plan = _plan(cost="0.000000000001")
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])

    with pytest.raises(g7f.G7FBudgetError, match="cost ceiling"):
        ledger.begin_send(
            logical_operation="translation",
            chapter=1,
            request_sha256="b" * 64,
            request_body=request,
            request_started_utc=NOW,
        )

    assert ledger.new_provider_sends_started == 0
    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
        allow_create=False,
    )
    assert reloaded.new_provider_sends_started == 0


def test_transport_exception_consumes_persisted_send_budget(tmp_path):
    plan = _plan(sends=2)
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])
    request_sha = g7f._sha256_json(request)

    started = ledger.begin_send(
        logical_operation="translation",
        chapter=1,
        request_sha256=request_sha,
        request_body=request,
        request_started_utc=NOW,
    )
    ledger.record_transport_exception(
        send_index=started["send_index"],
        request_sha256=request_sha,
        error=TimeoutError("ambiguous timeout"),
    )

    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
        allow_create=False,
    )
    assert reloaded.new_provider_sends_started == 1
    assert reloaded.document["records"][0]["status"] == "transport_exception"
    assert reloaded.document["records"][0]["error"]["type"] == "TimeoutError"


def test_over_ceiling_response_is_persisted_and_reloadable_but_blocks_more(tmp_path):
    plan = _plan(cost="0.000001", sends=2)
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])

    # Bypass the normal conservative preflight only for this synthetic
    # post-response accounting test by using a tiny started record directly.
    request_sha = "c" * 64
    ledger.document["new_provider_sends_started"] = 1
    ledger.document["records"].append(
        {
            "send_index": 1,
            "logical_operation": "translation",
            "chapter": 1,
            "request_sha256": request_sha,
            "request_started_utc": NOW.isoformat(),
            "status": "started",
            "conservative_cost_bound_usd": "0",
            "accepted_cost_usd": "0",
            "stats": {},
            "error": {},
        }
    )
    ledger._persist()

    with pytest.raises(g7f.G7FBudgetError, match="exceeded"):
        ledger.record_accepted_usage(
            send_index=1,
            request_sha256=request_sha,
            usage=_usage(prompt=1000000, completion=1000000),
            request_started_utc=NOW,
        )

    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
        allow_create=False,
    )
    assert reloaded.document["records"][0]["status"] == (
        "accepted_cost_ceiling_exceeded"
    )
    with pytest.raises(g7f.G7FBudgetError, match="cost ceiling"):
        reloaded.preflight_next_send(
            request_body=request,
            request_started_utc=NOW,
        )


def test_replay_requires_matching_accepted_ledger_record(tmp_path):
    plan = _plan()
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(plan["g7e_plan"]["model_id"])
    request_sha = g7f._sha256_json(request)

    started = ledger.begin_send(
        logical_operation="translation",
        chapter=1,
        request_sha256=request_sha,
        request_body=request,
        request_started_utc=NOW,
    )
    stats = ledger.record_accepted_usage(
        send_index=started["send_index"],
        request_sha256=request_sha,
        usage=_usage(),
        request_started_utc=NOW,
    )
    ledger.validate_replayed_response(
        request_sha256=request_sha,
        response={"stats": stats},
    )

    changed = copy.deepcopy(stats)
    changed["prompt_tokens"] += 1
    with pytest.raises(g7f.G7FBudgetError, match="differs"):
        ledger.validate_replayed_response(
            request_sha256=request_sha,
            response={"stats": changed},
        )


def test_no_operator_authorization_constructs_no_client_or_ledger(monkeypatch, tmp_path):
    plan = _plan()
    run_root = tmp_path / "run"
    run_root.mkdir()

    inspected = {
        "plan": plan,
        "binding": {"run_root": run_root},
        "chapter_state": {
            "journal_state": {"local_replay_only_count": 0}
        },
    }
    monkeypatch.setattr(
        g7f,
        "inspect_stage1_invocation_plan",
        lambda **_kwargs: inspected,
    )

    called = []
    monkeypatch.setattr(
        g7f,
        "create_provider_client",
        lambda **_kwargs: called.append("client"),
    )

    report = g7f.execute_stage1_invocation(
        code_root=tmp_path,
        stage1_root=tmp_path,
        plan=plan,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        operator_authorized=False,
    )

    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["provider_calls"] == 0
    assert called == []
    assert not g7f.invocation_ledger_path(run_root, 1).exists()


def test_authorized_execution_requires_explicit_api_key_before_client(monkeypatch, tmp_path):
    plan = _plan()
    run_root = tmp_path / "run"
    run_root.mkdir()

    inspected = {
        "plan": plan,
        "binding": {"run_root": run_root},
        "chapter_state": {
            "journal_state": {"local_replay_only_count": 0}
        },
    }
    monkeypatch.setattr(
        g7f,
        "inspect_stage1_invocation_plan",
        lambda **_kwargs: inspected,
    )

    called = []
    monkeypatch.setattr(
        g7f,
        "create_provider_client",
        lambda **_kwargs: called.append("client"),
    )

    with pytest.raises(g7f.G7FExecutionError, match="injected API key"):
        g7f.execute_stage1_invocation(
            code_root=tmp_path,
            stage1_root=tmp_path,
            plan=plan,
            expected_plan_sha256=plan["plan_sha256"],
            expected_approval_token=plan["approval_token"],
            operator_authorized=True,
            api_key=None,
        )

    assert called == []
    assert not g7f.invocation_ledger_path(run_root, 1).exists()


def test_install_runtime_uses_verified_provider_model_identity(monkeypatch, tmp_path):
    plan = _plan()
    model_id = plan["g7e_plan"]["model_id"]
    provider_model = live_provider_capability(model_id).provider_model_id
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )

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
        ACTIVE_NOVEL_CONFIG=SimpleNamespace(id=plan["g7e_plan"]["novel_id"]),
        ACTIVE_PROFILE=SimpleNamespace(id="fixture"),
        PAID_REQUEST_JOURNAL_DIR=tmp_path / "journals",
    )
    for name in g7f.CONTEXT_ENTRYPOINTS:
        setattr(module, name, lambda *a, **k: None)

    state = g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=SimpleNamespace(),
        budget_ledger=ledger,
        clock=_clock,
    )

    assert module.MODEL == provider_model
    assert module.AUTO_OFF_PEAK_ONLY is False
    assert state["coding_agent_used_for_inference"] is False
    assert state["provider_transport"] == "command_code_provider_direct"


def test_state_fingerprint_ignores_only_transport_operational_state(tmp_path):
    root = tmp_path / "run"
    (root / "data" / "provider_request_journal").mkdir(parents=True)
    (root / g7f.LEDGER_DIRECTORY).mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "data" / "progress.json").write_text(
        '{"last_completed":0,"next_chapter":1}',
        encoding="utf-8",
    )
    (root / ".production.lock").write_bytes(b"")

    before = g7f.state_fingerprint_sha256(root)

    (root / "data" / "provider_request_journal" / "x.json").write_text(
        '{"status":"planned"}',
        encoding="utf-8",
    )
    (root / g7f.LEDGER_DIRECTORY / "0001.json").write_text(
        '{"ledger":"changed"}',
        encoding="utf-8",
    )
    (root / "logs" / "run.log").write_text("changed", encoding="utf-8")
    (root / ".production.lock").write_bytes(b"x")

    assert g7f.state_fingerprint_sha256(root) == before

    (root / "data" / "progress.json").write_text(
        '{"last_completed":1,"next_chapter":2}',
        encoding="utf-8",
    )
    assert g7f.state_fingerprint_sha256(root) != before


# --------------------------------------------------------------------------
# G7J reasoning-aware completion budget and response diagnostics
# --------------------------------------------------------------------------

TRANSLATION_CHAPTER = 1
TRANSLATION_SOURCE = "Chapter 1 source text"


class FakeCompletions:
    def __init__(self, factory):
        self.calls = []
        self._factory = factory

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self._factory(kwargs)


def _fake_client(factory):
    completions = FakeCompletions(factory)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    ), completions


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


def _module(*, model_id, journal_dir, novel_id="ace_evolution"):
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


def _legacy_kwargs(model_id, *, max_tokens, content="translate this chapter"):
    return {
        "model": live_provider_capability(model_id).provider_model_id,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _runtime(tmp_path, *, run_index=1, sends=2, cost="1", factory):
    plan = _plan(sends=sends, cost=cost, run_index=run_index)
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
    client, completions = _fake_client(factory)
    g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=client,
        budget_ledger=ledger,
        clock=_clock,
    )
    return module, plan, ledger, completions


def test_g7f_revision_and_executor_identity_stay_frozen():
    assert g7f.G7F_REVISION == "goat-g7f-stage1-paid-executor-v2"
    identity = g7f.g7f_executor_identity()
    assert identity["revision"] == "goat-g7f-stage1-paid-executor-v2"
    assert identity["repository_anchor"] == (
        "8184eb05d4288099516a43261e400addffd5defb"
    )
    # G7J must extend executor behavior without leaking new fields into the
    # frozen G7F executor identity hash.
    assert set(identity) == {
        "schema_version",
        "revision",
        "repository_anchor",
        "predecessor",
        "execution_scope",
        "state_fingerprint",
        "budget",
        "authorization",
        "provider_calls",
    }
    assert "g7j" not in json.dumps(identity).lower()


def test_deepseek_translation_keeps_low_effort_and_receives_24000(tmp_path):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="translated prose", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    assert model_id == "deepseek/deepseek-v4.1-flash"

    result = module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )

    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == 24_000
    assert sent["reasoning_effort"] == "low"
    assert result["response"]["diagnostic"]["status"] == "ok"
    assert result["response"]["diagnostic"]["max_completion_tokens"] == 24_000
    assert ledger.new_provider_sends_started == 1


def test_default_only_translation_gets_24000_with_no_reasoning_field(tmp_path):
    module, plan, _ledger, completions = _runtime(
        tmp_path,
        run_index=9,
        factory=_provider_response(
            content="translated prose", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    assert model_id == "tencent/hy3-paid"

    module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )

    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == 24_000
    assert "reasoning_effort" not in sent


@pytest.mark.parametrize("run_index", tuple(range(1, 13)))
def test_every_stage1_run_rewrites_primary_translation_to_24000(
    tmp_path, run_index
):
    """Roster-wide fairness: all twelve runs bind 12000 -> 24000 exactly."""
    module, plan, ledger, completions = _runtime(
        tmp_path / f"run-{run_index:02d}",
        run_index=run_index,
        factory=_provider_response(
            content="translated prose", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    assert model_id == BENCHMARK_MODEL_IDS[(run_index - 1) // 2]

    result = module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )

    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == 24_000
    expected_effort = TRANSLATION_EFFORT_BY_MODEL[model_id]
    if expected_effort is None:
        # Provider-native/default-only models never gain an invented field.
        assert "reasoning_effort" not in sent
    else:
        # The frozen G7B per-model reasoning binding is preserved exactly.
        assert sent["reasoning_effort"] == expected_effort
    assert result["response"]["diagnostic"]["status"] == "ok"
    assert (
        result["response"]["diagnostic"]["max_completion_tokens"] == 24_000
    )
    assert ledger.new_provider_sends_started == 1


@pytest.mark.parametrize("drifted", (16_000, 32_000))
def test_primary_translation_budget_drift_fails_closed_with_zero_sends(
    tmp_path, drifted
):
    """A non-frozen incoming baseline fails closed before identity or network."""
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="never sent", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]

    with pytest.raises(
        g7f.G7FExecutionError,
        match="reasoning-aware completion budget",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=drifted),
        )

    assert completions.calls == []
    assert ledger.new_provider_sends_started == 0
    assert not (tmp_path / "journals").exists()


def test_auxiliary_operation_budget_is_unchanged_and_undiagnosed(tmp_path):
    module, plan, _ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(content="{}", finish_reason="stop"),
    )
    model_id = plan["g7e_plan"]["model_id"]

    result = module.journaled_chat_request(
        "state_extraction",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=3_000),
        translation_text="translated prose",
    )

    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 3_000
    assert "diagnostic" not in result["response"]


def test_corrected_request_identity_differs_from_the_legacy_12000_body(tmp_path):
    module, plan, _ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="translated prose", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )

    corrected_body = completions.calls[0]
    legacy_body = copy.deepcopy(corrected_body)
    legacy_body["max_completion_tokens"] = 12_000

    corrected = build_provider_request_identity(
        request_body=corrected_body
    )
    legacy = build_provider_request_identity(request_body=legacy_body)
    assert corrected.request_sha256 != legacy.request_sha256


def test_persisted_old_response_recorded_mismatch_blocks_with_zero_sends(
    tmp_path,
):
    plan = _plan()
    model_id = plan["g7e_plan"]["model_id"]
    novel_id = plan["g7e_plan"]["novel_id"]
    journal_dir = tmp_path / "journals"
    legacy_kwargs = _legacy_kwargs(model_id, max_tokens=12_000)
    legacy_body = g7f.normalize_legacy_request(
        model_id=model_id,
        logical_operation="translation",
        request_kwargs=legacy_kwargs,
    )
    assert legacy_body["max_completion_tokens"] == 12_000

    legacy_identity = request_identity(
        operation_kind="translation",
        novel_id=novel_id,
        chapter=TRANSLATION_CHAPTER,
        provider=PROVIDER_NAME,
        model=legacy_body["model"],
        profile_id="fixture",
        source_sha256=hashlib.sha256(
            TRANSLATION_SOURCE.encode("utf-8")
        ).hexdigest(),
        translation_sha256="",
        request_payload=legacy_body,
    )
    legacy_journal_path = paid_request_journal_path(
        journal_dir, "g7c_translation", TRANSLATION_CHAPTER
    )

    def legacy_normalize(_raw, _context):
        return {
            "content": "",
            "finish_reason": "length",
            "stats": {"completion_tokens": 11_994},
            "provider_response_id": "legacy-response",
            "provider_response_model": legacy_body["model"],
            "request_sha256": legacy_identity["request_sha256"],
            "logical_operation": "translation",
        }

    run_journaled_paid_request(
        journal_path=legacy_journal_path,
        identity=legacy_identity,
        save_json=atomic_write_json,
        prepare_attempt=lambda: {"mode": "legacy_12000_fixture"},
        send_once=lambda: SimpleNamespace(),
        normalize_response=legacy_normalize,
    )

    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    module = _module(
        model_id=model_id,
        journal_dir=journal_dir,
        novel_id=novel_id,
    )
    client, completions = _fake_client(
        _provider_response(content="translated prose", finish_reason="stop")
    )
    g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=client,
        budget_ledger=ledger,
        clock=_clock,
    )

    journal_before = legacy_journal_path.read_bytes()
    ledger_before = (
        g7f.invocation_ledger_path(tmp_path, TRANSLATION_CHAPTER).read_bytes()
    )

    with pytest.raises(PaidRequestJournalError, match="identity mismatch"):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            legacy_kwargs,
        )

    assert completions.calls == []
    assert legacy_journal_path.read_bytes() == journal_before
    assert (
        g7f.invocation_ledger_path(tmp_path, TRANSLATION_CHAPTER).read_bytes()
        == ledger_before
    )
    legacy_record = json.loads(journal_before)
    assert legacy_record["status"] == "response_recorded"
    assert "diagnostic" not in legacy_record["response"]


_NO_PERSISTED_DIAGNOSTIC = object()


def _seed_legacy_translation_envelope(
    tmp_path,
    *,
    content,
    finish_reason,
    completion_tokens=25,
    diagnostic=_NO_PERSISTED_DIAGNOSTIC,
):
    """Seed a matching durable envelope for one corrected 24000 request.

    The identity is the corrected 24000 request identity. By default the recorded
    response is the earlier envelope shape that predates the bounded G7J
    diagnostic; ``diagnostic`` may instead seed a persisted (possibly wrong)
    diagnostic so its semantic validation can be exercised.
    """
    plan = _plan()
    model_id = plan["g7e_plan"]["model_id"]
    novel_id = plan["g7e_plan"]["novel_id"]
    journal_dir = tmp_path / "journals"
    legacy_kwargs = _legacy_kwargs(model_id, max_tokens=12_000)

    corrected_body = g7f.normalize_legacy_request(
        model_id=model_id,
        logical_operation="translation",
        request_kwargs=legacy_kwargs,
    )
    assert corrected_body["max_completion_tokens"] == 12_000
    corrected_body = copy.deepcopy(corrected_body)
    corrected_body["max_completion_tokens"] = 24_000
    provider_identity = build_provider_request_identity(
        request_body=corrected_body
    )

    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    started = ledger.begin_send(
        logical_operation="translation",
        chapter=TRANSLATION_CHAPTER,
        request_sha256=provider_identity.request_sha256,
        request_body=corrected_body,
        request_started_utc=NOW,
    )
    stats = ledger.record_accepted_usage(
        send_index=started["send_index"],
        request_sha256=provider_identity.request_sha256,
        usage=_usage(completion=completion_tokens),
        request_started_utc=NOW,
    )

    journal_path = paid_request_journal_path(
        journal_dir, "g7c_translation", TRANSLATION_CHAPTER
    )

    def legacy_normalize(_raw, _context):
        envelope = {
            "content": content,
            "finish_reason": finish_reason,
            "stats": copy.deepcopy(stats),
            "provider_response_id": "legacy-pre-diagnostic-envelope",
            "provider_response_model": corrected_body["model"],
            "request_sha256": provider_identity.request_sha256,
            "logical_operation": "translation",
        }
        if diagnostic is not _NO_PERSISTED_DIAGNOSTIC:
            envelope["diagnostic"] = copy.deepcopy(diagnostic)
        return envelope

    run_journaled_paid_request(
        journal_path=journal_path,
        identity=request_identity(
            operation_kind="translation",
            novel_id=novel_id,
            chapter=TRANSLATION_CHAPTER,
            provider=PROVIDER_NAME,
            model=provider_identity.requested_model,
            profile_id="fixture",
            source_sha256=hashlib.sha256(
                TRANSLATION_SOURCE.encode("utf-8")
            ).hexdigest(),
            translation_sha256="",
            request_payload=corrected_body,
        ),
        save_json=atomic_write_json,
        prepare_attempt=lambda: {"mode": "legacy_pre_diagnostic_envelope"},
        send_once=lambda: SimpleNamespace(),
        normalize_response=legacy_normalize,
    )

    durable = json.loads(journal_path.read_bytes())
    assert durable["status"] == "response_recorded"
    if diagnostic is _NO_PERSISTED_DIAGNOSTIC:
        assert "diagnostic" not in durable["response"]
    else:
        assert durable["response"]["diagnostic"] == diagnostic

    module = _module(
        model_id=model_id,
        journal_dir=journal_dir,
        novel_id=novel_id,
    )
    client, completions = _fake_client(
        _provider_response(content="never sent", finish_reason="stop")
    )
    g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=client,
        budget_ledger=ledger,
        clock=_clock,
    )
    return module, ledger, journal_path, completions, legacy_kwargs


def test_legacy_envelope_replay_is_classified_locally_without_resend(tmp_path):
    module, ledger, journal_path, completions, legacy_kwargs = (
        _seed_legacy_translation_envelope(
            tmp_path,
            content="translated prose",
            finish_reason="stop",
        )
    )
    journal_before = journal_path.read_bytes()

    result = module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        legacy_kwargs,
    )

    # Local classification reached the same verdict as the durable diagnostic
    # would have; the historical envelope is never rewritten or re-sent.
    assert completions.calls == []
    assert journal_path.read_bytes() == journal_before
    assert ledger.new_provider_sends_started == 1
    assert result["response"]["content"] == "translated prose"
    assert "diagnostic" not in result["response"]


def test_legacy_envelope_definitive_failure_is_rejected_locally(tmp_path):
    module, ledger, journal_path, completions, legacy_kwargs = (
        _seed_legacy_translation_envelope(
            tmp_path,
            content="",
            finish_reason="length",
            completion_tokens=11_994,
        )
    )
    journal_before = journal_path.read_bytes()

    # The same evidence is the exact historical Run-1 budget exhaustion at the
    # legacy 12000 ceiling, and a length-terminated empty response at the
    # corrected 24000 ceiling. Neither is retried.
    assert (
        g7f.classify_translation_response(
            content="",
            finish_reason="length",
            completion_tokens=11_994,
            max_completion_tokens=12_000,
        )["status"]
        == "completion_budget_exhausted_before_visible_content"
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="empty_visible_content_length_terminated",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            legacy_kwargs,
        )

    assert completions.calls == []
    assert journal_path.read_bytes() == journal_before
    assert ledger.new_provider_sends_started == 1
    assert [item["status"] for item in ledger.document["records"]] == [
        "accepted"
    ]


def test_response_envelope_round_trips_reasoning_diagnostics():

    base_stats = {
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 100,
        "cost_usd": 0.01,
        "rate_correct_cost_usd": 0.01,
        "pricing_version": g7f.PRICING_VERSION,
    }
    without = g7f._response_from_envelope(
        {"stats": copy.deepcopy(base_stats), "content": "x"}
    )
    assert not hasattr(without.usage, "completion_tokens_details")

    with_reasoning = g7f._response_from_envelope(
        {
            "stats": {
                **copy.deepcopy(base_stats),
                "reasoning_tokens": 150,
                "reasoning_tokens_exposed": True,
                "visible_completion_tokens": 50,
            },
            "content": "x",
        }
    )
    assert with_reasoning.usage.completion_tokens_details.reasoning_tokens == 150
    assert with_reasoning.usage.completion_tokens == 200

    with pytest.raises(g7f.G7FExecutionError, match="exposure metadata"):
        g7f._response_from_envelope(
            {
                "stats": {
                    **copy.deepcopy(base_stats),
                    "reasoning_tokens_exposed": 1,
                },
            }
        )
    with pytest.raises(g7f.G7FExecutionError, match="invalid reasoning_tokens"):
        g7f._response_from_envelope(
            {
                "stats": {
                    **copy.deepcopy(base_stats),
                    "reasoning_tokens_exposed": True,
                    "reasoning_tokens": None,
                },
            }
        )


def test_cost_is_identical_with_and_without_reasoning_diagnostics(tmp_path):
    plan = _plan()
    model_id = plan["g7e_plan"]["model_id"]
    request = _request(model_id)
    request_sha = g7f._sha256_json(request)

    def _account(directory, usage):
        ledger = g7f.InvocationBudgetLedger.open_or_create(
            run_root=directory,
            plan=plan,
            clock=_clock,
        )
        started = ledger.begin_send(
            logical_operation="translation",
            chapter=1,
            request_sha256=request_sha,
            request_body=request,
            request_started_utc=NOW,
        )
        return ledger.record_accepted_usage(
            send_index=started["send_index"],
            request_sha256=request_sha,
            usage=usage,
            request_started_utc=NOW,
        )

    silent = _account(tmp_path / "silent", _usage())
    reported = _account(
        tmp_path / "reported",
        ProviderUsage(
            prompt_tokens=100,
            completion_tokens=25,
            cached_tokens=0,
            cached_tokens_exposed=True,
            total_tokens=125,
            reasoning_tokens=10,
            reasoning_tokens_exposed=True,
        ),
    )

    assert silent["cost_usd"] == reported["cost_usd"]
    assert (
        silent["rate_correct_cost_usd"] == reported["rate_correct_cost_usd"]
    )
    assert "reasoning_tokens" not in silent
    assert "reasoning_tokens_exposed" not in silent
    assert reported["reasoning_tokens"] == 10
    assert reported["reasoning_tokens_exposed"] is True
    assert reported["visible_completion_tokens"] == 15


def test_run_1_like_empty_length_response_is_durable_then_rejected(
    tmp_path,
):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="",
            finish_reason="length",
            completion_tokens=11_994,
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    journal_path = paid_request_journal_path(
        tmp_path / "journals", "g7c_translation", TRANSLATION_CHAPTER
    )

    # The historical 12000 evidence classifies exactly as budget exhaustion.
    historical = g7f.classify_translation_response(
        content="",
        finish_reason="length",
        completion_tokens=11_994,
        max_completion_tokens=12_000,
    )
    assert historical["status"] == (
        "completion_budget_exhausted_before_visible_content"
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="empty_visible_content_length_terminated",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )

    assert len(completions.calls) == 1
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["status"] == "response_recorded"
    assert record["response"]["diagnostic"]["status"] == (
        "empty_visible_content_length_terminated"
    )
    assert record["response"]["diagnostic"]["max_completion_tokens"] == 24_000
    assert ledger.new_provider_sends_started == 1
    assert [item["status"] for item in ledger.document["records"]] == [
        "accepted"
    ]

    journal_bytes = journal_path.read_bytes()
    # Replay is local-only: the durable diagnostic is reused, no second send.
    with pytest.raises(
        g7f.G7FExecutionError,
        match="empty_visible_content_length_terminated",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )
    assert len(completions.calls) == 1
    assert journal_path.read_bytes() == journal_bytes
    assert ledger.new_provider_sends_started == 1


def test_empty_length_at_the_corrected_ceiling_is_budget_exhausted(tmp_path):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="",
            finish_reason="length",
            completion_tokens=23_994,
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]

    with pytest.raises(
        g7f.G7FExecutionError,
        match="completion_budget_exhausted_before_visible_content",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )

    assert len(completions.calls) == 1
    assert ledger.new_provider_sends_started == 1


def test_nonempty_stop_translation_succeeds(tmp_path):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="complete translation", finish_reason="stop"
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]

    result = module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        _legacy_kwargs(model_id, max_tokens=12_000),
    )

    assert len(completions.calls) == 1
    assert result["response"]["content"] == "complete translation"
    assert result["response"]["diagnostic"]["status"] == "ok"
    assert ledger.new_provider_sends_started == 1
    assert ledger.document["records"][0]["status"] == "accepted"


def test_nonempty_length_translation_is_durable_then_rejected(tmp_path):
    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="partial translation",
            finish_reason="length",
            completion_tokens=23_999,
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    journal_path = paid_request_journal_path(
        tmp_path / "journals", "g7c_translation", TRANSLATION_CHAPTER
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="visible_content_truncated_by_completion_budget",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )

    assert len(completions.calls) == 1
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == (
        "response_recorded"
    )
    assert ledger.new_provider_sends_started == 1
    assert [item["status"] for item in ledger.document["records"]] == [
        "accepted"
    ]


def test_rejected_translation_never_mutates_pending_final_or_progress(
    tmp_path,
):
    (tmp_path / "data").mkdir()
    (tmp_path / "output" / "pending").mkdir(parents=True)
    (tmp_path / "output" / "chapters").mkdir(parents=True)
    progress = tmp_path / "data" / "progress.json"
    progress.write_text(
        '{"last_completed":0,"next_chapter":1}', encoding="utf-8"
    )
    pending = tmp_path / "output" / "pending" / "0001.md"
    pending.write_text("previous pending", encoding="utf-8")
    final = tmp_path / "output" / "chapters" / "0001.md"
    final.write_text("previous final", encoding="utf-8")

    before = {
        path: path.read_bytes()
        for path in (progress, pending, final)
    }
    output_before = sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    )

    module, plan, _ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="",
            finish_reason="length",
            completion_tokens=11_994,
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]

    with pytest.raises(g7f.G7FExecutionError):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )

    assert len(completions.calls) == 1
    for path, payload in before.items():
        assert path.read_bytes() == payload
    assert sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    ) == output_before


# --------------------------------------------------------------------------
# Strict replay validation of durable reasoning diagnostics
# --------------------------------------------------------------------------


def _envelope_stats(**overrides):
    stats = {
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 100,
        "cost_usd": 0.01,
        "rate_correct_cost_usd": 0.01,
        "pricing_version": g7f.PRICING_VERSION,
    }
    stats.update(overrides)
    return stats


def test_replay_envelope_accepts_a_consistent_exposed_reasoning_diagnostic():
    response = g7f._response_from_envelope(
        {
            "stats": _envelope_stats(
                reasoning_tokens=150,
                reasoning_tokens_exposed=True,
                visible_completion_tokens=50,
            ),
            "content": "translated prose",
        }
    )

    assert response.usage.completion_tokens == 200
    assert response.usage.completion_tokens_details.reasoning_tokens == 150
    assert response.usage._g7f_stats["reasoning_tokens"] == 150


def test_replay_envelope_rejects_an_unexposed_reasoning_subtotal():
    with pytest.raises(
        g7f.G7FExecutionError,
        match="invalid reasoning_tokens: unexposed_reasoning_tokens_present",
    ):
        g7f._response_from_envelope(
            {
                "stats": _envelope_stats(
                    reasoning_tokens=150,
                    reasoning_tokens_exposed=False,
                ),
                "content": "translated prose",
            }
        )


def test_replay_envelope_rejects_a_subtotal_above_the_completion_total():
    with pytest.raises(
        g7f.G7FExecutionError,
        match=(
            "invalid reasoning_tokens: "
            "reasoning_tokens_exceed_completion_tokens"
        ),
    ):
        g7f._response_from_envelope(
            {
                "stats": _envelope_stats(
                    reasoning_tokens=250,
                    reasoning_tokens_exposed=True,
                ),
                "content": "translated prose",
            }
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "reasoning_tokens": 150,
            "reasoning_tokens_exposed": True,
            "visible_completion_tokens": 40,
        },
        {
            "reasoning_tokens": 150,
            "reasoning_tokens_exposed": True,
            "visible_completion_tokens": "50",
        },
        {
            "reasoning_tokens": 150,
            "reasoning_tokens_exposed": True,
            "visible_completion_tokens": -1,
        },
        {
            "reasoning_tokens": 150,
            "reasoning_tokens_exposed": True,
            "visible_completion_tokens": True,
        },
    ),
)
def test_replay_envelope_rejects_an_inconsistent_visible_subtotal(overrides):
    with pytest.raises(g7f.G7FExecutionError, match="visible_completion_tokens"):
        g7f._response_from_envelope(
            {
                "stats": _envelope_stats(**overrides),
                "content": "translated prose",
            }
        )


@pytest.mark.parametrize("exposed", (1, 0, "yes", "false", [], {}))
def test_replay_envelope_rejects_malformed_exposure_metadata(exposed):
    with pytest.raises(g7f.G7FExecutionError, match="exposure metadata"):
        g7f._response_from_envelope(
            {
                "stats": _envelope_stats(
                    reasoning_tokens_exposed=exposed,
                ),
                "content": "translated prose",
            }
        )


def test_replay_envelope_rejects_a_subtotal_without_exposure_metadata():
    with pytest.raises(
        g7f.G7FExecutionError,
        match="without exposure metadata",
    ):
        g7f._response_from_envelope(
            {
                "stats": _envelope_stats(reasoning_tokens=150),
                "content": "translated prose",
            }
        )


def test_replay_envelope_rejects_a_visible_subtotal_without_any_subtotal():
    for overrides in (
        {"visible_completion_tokens": 50},
        {
            "reasoning_tokens": None,
            "reasoning_tokens_exposed": False,
            "visible_completion_tokens": 50,
        },
    ):
        with pytest.raises(
            g7f.G7FExecutionError,
            match="without an exposed reasoning subtotal",
        ):
            g7f._response_from_envelope(
                {
                    "stats": _envelope_stats(**overrides),
                    "content": "translated prose",
                }
            )


def test_replay_envelope_absence_cases_keep_the_original_usage_schema():
    # No diagnostics at all: the pre-G7J envelope shape is byte-identical.
    legacy = g7f._response_from_envelope(
        {
            "stats": _envelope_stats(),
            "content": "translated prose",
        }
    )
    assert not hasattr(legacy.usage, "completion_tokens_details")
    assert legacy.usage.completion_tokens == 200
    assert legacy.usage._g7f_stats == _envelope_stats()

    # An explicit unexposed diagnostic with no value is still an absence.
    explicit_absence = g7f._response_from_envelope(
        {
            "stats": _envelope_stats(
                reasoning_tokens=None,
                reasoning_tokens_exposed=False,
            ),
            "content": "translated prose",
        }
    )
    assert not hasattr(explicit_absence.usage, "completion_tokens_details")


# --------------------------------------------------------------------------
# Durable completion tokens are validated before any coercion on replay
# --------------------------------------------------------------------------

_MISSING_COMPLETION_TOKENS = object()

_AUXILIARY_ENTRYPOINT = "review_new_terms"
_AUXILIARY_OPERATION = g7f.CONTEXT_ENTRYPOINTS[_AUXILIARY_ENTRYPOINT]


def _synthetic_durable_stats(**overrides):
    """Return plausible durable accounting stats with overridable fields."""
    stats = {
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 100,
        "cost_usd": "0.01",
        "rate_correct_cost_usd": "0.01",
        "pricing_version": g7f.PRICING_VERSION,
    }
    stats.update(overrides)
    return stats


def _synthetic_protected_state(tmp_path):
    """Create the synthetic production state that no replay may touch."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "chapters").mkdir(parents=True, exist_ok=True)
    artifacts = {
        tmp_path / "data" / "progress.json": (
            '{"last_completed":0,"next_chapter":1}'
        ),
        tmp_path / "data" / "glossary.json": '{"terms":[]}',
        tmp_path / "data" / "continuity.json": '{"facts":[]}',
        tmp_path / "data" / "issues.json": "[]",
        tmp_path / "output" / "pending" / "0001.md": "previous pending",
        tmp_path / "output" / "chapters" / "0001.md": "previous final",
    }
    for path, text in artifacts.items():
        path.write_text(text, encoding="utf-8")
    return artifacts


def _synthetic_matching_replay(tmp_path, *, stats):
    """Seed one matching durable ``response_recorded`` replay.

    The harness performs no Provider transport at all: the durable journal record
    is written by ``run_journaled_paid_request`` from a synthetic envelope and the
    durable ledger mirrors the same synthetic stats, so a replay reaches response
    consumption exactly like one recovered after a process restart.
    """
    plan = _plan()
    model_id = plan["g7e_plan"]["model_id"]
    novel_id = plan["g7e_plan"]["novel_id"]
    journal_dir = tmp_path / "journals"
    legacy_kwargs = _legacy_kwargs(model_id, max_tokens=3_000)

    actual_request = g7f.normalize_legacy_request(
        model_id=model_id,
        logical_operation=_AUXILIARY_OPERATION,
        request_kwargs=legacy_kwargs,
    )
    # Auxiliary operations keep their original bounded completion ceiling.
    assert (
        g7f.translation_completion_budget(
            logical_operation=_AUXILIARY_OPERATION,
            requested_tokens=actual_request["max_completion_tokens"],
            reasoning_effort=actual_request.get("reasoning_effort"),
        )
        == actual_request["max_completion_tokens"]
    )

    provider_identity = build_provider_request_identity(
        request_body=actual_request
    )
    source_text = "\n".join(
        str(item.get("content", ""))
        for item in legacy_kwargs["messages"]
        if isinstance(item, dict)
    )
    identity = request_identity(
        operation_kind=_AUXILIARY_OPERATION,
        novel_id=novel_id,
        chapter=TRANSLATION_CHAPTER,
        provider=PROVIDER_NAME,
        model=provider_identity.requested_model,
        profile_id="fixture",
        source_sha256=hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        request_payload=actual_request,
    )
    journal_path = paid_request_journal_path(
        journal_dir,
        g7f._logical_operation_kind(
            _AUXILIARY_OPERATION,
            provider_identity.request_sha256,
            multi_request_slot=True,
        ),
        TRANSLATION_CHAPTER,
    )

    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    started = ledger.begin_send(
        logical_operation=_AUXILIARY_OPERATION,
        chapter=TRANSLATION_CHAPTER,
        request_sha256=provider_identity.request_sha256,
        request_body=actual_request,
        request_started_utc=NOW,
    )
    ledger.record_accepted_usage(
        send_index=started["send_index"],
        request_sha256=provider_identity.request_sha256,
        usage=_usage(completion=25),
        request_started_utc=NOW,
    )

    def synthetic_normalize(_raw_response, _attempt_metadata):
        return {
            "content": "{}",
            "finish_reason": "stop",
            "stats": copy.deepcopy(stats),
            "provider_response_id": "synthetic-durable-envelope",
            "provider_response_model": actual_request["model"],
            "request_sha256": provider_identity.request_sha256,
            "logical_operation": _AUXILIARY_OPERATION,
        }

    run_journaled_paid_request(
        journal_path=journal_path,
        identity=identity,
        save_json=atomic_write_json,
        prepare_attempt=lambda: {"mode": "g7f_synthetic_replay_fixture"},
        send_once=lambda: SimpleNamespace(),
        normalize_response=synthetic_normalize,
    )
    durable = json.loads(journal_path.read_bytes())
    assert durable["status"] == "response_recorded"
    assert durable["response"]["stats"] == stats

    # The durable ledger mirrors the same synthetic authoritative stats so the
    # replay's own ledger cross-check accepts this recovered state.
    ledger.document["records"][-1]["stats"] = copy.deepcopy(stats)
    atomic_write_json(ledger.path, g7f._with_ledger_hash(ledger.document))
    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    assert reloaded.document["records"][-1]["stats"] == stats

    module = _module(
        model_id=model_id,
        journal_dir=journal_dir,
        novel_id=novel_id,
    )

    def review_new_terms(*_args, **_kwargs):
        return module.call_api_with_retries(**copy.deepcopy(legacy_kwargs))

    module.review_new_terms = review_new_terms

    def forbidden_transport(_kwargs):
        raise AssertionError("replay must never perform provider transport")

    client, completions = _fake_client(forbidden_transport)
    g7f.install_stage1_paid_runtime(
        module,
        model_id=model_id,
        provider_client=client,
        budget_ledger=reloaded,
        clock=_clock,
    )
    return {
        "module": module,
        "plan": plan,
        "ledger": reloaded,
        "journal_path": journal_path,
        "completions": completions,
        "stats": copy.deepcopy(stats),
        "legacy_kwargs": legacy_kwargs,
    }


def _replay_auxiliary_request(module):
    """Make the one journaled auxiliary request for the current chapter."""
    return module.review_new_terms(TRANSLATION_CHAPTER)


@pytest.mark.parametrize(
    "completion_tokens",
    (
        _MISSING_COMPLETION_TOKENS,
        None,
        "200",
        200.0,
        200.5,
        True,
        False,
        -1,
    ),
)
def test_replay_envelope_rejects_malformed_durable_completion_tokens(
    completion_tokens,
):
    stats = _envelope_stats()
    if completion_tokens is _MISSING_COMPLETION_TOKENS:
        stats.pop("completion_tokens")
    else:
        stats["completion_tokens"] = completion_tokens

    # One stable completion-token-specific error instead of an incidental
    # KeyError/TypeError/ValueError from a coercion attempt.
    with pytest.raises(
        g7f.G7FExecutionError,
        match="journaled G7F stats have invalid completion_tokens",
    ):
        g7f._response_from_envelope(
            {"stats": stats, "content": "translated prose"}
        )


def test_replay_diagnostics_return_the_exact_validated_completion_total():
    completion_tokens, diagnostics = g7f._replayed_reasoning_diagnostics(
        _envelope_stats(completion_tokens=0)
    )
    assert type(completion_tokens) is int
    assert completion_tokens == 0
    assert diagnostics is None

    completion_tokens, diagnostics = g7f._replayed_reasoning_diagnostics(
        _envelope_stats(
            reasoning_tokens=150,
            reasoning_tokens_exposed=True,
            visible_completion_tokens=50,
        )
    )
    assert type(completion_tokens) is int
    assert completion_tokens == 200
    assert diagnostics["reasoning_tokens"] == 150
    assert diagnostics["visible_completion_tokens"] == 50


@pytest.mark.parametrize(
    "completion_tokens",
    (
        _MISSING_COMPLETION_TOKENS,
        None,
        "200",
        200.0,
        200.5,
        True,
        False,
        -1,
    ),
)
def test_replayed_malformed_completion_tokens_fail_closed_without_mutation(
    tmp_path,
    completion_tokens,
):
    protected = _synthetic_protected_state(tmp_path)
    stats = _synthetic_durable_stats()
    if completion_tokens is _MISSING_COMPLETION_TOKENS:
        stats.pop("completion_tokens")
    else:
        stats["completion_tokens"] = completion_tokens

    replay = _synthetic_matching_replay(tmp_path, stats=stats)
    module = replay["module"]
    plan = replay["plan"]
    ledger = replay["ledger"]
    journal_path = replay["journal_path"]
    completions = replay["completions"]

    journal_bytes = journal_path.read_bytes()
    ledger_bytes = ledger.path.read_bytes()
    inventory_before = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
    )
    protected_before = {
        path: path.read_bytes() for path in protected
    }
    evidence_before = copy.deepcopy(
        g7j.g7j_policy_identity()["historical_run_1_evidence"]
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="journaled G7F stats have invalid completion_tokens",
    ) as excinfo:
        _replay_auxiliary_request(module)

    # The rejection happens during response reconstruction on the replay path, so
    # the durable envelope really is consumed exactly like a recovered one.
    frames = []
    traceback = excinfo.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_replayed_completion_tokens" in frames
    assert "_response_from_envelope" in frames
    assert "auxiliary_request" in frames

    # Zero transport and no retry: the exact durable evidence never triggers a
    # second Provider send or a new journal slot.
    assert completions.calls == []
    assert journal_path.read_bytes() == journal_bytes
    assert json.loads(journal_bytes)["status"] == "response_recorded"
    assert json.loads(journal_bytes)["response"]["stats"] == stats

    # Ledger bytes, record count, and statuses are unchanged.
    assert ledger.path.read_bytes() == ledger_bytes
    durable_ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    assert durable_ledger.new_provider_sends_started == 1
    assert [record["status"] for record in durable_ledger.document["records"]] == [
        "accepted"
    ]
    assert len(durable_ledger.document["records"]) == 1

    # The single-use paid authorization already consumed by the seeded send stays
    # consumed and non-reusable: no new send slot and no re-issued binding.
    assert durable_ledger.document["approval_token"] == plan["approval_token"]
    assert durable_ledger.document["plan_sha256"] == plan["plan_sha256"]
    assert (
        durable_ledger.document["maximum_new_provider_sends"]
        == plan["g7e_plan"]["maximum_new_provider_sends"]
    )

    # No progress/pending/final/evidence artifact and no new path inventory entry.
    assert (
        sorted(
            str(path.relative_to(tmp_path))
            for path in tmp_path.rglob("*")
        )
        == inventory_before
    )
    for path, payload in protected_before.items():
        assert path.read_bytes() == payload

    # The frozen G7J historical Run-1 evidence is unchanged and its consumed paid
    # authorization remains explicitly non-reusable.
    evidence_after = g7j.g7j_policy_identity()["historical_run_1_evidence"]
    assert evidence_after == evidence_before
    assert evidence_after["consumed_paid_authorization_reusable"] is False


@pytest.mark.parametrize("completion_tokens", (0, 200))
def test_replayed_valid_completion_tokens_replay_without_rewrite(
    tmp_path,
    completion_tokens,
):
    stats = _synthetic_durable_stats(completion_tokens=completion_tokens)
    replay = _synthetic_matching_replay(tmp_path, stats=stats)
    module = replay["module"]
    ledger = replay["ledger"]
    journal_path = replay["journal_path"]
    completions = replay["completions"]

    journal_bytes = journal_path.read_bytes()
    ledger_bytes = ledger.path.read_bytes()
    evidence_before = copy.deepcopy(
        g7j.g7j_policy_identity()["historical_run_1_evidence"]
    )

    response = _replay_auxiliary_request(module)

    # The exact validated integer is used with no coercion, rewrite, additional
    # accounting, or transport.
    assert completions.calls == []
    assert type(response.usage.completion_tokens) is int
    assert response.usage.completion_tokens == completion_tokens
    assert response.usage._g7f_stats == stats
    assert not hasattr(response.usage, "completion_tokens_details")
    assert journal_path.read_bytes() == journal_bytes
    assert ledger.path.read_bytes() == ledger_bytes
    assert ledger.new_provider_sends_started == 1
    assert [record["status"] for record in ledger.document["records"]] == [
        "accepted"
    ]
    assert (
        g7j.g7j_policy_identity()["historical_run_1_evidence"]
        == evidence_before
    )


# --------------------------------------------------------------------------
# Persisted diagnostics must be semantically equal to the durable evidence
# --------------------------------------------------------------------------


def test_persisted_diagnostic_must_equal_the_recomputed_durable_evidence(
    tmp_path,
):
    module, ledger, journal_path, completions, legacy_kwargs = (
        _seed_legacy_translation_envelope(
            tmp_path,
            content="translated prose",
            finish_reason="stop",
            diagnostic={
                "status": "ok",
                "visible_content_empty": False,
                "finish_reason": "stop",
                "completion_tokens": 25,
                "max_completion_tokens": 24_000,
                "near_completion_ceiling": False,
            },
        )
    )
    journal_before = journal_path.read_bytes()

    result = module.journaled_chat_request(
        "chapter_translation",
        TRANSLATION_CHAPTER,
        TRANSLATION_SOURCE,
        legacy_kwargs,
    )

    assert completions.calls == []
    assert journal_path.read_bytes() == journal_before
    assert ledger.new_provider_sends_started == 1
    assert result["response"]["diagnostic"]["status"] == "ok"
    assert result["response"]["diagnostic"]["max_completion_tokens"] == 24_000


def test_wrong_persisted_diagnostic_is_rejected_after_zero_sends(tmp_path):
    """A structurally valid ``ok`` diagnostic over empty+length evidence."""
    module, ledger, journal_path, completions, legacy_kwargs = (
        _seed_legacy_translation_envelope(
            tmp_path,
            content="",
            finish_reason="length",
            completion_tokens=25,
            diagnostic={
                "status": "ok",
                "visible_content_empty": False,
                "finish_reason": "stop",
                "completion_tokens": 25,
                "max_completion_tokens": 24_000,
                "near_completion_ceiling": False,
            },
        )
    )

    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "output" / "pending").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / "chapters").mkdir(parents=True, exist_ok=True)
    progress = tmp_path / "data" / "progress.json"
    progress.write_text(
        '{"last_completed":0,"next_chapter":1}', encoding="utf-8"
    )
    pending = tmp_path / "output" / "pending" / "0001.md"
    pending.write_text("previous pending", encoding="utf-8")
    final = tmp_path / "output" / "chapters" / "0001.md"
    final.write_text("previous final", encoding="utf-8")
    before = {
        path: path.read_bytes() for path in (progress, pending, final)
    }
    output_before = sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    )
    journal_before = journal_path.read_bytes()
    ledger_before = ledger.path.read_bytes()

    # The durable evidence is a length-terminated empty response, so a persisted
    # ``ok`` diagnostic is wrong and must never be used.
    assert (
        g7f.classify_translation_response(
            content="",
            finish_reason="length",
            completion_tokens=25,
            max_completion_tokens=24_000,
        )["status"]
        == "empty_visible_content_length_terminated"
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="disagrees with the durable response evidence",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            legacy_kwargs,
        )

    assert completions.calls == []
    assert journal_path.read_bytes() == journal_before
    assert ledger.path.read_bytes() == ledger_before
    assert ledger.new_provider_sends_started == 1
    for path, payload in before.items():
        assert path.read_bytes() == payload
    assert sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    ) == output_before


def test_malformed_persisted_diagnostic_fails_closed_after_zero_sends(
    tmp_path,
):
    module, _ledger, journal_path, completions, legacy_kwargs = (
        _seed_legacy_translation_envelope(
            tmp_path,
            content="translated prose",
            finish_reason="stop",
            diagnostic={
                "status": "ok",
                "visible_content_empty": False,
                "finish_reason": "stop",
                "completion_tokens": 25,
                "max_completion_tokens": "24000",
                "near_completion_ceiling": False,
            },
        )
    )
    journal_before = journal_path.read_bytes()

    with pytest.raises(
        g7f.G7FExecutionError,
        match="max_completion_tokens is invalid",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            legacy_kwargs,
        )

    assert completions.calls == []
    assert journal_path.read_bytes() == journal_before


# --------------------------------------------------------------------------
# Deterministic post-response diagnostic failure stays durable and definitive
# --------------------------------------------------------------------------


def test_impossible_reasoning_subtotal_is_durable_then_rejected(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "output" / "pending").mkdir(parents=True)
    (tmp_path / "output" / "chapters").mkdir(parents=True)
    progress = tmp_path / "data" / "progress.json"
    progress.write_text(
        '{"last_completed":0,"next_chapter":1}', encoding="utf-8"
    )
    pending = tmp_path / "output" / "pending" / "0001.md"
    pending.write_text("previous pending", encoding="utf-8")
    final = tmp_path / "output" / "chapters" / "0001.md"
    final.write_text("previous final", encoding="utf-8")
    before = {
        path: path.read_bytes() for path in (progress, pending, final)
    }
    output_before = sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    )

    module, plan, ledger, completions = _runtime(
        tmp_path,
        factory=_provider_response(
            content="translated prose",
            finish_reason="stop",
            completion_tokens=25,
            prompt_tokens=100,
            reasoning_tokens=26,
            reasoning_exposed=True,
        ),
    )
    model_id = plan["g7e_plan"]["model_id"]
    journal_path = paid_request_journal_path(
        tmp_path / "journals", "g7c_translation", TRANSLATION_CHAPTER
    )

    with pytest.raises(
        g7f.G7FExecutionError,
        match="reasoning_tokens_exceed_completion_tokens",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )

    # Exactly one send, durably recorded and accounted exactly once.
    assert len(completions.calls) == 1
    record = json.loads(journal_path.read_bytes())
    assert record["status"] == "response_recorded"
    assert record["response"]["diagnostic"]["status"] == "ok"
    assert record["response"]["reasoning_diagnostic_failure"] == {
        "classification": "reasoning_tokens_exceed_completion_tokens",
        "completion_tokens": 25,
        "reasoning_tokens_reported": 26,
        "reasoning_tokens_exposed_reported": True,
    }
    # The untrustworthy subtotal is never written into accounting stats.
    assert "reasoning_tokens" not in record["response"]["stats"]
    assert "reasoning_tokens_exposed" not in record["response"]["stats"]
    assert ledger.new_provider_sends_started == 1
    assert [item["status"] for item in ledger.document["records"]] == [
        "accepted"
    ]

    # G7D accounting is authoritative and priced from total completion tokens.
    expected = g7d.account_provider_usage(
        model_id,
        ProviderUsage(
            prompt_tokens=100,
            completion_tokens=25,
            cached_tokens=None,
            cached_tokens_exposed=False,
            total_tokens=125,
        ),
        NOW,
    )
    stats = ledger.document["records"][0]["stats"]
    assert stats == record["response"]["stats"]
    assert stats["cost_usd"] == expected["cost_usd"]
    assert stats["rate_correct_cost_usd"] == expected["rate_correct_cost_usd"]
    assert float(ledger.accepted_cost) == expected["rate_correct_cost_usd"]

    # No pending/final/progress mutation.
    for path, payload in before.items():
        assert path.read_bytes() == payload
    assert sorted(
        path.name for path in (tmp_path / "output").rglob("*")
    ) == output_before

    # Zero resend and no automatic retry: the same bounded failure is re-derived
    # from the durable envelope and the journal bytes never change.
    journal_bytes = journal_path.read_bytes()
    with pytest.raises(
        g7f.G7FExecutionError,
        match="reasoning_tokens_exceed_completion_tokens",
    ):
        module.journaled_chat_request(
            "chapter_translation",
            TRANSLATION_CHAPTER,
            TRANSLATION_SOURCE,
            _legacy_kwargs(model_id, max_tokens=12_000),
        )
    assert len(completions.calls) == 1
    assert journal_path.read_bytes() == journal_bytes
    assert ledger.new_provider_sends_started == 1
    assert [item["status"] for item in ledger.document["records"]] == [
        "accepted"
    ]
    for path, payload in before.items():
        assert path.read_bytes() == payload


def test_durable_reasoning_diagnostic_record_is_validated_and_never_trusted():
    good = {
        "classification": "reasoning_tokens_exceed_completion_tokens",
        "completion_tokens": 25,
        "reasoning_tokens_reported": 26,
        "reasoning_tokens_exposed_reported": True,
    }
    with pytest.raises(
        g7f.G7FExecutionError,
        match="reasoning_tokens_exceed_completion_tokens",
    ):
        g7f._reject_invalid_reasoning_diagnostic(
            {"reasoning_diagnostic_failure": good}
        )

    # A record whose classification is ``ok`` is not a failure record.
    with pytest.raises(g7f.G7FExecutionError, match="unknown classification"):
        g7f._reject_invalid_reasoning_diagnostic(
            {
                "reasoning_diagnostic_failure": {
                    **good,
                    "classification": "ok",
                }
            }
        )

    # A record that disagrees with its own bounded evidence fails closed.
    with pytest.raises(
        g7f.G7FExecutionError,
        match="disagrees with its own bounded evidence",
    ):
        g7f._reject_invalid_reasoning_diagnostic(
            {
                "reasoning_diagnostic_failure": {
                    **good,
                    "reasoning_tokens_reported": 24,
                }
            }
        )

    with pytest.raises(g7f.G7FExecutionError, match="unexpected shape"):
        g7f._reject_invalid_reasoning_diagnostic(
            {
                "reasoning_diagnostic_failure": {
                    **good,
                    "extra": True,
                }
            }
        )

    with pytest.raises(g7f.G7FExecutionError, match="record is malformed"):
        g7f._reject_invalid_reasoning_diagnostic(
            {"reasoning_diagnostic_failure": "reasoning_tokens_exceeded"}
        )

    # No failure record at all is the normal, accepted case.
    g7f._reject_invalid_reasoning_diagnostic({})


# --------------------------------------------------------------------------
# G7Q target-chapter journal replay scoping
# --------------------------------------------------------------------------


def _journal_dir(run_root):
    directory = run_root / "data" / "provider_request_journal"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _journal_identity(*, chapter, kind, model="fixture-model"):
    return {
        "revision": paid_journal.REVISION,
        "operation_kind": kind,
        "novel_id": "rogue_first_prince",
        "chapter": chapter,
        "provider": PROVIDER_NAME,
        "model": model,
        "profile_id": "fixture",
        "source_sha256": hashlib.sha256(b"source").hexdigest(),
        "translation_sha256": "",
        "request_sha256": "0" * 64,
        "operation_id": "1" * 64,
    }


def _seed_journal(
    run_root,
    *,
    chapter,
    kind,
    status,
    model="fixture-model",
    response=None,
):
    """Write one valid, hash-correct journal record with a chosen status."""
    identity = _journal_identity(chapter=chapter, kind=kind, model=model)
    record = paid_journal._new_planned_record(identity)
    if status == "response_recorded":
        payload = {"content": "ok"} if response is None else response
        record = paid_journal._updated_record(
            record,
            status="response_recorded",
            response=payload,
            response_sha256=paid_journal._sha256_json(payload),
            error={},
        )
    elif status != "planned":
        record = paid_journal._updated_record(
            record,
            status=status,
            response={},
            response_sha256="",
            error={},
        )
    path = paid_request_journal_path(_journal_dir(run_root), kind, chapter)
    atomic_write_json(path, record)
    return path, identity


def _seed_chapter_one_responses(run_root):
    """Seed the real run-02 prior-chapter shape: durable Ch1 responses only."""
    seeded = {}
    for kind in (
        "g7c_translation",
        "g7c_state_extraction",
        "g7c_glossary_review_73af1fcd3841d8f27517",
    ):
        path, _identity = _seed_journal(
            run_root,
            chapter=1,
            kind=kind,
            status="response_recorded",
        )
        seeded[kind] = path
    return seeded


def _target_scoped_state(run_root, target_chapter, *, progress=None):
    inspection = g7f._journal_inspection(run_root, target_chapter)
    return g7e.classify_chapter_resume_state(
        progress=progress
        if progress is not None
        else {
            "last_completed": target_chapter - 1,
            "next_chapter": target_chapter,
        },
        target_chapter=target_chapter,
        journal_inspection=inspection,
    )


def test_prior_chapter_response_recorded_is_not_target_replay(tmp_path):
    """A durable prior-chapter response is not a target-chapter replay."""
    _seed_chapter_one_responses(tmp_path)

    state = _target_scoped_state(tmp_path, 2)

    assert state["status"] == "ready_or_resumable"
    assert state["journal_state"]["local_replay_only_count"] == 0
    assert state["journal_state"]["planned_send_count"] == 0


def test_target_chapter_response_recorded_is_target_replay(tmp_path):
    """A durable target-chapter response is still replay evidence."""
    _seed_journal(
        tmp_path,
        chapter=2,
        kind="g7c_translation",
        status="response_recorded",
    )

    state = _target_scoped_state(tmp_path, 2)

    assert state["status"] == "ready_or_resumable"
    assert state["journal_state"]["local_replay_only_count"] == 1


def test_fresh_target_ledger_is_creatable_after_prior_chapter_responses(tmp_path):
    """Chapter 1 durable responses never demand a Chapter-2 ledger."""
    _seed_chapter_one_responses(tmp_path)
    plan = _plan(target=2)

    assert _target_scoped_state(tmp_path, 2)["journal_state"][
        "local_replay_only_count"
    ] == 0

    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )

    assert g7f.invocation_ledger_path(tmp_path, 2).is_file()
    assert ledger.document["target_chapter"] == 2
    assert ledger.new_provider_sends_started == 0
    assert not g7f.invocation_ledger_path(tmp_path, 1).is_file()


def test_target_response_missing_ledger_fails_closed_before_client(
    monkeypatch,
    tmp_path,
):
    """Target replay evidence without its target ledger blocks before transport."""
    plan = _plan(target=2)
    run_root = tmp_path / "run"
    (run_root / "data" / "provider_request_journal").mkdir(parents=True)
    (tmp_path / g7f.PRODUCTION_SCRIPT_NAME).write_text("", encoding="utf-8")

    inspected = {
        "plan": plan,
        "binding": {"run_root": run_root},
        "chapter_state": {
            "journal_state": {"local_replay_only_count": 1}
        },
    }
    monkeypatch.setattr(
        g7f,
        "inspect_stage1_invocation_plan",
        lambda **_kwargs: inspected,
    )
    monkeypatch.setattr(
        g7f,
        "verify_stage1_invocation_plan",
        lambda **_kwargs: {**inspected, "authorized": True},
    )
    called = []
    monkeypatch.setattr(
        g7f,
        "create_provider_client",
        lambda **_kwargs: called.append("client"),
    )

    with pytest.raises(
        g7f.G7FBudgetError,
        match="budget ledger is missing",
    ):
        g7f.execute_stage1_invocation(
            code_root=tmp_path,
            stage1_root=tmp_path,
            plan=plan,
            expected_plan_sha256=plan["plan_sha256"],
            expected_approval_token=plan["approval_token"],
            operator_authorized=True,
            api_key="synthetic-key-never-used",
        )

    assert called == []
    assert not g7f.invocation_ledger_path(run_root, 2).exists()


def test_target_response_with_matching_ledger_replays_without_send(tmp_path):
    """Target response + matching target ledger stays exactly-once replay."""
    plan = _plan(target=2)
    model_id = plan["g7e_plan"]["model_id"]
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )
    request = _request(model_id)
    request_sha = g7f._sha256_json(request)
    started = ledger.begin_send(
        logical_operation="translation",
        chapter=2,
        request_sha256=request_sha,
        request_body=request,
        request_started_utc=NOW,
    )
    stats = ledger.record_accepted_usage(
        send_index=started["send_index"],
        request_sha256=request_sha,
        usage=_usage(),
        request_started_utc=NOW,
    )
    journal_path, identity = _seed_journal(
        tmp_path,
        chapter=2,
        kind="g7c_translation",
        status="response_recorded",
        model=request["model"],
        response={"content": "ok", "stats": stats},
    )

    state = _target_scoped_state(tmp_path, 2)
    assert state["status"] == "ready_or_resumable"
    assert state["journal_state"]["local_replay_only_count"] == 1
    assert g7f.invocation_ledger_path(tmp_path, 2).is_file()

    def forbidden_send():
        raise AssertionError("replay must never perform provider transport")

    result = run_journaled_paid_request(
        journal_path=journal_path,
        identity=identity,
        save_json=atomic_write_json,
        prepare_attempt=lambda: {"mode": "g7q_replay_fixture"},
        send_once=forbidden_send,
        normalize_response=lambda *_args: {},
    )

    assert result["replayed"] is True
    assert result["api_called"] is False
    ledger.validate_replayed_response(
        request_sha256=request_sha,
        response={"stats": stats},
    )
    assert ledger.new_provider_sends_started == 1


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_prior_chapter_ambiguous_journal_still_blocks_target(
    tmp_path,
    status,
):
    """Global ambiguity safety stays whole-run, not target-scoped."""
    _seed_chapter_one_responses(tmp_path)
    _seed_journal(
        tmp_path,
        chapter=1,
        kind="g7c_state_extraction_ambiguous",
        status=status,
    )

    state = _target_scoped_state(tmp_path, 2)

    assert state["status"] == "blocked_ambiguous_remote_outcome"
    assert state["automatic_paid_continuation_allowed"] is False
    assert state["paid_send_needed"] is False


def test_target_chapter_uncertain_journal_blocks_target(tmp_path):
    _seed_journal(
        tmp_path,
        chapter=2,
        kind="g7c_state_extraction_ambiguous",
        status="uncertain",
    )

    state = _target_scoped_state(tmp_path, 2)

    assert state["status"] == "blocked_ambiguous_remote_outcome"
    assert state["automatic_paid_continuation_allowed"] is False


def test_corrupt_journal_anywhere_still_blocks_target(tmp_path):
    _seed_chapter_one_responses(tmp_path)
    (_journal_dir(tmp_path) / "0001.g7c_corrupt.json").write_text(
        "{not-json",
        encoding="utf-8",
    )

    state = _target_scoped_state(tmp_path, 2)

    assert state["status"] == "blocked_corrupt_or_unknown_journal"
    assert state["automatic_paid_continuation_allowed"] is False


def test_chapter_one_target_semantics_are_unchanged(tmp_path):
    _seed_journal(
        tmp_path,
        chapter=1,
        kind="g7c_translation",
        status="response_recorded",
    )

    resumed = g7e.classify_chapter_resume_state(
        progress={"last_completed": 0, "next_chapter": 1},
        target_chapter=1,
        journal_inspection=g7f._journal_inspection(tmp_path, 1),
    )
    assert resumed["status"] == "ready_or_resumable"
    assert resumed["journal_state"]["local_replay_only_count"] == 1

    complete = g7e.classify_chapter_resume_state(
        progress={"last_completed": 1, "next_chapter": 2},
        target_chapter=1,
        journal_inspection=g7f._journal_inspection(tmp_path, 1),
    )
    assert complete["status"] == "target_already_complete"


def test_g7e_whole_run_classifier_is_unchanged(tmp_path):
    """Target scoping lives in the G7F stage-1 adapter, not in frozen G7E."""
    _seed_chapter_one_responses(tmp_path)
    whole = inspect_paid_request_journals(_journal_dir(tmp_path))

    result = g7e.classify_chapter_resume_state(
        progress={"last_completed": 1, "next_chapter": 2},
        target_chapter=2,
        journal_inspection=whole,
    )

    assert result["journal_state"]["local_replay_only_count"] == 3


def test_target_uncertain_journal_never_retries(tmp_path):
    journal_path, identity = _seed_journal(
        tmp_path,
        chapter=2,
        kind="g7c_translation",
        status="uncertain",
    )
    sends = []

    with pytest.raises(
        PaidRequestUncertainError,
        match="automatic reissue is blocked",
    ):
        run_journaled_paid_request(
            journal_path=journal_path,
            identity=identity,
            save_json=atomic_write_json,
            prepare_attempt=lambda: {"mode": "g7q_never_retry_fixture"},
            send_once=lambda: sends.append("send"),
            normalize_response=lambda *_args: {},
        )

    assert sends == []


def test_live_chapter_two_shape_is_ready_with_zero_target_replay(tmp_path):
    """Exact real run-02 shape: Ch1 responses, no Ch2 journal or ledger."""
    _seed_chapter_one_responses(tmp_path)
    plan = _plan(target=2)
    ledger_path = g7f.invocation_ledger_path(tmp_path, 2)
    assert not ledger_path.is_file()

    state = _target_scoped_state(
        tmp_path,
        2,
        progress={"last_completed": 1, "next_chapter": 2},
    )

    assert state["status"] == "ready_or_resumable"
    assert state["paid_send_needed"] is True
    assert state["journal_state"]["local_replay_only_count"] == 0

    g7f.InvocationBudgetLedger.open_or_create(
        run_root=tmp_path,
        plan=plan,
        clock=_clock,
    )

    # The historical missing-ledger guard no longer fires for this shape.
    assert ledger_path.is_file()


def test_journal_inspection_requires_target_chapter_argument():
    with pytest.raises(TypeError):
        g7f._journal_inspection(Path("."))
