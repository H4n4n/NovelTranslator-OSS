"""Zero-provider focused tests for the G11 practical FINAL FREEZE.

Every test uses only temporary synthetic data.  No test touches a real G11
execution root, reads a credential, constructs a provider client, sends a
request, scores a translation, or reveals an identity.  Network and credential
access are poisoned so an accidental call fails loudly instead of silently
reaching out.
"""

import builtins
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

import benchmark_longitudinal_g10_safety as g10_safety
import benchmark_longitudinal_g11_final_freeze as freeze_mod
import benchmark_longitudinal_g11_preregistration as prereg
import benchmark_longitudinal_g11_runner as runner


REPO_ROOT = Path(__file__).resolve().parents[1]
GLM = prereg.GLM_MODEL_ID
DEEPSEEK = prereg.DEEPSEEK_MODEL_ID
GLM_ACE = "g11-run01-glm-ace_evolution"
DEEPSEEK_ACE = "g11-run02-deepseek-ace_evolution"
GLM_ROGUE = "g11-run03-glm-rogue_first_prince"
DEEPSEEK_ROGUE = "g11-run04-deepseek-rogue_first_prince"
AUDIT_WINDOWS = ((1, 25), (26, 50), (51, 75), (76, 100))
FROZEN_UTC = "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Synthetic pristine S0 template and fake production root
# ---------------------------------------------------------------------------


def _chapter_library(final_chapter):
    return [
        {
            "chapter": index,
            "production_index": index,
            "volume_index": 1,
            "local_chapter_number": index,
            "local_chapter_start": index,
            "local_chapter_end": index,
            "heading": f"Source heading {index}",
            "text": f"source text for chapter {index}",
        }
        for index in range(1, final_chapter + 1)
    ]


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _make_pristine(root):
    root.mkdir(parents=True, exist_ok=True)
    for novel_id, spec in prereg.NOVELS.items():
        project = root / "runs" / f"g10b-pristine-{novel_id}"
        (project / "data").mkdir(parents=True, exist_ok=True)
        (project / "source").mkdir(parents=True, exist_ok=True)
        config = {
            "id": novel_id,
            "title": spec["title"],
            "source_language": spec["source_language"],
            "target_language": spec["target_language"],
            "profile": spec["profile"],
            "final_chapter": spec["final_chapter"],
            "audit_interval": spec["audit_interval"],
            "chapter_numbering": spec["chapter_numbering"],
        }
        if spec["source_type"] != "epub":
            config["source_type"] = spec["source_type"]
        _write_json(project / "novel.json", config)
        _write_json(
            project / "data" / "chapters.json",
            _chapter_library(spec["final_chapter"]),
        )
        (project / "translation_addendum.txt").write_text(
            "", encoding="utf-8", newline="\n"
        )
        suffix = "txt" if spec["source_type"] == "txt" else "epub"
        (project / "source" / f"novel.{suffix}").write_bytes(
            f"{novel_id}-source-bytes".encode("utf-8")
        )
    return root


def _make_production_state(root):
    root.mkdir(parents=True, exist_ok=True)
    for novel_id in prereg.NOVELS:
        project = root / "novels" / novel_id
        (project / "data").mkdir(parents=True, exist_ok=True)
        (project / "output" / "chapters").mkdir(parents=True, exist_ok=True)
        _write_json(project / "novel.json", {"id": novel_id})
        _write_json(project / "data" / "progress.json", {"last_completed": 7})
        (project / "output" / "chapters" / "0007.md").write_text(
            "live production translation\n", encoding="utf-8", newline="\n"
        )
    return root


# ---------------------------------------------------------------------------
# Finalize a prepared execution into the completed real G11 shape
# ---------------------------------------------------------------------------


def _seed_progress(root):
    state = _read_json(root / "data" / "progress.json")
    state["last_completed"] = 100
    state["next_chapter"] = 101
    state["last_audited_chapter"] = 100
    state["completed"] = list(range(1, 101))
    _write_json(root / "data" / "progress.json", state)


