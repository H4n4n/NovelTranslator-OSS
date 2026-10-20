import importlib.util
import json
import sys
from pathlib import Path


HELPER_PATH = Path(__file__).with_name("test_full_pipeline_experiment_harness.py")


def _load_harness_helpers():
    """Reuse the established zero-provider fixtures without duplicating the engine."""
    name = "_phase60f_full_pipeline_harness_helpers"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _audit_provider_calls(clients):
    return [
        request
        for client in clients
        for request in client.completions.calls
        if "periodic audit" in request["messages"][0]["content"].lower()
    ]


def test_real_engine_runs_no_periodic_audit_through_24_then_mutates_at_25(tmp_path):
    """Exercise the genuine production audit scheduler at its natural interval."""
    h = _load_harness_helpers()
    repo, experiment, manifest = h._make_experiment(
        tmp_path,
        target_chapters=25,
        only_zh=True,
    )
    assert manifest["target_chapters"] == 25

    project = experiment / "projects" / "great_king_zhuxia"
    config = _load_json(project / "novel.json")
    assert config["audit_interval"] == 25
    assert config["final_chapter"] == 26

    audit_file = project / "output" / "audits" / "audit_0001_0025.json"
    audit_history_path = project / "data" / "audit_history.json"
    progress_path = project / "data" / "progress.json"

    for wave_number in range(1, 25):
        h._collect(experiment, repo, wave_number)
        clients, factory = h._client_factory_registry()
        processed = h.process_collected_wave(
            experiment,
            repo,
            wave_number,
            client_factory=factory,
        )
        assert processed["status"] == "complete"

        progress = _load_json(progress_path)
        history = _load_json(audit_history_path)
        assert progress["last_completed"] == wave_number
        assert progress["next_chapter"] == wave_number + 1
        assert progress["last_audited_chapter"] == 0
        assert history == {"audits": []}
        assert not audit_file.exists()
        assert _audit_provider_calls(clients) == []

    h._collect(experiment, repo, 25)
    clients25, factory25 = h._client_factory_registry()
    processed25 = h.process_collected_wave(
        experiment,
        repo,
        25,
        client_factory=factory25,
    )
    assert processed25["status"] == "complete"

    progress25 = _load_json(progress_path)
    history25 = _load_json(audit_history_path)
    assert progress25["last_completed"] == 25
    assert progress25["next_chapter"] == 26
    assert progress25["last_audited_chapter"] == 25
    assert len(history25["audits"]) == 1
    assert audit_file.is_file()

    audit_record = _load_json(audit_file)
    assert audit_record == history25["audits"][0]
    assert _audit_provider_calls(clients25)

    checkpoint = project / "backup" / "checkpoint_0025"
    assert checkpoint.is_dir()
    assert (checkpoint / "glossary.json").is_file()
    assert (checkpoint / "continuity.json").is_file()
    assert (checkpoint / "progress.json").is_file()
