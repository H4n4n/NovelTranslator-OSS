from types import SimpleNamespace
import sys

import pytest

import noveltranslator_core as core
import run_production_engine as launcher
from translation_model_config import resolve_translation_model


@pytest.mark.parametrize(
    ("model", "version", "pricing_version"),
    [
        (
            "deepseek-v4-pro",
            "DeepSeek-V4-Pro-0813",
            "deepseek-v4-pro-0813-peak-offpeak",
        ),
        (
            "deepseek-v4-flash",
            "DeepSeek-V4-Flash-0731",
            "deepseek-v4-flash-0731-peak-offpeak",
        ),
    ],
)
def test_launcher_installs_runtime_layers_before_callable_production_module(
    model, version, pricing_version, tmp_path, monkeypatch, capsys
):
    events = []
    script = tmp_path / "legacy_runner.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "PRODUCTION_SCRIPT", script)
    monkeypatch.setattr(
        launcher,
        "reconcile_completed_pending_before_runtime",
        lambda: (),
    )
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", model)

    runtime = {
        "requested_model": model,
        "effective_model": model,
        "status": "requested",
        "pending_chapter": None,
        "config": f"{model}-config",
    }
    config = SimpleNamespace(
        model=model,
        version=version,
        thinking="disabled",
        max_tokens=12000,
        pricing_version=pricing_version,
    )

    monkeypatch.setattr(
        launcher,
        "resolve_runtime_model",
        lambda requested, pending_dir: runtime,
    )
    monkeypatch.setattr(
        launcher,
        "apply_core_model_config",
        lambda core, selected: config,
    )
    monkeypatch.setattr(
        launcher,
        "install_pending_model_provenance",
        lambda core, selected, pending_dir: events.append("pending_provenance"),
    )

    def install_committed(core, selected, qa_dir, *, excluded_chapters=()):
        assert selected is config
        assert str(qa_dir).replace("\\", "/").endswith("output/qa")
        assert not excluded_chapters
        events.append("committed_qa_provenance")
        return {
            "mode": "committed_qa_model_provenance",
            "excluded_chapters": frozenset(),
        }

    monkeypatch.setattr(
        launcher,
        "install_committed_qa_model_provenance",
        install_committed,
    )

    def install_completeness(core):
        events.append("catastrophic_completeness")
        return {"mode": "catastrophic_completeness_hard_gate"}

    monkeypatch.setattr(
        launcher,
        "install_catastrophic_completeness_qa",
        install_completeness,
    )

    reporter = object()
    monkeypatch.setattr(
        launcher,
        "make_console_semantic_shadow_reporter",
        lambda: reporter,
    )

    def install_shadow(core, *, reporter=None, include_experimental=False):
        assert reporter is not None
        assert include_experimental is False
        events.append("semantic_shadow")
        return {"mode": "calibrated_non_blocking_shadow"}

    monkeypatch.setattr(launcher, "install_semantic_shadow_qa", install_shadow)
    production_module = SimpleNamespace(
        initialize_production_runtime_state=lambda: events.append(
            "production_initialize"
        ),
        run_unattended_production=lambda: events.append(
            "production_run"
        ),
    )
    monkeypatch.setattr(
        launcher,
        "load_production_module",
        lambda path: (
            events.append(("load_production_module", str(path)))
            or production_module
        ),
    )

    assert launcher.main() == 0
    assert events[0:4] == [
        "pending_provenance",
        "committed_qa_provenance",
        "catastrophic_completeness",
        "semantic_shadow",
    ]
    assert events[4] == ("load_production_module", str(script))
    assert events[5:] == [
        "production_initialize",
        "production_run",
    ]
    output = capsys.readouterr().out
    assert f"Effective model: {model}" in output
    assert "Committed QA provenance: ENABLED" in output
    assert "Catastrophic completeness QA: ENABLED" in output
    assert "hard blocker; zero API calls" in output
    assert "Semantic shadow QA: ENABLED" in output
    assert "non-blocking; zero API calls" in output


