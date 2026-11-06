"""Run source-first batches through the production module (docs/BATCH_MODE.md).

prepare_batch_if_due() runs the paid batch phases before the lifecycle reaches a
batch's first chapter. batch_commit_scope() then lets the unchanged
process_chapter() commit each batch chapter in order, answering translation,
state extraction and glossary review from the batch instead of new requests.

Every request goes through the module's journaled request path, so the request
journal, retries, pause flag and budget apply unchanged. Each request's exact
payload is saved before it is sent, so a restart replays recorded answers.
"""
import copy
import json
import re
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import batch_translation as bt
from chapter_text_checks import missing_dialogue_lines
from glossary_integrity import stable_glossary_candidate_id
from noveltranslator_core import atomic_write_json, atomic_write_text
from production_failure_classification import ProductionControlError
from production_paid_request_journal import paid_request_journal_path
from production_translation_request import build_translation_request, filter_refused_marker
from production_translation_recovery import TranslationStageResult
from review_queue import add_item as add_review_item

VERSION = 1
WARM_UP_HEAD_START_SECONDS = 30
SHORT_TAIL_CHAPTERS = 10  # Fewer chapters than this (and than a quarter batch) left at the end join the last batch.
# While the next batch is prepared ahead, its requests leave this much of the budget
# for the batch being saved: an audit request holds about $1.50 aside.
AHEAD_HEADROOM_USD = 1.50
PREPARING = "preparing"
COMMITTING = "committing"
COMPLETE = "complete"
ABANDONED = "abandoned"
TERMS_ONLY = "terms_only"


class BatchTermsOnlyStop(ProductionControlError):
    """A terms-only rehearsal settled its terms; the run stops without translating."""


class BatchChapterFailed(Exception):
    """One chapter's answer came back unusable; the batch continues without it.

    `stats` is the answer's recorded usage, which still has to be counted.
    """

    def __init__(self, message, stats=None):
        super().__init__(message)
        self.stats = stats


class _Globals:
    """The production module seen through its globals(), however it was loaded."""

    def __init__(self, namespace):
        object.__setattr__(self, "_namespace", namespace)

    def __getattr__(self, name):
        try:
            return self._namespace[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self._namespace[name] = value


def _module(module):
    return _Globals(module) if isinstance(module, dict) else module


def batch_root(data_dir):
    return Path(data_dir) / "batch"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def manifests(data_dir):
    root = batch_root(data_dir)
    return [_read(path) for path in sorted(root.glob("*/manifest.json"))] if root.is_dir() else []


def active_manifests(data_dir):
    """Unfinished batches in chapter order: the one being saved, and at most two prepared ahead."""
    active = [item for item in manifests(data_dir) if item["status"] in (PREPARING, COMMITTING)]
    if len(active) > 3:
        raise RuntimeError("more than three unfinished translation batches")
    return sorted(active, key=lambda item: item["start"])


def active_manifest(data_dir):
    active = active_manifests(data_dir)
    return active[0] if active else None


def manifest_for(data_dir, chapter):
    return next((item for item in active_manifests(data_dir) if item["start"] <= chapter <= item["end"]), None)


def uncommitted_batch_cost(data_dir, last_completed):
    """Paid batch work not yet in progress accounting, for the run budget: translations
    of chapters not yet saved, and helper requests of a batch prepared ahead."""
    return sum(sum(item.get("translation_cost_usd", 0.0) for number, item in manifest["chapters"].items()
                   if int(number) > last_completed)
               + sum(entry["stats"].get("cost_usd", 0.0) for entry in manifest.get("deferred_accounting", []))
               for manifest in active_manifests(data_dir))


class Batch:
    """One batch's files under data/batch/<start>/."""

    def __init__(self, data_dir, start):
        self.dir = batch_root(data_dir) / f"{start:04d}"
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = _read(self.manifest_path)

    @property
    def start(self):
        return self.manifest["start"]

    @property
    def chapters(self):
        return list(range(self.manifest["start"], self.manifest["end"] + 1))

    def chapter(self, number):
        return self.manifest["chapters"].setdefault(str(number), {})

    def save(self):
        self.manifest["updated_utc"] = _now()
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.manifest_path, self.manifest)

    def path(self, *parts):
        return self.dir.joinpath(*parts)

    def load(self, *parts, default=None):
        return _read(self.path(*parts), default)

    def write(self, value, *parts):
        path = self.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            atomic_write_text(path, value)
        else:
            atomic_write_json(path, value)

    def saved_request(self, name, build):
        """The exact payload sent for this request; built once, then reused on restart."""
        saved = self.load("requests", f"{name}.json")
        if saved is None:
            saved = build()
            self.write(saved, "requests", f"{name}.json")
        return saved


def _log(module, event, **fields):
    module.log_event(event, stage="batch", **fields)


def _flag(module, chapter, kind, detail):
    add_review_item(module.GLOSSARY_FILE.parent, chapter=chapter, kind=kind, detail=detail)
    _log(module, "flagged_for_review", chapter=chapter, kind=kind)


def _journal_dir(module):
    return Path(module.PAID_REQUEST_JOURNAL_DIR)


def _slot(module, batch, kind, chapter):
    """Batch helper requests get slots named after their batch, so a later batch never collides."""
    return paid_request_journal_path(_journal_dir(module), f"{kind}_b{batch.start:04d}", chapter)


def _translation_slot(module, chapter):
    return paid_request_journal_path(_journal_dir(module), "chapter_translation", chapter)


