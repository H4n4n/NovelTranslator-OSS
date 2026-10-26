import hashlib
import importlib
import json
from pathlib import Path
import socket

import pytest

import translate
import translation_profile as profile_module
from novel_project import NovelConfig, load_novel_project
from noveltranslator_core import (
    ALLOWED_GLOSSARY_CATEGORIES,
    format_technique_name,
    obtain_translation,
    prefilter_candidates,
)
from translation_profile import (
    effective_translation_instructions,
    filter_profile_candidates,
    load_novel_terminology_policy,
    load_translation_profile,
    profile_audit_instructions,
    profile_glossary_review_instructions,
    profile_state_instructions,
    project_low_value_terms,
    select_profile_category,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_ming_low_value_terms_remain_project_scoped():
    assert "紅票" in project_low_value_terms("ming_novel")
    assert project_low_value_terms("guhuo_bird") == frozenset()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def production_state_hashes(novel_root):
    novel_root = Path(novel_root)
    scopes = (
        "data", "output/chapters", "output/pending", "output/qa",
        "output/audits", "output/retroactive_fixes", "backup",
    )
    files = []
    for relative in scopes:
        root = novel_root / relative
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return {
        path.relative_to(novel_root).as_posix(): sha256(path)
        for path in sorted(files)
    }


def novel_config(novel_id, profile_id):
    return NovelConfig(
        id=novel_id,
        title=novel_id,
        source_language="zh",
        target_language="en",
        profile=profile_id,
        final_chapter=10,
    )


def write_novel(repo_root, config):
    root = Path(repo_root) / "novels" / config.id
    root.mkdir(parents=True)
    (root / "novel.json").write_text(
        json.dumps(config.to_dict()), encoding="utf-8"
    )
    return load_novel_project(config.id, repo_root)


def test_historical_and_xianxia_profiles_load_independently():
    historical = load_translation_profile("historical_military", REPO_ROOT)
    xianxia = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    assert historical.id == "historical_military"
    assert xianxia.id == "xianxia_xuanhuan"
    assert historical is not xianxia
    assert historical.translation_instructions != xianxia.translation_instructions


def test_infinite_flow_loads_as_an_independent_valid_profile():
    infinite_flow = load_translation_profile("infinite_flow", REPO_ROOT)
    historical = load_translation_profile("historical_military", REPO_ROOT)
    xianxia = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)

    assert infinite_flow.id == "infinite_flow"
    assert infinite_flow.display_name == "Infinite Flow"
    assert infinite_flow not in (historical, xianxia)
    assert infinite_flow.translation_instructions not in {
        historical.translation_instructions,
        xianxia.translation_instructions,
    }
    assert "Terror Infinity" not in infinite_flow.translation_instructions


def test_korean_fantasy_profile_supports_core_reusable_categories():
    profile = load_translation_profile("korean_fantasy", REPO_ROOT)
    assert profile.id == "korean_fantasy"
    assert {
        "character", "title", "organization", "military unit", "location",
        "species", "artifact", "weapon", "magic", "spell", "skill",
        "technique", "progression rank", "system term",
        "specialized recurring term",
    }.issubset(profile.allowed_categories)
    assert "Korean fantasy" in profile.translation_instructions


@pytest.mark.parametrize(
    "category",
    [
        "character", "alias", "organization", "team", "title", "location",
        "world", "scenario", "system term", "mission", "mission type",
        "objective", "reward", "currency", "attribute", "stat", "skill",
        "ability", "technique", "cultivation technique", "cultivation realm",
        "bloodline", "enhancement", "class", "profession", "item",
        "equipment", "weapon", "artifact", "consumable", "material",
        "creature", "species", "monster", "status effect", "rule",
        "mechanic", "rank", "difficulty", "specialized recurring term",
    ],
)
def test_infinite_flow_accepts_required_glossary_categories(category):
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    eligible, rejected = filter_profile_candidates(profile, [{
        "chinese": "正式术语",
        "english": "Formal Named Term",
        "category": category,
    }])
    assert [item["category"] for item in eligible] == [category]
    assert rejected == []


