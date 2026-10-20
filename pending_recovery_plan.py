"""Read-only plan for safely finishing one existing paid pending chapter.

This planner never imports the side-effectful production runner, never calls a
provider, and never writes project data. It binds exactly one pending chapter,
its source/pending bytes, the current protected production state, the auxiliary
model, hard future API/cost ceilings, explicit high-risk tool identities, and a
repository-owned recovery/runtime manifest into a deterministic approval token.
"""

from production_provider_runtime import authorization_provider, provider_identity
from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import hashlib
import json
from pathlib import Path

from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
from chapter_library import index_chapter_library
from novel_project import load_novel_project, repository_root
from production_path_preflight import run_production_path_preflight
from production_partial_commit_reconciliation import (
    NORMAL_RECOVERY_MODE,
    PARTIAL_COMMIT_RECOVERY_MODE,
    PartialCommitReconciliationError,
    inspect_reviewed_partial_commit_evidence,
)
from semantic_integrity_audit import protected_state_snapshot
from translation_model_config import resolve_translation_model


PLAN_MODE = "bounded_existing_pending_recovery_plan"
DEFAULT_MAX_API_CALLS = 6
DEFAULT_MAX_COST_USD = 0.10
PLANNER_FILE = Path(__file__).resolve()
RECOVERY_EXECUTOR_FILE = PLANNER_FILE.with_name("pending_recovery_execute.py")
PRODUCTION_SCRIPT_FILE = PLANNER_FILE.with_name(
    "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
)
CHAPTER_COMMIT_FILE = PLANNER_FILE.with_name("production_chapter_commit.py")
REVIEWED_STATE_FILE = PLANNER_FILE.with_name("production_reviewed_state.py")
COMMIT_QA_CHECKPOINT_FILE = PLANNER_FILE.with_name(
    "production_commit_qa_checkpoint.py"
)
STATE_EXTRACTION_CHECKPOINT_FILE = PLANNER_FILE.with_name(
    "production_state_extraction_checkpoint.py"
)

PRODUCTION_RUN_LOCK_FILE = PLANNER_FILE.with_name("production_run_lock.py")
PAID_REQUEST_JOURNAL_FILE = PLANNER_FILE.with_name(
    "production_paid_request_journal.py"
)
CHAPTER_REVIEW_FILE = PLANNER_FILE.with_name("chapter_glossary_review.py")
CANDIDATE_PREPARATION_FILE = PLANNER_FILE.with_name("production_candidate_preparation.py")
PARTIAL_COMMIT_RECONCILIATION_FILE = PLANNER_FILE.with_name(
    "production_partial_commit_reconciliation.py"
)
ENGINE_RUNTIME_ROOT = PLANNER_FILE.parent
RUNTIME_MANIFEST_REVISION = "pending-recovery-runtime-manifest-v1"


class PendingRecoveryPlanError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise PendingRecoveryPlanError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingRecoveryPlanError(f"cannot read {label}: {path}: {exc}") from exc
    return value


def _sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _recovery_runtime_identity(profile_id):
    """Fingerprint repository-owned code/policy that recovery may execute."""

    root = Path(ENGINE_RUNTIME_ROOT).resolve()
    if not root.is_dir():
        raise PendingRecoveryPlanError(
            f"recovery engine root does not exist: {root}"
        )

    engine_files = sorted(
        path
        for path in root.glob("*.py")
        if path.is_file()
    )
    if not engine_files:
        raise PendingRecoveryPlanError(
            f"recovery engine root has no Python files: {root}"
        )

    profile_root = root / "profiles" / profile_id
    if not profile_root.is_dir():
        raise PendingRecoveryPlanError(
            "selected translation profile directory does not exist: "
            f"{profile_root}"
        )
    profile_files = sorted(
        path
        for path in profile_root.rglob("*")
        if path.is_file()
    )
    if not profile_files:
        raise PendingRecoveryPlanError(
            "selected translation profile contains no policy files: "
            f"{profile_root}"
        )

    dependency_manifest = root / "pyproject.toml"
    if not dependency_manifest.is_file():
        raise PendingRecoveryPlanError(
            "recovery dependency policy manifest does not exist: "
            f"{dependency_manifest}"
        )

    manifest_paths = [
        *engine_files,
        *profile_files,
        dependency_manifest,
    ]
    manifest = {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in manifest_paths
    }
    if any(not digest for digest in manifest.values()):
        raise PendingRecoveryPlanError(
            "cannot fingerprint recovery repository runtime manifest"
        )

    return {
        "revision": RUNTIME_MANIFEST_REVISION,
        "profile": profile_id,
        "root_python_file_count": len(engine_files),
        "profile_file_count": len(profile_files),
        "dependency_manifest": dependency_manifest.relative_to(root).as_posix(),
        "file_count": len(manifest),
        "sha256": _canonical_hash(manifest),
    }


