"""Launch the legacy production engine with validated model and QA runtimes.

This adapter keeps production behavior isolated while the legacy production
engine is incrementally refactored behind validated runtime layers. It applies
model-specific runtime constants before loading the import-safe production
module, installs pending-translation provenance pinning around the core's
established atomic write path, persists exact model provenance into committed
QA JSON writes, installs a deterministic catastrophic-completeness hard gate,
wraps commit QA with calibrated semantic evidence in non-blocking shadow mode,
installs a fail-closed provider-reported model identity guard around the
supported paid-request path, and corrects the legacy runner's one stale Pro-only
pricing display label at runtime. The production module is loaded only after
runtime preparation. The launcher then explicitly invokes its runtime
initializer followed by its unattended runner; the legacy module's ``main()``
remains only a compatibility entry point.
"""

from contextlib import contextmanager
from production_provider_runtime import (provider_identity, inspect_pending_provider, install_goat_provenance, selected_provider, reject_unattended_goat)
from production_provider_contract import PROVIDER_COMMAND_CODE
import importlib.util
import os
import sys
from pathlib import Path

import noveltranslator_core as core
from catastrophic_completeness_runtime import install_catastrophic_completeness_qa
from committed_qa_provenance import install_committed_qa_model_provenance
from novel_project import (
    load_novel_config,
    load_novel_project,
    repository_root,
)
from production_completed_pending_cleanup import (
    cleanup_completed_pending,
    inspect_completed_pending_cleanup,
)
from production_run_lock import production_run_lock
from production_output_runtime import model_aware_legacy_print
from provider_model_identity_runtime import install_provider_model_identity_runtime
from semantic_shadow_runtime import (
    install_semantic_shadow_qa,
    make_console_semantic_shadow_reporter,
)
from translation_model_config import TRANSLATION_MODEL_ENV
from translation_model_runtime import (
    apply_core_model_config,
    install_pending_model_provenance,
    resolve_runtime_model,
)


