import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import full_pipeline_experiment_contract_v3 as v3
import full_pipeline_experiment_harness as harness
import full_pipeline_periodic_audit_recovery as audit_recovery
import glossary_audit
from full_pipeline_batch_wave import SUBMIT_AUTHORIZATION
from production_run_lock import production_run_lock


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")


def _helpers():
    name = "_phase60h_v3_harness_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _make_v3(tmp_path, *, target_chapters=25):
    h = _helpers()
    repo = h._synthetic_repo(tmp_path, only_zh=True)
    experiment = tmp_path / "experiment_v3"
    manifest = v3.create_experiment(
        experiment,
        repo,
        repo_anchor="synthetic-phase60h-anchor",
        novel_ids=("great_king_zhuxia",),
        target_chapters=target_chapters,
        experiment_id="phase60h-contract-test",
    )
    return repo, experiment, manifest


def _collect_v3(experiment, repo, wave_number):
    h = _helpers()
    wave_manifest, target = v3.prepare_experiment_wave(
        experiment,
        repo,
        wave_number,
    )
    client = h.BatchClient(f"v3-wave-{wave_number}")
    submitted = v3.submit_experiment_wave(
        experiment,
        repo,
        wave_number,
        client,
        SUBMIT_AUTHORIZATION,
    )
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = f"file-output-v3-{wave_number}"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(
            h._batch_row(
                item["custom_id"],
                h._translation(item["novel_id"], wave_number),
            ),
            ensure_ascii=False,
        )
        for item in reversed(submitted["requests"])
    ) + "\n"
    document, collected = v3.collect_experiment_wave(
        experiment,
        repo,
        wave_number,
        client,
    )
    assert document["status"] == "complete"
    assert collected["status"] == "collected"
    assert collected["run_id"] == wave_manifest["run_id"]
    return document


def _process_v3(experiment, repo, wave_number):
    h = _helpers()
    clients, factory = h._client_factory_registry()
    processed = v3.process_collected_wave(
        experiment,
        repo,
        wave_number,
        client_factory=factory,
    )
    return processed, clients


def _audit_journals(project):
    return sorted(
        (project / "data" / "provider_request_journal").glob(
            "0025.periodic_audit_*.json"
        )
    )


def test_v3_contract_hash_binds_phase60h_recovery_and_replay_revisions(tmp_path):
    _repo, experiment, manifest = _make_v3(tmp_path, target_chapters=1)
    contract = v3.load_experiment_contract(experiment)

    assert contract["experiment_id"] == manifest["experiment_id"]
    assert contract["periodic_audit_recovery_revision"]
    assert contract["periodic_audit_replay_revision"] == v3.AUDIT_REPLAY_REVISION
    assert contract["v2_contract_sha256"]

    # Re-hash a deliberately altered policy identity so validation must reject
    # the policy revision itself rather than merely noticing random byte damage.
    path = experiment / v3.CONTRACT_FILENAME
    damaged = _read(path)
    damaged["periodic_audit_recovery_revision"] = "wrong-recovery-policy"
    stable = dict(damaged)
    stable.pop("contract_sha256")
    damaged["contract_sha256"] = v3._binding_hash(stable)
    _write(path, damaged)

    with pytest.raises(
        v3.FullPipelineExperimentContractV3Error,
        match="wrong audit-recovery revision",
    ):
        v3.load_experiment_contract(experiment)