def _numeric_files(path, suffix):
    path = Path(path)
    if not path.is_dir():
        return set()
    return {
        int(item.stem)
        for item in path.glob(f"*{suffix}")
        if item.is_file() and item.stem.isdigit()
    }


def _validate_budget(max_api_calls, max_cost_usd):
    if (
        not isinstance(max_api_calls, int)
        or isinstance(max_api_calls, bool)
        or max_api_calls < 1
    ):
        raise PendingRecoveryPlanError("max_api_calls must be a positive integer")
    if isinstance(max_cost_usd, bool) or not isinstance(max_cost_usd, (int, float)):
        raise PendingRecoveryPlanError("max_cost_usd must be a positive number")
    max_cost_usd = float(max_cost_usd)
    if max_cost_usd <= 0:
        raise PendingRecoveryPlanError("max_cost_usd must be greater than zero")
    return max_api_calls, max_cost_usd


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


def _recovery_tool_identity():
    tools = {
        "planner": PLANNER_FILE,
        "executor": RECOVERY_EXECUTOR_FILE,
        "production_script": PRODUCTION_SCRIPT_FILE,
        "chapter_commit": CHAPTER_COMMIT_FILE,
        "reviewed_state": REVIEWED_STATE_FILE,
        "commit_qa_checkpoint": COMMIT_QA_CHECKPOINT_FILE,
        "state_extraction_checkpoint": STATE_EXTRACTION_CHECKPOINT_FILE,
        "candidate_preparation": CANDIDATE_PREPARATION_FILE,
        "chapter_review": CHAPTER_REVIEW_FILE,
        "production_run_lock": PRODUCTION_RUN_LOCK_FILE,
        "paid_request_journal": PAID_REQUEST_JOURNAL_FILE,
        "partial_commit_reconciliation": PARTIAL_COMMIT_RECONCILIATION_FILE,
    }
    identity = {}
    for label, path in tools.items():
        path = Path(path)
        if not path.is_file():
            raise PendingRecoveryPlanError(
                f"recovery safety tool {label} does not exist: {path}"
            )
        digest = _sha256_file(path)
        if not digest:
            raise PendingRecoveryPlanError(
                f"cannot fingerprint recovery safety tool {label}: {path}"
            )
        identity[label] = {
            "path": path.name,
            "sha256": digest,
        }
    return identity


