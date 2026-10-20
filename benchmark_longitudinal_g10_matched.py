"""Zero-provider G10A2 matched-literary plan and request construction.

This module freezes the 51 matched source pairs and 102 future translation
requests without providing a paid execution entry point. It reconstructs the
same production-style literary prompt used by the mature benchmark: production
translation profile plus novel addendum, empty chapter glossary, and no
continuity context.

Provider wire identity is kept separate from benchmark model identity. In
particular, the live-verified Qwen wire model is case-sensitive and differs
from the benchmark model identifier.

Importing this module performs no filesystem I/O, credential access, network
activity, provider-client construction, or writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from benchmark_novel import build_benchmark_user_prompt
from benchmark_longitudinal_capability_results import (
    live_provider_capability,
    verified_reasoning_capability,
)
from benchmark_longitudinal_g7j_reasoning_budget import (
    GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS,
)
from benchmark_longitudinal_g10_preregistration import (
    BRIDGE_LITERARY_CASES,
    G10_BENCHMARK_ID,
    G10_PREREGISTRATION_SHA256,
    GLM_MODEL_ID,
    MODEL_ORDER,
    MODEL_POLICY,
    NOVELS,
    PRIMARY_LITERARY_CHAPTERS,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import (
    G10A1_SAFETY_SHA256,
    assert_fresh_execution_root,
)
from benchmark_longitudinal_provider import (
    PROVIDER_NAME,
    build_provider_request,
    build_provider_request_identity,
)
from chapter_library import (
    chapter_production_index,
    source_language_prompt_label,
    source_prompt_heading,
)
from novel_project import SOURCE_TYPE_TXT, load_novel_project
from production_paid_request_journal import (
    paid_request_journal_path,
    request_identity,
)
from translation_profile import (
    effective_translation_instructions,
    load_translation_profile,
)


G10A2_SCHEMA_VERSION = 1
G10A2_REVISION = "g10-matched-literary-plan-v1"
G10A2_OPERATION_KIND = "g10a_translation"
G10A2_CONTEXT_MODE = "production_profile_addendum_empty_glossary_no_continuity"
G10A2_MAX_COMPLETION_TOKENS = GOAT_TRANSLATION_TOTAL_COMPLETION_TOKENS
G10A2_PRIMARY_PAIR_COUNT = 48
G10A2_BRIDGE_PAIR_COUNT = 3
G10A2_PAIR_COUNT = 51
G10A2_REQUEST_COUNT = 102

if G10A2_MAX_COMPLETION_TOKENS != 24_000:
    raise RuntimeError("G10A2 translation completion ceiling drifted")


class G10MatchedPlanError(RuntimeError):
    """The G10A2 matched-literary source, prompt, or request plan is unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10MatchedPlanError(
            "G10A2 value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_historical_config(novel_id: str, repo_root: Path | str) -> tuple[dict[str, Any], str]:
    """Verify the registered local Git blob; never substitute live config bytes.

    The returned SHA256 binds exact historical bytes, while canonical JSON
    verification tolerates no semantic drift. Git lazy fetching is disabled so
    missing objects fail locally without network or evidence mutation.
    """
    if novel_id not in NOVELS:
        raise G10MatchedPlanError(f"unknown G10 novel id: {novel_id!r}")
    expected = NOVELS[novel_id]
    blob_sha = expected["config_blob_sha"]
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", blob_sha], cwd=Path(repo_root).resolve(),
            capture_output=True, check=True,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise G10MatchedPlanError(
            f"{novel_id} historical config Git blob cannot be loaded: {blob_sha}"
        ) from exc
    blob = result.stdout
    header = f"blob {len(blob)}\0".encode("ascii")
    if hashlib.sha1(header + blob).hexdigest() != blob_sha:
        raise G10MatchedPlanError(f"{novel_id} historical config Git blob identity mismatch")
    try:
        config = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10MatchedPlanError(f"{novel_id} historical config is not valid UTF-8 JSON") from exc
    if not isinstance(config, dict):
        raise G10MatchedPlanError(f"{novel_id} historical config must contain a JSON object")
    if _sha256_json(config) != expected["config_canonical_sha256"]:
        raise G10MatchedPlanError(f"{novel_id} historical config canonical SHA mismatch")
    return config, hashlib.sha256(blob).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class G10PairSpec:
    pair_index: int
    pair_id: str
    case_kind: str
    novel_id: str
    chapter: int
    first_model_id: str

    def identity(self) -> dict[str, Any]:
        return {
            "pair_index": self.pair_index,
            "pair_id": self.pair_id,
            "case_kind": self.case_kind,
            "novel_id": self.novel_id,
            "chapter": self.chapter,
            "first_model_id": self.first_model_id,
        }


def planned_pair_specs() -> tuple[G10PairSpec, ...]:
    rows: list[tuple[str, str, int]] = []
    for novel_id, chapters in PRIMARY_LITERARY_CHAPTERS.items():
        rows.extend(("primary", novel_id, chapter) for chapter in chapters)
    rows.extend(("bridge", novel_id, chapter) for novel_id, chapter in BRIDGE_LITERARY_CASES)

    if len(rows) != G10A2_PAIR_COUNT:
        raise G10MatchedPlanError("G10A2 pair count drifted")
    if sum(1 for kind, _novel, _chapter in rows if kind == "primary") != 48:
        raise G10MatchedPlanError("G10A2 primary pair count drifted")
    if sum(1 for kind, _novel, _chapter in rows if kind == "bridge") != 3:
        raise G10MatchedPlanError("G10A2 bridge pair count drifted")

    result = []
    for pair_index, (case_kind, novel_id, chapter) in enumerate(rows, start=1):
        first_model = QWEN_MODEL_ID if pair_index % 2 else GLM_MODEL_ID
        result.append(
            G10PairSpec(
                pair_index=pair_index,
                pair_id=f"G10A-P{pair_index:03d}",
                case_kind=case_kind,
                novel_id=novel_id,
                chapter=chapter,
                first_model_id=first_model,
            )
        )
    return tuple(result)


PAIR_SPECS = planned_pair_specs()


@dataclass(frozen=True)
class G10PromptBundle:
    novel_id: str
    chapter: int
    source_language: str
    target_language: str
    profile_id: str
    config_sha256: str
    source_sha256: str
    chapter_library_sha256: str
    addendum_sha256: str
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
            "chapter": self.chapter,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "profile_id": self.profile_id,
            "config_sha256": self.config_sha256,
            "config_canonical_sha256": NOVELS[self.novel_id]["config_canonical_sha256"],
            "source_sha256": self.source_sha256,
            "chapter_library_sha256": self.chapter_library_sha256,
            "addendum_sha256": self.addendum_sha256,
            "text_sha256": self.text_sha256,
            "system_prompt_sha256": self.system_prompt_sha256,
            "user_prompt_sha256": self.user_prompt_sha256,
            "messages_sha256": self.messages_sha256,
            "context_mode": G10A2_CONTEXT_MODE,
        }