def test_prepare_excludes_only_legacy_unpinned_pending_chapter(
    tmp_path, monkeypatch
):
    pending_dir = tmp_path / "output" / "pending"
    pending_dir.mkdir(parents=True)
    runtime = {
        "requested_model": "deepseek-v4-pro",
        "effective_model": "deepseek-v4-pro",
        "status": "legacy_unpinned",
        "pending_chapter": 393,
        "config": "pro-config",
    }
    config = SimpleNamespace(model="deepseek-v4-pro")
    captured = {}

    monkeypatch.setattr(
        launcher,
        "resolve_runtime_model",
        lambda requested, pending: runtime,
    )
    monkeypatch.setattr(
        launcher,
        "apply_core_model_config",
        lambda core, selected: config,
    )
    monkeypatch.setattr(
        launcher,
        "install_pending_model_provenance",
        lambda core, selected, pending: {"model": selected.model},
    )

    def install_committed(core, selected, qa_dir, *, excluded_chapters=()):
        captured["qa_dir"] = qa_dir
        captured["excluded"] = set(excluded_chapters)
        return {
            "mode": "committed_qa_model_provenance",
            "excluded_chapters": frozenset(excluded_chapters),
        }

    monkeypatch.setattr(
        launcher,
        "install_committed_qa_model_provenance",
        install_committed,
    )
    monkeypatch.setattr(
        launcher,
        "install_catastrophic_completeness_qa",
        lambda core: {"mode": "catastrophic_completeness_hard_gate"},
    )
    monkeypatch.setattr(
        launcher,
        "install_semantic_shadow_qa",
        lambda core, reporter=None, include_experimental=False: {
            "mode": "calibrated_non_blocking_shadow"
        },
    )

    prepared = launcher.prepare_production_runtime(
        "pro", pending_dir, core_module=SimpleNamespace()
    )
    assert captured["qa_dir"] == pending_dir.parent / "qa"
    assert captured["excluded"] == {393}
    assert prepared["committed_qa_provenance"]["excluded_chapters"] == frozenset({393})
    assert prepared["catastrophic_completeness"]["mode"] == (
        "catastrophic_completeness_hard_gate"
    )


def test_prepare_runtime_forwards_only_explicit_proven_ignore_set(
    tmp_path,
    monkeypatch,
):
    pending_dir = tmp_path / "output" / "pending"
    pending_dir.mkdir(parents=True)
    captured = {}

    runtime = {
        "status": "requested",
        "requested_model": "deepseek-v4-pro",
        "effective_model": "deepseek-v4-pro",
        "config": resolve_translation_model("pro"),
        "pending_chapter": None,
    }

    def resolve(requested, pending, *, ignored_pending_chapters=()):
        captured["ignored"] = tuple(sorted(ignored_pending_chapters))
        return runtime

    monkeypatch.setattr(launcher, "resolve_runtime_model", resolve)
    monkeypatch.setattr(
        launcher,
        "apply_core_model_config",
        lambda core, config: config,
    )
    monkeypatch.setattr(
        launcher,
        "install_pending_model_provenance",
        lambda core, config, pending: {"model": config.model},
    )
    monkeypatch.setattr(
        launcher,
        "install_committed_qa_model_provenance",
        lambda core, config, qa, excluded_chapters=(): {
            "mode": "test",
            "excluded_chapters": frozenset(excluded_chapters),
        },
    )
    monkeypatch.setattr(
        launcher,
        "install_catastrophic_completeness_qa",
        lambda core: {"mode": "catastrophic_completeness_hard_gate"},
    )
    monkeypatch.setattr(
        launcher,
        "install_semantic_shadow_qa",
        lambda core, reporter=None, include_experimental=False: {
            "mode": "test"
        },
    )

    launcher.prepare_production_runtime(
        "pro",
        pending_dir,
        core_module=SimpleNamespace(),
        ignored_pending_chapters={7, 3},
    )

    assert captured["ignored"] == (3, 7)


