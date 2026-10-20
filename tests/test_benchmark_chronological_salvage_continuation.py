import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_chronological_runtime as strict
import benchmark_chronological_salvage_continuation as cont
from benchmark_novel import BenchmarkSafetyError


def _state(chapter=5):
    return {
        "schema_version": 1,
        "policy_version": "chronological-state-v1",
        "benchmark": "chronological-state-growth-v1",
        "novel_id": "great_king_zhuxia",
        "source_language": "zh",
        "profile_id": "xianxia_xuanhuan",
        "through_chapter": chapter,
        "glossary_entries": {},
        "continuity_summary": "Prior continuity.",
        "issues": ["Prior issue."],
        "parent_state_sha256": None,
        "state_events": [],
    }


def _plan(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "chronological_state_plan.json").write_text("{}", encoding="utf-8")
    return {
        "schema_version": cont.SCHEMA_VERSION,
        "kind": cont.KIND,
        "status": "prepared",
        "run_id": "s" * 64,
        "repo_root": str(tmp_path / "repo"),
        "repo_head": "1" * 40,
        "strict_source": {
            "root": str(tmp_path / "strict"),
            "run_id": "r" * 64,
            "tree_sha256": "a" * 64,
        },
        "foundation": {
            "root": str(tmp_path / "foundation"),
            "run_id": "f" * 64,
            "tree_sha256": "b" * 64,
            "recoveries": {},
        },
        "canonical": {
            "root": str(canonical),
            "identity_sha256": "c" * 64,
            "plan_sha256": "d" * 64,
            "tree_sha256": "e" * 64,
        },
        "budgets_usd": {
            strict.FLASH_ARM: 0.75,
            strict.LUNA_ARM: 0.75,
        },
        "protected_state_before": {"stable": True},
        "authorization": {
            "maximum_new_paid_requests": {
                strict.FLASH_ARM: 40,
                strict.LUNA_ARM: 15,
            },
            "automatic_retry_forbidden": True,
            "batch_enabled": False,
        },
        "identity": {
            "model_configs": {
                strict.FLASH_ARM: {
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "version": "DeepSeek-V4.1-Flash",
                    "reasoning": "disabled",
                    "max_tokens": 12000,
                },
                strict.LUNA_ARM: {
                    "provider": "openai",
                    "model": "gpt-5.6-luna",
                    "version": "gpt-5.6-luna",
                    "reasoning": "none",
                    "max_tokens": 12000,
                },
            }
        },
    }


def _response(document, model="deepseek-flash", finish_reason="stop"):
    return {
        "id": "resp-salvage-test",
        "model": model,
        "system_fingerprint": "fp-test",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": json.dumps(document, ensure_ascii=False)},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }


class _Completions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error=None):
        self.completions = _Completions(response=response, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _patch_execute(monkeypatch, tmp_path, plan, state_before):
    monkeypatch.setattr(cont, "_load_plan", lambda root: plan)
    monkeypatch.setattr(
        cont,
        "_validate_static_sources",
        lambda root, value: (
            Path(value["strict_source"]["root"]),
            {},
            Path(value["canonical"]["root"]),
            {},
            {},
        ),
    )
    monkeypatch.setattr(
        cont,
        "_state_before_for_case",
        lambda root, value, arm, novel, chapter: (
            state_before,
            strict._canonical_json_sha256(state_before),
            "fixture-state-before.json",
        ),
    )
    config = SimpleNamespace(model="deepseek-flash")
    request = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "PROFILE + FROZEN PROTOCOL"},
            {"role": "user", "content": "STATE + SOURCE"},
        ],
        "max_tokens": 12000,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    monkeypatch.setattr(
        strict,
        "_build_request",
        lambda canonical_root, canonical_plan, arm, novel, chapter, state: (
            request,
            {"source_sha256": "9" * 64},
            config,
            {},
        ),
    )
    monkeypatch.setattr(cont, "_assert_request_budget", lambda *args: 0.5)
    monkeypatch.setattr(
        strict,
        "_ensure_protected_state",
        lambda repo, expected, stage: expected,
    )
    monkeypatch.setattr(strict, "_protected_state", lambda repo: {"stable": True})
    monkeypatch.setattr(
        strict,
        "_pricing_result",
        lambda config, started, usage, batch=False: {
            "total_cost_usd": 0.001,
            "pricing_mode": "test",
        },
    )
    monkeypatch.setattr(
        strict,
        "residual_source_script_report",
        lambda translation, language: {
            "remaining_cjk_character_count": 0,
            "residual_cjk_fragments": [],
        },
    )
    monkeypatch.setattr(
        strict,
        "_novel_record",
        lambda canonical_plan, novel_id: {"source_language": "zh"},
    )
    return request


