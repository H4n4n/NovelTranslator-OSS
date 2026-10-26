from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from provider_model_identity_runtime import (
    PROVIDER_MODEL_IDENTITY_MODE,
    ProviderModelIdentityError,
    install_provider_model_identity_runtime,
)


BEFORE_CUTOFF = datetime(2026, 9, 14, 3, 0, 0, tzinfo=timezone.utc)
AFTER_CUTOFF = datetime(2026, 9, 14, 5, 0, 0, tzinfo=timezone.utc)


def _module(response_model="deepseek-v4-pro", *, replay_response=None):
    calls = []
    durable = []

    def send_api_request_once(**kwargs):
        calls.append(dict(kwargs))
        return SimpleNamespace(
            id="resp-1",
            model=response_model,
            system_fingerprint="fp-1",
        )

    def run_journaled_paid_request(
        *,
        journal_path,
        identity,
        save_json,
        prepare_attempt,
        send_once,
        normalize_response,
        authorize_send=None,
    ):
        if replay_response is not None:
            return {
                "response": dict(replay_response),
                "operation_id": "op-1",
                "request_sha256": "request-sha",
                "api_called": False,
                "replayed": True,
                "journal_path": journal_path,
            }
        attempt = prepare_attempt()
        with authorize_send() if authorize_send is not None else nullcontext(send_once) as authorized_send:
            response = authorized_send()
        normalized = normalize_response(response, attempt)
        durable.append(dict(normalized))
        return {
            "response": dict(normalized),
            "operation_id": "op-1",
            "request_sha256": "request-sha",
            "api_called": True,
            "replayed": False,
            "journal_path": journal_path,
        }

    module = SimpleNamespace(
        send_api_request_once=send_api_request_once,
        run_journaled_paid_request=run_journaled_paid_request,
    )
    return module, calls, durable


def _journal_call(module, requested_model):
    return module.run_journaled_paid_request(
        journal_path="journal.json",
        identity={"model": requested_model},
        save_json=lambda *_: None,
        prepare_attempt=lambda: {"pricing": "test"},
        send_once=lambda: module.send_api_request_once(model=requested_model),
        normalize_response=lambda response, attempt: {
            "content": "translated",
            "finish_reason": "stop",
            "stats": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def test_exact_pro_response_is_recorded_with_provider_identity_before_cutoff():
    module, calls, durable = _module("deepseek-v4-pro")
    state = install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    result = _journal_call(module, "deepseek-v4-pro")

    assert state["mode"] == PROVIDER_MODEL_IDENTITY_MODE
    assert len(calls) == 1
    assert durable == [
        {
            "content": "translated",
            "finish_reason": "stop",
            "stats": {"prompt_tokens": 1, "completion_tokens": 1},
            "provider_response_model": "deepseek-v4-pro",
            "provider_system_fingerprint": "fp-1",
            "provider_response_id": "resp-1",
        }
    ]
    assert result["response"]["provider_response_model"] == "deepseek-v4-pro"


def test_routed_response_is_durably_recorded_then_fails_without_second_send():
    module, calls, durable = _module("deepseek-flash")
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="identity mismatch") as caught:
        _journal_call(module, "deepseek-v4-pro")

    assert "durable request journal" in str(caught.value)
    assert "automatic provider reissue is forbidden" in str(caught.value)
    assert len(calls) == 1
    assert durable[0]["provider_response_model"] == "deepseek-flash"
    assert durable[0]["provider_system_fingerprint"] == "fp-1"


def test_replayed_routed_response_fails_without_provider_call():
    module, calls, durable = _module(
        replay_response={
            "content": "translated",
            "finish_reason": "stop",
            "stats": {},
            "provider_response_model": "deepseek-flash",
            "provider_system_fingerprint": "fp-routed",
            "provider_response_id": "resp-routed",
        }
    )
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="replayed"):
        _journal_call(module, "deepseek-v4-pro")

    assert calls == []
    assert durable == []


def test_legacy_recorded_response_without_provider_model_fails_closed_on_replay():
    module, calls, _ = _module(
        replay_response={
            "content": "translated",
            "finish_reason": "stop",
            "stats": {},
            "provider_response_id": "legacy-response",
        }
    )
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="did not report"):
        _journal_call(module, "deepseek-v4-pro")

    assert calls == []


@pytest.mark.usefixtures("synthetic_retirement_cutoff")
def test_pro_alias_is_blocked_before_send_at_announced_cutoff():
    module, calls, _ = _module("deepseek-flash")
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: AFTER_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="alias routing"):
        module.send_api_request_once(model="deepseek-v4-pro")

    assert calls == []


