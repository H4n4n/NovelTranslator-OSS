"""Read-only preflight for the hardened production path.

This command exercises the same launcher runtime preparation used by real
production against an actual novel project while deliberately stopping before
the side-effectful legacy runner is imported. It validates project/profile
state, pending-model recovery, model-specific pricing, pending provenance,
committed-QA model provenance, and semantic shadow QA with zero provider calls
and zero project writes.
"""

from production_provider_runtime import selected_provider, ProductionProviderError, HistoricalGoatManualReviewRequired
from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import noveltranslator_core as core
from chapter_library import index_chapter_library
from novel_project import load_novel_project, repository_root
from noveltranslator_core import (
    glossary_strict_qa_errors,
    validate_progress_consistency,
)
from noveltranslator_observability import progress_commit_integrity_errors
from production_completed_pending_cleanup import (
    CompletedPendingCleanupError,
    inspect_completed_pending_cleanup,
)
from production_output_runtime import rewrite_legacy_rate_label
from production_paid_request_journal import inspect_paid_request_journals
from run_production_engine import prepare_production_runtime
from semantic_integrity_audit import protected_state_snapshot
from translation_profile import (
    effective_translation_instructions,
    load_novel_terminology_policy,
    load_translation_profile,
)


PREFLIGHT_MODE = "production_path_read_only_preflight"
OFF_PEAK_SAMPLE_UTC = datetime(2026, 9, 7, 0, 30, tzinfo=timezone.utc)
PEAK_SAMPLE_UTC = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)


