import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from full_pipeline_batch_wave import BATCH_MODEL, SUBMIT_AUTHORIZATION
from full_pipeline_experiment_contract_v2 import (
    FullPipelineExperimentContractV2Error,
    WAVE_BINDING_FILENAME,
    capture_experiment_state_binding,
    collect_experiment_wave,
    create_experiment,
    load_experiment_contract,
    load_wave_binding,
    prepare_experiment_wave,
    process_collected_wave,
    submit_experiment_wave,
)


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")


def _helpers():
    name = "_phase60g_contract_harness_helpers"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _make_v2(tmp_path, *, target_chapters=2, only_zh=False):
    h = _helpers()
    # Phase 60F intentionally configures synthetic source novels through
    # Chapter 26 so bounded 1/2-wave experiments do not accidentally exercise
    # the production final-partial-audit path.  Keep that shared fixture intact;
    # target_chapters bounds only the external experiment.
    repo = h._synthetic_repo(
        tmp_path,
        only_zh=only_zh,
    )
    experiment = tmp_path / "experiment_v2"
    novel_ids = (
        ("great_king_zhuxia",)
        if only_zh
        else ("great_king_zhuxia", "rogue_first_prince")
    )
    manifest = create_experiment(
        experiment,
        repo,
        repo_anchor="synthetic-phase60g-anchor",
        novel_ids=novel_ids,
        target_chapters=target_chapters,
        experiment_id="phase60g-contract-test",
    )
    return repo, experiment, manifest


def _collect_v2(experiment, repo, wave_number, *, wave_manifest=None):
    """Submit/collect a wave, preparing it only when it is not already frozen."""
    h = _helpers()
    if wave_manifest is None:
        wave_manifest, _target = prepare_experiment_wave(
            experiment,
            repo,
            wave_number,
        )
    else:
        assert wave_manifest["wave_number"] == wave_number
    client = h.BatchClient(f"v2-wave-{wave_number}")
    submitted = submit_experiment_wave(
        experiment,
        repo,
        wave_number,
        client,
        SUBMIT_AUTHORIZATION,
    )
    client.batches.batch.status = "completed"
    client.batches.batch.output_file_id = f"file-output-v2-{wave_number}"
    client.batches.batch.request_counts = SimpleNamespace(
        total=len(submitted["requests"]),
        completed=len(submitted["requests"]),
        failed=0,
    )
    client.files.contents[client.batches.batch.output_file_id] = "\n".join(
        json.dumps(
            h._batch_row(
                item["custom_id"],
                h._translation(item["novel_id"], wave_number),
            ),
            ensure_ascii=False,
        )
        for item in reversed(submitted["requests"])
    ) + "\n"
    document, collected = collect_experiment_wave(
        experiment,
        repo,
        wave_number,
        client,
    )
    assert document["status"] == "complete"
    assert collected["status"] == "collected"
    assert wave_manifest["run_id"] == collected["run_id"]
    return document, client


def test_state_binding_separates_causal_state_from_accounting_and_transient_files(tmp_path):
    repo, experiment, manifest = _make_v2(
        tmp_path,
        target_chapters=1,
        only_zh=True,
    )
    project = experiment / "projects" / manifest["novel_ids"][0]
    before = capture_experiment_state_binding(project)

    # Transient recovery artifacts are owned by other protocols and must not
    # alter either the causal or stable committed-history boundary identity.
    (project / "output" / "pending" / "0001.md").write_text(
        "pending", encoding="utf-8"
    )
    (project / "data" / "provider_request_journal" / "x.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (project / "data" / "glossary_candidate_resolutions" / "0001.json").write_text(
        "{}\n", encoding="utf-8"
    )
    transient = capture_experiment_state_binding(project)
    assert transient == before

    progress_path = project / "data" / "progress.json"
    progress = _read(progress_path)
    progress["total_prompt_tokens"] = 321
    progress["estimated_cost_usd"] = 0.0123
    _write(progress_path, progress)
    accounted = capture_experiment_state_binding(project)
    assert accounted["causal"] == before["causal"]
    assert accounted["causal_lineage_sha256"] == before["causal_lineage_sha256"]
    assert accounted["committed_evidence_sha256"] != before["committed_evidence_sha256"]

    glossary_path = project / "data" / "glossary.json"
    glossary = _read(glossary_path)
    glossary["entries"]["姚雲"] = {
        "english": "Yao Yun",
        "category": "character",
        "status": "provisional",
    }
    _write(glossary_path, glossary)
    semantic = capture_experiment_state_binding(project)
    assert semantic["causal_lineage_sha256"] != accounted["causal_lineage_sha256"]

    # Source production remains untouched by all experiment-only mutations.
    assert (repo / "novels" / "great_king_zhuxia" / "data" / "glossary.json").exists() is False


