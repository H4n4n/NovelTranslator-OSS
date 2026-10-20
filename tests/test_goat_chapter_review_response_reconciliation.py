"""Same-operation GOAT chapter-review replay, without changing correction budgets."""
import copy
import json
from pathlib import Path

import pytest
import chapter_glossary_review as review
from production_paid_request_journal import _updated_record
from test_production_goat_runtime import engine, zero_network, response
from test_goat_glossary_review_recovery import configure, STATE, VALID
from test_auxiliary_response_reconciliation import Crash, read


PAID_ATTEMPT_KINDS = ["original", "schema_correction", "semantic_correction", "semantic_correction_retry"]


@pytest.mark.parametrize("kind", PAID_ATTEMPT_KINDS)
@pytest.mark.parametrize("boundary", ["prepared", "in_flight"])
def test_bound_pre_transport_crash_resumes_exact_attempt(engine, monkeypatch, kind, boundary):
    _, module, sends, _ = configure(engine, answers(kind))
    path = engine[0].pending_dir / "0001.json"
    original_persist = review._persist
    snapshots = []
    target_status = "original_started" if kind == "original" else kind + "_in_flight"

    def crash_at_boundary(record, persist):
        item = record.get(kind)
        if isinstance(item, dict) and item.get("attempted") and "result" not in item:
            # Every durable unfinished NEW attempt, including the old dangerous
            # in-flight write, must already own its exact transport slot.
            assert "transport_binding" in item
            assert item.get("transport_ownership_version") == "bound-before-transport-v1"
        original_persist(record, persist)
        if record.get("status") in {kind + "_prepared", target_status}:
            snapshots.append(copy.deepcopy(read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][kind]))
            assert not Path(item["transport_binding"]["journal_path"]).exists()
            if item["lifecycle"] == boundary:
                raise Crash("bound outer checkpoint before lower journal creation")

    monkeypatch.setattr(review, "_persist", crash_at_boundary)
    with pytest.raises(Crash):
        module.process_chapter(1)
    attempt = snapshots[-1]
    assert snapshots[0]["lifecycle"] == "prepared"
    if boundary == "in_flight":
        assert len(snapshots) == 2
        assert snapshots[0]["transport_binding"] == attempt["transport_binding"]
    monkeypatch.setattr(review, "_persist", original_persist)
    before = len(sends)
    resumed = []

    def observe(record, persist):
        if record.get("status") == kind + "_recorded":
            resumed.append(copy.deepcopy(record[kind]))
        original_persist(record, persist)

    monkeypatch.setattr(review, "_persist", observe)
    assert module.process_chapter(1) is True
    assert len(sends) == before + 1
    assert resumed[0]["operation_id"] == attempt["operation_id"]
    assert resumed[0].get("attempt", 1) == attempt.get("attempt", 1)
    assert resumed[0]["transport_binding"] == attempt["transport_binding"]
    lower = read(Path(attempt["transport_binding"]["journal_path"]))
    assert lower["status"] == "response_recorded"
    assert all(lower[field] == value for field, value in attempt["transport_binding"]["identity"].items())
    records = [read(p) for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")]
    assert module.progress["total_prompt_tokens"] == sum(r["response"]["stats"]["prompt_tokens"] for r in records)
    assert module.progress["rate_correct_cost_usd"] == pytest.approx(sum(r["response"]["stats"]["cost_usd"] for r in records))


@pytest.mark.parametrize("kind", PAID_ATTEMPT_KINDS)
@pytest.mark.parametrize("boundary", ["before_prepare", "after_prepare"])
def test_no_ambiguous_unbound_checkpoint_before_binding(engine, monkeypatch, kind, boundary):
    from auxiliary_response_reconciliation import JournalOwnedAuxiliaryCallback
    _, module, sends, _ = configure(engine, answers(kind))
    prepare = JournalOwnedAuxiliaryCallback.prepare

    def interrupt_prepare(callback, item, args):
        metadata = args[-1]
        request_kind = ("original" if len(args) == 2 else
            "schema_correction" if metadata["correction_class"] == "model_schema" else
            "semantic_correction_retry" if metadata["correction_attempt"] == 2 else "semantic_correction")
        if request_kind == kind and boundary == "before_prepare":
            raise Crash("before exact binding construction")
        binding = prepare(callback, item, args)
        if request_kind == kind:
            assert not Path(binding["journal_path"]).exists()
            raise Crash("binding constructed but not yet persisted")
        return binding

    monkeypatch.setattr(JournalOwnedAuxiliaryCallback, "prepare", interrupt_prepare)
    with pytest.raises(Crash):
        module.process_chapter(1)
    path = engine[0].pending_dir / "0001.json"
    saved = read(path).get(review.CHAPTER_REVIEW_RECOVERY_KEY, {}).get(kind)
    assert saved is None  # No attempted/in-flight marker escaped before binding.
    before = len(sends)
    monkeypatch.setattr(JournalOwnedAuxiliaryCallback, "prepare", prepare)
    assert module.process_chapter(1) is True
    assert len(sends) == before + 1


@pytest.mark.parametrize("kind", PAID_ATTEMPT_KINDS)
def test_legacy_unbound_unfinished_attempt_still_blocks(engine, monkeypatch, kind):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    value = read(path)
    legacy = value[review.CHAPTER_REVIEW_RECOVERY_KEY][key]
    legacy.pop("transport_binding")
    legacy.pop("transport_ownership_version", None)
    module.atomic_write_json(path, value)
    count = len(sends)
    assert module.process_chapter(1) is False
    assert len(sends) == count
    saved = read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key]
    assert "transport_binding" not in saved
    assert saved["operation_id"] == attempt["operation_id"]
    assert saved.get("attempt", 1) == attempt.get("attempt", 1)


