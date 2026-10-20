"""Disposable-only, zero-provider accounting epoch migration proofs."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import production_accounting as accounting
import goat_accounting_migration_plan as planner
import goat_accounting_migration_execute as executor
from noveltranslator_core import add_accounting_totals, add_accounting_totals_once, PRICING_VERSION
from test_controlled_trial_plan import _empty_project, _write_json

GOAT = accounting.PROVIDER_COMMAND_CODE

# Independently pinned complete reviewed identity, never built from engine constants.
FROZEN_V1_POLICY = {
    "revision": "direct-to-goat-accounting-epoch-v1",
    "accounting_schema_version": 2,
    "historical_revision": "sealed-direct-accounting-epoch-v1",
    "active_revision": "active-goat-accounting-epoch-v1",
    "direction": "direct_deepseek->command_code_provider",
    "total_fields": ["total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
                     "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd"],
    "active_metadata": {
        "provider": "command_code_provider", "accounting": "command_code_credit_equivalent",
        "pricing_unit": "usd_credit_equivalent_per_1m_tokens",
        "pricing_version": "command-code-provider-public-ratecard-2026-09-19-v1",
        "provider_invoice_claimed": False,
    },
    "preserve_global_operation_ids": True,
    "any_pending_blocks": True,
    "ambiguous_journal_blocks": True,
    "history_count": 1,
    "zero_direct_adoption": "no-history",
    "currency_conversion": False,
    "boundary": "fixed-at-migration-monotonic-progress",
    "preserve_fields": ["total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
                        "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd",
                        "provider", "accounting", "pricing_unit", "pricing_version", "provider_invoice_claimed",
                        "rate_correct_cost_start_chapter", "accounted_api_operation_ids"],
}
FROZEN_V1_SHA = "9c2a545f43369a3371e65adab4525e776e8485a485cb99a3e14e53b8e104e1f5"


def test_complete_frozen_v1_policy_and_exact_sha():
    assert accounting.migration_v1_policy_identity() == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY_SHA256 == FROZEN_V1_SHA
    assert accounting.canonical_sha256(accounting.migration_v1_policy_identity()) == FROZEN_V1_SHA
    # Independent canonical encoder, not the production hashing helper.
    import hashlib
    encoded = json.dumps(FROZEN_V1_POLICY, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == FROZEN_V1_SHA


def test_frozen_v1_isolated_from_current_metadata_mutation(monkeypatch):
    value = migrated()
    sealed = copy.deepcopy(value)
    monkeypatch.setitem(accounting.GOAT_METADATA, "pricing_version", "future-current-ratecard")
    monkeypatch.setitem(accounting.GOAT_METADATA, "future_current_field", "future-value")
    monkeypatch.setattr(accounting, "GOAT_VERSION", "future-current-ratecard")
    assert accounting.migration_v1_policy_identity() == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY_SHA256 == FROZEN_V1_SHA
    accounting._validate_epochs(value)
    assert value == sealed
    # Current operation accounting remains current; v1 is not a global rate freeze.
    native = dict(completed=[], last_completed=0, next_chapter=1)
    accounting.bind_progress_accounting(native, dict(accounting.GOAT_METADATA))
    assert native["pricing_version"] == "future-current-ratecard"
    assert native["future_current_field"] == "future-value"


def test_frozen_v1_isolated_from_current_metadata_replacement(monkeypatch):
    monkeypatch.setattr(accounting, "GOAT_METADATA", {"provider": "future-provider", "pricing_version": "future-version"})
    monkeypatch.setattr(accounting, "GOAT_VERSION", "future-version")
    assert accounting.migration_v1_policy_identity() == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY_SHA256 == FROZEN_V1_SHA


def test_frozen_v1_survives_current_ratecard_dependency_reload():
    # Fresh process prevents reload from disturbing the suite's accounting module.
    script = f"""
