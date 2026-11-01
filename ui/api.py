"""The interface's JSON API. No sockets here: server.py turns HTTP into handle() calls."""
import hashlib
import json
import math
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import novel_profile_advisor
import novel_runner
import requested_corrections
import review_queue
from build_checkpoint_epub import latest_output_path, rebuild_reader_epub
from chapter_library import index_chapter_library
from list_novels import inspect_novel_directory
from novel_intake import analyze_source, create_from_analysis, slugify_title
from novel_project import load_novel_project, novels_root_for, validate_novel_id
from noveltranslator_core import atomic_write_bytes, atomic_write_json
from production_run_lock import ProductionRunLockedError, production_run_lock
from ui.worker import WORKER_LOG, Workers, translate_command

MAX_JSON_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_MAX_AGE_SECONDS = 24 * 3600
GLOSSARY_PAGE = 200
GLOSSARY_STATUSES = ("provisional", "locked", "contextual")
_CHAPTER_RE = re.compile(r"^[0-9]{1,6}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_EPUB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.epub$")
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_AUDIT_FILE_RE = re.compile(r"^audit_[0-9]+_([0-9]+)\.finalization\.json$")

RUNNING_REFUSAL = ("Translation is running on this novel, so this can't be changed right now. "
                   "Wait for it to finish, then try again. Pausing isn't enough, because a paused run "
                   "is still in the middle of its work.")
# The engine keeps a byte-for-byte record of chapters an audit has covered. Editing one
# makes its next start stop with "operator disposition/completed recovery proof failed",
# and the novel stays blocked until the file is restored (see tests/test_ui_api.py). So
# this is refused here rather than loosening that check.
AUDITED_REFUSAL = ("An audit has already recorded this chapter. The engine checks recorded chapters each "
                   "time it starts, and a changed one stops the whole novel from continuing, so this "
                   "chapter can't be edited here. To change a term across past chapters, use the glossary.")
STATE_TEXT = {
    "running": "Translating now",
    "pausing": "Paused. The run is waiting and makes no paid requests until you resume",
    "paused": "Paused",
    "queued": "Waiting in the queue",
    "ready": "Ready to start",
    "complete": "Finished",
    "budget": "Stopped: budget used up",
    "trial": "Trial finished",
    "stopped": "Stopped",
}
LAST_RUN_STATE = {novel_runner.COMPLETE: "complete", novel_runner.BUDGET_REACHED: "budget",
                  novel_runner.TRIAL_FINISHED: "trial", novel_runner.PAUSED: "ready",
                  novel_runner.STOPPED: "stopped"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    method: str
    target: str
    body: object = None  # A binary stream. Read at most `length` bytes from it.
    length: int = 0


@dataclass
class Response:
    status: int = 200
    value: object = None
    file_path: Path | None = None
    file_name: str = ""
    headers: dict = field(default_factory=dict)


def _read_json(request):
    if request.length > MAX_JSON_BYTES:
        raise ApiError(413, "That request is too large.")
    raw = request.body.read(request.length) if request.body and request.length else b"{}"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "The request wasn't valid JSON.") from None
    if not isinstance(value, dict):
        raise ApiError(400, "The request must be a JSON object.")
    return value


def _positive_money(value, label="Budget"):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ApiError(400, f"{label} must be a positive number of US dollars.")
    return float(value)


def _positive_int(value, label):
    if type(value) is not int or value < 1:
        raise ApiError(400, f"{label} must be a whole number above zero.")
    return value


def safe_upload_name(name):
    """The uploaded file's plain name, or an error. Unicode is fine: titles are often Chinese."""
    if not isinstance(name, str) or not name or len(name) > 200 or name != name.strip() or name.endswith("."):
        raise ApiError(400, "That file name isn't allowed.")
    if name.startswith(".") or ".." in name or any(c in name for c in '/\\:*?"<>|') \
            or any(ord(c) < 32 for c in name):
        raise ApiError(400, "That file name isn't allowed.")
    path = Path(name)
    if path.suffix.lower() not in (".epub", ".txt"):
        raise ApiError(400, "Only .epub and .txt files can be added.")
    if _WINDOWS_RESERVED.match(path.stem):
        raise ApiError(400, "That file name isn't allowed.")
    return name