def _synthetic_audit_record(start, end, *, candidate_count=1):
    if candidate_count:
        total_chunks = 1
        completed_chunks = ["chunk-0001"]
        candidate_set_hash = hashlib.sha256(
            f"synthetic-audit-{start}-{end}".encode("utf-8")
        ).hexdigest()
    else:
        total_chunks = 0
        completed_chunks = []
        candidate_set_hash = ""
    return {
        "start_chapter": start,
        "end_chapter": end,
        "timestamp_unix": 0.0,
        "stats": {
            "api_request_count": total_chunks,
            "cost_usd": 0.0,
        },
        "chunking": {
            "candidate_set_hash": candidate_set_hash,
            "candidate_count": candidate_count,
            "total_chunks": total_chunks,
            "completed_chunks": completed_chunks,
            "resumed": False,
            "source_integrity": {},
        },
        "changes": {
            "kept": [],
            "corrected": [],
            "rejected": [],
            "contextualized": [],
            "merged": [],
            "retroactive_replacements": [],
        },
        "retroactive": {
            "chapters_changed": [],
            "replacement_count": 0,
        },
        "issues": [],
    }


def _seed_audit_evidence(root, run_id):
    records = []
    for start, end in AUDIT_WINDOWS:
        record = _synthetic_audit_record(start, end)
        records.append(record)
        _write_json(
            root
            / "output"
            / "audits"
            / f"audit_{start:04d}_{end:04d}.json",
            record,
        )
        path = runner.g11_audit_checkpoint_path(root, start, end)
        _write_json(
            path,
            {
                "run_id": run_id,
                "start_chapter": start,
                "end_chapter": end,
                "audit_window": {
                    "start_chapter": start,
                    "end_chapter": end,
                },
                "status": "completed",
                "chunks": [
                    {
                        "chunk_id": "chunk-0001",
                        "status": "completed",
                    }
                ],
            },
        )
    _write_json(
        root / "data" / "audit_history.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "audits": records,
        },
    )


def _seed_low_mode(root, run_id, model_id):
    runner.activate_deepseek_low_mode(
        path=runner.deepseek_low_mode_path(root),
        run_id=run_id,
        model_id=model_id,
        chapter=4,
        reason="synthetic high-effort saturation on both DeepSeek tracks",
    )


def _seed_audit_recovery(root, start, end, cycle="cycle_01"):
    directory = (
        runner.audit_content_recovery_directory(root, start, end) / cycle
    )
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        directory / "manifest.json",
        {
            "revision": "g11-audit-content-recovery-v1",
            "audit_window": {"start_chapter": start, "end_chapter": end},
            "cycle": 1,
            "reason": "synthetic terminal audit-content recovery",
        },
    )
    return directory


def _seed_content_recovery(root, chapter, cycle="cycle_01"):
    directory = runner.content_recovery_directory(root, chapter) / cycle
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        directory / "manifest.json",
        {"chapter": chapter, "cycle": 1, "reason": "synthetic content recovery"},
    )
    return directory


def _finalize_execution(execution):
    track_by_run = {track["run_id"]: track for track in prereg.TRACKS}
    for run_id, track in track_by_run.items():
        root = runner.track_root(execution, run_id)
        _seed_progress(root)
        _seed_audit_evidence(root, run_id)
        if track["model_id"] == DEEPSEEK:
            _seed_low_mode(root, run_id, track["model_id"])
        for chapter in prereg.QUALITY_CHECKPOINTS:
            runner.freeze_checkpoints_for_chapter(
                execution_root=execution, run_id=run_id, chapter=chapter
            )

    # Real experimental evidence that must remain visible in the freeze.
    _seed_audit_recovery(
        runner.track_root(execution, GLM_ACE), 76, 100, cycle="cycle_01"
    )
    _seed_content_recovery(runner.track_root(execution, DEEPSEEK_ACE), 4)


