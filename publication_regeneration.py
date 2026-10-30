"""Local-only, approval-bound publication generations for unchanged source spines.

Existing stage owners apply their own plans. Nothing replaces the original final
corpus or EPUB. A generation becomes current only after revalidation and an atomic
pointer publication under the novel's existing cooperative writer lock.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import apply_normalization_plan as normalization
import apply_title_resolution_plan as titles
import build_publication_chapters as cleanup
import final_editorial as editorial
from historical_reconciliation import (
    plain, read_object, read_value, require, source_inventory, publication_revisions,
)
from retroactive_span_planning import canonical_hash, digest
from novel_project import select_project
from noveltranslator_core import atomic_write_json, translation_commit_qa
from production_run_lock import production_run_lock
from translation_profile import load_translation_profile

REVISION = "source-bound-publication-v1"
APPROVALS = {
    "normalization_plan": "config/publication_regeneration/normalization_plan.json",
    "title_plan": "config/publication_regeneration/title_plan.json",
    "editorial_authorization": "config/final_editorial.json",
}
AUTHORIZATION = "config/publication_regeneration.json"
POINTER = "output/publication_current.json"
GENERATIONS = "output/publication_generations"


def _sha(value):
    require(isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value), "INVALID_PROVENANCE_HASH")
    return value


def _bytes(path):
    return plain(path).read_bytes()


def _plain_tree(directory):
    """Reject links before any existing stage owner can traverse its workspace."""
    plain(directory)
    if directory.is_dir():
        for entry in directory.iterdir():
            plain(entry)
            if entry.is_dir():
                _plain_tree(entry)


def _payloads(directory, count):
    plain(directory)
    expected = [f"{n:04d}.md" for n in range(1, count + 1)]
    require(directory.is_dir() and sorted(p.name for p in directory.iterdir()) == expected,
            "CORPUS_INVENTORY_DRIFT")
    return [(name, _bytes(directory / name)) for name in expected]


def _immutable_json(path, value):
    """Atomic creation under the caller's novel lock; divergent residue blocks."""
    plain(path)
    if path.exists():
        require(read_object(path) == value, "GENERATION_EVIDENCE_DRIFT")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, value)


@dataclass(frozen=True)
class PublicationWorkspace:
    """Keep all novel state paths at the real novel; redirect only final outputs."""
    project: object
    publication_final_dir: Path

    def __getattr__(self, name):
        return getattr(self.project, name)


def _require_current_qa(project, units, payloads, glossary):
    profile = load_translation_profile(project.config.profile)
    for n, (_, raw) in enumerate(payloads, 1):
        text = raw.decode("utf-8-sig")
        qa = translation_commit_qa(units[n]["text"], text, glossary["entries"],
            profile.high_priority_categories, source_language=project.config.source_language, chapter_number=n)
        require(text.strip() and "[TRANSLATION REVIEW REQUIRED]" not in text and qa["commit_allowed"],
                f"CURRENT_CHAPTER_QA_BLOCKED_{n:04d}")


