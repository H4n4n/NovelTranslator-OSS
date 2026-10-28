import importlib.util
import json
import sys
from pathlib import Path

import pytest

from glossary_audit import AuditChunkConfig
from full_pipeline_luna_runtime import install_luna_auxiliary_runtime
from full_pipeline_periodic_audit_recovery import (
    FullPipelinePeriodicAuditRecoveryError,
    _snapshot_baseline,
    install_periodic_audit_recovery,
    reconcile_project_periodic_audit_recovery,
)
from production_paid_request_journal import _with_record_hash
from production_run_lock import production_run_lock


LUNA_HELPER_PATH = Path(__file__).with_name("test_full_pipeline_luna_runtime.py")


def _helpers():
    name = "_phase60h_luna_runtime_helpers"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, LUNA_HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _runtime(tmp_path, monkeypatch, *, completed=25):
    h = _helpers()
    root, module, _initialized = h._load_initialized_engine(tmp_path, monkeypatch, final_chapter=completed)
    client = h.FakeClient()
    install_luna_auxiliary_runtime(
        module,
        client,
        now_utc=lambda: h.FIXED_TIME,
    )

    progress = _read(root / "data" / "progress.json")
    progress.update(
        {
            "completed": list(range(1, completed + 1)),
            "last_completed": completed,
            "next_chapter": completed + 1,
            "last_audited_chapter": 0,
        }
    )
    _write(root / "data" / "progress.json", progress)
    module.progress.clear()
    module.progress.update(progress)
    for number in progress["completed"]:
        (root / "output/chapters" / f"{number:04d}.md").write_text(
            f"# Chapter {number}\n\nComplete synthetic English.\n", encoding="utf-8")
    return h, root, module, client


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    result = _runtime(tmp_path, monkeypatch)
    with production_run_lock(result[1]):
        yield result


def _audit_candidate():
    return {
        "chinese": "甲",
        "english": "Alpha",
        "category": "specialized recurring term",
        "status": "provisional",
        "contexts": [{"chapter": 1, "context": "甲 appears here"}],
    }


def _run_one_chunk(module, checkpoint_path):
    candidate = _audit_candidate()

    def request_chunk(_targets, _metadata):
        response = module.call_api_with_retries(
            model=module.MODEL,
            messages=[
                {"role": "system", "content": "Periodic audit fixture."},
                {"role": "user", "content": "Audit 甲."},
            ],
            max_tokens=400,
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "stats": module.usage_and_cost(response.usage),
        }

    return module.run_chunked_audit(
        candidates=[candidate],
        all_glossary_keys=["甲"],
        start_chapter=1,
        end_chapter=25,
        checkpoint_path=checkpoint_path,
        request_chunk=request_chunk,
        repair_chunk=None,
        correct_semantic_chunk=None,
        config=AuditChunkConfig(
            max_candidates=2,
            max_serialized_chars=100_000,
        ),
        context_fingerprint="phase60h-fixture",
    )


def _set_valid_audit_response(client):
    client.completions.content = json.dumps(
        {
            "decisions": [
                {
                    "chinese": "甲",
                    "action": "keep",
                    "english": "Alpha",
                    "approved_renderings": [],
                    "merge_with": "",
                    "notes": "",
                }
            ],
            "issues": [],
        },
        ensure_ascii=False,
    )


def _erase_checkpoint_response(checkpoint_path):
    checkpoint = _read(checkpoint_path)
    checkpoint["status"] = "in_progress"
    chunk = checkpoint["chunks"][0]
    chunk["status"] = "in_flight"
    chunk.pop("result", None)
    record = checkpoint["attempt_records"][0]
    record["lifecycle"] = "in_flight"
    for field in (
        "finish_reason",
        "stats",
        "raw_content",
        "raw_content_hash",
        "parsed_result",
        "result_hash",
        "validation_errors",
    ):
        record.pop(field, None)
    _write(checkpoint_path, checkpoint)


