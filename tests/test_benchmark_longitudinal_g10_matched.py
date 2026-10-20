from pathlib import Path
import inspect
import hashlib
import subprocess
import json

import pytest

import benchmark_longitudinal_g10_matched as matched
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    NOVELS,
    QWEN_MODEL_ID,
)


def _fake_prompt(novel_id, chapter):
    system = f"system::{novel_id}"
    user = f"user::{novel_id}::{chapter}"
    messages = (
        ("system", system),
        ("user", user),
    )
    message_dicts = [
        {"role": role, "content": content}
        for role, content in messages
    ]
    return matched.G10PromptBundle(
        novel_id=novel_id,
        chapter=chapter,
        source_language="zh" if novel_id == "ace_evolution" else "ko",
        target_language="en",
        profile_id="infinite_flow" if novel_id == "ace_evolution" else "korean_fantasy",
        config_sha256="1" * 64,
        source_sha256="2" * 64,
        chapter_library_sha256="3" * 64,
        addendum_sha256="4" * 64,
        text_sha256=matched._sha256_text(user),
        messages=messages,
        system_prompt_sha256=matched._sha256_text(system),
        user_prompt_sha256=matched._sha256_text(user),
        messages_sha256=matched._sha256_json(message_dicts),
    )


def _prompts():
    result = {}
    for pair in matched.PAIR_SPECS:
        result[(pair.novel_id, pair.chapter)] = _fake_prompt(
            pair.novel_id,
            pair.chapter,
        )
    return result


def test_contract_is_zero_provider_and_paid_execution_is_blocked():
    matched.assert_paid_execution_blocked()
    identity = matched.g10a2_contract_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["paid_execution_entrypoint_present"] is False
    assert identity["max_completion_tokens"] == 24_000
    assert identity["request_count"] == 102


def test_pair_roster_is_48_fresh_primary_plus_three_bridge():
    pairs = matched.PAIR_SPECS
    assert len(pairs) == 51
    primary = [pair for pair in pairs if pair.case_kind == "primary"]
    bridge = [pair for pair in pairs if pair.case_kind == "bridge"]
    assert len(primary) == 48
    assert len(bridge) == 3
    assert {(pair.novel_id, pair.chapter) for pair in bridge} == {
        ("ace_evolution", 1),
        ("ace_evolution", 2),
        ("rogue_first_prince", 1),
    }
    assert all(pair.chapter not in {1, 2} for pair in primary)


def test_primary_execution_order_balances_first_model_exactly():
    primary = [pair for pair in matched.PAIR_SPECS if pair.case_kind == "primary"]
    qwen_first = sum(
        pair.first_model_id == QWEN_MODEL_ID
        for pair in primary
    )
    glm_first = sum(
        pair.first_model_id == GLM_MODEL_ID
        for pair in primary
    )
    assert qwen_first == 24
    assert glm_first == 24
    for pair in matched.PAIR_SPECS:
        expected = QWEN_MODEL_ID if pair.pair_index % 2 else GLM_MODEL_ID
        assert pair.first_model_id == expected


def test_provider_wire_model_identity_is_separate_and_case_exact():
    identity = matched.g10a2_contract_identity()
    assert identity["provider_model_ids"] == {
        QWEN_MODEL_ID: "Qwen/Qwen3.8-Max-0902",
        GLM_MODEL_ID: "z-ai/glm-5.3-flash",
    }
    assert identity["translation_efforts"] == {
        QWEN_MODEL_ID: "medium",
        GLM_MODEL_ID: "high",
    }


def test_runtime_builds_102_requests_with_identical_pair_prompts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    cases = matched.build_runtime_cases(
        repo_root=repo,
        evidence_root=evidence,
        prompt_bundles=_prompts(),
    )
    assert len(cases) == 102
    assert [case.execution_index for case in cases] == list(range(1, 103))

    by_pair = {}
    for case in cases:
        by_pair.setdefault(case.pair.pair_id, []).append(case)
        assert case.request_body["max_completion_tokens"] == 24_000
        assert (
            case.provider_request_sha256
            == case.journal_identity["request_sha256"]
        )
        assert case.journal_identity["chapter"] == case.pair.chapter
        assert case.journal_path.name.startswith(
            f"{case.pair.chapter:04d}."
        )
        assert case.journal_path.parent.name in {"qwen", "glm"}
        assert case.journal_path.parent.parent.name == case.pair.novel_id

    assert len(by_pair) == 51
    for pair_cases in by_pair.values():
        assert len(pair_cases) == 2
        assert {case.benchmark_model_id for case in pair_cases} == {
            QWEN_MODEL_ID,
            GLM_MODEL_ID,
        }
        assert len({case.prompt.messages_sha256 for case in pair_cases}) == 1


