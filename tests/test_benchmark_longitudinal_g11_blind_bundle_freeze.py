"""Zero-provider focused tests for the G11 blind bundle freeze.

Every test uses only temporary synthetic data.  No test touches a real G11
execution or final-freeze root, reads a credential, constructs a Provider client,
sends a request, scores a translation, or reveals an identity.
"""

import builtins
import copy
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

import benchmark_longitudinal_g11_blind as blind
import benchmark_longitudinal_g11_blind_bundle_freeze as bundle_mod
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


def _sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Synthetic pristine S0 template and production root
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Finalize a prepared execution into the completed G11 shape
# --------------------------------------------------------------------------- #


def _seed_progress(root):
    state = _read_json(root / "data" / "progress.json")
    state["last_completed"] = 100
    state["next_chapter"] = 101
    state["last_audited_chapter"] = 100
    state["completed"] = list(range(1, 101))
    _write_json(root / "data" / "progress.json", state)


def _synthetic_audit_record(start, end):
    return {
        "start_chapter": start,
        "end_chapter": end,
        "timestamp_unix": 0.0,
        "stats": {"api_request_count": 1, "cost_usd": 0.0},
        "chunking": {
            "candidate_set_hash": _sha_text(f"synthetic-audit-{start}-{end}"),
            "candidate_count": 1,
            "total_chunks": 1,
            "completed_chunks": ["chunk-0001"],
            "resumed": False,
            "source_integrity": {},
        },
        "changes": {
            "kept": [{"chapter": start, "term": "term-a"}],
            "corrected": [],
            "rejected": [],
            "contextualized": [],
            "merged": [],
            "retroactive_replacements": [],
        },
        "retroactive": {"chapters_changed": [], "replacement_count": 0},
        "issues": [{"chapter": start, "type": "terminology", "details": "ok"}],
    }


def _seed_audit_evidence(root, run_id):
    records = []
    for start, end in AUDIT_WINDOWS:
        record = _synthetic_audit_record(start, end)
        records.append(record)
        _write_json(
            root / "output" / "audits" / f"audit_{start:04d}_{end:04d}.json",
            record,
        )
        _write_json(
            runner.g11_audit_checkpoint_path(root, start, end),
            {
                "run_id": run_id,
                "start_chapter": start,
                "end_chapter": end,
                "audit_window": {"start_chapter": start, "end_chapter": end},
                "status": "completed",
                "chunks": [{"chunk_id": "chunk-0001", "status": "completed"}],
            },
        )
    _write_json(
        root / "data" / "audit_history.json",
        {"schema_version": 1, "run_id": run_id, "audits": records},
    )


def _seed_chapter_files(root, track):
    for chapter in range(1, 101):
        path = root / "output" / "chapters" / f"{chapter:04d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"variant {track['run_index']} chapter {chapter}\n",
            encoding="utf-8",
            newline="\n",
        )


def _seed_low_mode(root, run_id, model_id):
    runner.activate_deepseek_low_mode(
        path=runner.deepseek_low_mode_path(root),
        run_id=run_id,
        model_id=model_id,
        chapter=4,
        reason="synthetic high-effort saturation",
    )


def _finalize_execution(execution):
    track_by_run = {track["run_id"]: track for track in prereg.TRACKS}
    for run_id, track in track_by_run.items():
        root = runner.track_root(execution, run_id)
        _seed_progress(root)
        _seed_audit_evidence(root, run_id)
        _seed_chapter_files(root, track)
        if track["model_id"] == DEEPSEEK:
            _seed_low_mode(root, run_id, track["model_id"])
        for chapter in prereg.QUALITY_CHECKPOINTS:
            runner.freeze_checkpoints_for_chapter(
                execution_root=execution, run_id=run_id, chapter=chapter
            )


