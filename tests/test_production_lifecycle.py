import builtins

import pytest

from production_failure_classification import NonProviderProductionError
from production_lifecycle import (
    run_due_audits,
    run_final_audit_if_needed,
    run_unattended_lifecycle,
)


class SyntheticProviderError(RuntimeError):
    pass


class SyntheticLocalError(NonProviderProductionError):
    pass


def test_due_audits_runs_only_complete_intervals():
    progress = {
        "last_completed": 5,
        "last_audited_chapter": 1,
    }
    calls = []

    def audit(start, end):
        calls.append((start, end))
        progress["last_audited_chapter"] = end

    run_due_audits(
        progress=progress,
        audit_interval=2,
        run_periodic_audit=audit,
    )

    assert calls == [(2, 3), (4, 5)]
    assert progress["last_audited_chapter"] == 5


def test_final_audit_runs_only_for_completed_tail():
    progress = {
        "last_completed": 5,
        "last_audited_chapter": 3,
    }
    calls = []

    run_final_audit_if_needed(
        progress=progress,
        final_chapter=5,
        run_periodic_audit=lambda start, end: (calls.append((start, end)),
            progress.update(last_audited_chapter=end)),
    )

    assert calls == [(4, 5)]

    calls.clear()
    progress["last_completed"] = 4

    run_final_audit_if_needed(
        progress=progress,
        final_chapter=5,
        run_periodic_audit=lambda start, end: calls.append(
            (start, end)
        ),
    )

    assert calls == []


def _fixture(*, next_chapter=2, final_chapter=3):
    events = []
    output = []
    errors = []

    progress = {
        "completed": list(range(1, next_chapter)),
        "last_completed": next_chapter - 1,
        "next_chapter": next_chapter,
        "last_audited_chapter": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "rate_correct_cost_start_chapter": 1,
        "rate_correct_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
    }
    glossary = {"entries": {}}
    prices = {
        "off_peak": {
            "cache_hit": 0.0,
            "cache_miss": 0.0,
            "output": 0.0,
        },
        "peak": {
            "cache_hit": 0.0,
            "cache_miss": 0.0,
            "output": 0.0,
        },
    }

    def process(chapter):
        events.append(f"process:{chapter}")
        progress["completed"].append(chapter)
        progress["last_completed"] = chapter
        progress["next_chapter"] = chapter + 1
        return True

    kwargs = {
        "progress": progress,
        "glossary": glossary,
        "final_chapter": final_chapter,
        "auto_off_peak_only": True,
        "manual_pause_file": "pause.flag",
        "prices": prices,
        "run_log_path": None,
        "reconcile_stale_completed_pending": (
            lambda: events.append("reconcile")
        ),
        "perform_startup_self_check": (
            lambda: events.append("self_check")
        ),
        "reconcile_partial_commit_before_audits": (
            lambda: False
        ),
        "run_due_audits_callback": (
            lambda: events.append("due_audits")
        ),
        "run_final_audit_if_needed_callback": (
            lambda: events.append("final_audit")
        ),
        "process_chapter": process,
        "log_event": lambda event, **fields: events.append(
            ("log", event, fields)
        ),
        "log_error": lambda error, **fields: errors.append(
            (error, fields)
        ),
        "describe_api_error": lambda exc: f"provider-described:{exc}",
        "handled_error_types": (SyntheticProviderError,),
        "describe_local_error": lambda exc: f"local-described:{exc}",
        "local_error_types": (NonProviderProductionError,),
        "print_fn": output.append,
    }
    return progress, events, output, errors, kwargs


def _sequence_events(events):
    return [
        item
        for item in events
        if isinstance(item, str)
    ]


def test_unattended_lifecycle_preserves_full_range_sequence():
    progress, events, _output, errors, kwargs = _fixture()

    assert run_unattended_lifecycle(**kwargs) is None

    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
        "process:2",
        "due_audits",
        "process:3",
        "due_audits",
        "final_audit",
    ]
    assert progress["last_completed"] == 3
    assert progress["next_chapter"] == 4
    assert errors == []
    assert events[-1][0:2] == ("log", "shutdown")
    assert events[-1][2]["reason"] == "translation_complete"


def test_startup_partial_reconciliation_precedes_due_audits_and_chapter_loop():
    progress, events, _output, errors, kwargs = _fixture()

    def reconcile_partial():
        events.append("partial_reconcile")
        progress["completed"].append(2)
        progress["last_completed"] = 2
        progress["next_chapter"] = 3
        return True

    kwargs["reconcile_partial_commit_before_audits"] = reconcile_partial

    assert run_unattended_lifecycle(**kwargs) is None

    sequence = _sequence_events(events)
    assert sequence[:4] == [
        "reconcile",
        "self_check",
        "partial_reconcile",
        "due_audits",
    ]
    assert "process:2" not in sequence
    assert "process:3" in sequence
    assert progress["last_completed"] == 3
    assert progress["next_chapter"] == 4
    assert errors == []


def test_unattended_lifecycle_bounds_exact_target():
    progress, events, _output, _errors, kwargs = _fixture()

    assert (
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=2,
        )
        is None
    )

    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
        "process:2",
        "due_audits",
        "final_audit",
    ]
    assert progress["last_completed"] == 2
    assert progress["next_chapter"] == 3
    assert "process:3" not in events


def test_target_start_mismatch_fails_closed_before_processing():
    progress, events, _output, errors, kwargs = _fixture()

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=3,
        )

    assert exc.value.code == 1
    assert _sequence_events(events) == [
        "reconcile",
        "self_check",
        "due_audits",
    ]
    assert progress["next_chapter"] == 2
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_local"


def test_invalid_target_fails_closed_before_processing():
    _progress, events, _output, errors, kwargs = _fixture()

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(
            **kwargs,
            target_chapter=True,
        )

    assert exc.value.code == 1
    assert not any(
        isinstance(item, str) and item.startswith("process:")
        for item in events
    )
    assert len(errors) == 1


