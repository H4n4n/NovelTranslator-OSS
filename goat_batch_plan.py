"""Read-only GOAT parent authorization specification; never a paid executor."""
import argparse
import json
import math
from pathlib import Path

from chapter_library import index_chapter_library
from controlled_trial_plan import (
    _canonical_hash, _engine_runtime_identity, _immutable_inputs, _sha256_file,
)
from novel_project import load_novel_project
from production_goat_auxiliary_policy import completion_exposure
from production_lifecycle import run_due_audits, run_final_audit_if_needed
from production_paid_request_journal import inspect_paid_request_journals
from production_path_preflight import run_production_path_preflight
from production_provider_runtime import provider_identity, canonical_pending_provenance
from production_uncertain_request_disposition import verified_completed_dispositions
from translation_model_config import resolve_translation_model


MODE = "goat_bounded_parent_plan_b1"
MAX_CHAPTER_COUNT = 1_000
MAX_PHYSICAL_SENDS = 1_000_000
MAX_CREDIT_EQUIVALENT_USD = 1_000_000


class GoatBatchPlanError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise GoatBatchPlanError(message)


def _integer(value, label, maximum):
    _require(type(value) is int and 1 <= value <= maximum,
             f"{label} must be an integer in 1..{maximum}")
    return value


def _read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate JSON field in {path}")
            result[key] = value
        return result

    def invalid(_value):
        raise GoatBatchPlanError(f"nonfinite JSON number in {path}")

    def finite_float(value):
        parsed = float(value)
        _require(math.isfinite(parsed), f"nonfinite JSON number in {path}")
        return parsed

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=unique, parse_constant=invalid,
                      parse_float=finite_float)


def _snapshot(project):
    """Bind initial evidence and detect concurrent changes without a lock/write."""
    roots = (project.source_dir, project.data_dir, project.chapter_dir,
             project.pending_dir, project.qa_dir, project.audit_dir,
             project.retroactive_dir, project.backup_dir)
    manifest = {}
    for root in roots:
        _require(not root.is_symlink(), "symlinked evidence root")
        if not root.exists():
            manifest[root.relative_to(project.root).as_posix() + "/"] = None
            continue
        _require(root.is_dir(), "evidence root is not a directory")
        for path in sorted(root.rglob("*")):
            _require(not path.is_symlink(), "symlinked evidence")
            if path.is_dir():
                continue
            _require(path.is_file() and path.suffix != ".tmp", "invalid or temporary evidence")
            if path.suffix == ".json":
                _read_json(path)
            manifest[path.relative_to(project.root).as_posix()] = _sha256_file(path)
    for path in (project.config_file, project.translation_addendum_file,
                 project.translation_prompt_file, project.source_overrides_file):
        _require(not path.is_symlink(), "symlinked configuration")
        if path.is_file() and path.suffix == ".json":
            _read_json(path)
        manifest[path.relative_to(project.root).as_posix()] = _sha256_file(path)
    manifest["pause.flag"] = (_sha256_file(project.pause_file)
                              if project.pause_file.exists() else None)
    return manifest


def _audit_obligations(progress, interval, end, final):
    detached = dict(progress)
    windows = []
    phase = "startup"

    def record(start, stop):
        windows.append(dict(start_chapter=start, end_chapter=stop, phase=phase,
                            before_next_chapter=(progress["next_chapter"] if phase == "startup"
                                                 else stop + 1 if stop < final else None)))
        detached["last_audited_chapter"] = stop

    run_due_audits(progress=detached, audit_interval=interval, run_periodic_audit=record)
    phase = "after_commit"
    for chapter in range(progress["next_chapter"], end + 1):
        detached["last_completed"] = chapter
        run_due_audits(progress=detached, audit_interval=interval, run_periodic_audit=record)
    phase = "final_partial"
    run_final_audit_if_needed(progress=detached, final_chapter=final, run_periodic_audit=record)
    return dict(interval=interval, initial_audited_through=progress["last_audited_chapter"],
                windows=windows, final_novel_audit_required=True,
                publication_finalization_required=True,
                crossing_contract="existing_audit_finalization_and_historical_proof_under_B2_ledger")


