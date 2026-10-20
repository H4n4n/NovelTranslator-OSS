import hashlib
import json
from pathlib import Path
import shutil

import pytest

from benchmark_matched_engine_finalize import (
    MatchedFinalizationError,
    finalize_matched,
)
from benchmark_novel import (
    BenchmarkSafetyError,
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
)
from benchmark_sample_plan import PLAN_SCHEMA_VERSION, _canonical_json_sha256
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_fixture(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")
    config = NovelConfig(
        id="matched_finalize_fixture",
        title="Matched Finalize Fixture",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=4,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)
    chapters = [
        {
            "chapter": number,
            "heading": f"第{number}章",
            "source_file": f"Text/{number:04d}.xhtml",
            "paragraph_count": 1,
            "character_count": len(text),
            "text": text,
        }
        for number, text in (
            (1, "姚雲施展鳴鶴拳。"),
            (2, "姚雲看見道。"),
            (3, "姚雲回到城中。"),
            (4, "姚雲再次施展鳴鶴拳。"),
        )
    ]
    project.chapters_file.write_text(
        json.dumps(chapters, ensure_ascii=False),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps(
            {
                "entries": {
                    "姚雲": {
                        "english": "Yao Yun",
                        "category": "character",
                        "status": "locked",
                        "strict_qa": True,
                        "auto_repair": True,
                        "repair_aliases": ["Yao-Yun"],
                    },
                    "鳴鶴拳": {
                        "english": "Minghequan (Calling Crane Boxing)",
                        "category": "technique",
                        "status": "locked",
                        "strict_qa": True,
                        "auto_repair": True,
                        "repair_aliases": ["Minghequan"],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    inputs = load_benchmark_inputs(config.id, tmp_path)
    selected = [1, 2, 3]
    rows = []
    for stratum, chapter in enumerate(selected, start=1):
        rows.append(
            {
                "chapter_number": chapter,
                "stratum": stratum,
                "role": "representative",
                "luna_request_sha256": _canonical_json_sha256(
                    request_kwargs(inputs, chapter, "luna")
                ),
                "deepseek_request_sha256": _canonical_json_sha256(
                    request_kwargs(inputs, chapter, "deepseek-pro")
                ),
            }
        )
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "benchmark": "mature-glossary-engine-benchmark-v2",
        "novel_id": config.id,
        "selected_chapters": selected,
        "selections": rows,
        "protected_state_binding": capture_protected_state(project),
    }
    plan_path = project.benchmark_dir / "sample-plan.json"
    _write_json(plan_path, plan)

    replacement = 4
    recovery = {
        "schema_version": 1,
        "benchmark": "mature-glossary-ambiguous-recovery-v2",
        "status": "frozen_zero_api",
        "recovery_id": "recovery-fixture",
        "authorization": {
            "ambiguous_target_retry_forbidden": True,
        },
        "replacement": {
            "novel_id": config.id,
            "retired_chapter": 2,
            "chapter_number": replacement,
            "stratum": 2,
            "role": "representative",
            "luna_request_sha256": _canonical_json_sha256(
                request_kwargs(inputs, replacement, "luna")
            ),
            "deepseek_request_sha256": _canonical_json_sha256(
                request_kwargs(inputs, replacement, "deepseek-pro")
            ),
        },
        "final_matched_sample": {
            "chapters_by_novel": {
                config.id: [1, 4, 3],
            }
        },
    }
    recovery_path = tmp_path / "external" / "recovery.json"
    recovery_sha = _write_json(recovery_path, recovery)

    translations = {
        1: "Chapter 1\n\nYao-Yun used Minghequan.",
        4: "Chapter 4\n\nYao-Yun used Minghequan again.",
        3: "Chapter 3\n\nYao Yun returned to the city.",
    }

    luna_dir = project.benchmark_dir / "raw-luna"
    luna_dir.mkdir(parents=True)
    luna_payloads = {}
    for chapter, text in translations.items():
        payload = (
            text.replace("\n", "\r\n").encode("utf-8")
            if chapter == 1
            else text.encode("utf-8")
        )
        luna_payloads[chapter] = payload
        (luna_dir / f"chapter_{chapter:04d}.txt").write_bytes(payload)
    luna_result = {
        "status": "complete",
        "novel_id": config.id,
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "model_version": "gpt-5.6-luna",
        "chapters_requested": [1, 4, 3],
        "chapters": [
            {
                "chapter_number": chapter,
                "finish_reason": "stop",
                "remaining_cjk_character_count": 0,
            }
            for chapter in [1, 4, 3]
        ],
        "transport": {
            "type": "openai_batch",
            "batch_id": "batch-fixture",
        },
        "safety": {
            "protected_state_verified_unchanged": True,
        },
    }
    _write_json(luna_dir / "results.json", luna_result)

    pairs = []
    for chapter in [1, 4, 3]:
        ds_relative = f"raw-ds/unit-{chapter:04d}"
        ds_dir = project.benchmark_dir / ds_relative
        ds_dir.mkdir(parents=True)
        ds_text = translations[chapter]
        ds_payload = (
            ds_text.replace("\n", "\r\n").encode("utf-8")
            if chapter == 4
            else ds_text.encode("utf-8")
        )
        (ds_dir / f"chapter_{chapter:04d}.txt").write_bytes(ds_payload)
        ds_result = {
            "status": "complete",
            "novel_id": config.id,
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "model_version": "DeepSeek-V4-Pro-fixture",
            "thinking": "disabled",
            "chapters_requested": [chapter],
            "chapters": [
                {
                    "chapter_number": chapter,
                    "finish_reason": "stop",
                    "remaining_cjk_character_count": 0,
                }
            ],
            "safety": {
                "protected_state_verified_unchanged": True,
            },
        }
        _write_json(ds_dir / "results.json", ds_result)
        source = inputs.chapters[chapter]["text"]
        pairs.append(
            {
                "novel_id": config.id,
                "chapter_number": chapter,
                "source_sha256": hashlib.sha256(
                    source.encode("utf-8")
                ).hexdigest(),
                "deepseek": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "output_dir": ds_relative,
                    "translation_sha256": hashlib.sha256(
                        ds_payload
                    ).hexdigest(),
                },
                "luna": {
                    "provider": "openai",
                    "model": "gpt-5.6-luna",
                    "batch_id": "batch-fixture",
                    "translation_sha256": hashlib.sha256(
                        luna_payloads[chapter]
                    ).hexdigest(),
                },
            }
        )

    corpus = {
        "schema_version": 1,
        "benchmark": "mature-glossary-raw-matched-corpus-v1",
        "status": "frozen_raw_complete",
        "recovery_id": "recovery-fixture",
        "source_count": 3,
        "translation_count": 6,
        "pairs": pairs,
    }
    corpus_path = tmp_path / "external" / "raw-corpus.json"
    corpus_sha = _write_json(corpus_path, corpus)

    return {
        "project": project,
        "inputs": inputs,
        "plan_path": plan_path,
        "recovery_path": recovery_path,
        "recovery_sha": recovery_sha,
        "corpus_path": corpus_path,
        "corpus_sha": corpus_sha,
        "luna_dir": "raw-luna",
    }


def _run(fixture, provider, output_dir):
    return finalize_matched(
        fixture["inputs"],
        plan_path=fixture["plan_path"],
        recovery_path=fixture["recovery_path"],
        recovery_sha256=fixture["recovery_sha"],
        raw_corpus_path=fixture["corpus_path"],
        raw_corpus_sha256=fixture["corpus_sha"],
        provider=provider,
        output_dir=output_dir,
        raw_output_dir=fixture["luna_dir"] if provider == "openai" else None,
    )


def test_raw_identity_hashes_exact_file_bytes_across_crlf_and_lf(tmp_path):
    fixture = _make_fixture(tmp_path)

    luna_path = (
        fixture["project"].benchmark_dir
        / "raw-luna"
        / "chapter_0001.txt"
    )
    deepseek_path = (
        fixture["project"].benchmark_dir
        / "raw-ds"
        / "unit-0004"
        / "chapter_0004.txt"
    )

    assert b"\r\n" in luna_path.read_bytes()
    assert b"\r\n" in deepseek_path.read_bytes()

    _run(fixture, "deepseek", "finalized-deepseek-crlf")
    _run(fixture, "openai", "finalized-luna-crlf")


def test_recovery_aware_finalization_supports_luna_and_sharded_deepseek(tmp_path):
    fixture = _make_fixture(tmp_path)
    before = capture_protected_state(fixture["project"])

    deepseek, ds_target = _run(fixture, "deepseek", "finalized-deepseek")
    luna, luna_target = _run(fixture, "openai", "finalized-luna")

    for document, target in ((deepseek, ds_target), (luna, luna_target)):
        assert document["chapters_requested"] == [1, 4, 3]
        assert document["recovery_id"] == "recovery-fixture"
        assert document["summary"]["chapter_count"] == 3
        assert document["safety"]["protected_state_verified_unchanged"] is True
        assert (target / "chapter_0004.txt").is_file()
        assert not (target / "chapter_0002.txt").exists()
        assert (target / "engine_finalization.json").is_file()

    assert deepseek["raw_transport"]["type"] == "synchronous_sharded"
    assert luna["raw_transport"]["type"] == "openai_batch"
    assert capture_protected_state(fixture["project"]) == before


def test_matched_finalization_rejects_bound_manifest_hash_drift(tmp_path):
    fixture = _make_fixture(tmp_path)
    with pytest.raises(BenchmarkSafetyError, match="RAW corpus manifest SHA256"):
        finalize_matched(
            fixture["inputs"],
            plan_path=fixture["plan_path"],
            recovery_path=fixture["recovery_path"],
            recovery_sha256=fixture["recovery_sha"],
            raw_corpus_path=fixture["corpus_path"],
            raw_corpus_sha256="0" * 64,
            provider="deepseek",
            output_dir="must-not-exist",
        )


def test_matched_finalization_rejects_raw_translation_sha_drift(tmp_path):
    fixture = _make_fixture(tmp_path)
    path = (
        fixture["project"].benchmark_dir
        / "raw-ds"
        / "unit-0004"
        / "chapter_0004.txt"
    )
    path.write_text("tampered", encoding="utf-8")

    with pytest.raises(BenchmarkSafetyError, match="DeepSeek RAW SHA mismatch"):
        _run(fixture, "deepseek", "must-not-exist")


def test_matched_finalization_rejects_replacement_role_drift(tmp_path):
    fixture = _make_fixture(tmp_path)
    recovery = json.loads(
        fixture["recovery_path"].read_text(encoding="utf-8")
    )
    recovery["replacement"]["role"] = "dialogue_stress"
    fixture["recovery_sha"] = _write_json(
        fixture["recovery_path"],
        recovery,
    )

    with pytest.raises(BenchmarkSafetyError, match="replacement role changed"):
        _run(fixture, "deepseek", "must-not-exist")
