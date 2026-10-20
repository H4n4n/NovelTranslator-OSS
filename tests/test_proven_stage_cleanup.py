from pathlib import Path

import pytest

import proven_stage_cleanup as cleanup


class CleanupError(ValueError):
    pass


def _stage(parent: Path, name: str = ".fixture.staging-one") -> Path:
    stage = parent / name
    stage.mkdir()
    (stage / "a.txt").write_text("A", encoding="utf-8")
    (stage / "b.txt").write_text("B", encoding="utf-8")
    return stage


def _proof(value: str = "one") -> str:
    return cleanup.cleanup_proof_id("fixture", {"expected": value})


def _inspect(tmp_path: Path, proof_id: str, current_stage: Path | None = None):
    return cleanup.inspect_stage_cleanup(
        tmp_path,
        namespace="fixture",
        stage_prefix=".fixture.staging-",
        proof_id=proof_id,
        current_stage=current_stage,
        error_cls=CleanupError,
    )


def test_cleanup_proof_id_is_deterministic_and_identity_bound():
    assert _proof("one") == _proof("one")
    assert _proof("one") != _proof("two")


def test_new_proven_stage_cleanup_completes_without_residue(tmp_path):
    stage = _stage(tmp_path)
    inspection = _inspect(tmp_path, _proof())

    assert inspection.stage_candidates == (stage,)

    cleanup.cleanup_proven_stages(
        inspection,
        [stage],
        error_cls=CleanupError,
    )

    assert not stage.exists()
    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))


def test_partial_recursive_delete_resumes_from_proof_bound_journal(
    tmp_path, monkeypatch
):
    stage = _stage(tmp_path)
    proof_id = _proof()
    inspection = _inspect(tmp_path, proof_id)
    real_rmtree = cleanup.shutil.rmtree
    failed = {"done": False}

    def partial_then_fail(path):
        path = Path(path)
        if not failed["done"]:
            failed["done"] = True
            (path / "a.txt").unlink()
            raise OSError("injected partial recursive cleanup")
        return real_rmtree(path)

    monkeypatch.setattr(cleanup.shutil, "rmtree", partial_then_fail)

    with pytest.raises(OSError, match="partial recursive cleanup"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    assert not stage.exists()

    journals = list(tmp_path.glob(".fixture.cleanup-journal-*.json"))
    tombstones = list(tmp_path.glob(".fixture.cleanup-stage-*"))

    assert len(journals) == 1
    assert len(tombstones) == 1
    assert not (tombstones[0] / "a.txt").exists()
    assert (tombstones[0] / "b.txt").is_file()

    monkeypatch.setattr(cleanup.shutil, "rmtree", real_rmtree)

    retry = _inspect(tmp_path, proof_id)
    assert retry.stage_candidates == ()
    assert len(retry.records) == 1
    assert retry.records[0].state == "tombstone"

    cleanup.cleanup_proven_stages(
        retry,
        [],
        error_cls=CleanupError,
    )

    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))


def test_rename_failure_leaves_original_stage_reprovable(tmp_path, monkeypatch):
    stage = _stage(tmp_path)
    proof_id = _proof()
    inspection = _inspect(tmp_path, proof_id)
    real_rename = cleanup.os.rename

    def fail_rename(source, destination):
        raise OSError("injected cleanup rename failure")

    monkeypatch.setattr(cleanup.os, "rename", fail_rename)

    with pytest.raises(OSError, match="cleanup rename"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    assert stage.is_dir()
    assert len(list(tmp_path.glob(".fixture.cleanup-journal-*.json"))) == 1
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))

    monkeypatch.setattr(cleanup.os, "rename", real_rename)

    retry = _inspect(tmp_path, proof_id)
    assert retry.stage_candidates == (stage,)
    assert retry.records[0].state == "original"

    cleanup.cleanup_proven_stages(
        retry,
        [stage],
        error_cls=CleanupError,
    )

    assert not stage.exists()


def test_journal_only_state_finishes_after_tombstone_removal(
    tmp_path, monkeypatch
):
    stage = _stage(tmp_path)
    proof_id = _proof()
    inspection = _inspect(tmp_path, proof_id)
    real_unlink = Path.unlink

    def fail_journal_unlink(path, *args, **kwargs):
        path = Path(path)
        if path.name.startswith(".fixture.cleanup-journal-"):
            raise OSError("injected journal cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)

    with pytest.raises(OSError, match="journal cleanup"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    assert not stage.exists()
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))
    assert len(list(tmp_path.glob(".fixture.cleanup-journal-*.json"))) == 1

    monkeypatch.setattr(Path, "unlink", real_unlink)

    retry = _inspect(tmp_path, proof_id)
    assert retry.stage_candidates == ()
    assert retry.records[0].state == "journal_only"

    cleanup.cleanup_proven_stages(
        retry,
        [],
        error_cls=CleanupError,
    )

    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))


