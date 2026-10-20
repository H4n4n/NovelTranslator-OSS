"""Zero-Provider tests for the G7S durable Stage-1 quarantine marker.

Every test here is local and synthetic: no test constructs a Provider client,
reads a credential, reads ``.env``, opens a network socket, writes a real Stage-1
run root, or executes a paid chapter.  G7S markers in this suite exist only inside
``tmp_path``.

The fixtures are deliberately minimal but faithful: each synthetic run carries a
genuinely self-verified ``g7c_run_identity.json`` (the real ``SOURCE_BUNDLE_SEMANTIC_FIELDS``
copied from the frozen G7C run specs with a recomputed ``identity_sha256``) and a
canonical target-chapter invocation-ledger stub with the exact durable fields the
marker binds.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import importlib.util
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest

import benchmark_longitudinal_g7c_stage1_integration as g7c
import benchmark_longitudinal_g7s_stage1_durable_quarantine as g7s


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "benchmark_longitudinal_g7s_stage1_durable_quarantine.py"
)

SPECS = {
    index: spec for index, spec in enumerate(g7c.stage1_run_specs(), start=1)
}

RUN_INDEX = 3
BLOCKED_CHAPTER = 3
SENDS = 4
ACCEPTED = "1.5"
STABLE_SHA = "a" * 64
CONTROLLER_SHA = "b" * 64
OTHER_CONTROLLER_SHA = "1" * 64
REPOSITORY_HEAD = "9deaf11107f9ef43efd56b95ba2b30f7be61f57a"
WAVE_BUNDLE_SHA = "f" * 64
PLAN_SHA = "c" * 64
APPROVAL_TOKEN = "d" * 64
LEDGER_REVISION = "g7f-persisted-invocation-budget-v1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_json(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _run_root(base: Path, index: int) -> Path:
    return base / SPECS[index]["relative_root"]


def _write_identity(
    run_root: Path, index: int, overrides=None
) -> Path:
    """Write a genuinely self-verified G7C run identity for one run."""
    run_root.mkdir(parents=True, exist_ok=True)
    identity = {
        "schema_version": 1,
        "g7c_revision": "goat-g7c-stage1-execution-integration-v1",
        "g7c_integration_sha256": g7c.G7C_INTEGRATION_SHA256,
        **{
            field: (
                copy.deepcopy(SPECS[index][field])
                if field in SPECS[index]
                else STABLE_SHA
            )
            for field in g7s.SOURCE_BUNDLE_SEMANTIC_FIELDS
        },
    }
    identity.update(overrides or {})
    identity["identity_sha256"] = _sha256_json(identity)
    path = run_root / "g7c_run_identity.json"
    path.write_text(
        json.dumps(identity, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def _ledger_records(sends: int) -> list:
    records = []
    for index in range(1, sends + 1):
        records.append(
            {
                "send_index": index,
                "logical_operation": "translation",
                "chapter": BLOCKED_CHAPTER,
                "request_sha256": f"{index:064d}",
                "request_started_utc": "2026-09-25T00:00:00+00:00",
                "status": "accepted",
                "conservative_cost_bound_usd": "0.01",
                "accepted_cost_usd": "0",
                "stats": {},
                "error": {},
            }
        )
    return records


def _ledger_document(index: int, **overrides):
    document = {
        "schema_version": 1,
        "revision": LEDGER_REVISION,
        "plan_sha256": PLAN_SHA,
        "approval_token": APPROVAL_TOKEN,
        "run_index": SPECS[index]["run_index"],
        "relative_root": SPECS[index]["relative_root"],
        "model_id": SPECS[index]["model_id"],
        "novel_id": SPECS[index]["novel_id"],
        "target_chapter": BLOCKED_CHAPTER,
        "maximum_new_provider_sends": 8,
        "maximum_credit_equivalent_usd": "10",
        "new_provider_sends_started": SENDS,
        "accepted_credit_equivalent_usd": ACCEPTED,
        "records": _ledger_records(SENDS),
        "created_utc": "2026-09-25T00:00:00+00:00",
        "updated_utc": "2026-09-25T00:00:00+00:00",
    }
    document.update(overrides)
    document["ledger_sha256"] = _sha256_json(
        {key: value for key, value in document.items() if key != "ledger_sha256"}
    )
    return document


def _write_ledger(run_root: Path, document) -> Path:
    ledger_dir = run_root / "data" / "g7f_invocation_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{int(document['target_chapter']):04d}.json"
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def _ledger_path_for(run_root: Path, chapter: int = BLOCKED_CHAPTER) -> Path:
    return (
        run_root
        / "data"
        / "g7f_invocation_ledgers"
        / f"{int(chapter):04d}.json"
    )


def _rehash_ledger(document):
    body = {key: value for key, value in document.items() if key != "ledger_sha256"}
    document["ledger_sha256"] = _sha256_json(body)
    return document


def _fixture_run(
    tmp_path: Path,
    index: int = RUN_INDEX,
    *,
    overrides=None,
    sends: int = SENDS,
    accepted=ACCEPTED,
):
    base = tmp_path / "stage1"
    run_root = _run_root(base, index)
    _write_identity(run_root, index, overrides)
    (run_root / "data").mkdir(parents=True, exist_ok=True)
    _write_ledger(
        run_root,
        _ledger_document(
            index,
            new_provider_sends_started=sends,
            accepted_credit_equivalent_usd=accepted,
            records=_ledger_records(sends),
        ),
    )
    return base, run_root


def _build(base: Path, run_root: Path, index: int = RUN_INDEX, **overrides):
    kwargs = {
        "run_root": run_root,
        "spec": SPECS[index],
        "first_blocked_chapter": BLOCKED_CHAPTER,
        "source_outcome": g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE,
        "reason": "  paid boundary corrupt  ",
        "provider_sends_started": SENDS,
        "accepted_credit_equivalent_usd": ACCEPTED,
        "run_source_identity_semantic_sha256": (
            g7s.run_source_identity_semantic_sha256(run_root)
        ),
        "source_wave_bundle_sha256": WAVE_BUNDLE_SHA,
        "g7r_controller_sha256": CONTROLLER_SHA,
        "created_utc": "2026-09-25T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return g7s.build_durable_quarantine_marker(**kwargs)


def _mark(base: Path, index: int = RUN_INDEX, **overrides):
    kwargs = {
        "stage1_root": base,
        "run_index": index,
        "first_blocked_chapter": BLOCKED_CHAPTER,
        "provider_sends_started": SENDS,
        "accepted_credit_equivalent_usd": ACCEPTED,
        "source_wave_bundle_sha256": WAVE_BUNDLE_SHA,
        "g7r_controller_sha256": CONTROLLER_SHA,
        "reason": "paid boundary corrupt",
    }
    kwargs.update(overrides)
    return g7s.mark_durable_quarantine(**kwargs)


def _write_marker(run_root: Path, marker) -> Path:
    path = g7s.durable_quarantine_marker_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marker, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _set(**values):
    def mutate(marker):
        marker.update(values)

    return mutate


def _drop(field):
    def mutate(marker):
        marker.pop(field)

    return mutate


def _rehash(marker):
    marker["marker_sha256"] = g7s.compute_marker_sha256(marker)


def _rehash_evidence(marker):
    evidence = marker["evidence"]
    evidence["evidence_fingerprint_sha256"] = _sha256_json(
        {"revision": evidence["revision"], "references": evidence["references"]}
    )
    return marker


# ---------------------------------------------------------------------------
# T1/T2. import safety and entry point
# ---------------------------------------------------------------------------


def test_import_is_zero_write_zero_network_zero_credential(
    monkeypatch, tmp_path
):
    before = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
    )

    def forbid(*_args, **_kwargs):
        raise AssertionError("module import must not perform this operation")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in ("openai", "dotenv"):
            raise AssertionError("import must not load the provider SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.chdir(tmp_path)

    unique = f"_g7s_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
        after = sorted(
            str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
        )
        assert after == before == []
        assert callable(module.main)
        assert callable(module.build_parser)
        assert callable(module.mark_durable_quarantine)
        assert callable(module.inspect_durable_quarantine_marker)
        assert module.G7S_REVISION == "goat-g7s-stage1-durable-quarantine-v2"
        assert module.G7S_POLICY_SHA256 == g7s.G7S_POLICY_SHA256
    finally:
        sys.modules.pop(unique, None)


def test_module_has_executable_main_guard():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source


# ---------------------------------------------------------------------------
# T3/T4. policy identity and marker path
# ---------------------------------------------------------------------------


def test_policy_identity_is_deterministic_and_zero_provider():
    identity = g7s.g7s_policy_identity()

    assert identity == g7s.g7s_policy_identity()
    assert identity["provider_calls"] == 0
    assert identity["schema_version"] == g7s.G7S_SCHEMA_VERSION == 2
    assert identity["revision"] == g7s.G7S_REVISION == (
        "goat-g7s-stage1-durable-quarantine-v2"
    )
    assert identity["run_source_identity_semantic_revision"] == (
        "g7s-run-source-identity-semantic-v2"
    )
    assert identity["repository_anchor"] == g7s.G7S_REPOSITORY_ANCHOR == (
        "adf261379089fe8668dbabe659e69b5f463ae2b5"
    )
    assert identity["marker_relative_path"] == g7s.MARKER_RELATIVE_PATH == (
        "data/g7s_durable_quarantine.json"
    )
    assert identity["allowed_source_outcomes"] == [
        "corrupt_or_unknown_durable_state"
    ]
    assert g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE == (
        "corrupt_or_unknown_durable_state"
    )
    assert g7s.ALLOWED_SOURCE_OUTCOMES == frozenset(
        {"corrupt_or_unknown_durable_state"}
    )
    assert g7s.MARKER_STATE_ABSENT == "absent"
    assert g7s.MARKER_STATE_VALID == "valid"
    assert g7s.MARKER_STATE_INVALID == "invalid"
    assert g7s.G7S_POLICY_SHA256 == _sha256_json(g7s.g7s_policy_identity())
    assert len(g7s.G7S_POLICY_SHA256) == 64
    assert set(g7s.MARKER_REQUIRED_FIELDS) == {
        "schema_version",
        "revision",
        "run_index",
        "relative_root",
        "model_id",
        "novel_id",
        "first_blocked_chapter",
        "source_outcome",
        "reason",
        "provider_sends_started",
        "accepted_credit_equivalent_usd",
        "run_source_identity_semantic_sha256",
        "source_wave_bundle_sha256",
        "run_identity_sha256",
        "g7r_controller_sha256",
        "g7s_policy_sha256",
        "evidence",
        "created_utc",
        "marker_sha256",
    }
    semantics = identity["semantics"]
    assert semantics["first_blocked_chapter_blocks_target_and_later"] is True
    assert semantics["blocked_chapter_never_advances_to_next"] is True
    assert semantics["invalid_marker_fails_closed_as_corrupt"] is True
    assert semantics["create_once_never_overwrite"] is True
    assert semantics["atomic_create_once_publication"] is True
    assert semantics["observing_controller_must_be_explicit"] is True
    assert semantics["blocked_chapter_canonical_ledger_required"] is True
    assert semantics["fake_paid_request_journal_state_forbidden"] is True
    assert semantics["status_and_plan_remain_read_only"] is True


def test_marker_path_is_deterministic(tmp_path):
    assert g7s.MARKER_FILENAME == "g7s_durable_quarantine.json"
    assert g7s.MARKER_RELATIVE_PATH == f"data/{g7s.MARKER_FILENAME}"
    assert g7s.durable_quarantine_marker_path(tmp_path) == (
        tmp_path / "data" / "g7s_durable_quarantine.json"
    )
    assert g7s.durable_quarantine_marker_path(str(tmp_path)) == (
        tmp_path / "data" / "g7s_durable_quarantine.json"
    )


# ---------------------------------------------------------------------------
# T5. marker assembly binds identity / evidence / ledger and stays pure
# ---------------------------------------------------------------------------


def test_build_binds_run_identity_evidence_and_ledger_without_writing(
    tmp_path,
):
    _base, run_root = _fixture_run(tmp_path)
    before = _tree_hash(run_root)

    marker = _build(_base, run_root)

    assert _tree_hash(run_root) == before
    assert not g7s.durable_quarantine_marker_path(run_root).exists()
    assert set(marker) == set(g7s.MARKER_REQUIRED_FIELDS) | {
        "repository_head"
    }
    assert marker["schema_version"] == g7s.G7S_SCHEMA_VERSION
    assert marker["revision"] == g7s.G7S_REVISION
    assert marker["run_index"] == RUN_INDEX
    assert marker["relative_root"] == SPECS[RUN_INDEX]["relative_root"]
    assert marker["model_id"] == SPECS[RUN_INDEX]["model_id"]
    assert marker["novel_id"] == SPECS[RUN_INDEX]["novel_id"]
    assert marker["first_blocked_chapter"] == BLOCKED_CHAPTER
    assert marker["source_outcome"] == (
        g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE
    )
    # The operator's reason is stripped, never silently rewritten.
    assert marker["reason"] == "paid boundary corrupt"
    assert marker["provider_sends_started"] == SENDS
    assert marker["accepted_credit_equivalent_usd"] == "1.5"
    assert marker["run_source_identity_semantic_sha256"] == (
        g7s.run_source_identity_semantic_sha256(run_root)
    )
    assert marker["source_wave_bundle_sha256"] == WAVE_BUNDLE_SHA
    assert marker["run_identity_sha256"] == (
        g7s.run_identity_sha256(run_root)
    )
    assert marker["g7r_controller_sha256"] == CONTROLLER_SHA
    assert marker["g7s_policy_sha256"] == g7s.G7S_POLICY_SHA256
    assert marker["evidence"]["revision"] == "g7s-marker-evidence-v2"
    assert marker["evidence"]["references"]["target_chapter_ledger"][
        "target_chapter"
    ] == BLOCKED_CHAPTER
    assert marker["evidence"]["references"]["target_chapter_ledger"][
        "new_provider_sends_started"
    ] == SENDS
    assert marker["created_utc"] == "2026-09-25T00:00:00+00:00"
    assert marker["repository_head"] is None
    assert marker["marker_sha256"] == g7s.compute_marker_sha256(marker)
    # The build is deterministic and pure: the same inputs rebuild identically.
    assert _build(_base, run_root) == marker
    assert g7s.marker_semantic_payload(marker) == {
        key: value
        for key, value in marker.items()
        if key != "marker_sha256"
    }
    assert marker["evidence"]["evidence_fingerprint_sha256"] == (
        g7s.capture_quarantine_evidence(run_root, BLOCKED_CHAPTER)[
            "evidence_fingerprint_sha256"
        ]
    )


# ---------------------------------------------------------------------------
# T6. an untruthful source outcome is rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "uncertain",
        "started",
        "",
        None,
        "corrupt_or_unknown_durable_state_v2",
        "ambiguous_transport",
    ],
)
def test_untruthful_source_outcome_is_rejected(tmp_path, outcome):
    base, run_root = _fixture_run(tmp_path)

    with pytest.raises(g7s.G7SDurableQuarantineError, match="source_outcome"):
        _build(base, run_root, source_outcome=outcome)

    assert not g7s.durable_quarantine_marker_path(run_root).exists()


# ---------------------------------------------------------------------------
# T7. durable ledger disagreement is rejected
# ---------------------------------------------------------------------------


def test_ledger_disagreement_is_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    with pytest.raises(g7s.G7SDurableQuarantineError, match="sends_started"):
        _build(base, run_root, provider_sends_started=SENDS + 1)
    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="accepted_credit_equivalent_usd"
    ):
        _build(base, run_root, accepted_credit_equivalent_usd="2.5")

    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_ledger_bound_to_another_chapter_is_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    # A ledger whose durable target_chapter contradicts the chapter it is cited
    # for can never be the blocked-chapter evidence.
    ledger_path = _ledger_path_for(run_root)
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    document["target_chapter"] = BLOCKED_CHAPTER + 1
    _rehash_ledger(document)
    ledger_path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(g7s.G7SDurableQuarantineError, match="another chapter"):
        _build(base, run_root)


# ---------------------------------------------------------------------------
# T8. wrong run identity / source bundle is rejected
# ---------------------------------------------------------------------------


def test_wrong_run_identity_and_source_bundle_are_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="expected run identity"
    ):
        _build(
            base,
            run_root,
            expected_run_identity_sha256="0" * 64,
        )
    with pytest.raises(
        g7s.G7SDurableQuarantineError,
        match="run source identity semantic SHA",
    ):
        _build(base, run_root, run_source_identity_semantic_sha256="0" * 64)

    other = tmp_path / "other"
    other_base, other_root = _fixture_run(
        other, overrides={"model_id": "another/model"}
    )
    with pytest.raises(g7s.G7SDurableQuarantineError, match="model_id"):
        _build(other_base, other_root)

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="disagrees with the selected run"
    ):
        _build(base, run_root, spec=SPECS[RUN_INDEX + 1])
    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_missing_run_root_is_rejected(tmp_path):
    base = tmp_path / "stage1"
    missing = _run_root(base, RUN_INDEX)
    assert not missing.exists()

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="run root is missing"
    ):
        g7s.build_durable_quarantine_marker(
            run_root=missing,
            spec=SPECS[RUN_INDEX],
            first_blocked_chapter=BLOCKED_CHAPTER,
            source_outcome=g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE,
            reason="paid boundary corrupt",
            provider_sends_started=SENDS,
            accepted_credit_equivalent_usd=ACCEPTED,
            run_source_identity_semantic_sha256=STABLE_SHA,
            source_wave_bundle_sha256=WAVE_BUNDLE_SHA,
            g7r_controller_sha256=CONTROLLER_SHA,
        )

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="cannot read G7C run identity"
    ):
        _build(base, missing)


# ---------------------------------------------------------------------------
# T9. malformed marking inputs are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"first_blocked_chapter": 0}, id="zero-chapter"),
        pytest.param({"first_blocked_chapter": "3"}, id="string-chapter"),
        pytest.param({"first_blocked_chapter": True}, id="bool-chapter"),
        pytest.param({"reason": "   "}, id="blank-reason"),
        pytest.param({"reason": None}, id="null-reason"),
        pytest.param(
            {"provider_sends_started": -1}, id="negative-sends"
        ),
        pytest.param(
            {"provider_sends_started": "4"}, id="string-sends"
        ),
        pytest.param(
            {"accepted_credit_equivalent_usd": "-1"}, id="negative-cost"
        ),
        pytest.param(
            {"accepted_credit_equivalent_usd": "not-a-number"},
            id="non-numeric-cost",
        ),
        pytest.param(
            {"accepted_credit_equivalent_usd": "nan"}, id="nan-cost"
        ),
        pytest.param(
            {"g7r_controller_sha256": "zz"}, id="malformed-controller"
        ),
        pytest.param({"g7r_controller_sha256": None}, id="null-controller"),
        pytest.param({"created_utc": ""}, id="blank-created-utc"),
        pytest.param({"created_utc": 5}, id="non-string-created-utc"),
        pytest.param({"repository_head": "abc"}, id="short-repo-head"),
        pytest.param(
            {"repository_head": "A" * 40}, id="uppercase-repo-head"
        ),
    ],
)
def test_malformed_build_inputs_are_rejected(tmp_path, overrides):
    base, run_root = _fixture_run(tmp_path)

    with pytest.raises(g7s.G7SDurableQuarantineError):
        _build(base, run_root, **overrides)

    assert not g7s.durable_quarantine_marker_path(run_root).exists()


@pytest.mark.parametrize("run_index", [0, -1, 13, "3", True])
def test_mark_rejects_an_invalid_run_index(tmp_path, run_index):
    base, run_root = _fixture_run(tmp_path)

    with pytest.raises(g7s.G7SDurableQuarantineError, match="run_index"):
        _mark(base, run_index)

    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_repository_head_is_recorded_when_supplied(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    marker = _build(base, run_root, repository_head=REPOSITORY_HEAD)

    assert marker["repository_head"] == REPOSITORY_HEAD
    assert set(marker) == set(g7s.MARKER_REQUIRED_FIELDS) | {
        "repository_head"
    }


# ---------------------------------------------------------------------------
# T10/T11. create-once writing
# ---------------------------------------------------------------------------


def test_mark_writes_exactly_once_and_refuses_overwrite(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    result = _mark(base, run_index=RUN_INDEX)

    path = Path(result["marker_path"])
    assert result["state"] == g7s.MARKER_STATE_VALID
    assert result["run_root"] == str(run_root)
    assert path == g7s.durable_quarantine_marker_path(run_root)
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == result["marker"]
    assert set(on_disk) == set(g7s.MARKER_REQUIRED_FIELDS) | {
        "repository_head"
    }
    first_bytes = path.read_bytes()

    with pytest.raises(g7s.G7SDurableQuarantineError, match="overwrite"):
        _mark(base, run_index=RUN_INDEX, reason="second attempt")
    with pytest.raises(g7s.G7SDurableQuarantineError, match="overwrite"):
        g7s.write_durable_quarantine_marker(run_root, result["marker"])

    assert path.read_bytes() == first_bytes


def test_writer_requires_an_existing_run_data_directory(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="data directory is missing"
    ):
        g7s.write_durable_quarantine_marker(
            run_root, {"marker_sha256": "0" * 64}
        )
    with pytest.raises(g7s.G7SDurableQuarantineError, match="JSON object"):
        g7s.write_durable_quarantine_marker(run_root, ["not", "an", "object"])


def test_mark_refuses_an_expected_mismatch_and_writes_nothing(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    path = g7s.durable_quarantine_marker_path(run_root)

    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="expected run identity"
    ):
        _mark(base, expected_run_identity_sha256="0" * 64)
    with pytest.raises(
        g7s.G7SDurableQuarantineError, match="expected evidence fingerprint"
    ):
        _mark(base, expected_evidence_fingerprint_sha256="0" * 64)

    assert not path.exists()

    # Matching expectations succeed and record the repository provenance.
    result = _mark(
        base,
        expected_run_identity_sha256=g7s.run_identity_sha256(run_root),
        expected_evidence_fingerprint_sha256=(
            g7s.capture_quarantine_evidence(run_root, BLOCKED_CHAPTER)[
                "evidence_fingerprint_sha256"
            ]
        ),
        repository_head=REPOSITORY_HEAD,
    )

    assert result["state"] == g7s.MARKER_STATE_VALID
    assert result["marker"]["repository_head"] == REPOSITORY_HEAD
    assert path.is_file()


# ---------------------------------------------------------------------------
# T12. absent -> valid inspection round trip
# ---------------------------------------------------------------------------


def test_inspect_absent_then_valid_round_trip(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    path = g7s.durable_quarantine_marker_path(run_root)

    absent = g7s.inspect_durable_quarantine_marker(run_root)

    assert absent["state"] == g7s.MARKER_STATE_ABSENT
    assert absent["reason"] is None
    assert absent["marker_path"] == str(path)
    assert isinstance(absent["detail"], str)
    for field in (
        "first_blocked_chapter",
        "source_outcome",
        "provider_sends_started",
        "accepted_credit_equivalent_usd",
        "run_identity_sha256",
        "marker_sha256",
        "created_utc",
        "evidence_references",
    ):
        assert absent[field] is None, field
    assert absent["evidence_drift"] == []
    assert not path.exists()

    result = _mark(base, run_index=RUN_INDEX)
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_VALID
    assert snapshot["reason"] is None
    assert snapshot["marker_path"] == str(path)
    assert snapshot["first_blocked_chapter"] == BLOCKED_CHAPTER
    assert snapshot["source_outcome"] == (
        g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE
    )
    assert snapshot["provider_sends_started"] == SENDS
    assert snapshot["accepted_credit_equivalent_usd"] == "1.5"
    assert snapshot["run_identity_sha256"] == (
        g7s.run_identity_sha256(run_root)
    )
    assert snapshot["marker_sha256"] == result["marker"]["marker_sha256"]
    assert snapshot["created_utc"] == result["marker"]["created_utc"]
    assert snapshot["evidence_references"]["target_chapter_ledger"][
        "target_chapter"
    ] == BLOCKED_CHAPTER
    assert snapshot["evidence_drift"] == []
    # A spec that matches the run's own immutable identity stays valid.
    assert g7s.inspect_durable_quarantine_marker(
        run_root, spec=SPECS[RUN_INDEX]
    )["state"] == g7s.MARKER_STATE_VALID
    # A matching expected controller digest stays valid.
    assert g7s.inspect_durable_quarantine_marker(
        run_root, expected_g7r_controller_sha256=CONTROLLER_SHA
    )["state"] == g7s.MARKER_STATE_VALID


# ---------------------------------------------------------------------------
# T13. unreadable / non-object / malformed / unsupported identity
# ---------------------------------------------------------------------------


def test_unreadable_and_non_object_markers_are_invalid(tmp_path):
    _base, run_root = _fixture_run(tmp_path)
    path = g7s.durable_quarantine_marker_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_unreadable"

    path.write_text("", encoding="utf-8")
    assert g7s.inspect_durable_quarantine_marker(run_root)["reason"] == (
        "marker_unreadable"
    )

    path.write_text("{not json", encoding="utf-8")
    assert g7s.inspect_durable_quarantine_marker(run_root)["reason"] == (
        "marker_unreadable"
    )

    path.write_text("[1, 2, 3]", encoding="utf-8")
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_not_an_object"
    assert snapshot["evidence_references"] is None

    path.write_text("\"a json string\"", encoding="utf-8")
    assert g7s.inspect_durable_quarantine_marker(run_root)["reason"] == (
        "marker_not_an_object"
    )

    # A directory at the marker path is never reported as absent.
    path.unlink()
    path.mkdir()
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_unreadable"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop("reason"), id="missing-reason"),
        pytest.param(_drop("evidence"), id="missing-evidence"),
        pytest.param(_drop("created_utc"), id="missing-created-utc"),
        pytest.param(_drop("run_identity_sha256"), id="missing-identity-sha"),
        pytest.param(
            _set(first_blocked_chapter=0), id="zero-blocked-chapter"
        ),
        pytest.param(
            _set(first_blocked_chapter="3"), id="string-blocked-chapter"
        ),
        pytest.param(_set(run_index=0), id="zero-run-index"),
        pytest.param(_set(source_outcome="uncertain"), id="untruthful-outcome"),
        pytest.param(_set(source_outcome=None), id="null-outcome"),
        pytest.param(_set(provider_sends_started=-1), id="negative-sends"),
        pytest.param(_set(provider_sends_started=True), id="bool-sends"),
        pytest.param(
            _set(accepted_credit_equivalent_usd="not-a-number"),
            id="non-numeric-cost",
        ),
        pytest.param(
            _set(accepted_credit_equivalent_usd=-1), id="negative-cost"
        ),
        pytest.param(
            _set(accepted_credit_equivalent_usd=None), id="null-cost"
        ),
        pytest.param(_set(reason="   "), id="blank-reason"),
        pytest.param(_set(reason=7), id="non-string-reason"),
        pytest.param(_set(created_utc=""), id="blank-created-utc"),
        pytest.param(_set(evidence="not-an-object"), id="non-object-evidence"),
        pytest.param(
            _set(g7r_controller_sha256=None), id="null-controller"
        ),
        pytest.param(_set(g7s_policy_sha256=None), id="null-policy"),
        pytest.param(_set(schema_version="1"), id="string-schema"),
        pytest.param(_set(revision=""), id="blank-revision"),
    ],
)
def test_malformed_marker_fields_are_invalid(tmp_path, mutate):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    mutate(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_malformed"


def test_unsupported_marker_identity_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    for field, value in (
        ("schema_version", 3),
        ("revision", "goat-g7s-stage1-durable-quarantine-v3"),
    ):
        marker = _build(base, run_root)
        marker[field] = value
        _rehash(marker)
        _write_marker(run_root, marker)

        snapshot = g7s.inspect_durable_quarantine_marker(run_root)

        assert snapshot["state"] == g7s.MARKER_STATE_INVALID
        assert snapshot["reason"] == "unsupported_marker_identity"


# ---------------------------------------------------------------------------
# T14. hash-invalid markers
# ---------------------------------------------------------------------------


def test_hash_invalid_markers_are_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    # A tampered field with a stale marker SHA.
    marker = _build(base, run_root)
    marker["reason"] = "tampered after marking"
    _write_marker(run_root, marker)
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_sha256_mismatch"
    # The tampered value is still reported for the operator.
    assert snapshot["first_blocked_chapter"] == BLOCKED_CHAPTER

    for name, mutate in (
        ("missing", _drop("marker_sha256")),
        ("zeroed", _set(marker_sha256="0" * 64)),
        ("not-hex", _set(marker_sha256="z" * 64)),
        ("short", _set(marker_sha256="a" * 63)),
        ("non-string", _set(marker_sha256=None)),
    ):
        marker = _build(base, run_root)
        mutate(marker)
        _write_marker(run_root, marker)

        snapshot = g7s.inspect_durable_quarantine_marker(run_root)

        assert snapshot["state"] == g7s.MARKER_STATE_INVALID, name
        assert snapshot["reason"] == "marker_sha256_mismatch", name


# ---------------------------------------------------------------------------
# T15. wrong run identity
# ---------------------------------------------------------------------------


def test_marker_bound_to_another_run_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    result = _mark(base, run_index=RUN_INDEX)

    other_index = RUN_INDEX + 1
    other_root = _run_root(base, other_index)
    _write_identity(other_root, other_index)
    _write_marker(other_root, result["marker"])

    snapshot = g7s.inspect_durable_quarantine_marker(other_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "wrong_run_identity"


def test_marker_is_invalid_once_the_run_identity_changes(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _mark(base, run_index=RUN_INDEX)

    _write_identity(
        run_root, RUN_INDEX, {"profile": "another-profile"}
    )

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "wrong_run_identity"


def test_marker_is_invalid_for_an_unverifiable_run_identity(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _mark(base, run_index=RUN_INDEX)

    (run_root / "g7c_run_identity.json").unlink()

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "wrong_run_identity"


def test_marker_disagreeing_with_the_selected_spec_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _mark(base, run_index=RUN_INDEX)

    snapshot = g7s.inspect_durable_quarantine_marker(
        run_root, spec=SPECS[RUN_INDEX + 1]
    )

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "wrong_run_identity"


# ---------------------------------------------------------------------------
# T16. policy drift and controller mismatch
# ---------------------------------------------------------------------------


def test_policy_drift_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    marker["g7s_policy_sha256"] = "0" * 64
    _rehash(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "g7s_policy_drift"


def test_controller_mismatch_and_malformed_controller_are_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    marker = _build(base, run_root)
    marker["g7r_controller_sha256"] = OTHER_CONTROLLER_SHA
    _rehash(marker)
    _write_marker(run_root, marker)

    # Individually well-formed: only an explicit expectation notices.
    assert g7s.inspect_durable_quarantine_marker(run_root)["state"] == (
        g7s.MARKER_STATE_VALID
    )
    snapshot = g7s.inspect_durable_quarantine_marker(
        run_root, expected_g7r_controller_sha256=CONTROLLER_SHA
    )
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "g7r_controller_drift"

    marker = _build(base, run_root)
    marker["g7r_controller_sha256"] = "not-a-sha"
    _rehash(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(
        run_root, expected_g7r_controller_sha256=CONTROLLER_SHA
    )
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_malformed"


# ---------------------------------------------------------------------------
# T17. conflicting evidence reference and source-bundle drift
# ---------------------------------------------------------------------------


def test_conflicting_evidence_reference_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    marker = _build(base, run_root)
    marker["evidence"]["references"]["target_chapter_ledger"][
        "target_chapter"
    ] = BLOCKED_CHAPTER + 1
    _rehash_evidence(marker)
    _rehash(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "conflicting_evidence_reference"

    # A null ledger reference is not a conflict: targets below the blocked
    # chapter carry no target ledger at all.
    marker = _build(base, run_root)
    marker["evidence"]["references"]["target_chapter_ledger"] = None
    _rehash_evidence(marker)
    _rehash(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_VALID
    assert snapshot["evidence_references"]["target_chapter_ledger"] is None


def test_run_source_identity_drift_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)

    marker = _build(base, run_root)
    marker["run_source_identity_semantic_sha256"] = "0" * 64
    _rehash(marker)
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "run_source_identity_drift"


# ---------------------------------------------------------------------------
# T18. inspection is total and never writes
# ---------------------------------------------------------------------------


def test_inspect_never_raises_and_never_writes(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _mark(base, run_index=RUN_INDEX)
    path = g7s.durable_quarantine_marker_path(run_root)

    hostile = [
        {},
        {"marker_sha256": None},
        {"marker_sha256": "0" * 64, "schema_version": 1},
        {
            "evidence": {"references": {"target_chapter_ledger": "x"}},
            "first_blocked_chapter": BLOCKED_CHAPTER,
        },
        {"evidence": {"references": [1, 2, 3]}},
        {"evidence": {"references": None}, "first_blocked_chapter": 3},
        {"first_blocked_chapter": 3, "source_outcome": ["x"]},
        {"created_utc": 5},
        {"run_index": None, "relative_root": None},
        {"schema_version": 1, "revision": ["x"], "run_index": 3},
        {"first_blocked_chapter": 3, "provider_sends_started": "many"},
        {"accepted_credit_equivalent_usd": {"usd": 1}},
    ]

    for payload in hostile:
        path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        before = _tree_hash(run_root)

        snapshot = g7s.inspect_durable_quarantine_marker(run_root)

        assert isinstance(snapshot, dict)
        assert snapshot["state"] in {
            g7s.MARKER_STATE_ABSENT,
            g7s.MARKER_STATE_VALID,
            g7s.MARKER_STATE_INVALID,
        }
        assert snapshot["state"] != g7s.MARKER_STATE_VALID
        assert isinstance(snapshot["reason"], str)
        assert isinstance(snapshot["detail"], str)
        assert snapshot["marker_path"] == str(path)
        assert _tree_hash(run_root) == before

    # Unparseable and oversized payloads are equally safe.
    path.write_bytes(b"\x00" * 200000)
    before = _tree_hash(run_root)
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "marker_unreadable"
    assert _tree_hash(run_root) == before

    # A genuinely valid marker is still inspected read-only, and a mismatched
    # expectation is a clean invalid rather than a crash.
    marker = _build(base, run_root)
    _write_marker(run_root, marker)
    before = _tree_hash(run_root)
    assert g7s.inspect_durable_quarantine_marker(
        run_root, spec=SPECS[RUN_INDEX]
    )["state"] == g7s.MARKER_STATE_VALID
    assert g7s.inspect_durable_quarantine_marker(run_root)["reason"] is None
    mismatched = g7s.inspect_durable_quarantine_marker(
        run_root, spec=SPECS[RUN_INDEX + 1]
    )
    assert mismatched["state"] == g7s.MARKER_STATE_INVALID
    assert mismatched["reason"] == "wrong_run_identity"
    assert _tree_hash(run_root) == before


def test_evidence_drift_is_reported_without_corrupting_the_marker(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    progress_path = run_root / "data" / "progress.json"
    progress_path.write_text('{"last_completed": 2}', encoding="utf-8")
    _mark(base, run_index=RUN_INDEX)

    journal_dir = run_root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "0003.g7c_translation.json").write_text(
        "{}", encoding="utf-8"
    )
    progress_path.write_text('{"last_completed": 3}', encoding="utf-8")

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    # Drift is purely informational: the historical marker stays valid.
    assert snapshot["state"] == g7s.MARKER_STATE_VALID
    assert snapshot["reason"] is None
    assert any(
        "new journal added" in item for item in snapshot["evidence_drift"]
    )
    assert any(
        "progress.json" in item for item in snapshot["evidence_drift"]
    )


# ---------------------------------------------------------------------------
# T19/T20. CLI
# ---------------------------------------------------------------------------


def test_cli_inspect_emits_one_json_document_with_zero_providers(
    tmp_path, capsys
):
    base, run_root = _fixture_run(tmp_path)
    path = g7s.durable_quarantine_marker_path(run_root)
    args = ["inspect", "--stage1-root", str(base), "--run-index", str(RUN_INDEX)]

    exit_code = g7s.main(args)
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert document["status"] == "OK"
    assert document["state"] == g7s.MARKER_STATE_ABSENT
    assert document["reason"] is None
    assert document["revision"] == g7s.G7S_REVISION
    assert document["policy_sha256"] == g7s.G7S_POLICY_SHA256
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False
    assert document["marker_path"] == str(path)
    assert document["run_root"] == str(run_root)
    assert document["first_blocked_chapter"] is None
    assert document["source_outcome"] is None
    assert isinstance(document["detail"], str)
    assert not path.exists()

    _mark(base, run_index=RUN_INDEX)
    exit_code = g7s.main(args)
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert document["status"] == "OK"
    assert document["state"] == g7s.MARKER_STATE_VALID
    assert document["reason"] is None
    assert document["first_blocked_chapter"] == BLOCKED_CHAPTER
    assert document["source_outcome"] == (
        g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE
    )
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False

    path.write_text("{not json", encoding="utf-8")
    exit_code = g7s.main(args)
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert document["status"] == "BLOCKED"
    assert document["state"] == g7s.MARKER_STATE_INVALID
    assert document["reason"] == "marker_unreadable"
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False

    # A usage error is exit 1 and never claims anything about a paid boundary.
    assert g7s.main(
        ["inspect", "--stage1-root", str(base), "--run-index", "99"]
    ) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "ERROR"
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False


def test_cli_mark_requires_the_explicit_authorization_flag(tmp_path, capsys):
    base, run_root = _fixture_run(tmp_path)
    path = g7s.durable_quarantine_marker_path(run_root)
    args = [
        "mark",
        "--stage1-root",
        str(base),
        "--run-index",
        str(RUN_INDEX),
        "--first-blocked-chapter",
        str(BLOCKED_CHAPTER),
        "--provider-sends-started",
        str(SENDS),
        "--accepted-credit-equivalent-usd",
        ACCEPTED,
        "--reason",
        "paid boundary corrupt",
        "--g7r-controller-sha256",
        CONTROLLER_SHA,
        "--source-wave-bundle-sha256",
        WAVE_BUNDLE_SHA,
    ]

    exit_code = g7s.main(args)
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert document["status"] == "ERROR"
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False
    assert not path.exists()

    exit_code = g7s.main(list(args) + ["--authorize-durable-quarantine"])
    document = json.loads(capsys.readouterr().out)

    # The historical controller provenance is exactly what the operator passed;
    # it is never re-derived from the current checkout.
    assert exit_code == 0
    assert document["status"] == "OK"
    assert document["state"] == g7s.MARKER_STATE_VALID
    assert document["marker_path"] == str(path)
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False
    assert document["g7r_controller_sha256"] == CONTROLLER_SHA
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["g7r_controller_sha256"] == CONTROLLER_SHA
    assert on_disk["source_wave_bundle_sha256"] == WAVE_BUNDLE_SHA
    assert on_disk["marker_sha256"] == g7s.compute_marker_sha256(on_disk)
    assert (
        g7s.inspect_durable_quarantine_marker(run_root)["state"]
        == g7s.MARKER_STATE_VALID
    )
    first_bytes = path.read_bytes()

    # A second authorized mark is refused create-once and rewrites nothing.
    exit_code = g7s.main(
        list(args)
        + ["--authorize-durable-quarantine", "--reason", "second attempt"]
    )
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert document["status"] == "ERROR"
    assert document["provider_calls"] == 0
    assert path.read_bytes() == first_bytes


# ---------------------------------------------------------------------------
# T21. no module-level G7R import (no cycle)
# ---------------------------------------------------------------------------


def test_g7s_never_imports_g7r_at_module_level():
    # ``utf-8-sig`` tolerates the module's preserved UTF-8 BOM.
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8-sig"))
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_level.add(node.module or "")

    assert not any(
        name.startswith("benchmark_longitudinal_g7r") for name in module_level
    )
    assert {
        name
        for name in module_level
        if name.startswith("benchmark_longitudinal_")
    } == {"benchmark_longitudinal_g7c_stage1_integration"}
    allowed = {
        "__future__",
        "argparse",
        "hashlib",
        "json",
        "os",
        "sys",
        "uuid",
        "datetime",
        "decimal",
        "pathlib",
        "typing",
    }
    assert module_level - allowed == {
        "benchmark_longitudinal_g7c_stage1_integration"
    }

    # No G7R import exists at all: the module never depends on the controller.
    g7r_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("benchmark_longitudinal_g7r")
    ]
    assert g7r_imports == []

    # The frozen G7F ledger validator is imported lazily, inside the function
    # that validates the blocked chapter's canonical ledger.
    assert not any("g7f" in name for name in module_level)
    g7f_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("benchmark_longitudinal_g7f")
    ]
    assert g7f_imports


def test_public_surface_is_declared_and_importable():
    expected = {
        "ALLOWED_SOURCE_OUTCOMES",
        "DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE",
        "G7SDurableQuarantineError",
        "G7S_POLICY_SHA256",
        "G7S_REPOSITORY_ANCHOR",
        "G7S_REVISION",
        "G7S_SCHEMA_VERSION",
        "MARKER_FILENAME",
        "MARKER_RELATIVE_PATH",
        "MARKER_REQUIRED_FIELDS",
        "MARKER_STATE_ABSENT",
        "MARKER_STATE_INVALID",
        "MARKER_STATE_VALID",
        "SOURCE_BUNDLE_SEMANTIC_FIELDS",
        "build_durable_quarantine_marker",
        "build_parser",
        "capture_quarantine_evidence",
        "compute_marker_sha256",
        "durable_quarantine_marker_path",
        "g7s_policy_identity",
        "inspect_durable_quarantine_marker",
        "main",
        "mark_durable_quarantine",
        "marker_semantic_payload",
        "run_identity_sha256",
        "run_source_identity_semantic_sha256",
        "write_durable_quarantine_marker",
    }

    assert set(g7s.__all__) == expected
    for name in g7s.__all__:
        assert hasattr(g7s, name), name

    marker = g7s.marker_semantic_payload(
        {
            "schema_version": 1,
            "marker_sha256": "0" * 64,
            "accepted_credit_equivalent_usd": "1.5",
        }
    )
    assert marker == {
        "schema_version": 1,
        "accepted_credit_equivalent_usd": "1.5",
    }


# ---------------------------------------------------------------------------
# T22. atomic create-once publication
# ---------------------------------------------------------------------------


def test_partial_write_never_publishes_a_final_marker(tmp_path, monkeypatch):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    path = g7s.durable_quarantine_marker_path(run_root)

    def boom(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(g7s, "_fsync", boom)
    with pytest.raises(g7s.G7SDurableQuarantineError):
        g7s.write_durable_quarantine_marker(run_root, marker)

    assert not path.exists()
    leftovers = [
        item.name for item in path.parent.iterdir() if ".tmp." in item.name
    ]
    assert leftovers == []


def test_publication_writes_the_complete_payload(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)

    path = g7s.write_durable_quarantine_marker(run_root, marker)

    expected = json.dumps(marker, indent=2, sort_keys=True, ensure_ascii=False)
    assert path.read_text(encoding="utf-8") == expected
    assert json.loads(path.read_text(encoding="utf-8")) == marker
    assert [
        item.name for item in path.parent.iterdir() if ".tmp." in item.name
    ] == []


def test_publication_link_refuses_an_existing_marker(tmp_path, monkeypatch):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    path = g7s.durable_quarantine_marker_path(run_root)

    def refuse(_src, _dst):
        raise FileExistsError("destination exists")

    monkeypatch.setattr(g7s.os, "link", refuse)
    with pytest.raises(g7s.G7SDurableQuarantineError, match="overwrite"):
        g7s.write_durable_quarantine_marker(run_root, marker)
    assert not path.exists()
    assert [
        item.name for item in path.parent.iterdir() if ".tmp." in item.name
    ] == []


# ---------------------------------------------------------------------------
# T23. explicit wave-bundle provenance and the canonical blocked-chapter ledger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wave",
    [None, "", "zz", "f" * 63, "F" * 64, 5],
)
def test_source_wave_bundle_sha256_must_be_explicit_sha256(tmp_path, wave):
    base, run_root = _fixture_run(tmp_path)
    with pytest.raises(g7s.G7SDurableQuarantineError, match="source_wave_bundle"):
        _build(base, run_root, source_wave_bundle_sha256=wave)
    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_run_identity_and_wave_bundle_hashes_are_distinct(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    assert marker["run_source_identity_semantic_sha256"] != (
        marker["source_wave_bundle_sha256"]
    )
    changed = dict(marker)
    changed["source_wave_bundle_sha256"] = "0" * 64
    assert g7s.compute_marker_sha256(changed) != marker["marker_sha256"]
    changed = dict(marker)
    changed["run_source_identity_semantic_sha256"] = "0" * 64
    assert g7s.compute_marker_sha256(changed) != marker["marker_sha256"]


def test_zero_sends_is_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    with pytest.raises(g7s.G7SDurableQuarantineError, match="provider_sends_started"):
        _build(base, run_root, provider_sends_started=0)
    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_missing_ledger_is_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _ledger_path_for(run_root).unlink()
    with pytest.raises(g7s.G7SDurableQuarantineError, match="ledger is missing"):
        _build(base, run_root)
    with pytest.raises(g7s.G7SDurableQuarantineError, match="ledger is missing"):
        _mark(base)
    assert not g7s.durable_quarantine_marker_path(run_root).exists()


def test_corrupt_canonical_ledger_is_rejected(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    _ledger_path_for(run_root).write_text("{not json", encoding="utf-8")
    with pytest.raises(g7s.G7SDurableQuarantineError, match="cannot read"):
        _build(base, run_root)

    base, run_root = _fixture_run(tmp_path / "b")
    document = _ledger_document(RUN_INDEX)
    document["revision"] = "g7f-persisted-invocation-budget-v999"
    _ledger_path_for(run_root).write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(g7s.G7SDurableQuarantineError, match="not canonical"):
        _build(base, run_root)
    assert not g7s.durable_quarantine_marker_path(run_root).exists()


# ---------------------------------------------------------------------------
# T24. stored evidence must stay internally consistent with the marker
# ---------------------------------------------------------------------------


def test_stale_evidence_fingerprint_is_invalid_after_marker_rehash(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    marker["evidence"]["references"]["target_chapter_ledger"][
        "new_provider_sends_started"
    ] = SENDS + 5
    _rehash(marker)  # marker_sha256 now recomputes correctly
    _write_marker(run_root, marker)

    snapshot = g7s.inspect_durable_quarantine_marker(run_root)

    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "evidence_fingerprint_mismatch"


def test_unsupported_evidence_revision_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    marker["evidence"]["revision"] = "g7s-marker-evidence-v999"
    _rehash(marker)
    _write_marker(run_root, marker)
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "unsupported_evidence_revision"


def test_non_object_evidence_references_is_invalid(tmp_path):
    base, run_root = _fixture_run(tmp_path)
    marker = _build(base, run_root)
    marker["evidence"]["references"] = "not-an-object"
    _rehash(marker)
    _write_marker(run_root, marker)
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_INVALID
    assert snapshot["reason"] == "evidence_references_invalid"


# ---------------------------------------------------------------------------
# T25. the CLI requires explicit historical controller and wave-bundle digests
# ---------------------------------------------------------------------------


def test_cli_mark_requires_explicit_controller_and_wave_bundle(tmp_path):
    base, _run_root = _fixture_run(tmp_path)
    base_args = [
        "mark",
        "--stage1-root", str(base),
        "--run-index", str(RUN_INDEX),
        "--first-blocked-chapter", str(BLOCKED_CHAPTER),
        "--provider-sends-started", str(SENDS),
        "--accepted-credit-equivalent-usd", ACCEPTED,
        "--reason", "paid boundary corrupt",
        "--authorize-durable-quarantine",
    ]
    with pytest.raises(SystemExit):
        g7s.main(base_args)
    with pytest.raises(SystemExit):
        g7s.main(base_args + ["--g7r-controller-sha256", CONTROLLER_SHA])
    # With both explicit values the historical provenance is recorded.
    code = g7s.main(
        base_args
        + ["--g7r-controller-sha256", CONTROLLER_SHA,
           "--source-wave-bundle-sha256", WAVE_BUNDLE_SHA]
    )
    assert code == 0
