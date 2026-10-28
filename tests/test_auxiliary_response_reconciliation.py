"""Production GOAT crash proofs. Only disposable state and synthetic transport."""
import copy
from dataclasses import replace
import json

import pytest
import glossary_audit as audit
from auxiliary_response_reconciliation import JournalOwnedAuditCallback
from noveltranslator_core import add_accounting_totals_once
from production_paid_request_journal import _updated_record, _sha256_json
from test_glossary_audit import identified_candidate
from test_production_goat_runtime import engine as unlocked_engine, zero_network, response, IDENTITY
from production_run_lock import production_run_lock


@pytest.fixture
def engine(unlocked_engine):
    # Direct mutating calls reproduce the launcher's outer novel lock lifetime.
    # Keep the shared fixture unlocked: executor tests own their own acquisition.
    with production_run_lock(unlocked_engine[0].root):
        yield unlocked_engine


class Crash(BaseException):
    pass


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def setup(engine, monkeypatch, kind="audit", count=1):
    project, module, sends, client, budget, *_ = engine
    candidates = [identified_candidate("甲", "Alpha")]
    if count == 2:
        candidates.append(identified_candidate("乙", "Beta"))
    module.glossary["entries"] = {c["chinese"]: copy.deepcopy(c) for c in candidates}
    # Audits publish only committed chapter windows. This fixture formerly ran
    # an audit ahead of progress, which never represented a production state.
    module.progress.update(completed=[1], last_completed=1, next_chapter=2, last_audited_chapter=0)
    (module.OUTPUT_DIR / "0001.md").write_text("Alpha arrived.", encoding="utf-8")
    for name, path in (("progress", module.PROGRESS_FILE), ("glossary", module.GLOSSARY_FILE),
                       ("audit_history", module.AUDIT_HISTORY_FILE), ("issues_data", module.ISSUES_FILE)):
        module.save_json(path, getattr(module, name))
    module._test_audit_baseline = {name: copy.deepcopy(getattr(module, name)) for name in
                                  ("progress", "glossary", "audit_history", "issues_data")}
    monkeypatch.setattr(module, "audit_targets", lambda *a, **kw: ((copy.deepcopy(candidates),
        {"safe_glossary_keys": [c["chinese"] for c in candidates]}) if kw.get("return_integrity") else copy.deepcopy(candidates)))
    monkeypatch.setattr(module, "compact_glossary_index", lambda **kw: "exact immutable index")
    valid = {"decisions": [{"candidate_id": f"c{i:02d}", "action": "keep", "english": c["english"],
        "approved_renderings": [], "merge_with": "", "notes": ""} for i, c in enumerate(candidates, 1)], "issues": []}

    def physical(**body):
        sends.append(copy.deepcopy(body))
        prompt = body["messages"][-1]["content"]
        content = json.dumps(valid, ensure_ascii=False)
        if kind == "json_repair" and not prompt.startswith("Repair this single"):
            content = "{malformed paid JSON"
        elif kind == "semantic_correction":
            if "AUTHORIZED FIELDS BY CANDIDATE" in prompt:
                content = '{"decisions":[{"candidate_id":"c01","action":"keep"}]}'
            else:
                bad = copy.deepcopy(valid)
                bad["decisions"][0]["action"] = ""
                content = json.dumps(bad)
        return response(content=content, reasoning=4)
    client.chat.completions.create = physical
    path = module.periodic_audit_checkpoint_path(1, 1)
    return module, sends, client, budget, path, valid