def build_pending_recovery_plan(
    novel_id,
    *,
    repo_root=None,
    model_selection=None,
    max_api_calls=DEFAULT_MAX_API_CALLS,
    max_cost_usd=None,
    provider_selection=None,
    max_credit_equivalent_usd=None,
):
    """Bind one existing numeric pending chapter to a future bounded recovery."""
    provider_binding, allowance = authorization_provider(provider_selection, model_selection,
        max_cost_usd, max_credit_equivalent_usd)
    max_cost_usd = DEFAULT_MAX_COST_USD if allowance is None else allowance
    max_api_calls, max_cost_usd = _validate_budget(max_api_calls, max_cost_usd)
    requested_config = resolve_translation_model(model_selection)
    project = load_novel_project(novel_id, repo_root=repo_root)

    preflight = run_production_path_preflight(
        novel_id,
        repo_root=repo_root,
        model_selection=requested_config.model,
        **({"completed_uncertain_pending_recovery": True}
           if (project.data_dir / "uncertain_request_dispositions").exists() else {}),
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
    )
    if preflight.get("status") != "PASS":
        raise PendingRecoveryPlanError(
            "production-path preflight is not clean: "
            + "; ".join(preflight.get("errors", []) or ["unknown preflight failure"])
        )

    pending_status = preflight.get("pending_status")
    pending_chapter = preflight.get("pending_chapter")
    if pending_status not in {
        "legacy_unpinned",
        "pending_model_match",
        "pending_model_override",
    } or not isinstance(pending_chapter, int):
        raise PendingRecoveryPlanError(
            "bounded recovery requires one existing pending translation; "
            f"preflight reported {pending_status!r} for Chapter {pending_chapter!r}"
        )

    effective_model = preflight.get("effective_model")
    try:
        config = resolve_translation_model(effective_model)
    except ValueError as exc:
        raise PendingRecoveryPlanError(
            "production preflight returned an invalid effective recovery model: "
            f"{effective_model!r}"
        ) from exc

    requested_provider_binding = provider_binding
    provider_binding = provider_identity(provider_binding["provider"], config.model)

    progress = _read_json(project.progress_file, "progress")
    chapters = _read_json(project.chapters_file, "chapter library")
    if not isinstance(progress, dict):
        raise PendingRecoveryPlanError("progress must be an object")
    if not isinstance(chapters, list):
        raise PendingRecoveryPlanError("chapter library must be a list")

    target = progress.get("next_chapter")
    if not isinstance(target, int) or isinstance(target, bool) or target < 1:
        raise PendingRecoveryPlanError("progress.next_chapter must be a positive integer")
    if target != pending_chapter:
        raise PendingRecoveryPlanError(
            f"progress.next_chapter is {target}, but preflight pending chapter is {pending_chapter}"
        )
    if target > project.config.final_chapter:
        raise PendingRecoveryPlanError("pending chapter is beyond configured final chapter")

    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise PendingRecoveryPlanError("progress.completed must be a list")
    if target in completed:
        raise PendingRecoveryPlanError(f"Chapter {target} is already completed")

    committed = _numeric_files(project.chapter_dir, ".md")

    pending_md_set = _numeric_files(project.pending_dir, ".md")
    pending_json_set = _numeric_files(project.pending_dir, ".json")
    if pending_md_set != {target} or pending_json_set != {target}:
        raise PendingRecoveryPlanError(
            "bounded recovery requires exactly one matching pending .md/.json pair; "
            f"md={sorted(pending_md_set)}, json={sorted(pending_json_set)}"
        )

    pending_md = Path(project.pending_dir) / f"{target:04d}.md"
    pending_json = Path(project.pending_dir) / f"{target:04d}.json"
    translation = pending_md.read_text(encoding="utf-8")
    if not translation.strip():
        raise PendingRecoveryPlanError("pending translation is empty")
    meta = _read_json(pending_json, "pending metadata")
    if not isinstance(meta, dict):
        raise PendingRecoveryPlanError("pending metadata must be an object")

    recovery_mode = NORMAL_RECOVERY_MODE
    partial_commit_evidence = {
        "mode": NORMAL_RECOVERY_MODE,
        "version": "reviewed-partial-commit-v1",
        "final_exists": False,
        "final_sha256": "",
        "qa_sha256": "",
    }
    if target in committed:
        try:
            partial_commit_evidence = inspect_reviewed_partial_commit_evidence(
                chapter_number=target,
                pending_file=pending_md,
                pending_meta_file=pending_json,
                final_file=Path(project.chapter_dir) / f"{target:04d}.md",
                qa_file=Path(project.qa_dir) / f"{target:04d}.json",
                progress=progress,
                meta=meta,
            )
        except PartialCommitReconciliationError as exc:
            raise PendingRecoveryPlanError(str(exc)) from exc
        recovery_mode = partial_commit_evidence["mode"]
        if recovery_mode != PARTIAL_COMMIT_RECOVERY_MODE:
            raise PendingRecoveryPlanError(
                f"Chapter {target} already has committed output without "
                "proven reviewed partial-commit evidence"
            )
    if meta.get("chapter") not in (None, target):
        raise PendingRecoveryPlanError(
            f"pending metadata chapter {meta.get('chapter')!r} does not match {target}"
        )
    stage = str(meta.get("stage", "translated") or "translated")
    if stage not in {"translated", "state_extracted", "reviewed"}:
        raise PendingRecoveryPlanError(f"unsupported pending recovery stage {stage!r}")
    if stage in {"state_extracted", "reviewed"} and not isinstance(
        meta.get("state_checkpoint_identity"),
        dict,
    ):
        raise PendingRecoveryPlanError(
            f"pending Chapter {target} stage {stage} lacks "
            "state_checkpoint_identity"
        )
    if stage == "reviewed" and not isinstance(
        meta.get("review_checkpoint_identity"),
        dict,
    ):
        raise PendingRecoveryPlanError(
            f"pending Chapter {target} stage reviewed lacks "
            "review_checkpoint_identity"
        )

    review_recovery = meta.get(CHAPTER_REVIEW_RECOVERY_KEY)
    if stage != "reviewed" and review_recovery is not None:
        if not isinstance(review_recovery, dict):
            raise PendingRecoveryPlanError("chapter glossary-review recovery must be an object")
        original = review_recovery.get("original")
        if (
            "original" in review_recovery
            or review_recovery.get("status") != "pending"
        ) and not (
            isinstance(original, dict) and isinstance(original.get("result"), dict)
        ):
            raise PendingRecoveryPlanError(
                "original chapter glossary review has no valid durable response; "
                "automatic retry is forbidden because paid work may have occurred. "
                "Preserve pending files and inspect recovery evidence before proceeding."
            )

    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    source_entry = chapter_map.get(target)
    if not isinstance(source_entry, dict):
        raise PendingRecoveryPlanError(f"Chapter {target} is absent from source library")
    source_text = source_entry.get("text", "")
    if not isinstance(source_text, str) or not source_text.strip():
        raise PendingRecoveryPlanError(f"Chapter {target} has no source text")

    local_qa = meta.get("local_qa") if isinstance(meta.get("local_qa"), dict) else {}
    has_state = isinstance(meta.get("state"), dict)
    has_review = isinstance(meta.get("review_result"), dict)
    minimum_new_logical_calls = (
        0
        if recovery_mode == PARTIAL_COMMIT_RECOVERY_MODE
        else (0 if has_state else 1)
    )

    protected = protected_state_snapshot(project)
    recovery_tools = _recovery_tool_identity()
    recovery_runtime_identity = _recovery_runtime_identity(
        project.config.profile
    )
    state_binding = {
        "provider_identity": provider_binding,
        "requested_provider_identity": requested_provider_binding,
        "pending_provider": provider_binding["provider"],
        "requested_provider": requested_provider_binding["provider"],
        "effective_provider": provider_binding["provider"],
        "provider": provider_binding["provider"],
        "budget_unit": provider_binding["pricing_unit"],
        "max_credit_equivalent_usd": (max_cost_usd if provider_binding["provider"] == PROVIDER_COMMAND_CODE else None),
        "novel_id": project.config.id,
        "target_chapter": target,
        "pending_status": pending_status,
        "pending_stage": stage,
        "requested_auxiliary_model": requested_config.model,
        "effective_auxiliary_model": config.model,
        "model_version": config.version,
        "pricing_version": provider_binding["pricing_version"],
        "max_api_calls": max_api_calls,
        "max_cost_usd": round(max_cost_usd, 8),
        "pending_md_sha256": _sha256_file(pending_md),
        "pending_json_sha256": _sha256_file(pending_json),
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "protected_state": protected,
        "immutable_inputs": _immutable_inputs(project),
        "recovery_tools": recovery_tools,
        "recovery_runtime_identity": recovery_runtime_identity,
        "recovery_mode": recovery_mode,
        "partial_commit_evidence": partial_commit_evidence,
        "completed": completed,
        "committed_outputs": sorted(committed),
        "translation_generation_allowed": False,
        "startup_periodic_audits_allowed": False,
    }
    state_fingerprint = _canonical_hash(state_binding)
    approval_payload = {
        "provider_identity": provider_binding,
        "requested_provider_identity": requested_provider_binding,
        "pending_provider": provider_binding["provider"],
        "requested_provider": requested_provider_binding["provider"],
        "effective_provider": provider_binding["provider"],
        "provider": provider_binding["provider"],
        "budget_unit": provider_binding["pricing_unit"],
        "max_credit_equivalent_usd": (max_cost_usd if provider_binding["provider"] == PROVIDER_COMMAND_CODE else None),
        "mode": PLAN_MODE,
        "state_fingerprint": state_fingerprint,
        "novel_id": project.config.id,
        "target_chapter": target,
        "auxiliary_model": config.model,
        "max_api_calls": max_api_calls,
        "max_cost_usd": round(max_cost_usd, 8),
        "recovery_tools": recovery_tools,
        "recovery_runtime_identity": recovery_runtime_identity,
        "recovery_mode": recovery_mode,
        "partial_commit_evidence": partial_commit_evidence,
        "translation_generation_allowed": False,
        "startup_periodic_audits_allowed": False,
    }
    approval_token = _canonical_hash(approval_payload)

    return {
        "mode": PLAN_MODE,
        "provider_identity": provider_binding,
        "requested_provider_identity": requested_provider_binding,
        "pending_provider": provider_binding["provider"],
        "requested_provider": requested_provider_binding["provider"],
        "effective_provider": provider_binding["provider"],
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
        "target_chapter": target,
        "pending_status": pending_status,
        "pending_stage": stage,
        "recovery_mode": recovery_mode,
        "partial_commit_evidence": partial_commit_evidence,
        "pending_md_sha256": state_binding["pending_md_sha256"],
        "pending_json_sha256": state_binding["pending_json_sha256"],
        "source_text_sha256": state_binding["source_text_sha256"],
        "translation_characters": len(translation),
        "saved_local_qa_commit_allowed": local_qa.get("commit_allowed"),
        "has_saved_state": has_state,
        "has_saved_glossary_review": has_review,
        "minimum_new_logical_api_calls": minimum_new_logical_calls,
        "requested_auxiliary_model": requested_config.model,
        "auxiliary_model": config.model,
        "model_version": config.version,
        "pricing_version": provider_binding["pricing_version"],
        "max_api_calls": max_api_calls,
        "max_cost_usd": max_cost_usd,
        "recovery_planner_sha256": recovery_tools["planner"]["sha256"],
        "recovery_executor_sha256": recovery_tools["executor"]["sha256"],
        "recovery_production_script_sha256": (
            recovery_tools["production_script"]["sha256"]
        ),
        "recovery_chapter_commit_sha256": (
            recovery_tools["chapter_commit"]["sha256"]
        ),
        "recovery_reviewed_state_sha256": (
            recovery_tools["reviewed_state"]["sha256"]
        ),
        "recovery_commit_qa_checkpoint_sha256": (
            recovery_tools["commit_qa_checkpoint"]["sha256"]
        ),
        "recovery_state_extraction_checkpoint_sha256": (
            recovery_tools["state_extraction_checkpoint"]["sha256"]
        ),
        "recovery_runtime_identity": recovery_runtime_identity,
        "recovery_partial_commit_reconciliation_sha256": (
            recovery_tools["partial_commit_reconciliation"]["sha256"]
        ),
        "recovery_production_run_lock_sha256": recovery_tools["production_run_lock"]["sha256"],
        "recovery_chapter_review_sha256": recovery_tools["chapter_review"]["sha256"],
        "recovery_candidate_preparation_sha256": recovery_tools["candidate_preparation"]["sha256"],
        "translation_generation_allowed": False,
        "startup_periodic_audits_allowed": False,
        "state_fingerprint": state_fingerprint,
        "approval_token": approval_token,
        "protected_state_unchanged": preflight.get("protected_state_unchanged", False),
        "preflight_status": preflight.get("status", ""),
    }


