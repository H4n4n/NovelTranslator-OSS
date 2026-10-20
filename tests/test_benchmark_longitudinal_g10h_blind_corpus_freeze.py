"""Focused tests for the G10H zero-provider blind corpus freeze."""

import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g10h_blind_corpus_freeze as h
from benchmark_longitudinal_g10_blind import (
    LONGITUDINAL_AVAILABLE,
    LONGITUDINAL_UNAVAILABLE,
    build_private_identity_key,
    build_public_blind_bundle,
    private_identity_key_commitment,
    validate_public_blind_bundle,
)
from benchmark_longitudinal_g10_chronological import (
    TRACK_SPECS,
    TRACK_BY_ID,
)
from benchmark_longitudinal_g10_matched import PAIR_SPECS
from benchmark_longitudinal_g10_paid_authorization import (
    EXPECTED_FREEZE_ID,
    EXPECTED_MATCHED_PLAN_SHA256,
    EXPECTED_PRICING_VERSION,
    MODEL_RATECARD,
    quarantine_chronological_track,
    quarantine_literary_case,
)
from benchmark_longitudinal_g10_preregistration import (
    G10_BENCHMARK_ID,
    GLM_MODEL_ID,
    MODEL_ORDER,
    NOVELS,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_s0 import G10S0_RUN_IDENTITY_FILENAME
from benchmark_longitudinal_g10f_matched_executor import (
    G10F_EXECUTOR_SHA256,
    G10F_REVISION,
)

SECRET = "1a" * 32

REAL = {
    "repo_root": r"D:\NovelTranslator_G10H_Engineering_20260930_144500",
    "g10e_root": r"D:\NovelTranslator_G10E_Freeze_20260929",
    "matched_root": r"D:\NovelTranslator_G10A_Matched_Evidence_20260929",
    "chronological_root": r"D:\NovelTranslator_G10B_Execution_20260930",
    "s0_root": r"D:\NovelTranslator_G10B_S0_20260929",
    "g10g_control_root": r"D:\NovelTranslator_AgentControl\G10G_Automated_R2_20260930",
}


def _real_roots_present():
    return all(Path(path).is_dir() for path in REAL.values())


requires_real_roots = pytest.mark.skipif(
    not _real_roots_present(), reason="frozen predecessor roots are not present"
)


def _sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(model_id):
    return {QWEN_MODEL_ID: "qwen", GLM_MODEL_ID: "glm"}[model_id]


# --------------------------------------------------------------------------- #
# Literary fixtures
# --------------------------------------------------------------------------- #

# 12 primary pairs become unavailable so the derived counts are 39/12 and 36/3.
_UNAVAILABLE_PRIMARY_INDEXES = frozenset(range(13, 25))


def _sources():
    return {
        (pair.novel_id, pair.chapter): h.FrozenSource(
            novel_id=pair.novel_id,
            chapter=pair.chapter,
            source_text=f"SOURCE::{pair.novel_id}::{pair.chapter}",
            text_sha256=_sha_text(f"SOURCE::{pair.novel_id}::{pair.chapter}"),
        )
        for pair in PAIR_SPECS
    }


def _artifact(pair, model_id, source, execution_index):
    return {
        "schema_version": 1,
        "revision": G10F_REVISION,
        "g10f_executor_sha256": G10F_EXECUTOR_SHA256,
        "benchmark_id": G10_BENCHMARK_ID,
        "arm_id": "G10A",
        "freeze_id": EXPECTED_FREEZE_ID,
        "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
        "pricing_version": EXPECTED_PRICING_VERSION,
        "plan_sha256": "1" * 64,
        "execution_index": execution_index,
        "pair_id": pair.pair_id,
        "pair_index": pair.pair_index,
        "case_kind": pair.case_kind,
        "novel_id": pair.novel_id,
        "chapter": pair.chapter,
        "model_id": model_id,
        "provider_model_id": MODEL_RATECARD[model_id]["provider_model_id"],
        "request_sha256": "2" * 64,
        "journal_operation_id": "3" * 64,
        "messages_sha256": "4" * 64,
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        "content": f"TRANSLATION::{_slug(model_id)}::{pair.pair_id}",
        "text_sha256": source.text_sha256,
        "accepted_credit_equivalent": "0.01",
        "maximum_credit_equivalent": "0.02",
        "provider_calls": 1,
    }


def _artifact_path(root, pair, model_id):
    return (
        Path(root)
        / "artifacts"
        / pair.novel_id
        / _slug(model_id)
        / f"{pair.chapter:04d}.{pair.case_kind}.json"
    )


def _quarantine_path(root, pair, model_id):
    return (
        Path(root)
        / "quarantine"
        / pair.novel_id
        / _slug(model_id)
        / f"{pair.chapter:04d}.{pair.case_kind}.json"
    )


def _build_matched_fixture(root):
    sources = _sources()
    execution_index = 0
    for pair in PAIR_SPECS:
        source = sources[(pair.novel_id, pair.chapter)]
        unavailable = (
            pair.case_kind == "primary"
            and pair.pair_index in _UNAVAILABLE_PRIMARY_INDEXES
        )
        for model_id in MODEL_ORDER:
            execution_index += 1
            if unavailable and model_id == QWEN_MODEL_ID:
                quarantine_literary_case(
                    path=_quarantine_path(root, pair, model_id),
                    model_id=model_id,
                    novel_id=pair.novel_id,
                    chapter=pair.chapter,
                    case_kind=pair.case_kind,
                    request_sha256="5" * 64,
                    reservation_id="6" * 64,
                    reason="test quarantined candidate",
                )
                continue
            _write_json(
                _artifact_path(root, pair, model_id),
                _artifact(pair, model_id, source, execution_index),
            )
    return sources


# --------------------------------------------------------------------------- #
# S0 / chronological fixtures for reference and longitudinal evidence
# --------------------------------------------------------------------------- #


def _chapter_entry(novel_id, chapter):
    return {
        "chapter": chapter,
        "production_index": chapter,
        "heading": f"Chapter {chapter}",
        "title": "",
        "text": f"{novel_id} chapter {chapter} source text",
        "paragraph_count": 1,
        "character_count": 10,
    }


def _run_identity(run_id, source_bindings):
    track = TRACK_BY_ID[run_id]
    identity = {
        "schema_version": 1,
        "revision": "g10-four-track-fresh-s0-v1",
        "benchmark_id": G10_BENCHMARK_ID,
        "run_index": track.run_index,
        "run_id": run_id,
        "relative_root": f"runs/{run_id}",
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "audit_interval": 25,
        "first_chapter": 1,
        "last_chapter": 50,
        "audit_checkpoints": [25, 50],
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "source_bindings": source_bindings,
    }
    identity["identity_sha256"] = _sha_json(identity)
    return identity


def _build_s0_fixture(root):
    for novel_id in NOVELS:
        chapters_path = None
        source_bindings = None
        for track in TRACK_SPECS:
            if track.novel_id != novel_id:
                continue
            run_root = Path(root) / "runs" / track.run_id
            chapters_path = run_root / "data" / "chapters.json"
            if not chapters_path.exists():
                _write_json(
                    chapters_path,
                    [_chapter_entry(novel_id, n) for n in range(1, 51)],
                )
            source_bindings = {"chapter_library_sha256": _sha_file(chapters_path)}
            _write_json(
                run_root / G10S0_RUN_IDENTITY_FILENAME,
                _run_identity(track.run_id, source_bindings),
            )


def _progress(last_completed, last_audited, quarantine_first=None):
    return {
        "last_completed": last_completed,
        "next_chapter": last_completed + 1,
        "last_audited_chapter": last_audited,
    }


def _build_chronological_fixture(root, progress_by_run):
    """progress_by_run maps run_id -> (last_completed, last_audited, blocked|None)."""
    r2_tracks = {}
    for run_id, (last_completed, last_audited, blocked) in progress_by_run.items():
        run_root = Path(root) / "runs" / run_id
        source_bindings = {"chapter_library_sha256": "7" * 64}
        _write_json(
            run_root / G10S0_RUN_IDENTITY_FILENAME,
            _run_identity(run_id, source_bindings),
        )
        _write_json(
            run_root / "data" / "progress.json",
            _progress(last_completed, last_audited),
        )
        quarantine = None
        if blocked is not None:
            track = TRACK_BY_ID[run_id]
            quarantine_chronological_track(
                path=run_root / "data" / "g10_quarantine.json",
                model_id=track.model_id,
                novel_id=track.novel_id,
                run_id=run_id,
                first_blocked_chapter=blocked,
                request_sha256="8" * 64,
                reservation_id="9" * 64,
                reason="test track quarantine",
            )
            quarantine = {"first_blocked_chapter": blocked}
        r2_tracks[run_id] = {
            "last_completed": last_completed,
            "next_chapter": last_completed + 1,
            "last_audited_chapter": last_audited,
            "quarantine": quarantine,
        }
    return r2_tracks


def _reference_evidence():
    result = {}
    for novel_id in sorted(NOVELS):
        for checkpoint in (25, 50):
            window = [1, 25] if checkpoint == 25 else [26, 50]
            hashes = {str(n): _sha_text(f"{novel_id}:{n}") for n in range(window[0], window[1] + 1)}
            result[(novel_id, checkpoint)] = {
                "reference_revision": h.REFERENCE_EVIDENCE_REVISION,
                "novel_id": novel_id,
                "checkpoint": checkpoint,
                "audit_window": window,
                "chapter_source_sha256": hashes,
                "window_source_sha256": _sha_json(hashes),
            }
    return result


# --------------------------------------------------------------------------- #
# Zero-provider / reveal-free contract
# --------------------------------------------------------------------------- #


def test_contract_is_zero_provider_and_reveal_free():
    h.assert_paid_execution_blocked()
    assert not hasattr(h, "reveal_frozen_scores")
    assert not hasattr(h, "freeze_anonymous_scores")
    source = inspect.getsource(h)
    assert "getenv" not in source
    assert "os.environ" not in source


def test_core_pipeline_builds_no_provider_client(monkeypatch, tmp_path):
    import benchmark_longitudinal_provider as provider

    def _boom(*_args, **_kwargs):
        raise AssertionError("G10H must not construct a Provider client")

    monkeypatch.setattr(provider, "create_provider_client", _boom)
    monkeypatch.setattr(provider, "send_chat_completion_once", _boom)

    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    literary = h.build_literary_records(matched_root=matched, sources=sources)
    key = build_private_identity_key(SECRET)
    bundle = h._build_public_bundle(key, literary, {"records": _plain_longitudinal_records()})
    assert bundle["provider_calls"] == 0
    assert literary["document"]["provider_calls"] == 0


def _plain_longitudinal_records():
    rows = []
    for novel_id in sorted(NOVELS):
        for checkpoint in (25, 50):
            rows.append(
                {
                    "novel_id": novel_id,
                    "checkpoint": checkpoint,
                    "audit_window": [1, 25] if checkpoint == 25 else [26, 50],
                    "reference_evidence": _reference_evidence()[(novel_id, checkpoint)],
                    "availability": LONGITUDINAL_UNAVAILABLE,
                    "tracks": {},
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# Literary rows
# --------------------------------------------------------------------------- #


def test_literary_counts_are_derived_and_preserved(tmp_path):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    result = h.build_literary_records(matched_root=matched, sources=sources)
    assert result["counts"] == {
        "literary_planned": 51,
        "literary_scoreable": 39,
        "literary_unavailable": 12,
        "primary_scoreable": 36,
        "bridge_scoreable": 3,
    }
    assert len(result["records"]) == 51
    assert {row["pair_id"] for row in result["records"]} == {
        pair.pair_id for pair in PAIR_SPECS
    }


def test_literary_fails_closed_when_counts_drift(tmp_path):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    victim = _artifact_path(matched, PAIR_SPECS[0], QWEN_MODEL_ID)
    victim.unlink()
    with pytest.raises(h.G10HFreezeError):
        h.build_literary_records(matched_root=matched, sources=sources)


def test_unavailable_candidate_is_never_exposed_publicly(tmp_path):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    literary = h.build_literary_records(matched_root=matched, sources=sources)
    # The unavailable private rows still carry the one accepted partner translation.
    unavailable = [
        row for row in literary["records"] if row["availability"] != "paired_translation_available"
    ]
    assert len(unavailable) == 12
    assert all(len(row["translations"]) == 1 for row in unavailable)

    key = build_private_identity_key(SECRET)
    bundle = build_public_blind_bundle(
        private_key=key,
        literary_records=literary["records"],
        longitudinal_records=_plain_longitudinal_records(),
    )
    validate_public_blind_bundle(bundle)
    for row in bundle["literary_pairs"]:
        if row["availability"] != "paired_translation_available":
            assert "side_a" not in row and "side_b" not in row
        else:
            assert "side_a" in row and "side_b" in row
    assert sum("side_a" in row for row in bundle["literary_pairs"]) == 39


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", QWEN_MODEL_ID),
        ("pair_id", "G10A-P999"),
        ("text_sha256", "0" * 64),
        ("messages_sha256", "not-a-sha"),
        ("provider_calls", 0),
        ("matched_plan_sha256", "0" * 64),
        ("content", ""),
        ("execution_index", 0),
    ],
)
def test_tampered_accepted_artifact_is_rejected(tmp_path, field, value):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    pair = PAIR_SPECS[0]
    path = _artifact_path(matched, pair, GLM_MODEL_ID)
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if field == "model_id":
        artifact[field] = QWEN_MODEL_ID
    else:
        artifact[field] = value
    _write_json(path, artifact)
    with pytest.raises(h.G10HFreezeError):
        h.build_literary_records(matched_root=matched, sources=sources)


def test_source_drift_between_library_and_artifact_is_rejected(tmp_path):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    drifted = dict(sources)
    pair = PAIR_SPECS[0]
    drifted[(pair.novel_id, pair.chapter)] = h.FrozenSource(
        novel_id=pair.novel_id,
        chapter=pair.chapter,
        source_text="DIFFERENT SOURCE",
        text_sha256=_sha_text("DIFFERENT SOURCE"),
    )
    with pytest.raises(h.G10HFreezeError):
        h.build_literary_records(matched_root=matched, sources=drifted)


# --------------------------------------------------------------------------- #
# Longitudinal rows
# --------------------------------------------------------------------------- #


def _equal_progress(last_completed, last_audited):
    return {track.run_id: (last_completed, last_audited, None) for track in TRACK_SPECS}


def test_checkpoint_requires_both_model_tracks(tmp_path):
    root = tmp_path / "chron"
    progress = _equal_progress(25, 25)
    r2 = _build_chronological_fixture(root, progress)
    records, counts = h.build_longitudinal_rows(
        chronological_root=root,
        s0_root=tmp_path,  # unused by the row builder
        reference_evidence=_reference_evidence(),
        r2_tracks=r2,
    )
    by_key = {(row["novel_id"], row["checkpoint"]): row for row in records}
    assert counts == {
        "longitudinal_planned": 4,
        "longitudinal_scoreable": 2,
        "longitudinal_unavailable": 2,
    }
    assert by_key[("ace_evolution", 25)]["availability"] == LONGITUDINAL_AVAILABLE
    assert set(by_key[("ace_evolution", 25)]["tracks"]) == set(MODEL_ORDER)
    assert by_key[("ace_evolution", 50)]["availability"] == LONGITUDINAL_UNAVAILABLE
    assert by_key[("ace_evolution", 50)]["tracks"] == {}

    # Drop one model track below the checkpoint: the row becomes unavailable.
    root2 = tmp_path / "chron2"
    progress2 = _equal_progress(25, 25)
    progress2[h.RUN_ID_BY_TRACK[(QWEN_MODEL_ID, "ace_evolution")]] = (10, 0, None)
    r2b = _build_chronological_fixture(root2, progress2)
    records_b, _ = h.build_longitudinal_rows(
        chronological_root=root2,
        s0_root=tmp_path,
        reference_evidence=_reference_evidence(),
        r2_tracks=r2b,
    )
    by_key_b = {(row["novel_id"], row["checkpoint"]): row for row in records_b}
    assert by_key_b[("ace_evolution", 25)]["availability"] == LONGITUDINAL_UNAVAILABLE
    assert by_key_b[("ace_evolution", 25)]["tracks"] == {}


def test_quarantined_track_is_never_comparable(tmp_path):
    root = tmp_path / "chron"
    qwen_ace = h.RUN_ID_BY_TRACK[(QWEN_MODEL_ID, "ace_evolution")]
    progress = _equal_progress(25, 25)
    progress[qwen_ace] = (3, 0, 4)
    r2 = _build_chronological_fixture(root, progress)
    records, _ = h.build_longitudinal_rows(
        chronological_root=root,
        s0_root=tmp_path,
        reference_evidence=_reference_evidence(),
        r2_tracks=r2,
    )
    by_key = {(row["novel_id"], row["checkpoint"]): row for row in records}
    assert by_key[("ace_evolution", 25)]["availability"] == LONGITUDINAL_UNAVAILABLE


def test_tampered_durable_progress_is_rejected(tmp_path):
    root = tmp_path / "chron"
    progress = _equal_progress(25, 25)
    r2 = _build_chronological_fixture(root, progress)
    qwen_ace = h.RUN_ID_BY_TRACK[(QWEN_MODEL_ID, "ace_evolution")]
    _write_json(
        Path(root) / "runs" / qwen_ace / "data" / "progress.json",
        _progress(26, 25),
    )
    with pytest.raises(h.G10HFreezeError):
        h.build_longitudinal_rows(
            chronological_root=root,
            s0_root=tmp_path,
            reference_evidence=_reference_evidence(),
            r2_tracks=r2,
        )


def test_longitudinal_fails_closed_when_outcome_drifts(tmp_path):
    root = tmp_path / "chron"
    progress = _equal_progress(25, 25)
    r2 = _build_chronological_fixture(root, progress)
    with pytest.raises(h.G10HFreezeError):
        h.build_longitudinal_records(
            chronological_root=root,
            s0_root=tmp_path,
            reference_evidence=_reference_evidence(),
            r2_tracks=r2,
        )


# --------------------------------------------------------------------------- #
# Reference evidence / S0 binding
# --------------------------------------------------------------------------- #


def test_reference_evidence_covers_full_windows(tmp_path):
    s0 = tmp_path / "s0"
    _build_s0_fixture(s0)
    reference = h.build_reference_evidence(s0_root=s0)
    assert set(reference) == {
        (novel_id, checkpoint)
        for novel_id in NOVELS
        for checkpoint in (25, 50)
    }
    assert len(reference[("ace_evolution", 25)]["chapter_source_sha256"]) == 25
    assert len(reference[("ace_evolution", 50)]["chapter_source_sha256"]) == 25
    assert reference[("ace_evolution", 25)]["audit_window"] == [1, 25]


def test_reference_source_drift_is_rejected(tmp_path):
    s0 = tmp_path / "s0"
    _build_s0_fixture(s0)
    run_id = next(
        track.run_id for track in TRACK_SPECS if track.novel_id == "ace_evolution"
    )
    chapters = Path(s0) / "runs" / run_id / "data" / "chapters.json"
    entries = json.loads(chapters.read_text(encoding="utf-8"))
    entries[0]["text"] = "tampered source text"
    _write_json(chapters, entries)
    with pytest.raises(h.G10HFreezeError):
        h.build_reference_evidence(s0_root=s0)


# --------------------------------------------------------------------------- #
# Public bundle leak scan / private key separation
# --------------------------------------------------------------------------- #


def _public_bundle(tmp_path):
    matched = tmp_path / "matched"
    sources = _build_matched_fixture(matched)
    literary = h.build_literary_records(matched_root=matched, sources=sources)
    key = build_private_identity_key(SECRET)
    bundle = build_public_blind_bundle(
        private_key=key,
        literary_records=literary["records"],
        longitudinal_records=_plain_longitudinal_records(),
    )
    return key, bundle


def test_private_key_is_not_in_public_output(tmp_path):
    key, bundle = _public_bundle(tmp_path)
    canonical = h._canonical_json(bundle)
    assert SECRET not in canonical
    assert "secret_sha256" not in canonical
    assert "literary_mapping" not in canonical
    assert "longitudinal_mapping" not in canonical
    assert bundle["identity_revealed"] is False
    assert bundle["provider_calls"] == 0
    assert bundle["private_identity_key_commitment_sha256"] == (
        private_identity_key_commitment(key)
    )
    h.assert_no_public_identity_leak(bundle)


@pytest.mark.parametrize(
    "literal",
    [
        "qwen/qwen3.8-max-0902",
        "Qwen/Qwen3.8-Max-0902",
        "z-ai/glm-5.3-flash",
        "reasoning_effort",
        "credit_equivalent",
        "cost_usd",
        "provider_model",
        "request_sha",
        "reservation_id",
    ],
)
def test_public_literal_identity_leak_detected(tmp_path, literal):
    _key, bundle = _public_bundle(tmp_path)
    leaked = copy.deepcopy(bundle)
    leaked["statistics_policy"][literal] = 1
    with pytest.raises(h.G10HIdentityLeakError):
        h.assert_no_public_identity_leak(leaked)


def test_public_bare_model_token_detected_outside_prose(tmp_path):
    _key, bundle = _public_bundle(tmp_path)
    leaked = copy.deepcopy(bundle)
    leaked["statistics_policy"]["extra"] = "use glm here"
    with pytest.raises(h.G10HIdentityLeakError):
        h.assert_no_public_identity_leak(leaked)


def test_public_story_prose_is_not_a_false_positive(tmp_path):
    _key, bundle = _public_bundle(tmp_path)
    benign = copy.deepcopy(bundle)
    benign["literary_pairs"][0]["side_a"]["translation"] = "the qwen and glm story text"
    h.assert_no_public_identity_leak(benign)


# --------------------------------------------------------------------------- #
# Real frozen evidence integration
# --------------------------------------------------------------------------- #


@requires_real_roots
def test_real_longitudinal_outcome_is_all_unavailable():
    predecessors = h.verify_predecessors(**REAL)
    records, counts = h.build_longitudinal_rows(
        chronological_root=REAL["chronological_root"],
        s0_root=REAL["s0_root"],
        reference_evidence=h.build_reference_evidence(s0_root=REAL["s0_root"]),
        r2_tracks=predecessors["r2"]["tracks"],
    )
    assert counts == {
        "longitudinal_planned": 4,
        "longitudinal_scoreable": 0,
        "longitudinal_unavailable": 4,
    }
    assert all(row["tracks"] == {} for row in records)
    assert all(row["availability"] == LONGITUDINAL_UNAVAILABLE for row in records)


@requires_real_roots
def test_real_freeze_create_verify_idempotent_and_tamper(tmp_path):
    out = tmp_path / "G10H"
    created = h.create_freeze(output_root=out, **REAL)
    assert created["literary_planned"] == 51
    assert created["literary_scoreable"] == 39
    assert created["literary_unavailable"] == 12
    assert created["primary_scoreable"] == 36
    assert created["bridge_scoreable"] == 3
    assert created["longitudinal_scoreable"] == 0
    assert created["longitudinal_unavailable"] == 4
    assert created["provider_calls"] == 0
    assert created["credential_access"] is False

    verified = h.verify_freeze(output_root=out, **REAL)
    assert verified == created

    before = {p.name: _sha_file(p) for p in out.iterdir()}
    h.verify_freeze(output_root=out, **REAL)
    after = {p.name: _sha_file(p) for p in out.iterdir()}
    assert before == after

    # Public bundle tamper.
    public_path = out / h.PUBLIC_BUNDLE_FILENAME
    original_public = public_path.read_bytes()
    tampered_public = json.loads(original_public.decode("utf-8"))
    tampered_public["identity_revealed"] = True
    _write_json(public_path, tampered_public)
    with pytest.raises(h.G10HFreezeError):
        h.verify_freeze(output_root=out, **REAL)
    public_path.write_bytes(original_public)

    # Freeze file tamper.
    freeze_path = out / h.FREEZE_FILENAME
    original_freeze = freeze_path.read_bytes()
    tampered_freeze = json.loads(original_freeze.decode("utf-8"))
    tampered_freeze["counts"]["literary_planned"] = 52
    _write_json(freeze_path, tampered_freeze)
    with pytest.raises(h.G10HFreezeError):
        h.verify_freeze(output_root=out, **REAL)
    freeze_path.write_bytes(original_freeze)

    # Private input tamper.
    literary_path = out / h.LITERARY_PRIVATE_FILENAME
    original_literary = literary_path.read_bytes()
    tampered_literary = json.loads(original_literary.decode("utf-8"))
    tampered_literary["counts"]["literary_scoreable"] = 40
    _write_json(literary_path, tampered_literary)
    with pytest.raises(h.G10HFreezeError):
        h.verify_freeze(output_root=out, **REAL)
    literary_path.write_bytes(original_literary)

    assert h.verify_freeze(output_root=out, **REAL) == created

    bundle = json.loads((out / h.PUBLIC_BUNDLE_FILENAME).read_text(encoding="utf-8"))
    h.assert_no_public_identity_leak(bundle)
    assert bundle["identity_revealed"] is False
    assert bundle["provider_calls"] == 0
    assert sum("side_a" in row for row in bundle["literary_pairs"]) == 39
    assert all("side_a" not in row for row in bundle["longitudinal_pairs"])

