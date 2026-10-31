"""A synthetic novels folder and a fake worker, for trying the interface without spending anything.

    python -m ui.demo C:\\some\\empty\\folder
    python -m ui.server --demo --novels-root C:\\some\\empty\\folder --open

The server only accepts --demo for a folder made here. The fake worker writes placeholder
chapters and makes no requests. The API key screen uses a file inside this folder.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import novel_runner  # noqa: E402
import requested_corrections  # noqa: E402
import review_queue  # noqa: E402
from build_checkpoint_epub import rebuild_reader_epub  # noqa: E402
from create_novel import create_novel_project  # noqa: E402
from novel_project import NOVELS_ROOT_ENV, NovelConfig, load_novel_project  # noqa: E402
from noveltranslator_core import atomic_write_json  # noqa: E402
from production_run_lock import production_run_lock  # noqa: E402
from ui.api import App  # noqa: E402

MARKER = "demo_folder.txt"
SECONDS_PER_CHAPTER = 3
AUDIT_INTERVAL = 5
COST_PER_CHAPTER = 0.02
HALL = "\u5927\u5385"
GATE = "\u957f\u95e8"
SOURCE = "\u7532\u8d70\u8fdb\u5927\u5385\uff0c\u770b\u89c1\u957f\u95e8\u5916\u7684\u96ea\u3002\n\n\u4ed6\u6ca1\u6709\u8bf4\u8bdd\u3002"


def english(number):
    return (f"# Chapter {number}\n\nJia walked into the Great Hall and saw the snow beyond the Long Gate.\n\n"
            f"He said nothing. This is placeholder text for chapter {number}.\n")


def _write_chapter(project, number):
    project.chapter_dir.mkdir(parents=True, exist_ok=True)
    (project.chapter_dir / f"{number:04d}.md").write_text(english(number), encoding="utf-8", newline="\n")


def _save_progress(project, completed, audited, cost):
    progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
    progress.update(completed=list(range(1, completed + 1)), last_completed=completed, next_chapter=completed + 1,
                    last_audited_chapter=audited, rate_correct_cost_usd=cost)
    atomic_write_json(project.progress_file, progress)


def _make_novel(repo_root, novel_id, title, language, final, done, audited):
    config = NovelConfig(id=novel_id, title=title, source_language=language, target_language="en",
                         profile="historical_military", final_chapter=final, audit_interval=AUDIT_INTERVAL)
    create_novel_project(config, repo_root)
    project = load_novel_project(novel_id, repo_root)
    chapters = [dict(chapter=n, source_number=n, source_index=n, title=f"\u7b2c{n}\u7ae0", text=SOURCE * 3)
                for n in range(1, final + 1)]
    atomic_write_json(project.chapters_file, chapters)
    glossary = {"entries": {
        HALL: dict(english="Great Hall", category="location", status="provisional", first_chapter=1),
        GATE: dict(english="Long Gate", category="location", status="locked", first_chapter=1),
        "\u7532": dict(english="Jia", category="character", status="provisional", first_chapter=1),
    }}
    atomic_write_json(project.glossary_file, glossary)
    for number in range(1, done + 1):
        _write_chapter(project, number)
    _save_progress(project, done, audited, round(done * COST_PER_CHAPTER, 4))
    return project


def make_demo_folder(folder):
    folder = Path(folder).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError(f"The folder must be empty or new: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    os.environ[NOVELS_ROOT_ENV] = str(folder)
    (folder / ".ui").mkdir()
    (folder / ".ui" / MARKER).write_text("Made by ui.demo. Synthetic novels only.\n", encoding="utf-8")

    midway = _make_novel(REPO_ROOT, "demo-midway", "Demo: Halfway Through", "zh", 40, 12, 10)
    novel_runner.set_budget(midway, 1.0)
    review_queue.add_item(midway.data_dir, chapter=7, kind="commit_qa",
                          detail="Leftover source text remained in the chapter after repair.")
    review_queue.add_item(midway.data_dir, chapter=11, kind="commit_qa", detail="A locked term may not match.")
    requested_corrections.add_request(midway.data_dir, chinese=HALL, new_english="Grand Hall")
    _save_last_run(midway, novel_runner.TRIAL_FINISHED, "The trial chapters are ready to read.",
                   "Start again to continue the novel.")

    finished = _make_novel(REPO_ROOT, "demo-finished", "Demo: Finished Book", "zh", 10, 10, 10)
    _save_last_run(finished, novel_runner.COMPLETE, "The novel is fully translated.", "")
    rebuild_reader_epub(finished, take_lock=True)

    stopped = _make_novel(REPO_ROOT, "demo-stopped", "Demo: Stopped By The Provider", "zh", 20, 3, 0)
    message, action = novel_runner.STOP_MESSAGES["provider_key_or_credit"]
    _save_last_run(stopped, novel_runner.STOPPED, message, action)

    _make_novel(REPO_ROOT, "demo-new", "Demo: Not Started", "zh", 15, 0, 0)

    sample = folder.parent / f"{folder.name}_sample_novel.txt"
    sample.write_text("\u6837\u4e66\n\n" + "\n\n".join(
        f"\u7b2c{n}\u7ae0 \u96ea\u591c\n\n" + SOURCE * 8 for n in range(1, 13)) + "\n", encoding="utf-8")
    return folder, sample


def _save_last_run(project, status, message, action):
    atomic_write_json(project.data_dir / "last_run.json", dict(
        status=status, message=message, action=action, finished_utc=datetime.now(timezone.utc).isoformat()))


def demo_command(repo_root, novel_id, budget_usd=None, trial=None):
    command = [sys.executable, "-m", "ui.demo", "--worker", novel_id]
    if budget_usd is not None:
        command += ["--budget-usd", str(budget_usd)]
    if trial is not None:
        command += ["--trial", str(trial)]
    return command


def demo_app(repo_root):
    app = App(repo_root, command=demo_command, allow_paid_advice=False, require_key=False)
    if not (app.state_dir / MARKER).is_file():
        raise ValueError("--demo only runs on a folder made by: python -m ui.demo <folder>")
    app.secret_path = app.state_dir / "demo_key.dpapi"
    return app


def run_worker(argv):
    """Stands in for translate.py --start: same lock, same pause file, same last_run.json."""
    novel_id = argv[0]
    budget = float(argv[argv.index("--budget-usd") + 1]) if "--budget-usd" in argv else None
    trial = int(argv[argv.index("--trial") + 1]) if "--trial" in argv else None
    project = load_novel_project(novel_id, REPO_ROOT)
    if budget is not None:
        novel_runner.set_budget(project, budget)
    with production_run_lock(project.root):
        progress = json.loads(project.progress_file.read_text(encoding="utf-8"))
        done, audited = progress["last_completed"], progress.get("last_audited_chapter", 0)
        cost = float(progress.get("rate_correct_cost_usd", 0.0))
        stop_after = done + trial if trial else project.config.final_chapter
        result = (novel_runner.COMPLETE, "The novel is fully translated.", "")
        while done < project.config.final_chapter:
            if done >= stop_after:
                result = (novel_runner.TRIAL_FINISHED, "The trial chapters are ready to read.",
                          "Start again to continue the novel.")
                break
            if (novel_runner.remaining_budget(project) or 0) <= 0:
                result = (novel_runner.BUDGET_REACHED, "The budget for this novel is used up.", "Add budget to continue.")
                break
            while project.pause_file.exists():
                time.sleep(1)
            time.sleep(SECONDS_PER_CHAPTER)
            done += 1
            cost = round(cost + COST_PER_CHAPTER, 4)
            _write_chapter(project, done)
            if done % AUDIT_INTERVAL == 0 or done == project.config.final_chapter:
                audited = done
            _save_progress(project, done, audited, cost)
            print(f"Demo worker: chapter {done} done.", flush=True)
    _save_last_run(project, *result)
    rebuild_reader_epub(project, take_lock=True)
    print(f"Demo worker: {result[0]}", flush=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "--worker":
        return run_worker(argv[1:])
    if len(argv) != 1:
        print(__doc__)
        return 2
    folder, sample = make_demo_folder(argv[0])
    print(f"Made synthetic novels in {folder}")
    print(f"A sample file for Add novel is at {sample}")
    print(f'Start the app with: python -m ui.server --demo --novels-root "{folder}" --open')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