def test_infinite_flow_filter_rejects_generic_vocabulary_conservatively():
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    candidates = [
        {"chinese": "能力", "english": "ordinary ability", "category": "ability"},
        {"chinese": "技能", "english": "generic skill", "category": "skill"},
        {"chinese": "任务", "english": "ordinary task", "category": "mission"},
        {"chinese": "世界", "english": "ordinary world", "category": "world"},
        {"chinese": "武器", "english": "ordinary weapon", "category": "weapon"},
        {"chinese": "怪物", "english": "generic monster", "category": "monster"},
        {"chinese": "房间", "english": "ordinary room", "category": "location"},
        {"chinese": "衬衫", "english": "ordinary clothing", "category": "item"},
        {"chinese": "恐惧", "english": "ordinary fear", "category": "status effect"},
        {
            "chinese": "挥拳",
            "english": "generic combat action",
            "category": "specialized recurring term",
        },
    ]
    eligible, rejected = filter_profile_candidates(profile, candidates)
    assert eligible == []
    assert {item["chinese"] for item in rejected} == {
        item["chinese"] for item in candidates
    }
    assert {item["reason"] for item in rejected} == {"generic or mundane term"}


def test_infinite_flow_filter_keeps_formal_mechanical_candidates():
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    candidates = [
        {"chinese": "主神提示", "english": "System Notice", "category": "system term"},
        {"chinese": "支线任务", "english": "Side Mission", "category": "mission type"},
        {"chinese": "生存点", "english": "Survival Points", "category": "currency"},
        {"chinese": "龙族血统", "english": "Dragon Bloodline", "category": "bloodline"},
        {
            "chinese": "念动力强化",
            "english": "Psychic Force Enhancement",
            "category": "enhancement",
        },
        {"chinese": "灵能护甲", "english": "Psionic Armor", "category": "equipment"},
        {"chinese": "生化危机场景", "english": "Resident Evil Scenario", "category": "scenario"},
    ]
    eligible, rejected = filter_profile_candidates(profile, candidates)
    assert [item["chinese"] for item in eligible] == [
        item["chinese"] for item in candidates
    ]
    assert rejected == []


def test_infinite_flow_capitalization_is_not_applied_to_every_system_phrase():
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    assert profile.requires_formal_capitalization("technique")
    assert profile.requires_formal_capitalization("cultivation technique")
    assert not profile.requires_formal_capitalization("skill")
    assert not profile.requires_formal_capitalization("ability")
    assert not profile.requires_formal_capitalization("system term")


def test_infinite_flow_auxiliary_and_audit_policy_cover_cross_world_mechanics():
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    policy = load_novel_terminology_policy(profile)
    state_prompt = profile_state_instructions(profile, policy)
    review_prompt = profile_glossary_review_instructions(profile, policy)
    audit_prompt = profile_audit_instructions(profile, policy)

    assert "ordinary vocabulary from formal system labels" in state_prompt
    assert "overarching system terminology" in review_prompt
    assert "Accidental terminology contamination" in audit_prompt
    assert "Residual Chinese or CJK" in audit_prompt
    assert len(profile.periodic_audit_priorities) == 15


def test_infinite_flow_novel_specific_policy_is_isolated(tmp_path):
    profile = load_translation_profile("infinite_flow", REPO_ROOT)
    first = write_novel(tmp_path, novel_config("flow_a", "infinite_flow"))
    second = write_novel(tmp_path, novel_config("flow_b", "infinite_flow"))
    first.translation_addendum_file.write_text(
        "[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Novel A calls its hub the Silver Junction.\n"
        "[/AUXILIARY_TERMINOLOGY_POLICY]\n",
        encoding="utf-8",
    )
    second.translation_addendum_file.write_text(
        "[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Novel B calls its hub the Black Atrium.\n"
        "[/AUXILIARY_TERMINOLOGY_POLICY]\n",
        encoding="utf-8",
    )

    first_prompt = profile_state_instructions(
        profile,
        load_novel_terminology_policy(profile, first.translation_addendum_file),
    )
    second_prompt = profile_state_instructions(
        profile,
        load_novel_terminology_policy(profile, second.translation_addendum_file),
    )
    assert "Silver Junction" in first_prompt and "Black Atrium" not in first_prompt
    assert "Black Atrium" in second_prompt and "Silver Junction" not in second_prompt
    assert first.glossary_file != second.glossary_file
    assert first.progress_file != second.progress_file


def test_two_novels_can_share_profile_without_sharing_mutable_state(tmp_path):
    first = write_novel(tmp_path, novel_config("novel_a", "xianxia_xuanhuan"))
    second = write_novel(tmp_path, novel_config("novel_b", "xianxia_xuanhuan"))
    first_profile = load_translation_profile(first.config.profile, REPO_ROOT)
    second_profile = load_translation_profile(second.config.profile, REPO_ROOT)
    assert first_profile == second_profile
    assert first.progress_file != second.progress_file
    assert first.glossary_file != second.glossary_file
    first.data_dir.mkdir()
    second.data_dir.mkdir()
    first.progress_file.write_text('{"last_completed": 7}', encoding="utf-8")
    second.progress_file.write_text('{"last_completed": 1}', encoding="utf-8")
    assert second.progress_file.read_text(encoding="utf-8") == '{"last_completed": 1}'