def reset_synthetic_publication(module):
    """Rewind only disposable local publication when testing an earlier paid prefix.

    Finalization now deliberately rejects checkpoint rewinds after its intent;
    transport tests must rewind the entire synthetic associated publication.
    Paid journals remain intact. Only the proven zero-edit transaction belonging
    to the discarded synthetic publication is reset with that publication.
    """
    from production_periodic_audit_finalization import load_intent
    from historical_reconciliation import (
        validate_invalidation_identity, read_object, invalidation_record,
    )
    intent = load_intent(module, 1, 1)
    if intent is not None:
        import production_periodic_audit_finalization as final
        plan = intent["transaction_plan"]
        assert not any(item["edits"] for item in plan["chapters"])
        proof = final.transaction_proof(module, 1, 1, intent["baseline"]["glossary"],
            intent["post"]["glossary"], intent["record"]["changes"])
        assert proof["plan"] == plan and proof["state"]["state"] == "FINALIZED"
        slot = final.canonical_sha256({k: plan["request"][k]
            for k in ("novel_id", "audit_label", "through_chapter")})
        directory = module.RETROACTIVE_DIR / "transactions"
        authorization = directory / (slot + ".authorization.json")
        assert read(authorization)["plan_sha256"] == final.canonical_sha256(plan)
        owned = [directory / (slot + suffix) for suffix in
                 (".plan.json", ".authorization.json", ".state.json", ".result.json")]
        owned += list(directory.glob(slot + ".reconcile.*.json"))
        for path in owned:
            path.unlink()
        receipt = validate_invalidation_identity(module, intent, 1, 1)
        if receipt.exists():
            # Only discard the exact derived receipt of the discarded intent.
            assert read_object(receipt) == invalidation_record(intent)
            receipt.unlink()
    for suffix in (".json", ".finalization.json"):
        path = module.AUDIT_DIR / ("audit_0001_0001" + suffix)
        if path.exists(): path.unlink()
    backup = module.BACKUP_DIR / "checkpoint_0001"
    if backup.exists():
        for path in backup.iterdir(): path.unlink()
        backup.rmdir()
    for name, path in (("progress", module.PROGRESS_FILE), ("glossary", module.GLOSSARY_FILE),
                       ("audit_history", module.AUDIT_HISTORY_FILE), ("issues_data", module.ISSUES_FILE)):
        value = copy.deepcopy(module._test_audit_baseline[name])
        getattr(module, name).clear(); getattr(module, name).update(value)
        module.save_json(path, value)


def unfinished(module, path, kind="audit", lifecycle="in_flight"):
    reset_synthetic_publication(module)
    checkpoint = read(path)
    checkpoint["status"] = "in_progress"
    checkpoint.pop("combined_result", None)
    chunk = checkpoint["chunks"][0]
    chunk["status"] = lifecycle
    chunk.pop("result", None)
    record = next(r for r in checkpoint["attempt_records"] if r["kind"] == kind)
    record["lifecycle"] = lifecycle
    for key in ("raw_content", "raw_content_hash", "parsed_result", "result_hash", "stats",
                "finish_reason", "validation_errors", "effective_result_hash", "merge_audit"):
        record.pop(key, None)
    module.atomic_write_json(path, checkpoint)
    return copy.deepcopy(record)


def journal(record):
    from pathlib import Path
    return Path(record["transport_binding"]["journal_path"])


@pytest.mark.parametrize("kind", ["audit", "json_repair", "semantic_correction"])
def test_exact_paid_response_hydration_result_accounting_and_publication(engine, monkeypatch, kind):
    module, sends, client, budget, path, _ = setup(engine, monkeypatch, kind)
    baseline = {name: copy.deepcopy(getattr(module, name)) for name in
                ("progress", "glossary", "audit_history", "issues_data")}
    module.run_periodic_audit(1, 1)
    first = read(path)
    expected = {name: copy.deepcopy(getattr(module, name)) for name in baseline}
    saved = next(r for r in first["attempt_records"] if r["kind"] == kind)
    lower_before = journal(saved).read_bytes()
    for name, value in baseline.items():
        getattr(module, name).clear()
        getattr(module, name).update(value)
    record = unfinished(module, path, kind)
    before_sends = len(sends)
    client.chat.completions.create = lambda **kw: pytest.fail("provider send during hydration")
    module.run_periodic_audit(1, 1)
    recovered = read(path)
    after = next(r for r in recovered["attempt_records"] if r["kind"] == kind)
    assert len(sends) == before_sends == budget.api_calls
    assert after["operation_id"] == record["operation_id"]
    assert after["attempt"] == record["attempt"]
    assert after["transport_binding"] == record["transport_binding"]
    for key in ("raw_content", "raw_content_hash", "finish_reason", "stats", "parsed_result", "result_hash"):
        assert after[key] == saved[key]
    assert after["paid_journal_recovered"] is True
    assert journal(saved).read_bytes() == lower_before
    assert recovered["combined_result"] == first["combined_result"]
    assert len(recovered["attempt_records"]) == len(first["attempt_records"])
    assert module.progress == expected["progress"]
    assert module.glossary == expected["glossary"]
    assert module.issues_data == expected["issues_data"]
    assert module.audit_history["audits"][-1]["stats"] == expected["audit_history"]["audits"][-1]["stats"]
    assert module.audit_history["audits"][-1]["changes"] == expected["audit_history"]["audits"][-1]["changes"]
    stats = module.audit_history["audits"][-1]["stats"]
    progress = copy.deepcopy(module.progress)
    operation = module.progress["accounted_api_operation_ids"][-1]
    assert add_accounting_totals_once(module.progress, operation, stats) is False
    assert progress == module.progress


