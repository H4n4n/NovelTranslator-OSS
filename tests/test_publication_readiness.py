"""D1 exact freshness gates: disposable Chinese/Korean, no EPUB regeneration."""
import copy
from dataclasses import replace
from pathlib import Path

import pytest

import build_publication_epub as epub
import historical_reconciliation as history
import publication_readiness as readiness
import publication_regeneration as generation
from production_run_lock import production_run_lock
from retroactive_span_planning import canonical_hash
from test_build_publication_epub import novel, install_approvals, write, snapshot
from test_production_lifecycle import aut1_engine


def prepare_existing_evidence(project):
    """Existing owners produce synthetic non-EPUB stages; never regenerate()."""
    from build_finalization_corpus import build_identity_finalization_corpus
    inputs = generation._inputs(project)
    final = project.root / generation.GENERATIONS / canonical_hash(inputs) / "final"
    workspace = generation.PublicationWorkspace(project, final)
    generation._immutable_json(final.parent / "intent.json", inputs)
    build_identity_finalization_corpus(project, final)
    reports = final / "reports"
    normal = generation._stage_previews(project, final, inputs)
    generation.normalization.publish_normalized_corpus(normal, output_dir=final / "normalized_chapters",
        report_json_path=reports / "normalization_application.json", report_md_path=reports / "normalization_application.md")
    title = generation._title_preview(project, final, inputs)
    generation.titles.publish_titled_corpus(title, final / "titled_chapters", reports / "title_application.json", reports / "title_application.md")
    presentation = generation.cleanup.build_plan(final / "titled_chapters", project.config.final_chapter)
    generation.cleanup.publish_plan(presentation, final / "publication_chapters", reports / "publication_cleanup.json", reports / "publication_cleanup.md")
    plan = generation.editorial.build_plan_report(workspace)
    generation._immutable_json(reports / "final_editorial_plan.json", plan)
    (reports / "final_editorial_plan.md").write_text(generation.editorial.render_plan_markdown(plan), encoding="utf-8")
    generation.editorial.apply_ready_plan(workspace)
    generation._immutable_json(reports / "source_bound_provenance.json", inputs)
    write(final / "glossary.json", history.read_object(project.glossary_file))
    return workspace


def explicit_synthetic_review(workspace):
    graph = readiness.dependency_graph(workspace)
    write(workspace.root / readiness.REVIEW, {"revision": readiness.REVISION,
        "novel_id": workspace.config.id, "graph_sha256": canonical_hash(graph),
        "reviewer": "synthetic fixture authority",
        "reviews": {key: "explicitly_verified" for key in readiness.REQUIRED_REVIEWS}})
    return graph


def test_complete_existing_evidence_passes_gate_read_only_and_repeatable(novel):
    project, _, _, harness = novel
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    graph = explicit_synthetic_review(workspace)
    before = snapshot(project.root)
    for _ in range(2):
        history.require_publication_freshness(workspace)
        assert readiness.require_publication_ready(workspace) == graph
    assert snapshot(project.root) == before and harness.calls == []
    assert not list(workspace.publication_final_dir.rglob("*.epub"))
    ids = {node["id"] for node in graph["nodes"]}
    assert {"chapter:0001.md", "chapter:0002.md", "chapter:0003.md", "state_continuity",
            "qa", "normalization", "editorial_input", "editorial", "final_glossary", "final_epub"} <= ids
    assert all(edge["from"] in ids and edge["to"] in ids for edge in graph["edges"])
    assert any(row["chapter"] == 1 and row["target"] == "output/qa/0001.json"
               and row["status"] == "STALE" and len(row["source_sha256"]) == 64
               and row["before_sha256"] != row["after_sha256"]
               for row in graph["historical_invalidations"])


@pytest.mark.parametrize("damage", ["source", "chapter", "glossary", "continuity", "issues", "qa",
    "missing_chapter", "extra_chapter", "editorial", "editorial_apply", "final_glossary", "missing_review",
    "foreign_review", "forked_review", "unreviewed_state", "missing_proof", "interrupted", "receipt",
    "competing_history", "forged_intent", "pending"])
