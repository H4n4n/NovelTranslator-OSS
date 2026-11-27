"""A finished novel extended with TXT continuation files: numbering, volumes and EPUB novels."""
import hashlib
import json
from pathlib import Path

from ebooklib import epub
import pytest

import extract_epub
import extract_txt
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project

BODY = "그는 조용히 문을 열고 안으로 들어갔다. 방 안에는 아무도 없었다."


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_project(tmp_path, *, final_chapter, source_type="txt", numbering="spine_sequential"):
    profiles = tmp_path / "profiles" / "korean_fantasy"
    profiles.mkdir(parents=True)
    for path in (Path(__file__).resolve().parents[1] / "profiles" / "korean_fantasy").iterdir():
        (profiles / path.name).write_bytes(path.read_bytes())
    config = NovelConfig(id="sequel_fixture", title="Sequel Fixture", source_language="ko",
                         target_language="en", profile="korean_fantasy", final_chapter=final_chapter,
                         chapter_numbering=numbering, source_type=source_type)
    create_novel_project(config, tmp_path)
    return load_novel_project("sequel_fixture", tmp_path)


def chapters_text(numbers):
    return "".join(f"{n}화 제목{n}\n\n{BODY}\n\n" for n in numbers)


def add_continuation(project, text, overrides, **entry):
    data = text.encode()
    (project.source_dir / "novel_2.txt").write_bytes(data)
    overrides["continuations"] = [{"file": "novel_2.txt", "source_sha256": digest(data), **entry}]
    project.source_overrides_file.write_text(json.dumps(overrides, ensure_ascii=False), encoding="utf-8")


def txt_novel(tmp_path, numbers, final_chapter):
    project = make_project(tmp_path, final_chapter=final_chapter)
    first = chapters_text(numbers).encode()
    project.source_txt.write_bytes(first)
    return project, {"source_sha256": digest(first)}


def test_a_number_offset_renumbers_what_readers_see_and_keeps_the_authors_numbers(tmp_path):
    """A revised edition's chapters 453-454 follow an old edition's chapter 2 as Chapters 3-4."""
    project, overrides = txt_novel(tmp_path, [1, 2], 4)
    add_continuation(project, chapters_text([453, 454]), overrides, number_offset=-450)
    units = extract_txt.extract_source_txt(project)["chapters"]
    assert [u["local_chapter_start"] for u in units] == [1, 2, 3, 4]
    assert [u["source_number"] for u in units] == [1, 2, 453, 454]
    assert {u["volume_index"] for u in units} == {1}


def test_an_offset_below_chapter_one_or_of_the_wrong_type_blocks(tmp_path):
    (tmp_path / "low").mkdir()
    project, overrides = txt_novel(tmp_path / "low", [1, 2], 4)
    add_continuation(project, chapters_text([3, 4]), overrides, number_offset=-3)
    with pytest.raises(extract_txt.TXTExtractionError, match="below 1"):
        extract_txt.extract_source_txt(project)
    (tmp_path / "type").mkdir()
    project, overrides = txt_novel(tmp_path / "type", [1, 2], 4)
    add_continuation(project, chapters_text([3, 4]), overrides, number_offset="2")
    with pytest.raises(extract_txt.TXTExtractionError, match="integer"):
        extract_txt.extract_source_txt(project)


def test_a_sequel_that_starts_again_at_chapter_one_is_a_new_volume(tmp_path):
    project, overrides = txt_novel(tmp_path, [1, 2], 4)
    add_continuation(project, chapters_text([1, 2]), overrides)
    result = extract_txt.extract_source_txt(project)
    units = result["chapters"]
    assert [(u["volume_index"], u["local_chapter_start"]) for u in units] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    extract_txt.install_extraction(project, result)


def make_epub(path, numbers):
    book = epub.EpubBook()
    book.set_identifier("x")
    book.set_title("테스트")
    book.set_language("ko")
    items = []
    for number in numbers:
        item = epub.EpubHtml(title=f"第{number}章", file_name=f"c{number}.xhtml", lang="ko")
        item.content = f"<html><body><h1>第{number}章</h1><p>{BODY}</p><p>{BODY}</p></body></html>"
        book.add_item(item)
        items.append(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = items
    epub.write_epub(str(path), book)
    return path.read_bytes()


def test_an_epub_novel_takes_a_txt_continuation_and_keeps_its_own_units(tmp_path):
    (tmp_path / "alone").mkdir()
    alone = make_project(tmp_path / "alone", final_chapter=2, source_type="epub", numbering="global")
    make_epub(alone.source_epub, [1, 2])
    before = extract_epub.extract_source_epub(alone)
    assert not before.validation_errors and not before.continuations

    project = make_project(tmp_path, final_chapter=4, source_type="epub", numbering="global")
    data = make_epub(project.source_epub, [1, 2])
    add_continuation(project, chapters_text([3, 4]), {"source_sha256": digest(data)})
    result = extract_epub.extract_source_epub(project)
    assert not result.validation_errors, result.validation_errors
    assert result.chapters[:2] == before.chapters
    assert [u["chapter"] for u in result.chapters] == [1, 2, 3, 4]
    assert {u["source_file"] for u in result.chapters[2:]} == {"novel_2.txt"}
    assert result.continuations[0]["production_index_start"] == 3
    extract_epub.install_extraction(project, result)
    assert len(json.loads(project.chapters_file.read_text(encoding="utf-8"))) == 4


def test_an_epub_novel_with_too_small_a_final_chapter_is_blocked(tmp_path):
    project = make_project(tmp_path, final_chapter=3, source_type="epub", numbering="global")
    data = make_epub(project.source_epub, [1, 2])
    add_continuation(project, chapters_text([3, 4]), {"source_sha256": digest(data)})
    result = extract_epub.extract_source_epub(project)
    assert any("configured final_chapter is 3" in error for error in result.validation_errors)
    with pytest.raises(extract_epub.ExtractionValidationError):
        extract_epub.install_extraction(project, result)
