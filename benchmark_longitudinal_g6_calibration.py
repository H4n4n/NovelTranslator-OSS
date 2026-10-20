"""Frozen G6 literary calibration design for the GOAT longitudinal benchmark.

G6A selected four difficult source chapters without provider inference. G5C/G5D
live-verified the exact Provider model spellings and reasoning controls. This
module binds those two evidence layers into a deterministic 40-call calibration
plan, but it does not authorize execution.

The calibration isolates PRIMARY TRANSLATION reasoning only. Every arm for the
same model/chapter must receive byte-identical messages; the only adjustable
field is the live-verified reasoning_effort. Tencent Hy3 Paid and MiMo V2.5 Pro
have one provider-native/default arm each.

No glossary, continuity, translated text, model output, or lifecycle mutation is
part of G6. Those are reserved for the later chronological benchmark. The G6
source chapters are stress cases, not a chronological state-growth experiment.

Importing this module performs no I/O, credential access, network activity, or
writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from benchmark_longitudinal_capability_results import (
    G5C_LIVE_RESULT_SHA256,
    g6_translation_efforts,
    live_provider_capability,
)
from benchmark_longitudinal_config import BENCHMARK_MODEL_IDS


G6_CALIBRATION_SCHEMA_VERSION = 1
G6_CALIBRATION_REVISION = "goat-g6-literary-calibration-v3"
G6_SOURCE_SELECTION_REVISION = "goat-g6-source-stress-v1"

G6_REPOSITORY_ANCHOR = "8ee99d973e3280dd090ceab765df95835d04f606"

G6A_SELECTION_SEMANTIC_SHA256 = (
    "d5fa68513570c11feae996fcc1e956c8d98823c974b4449c14a1e02f304bc3a5"
)
G6A_SELECTION_FILE_SHA256 = (
    "d40c03014bdbe471c53a078bc63f560a1e74c77d65176f07be29adfd8797ea59"
)
G6A_REVIEW_SHA256 = (
    "c5c6370489d0f92c049b37dab6eecc1a368a86f5cc4bce54943683696cc2a412"
)
G6A_TARGETED_TESTS_SHA256 = (
    "c13c38229a82fa1144d24b407170d3d7df0c56c54da7395ddf8e0e9c5b6d53b3"
)
G6A_MANIFEST_SHA256 = (
    "4c1158e9a80997d9945a6837fd9e432b95df511581930cbc1df2e76ce89b2718"
)

ACE_SOURCE_SHA256 = (
    "b43ab411a4e33a616a0353a8f6c8b4682c75d36dc96090915c91c4fc0d9dbc1f"
)
ACE_CHAPTER_LIBRARY_SHA256 = (
    "5eb324aaedebf81f60bf00facad7afe830d3201d39d837973ccf82caaee8e6a3"
)
ROGUE_SOURCE_SHA256 = (
    "ce939a2167c0eb11700bd1b5a0f463c891da232d1bbe84265e2c5e718714015e"
)
ROGUE_CHAPTER_LIBRARY_SHA256 = (
    "39d2c0e63627d122eb2b3d5a46586dcba87e9ba0f69e9955b1ba735d571ff63d"
)

TRANSLATION_CONTEXT_MODE = "production_profile_empty_glossary_no_continuity"
MAX_COMPLETION_TOKENS = 12_288
AUTOMATIC_RETRIES = 0
ZDR_ENABLED = False
LITERARY_PROVIDER_CALLS_AUTHORIZED = False

STRESS_DIALOGUE = "dialogue_stress"
STRESS_INFORMATION = "information_density_stress"
_ALLOWED_STRESS_AXES = frozenset({STRESS_DIALOGUE, STRESS_INFORMATION})


class G6CalibrationConfigurationError(ValueError):
    """Frozen G6 calibration evidence or plan is malformed."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6CalibrationConfigurationError(
            "G6 calibration identity is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class G6SourceCase:
    novel_id: str
    source_language: str
    stress_axis: str
    chapter: int
    source_sha256: str
    chapter_library_sha256: str
    text_sha256: str

    def __post_init__(self) -> None:
        if self.source_language not in {"zh", "ko"}:
            raise G6CalibrationConfigurationError("unsupported source language")
        if self.stress_axis not in _ALLOWED_STRESS_AXES:
            raise G6CalibrationConfigurationError("unsupported stress axis")
        if not isinstance(self.chapter, int) or isinstance(self.chapter, bool) or self.chapter < 1:
            raise G6CalibrationConfigurationError("chapter must be a positive integer")
        for label, digest in (
            ("source", self.source_sha256),
            ("chapter library", self.chapter_library_sha256),
            ("chapter text", self.text_sha256),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise G6CalibrationConfigurationError(
                    f"{label} SHA256 must be lowercase hexadecimal"
                )

    def identity(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "stress_axis": self.stress_axis,
            "chapter": self.chapter,
            "source_sha256": self.source_sha256,
            "chapter_library_sha256": self.chapter_library_sha256,
            "text_sha256": self.text_sha256,
        }


G6_SOURCE_CASES = (
    G6SourceCase(
        novel_id="ace_evolution",
        source_language="zh",
        stress_axis=STRESS_DIALOGUE,
        chapter=18,
        source_sha256=ACE_SOURCE_SHA256,
        chapter_library_sha256=ACE_CHAPTER_LIBRARY_SHA256,
        text_sha256="e1b141fbd64069d5208d8a1d73076afa0a25bb159efd2d2907638ac9c5e0798a",
    ),
    G6SourceCase(
        novel_id="ace_evolution",
        source_language="zh",
        stress_axis=STRESS_INFORMATION,
        chapter=2,
        source_sha256=ACE_SOURCE_SHA256,
        chapter_library_sha256=ACE_CHAPTER_LIBRARY_SHA256,
        text_sha256="769310fa78921a78c4e148d0282818bc7ffc1f11d0ae6b83b1695e0e7d817596",
    ),
    G6SourceCase(
        novel_id="rogue_first_prince",
        source_language="ko",
        stress_axis=STRESS_DIALOGUE,
        chapter=25,
        source_sha256=ROGUE_SOURCE_SHA256,
        chapter_library_sha256=ROGUE_CHAPTER_LIBRARY_SHA256,
        text_sha256="a42ecf2525be7c2c3bf59adddf9135d89d8e911fbc6fc70cdd8c140398e2d2cb",
    ),
    G6SourceCase(
        novel_id="rogue_first_prince",
        source_language="ko",
        stress_axis=STRESS_INFORMATION,
        chapter=14,
        source_sha256=ROGUE_SOURCE_SHA256,
        chapter_library_sha256=ROGUE_CHAPTER_LIBRARY_SHA256,
        text_sha256="4b07c5c545fcf8683d47eeb7521518c913efaa3751c89e7f698aefc79555d5f3",
    ),
)

if len(G6_SOURCE_CASES) != 4:
    raise G6CalibrationConfigurationError("G6 must freeze exactly four source cases")
if len({(case.novel_id, case.chapter) for case in G6_SOURCE_CASES}) != 4:
    raise G6CalibrationConfigurationError("G6 source cases must be unique")

_SOURCE_CASE_BY_KEY = MappingProxyType(
    {(case.novel_id, case.chapter): case for case in G6_SOURCE_CASES}
)


def source_case(novel_id: str, chapter: int) -> G6SourceCase:
    try:
        return _SOURCE_CASE_BY_KEY[(novel_id, chapter)]
    except KeyError as exc:
        raise G6CalibrationConfigurationError(
            f"unknown frozen G6 source case: {(novel_id, chapter)!r}"
        ) from exc


@dataclass(frozen=True)
class G6CalibrationCase:
    case_id: str
    benchmark_model_id: str
    provider_model_id: str
    novel_id: str
    source_language: str
    stress_axis: str
    chapter: int
    text_sha256: str
    effort: str | None
    max_completion_tokens: int = MAX_COMPLETION_TOKENS
    zdr_enabled: bool = ZDR_ENABLED

    def identity(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "benchmark_model_id": self.benchmark_model_id,
            "provider_model_id": self.provider_model_id,
            "novel_id": self.novel_id,
            "source_language": self.source_language,
            "stress_axis": self.stress_axis,
            "chapter": self.chapter,
            "text_sha256": self.text_sha256,
            "effort": self.effort,
            "max_completion_tokens": self.max_completion_tokens,
            "zdr_enabled": self.zdr_enabled,
        }


def build_g6_calibration_plan() -> tuple[G6CalibrationCase, ...]:
    cases: list[G6CalibrationCase] = []
    index = 0

    for model_id in BENCHMARK_MODEL_IDS:
        live = live_provider_capability(model_id)
        efforts = g6_translation_efforts(model_id)

        for source in G6_SOURCE_CASES:
            for effort in efforts:
                index += 1
                cases.append(
                    G6CalibrationCase(
                        case_id=f"G6C{index:03d}",
                        benchmark_model_id=model_id,
                        provider_model_id=live.provider_model_id,
                        novel_id=source.novel_id,
                        source_language=source.source_language,
                        stress_axis=source.stress_axis,
                        chapter=source.chapter,
                        text_sha256=source.text_sha256,
                        effort=effort,
                    )
                )

    return tuple(cases)


G6_CALIBRATION_CASES = build_g6_calibration_plan()
G6_PRIMARY_CALL_COUNT = len(G6_CALIBRATION_CASES)

if G6_PRIMARY_CALL_COUNT != 40:
    raise G6CalibrationConfigurationError(
        f"G6 calibration must contain exactly 40 primary calls, got {G6_PRIMARY_CALL_COUNT}"
    )

if len({case.case_id for case in G6_CALIBRATION_CASES}) != G6_PRIMARY_CALL_COUNT:
    raise G6CalibrationConfigurationError("duplicate G6 calibration case id")


def g6_calibration_identity() -> dict[str, Any]:
    return {
        "schema_version": G6_CALIBRATION_SCHEMA_VERSION,
        "revision": G6_CALIBRATION_REVISION,
        "repository_anchor": G6_REPOSITORY_ANCHOR,
        "g5c_live_result_sha256": G5C_LIVE_RESULT_SHA256,
        "g6a": {
            "selection_revision": G6_SOURCE_SELECTION_REVISION,
            "selection_semantic_sha256": G6A_SELECTION_SEMANTIC_SHA256,
            "selection_file_sha256": G6A_SELECTION_FILE_SHA256,
            "review_sha256": G6A_REVIEW_SHA256,
            "targeted_tests_sha256": G6A_TARGETED_TESTS_SHA256,
            "manifest_sha256": G6A_MANIFEST_SHA256,
        },
        "translation_context_mode": TRANSLATION_CONTEXT_MODE,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "automatic_retries": AUTOMATIC_RETRIES,
        "zdr_enabled": ZDR_ENABLED,
        "literary_provider_calls_authorized": LITERARY_PROVIDER_CALLS_AUTHORIZED,
        "source_cases": [case.identity() for case in G6_SOURCE_CASES],
        "calibration_cases": [case.identity() for case in G6_CALIBRATION_CASES],
        "primary_call_count": G6_PRIMARY_CALL_COUNT,
    }


G6_CALIBRATION_FREEZE_SHA256 = _sha256_json(g6_calibration_identity())
