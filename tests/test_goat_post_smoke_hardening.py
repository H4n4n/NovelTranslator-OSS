"""Post-smoke regressions: disposable state, fake transport, no secret reads."""
import copy
import json
from types import SimpleNamespace as NS
import pytest
import noveltranslator_core as core
from production_accounting import GOAT_METADATA, validate_progress_accounting
from test_production_goat_runtime import engine, zero_network, kwargs, response, GOAT, CLOCK
from test_controlled_trial_plan import _empty_project
from production_paid_request_journal import _updated_record, _sha256_json
from production_path_preflight import run_production_path_preflight
from command_code_provider_transport import account_command_code_usage
from production_goat_runtime import GoatRuntimeError
from noveltranslator_observability import stats_event_fields


def live_stats(engine):
    project, module, sends, client, budget, *_ = engine
    raw = response(completion=3617, reasoning=3501)
    def send(**body):
        sends.append(body)
        return raw
    client.chat.completions.create = send
    result = module.journaled_chat_request("chapter_translation", 1, "source", kwargs())
    return result["response"]["stats"]


def test_live_nested_reasoning_fixture_replay_cost_and_legacy_enrichment(engine):
    project, module, sends, client, budget, *_ = engine
    stats = live_stats(engine)
    assert stats["completion_tokens"] == 3617
    assert stats["reasoning_tokens"] == 3501
    assert stats["reasoning_tokens_exposed"] is True
    assert stats["visible_completion_tokens"] == 116
    expected = account_command_code_usage("deepseek/deepseek-v4.1-flash",
        NS(prompt_tokens=20, completion_tokens=3617, cached_tokens=3, cached_tokens_exposed=True), CLOCK())
    assert stats["cost_usd"] == stats["rate_correct_cost_usd"] == expected["cost_usd"]
    assert core.validate_stats_record(stats, "live") == []
    assert stats_event_fields(stats)["reasoning_tokens"] == 3501
    path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["response"]["raw_response"]["usage"]["completion_tokens_details"] == {"reasoning_tokens":3501}
    # Reproduce the pre-hardening envelope without touching frozen artifacts.
    envelope = copy.deepcopy(record["response"])
    for key in ("reasoning_tokens", "reasoning_tokens_exposed", "visible_completion_tokens", "provider"):
        envelope["stats"].pop(key)
    module.atomic_write_json(path, _updated_record(record, response=envelope, response_sha256=_sha256_json(envelope)))
    client.chat.completions.create = lambda **body: pytest.fail("replay sent")
    replay = module.journaled_chat_request("chapter_translation", 1, "source", kwargs())
    assert replay["replayed"] and replay["response"]["stats"] == stats
    assert len(sends) == budget.api_calls == 1
    assert budget.actual_cost_usd == expected["cost_usd"]
    assert len(budget.response_records) == 2 and budget.response_records[-1]["replayed"]


@pytest.mark.parametrize("reasoning", [-1, 3618, True])
def test_invalid_reasoning_is_durable_but_never_accepted_or_reissued(engine, reasoning):
    project, module, sends, client, budget, *_ = engine
    def send(**body):
        sends.append(body)
        return response(completion=3617, reasoning=reasoning)
    client.chat.completions.create = send
    for _ in range(2):
        with pytest.raises(GoatRuntimeError):
            module.journaled_chat_request("chapter_translation", 1, "source", kwargs())
    record = json.loads((project.data_dir / "provider_request_journal/0001.chapter_translation.json").read_text(encoding="utf-8"))
    assert record["status"] == "response_recorded" and "stats" not in record["response"]
    assert len(sends) == 1 and budget.actual_cost_usd == 0


def test_not_exposed_not_guessed_and_aggregate_not_double_counted(engine):
    _, module, *_ = engine
    exposed = live_stats(engine)
    combined = module.combine_stats(exposed, exposed, module.zero_stats())
    assert core.validate_stats_record(combined, "combined") == []
    assert combined["completion_tokens"] == 7234
    assert combined["reasoning_tokens"] == 7002
    assert combined["visible_completion_tokens"] == 232
    assert combined["cost_usd"] == exposed["cost_usd"] * 2
    unknown = copy.deepcopy(exposed)
    unknown.update(reasoning_tokens=None, reasoning_tokens_exposed=False)
    unknown.pop("visible_completion_tokens")
    mixed = module.combine_stats(exposed, unknown)
    assert mixed["reasoning_tokens_exposed"] is False and mixed["reasoning_tokens"] is None
    assert "visible_completion_tokens" not in mixed
    assert core.validate_stats_record(mixed, "mixed") == []
    unknown["provider"] = "direct_deepseek"
    with pytest.raises(ValueError): module.combine_stats(exposed, unknown)