def _ledger_document(model_id):
    ceiling = prereg.MODEL_BUDGETS[model_id]
    if model_id == GLM:
        credits = ["1.50", "2.50", "0.50"]
        regenerations = [False, False, True]
    else:
        credits = ["10.00", "20.00", "5.00"]
        regenerations = [False, False, False]

    reservations = []
    attempts = []
    for index, (credit, regeneration) in enumerate(
        zip(credits, regenerations), start=1
    ):
        reservation_id = hashlib.sha256(
            f"{model_id}-{index}".encode("utf-8")
        ).hexdigest()
        reservations.append(
            {
                "reservation_id": reservation_id,
                "operation": "g11_chapter_translation",
                "request_sha256": hashlib.sha256(
                    f"request-{model_id}-{index}".encode("utf-8")
                ).hexdigest(),
                "run_id": "synthetic-run",
                "novel_id": "ace_evolution",
                "chapter": index,
                "regeneration": regeneration,
                "status": "settled",
                "started_utc": FROZEN_UTC,
                "transport_attempts": 1,
                "first_attempt_success": True,
                "eventual_success": True,
                "credit_equivalent": credit,
                "tokens": {
                    "prompt_tokens": 100 * index,
                    "completion_tokens": 200 * index,
                    "cache_hit_tokens": 10 * index,
                },
                "reason": "",
                "settled_utc": FROZEN_UTC,
            }
        )
        attempts.append(
            {
                "reservation_id": reservation_id,
                "attempt_index": 1,
                "outcome": "success",
                "error_type": "",
                "error_message": "",
                "classification": "",
                "elapsed_seconds": float(index),
                "credit_equivalent": credit,
                "recorded_utc": FROZEN_UTC,
            }
        )
    return {
        "schema_version": 1,
        "revision": "g11-benchmark-ledger-v1",
        "benchmark_id": prereg.G11_BENCHMARK_ID,
        "g11_preregistration_sha256": prereg.G11_PREREGISTRATION_SHA256,
        "model_id": model_id,
        "ceiling_credit_equivalent": ceiling,
        "created_utc": FROZEN_UTC,
        "updated_utc": FROZEN_UTC,
        "reservations": reservations,
        "attempts": attempts,
    }


