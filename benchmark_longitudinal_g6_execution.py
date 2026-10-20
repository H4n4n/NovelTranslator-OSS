"""Journal-protected execution harness for GOAT G6 literary calibration.

G6C executes the frozen 40-call primary-translation reasoning calibration.  The
module is dry-run by default and reads no credential unless --execute is
explicitly supplied.  Every new paid request runs underneath the repository's
existing durable paid-request journal; ambiguous started/uncertain state blocks
automatic reissue.

G6 intentionally calibrates PRIMARY TRANSLATION reasoning only.  It uses the
production translation profile and novel addendum, an empty chapter glossary,
and no continuity context.  For a given source case, messages are identical
across models; for a given model/source pair, effort arms differ only by the
exact G5C-live-verified reasoning_effort control.

Importing this module performs no I/O, network activity, credential access, or
writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from benchmark_novel import build_benchmark_user_prompt
from benchmark_longitudinal_capability_results import (
    verified_reasoning_capability,
)
from benchmark_longitudinal_config import CHAT_COMPLETIONS_PATH, PROVIDER_BASE_URL
from benchmark_longitudinal_g6_blind import G6_BLIND_PROTOCOL_SHA256
from benchmark_longitudinal_g6_calibration import (
    AUTOMATIC_RETRIES,
    G6A_MANIFEST_SHA256,
    G6A_REVIEW_SHA256,
    G6A_SELECTION_FILE_SHA256,
    G6A_SELECTION_SEMANTIC_SHA256,
    G6A_TARGETED_TESTS_SHA256,
    G6_CALIBRATION_CASES,
    G6_CALIBRATION_FREEZE_SHA256,
    G6_CALIBRATION_REVISION,
    G6_PRIMARY_CALL_COUNT,
    G6_SOURCE_CASES,
    MAX_COMPLETION_TOKENS,
    source_case,
    TRANSLATION_CONTEXT_MODE,
    ZDR_ENABLED,
)
from benchmark_longitudinal_provider import (
    PROVIDER_NAME,
    build_provider_request,
)
from chapter_library import (
    chapter_production_index,
    source_language_prompt_label,
    source_prompt_heading,
)
from novel_project import SOURCE_TYPE_TXT, load_novel_project
from production_paid_request_journal import (
    PaidRequestUncertainError,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)
from translation_profile import (
    effective_translation_instructions,
    load_translation_profile,
)


EXECUTION_SCHEMA_VERSION = 1
EXECUTION_REVISION = "goat-g6-literary-execution-v1"
EXECUTION_OPERATION_KIND = "goat_g6_translation"
EXECUTION_API_KEY_ENV = "COMMAND_CODE_API_KEY"
EXECUTION_TIMEOUT_SECONDS = 900
PROVIDER_CHAT_URL = PROVIDER_BASE_URL.rstrip("/") + CHAT_COMPLETIONS_PATH

G6A_EVIDENCE_FILES = {
    "g6a_source_selection.json": G6A_SELECTION_FILE_SHA256,
    "g6a_source_selection_review.txt": G6A_REVIEW_SHA256,
    "g6a_targeted_tests.txt": G6A_TARGETED_TESTS_SHA256,
    "g6a_source_selection_manifest.json": G6A_MANIFEST_SHA256,
}


class G6ExecutionError(RuntimeError):
    """Base class for G6 execution safety and provider-boundary failures."""


class G6ExecutionPlanError(G6ExecutionError):
    """Frozen source, prompt, plan, or evidence identity is unsafe."""


class G6ExecutionAuthorizationError(G6ExecutionError):
    """Operator authorization does not exactly match the required paid sends."""


class G6ExecutionRejectedError(G6ExecutionError):
    """A durable definitive Provider response failed transport acceptance."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6ExecutionPlanError(
            "G6 execution value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        dict(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6ExecutionPlanError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise G6ExecutionPlanError(f"{label} must contain a JSON object")
    return value


