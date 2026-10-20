"""Runtime bridge for selectable DeepSeek translation models.

The legacy production engine is intentionally left untouched while this branch
hardens model selection.  This module applies one validated model configuration
to the import-safe core before the production engine is executed, and pins new
paid pending translations to the model that generated them.

No API calls are made here.  Files are only read when resolving pending-model
provenance; writes happen solely through wrappers around the production core's
existing atomic write functions.
"""

import json
from production_failure_classification import NonProviderProductionError
from production_translation_recovery import validate_recovery_provenance
from production_translation_recovery_prefix import load_recorded_recovery_prefix
from pathlib import Path

from translation_model_config import (
    pending_model_compatibility,
    pending_translation_model_record,
    resolve_translation_model,
)


class TranslationModelRuntimeError(RuntimeError):
    """Fail-closed runtime model/provenance error."""


def _normalize_ignored_pending_chapters(values):
    chapters = set()
    for value in values or ():
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise TranslationModelRuntimeError(
                "ignored pending chapters must be positive integers"
            )
        chapters.add(value)
    return frozenset(chapters)


def _numeric_pending_records(pending_dir, *, ignored_pending_chapters=()):
    """Discover numeric Markdown pending and proven JSON-only recovery prefixes.

    The ignore set is an internal preflight bridge. Callers may supply only
    chapters independently proven as stale completed residue.
    """
    pending_dir = Path(pending_dir)
    ignored = _normalize_ignored_pending_chapters(
        ignored_pending_chapters
    )
    if not pending_dir.exists():
        return ()
    records = []
    stems = {path.stem for pattern in ("*.md", "*.json") for path in pending_dir.glob(pattern)
             if path.stem.isdigit()}
    for stem in sorted(stems):
        chapter = int(stem)
        if chapter in ignored:
            continue
        text_path = pending_dir / f"{stem}.md"
        meta_path = pending_dir / f"{stem}.json"
        meta = {}
        if meta_path.exists():
            try:
                with meta_path.open("r", encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise TranslationModelRuntimeError(
                    f"pending Chapter {int(text_path.stem)} metadata is unreadable: {exc}"
                ) from exc
            if not isinstance(meta, dict):
                raise TranslationModelRuntimeError(
                    f"pending Chapter {int(text_path.stem)} metadata must be a JSON object"
                )
        if not text_path.exists():
            # JSON alone never supplies a model pin without the completed paid
            # response chain. Ordinary orphan metadata remains a hard stop.
            try:
                load_recorded_recovery_prefix(meta_path, meta)
            except (NonProviderProductionError, OSError, ValueError, KeyError, TypeError) as exc:
                raise TranslationModelRuntimeError(
                    f"pending Chapter {chapter} metadata-only recovery proof failed: {exc}"
                ) from exc
        records.append({
            "chapter": chapter,
            "text_path": text_path,
            "meta_path": meta_path,
            "meta": meta,
        })
    return tuple(records)


def resolve_runtime_model(
    selection=None,
    pending_dir="output/pending",
    *,
    ignored_pending_chapters=(),
):
    """Resolve the effective model, honoring one already-paid pinned pending job.

    A pinned pending translation wins over a newly requested default/override so
    continuation and any model-dependent repair work stay on its original model.
    Legacy unpinned pending output remains recoverable and does not get guessed.
    Multiple pending translations fail closed because production is sequential.
    """
    requested = resolve_translation_model(selection)
    records = _numeric_pending_records(
        pending_dir,
        ignored_pending_chapters=ignored_pending_chapters,
    )
    if len(records) > 1:
        chapters = ", ".join(str(item["chapter"]) for item in records)
        raise TranslationModelRuntimeError(
            "multiple pending translations are incompatible with the sequential "
            f"runtime model resolver: Chapters {chapters}"
        )
    if not records:
        return {
            "status": "requested",
            "requested_model": requested.model,
            "effective_model": requested.model,
            "config": requested,
            "pending_chapter": None,
        }

    record = records[0]
    compatibility = pending_model_compatibility(record["meta"], requested.model)
    if compatibility["status"] == "unknown_pending_model":
        raise TranslationModelRuntimeError(
            f"pending Chapter {record['chapter']} uses unknown model "
            f"{compatibility['pending_model']!r}; refusing to guess"
        )
    if compatibility["status"] == "legacy_unpinned":
        return {
            "status": "legacy_unpinned",
            "requested_model": requested.model,
            "effective_model": requested.model,
            "config": requested,
            "pending_chapter": record["chapter"],
        }

    effective = resolve_translation_model(compatibility["pending_model"])
    return {
        "status": (
            "pending_model_override"
            if effective.model != requested.model
            else "pending_model_match"
        ),
        "requested_model": requested.model,
        "effective_model": effective.model,
        "config": effective,
        "pending_chapter": record["chapter"],
    }


def apply_core_model_config(core_module, config):
    """Apply one immutable model config to the legacy core module in memory.

    The legacy names ``V4_PRO_PRICES`` and ``PRICING_VERSION`` are retained so
    existing production code can be exercised without a risky large-file
    rewrite.  Their runtime values become the selected model's exact values.
    """
    resolved = resolve_translation_model(config.model if hasattr(config, "model") else config)
    core_module.PRODUCTION_TRANSLATION_MODEL = resolved.model
    core_module.PRODUCTION_TRANSLATION_MAX_TOKENS = resolved.max_tokens
    core_module.PRODUCTION_TRANSLATION_THINKING = resolved.thinking
    core_module.V4_PRO_PRICES = {
        period: dict(rates) for period, rates in resolved.prices.items()
    }
    core_module.PRICING_VERSION = resolved.pricing_version
    return resolved


def install_pending_model_provenance(core_module, config, pending_dir="output/pending"):
    """Pin only newly generated pending translations to the active model.

    Existing legacy pending translations are deliberately left unpinned.  The
    wrapper recognizes a new paid translation by the first creation of its
    numeric pending ``.md`` file in this process.  Subsequent metadata writes
    for that chapter carry exact model/request provenance.  Existing pinned
    metadata must match the effective runtime model or the write fails closed.
    """
    resolved = resolve_translation_model(config.model if hasattr(config, "model") else config)
    pending_root = Path(pending_dir).resolve()
    original_write_text = core_module.atomic_write_text
    original_write_json = core_module.atomic_write_json
    generated_chapters = set()

    def pending_path(path, suffix):
        candidate = Path(path)
        try:
            parent = candidate.resolve().parent
        except OSError:
            return None
        if parent != pending_root or candidate.suffix != suffix or not candidate.stem.isdigit():
            return None
        return candidate

    def write_text(path, text, encoding="utf-8"):
        candidate = pending_path(path, ".md")
        if candidate is not None and not candidate.exists():
            generated_chapters.add(int(candidate.stem))
        return original_write_text(path, text, encoding)

    def write_json(path, data):
        candidate = pending_path(path, ".json")
        payload = data
        if candidate is not None and isinstance(data, dict):
            chapter = int(candidate.stem)
            existing_model = data.get("translation_model")
            if existing_model:
                compatibility = pending_model_compatibility(data, resolved.model)
                if compatibility["status"] != "match":
                    raise TranslationModelRuntimeError(
                        f"pending Chapter {chapter} model provenance conflicts with "
                        f"runtime model {resolved.model}: {existing_model}"
                    )
            if chapter in generated_chapters or existing_model:
                if data.get("translation_recovery_used") is True:
                    validate_recovery_provenance(data)
                payload = dict(data)
                payload.update(pending_translation_model_record(resolved.model))
                if payload.get("translation_recovery_used") is True:
                    payload["translation_max_tokens"] = payload["translation_recovery_max_tokens"]
        return original_write_json(path, payload)

    core_module.atomic_write_text = write_text
    core_module.atomic_write_json = write_json
    return {
        "model": resolved.model,
        "generated_chapters": generated_chapters,
        "original_atomic_write_text": original_write_text,
        "original_atomic_write_json": original_write_json,
    }