def _write_ledgers(execution):
    for model_id in prereg.MODEL_ORDER:
        _write_json(runner.ledger_path(execution, model_id), _ledger_document(model_id))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def g11_base():
    base = Path(tempfile.mkdtemp(prefix="g11-ff-tests-"))
    try:
        pristine = _make_pristine(base / "s0_template")
        production = _make_production_state(base / "production")
        execution = base / "g11_execution"
        runner.prepare_g11(
            execution_root=execution,
            pristine_root=pristine,
            production_root=production,
        )
        _finalize_execution(execution)
        _write_ledgers(execution)
        yield {
            "base": base,
            "pristine": pristine,
            "production": production,
            "execution": execution,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def work():
    """A private scratch directory, independent of pytest's tmp_path base.

    The default pytest temp base can be inaccessible in hardened Windows
    environments, so these tests create their own isolated scratch root.
    """
    base = Path(tempfile.mkdtemp(prefix="g11-ff-work-"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def env(g11_base, work):
    execution = work / "execution"
    shutil.copytree(g11_base["execution"], execution)
    return {
        "tmp": work,
        "pristine": g11_base["pristine"],
        "production": g11_base["production"],
        "execution": execution,
        "output": work / "freeze",
    }


def _kwargs(env, output=None):
    return {
        "repo_root": REPO_ROOT,
        "execution_root": env["execution"],
        "pristine_root": env["pristine"],
        "production_root": env["production"],
        "output_root": output if output is not None else env["output"],
    }


def _create(env, output=None):
    return freeze_mod.create_final_freeze(**_kwargs(env, output))


def _verify(env, output=None):
    return freeze_mod.verify_final_freeze(**_kwargs(env, output))


def _freeze_document(env, output=None):
    target = Path(output if output is not None else env["output"])
    return _read_json(target / freeze_mod.FREEZE_FILENAME)


def _snapshot(env, output=None):
    target = Path(output if output is not None else env["output"])
    return target / freeze_mod.EXECUTION_SNAPSHOT_DIRECTORY


def _forbid(*_args, **_kwargs):
    raise AssertionError("G11 final freeze must not perform this operation")


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------


def test_module_import_is_zero_write_zero_network_zero_credential(monkeypatch, work):
    source = REPO_ROOT / "benchmark_longitudinal_g11_final_freeze.py"
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"openai", "dotenv"}:
            raise AssertionError(
                "G11 final freeze must not import the provider SDK at import time"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(os, "getenv", _forbid)
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    monkeypatch.chdir(work)

    unique = f"_g11_final_freeze_import_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(unique, None)

    assert [path.name for path in work.rglob("*")] == []
    assert hasattr(module, "main")


def test_contract_is_zero_provider_and_reveal_free():
    freeze_mod.assert_zero_provider_safety()
    assert freeze_mod.EXPECTED_CANONICAL_CHAPTERS == 400
    assert freeze_mod.EXPECTED_SCHEDULED_AUDITS == 16
    assert freeze_mod.EXPECTED_LITERARY_MATCHED == 28
    assert freeze_mod.EXPECTED_STATE_MATCHED == 8
    assert freeze_mod.DEEPSEEK_ADAPTIVE_LOW_ACTIVATION_CHAPTER == 4
    assert freeze_mod.EXPECTED_TRACK_RUN_IDS == (
        GLM_ACE,
        DEEPSEEK_ACE,
        GLM_ROGUE,
        DEEPSEEK_ROGUE,
    )


# ---------------------------------------------------------------------------
# Successful create + verify
# ---------------------------------------------------------------------------


def test_create_and_verify_successful_synthetic(env, monkeypatch):
    monkeypatch.setattr(runner, "create_provider_client", _forbid)

    result = _create(env)
    assert result["provider_calls"] == 0
    assert result["credential_access"] is False
    assert result["blind_bundle_created"] is False
    assert result["scoring_performed"] is False
    assert result["identity_revealed"] is False
    assert result["paid_execution_authorized"] is False
    assert result["canonical_chapter_translations"] == 400
    assert result["scheduled_audits"] == 16
    assert result["literary_matched_checkpoints"] == 28
    assert result["state_matched_checkpoints"] == 8
    assert result["model_ids"] == [GLM, DEEPSEEK]

    assert (env["output"] / freeze_mod.EXECUTION_MANIFEST_FILENAME).is_file()
    assert (env["output"] / freeze_mod.SUMMARY_FILENAME).is_file()
    assert _snapshot(env).is_dir()

    document = _freeze_document(env)
    assert document["freeze_id"] == result["freeze_id"]
    assert document["shared_ledger_deduplicated"] is True
    assert len(document["model_ledgers"]) == 2
    assert document["provider_calls"] == 0
    assert document["scoring_performed"] is False
    assert document["identity_revealed"] is False
    assert document["blind_bundle_created"] is False

    verification = _verify(env)
    assert verification["verified"] is True
    assert verification["freeze_id"] == result["freeze_id"]


def test_frozen_operational_facts_and_recovery_preserved(env):
    _create(env)
    document = _freeze_document(env)
    tracks = {track["run_id"]: track for track in document["tracks"]}

    assert set(tracks) == {GLM_ACE, DEEPSEEK_ACE, GLM_ROGUE, DEEPSEEK_ROGUE}
    for track in tracks.values():
        assert track["progress"]["last_completed"] == 100
        assert track["progress"]["next_chapter"] == 101
        assert track["progress"]["last_audited_chapter"] == 100
        assert track["quarantine"] is None
        assert len(track["literary_checkpoints"]) == 14
        assert len(track["state_checkpoints"]) == 4
        assert len(track["audit_evidence"]["windows"]) == 4

    assert tracks[DEEPSEEK_ACE]["adaptive_low_translation_mode"] == {
        "activated": True,
        "activation_chapter": 4,
        "high_effort_saturation_events": 1,
    }
    assert tracks[DEEPSEEK_ROGUE]["adaptive_low_translation_mode"][
        "activation_chapter"
    ] == 4
    assert tracks[GLM_ACE]["adaptive_low_translation_mode"]["activated"] is False
    assert tracks[GLM_ROGUE]["adaptive_low_translation_mode"]["activated"] is False

    glm_ace = tracks[GLM_ACE]
    assert glm_ace["final_audit_content_recovery_cycles"] == 1
    evidence = glm_ace["audit_content_recovery_evidence"]["0076_0100"]
    assert len(evidence) == 1
    assert evidence[0]["cycle"] == "cycle_01"
    assert evidence[0]["manifest_sha256"] is not None
    assert glm_ace["audit_evidence"]["audit_history_sha256"] is not None

    deepseek_ace = tracks[DEEPSEEK_ACE]
    assert deepseek_ace["content_recovery_cycles"] == 1


def test_summary_exposes_frozen_facts(env):
    _create(env)
    text = (env["output"] / freeze_mod.SUMMARY_FILENAME).read_text(encoding="utf-8")
    document = _freeze_document(env)
    assert document["freeze_id"] in text
    assert document["execution"]["snapshot_tree_sha256"] in text
    assert "canonical_chapter_translations: 400 / 400" in text
    assert "scheduled_audits: 16 / 16" in text
    assert "literary_matched_checkpoints: 28 / 28" in text
    assert "state_matched_checkpoints: 8 / 8" in text
    assert "provider_calls: 0" in text
    assert "identity_revealed: false" in text
    assert "literary winner" not in text.lower()


def test_no_real_filesystem_paths_are_frozen(env):
    _create(env)
    document = _freeze_document(env)

    def strings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    for text in strings(document):
        assert not re.match(r"^[A-Za-z]:[\\/]", text), text
        assert str(REPO_ROOT) not in text
        assert str(env["execution"]) not in text


def test_deterministic_freeze_id(env):
    first = _create(env, output=env["tmp"] / "freeze_a")
    second = _create(env, output=env["tmp"] / "freeze_b")
    assert first["freeze_id"] == second["freeze_id"]


def test_repo_argument_must_be_executing_checkout(env, work):
    other_repo = work / "other_repo"
    other_repo.mkdir()
    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="must be the checkout that contains",
    ):
        freeze_mod.create_final_freeze(
            repo_root=other_repo,
            execution_root=env["execution"],
            pristine_root=env["pristine"],
            production_root=env["production"],
            output_root=work / "freeze_other_repo",
        )


def test_historical_source_binding_proof_must_be_exact(env):
    manifest_path = env["execution"] / runner.G11_EXECUTION_MANIFEST_FILENAME
    manifest = _read_json(manifest_path)
    manifest["source_bindings_verified"] = {
        "verified": True,
        "binding_count": 0,
    }
    check = dict(manifest)
    check.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = freeze_mod._sha256_json(check)
    _write_json(manifest_path, manifest)
    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="exact verified source-binding proof",
    ):
        _create(env)


# ---------------------------------------------------------------------------
# Create-once and path safety
# ---------------------------------------------------------------------------


def test_output_create_once_refusal(env):
    _create(env)
    with pytest.raises(freeze_mod.G11FinalFreezeSafetyError):
        _create(env)


def test_output_overlap_refusal(env):
    inside = env["execution"] / "freeze"
    with pytest.raises(freeze_mod.G11FinalFreezeSafetyError):
        _create(env, output=inside)


def test_output_equal_to_input_root_refusal(env):
    with pytest.raises(freeze_mod.G11FinalFreezeSafetyError):
        _create(env, output=env["execution"])


def test_failed_create_leaves_no_completed_freeze(env, monkeypatch):
    original = freeze_mod._freeze_document

    def boom(_payload):
        raise freeze_mod.G11FinalFreezeError("synthetic failure")

    monkeypatch.setattr(freeze_mod, "_freeze_document", boom)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)
    monkeypatch.setattr(freeze_mod, "_freeze_document", original)
    assert not env["output"].exists()
    assert (
        list(env["tmp"].glob(f".{env['output'].name}.creating-*")) == []
    )