def _path_is_within(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or root in path.parents


def assert_external_evidence_root(
    evidence_root: Path | str,
    repo_root: Path | str,
) -> Path:
    """Require paid evidence outside the production repository."""
    evidence = Path(evidence_root).resolve()
    repo = Path(repo_root).resolve()
    if _path_is_within(evidence, repo):
        raise G6ExecutionPlanError(
            "G6 execution evidence must remain outside the production repository"
        )
    return evidence


def verify_g6a_evidence(g6a_root: Path | str) -> dict[str, Any]:
    """Bind the local G6A source-only selection bytes before any G6 request."""
    root = Path(g6a_root).resolve()
    observed: dict[str, str] = {}

    for name, expected in G6A_EVIDENCE_FILES.items():
        path = root / name
        if not path.is_file():
            raise G6ExecutionPlanError(f"missing frozen G6A evidence: {path}")
        actual = _sha256_file(path)
        if actual != expected:
            raise G6ExecutionPlanError(
                f"G6A evidence SHA256 mismatch for {name}: {actual} != {expected}"
            )
        observed[name] = actual

    selection = _load_json_object(
        root / "g6a_source_selection.json",
        "G6A source selection",
    )
    manifest = _load_json_object(
        root / "g6a_source_selection_manifest.json",
        "G6A manifest",
    )

    if selection.get("selection_revision") != "goat-g6-source-stress-v1":
        raise G6ExecutionPlanError("unexpected G6A source-selection revision")
    if selection.get("selection_sha256") != G6A_SELECTION_SEMANTIC_SHA256:
        raise G6ExecutionPlanError("G6A semantic selection SHA does not match freeze")
    if selection.get("provider_calls") != 0:
        raise G6ExecutionPlanError("G6A evidence unexpectedly reports provider calls")
    for key in (
        "model_outputs_consulted",
        "glossary_consulted",
        "continuity_consulted",
    ):
        if selection.get(key) is not False:
            raise G6ExecutionPlanError(f"G6A evidence must freeze {key}=false")

    if manifest.get("g6_literary_calls_authorized") is not False:
        raise G6ExecutionPlanError(
            "G6A manifest must not independently authorize literary calls"
        )

    return {
        "root": str(root),
        "files": observed,
        "selection_semantic_sha256": selection["selection_sha256"],
    }


@dataclass(frozen=True)
class PromptBundle:
    novel_id: str
    source_language: str
    chapter: int
    profile_id: str
    source_sha256: str
    chapter_library_sha256: str
    text_sha256: str
    messages: tuple[tuple[str, str], ...]
    system_prompt_sha256: str
    user_prompt_sha256: str
    messages_sha256: str

    def message_dicts(self) -> list[dict[str, str]]:
        return [
            {"role": role, "content": content}
            for role, content in self.messages
        ]

    def public_identity(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "chapter": self.chapter,
            "profile_id": self.profile_id,
            "source_sha256": self.source_sha256,
            "chapter_library_sha256": self.chapter_library_sha256,
            "text_sha256": self.text_sha256,
            "system_prompt_sha256": self.system_prompt_sha256,
            "user_prompt_sha256": self.user_prompt_sha256,
            "messages_sha256": self.messages_sha256,
        }


def _read_chapter_library(project) -> dict[int, dict[str, Any]]:
    path = project.chapters_file
    if not path.is_file():
        raise G6ExecutionPlanError(
            f"canonical chapter library is missing for {project.config.id}: {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6ExecutionPlanError(
            f"cannot read canonical chapter library for {project.config.id}: {exc}"
        ) from exc
    if not isinstance(raw, list):
        raise G6ExecutionPlanError("canonical chapter library must contain a JSON array")

    result: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise G6ExecutionPlanError("chapter library contains a non-object entry")
        try:
            number = chapter_production_index(item)
        except ValueError as exc:
            raise G6ExecutionPlanError(f"invalid chapter-library entry: {exc}") from exc
        if number in result:
            raise G6ExecutionPlanError(f"duplicate canonical Chapter {number}")
        result[number] = item
    return result


def load_prompt_bundle(
    source,
    repo_root: Path | str,
) -> PromptBundle:
    """Reconstruct one frozen G6 production-style prompt from canonical source."""
    repo = Path(repo_root).resolve()
    project = load_novel_project(source.novel_id, repo)

    if project.config.id != source.novel_id:
        raise G6ExecutionPlanError("loaded novel id differs from frozen G6 source case")
    if project.config.source_language != source.source_language:
        raise G6ExecutionPlanError("loaded novel source language differs from G6 freeze")
    if project.config.target_language != "en":
        raise G6ExecutionPlanError("G6 calibration requires English target language")

    source_path = (
        project.source_txt
        if project.config.source_type == SOURCE_TYPE_TXT
        else project.source_epub
    )
    if not source_path.is_file():
        raise G6ExecutionPlanError(f"canonical source file is missing: {source_path}")
    actual_source_sha = _sha256_file(source_path)
    if actual_source_sha != source.source_sha256:
        raise G6ExecutionPlanError(
            f"source SHA drift for {source.novel_id}: "
            f"{actual_source_sha} != {source.source_sha256}"
        )

    actual_library_sha = _sha256_file(project.chapters_file)
    if actual_library_sha != source.chapter_library_sha256:
        raise G6ExecutionPlanError(
            f"chapter-library SHA drift for {source.novel_id}: "
            f"{actual_library_sha} != {source.chapter_library_sha256}"
        )

    chapters = _read_chapter_library(project)
    chapter = chapters.get(source.chapter)
    if chapter is None:
        raise G6ExecutionPlanError(
            f"frozen G6 Chapter {source.chapter} is absent for {source.novel_id}"
        )
    text = chapter.get("text")
    if not isinstance(text, str) or not text.strip():
        raise G6ExecutionPlanError(
            f"frozen G6 Chapter {source.chapter} has no source text"
        )
    actual_text_sha = _sha256_text(text)
    if actual_text_sha != source.text_sha256:
        raise G6ExecutionPlanError(
            f"source-text SHA drift for {source.novel_id} Chapter {source.chapter}: "
            f"{actual_text_sha} != {source.text_sha256}"
        )

    profile = load_translation_profile(
        project.config.profile,
        project.repository_root,
    )
    system_prompt = effective_translation_instructions(
        profile,
        project.translation_addendum_file,
    )
    user_prompt = build_benchmark_user_prompt(
        source.chapter,
        text,
        "",
        source_prompt_heading(
            chapter,
            project.config.chapter_numbering,
            source_language=project.config.source_language,
            target_language=project.config.target_language,
        ),
        source_language_prompt_label(project.config.source_language),
    )
    messages = (
        ("system", system_prompt),
        ("user", user_prompt),
    )
    message_dicts = [
        {"role": role, "content": content}
        for role, content in messages
    ]

    return PromptBundle(
        novel_id=source.novel_id,
        source_language=source.source_language,
        chapter=source.chapter,
        profile_id=profile.id,
        source_sha256=source.source_sha256,
        chapter_library_sha256=source.chapter_library_sha256,
        text_sha256=source.text_sha256,
        messages=messages,
        system_prompt_sha256=_sha256_text(system_prompt),
        user_prompt_sha256=_sha256_text(user_prompt),
        messages_sha256=_sha256_json(message_dicts),
    )


def load_all_prompt_bundles(
    repo_root: Path | str,
) -> dict[tuple[str, int], PromptBundle]:
    result = {}
    for source in G6_SOURCE_CASES:
        bundle = load_prompt_bundle(source, repo_root)
        key = (source.novel_id, source.chapter)
        if key in result:
            raise G6ExecutionPlanError(f"duplicate G6 prompt source identity: {key}")
        result[key] = bundle
    if len(result) != 4:
        raise G6ExecutionPlanError("G6 must reconstruct exactly four prompt bundles")
    return result


@dataclass(frozen=True)
class G6RuntimeCase:
    index: int
    calibration_case: Any
    prompt: PromptBundle
    request_body: dict[str, Any]
    identity: dict[str, Any]
    journal_path: Path

    def plan_identity(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "case": self.calibration_case.identity(),
            "prompt": self.prompt.public_identity(),
            "request_sha256": self.identity["request_sha256"],
            "operation_id": self.identity["operation_id"],
            "journal_file": self.journal_path.name,
        }


def build_runtime_cases(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], PromptBundle] | None = None,
) -> tuple[G6RuntimeCase, ...]:
    root = Path(evidence_root)
    prompts = (
        dict(prompt_bundles)
        if prompt_bundles is not None
        else load_all_prompt_bundles(repo_root)
    )
    journal_dir = root / "journal"
    runtime: list[G6RuntimeCase] = []

    for index, case in enumerate(G6_CALIBRATION_CASES, start=1):
        prompt = prompts.get((case.novel_id, case.chapter))
        if prompt is None:
            raise G6ExecutionPlanError(
                f"missing prompt bundle for {(case.novel_id, case.chapter)!r}"
            )
        frozen_source = source_case(case.novel_id, case.chapter)
        if prompt.text_sha256 != case.text_sha256:
            raise G6ExecutionPlanError("prompt text SHA differs from calibration case")
        if prompt.source_sha256 != frozen_source.source_sha256:
            raise G6ExecutionPlanError("prompt source SHA differs from frozen source case")
        if prompt.chapter_library_sha256 != frozen_source.chapter_library_sha256:
            raise G6ExecutionPlanError(
                "prompt chapter-library SHA differs from frozen source case"
            )
        if prompt.source_language != frozen_source.source_language:
            raise G6ExecutionPlanError(
                "prompt source language differs from frozen source case"
            )

        capability = None
        reasoning_level = None
        if case.effort is not None:
            capability = verified_reasoning_capability(case.benchmark_model_id)
            if capability is None:
                raise G6ExecutionPlanError(
                    f"missing live reasoning capability for {case.benchmark_model_id}"
                )
            reasoning_level = case.effort
        elif verified_reasoning_capability(case.benchmark_model_id) is not None:
            raise G6ExecutionPlanError(
                "adjustable model cannot silently use default reasoning in G6"
            )

        request_body = build_provider_request(
            model=case.provider_model_id,
            messages=prompt.message_dicts(),
            max_output_tokens=case.max_completion_tokens,
            reasoning_capability=capability,
            reasoning_level=reasoning_level,
        )
        identity = request_identity(
            operation_kind=EXECUTION_OPERATION_KIND,
            novel_id=case.novel_id,
            chapter=index,
            provider=PROVIDER_NAME,
            model=case.provider_model_id,
            profile_id=prompt.profile_id,
            source_sha256=case.text_sha256,
            request_payload=request_body,
        )
        runtime.append(
            G6RuntimeCase(
                index=index,
                calibration_case=case,
                prompt=prompt,
                request_body=request_body,
                identity=identity,
                journal_path=paid_request_journal_path(
                    journal_dir,
                    EXECUTION_OPERATION_KIND,
                    index,
                ),
            )
        )

    if len(runtime) != G6_PRIMARY_CALL_COUNT:
        raise G6ExecutionPlanError("frozen G6 runtime call count changed")

    # For every source case, all models/efforts receive byte-identical messages.
    by_source: dict[tuple[str, int], set[str]] = {}
    for item in runtime:
        key = (item.calibration_case.novel_id, item.calibration_case.chapter)
        by_source.setdefault(key, set()).add(item.prompt.messages_sha256)
    if any(len(values) != 1 for values in by_source.values()):
        raise G6ExecutionPlanError("G6 messages differ across arms for the same source")

    # For a model/source pair, effort arms may differ ONLY by reasoning_effort.
    by_model_source: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for item in runtime:
        key = (
            item.calibration_case.benchmark_model_id,
            item.calibration_case.novel_id,
            item.calibration_case.chapter,
        )
        body = dict(item.request_body)
        body.pop("reasoning_effort", None)
        by_model_source.setdefault(key, []).append(body)
    for values in by_model_source.values():
        canonical = {_canonical_json(value) for value in values}
        if len(canonical) != 1:
            raise G6ExecutionPlanError(
                "paired G6 effort arms differ outside reasoning_effort"
            )

    return tuple(runtime)


