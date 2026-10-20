"""Persist immutable translation-model provenance in committed QA JSON files.

The legacy runner's ``local_qa`` function intentionally selects only established
commit-QA fields before writing ``output/qa/<chapter>.json``. That means adding
extra keys to ``translation_commit_qa`` itself is not enough: they are discarded
before persistence.

This runtime bridge therefore wraps the core's existing ``atomic_write_json``
function and augments only numeric JSON files written directly under the selected
QA directory. Commit eligibility, blockers, translation text, pending state,
and every non-QA JSON write are left unchanged.

The module is import-safe and performs no API calls or filesystem I/O until the
wrapped writer is actually used by production.
"""

from pathlib import Path

from translation_model_config import (
    pending_translation_model_record,
    resolve_translation_model,
)


PROVENANCE_MODE = "committed_qa_model_provenance"
_WRAPPER_STATE_ATTR = "_noveltranslator_committed_qa_provenance_state"


class CommittedQAProvenanceError(RuntimeError):
    """Fail-closed committed-QA provenance error."""


def committed_qa_model_record(selection=None):
    """Return the exact immutable model fields persisted with committed QA."""
    return pending_translation_model_record(selection)


def _normalized_chapter_set(values):
    chapters = set()
    for value in values or ():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CommittedQAProvenanceError(
                "excluded committed-QA chapters must be positive integers"
            )
        chapters.add(value)
    return frozenset(chapters)


def install_committed_qa_model_provenance(
    core_module,
    config,
    qa_dir="output/qa",
    *,
    excluded_chapters=(),
):
    """Add model provenance only when numeric committed-QA JSON is written.

    ``excluded_chapters`` is used for legacy paid pending translations whose
    generating model is genuinely unknown. Those chapters must remain unlabelled
    rather than being falsely attributed to the model selected for recovery.

    The wrapper composes on top of any already-installed pending-provenance
    writer. Idempotence is attached to the currently installed callable instead
    of a module-global flag so read-only preflights can restore core writers and
    later install a fresh wrapper normally.
    """
    resolved = resolve_translation_model(
        config.model if hasattr(config, "model") else config
    )
    qa_root = Path(qa_dir).resolve()
    excluded = _normalized_chapter_set(excluded_chapters)
    current = getattr(core_module, "atomic_write_json", None)
    if not callable(current):
        raise AttributeError("core module has no callable atomic_write_json")

    existing = getattr(current, _WRAPPER_STATE_ATTR, None)
    if isinstance(existing, dict):
        if (
            existing.get("model") != resolved.model
            or existing.get("qa_root") != qa_root
            or existing.get("excluded_chapters") != excluded
        ):
            raise CommittedQAProvenanceError(
                "committed QA provenance writer is already installed for a "
                "different model, QA directory, or exclusion set"
            )
        return existing

    provenance = committed_qa_model_record(resolved.model)
    original = current

    def qa_path(path):
        candidate = Path(path)
        try:
            parent = candidate.resolve().parent
        except OSError:
            return None
        if parent != qa_root or candidate.suffix != ".json" or not candidate.stem.isdigit():
            return None
        return candidate

    def wrapped_atomic_write_json(path, data):
        candidate = qa_path(path)
        payload = data
        if candidate is not None and int(candidate.stem) not in excluded:
            if not isinstance(data, dict):
                raise CommittedQAProvenanceError(
                    f"committed QA file {candidate.name} must be written from a dict"
                )
            payload = dict(data)
            payload.update(provenance)
        return original(path, payload)

    state = {
        "mode": PROVENANCE_MODE,
        "model": resolved.model,
        "model_version": resolved.version,
        "pricing_version": resolved.pricing_version,
        "qa_root": qa_root,
        "excluded_chapters": excluded,
        "original_atomic_write_json": original,
        "wrapped_atomic_write_json": wrapped_atomic_write_json,
        "provenance": dict(provenance),
    }
    setattr(wrapped_atomic_write_json, _WRAPPER_STATE_ATTR, state)
    core_module.atomic_write_json = wrapped_atomic_write_json
    return state
