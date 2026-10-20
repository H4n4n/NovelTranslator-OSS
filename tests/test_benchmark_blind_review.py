import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_blind_review as blind
from benchmark_novel import BenchmarkSafetyError


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    recovery = {
        "benchmark": "mature-glossary-ambiguous-recovery-v2",
        "status": "frozen_zero_api",
        "recovery_id": "recovery-fixture",
    }
    recovery_path = tmp_path / "external" / "recovery.json"
    recovery_sha = _write_json(recovery_path, recovery)

    inputs_by_novel = {}
    pairs = []
    arms = []

    for novel_id, language in (
        ("novel_zh", "zh"),
        ("novel_ko", "ko"),
    ):
        benchmark_dir = (
            repo
            / "novels"
            / novel_id
            / "output"
            / "benchmark"
        )
        benchmark_dir.mkdir(parents=True)

        chapters = {}
        for chapter in range(1, 31):
            if language == "zh":
                source = f"第{chapter}章\n\n源文 {chapter} 技法。"
                heading = f"第{chapter}章"
            else:
                source = f"제{chapter}화\n\n원문 {chapter} 기술."
                heading = f"제{chapter}화"
            chapters[chapter] = {
                "chapter": chapter,
                "heading": heading,
                "text": source,
            }

        project = SimpleNamespace(
            config=SimpleNamespace(
                id=novel_id,
                source_language=language,
            ),
            benchmark_dir=benchmark_dir,
        )
        inputs_by_novel[novel_id] = SimpleNamespace(
            project=project,
            chapters=chapters,
        )

        for provider, model, suffix in (
            ("deepseek", "deepseek-v4-pro", "ds"),
            ("openai", "gpt-5.6-luna", "luna"),
        ):
            output_dir = f"final/{suffix}"
            target = benchmark_dir / output_dir
            target.mkdir(parents=True)

            rows = []
            for chapter in range(1, 31):
                text = (
                    f"Chapter {chapter}\n\n"
                    f"{chapter + 45}. Fixture Reader Heading\n\n"
                    f"Translated body {novel_id} {chapter} {suffix}."
                )
                (target / f"chapter_{chapter:04d}.txt").write_text(
                    text,
                    encoding="utf-8",
                )
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                rows.append(
                    {
                        "chapter_number": chapter,
                        "raw_sha256": text_sha,
                        "deterministic_final_sha256": text_sha,
                        "text_changed": False,
                    }
                )

            engine = {
                "benchmark":
                    "mature-glossary-matched-engine-finalization-v2",
                "novel_id": novel_id,
                "provider": provider,
                "model": model,
                "recovery_manifest_sha256": recovery_sha,
                "raw_corpus_manifest_sha256": "RAW_SHA_PLACEHOLDER",
                "safety": {
                    "protected_state_verified_unchanged": True,
                },
                "chapters": rows,
            }
            engine_path = target / "engine_finalization.json"
            _write_json(engine_path, engine)

    for novel_id in inputs_by_novel:
        for chapter in range(1, 31):
            source = inputs_by_novel[novel_id].chapters[chapter]["text"]
            pairs.append(
                {
                    "novel_id": novel_id,
                    "chapter_number": chapter,
                    "source_sha256": hashlib.sha256(
                        source.encode("utf-8")
                    ).hexdigest(),
                    "deepseek": {
                        "translation_sha256": "unused-in-builder",
                    },
                    "luna": {
                        "translation_sha256": "unused-in-builder",
                    },
                }
            )

    raw = {
        "benchmark": "mature-glossary-raw-matched-corpus-v1",
        "status": "frozen_raw_complete",
        "recovery_id": "recovery-fixture",
        "source_count": 60,
        "translation_count": 120,
        "pairs": pairs,
        "costs_usd": {
            "deepseek_scored_known": 1.0,
            "luna_batch_actual": 0.25,
        },
    }
    raw_path = tmp_path / "external" / "raw.json"
    raw_sha = _write_json(raw_path, raw)

    # Rebind the engine documents now that the RAW manifest SHA is known.
    for novel_id, inputs in inputs_by_novel.items():
        for provider, model, suffix in (
            ("deepseek", "deepseek-v4-pro", "ds"),
            ("openai", "gpt-5.6-luna", "luna"),
        ):
            target = inputs.project.benchmark_dir / f"final/{suffix}"
            engine_path = target / "engine_finalization.json"
            engine = json.loads(engine_path.read_text(encoding="utf-8"))
            engine["raw_corpus_manifest_sha256"] = raw_sha
            engine_sha = _write_json(engine_path, engine)
            arms.append(
                {
                    "key": f"{novel_id}_{suffix}",
                    "novel_id": novel_id,
                    "provider": provider,
                    "model": model,
                    "output_dir": f"final/{suffix}",
                    "engine_finalization_sha256": engine_sha,
                    "summary": {
                        "chapter_count": 30,
                    },
                }
            )

    finalization = {
        "benchmark":
            "mature-glossary-deterministic-finalization-v1",
        "status": "frozen_complete",
        "repository_main_sha": "fixture-main",
        "recovery_id": "recovery-fixture",
        "recovery_manifest_sha256": recovery_sha,
        "raw_corpus_manifest_sha256": raw_sha,
        "matched_source_count": 60,
        "deterministic_finalized_translation_count": 120,
        "aggregate": {
            "deepseek": {"chapter_count": 60},
            "luna": {"chapter_count": 60},
        },
        "arms": arms,
    }
    finalization_path = tmp_path / "external" / "finalization.json"
    finalization_sha = _write_json(finalization_path, finalization)

    monkeypatch.setattr(
        blind,
        "load_benchmark_inputs",
        lambda novel_id, repo_root: inputs_by_novel[novel_id],
    )
    monkeypatch.setattr(
        blind,
        "resolve_output_dir",
        lambda project, relative: project.benchmark_dir / relative,
    )
    monkeypatch.setattr(
        blind,
        "relevant_glossary_for_chapter",
        lambda inputs, chapter: [
            {
                "chinese": "技法",
                "english": "Technique",
                "category": "technique",
                "status": "locked",
            }
        ],
    )
    monkeypatch.setattr(
        blind,
        "_verify_repository_compatibility",
        lambda repo_root, frozen_finalization_main: "fixture-current",
    )

    return {
        "repo": repo,
        "recovery_path": recovery_path,
        "recovery_sha": recovery_sha,
        "raw_path": raw_path,
        "raw_sha": raw_sha,
        "finalization_path": finalization_path,
        "finalization_sha": finalization_sha,
    }


