import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g7c_stage1_integration as g7c
from benchmark_longitudinal_capability_results import live_provider_capability
from benchmark_longitudinal_config import PURE_RUN_LIFECYCLE_OPERATIONS
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)


def _legacy_request(model_id, text):
    provider_model = live_provider_capability(model_id).provider_model_id
    return {
        "model": provider_model,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": text},
        ],
        "max_tokens": 128,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _raw_response(model, text="ok"):
    return SimpleNamespace(
        id="resp_fixture",
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=5,
            total_tokens=25,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        ),
    )


def _fake_module(tmp_path, model_id):
    module = SimpleNamespace()
    module._RUNTIME_INITIALIZED = True
    module.ACTIVE_NOVEL_CONFIG = SimpleNamespace(id="fixture_novel")
    module.ACTIVE_PROFILE = SimpleNamespace(id="fixture_profile")
    module.PAID_REQUEST_JOURNAL_DIR = tmp_path / "journals"
    module.PAID_REQUEST_JOURNAL_DIR.mkdir()
    module.request_identity = request_identity
    module.paid_request_journal_path = paid_request_journal_path
    module.run_journaled_paid_request = run_journaled_paid_request
    module.atomic_write_json = atomic_write_json
    module.MODEL = live_provider_capability(model_id).provider_model_id
    module.client = None
    module.api_key = None
    module.AUTO_OFF_PEAK_ONLY = False

    module.journaled_chat_request = lambda *a, **k: None
    module.call_api_with_retries = lambda **k: None
    module.send_api_request_once = lambda **k: None
    module.usage_and_cost = lambda usage: {}
    module.usage_and_cost_for_pricing = lambda usage, pricing: {}
    module.reprice_saved_stats_if_needed = lambda stats: stats
    module.pricing_snapshot = lambda moment=None: {}

    def review_new_terms(chapter_number, *args, **kwargs):
        return module.call_api_with_retries(
            **_legacy_request(model_id, f"glossary {chapter_number}")
        )

    def candidate_resolution(chapter_number, *args, **kwargs):
        return module.send_api_request_once(
            **_legacy_request(model_id, f"candidate {chapter_number}")
        )

    def run_periodic_audit(start_chapter, end_chapter, *args, **kwargs):
        return module.call_api_with_retries(
            **_legacy_request(model_id, f"audit {start_chapter}-{end_chapter}")
        )

    def strict_repair(chapter_number, *args, **kwargs):
        return module.call_api_with_retries(
            **_legacy_request(model_id, f"term {chapter_number}")
        )

    def residual_repair(chapter_number, *args, **kwargs):
        return module.call_api_with_retries(
            **_legacy_request(model_id, f"residual {chapter_number}")
        )

    module.review_new_terms = review_new_terms
    module.attempt_automatic_glossary_candidate_resolution = candidate_resolution
    module.run_periodic_audit = run_periodic_audit
    module.maybe_repair_strict_terminology = strict_repair
    module.maybe_repair_residual_cjk = residual_repair
    return module


def test_g7c_identity_closes_g7b_integration_blockers_but_keeps_paid_blocked():
    identity = g7c.g7c_integration_identity()

    assert g7c.G7C_INTEGRATION_SHA256 == g7c._sha256_json(identity)
    assert identity["stage1"]["run_count"] == 12
    assert identity["stage1"]["provider_calls_authorized"] is False
    assert identity["provider_calls"] == 0
    assert tuple(identity["resolved_by_g7c"]) == (
        g7c.RESOLVED_G7B_INTEGRATION_BLOCKERS
    )
    assert tuple(identity["execution_gate"]["required_before_paid_stage1"]) == (
        g7c.PAID_STAGE1_REMAINING_BLOCKERS
    )
    assert identity["execution_gate"]["status"] == "blocked"
    assert identity["runtime_contract"]["real_provider_transport_present"] is False
    assert identity["runtime_contract"]["proof_runtime_transport"] == "synthetic_only"


def test_twelve_external_run_specs_are_unique_and_preserve_s0_identity():
    specs = g7c.stage1_run_specs()
    assert len(specs) == 12
    assert len({row["relative_root"] for row in specs}) == 12
    assert len({(row["model_id"], row["novel_id"]) for row in specs}) == 12
    assert all(len(row["s0_identity_sha256"]) == 64 for row in specs)
    assert all(row["stage1_chapters"] == list(range(1, 31)) for row in specs)
    assert all(row["stage1_audit_checkpoints"] == [25] for row in specs)


