import hashlib
import json
from pathlib import Path
import shutil
import socket

import pytest

import create_novel
from create_novel import NovelCreationError, create_novel_project
from list_novels import collect_novels, show_novels
from novel_project import DEFAULT_AUDIT_INTERVAL, NovelConfig, load_novel_project
from noveltranslator_core import PRICING_VERSION
from validate_project import validate_project


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
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")
    return tmp_path


def config(novel_id, profile="historical_military", **overrides):
    values = {
        "id": novel_id,
        "title": overrides.pop("title", novel_id.replace("_", " ").title()),
        "source_language": overrides.pop("source_language", "zh"),
        "target_language": overrides.pop("target_language", "en"),
        "profile": profile,
        "final_chapter": overrides.pop("final_chapter", 100),
        "audit_interval": overrides.pop("audit_interval", DEFAULT_AUDIT_INTERVAL),
    }
    values.update(overrides)
    return NovelConfig.from_dict(values)


def snapshot_tree(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_create_command_builds_complete_safe_project_with_optional_fields(
    tmp_path, monkeypatch,
):
    prepare_repo(tmp_path)
    monkeypatch.setattr(create_novel, "repository_root", lambda: tmp_path)
    result = create_novel.main([
        "--id", "immortal_path",
        "--title", "Immortal Path",
        "--profile", "xianxia_xuanhuan",
        "--final-chapter", "1800",
        "--author", "Example Author",
        "--audit-interval", "30",
    ])
    assert result == 0
    project = load_novel_project("immortal_path", tmp_path)
    assert project.config.author == "Example Author"
    assert project.config.audit_interval == 30
    assert project.translation_addendum_file.read_bytes() == b""
    for directory in create_novel.PROJECT_DIRECTORIES:
        assert (project.root / directory).is_dir()
    novel_json = json.loads(project.config_file.read_text(encoding="utf-8"))
    assert novel_json["audit_interval"] == 30
    assert novel_json["chapter_numbering"] == "global"
    assert "api_key" not in novel_json
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress == create_novel.initial_project_state()["progress.json"]
    assert progress["pricing_version"] == PRICING_VERSION


def test_create_command_supports_native_txt_source_copy(tmp_path, monkeypatch):
    prepare_repo(tmp_path)
    monkeypatch.setattr(create_novel, "repository_root", lambda: tmp_path)
    source = tmp_path / "incoming.txt"
    source.write_bytes(b"\xef\xbb\xbfPrologue\r\nbody\r\n")
    result = create_novel.main([
        "--id", "txt_novel",
        "--title", "TXT Novel",
        "--profile", "historical_military",
        "--final-chapter", "1",
        "--source-language", "ko",
        "--chapter-numbering", "spine_sequential",
        "--source-type", "txt",
        "--source", str(source),
    ])
    assert result == 0
    project = load_novel_project("txt_novel", tmp_path)
    assert project.config.source_type == "txt"
    assert project.source_txt.read_bytes() == source.read_bytes()


def test_legacy_config_without_source_type_defaults_to_epub():
    assert config("legacy_source").source_type == "epub"


def test_creating_novel_a_cannot_modify_novel_b(tmp_path):
    prepare_repo(tmp_path)
    novel_b = create_novel_project(config("novel_b"), tmp_path)
    before = snapshot_tree(novel_b)
    create_novel_project(config("novel_a", "xianxia_xuanhuan"), tmp_path)
    assert snapshot_tree(novel_b) == before


def test_same_profile_projects_have_independent_mutable_state(tmp_path):
    prepare_repo(tmp_path)
    create_novel_project(config("novel_a", "xianxia_xuanhuan"), tmp_path)
    create_novel_project(config("novel_b", "xianxia_xuanhuan"), tmp_path)
    first = load_novel_project("novel_a", tmp_path)
    second = load_novel_project("novel_b", tmp_path)
    first.progress_file.write_text('{"changed":"only A"}', encoding="utf-8")
    first.glossary_file.write_text('{"entries":{"A":{}}}', encoding="utf-8")
    assert json.loads(second.progress_file.read_text(encoding="utf-8"))[
        "last_completed"
    ] == 0
    assert json.loads(second.glossary_file.read_text(encoding="utf-8")) == {
        "entries": {}
    }


def test_projects_with_different_profiles_are_created_independently(tmp_path):
    prepare_repo(tmp_path)
    create_novel_project(config("history", "historical_military"), tmp_path)
    create_novel_project(config("cultivation", "xianxia_xuanhuan"), tmp_path)
    assert load_novel_project("history", tmp_path).config.profile == "historical_military"
    assert load_novel_project("cultivation", tmp_path).config.profile == "xianxia_xuanhuan"


def test_duplicate_id_fails_without_overwriting_existing_project(tmp_path):
    prepare_repo(tmp_path)
    target = create_novel_project(config("duplicate"), tmp_path)
    before = snapshot_tree(target)
    with pytest.raises(NovelCreationError, match="already exists"):
        create_novel_project(config("duplicate", title="Different Title"), tmp_path)
    assert snapshot_tree(target) == before


def test_missing_profile_fails_before_creating_novels_directory(tmp_path):
    prepare_repo(tmp_path)
    with pytest.raises(FileNotFoundError, match="translation profile does not exist"):
        create_novel_project(config("broken", "missing_profile"), tmp_path)
    assert not (tmp_path / "novels").exists()


def test_invalid_profile_id_fails_before_creating_novels_directory(tmp_path):
    prepare_repo(tmp_path)
    with pytest.raises(ValueError, match="profile identifier"):
        create_novel_project(config("broken", "../xianxia_xuanhuan"), tmp_path)
    assert not (tmp_path / "novels").exists()


@pytest.mark.parametrize("novel_id", ["../escape", "bad/name", "bad\\name", "Uppercase"])
def test_path_traversal_and_non_slug_ids_fail(novel_id):
    with pytest.raises(ValueError, match="novel identifier"):
        config(novel_id)


@pytest.mark.parametrize("field,value", [("final_chapter", 0), ("audit_interval", 0)])
def test_non_positive_numeric_configuration_fails(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        config("invalid_numbers", **{field: value})


def test_invalid_chapter_numbering_mode_fails_safely():
    with pytest.raises(ValueError, match="chapter_numbering"):
        config("invalid_numbering", chapter_numbering="guess_from_filenames")


def test_partial_creation_is_cleaned_without_valid_half_project(tmp_path, monkeypatch):
    prepare_repo(tmp_path)
    original_write = create_novel._write_json
    calls = []

    def fail_during_write(path, value):
        calls.append(Path(path).name)
        if len(calls) == 3:
            raise OSError("simulated disk failure")
        return original_write(path, value)

    monkeypatch.setattr(create_novel, "_write_json", fail_during_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        create_novel_project(config("interrupted"), tmp_path)
    novels_root = tmp_path / "novels"
    assert not (novels_root / "interrupted").exists()
    assert not list(novels_root.glob(".interrupted.creating-*"))


def test_empty_created_state_passes_validator_semantics(tmp_path, capsys):
    prepare_repo(tmp_path)
    create_novel_project(config("empty_novel", "xianxia_xuanhuan"), tmp_path)
    project = load_novel_project("empty_novel", tmp_path)
    assert validate_project(project) == 0
    output = capsys.readouterr().out
    assert "Current completed chapter: 0" in output
    assert "Next chapter: 1" in output
    assert "SAFE TO RESUME: YES" in output


def test_validator_reports_explicit_strict_terms(tmp_path, capsys):
    prepare_repo(tmp_path)
    create_novel_project(config("strict_novel", "infinite_flow"), tmp_path)
    project = load_novel_project("strict_novel", tmp_path)
    project.glossary_file.write_text(json.dumps({
        "entries": {
            "鳴鶴拳": {
                "english": "Minghequan (Calling Crane Boxing)",
                "category": "technique",
                "status": "locked",
                "strict_qa": True,
            }
        }
    }, ensure_ascii=False), encoding="utf-8")

    assert validate_project(project) == 0
    output = capsys.readouterr().out
    assert "Explicit strict terminology entries: 1" in output
    assert "Locked canonical entries: 1" in output
    assert "Legacy hard terminology entries: 0" in output
    assert "Legacy and explicit hard entries (overlap): 0" in output
    assert "Entries with accepted inflections: 0" in output
    assert "Explicit automatic-repair opt-outs: 0" in output
    assert "Effective strict terminology entries: 1" in output
    assert "SAFE TO RESUME: YES" in output


def test_validator_rejects_invalid_explicit_strict_status(tmp_path, capsys):
    prepare_repo(tmp_path)
    create_novel_project(config("invalid_strict", "infinite_flow"), tmp_path)
    project = load_novel_project("invalid_strict", tmp_path)
    project.glossary_file.write_text(json.dumps({
        "entries": {
            "鳴鶴拳": {
                "english": "Minghequan (Calling Crane Boxing)",
                "category": "technique",
                "status": "provisional",
                "strict_qa": True,
            }
        }
    }, ensure_ascii=False), encoding="utf-8")

    assert validate_project(project) == 1
    output = capsys.readouterr().out
    assert "novel invalid_strict, source term '鳴鶴拳'" in output
    assert "strict_qa=true requires status=locked" in output
    assert "SAFE TO RESUME: NO" in output


def test_list_novels_reports_independent_progress(tmp_path, capsys):
    prepare_repo(tmp_path)
    create_novel_project(config("novel_a", final_chapter=10), tmp_path)
    create_novel_project(config("novel_b", "xianxia_xuanhuan", final_chapter=20), tmp_path)
    first = load_novel_project("novel_a", tmp_path)
    progress = create_novel.initial_project_state()["progress.json"]
    progress.update({"completed": [1, 2], "last_completed": 2, "next_chapter": 3})
    first.progress_file.write_text(json.dumps(progress), encoding="utf-8")
    for chapter in (1, 2):
        (first.chapter_dir / f"{chapter:04d}.md").write_text(
            f"Chapter {chapter}", encoding="utf-8"
        )

    entries = {entry["id"]: entry for entry in collect_novels(tmp_path)}
    assert entries["novel_a"]["completed"] == 2
    assert entries["novel_a"]["percent"] == 20.0
    assert entries["novel_b"]["completed"] == 0
    assert entries["novel_b"]["percent"] == 0.0
    assert show_novels(tmp_path) == 0
    output = capsys.readouterr().out
    assert "novel_a" in output and "progress: 20.0%" in output
    assert "novel_b" in output and "progress: 0.0%" in output


def test_malformed_novel_does_not_stop_listing_valid_projects(tmp_path, capsys):
    prepare_repo(tmp_path)
    create_novel_project(config("valid_novel"), tmp_path)
    malformed = tmp_path / "novels" / "malformed"
    malformed.mkdir()
    (malformed / "novel.json").write_text("{not json", encoding="utf-8")
    assert show_novels(tmp_path) == 1
    captured = capsys.readouterr()
    assert "valid_novel" in captured.out
    assert "malformed" in captured.out
    assert "status: INVALID" in captured.out
    assert "Invalid projects: 1" in captured.err


def test_creation_and_listing_require_no_credentials_or_network(
    tmp_path, monkeypatch,
):
    prepare_repo(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("novel management attempted network access"),
    )
    create_novel_project(config("offline_novel"), tmp_path)
    assert collect_novels(tmp_path)[0]["id"] == "offline_novel"


def test_novel_creation_tests_do_not_modify_current_ming_checkpoint():
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
