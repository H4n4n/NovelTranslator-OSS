"""Read-only proof/loading for interrupted translation-recovery pending writes.

No IO occurs at import. Startup validation and fresh-process model resolution
share this boundary before admitting metadata-only paid pending provenance.
"""
from pathlib import Path
import re

from production_paid_request_journal import load_matching_journal, paid_request_journal_path
from production_translation_recovery import TranslationRecoveryError, prove_recorded_pending_prefix, response_sha256


def load_recorded_recovery_prefix(meta_path, meta, *, verify_text=True):
    meta_path = Path(meta_path)
    if (not meta_path.stem.isdigit() or not isinstance(meta, dict)
        or type(meta.get("chapter")) is not int or meta["chapter"] < 1
        or meta_path.stem != f"{meta['chapter']:04d}"
        or meta.get("stage") != "translation_recovery_recorded"):
        raise TranslationRecoveryError("invalid recorded translation recovery prefix")
    chapter = meta["chapter"]
    journal_dir = meta_path.parent.parent.parent / "data" / "provider_request_journal"
    goat = meta.get("goat_reasoning_saturation_recovery_used") is True
    from production_provider_runtime import recovery_module
    recovery = recovery_module(meta) if goat else None
    KIND = recovery.KIND if goat else None
    records = []
    for kind, operation_key in (
        ("chapter_translation", "initial_translation_operation_id"),
        (KIND if goat else "chapter_translation_recovery", "recovery_translation_operation_id"),
    ):
        record = load_matching_journal(paid_request_journal_path(journal_dir, kind, chapter),
            {"operation_kind": kind, "chapter": chapter, "operation_id": meta.get(operation_key),
             "model": "deepseek/deepseek-v4.1-flash" if goat else "deepseek-flash",
             "provider": "command_code_provider" if goat else "deepseek"})
        if record is None:
            raise TranslationRecoveryError("missing recorded translation journal")
        stable_keys = ("revision", "operation_kind", "novel_id", "chapter", "provider", "model",
                       "profile_id", "source_sha256", "translation_sha256", "request_sha256")
        if (response_sha256({key: record[key] for key in stable_keys}) != record["operation_id"]
            or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", record[key])
                   for key in ("source_sha256", "request_sha256"))):
            raise TranslationRecoveryError("recorded translation journal operation identity mismatch")
        records.append(record)
    text_path = meta_path.with_suffix(".md")
    text = text_path.read_text(encoding="utf-8") if verify_text and text_path.exists() else None
    (recovery.prove_pending_prefix if goat else prove_recorded_pending_prefix)(meta, *records, text=text)
    return tuple(records)
