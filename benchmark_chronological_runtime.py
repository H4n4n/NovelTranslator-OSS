"""Restart-safe chronological state-growth benchmark execution harness.

Importing this module is side-effect free: it reads no credentials, performs no
network activity, makes no provider calls, and writes no files.

The harness consumes the immutable Phase 59A canonical corpus. Two synchronous
arms (DeepSeek V4.1 Flash and GPT-5.6 Luna) independently grow model-authored
translation state chapter by chapter. A third arm replays the exact frozen Luna
synchronous requests through OpenAI Batch after the Luna lineage is complete.

Every paid boundary is explicit and fail-closed. Automatic retry is forbidden.
Transport ambiguity blocks the affected arm. Provider responses are durably
recorded before parsing. Production-owned novel state must remain byte-identical
across each synchronous request and Batch remote operation.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from benchmark_model_config import (
    benchmark_pricing_snapshot,
    economy_pricing_snapshot,
    resolve_benchmark_model,
)
from benchmark_novel import (
    BenchmarkError,
    BenchmarkSafetyError,
    capture_protected_state,
    create_benchmark_client,
)
from novel_project import load_novel_project
from noveltranslator_core import (
    atomic_write_json,
    calculate_cost_from_token_counts,
    residual_source_script_report,
)


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_KIND = "chronological-state-growth-runtime-v1"
PLAN_FILENAME = "run_plan.json"
RUN_JOURNAL_FILENAME = "run_journal.jsonl"

FLASH_ARM = "flash_sync"
LUNA_ARM = "luna_sync"
BATCH_ARM = "luna_batch_replay"
SYNC_ARMS = (FLASH_ARM, LUNA_ARM)
ALL_ARMS = (FLASH_ARM, LUNA_ARM, BATCH_ARM)

ARM_MODELS = {
    FLASH_ARM: "deepseek-v4.1-flash",
    LUNA_ARM: "gpt-5.6-luna",
}
EXPECTED_MODEL_CONFIG = {
    FLASH_ARM: {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "version": "DeepSeek-V4.1-Flash",
        "reasoning": "disabled",
        "max_tokens": 12000,
    },
    LUNA_ARM: {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "version": "gpt-5.6-luna",
        "reasoning": "none",
        "max_tokens": 12000,
    },
}

SYNC_EXECUTE_AUTHORIZATION = {
    FLASH_ARM: "AUTHORIZE_ONE_CHRONOLOGICAL_FLASH_SYNC_REQUEST",
    LUNA_ARM: "AUTHORIZE_ONE_CHRONOLOGICAL_LUNA_SYNC_REQUEST",
}
BATCH_SUBMIT_AUTHORIZATION = "AUTHORIZE_ONE_CHRONOLOGICAL_LUNA_BATCH_REPLAY"

BATCH_ENDPOINT = "/v1/chat/completions"
BATCH_COMPLETION_WINDOW = "24h"
BATCH_INPUT_FILENAME = "batch_input.jsonl"
BATCH_MANIFEST_FILENAME = "batch_manifest.json"
BATCH_JOURNAL_FILENAME = "batch_journal.jsonl"
BATCH_OUTPUT_FILENAME = "batch_output.jsonl"

DEFAULT_FLASH_BUDGET_USD = 0.75
DEFAULT_LUNA_BUDGET_USD = 0.75
DEFAULT_BATCH_BUDGET_USD = 0.75
MAX_CASES_PER_SYNC_ARM = 50
EXPECTED_NOVELS = ("great_king_zhuxia", "rogue_first_prince")
EXPECTED_CHAPTERS = tuple(range(1, 26))

MAX_STATE_BYTES = 256_000
MAX_GLOSSARY_UPSERTS_PER_CHAPTER = 64
MAX_GLOSSARY_ENTRIES = 1024
MAX_TERM_CHARS = 240
MAX_NOTE_CHARS = 1000
MAX_CONTINUITY_CHARS = 8000
MAX_ISSUES = 40
MAX_ISSUE_CHARS = 1200

STATE_PROTOCOL_VERSION = "chronological-state-protocol-v1"
STATE_PROTOCOL = r"""
CHRONOLOGICAL STATE-GROWTH EXPERIMENT

Translate the source faithfully into natural English while maintaining a small
translation state for future chapters.

The CURRENT EXPERIMENTAL STATE below was created only by earlier outputs from
this same arm. Treat it as continuity context, not as infallible truth. If an
earlier glossary choice is clearly wrong in the new context, correct it by
upserting the same source term with the better English rendering. Do not invent
facts that are not supported by the source.

Return exactly one JSON object and no surrounding commentary or Markdown fence:

{
  "translation": "complete English translation of this source unit",
  "state_update": {
    "glossary_upserts": [
      {
        "source": "source-language term",
        "english": "preferred English rendering",
        "category": "character/title/faction/technique/item/place/concept/other",
        "confidence": "high/medium/low",
        "note": "short reason or disambiguating context"
      }
    ],
    "continuity_summary": "compact current continuity useful for the next chapter",
    "issues": [
      "only unresolved ambiguities worth remembering for later chapters"
    ]
  }
}