def execution_plan_identity(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], PromptBundle] | None = None,
) -> dict[str, Any]:
    cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=evidence_root,
        prompt_bundles=prompt_bundles,
    )
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_revision": EXECUTION_REVISION,
        "g6_calibration_revision": G6_CALIBRATION_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "provider_base_url": PROVIDER_BASE_URL,
        "endpoint_path": CHAT_COMPLETIONS_PATH,
        "translation_context_mode": TRANSLATION_CONTEXT_MODE,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "timeout_seconds": EXECUTION_TIMEOUT_SECONDS,
        "automatic_retries": AUTOMATIC_RETRIES,
        "zdr_enabled": ZDR_ENABLED,
        "case_count": len(cases),
        "cases": [item.plan_identity() for item in cases],
    }


def execution_plan_sha256(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], PromptBundle] | None = None,
) -> str:
    return _sha256_json(
        execution_plan_identity(
            repo_root=repo_root,
            evidence_root=evidence_root,
            prompt_bundles=prompt_bundles,
        )
    )


def _response_header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def send_translation_http_once(
    *,
    api_key: str,
    request_body: Mapping[str, Any],
    url: str = PROVIDER_CHAT_URL,
    timeout_seconds: int = EXECUTION_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Perform one direct Provider POST; HTTP errors are durable responses."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise G6ExecutionAuthorizationError(
            f"{EXECUTION_API_KEY_ENV} must contain a Command Code API key"
        )
    if url != PROVIDER_CHAT_URL:
        raise G6ExecutionPlanError("G6 URL must be the exact Provider endpoint")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 1
    ):
        raise G6ExecutionPlanError("G6 Provider timeout must be a positive integer")

    payload = _canonical_json(dict(request_body)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NovelTranslator-GOAT-G6/1",
        },
    )

    try:
        response = opener(request, timeout=timeout_seconds)
        try:
            status = int(response.getcode())
            raw_body = response.read()
            headers = getattr(response, "headers", None)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw_body = exc.read()
        headers = getattr(exc, "headers", None)

    try:
        body_json = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        body_json = None

    return {
        "http_status": status,
        "body_json": body_json,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "request_id": (
            _response_header(headers, "x-request-id")
            or _response_header(headers, "x-cmd-request-id")
        ),
    }