def test_run_production_reconciles_completed_pending_before_model_runtime(
    monkeypatch,
):
    events = []

    class StopAfterOrderingProof(Exception):
        pass

    monkeypatch.setattr(
        launcher,
        "reconcile_completed_pending_before_runtime",
        lambda: events.append("cleanup"),
    )

    def stop_prepare(*args, **kwargs):
        events.append("prepare")
        raise StopAfterOrderingProof()

    monkeypatch.setattr(
        launcher,
        "prepare_production_runtime",
        stop_prepare,
    )

    with pytest.raises(StopAfterOrderingProof):
        launcher._run_production()

    assert events == ["cleanup", "prepare"]


def test_completed_pending_proof_failure_blocks_before_model_runtime(
    monkeypatch,
):
    class ProofFailure(RuntimeError):
        pass

    monkeypatch.setattr(
        launcher,
        "reconcile_completed_pending_before_runtime",
        lambda: (_ for _ in ()).throw(ProofFailure("ambiguous residue")),
    )
    monkeypatch.setattr(
        launcher,
        "prepare_production_runtime",
        lambda *args, **kwargs: pytest.fail(
            "model runtime reached after cleanup proof failure"
        ),
    )

    with pytest.raises(ProofFailure, match="ambiguous residue"):
        launcher._run_production()


def test_load_production_module_is_inert_until_lifecycle_is_called(
    tmp_path,
    monkeypatch,
):
    script = tmp_path / "production_module.py"
    marker = tmp_path / "lifecycle_called.txt"
    script.write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "events = []\n"
        "def initialize_production_runtime_state():\n"
        "    events.append('initialize')\n"
        "def run_unattended_production():\n"
        "    events.append('run')\n"
        "    MARKER.write_text('called', encoding='utf-8')\n"
        "def main():\n"
        "    raise AssertionError('legacy main should not be required by loader')\n"
        "if __name__ == '__main__':\n"
        "    raise AssertionError('main guard executed during module load')\n",
        encoding="utf-8",
    )

    monkeypatch.delitem(
        sys.modules,
        launcher.PRODUCTION_MODULE_NAME,
        raising=False,
    )

    module = launcher.load_production_module(script)

    assert module.__name__ == launcher.PRODUCTION_MODULE_NAME
    assert sys.modules[launcher.PRODUCTION_MODULE_NAME] is module
    assert module.events == []
    assert not marker.exists()

    module.initialize_production_runtime_state()
    module.run_unattended_production()

    assert module.events == ["initialize", "run"]
    assert marker.read_text(encoding="utf-8") == "called"