@pytest.mark.parametrize("kind", ["audit", "json_repair", "semantic_correction"])
@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_ambiguous_lower_absolute_block(engine, monkeypatch, kind, status):
    module, sends, _, _, path, _ = setup(engine, monkeypatch, kind)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path, kind)
    lower = journal(record)
    module.atomic_write_json(lower, _updated_record(read(lower), status=status, response={}, response_sha256=""))
    before = path.read_bytes(), lower.read_bytes(), len(sends)
    with pytest.raises(RuntimeError, match=status + "; automatic reissue is blocked"):
        module.run_periodic_audit(1, 1)
    assert before[1:] == (lower.read_bytes(), len(sends))
    after = next(r for r in read(path)["attempt_records"] if r["kind"] == kind)
    assert after["operation_id"] == record["operation_id"] and after["attempt"] == record["attempt"]
    assert after["lifecycle"] == record["lifecycle"]


@pytest.mark.parametrize("field", ["operation_id", "request_sha256", "wire_request_sha256", "provider", "model",
    "profile_id", "novel_id", "chapter", "source_sha256", "translation_sha256", "provider_policy",
    "auxiliary_domain_binding"])
def test_identity_mismatch_never_finds_replacement_slot(engine, monkeypatch, field):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path)
    lower = journal(record)
    module.atomic_write_json(lower, _updated_record(read(lower), **{field: "wrong-bound-identity"}))
    before = path.read_bytes(), lower.read_bytes(), len(sends)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        module.run_periodic_audit(1, 1)
    assert before == (path.read_bytes(), lower.read_bytes(), len(sends))


@pytest.mark.parametrize("bad", ["corrupt_json", "record_hash", "response_hash", "unsupported_state", "bad_usage",
    "bad_model", "accepted_content_drift", "accepted_finish_drift"])
def test_corruption_and_invalid_envelope_block(engine, monkeypatch, bad):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path)
    lower = journal(record)
    value = read(lower)
    if bad == "corrupt_json":
        lower.write_text("{broken", encoding="utf-8")
    elif bad == "record_hash":
        value["record_sha256"] = "0" * 64
        module.atomic_write_json(lower, value)
    elif bad in {"response_hash", "unsupported_state"}:
        module.atomic_write_json(lower, _updated_record(value,
            **({"response_sha256": "0" * 64} if bad == "response_hash" else {"status": "unsupported"})))
    else:
        envelope = value["response"]
        if bad == "bad_usage":
            envelope["raw_response"]["usage"] = None
        elif bad == "bad_model":
            envelope["raw_response"]["model"] = "wrong-model"
        elif bad == "accepted_content_drift":
            envelope["content"] = "tampered accepted content"
        else:
            envelope["finish_reason"] = "length"
        module.atomic_write_json(lower, _updated_record(value, response=envelope, response_sha256=_sha256_json(envelope)))
    before = lower.read_bytes(), len(sends)
    with pytest.raises(RuntimeError):
        module.run_periodic_audit(1, 1)
    assert before == (lower.read_bytes(), len(sends))