def test_fresh_goat_commit_restart_and_idempotent_auxiliary_accounting(engine):
    project, module, *_ = engine
    stats = live_stats(engine)
    progress = {"completed": [], "last_completed": 0, "next_chapter": 1, "pricing_version": core.PRICING_VERSION}
    assert core.commit_chapter_accounting(progress, 1, stats)
    assert all(progress[key] == value for key, value in GOAT_METADATA.items())
    assert progress["estimated_cost_usd"] == stats["cost_usd"]
    module.atomic_write_json(project.progress_file, progress)
    restored = json.loads(project.progress_file.read_text(encoding="utf-8"))
    validate_progress_accounting(restored, expected_provider=GOAT, direct_pricing_version=core.PRICING_VERSION)
    assert not core.commit_chapter_accounting(restored, 1, stats)
    assert core.add_accounting_totals_once(restored, "auxiliary", stats)
    assert not core.add_accounting_totals_once(restored, "auxiliary", stats)
    assert restored["estimated_cost_usd"] == stats["cost_usd"] * 2


@pytest.mark.parametrize("field", ["estimated_cost_usd", "rate_correct_cost_usd", "total_prompt_tokens", "total_completion_tokens"])
def test_historical_direct_cannot_be_relabeled_and_preflight_refuses(engine, field):
    project, module, *_ = engine
    stats = live_stats(engine)
    progress = {"completed": [], "last_completed": 0, "next_chapter": 1, "pricing_version": core.PRICING_VERSION, field: 1}
    original = copy.deepcopy(progress)
    with pytest.raises(ValueError, match="mixing"):
        core.add_accounting_totals(progress, stats)
    assert progress == original
    module.atomic_write_json(project.progress_file, progress)
    report = run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert report["status"] == "FAIL"
    assert any("mixing" in error for error in report["errors"]), report


def test_direct_legacy_semantics_and_goat_to_direct_refusal(engine):
    stats = live_stats(engine)
    direct = {"pricing_version": core.PRICING_VERSION}
    direct_stats = {key: value for key,value in stats.items() if key in core.ACCOUNTING_FIELDS}
    core.add_accounting_totals(direct, direct_stats)
    assert direct["pricing_version"] == core.PRICING_VERSION
    assert "provider" not in direct
    historical = copy.deepcopy(direct)
    validate_progress_accounting(direct, expected_provider="direct_deepseek", direct_pricing_version=core.PRICING_VERSION)
    assert direct == historical
    goat = dict(GOAT_METADATA)
    with pytest.raises(ValueError): core.add_accounting_totals(goat, direct_stats)


@pytest.mark.parametrize("language,source_size,translation_size,warning", [
    ("zh",215,471,False), ("ko",215,471,False),
    ("zh",215,15,True), ("ko",215,15,True),
    ("zh",3000,500,True), ("ko",3000,500,True),
    ("zh",100,220,False), ("ko",100,220,False),
    ("zh",3000,4000,False), ("zh",215,0,False),
])
def test_source_aware_short_output_preserves_empty_error(engine, language, source_size, translation_size, warning):
    _, module, *_ = engine
    source = ("甲" if language == "zh" else "가") * source_size
    translation = "a" * translation_size
    qa = module.local_qa(1, source, translation, "stop")
    assert ("Translation suspiciously short" in qa["issues"]) is warning
    assert ("Translation is empty" in qa["issues"]) is (translation_size == 0)
    if translation_size == 0:
        assert not qa["commit_allowed"]


def test_replay_cannot_drop_accounting_fields(engine):
    project, module, *_ = engine
    live_stats(engine)
    path = project.data_dir / "provider_request_journal/0001.chapter_translation.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    envelope = copy.deepcopy(record["response"])
    envelope["stats"].pop("cost_usd")
    module.atomic_write_json(path, _updated_record(record, response=envelope, response_sha256=_sha256_json(envelope)))
    with pytest.raises(GoatRuntimeError):
        module.journaled_chat_request("chapter_translation", 1, "source", kwargs())


def test_nested_aggregate_validation_and_zero_usage_binding(engine):
    _, module, *_ = engine
    stats = live_stats(engine)
    aggregate = module.combine_stats(module.combine_stats(stats, stats), stats)
    assert len(aggregate["accounting_components"]) == 3
    assert aggregate["completion_tokens"] == 3617 * 3
    assert core.validate_stats_record(aggregate, "aggregate") == []
    aggregate["reasoning_tokens"] += 1
    assert core.validate_stats_record(aggregate, "bad")
    zero = {**module.zero_stats(), **GOAT_METADATA, "reasoning_tokens_exposed": False, "reasoning_tokens":None}
    combined = module.combine_stats(zero, module.zero_stats())
    progress = {}
    core.add_accounting_totals(progress, combined)
    assert all(progress[key] == value for key, value in GOAT_METADATA.items())


@pytest.mark.parametrize("metadata", [
    {"provider":"unknown"},
    {"provider":"direct_deepseek", "accounting":"command_code_credit_equivalent"},
    {"provider":"direct_deepseek", "pricing_unit":"usd_credit_equivalent_per_1m_tokens"},
    {"pricing_version":GOAT_METADATA["pricing_version"]},
])
def test_operation_metadata_cannot_silently_mix(metadata):
    progress = {}
    stats = {"prompt_tokens":1, "completion_tokens":1, "cost_usd":0.01, **metadata}
    with pytest.raises(ValueError): core.add_accounting_totals(progress, stats)
    assert progress == {}