def normalize_translation_http_result(
    raw: Mapping[str, Any],
    *,
    expected_model: str,
) -> dict[str, Any]:
    status = raw.get("http_status")
    body = raw.get("body_json")
    errors: list[str] = []

    if not isinstance(status, int) or isinstance(status, bool):
        errors.append("missing_or_invalid_http_status")
        status = None

    response_id = None
    response_model = None
    finish_reason = None
    content = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    if isinstance(body, Mapping):
        raw_id = body.get("id")
        if isinstance(raw_id, str):
            response_id = raw_id

        raw_model = body.get("model")
        if isinstance(raw_model, str):
            response_model = raw_model

        choices = body.get("choices")
        if isinstance(choices, list) and len(choices) == 1:
            choice = choices[0]
            if isinstance(choice, Mapping):
                raw_finish = choice.get("finish_reason")
                if raw_finish is None or isinstance(raw_finish, str):
                    finish_reason = raw_finish
                else:
                    errors.append("invalid_finish_reason")

                message = choice.get("message")
                if isinstance(message, Mapping):
                    raw_content = message.get("content")
                    if raw_content is None:
                        content = ""
                    elif isinstance(raw_content, str):
                        content = raw_content
                    else:
                        errors.append("invalid_content")
                else:
                    errors.append("missing_message")
            else:
                errors.append("invalid_choice")
        elif status == 200:
            errors.append("expected_exactly_one_choice")

        raw_usage = body.get("usage")
        if isinstance(raw_usage, Mapping):
            usage = {
                "prompt_tokens": raw_usage.get("prompt_tokens"),
                "completion_tokens": raw_usage.get("completion_tokens"),
                "total_tokens": raw_usage.get("total_tokens"),
            }
            details = raw_usage.get("prompt_tokens_details")
            if isinstance(details, Mapping):
                usage["cached_tokens"] = details.get("cached_tokens")

        raw_error = body.get("error")
        if isinstance(raw_error, Mapping):
            error = {
                "type": raw_error.get("type"),
                "code": raw_error.get("code"),
                "param": raw_error.get("param"),
                "message": (
                    str(raw_error.get("message"))[:1000]
                    if raw_error.get("message") is not None
                    else None
                ),
            }
    else:
        errors.append("response_body_not_json")

    return {
        "http_status": status,
        "expected_model": expected_model,
        "provider_response_id": response_id,
        "response_model": response_model,
        "finish_reason": finish_reason,
        "content": content,
        "usage": usage,
        "error": error,
        "normalization_errors": errors,
        "body_sha256": raw.get("body_sha256"),
        "request_id": raw.get("request_id"),
    }