@pytest.mark.parametrize(
    ("model_id", "operation", "expected_effort"),
    [
        ("deepseek/deepseek-v4.1-flash", "translation", "low"),
        ("z-ai/glm-5.3-flash", "glossary_review", "high"),
        ("qwen/qwen3.8-max-0902", "periodic_audit", "medium"),
        ("google/gemini-3.8-flash", "state_extraction", "low"),
    ],
)
def test_normalize_legacy_request_applies_exact_g7b_effort(
    model_id,
    operation,
    expected_effort,
):
    request = g7c.normalize_legacy_request(
        model_id=model_id,
        logical_operation=operation,
        request_kwargs=_legacy_request(model_id, operation),
    )
    assert request["model"] == live_provider_capability(model_id).provider_model_id
    assert request["reasoning_effort"] == expected_effort
    assert "extra_body" not in request
    assert request["max_completion_tokens"] == 128


@pytest.mark.parametrize(
    "model_id",
    ["tencent/hy3-paid", "xiaomi/mimo-v2.5-pro"],
)
def test_default_only_models_never_invent_reasoning(model_id):
    request = g7c.normalize_legacy_request(
        model_id=model_id,
        logical_operation="translation",
        request_kwargs=_legacy_request(model_id, "default"),
    )
    assert "reasoning_effort" not in request


def test_unexpected_legacy_reasoning_or_provider_shape_fails_closed():
    model_id = "deepseek/deepseek-v4.1-flash"
    request = _legacy_request(model_id, "bad")
    request["reasoning_effort"] = "high"
    with pytest.raises(g7c.G7CIntegrationError, match="must not pre-populate"):
        g7c.normalize_legacy_request(
            model_id=model_id,
            logical_operation="translation",
            request_kwargs=request,
        )

    request = _legacy_request(model_id, "bad")
    request["extra_body"] = {"thinking": {"type": "enabled"}}
    with pytest.raises(g7c.G7CIntegrationError, match="extra_body"):
        g7c.normalize_legacy_request(
            model_id=model_id,
            logical_operation="translation",
            request_kwargs=request,
        )


def test_direct_journal_replays_identical_request_without_second_send(tmp_path):
    model_id = "deepseek/deepseek-v4.1-flash"
    module = _fake_module(tmp_path, model_id)
    calls = []

    def send_once(request):
        calls.append(copy.deepcopy(dict(request)))
        return _raw_response(request["model"], "translated")

    runtime = g7c.install_stage1_synthetic_proof_runtime(
        module,
        model_id=model_id,
        synthetic_send_once=send_once,
        usage_to_stats=g7c.build_proof_stats,
        validate_saved_stats=lambda stats: stats,
    )
    assert runtime["real_provider_transport_present"] is False
    assert runtime["provider_calls_authorized"] is False

    kwargs = _legacy_request(model_id, "chapter one")
    first = module.journaled_chat_request(
        "chapter_translation",
        1,
        "source",
        kwargs,
    )
    second = module.journaled_chat_request(
        "chapter_translation",
        1,
        "source",
        kwargs,
    )

    assert len(calls) == 1
    assert first["replayed"] is False
    assert second["replayed"] is True
    journals = list(module.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))
    assert len(journals) == 1
    record = json.loads(journals[0].read_text(encoding="utf-8"))
    assert record["status"] == "response_recorded"
    assert record["provider"] == "command_code_provider"


def test_changed_direct_request_cannot_create_second_paid_slot(tmp_path):
    model_id = "deepseek/deepseek-v4.1-flash"
    module = _fake_module(tmp_path, model_id)
    calls = []

    def send_once(request):
        calls.append(copy.deepcopy(dict(request)))
        return _raw_response(request["model"], "translated")

    g7c.install_stage1_synthetic_proof_runtime(
        module,
        model_id=model_id,
        synthetic_send_once=send_once,
        usage_to_stats=g7c.build_proof_stats,
        validate_saved_stats=lambda stats: stats,
    )

    module.journaled_chat_request(
        "chapter_translation",
        1,
        "source",
        _legacy_request(model_id, "first payload"),
    )
    assert len(calls) == 1

    with pytest.raises(Exception, match="identity"):
        module.journaled_chat_request(
            "chapter_translation",
            1,
            "source",
            _legacy_request(model_id, "changed payload"),
        )

    assert len(calls) == 1
    journals = list(module.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))
    assert len(journals) == 1
    assert journals[0].name == "0001.g7c_translation.json"


