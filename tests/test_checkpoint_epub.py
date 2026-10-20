import hashlib
import json
from pathlib import Path
import shutil
import socket
import zipfile

import pytest

import build_checkpoint_epub as checkpoint
from status_project import show_status
from validate_project import validate_project
from create_novel import create_novel_project, initial_project_state
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_MING_FILES = (
    "data/progress.json",
    "data/glossary.json",
    "output/pending/0205.md",
    "output/pending/0205.json",
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


def prepare_repo(tmp_path):
    profiles = tmp_path / "profiles"
    if not profiles.exists():
        shutil.copytree(REPO_ROOT / "profiles", profiles)
    return tmp_path


def create_project(
    repo_root,
    novel_id,
    title=None,
    author=None,
    source_language="zh",
    target_language="en",
    chapter_numbering="global",
):
    prepare_repo(repo_root)
    config = NovelConfig(
        id=novel_id,
        title=title or novel_id,
        author=author,
        source_language=source_language,
        target_language=target_language,
        profile="historical_military",
        final_chapter=100,
        chapter_numbering=chapter_numbering,
    )
    create_novel_project(config, repo_root)
    return load_novel_project(novel_id, repo_root)


def commit_chapters(project, chapter_text):
    chapters = sorted(chapter_text)
    assert chapters == list(range(1, chapters[-1] + 1))
    progress = initial_project_state()["progress.json"]
    progress.update({
        "completed": chapters,
        "last_completed": chapters[-1],
        "next_chapter": chapters[-1] + 1,
    })
    project.progress_file.write_text(json.dumps(progress), encoding="utf-8")
    for chapter, text in chapter_text.items():
        (project.chapter_dir / f"{chapter:04d}.md").write_text(
            text, encoding="utf-8"
        )


def epub_member_text(path, suffix):
    with zipfile.ZipFile(path) as archive:
        name = next(name for name in archive.namelist() if name.endswith(suffix))
        return archive.read(name).decode("utf-8")


def epub_chapter_members(path):
    with zipfile.ZipFile(path) as archive:
        return sorted(
            name for name in archive.namelist()
            if "/chapters/chapter_" in name and name.endswith(".xhtml")
        )


def test_ming_epub_contains_only_selected_ming_chapters(tmp_path):
    ming = create_project(tmp_path, "ming_novel", "Fixture Ming")
    other = create_project(tmp_path, "other_novel", "Other Novel")
    commit_chapters(ming, {1: "Chapter 1\n\nMING-ONLY-MARKER"})
    commit_chapters(other, {1: "Chapter 1\n\nOTHER-ONLY-MARKER"})
    output = checkpoint.build_epub(ming)
    rendered = epub_member_text(output, "chapters/chapter_0001.xhtml")
    assert "MING-ONLY-MARKER" in rendered
    assert "OTHER-ONLY-MARKER" not in rendered


def test_two_novels_can_both_have_chapter_one_without_cross_contamination(tmp_path):
    first = create_project(tmp_path, "novel_a")
    second = create_project(tmp_path, "novel_b")
    commit_chapters(first, {1: "Chapter 1\n\nNOVEL-A"})
    commit_chapters(second, {1: "Chapter 1\n\nNOVEL-B"})
    first_output = checkpoint.build_epub(first)
    second_output = checkpoint.build_epub(second)
    first_text = epub_member_text(first_output, "chapters/chapter_0001.xhtml")
    second_text = epub_member_text(second_output, "chapters/chapter_0001.xhtml")
    assert "NOVEL-A" in first_text and "NOVEL-B" not in first_text
    assert "NOVEL-B" in second_text and "NOVEL-A" not in second_text


def test_reset_numbered_epub_uses_production_order_and_local_headings(tmp_path):
    project = create_project(
        tmp_path,
        "reset_epub",
        chapter_numbering="spine_sequential",
    )
    commit_chapters(project, {
        1: "Chapter 1: Alpha Start\n\nALPHA-ONE",
        2: "Chapter 2: Alpha End\n\nALPHA-TWO",
        3: "Chapter 1: Beta Start\n\nBETA-ONE",
    })

    output = checkpoint.build_epub(project)

    assert [Path(name).name for name in epub_chapter_members(output)] == [
        "chapter_0001.xhtml", "chapter_0002.xhtml", "chapter_0003.xhtml"
    ]
    third = epub_member_text(output, "chapters/chapter_0003.xhtml")
    assert "<h1>Chapter 1: Beta Start</h1>" in third
    assert "BETA-ONE" in third
    with zipfile.ZipFile(output) as archive:
        nav_name = next(name for name in archive.namelist() if name.endswith("nav.xhtml"))
        nav = archive.read(nav_name).decode("utf-8")
    assert "Chapter 1: Alpha Start" in nav
    assert "Chapter 2: Alpha End" in nav
    assert "Chapter 1: Beta Start" in nav


def test_checkpoint_epub_orders_supplement_and_uses_its_translated_title(tmp_path):
    project = create_project(
        tmp_path, "supplement_epub", chapter_numbering="spine_sequential"
    )
    raw = json.loads(project.config_file.read_text(encoding="utf-8"))
    raw["final_chapter"] = 3
    project.config_file.write_text(json.dumps(raw), encoding="utf-8")
    project = load_novel_project("supplement_epub", tmp_path)
    project.chapters_file.write_text(json.dumps([
        {
            "chapter": 1, "production_index": 1, "unit_kind": "chapter",
            "local_chapter_number": 1, "local_chapter_start": 1,
            "local_chapter_end": 1, "volume_index": 1,
            "heading": "第一章 開始", "title": "開始",
        },
        {
            "chapter": 2, "production_index": 2, "unit_kind": "supplement",
            "volume_index": 1, "heading": "李閻全信息", "title": "李閻全信息",
            "source_file": "Text/info.xhtml", "spine_position": 2,
        },
        {
            "chapter": 3, "production_index": 3, "unit_kind": "chapter",
            "local_chapter_number": 2, "local_chapter_start": 2,
            "local_chapter_end": 2, "volume_index": 1,
            "heading": "第二章 繼續", "title": "繼續",
        },
    ], ensure_ascii=False), encoding="utf-8")
    commit_chapters(project, {
        1: "# Chapter 1: Beginning\n\nONE",
        2: "# Li Yan — Full Information\n\nMECHANICS",
        3: "# Chapter 2: Continuing\n\nTWO",
    })
    output = checkpoint.build_epub(project)
    members = epub_chapter_members(output)
    assert len(members) == 3
    first = epub_member_text(output, "chapter_0001.xhtml")
    supplement = epub_member_text(output, "chapter_0002.xhtml")
    last = epub_member_text(output, "chapter_0003.xhtml")
    assert "Chapter 1: Beginning" in first
    assert "Li Yan — Full Information" in supplement
    assert "<p># Li Yan — Full Information</p>" not in supplement
    assert "Chapter 2" not in supplement and "Supplement 2" not in supplement
    assert "Chapter 2: Continuing" in last
    assert validate_project(project) == 0
    assert show_status(project) == 0


def test_checkpoint_epub_uses_named_txt_unit_heading_without_production_number(tmp_path):
    project = create_project(
        tmp_path, "txt_named_epub", source_language="ko",
        chapter_numbering="spine_sequential",
    )
    raw = json.loads(project.config_file.read_text(encoding="utf-8"))
    raw["final_chapter"] = 1
    raw["source_type"] = "txt"
    project.config_file.write_text(json.dumps(raw), encoding="utf-8")
    project = load_novel_project("txt_named_epub", tmp_path)
    project.chapters_file.write_text(json.dumps([{
        "chapter": 1,
        "production_index": 1,
        "unit_kind": "prologue",
        "volume_index": 1,
        "heading": "Prologue",
        "title": "Prologue",
        "source_file": "novel.txt",
        "source_line_start": 1,
    }]), encoding="utf-8")
    commit_chapters(project, {1: "# Prologue\n\nOPENING"})
    output = checkpoint.build_epub(project)
    rendered = epub_member_text(output, "chapter_0001.xhtml")
    assert "<h1>Prologue</h1>" in rendered
    assert "<p># Prologue</p>" not in rendered
    assert "Chapter 1" not in rendered


def test_pending_chapter_is_never_included_or_read_as_committed(tmp_path):
    project = create_project(tmp_path, "pending_test")
    commit_chapters(project, {1: "Chapter 1\n\nCOMMITTED"})
    (project.pending_dir / "0002.md").write_text(
        "Chapter 2\n\nPENDING-PAID-MARKER", encoding="utf-8"
    )
    (project.pending_dir / "0002.json").write_text(
        "deliberately invalid metadata that must not be read", encoding="utf-8"
    )
    output = checkpoint.build_epub(project)
    with zipfile.ZipFile(output) as archive:
        all_content = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"COMMITTED" in all_content
    assert b"PENDING-PAID-MARKER" not in all_content
    assert not any("chapter_0002.xhtml" in name for name in epub_chapter_members(output))


def test_unexpected_extra_committed_file_fails_before_output(tmp_path):
    project = create_project(tmp_path, "extra_test")
    commit_chapters(project, {1: "Chapter 1\n\nONE"})
    (project.chapter_dir / "0002.md").write_text("EXTRA", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unexpected extra committed chapter files: 2"):
        checkpoint.build_epub(project)
    assert not list(project.epub_dir.glob("*.epub"))


def test_missing_committed_file_fails_before_output(tmp_path):
    project = create_project(tmp_path, "missing_test")
    commit_chapters(project, {
        1: "Chapter 1\n\nONE",
        2: "Chapter 2\n\nTWO",
    })
    (project.chapter_dir / "0002.md").unlink()
    with pytest.raises(RuntimeError, match="Missing committed chapter files: 2"):
        checkpoint.build_epub(project)
    assert not list(project.epub_dir.glob("*.epub"))


def test_custom_range_includes_only_requested_committed_chapters(tmp_path):
    project = create_project(tmp_path, "range_test")
    commit_chapters(project, {
        chapter: f"Chapter {chapter}\n\nMARKER-{chapter}"
        for chapter in range(1, 5)
    })
    output = checkpoint.build_epub(project, from_chapter=2, to_chapter=3)
    assert output.name == "range_test_chapters_0002_0003.epub"
    assert [Path(name).name for name in epub_chapter_members(output)] == [
        "chapter_0002.xhtml", "chapter_0003.xhtml"
    ]
    combined = epub_member_text(output, "chapters/chapter_0002.xhtml")
    combined += epub_member_text(output, "chapters/chapter_0003.xhtml")
    assert "MARKER-2" in combined and "MARKER-3" in combined
    assert "MARKER-1" not in combined and "MARKER-4" not in combined


@pytest.mark.parametrize(
    "from_chapter,to_chapter,message",
    [
        (0, 2, "from_chapter must be at least 1"),
        (3, 2, "to_chapter must be greater"),
        (1, 4, "exceeds the safely committed range"),
        (4, None, "to_chapter must be greater"),
    ],
)
def test_invalid_ranges_fail_without_output(
    tmp_path, from_chapter, to_chapter, message,
):
    project = create_project(tmp_path, "invalid_range")
    commit_chapters(project, {
        chapter: f"Chapter {chapter}\n\nBODY" for chapter in range(1, 4)
    })
    with pytest.raises(ValueError, match=message):
        checkpoint.build_epub(
            project, from_chapter=from_chapter, to_chapter=to_chapter
        )
    assert not list(project.epub_dir.glob("*.epub"))


def test_output_isolated_to_selected_novel_epub_directory(tmp_path):
    first = create_project(tmp_path, "novel_a")
    second = create_project(tmp_path, "novel_b")
    commit_chapters(first, {1: "Chapter 1\n\nA"})
    commit_chapters(second, {1: "Chapter 1\n\nB"})
    output = checkpoint.build_epub(first)
    assert output == first.epub_dir / "novel_a_chapters_0001_0001.epub"
    assert output.is_file()
    assert not list(second.epub_dir.glob("*.epub"))


def test_epub_metadata_comes_from_selected_novel(tmp_path):
    project = create_project(
        tmp_path,
        "metadata_novel",
        title="Selected Novel",
        author="Selected Author",
        source_language="zh",
        target_language="fr",
    )
    commit_chapters(project, {1: "Chapter 1\n\nBODY"})
    output = checkpoint.build_epub(project)
    opf = epub_member_text(output, ".opf")
    assert "Selected Novel" in opf
    assert "Selected Author" in opf
    assert "noveltranslator-metadata_novel-1-1" in opf
    assert ">fr</dc:language>" in opf
    assert ">zh</dc:source>" in opf


def test_absent_author_is_not_invented(tmp_path):
    project = create_project(tmp_path, "anonymous_novel", title="No Author Novel")
    commit_chapters(project, {1: "Chapter 1\n\nBODY"})
    output = checkpoint.build_epub(project)
    opf = epub_member_text(output, ".opf")
    assert "<dc:creator" not in opf
    assert "Unknown" not in opf


@pytest.mark.parametrize("separator", ["~~~~~~~", "***", "---", "* * *"])
def test_scene_break_markdown_becomes_readable_separator(separator):
    rendered = checkpoint.markdownish_to_xhtml(
        f"Chapter 1\n\nBefore\n\n{separator}\n\nAfter", "Fallback"
    )
    assert rendered.count('class="scene-break"') == 1
    assert '<p class="scene-break">* * *</p>' in rendered
    if separator != "* * *":
        assert f">{separator}<" not in rendered


def test_epub_build_makes_zero_api_or_network_calls(tmp_path, monkeypatch):
    project = create_project(tmp_path, "offline_epub")
    commit_chapters(project, {1: "Chapter 1\n\nOFFLINE"})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("EPUB build attempted network access"),
    )
    assert checkpoint.build_epub(project).is_file()


def test_failed_write_preserves_existing_epub_and_removes_temp(tmp_path, monkeypatch):
    project = create_project(tmp_path, "atomic_epub")
    commit_chapters(project, {1: "Chapter 1\n\nBODY"})
    output = project.epub_dir / "atomic_epub_chapters_0001_0001.epub"
    output.write_bytes(b"existing valid checkpoint")

    def fail_write(path, book, options):
        Path(path).write_bytes(b"partial")
        raise OSError("simulated EPUB write failure")

    monkeypatch.setattr(checkpoint.epub, "write_epub", fail_write)
    with pytest.raises(OSError, match="simulated EPUB write failure"):
        checkpoint.build_epub(project)
    assert output.read_bytes() == b"existing valid checkpoint"
    assert not list(project.epub_dir.glob("*.tmp"))


def test_successful_checkpoint_publish_does_not_unlink_after_replace(
    tmp_path, monkeypatch
):
    project = create_project(tmp_path, "truthful_checkpoint")
    commit_chapters(project, {1: "Chapter 1\n\nBODY"})
    replaced = {"done": False}
    real_publish = checkpoint.atomic_replace_prepared_file
    real_unlink = Path.unlink

    def observed_publish(temp_path, output_path):
        real_publish(temp_path, output_path)
        replaced["done"] = True

    def guarded_unlink(path, *args, **kwargs):
        if replaced["done"]:
            pytest.fail("checkpoint attempted filesystem cleanup after publish")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(checkpoint, "atomic_replace_prepared_file", observed_publish)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    output = checkpoint.build_epub(project)

    assert replaced["done"] is True
    assert output.is_file()


def test_epub_tests_do_not_modify_current_ming_checkpoint():
    ming_root = REPO_ROOT / "novels" / "ming_novel"

    missing = [
        relative
        for relative in PROTECTED_MING_FILES
        if not (ming_root / relative).is_file()
    ]

    if missing:
        pytest.skip("real Ming production checkpoint is not present")

    expected = {
        "data/progress.json": "5D2A0CBFC498FF5CAF030C129171C01E48C9D76B4839E8CB3C9420715DEC6A61",
        "data/glossary.json": "4A8B4818F6E3F4DA0BA90E469B350B8587D74A0B8DCA8714DFA64790185F2FF1",
        "output/pending/0205.md": "08551415A7CB18C28D79A41417B4FE3E88098646DF1DA49023AE208DE66237AB",
        "output/pending/0205.json": "D81C9614636C03E1984631731E450F97727BB5B34271DCBBB8730A2D4AA26007",
    }

    assert {
        relative: sha256(ming_root / relative)
        for relative in PROTECTED_MING_FILES
    } == expected