def evaluate_translation_response(
    response: Mapping[str, Any],
    *,
    runtime_case: G6RuntimeCase,
) -> dict[str, Any]:
    """Separate Provider-contract acceptance from literary defect diagnostics."""
    reasons: list[str] = []

    if response.get("http_status") != 200:
        reasons.append(f"http_status_{response.get('http_status')}")

    expected_model = runtime_case.calibration_case.provider_model_id
    if response.get("response_model") != expected_model:
        reasons.append("provider_model_identity_mismatch")

    normalization_errors = response.get("normalization_errors")
    if isinstance(normalization_errors, list) and normalization_errors:
        reasons.extend(
            f"normalization:{item}"
            for item in normalization_errors
            if isinstance(item, str)
        )

    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        reasons.append("missing_usage")
    else:
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                reasons.append(f"missing_or_invalid_{name}")

    content = response.get("content")
    if content is not None and not isinstance(content, str):
        reasons.append("invalid_translation_content")

    provider_accepted = not reasons
    text = content if isinstance(content, str) else ""
    finish_reason = response.get("finish_reason")

    return {
        "provider_accepted": provider_accepted,
        "provider_rejection_reasons": reasons,
        "benchmark_model_id": runtime_case.calibration_case.benchmark_model_id,
        "provider_model_id": runtime_case.calibration_case.provider_model_id,
        "novel_id": runtime_case.calibration_case.novel_id,
        "chapter": runtime_case.calibration_case.chapter,
        "stress_axis": runtime_case.calibration_case.stress_axis,
        "effort": runtime_case.calibration_case.effort,
        "literary_diagnostics": {
            "empty_output": provider_accepted and text == "",
            "finish_reason_length": provider_accepted and finish_reason == "length",
            "finish_reason": finish_reason,
            "translation_sha256": _sha256_text(text) if provider_accepted else None,
            "translation_characters": len(text) if provider_accepted else None,
            "translation_words": len(text.split()) if provider_accepted else None,
            "preserve_without_automatic_retry": provider_accepted,
        },
    }


