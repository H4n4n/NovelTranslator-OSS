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
    # Regeneration proves its inputs and supplies the exact prepared workspace;
    # do not repeat that complete source proof merely to predict its path.
    workspace = None

    def stop_before_epub(prepared):
        nonlocal workspace
        workspace = prepared
        raise InterruptedError("synthetic pre-EPUB stop")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(epub, "build_publication_epub", stop_before_epub)
        with pytest.raises(InterruptedError, match="synthetic pre-EPUB stop"):
            generation._regenerate_locked(project)
    assert workspace is not None
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


# D2 advisory preflight and immutable historical recertification.
def d2_finish_next_audit(novel, monkeypatch, *, deferred=False):
    import production_periodic_audit_finalization as finalizer
    project, engine, _, _ = novel
    accepted, stats, chunking = d11_next_audit(novel, monkeypatch)
    # Add an independently source-backed identity discovered in chapter 4.
    # Its source also occurs in chapter 1, where the correction must reach.
    # Unlike the inherited D1 helper's forced old-term decision, this candidate
    # is selected by the real first_chapter window policy during reconstruction.
    engine.glossary["entries"]["1"] = {"english": "Old Title", "category": "character",
                                        "status": "provisional", "first_chapter": 4}
    engine.save_json(engine.GLOSSARY_FILE, engine.glossary)
    engine.chapters[-1]["text"] += " 1"
    engine.save_json(engine.CHAPTERS_FILE, engine.chapters)
    (project.chapter_dir / "0004.md").write_bytes(b"Chapter 4. Stable\n\nHann returned with Old Title.\n")
    accepted["decisions"] = [{"chinese": "1", "action": "correct", "english": "Hann" if deferred else "New Identity"}]
    engine.save_json(engine.periodic_audit_checkpoint_path(4, 4), {"synthetic_saved_paid_response": accepted})
    # Remove the inherited fixture's forced-target adapter for this window.
    # The real function's globals belong to this synthetic engine; retrieve it
    # from the inherited closure without substituting an implementation.
    forced = engine.apply_periodic_audit
    original = next(cell.cell_contents for cell in forced.__closure__ if callable(cell.cell_contents))
    monkeypatch.setattr(engine, "apply_periodic_audit", original)
    with production_run_lock(project.root):
        finalizer.finalize_periodic_audit(engine, 4, 4, accepted, stats, chunking)
    return replace(project, config=engine.ACTIVE_NOVEL_CONFIG)


def d2_no_prose(result, project):
    import json
    serialized = json.dumps(result, ensure_ascii=False)
    for text in ("Hann walked", "strete", "expected_english", "anchor_text", "preservation_anchors",
                 "PRIVATE_APPROVAL_ANCHOR", "Hand**", "arrived", "departed"):
        assert text not in serialized
    assert all(key not in serialized for key in ("韓", "한민"))
    assert len(serialized) < 16000
    assert result["publication_authority"] is False and result["provider_calls"] == 0


def test_d2_current_preflight_is_independent_read_only_and_cli_equivalent(novel, monkeypatch, capsys):
    import json
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    before = snapshot(project.root)
    lock = project.root / ".production.lock"
    lock_before = (lock.read_bytes(), lock.stat().st_mtime_ns)
    monkeypatch.setattr(generation, "regenerate", lambda *a: pytest.fail("preflight cannot rebuild"))
    monkeypatch.setattr(generation, "select_project", lambda *a: project)
    result = generation.publication_preflight(project)
    assert result["status"] == "CURRENT"
    assert all(result["diagnostics"][key]["stage_validation"] == "VERIFIED" for key in generation.APPROVALS)
    assert generation.main(["--novel", project.config.id, "--report"]) == 0
    assert json.loads(capsys.readouterr().out) == result
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []
    assert (lock.read_bytes(), lock.stat().st_mtime_ns) == lock_before