# ---------------------------------------------------------------------------
# Completion validation refusals
# ---------------------------------------------------------------------------


def test_incomplete_track_refusal(env):
    root = runner.track_root(env["execution"], GLM_ACE)
    state = _read_json(root / "data" / "progress.json")
    state["last_completed"] = 99
    state["next_chapter"] = 100
    _write_json(root / "data" / "progress.json", state)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_active_quarantine_refusal(env):
    track = prereg.TRACK_BY_ID[GLM_ROGUE]
    runner.write_quarantine(
        track_root_path=runner.track_root(env["execution"], GLM_ROGUE),
        run_id=GLM_ROGUE,
        model_id=track["model_id"],
        novel_id=track["novel_id"],
        first_blocked_chapter=101,
        outcome="terminal_failure",
        reason="synthetic",
    )
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_missing_literary_checkpoint_refusal(env):
    target = (
        env["execution"]
        / runner.G11_EVIDENCE_DIRECTORY
        / DEEPSEEK_ROGUE
        / "0090"
    )
    assert target.is_dir()
    shutil.rmtree(target)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_missing_state_checkpoint_refusal(env):
    checkpoint = (
        env["execution"]
        / runner.G11_EVIDENCE_DIRECTORY
        / GLM_ACE
        / "0025"
        / "checkpoint.json"
    )
    document = _read_json(checkpoint)
    document["state_evidence_checkpoint"] = False
    _write_json(checkpoint, document)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_zero_candidate_audit_does_not_require_chunk_checkpoint(env):
    root = runner.track_root(env["execution"], DEEPSEEK_ACE)
    start, end = 26, 50

    record = _synthetic_audit_record(start, end, candidate_count=0)
    audit_path = (
        root
        / "output"
        / "audits"
        / f"audit_{start:04d}_{end:04d}.json"
    )
    _write_json(audit_path, record)

    history_path = root / "data" / "audit_history.json"
    history = _read_json(history_path)
    history["audits"] = [
        record
        if item.get("start_chapter") == start and item.get("end_chapter") == end
        else item
        for item in history["audits"]
    ]
    _write_json(history_path, history)

    checkpoint_path = runner.g11_audit_checkpoint_path(root, start, end)
    checkpoint_path.unlink()

    result = _create(env)
    assert result["scheduled_audits"] == 16

    frozen = _freeze_document(env)
    track = next(
        item for item in frozen["tracks"] if item["run_id"] == DEEPSEEK_ACE
    )
    window = next(
        item
        for item in track["audit_evidence"]["windows"]
        if item["start_chapter"] == start and item["end_chapter"] == end
    )
    assert window["candidate_count"] == 0
    assert window["total_chunks"] == 0
    assert window["completed_chunks"] == []
    assert window["checkpoint_present"] is False
    assert window["checkpoint_sha256"] is None
    assert (
        window["completion_basis"]
        == "committed_zero_candidate_audit_record"
    )


