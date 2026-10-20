"""Blind A/B review tooling for the mature-glossary benchmark.

The workflow is deliberately three-stage:

1. build
   Validate the frozen RAW/finalization corpus, create a balanced anonymous A/B
   review bundle, and commit the private identity key by SHA256.
2. freeze-scores
   Validate a complete anonymous score sheet and freeze it without consulting
   the identity key.
3. reveal
   Only after scores are frozen, verify the identity-key commitment, reveal
   model identities, and compute provider-level quality results.

Importing this module performs no credential loading, network activity, writes,
or production-state mutation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys

from benchmark_novel import (
    BenchmarkSafetyError,
    load_benchmark_inputs,
    relevant_glossary_for_chapter,
    resolve_output_dir,
)


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_BENCHMARK = "mature-glossary-blind-review-v1"
RAW_BENCHMARK = "mature-glossary-raw-matched-corpus-v1"
FINALIZATION_BENCHMARK = "mature-glossary-deterministic-finalization-v1"
ENGINE_FINALIZATION_BENCHMARK = "mature-glossary-matched-engine-finalization-v2"

RUBRIC = {
    "scale": {"minimum": 0.0, "maximum": 10.0},
    "weights": {
        "fidelity": 0.30,
        "completeness": 0.15,
        "terminology": 0.15,
        "naturalness": 0.15,
        "dialogue_tone": 0.10,
        "relationships_pronouns": 0.075,
        "cultural_technical": 0.075,
    },
    "definitions": {
        "fidelity": "Meaning accuracy, logic, nuance, and source-faithful interpretation.",
        "completeness": "No meaningful omissions, additions, hallucinations, or duplicated content.",
        "terminology": "Consistency with the supplied chapter-relevant established glossary.",
        "naturalness": "Readable literary English without unnecessary stiffness or machine-like phrasing.",
        "dialogue_tone": "Voice, dialogue flow, register, emotional tone, and character differentiation.",
        "relationships_pronouns": "Correct pronouns, kinship/status relations, referents, and speaker relationships.",
        "cultural_technical": "Appropriate handling of culture-specific, martial, magical, institutional, and technical concepts.",
        "blocker": "Mark 1 only for a publication-blocking semantic/terminology/readability defect; otherwise 0.",
        "preference": "Choose A, B, or TIE after scoring both candidates independently.",
    },
}

DIMENSIONS = tuple(RUBRIC["weights"])
SCORE_COLUMNS = [
    "case_id",
    *[f"{side}_{dimension}" for dimension in DIMENSIONS for side in ("a", "b")],
    "a_blocker",
    "b_blocker",
    "preference",
    "notes",
]

_BLIND_HEADING_PLACEHOLDER = "[CHAPTER HEADING WITHHELD FOR BLINDING]"

_HEADING_PATTERNS = (
    re.compile(
        r"^\s*(?:#{1,6}[ \t]*)?(?:\*\*[ \t]*)?"
        r"(?:chapter|ch\.?)[ \t]*(?:\d+|[ivxlcdm]+)\b.*$",
        re.I,
    ),
    re.compile(r"^\s*(?:#{1,6}[ \t]*)?第.{1,24}[章节回卷].*$"),
    re.compile(
        r"^\s*(?:#{1,6}[ \t]*)?(?:제\s*)?\d+\s*(?:화|장|회)\b.*$"
    ),
    re.compile(
        r"^\s*(?:#{1,6}[ \t]*)?\d{1,6}\s*(?:[.：:\-–—])\s*\S.+$"
    ),
)


class BlindReviewError(RuntimeError):
    pass


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _load_json(path: Path, label: str):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BlindReviewError(f"{label} could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise BlindReviewError(f"{label} must be a JSON object")
    return value


def _load_bound_json(path: Path, expected_sha256: str, label: str):
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise BlindReviewError(f"{label} could not be read: {exc}") from exc
    actual = _sha_bytes(payload)
    if actual != expected_sha256:
        raise BenchmarkSafetyError(
            f"{label} SHA256 mismatch: {actual} != {expected_sha256}"
        )
    return _load_json(path, label)


def _write_new_bytes(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
    if path.read_bytes() != payload:
        raise BlindReviewError(f"write verification failed for {path}")


def _write_new_text(path: Path, text: str):
    _write_new_bytes(path, text.encode("utf-8"))


def _write_new_json(path: Path, value):
    _write_new_bytes(
        path,
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8"),
    )


def _git_head(repo_root: Path) -> str:
    try:
        value = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BlindReviewError(f"could not resolve repository HEAD: {exc}") from exc
    return value


_ALLOWED_POST_FINALIZATION_PATHS = frozenset(
    {
        "benchmark_blind_review.py",
        "tests/test_benchmark_blind_review.py",
    }
)


def _verify_repository_compatibility(
    repo_root: Path,
    frozen_finalization_main: str,
) -> str:
    current = _git_head(repo_root)
    if current == frozen_finalization_main:
        return current

    try:
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "merge-base",
                "--is-ancestor",
                frozen_finalization_main,
                current,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise BlindReviewError(
            f"could not verify repository ancestry: {exc}"
        ) from exc

    if ancestry.returncode != 0:
        raise BenchmarkSafetyError(
            "repository HEAD is not descended from frozen finalization main"
        )

    try:
        changed = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--name-only",
                f"{frozen_finalization_main}..{current}",
            ],
            text=True,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BlindReviewError(
            f"could not inspect post-finalization repository drift: {exc}"
        ) from exc

    unexpected = sorted(
        {
            path.strip().replace("\\", "/")
            for path in changed
            if path.strip()
        }
        - _ALLOWED_POST_FINALIZATION_PATHS
    )

    if unexpected:
        raise BenchmarkSafetyError(
            "repository changed outside blind-review tooling after frozen "
            f"finalization main: {unexpected}"
        )

    return current


def _heading_like(
    line: str,
    known_heading: str | None = None,
    known_chapter: int | None = None,
) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped == _BLIND_HEADING_PLACEHOLDER:
        return True
    if known_heading and stripped == known_heading.strip():
        return True

    markdown = re.match(r"^\s*#{1,6}[ \t]*(.*)$", stripped)
    normalized = markdown.group(1).strip() if markdown else stripped
    normalized = normalized.strip("*_ ")

    if known_chapter is not None:
        chapter = re.escape(str(int(known_chapter)))
        known_patterns = (
            rf"^(?:chapter|ch\.?)\s*0*{chapter}\b",
            rf"^第\s*0*{chapter}\s*[章节回卷]",
            rf"^(?:제\s*)?0*{chapter}\s*(?:화|장|회)\b",
        )
        if markdown:
            known_patterns += (
                rf"^0*{chapter}\s*(?:[.：:\-]|$)",
            )
        if any(re.match(pattern, normalized, re.I) for pattern in known_patterns):
            return True

    return any(pattern.match(stripped) for pattern in _HEADING_PATTERNS)


def _blind_display_text(
    text: str,
    known_heading: str | None = None,
    known_chapter: int | None = None,
) -> tuple[str, int]:
    """Neutralize consecutive leading chapter headings in the review copy only."""
    lines = text.splitlines()
    if not lines:
        return text, 0

    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1

    cursor = first
    removed = 0
    while (
        cursor < len(lines)
        and removed < 4
        and _heading_like(lines[cursor], known_heading, known_chapter)
    ):
        cursor += 1
        removed += 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1

    if not removed:
        return text, 0

    new_lines = lines[:first]
    new_lines.append(_BLIND_HEADING_PLACEHOLDER)
    if cursor < len(lines):
        new_lines.append("")
        new_lines.extend(lines[cursor:])
    return "\n".join(new_lines).rstrip() + "\n", removed


def _mapping_seed(raw_sha: str, final_sha: str, recovery_id: str) -> str:
    return _sha_bytes(
        f"{BUNDLE_BENCHMARK}|mapping|{raw_sha}|{final_sha}|{recovery_id}".encode(
            "utf-8"
        )
    )


def _balanced_assignment(seed: str, novel_id: str, chapters: list[int]):
    if len(chapters) % 2:
        raise BlindReviewError(
            f"balanced A/B assignment requires an even chapter count for {novel_id}"
        )
    ranked = sorted(
        chapters,
        key=lambda chapter: _sha_bytes(
            f"{seed}|assign|{novel_id}|{chapter}".encode("utf-8")
        ),
    )
    return set(ranked[: len(ranked) // 2])


def _case_order(seed: str, identities):
    return sorted(
        identities,
        key=lambda item: _sha_bytes(
            f"{seed}|order|{item[0]}|{item[1]}".encode("utf-8")
        ),
    )


def _arm_index(finalization):
    rows = finalization.get("arms")
    if not isinstance(rows, list) or len(rows) != 4:
        raise BlindReviewError("finalization manifest must contain four arms")
    result = {}
    for row in rows:
        key = (row.get("novel_id"), row.get("provider"))
        if key in result:
            raise BlindReviewError("duplicate finalization arm identity")
        result[key] = row
    return result


def _load_engine_arm(repo_root: Path, arm, expected_recovery_sha, expected_raw_sha):
    output_dir = arm.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise BlindReviewError("finalization arm lacks output_dir")
    novel_id = arm.get("novel_id")
    inputs = load_benchmark_inputs(novel_id, repo_root)
    target = resolve_output_dir(inputs.project, output_dir)
    document_path = target / "engine_finalization.json"
    if not document_path.is_file():
        raise BlindReviewError(f"missing engine finalization: {document_path}")
    actual_sha = _sha_bytes(document_path.read_bytes())
    if actual_sha != arm.get("engine_finalization_sha256"):
        raise BenchmarkSafetyError(
            f"engine finalization SHA mismatch for {novel_id}/{arm.get('provider')}"
        )
    document = _load_json(document_path, "engine finalization")
    if document.get("benchmark") != ENGINE_FINALIZATION_BENCHMARK:
        raise BlindReviewError("unexpected engine finalization benchmark")
    if document.get("novel_id") != novel_id:
        raise BlindReviewError("engine finalization novel mismatch")
    if document.get("provider") != arm.get("provider"):
        raise BlindReviewError("engine finalization provider mismatch")
    if document.get("model") != arm.get("model"):
        raise BlindReviewError("engine finalization model mismatch")
    if document.get("recovery_manifest_sha256") != expected_recovery_sha:
        raise BenchmarkSafetyError("engine finalization recovery SHA mismatch")
    if document.get("raw_corpus_manifest_sha256") != expected_raw_sha:
        raise BenchmarkSafetyError("engine finalization RAW corpus SHA mismatch")
    if not document.get("safety", {}).get("protected_state_verified_unchanged"):
        raise BenchmarkSafetyError("engine finalization lacks protected-state proof")
    rows = document.get("chapters")
    if not isinstance(rows, list) or len(rows) != 30:
        raise BlindReviewError("engine finalization chapter metadata must contain 30 rows")
    by_chapter = {}
    for row in rows:
        chapter = int(row["chapter_number"])
        if chapter in by_chapter:
            raise BlindReviewError("duplicate engine-finalization chapter")
        if row.get("text_changed") is not False:
            raise BenchmarkSafetyError(
                f"blind-review v1 requires RAW/final text equivalence; Chapter {chapter} changed"
            )
        if row.get("raw_sha256") != row.get("deterministic_final_sha256"):
            raise BenchmarkSafetyError(
                f"blind-review v1 requires equal RAW/final text SHA; Chapter {chapter}"
            )
        by_chapter[chapter] = row
    return inputs, target, document, by_chapter


def build_bundle(
    *,
    repo_root,
    recovery_path,
    recovery_sha256,
    raw_path,
    raw_sha256,
    finalization_path,
    finalization_sha256,
    output_dir,
):
    repo_root = Path(repo_root)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise BenchmarkSafetyError(f"blind-review target already exists: {output_dir}")

    recovery = _load_bound_json(
        Path(recovery_path), recovery_sha256, "recovery manifest"
    )
    corpus = _load_bound_json(Path(raw_path), raw_sha256, "RAW corpus manifest")
    finalization = _load_bound_json(
        Path(finalization_path),
        finalization_sha256,
        "deterministic finalization manifest",
    )

    if corpus.get("benchmark") != RAW_BENCHMARK or corpus.get("status") != "frozen_raw_complete":
        raise BlindReviewError("RAW corpus is not the frozen mature-glossary corpus")
    if finalization.get("benchmark") != FINALIZATION_BENCHMARK or finalization.get("status") != "frozen_complete":
        raise BlindReviewError("deterministic finalization manifest is not frozen complete")
    if corpus.get("recovery_id") != recovery.get("recovery_id"):
        raise BenchmarkSafetyError("RAW corpus recovery identity mismatch")
    if finalization.get("recovery_id") != recovery.get("recovery_id"):
        raise BenchmarkSafetyError("finalization recovery identity mismatch")
    if finalization.get("recovery_manifest_sha256") != recovery_sha256:
        raise BenchmarkSafetyError("finalization recovery SHA mismatch")
    if finalization.get("raw_corpus_manifest_sha256") != raw_sha256:
        raise BenchmarkSafetyError("finalization RAW SHA mismatch")
    if finalization.get("matched_source_count") != 60:
        raise BlindReviewError("finalization matched-source count is not 60")
    if finalization.get("deterministic_finalized_translation_count") != 120:
        raise BlindReviewError("finalization translation count is not 120")
    frozen_finalization_main = finalization.get("repository_main_sha")
    if not isinstance(frozen_finalization_main, str) or not frozen_finalization_main:
        raise BlindReviewError("finalization manifest lacks repository_main_sha")
    _verify_repository_compatibility(
        repo_root,
        frozen_finalization_main,
    )

    pairs = corpus.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != 60:
        raise BlindReviewError("RAW corpus must contain 60 pairs")
    pair_map = {}
    chapters_by_novel = {}
    for pair in pairs:
        identity = (pair.get("novel_id"), int(pair.get("chapter_number")))
        if identity in pair_map:
            raise BlindReviewError("duplicate RAW corpus source identity")
        pair_map[identity] = pair
        chapters_by_novel.setdefault(identity[0], []).append(identity[1])
    if sorted(len(value) for value in chapters_by_novel.values()) != [30, 30]:
        raise BlindReviewError("blind-review v1 expects two 30-chapter novels")

    arms = _arm_index(finalization)
    engine = {}
    for (novel_id, provider), arm in arms.items():
        engine[(novel_id, provider)] = _load_engine_arm(
            repo_root,
            arm,
            recovery_sha256,
            raw_sha256,
        )

    seed = _mapping_seed(
        raw_sha256,
        finalization_sha256,
        recovery["recovery_id"],
    )
    deepseek_a = {
        novel_id: _balanced_assignment(seed, novel_id, sorted(chapters))
        for novel_id, chapters in chapters_by_novel.items()
    }
    ordered = _case_order(seed, list(pair_map))

    private_cases = []
    visible_cases = []
    score_rows = []

    output_dir.mkdir(parents=True, exist_ok=False)

    for index, (novel_id, chapter) in enumerate(ordered, start=1):
        case_id = f"BR{index:03d}"
        pair = pair_map[(novel_id, chapter)]
        ds_inputs, ds_target, _, ds_rows = engine[(novel_id, "deepseek")]
        luna_inputs, luna_target, _, luna_rows = engine[(novel_id, "openai")]
        if ds_inputs.project.config.id != luna_inputs.project.config.id:
            raise BlindReviewError("provider arms disagree on novel identity")
        inputs = ds_inputs

        source = inputs.chapters[chapter]["text"]
        if _sha_bytes(source.encode("utf-8")) != pair.get("source_sha256"):
            raise BenchmarkSafetyError(
                f"source SHA drift for {novel_id} Chapter {chapter}"
            )

        ds_path = ds_target / f"chapter_{chapter:04d}.txt"
        luna_path = luna_target / f"chapter_{chapter:04d}.txt"
        if not ds_path.is_file() or not luna_path.is_file():
            raise BlindReviewError(f"missing finalized text for {novel_id} Chapter {chapter}")

        ds_text = ds_path.read_text(encoding="utf-8")
        luna_text = luna_path.read_text(encoding="utf-8")
        if _sha_bytes(ds_text.encode("utf-8")) != ds_rows[chapter]["deterministic_final_sha256"]:
            raise BenchmarkSafetyError("DeepSeek finalized text SHA mismatch")
        if _sha_bytes(luna_text.encode("utf-8")) != luna_rows[chapter]["deterministic_final_sha256"]:
            raise BenchmarkSafetyError("Luna finalized text SHA mismatch")

        heading = inputs.chapters[chapter].get("heading")
        source_visible, source_headings = _blind_display_text(
            source,
            heading,
            chapter,
        )
        ds_visible, ds_headings = _blind_display_text(
            ds_text,
            known_chapter=chapter,
        )
        luna_visible, luna_headings = _blind_display_text(
            luna_text,
            known_chapter=chapter,
        )

        for label, visible, known in (
            ("source", source_visible, heading),
            ("DeepSeek candidate", ds_visible, None),
            ("Luna candidate", luna_visible, None),
        ):
            reblinded, _ = _blind_display_text(
                visible,
                known,
                chapter,
            )
            if reblinded != visible:
                raise BenchmarkSafetyError(
                    f"{label} retains a leading chapter heading after blinding "
                    f"for {novel_id} Chapter {chapter}"
                )

        ds_is_a = chapter in deepseek_a[novel_id]
        a_provider = "deepseek" if ds_is_a else "openai"
        b_provider = "openai" if ds_is_a else "deepseek"
        a_model = "deepseek-v4-pro" if ds_is_a else "gpt-5.6-luna"
        b_model = "gpt-5.6-luna" if ds_is_a else "deepseek-v4-pro"
        a_text = ds_visible if ds_is_a else luna_visible
        b_text = luna_visible if ds_is_a else ds_visible

        case_dir = output_dir / "cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=False)
        _write_new_text(case_dir / "source.txt", source_visible)
        _write_new_text(case_dir / "A.txt", a_text)
        _write_new_text(case_dir / "B.txt", b_text)

        relevant = relevant_glossary_for_chapter(inputs, chapter)
        _write_new_json(case_dir / "glossary.json", {"entries": relevant})

        visible_case = {
            "case_id": case_id,
            "source_language": inputs.project.config.source_language,
            "source_display_sha256": _sha_bytes(source_visible.encode("utf-8")),
            "candidate_a_display_sha256": _sha_bytes(a_text.encode("utf-8")),
            "candidate_b_display_sha256": _sha_bytes(b_text.encode("utf-8")),
            "relevant_glossary_entry_count": len(relevant),
            "display_heading_policy": {
                "source_leading_headings_withheld": source_headings,
                "candidate_a_leading_headings_withheld": (
                    ds_headings if ds_is_a else luna_headings
                ),
                "candidate_b_leading_headings_withheld": (
                    luna_headings if ds_is_a else ds_headings
                ),
            },
        }
        _write_new_json(case_dir / "case.json", visible_case)
        visible_cases.append(visible_case)

        private_cases.append(
            {
                "case_id": case_id,
                "novel_id": novel_id,
                "chapter_number": chapter,
                "source_sha256": pair["source_sha256"],
                "candidate_a": {
                    "provider": a_provider,
                    "model": a_model,
                    "engine_finalization_output_dir": arms[(novel_id, a_provider)][
                        "output_dir"
                    ],
                },
                "candidate_b": {
                    "provider": b_provider,
                    "model": b_model,
                    "engine_finalization_output_dir": arms[(novel_id, b_provider)][
                        "output_dir"
                    ],
                },
            }
        )
        score_rows.append({column: "" for column in SCORE_COLUMNS} | {"case_id": case_id})

    key_document = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "benchmark": BUNDLE_BENCHMARK,
        "mapping_seed_sha256": seed,
        "recovery_id": recovery["recovery_id"],
        "raw_corpus_manifest_path": str(Path(raw_path)),
        "raw_corpus_manifest_sha256": raw_sha256,
        "finalization_manifest_path": str(Path(finalization_path)),
        "finalization_manifest_sha256": finalization_sha256,
        "cases": private_cases,
    }
    key_payload = (
        json.dumps(key_document, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    key_commitment = _sha_bytes(key_payload)
    _write_new_bytes(output_dir / "private" / "identity_key.json", key_payload)

    rubric_document = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "benchmark": BUNDLE_BENCHMARK,
        "rubric": RUBRIC,
        "review_policy": {
            "score_candidates_independently_before_preference": True,
            "do_not_open_private_identity_key_before_score_freeze": True,
            "leading_chapter_headings_may_be_withheld_in_display_copies": True,
            "heading_format_and_engineering_QA_are_scored_separately_from_literary_review": True,
            "raw_and_finalized_prose_are_identical_for_all_candidates_in_this_bundle": True,
        },
    }
    _write_new_json(output_dir / "rubric.json", rubric_document)

    with (output_dir / "score_sheet.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=SCORE_COLUMNS)
        writer.writeheader()
        writer.writerows(score_rows)

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "benchmark": BUNDLE_BENCHMARK,
        "status": "blind_bundle_frozen",
        "case_count": 60,
        "candidate_count": 120,
        "source_languages": sorted(
            {case["source_language"] for case in visible_cases}
        ),
        "balanced_a_b_assignment_per_novel": True,
        "raw_final_text_equivalent_for_all_candidates": True,
        "raw_corpus_manifest_sha256": raw_sha256,
        "finalization_manifest_sha256": finalization_sha256,
        "identity_key_commitment_sha256": key_commitment,
        "cases": visible_cases,
    }
    _write_new_json(output_dir / "bundle_manifest.json", manifest)

    readme = """MATURE-GLOSSARY BLIND A/B REVIEW

