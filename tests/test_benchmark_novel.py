from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import benchmark_novel
from benchmark_novel import (
    BenchmarkSafetyError,
    RESULTS_FILENAME,
    capture_protected_state,
    load_benchmark_inputs,
    request_kwargs,
    resolve_output_dir,
    run_benchmark,
    validate_chapter_selection,
)
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project
from noveltranslator_core import calculate_cost_from_token_counts, pricing_snapshot
from production_run_lock import ProductionRunLockedError, production_run_lock


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED_PEAK_TIME = datetime(2026, 8, 21, 2, tzinfo=timezone.utc)


def prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")
    return tmp_path


def make_project(
    repo,
    novel_id,
    profile="xianxia_xuanhuan",
    chapters=None,
    glossary=None,
    addendum="",
    chapter_numbering="global",
    source_language="zh",
):
    config = NovelConfig(
        id=novel_id,
        title=novel_id.replace("_", " ").title(),
        source_language=source_language,
        target_language="en",
        profile=profile,
        final_chapter=20,
        chapter_numbering=chapter_numbering,
    )
    create_novel_project(config, repo)
    project = load_novel_project(novel_id, repo)
    project.chapters_file.write_text(
        json.dumps(chapters or [chapter(1, "姚雲使用神通。")], ensure_ascii=False),
        encoding="utf-8",
    )
    project.glossary_file.write_text(
        json.dumps({"entries": glossary or {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    project.translation_addendum_file.write_text(addendum, encoding="utf-8")
    return project


def chapter(number, text):
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": len([part for part in text.split("\n\n") if part]),
        "character_count": len(text),
        "text": text,
    }


class FakeClient:
    def __init__(self, translations=None, usage=None):
        self.calls = []
        self.translations = iter(translations or ["Chapter 1\n\nEnglish text."])
        self.usage = usage or SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=500,
            prompt_cache_hit_tokens=600,
            prompt_cache_miss_tokens=400,
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=next(self.translations)),
                finish_reason="stop",
            )],
            usage=self.usage,
        )


def fixed_timer(start=10.0, elapsed=2.5):
    values = iter((start, start + elapsed))
    return lambda: next(values)


def test_benchmark_loads_selected_profile_addendum_and_glossary(tmp_path):
    prepare_repo(tmp_path)
    first = make_project(
        tmp_path,
        "novel_a",
        glossary={
            "姚雲": {
                "english": "Yao Yun",
                "category": "character",
                "status": "locked",
            },
            "不相關": {
                "english": "Irrelevant",
                "category": "specialized recurring term",
                "status": "locked",
            },
        },
        addendum="NOVEL A ONLY POLICY",
    )
    make_project(
        tmp_path,
        "novel_b",
        profile="historical_military",
        addendum="NOVEL B ONLY POLICY",
    )

    inputs = load_benchmark_inputs("novel_a", tmp_path)
    kwargs = request_kwargs(inputs, 1)
    system_prompt = kwargs["messages"][0]["content"]
    user_prompt = kwargs["messages"][1]["content"]

    assert inputs.project.root == first.root
    assert inputs.profile.id == "xianxia_xuanhuan"
    assert "NOVEL A ONLY POLICY" in system_prompt
    assert "NOVEL B ONLY POLICY" not in system_prompt
    assert "姚雲 → Yao Yun" in user_prompt
    assert "不相關" not in user_prompt
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["max_tokens"] == 12000
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_benchmark_helpers_accept_infinite_flow_profile_without_api(tmp_path):
    prepare_repo(tmp_path)
    make_project(
        tmp_path,
        "flow_benchmark",
        profile="infinite_flow",
        chapters=[chapter(1, "主神提示：支线任务开启。")],
        glossary={
            "主神提示": {
                "english": "System Notice",
                "category": "system term",
                "status": "locked",
            },
            "无关术语": {
                "english": "Irrelevant Term",
                "category": "specialized recurring term",
                "status": "provisional",
            },
        },
        addendum="FLOW BENCHMARK NOVEL POLICY",
    )
    inputs = load_benchmark_inputs("flow_benchmark", tmp_path)
    kwargs = request_kwargs(inputs, 1)

    assert inputs.profile.id == "infinite_flow"
    assert "FLOW BENCHMARK NOVEL POLICY" in kwargs["messages"][0]["content"]
    assert "主神提示 → System Notice" in kwargs["messages"][1]["content"]
    assert "无关术语" not in kwargs["messages"][1]["content"]
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_benchmark_outputs_and_protected_state_are_isolated_per_novel(tmp_path):
    prepare_repo(tmp_path)
    first = make_project(tmp_path, "novel_a")
    second = make_project(tmp_path, "novel_b")
    first_before = capture_protected_state(first)
    second_before = capture_protected_state(second)
    client = FakeClient()

    document = run_benchmark(
        load_benchmark_inputs("novel_a", tmp_path),
        [1],
        client,
        now=lambda: FIXED_PEAK_TIME,
        timer=fixed_timer(),
        sleep=lambda _: None,
    )

    assert (first.benchmark_dir / "pilot/chapter_0001.txt").is_file()
    assert (first.benchmark_dir / f"pilot/{RESULTS_FILENAME}").is_file()
    assert not second.benchmark_dir.exists()
    assert capture_protected_state(first) == first_before
    assert capture_protected_state(second) == second_before
    assert document["safety"]["protected_state_verified_unchanged"] is True


