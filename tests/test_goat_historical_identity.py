"""Independent literal v1 identity and credential-free migration boundary proofs."""
import hashlib
import json

import pytest
import production_provider_runtime as providers
from test_production_goat_runtime import engine, zero_network

# Independent expected snapshot: do not derive expectations from production helpers.
EXPECTED = {
    "provider": "command_code_provider",
    "base_url": "https://api.commandcode.ai/provider/v1",
    "chat_completions_path": "/chat/completions",
    "wire_model": "deepseek/deepseek-v4.1-flash",
    "reasoning_effort": "low", "translation_completion_tokens": 24000,
    "auxiliary_completion_tokens": 8000,
    "accounting": "command_code_credit_equivalent",
    "pricing_unit": "usd_credit_equivalent_per_1m_tokens",
    "revision": "production-provider-runtime-v1",
    "logical_model": "deepseek-flash", "model_version": "DeepSeek-V4.1-Flash",
    "reasoning_by_operation": {"translation": "low", "state_extraction": "low",
        "glossary_review": "low", "periodic_audit": "low", "state_json_repair": "low",
        "terminology_repair": "low"},
    "translation_budget_policy_sha256": "13d8c63f629f26f6e4bc098deba80a865075276859ab858a3e2c8ddc03b0539a",
    "auxiliary_budget_policy_sha256": "1246c5a6ec736e5e3246c02536eb72cb6a088a99f08f0bf5bcb2795d91f1a4e9",
    "accounting_policy_sha256": "1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0",
    "pricing_version": "command-code-provider-public-ratecard-2026-09-19-v1",
    "provider_invoice_claimed": False, "scheduler_mode": "provider_unrestricted",
}
EXPECTED_SHA = "f0706bfd4d7fd953ad64abdefcc438a37632225afa87564a6f58537b17ee9cd8"


@pytest.fixture(autouse=True)
def restore_core_policy():
    # A refused launcher preparation can apply the logical model before finding
    # historical pending. Preserve process-global test policy across that refusal.
    from controlled_trial_execute import _capture_core_runtime, _restore_core_runtime
    before = _capture_core_runtime()
    yield
    _restore_core_runtime(before)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()


def test_full_frozen_snapshot_hash_and_distinct_active_identity():
    import production_goat_saturation_recovery as v1
    assert providers.historical_goat_provider_identity() == EXPECTED
    assert digest(EXPECTED) == EXPECTED_SHA
    assert providers.HISTORICAL_GOAT_PROVIDER_IDENTITY_SHA256 == EXPECTED_SHA
    assert digest(providers.historical_goat_provider_identity()) == EXPECTED_SHA
    assert v1.policy_sha256() == "17a59c838993201495ca02c5758229abefdc48e327aa48c701265917092b3e63"
    active = providers.provider_identity("command_code_provider", "v41-flash")
    assert active != EXPECTED and digest(active) != EXPECTED_SHA
    assert active["translation_completion_tokens"] == 50000


@pytest.mark.parametrize("field", list(EXPECTED) + ["translation_budget_policy_revision"])
def test_every_active_field_can_change_without_historical_drift(monkeypatch, field):
    active = providers.provider_identity("command_code_provider", "v41-flash")
    active[field] = {"future": "changed"}
    monkeypatch.setattr(providers, "provider_identity", lambda *a, **k: active)
    assert providers.historical_goat_provider_identity() == EXPECTED
    assert digest(providers.historical_goat_provider_identity()) == EXPECTED_SHA
    assert providers.HISTORICAL_GOAT_PROVIDER_IDENTITY_SHA256 == EXPECTED_SHA