def _print_human(plan):
    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        print("Provider: command_code_provider; budget/accounting: credit-equivalent USD (no invoice claim)")
        print(json.dumps(plan["provider_identity"], ensure_ascii=False, sort_keys=True))
    print("=" * 70)
    print("BOUNDED EXISTING-PENDING RECOVERY — READ-ONLY PLAN")
    print("=" * 70)
    print(f"Status: {plan['status']}")
    print(f"Novel: {plan['novel_id']} — {plan['title']}")
    print(f"Target chapter: {plan['target_chapter']}")
    print(f"Pending status: {plan['pending_status']}")
    print(f"Pending stage: {plan['pending_stage']}")
    print(f"Recovery mode: {plan['recovery_mode']}")
    if plan["partial_commit_evidence"].get("final_exists"):
        print(
            "Proven partial final SHA256: "
            f"{plan['partial_commit_evidence']['final_sha256']}"
        )
        print(
            "Proven committed QA SHA256: "
            f"{plan['partial_commit_evidence']['qa_sha256']}"
        )
    print(f"Pending translation characters: {plan['translation_characters']}")
    print(f"Saved local QA commit_allowed: {plan['saved_local_qa_commit_allowed']!r}")
    print(f"Saved state: {'YES' if plan['has_saved_state'] else 'NO'}")
    print(f"Saved glossary review: {'YES' if plan['has_saved_glossary_review'] else 'NO'}")
    print(f"Minimum new logical API calls: {plan['minimum_new_logical_api_calls']}")
    print(f"Requested auxiliary model: {plan['requested_auxiliary_model']}")
    print(f"Effective auxiliary model: {plan['auxiliary_model']}")
    if plan["requested_auxiliary_model"] != plan["auxiliary_model"]:
        print("Pending model override: YES")
    print(f"Model version: {plan['model_version']}")
    print(f"Pricing version: {plan['pricing_version']}")
    print(f"Future raw provider-attempt ceiling: {plan['max_api_calls']}")
    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        print(f"Future allowance: {plan['max_credit_equivalent_usd']:.4f} credit-equivalent USD; provider_invoice_claimed=false")
    else:
        print(f"Future cost ceiling: ${plan['max_cost_usd']:.4f}")
    print(f"Recovery planner SHA256: {plan['recovery_planner_sha256']}")
    print(f"Recovery executor SHA256: {plan['recovery_executor_sha256']}")
    print(
        "Recovery production script SHA256: "
        f"{plan['recovery_production_script_sha256']}"
    )
    print(
        "Recovery chapter commit SHA256: "
        f"{plan['recovery_chapter_commit_sha256']}"
    )
    print(
        "Recovery reviewed state SHA256: "
        f"{plan['recovery_reviewed_state_sha256']}"
    )
    print(
        "Recovery commit QA checkpoint SHA256: "
        f"{plan['recovery_commit_qa_checkpoint_sha256']}"
    )
    print(
        "Recovery state extraction checkpoint SHA256: "
        f"{plan['recovery_state_extraction_checkpoint_sha256']}"
    )
    runtime_identity = plan["recovery_runtime_identity"]
    print(
        "Recovery repository runtime manifest: "
        f"{runtime_identity['sha256']} "
        f"({runtime_identity['file_count']} files; "
        f"profile {runtime_identity['profile']}; "
        f"{runtime_identity['dependency_manifest']})"
    )
    print(
        "Recovery partial-commit reconciliation SHA256: "
        f"{plan['recovery_partial_commit_reconciliation_sha256']}"
    )
    print(f"Recovery candidate preparation SHA256: {plan['recovery_candidate_preparation_sha256']}")
    print(f"Recovery chapter review SHA256: {plan['recovery_chapter_review_sha256']}")
    print(f"Recovery production run lock SHA256: {plan['recovery_production_run_lock_sha256']}")
    print("Translation generation allowed: NO")
    print("Startup periodic audits allowed: NO")
    print(f"State fingerprint: {plan['state_fingerprint']}")
    print(f"Approval token: {plan['approval_token']}")
    print("Paid execution authorized: NO")
    print("API calls: 0")
    print("Project writes: 0")
    print(
        "Protected production state unchanged: "
        + ("YES" if plan["protected_state_unchanged"] else "NO")
    )
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Bind exactly one existing paid pending chapter to a future bounded recovery."
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--max-credit-equivalent-usd", type=float, default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--model", default="pro")
    parser.add_argument("--max-api-calls", type=int, default=DEFAULT_MAX_API_CALLS)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        plan = build_pending_recovery_plan(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            max_api_calls=args.max_api_calls,
            max_cost_usd=args.max_cost_usd,
            provider_selection=args.provider,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd,
        )
    except Exception as exc:
        report = {
            "mode": PLAN_MODE,
            "status": "NOT_READY",
            "paid_execution_authorized": False,
            "api_calls": 0,
            "project_writes": 0,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("BOUNDED EXISTING-PENDING RECOVERY — READ-ONLY PLAN")
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
