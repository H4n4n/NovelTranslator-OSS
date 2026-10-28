"""Independent accepted-audit proof and crash replay; synthetic transport only."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import production_periodic_audit_finalization as final
import retroactive_transactions as tx
from goat_periodic_audit_finalize_plan import checkpoint_proof
from production_paid_request_journal import _with_record_hash, _sha256_json
from production_run_lock import production_run_lock
from noveltranslator_core import atomic_write_json
from test_experimental_goat import (MODEL, LIMITS, STATE, ENGLISH, FakeClient, inputs, response,
    build_plan, create_s0, probe, runtime_for, real_engine, authorization_token, SOURCE)
from experimental_goat_experiment import require_authorization
from experimental_goat_policy import digest
from test_production_goat_runtime import engine as native_engine, zero_network
from test_auxiliary_response_reconciliation import setup as native_audit_setup


class BeforePublication(BaseException):
    pass


class Crash(BaseException):
    pass


def tree(root):
    # Windows refuses an independent read of the caller's locked rendezvous.
    # Preserve every other path, including all transaction and budget evidence.
    rendezvous = root / "tracks/ace/.production.lock"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p != rendezvous}


@contextmanager
def paid_audit(tmp_path, mode="json_repair"):
    production = tmp_path / "production"
    track = inputs(production)
    config_path = production / "novels/ace/novel.json"
    config = json.loads(config_path.read_bytes()); config["audit_interval"] = 1
    atomic_write_json(config_path, config)
    if mode == "split":
        source_path = production / "novels/ace/source/novel.txt"
        source_path.write_text(SOURCE + "王林也在。", encoding="utf-8")
        chapters = production / "novels/ace/data/chapters.json"
        rows = json.loads(chapters.read_bytes()); rows[0]["text"] = SOURCE + "王林也在。"
        atomic_write_json(chapters, rows)
    plan = build_plan(production_root=production, output_root=tmp_path / "experiment",
        config={"model": MODEL.to_dict(), "limits": LIMITS, "tracks": [track]})
    root = create_s0(plan)
    probe(root, plan)
    require_authorization(plan, "run", authorization_token(plan, "run"), True)
    state = {**STATE, "new_terms": [{"chinese": "李明", "english": "Li Ming",
                                    "category": "character", "status": "provisional"}]}
    review = {"decisions": [{"candidate_id": "c01", "action": "accept", "english": "Li Ming",
                            "category": "character", "notes": ""}], "issues": []}
    valid = {"decisions": [{"candidate_id": "c01", "action": "keep", "english": "Li Ming",
                           "category": "character", "notes": ""}], "issues": []}
    bad = copy.deepcopy(valid); bad["decisions"][0]["action"] = ""
    if mode == "split":
        state["new_terms"].append({"chinese": "王林", "english": "Wang Lin", "category": "character", "status": "provisional"})
        review["decisions"].append({"candidate_id": "c02", "action": "accept", "english": "Wang Lin", "category": "character", "notes": ""})
    english = ENGLISH + (" Wang Lin stood nearby." if mode == "split" else "")
    replies = [response(english), response(json.dumps(state)), response(json.dumps(review))]
    if mode == "split":
        child = json.dumps({"decisions": [{"candidate_id": "c01", "action": "keep"}], "issues": []})
        replies += [response("{truncated", finish="length"), response(child), response(child)]
    elif mode == "json_repair": replies += [response('{"decisions":broken}'), response(json.dumps(valid))]
    elif mode == "semantic": replies += [response(json.dumps(bad)), response('{"decisions":[{"candidate_id":"c01","action":"keep"}]}')]
    else: replies += [response(json.dumps(valid))]
    client = FakeClient(lambda body: replies.pop(0))
    runtime = runtime_for(plan, client)
    track_root = root / "tracks/ace"
    with production_run_lock(track_root):
        with real_engine(plan, track_root, runtime) as module:
            assert module.process_chapter(1)
            capture = []
            original = module.finalize_periodic_audit
            def stop(*args):
                capture.append(args); raise BeforePublication()
            module.finalize_periodic_audit = stop
            try:
                with pytest.raises(BeforePublication): module.run_periodic_audit(1, 1)
            finally:
                module.finalize_periodic_audit = original
            expected_sends = {"plain": 4, "split": 6}.get(mode, 5)
            assert len(client.calls) == expected_sends
            client.handler = lambda body: pytest.fail("unauthorized repeat experimental send")
            path = module.periodic_audit_checkpoint_path(1, 1)
            if not path.is_absolute(): path = track_root / path
            checkpoint = json.loads(path.read_bytes())
            operation = checkpoint["attempt_records"][0]["kind"]
            assert operation == "audit"
            journal = next((track_root / "data/provider_request_journal").glob("0001.periodic_audit.audit_*.json"))
            yield NS(root=root, track_root=track_root, plan=plan, runtime=runtime, module=module,
                     client=client, args=capture[0], checkpoint=checkpoint, path=path, journal=journal,
                     expected_sends=expected_sends)


@pytest.mark.parametrize("mode", ["plain", "json_repair", "semantic", "split"])
def test_accepted_experimental_audit_is_reproved_and_committed_once(tmp_path, mode):
    with paid_audit(tmp_path, mode) as c:
        before = tree(c.root)
        proof = checkpoint_proof(c.module, 1, 1)
        assert proof["audit_result"] == c.args[2] and proof["audit_stats"] == c.args[3]
        assert before == tree(c.root)
        paid = {p: p.read_bytes() for p in (c.track_root / "data/provider_request_journal").glob("*")}
        budget = (c.root / "budget.json").read_bytes()
        assert final.finalize_periodic_audit(c.module, *c.args)["status"] == "FINALIZED"
        progress = json.loads((c.track_root / "data/progress.json").read_bytes())
        assert progress["last_audited_chapter"] == 1
        assert len(progress["accounted_api_operation_ids"]) == len(set(progress["accounted_api_operation_ids"]))
        before = tree(c.root)
        assert final.finalize_periodic_audit(c.module, *c.args)["status"] == "ALREADY_FINALIZED"
        assert tree(c.root) == before
        assert (c.root / "budget.json").read_bytes() == budget
        assert all(p.read_bytes() == raw for p, raw in paid.items())
        assert len(c.client.calls) == c.expected_sends


@pytest.mark.parametrize("damage", ["model", "provider", "runtime_missing", "recorded_policy", "journal",
    "missing_response", "missing_journal", "started", "uncertain", "wrong_operation_id", "cross_provider",
    "raw_response_rehashed", "parsed_result", "accounting", "chunk_identity", "metadata", "duplicate", "order",
    "budget_settlement", "plan_policy"])
def test_experimental_audit_refuses_foreign_or_damaged_paid_evidence(tmp_path, damage):
    with paid_audit(tmp_path) as c:
        baseline = tree(c.root)
        assert checkpoint_proof(c.module, 1, 1)["audit_result"] == c.args[2]
        assert tree(c.root) == baseline
        journal = json.loads(c.journal.read_bytes())
        checkpoint = copy.deepcopy(c.checkpoint)
        if damage == "model": c.runtime.model = replace(MODEL, provider_model="another/model")
        elif damage == "provider": c.module.progress["provider"] = "direct_deepseek"
        elif damage == "runtime_missing": c.module._experimental_runtime = None
        elif damage == "missing_journal": c.journal.unlink()
        elif damage in {"recorded_policy", "journal", "missing_response", "started", "uncertain", "wrong_operation_id", "cross_provider", "raw_response_rehashed"}:
            if damage == "recorded_policy": journal["model_policy_sha256"] = "f" * 64
            elif damage == "journal": journal["response"]["usage"]["prompt_tokens"] += 1
            elif damage == "missing_response": journal.pop("response")
            elif damage in {"started", "uncertain"}: journal.update(status=damage, response={}, response_sha256="")
            elif damage == "wrong_operation_id": journal["operation_id"] = "f" * 64
            elif damage == "cross_provider":
                journal.update(provider="direct_deepseek", model="deepseek-v4-pro", provider_policy={"provider": "direct_deepseek"})
            else:
                journal["response"]["choices"][0]["content"] = "changed accepted response"
                journal["response_sha256"] = _sha256_json(journal["response"])
            atomic_write_json(c.journal, _with_record_hash(journal))
        elif damage in {"budget_settlement", "plan_policy"}:
            path = c.root / ("budget.json" if damage == "budget_settlement" else "plan.json")
            value = json.loads(path.read_bytes())
            if damage == "budget_settlement":
                value["records"][journal["operation_id"]]["actual_ce"] = "0"
                value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
            else:
                value["model"]["provider_model"] = "another/model"
                value["plan_sha256"] = digest({k: v for k, v in value.items() if k != "plan_sha256"})
            atomic_write_json(path, value)
        else:
            record = checkpoint["attempt_records"][0]
            if damage == "parsed_result":
                checkpoint["attempt_records"][1]["parsed_result"]["issues"] = ["forged"]
                checkpoint["attempt_records"][1]["result_hash"] = digest(checkpoint["attempt_records"][1]["parsed_result"])
            elif damage == "accounting": record["stats"]["cost_credit_equivalent"] = "0"
            elif damage == "chunk_identity": record["chunk_id"] = "foreign"
            elif damage == "metadata": record["request_metadata"]["chunk_id"] = "foreign"
            elif damage == "duplicate": checkpoint["attempt_records"].append(copy.deepcopy(record))
            elif damage == "order": checkpoint["attempt_records"].reverse()
            atomic_write_json(c.path, checkpoint)
        before = tree(c.root)
        with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
            final.finalize_periodic_audit(c.module, *c.args)
        assert tree(c.root) == before
        assert len(c.client.calls) == 5


@pytest.mark.parametrize("boundary", ["intent", "plan", "audit", "progress"])
def test_experimental_audit_restart_finishes_only_authorized_local_tail(tmp_path, monkeypatch, boundary):
    with paid_audit(tmp_path) as c:
        before_budget = (c.root / "budget.json").read_bytes()
        paid = {p: p.read_bytes() for p in (c.track_root / "data/provider_request_journal").glob("*")}
        fired = []
        def wrap(original):
            def save(path, value):
                original(path, value)
                p = Path(path)
                hit = ((boundary == "intent" and p.name.endswith(".finalization.json"))
                    or (boundary == "plan" and p.name.endswith(".plan.json"))
                    or (boundary == "audit" and p.name == "audit_0001_0001.json")
                    or (boundary == "progress" and p.name == "progress.json"))
                if hit and not fired: fired.append(True); raise Crash(boundary)
            return save
        with monkeypatch.context() as mp:
            mp.setattr(final, "atomic_write_json", wrap(final.atomic_write_json))
            mp.setattr(tx, "atomic_write_json", wrap(tx.atomic_write_json))
            mp.setattr(c.module, "save_json", wrap(c.module.save_json))
            with pytest.raises(Crash): final.finalize_periodic_audit(c.module, *c.args)
        assert fired
    # A fresh runtime still has to match the original immutable plan and journals.
    runtime = runtime_for(c.plan, c.client)
    with production_run_lock(c.track_root):
        with real_engine(c.plan, c.track_root, runtime) as module:
            report = final.finalize_periodic_audit(module, *c.args)
            assert report["status"] in {"FINALIZED", "ALREADY_FINALIZED"}
            before = tree(c.root)
            assert final.finalize_periodic_audit(module, *c.args)["status"] == "ALREADY_FINALIZED"
            assert before == tree(c.root)
    assert (c.root / "budget.json").read_bytes() == before_budget
    assert all(p.read_bytes() == raw for p, raw in paid.items())
    assert len(c.client.calls) == 5


@pytest.mark.parametrize("damage", ["missing_parent", "authorized_fields", "merge_proof", "effective_hash"])
def test_experimental_semantic_correction_reproves_original_authorization(tmp_path, damage):
    with paid_audit(tmp_path, "semantic") as c:
        assert checkpoint_proof(c.module, 1, 1)["audit_result"] == c.args[2]
        checkpoint = copy.deepcopy(c.checkpoint)
        correction = checkpoint["attempt_records"][-1]
        assert correction["kind"] == "semantic_correction"
        if damage == "missing_parent": checkpoint["attempt_records"].pop(0)
        elif damage == "authorized_fields": correction["request_metadata"]["authorized_fields"] = {"c01": ["english"]}
        elif damage == "merge_proof": correction["merge_audit"] = {}
        else: correction["effective_result_hash"] = "f" * 64
        atomic_write_json(c.path, checkpoint)
        before = tree(c.root)
        with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
            final.finalize_periodic_audit(c.module, *c.args)
        assert tree(c.root) == before and len(c.client.calls) == 5


def test_real_native_and_experimental_paid_journals_are_never_interchangeable(native_engine, tmp_path, monkeypatch):
    project, native, sends, *_ = native_engine
    # Match the read-only planner: observability is disabled, proof gates stay real.
    monkeypatch.setattr(native, "RUN_LOGGER", NS(emit=lambda *a, **k: None))
    native_audit_setup(native_engine, monkeypatch)
    with production_run_lock(project.root):
        native.run_periodic_audit(1, 1)
        assert checkpoint_proof(native, 1, 1)["audit_result"]["decisions"]
    checkpoint = json.loads(native.periodic_audit_checkpoint_path(1, 1).read_bytes())
    native_path = Path(checkpoint["attempt_records"][0]["transport_binding"]["journal_path"]).resolve()
    native_bytes = native_path.read_bytes()
    with paid_audit(tmp_path / "separate_experiment") as c:
        assert checkpoint_proof(c.module, 1, 1)["audit_result"] == c.args[2]
        experimental_bytes = c.journal.read_bytes()
        c.journal.write_bytes(native_bytes)
        before = tree(c.root)
        with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
            final.finalize_periodic_audit(c.module, *c.args)
        assert tree(c.root) == before and len(c.client.calls) == 5
        c.journal.write_bytes(experimental_bytes)
    with production_run_lock(project.root):
        native_path.write_bytes(experimental_bytes)
        def snapshot():
            return {p.relative_to(project.root): p.read_bytes() for p in project.root.rglob("*")
                    if p.is_file() and p != project.root / ".production.lock"}
        before = snapshot()
        with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
            checkpoint_proof(native, 1, 1)
        assert before == snapshot() and len(sends) == 1
        native_path.write_bytes(native_bytes)


@pytest.mark.parametrize("damage", ["journal", "changed_model"])
def test_interrupted_experimental_intent_never_grants_stale_paid_proof(tmp_path, monkeypatch, damage):
    with paid_audit(tmp_path) as c:
        original = final.atomic_write_json
        def crash_after_intent(path, value):
            original(path, value)
            if Path(path).name.endswith(".finalization.json"):
                raise Crash("intent")
        with monkeypatch.context() as mp:
            mp.setattr(final, "atomic_write_json", crash_after_intent)
            with pytest.raises(Crash): final.finalize_periodic_audit(c.module, *c.args)
    if damage == "journal":
        journal = json.loads(c.journal.read_bytes())
        journal["response"]["usage"]["prompt_tokens"] += 1
        journal["response_sha256"] = _sha256_json(journal["response"])
        atomic_write_json(c.journal, _with_record_hash(journal))
    runtime = runtime_for(c.plan, c.client)
    if damage == "changed_model": runtime.model = replace(MODEL, provider_model="another/model")
    with production_run_lock(c.track_root):
        before = tree(c.root)
        with pytest.raises((RuntimeError, ValueError, KeyError, OSError)):
            with real_engine(c.plan, c.track_root, runtime) as module:
                final.finalize_periodic_audit(module, *c.args)
        assert before == tree(c.root) and len(c.client.calls) == 5