def test_proof_drift_blocks_authorized_partial_tombstone(tmp_path, monkeypatch):
    stage = _stage(tmp_path)
    inspection = _inspect(tmp_path, _proof("old"))

    def fail_delete(path):
        raise OSError("injected stop")

    monkeypatch.setattr(cleanup.shutil, "rmtree", fail_delete)

    with pytest.raises(OSError, match="injected stop"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    with pytest.raises(CleanupError, match="proof identity mismatch"):
        _inspect(tmp_path, _proof("new"))

    assert len(list(tmp_path.glob(".fixture.cleanup-journal-*.json"))) == 1
    assert len(list(tmp_path.glob(".fixture.cleanup-stage-*"))) == 1


def test_unknown_tombstone_fails_closed(tmp_path):
    tombstone = tmp_path / (".fixture.cleanup-stage-" + ("a" * 32))
    tombstone.mkdir()

    with pytest.raises(CleanupError, match="unproven stage-cleanup tombstone"):
        _inspect(tmp_path, _proof())

    assert tombstone.is_dir()


def test_malformed_journal_fails_closed(tmp_path):
    journal = tmp_path / (".fixture.cleanup-journal-" + ("a" * 32) + ".json")
    journal.write_text("{broken", encoding="utf-8")

    with pytest.raises(CleanupError, match="malformed stage-cleanup journal"):
        _inspect(tmp_path, _proof())

    assert journal.is_file()


def test_original_and_tombstone_coexistence_blocks(tmp_path, monkeypatch):
    stage = _stage(tmp_path)
    proof_id = _proof()
    inspection = _inspect(tmp_path, proof_id)
    real_rename = cleanup.os.rename

    def stop_before_rename(source, destination):
        raise OSError("stop before rename")

    monkeypatch.setattr(cleanup.os, "rename", stop_before_rename)

    with pytest.raises(OSError, match="stop before rename"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    monkeypatch.setattr(cleanup.os, "rename", real_rename)

    journal = list(tmp_path.glob(".fixture.cleanup-journal-*.json"))[0]
    payload = cleanup.json.loads(journal.read_text(encoding="utf-8"))
    tombstone = tmp_path / payload["tombstone_name"]
    tombstone.mkdir()

    with pytest.raises(CleanupError, match="original stage and tombstone coexist"):
        _inspect(tmp_path, proof_id)

    assert stage.is_dir()
    assert tombstone.is_dir()


def test_cleanup_requires_exact_proven_candidate_inventory(tmp_path):
    stage = _stage(tmp_path)
    inspection = _inspect(tmp_path, _proof())

    with pytest.raises(CleanupError, match="proof inventory mismatch"):
        cleanup.cleanup_proven_stages(
            inspection,
            [],
            error_cls=CleanupError,
        )

    assert stage.is_dir()
    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))


def test_current_stage_is_excluded_from_stale_candidate_inventory(tmp_path):
    stale = _stage(tmp_path, ".fixture.staging-stale")
    current = _stage(tmp_path, ".fixture.staging-current")

    inspection = _inspect(
        tmp_path,
        _proof(),
        current_stage=current,
    )

    assert inspection.stage_candidates == (stale,)


def test_partial_prejournal_temp_is_recovered_after_exact_stage_proof(tmp_path):
    stage = _stage(tmp_path)
    prep = tmp_path / (
        ".fixture.cleanup-journal-prep-" + ("a" * 32) + ".tmp"
    )
    prep.write_bytes(b"{partial")

    inspection = _inspect(tmp_path, _proof())

    assert inspection.residue_present is True
    assert inspection.prejournal_temps == (prep,)
    assert inspection.stage_candidates == (stage,)

    cleanup.cleanup_proven_stages(
        inspection,
        [stage],
        error_cls=CleanupError,
    )

    assert not prep.exists()
    assert not stage.exists()
    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))


