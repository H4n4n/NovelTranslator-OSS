import copy
import hashlib
import json
from pathlib import Path

import pytest

import final_editorial as editorial
import proven_stage_cleanup as stage_cleanup
from build_publication_chapters import framed_corpus_fingerprint
from novel_project import NovelConfig, NovelProject


REPO_ROOT = Path(__file__).resolve().parents[1]
ROGUE_MANIFEST = (
    REPO_ROOT / "novels" / "rogue_first_prince" / "config" / "final_editorial.json"
)


def make_project(tmp_path):
    root = tmp_path / "novels" / "fixture_novel"
    root.mkdir(parents=True)
    config = NovelConfig(
        id="fixture_novel",
        title="Fixture",
        author="Author",
        source_language="ko",
        target_language="en",
        profile="historical_military",
        final_chapter=3,
    )
    return NovelProject(tmp_path, root, config)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def install_fixture(tmp_path):
    project = make_project(tmp_path)
    paths = editorial.editorial_paths(project)
    paths.publication_dir.mkdir(parents=True)
    payloads = [
        ("0001.md", b"Prologue\r\n\r\nOPEN\r\n"),
        ("0002.md", b"Chapter 2. Wrong (1)\r\n\r\nBODY\r\n"),
        ("0003.md", b"Chapter 3. Stable\n\nEND"),
    ]
    for name, data in payloads:
        (paths.publication_dir / name).write_bytes(data)
    fingerprint = framed_corpus_fingerprint(payloads)
    write_json(
        paths.cleanup_report,
        {
            "schema_version": 1,
            "revision": "publication-cleanup-v1",
            "status": "success",
            "ready": True,
            "success": True,
            "blocker_count": 0,
            "output_corpus_fingerprint": fingerprint,
        },
    )
    source = "Fixture source\n\n2화. 제목(1)\n"
    source_bytes = source.encode("utf-8")
    paths.korean_source.parent.mkdir(parents=True)
    paths.korean_source.write_bytes(source_bytes)
    anchor = "2화. 제목(1)"
    manifest = {
        "schema_version": 1,
        "revision": "fixture-final-editorial-v1",
        "novel": "fixture_novel",
        "immutable_publication_checkpoint": {
            "file_count": 3,
            "aggregate_byte_count": fingerprint["aggregate_byte_count"],
            "aggregate_sha256": fingerprint["aggregate_sha256"],
        },
        "korean_source": {
            "identity": "source/novel.txt",
            "byte_count": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "preservation_anchors": [],
        },
        "authorized_set": {
            "operation_count": 1,
            "heading_operation_count": 1,
            "prose_operation_count": 0,
            "affected_units": [2],
        },
        "edits": [
            {
                "edit_id": "title-fixture-u0002",
                "unit": 2,
                "edit_category": "CANONICAL_TITLE",
                "scope": "heading",
                "expected_english": "Wrong",
                "replacement": "Right",
                "confidence": "SOURCE_BACKED_CANONICAL",
                "evidence_type": "SOURCE_HEADING_SEMANTICS",
                "source_evidence": [
                    {
                        "source_identity": "fixture-title",
                        "source_unit": 2,
                        "korean_heading": anchor,
                        "anchor_text": anchor,
                        "source_locator": {
                            "kind": "exact_utf8_line",
                            "line_number_hint": 3,
                            "occurrence_index": 1,
                            "exact_line_occurrence_count": 1,
                        },
                        "anchor_sha256": hashlib.sha256(anchor.encode()).hexdigest(),
                        "evidence_type": "SOURCE_HEADING_SEMANTICS",
                    }
                ],
            }
        ],
        "preservation_guards": [
            {
                "guard_id": "prologue",
                "units": [1],
                "expected_heading_lines": {"1": ["Prologue", ""]},
            }
        ],
    }
    write_json(paths.authorization, manifest)
    return project, paths, manifest, payloads


def ready_fixture(tmp_path):
    project, paths, manifest, payloads = install_fixture(tmp_path)
    plan = editorial.build_plan_report(project)
    assert plan["ready"] is True
    editorial.write_plan_reports(project, plan)
    return project, paths, manifest, payloads, plan


def rogue_manifest():
    return json.loads(ROGUE_MANIFEST.read_text(encoding="utf-8"))