def test_d2_preflight_stale_runtime_is_distinct_from_current(novel):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    previous = d11_previous_generation(project, workspace)
    write(project.root / generation.POINTER, d11_pointer(project, previous))
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "STALE"
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_correction_names_every_stale_approval_and_report(novel, monkeypatch):
    project, _, old_report, harness = novel
    project = d2_finish_next_audit(novel, monkeypatch)
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "REVIEW_REQUIRED"
    diag = result["diagnostics"]
    assert diag["whole_history"]["status"] == "STALE"
    assert diag["whole_history"]["path"] == "output/audits/whole_history/" + old_report + ".json"
    assert diag["source_snapshot"]["path"] == generation.AUTHORIZATION
    for key, path in generation.APPROVALS.items():
        assert diag[key]["status"] == "REVIEW_REQUIRED" and diag[key]["path"] == path
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_recertification_creates_only_immutable_report_and_replays(novel, monkeypatch, capsys):
    import json
    from goat_periodic_audit_finalize_plan import local_module
    project, engine, _, harness = novel
    project = d2_finish_next_audit(novel, monkeypatch)
    # The real loader, not the synthetic engine, performs recertification.
    module = local_module(project)
    with pytest.raises(RuntimeError, match="tripwire"):
        module.initialize_production_runtime_state()
    with pytest.raises(RuntimeError, match="tripwire"):
        module.client.chat.completions.create()
    before = snapshot(project.root)
    result = generation.recertify_history(project)
    assert result["status"] == "REVIEW_REQUIRED" and result["history_status"] == "CHECKED"
    assert result["deferred_chapters"] == 0 and result["examined_chapters"] == 4
    report_path = project.root / result["report_path"]
    assert canonical_hash(history.read_object(report_path)) == result["report_sha256"]
    after = snapshot(project.root)
    assert set(after) - set(before) == {result["report_path"]}
    assert all(after[p] == value for p, value in before.items())
    monkeypatch.setattr(generation, "select_project", lambda *a: project)
    assert generation.main(["--novel", project.config.id, "--recertify-history"]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert generation.recertify_history(project) == result
    assert snapshot(project.root) == after and harness.calls == []
    d2_no_prose(result, project)
    # Merely creating a new report never updates authorization or approvals.
    assert generation.publication_preflight(project)["status"] == "REVIEW_REQUIRED"


def test_d2_historical_deferrals_remain_review_required(novel, monkeypatch):
    project, _, _, harness = novel
    project = d2_finish_next_audit(novel, monkeypatch, deferred=True)
    result = generation.recertify_history(project)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["history_status"] == "CHECKED_WITH_DEFERRALS" and result["deferred_chapters"] > 0
    before = snapshot(project.root)
    preflight = generation.publication_preflight(project, result["report_sha256"])
    assert preflight["status"] == "REVIEW_REQUIRED"
    assert preflight["diagnostics"]["whole_history"]["reason_code"] == "HISTORICAL_DEFERRALS_REQUIRE_REVIEW"
    assert generation.recertify_history(project) == result
    d2_no_prose(preflight, project)
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("dependency", ["authorization", "whole_history", *generation.APPROVALS])
def test_d2_missing_dependencies_name_exact_path_without_mutation(novel, dependency):
    project, _, sha, harness = novel
    relative = (generation.AUTHORIZATION if dependency == "authorization" else
                "output/audits/whole_history/" + sha + ".json" if dependency == "whole_history" else
                generation.APPROVALS[dependency])
    (project.root / relative).unlink()
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["diagnostics"][dependency]["path"] == relative
    assert result["diagnostics"][dependency]["reason_code"] == "MISSING_PREREQUISITE"
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["duplicate_approval", "bad_report", "bad_receipt", "source", "pending",
                                     "standalone", "foreign_auth", "unsupported_source", "invalid_pointer"])
