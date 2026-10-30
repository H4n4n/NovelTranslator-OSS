"""NOVELTRANSLATOR_NOVELS_ROOT moves novel data; unset keeps <repo>/novels."""
import json

from create_novel import create_novel_project
from list_novels import collect_novels
from novel_project import NOVELS_ROOT_ENV, NovelConfig, load_novel_project, novels_root_for, repository_root


def _config(novel_id="env_root_novel"):
    return NovelConfig.from_dict({
        "id": novel_id, "title": "T", "source_language": "zh", "target_language": "en",
        "profile": "xianxia_xuanhuan", "final_chapter": 3,
    })


def test_unset_uses_repo_novels(monkeypatch, tmp_path):
    monkeypatch.delenv(NOVELS_ROOT_ENV, raising=False)
    assert novels_root_for(tmp_path) == (tmp_path / "novels").resolve()


def test_setting_moves_create_load_and_list(monkeypatch, tmp_path):
    data = tmp_path / "library" / "novels"
    data.mkdir(parents=True)
    monkeypatch.setenv(NOVELS_ROOT_ENV, str(data))
    repo = repository_root()

    created = create_novel_project(_config(), repo_root=repo)

    assert created == (data / "env_root_novel").resolve()
    project = load_novel_project("env_root_novel", repo_root=repo)
    assert project.root == created
    assert project.repository_root == repo
    assert [entry["id"] for entry in collect_novels(repo)] == ["env_root_novel"]
    assert json.loads(project.progress_file.read_text(encoding="utf-8"))["next_chapter"] == 1


def test_traversal_still_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv(NOVELS_ROOT_ENV, str(tmp_path))
    try:
        load_novel_project("../escape", repo_root=repository_root())
    except ValueError:
        return
    raise AssertionError("traversal was accepted")
