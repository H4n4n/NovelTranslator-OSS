"""D1 exact freshness gates: disposable Chinese/Korean, no D2 regeneration."""
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
    """Run the real locked regeneration path up to the EPUB boundary."""
    inputs = generation._inputs(project)
    final = project.root / generation.GENERATIONS / canonical_hash(inputs) / "final"
    workspace = generation.PublicationWorkspace(project, final)

    def stop_before_epub(project):
        raise InterruptedError("synthetic pre-EPUB stop")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(epub, "build_publication_epub", stop_before_epub)
        with pytest.raises(InterruptedError, match="synthetic pre-EPUB stop"):
            generation._regenerate_locked(project)
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
    with production_run_lock(project.root):
        epub.build_publication_epub(workspace)
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

# D1.1 integration regressions. ZIP construction is disposable fixture setup;
# regenerate() below is exercised only for already-complete generations.
def d11_existing_epub(project):
    with production_run_lock(project.root):
        workspace = prepare_existing_evidence(project)
    explicit_synthetic_review(workspace)
    with production_run_lock(project.root):
        epub.build_publication_epub(workspace)
    generation._verify_epub(workspace)
    return workspace


def d11_pointer(project, workspace):
    from retroactive_span_planning import digest
    output = epub.publication_paths(workspace)["output"]
    return {"revision": generation.REVISION, "novel_id": project.config.id,
            "generation": workspace.publication_final_dir.parent.name,
            "epub_sha256": digest(output.read_bytes())}


def d11_previous_generation(project, workspace):
    """A byte-valid older runtime generation, not current-input authority."""
    import shutil
    original = history.read_object(workspace.publication_final_dir.parent / "intent.json")
    original["runtime"]["synthetic_previous_runtime.py"] = "1" * 64
    target = project.root / generation.GENERATIONS / canonical_hash(original) / "final"
    shutil.copytree(workspace.publication_final_dir, target)
    write(target.parent / "intent.json", original)
    write(target / "reports/source_bound_provenance.json", original)
    previous = generation.PublicationWorkspace(project, target)
    paths = epub.publication_paths(previous)
    corpus = epub.load_publication_corpus(previous)
    validation = epub.verify_epub_archive(paths["output"], corpus)
    report = epub.build_epub_report(corpus, paths["output"], paths["output"].read_bytes(), validation, corpus.fingerprint)
    write(paths["report_json"], report)
    paths["report_md"].write_bytes(epub.render_markdown_report(report).encode("utf-8"))
    generation._verify_epub(previous)
    return previous


@pytest.mark.parametrize("pointer_mode", ["absent", "current", "previous"])
@pytest.mark.parametrize("damage", ["missing", "malformed", "forged", "foreign", "stale"])
def test_d11_existing_zip_cannot_certify_or_promote_without_current_review(novel, monkeypatch, pointer_mode, damage):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    pointer = project.root / generation.POINTER
    if pointer_mode == "current":
        write(pointer, d11_pointer(project, workspace))
        assert generation.current_generation(project)["status"] == "CURRENT"
    elif pointer_mode == "previous":
        previous = d11_previous_generation(project, workspace)
        write(pointer, d11_pointer(project, previous))
        generation._validate_previous_pointer(project)
    review_path = project.root / readiness.REVIEW
    if damage == "missing": review_path.unlink()
    elif damage == "malformed": review_path.write_bytes(b'{"revision":')
    elif damage == "stale": write(project.qa_dir / "0001.json", {"changed_report": True})
    else:
        review = history.read_object(review_path)
        if damage == "forged": review["graph_sha256"] = "0" * 64
        else: review["novel_id"] = "foreign_novel"
        write(review_path, review)
    before = snapshot(project.root)
    monkeypatch.setattr(epub, "build_publication_epub", lambda *a, **k: pytest.fail("EPUB regeneration forbidden"))
    monkeypatch.setattr(generation.editorial, "apply_ready_plan", lambda *a, **k: pytest.fail("new editorial apply forbidden"))
    for _ in range(2):
        # The archive/report proof remains valid throughout every refusal.
        generation._verify_epub(workspace)
        with pytest.raises(RuntimeError, match="PUBLICATION|UNREADABLE_EVIDENCE"): generation.regenerate(project)
        assert generation.current_generation(project)["status"] in {"STALE", "NOT_VERIFIED"}
        assert snapshot(project.root) == before
    assert harness.calls == []


@pytest.mark.parametrize("operation", ["regenerate", "current_generation"])
def test_d11_review_removed_during_archive_check_blocks_final_current_decision(novel, monkeypatch, operation):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    pointer = project.root / generation.POINTER
    if operation == "current_generation": write(pointer, d11_pointer(project, workspace))
    before = snapshot(project.root)
    original = generation._verify_epub
    def remove_review(current):
        result = original(current)
        (project.root / readiness.REVIEW).unlink(missing_ok=True)
        return result
    monkeypatch.setattr(generation, "_verify_epub", remove_review)
    if operation == "regenerate":
        with pytest.raises(RuntimeError, match="PUBLICATION|UNREADABLE_EVIDENCE"): generation.regenerate(project)
    else:
        assert generation.current_generation(project)["status"] == "STALE"
    before.pop(readiness.REVIEW)
    assert snapshot(project.root) == before and harness.calls == []


