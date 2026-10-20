"""Zero-provider tests for the G10F matched-literary paid executor (arm G10A).

Every provider interaction goes through an injected fake client. No test reads a
credential, constructs a real client, executes a production chapter, or mutates a
frozen/production external root.
"""

import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g10_matched as matched
import benchmark_longitudinal_g10_paid_authorization as paid
import benchmark_longitudinal_g10f_matched_executor as g10f
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import CreditReservationLedger
from benchmark_longitudinal_provider import ProviderIdentityError


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
API_KEY = "unit-test-injected-key"


def _clock():
    return NOW


def _prompt(novel_id, chapter):
    system = f"system::{novel_id}"
    user = f"user::{novel_id}::{chapter}"
    messages = (("system", system), ("user", user))
    message_dicts = [
        {"role": role, "content": content} for role, content in messages
    ]
    return matched.G10PromptBundle(
        novel_id=novel_id,
        chapter=chapter,
        source_language="zh" if novel_id == "ace_evolution" else "ko",
        target_language="en",
        profile_id=(
            "infinite_flow" if novel_id == "ace_evolution" else "korean_fantasy"
        ),
        config_sha256="1" * 64,
        source_sha256="2" * 64,
        chapter_library_sha256="3" * 64,
        addendum_sha256="4" * 64,
        text_sha256=matched._sha256_text(user),
        messages=messages,
        system_prompt_sha256=matched._sha256_text(system),
        user_prompt_sha256=matched._sha256_text(user),
        messages_sha256=matched._sha256_json(message_dicts),
    )


def _prompts():
    return {
        (pair.novel_id, pair.chapter): _prompt(pair.novel_id, pair.chapter)
        for pair in matched.PAIR_SPECS
    }


def _binding(tmp_path, **overrides):
    from benchmark_longitudinal_g10_chronological import TRACK_BY_ID

    value = {
        "schema_version": paid.G10PAID_SCHEMA_VERSION,
        "revision": paid.G10PAID_REVISION,
        "benchmark_id": "g10-qwen-glm-headtohead-v1",
        "g10_paid_authorization_sha256": paid.G10PAID_AUTHORIZATION_SHA256,
        "freeze_id": paid.EXPECTED_FREEZE_ID,
        "freeze_file_sha256": paid.EXPECTED_FREEZE_FILE_SHA256,
        "freeze_file_path": str(tmp_path / "freeze" / "freeze.json"),
        "freeze_root": str(tmp_path / "freeze"),
        "budget_ledger_sha256": paid.EXPECTED_LEDGER_SHA256,
        "budget_ledger_path": str(tmp_path / "freeze" / "g10_budget.json"),
        "maximum_credit_equivalent": "30",
        "arm_credit_equivalent_ceilings": {"G10A": "5", "G10B": "25"},
        "matched_plan_sha256": paid.EXPECTED_MATCHED_PLAN_SHA256,
        "s0_manifest_sha256": paid.EXPECTED_S0_MANIFEST_SHA256,
        "g10a1_safety_sha256": paid.EXPECTED_G10A1_SAFETY_SHA256,
        "pricing_version": paid.EXPECTED_PRICING_VERSION,
        "model_ratecard": copy.deepcopy(paid.MODEL_RATECARD),
        "read_only_production_root": str(tmp_path / "production"),
        "matched_evidence_root": str(tmp_path / "evidence"),
        "s0_root": str(tmp_path / "s0"),
        "repo_root": str(tmp_path / "production"),
        "s0_runs": {
            run_id: {
                "relative_root": f"runs/{run_id}",
                "model_id": track.model_id,
                "novel_id": track.novel_id,
                "run_identity_sha256": "a" * 64,
            }
            for run_id, track in TRACK_BY_ID.items()
        },
    }
    value.update(overrides)
    return value


def _sandbox_ledger(tmp_path, *, maximum="30", arms=None):
    path = tmp_path / "freeze" / "g10_budget.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return CreditReservationLedger.create(
        path=path,
        authorization_sha256=paid.EXPECTED_FREEZE_ID,
        maximum_credit_equivalent=maximum,
        arm_credit_equivalent_ceilings=arms or {"G10A": "5", "G10B": "25"},
        clock=_clock,
    )