PRODUCTION_SCRIPT = (
    repository_root()
    / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
PRODUCTION_MODULE_NAME = "_noveltranslator_legacy_production"


def load_production_module(path=None):
    """Load the import-safe production module without dispatching its main guard.

    The supported real production script also receives the provider-model
    identity guard before it is returned to production/recovery/trial callers.
    Custom fixture scripts stay untouched so this helper remains useful for
    focused import-safety tests.
    """
    path = Path(PRODUCTION_SCRIPT if path is None else path)
    if not path.is_file():
        raise FileNotFoundError(path)

    spec = importlib.util.spec_from_file_location(
        PRODUCTION_MODULE_NAME,
        path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot create production module spec for {path}"
        )

    module = importlib.util.module_from_spec(spec)
    missing_module = object()
    previous_module = sys.modules.get(
        PRODUCTION_MODULE_NAME,
        missing_module,
    )
    sys.modules[PRODUCTION_MODULE_NAME] = module

    try:
        spec.loader.exec_module(module)

        required_lifecycle = (
            "initialize_production_runtime_state",
            "run_unattended_production",
        )
        for name in required_lifecycle:
            if not callable(getattr(module, name, None)):
                raise RuntimeError(
                    f"production module does not define a callable {name}()"
                )

        if path.resolve() == Path(PRODUCTION_SCRIPT).resolve():
            install_provider_model_identity_runtime(module)
    except BaseException:
        if previous_module is missing_module:
            sys.modules.pop(PRODUCTION_MODULE_NAME, None)
        else:
            sys.modules[PRODUCTION_MODULE_NAME] = previous_module
        raise

    return module


def prepare_production_runtime(
    requested=None,
    pending_dir="output/pending",
    *,
    core_module=core,
    reporter=None,
    ignored_pending_chapters=(),
    provider_selection=None,
    pending_provider_identity=None,
):
    """Install the same in-memory runtime layers used by production.

    The preparation itself performs no API request and no filesystem write.
    ``install_pending_model_provenance`` wraps the core's existing atomic write
    functions so future paid pending metadata can carry exact configured model
    provenance. Provider-response identity is separately enforced by the
    production-module transport/journal guard before this configured provenance
    can be persisted. ``install_committed_qa_model_provenance`` composes on the
    pending writer and adds the same immutable model/request/pricing fields only
    to numeric JSON writes under the sibling ``output/qa`` directory. If a
    legacy paid pending chapter has no trustworthy model pin, that one chapter
    is excluded from committed-QA attribution rather than being falsely
    labelled.

    Catastrophic-completeness QA is installed before semantic shadow so the
    shadow observes the same authoritative commit decision used by production.
    The completeness layer is deterministic, provider-agnostic, zero-API, and
    performs no filesystem writes. The semantic shadow then wraps that commit QA
    independently and remains non-blocking.

    Returning wrapper states lets a read-only preflight restore the imported
    core module after exercising this exact launcher path.
    """
    pending_dir = Path(pending_dir)
    qa_dir = pending_dir.parent / "qa"
    if ignored_pending_chapters:
        runtime = resolve_runtime_model(
            requested,
            pending_dir,
            ignored_pending_chapters=ignored_pending_chapters,
        )
    else:
        runtime = resolve_runtime_model(requested, pending_dir)
    config = apply_core_model_config(core_module, runtime["config"])
    backend_identity = provider_identity(provider_selection, config.model)
    from production_accounting import validate_progress_accounting
    progress_path = pending_dir.parent.parent / "data/progress.json"
    if progress_path.is_file():
        import json
        validate_progress_accounting(json.loads(progress_path.read_text(encoding="utf-8")),
                                     expected_provider=backend_identity["provider"])
    pending_identity = pending_provider_identity or backend_identity
    if pending_provider_identity is not None:
        from production_provider_runtime import historical_capped_goat_provider_identity
        if (backend_identity["revision"] != "production-provider-runtime-v3"
            or pending_provider_identity != historical_capped_goat_provider_identity()):
            raise RuntimeError("pending migration identity is not the exact reviewed 50K/8K snapshot")
    provider_records = inspect_pending_provider(pending_dir, pending_identity,
        ignored_pending_chapters=ignored_pending_chapters)
    if backend_identity["provider"] == PROVIDER_COMMAND_CODE:
        pending_provenance = install_goat_provenance(core_module, config, pending_dir, pending_identity)
        committed_qa_provenance = pending_provenance
    else:
        pending_provenance = install_pending_model_provenance(
            core_module, config, pending_dir
        )
        legacy_unpinned_exclusions = (
            {runtime["pending_chapter"]}
            if runtime["status"] == "legacy_unpinned"
            and runtime["pending_chapter"] is not None
            else set()
        )
        committed_qa_provenance = install_committed_qa_model_provenance(
            core_module,
            config,
            qa_dir,
            excluded_chapters=legacy_unpinned_exclusions,
        )
    catastrophic_completeness = install_catastrophic_completeness_qa(
        core_module
    )
    semantic_shadow = install_semantic_shadow_qa(
        core_module,
        reporter=reporter,
        include_experimental=False,
    )
    return {
        "provider_identity": backend_identity,
        "pending_providers": provider_records,
        "runtime": runtime,
        "config": config,
        "pending_provenance": pending_provenance,
        "committed_qa_provenance": committed_qa_provenance,
        "catastrophic_completeness": catastrophic_completeness,
        "semantic_shadow": semantic_shadow,
    }


def _selected_project_from_working_directory():
    root = Path.cwd().resolve()
    config = load_novel_config(root / "novel.json")
    project = load_novel_project(
        config.id,
        repo_root=repository_root(),
    )
    if project.root.resolve() != root:
        raise RuntimeError(
            "production working directory does not match selected novel project"
        )
    return project


def reconcile_completed_pending_before_runtime():
    """Remove proof-complete post-commit residue before model selection."""
    project = _selected_project_from_working_directory()
    inspection = inspect_completed_pending_cleanup(project)
    removed = cleanup_completed_pending(inspection)
    for chapter in removed:
        print(
            "Removed proven completed pending residue for "
            f"Chapter {chapter} before model resolution."
        )
    return removed


def _run_production():
    reject_unattended_goat()
    requested = os.getenv(TRANSLATION_MODEL_ENV) or None
    # Provider identity must be safe before even local completed-pending cleanup.
    backend = provider_identity(None, requested)
    inspect_pending_provider(Path("output/pending"), backend)
    reconcile_completed_pending_before_runtime()
    prepared = prepare_production_runtime(
        requested,
        Path("output/pending"),
        reporter=make_console_semantic_shadow_reporter(),
    )
    runtime = prepared["runtime"]
    config = prepared["config"]
    committed_qa_provenance = prepared["committed_qa_provenance"]
    catastrophic_completeness = prepared["catastrophic_completeness"]
    semantic_shadow = prepared["semantic_shadow"]

    print("=" * 70)
    print("NOVELTRANSLATOR MODEL RUNTIME")
    print("=" * 70)
    print(f"Requested model: {runtime['requested_model']}")
    print(f"Effective model: {runtime['effective_model']}")
    if runtime["status"] == "pending_model_override":
        print(
            "Pending model override: Chapter "
            f"{runtime['pending_chapter']} was already paid with "
            f"{runtime['effective_model']}; continuing on that model."
        )
    elif runtime["status"] == "legacy_unpinned":
        print(
            "Legacy pending translation detected without model provenance; "
            "the paid translation will be reused and not regenerated."
        )
    print(f"Provider: {prepared['provider_identity']['provider']}")
    print(f"Wire model: {prepared['provider_identity']['wire_model']}")
    print(f"Model version: {config.version}")
    backend = prepared["provider_identity"]
    print(f"Reasoning: {backend.get('reasoning_effort') or config.thinking}")
    print(f"Translation completion ceiling: {backend['translation_completion_tokens']}")
    print(f"Pricing version: {backend['pricing_version']}")
    print(f"Accounting: {backend['accounting']}")
    print(
        "Provider model identity: ENABLED "
        "(exact response.model required for journaled paid translations)"
    )
    print(
        "Committed QA provenance: ENABLED "
        f"({committed_qa_provenance['mode']}; {config.model})"
    )
    if committed_qa_provenance["excluded_chapters"]:
        print(
            "Committed QA provenance exclusions: "
            + ", ".join(
                f"Chapter {chapter} (legacy unpinned)"
                for chapter in sorted(committed_qa_provenance["excluded_chapters"])
            )
        )
    print(
        "Catastrophic completeness QA: ENABLED "
        f"({catastrophic_completeness['mode']}; hard blocker; zero API calls)"
    )
    print(
        "Semantic shadow QA: ENABLED "
        f"({semantic_shadow['mode']}; non-blocking; zero API calls)"
    )
    print("=" * 70)

    with model_aware_legacy_print(config):
        production_module = load_production_module(PRODUCTION_SCRIPT)
        production_module.initialize_production_runtime_state()
        production_module.run_unattended_production()
    return 0


def main():
    reject_unattended_goat()
    with production_run_lock(Path.cwd()):
        return _run_production()




@contextmanager
def activated_goat_transport(module, expected_identity, *, budget=None):
    """Paid activation seam. Callers have already verified authorization/state."""
    from command_code_provider_credentials import command_code_api_key_scope
    from command_code_provider_transport import create_command_code_client
    from production_goat_runtime import install_goat_runtime, GoatBudget
    if not isinstance(budget, GoatBudget):
        raise RuntimeError("GOAT activation requires an explicit bounded budget before credentials")
    if provider_identity(PROVIDER_COMMAND_CODE, module.MODEL) != expected_identity:
        raise RuntimeError("GOAT activation provider/policy drift before credentials")
    client = None
    with command_code_api_key_scope() as key:
        try:
            client = create_command_code_client(api_key=key)
            key = None
            install_goat_runtime(module, provider_client=client, budget=budget)
            yield
        finally:
            runtime = getattr(module, "_production_goat_runtime", None)
            if runtime is not None:
                runtime["release_transport"]()
            module.client = None
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            client = None


if __name__ == "__main__":
    raise SystemExit(main())