def test_two_novels_can_select_different_profiles(tmp_path):
    historical_novel = write_novel(
        tmp_path, novel_config("history", "historical_military")
    )
    xianxia_novel = write_novel(
        tmp_path, novel_config("cultivation", "xianxia_xuanhuan")
    )
    historical = load_translation_profile(historical_novel.config.profile, REPO_ROOT)
    xianxia = load_translation_profile(xianxia_novel.config.profile, REPO_ROOT)
    assert historical.id != xianxia.id


def test_profile_category_allowlists_differ_appropriately():
    historical = load_translation_profile("historical_military", REPO_ROOT)
    xianxia = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    assert "historical office" in historical.allowed_categories
    assert "historical office" in xianxia.allowed_categories
    assert "historical person" in historical.allowed_categories
    assert "historical person" not in xianxia.allowed_categories
    assert "cultivation realm" in xianxia.allowed_categories
    assert "cultivation realm" not in historical.allowed_categories


@pytest.mark.parametrize(
    "category",
    [
        "state", "dynasty", "noble title", "government office",
        "historical office", "military rank", "military unit",
        "political institution", "divine office", "mythological figure",
        "mythological species",
    ],
)
def test_xianxia_accepts_recurring_political_military_and_divine_categories(category):
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    eligible, rejected = filter_profile_candidates(profile, [{
        "chinese": "專名",
        "english": "Stable Named Term",
        "category": category,
    }])
    assert [item["category"] for item in eligible] == [category]
    assert rejected == []


def test_xianxia_new_categories_still_reject_mundane_vocabulary():
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    eligible, rejected = filter_profile_candidates(profile, [
        {
            "chinese": "普通部隊",
            "english": "ordinary troops",
            "category": "military unit",
        },
        {
            "chinese": "日常行政",
            "english": "daily administration",
            "category": "political institution",
        },
        {
            "chinese": "政府大樓",
            "english": "government building",
            "category": "government office",
        },
    ])
    assert eligible == []
    assert {item["chinese"] for item in rejected} == {
        "普通部隊", "日常行政", "政府大樓"
    }


def test_xianxia_cultivation_realms_are_high_priority():
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    assert "cultivation realm" in profile.high_priority_categories
    assert "cultivation stage" in profile.high_priority_categories
    assert profile.high_priority_categories.index("cultivation realm") < 2


@pytest.mark.parametrize(
    "category",
    [
        "cultivation technique", "martial technique", "movement technique",
        "sword technique", "secret art", "divine ability",
        "forbidden technique", "formation",
    ],
)
def test_xianxia_formal_technique_categories_are_recognized(category):
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    assert profile.requires_formal_capitalization(category)


def test_profile_uses_existing_formal_capitalization_convention():
    assert format_technique_name("black tiger seizes the heart") == (
        "Black Tiger Seizes The Heart"
    )
    assert format_technique_name("nine revolutions golden body") == (
        "Nine Revolutions Golden Body"
    )


def test_xianxia_filter_rejects_mundane_candidates_but_keeps_realm():
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    eligible, rejected = filter_profile_candidates(profile, [
        {
            "chinese": "筑基境",
            "english": "Foundation Establishment",
            "category": "cultivation realm",
        },
        {"chinese": "木桌", "english": "wooden table", "category": "artifact"},
        {"chinese": "凡剑", "english": "ordinary sword", "category": "weapon"},
        {"chinese": "伤药", "english": "ordinary medicine", "category": "medicine"},
    ])
    assert [item["chinese"] for item in eligible] == ["筑基境"]
    assert {item["chinese"] for item in rejected} == {"木桌", "凡剑", "伤药"}


def test_historical_profile_is_compatible_with_ming_prompt_and_filtering():
    profile = load_translation_profile("historical_military", REPO_ROOT)
    ming = load_novel_project("ming_novel", REPO_ROOT)
    current_prompt = (REPO_ROOT / "production_translation.txt").read_text(
        encoding="utf-8"
    )
    assert ming.config.profile == "historical_military"
    assert profile.translation_instructions == current_prompt
    assert effective_translation_instructions(
        profile, ming.translation_addendum_file
    ) == current_prompt
    assert profile.allowed_categories == frozenset(ALLOWED_GLOSSARY_CATEGORIES)
    candidates = [
        {"chinese": "张三", "english": "Zhang San", "category": "character"},
        {"chinese": "木桌", "english": "wooden table", "category": "artifact"},
    ]
    assert filter_profile_candidates(profile, candidates) == prefilter_candidates(candidates)