def rogue_synthetic_payloads(manifest):
    edits = {item["unit"]: item for item in manifest["edits"]}
    guarded = {}
    for guard in manifest["preservation_guards"]:
        guarded.update({int(unit): lines for unit, lines in guard["expected_heading_lines"].items()})
    payloads = []
    for unit in range(1, 352):
        if unit in guarded:
            lines = guarded[unit]
            text = "\r\n".join(lines) + "\r\nUNCHANGED\r\n"
        elif unit in edits:
            edit = edits[unit]
            if edit["scope"] == "prose":
                text = f"Chapter {unit}. Stable\r\n\r\nBefore {edit['expected_english']} after\r\n"
            elif edit["expected_english"].startswith(f"Chapter {unit}"):
                text = edit["expected_english"] + "\r\n\r\nBODY\r\n"
            elif unit == 90:
                text = f"Chapter 90\r\n{edit['expected_english']} Title (1)\r\n\r\nBODY\r\n"
            else:
                text = f"Chapter {unit}. 99. {edit['expected_english']} (1)\r\n\r\nBODY\r\n"
        else:
            text = f"Chapter {unit}. Stable\n\nBODY-{unit}"
        payloads.append((f"{unit:04d}.md", text.encode("utf-8")))
    return payloads


def test_publication_fingerprint_mismatch_fails(tmp_path):
    project, paths, _, _ = install_fixture(tmp_path)
    (paths.publication_dir / "0003.md").write_bytes(b"Chapter 3. Changed\n\nEND")
    report = editorial.build_plan_report(project)
    assert report["ready"] is False
    assert report["blocker_count"] == 1
    assert "immutable publication" in report["blockers"][0] or "cleanup" in report["blockers"][0]


def test_authorization_manifest_mismatch_fails_apply(tmp_path):
    project, paths, manifest, _, _ = ready_fixture(tmp_path)
    manifest["note"] = "changed after plan"
    write_json(paths.authorization, manifest)
    with pytest.raises(editorial.FinalEditorialError, match="authorization fingerprint"):
        editorial.apply_ready_plan(project)


def test_korean_source_anchor_mismatch_fails(tmp_path):
    project, paths, _, _ = install_fixture(tmp_path)
    data = paths.korean_source.read_text(encoding="utf-8").replace("제목", "변경")
    paths.korean_source.write_text(data, encoding="utf-8")
    manifest = json.loads(paths.authorization.read_text(encoding="utf-8"))
    changed = paths.korean_source.read_bytes()
    manifest["korean_source"]["byte_count"] = len(changed)
    manifest["korean_source"]["sha256"] = hashlib.sha256(changed).hexdigest()
    write_json(paths.authorization, manifest)
    report = editorial.build_plan_report(project)
    assert report["ready"] is False
    assert "source anchor" in report["blockers"][0]


def test_expected_english_anchor_mismatch_fails(tmp_path):
    project, paths, _, _ = install_fixture(tmp_path)
    (paths.publication_dir / "0002.md").write_bytes(b"Chapter 2. Other (1)\r\n\r\nBODY\r\n")
    payloads = [(p.name, p.read_bytes()) for p in sorted(paths.publication_dir.glob("*.md"))]
    fingerprint = framed_corpus_fingerprint(payloads)
    cleanup = json.loads(paths.cleanup_report.read_text(encoding="utf-8"))
    cleanup["output_corpus_fingerprint"] = fingerprint
    write_json(paths.cleanup_report, cleanup)
    manifest = json.loads(paths.authorization.read_text(encoding="utf-8"))
    manifest["immutable_publication_checkpoint"].update(
        aggregate_byte_count=fingerprint["aggregate_byte_count"],
        aggregate_sha256=fingerprint["aggregate_sha256"],
    )
    write_json(paths.authorization, manifest)
    report = editorial.build_plan_report(project)
    assert report["ready"] is False
    assert "expected English anchor" in report["blockers"][0]


def test_planner_is_read_only_for_corpora(tmp_path):
    project, paths, _, payloads = install_fixture(tmp_path)
    before = framed_corpus_fingerprint(payloads)
    source_before = paths.korean_source.read_bytes()
    report = editorial.build_plan_report(project)
    editorial.write_plan_reports(project, report)
    after_payloads = [(p.name, p.read_bytes()) for p in sorted(paths.publication_dir.glob("*.md"))]
    assert framed_corpus_fingerprint(after_payloads) == before
    assert paths.korean_source.read_bytes() == source_before
    assert not paths.output_dir.exists()