def test_contract_and_wave_binding_are_hash_bound_and_block_pre_provider_drift(tmp_path):
    repo, experiment, manifest = _make_v2(
        tmp_path,
        target_chapters=1,
        only_zh=True,
    )
    contract = load_experiment_contract(experiment)
    assert contract["experiment_id"] == manifest["experiment_id"]

    wave_manifest, target = prepare_experiment_wave(experiment, repo, 1)
    wave_binding = load_wave_binding(experiment, 1)
    assert (target / WAVE_BINDING_FILENAME).is_file()
    assert wave_binding["run_id"] == wave_manifest["run_id"]
    assert wave_binding["requests"][0]["state_binding_before"]["causal"] == (
        wave_manifest["requests"][0]["lineage_before"]
    )

    project = experiment / "projects" / "great_king_zhuxia"
    progress_path = project / "data" / "progress.json"
    progress = _read(progress_path)
    progress["total_prompt_tokens"] = 1
    _write(progress_path, progress)
    with pytest.raises(FullPipelineExperimentContractV2Error, match="state changed"):
        submit_experiment_wave(
            experiment,
            repo,
            1,
            _helpers().BatchClient("drift"),
            SUBMIT_AUTHORIZATION,
        )

    # Contract byte tampering is detected independently of project state.
    contract_path = experiment / "experiment_contract_v2.json"
    damaged = _read(contract_path)
    damaged["experiment_id"] = "tampered"
    _write(contract_path, damaged)
    with pytest.raises(FullPipelineExperimentContractV2Error, match="hash mismatch"):
        load_experiment_contract(experiment)


def test_v2_two_wave_real_pipeline_preserves_causal_growth_and_separate_evidence(tmp_path):
    h = _helpers()
    repo, experiment, manifest = _make_v2(tmp_path, target_chapters=2)

    _collect_v2(experiment, repo, 1)
    clients1, factory1 = h._client_factory_registry()
    processed1 = process_collected_wave(
        experiment,
        repo,
        1,
        client_factory=factory1,
    )
    assert processed1["status"] == "complete"
    assert len(processed1["completed_state_bindings_v2"]) == 2

    wave2, target2 = prepare_experiment_wave(experiment, repo, 2)
    rows = [
        json.loads(line)
        for line in (target2 / "batch_input.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prompts = [row["body"]["messages"][1]["content"] for row in rows]
    assert any("姚雲" in prompt and "Yao Yun" in prompt for prompt in prompts)
    assert any("아렌" in prompt and "Aren" in prompt for prompt in prompts)
    assert any(
        "Accepted continuity after Chapter 1 for great_king_zhuxia." in prompt
        for prompt in prompts
    )
    assert any(
        "Accepted continuity after Chapter 1 for rogue_first_prince." in prompt
        for prompt in prompts
    )

    binding2 = load_wave_binding(experiment, 2)
    by_novel = {item["novel_id"]: item for item in binding2["requests"]}
    for request in wave2["requests"]:
        frozen = by_novel[request["novel_id"]]["state_binding_before"]
        assert request["lineage_before"] == frozen["causal"]
        assert request["lineage_before_sha256"] == frozen["causal_lineage_sha256"]
        assert frozen["committed_evidence_sha256"]

    # Wave 2 is already frozen because we inspected its exact prompts above.
    # Submit/collect that same immutable wave rather than regenerating it.
    document2, _client2 = _collect_v2(
        experiment,
        repo,
        2,
        wave_manifest=wave2,
    )
    assert all(
        item["lineage_before"] == by_novel[item["novel_id"]]["state_binding_before"]["causal"]
        for item in document2["requests"]
    )
    clients2, factory2 = h._client_factory_registry()
    processed2 = process_collected_wave(
        experiment,
        repo,
        2,
        client_factory=factory2,
    )
    assert processed2["status"] == "complete"
    assert len(clients2) == 2

    for novel_id in manifest["novel_ids"]:
        project = experiment / "projects" / novel_id
        progress = _read(project / "data" / "progress.json")
        assert progress["last_completed"] == 2
        after = processed2["completed_state_bindings_v2"][novel_id]
        assert after["causal"]["scheduler"]["last_completed"] == 2
        assert after["committed_evidence_sha256"]