def test_gate_refuses_stale_foreign_changed_missing_interrupted_without_writes(novel, damage, monkeypatch):
    project, _, _, harness = novel
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    explicit_synthetic_review(workspace)
    final = workspace.publication_final_dir
    if damage == "source": project.source_txt.write_bytes(b"changed source")
    elif damage == "chapter": (project.chapter_dir / "0001.md").write_bytes(b"outside correction")
    elif damage == "glossary": write(project.glossary_file, {"entries": {}})
    elif damage == "continuity": write(project.continuity_file, {"summary": "old or forged state"})
    elif damage == "issues": write(project.issues_file, {"issues": ["unresolved"]})
    elif damage == "qa": write(project.qa_dir / "0001.json", {"commit_allowed": True})
    elif damage == "missing_chapter": (project.chapter_dir / "0002.md").unlink()
    elif damage == "extra_chapter": (project.chapter_dir / "0004.md").write_bytes(b"foreign inventory")
    elif damage == "editorial": (final / "editorial_chapters/0001.md").write_bytes(b"stale Hann")
    elif damage == "editorial_apply": write(final / "reports/final_editorial_apply.json", {"ready": True})
    elif damage == "final_glossary": write(final / "glossary.json", {"entries": {}})
    elif damage == "missing_review": (project.root / readiness.REVIEW).unlink()
    elif damage in {"foreign_review", "forked_review", "unreviewed_state"}:
        review = history.read_object(project.root / readiness.REVIEW)
        if damage == "foreign_review": review["novel_id"] = "other_novel"
        elif damage == "forked_review": review["graph_sha256"] = "f" * 64
        else: review["reviews"]["state_continuity"] = "not_performed"
        write(project.root / readiness.REVIEW, review)
    elif damage == "missing_proof": (final / "reports/source_bound_provenance.json").unlink()
    elif damage == "interrupted": (final.parent / "intent.json").write_bytes(b'{"revision":')
    elif damage == "receipt": next((project.root / "output/publication_freshness").glob("*.json")).write_bytes(b'{}')
    elif damage == "competing_history":
        path = next(project.audit_dir.glob("*.finalization.json"))
        (project.audit_dir / "audit_0002_0003.finalization.json").write_bytes(path.read_bytes())
    elif damage == "forged_intent":
        path = next(project.audit_dir.glob("*.finalization.json"))
        value = history.read_object(path)
        value["audit_result"]["decisions"][0]["english"] = "Forged"
        value["intent_sha256"] = canonical_hash({k: v for k, v in value.items() if k != "intent_sha256"})
        write(path, value)
    else: (project.pending_dir / "0004.md").write_bytes(b"paid pending preserved")
    # A prior published file must survive even refusal through the real builder.
    output = final / "epub" / (project.config.id + ".epub")
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"published before refusal")
    before = snapshot(project.root)
    # Prove refusal is at the freshness gate, not merely existing-output or
    # malformed-EPUB refusal. Neither text loading nor the writer may be reached.
    with pytest.raises((RuntimeError, ValueError, OSError, KeyError, TypeError)):
        history.require_publication_freshness(workspace)
    monkeypatch.setattr(epub, "_refuse_existing", lambda *a: pytest.fail("writer reached before stale refusal"))
    monkeypatch.setattr(epub, "load_publication_corpus", lambda *a: pytest.fail("editorial loading reached before stale refusal"))
    for _ in range(2):
        with pytest.raises((RuntimeError, ValueError, OSError, KeyError, TypeError)):
            epub.build_publication_epub(workspace)
        with pytest.raises((RuntimeError, ValueError, OSError, KeyError, TypeError)):
            epub.verify_existing_publication_epub(workspace)
    assert snapshot(project.root) == before and harness.calls == []


def test_no_history_is_not_permission_to_publish_editorial_only(tmp_path):
    from test_build_publication_epub import make_project, install_corpus
    project = make_project(tmp_path)
    install_corpus(project, ["Chapter 1\n\nEditorial bytes.\n"])
    before = snapshot(project.root)
    with pytest.raises(RuntimeError, match="PUBLICATION_MISSING_SOURCE_PROOF"):
        epub.build_publication_epub(project)
    assert snapshot(project.root) == before


def test_review_cannot_cross_novel_roots(novel, tmp_path):
    project, _, _, _ = novel
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    explicit_synthetic_review(workspace)
    from novel_project import NovelProject
    other = NovelProject(project.repository_root, tmp_path / "foreign", replace(project.config, id="other"))
    other.root.mkdir()
    write(other.root / readiness.REVIEW, history.read_object(project.root / readiness.REVIEW))
    foreign = generation.PublicationWorkspace(other, workspace.publication_final_dir)
    before = snapshot(project.root)
    with pytest.raises((RuntimeError, OSError)):
        readiness.require_publication_ready(foreign)
    assert snapshot(project.root) == before