@pytest.mark.parametrize("kind", ["audit", "json_repair", "semantic_correction"])
@pytest.mark.parametrize("state", ["absent", "planned"])
def test_proven_pre_transport_continues_same_attempt(engine, monkeypatch, kind, state):
    module, sends, _, _, path, _ = setup(engine, monkeypatch, kind)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path, kind)
    lower = journal(record)
    if state == "absent":
        lower.unlink()
    else:
        module.atomic_write_json(lower, _updated_record(read(lower), status="planned", response={}, response_sha256=""))
    count = len(sends)
    module.run_periodic_audit(1, 1)
    assert len(sends) == count + 1
    after = next(r for r in read(path)["attempt_records"] if r["kind"] == kind)
    assert after["operation_id"] == record["operation_id"] and after["attempt"] == record["attempt"]
    assert after["transport_binding"] == record["transport_binding"]


@pytest.mark.parametrize("boundary", ["response_received", "response_recorded", "chunk_completed", "audit_completed"])
def test_second_crash_after_hydration_uses_outer_response(engine, monkeypatch, boundary):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path)
    original_save = audit.atomic_write_json
    def save(target, data):
        original_save(target, data)
        r = data["attempt_records"][-1] if data.get("attempt_records") else {}
        if (boundary == r.get("lifecycle")
            or boundary == "chunk_completed" and data["chunks"][0]["status"] == "completed"
            or boundary == "audit_completed" and data["status"] == "completed"):
            raise Crash(boundary)
    monkeypatch.setattr(audit, "atomic_write_json", save)
    with pytest.raises(Crash):
        module.run_periodic_audit(1, 1)
    assert isinstance(read(path)["attempt_records"][0]["raw_content"], str)
    monkeypatch.setattr(audit, "atomic_write_json", original_save)
    monkeypatch.setattr(JournalOwnedAuditCallback, "recover", lambda *a: pytest.fail("second transport reconciliation"))
    count = len(sends)
    module.run_periodic_audit(1, 1)
    assert len(sends) == count
    assert read(path)["attempt_records"][0]["operation_id"] == record["operation_id"]


@pytest.mark.parametrize("boundary", ["prepared", "in_flight", "planned", "started", "lower_response", "before_outer_response"])
def test_process_failure_at_durable_boundaries(engine, monkeypatch, boundary):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    outer_save, lower_save = audit.atomic_write_json, module.atomic_write_json
    def save_outer(target, data):
        last = data.get("attempt_records", [])[-1:]
        if boundary == "before_outer_response" and last and last[0]["lifecycle"] == "response_received":
            raise Crash(boundary)
        outer_save(target, data)
        if last and last[0]["lifecycle"] == boundary:
            raise Crash(boundary)
    crashed = False
    def save_lower(target, data):
        nonlocal crashed
        if crashed:  # Model abrupt death: no finally/uncertainty cleanup write.
            raise Crash(boundary)
        lower_save(target, data)
        if data.get("status") == boundary or boundary == "lower_response" and data.get("status") == "response_recorded":
            crashed = True
            raise Crash(boundary)
    monkeypatch.setattr(audit, "atomic_write_json", save_outer)
    monkeypatch.setattr(module, "atomic_write_json", save_lower)
    with pytest.raises(Crash):
        module.run_periodic_audit(1, 1)
    original = read(path)["attempt_records"][0]
    monkeypatch.setattr(audit, "atomic_write_json", outer_save)
    monkeypatch.setattr(module, "atomic_write_json", lower_save)
    count = len(sends)
    if boundary == "started":
        with pytest.raises(RuntimeError, match="started"):
            module.run_periodic_audit(1, 1)
        assert len(sends) == count == 0
    else:
        module.run_periodic_audit(1, 1)
        assert len(sends) == 1
        restored = read(path)["attempt_records"][0]
        assert restored["operation_id"] == original["operation_id"] and restored["attempt"] == original["attempt"]
        assert restored["stats"]["reasoning_tokens"] == 4
        assert restored["transport_binding"]["identity"]["provider_policy"] == IDENTITY