def _read_chapter_library(project) -> dict[int, dict[str, Any]]:
    path = project.chapters_file
    if not path.is_file():
        raise G10MatchedPlanError(
            f"canonical chapter library is missing for {project.config.id}: {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G10MatchedPlanError(
            f"cannot read canonical chapter library for {project.config.id}: {exc}"
        ) from exc
    if not isinstance(raw, list):
        raise G10MatchedPlanError("canonical chapter library must contain a JSON array")

    result: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise G10MatchedPlanError(
                "canonical chapter library contains a non-object entry"
            )
        try:
            number = chapter_production_index(item)
        except ValueError as exc:
            raise G10MatchedPlanError(
                f"invalid chapter-library entry: {exc}"
            ) from exc
        if number in result:
            raise G10MatchedPlanError(
                f"duplicate canonical Chapter {number}"
            )
        result[number] = item
    return result


def load_prompt_bundle(
    novel_id: str,
    chapter: int,
    repo_root: Path | str,
) -> G10PromptBundle:
    if novel_id not in NOVELS:
        raise G10MatchedPlanError(f"unknown G10 novel id: {novel_id!r}")
    expected = NOVELS[novel_id]
    repo = Path(repo_root).resolve()
    project = load_novel_project(novel_id, repo)

    config_checks = {
        "id": novel_id,
        "source_language": expected["source_language"],
        "target_language": expected["target_language"],
        "profile": expected["profile"],
        "final_chapter": expected["final_chapter"],
        "audit_interval": expected["audit_interval"],
        "chapter_numbering": expected["chapter_numbering"],
    }
    for field, wanted in config_checks.items():
        if getattr(project.config, field) != wanted:
            raise G10MatchedPlanError(
                f"{novel_id} configuration drifted for {field}"
            )
    _historical_config, actual_config_sha256 = load_historical_config(novel_id, repo)

    source_path = (
        project.source_txt
        if project.config.source_type == SOURCE_TYPE_TXT
        else project.source_epub
    )
    for label, path in {
        "novel config": project.config_file,
        "source": source_path,
        "chapter library": project.chapters_file,
        "translation addendum": project.translation_addendum_file,
    }.items():
        if not path.is_file():
            raise G10MatchedPlanError(f"{label} file is missing: {path}")

    chapters = _read_chapter_library(project)
    chapter_row = chapters.get(chapter)
    if chapter_row is None:
        raise G10MatchedPlanError(
            f"{novel_id} Chapter {chapter} is absent from canonical library"
        )
    text = chapter_row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise G10MatchedPlanError(
            f"{novel_id} Chapter {chapter} has no source text"
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
        chapter,
        text,
        "",
        source_prompt_heading(
            chapter_row,
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

    return G10PromptBundle(
        novel_id=novel_id,
        chapter=chapter,
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        profile_id=profile.id,
        config_sha256=actual_config_sha256,
        source_sha256=_sha256_file(source_path),
        chapter_library_sha256=_sha256_file(project.chapters_file),
        addendum_sha256=_sha256_file(project.translation_addendum_file),
        text_sha256=_sha256_text(text),
        messages=messages,
        system_prompt_sha256=_sha256_text(system_prompt),
        user_prompt_sha256=_sha256_text(user_prompt),
        messages_sha256=_sha256_json(message_dicts),
    )


def load_all_prompt_bundles(
    repo_root: Path | str,
) -> dict[tuple[str, int], G10PromptBundle]:
    result: dict[tuple[str, int], G10PromptBundle] = {}
    for pair in PAIR_SPECS:
        key = (pair.novel_id, pair.chapter)
        if key not in result:
            result[key] = load_prompt_bundle(
                pair.novel_id,
                pair.chapter,
                repo_root,
            )
    if len(result) != G10A2_PAIR_COUNT:
        raise G10MatchedPlanError(
            "G10A2 must reconstruct exactly 51 unique source prompt bundles"
        )
    return result


def assert_matched_evidence_root(
    evidence_root: Path | str,
    *,
    repo_root: Path | str,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> Path:
    evidence = assert_fresh_execution_root(
        evidence_root,
        forbidden_roots=(repo_root, *forbidden_roots),
    )
    repo = Path(repo_root).resolve()
    if _is_within(evidence, repo) or _is_within(repo, evidence):
        raise G10MatchedPlanError(
            "G10A2 evidence root must be external to the repository"
        )
    return evidence


@dataclass(frozen=True)
class G10MatchedRuntimeCase:
    execution_index: int
    pair: G10PairSpec
    benchmark_model_id: str
    provider_model_id: str
    effort: str
    prompt: G10PromptBundle
    request_body: dict[str, Any]
    provider_request_sha256: str
    journal_identity: dict[str, Any]
    journal_path: Path

    def plan_identity(self) -> dict[str, Any]:
        return {
            "execution_index": self.execution_index,
            "pair": self.pair.identity(),
            "benchmark_model_id": self.benchmark_model_id,
            "provider_model_id": self.provider_model_id,
            "effort": self.effort,
            "max_completion_tokens": G10A2_MAX_COMPLETION_TOKENS,
            "prompt": self.prompt.public_identity(),
            "request_sha256": self.provider_request_sha256,
            "journal_operation_id": self.journal_identity["operation_id"],
            "journal_file": self.journal_path.name,
        }


def _model_order_for_pair(pair: G10PairSpec) -> tuple[str, str]:
    second = (
        GLM_MODEL_ID
        if pair.first_model_id == QWEN_MODEL_ID
        else QWEN_MODEL_ID
    )
    return pair.first_model_id, second


def _journal_model_slug(model_id: str) -> str:
    if model_id == QWEN_MODEL_ID:
        return "qwen"
    if model_id == GLM_MODEL_ID:
        return "glm"
    raise G10MatchedPlanError(f"unexpected G10A2 model for journal path: {model_id!r}")


def build_runtime_cases(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], G10PromptBundle] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> tuple[G10MatchedRuntimeCase, ...]:
    evidence = assert_matched_evidence_root(
        evidence_root,
        repo_root=repo_root,
        forbidden_roots=forbidden_roots,
    )
    prompts = (
        dict(prompt_bundles)
        if prompt_bundles is not None
        else load_all_prompt_bundles(repo_root)
    )
    runtime: list[G10MatchedRuntimeCase] = []
    execution_index = 0

    for pair in PAIR_SPECS:
        prompt = prompts.get((pair.novel_id, pair.chapter))
        if prompt is None:
            raise G10MatchedPlanError(
                f"missing prompt bundle for {(pair.novel_id, pair.chapter)!r}"
            )
        if prompt.novel_id != pair.novel_id or prompt.chapter != pair.chapter:
            raise G10MatchedPlanError(
                "G10A2 prompt bundle source identity changed"
            )

        pair_message_sha = prompt.messages_sha256
        for model_id in _model_order_for_pair(pair):
            execution_index += 1
            journal_dir = (
                evidence
                / "journal"
                / pair.novel_id
                / _journal_model_slug(model_id)
            )
            live = live_provider_capability(model_id)
            effort = MODEL_POLICY[model_id]["primary_translation_effort"]
            capability = verified_reasoning_capability(model_id)
            if capability is None:
                raise G10MatchedPlanError(
                    f"G10A2 adjustable model lacks reasoning capability: {model_id}"
                )
            if effort not in live.verified_reasoning_efforts:
                raise G10MatchedPlanError(
                    f"G10A2 effort is not live-verified for {model_id}"
                )

            request_body = build_provider_request(
                model=live.provider_model_id,
                messages=prompt.message_dicts(),
                max_output_tokens=G10A2_MAX_COMPLETION_TOKENS,
                reasoning_capability=capability,
                reasoning_level=effort,
            )
            provider_identity = build_provider_request_identity(
                request_body=request_body
            )
            journal_identity = request_identity(
                operation_kind=G10A2_OPERATION_KIND,
                novel_id=pair.novel_id,
                chapter=pair.chapter,
                provider=PROVIDER_NAME,
                model=live.provider_model_id,
                profile_id=prompt.profile_id,
                source_sha256=prompt.text_sha256,
                request_payload=request_body,
            )
            if (
                provider_identity.request_sha256
                != journal_identity["request_sha256"]
            ):
                raise G10MatchedPlanError(
                    "provider and journal request SHA identities differ"
                )
            if prompt.messages_sha256 != pair_message_sha:
                raise G10MatchedPlanError(
                    "paired model prompts differ for one source"
                )

            runtime.append(
                G10MatchedRuntimeCase(
                    execution_index=execution_index,
                    pair=pair,
                    benchmark_model_id=model_id,
                    provider_model_id=live.provider_model_id,
                    effort=effort,
                    prompt=prompt,
                    request_body=request_body,
                    provider_request_sha256=provider_identity.request_sha256,
                    journal_identity=journal_identity,
                    journal_path=paid_request_journal_path(
                        journal_dir,
                        G10A2_OPERATION_KIND,
                        pair.chapter,
                    ),
                )
            )

    if len(runtime) != G10A2_REQUEST_COUNT:
        raise G10MatchedPlanError(
            "G10A2 runtime request count drifted"
        )
    if tuple(item.execution_index for item in runtime) != tuple(
        range(1, G10A2_REQUEST_COUNT + 1)
    ):
        raise G10MatchedPlanError(
            "G10A2 execution indexes are not contiguous"
        )

    by_pair: dict[str, list[G10MatchedRuntimeCase]] = {}
    for item in runtime:
        by_pair.setdefault(item.pair.pair_id, []).append(item)
    if set(by_pair) != {pair.pair_id for pair in PAIR_SPECS}:
        raise G10MatchedPlanError("G10A2 pair roster changed")
    for pair_id, cases in by_pair.items():
        if len(cases) != 2:
            raise G10MatchedPlanError(
                f"{pair_id} must contain exactly two model requests"
            )
        if {case.benchmark_model_id for case in cases} != set(MODEL_ORDER):
            raise G10MatchedPlanError(
                f"{pair_id} model roster differs from preregistration"
            )
        if len({case.prompt.messages_sha256 for case in cases}) != 1:
            raise G10MatchedPlanError(
                f"{pair_id} model prompts are not byte-identical"
            )

    return tuple(runtime)


def matched_plan_identity(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[tuple[str, int], G10PromptBundle] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=evidence_root,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    return {
        "schema_version": G10A2_SCHEMA_VERSION,
        "revision": G10A2_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
        "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        "context_mode": G10A2_CONTEXT_MODE,
        "max_completion_tokens": G10A2_MAX_COMPLETION_TOKENS,
        "pair_count": G10A2_PAIR_COUNT,
        "primary_pair_count": G10A2_PRIMARY_PAIR_COUNT,
        "bridge_pair_count": G10A2_BRIDGE_PAIR_COUNT,
        "request_count": G10A2_REQUEST_COUNT,
        "pair_execution_policy": {
            "primary_first_model_balance": {
                QWEN_MODEL_ID: 24,
                GLM_MODEL_ID: 24,
            },
            "rule": "alternate_first_model_by_pair_index",
            "blind_orientation_independent_of_execution_order": True,
        },
        "pairs": [pair.identity() for pair in PAIR_SPECS],
        "cases": [case.plan_identity() for case in cases],
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


def matched_plan_sha256(**kwargs: Any) -> str:
    return _sha256_json(matched_plan_identity(**kwargs))


def g10a2_contract_identity() -> dict[str, Any]:
    primary = [pair for pair in PAIR_SPECS if pair.case_kind == "primary"]
    qwen_first = sum(
        1 for pair in primary if pair.first_model_id == QWEN_MODEL_ID
    )
    glm_first = sum(
        1 for pair in primary if pair.first_model_id == GLM_MODEL_ID
    )
    provider_models = {
        model_id: live_provider_capability(model_id).provider_model_id
        for model_id in MODEL_ORDER
    }
    return {
        "schema_version": G10A2_SCHEMA_VERSION,
        "revision": G10A2_REVISION,
        "benchmark_id": G10_BENCHMARK_ID,
        "predecessors": {
            "g10_preregistration_sha256": G10_PREREGISTRATION_SHA256,
            "g10a1_safety_sha256": G10A1_SAFETY_SHA256,
        },
        "context_mode": G10A2_CONTEXT_MODE,
        "max_completion_tokens": G10A2_MAX_COMPLETION_TOKENS,
        "pair_count": G10A2_PAIR_COUNT,
        "primary_pair_count": G10A2_PRIMARY_PAIR_COUNT,
        "bridge_pair_count": G10A2_BRIDGE_PAIR_COUNT,
        "request_count": G10A2_REQUEST_COUNT,
        "provider_model_ids": provider_models,
        "translation_efforts": {
            model_id: MODEL_POLICY[model_id]["primary_translation_effort"]
            for model_id in MODEL_ORDER
        },
        "execution_order": {
            "rule": "alternate_first_model_by_pair_index",
            "primary_qwen_first": qwen_first,
            "primary_glm_first": glm_first,
        },
        "journal_operation_kind": G10A2_OPERATION_KIND,
        "provider_calls": 0,
        "paid_execution_authorized": False,
        "paid_execution_entrypoint_present": False,
    }


G10A2_CONTRACT_SHA256 = _sha256_json(g10a2_contract_identity())


def assert_paid_execution_blocked() -> None:
    identity = g10a2_contract_identity()
    if identity["provider_calls"] != 0:
        raise G10MatchedPlanError(
            "G10A2 unexpectedly reports provider calls"
        )
    if identity["paid_execution_authorized"] is not False:
        raise G10MatchedPlanError(
            "G10A2 unexpectedly authorizes paid execution"
        )
    if identity["paid_execution_entrypoint_present"] is not False:
        raise G10MatchedPlanError(
            "G10A2 must not expose a paid execution entry point"
        )


__all__ = [
    "G10A2_BRIDGE_PAIR_COUNT",
    "G10A2_CONTEXT_MODE",
    "G10A2_CONTRACT_SHA256",
    "G10A2_MAX_COMPLETION_TOKENS",
    "G10A2_OPERATION_KIND",
    "G10A2_PAIR_COUNT",
    "G10A2_PRIMARY_PAIR_COUNT",
    "G10A2_REQUEST_COUNT",
    "G10A2_REVISION",
    "G10MatchedPlanError",
    "G10MatchedRuntimeCase",
    "G10PairSpec",
    "G10PromptBundle",
    "PAIR_SPECS",
    "assert_matched_evidence_root",
    "assert_paid_execution_blocked",
    "build_runtime_cases",
    "g10a2_contract_identity",
    "load_all_prompt_bundles",
    "load_prompt_bundle",
    "matched_plan_identity",
    "matched_plan_sha256",
    "planned_pair_specs",
]