def _ledger_document(model_id):
    ceiling = prereg.MODEL_BUDGETS[model_id]
    credits = ["1.50", "2.50", "0.50"] if model_id == GLM else ["10.00", "20.00", "5.00"]
    reservations = []
    attempts = []
    for index, credit in enumerate(credits, start=1):
        reservation_id = _sha_text(f"{model_id}-{index}")
        reservations.append(
            {
                "reservation_id": reservation_id,
                "operation": "g11_chapter_translation",
                "request_sha256": _sha_text(f"request-{model_id}-{index}"),
                "run_id": "synthetic-run",
                "novel_id": "ace_evolution",
                "chapter": index,
                "regeneration": False,
                "status": "settled",
                "started_utc": FROZEN_UTC,
                "transport_attempts": 1,
                "first_attempt_success": True,
                "eventual_success": True,
                "credit_equivalent": credit,
                "tokens": {"prompt_tokens": 100, "completion_tokens": 200},
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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def ff_base():
    base = Path(tempfile.mkdtemp(prefix="g11-bb-ff-"))
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
        final_freeze = base / "final_freeze"
        freeze_mod.create_final_freeze(
            repo_root=REPO_ROOT,
            execution_root=execution,
            pristine_root=pristine,
            production_root=production,
            output_root=final_freeze,
        )
        yield {"base": base, "final_freeze": final_freeze}
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def work():
    base = Path(tempfile.mkdtemp(prefix="g11-bb-work-"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _pin_constants(monkeypatch, final_freeze_root):
    document = _read_json(final_freeze_root / freeze_mod.FREEZE_FILENAME)
    monkeypatch.setattr(bundle_mod, "EXPECTED_FINAL_FREEZE_ID", document["freeze_id"])
    monkeypatch.setattr(
        bundle_mod,
        "EXPECTED_FINAL_FREEZE_FILE_SHA256",
        _sha_file(final_freeze_root / freeze_mod.FREEZE_FILENAME),
    )
    monkeypatch.setattr(
        bundle_mod,
        "EXPECTED_EXECUTION_MANIFEST_FILE_SHA256",
        _sha_file(final_freeze_root / freeze_mod.EXECUTION_MANIFEST_FILENAME),
    )
    monkeypatch.setattr(
        bundle_mod,
        "EXPECTED_FINAL_FREEZE_SUMMARY_SHA256",
        _sha_file(final_freeze_root / freeze_mod.SUMMARY_FILENAME),
    )
    monkeypatch.setattr(
        bundle_mod,
        "EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256",
        document["execution"]["snapshot_tree_sha256"],
    )
    monkeypatch.setattr(
        bundle_mod,
        "EXPECTED_TOOL_REPOSITORY_BINDING",
        document["repository"]["worktree_head"],
    )


@pytest.fixture
def env(ff_base, work, monkeypatch):
    final_freeze = work / "final_freeze"
    shutil.copytree(ff_base["final_freeze"], final_freeze)
    _pin_constants(monkeypatch, final_freeze)
    return {
        "work": work,
        "final_freeze": final_freeze,
        "snapshot": final_freeze / freeze_mod.EXECUTION_SNAPSHOT_DIRECTORY,
        "output": work / "bundle",
    }


def _kwargs(env, output=None):
    return {
        "repo_root": REPO_ROOT,
        "final_freeze_root": env["final_freeze"],
        "output_root": output if output is not None else env["output"],
    }


def _create(env, output=None):
    return bundle_mod.create_blind_bundle(**_kwargs(env, output))


def _verify(env, output=None):
    return bundle_mod.verify_blind_bundle(**_kwargs(env, output))


def _forbid(*_args, **_kwargs):
    raise AssertionError("the G11 blind bundle must not perform this operation")


# --------------------------------------------------------------------------- #
# Import safety
# --------------------------------------------------------------------------- #


def test_module_import_is_zero_write_zero_network_zero_credential(monkeypatch):
    work = Path(tempfile.mkdtemp(prefix="g11-bb-import-"))
    try:
        source = REPO_ROOT / "benchmark_longitudinal_g11_blind_bundle_freeze.py"
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"openai", "dotenv"}:
                raise AssertionError("G11 blind bundle must not import the provider SDK")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        monkeypatch.setattr(os, "getenv", _forbid)
        monkeypatch.setattr(socket, "socket", _forbid)
        monkeypatch.setattr(socket, "create_connection", _forbid)
        monkeypatch.setattr(socket, "getaddrinfo", _forbid)
        monkeypatch.chdir(work)

        unique = f"_g11_bundle_import_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(unique, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(unique, None)

        assert [path.name for path in work.rglob("*")] == []
        assert module.EXPECTED_LITERARY_COUNT == 28
        assert module.EXPECTED_LONGITUDINAL_COUNT == 8
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_contract_is_zero_provider_and_reveal_free():
    bundle_mod.assert_zero_provider_safety()
    assert bundle_mod.EXPECTED_CANONICAL_CHAPTERS == 400
    assert bundle_mod.EXPECTED_SCHEDULED_AUDITS == 16
    assert bundle_mod.EXPECTED_LITERARY_MATCHED == 28
    assert bundle_mod.EXPECTED_STATE_MATCHED == 8
    assert bundle_mod.EXPECTED_FINAL_FREEZE_ID == blind.G11_FINAL_FREEZE_ID


# --------------------------------------------------------------------------- #
# Successful create + verify
# --------------------------------------------------------------------------- #


def test_create_and_verify_successful_synthetic(env, monkeypatch):
    monkeypatch.setattr(runner, "create_provider_client", _forbid)

    result = _create(env)
    assert result["provider_calls"] == 0
    assert result["credential_access"] is False
    assert result["scoring_performed"] is False
    assert result["identity_revealed"] is False
    assert result["paid_execution_authorized"] is False
    assert result["literary_case_count"] == 28
    assert result["longitudinal_case_count"] == 8

    assert (env["output"] / bundle_mod.PRIVATE_DIRECTORY).is_dir()
    assert (
        env["output"]
        / bundle_mod.PRIVATE_DIRECTORY
        / bundle_mod.PRIVATE_KEY_FILENAME
    ).is_file()
    assert (
        env["output"]
        / bundle_mod.PUBLIC_DIRECTORY
        / bundle_mod.PUBLIC_MANIFEST_FILENAME
    ).is_file()
    assert len(list((env["output"] / bundle_mod.PUBLIC_DIRECTORY / "literary").glob("*.json"))) == 28
    assert len(
        list((env["output"] / bundle_mod.PUBLIC_DIRECTORY / "longitudinal").glob("*.json"))
    ) == 8

    document = _read_json(env["output"] / bundle_mod.FREEZE_FILENAME)
    assert document["bundle_freeze_id"] == result["bundle_freeze_id"]
    assert document["counts"]["literary"] == 28
    assert document["counts"]["state"] == 8
    for novel_id in blind.NOVEL_ORDER:
        assert document["counts"]["per_novel"][novel_id]["literary"] == 14
        assert document["counts"]["per_novel"][novel_id]["state"] == 4
    assert document["literary_orientation_proof"]["balanced"] is True
    assert document["literary_orientation_proof"]["global_side_a_orientation_counts"] == [14, 14]
    for novel_id in blind.NOVEL_ORDER:
        assert document["literary_orientation_proof"]["per_novel_side_a_orientation_counts"][novel_id] == [7, 7]
    assert document["longitudinal_orientation_proof"]["orientation_stable_across_checkpoints"] is True
    assert document["longitudinal_orientation_proof"]["one_novel_each_orientation"] is True
    assert document["provider_calls"] == 0
    assert document["scoring_performed"] is False
    assert document["identity_revealed"] is False
    assert "literary_mapping" not in json.dumps(document)

    verification = _verify(env)
    assert verification["verified"] is True
    assert verification["bundle_freeze_id"] == result["bundle_freeze_id"]


def test_deterministic_bundle_tree_across_creation_but_secret_random(env):
    first = _create(env, output=env["work"] / "bundle_a")
    second = _create(env, output=env["work"] / "bundle_b")
    first_key = _read_json(
        env["work"] / "bundle_a" / bundle_mod.PRIVATE_DIRECTORY / bundle_mod.PRIVATE_KEY_FILENAME
    )
    second_key = _read_json(
        env["work"] / "bundle_b" / bundle_mod.PRIVATE_DIRECTORY / bundle_mod.PRIVATE_KEY_FILENAME
    )
    # A fresh secret is drawn per creation, so the private mapping may differ.
    assert first_key["secret_sha256"] != second_key["secret_sha256"]
    # But the deterministic freeze shape is identical for the same evidence.
    assert first["literary_case_count"] == second["literary_case_count"] == 28


def test_public_package_is_blind(env):
    _create(env)
    public_dir = env["output"] / bundle_mod.PUBLIC_DIRECTORY
    for path in public_dir.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").casefold()
        for literal in blind.FORBIDDEN_PUBLIC_EXACT_LITERALS:
            assert literal.casefold() not in text
        assert GLM.casefold() not in text
        assert DEEPSEEK.casefold() not in text

    forbidden_tokens = (
        "model_id",
        "model_name",
        "provider",
        "reasoning_effort",
        "credit_equivalent",
        "cost",
        "ledger",
        "transport",
        "retries",
        "regeneration",
        "recovery",
        "request_sha",
        "reservation_id",
        "journal",
        "accounting",
        "run_id",
    )
    for path in list((public_dir / "literary").glob("*.json")) + list(
        (public_dir / "longitudinal").glob("*.json")
    ):
        text = path.read_text(encoding="utf-8").casefold()
        for token in forbidden_tokens:
            assert token not in text, f"{token} leaked into {path.name}"
        case = _read_json(path)
        assert "side_a" in case and "side_b" in case


def test_all_cases_available(env):
    _create(env)
    manifest = _read_json(
        env["output"] / bundle_mod.PUBLIC_DIRECTORY / bundle_mod.PUBLIC_MANIFEST_FILENAME
    )
    assert manifest["literary_case_count"] == 28
    assert manifest["longitudinal_case_count"] == 8
    assert len(manifest["literary_cases"]) == 28
    assert len(manifest["longitudinal_cases"]) == 8
    for row in manifest["literary_cases"]:
        assert (env["output"] / bundle_mod.PUBLIC_DIRECTORY / row["path"]).is_file()
        assert _sha_file(env["output"] / bundle_mod.PUBLIC_DIRECTORY / row["path"]) == row["sha256"]
    for row in manifest["longitudinal_cases"]:
        assert (env["output"] / bundle_mod.PUBLIC_DIRECTORY / row["path"]).is_file()
        assert _sha_file(env["output"] / bundle_mod.PUBLIC_DIRECTORY / row["path"]) == row["sha256"]


# --------------------------------------------------------------------------- #
# Create-once and path safety
# --------------------------------------------------------------------------- #


def test_output_create_once_refusal(env):
    _create(env)
    with pytest.raises(bundle_mod.G11BlindBundleSafetyError):
        _create(env)


def test_output_overlap_refusal(env):
    with pytest.raises(bundle_mod.G11BlindBundleSafetyError):
        _create(env, output=env["final_freeze"] / "bundle")


def test_output_equal_to_final_freeze_refusal(env):
    with pytest.raises(bundle_mod.G11BlindBundleSafetyError):
        _create(env, output=env["final_freeze"])


def test_failed_create_cleanup(env, monkeypatch):
    original = bundle_mod._freeze_document

    def boom(_payload):
        raise bundle_mod.G11BlindBundleError("synthetic failure")

    monkeypatch.setattr(bundle_mod, "_freeze_document", boom)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _create(env)
    monkeypatch.setattr(bundle_mod, "_freeze_document", original)
    assert not env["output"].exists()
    assert list(env["work"].glob(f".{env['output'].name}.creating-*")) == []


def test_output_symlink_refusal(env):
    link = env["work"] / "bundle_link"
    try:
        os.symlink(env["work"] / "bundle_target", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported in this environment")
    with pytest.raises(bundle_mod.G11BlindBundleSafetyError):
        bundle_mod.create_blind_bundle(
            repo_root=REPO_ROOT,
            final_freeze_root=env["final_freeze"],
            output_root=link,
        )


def test_snapshot_symlink_refusal(env):
    link = env["snapshot"] / "syntactic_link"
    try:
        os.symlink(env["snapshot"] / runner.G11_EXECUTION_MANIFEST_FILENAME, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported in this environment")
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _create(env)


# --------------------------------------------------------------------------- #
# Predecessor validation refusals
# --------------------------------------------------------------------------- #


def test_final_freeze_id_mismatch_refusal(env, monkeypatch):
    monkeypatch.setattr(bundle_mod, "EXPECTED_FINAL_FREEZE_ID", "0" * 64)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="final-freeze ID"):
        _create(env)


def test_final_freeze_file_sha_mismatch_refusal(env, monkeypatch):
    monkeypatch.setattr(bundle_mod, "EXPECTED_FINAL_FREEZE_FILE_SHA256", "0" * 64)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="g11_final_freeze.json"):
        _create(env)


def test_execution_manifest_sha_mismatch_refusal(env, monkeypatch):
    monkeypatch.setattr(
        bundle_mod, "EXPECTED_EXECUTION_MANIFEST_FILE_SHA256", "0" * 64
    )
    with pytest.raises(bundle_mod.G11BlindBundleError, match="execution_tree_manifest"):
        _create(env)


def test_summary_sha_mismatch_refusal(env, monkeypatch):
    monkeypatch.setattr(bundle_mod, "EXPECTED_FINAL_FREEZE_SUMMARY_SHA256", "0" * 64)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="SUMMARY"):
        _create(env)


def test_snapshot_tree_sha_mismatch_refusal(env, monkeypatch):
    monkeypatch.setattr(
        bundle_mod, "EXPECTED_EXECUTION_SNAPSHOT_TREE_SHA256", "0" * 64
    )
    with pytest.raises(bundle_mod.G11BlindBundleError, match="snapshot tree"):
        _create(env)


def test_snapshot_tamper_refusal(env):
    victim = env["snapshot"] / runner.G11_EXECUTION_MANIFEST_FILENAME
    victim.write_bytes(victim.read_bytes() + b"tampered")
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _create(env)


def test_tool_repository_binding_refusal(env, monkeypatch):
    monkeypatch.setattr(bundle_mod, "EXPECTED_TOOL_REPOSITORY_BINDING", "0" * 40)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="repository binding"):
        _create(env)


# --------------------------------------------------------------------------- #
# Source / evidence refusals (direct, on a tampered snapshot copy)
# --------------------------------------------------------------------------- #


def _tampered_snapshot(env, work, name):
    target = work / name
    shutil.copytree(env["snapshot"], target)
    return target


def test_source_libraries_differ_between_model_tracks_refusal(env, work):
    snapshot = _tampered_snapshot(env, work, "snap_divergent")
    path = snapshot / "runs" / GLM_ACE / "data" / "chapters.json"
    data = _read_json(path)
    data[0]["text"] = "divergent source text"
    _write_json(path, data)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="different source"):
        bundle_mod.load_source_libraries(snapshot)


