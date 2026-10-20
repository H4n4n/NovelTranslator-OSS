import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import benchmark_flash_smoke as smoke
from benchmark_novel import BenchmarkSafetyError, capture_protected_state, load_benchmark_inputs
from benchmark_sample_plan import plan_sample, write_plan
from create_novel import create_novel_project
from novel_project import NovelConfig, load_novel_project


REPO_ROOT = Path(__file__).resolve().parents[1]
HEAD = "9" * 40


def _prepare_repo(tmp_path):
    shutil.copytree(REPO_ROOT / "profiles", tmp_path / "profiles")


def _chapter(number):
    text = (
        f"第{number}章 測試\n\n"
        + ("姚雲施展神通，眾人議論紛紛。" * (6 + number % 5))
        + "\n\n“你來了。”\n\n“是，我來了。”"
    )
    return {
        "chapter": number,
        "heading": f"第{number}章",
        "source_file": f"Text/{number:04d}.xhtml",
        "paragraph_count": 4,
        "character_count": len(text),
        "text": text,
    }


def _make_project(tmp_path):
    config = NovelConfig(
        id="flash_smoke_test",
        title="Flash Smoke Test",
        source_language="zh",
        target_language="en",
        profile="xianxia_xuanhuan",
        final_chapter=80,
    )
    create_novel_project(config, tmp_path)
    project = load_novel_project(config.id, tmp_path)
    project.chapters_file.write_text(
        json.dumps([_chapter(number) for number in range(1, 81)], ensure_ascii=False),
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
                    },
                    "神通": {
                        "english": "divine ability",
                        "category": "specialized recurring term",
                        "status": "provisional",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project


@pytest.fixture
def smoke_fixture(tmp_path, monkeypatch):
    _prepare_repo(tmp_path)
    project = _make_project(tmp_path)
    inputs = load_benchmark_inputs(project.config.id, tmp_path)
    plan = plan_sample(inputs, sample_size=30, strata=10)
    plan_path = write_plan(inputs, plan, "flash-smoke-test/sample-plan")
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    chapter = plan["selected_chapters"][2]
    output = tmp_path.with_name(tmp_path.name + "-external-smoke")
    monkeypatch.setattr(smoke, "_git_head", lambda _root: HEAD)
    return {
        "root": tmp_path,
        "project": project,
        "plan": plan,
        "plan_path": plan_path,
        "plan_sha": plan_sha,
        "chapter": chapter,
        "output": output,
    }


def _build(fixture, **overrides):
    values = {
        "repo_root": fixture["root"],
        "novel_id": fixture["project"].config.id,
        "chapter_number": fixture["chapter"],
        "plan_path": fixture["plan_path"],
        "expected_plan_sha256": fixture["plan_sha"],
        "output_dir": fixture["output"],
        "expected_repo_head": HEAD,
    }
    values.update(overrides)
    return smoke.build_smoke_plan(**values)


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.completions = FakeCompletions(response=response, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(content="Yao Yun displayed a divine ability."):
    return SimpleNamespace(
        id="resp-flash-smoke",
        model="deepseek-flash",
        system_fingerprint="fp-v41-test",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            prompt_cache_hit_tokens=40,
            prompt_cache_miss_tokens=60,
        ),
    )


def _journal_events(path):
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_zero_provider_plan_binds_frozen_pro_request_and_flash_model(smoke_fixture):
    before = capture_protected_state(smoke_fixture["project"])

    plan = _build(smoke_fixture)

    assert plan["config"].model == "deepseek-flash"
    assert plan["config"].version == "DeepSeek-V4.1-Flash"
    assert plan["config"].reasoning == "disabled"
    assert plan["config"].max_tokens == 12000
    assert plan["request"]["model"] == "deepseek-flash"
    assert plan["request"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert plan["pro_request_sha256"] == plan["row"]["deepseek_request_sha256"]
    assert len(plan["request_sha256"]) == 64
    assert len(plan["identity_sha256"]) == 64
    assert not smoke_fixture["output"].exists()
    assert capture_protected_state(smoke_fixture["project"]) == before


def test_plan_rejects_sample_plan_hash_drift(smoke_fixture):
    with pytest.raises(BenchmarkSafetyError, match="sample-plan SHA256 mismatch"):
        _build(smoke_fixture, expected_plan_sha256="0" * 64)


def test_plan_rejects_repository_head_drift(smoke_fixture, monkeypatch):
    monkeypatch.setattr(smoke, "_git_head", lambda _root: "8" * 40)
    with pytest.raises(BenchmarkSafetyError, match="repository HEAD drifted"):
        _build(smoke_fixture)


def test_plan_rejects_output_inside_repository(smoke_fixture):
    inside = smoke_fixture["root"] / "smoke-output"
    with pytest.raises(BenchmarkSafetyError, match="outside the repository"):
        _build(smoke_fixture, output_dir=inside)


def test_plan_rejects_preexisting_external_output(smoke_fixture):
    smoke_fixture["output"].mkdir()
    with pytest.raises(BenchmarkSafetyError, match="already exists"):
        _build(smoke_fixture)


def test_one_call_success_writes_append_only_evidence_and_no_production_state(
    smoke_fixture,
    monkeypatch,
):
    plan = _build(smoke_fixture)
    before = capture_protected_state(smoke_fixture["project"])
    client = FakeClient(response=_response())
    monkeypatch.setattr(smoke, "create_benchmark_client", lambda _config: client)

    result = smoke.execute_paid_smoke(plan)

    assert result["status"] == "complete"
    assert result["provider_response"]["response_model"] == "deepseek-flash"
    assert result["provider_response"]["system_fingerprint"] == "fp-v41-test"
    assert result["provider_response"]["finish_reason"] == "stop"
    assert result["safety"]["provider_calls_attempted"] == 1
    assert result["safety"]["paid_requests_attempted"] == 1
    assert result["safety"]["protected_state_verified_unchanged"] is True
    assert len(client.completions.calls) == 1
    assert client.completions.calls[0] == plan["request"]
    assert (smoke_fixture["output"] / "manifest.json").is_file()
    assert (smoke_fixture["output"] / "request.json").is_file()
    assert (smoke_fixture["output"] / "translation.txt").is_file()
    assert (smoke_fixture["output"] / "complete.json").is_file()
    assert not (smoke_fixture["output"] / "uncertain.json").exists()
    assert not (smoke_fixture["output"] / "response_failed.json").exists()
    assert _journal_events(smoke_fixture["output"] / "journal.jsonl") == [
        "planned",
        "started",
        "response_recorded",
        "complete",
    ]
    assert capture_protected_state(smoke_fixture["project"]) == before


def test_transport_exception_is_uncertain_and_never_auto_retried(
    smoke_fixture,
    monkeypatch,
):
    plan = _build(smoke_fixture)
    client = FakeClient(error=TimeoutError("simulated ambiguous timeout"))
    monkeypatch.setattr(smoke, "create_benchmark_client", lambda _config: client)

    with pytest.raises(smoke.FlashSmokeUncertainError, match="retry is forbidden"):
        smoke.execute_paid_smoke(plan)

    assert len(client.completions.calls) == 1
    uncertain = json.loads(
        (smoke_fixture["output"] / "uncertain.json").read_text(encoding="utf-8")
    )
    assert uncertain["status"] == "uncertain"
    assert uncertain["authorization"]["paid_requests_attempted"] == 1
    assert uncertain["authorization"]["retry_forbidden"] is True
    assert not (smoke_fixture["output"] / "translation.txt").exists()
    assert _journal_events(smoke_fixture["output"] / "journal.jsonl") == [
        "planned",
        "started",
        "uncertain",
    ]


def test_recorded_provider_response_failure_is_not_mislabeled_uncertain(
    smoke_fixture,
    monkeypatch,
):
    plan = _build(smoke_fixture)
    bad_response = _response()
    bad_response.choices = []
    client = FakeClient(response=bad_response)
    monkeypatch.setattr(smoke, "create_benchmark_client", lambda _config: client)

    with pytest.raises(smoke.FlashSmokeError, match="response.*retry is forbidden"):
        smoke.execute_paid_smoke(plan)

    assert len(client.completions.calls) == 1
    assert (smoke_fixture["output"] / "response_failed.json").is_file()
    assert not (smoke_fixture["output"] / "uncertain.json").exists()
    failed = json.loads(
        (smoke_fixture["output"] / "response_failed.json").read_text(encoding="utf-8")
    )
    assert failed["status"] == "response_failed"
    assert failed["safety"]["provider_response_recorded"] is True
    assert failed["safety"]["retry_forbidden"] is True
    assert _journal_events(smoke_fixture["output"] / "journal.jsonl") == [
        "planned",
        "started",
        "response_recorded",
        "response_failed",
    ]


def test_paid_confirmation_literal_is_explicit_and_stable():
    assert smoke.EXECUTE_CONFIRMATION == (
        "AUTHORIZE_ONE_DEEPSEEK_V41_FLASH_PAID_REQUEST"
    )