def test_rogue_exact_authorized_counts_and_units():
    manifest = rogue_manifest()
    edits = editorial._validate_authorized_set(manifest)
    assert len(edits) == 37
    assert sum(item["scope"] == "heading" for item in edits) == 36
    assert sum(item["scope"] == "prose" for item in edits) == 1
    assert len({item["unit"] for item in edits}) == 37


def test_rogue_exact_314_unaffected_units_are_byte_identical():
    manifest = rogue_manifest()
    payloads = rogue_synthetic_payloads(manifest)
    outputs, operations, _ = editorial.simulate(manifest, payloads)
    source = dict(payloads)
    output = dict(outputs)
    affected = {item["unit"] for item in operations}
    assert len(affected) == 37
    assert sum(source[name] == output[name] for name in source if int(name[:4]) not in affected) == 314


@pytest.mark.parametrize(
    "unit,expected,replacement",
    [
        (90, "28\\.", "28."),
        (26, "Chapter 26.08. When Sword Meets Sword (1)", "Chapter 26 — 08. When Sword Meets Sword (1)"),
        (29, "Chapter 29.10. Starting Out Ordinary, Ending Extraordinary (1)", "Chapter 29 — 10. Starting Out Ordinary, Ending Extraordinary (1)"),
        (34, "Chapter 34.11. When My Thinking Changed, What I Saw Changed Too (3)", "Chapter 34 — 11. When My Thinking Changed, What I Saw Changed Too (3)"),
        (143, "Chapter 143.45. The Day the Sky Road Opens (1)", "Chapter 143 — 45. The Day the Sky Road Opens (1)"),
    ],
)
def test_structural_heading_edits_are_exact(unit, expected, replacement):
    edit = next(item for item in rogue_manifest()["edits"] if item["unit"] == unit)
    assert edit["expected_english"] == expected
    assert edit["replacement"] == replacement
    before = (expected + "\r\n\r\nBODY").encode()
    after = editorial.apply_edit_to_bytes(before, edit)
    assert after == (replacement + "\r\n\r\nBODY").encode()


@pytest.mark.parametrize(
    "decision_id,units,canonical",
    [
        ("title-0002", [6, 7], "Even Inherited Arts Have Class Differences"),
        ("title-0003", [8, 9, 10, 11], "A Famous Eatery"),
        ("title-0012", [37], "A Fateful Bond from the Past"),
        ("title-0030", [95, 96], "Nothing Comes Free"),
        ("title-0050", [160, 161, 162], "An Ill-Fated Bond, or Destiny"),
        ("title-0069", [238, 241, 242], "Those Who Chase the Dawn"),
        ("title-0071", [249, 250], "The Virtues of a Crown Prince"),
        ("title-0085", list(range(320, 331)), "After Walking a Thousand Years, I Finally Arrived"),
        ("title-0089", [342, 343, 344], "Oh, Your Imperial Majesty!"),
    ],
)
def test_all_nine_title_families(decision_id, units, canonical):
    edits = [item for item in rogue_manifest()["edits"] if item["edit_id"].startswith(decision_id)]
    assert [item["unit"] for item in edits] == units
    assert {item["replacement"] for item in edits} == {canonical}


def test_title_0012_unit35_is_a_separate_guarded_identity():
    manifest = rogue_manifest()
    assert 35 not in manifest["authorized_set"]["affected_units"]
    guard = next(g for g in manifest["preservation_guards"] if 35 in g["units"])
    assert "A Connection from the Past" in guard["expected_heading_lines"]["35"][0]


def test_title_0085_context_metadata_distinguishes_subject_evidence():
    edits = [e for e in rogue_manifest()["edits"] if e["edit_id"].startswith("title-0085")]
    assert len(edits) == 11
    assert all(e["confidence"] == "CONTEXT_BACKED_CANONICAL" for e in edits)
    assert all(e["heading_grammatical_subject_explicit"] is False for e in edits)
    assert all(e["first_person_subject_from_context"] is True for e in edits)
    assert all({x["evidence_type"] for x in e["source_evidence"]} == {
        "SOURCE_HEADING_SUBJECT_OMITTED", "ARC_CONTEXT_FIRST_PERSON_EXACT"
    } for e in edits)


def test_unit53_exact_one_token_replacement():
    edit = next(e for e in rogue_manifest()["edits"] if e["unit"] == 53)
    before = b"Chapter 53. Stable\r\n\r\nBefore every ringer after"
    after = editorial.apply_edit_to_bytes(before, edit)
    assert after == b"Chapter 53. Stable\r\n\r\nBefore every finger after"