def _build(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "blind"
    manifest, commitment = blind.build_bundle(
        repo_root=fixture["repo"],
        recovery_path=fixture["recovery_path"],
        recovery_sha256=fixture["recovery_sha"],
        raw_path=fixture["raw_path"],
        raw_sha256=fixture["raw_sha"],
        finalization_path=fixture["finalization_path"],
        finalization_sha256=fixture["finalization_sha"],
        output_dir=output,
    )
    fixture["output"] = output
    fixture["manifest"] = manifest
    fixture["commitment"] = commitment
    return fixture


def _fill_scores(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    for index, row in enumerate(rows):
        for dimension in blind.DIMENSIONS:
            row[f"a_{dimension}"] = "9"
            row[f"b_{dimension}"] = "8"
        row["a_blocker"] = "0"
        row["b_blocker"] = "0"
        row["preference"] = "A" if index % 3 else "TIE"
        row["notes"] = f"review {index}"

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=blind.SCORE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_blind_display_collapses_plain_and_markdown_duplicate_headings():
    text = (
        "Chapter 22\n\n"
        "# Chapter 22 — The Real Thing Is Different (3)\n\n"
        "Body line.\n"
    )

    blinded, removed = blind._blind_display_text(text)

    assert removed == 2
    assert blinded.count(blind._BLIND_HEADING_PLACEHOLDER) == 1
    assert "Chapter 22" not in blinded
    assert blinded.endswith("Body line.\n")


def test_blind_display_collapses_existing_placeholder_and_markdown_heading():
    text = (
        f"{blind._BLIND_HEADING_PLACEHOLDER}\n\n"
        "## Chapter 135: Divine Beast—The One-Horned Si Bull\n\n"
        "Body line.\n"
    )

    blinded, removed = blind._blind_display_text(text)

    assert removed == 2
    assert blinded.count(blind._BLIND_HEADING_PLACEHOLDER) == 1
    assert "Chapter 135" not in blinded
    assert blinded.endswith("Body line.\n")


def test_blind_display_collapses_reader_facing_numeric_heading_after_placeholder():
    text = (
        f"{blind._BLIND_HEADING_PLACEHOLDER}\n\n"
        "46. The Great Upheaval (2)\n\n"
        "Body line.\n"
    )

    blinded, removed = blind._blind_display_text(text)

    assert removed == 2
    assert blinded.count(blind._BLIND_HEADING_PLACEHOLDER) == 1
    assert "46. The Great Upheaval (2)" not in blinded
    assert blinded.endswith("Body line.\n")


def test_blind_display_uses_known_chapter_for_numeric_markdown_heading():
    text = "## 135: Divine Beast\n\nBody line.\n"

    blinded, removed = blind._blind_display_text(
        text,
        known_chapter=135,
    )

    assert removed == 1
    assert blind._BLIND_HEADING_PLACEHOLDER in blinded
    assert "135:" not in blinded
    assert blinded.endswith("Body line.\n")


def test_build_bundle_is_balanced_blind_and_heading_neutral(tmp_path, monkeypatch):
    fixture = _build(tmp_path, monkeypatch)
    output = fixture["output"]

    visible_payload = (output / "bundle_manifest.json").read_text(encoding="utf-8")
    assert "deepseek" not in visible_payload.lower()
    assert "gpt-5.6-luna" not in visible_payload.lower()
    assert "chapter_number" not in visible_payload

    key_payload = (output / "private" / "identity_key.json").read_bytes()
    assert hashlib.sha256(key_payload).hexdigest() == fixture["commitment"]
    key = json.loads(key_payload.decode("utf-8"))

    for novel_id in ("novel_zh", "novel_ko"):
        rows = [row for row in key["cases"] if row["novel_id"] == novel_id]
        assert len(rows) == 30
        assert sum(
            row["candidate_a"]["model"] == "deepseek-v4-pro"
            for row in rows
        ) == 15
        assert sum(
            row["candidate_a"]["model"] == "gpt-5.6-luna"
            for row in rows
        ) == 15

    assert fixture["manifest"]["case_count"] == 60
    assert fixture["manifest"]["candidate_count"] == 120
    assert fixture["manifest"]["raw_final_text_equivalent_for_all_candidates"] is True

    first = output / "cases" / "BR001"
    assert "[CHAPTER HEADING WITHHELD FOR BLINDING]" in (
        first / "source.txt"
    ).read_text(encoding="utf-8")
    assert "[CHAPTER HEADING WITHHELD FOR BLINDING]" in (
        first / "A.txt"
    ).read_text(encoding="utf-8")
    assert "[CHAPTER HEADING WITHHELD FOR BLINDING]" in (
        first / "B.txt"
    ).read_text(encoding="utf-8")

    for case_dir in (output / "cases").iterdir():
        if not case_dir.is_dir():
            continue
        a_text = (case_dir / "A.txt").read_text(encoding="utf-8")
        b_text = (case_dir / "B.txt").read_text(encoding="utf-8")
        assert "Fixture Reader Heading" not in a_text
        assert "Fixture Reader Heading" not in b_text

    with (output / "score_sheet.csv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 60
    assert [row["case_id"] for row in rows] == [
        f"BR{index:03d}" for index in range(1, 61)
    ]


def test_build_bundle_rejects_finalization_manifest_hash_drift(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    with pytest.raises(BenchmarkSafetyError, match="deterministic finalization manifest SHA256"):
        blind.build_bundle(
            repo_root=fixture["repo"],
            recovery_path=fixture["recovery_path"],
            recovery_sha256=fixture["recovery_sha"],
            raw_path=fixture["raw_path"],
            raw_sha256=fixture["raw_sha"],
            finalization_path=fixture["finalization_path"],
            finalization_sha256="0" * 64,
            output_dir=tmp_path / "must-not-exist",
        )


def test_repository_compatibility_accepts_allowed_descendant_drift(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        blind,
        "_git_head",
        lambda repo_root: "current-main",
    )

    monkeypatch.setattr(
        blind.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    monkeypatch.setattr(
        blind.subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "benchmark_blind_review.py\n"
            "tests/test_benchmark_blind_review.py\n"
        ),
    )

    assert (
        blind._verify_repository_compatibility(
            repo,
            "frozen-main",
        )
        == "current-main"
    )


def test_repository_compatibility_rejects_unrelated_descendant_drift(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        blind,
        "_git_head",
        lambda repo_root: "current-main",
    )

    monkeypatch.setattr(
        blind.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    monkeypatch.setattr(
        blind.subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "benchmark_blind_review.py\n"
            "noveltranslator_core.py\n"
        ),
    )

    with pytest.raises(
        BenchmarkSafetyError,
        match="outside blind-review tooling",
    ):
        blind._verify_repository_compatibility(
            repo,
            "frozen-main",
        )


def test_repository_compatibility_rejects_non_descendant(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        blind,
        "_git_head",
        lambda repo_root: "unrelated-main",
    )

    monkeypatch.setattr(
        blind.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(
        BenchmarkSafetyError,
        match="not descended from frozen finalization main",
    ):
        blind._verify_repository_compatibility(
            repo,
            "frozen-main",
        )


def test_freeze_scores_requires_complete_valid_sheet(tmp_path, monkeypatch):
    fixture = _build(tmp_path, monkeypatch)
    score_sheet = fixture["output"] / "score_sheet.csv"

    with pytest.raises(blind.BlindReviewError, match="must be numeric"):
        blind.freeze_scores(bundle_dir=fixture["output"])

    _fill_scores(score_sheet)
    document, path, action = blind.freeze_scores(bundle_dir=fixture["output"])

    assert action == "CREATED"
    assert path.name == "scores_frozen.json"
    assert document["status"] == "anonymous_scores_frozen"
    assert len(document["rows"]) == 60
    assert "deepseek" not in path.read_text(encoding="utf-8").lower()
    assert "gpt-5.6-luna" not in path.read_text(encoding="utf-8").lower()


def test_reveal_requires_frozen_scores_and_then_joins_identity(tmp_path, monkeypatch):
    fixture = _build(tmp_path, monkeypatch)

    with pytest.raises(blind.BlindReviewError, match="frozen anonymous scores"):
        blind.reveal_scores(bundle_dir=fixture["output"])

    _fill_scores(fixture["output"] / "score_sheet.csv")
    frozen, _, _ = blind.freeze_scores(bundle_dir=fixture["output"])
    result, path, action = blind.reveal_scores(bundle_dir=fixture["output"])

    assert action == "CREATED"
    assert result["status"] == "identity_revealed_after_score_freeze"
    assert result["score_freeze_id"] == frozen["score_freeze_id"]
    assert result["model_results"]["deepseek-v4-pro"]["candidate_count"] == 60
    assert result["model_results"]["gpt-5.6-luna"]["candidate_count"] == 60
    assert len(result["cases"]) == 60
    assert path.name == "revealed_results.json"
    assert result["raw_costs_usd"]["deepseek_scored_known"] == 1.0
    assert result["engineering_repair_burden"]["deepseek"]["chapter_count"] == 60


def test_reveal_rejects_identity_key_tamper(tmp_path, monkeypatch):
    fixture = _build(tmp_path, monkeypatch)
    _fill_scores(fixture["output"] / "score_sheet.csv")
    blind.freeze_scores(bundle_dir=fixture["output"])

    key_path = fixture["output"] / "private" / "identity_key.json"
    key_path.write_bytes(key_path.read_bytes() + b"\n")

    with pytest.raises(BenchmarkSafetyError, match="identity-key commitment mismatch"):
        blind.reveal_scores(bundle_dir=fixture["output"])
