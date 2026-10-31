import json
from pathlib import Path
import shutil

from ebooklib import epub
import pytest

from novel_intake import (
    analyze_source,
    create_from_analysis,
    detect_encoding_and_decode,
    detect_language,
    normalize_txt_source,
    slugify_title,
)
from novel_project import load_novel_project
import validate_project

REPO = Path(__file__).resolve().parents[1]

ZH_TEXT = "他们没有说话。这个国家的学生们都对时间过得很快感到惊讶，我们从东边来。" * 4
ZH_HANT_TEXT = "他們沒有說話。這個國家的學生們都對時間過得很快感到驚訝，我們從東邊來。" * 4
KO_BODY = "그는 조용히 문을 열고 안으로 들어갔다. 방 안에는 아무도 없었다. " * 6


def make_epub(path, chapters, title="測試小說", author="作者"):
    book = epub.EpubBook()
    book.set_identifier("x")
    book.set_title(title)
    book.set_language("zh")
    book.add_author(author)
    items = []
    for index, (heading, body) in enumerate(chapters, 1):
        item = epub.EpubHtml(title=heading, file_name=f"c{index}.xhtml", lang="zh")
        item.content = (
            f"<html><body><h1>{heading}</h1><p>{body}</p><p>{body}</p></body></html>"
        )
        book.add_item(item)
        items.append(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = items
    epub.write_epub(str(path), book)
    return path


def make_ko_txt(path, numbers, encoding="utf-8"):
    parts = ["나의 소설\n"]
    for n in numbers:
        parts.append(f"{n}화. 제목{n}\n\n{KO_BODY}\n")
    path.write_bytes("\n".join(parts).encode(encoding))
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELTRANSLATOR_NOVELS_ROOT", str(tmp_path / "novels"))
    shutil.copytree(REPO / "profiles", tmp_path / "profiles")
    return tmp_path


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gb18030", "big5", "cp949"])
def test_encoding_detection(encoding):
    sample = {
        "utf-8": ZH_TEXT, "utf-8-sig": ZH_TEXT, "gb18030": ZH_TEXT,
        "big5": ZH_HANT_TEXT, "cp949": KO_BODY,
    }[encoding]
    text, found, confidence = detect_encoding_and_decode(sample.encode(encoding))
    assert text == sample
    assert found == encoding
    assert confidence == "high"


def test_garbage_bytes_do_not_crash():
    text, _, _ = detect_encoding_and_decode(b"\xff\xfe\x00\x81\x00\x00\xfd")
    assert isinstance(text, str)


def test_language_detection():
    assert detect_language(ZH_TEXT) == "zh"
    assert detect_language(ZH_HANT_TEXT) == "zh-Hant"
    assert detect_language(KO_BODY) == "ko"
    assert detect_language("plain ascii") == "zh"


def test_normalize_txt_leaves_original(tmp_path):
    original = tmp_path / "raw.txt"
    original.write_bytes(KO_BODY.encode("cp949"))
    before = original.read_bytes()
    info = normalize_txt_source(original, tmp_path / "out" / "novel.txt")
    assert original.read_bytes() == before
    assert (tmp_path / "out" / "novel.txt").read_text(encoding="utf-8") == KO_BODY
    assert info["encoding"] == "cp949"


def test_slug(repo):
    assert slugify_title("The Great King's Rise!", repo_root=repo) == "the-great-king-s-rise"
    cjk = slugify_title("測試小說", repo_root=repo)
    assert cjk.startswith("novel-") and cjk == slugify_title("測試小說", repo_root=repo)


def test_analyze_epub_clean_and_samples(tmp_path):
    chapters = [(f"第{n}章 標題{n}", ZH_HANT_TEXT) for n in range(1, 8)]
    analysis = analyze_source(make_epub(tmp_path / "book.epub", chapters))
    assert analysis["error"] is None, analysis
    assert analysis["source_type"] == "epub"
    assert analysis["language"] == "zh-Hant"
    assert analysis["chapter_count"] == 7
    assert analysis["final_chapter"] == 7
    assert analysis["chapter_numbering"] == "global"
    assert analysis["gaps"] == [] and analysis["duplicates"] == []
    assert analysis["title_guess"] == "測試小說"
    assert len(analysis["samples"]) == 5
    assert all(len(s["text"]) <= 1200 for s in analysis["samples"])


def test_analyze_epub_gap_and_duplicate_reported(tmp_path):
    chapters = [(f"第{n}章 標題", ZH_TEXT) for n in (1, 2, 4, 5)]
    analysis = analyze_source(make_epub(tmp_path / "gap.epub", chapters))
    assert analysis["gaps"] == [3]
    assert analysis["chapter_numbering"] == "spine_sequential"
    assert any("irregular" in w for w in analysis["warnings"])
    chapters = [(f"第{n}章 標題", ZH_TEXT) for n in (1, 2, 2, 3)]
    analysis = analyze_source(make_epub(tmp_path / "dup.epub", chapters))
    assert analysis["duplicates"] == [2]
    assert analysis["chapter_numbering"] == "spine_sequential"


def test_analyze_txt_each_encoding_and_gaps(tmp_path):
    path = make_ko_txt(tmp_path / "ko.txt", [1, 2, 4], encoding="cp949")
    analysis = analyze_source(path)
    assert analysis["error"] is None, analysis
    assert analysis["encoding"] == "cp949"
    assert analysis["language"] == "ko"
    assert analysis["chapter_numbering"] == "spine_sequential"
    assert analysis["chapter_count"] == 3 and analysis["final_chapter"] == 3
    assert analysis["gaps"] == [3]
    assert analysis["title_guess"] == "나의 소설"


def test_analyze_bad_inputs(tmp_path):
    assert analyze_source(tmp_path / "missing.txt")["error"]
    other = tmp_path / "book.pdf"
    other.write_bytes(b"x")
    assert "unsupported" in analyze_source(other)["error"]
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"no headings here at all\n")
    assert analyze_source(empty)["error"]