1. Review only bundle_manifest.json, rubric.json, score_sheet.csv, and cases/.
2. Do NOT open private/identity_key.json before scores have been frozen.
3. Each case contains source.txt, glossary.json, A.txt, B.txt, and case.json.
4. Leading chapter headings may be replaced by a neutral placeholder only in the
   review copies to avoid leaking known chapter identities. Prose is otherwise
   unchanged.
5. RAW and deterministic-finalized prose are identical in this benchmark, so
   each candidate is scored once for translation quality. Engineering repair
   burden remains a separate frozen metric.
6. Fill every required score in score_sheet.csv, then run freeze-scores.
7. Only after freeze-scores succeeds should reveal be run.
"""
    _write_new_text(output_dir / "README.txt", readme)

    return manifest, key_commitment


def _parse_score(value: str, label: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise BlindReviewError(f"{label} must be numeric") from exc
    if not math.isfinite(score) or not 0.0 <= score <= 10.0:
        raise BlindReviewError(f"{label} must be between 0 and 10")
    return score


def _weighted_score(row, side: str) -> float:
    return 10.0 * sum(
        _parse_score(row[f"{side}_{dimension}"], f"{side}_{dimension}")
        * RUBRIC["weights"][dimension]
        for dimension in DIMENSIONS
    )


def freeze_scores(*, bundle_dir, scores_csv=None, output_path=None):
    bundle_dir = Path(bundle_dir)
    manifest = _load_json(bundle_dir / "bundle_manifest.json", "bundle manifest")
    if manifest.get("benchmark") != BUNDLE_BENCHMARK:
        raise BlindReviewError("unexpected blind bundle benchmark")
    expected_ids = [case["case_id"] for case in manifest.get("cases", [])]
    if len(expected_ids) != 60 or len(set(expected_ids)) != 60:
        raise BlindReviewError("blind bundle must contain 60 unique cases")

    scores_csv = Path(scores_csv) if scores_csv else bundle_dir / "score_sheet.csv"
    output_path = (
        Path(output_path)
        if output_path
        else bundle_dir / "scores_frozen.json"
    )
    try:
        csv_payload = scores_csv.read_bytes()
    except OSError as exc:
        raise BlindReviewError(f"score sheet could not be read: {exc}") from exc

    with scores_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != SCORE_COLUMNS:
            raise BlindReviewError("score-sheet columns do not match frozen template")
        rows = list(reader)

    if len(rows) != 60:
        raise BlindReviewError("score sheet must contain exactly 60 rows")
    by_case = {}
    frozen_rows = []
    for row in rows:
        case_id = row.get("case_id", "")
        if case_id in by_case:
            raise BlindReviewError(f"duplicate score row for {case_id}")
        if case_id not in expected_ids:
            raise BlindReviewError(f"unknown score-sheet case ID: {case_id}")
        for side in ("a", "b"):
            for dimension in DIMENSIONS:
                _parse_score(row[f"{side}_{dimension}"], f"{case_id} {side}_{dimension}")
            if row[f"{side}_blocker"] not in {"0", "1"}:
                raise BlindReviewError(
                    f"{case_id} {side}_blocker must be 0 or 1"
                )
        preference = row["preference"].strip().upper()
        if preference not in {"A", "B", "TIE"}:
            raise BlindReviewError(
                f"{case_id} preference must be A, B, or TIE"
            )
        frozen = {
            "case_id": case_id,
            "a_scores": {
                dimension: _parse_score(
                    row[f"a_{dimension}"], f"{case_id} a_{dimension}"
                )
                for dimension in DIMENSIONS
            },
            "b_scores": {
                dimension: _parse_score(
                    row[f"b_{dimension}"], f"{case_id} b_{dimension}"
                )
                for dimension in DIMENSIONS
            },
            "a_weighted_score": _weighted_score(row, "a"),
            "b_weighted_score": _weighted_score(row, "b"),
            "a_blocker": row["a_blocker"] == "1",
            "b_blocker": row["b_blocker"] == "1",
            "preference": preference,
            "notes": row.get("notes", ""),
        }
        by_case[case_id] = frozen
        frozen_rows.append(frozen)

    if set(by_case) != set(expected_ids):
        raise BlindReviewError("score sheet does not exactly cover the blind bundle")

    document = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "benchmark": BUNDLE_BENCHMARK,
        "status": "anonymous_scores_frozen",
        "bundle_manifest_sha256": _sha_bytes(
            (bundle_dir / "bundle_manifest.json").read_bytes()
        ),
        "identity_key_commitment_sha256": manifest[
            "identity_key_commitment_sha256"
        ],
        "score_sheet_sha256": _sha_bytes(csv_payload),
        "rubric": RUBRIC,
        "rows": frozen_rows,
    }
    document["score_freeze_id"] = _sha_bytes(_canonical_bytes(document))

    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    if output_path.exists():
        if output_path.read_bytes() != payload:
            raise BenchmarkSafetyError("existing frozen scores differ from recomputation")
        action = "VERIFIED EXISTING"
    else:
        _write_new_bytes(output_path, payload)
        action = "CREATED"
    return document, output_path, action


def _percentile_nearest(values, percentile: float):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _aggregate_candidates(rows):
    weighted = [row["weighted_score"] for row in rows]
    result = {
        "candidate_count": len(rows),
        "mean_weighted_score": statistics.fmean(weighted),
        "median_weighted_score": statistics.median(weighted),
        "p10_weighted_score": _percentile_nearest(weighted, 0.10),
        "score_below_90_count": sum(score < 90.0 for score in weighted),
        "score_below_85_count": sum(score < 85.0 for score in weighted),
        "publication_blocker_count": sum(row["blocker"] for row in rows),
        "dimension_means": {
            dimension: statistics.fmean(
                row["scores"][dimension] for row in rows
            )
            for dimension in DIMENSIONS
        },
    }
    return result


def reveal_scores(*, bundle_dir, scores_path=None, output_path=None):
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest = _load_json(manifest_path, "bundle manifest")
    key_path = bundle_dir / "private" / "identity_key.json"
    try:
        key_payload = key_path.read_bytes()
    except OSError as exc:
        raise BlindReviewError(f"identity key could not be read: {exc}") from exc
    if _sha_bytes(key_payload) != manifest.get("identity_key_commitment_sha256"):
        raise BenchmarkSafetyError("identity-key commitment mismatch")
    key = json.loads(key_payload.decode("utf-8"))

    scores_path = (
        Path(scores_path)
        if scores_path
        else bundle_dir / "scores_frozen.json"
    )
    scores = _load_json(scores_path, "frozen anonymous scores")
    if scores.get("status") != "anonymous_scores_frozen":
        raise BlindReviewError("scores have not been frozen")
    if scores.get("bundle_manifest_sha256") != _sha_bytes(manifest_path.read_bytes()):
        raise BenchmarkSafetyError("frozen scores belong to another blind bundle")
    if scores.get("identity_key_commitment_sha256") != _sha_bytes(key_payload):
        raise BenchmarkSafetyError("frozen scores identity commitment mismatch")

    score_by_case = {row["case_id"]: row for row in scores["rows"]}
    key_by_case = {row["case_id"]: row for row in key["cases"]}
    if set(score_by_case) != set(key_by_case):
        raise BlindReviewError("frozen scores and identity key cover different cases")

    candidates = {"deepseek-v4-pro": [], "gpt-5.6-luna": []}
    preferences = {
        "deepseek-v4-pro": {"wins": 0, "losses": 0, "ties": 0},
        "gpt-5.6-luna": {"wins": 0, "losses": 0, "ties": 0},
    }
    by_novel = {}

    revealed_rows = []
    for case_id in sorted(score_by_case):
        score = score_by_case[case_id]
        identity = key_by_case[case_id]
        a_model = identity["candidate_a"]["model"]
        b_model = identity["candidate_b"]["model"]
        for side, model in (("a", a_model), ("b", b_model)):
            candidate = {
                "case_id": case_id,
                "novel_id": identity["novel_id"],
                "chapter_number": identity["chapter_number"],
                "model": model,
                "scores": score[f"{side}_scores"],
                "weighted_score": score[f"{side}_weighted_score"],
                "blocker": score[f"{side}_blocker"],
            }
            candidates[model].append(candidate)
            by_novel.setdefault(identity["novel_id"], {}).setdefault(model, []).append(
                candidate
            )

        preference = score["preference"]
        if preference == "TIE":
            preferences[a_model]["ties"] += 1
            preferences[b_model]["ties"] += 1
            winner = "TIE"
        else:
            winner_model = a_model if preference == "A" else b_model
            loser_model = b_model if preference == "A" else a_model
            preferences[winner_model]["wins"] += 1
            preferences[loser_model]["losses"] += 1
            winner = winner_model

        revealed_rows.append(
            {
                "case_id": case_id,
                "novel_id": identity["novel_id"],
                "chapter_number": identity["chapter_number"],
                "candidate_a_model": a_model,
                "candidate_b_model": b_model,
                "preference": preference,
                "winner_model": winner,
                "a_weighted_score": score["a_weighted_score"],
                "b_weighted_score": score["b_weighted_score"],
            }
        )

    model_results = {
        model: {
            **_aggregate_candidates(rows),
            "pairwise_preferences": preferences[model],
        }
        for model, rows in candidates.items()
    }

    novel_results = {
        novel_id: {
            model: _aggregate_candidates(rows)
            for model, rows in models.items()
        }
        for novel_id, models in by_novel.items()
    }

    finalization = _load_bound_json(
        Path(key["finalization_manifest_path"]),
        key["finalization_manifest_sha256"],
        "deterministic finalization manifest",
    )
    raw = _load_bound_json(
        Path(key["raw_corpus_manifest_path"]),
        key["raw_corpus_manifest_sha256"],
        "RAW corpus manifest",
    )

    document = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "benchmark": BUNDLE_BENCHMARK,
        "status": "identity_revealed_after_score_freeze",
        "score_freeze_id": scores["score_freeze_id"],
        "identity_key_commitment_sha256": _sha_bytes(key_payload),
        "model_results": model_results,
        "novel_results": novel_results,
        "engineering_repair_burden": finalization.get("aggregate"),
        "raw_costs_usd": raw.get("costs_usd"),
        "cases": revealed_rows,
    }

    output_path = (
        Path(output_path)
        if output_path
        else bundle_dir / "revealed_results.json"
    )
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    if output_path.exists():
        if output_path.read_bytes() != payload:
            raise BenchmarkSafetyError("existing reveal differs from recomputation")
        action = "VERIFIED EXISTING"
    else:
        _write_new_bytes(output_path, payload)
        action = "CREATED"
    return document, output_path, action


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build, freeze, and reveal a balanced blind A/B benchmark review"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--repo-root", type=Path, required=True)
    build.add_argument("--recovery-manifest", type=Path, required=True)
    build.add_argument("--recovery-manifest-sha256", required=True)
    build.add_argument("--raw-corpus-manifest", type=Path, required=True)
    build.add_argument("--raw-corpus-manifest-sha256", required=True)
    build.add_argument("--finalization-manifest", type=Path, required=True)
    build.add_argument("--finalization-manifest-sha256", required=True)
    build.add_argument("--output-dir", type=Path, required=True)

    freeze = sub.add_parser("freeze-scores")
    freeze.add_argument("--bundle-dir", type=Path, required=True)
    freeze.add_argument("--scores-csv", type=Path, default=None)
    freeze.add_argument("--output", type=Path, default=None)

    reveal = sub.add_parser("reveal")
    reveal.add_argument("--bundle-dir", type=Path, required=True)
    reveal.add_argument("--scores", type=Path, default=None)
    reveal.add_argument("--output", type=Path, default=None)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest, commitment = build_bundle(
                repo_root=args.repo_root,
                recovery_path=args.recovery_manifest,
                recovery_sha256=args.recovery_manifest_sha256,
                raw_path=args.raw_corpus_manifest,
                raw_sha256=args.raw_corpus_manifest_sha256,
                finalization_path=args.finalization_manifest,
                finalization_sha256=args.finalization_manifest_sha256,
                output_dir=args.output_dir,
            )
            print("=" * 88)
            print("BLIND REVIEW BUNDLE BUILT")
            print("=" * 88)
            print("Cases                 :", manifest["case_count"])
            print("Anonymous candidates  :", manifest["candidate_count"])
            print("Balanced A/B per novel: YES")
            print("RAW/final prose equal : YES")
            print("Identity commitment   :", commitment)
            print("Output                :", args.output_dir)
            print("Provider calls        : 0")
            print("=" * 88)
            return 0

        if args.command == "freeze-scores":
            document, path, action = freeze_scores(
                bundle_dir=args.bundle_dir,
                scores_csv=args.scores_csv,
                output_path=args.output,
            )
            print("=" * 88)
            print("ANONYMOUS SCORES FROZEN")
            print("=" * 88)
            print("Action          :", action)
            print("Cases           :", len(document["rows"]))
            print("Score freeze ID :", document["score_freeze_id"])
            print("Output          :", path)
            print("Identity revealed: NO")
            print("=" * 88)
            return 0

        document, path, action = reveal_scores(
            bundle_dir=args.bundle_dir,
            scores_path=args.scores,
            output_path=args.output,
        )
        print("=" * 88)
        print("BLIND REVIEW IDENTITY REVEALED")
        print("=" * 88)
        print("Action :", action)
        for model, result in document["model_results"].items():
            print()
            print(model)
            print("  Mean weighted :", f"{result['mean_weighted_score']:.3f}")
            print("  Median        :", f"{result['median_weighted_score']:.3f}")
            print("  P10           :", f"{result['p10_weighted_score']:.3f}")
            print("  <90           :", result["score_below_90_count"])
            print("  <85           :", result["score_below_85_count"])
            print("  Review blockers:", result["publication_blocker_count"])
            print("  Preferences   :", result["pairwise_preferences"])
        print()
        print("Output :", path)
        print("=" * 88)
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
