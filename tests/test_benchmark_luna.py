import json
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from benchmark_novel import (
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
    run_benchmark,
)
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 8, 21, 2, tzinfo=timezone.utc)


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number=1, text="姚雲使用神通。"):
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": 1,
        "character_count": len(text),
        "text": text,
    }


def _make_epub_fixture(tmp_path):
    _prepare_repo(tmp_path)
    config = NovelConfig(
        id="luna_fixture",
        title="Luna Fixture",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=1,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project("luna_fixture", tmp_path)
    project.chapters_file.write_text(
        json.dumps([_chapter()], ensure_ascii=False),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps({"entries": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return project


class OpenAIUsageClient:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Chapter 1\n\nNatural English translation."
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=500,
                prompt_tokens_details=SimpleNamespace(cached_tokens=200),
            ),
        )


def _fixed_timer():
    values = iter((10.0, 12.0))
    return lambda: next(values)


def test_luna_request_uses_openai_specific_non_reasoning_shape(tmp_path):
    project = _make_epub_fixture(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    kwargs = request_kwargs(inputs, 1, "luna")

    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["max_completion_tokens"] == 12000
    assert kwargs["reasoning_effort"] == "none"
    assert "max_tokens" not in kwargs
    assert "extra_body" not in kwargs


def test_deepseek_request_shape_remains_unchanged(tmp_path):
    project = _make_epub_fixture(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    kwargs = request_kwargs(inputs, 1, "deepseek-pro")

    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["max_tokens"] == 12000
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "max_completion_tokens" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_luna_usage_accounting_includes_nested_cached_tokens_and_batch_projection(
    tmp_path,
):
    project = _make_epub_fixture(tmp_path)
    before = capture_protected_state(project)
    client = OpenAIUsageClient()

    document = run_benchmark(
        load_benchmark_inputs(project.config.id, tmp_path),
        [1],
        client,
        output_dir="luna",
        now=lambda: FIXED_TIME,
        timer=_fixed_timer(),
        model="luna",
    )

    result = document["chapters"][0]
    assert document["provider"] == "openai"
    assert document["model"] == "gpt-5.6-luna"
    assert result["cache_hit_tokens"] == 200
    assert result["cache_miss_tokens"] == 800
    assert result["total_cost_usd"] == pytest.approx(0.000764)
    assert result["economy_projected_cost_usd"] == pytest.approx(0.000382)
    assert result["economy_uncached_projected_cost_usd"] == pytest.approx(0.0004)
    assert result["economy_pricing_mode"] == "batch_50_percent_projection"
    assert document["totals"]["economy_projected_cost_usd"] == pytest.approx(
        0.000382
    )
    assert capture_protected_state(project) == before


def test_missing_txt_chapter_library_is_extracted_in_memory_without_install(tmp_path):
    _prepare_repo(tmp_path)
    config = NovelConfig(
        id="korean_txt_benchmark",
        title="Korean TXT Benchmark",
        source_language="ko",
        target_language="en",
        profile="korean_fantasy",
        final_chapter=2,
        chapter_numbering="spine_sequential",
        source_type="txt",
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project("korean_txt_benchmark", tmp_path)
    raw = "Prologue\n\n첫 본문.\n2화. 둘째\n\n둘째 본문.\n".encode("utf-8")
    project.source_txt.write_bytes(raw)
    project.chapters_file.unlink(missing_ok=True)
    project.glossary_file.write_text(
        json.dumps({"entries": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    before = capture_protected_state(project)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)

    assert set(inputs.chapters) == {1, 2}
    assert inputs.chapters[1]["unit_kind"] == "prologue"
    assert inputs.chapters[2]["source_number"] == 2
    assert not project.chapters_file.exists()
    assert capture_protected_state(project) == before


def test_benchmark_client_has_no_sdk_retry_path_for_either_provider():
    source = (REPO_ROOT / "benchmark_novel.py").read_text(encoding="utf-8")
    block = source[
        source.index("def create_benchmark_client(config):"):
        source.index("def build_parser():")
    ]
    assert block.count("max_retries=0") == 2
    assert "DEEPSEEK_API_KEY" in block
    assert "OPENAI_API_KEY" in block