def test_profile_aware_audit_instructions_differ():
    historical = profile_audit_instructions(
        load_translation_profile("historical_military", REPO_ROOT)
    )
    xianxia = profile_audit_instructions(
        load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    )
    assert historical != xianxia
    assert "Historical offices" in historical
    assert "Cultivation realm" in xianxia
    assert "synonym drift" in xianxia


def test_xianxia_policy_separates_titles_divine_classes_and_cultivation_grades():
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    policy = load_novel_terminology_policy(profile)
    compact = profile_audit_instructions(profile, policy)
    assert "Noble titles and cultivation grades are separate" in compact
    assert "Divine offices or divine classes" in compact
    assert "cultivation realm, stage, subdivision" in compact


def test_compact_novel_policy_reaches_all_auxiliary_prompts(tmp_path):
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    addendum = tmp_path / "translation_addendum.txt"
    addendum.write_text(
        "Full translation style prose FULL_ADDENDUM_SENTINEL.\n\n"
        "[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Keep Iron Duke as a noble title, never a cultivation grade.\n"
        "- Keep Star Marshal as a divine office, never a personal name.\n"
        "[/AUXILIARY_TERMINOLOGY_POLICY]\n\n"
        "More translation-only prose TRANSLATION_ONLY_SENTINEL.\n",
        encoding="utf-8",
    )
    policy = load_novel_terminology_policy(profile, addendum)
    prompts = (
        profile_state_instructions(profile, policy),
        profile_glossary_review_instructions(profile, policy),
        profile_audit_instructions(profile, policy),
    )
    for prompt in prompts:
        assert "Iron Duke" in prompt
        assert "Star Marshal" in prompt
        assert "FULL_ADDENDUM_SENTINEL" not in prompt
        assert "TRANSLATION_ONLY_SENTINEL" not in prompt
    main_prompt = effective_translation_instructions(profile, addendum)
    assert "FULL_ADDENDUM_SENTINEL" in main_prompt
    assert "TRANSLATION_ONLY_SENTINEL" in main_prompt


def test_compact_novel_policy_is_isolated_between_novels(tmp_path):
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    first = tmp_path / "novel_a.txt"
    second = tmp_path / "novel_b.txt"
    first.write_text(
        "[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Novel A uses Azure Court for its political institution.\n"
        "[/AUXILIARY_TERMINOLOGY_POLICY]\n",
        encoding="utf-8",
    )
    second.write_text(
        "[AUXILIARY_TERMINOLOGY_POLICY]\n"
        "- Novel B uses Golden Court for its political institution.\n"
        "[/AUXILIARY_TERMINOLOGY_POLICY]\n",
        encoding="utf-8",
    )
    first_prompt = profile_state_instructions(
        profile, load_novel_terminology_policy(profile, first)
    )
    second_prompt = profile_state_instructions(
        profile, load_novel_terminology_policy(profile, second)
    )
    assert "Azure Court" in first_prompt and "Golden Court" not in first_prompt
    assert "Golden Court" in second_prompt and "Azure Court" not in second_prompt


def test_auxiliary_policy_rejects_a_different_selected_profile():
    xianxia = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    historical = load_translation_profile("historical_military", REPO_ROOT)
    xianxia_policy = load_novel_terminology_policy(xianxia)
    with pytest.raises(ValueError, match="does not match"):
        profile_state_instructions(historical, xianxia_policy)