def test_false_process_result_stops_at_exact_chapter():
    progress, events, _output, errors, kwargs = _fixture()

    def fail_process(chapter):
        events.append(f"process:{chapter}")
        return False

    kwargs["process_chapter"] = fail_process

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert progress["last_completed"] == 1
    assert "process:3" not in events
    assert errors == []
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "chapter_failed"


def test_keyboard_interrupt_is_contained_as_clean_operator_stop():
    _progress, events, _output, errors, kwargs = _fixture()

    def interrupt():
        raise KeyboardInterrupt

    kwargs["perform_startup_self_check"] = interrupt

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 0
    assert errors == []
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "keyboard_interrupt"


def test_provider_error_uses_provider_description_and_safe_stop():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise SyntheticProviderError("synthetic provider failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_provider"
    assert "provider-described:synthetic provider failure" in output
    assert not any("local-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "provider_error"


def test_local_controlled_error_uses_local_description_and_safe_stop():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise SyntheticLocalError("synthetic local failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(SystemExit) as exc:
        run_unattended_lifecycle(**kwargs)

    assert exc.value.code == 1
    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_local"
    assert "local-described:synthetic local failure" in output
    assert not any("provider-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "local_error"


def test_raw_runtime_error_is_not_classified_as_provider_failure():
    _progress, events, output, errors, kwargs = _fixture()

    def fail():
        raise RuntimeError("unclassified runtime failure")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(RuntimeError, match="unclassified runtime failure"):
        run_unattended_lifecycle(**kwargs)

    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_unexpected"
    assert not any("provider-described:" in item for item in output)
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "unexpected_error"


def test_unexpected_error_is_logged_and_reraised():
    _progress, events, _output, errors, kwargs = _fixture()

    def fail():
        raise ValueError("unexpected")

    kwargs["perform_startup_self_check"] = fail

    with pytest.raises(ValueError, match="unexpected"):
        run_unattended_lifecycle(**kwargs)

    assert len(errors) == 1
    assert errors[0][1]["stage"] == "production_unexpected"
    shutdowns = [
        item
        for item in events
        if isinstance(item, tuple)
        and item[0:2] == ("log", "shutdown")
    ]
    assert shutdowns[-1][2]["reason"] == "unexpected_error"


def test_all_lifecycle_output_uses_injected_print(monkeypatch):
    _progress, _events, output, _errors, kwargs = _fixture()

    def forbidden_print(*_args, **_kwargs):
        raise AssertionError("shared lifecycle used builtins.print directly")

    monkeypatch.setattr(builtins, "print", forbidden_print)

    run_unattended_lifecycle(**kwargs)

    assert output

# AUT-1 tests extend this already-classified lifecycle module. All state is
# disposable and every transport below is a Python fake under the public guard.
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

from production_autonomous_recovery import RecoverySupervisor, decide_recovery, classify_exception
from production_failure_classification import ProductionIntegrityError, ProductionControlError
from production_paid_request_journal import PaidRequestUncertainError, PaidRequestJournalError, _with_record_hash
from production_saved_checkpoint_identity import SavedCheckpointIdentityError


def test_aut1_no_duplicate_dispatch_or_invented_route():
    calls, events = [], []
    supervisor = RecoverySupervisor(identity={"novel": "fixture", "chapter": 1},
                                    emit=lambda *a, **k: events.append(k))
    supervisor.run("state", lambda: calls.append(1) or {"status": "extracted"})
    with pytest.raises(ProductionControlError):
        supervisor.run("state", lambda: calls.append(2))
    with pytest.raises(ProductionControlError):
        supervisor.run("goat_lifetime_batch", lambda: calls.append(3))
    assert calls == [1]
    assert events[-1]["reason_code"] == "DUPLICATE_OR_UNSUPPORTED_DISPATCH"


@pytest.mark.parametrize("exc,reason", [
    (PaidRequestUncertainError("do not echo source"), "AMBIGUOUS_PAID_REQUEST"),
    (PaidRequestJournalError("request/model/source mismatch"), "IDENTITY_OR_PROOF_MISMATCH"),
    (SavedCheckpointIdentityError("stale policy"), "IDENTITY_OR_PROOF_MISMATCH"),
    (RuntimeError("try again length"), "UNCLASSIFIED_FAILURE"),
])
def test_aut1_typed_failure_never_retries_or_echoes_prose(exc, reason):
    calls, events = [], []
    def fail():
        calls.append(1)
        raise exc
    supervisor = RecoverySupervisor(identity={}, emit=lambda *a, **k: events.append(k))
    with pytest.raises(type(exc)):
        supervisor.run("translation", fail)
    assert calls == [1]
    assert events[-1]["reason_code"] == reason
    assert str(exc) not in json.dumps(events)


@pytest.mark.parametrize("stage,result,reason", [
    ("strict_micro", {"status": "attempt_limit_reached"}, "REPAIR_BUDGET_EXHAUSTED"),
    ("residual_cjk", {"status": "ineligible"}, "REPAIR_NOT_PROVEN"),
    ("commit_qa", {"commit_allowed": False}, "MANDATORY_QA_UNRESOLVED"),
    ("candidates", {"status": "identity_failed"}, "GLOSSARY_IDENTITY_UNRESOLVED"),
])
def test_aut1_known_blockers_have_machine_readable_reasons(stage, result, reason):
    decision = decide_recovery(stage, result)
    assert decision.disposition == "BLOCKED_UNVERIFIED"
    assert decision.reason == reason


@pytest.mark.parametrize("action", ["missing", "divergent", "spent"])
def test_aut1_local_audit_recovery_is_once_and_never_acquires(action):
    calls = []
    supervisor = RecoverySupervisor(identity={}, emit=lambda *a, **k: None)
    def publish():
        calls.append("publish")
        raise OSError("synthetic interrupted write")
    def local():
        calls.append("prove_intent")
        if action != "spent":
            raise ProductionIntegrityError("proof incomplete")
        raise OSError("still unavailable")
    with pytest.raises((OSError, ProductionIntegrityError)):
        supervisor.run("audit_publication", publish, recover_local=local)
    assert calls == ["publish", "prove_intent"]


def test_aut1_local_recovery_cannot_be_attached_to_paid_stage():
    supervisor = RecoverySupervisor(identity={}, emit=lambda *a, **k: None)
    with pytest.raises(ProductionControlError):
        supervisor.run("translation", lambda: pytest.fail("send"), recover_local=lambda: pytest.fail("retry"))


def test_aut1_lifecycle_refuses_uncommitted_success_and_nonadvancing_audit():
    progress, events, _, _, kwargs = _fixture()
    kwargs["process_chapter"] = lambda chapter: True
    with pytest.raises(SystemExit) as stopped:
        run_unattended_lifecycle(**kwargs)
    assert stopped.value.code == 1
    assert progress["next_chapter"] == 2
    assert "process:3" not in events
    with pytest.raises(ProductionIntegrityError):
        run_due_audits(progress={"last_completed": 25, "last_audited_chapter": 0},
                       audit_interval=25, run_periodic_audit=lambda *a: None)


@pytest.fixture
def aut1_engine(monkeypatch, tmp_path):
    import translate_production_unattended_scheduled_retroactive_dynamic_pricing as engine
    from novel_project import NovelConfig, ResidualCJKMicroRepairConfig
    from translation_profile import load_translation_profile
    from translation_model_config import resolve_translation_model
    from production_state_extraction import DEFAULT_STATE_POLICY
    monkeypatch.chdir(tmp_path)
    paths = {"OUTPUT_DIR": "output/chapters", "PENDING_DIR": "output/pending",
             "QA_DIR": "output/qa", "AUDIT_DIR": "output/audits", "BACKUP_DIR": "backup",
             "RETROACTIVE_DIR": "output/retroactive_fixes", "PAID_REQUEST_JOURNAL_DIR": "data/provider_request_journal",
             "GLOSSARY_FILE": "data/glossary.json", "PROGRESS_FILE": "data/progress.json",
             "CONTINUITY_FILE": "data/continuity.json", "ISSUES_FILE": "data/issues.json",
             "AUDIT_HISTORY_FILE": "data/audit_history.json", "CHAPTERS_FILE": "data/chapters.json"}
    for name, rel in paths.items():
        path = tmp_path / rel
        (path.parent if path.suffix else path).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, name, path)
    config = NovelConfig(id="aut1_fixture", title="Synthetic", source_language="zh", target_language="en",
        profile="xianxia_xuanhuan", final_chapter=26,
        residual_cjk_micro_repair=ResidualCJKMicroRepairConfig(enabled=True))
    model = resolve_translation_model("v41-flash")
    progress = _fixture(next_chapter=1)[0]
    progress.update(total_cache_hit_tokens=0, total_cache_miss_tokens=0)
    chapters = {n: {"chapter": n, "text": "青雲宗的弟子回家。", "title": "Synthetic"} for n in range(1, 27)}
    values = {"ACTIVE_NOVEL_CONFIG": config, "ACTIVE_PROFILE": load_translation_profile("xianxia_xuanhuan", Path(__file__).resolve().parents[1]),
        "ACTIVE_NOVEL_POLICY": None, "MODEL": model.model,
        "PRODUCTION_TRANSLATION_MAX_TOKENS": 12000, "PRODUCTION_TRANSLATION_THINKING": "disabled",
        "STATE_EXTRACTION_POLICY": DEFAULT_STATE_POLICY,
        "V4_PRO_PRICES": model.prices, "PRICING_VERSION": model.pricing_version,
        "chapters": list(chapters.values()), "chapter_map": chapters, "progress": progress,
        "glossary": {"entries": {"青雲宗": {"english": "Azure Cloud Sect", "category": "sect", "status": "locked",
                                                "strict_qa": True, "repair_aliases": ["Blue Cloud Sect"]}}},
        "continuity": {"latest_chapter": 0, "summary": ""}, "issues_data": {"issues": []},
        "audit_history": {"audits": []}, "system_prompt": "Synthetic translation policy.",
        "STATE_PROMPT": "Synthetic state policy.", "TERM_REVIEW_MODEL_PROMPT": "Synthetic review policy.",
        "GENERIC_JSON_REPAIR_PROMPT": "Synthetic JSON policy.", "FINAL_CHAPTER": 26, "AUDIT_INTERVAL": 25,
        "RUN_LOGGER": NS(path=None), "AUTO_OFF_PEAK_ONLY": False}
    for name, value in values.items():
        monkeypatch.setattr(engine, name, value)
    engine.save_json(tmp_path / "novel.json", config.to_dict())
    engine.save_json(engine.CHAPTERS_FILE, list(chapters.values()))
    calls, events, queue, audits = [], [], [], []
    monkeypatch.setattr(engine, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(engine, "log_error", lambda *a, **k: None)
    monkeypatch.setattr(engine, "wait_for_api_permission", lambda: None)
    def response(content, finish="stop"):
        return NS(model=model.model, id="synthetic", choices=[NS(message=NS(content=content), finish_reason=finish)],
                  usage=NS(prompt_tokens=20, completion_tokens=30, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=20))
    def send(**kwargs):
        # All requests originate in the current chronological chapter.
        assert engine.continuity["latest_chapter"] == engine.progress["last_completed"]
        if engine.progress["next_chapter"] == 26:
            assert engine.progress["last_audited_chapter"] == 25
        calls.append(copy.deepcopy(kwargs))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    monkeypatch.setattr(engine, "send_api_request_once", send)
    for name in ("progress", "glossary", "continuity", "issues_data", "audit_history"):
        path = {"progress": engine.PROGRESS_FILE, "glossary": engine.GLOSSARY_FILE, "continuity": engine.CONTINUITY_FILE,
                "issues_data": engine.ISSUES_FILE, "audit_history": engine.AUDIT_HISTORY_FILE}[name]
        engine.save_json(path, getattr(engine, name))
    def audit(start, end):
        assert engine.progress["last_completed"] == end
        audits.append((start, end))
        engine.progress["last_audited_chapter"] = end
        engine.save_json(engine.PROGRESS_FILE, engine.progress)
        engine.create_checkpoint(end)
    real_audit = engine.run_periodic_audit
    monkeypatch.setattr(engine, "run_periodic_audit", audit)
    # This AUT-1 fixture deliberately stubs audit publication/intent evidence.
    # AUT-2's real whole-history proof is exercised with real finalization below.
    monkeypatch.setattr(engine, "run_whole_history_consistency", lambda: {
        "status": "SYNTHETIC_AUDIT_STUB", "deferred_chapters": 0})
    return NS(real_audit=real_audit, engine=engine, calls=calls, events=events, queue=queue, audits=audits,
              response=response, root=tmp_path)


def _aut1_queue(h, count=26):
    text = "The Azure Cloud Sect gate opened. The disciple returned home."
    for n in range(1, count + 1):
        state = json.dumps({"new_terms": [], "continuity_summary": f"The disciple returned in chapter {n}.", "issues": []})
        if n == 1:
            h.queue.extend([h.response("The disciple crossed the valley and saw the gate", "length"),
                            h.response("The Blue Cloud Sect (青雲宗) gate opened. The disciple returned home."),
                            h.response('{"new_terms": [] "continuity_summary": "He returned.", "issues": []}'), h.response(state)])
        elif n == 2:
            h.queue.extend([h.response(text), h.response("", "length"), h.response(state)])
        else:
            h.queue.extend([h.response(text), h.response(state)])


def test_aut1_real_chapters_recover_and_continue_through_25_and_final_audits(aut1_engine):
    h = aut1_engine
    _aut1_queue(h)
    h.engine.run_unattended_production()
    assert h.engine.progress["completed"] == list(range(1, 27))
    assert h.audits == [(1, 25), (26, 26)]
    assert h.engine.continuity["latest_chapter"] == 26
    assert not list(h.engine.PENDING_DIR.iterdir())
    assert len(list(h.engine.OUTPUT_DIR.glob("*.md"))) == 26
    assert len(h.calls) == 55
    assert [call["max_tokens"] for call in h.calls[:4]] == [12000, 24000, 4000, 5000]
    first = (h.engine.OUTPUT_DIR / "0001.md").read_text(encoding="utf-8")
    assert "Azure Cloud Sect" in first and "青" not in first and "Blue Cloud" not in first
    assert (h.engine.BACKUP_DIR / "checkpoint_0025/progress.json").is_file()
    for path in h.engine.QA_DIR.glob("*.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["commit_allowed"]
    status = [fields for event, fields in h.events if event == "autonomous_recovery"]
    assert all("青雲宗" not in json.dumps(item, ensure_ascii=False) for item in status)
    assert any(item["reason_code"] == "QA_PASSED" for item in status)
    before = len(h.calls)
    h.engine.run_unattended_production()
    assert len(h.calls) == before and h.audits == [(1, 25), (26, 26)]


def _aut1_reload_durable(h, monkeypatch):
    for name, path in (("progress", h.engine.PROGRESS_FILE), ("glossary", h.engine.GLOSSARY_FILE),
                       ("continuity", h.engine.CONTINUITY_FILE), ("issues_data", h.engine.ISSUES_FILE),
                       ("audit_history", h.engine.AUDIT_HISTORY_FILE)):
        monkeypatch.setattr(h.engine, name, json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("boundary", ["before_state", "after_state", "before_final", "after_final",
                                     "before_glossary", "before_progress", "after_progress"])
def test_aut1_crash_resume_commits_next_chapter_without_duplicate_requests(aut1_engine, monkeypatch, boundary):
    h = aut1_engine
    _aut1_queue(h, count=3)
    monkeypatch.setattr(h.engine, "FINAL_CHAPTER", 3)
    save, write = h.engine.save_json, h.engine.atomic_write_text
    failed = []
    def checkpoint(path, value):
        trigger = ((boundary.endswith("state") and Path(path) == h.engine.PENDING_DIR / "0001.json" and value.get("stage") == "state_extracted")
                   or (boundary.endswith("progress") and Path(path) == h.engine.PROGRESS_FILE and value.get("last_completed") == 1)
                   or (boundary == "before_glossary" and Path(path) == h.engine.GLOSSARY_FILE))
        if trigger and not failed:
            failed.append(True)
            if boundary.startswith("after"):
                save(path, value)
            raise OSError("synthetic crash at checkpoint")
        return save(path, value)
    def prose(path, value):
        if boundary.endswith("final") and Path(path) == h.engine.OUTPUT_DIR / "0001.md" and not failed:
            failed.append(True)
            if boundary.startswith("after"):
                write(path, value)
            raise OSError("synthetic crash at final")
        return write(path, value)
    monkeypatch.setattr(h.engine, "save_json", checkpoint)
    monkeypatch.setattr(h.engine, "atomic_write_text", prose)
    if boundary.endswith("state"):
        assert h.engine.process_chapter(1) is False
    else:
        with pytest.raises(OSError):
            h.engine.process_chapter(1)
    assert failed and len(h.calls) == 4
    assert (h.engine.PENDING_DIR / "0001.md").is_file()
    _aut1_reload_durable(h, monkeypatch)
    h.engine.run_unattended_production()
    assert h.engine.progress["completed"] == [1, 2, 3]
    assert h.engine.continuity["latest_chapter"] == 3
    assert len(h.calls) == 9 and not h.queue
    assert not list(h.engine.PENDING_DIR.iterdir())


@pytest.mark.parametrize("mutation", ["uncertain", "started", "source", "model", "policy"])
def test_aut1_ambiguous_or_mismatched_state_stops_without_resend(aut1_engine, monkeypatch, mutation):
    h = aut1_engine
    h.queue.extend([h.response("The Azure Cloud Sect disciple returned home."), TimeoutError("synthetic timeout")])
    with pytest.raises(TimeoutError):
        h.engine.process_chapter(1)
    pending = h.engine.PENDING_DIR / "0001.md"
    original = pending.read_bytes()
    if mutation == "started":
        path = h.engine.PAID_REQUEST_JOURNAL_DIR / "0001.state_extraction.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "started"
        h.engine.save_json(path, _with_record_hash(record))
    elif mutation == "source":
        h.engine.chapter_map[1]["text"] += "他停下。"
    elif mutation == "model":
        monkeypatch.setattr(h.engine, "MODEL", "deepseek-pro")
    elif mutation == "policy":
        monkeypatch.setattr(h.engine, "STATE_PROMPT", "Changed synthetic policy")
    assert h.engine.process_chapter(1) is False
    assert len(h.calls) == 2 and not h.queue
    assert pending.read_bytes() == original
    assert h.engine.progress["completed"] == [] and not list(h.engine.OUTPUT_DIR.iterdir())
    assert any(fields.get("reason_code") in {"AMBIGUOUS_PAID_REQUEST", "IDENTITY_OR_PROOF_MISMATCH"}
               for event, fields in h.events if event == "autonomous_recovery")


@pytest.mark.parametrize("failure", ["cap", "unproven_truncation"])
def test_aut1_spent_or_ineligible_recovery_preserves_pending_and_never_advances(aut1_engine, failure):
    h = aut1_engine
    if failure == "cap":
        h.queue.extend([h.response("The disciple crossed the valley and saw the gate", "length"),
                        h.response("The disciple crossed the valley and saw the gate", "length")])
    else:
        h.queue.append(h.response("...", "length"))
    for _ in range(2):
        assert h.engine.process_chapter(1) is False
    assert len(h.calls) == (2 if failure == "cap" else 1)
    assert not h.queue and h.engine.progress["completed"] == []
    assert not list(h.engine.OUTPUT_DIR.iterdir())


@pytest.mark.parametrize("failure", ["cjk", "terminology"])
def test_aut1_unrepairable_reader_flaw_commits_and_is_flagged(aut1_engine, failure):
    from review_queue import read_items
    h = aut1_engine
    if failure == "cjk":
        translation = "The Azure Cloud Sect disciple spoke 未知陌生文字從未出現在來源文字中。"
    else:
        h.engine.glossary["entries"]["青雲宗"].update(auto_repair=False)
        h.engine.glossary["entries"]["青雲宗"].pop("repair_aliases")
        translation = "The wrong organization opened its gate."
    h.queue.extend([h.response(translation), h.response(json.dumps(
        {"new_terms": [], "continuity_summary": "He returned.", "issues": []}))])
    if failure == "cjk":
        # Too much for a repair, so one fresh translation comes first.
        h.queue.insert(0, h.response(translation))
        assert h.engine.process_chapter(1) is False
    assert h.engine.process_chapter(1) is True
    assert not h.queue and h.engine.progress["completed"] == [1]
    assert (h.engine.OUTPUT_DIR / "0001.md").read_text(encoding="utf-8") == translation
    kinds = [item["kind"] for item in read_items(h.engine.GLOSSARY_FILE.parent)]
    assert kinds == (["fresh_translation", "commit_qa"] if failure == "cjk" else ["commit_qa"])


def test_aut1_heavy_leftovers_get_one_fresh_translation_and_keep_the_cost(aut1_engine):
    h = aut1_engine
    h.queue.extend([h.response("The Azure Cloud Sect disciple spoke 未知陌生文字從未出現在來源文字中。"),
                    h.response("The Azure Cloud Sect disciple returned home."), h.response(json.dumps(
                        {"new_terms": [], "continuity_summary": "He returned.", "issues": []}))])
    assert h.engine.process_chapter(1) is False
    assert not (h.engine.PENDING_DIR / "0001.md").exists()
    saved = list((h.engine.BACKUP_DIR / "superseded_translations").rglob("0001.md"))
    assert len(saved) == 1
    cost_of_first = h.engine.progress["rate_correct_cost_usd"]
    assert cost_of_first > 0
    assert h.engine.process_chapter(1) is True
    assert len(h.calls) == 3 and not h.queue
    assert (h.engine.OUTPUT_DIR / "0001.md").read_text(encoding="utf-8") == "The Azure Cloud Sect disciple returned home."
    assert h.engine.progress["rate_correct_cost_usd"] > cost_of_first


@pytest.mark.parametrize("outcome", ["resolution_failed", "identity_failed", "unresolved"])
def test_aut1_candidate_conflict_commits_without_review_and_is_flagged(aut1_engine, monkeypatch, outcome):
    from review_queue import read_items
    h = aut1_engine
    monkeypatch.setattr(h.engine, "run_candidate_preparation_checkpoint",
                        lambda **kwargs: {"status": outcome, "error": RuntimeError("synthetic conflict")})
    h.queue.extend([h.response("The Azure Cloud Sect disciple returned home."), h.response(json.dumps(
        {"new_terms": [], "continuity_summary": "He returned.", "issues": []}))])
    assert h.engine.process_chapter(1) is True
    assert h.engine.progress["completed"] == [1]
    assert [item["kind"] for item in read_items(h.engine.GLOSSARY_FILE.parent)] == ["glossary_review"]


def test_opting_out_of_automatic_repair_survives_a_round_trip():
    from novel_project import NovelConfig
    base = dict(id="x", title="X", source_language="zh", target_language="en", profile="p", final_chapter=1)
    assert NovelConfig.from_dict(base).residual_cjk_micro_repair.enabled is True
    assert NovelConfig.from_dict(base).glossary_candidate_auto_resolution.allow_model_adjudication is True
    config = NovelConfig.from_dict({**base, "residual_cjk_micro_repair": {"enabled": False},
                                    "glossary_candidate_auto_resolution": {"enabled": False}})
    again = NovelConfig.from_dict(config.to_dict())
    assert again.residual_cjk_micro_repair.enabled is False
    assert again.glossary_candidate_auto_resolution.enabled is False
    assert "residual_cjk_micro_repair" not in NovelConfig.from_dict(base).to_dict()


@pytest.mark.parametrize("drift", [False, True])
def test_aut1_real_audit_publication_reconciles_once_without_reacquisition(aut1_engine, monkeypatch, drift):
    h = aut1_engine
    h.engine.glossary["entries"].clear()
    h.engine.save_json(h.engine.GLOSSARY_FILE, h.engine.glossary)
    h.queue.extend([h.response("The disciple returned home."), h.response(json.dumps(
        {"new_terms": [], "continuity_summary": "He returned.", "issues": []}))])
    assert h.engine.process_chapter(1)
    # A zero-candidate audit uses the actual acquisition/finalization protocol
    # and creates the real durable intent/transaction/backups, with zero sends.
    save = h.engine.save_json
    failures = []
    def fail_once(path, value):
        if Path(path) == h.engine.AUDIT_HISTORY_FILE and not failures:
            failures.append(True)
            if drift:
                (h.engine.OUTPUT_DIR / "0001.md").write_text("Unexpected historic mutation.", encoding="utf-8")
            raise OSError("synthetic local publication failure")
        return save(path, value)
    monkeypatch.setattr(h.engine, "save_json", fail_once)
    before = (h.engine.OUTPUT_DIR / "0001.md").read_bytes()
    if drift:
        with pytest.raises(ProductionIntegrityError):
            from production_run_lock import production_run_lock
            with production_run_lock(h.root):
                h.real_audit(1, 1)
        assert h.engine.progress["last_audited_chapter"] == 0
    else:
        from production_run_lock import production_run_lock
        with production_run_lock(h.root):
            h.real_audit(1, 1)
        assert h.engine.progress["last_audited_chapter"] == 1
        assert (h.engine.OUTPUT_DIR / "0001.md").read_bytes() == before
        assert any(fields.get("reason_code") == "LOCAL_AUDIT_RECONCILED" for _, fields in h.events)
        assert (h.engine.BACKUP_DIR / "checkpoint_0001/progress.json").is_file()
    assert failures == [True] and len(h.calls) == 2 and not h.queue


def test_aut1_korean_three_chapter_sequence(aut1_engine, monkeypatch):
    from dataclasses import replace
    h = aut1_engine
    monkeypatch.setattr(h.engine, "ACTIVE_NOVEL_CONFIG", replace(h.engine.ACTIVE_NOVEL_CONFIG,
        source_language="ko", final_chapter=3))
    monkeypatch.setattr(h.engine, "FINAL_CHAPTER", 3)
    for n in (1, 2, 3):
        h.engine.chapter_map[n]["text"] = "청운종의 제자가 집으로 돌아왔다."
    term = h.engine.glossary["entries"].pop("青雲宗")
    h.engine.glossary["entries"]["청운종"] = term
    h.engine.save_json(h.engine.GLOSSARY_FILE, h.engine.glossary)
    _aut1_queue(h, count=3)
    for response in h.queue:
        response.choices[0].message.content = response.choices[0].message.content.replace("青雲宗", "청운종")
    h.engine.run_unattended_production()
    assert h.engine.progress["completed"] == [1, 2, 3]
    assert len(h.calls) == 9
    assert "청운종" not in (h.engine.OUTPUT_DIR / "0001.md").read_text(encoding="utf-8")


def test_aut1_logging_failure_never_resets_or_retries_a_stage():
    calls = []
    def broken_log(*args, **kwargs):
        raise OSError("synthetic log unavailable")
    supervisor = RecoverySupervisor(identity={}, emit=broken_log)
    assert supervisor.run("state", lambda: calls.append(1) or {"status": "extracted"}) == {"status": "extracted"}
    with pytest.raises(ProductionControlError):
        supervisor.run("state", lambda: calls.append(2))
    assert calls == [1]


@pytest.mark.parametrize("drift", ["count", "fragment", "missing_inventory", "new_blocker"])
def test_aut1_deterministic_alias_never_changes_unrelated_qa_inventory(aut1_engine, drift):
    from strict_terminology_repair import perform_deterministic_strict_terminology_repair
    h = aut1_engine
    text = "The Blue Cloud Sect (青雲宗) gate opened."
    writes = []
    def qa(candidate):
        value = h.engine.local_qa(1, h.engine.chapter_map[1]["text"], candidate, "stop")
        if "Azure" in candidate:
            if drift == "count":
                value["commit_blockers"][0]["count"] += 1
            elif drift == "fragment":
                value["commit_blockers"][0]["fragments"] = ["不同"]
            elif drift == "missing_inventory":
                value.pop("commit_blockers")
            else:
                value["commit_blockers"].append({"type": "synthetic_new_blocker"})
        return value
    result = perform_deterministic_strict_terminology_repair(chapter_number=1,
        source_text=h.engine.chapter_map[1]["text"], translation=text, meta={},
        glossary_entries=h.engine.glossary["entries"], high_priority_categories=h.engine.ACTIVE_PROFILE.high_priority_categories,
        persist_meta=writes.append, persist_translation=writes.append, qa_checker=qa)
    assert result["status"] == "qa_rejected" and result["translation"] == text
    assert writes == [] and not h.calls


@pytest.mark.parametrize("tamper", [None, "sum", "duplicate", "order", "nested", "rate", "nonfinite", "provider", "component_provider"])
def test_aut1_state_recovery_retains_exact_mixed_window_prices(aut1_engine, monkeypatch, tamper):
    from datetime import datetime, timezone
    import noveltranslator_core as core
    h = aut1_engine
    moments = iter([datetime(2026, 10, 22, 2, tzinfo=timezone.utc), datetime(2026, 10, 22, 12, tzinfo=timezone.utc)])
    monkeypatch.setattr(h.engine, "pricing_snapshot", lambda: core.pricing_snapshot(next(moments)))
    h.queue.extend([h.response('{"new_terms": [] "continuity_summary": "He returned.", "issues": []}'),
                    h.response(json.dumps({"new_terms": [], "continuity_summary": "He returned.", "issues": []}))])
    _, stats = h.engine.extract_state(1, h.engine.chapter_map[1]["text"], "The disciple returned home.")
    operations = stats["state_accepted_operations"]
    assert [item["stats"]["pricing_period"] for item in operations] == ["peak", "off_peak"]
    assert stats["cost_usd"] == sum(item["stats"]["cost_usd"] for item in operations)
    assert core.validate_stats_record(stats, "state") == []
    if tamper is None:
        assert h.engine.reprice_saved_stats_if_needed(stats) == stats
    else:
        if tamper == "sum": stats["cost_usd"] += 1
        elif tamper == "duplicate": operations[1]["operation_id"] = operations[0]["operation_id"]
        elif tamper == "order": operations.reverse()
        elif tamper == "nested": operations[0]["stats"]["state_accepted_operations"] = []
        elif tamper == "rate": operations[0]["stats"]["output_rate_per_million"] += 1
        elif tamper == "provider": stats["provider"] = "command_code_provider"
        elif tamper == "component_provider": operations[0]["stats"]["provider"] = "command_code_provider"
        else: stats["cost_usd"] = float("nan")
        with pytest.raises(ProductionIntegrityError):
            h.engine.reprice_saved_stats_if_needed(stats)
    assert len(h.calls) == 2


@pytest.mark.parametrize("crash", [False, True])
def test_aut1_glossary_semantic_correction_commits_then_next_chapter(aut1_engine, monkeypatch, crash):
    h = aut1_engine
    monkeypatch.setattr(h.engine, "FINAL_CHAPTER", 3)
    h.engine.chapter_map[2]["text"] = "青雲宗的弟子見到紫雲門。"
    h.engine.chapter_map[3]["text"] = "青雲宗的弟子再次見到紫雲門。"
    _aut1_queue(h, count=3)
    state = {"new_terms": [{"chinese": "紫雲門", "english": "Violet Cloud Gate", "category": "sect", "notes": ""}],
             "continuity_summary": "The disciple saw Violet Cloud Gate.", "issues": []}
    h.queue[6] = h.response(json.dumps(state))
    h.queue[7:7] = [h.response(json.dumps({"decisions": [{"candidate_id": "c01", "action": ""}], "issues": []})),
                     h.response(json.dumps({"decisions": [{"candidate_id": "c01", "action": "accept"}], "issues": []}))]
    save = h.engine.save_json
    failures, reviewed = [], []
    def fail_glossary(path, value):
        if Path(path) == h.engine.PENDING_DIR / "0002.json" and value.get("stage") == "reviewed":
            reviewed.append(copy.deepcopy(value))
        if crash and Path(path) == h.engine.GLOSSARY_FILE and "紫雲門" in value["entries"] and not failures:
            failures.append(True)
            raise OSError("synthetic interrupted glossary commit")
        return save(path, value)
    monkeypatch.setattr(h.engine, "save_json", fail_glossary)
    if crash:
        with pytest.raises(OSError):
            h.engine.run_unattended_production()
        assert len(h.calls) == 9
        assert h.engine.progress["last_completed"] == 1
        assert (h.engine.PENDING_DIR / "0002.md").is_file()
        _aut1_reload_durable(h, monkeypatch)
    h.engine.run_unattended_production()
    assert len(h.calls) == 11 and not h.queue
    assert h.engine.progress["completed"] == [1, 2, 3]
    assert h.engine.glossary["entries"]["紫雲門"]["english"] == "Violet Cloud Gate"
    assert "Violet Cloud Gate" in h.calls[-2]["messages"][-1]["content"]
    from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
    assert reviewed[-1][CHAPTER_REVIEW_RECOVERY_KEY]["semantic_correction"]["attempted"] is True
    assert reviewed[-1]["review_result"]["decisions"][0]["action"] == "accept"


def test_aut2_history_check_follows_tail_and_does_not_claim_publication():
    progress, events, output, errors, kwargs = _fixture()
    def whole_history():
        assert events[-1] == "final_audit"
        events.append("whole_history")
        return {"status": "CHECKED_WITH_DEFERRALS", "deferred_chapters": 1}
    kwargs["run_whole_history_consistency"] = whole_history
    run_unattended_lifecycle(**kwargs)
    assert not errors
    assert "whole_history" in events
    assert "NOVEL TRANSLATION COMPLETE" not in output
    assert "TRANSLATION COMMITTED — PUBLICATION NOT VERIFIED" in output
    assert events[-1][2]["reason"] == "publication_not_verified"
    assert events[-1][2]["stage"] == "translation_committed"


@pytest.fixture(params=["zh", "ko"])
def aut2_history_novel(aut1_engine, monkeypatch, request):
    """Accepted synthetic decisions; actual finalizer/transaction, zero transport."""
    from dataclasses import replace
    import production_periodic_audit_finalization as final
    from retroactive_span_planning import canonical_hash
    h, e = aut1_engine, aut1_engine.engine
    key = "韓" if request.param == "zh" else "한민"
    monkeypatch.setattr(e, "ACTIVE_NOVEL_CONFIG", replace(e.ACTIVE_NOVEL_CONFIG, source_language=request.param))
    chapters = {n: {"chapter": n, "production_index": n, "text": key if n in (1, 2, 26) else ""} for n in range(1, 27)}
    monkeypatch.setattr(e, "chapters", list(chapters.values()))
    monkeypatch.setattr(e, "chapter_map", chapters)
    monkeypatch.setattr(e, "glossary", {"entries": {key: {"english": "Han", "category": "character", "status": "provisional", "first_chapter": 1}}})
    original_apply = e.apply_periodic_audit
    monkeypatch.setattr(e, "apply_periodic_audit", lambda start, end, result, glossary_data=None:
        original_apply(start, end, result, glossary_data, targets=[{"chinese": key}]))
    e.save_json(e.GLOSSARY_FILE, e.glossary)
    e.save_json(h.root / "novel.json", e.ACTIVE_NOVEL_CONFIG.to_dict())
    e.save_json(e.CHAPTERS_FILE, list(chapters.values()))
    publication_dir = h.root / "final/epub"
    publication_dir.mkdir(parents=True)
    (publication_dir / "prior.epub").write_bytes(b"synthetic prior published bytes")
    before = {n: ("\ufeff# Chapter %d\r\n\r\n**Han** — Hand, café.\r\n" % n if n in (1, 26)
                  else "Han met Han.\r\n" if n == 2 else "Ordinary prose.\r\n").encode() for n in range(1, 27)}
    for n in range(1, 26):
        (e.OUTPUT_DIR / f"{n:04d}.md").write_bytes(before[n])
    e.progress.update(completed=list(range(1, 26)), last_completed=25, next_chapter=26)
    e.save_json(e.PROGRESS_FILE, e.progress)
    stats = {field: 0 for field in final.STAT_TOTAL_FIELDS}
    stats["api_request_count"] = 0
    chunking = {"candidate_count": 1, "candidate_set_hash": canonical_hash("synthetic-accepted-decisions"), "api_request_count": 0}
    e.save_json(e.periodic_audit_checkpoint_path(1, 25), {"synthetic_accepted_decisions": True})
    from production_run_lock import production_run_lock
    with production_run_lock(h.root):
        final.finalize_periodic_audit(e, 1, 25, {"decisions": [{"chinese": key, "action": "keep"}], "issues": []}, stats, chunking)
    (e.OUTPUT_DIR / "0026.md").write_bytes(before[26])
    e.progress.update(completed=list(range(1, 27)), last_completed=26, next_chapter=27)
    e.save_json(e.PROGRESS_FILE, e.progress)
    e.save_json(e.periodic_audit_checkpoint_path(26, 26), {"synthetic_accepted_decisions": True})
    with production_run_lock(h.root):
        final.finalize_periodic_audit(e, 26, 26, {"decisions": [{"chinese": key, "action": "correct", "english": "Hann"}], "issues": []}, stats, chunking)
    return h, before


def test_aut2_final_pass_reaches_history_and_replays_without_writes(aut2_history_novel):
    import historical_reconciliation as history
    import production_periodic_audit_finalization as final
    h, before = aut2_history_novel
    e = h.engine
    intent = final.load_intent(e, 26, 26)
    public_proof = final.transaction_proof(e, 26, 26, intent["baseline"]["glossary"],
        intent["post"]["glossary"], intent["record"]["changes"])
    assert "_before_bytes" not in public_proof
    json.dumps(public_proof)  # operator fingerprints must remain serializable
    discovery = intent["record"]["historical_reconciliation"]
    assert (discovery["examined_chapters"], discovery["proven_eligible_chapters"], discovery["deferred_chapters"]) == (26, 2, 1)
    report = history.save_final_consistency(e)
    assert (report["examined_chapters"], report["audit_boundary"], report["deferred_chapters"]) == (26, 26, 1)
    assert report["corrected_chapters"] == 0  # transaction owns edits; this pass observes
    assert len(report["transactions"]) == 2 and report["publication_status"] == "NOT_VERIFIED"
    assert h.calls == []
    for n, raw in before.items():
        assert (e.OUTPUT_DIR / f"{n:04d}.md").read_bytes() == (raw.replace(b"**Han**", b"**Hann**") if n in (1, 26) else raw)
    snapshot = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in h.root.rglob("*") if p.is_file()}
    assert history.save_final_consistency(e) == report
    assert snapshot == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in h.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("damage", ["source", "backup", "outside_editor", "intent", "checkpoint", "glossary", "missing_chapter", "extra_chapter"])
def test_aut2_final_pass_rejects_unproven_history(aut2_history_novel, damage):
    import historical_reconciliation as history
    h, _ = aut2_history_novel
    e = h.engine
    if damage == "source": e.chapter_map[1]["text"] += "changed"
    elif damage == "backup": next(e.BACKUP_DIR.glob("retroactive_*/chapters_before/0001.md")).unlink()
    elif damage == "outside_editor": (e.OUTPUT_DIR / "0001.md").write_bytes(b"Outside editor")
    elif damage == "intent": (e.AUDIT_DIR / "audit_0026_0026.finalization.json").write_bytes(b"{bad")
    elif damage == "checkpoint": e.periodic_audit_checkpoint_path(26, 26).write_bytes(b"changed")
    elif damage == "glossary": e.glossary["entries"][next(iter(e.glossary["entries"]))]["english"] = "Changed"
    elif damage == "missing_chapter": (e.OUTPUT_DIR / "0002.md").unlink()
    else: (e.OUTPUT_DIR / "0999.md").write_bytes(b"foreign")
    snapshot = {str(p): p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    with pytest.raises((RuntimeError, ValueError, KeyError, FileNotFoundError)):
        history.save_final_consistency(e)
    assert snapshot == {str(p): p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    assert h.calls == []


def test_aut2_publication_inventory_detects_missing_and_renamed_outputs(aut2_history_novel):
    import historical_reconciliation as history
    import build_publication_epub as publication
    from novel_project import NovelProject
    from production_failure_classification import ProductionIntegrityError
    h, _ = aut2_history_novel
    project = NovelProject(Path(__file__).resolve().parents[1], h.root, h.engine.ACTIVE_NOVEL_CONFIG)
    prior = h.root / "final/epub/prior.epub"
    raw = prior.read_bytes()
    renamed = prior.with_name("renamed.epub")
    prior.rename(renamed)
    status = history.publication_freshness(project)
    assert status["status"] == "STALE" and status["rebuilt_epubs"] == 0
    assert any(a["path"] == "final/epub/prior.epub" and a["status"] == "MISSING" for a in status["artifacts"])
    assert any(a["path"] == "final/epub/renamed.epub" and a["status"] == "NOT_VERIFIED" for a in status["artifacts"])
    snapshot = {str(p): p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    for call in (publication.build_publication_epub, publication.verify_existing_publication_epub):
        with pytest.raises(ProductionIntegrityError, match="PUBLICATION_STALE"):
            call(project)
    assert renamed.read_bytes() == raw
    assert snapshot == {str(p): p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