@pytest.mark.parametrize(
    ("source", "missing_name"),
    [
        (
            "def run_unattended_production():\n"
            "    return None\n",
            "initialize_production_runtime_state",
        ),
        (
            "def initialize_production_runtime_state():\n"
            "    return None\n",
            "run_unattended_production",
        ),
    ],
)
def test_load_production_module_requires_callable_lifecycle(
    source,
    missing_name,
    tmp_path,
    monkeypatch,
):
    script = tmp_path / "production_module.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.delitem(
        sys.modules,
        launcher.PRODUCTION_MODULE_NAME,
        raising=False,
    )

    with pytest.raises(
        RuntimeError,
        match=rf"production module does not define a callable "
        rf"{missing_name}\(\)",
    ):
        launcher.load_production_module(script)

    assert launcher.PRODUCTION_MODULE_NAME not in sys.modules


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ],
)
def test_real_production_module_captures_prepared_runtime_without_initializing(
    model,
    tmp_path,
    monkeypatch,
):
    runtime_names = (
        "PRODUCTION_TRANSLATION_MODEL",
        "PRODUCTION_TRANSLATION_MAX_TOKENS",
        "PRODUCTION_TRANSLATION_THINKING",
        "V4_PRO_PRICES",
        "PRICING_VERSION",
        "atomic_write_text",
        "atomic_write_json",
        "translation_commit_qa",
    )
    snapshot = {
        name: getattr(core, name)
        for name in runtime_names
    }
    completeness_state_present = hasattr(
        core,
        "_catastrophic_completeness_runtime_state",
    )
    completeness_state = getattr(
        core,
        "_catastrophic_completeness_runtime_state",
        None,
    )
    semantic_state_present = hasattr(
        core,
        "_semantic_shadow_runtime_state",
    )
    semantic_state = getattr(
        core,
        "_semantic_shadow_runtime_state",
        None,
    )

    pending_dir = tmp_path / "output" / "pending"
    before_paths = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    }

    monkeypatch.chdir(tmp_path)

    try:
        prepared = launcher.prepare_production_runtime(
            model,
            pending_dir,
            core_module=core,
            reporter=None,
        )
        config = prepared["config"]

        prepared_write_text = core.atomic_write_text
        prepared_write_json = core.atomic_write_json
        prepared_commit_qa = core.translation_commit_qa

        monkeypatch.delitem(
            sys.modules,
            launcher.PRODUCTION_MODULE_NAME,
            raising=False,
        )
        module = launcher.load_production_module(
            launcher.PRODUCTION_SCRIPT
        )

        assert sys.modules[launcher.PRODUCTION_MODULE_NAME] is module

        # The real production module must capture the runtime selected
        # before its import, not stale default values.
        assert module.PRODUCTION_TRANSLATION_MODEL == config.model
        assert (
            module.PRODUCTION_TRANSLATION_MAX_TOKENS
            == config.max_tokens
        )
        assert (
            module.PRODUCTION_TRANSLATION_THINKING
            == config.thinking
        )
        assert module.PRICING_VERSION == config.pricing_version
        assert module.V4_PRO_PRICES == core.V4_PRO_PRICES

        # The direct imports used by the legacy module must capture the
        # already-installed production safety wrappers.
        assert module.atomic_write_text is prepared_write_text
        assert module.atomic_write_json is prepared_write_json
        assert module.translation_commit_qa is prepared_commit_qa

        # Importing the actual 140 KB module must remain inert.
        assert module.MODEL is None
        assert module.RUN_LOGGER is None
        assert module.api_key is None
        assert module.client is None
        assert module.chapters is None
        assert module.chapter_map is None
        assert module.glossary is None
        assert module.progress is None
        assert module.continuity is None
        assert module.issues_data is None
        assert module.audit_history is None
        assert module.system_prompt is None
        assert module._RUNTIME_INITIALIZED is False
        assert module._RUNTIME_INITIALIZING is False

        after_paths = {
            path.relative_to(tmp_path)
            for path in tmp_path.rglob("*")
        }
        assert after_paths == before_paths

    finally:
        for name, value in snapshot.items():
            setattr(core, name, value)

        if completeness_state_present:
            core._catastrophic_completeness_runtime_state = completeness_state
        elif hasattr(core, "_catastrophic_completeness_runtime_state"):
            delattr(core, "_catastrophic_completeness_runtime_state")

        if semantic_state_present:
            core._semantic_shadow_runtime_state = semantic_state
        elif hasattr(core, "_semantic_shadow_runtime_state"):
            delattr(core, "_semantic_shadow_runtime_state")


@pytest.fixture(autouse=True)
def explicit_direct_route(monkeypatch, tmp_path):
    """Launcher layer proofs use explicit direct selection and synthetic history."""
    monkeypatch.setenv("NOVELTRANSLATOR_PROVIDER_BACKEND", "direct_deepseek")
    monkeypatch.setenv("NOVELTRANSLATOR_TRANSLATION_MODEL", "pro")
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "progress.json").write_text("{}", encoding="utf-8")