def source_snapshot(project, whole_history_sha256):
    """Reprove audit/transaction evidence and bind the explicitly reviewed report.

    The hash-selected whole-history report is an existing local decision, not a
    new semantic audit. The downstream authorization must explicitly approve it.
    No provider journal, credentials, or production runtime initialization.
    """
    from chapter_library import chapter_production_index
    from novel_project import NovelConfig
    from production_periodic_audit_finalization import transaction_proof
    require(not project.is_legacy and project.config.source_type == "txt",
            "REGENERATION_REQUIRES_TXT_IDENTITY_SPINE")
    require(project.config.source_language in {"zh", "ko"} and project.config.target_language == "en", "UNSUPPORTED_REGENERATION_LANGUAGE")
    require(NovelConfig.from_dict(read_object(project.config_file)) == project.config, "SELECTED_CONFIG_DRIFT")
    units = read_value(project.chapters_file)
    require(isinstance(units, list) and units, "MISSING_SOURCE_INVENTORY")
    chapter_map = {chapter_production_index(u): u for u in units}
    count = project.config.final_chapter
    numbers = list(range(1, count + 1))
    require(len(units) == count and list(chapter_map) == numbers, "REPARTITION_REQUIRES_SEPARATE_APPROVAL")
    progress = read_object(project.progress_file)
    require(all(type(n) is int for n in progress.get("completed", []))
            and type(progress.get("last_completed")) is int and type(progress.get("last_audited_chapter")) is int
            and progress.get("completed") == numbers and progress.get("last_completed") == count
            and progress.get("last_audited_chapter") == count and progress.get("next_chapter") == count + 1, "INCOMPLETE_COMMITTED_AUDIT_BOUNDARY")
    require(not any(plain(project.pending_dir).glob("*")), "PENDING_WORK_REQUIRES_RECONCILIATION")
    glossary = read_object(project.glossary_file)
    history = read_object(project.audit_history_file)
    payloads = _payloads(project.chapter_dir, count)
    source = _bytes(project.source_txt)
    require(all(u.get("source_document_sha256") == digest(source) for u in units),
            "ORIGINAL_SOURCE_IDENTITY_DRIFT")
    profile = load_translation_profile(project.config.profile)
    _require_current_qa(project, chapter_map, payloads, glossary)
    revisions = publication_revisions(project)
    require(revisions, "MISSING_SOURCE_BOUND_AUDIT_HISTORY")
    report_path = project.audit_dir / "whole_history" / (_sha(whole_history_sha256) + ".json")
    report = read_object(report_path)
    require(canonical_hash(report) == whole_history_sha256 and report.get("status") == "CHECKED"
            and report.get("deferred_chapters") == 0, "WHOLE_HISTORY_APPROVAL_NOT_READY")
    require(report.get("revision") == "aut2-history-evidence-v1"
            and report.get("scope") == "final_whole_committed_history"
            and all(isinstance(row.get("decisions"), list) and all(d.get("outcome") == "already_current" for d in row["decisions"]) for row in report["chapters"])
            and report.get("already_current_chapters") == sum(bool(row["decisions"]) for row in report["chapters"])
            and report.get("unchanged_chapters") == sum(not row["decisions"] for row in report["chapters"]),
            "CONTRADICTORY_WHOLE_HISTORY_REPORT")
    require(report.get("novel_id") == project.config.id and report.get("audit_boundary") == count
            and report.get("examined_chapters") == count, "WHOLE_HISTORY_BOUNDARY_DRIFT")
    require(report.get("source_inventory_sha256") == canonical_hash(source_inventory(chapter_map))
            and report.get("glossary_revision_sha256") == canonical_hash(glossary), "WHOLE_HISTORY_SOURCE_DRIFT")
    require([(r["chapter"], r["english_sha256"], r["source_sha256"]) for r in report["chapters"]] ==
            [(n, digest(raw), digest(chapter_map[n]["text"].encode())) for n, (_, raw) in enumerate(payloads, 1)],
            "WHOLE_HISTORY_CHAPTER_DRIFT")
    module = SimpleNamespace(ACTIVE_NOVEL_CONFIG=project.config, ACTIVE_PROFILE=profile,
        AUDIT_DIR=project.audit_dir, OUTPUT_DIR=project.chapter_dir, RETROACTIVE_DIR=project.retroactive_dir,
        BACKUP_DIR=project.backup_dir, PROGRESS_FILE=project.progress_file, chapter_map=chapter_map,
        progress=progress)
    blobs = {digest(raw): raw for _, raw in payloads}
    intents = []
    from historical_reconciliation import audit_intent
    for path in sorted(plain(project.audit_dir).glob("*.finalization.json")):
        intent = audit_intent(module, path)
        intents.append(intent)
        for item in intent["transaction_plan"]["chapters"]:
            if item["edits"]:
                backup = project.backup_dir / ("retroactive_" + intent["transaction_plan"]["transaction_id"]) / "chapters_before" / f"{item['chapter']:04d}.md"
                raw = _bytes(backup)
                require(digest(raw) == item["before_sha256"], "HISTORICAL_BACKUP_DRIFT")
                blobs[digest(raw)] = raw
    latest, previous_end, transactions = {}, 0, []
    evidence = {}
    for intent in intents:
        require(intent["start"] == previous_end + 1, "DISCONNECTED_AUDIT_HISTORY")
        previous_end = intent["end"]
        plan = intent["transaction_plan"]
        after = {}
        import retroactive_transactions as tx
        for item in plan["chapters"]:
            n = item["chapter"]
            require(n not in latest or latest[n] == item["before_sha256"], "DISCONNECTED_HISTORICAL_BYTE_CHAIN")
            require(item["before_sha256"] in blobs, "HISTORICAL_BEFORE_WITNESS_MISSING")
            after[n] = tx.transform_bytes(blobs[item["before_sha256"]], item["edits"])
            latest[n] = item["after_sha256"]
        module.progress = intent["baseline"]["progress"]
        proof = transaction_proof(module, intent["start"], intent["end"], intent["baseline"]["glossary"],
            intent["post"]["glossary"], intent["record"]["changes"], chapter_bytes=after)
        require(proof["state"] and proof["state"]["state"] == "FINALIZED", "UNFINISHED_HISTORICAL_TRANSACTION")
        checkpoint = project.audit_dir / f"audit_{intent['start']:04d}_{intent['end']:04d}.checkpoint.json"
        require(digest(_bytes(checkpoint)) == intent["checkpoint_sha256"], "AUDIT_CHECKPOINT_DRIFT")
        require(read_object(project.audit_dir / f"audit_{intent['start']:04d}_{intent['end']:04d}.json") == intent["record"], "AUDIT_RECORD_DRIFT")
        require(history["audits"][:len(intent["post"]["history"]["audits"])] == intent["post"]["history"]["audits"], "AUDIT_HISTORY_DRIFT")
        transactions.append({"transaction_id": plan["transaction_id"], "intent_sha256": intent["intent_sha256"]})
        for name, sha in proof["evidence"].items():
            if sha:
                path = Path(name)
                if path.suffix == ".json":
                    read_object(path)  # Reject duplicate/contradictory JSON even if checksums match.
                evidence[path.relative_to(project.root).as_posix()] = sha
    require(previous_end == count and transactions == report["transactions"], "WHOLE_HISTORY_AUDIT_DRIFT")
    require(all(digest(payloads[n-1][1]) == sha for n, sha in latest.items()), "CURRENT_PROSE_OUTSIDE_AUDIT_CHAIN")
    for key, path in (("progress", project.progress_file), ("glossary", project.glossary_file),
                      ("issues", project.issues_file), ("history", project.audit_history_file)):
        require(read_object(path) == intents[-1]["post"][key], "FINAL_STATE_OUTSIDE_AUDIT_CHAIN")
    files = [project.config_file, project.chapters_file, project.source_txt, project.progress_file,
             project.glossary_file, project.issues_file, project.continuity_file, project.audit_history_file, report_path]
    optional_policy = {}
    for name in ("translation_addendum.txt", "source_overrides.json"):
        path = plain(project.root / name)
        if path.exists() and path.suffix == ".json":
            read_object(path)
        optional_policy[name] = digest(_bytes(path)) if path.exists() else None
    files += list(project.audit_dir.glob("*.json"))
    files += list((project.root / "output/publication_freshness").glob("*.json"))
    for path in files:
        if path.suffix == ".json":
            read_value(path)
        evidence[path.relative_to(project.root).as_posix()] = digest(_bytes(path))
    profile_dir = Path(__file__).parent / "profiles" / project.config.profile
    return {"revision": REVISION, "novel_id": project.config.id, "config": project.config.to_dict(),
            "source_inventory_sha256": canonical_hash(source_inventory(chapter_map)),
            "chapters": cleanup.framed_corpus_fingerprint(payloads), "evidence": dict(sorted(evidence.items())),
            "profile": {p.name: digest(_bytes(p)) for p in sorted(profile_dir.iterdir()) if p.is_file()},
            "whole_history_sha256": whole_history_sha256, "transactions": transactions, "novel_policy": optional_policy}


def _inputs(project):
    authorization = read_object(project.root / AUTHORIZATION)
    require(authorization.get("revision") == REVISION and authorization.get("novel_id") == project.config.id
            and authorization.get("enabled") is True
            and authorization.get("finalization") == "committed-identity-v1"
            and type(authorization.get("allow_manual_approved")) is bool, "REGENERATION_NOT_AUTHORIZED")
    snapshot = source_snapshot(project, authorization["whole_history_sha256"])
    require(canonical_hash(snapshot) == authorization.get("source_snapshot_sha256"), "SOURCE_APPROVAL_STALE")
    approvals = {}
    for key, name in APPROVALS.items():
        path = project.root / name
        read_object(path)
        approvals[key] = digest(_bytes(path))
        require(approvals[key] == authorization.get(key + "_sha256"), "MISSING_OR_STALE_" + key.upper())
    inventory = project.root / "config/publication_regeneration/normalization_inventory.json"
    if inventory.exists():
        read_object(inventory)
        approvals["normalization_inventory"] = digest(_bytes(inventory))
    # Bind builders and validators as well as approvals; code drift never reuses a generation.
    runtime = {p.name: digest(_bytes(p)) for p in sorted(Path(__file__).parent.glob("*.py"))}
    return {"revision": REVISION, "source": snapshot, "approvals": approvals,
            "authorization_sha256": digest(_bytes(project.root / AUTHORIZATION)), "runtime": runtime}


def _stage_previews(project, final_dir, inputs):
    manual = read_object(project.root / AUTHORIZATION)["allow_manual_approved"]
    plan = read_object(project.root / APPROVALS["normalization_plan"])
    inventory = project.root / "config/publication_regeneration/normalization_inventory.json"
    normal = normalization.prepare_application(baseline_dir=final_dir / "chapters", plan=plan,
        plan_sha256=inputs["approvals"]["normalization_plan"], project_id=project.config.id,
        allow_manual_approved=manual, inventory_path=inventory if inventory.exists() else None)
    require(not normal.manual_record_count or normal.manual_occurrence_count > 0, "MANUAL_NORMALIZATION_NOT_AUTHORIZED")
    require(normal.review_record_count == 0, "UNRESOLVED_NORMALIZATION_APPROVAL")
    return normal


def _title_preview(project, final_dir, inputs):
    plan = read_object(project.root / APPROVALS["title_plan"])
    require(not any(d["decision"] in {"REVIEW_REQUIRED", "STRUCTURAL_ONLY"} for d in plan["title_decisions"]), "UNRESOLVED_TITLE_APPROVAL")
    preview = titles.prepare_application(final_dir / "normalized_chapters", plan, inputs["approvals"]["title_plan"],
        project.config.id, read_object(project.root / AUTHORIZATION)["allow_manual_approved"])
    require(not preview.blocked_manual_anchor_count and not preview.recovery_review_targets, "TITLE_APPROVAL_NOT_EXECUTABLE")
    return preview