class _FakeCompletions:
    def __init__(self, responder, probe=None):
        self.calls = []
        self.probe_states = []
        self._responder = responder
        self._probe = probe

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self._probe is not None:
            self.probe_states.append(self._probe())
        return self._responder(kwargs)


class _FakeFactory:
    def __init__(self, completions):
        self.completions = completions
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(copy.deepcopy(kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )


def _usage(prompt=100, completion=25, cached=0, expose_cached=True):
    details = SimpleNamespace(cached_tokens=cached) if expose_cached else None
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=details,
    )


_OMITTED_USAGE = object()


def _response(
    *,
    model,
    content="translated prose",
    finish_reason="stop",
    usage=_OMITTED_USAGE,
):
    # ``usage=None`` deliberately means "the Provider omitted usage"; only an
    # absent keyword falls back to complete synthetic usage.
    resolved = _usage() if usage is _OMITTED_USAGE else usage

    def responder(kwargs):
        return SimpleNamespace(
            id="fake-response",
            model=model,
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=resolved,
        )

    return responder


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    prompts = _prompts()
    cases = matched.build_runtime_cases(
        repo_root=repo,
        evidence_root=evidence,
        prompt_bundles=prompts,
    )
    monkeypatch.setattr(
        g10f, "build_runtime_cases", lambda **_kwargs: cases
    )
    binding = _binding(tmp_path, matched_evidence_root=str(evidence))
    monkeypatch.setattr(
        g10f,
        "frozen_execution_binding",
        lambda **_kwargs: copy.deepcopy(binding),
    )
    ledger = _sandbox_ledger(tmp_path)
    env = {
        "tmp_path": tmp_path,
        "repo": repo,
        "evidence": evidence,
        "prompts": prompts,
        "cases": cases,
        "binding": binding,
        "ledger": ledger,
    }

    # ``execute_g10f_invocation`` opens the ledger the frozen binding names.
    # Re-open it from that exact path here and hand back the live instance, so
    # every assertion below observes the durable ledger the executor actually
    # mutates (accept/settle/quarantine) instead of a stale snapshot.
    def _open_frozen_budget_ledger(binding, **_kwargs):
        assert Path(binding["budget_ledger_path"]) == ledger.path
        current = CreditReservationLedger.open(
            path=ledger.path,
            authorization_sha256=paid.EXPECTED_FREEZE_ID,
            clock=_clock,
        )
        env["ledger"] = current
        return current

    monkeypatch.setattr(
        g10f, "open_frozen_budget_ledger", _open_frozen_budget_ledger
    )
    return env


def _plan(env, *, execution_index=1, model_id=None, maximum="0.5"):
    case = env["cases"][execution_index - 1]
    return g10f.build_g10f_invocation(
        binding=env["binding"],
        repo_root=env["repo"],
        evidence_root=env["evidence"],
        execution_index=execution_index,
        model_id=model_id or case.benchmark_model_id,
        target_state_fingerprint_sha256=g10f.g10f_target_state_fingerprint(
            env["evidence"]
        ),
        maximum_credit_equivalent=maximum,
        prompt_bundles=env["prompts"],
    )


def _execute(env, plan, *, responder, operator_authorized=True, api_key=API_KEY,
             probe=None):
    completions = _FakeCompletions(responder, probe=probe)
    factory = _FakeFactory(completions)
    report = g10f.execute_g10f_invocation(
        code_root=REPO_ROOT,
        production_root=env["tmp_path"] / "production",
        freeze_root=env["tmp_path"] / "freeze",
        matched_evidence_root=env["evidence"],
        s0_root=env["tmp_path"] / "s0",
        repo_root=env["repo"],
        evidence_root=env["evidence"],
        plan=plan,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        operator_authorized=operator_authorized,
        api_key=api_key,
        client_factory=factory,
        prompt_bundles=env["prompts"],
        clock=_clock,
    )
    return report, completions, factory


def _journal_path(env, plan):
    return (env["evidence"] / plan["journal_relative_path"]).resolve()