def d11_next_audit(novel, monkeypatch):
    """A saved, paid-shaped direct response; no transport or new acquisition."""
    import production_periodic_audit_finalization as finalizer
    from retroactive_span_planning import digest
    project, engine, _, _ = novel
    key = next(iter(engine.glossary["entries"]))
    config = replace(project.config, final_chapter=4)
    monkeypatch.setattr(engine, "ACTIVE_NOVEL_CONFIG", config)
    write(project.config_file, config.to_dict())
    units = history.read_value(project.chapters_file)
    units.append({"chapter": 4, "production_index": 4, "text": key + " 4", "title": "Synthetic",
                  "source_document_sha256": digest(project.source_txt.read_bytes())})
    write(project.chapters_file, units)
    monkeypatch.setattr(engine, "chapters", units)
    monkeypatch.setattr(engine, "chapter_map", {u["chapter"]: u for u in units})
    (project.chapter_dir / "0004.md").write_bytes(b"Chapter 4. Stable\n\nHann returned.\n")
    engine.progress.update(completed=[1, 2, 3, 4], last_completed=4, next_chapter=5)
    engine.save_json(engine.PROGRESS_FILE, engine.progress)
    accepted = {"decisions": [{"chinese": key, "action": "correct", "english": "Hahn"}], "issues": []}
    engine.save_json(engine.periodic_audit_checkpoint_path(4, 4), {"synthetic_saved_paid_response": accepted})
    stats = {field: 0 for field in finalizer.STAT_TOTAL_FIELDS}
    stats.update(prompt_tokens=20, cache_miss_tokens=20, completion_tokens=10, cost_usd=0.0001, api_request_count=1)
    return accepted, stats, {"candidate_count": 1, "candidate_set_hash": canonical_hash("d11 accepted"), "api_request_count": 1}


@pytest.mark.parametrize("published", [False, True])
def test_d11_unrelated_residue_allows_saved_paid_audit_completion(novel, monkeypatch, published):
    import production_periodic_audit_finalization as finalizer
    project, engine, _, harness = novel
    if published:
        workspace = d11_existing_epub(project)
        (project.root / readiness.REVIEW).unlink()  # Audit inventory is not CURRENT certification.
    else:
        with production_run_lock(project.root):
            workspace = prepare_existing_evidence(project)
    directory = project.root / generation.GENERATIONS
    (directory / "stray-directory").mkdir(parents=True)
    (directory / "stray-directory/note.txt").write_bytes(b"unrelated")
    (directory / "stray-file.txt").write_bytes(b"unrelated")
    (directory / ".gitkeep").write_bytes(b"")
    (directory / ("a" * 64)).mkdir()  # A directory name alone is no registration.
    residue = snapshot(directory)
    args = d11_next_audit(novel, monkeypatch)
    with production_run_lock(project.root):
        assert finalizer.finalize_periodic_audit(engine, 4, 4, *args)["status"] == "FINALIZED"
        before = snapshot(project.root)
        assert finalizer.finalize_periodic_audit(engine, 4, 4, *args)["status"] == "ALREADY_FINALIZED"
        assert snapshot(project.root) == before
    assert snapshot(directory) == residue
    captured = finalizer.load_intent(engine, 4, 4)["publication_inputs_before"]
    target = (workspace.publication_final_dir / "editorial_chapters").relative_to(project.root).as_posix()
    assert captured[target]["0001.md"]
    assert not any("stray" in name or ".gitkeep" in name for name in captured)
    assert generation.current_generation(project)["status"] == "NOT_VERIFIED"
    assert not (project.root / generation.POINTER).exists()
    assert engine.progress["last_audited_chapter"] == 4 and harness.calls == []