def test_zero_candidate_audit_with_contradictory_chunks_refusal(env):
    root = runner.track_root(env["execution"], DEEPSEEK_ACE)
    start, end = 26, 50

    record = _synthetic_audit_record(start, end, candidate_count=0)
    record["chunking"]["total_chunks"] = 1
    record["chunking"]["completed_chunks"] = ["impossible"]

    audit_path = (
        root
        / "output"
        / "audits"
        / f"audit_{start:04d}_{end:04d}.json"
    )
    _write_json(audit_path, record)

    history_path = root / "data" / "audit_history.json"
    history = _read_json(history_path)
    history["audits"] = [
        record
        if item.get("start_chapter") == start and item.get("end_chapter") == end
        else item
        for item in history["audits"]
    ]
    _write_json(history_path, history)

    runner.g11_audit_checkpoint_path(root, start, end).unlink()

    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="contradictory chunk accounting",
    ):
        _create(env)


def test_missing_committed_audit_record_refusal(env):
    root = runner.track_root(env["execution"], GLM_ROGUE)
    path = root / "output" / "audits" / "audit_0051_0075.json"
    path.unlink()
    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="audit record 51-75 is missing",
    ):
        _create(env)


def test_incomplete_audit_checkpoint_refusal(env):
    root = runner.track_root(env["execution"], GLM_ROGUE)
    checkpoint_path = runner.g11_audit_checkpoint_path(root, 51, 75)
    checkpoint = _read_json(checkpoint_path)
    checkpoint["status"] = "in_progress"
    _write_json(checkpoint_path, checkpoint)
    with pytest.raises(freeze_mod.G11FinalFreezeError, match="is not completed"):
        _create(env)