def test_distant_chapter_receives_no_fabricated_or_saved_continuity(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(
        tmp_path,
        "distant",
        chapters=[chapter(1, "開始"), chapter(18, "姚雲渡劫。")],
    )
    project.continuity_file.write_text(
        json.dumps({"latest_chapter": 17, "summary": "SECRET SAVED CONTINUITY"}),
        encoding="utf-8",
    )
    inputs = load_benchmark_inputs("distant", tmp_path)
    user_prompt = request_kwargs(inputs, 18)["messages"][1]["content"]

    assert "SECRET SAVED CONTINUITY" not in user_prompt
    assert "RECENT CONTINUITY" not in user_prompt
    assert "CHINESE SOURCE — CHAPTER 18" in user_prompt


def test_benchmark_selection_uses_production_index_for_reset_numbering(tmp_path):
    prepare_repo(tmp_path)
    reset_chapter = chapter(3, "第一章 新篇正文")
    reset_chapter.update({
        "production_index": 3,
        "local_chapter_number": 1,
        "local_chapter_start": 1,
        "local_chapter_end": 1,
        "volume_index": 2,
        "heading": "第一章 新篇",
        "title": "新篇",
    })
    make_project(
        tmp_path,
        "reset_benchmark",
        chapters=[reset_chapter],
        chapter_numbering="spine_sequential",
    )
    inputs = load_benchmark_inputs("reset_benchmark", tmp_path)

    assert validate_chapter_selection(inputs, [3]) == (3,)
    prompt = request_kwargs(inputs, 3)["messages"][1]["content"]
    assert "PRODUCTION UNIT 3 (INTERNAL CONTROL INDEX" in prompt
    assert "READER-FACING LOCAL IDENTITY: Chapter 1" in prompt
    assert "SOURCE HEADING/TITLE TO TRANSLATE: 第一章 新篇" in prompt
    assert "Translate the heading from zh into en" in prompt
    assert "do not copy source-language script" in prompt
    assert "Chapter 3" not in prompt


def test_benchmark_can_address_supplement_by_production_index(tmp_path):
    prepare_repo(tmp_path)
    supplement = {
        "chapter": 3,
        "production_index": 3,
        "unit_kind": "supplement",
        "volume_index": 4,
        "heading": "李閻全信息",
        "title": "李閻全信息",
        "source_file": "Text/0171_.xhtml",
        "spine_position": 172,
        "text": "姓名：李閻",
    }
    make_project(
        tmp_path,
        "supplement_benchmark",
        chapters=[supplement],
        chapter_numbering="spine_sequential",
    )
    inputs = load_benchmark_inputs("supplement_benchmark", tmp_path)
    assert validate_chapter_selection(inputs, [3]) == (3,)
    prompt = request_kwargs(inputs, 3)["messages"][1]["content"]
    assert "UNIT KIND: SUPPLEMENTAL DOCUMENT" in prompt
    assert "SOURCE TITLE TO TRANSLATE: 李閻全信息" in prompt
    assert "Chapter 3" not in prompt


def test_relevant_glossary_filtering_preserves_production_format(tmp_path):
    prepare_repo(tmp_path)
    make_project(
        tmp_path,
        "glossary_test",
        glossary={
            "姚雲": {
                "english": "Yao Yun",
                "category": "character",
                "status": "locked",
                "notes": "Approved name.",
            },
            "天子": {
                "english": "Son of Heaven",
                "category": "noble title",
                "status": "locked",
            },
        },
    )
    inputs = load_benchmark_inputs("glossary_test", tmp_path)
    user_prompt = request_kwargs(inputs, 1)["messages"][1]["content"]

    assert "姚雲 → Yao Yun" in user_prompt
    assert "Status: LOCKED" in user_prompt
    assert "Category: character" in user_prompt
    assert "Notes: Approved name." in user_prompt
    assert "天子" not in user_prompt


def test_dynamic_cost_and_cache_accounting_use_current_peak_rates(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "priced")
    client = FakeClient()
    document = run_benchmark(
        load_benchmark_inputs("priced", tmp_path),
        [1],
        client,
        now=lambda: FIXED_PEAK_TIME,
        timer=fixed_timer(elapsed=3.25),
        sleep=lambda _: None,
    )
    result = document["chapters"][0]
    price = pricing_snapshot(FIXED_PEAK_TIME)
    expected_total, _, _ = calculate_cost_from_token_counts(
        1000, 500, 600, 400, price
    )

    assert result["pricing_period"] == "peak"
    assert result["cache_hit_tokens"] == 600
    assert result["cache_miss_tokens"] == 400
    assert result["input_cost_usd"] == pytest.approx(0.0005544)
    assert result["output_cost_usd"] == pytest.approx(0.00198)
    assert result["total_cost_usd"] == pytest.approx(expected_total)
    assert document["totals"]["total_cost_usd"] == pytest.approx(expected_total)
    assert json.loads(
        (project.benchmark_dir / "pilot/results.json").read_text(encoding="utf-8")
    ) == document


@pytest.mark.parametrize(
    "moment, expected_period",
    [
        (datetime(2026, 8, 21, 5, tzinfo=timezone.utc), "off_peak"),
        (datetime(2026, 8, 21, 7, tzinfo=timezone.utc), "peak"),
        (datetime(2026, 8, 22, 7, tzinfo=timezone.utc), "off_peak"),
    ],
)
def test_benchmark_runs_immediately_but_reports_canonical_rate(
    tmp_path, moment, expected_period
):
    prepare_repo(tmp_path)
    make_project(tmp_path, "priced_now")
    document = run_benchmark(
        load_benchmark_inputs("priced_now", tmp_path),
        [1],
        FakeClient(),
        now=lambda: moment,
        timer=fixed_timer(),
        sleep=lambda _: None,
    )
    result = document["chapters"][0]
    pricing = pricing_snapshot(moment)
    expected_cost, _, _ = calculate_cost_from_token_counts(
        1000, 500, 600, 400, pricing
    )
    assert result["pricing_period"] == expected_period
    assert result["total_cost_usd"] == pytest.approx(expected_cost)


def test_legacy_benchmark_scripts_use_canonical_dynamic_pricing_helpers():
    for filename in ("benchmark_chapter.py", "benchmark_more_chapters.py"):
        source = (REPO_ROOT / filename).read_text(encoding="utf-8")
        assert "pricing_snapshot" in source
        assert "calculate_cost_from_token_counts" in source
        assert "PRICE_CACHE_MISS_INPUT" not in source
        assert "PRICE_CACHE_HIT_INPUT" not in source
        assert "PRICE_OUTPUT" not in source


def test_results_json_has_stable_explicit_schema(tmp_path):
    prepare_repo(tmp_path)
    make_project(tmp_path, "schema")
    document = run_benchmark(
        load_benchmark_inputs("schema", tmp_path),
        [1],
        FakeClient(),
        now=lambda: FIXED_PEAK_TIME,
        timer=fixed_timer(),
        sleep=lambda _: None,
    )

    assert set(document) == {
        "schema_version", "status", "novel_id", "novel_title", "profile_id",
        "provider", "model", "model_version", "thinking", "max_tokens",
        "continuity_mode", "chapters_requested", "chapters", "totals", "safety",
    }
    assert set(document["chapters"][0]) == {
        "chapter_number", "source_character_count", "source_paragraph_count",
        "elapsed_seconds", "finish_reason", "prompt_tokens", "completion_tokens",
        "cache_hit_tokens", "cache_miss_tokens", "translation_character_count",
        "translation_word_count", "remaining_cjk_character_count",
        "residual_cjk_fragments", "input_cost_usd", "output_cost_usd",
        "total_cost_usd", "pricing_version", "pricing_period", "pricing_mode",
        "request_started_utc", "cache_hit_rate_per_million",
        "cache_miss_rate_per_million", "output_rate_per_million",
        "economy_projected_cost_usd", "economy_uncached_projected_cost_usd",
        "economy_pricing_version", "economy_pricing_period",
        "economy_pricing_mode", "economy_cache_hit_rate_per_million",
        "economy_cache_miss_rate_per_million", "economy_output_rate_per_million",
        "output_path",
    }
    assert set(document["totals"]) == {
        "elapsed_seconds", "input_tokens", "output_tokens", "cache_hit_tokens",
        "cache_miss_tokens", "input_cost_usd", "output_cost_usd", "total_cost_usd",
        "economy_projected_cost_usd", "economy_uncached_projected_cost_usd",
    }


def test_benchmark_reports_residual_cjk_without_mutating_production(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "residual_qa")
    before = capture_protected_state(project)
    document = run_benchmark(
        load_benchmark_inputs("residual_qa", tmp_path),
        [1],
        FakeClient(translations=["Chapter 1\n\nHe felt心虚!"]),
        now=lambda: FIXED_PEAK_TIME,
        timer=fixed_timer(),
        sleep=lambda _: None,
    )

    result = document["chapters"][0]
    assert result["remaining_cjk_character_count"] == 2
    assert result["residual_cjk_fragments"] == ["心虚"]
    assert capture_protected_state(project) == before
    assert document["safety"]["protected_state_verified_unchanged"] is True


@pytest.mark.parametrize("fragment", ("隸屬", "支配", "心魔"))
def test_benchmark_uses_canonical_korean_source_script_detection(
    tmp_path, fragment
):
    prepare_repo(tmp_path)
    project = make_project(
        tmp_path,
        "korean_residual_qa",
        source_language="ko",
        chapters=[chapter(1, "이 문장은 합성 테스트 문장이다.")],
    )
    before = capture_protected_state(project)
    document = run_benchmark(
        load_benchmark_inputs("korean_residual_qa", tmp_path),
        [1],
        FakeClient(translations=[
            f"Chapter 1\n\nThe formal label remains {fragment} in English."
        ]),
        now=lambda: FIXED_PEAK_TIME,
        timer=fixed_timer(),
        sleep=lambda _: None,
    )

    result = document["chapters"][0]
    assert result["remaining_cjk_character_count"] == 2
    assert result["residual_cjk_fragments"] == [fragment]
    assert capture_protected_state(project) == before


@pytest.mark.parametrize("selection", ([0], [21], [2], [1, 1]))
def test_invalid_chapter_numbers_fail_before_output_or_api(tmp_path, selection):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "invalid")
    inputs = load_benchmark_inputs("invalid", tmp_path)
    client = FakeClient()
    with pytest.raises(benchmark_novel.BenchmarkError):
        run_benchmark(inputs, selection, client)
    assert client.calls == []
    assert not project.benchmark_dir.exists()