def test_v3_carried_response_without_paid_journal_fails_closed_before_rewind(tmp_path):
    """Local response bytes can never substitute for durable paid-journal proof."""
    repo, experiment, _manifest = _make_v3(tmp_path, target_chapters=1)
    project = experiment / "projects" / "great_king_zhuxia"
    h = _helpers()
    client = h.AuxiliaryClient("great_king_zhuxia", 25)
    loaded = None

    with production_run_lock(project), harness._working_directory(project):
        try:
            loaded = harness.load_experiment_engine_module(
                repo / harness.PRODUCTION_SCRIPT_NAME
            )
            module = loaded["module"]
            harness.initialize_engine_without_provider(module)
            harness.install_luna_auxiliary_runtime(module, client)

            # This focused regression does not need to replay 25 synthetic waves;
            # it only needs a valid pre-audit publication baseline.  Advance the
            # synthetic disk progress to the minimum real state required for the
            # natural 1-25 audit window, leaving all provider/journal evidence empty.
            progress_path = project / "data" / "progress.json"
            progress = _read(progress_path)
            progress["completed"] = list(range(1, 26))
            progress["last_completed"] = 25
            progress["next_chapter"] = 26
            progress["last_audited_chapter"] = 0
            _write(progress_path, progress)
            module.progress.clear()
            module.progress.update(progress)
            for number in progress["completed"]:
                (project / "output/chapters" / f"{number:04d}.md").write_text(
                    f"# Chapter {number}\n\nComplete synthetic English.\n", encoding="utf-8")
            audit_recovery._snapshot_baseline(project, 1, 25)

            checkpoint_path = (
                project
                / "output"
                / "audits"
                / "audit_0001_0025.checkpoint.json"
            )
            checkpoint = {
                "audit_window": {"start_chapter": 1, "end_chapter": 25},
                "status": "in_progress",
                "chunks": [
                    {
                        "chunk_id": "c0001",
                        "status": "response_received",
                        "attempts": 1,
                    }
                ],
                "attempt_records": [
                    {
                        "chunk_id": "c0001",
                        "kind": "audit",
                        "attempt": 1,
                        "operation_id": (
                            "periodic_glossary_audit_chunk:fixture:c0001:audit:1"
                        ),
                        "lifecycle": "response_received",
                        "raw_content": '{"decisions":[],"issues":[]}',
                    }
                ],
            }
            harness.atomic_write_json(checkpoint_path, checkpoint)
            before = checkpoint_path.read_bytes()

            with pytest.raises(
                v3.FullPipelineExperimentContractV3Error,
                match="carries provider response evidence",
            ):
                v3._strict_reconcile_audit_checkpoint(
                    module,
                    project,
                    checkpoint_path,
                    1,
                    25,
                )

            assert checkpoint_path.read_bytes() == before
            assert client.completions.calls == []
            assert not _audit_journals(project)
        finally:
            if loaded is not None:
                sys.modules.pop(loaded["module_name"], None)