def _artifact_path(env, plan):
    from benchmark_longitudinal_g10f_matched_executor import _model_slug

    return (
        env["evidence"]
        / "artifacts"
        / plan["novel_id"]
        / _model_slug(plan["model_id"])
        / f"{plan['chapter']:04d}.{plan['case_kind']}.json"
    )


def _quarantine_path(env, plan):
    from benchmark_longitudinal_g10f_matched_executor import _model_slug

    return (
        env["evidence"]
        / "quarantine"
        / plan["novel_id"]
        / _model_slug(plan["model_id"])
        / f"{plan['chapter']:04d}.{plan['case_kind']}.json"
    )


# ---------------------------------------------------------------------------


def test_executor_identity_is_zero_provider_and_bounded():
    identity = g10f.g10f_executor_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["arm_id"] == "G10A"
    scope = identity["execution_scope"]
    assert scope["one_execution_index_per_invocation"] is True
    assert scope["execution_index_range"] == [1, 102]
    assert scope["automatic_loop"] is False
    assert scope["automatic_retry"] is False
    assert scope["provider_client_max_retries"] == 0
    assert scope["production_lifecycle_used"] is False
    assert identity["quarantine"]["scope_kind"] == "literary_model_case"
    assert g10f.G10F_EXECUTOR_SHA256 == g10f._sha256_json(identity)


def test_one_execution_index_per_invocation_and_no_auto_loop(env):
    for index in (0, -1, 103, 200):
        with pytest.raises(g10f.G10FExecutionError, match="execution_index"):
            g10f.materialize_g10f_case(
                repo_root=env["repo"],
                evidence_root=env["evidence"],
                execution_index=index,
                prompt_bundles=env["prompts"],
            )

    plan = _plan(env, execution_index=1)
    report, completions, factory = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    assert report["status"] == "COMPLETED"
    assert report["execution_index"] == 1
    assert len(completions.calls) == 1
    journals = sorted(
        path.relative_to(env["evidence"]).as_posix()
        for path in env["evidence"].rglob("*.json")
        if "journal" in path.parts
    )
    assert journals == [plan["journal_relative_path"]]
    assert (env["evidence"] / plan["journal_relative_path"]).is_file()


def test_frozen_matched_plan_binding_and_wrong_sha_rejection(env):
    plan = _plan(env)
    assert plan["matched_plan_sha256"] == paid.EXPECTED_MATCHED_PLAN_SHA256
    assert (
        plan["authorization"]["matched_plan_sha256"]
        == paid.EXPECTED_MATCHED_PLAN_SHA256
    )

    bad_binding = dict(env["binding"])
    bad_binding["matched_plan_sha256"] = "9" * 64
    with pytest.raises(
        paid.G10PaidAuthorizationError, match="matched_plan_sha256"
    ):
        g10f.build_g10f_invocation(
            binding=bad_binding,
            repo_root=env["repo"],
            evidence_root=env["evidence"],
            execution_index=1,
            model_id=QWEN_MODEL_ID,
            target_state_fingerprint_sha256=g10f.g10f_target_state_fingerprint(
                env["evidence"]
            ),
            maximum_credit_equivalent="0.5",
            prompt_bundles=env["prompts"],
        )

    # A plan whose own frozen binding was rewritten (with a recomputed plan SHA)
    # still fails closed against the live binding.
    tampered = copy.deepcopy(plan)
    tampered["matched_plan_sha256"] = "9" * 64
    body = copy.deepcopy(tampered)
    body.pop("plan_sha256")
    tampered["plan_sha256"] = g10f._sha256_json(body)
    with pytest.raises(g10f.G10FExecutionError, match="binding changed"):
        g10f.verify_g10f_invocation(
            plan=tampered,
            binding=env["binding"],
            repo_root=env["repo"],
            evidence_root=env["evidence"],
            prompt_bundles=env["prompts"],
        )