def test_audit_checkpoint_wrong_window_refusal(env):
    root = runner.track_root(env["execution"], DEEPSEEK_ROGUE)
    checkpoint_path = runner.g11_audit_checkpoint_path(root, 26, 50)
    checkpoint = _read_json(checkpoint_path)
    checkpoint["audit_window"] = {"start_chapter": 1, "end_chapter": 25}
    _write_json(checkpoint_path, checkpoint)
    with pytest.raises(freeze_mod.G11FinalFreezeError, match="binds another audit window"):
        _create(env)


def test_corrupt_paid_journal_refusal(env):
    root = runner.track_root(env["execution"], GLM_ROGUE)
    journal_dir = root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="corrupt or unexpected provider journal evidence",
    ):
        _create(env)


def test_stale_paid_journal_tmp_refusal(env):
    root = runner.track_root(env["execution"], GLM_ROGUE)
    journal_dir = root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "stale.tmp").write_text("partial", encoding="utf-8")
    with pytest.raises(
        freeze_mod.G11FinalFreezeError,
        match="corrupt or unexpected provider journal evidence",
    ):
        _create(env)


def test_deepseek_chapter_four_adaptive_low_validation(env):
    marker_path = runner.deepseek_low_mode_path(
        runner.track_root(env["execution"], DEEPSEEK_ROGUE)
    )
    marker = _read_json(marker_path)
    marker["activation_chapter"] = 5
    _write_json(marker_path, marker)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_glm_adaptive_low_rejection(env):
    track = prereg.TRACK_BY_ID[GLM_ROGUE]
    marker = {
        "schema_version": 1,
        "revision": "g11-deepseek-adaptive-low-translation-v1",
        "benchmark_id": prereg.G11_BENCHMARK_ID,
        "run_id": GLM_ROGUE,
        "model_id": track["model_id"],
        "activated": True,
        "activation_chapter": 4,
        "activation_reason": "synthetic invalid GLM low mode",
        "high_effort_saturation_events": [],
        "created_utc": FROZEN_UTC,
        "updated_utc": FROZEN_UTC,
    }
    _write_json(
        runner.deepseek_low_mode_path(
            runner.track_root(env["execution"], GLM_ROGUE)
        ),
        marker,
    )
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _create(env)


