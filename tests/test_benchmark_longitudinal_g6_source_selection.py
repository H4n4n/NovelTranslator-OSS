import json

import pytest

import benchmark_longitudinal_g6_source_selection as selection


def _plain(ch, length=300):
    return ("敘事文字" if ch % 2 else "一般內容") * length


def test_profile_and_selection_are_deterministic_for_chinese():
    chapters = {chapter: _plain(chapter, 120 + chapter) for chapter in range(1, 6)}
    chapters[2] = (
        "“第一句對話。”\n“第二句對話。”\n“第三句對話。”\n"
        + "旁白。" * 80
    )
    chapters[4] = (
        "稀有甲乙丙丁戊己庚辛壬癸" * 90
        + "【123】《特殊結構》"
        + "長句沒有停頓" * 100
    )

    first = selection.profile_chapters(chapters, "zh")
    second = selection.profile_chapters(chapters, "zh")

    assert first == second
    chosen = selection.select_stress_cases(first)
    assert chosen["dialogue_stress"].chapter == 2
    assert chosen["information_density_stress"].chapter != 2


def test_profile_and_selection_are_deterministic_for_korean():
    chapters = {
        1: "평범한 서술 문장입니다. " * 100,
        2: '"첫 대사입니다."\n"둘째 대사입니다."\n"셋째 대사입니다."\n' + "서술. " * 40,
        3: ("희귀용어 알파베타 감마델타 " * 160) + "[123] <상태창>",
        4: "짧은 설명입니다. " * 80,
    }

    metrics = selection.profile_chapters(chapters, "ko")
    chosen = selection.select_stress_cases(metrics)

    assert chosen["dialogue_stress"].chapter == 2
    assert chosen["information_density_stress"].chapter != 2


def test_selection_always_uses_distinct_chapters():
    chapters = {
        1: '"대사."\n' * 20 + "희귀정보 " * 300,
        2: "두번째 장면입니다. " * 80,
    }
    metrics = selection.profile_chapters(chapters, "ko")
    chosen = selection.select_stress_cases(metrics)
    assert {
        chosen["dialogue_stress"].chapter,
        chosen["information_density_stress"].chapter,
    } == {1, 2}


def test_metric_identity_contains_hashes_but_not_source_text():
    chapters = {
        1: '"대화입니다."\n설명 문장입니다. ' * 20,
        2: "다른 설명 문장입니다. " * 30,
    }
    metrics = selection.profile_chapters(chapters, "ko")
    rendered = json.dumps([item.identity() for item in metrics], ensure_ascii=False)

    assert "대화입니다" not in rendered
    assert "다른 설명" not in rendered
    for item in metrics:
        assert len(item.text_sha256) == 64


def test_changing_source_text_changes_metric_hash():
    original = {
        1: "첫 문장입니다. " * 20,
        2: "둘째 문장입니다. " * 20,
    }
    changed = dict(original)
    changed[2] += "추가 문장."

    before = selection.profile_chapters(original, "ko")
    after = selection.profile_chapters(changed, "ko")

    assert before[0].text_sha256 == after[0].text_sha256
    assert before[1].text_sha256 != after[1].text_sha256


def test_unsupported_language_fails_closed():
    with pytest.raises(selection.G6SourceSelectionError, match="unsupported"):
        selection.profile_chapters(
            {1: "text one", 2: "text two"},
            "en",
        )


def test_output_refuses_novels_tree(tmp_path):
    repo = tmp_path / "repo"
    novels = repo / "novels"
    novels.mkdir(parents=True)

    with pytest.raises(selection.G6SourceSelectionError, match="must not be written"):
        selection.write_selection_bundle(
            novels / "evidence.json",
            {"safe": True},
            repo_root=repo,
        )


def test_output_writes_only_explicit_external_path(tmp_path):
    repo = tmp_path / "repo"
    (repo / "novels").mkdir(parents=True)
    output = tmp_path / "evidence" / "selection.json"

    selection.write_selection_bundle(
        output,
        {"schema": 1},
        repo_root=repo,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == {"schema": 1}
    assert not any((repo / "novels").iterdir())


def test_percentile_ties_are_deterministic():
    values = [1.0, 1.0, 1.0, 1.0]
    assert selection._percentile_rank(values, 1.0) == 0.5


def test_top_candidates_reject_unknown_axis():
    metrics = selection.profile_chapters(
        {
            1: "평범한 문장입니다. " * 20,
            2: "다른 문장입니다. " * 20,
        },
        "ko",
    )
    with pytest.raises(selection.G6SourceSelectionError, match="unknown stress axis"):
        selection.top_candidates(metrics, stress="not-real")