def _recorded(path):
    record = _read(path)
    return record is not None and record.get("status") == "response_recorded"


def _bound(module, logical, kwargs):
    """Worst-case cost the budget will reserve for this request while it is in flight."""
    from production_goat_runtime import normalize_production_request
    runtime = module._production_goat_runtime
    wire = normalize_production_request(logical, kwargs)
    started = runtime["clock"]().astimezone(timezone.utc).isoformat()
    return runtime["budget"].request_bound(wire, started)["credit_equivalent_upper"]


def _send_all(module, batch, jobs, *, logical, first_must_work=None, warm_up=True,
                head_start=WARM_UP_HEAD_START_SECONDS, limit=None):
    """Send jobs (chapter, kind, kwargs, path) through a rolling pool.

    Up to max_parallel requests are in flight; the next starts as soon as any one
    finishes, so a slow chapter holds up only itself. The first job goes alone to warm
    the prompt cache. The cache fills while the provider reads the prompt, so the rest
    start after `head_start` seconds, or when it finishes. With first_must_work, they
    wait for the warm-up answer, and if first_must_work(chapter, envelope) raises
    BatchResponseError, nothing else is sent. A request starts only when the worst
    case of everything in flight still fits the budget.

    Returns ({chapter: envelope or BatchChapterFailed}, stop). An answer that came back
    unusable fails only its chapter. Anything else (budget, provider stop, an
    unanswered request) is returned as `stop` once the requests in flight have
    finished; the caller records the finished answers, then raises it.
    """
    budget = module._production_goat_runtime["budget"]
    limit = limit or batch.manifest["max_parallel"]
    results, stop = {}, None

    with _AHEAD_LOCK:
        slots = _SLOTS.setdefault(_run_key(module), threading.BoundedSemaphore(batch.manifest["max_parallel"]))

    def send(job):
        number, kind, kwargs, path = job
        source = module.chapter_map[number]["text"]
        try:
            with slots:
                return number, module.journaled_chat_request(kind, number, source, kwargs, journal_path=path)["response"]
        except Exception as exc:
            if getattr(exc, "response_rejected", False) or _recorded(path):
                record = _read(path) or {}
                return number, BatchChapterFailed(f"{type(exc).__name__}: {exc}",
                                                  stats=(record.get("response") or {}).get("stats"))
            return number, exc

    def collect(done):
        nonlocal stop
        for future in done:
            running.pop(future)
            number, outcome = future.result()
            if isinstance(outcome, BaseException) and not isinstance(outcome, BatchChapterFailed):
                stop = stop or outcome
            else:
                results[number] = outcome

    pending = deque(jobs)
    running = {}  # future -> worst-case cost held for it
    with ThreadPoolExecutor(max_workers=limit) as pool:
        def launch(job):
            running[pool.submit(send, job)] = _bound(module, logical, job[2])

        if warm_up and pending:
            launch(pending.popleft())
            if first_must_work is not None:
                collect(wait(running).done)
                if stop is not None:
                    return results, stop
                (number, outcome), = results.items()
                try:
                    if isinstance(outcome, BatchChapterFailed):
                        raise bt.BatchResponseError(str(outcome))
                    first_must_work(number, outcome)
                except bt.BatchResponseError:
                    return results, stop
            else:
                collect(wait(running, timeout=head_start, return_when=FIRST_COMPLETED).done)
        while pending or running:
            while pending and stop is None and len(running) < limit:
                bound = _bound(module, logical, pending[0][2])
                room = budget.max_cost_usd - budget.actual_cost_usd - sum(running.values())
                if getattr(_ahead, "active", False):
                    room -= AHEAD_HEADROOM_USD
                if running and bound > room:
                    break  # Wait for a request in flight to finish and free its share.
                launch(pending.popleft())
            if not running:
                break
            collect(wait(running, return_when=FIRST_COMPLETED).done)
    return results, stop


_ahead = threading.local()  # Set in the thread that prepares the next batch.
_AHEAD_RUNS = {}  # data dir -> {first chapter: {"start", "thread", "error"}}
_AHEAD_STOPPING = set()  # data dirs whose run is ending: no new batch is started
_AHEAD_LOCK = threading.RLock()
_SLOTS = {}  # data dir -> semaphore: one request limit shared by every batch in flight


def _run_key(module):
    """One novel's entry in the shared background state: its data folder, as a full path."""
    glossary_file = getattr(module, "GLOSSARY_FILE", None)
    return Path(glossary_file).parent.resolve() if glossary_file is not None else id(module)


def _account(module, batch, operation_id, stats):
    """Helper requests count toward the novel's cost now, not at a chapter commit.

    progress.json is the commit marker, so only the main thread writes it: a batch
    prepared ahead records its costs and the main thread accounts them later.
    """
    if getattr(_ahead, "active", False):
        deferred = batch.manifest.setdefault("deferred_accounting", [])
        if all(entry["operation_id"] != operation_id for entry in deferred):
            deferred.append({"operation_id": operation_id, "stats": stats})
            batch.save()
        return
    module.account_residual_cjk_micro_repair(operation_id, stats)


def _account_deferred(module, batch):
    for entry in batch.manifest.pop("deferred_accounting", []):
        module.account_residual_cjk_micro_repair(entry["operation_id"], entry["stats"])
    batch.save()