def test_missing_literary_checkpoint_refusal(env, work):
    snapshot = _tampered_snapshot(env, work, "snap_missing_literary")
    shutil.rmtree(
        snapshot / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0001"
    )
    sources = bundle_mod.load_source_libraries(snapshot)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="missing"):
        bundle_mod.build_private_literary_records(snapshot, sources)


def test_literary_checkpoint_sha_mismatch_refusal(env, work):
    snapshot = _tampered_snapshot(env, work, "snap_literary_sha")
    chapter_md = (
        snapshot / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0001" / "chapter.md"
    )
    chapter_md.write_bytes(chapter_md.read_bytes() + b"tampered")
    with pytest.raises(bundle_mod.G11BlindBundleError, match="does not match"):
        bundle_mod.load_literary_evidence(snapshot, GLM_ACE, 1)


def test_state_checkpoint_missing_refusal(env, work):
    snapshot = _tampered_snapshot(env, work, "snap_missing_state")
    target = (
        snapshot
        / runner.G11_EVIDENCE_DIRECTORY
        / GLM_ACE
        / "0025"
        / "audit_history.json"
    )
    target.unlink()
    with pytest.raises(bundle_mod.G11BlindBundleError, match="missing audit_history"):
        bundle_mod.load_state_evidence(snapshot, GLM_ACE, 25)