def test_all_six_logical_operations_route_through_durable_journals(tmp_path):
    model_id = "z-ai/glm-5.3-flash"
    module = _fake_module(tmp_path, model_id)
    calls = []

    def send_once(request):
        calls.append(copy.deepcopy(dict(request)))
        return _raw_response(request["model"])

    g7c.install_stage1_synthetic_proof_runtime(
        module,
        model_id=model_id,
        synthetic_send_once=send_once,
        usage_to_stats=g7c.build_proof_stats,
        validate_saved_stats=lambda stats: stats,
    )

    module.journaled_chat_request(
        "chapter_translation",
        1,
        "source",
        _legacy_request(model_id, "translation"),
    )
    module.journaled_chat_request(
        "state_extraction",
        1,
        "source",
        _legacy_request(model_id, "state"),
        translation_text="translation",
    )
    module.journaled_chat_request(
        "state_json_repair",
        1,
        "source",
        _legacy_request(model_id, "state repair"),
        translation_text="{bad",
    )
    module.review_new_terms(1)
    module.run_periodic_audit(1, 25)
    module.maybe_repair_strict_terminology(1)

    assert len(calls) == 6
    journal_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")
    ]
    assert {record["operation_kind"] for record in journal_records} == set(
        PURE_RUN_LIFECYCLE_OPERATIONS
    )
    assert all(record["status"] == "response_recorded" for record in journal_records)


def test_auxiliary_provider_shaped_call_without_context_fails_closed(tmp_path):
    model_id = "deepseek/deepseek-v4.1-flash"
    module = _fake_module(tmp_path, model_id)

    g7c.install_stage1_synthetic_proof_runtime(
        module,
        model_id=model_id,
        synthetic_send_once=lambda request: _raw_response(request["model"]),
        usage_to_stats=g7c.build_proof_stats,
        validate_saved_stats=lambda stats: stats,
    )

    with pytest.raises(g7c.G7CIntegrationError, match="outside an explicit"):
        module.call_api_with_retries(**_legacy_request(model_id, "unclassified"))

    assert not list(module.PAID_REQUEST_JOURNAL_DIR.glob("*.json"))


def test_candidate_conflict_direct_send_is_classified_as_glossary_review(tmp_path):
    model_id = "deepseek/deepseek-v4.1-flash"
    module = _fake_module(tmp_path, model_id)
    calls = []

    g7c.install_stage1_synthetic_proof_runtime(
        module,
        model_id=model_id,
        synthetic_send_once=lambda request: (
            calls.append(copy.deepcopy(dict(request)))
            or _raw_response(request["model"])
        ),
        usage_to_stats=g7c.build_proof_stats,
        validate_saved_stats=lambda stats: stats,
    )

    module.attempt_automatic_glossary_candidate_resolution(7)
    assert len(calls) == 1
    names = [p.name for p in module.PAID_REQUEST_JOURNAL_DIR.glob("*.json")]
    assert len(names) == 1
    assert "g7c_glossary_review_" in names[0]


def test_synthetic_stats_are_explicitly_non_authoritative_for_paid_execution():
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=3,
        cached_tokens=2,
    )
    stats = g7c.build_proof_stats("model", "translation", usage)
    assert stats["pricing_version"] == g7c.PROOF_ACCOUNTING_VERSION
    assert stats["cost_usd"] == 0.0
    identity = g7c.g7c_integration_identity()
    assert "freeze_command_code_provider_accounting_policy" in (
        identity["execution_gate"]["required_before_paid_stage1"]
    )
    assert identity["stage1"]["provider_calls_authorized"] is False