def test_active_dependency_changes_and_returned_mutation_cannot_change_snapshot(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("historical snapshot consulted an active dependency")
    for name in ("provider_identity", "transport_identity", "reasoning_by_operation",
                 "goat_policy_sha256", "resolve_translation_model", "selected_provider"):
        monkeypatch.setattr(providers, name, forbidden)
    for name in ("GOAT_POLICY_REVISION", "G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256",
                 "G7D_ACCOUNTING_SHA256", "PRICING_VERSION", "G7J_POLICY_SHA256"):
        monkeypatch.setattr(providers, name, "future-policy")
    changed = providers.historical_goat_provider_identity()
    changed["reasoning_by_operation"]["translation"] = "high"
    assert providers.historical_goat_provider_identity() == EXPECTED
    assert digest(providers.historical_goat_provider_identity()) == EXPECTED_SHA


def snapshot(project):
    return {str(p.relative_to(project.root)): p.read_bytes()
            for p in project.root.rglob("*") if p.is_file()}


@pytest.fixture(params=["normal_stop", "pr147_empty_review"])
def old_pending(request, tmp_path, monkeypatch):
    """Generate exact old shapes with injected literal v1 policy, then restore v2."""
    import production_goat_runtime as runtime
    import run_production_engine as launcher
    import production_goat_cap_recovery as cap
    import production_goat_glossary_review_recovery as review
    from test_production_goat_runtime import response
    from test_goat_glossary_review_recovery import configure, incident
    with monkeypatch.context() as old:
        for module in (runtime, launcher, cap, review):
            old.setattr(module, "provider_identity", lambda *a, **k: json.loads(json.dumps(EXPECTED)))
        normalize = runtime.normalize_production_request
        def old_normalize(logical, kwargs):
            wire = normalize(logical, kwargs)
            if logical == "translation":
                wire["max_completion_tokens"] = 24000
            return wire
        old.setattr(runtime, "normalize_production_request", old_normalize)
        generator = engine.__wrapped__(tmp_path, old)
        value = next(generator)
        try:
            if request.param == "pr147_empty_review":
                project, module, sends, _ = incident(value)
            else:
                project, module, sends, budget = configure(value, [response(content="A man entered the hall.")])
                budget.max_api_calls = 1
                assert module.process_chapter(1) is False
            records = list((project.data_dir / "provider_request_journal").glob("*.json"))
            from production_paid_request_journal import load_matching_journal
            for path in records:
                assert load_matching_journal(path, {})["provider_policy"] == EXPECTED
            meta = json.loads(project.pending_dir.joinpath("0001.json").read_text(encoding="utf-8"))
            assert meta["translation_provider_identity"] == EXPECTED
            assert sends[0]["max_completion_tokens"] == 24000
            if request.param == "pr147_empty_review":
                assert sends[2]["max_completion_tokens"] == 8000
                from test_goat_glossary_review_recovery import initial_path
                record = load_matching_journal(initial_path(project), {})
                assert review.eligible_envelope(record["response"], sends[2], original=True)
            assert meta.get("goat_reasoning_saturation_recovery_used") is not True
        finally:
            generator.close()
    return project, sends, request.param


def test_old_pending_manual_review_before_send_or_new_journal(old_pending, monkeypatch):
    project, sends, shape = old_pending
    import run_production_engine as launcher
    from operator_preflight import build_operator_preflight
    from goat_glossary_review_recovery_plan import build_goat_glossary_review_recovery_plan
    before = snapshot(project)
    count = len(sends)
    with pytest.raises(providers.HistoricalGoatManualReviewRequired):
        launcher.prepare_production_runtime("v41-flash", project.pending_dir,
                                            provider_selection="command_code_provider")
    route = build_operator_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection="command_code_provider",
        max_api_calls=6, max_credit_equivalent_usd=1)
    assert route["status"] == "GOAT_HISTORICAL_MANUAL_REVIEW", route
    assert route["recommended_action"] == "manual_review_historical_goat_evidence"
    assert route["api_calls"] == route["project_writes"] == 0
    assert route["paid_execution_authorized"] is False
    assert route["recommended_entrypoint"] == ""
    if shape == "pr147_empty_review":
        with pytest.raises((ValueError, RuntimeError), match="GOAT_HISTORICAL_MANUAL_REVIEW"):
            build_goat_glossary_review_recovery_plan("fixture", repo_root=project.repository_root,
                max_api_calls=6, max_credit_equivalent_usd=1)
    assert len(sends) == count
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["state_extraction", "chapter_glossary_review"])
def test_exact_old_auxiliary_journal_cannot_replay_or_send_under_active_runtime(engine, kind):
    from production_paid_request_journal import _updated_record, load_matching_journal
    import production_goat_saturation_recovery as v1
    from test_production_goat_runtime import kwargs
    project, module, sends, _, budget, *_ = engine
    # Obtain a valid synthetic response and journal, then rebuild the exact old
    # immutable identity and envelope (8K wire unchanged, v1 provider policy).
    module.journaled_chat_request(kind, 1, "甲", kwargs(4000))
    wire = sends[0]
    path = project.data_dir / f"provider_request_journal/0001.{kind}.json"
    record = load_matching_journal(path, {})
    identity = v1.journal_identity(kind=kind, novel_id=record["novel_id"],
        chapter=1, profile_id=record["profile_id"], source_sha256=record["source_sha256"], wire=wire)
    envelope = v1.accepted_envelope(record["response"], wire)
    path.write_text(json.dumps(_updated_record(record, **identity, response=envelope,
        response_sha256=digest(envelope))), encoding="utf-8")
    assert load_matching_journal(path, {})["provider_policy"] == EXPECTED
    before = snapshot(project)
    calls = budget.api_calls
    with pytest.raises(providers.HistoricalGoatManualReviewRequired):
        module.journaled_chat_request(kind, 1, "甲", kwargs(4000))
    assert len(sends) == 1 and budget.api_calls == calls
    assert snapshot(project) == before