def test_legacy_v4_flash_alias_is_blocked_before_any_new_send():
    module, calls, _ = _module("deepseek-flash")
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="legacy model"):
        module.send_api_request_once(model="deepseek-v4-flash")

    assert calls == []


def test_explicit_v41_flash_succeeds_after_pro_cutoff():
    module, calls, durable = _module("deepseek-flash")
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: AFTER_CUTOFF,
    )

    result = _journal_call(module, "deepseek-flash")

    assert len(calls) == 1
    assert durable[0]["provider_response_model"] == "deepseek-flash"
    assert result["response"]["provider_response_model"] == "deepseek-flash"


def test_missing_provider_response_model_is_recorded_then_fails_closed():
    module, calls, durable = _module("")
    install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )

    with pytest.raises(ProviderModelIdentityError, match="did not report"):
        _journal_call(module, "deepseek-v4-pro")

    assert len(calls) == 1
    assert durable[0]["provider_response_model"] == ""


def test_installation_is_idempotent():
    module, _, _ = _module("deepseek-v4-pro")
    first = install_provider_model_identity_runtime(
        module,
        now_utc=lambda: BEFORE_CUTOFF,
    )
    second = install_provider_model_identity_runtime(
        module,
        now_utc=lambda: AFTER_CUTOFF,
    )

    assert second is first
    assert module.send_api_request_once is first["guarded_send_api_request_once"]
    assert (
        module.run_journaled_paid_request
        is first["guarded_run_journaled_paid_request"]
    )

# Exercise the real durable journal, including the controlled-trial transport
# proxy, rather than inferring state ordering from an in-memory journal stub.
def _durable_runtime(tmp_path, *, selection="v41-flash", ceiling=12000,
                     controlled=False, clock=None, response_model=None, transport_error=False):
    import json
    from contextlib import contextmanager
    from controlled_trial_execute import ControlledTrialBudget, _BudgetedClientProxy
    from production_paid_request_journal import request_identity, run_journaled_paid_request
    from translation_model_config import resolve_translation_model

    config = resolve_translation_model(selection)
    path = tmp_path / "request.json"
    events, sends = [], []
    budget = ControlledTrialBudget(config.model, 2, 1.0) if controlled else None
    kwargs = {"model": config.model, "max_tokens": ceiling,
              "extra_body": {"thinking": {"type": "disabled"}},
              "messages": [{"role": "user", "content": "fixture source"}]}

    def record():
        return json.loads(path.read_text(encoding="utf-8"))

    def physical_send(**payload):
        assert record()["status"] == "started"
        if budget is not None:
            assert budget.api_calls == 1
        events.append("physical_send")
        sends.append(payload)
        if transport_error:
            raise RuntimeError("ambiguous physical transport")
        return SimpleNamespace(id="response-1", model=response_model or config.model,
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2,
                                  prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=2))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=physical_send)))
    if controlled:
        original_authorize = budget.authorize_request
        @contextmanager
        def observe_budget(**payload):
            assert record()["status"] == "planned"
            events.append("budget_authorization")
            with original_authorize(**payload) as authorization:
                yield authorization
        budget.authorize_request = observe_budget
        client = _BudgetedClientProxy(client, budget)

    module = SimpleNamespace(send_api_request_once=lambda **payload: client.chat.completions.create(**payload),
                             run_journaled_paid_request=run_journaled_paid_request)
    def observed_clock():
        events.append("route_" + record()["status"])
        return clock() if callable(clock) else clock
    install_provider_model_identity_runtime(module, now_utc=observed_clock if clock is not None else None)
    identity = request_identity(operation_kind="chapter_translation_recovery" if ceiling == 24000 else "chapter_translation",
        novel_id="fixture", chapter=1, provider="deepseek", model=config.model,
        profile_id="historical_military", source_sha256="1" * 64, request_payload=kwargs)

    def save_json(target, value):
        if value["status"] == "started":
            events.append("persist_started")
            if controlled:
                assert budget._active_authorization is not None
                assert budget.api_calls == 0
        target.write_text(json.dumps(value), encoding="utf-8")

    def run():
        send = lambda: module.send_api_request_once(**kwargs)
        return module.run_journaled_paid_request(journal_path=path, identity=identity,
            save_json=save_json, prepare_attempt=lambda: {}, send_once=send,
            normalize_response=lambda response, context: {"content": "complete English", "finish_reason": "stop"},
            **({"authorize_send": lambda: client.chat.completions.authorize_journaled_send(send_once=send, **kwargs)}
               if controlled else {}))
    return SimpleNamespace(run=run, path=path, record=record, sends=sends, events=events, budget=budget, kwargs=kwargs)


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("selection,reason", [("pro", "alias routing"), ("flash", "legacy model")])
@pytest.mark.usefixtures("synthetic_retirement_cutoff")
def test_current_date_route_refusal_leaves_real_journal_planned(tmp_path, controlled, selection, reason):
    fixture = _durable_runtime(tmp_path, controlled=controlled, selection=selection)
    # Deliberately use the real default clock: today's Pro route is expired.
    with pytest.raises(ProviderModelIdentityError, match=reason):
        fixture.run()
    assert fixture.record()["status"] == "planned"
    assert fixture.record()["response"] == {} and fixture.record()["attempt_context"] == {}
    assert not fixture.sends and not fixture.events
    if controlled:
        assert fixture.budget.api_calls == 0 and fixture.budget._active_authorization is None
    before = fixture.path.read_bytes()
    with pytest.raises(ProviderModelIdentityError, match=reason):
        fixture.run()
    assert fixture.path.read_bytes() == before


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("selection,ceiling,clock", [
    ("pro", 12000, BEFORE_CUTOFF),
    ("v41-flash", 12000, AFTER_CUTOFF),
    ("v41-flash", 24000, AFTER_CUTOFF),
])
def test_route_and_budget_authorize_before_started_and_replay_skips_both(tmp_path, controlled, selection, ceiling, clock):
    fixture = _durable_runtime(tmp_path, controlled=controlled, selection=selection, ceiling=ceiling, clock=clock)
    result = fixture.run()
    assert result["api_called"] and not result["replayed"]
    assert fixture.events == ["route_planned"] + (["budget_authorization"] if controlled else []) + [
        "persist_started", "route_started", "physical_send"]
    assert fixture.sends == [fixture.kwargs]
    assert "reasoning_effort" not in fixture.sends[0]
    assert fixture.record()["status"] == "response_recorded"
    before = fixture.path.read_bytes()
    fixture.events.clear()
    replay = fixture.run()
    assert replay["replayed"] and not replay["api_called"]
    assert not fixture.events and len(fixture.sends) == 1
    assert fixture.path.read_bytes() == before
    if controlled:
        assert fixture.budget.api_calls == 1 and fixture.budget.actual_cost_usd > 0