def _previous_heading(module, batch):
    """The heading before the batch: committed, or settled by the batch prepared before it."""
    saved = batch.manifest.get("previous_heading")
    return tuple(saved) if saved else module.previous_chapter_heading(batch.start)


def _base_entries(module, batch):
    """The glossary a batch is built from, saved when it starts so every request uses the same one."""
    saved = batch.load("base_glossary.json")
    return saved if saved is not None else copy.deepcopy(module.glossary["entries"])


def _rendered_glossary(module, number, entries):
    source = module.chapter_map[number]["text"]
    return module.glossary_to_text(module.relevant_glossary_for_entries(source, entries), chapter_number=number)


def _first_unusable_chapter(module, start, end):
    """A batch must not cover a chapter that already has its own paid translation."""
    for number in range(start, end + 1):
        if ((module.PENDING_DIR / f"{number:04d}.md").exists() or (module.OUTPUT_DIR / f"{number:04d}.md").exists()
                or _translation_slot(module, number).exists()):
            return number
    return None


def prepare_batch_if_due(module, chapter_number):
    """Run or resume the paid batch phases so chapter_number can commit from a batch.

    Returns the batch, or None when this chapter takes the sequential path.
    """
    module = _module(module)
    config = getattr(module.ACTIVE_NOVEL_CONFIG, "batch_mode", None)  # None for the old repository layout
    data_dir = module.GLOSSARY_FILE.parent
    if config is not None and config.enabled and config.terms_only:
        _settle_terms_only(module, chapter_number, config)
        raise BatchTermsOnlyStop("terms-only rehearsal: batch terms settled; nothing is translated")
    _join_ahead(module, chapter_number)
    for manifest in active_manifests(data_dir):
        _finish_if_committed(module, Batch(data_dir, manifest["start"]))
    stale = [item for item in active_manifests(data_dir) if item["start"] < chapter_number and item["end"] < chapter_number]
    if stale:
        raise RuntimeError(f"unfinished batch {stale[0]['start']}-{stale[0]['end']} lies before chapter {chapter_number}")
    manifest = manifest_for(data_dir, chapter_number)
    if manifest is not None:
        batch = Batch(data_dir, manifest["start"])
        if batch.manifest.get("deferred_accounting"):
            _account_deferred(module, batch)
        if batch.manifest["status"] == PREPARING:
            if getattr(module, "_production_goat_runtime", None) is None:
                raise RuntimeError("an unfinished translation batch needs the Command Code provider to finish")
            _prepare(module, batch)
    else:
        if config is None or not config.enabled or getattr(module, "_production_goat_runtime", None) is None:
            return None
        batch = Batch(data_dir, chapter_number)
        if batch.manifest is not None:
            return None  # This start was abandoned before; the chapter runs sequentially.
        if not _start_batch(module, batch, config):
            return None
        _prepare(module, batch)
    if batch.manifest["status"] != COMMITTING:
        return None
    if config is not None and config.enabled and config.overlap:
        _start_ahead(module, batch, config)
    return batch


def _start_ahead(module, batch, config):
    """Prepare the batch after this one in the background.

    It needs only this batch's audited terms, end-of-batch memory and last heading,
    so it starts as soon as this batch's term audit is done, while this batch is
    still translating. Each batch prepared ahead starts the next one the same way,
    so reading and settling one batch overlaps translating the one before it.
    """
    data_dir, key = module.GLOSSARY_FILE.parent, _run_key(module)
    start = batch.manifest["end"] + 1
    with _AHEAD_LOCK:
        runs = _AHEAD_RUNS.get(key, {})
        if (key in _AHEAD_STOPPING or start in runs or start > module.FINAL_CHAPTER
                or (_trial_last_chapter(module) is not None and start > _trial_last_chapter(module))):
            return
        following = Batch(data_dir, start)
        if following.manifest is None:
            if len(active_manifests(data_dir)) >= 3:
                return  # Saving has fallen behind; the batch starts when its chapters come due.
            if not _start_batch(module, following, config, previous=batch):
                return
        elif following.manifest["status"] != PREPARING:
            return
        run = {"start": start, "error": None}

        def prepare():
            _ahead.active = True
            try:
                _prepare(module, Batch(data_dir, start))
            except BaseException as exc:  # Raised in the main thread when this batch is needed.
                run["error"] = exc
            finally:
                _ahead.active = False

        run["thread"] = threading.Thread(target=prepare, name=f"batch-{start:04d}", daemon=False)
        _AHEAD_RUNS.setdefault(key, {})[start] = run
    run["thread"].start()
    _log(module, "batch_ahead_started", chapter=start)


def _join_ahead(module, chapter_number=None):
    """Wait for the batches prepared ahead whose chapters are due, or all of them when the run ends.

    The first error, if any, is raised here, in the main thread.
    """
    key = _run_key(module)
    error = None
    while True:
        with _AHEAD_LOCK:
            runs = _AHEAD_RUNS.get(key, {})
            due = sorted(start for start in runs if chapter_number is None or start <= chapter_number)
            if not due:
                if not runs:
                    _AHEAD_RUNS.pop(key, None)
                break
            run = runs[due[0]]
        run["thread"].join()
        with _AHEAD_LOCK:
            runs.pop(run["start"], None)
        error = error or run["error"]
    if error is not None:
        raise error


