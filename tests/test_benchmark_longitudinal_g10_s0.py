import copy
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g10_s0 as s0
from benchmark_longitudinal_g10_chronological import TRACK_SPECS
from benchmark_longitudinal_g10_preregistration import NOVELS
from create_novel import PROJECT_DIRECTORIES, initial_project_state


def _canonical_sha(value):
    return s0._sha256_json(value)


def _chapter(production_index, local_number, volume_index, text):
    return {
        "chapter": production_index,
        "production_index": production_index,
        "local_chapter_number": local_number,
        "local_chapter_start": local_number,
        "local_chapter_end": local_number,
        "volume_index": volume_index,
        "heading": f"Chapter {local_number}",
        "title": f"Title {local_number}",
        "text": text,
    }


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _config(novel_id):
    if novel_id == "ace_evolution":
        return {
            "id": "ace_evolution",
            "title": "Ace Evolution",
            "source_language": "zh",
            "target_language": "en",
            "profile": "infinite_flow",
            "final_chapter": 1214,
            "audit_interval": 25,
            "chapter_numbering": "spine_sequential",
        }
    return {
        "id": "rogue_first_prince",
        "title": "I Became the Rogue First Prince",
        "source_language": "ko",
        "target_language": "en",
        "profile": "korean_fantasy",
        "final_chapter": 350,
        "audit_interval": 25,
        "chapter_numbering": "spine_sequential",
        "source_type": "txt",
        "residual_cjk_micro_repair": {
            "enabled": True,
            "max_total_cjk_chars": 12,
            "max_fragments": 6,
            "max_affected_lines": 3,
            "max_attempts_per_pending_version": 1,
        },
    }


def _make_project(repo, novel_id):
    root = repo / "novels" / novel_id
    for relative in PROJECT_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    config = _config(novel_id)
    assert _canonical_sha(config) == NOVELS[novel_id]["config_canonical_sha256"]
    _write_json(root / "novel.json", config)
    (root / "translation_addendum.txt").write_text(
        f"{novel_id} addendum\n",
        encoding="utf-8",
        newline="\n",
    )

    final_chapter = config["final_chapter"]
    chapters = [
        _chapter(
            number,
            number,
            1,
            f"source text {novel_id} {number}",
        )
        for number in range(1, final_chapter + 1)
    ]
    _write_json(root / "data" / "chapters.json", chapters)

    # Production may contain mature mutable state; S0 creation must not copy it.
    mature = initial_project_state()
    mature["progress.json"] = copy.deepcopy(mature["progress.json"])
    mature["progress.json"]["completed"] = [1]
    mature["progress.json"]["last_completed"] = 1
    mature["progress.json"]["next_chapter"] = 2
    mature["glossary.json"] = {
        "entries": {"測試": {"english": "Test"}}
    }
    for name, value in mature.items():
        if name == "chapters.json":
            continue
        _write_json(root / "data" / name, value)

    suffix = "txt" if config.get("source_type") == "txt" else "epub"
    (root / "source" / f"novel.{suffix}").write_bytes(
        f"raw-source::{novel_id}".encode("utf-8")
    )
    return root


def _make_production(tmp_path):
    repo = tmp_path / "production"
    repo.mkdir()
    for novel_id in NOVELS:
        _make_project(repo, novel_id)
    return repo


def test_contract_is_zero_provider_and_paid_execution_blocked():
    s0.assert_paid_execution_blocked()
    identity = s0.g10s0_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["run_count"] == 4
    assert identity["chapters_per_track"] == 50
    assert identity["creation"]["production_guard_before_and_after"] is True
    assert identity["creation"]["source_file_copied_and_byte_verified"] is True


