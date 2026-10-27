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
    from glossary_audit import run_chunked_audit
    from production_run_lock import production_run_lock
    with production_run_lock(project.root):
        assert not module.audit_targets(1, 375)
        prefix = run_chunked_audit(candidates=[], all_glossary_keys=module.glossary["entries"],
            start_chapter=1, end_chapter=375,
            checkpoint_path=module.periodic_audit_checkpoint_path(1, 375),
            request_chunk=lambda *a: pytest.fail("zero-candidate prefix must never send"))
        final.finalize_periodic_audit(module, 1, 375, prefix["audit_result"], prefix["stats"],
            {"candidate_count": 0, "api_request_count": 0,
             "candidate_set_hash": prefix["candidate_set_hash"]})
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
