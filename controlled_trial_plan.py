"""Read-only plan gate for a future one-chapter paid production trial.

This command never imports the side-effectful production runner, never calls a
provider, and never writes project data. It binds one exact next chapter, model,
budget envelope, and project-state fingerprint into a deterministic approval
token that a later paid executor can require.
"""

from production_provider_runtime import authorization_provider
from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import hashlib
import json
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project
from production_path_preflight import run_production_path_preflight
from semantic_integrity_audit import protected_state_snapshot
from translation_model_config import resolve_translation_model


PLAN_MODE = "controlled_one_chapter_trial_plan"
DEFAULT_MAX_API_CALLS = 6
DEFAULT_MAX_COST_USD = 0.10

PLANNER_FILE = Path(__file__).resolve()
TRIAL_EXECUTOR_FILE = PLANNER_FILE.with_name(
    "controlled_trial_execute.py"
)
PRODUCTION_SCRIPT_FILE = PLANNER_FILE.with_name(
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
PRODUCTION_LAUNCHER_FILE = PLANNER_FILE.with_name(
    "run_production_engine.py"
)
PEAK_WRAPPER_FILE = PLANNER_FILE.with_name(
    "controlled_trial_peak_allowed.py"
)
REPO_ENV_FILE = PLANNER_FILE.with_name(
    "controlled_trial_repo_env.py"
)
ENGINE_RUNTIME_ROOT = PLANNER_FILE.parent


class ControlledTrialPlanError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise ControlledTrialPlanError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlledTrialPlanError(f"cannot read {label}: {path}: {exc}") from exc


def _sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _numeric_files(path, suffix):
    path = Path(path)
    if not path.is_dir():
        return set()
    return {
        int(item.stem)
        for item in path.glob(f"*{suffix}")
        if item.is_file() and item.stem.isdigit()
    }


def _immutable_inputs(project):
    files = {
        "novel_json": project.config_file,
        "chapters_json": project.chapters_file,
        "translation_addendum": project.translation_addendum_file,
        "production_translation": project.translation_prompt_file,
        "source_overrides": project.source_overrides_file,
    }
    return {
        label: {
            "exists": Path(path).is_file(),
            "sha256": _sha256_file(path),
        }
        for label, path in files.items()
    }


def _trial_tool_identity():
    """Fingerprint the exact safety/production code used by a paid trial."""

    tools = {
        "planner": PLANNER_FILE,
        "executor": TRIAL_EXECUTOR_FILE,
        "production_script": PRODUCTION_SCRIPT_FILE,
        "production_launcher": PRODUCTION_LAUNCHER_FILE,
        "peak_wrapper": PEAK_WRAPPER_FILE,
        "credential_bridge": REPO_ENV_FILE,
    }

    identity = {}

    for label, tool_path in tools.items():
        tool_path = Path(tool_path)

        if not tool_path.is_file():
            raise ControlledTrialPlanError(
                f"controlled-trial safety tool {label} "
                f"does not exist: {tool_path}"
            )

        digest = _sha256_file(tool_path)

        if not digest:
            raise ControlledTrialPlanError(
                f"cannot fingerprint controlled-trial "
                f"safety tool {label}: {tool_path}"
            )

        identity[label] = {
            "path": tool_path.name,
            "sha256": digest,
        }

    return identity


def _canonical_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _engine_runtime_identity(profile_id):
    """Fingerprint shared engine code and the selected immutable profile policy."""

    root = Path(ENGINE_RUNTIME_ROOT).resolve()
    if not root.is_dir():
        raise ControlledTrialPlanError(
            f"controlled-trial engine root does not exist: {root}"
        )

    engine_files = sorted(
        path
        for path in root.glob("*.py")
        if path.is_file()
    )
    if not engine_files:
        raise ControlledTrialPlanError(
            f"controlled-trial engine root has no Python files: {root}"
        )

    profile_root = root / "profiles" / profile_id
    if not profile_root.is_dir():
        raise ControlledTrialPlanError(
            "selected translation profile directory does not exist: "
            f"{profile_root}"
        )
    profile_files = sorted(
        path
        for path in profile_root.rglob("*")
        if path.is_file()
    )
    if not profile_files:
        raise ControlledTrialPlanError(
            "selected translation profile contains no policy files: "
            f"{profile_root}"
        )

    manifest = {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in engine_files + profile_files
    }
    if any(not digest for digest in manifest.values()):
        raise ControlledTrialPlanError(
            "cannot fingerprint controlled-trial engine/runtime manifest"
        )

    return {
        "profile": profile_id,
        "file_count": len(manifest),
        "sha256": _canonical_hash(manifest),
    }


def _validate_budget(max_api_calls, max_cost_usd):
    if (
        not isinstance(max_api_calls, int)
        or isinstance(max_api_calls, bool)
        or max_api_calls < 1
    ):
        raise ControlledTrialPlanError("max_api_calls must be a positive integer")
    if isinstance(max_cost_usd, bool) or not isinstance(max_cost_usd, (int, float)):
        raise ControlledTrialPlanError("max_cost_usd must be a positive number")
    max_cost_usd = float(max_cost_usd)
    if max_cost_usd <= 0:
        raise ControlledTrialPlanError("max_cost_usd must be greater than zero")
    return max_api_calls, max_cost_usd


def build_controlled_trial_plan(
    novel_id,
    *,
    repo_root=None,
    model_selection=None,
    max_api_calls=DEFAULT_MAX_API_CALLS,
    max_cost_usd=None,
    provider_selection=None,
    max_credit_equivalent_usd=None,
    allow_peak=False,
):
    """Return a deterministic read-only plan for exactly one future chapter.

    The project must have no pending translation. This deliberately excludes
    legacy-unpinned recovery cases from the first paid hardening trial.
    """
    provider_binding, allowance = authorization_provider(provider_selection, model_selection,
        max_cost_usd, max_credit_equivalent_usd)
    max_cost_usd = DEFAULT_MAX_COST_USD if allowance is None else allowance
    max_api_calls, max_cost_usd = _validate_budget(
        max_api_calls,
        max_cost_usd,
    )

    if not isinstance(allow_peak, bool):
        raise ControlledTrialPlanError(
            "allow_peak must be a boolean"
        )

    scheduler_mode = (
        "peak_allowed"
        if allow_peak
        else "off_peak_only"
    )

    if provider_binding["provider"] == PROVIDER_COMMAND_CODE:
        if allow_peak:
            raise ControlledTrialPlanError("GOAT does not use direct allow_peak authorization")
        scheduler_mode = "provider_unrestricted"
    config = resolve_translation_model(model_selection)
    project = load_novel_project(novel_id, repo_root=repo_root)

    preflight = run_production_path_preflight(
        novel_id,
        repo_root=repo_root,
        model_selection=config.model,
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
    )
    if preflight.get("status") != "PASS":
        raise ControlledTrialPlanError(
            "production-path preflight is not clean: "
            + "; ".join(preflight.get("errors", []) or ["unknown preflight failure"])
        )
    if preflight.get("pending_status") != "requested" or preflight.get(
        "pending_chapter"
    ) is not None:
        raise ControlledTrialPlanError(
            "controlled first trial requires no existing pending translation; "
            f"preflight reported {preflight.get('pending_status')!r} "
            f"for Chapter {preflight.get('pending_chapter')!r}"
        )

    progress = _read_json(project.progress_file, "progress")
    chapters = _read_json(project.chapters_file, "chapter library")
    if not isinstance(progress, dict):
        raise ControlledTrialPlanError("progress must be an object")
    if not isinstance(chapters, list):
        raise ControlledTrialPlanError("chapter library must be a list")

    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    next_chapter = progress.get("next_chapter")
    if (
        not isinstance(next_chapter, int)
        or isinstance(next_chapter, bool)
        or next_chapter < 1
    ):
        raise ControlledTrialPlanError("progress.next_chapter must be a positive integer")
    if next_chapter > project.config.final_chapter:
        raise ControlledTrialPlanError("novel has no remaining chapter for a trial")
    if next_chapter not in chapter_map:
        raise ControlledTrialPlanError(
            f"Chapter {next_chapter} is absent from the validated source library"
        )

    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise ControlledTrialPlanError("progress.completed must be a list")
    if next_chapter in completed:
        raise ControlledTrialPlanError(
            f"Chapter {next_chapter} is already marked completed"
        )

    committed = _numeric_files(project.chapter_dir, ".md")
    pending_md = _numeric_files(project.pending_dir, ".md")
    pending_json = _numeric_files(project.pending_dir, ".json")
    if pending_md or pending_json:
        raise ControlledTrialPlanError(
            "controlled first trial requires an empty numeric pending set"
        )
    if next_chapter in committed:
        raise ControlledTrialPlanError(
            f"Chapter {next_chapter} already has a committed output file"
        )

    if provider_binding["provider"] == PROVIDER_COMMAND_CODE:
        from production_paid_request_journal import paid_request_journal_path
        initial_path = paid_request_journal_path(project.data_dir / "provider_request_journal",
            "chapter_translation", next_chapter)
        if initial_path.exists():
            from goat_saturation_recovery_plan import inspect_target
            try:
                inspect_target(project, initial_only=True)
            except Exception:
                # Other recorded rejections retain their historical zero-send replay/report path.
                pass
            else:
                raise ControlledTrialPlanError("recorded GOAT target requires goat_saturation_recovery_plan.py; not a fresh trial")

    source_entry = chapter_map[next_chapter]
    source_text = source_entry.get("text", "")
    if not isinstance(source_text, str) or not source_text.strip():
        raise ControlledTrialPlanError(
            f"Chapter {next_chapter} has no non-empty source text"
        )

    protected = protected_state_snapshot(project)
    immutable = _immutable_inputs(project)
    trial_tools = _trial_tool_identity()
    engine_runtime_identity = _engine_runtime_identity(
        project.config.profile
    )

    state_binding = {
        "provider_identity": provider_binding,
        "provider": provider_binding["provider"],
        "budget_unit": provider_binding["pricing_unit"],
        "max_credit_equivalent_usd": (max_cost_usd if provider_binding["provider"] == PROVIDER_COMMAND_CODE else None),
        "novel_id": project.config.id,
        "target_chapter": next_chapter,
        "model": config.model,
        "model_version": config.version,
        "pricing_version": provider_binding["pricing_version"],
        "scheduler_mode": scheduler_mode,
        "allow_peak": allow_peak,
        "trial_tools": trial_tools,
        "engine_runtime_identity": engine_runtime_identity,
        "max_api_calls": max_api_calls,
        "max_cost_usd": round(max_cost_usd, 8),
        "protected_state": protected,
        "immutable_inputs": immutable,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "completed": completed,
        "committed_outputs": sorted(committed),
    }
    state_fingerprint = _canonical_hash(state_binding)
    approval_payload = {
        "provider_identity": provider_binding,
        "provider": provider_binding["provider"],
        "budget_unit": provider_binding["pricing_unit"],
        "max_credit_equivalent_usd": (max_cost_usd if provider_binding["provider"] == PROVIDER_COMMAND_CODE else None),
        "mode": PLAN_MODE,
        "state_fingerprint": state_fingerprint,
        "novel_id": project.config.id,
        "target_chapter": next_chapter,
        "model": config.model,
        "scheduler_mode": scheduler_mode,
        "allow_peak": allow_peak,
        "trial_tools": trial_tools,
        "engine_runtime_identity": engine_runtime_identity,
        "max_api_calls": max_api_calls,
        "max_cost_usd": round(max_cost_usd, 8),
    }
    approval_token = _canonical_hash(approval_payload)

    return {
        "mode": PLAN_MODE,
        "provider_identity": provider_binding,
        "provider": provider_binding["provider"],
        "budget_unit": provider_binding["pricing_unit"],
        "max_credit_equivalent_usd": (max_cost_usd if provider_binding["provider"] == PROVIDER_COMMAND_CODE else None),
        "status": "READY",
        "paid_execution_authorized": False,
        "api_calls": 0,
        "project_writes": 0,
        "novel_id": project.config.id,
        "title": project.config.title,
        "project_root": str(project.root),
        "profile": project.config.profile,
        "target_chapter": next_chapter,
        "source_text_sha256": state_binding["source_text_sha256"],
        "completed_chapter_count": len(completed),
        "committed_output_count": len(committed),
        "pending_numeric_count": len(pending_md | pending_json),
        "requested_model": config.model,
        "model_key": config.key,
        "model_version": config.version,
        "thinking": (provider_binding["reasoning_effort"] if provider_binding["provider"] == PROVIDER_COMMAND_CODE else config.thinking),
        "max_output_tokens": (provider_binding["translation_completion_tokens"] if provider_binding["provider"] == PROVIDER_COMMAND_CODE else config.max_tokens),
        "pricing_version": provider_binding["pricing_version"],
        "scheduler_mode": scheduler_mode,
        "allow_peak": allow_peak,
        "trial_tools": trial_tools,
        "engine_runtime_identity": engine_runtime_identity,
        "max_api_calls": max_api_calls,
        "max_cost_usd": max_cost_usd,
        "state_fingerprint": state_fingerprint,
        "approval_token": approval_token,
        "protected_state_unchanged": preflight.get(
            "protected_state_unchanged", False
        ),
        "preflight_status": preflight.get("status", ""),
        "preflight_pending_status": preflight.get("pending_status", ""),
    }


def _print_human(plan):
    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        print("Provider: command_code_provider; budget/accounting: credit-equivalent USD (no invoice claim)")
        print(json.dumps(plan["provider_identity"], ensure_ascii=False, sort_keys=True))
    print("=" * 70)
    print("CONTROLLED ONE-CHAPTER PRODUCTION TRIAL — READ-ONLY PLAN")
    print("=" * 70)
    print(f"Status: {plan['status']}")
    print(f"Novel: {plan['novel_id']} — {plan['title']}")
    print(f"Project root: {plan['project_root']}")
    print(f"Profile: {plan['profile']}")
    print(f"Target chapter: {plan['target_chapter']}")
    print(
        "Current progress: "
        f"{plan['completed_chapter_count']} completed / "
        f"{plan['committed_output_count']} committed files"
    )
    print(f"Existing numeric pending files: {plan['pending_numeric_count']}")
    print(f"Pinned model: {plan['requested_model']}")
    print(f"Model version: {plan['model_version']}")
    print(f"Thinking: {plan['thinking']}")
    print(f"Max model output tokens: {plan['max_output_tokens']}")
    print(f"Pricing version: {plan['pricing_version']}")
    print(f"Scheduler mode: {plan['scheduler_mode']}")
    print(f"Future trial API-call ceiling: {plan['max_api_calls']}")
    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        print(f"Future trial allowance: {plan['max_credit_equivalent_usd']:.4f} credit-equivalent USD; provider_invoice_claimed=false")
    else:
        print(f"Future trial cost ceiling: ${plan['max_cost_usd']:.4f}")
    print(f"State fingerprint: {plan['state_fingerprint']}")
    print(f"Approval token: {plan['approval_token']}")
    print("Paid execution authorized: NO")
    print(f"API calls: {plan['api_calls']}")
    print(f"Project writes: {plan['project_writes']}")
    print(
        "Protected production state unchanged: "
        + ("YES" if plan["protected_state_unchanged"] else "NO")
    )
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Bind a clean project state, next chapter, model, and future budget "
            "into a read-only approval token. Makes zero API calls and writes nothing."
        )
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--max-credit-equivalent-usd", type=float, default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--model", default=None, help="pro (default), flash, or full ID")
    parser.add_argument(
        "--allow-peak",
        action="store_true",
        help=(
            "Bind this approval to peak-allowed execution. "
            "Execution must then use controlled_trial_peak_allowed.py."
        ),
    )
    parser.add_argument("--max-api-calls", type=int, default=DEFAULT_MAX_API_CALLS)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        plan = build_controlled_trial_plan(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            max_api_calls=args.max_api_calls,
            max_cost_usd=args.max_cost_usd,
            provider_selection=args.provider,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            allow_peak=args.allow_peak,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": PLAN_MODE,
                "status": "NOT_READY",
                "paid_execution_authorized": False,
                "api_calls": 0,
                "project_writes": 0,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("CONTROLLED ONE-CHAPTER PRODUCTION TRIAL — READ-ONLY PLAN")
            print("=" * 70)
            print("Status: NOT_READY")
            print(f"Error: {exc}")
            print("Paid execution authorized: NO")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_human(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