@pytest.mark.parametrize("controlled", [False, True])
def test_true_transport_error_after_composed_authorization_remains_uncertain(tmp_path, controlled):
    from production_paid_request_journal import PaidRequestUncertainError
    fixture = _durable_runtime(tmp_path, controlled=controlled, clock=AFTER_CUTOFF, transport_error=True)
    with pytest.raises(RuntimeError, match="ambiguous physical transport"):
        fixture.run()
    assert fixture.record()["status"] == "uncertain" and len(fixture.sends) == 1
    assert fixture.events == ["route_planned"] + (["budget_authorization"] if controlled else []) + [
        "persist_started", "route_started", "physical_send"]
    before = fixture.path.read_bytes()
    fixture.events.clear()
    with pytest.raises(PaidRequestUncertainError):
        fixture.run()
    assert not fixture.events and fixture.path.read_bytes() == before and len(fixture.sends) == 1


@pytest.mark.parametrize("controlled", [False, True])
def test_wrong_provider_model_stays_recorded_before_rejection_and_on_replay(tmp_path, controlled):
    fixture = _durable_runtime(tmp_path, controlled=controlled, clock=AFTER_CUTOFF,
                               response_model="deepseek-v4-pro")
    with pytest.raises(ProviderModelIdentityError, match="newly recorded"):
        fixture.run()
    assert fixture.record()["status"] == "response_recorded"
    assert fixture.record()["response"]["provider_response_model"] == "deepseek-v4-pro"
    before = fixture.path.read_bytes()
    fixture.events.clear()
    with pytest.raises(ProviderModelIdentityError, match="replayed"):
        fixture.run()
    assert not fixture.events and len(fixture.sends) == 1 and fixture.path.read_bytes() == before


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("status", ["started", "uncertain"])
@pytest.mark.usefixtures("synthetic_retirement_cutoff")
def test_historical_ambiguous_journal_blocks_before_route_and_budget_unchanged(tmp_path, controlled, status):
    import json
    from production_paid_request_journal import PaidRequestUncertainError, _updated_record
    fixture = _durable_runtime(tmp_path, controlled=controlled, selection="pro")
    with pytest.raises(ProviderModelIdentityError):
        fixture.run()
    # Create a valid historical record using the existing unchanged hash rules.
    historical = _updated_record(fixture.record(), status=status, attempt_context={"historical": True})
    fixture.path.write_text(json.dumps(historical), encoding="utf-8")
    before = fixture.path.read_bytes()
    with pytest.raises(PaidRequestUncertainError):
        fixture.run()
    assert fixture.path.read_bytes() == before and not fixture.sends and not fixture.events