def test_output_path_cannot_escape_selected_novel_benchmark_tree(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "safe_path")
    with pytest.raises(BenchmarkSafetyError, match="must remain"):
        resolve_output_dir(project, project.root / "output/pending")


def test_existing_paid_benchmark_output_is_not_overwritten_or_rebilled(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "existing")
    output_dir = project.benchmark_dir / "pilot"
    output_dir.mkdir(parents=True)
    prior = output_dir / "chapter_0001.txt"
    prior.write_text("paid benchmark output", encoding="utf-8")
    client = FakeClient()
    with pytest.raises(BenchmarkSafetyError, match="not overwritten"):
        run_benchmark(load_benchmark_inputs("existing", tmp_path), [1], client)
    assert prior.read_text(encoding="utf-8") == "paid benchmark output"
    assert client.calls == []


def test_same_target_concurrent_benchmarks_issue_only_one_paid_request(
    tmp_path,
    monkeypatch,
):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "concurrent")
    inputs = load_benchmark_inputs("concurrent", tmp_path)
    before = capture_protected_state(project)
    clients = [
        FakeClient(translations=["Chapter 1\n\nFirst worker."]),
        FakeClient(translations=["Chapter 1\n\nSecond worker."]),
    ]

    real_claim = benchmark_novel._claim_results_path
    claim_barrier = threading.Barrier(2, timeout=5)

    def synchronized_claim(path, document):
        claim_barrier.wait()
        return real_claim(path, document)

    monkeypatch.setattr(
        benchmark_novel,
        "_claim_results_path",
        synchronized_claim,
    )

    def worker(client):
        try:
            document = run_benchmark(
                inputs,
                [1],
                client,
                now=lambda: FIXED_PEAK_TIME,
                timer=fixed_timer(),
                sleep=lambda _: None,
            )
        except BenchmarkSafetyError as exc:
            return "blocked", str(exc)
        return "complete", document["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, clients))

    assert sorted(status for status, _ in outcomes) == ["blocked", "complete"]
    blocked = next(detail for status, detail in outcomes if status == "blocked")
    assert "not overwritten" in blocked
    assert sum(len(client.calls) for client in clients) == 1

    results_path = project.benchmark_dir / f"pilot/{RESULTS_FILENAME}"
    saved = json.loads(results_path.read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert len(saved["chapters"]) == 1
    assert (project.benchmark_dir / "pilot/chapter_0001.txt").is_file()
    assert capture_protected_state(project) == before


def test_failed_exclusive_claim_blocks_provider_and_future_reuse(
    tmp_path,
    monkeypatch,
):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "claim_failure")
    inputs = load_benchmark_inputs("claim_failure", tmp_path)
    client = FakeClient()

    def fail_fsync(_descriptor):
        raise OSError("synthetic claim durability failure")

    monkeypatch.setattr(benchmark_novel.os, "fsync", fail_fsync)

    with pytest.raises(BenchmarkSafetyError, match="could not be durably written"):
        run_benchmark(inputs, [1], client)
    assert client.calls == []

    results_path = project.benchmark_dir / f"pilot/{RESULTS_FILENAME}"
    assert results_path.is_file()

    replacement = FakeClient()
    with pytest.raises(BenchmarkSafetyError, match="not overwritten"):
        run_benchmark(inputs, [1], replacement)
    assert replacement.calls == []


def test_api_client_is_mockable_and_main_never_needs_real_network(
    tmp_path, monkeypatch,
):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "cli_mock")
    inputs = load_benchmark_inputs("cli_mock", tmp_path)
    fake = FakeClient()
    monkeypatch.setattr(benchmark_novel, "load_novel_project", lambda _: project)
    monkeypatch.setattr(benchmark_novel, "load_benchmark_inputs", lambda _: inputs)
    monkeypatch.setattr(benchmark_novel, "create_benchmark_client", lambda _config: fake)
    monkeypatch.setattr(benchmark_novel, "utc_now", lambda: FIXED_PEAK_TIME)

    assert benchmark_novel.main(["--novel", "cli_mock", "--chapters", "1"]) == 0
    assert len(fake.calls) == 1