def _audit_journal(root):
    matches = sorted(
        (root / "data" / "provider_request_journal").glob(
            "0025.periodic_audit_*.json"
        )
    )
    assert len(matches) == 1
    return matches[0]


def test_recorded_audit_paid_response_hydrates_lost_chunk_without_reissue(
    runtime,
):
    _h, root, module, client = runtime
    _snapshot_baseline(root, 1, 25)
    install_periodic_audit_recovery(module, root)
    _set_valid_audit_response(client)
    checkpoint = root / "output" / "audits" / "audit_0001_0025.checkpoint.json"

    first = _run_one_chunk(module, checkpoint)
    assert first["checkpoint"]["status"] == "completed"
    assert len(client.completions.calls) == 1
    journal_path = _audit_journal(root)
    journal = _read(journal_path)
    assert journal["status"] == "response_recorded"
    assert journal["provider"] == "openai"
    assert journal["model"] == module.MODEL

    # Simulate process death after the paid journal persisted the response but
    # before the audit chunk checkpoint recorded that response.
    _erase_checkpoint_response(checkpoint)

    resumed = _run_one_chunk(module, checkpoint)
    assert resumed["checkpoint"]["status"] == "completed"
    assert resumed["resumed"] is True
    assert len(client.completions.calls) == 1
    saved = _read(checkpoint)
    assert saved["attempt_records"][0]["paid_journal_recovered"] is True
    # Recovery does not stop at response persistence; the real audit parser and
    # validator continue locally and finish the same attempt.
    assert saved["attempt_records"][0]["lifecycle"] == "validated"


def test_uncertain_audit_paid_journal_blocks_automatic_reissue(
    runtime,
):
    _h, root, module, client = runtime
    _snapshot_baseline(root, 1, 25)
    install_periodic_audit_recovery(module, root)
    _set_valid_audit_response(client)
    checkpoint = root / "output" / "audits" / "audit_0001_0025.checkpoint.json"

    _run_one_chunk(module, checkpoint)
    assert len(client.completions.calls) == 1
    _erase_checkpoint_response(checkpoint)

    journal_path = _audit_journal(root)
    journal = _read(journal_path)
    journal.update(
        {
            "status": "uncertain",
            "response": {},
            "response_sha256": "",
            "error": {"type": "FixtureCrash", "message": "remote outcome unknown"},
        }
    )
    _write(journal_path, _with_record_hash(journal))

    with pytest.raises(
        FullPipelinePeriodicAuditRecoveryError,
        match="uncertain remote outcome",
    ):
        _run_one_chunk(module, checkpoint)
    assert len(client.completions.calls) == 1