def _write_fake_production_project(root, novel_id):
    project = root / "novels" / novel_id
    (project / "source").mkdir(parents=True)
    (project / "data").mkdir()
    (project / "output").mkdir()

    config = {
        "id": novel_id,
        "title": novel_id,
        "source_language": "zh",
        "target_language": "en",
        "profile": "fixture_profile",
        "final_chapter": 30,
        "audit_interval": 25,
        "chapter_numbering": "global",
        "source_type": "txt",
    }
    (project / "novel.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "translation_addendum.txt").write_text(
        "fixture addendum\n",
        encoding="utf-8",
    )
    source = project / "source" / "novel.txt"
    source.write_text("fixture source " + novel_id, encoding="utf-8")
    chapters = [
        {"chapter": index, "text": f"source {index}"}
        for index in range(1, 31)
    ]
    chapter_file = project / "data" / "chapters.json"
    chapter_file.write_text(
        json.dumps(chapters, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "project": project,
        "source_sha256": g7c._sha256_file(source),
        "chapter_library_sha256": g7c._sha256_file(chapter_file),
    }


def test_external_s0_root_creation_is_atomic_nonoverwriting_and_source_guarded(
    tmp_path,
    monkeypatch,
):
    production = tmp_path / "production"
    production.mkdir()
    a = _write_fake_production_project(production, "novel_a")
    b = _write_fake_production_project(production, "novel_b")

    fake_bindings = {
        "novel_a": {
            "profile": "fixture_profile",
            "source_language": "zh",
            "target_language": "en",
            "chapter_numbering": "global",
            "audit_interval": 25,
            "source_sha256": a["source_sha256"],
            "chapter_library_sha256": a["chapter_library_sha256"],
        },
        "novel_b": {
            "profile": "fixture_profile",
            "source_language": "zh",
            "target_language": "en",
            "chapter_numbering": "global",
            "audit_interval": 25,
            "source_sha256": b["source_sha256"],
            "chapter_library_sha256": b["chapter_library_sha256"],
        },
    }
    monkeypatch.setattr(g7c, "SOURCE_BINDINGS", fake_bindings)

    specs = []
    index = 0
    for model_id in g7c.BENCHMARK_MODEL_IDS:
        for novel_id in ("novel_a", "novel_b"):
            index += 1
            binding = fake_bindings[novel_id]
            specs.append(
                {
                    "run_index": index,
                    "model_id": model_id,
                    "novel_id": novel_id,
                    "run_sha256": f"{index:064x}"[-64:],
                    "run_plan_sha256": f"{index + 100:064x}"[-64:],
                    "s0_identity_sha256": f"{index + 200:064x}"[-64:],
                    "source_sha256": binding["source_sha256"],
                    "chapter_library_sha256": binding["chapter_library_sha256"],
                    "profile": "fixture_profile",
                    "audit_interval": 25,
                    "chapter_numbering": "global",
                    "stage1_chapters": list(range(1, 31)),
                    "stage1_audit_checkpoints": [25],
                    "relative_root": (
                        f"runs/{index:02d}_{model_id.replace('/', '_')}__{novel_id}"
                    ),
                }
            )
    monkeypatch.setattr(g7c, "stage1_run_specs", lambda: tuple(specs))

    output = tmp_path / "external" / "G7C"
    prepared = g7c.prepare_external_s0_roots(
        production_root=production,
        output_root=output,
    )
    assert prepared["run_count"] == 12
    assert prepared["production_guard_verified"] is True
    assert prepared["provider_calls"] == 0
    assert prepared["paid_stage1_authorized"] is False
    assert len(list((output / "runs").iterdir())) == 12

    for spec in specs:
        root = output / Path(spec["relative_root"])
        assert json.loads(
            (root / "data" / "glossary.json").read_text(encoding="utf-8")
        ) == {"entries": {}}
        assert not list((root / "data" / "provider_request_journal").iterdir())
        assert not list((root / "output" / "chapters").iterdir())

    with pytest.raises(g7c.G7CIntegrationError, match="never overwritten"):
        g7c.prepare_external_s0_roots(
            production_root=production,
            output_root=output,
        )

    # Source files are deliberately outside the shared mutable-tree guard, so
    # G7C independently revalidates their frozen SHA on every verification.
    (a["project"] / "source" / "novel.txt").write_text(
        "mutated source",
        encoding="utf-8",
    )
    with pytest.raises(g7c.G7CIntegrationError, match="source SHA changed"):
        g7c.verify_external_s0_roots(
            production_root=production,
            output_root=output,
        )

