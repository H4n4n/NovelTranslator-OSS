"""Deterministic source-only chapter selection for GOAT G6 calibration.

G6 needs two difficult Chinese chapters and two difficult Korean chapters before
any literary provider call is made.  This module selects those cases without
consulting translations, glossary state, continuity, model outputs, scores, or
provider data.

For each novel, Chapters 1-30 are profiled on two independent source-only stress
axes:

* dialogue stress: dialogue-line share, quote-turn density, and short dialogue
  turn share;
* information-density stress: source length, rare lexical-unit density,
  long-sentence pressure, structured-marker density, and mean non-empty line
  length.

The highest dialogue-stress chapter is selected first.  The highest
information-density chapter not already selected is selected second.  All
ranking, tie-breaking, tokenization, and weights are deterministic.

Importing this module performs no I/O, network access, credential access, or
writes.  The CLI reads only canonical local source/chapter-library files and
writes one explicit evidence JSON outside novels/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from chapter_library import chapter_production_index
from novel_project import SOURCE_TYPE_TXT, load_novel_project


SELECTION_SCHEMA_VERSION = 1
SELECTION_REVISION = "goat-g6-source-stress-v1"
CHAPTER_START = 1
CHAPTER_END = 30
TOP_CANDIDATE_COUNT = 5

DIALOGUE_WEIGHTS = {
    "dialogue_line_ratio": 0.45,
    "quote_turns_per_1000": 0.35,
    "short_dialogue_line_ratio": 0.20,
}
INFORMATION_WEIGHTS = {
    "visible_chars": 0.25,
    "rare_token_types_per_1000": 0.25,
    "p95_sentence_visible_chars": 0.20,
    "structured_markers_per_1000": 0.15,
    "mean_nonempty_line_visible_chars": 0.15,
}

if not math.isclose(sum(DIALOGUE_WEIGHTS.values()), 1.0):
    raise RuntimeError("dialogue stress weights must sum to one")
if not math.isclose(sum(INFORMATION_WEIGHTS.values()), 1.0):
    raise RuntimeError("information-density stress weights must sum to one")


class G6SourceSelectionError(RuntimeError):
    """Source selection input, identity, or output boundary is unsafe."""


@dataclass(frozen=True)
class ChapterStressMetrics:
    chapter: int
    text_sha256: str
    visible_chars: int
    nonempty_line_count: int
    sentence_count: int
    p95_sentence_visible_chars: int
    mean_nonempty_line_visible_chars: float
    dialogue_line_ratio: float
    quote_turns_per_1000: float
    short_dialogue_line_ratio: float
    structured_markers_per_1000: float
    rare_token_types_per_1000: float
    dialogue_stress_score: float
    information_density_score: float

    def identity(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "text_sha256": self.text_sha256,
            "visible_chars": self.visible_chars,
            "nonempty_line_count": self.nonempty_line_count,
            "sentence_count": self.sentence_count,
            "p95_sentence_visible_chars": self.p95_sentence_visible_chars,
            "mean_nonempty_line_visible_chars": self.mean_nonempty_line_visible_chars,
            "dialogue_line_ratio": self.dialogue_line_ratio,
            "quote_turns_per_1000": self.quote_turns_per_1000,
            "short_dialogue_line_ratio": self.short_dialogue_line_ratio,
            "structured_markers_per_1000": self.structured_markers_per_1000,
            "rare_token_types_per_1000": self.rare_token_types_per_1000,
            "dialogue_stress_score": self.dialogue_stress_score,
            "information_density_score": self.information_density_score,
        }


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G6SourceSelectionError(
            "G6 source-selection identity is not JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _visible_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def _nonempty_lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?]+|(?<=[.])\s+")
_QUOTE_MARKER_RE = re.compile(r'["“”「」『』]')
_STRUCTURED_MARKER_RE = re.compile(r"[0-9\[\]【】〔〕()（）<>《》]")


def _sentence_visible_lengths(text: str) -> tuple[int, ...]:
    values = tuple(
        _visible_len(part)
        for part in _SENTENCE_SPLIT_RE.split(text)
        if _visible_len(part) > 0
    )
    if values:
        return values
    visible = _visible_len(text)
    return (visible,) if visible else ()


def _p95(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return int(ordered[index])


def _lexical_units(text: str, source_language: str) -> tuple[str, ...]:
    if source_language == "ko":
        return tuple(
            token.casefold()
            for token in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}|[0-9]+", text)
        )

    if source_language == "zh":
        units: list[str] = []
        for run in re.findall(r"[\u3400-\u9fff]+", text):
            if len(run) == 1:
                units.append(run)
            else:
                units.extend(run[index:index + 2] for index in range(len(run) - 1))
        units.extend(
            token.casefold()
            for token in re.findall(r"[A-Za-z]{2,}|[0-9]+", text)
        )
        return tuple(units)

    raise G6SourceSelectionError(
        f"unsupported G6 source language: {source_language!r}"
    )


def _percentile_rank(values: Sequence[float], value: float) -> float:
    if not values:
        raise G6SourceSelectionError("cannot rank an empty metric sequence")
    if len(values) == 1:
        return 0.5
    less = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return (less + 0.5 * (equal - 1)) / (len(values) - 1)


def _round_metric(value: float) -> float:
    return round(float(value), 8)


def _base_metrics(
    chapter: int,
    text: str,
    source_language: str,
    rare_tokens: set[str],
) -> dict[str, Any]:
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
        raise G6SourceSelectionError("chapter number must be a positive integer")
    if not isinstance(text, str) or not text.strip():
        raise G6SourceSelectionError(f"Chapter {chapter} has empty source text")

    visible = _visible_len(text)
    lines = _nonempty_lines(text)
    sentence_lengths = _sentence_visible_lengths(text)

    dialogue_lines = tuple(
        line
        for line in lines
        if len(_QUOTE_MARKER_RE.findall(line)) >= 2
        or bool(_QUOTE_MARKER_RE.match(line))
    )
    short_dialogue_lines = tuple(
        line
        for line in dialogue_lines
        if _visible_len(line) <= 60
    )

    quote_turns = len(dialogue_lines)
    structured_markers = len(_STRUCTURED_MARKER_RE.findall(text))
    chapter_tokens = set(_lexical_units(text, source_language))
    rare_type_count = len(chapter_tokens & rare_tokens)

    mean_line = (
        sum(_visible_len(line) for line in lines) / len(lines)
        if lines
        else 0.0
    )

    return {
        "chapter": chapter,
        "text_sha256": _sha256_text(text),
        "visible_chars": visible,
        "nonempty_line_count": len(lines),
        "sentence_count": len(sentence_lengths),
        "p95_sentence_visible_chars": _p95(sentence_lengths),
        "mean_nonempty_line_visible_chars": mean_line,
        "dialogue_line_ratio": (
            len(dialogue_lines) / len(lines)
            if lines
            else 0.0
        ),
        "quote_turns_per_1000": (
            quote_turns * 1000.0 / visible
            if visible
            else 0.0
        ),
        "short_dialogue_line_ratio": (
            len(short_dialogue_lines) / len(dialogue_lines)
            if dialogue_lines
            else 0.0
        ),
        "structured_markers_per_1000": (
            structured_markers * 1000.0 / visible
            if visible
            else 0.0
        ),
        "rare_token_types_per_1000": (
            rare_type_count * 1000.0 / visible
            if visible
            else 0.0
        ),
    }


def profile_chapters(
    chapters: Mapping[int, str],
    source_language: str,
) -> tuple[ChapterStressMetrics, ...]:
    """Profile one source corpus without consulting any translated/state data."""
    if source_language not in {"zh", "ko"}:
        raise G6SourceSelectionError(
            f"unsupported G6 source language: {source_language!r}"
        )
    if len(chapters) < 2:
        raise G6SourceSelectionError("at least two source chapters are required")

    normalized: dict[int, str] = {}
    document_frequency: dict[str, int] = {}

    for chapter, text in chapters.items():
        if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
            raise G6SourceSelectionError("chapter keys must be positive integers")
        if chapter in normalized:
            raise G6SourceSelectionError(f"duplicate Chapter {chapter}")
        if not isinstance(text, str) or not text.strip():
            raise G6SourceSelectionError(f"Chapter {chapter} has empty source text")
        normalized[chapter] = text

        for token in set(_lexical_units(text, source_language)):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    rare_threshold = max(1, min(2, math.ceil(len(normalized) * 0.08)))
    rare_tokens = {
        token
        for token, frequency in document_frequency.items()
        if frequency <= rare_threshold
    }

    rows = [
        _base_metrics(chapter, normalized[chapter], source_language, rare_tokens)
        for chapter in sorted(normalized)
    ]

    metric_columns = {
        name: [float(row[name]) for row in rows]
        for name in (
            "visible_chars",
            "p95_sentence_visible_chars",
            "mean_nonempty_line_visible_chars",
            "dialogue_line_ratio",
            "quote_turns_per_1000",
            "short_dialogue_line_ratio",
            "structured_markers_per_1000",
            "rare_token_types_per_1000",
        )
    }

    result: list[ChapterStressMetrics] = []

    for row in rows:
        dialogue_score = sum(
            DIALOGUE_WEIGHTS[name]
            * _percentile_rank(metric_columns[name], float(row[name]))
            for name in DIALOGUE_WEIGHTS
        )
        information_score = sum(
            INFORMATION_WEIGHTS[name]
            * _percentile_rank(metric_columns[name], float(row[name]))
            for name in INFORMATION_WEIGHTS
        )

        result.append(
            ChapterStressMetrics(
                chapter=row["chapter"],
                text_sha256=row["text_sha256"],
                visible_chars=row["visible_chars"],
                nonempty_line_count=row["nonempty_line_count"],
                sentence_count=row["sentence_count"],
                p95_sentence_visible_chars=row["p95_sentence_visible_chars"],
                mean_nonempty_line_visible_chars=_round_metric(
                    row["mean_nonempty_line_visible_chars"]
                ),
                dialogue_line_ratio=_round_metric(row["dialogue_line_ratio"]),
                quote_turns_per_1000=_round_metric(row["quote_turns_per_1000"]),
                short_dialogue_line_ratio=_round_metric(
                    row["short_dialogue_line_ratio"]
                ),
                structured_markers_per_1000=_round_metric(
                    row["structured_markers_per_1000"]
                ),
                rare_token_types_per_1000=_round_metric(
                    row["rare_token_types_per_1000"]
                ),
                dialogue_stress_score=_round_metric(dialogue_score),
                information_density_score=_round_metric(information_score),
            )
        )

    return tuple(result)


def select_stress_cases(
    metrics: Sequence[ChapterStressMetrics],
) -> dict[str, ChapterStressMetrics]:
    """Select one dialogue and one distinct information-density stress case."""
    if len(metrics) < 2:
        raise G6SourceSelectionError("at least two chapter metrics are required")

    dialogue = sorted(
        metrics,
        key=lambda item: (
            -item.dialogue_stress_score,
            -item.quote_turns_per_1000,
            -item.dialogue_line_ratio,
            -item.visible_chars,
            item.chapter,
        ),
    )[0]

    remaining = [item for item in metrics if item.chapter != dialogue.chapter]
    information = sorted(
        remaining,
        key=lambda item: (
            -item.information_density_score,
            -item.rare_token_types_per_1000,
            -item.visible_chars,
            -item.p95_sentence_visible_chars,
            item.chapter,
        ),
    )[0]

    return {
        "dialogue_stress": dialogue,
        "information_density_stress": information,
    }


def top_candidates(
    metrics: Sequence[ChapterStressMetrics],
    *,
    stress: str,
    count: int = TOP_CANDIDATE_COUNT,
) -> tuple[ChapterStressMetrics, ...]:
    if stress == "dialogue_stress":
        key = lambda item: (
            -item.dialogue_stress_score,
            -item.quote_turns_per_1000,
            -item.dialogue_line_ratio,
            item.chapter,
        )
    elif stress == "information_density_stress":
        key = lambda item: (
            -item.information_density_score,
            -item.rare_token_types_per_1000,
            -item.visible_chars,
            item.chapter,
        )
    else:
        raise G6SourceSelectionError(f"unknown stress axis: {stress!r}")

    return tuple(sorted(metrics, key=key)[:count])


def _load_json(path: Path, expected_type: type, label: str) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G6SourceSelectionError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, expected_type):
        raise G6SourceSelectionError(f"{label} has an invalid top-level structure")
    return value


def load_source_selection_input(
    novel_id: str,
    repo_root: Path | str,
) -> dict[str, Any]:
    """Load only canonical source/chapter-library data for Chapters 1-30."""
    project = load_novel_project(novel_id, repo_root)

    if project.config.source_language not in {"zh", "ko"}:
        raise G6SourceSelectionError(
            f"novel {novel_id!r} has unsupported source language "
            f"{project.config.source_language!r}"
        )

    if not project.chapters_file.is_file():
        raise G6SourceSelectionError(
            f"novel {novel_id!r} has no installed canonical chapters.json"
        )

    chapter_items = _load_json(
        project.chapters_file,
        list,
        f"{novel_id} chapter library",
    )

    chapter_map: dict[int, str] = {}

    for item in chapter_items:
        if not isinstance(item, dict):
            raise G6SourceSelectionError(
                f"{novel_id} chapter library contains a non-object entry"
            )
        try:
            chapter = chapter_production_index(item)
        except ValueError as exc:
            raise G6SourceSelectionError(
                f"{novel_id} chapter library contains an invalid entry: {exc}"
            ) from exc

        if not CHAPTER_START <= chapter <= CHAPTER_END:
            continue
        if chapter in chapter_map:
            raise G6SourceSelectionError(
                f"{novel_id} chapter library duplicates Chapter {chapter}"
            )
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise G6SourceSelectionError(
                f"{novel_id} Chapter {chapter} has no source text"
            )
        chapter_map[chapter] = text

    expected = set(range(CHAPTER_START, CHAPTER_END + 1))
    missing = sorted(expected - set(chapter_map))
    if missing:
        raise G6SourceSelectionError(
            f"{novel_id} is missing G6 selection chapters: {missing}"
        )

    source_path = (
        project.source_txt
        if project.config.source_type == SOURCE_TYPE_TXT
        else project.source_epub
    )
    if not source_path.is_file():
        raise G6SourceSelectionError(
            f"{novel_id} canonical source file is missing: {source_path}"
        )

    return {
        "novel_id": project.config.id,
        "source_language": project.config.source_language,
        "source_type": project.config.source_type,
        "source_path": source_path,
        "source_sha256": sha256_file(source_path),
        "source_bytes": source_path.stat().st_size,
        "chapter_library_sha256": sha256_file(project.chapters_file),
        "chapter_library_bytes": project.chapters_file.stat().st_size,
        "chapters": chapter_map,
    }


def build_novel_selection(
    novel_id: str,
    repo_root: Path | str,
) -> dict[str, Any]:
    loaded = load_source_selection_input(novel_id, repo_root)
    metrics = profile_chapters(
        loaded["chapters"],
        loaded["source_language"],
    )
    selected = select_stress_cases(metrics)

    def selected_identity(item: ChapterStressMetrics) -> dict[str, Any]:
        return {
            "chapter": item.chapter,
            "text_sha256": item.text_sha256,
            "dialogue_stress_score": item.dialogue_stress_score,
            "information_density_score": item.information_density_score,
        }

    return {
        "novel_id": loaded["novel_id"],
        "source_language": loaded["source_language"],
        "source_type": loaded["source_type"],
        "source_sha256": loaded["source_sha256"],
        "source_bytes": loaded["source_bytes"],
        "chapter_library_sha256": loaded["chapter_library_sha256"],
        "chapter_library_bytes": loaded["chapter_library_bytes"],
        "chapter_range": [CHAPTER_START, CHAPTER_END],
        "selected": {
            name: selected_identity(item)
            for name, item in selected.items()
        },
        "top_candidates": {
            stress: [item.identity() for item in top_candidates(metrics, stress=stress)]
            for stress in (
                "dialogue_stress",
                "information_density_stress",
            )
        },
        "metrics": [item.identity() for item in metrics],
    }


def build_selection_bundle(
    repo_root: Path | str,
    novel_ids: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(novel_ids, Sequence) or isinstance(novel_ids, (str, bytes)):
        raise G6SourceSelectionError("novel_ids must be a sequence")
    ids = tuple(novel_ids)
    if not ids or len(ids) != len(set(ids)):
        raise G6SourceSelectionError("novel_ids must be non-empty and unique")

    novels = [build_novel_selection(novel_id, repo_root) for novel_id in ids]

    result = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "selection_revision": SELECTION_REVISION,
        "chapter_range": [CHAPTER_START, CHAPTER_END],
        "source_only": True,
        "model_outputs_consulted": False,
        "glossary_consulted": False,
        "continuity_consulted": False,
        "provider_calls": 0,
        "dialogue_weights": dict(DIALOGUE_WEIGHTS),
        "information_weights": dict(INFORMATION_WEIGHTS),
        "novels": novels,
    }
    return {
        **result,
        "selection_sha256": _sha256_json(result),
    }


def _output_path_is_safe(output: Path, repo_root: Path) -> bool:
    output = output.resolve()
    novels_root = (repo_root / "novels").resolve()
    return output != novels_root and novels_root not in output.parents


def write_selection_bundle(
    output_path: Path | str,
    bundle: Mapping[str, Any],
    *,
    repo_root: Path | str,
) -> Path:
    output = Path(output_path)
    root = Path(repo_root).resolve()

    if not _output_path_is_safe(output, root):
        raise G6SourceSelectionError(
            "G6 selection evidence must not be written under novels/"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        dict(bundle),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"

    temp = output.with_name(output.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, output)
    return output


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build zero-provider G6 source-only chapter selection evidence."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--novel",
        action="append",
        dest="novels",
        required=True,
        help="Novel id; pass once per benchmark novel.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    bundle = build_selection_bundle(args.repo_root, tuple(args.novels))
    path = write_selection_bundle(
        args.output,
        bundle,
        repo_root=args.repo_root,
    )

    print(f"selection_revision={bundle['selection_revision']}")
    print(f"selection_sha256={bundle['selection_sha256']}")
    print(f"provider_calls={bundle['provider_calls']}")

    for novel in bundle["novels"]:
        dialogue = novel["selected"]["dialogue_stress"]["chapter"]
        information = novel["selected"]["information_density_stress"]["chapter"]
        print(
            f"{novel['novel_id']}: "
            f"dialogue_stress={dialogue}, "
            f"information_density_stress={information}, "
            f"source_sha256={novel['source_sha256']}"
        )

    print(f"output={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