def test_publication_recovery_restores_pre_audit_bytes_but_preserves_paid_evidence(
    runtime,
):
    _h, root, module, _client = runtime
    chapter = root / "output" / "chapters" / "0001.md"
    retro = root / "output" / "retroactive_fixes" / "before.json"
    chapter.parent.mkdir(parents=True, exist_ok=True)
    retro.parent.mkdir(parents=True, exist_ok=True)
    chapter.write_text("before chapter\n", encoding="utf-8")
    retro.write_text("before retro\n", encoding="utf-8")

    baseline_data = {
        name: (root / "data" / name).read_bytes()
        for name in (
            "glossary.json",
            "progress.json",
            "continuity.json",
            "issues.json",
            "audit_history.json",
        )
    }
    _snapshot_baseline(root, 1, 25)

    # Simulate a crash after arbitrary audit publication prefixes changed both
    # semantic/accounting state and retroactive prose, but before progress's
    # last_audited_chapter marker advanced.
    glossary = _read(root / "data" / "glossary.json")
    glossary["entries"]["甲"] = {"english": "Changed"}
    _write(root / "data" / "glossary.json", glossary)
    progress = _read(root / "data" / "progress.json")
    progress["total_prompt_tokens"] = 99999
    progress["last_audited_chapter"] = 0
    _write(root / "data" / "progress.json", progress)
    _write(
        root / "data" / "audit_history.json",
        {"audits": [{"start_chapter": 1, "end_chapter": 25}]},
    )
    chapter.write_text("partially rewritten chapter\n", encoding="utf-8")
    retro.write_text("partially rewritten retro\n", encoding="utf-8")
    (root / "output" / "retroactive_fixes" / "new.json").write_text(
        "new partial evidence\n", encoding="utf-8"
    )
    audit_record = root / "output" / "audits" / "audit_0001_0025.json"
    _write(audit_record, {"start_chapter": 1, "end_chapter": 25})
    backup = root / "backup" / "checkpoint_0025"
    backup.mkdir(parents=True)
    (backup / "partial.txt").write_text("partial\n", encoding="utf-8")

    # Provider/checkpoint evidence is deliberately outside the rollback set.
    provider_journal = root / "data" / "provider_request_journal" / "evidence.json"
    _write(provider_journal, {"preserve": True})
    audit_checkpoint = (
        root / "output" / "audits" / "audit_0001_0025.checkpoint.json"
    )
    _write(audit_checkpoint, {"preserve": True})

    actions = reconcile_project_periodic_audit_recovery(root)
    assert any(item["action"] == "restored_baseline" for item in actions)
    for name, payload in baseline_data.items():
        assert (root / "data" / name).read_bytes() == payload
    assert chapter.read_text(encoding="utf-8") == "before chapter\n"
    assert retro.read_text(encoding="utf-8") == "before retro\n"
    assert not (root / "output" / "retroactive_fixes" / "new.json").exists()
    assert not audit_record.exists()
    assert not backup.exists()
    assert _read(provider_journal) == {"preserve": True}
    assert _read(audit_checkpoint) == {"preserve": True}

    # Keep the loaded engine globals aligned with the restored disk state when
    # the full adapter is installed on restart.
    install_periodic_audit_recovery(module, root)
    assert module.progress["last_audited_chapter"] == 0
    assert module.glossary == _read(root / "data" / "glossary.json")


def test_post_marker_restart_finishes_missing_backup_without_rerunning_audit(
    runtime,
):
    _h, root, _module, _client = runtime
    _snapshot_baseline(root, 1, 25)

    audit_record = {
        "start_chapter": 1,
        "end_chapter": 25,
        "stats": {},
        "chunking": {},
        "changes": {},
        "retroactive": {},
        "issues": [],
    }
    _write(root / "output" / "audits" / "audit_0001_0025.json", audit_record)
    _write(root / "data" / "audit_history.json", {"audits": [audit_record]})
    progress = _read(root / "data" / "progress.json")
    progress["last_audited_chapter"] = 25
    _write(root / "data" / "progress.json", progress)
    backup = root / "backup" / "checkpoint_0025"
    assert not backup.exists()

    actions = reconcile_project_periodic_audit_recovery(root)
    assert any(item["action"] == "committed" for item in actions)
    assert backup.is_dir()
    for name in (
        "glossary.json",
        "progress.json",
        "continuity.json",
        "issues.json",
        "audit_history.json",
    ):
        assert (backup / name).read_bytes() == (root / "data" / name).read_bytes()



def _tree_snapshot(root):
    files = {}
    for path in root.rglob("*"):
        if path.is_file():
            info = path.stat()
            files[path.relative_to(root).as_posix()] = (
                (info.st_ino, info.st_size) if path.name == ".production.lock" else path.read_bytes(),
                info.st_mtime_ns)
    return files, {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}


def _post_marker(root):
    record = dict(start_chapter=1, end_chapter=25, stats={}, chunking={},
                  changes={}, retroactive={}, issues=[])
    _write(root / "output/audits/audit_0001_0025.json", record)
    _write(root / "data/audit_history.json", {"audits": [record]})
    progress = _read(root / "data/progress.json")
    progress["last_audited_chapter"] = 25
    _write(root / "data/progress.json", progress)