@pytest.mark.parametrize("change", ["source", "profile", "index", "operation", "attempt", "window", "candidate", "request_body"])
def test_reconstruction_drift_blocks_before_send(engine, monkeypatch, change):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    unfinished(module, path)
    if change == "source":
        module.chapter_map[1]["text"] += "drift"
    elif change == "profile":
        monkeypatch.setattr(module, "ACTIVE_PROFILE", replace(module.ACTIVE_PROFILE, id="drift"))
    elif change == "index":
        monkeypatch.setattr(module, "compact_glossary_index", lambda **kw: "drift")
    elif change == "request_body":
        module.AUDIT_MODEL_PROMPT += "drift"
    else:
        value = read(path)
        if change == "operation":
            value["attempt_records"][0]["operation_id"] += "drift"
        elif change == "attempt":
            value["attempt_records"][0]["attempt"] += 1
        elif change == "window":
            value["audit_window"]["start_chapter"] = 2
        else:
            value["candidate_set_hash"] = "wrong-candidate-set"
        module.atomic_write_json(path, value)
    count = len(sends)
    with pytest.raises((RuntimeError, ValueError)):
        module.run_periodic_audit(1, 1)
    assert len(sends) == count


def test_impossible_multiple_unresolved_attempts_block(engine, monkeypatch):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    unfinished(module, path)
    value = read(path)
    value["attempt_records"].append(copy.deepcopy(value["attempt_records"][0]))
    module.atomic_write_json(path, value)
    with pytest.raises(RuntimeError, match="multiple unresolved"):
        module.run_periodic_audit(1, 1)
    assert len(sends) == 1


def test_legacy_unowned_inflight_absence_blocks(engine, monkeypatch):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    record = unfinished(module, path)
    journal(record).unlink()
    value = read(path)
    value["attempt_records"][0].pop("transport_binding")
    module.atomic_write_json(path, value)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        module.run_periodic_audit(1, 1)
    assert len(sends) == 1


@pytest.mark.parametrize("kind", ["json_repair", "semantic_correction"])
def test_recovered_initial_invalid_response_uses_normal_bounded_policy(engine, monkeypatch, kind):
    module, sends, _, _, path, _ = setup(engine, monkeypatch, kind)
    module.run_periodic_audit(1, 1)
    checkpoint = read(path)
    initial = checkpoint["attempt_records"][0]
    # Stop history precisely at the first paid response; the downstream bounded
    # subrequest has not been entered in this synthetic crash state.
    for record in checkpoint["attempt_records"][1:]:
        journal(record).unlink()
    checkpoint["attempt_records"] = [initial]
    checkpoint["chunks"][0].pop("semantic_correction_attempted", None)
    checkpoint["chunks"][0].pop("semantic_correction_operation_id", None)
    module.atomic_write_json(path, checkpoint)
    unfinished(module, path)
    before = len(sends)
    module.run_periodic_audit(1, 1)
    assert len(sends) == before + 1  # New bounded repair, zero initial resend.
    assert [r["kind"] for r in read(path)["attempt_records"]] == ["audit", kind]


def test_recovered_length_preserves_split_and_never_json_repairs(engine, monkeypatch):
    module, sends, client, _, path, _ = setup(engine, monkeypatch, count=2)
    original_create = client.chat.completions.create
    def truncated(**body):
        sends.append(copy.deepcopy(body))
        return response(content="{truncated", finish="length", completion=8000, reasoning=3)
    client.chat.completions.create = truncated
    original_save = audit.atomic_write_json
    def before_outer(target, data):
        if data.get("attempt_records") and data["attempt_records"][-1].get("raw_content"):
            raise Crash("before outer response")
        original_save(target, data)
    monkeypatch.setattr(audit, "atomic_write_json", before_outer)
    with pytest.raises(Crash):
        module.run_periodic_audit(1, 1)
    monkeypatch.setattr(audit, "atomic_write_json", original_save)
    def child_response(**body):
        sends.append(copy.deepcopy(body))
        english = "Alpha" if '"english": "Alpha"' in body["messages"][-1]["content"] else "Beta"
        return response(content=json.dumps({"decisions": [{"candidate_id": "c01", "action": "keep",
            "english": english, "approved_renderings": [], "merge_with": "", "notes": ""}], "issues": []}))
    client.chat.completions.create = child_response
    module.run_periodic_audit(1, 1)
    checkpoint = read(path)
    assert len(sends) == 3  # One original LENGTH; two new subdivision requests.
    assert checkpoint["attempt_records"][0]["finish_reason"] == "length"
    assert checkpoint["attempt_records"][0]["raw_content"] == "{truncated"
    assert checkpoint["chunks"][0]["status"] == "split"
    assert all(r["kind"] == "audit" for r in checkpoint["attempt_records"])


