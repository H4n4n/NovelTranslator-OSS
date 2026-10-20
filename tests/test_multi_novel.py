from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from build_checkpoint_epub import checkpoint_output_path
from migrate_single_novel import MigrationError, migrate_single_novel
from novel_project import NovelConfig, load_novel_project
from noveltranslator_core import obtain_translation, pricing_snapshot
from noveltranslator_observability import SafeRunLogger
from translation_model_config import TRANSLATION_MODEL_ENV
import translate


def config(novel_id, title=None, final_chapter=10):
    return NovelConfig(
        id=novel_id,
        title=title or novel_id,
        source_language="zh",
        target_language="en",
        profile="historical_military",
        final_chapter=final_chapter,
    )


def write_project(repo, novel_config):
    root = repo / "novels" / novel_config.id
    root.mkdir(parents=True)
    (root / "novel.json").write_text(
        json.dumps(novel_config.to_dict()), encoding="utf-8"
    )
    return load_novel_project(novel_config.id, repo)


def saved_stats():
    price = pricing_snapshot(datetime(2026, 8, 21, 5, tzinfo=timezone.utc))
    return {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 10,
        "cost_usd": 0.0000165,
        "rate_correct_cost_usd": 0.0000165,
        **price,
    }


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_two_novel_configs_resolve_to_independent_paths(tmp_path):
    first = write_project(tmp_path, config("novel_a"))
    second = write_project(tmp_path, config("novel_b"))
    assert first.root != second.root
    for attribute in (
        "chapters_file", "progress_file", "glossary_file", "continuity_file",
        "issues_file", "audit_history_file", "chapter_dir", "pending_dir",
        "qa_dir", "audit_dir", "retroactive_dir", "epub_dir", "backup_dir",
        "benchmark_dir", "logs_dir", "pause_file",
        "glossary_candidate_resolution_dir",
    ):
        assert getattr(first, attribute) != getattr(second, attribute)