@pytest.mark.parametrize("kind", PAID_ATTEMPT_KINDS)
@pytest.mark.parametrize("status", ["planned", "started", "uncertain"])
def test_crash_at_first_lower_writes_preserves_bound_attempt(engine, monkeypatch, kind, status):
    project, module, sends, _ = configure(engine, answers(kind))
    path = project.pending_dir / "0001.json"
    save = module.atomic_write_json

    def interrupt_lower(lower_path, value):
        save(lower_path, value)
        attempt = read(path).get(review.CHAPTER_REVIEW_RECOVERY_KEY, {}).get(kind) if path.exists() else None
        if (attempt and "transport_binding" in attempt
                and Path(lower_path) == Path(attempt["transport_binding"]["journal_path"])
                and value.get("status") == ("started" if status == "uncertain" else status)):
            assert attempt["lifecycle"] == "in_flight"
            assert attempt["transport_ownership_version"] == "bound-before-transport-v1"
            if status == "uncertain":
                # Simulate durable uncertain evidence; regardless of whether
                # the remote side received anything, automatic send is forbidden.
                save(lower_path, _updated_record(value, status="uncertain"))
            raise Crash("lower state persisted before physical transport")

    monkeypatch.setattr(module, "atomic_write_json", interrupt_lower)
    with pytest.raises(Crash):
        module.process_chapter(1)
    monkeypatch.setattr(module, "atomic_write_json", save)
    attempt = read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][kind]
    lower_path = Path(attempt["transport_binding"]["journal_path"])
    lower_before = lower_path.read_bytes()
    count = len(sends)
    if status == "planned":
        assert module.process_chapter(1) is True
        assert len(sends) == count + 1
        lower = read(lower_path)
        assert all(lower[field] == value for field, value in attempt["transport_binding"]["identity"].items())
    else:
        assert module.process_chapter(1) is False
        assert len(sends) == count
        assert lower_path.read_bytes() == lower_before
        assert read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][kind] == attempt


def answers(kind):
    decision = {"candidate_id": "c01", "action": "accept", "english": "Jia", "category": "character"}
    initial = {"decisions": [decision], "issues": []}
    requests = []
    if kind == "json_repair":
        requests = [response(content="{broken paid review"), response(content=VALID)]
    elif kind == "schema_correction":
        initial["issues"] = "unsafe scalar"
        requests = [response(content=json.dumps(initial)), response(content=json.dumps({"decisions": [decision], "issues": []}))]
    elif kind in {"semantic_correction", "semantic_correction_retry"}:
        decision.update(action="contextual", approved_renderings=[])
        requests = [response(content=json.dumps(initial))]
        if kind.endswith("retry"):
            requests.append(response(content='{"decisions":[{"candidate_id":"c01","approved_renderings":[""]}]}'))
        requests.append(response(content='{"decisions":[{"candidate_id":"c01","approved_renderings":["Jia"]}]}'))
    else:
        requests = [response(content=VALID)]
    return [response(content="Jia entered the hall."), response(content=STATE), *requests]


