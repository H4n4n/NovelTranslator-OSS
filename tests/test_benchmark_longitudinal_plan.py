import json

import pytest

import benchmark_longitudinal_plan as plan_module
from benchmark_longitudinal_config import DEFAULT_BENCHMARK_CONFIGURATION
from benchmark_longitudinal_plan import (
    BenchmarkPlanError,
    BenchmarkPlanValidationError,
    SourceIdentityDescriptor,
    assert_causal_sequential_step,
    build_benchmark_plan,
    build_default_plan,
    synthetic_source_identities,
    validate_benchmark_plan,
)


CONFIG = DEFAULT_BENCHMARK_CONFIGURATION
MODEL_IDS = CONFIG.model_ids
NOVEL_IDS = CONFIG.novel_ids


def test_plan_has_one_run_per_model_and_novel():
    plan = build_default_plan()
    assert plan.run_count == len(MODEL_IDS) * len(NOVEL_IDS) == 12
    for model_id in MODEL_IDS:
        assert len(plan.runs_for_model(model_id)) == len(NOVEL_IDS)
    for novel_id in NOVEL_IDS:
        assert len(plan.runs_for_novel(novel_id)) == len(MODEL_IDS)


def test_run_identities_are_independent_per_model_and_novel():
    plan = build_default_plan()
    run_hashes = {run.run_identity.run_sha256 for run in plan.runs}
    s0_hashes = {run.s0_state.identity_sha256 for run in plan.runs}
    assert len(run_hashes) == plan.run_count
    assert len(s0_hashes) == plan.run_count

    glm_ace = plan.run_for("z-ai/glm-5.3-flash", "ace_evolution")
    glm_rogue = plan.run_for("z-ai/glm-5.3-flash", "rogue_first_prince")
    mimo_ace = plan.run_for("xiaomi/mimo-v2.5-pro", "ace_evolution")
    assert glm_ace.run_identity.run_sha256 != glm_rogue.run_identity.run_sha256
    assert glm_ace.run_identity.run_sha256 != mimo_ace.run_identity.run_sha256
    assert glm_rogue.run_identity.run_sha256 != mimo_ace.run_identity.run_sha256


def test_stage1_targets_thirty_and_finalist_targets_sixty():
    for run in build_default_plan().runs:
        assert run.stage1_chapters == tuple(range(1, 31))
        assert run.finalist_continuation_chapters == tuple(range(31, 61))
        assert run.chapters == tuple(range(1, 61))


def test_expected_audit_checkpoints_are_25_and_50():
    for run in build_default_plan().runs:
        assert run.stage1_audit_checkpoints == (25,)
        assert run.finalist_audit_checkpoints == (50,)
        assert run.audit_checkpoints == (25, 50)


def test_run_invariants_are_recorded():
    for run in build_default_plan().runs:
        assert run.requires_s0_state is True
        assert run.causal_sequential is True
        assert run.same_model_pure_run_lifecycle is True
        assert run.independent_model_state is True
        assert run.s0_state.glossary_entries == 0
        assert run.s0_state.continuity == ""
        assert run.s0_state.last_completed_chapter == 0


def test_plan_identity_is_deterministic():
    first = build_default_plan()
    second = build_default_plan()
    assert first.plan_sha256 == second.plan_sha256
    assert len(first.plan_sha256) == 64
    assert first.configuration_sha256 == CONFIG.identity_sha256
    assert first.identity() == second.identity()
    assert first.benchmark_id == "goat-longitudinal-cjk-v1"
    for run in first.runs:
        assert run.run_identity.benchmark_id == "goat-longitudinal-cjk-v1"
        assert run.s0_state.benchmark_id == "goat-longitudinal-cjk-v1"