def test_prepare_creates_four_independent_pristine_native_roots(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    result = s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )

    assert result["run_count"] == 4
    assert result["production_guard_verified"] is True
    assert result["sources_verified"] is True
    assert result["all_roots_pristine_s0"] is True
    assert result["provider_calls"] == 0
    assert result["paid_execution_authorized"] is False

    expected = initial_project_state()
    expected["progress.json"] = copy.deepcopy(expected["progress.json"])
    expected["progress.json"]["pricing_version"] = s0.G10S0_PRICING_VERSION

    for track in TRACK_SPECS:
        root = output / "runs" / track.run_id
        identity = json.loads(
            (root / s0.G10S0_RUN_IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["run_id"] == track.run_id
        assert identity["model_id"] == track.model_id
        assert identity["novel_id"] == track.novel_id
        assert identity["state"] == "s0_unexecuted"

        for name, value in expected.items():
            if name == "chapters.json":
                continue
            actual = json.loads(
                (root / "data" / name).read_text(encoding="utf-8")
            )
            assert actual == value

        assert not any((root / "output" / "chapters").iterdir())
        assert not any((root / "output" / "audits").iterdir())
        assert not any((root / "output" / "retroactive_fixes").iterdir())
        assert not any((root / "data" / "provider_request_journal").iterdir())

    # Two model roots for the same novel begin byte-identical in source/state,
    # but carry different immutable run identities.
    qwen_ace = output / "runs" / TRACK_SPECS[0].run_id
    glm_ace = output / "runs" / TRACK_SPECS[1].run_id
    assert (
        (qwen_ace / "data" / "chapters.json").read_bytes()
        == (glm_ace / "data" / "chapters.json").read_bytes()
    )
    assert (
        (qwen_ace / "data" / "glossary.json").read_bytes()
        == (glm_ace / "data" / "glossary.json").read_bytes()
    )
    assert (
        (qwen_ace / s0.G10S0_RUN_IDENTITY_FILENAME).read_bytes()
        != (glm_ace / s0.G10S0_RUN_IDENTITY_FILENAME).read_bytes()
    )


def test_creation_never_copies_mature_production_state(tmp_path):
    production = _make_production(tmp_path)
    production_glossary = json.loads(
        (
            production
            / "novels"
            / "ace_evolution"
            / "data"
            / "glossary.json"
        ).read_text(encoding="utf-8")
    )
    assert production_glossary["entries"]

    output = tmp_path / "G10_S0"
    s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )
    for track in TRACK_SPECS:
        root = output / "runs" / track.run_id
        glossary = json.loads(
            (root / "data" / "glossary.json").read_text(encoding="utf-8")
        )
        progress = json.loads(
            (root / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert glossary == {"entries": {}}
        assert progress["last_completed"] == 0
        assert progress["next_chapter"] == 1
        assert progress["last_audited_chapter"] == 0


def test_create_once_refuses_existing_output(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    output.mkdir()
    with pytest.raises(s0.G10S0Error, match="already exists"):
        s0.prepare_s0_roots(
            production_root=production,
            output_root=output,
        )


def test_output_root_must_be_external_to_production(tmp_path):
    production = _make_production(tmp_path)
    with pytest.raises(Exception, match="overlaps|external"):
        s0.prepare_s0_roots(
            production_root=production,
            output_root=production / "G10",
        )


def test_verify_detects_mutated_run_state(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )
    first = output / "runs" / TRACK_SPECS[0].run_id
    progress_path = first / "data" / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["next_chapter"] = 2
    _write_json(progress_path, progress)

    with pytest.raises(s0.G10S0Error, match="not pristine S0"):
        s0.verify_s0_roots(
            production_root=production,
            output_root=output,
        )


def test_verify_detects_authoritative_production_change_after_binding(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )
    glossary_path = (
        production
        / "novels"
        / "ace_evolution"
        / "data"
        / "glossary.json"
    )
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["entries"]["新"] = {"english": "New"}
    _write_json(glossary_path, glossary)

    with pytest.raises(s0.G10S0Error, match="production projects changed"):
        s0.verify_s0_roots(
            production_root=production,
            output_root=output,
        )


def test_verify_detects_source_file_change_not_covered_by_mutable_guard(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )
    source = production / "novels" / "ace_evolution" / "source" / "novel.epub"
    source.write_bytes(b"changed-source")

    with pytest.raises(s0.G10S0Error, match="authoritative source changed"):
        s0.verify_s0_roots(
            production_root=production,
            output_root=output,
        )


def test_manifest_and_run_identity_tampering_fail_closed(tmp_path):
    production = _make_production(tmp_path)
    output = tmp_path / "G10_S0"
    s0.prepare_s0_roots(
        production_root=production,
        output_root=output,
    )

    manifest_path = output / s0.G10S0_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_count"] = 99
    _write_json(manifest_path, manifest)
    with pytest.raises(s0.G10S0Error, match="manifest SHA"):
        s0.verify_s0_roots(
            production_root=production,
            output_root=output,
        )
