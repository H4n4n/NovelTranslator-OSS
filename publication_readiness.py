"""Read-only D1 dependency proofs and explicit publication review gate.

Hashes prove local consistency under the existing cooperative writer/approval
model. They do not authenticate a reviewer against wholesale evidence forgery.
No builder, provider, evidence repair, or semantic reviewer is invoked here.
"""
from pathlib import Path

from historical_reconciliation import plain, read_object, read_value, require, publication_revisions
from retroactive_span_planning import canonical_hash, digest

REVISION = "publication-readiness-v1"
REVIEW = "config/publication_readiness.json"
REQUIRED_REVIEWS = ("state_continuity", "issues", "whole_history_semantics", "final_glossary")
STAGES = {
    "finalization": ("chapters", "manifest.json"),
    "normalization": ("normalized_chapters", "reports/normalization_application.json"),
    "titles": ("titled_chapters", "reports/title_application.json"),
    "editorial_input": ("publication_chapters", "reports/publication_cleanup.json"),
    "editorial": ("editorial_chapters", "reports/final_editorial_apply.json"),
}


def _file(project, path):
    path = plain(path)
    require(path.absolute().is_relative_to(project.root.absolute()), "FOREIGN_DEPENDENCY_TARGET")
    raw = path.read_bytes()
    return {"path": path.relative_to(project.root).as_posix(), "sha256": digest(raw)}


def _tree(project, directory):
    directory = plain(directory)
    require(directory.is_dir(), "MISSING_DEPENDENCY_DIRECTORY")
    result = []
    for path in sorted(directory.iterdir()):
        require(plain(path).is_file(), "AMBIGUOUS_DEPENDENCY_INVENTORY")
        if path.suffix == ".json":
            read_object(path)
        result.append(_file(project, path))
    return result


