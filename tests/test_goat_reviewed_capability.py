"""Source-bound capability tests: disposable copies, no provider/secret IO."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

import pytest

import production_goat_auxiliary_policy as policy
from production_goat_runtime import GoatBudget, GoatRuntimeError, install_goat_runtime, normalize_production_request
from test_production_goat_runtime import engine, zero_network, kwargs, CLOCK, GOAT
from production_paid_request_journal import _updated_record

KEY = (GOAT, "deepseek/deepseek-v4.1-flash")
MUTATIONS = ("missing", "one_byte", "wrong_expected_sha", "provider", "model",
    "context", "output", "primary_source", "primary_fact", "secondary_source",
    "secondary_fact", "reviewed_date", "normalization", "schema", "revision",
    "boolean_schema", "float_context", "unknown_field", "invalid_json", "duplicate_field")


def copied_capability(tmp_path, monkeypatch):
    cap = policy.COMMAND_CODE_FLASH_CAPABILITY
    raw = (Path(policy.__file__).resolve().parent / cap.evidence_path).read_bytes()
    path = tmp_path / "reviewed_capability_copy.json"
    path.write_bytes(raw)
    cap = replace(cap, evidence_path=str(path))
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", MappingProxyType({KEY: cap}))
    return cap


@pytest.fixture
def reviewed_engine(engine, tmp_path, monkeypatch):
    cap = copied_capability(tmp_path, monkeypatch)
    # Explicit fixture allowance: do not weaken the actual production bound.
    engine[4].max_cost_usd = 2.0
    return engine, cap


def mutate(cap, mutation, monkeypatch):
    path = Path(cap.evidence_path)
    if mutation == "missing":
        path.unlink()
    elif mutation == "one_byte":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "wrong_expected_sha":
        cap = replace(cap, evidence_sha256="0" * 64)
    else:
        value = json.loads(path.read_bytes())
        changes = dict(provider=("provider", "wrong_provider"), model=("model", "wrong_model"),
            context=("normalized_context_tokens", 999999), output=("output_tokens", 8000),
            primary_source=("primary_source", "https://example.invalid/other"),
            primary_fact=("primary_source_reviewed_fact", "2M context"),
            secondary_source=("secondary_source", "https://example.invalid/other"),
            secondary_fact=("secondary_source_reviewed_fact", "different evidence"),
            reviewed_date=("reviewed_date", "2026-10-12"), normalization=("normalization_explanation", "binary million"),
            schema=("schema_version", 2), revision=("revision", "unreviewed-revision"),
            boolean_schema=("schema_version", True), float_context=("normalized_context_tokens", 1000000.0),
            unknown_field=("unreviewed_provenance", "extra"))
        if mutation in changes:
            key, replacement = changes[mutation]
            value[key] = replacement
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if mutation == "invalid_json": raw = b"{"
        if mutation == "duplicate_field": raw = raw.rstrip()[:-1] + b',"provider":"command_code_provider"}\n'
        path.write_bytes(raw)
        # Deliberately defeat only the hash check in these cases. Parsed facts
        # and every provenance field must independently match compiled policy.
        cap = replace(cap, evidence_sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", MappingProxyType({KEY: cap}))


def test_exact_repository_evidence_and_immutable_registered_facts(monkeypatch):
    cap = policy.COMMAND_CODE_FLASH_CAPABILITY
    assert policy.REVIEWED_HARD_BOUNDS[KEY] is cap
    assert cap.context_tokens == 1_000_000 and cap.output_tokens is None
    path = Path(policy.__file__).resolve().parent / cap.evidence_path
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == cap.evidence_sha256 == "99138eabe21ec68eda4fb18751b92bec38508924e5830a36c77317ba64ef3c99"
    assert json.loads(raw) == cap.expected_evidence()
    assert raw == (json.dumps(cap.expected_evidence(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert b"\r" not in raw
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: (reads.append(p) or original(p)))
    for _ in range(2):
        assert cap.validate_evidence(provider=KEY[0], model=KEY[1])["context_tokens"] == 1_000_000
    assert reads == [path, path]  # Never a cached syntactic SHA check.
    with pytest.raises(FrozenInstanceError): cap.context_tokens = 8000
    with pytest.raises(TypeError): policy.REVIEWED_HARD_BOUNDS[KEY] = cap
    assert not hasattr(cap, "__dict__")


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_evidence_tamper_blocks_before_started_credentials_and_transport(reviewed_engine, mutation, monkeypatch):
    (project, module, sends, _, budget, *_), cap = reviewed_engine
    mutate(cap, mutation, monkeypatch)
    with pytest.raises(GoatRuntimeError, match="architecture review"):
        module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    path = project.data_dir / "provider_request_journal/0001.state_extraction.json"
    assert json.loads(path.read_text())["status"] == "planned"
    assert not sends and budget.api_calls == budget.actual_cost_usd == 0
    import run_production_engine as launcher
    from production_goat_execution import execute_authorized_goat
    activations = []
    monkeypatch.setattr(launcher, "activated_goat_transport", lambda *a, **k: activations.append(True))
    # The shared production executor checks the evidence before even consuming
    # a plan, initializing a project, or entering credential activation.
    with pytest.raises(ValueError):
        execute_authorized_goat({}, repo_root=project.repository_root, recovery=True)
    assert not activations and not sends


def test_runtime_capability_validation_is_local_without_network(monkeypatch):
    import urllib.request
    import socket
    forbidden = lambda *a, **k: pytest.fail("capability validation fetched the network")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    wire = normalize_production_request("state_extraction", kwargs(4000))
    exposure = policy.completion_exposure(wire, prompt_upper=10000)
    assert exposure["prompt_upper"] == exposure["completion_upper"] == 1_000_000
    assert exposure["capability_source"]["output_tokens"] is None
    assert "max_tokens" not in wire and "max_completion_tokens" not in wire


@pytest.mark.parametrize("hour,day,expected", [(2,5,1.50), (5,5,.75), (2,10,.75)])
def test_conservative_current_time_financial_reservation(hour, day, expected):
    wire = normalize_production_request("state_extraction", kwargs(4000))
    budget = GoatBudget(1, 2.0)
    with budget.authorize(wire, datetime(2026,10,day,hour,tzinfo=timezone.utc)):
        proof = budget.authorized_exposure
    assert proof["prompt_upper"] == proof["completion_upper"] == 1_000_000
    assert proof["credit_equivalent_upper"] == expected
    assert budget.api_calls == 0
    assert "max_tokens" not in wire and "max_completion_tokens" not in wire


@pytest.mark.parametrize("hour,limit", [(2,1.4999), (5,.7499)])
def test_insufficient_current_time_credit_leaves_planned_with_zero_send(reviewed_engine, hour, limit):
    (project, _, sends, client, *_), _ = reviewed_engine
    import run_production_engine as launcher
    module = launcher.load_production_module()
    module.initialize_production_runtime_state(activate_provider=False, provider_backend=GOAT)
    budget = GoatBudget(1, limit)
    install_goat_runtime(module, provider_client=client, budget=budget,
        clock=lambda: datetime(2026,10,5,hour,tzinfo=timezone.utc))
    with pytest.raises(GoatRuntimeError, match="ceiling refuses"):
        module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    record = json.loads((project.data_dir / "provider_request_journal/0001.state_extraction.json").read_text())
    assert record["status"] == "planned" and not sends
    assert budget.api_calls == budget.actual_cost_usd == 0


def test_valid_reviewed_capability_authorizes_fake_send_and_replays_once(reviewed_engine):
    (project, module, sends, _, budget, *_), _ = reviewed_engine
    first = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    second = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    assert first["response"] == second["response"] and second["replayed"]
    assert len(sends) == budget.api_calls == 1
    assert "max_tokens" not in sends[0] and "max_completion_tokens" not in sends[0]
    exposure = budget.records[0]["authorization_exposure"]
    assert exposure["prompt_upper"] == exposure["completion_upper"] == 1_000_000
    assert exposure["credit_equivalent_upper"] == 1.5
    assert budget.actual_cost_usd == first["response"]["stats"]["cost_usd"]


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_reviewed_capability_never_reissues_ambiguous_slot(reviewed_engine, status):
    (project, module, sends, _, budget, *_), _ = reviewed_engine
    module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    path = project.data_dir / "provider_request_journal/0001.state_extraction.json"
    record = json.loads(path.read_text())
    module.atomic_write_json(path, _updated_record(record, status=status, response={}, response_sha256=""))
    with pytest.raises(RuntimeError): module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    assert len(sends) == budget.api_calls == 1


def test_recorded_replay_does_not_authorize_or_duplicate_send(reviewed_engine):
    (project, module, sends, _, budget, *_), cap = reviewed_engine
    first = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    Path(cap.evidence_path).unlink()  # No capability is needed for a local replay.
    second = module.journaled_chat_request("state_extraction", 1, "甲", kwargs(4000))
    assert second["response"] == first["response"] and second["replayed"]
    assert budget.api_calls == len(sends) == 1
    assert budget.actual_cost_usd == first["response"]["stats"]["cost_usd"]


def test_real_reviewed_capability_migration_is_ready_in_disposable_incident(incident, monkeypatch, tmp_path):
    from goat_state_policy_migration_plan import build_goat_state_policy_migration_plan
    copied_capability(tmp_path, monkeypatch)
    project = incident[0]
    plan = build_goat_state_policy_migration_plan("fixture", repo_root=project.repository_root,
        max_api_calls=6, max_credit_equivalent_usd=2)
    assert plan["status"] == plan["send_authorization_status"] == "READY"
    assert plan["api_calls"] == plan["credential_reads"] == plan["project_writes"] == 0
    assert plan["exposure"]["prompt_upper"] == plan["exposure"]["completion_upper"] == 1_000_000
    assert plan["migration_proof"]["max_generations"] == plan["recovery_generation_send_limit"] == 1
    assert plan["translation_generation_allowed"] is plan["startup_periodic_audits_allowed"] is False
    assert "max_tokens" not in plan["recovery_wire"] and "max_completion_tokens" not in plan["recovery_wire"]


# Reuse only the disposable incident constructor; no real Ace files are read.
from test_goat_state_policy_migration import incident