@pytest.mark.parametrize("damage", ["intent", "hash", "schema", "foreign", "competing", "missing_intent", "proof", "missing_published_proof"])
def test_d11_residue_never_masks_bad_registered_generation(novel, monkeypatch, damage):
    import production_periodic_audit_finalization as finalizer
    project, engine, _, harness = novel
    if damage == "missing_published_proof": workspace = d11_existing_epub(project)
    else:
        with production_run_lock(project.root): workspace = prepare_existing_evidence(project)
    registered = workspace.publication_final_dir.parent
    directory = project.root / generation.GENERATIONS
    (directory / "unrelated").mkdir()
    (directory / ".gitkeep").write_bytes(b"")
    if damage == "intent": (registered / "intent.json").write_bytes(b'{"revision":')
    elif damage == "hash":
        original = history.read_object(registered / "intent.json")
        original["runtime"]["forged.py"] = "0" * 64
        write(registered / "intent.json", original)
    elif damage == "schema":
        original = history.read_object(registered / "intent.json")
        original["approvals"] = {}
        malformed = directory / canonical_hash(original)
        write(malformed / "intent.json", original)
    elif damage == "foreign":
        original = history.read_object(registered / "intent.json")
        original["source"]["novel_id"] = "foreign"
        original["source"]["config"]["id"] = "foreign"
        foreign = directory / canonical_hash(original)
        write(foreign / "intent.json", original)
        write(foreign / "final/reports/source_bound_provenance.json", original)
    elif damage == "competing":
        write(directory / ("f" * 64) / "intent.json", history.read_object(registered / "intent.json"))
    elif damage == "missing_intent": (registered / "intent.json").unlink()
    elif damage == "proof": write(workspace.publication_final_dir / "reports/source_bound_provenance.json", {})
    else: (workspace.publication_final_dir / "reports/source_bound_provenance.json").unlink()
    args = d11_next_audit(novel, monkeypatch)
    before = snapshot(project.root)
    for _ in range(2):
        with production_run_lock(project.root):
            with pytest.raises((RuntimeError, KeyError, TypeError, OSError)):
                finalizer.finalize_periodic_audit(engine, 4, 4, *args)
        assert snapshot(project.root) == before
    assert harness.calls == []


def test_d11_missing_referenced_generation_and_links_fail_closed(novel, monkeypatch):
    project, _, _, _ = novel
    directory = project.root / generation.GENERATIONS
    directory.mkdir(parents=True)
    target = directory / "unrelated.txt"
    target.write_bytes(b"unrelated")
    original = Path.is_symlink
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", lambda p: p == target or original(p))
        with pytest.raises(RuntimeError, match="LINKED_EVIDENCE"):
            history.publication_input_snapshot(project.root)
    # A durable captured target is an obligation even if its root was removed.
    write(project.root / "output/publication_freshness/missing-reference.json",
          {"publication_inputs_before": {generation.GENERATIONS + "/" + "b" * 64 + "/final/editorial_chapters": {"0001.md": "0" * 64}}})
    before = snapshot(project.root)
    with pytest.raises(RuntimeError, match="MISSING_REFERENCED_GENERATION"):
        history.publication_input_snapshot(project.root)
    assert snapshot(project.root) == before

@pytest.mark.parametrize("has_pointer", [False, True])
def test_d11_reviewed_existing_generation_can_be_certified_idempotently(novel, monkeypatch, has_pointer):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    pointer = project.root / generation.POINTER
    if has_pointer: write(pointer, d11_pointer(project, workspace))
    before = snapshot(project.root)
    monkeypatch.setattr(epub, "build_publication_epub", lambda *a, **k: pytest.fail("new EPUB build forbidden"))
    monkeypatch.setattr(generation.editorial, "apply_ready_plan", lambda *a, **k: pytest.fail("new editorial apply forbidden"))
    assert generation.regenerate(project)["status"] == "CURRENT"
    assert generation.current_generation(project)["status"] == "CURRENT"
    after = snapshot(project.root)
    if not has_pointer: after.pop(generation.POINTER)
    assert after == before
    before_repeat = snapshot(project.root)
    assert generation.regenerate(project)["status"] == "CURRENT"
    assert snapshot(project.root) == before_repeat and harness.calls == []


def test_d11_partial_registration_is_captured_without_certification(novel):
    project, _, _, _ = novel
    inputs = generation._inputs(project)
    registered = project.root / generation.GENERATIONS / canonical_hash(inputs)
    write(registered / "intent.json", inputs)
    before = snapshot(project.root)
    captured = history.publication_input_snapshot(project.root)
    assert registered.relative_to(project.root).as_posix() + "/final/editorial_chapters" in captured
    assert generation.current_generation(project)["status"] == "NOT_VERIFIED"
    assert snapshot(project.root) == before


@pytest.mark.parametrize("reference", ["output/publication_generations/" + "b" * 64 + "/../final",
    "Output/Publication_Generations/" + "b" * 64 + "/final/editorial_chapters"])
def test_d11_noncanonical_reference_cannot_drop_generation_obligation(novel, reference):
    project, _, _, _ = novel
    write(project.root / "output/publication_freshness/reference.json",
          {"publication_inputs_before": {reference: {}}})
    before = snapshot(project.root)
    with pytest.raises(RuntimeError, match="INVALID_GENERATION_REFERENCE|MISSING_REFERENCED_GENERATION"):
        history.publication_input_snapshot(project.root)
    assert snapshot(project.root) == before


@pytest.mark.parametrize("marker", ["INTENT.JSON", "SOURCE_BOUND_PROVENANCE.JSON", "PUBLICATION_CURRENT.JSON"])
def test_d11_windows_case_aliases_are_evidence_claims_not_residue(novel, marker):
    project, _, _, _ = novel
    directory = project.root / generation.GENERATIONS
    target = directory / marker if marker == "PUBLICATION_CURRENT.JSON" else directory / ("b" * 64) / marker
    write(target, {})
    before = snapshot(project.root)
    with pytest.raises(RuntimeError): history.publication_input_snapshot(project.root)
    assert snapshot(project.root) == before
