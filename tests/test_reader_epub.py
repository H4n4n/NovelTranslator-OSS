import json
import os
import shutil
import zipfile
from pathlib import Path

import build_checkpoint_epub as checkpoint
import epub_validate
from create_novel import create_novel_project, initial_project_state
from novel_project import NovelConfig, load_novel_project
from production_run_lock import production_run_lock

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_project(tmp_path, novel_id="reader_novel", chapters=None):
    profiles = tmp_path / "profiles"
    if not profiles.exists():
        shutil.copytree(REPO_ROOT / "profiles", profiles)
    config = NovelConfig(
        id=novel_id, title="Reader Novel", author="A. Writer",
        source_language="zh", target_language="en",
        profile="historical_military", final_chapter=100,
        chapter_numbering="global",
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(novel_id, tmp_path)
    commit(project, chapters or {1: "Chapter 1: Start\n\nHello."})
    return project


def commit(project, chapter_text):
    chapters = sorted(chapter_text)
    progress = initial_project_state()["progress.json"]
    progress.update({
        "completed": chapters, "last_completed": chapters[-1],
        "next_chapter": chapters[-1] + 1,
    })
    project.progress_file.write_text(json.dumps(progress), encoding="utf-8")
    for number, text in chapter_text.items():
        (project.chapter_dir / f"{number:04d}.md").write_text(text, encoding="utf-8")


def latest(project):
    return project.epub_dir / f"{project.config.id}_latest.epub"


def test_latest_file_report_and_range_file_created(tmp_path):
    project = make_project(tmp_path)
    report = checkpoint.rebuild_reader_epub(project)
    assert report["status"] == "BUILT"
    assert latest(project).is_file()
    assert (project.epub_dir / "reader_novel_chapters_0001_0001.epub").is_file()
    saved = json.loads((project.epub_dir / "latest_report.json").read_text("utf-8"))
    assert saved["chapters_covered"] == 1
    assert saved["path"] == str(latest(project))
    assert saved["validation"]["status"] in ("PASS", "NOT_AVAILABLE")
    assert saved["utc"]
    assert not list(project.epub_dir.glob("*.tmp"))


def test_old_latest_kept_when_build_fails(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert checkpoint.rebuild_reader_epub(project)["status"] == "BUILT"
    before = latest(project).read_bytes()
    commit(project, {1: "Chapter 1\n\nA", 2: "Chapter 2\n\nB"})

    def boom(path, book, options):
        raise OSError("simulated failure")

    monkeypatch.setattr(checkpoint.epub, "write_epub", boom)
    report = checkpoint.rebuild_reader_epub(project)
    assert report == {
        "status": "ERROR", "error_type": "OSError", "error": "simulated failure",
    }
    assert latest(project).read_bytes() == before


def test_not_available_without_jar(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.delenv(epub_validate.EPUBCHECK_ENV, raising=False)
    checkpoint.build_epub(project, write_latest=True)
    result = epub_validate.validate_epub(latest(project))
    assert result["status"] == "NOT_AVAILABLE"
    assert result["structural_ok"] is True
    missing = epub_validate.validate_epub(latest(project), tmp_path / "nope.jar")
    assert missing["status"] == "NOT_AVAILABLE"


def test_fallback_catches_broken_zip_and_bad_structure(tmp_path):
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"this is not a zip")
    result = epub_validate.validate_epub(broken)
    assert result["status"] == "FAIL" and result["errors"] >= 1

    nomime = tmp_path / "nomime.epub"
    with zipfile.ZipFile(nomime, "w") as archive:
        archive.writestr("other.txt", "x")
    result = epub_validate.validate_epub(nomime)
    assert result["status"] == "FAIL"
    assert any("mimetype" in message for message in result["messages"])


def test_xhtml_special_characters_survive(tmp_path):
    text = "Chapter 1: Tom & Jerry\n\nA < B && C > D, \"quoted\" <b>not bold</b>\x0b."
    project = make_project(tmp_path, chapters={1: text})
    output = checkpoint.build_epub(project, write_latest=True)
    with zipfile.ZipFile(latest(project)) as archive:
        name = next(n for n in archive.namelist() if n.endswith("chapter_0001.xhtml"))
        raw = archive.read(name).decode("utf-8")
    assert "A &lt; B &amp;&amp; C &gt; D" in raw
    assert "&lt;b&gt;not bold&lt;/b&gt;" in raw
    assert "Tom &amp; Jerry" in raw
    assert epub_validate.structural_check(output) == []


def test_skip_when_current_and_rebuild_when_chapter_newer(tmp_path):
    project = make_project(tmp_path)
    assert checkpoint.rebuild_reader_epub(project)["status"] == "BUILT"
    built = latest(project).stat().st_mtime_ns
    assert checkpoint.rebuild_reader_epub(project)["status"] == "SKIPPED"
    assert latest(project).stat().st_mtime_ns == built
    chapter = project.chapter_dir / "0001.md"
    future = latest(project).stat().st_mtime + 100
    os.utime(chapter, (future, future))
    assert checkpoint.rebuild_reader_epub(project)["status"] == "BUILT"


def test_no_deadlock_when_caller_holds_lock(tmp_path):
    project = make_project(tmp_path)
    with production_run_lock(project.root):
        assert checkpoint.rebuild_reader_epub(project)["status"] == "BUILT"
        # Asking for a second acquisition reports ERROR instead of raising.
        os.utime(project.chapter_dir / "0001.md", None)
        os.remove(latest(project))
        assert checkpoint.rebuild_reader_epub(project, take_lock=True)["status"] == "ERROR"


def test_cli_latest_flag(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    monkeypatch.setattr(checkpoint, "select_project", lambda novel: project)
    assert checkpoint.main(["--novel", "reader_novel", "--latest"]) == 0
    assert latest(project).is_file()
    assert '"status": "BUILT"' in capsys.readouterr().out