import importlib, json
import production_accounting as accounting
import benchmark_longitudinal_g7d_accounting_policy as rates
rates.PRICING_VERSION = 'future-current-ratecard'
importlib.reload(accounting)
assert accounting.GOAT_METADATA['pricing_version'] == 'future-current-ratecard'
assert accounting.migration_v1_policy_identity() == json.loads({json.dumps(FROZEN_V1_POLICY)!r})
assert accounting.MIGRATION_POLICY_SHA256 == {FROZEN_V1_SHA!r}
accounting._validate_epochs(json.loads({json.dumps(migrated())!r}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_frozen_v1_returns_isolated_copies_and_ignores_inspection_copy(monkeypatch):
    value = migrated()
    identity = accounting.migration_v1_policy_identity()
    identity["active_metadata"]["pricing_version"] = "changed-copy"
    identity["preserve_fields"].clear()
    monkeypatch.setattr(accounting, "MIGRATION_POLICY", identity)
    assert accounting.migration_v1_policy_identity() == FROZEN_V1_POLICY
    assert accounting.MIGRATION_POLICY_SHA256 == FROZEN_V1_SHA
    accounting._validate_epochs(value)


def test_changed_v1_semantics_fail_pinned_hash_and_require_new_revision(monkeypatch):
    altered = copy.deepcopy(FROZEN_V1_POLICY)
    altered["currency_conversion"] = True
    monkeypatch.setattr(accounting, "_MIGRATION_V1_POLICY_JSON", json.dumps(altered))
    with pytest.raises(ValueError, match="new semantics require a new revision"):
        accounting.migration_v1_policy_identity()
    assert accounting.MIGRATION_POLICY_SHA256 == FROZEN_V1_SHA


def legacy():
    return dict(completed=[], last_completed=0, next_chapter=1, failed=[],
        last_audited_chapter=0, total_prompt_tokens=123, total_completion_tokens=12,
        total_cache_hit_tokens=3, total_cache_miss_tokens=120,
        estimated_cost_usd=1.23456789, rate_correct_cost_usd=0.987654,
        rate_correct_cost_start_chapter=1, pricing_version=PRICING_VERSION,
        accounted_api_operation_ids=["legacy-op"], unrelated={"preserve": True})


def migrated(value=None):
    value = legacy() if value is None else value
    return accounting.migrate_direct_progress(value, planner.sha256(planner.json_bytes(value)))


@pytest.fixture
def project(tmp_path):
    project = _empty_project(tmp_path / "repo")
    _write_json(project.progress_file, legacy())
    (project.root / ".production.lock").touch()
    return project


def build(project):
    return planner.build_goat_accounting_migration_plan("fixture", repo_root=project.repository_root)


def apply(project, plan, tmp_path, **kwargs):
    arguments = dict(repo_root=project.repository_root, plan=plan,
        state_fingerprint=plan["state_fingerprint"], approval_token=plan["approval_token"],
        evidence_root=tmp_path / "evidence", apply_accounting_migration=True)
    arguments.update(kwargs)
    return executor.execute_goat_accounting_migration("fixture", **arguments)


def snapshot(project):
    return {p.relative_to(project.root).as_posix(): p.read_bytes()
            for p in project.root.rglob("*") if p.is_file()}


def test_legacy_guard_and_zero_adoption():
    value = legacy()
    before = copy.deepcopy(value)
    assert accounting.validate_progress_accounting(value) == "direct_deepseek"
    with pytest.raises(ValueError, match="mixing forbidden"):
        accounting.validate_progress_accounting(value, expected_provider=GOAT)
    assert value == before
    zero = dict(completed=[], last_completed=0, next_chapter=1)
    accounting.bind_progress_accounting(zero, accounting.GOAT_METADATA)
    assert zero["provider"] == GOAT and not any(k in zero for k in accounting.EPOCH_FIELDS)


@pytest.mark.parametrize("explicit", [False, True])
def test_exact_history_and_presence(explicit):
    value = legacy()
    if explicit:
        value.update(provider="direct_deepseek", accounting="deepseek_usd_peak_offpeak",
                     pricing_unit="usd_per_1m_tokens", provider_invoice_claimed=True)
    after = migrated(value)
    history = after["accounting_history"][0]
    assert history["totals"] == {k: value[k] for k in accounting.TOTAL_FIELDS}
    for key in accounting.LEGACY_FIELDS:
        assert history["legacy_field_presence"][key] == (key in value)
    assert history["legacy_metadata"] == {k: value[k] for k in accounting.GOAT_METADATA if k in value}
    assert history["pricing_version"] == value["pricing_version"]
    assert history["rate_correct_cost_start_chapter"] == value["rate_correct_cost_start_chapter"]
    assert history["accounted_api_operation_ids_at_close"] == value["accounted_api_operation_ids"]
    assert history["source_progress_sha256"] == planner.sha256(planner.json_bytes(value))
    assert history["epoch_payload_sha256"] == accounting.canonical_sha256({k: v for k, v in history.items() if k != "epoch_payload_sha256"})
    assert migrated(value) == after
    assert accounting.validate_progress_accounting(after, expected_provider=GOAT) == GOAT
    assert all(after[k] == 0 for k in accounting.TOTAL_FIELDS)
    assert all(after[k] == v for k, v in accounting.GOAT_METADATA.items())
    for key in value.keys() - set(accounting.TOTAL_FIELDS) - set(accounting.GOAT_METADATA) - {"rate_correct_cost_start_chapter"}:
        assert after[key] == value[key]


@pytest.mark.parametrize("field", accounting.LEGACY_FIELDS)
def test_missing_fields_remain_missing_in_history(field):
    value = legacy()
    value.pop(field, None)
    after = migrated(value)
    assert after["accounting_history"][0]["legacy_field_presence"][field] is False
    if field in accounting.TOTAL_FIELDS:
        assert field not in after["accounting_history"][0]["totals"]


@pytest.mark.parametrize("field", accounting.TOTAL_FIELDS + ("pricing_version", "legacy_metadata", "source_progress_sha256", "epoch_payload_sha256", "revision"))
def test_tampered_history_rejected(field):
    after = migrated()
    epoch = after["accounting_history"][0]
    if field in accounting.TOTAL_FIELDS:
        epoch["totals"][field] += 1
    else:
        epoch[field] = "tampered"
    with pytest.raises(ValueError):
        accounting.validate_progress_accounting(after)


@pytest.mark.parametrize("mutation", ["two", "goat-history", "bad-source-sha", "unknown-revision", "presence", "history-stripped", "active-stripped", "schema-stripped", "active-provider", "active-boundary", "progress-boundary", "registry", "direct-top", "invoice", "cost-start", "bool-index"])
def test_malformed_epoch_rejected(mutation):
    after = migrated()
    epoch = after["accounting_history"][0]
    active = after["active_accounting_epoch"]
    if mutation == "two": after["accounting_history"].append(copy.deepcopy(epoch))
    elif mutation == "goat-history": epoch["normalized_provider"] = GOAT
    elif mutation == "bad-source-sha": epoch["source_progress_sha256"] = "bad"
    elif mutation == "unknown-revision": epoch["revision"] = "unknown"
    elif mutation == "presence": epoch["legacy_field_presence"]["provider"] = True
    elif mutation == "history-stripped": del after["accounting_history"]
    elif mutation == "active-stripped": del after["active_accounting_epoch"]
    elif mutation == "schema-stripped": del after["accounting_schema_version"]
    elif mutation == "active-provider": active["provider"] = "direct_deepseek"
    elif mutation == "active-boundary": active["start_chapter"] += 1
    elif mutation == "progress-boundary": after["next_chapter"] += 1
    elif mutation == "registry": after["accounted_api_operation_ids"] = []
    elif mutation == "direct-top": after["provider"] = "direct_deepseek"
    elif mutation == "invoice": after["provider_invoice_claimed"] = True
    elif mutation == "cost-start": after["rate_correct_cost_start_chapter"] += 1
    elif mutation == "bool-index": active["epoch_index"] = True
    # Even recomputed historical hashes cannot authorize unknown identities.
    epoch["epoch_payload_sha256"] = accounting.canonical_sha256({k: v for k, v in epoch.items() if k != "epoch_payload_sha256"})
    active["historical_epoch_payload_sha256"] = epoch["epoch_payload_sha256"]
    with pytest.raises(ValueError): accounting.validate_progress_accounting(after)


def test_accounting_additions_preserve_history_and_global_registry():
    after = migrated()
    history = copy.deepcopy(after["accounting_history"])
    stats = dict(accounting.GOAT_METADATA, prompt_tokens=20, completion_tokens=10,
        cache_hit_tokens=3, cache_miss_tokens=17, cost_usd=0.0001, rate_correct_cost_usd=0.0001)
    for _ in range(10): add_accounting_totals(after, stats)
    assert add_accounting_totals_once(after, "new-op", stats) is True
    assert add_accounting_totals_once(after, "new-op", stats) is False
    assert add_accounting_totals_once(after, "legacy-op", stats) is False
    assert after["accounted_api_operation_ids"] == ["legacy-op", "new-op"]
    assert after["total_prompt_tokens"] == 220
    assert after["estimated_cost_usd"] == pytest.approx(0.0011)
    assert after["accounting_history"] == history
    after.update(completed=[1], last_completed=1, next_chapter=2)
    accounting.validate_progress_accounting(after)
    with pytest.raises(ValueError): accounting.bind_progress_accounting(after, {})
    with pytest.raises(ValueError): accounting.validate_progress_accounting(after, expected_provider="direct_deepseek")
    after["accounting_history"][0]["totals"]["estimated_cost_usd"] += 1
    with pytest.raises(ValueError): add_accounting_totals_once(after, "legacy-op", stats)


def test_planner_deterministic_readonly_and_bindings(project):
    before = snapshot(project)
    first = build(project)
    assert first == build(project)
    assert first["status"] == planner.READY
    assert snapshot(project) == before
    assert first["state_binding"]["progress_before_sha256"] == planner.sha256(project.progress_file.read_bytes())
    assert first["state_binding"]["code_identity"]["head"]
    assert first["state_binding"]["code_identity"]["code_tree_sha256"]
    assert len(first["state_fingerprint"]) == len(first["approval_token"]) == 64
    assert first["api_calls"] == first["credential_reads"] == first["project_writes"] == 0


@pytest.mark.parametrize("state,status", [("zero", "ZERO_DIRECT_STATE_NO_MIGRATION_REQUIRED"), ("native", "ALREADY_GOAT_ACTIVE"), ("migrated", "ALREADY_GOAT_ACTIVE"), ("unknown", "BLOCKED_UNRECOGNIZED_ACCOUNTING_STATE"), ("negative", "BLOCKED_UNRECOGNIZED_ACCOUNTING_STATE"), ("malformed", "BLOCKED_INVALID_PROGRESS")])
def test_planner_classifications(project, state, status):
    value = legacy()
    if state == "zero": value.update({k: 0 for k in accounting.TOTAL_FIELDS})
    elif state == "native": value.update(accounting.GOAT_METADATA)
    elif state == "migrated": value = migrated()
    elif state == "unknown": value["provider"] = "unknown"
    elif state == "negative": value["estimated_cost_usd"] = -1
    if state == "malformed": project.progress_file.write_bytes(b"{")
    else: _write_json(project.progress_file, value)
    assert build(project)["status"] == status


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_ambiguous_journal_blocks(project, tmp_path, status):
    from production_paid_request_journal import _new_planned_record, _updated_record, request_identity
    record = _new_planned_record(request_identity(operation_kind="translation", novel_id="fixture",
        chapter=1, provider="direct_deepseek", model="deepseek-v4-pro", profile_id="historical_military",
        source_sha256="a" * 64, request_payload={"synthetic": True}))
    record = _updated_record(record, status=status)
    path = project.data_dir / "provider_request_journal/0001.translation.json"
    _write_json(path, record)
    plan = build(project)
    assert plan["status"] == "BLOCKED_AMBIGUOUS_PAID_STATE"
    assert plan["started_uncertain_count"] == 1
    before = snapshot(project)
    with pytest.raises(ValueError): apply(project, plan, tmp_path)
    assert snapshot(project) == before
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("status", ["planned", "response_recorded"])
def test_existing_valid_direct_journal_blocks_without_any_writes(project, tmp_path, status, monkeypatch):
    from production_paid_request_journal import (
        _new_planned_record, _updated_record, _sha256_json, request_identity, inspect_paid_request_journals,
    )
    record = _new_planned_record(request_identity(operation_kind="translation", novel_id="fixture",
        chapter=1, provider="direct_deepseek", model="deepseek-v4-pro", profile_id="historical_military",
        source_sha256="a" * 64, request_payload={"synthetic": True}))
    if status == "response_recorded":
        response = dict(content="Recorded synthetic paid response.", finish_reason="stop",
            stats=dict(prompt_tokens=20, completion_tokens=10, cache_hit_tokens=3, cache_miss_tokens=17,
                cost_usd=0.01, rate_correct_cost_usd=0.01, provider="direct_deepseek",
                accounting="deepseek_usd_peak_offpeak", pricing_unit="usd_per_1m_tokens",
                pricing_version=PRICING_VERSION),
            pricing=dict(request_started_utc="2026-10-06T12:00:00+00:00"))
        record = _updated_record(record, status=status, response=response, response_sha256=_sha256_json(response))
    path = project.data_dir / "provider_request_journal/0001.translation.json"
    _write_json(path, record)
    inspection = inspect_paid_request_journals(path.parent)
    assert inspection["errors"] == [] and inspection["uncertain"] == []
    assert [r["status"] for r in inspection["records"]] == [status]
    assert list(project.pending_dir.iterdir()) == []
    assert record["operation_id"] not in json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]
    before = snapshot(project)
    plan = build(project)
    assert plan["status"] == "BLOCKED_EXISTING_PAID_JOURNAL"
    assert plan["proposed_progress"] is None
    assert plan["api_calls"] == plan["credential_reads"] == plan["project_writes"] == 0
    assert snapshot(project) == before
    def forbidden(*args, **kwargs): pytest.fail("blocked plan reached write/lock/evidence path")
    monkeypatch.setattr(executor, "atomic_write_bytes", forbidden)
    monkeypatch.setattr(executor, "_evidence", forbidden)
    monkeypatch.setattr(executor, "production_run_lock", forbidden)
    with pytest.raises(ValueError, match="eligible READY plan"):
        apply(project, plan, tmp_path)
    assert snapshot(project) == before
    assert not (tmp_path / "evidence").exists()


def test_malformed_journal_priority_and_zero_journal_ready(project, tmp_path):
    assert build(project)["status"] == planner.READY
    path = project.data_dir / "provider_request_journal/0001.translation.json"
    _write_json(path, {"status": "response_recorded", "response": {"content": "unproven"}})
    # Ambiguous/malformed journals outrank even the existing pending blocker.
    (project.pending_dir / "0205.md").write_bytes(b"synthetic paid pending")
    before = snapshot(project)
    plan = build(project)
    assert plan["status"] == "BLOCKED_AMBIGUOUS_PAID_STATE"
    assert plan["state_binding"]["journal_inspection"]["errors"]
    with pytest.raises(ValueError): apply(project, plan, tmp_path)
    assert snapshot(project) == before and not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("status", ["planned", "response_recorded"])
def test_new_nonambiguous_journal_after_ready_plan_refuses_stale_apply(project, tmp_path, status):
    from production_paid_request_journal import _new_planned_record, _updated_record, _sha256_json, request_identity
    plan = build(project)
    record = _new_planned_record(request_identity(operation_kind="translation", novel_id="fixture",
        chapter=1, provider="direct_deepseek", model="deepseek-v4-pro", profile_id="historical_military",
        source_sha256="a" * 64, request_payload={"synthetic": True}))
    if status == "response_recorded":
        response = {"content": "Synthetic recorded paid work", "stats": {"prompt_tokens": 20, "completion_tokens": 10}}
        record = _updated_record(record, status=status, response=response, response_sha256=_sha256_json(response))
    _write_json(project.data_dir / "provider_request_journal/0001.translation.json", record)
    before = snapshot(project)
    with pytest.raises(ValueError, match="stale plan/current state mismatch"):
        apply(project, plan, tmp_path)
    assert snapshot(project) == before and not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("target", ["novel-root", "progress"])
def test_selected_state_symlink_rejected(project, tmp_path, target):
    plan = build(project)
    source = project.root if target == "novel-root" else project.progress_file
    destination = tmp_path / "synthetic-indirection-target"
    original = snapshot(project) if target == "novel-root" else source.read_bytes()
    source.rename(destination)
    try:
        source.symlink_to(destination, target_is_directory=target == "novel-root")
    except OSError as exc:
        destination.rename(source)
        pytest.skip("test host cannot create symlinks: " + str(exc))
    with pytest.raises(ValueError, match="escapes novel root|linked novel state"):
        build(project)
    with pytest.raises(ValueError, match="escapes novel root|linked novel state"):
        apply(project, plan, tmp_path)
    assert not (tmp_path / "evidence").exists()
    if target == "novel-root":
        assert {p.relative_to(destination).as_posix(): p.read_bytes()
                for p in destination.rglob("*") if p.is_file()} == original
    else:
        assert destination.read_bytes() == original


@pytest.mark.parametrize("suffix", ["json", "md"])
def test_ming_like_pending_blocks(project, tmp_path, suffix):
    # Legacy/unpinned translated Chapter 205, generically blocked by inventory.
    path = project.pending_dir / ("0205." + suffix)
    path.write_bytes(b'{"chapter":205,"stage":"translated"}' if suffix == "json" else b"paid English")
    before = snapshot(project)
    plan = build(project)
    assert plan["status"] == "BLOCKED_PENDING_PAID_WORK"
    with pytest.raises(ValueError): apply(project, plan, tmp_path)
    assert snapshot(project) == before
    assert not (project.data_dir / "provider_request_journal").exists()


@pytest.mark.parametrize("case", ["no-apply", "token", "fingerprint", "plan-sha", "progress", "pending", "journal", "temp", "identity", "missing-lock", "evidence-inside"])
def test_executor_failclosed(project, tmp_path, case):
    plan = build(project)
    kwargs = {}
    if case == "no-apply": kwargs["apply_accounting_migration"] = False
    elif case == "token": kwargs["approval_token"] = "bad"
    elif case == "fingerprint": kwargs["state_fingerprint"] = "bad"
    elif case == "plan-sha": plan["plan_sha256"] = "bad"
    elif case == "progress": project.progress_file.write_bytes(project.progress_file.read_bytes() + b" ")
    elif case == "pending": (project.pending_dir / "0205.md").write_bytes(b"paid")
    elif case == "journal": _write_json(project.data_dir / "provider_request_journal/0001.translation.json", {"status": "started"})
    elif case == "temp": (project.data_dir / ".progress.json.interrupted.tmp").write_bytes(b"unproven")
    elif case == "identity": _write_json(project.config_file, dict(project.config.to_dict(), title="drift"))
    elif case == "missing-lock": (project.root / ".production.lock").unlink()
    elif case == "evidence-inside": kwargs["evidence_root"] = project.root / "evidence"
    before = snapshot(project)
    if case == "no-apply": assert apply(project, plan, tmp_path, **kwargs)["status"] == "NOT_APPLIED"
    else:
        with pytest.raises(ValueError): apply(project, plan, tmp_path, **kwargs)
    assert snapshot(project) == before


def test_apply_only_progress_exact_and_idempotent(project, tmp_path, monkeypatch):
    plan = build(project)
    before = snapshot(project)
    receipt = apply(project, plan, tmp_path)
    after = snapshot(project)
    assert {k for k in before if before[k] != after[k]} == {"data/progress.json"}
    assert before.keys() == after.keys()
    assert after["data/progress.json"] == planner.json_bytes(plan["proposed_progress"])
    accounting.validate_progress_accounting(json.loads(after["data/progress.json"]))
    directory = Path(receipt["evidence_directory"])
    assert (directory / "progress.before.json").read_bytes() == before["data/progress.json"]
    assert receipt["progress_after_sha256"] == planner.sha256(after["data/progress.json"])
    assert json.loads((directory / "receipt.json").read_bytes())["plan_sha256"] == plan["plan_sha256"]
    monkeypatch.setattr(executor, "atomic_write_bytes", lambda *a: pytest.fail("second write"))
    assert apply(project, plan, tmp_path)["status"] == "ALREADY_APPLIED"
    assert snapshot(project) == after
    value = json.loads(project.progress_file.read_bytes())
    value["estimated_cost_usd"] = 0.01
    _write_json(project.progress_file, value)
    before_replay = snapshot(project)
    with pytest.raises(ValueError): apply(project, plan, tmp_path)
    assert snapshot(project) == before_replay


def test_receipt_interruption_replay(project, tmp_path, monkeypatch):
    plan = build(project)
    original = executor._evidence
    def interrupt(path, content):
        if path.name == "receipt.json": raise OSError("interrupted receipt")
        original(path, content)
    monkeypatch.setattr(executor, "_evidence", interrupt)
    with pytest.raises(OSError): apply(project, plan, tmp_path)
    assert project.progress_file.read_bytes() == planner.json_bytes(plan["proposed_progress"])
    monkeypatch.setattr(executor, "_evidence", original)
    assert apply(project, plan, tmp_path)["status"] == "ALREADY_APPLIED"


def test_reporting_separate_units():
    value = migrated()
    value["estimated_cost_usd"] = 0.5
    lines = accounting.accounting_report_lines(value)
    assert "Sealed historical direct DeepSeek accounting (USD)" in lines[0]
    assert "1.234568" in lines[0]
    assert "Active GOAT accounting (USD credit-equivalent; provider_invoice_claimed=false)" in lines[1]
    assert "0.500000" in lines[1]
    assert "1.734568" not in " ".join(lines)


def test_recent_costs_do_not_mix_epochs(tmp_path):
    from noveltranslator_observability import recent_chapter_metrics
    events = [dict(event="chapter_commit", chapter=1, cost_usd=100),
        dict(accounting.GOAT_METADATA, event="chapter_commit", chapter=2, cost_usd=20),
        dict(accounting.GOAT_METADATA, event="chapter_commit", chapter=3, cost_usd=0.25)]
    (tmp_path / "run_fixture.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    result = recent_chapter_metrics(tmp_path, accounting_metadata=accounting.GOAT_METADATA, start_chapter=3)
    assert result["average_chapter_cost"] == 0.25 and result["sample_size"] == 1


def test_imports_no_credentials_or_client():
    script = """
import sys
class Guard:
    def find_spec(self, fullname, *args):
        if fullname in {'command_code_provider_credentials', 'command_code_provider_transport',
                        'production_goat_runtime', 'openai', 'dotenv', 'ctypes'}:
            raise RuntimeError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
import goat_accounting_migration_plan, goat_accounting_migration_execute
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_preflight_safe_route_after_migration(project, tmp_path):
    from production_path_preflight import run_production_path_preflight
    from operator_preflight import build_operator_preflight
    before = run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert "cross-provider cumulative accounting mixing forbidden" in str(before)
    apply(project, build(project), tmp_path)
    result = run_production_path_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert result["status"] == "PASS", result
    operator = build_operator_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT)
    assert operator["paid_execution_authorized"] is False
    assert "mixing forbidden" not in str(operator)
    bounded = build_operator_preflight("fixture", repo_root=project.repository_root,
        model_selection="v41-flash", provider_selection=GOAT,
        max_api_calls=4, max_credit_equivalent_usd=0.1)
    assert bounded["status"] == "CONTROLLED_TRIAL_READY"
    assert bounded["paid_execution_authorized"] is False
    assert bounded["recommended_entrypoint"] == "controlled_trial_plan.py"


def test_validate_project_accepts_and_rejects_epoch(project, tmp_path, capsys):
    import validate_project
    import shutil
    profile = Path(accounting.__file__).parent / "profiles/historical_military"
    shutil.copytree(profile, project.repository_root / "profiles/historical_military")
    apply(project, build(project), tmp_path)
    assert validate_project.validate_project(project) == 0
    text = capsys.readouterr().out
    assert "Sealed historical direct DeepSeek" in text and "Active GOAT" in text
    value = json.loads(project.progress_file.read_bytes())
    value["accounting_history"][0]["totals"]["estimated_cost_usd"] += 1
    _write_json(project.progress_file, value)
    assert validate_project.validate_project(project) == 1
    assert "invalid historical payload SHA" in capsys.readouterr().out


def test_external_backup_failure_prevents_progress_write(project, tmp_path, monkeypatch):
    plan, before = build(project), snapshot(project)
    def fail(*args): raise OSError("evidence unavailable")
    monkeypatch.setattr(executor, "_evidence", fail)
    with pytest.raises(OSError): apply(project, plan, tmp_path)
    assert snapshot(project) == before


def test_replace_error_after_visible_commit(project, tmp_path, monkeypatch):
    plan = build(project)
    original = executor.atomic_write_bytes
    def committed_error(path, content):
        original(path, content)
        raise OSError("observable replace completed")
    monkeypatch.setattr(executor, "atomic_write_bytes", committed_error)
    assert apply(project, plan, tmp_path)["status"] == "APPLIED"


def test_crash_before_replace_preserves_unknown_temp(project, tmp_path, monkeypatch):
    plan = build(project)
    before = project.progress_file.read_bytes()
    path = project.data_dir / ".progress.json.crashed.tmp"
    def crash(target, content):
        path.write_bytes(content[:20])
        raise SystemExit("process interruption")
    monkeypatch.setattr(executor, "atomic_write_bytes", crash)
    with pytest.raises(SystemExit): apply(project, plan, tmp_path)
    assert project.progress_file.read_bytes() == before
    temp = path.read_bytes()
    with pytest.raises(ValueError): apply(project, plan, tmp_path)
    assert path.read_bytes() == temp and project.progress_file.read_bytes() == before