def finish_preparation_ahead(module):
    """At the end of a run: start no new batch, and let those in flight finish, so no request is left unanswered."""
    if not any(_AHEAD_RUNS.values()):
        return
    module = _module(module)
    key = _run_key(module)
    with _AHEAD_LOCK:
        _AHEAD_STOPPING.add(key)
    try:
        _join_ahead(module)
    except Exception as exc:
        _log(module, "batch_ahead_failed", error_type=type(exc).__name__)
    finally:
        with _AHEAD_LOCK:
            _AHEAD_STOPPING.discard(key)


def _settle_terms_only(module, chapter_number, config):
    """Pre-pass and consolidation only, resumable; a restart sends nothing more."""
    if getattr(module, "_production_goat_runtime", None) is None:
        raise RuntimeError("a terms-only batch rehearsal needs the Command Code provider")
    batch = Batch(module.GLOSSARY_FILE.parent, chapter_number)
    if batch.manifest is None and not _start_batch(module, batch, config):
        return
    if batch.manifest["status"] != PREPARING:
        return
    _timed(batch, "prepass", lambda: _run_prepass(module, batch))
    if batch.manifest["status"] == ABANDONED:
        return
    if batch.load("settled.json") is None:
        _timed(batch, "consolidation", lambda: _run_consolidation(module, batch))
    if "term_audit" not in batch.load("settled.json"):
        _timed(batch, "term_audit", lambda: _run_term_audit(module, batch))
    batch.manifest["status"] = TERMS_ONLY
    batch.save()
    _log(module, "batch_terms_settled", chapter=batch.start, end_chapter=batch.manifest["end"],
         timings=batch.manifest["timings"])


def _trial_last_chapter(module):
    trial = _read(module.GLOSSARY_FILE.parent / "trial_stop.json")
    return trial["after_chapter"] if trial else None