def test_benchmark_cli_holds_writer_lock_through_provider_transport(
    tmp_path,
    monkeypatch,
):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "cli_lock_span")
    inputs = load_benchmark_inputs("cli_lock_span", tmp_path)
    fake = FakeClient()
    successful_create = fake.create
    observed = []

    def locked_create(**kwargs):
        with pytest.raises(ProductionRunLockedError):
            with production_run_lock(project.root):
                pytest.fail("provider transport ran without benchmark writer lock")
        observed.append("provider_under_lock")
        return successful_create(**kwargs)

    fake.chat.completions.create = locked_create
    monkeypatch.setattr(benchmark_novel, "load_novel_project", lambda _: project)
    monkeypatch.setattr(benchmark_novel, "load_benchmark_inputs", lambda _: inputs)
    monkeypatch.setattr(benchmark_novel, "create_benchmark_client", lambda _config: fake)

    assert benchmark_novel.main([
        "--novel", "cli_lock_span",
        "--chapters", "1",
        "--output-dir", "lock-span",
    ]) == 0
    assert observed == ["provider_under_lock"]
    assert len(fake.calls) == 1


def test_benchmark_cli_rejects_contention_before_mutable_input_load(
    tmp_path,
    monkeypatch,
):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "cli_contention")
    fake = FakeClient()

    monkeypatch.setattr(benchmark_novel, "load_novel_project", lambda _: project)
    monkeypatch.setattr(
        benchmark_novel,
        "load_benchmark_inputs",
        lambda *_args, **_kwargs: pytest.fail(
            "benchmark loaded mutable inputs while another writer owned the lock"
        ),
    )
    monkeypatch.setattr(
        benchmark_novel,
        "create_benchmark_client",
        lambda _config: pytest.fail("benchmark initialized provider client under contention"),
    )

    with production_run_lock(project.root):
        assert benchmark_novel.main([
            "--novel", "cli_contention",
            "--chapters", "1",
        ]) == 1
    assert fake.calls == []