def test_translation_adapter_selects_project_as_process_working_directory(
    tmp_path, monkeypatch,
):
    project = write_project(tmp_path, config("novel_a"))
    project.data_dir.mkdir()
    project.chapters_file.write_text(
        json.dumps([{"chapter": number} for number in range(1, 11)]),
        encoding="utf-8",
    )
    calls = []

    class Result:
        returncode = 17

    def fake_run(command, cwd, env, check):
        calls.append((command, cwd, env, check))
        return Result()

    runner = tmp_path / "run_production_engine.py"
    monkeypatch.setattr(translate, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(translate, "PRODUCTION_RUNNER", runner)
    runner.write_text("unused", encoding="utf-8")
    monkeypatch.setattr(translate.subprocess, "run", fake_run)
    monkeypatch.setattr(translate, "load_translation_profile", lambda *args: object())
    monkeypatch.setattr(
        translate, "effective_translation_instructions", lambda *args: "instructions"
    )
    monkeypatch.setattr(
        translate, "load_novel_terminology_policy", lambda *args: object()
    )
    monkeypatch.setattr(
        translate,
        "load_novel_project",
        lambda novel_id: project if novel_id == "novel_a" else None,
    )

    assert translate.main(["--novel", "novel_a"]) == 17
    command, cwd, environment, check = calls[0]
    assert command[1] == str(runner)
    assert cwd == project.root
    assert environment[TRANSLATION_MODEL_ENV] == "deepseek-v4-pro"
    assert check is False


def test_progress_and_glossary_changes_do_not_cross_projects(tmp_path):
    first = write_project(tmp_path, config("novel_a"))
    second = write_project(tmp_path, config("novel_b"))
    first.data_dir.mkdir()
    second.data_dir.mkdir()
    first.progress_file.write_text('{"last_completed": 7}', encoding="utf-8")
    second.progress_file.write_text('{"last_completed": 2}', encoding="utf-8")
    first.glossary_file.write_text('{"entries": {"甲": {}}}', encoding="utf-8")
    second.glossary_file.write_text('{"entries": {"乙": {}}}', encoding="utf-8")

    first_progress = json.loads(first.progress_file.read_text(encoding="utf-8"))
    first_progress["last_completed"] = 8
    first.progress_file.write_text(json.dumps(first_progress), encoding="utf-8")
    first_glossary = json.loads(first.glossary_file.read_text(encoding="utf-8"))
    first_glossary["entries"]["丙"] = {}
    first.glossary_file.write_text(json.dumps(first_glossary), encoding="utf-8")

    assert json.loads(second.progress_file.read_text(encoding="utf-8")) == {
        "last_completed": 2
    }
    assert set(json.loads(second.glossary_file.read_text(encoding="utf-8"))["entries"]) == {"乙"}


def test_pending_recovery_is_isolated_per_novel(tmp_path):
    first = write_project(tmp_path, config("novel_a"))
    second = write_project(tmp_path, config("novel_b"))
    first.pending_dir.mkdir(parents=True)
    second.pending_dir.mkdir(parents=True)
    (first.pending_dir / "0200.md").write_text("paid A", encoding="utf-8")
    (first.pending_dir / "0200.json").write_text(
        json.dumps({"chapter": 200, "stage": "translated"}), encoding="utf-8"
    )
    calls = []
    recovered = obtain_translation(200, first.pending_dir, lambda: calls.append("A"))
    generated = obtain_translation(200, second.pending_dir, lambda: "new B")
    assert recovered["source"] == "pending"
    assert recovered["translation"] == "paid A"
    assert calls == []
    assert generated == {"source": "generated", "generated_result": "new B", "meta": {}}


def test_epub_logs_and_backups_are_isolated_per_novel(tmp_path):
    first = write_project(tmp_path, config("novel_a"))
    second = write_project(tmp_path, config("novel_b"))
    assert checkpoint_output_path(first.epub_dir, 1, 7).parent == first.epub_dir
    assert checkpoint_output_path(second.epub_dir, 1, 2).parent == second.epub_dir
    logger_a = SafeRunLogger(first.logs_dir)
    logger_b = SafeRunLogger(second.logs_dir)
    assert logger_a.emit("program_start")
    assert logger_b.emit("program_start")
    assert logger_a.path.parent == first.logs_dir
    assert logger_b.path.parent == second.logs_dir
    first_backup = first.backup_dir / "checkpoint_0007" / "progress.json"
    second_backup = second.backup_dir / "checkpoint_0002" / "progress.json"
    first_backup.parent.mkdir(parents=True)
    second_backup.parent.mkdir(parents=True)
    first_backup.write_text("A", encoding="utf-8")
    second_backup.write_text("B", encoding="utf-8")
    assert first_backup.read_text(encoding="utf-8") == "A"
    assert second_backup.read_text(encoding="utf-8") == "B"


def make_legacy_ming_checkpoint(root):
    for relative in ("source", "data", "output/chapters", "output/pending", "backup", "logs"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "source" / "novel.epub").write_bytes(b"epub bytes")
    (root / "data" / "progress.json").write_bytes(
        json.dumps({
            "completed": list(range(1, 200)),
            "last_completed": 199,
            "next_chapter": 200,
        }, indent=2).encode("utf-8")
    )
    (root / "data" / "glossary.json").write_bytes(b'{"entries":{"term":{}}}')
    (root / "data" / "continuity.json").write_bytes(b'{"latest_chapter":199}')
    (root / "data" / "issues.json").write_bytes(b'{"issues":[]}')
    (root / "data" / "audit_history.json").write_bytes(b'{"audits":[]}')
    (root / "data" / "chapters.json").write_bytes(b'[]')
    (root / "output" / "chapters" / "0199.md").write_bytes(b"committed 199")
    (root / "output" / "pending" / "0200.md").write_bytes(b"already-paid Chapter 200")
    (root / "output" / "pending" / "0200.json").write_text(json.dumps({
        "chapter": 200,
        "stage": "translated",
        "translation_stats": saved_stats(),
    }), encoding="utf-8")
    (root / "production_translation.txt").write_bytes(b"prompt bytes")


def test_migration_preserves_ming_checkpoint_and_paid_pending_bytes(tmp_path):
    make_legacy_ming_checkpoint(tmp_path)
    expected_hashes = {
        relative: sha256(tmp_path / relative)
        for relative in (
            "data/progress.json", "data/glossary.json",
            "output/pending/0200.md", "output/pending/0200.json",
        )
    }
    ming = config("ming_novel", "Ming Historical Novel", 1406)
    result = migrate_single_novel(tmp_path, ming)
    target = result["target"]
    assert result["status"] == "migrated"
    for relative, expected in expected_hashes.items():
        assert sha256(target / relative) == expected
    project = load_novel_project("ming_novel", tmp_path)
    recovered = obtain_translation(200, project.pending_dir, lambda: pytest.fail("regenerated"))
    assert recovered["source"] == "pending"
    assert recovered["meta"]["stage"] == "translated"
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    assert progress["last_completed"] == 199
    assert progress["next_chapter"] == 200

    repeated = migrate_single_novel(tmp_path, ming)
    assert repeated["status"] == "already_migrated"


def test_migration_never_overwrites_a_diverged_destination(tmp_path):
    make_legacy_ming_checkpoint(tmp_path)
    ming = config("ming_novel", "Ming Historical Novel", 1406)
    target = migrate_single_novel(tmp_path, ming)["target"]
    target_progress = target / "data" / "progress.json"
    target_progress.write_bytes(b"independent newer state")
    before = target_progress.read_bytes()
    with pytest.raises(MigrationError, match="not overwritten"):
        migrate_single_novel(tmp_path, ming)
    assert target_progress.read_bytes() == before