def batch_end(start, size, audit_interval, last):
    """The last chapter of a batch that starts at `start`.

    A batch ends on a periodic-audit boundary, so each audit covers whole batches and
    the next batch starts from audited terms. When the batch starts on a boundary, it
    ends on the last one in its range if that still leaves half a batch. When it starts
    between boundaries (the novel was translated one by one up to there, or a chapter
    with its own paid translation came first), it ends on the next one even if that is
    short, so the batches after it line up; a boundary chapter on its own takes the
    sequential path (end == start). A few chapters left after the batch join it.
    """
    end = min(start + size - 1, last)
    if isinstance(audit_interval, int) and audit_interval > 0:
        boundary = end // audit_interval * audit_interval
        aligned = (start - 1) % audit_interval == 0
        if start <= boundary < end and (not aligned or boundary - start + 1 >= max(2, size // 2)):
            end = boundary
    if end < last and last - end < min(SHORT_TAIL_CHAPTERS, max(2, size // 4)):
        end = last
    return end


def _start_batch(module, batch, config, previous=None):
    """Write a new batch's manifest; False when the range is too short for a batch.

    A batch is built from a fixed snapshot of the glossary. Prepared ahead, after
    `previous`, it starts from that batch's settled terms, end-of-batch memory and
    last heading, all known before the previous batch's chapters are saved.
    """
    chapter_number = int(batch.dir.name)
    last = min(module.FINAL_CHAPTER, _trial_last_chapter(module) or 10**9)
    end = batch_end(chapter_number, config.chapters, getattr(module, "AUDIT_INTERVAL", None), last)
    blocked = _first_unusable_chapter(module, chapter_number, end)
    if blocked is not None:
        end = blocked - 1
    if end <= chapter_number:
        return False  # A batch of one is just the sequential path.
    entries = copy.deepcopy(module.glossary["entries"])
    manifest = {"version": VERSION, "start": chapter_number, "end": end, "status": PREPARING,
                "max_parallel": config.max_parallel, "base_continuity": module.continuity.get("summary", ""),
                "created_utc": _now(), "timings": {}, "chapters": {}}
    if previous is not None:
        settled, headings, _ = _settled(previous)
        entries = bt.glossary_entries_for_translation(entries, settled)
        last = previous.manifest["end"]
        source_heading = _source_heading(module, last)
        manifest.update(ahead_of=previous.start,
                        base_continuity=timeline(module, previous)["after"][str(last)],
                        previous_heading=[source_heading, headings[last]] if source_heading and last in headings else None,
                        previous_tail=bt.source_tail(module.chapter_map[last]["text"]))
        batch.write(settled, "inherited.json")
    batch.write(entries, "base_glossary.json")
    batch.manifest = manifest
    batch.save()
    _log(module, "batch_start", chapter=chapter_number, end_chapter=end, ahead=previous is not None)
    return True


def _timed(batch, phase, function):
    started = time.perf_counter()
    function()
    timings = batch.manifest["timings"]
    timings[phase] = round(timings.get(phase, 0.0) + time.perf_counter() - started, 3)
    batch.save()


def _prepare(module, batch):
    _timed(batch, "prepass", lambda: _run_prepass(module, batch))
    if batch.manifest["status"] == ABANDONED:
        return
    if batch.load("settled.json") is None:
        _timed(batch, "consolidation", lambda: _run_consolidation(module, batch))
    if "term_audit" not in batch.load("settled.json"):
        _timed(batch, "term_audit", lambda: _run_term_audit(module, batch))
    config = getattr(module.ACTIVE_NOVEL_CONFIG, "batch_mode", None)
    if config is not None and config.overlap and not config.terms_only:
        _start_ahead(module, batch, config)
    _timed(batch, "translation", lambda: _run_translation(module, batch))
    if not batch.manifest.get("checked"):
        _timed(batch, "checks", lambda: _run_checks(module, batch))
    batch.manifest["status"] = COMMITTING
    batch.save()
    _log(module, "batch_ready", chapter=batch.start, end_chapter=batch.manifest["end"],
         timings=batch.manifest["timings"])


def _run_prepass(module, batch):
    existing = _base_entries(module, batch)
    policy = _state_policy(module)
    jobs = []
    for number in batch.chapters:
        if batch.load("sheets", f"{number:04d}.json") is not None:
            continue
        kwargs = batch.saved_request(f"prepass_{number:04d}", lambda number=number: bt.build_prepass_request(
            module.chapter_map[number], model=module.MODEL, policy=policy,
            established_glossary=_rendered_glossary(module, number, existing)))
        jobs.append((number, bt.PREPASS_KIND, kwargs, _slot(module, batch, bt.PREPASS_KIND, number)))
    if not jobs:
        return
    results, stop = _send_all(module, batch, jobs, logical=bt.PREPASS_KIND, first_must_work=lambda number, outcome:
                                bt.parse_prepass(outcome["content"], number, module.chapter_map[number]["text"]))
    failed = []
    for number, outcome in sorted(results.items()):
        stats = outcome.stats if isinstance(outcome, BatchChapterFailed) else outcome["stats"]
        if stats:
            _account(module, batch, f"{bt.PREPASS_KIND}:{batch.start}:{number}", stats)
        if isinstance(outcome, BatchChapterFailed):
            failed.append((number, str(outcome)))
            continue
        try:
            sheet = bt.parse_prepass(outcome["content"], number, module.chapter_map[number]["text"])
        except bt.BatchResponseError as exc:
            failed.append((number, str(exc)))
            continue
        batch.write(sheet, "sheets", f"{number:04d}.json")
    if stop is not None:
        raise stop
    if failed:
        first, reason = failed[0]
        _flag(module, first, "batch_prepass", reason)
        if first == batch.start:
            batch.manifest["status"] = ABANDONED
            _log(module, "batch_abandoned", chapter=first, reason=reason)
        else:
            # Later chapters' source notes would miss this chapter, so the batch stops before it.
            batch.manifest["end"] = first - 1
            _log(module, "batch_shortened", chapter=first, end_chapter=first - 1, reason=reason)


def _state_policy(module):
    from translation_profile import profile_state_instructions
    return profile_state_instructions(module.ACTIVE_PROFILE, module.ACTIVE_NOVEL_POLICY)


def _review_policy(module):
    from translation_profile import profile_glossary_review_instructions
    return profile_glossary_review_instructions(module.ACTIVE_PROFILE, module.ACTIVE_NOVEL_POLICY)


def _sheets(batch):
    return {number: batch.load("sheets", f"{number:04d}.json") for number in batch.chapters}


def _source_heading(module, number):
    return (module.chapter_map.get(number, {}).get("heading") or "").strip()


def _run_consolidation(module, batch):
    existing = _base_entries(module, batch)
    sheets = _sheets(batch)
    sources = {number: module.chapter_map[number]["text"] for number in batch.chapters}
    groups = bt.consolidate_candidates(sheets, sources, existing)
    changes = bt.ordered_changes(sheets)
    endings = bt.shared_endings(groups, existing)
    batch_text = "\n".join(sources.values())
    established = module.glossary_to_text(module.relevant_glossary_for_entries(batch_text, existing),
                                          chapter_number=batch.start)
    headings = [(number, _source_heading(module, number)) for number in batch.chapters if _source_heading(module, number)]
    base = batch.manifest["base_continuity"]
    _, local_after = bt.build_timeline(base, sheets, batch.chapters)
    chunks = [groups[index:index + bt.DECISION_CHUNK] for index in range(0, len(groups), bt.DECISION_CHUNK)] or [[]]
    jobs = []
    for index, chunk in enumerate(chunks):
        first = index == 0
        kwargs = batch.saved_request(f"decision_{index:02d}", lambda chunk=chunk, first=first: bt.build_decision_request(
            chunk, model=module.MODEL, policy=_review_policy(module), established_glossary=established,
            changes=changes, endings=endings, headings=headings if first else None,
            last_heading=_previous_heading(module, batch) if first else None,
            prior_continuity=base if first else None, chapter_notes=local_after[batch.manifest["end"]] if first else None))
        jobs.append((batch.start + index, bt.DECISION_KIND, kwargs, _slot(module, batch, bt.DECISION_KIND, batch.start + index)))
    # Each decision chunk has its own prompt, so a warm-up would only add a wait.
    results, stop = _send_all(module, batch, jobs, logical=bt.DECISION_KIND, warm_up=False)
    if stop is not None:
        raise stop
    decisions, decided_headings, continuity = {}, {}, ""
    for index, chunk in enumerate(chunks):
        outcome = results[batch.start + index]
        stats = outcome.stats if isinstance(outcome, BatchChapterFailed) else outcome["stats"]
        if stats:
            _account(module, batch, f"{bt.DECISION_KIND}:{batch.start}:{index}", stats)
        try:
            if isinstance(outcome, BatchChapterFailed):
                raise bt.BatchResponseError(str(outcome))
            parsed = bt.parse_decision(outcome["content"], chunk, [number for number, _ in headings] if index == 0 else ())
        except bt.BatchResponseError as exc:
            # Proposals are still usable; the most common one stands for every key in the chunk.
            _flag(module, batch.start, "batch_glossary_decision", f"Decision {index} unusable ({exc}); proposals used.")
            continue
        decisions.update(parsed["terms"])
        decided_headings.update(parsed["headings"])
        continuity = continuity or parsed["continuity"]
    settled = bt.settle_terms(groups, decisions, existing, format_name=lambda category, english: (
        module.format_technique_name(english) if module.ACTIVE_PROFILE.requires_formal_capitalization(category)
        else english))
    by_chapter = {}
    for key, entry in bt.flagged_terms(settled):
        by_chapter.setdefault(entry["first_chapter"], []).append(
            f"{key} → {entry['english'] or ' / '.join(entry['approved_renderings'])} ({entry['confidence']}"
            + (", proposals disagreed" if entry["disputed"] else "") + ")")
    for number, lines in sorted(by_chapter.items()):
        _flag(module, number, "batch_term", "Settled for the whole batch; please check: " + "; ".join(lines))
    shared = {}
    for conflict in bt.shared_name_conflicts(settled, existing):
        shared.setdefault(conflict["chapter"], []).append(conflict["detail"])
    for number, lines in sorted(shared.items()):
        _flag(module, number, "batch_term_shared", "Shared name parts rendered differently: " + "; ".join(lines))
    batch.write({"settled": settled, "headings": {str(key): value for key, value in decided_headings.items()},
                 "continuity_after": continuity, "candidates": len(groups)}, "settled.json")


def _run_term_audit(module, batch):
    """Audit the settled terms before any chapter is translated with them.

    The periodic audit's prompt and source-backed targets, its chunks sent together.
    Its corrections, rejections and merges change the settled terms here, so a wrong
    rendering never reaches the batch's chapters. A chunk that fails leaves its terms
    as settled and is flagged; the periodic audit still runs after the chapters are saved.
    """
    from glossary_integrity import build_source_integrity_audit_plan
    value = batch.load("settled.json")
    settled = value["settled"]
    base = _base_entries(module, batch)
    combined = bt.glossary_entries_for_translation(base, settled)
    plan = build_source_integrity_audit_plan(combined, module.chapters, batch.start, batch.manifest["end"],
        max_contexts=module.MAX_TERM_CONTEXTS, context_radius=module.TERM_CONTEXT_RADIUS)
    targets = [target for target in plan["candidates"] if target["chinese"] in settled]
    index = module.compact_glossary_index(allowed_source_keys=plan["safe_glossary_keys"], glossary_data={"entries": combined})
    chunks = [targets[at:at + bt.TERM_AUDIT_CHUNK] for at in range(0, len(targets), bt.TERM_AUDIT_CHUNK)]
    batch_source = "\n".join(module.chapter_map[number]["text"] for number in batch.chapters)
    inside = set()
    for target in targets:
        as_word, inside_count = bt.word_occurrences(target["chinese"], batch_source)
        if inside_count and inside_count >= bt.INSIDE_WORDS_SHARE * (as_word + inside_count):
            inside.add(target["chinese"])
    jobs, maps = [], {}
    for number, chunk in enumerate(chunks, 1):
        request, ids = bt.build_term_audit_request(chunk, model=module.MODEL, system_prompt=module.AUDIT_MODEL_PROMPT,
            glossary_index=index, start=batch.start, end=batch.manifest["end"], chunk_index=number, total_chunks=len(chunks),
            inside_words=inside)
        kwargs = batch.saved_request(f"term_audit_{number:02d}", lambda request=request: request)
        maps[batch.start + number - 1] = ids
        jobs.append((batch.start + number - 1, bt.TERM_AUDIT_KIND, kwargs,
                     _slot(module, batch, bt.TERM_AUDIT_KIND, batch.start + number - 1)))
    # Chunks are independent: each has the same glossary index and its own targets.
    results, stop = _send_all(module, batch, jobs, logical=bt.TERM_AUDIT_KIND, warm_up=False) if jobs else ({}, None)
    if stop is not None:
        raise stop
    decisions = {}
    for slot, outcome in sorted(results.items()):
        stats = outcome.stats if isinstance(outcome, BatchChapterFailed) else outcome["stats"]
        if stats:
            _account(module, batch, f"{bt.TERM_AUDIT_KIND}:{batch.start}:{slot - batch.start}", stats)
        try:
            if isinstance(outcome, BatchChapterFailed):
                raise bt.BatchResponseError(str(outcome))
            decisions.update(bt.parse_term_audit(outcome["content"], maps[slot]))
        except bt.BatchResponseError as exc:
            _flag(module, batch.start, "batch_term_audit_chunk", f"Audit chunk {slot - batch.start + 1} unusable ({exc}); its terms stay as settled.")
    audited, changes = bt.apply_term_audit(settled, decisions, base, format_name=lambda category, english: (
        module.format_technique_name(english) if module.ACTIVE_PROFILE.requires_formal_capitalization(category)
        else english))
    if changes:
        _flag(module, batch.start, "batch_term_audit", "Changed before translation: " + "; ".join(changes)[:480])
    _log(module, "batch_term_audit", chapter=batch.start, targets=len(targets), changes=len(changes))
    batch.write({**value, "settled": audited, "term_audit": {"targets": len(targets), "changes": changes}},
                "settled.json")


def _settled(batch):
    value = batch.load("settled.json")
    return value["settled"], {int(key): text for key, text in value["headings"].items()}, value["continuity_after"]


def timeline(module, batch):
    """Context packets and the memory after each chapter; local and deterministic."""
    saved = batch.load("timeline.json")
    if saved is None:
        _, _, continuity_after = _settled(batch)
        packets, after = bt.build_timeline(batch.manifest["base_continuity"], _sheets(batch), batch.chapters)
        if continuity_after:
            after[batch.manifest["end"]] = continuity_after
        saved = {"packets": {str(key): value for key, value in packets.items()},
                 "after": {str(key): value for key, value in after.items()}}
        batch.write(saved, "timeline.json")
    return saved


def _translation_kwargs(module, batch, number):
    def build():
        settled, headings, _ = _settled(batch)
        entries = bt.glossary_entries_for_translation(_base_entries(module, batch), settled)
        chapter = module.chapter_map[number]
        used = module.relevant_glossary_for_entries(chapter["text"], entries)
        batch.write({item["chinese"]: item for item in used}, "glossary_used", f"{number:04d}.json")
        if number == batch.start:
            previous_heading, tail = _previous_heading(module, batch), batch.manifest.get("previous_tail")
        else:
            heading = headings.get(number - 1)
            source_heading = _source_heading(module, number - 1)
            previous_heading = (source_heading, heading) if heading and source_heading else None
            tail = bt.source_tail(module.chapter_map[number - 1]["text"])
        return build_translation_request(chapter=chapter, config=module.ACTIVE_NOVEL_CONFIG,
            profile=module.ACTIVE_PROFILE, system_prompt=module.system_prompt,
            rendered_glossary=module.glossary_to_text(used, chapter_number=number),
            continuity={"summary": timeline(module, batch)["packets"][str(number)]}, model=module.MODEL,
            controls=module.operation_request_controls("chapter_translation"),
            previous_heading=previous_heading, previous_source_tail=tail)
    return batch.saved_request(f"translation_{number:04d}", build)


def _run_translation(module, batch):
    jobs = []
    for number in batch.chapters:
        state = batch.chapter(number)
        if state.get("translation") in ("ok", "failed") and state.get("retranslation") != "due":
            continue
        jobs.append((number, "chapter_translation", _translation_kwargs(module, batch, number),
                     _translation_slot(module, number)))
    if not jobs:
        return
    results, stop = _send_all(module, batch, jobs, logical="translation")
    for number, outcome in sorted(results.items()):
        state = batch.chapter(number)
        if isinstance(outcome, BatchChapterFailed) or outcome["finish_reason"] != "stop" or not outcome["content"].strip():
            state["translation"] = "failed"
            state["translation_failure"] = str(outcome)[:300] if isinstance(outcome, BatchChapterFailed) else outcome["finish_reason"]
            continue
        state.update(translation="ok", translation_cost_usd=outcome["stats"]["cost_usd"])
        if state.get("retranslation") == "due":
            state["retranslation"] = "done"
        batch.write(outcome["content"], "translations", f"{number:04d}.md")
    batch.save()
    if stop is not None:
        raise stop


def _run_checks(module, batch):
    """Free local term checks; one more translation for a chapter that misses too much."""
    settled, _, _ = _settled(batch)
    retranslate = []
    for number in batch.chapters:
        state = batch.chapter(number)
        text = _translation_text(batch, number)
        if state.get("translation") != "ok" or text is None:
            continue
        source = module.chapter_map[number]["text"]
        misses = bt.term_misses(text, source, settled)
        text, repairs = bt.repair_variants(text, source, settled, misses)
        if repairs:
            batch.write(text, "translations", f"{number:04d}.md")
            state["repairs"] = state.get("repairs", []) + repairs
            misses = bt.term_misses(text, source, settled)
        state["misses"] = misses
        state["missing_dialogue"] = dialogue_gap(module, source, text)
        reason = ("batch_term_misses" if bt.needs_retranslation(misses, source, settled) else
                  "batch_missing_dialogue" if state["missing_dialogue"] >= bt.RETRANSLATE_DIALOGUE_LINES else None)
        if reason and state.get("retranslation") is None:
            retranslate.append((number, reason))
    for number, reason in retranslate:
        _set_aside_translation(module, batch, number, reason)
        batch.chapter(number)["retranslation"] = "due"
    retranslate = [number for number, _ in retranslate]
    batch.save()
    if retranslate:
        _run_translation(module, batch)
        for number in retranslate:
            text = _translation_text(batch, number)
            if text is not None:
                source = module.chapter_map[number]["text"]
                batch.chapter(number).update(misses=bt.term_misses(text, source, settled),
                                             missing_dialogue=dialogue_gap(module, source, text))
    for number in batch.chapters:
        misses = batch.chapter(number).get("misses")
        if misses:
            lines = [f"{key} → {' / '.join(bt.renderings(settled[key]))}" for key in misses]
            _flag(module, number, "batch_terms", "Settled terms not used: " + "; ".join(lines))
    batch.manifest["checked"] = True
    batch.save()


def dialogue_gap(module, source, text):
    """Lines of speech the English has fewer than the source; Korean only, as at commit."""
    if not (module.ACTIVE_NOVEL_CONFIG.source_language or "").lower().startswith("ko"):
        return 0
    return missing_dialogue_lines(source, text)


def _translation_text(batch, number):
    path = batch.path("translations", f"{number:04d}.md")
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _set_aside_translation(module, batch, number, reason):
    """Keep a paid translation's cost counted, then move its record so the chapter can be sent again."""
    path = _translation_slot(module, number)
    record = _read(path)
    if record is None:
        return
    if record.get("status") == "response_recorded":
        _account(module, batch, f"batch_superseded_translation:{batch.start}:{number}", record["response"]["stats"])
    destination = Path(module.BACKUP_DIR) / "superseded_translations" / f"batch_{batch.start:04d}" / f"{number:04d}_{int(time.time())}"
    destination.mkdir(parents=True, exist_ok=True)
    path.replace(destination / path.name)
    state = batch.chapter(number)
    state.pop("translation_cost_usd", None)
    state["set_aside"] = reason
    batch.save()
    _log(module, "batch_translation_set_aside", chapter=number, reason=reason)


def _audit_corrections(module, batch, number, text):
    """Apply glossary corrections made after this chapter was translated, source-anchored.

    A periodic audit may correct a term between the batch's translation and this
    commit. Sequential chapters would already use the correction. Only a corrected
    rendering is applied. A term merged into another as an alias takes the other's
    English, which is not a correction of this text (도리스 "Doris" merged into
    도리스 도트린 "Doris Dotrin" would turn every Doris into Doris Dotrin), and neither
    is an English that only adds words to the old one.
    """
    used = batch.load("glossary_used", f"{number:04d}.json", default={})
    source = module.chapter_map[number]["text"]
    for key, old in used.items():
        current = module.glossary["entries"].get(key)
        if (current is None or key not in source or old.get("status") == "contextual"
                or current.get("status") == "contextual"
                or (current.get("alias_of") or "") != (old.get("alias_of") or "")):
            continue
        before, after = old.get("english", ""), current.get("english", "")
        if not before or not after or before == after or before not in text or before.casefold() in after.casefold():
            continue
        if not module.retroactive_replacement_is_safe(before, after, current.get("category", "")):
            continue
        text = re.sub(r"(?<!\w)" + re.escape(before) + r"(?!\w)", after, text)
        _log(module, "batch_audit_correction", chapter=number, source_key=key, old_english=before, new_english=after)
    return text


@contextmanager
def batch_commit_scope(module, chapter_number):
    """While one batch chapter commits, answer its translation, state and review from the batch."""
    module = _module(module)
    data_dir = module.GLOSSARY_FILE.parent
    manifest = manifest_for(data_dir, chapter_number)
    if manifest is None or manifest["status"] != COMMITTING:
        yield
        return
    batch = Batch(data_dir, manifest["start"])
    settled, _, _ = _settled(batch)
    # A batch prepared ahead treated the previous batch's terms as established. One
    # the commit step left out of the glossary enters here, where this batch uses it.
    source = module.chapter_map[chapter_number]["text"]
    inherited = {key: {**entry, "first_chapter": chapter_number}
                 for key, entry in (batch.load("inherited.json") or {}).items()
                 if key not in module.glossary["entries"] and key in source and len(key) >= bt.MIN_KEY_CHARS}
    settled = {**inherited, **settled}
    after = timeline(module, batch)["after"][str(chapter_number)]
    sheet = batch.load("sheets", f"{chapter_number:04d}.json")
    names = ("translate_chapter", "extract_state", "review_new_terms", "relevant_glossary")
    originals = {name: getattr(module, name) for name in names}
    entries = lambda: bt.glossary_entries_for_translation(module.glossary["entries"], settled)

    def translate_chapter(chapter):
        number = chapter["chapter"]
        state = batch.chapter(number)
        if state.get("translation") == "ok":
            kwargs = _translation_kwargs(module, batch, number)
            started = time.perf_counter()
            journaled = module.journaled_chat_request("chapter_translation", number, chapter["text"], kwargs)
            response = journaled["response"]
            text = _audit_corrections(module, batch, number, _translation_text(batch, number) or response["content"])
            return TranslationStageResult(content=text, stats=copy.deepcopy(response["stats"]),
                elapsed_seconds=time.perf_counter() - started, finish_reason=response["finish_reason"],
                initial=journaled)
        # The batch could not translate this chapter: the sequential request, with the batch's terms.
        if not filter_refused_marker(module.PAID_REQUEST_JOURNAL_DIR.parent, number).exists():
            _set_aside_translation(module, batch, number, "batch_translation_failed")
        _flag(module, number, "batch_translation", state.get("translation_failure", "no batch translation"))
        module.relevant_glossary = lambda text: module.relevant_glossary_for_entries(text, entries())
        return originals["translate_chapter"](chapter)

    def extract_state(number, chinese_text, translation, *, prior_continuity=None):
        _log(module, "batch_state", chapter=number)
        return bt.state_for_chapter(number, settled, after, sheet), module.zero_stats()

    def review_new_terms(number, chinese_text, translation, candidates, *, recovery_record=None,
                         persist_recovery=lambda record: None):
        return (bt.review_for_candidates(candidates, settled, set(module.glossary["entries"]),
                lambda candidate: candidate.get("candidate_id") or stable_glossary_candidate_id(candidate["chinese"])),
                module.zero_stats())

    module.translate_chapter = translate_chapter
    module.extract_state = extract_state
    module.review_new_terms = review_new_terms
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)
    _finish_if_committed(module, batch)


def _finish_if_committed(module, batch):
    batch.manifest = _read(batch.manifest_path)
    if batch.manifest["status"] == COMMITTING and module.progress["last_completed"] >= batch.manifest["end"]:
        batch.manifest["status"] = COMPLETE
        batch.save()
        _log(module, "batch_complete", chapter=batch.start, end_chapter=batch.manifest["end"])