def dependency_graph(project):
    """Reconstruct the complete supported graph, without approving publication.

    The existing independent generation verifier owns exact normalization,
    title, presentation, editorial plan/apply reconstruction and current QA.
    Unsupported source partitions or standalone history remain hard blockers.
    """
    import publication_regeneration as generation
    from chapter_library import chapter_production_index
    from noveltranslator_core import translation_commit_qa
    from translation_profile import load_translation_profile
    final = plain(getattr(project, "publication_final_dir", project.root / "final"))
    require((final / "reports/source_bound_provenance.json").is_file(),
            "PUBLICATION_MISSING_SOURCE_PROOF_REVALIDATE_DOWNSTREAM")
    inputs = generation.verify_generation_corpus(project, final)
    source = inputs["source"]
    # Independently qualify actual authorization, source/span plans, backups,
    # audit/standalone chronology and finalized evidence, not just receipt hashes.
    from goat_periodic_audit_finalize_plan import local_module
    from production_standalone_lineage import verified_events
    events = verified_events(local_module(project))
    require([p["transaction_id"] for p, _, _ in events] ==
            [p["transaction_id"] for p in source["transactions"]], "PUBLICATION_HISTORY_CHAIN_DRIFT")
    revisions = publication_revisions(project)
    by_transaction = {plan["transaction_id"]: plan for plan, _, _ in events}
    require(len(by_transaction) == len(events), "COMPETING_PUBLICATION_HISTORY")
    invalidations = []
    # These edges are derived from immutable BEFORE target inventories captured
    # by the real historical writers. A changed target remains NOT_VERIFIED.
    for revision in revisions:
        require(revision["transaction_id"] in by_transaction, "UNOWNED_INVALIDATION_TRANSACTION")
        plan = by_transaction[revision["transaction_id"]]
        for item in plan["chapters"]:
            if not item["edits"]:
                continue
            for folder, files in revision["publication_inputs_before"].items():
                if isinstance(files, str):
                    targets = {folder: files}
                elif files is None:
                    targets = {}
                else:
                    require(isinstance(files, dict), "INVALID_DOWNSTREAM_INVENTORY")
                    targets = {folder + "/" + name: sha for name, sha in files.items()}
                for target, before in sorted(targets.items()):
                    relative = Path(target)
                    require(not relative.is_absolute() and not relative.drive
                            and ".." not in relative.parts and ":" not in target,
                            "FOREIGN_DEPENDENCY_TARGET")
                    path = plain(project.root / relative)
                    after = digest(path.read_bytes()) if path.is_file() else None
                    invalidations.append({"transaction_id": plan["transaction_id"],
                        "chapter": item["chapter"], "source_sha256": item["source_sha256"],
                        "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"],
                        "target": relative.as_posix(), "target_before_sha256": before,
                        "target_sha256": after, "status": "MISSING" if after is None else
                            "STALE" if after == before else "NOT_VERIFIED"})
    units = {chapter_production_index(u): u for u in read_value(project.chapters_file)}
    profile = load_translation_profile(project.config.profile)
    glossary = read_object(project.glossary_file)
    final_glossary = final / "glossary.json"
    require(read_object(final_glossary) == glossary, "FINAL_GLOSSARY_STALE_REVIEW_REQUIRED")
    nodes, edges = [], []
    def node(identity, evidence, depends=()):
        nodes.append({"id": identity, "evidence": evidence})
        edges.extend({"from": parent, "to": identity} for parent in depends)
    node("source", {"inventory_sha256": source["source_inventory_sha256"],
                    "files": {k: v for k, v in source["evidence"].items() if k in
                              {"novel.json", "data/chapters.json", "source/novel.txt"}},
                    "profile": source["profile"], "novel_policy": source["novel_policy"]})
    node("glossary_decisions", {"files": [_file(project, project.glossary_file),
                                          _file(project, project.audit_history_file)],
                                "transactions": source["transactions"],
                                "invalidations_sha256": canonical_hash(revisions)}, ("source",))
    for n in range(1, project.config.final_chapter + 1):
        name = f"{n:04d}.md"
        chapter = _file(project, project.chapter_dir / name)
        chapter["source_sha256"] = digest(units[n]["text"].encode("utf-8"))
        chapter["transactions"] = [{"transaction_id": r["transaction_id"], **item}
            for r in revisions for item in r["chapters"] if item["chapter"] == n]
        node("chapter:" + name, chapter, ("source", "glossary_decisions"))
    chapters = tuple("chapter:" + f"{n:04d}.md" for n in range(1, project.config.final_chapter + 1))
    node("state_continuity", {"files": [_file(project, project.continuity_file),
         _file(project, project.issues_file), _file(project, project.progress_file)],
         "verification": "EXPLICIT_SEMANTIC_REVIEW_REQUIRED"}, chapters + ("glossary_decisions",))
    qa = []
    for n in range(1, project.config.final_chapter + 1):
        result = translation_commit_qa(units[n]["text"],
            (project.chapter_dir / f"{n:04d}.md").read_text(encoding="utf-8-sig"),
            glossary["entries"], profile.high_priority_categories,
            source_language=project.config.source_language, chapter_number=n)
        require(result["commit_allowed"], f"PUBLICATION_QA_BLOCKED_{n:04d}")
        qa.append({"chapter": n, "result_sha256": canonical_hash(result)})
    node("qa", {"current_deterministic_results": qa,
         "saved_reports": _tree(project, project.qa_dir),
         "saved_report_policy": "HASH_BOUND_ONLY_CURRENT_QA_RECOMPUTED"}, chapters + ("glossary_decisions",))
    previous = ("qa", "state_continuity")
    for stage, (directory, report) in STAGES.items():
        evidence = {"corpus": _tree(project, final / directory), "report": _file(project, final / report)}
        if stage == "editorial":
            evidence["plan"] = _file(project, final / "reports/final_editorial_plan.json")
        node(stage, evidence, previous)
        previous = (stage,)
    node("final_glossary", _file(project, final_glossary), ("glossary_decisions", "editorial"))
    node("final_epub", {"target": (final / "epub" / (project.config.id + ".epub")).relative_to(project.root).as_posix(),
         "verification": "EXISTING_ZIP_OPF_SPINE_NAV_NCX_CONTENT_AND_REPORT_VALIDATORS",
         "epubcheck": "NOT_PERFORMED"}, ("editorial", "final_glossary"))
    return {"revision": REVISION, "novel_id": project.config.id,
            "generation": canonical_hash(inputs), "source_proof_sha256": canonical_hash(source),
            "nodes": nodes, "edges": edges, "historical_invalidations": invalidations, "provider_calls": 0}


def require_publication_ready(project):
    """Final path gate. A candidate graph, timestamp or file presence cannot pass."""
    graph = dependency_graph(project)
    require(plain(project.root / REVIEW).is_file(), "PUBLICATION_REVIEW_REQUIRED_AT_CONFIG_PUBLICATION_READINESS")
    review = read_object(project.root / REVIEW)
    require(set(review) == {"revision", "novel_id", "graph_sha256", "reviewer", "reviews"}
            and review["revision"] == REVISION and review["novel_id"] == project.config.id,
            "PUBLICATION_REVIEW_FOREIGN_OR_INVALID")
    require(isinstance(review["reviewer"], str) and bool(review["reviewer"].strip())
            and review["reviews"] == {key: "explicitly_verified" for key in REQUIRED_REVIEWS},
            "PUBLICATION_SEMANTIC_REVIEW_REQUIRED")
    require(review["graph_sha256"] == canonical_hash(graph),
            "PUBLICATION_REVIEW_STALE_REVERIFY_EXACT_DEPENDENCIES")
    return graph