def test_remaining_case_boundaries_are_exact():
    flash = cont._remaining_cases(strict.FLASH_ARM)
    luna = cont._remaining_cases(strict.LUNA_ARM)

    assert len(flash) == 40
    assert flash[0] == ("great_king_zhuxia", 6)
    assert flash[-1] == ("rogue_first_prince", 25)

    assert len(luna) == 15
    assert luna[0] == ("rogue_first_prince", 18)
    assert luna[-1] == ("rogue_first_prince", 25)


def test_parser_exposes_no_batch_command():
    parser = cont.build_parser()
    help_text = parser.format_help()
    assert "Batch" not in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["submit-batch"])


def test_wrong_authorization_fails_before_provider(monkeypatch, tmp_path):
    plan = _plan(tmp_path)
    monkeypatch.setattr(cont, "_load_plan", lambda root: plan)
    client = _Client(response=_response({"translation": "x"}))

    with pytest.raises(
        strict.ChronologicalRuntimeError,
        match="exact one-call salvage authorization token",
    ):
        cont.execute_next(
            tmp_path / "run",
            arm=strict.FLASH_ARM,
            authorization="WRONG",
            client_factory=lambda config: client,
        )

    assert client.completions.calls == 0


def test_execute_missing_state_update_is_noop_and_one_provider_call(monkeypatch, tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    plan = _plan(tmp_path)
    state_before = _state(5)
    request = _patch_execute(monkeypatch, tmp_path, plan, state_before)
    client = _Client(
        response=_response({"translation": "Complete English translation."})
    )
    ticks = iter([10.0, 11.25])
    now = lambda: datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)

    complete = cont.execute_next(
        root,
        arm=strict.FLASH_ARM,
        authorization=cont.FLASH_AUTHORIZATION,
        now=now,
        timer=lambda: next(ticks),
        client_factory=lambda config: client,
    )

    assert client.completions.calls == 1
    assert client.completions.requests == [request]
    assert complete["identity"]["case_id"] == "great_king_zhuxia-ch0006"
    assert [x["kind"] for x in complete["salvage_transformations"]] == [
        "missing_state_update_noop"
    ]
    assert complete["state_metrics"]["glossary_added"] == 0
    state_after = json.loads(
        (
            root
            / "continuation"
            / "arms"
            / strict.FLASH_ARM
            / "cases"
            / "great_king_zhuxia-ch0006"
            / "state_after.json"
        ).read_text(encoding="utf-8")
    )
    assert state_after["through_chapter"] == 6
    assert state_after["continuity_summary"] == "Prior continuity."
    assert state_after["issues"] == ["Prior issue."]