@pytest.mark.parametrize("operation", ["new_snapshot", "existing_snapshot", "restore", "post_marker", "staging"])
def test_recovery_requires_actual_caller_lock_before_mutation(tmp_path, monkeypatch, operation):
    from full_pipeline_periodic_audit_recovery import _restore_baseline
    from production_run_lock import ProductionRunLockedError
    _h, root, _module, client = _runtime(tmp_path, monkeypatch)
    if operation != "new_snapshot":
        with production_run_lock(root):
            manifest, _ = _snapshot_baseline(root, 1, 25)
        if operation == "post_marker":
            _post_marker(root)
        if operation == "staging":
            (root / "data/full_pipeline_audit_recovery/.abandoned").mkdir()
    before = _tree_snapshot(root)
    with pytest.raises(ProductionRunLockedError, match="Actual caller-held"):
        if operation in {"new_snapshot", "existing_snapshot"}:
            _snapshot_baseline(root, 1, 25)
        elif operation == "restore":
            _restore_baseline(root, manifest)
        else:
            reconcile_project_periodic_audit_recovery(root)
    assert _tree_snapshot(root) == before
    assert client.completions.calls == []


@pytest.mark.parametrize("operation,damage", [
    (operation, damage)
    for operation in ("snapshot", "restore", "post_marker")
    for damage in ("missing", "sparse", "conflict")
] + [(operation, "metadata") for operation in ("restore", "post_marker")])
def test_recovery_authoritative_source_inventory_blocks_without_mutation(runtime, operation, damage):
    _h, root, _module, client = runtime
    if operation != "snapshot":
        _snapshot_baseline(root, 1, 25)
        if operation == "post_marker":
            _post_marker(root)
    path = root / "data/chapters.json"
    library = _read(path)
    if damage == "missing":
        path.unlink()
    else:
        if damage == "sparse":
            library.pop()
        elif damage == "conflict":
            library[-1]["production_index"] = 1
        else:
            library[-1]["text"] += " Exact source drift."
        _write(path, library)
    before = _tree_snapshot(root)
    with pytest.raises(FullPipelinePeriodicAuditRecoveryError):
        if operation == "snapshot":
            _snapshot_baseline(root, 1, 25)
        else:
            reconcile_project_periodic_audit_recovery(root)
    assert _tree_snapshot(root) == before
    assert client.completions.calls == []


@pytest.mark.parametrize("damage", ["missing_binding", "changed_binding", "baseline_bytes"])
def test_legacy_rollback_requires_exact_original_snapshot_proof(runtime, damage):
    from full_pipeline_periodic_audit_recovery import _with_record_hash as recovery_hash
    _h, root, _module, client = runtime
    _snapshot_baseline(root, 1, 25)
    slot = root / "data/full_pipeline_audit_recovery/audit_0001_0025"
    manifest_path = slot / "recovery_manifest.json"
    if damage == "baseline_bytes":
        (slot / "baseline/chapters/0001.md").write_bytes(b"Contradictory baseline")
    else:
        manifest = _read(manifest_path)
        if damage == "missing_binding":
            del manifest["writer_baseline_authorization"]
        else:
            manifest["writer_baseline_authorization"]["inventory_sha256"] = "0" * 64
        _write(manifest_path, recovery_hash(manifest))
    (root / "output/chapters/0001.md").write_bytes(b"Interrupted publication")
    before = _tree_snapshot(root)
    with pytest.raises(FullPipelinePeriodicAuditRecoveryError):
        reconcile_project_periodic_audit_recovery(root)
    assert _tree_snapshot(root) == before
    assert client.completions.calls == []