# --------------------------------------------------------------------------- #
# Public state projection sanitization
# --------------------------------------------------------------------------- #


def test_raw_operational_audit_metadata_never_enters_state_projection():
    raw = {
        "audits": [
            {
                "start_chapter": 1,
                "end_chapter": 25,
                "timestamp_unix": 123.5,
                "stats": {"api_request_count": 3, "cost_usd": 1.5},
                "chunking": {"candidate_count": 2, "total_chunks": 1},
                "changes": {
                    "kept": [{"chapter": 1, "term": "x", "request_sha": "a" * 64}],
                    "corrected": [],
                    "rejected": [],
                    "contextualized": [],
                    "merged": [],
                },
                "retroactive": {"chapters_changed": [2], "replacement_count": 1},
                "issues": [{"chapter": 1, "type": "terminology", "details": "d"}],
            }
        ]
    }
    projection = bundle_mod._audit_history_projection(raw)
    text = json.dumps(projection)
    for forbidden in (
        "timestamp",
        "stats",
        "chunking",
        "cost",
        "api_request_count",
        "request_sha",
        "provider",
        "checkpoint",
    ):
        assert forbidden not in text
    record = projection[0]
    assert record["start_chapter"] == 1
    assert record["end_chapter"] == 25
    assert set(record["changes"]) == {
        "kept",
        "corrected",
        "rejected",
        "contextualized",
        "merged",
    }
    assert record["retroactive"] == {"chapters_changed": [2], "replacement_count": 1}


