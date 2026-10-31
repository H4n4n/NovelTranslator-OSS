import hashlib
import json
from pathlib import Path

import pytest

import extract_txt
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_project(tmp_path, final_chapter=3):
    profile_root = Path(__file__).resolve().parents[1] / "profiles"
    target_profiles = tmp_path / "profiles"
    target_profiles.mkdir()
    source_profile = profile_root / "korean_fantasy"
    (target_profiles / "korean_fantasy").mkdir()
    for path in source_profile.iterdir():
        (target_profiles / "korean_fantasy" / path.name).write_bytes(path.read_bytes())
    config = NovelConfig(
        id="txt_fixture",
        title="TXT Fixture",
        source_language="ko",
        target_language="en",
        profile="korean_fantasy",
        final_chapter=final_chapter,
        chapter_numbering="spine_sequential",
        source_type="txt",
    )
    create_novel_project(config, tmp_path)
    return load_novel_project("txt_fixture", tmp_path)


@pytest.mark.parametrize(
    "raw,bom,newlines",
    [
        ("Prologue\n본문\n".encode(), False, "LF"),
        (b"\xef\xbb\xbf" + "Prologue\r\n본문\r\n".encode(), True, "CRLF"),
    ],
)
def test_utf8_bom_and_newline_inventory(tmp_path, raw, bom, newlines):
    path = tmp_path / "novel.txt"
    path.write_bytes(raw)
    report = extract_txt.inspect_utf8_txt(path)
    assert report["has_utf8_bom"] is bom
    assert report["newline_style"] == newlines
    assert report["source_sha256"] == digest(raw)


def test_generic_heading_forms_preserve_prose_dialogue_blanks_and_scene_breaks():
    text = """Prologue

첫 문단.

"대화."
**
===================
2화.

둘째 본문.
3화. 제목

셋째 본문.
4화.<꺾쇠 제목>

넷째 본문.
5화. < 띄운 제목 >

다섯째 본문.
6화 <공백 꺾쇠>

여섯째 본문.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode()))
    units = result["chapters"]
    assert [item["production_index"] for item in units] == list(range(1, 7))
    assert units[0]["unit_kind"] == "prologue"
    assert units[1]["source_number"] == 2
    assert units[3]["title"] == "꺾쇠 제목"
    assert units[4]["title"] == "띄운 제목"
    assert units[5]["title"] == "공백 꺾쇠"
    assert '"대화."' in units[0]["text"]
    assert "\n\n" in units[0]["text"]
    assert "**" in units[0]["text"]
    assert "===================" in units[0]["text"]
    assert result["report"]["narrative_coverage"] == 1.0


def test_a_closing_line_that_looks_like_a_heading_is_not_a_chapter():
    text = """1화. 시작

본문입니다.

1화. 시작 끝

제 2화

둘째

본문입니다.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode("utf-8")), source_file="novel.txt")
    assert [chapter["source_number"] for chapter in result["chapters"]] == [1, 2]
    assert "끝" not in result["chapters"][0]["text"]
    assert [item["classification"] for item in result["report"]["excluded_sections"]].count("empty_heading") == 1


def test_korean_numbered_headings_accept_hwa_and_hoe_as_distinct_units():
    text = """170화.< title >
body 170.
171회.< title >
body 171.
172화.< title >
body 172.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode()))

    assert [item["source_number"] for item in result["chapters"]] == [170, 171, 172]
    assert [item["heading_format"] for item in result["chapters"]] == [
        "number_dot", "number_dot", "number_dot"
    ]
    assert [item["title"] for item in result["chapters"]] == ["title"] * 3


def test_hwa_heading_forms_remain_supported_and_hoe_in_prose_is_not_a_heading():
    text = """1화. Existing dot heading
This ordinary prose mentions 화 and 회.
171회 공연은 본문이지 장 제목이 아니다.
2화 <Existing angle heading>
Second body.
제 3화
Existing two-line heading
Third body.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode()))

    assert [item["source_number"] for item in result["chapters"]] == [1, 2, 3]
    assert [item["heading_format"] for item in result["chapters"]] == [
        "number_dot", "number_angle", "two_line_je_number"
    ]
    assert "171회 공연은 본문이지 장 제목이 아니다." in result["chapters"][0]["text"]


def test_hoe_uses_the_existing_angle_and_two_line_heading_forms():
    text = """1회 <Angle title>
First body.
제 2회
Two-line title
Second body.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode()))

    assert [item["source_number"] for item in result["chapters"]] == [1, 2]
    assert [item["heading_format"] for item in result["chapters"]] == [
        "number_angle", "two_line_je_number"
    ]


def test_two_line_side_story_epilogue_end_marker_and_afterword_exclusion():
    text = """책 제목 @작가