def test_qwen_and_glm_requests_use_frozen_effort_and_wire_models(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cases = matched.build_runtime_cases(
        repo_root=repo,
        evidence_root=tmp_path / "evidence",
        prompt_bundles=_prompts(),
    )
    qwen = next(case for case in cases if case.benchmark_model_id == QWEN_MODEL_ID)
    glm = next(case for case in cases if case.benchmark_model_id == GLM_MODEL_ID)

    assert qwen.provider_model_id == "Qwen/Qwen3.8-Max-0902"
    assert qwen.request_body["model"] == "Qwen/Qwen3.8-Max-0902"
    assert qwen.request_body["reasoning_effort"] == "medium"
    assert qwen.effort == "medium"

    assert glm.provider_model_id == "z-ai/glm-5.3-flash"
    assert glm.request_body["model"] == "z-ai/glm-5.3-flash"
    assert glm.request_body["reasoning_effort"] == "high"
    assert glm.effort == "high"


def test_evidence_root_must_not_overlap_repository_or_frozen_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    frozen = tmp_path / "GOAT"
    frozen.mkdir()

    with pytest.raises(Exception, match="overlaps"):
        matched.assert_matched_evidence_root(
            repo / "evidence",
            repo_root=repo,
        )

    with pytest.raises(Exception, match="overlaps"):
        matched.assert_matched_evidence_root(
            frozen / "G10",
            repo_root=repo,
            forbidden_roots=(frozen,),
        )

    clean = matched.assert_matched_evidence_root(
        tmp_path / "G10A2",
        repo_root=repo,
        forbidden_roots=(frozen,),
    )
    assert clean == (tmp_path / "G10A2").resolve()


def test_plan_identity_stays_zero_provider_and_binds_all_cases(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    identity = matched.matched_plan_identity(
        repo_root=repo,
        evidence_root=tmp_path / "evidence",
        prompt_bundles=_prompts(),
    )
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["pair_count"] == 51
    assert identity["primary_pair_count"] == 48
    assert identity["bridge_pair_count"] == 3
    assert identity["request_count"] == 102
    assert len(identity["cases"]) == 102
    assert len(matched.matched_plan_sha256(
        repo_root=repo,
        evidence_root=tmp_path / "evidence",
        prompt_bundles=_prompts(),
    )) == 64


def test_real_prompt_loader_uses_current_novelproject_path_api():
    source = inspect.getsource(matched.load_prompt_bundle)
    assert "project.config_file" in source
    assert "project.config_path" not in source
    assert "project.translation_addendum_file" in source
    assert "project.chapters_file" in source


def test_frozen_novel_configs_match_canonical_json_across_line_endings():
    repo = Path(__file__).resolve().parents[1]
    for novel_id, spec in NOVELS.items():
        # The preregistration binds historical Git objects, not mutable current
        # production configs. CI retains history so these exact objects exist.
        blob = subprocess.run(
            ["git", "cat-file", "blob", spec["config_blob_sha"]],
            cwd=repo, check=True, capture_output=True,
        ).stdout
        header = f"blob {len(blob)}\0".encode("ascii")
        assert hashlib.sha1(header + blob).hexdigest() == spec["config_blob_sha"]
        text = blob.decode("utf-8")
        for serialized in (text.replace("\r\n", "\n"), text.replace("\r\n", "\n").replace("\n", "\r\n")):
            assert matched._sha256_json(json.loads(serialized)) == spec["config_canonical_sha256"]


@pytest.mark.parametrize("novel_id", NOVELS)
def test_registered_historical_config_reconstruction(novel_id):
    repo = Path(__file__).resolve().parents[1]
    spec = NOVELS[novel_id]
    blob = subprocess.run(["git", "cat-file", "blob", spec["config_blob_sha"]],
                          cwd=repo, check=True, capture_output=True).stdout
    config, raw_sha = matched.load_historical_config(novel_id, repo)
    assert hashlib.sha1(f"blob {len(blob)}\0".encode("ascii") + blob).hexdigest() == spec["config_blob_sha"]
    assert matched._sha256_json(config) == spec["config_canonical_sha256"]
    assert raw_sha == hashlib.sha256(blob).hexdigest()
    assert matched.load_historical_config(novel_id, repo) == (config, raw_sha)


def test_live_ace_addition_preserves_historical_bundle_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace
    repo = Path(__file__).resolve().parents[1]
    project = matched.load_novel_project("ace_evolution", repo)
    live = json.loads(project.config_file.read_text(encoding="utf-8"))
    assert live["residual_cjk_micro_repair"] == {
        "enabled": True, "max_total_cjk_chars": 12, "max_fragments": 3,
        "max_affected_lines": 3, "max_attempts_per_pending_version": 1}
    # Only the ignored source-book carrier is synthetic. Use actual current
    # config, tracked chapter library, addendum and profile for the prompt seam.
    source = tmp_path / "source.epub"
    source.write_bytes(b"synthetic source carrier")
    view = SimpleNamespace(config=project.config, config_file=project.config_file,
        source_epub=source, source_txt=source, chapters_file=project.chapters_file,
        translation_addendum_file=project.translation_addendum_file, repository_root=repo)
    monkeypatch.setattr(matched, "load_novel_project", lambda *args: view)
    bundle = matched.load_prompt_bundle("ace_evolution", 3, repo)
    assert bundle.config_sha256 == "022b8f1828d74f147ead916f6c0d86fd978cd948912b82fa1218c47d536915a4"
    assert bundle.config_sha256 != hashlib.sha256(project.config_file.read_bytes()).hexdigest()
    assert bundle.public_identity()["config_canonical_sha256"] == (
        "82e7631ce2dc6d8c99419bfc047d0788cc504815a3a5112828f32abe665b4cd5")


@pytest.mark.parametrize("field,value", [("profile", "historical_military"),
    ("source_language", "ko"), ("chapter_numbering", "source_numbered")])
def test_live_prompt_relevant_config_drift_still_blocks(field, value, monkeypatch):
    from dataclasses import replace
    repo = Path(__file__).resolve().parents[1]
    project = matched.load_novel_project("ace_evolution", repo)
    altered = replace(project, config=replace(project.config, **{field: value}))
    monkeypatch.setattr(matched, "load_novel_project", lambda *args: altered)
    with pytest.raises(matched.G10MatchedPlanError, match=f"configuration drifted for {field}"):
        matched.load_prompt_bundle("ace_evolution", 3, repo)


@pytest.mark.parametrize("failure", ["missing", "git_failure", "identity", "utf8", "json", "non_object", "canonical"])
def test_historical_config_failures_never_fall_back_to_live_config(monkeypatch, failure):
    from types import SimpleNamespace
    repo = Path(__file__).resolve().parents[1]
    spec = dict(NOVELS["ace_evolution"])
    if failure in {"missing", "git_failure"}:
        def run(*args, **kwargs):
            assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
            raise FileNotFoundError("git unavailable") if failure == "missing" else subprocess.CalledProcessError(128, args[0])
        monkeypatch.setattr(matched.subprocess, "run", run)
        expected_error = "Git blob cannot be loaded"
    else:
        blob = {"identity": b"{}", "utf8": b"\xff", "json": b"{", "non_object": b"[]", "canonical": b"{}"}[failure]
        if failure != "identity":
            spec["config_blob_sha"] = hashlib.sha1(f"blob {len(blob)}\0".encode("ascii") + blob).hexdigest()
        monkeypatch.setitem(matched.NOVELS, "ace_evolution", spec)
        monkeypatch.setattr(matched.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=blob))
        expected_error = {"identity": "Git blob identity mismatch", "utf8": "valid UTF-8 JSON",
            "json": "valid UTF-8 JSON", "non_object": "JSON object", "canonical": "canonical SHA mismatch"}[failure]
    with pytest.raises(matched.G10MatchedPlanError, match=expected_error):
        matched.load_prompt_bundle("ace_evolution", 3, repo)



def test_current_config_historical_identity_seam_in_all_matched_cases(tmp_path, monkeypatch):
    """Non-skipping CI seam: real configs/profiles, synthetic ignored source.

    Full frozen-plan proof requires local copyrighted source carriers/libraries;
    this fixture does not claim synthetic prompts reproduce their frozen hash.
    """
    from types import SimpleNamespace
    repo = Path(__file__).resolve().parents[1]
    projects = {}
    for novel_id in NOVELS:
        project = matched.load_novel_project(novel_id, repo)
        carrier = tmp_path / (novel_id + ".source")
        carrier.write_bytes(b"synthetic source carrier")
        library = tmp_path / (novel_id + ".chapters.json")
        library.write_text(json.dumps([
            {"chapter": pair.chapter, "heading": f"Chapter {pair.chapter} Synthetic",
             "local_chapter_start": pair.chapter, "local_chapter_end": pair.chapter,
             "text": "Synthetic source for historical config binding."}
            for pair in matched.PAIR_SPECS if pair.novel_id == novel_id]), encoding="utf-8")
        projects[novel_id] = SimpleNamespace(config=project.config, config_file=project.config_file,
            source_epub=carrier, source_txt=carrier, chapters_file=library,
            translation_addendum_file=project.translation_addendum_file, repository_root=repo)
    monkeypatch.setattr(matched, "load_novel_project", lambda novel_id, root: projects[novel_id])
    plan = matched.matched_plan_identity(repo_root=repo, evidence_root=tmp_path/"evidence")
    assert plan["provider_calls"] == 0 and plan["paid_execution_authorized"] is False
    assert len(plan["cases"]) == 102
    for case in plan["cases"]:
        identity = case["prompt"]
        novel_id = identity["novel_id"]
        historical_sha = matched.load_historical_config(novel_id, repo)[1]
        assert identity["config_sha256"] == historical_sha
        assert identity["config_canonical_sha256"] == NOVELS[novel_id]["config_canonical_sha256"]
    assert not (tmp_path/"evidence").exists()