def test_d2_invalid_or_unsupported_evidence_blocks_without_prose_or_writes(novel, damage, monkeypatch):
    project, _, sha, harness = novel
    if damage == "duplicate_approval":
        (project.root / generation.APPROVALS["title_plan"]).write_bytes(b'{"PRIVATE_APPROVAL_ANCHOR":1,"PRIVATE_APPROVAL_ANCHOR":2}')
    elif damage == "bad_report":
        (project.audit_dir / "whole_history" / (sha + ".json")).write_bytes(b'{}')
    elif damage == "bad_receipt":
        next((project.root / "output/publication_freshness").glob("*.json")).write_bytes(b'{}')
    elif damage == "source": project.source_txt.write_bytes(b'PRIVATE_APPROVAL_ANCHOR')
    elif damage == "pending": (project.pending_dir / "0004.md").write_bytes(b'paid pending')
    elif damage == "standalone":
        write(project.retroactive_dir / "standalone_lineage/orphan.prepared.json", {})
    elif damage == "foreign_auth":
        auth = history.read_object(project.root / generation.AUTHORIZATION)
        auth["novel_id"] = "foreign"
        write(project.root / generation.AUTHORIZATION, auth)
    elif damage == "unsupported_source":
        project = replace(project, config=replace(project.config, source_type="epub"))
        write(project.config_file, project.config.to_dict())
    else: write(project.root / generation.POINTER, {"generation": "../PRIVATE_APPROVAL_ANCHOR"})
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "BLOCKED"
    if damage == "standalone":
        assert result["diagnostics"]["whole_history"]["reason_code"] == "UNSUPPORTED_STANDALONE_REGENERATION"
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_recertification_honors_lock_and_blocks_drift_before_writes(novel, monkeypatch, capsys):
    import json
    from production_run_lock import ProductionRunLockedError
    project, _, _, harness = novel
    before = snapshot(project.root)
    with production_run_lock(project.root):
        with pytest.raises(ProductionRunLockedError): generation.recertify_history(project)
    assert snapshot(project.root) == before
    project.source_txt.write_bytes(b"changed source")
    before = snapshot(project.root)
    monkeypatch.setattr(generation, "select_project", lambda *a: project)
    assert generation.main(["--novel", project.config.id, "--recertify-history"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["report_shape", "report_count", "report_transactions", "stage_anchor", "foreign_review", "corpus"])
def test_d2_self_hashed_malformed_or_corrupt_current_evidence_is_blocked(novel, damage, monkeypatch):
    project, _, sha, harness = novel
    if damage.startswith("report_"):
        report = history.read_object(project.audit_dir / "whole_history" / (sha + ".json"))
        if damage == "report_shape": report["chapters"] = "PRIVATE_APPROVAL_ANCHOR"
        elif damage == "report_transactions": report["transactions"] = [{}]
        else: report["unchanged_chapters"] += 1
        sha = canonical_hash(report)
        write(project.audit_dir / "whole_history" / (sha + ".json"), report)
        auth = history.read_object(project.root / generation.AUTHORIZATION)
        auth["whole_history_sha256"] = sha
        write(project.root / generation.AUTHORIZATION, auth)
    else:
        workspace = d11_existing_epub(project)
        write(project.root / generation.POINTER, d11_pointer(project, workspace))
        if damage == "stage_anchor":
            def reject(*a, **k):
                raise generation.titles.TitleApplicationError("PRIVATE_APPROVAL_ANCHOR expected English prose")
            monkeypatch.setattr(generation.titles, "prepare_application", reject)
        elif damage == "foreign_review":
            review = history.read_object(project.root / readiness.REVIEW)
            review["novel_id"] = "foreign"
            write(project.root / readiness.REVIEW, review)
        else:
            (workspace.publication_final_dir / "normalized_chapters/0001.md").write_bytes(b"PRIVATE_APPROVAL_ANCHOR")
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "BLOCKED"
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["checkpoint", "backup", "transaction", "progress"])
def test_d2_recertification_refuses_historical_drift_without_report_writes(novel, damage):
    project, _, _, harness = novel
    if damage == "checkpoint":
        (project.audit_dir / "audit_0001_0003.checkpoint.json").write_bytes(b"{}")
    elif damage == "backup":
        next(project.backup_dir.glob("retroactive_*/chapters_before/0001.md")).write_bytes(b"changed backup")
    elif damage == "transaction":
        next((project.retroactive_dir / "transactions").glob("*.plan.json")).write_bytes(b"{}")
    else:
        progress = history.read_object(project.progress_file)
        progress["last_audited_chapter"] = 0
        write(project.progress_file, progress)
    before = snapshot(project.root)
    with pytest.raises((RuntimeError, ValueError, KeyError, TypeError)):
        generation.recertify_history(project)
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_preflight_does_not_create_a_lock_file(novel):
    project, _, _, harness = novel
    lock = project.root / ".production.lock"
    # Fixture setup has released its lock; use a pristine read-only situation.
    lock.unlink()
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "REVIEW_REQUIRED"
    assert not lock.exists() and snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("approval", list(generation.APPROVALS))
