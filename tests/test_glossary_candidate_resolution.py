import copy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import resolve_glossary_candidate_conflict as cli
from glossary_candidate_resolution import (
    GlossaryCandidateResolutionError,
    apply_candidate_conflict_resolutions,
    candidate_resolution_path,
    candidate_resolution_record_sha256,
    create_candidate_resolution,
    load_candidate_resolution_record,
    pending_content_identity,
    state_extraction_identity,
    validate_candidate_resolution_record,
)
from glossary_integrity import (
    GlossaryCandidateIdentityError,
    canonicalize_glossary_review_candidates,
    glossary_candidate_semantic_fingerprint,
    stable_glossary_candidate_id,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def candidate(
    source="小金鼠香煙",
    english="Little Golden Mouse Cigarettes",
    category="consumable",
    *,
    chapter=103,
    notes="Approved consumable evidence.",
):
    candidate_id = stable_glossary_candidate_id(source)
    return {
        "chinese": source,
        "english": english,
        "category": category,
        "status": "provisional",
        "notes": notes,
        "candidate_id": candidate_id,
        "source_provenance": {
            "origin": "chapter_state_extraction",
            "chapter": chapter,
            "verification": "exact_literal",
            "candidate_id": candidate_id,
            "span_start": 30,
            "span_end": 35,
        },
    }


def conflict_candidates(source="小金鼠香煙", *, chapter=103):
    approved = candidate(source, category="consumable", chapter=chapter)
    rejected = candidate(
        source,
        category="item",
        chapter=chapter,
        notes="Rejected broader item evidence.",
    )
    return [approved, rejected]


def conflict_for(candidates):
    with pytest.raises(GlossaryCandidateIdentityError) as caught:
        canonicalize_glossary_review_candidates(candidates)
    return caught.value.diagnostics["conflicts"][0]


def identities(candidates):
    state = {
        "new_terms": [
            {
                key: copy.deepcopy(value)
                for key, value in item.items()
                if key not in {"candidate_id", "source_provenance"}
            }
            for item in candidates
        ],
        "continuity_summary": "Synthetic state.",
        "issues": [],
    }
    return pending_content_identity("Paid target."), state_extraction_identity(state)


def resolution(tmp_path, candidates=None, *, novel_id="synthetic", chapter=103):
    candidates = candidates or conflict_candidates(chapter=chapter)
    conflict = conflict_for(candidates)
    pending_identity, state_identity = identities(candidates)
    selected = glossary_candidate_semantic_fingerprint(candidates[0])
    path = tmp_path / "resolutions" / f"{chapter:04d}.json"
    created = create_candidate_resolution(
        path,
        novel_id=novel_id,
        chapter=chapter,
        pending_identity=pending_identity,
        state_identity=state_identity,
        conflict=conflict,
        selected_semantic_fingerprint=selected,
        rationale="Explicit source metadata identifies a consumable.",
        timestamp_utc="2026-08-29T20:00:00+00:00",
    )
    return candidates, created, pending_identity, state_identity


def test_no_resolution_keeps_true_conflict_fail_closed():
    candidates = conflict_candidates()
    pending_identity, state_identity = identities(candidates)
    with pytest.raises(GlossaryCandidateIdentityError):
        apply_candidate_conflict_resolutions(
            candidates,
            None,
            novel_id="synthetic",
            chapter=103,
            expected_pending_identity=pending_identity,
            expected_state_identity=state_identity,
        )


def test_exact_chapter_103_shape_resolves_by_semantic_fingerprint(tmp_path):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    result = apply_candidate_conflict_resolutions(
        candidates,
        created["record"],
        novel_id="synthetic",
        chapter=103,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    assert len(result["candidates"]) == 1
    selected = result["candidates"][0]
    assert selected["category"] == "consumable"
    assert selected["english"] == "Little Golden Mouse Cigarettes"
    assert "item" not in selected.values()
    assert selected["notes"] == "Approved consumable evidence."
    audit_proposals = created["record"]["resolutions"][0][
        "conflicting_proposals"
    ]
    assert {item["notes"] for item in audit_proposals} == {
        "Approved consumable evidence.",
        "Rejected broader item evidence.",
    }


def test_candidate_order_changes_but_conflict_and_resolution_stay_valid(tmp_path):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    reordered = list(reversed(candidates))
    reordered_pending, reordered_state = identities(reordered)
    assert reordered_pending == pending_identity
    assert reordered_state == state_identity
    assert (
        conflict_for(reordered)["conflict_fingerprint"]
        == conflict_for(candidates)["conflict_fingerprint"]
    )
    result = apply_candidate_conflict_resolutions(
        reordered,
        created["record"],
        novel_id="synthetic",
        chapter=103,
        expected_pending_identity=reordered_pending,
        expected_state_identity=reordered_state,
    )
    assert result["candidates"][0]["category"] == "consumable"


@pytest.mark.parametrize(
    "override, code",
    [
        ({"novel_id": "other"}, "resolution_novel_mismatch"),
        ({"chapter": 104}, "resolution_chapter_mismatch"),
        (
            {"expected_pending_identity": "0" * 64},
            "resolution_pending_identity_mismatch",
        ),
        (
            {"expected_state_identity": "1" * 64},
            "resolution_state_identity_mismatch",
        ),
    ],
)
def test_exact_scope_mismatches_fail_closed(tmp_path, override, code):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    expected = {
        "novel_id": "synthetic",
        "chapter": 103,
        "expected_pending_identity": pending_identity,
        "expected_state_identity": state_identity,
    }
    expected.update(override)
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        apply_candidate_conflict_resolutions(
            candidates,
            created["record"],
            **expected,
        )
    assert caught.value.code == code


def test_changed_semantic_proposal_makes_old_resolution_stale(tmp_path):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    changed = copy.deepcopy(candidates)
    changed[1]["category"] = "artifact"
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        apply_candidate_conflict_resolutions(
            changed,
            created["record"],
            novel_id="synthetic",
            chapter=103,
            expected_pending_identity=pending_identity,
            expected_state_identity=state_identity,
        )
    assert caught.value.code == "resolution_conflict_fingerprint_mismatch"


def test_changed_source_concept_does_not_receive_old_resolution(tmp_path):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    changed = conflict_candidates("다른 담배")
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        apply_candidate_conflict_resolutions(
            changed,
            created["record"],
            novel_id="synthetic",
            chapter=103,
            expected_pending_identity=pending_identity,
            expected_state_identity=state_identity,
        )
    assert caught.value.code == "resolution_conflict_fingerprint_mismatch"


def test_malformed_and_tampered_records_fail_closed(tmp_path):
    candidates, created, pending_identity, state_identity = resolution(tmp_path)
    malformed = copy.deepcopy(created["record"])
    malformed["unsupported"] = True
    with pytest.raises(GlossaryCandidateResolutionError) as malformed_error:
        validate_candidate_resolution_record(malformed)
    assert malformed_error.value.code == "resolution_schema_malformed"

    tampered = copy.deepcopy(created["record"])
    tampered["resolutions"][0]["rationale"] = "Unrecorded change."
    with pytest.raises(GlossaryCandidateResolutionError) as tampered_error:
        validate_candidate_resolution_record(tampered)
    assert tampered_error.value.code == "resolution_record_hash_mismatch"


def test_selected_fingerprint_absent_fails_closed():
    conflict = conflict_for(conflict_candidates())
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        create_candidate_resolution(
            Path("not-written.json"),
            novel_id="synthetic",
            chapter=103,
            pending_identity="0" * 64,
            state_identity="1" * 64,
            conflict=conflict,
            selected_semantic_fingerprint="2" * 64,
            rationale="Invalid selection.",
        )
    assert caught.value.code == "resolution_selected_fingerprint_absent"
    assert not Path("not-written.json").exists()


def test_duplicate_contradictory_resolutions_fail_closed(tmp_path):
    candidates, created, _, _ = resolution(tmp_path)
    record = copy.deepcopy(created["record"])
    second = copy.deepcopy(record["resolutions"][0])
    rejected_fingerprint = glossary_candidate_semantic_fingerprint(candidates[1])
    rejected_proposal = next(
        item for item in second["conflicting_proposals"]
        if item["semantic_identity_fingerprint"] == rejected_fingerprint
    )
    second["selected_semantic_fingerprint"] = rejected_fingerprint
    second["approved_resolution"] = {
        key: rejected_proposal[key] for key in ("english", "category", "status")
    }
    record["resolutions"].append(second)
    record["record_sha256"] = candidate_resolution_record_sha256(record)
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        validate_candidate_resolution_record(record)
    assert caught.value.code == "duplicate_conflict_resolutions"


def test_identical_creation_is_idempotent_and_overwrite_is_blocked(tmp_path):
    candidates, first, pending_identity, state_identity = resolution(tmp_path)
    conflict = conflict_for(candidates)
    path = first["path"]
    before = path.read_bytes()
    same = create_candidate_resolution(
        path,
        novel_id="synthetic",
        chapter=103,
        pending_identity=pending_identity,
        state_identity=state_identity,
        conflict=conflict,
        selected_semantic_fingerprint=glossary_candidate_semantic_fingerprint(
            candidates[0]
        ),
        rationale="Explicit source metadata identifies a consumable.",
        timestamp_utc="2026-08-30T00:00:00+00:00",
    )
    assert same["status"] == "unchanged"
    assert path.read_bytes() == before
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        create_candidate_resolution(
            path,
            novel_id="synthetic",
            chapter=103,
            pending_identity=pending_identity,
            state_identity=state_identity,
            conflict=conflict,
            selected_semantic_fingerprint=glossary_candidate_semantic_fingerprint(
                candidates[1]
            ),
            rationale="Contradictory approval.",
        )
    assert caught.value.code == "resolution_overwrite_blocked"
    assert path.read_bytes() == before


def test_normal_and_true_duplicate_batches_need_no_resolution():
    normal = [candidate("術式", "Formal Art", "technique")]
    normal_result = apply_candidate_conflict_resolutions(
        normal,
        None,
        novel_id="synthetic",
        chapter=103,
        expected_pending_identity="0" * 64,
        expected_state_identity="1" * 64,
    )
    assert len(normal_result["candidates"]) == 1
    duplicate_result = apply_candidate_conflict_resolutions(
        [normal[0], copy.deepcopy(normal[0])],
        None,
        novel_id="synthetic",
        chapter=103,
        expected_pending_identity="0" * 64,
        expected_state_identity="1" * 64,
    )
    assert len(duplicate_result["candidates"]) == 1


@pytest.mark.parametrize("source", ["同一術式", "동일 술식"])
def test_chinese_and_korean_conflicts_use_same_resolution_layer(tmp_path, source):
    candidates = conflict_candidates(source)
    candidates, created, pending_identity, state_identity = resolution(
        tmp_path, candidates
    )
    result = apply_candidate_conflict_resolutions(
        candidates,
        created["record"],
        novel_id="synthetic",
        chapter=103,
        expected_pending_identity=pending_identity,
        expected_state_identity=state_identity,
    )
    assert result["candidates"][0]["chinese"] == source
    assert result["candidates"][0]["category"] == "consumable"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def synthetic_cli_project(tmp_path):
    repo = tmp_path / "repo"
    project = repo / "novels" / "synthetic"
    shutil.copytree(REPO_ROOT / "profiles" / "infinite_flow", repo / "profiles" / "infinite_flow")
    _write_json(
        project / "novel.json",
        {
            "id": "synthetic",
            "title": "Synthetic",
            "source_language": "zh-Hant",
            "target_language": "en",
            "profile": "infinite_flow",
            "final_chapter": 1,
        },
    )
    source = "【小金鼠香煙】\n\n類別：消耗品"
    _write_json(
        project / "data" / "chapters.json",
        [{"chapter": 1, "heading": "第一章", "text": source}],
    )
    _write_json(project / "data" / "glossary.json", {"entries": {}})
    progress = {"completed": [], "last_completed": 0, "next_chapter": 1}
    _write_json(project / "data" / "progress.json", progress)
    state = {
        "new_terms": [
            {
                "chinese": "小金鼠香煙",
                "english": "Little Golden Mouse Cigarettes",
                "category": "consumable",
                "status": "provisional",
                "notes": "Explicit consumable.",
            },
            {
                "chinese": "小金鼠香煙",
                "english": "Little Golden Mouse Cigarettes",
                "category": "item",
                "status": "provisional",
                "notes": "Broader item.",
            },
        ],
        "continuity_summary": "Synthetic.",
        "issues": [],
    }
    pending_md = project / "output" / "pending" / "0001.md"
    pending_md.parent.mkdir(parents=True)
    pending_md.write_text("【Little Golden Mouse Cigarettes】\n\nCategory: Consumable", encoding="utf-8")
    _write_json(
        project / "output" / "pending" / "0001.json",
        {"chapter": 1, "stage": "state_extracted", "state": state},
    )
    return repo, project


def test_cli_is_zero_api_atomic_idempotent_and_preserves_pending(tmp_path, capsys):
    repo, project = synthetic_cli_project(tmp_path)
    args = SimpleNamespace(
        novel="synthetic",
        chapter=1,
        source="小金鼠香煙",
        choose="",
        rationale="Source explicitly labels this as a consumable.",
        repo_root=repo,
    )
    reconstructed = cli.reconstruct_candidate_conflicts(
        cli.load_novel_project("synthetic", repo), 1
    )
    conflict = reconstructed["conflicts"][0]
    args.choose = next(
        item["semantic_identity_fingerprint"]
        for item in conflict["proposals"]
        if item["category"] == "consumable"
    )
    protected = [
        project / "output" / "pending" / "0001.md",
        project / "output" / "pending" / "0001.json",
        project / "data" / "progress.json",
    ]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    result = cli.resolve_from_args(args)
    assert result["created"]["status"] == "created"
    path = candidate_resolution_path(project / "data", 1)
    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()
    assert cli.resolve_from_args(args)["created"]["status"] == "unchanged"
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == before[path]
        for path in protected
    )
    assert len(result["verified"]["candidates"]) == 1
    assert result["verified"]["candidates"][0]["category"] == "consumable"
    loaded = load_candidate_resolution_record(path)
    assert loaded["record_sha256"] == candidate_resolution_record_sha256(loaded)
    assert capsys.readouterr().out == ""