class ProductionPreflightError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise ProductionPreflightError(f"{label} does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionPreflightError(f"cannot read {label}: {path}: {exc}") from exc


def _numeric_chapter_files(path):
    path = Path(path)
    if not path.is_dir():
        return set()
    return {
        int(item.stem)
        for item in path.glob("*.md")
        if item.is_file() and item.stem.isdigit()
    }


def _capture_core_runtime(core_module):
    names = (
        "PRODUCTION_TRANSLATION_MODEL",
        "PRODUCTION_TRANSLATION_MAX_TOKENS",
        "PRODUCTION_TRANSLATION_THINKING",
        "V4_PRO_PRICES",
        "PRICING_VERSION",
        "atomic_write_text",
        "atomic_write_json",
        "translation_commit_qa",
    )
    snapshot = {name: getattr(core_module, name) for name in names}
    snapshot["semantic_state_present"] = hasattr(
        core_module, "_semantic_shadow_runtime_state"
    )
    snapshot["semantic_state"] = getattr(
        core_module, "_semantic_shadow_runtime_state", None
    )
    return snapshot


def _restore_core_runtime(core_module, snapshot):
    for name, value in snapshot.items():
        if name in {"semantic_state_present", "semantic_state"}:
            continue
        setattr(core_module, name, value)
    if snapshot["semantic_state_present"]:
        core_module._semantic_shadow_runtime_state = snapshot["semantic_state"]
    elif hasattr(core_module, "_semantic_shadow_runtime_state"):
        delattr(core_module, "_semantic_shadow_runtime_state")


def _deny_write_factory(counter, label):
    def deny(*_args, **_kwargs):
        counter["attempts"] += 1
        raise ProductionPreflightError(
            f"preflight blocked an unexpected project write through {label}"
        )

    return deny


def run_production_path_preflight(
    novel_id,
    *,
    repo_root=None,
    model_selection=None,
    engine_root=None,
    core_module=core,
    provider_selection=None,
    completed_uncertain_pending_recovery=False,
):
    """Exercise hardened production preparation against one real project.

    No legacy runner or provider client is imported. After launcher preparation
    installs the normal provenance wrappers, atomic writes are replaced by
    fail-closed guards for the remainder of the preflight. The imported core
    module is restored in ``finally``.
    """
    project = load_novel_project(novel_id, repo_root=repo_root)
    engine_root = Path(engine_root or repository_root()).resolve()
    before = protected_state_snapshot(project)

    chapters = _read_json(project.chapters_file, "chapter library")
    glossary_data = _read_json(project.glossary_file, "glossary")
    progress = _read_json(project.progress_file, "progress")
    if not isinstance(chapters, list):
        raise ProductionPreflightError("chapter library must be a list")
    if not isinstance(glossary_data, dict) or not isinstance(
        glossary_data.get("entries"), dict
    ):
        raise ProductionPreflightError("glossary must contain an entries object")
    if not isinstance(progress, dict):
        raise ProductionPreflightError("progress must be an object")

    profile = load_translation_profile(project.config.profile, engine_root)
    effective_translation_instructions(profile, project.translation_addendum_file)
    load_novel_terminology_policy(profile, project.translation_addendum_file)
    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    committed_files = _numeric_chapter_files(project.chapter_dir)
    progress_report = validate_progress_consistency(progress, committed_files)
    progress_errors = progress_commit_integrity_errors(
        progress,
        project.chapter_dir,
        project.pending_dir,
    )
    cleanup_error = ""
    try:
        completed_pending_cleanup = inspect_completed_pending_cleanup(
            project
        )
    except CompletedPendingCleanupError as exc:
        completed_pending_cleanup = None
        cleanup_error = (
            "completed-pending cleanup proof failed: "
            f"{exc}"
        )
    cleanup_chapters = (
        completed_pending_cleanup.chapters
        if completed_pending_cleanup is not None
        else ()
    )
    strict_config_errors = glossary_strict_qa_errors(
        glossary_data["entries"],
        project.config.id,
        profile.high_priority_categories,
    )

    core_snapshot = _capture_core_runtime(core_module)
    write_counter = {"attempts": 0}
    prepared = None
    qa = None
    sample_chapter = None
    off_peak = None
    peak = None
    rate_label = ""
    provider_error = ""
    provider_manual_review = False
    try:
        if not cleanup_error:
            prepared = prepare_production_runtime(
                model_selection,
                project.pending_dir,
                core_module=core_module,
                reporter=None,
                ignored_pending_chapters=cleanup_chapters,
                **({"provider_selection": provider_selection} if provider_selection is not None else {}),
            )
            # From this point onward, fail closed if any code unexpectedly
            # attempts to persist through the production core's established
            # write path.
            core_module.atomic_write_text = _deny_write_factory(
                write_counter, "atomic_write_text"
            )
            core_module.atomic_write_json = _deny_write_factory(
                write_counter, "atomic_write_json"
            )

            config = prepared["config"]
            off_peak = core_module.pricing_snapshot(OFF_PEAK_SAMPLE_UTC)
            peak = core_module.pricing_snapshot(PEAK_SAMPLE_UTC)
            if prepared["provider_identity"]["provider"] == PROVIDER_COMMAND_CODE:
                from benchmark_longitudinal_g7d_accounting_policy import pricing_snapshot
                off_peak = pricing_snapshot(prepared["provider_identity"]["wire_model"], OFF_PEAK_SAMPLE_UTC)
                peak = pricing_snapshot(prepared["provider_identity"]["wire_model"], PEAK_SAMPLE_UTC)
            rate_label = ("GOAT public rate card / 1M: USD credit equivalents; no invoice claim"
                if prepared["provider_identity"]["provider"] == PROVIDER_COMMAND_CODE else
                rewrite_legacy_rate_label("V4 Pro rates / 1M:", config))

            completed = progress.get("completed", [])
            if isinstance(completed, list):
                completed = [
                    value
                    for value in completed
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                ]
            else:
                completed = []
            if completed:
                candidate = max(completed)
                source_entry = chapter_map.get(candidate)
                output_path = (
                    Path(project.chapter_dir) / f"{candidate:04d}.md"
                )
                if source_entry is not None and output_path.is_file():
                    source_text = source_entry.get("text", "")
                    translation = output_path.read_text(encoding="utf-8")
                    qa = core_module.translation_commit_qa(
                        source_text,
                        translation,
                        glossary_data["entries"],
                        profile.high_priority_categories,
                        source_language=project.config.source_language,
                        chapter_number=candidate,
                    )
                    sample_chapter = candidate
    except ProductionProviderError as exc:
        provider_error = str(exc)
        provider_manual_review = isinstance(exc, HistoricalGoatManualReviewRequired)
    finally:
        _restore_core_runtime(core_module, core_snapshot)

    after = protected_state_snapshot(project)
    protected_unchanged = before == after
    paid_request_journals = inspect_paid_request_journals(
        Path(project.root) / "data" / "provider_request_journal"
    )
    from production_uncertain_request_disposition import verified_completed_dispositions
    try:
        paid_request_journals = verified_completed_dispositions(project, paid_request_journals,
            allow_pending=completed_uncertain_pending_recovery)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        paid_request_journals["errors"].append("operator disposition/completed recovery proof failed")
    runtime = prepared["runtime"] if prepared else {}
    committed_qa_provenance = (
        prepared.get("committed_qa_provenance", {}) if prepared else {}
    )
    provenance_exclusions = sorted(
        committed_qa_provenance.get("excluded_chapters", ()) or ()
    )
    semantic_shadow = qa.get("semantic_shadow", {}) if isinstance(qa, dict) else {}
    semantic_changed = bool(semantic_shadow.get("commit_decision_changed", False))

    errors = []
    if provider_error:
        errors.append(provider_error)
    if cleanup_error:
        errors.append(cleanup_error)
    errors.extend(progress_errors)
    errors.extend(strict_config_errors)
    errors.extend(paid_request_journals["errors"])
    if paid_request_journals["uncertain"]:
        errors.append(
            "uncertain paid provider request requires operator review: "
            + ", ".join(
                f"{item['operation_kind']} chapter {item['chapter']} "
                f"({item['operation_id']})"
                for item in paid_request_journals["uncertain"]
            )
        )
    if not protected_unchanged:
        errors.append("protected production state changed during preflight")
    if write_counter["attempts"]:
        errors.append(
            f"unexpected project write attempts blocked: {write_counter['attempts']}"
        )
    if semantic_changed:
        errors.append("semantic shadow changed the legacy commit decision")

    expected_qa_root = (Path(project.pending_dir).parent / "qa").resolve()
    actual_qa_root = committed_qa_provenance.get("qa_root")
    if prepared is not None:
        if actual_qa_root != expected_qa_root:
            errors.append(
                "committed QA provenance is bound to the wrong QA directory: "
                f"expected {expected_qa_root}, got {actual_qa_root}"
            )
        expected_exclusions = (
            [runtime.get("pending_chapter")]
            if runtime.get("status") == "legacy_unpinned"
            and runtime.get("pending_chapter") is not None
            else []
        )
        if provenance_exclusions != expected_exclusions:
            errors.append(
                "committed QA provenance exclusion set does not match pending recovery: "
                f"expected {expected_exclusions}, got {provenance_exclusions}"
            )

    return {
        "mode": PREFLIGHT_MODE,
        "status": "PASS" if not errors else "FAIL",
        "provider_manual_review_required": provider_manual_review,
        "novel_id": project.config.id,
        "title": project.config.title,
        "project_root": str(project.root),
        "profile": profile.id,
        "final_chapter": project.config.final_chapter,
        "completed_chapter_count": len(progress.get("completed", []) or []),
        "audited_through": progress.get("last_audited_chapter", len(progress.get("completed", []) or [])),
        "committed_output_count": len(committed_files),
        "requested_model": runtime.get("requested_model", ""),
        "effective_model": runtime.get("effective_model", ""),
        "pending_status": runtime.get("status", ""),
        "pending_chapter": runtime.get("pending_chapter"),
        "completed_pending_cleanup_required": (
            completed_pending_cleanup.required
            if completed_pending_cleanup is not None
            else False
        ),
        "completed_pending_cleanup_chapters": (
            list(completed_pending_cleanup.chapters)
            if completed_pending_cleanup is not None
            else []
        ),
        "completed_pending_cleanup_states": (
            [
                {
                    "chapter": record.chapter,
                    "state": record.state,
                }
                for record in completed_pending_cleanup.records
            ]
            if completed_pending_cleanup is not None
            else []
        ),
        "requested_provider": selected_provider(provider_selection),
        "effective_provider": prepared["provider_identity"]["provider"] if prepared else "",
        "provider_identity": prepared["provider_identity"] if prepared else {},
        "pending_providers": prepared["pending_providers"] if prepared else [],
        "provider_compatible": bool(prepared),
        "pricing_version": prepared["provider_identity"]["pricing_version"] if prepared else "",
        "off_peak_pricing": off_peak,
        "peak_pricing": peak,
        "rate_label": rate_label,
        "committed_qa_provenance_mode": committed_qa_provenance.get("mode", ""),
        "committed_qa_provenance_qa_root": str(actual_qa_root or ""),
        "committed_qa_provenance_excluded_chapters": provenance_exclusions,
        "semantic_shadow_mode": (
            prepared["semantic_shadow"].get("mode", "") if prepared else ""
        ),
        "sample_chapter": sample_chapter,
        "sample_legacy_commit_allowed": (
            qa.get("commit_allowed") if isinstance(qa, dict) else None
        ),
        "sample_semantic_commit_decision_changed": semantic_changed,
        "sample_semantic_diagnostic_count": semantic_shadow.get(
            "diagnostic_count", 0
        ),
        "sample_semantic_actionable_count": semantic_shadow.get(
            "actionable_advisory_count", 0
        ),
        "api_calls": 0,
        "project_writes": 0,
        "blocked_write_attempts": write_counter["attempts"],
        "protected_state_unchanged": protected_unchanged,
        "progress_errors": progress_errors,
        "progress_consistency_report": progress_report,
        "strict_configuration_errors": strict_config_errors,
        "paid_request_journal_errors": paid_request_journals["errors"],
        "uncertain_paid_requests": paid_request_journals["uncertain"],
        "paid_request_journal_record_count": len(
            paid_request_journals["records"]
        ),
        "errors": errors,
    }


def _print_human(report):
    print("=" * 70)
    print("NOVELTRANSLATOR PRODUCTION-PATH PREFLIGHT — READ ONLY")
    print("=" * 70)
    print(f"Status: {report['status']}")
    print(f"Novel: {report['novel_id']} — {report['title']}")
    print(f"Project root: {report['project_root']}")
    print(f"Profile: {report['profile']}")
    print(
        "Progress: "
        f"{report['completed_chapter_count']} completed / "
        f"{report['committed_output_count']} committed files"
    )
    print(f"Requested model: {report['requested_model']}")
    print(f"Effective model: {report['effective_model']}")
    print(
        "Pending recovery: "
        f"{report['pending_status']}"
        + (
            f" (Chapter {report['pending_chapter']})"
            if report['pending_chapter'] is not None
            else ""
        )
    )
    print(f"Pricing version: {report['pricing_version']}")
    print(f"Pricing display: {report['rate_label']}")
    print(
        "Committed QA provenance: "
        f"{report['committed_qa_provenance_mode']}"
    )
    if report["committed_qa_provenance_excluded_chapters"]:
        print(
            "Committed QA provenance exclusions: "
            + ", ".join(
                f"Chapter {chapter}"
                for chapter in report["committed_qa_provenance_excluded_chapters"]
            )
        )
    print(f"Semantic shadow: {report['semantic_shadow_mode']}")
    if report["sample_chapter"] is not None:
        print(f"Read-only QA sample chapter: {report['sample_chapter']}")
        print(
            "Sample legacy commit allowed: "
            + ("YES" if report["sample_legacy_commit_allowed"] else "NO")
        )
        print(
            "Semantic shadow changed commit decision: "
            + (
                "YES"
                if report["sample_semantic_commit_decision_changed"]
                else "NO"
            )
        )
    print(f"API calls: {report['api_calls']}")
    print(f"Project writes: {report['project_writes']}")
    print(f"Blocked write attempts: {report['blocked_write_attempts']}")
    print(
        "Protected production state unchanged: "
        + ("YES" if report["protected_state_unchanged"] else "NO")
    )
    if report["errors"]:
        print("Errors:")
        for error in report["errors"]:
            print(f"  - {error}")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Exercise hardened production runtime preparation against an actual "
            "novel project with zero provider calls and zero project writes."
        )
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--model", default=None, help="pro (default), flash, or full model ID")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_production_path_preflight(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            provider_selection=args.provider,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": PREFLIGHT_MODE,
                "status": "FAIL",
                "api_calls": 0,
                "project_writes": 0,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("NOVELTRANSLATOR PRODUCTION-PATH PREFLIGHT — READ ONLY")
            print("=" * 70)
            print("Status: FAIL")
            print(f"Error: {exc}")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