@pytest.mark.parametrize("drift", [None, "lower_request", "carried_content"])
def test_v3_wave25_replays_recorded_audit_response_after_checkpoint_crash(
    tmp_path,
    monkeypatch,
    drift,
):
    """Prove the real Chapter-25 audit restart makes zero new provider calls."""
    repo, experiment, _manifest = _make_v3(tmp_path, target_chapters=25)
    project = experiment / "projects" / "great_king_zhuxia"

    # Grow the genuine chronological state through Chapter 24.  The production
    # audit interval is 25, so no periodic audit may run in this prefix.
    for wave_number in range(1, 25):
        _collect_v3(experiment, repo, wave_number)
        processed, clients = _process_v3(experiment, repo, wave_number)
        assert processed["status"] == "complete"
        assert all(
            "periodic audit" not in request["messages"][0]["content"].lower()
            for client in clients
            for request in client.completions.calls
        )

    _collect_v3(experiment, repo, 25)

    # Crash the real glossary-audit checkpoint write exactly after the paid
    # callback returned and the paid journal durably recorded its response.
    original_atomic = glossary_audit.atomic_write_json
    injected = {"raised": False}

    def crash_after_paid_response(path, value):
        target = Path(path)
        records = value.get("attempt_records", []) if isinstance(value, dict) else []
        if (
            not injected["raised"]
            and target.name == "audit_0001_0025.checkpoint.json"
            and any(
                isinstance(record, dict)
                and record.get("lifecycle") == "response_recorded"
                for record in records
            )
        ):
            injected["raised"] = True
            raise RuntimeError("phase60h simulated post-paid audit checkpoint crash")
        return original_atomic(path, value)

    monkeypatch.setattr(glossary_audit, "atomic_write_json", crash_after_paid_response)
    h = _helpers()
    first_clients, first_factory = h._client_factory_registry()
    with pytest.raises(RuntimeError, match="post-paid audit checkpoint crash"):
        v3.process_collected_wave(
            experiment,
            repo,
            25,
            client_factory=first_factory,
        )
    assert injected["raised"] is True

    progress_after_crash = _read(project / "data" / "progress.json")
    assert progress_after_crash["last_completed"] == 25
    assert progress_after_crash["next_chapter"] == 26
    assert progress_after_crash["last_audited_chapter"] == 0

    checkpoint_path = (
        project / "output" / "audits" / "audit_0001_0025.checkpoint.json"
    )
    checkpoint = _read(checkpoint_path)
    assert checkpoint["status"] == "in_progress"
    assert len(checkpoint["attempt_records"]) == 1
    unresolved = checkpoint["attempt_records"][0]
    # The generic audit recovery path persists the response bytes that were in
    # process memory even though the normal response-recorded checkpoint write
    # failed.  V3 must not trust that local response_received state by itself:
    # the separate paid journal remains the authority for exact replay.
    assert isinstance(unresolved.get("raw_content"), str)
    assert "parsed_result" not in unresolved
    assert checkpoint["chunks"][0]["status"] == "response_received"

    journals = _audit_journals(project)
    assert len(journals) == 1
    paid = _read(journals[0])
    assert paid["status"] == "response_recorded"
    assert paid["provider"] == "openai"

    recovery_manifest = _read(
        project
        / "data"
        / "full_pipeline_audit_recovery"
        / "audit_0001_0025"
        / "recovery_manifest.json"
    )
    assert recovery_manifest["status"] == "active"

    # Restart with a completely fresh provider client.  V3 must restore the
    # exact pre-audit baseline, preserve the unresolved local response record,
    # recompute the current request identity, and replay the journaled response
    # locally only after the journal validates that identity.
    monkeypatch.setattr(glossary_audit, "atomic_write_json", original_atomic)
    resumed_clients, resumed_factory = h._client_factory_registry()
    if drift == "lower_request":
        from production_paid_request_journal import _updated_record
        _write(journals[0], _updated_record(paid, request_sha256="0" * 64))
    elif drift == "carried_content":
        import hashlib
        unresolved["raw_content"] += " "
        unresolved["raw_content_hash"] = hashlib.sha256(unresolved["raw_content"].encode()).hexdigest()
        _write(checkpoint_path, checkpoint)
    if drift is not None:
        with pytest.raises((RuntimeError, SystemExit)):
            v3.process_collected_wave(experiment, repo, 25, client_factory=resumed_factory)
        assert all(client.completions.calls == [] for client in resumed_clients)
        return
    processed = v3.process_collected_wave(
        experiment,
        repo,
        25,
        client_factory=resumed_factory,
    )
    assert processed["status"] == "complete"
    assert len(resumed_clients) == 1
    assert resumed_clients[0].completions.calls == []

    final_progress = _read(project / "data" / "progress.json")
    final_history = _read(project / "data" / "audit_history.json")
    assert final_progress["last_completed"] == 25
    assert final_progress["last_audited_chapter"] == 25
    assert len(final_history["audits"]) == 1
    assert (
        project / "output" / "audits" / "audit_0001_0025.json"
    ).is_file()
    assert (project / "backup" / "checkpoint_0025").is_dir()

    committed_recovery = _read(
        project
        / "data"
        / "full_pipeline_audit_recovery"
        / "audit_0001_0025"
        / "recovery_manifest.json"
    )
    assert committed_recovery["status"] == "committed"

    # The original provider interaction is durable and unchanged; restart did
    # not create a second paid audit journal or replace its exact operation ID.
    journals_after = _audit_journals(project)
    assert journals_after == journals
    assert _read(journals_after[0]) == paid
    final_attempt = _read(checkpoint_path)["attempt_records"][0]
    assert final_attempt["operation_id"] == unresolved["operation_id"]
    assert final_attempt["attempt"] == unresolved["attempt"]