def test_durable_invalidation_captures_qa_state_and_actual_targets(novel):
    project, engine, _, harness = novel
    # Inspect the receipt made by the real audit writer before its Chapter 1 edit.
    revision = history.publication_revisions(project)[0]
    assert 1 in revision["impacted_chapters"]
    assert "output/qa" in revision["publication_inputs_before"]
    assert {"continuity.json", "issues.json"} <= revision["publication_inputs_before"]["data"].keys()
    report = history.publication_freshness(project)
    assert any(row["path"] == "final/editorial_chapters/0001.md" and row["status"] == "STALE"
               for row in report["downstream_outputs"])
    assert any(row["path"] == "data/continuity.json" and row["status"] == "STALE"
               for row in report["downstream_outputs"])
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    before = snapshot(project.root)
    for _ in range(2):
        with pytest.raises(RuntimeError): history.require_publication_freshness(workspace)
    assert before == snapshot(project.root) and harness.calls == []


def test_later_verified_correction_invalidates_existing_generation_targets(novel, monkeypatch):
    """Real audit window 4 corrects Chapter 1 after all editorial stages exist."""
    import production_periodic_audit_finalization as finalizer
    from retroactive_span_planning import digest
    project, engine, _, harness = novel
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    explicit_synthetic_review(workspace)
    history.require_publication_freshness(workspace)
    final_dir = workspace.publication_final_dir
    output = final_dir / "epub" / (project.config.id + ".epub")
    output.parent.mkdir()
    output.write_bytes(b"previous publication bytes")
    old_targets = snapshot(final_dir)
    key = next(iter(engine.glossary["entries"]))
    config = replace(project.config, final_chapter=4)
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", config)
    write(project.config_file, config.to_dict())
    units = history.read_value(project.chapters_file)
    units.append({"chapter": 4, "production_index": 4, "text": key + " 4",
                  "title": "Synthetic", "source_document_sha256": digest(project.source_txt.read_bytes())})
    write(project.chapters_file, units)
    monkeypatch.setattr(engine, "chapters", units)
    monkeypatch.setattr(engine, "chapter_map", {u["chapter"]: u for u in units})
    (project.chapter_dir / "0004.md").write_bytes(b"Chapter 4. Stable\n\nHann returned.\n")
    write(project.qa_dir / "0004.json", {"synthetic_pre_correction_qa": True})
    engine.progress.update(completed=[1, 2, 3, 4], last_completed=4, next_chapter=5)
    engine.save_json(engine.PROGRESS_FILE, engine.progress)
    engine.save_json(engine.periodic_audit_checkpoint_path(4, 4), {"synthetic_accepted_decisions": True})
    stats = {field: 0 for field in finalizer.STAT_TOTAL_FIELDS}
    stats["api_request_count"] = 0
    with production_run_lock(project.root):
        result = finalizer.finalize_periodic_audit(engine, 4, 4,
            {"decisions": [{"chinese": key, "action": "correct", "english": "Hahn"}], "issues": []},
            stats, {"candidate_count": 1, "candidate_set_hash": canonical_hash("later accepted"), "api_request_count": 0})
        assert result["status"] == "FINALIZED"
        before_replay = snapshot(project.root)
        assert finalizer.finalize_periodic_audit(engine, 4, 4,
            {"decisions": [{"chinese": key, "action": "correct", "english": "Hahn"}], "issues": []},
            stats, {"candidate_count": 1, "candidate_set_hash": canonical_hash("later accepted"), "api_request_count": 0})["status"] == "ALREADY_FINALIZED"
        assert snapshot(project.root) == before_replay
    assert b"Hahn walked" in (project.chapter_dir / "0001.md").read_bytes()
    receipt = history.read_object(next((project.root / "output/publication_freshness").glob(
        finalizer.load_intent(engine, 4, 4)["transaction_plan"]["transaction_id"] + ".json")))
    target_dir = (final_dir / "editorial_chapters").relative_to(project.root).as_posix()
    assert receipt["publication_inputs_before"][target_dir]["0001.md"] == digest(
        (final_dir / "editorial_chapters/0001.md").read_bytes())
    assert receipt["publication_inputs_before"]["output/qa"]["0001.json"]
    assert 1 in receipt["impacted_chapters"]
    assert snapshot(final_dir) == old_targets
    before = snapshot(project.root)
    for _ in range(2):
        with pytest.raises(RuntimeError): epub.build_publication_epub(workspace)
    assert snapshot(project.root) == before and harness.calls == []