def build_goat_batch_plan(novel_id, *, initial_chapter, chapter_count,
                         max_api_calls, max_credit_equivalent_usd,
                         repo_root=None, provider_selection="command_code_provider",
                         model_selection="v41-flash"):
    """Return deterministic review material, with no spending authority or writes."""
    _require(isinstance(novel_id, str) and bool(novel_id.strip()), "explicit novel required")
    start = _integer(initial_chapter, "initial_chapter", 2**31 - 1)
    count = _integer(chapter_count, "chapter_count", MAX_CHAPTER_COUNT)
    sends = _integer(max_api_calls, "max_api_calls", MAX_PHYSICAL_SENDS)
    credit = max_credit_equivalent_usd
    _require(type(credit) in (int, float) and 0 < credit <= MAX_CREDIT_EQUIVALENT_USD,
             "max_credit_equivalent_usd must be finite, positive and <= 1000000")
    _require(math.isfinite(credit), "credit-equivalent ceiling must be finite")
    credit = float(credit)
    _require(provider_selection == "command_code_provider" and model_selection == "v41-flash",
             "B1 requires command_code_provider / logical v41-flash; no fallback")
    identity = provider_identity(provider_selection, model_selection)
    capability = completion_exposure(dict(model=identity["wire_model"], reasoning_effort="low"),
                                     prompt_upper=1)
    project = load_novel_project(novel_id, repo_root=repo_root)
    before = _snapshot(project)
    progress = _read_json(project.progress_file)
    _require(isinstance(progress, dict), "progress must be an object")
    _require(type(progress.get("next_chapter")) is int and progress["next_chapter"] == start,
             "initial chapter must equal currently authorized progress.next_chapter")
    _require(type(progress.get("last_completed")) is int and progress["last_completed"] == start - 1,
             "non-contiguous last_completed")
    completed = progress.get("completed")
    _require(isinstance(completed, list) and len(completed) == start - 1
             and all(type(n) is int and n == i for i, n in enumerate(completed, 1)),
             "non-contiguous progress.completed")
    audited = progress.get("last_audited_chapter", 0)
    _require(type(audited) is int and 0 <= audited <= start - 1, "invalid audited-through marker")
    progress = {**progress, "last_audited_chapter": audited}
    end = start + count - 1
    _require(end <= min(project.config.final_chapter, 2**31 - 1),
             "insufficient remaining chapters or exhausted novel")
    chapters = index_chapter_library(_read_json(project.chapters_file),
                                    project.config.final_chapter, project.config.chapter_numbering)
    _require(all(isinstance(chapters[n].get("text"), str) and chapters[n]["text"].strip()
                 for n in range(start, end + 1)), "missing source text in requested range")
    _require(not project.pending_dir.exists() or not any(project.pending_dir.iterdir()),
             "pending evidence blocks fresh batch planning")
    outputs = list(project.chapter_dir.glob("*.md"))
    _require(all(p.stem.isdigit() for p in outputs), "invalid committed chapter filename")
    outputs.sort(key=lambda p: int(p.stem))
    _require([p.name for p in outputs] == [f"{n:04d}.md" for n in range(1, start)],
             "non-contiguous or duplicate committed chapter inventory")
    for path in project.qa_dir.glob("*.json"):
        _require(path.stem.isdigit(), "invalid committed QA filename")
        meta = _read_json(path)
        _require(isinstance(meta, dict), "invalid committed QA object")
        _require(1 <= int(path.stem) < start and ("chapter" not in meta or
                 (type(meta["chapter"]) is int and meta["chapter"] == int(path.stem))),
                 "committed QA chapter mismatch")
        if "translation_model" in meta:
            canonical = canonical_pending_provenance(meta)
            _require(all(meta[key] == value for key, value in canonical.items() if key in meta),
                     "committed QA provenance drift")
        else:
            _require("translation_provider" not in meta and "translation_provider_identity" not in meta,
                     "incomplete committed QA provenance")
    preflight = run_production_path_preflight(novel_id, repo_root=repo_root,
        model_selection=model_selection, provider_selection=provider_selection)
    _require(preflight.get("status") == "PASS", "GOAT preflight failed: " + "; ".join(preflight.get("errors", [])))
    _require(preflight.get("provider_identity") == identity
             and preflight.get("pending_status") == "requested"
             and preflight.get("pending_chapter") is None
             and preflight.get("protected_state_unchanged") is True
             and preflight.get("api_calls") == 0 and preflight.get("project_writes") == 0
             and preflight.get("blocked_write_attempts") == 0
             and not preflight.get("completed_pending_cleanup_required"), "invalid clean preflight evidence")
    inventory = verified_completed_dispositions(project, inspect_paid_request_journals(
        project.data_dir / "provider_request_journal"))
    _require(not inventory["errors"] and not inventory["uncertain"], "unresolved paid journal evidence")
    _require(all(r["status"] in {"response_recorded", "uncertain"} and r["chapter"] < start
                 for r in inventory["records"]), "incomplete or future paid journal operation")
    for summary in inventory["records"]:
        record = _read_json(project.data_dir / "provider_request_journal" / summary["file"])
        _require(record["novel_id"] == project.config.id and record["profile_id"] == project.config.profile,
                 "journal novel/profile provenance drift")
        if record["provider"] == identity["provider"]:
            _require(record["model"] == identity["wire_model"], "journal GOAT model drift")
        else:
            history = progress.get("accounting_history", [])
            _require(record["provider"] == "direct_deepseek" and history
                     and record["chapter"] <= history[0]["closed_after_completed_chapter"],
                     "journal provider incompatible with verified accounting epoch")
            _require(resolve_translation_model(record["model"]).model == record["model"],
                     "historical direct journal model drift")
    for path in project.audit_dir.glob("*.checkpoint.json"):
        checkpoint = _read_json(path)
        window = checkpoint["audit_window"]
        first, last = window["start_chapter"], window["end_chapter"]
        _require(type(first) is int and type(last) is int and 1 <= first <= last <= audited
                 and checkpoint.get("status") == "completed"
                 and path.name == f"audit_{first:04d}_{last:04d}.checkpoint.json"
                 and (project.audit_dir / f"audit_{first:04d}_{last:04d}.json").is_file(),
                 "unfinished or invalid audit checkpoint requires reconciliation")
    # Historical UNCERTAIN is accepted only by the verified inventory above.
    # Completion is owned by an outer audit intent or a re-proven standalone witness.
    referenced = set()
    authorizations = {}
    standalone_authorizations = {}
    for path in project.audit_dir.glob("*.finalization.json"):
        owned_plan = _read_json(path)["transaction_plan"]
        referenced.add(owned_plan["transaction_id"])
        slot = _canonical_hash({k: owned_plan["request"][k]
            for k in ("novel_id", "audit_label", "through_chapter")})
        authorizations[slot + ".authorization.json"] = owned_plan
    if (project.retroactive_dir / "standalone_lineage").exists():
        from goat_periodic_audit_finalize_plan import local_module
        from production_standalone_lineage import verified_events, witnesses, slot_for, bound_authorization
        module = local_module(project)
        verified_events(module)  # Original source/profile/policy, chronology, spans and durable receipts.
        for witness in witnesses(module):
            owned_plan = witness["plan"]
            name = slot_for(owned_plan) + ".authorization.json"
            _require(name not in authorizations, "duplicate historical writer authorization slot")
            authorizations[name] = owned_plan
            referenced.add(owned_plan["transaction_id"])
            standalone_authorizations[name] = bound_authorization(
                witness["authorization"], witness["witness_sha256"])
    for path in (project.retroactive_dir / "transactions").glob("*.json"):
        value = _read_json(path)
        if path.name.endswith(".authorization.json"):
            # Writer receipts bind a complete plan hash, not a transaction_id.
            # The ordinary preflight above still owns the historical proof.
            from historical_reconciliation import WRITER_AUTHORIZATION_REVISION
            fields = {"plan_sha256", "accepted_change_sha256", "historical_evidence_sha256",
                "inventory_sha256", "reservations_sha256", "prior_intents_sha256", "authorization_sha256"}
            owned_plan = authorizations.get(path.name)
            witnessed = standalone_authorizations.get(path.name)
            expected_fields = fields | {"revision"}
            if witnessed is not None:
                expected_fields.add("standalone_witness_sha256")
                _require(value == witnessed, "standalone writer authorization witness mismatch")
            _require(owned_plan is not None and set(value) == expected_fields
                and value["revision"] == WRITER_AUTHORIZATION_REVISION
                and all(isinstance(value[k], str) and len(value[k]) == 64
                        and set(value[k]) <= set("0123456789abcdef") for k in fields)
                and value["plan_sha256"] == _canonical_hash(owned_plan)
                and value["authorization_sha256"] == _canonical_hash(
                    {k: v for k, v in value.items() if k != "authorization_sha256"}),
                "invalid or orphan historical writer authorization")
            continue
        _require(value.get("transaction_id") in referenced, "orphan historical transaction")
        if path.name.endswith(".state.json"):
            _require(value.get("state") == "FINALIZED", "incomplete historical transaction")
    runtime = _engine_runtime_identity(project.config.profile)
    dependency_file = Path(__file__).resolve().with_name("pyproject.toml")
    _require(dependency_file.is_file(), "missing dependency policy")
    audit = _audit_obligations(progress, project.config.audit_interval, end, project.config.final_chapter)
    dependency_hash = _sha256_file(dependency_file)
    binding = dict(mode=MODE, novel_id=project.config.id, project_root=str(project.root.resolve()),
        source_language=project.config.source_language, target_language=project.config.target_language,
        profile=project.config.profile, initial_chapter=start, end_chapter=end, chapter_count=count,
        authorized_next_chapter=start, final_chapter=project.config.final_chapter,
        max_api_calls=sends, max_credit_equivalent_usd=credit, provider_identity=identity,
        capability_exposure=capability, runtime_identity=runtime,
        dependency_policy_sha256=dependency_hash, immutable_inputs=_immutable_inputs(project),
        pricing=dict(off_peak=preflight["off_peak_pricing"], peak=preflight["peak_pricing"]),
        source_range_sha256=_canonical_hash([chapters[n] for n in range(start, end + 1)]),
        initial_evidence=before, audit_obligations=audit,
        transition_policy="proved_monotonic_B2_state_evolution_no_budget_renewal",
        recovery_policy="existing_bounded_operations_only_no_ambiguous_resend_or_migration",
        publication_policy="final_audit_QA_historical_reconciliation_and_EPUB_freshness_gate")
    _require(before == _snapshot(project), "project evidence changed during planning")
    _require(project.config == load_novel_project(novel_id, repo_root=repo_root).config,
             "configuration changed during planning")
    _require(dependency_hash == _sha256_file(dependency_file)
             and capability == completion_exposure(dict(model=identity["wire_model"], reasoning_effort="low"), prompt_upper=1)
             and runtime == _engine_runtime_identity(project.config.profile)
             and identity == provider_identity(provider_selection, model_selection),
             "runtime or policy changed during planning")
    fingerprint = _canonical_hash(binding)
    return dict(mode=MODE, status="READ_ONLY_PLAN", paid_execution_authorized=False,
        provider_invoice_claimed=False, api_calls=0, project_writes=0,
        execution_blockers=["B2_DURABLE_LEDGER_AND_EXPLICIT_EXECUTION_REQUIRED"],
        authorization_binding=binding, state_fingerprint=fingerprint,
        approval_token=_canonical_hash(dict(mode=MODE, state_fingerprint=fingerprint)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--initial-chapter", required=True, type=int)
    parser.add_argument("--chapter-count", required=True, type=int)
    parser.add_argument("--max-api-calls", required=True, type=int)
    parser.add_argument("--max-credit-equivalent-usd", required=True, type=float)
    parser.add_argument("--provider", default="command_code_provider")
    parser.add_argument("--model", default="v41-flash")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = build_goat_batch_plan(args.novel, initial_chapter=args.initial_chapter,
            chapter_count=args.chapter_count, max_api_calls=args.max_api_calls,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd, repo_root=args.repo_root,
            provider_selection=args.provider, model_selection=args.model)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(json.dumps(dict(mode=MODE, status="BLOCKED", paid_execution_authorized=False,
            provider_invoice_claimed=False, api_calls=0, project_writes=0, error=str(exc)), sort_keys=True))
        return 2
    if not args.json:
        print("GOAT BATCH PLAN — READ ONLY; no spending authorization")
        bound = plan["authorization_binding"]
        print(f"Novel {bound['novel_id']}; chapters {bound['initial_chapter']}..{bound['end_chapter']}")
        print(f"Lifetime caps: {bound['max_api_calls']} physical sends; "
              f"{bound['max_credit_equivalent_usd']!r} USD credit equivalents")
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
