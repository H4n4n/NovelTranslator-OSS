"""Start translate.py as its own process, notice when one is running, and keep a queue.

A novel counts as running if the process this app started is alive, or if something
else holds the novel's lock. Both checks are needed: novel_runner lets go of the lock
between steps, so the lock alone would look free in those gaps, and a run started from
the command line has no process record here.

Checking the lock means holding it for an instant. A command-line run starting in that
same instant would stop with "writer_busy", which loses nothing and can simply be
started again. Runs started here aren't exposed to this, because their process record
is checked first.
"""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from noveltranslator_core import atomic_write_json
from production_run_lock import ProductionRunLockedError, production_run_lock

WORKER_LOG = "ui_worker.log"


def process_start_time(pid):
    """A value identifying a live process, or None if it's gone. It guards against a reused pid."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return 0
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if (not kernel32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)) or code.value != 259
                or not kernel32.GetProcessTimes(wintypes.HANDLE(handle), *[ctypes.byref(t) for t in times])):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def translate_command(repo_root, novel_id, budget_usd=None, trial=None):
    command = [sys.executable, str(Path(repo_root) / "translate.py"), "--novel", novel_id, "--start",
               "--repo-root", str(repo_root)]
    if budget_usd is not None:
        command += ["--budget-usd", str(budget_usd)]
    if trial is not None:
        command += ["--trial", str(trial)]
    return command


class Workers:
    def __init__(self, state_dir, repo_root, command=translate_command):
        self.state_dir = Path(state_dir)
        self.repo_root = Path(repo_root)
        self.command = command
        self._mutex = threading.RLock()
        self._children = {}

    # Process records -----------------------------------------------------

    def _record_path(self, novel_id):
        return self.state_dir / "workers" / f"{novel_id}.json"

    def record(self, novel_id):
        try:
            return json.loads(self._record_path(novel_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def process_alive(self, novel_id):
        record = self.record(novel_id)
        return bool(record) and record.get("pid") is not None \
            and process_start_time(record["pid"]) == record.get("process_start")

    def running(self, project):
        with self._mutex:
            if self.process_alive(project.config.id):
                return True
            try:
                with production_run_lock(project.root):
                    return False
            except ProductionRunLockedError:
                return True

    def start(self, project, budget_usd=None, trial=None):
        """Launch the worker detached, so it outlives this server."""
        novel_id = project.config.id
        with self._mutex:
            project.logs_dir.mkdir(parents=True, exist_ok=True)
            environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            started = datetime.now(timezone.utc).isoformat()
            flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} \
                if os.name == "nt" else {"start_new_session": True}
            with (project.logs_dir / WORKER_LOG).open("ab") as log:
                log.write(f"\n--- Started {datetime.now(timezone.utc).isoformat()} ---\n".encode("utf-8"))
                log.flush()
                child = subprocess.Popen(self.command(self.repo_root, novel_id, budget_usd, trial),
                                         cwd=self.repo_root, env=environment, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, **flags)
            self._children[novel_id] = child
            self._record_path(novel_id).parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._record_path(novel_id), dict(
                pid=child.pid, process_start=process_start_time(child.pid), started_utc=started))

    # Queue ---------------------------------------------------------------

    @property
    def _queue_path(self):
        return self.state_dir / "queue.json"

    def queue(self):
        try:
            value = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return value if isinstance(value, list) else []

    def _save_queue(self, entries):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._queue_path, entries)

    def enqueue(self, novel_id, budget_usd=None, trial=None):
        with self._mutex:
            entries = [entry for entry in self.queue() if entry["novel_id"] != novel_id]
            entries.append(dict(novel_id=novel_id, budget_usd=budget_usd, trial=trial))
            self._save_queue(entries)

    def dequeue(self, novel_id):
        with self._mutex:
            self._save_queue([entry for entry in self.queue() if entry["novel_id"] != novel_id])

    def reorder(self, novel_ids):
        with self._mutex:
            entries = {entry["novel_id"]: entry for entry in self.queue()}
            if sorted(novel_ids) != sorted(entries):
                raise ValueError("the order must list every queued novel once")
            self._save_queue([entries[novel_id] for novel_id in novel_ids])


def run_scheduler(tick, stop_event, seconds=5):
    """Call tick() until stopped. A failing tick is printed and retried; it never ends the loop."""
    while not stop_event.wait(seconds):
        try:
            tick()
        except Exception as exc:
            print(f"Queue check failed: {type(exc).__name__}", file=sys.stderr)