def test_production_auxiliary_prompts_use_selected_novel_policy():
    source = (
        REPO_ROOT
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    # State extraction, glossary review, periodic audit, bounded residual-CJK
    # repair, and bounded strict-terminology repair are the five intentional
    # auxiliary consumers.
    assert source.count("ACTIVE_PROFILE, ACTIVE_NOVEL_POLICY") == 5
    assert "ACTIVE_NOVEL_POLICY = load_novel_terminology_policy(" in source
    assert "compact_auxiliary_terminology_policy(" in source


def test_historical_auxiliary_policy_regression_is_empty_without_addendum():
    profile = load_translation_profile("historical_military", REPO_ROOT)
    policy = load_novel_terminology_policy(profile)
    assert policy.profile_distinctions == ()
    assert policy.novel_rules == ()
    assert profile_state_instructions(profile, policy) == profile_state_instructions(profile)


def test_novel_specific_addendum_affects_only_that_novel(tmp_path):
    first = write_novel(tmp_path, novel_config("novel_a", "xianxia_xuanhuan"))
    second = write_novel(tmp_path, novel_config("novel_b", "xianxia_xuanhuan"))
    first.translation_addendum_file.write_text(
        "Render one novel-specific epithet as Moon Fox.", encoding="utf-8"
    )
    profile = load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    first_effective = effective_translation_instructions(
        profile, first.translation_addendum_file
    )
    second_effective = effective_translation_instructions(
        profile, second.translation_addendum_file
    )
    assert "Moon Fox" in first_effective
    assert "Moon Fox" not in second_effective
    assert second_effective == profile.translation_instructions


def test_missing_profile_fails_before_production_subprocess(tmp_path, monkeypatch):
    project = write_novel(tmp_path, novel_config("broken", "missing_profile"))
    project.data_dir.mkdir()
    project.progress_file.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(translate.subprocess, "run", lambda *args, **kwargs: calls.append(1))
    with pytest.raises(FileNotFoundError, match="translation profile does not exist"):
        translate.main(["--novel", "broken", "--repo-root", str(tmp_path),
                        "--provider", "direct_deepseek", "--model", "pro"])
    assert calls == []


def test_invalid_profile_id_fails_before_path_or_api_activity():
    with pytest.raises(ValueError, match="profile identifier"):
        load_translation_profile("../historical_military", REPO_ROOT)


def test_reviewer_cannot_introduce_category_outside_selected_profile():
    profile = load_translation_profile("historical_military", REPO_ROOT)
    assert select_profile_category(
        profile, "cultivation realm", "historical office"
    ) == "historical office"
    assert select_profile_category(
        profile, "military rank", "historical office"
    ) == "military rank"


def test_profile_config_cannot_redirect_mutable_novel_paths(tmp_path):
    profile_root = tmp_path / "profiles" / "unsafe"
    profile_root.mkdir(parents=True)
    source = json.loads(
        (REPO_ROOT / "profiles/historical_military/profile.json").read_text(
            encoding="utf-8"
        )
    )
    source["id"] = "unsafe"
    source["progress_file"] = "../../novels/other/data/progress.json"
    (profile_root / "profile.json").write_text(json.dumps(source), encoding="utf-8")
    (profile_root / "translation_instructions.txt").write_text(
        "Valid instructions", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unsupported fields: progress_file"):
        load_translation_profile("unsafe", tmp_path)

    profile = load_translation_profile("historical_military", REPO_ROOT)
    forbidden = {
        "progress_file", "glossary_file", "pending_dir", "chapter_dir",
        "audit_history_file", "backup_dir", "logs_dir", "epub_dir",
    }
    assert forbidden.isdisjoint(profile.__dataclass_fields__)


def test_loading_profiles_is_import_safe_and_does_not_mutate_ming(monkeypatch):
    ming_root = REPO_ROOT / "novels" / "ming_novel"
    great_root = REPO_ROOT / "novels" / "great_king_zhuxia"
    before_ming = production_state_hashes(ming_root)
    before_great = production_state_hashes(great_root)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("profile loading attempted network access"),
    )
    reloaded = importlib.reload(profile_module)
    reloaded.load_translation_profile("historical_military", REPO_ROOT)
    xianxia = reloaded.load_translation_profile("xianxia_xuanhuan", REPO_ROOT)
    infinite_flow = reloaded.load_translation_profile("infinite_flow", REPO_ROOT)
    reloaded.load_novel_terminology_policy(
        xianxia,
        REPO_ROOT / "novels/great_king_zhuxia/translation_addendum.txt",
    )
    assert infinite_flow.id == "infinite_flow"
    assert "OpenAI" not in reloaded.__dict__
    assert production_state_hashes(ming_root) == before_ming
    assert production_state_hashes(great_root) == before_great


def test_current_ming_pending_translation_remains_recoverable():
    project = load_novel_project("ming_novel", REPO_ROOT)

    pending_md = project.pending_dir / "0205.md"
    pending_json = project.pending_dir / "0205.json"

    if not pending_md.is_file() or not pending_json.is_file():
        pytest.skip("real Ming paid pending translation is not present")

    calls = []

    def must_not_generate():
        calls.append(1)
        raise AssertionError("paid pending translation must not be regenerated")

    selected = obtain_translation(
        205,
        project.pending_dir,
        must_not_generate,
    )

    assert selected["source"] == "pending"
    assert selected["meta"]["stage"] == "translated"
    assert calls == []