@pytest.mark.parametrize("state", ["raw", "parsed"])
def test_existing_outer_response_is_authoritative(engine, monkeypatch, state):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    reset_synthetic_publication(module)
    checkpoint = read(path)
    checkpoint["status"] = "in_progress"
    checkpoint["chunks"][0]["status"] = "response_received"
    checkpoint["chunks"][0].pop("result")
    if state == "raw":
        checkpoint["attempt_records"][0].pop("parsed_result")
        checkpoint["attempt_records"][0].pop("result_hash")
    module.atomic_write_json(path, checkpoint)
    monkeypatch.setattr(JournalOwnedAuditCallback, "recover", lambda *a: pytest.fail("outer overwritten from lower"))
    module.run_periodic_audit(1, 1)
    assert len(sends) == 1


@pytest.mark.parametrize("boundary", ["before_accounting", "after_accounting", "before_progress"])
def test_publication_prefix_keeps_recovered_accounting_once(engine, monkeypatch, boundary):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    original_account = module.add_accounting_totals_once
    original_save = module.save_json
    crashed = False
    def account(progress, operation_id, stats):
        nonlocal crashed
        if boundary == "before_accounting" and not crashed:
            crashed = True
            raise Crash(boundary)
        value = original_account(progress, operation_id, stats)
        if boundary == "after_accounting" and not crashed:
            crashed = True
            # Accounting is detached. A failure here must not durably expose
            # counters ahead of the audit's final progress marker.
            assert read(module.PROGRESS_FILE) == module._test_audit_baseline["progress"]
            raise Crash(boundary)
        return value
    def save(target, data):
        nonlocal crashed
        if boundary == "before_progress" and target == module.PROGRESS_FILE and not crashed:
            crashed = True
            raise Crash(boundary)
        original_save(target, data)
    monkeypatch.setattr(module, "add_accounting_totals_once", account)
    monkeypatch.setattr(module, "save_json", save)
    with pytest.raises(Crash):
        module.run_periodic_audit(1, 1)
    module.run_periodic_audit(1, 1)
    stats = module.audit_history["audits"][-1]["stats"]
    assert len(sends) == 1
    assert module.progress["total_prompt_tokens"] == stats["prompt_tokens"]
    assert module.progress["estimated_cost_usd"] == stats["cost_usd"]
    assert len(module.progress["accounted_api_operation_ids"]) == 1