def interrupt_outer(engine, monkeypatch, kind):
    project, module, sends, budget = configure(engine, answers(kind))
    key = "original" if kind in {"original", "json_repair"} else kind
    original_persist = review._persist
    def before_response(record, persist):
        if record.get("status") == key + "_recorded":
            raise Crash("lower returned before outer checkpoint")
        original_persist(record, persist)
    monkeypatch.setattr(review, "_persist", before_response)
    with pytest.raises(Crash):
        module.process_chapter(1)
    monkeypatch.setattr(review, "_persist", original_persist)
    path = project.pending_dir / "0001.json"
    attempt = copy.deepcopy(read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key])
    assert "result" not in attempt
    return project, module, sends, budget, path, key, attempt


@pytest.mark.parametrize("kind", ["original", "json_repair", "schema_correction", "semantic_correction", "semantic_correction_retry"])
def test_exact_lower_response_replays_same_chapter_review_operation(engine, monkeypatch, kind):
    project, module, sends, budget, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    lower = {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    count = len(sends)
    engine[3].chat.completions.create = lambda **kw: pytest.fail("paid review replay sent")
    assert module.process_chapter(1) is True
    assert len(sends) == budget.api_calls == count
    assert not path.exists()  # Full normal local chapter commit.
    assert lower == {p.name: p.read_bytes() for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")}
    records = [read(p) for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")]
    assert module.progress["total_prompt_tokens"] == sum(r["response"]["stats"]["prompt_tokens"] for r in records)
    assert module.progress["total_completion_tokens"] == sum(r["response"]["stats"]["completion_tokens"] for r in records)
    assert module.progress["rate_correct_cost_usd"] == pytest.approx(sum(r["response"]["stats"]["cost_usd"] for r in records))
    assert any(r.get("auxiliary_domain_binding", {}).get("operation_id") == attempt["operation_id"] for r in records)


@pytest.mark.parametrize("kind", ["original", "schema_correction", "semantic_correction", "semantic_correction_retry"])
@pytest.mark.parametrize("status", ["started", "uncertain", "planned"])
def test_chapter_review_transport_state_is_authoritative(engine, monkeypatch, kind, status):
    project, module, sends, budget, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    from pathlib import Path
    lower = Path(attempt["transport_binding"]["journal_path"])
    module.atomic_write_json(lower, _updated_record(read(lower), status=status, response={}, response_sha256=""))
    before = len(sends)
    before_bytes = lower.read_bytes()
    if status in {"started", "uncertain"}:
        assert module.process_chapter(1) is False
        assert len(sends) == before
        assert lower.read_bytes() == before_bytes
        assert read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key] == attempt
    else:
        engine[3].chat.completions.create = lambda **kw: (sends.append(kw) or answers(kind)[-1])
        assert module.process_chapter(1) is True
        assert len(sends) == before + 1
        assert read(lower)["auxiliary_domain_binding"]["operation_id"] == attempt["operation_id"]


@pytest.mark.parametrize("kind", ["original", "json_repair", "schema_correction", "semantic_correction", "semantic_correction_retry"])
def test_second_chapter_review_crash_uses_durable_outer_result(engine, monkeypatch, kind):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    original_persist = review._persist
    def after_response(record, persist):
        original_persist(record, persist)
        if record.get("status") == key + "_recorded":
            raise Crash("hydrated outer response persisted")
    monkeypatch.setattr(review, "_persist", after_response)
    with pytest.raises(Crash):
        module.process_chapter(1)
    assert read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key]["operation_id"] == attempt["operation_id"]
    assert "result" in read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key]
    monkeypatch.setattr(review, "_persist", original_persist)
    count = len(sends)
    engine[3].chat.completions.create = lambda **kw: pytest.fail("second restart sent")
    assert module.process_chapter(1) is True
    assert len(sends) == count


@pytest.mark.parametrize("kind", ["original", "json_repair", "schema_correction", "semantic_correction", "semantic_correction_retry"])
def test_fresh_runtime_closed_transport_replays_paid_chapter_review(engine, monkeypatch, kind):
    project, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    import run_production_engine as launcher
    from production_goat_runtime import install_goat_runtime, GoatBudget
    from production_provider_contract import PROVIDER_COMMAND_CODE
    from test_production_goat_runtime import CLOCK
    fresh = launcher.load_production_module()
    fresh.initialize_production_runtime_state(activate_provider=False, provider_backend=PROVIDER_COMMAND_CODE)
    fresh.local_qa = lambda *a, **kw: {"commit_allowed": True, "issues": []}
    budget = GoatBudget(1, 1.0)
    install_goat_runtime(fresh, provider_client=None, budget=budget, clock=CLOCK)
    count = len(sends)
    assert fresh.process_chapter(1) is True
    assert budget.api_calls == 0 and budget.actual_cost_usd == 0
    assert len(sends) == count and not path.exists()