def test_benchmark_does_not_retry_ambiguous_provider_failure(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "retry_test")
    inputs = load_benchmark_inputs("retry_test", tmp_path)
    before = capture_protected_state(project)
    attempts = []

    class AmbiguousProviderError(RuntimeError):
        status_code = 503

    class FailingClient(FakeClient):
        def create(self, **kwargs):
            attempts.append(kwargs)
            raise AmbiguousProviderError("provider outcome is uncertain")

    client = FailingClient()
    client.chat.completions.create = client.create
    sleeps = []

    with pytest.raises(AmbiguousProviderError, match="outcome is uncertain"):
        run_benchmark(
            inputs,
            [1],
            client,
            now=lambda: FIXED_PEAK_TIME,
            timer=fixed_timer(),
            sleep=sleeps.append,
        )

    assert len(attempts) == 1
    assert sleeps == []
    assert capture_protected_state(project) == before
    assert not (project.benchmark_dir / "pilot/chapter_0001.txt").exists()

    results_path = project.benchmark_dir / f"pilot/{RESULTS_FILENAME}"
    saved = json.loads(results_path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["error"]["type"] == "AmbiguousProviderError"
    assert saved["chapters"] == []
    assert saved["safety"]["protected_state_verified_unchanged"] is True

    replacement = FakeClient()
    with pytest.raises(BenchmarkSafetyError, match="not overwritten"):
        run_benchmark(
            inputs,
            [1],
            replacement,
            now=lambda: FIXED_PEAK_TIME,
            timer=fixed_timer(),
            sleep=lambda _: pytest.fail("blocked rerun attempted retry delay"),
        )
    assert replacement.calls == []


def test_benchmark_client_disables_sdk_retries():
    source = (REPO_ROOT / "benchmark_novel.py").read_text(encoding="utf-8")
    client_block = source[
        source.index("def create_benchmark_client(config):"):
        source.index("def build_parser():")
    ]
    run_block = source[
        source.index("def run_benchmark("):
        source.index("def create_benchmark_client(config):")
    ]

    assert "max_retries=0" in client_block
    assert "run_with_retries" not in run_block
    assert "MAX_API_RETRIES" not in source
    assert "RETRY_DELAYS" not in source


def test_protected_state_change_is_detected_and_marks_benchmark_failed(tmp_path):
    prepare_repo(tmp_path)
    project = make_project(tmp_path, "guard_test")
    client = FakeClient()
    successful_create = client.create

    def mutating_create(**kwargs):
        project.progress_file.write_text('{"unexpected": true}', encoding="utf-8")
        return successful_create(**kwargs)

    client.chat.completions.create = mutating_create
    with pytest.raises(BenchmarkSafetyError, match="protected production state changed"):
        run_benchmark(
            load_benchmark_inputs("guard_test", tmp_path),
            [1],
            client,
            now=lambda: FIXED_PEAK_TIME,
            timer=fixed_timer(),
            sleep=lambda _: None,
        )

    saved = json.loads(
        (project.benchmark_dir / "pilot/results.json").read_text(encoding="utf-8")
    )
    assert saved["status"] == "failed"
    assert saved["error"]["type"] == "BenchmarkSafetyError"


def test_real_great_king_pilot_selection_is_read_only_and_valid():
    project = load_novel_project("great_king_zhuxia", REPO_ROOT)
    before = capture_protected_state(project)
    inputs = load_benchmark_inputs("great_king_zhuxia", REPO_ROOT)

    assert inputs.profile.id == "xianxia_xuanhuan"
    assert validate_chapter_selection(inputs, [1, 5, 118, 423, 1059]) == (
        1, 5, 118, 423, 1059,
    )
    assert "NOVEL-SPECIFIC TRANSLATION POLICY" in inputs.system_prompt
    assert len(inputs.glossary_entries) > 0
    assert capture_protected_state(project) == before