def verify_generation_corpus(project, final_dir):
    """Independently reprove every stage, including the production ancestry."""
    final_dir = plain(final_dir)
    _plain_tree(final_dir)
    inputs = _inputs(project)
    identity = canonical_hash(inputs)
    require(final_dir.resolve() == (project.root / GENERATIONS / identity / "final").resolve(), "FOREIGN_GENERATION_ROOT")
    require(read_object(final_dir.parent / "intent.json") == inputs, "GENERATION_INTENT_DRIFT")
    proof = read_object(final_dir / "reports/source_bound_provenance.json")
    require(proof == inputs, "GENERATION_SOURCE_PROVENANCE_STALE")
    count = project.config.final_chapter
    require(_payloads(final_dir / "chapters", count) == _payloads(project.chapter_dir, count), "FINAL_CORPUS_SOURCE_DRIFT")
    from build_finalization_corpus import inventory_production_chapters
    require(read_object(final_dir / "manifest.json") == {"schema_version": 1, "project_id": project.config.id,
        "migration_kind": "committed-identity-v1", "production_inventory": inventory_production_chapters(project.chapter_dir, count)[0]}, "FINALIZATION_MANIFEST_DRIFT")
    # Reports must have one unambiguous interpretation before owner validation.
    for path in plain(final_dir / "reports").glob("*.json"):
        read_object(path)
    normal = _stage_previews(project, final_dir, inputs)
    verification = normalization.verify_existing_application(normal, output_dir=final_dir / "normalized_chapters",
        report_json_path=final_dir / "reports/normalization_application.json")
    actual_report = read_object(final_dir / "reports/normalization_application.json")
    expected_report = normalization.build_application_report(normal, verification)
    require(normalization._normalization_report_semantics(actual_report) == normalization._normalization_report_semantics(expected_report), "NORMALIZATION_REPORT_DRIFT")
    title = _title_preview(project, final_dir, inputs)
    verification = titles.verify_existing_application(title, final_dir / "titled_chapters", final_dir / "reports/title_application.json")
    require(read_object(final_dir / "reports/title_application.json") == titles.build_application_report(title, verification), "TITLE_REPORT_DRIFT")
    presentation = cleanup.build_plan(final_dir / "titled_chapters", count)
    require(_payloads(final_dir / "publication_chapters", count) ==
            [(u.source_path.name, u.output_bytes) for u in presentation.units], "PUBLICATION_CORPUS_DRIFT")
    require(read_object(final_dir / "reports/publication_cleanup.json") == cleanup.build_report(presentation), "CLEANUP_REPORT_DRIFT")
    workspace = PublicationWorkspace(project, final_dir)
    plan = editorial.build_plan_report(workspace)
    require(plan["ready"], "EDITORIAL_APPROVAL_NOT_READY")
    require(read_object(final_dir / "reports/final_editorial_plan.json") == plan, "EDITORIAL_PLAN_DRIFT")
    manifest = read_object(project.root / APPROVALS["editorial_authorization"])
    require(manifest.get("novel") == project.config.id, "FOREIGN_EDITORIAL_AUTHORIZATION")
    outputs, _, guards = editorial.simulate(manifest, _payloads(final_dir / "publication_chapters", count))
    require(_payloads(final_dir / "editorial_chapters", count) == outputs, "EDITORIAL_CORPUS_DRIFT")
    from chapter_library import chapter_production_index
    units = {chapter_production_index(u): u for u in read_value(project.chapters_file)}
    _require_current_qa(project, units, outputs, read_object(project.glossary_file))
    expected = editorial._build_apply_report(workspace, plan, inputs["approvals"]["editorial_authorization"],
        cleanup.framed_corpus_fingerprint(outputs), guards)
    require(read_object(final_dir / "reports/final_editorial_apply.json") == expected, "EDITORIAL_APPLY_DRIFT")
    return inputs


def _regenerate_locked(project):
    from publication_readiness import require_publication_ready
    from build_finalization_corpus import build_identity_finalization_corpus
    import build_publication_epub as epub
    from production_run_lock import held_production_run_lock
    held_production_run_lock(project.root)
    inputs = _inputs(project)
    _validate_previous_pointer(project)
    identity = canonical_hash(inputs)
    generation = plain(project.root / GENERATIONS / identity)
    final_dir = generation / "final"
    workspace = PublicationWorkspace(project, final_dir)
    # Existing EPUB reuse must fail before any stage/report write. Archive
    # integrity alone cannot authorize certification or pointer promotion.
    if plain(epub.publication_paths(workspace)["output"]).exists():
        require_publication_ready(workspace)
    _plain_tree(generation)
    generation.mkdir(parents=True, exist_ok=True)
    _immutable_json(generation / "intent.json", inputs)
    count = project.config.final_chapter
    if not final_dir.exists():
        build_identity_finalization_corpus(project, final_dir)
    require(_payloads(final_dir / "chapters", count) == _payloads(project.chapter_dir, count), "FINAL_CORPUS_SOURCE_DRIFT")
    reports = final_dir / "reports"
    normal = _stage_previews(project, final_dir, inputs)
    if not (final_dir / "normalized_chapters").exists():
        normalization.publish_normalized_corpus(normal, output_dir=final_dir / "normalized_chapters",
            report_json_path=reports / "normalization_application.json", report_md_path=reports / "normalization_application.md")
    normalization.verify_existing_application(normal, output_dir=final_dir / "normalized_chapters", report_json_path=reports / "normalization_application.json")
    title = _title_preview(project, final_dir, inputs)
    if not (final_dir / "titled_chapters").exists():
        titles.publish_titled_corpus(title, final_dir / "titled_chapters", reports / "title_application.json", reports / "title_application.md")
    titles.verify_existing_application(title, final_dir / "titled_chapters", reports / "title_application.json")
    presentation = cleanup.build_plan(final_dir / "titled_chapters", count)
    if not (final_dir / "publication_chapters").exists():
        cleanup.publish_plan(presentation, final_dir / "publication_chapters", reports / "publication_cleanup.json", reports / "publication_cleanup.md")
    plan = editorial.build_plan_report(workspace)
    require(plan["ready"], "EDITORIAL_APPROVAL_NOT_READY")
    _immutable_json(reports / "final_editorial_plan.json", plan)
    md = reports / "final_editorial_plan.md"
    expected_md = editorial.render_plan_markdown(plan).encode()
    if md.exists():
        require(_bytes(md) == expected_md, "EDITORIAL_PLAN_MARKDOWN_DRIFT")
    else:
        from noveltranslator_core import atomic_write_bytes
        atomic_write_bytes(md, expected_md)
    if not (final_dir / "editorial_chapters").exists():
        editorial.apply_ready_plan(workspace)
    _immutable_json(final_dir / "glossary.json", read_object(project.glossary_file))
    _immutable_json(reports / "source_bound_provenance.json", inputs)
    verify_generation_corpus(project, final_dir)
    output = epub.publication_paths(workspace)["output"]
    if not output.exists():
        epub.build_publication_epub(workspace)
    validation = _verify_epub(workspace)
    require(_inputs(project) == inputs, "SOURCE_CHANGED_DURING_REGENERATION")
    pointer = {"revision": REVISION, "novel_id": project.config.id, "generation": identity,
               "epub_sha256": digest(_bytes(output))}
    require_publication_ready(workspace)
    # This is the only replaceable publication artifact: old generation bytes stay.
    path = plain(project.root / POINTER)
    if path.exists():
        previous = read_object(path)
        require(set(previous) == {"revision", "novel_id", "generation", "epub_sha256"}
                and previous.get("revision") == REVISION and previous.get("novel_id") == project.config.id,
                "CONTRADICTORY_CURRENT_POINTER")
        _sha(previous.get("generation")); _sha(previous.get("epub_sha256"))
    if not path.exists() or read_object(path) != pointer:
        atomic_write_json(path, pointer)
    return {"status": "CURRENT", "generation": identity, "epub": str(output),
            "content_document_count": validation["content_document_count"], "provider_calls": 0}