def test_byte_surgery_preserves_bom_newlines_final_newline_and_unrelated_bytes():
    edit = next(e for e in rogue_manifest()["edits"] if e["unit"] == 53)
    prefix = b"\xef\xbb\xbfChapter 53. Stable\r\n\r\n\xe2\x98\x83 Before "
    suffix = b" after\t\r\n"
    before = prefix + b"every ringer" + suffix
    after = editorial.apply_edit_to_bytes(before, edit)
    assert after == prefix + b"every finger" + suffix


@pytest.mark.parametrize(
    "body,count",
    [(b"no typo here", 0), (b"every ringer and every ringer", 2)],
)
def test_unit53_absent_or_duplicate_token_fails(body, count):
    edit = next(e for e in rogue_manifest()["edits"] if e["unit"] == 53)
    with pytest.raises(editorial.FinalEditorialError, match=f"found {count}"):
        editorial.apply_edit_to_bytes(b"Chapter 53. Stable\n\n" + body, edit)


@pytest.mark.parametrize(
    "guard_id",
    [
        "prologue-then-visible-chapter-2-no-invented-chapter-1",
        "title-0080-source-authored-1-2-3-3-4-5",
        "title-0081-unit-301-source-omits-part-1",
        "epilogue-layout-preserved",
    ],
)
def test_explicit_preservation_guards_pass(guard_id):
    manifest = rogue_manifest()
    payloads = rogue_synthetic_payloads(manifest)
    _, _, guards = editorial.simulate(manifest, payloads)
    assert next(g for g in guards if g["guard_id"] == guard_id)["status"] == "preserved"


def test_apply_refuses_existing_output(tmp_path):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    paths.output_dir.mkdir()
    with pytest.raises(editorial.FinalEditorialError, match="refusing to overwrite"):
        editorial.apply_ready_plan(project)