def inspect_execution_state(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], PromptBundle] | None = None,
) -> dict[str, Any]:
    """Read journals only; ambiguous or definitive rejected state blocks sends."""
    cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=evidence_root,
        prompt_bundles=prompt_bundles,
    )
    records = []
    new_calls_required = 0

    for item in cases:
        record = load_matching_journal(item.journal_path, item.identity)
        status = None if record is None else record["status"]

        if status in {"started", "uncertain"}:
            raise PaidRequestUncertainError(
                "G6 contains an ambiguous paid request; automatic continuation "
                f"is blocked: {item.journal_path}"
            )

        accepted = None
        if status == "response_recorded":
            evaluation = evaluate_translation_response(
                record["response"],
                runtime_case=item,
            )
            accepted = evaluation["provider_accepted"]
            if not accepted:
                raise G6ExecutionRejectedError(
                    "G6 contains a durable definitive Provider rejection; "
                    f"automatic continuation is blocked at {item.calibration_case.case_id}: "
                    f"{evaluation['provider_rejection_reasons']}"
                )
        else:
            new_calls_required += 1

        records.append(
            {
                "index": item.index,
                "case_id": item.calibration_case.case_id,
                "benchmark_model_id": item.calibration_case.benchmark_model_id,
                "provider_model_id": item.calibration_case.provider_model_id,
                "novel_id": item.calibration_case.novel_id,
                "chapter": item.calibration_case.chapter,
                "effort": item.calibration_case.effort,
                "journal_status": status or "absent",
                "provider_accepted": accepted,
            }
        )

    return {
        "case_count": len(cases),
        "new_calls_required": new_calls_required,
        "records": records,
    }


def _write_plan_if_absent(root: Path, plan: Mapping[str, Any]) -> Path:
    path = root / "execution_plan.json"
    if path.exists():
        existing = _load_json_object(path, "existing G6 execution plan")
        if _sha256_json(existing) != _sha256_json(plan):
            raise G6ExecutionPlanError(
                "existing G6 execution_plan.json does not match frozen plan"
            )
        return path
    _atomic_write_json(path, plan)
    return path