def test_validate_proves_and_rejects_drift():
    sources = synthetic_source_identities(CONFIG)
    plan = build_benchmark_plan(CONFIG, sources)
    assert validate_benchmark_plan(plan, CONFIG, sources) is plan

    drifted = build_benchmark_plan(
        CONFIG,
        tuple(
            SourceIdentityDescriptor(
                novel_id=descriptor.novel_id,
                source_language=descriptor.source_language,
                target_language=descriptor.target_language,
                chapter_count=descriptor.chapter_count + 1,
                source_sha256="",
            )
            for descriptor in sources
        ),
    )
    with pytest.raises(BenchmarkPlanValidationError, match="deterministic rebuild"):
        validate_benchmark_plan(drifted, CONFIG, sources)


def test_missing_or_unknown_source_descriptors_fail_closed():
    with pytest.raises(BenchmarkPlanError, match="missing configured novels"):
        build_benchmark_plan(CONFIG, synthetic_source_identities(CONFIG)[:1])
    with pytest.raises(BenchmarkPlanError, match="unknown novels"):
        build_benchmark_plan(
            CONFIG,
            synthetic_source_identities(CONFIG)
            + (
                SourceIdentityDescriptor(
                    novel_id="mystery_novel",
                    source_language="zh",
                    target_language="en",
                    chapter_count=60,
                ),
            ),
        )
    with pytest.raises(BenchmarkPlanError, match="below the finalist target"):
        build_benchmark_plan(CONFIG, synthetic_source_identities(CONFIG, chapter_count=30))


def test_source_language_must_match_configuration():
    sources = tuple(
        SourceIdentityDescriptor(
            novel_id=novel.novel_id,
            source_language="fr",
            target_language=novel.target_language,
            chapter_count=60,
        )
        for novel in CONFIG.novels
    )
    with pytest.raises(BenchmarkPlanError, match="does not match configured novel"):
        build_benchmark_plan(CONFIG, sources)


def test_target_language_must_match_configuration():
    sources = tuple(
        SourceIdentityDescriptor(
            novel_id=novel.novel_id,
            source_language=novel.source_language,
            target_language="de",
            chapter_count=60,
        )
        for novel in CONFIG.novels
    )
    with pytest.raises(BenchmarkPlanError, match="target language"):
        build_benchmark_plan(CONFIG, sources)


def test_causal_sequential_step_is_enforced():
    assert assert_causal_sequential_step(0, 1) == 1
    assert assert_causal_sequential_step(29, 30) == 30
    with pytest.raises(BenchmarkPlanError, match="causally sequential"):
        assert_causal_sequential_step(29, 31)


def test_reasoning_candidate_is_not_bound_while_unverified():
    # Planning records the model/novel run without inventing a reasoning level.
    run = build_default_plan().run_for("z-ai/glm-5.3-flash", "ace_evolution")
    assert run.same_model_pure_run_lifecycle is True
    assert "reasoning_level" not in run.identity()


def test_planning_makes_no_provider_call(monkeypatch):
    import benchmark_longitudinal_provider as provider

    def forbidden(*args, **kwargs):
        raise AssertionError("planning must not call the provider")

    monkeypatch.setattr(provider, "create_provider_client", forbidden)
    monkeypatch.setattr(provider, "send_chat_completion_once", forbidden)
    monkeypatch.setattr(provider, "perform_single_provider_request", forbidden)

    plan = build_default_plan()
    assert plan.run_count == 12


def test_planning_writes_no_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    build_default_plan()
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before
    assert after == []


def test_planning_does_not_read_real_source_files():
    # Synthetic descriptors carry everything planning needs; no path is opened.
    plan = build_default_plan()
    for run in plan.runs:
        assert isinstance(run.source_identity.source_sha256, str)
        assert run.source_identity.label == "synthetic"


def test_plan_identity_is_json_serializable():
    rendered = json.dumps(build_default_plan().identity(), sort_keys=True)
    assert "z-ai/glm-5.3-flash" in rendered
    assert plan_module.PLAN_REVISION == "goat-longitudinal-cjk-plan-v1"