Rules:
- The translation must be complete; do not summarize or omit content.
- Add glossary entries only when future consistency is genuinely useful.
- Keep source terms in their original source-language form.
- Reuse established glossary renderings unless new evidence justifies correction.
- The continuity summary should remain compact and current, not accumulate prose.
- Issues are unresolved questions only; remove issues once they are resolved.
- Never mention this experiment or the JSON protocol inside the translation.
""".strip()


class ChronologicalRuntimeError(BenchmarkError):
    pass


class ChronologicalUncertainError(ChronologicalRuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_sha256(value) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_tree_sha(root: Path) -> str:
    root = Path(root).resolve()
    rows = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: str(item.resolve()),
    ):
        relative = path.resolve().relative_to(root).as_posix()
        rows.append(f"{relative}\t{_sha256_file(path)}\t{path.stat().st_size}")
    return _sha256_text("\n".join(rows))


def _load_json(path: Path, label: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChronologicalRuntimeError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ChronologicalRuntimeError(f"{label} must be a JSON object")
    return value


def _write_exclusive(path: Path, payload: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise BenchmarkSafetyError(f"artifact already exists: {path}") from exc
    recorded = path.read_bytes()
    if recorded != payload:
        raise BenchmarkSafetyError(f"artifact byte verification failed: {path}")


def _write_json_exclusive(path: Path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _write_exclusive(path, payload)


def _write_text_exclusive(path: Path, text: str):
    _write_exclusive(path, text.encode("utf-8"))


def _append_journal(path: Path, event: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(_canonical_json_bytes(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _journal_events(path: Path):
    path = Path(path)
    if not path.is_file():
        return []
    result = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            value = json.loads(raw)
            result.append(value.get("event"))
    return result


def _git_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChronologicalRuntimeError(f"repository HEAD could not be resolved: {exc}") from exc
    value = completed.stdout.strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ChronologicalRuntimeError(f"repository HEAD is malformed: {value!r}")
    return value


def _ensure_repo_head(repo_root: Path, expected: str) -> str:
    expected = str(expected or "").strip().lower()
    actual = _git_head(repo_root)
    if actual != expected:
        raise BenchmarkSafetyError(f"repository HEAD drifted: {actual} != {expected}")
    return actual


def _ensure_external_run_root(repo_root: Path, canonical_root: Path, run_root: Path, exists: bool):
    repo_root = Path(repo_root).resolve()
    canonical_root = Path(canonical_root).resolve()
    run_root = Path(run_root)
    if not run_root.is_absolute():
        raise ChronologicalRuntimeError("run root must be an absolute external path")
    run_root = run_root.resolve()
    for protected in (repo_root, canonical_root):
        if run_root == protected or protected in run_root.parents:
            raise BenchmarkSafetyError("runtime output must be outside repository and canonical corpus")
    if exists and not run_root.is_dir():
        raise ChronologicalRuntimeError(f"runtime root does not exist: {run_root}")
    if not exists and run_root.exists():
        raise BenchmarkSafetyError(f"runtime root already exists: {run_root}")
    return run_root


def _model_snapshot(arm: str):
    config = resolve_benchmark_model(ARM_MODELS[arm])
    core = {
        "provider": config.provider,
        "model": config.model,
        "version": config.version,
        "reasoning": config.reasoning,
        "max_tokens": config.max_tokens,
    }
    if core != EXPECTED_MODEL_CONFIG[arm]:
        raise BenchmarkSafetyError(f"{arm} benchmark model configuration drifted: {core!r}")
    snapshot = {
        **core,
        "pricing_version": config.pricing_version,
        "prices": copy.deepcopy(config.prices),
        "economy_pricing_version": config.economy_pricing_version,
        "economy_prices": copy.deepcopy(config.economy_prices),
        "economy_mode": config.economy_mode,
    }
    return config, snapshot


def _protected_state(repo_root: Path):
    result = {}
    for novel_id in EXPECTED_NOVELS:
        project = load_novel_project(novel_id, repo_root)
        result[novel_id] = capture_protected_state(project)
    return result


def _ensure_protected_state(repo_root: Path, expected: dict, stage: str):
    actual = _protected_state(repo_root)
    if actual != expected:
        raise BenchmarkSafetyError(f"protected production state changed: {stage}")
    return actual


def _validate_canonical(
    canonical_root: Path,
    *,
    expected_plan_sha256: str,
    expected_identity_sha256: str,
    expected_tree_sha256: str,
):
    canonical_root = Path(canonical_root).resolve()
    plan_path = canonical_root / "chronological_state_plan.json"
    if not plan_path.is_file():
        raise ChronologicalRuntimeError(f"canonical plan is missing: {plan_path}")
    actual_plan_sha = _sha256_file(plan_path)
    if actual_plan_sha != str(expected_plan_sha256).lower():
        raise BenchmarkSafetyError(
            f"canonical plan SHA mismatch: {actual_plan_sha} != {expected_plan_sha256}"
        )
    actual_tree_sha = _external_tree_sha(canonical_root)
    if actual_tree_sha != str(expected_tree_sha256).lower():
        raise BenchmarkSafetyError(
            f"canonical tree SHA mismatch: {actual_tree_sha} != {expected_tree_sha256}"
        )
    plan = _load_json(plan_path, "canonical chronological plan")
    if plan.get("status") != "frozen" or plan.get("benchmark") != "chronological-state-growth-v1":
        raise ChronologicalRuntimeError("canonical plan is not the frozen Phase 59A corpus")
    if plan.get("identity_sha256") != str(expected_identity_sha256).lower():
        raise BenchmarkSafetyError("canonical identity SHA mismatch")
    novels = plan.get("novels")
    if not isinstance(novels, list) or [row.get("novel_id") for row in novels] != list(EXPECTED_NOVELS):
        raise ChronologicalRuntimeError("canonical novel order/identity is unexpected")
    for novel in novels:
        if tuple(novel.get("chapters") or ()) != EXPECTED_CHAPTERS:
            raise ChronologicalRuntimeError(
                f"{novel.get('novel_id')} canonical corpus is not exactly units 1-25"
            )
        prompt = canonical_root / novel["provider_visible_system_prompt"]["path"]
        state = canonical_root / novel["initial_state"]["path"]
        if _sha256_file(prompt) != novel["provider_visible_system_prompt"]["sha256"]:
            raise BenchmarkSafetyError(f"{novel['novel_id']} profile prompt hash drifted")
        state_value = _load_json(state, f"{novel['novel_id']} initial state")
        if _canonical_json_sha256(state_value) != novel["initial_state"]["identity_sha256"]:
            raise BenchmarkSafetyError(f"{novel['novel_id']} initial state identity drifted")
        by_number = {record["chapter_number"]: record for record in novel["chapter_records"]}
        if set(by_number) != set(EXPECTED_CHAPTERS):
            raise ChronologicalRuntimeError(f"{novel['novel_id']} canonical source rows are incomplete")
        for number in EXPECTED_CHAPTERS:
            record = by_number[number]
            source_path = canonical_root / record["source_snapshot_path"]
            if _sha256_file(source_path) != record["source_sha256"]:
                raise BenchmarkSafetyError(
                    f"{novel['novel_id']} Chapter {number} source hash drifted"
                )
    return plan, actual_plan_sha, actual_tree_sha


def _novel_record(plan: dict, novel_id: str):
    matches = [row for row in plan["novels"] if row["novel_id"] == novel_id]
    if len(matches) != 1:
        raise ChronologicalRuntimeError(f"canonical novel record missing/duplicated: {novel_id}")
    return matches[0]


def _case_id(novel_id: str, chapter: int) -> str:
    return f"{novel_id}-ch{chapter:04d}"


def _case_order():
    return [
        (novel_id, chapter)
        for chapter in EXPECTED_CHAPTERS
        for novel_id in EXPECTED_NOVELS
    ]


def _case_dir(run_root: Path, arm: str, novel_id: str, chapter: int):
    return Path(run_root) / "arms" / arm / "cases" / _case_id(novel_id, chapter)


def _initial_state_path(canonical_root: Path, canonical_plan: dict, novel_id: str):
    novel = _novel_record(canonical_plan, novel_id)
    return Path(canonical_root) / novel["initial_state"]["path"]


def _state_before_for_case(run_root, canonical_root, canonical_plan, arm, novel_id, chapter):
    if chapter == 1:
        path = _initial_state_path(canonical_root, canonical_plan, novel_id)
        value = _load_json(path, f"{novel_id} initial state")
        expected = _novel_record(canonical_plan, novel_id)["initial_state"]["identity_sha256"]
        if _canonical_json_sha256(value) != expected:
            raise BenchmarkSafetyError(f"{novel_id} initial state identity drifted")
        return value, expected, str(path)
    previous = _case_dir(run_root, arm, novel_id, chapter - 1)
    complete = _validate_sync_complete(
        run_root,
        canonical_root,
        canonical_plan,
        arm,
        novel_id,
        chapter - 1,
        recurse_state=False,
    )
    state_path = previous / "state_after.json"
    state = _load_json(state_path, f"{arm} {novel_id} state after Chapter {chapter - 1}")
    if _canonical_json_sha256(state) != complete["artifacts"]["state_after_identity_sha256"]:
        raise BenchmarkSafetyError("state-before identity no longer matches prior completion")
    return state, complete["artifacts"]["state_after_identity_sha256"], str(state_path)


def _state_prompt(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)


def _request_messages(canonical_root: Path, canonical_plan: dict, novel_id: str, chapter: int, state: dict):
    novel = _novel_record(canonical_plan, novel_id)
    prompt_path = Path(canonical_root) / novel["provider_visible_system_prompt"]["path"]
    profile_prompt = prompt_path.read_text(encoding="utf-8")
    record = next(row for row in novel["chapter_records"] if row["chapter_number"] == chapter)
    source_path = Path(canonical_root) / record["source_snapshot_path"]
    source_text = source_path.read_text(encoding="utf-8")
    system = profile_prompt.rstrip() + "\n\n" + STATE_PROTOCOL
    user = (
        "CURRENT EXPERIMENTAL STATE — USE FOR CONSISTENCY BUT CORRECT IT IF NEW "
        "SOURCE EVIDENCE REQUIRES:\n\n"
        + _state_prompt(state)
        + "\n\n"
        + f"{record['source_label']} — {record['source_heading']}:\n\n"
        + source_text
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], record


def _build_request(canonical_root, canonical_plan, arm, novel_id, chapter, state):
    config, snapshot = _model_snapshot(arm)
    messages, record = _request_messages(
        canonical_root, canonical_plan, novel_id, chapter, state
    )
    request = {"model": config.model, "messages": messages}
    if config.provider == "deepseek":
        request.update(
            {
                "max_tokens": config.max_tokens,
                "extra_body": {"thinking": {"type": config.reasoning}},
            }
        )
    else:
        request.update(
            {
                "max_completion_tokens": config.max_tokens,
                "reasoning_effort": config.reasoning,
            }
        )
    return request, record, config, snapshot


def _strip_json_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _bounded_string(value, label, *, allow_empty=False, limit=MAX_NOTE_CHARS):
    if not isinstance(value, str):
        raise ChronologicalRuntimeError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ChronologicalRuntimeError(f"{label} must not be empty")
    if len(value) > limit:
        raise ChronologicalRuntimeError(f"{label} exceeds {limit} characters")
    return value


def parse_model_document(text: str):
    raw = _strip_json_fence(text)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChronologicalRuntimeError(f"model response is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ChronologicalRuntimeError("model response must be a JSON object")
    translation = _bounded_string(
        document.get("translation"), "translation", limit=200_000
    )
    update = document.get("state_update")
    if not isinstance(update, dict):
        raise ChronologicalRuntimeError("state_update must be a JSON object")
    upserts = update.get("glossary_upserts")
    if not isinstance(upserts, list):
        raise ChronologicalRuntimeError("glossary_upserts must be a list")
    if len(upserts) > MAX_GLOSSARY_UPSERTS_PER_CHAPTER:
        raise ChronologicalRuntimeError("glossary_upserts exceeds per-chapter safety limit")
    normalized_upserts = []
    seen = set()
    for index, row in enumerate(upserts):
        if not isinstance(row, dict):
            raise ChronologicalRuntimeError(f"glossary_upserts[{index}] must be an object")
        source = _bounded_string(
            row.get("source"), f"glossary_upserts[{index}].source", limit=MAX_TERM_CHARS
        )
        english = _bounded_string(
            row.get("english"), f"glossary_upserts[{index}].english", limit=MAX_TERM_CHARS
        )
        category = _bounded_string(
            row.get("category", "other"),
            f"glossary_upserts[{index}].category",
            limit=80,
        )
        confidence = _bounded_string(
            row.get("confidence", "medium"),
            f"glossary_upserts[{index}].confidence",
            limit=40,
        )
        note = _bounded_string(
            row.get("note", ""),
            f"glossary_upserts[{index}].note",
            allow_empty=True,
            limit=MAX_NOTE_CHARS,
        )
        if source in seen:
            raise ChronologicalRuntimeError(
                f"glossary_upserts contains duplicate source term in one response: {source!r}"
            )
        seen.add(source)
        normalized_upserts.append(
            {
                "source": source,
                "english": english,
                "category": category,
                "confidence": confidence,
                "note": note,
            }
        )
    continuity = _bounded_string(
        update.get("continuity_summary", ""),
        "continuity_summary",
        allow_empty=True,
        limit=MAX_CONTINUITY_CHARS,
    )
    issues = update.get("issues")
    if not isinstance(issues, list):
        raise ChronologicalRuntimeError("issues must be a list")
    if len(issues) > MAX_ISSUES:
        raise ChronologicalRuntimeError("issues exceeds safety limit")
    normalized_issues = [
        _bounded_string(
            value,
            f"issues[{index}]",
            limit=MAX_ISSUE_CHARS,
        )
        for index, value in enumerate(issues)
    ]
    return {
        "translation": translation,
        "state_update": {
            "glossary_upserts": normalized_upserts,
            "continuity_summary": continuity,
            "issues": normalized_issues,
        },
    }


def apply_state_update(state_before: dict, model_document: dict, chapter: int):
    if state_before.get("through_chapter") != chapter - 1:
        raise BenchmarkSafetyError(
            f"state chain mismatch: expected through_chapter={chapter - 1}, "
            f"got {state_before.get('through_chapter')!r}"
        )
    before_identity = _canonical_json_sha256(state_before)
    update = model_document["state_update"]
    after = copy.deepcopy(state_before)
    entries = after.get("glossary_entries")
    if not isinstance(entries, dict):
        raise ChronologicalRuntimeError("state glossary_entries must be an object")
    added = 0
    revised = 0
    unchanged = 0
    for row in update["glossary_upserts"]:
        source = row["source"]
        existing = entries.get(source)
        new_entry = {
            "english": row["english"],
            "category": row["category"],
            "confidence": row["confidence"],
            "note": row["note"],
            "first_chapter": (
                existing.get("first_chapter", chapter)
                if isinstance(existing, dict)
                else chapter
            ),
            "last_chapter": chapter,
            "revision_count": (
                int(existing.get("revision_count", 0)) + 1
                if isinstance(existing, dict)
                else 0
            ),
        }
        if existing is None:
            added += 1
        elif all(
            existing.get(key) == new_entry.get(key)
            for key in ("english", "category", "confidence", "note")
        ):
            unchanged += 1
        else:
            revised += 1
        entries[source] = new_entry
    if len(entries) > MAX_GLOSSARY_ENTRIES:
        raise ChronologicalRuntimeError("dynamic glossary exceeds experiment safety limit")
    after["through_chapter"] = chapter
    after["continuity_summary"] = update["continuity_summary"]
    after["issues"] = list(update["issues"])
    after["parent_state_sha256"] = before_identity
    events = after.get("state_events")
    if not isinstance(events, list):
        raise ChronologicalRuntimeError("state_events must be a list")
    update_identity = _canonical_json_sha256(update)
    events.append(
        {
            "chapter": chapter,
            "state_update_sha256": update_identity,
            "glossary_upserts": len(update["glossary_upserts"]),
            "glossary_added": added,
            "glossary_revised": revised,
            "glossary_unchanged": unchanged,
        }
    )
    payload = _canonical_json_bytes(after)
    if len(payload) > MAX_STATE_BYTES:
        raise ChronologicalRuntimeError("dynamic state exceeds experiment byte safety limit")
    return after, {
        "state_before_identity_sha256": before_identity,
        "state_after_identity_sha256": _canonical_json_sha256(after),
        "state_update_identity_sha256": update_identity,
        "glossary_added": added,
        "glossary_revised": revised,
        "glossary_unchanged": unchanged,
        "glossary_entry_count_after": len(entries),
        "state_bytes_after": len(payload),
    }


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _provider_dict(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return copy.deepcopy(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "id": _field(value, "id"),
        "model": _field(value, "model"),
        "system_fingerprint": _field(value, "system_fingerprint"),
        "choices": _field(value, "choices", []),
        "usage": _field(value, "usage"),
    }


def _provider_identity(response):
    choices = _field(response, "choices", []) or []
    choice = choices[0] if choices else None
    return {
        "response_id": _field(response, "id"),
        "response_model": _field(response, "model"),
        "system_fingerprint": _field(response, "system_fingerprint"),
        "finish_reason": _field(choice, "finish_reason"),
    }


def _response_content(response):
    choices = _field(response, "choices", []) or []
    if len(choices) != 1:
        raise ChronologicalRuntimeError("provider response must contain exactly one choice")
    message = _field(choices[0], "message")
    content = _field(message, "content", "") if message is not None else ""
    return _bounded_string(content, "provider response content", limit=300_000)


def _usage_snapshot(response):
    usage = _field(response, "usage")
    prompt_tokens = int((_field(usage, "prompt_tokens", 0) if usage is not None else 0) or 0)
    completion_tokens = int(
        (_field(usage, "completion_tokens", 0) if usage is not None else 0) or 0
    )
    cache_hits = int(
        (_field(usage, "prompt_cache_hit_tokens", 0) if usage is not None else 0) or 0
    )
    cache_misses = int(
        (_field(usage, "prompt_cache_miss_tokens", 0) if usage is not None else 0) or 0
    )
    if not cache_hits and usage is not None:
        details = _field(usage, "prompt_tokens_details")
        cache_hits = int((_field(details, "cached_tokens", 0) if details is not None else 0) or 0)
    if not cache_misses and prompt_tokens:
        cache_misses = max(prompt_tokens - cache_hits, 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "total_tokens": int((_field(usage, "total_tokens", 0) if usage is not None else 0) or 0),
    }


def _pricing_result(config, request_started, usage: dict, *, batch=False):
    pricing = (
        economy_pricing_snapshot(config, request_started)
        if batch
        else benchmark_pricing_snapshot(config, request_started)
    )
    total, hits, misses = calculate_cost_from_token_counts(
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["cache_hit_tokens"],
        usage["cache_miss_tokens"],
        pricing,
    )
    return {
        "pricing": pricing,
        "cache_hit_tokens": hits,
        "cache_miss_tokens": misses,
        "total_cost_usd": total,
    }


def _response_from_dict(body: dict):
    return body


def _sync_complete_paths(case_dir: Path):
    return {
        "request": case_dir / "request.json",
        "response": case_dir / "response.json",
        "translation": case_dir / "translation.txt",
        "state_update": case_dir / "state_update.json",
        "state_after": case_dir / "state_after.json",
        "journal": case_dir / "journal.jsonl",
        "complete": case_dir / "complete.json",
        "uncertain": case_dir / "uncertain.json",
        "response_failed": case_dir / "response_failed.json",
    }


def _validate_sync_complete(
    run_root,
    canonical_root,
    canonical_plan,
    arm,
    novel_id,
    chapter,
    *,
    recurse_state=True,
):
    case_dir = _case_dir(run_root, arm, novel_id, chapter)
    paths = _sync_complete_paths(case_dir)
    if not paths["complete"].is_file():
        raise ChronologicalRuntimeError(f"complete artifact is missing: {paths['complete']}")
    complete = _load_json(paths["complete"], "sync complete")
    if complete.get("status") != "complete":
        raise BenchmarkSafetyError("sync complete artifact is not terminal complete")
    identity = complete.get("identity") or {}
    if identity.get("arm") != arm or identity.get("novel_id") != novel_id:
        raise BenchmarkSafetyError("sync complete arm/novel identity mismatch")
    if identity.get("chapter_number") != chapter:
        raise BenchmarkSafetyError("sync complete chapter identity mismatch")
    expected_model = EXPECTED_MODEL_CONFIG[arm]
    for key in ("provider", "model", "version", "reasoning", "max_tokens"):
        if identity.get(key) != expected_model[key]:
            raise BenchmarkSafetyError(f"sync complete model binding mismatch: {key}")
    novel = _novel_record(canonical_plan, novel_id)
    source_record = next(
        row for row in novel["chapter_records"] if row["chapter_number"] == chapter
    )
    if identity.get("source_sha256") != source_record["source_sha256"]:
        raise BenchmarkSafetyError("sync complete source binding mismatch")
    if recurse_state:
        state_before, state_before_sha, _ = _state_before_for_case(
            run_root, canonical_root, canonical_plan, arm, novel_id, chapter
        )
        if identity.get("state_before_identity_sha256") != state_before_sha:
            raise BenchmarkSafetyError("sync complete state-before binding mismatch")
    if complete.get("identity_sha256") != _canonical_json_sha256(identity):
        raise BenchmarkSafetyError("sync complete transaction identity hash mismatch")
    request_sha = _sha256_file(paths["request"])
    response_sha = _sha256_file(paths["response"])
    translation_sha = _sha256_file(paths["translation"])
    state_update_file_sha = _sha256_file(paths["state_update"])
    state_after_file_sha = _sha256_file(paths["state_after"])
    artifacts = complete.get("artifacts") or {}
    checks = {
        "request_sha256": request_sha,
        "response_sha256": response_sha,
        "translation_sha256": translation_sha,
        "state_update_file_sha256": state_update_file_sha,
        "state_after_file_sha256": state_after_file_sha,
    }
    for key, actual in checks.items():
        if artifacts.get(key) != actual:
            raise BenchmarkSafetyError(f"sync complete artifact hash mismatch: {key}")
    if identity.get("request_sha256") != request_sha:
        raise BenchmarkSafetyError("sync complete request identity/hash mismatch")
    raw_response = _load_json(paths["response"], "raw provider response")
    raw_provider = _provider_identity(raw_response)
    provider = complete.get("provider_response") or {}
    for key in ("response_id", "response_model", "system_fingerprint", "finish_reason"):
        if provider.get(key) != raw_provider.get(key):
            raise BenchmarkSafetyError(f"sync complete provider evidence mismatch: {key}")
    if provider.get("response_model") != expected_model["model"]:
        raise BenchmarkSafetyError("sync complete provider response model mismatch")
    if provider.get("finish_reason") != "stop":
        raise BenchmarkSafetyError("sync complete finish_reason mismatch")
    model_document = parse_model_document(_response_content(raw_response))
    translation = paths["translation"].read_text(encoding="utf-8")
    if translation != model_document["translation"]:
        raise BenchmarkSafetyError("sync complete translation no longer matches raw response")
    state_update = _load_json(paths["state_update"], "state update")
    if state_update != model_document["state_update"]:
        raise BenchmarkSafetyError("sync complete state update no longer matches raw response")
    if _canonical_json_sha256(state_update) != artifacts.get(
        "state_update_identity_sha256"
    ):
        raise BenchmarkSafetyError("sync complete state-update identity mismatch")
    state_after = _load_json(paths["state_after"], "state after")
    if _canonical_json_sha256(state_after) != artifacts.get("state_after_identity_sha256"):
        raise BenchmarkSafetyError("sync complete state-after identity mismatch")
    if state_after.get("through_chapter") != chapter:
        raise BenchmarkSafetyError("sync complete state-after chapter mismatch")
    if recurse_state:
        expected_after, expected_metrics = apply_state_update(
            state_before, model_document, chapter
        )
        if state_after != expected_after:
            raise BenchmarkSafetyError("sync complete deterministic state transition mismatch")
        if artifacts.get("state_after_identity_sha256") != expected_metrics[
            "state_after_identity_sha256"
        ]:
            raise BenchmarkSafetyError("sync complete deterministic state identity mismatch")
    safety = complete.get("safety") or {}
    if safety.get("provider_calls_attempted") != 1 or safety.get("paid_requests_attempted") != 1:
        raise BenchmarkSafetyError("sync complete paid-call accounting mismatch")
    if safety.get("provider_response_recorded") is not True:
        raise BenchmarkSafetyError("sync complete provider-response durability proof missing")
    if safety.get("protected_state_verified_unchanged") is not True:
        raise BenchmarkSafetyError("sync complete protected-state proof missing")
    if _journal_events(paths["journal"]) != ["planned", "started", "response_recorded", "complete"]:
        raise BenchmarkSafetyError("sync complete journal sequence mismatch")
    return complete


def _case_status(run_root, canonical_root, canonical_plan, arm, novel_id, chapter):
    case_dir = _case_dir(run_root, arm, novel_id, chapter)
    paths = _sync_complete_paths(case_dir)
    if paths["uncertain"].is_file():
        return "uncertain"
    if paths["response_failed"].is_file():
        return "response_failed"
    if paths["complete"].is_file():
        try:
            _validate_sync_complete(
                run_root, canonical_root, canonical_plan, arm, novel_id, chapter
            )
        except Exception:
            return "invalid_complete"
        return "complete"
    if case_dir.exists():
        return "incomplete_started"
    return "planned"


def sync_arm_status(run_plan: dict, run_root: Path, arm: str):
    if arm not in SYNC_ARMS:
        raise ChronologicalRuntimeError(f"not a synchronous arm: {arm}")
    canonical_root = Path(run_plan["canonical"]["root"])
    canonical_plan = _load_json(
        canonical_root / "chronological_state_plan.json",
        "canonical plan",
    )
    counts = {
        key: 0
        for key in (
            "planned",
            "complete",
            "uncertain",
            "response_failed",
            "invalid_complete",
            "incomplete_started",
        )
    }
    rows = []
    for novel_id, chapter in _case_order():
        status = _case_status(
            run_root, canonical_root, canonical_plan, arm, novel_id, chapter
        )
        counts[status] += 1
        rows.append(
            {
                "novel_id": novel_id,
                "chapter_number": chapter,
                "case_id": _case_id(novel_id, chapter),
                "status": status,
            }
        )
    blocked = any(
        counts[name]
        for name in (
            "uncertain",
            "response_failed",
            "invalid_complete",
            "incomplete_started",
        )
    )
    next_case = None
    if not blocked:
        for row in rows:
            if row["status"] == "planned":
                next_case = {
                    "novel_id": row["novel_id"],
                    "chapter_number": row["chapter_number"],
                    "case_id": row["case_id"],
                }
                break
    completed = counts["complete"]
    total_cost = 0.0
    paid_attempts = 0
    for row in rows:
        case_dir = _case_dir(
            run_root, arm, row["novel_id"], row["chapter_number"]
        )
        journal = case_dir / "journal.jsonl"
        paid_attempts += _journal_events(journal).count("started")
        if row["status"] == "complete":
            complete = _load_json(case_dir / "complete.json", "sync complete")
            total_cost += float((complete.get("cost") or {}).get("total_cost_usd", 0.0))
    return {
        "arm": arm,
        "counts": counts,
        "completed_total": completed,
        "paid_attempts": paid_attempts,
        "measured_cost_usd": total_cost,
        "blocked": blocked,
        "next_case": next_case,
        "complete": completed == MAX_CASES_PER_SYNC_ARM and not blocked,
    }


def _budget_cap(run_plan: dict, arm: str):
    return float(run_plan["budgets"][f"{arm}_usd"])


def _assert_budget(run_plan: dict, status: dict, arm: str):
    if status["paid_attempts"] >= MAX_CASES_PER_SYNC_ARM:
        raise BenchmarkSafetyError(f"{arm} paid-request count cap reached")
    if status["measured_cost_usd"] >= _budget_cap(run_plan, arm):
        raise BenchmarkSafetyError(f"{arm} measured budget cap reached")


def _worst_case_sync_request_cost(config, request: dict) -> float:
    """Conservative pre-send ceiling using request bytes as an input-token upper bound."""
    request_input_ceiling = len(_canonical_json_bytes(request)) + 1024
    highest_cache_miss = max(
        float(rates["cache_miss"]) for rates in config.prices.values()
    )
    highest_output = max(
        float(rates["output"]) for rates in config.prices.values()
    )
    return (
        request_input_ceiling / 1_000_000 * highest_cache_miss
        + config.max_tokens / 1_000_000 * highest_output
    )


def _assert_request_budget(run_plan: dict, status: dict, arm: str, config, request: dict):
    _assert_budget(run_plan, status, arm)
    worst = _worst_case_sync_request_cost(config, request)
    cap = _budget_cap(run_plan, arm)
    if status["measured_cost_usd"] + worst > cap:
        raise BenchmarkSafetyError(
            f"{arm} conservative next-request ceiling would exceed budget: "
            f"${status['measured_cost_usd'] + worst:.6f} > ${cap:.6f}"
        )
    return worst


def _load_run_plan(run_root: Path):
    run_root = Path(run_root).resolve()
    plan = _load_json(run_root / PLAN_FILENAME, "chronological runtime plan")
    if plan.get("schema_version") != RUNTIME_SCHEMA_VERSION or plan.get("kind") != RUNTIME_KIND:
        raise ChronologicalRuntimeError("runtime plan schema/kind mismatch")
    return plan


def prepare_run(
    *,
    repo_root: Path,
    expected_repo_head: str,
    canonical_root: Path,
    expected_canonical_plan_sha256: str,
    expected_canonical_identity_sha256: str,
    expected_canonical_tree_sha256: str,
    run_root: Path,
    flash_budget_usd: float = DEFAULT_FLASH_BUDGET_USD,
    luna_budget_usd: float = DEFAULT_LUNA_BUDGET_USD,
    batch_budget_usd: float = DEFAULT_BATCH_BUDGET_USD,
    now=utc_now,
):
    repo_root = Path(repo_root).resolve()
    head = _ensure_repo_head(repo_root, expected_repo_head)
    canonical_root = Path(canonical_root).resolve()
    canonical_plan, canonical_plan_sha, canonical_tree_sha = _validate_canonical(
        canonical_root,
        expected_plan_sha256=expected_canonical_plan_sha256,
        expected_identity_sha256=expected_canonical_identity_sha256,
        expected_tree_sha256=expected_canonical_tree_sha256,
    )
    run_root = _ensure_external_run_root(repo_root, canonical_root, run_root, exists=False)
    budgets = {
        f"{FLASH_ARM}_usd": float(flash_budget_usd),
        f"{LUNA_ARM}_usd": float(luna_budget_usd),
        f"{BATCH_ARM}_usd": float(batch_budget_usd),
    }
    if any(value <= 0 or value > 10 for value in budgets.values()):
        raise ChronologicalRuntimeError("each experiment budget must be >0 and <=10 USD")
    configs = {}
    for arm in SYNC_ARMS:
        _, configs[arm] = _model_snapshot(arm)
    protected_before = _protected_state(repo_root)
    cases = [
        {
            "ordinal": index + 1,
            "case_id": _case_id(novel_id, chapter),
            "novel_id": novel_id,
            "chapter_number": chapter,
            "source_sha256": next(
                row["source_sha256"]
                for row in _novel_record(canonical_plan, novel_id)["chapter_records"]
                if row["chapter_number"] == chapter
            ),
        }
        for index, (novel_id, chapter) in enumerate(_case_order())
    ]
    identity = {
        "kind": RUNTIME_KIND,
        "repo_head": head,
        "canonical_identity_sha256": canonical_plan["identity_sha256"],
        "canonical_plan_sha256": canonical_plan_sha,
        "canonical_tree_sha256": canonical_tree_sha,
        "state_protocol_version": STATE_PROTOCOL_VERSION,
        "state_protocol_sha256": _sha256_text(STATE_PROTOCOL),
        "case_count": len(cases),
        "case_order": cases,
        "model_configs": configs,
        "batch_design": {
            "mode": "replay_exact_luna_sync_requests_after_lineage_complete",
            "request_count": MAX_CASES_PER_SYNC_ARM,
            "endpoint": BATCH_ENDPOINT,
            "completion_window": BATCH_COMPLETION_WINDOW,
            "output_order_must_use_custom_id": True,
            "batch_does_not_mutate_state_lineage": True,
        },
        "budgets": budgets,
    }
    run_id = _canonical_json_sha256(identity)
    document = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": RUNTIME_KIND,
        "status": "prepared",
        "run_id": run_id,
        "identity": identity,
        "canonical": {
            "root": str(canonical_root),
            "identity_sha256": canonical_plan["identity_sha256"],
            "plan_sha256": canonical_plan_sha,
            "tree_sha256": canonical_tree_sha,
        },
        "repo_root": str(repo_root),
        "repo_head": head,
        "budgets": budgets,
        "protected_state_before": protected_before,
        "authorization": {
            "automatic_retry_forbidden": True,
            "ambiguous_sync_outcome_blocks_arm": True,
            "batch_submission_ambiguity_blocks_batch_arm": True,
            "maximum_paid_sync_requests_per_arm": MAX_CASES_PER_SYNC_ARM,
            "batch_replay_requests": MAX_CASES_PER_SYNC_ARM,
        },
        "prepared_at_utc": now().isoformat(),
    }
    run_root.mkdir(parents=True, exist_ok=False)
    _write_json_exclusive(run_root / PLAN_FILENAME, document)
    _write_exclusive(run_root / RUN_JOURNAL_FILENAME, b"")
    _append_journal(
        run_root / RUN_JOURNAL_FILENAME,
        {
            "event": "prepared",
            "at_utc": now().isoformat(),
            "run_id": run_id,
            "provider_calls": 0,
        },
    )
    _ensure_protected_state(repo_root, protected_before, "after run preparation")
    return document


def _case_identity(run_plan, arm, novel_id, chapter, record, state_before_sha, request_sha):
    config = run_plan["identity"]["model_configs"][arm]
    return {
        "kind": RUNTIME_KIND,
        "run_id": run_plan["run_id"],
        "canonical_identity_sha256": run_plan["canonical"]["identity_sha256"],
        "arm": arm,
        "novel_id": novel_id,
        "chapter_number": chapter,
        "source_sha256": record["source_sha256"],
        "state_before_identity_sha256": state_before_sha,
        "request_sha256": request_sha,
        **config,
    }


def execute_sync_next(
    run_root: Path,
    *,
    arm: str,
    authorization: str,
    now=utc_now,
    timer=time.perf_counter,
    client_factory=create_benchmark_client,
):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    if arm not in SYNC_ARMS:
        raise ChronologicalRuntimeError(f"unsupported sync arm: {arm}")
    if authorization != SYNC_EXECUTE_AUTHORIZATION[arm]:
        raise ChronologicalRuntimeError("exact one-call paid authorization token is required")
    repo_root = Path(run_plan["repo_root"])
    _ensure_repo_head(repo_root, run_plan["repo_head"])
    canonical_root = Path(run_plan["canonical"]["root"])
    canonical_plan, plan_sha, tree_sha = _validate_canonical(
        canonical_root,
        expected_plan_sha256=run_plan["canonical"]["plan_sha256"],
        expected_identity_sha256=run_plan["canonical"]["identity_sha256"],
        expected_tree_sha256=run_plan["canonical"]["tree_sha256"],
    )
    status = sync_arm_status(run_plan, run_root, arm)
    if status["blocked"]:
        raise BenchmarkSafetyError(
            f"{arm} contains uncertain/response_failed/invalid/incomplete evidence; "
            "further paid calls are blocked"
        )
    if status["complete"]:
        raise ChronologicalRuntimeError(f"{arm} is already complete")
    case = status["next_case"]
    novel_id = case["novel_id"]
    chapter = case["chapter_number"]
    state_before, state_before_sha, _ = _state_before_for_case(
        run_root, canonical_root, canonical_plan, arm, novel_id, chapter
    )
    request, source_record, config, config_snapshot = _build_request(
        canonical_root, canonical_plan, arm, novel_id, chapter, state_before
    )
    conservative_request_cost_ceiling = _assert_request_budget(
        run_plan, status, arm, config, request
    )
    request_sha = _canonical_json_sha256(request)
    identity = _case_identity(
        run_plan,
        arm,
        novel_id,
        chapter,
        source_record,
        state_before_sha,
        request_sha,
    )
    identity_sha = _canonical_json_sha256(identity)
    case_dir = _case_dir(run_root, arm, novel_id, chapter)
    if case_dir.exists():
        raise BenchmarkSafetyError(f"case directory already exists: {case_dir}")
    case_dir.mkdir(parents=True, exist_ok=False)
    paths = _sync_complete_paths(case_dir)
    _write_exclusive(paths["request"], _canonical_json_bytes(request))
    _write_exclusive(paths["journal"], b"")
    planned = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": RUNTIME_KIND,
        "status": "planned",
        "identity_sha256": identity_sha,
        "identity": identity,
        "authorization": {
            "paid_requests_authorized": 1,
            "automatic_retry_forbidden": True,
            "ambiguous_outcome_retry_forbidden": True,
            "conservative_request_cost_ceiling_usd": conservative_request_cost_ceiling,
            "arm_budget_usd": _budget_cap(run_plan, arm),
            "measured_cost_before_usd": status["measured_cost_usd"],
        },
    }
    _write_json_exclusive(case_dir / "manifest.json", planned)
    _append_journal(
        paths["journal"],
        {
            "event": "planned",
            "at_utc": now().isoformat(),
            "identity_sha256": identity_sha,
            "request_sha256": request_sha,
        },
    )
    protected_before = _ensure_protected_state(
        repo_root, run_plan["protected_state_before"], "before sync request"
    )
    request_started = now()
    _append_journal(
        paths["journal"],
        {
            "event": "started",
            "at_utc": request_started.isoformat(),
            "attempt": 1,
            "request_sha256": request_sha,
            "model": config.model,
            "automatic_retry_forbidden": True,
        },
    )
    client = client_factory(config)
    started = timer()
    try:
        response = client.chat.completions.create(**request)
        elapsed = timer() - started
    except BaseException as exc:
        elapsed = timer() - started
        uncertain = {
            **planned,
            "status": "uncertain",
            "elapsed_seconds": elapsed,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "safety": {
                "provider_calls_attempted": 1,
                "paid_requests_attempted": 1,
                "provider_response_recorded": False,
                "retry_forbidden": True,
            },
        }
        _append_journal(
            paths["journal"],
            {
                "event": "uncertain",
                "at_utc": now().isoformat(),
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "retry_forbidden": True,
            },
        )
        _write_json_exclusive(paths["uncertain"], uncertain)
        raise ChronologicalUncertainError(
            f"{arm} transport outcome is ambiguous; retry is forbidden"
        ) from exc
    raw_response = _provider_dict(response)
    _write_json_exclusive(paths["response"], raw_response)
    provider = _provider_identity(response)
    usage = _usage_snapshot(response)
    _append_journal(
        paths["journal"],
        {
            "event": "response_recorded",
            "at_utc": now().isoformat(),
            "elapsed_seconds": elapsed,
            **provider,
            "usage": usage,
        },
    )
    try:
        if provider["response_model"] != config.model:
            raise BenchmarkSafetyError(
                f"provider model mismatch: {provider['response_model']!r} != {config.model!r}"
            )
        if provider["finish_reason"] != "stop":
            raise ChronologicalRuntimeError(
                f"provider finish_reason is not stop: {provider['finish_reason']!r}"
            )
        content = _response_content(response)
        model_document = parse_model_document(content)
        state_after, state_metrics = apply_state_update(
            state_before, model_document, chapter
        )
        translation = model_document["translation"]
        state_update = model_document["state_update"]
        _write_text_exclusive(paths["translation"], translation)
        _write_json_exclusive(paths["state_update"], state_update)
        _write_json_exclusive(paths["state_after"], state_after)
        protected_after = _ensure_protected_state(
            repo_root, protected_before, "after sync response processing"
        )
        pricing_result = _pricing_result(config, request_started, usage, batch=False)
        residual = residual_source_script_report(
            translation, _novel_record(canonical_plan, novel_id)["source_language"]
        )
    except BaseException as exc:
        protected_after = _protected_state(repo_root)
        failed = {
            **planned,
            "status": "response_failed",
            "elapsed_seconds": elapsed,
            "provider_response": {**provider, "usage": usage},
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "safety": {
                "provider_calls_attempted": 1,
                "paid_requests_attempted": 1,
                "provider_response_recorded": True,
                "protected_state_verified_unchanged": protected_after == protected_before,
                "retry_forbidden": True,
            },
        }
        _append_journal(
            paths["journal"],
            {
                "event": "response_failed",
                "at_utc": now().isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "retry_forbidden": True,
            },
        )
        _write_json_exclusive(paths["response_failed"], failed)
        raise ChronologicalRuntimeError(
            f"{arm} received a provider response but validation failed; retry is forbidden"
        ) from exc
    artifacts = {
        "request_sha256": _sha256_file(paths["request"]),
        "response_sha256": _sha256_file(paths["response"]),
        "translation_sha256": _sha256_file(paths["translation"]),
        "state_update_file_sha256": _sha256_file(paths["state_update"]),
        "state_update_identity_sha256": state_metrics["state_update_identity_sha256"],
        "state_after_file_sha256": _sha256_file(paths["state_after"]),
        "state_after_identity_sha256": state_metrics["state_after_identity_sha256"],
    }
    final = {
        **planned,
        "status": "complete",
        "request_started_utc": request_started.isoformat(),
        "response_recorded_utc": now().isoformat(),
        "elapsed_seconds": elapsed,
        "provider_response": {**provider, "usage": usage},
        "cost": pricing_result,
        "state_metrics": state_metrics,
        "quality_signals": {
            "remaining_cjk_character_count": residual["remaining_cjk_character_count"],
            "residual_cjk_fragments": residual["residual_cjk_fragments"],
            "translation_character_count": len(translation),
            "translation_word_count": len(translation.split()),
        },
        "artifacts": artifacts,
        "safety": {
            "provider_calls_attempted": 1,
            "paid_requests_attempted": 1,
            "provider_response_recorded": True,
            "protected_state_verified_unchanged": True,
            "automatic_retry_forbidden": True,
        },
    }
    _write_json_exclusive(paths["complete"], final)
    _append_journal(
        paths["journal"],
        {
            "event": "complete",
            "at_utc": now().isoformat(),
            "translation_sha256": artifacts["translation_sha256"],
            "state_after_identity_sha256": artifacts["state_after_identity_sha256"],
            "protected_state_verified_unchanged": True,
            "retry_forbidden": True,
        },
    )
    return final


def _batch_dir(run_root: Path):
    return Path(run_root) / "arms" / BATCH_ARM


def _batch_manifest_path(run_root: Path):
    return _batch_dir(run_root) / BATCH_MANIFEST_FILENAME


def _batch_snapshot(batch):
    counts = _field(batch, "request_counts")
    return {
        "id": _field(batch, "id"),
        "status": _field(batch, "status"),
        "endpoint": _field(batch, "endpoint"),
        "input_file_id": _field(batch, "input_file_id"),
        "output_file_id": _field(batch, "output_file_id"),
        "error_file_id": _field(batch, "error_file_id"),
        "completion_window": _field(batch, "completion_window"),
        "created_at": _field(batch, "created_at"),
        "completed_at": _field(batch, "completed_at"),
        "expires_at": _field(batch, "expires_at"),
        "request_counts": {
            "total": int((_field(counts, "total", 0) if counts is not None else 0) or 0),
            "completed": int((_field(counts, "completed", 0) if counts is not None else 0) or 0),
            "failed": int((_field(counts, "failed", 0) if counts is not None else 0) or 0),
        },
        "metadata": _provider_dict(_field(batch, "metadata")) or {},
    }


def _create_openai_batch_client():
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ChronologicalRuntimeError("OPENAI_API_KEY was not found in .env")
    return OpenAI(api_key=api_key, max_retries=0)


def prepare_batch_replay(run_root: Path, *, now=utc_now):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    luna_status = sync_arm_status(run_plan, run_root, LUNA_ARM)
    if not luna_status["complete"]:
        raise BenchmarkSafetyError(
            "Luna synchronous lineage must be fully complete before Batch replay is prepared"
        )
    batch_dir = _batch_dir(run_root)
    if batch_dir.exists():
        raise BenchmarkSafetyError(f"Batch replay directory already exists: {batch_dir}")
    lines = []
    requests = []
    seen = set()
    for ordinal, (novel_id, chapter) in enumerate(_case_order(), 1):
        case_dir = _case_dir(run_root, LUNA_ARM, novel_id, chapter)
        complete = _validate_sync_complete(
            run_root,
            Path(run_plan["canonical"]["root"]),
            _load_json(
                Path(run_plan["canonical"]["root"]) / "chronological_state_plan.json",
                "canonical plan",
            ),
            LUNA_ARM,
            novel_id,
            chapter,
        )
        request_path = case_dir / "request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request_sha = _sha256_file(request_path)
        if request_sha != complete["artifacts"]["request_sha256"]:
            raise BenchmarkSafetyError("Luna sync request drifted before Batch replay")
        custom_id = f"{ordinal:02d}-{_case_id(novel_id, chapter)}-{request_sha[:16]}"
        if custom_id in seen:
            raise ChronologicalRuntimeError("duplicate deterministic Batch custom_id")
        seen.add(custom_id)
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": request,
        }
        lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        requests.append(
            {
                "ordinal": ordinal,
                "custom_id": custom_id,
                "case_id": _case_id(novel_id, chapter),
                "novel_id": novel_id,
                "chapter_number": chapter,
                "request_sha256": request_sha,
                "batch_line_sha256": _canonical_json_sha256(line),
                "state_before_identity_sha256": complete["identity"][
                    "state_before_identity_sha256"
                ],
            }
        )
    if len(requests) != MAX_CASES_PER_SYNC_ARM:
        raise ChronologicalRuntimeError("Batch replay did not resolve to exactly 50 requests")
    input_text = "\n".join(lines) + "\n"
    if len(input_text.encode("utf-8")) > 200 * 1024 * 1024:
        raise ChronologicalRuntimeError("Batch replay JSONL exceeds 200 MB API limit")
    batch_dir.mkdir(parents=True, exist_ok=False)
    input_path = batch_dir / BATCH_INPUT_FILENAME
    _write_text_exclusive(input_path, input_text)
    identity = {
        "kind": RUNTIME_KIND,
        "run_id": run_plan["run_id"],
        "arm": BATCH_ARM,
        "source_arm": LUNA_ARM,
        "model": EXPECTED_MODEL_CONFIG[LUNA_ARM],
        "endpoint": BATCH_ENDPOINT,
        "completion_window": BATCH_COMPLETION_WINDOW,
        "request_count": len(requests),
        "input_sha256": _sha256_file(input_path),
        "requests": requests,
        "batch_state_policy": "replay_only_no_state_mutation",
    }
    manifest = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": RUNTIME_KIND,
        "status": "prepared",
        "batch_identity_sha256": _canonical_json_sha256(identity),
        "identity": identity,
        "prepared_at_utc": now().isoformat(),
        "input_file_id": None,
        "batch_id": None,
        "provider_batch": None,
        "authorization": {
            "one_batch_authorized_request_count": MAX_CASES_PER_SYNC_ARM,
            "automatic_retry_forbidden": True,
            "ambiguous_submission_retry_forbidden": True,
        },
    }
    _write_json_exclusive(_batch_manifest_path(run_root), manifest)
    _write_exclusive(batch_dir / BATCH_JOURNAL_FILENAME, b"")
    _append_journal(
        batch_dir / BATCH_JOURNAL_FILENAME,
        {
            "event": "prepared",
            "at_utc": now().isoformat(),
            "batch_identity_sha256": manifest["batch_identity_sha256"],
            "input_sha256": identity["input_sha256"],
            "provider_calls": 0,
        },
    )
    return manifest


def _read_batch_manifest(run_root: Path):
    value = _load_json(_batch_manifest_path(run_root), "Batch replay manifest")
    if value.get("kind") != RUNTIME_KIND or value.get("identity", {}).get("arm") != BATCH_ARM:
        raise ChronologicalRuntimeError("Batch replay manifest identity mismatch")
    input_path = _batch_dir(run_root) / BATCH_INPUT_FILENAME
    if not input_path.is_file():
        raise ChronologicalRuntimeError("Batch input JSONL is missing")
    if _sha256_file(input_path) != value["identity"]["input_sha256"]:
        raise BenchmarkSafetyError("Batch input JSONL hash drifted")
    return value


def submit_batch_replay(
    run_root: Path,
    *,
    authorization: str,
    client=None,
    now=utc_now,
):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    manifest = _read_batch_manifest(run_root)
    if authorization != BATCH_SUBMIT_AUTHORIZATION:
        raise ChronologicalRuntimeError("exact paid Batch authorization token is required")
    if manifest.get("status") not in {"prepared", "input_uploaded"}:
        raise BenchmarkSafetyError(
            f"Batch replay cannot be submitted from status {manifest.get('status')!r}"
        )
    batch_budget = float(run_plan["budgets"][f"{BATCH_ARM}_usd"])
    if batch_budget <= 0:
        raise BenchmarkSafetyError("Batch budget is not positive")
    input_path_for_budget = _batch_dir(run_root) / BATCH_INPUT_FILENAME
    input_byte_ceiling = input_path_for_budget.stat().st_size + 4096
    luna_config, _ = _model_snapshot(LUNA_ARM)
    batch_rates = luna_config.economy_prices
    conservative_batch_ceiling = (
        input_byte_ceiling / 1_000_000 * float(batch_rates["cache_miss"])
        + (
            luna_config.max_tokens
            * int(manifest["identity"]["request_count"])
            / 1_000_000
            * float(batch_rates["output"])
        )
    )
    if conservative_batch_ceiling > batch_budget:
        raise BenchmarkSafetyError(
            "conservative Batch cost ceiling exceeds frozen budget: "
            f"${conservative_batch_ceiling:.6f} > ${batch_budget:.6f}"
        )
    manifest["conservative_cost_ceiling_usd"] = conservative_batch_ceiling
    repo_root = Path(run_plan["repo_root"])
    _ensure_repo_head(repo_root, run_plan["repo_head"])
    protected_before = _ensure_protected_state(
        repo_root, run_plan["protected_state_before"], "before Batch submit"
    )
    if client is None:
        client = _create_openai_batch_client()
    batch_dir = _batch_dir(run_root)
    manifest_path = _batch_manifest_path(run_root)
    journal = batch_dir / BATCH_JOURNAL_FILENAME
    input_path = batch_dir / BATCH_INPUT_FILENAME
    if manifest["status"] == "prepared":
        manifest["status"] = "upload_started"
        atomic_write_json(manifest_path, manifest)
        _append_journal(
            journal,
            {"event": "upload_started", "at_utc": now().isoformat(), "attempt": 1},
        )
        try:
            with input_path.open("rb") as handle:
                uploaded = client.files.create(file=handle, purpose="batch")
        except BaseException as exc:
            manifest["status"] = "upload_uncertain"
            manifest["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
            atomic_write_json(manifest_path, manifest)
            _append_journal(
                journal,
                {
                    "event": "upload_uncertain",
                    "at_utc": now().isoformat(),
                    "retry_forbidden": True,
                },
            )
            raise ChronologicalUncertainError(
                "Batch input upload outcome is ambiguous; automatic retry is forbidden"
            ) from exc
        file_id = _field(uploaded, "id")
        if not isinstance(file_id, str) or not file_id:
            manifest["status"] = "upload_uncertain"
            atomic_write_json(manifest_path, manifest)
            raise ChronologicalUncertainError("Batch upload returned no file ID")
        manifest["input_file_id"] = file_id
        manifest["uploaded_file"] = _provider_dict(uploaded)
        manifest["status"] = "input_uploaded"
        atomic_write_json(manifest_path, manifest)
        _append_journal(
            journal,
            {
                "event": "input_uploaded",
                "at_utc": now().isoformat(),
                "input_file_id": file_id,
            },
        )
    metadata = {
        "benchmark": RUNTIME_KIND,
        "run_id": run_plan["run_id"],
        "batch_identity": manifest["batch_identity_sha256"],
    }
    manifest["status"] = "submission_started"
    manifest["submission_metadata"] = metadata
    atomic_write_json(manifest_path, manifest)
    _append_journal(
        journal,
        {"event": "submission_started", "at_utc": now().isoformat(), "attempt": 1},
    )
    try:
        batch = client.batches.create(
            input_file_id=manifest["input_file_id"],
            endpoint=BATCH_ENDPOINT,
            completion_window=BATCH_COMPLETION_WINDOW,
            metadata=metadata,
        )
    except BaseException as exc:
        manifest["status"] = "submission_uncertain"
        manifest["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
        atomic_write_json(manifest_path, manifest)
        _append_journal(
            journal,
            {
                "event": "submission_uncertain",
                "at_utc": now().isoformat(),
                "retry_forbidden": True,
            },
        )
        raise ChronologicalUncertainError(
            "Batch creation outcome is ambiguous; automatic reissue is forbidden"
        ) from exc
    batch_id = _field(batch, "id")
    if not isinstance(batch_id, str) or not batch_id:
        manifest["status"] = "submission_uncertain"
        atomic_write_json(manifest_path, manifest)
        raise ChronologicalUncertainError("Batch creation returned no batch ID")
    snapshot = _batch_snapshot(batch)
    if snapshot["endpoint"] not in {None, BATCH_ENDPOINT}:
        raise BenchmarkSafetyError("provider Batch endpoint mismatch")
    manifest["batch_id"] = batch_id
    manifest["provider_batch"] = snapshot
    manifest["status"] = "submitted"
    manifest.pop("last_error", None)
    atomic_write_json(manifest_path, manifest)
    _append_journal(
        journal,
        {
            "event": "submitted",
            "at_utc": now().isoformat(),
            "batch_id": batch_id,
            "input_file_id": manifest["input_file_id"],
        },
    )
    _ensure_protected_state(repo_root, protected_before, "after Batch submit")
    return manifest


def _iter_batch_list(client, max_pages=20):
    page = client.batches.list(limit=100)
    for _ in range(max_pages):
        for item in list(_field(page, "data", []) or []):
            yield item
        has_next = getattr(page, "has_next_page", None)
        get_next = getattr(page, "get_next_page", None)
        if not callable(has_next) or not has_next():
            return
        if not callable(get_next):
            raise ChronologicalUncertainError(
                "Batch listing indicates another page but exposes no paginator"
            )
        page = get_next()
    raise ChronologicalUncertainError("Batch reconciliation exceeded bounded listing horizon")


def reconcile_batch_submission(run_root: Path, *, client=None, now=utc_now):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    manifest = _read_batch_manifest(run_root)
    if manifest.get("status") != "submission_uncertain":
        raise ChronologicalRuntimeError("Batch reconciliation requires submission_uncertain")
    if not manifest.get("input_file_id"):
        raise ChronologicalRuntimeError("Batch reconciliation requires a known input_file_id")
    if client is None:
        client = _create_openai_batch_client()
    metadata = manifest.get("submission_metadata") or {}
    matches = []
    for batch in _iter_batch_list(client):
        snapshot = _batch_snapshot(batch)
        if (
            snapshot.get("input_file_id") == manifest["input_file_id"]
            and snapshot.get("endpoint") == BATCH_ENDPOINT
            and snapshot.get("metadata", {}).get("run_id") == run_plan["run_id"]
            and snapshot.get("metadata", {}).get("batch_identity")
            == manifest["batch_identity_sha256"]
        ):
            matches.append(batch)
    if len(matches) != 1:
        raise ChronologicalUncertainError(
            f"Batch reconciliation found {len(matches)} exact candidates; manual resolution required"
        )
    batch = matches[0]
    manifest["batch_id"] = _field(batch, "id")
    manifest["provider_batch"] = _batch_snapshot(batch)
    manifest["status"] = "submitted"
    manifest.pop("last_error", None)
    atomic_write_json(_batch_manifest_path(run_root), manifest)
    _append_journal(
        _batch_dir(run_root) / BATCH_JOURNAL_FILENAME,
        {
            "event": "reconciled",
            "at_utc": now().isoformat(),
            "batch_id": manifest["batch_id"],
        },
    )
    return manifest


def refresh_batch_status(run_root: Path, *, client=None, now=utc_now):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    manifest = _read_batch_manifest(run_root)
    if not manifest.get("batch_id"):
        raise ChronologicalRuntimeError("Batch has no known batch_id")
    if manifest.get("status") in {"upload_uncertain", "submission_uncertain"}:
        raise BenchmarkSafetyError("ambiguous Batch evidence must be reconciled manually first")
    if client is None:
        client = _create_openai_batch_client()
    repo_root = Path(run_plan["repo_root"])
    protected_before = _ensure_protected_state(
        repo_root, run_plan["protected_state_before"], "before Batch status"
    )
    batch = client.batches.retrieve(manifest["batch_id"])
    snapshot = _batch_snapshot(batch)
    if snapshot["id"] != manifest["batch_id"]:
        raise BenchmarkSafetyError("Batch status returned the wrong batch identity")
    manifest["provider_batch"] = snapshot
    provider_status = snapshot.get("status")
    manifest["status"] = (
        "provider_completed"
        if provider_status == "completed"
        else f"provider_{provider_status}"
    )
    manifest["status_checked_at_utc"] = now().isoformat()
    atomic_write_json(_batch_manifest_path(run_root), manifest)
    _append_journal(
        _batch_dir(run_root) / BATCH_JOURNAL_FILENAME,
        {
            "event": "status_checked",
            "at_utc": now().isoformat(),
            "provider_status": provider_status,
            "request_counts": snapshot["request_counts"],
        },
    )
    _ensure_protected_state(repo_root, protected_before, "after Batch status")
    return manifest


def _file_content_bytes(content):
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    read = getattr(content, "read", None)
    if callable(read):
        value = read()
        return value if isinstance(value, bytes) else str(value).encode("utf-8")
    field_content = _field(content, "content")
    if isinstance(field_content, bytes):
        return field_content
    if isinstance(field_content, str):
        return field_content.encode("utf-8")
    raise ChronologicalRuntimeError("provider file content could not be converted to bytes")


def collect_batch_replay(run_root: Path, *, client=None, now=utc_now):
    run_root = Path(run_root).resolve()
    run_plan = _load_run_plan(run_root)
    manifest = _read_batch_manifest(run_root)
    if manifest.get("status") != "provider_completed":
        raise ChronologicalRuntimeError("Batch collection requires provider_completed status")
    provider_batch = manifest.get("provider_batch") or {}
    output_file_id = provider_batch.get("output_file_id")
    if not isinstance(output_file_id, str) or not output_file_id:
        raise ChronologicalRuntimeError("completed Batch has no output_file_id")
    if client is None:
        client = _create_openai_batch_client()
    repo_root = Path(run_plan["repo_root"])
    protected_before = _ensure_protected_state(
        repo_root, run_plan["protected_state_before"], "before Batch collect"
    )
    content = client.files.content(output_file_id)
    payload = _file_content_bytes(content)
    output_path = _batch_dir(run_root) / BATCH_OUTPUT_FILENAME
    _write_exclusive(output_path, payload)
    expected_by_custom = {
        row["custom_id"]: row
        for row in manifest["identity"]["requests"]
    }
    seen = set()
    results = []
    result_dir = _batch_dir(run_root) / "results"
    result_dir.mkdir()
    config, _ = _model_snapshot(LUNA_ARM)
    total_cost = 0.0
    for line_number, raw in enumerate(payload.decode("utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChronologicalRuntimeError(
                f"Batch output line {line_number} is invalid JSON: {exc}"
            ) from exc
        custom_id = row.get("custom_id")
        if custom_id not in expected_by_custom or custom_id in seen:
            raise BenchmarkSafetyError(f"unexpected/duplicate Batch custom_id: {custom_id!r}")
        seen.add(custom_id)
        if row.get("error") is not None:
            raise ChronologicalRuntimeError(
                f"Batch request {custom_id} returned an error: {row.get('error')!r}"
            )
        response_wrapper = row.get("response")
        if not isinstance(response_wrapper, dict) or response_wrapper.get("status_code") != 200:
            raise ChronologicalRuntimeError(f"Batch request {custom_id} was not HTTP 200")
        body = response_wrapper.get("body")
        if not isinstance(body, dict):
            raise ChronologicalRuntimeError(f"Batch request {custom_id} has no response body")
        provider = _provider_identity(body)
        if provider["response_model"] != config.model or provider["finish_reason"] != "stop":
            raise BenchmarkSafetyError(f"Batch request {custom_id} provider identity mismatch")
        content_text = _response_content(body)
        model_document = parse_model_document(content_text)
        mapping = expected_by_custom[custom_id]
        usage = _usage_snapshot(body)
        cost = _pricing_result(config, now(), usage, batch=True)
        total_cost += cost["total_cost_usd"]
        translation_path = result_dir / f"{mapping['case_id']}.translation.txt"
        result_path = result_dir / f"{mapping['case_id']}.json"
        _write_text_exclusive(translation_path, model_document["translation"])
        result_document = {
            "custom_id": custom_id,
            "case_id": mapping["case_id"],
            "novel_id": mapping["novel_id"],
            "chapter_number": mapping["chapter_number"],
            "request_sha256": mapping["request_sha256"],
            "state_before_identity_sha256": mapping["state_before_identity_sha256"],
            "provider_response": {**provider, "usage": usage},
            "cost": cost,
            "translation_sha256": _sha256_file(translation_path),
            "state_update_identity_sha256": _canonical_json_sha256(
                model_document["state_update"]
            ),
            "state_update": model_document["state_update"],
            "batch_state_applied": False,
        }
        _write_json_exclusive(result_path, result_document)
        results.append(result_document)
    if seen != set(expected_by_custom):
        missing = sorted(set(expected_by_custom) - seen)
        raise ChronologicalRuntimeError(f"Batch output is missing {len(missing)} requests")
    budget = float(run_plan["budgets"][f"{BATCH_ARM}_usd"])
    if total_cost > budget:
        raise BenchmarkSafetyError(
            f"measured Batch cost ${total_cost:.6f} exceeded frozen budget ${budget:.6f}"
        )
    protected_after = _ensure_protected_state(
        repo_root, protected_before, "after Batch collect"
    )
    final = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "kind": RUNTIME_KIND,
        "status": "complete",
        "batch_identity_sha256": manifest["batch_identity_sha256"],
        "batch_id": manifest["batch_id"],
        "output_file_id": output_file_id,
        "output_sha256": _sha256_file(output_path),
        "request_count": len(results),
        "total_cost_usd": total_cost,
        "results_identity_sha256": _canonical_json_sha256(
            [
                {
                    "custom_id": item["custom_id"],
                    "translation_sha256": item["translation_sha256"],
                    "state_update_identity_sha256": item["state_update_identity_sha256"],
                }
                for item in sorted(results, key=lambda item: item["custom_id"])
            ]
        ),
        "safety": {
            "provider_calls_attempted_for_collection": 1,
            "inference_retries": 0,
            "protected_state_verified_unchanged": protected_after == protected_before,
            "batch_state_applied": False,
        },
        "completed_at_utc": now().isoformat(),
    }
    _write_json_exclusive(_batch_dir(run_root) / "complete.json", final)
    manifest["status"] = "complete"
    manifest["final_complete_sha256"] = _sha256_file(_batch_dir(run_root) / "complete.json")
    atomic_write_json(_batch_manifest_path(run_root), manifest)
    _append_journal(
        _batch_dir(run_root) / BATCH_JOURNAL_FILENAME,
        {
            "event": "complete",
            "at_utc": now().isoformat(),
            "request_count": len(results),
            "total_cost_usd": total_cost,
            "results_identity_sha256": final["results_identity_sha256"],
        },
    )
    return final


def runtime_status(run_root: Path):
    run_root = Path(run_root).resolve()
    plan = _load_run_plan(run_root)
    result = {
        "run_id": plan["run_id"],
        "canonical_identity_sha256": plan["canonical"]["identity_sha256"],
        "arms": {
            FLASH_ARM: sync_arm_status(plan, run_root, FLASH_ARM),
            LUNA_ARM: sync_arm_status(plan, run_root, LUNA_ARM),
        },
    }
    batch_manifest = _batch_manifest_path(run_root)
    if batch_manifest.is_file():
        manifest = _read_batch_manifest(run_root)
        result["arms"][BATCH_ARM] = {
            "status": manifest.get("status"),
            "batch_id": manifest.get("batch_id"),
            "request_count": manifest.get("identity", {}).get("request_count"),
            "complete": (_batch_dir(run_root) / "complete.json").is_file(),
        }
    else:
        result["arms"][BATCH_ARM] = {
            "status": "not_prepared",
            "batch_id": None,
            "request_count": 0,
            "complete": False,
        }
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        description="Restart-safe chronological Flash/Luna/Luna-Batch benchmark harness"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo-root", required=True)
    prepare.add_argument("--expected-repo-head", required=True)
    prepare.add_argument("--canonical-root", required=True)
    prepare.add_argument("--expected-canonical-plan-sha256", required=True)
    prepare.add_argument("--expected-canonical-identity-sha256", required=True)
    prepare.add_argument("--expected-canonical-tree-sha256", required=True)
    prepare.add_argument("--run-root", required=True)
    prepare.add_argument("--flash-budget-usd", type=float, default=DEFAULT_FLASH_BUDGET_USD)
    prepare.add_argument("--luna-budget-usd", type=float, default=DEFAULT_LUNA_BUDGET_USD)
    prepare.add_argument("--batch-budget-usd", type=float, default=DEFAULT_BATCH_BUDGET_USD)

    status = sub.add_parser("status")
    status.add_argument("--run-root", required=True)

    execute = sub.add_parser("execute-next")
    execute.add_argument("--run-root", required=True)
    execute.add_argument("--arm", choices=SYNC_ARMS, required=True)
    execute.add_argument("--authorization", required=True)

    batch_prepare = sub.add_parser("prepare-batch")
    batch_prepare.add_argument("--run-root", required=True)

    batch_submit = sub.add_parser("submit-batch")
    batch_submit.add_argument("--run-root", required=True)
    batch_submit.add_argument("--authorization", required=True)

    batch_reconcile = sub.add_parser("reconcile-batch")
    batch_reconcile.add_argument("--run-root", required=True)

    batch_status = sub.add_parser("batch-status")
    batch_status.add_argument("--run-root", required=True)

    batch_collect = sub.add_parser("collect-batch")
    batch_collect.add_argument("--run-root", required=True)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            document = prepare_run(
                repo_root=Path(args.repo_root),
                expected_repo_head=args.expected_repo_head,
                canonical_root=Path(args.canonical_root),
                expected_canonical_plan_sha256=args.expected_canonical_plan_sha256,
                expected_canonical_identity_sha256=args.expected_canonical_identity_sha256,
                expected_canonical_tree_sha256=args.expected_canonical_tree_sha256,
                run_root=Path(args.run_root),
                flash_budget_usd=args.flash_budget_usd,
                luna_budget_usd=args.luna_budget_usd,
                batch_budget_usd=args.batch_budget_usd,
            )
            print(json.dumps({"run_id": document["run_id"], "provider_calls": 0}, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(runtime_status(Path(args.run_root)), indent=2))
            return 0
        if args.command == "execute-next":
            document = execute_sync_next(
                Path(args.run_root),
                arm=args.arm,
                authorization=args.authorization,
            )
            print(
                json.dumps(
                    {
                        "status": document["status"],
                        "arm": document["identity"]["arm"],
                        "case_id": _case_id(
                            document["identity"]["novel_id"],
                            document["identity"]["chapter_number"],
                        ),
                        "cost_usd": document["cost"]["total_cost_usd"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "prepare-batch":
            document = prepare_batch_replay(Path(args.run_root))
            print(
                json.dumps(
                    {
                        "status": document["status"],
                        "request_count": document["identity"]["request_count"],
                        "provider_calls": 0,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "submit-batch":
            document = submit_batch_replay(
                Path(args.run_root),
                authorization=args.authorization,
            )
            print(json.dumps({"status": document["status"], "batch_id": document["batch_id"]}, indent=2))
            return 0
        if args.command == "reconcile-batch":
            document = reconcile_batch_submission(Path(args.run_root))
            print(json.dumps({"status": document["status"], "batch_id": document["batch_id"]}, indent=2))
            return 0
        if args.command == "batch-status":
            document = refresh_batch_status(Path(args.run_root))
            print(
                json.dumps(
                    {
                        "status": document["status"],
                        "provider_batch": document["provider_batch"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "collect-batch":
            document = collect_batch_replay(Path(args.run_root))
            print(json.dumps(document, indent=2))
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