@pytest.mark.parametrize("selection", ["pro", "flash"])
@pytest.mark.usefixtures("synthetic_retirement_cutoff")
def test_paid_historical_response_replays_after_route_retirement_without_authorization(tmp_path, selection):
    import json
    from production_paid_request_journal import _sha256_json, _updated_record
    from translation_model_config import resolve_translation_model
    fixture = _durable_runtime(tmp_path, controlled=True, selection=selection)
    with pytest.raises(ProviderModelIdentityError):
        fixture.run()
    response = {"content": "historical paid English", "finish_reason": "stop",
                "provider_response_model": resolve_translation_model(selection).model}
    recorded = _updated_record(fixture.record(), status="response_recorded", response=response,
                               response_sha256=_sha256_json(response))
    fixture.path.write_text(json.dumps(recorded), encoding="utf-8")
    before = fixture.path.read_bytes()
    result = fixture.run()
    assert result["replayed"] and result["response"] == response
    assert not fixture.events and not fixture.sends and fixture.budget.api_calls == 0
    assert fixture.path.read_bytes() == before


@pytest.mark.usefixtures("synthetic_retirement_cutoff")
def test_cutoff_crossing_after_started_keeps_conservative_uncertainty(tmp_path):
    moments = iter([BEFORE_CUTOFF, AFTER_CUTOFF])
    fixture = _durable_runtime(tmp_path, selection="pro", controlled=True, clock=lambda: next(moments))
    with pytest.raises(ProviderModelIdentityError, match="alias routing"):
        fixture.run()
    assert fixture.record()["status"] == "uncertain"
    assert fixture.events == ["route_planned", "budget_authorization", "persist_started", "route_started"]
    assert not fixture.sends and fixture.budget.api_calls == 0

@pytest.mark.parametrize("refusal", ["call", "cost", "controls"])
def test_composed_budget_refusal_still_leaves_planned(tmp_path, refusal):
    from controlled_trial_execute import ControlledTrialPreSendRefusal
    fixture = _durable_runtime(tmp_path, controlled=True, clock=AFTER_CUTOFF,
                               ceiling=16000 if refusal == "controls" else 24000)
    if refusal == "call":
        fixture.budget.max_api_calls = 0
    elif refusal == "cost":
        fixture.budget.max_cost_usd = 0
    with pytest.raises(ControlledTrialPreSendRefusal):
        fixture.run()
    assert fixture.events == ["route_planned", "budget_authorization"]
    assert fixture.record()["status"] == "planned" and not fixture.sends
    assert fixture.budget.api_calls == 0 and fixture.budget._active_authorization is None


def test_route_guard_import_has_no_provider_credential_or_write_side_effects(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    code = r'''
import sys, os
sys.dontwritebytecode = True
sys.path.insert(0, REPOSITORY)
def audit(event, args):
    if event == 'open':
        name, mode, flags = args
        if '.env' in str(name).lower() or 'credential' in str(name).lower():
            raise AssertionError('credential access during import')
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError('write during import')
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('provider/network access during import')
sys.addaudithook(audit)
import provider_model_identity_runtime
print('import safe')
'''
    code = code.replace("REPOSITORY", repr(str(Path(__file__).resolve().parents[1])))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import safe" and not list(tmp_path.iterdir())


@pytest.fixture
def synthetic_retirement_cutoff(monkeypatch):
    """Exercise generic retirement guards independently of continued Pro service."""
    from dataclasses import replace
    from translation_model_config import MODEL_CONFIGS, DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC
    monkeypatch.setitem(MODEL_CONFIGS, "deepseek-v4-pro", replace(MODEL_CONFIGS["deepseek-v4-pro"],
        request_cutoff_utc=DEEPSEEK_V4_PRO_ROUTE_CUTOFF_UTC))


def test_current_pro_service_exact_identity_after_former_cutoff():
    module, calls, durable = _module("deepseek-v4-pro")
    install_provider_model_identity_runtime(module, now_utc=lambda: AFTER_CUTOFF)
    assert _journal_call(module, "deepseek-v4-pro")["api_called"]
    assert len(calls) == 1 and durable[0]["provider_response_model"] == "deepseek-v4-pro"