def test_continuity_projection_strips_operational_keys():
    raw = {
        "latest_chapter": 25,
        "summary": "the hero reaches the capital",
        "provider_notes": {"provider_model": "x"},
        "cost_usd": 3.0,
    }
    projection = bundle_mod._continuity_projection(raw)
    assert projection == {
        "latest_chapter": 25,
        "summary": "the hero reaches the capital",
    }


# --------------------------------------------------------------------------- #
# Tamper detection
# --------------------------------------------------------------------------- #


def _tamper(path):
    path = Path(path)
    path.write_bytes(path.read_bytes() + b"tampered")


def test_public_literary_tamper_detection(env):
    _create(env)
    case = next(
        (env["output"] / bundle_mod.PUBLIC_DIRECTORY / "literary").glob("*.json")
    )
    _tamper(case)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_public_state_tamper_detection(env):
    _create(env)
    case = next(
        (env["output"] / bundle_mod.PUBLIC_DIRECTORY / "longitudinal").glob("*.json")
    )
    _tamper(case)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_private_identity_tamper_detection(env):
    _create(env)
    path = (
        env["output"]
        / bundle_mod.PRIVATE_DIRECTORY
        / bundle_mod.PRIVATE_KEY_FILENAME
    )
    document = _read_json(path)
    document["secret_sha256"] = "0" * 64
    _write_json(path, document)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_private_input_tamper_detection(env):
    _create(env)
    path = (
        env["output"]
        / bundle_mod.PRIVATE_DIRECTORY
        / bundle_mod.PRIVATE_INPUT_FILENAME
    )
    document = _read_json(path)
    document["provider_calls"] = 9
    _write_json(path, document)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_public_manifest_tamper_detection(env):
    _create(env)
    path = (
        env["output"]
        / bundle_mod.PUBLIC_DIRECTORY
        / bundle_mod.PUBLIC_MANIFEST_FILENAME
    )
    _tamper(path)
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_public_tree_extra_file_detection(env):
    _create(env)
    (env["output"] / bundle_mod.PUBLIC_DIRECTORY / "unexpected_extra.bin").write_bytes(
        b"extra"
    )
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_public_tree_missing_file_detection(env):
    _create(env)
    case = next(
        (env["output"] / bundle_mod.PUBLIC_DIRECTORY / "literary").glob("*.json")
    )
    case.unlink()
    with pytest.raises(bundle_mod.G11BlindBundleError):
        _verify(env)


def test_bundle_freeze_id_tamper_detection(env):
    _create(env)
    path = env["output"] / bundle_mod.FREEZE_FILENAME
    document = _read_json(path)
    document["bundle_freeze_id"] = "0" * 64
    _write_json(path, document)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="does not recompute"):
        _verify(env)


def test_safety_flag_tamper_detection(env):
    _create(env)
    path = env["output"] / bundle_mod.FREEZE_FILENAME
    document = _read_json(path)
    document["provider_calls"] = 5
    body = {k: v for k, v in document.items() if k != "bundle_freeze_id"}
    document["bundle_freeze_id"] = _sha_json(body)
    _write_json(path, document)
    with pytest.raises(bundle_mod.G11BlindBundleError, match="safety flag"):
        _verify(env)


# --------------------------------------------------------------------------- #
# Zero provider / zero credential execution
# --------------------------------------------------------------------------- #


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
    assert verification["scoring_performed"] is False
    assert verification["identity_revealed"] is False
