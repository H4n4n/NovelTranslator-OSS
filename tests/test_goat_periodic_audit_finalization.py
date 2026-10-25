"""Synthetic 376–400 incident, exact paid replay and publication crash proofs."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS, FunctionType

import pytest

import noveltranslator_core as core
import production_periodic_audit_finalization as final
from production_accounting import migrate_direct_progress, GOAT_METADATA, combine_goat_stats
from production_goat_runtime import install_goat_runtime, GoatBudget
from production_provider_runtime import provider_identity
from production_paid_request_journal import _with_record_hash
from production_provider_errors import ProductionProviderError
from goat_periodic_audit_finalize_plan import build_goat_periodic_audit_finalize_plan, local_module
from goat_periodic_audit_finalize_execute import execute_goat_periodic_audit_finalize
from novel_project import load_novel_project
from test_controlled_trial_plan import _empty_project, _write_json
from run_production_engine import load_production_module
from chapter_library import index_chapter_library
from translation_profile import load_translation_profile, load_novel_terminology_policy, profile_audit_instructions
from pathlib import Path

CLOCK = lambda: datetime(2026, 10, 12, 11, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("synthetic_goat_hard_bound")


def locked_finalize(module, *args, **kwargs):
    from production_run_lock import production_run_lock
    with production_run_lock(Path(module.OUTPUT_DIR).absolute().parent.parent):
        return final.finalize_periodic_audit(module, *args, **kwargs)


def snapshot(project):
    return {str(p.relative_to(project.root)): p.read_bytes() for p in project.root.rglob("*") if p.is_file()}


@pytest.fixture
def incident(tmp_path, monkeypatch, request):
    project = _empty_project(tmp_path)
    config = json.loads(project.config_file.read_bytes())
    config.update(final_chapter=500, profile="xianxia_xuanhuan")
    _write_json(project.config_file, config)
    terms = [chr(0x5200 + i) + "山嶺" for i in range(45)]
    fixture_mode = getattr(request, "param", "audit")
    chapters = [dict(chapter=n, production_index=n, text="普通文字") for n in range(1, 501)]
    chapters[387]["text"] = "有榮山在此"
    if fixture_mode.startswith("mixed"):
        chapters[1]["text"] = "有榮山高聳"
        chapters[0]["text"] = "有榮山在此。乙在此。"
    chapters[399]["text"] = "。".join(terms) + "。有熙氏。"
    _write_json(project.chapters_file, chapters)
    glossary = {"entries": {term: dict(english=f"Place {i}", category="location", status="provisional", first_chapter=400) for i, term in enumerate(terms)}}
    glossary["entries"].update({"有榮山": dict(english="Mount Rong", category="location", status="provisional", first_chapter=388),
        "有熙氏": dict(english="Youxi Clan", category="clan", status="locked", first_chapter=2, strict_qa=True, strict_qa_from_chapter=397)})
    if fixture_mode in {"mixed_competing", "mixed_nested"}:
        glossary["entries"]["乙"] = dict(english="Mount Rong" if fixture_mode == "mixed_competing" else "Greater Mount Rong",
            category="location", status="locked", first_chapter=1)
    if fixture_mode == "mixed_contextual":
        glossary["entries"]["有榮山"]["status"] = "contextual"
    _write_json(project.glossary_file, glossary)
    direct = dict(completed=list(range(1, 398)), last_completed=397, next_chapter=398, last_audited_chapter=375,
                  total_prompt_tokens=100, total_completion_tokens=20, estimated_cost_usd=0.01)
    progress = migrate_direct_progress(direct, "0" * 64)
    progress.update(completed=list(range(1, 401)), last_completed=400, next_chapter=401)
    _write_json(project.progress_file, progress)
    _write_json(project.continuity_file, dict(latest_chapter=400, summary="fixture"))
    for n in range(1, 401):
        (project.chapter_dir / f"{n:04d}.md").write_bytes(b"Ordinary English.\r\n")
    (project.chapter_dir / "0388.md").write_bytes(b"# Chapter 388\r\nMount Rong stood there.\r\n")
    if fixture_mode.startswith("mixed"):
        (project.chapter_dir / "0002.md").write_bytes("# Chapter 2\r\nMount Yourong — café.\r\n".encode())
        extra = {"mixed_multi": "# Heading\r\n\r\n> **Mount Rong** — café.\r\n",
                 "mixed_case": "mount rong remained lower case.\r\n",
                 "mixed_nested": "Greater Mount Rong stood there.\r\n"}.get(fixture_mode)
        if extra: (project.chapter_dir / "0001.md").write_bytes(extra.encode())
        if fixture_mode == "mixed_noop":
            (project.chapter_dir / "0388.md").write_bytes(b"Mount Yourong stood there.\r\n")
    project = load_novel_project("fixture", repo_root=tmp_path)
    module = local_module(project)
    # The old fixture asserted an audited prefix without any immutable lineage.
    # Supply a genuine local zero-send prior intent/checkpoint instead.
    module.progress["last_audited_chapter"] = 0
    _write_json(project.progress_file, module.progress)
    _write_json(module.periodic_audit_checkpoint_path(1, 375), {"synthetic_zero_send": True})
    locked_finalize(module, 1, 375, {"decisions": [], "issues": []},
        {field: 0 for field in final.STAT_TOTAL_FIELDS},
        {"candidate_count": 0, "api_request_count": 0, "candidate_set_hash": "synthetic-zero-prefix"})
    if fixture_mode == "relative":
        monkeypatch.chdir(project.root)
        for name in ("CHAPTERS_FILE", "GLOSSARY_FILE", "PROGRESS_FILE", "CONTINUITY_FILE", "ISSUES_FILE", "AUDIT_HISTORY_FILE",
                     "OUTPUT_DIR", "PENDING_DIR", "AUDIT_DIR", "RETROACTIVE_DIR", "BACKUP_DIR", "PAID_REQUEST_JOURNAL_DIR"):
            setattr(module, name, getattr(module, name).relative_to(project.root))
    # Restore only acquisition callbacks for the synthetic runtime. No initializer
    # or real credential/client is used; fake responses exercise actual journaling.
    acquisition = load_production_module()
    for name in ("request_periodic_audit_chunk", "repair_periodic_audit_chunk", "correct_periodic_audit_chunk_semantics",
                 "call_api_with_retries", "journaled_chat_request", "process_chapter", "extract_state"):
        function = getattr(acquisition, name)
        setattr(module, name, FunctionType(function.__code__, module.__dict__, function.__name__, function.__defaults__, function.__closure__))
    # Fresh numeric combiner for install_goat_runtime (avoid wrapping twice).
    module.combine_stats = acquisition.combine_stats
    module.RUN_LOGGER = NS(emit=lambda *a, **k: None)
    module.log_event = lambda *a, **k: None
    module.AUTO_OFF_PEAK_ONLY = False
    module.STATE_EXTRACTION_POLICY = acquisition.STATE_EXTRACTION_POLICY
    physical_fixture_sends = []
    prior_candidates = []
    def create(**wire):
        physical_fixture_sends.append(wire)
        prompt = wire["messages"][-1]["content"]
        if "AUDIT TARGETS:\n" in prompt:
            candidates = json.loads(prompt.split("AUDIT TARGETS:\n")[1].split("\nSTRUCTURED FAMILY CONTEXT:")[0])
            prior_candidates[:] = candidates
        else:
            candidates = prior_candidates
        content = json.dumps(dict(decisions=[dict(candidate_id=t["candidate_id"],
            action="correct" if t["source_key"] == "有榮山" else "keep",
            english="Mount Yourong" if t["source_key"] == "有榮山" else t["english"], notes="fixture") for t in candidates], issues=["fixture issue"]), ensure_ascii=False)
        finish = "stop"
        if len(physical_fixture_sends) == 1:
            if fixture_mode == "json_repair": content = "{malformed"
            if fixture_mode == "semantic_correction":
                parsed = json.loads(content); parsed["decisions"][0]["action"] = ""; content = json.dumps(parsed)
            if fixture_mode == "split": content, finish = "{truncated", "length"
        elif fixture_mode == "semantic_correction" and "AUTHORIZED FIELDS BY CANDIDATE" in prompt:
            content = '{"decisions":[{"candidate_id":"c01","action":"keep"}]}'
        counts = [(18017, 10183, 9220), (18420, 11414, 10565), (14277, 1701, 1436)] + [(20, 10, 4)] * 4
        p, c, r = counts[len(physical_fixture_sends) - 1]
        return NS(id=f"synthetic-{len(physical_fixture_sends)}", model=provider_identity("command_code_provider", "v41-flash")["wire_model"],
            choices=[NS(finish_reason=finish, message=NS(content=content))],
            usage=NS(prompt_tokens=p, completion_tokens=c, total_tokens=p+c,
                prompt_tokens_details=NS(cached_tokens=0), completion_tokens_details=NS(reasoning_tokens=r)))
    # Context ownership is installed on the real call_periodic_audit entry point.
    module.run_periodic_audit = module.call_periodic_audit
    module._RUNTIME_INITIALIZED = True  # Disposable manually hydrated state only.
    install_goat_runtime(module, provider_client=NS(chat=NS(completions=NS(create=create))), budget=GoatBudget(6, 10), clock=CLOCK)
    audit_result, stats, chunking = module.run_periodic_audit(376, 400)
    if fixture_mode in {"audit", "relative"}:
        assert len(physical_fixture_sends) == 3
        assert stats["cost_usd"] == pytest.approx(0.0215859)
    prepared = final.prepare_finalization(module, 376, 400, audit_result, stats, chunking)
    # Reproduce the live failure prefix: durable PR155 transaction, old state.
    if not fixture_mode.startswith("mixed"):
        from production_run_lock import production_run_lock
        with production_run_lock(project.root):
            module.apply_retroactive_replacements("0376_0400", 400, prepared["record"]["changes"]["retroactive_replacements"], staged_glossary=prepared["post"]["glossary"])
    return project, module, audit_result, stats, chunking


def plan(project):
    return build_goat_periodic_audit_finalize_plan("fixture", repo_root=project.root.parent.parent, start_chapter=376, end_chapter=400)


def execute(project, approved=None):
    approved = approved or plan(project)
    return execute_goat_periodic_audit_finalize("fixture", repo_root=project.root.parent.parent, start_chapter=376, end_chapter=400,
        expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"], authorize_local_finalization=True)


def test_live_shaped_local_recovery_and_already_finalized(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    before = snapshot(project)
    approved = plan(project)
    assert snapshot(project) == before
    assert approved["status"] == "READY" and approved["provider_calls_required"] == 0
    assert all(approved[k] is False for k in ("translation_generation_allowed", "chapter_generation_allowed", "audit_provider_generation_allowed"))
    assert stats["provider"] == "command_code_provider" and stats["provider_invoice_claimed"] is False
    assert len(stats["accounting_components"]) == 3
    # Demonstrate original failure without weakening its guard.
    generic = {k: stats[k] for k in (*final.STAT_TOTAL_FIELDS, "api_request_count")}
    with pytest.raises(ProductionProviderError, match="cross-provider"):
        core.add_accounting_totals_once(copy.deepcopy(module.progress), "reproduce-original", generic)
    before_prose = (project.chapter_dir / "0388.md").read_bytes()
    before_tx = {k: v for k, v in before.items() if "transactions/" in k.replace("\\", "/")}
    assert execute(project, approved)["status"] == "FINALIZED"
    assert (project.chapter_dir / "0388.md").read_bytes() == before_prose
    progress = json.loads(project.progress_file.read_bytes())
    assert (progress["last_completed"], progress["next_chapter"], progress["last_audited_chapter"]) == (400, 401, 400)
    assert progress["rate_correct_cost_usd"] == stats["cost_usd"]
    assert len(progress["accounted_api_operation_ids"]) == 1
    glossary = json.loads(project.glossary_file.read_bytes())
    assert glossary["entries"]["有榮山"]["english"] == "Mount Yourong"
    assert glossary["entries"]["有熙氏"] == module.glossary["entries"]["有熙氏"]
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    for key, payload in before_tx.items():
        assert (project.root / key).read_bytes() == payload
    after = snapshot(project)
    assert plan(project)["status"] == "ALREADY_FINALIZED"
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert snapshot(project) == after


@pytest.mark.parametrize("fault", ["mixed", "metadata", "numeric", "usage", "duplicate"])
def test_authoritative_accounting_rejects_component_or_numeric_drift(incident, fault):
    project, module, result, stats, chunking = incident
    cp = json.loads(module.periodic_audit_checkpoint_path(376, 400).read_bytes())
    generic = {k: stats[k] for k in (*final.STAT_TOTAL_FIELDS, "api_request_count")}
    if fault == "mixed": cp["attempt_records"][0]["stats"]["provider"] = "direct_deepseek"
    if fault == "metadata": cp["attempt_records"][0]["stats"].pop("pricing_unit")
    if fault == "numeric": generic["prompt_tokens"] += 1
    if fault == "usage": cp["attempt_records"][0]["stats"].pop("completion_tokens")
    if fault == "duplicate": cp["attempt_records"][1]["operation_id"] = cp["attempt_records"][0]["operation_id"]
    with pytest.raises((RuntimeError, KeyError, ProductionProviderError)):
        final.authoritative_audit_stats(dict(checkpoint=cp, stats=generic), module.combine_stats, provider="command_code_provider")


def test_accounting_failure_before_any_prose_transaction(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    # Restore a separate disposable original fixture; never touch live evidence.
    isolated = project.root / "no_transaction"
    isolated.mkdir()
    monkeypatch.setattr(module, "RETROACTIVE_DIR", isolated)
    bad = copy.deepcopy(stats)
    bad["provider"] = "direct_deepseek"
    before = snapshot(project)
    monkeypatch.setattr(final.tx, "execute_retroactive_transaction", lambda **k: pytest.fail("transaction before accounting validation"))
    with pytest.raises((RuntimeError, ProductionProviderError)):
        locked_finalize(module, 376, 400, result, bad, chunking)
    assert snapshot(project) == before


@pytest.mark.parametrize("boundary", ["glossary", "issues", "history", "audit", "backup", "progress"])
def test_every_local_publication_prefix_reconciles_without_duplicates(incident, monkeypatch, boundary):
    project, module, result, stats, chunking = incident
    original = core.atomic_write_json
    fired = []
    def crash(path, value):
        original(path, value)
        path = Path(path)
        match = ((boundary == "glossary" and path == project.glossary_file)
            or (boundary == "issues" and path == project.issues_file)
            or (boundary == "history" and path == project.audit_history_file)
            or (boundary == "audit" and path.name == "audit_0376_0400.json")
            or (boundary == "backup" and path.parent.name == "checkpoint_0400")
            or (boundary == "progress" and path == project.progress_file))
        if match and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic crash")
    monkeypatch.setattr(module, "save_json", crash)
    original_bytes = core.atomic_write_bytes
    def crash_backup(path, value):
        original_bytes(path, value)
        if boundary == "backup" and path.parent.name == "checkpoint_0400" and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic backup crash")
    monkeypatch.setattr(final, "atomic_write_bytes", crash_backup)
    with pytest.raises(KeyboardInterrupt):
        locked_finalize(module, 376, 400, result, stats, chunking)
    monkeypatch.setattr(module, "save_json", original)
    monkeypatch.setattr(final, "atomic_write_bytes", original_bytes)
    outcome = locked_finalize(module, 376, 400, result, stats, chunking)
    assert outcome["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("fault", ["missing_tx", "chapter", "backup", "result", "state", "extra_prose", "glossary", "checkpoint",
    "candidate", "journal", "started", "uncertain", "corrupt", "window", "progress", "continuity", "policy"])
def test_recovery_drift_fails_closed(incident, fault):
    project, module, result, stats, chunking = incident
    approved = plan(project)
    directory = project.retroactive_dir / "transactions"
    cp_path = module.periodic_audit_checkpoint_path(376, 400)
    journal_path = next((project.data_dir / "provider_request_journal").glob("*.json"))
    def change(path, key, value):
        data = json.loads(path.read_bytes()); data[key] = value; _write_json(path, data)
    if fault == "missing_tx":
        for p in directory.glob("*.json"): p.unlink()
    if fault == "chapter": (project.chapter_dir / "0388.md").write_bytes(b"wrong")
    if fault == "backup": next(project.backup_dir.rglob("0388.md")).write_bytes(b"wrong")
    if fault == "result": change(next(directory.glob("*.result.json")), "replacement_count", 99)
    if fault == "state": change(next(directory.glob("*.state.json")), "state", "BLOCKED")
    if fault == "extra_prose": (project.chapter_dir / "0387.md").write_bytes(b"extra")
    if fault == "glossary":
        data = json.loads(project.glossary_file.read_bytes()); data["entries"]["有榮山"]["english"] = "Third Value"; _write_json(project.glossary_file, data)
    if fault == "checkpoint": change(cp_path, "status", "in_progress")
    if fault == "candidate": change(cp_path, "candidate_set_hash", "a"*64)
    if fault in {"journal", "started", "uncertain", "policy"}:
        journal = json.loads(journal_path.read_bytes())
        if fault == "journal": journal["response"]["stats"]["prompt_tokens"] += 1
        if fault in {"started", "uncertain"}: journal.update(status=fault, response={}, response_sha256="")
        if fault == "policy": journal["provider_policy"]["revision"] = "wrong"
        _write_json(journal_path, _with_record_hash(journal))
    if fault == "corrupt": journal_path.write_bytes(b"{")
    if fault == "window": change(cp_path, "audit_window", dict(start_chapter=377, end_chapter=400))
    if fault == "progress": change(project.progress_file, "next_chapter", 402)
    if fault == "continuity": change(project.continuity_file, "latest_chapter", 399)
    before = snapshot(project)
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        execute(project, approved)
    # The existing empty rendezvous lock is the only executor side effect.
    after = snapshot(project)
    assert {k: v for k, v in after.items() if k != ".production.lock"} == before


def test_zero_candidate_goat_audit_no_fabricated_paid_accounting(incident):
    project, module, result, stats, chunking = incident
    locked_finalize(module, 376, 400, result, stats, chunking)
    module.progress.update(last_completed=425, next_chapter=426, completed=list(range(1, 426)))
    for n in range(401, 426): (project.chapter_dir / f"{n:04d}.md").write_bytes(b"English.")
    _write_json(project.progress_file, module.progress)
    _write_json(project.continuity_file, dict(latest_chapter=425, summary="fixture"))
    zero_result, zero_stats, zero_chunking = module.call_periodic_audit(401, 425)
    before = copy.deepcopy(module.progress)
    locked_finalize(module, 401, 425, zero_result, zero_stats, zero_chunking)
    assert module.progress == {**before, "last_audited_chapter": 425}
    assert "provider" not in zero_stats and "accounting_components" not in zero_stats
    assert locked_finalize(module, 401, 425, zero_result, zero_stats, zero_chunking)["status"] == "ALREADY_FINALIZED"


def test_direct_generic_aggregate_unchanged():
    run = dict(stats=dict(prompt_tokens=10, cost_usd=0.01), checkpoint=dict(attempt_records=[]))
    assert final.authoritative_audit_stats(run, lambda *a: pytest.fail("direct combiner altered"), provider="direct_deepseek") is run["stats"]


def test_local_executor_requires_explicit_flag_and_installs_tripwire(incident):
    project, *_ = incident
    approved = plan(project)
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="authorize-local-finalization"):
        execute_goat_periodic_audit_finalize("fixture", repo_root=project.repository_root, start_chapter=376, end_chapter=400,
            expected_state_fingerprint=approved["state_fingerprint"], approval_token=approved["approval_token"])
    assert snapshot(project) == before
    from goat_periodic_audit_finalize_execute import no_transport
    import socket
    import command_code_provider_credentials as credentials
    import dotenv
    with no_transport():
        for call in (lambda: socket.create_connection(("example.invalid", 443)), credentials.load_platform_dpapi, dotenv.load_dotenv):
            with pytest.raises(RuntimeError, match="tripwire"): call()


@pytest.mark.parametrize("stage", ["after_plan", "after_backup", "after_chapter", "after_applied", "after_result"])
def test_shared_finalizer_reconciles_interrupted_new_pr155_transaction(incident, monkeypatch, stage):
    project, module, result, stats, chunking = incident
    original_bytes = next(project.backup_dir.rglob("0388.md")).read_bytes()
    (project.chapter_dir / "0388.md").write_bytes(original_bytes)
    from retroactive_span_planning import canonical_hash
    slot = canonical_hash(dict(novel_id=project.config.id, audit_label="0376_0400", through_chapter=400))
    for path in (project.retroactive_dir / "transactions").glob(f"{slot}.*.json"): path.unlink()
    # This starts an independent synthetic new transaction from exact BEFORE.
    original_json, original_write = final.tx.atomic_write_json, final.tx.atomic_write_bytes
    def json_crash(path, data):
        original_json(path, data)
        if ((stage == "after_plan" and path.name.endswith(".plan.json")) or
            (stage == "after_applied" and data.get("state") == "APPLIED") or
            (stage == "after_result" and path.name.endswith(".result.json"))):
            raise KeyboardInterrupt("synthetic transaction crash")
    def byte_crash(path, data):
        original_write(path, data)
        if ((stage == "after_backup" and path.parent != project.chapter_dir) or
            (stage == "after_chapter" and path.parent == project.chapter_dir)):
            raise KeyboardInterrupt("synthetic transaction crash")
    # Remove the old test backup so AFTER_BACKUP exercises a real new backup.
    for path in project.backup_dir.rglob("0388.md"): path.unlink()
    monkeypatch.setattr(final.tx, "atomic_write_json", json_crash)
    monkeypatch.setattr(final.tx, "atomic_write_bytes", byte_crash)
    with pytest.raises(KeyboardInterrupt):
        locked_finalize(module, 376, 400, result, stats, chunking)
    monkeypatch.setattr(final.tx, "atomic_write_json", original_json)
    monkeypatch.setattr(final.tx, "atomic_write_bytes", original_write)
    assert plan(project)["status"] == "READY"
    execute(project)
    assert plan(project)["status"] == "ALREADY_FINALIZED"
    assert (project.chapter_dir / "0388.md").read_bytes().count(b"Mount Yourong") == 1
    assert json.loads(project.progress_file.read_bytes())["rate_correct_cost_usd"] == stats["cost_usd"]


@pytest.mark.parametrize("incident", ["json_repair", "semantic_correction", "split"], indirect=True)
def test_recovery_reuses_generic_repair_and_split_semantics(incident):
    project, module, result, stats, chunking = incident
    approved = plan(project)
    assert approved["provider_calls_required"] == 0
    assert len(approved["state_binding"]["proof"]["journals"]) == chunking["api_request_count"]
    assert chunking["api_request_count"] in {4, 5}
    assert execute(project, approved)["status"] == "FINALIZED"
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("incident", ["relative"], indirect=True)
def test_relative_production_evidence_paths_remain_exact_and_replayable(incident):
    project, module, result, stats, chunking = incident
    before = snapshot(project)
    approved = plan(project)
    assert snapshot(project) == before
    assert execute(project, approved)["provider_calls"] == 0
    assert plan(project)["status"] == "ALREADY_FINALIZED"


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
def test_fresh_audit_mixed_old_and_preferred_baseline_finalizes_once(incident):
    project, module, result, stats, chunking = incident
    preferred = (project.chapter_dir / "0002.md").read_bytes()
    assert execute(project)["status"] == "FINALIZED"
    assert (project.chapter_dir / "0002.md").read_bytes() == preferred
    assert b"Mount Yourong" in (project.chapter_dir / "0388.md").read_bytes()
    intent = final.load_intent(local_module(project), 376, 400)
    item = intent["transaction_plan"]["chapters"][1]
    assert item["before_sha256"] == item["after_sha256"] == final.tx.digest(preferred)
    before = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert before == snapshot(project)
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["plan", "state", "result", "backup", "reconcile"])
@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_missing_transaction_evidence_after_edit_cannot_be_reclassified_as_fresh(incident, damage, corrupt, published, monkeypatch):
    project, module, result, stats, chunking = incident
    if published:
        execute(project)
    else:
        original = final.tx.atomic_write_json
        def stop(path, data):
            original(path, data)
            if Path(path).name.endswith(".state.json") and data.get("state") == "FINALIZED":
                raise KeyboardInterrupt("before outer publication")
        with monkeypatch.context() as mp:
            mp.setattr(final.tx, "atomic_write_json", stop)
            with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    if damage == "backup":
        path = next(project.backup_dir.rglob("0388.md"))
    else:
        path = next((project.retroactive_dir / "transactions").glob(f"*.{damage}.*json" if damage == "reconcile" else f"*.{damage}.json"))
    if corrupt: path.write_bytes(b"{corrupt")
    else: path.unlink()
    before = snapshot(project)
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        plan(project)
    from production_uncertain_request_disposition import effective_paid_request_inventory
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        effective_paid_request_inventory(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident,changed", [("mixed_multi", 2), ("mixed_case", 1),
    ("mixed_nested", 1), ("mixed_competing", 0), ("mixed_contextual", 0), ("mixed_noop", 0)], indirect=["incident"])
def test_fresh_baseline_ownership_markdown_case_and_noops(incident, changed):
    project, *_ = incident
    before = {p.name: p.read_bytes() for p in project.chapter_dir.glob("*.md")}
    execute(project)
    after = {p.name: p.read_bytes() for p in project.chapter_dir.glob("*.md")}
    assert sum(before[n] != after[n] for n in before) == changed
    assert before["0002.md"] == after["0002.md"]
    if changed == 2:
        assert after["0001.md"] == before["0001.md"].replace(b"Mount Rong", b"Mount Yourong")
    before_replay = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert snapshot(project) == before_replay


@pytest.mark.parametrize("incident", ["mixed_multi"], indirect=True)
@pytest.mark.parametrize("boundary", ["intent", "plan", "backup", "chapter", "APPLIED", "result", "FINALIZED", "audit", "progress"])
def test_fresh_baseline_crash_prefixes_preserve_manifest_and_account_once(incident, monkeypatch, boundary):
    project, module, result, stats, chunking = incident
    original_json, original_bytes = final.atomic_write_json, final.atomic_write_bytes
    preferred = (project.chapter_dir / "0002.md").read_bytes()
    fired = []
    def crash_json(path, value):
        original_json(path, value)
        path = Path(path)
        match = ((boundary == "intent" and path.name.endswith(".finalization.json")) or
            (boundary == "plan" and path.name.endswith(".plan.json")) or
            (boundary in {"APPLIED", "FINALIZED"} and path.name.endswith(".state.json") and value.get("state") == boundary) or
            (boundary == "result" and path.name.endswith(".result.json")) or
            (boundary == "audit" and path.name == "audit_0376_0400.json") or
            (boundary == "progress" and path == project.progress_file))
        if match and not fired:
            fired.append(True); raise KeyboardInterrupt("durable fresh audit boundary")
    def crash_bytes(path, value):
        original_bytes(path, value)
        path = Path(path)
        if not fired and ((boundary == "backup" and path.parent.name == "chapters_before") or
                          (boundary == "chapter" and path.parent == project.chapter_dir)):
            fired.append(True); raise KeyboardInterrupt("durable fresh audit bytes")
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_json", crash_json)
        mp.setattr(final.tx, "atomic_write_bytes", crash_bytes)
        mp.setattr(module, "save_json", crash_json)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    assert fired
    from production_uncertain_request_disposition import effective_paid_request_inventory
    if boundary != "progress":
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project)
    approved = plan(project)
    assert execute(project, approved)["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
    assert (project.chapter_dir / "0002.md").read_bytes() == preferred
    assert len(json.loads(project.progress_file.read_bytes())["accounted_api_operation_ids"]) == 1
    assert len(json.loads(project.audit_history_file.read_bytes())["audits"]) == 2
    before = snapshot(project)
    assert execute(project)["status"] == "ALREADY_FINALIZED"
    assert before == snapshot(project)


@pytest.mark.parametrize("incident", ["mixed_multi"], indirect=True)
def test_out_of_order_chapter_after_prefix_blocks_local_reconciliation(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    original = final.tx.atomic_write_bytes
    def stop(path, value):
        original(path, value)
        if Path(path).parent == project.chapter_dir:
            raise KeyboardInterrupt("after first historical edit")
    with monkeypatch.context() as mp:
        mp.setattr(final.tx, "atomic_write_bytes", stop)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    intent = final.load_intent(module,376,400)
    items = [i for i in intent["transaction_plan"]["chapters"] if i["edits"]]
    assert [i["chapter"] for i in items] == [1,388]
    backup = project.backup_dir / f"retroactive_{intent['transaction_plan']['transaction_id']}" / "chapters_before"
    (project.chapter_dir / "0001.md").write_bytes((backup / "0001.md").read_bytes())
    raw = (project.chapter_dir / "0388.md").read_bytes()
    (backup / "0388.md").write_bytes(raw)
    (project.chapter_dir / "0388.md").write_bytes(final.tx.transform_bytes(raw,items[1]["edits"]))
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="prefix"):
        plan(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("missing_plan", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_legacy_intent_preserves_completion_but_cannot_regenerate_plan(incident, missing_plan, published, monkeypatch):
    project, module, result, stats, chunking = incident
    if published:
        execute(project)
    else:
        original = module.save_json
        def stop(path, data):
            original(path,data)
            if Path(path) == project.glossary_file: raise KeyboardInterrupt("legacy outer tail")
        with monkeypatch.context() as mp:
            mp.setattr(module,"save_json",stop)
            with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    path = next(project.audit_dir.glob("*.finalization.json"))
    value = json.loads(path.read_bytes())
    value.pop("historical_proof")
    # PR158 captured current AFTER hashes in its original chapters field.
    value["chapters"] = final.chapter_manifest(local_module(project))
    value["intent_sha256"] = final.canonical_sha256({k:v for k,v in value.items() if k != "intent_sha256"})
    _write_json(path,value)
    if missing_plan: next((project.retroactive_dir / "transactions").glob("*.plan.json")).unlink()
    before = snapshot(project)
    if missing_plan:
        with pytest.raises(RuntimeError): plan(project)
    else:
        assert plan(project)["status"] == ("ALREADY_FINALIZED" if published else "READY")
    from production_uncertain_request_disposition import effective_paid_request_inventory
    if missing_plan or not published:
        with pytest.raises(RuntimeError): effective_paid_request_inventory(project)
    else:
        assert not effective_paid_request_inventory(project)["uncertain"]
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
def test_intent_before_plan_with_orphan_checkpoint_is_not_untouched_prefix(incident, monkeypatch):
    project, module, result, stats, chunking = incident
    original = final.atomic_write_json
    def stop(path, data):
        original(path,data)
        if Path(path).name.endswith(".finalization.json"):
            raise KeyboardInterrupt("intent before plan")
    with monkeypatch.context() as mp:
        mp.setattr(final,"atomic_write_json",stop)
        with pytest.raises(KeyboardInterrupt): locked_finalize(module,376,400,result,stats,chunking)
    intent = final.load_intent(module,376,400)
    checkpoint = project.backup_dir / "checkpoint_0400"
    checkpoint.mkdir(parents=True)
    _write_json(checkpoint / "progress.json",intent["post"]["progress"])
    before = snapshot(project)
    with pytest.raises(RuntimeError, match="checkpoint"):
        plan(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed", "mixed_competing", "mixed_contextual"], indirect=True)
def test_aut2_final_whole_history_and_publication_stale_are_independent_of_tail(incident):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    execute(project)
    current = local_module(project)
    # Fixture starts at 376; earlier legacy windows have no intents. Full-history
    # proof must block rather than silently treating that tail as the entire book.
    before = snapshot(project)
    with pytest.raises(Exception, match="MISSING_OR_COMPETING_AUDIT_HISTORY"):
        history.final_consistency_report(current)
    assert snapshot(project) == before
    freshness = history.publication_freshness(project)
    assert freshness["status"] == "STALE"
    assert 388 in freshness["impacted_chapters"] or freshness["impacted_chapters"] == []
    assert freshness["rebuilt_epubs"] == 0
    with pytest.raises(Exception, match="PUBLICATION_STALE"):
        history.require_publication_freshness(project)
    intent = final.load_intent(current, 376, 400)
    report = intent["record"]["historical_reconciliation"]
    assert report["scope"] == "whole_committed_history"
    assert report["examined_chapters"] == 400
    assert "有榮山" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("stage", ["invalidation", "first_chapter", "glossary", "progress"])
def test_aut2_crash_invalidation_precedes_edits_and_progress_last(incident, monkeypatch, stage):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    original_json, original_prose, original_save = history.atomic_write_json, final.tx.atomic_write_bytes, module.save_json
    fired = []
    def crash(path, value):
        original_json(path, value)
        if stage == "invalidation" and "publication_freshness" in str(path) and not fired:
            fired.append(True)
            raise KeyboardInterrupt()
    def prose(path, raw):
        original_prose(path, raw)
        if stage == "first_chapter" and Path(path).parent == module.OUTPUT_DIR and not fired:
            fired.append(True)
            assert list((project.root / "output/publication_freshness").glob("*.json"))
            raise KeyboardInterrupt()
    def save(path, value):
        original_save(path, value)
        if ((stage == "glossary" and path == module.GLOSSARY_FILE)
                or (stage == "progress" and path == module.PROGRESS_FILE)) and not fired:
            fired.append(True)
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(history, "atomic_write_json", crash)
        mp.setattr(final.tx, "atomic_write_bytes", prose)
        mp.setattr(module, "save_json", save)
        with pytest.raises(KeyboardInterrupt):
            locked_finalize(module, 376, 400, result, stats, chunking)
    journals = {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    reloaded = local_module(project)
    locked_finalize(reloaded, 376, 400, result, stats, chunking)
    assert journals == {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    assert history.publication_freshness(project)["status"] == "STALE"
    before = snapshot(project)
    locked_finalize(local_module(project), 376, 400, result, stats, chunking)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["malformed", "missing", "tampered", "foreign"])
def test_aut2_invalid_freshness_never_certifies_editorial_copy(incident, damage):
    import historical_reconciliation as history
    import build_publication_epub as publication
    project, module, result, stats, chunking = incident
    execute(project)
    path = next((project.root / "output/publication_freshness").glob("*.json"))
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_bytes(b"{bad")
    else:
        value = json.loads(path.read_bytes())
        value["status" if damage == "tampered" else "novel_id"] = "CURRENT" if damage == "tampered" else "foreign"
        path.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(project)
    with pytest.raises(Exception, match="AUT2"):
        publication.build_publication_epub(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("incident", ["mixed"], indirect=True)
@pytest.mark.parametrize("damage", ["checkpoint", "paid_journal"])
def test_aut2_review_paid_proof_failure_precedes_first_receipt(incident, monkeypatch, damage):
    import historical_reconciliation as history
    project, module, result, stats, chunking = incident
    write = final.atomic_write_json
    def stop(path, value):
        write(path, value)
        if Path(path).name.endswith(".finalization.json"):
            raise KeyboardInterrupt()
    with monkeypatch.context() as mp:
        mp.setattr(final, "atomic_write_json", stop)
        with pytest.raises(KeyboardInterrupt):
            locked_finalize(module, 376, 400, result, stats, chunking)
    if damage == "checkpoint":
        module.periodic_audit_checkpoint_path(376, 400).write_bytes(b"stale checkpoint")
    else:
        path = next(module.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))
        value = json.loads(path.read_bytes())
        value["response"]["stats"]["prompt_tokens"] += 1
        _write_json(path, _with_record_hash(value))
    monkeypatch.setattr(module.client.chat.completions, "create", lambda **k: pytest.fail("provider send"))
    before = snapshot(project)
    directories = {p.relative_to(project.root).as_posix() for p in project.root.rglob("*") if p.is_dir()}
    with pytest.raises((RuntimeError, ValueError)):
        locked_finalize(module, 376, 400, result, stats, chunking)
    assert snapshot(project) == before
    assert {p.relative_to(project.root).as_posix() for p in project.root.rglob("*") if p.is_dir()} == directories
    intent = final.load_intent(module, 376, 400)
    assert not history.invalidation_path(module, intent).exists()