Prologue

서문 본문.
2화.< 본편 >

본편 본문.
3화.< 에필로그(1) >

에필로그 본문.
제 4화

외전 1편. 아침

외전 본문.
4화. < 외전 1편. 아침 > 끝
<완결>
외전 완결 후기
후기 본문.
"""
    lines = text.splitlines()
    line = lambda value: lines.index(value) + 1
    overrides = {
        "line_classifications": [
            {"line": 1, "expected_text": "책 제목 @작가", "classification": "metadata"},
            {
                "line": line("4화. < 외전 1편. 아침 > 끝"),
                "expected_text": "4화. < 외전 1편. 아침 > 끝",
                "classification": "end_marker",
            },
            {
                "line": line("외전 완결 후기"),
                "expected_text": "외전 완결 후기",
                "classification": "author_afterword_start",
            },
        ],
        "heading_overrides": [],
    }
    result = extract_txt.extract_txt_text(text, digest(text.encode()), overrides)
    units = result["chapters"]
    assert [item["unit_kind"] for item in units] == [
        "prologue", "chapter", "epilogue", "side_story"
    ]
    assert units[-1]["heading_format"] == "two_line_je_number"
    assert units[-1]["heading_line_end"] > units[-1]["heading_line_start"]
    assert "제 4화" not in units[-1]["text"].split("\n\n", 1)[1]
    assert "끝" not in units[-1]["text"]
    assert "후기 본문" not in "\n".join(item["text"] for item in units)
    kinds = [item["classification"] for item in result["report"]["excluded_sections"]]
    assert "author_afterword_start" in kinds
    assert "end_marker" in kinds


def test_duplicate_source_numbers_do_not_duplicate_production_indexes():
    text = """Prologue
본문.
2화. 본편
본문.
제 2화
외전 1편
본문.
"""
    result = extract_txt.extract_txt_text(text, digest(text.encode()))
    assert [item["source_number"] for item in result["chapters"]] == [None, 2, 2]
    assert result["report"]["source_number_duplicates"] == [2]
    assert [item["chapter"] for item in result["chapters"]] == [1, 2, 3]
    assert result["report"]["heading_format_counts"]["two_line_je_number"] == 1


def test_narrative_coverage_guard_blocks_catastrophic_normalizer_loss(monkeypatch):
    text = "Prologue\n" + ("긴 본문입니다.\n" * 20)
    monkeypatch.setattr(extract_txt, "_normalize_body", lambda lines: "한 글자")
    with pytest.raises(extract_txt.TXTExtractionError, match="coverage"):
        extract_txt.extract_txt_text(text, digest(text.encode()))


def test_hash_bound_override_and_stale_hash_rejection(tmp_path):
    project = make_project(tmp_path, final_chapter=1)
    raw = "메타\nPrologue\n본문.\n".encode()
    project.source_txt.write_bytes(raw)
    project.source_overrides_file.write_text(json.dumps({
        "source_sha256": digest(raw),
        "txt": {"line_classifications": [{
            "line": 1,
            "expected_text": "메타",
            "classification": "metadata",
        }]},
    }, ensure_ascii=False), encoding="utf-8")
    result = extract_txt.extract_source_txt(project)
    assert result["chapters"][0]["unit_kind"] == "prologue"
    assert "메타" not in result["chapters"][0]["text"]
    project.source_txt.write_text("변경\nPrologue\n본문.\n", encoding="utf-8")
    with pytest.raises(extract_txt.TXTExtractionError, match="stale"):
        extract_txt.extract_source_txt(project)


def test_source_override_expected_text_mismatch_blocks():
    text = "Prologue\n본문.\n"
    overrides = {"line_classifications": [{
        "line": 1, "expected_text": "DIFFERENT", "classification": "metadata"
    }], "heading_overrides": []}
    with pytest.raises(extract_txt.TXTExtractionError, match="expected text mismatch"):
        extract_txt.extract_txt_text(text, digest(text.encode()), overrides)


def test_atomic_install_and_project_source_type(tmp_path):
    project = make_project(tmp_path, final_chapter=1)
    raw = "Prologue\n본문.\n".encode()
    project.source_txt.write_bytes(raw)
    result = extract_txt.extract_source_txt(project)
    extract_txt.install_extraction(project, result)
    installed = json.loads(project.chapters_file.read_text(encoding="utf-8"))
    assert len(installed) == 1
    assert project.config.source_type == "txt"
    assert project.source_file == project.source_txt
    assert (project.data_dir / "source_inventory.json").is_file()