class App:
    def __init__(self, repo_root, *, secret_path=None, dpapi=None, command=translate_command,
                 advice_client=None, allow_paid_advice=True, require_key=True):
        self.repo_root = Path(repo_root).resolve()
        self.novels_root = novels_root_for(self.repo_root)
        self.state_dir = self.novels_root / ".ui"
        self.workers = Workers(self.state_dir, self.repo_root, command)
        self.secret_path = Path(secret_path) if secret_path else None
        self.dpapi = dpapi
        self.advice_client = advice_client
        self.allow_paid_advice = allow_paid_advice
        self.require_key = require_key
        self._library_cache = {}
        self._routes = [
            ("GET", ("novels",), self.list_novels),
            ("GET", ("novels", "*"), self.novel_detail),
            ("POST", ("novels", "*", "start"), self.start),
            ("POST", ("novels", "*", "pause"), self.pause),
            ("POST", ("novels", "*", "resume"), self.resume),
            ("POST", ("novels", "*", "budget"), self.set_budget),
            ("POST", ("novels", "*", "allow-resend"), self.allow_resend),
            ("GET", ("novels", "*", "review"), self.review),
            ("POST", ("novels", "*", "review", "resolve"), self.resolve_flag),
            ("GET", ("novels", "*", "glossary"), self.glossary),
            ("POST", ("novels", "*", "glossary"), self.edit_glossary),
            ("GET", ("novels", "*", "chapters"), self.chapters),
            ("GET", ("novels", "*", "chapters", "*"), self.chapter),
            ("POST", ("novels", "*", "chapters", "*"), self.edit_chapter),
            ("POST", ("novels", "*", "epub", "rebuild"), self.rebuild_epub),
            ("GET", ("novels", "*", "epub", "*"), self.download_epub),
            ("GET", ("queue",), self.queue),
            ("POST", ("queue",), self.reorder_queue),
            ("POST", ("queue", "remove"), self.remove_from_queue),
            ("GET", ("profiles",), self.profiles),
            ("POST", ("profiles",), self.save_suggested_profile),
            ("POST", ("uploads",), self.upload),
            ("POST", ("uploads", "*", "advice"), self.paid_advice),
            ("POST", ("uploads", "*", "create"), self.create_novel),
            ("GET", ("settings",), self.settings),
            ("POST", ("settings",), self.save_settings),
            ("POST", ("settings", "api-key"), self.set_api_key),
        ]

    # Routing -------------------------------------------------------------

    def handle(self, request):
        parts = urlsplit(request.target)
        # Split before decoding, so an encoded slash can't make a new path segment.
        segments = tuple(unquote(part) for part in parts.path.split("/")[2:])
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        known_path = False
        for method, pattern, function in self._routes:
            if len(pattern) != len(segments) or not all(p in ("*", s) for p, s in zip(pattern, segments)):
                continue
            known_path = True
            if method != request.method:
                continue
            arguments = [s for p, s in zip(pattern, segments) if p == "*"]
            try:
                result = function(request, query, *arguments)
            except ApiError as error:
                return Response(error.status, {"error": error.message})
            return result if isinstance(result, Response) else Response(200, result)
        return Response(405 if known_path else 404, {"error": "Not found."})

    def _project(self, novel_id):
        try:
            return load_novel_project(validate_novel_id(novel_id), self.repo_root)
        except ValueError as exc:
            raise ApiError(400, f"That novel can't be used: {exc}") from None
        except OSError:
            raise ApiError(404, "There's no novel with that name.") from None

    @contextmanager
    def _no_worker(self, project):
        """Hold the novel's lock for a write, refusing if a run is active or between steps."""
        if self.workers.process_alive(project.config.id):
            raise ApiError(409, RUNNING_REFUSAL)
        try:
            with production_run_lock(project.root):
                yield
        except ProductionRunLockedError:
            raise ApiError(409, RUNNING_REFUSAL) from None

    # Library and status --------------------------------------------------

    @staticmethod
    def _progress(project):
        return json.loads(project.progress_file.read_text(encoding="utf-8"))

    @staticmethod
    def _last_run(project):
        try:
            return json.loads((project.data_dir / "last_run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _state(self, project):
        """The novel's state in one word, plus the message and next step to show with it."""
        novel_id = project.config.id
        paused = project.pause_file.exists()
        if self.workers.running(project):
            return dict(state="pausing" if paused else "running", message="", action="")
        if paused:
            return dict(state="paused", message="", action="")
        if any(entry["novel_id"] == novel_id for entry in self.workers.queue()):
            return dict(state="queued", message="", action="")
        last = self._last_run(project)
        record = self.workers.record(novel_id)
        if record and (not last or last.get("finished_utc", "") < record.get("started_utc", "")):
            return dict(state="stopped", message="The last run ended without reporting a result.",
                        action="See the run output below, then start again. Finished chapters are safe.")
        if not last:
            return dict(state="ready", message="", action="")
        return dict(state=LAST_RUN_STATE.get(last.get("status"), "stopped"), message=last.get("message", ""),
                    action=last.get("action", ""), blockers=last.get("blockers") or [], reason=last.get("reason"))

    def _summary(self, project):
        progress = self._progress(project)
        state = self._state(project)
        epub = latest_output_path(project.epub_dir, project.config.id)
        return dict(
            id=project.config.id, title=project.config.title, valid=True,
            completed=progress.get("last_completed", 0), final=project.config.final_chapter,
            cost_usd=novel_runner._cost(progress), budget_left_usd=novel_runner.remaining_budget(project),
            flagged=len(review_queue.open_items(project.data_dir)),
            epub=epub.name if epub.is_file() else None, paused=project.pause_file.exists(),
            state_text=STATE_TEXT[state["state"]], **state)

    def list_novels(self, request, query):
        novels = []
        if self.novels_root.is_dir():
            for directory in sorted(self.novels_root.iterdir(), key=lambda path: path.name):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    inspected = inspect_novel_directory(directory, self.repo_root)
                    if not inspected["valid"]:
                        raise ValueError(inspected["error"])
                    novels.append(self._summary(load_novel_project(directory.name, self.repo_root)))
                except Exception as exc:
                    novels.append(dict(id=directory.name, title=directory.name, valid=False,
                                       error=f"This novel's files have a problem: {exc}"))
        return dict(novels=novels, queue=self.workers.queue())

    def _pace(self, project, progress):
        from noveltranslator_observability import recent_chapter_metrics
        from production_accounting import GOAT_METADATA
        goat = progress.get("provider") == GOAT_METADATA["provider"]
        metrics = recent_chapter_metrics(project.logs_dir, **(dict(
            accounting_metadata=GOAT_METADATA,
            start_chapter=progress.get("active_accounting_epoch", {}).get("start_chapter")) if goat else {}))
        return dict(seconds_per_chapter=metrics.get("average_chapter_seconds"),
                    cost_per_chapter=metrics.get("average_chapter_cost"))

    def _log_tail(self, project, lines=60):
        def tail(path):
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 64 * 1024))
                return stream.read().decode("utf-8", errors="replace").splitlines()[-lines:]

        output = []
        worker_log = project.logs_dir / WORKER_LOG
        if worker_log.is_file():
            output = tail(worker_log)
        events = []
        logs = sorted(project.logs_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime) \
            if project.logs_dir.is_dir() else []
        if logs:
            for line in tail(logs[-1]):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    moment = str(event.get("utc") or event.get("timestamp") or event.get("time") or "")[:19]
                    chapter = f"chapter {event['chapter']} " if event.get("chapter") else ""
                    stage = f" ({event['stage']})" if event.get("stage") else ""
                    events.append(f"{moment} {chapter}{event.get('event', '')}{stage}".strip())
        return dict(events=events, output=output)

    def novel_detail(self, request, query, novel_id):
        project = self._project(novel_id)
        progress = self._progress(project)
        epubs = sorted(path.name for path in project.epub_dir.glob("*.epub")) if project.epub_dir.is_dir() else []
        try:
            pace = self._pace(project, progress)
        except Exception:  # Pace is a nicety; a bad log line shouldn't hide the page.
            pace = dict(seconds_per_chapter=None, cost_per_chapter=None)
        return dict(self._summary(project), **pace, epubs=epubs, log=self._log_tail(project),
                    last_audited=progress.get("last_audited_chapter", 0),
                    corrections_waiting=requested_corrections.waiting(project.data_dir),
                    has_next_audit=self._has_next_audit(project, progress))

    # Running -------------------------------------------------------------

    def _default_budget(self):
        return self._load_settings().get("default_budget_usd")

    def _start_or_queue(self, project, budget_usd, trial):
        novel_id = project.config.id
        if self.workers.running(project):
            raise ApiError(409, "This novel is already being translated.")
        if project.pause_file.exists():
            raise ApiError(409, "This novel is paused. Resume it instead.")
        if self.require_key and not self._key_present():
            raise ApiError(400, "Add your API key in Settings before starting a novel.")
        if budget_usd is None and novel_runner.remaining_budget(project) is None:
            budget_usd = self._default_budget()
            if budget_usd is None:
                raise ApiError(400, "This novel has no budget yet. Enter one, or set a default budget in Settings.")
        with self.workers._mutex:
            queue = self.workers.queue()
            if self._someone_running() or (queue and queue[0]["novel_id"] != novel_id):
                self.workers.enqueue(novel_id, budget_usd, trial)
                return dict(started=False, queued=True,
                            message="Another novel goes first, so this one is in the queue.")
            self.workers.dequeue(novel_id)
            self.workers.start(project, budget_usd, trial)
        return dict(started=True, queued=False, message="Started.")

    def _someone_running(self):
        if not self.novels_root.is_dir():
            return False
        for directory in self.novels_root.iterdir():
            if directory.is_dir() and not directory.name.startswith("."):
                try:
                    if self.workers.running(load_novel_project(directory.name, self.repo_root)):
                        return True
                except (OSError, ValueError):
                    continue
        return False

    def tick(self):
        """Start the next queued novel once nothing is running. Called on a timer."""
        with self.workers._mutex:
            queue = self.workers.queue()
            if not queue or self._someone_running():
                return None
            entry = queue[0]
            self.workers.dequeue(entry["novel_id"])
            try:
                project = load_novel_project(entry["novel_id"], self.repo_root)
            except (OSError, ValueError):
                return None
            if project.pause_file.exists():
                return None
            self.workers.start(project, entry.get("budget_usd"), entry.get("trial"))
            return entry["novel_id"]

    def start(self, request, query, novel_id):
        project = self._project(novel_id)
        data = _read_json(request)
        budget = data.get("budget_usd")
        budget = None if budget in (None, "") else _positive_money(budget)
        trial = data.get("trial")
        trial = None if trial in (None, "") else _positive_int(trial, "Trial chapters")
        return self._start_or_queue(project, budget, trial)

    def pause(self, request, query, novel_id):
        project = self._project(novel_id)
        atomic_write_bytes(project.pause_file, b"Paused from the interface.\n")
        return dict(paused=True)

    def resume(self, request, query, novel_id):
        """Clear the pause. A run still waiting carries on; otherwise start one if it has budget."""
        project = self._project(novel_id)
        project.pause_file.unlink(missing_ok=True)
        if self.workers.running(project):
            return dict(paused=False, started=False, queued=False, message="Resumed.")
        left = novel_runner.remaining_budget(project)
        if (left is None and self._default_budget() is None) or (left is not None and left <= 0):
            return dict(paused=False, started=False, queued=False,
                        message="The pause is off. This novel needs more budget before it can start.")
        return dict(paused=False, **self._start_or_queue(project, None, None))

    def allow_resend(self, request, query, novel_id):
        project = self._project(novel_id)
        if self.workers.running(project):
            raise ApiError(409, "This novel is running.")
        novel_runner.allow_resend(project)
        return self._start_or_queue(project, None, None)

    def set_budget(self, request, query, novel_id):
        project = self._project(novel_id)
        budget = _positive_money(_read_json(request).get("budget_usd"))
        novel_runner.set_budget(project, budget)
        return dict(budget_left_usd=novel_runner.remaining_budget(project))

    def queue(self, request, query):
        return dict(queue=self.workers.queue())

    def reorder_queue(self, request, query):
        order = _read_json(request).get("order")
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ApiError(400, "The order must be a list of novel names.")
        try:
            self.workers.reorder(order)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return dict(queue=self.workers.queue())

    def remove_from_queue(self, request, query):
        novel_id = _read_json(request).get("novel_id")
        self.workers.dequeue(novel_id)  # Only compared with queued names, never used as a path.
        return dict(queue=self.workers.queue())

    # Review --------------------------------------------------------------

    def review(self, request, query, novel_id):
        project = self._project(novel_id)
        return dict(items=[dict(chapter=item["chapter"], kind=item["kind"], detail=item.get("detail", ""),
                                utc=item.get("utc", "")) for item in review_queue.open_items(project.data_dir)])

    def resolve_flag(self, request, query, novel_id):
        project = self._project(novel_id)
        data = _read_json(request)
        chapter, kind = data.get("chapter"), data.get("kind")
        if not any(item["chapter"] == chapter and item["kind"] == kind
                   for item in review_queue.open_items(project.data_dir)):
            raise ApiError(404, "That flag isn't open.")
        with self._no_worker(project):
            review_queue.add_item(project.data_dir, chapter=chapter, kind="flag_resolved", detail=kind)
        return dict(flagged=len(review_queue.open_items(project.data_dir)))

    # Glossary ------------------------------------------------------------

    @staticmethod
    def _glossary_entries(project):
        return json.loads(project.glossary_file.read_text(encoding="utf-8")).get("entries", {})

    def glossary(self, request, query, novel_id):
        project = self._project(novel_id)
        entries = self._glossary_entries(project)
        search = query.get("q", "").strip().lower()
        rows = []
        for chinese, entry in sorted(entries.items(), key=lambda pair: str(pair[1].get("english", "")).lower()):
            if query.get("status") and entry.get("status") != query["status"]:
                continue
            if query.get("category") and entry.get("category") != query["category"]:
                continue
            if search and search not in chinese.lower() and search not in str(entry.get("english", "")).lower():
                continue
            rows.append(dict(chinese=chinese, english=entry.get("english", ""), category=entry.get("category", ""),
                             status=entry.get("status", "provisional"), notes=entry.get("notes", ""),
                             first_chapter=entry.get("first_chapter")))
        try:
            offset = max(0, int(query.get("offset", 0)))
        except ValueError:
            offset = 0
        return dict(total=len(rows), offset=offset, page_size=GLOSSARY_PAGE, entries=rows[offset:offset + GLOSSARY_PAGE],
                    categories=sorted({str(entry.get("category", "")) for entry in entries.values()} - {""}),
                    statuses=GLOSSARY_STATUSES, corrections_waiting=requested_corrections.waiting(project.data_dir))

    @staticmethod
    def _has_next_audit(project, progress):
        return progress.get("last_audited_chapter", 0) < project.config.final_chapter

    def edit_glossary(self, request, query, novel_id):
        project = self._project(novel_id)
        data = _read_json(request)
        chinese = data.get("chinese")
        if not isinstance(chinese, str):
            raise ApiError(400, "Say which term to change.")
        changes = {}
        for key in ("english", "category", "notes", "status"):
            if key in data:
                if not isinstance(data[key], str):
                    raise ApiError(400, f"{key} must be text.")
                changes[key] = data[key].strip()
        for key in ("english", "category"):
            if key in changes and not changes[key]:
                raise ApiError(400, f"{key} can't be empty.")
        if "status" in changes and changes["status"] not in GLOSSARY_STATUSES:
            raise ApiError(400, "That status isn't recognised.")
        with self._no_worker(project):
            document = json.loads(project.glossary_file.read_text(encoding="utf-8"))
            entry = document.get("entries", {}).get(chinese)
            if entry is None:
                raise ApiError(404, "That term isn't in this novel's glossary.")
            message = "Saved."
            new_english = changes.get("english")
            if new_english and new_english != entry.get("english") and data.get("correct_past") is True:
                # The audit compares old and new English, so the entry keeps its old
                # English until then. Changing it now would leave nothing to correct.
                changes.pop("english")
                requested_corrections.add_request(project.data_dir, chinese=chinese, new_english=new_english)
                message = ("Queued. The next audit changes this term and corrects past chapters. Until then "
                           "new chapters keep the current English."
                           if self._has_next_audit(project, self._progress(project)) else
                           "Saved as a waiting request, but nothing has changed yet: this novel's last audit has "
                           "already run, so there's no audit left to apply it.")
            elif new_english and new_english != entry.get("english"):
                message = "Saved. New chapters use this English. Past chapters keep what they have."
            if changes:
                entry.update(changes)
                atomic_write_json(project.glossary_file, document)
        return dict(message=message, corrections_waiting=requested_corrections.waiting(project.data_dir))

    # Chapters ------------------------------------------------------------

    def _source_library(self, project):
        stat = project.chapters_file.stat()
        key = (project.config.id, stat.st_mtime_ns, stat.st_size)
        if key not in self._library_cache:
            items = json.loads(project.chapters_file.read_text(encoding="utf-8"))
            library = index_chapter_library(items, project.config.final_chapter, project.config.chapter_numbering)
            self._library_cache = {key: library}  # Only the latest novel: these can be large.
        return self._library_cache[key]

    @staticmethod
    def _audited_through(project, progress):
        """The newest chapter an audit has recorded, counting one that began but didn't finish."""
        through = progress.get("last_audited_chapter", 0)
        if project.audit_dir.is_dir():
            for path in project.audit_dir.glob("*.finalization.json"):
                match = _AUDIT_FILE_RE.match(path.name)
                if match:
                    through = max(through, int(match.group(1)))
        return through

    def _chapter_number(self, project, text):
        if not _CHAPTER_RE.match(text) or not 1 <= int(text) <= project.config.final_chapter:
            raise ApiError(400, "That chapter number isn't valid for this novel.")
        return int(text)

    def chapters(self, request, query, novel_id):
        project = self._project(novel_id)
        progress = self._progress(project)
        audited = self._audited_through(project, progress)
        flagged = {item["chapter"] for item in review_queue.open_items(project.data_dir)}
        library = self._source_library(project)
        rows = [dict(number=number, title=item.get("title") or item.get("heading") or "",
                     translated=(project.chapter_dir / f"{number:04d}.md").is_file(),
                     audited=number <= audited, flagged=number in flagged)
                for number, item in sorted(library.items())]
        return dict(chapters=rows, audited_through=audited, last_completed=progress.get("last_completed", 0))

    def chapter(self, request, query, novel_id, number):
        project = self._project(novel_id)
        number = self._chapter_number(project, number)
        item = self._source_library(project).get(number) or {}
        path = project.chapter_dir / f"{number:04d}.md"
        raw = path.read_bytes() if path.is_file() else None
        reason = ""
        if raw is None:
            reason = "This chapter hasn't been translated yet."
        elif number <= self._audited_through(project, self._progress(project)):
            reason = AUDITED_REFUSAL
        elif self.workers.running(project):
            reason = RUNNING_REFUSAL
        return dict(number=number, title=item.get("title") or item.get("heading") or "", source=item.get("text", ""),
                    english=raw.decode("utf-8", errors="replace") if raw is not None else None,
                    sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
                    editable=not reason, edit_refusal=reason)

    def edit_chapter(self, request, query, novel_id, number):
        project = self._project(novel_id)
        number = self._chapter_number(project, number)
        data = _read_json(request)
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "A chapter can't be empty.")
        path = project.chapter_dir / f"{number:04d}.md"
        with self._no_worker(project):
            if not path.is_file():
                raise ApiError(404, "This chapter hasn't been translated yet.")
            if number <= self._audited_through(project, self._progress(project)):
                raise ApiError(409, AUDITED_REFUSAL)
            before = path.read_bytes()
            if hashlib.sha256(before).hexdigest() != data.get("expected_sha256"):
                raise ApiError(409, "This chapter changed since you opened it. Reopen it and make your edit again.")
            after = text.replace("\r\n", "\n").encode("utf-8")
            if after == before:
                return dict(message="Nothing changed.", backup=None)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_dir = project.backup_dir / "manual_edits"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{number:04d}_{stamp}.md"
            backup.write_bytes(before)
            atomic_write_bytes(path, after)
            with (backup_dir / "edits.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(chapter=number, utc=stamp, backup=backup.name)) + "\n")
        return dict(message="Saved. The earlier text is kept in the novel's backup folder.", backup=backup.name)

    # EPUB ----------------------------------------------------------------

    def rebuild_epub(self, request, query, novel_id):
        project = self._project(novel_id)
        if self.workers.process_alive(project.config.id):
            raise ApiError(409, RUNNING_REFUSAL)
        report = rebuild_reader_epub(project, take_lock=True)
        if report.get("status") == "ERROR":
            if report.get("error_type") == "ProductionRunLockedError":
                raise ApiError(409, RUNNING_REFUSAL)
            raise ApiError(400, f"The EPUB couldn't be built: {report.get('error')}")
        return dict(status=report.get("status"), name=latest_output_path(project.epub_dir, project.config.id).name)

    def download_epub(self, request, query, novel_id, name):
        project = self._project(novel_id)
        if not _EPUB_RE.match(name) or ".." in name:
            raise ApiError(400, "That file name isn't allowed.")
        path = (project.epub_dir / name).resolve()
        if path.parent != project.epub_dir.resolve() or not path.is_file():
            raise ApiError(404, "That EPUB doesn't exist.")
        return Response(file_path=path, file_name=name)

    # Adding a novel ------------------------------------------------------

    def _upload_dir(self, token):
        if not _TOKEN_RE.match(token):
            raise ApiError(400, "That upload isn't valid.")
        path = self.state_dir / "uploads" / token
        if not path.is_dir():
            raise ApiError(404, "That upload has expired. Add the file again.")
        return path

    def _clear_old_uploads(self):
        root = self.state_dir / "uploads"
        if root.is_dir():
            for path in root.iterdir():
                if path.is_dir() and time.time() - path.stat().st_mtime > UPLOAD_MAX_AGE_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)

    def upload(self, request, query):
        name = safe_upload_name(query.get("name"))
        if request.length <= 0:
            raise ApiError(400, "That file is empty.")
        if request.length > MAX_UPLOAD_BYTES:
            raise ApiError(413, "That file is larger than 2 GB.")
        self._clear_old_uploads()
        token = secrets.token_hex(16)
        folder = self.state_dir / "uploads" / token
        folder.mkdir(parents=True)
        source = folder / name
        try:
            remaining = request.length
            with source.open("wb") as stream:
                while remaining > 0:
                    chunk = request.body.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(400, "The upload was cut off. Try again.")
                    stream.write(chunk)
                    remaining -= len(chunk)
            analysis = analyze_source(source)
            atomic_write_json(folder / "analysis.json", analysis)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        shown = {key: value for key, value in analysis.items() if key != "source_path"}
        return dict(token=token, analysis=shown,
                    suggested_id=slugify_title(analysis.get("title_guess"), repo_root=self.repo_root),
                    profiles=novel_profile_advisor.list_profiles(self.repo_root))

    def profiles(self, request, query):
        return dict(profiles=novel_profile_advisor.list_profiles(self.repo_root))

    def _request_advice(self, analysis, profiles):
        if self.advice_client is not None:
            return novel_profile_advisor.request_advice(analysis, client=self.advice_client, profiles=profiles)
        from command_code_provider_credentials import command_code_api_key_scope
        from command_code_provider_transport import create_command_code_client
        with command_code_api_key_scope(secret_path=self.secret_path, dpapi=self.dpapi) as key:
            client = create_command_code_client(api_key=key)
            try:
                return novel_profile_advisor.request_advice(analysis, client=client, profiles=profiles)
            finally:
                client.close()

    def paid_advice(self, request, query, token):
        """One paid request. Only ever runs from the explicit button, with confirm_paid set."""
        folder = self._upload_dir(token)
        if _read_json(request).get("confirm_paid") is not True:
            raise ApiError(400, "This makes a paid request, so it needs confirmation.")
        if not self.allow_paid_advice:
            raise ApiError(409, "Paid suggestions are turned off in demo mode.")
        if self.advice_client is None and not self._key_present():
            raise ApiError(400, "Add your API key in Settings first.")
        analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        if analysis.get("error"):
            raise ApiError(400, "This file couldn't be read, so there's nothing to suggest from.")
        profiles = novel_profile_advisor.list_profiles(self.repo_root)
        try:
            advice, usage = self._request_advice(analysis, profiles)
        except Exception as exc:  # Only the type: a provider's message could echo request details.
            raise ApiError(502, f"The suggestion request failed ({type(exc).__name__}). "
                                "You can still choose a profile yourself.") from None
        atomic_write_json(folder / "advice.json", advice)
        return dict(advice=advice, usage=usage)

    def save_suggested_profile(self, request, query):
        data = _read_json(request)
        advice_path = self._upload_dir(str(data.get("token", ""))) / "advice.json"
        if not advice_path.is_file():
            raise ApiError(400, "There's no suggested profile to save.")
        advice = json.loads(advice_path.read_text(encoding="utf-8"))
        try:
            draft = novel_profile_advisor.draft_new_profile(data.get("base_profile"), advice, repo_root=self.repo_root)
            try:
                novel_profile_advisor.save_profile(draft, data.get("id"), repo_root=self.repo_root)
            finally:
                novel_profile_advisor.discard_draft(draft)
        except (OSError, ValueError, TypeError) as exc:
            raise ApiError(400, f"The profile couldn't be saved: {exc}") from None
        return dict(profiles=novel_profile_advisor.list_profiles(self.repo_root))

    def create_novel(self, request, query, token):
        folder = self._upload_dir(token)
        data = _read_json(request)
        analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "The novel needs a title.")
        novel_id = data.get("novel_id") or slugify_title(title, repo_root=self.repo_root)
        try:
            validate_novel_id(novel_id)
            if (self.novels_root / novel_id).exists():
                raise ValueError(f"a novel named {novel_id} already exists")
            create_from_analysis(analysis, novel_id=novel_id, title=title.strip(), profile=data.get("profile"),
                                 addendum=data.get("addendum") or "", repo_root=self.repo_root)
        except Exception as exc:
            raise ApiError(400, f"The novel couldn't be created: {exc}") from None
        shutil.rmtree(folder, ignore_errors=True)
        return dict(id=novel_id)

    # Settings ------------------------------------------------------------

    def _load_settings(self):
        try:
            value = json.loads((self.state_dir / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _secret_path(self):
        if self.secret_path is not None:
            return self.secret_path
        from command_code_provider_credentials import command_code_secret_path
        return Path(command_code_secret_path())

    def _key_present(self):
        try:
            return self._secret_path().is_file()  # Only asks if the file exists. Never opens it.
        except Exception:
            return False

    def settings(self, request, query):
        return dict(api_key_present=self._key_present(), default_budget_usd=self._default_budget(),
                    novels_folder=str(self.novels_root), paid_advice_available=self.allow_paid_advice)

    def save_settings(self, request, query):
        value = _read_json(request).get("default_budget_usd")
        settings = self._load_settings()
        settings["default_budget_usd"] = None if value in (None, "") else _positive_money(value, "Default budget")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_dir / "settings.json", settings)
        return self.settings(request, query)

    def set_api_key(self, request, query):
        """Encrypt and store the key. It is never echoed, logged or sent anywhere from here."""
        key = _read_json(request).get("key")
        if not isinstance(key, str) or len(key.strip()) < 8 or len(key) > 4096 or any(c.isspace() for c in key.strip()):
            raise ApiError(400, "That doesn't look like an API key.")
        from benchmark_longitudinal_g7g_stage1_secure_launcher import init_secret
        try:
            init_secret(secret_path=self._secret_path(), dpapi=self.dpapi, passphrase_reader=lambda: key.strip())
        except Exception as exc:  # Type only, so nothing about the key can reach a response.
            raise ApiError(500, f"The key couldn't be stored securely ({type(exc).__name__}).") from None
        return dict(api_key_present=self._key_present())