def test_create_from_epub_analysis(repo, tmp_path):
    chapters = [(f"第{n}章 標題{n}", ZH_HANT_TEXT) for n in range(1, 5)]
    analysis = analyze_source(make_epub(tmp_path / "book.epub", chapters))
    novel_id = slugify_title("測試小說", repo_root=repo)
    addendum = (
        "Tone: grim.\n\n[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Keep ranks distinct.\n[/AUXILIARY_TERMINOLOGY_POLICY]"
    )
    project = create_from_analysis(
        analysis, novel_id=novel_id, title="測試小說", profile="historical_military",
        addendum=addendum, repo_root=repo,
    )
    assert project.config.source_language == "zh-Hant"
    assert project.config.final_chapter == 4
    saved = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    assert [c["chapter"] for c in saved] == [1, 2, 3, 4]
    assert "Keep ranks distinct." in project.translation_addendum_file.read_text(
        encoding="utf-8"
    )
    assert validate_project.validate_project(load_novel_project(novel_id, repo)) == 0


def test_create_from_txt_analysis_converts_encoding(repo, tmp_path):
    raw = make_ko_txt(tmp_path / "ko.txt", [1, 2, 3], encoding="cp949")
    before = raw.read_bytes()
    analysis = analyze_source(raw)
    project = create_from_analysis(
        analysis, novel_id="ko-novel", title="나의 소설", profile="korean_fantasy",
        repo_root=repo,
    )
    assert raw.read_bytes() == before
    assert project.config.source_type == "txt"
    assert project.source_txt.read_bytes().decode("utf-8").startswith("나의 소설")
    saved = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    assert len(saved) == 3
    assert validate_project.validate_project(project) == 0


def test_create_refuses_error_analysis_and_bad_addendum(repo, tmp_path):
    bad = analyze_source(tmp_path / "nope.txt")
    with pytest.raises(ValueError):
        create_from_analysis(
            bad, novel_id="x", title="x", profile="korean_fantasy", repo_root=repo
        )
    good = analyze_source(make_ko_txt(tmp_path / "k.txt", [1, 2]))
    with pytest.raises(ValueError):
        create_from_analysis(
            good, novel_id="x", title="x", profile="korean_fantasy",
            addendum="[AUXILIARY_TERMINOLOGY_POLICY]\nnot a list\n"
            "[/AUXILIARY_TERMINOLOGY_POLICY]",
            repo_root=repo,
        )
    assert not (repo / "novels" / "x").exists()


ZH_BODY = "他们没有说话，只是静静地看着远方的群山。" * 8


def make_zh_txt(path, headings, encoding="gb18030", title="测试小说"):
    parts = [title + "\n"]
    for heading in headings:
        parts.append(f"{heading}\n\n{ZH_BODY}\n")
    path.write_bytes("\n".join(parts).encode(encoding))
    return path


def test_chinese_txt_headings_and_kinds(tmp_path):
    headings = [
        "楔子", "第一章 开端", "第２章", "第三回：风起", "第十一节 归来",
        "第一百零二章 终局", "第两章 怪数", "番外 一 旧事", "尾声", "后记",
    ]
    analysis = analyze_source(make_zh_txt(tmp_path / "zh.txt", headings))
    assert analysis["error"] is None, analysis
    assert analysis["language"] == "zh"
    assert analysis["encoding"] == "gb18030"
    assert analysis["chapter_count"] == 10
    assert analysis["chapter_numbering"] == "spine_sequential"
    assert analysis["title_guess"] == "测试小说"


def test_chinese_txt_traditional_language(tmp_path):
    body = ZH_HANT_TEXT
    path = tmp_path / "tw.txt"
    path.write_bytes(
        ("測試\n\n第一章 開端\n\n" + body + "\n\n第二章 後來\n\n" + body + "\n").encode("big5")
    )
    analysis = analyze_source(path)
    assert analysis["error"] is None, analysis
    assert analysis["language"] == "zh-Hant"
    assert analysis["encoding"] == "big5"


def test_chinese_txt_does_not_match_prose(tmp_path):
    from extract_txt import extract_txt_text

    text = (
        "第一章 开始\n\n" + ZH_BODY + "\n第三章的内容让他很吃惊，因为一切都变了。\n"
        "番外的人都走了\n" + "第" + "很长" * 30 + "章\n" + ZH_BODY + "\n"
    )
    result = extract_txt_text(text, "0" * 64)
    assert len(result["chapters"]) == 1


def test_chinese_txt_gaps_reported(tmp_path):
    analysis = analyze_source(
        make_zh_txt(tmp_path / "g.txt", ["第一章", "第二章", "第四章"])
    )
    assert analysis["error"] is None
    assert analysis["gaps"] == [3]


def test_gb18030_txt_end_to_end(repo, tmp_path):
    raw = make_zh_txt(tmp_path / "zh.txt", ["序章", "第一章 开端", "第二章 风起", "尾声"])
    before = raw.read_bytes()
    analysis = analyze_source(raw)
    project = create_from_analysis(
        analysis, novel_id="zh-novel", title="测试小说", profile="xianxia_xuanhuan",
        repo_root=repo,
    )
    assert raw.read_bytes() == before
    assert project.config.source_language == "zh"
    saved = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    assert [c["unit_kind"] for c in saved] == ["prologue", "chapter", "chapter", "epilogue"]
    assert validate_project.validate_project(project) == 0