def test_recovery_snapshot_reproof_cannot_authorize_an_edit(runtime):
    from historical_reconciliation import writer_module, writer_authorization
    from retroactive_transactions import build_plan, ownership_policy
    from production_failure_classification import ProductionIntegrityError
    _h, root, module, client = runtime
    context = writer_module(module.OUTPUT_DIR, module.ACTIVE_NOVEL_CONFIG.id)
    originals = {n: (module.OUTPUT_DIR / f"{n:04d}.md").read_bytes() for n in module.progress["completed"]}
    request = dict(novel_id=module.ACTIVE_NOVEL_CONFIG.id, audit_label="0001_0025", through_chapter=25,
        committed_chapters=list(originals), safe_categories=sorted(module.ACTIVE_PROFILE.retroactive_safe_categories),
        ownership_policy=ownership_policy(context.glossary),
        rules=[dict(chinese="甲", old_english="Alpha", new_english="Beta", category="character")])
    before = _tree_snapshot(root)
    with pytest.raises(ProductionIntegrityError, match="RECOVERY_BASELINE_NOT_ZERO_EDIT"):
        writer_authorization(context, build_plan(request, originals, context.chapter_map), recovery_before=originals)
    assert _tree_snapshot(root) == before
    assert client.completions.calls == []


def test_real_experiment_caller_holds_lock_during_recovery_install_and_lifecycle(tmp_path, monkeypatch):
    import full_pipeline_experiment_harness as harness
    import full_pipeline_experiment_contract_v3 as contract
    from production_run_lock import held_production_run_lock
    _h, root, module, client = _runtime(tmp_path, monkeypatch)
    with production_run_lock(root):
        _snapshot_baseline(root, 1, 25)
    _write(root / "data/issues.json", {"issues": ["partial publication"]})
    calls = []
    def locked_step(name):
        held_production_run_lock(root)
        calls.append(name)
    def load(_script):
        locked_step("load")
        return dict(module=module, module_name=module.__name__)
    def run(*, target_chapter):
        locked_step("lifecycle")
        assert target_chapter == 25
        assert module.issues_data == {"issues": []}
        assert _read(root / "data/issues.json") == {"issues": []}
    monkeypatch.setattr(harness, "load_experiment_engine_module", load)
    monkeypatch.setattr(harness, "initialize_engine_without_provider", lambda _m: locked_step("initialize"))
    monkeypatch.setattr(harness, "install_luna_batch_qa_provenance", lambda *_a: locked_step("qa"))
    monkeypatch.setattr(harness, "install_batch_acceptance_provenance", lambda *_a: locked_step("acceptance"))
    monkeypatch.setattr(module, "run_unattended_production", run)
    with contract._audit_runtime_contract():
        harness._run_real_engine_for_result(root, Path(__file__).resolve().parents[1], 25, {}, client)
    assert calls == ["load", "initialize", "qa", "acceptance", "lifecycle"]
    assert client.completions.calls == []
    with pytest.raises(RuntimeError, match="Actual caller-held"):
        held_production_run_lock(root)



def test_recovery_snapshot_binds_full_inventory_beyond_due_audit_prefix(tmp_path, monkeypatch):
    _h, root, _module, client = _runtime(tmp_path, monkeypatch, completed=26)
    with production_run_lock(root):
        _snapshot_baseline(root, 1, 25)
        originals = {p.name: p.read_bytes() for p in (root / "output/chapters").glob("*.md")}
        for name in ("0001.md", "0026.md"):
            (root / "output/chapters" / name).write_bytes(b"Interrupted publication")
        assert reconcile_project_periodic_audit_recovery(root)[0]["action"] == "restored_baseline"
        assert {p.name: p.read_bytes() for p in (root / "output/chapters").glob("*.md")} == originals
    assert client.completions.calls == []



def test_invalid_inventory_blocks_before_abandoned_snapshot_cleanup(runtime):
    _h, root, _module, client = runtime
    _snapshot_baseline(root, 1, 25)
    (root / "data/full_pipeline_audit_recovery/.abandoned").mkdir()
    (root / "data/chapters.json").unlink()
    before = _tree_snapshot(root)
    with pytest.raises(FullPipelinePeriodicAuditRecoveryError):
        reconcile_project_periodic_audit_recovery(root)
    assert _tree_snapshot(root) == before
    assert client.completions.calls == []