def regenerate(project):
    plain(project.root)
    with production_run_lock(project.root):
        return _regenerate_locked(project)


def _verify_epub(workspace):
    """Archive/report integrity only; never a CURRENT/readiness authority."""
    import build_publication_epub as epub
    paths = epub.publication_paths(workspace)
    corpus = epub.load_publication_corpus(workspace)
    validation = epub.verify_epub_archive(paths["output"], corpus)
    expected = epub.build_epub_report(corpus, paths["output"], _bytes(paths["output"]), validation, corpus.fingerprint)
    require(read_object(paths["report_json"]) == expected, "EPUB_REPORT_DRIFT")
    require(_bytes(paths["report_md"]) == epub.render_markdown_report(expected).encode(), "EPUB_MARKDOWN_DRIFT")
    return validation


def _validate_previous_pointer(project):
    """An old pointer may select stale input, but cannot contradict its own bytes."""
    path = plain(project.root / POINTER)
    if not path.exists():
        return
    value = read_object(path)
    require(set(value) == {"revision", "novel_id", "generation", "epub_sha256"}
            and value["revision"] == REVISION and value["novel_id"] == project.config.id,
            "CONTRADICTORY_CURRENT_POINTER")
    identity = _sha(value["generation"])
    final_dir = plain(project.root / GENERATIONS / identity / "final")
    original = read_object(final_dir.parent / "intent.json")
    require(canonical_hash(original) == identity and original["source"]["novel_id"] == project.config.id
            and read_object(final_dir / "reports/source_bound_provenance.json") == original,
            "CONTRADICTORY_PREVIOUS_GENERATION")
    _validated_previous_source(project, original, final_dir)
    workspace = PublicationWorkspace(project, final_dir)
    import build_publication_epub as epub
    output = epub.publication_paths(workspace)["output"]
    require(digest(_bytes(output)) == _sha(value["epub_sha256"]), "CONTRADICTORY_PREVIOUS_EPUB")
    _verify_epub(workspace)


def current_generation(project):
    """Read-only; a pointer or self-consistent editorial corpus grants no freshness."""
    path = plain(project.root / POINTER)
    if not path.exists():
        return {"status": "NOT_VERIFIED", "reason_code": "NO_SOURCE_BOUND_GENERATION"}
    stale_artifact = None
    try:
        pointer = read_object(path)
        require(set(pointer) == {"revision", "novel_id", "generation", "epub_sha256"}
                and pointer.get("revision") == REVISION and pointer.get("novel_id") == project.config.id,
                "FOREIGN_CURRENT_POINTER")
        identity = _sha(pointer.get("generation"))
        final_dir = plain(project.root / GENERATIONS / identity / "final")
        stale_artifact = {"path": (Path(GENERATIONS) / identity / "final/epub" / (project.config.id + ".epub")).as_posix(),
                          "status": "STALE"}
        inputs = verify_generation_corpus(project, final_dir)
        require(canonical_hash(inputs) == identity, "GENERATION_IDENTITY_DRIFT")
        import build_publication_epub as epub
        workspace = PublicationWorkspace(project, final_dir)
        output = epub.publication_paths(workspace)["output"]
        require(digest(_bytes(output)) == pointer.get("epub_sha256"), "GENERATION_EPUB_DRIFT")
        _verify_epub(workspace)
        require(_inputs(project) == inputs, "SOURCE_CHANGED_DURING_VERIFICATION")
        from publication_readiness import require_publication_ready
        require_publication_ready(workspace)
        return {"status": "CURRENT", "generation": identity, "artifact": {
            "path": output.relative_to(project.root).as_posix(), "sha256": pointer["epub_sha256"], "status": "CURRENT"}}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {"status": "STALE", "reason_code": "SOURCE_BOUND_GENERATION_REQUIRES_REVALIDATION",
                "blocker": _blocker(exc), **({"artifact": stale_artifact} if stale_artifact else {}),
                "next_safe_action": "Preserve outputs; revalidate source, whole-history and exact stage approvals."}


def _blocker(exc):
    """Bounded diagnostics; never expose a validator's expected prose anchors."""
    message = str(exc)
    if message.startswith("AUT2: ") and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in message[6:]):
        result = {"reason_code": message[6:]}
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.filename:
            result["file"] = Path(cause.filename).name
        return result
    if isinstance(exc, OSError):
        return {"reason_code": "MISSING_OR_UNREADABLE_PREREQUISITE", "file": Path(exc.filename).name if exc.filename else None}
    for kind, stage in ((normalization.NormalizationError, "NORMALIZATION"),
                        (titles.TitleApplicationError, "TITLE"), (editorial.FinalEditorialError, "EDITORIAL")):
        if isinstance(exc, kind):
            return {"reason_code": stage + "_PLAN_OR_CORPUS_INVALID", "dependency": stage.lower() + " approval and exact input corpus"}
    return {"reason_code": "INVALID_REGENERATION_PREREQUISITE", "error_type": type(exc).__name__}



# Advisory D2 operations deliberately do not participate in CURRENT authority.
_DIAGNOSTIC_ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError, AttributeError)


def _history_source(project):
    """Bind the offline history owner to the supported, exact original TXT."""
    from novel_project import NovelConfig
    from chapter_library import index_chapter_library
    require(not project.is_legacy and project.config.source_type == "txt",
            "REGENERATION_REQUIRES_TXT_IDENTITY_SPINE")
    require(project.config.source_language in {"zh", "ko"} and project.config.target_language == "en",
            "UNSUPPORTED_REGENERATION_LANGUAGE")
    require(NovelConfig.from_dict(read_object(project.config_file)) == project.config, "SELECTED_CONFIG_DRIFT")
    units = read_value(project.chapters_file)
    chapter_map = index_chapter_library(units, project.config.final_chapter, project.config.chapter_numbering)
    numbers = list(range(1, project.config.final_chapter + 1))
    require(list(chapter_map) == numbers and len(units) == len(numbers), "REPARTITION_REQUIRES_SEPARATE_APPROVAL")
    source_sha = digest(_bytes(project.source_txt))
    require(all(u.get("source_document_sha256") == source_sha for u in units), "ORIGINAL_SOURCE_IDENTITY_DRIFT")
    progress = read_object(project.progress_file)
    require(all(type(n) is int for n in progress.get("completed", []))
            and all(type(progress.get(k)) is int for k in ("last_completed", "last_audited_chapter", "next_chapter"))
            and progress.get("completed") == numbers and progress.get("last_completed") == len(numbers)
            and progress.get("last_audited_chapter") == len(numbers)
            and progress.get("next_chapter") == len(numbers) + 1, "INCOMPLETE_COMMITTED_AUDIT_BOUNDARY")
    require(not any(plain(project.pending_dir).glob("*")), "PENDING_WORK_REQUIRES_RECONCILIATION")
    # local_module's compatibility JSON loader accepts duplicate keys and
    # UTF-16/32. Reject those before it can supply ambiguous proof inputs to the
    # existing report writer. No provider journals or credentials are traversed.
    files = [project.glossary_file, project.issues_file, project.continuity_file, project.audit_history_file]
    files += sorted(plain(project.audit_dir).glob("*.json"))
    files += sorted(plain(project.retroactive_dir / "transactions").glob("*.json"))
    for path in files:
        try:
            read_object(path)
        except _DIAGNOSTIC_ERRORS as exc:
            exc.filename = str(path)
            raise