def _usage_totals(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    for record in results:
        response = record.get("response")
        if not isinstance(response, Mapping):
            continue
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for name in totals:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[name] += value
    return totals


def run_execution_suite(
    *,
    repo_root: Path | str,
    g6a_evidence_root: Path | str,
    evidence_root: Path | str,
    api_key: str,
    authorize_new_calls: int,
    sender: Callable[..., Mapping[str, Any]] = send_translation_http_once,
    prompt_bundles: Mapping[tuple[str, int], PromptBundle] | None = None,
) -> dict[str, Any]:
    """Execute/replay frozen G6 sequentially under durable paid journals."""
    repo = Path(repo_root).resolve()
    root = assert_external_evidence_root(evidence_root, repo)
    g6a_binding = verify_g6a_evidence(g6a_evidence_root)
    runtime_cases = build_runtime_cases(
        repo_root=repo,
        evidence_root=root,
        prompt_bundles=prompt_bundles,
    )
    state = inspect_execution_state(
        repo_root=repo,
        evidence_root=root,
        prompt_bundles=prompt_bundles,
    )

    if (
        not isinstance(authorize_new_calls, int)
        or isinstance(authorize_new_calls, bool)
        or authorize_new_calls < 0
    ):
        raise G6ExecutionAuthorizationError(
            "authorized new-call count must be a non-negative integer"
        )
    if authorize_new_calls != state["new_calls_required"]:
        raise G6ExecutionAuthorizationError(
            "authorized new-call count does not equal exact remaining G6 sends: "
            f"authorized={authorize_new_calls}, required={state['new_calls_required']}"
        )
    if authorize_new_calls and (
        not isinstance(api_key, str) or not api_key.strip()
    ):
        raise G6ExecutionAuthorizationError(
            f"{EXECUTION_API_KEY_ENV} is required before any new Provider send"
        )

    root.mkdir(parents=True, exist_ok=True)
    plan = execution_plan_identity(
        repo_root=repo,
        evidence_root=root,
        prompt_bundles=prompt_bundles,
    )
    plan_path = _write_plan_if_absent(root, plan)
    result_dir = root / "results"
    result_dir.mkdir(parents=True, exist_ok=True)

    api_calls_this_run = 0
    results: list[dict[str, Any]] = []

    for item in runtime_cases:
        case = item.calibration_case

        def prepare_attempt(item=item) -> Mapping[str, Any]:
            return {
                "execution_revision": EXECUTION_REVISION,
                "g6_calibration_revision": G6_CALIBRATION_REVISION,
                "case_id": item.calibration_case.case_id,
                "case_index": item.index,
                "benchmark_model_id": item.calibration_case.benchmark_model_id,
                "provider_model_id": item.calibration_case.provider_model_id,
                "novel_id": item.calibration_case.novel_id,
                "source_chapter": item.calibration_case.chapter,
                "stress_axis": item.calibration_case.stress_axis,
                "effort": item.calibration_case.effort,
                "messages_sha256": item.prompt.messages_sha256,
                "request_sha256": item.identity["request_sha256"],
                "provider_url": PROVIDER_CHAT_URL,
                "timeout_seconds": EXECUTION_TIMEOUT_SECONDS,
                "max_completion_tokens": item.calibration_case.max_completion_tokens,
                "zdr_enabled": False,
            }

        def send_once(item=item) -> Mapping[str, Any]:
            return sender(
                api_key=api_key,
                request_body=item.request_body,
                url=PROVIDER_CHAT_URL,
                timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            )

        def normalize(raw, _attempt, item=item) -> Mapping[str, Any]:
            if not isinstance(raw, Mapping):
                return {
                    "http_status": None,
                    "expected_model": item.calibration_case.provider_model_id,
                    "provider_response_id": None,
                    "response_model": None,
                    "finish_reason": None,
                    "content": None,
                    "usage": None,
                    "error": None,
                    "normalization_errors": ["raw_http_result_not_mapping"],
                    "body_sha256": None,
                    "request_id": None,
                }
            return normalize_translation_http_result(
                raw,
                expected_model=item.calibration_case.provider_model_id,
            )

        journal_result = run_journaled_paid_request(
            journal_path=item.journal_path,
            identity=item.identity,
            save_json=_atomic_write_json,
            prepare_attempt=prepare_attempt,
            send_once=send_once,
            normalize_response=normalize,
        )

        if journal_result["api_called"]:
            api_calls_this_run += 1

        response = journal_result["response"]
        evaluation = evaluate_translation_response(
            response,
            runtime_case=item,
        )
        result_record = {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "execution_revision": EXECUTION_REVISION,
            "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
            "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
            "case": item.plan_identity(),
            "response": response,
            "evaluation": evaluation,
        }
        result_path = result_dir / f"{item.index:04d}.json"
        _atomic_write_json(result_path, result_record)
        results.append(result_record)

        if not evaluation["provider_accepted"]:
            summary = {
                "schema_version": EXECUTION_SCHEMA_VERSION,
                "execution_revision": EXECUTION_REVISION,
                "plan_sha256": _sha256_json(plan),
                "status": "stopped_on_definitive_provider_rejection",
                "api_calls_this_run": api_calls_this_run,
                "completed_case_count": len(results),
                "failed_case_index": item.index,
                "failed_case_id": case.case_id,
                "provider_rejection_reasons": (
                    evaluation["provider_rejection_reasons"]
                ),
            }
            _atomic_write_json(root / "execution_summary.json", summary)
            raise G6ExecutionRejectedError(
                "G6 stopped after durable definitive Provider rejection at "
                f"{case.case_id}: {evaluation['provider_rejection_reasons']}"
            )

    literary_flags = {
        "empty_output_count": sum(
            1
            for record in results
            if record["evaluation"]["literary_diagnostics"]["empty_output"]
        ),
        "finish_reason_length_count": sum(
            1
            for record in results
            if record["evaluation"]["literary_diagnostics"]["finish_reason_length"]
        ),
    }

    summary = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_revision": EXECUTION_REVISION,
        "plan_sha256": _sha256_json(plan),
        "status": "provider_accepted_complete",
        "case_count": len(results),
        "provider_accepted_count": sum(
            1 for record in results if record["evaluation"]["provider_accepted"]
        ),
        "api_calls_this_run": api_calls_this_run,
        "replayed_count_this_run": (
            len(results) - api_calls_this_run
        ),
        "provider_calls_total_bound": len(results),
        "usage_totals": _usage_totals(results),
        "literary_diagnostics": literary_flags,
        "g6a_selection_semantic_sha256": (
            g6a_binding["selection_semantic_sha256"]
        ),
        "results": [
            {
                "index": record["case"]["index"],
                "case_id": record["case"]["case"]["case_id"],
                "benchmark_model_id": record["evaluation"]["benchmark_model_id"],
                "provider_model_id": record["evaluation"]["provider_model_id"],
                "novel_id": record["evaluation"]["novel_id"],
                "chapter": record["evaluation"]["chapter"],
                "effort": record["evaluation"]["effort"],
                "translation_sha256": record["evaluation"][
                    "literary_diagnostics"
                ]["translation_sha256"],
                "finish_reason": record["evaluation"][
                    "literary_diagnostics"
                ]["finish_reason"],
            }
            for record in results
        ],
    }
    _atomic_write_json(root / "execution_summary.json", summary)

    result_hashes = {}
    for path in sorted(result_dir.glob("*.json")):
        result_hashes[path.name] = _sha256_file(path)
    journal_hashes = {}
    journal_dir = root / "journal"
    for path in sorted(journal_dir.glob("*.json")):
        journal_hashes[path.name] = _sha256_file(path)

    manifest = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "execution_revision": EXECUTION_REVISION,
        "g6_calibration_revision": G6_CALIBRATION_REVISION,
        "g6_calibration_freeze_sha256": G6_CALIBRATION_FREEZE_SHA256,
        "g6_blind_protocol_sha256": G6_BLIND_PROTOCOL_SHA256,
        "plan_sha256": _sha256_json(plan),
        "plan_file_sha256": _sha256_file(plan_path),
        "summary_sha256": _sha256_file(root / "execution_summary.json"),
        "status": "provider_accepted_complete",
        "new_provider_calls_this_run": api_calls_this_run,
        "automatic_retries": 0,
        "credentials_persisted": False,
        "zdr_enabled": False,
        "g6a_evidence": g6a_binding["files"],
        "result_files": result_hashes,
        "journal_files": journal_hashes,
    }
    _atomic_write_json(root / "execution_manifest.json", manifest)
    return summary