def test_d2_rehashed_empty_stage_approval_is_blocked_before_intermediates_exist(novel, approval):
    from retroactive_span_planning import digest
    project, _, _, harness = novel
    path = project.root / generation.APPROVALS[approval]
    write(path, {})
    auth = history.read_object(project.root / generation.AUTHORIZATION)
    auth[approval + "_sha256"] = digest(path.read_bytes())
    write(project.root / generation.AUTHORIZATION, auth)
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "BLOCKED"
    assert result["diagnostics"][approval]["status"] == "BLOCKED"
    assert result["diagnostics"][approval]["path"] == generation.APPROVALS[approval]
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_missing_original_source_names_precise_path(novel):
    project, _, _, harness = novel
    project.source_txt.unlink()
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["status"] == "BLOCKED"
    assert result["diagnostics"]["whole_history"]["path"] == project.source_txt.relative_to(project.root).as_posix()
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["glossary_duplicates", "transaction_duplicates", "utf16_issues", "boolean_progress"])
def test_d2_recertification_rejects_ambiguous_json_before_saving(novel, damage, monkeypatch, capsys):
    import json
    project, _, sha, harness = novel
    # A last-value-wins parser would see the same authoritative state despite
    # contradictory duplicate fields. An absent report makes a mistaken save
    # observable, rather than merely checking replay against an existing file.
    (project.audit_dir / "whole_history" / (sha + ".json")).unlink()
    if damage.endswith("duplicates"):
        path = (project.glossary_file if damage == "glossary_duplicates" else
                next((project.retroactive_dir / "transactions").glob("*.plan.json")))
        value = history.read_object(path)
        key = next(iter(value))
        path.write_text("{" + json.dumps(key) + ":null," + json.dumps(value)[1:], encoding="utf-8")
    elif damage == "utf16_issues":
        value = history.read_object(project.issues_file)
        project.issues_file.write_bytes(json.dumps(value).encode("utf-16"))
    else:
        value = history.read_object(project.progress_file)
        value["completed"][0] = True
        write(project.progress_file, value)
    before = snapshot(project.root)
    monkeypatch.setattr(generation, "select_project", lambda *a: project)
    assert generation.main(["--novel", project.config.id, "--recertify-history"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and result["provider_calls"] == 0
    preflight = generation.publication_preflight(project)
    assert preflight["status"] == "BLOCKED"
    d2_no_prose(preflight, project)
    assert snapshot(project.root) == before and harness.calls == []


@pytest.fixture(params=["zh", "ko"])
def d2_recovery_novel(aut1_engine, monkeypatch, request):
    """Public initial history with an earlier body occurrence, before any proof."""
    from types import SimpleNamespace
    import production_periodic_audit_finalization as finalizer
    original = finalizer.finalize_periodic_audit
    def initial_body(engine, start, end, accepted, stats, chunking):
        if (start, end) == (1, 3):
            path = Path(engine.OUTPUT_DIR) / "0001.md"
            path.write_bytes(path.read_bytes().replace(b"Old Title", b"Stable Beginning", 1) + b"The envoy met Old Title.\r\n")
        return original(engine, start, end, accepted, stats, chunking)
    with monkeypatch.context() as patch:
        patch.setattr(finalizer, "finalize_periodic_audit", initial_body)
        import test_build_publication_epub as inherited
        fixture = inherited.title_fixture
        patch.setattr(inherited, "title_fixture", lambda *a, **kw: fixture(*a, current="Stable Beginning", **kw))
        # Call the existing public fixture's constructor, with its real owners.
        result = novel.__wrapped__(aut1_engine, monkeypatch,
            SimpleNamespace(param=request.param, node=request.node))
    # Explicit initial decisions: canonicalize only the heading, preserve body.
    project = result[0]
    from test_apply_title_resolution_plan import _fixture as title_fixture
    directory = project.root / "explicit_initial_title_review"
    directory.mkdir()
    first = (project.chapter_dir / "0001.md").read_bytes()
    _, title_plan = title_fixture(directory, heading=first.decode().splitlines()[0], current="Stable Beginning")
    title_plan["corpora"]["normalized_title_base"] = generation.titles.corpus_fingerprint(project.chapter_dir, 3)
    from retroactive_span_planning import digest
    title_plan["title_decisions"][0]["application_anchors"][0]["baseline_normalized_chapter_file_sha256"] = digest(first)
    write(project.root / generation.APPROVALS["title_plan"], title_plan)
    outputs = []
    for path in sorted(project.chapter_dir.glob("*.md")):
        raw = path.read_bytes()
        if path.stem == "0001":
            raw = raw.replace(b"Stable Beginning", b"New Title", 1)
        outputs.append((path.name, generation.cleanup.transform_unit(int(path.stem), path, raw).output_bytes))
    fp = generation.cleanup.framed_corpus_fingerprint(outputs)
    manifest = history.read_object(project.root / generation.APPROVALS["editorial_authorization"])
    manifest["immutable_publication_checkpoint"] = {k: fp[k] for k in ("file_count", "aggregate_byte_count", "aggregate_sha256")}
    write(project.root / generation.APPROVALS["editorial_authorization"], manifest)
    from retroactive_span_planning import digest
    auth = history.read_object(project.root / generation.AUTHORIZATION)
    auth.update({k + "_sha256": digest((project.root / p).read_bytes()) for k, p in generation.APPROVALS.items()})
    write(project.root / generation.AUTHORIZATION, auth)
    return result


def d2_synthetic_reissue(project, report_sha):
    """Explicit test-only decisions; never an operator approval generator.

    Review: preserve the historical correction in body/title; normalization
    introduces no edits, titles retain the corrected name, editorial fixes only
    the witnessed chapter-3 typo and keeps the chapter-2 heading.
    """
    from retroactive_span_planning import digest
    from test_apply_title_resolution_plan import _fixture as title_fixture
    normal = generation.normalization
    cleanup = generation.cleanup
    raw = (project.chapter_dir / "0001.md").read_bytes()
    assert b"The envoy met New Identity." in raw and b"Old Title" not in raw
    fp = normal.corpus_fingerprint(project.chapter_dir)
    prior = history.read_object(project.root / generation.APPROVALS["normalization_plan"])
    normal_plan = {"schema_version": 3, "plan_revision": "2.1",
        "corpus": {"project_id": project.config.id, "inventory_recorded_final_sha256": fp["aggregate_sha256"],
                   "verified_final_fingerprint": fp},
        "safety": prior["safety"], "application_policy": prior["application_policy"],
        "text_decisions": [], "pronoun_decisions": [], "summary_counts": prior["summary_counts"]}
    directory = project.root / "explicit_synthetic_title_review"
    directory.mkdir()
    _, title_plan = title_fixture(directory, heading=raw.decode().splitlines()[0],
        body="The envoy met New Identity.", current="Stable Beginning", decision="ALREADY_CONSISTENT",
        canonical_english_title_text="Stable Beginning")
    title_plan["corpora"]["normalized_title_base"] = generation.titles.corpus_fingerprint(project.chapter_dir, 4)
    outputs = [(p.name, cleanup.transform_unit(int(p.stem), p, p.read_bytes()).output_bytes)
               for p in sorted(project.chapter_dir.glob("*.md"))]
    fp = cleanup.framed_corpus_fingerprint(outputs)
    source = project.source_txt.read_bytes()
    text = source.decode().splitlines()[2]
    manifest = {"schema_version": 1, "revision": "explicit-synthetic-recovery-review-v1", "novel": project.config.id,
        "immutable_publication_checkpoint": {k: fp[k] for k in ("file_count", "aggregate_byte_count", "aggregate_sha256")},
        "korean_source": {"byte_count": len(source), "sha256": digest(source), "preservation_anchors": []},
        "authorized_set": {"operation_count": 1, "heading_operation_count": 0, "prose_operation_count": 1, "affected_units": [3]},
        "edits": [{"edit_id": "reviewed-chapter-three-typo", "unit": 3, "edit_category": "PROSE_TYPO", "scope": "prose",
            "expected_english": "strete", "replacement": "street", "source_evidence": [{"anchor_text": text,
                "anchor_sha256": digest(text.encode()), "source_locator": {"line_number_hint": 3}}]}],
        "preservation_guards": [{"guard_id": "reviewed-keep-title", "units": [2],
                                 "expected_heading_lines": {"2": ["Chapter 2. Stable", ""]}}]}
    for key, value in (("normalization_plan", normal_plan), ("title_plan", title_plan), ("editorial_authorization", manifest)):
        write(project.root / generation.APPROVALS[key], value)
    authorization = {"revision": generation.REVISION, "novel_id": project.config.id, "enabled": True,
        "finalization": "committed-identity-v1", "allow_manual_approved": False, "whole_history_sha256": report_sha,
        "source_snapshot_sha256": canonical_hash(generation.source_snapshot(project, report_sha))}
    authorization.update({k + "_sha256": digest((project.root / p).read_bytes()) for k, p in generation.APPROVALS.items()})
    write(project.root / generation.AUTHORIZATION, authorization)


def d2_current_then_correct(novel, monkeypatch):
    project, engine, _, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    assert generation.current_generation(project)["status"] == "CURRENT"
    preserved = snapshot(workspace.publication_final_dir.parent)
    pointer = (project.root / generation.POINTER).read_bytes()
    project = d2_finish_next_audit(novel, monkeypatch)
    assert b"The envoy met New Identity." in (project.chapter_dir / "0001.md").read_bytes()
    assert generation.current_generation(project)["status"] == "STALE"
    assert snapshot(workspace.publication_final_dir.parent) == preserved
    assert (project.root / generation.POINTER).read_bytes() == pointer
    return project, workspace, preserved, pointer


def test_d2_approval_handoff_complete_recovery_to_corrected_epub(d2_recovery_novel, monkeypatch):
    import zipfile
    from retroactive_span_planning import digest
    project, old, preserved, pointer = d2_current_then_correct(d2_recovery_novel, monkeypatch)
    before = snapshot(project.root)
    unavailable = generation.publication_preflight(project)
    assert unavailable["approval_review"]["source_changes"]["reason_code"] == "CURRENT_SOURCE_EVIDENCE_UNAVAILABLE"
    assert snapshot(project.root) == before
    recertified = generation.recertify_history(project)
    assert recertified["history_status"] == "CHECKED"
    before = snapshot(project.root)
    packet = generation.publication_preflight(project, recertified["report_sha256"])
    changes = packet["approval_review"]["source_changes"]
    assert changes["status"] == "AVAILABLE", changes
    assert changes["changed_chapters"] == [1, 4]
    assert "data/glossary.json" in changes["changed_evidence_paths"]
    assert packet["diagnostics"]["normalization_plan"]["decision_reissuance_required"] is True
    assert all(packet["diagnostics"][k]["approval_bytes_valid"] is True for k in generation.APPROVALS)
    actions = packet["approval_review"]["required_operator_actions"]
    assert [a["order"] for a in actions] == list(range(1, len(actions) + 1))
    assert {generation.AUTHORIZATION, readiness.REVIEW, *generation.APPROVALS.values()} <= {a["path"] for a in actions}
    d2_no_prose(packet, project)
    assert snapshot(project.root) == before
    old_review = (project.root / readiness.REVIEW).read_bytes()
    d2_synthetic_reissue(project, recertified["report_sha256"])
    # An actual interruption at a real owner boundary retains recoverable output.
    publish_titles = generation.titles.publish_titled_corpus
    with monkeypatch.context() as patch:
        def interrupt_titles(*args, **kwargs):
            publish_titles(*args, **kwargs)
            raise InterruptedError("synthetic after title publication")
        patch.setattr(generation.titles, "publish_titled_corpus", interrupt_titles)
        with pytest.raises(InterruptedError, match="synthetic after title"):
            generation.regenerate(project)
    assert (project.root / generation.POINTER).read_bytes() == pointer
    assert snapshot(old.publication_final_dir.parent) == preserved
    # Resume the same engine; complete candidate exists before deriving its graph.
    with pytest.raises(RuntimeError, match="PUBLICATION_REVIEW_STALE"):
        generation.regenerate(project)
    inputs = generation._inputs(project)
    candidate = generation.PublicationWorkspace(project, project.root / generation.GENERATIONS / canonical_hash(inputs) / "final")
    generation.verify_generation_corpus(project, candidate.publication_final_dir)
    assert (candidate.publication_final_dir / "editorial_chapters/0001.md").is_file()
    assert (project.root / readiness.REVIEW).read_bytes() == old_review
    with pytest.raises(RuntimeError, match="PUBLICATION_REVIEW_STALE"):
        readiness.require_publication_ready(candidate)
    with pytest.raises(RuntimeError, match="PUBLICATION_REVIEW_REQUIRED"):
        with monkeypatch.context() as patch:
            patch.setattr(readiness, "REVIEW", "config/synthetic_missing_review.json")
            readiness.require_publication_ready(candidate)
    graph = explicit_synthetic_review(candidate)
    assert graph == readiness.require_publication_ready(candidate)
    result = generation.regenerate(project)
    assert result["status"] == generation.current_generation(project)["status"] == "CURRENT"
    assert result["generation"] != old.publication_final_dir.parent.name
    with zipfile.ZipFile(result["epub"]) as archive:
        chapter = [n for n in archive.namelist() if "unit_0001" in n and n.endswith(".xhtml")]
        assert len(chapter) == 1
        text = archive.read(chapter[0]).decode()
        assert "The envoy met New Identity." in text and "Old Title" not in text
    new_pointer = history.read_object(project.root / generation.POINTER)
    assert new_pointer["generation"] == result["generation"]
    assert new_pointer["epub_sha256"] == digest(Path(result["epub"]).read_bytes())
    assert snapshot(old.publication_final_dir.parent) == preserved
    assert generation.regenerate(project) == result
    assert d2_recovery_novel[3].calls == []
    # Retained, public-only evidence for independent archive inspection.
    write(project.root / "synthetic_recovery_proof.json", {
        "old_generation": old.publication_final_dir.parent.name, "new_generation": result["generation"],
        "epub": result["epub"], "epub_sha256": new_pointer["epub_sha256"],
        "old_generation_bytes_and_mtimes_unchanged": True,
        "old_inventory_sha256": canonical_hash({p: digest(value[0]) for p, value in preserved.items()}),
        "corrected_chapter": 1, "archive_content_verified": True, "provider_calls": 0,
        "candidate_graph_sha256": canonical_hash(graph), "status": "CURRENT"})
    write(project.root / "synthetic_approval_handoff.json", packet)


@pytest.mark.parametrize("damage", ["rehash_old_decisions", "authorization_hashes_only", "wrong_normalization_corpus", "wrong_title_corpus", "rebased_title_inventory"])
def test_d2_recovery_refuses_invalid_decisions_preserving_current(d2_recovery_novel, monkeypatch, damage):
    from retroactive_span_planning import digest
    project, old, preserved, pointer = d2_current_then_correct(d2_recovery_novel, monkeypatch)
    report = generation.recertify_history(project)
    auth = history.read_object(project.root / generation.AUTHORIZATION)
    auth.update(whole_history_sha256=report["report_sha256"],
                source_snapshot_sha256=canonical_hash(generation.source_snapshot(project, report["report_sha256"])))
    old_title = history.read_object(project.root / generation.APPROVALS["title_plan"])
    if damage.startswith("wrong_") or damage == "rebased_title_inventory":
        d2_synthetic_reissue(project, report["report_sha256"])
        auth = history.read_object(project.root / generation.AUTHORIZATION)
        key = "normalization_plan" if damage == "wrong_normalization_corpus" else "title_plan"
        path = project.root / generation.APPROVALS[key]
        plan = history.read_object(path)
        corpus = plan["corpus"]["verified_final_fingerprint"] if key == "normalization_plan" else plan["corpora"]["normalized_title_base"]
        if damage == "rebased_title_inventory":
            # Full new corpus inventory still cannot repair old exact title
            # anchors or turn the old semantic choice into a fresh decision.
            old_title["corpora"] = plan["corpora"]
            plan = old_title
        else:
            corpus["aggregate_sha256"] = "0" * 64
        write(path, plan)
    elif damage == "rehash_old_decisions":
        for key, path in generation.APPROVALS.items():
            target = project.root / path
            target.write_bytes(target.read_bytes() + b"\n")
    auth.update({k + "_sha256": digest((project.root / p).read_bytes()) for k, p in generation.APPROVALS.items()})
    write(project.root / generation.AUTHORIZATION, auth)
    with pytest.raises((RuntimeError, ValueError)):
        generation.regenerate(project)
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    key = "title_plan" if damage in {"wrong_title_corpus", "rebased_title_inventory"} else "normalization_plan"
    assert result["diagnostics"][key]["decision_binding"] == "INVALID"
    assert result["diagnostics"][key]["decision_reissuance_required"] is True
    d2_no_prose(result, project)
    assert snapshot(project.root) == before
    assert (project.root / generation.POINTER).read_bytes() == pointer
    assert snapshot(old.publication_final_dir.parent) == preserved
    assert d2_recovery_novel[3].calls == []


def test_d2_approval_handoff_history_only_reuses_independently_valid_decisions(novel):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    # Evidence-only drift in a node that no stage approval embeds.
    write(project.continuity_file, {"summary": "synthetic independently reviewed continuity"})
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    assert result["approval_review"]["source_changes"]["changed_chapters"] == []
    assert result["approval_review"]["source_changes"]["changed_evidence_paths"] == ["data/continuity.json"]
    for key in generation.APPROVALS:
        row = result["diagnostics"][key]
        assert row["approval_bytes_valid"] is True and row["decision_binding"] == "VALID"
        assert row["decision_reissuance_required"] is False
        assert row["semantic_review"] == "NOT_PERFORMED"
    assert not any(a["path"] in generation.APPROVALS.values()
                   for a in result["approval_review"]["required_operator_actions"])
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["malformed_intent", "malformed_source", "corrupt_corpus_fingerprint", "missing_pointer", "unsafe_evidence_path"])
def test_d2_approval_handoff_unavailable_prior_evidence_is_read_only(novel, damage):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    if damage == "missing_pointer":
        (project.root / generation.POINTER).unlink()
    elif damage == "malformed_intent":
        (workspace.publication_final_dir.parent / "intent.json").write_bytes(b'{"source":')
    else:
        original = history.read_object(workspace.publication_final_dir.parent / "intent.json")
        if damage == "malformed_source":
            original["source"]["chapters"] = {"file_count": True}
        elif damage == "corrupt_corpus_fingerprint":
            original["source"]["chapters"]["aggregate_sha256"] = "0" * 64
        else:
            original["source"]["evidence"]["PRIVATE_APPROVAL_ANCHOR"] = "1" * 64
        # Keep hashes mutually consistent so comparison must inspect structure.
        import shutil
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
        paths["report_md"].write_bytes(epub.render_markdown_report(report).encode())
        write(project.root / generation.POINTER, d11_pointer(project, previous))
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    changes = result["approval_review"]["source_changes"]
    assert changes["status"] == "UNAVAILABLE"
    assert changes["changed_chapters"] is None and changes["changed_evidence_paths"] is None
    assert changes["reason_code"] == ("NO_PREVIOUS_GENERATION" if damage == "missing_pointer" else "PREVIOUS_GENERATION_EVIDENCE_INVALID")
    if damage in {"malformed_intent", "malformed_source", "corrupt_corpus_fingerprint"}:
        assert result["status"] == "BLOCKED"
        with pytest.raises((RuntimeError, ValueError, KeyError)):
            generation.regenerate(project)
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("damage", ["standalone", "malformed_prior", "missing_review", "stale_review"])
def test_d2_handoff_refusal_preserves_selected_generation_and_pointer(novel, damage):
    project, _, _, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    assert generation.current_generation(project)["status"] == "CURRENT"
    if damage == "standalone":
        write(project.retroactive_dir / "standalone_lineage/orphan.prepared.json", {})
    elif damage == "malformed_prior":
        (workspace.publication_final_dir.parent / "intent.json").write_bytes(b'{"source":')
    elif damage == "missing_review":
        (project.root / readiness.REVIEW).unlink()
    else:
        review = history.read_object(project.root / readiness.REVIEW)
        review["graph_sha256"] = "0" * 64
        write(project.root / readiness.REVIEW, review)
    before = snapshot(project.root)
    with pytest.raises((RuntimeError, ValueError)):
        generation.regenerate(project)
    result = generation.publication_preflight(project)
    assert result["status"] in {"BLOCKED", "REVIEW_REQUIRED"}
    if damage == "standalone":
        assert result["diagnostics"]["whole_history"]["reason_code"] == "UNSUPPORTED_STANDALONE_REGENERATION"
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []


def test_d2_evidence_only_drift_never_preserves_invalid_embedded_binding(novel):
    from retroactive_span_planning import digest
    project, _, report, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    write(project.continuity_file, {"summary": "synthetic changed evidence"})
    path = project.root / generation.APPROVALS["title_plan"]
    plan = history.read_object(path)
    plan["corpora"]["normalized_title_base"]["aggregate_sha256"] = "0" * 64
    write(path, plan)
    auth = history.read_object(project.root / generation.AUTHORIZATION)
    auth["source_snapshot_sha256"] = canonical_hash(generation.source_snapshot(project, report))
    auth["title_plan_sha256"] = digest(path.read_bytes())
    write(project.root / generation.AUTHORIZATION, auth)
    before = snapshot(project.root)
    result = generation.publication_preflight(project)
    row = result["diagnostics"]["title_plan"]
    assert row["approval_bytes_valid"] is True
    assert row["decision_binding"] == "INVALID" and row["decision_reissuance_required"] is True
    downstream = result["diagnostics"]["editorial_authorization"]
    assert downstream["decision_binding"] == "NOT_VERIFIED" and downstream["decision_reissuance_required"] is None
    assert downstream["binding_reason_code"] == "UPSTREAM_STAGE_INPUT_UNAVAILABLE"
    assert any(a["path"] == generation.AUTHORIZATION for a in result["approval_review"]["required_operator_actions"])
    assert result["approval_review"]["source_changes"]["changed_chapters"] == []
    assert snapshot(project.root) == before and harness.calls == []


@pytest.mark.parametrize("missing", ["title_plan", "authorization"])
def test_d2_handoff_missing_dependency_does_not_invalidate_unrelated_decisions(novel, missing):
    project, _, report, harness = novel
    workspace = d11_existing_epub(project)
    write(project.root / generation.POINTER, d11_pointer(project, workspace))
    path = generation.AUTHORIZATION if missing == "authorization" else generation.APPROVALS[missing]
    (project.root / path).unlink()
    before = snapshot(project.root)
    result = generation.publication_preflight(project, report)
    row = result["diagnostics"]["normalization_plan"]
    if missing == "title_plan":
        assert row["approval_bytes_valid"] is True
        assert row["decision_binding"] == "VALID" and row["decision_reissuance_required"] is False
        assert result["diagnostics"]["editorial_authorization"]["decision_binding"] == "NOT_VERIFIED"
    else:
        assert row["decision_binding"] == "NOT_VERIFIED" and row["decision_reissuance_required"] is None
        assert row["binding_reason_code"] == "REGENERATION_AUTHORIZATION_UNAVAILABLE"
    d2_no_prose(result, project)
    assert snapshot(project.root) == before and harness.calls == []