def test_execute_duplicate_source_first_wins(monkeypatch, tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    plan = _plan(tmp_path)
    state_before = _state(5)
    _patch_execute(monkeypatch, tmp_path, plan, state_before)
    document = {
        "translation": "Complete English translation.",
        "state_update": {
            "glossary_upserts": [
                {
                    "source": "甲",
                    "english": "First",
                    "category": "concept",
                    "confidence": "low",
                    "note": "first",
                },
                {
                    "source": "甲",
                    "english": "Second",
                    "category": "other",
                    "confidence": "high",
                    "note": "second",
                },
            ],
            "continuity_summary": "Updated.",
            "issues": [],
        },
    }
    client = _Client(response=_response(document))
    ticks = iter([1.0, 2.0])

    complete = cont.execute_next(
        root,
        arm=strict.FLASH_ARM,
        authorization=cont.FLASH_AUTHORIZATION,
        now=lambda: datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
        timer=lambda: next(ticks),
        client_factory=lambda config: client,
    )

    assert client.completions.calls == 1
    assert complete["salvage_transformations"][0]["kind"] == "duplicate_source_first_wins"
    assert complete["salvage_transformations"][0]["discarded_count"] == 1
    update = json.loads(
        (
            root
            / "continuation"
            / "arms"
            / strict.FLASH_ARM
            / "cases"
            / "great_king_zhuxia-ch0006"
            / "state_update.json"
        ).read_text(encoding="utf-8")
    )
    assert len(update["glossary_upserts"]) == 1
    assert update["glossary_upserts"][0]["english"] == "First"


def test_transport_error_is_uncertain_and_never_retried(monkeypatch, tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    plan = _plan(tmp_path)
    _patch_execute(monkeypatch, tmp_path, plan, _state(5))
    client = _Client(error=TimeoutError("ambiguous transport"))
    ticks = iter([3.0, 4.0])

    with pytest.raises(strict.ChronologicalUncertainError, match="retry is forbidden"):
        cont.execute_next(
            root,
            arm=strict.FLASH_ARM,
            authorization=cont.FLASH_AUTHORIZATION,
            now=lambda: datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
            timer=lambda: next(ticks),
            client_factory=lambda config: client,
        )

    assert client.completions.calls == 1
    case = (
        root
        / "continuation"
        / "arms"
        / strict.FLASH_ARM
        / "cases"
        / "great_king_zhuxia-ch0006"
    )
    assert (case / "uncertain.json").is_file()
    assert strict._journal_events(case / "journal.jsonl") == [
        "planned",
        "started",
        "uncertain",
    ]


def test_other_schema_error_remains_response_failed(monkeypatch, tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    plan = _plan(tmp_path)
    _patch_execute(monkeypatch, tmp_path, plan, _state(5))
    document = {
        "translation": "Complete English translation.",
        "state_update": {
            "glossary_upserts": [],
            "continuity_summary": "Updated.",
            "issues": "not-a-list",
        },
    }
    client = _Client(response=_response(document))
    ticks = iter([5.0, 6.0])

    with pytest.raises(cont.SalvageContinuationError, match="validation failed"):
        cont.execute_next(
            root,
            arm=strict.FLASH_ARM,
            authorization=cont.FLASH_AUTHORIZATION,
            now=lambda: datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
            timer=lambda: next(ticks),
            client_factory=lambda config: client,
        )

    assert client.completions.calls == 1
    case = (
        root
        / "continuation"
        / "arms"
        / strict.FLASH_ARM
        / "cases"
        / "great_king_zhuxia-ch0006"
    )
    failed = json.loads((case / "response_failed.json").read_text(encoding="utf-8"))
    assert failed["error"]["message"] == "issues must be a list"
    assert failed["safety"]["retry_forbidden"] is True


def test_budget_count_cap_uses_remaining_case_count():
    plan = {
        "budgets_usd": {strict.FLASH_ARM: 1.0},
        "authorization": {"maximum_new_paid_requests": {strict.FLASH_ARM: 40}},
    }
    status = {"paid_attempts": 40, "measured_cost_usd": 0.0}
    config = SimpleNamespace()
    with pytest.raises(BenchmarkSafetyError, match="paid-request count cap reached"):
        cont._assert_request_budget(
            plan,
            strict.FLASH_ARM,
            status,
            config,
            {"model": "deepseek-flash"},
        )