@pytest.mark.parametrize("kind", ["original", "json_repair", "schema_correction", "semantic_correction"])
@pytest.mark.parametrize("drift", ["request", "repair_policy", "operation", "provider"])
def test_chapter_review_reconstruction_drift_fails_closed(engine, monkeypatch, kind, drift):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    from pathlib import Path
    lower = Path(attempt["transport_binding"]["journal_path"])
    if drift == "request":
        module.TERM_REVIEW_SCHEMA += "changed schema"
    elif drift == "repair_policy":
        module.GENERIC_JSON_REPAIR_PROMPT += "changed repair prompt"
    elif drift == "operation":
        meta = read(path)
        meta[review.CHAPTER_REVIEW_RECOVERY_KEY][key]["operation_id"] += "wrong attempt"
        module.atomic_write_json(path, meta)
    else:
        module.atomic_write_json(lower, _updated_record(read(lower), provider="wrong provider"))
    count = len(sends)
    assert module.process_chapter(1) is False
    assert len(sends) == count


def test_identical_wire_cannot_satisfy_a_different_correction_attempt(engine, monkeypatch):
    # The existing context slot is wire-addressed. If two distinct domain
    # operations reconstruct identical wire bytes, preserve the old slot and
    # block, rather than borrowing attempt 1's response or inventing a slot.
    raw = {"decisions": [{"candidate_id": "c01", "action": "contextual", "english": "Jia",
        "category": "character", "approved_renderings": []}], "issues": []}
    _, module, sends, _ = configure(engine, [response(content="Jia entered."), response(content=STATE),
        response(content=json.dumps(raw)), response(content='{"decisions":[{"candidate_id":"c01"}]}')])
    assert module.process_chapter(1) is False
    assert len(sends) == 4
    assert module.process_chapter(1) is False
    assert len(sends) == 4


@pytest.mark.parametrize("kind", ["original", "schema_correction", "semantic_correction", "semantic_correction_retry"])
def test_missing_chapter_review_lower_proof_blocks(engine, monkeypatch, kind):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, kind)
    from pathlib import Path
    Path(attempt["transport_binding"]["journal_path"]).unlink()
    # A blocked-head/historical binding did not prove pre-transport ordering.
    value = read(path)
    attempt.pop("transport_ownership_version", None)
    value[review.CHAPTER_REVIEW_RECOVERY_KEY][key] = attempt
    module.atomic_write_json(path, value)
    count = len(sends)
    assert module.process_chapter(1) is False
    assert len(sends) == count
    assert read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key] == attempt


@pytest.mark.parametrize("status", ["started", "uncertain", "planned"])
def test_nested_chapter_review_json_repair_transport_state(engine, monkeypatch, status):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, "json_repair")
    lower = next(p for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")
        if p.name.startswith("0001.context_glossary_review_"))
    module.atomic_write_json(lower, _updated_record(read(lower), status=status, response={}, response_sha256=""))
    before = lower.read_bytes()
    count = len(sends)
    if status == "planned":
        engine[3].chat.completions.create = lambda **kw: (sends.append(kw) or response(content=VALID))
        assert module.process_chapter(1) is True
        assert len(sends) == count + 1
    else:
        assert module.process_chapter(1) is False
        assert lower.read_bytes() == before
        assert len(sends) == count
        assert read(path)[review.CHAPTER_REVIEW_RECOVERY_KEY][key] == attempt


def test_multiple_unfinished_chapter_review_requests_block(engine, monkeypatch):
    _, module, sends, _, path, key, attempt = interrupt_outer(engine, monkeypatch, "original")
    value = read(path)
    value[review.CHAPTER_REVIEW_RECOVERY_KEY]["semantic_correction"] = copy.deepcopy(attempt)
    module.atomic_write_json(path, value)
    count = len(sends)
    assert module.process_chapter(1) is False
    assert len(sends) == count