@pytest.mark.parametrize("failure_target", ["output_dir", "apply_json", "apply_md"])
def test_apply_is_transactional_on_atomic_publish_failure(
    tmp_path, monkeypatch, failure_target
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    def fail_output(source, destination):
        if Path(destination) == getattr(paths, failure_target):
            raise OSError("injected publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", fail_output)
    with pytest.raises(OSError, match="injected"):
        editorial.apply_ready_plan(project)
    assert not paths.output_dir.exists()
    assert not paths.apply_json.exists()
    assert not paths.apply_md.exists()
    assert not list(paths.output_dir.parent.glob(".editorial-stage-*"))


def test_terminal_editorial_commit_then_error_is_classified_as_success(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    def commit_then_error(source, destination):
        destination = Path(destination)
        if destination == paths.output_dir:
            real_replace(source, destination)
            raise OSError("injected error after terminal editorial rename")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", commit_then_error)

    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert not list(paths.output_dir.parent.glob(".editorial-stage-*"))
    assert not paths.apply_json.with_name(f".{paths.apply_json.name}.tmp").exists()
    assert not paths.apply_md.with_name(f".{paths.apply_md.name}.tmp").exists()


def test_ambiguous_terminal_editorial_commit_then_error_is_preserved(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    def commit_tamper_then_error(source, destination):
        destination = Path(destination)
        if destination == paths.output_dir:
            real_replace(source, destination)
            first = sorted(paths.output_dir.iterdir())[0]
            first.write_bytes(first.read_bytes() + b"\nTAMPERED\n")
            raise OSError("injected ambiguous post-commit editorial error")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", commit_tamper_then_error)

    with pytest.raises(
        editorial.FinalEditorialError,
        match="ambiguous terminal editorial commit outcome",
    ):
        editorial.apply_ready_plan(project)

    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert b"TAMPERED" in sorted(paths.output_dir.iterdir())[0].read_bytes()
    assert not list(paths.output_dir.parent.glob(".editorial-stage-*"))


def test_hard_stop_before_terminal_editorial_commit_never_exposes_corpus(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace
    destinations = []

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        destination = Path(destination)
        destinations.append(destination)
        if destination == paths.output_dir:
            raise SimulatedHardStop("simulated process stop before corpus commit")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_terminal_commit)

    with pytest.raises(SimulatedHardStop, match="before corpus commit"):
        editorial.apply_ready_plan(project)

    assert destinations == [paths.apply_json, paths.apply_md, paths.output_dir]
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert not paths.output_dir.exists()


def test_exact_interrupted_editorial_report_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        if Path(destination) == paths.output_dir:
            raise SimulatedHardStop("simulated stop")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop):
        editorial.apply_ready_plan(project)

    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert not paths.output_dir.exists()

    monkeypatch.setattr(editorial.os, "replace", real_replace)
    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()


def test_interrupted_editorial_cleanup_remains_recoverable(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace
    real_unlink = Path.unlink

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        if Path(destination) == paths.output_dir:
            raise SimulatedHardStop("simulated stop")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop):
        editorial.apply_ready_plan(project)
    monkeypatch.setattr(editorial.os, "replace", real_replace)

    def fail_json_cleanup(path, *args, **kwargs):
        if Path(path) == paths.apply_json:
            raise OSError("injected recovery cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_json_cleanup)
    with pytest.raises(OSError, match="recovery cleanup"):
        editorial.apply_ready_plan(project)

    assert paths.apply_json.is_file()
    assert not paths.apply_md.exists()
    assert not paths.output_dir.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()


def test_partial_editorial_stage_delete_resumes_from_cleanup_journal(
    tmp_path, monkeypatch
):
    project, paths, manifest, payloads, _ = ready_fixture(tmp_path)
    outputs, _, _ = editorial.simulate(manifest, payloads)
    stale_stage = paths.output_dir.parent / ".editorial-stage-stale"
    stale_stage.mkdir(parents=True)
    for name, data in outputs:
        (stale_stage / name).write_bytes(data)

    real_rmtree = stage_cleanup.shutil.rmtree
    failed = {"done": False}

    def partial_then_fail(path):
        path = Path(path)
        if (
            path.name.startswith(".final-editorial.cleanup-stage-")
            and not failed["done"]
        ):
            failed["done"] = True
            first = sorted(path.iterdir())[0]
            first.unlink()
            raise OSError("injected partial editorial stage cleanup")
        return real_rmtree(path)

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", partial_then_fail)
    with pytest.raises(OSError, match="partial editorial stage cleanup"):
        editorial.apply_ready_plan(project)

    assert not stale_stage.exists()
    assert not paths.output_dir.exists()
    assert not list(paths.output_dir.parent.glob(".editorial-stage-*"))
    assert len(
        list(paths.output_dir.parent.glob(".final-editorial.cleanup-journal-*.json"))
    ) == 1
    assert len(
        list(paths.output_dir.parent.glob(".final-editorial.cleanup-stage-*"))
    ) == 1

    monkeypatch.setattr(stage_cleanup.shutil, "rmtree", real_rmtree)
    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert not list(
        paths.output_dir.parent.glob(".final-editorial.cleanup-journal-*.json")
    )
    assert not list(paths.output_dir.parent.glob(".final-editorial.cleanup-stage-*"))


def test_divergent_interrupted_editorial_report_prefix_blocks_without_cleanup(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        if Path(destination) == paths.output_dir:
            raise SimulatedHardStop("simulated stop")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop):
        editorial.apply_ready_plan(project)

    original_md = paths.apply_md.read_bytes()
    paths.apply_json.write_bytes(paths.apply_json.read_bytes() + b" ")

    monkeypatch.setattr(editorial.os, "replace", real_replace)
    with pytest.raises(
        editorial.FinalEditorialError,
        match="does not match reconstructed bytes",
    ):
        editorial.apply_ready_plan(project)

    assert paths.apply_json.is_file()
    assert paths.apply_md.read_bytes() == original_md
    assert not paths.output_dir.exists()


def test_impossible_editorial_markdown_only_prefix_fails_closed(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_terminal_commit(source, destination):
        if Path(destination) == paths.output_dir:
            raise SimulatedHardStop("simulated stop")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_terminal_commit)
    with pytest.raises(SimulatedHardStop):
        editorial.apply_ready_plan(project)

    paths.apply_json.unlink()
    monkeypatch.setattr(editorial.os, "replace", real_replace)

    with pytest.raises(
        editorial.FinalEditorialError,
        match="Markdown apply report exists without JSON",
    ):
        editorial.apply_ready_plan(project)

    assert paths.apply_md.is_file()
    assert not paths.output_dir.exists()


def test_exact_stale_editorial_stage_recovers_before_fresh_apply(tmp_path):
    project, paths, manifest, payloads, _ = ready_fixture(tmp_path)
    outputs, _, _ = editorial.simulate(manifest, payloads)
    stale_stage = paths.output_dir.parent / ".editorial-stage-stale"
    stale_stage.mkdir(parents=True)
    for name, data in outputs:
        (stale_stage / name).write_bytes(data)

    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert not stale_stage.exists()


def test_divergent_stale_editorial_stage_blocks_without_cleanup(tmp_path):
    project, paths, manifest, payloads, _ = ready_fixture(tmp_path)
    outputs, _, _ = editorial.simulate(manifest, payloads)
    stale_stage = paths.output_dir.parent / ".editorial-stage-stale"
    stale_stage.mkdir(parents=True)
    for name, data in outputs:
        (stale_stage / name).write_bytes(data)
    (stale_stage / "0002.md").write_bytes(b"divergent")

    with pytest.raises(
        editorial.FinalEditorialError,
        match="does not match reconstructed output",
    ):
        editorial.apply_ready_plan(project)

    assert stale_stage.is_dir()
    assert not paths.output_dir.exists()


def test_exact_editorial_report_temp_prefix_recovers_on_retry(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace

    class SimulatedHardStop(BaseException):
        pass

    def stop_before_first_report_publish(source, destination):
        if Path(destination) == paths.apply_json:
            raise SimulatedHardStop("simulated stop before report publish")
        return real_replace(source, destination)

    monkeypatch.setattr(editorial.os, "replace", stop_before_first_report_publish)
    with pytest.raises(SimulatedHardStop):
        editorial.apply_ready_plan(project)

    json_temp = paths.apply_json.with_name(f".{paths.apply_json.name}.tmp")
    md_temp = paths.apply_md.with_name(f".{paths.apply_md.name}.tmp")
    assert json_temp.is_file()
    assert md_temp.is_file()
    assert not paths.apply_json.exists()
    assert not paths.apply_md.exists()
    assert not paths.output_dir.exists()

    monkeypatch.setattr(editorial.os, "replace", real_replace)
    report = editorial.apply_ready_plan(project)

    assert report["success"] is True
    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert not json_temp.exists()
    assert not md_temp.exists()


def test_editorial_source_validation_finishes_before_terminal_corpus_commit(
    tmp_path, monkeypatch
):
    project, paths, _, _, _ = ready_fixture(tmp_path)
    real_replace = editorial.os.replace
    real_inventory = editorial._inventory_payloads
    committed = {"value": False}

    def observed_replace(source, destination):
        result = real_replace(source, destination)
        if Path(destination) == paths.output_dir:
            committed["value"] = True
        return result

    def guarded_inventory(directory, expected_count):
        if committed["value"] and Path(directory) == paths.publication_dir:
            pytest.fail("editorial source was read after terminal corpus commit")
        return real_inventory(directory, expected_count)

    monkeypatch.setattr(editorial.os, "replace", observed_replace)
    monkeypatch.setattr(editorial, "_inventory_payloads", guarded_inventory)

    report = editorial.apply_ready_plan(project)

    assert committed["value"] is True
    assert paths.output_dir.is_dir()
    assert paths.apply_json.is_file()
    assert paths.apply_md.is_file()
    assert report["publication_source_unchanged"] is True



def test_apply_fingerprint_equals_plan_and_source_is_unchanged(tmp_path):
    project, paths, _, payloads, plan = ready_fixture(tmp_path)
    before = framed_corpus_fingerprint(payloads)
    report = editorial.apply_ready_plan(project)
    output_payloads = [(p.name, p.read_bytes()) for p in sorted(paths.output_dir.glob("*.md"))]
    assert framed_corpus_fingerprint(output_payloads) == plan["expected_output_corpus_fingerprint"]
    assert report["output_corpus_fingerprint"] == plan["expected_output_corpus_fingerprint"]
    source_payloads = [(p.name, p.read_bytes()) for p in sorted(paths.publication_dir.glob("*.md"))]
    assert framed_corpus_fingerprint(source_payloads) == before
    assert report["publication_source_unchanged"] is True


def test_plan_schema_and_apply_schema_revisions(tmp_path):
    project, _, _, _, plan = ready_fixture(tmp_path)
    report = editorial.apply_ready_plan(project)
    assert (plan["schema_version"], plan["revision"]) == (1, "final-editorial-plan-v1.2")
    assert (report["schema_version"], report["revision"]) == (1, "final-editorial-apply-v1.2")