def test_partial_prejournal_temp_is_preserved_until_stage_is_proven(tmp_path):
    stage = _stage(tmp_path)
    prep = tmp_path / (
        ".fixture.cleanup-journal-prep-" + ("b" * 32) + ".tmp"
    )
    prep.write_bytes(b"{partial")

    inspection = _inspect(tmp_path, _proof())

    with pytest.raises(CleanupError, match="proof inventory mismatch"):
        cleanup.cleanup_proven_stages(
            inspection,
            [],
            error_cls=CleanupError,
        )

    assert prep.is_file()
    assert stage.is_dir()


def test_invalid_prejournal_token_fails_closed(tmp_path):
    stage = _stage(tmp_path)
    prep = tmp_path / ".fixture.cleanup-journal-prep-not-a-token.tmp"
    prep.write_bytes(b"scratch")

    with pytest.raises(
        CleanupError,
        match="invalid stage-cleanup pre-journal token",
    ):
        _inspect(tmp_path, _proof())

    assert prep.is_file()
    assert stage.is_dir()


def test_directory_prejournal_artifact_fails_closed(tmp_path):
    stage = _stage(tmp_path)
    prep = tmp_path / (
        ".fixture.cleanup-journal-prep-" + ("c" * 32) + ".tmp"
    )
    prep.mkdir()

    with pytest.raises(
        CleanupError,
        match="ambiguous stage-cleanup pre-journal artifact",
    ):
        _inspect(tmp_path, _proof())

    assert prep.is_dir()
    assert stage.is_dir()


def test_journal_publication_precedes_stage_rename(tmp_path, monkeypatch):
    stage = _stage(tmp_path)
    inspection = _inspect(tmp_path, _proof())
    events = []
    real_replace = cleanup.os.replace
    real_rename = cleanup.os.rename

    def record_replace(source, destination):
        events.append(
            (
                "replace",
                Path(source).name,
                Path(destination).name,
            )
        )
        return real_replace(source, destination)

    def record_rename(source, destination):
        events.append(
            (
                "rename",
                Path(source).name,
                Path(destination).name,
            )
        )
        return real_rename(source, destination)

    monkeypatch.setattr(cleanup.os, "replace", record_replace)
    monkeypatch.setattr(cleanup.os, "rename", record_rename)

    cleanup.cleanup_proven_stages(
        inspection,
        [stage],
        error_cls=CleanupError,
    )

    assert [event[0] for event in events[:2]] == [
        "replace",
        "rename",
    ]
    assert ".cleanup-journal-prep-" in events[0][1]
    assert ".cleanup-journal-" in events[0][2]
    assert events[1][1] == stage.name


def test_journal_replace_failure_cleans_owned_prep_and_preserves_stage(
    tmp_path,
    monkeypatch,
):
    stage = _stage(tmp_path)
    inspection = _inspect(tmp_path, _proof())

    def fail_replace(source, destination):
        raise OSError("injected journal publication failure")

    monkeypatch.setattr(cleanup.os, "replace", fail_replace)

    with pytest.raises(OSError, match="journal publication failure"):
        cleanup.cleanup_proven_stages(
            inspection,
            [stage],
            error_cls=CleanupError,
        )

    assert stage.is_dir()
    assert not list(
        tmp_path.glob(".fixture.cleanup-journal-prep-*.tmp")
    )
    assert not list(tmp_path.glob(".fixture.cleanup-journal-*.json"))
    assert not list(tmp_path.glob(".fixture.cleanup-stage-*"))


def test_orphan_prejournal_temp_without_stage_fails_closed(tmp_path):
    prep = tmp_path / (
        ".fixture.cleanup-journal-prep-" + ("d" * 32) + ".tmp"
    )
    prep.write_bytes(b"{partial")

    inspection = _inspect(tmp_path, _proof())

    assert inspection.prejournal_temps == (prep,)
    assert inspection.stage_candidates == ()

    with pytest.raises(
        CleanupError,
        match="pre-journal residue has no stale stage candidate",
    ):
        cleanup.cleanup_proven_stages(
            inspection,
            [],
            error_cls=CleanupError,
        )

    assert prep.is_file()


def test_symlink_prejournal_artifact_fails_closed_without_unlink(
    tmp_path,
    monkeypatch,
):
    stage = _stage(tmp_path)
    prep = tmp_path / (
        ".fixture.cleanup-journal-prep-" + ("f" * 32) + ".tmp"
    )
    prep.write_bytes(b"scratch")
    real_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: (
            True if Path(path) == prep else real_is_symlink(path)
        ),
    )

    with pytest.raises(
        CleanupError,
        match="ambiguous stage-cleanup pre-journal artifact",
    ):
        _inspect(tmp_path, _proof())

    assert prep.is_file()
    assert stage.is_dir()