def read_execution_api_key() -> str:
    """Read the process-only API key only on explicit execution path."""
    return os.environ.get(EXECUTION_API_KEY_ENV, "")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run/replay journal-protected GOAT G6 literary calibration."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--g6a-evidence-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Enable paid execution path. Without this flag no credential is read.",
    )
    parser.add_argument("--authorize-new-calls", type=int)
    parser.add_argument("--confirm-calibration-revision")
    parser.add_argument("--confirm-blind-protocol-sha")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo = Path(args.repo_root).resolve()
    root = assert_external_evidence_root(args.evidence_root, repo)

    g6a = verify_g6a_evidence(args.g6a_evidence_root)
    plan = execution_plan_identity(
        repo_root=repo,
        evidence_root=root,
    )
    state = inspect_execution_state(
        repo_root=repo,
        evidence_root=root,
    )

    print(f"execution_revision={EXECUTION_REVISION}")
    print(f"g6_calibration_revision={G6_CALIBRATION_REVISION}")
    print(f"g6_calibration_freeze_sha256={G6_CALIBRATION_FREEZE_SHA256}")
    print(f"g6_blind_protocol_sha256={G6_BLIND_PROTOCOL_SHA256}")
    print(f"execution_plan_sha256={_sha256_json(plan)}")
    print(f"case_count={state['case_count']}")
    print(f"new_calls_required={state['new_calls_required']}")
    print(f"g6a_selection_sha256={g6a['selection_semantic_sha256']}")
    print(f"zdr_enabled={ZDR_ENABLED}")

    if not args.execute:
        if args.authorize_new_calls is not None:
            raise G6ExecutionAuthorizationError(
                "--authorize-new-calls is valid only together with --execute"
            )
        print("execution=disabled")
        return 0

    if args.confirm_calibration_revision != G6_CALIBRATION_REVISION:
        raise G6ExecutionAuthorizationError(
            "explicit execution requires the exact frozen G6 calibration revision"
        )
    if args.confirm_blind_protocol_sha != G6_BLIND_PROTOCOL_SHA256:
        raise G6ExecutionAuthorizationError(
            "explicit execution requires the exact frozen blind-protocol SHA"
        )
    if args.authorize_new_calls is None:
        raise G6ExecutionAuthorizationError(
            "--authorize-new-calls is required with --execute"
        )

    api_key = read_execution_api_key()
    summary = run_execution_suite(
        repo_root=repo,
        g6a_evidence_root=args.g6a_evidence_root,
        evidence_root=root,
        api_key=api_key,
        authorize_new_calls=args.authorize_new_calls,
    )
    print(f"status={summary['status']}")
    print(f"api_calls_this_run={summary['api_calls_this_run']}")
    print(f"provider_accepted_count={summary['provider_accepted_count']}")
    print(
        "finish_reason_length_count="
        f"{summary['literary_diagnostics']['finish_reason_length_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