def _standalone_present(project):
    folder = plain(project.retroactive_dir / "standalone_lineage")
    return folder.exists() and any(folder.iterdir())


def recertify_history(project):
    """Save only the existing immutable report, under the existing writer lock.

    The persistent lock rendezvous is the only non-report filesystem artifact.
    Loading local_module installs NO_SEND/runtime-initializer tripwires.
    A successful local check still needs explicit human publication review.
    """
    from goat_periodic_audit_finalize_plan import local_module
    from historical_reconciliation import save_final_consistency
    plain(project.root)
    with production_run_lock(project.root):
        _history_source(project)
        report = save_final_consistency(local_module(project))
        sha = canonical_hash(report)
        unsupported = _standalone_present(project)
        return {"status": "BLOCKED" if unsupported else "REVIEW_REQUIRED",
                "reason_code": "UNSUPPORTED_STANDALONE_REGENERATION" if unsupported else
                    "HISTORICAL_DEFERRALS_REQUIRE_REVIEW" if report["deferred_chapters"] else
                    "WHOLE_HISTORY_REPORT_REQUIRES_HUMAN_REVIEW",
                "report_path": "output/audits/whole_history/" + sha + ".json",
                "report_sha256": sha, "history_status": report["status"],
                "examined_chapters": report["examined_chapters"],
                "deferred_chapters": report["deferred_chapters"],
                "publication_authority": False, "provider_calls": 0}