def test_symlink_rejection_when_supported(env):
    link = env["execution"] / "syntactic_link"
    try:
        os.symlink(env["execution"] / "g11_execution_manifest.json", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported in this environment")
    with pytest.raises(
        (
            freeze_mod.G11FinalFreezeError,
            runner.G11RunnerError,
            g10_safety.G10SafetyError,
        )
    ):
        _create(env)


# ---------------------------------------------------------------------------
# Shared-ledger deduplication
# ---------------------------------------------------------------------------


def test_shared_ledger_deduplication(env):
    _create(env)
    document = _freeze_document(env)
    ledgers = document["model_ledgers"]
    assert set(ledgers) == {GLM, DEEPSEEK}

    glm_summary = ledgers[GLM]["summary"]
    assert glm_summary["committed_credit_equivalent"] == "4.5"
    assert glm_summary["ceiling_credit_equivalent"] == "40.00"
    assert glm_summary["remaining_credit_equivalent"] == "35.5"
    assert glm_summary["regenerations"] == 1

    deepseek_summary = ledgers[DEEPSEEK]["summary"]
    assert deepseek_summary["committed_credit_equivalent"] == "35"
    assert deepseek_summary["ceiling_credit_equivalent"] == "60.00"
    assert deepseek_summary["remaining_credit_equivalent"] == "25"

    # Two tracks per model point at the SAME physical ledger and are never summed.
    assert document["shared_ledger_deduplicated"] is True
    assert ledgers[GLM]["derived_from_durable_ledger"][
        "total_attempt_elapsed_seconds"
    ] == "6"
    assert ledgers[GLM]["derived_from_durable_ledger"]["reservation_status_counts"] == {
        "settled": 3
    }


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def _tamper(path):
    path = Path(path)
    path.write_bytes(path.read_bytes() + b"tampered")


def test_snapshot_byte_tamper_detection(env):
    _create(env)
    _tamper(_snapshot(env) / "g11_execution_manifest.json")
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_snapshot_missing_file_detection(env):
    _create(env)
    victim = _snapshot(env) / "g11_ledgers" / "glm.json"
    victim.unlink()
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_snapshot_extra_file_detection(env):
    _create(env)
    (_snapshot(env) / "unexpected_extra_file.bin").write_bytes(b"extra")
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_execution_manifest_tamper_detection(env):
    _create(env)
    manifest_path = env["output"] / freeze_mod.EXECUTION_MANIFEST_FILENAME
    manifest = _read_json(manifest_path)
    manifest["files"][0]["sha256"] = "0" * 64
    _write_json(manifest_path, manifest)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_ledger_tamper_detection(env):
    _create(env)
    ledger_path = _snapshot(env) / "g11_ledgers" / "glm.json"
    document = _read_json(ledger_path)
    document["reservations"][0]["credit_equivalent"] = "999.00"
    _write_json(ledger_path, document)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_checkpoint_tamper_detection(env):
    _create(env)
    checkpoint = (
        _snapshot(env) / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0090" / "checkpoint.json"
    )
    document = _read_json(checkpoint)
    document["quality_checkpoint"] = False
    _write_json(checkpoint, document)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_freeze_json_tamper_detection(env):
    _create(env)
    path = env["output"] / freeze_mod.FREEZE_FILENAME
    document = _read_json(path)
    document["completion"]["canonical_chapter_translations"] = 399
    _write_json(path, document)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_freeze_id_tamper_detection(env):
    _create(env)
    path = env["output"] / freeze_mod.FREEZE_FILENAME
    document = _read_json(path)
    document["freeze_id"] = "0" * 64
    _write_json(path, document)
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_declared_source_drift_detection(env):
    _create(env)
    _tamper(env["execution"] / "g11_execution_manifest.json")
    with pytest.raises(freeze_mod.G11FinalFreezeError):
        _verify(env)


def test_safety_flag_tamper_detection(env):
    _create(env)
    path = env["output"] / freeze_mod.FREEZE_FILENAME
    document = _read_json(path)
    document["provider_calls"] = 5
    # Recompute the freeze_id so only the flag is wrong.
    payload = {k: v for k, v in document.items() if k != "freeze_id"}
    document["freeze_id"] = freeze_mod._sha256_json(payload)
    _write_json(path, document)
    with pytest.raises(freeze_mod.G11FinalFreezeSafetyError):
        _verify(env)


# ---------------------------------------------------------------------------
# Zero provider / zero credential execution
# ---------------------------------------------------------------------------


def test_create_and_verify_make_no_network_or_credential_calls(env, monkeypatch):
    monkeypatch.setattr(os, "getenv", _forbid)
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    monkeypatch.setattr(runner, "create_provider_client", _forbid)

    result = _create(env)
    assert result["provider_calls"] == 0
    verification = _verify(env)
    assert verification["provider_calls"] == 0
    assert verification["credential_access"] is False
    assert verification["blind_bundle_created"] is False
    assert verification["scoring_performed"] is False
    assert verification["identity_revealed"] is False