@pytest.mark.parametrize("kind", ["audit", "json_repair", "semantic_correction"])
def test_fresh_goat_runtime_with_closed_transport_recovers_audit(engine, monkeypatch, kind):
    module, sends, _, _, path, _ = setup(engine, monkeypatch, kind)
    original_write = audit.atomic_write_json
    def before_response(target, checkpoint):
        if checkpoint.get("attempt_records") and checkpoint["attempt_records"][-1].get("kind") == kind and "raw_content" in checkpoint["attempt_records"][-1]:
            raise Crash("lower recorded; outer not persisted")
        original_write(target, checkpoint)
    monkeypatch.setattr(audit, "atomic_write_json", before_response)
    with pytest.raises(Crash):
        module.run_periodic_audit(1, 1)
    monkeypatch.setattr(audit, "atomic_write_json", original_write)
    saved = copy.deepcopy(read(path)["attempt_records"][-1])
    import run_production_engine as launcher
    from production_goat_runtime import install_goat_runtime, GoatBudget
    from production_provider_contract import PROVIDER_COMMAND_CODE
    from test_production_goat_runtime import CLOCK
    fresh = launcher.load_production_module()
    fresh.initialize_production_runtime_state(activate_provider=False, provider_backend=PROVIDER_COMMAND_CODE)
    budget = GoatBudget(1, 1.0)
    install_goat_runtime(fresh, provider_client=None, budget=budget, clock=CLOCK)
    # Same immutable synthetic audit context as the original process.
    setup((engine[0], fresh, sends, engine[3], budget), monkeypatch, kind)
    count = len(sends)
    fresh.run_periodic_audit(1, 1)
    after = next(r for r in read(path)["attempt_records"] if r["kind"] == kind)
    assert after["operation_id"] == saved["operation_id"]
    assert after["attempt"] == saved["attempt"]
    assert after["transport_binding"] == saved["transport_binding"]
    assert len(sends) == count and budget.api_calls == 0
    assert budget.actual_cost_usd == 0


@pytest.mark.parametrize("drift", ["kind", "chunk", "completed_chunk", "completed_audit"])
def test_impossible_unresolved_ownership_blocks_before_new_attempt(engine, monkeypatch, drift):
    module, sends, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    unfinished(module, path)
    checkpoint = read(path)
    if drift == "kind":
        checkpoint["attempt_records"][0]["kind"] = "unsupported"
    elif drift == "chunk":
        checkpoint["attempt_records"][0]["chunk_id"] = "other_chunk"
    elif drift == "completed_chunk":
        checkpoint["chunks"][0]["status"] = "completed"
    else:
        checkpoint["status"] = "completed"
    module.atomic_write_json(path, checkpoint)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="impossible kind/chunk/lifecycle"):
        module.run_periodic_audit(1, 1)
    assert path.read_bytes() == before and len(sends) == 1


def test_synthetic_rewind_preserves_unrelated_receipt_and_paid_evidence(engine, monkeypatch):
    from production_periodic_audit_finalization import load_intent
    from historical_reconciliation import invalidation_path
    module, _, _, _, path, _ = setup(engine, monkeypatch)
    module.run_periodic_audit(1, 1)
    receipt = invalidation_path(module, load_intent(module, 1, 1))
    unrelated = receipt.with_name("f" * 64 + ".json")
    assert unrelated != receipt
    unrelated.write_bytes(b"Unrelated disposable evidence")
    paid = journal(read(path)["attempt_records"][0])
    original_paid = paid.read_bytes()
    transactions = {p: p.read_bytes() for p in (module.RETROACTIVE_DIR / "transactions").glob("*.json")}
    unrelated_transaction = module.RETROACTIVE_DIR / "transactions" / "unrelated.json"
    unrelated_transaction.write_bytes(b"Unrelated disposable transaction")
    reset_synthetic_publication(module)
    assert not receipt.exists()
    assert unrelated.read_bytes() == b"Unrelated disposable evidence"
    assert paid.read_bytes() == original_paid
    assert transactions and all(not p.exists() for p in transactions)
    assert unrelated_transaction.read_bytes() == b"Unrelated disposable transaction"


def test_direct_audit_finalization_requires_real_caller_lease(unlocked_engine, monkeypatch):
    from production_run_lock import ProductionRunLockedError
    module, sends, *_ = setup(unlocked_engine, monkeypatch)
    project = unlocked_engine[0]
    before = {p.relative_to(project.root): p.read_bytes()
              for p in project.root.rglob("*") if p.is_file()}
    with pytest.raises(ProductionRunLockedError, match="Actual caller-held novel"):
        module.finalize_periodic_audit(1, 1, {"decisions": [], "issues": []},
            module.zero_stats(), {"candidate_count": 0, "candidate_set_hash": ""})
    assert not sends
    assert before == {p.relative_to(project.root): p.read_bytes()
                      for p in project.root.rglob("*") if p.is_file()}