def _validated_previous_source(project, original, final_dir):
    """A stale pointer must still identify well-formed, byte-consistent evidence."""
    source = original["source"]
    require(set(original) == {"revision", "source", "approvals", "authorization_sha256", "runtime"}
            and original["revision"] == REVISION
            and set(source) == {"revision", "novel_id", "config", "source_inventory_sha256",
                "chapters", "evidence", "profile", "whole_history_sha256", "transactions", "novel_policy"}
            and source.get("revision") == REVISION, "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    from novel_project import NovelConfig
    prior_config = NovelConfig.from_dict(source["config"])
    require(prior_config.id == project.config.id and prior_config.source_type == "txt"
            and prior_config.final_chapter == source["chapters"]["file_count"],
            "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    _sha(source["source_inventory_sha256"]); _sha(source["whole_history_sha256"])
    require(isinstance(source["transactions"], list) and source["transactions"],
            "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    for transaction in source["transactions"]:
        require(set(transaction) == {"transaction_id", "intent_sha256"}, "INVALID_PREVIOUS_SOURCE_EVIDENCE")
        _sha(transaction["transaction_id"]); _sha(transaction["intent_sha256"])
    require(isinstance(original["runtime"], dict) and isinstance(source["profile"], dict)
            and set(source["novel_policy"]) == {"translation_addendum.txt", "source_overrides.json"},
            "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    for sha in [*original["runtime"].values(), *source["profile"].values(),
                *(v for v in source["novel_policy"].values() if v is not None)]:
        _sha(sha)
    require(isinstance(source["evidence"], dict) and source["novel_id"] == project.config.id
            and set(original["approvals"]) in (set(APPROVALS), set(APPROVALS) | {"normalization_inventory"}),
            "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    for sha in [*source["evidence"].values(), *original["approvals"].values(), original["authorization_sha256"]]:
        _sha(sha)
    count = source["chapters"]["file_count"]
    require(type(count) is int and 0 < count <= 9999,
            "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    require(canonical_hash(source["chapters"]) == canonical_hash(cleanup.framed_corpus_fingerprint(
        _payloads(final_dir / "chapters", count))), "INVALID_PREVIOUS_SOURCE_EVIDENCE")
    return source


def _review_handoff(project, diagnostics, snapshot, inputs):
    """Enrich the existing advisory rows; never write or issue decisions.

    A prior pointer is only a comparison witness after its immutable intent,
    provenance, archive and committed inventory independently agree. No old
    source bytes are read from today's mutable source paths to prove that past.
    """
    import re
    from publication_readiness import REVIEW
    previous = None
    previous_final = None
    comparison = {"status": "UNAVAILABLE", "reason_code": "NO_PREVIOUS_GENERATION",
                  "changed_evidence_paths": None, "changed_chapters": None}
    try:
        if plain(project.root / POINTER).exists():
            _validate_previous_pointer(project)
            pointer = read_object(project.root / POINTER)
            previous_final = project.root / GENERATIONS / _sha(pointer["generation"]) / "final"
            previous = read_object(previous_final.parent / "intent.json")
            source = _validated_previous_source(project, previous, previous_final)
            # Only owner-defined filenames may enter the packet. Unknown names
            # make the comparison unavailable, never a prose-bearing path list.
            fixed = {"novel.json", "data/chapters.json", "source/novel.txt",
                     "data/progress.json", "data/glossary.json", "data/issues.json",
                     "data/continuity.json", "data/audit_history.json"}
            patterns = (
                r"output/audits/audit_[0-9]{4}_[0-9]{4}(?:\.checkpoint|\.finalization)?\.json",
                r"output/audits/whole_history/[0-9a-f]{64}\.json",
                r"output/publication_freshness/[0-9a-f]{64}\.json",
                r"output/retroactive_fixes/transactions/[0-9a-f]{64}\.(?:plan|state|result|reconcile\.[0-9a-f]{64})\.json",
                r"backup/retroactive_[0-9a-f]{64}/chapters_before/[0-9]{4}\.md",
            )
            def evidence(value):
                require(isinstance(value, dict), "INVALID_PREVIOUS_SOURCE_EVIDENCE")
                for path, sha in value.items():
                    require(path in fixed or any(re.fullmatch(pattern, path) for pattern in patterns),
                            "UNSUPPORTED_COMPARISON_EVIDENCE_PATH")
                    _sha(sha)
                return value
            old = evidence(source["evidence"])
            for key in APPROVALS:
                _sha(previous["approvals"][key])
            _sha(previous["authorization_sha256"])
            comparison["reason_code"] = "CURRENT_SOURCE_EVIDENCE_UNAVAILABLE"
            if snapshot is not None:
                new = evidence(snapshot["evidence"])
                paths = [path for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]
                before = {r["unit"]: r["sha256"] for r in source["chapters"]["files"]}
                after = {r["unit"]: r["sha256"] for r in snapshot["chapters"]["files"]}
                chapters = [n for n in sorted(before.keys() | after.keys()) if before.get(n) != after.get(n)]
                comparison = {"status": "AVAILABLE", "reason_code": "VALIDATED_SOURCE_EVIDENCE_COMPARISON",
                    "changed_evidence_paths": paths[:256], "changed_chapters": chapters[:256],
                    "changed_evidence_count": len(paths), "changed_chapter_count": len(chapters),
                    "truncated": len(paths) > 256 or len(chapters) > 256}
    except _DIAGNOSTIC_ERRORS:
        previous = previous_final = None
        comparison = {"status": "UNAVAILABLE", "reason_code": "PREVIOUS_GENERATION_EVIDENCE_INVALID",
                      "changed_evidence_paths": None, "changed_chapters": None}

    try:
        auth = read_object(project.root / AUTHORIZATION)
    except _DIAGNOSTIC_ERRORS:
        auth = {}
    candidate = project.root / GENERATIONS / canonical_hash(inputs) / "final" if inputs else None
    same_corpus = (snapshot is not None and previous is not None
                   and snapshot["chapters"] == previous["source"]["chapters"])
    for key, path in {**APPROVALS, "authorization": AUTHORIZATION, "publication_review": REVIEW}.items():
        row = diagnostics[key]
        row.update(authorized_sha256=None, previously_authorized_sha256=None, sha256=None, approval_bytes_valid=None,
                   decision_binding="NOT_VERIFIED", decision_reissuance_required=None,
                   semantic_review="NOT_PERFORMED")
        try:
            sha = digest(_bytes(project.root / path))
            row["sha256"] = sha
            if previous and key in APPROVALS:
                row["previously_authorized_sha256"] = previous["approvals"][key]
            elif previous and key == "authorization":
                row["previously_authorized_sha256"] = previous["authorization_sha256"]
            authorized = (auth.get(key + "_sha256") if key in APPROVALS else
                          previous.get("authorization_sha256") if previous and key == "authorization" else None)
            row["authorized_sha256"] = _sha(authorized) if authorized is not None else None
            row["approval_bytes_valid"] = sha == authorized if authorized is not None else None
            if key in APPROVALS:
                if row["status"] == "BLOCKED":
                    row.update(decision_binding="INVALID", decision_reissuance_required=True)
                    continue
                if diagnostics["authorization"]["status"] != "VERIFIED":
                    row["binding_reason_code"] = "REGENERATION_AUTHORIZATION_UNAVAILABLE"
                    continue
                # No temporary corpus or predicted downstream pipeline. Reuse
                # the current candidate, or an identical previously proven input.
                stage_final = candidate
                if stage_final is None or not (stage_final / {"normalization_plan": "chapters",
                        "title_plan": "normalized_chapters", "editorial_authorization": "publication_chapters"}[key]).is_dir():
                    stage_final = previous_final if same_corpus else None
                stage_inputs = {"approvals": {k: diagnostics[k].get("sha256") for k in APPROVALS}}
                if stage_final == previous_final and stage_final != candidate and key != "normalization_plan":
                    # Retained intermediates are today's input only if current
                    # upstream decisions independently reproduce them. Failure
                    # here leaves this downstream decision unproved, not invalid.
                    try:
                        normal = _stage_previews(project, stage_final, stage_inputs)
                        normalization.verify_existing_application(normal,
                            output_dir=stage_final / "normalized_chapters",
                            report_json_path=stage_final / "reports/normalization_application.json")
                        if key == "editorial_authorization":
                            title = _title_preview(project, stage_final, stage_inputs)
                            titles.verify_existing_application(title, stage_final / "titled_chapters",
                                stage_final / "reports/title_application.json")
                            presentation = cleanup.build_plan(stage_final / "titled_chapters", project.config.final_chapter)
                            require(_payloads(stage_final / "publication_chapters", project.config.final_chapter) ==
                                    [(u.source_path.name, u.output_bytes) for u in presentation.units],
                                    "PUBLICATION_CORPUS_DRIFT")
                    except _DIAGNOSTIC_ERRORS:
                        row["binding_reason_code"] = "UPSTREAM_STAGE_INPUT_UNAVAILABLE"
                        continue
                if key == "normalization_plan" and snapshot is not None:
                    _stage_previews(project, project.chapter_dir.parent, stage_inputs)
                elif stage_final is not None and snapshot is not None:
                    _plain_tree(stage_final)
                    if key == "title_plan":
                        # Prior normalized bytes are usable only after current
                        # normalization independently reconstructs that corpus.
                        normal = _stage_previews(project, stage_final, stage_inputs)
                        normalization.verify_existing_application(normal,
                            output_dir=stage_final / "normalized_chapters",
                            report_json_path=stage_final / "reports/normalization_application.json")
                        _title_preview(project, stage_final, stage_inputs)
                    else:
                        require(editorial.build_plan_report(PublicationWorkspace(project, stage_final))["ready"],
                                "EDITORIAL_APPROVAL_NOT_READY")
                else:
                    row["binding_reason_code"] = "EXACT_STAGE_INPUT_UNAVAILABLE"
                    continue
                row.update(decision_binding="VALID", decision_reissuance_required=False,
                           binding_reason_code="EXISTING_STAGE_OWNER_VERIFIED")
            elif key == "publication_review":
                if row["status"] == "VERIFIED":
                    row.update(decision_binding="VALID", decision_reissuance_required=False)
                elif row["reason_code"] in {"PUBLICATION_REVIEW_STALE_REVERIFY_EXACT_DEPENDENCIES",
                        "PUBLICATION_SEMANTIC_REVIEW_REQUIRED", "MISSING_PREREQUISITE"}:
                    row.update(decision_binding="INVALID", decision_reissuance_required=True)
            elif diagnostics["source_snapshot"]["status"] == "VERIFIED" and row["status"] == "VERIFIED":
                row.update(decision_binding="VALID", decision_reissuance_required=False)
            elif snapshot is not None:
                row.update(decision_binding="INVALID", decision_reissuance_required=True)
        except _DIAGNOSTIC_ERRORS:
            row.update(decision_binding="INVALID", decision_reissuance_required=True,
                       binding_reason_code="APPROVAL_OR_INPUT_BINDING_INVALID")

    actions = []
    def action(code, path):
        actions.append({"order": len(actions) + 1, "action": code, "path": path})
    if diagnostics["whole_history"]["status"] != "VERIFIED":
        action("RECERTIFY_AND_REVIEW_WHOLE_HISTORY", diagnostics["whole_history"]["path"])
    for key, path in APPROVALS.items():
        row = diagnostics[key]
        if row["decision_binding"] != "VALID":
            action("EXPLICITLY_REVIEW_OR_REISSUE_STAGE_DECISION", path)
        elif row["approval_bytes_valid"] is not True:
            action("EXPLICITLY_AUTHORIZE_REVIEWED_APPROVAL_BYTES", path)
    if (diagnostics["authorization"]["decision_binding"] != "VALID"
            or any(diagnostics[key]["decision_binding"] == "INVALID"
                   or diagnostics[key]["approval_bytes_valid"] is False for key in APPROVALS)):
        action("EXPLICITLY_REISSUE_SOURCE_AND_APPROVAL_AUTHORIZATION", AUTHORIZATION)
    if diagnostics["publication_review"]["decision_binding"] != "VALID":
        action("REGENERATE_CANDIDATE_THROUGH_EXISTING_STAGE_OWNERS", AUTHORIZATION)
        action("REVIEW_ACTUAL_COMPLETED_DEPENDENCY_GRAPH_AND_REISSUE", REVIEW)
        action("RESUME_REGENERATION_FOR_PUBLICATION_CERTIFICATION", POINTER)
    return {"source_changes": comparison, "required_operator_actions": actions}


def publication_preflight(project, whole_history_sha256=None):
    """Read-only bounded diagnostics. Only current_generation can prove CURRENT.

    Fixed paths, codes and hashes only; never emit raw validator exceptions,
    plans, anchors, source, prose or the production module's mutable state.
    No lock is acquired: this operation must not even create a rendezvous file.
    Unavailable intermediate corpora are explicitly unvalidated, never approved.
    """
    diagnostics = {}

    def row(key, status, code, path, **extra):
        diagnostics[key] = dict(status=status, reason_code=code, path=path, **extra)

    def failure(key, path, exc):
        code = _blocker(exc)["reason_code"]
        known = {"ORIGINAL_SOURCE_IDENTITY_DRIFT": project.source_txt,
                 "SOURCE_INVENTORY_DRIFT": project.chapters_file,
                 "SELECTED_CONFIG_DRIFT": project.config_file,
                 "INCOMPLETE_COMMITTED_AUDIT_BOUNDARY": project.progress_file,
                 "PENDING_WORK_REQUIRES_RECONCILIATION": project.pending_dir,
                 "UNSUPPORTED_STANDALONE_REGENERATION": project.retroactive_dir / "standalone_lineage"}
        if code in known:
            path = known[code].relative_to(project.root).as_posix()
        cause = exc
        for _ in range(5):
            if getattr(cause, "filename", None):
                candidate = Path(cause.filename).absolute()
                if candidate.is_relative_to(project.root.absolute()):
                    relative = candidate.relative_to(project.root.absolute()).as_posix()
                    if len(relative) <= 512:
                        path = relative
                break
            cause = cause.__cause__
            if cause is None:
                break
        row(key, "BLOCKED", code, path)

    def object_at(key, path):
        try:
            target = plain(project.root / path)
            if not target.exists():
                row(key, "REVIEW_REQUIRED", "MISSING_PREREQUISITE", path)
                return None
            return read_object(target)
        except _DIAGNOSTIC_ERRORS as exc:
            failure(key, path, exc)
            return None

    auth = object_at("authorization", AUTHORIZATION)
    if auth is not None:
        valid = (auth.get("revision") == REVISION and auth.get("novel_id") == project.config.id
                 and auth.get("enabled") is True and auth.get("finalization") == "committed-identity-v1"
                 and type(auth.get("allow_manual_approved")) is bool)
        row("authorization", "VERIFIED" if valid else "BLOCKED",
            "AUTHORIZATION_BINDING_PRESENT" if valid else "REGENERATION_NOT_AUTHORIZED", AUTHORIZATION)
        if whole_history_sha256 is None:
            whole_history_sha256 = auth.get("whole_history_sha256")
    report_path = "output/audits/whole_history"
    snapshot = None
    try:
        _history_source(project)
        require(not _standalone_present(project), "UNSUPPORTED_STANDALONE_REGENERATION")
        if whole_history_sha256 is None:
            row("whole_history", "REVIEW_REQUIRED", "SELECT_WHOLE_HISTORY_REPORT", report_path)
        else:
            sha = _sha(whole_history_sha256)
            report_path += "/" + sha + ".json"
            saved = object_at("whole_history", report_path)
            if saved is not None:
                require(canonical_hash(saved) == sha and saved.get("revision") == "aut2-history-evidence-v1"
                        and saved.get("novel_id") == project.config.id
                        and saved.get("scope") == "final_whole_committed_history"
                        and saved.get("status") in {"CHECKED", "CHECKED_WITH_DEFERRALS"}, "INVALID_WHOLE_HISTORY_REPORT")
                from goat_periodic_audit_finalize_plan import local_module
                from historical_reconciliation import final_consistency_report
                actual = final_consistency_report(local_module(project))
                # Reject malformed self-hashed reports rather than calling them
                # stale. This is shape checking only; exact equality with the
                # existing owner remains necessary for a verified report.
                require(set(saved) == set(actual)
                        and all(type(saved[k]) is type(v) for k, v in actual.items()), "INVALID_WHOLE_HISTORY_REPORT")
                _sha(saved["glossary_revision_sha256"])
                _sha(saved["source_inventory_sha256"])
                require(saved["transactions"] and all(isinstance(t, dict)
                        and set(t) == {"transaction_id", "intent_sha256"} for t in saved["transactions"]),
                        "INVALID_WHOLE_HISTORY_REPORT")
                for transaction in saved["transactions"]:
                    _sha(transaction["transaction_id"])
                    _sha(transaction["intent_sha256"])
                require(len({t["transaction_id"] for t in saved["transactions"]}) == len(saved["transactions"]),
                        "INVALID_WHOLE_HISTORY_REPORT")
                rows = saved["chapters"]
                require(saved["audit_boundary"] == saved["examined_chapters"] == len(rows)
                        and [r["chapter"] for r in rows] == list(range(1, len(rows) + 1)), "INVALID_WHOLE_HISTORY_REPORT")
                for item in rows:
                    require(set(item) == {"chapter", "english_sha256", "source_sha256", "decisions"}
                            and type(item["chapter"]) is int and isinstance(item["decisions"], list), "INVALID_WHOLE_HISTORY_REPORT")
                    _sha(item["english_sha256"]); _sha(item["source_sha256"])
                    for decision in item["decisions"]:
                        _sha(decision["term_id"])
                        require(decision.get("outcome") in {"already_current", "deferred"}
                                and isinstance(decision.get("reason_code"), str), "INVALID_WHOLE_HISTORY_REPORT")
                deferred = sum(any(d["outcome"] == "deferred" for d in r["decisions"]) for r in rows)
                require(saved["deferred_chapters"] == deferred
                        and saved["status"] == ("CHECKED_WITH_DEFERRALS" if deferred else "CHECKED")
                        and saved["already_current_chapters"] == sum(bool(r["decisions"]) and all(d["outcome"] == "already_current" for d in r["decisions"]) for r in rows)
                        and saved["unchanged_chapters"] == sum(not r["decisions"] for r in rows)
                        and saved["corrected_chapters"] == saved["provider_calls"] == 0
                        and saved["semantic_rereview"] == "NOT_PERFORMED"
                        and saved["publication_status"] == "NOT_VERIFIED", "CONTRADICTORY_WHOLE_HISTORY_REPORT")
                if saved != actual:
                    row("whole_history", "STALE", "RECERTIFY_WHOLE_HISTORY", report_path)
                elif actual["deferred_chapters"]:
                    row("whole_history", "REVIEW_REQUIRED", "HISTORICAL_DEFERRALS_REQUIRE_REVIEW", report_path,
                        deferred_chapters=actual["deferred_chapters"])
                else:
                    row("whole_history", "VERIFIED", "EXACT_WHOLE_HISTORY_VERIFIED", report_path, sha256=sha)
                    snapshot = source_snapshot(project, sha)
    except _DIAGNOSTIC_ERRORS as exc:
        failure("whole_history", report_path, exc)
    if snapshot is None:
        row("source_snapshot", diagnostics["whole_history"]["status"], "WHOLE_HISTORY_PREREQUISITE", AUTHORIZATION)
    else:
        snapshot_sha = canonical_hash(snapshot)
        bound = auth is not None and auth.get("source_snapshot_sha256") == snapshot_sha
        row("source_snapshot", "VERIFIED" if bound else "REVIEW_REQUIRED",
            "SOURCE_SNAPSHOT_BOUND" if bound else "SOURCE_APPROVAL_STALE", AUTHORIZATION, sha256=snapshot_sha)

    # File binding diagnostics are independent: report every exact approval path
    # even when an earlier prerequisite prevents the authoritative input proof.
    for key, name in APPROVALS.items():
        approval = object_at(key, name)
        if approval is None:
            continue
        try:
            # Schema checks belong to the existing stage owners even when the
            # exact intermediate corpus has not been constructed yet.
            if key == "normalization_plan":
                normalization._validate_plan_contract(approval, project.config.id, "prose")
            elif key == "title_plan":
                titles._validate_plan_contract(approval, project.config.id)
            else:
                manifest, _ = editorial.load_authorization(project.root / name)
                require(manifest.get("novel") == project.config.id, "FOREIGN_EDITORIAL_AUTHORIZATION")
                editorial._expected_checkpoint(manifest)
                editorial._validate_authorized_set(manifest)
            sha = digest(_bytes(project.root / name))
        except _DIAGNOSTIC_ERRORS as exc:
            failure(key, name, exc)
            continue
        bound = auth is not None and auth.get(key + "_sha256") == sha
        upstream = snapshot is not None and diagnostics["source_snapshot"]["status"] == "VERIFIED"
        row(key, "VERIFIED" if bound and upstream else "REVIEW_REQUIRED",
            "APPROVAL_BYTES_BOUND_VALIDATION_PENDING" if bound and upstream else "APPROVAL_REVIEW_OR_REISSUANCE_REQUIRED",
            name, sha256=sha, stage_validation="NOT_PERFORMED")

    # Keep _inputs authoritative. Its failure cannot be replaced by advisory
    # equality checks above. Stage validators run only against an existing exact
    # input generation; no temporary corpus, application or regeneration here.
    inputs = None
    try:
        inputs = _inputs(project)
        row("inputs", "VERIFIED", "AUTHORITATIVE_INPUTS_VERIFIED", AUTHORIZATION)
    except _DIAGNOSTIC_ERRORS as exc:
        if any(d["status"] in {"REVIEW_REQUIRED", "STALE"} for d in diagnostics.values()):
            row("inputs", "REVIEW_REQUIRED", _blocker(exc)["reason_code"], AUTHORIZATION)
        else:
            failure("inputs", AUTHORIZATION, exc)
    if inputs is not None:
        final = project.root / GENERATIONS / canonical_hash(inputs) / "final"
        for key, validator, corpus in (
                ("normalization_plan", lambda: _stage_previews(project, final, inputs), "chapters"),
                ("title_plan", lambda: _title_preview(project, final, inputs), "normalized_chapters"),
                ("editorial_authorization", lambda: require(editorial.build_plan_report(PublicationWorkspace(project, final))["ready"],
                    "EDITORIAL_APPROVAL_NOT_READY"), "publication_chapters")):
            try:
                if not plain(final / corpus).is_dir():
                    diagnostics[key]["stage_validation"] = "INPUT_CORPUS_MISSING"
                    diagnostics[key]["input_path"] = (final / corpus).relative_to(project.root).as_posix()
                    continue
                _plain_tree(final)
                validator()
                diagnostics[key]["stage_validation"] = "VERIFIED"
            except _DIAGNOSTIC_ERRORS as exc:
                failure(key, APPROVALS[key], exc)

    from publication_readiness import REVIEW
    object_at("publication_review", REVIEW)
    if "publication_review" not in diagnostics:
        row("publication_review", "REVIEW_REQUIRED", "EXACT_GRAPH_REVIEW_REQUIRES_VALIDATION", REVIEW)
        if inputs is not None and plain(final).is_dir():
            try:
                from publication_readiness import require_publication_ready
                require_publication_ready(PublicationWorkspace(project, final))
                row("publication_review", "VERIFIED", "EXACT_GRAPH_REVIEW_VERIFIED", REVIEW)
            except _DIAGNOSTIC_ERRORS as exc:
                code = _blocker(exc)["reason_code"]
                status = "REVIEW_REQUIRED" if code in {
                    "PUBLICATION_REVIEW_STALE_REVERIFY_EXACT_DEPENDENCIES",
                    "PUBLICATION_REVIEW_REQUIRED_AT_CONFIG_PUBLICATION_READINESS",
                    "PUBLICATION_SEMANTIC_REVIEW_REQUIRED"} else "BLOCKED"
                row("publication_review", status, code, REVIEW)
    current = {}
    try:
        _validate_previous_pointer(project)
        current = current_generation(project)
        if current["status"] == "CURRENT":
            row("publication_review", "VERIFIED", "EXACT_GRAPH_REVIEW_VERIFIED", REVIEW)
            status = "CURRENT"
        else:
            reason = current.get("blocker", {}).get("reason_code", "NO_CURRENT_GENERATION")
            if reason in {"PUBLICATION_REVIEW_STALE_REVERIFY_EXACT_DEPENDENCIES",
                          "PUBLICATION_REVIEW_REQUIRED_AT_CONFIG_PUBLICATION_READINESS",
                          "PUBLICATION_SEMANTIC_REVIEW_REQUIRED"}:
                row("publication_review", "REVIEW_REQUIRED", reason, REVIEW)
            status = "STALE"
            if inputs is not None and plain(project.root / POINTER).exists():
                pointer = read_object(project.root / POINTER)
                if pointer.get("generation") == canonical_hash(inputs) and not reason.startswith("PUBLICATION_"):
                    row("generation", "BLOCKED", reason, POINTER)
        if any(d["status"] == "REVIEW_REQUIRED" for d in diagnostics.values()):
            status = "REVIEW_REQUIRED"
        if any(d["status"] == "BLOCKED" for d in diagnostics.values()):
            status = "BLOCKED"
    except _DIAGNOSTIC_ERRORS as exc:
        failure("generation", POINTER, exc)
        status = "BLOCKED"
    handoff = _review_handoff(project, diagnostics, snapshot, inputs)
    return {"status": status, "diagnostics": diagnostics, "approval_review": handoff,
            "generation": current.get("generation") if status == "CURRENT" else None,
            "publication_authority": False, "provider_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--report", action="store_true", help="read-only publication preflight")
    operations.add_argument("--recertify-history", action="store_true", help="save immutable local whole-history evidence only")
    operations.add_argument("--inspect-whole-history", help="read-only snapshot for an existing whole-history report hash")
    args = parser.parse_args(argv)
    try:
        project = select_project(args.novel)
        if args.report:
            result = publication_preflight(project)
        elif args.recertify_history:
            result = recertify_history(project)
        elif args.inspect_whole_history:
            source = source_snapshot(project, args.inspect_whole_history)
            result = {"status": "SNAPSHOT_NOT_AUTHORITY", "source_snapshot_sha256": canonical_hash(source),
                      "source": source, "required_approvals": APPROVALS, "provider_calls": 0}
        else:
            result = regenerate(project)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["status"] == "BLOCKED" else 0
    except _DIAGNOSTIC_ERRORS as exc:
        print(json.dumps({"status": "BLOCKED" if args.report or args.recertify_history else "STALE",
                          "blocker": _blocker(exc), "provider_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