def test_request_body_drift_fails_closed_before_transport(env, monkeypatch):
    plan = _plan(env)
    drifted = []
    for case in env["cases"]:
        if case.execution_index != plan["execution_index"]:
            drifted.append(case)
            continue
        body = copy.deepcopy(case.request_body)
        body["messages"] = [
            {"role": "user", "content": "different source text"}
        ]
        drifted.append(
            type(case)(
                execution_index=case.execution_index,
                pair=case.pair,
                benchmark_model_id=case.benchmark_model_id,
                provider_model_id=case.provider_model_id,
                effort=case.effort,
                prompt=case.prompt,
                request_body=body,
                provider_request_sha256=case.provider_request_sha256,
                journal_identity=case.journal_identity,
                journal_path=case.journal_path,
            )
        )
    monkeypatch.setattr(g10f, "build_runtime_cases", lambda **_kwargs: tuple(drifted))

    with pytest.raises(
        g10f.G10FExecutionError, match="request body SHA differs"
    ):
        _execute(env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902"))

    completions = _FakeCompletions(_response(model="Qwen/Qwen3.8-Max-0902"))
    factory = _FakeFactory(completions)
    with pytest.raises(g10f.G10FExecutionError):
        g10f.execute_g10f_invocation(
            code_root=REPO_ROOT,
            production_root=env["tmp_path"] / "production",
            freeze_root=env["tmp_path"] / "freeze",
            matched_evidence_root=env["evidence"],
            s0_root=env["tmp_path"] / "s0",
            repo_root=env["repo"],
            evidence_root=env["evidence"],
            plan=plan,
            expected_plan_sha256=plan["plan_sha256"],
            expected_approval_token=plan["approval_token"],
            operator_authorized=True,
            api_key=API_KEY,
            client_factory=factory,
            prompt_bundles=env["prompts"],
            clock=_clock,
        )
    assert completions.calls == []
    assert factory.kwargs == []
    assert not _journal_path(env, plan).exists()
    assert not (env["evidence"] / "artifacts").exists()


def test_exactly_one_transport_attempt_even_when_the_send_raises(env):
    plan = _plan(env)

    def exploding(_kwargs):
        raise TimeoutError("ambiguous timeout")

    completions = _FakeCompletions(exploding)
    factory = _FakeFactory(completions)
    report = g10f.execute_g10f_invocation(
        code_root=REPO_ROOT,
        production_root=env["tmp_path"] / "production",
        freeze_root=env["tmp_path"] / "freeze",
        matched_evidence_root=env["evidence"],
        s0_root=env["tmp_path"] / "s0",
        repo_root=env["repo"],
        evidence_root=env["evidence"],
        plan=plan,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        operator_authorized=True,
        api_key=API_KEY,
        client_factory=factory,
        prompt_bundles=env["prompts"],
        clock=_clock,
    )

    assert report["status"] == "QUARANTINED"
    assert report["provider_transport_attempts"] == 1
    assert len(completions.calls) == 1
    # The client is always constructed with the frozen no-retry policy.
    assert factory.kwargs[0]["max_retries"] == 0


def test_reservation_and_send_started_precede_transport(env):
    plan = _plan(env)
    journal_path = _journal_path(env, plan)

    def probe():
        record = json.loads(journal_path.read_text(encoding="utf-8"))
        return record["status"]

    report, completions, _factory = _execute(
        env,
        plan,
        responder=_response(model="Qwen/Qwen3.8-Max-0902"),
        probe=probe,
    )
    assert report["status"] == "COMPLETED"
    # The durable journal was already ``started`` when the transport ran, and the
    # CE reservation plus its send_started marker existed strictly before it.
    assert completions.probe_states == ["started"]
    records = env["ledger"].document["records"]
    assert len(records) == 1
    assert records[0]["status"] == "accepted"
    assert records[0]["send_started"] is True
    assert records[0]["request_sha256"] == plan["request_sha256"]
    assert records[0]["arm_id"] == "G10A"
    assert records[0]["reserved_credit_equivalent"] == plan[
        "maximum_credit_equivalent"
    ]
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == (
        "response_recorded"
    )


def test_completed_run_persists_exact_artifact_and_journal(env):
    plan = _plan(env)
    report, completions, _factory = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    assert report["status"] == "COMPLETED"
    assert report["paid_execution_authorized"] is True
    assert report["automatic_chapter_advance"] is False
    assert report["automatic_retry"] is False

    artifact = json.loads(_artifact_path(env, plan).read_text(encoding="utf-8"))
    assert artifact["arm_id"] == "G10A"
    assert artifact["model_id"] == QWEN_MODEL_ID
    assert artifact["provider_model_id"] == "Qwen/Qwen3.8-Max-0902"
    assert artifact["request_sha256"] == plan["request_sha256"]
    assert artifact["plan_sha256"] == plan["plan_sha256"]
    assert artifact["content"] == "translated prose"
    assert artifact["chapter"] == plan["chapter"]
    assert artifact["case_kind"] == plan["case_kind"]
    assert artifact["provider_calls"] == 1

    journal = json.loads(
        _journal_path(env, plan).read_text(encoding="utf-8")
    )
    assert journal["status"] == "response_recorded"
    assert journal["request_sha256"] == plan["request_sha256"]
    assert journal["chapter"] == plan["chapter"]
    assert journal["response"]["provider_response_model"] == (
        "Qwen/Qwen3.8-Max-0902"
    )


@pytest.mark.parametrize(
    "factory_kwargs, usage, error",
    (
        ({"model": "z-ai/glm-5.3-flash"}, _usage(), "identity mismatch"),
        ({"model": "Qwen/Qwen3.8-Max-0902"}, None, "omitted usage"),
        (
            {"model": "Qwen/Qwen3.8-Max-0902"},
            SimpleNamespace(prompt_tokens=100, completion_tokens=None),
            "omitted required token usage",
        ),
    ),
)
def test_unusable_or_incomplete_responses_quarantine_without_accepted_ce(
    env, factory_kwargs, usage, error
):
    plan = _plan(env)
    responder = _response(usage=usage, **factory_kwargs)
    report, completions, _factory = _execute(env, plan, responder=responder)

    assert report["status"] == "QUARANTINED"
    assert len(completions.calls) == 1
    assert report["full_reservation_retained"] is True
    assert env["ledger"].accepted_credit_equivalent == Decimal("0")
    assert env["ledger"].committed_credit_equivalent == Decimal(
        plan["maximum_credit_equivalent"]
    )
    quarantine = json.loads(
        _quarantine_path(env, plan).read_text(encoding="utf-8")
    )
    assert quarantine["scope_kind"] == "literary_model_case"
    assert quarantine["first_blocked_chapter"] is None
    assert quarantine["reservation_id"] == report["reservation_id"]
    assert report["reservation_status"] == "ambiguous"


@pytest.mark.parametrize(
    "model_id, provider_model_id, prompt, completion, cached, expected",
    (
        (QWEN_MODEL_ID, "Qwen/Qwen3.8-Max-0902", 100, 25, 0, "0.00035"),
        (GLM_MODEL_ID, "z-ai/glm-5.3-flash", 100, 25, 0, "0.0000275"),
        (QWEN_MODEL_ID, "Qwen/Qwen3.8-Max-0902", 1000, 0, 400, "0.0013"),
    ),
)
def test_exact_accepted_ce_settlement_matches_the_frozen_ratecard(
    env, model_id, provider_model_id, prompt, completion, cached, expected
):
    index = 1 if model_id == QWEN_MODEL_ID else 2
    plan = _plan(env, execution_index=index, model_id=model_id)
    assert plan["provider_model_id"] == provider_model_id
    responder = _response(
        model=provider_model_id,
        usage=_usage(prompt=prompt, completion=completion, cached=cached),
    )
    report, completions, _factory = _execute(env, plan, responder=responder)

    assert report["status"] == "COMPLETED"
    assert len(completions.calls) == 1
    assert report["accepted_credit_equivalent"] == expected
    assert env["ledger"].accepted_credit_equivalent == Decimal(expected)
    artifact = json.loads(_artifact_path(env, plan).read_text(encoding="utf-8"))
    assert artifact["accepted_credit_equivalent"] == expected
    assert artifact["usage"]["cached_tokens"] == cached


def test_ambiguity_keeps_the_full_reservation_and_never_resends(env):
    plan = _plan(env)

    def exploding(_kwargs):
        raise TimeoutError("ambiguous timeout")

    first, completions, _factory = _execute(env, plan, responder=exploding)
    assert first["status"] == "QUARANTINED"
    assert len(completions.calls) == 1
    assert env["ledger"].committed_credit_equivalent == Decimal(
        plan["maximum_credit_equivalent"]
    )
    assert env["ledger"].accepted_credit_equivalent == Decimal("0")
    records = env["ledger"].document["records"]
    assert [record["status"] for record in records] == ["ambiguous"]

    second, _completions2, factory2 = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    assert second["status"] == "QUARANTINED"
    assert second["provider_transport_attempts"] == 0
    assert factory2.kwargs == []
    assert env["ledger"].committed_credit_equivalent == Decimal(
        plan["maximum_credit_equivalent"]
    )


def test_response_recorded_replay_is_local_only_and_artifact_is_idempotent(env):
    plan = _plan(env)
    first, completions, _factory = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    assert first["status"] == "COMPLETED"
    artifact_path = _artifact_path(env, plan)
    artifact_bytes = artifact_path.read_bytes()
    journal_bytes = _journal_path(env, plan).read_bytes()
    ledger_bytes = env["ledger"].path.read_bytes()

    second, completions2, factory2 = _execute(
        env,
        plan,
        responder=_response(model="Qwen/Qwen3.8-Max-0902"),
    )
    assert second["status"] == "REPLAYED"
    assert second["provider_transport_attempts"] == 0
    assert second["new_provider_send_authorized"] is False
    assert completions2.calls == []
    assert factory2.kwargs == []
    assert second["replay_gate"]["replay_authorized"] is True
    assert second["replay_gate"]["provider_send_authorized"] is False
    assert artifact_path.read_bytes() == artifact_bytes
    assert _journal_path(env, plan).read_bytes() == journal_bytes
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert len(completions.calls) == 1


def test_replay_fails_closed_when_the_ledger_has_no_accepted_reservation(env):
    plan = _plan(env)
    report, completions, _factory = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    assert report["status"] == "COMPLETED"

    # A durable response without its accepted reservation is corrupt durable
    # state: the executor must refuse both replay and resend.
    ledger = CreditReservationLedger.open(
        path=env["ledger"].path,
        authorization_sha256=paid.EXPECTED_FREEZE_ID,
        clock=_clock,
    )
    ledger.document["records"][0]["status"] = "released_pre_send"
    ledger.document["records"][0]["send_started"] = False
    ledger.document["accepted_credit_equivalent"] = "0"
    ledger.document["records"][0]["accepted_credit_equivalent"] = "0"
    ledger._persist()

    with pytest.raises(paid.G10PaidBudgetError, match="accepted started"):
        _execute(env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902"))
    assert len(completions.calls) == 1


@pytest.mark.parametrize(
    "failing_index, partner_index",
    ((1, 2), (2, 1)),
)
def test_quarantined_case_leaves_the_paired_partner_executable(
    env, failing_index, partner_index
):
    failing_plan = _plan(env, execution_index=failing_index)
    partner_plan = _plan(env, execution_index=partner_index)
    assert failing_plan["pair_id"] == partner_plan["pair_id"]
    assert failing_plan["model_id"] != partner_plan["model_id"]

    def exploding(_kwargs):
        raise ConnectionError("network down")

    report, _completions, _factory = _execute(
        env, failing_plan, responder=exploding
    )
    assert report["status"] == "QUARANTINED"

    partner, completions, _factory2 = _execute(
        env,
        partner_plan,
        responder=_response(model=partner_plan["provider_model_id"]),
    )
    assert partner["status"] == "COMPLETED"
    assert len(completions.calls) == 1
    assert partner["model_id"] == partner_plan["model_id"]
    marker = (
        env["evidence"]
        / "quarantine"
        / failing_plan["novel_id"]
        / ("qwen" if failing_plan["model_id"] == QWEN_MODEL_ID else "glm")
        / f"{failing_plan['chapter']:04d}.{failing_plan['case_kind']}.json"
    )
    assert marker.is_file()
    assert not (
        env["evidence"]
        / "quarantine"
        / partner_plan["novel_id"]
        / ("qwen" if partner_plan["model_id"] == QWEN_MODEL_ID else "glm")
        / f"{partner_plan['chapter']:04d}.{partner_plan['case_kind']}.json"
    ).exists()


def test_missing_operator_authorization_has_no_side_effect(env):
    plan = _plan(env)
    ledger_bytes = env["ledger"].path.read_bytes()
    completions = _FakeCompletions(_response(model="Qwen/Qwen3.8-Max-0902"))
    factory = _FakeFactory(completions)

    report = g10f.execute_g10f_invocation(
        code_root=REPO_ROOT,
        production_root=env["tmp_path"] / "production",
        freeze_root=env["tmp_path"] / "freeze",
        matched_evidence_root=env["evidence"],
        s0_root=env["tmp_path"] / "s0",
        repo_root=env["repo"],
        evidence_root=env["evidence"],
        plan=plan,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        operator_authorized=False,
        api_key=API_KEY,
        client_factory=factory,
        prompt_bundles=env["prompts"],
        clock=_clock,
    )

    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["provider_calls"] == 0
    assert report["provider_transport_attempts"] == 0
    assert "AUTHORIZATION_REQUIRED" in report["reason"]
    assert factory.kwargs == []
    assert completions.calls == []
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert not (env["evidence"] / "journal").exists()
    assert not (env["evidence"] / "artifacts").exists()
    assert not (env["evidence"] / "authorization").exists()
    assert not (env["evidence"] / "quarantine").exists()


def test_missing_api_key_fails_before_any_side_effect(env):
    plan = _plan(env)
    ledger_bytes = env["ledger"].path.read_bytes()
    completions = _FakeCompletions(_response(model="Qwen/Qwen3.8-Max-0902"))
    factory = _FakeFactory(completions)

    with pytest.raises(g10f.G10FExecutionError, match="injected API key"):
        g10f.execute_g10f_invocation(
            code_root=REPO_ROOT,
            production_root=env["tmp_path"] / "production",
            freeze_root=env["tmp_path"] / "freeze",
            matched_evidence_root=env["evidence"],
            s0_root=env["tmp_path"] / "s0",
            repo_root=env["repo"],
            evidence_root=env["evidence"],
            plan=plan,
            expected_plan_sha256=plan["plan_sha256"],
            expected_approval_token=plan["approval_token"],
            operator_authorized=True,
            api_key=None,
            client_factory=factory,
            prompt_bundles=env["prompts"],
            clock=_clock,
        )

    assert factory.kwargs == []
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert not (env["evidence"] / "journal").exists()


def test_ceiling_rejection_before_send_leaves_no_journal_mutation(env):
    plan = _plan(env, maximum="0.0000000001")
    report, completions, factory = _execute(
        env, plan, responder=_response(model="Qwen/Qwen3.8-Max-0902")
    )
    if report["status"] == "QUARANTINED":
        # A failed send is never released; the durable record proves the refusal
        # happened before transport and the CE stayed committed.
        assert report["provider_transport_attempts"] == 0
    assert completions.calls == []
    assert factory.kwargs != []
    journal = _journal_path(env, plan)
    assert not journal.exists()
    records = env["ledger"].document["records"]
    assert [record["send_started"] for record in records] == [False]
    assert records[0]["status"] == "released_pre_send"


def test_conservative_ce_bound_is_positive_and_ratecard_based():
    body = {
        "model": "Qwen/Qwen3.8-Max-0902",
        "messages": [{"role": "user", "content": "x"}],
        "max_completion_tokens": 24_000,
    }
    qwen = g10f.conservative_credit_equivalent_bound(QWEN_MODEL_ID, body)
    glm = g10f.conservative_credit_equivalent_bound(GLM_MODEL_ID, body)
    assert qwen > glm > 0
    assert qwen > Decimal("0.14")
    with pytest.raises(g10f.G10FExecutionError, match="max_completion_tokens"):
        g10f.conservative_credit_equivalent_bound(
            QWEN_MODEL_ID, {"messages": []}
        )
    with pytest.raises(g10f.G10FExecutionError, match="ratecard"):
        g10f.conservative_credit_equivalent_bound("nope/nope", body)


def test_provider_identity_error_type_is_the_real_one():
    # The executor relies on the adapter's fail-closed acceptance, not on a
    # locally invented exception type.
    assert issubclass(ProviderIdentityError, RuntimeError)
